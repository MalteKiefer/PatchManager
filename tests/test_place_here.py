import pytest
from django.test import Client

from patchmanager.dcim import devices
from patchmanager.dcim.models import Device
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db


@pytest.fixture
def spare(make_tree, make_type, super_admin):
    tree = make_tree()
    with rls.system_context():
        rack_type = make_type("Switch 1U")
        floor_type = make_type("AP", form="floor", u_height=0)
        loose = devices.create(
            tree["request"], super_admin, device_type=rack_type, room=tree["room"], name="SPARE-SW"
        )
        ap = devices.create(
            tree["request"], super_admin, device_type=floor_type, room=tree["room"], name="AP-1"
        )
    return {**tree, "loose": loose, "ap": ap}


def test_mount_an_existing_device(admin_client: Client, spare, system) -> None:
    rack = spare["rack"]
    url = f"/devices/place/?rack={rack.pk}&position=10"
    page = admin_client.get(url, secure=True).content.decode()
    assert "SPARE-SW" in page and "AP-1" not in page  # floor types do not go into racks
    assert f"/devices/new/?rack={rack.pk}&amp;position=10" in page
    done = admin_client.post(
        "/devices/place/",
        {"device": spare["loose"].pk, "rack": rack.pk, "position": "10", "face": "front"},
        secure=True,
    )
    assert done.url == f"/racks/{rack.pk}/"
    with system():
        device = Device.objects.get(pk=spare["loose"].pk)
        assert device.rack_id == rack.pk and device.space.start == 10


def test_place_an_existing_device_on_the_plan(admin_client: Client, spare, system) -> None:
    import io

    from PIL import Image

    from patchmanager.floorplans import services as plans

    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buffer, "PNG")
    with system():
        plans.upload(
            spare["request"],
            spare["request"].user,
            spare["floor"],
            name="p.png",
            data=buffer.getvalue(),
        )
    room = spare["room"]
    page = admin_client.get(
        f"/devices/place/?room={room.pk}&plan_x=120&plan_y=80", secure=True
    ).content.decode()
    assert "AP-1" in page and "SPARE-SW" in page
    done = admin_client.post(
        "/devices/place/",
        {"device": spare["ap"].pk, "room": room.pk, "plan_x": "120", "plan_y": "80"},
        secure=True,
    )
    assert done.url == f"/plans/{spare['floor'].pk}/"
    with system():
        ap = Device.objects.get(pk=spare["ap"].pk)
        assert (ap.plan_x, ap.plan_y) == (120, 80)
    again = admin_client.get(f"/devices/place/?room={room.pk}&plan_x=10&plan_y=10", secure=True)
    assert f'value="{spare["ap"].pk}"' not in again.content.decode()  # already on the plan


def test_no_candidates_goes_straight_to_create(admin_client: Client, make_tree) -> None:
    tree = make_tree()
    response = admin_client.get(f"/devices/place/?rack={tree['rack'].pk}&position=3", secure=True)
    assert response.status_code == 302 and response.url.startswith("/devices/new/?rack=")
    assert admin_client.post(
        "/devices/place/", {"rack": tree["rack"].pk, "device": "x"}, secure=True
    ).status_code in (302, 404)
