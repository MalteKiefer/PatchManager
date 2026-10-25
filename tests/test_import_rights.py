"""Importing needs the import right of its object type; the API applies token limits."""

import pytest
from django.test import Client

from patchmanager.accounts.models import Role
from patchmanager.accounts.roles import SYSTEM_ROLES
from patchmanager.accounts.services import PermissionDeniedError
from patchmanager.dcim import devices
from patchmanager.ipam import dns_import
from patchmanager.library import interchange
from patchmanager.networks import switchconfig
from patchmanager.networks.platforms import common
from patchmanager.tenancy import rls
from tests.conftest import login
from tests.test_api import _api, _get, _token

pytestmark = pytest.mark.django_db

TYPE = "manufacturer: Acme\nmodel: Box\nu_height: 1\n"


@pytest.fixture
def no_import(make_tree, make_user, grant):
    """An editor without any import right."""
    Role.objects.create(
        key="no-import",
        name="No import",
        permissions=sorted(
            p for p in SYSTEM_ROLES["editor"].permissions() if not p.endswith(".import")
        ),
    )
    tree = make_tree()
    user = make_user()
    grant(user, "no-import", tree["tenant"])
    return {**tree, "user": user}


def test_importers_need_the_import_right(no_import, make_type, super_admin) -> None:
    t, user = no_import, no_import["user"]
    with rls.system_context():
        device = devices.create(
            t["request"], super_admin, device_type=make_type("Switch"), room=t["room"]
        )
        with pytest.raises(PermissionDeniedError):
            dns_import.save(
                t["request"],
                user,
                t["tenant"],
                dns_import.Zone("example.org"),
                replace=False,
                source="test",
            )
        with pytest.raises(PermissionDeniedError):
            interchange.import_type(t["request"], user, TYPE, tenant=t["tenant"])
        with pytest.raises(PermissionDeniedError):
            switchconfig.apply(
                t["request"], user, device, common.Config(vendor="test"), commit=False
            )
        # The same user may still create and change these objects by hand.
        assert dns_import.policy.has_perm(user, "ipam.dnszone.add", device)


def test_import_pages_need_the_import_right(client: Client, no_import, grant) -> None:
    t, user = no_import, no_import["user"]
    login(client, user)
    client.post("/tenants/switch/", {"tenant": t["tenant"].pk}, secure=True)
    for url in ("/ipam/dns/import/", "/compute/import/"):
        assert client.get(url, secure=True).status_code == 403, url
    grant(user, "editor", t["tenant"])
    for url in ("/ipam/dns/import/", "/compute/import/"):
        assert client.get(url, secure=True).status_code == 200, url


def test_library_and_tenant_endpoints_follow_token_limits(
    make_tree, make_type, super_admin
) -> None:
    make_tree()
    make_type("Switch")
    full = _api(_token(super_admin, ("read",)))
    for url in ("/api/v1/vendors/", "/api/v1/device-types/", "/api/v1/tenants/"):
        assert _get(full, url).json()["results"], url
    sites_only = _api(_token(super_admin, ("read",), object_types=["locations.site"]))
    for url in ("/api/v1/vendors/", "/api/v1/device-types/", "/api/v1/tenants/"):
        assert _get(sites_only, url).json()["results"] == [], url
