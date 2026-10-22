import pytest
from django.test import Client

from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.dcim import devices
from patchmanager.tenancy import rls
from tests.test_api import _api, _get, _send, _token

pytestmark = pytest.mark.django_db


@pytest.fixture
def two_sites(make_tree, make_type, super_admin):
    from patchmanager.locations import services
    from patchmanager.locations.models import Building, Floor, Room, Site

    a = make_tree()
    with rls.system_context():
        site = services.create(a["request"], super_admin, Site, parent=a["tenant"], name="Branch")
        building = services.create(a["request"], super_admin, Building, parent=site, name="B2")
        floor = services.create(
            a["request"], super_admin, Floor, parent=building, name="F", level=0
        )
        room = services.create(a["request"], super_admin, Room, parent=floor, name="R")
        model = make_type("AP-2", form="floor", u_height=0)
        here = devices.create(
            a["request"], super_admin, device_type=model, room=a["room"], name="AP-HQ"
        )
        there = devices.create(
            a["request"], super_admin, device_type=model, room=room, name="AP-BR"
        )
    return {**a, "branch": site, "branch_room": room, "here": here, "there": there, "model": model}


def test_location_limit(two_sites, super_admin) -> None:
    api = _api(_token(super_admin, ("read", "write"), scope=two_sites["site"]))
    names = [d["name"] for d in _get(api, "/api/v1/devices/").json()["results"]]
    assert names == ["AP-HQ"]  # even a super admin token sees only its location
    assert _get(api, f"/api/v1/devices/{two_sites['there'].pk}/").status_code == 404
    sites = [s["name"] for s in _get(api, "/api/v1/sites/").json()["results"]]
    assert sites == ["HQ"]
    moved = _send(api, "patch", f"/api/v1/devices/{two_sites['here'].pk}/", {"name": "AP-HQ-1"})
    assert moved.status_code == 200
    outside = _send(
        api,
        "post",
        "/api/v1/devices/",
        {
            "name": "X",
            "device_type": str(two_sites["model"].pk),
            "room": str(two_sites["branch_room"].pk),
        },
    )
    assert outside.status_code in (400, 403)


def test_object_type_limit(two_sites, super_admin) -> None:
    api = _api(_token(super_admin, ("read",), object_types=["locations.site"]))
    assert len(_get(api, "/api/v1/sites/").json()["results"]) == 2
    assert _get(api, "/api/v1/devices/").json()["results"] == []
    assert _get(api, f"/api/v1/devices/{two_sites['here'].pk}/").status_code == 404


def test_limits_are_checked_when_created(two_sites, make_user, grant) -> None:
    from patchmanager.locations.models import Site

    stranger = make_user()
    with pytest.raises(PermissionDeniedError):
        _token(stranger, ("read",), scope=two_sites["branch"])
    with pytest.raises(ServiceError):
        _token(stranger, ("read",), object_types=["no.such"])
    with rls.system_context():
        assert Site.objects.count() == 2


def test_token_page_offers_limits(admin_client: Client, two_sites, system) -> None:
    from patchmanager.api.models import ApiToken

    page = admin_client.get("/account/tokens/new/", secure=True).content.decode()
    assert "dcim.device" in page and "Branch" in page
    key = f"site:{two_sites['branch'].pk}"
    admin_client.post(
        "/account/tokens/new/",
        {
            "name": "branch",
            "access": "read",
            "days": 30,
            "tenant": "",
            "object_types": ["dcim.device"],
            "scope": key,
        },
        secure=True,
    )
    with system():
        token = ApiToken.objects.get(name="branch")
        assert token.object_types == ["dcim.device"] and token.scope_path == str(
            two_sites["branch"].path
        )
        assert token.tenant_id == two_sites["tenant"].pk
    listing = admin_client.get("/account/tokens/", secure=True).content.decode()
    assert "one location only" in listing


def test_token_form_groups_types_and_write_includes_read(admin_client) -> None:
    page = admin_client.get("/account/tokens/new/", secure=True).content.decode()
    assert "Racks and devices" in page and 'data-group="dcim"' in page
    client_page = admin_client.get("/manage/api-clients/new/", secure=True).content.decode()
    assert 'aria-label="Administration"' in client_page  # admin side menu
    admin_client.post("/manage/api-clients/new/", {"name": "CMDB", "access": "write"}, secure=True)
    from patchmanager.api.models import ApiClient

    assert sorted(ApiClient.objects.get(name="CMDB").scopes) == ["read", "write"]
