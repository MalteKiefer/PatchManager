import pytest
from django.db import DatabaseError, transaction
from django.test import Client

from patchmanager.accounts.services import ServiceError
from patchmanager.cabling import services as cabling
from patchmanager.dcim import devices
from patchmanager.dcim.models import Port
from patchmanager.locations import services as tree
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.networks import services
from patchmanager.networks.models import VLAN, WLAN, TaggedVLAN
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db


@pytest.fixture
def net(make_tree, make_type, super_admin):
    t = make_tree()
    with rls.system_context():
        other_site = tree.create(t["request"], super_admin, Site, parent=t["tenant"], name="Branch")
        switch = make_type(
            "Switch",
            ports=[
                {"name_pattern": "ge{n}", "count": 4},
                {"name_pattern": "wlan0", "kind": "wireless", "connector": "wireless"},
            ],
        )
        device = devices.create(
            t["request"], super_admin, device_type=switch, room=t["room"], name="sw1"
        )
    return {**t, "other_site": other_site, "device": device}


def test_vlan_rules(net, super_admin) -> None:
    r, tenant = net["request"], net["tenant"]
    with rls.system_context():
        global_vlan = services.create_vlan(r, super_admin, tenant, vid=10, name="Users")
        services.create_vlan(r, super_admin, tenant, vid=10, name="HQ users", site=net["site"])
        with pytest.raises(ServiceError, match="already used"):
            services.create_vlan(r, super_admin, tenant, vid=10, name="Dup")
        with pytest.raises(ServiceError):
            services.create_vlan(r, super_admin, tenant, vid=5000, name="Too big")
        services.update_vlan(r, super_admin, global_vlan, name="Staff")
        assert VLAN.objects.get(pk=global_vlan.pk).name == "Staff"


def test_port_vlans_respect_site(net, super_admin) -> None:
    r, tenant = net["request"], net["tenant"]
    with rls.system_context():
        users = services.create_vlan(r, super_admin, tenant, vid=10, name="Users")
        voice = services.create_vlan(r, super_admin, tenant, vid=20, name="Voice", site=net["site"])
        branch = services.create_vlan(
            r, super_admin, tenant, vid=30, name="Branch", site=net["other_site"]
        )
        port = net["device"].ports.get(name="ge1")
        assert set(services.vlans_for_port(port)) == {users, voice}
        services.set_port_vlans(r, super_admin, port, mode="trunk", untagged=users, tagged=[voice])
        port.refresh_from_db()
        assert port.untagged_vlan == users and list(
            port.tagged.values_list("vlan__vid", flat=True)
        ) == [20]
        with pytest.raises(ServiceError, match="site"):
            services.set_port_vlans(r, super_admin, port, mode="access", untagged=branch, tagged=[])
        with pytest.raises(ServiceError, match="Access ports"):
            services.set_port_vlans(
                r, super_admin, port, mode="access", untagged=users, tagged=[voice]
            )
        with pytest.raises(ServiceError, match="access or trunk"):
            services.set_port_vlans(r, super_admin, port, mode="", untagged=users, tagged=[])
        with pytest.raises(DatabaseError, match="tagged VLAN"), transaction.atomic():
            TaggedVLAN.objects.create(tenant=tenant, port=port, vlan=branch)
        with pytest.raises(ServiceError, match="in use"):
            services.update_vlan(r, super_admin, voice, site=None)


def test_wlan_radios(net, super_admin) -> None:
    r, tenant = net["request"], net["tenant"]
    with rls.system_context():
        vlan = services.create_vlan(r, super_admin, tenant, vid=50, name="Guests")
        wlan = services.create_wlan(
            r, super_admin, tenant, ssid="Guest", vlan=vlan, security="wpa3-personal"
        )
        with pytest.raises(ServiceError, match="SSID"):
            services.create_wlan(r, super_admin, tenant, ssid="Guest")
        radio = net["device"].ports.get(name="wlan0")
        services.set_broadcast(r, super_admin, wlan, radio, on=True)
        assert list(wlan.radios.all()) == [radio]
        with pytest.raises(ServiceError, match="wireless"):
            services.set_broadcast(
                r, super_admin, wlan, net["device"].ports.get(name="ge1"), on=True
            )
        services.set_broadcast(r, super_admin, wlan, radio, on=False)
        assert not wlan.radios.exists()
        with pytest.raises(ServiceError, match="WLAN still uses"):
            services.delete_vlan(r, super_admin, vlan)
        services.delete_wlan(r, super_admin, wlan)
        services.delete_vlan(r, super_admin, vlan)


def test_site_move_rules(net, make_tenant, super_admin) -> None:
    r, tenant = net["request"], net["tenant"]
    target = make_tenant()
    with rls.system_context():
        port = net["device"].ports.get(name="ge1")
        users = services.create_vlan(r, super_admin, tenant, vid=10, name="Users")
        services.set_port_vlans(r, super_admin, port, mode="access", untagged=users, tagged=[])
        with pytest.raises(ServiceError, match="tenant wide VLANs"):
            tree.move_site(r, super_admin, net["site"], target)
        services.set_port_vlans(r, super_admin, port, mode="", untagged=None, tagged=[])
        site_vlan = services.create_vlan(
            r, super_admin, tenant, vid=20, name="Local", site=net["site"]
        )
        services.set_port_vlans(
            r, super_admin, port, mode="trunk", untagged=None, tagged=[site_vlan]
        )
        cabling.connect(r, super_admin, port, net["device"].ports.get(name="ge2"))
        tree.move_site(r, super_admin, net["site"], target)
        assert VLAN.objects.get(pk=site_vlan.pk).tenant_id == target.pk
        moved = Port.objects.get(pk=port.pk)
        assert moved.cable_end.cable.tenant_id == target.pk
        assert str(moved.cable_end.cable.path).startswith(str(target.policy_path))


def test_site_move_refuses_cables_leaving_the_site(
    net, make_tenant, make_type, super_admin
) -> None:
    r = net["request"]
    with rls.system_context():
        building = tree.create(r, super_admin, Building, parent=net["other_site"], name="B")
        floor = tree.create(r, super_admin, Floor, parent=building, name="F")
        room = tree.create(r, super_admin, Room, parent=floor, name="R")
        remote = devices.create(
            r, super_admin, device_type=net["device"].device_type, room=room, name="remote"
        )
        cabling.connect(
            r, super_admin, net["device"].ports.get(name="ge1"), remote.ports.get(name="ge1")
        )
        with pytest.raises(ServiceError, match="Cables connect"):
            tree.move_site(r, super_admin, net["site"], make_tenant())


def test_network_views(admin_client: Client, net, system) -> None:
    admin_client.post("/tenants/switch/", {"tenant": net["tenant"].pk}, secure=True)
    assert admin_client.get("/networks/vlans/", secure=True).status_code == 200
    admin_client.post(
        "/networks/vlans/", {"vid": 100, "name": "Servers", "status": "active"}, secure=True
    )
    with system():
        vlan = VLAN.objects.get(vid=100)
    assert admin_client.get(f"/networks/vlans/{vlan.pk}/", secure=True).status_code == 200
    admin_client.post(
        f"/networks/vlans/{vlan.pk}/",
        {"vid": 100, "name": "Servers DC", "status": "active"},
        secure=True,
    )
    with system():
        port = net["device"].ports.get(name="ge3")
        radio = net["device"].ports.get(name="wlan0")
    admin_client.post(
        f"/devices/ports/{port.pk}/",
        {"action": "vlans", "mode": "access", "untagged": vlan.pk},
        secure=True,
    )
    with system():
        assert Port.objects.get(pk=port.pk).untagged_vlan_id == vlan.pk
    assert "ge3" in admin_client.get(f"/networks/vlans/{vlan.pk}/", secure=True).content.decode()
    admin_client.post(
        "/networks/wlans/",
        {"ssid": "Corp", "security": "wpa3-enterprise", "vlan": vlan.pk},
        secure=True,
    )
    with system():
        wlan = WLAN.objects.get()
    url = f"/networks/wlans/{wlan.pk}/"
    admin_client.post(url, {"action": "radio", "port": radio.pk}, secure=True)
    assert "wlan0" in admin_client.get(url, secure=True).content.decode()
    admin_client.post(url, {"action": "remove_radio", "port": radio.pk}, secure=True)
    admin_client.post(url, {"action": "edit", "ssid": "Corp2", "security": "open"}, secure=True)
    admin_client.post(url, {"action": "delete"}, secure=True)
    admin_client.post(f"/devices/ports/{port.pk}/", {"action": "vlans", "mode": ""}, secure=True)
    admin_client.post(f"/networks/vlans/{vlan.pk}/", {"action": "delete"}, secure=True)
    with system():
        assert not VLAN.objects.filter(pk=vlan.pk).exists()
        assert not WLAN.objects.exists()


def test_scoped_user_sees_only_permitted_vlans_and_ports(
    net, make_user, make_type, super_admin, client
) -> None:
    from patchmanager.accounts.models import Assignment, Role
    from patchmanager.accounts.services import PermissionDeniedError
    from tests.conftest import login

    r, tenant = net["request"], net["tenant"]
    with rls.system_context():
        tenant_vlan = services.create_vlan(r, super_admin, tenant, vid=10, name="Tenant wide")
        site_vlan = services.create_vlan(
            r, super_admin, tenant, vid=20, name="HQ", site=net["site"]
        )
        other_room = tree.create(r, super_admin, Room, parent=net["floor"], name="Other room")
        hidden = devices.create(
            r,
            super_admin,
            device_type=net["device"].device_type,
            room=other_room,
            name="hidden-switch",
        )
        services.set_port_vlans(
            r,
            super_admin,
            hidden.ports.get(name="ge1"),
            mode="access",
            untagged=site_vlan,
            tagged=[],
        )
        port = net["device"].ports.get(name="ge1")
    editor = make_user()
    Assignment.objects.create(
        role=Role.objects.get(key="editor"), user=editor, tenant=tenant, scope_path=net["site"].path
    )
    room_editor = make_user()
    Assignment.objects.create(
        role=Role.objects.get(key="editor"),
        user=room_editor,
        tenant=tenant,
        scope_path=net["room"].path,
    )
    with rls.system_context():
        assert set(services.vlans_for_port(port, editor)) == {site_vlan}
        assert set(services.vlans_for_port(port, room_editor)) == set()
        with pytest.raises(PermissionDeniedError):
            services.set_port_vlans(
                r, room_editor, port, mode="access", untagged=tenant_vlan, tagged=[]
            )
    login(client, editor)
    page = client.get(f"/networks/vlans/{site_vlan.pk}/", secure=True).content.decode()
    assert "hidden-switch" in page  # editor covers the whole site
    viewer = make_user()
    Assignment.objects.create(
        role=Role.objects.get(key="viewer"), user=viewer, tenant=tenant, scope_path=net["site"].path
    )
    Assignment.objects.create(
        role=Role.objects.get(key="viewer"),
        user=viewer,
        tenant=tenant,
        scope_path=other_room.path,
        effect="deny",
    )
    from django.test import Client

    viewer_client = Client()
    login(viewer_client, viewer)
    page = viewer_client.get(f"/networks/vlans/{site_vlan.pk}/", secure=True).content.decode()
    assert "hidden-switch" not in page


def test_site_move_refused_when_wlan_bridges_site_vlan(net, make_tenant, super_admin) -> None:
    r, tenant = net["request"], net["tenant"]
    with rls.system_context():
        site_vlan = services.create_vlan(
            r, super_admin, tenant, vid=40, name="Site", site=net["site"]
        )
        services.create_wlan(r, super_admin, tenant, ssid="Site WiFi", vlan=site_vlan)
        with pytest.raises(ServiceError, match="tenant wide VLANs or WLANs"):
            tree.move_site(r, super_admin, net["site"], make_tenant())
