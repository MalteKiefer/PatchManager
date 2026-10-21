import pytest
from django.test import Client

from patchmanager.accounts.models import Assignment, Role
from patchmanager.dcim import devices
from patchmanager.locations import services as tree
from patchmanager.locations.models import Room
from patchmanager.metadata import services as metadata
from patchmanager.networks import services as networks
from patchmanager.search import services
from patchmanager.search.models import SavedSearch
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def kit(make_tree, make_type, super_admin):
    t = make_tree()
    switch = make_type("Switch 48")
    with rls.system_context():
        secret = tree.create(t["request"], super_admin, Room, parent=t["floor"], name="Vault")
        devices.create(t["request"], super_admin, device_type=switch, room=t["room"], name="core-sw-01", serial="FDO1234")
        devices.create(t["request"], super_admin, device_type=switch, room=secret, name="core-sw-02")
        networks.create_vlan(t["request"], super_admin, tenant=t["tenant"], vid=120, name="Voice")
        metadata.add_note(t["request"], super_admin, t["rack"], "Replaced the PDU fuses in March")
    t["secret"] = secret
    return t


def test_search_by_name_serial_typo_vid_and_note(kit, super_admin) -> None:
    def find(q: str, kind: str = "") -> dict:
        with rls.system_context():
            return services.search(super_admin, kit["tenant"], q, kind=kind)[0]

    assert [str(h.obj) for h in find("core-sw")["device"]] == ["core-sw-01", "core-sw-02"]
    assert str(find("FDO1234")["device"][0].obj) == "core-sw-01"
    assert "core-sw-01" in [str(h.obj) for h in find("core-sv-01")["device"]]  # typo, trigram
    assert str(find("120")["vlan"][0].obj).startswith("120") or find("120")["vlan"][0].obj.vid == 120
    assert find("fuses")["note"][0].obj == kit["rack"]
    assert set(find("core", kind="device")) == {"device"}
    assert find("x") == {}


def test_search_respects_scope(kit, make_user) -> None:
    user = make_user()
    with rls.system_context():
        Assignment.objects.create(role=Role.objects.get(key="viewer"), user=user, tenant=kit["tenant"],
                                  scope_path=kit["room"].path)
        results, counts = services.search(user, kit["tenant"], "core-sw")
        assert [str(h.obj) for h in results["device"]] == ["core-sw-01"]
        assert counts["device"] == 1
        assert "vlan" not in services.search(user, kit["tenant"], "Voice")[0]  # tenant VLAN, room grant
        assert "note" in services.search(user, kit["tenant"], "fuses")[0]


def test_search_views_and_saved(kit, admin_client: Client, super_admin, make_user) -> None:
    page = admin_client.get("/search/?q=core-sw", secure=True).content.decode()
    assert "core-sw-01" in page and "Filter by type" in page
    assert "core-sw-01" in admin_client.get("/search/?q=core&type=device", secure=True).content.decode()
    admin_client.post("/search/saved/", {"name": "Switches", "q": "core-sw", "type": "device"}, secure=True)
    again = admin_client.post("/search/saved/", {"name": "Switches", "q": "x1", "type": "bogus"}, secure=True, follow=True)
    assert "exists" in again.content.decode()
    saved = SavedSearch.objects.get(user=super_admin)
    assert (saved.query, saved.kind) == ("core-sw", "device")
    other = make_user()
    client = Client()
    login(client, other)
    assert client.post(f"/search/saved/{saved.pk}/delete/", secure=True).status_code == 404
    assert "core-sw-01" not in client.get("/search/?q=core-sw", secure=True).content.decode()
    admin_client.post(f"/search/saved/{saved.pk}/delete/", secure=True)
    assert not SavedSearch.objects.exists()
