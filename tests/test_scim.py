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
    call(scim, "post", "Users", {"userName": "grace@example.com"})
    page = call(scim, "get", "Users?startIndex=2&count=1")[1]
    assert (page["startIndex"], page["itemsPerPage"], page["totalResults"]) == (2, 1, 2)
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


def _provision(scim, email: str) -> str:
    status, body, _r = call(scim, "post", "Users", {"userName": email})
    assert status == 201, body
    return str(body["id"])


def test_local_and_protected_accounts_are_out_of_reach(scim, super_admin, make_user) -> None:
    local = make_user("local@example.com")
    for target in (super_admin, local):
        assert (
            call(scim, "get", f"Users/{target.pk}")[0] == 404
        )  # SCIM only sees what it provisioned
        assert call(scim, "delete", f"Users/{target.pk}")[0] == 404
    assert (
        call(scim, "post", "Users", {"userName": "local@example.com"})[1]["scimType"]
        == "uniqueness"
    )
    uid = _provision(scim, "glass@example.com")
    User.objects.filter(pk=uid).update(break_glass=True)
    assert call(scim, "put", f"Users/{uid}", {"userName": "glass@example.com"})[0] == 403
    # a locked super admin is still protected (judged by assignments, not by usability)
    boss = _provision(scim, "boss@example.com")
    Assignment.objects.create(
        role=Role.objects.get(key="super_admin"), user_id=boss, scope_label="global"
    )
    User.objects.filter(pk=boss).update(is_active=False)
    patch = {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": True}]}
    assert call(scim, "patch", f"Users/{boss}", patch)[0] == 403
    assert super_admin.is_active


def test_deactivation_signs_out(scim) -> None:
    user = User.objects.get(pk=_provision(scim, "bob@example.com"))
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
    a = User.objects.get(pk=_provision(scim, "a@example.com"))
    b = User.objects.get(pk=_provision(scim, "b@example.com"))
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


def test_reactivation_rules_and_clean_revival(scim) -> None:
    from patchmanager.sso.models import ExternalIdentity, IdentityProvider

    uid = _provision(scim, "carol@example.com")
    user = User.objects.get(pk=uid)
    user.is_active = False  # an administrator deactivated the account
    user.deactivated_at = user.created_at
    user.save()
    blocked = call(
        scim,
        "patch",
        f"Users/{uid}",
        {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": True}]},
    )
    assert blocked[0] == 403 and "administrator" in blocked[1]["detail"]
    User.objects.filter(pk=uid).update(is_active=True, deactivated_at=None)
    provider = IdentityProvider.objects.create(
        name="Corp", slug="corp", kind="oidc", domains=["example.com"]
    )
    ExternalIdentity.objects.create(user_id=uid, provider=provider, subject="s-1")
    call(
        scim,
        "patch",
        f"Users/{uid}",
        {
            "schemas": [PATCH],
            "Operations": [{"op": "replace", "path": "userName", "value": "carol.new@example.com"}],
        },
    )
    assert not ExternalIdentity.objects.filter(
        user_id=uid
    ).exists()  # the old SSO link goes with the address
    Assignment.objects.create(
        role=Role.objects.get(key="editor"), user_id=uid, scope_label="global"
    )
    assert call(scim, "delete", f"Users/{uid}")[0] == 204
    assert not Assignment.objects.filter(user_id=uid).exists()
    revived = call(scim, "post", "Users", {"userName": "carol.new@example.com"})
    assert revived[0] == 201 and revived[1]["id"] == uid and revived[1]["active"] is True


def test_token_domains(admin_client: Client) -> None:
    created = admin_client.post(
        "/manage/scim/", {"name": "Acme", "domains": "acme.example"}, secure=True, follow=True
    )
    acme = Client(HTTP_AUTHORIZATION=f"Bearer {created.context['new_token']}")
    assert call(acme, "post", "Users", {"userName": "x@other.example"})[0] == 403
    status, body, _r = call(acme, "post", "Users", {"userName": "x@acme.example"})
    assert status == 201
    moved = call(
        acme,
        "patch",
        f"Users/{body['id']}",
        {
            "schemas": [PATCH],
            "Operations": [{"op": "replace", "path": "userName", "value": "x@other.example"}],
        },
    )
    assert moved[0] == 403
    assert (
        b"domains"
        in admin_client.post(
            "/manage/scim/", {"name": "Bad", "domains": "me@x.example"}, secure=True
        ).content
    )


def test_groups_belong_to_their_token_and_admin_decisions_stick(
    scim, admin_client: Client, super_admin
) -> None:
    from django.test import RequestFactory

    from patchmanager.accounts.services import users

    _status, body, _r = call(scim, "post", "Groups", {"displayName": "Team A"})
    other_token = admin_client.post(
        "/manage/scim/", {"name": "Okta"}, secure=True, follow=True
    ).context["new_token"]
    other = Client(HTTP_AUTHORIZATION=f"Bearer {other_token}")
    assert call(other, "get", f"Groups/{body['id']}")[0] == 404
    assert call(other, "get", "Groups")[1]["totalResults"] == 0
    uid = _provision(scim, "dan@example.com")
    call(
        scim,
        "patch",
        f"Users/{uid}",
        {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    user = User.objects.get(pk=uid)
    request = RequestFactory().post("/")
    request.user = super_admin
    users.set_active(request, super_admin, user, active=True)
    users.set_active(request, super_admin, user, active=False)  # now the administrator's decision
    blocked = call(
        scim,
        "patch",
        f"Users/{uid}",
        {"schemas": [PATCH], "Operations": [{"op": "replace", "path": "active", "value": True}]},
    )
    assert blocked[0] == 403
