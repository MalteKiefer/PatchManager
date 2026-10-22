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


def test_surveys(admin_client: Client, office, system) -> None:
    from patchmanager.locations.models import RoomSurvey
    from patchmanager.metadata import services as metadata
    from patchmanager.metadata.models import Note

    room = office["room"]
    url = f"/locations/room/{room.pk}/surveys/new/"
    page = admin_client.get(url, secure=True).content.decode()
    assert 'name="found-outlet"' in page and "Planned" in page
    saved = admin_client.post(
        url,
        {
            "surveyed_on": "2026-10-20",
            "surveyor": "Kim",
            "status": "done",
            "notes": "Two outlets behind the cupboard.",
            "found-outlet": "3",
            "found-access_point": "",
        },
        secure=True,
    )
    with system():
        survey = RoomSurvey.objects.get()
    assert saved.url == f"/locations/surveys/{survey.pk}/"
    assert survey.counts == {"outlet": 3}
    detail = admin_client.get(saved.url, secure=True).content.decode()
    assert "differs" in detail  # 3 found, 1 documented
    room_page = admin_client.get(f"/locations/room/{room.pk}/", secure=True).content.decode()
    assert "Kim" in room_page
    bad = admin_client.post(
        saved.url,
        {"surveyed_on": "2026-10-20", "surveyor": "K", "status": "done", "found-outlet": "lots"},
        secure=True,
    )
    assert "whole numbers" in bad.content.decode()
    with system():
        metadata.add_note(office["request"], office["request"].user, survey, "behind the door")
    admin_client.post(saved.url, {"action": "delete"}, secure=True)
    with system():
        assert not RoomSurvey.objects.exists() and not Note.objects.exists()


def test_surveys_need_change_permission(client: Client, office, make_user, grant) -> None:
    from tests.conftest import login

    viewer = make_user()
    grant(viewer, "viewer", office["tenant"])
    login(client, viewer)
    url = f"/locations/room/{office['room'].pk}/surveys/new/"
    assert client.get(url, secure=True).status_code == 403


def test_surveys_and_needs_follow_a_site_to_another_tenant(office, make_tenant, system) -> None:
    import datetime

    from patchmanager.locations import services as tree
    from patchmanager.locations.models import RoomSurvey
    from patchmanager.metadata import services as metadata
    from patchmanager.metadata.models import Note

    request, actor = office["request"], office["request"].user
    target = make_tenant()
    with system():
        room = Room.objects.get(pk=office["room"].pk)
        rooms.set_needs(request, actor, room, {"outlet": 2})
        survey = rooms.save_survey(
            request,
            actor,
            RoomSurvey(room=room, surveyed_on=datetime.date(2026, 10, 20), surveyor="Kim"),
            {"outlet": 1},
        )
        note = metadata.add_note(request, actor, survey, "fine")
        # Devices are removed so the site holds no private device types.
        for device in (office["outlet"], office["panel"]):
            devices.delete(request, actor, type(device).objects.get(pk=device.pk))
        tree.move_site(request, actor, office["site"], target)
        assert RoomSurvey.objects.get(pk=survey.pk).tenant_id == target.pk
        assert RoomNeed.objects.get(room=room).tenant_id == target.pk
        assert Note.objects.get(pk=note.pk).tenant_id == target.pk


def test_deleting_a_device_drops_notes_of_it_and_its_ports(office, system) -> None:
    from patchmanager.metadata import services as metadata
    from patchmanager.metadata.models import Note

    request, actor = office["request"], office["request"].user
    with system():
        outlet = type(office["outlet"]).objects.get(pk=office["outlet"].pk)
        metadata.add_note(request, actor, outlet, "device note")
        metadata.add_note(request, actor, outlet.ports.get(name="A2"), "port note")
        devices.delete(request, actor, outlet)
        assert not Note.objects.exists()


def test_plan_pin_panel(admin_client: Client, office, make_tree, make_user, grant, system) -> None:
    from patchmanager.metadata import services as metadata
    from tests.conftest import login

    floor, outlet = office["floor"], office["outlet"]
    with system():
        metadata.add_note(office["request"], office["request"].user, outlet, "behind the cupboard")
    url = f"/plans/{floor.pk}/device/{outlet.pk}/"
    page = admin_client.get(url, secure=True).content.decode()
    assert "O-01" in page and "PP1" in page and "A01" in page  # A1 is patched to PP1 in A01
    assert "1 note" in page and "Remove from plan" in page
    rack = admin_client.get(f"/plans/{floor.pk}/rack/{office['rack'].pk}/", secure=True)
    assert "1 used" in rack.content.decode()
    other = make_tree(name="Elsewhere")
    assert (
        admin_client.get(f"/plans/{other['floor'].pk}/device/{outlet.pk}/", secure=True).status_code
        == 404
    )
    assert admin_client.get(f"/plans/{floor.pk}/cable/{outlet.pk}/", secure=True).status_code == 404
    viewer = make_user()
    grant(viewer, "viewer", office["tenant"])
    client = Client()
    login(client, viewer)
    page = client.get(url, secure=True).content.decode()
    assert "O-01" in page and "Remove from plan" not in page
