import pytest
from django.test import Client

from patchmanager.cabling import services as cabling
from patchmanager.dcim import devices
from patchmanager.locations import rooms
from patchmanager.locations.models import Room, RoomNeed
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db


@pytest.fixture
def office(make_tree, make_type):
    t = make_tree()
    request, actor = t["request"], t["request"].user
    with rls.system_context():
        room = t["room"]
        room.kind_id = "office"
        room.save()
        outlet = make_type(
            "Double outlet",
            form="floor",
            u_height=0,
            category="outlet",
            ports=[{"name_pattern": "A{n}", "kind": "front", "count": 2}],
        )
        panel = make_type(
            "Panel 2",
            category="patch_panel",
            full_depth=False,
            ports=[{"name_pattern": "R{n}", "kind": "rear", "count": 2}],
        )
        o1 = devices.create(request, actor, device_type=outlet, room=room, name="O-01")
        pp = devices.create(
            request, actor, device_type=panel, room=room, rack=t["rack"], position=1, name="PP1"
        )
        cabling.connect(request, actor, o1.ports.get(name="A1"), pp.ports.get(name="R1"))
    return {**t, "outlet": o1, "panel": pp}


def test_plan_against_installed(office, system) -> None:
    with system():
        room = Room.objects.get(pk=office["room"].pk)
        plan = {r.category.key: r for r in rooms.plan(office["request"].user, room)}
        assert (plan["outlet"].planned, plan["outlet"].installed, plan["outlet"].status) == (
            4,
            1,
            "missing",
        )
        assert plan["access_point"].missing == 1
        assert plan["patch_panel"].status == "unplanned"
        rooms.set_needs(office["request"], office["request"].user, room, {"outlet": 1})
        assert RoomNeed.objects.get(room=room).count == 1
        plan = {r.category.key: r for r in rooms.plan(office["request"].user, room)}
        assert plan["outlet"].status == "done" and plan["outlet"].own


def test_room_page_shows_plan_and_patch_targets(admin_client: Client, office) -> None:
    url = f"/locations/room/{office['room'].pk}/"
    page = admin_client.get(url, secure=True).content.decode()
    assert "Planned and installed" in page and "3 missing" in page
    assert "O-01" in page and "PP1 · R1" in page and "A01" in page
    assert admin_client.get(url + "plan/", secure=True).status_code == 200
    admin_client.post(url + "plan/", {"need-outlet": "1", "need-access_point": "0"}, secure=True)
    page = admin_client.get(url, secure=True).content.decode()
    assert "3 missing" not in page
    bad = admin_client.post(url + "plan/", {"need-outlet": "x"}, secure=True)
    assert "whole numbers" in bad.content.decode()
