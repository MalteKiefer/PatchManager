import base64
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from patchmanager.accounts.models import Assignment, Role
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.api import tokens
from patchmanager.api.models import ApiToken
from patchmanager.dcim import devices
from patchmanager.dcim.models import Device, Rack
from patchmanager.locations.models import Building, Room
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def kit(make_tree, make_type, super_admin):
    t = make_tree(u_height=10)
    t["switch"] = make_type("Switch 8", u_height=1, ports=[{"name_pattern": "ge{n}", "count": 2}])
    t["ap"] = make_type(
        "AP", form="floor", u_height=0, ports=[{"name_pattern": "eth0", "count": 1}]
    )
    return t


def _token(user, scopes=("read", "write"), **kw) -> str:
    with rls.system_context():
        from django.test import RequestFactory

        request = RequestFactory().post("/")
        request.user = user
        return tokens.create_token(
            request, user, name="ci", scopes=list(scopes), days=kw.pop("days", 30), **kw
        )[1]


def _api(token: str) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_HOST="testserver")


def _get(client: Client, url: str, **params):
    return client.get(url, params, secure=True)


def _send(client: Client, method: str, url: str, data):
    return getattr(client, method)(url, data, content_type="application/json", secure=True)


def test_token_lifecycle(kit, super_admin) -> None:
    plain = _token(super_admin, ("read",))
    assert plain.startswith("pmat_")
    api = _api(plain)
    assert _get(api, "/api/v1/sites/").json()["results"][0]["name"] == "HQ"
    assert (
        _get(Client(HTTP_AUTHORIZATION="Bearer pmat_nope_x"), "/api/v1/sites/").status_code == 401
    )
    assert _get(Client(), "/api/v1/sites/").status_code in (401, 403)
    assert (
        _send(
            api, "post", "/api/v1/buildings/", {"site": str(kit["site"].pk), "name": "B2"}
        ).status_code
        == 403
    )
    with rls.system_context():
        token = ApiToken.objects.get(prefix=plain.split("_")[1])
        assert token.last_used_at is not None
        ApiToken.objects.filter(pk=token.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    assert _get(api, "/api/v1/sites/").status_code == 401
    with pytest.raises(ServiceError):
        _token(super_admin, ("admin",))
    with pytest.raises(ServiceError):
        _token(super_admin, days=400)


def test_crud_through_services(kit, super_admin) -> None:
    api = _api(_token(super_admin))
    site = str(kit["site"].pk)
    building = _send(api, "post", "/api/v1/buildings/", {"site": site, "name": "B2"})
    assert building.status_code == 201, building.content
    bid = building.json()["id"]
    floor = _send(
        api, "post", "/api/v1/floors/", {"building": bid, "name": "F1", "level": 1}
    ).json()
    room = _send(
        api, "post", "/api/v1/rooms/", {"floor": floor["id"], "name": "Lab", "kind": "server"}
    ).json()
    rack = _send(
        api, "post", "/api/v1/racks/", {"room": room["id"], "name": "L1", "u_height": 12}
    ).json()
    assert (
        _send(api, "patch", f"/api/v1/racks/{rack['id']}/", {"serial": "S1"}).json()["serial"]
        == "S1"
    )
    device = _send(
        api,
        "post",
        "/api/v1/devices/",
        {
            "device_type": str(kit["switch"].pk),
            "room": room["id"],
            "rack": rack["id"],
            "position": 3,
            "name": "sw9",
        },
    )
    assert device.status_code == 201, device.content
    assert (device.json()["position"], device.json()["face"]) == (3, "both") or device.json()[
        "position"
    ] == 3
    did = device.json()["id"]
    moved = _send(
        api,
        "post",
        f"/api/v1/devices/{did}/place/",
        {"room": room["id"], "rack": rack["id"], "position": 5},
    )
    assert moved.json()["position"] == 5
    taken = _send(
        api,
        "post",
        "/api/v1/devices/",
        {
            "device_type": str(kit["switch"].pk),
            "room": room["id"],
            "rack": rack["id"],
            "position": 5,
            "name": "sw10",
        },
    )
    assert taken.status_code == 400 and "detail" in taken.json()
    ports = _get(api, "/api/v1/ports/", device=did).json()["results"]
    assert [p["name"] for p in ports] == ["ge1", "ge2"]
    assert (
        _send(api, "patch", f"/api/v1/ports/{ports[0]['id']}/", {"label": "up"}).json()["label"]
        == "up"
    )
    cable = _send(
        api, "post", "/api/v1/cables/", {"a": ports[0]["id"], "b": ports[1]["id"], "label": "C1"}
    )
    assert cable.status_code == 201, cable.content
    cid = cable.json()["id"]
    assert set(cable.json()["ends"]) == {"a", "b"}
    assert (
        _send(api, "patch", f"/api/v1/cables/{cid}/", {"length_m": 2}).json()["length_m"] == "2.00"
    )  # decimals as strings
    vlan = _send(
        api, "post", "/api/v1/vlans/", {"tenant": str(kit["tenant"].pk), "vid": 30, "name": "Voice"}
    ).json()
    wlan = _send(
        api,
        "post",
        "/api/v1/wlans/",
        {"tenant": str(kit["tenant"].pk), "ssid": "Corp", "vlan": vlan["id"]},
    )
    assert wlan.status_code == 201, wlan.content
    assert (
        _send(api, "patch", f"/api/v1/wlans/{wlan.json()['id']}/", {"hidden": True}).json()[
            "hidden"
        ]
        is True
    )
    assert (
        _send(api, "patch", f"/api/v1/vlans/{vlan['id']}/", {"name": "VoIP"}).json()["name"]
        == "VoIP"
    )
    assert _get(api, "/api/v1/vlans/", vid=30).json()["count"] == 1
    assert _get(api, "/api/v1/changes/", object_type="dcim.rack").json()["count"] >= 2
    assert (
        _get(api, "/api/v1/search/", q="sw9", tenant=str(kit["tenant"].pk)).json()["results"][
            "device"
        ][0]["name"]
        == "sw9"
    )
    assert _get(api, "/api/v1/device-types/", q="switch").json()["count"] == 1
    assert _get(api, "/api/v1/vendors/").json()["count"] == 1
    assert _get(api, "/api/v1/tenants/").json()["count"] == 1
    for url in (
        f"/api/v1/cables/{cid}/",
        f"/api/v1/wlans/{wlan.json()['id']}/",
        f"/api/v1/vlans/{vlan['id']}/",
        f"/api/v1/devices/{did}/",
        f"/api/v1/racks/{rack['id']}/",
    ):
        assert api.delete(url, secure=True).status_code == 204, url
    with rls.system_context():
        assert not Device.objects.filter(pk=did).exists()
        rows = list(
            __import__("patchmanager.history.models", fromlist=["Change"]).Change.objects.filter(
                object_id=did
            )
        )
        assert rows[0].actor_label.startswith("root@example.com (API: ci)")
    assert _get(api, "/api/v1/racks/", room="nope").status_code == 400
    assert _send(
        api,
        "post",
        "/api/v1/buildings/",
        {"site": "00000000-0000-0000-0000-000000000000", "name": "x"},
    ).json() == {"site": ["Not found."]}


def test_scope_and_tenant_isolation(kit, make_tree, make_user, super_admin) -> None:
    other = make_tree(name="Elsewhere")
    user = make_user()
    with rls.system_context():
        secret = Room.objects.create(
            floor=kit["floor"], tenant=kit["tenant"], name="Vault", created_by=super_admin
        )
        devices.create(
            kit["request"], super_admin, device_type=kit["ap"], room=secret, name="hidden"
        )
        devices.create(
            kit["request"], super_admin, device_type=kit["ap"], room=kit["room"], name="seen"
        )
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=user,
            tenant=kit["tenant"],
            scope_path=kit["room"].path,
        )
    api = _api(_token(user))
    assert [d["name"] for d in _get(api, "/api/v1/devices/").json()["results"]] == ["seen"]
    assert _get(api, f"/api/v1/racks/{other['rack'].pk}/").status_code == 404
    assert _get(api, f"/api/v1/sites/{kit['site'].pk}/").status_code == 404  # grant below the site
    assert [p["name"] for p in _get(api, "/api/v1/ports/").json()["results"]] == ["eth0"]
    with rls.system_context():
        hidden_port = Device.objects.get(name="hidden").ports.get()
        seen_port = Device.objects.get(name="seen").ports.get()
    blocked = _send(
        api, "post", "/api/v1/cables/", {"a": str(seen_port.pk), "b": str(hidden_port.pk)}
    )
    assert blocked.status_code == 400 and blocked.json() == {"b": ["Not found."]}
    denied = _send(api, "post", "/api/v1/buildings/", {"site": str(kit["site"].pk), "name": "B9"})
    assert denied.status_code == 400  # site not visible to a room editor

    admin_other = _api(_token(super_admin, tenant=other["tenant"]))
    names = {s["name"] for s in _get(admin_other, "/api/v1/sites/").json()["results"]}
    assert names == {"Elsewhere"}


def test_oauth_client_credentials(kit, super_admin, admin_client: Client) -> None:
    from django.core.cache import cache

    cache.clear()
    with rls.system_context():
        client, secret = tokens.create_client(
            kit["request"], super_admin, name="Monitoring", scopes=["read"]
        )
        Assignment.objects.create(
            role=Role.objects.get(key="viewer"),
            user=client.user,
            tenant=kit["tenant"],
            scope_path=kit["tenant"].policy_path,
        )
    anonymous = Client()
    basic = base64.b64encode(f"{client.client_id}:{secret}".encode()).decode()
    response = anonymous.post(
        "/api/oauth/token/",
        {"grant_type": "client_credentials"},
        HTTP_AUTHORIZATION=f"Basic {basic}",
        secure=True,
    )
    assert response.status_code == 200 and response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3600 and body["scope"] == "read"
    api = _api(body["access_token"])
    assert _get(api, "/api/v1/racks/").json()["count"] == 1
    post = anonymous.post(
        "/api/oauth/token/",
        {
            "grant_type": "client_credentials",
            "client_id": client.client_id,
            "client_secret": secret,
            "scope": "read",
        },
        secure=True,
    )
    assert post.status_code == 200
    for data, headers, status, error in (
        ({"grant_type": "password"}, {}, 400, "unsupported_grant_type"),
        ({"grant_type": "client_credentials"}, {}, 401, "invalid_client"),
        (
            {
                "grant_type": "client_credentials",
                "client_id": client.client_id,
                "client_secret": "x",
            },
            {},
            401,
            "invalid_client",
        ),
        (
            {"grant_type": "client_credentials", "scope": "write"},
            {"HTTP_AUTHORIZATION": f"Basic {basic}"},
            400,
            "invalid_scope",
        ),
        (
            {"grant_type": "client_credentials"},
            {"HTTP_AUTHORIZATION": "Basic !!"},
            401,
            "invalid_client",
        ),
    ):
        reply = anonymous.post("/api/oauth/token/", data, secure=True, **headers)
        assert (reply.status_code, reply.json()["error"]) == (status, error)
    assert reply.status_code == 401 and "Basic" in reply["WWW-Authenticate"]

    with rls.system_context():
        new_secret = tokens.rotate_secret(kit["request"], super_admin, client)
    assert (
        _get(api, "/api/v1/racks/").status_code == 401
    )  # access tokens revoked with the old secret
    basic = base64.b64encode(f"{client.client_id}:{new_secret}".encode()).decode()
    fresh = anonymous.post(
        "/api/oauth/token/",
        {"grant_type": "client_credentials"},
        HTTP_AUTHORIZATION=f"Basic {basic}",
        secure=True,
    )
    assert fresh.status_code == 200
    with rls.system_context():
        tokens.revoke_client(kit["request"], super_admin, client)
    assert _get(_api(fresh.json()["access_token"]), "/api/v1/racks/").status_code == 401
    assert (
        anonymous.post(
            "/api/oauth/token/",
            {"grant_type": "client_credentials"},
            HTTP_AUTHORIZATION=f"Basic {basic}",
            secure=True,
        ).status_code
        == 401
    )
    assert anonymous.get("/api/oauth/token/", secure=True).status_code == 405


def test_oauth_rate_limit(monkeypatch, super_admin) -> None:
    from django.core.cache import cache

    from patchmanager.api import oauth

    cache.clear()
    monkeypatch.setattr(oauth, "ATTEMPTS_PER_IP", 1)
    anonymous = Client()
    anonymous.post("/api/oauth/token/", {"grant_type": "client_credentials"}, secure=True)
    assert (
        anonymous.post(
            "/api/oauth/token/", {"grant_type": "client_credentials"}, secure=True
        ).status_code
        == 429
    )


def test_service_users_get_no_mfa_roles(kit, super_admin, make_user) -> None:
    from patchmanager.accounts import policy
    from patchmanager.accounts.models import Group
    from patchmanager.accounts.services import rbac

    with rls.system_context():
        client, _secret = tokens.create_client(
            kit["request"], super_admin, name="Bot", scopes=["read", "write"]
        )
        with pytest.raises(ServiceError, match="MFA"):
            rbac.assign(
                kit["request"],
                super_admin,
                role=Role.objects.get(key="super_admin"),
                user=client.user,
            )
        group = Group.objects.create(name="Admins")
        group.members.add(client.user)
        Assignment.objects.create(
            role=Role.objects.get(key="super_admin"), group=group, scope_label="global"
        )
        policy.bump_version()
        assert not policy.is_super_admin(client.user)
        with pytest.raises(PermissionDeniedError):
            tokens.create_token(kit["request"], client.user, name="x", scopes=["read"], days=1)
    assert (
        Client()
        .post("/auth/login/", {"email": client.user.email, "password": ""}, secure=True)
        .status_code
        == 200
    )


def test_ui_pages(kit, admin_client: Client, super_admin, make_user) -> None:
    assert admin_client.get("/account/tokens/new/", secure=True).status_code == 200
    page = admin_client.post(
        "/account/tokens/new/",
        {"name": "laptop", "access": "read", "days": 7, "tenant": ""},
        secure=True,
        follow=True,
    )
    assert b"pmat_" in page.content and b"Copy it now" in page.content
    assert (
        b"Copy it now" not in admin_client.get("/account/tokens/", secure=True).content
    )  # shown once
    with rls.system_context():
        token = ApiToken.objects.get(name="laptop")
    admin_client.post(f"/account/tokens/{token.pk}/revoke/", secure=True)
    with rls.system_context():
        assert ApiToken.objects.get(pk=token.pk).revoked_at is not None
    assert admin_client.get("/manage/api-clients/new/", secure=True).status_code == 200
    created = admin_client.post(
        "/manage/api-clients/new/",
        {"name": "CMDB", "access": "write"},
        secure=True,
        follow=True,
    )
    assert b"client_secret" in created.content
    with rls.system_context():
        from patchmanager.api.models import ApiClient

        client = ApiClient.objects.get(name="CMDB")
    assert (
        b"client_secret"
        in admin_client.post(
            f"/manage/api-clients/{client.pk}/rotate/", secure=True, follow=True
        ).content
    )
    admin_client.post(f"/manage/api-clients/{client.pk}/revoke/", secure=True)
    assert (
        admin_client.post(f"/manage/api-clients/{client.pk}/rotate/", secure=True).status_code
        == 404
    )
    assert admin_client.get("/api/schema/", secure=True).status_code == 200
    viewer = make_user()
    other = Client()
    login(other, viewer)
    assert other.get("/manage/api-clients/", secure=True).status_code == 403
    assert other.post(f"/account/tokens/{token.pk}/revoke/", secure=True).status_code == 404


def test_session_auth_requires_csrf(kit, super_admin) -> None:
    from tests.conftest import add_totp

    client = Client()
    login(client, super_admin, totp=add_totp(super_admin))
    client.handler.enforce_csrf_checks = True
    assert client.get("/api/v1/sites/", secure=True).status_code == 200
    response = client.post(
        "/api/v1/buildings/",
        {"site": str(kit["site"].pk), "name": "X"},
        content_type="application/json",
        secure=True,
    )
    assert response.status_code == 403
    with rls.system_context():
        assert not Building.objects.filter(name="X").exists()
        assert Rack.objects.count() == 1


def test_rotate_needs_the_clients_rights_and_no_tokens_while_impersonating(
    kit, super_admin, make_user
) -> None:
    from django.test import RequestFactory

    from patchmanager.accounts import policy
    from patchmanager.tenancy import services as tenancy

    manager = make_user()
    with rls.system_context():
        client, _secret = tokens.create_client(
            kit["request"], super_admin, name="Bot", scopes=["read"]
        )
        Assignment.objects.create(
            role=Role.objects.get(key="admin"),
            user=client.user,
            tenant=kit["tenant"],
            scope_path=kit["tenant"].policy_path,
        )
        Assignment.objects.create(
            role=Role.objects.get(key="tenant_owner"), user=manager, scope_label="global"
        )
        policy.bump_version()
        assert policy.has_perm(manager, "accounts.apiclient.manage")
        Assignment.objects.filter(user=manager).delete()
        Assignment.objects.create(
            role=Role.objects.get(key="viewer"), user=manager, scope_label="global"
        )
        custom = Role.objects.create(
            key="client_keeper", name="Client keeper", permissions=["accounts.apiclient.manage"]
        )
        Assignment.objects.create(role=custom, user=manager, scope_label="global")
        policy.bump_version()
        with pytest.raises(PermissionDeniedError):
            tokens.rotate_secret(
                kit["request"], manager, client
            )  # the client is an admin, the manager is not

        request = RequestFactory().post("/")
        request.user = super_admin
        request.impersonator = manager
        with pytest.raises(PermissionDeniedError, match="acting as"):
            tokens.create_token(request, super_admin, name="x", scopes=["read"], days=1)

        assert policy.has_perm(client.user, "dcim.rack.view", kit["rack"])
        tenancy.update_tenant(kit["request"], super_admin, kit["tenant"], requires_mfa=True)
        client.user.refresh_from_db()
        assert not policy.has_perm(
            client.user, "dcim.rack.view", kit["rack"]
        )  # no second factor, no MFA tenant


def test_api_clients_never_reach_mfa_tenants(kit, make_tree, super_admin) -> None:
    from patchmanager.accounts import policy
    from patchmanager.tenancy import services as tenancy

    other = make_tree(name="Strict")
    with rls.system_context():
        client, secret = tokens.create_client(
            kit["request"], super_admin, name="Bot", scopes=["read"]
        )
        Assignment.objects.create(
            role=Role.objects.get(key="viewer"), user=client.user, scope_label="global"
        )
        tenancy.update_tenant(kit["request"], super_admin, other["tenant"], requires_mfa=True)
        policy.bump_version()
        all_tenants, ids = policy.accessible_tenants(client.user)
        assert all_tenants is False and ids == {kit["tenant"].pk}
    rls.clear_context()  # as in a request before token authentication
    assert policy.accessible_tenants(client.user) == (False, {kit["tenant"].pk})
    import base64

    basic = base64.b64encode(f"{client.client_id}:{secret}".encode()).decode()
    token = (
        Client()
        .post(
            "/api/oauth/token/",
            {"grant_type": "client_credentials"},
            HTTP_AUTHORIZATION=f"Basic {basic}",
            secure=True,
        )
        .json()["access_token"]
    )
    names = {s["name"] for s in _get(_api(token), "/api/v1/sites/").json()["results"]}
    assert names == {"HQ"}


def test_rotate_checks_unfiltered_grants(kit, super_admin, make_user) -> None:
    from patchmanager.accounts import policy

    manager = make_user()
    with rls.system_context():
        client, _secret = tokens.create_client(
            kit["request"], super_admin, name="Bot", scopes=["read"]
        )
        Assignment.objects.create(
            role=Role.objects.get(key="super_admin"), user=client.user, scope_label="global"
        )
        custom = Role.objects.create(
            key="keeper", name="Keeper", permissions=["accounts.apiclient.manage"]
        )
        Assignment.objects.create(role=custom, user=manager, scope_label="global")
        policy.bump_version()
        assert not policy.is_super_admin(
            client.user
        )  # filtered, but it would count if the rule changed
        with pytest.raises(PermissionDeniedError):
            tokens.rotate_secret(kit["request"], manager, client)


def test_filters_search_fields_and_shared_types_for_scoped_users(
    kit, make_user, super_admin
) -> None:
    user = make_user()
    with rls.system_context():
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=user,
            tenant=kit["tenant"],
            scope_path=kit["room"].path,
        )
    api = _api(_token(user))
    created = _send(
        api,
        "post",
        "/api/v1/devices/",
        {"device_type": str(kit["ap"].pk), "room": str(kit["room"].pk), "name": "ap7"},
    )
    assert created.status_code == 201, created.content  # shared library type, room scoped user
    admin = _api(_token(super_admin))
    assert _get(admin, "/api/v1/cables/", q="x").status_code == 200
    assert _get(admin, "/api/v1/wlans/", q="x").status_code == 200
    assert _get(admin, "/api/v1/vlans/", vid="abc").status_code == 400
    assert _get(admin, "/api/v1/changes/", since="yesterday").status_code == 400
