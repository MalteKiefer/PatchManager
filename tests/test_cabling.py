import pytest
from django.db import DatabaseError, transaction
from django.test import Client
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.cabling import services, trace
from patchmanager.cabling.models import Cable, CableEnd
from patchmanager.dcim import devices
from patchmanager.dcim.models import Port
from patchmanager.library import services as library
from patchmanager.locations import services as tree
from patchmanager.locations.models import Building, Floor, Room
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture
def kit(make_tree, make_type, super_admin):
    """Switch type, 24 port 1:1 panel type, 12 LC to MPO cassette type."""
    t = make_tree()
    request = t["request"]
    with rls.system_context():
        switch = make_type("Switch", ports=[{"name_pattern": "ge{n}", "count": 24}])
        panel = make_type(
            "Panel", ports=[{"name_pattern": "R{n}", "kind": "rear", "count": 24}], full_depth=False
        )
        library.add_port_template(
            request,
            super_admin,
            panel,
            name_pattern="F{n}",
            kind="front",
            count=24,
            rear_template=panel.port_templates.get(),
        )
        cassette = make_type(
            "Cassette",
            ports=[
                {
                    "name_pattern": "MPO",
                    "kind": "rear",
                    "count": 1,
                    "positions": 12,
                    "connector": "mpo",
                }
            ],
            full_depth=False,
        )
        library.add_port_template(
            request,
            super_admin,
            cassette,
            name_pattern="LC{n}",
            kind="front",
            count=12,
            connector="lc",
            rear_template=cassette.port_templates.get(),
        )
    return {**t, "switch": switch, "panel": panel, "cassette": cassette}


def _device(kit, type_key, name, **kwargs):
    room = kwargs.pop("room", kit["room"])
    return devices.create(
        kit["request"],
        kit["request"].user,
        device_type=kit[type_key],
        room=room,
        name=name,
        **kwargs,
    )


def _port(device, name):
    return device.ports.get(name=name)


def _batched_matches_single() -> None:
    """``trace_many`` finds exactly what ``trace`` finds, for every port."""
    ports = list(Port.objects.all())
    many = trace.trace_many(ports)
    for port in ports:
        one = trace.trace(port)
        assert [p.pk for p in many[port.pk].ports] == [p.pk for p in one.ports], port
        assert [(link.kind, link.position) for link in many[port.pk].links] == [
            (link.kind, link.position) for link in one.links
        ]
        assert many[port.pk].loop == one.loop


def test_connect_rules(kit, super_admin) -> None:
    with rls.system_context():
        a = _device(kit, "switch", "sw-a")
        b = _device(kit, "switch", "sw-b")
        cable = services.connect(
            kit["request"],
            super_admin,
            _port(a, "ge1"),
            _port(b, "ge1"),
            kind="patch",
            colour="#ff0000",
            length_m=2,
        )
        assert {e.side for e in cable.ends.all()} == {"a", "b"}
        assert services.scope(_port(a, "ge1"), _port(b, "ge1")) == "within the room"
        with pytest.raises(ServiceError, match="already has a cable"):
            services.connect(kit["request"], super_admin, _port(a, "ge1"), _port(b, "ge2"))
        with pytest.raises(ServiceError, match="two different"):
            services.connect(kit["request"], super_admin, _port(a, "ge3"), _port(a, "ge3"))
        with pytest.raises(ServiceError):
            services.connect(
                kit["request"], super_admin, _port(a, "ge4"), _port(b, "ge4"), colour="red"
            )
        services.update(kit["request"], super_admin, cable, label="C-001", length_m=3)
        assert Cable.objects.get(pk=cable.pk).label == "C-001"
        services.disconnect(kit["request"], super_admin, cable)
        assert not CableEnd.objects.exists()


def test_scope_labels(kit, super_admin) -> None:
    with rls.system_context():
        a = _device(kit, "switch", "in-rack-a", rack=kit["rack"], position=1)
        b = _device(kit, "switch", "in-rack-b", rack=kit["rack"], position=2)
        other_building = tree.create(
            kit["request"], super_admin, Building, parent=kit["site"], name="B2"
        )
        floor = tree.create(kit["request"], super_admin, Floor, parent=other_building, name="F")
        room = tree.create(kit["request"], super_admin, Room, parent=floor, name="R")
        c = _device(kit, "switch", "far", room=room)
        assert services.scope(_port(a, "ge1"), _port(b, "ge1")) == "within the rack"
        assert services.scope(_port(a, "ge1"), _port(a, "ge2")) == "within the device"
        assert services.scope(_port(a, "ge1"), _port(c, "ge1")) == "within the site"
        cable = services.connect(kit["request"], super_admin, _port(a, "ge1"), _port(c, "ge1"))
        assert str(cable.path) == str(kit["site"].path)


def test_cross_tenant_cables_are_impossible(kit, make_tree, make_type, super_admin) -> None:
    other = make_tree(name="Other")
    with rls.system_context():
        mine = _device(kit, "switch", "mine")
        theirs = devices.create(
            other["request"],
            super_admin,
            device_type=kit["switch"],
            room=other["room"],
            name="theirs",
        )
        with pytest.raises(ServiceError, match="different tenants"):
            services.connect(kit["request"], super_admin, _port(mine, "ge1"), _port(theirs, "ge1"))
        cable = services.connect(
            kit["request"], super_admin, _port(mine, "ge2"), _port(mine, "ge3")
        )
        with pytest.raises(DatabaseError, match="tenant"), transaction.atomic():
            CableEnd.objects.filter(cable=cable, side="b").update(port=_port(theirs, "ge2"))


def test_connect_needs_permission_on_both_ends(kit, make_user, grant, super_admin) -> None:
    from patchmanager.accounts.models import Assignment, Role

    user = make_user()
    with rls.system_context():
        a = _device(kit, "switch", "a")
        room2 = tree.create(kit["request"], super_admin, Room, parent=kit["floor"], name="R2")
        b = _device(kit, "switch", "b", room=room2)
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=user,
            tenant=kit["tenant"],
            scope_path=kit["room"].path,
        )
        with pytest.raises(PermissionDeniedError):
            services.connect(kit["request"], user, _port(a, "ge1"), _port(b, "ge1"))
        services.connect(kit["request"], user, _port(a, "ge1"), _port(a, "ge2"))


def test_trace_through_two_panels(kit, super_admin) -> None:
    with rls.system_context():
        sw = _device(kit, "switch", "sw")
        p1 = _device(kit, "panel", "pp1")
        p2 = _device(kit, "panel", "pp2")
        pc = _device(kit, "switch", "pc")
        services.connect(kit["request"], super_admin, _port(sw, "ge5"), _port(p1, "F7"))
        services.connect(
            kit["request"], super_admin, _port(p1, "R7"), _port(p2, "R7"), kind="permanent"
        )
        services.connect(kit["request"], super_admin, _port(p2, "F7"), _port(pc, "ge1"))
        path = trace.trace(_port(sw, "ge5"))
        assert [f"{p.device}:{p.name}" for p in path.ports] == [
            "sw:ge5",
            "pp1:F7",
            "pp1:R7",
            "pp2:R7",
            "pp2:F7",
            "pc:ge1",
        ]
        assert path.complete and len(path.cables) == 3
        middle = trace.trace(_port(p2, "R7"))
        assert middle.ends[0].pk in {_port(sw, "ge5").pk, _port(pc, "ge1").pk}
        assert {middle.ends[0].pk, middle.ends[1].pk} == {_port(sw, "ge5").pk, _port(pc, "ge1").pk}
        _batched_matches_single()


def test_trace_through_mpo_trunk_keeps_position(kit, super_admin) -> None:
    with rls.system_context():
        c1 = _device(kit, "cassette", "cas1")
        c2 = _device(kit, "cassette", "cas2")
        a = _device(kit, "switch", "a")
        b = _device(kit, "switch", "b")
        services.connect(
            kit["request"],
            super_admin,
            _port(c1, "MPO"),
            _port(c2, "MPO"),
            kind="permanent",
            media="om4",
        )
        services.connect(kit["request"], super_admin, _port(a, "ge1"), _port(c1, "LC5"))
        services.connect(kit["request"], super_admin, _port(c2, "LC5"), _port(b, "ge1"))
        path = trace.trace(_port(a, "ge1"))
        assert path.ends[1].pk == _port(b, "ge1").pk
        assert [link.position for link in path.links if link.kind == "internal"] == [5, 5]
        _batched_matches_single()


def test_incomplete_and_loop(kit, super_admin) -> None:
    with rls.system_context():
        sw = _device(kit, "switch", "sw")
        p1 = _device(kit, "panel", "pp1")
        services.connect(kit["request"], super_admin, _port(sw, "ge1"), _port(p1, "F1"))
        open_path = trace.trace(_port(sw, "ge1"))
        assert not open_path.complete and open_path.ends[1].name == "R1"
        lonely = trace.trace(_port(sw, "ge2"))
        assert lonely.ports == [_port(sw, "ge2")] and not lonely.complete
        _batched_matches_single()


def test_loop_detection(kit, super_admin) -> None:
    with rls.system_context():
        p1 = _device(kit, "panel", "pp1")
        p2 = _device(kit, "panel", "pp2")
        services.connect(kit["request"], super_admin, _port(p1, "R1"), _port(p2, "R1"))
        services.connect(kit["request"], super_admin, _port(p2, "F1"), _port(p1, "F1"))
        path = trace.trace(_port(p1, "F1"))
        assert path.loop and not path.complete
        _batched_matches_single()


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(panels=st.integers(0, 4), index=st.integers(1, 24))
def test_trace_property_any_panel_chain(kit, super_admin, panels, index) -> None:
    """Through any number of 1:1 panels, every start point finds the same two ends."""
    with rls.system_context():
        CableEnd.objects.all().delete()
        Cable.objects.all().delete()
        from patchmanager.dcim.models import Device

        Device.objects.all().delete()
        a = _device(kit, "switch", "a")
        b = _device(kit, "switch", "b")
        chain = [_device(kit, "panel", f"pp{i}") for i in range(panels)]
        current = _port(a, "ge1")
        for i, panel in enumerate(chain):
            front, rear = _port(panel, f"F{index}"), _port(panel, f"R{index}")
            services.connect(kit["request"], super_admin, current, front if i % 2 == 0 else rear)
            current = rear if i % 2 == 0 else front
        services.connect(kit["request"], super_admin, current, _port(b, "ge1"))
        expected = {_port(a, "ge1").pk, _port(b, "ge1").pk}
        forward = trace.trace(_port(a, "ge1"))
        backward = trace.trace(_port(b, "ge1"))
        assert {forward.ends[0].pk, forward.ends[1].pk} == expected
        assert [p.pk for p in backward.ports] == [p.pk for p in reversed(forward.ports)]
        assert forward.complete and len(forward.cables) == panels + 1
        for hop in forward.ports[1:-1]:
            via = trace.trace(Port.objects.get(pk=hop.pk))
            assert {via.ends[0].pk, via.ends[1].pk} == expected
        _batched_matches_single()


def test_cabling_views(admin_client: Client, kit, system) -> None:
    with rls.system_context():
        sw = _device(kit, "switch", "sw")
        pp = _device(kit, "panel", "pp")
        source, target = _port(sw, "ge1"), _port(pp, "F1")
    admin_client.post("/tenants/switch/", {"tenant": kit["tenant"].pk}, secure=True)
    url = f"/cabling/connect/{source.pk}/"
    assert admin_client.get(url, secure=True).status_code == 200
    options = admin_client.get(
        f"/cabling/port-options/?device={pp.pk}&exclude={source.pk}", secure=True
    )
    assert "F1" in options.content.decode()
    assert admin_client.get("/cabling/port-options/?device=x", secure=True).status_code == 404
    response = admin_client.post(
        url,
        {
            "device": pp.pk,
            "port": target.pk,
            "kind": "patch",
            "media": "cat6a",
            "colour": "#00aa00",
            "status": "installed",
        },
        secure=True,
    )
    assert response.status_code == 302
    with system():
        cable = Cable.objects.get()
    assert admin_client.get(f"/cabling/{cable.pk}/", secure=True).status_code == 200
    admin_client.post(
        f"/cabling/{cable.pk}/",
        {
            "kind": "patch",
            "media": "cat6a",
            "colour": "#00aa00",
            "status": "installed",
            "label": "P-1",
        },
        secure=True,
    )
    assert "P-1" in admin_client.get("/cabling/?kind=patch&q=sw", secure=True).content.decode()
    trace_page = admin_client.get(f"/cabling/trace/{source.pk}/", secure=True).content.decode()
    assert "incomplete" in trace_page and "pp" in trace_page
    port_page = admin_client.get(f"/devices/ports/{source.pk}/", secure=True).content.decode()
    assert "Connected to" in port_page
    device_page = admin_client.get(f"/devices/{sw.pk}/ports/", secure=True).content.decode()
    assert "P-1" in device_page
    admin_client.post(f"/cabling/disconnect/{source.pk}/", secure=True)
    with system():
        assert not Cable.objects.exists()


def test_trace_hides_devices_the_viewer_cannot_see(
    client: Client, kit, make_user, super_admin
) -> None:
    from patchmanager.accounts.models import Assignment, Role

    with rls.system_context():
        room2 = tree.create(
            kit["request"], super_admin, Room, parent=kit["floor"], name="Secret room"
        )
        a = _device(kit, "switch", "visible")
        b = _device(kit, "switch", "secret-core", room=room2)
        services.connect(kit["request"], super_admin, _port(a, "ge1"), _port(b, "ge1"))
        start = _port(a, "ge1")
    user = make_user()
    Assignment.objects.create(
        role=Role.objects.get(key="viewer"),
        user=user,
        tenant=kit["tenant"],
        scope_path=kit["room"].path,
    )
    login(client, user)
    page = client.get(f"/cabling/trace/{start.pk}/", secure=True)
    assert page.status_code == 200
    assert "secret-core" not in page.content.decode()
    for url in (f"/devices/ports/{start.pk}/", f"/devices/{start.device_id}/"):
        body = client.get(url, secure=True).content.decode()
        assert "secret-core" not in body and "not visible to you" in body
