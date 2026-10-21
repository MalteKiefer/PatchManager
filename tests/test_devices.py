import json

import pytest
from django.db import DatabaseError, transaction
from django.test import Client

from patchmanager.accounts.services import ServiceError
from patchmanager.dcim import devices
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.locations import services as tree
from patchmanager.locations.models import Room
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db

PANEL_PORTS = [{"name_pattern": "R{n}", "kind": "rear", "count": 24, "connector": "rj45"}]


def test_floor_device_with_ports_and_naming(make_tree, make_type, super_admin) -> None:
    t = make_tree()
    ap = make_type(
        "AP",
        form="floor",
        u_height=0,
        name_pattern="AP-{n:02}",
        ports=[{"name_pattern": "eth0", "count": 1}],
    )
    with rls.system_context():
        first = devices.create(t["request"], super_admin, device_type=ap, room=t["room"])
        second = devices.create(t["request"], super_admin, device_type=ap, room=t["room"])
        assert (first.name, second.name) == ("AP-01", "AP-02")
        assert list(first.ports.values_list("name", flat=True)) == ["eth0"]
        assert str(first.path).startswith(str(t["room"].path) + ".")
        with pytest.raises(ServiceError, match="already exists"):
            devices.create(t["request"], super_admin, device_type=ap, room=t["room"], name="ap-01")


def test_rack_mount_faces_and_fit(make_tree, make_type, super_admin) -> None:
    t = make_tree(u_height=10)
    server = make_type("Server", u_height=2, full_depth=True)
    shelf = make_type("Shelf", u_height=1, full_depth=False)
    with rls.system_context():
        s1 = devices.create(
            t["request"],
            super_admin,
            device_type=server,
            room=t["room"],
            rack=t["rack"],
            position=1,
        )
        assert (s1.space.start, s1.space.end, s1.space.face) == (1, 2, "both")
        front = devices.create(
            t["request"],
            super_admin,
            device_type=shelf,
            room=t["room"],
            rack=t["rack"],
            position=3,
            face="front",
            name="front shelf",
        )
        devices.create(
            t["request"],
            super_admin,
            device_type=shelf,
            room=t["room"],
            rack=t["rack"],
            position=3,
            face="rear",
            name="rear shelf",
        )
        assert front.space.face == "front"
        with pytest.raises(ServiceError, match="taken"):
            devices.create(
                t["request"],
                super_admin,
                device_type=server,
                room=t["room"],
                rack=t["rack"],
                position=2,
                name="overlap",
            )
        with pytest.raises(ServiceError, match="fit"):
            devices.create(
                t["request"],
                super_admin,
                device_type=server,
                room=t["room"],
                rack=t["rack"],
                position=10,
                name="too high",
            )
        with pytest.raises(ServiceError, match="position"):
            devices.create(
                t["request"],
                super_admin,
                device_type=server,
                room=t["room"],
                rack=t["rack"],
                name="nopos",
            )
        assert not Device.objects.filter(name__in=["overlap", "too high", "nopos"]).exists()


def test_patch_panel_front_to_rear_mapping(make_tree, make_type, super_admin) -> None:
    t = make_tree()
    with rls.system_context():
        one_to_one = make_type("24 port panel", category="patch_panel", ports=PANEL_PORTS)
        rear = one_to_one.port_templates.get()
        from patchmanager.library import services as library

        library.add_port_template(
            t["request"],
            super_admin,
            one_to_one,
            name_pattern="F{n}",
            kind="front",
            count=24,
            rear_template=rear,
        )
        panel = devices.create(
            t["request"],
            super_admin,
            device_type=one_to_one,
            room=t["room"],
            rack=t["rack"],
            position=40,
        )
        front5 = panel.ports.get(name="F5")
        assert (front5.rear_port.name, front5.rear_position) == ("R5", 1)
        mpo = make_type(
            "MPO cassette",
            ports=[
                {
                    "name_pattern": "MPO",
                    "kind": "rear",
                    "count": 1,
                    "positions": 12,
                    "connector": "mpo",
                }
            ],
        )
        library.add_port_template(
            t["request"],
            super_admin,
            mpo,
            name_pattern="LC{n}",
            kind="front",
            count=12,
            connector="lc",
            rear_template=mpo.port_templates.get(),
        )
        cassette = devices.create(
            t["request"], super_admin, device_type=mpo, room=t["room"], rack=t["rack"], position=41
        )
        assert [
            p.rear_position for p in cassette.ports.filter(kind="front").order_by("order")
        ] == list(range(1, 13))


def test_place_and_move(make_tree, make_type, super_admin) -> None:
    t = make_tree()
    other_rack = None
    server = make_type("Server", u_height=1)
    with rls.system_context():
        other_rack = tree.create(t["request"], super_admin, Rack, parent=t["room"], name="A02")
        device = devices.create(
            t["request"],
            super_admin,
            device_type=server,
            room=t["room"],
            rack=t["rack"],
            position=5,
        )
        devices.place(
            t["request"], super_admin, device, room=t["room"], rack=other_rack, position=7
        )
        device = Device.objects.get(pk=device.pk)
        assert device.rack == other_rack and device.space.start == 7
        assert str(device.path).startswith(str(other_rack.path))
        devices.place(t["request"], super_admin, device, room=t["room"], rack=None, position=None)
        assert not RackSpace.objects.filter(device=device).exists()
        room2 = tree.create(t["request"], super_admin, Room, parent=t["floor"], name="R2")
        with pytest.raises(ServiceError, match="not in this room"):
            devices.place(t["request"], super_admin, device, room=room2, rack=t["rack"], position=1)


def test_database_integrity(make_tree, make_type, super_admin) -> None:
    a, b = make_tree(), make_tree(name="Other")
    switch = make_type("Switch")
    private = make_type("Private", tenant=b["tenant"])
    with rls.system_context():
        device = devices.create(a["request"], super_admin, device_type=switch, room=a["room"])
        other = devices.create(
            a["request"], super_admin, device_type=switch, room=b["room"], name="b-dev"
        )
        with pytest.raises(DatabaseError, match="tenant"), transaction.atomic():
            Device.objects.filter(pk=device.pk).update(room=b["room"])
        with pytest.raises(DatabaseError, match="rack must stand"), transaction.atomic():
            Device.objects.filter(pk=device.pk).update(rack=b["rack"])
        with pytest.raises(DatabaseError, match="private"), transaction.atomic():
            Device.objects.filter(pk=device.pk).update(device_type=private)
        port = device.ports.first()
        with pytest.raises(DatabaseError, match="same device"), transaction.atomic():
            Port.objects.filter(pk=port.pk).update(rear_port=other.ports.first())
        with pytest.raises(ServiceError, match="private"):
            devices.create(a["request"], super_admin, device_type=private, room=a["room"], name="x")


def test_ports_crud(make_tree, make_type, super_admin) -> None:
    t = make_tree()
    with rls.system_context():
        device = devices.create(
            t["request"], super_admin, device_type=make_type("Switch"), room=t["room"]
        )
        extra = devices.add_port(t["request"], super_admin, device, name="mgmt", kind="interface")
        with pytest.raises(ServiceError, match="already used"):
            devices.add_port(t["request"], super_admin, device, name="MGMT")
        devices.update_port(t["request"], super_admin, extra, label="Uplink", description="to core")
        assert Port.objects.get(pk=extra.pk).label == "Uplink"
        devices.delete_port(t["request"], super_admin, extra)
        assert device.ports.count() == 4


def test_move_site_carries_devices(make_tree, make_type, make_tenant, super_admin) -> None:
    t = make_tree()
    target = make_tenant()
    with rls.system_context():
        device = devices.create(
            t["request"],
            super_admin,
            device_type=make_type("Switch"),
            room=t["room"],
            rack=t["rack"],
            position=1,
        )
        tree.move_site(t["request"], super_admin, t["site"], target)
        moved = Device.objects.get(pk=device.pk)
        assert moved.tenant_id == target.pk
        assert set(moved.ports.values_list("tenant_id", flat=True)) == {target.pk}


def test_move_site_refuses_private_types(make_tree, make_type, make_tenant, super_admin) -> None:
    t = make_tree()
    private = make_type("Private", tenant=t["tenant"])
    with rls.system_context():
        devices.create(t["request"], super_admin, device_type=private, room=t["room"])
        with pytest.raises(ServiceError, match="private"):
            tree.move_site(t["request"], super_admin, t["site"], make_tenant())


def test_device_views(admin_client: Client, make_tree, make_type, system) -> None:
    t = make_tree()
    server = make_type("Server", u_height=1)
    ap = make_type("AP", form="floor", u_height=0)
    admin_client.post("/tenants/switch/", {"tenant": t["tenant"].pk}, secure=True)
    rack_new = f"/devices/new/?rack={t['rack'].pk}&position=3"
    assert admin_client.get(rack_new, secure=True).status_code == 200
    response = admin_client.post(
        rack_new,
        {
            "device_type": server.pk,
            "name": "srv1",
            "status": "active",
            "position": 3,
            "face": "front",
        },
        secure=True,
    )
    assert response.status_code == 302
    with system():
        device = Device.objects.get(name="srv1")
    page = admin_client.get(f"/racks/{t['rack'].pk}/", secure=True).content.decode()
    assert "srv1" in page and f"/devices/{device.pk}/" in page
    room_new = f"/devices/new/?room={t['room'].pk}"
    admin_client.post(
        room_new, {"device_type": ap.pk, "name": "ap1", "status": "active"}, secure=True
    )
    assert (
        "ap1" in admin_client.get(f"/locations/room/{t['room'].pk}/", secure=True).content.decode()
    )
    assert admin_client.get("/devices/new/?room=nope", secure=True).status_code == 404

    url = f"/devices/{device.pk}/"
    assert admin_client.get(url, secure=True).status_code == 200
    admin_client.post(
        url, {"action": "edit", "name": "srv-1", "status": "planned", "serial": "S1"}, secure=True
    )
    admin_client.post(
        url,
        {"action": "add_port", "name": "ipmi", "kind": "interface", "connector": "rj45"},
        secure=True,
    )
    admin_client.post(
        url,
        {
            "action": "place",
            "room": t["room"].pk,
            "rack": t["rack"].pk,
            "position": 9,
            "face": "front",
        },
        secure=True,
    )
    with system():
        device.refresh_from_db()
        assert (device.name, device.space.start) == ("srv-1", 9)
        port = device.ports.get(name="ipmi")
        space = device.space
    move = admin_client.post(
        f"/racks/{t['rack'].pk}/spaces/{space.pk}/move/",
        json.dumps({"start": 20, "face": "rear"}),
        content_type="application/json",
        secure=True,
    )
    assert move.json() == {"start": 20, "end": 20, "face": "both"}  # full depth stays on both faces
    response = admin_client.post(
        f"/racks/{t['rack'].pk}/spaces/{space.pk}/delete/", secure=True, follow=True
    )
    assert "Move or delete the device instead." in response.content.decode()

    port_url = f"/devices/ports/{port.pk}/"
    assert admin_client.get(port_url, secure=True).status_code == 200
    admin_client.post(
        port_url,
        {
            "action": "edit",
            "name": "ipmi",
            "kind": "interface",
            "connector": "rj45",
            "enabled": "on",
            "label": "BMC",
            "description": "out of band",
        },
        secure=True,
    )
    with system():
        assert Port.objects.get(pk=port.pk).label == "BMC"
    admin_client.post(port_url, {"action": "delete"}, secure=True)
    admin_client.post(url, {"action": "delete"}, secure=True)
    with system():
        assert not Device.objects.filter(pk=device.pk).exists()


def test_viewer_and_foreign_access(
    client: Client, make_tree, make_type, make_user, grant, super_admin
) -> None:
    a, b = make_tree(), make_tree(name="Other")
    with rls.system_context():
        mine = devices.create(
            a["request"], super_admin, device_type=make_type("S"), room=a["room"], name="mine"
        )
        theirs = devices.create(
            b["request"], super_admin, device_type=make_type("T"), room=b["room"], name="theirs"
        )
        their_port = theirs.ports.first()
    user = make_user()
    grant(user, "viewer", a["tenant"])
    login(client, user)
    assert client.get(f"/devices/{mine.pk}/", secure=True).status_code == 200
    assert client.get(f"/devices/{theirs.pk}/", secure=True).status_code == 404
    assert client.get(f"/devices/ports/{their_port.pk}/", secure=True).status_code == 404
    client.post(
        f"/devices/{mine.pk}/",
        {"action": "edit", "name": "hacked", "status": "active"},
        secure=True,
    )
    assert client.get(f"/devices/new/?room={a['room'].pk}", secure=True).status_code == 403
    with rls.system_context():
        assert Device.objects.get(pk=mine.pk).name == "mine"
