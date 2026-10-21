import json

import pytest
from django.test import Client

from patchmanager.accounts.models import Assignment, Group, Role, User, UserSession
from patchmanager.scim import views
from patchmanager.scim.models import ScimToken

pytestmark = pytest.mark.django_db
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@pytest.fixture
def scim(admin_client: Client):
    created = admin_client.post("/manage/scim/", {"name": "Entra"}, secure=True, follow=True)
    plain = created.context["new_token"]
    assert plain.startswith("pmsc_")
    return Client(HTTP_AUTHORIZATION=f"Bearer {plain}")


def call(client: Client, method: str, url: str, body=None, **headers):
    data = json.dumps(body) if body is not None else None
    response = getattr(client, method)(
        f"/scim/v2/{url}", data, content_type="application/scim+json", secure=True, **headers
    )
    return response.status_code, (response.json() if response.content else None), response


def test_discovery_and_auth(scim) -> None:
    status, body, response = call(Client(), "get", "Users")
    assert status == 401 and body["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert response["Content-Type"] == "application/scim+json"
    assert call(scim, "get", "ServiceProviderConfig")[1]["patch"]["supported"] is True
    assert [r["name"] for r in call(scim, "get", "ResourceTypes")[1]["Resources"]] == [
        "User",
        "Group",
    ]
    assert call(scim, "get", "Schemas")[1]["totalResults"] == 2
    token = ScimToken.objects.get()
    assert token.last_used_at is not None
    token.revoked_at = token.created_at
    token.save()
    assert call(scim, "get", "Users")[0] == 401


def test_user_lifecycle(scim, super_admin) -> None:
    status, body, response = call(
        scim,
        "post",
        "Users",
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "Ada@Example.com",
            "name": {"givenName": "Ada", "familyName": "Lovelace"},
            "externalId": "e-1",
            "active": True,
        },
    )
    assert status == 201, body
    uid = body["id"]
    assert body["userName"] == "ada@example.com" and body["displayName"] == "Ada Lovelace"
    assert response["ETag"] == body["meta"]["version"] and response["Location"].endswith(
        f"/Users/{uid}"
    )
    assert (
        call(scim, "post", "Users", {"userName": "ada@example.com"})[1]["scimType"] == "uniqueness"
    )
    listed = call(scim, "get", 'Users?filter=userName eq "ADA@example.com"')[1]
    assert listed["totalResults"] == 1 and listed["Resources"][0]["id"] == uid
    assert (
        call(scim, "get", 'Users?filter=externalId eq "e-1" and active eq true')[1]["totalResults"]
        == 1
    )
    assert call(scim, "get", 'Users?filter=password eq "x"')[1]["scimType"] == "invalidFilter"
    page = call(scim, "get", "Users?startIndex=2&count=1")[1]
    assert (
        page["startIndex"] == 2 and page["itemsPerPage"] == 1 and page["totalResults"] == 2
    )  # with root
    # Entra style PATCH with path and string boolean
    status, body, _r = call(
        scim,
        "patch",
        f"Users/{uid}",
        {
            "schemas": [PATCH],
            "Operations": [
                {"op": "Replace", "path": "displayName", "value": "Countess"},
                {"op": "Replace", "path": "active", "value": "False"},
            ],
        },
    )
    assert status == 200 and body["displayName"] == "Countess" and body["active"] is False
    user = User.objects.get(pk=uid)
    assert user.deactivated_at is not None and not user.is_active
    # Okta style PATCH without path
    status, body, _r = call(
        scim,
        "patch",
        f"Users/{uid}",
        {"schemas": [PATCH], "Operations": [{"op": "replace", "value": {"active": True}}]},
    )
    assert body["active"] is True
    stale = call(
        scim, "put", f"Users/{uid}", {"userName": "ada@example.com"}, HTTP_IF_MATCH='W/"1"'
    )
    assert stale[0] == 412
    status, body, _r = call(
        scim,
        "put",
        f"Users/{uid}",
        {"userName": "ada@example.com", "displayName": "Ada", "externalId": "e-1"},
        HTTP_IF_MATCH=body["meta"]["version"],
    )
    assert status == 200 and body["displayName"] == "Ada"
    assert call(scim, "delete", f"Users/{uid}")[0] == 204
    assert call(scim, "get", f"Users/{uid}")[0] == 404
    assert not User.objects.get(pk=uid).is_active
    revived = call(scim, "post", "Users", {"userName": "ada@example.com"})
    assert revived[0] == 201 and revived[1]["id"] == uid and revived[1]["active"] is True
    assert call(scim, "get", "Users/not-a-uuid")[0] == 404


def test_protected_accounts(scim, super_admin, make_user) -> None:
    status, _body, _r = call(
        scim,
        "patch",
        f"Users/{super_admin.pk}",
        {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert status == 403
    assert call(scim, "delete", f"Users/{super_admin.pk}")[0] == 403
    glass = make_user("glass@example.com", break_glass=True)
    assert call(scim, "put", f"Users/{glass.pk}", {"userName": "x@example.com"})[0] == 403
    assert super_admin.is_active


def test_deactivation_signs_out(scim, make_user) -> None:
    user = make_user("bob@example.com")
    UserSession.objects.create(
        session_key="k" * 32, user=user, session_data="", expire_date="2999-01-01T00:00:00Z"
    )
    call(
        scim,
        "patch",
        f"Users/{user.pk}",
        {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    assert not UserSession.objects.filter(user=user).exists()


def test_groups(scim, make_user) -> None:
    local = Group.objects.create(name="Admins")  # not managed by SCIM: invisible and untouchable
    a, b = make_user("a@example.com"), make_user("b@example.com")
    status, body, _r = call(
        scim,
        "post",
        "Groups",
        {"displayName": "Network team", "externalId": "g-1", "members": [{"value": str(a.pk)}]},
    )
    assert status == 201, body
    gid = body["id"]
    assert [m["value"] for m in body["members"]] == [str(a.pk)]
    assert call(scim, "get", "Groups")[1]["totalResults"] == 1
    assert call(scim, "get", f"Groups/{local.pk}")[0] == 404
    assert call(scim, "get", 'Groups?filter=displayName eq "network team"')[1]["totalResults"] == 1
    assert call(scim, "get", f'Groups?filter=members[value eq "{a.pk}"]')[1]["totalResults"] == 1
    status, body, _r = call(
        scim,
        "patch",
        f"Groups/{gid}",
        {
            "schemas": [PATCH],
            "Operations": [
                {"op": "add", "path": "members", "value": [{"value": str(b.pk)}]},
                {"op": "remove", "path": f'members[value eq "{a.pk}"]'},
                {"op": "replace", "path": "displayName", "value": "Net team"},
            ],
        },
    )
    assert (
        status == 200
        and [m["value"] for m in body["members"]] == [str(b.pk)]
        and body["displayName"] == "Net team"
    )
    nested = call(
        scim,
        "patch",
        f"Groups/{gid}",
        {
            "schemas": [PATCH],
            "Operations": [
                {
                    "op": "add",
                    "path": "members",
                    "value": [{"value": str(local.pk), "type": "Group"}],
                }
            ],
        },
    )
    assert nested[0] == 400
    unknown = call(
        scim,
        "post",
        "Groups",
        {"displayName": "X", "members": [{"value": "00000000-0000-0000-0000-000000000000"}]},
    )
    assert unknown[0] == 400
    assert call(scim, "post", "Groups", {"displayName": "net team"})[1]["scimType"] == "uniqueness"
    assert call(scim, "get", f"Users/{b.pk}")[1]["groups"][0]["display"] == "Net team"
    call(scim, "put", f"Groups/{gid}", {"displayName": "Net team", "members": []})
    assert not Group.objects.get(pk=gid).members.exists()
    # a role the admin gave the group applies to its members
    Assignment.objects.create(
        role=Role.objects.get(key="viewer"), group_id=gid, scope_label="global"
    )
    assert call(scim, "delete", f"Groups/{gid}")[0] == 204
    assert not Group.objects.filter(pk=gid).exists() and Group.objects.filter(pk=local.pk).exists()


def test_bad_requests_and_rate_limit(scim, monkeypatch) -> None:
    from django.core.cache import cache

    bad = scim.post("/scim/v2/Users", "not json", content_type="application/scim+json", secure=True)
    assert bad.status_code == 400 and bad.json()["scimType"] == "invalidSyntax"
    assert (
        call(scim, "post", "Users", {"userName": "not-an-email"})[1]["scimType"] == "invalidValue"
    )
    assert call(scim, "get", "Users?count=x")[0] == 400
    cache.clear()
    monkeypatch.setattr(views, "REQUESTS_PER_MINUTE", 1)
    call(scim, "get", "Users")
    assert call(scim, "get", "Users")[0] == 429


def test_token_admin(admin_client: Client, make_user) -> None:
    admin_client.post("/manage/scim/", {"name": "Okta"}, secure=True)
    token = ScimToken.objects.get(name="Okta")
    assert b"Copy it now" in admin_client.get("/manage/scim/", secure=True).content
    assert (
        b"Copy it now" not in admin_client.get("/manage/scim/", secure=True).content
    )  # shown once
    admin_client.post(f"/manage/scim/{token.pk}/revoke/", secure=True)
    token.refresh_from_db()
    assert token.revoked_at is not None
    from tests.conftest import login

    other = Client()
    login(other, make_user())
    assert other.get("/manage/scim/", secure=True).status_code == 403
