import json

import pytest
import yaml
from django.test import Client, RequestFactory

from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.library import interchange, services
from patchmanager.library.models import DeviceType, PortTemplate, Vendor, expand
from patchmanager.tenancy import rls
from tests.conftest import login

pytestmark = pytest.mark.django_db


def test_expand_only_interprets_counter() -> None:
    assert expand("Gi1/0/{n}", 7) == "Gi1/0/7"
    assert expand("Port {n:03}", 7) == "Port 007"
    assert expand("{n.__class__}", 1) == "{n.__class__}"


def test_template_names() -> None:
    assert PortTemplate(name_pattern="eth{n}", count=3, start=0).names() == ["eth0", "eth1", "eth2"]
    assert PortTemplate(name_pattern="mgmt", count=1).names() == ["mgmt"]
    assert PortTemplate(name_pattern="P", count=2).names() == ["P1", "P2"]


def test_shared_type_needs_global_grant(make_user, make_tenant, grant) -> None:
    tenant = make_tenant()
    owner = make_user()
    grant(owner, "tenant_owner", tenant)
    request = RequestFactory().post("/")
    request.user = owner
    with rls.tenant_context([tenant.pk]):
        vendor = Vendor.objects.create(name="Acme")
        with pytest.raises(PermissionDeniedError):
            services.create_type(request, owner, tenant=None, vendor=vendor, model="Shared")
        private = services.create_type(
            request, owner, tenant=tenant, vendor=vendor, model="Private"
        )
        assert private.tenant_id == tenant.pk


def test_private_types_are_isolated(make_type, make_tenant) -> None:
    a, b = make_tenant(), make_tenant()
    private = make_type("Private", tenant=a)
    shared = make_type("Shared")
    with rls.tenant_context([b.pk]):
        assert set(DeviceType.objects.all()) == {shared}
        assert not PortTemplate.objects.filter(device_type=private).exists()
    with rls.tenant_context([a.pk]):
        assert set(DeviceType.objects.all()) == {shared, private}


def test_rear_mapping_rules(make_type, super_admin) -> None:
    other = make_type("Other", ports=[{"name_pattern": "R", "kind": "rear", "count": 1}])
    panel = make_type(
        "Panel",
        form="rack",
        category="patch_panel",
        ports=[{"name_pattern": "R{n}", "kind": "rear", "count": 2}],
    )
    rear = panel.port_templates.get()
    request = RequestFactory().post("/")
    request.user = super_admin
    with rls.system_context():
        with pytest.raises(ServiceError, match="same type"):
            services.add_port_template(
                request,
                super_admin,
                panel,
                name_pattern="F{n}",
                kind="front",
                count=2,
                rear_template=other.port_templates.get(),
            )
        with pytest.raises(ServiceError, match="multiple"):
            services.add_port_template(
                request,
                super_admin,
                panel,
                name_pattern="F{n}",
                kind="front",
                count=3,
                rear_template=rear,
            )
        services.add_port_template(
            request,
            super_admin,
            panel,
            name_pattern="F{n}",
            kind="front",
            count=4,
            rear_template=rear,
        )
        copy = services.clone_type(request, super_admin, panel, tenant=None, model="Panel copy")
        front = copy.port_templates.get(kind="front")
        assert front.rear_template.device_type_id == copy.pk


def test_shape_of_used_type_is_locked(make_type, make_tree, super_admin) -> None:
    from patchmanager.dcim import devices

    t = make_tree()
    device_type = make_type("Server", u_height=2)
    with rls.system_context():
        devices.create(
            t["request"],
            super_admin,
            device_type=device_type,
            room=t["room"],
            rack=t["rack"],
            position=1,
            name="srv1",
        )
        with pytest.raises(ServiceError, match="in use"):
            services.update_type(t["request"], super_admin, device_type, u_height=4)
        services.update_type(t["request"], super_admin, device_type, description="ok")
        with pytest.raises(ServiceError, match="Devices"):
            services.delete_type(t["request"], super_admin, device_type)


SWITCH = (
    """
manufacturer: Example Networks
model: EX-48T
part_number: EX48
u_height: 1
is_full_depth: false
airflow: front-to-rear
weight: 11
weight_unit: lb
interfaces:
"""
    + "".join(f"  - name: ge-0/0/{i}\n    type: 1000base-t\n" for i in range(48))
    + """
  - name: xe-0/1/0
    type: 10gbase-x-sfpp
  - name: mgmt
    type: 1000base-t
console-ports:
  - name: console
    type: rj-45
power-ports:
  - name: PSU1
    type: iec-60320-c14
    maximum_draw: 150
  - name: PSU2
    type: iec-60320-c14
    maximum_draw: 150
"""
)

PANEL = """
manufacturer: Example Cabling
model: MPO-12
u_height: 1
rear-ports:
  - name: MPO
    type: mpo
    positions: 12
front-ports:
""" + "".join(
    f"  - name: '{i}'\n    type: lc\n    rear_port: MPO\n    rear_port_position: {i}\n"
    for i in range(1, 13)
)


def test_import_switch_compresses_ports(super_admin) -> None:
    request = RequestFactory().post("/")
    request.user = super_admin
    with rls.system_context():
        device_type = interchange.import_type(request, super_admin, SWITCH, tenant=None)
        templates = list(device_type.port_templates.all())
    names = [n for t in templates for n in t.names()]
    assert len(names) == 53 and "ge-0/0/0" in names and "ge-0/0/47" in names
    ge = next(t for t in templates if t.name_pattern == "ge-0/0/{n}")
    assert (ge.start, ge.count, ge.connector, ge.speed_mbps) == (0, 48, "rj45", 1000)
    assert next(t for t in templates if t.name_pattern == "xe-0/1/0").connector == "sfp+"
    assert {t.kind for t in templates} == {"interface", "console", "power"}
    assert not device_type.full_depth
    assert (device_type.airflow, device_type.mounting) == ("front-to-rear", "rack-19")
    assert (device_type.power_supplies, device_type.max_power_w) == (2, 150)
    assert str(device_type.weight_kg) == "4.99"
    exported = yaml.safe_load(interchange.export_type(device_type))
    assert exported["airflow"] == "front-to-rear" and exported["weight_unit"] == "kg"


def test_import_panel_and_roundtrip(super_admin, make_tree) -> None:
    from patchmanager.dcim import devices

    t = make_tree()
    with rls.system_context():
        panel = interchange.import_type(t["request"], super_admin, PANEL, tenant=None)
        device = devices.create(
            t["request"],
            super_admin,
            device_type=panel,
            room=t["room"],
            rack=t["rack"],
            position=10,
            name="pp1",
        )
        fronts = list(device.ports.filter(kind="front").order_by("order"))
        assert [(p.rear_port.name, p.rear_position) for p in fronts] == [
            ("MPO", i) for i in range(1, 13)
        ]
        exported = yaml.safe_load(interchange.export_type(panel))
        assert exported["rear-ports"] == [{"name": "MPO", "type": "mpo", "positions": 12}]
        assert exported["front-ports"][4] == {
            "name": "5",
            "type": "lc",
            "rear_port": "MPO",
            "rear_port_position": 5,
        }
        assert json.loads(interchange.export_type(panel, fmt="json"))["model"] == "MPO-12"


@pytest.mark.parametrize(
    "payload",
    [
        "::",
        "[]",
        "model: x",
        "manufacturer: a\nmodel: b\ninterfaces: nope",
        "manufacturer: a\nmodel: b\ninterfaces: [{type: x}]",
    ],
)
def test_import_rejects_bad_files(super_admin, payload) -> None:
    request = RequestFactory().post("/")
    request.user = super_admin
    with rls.system_context(), pytest.raises(ServiceError):
        interchange.import_type(request, super_admin, payload, tenant=None)


def test_library_views(admin_client: Client, make_tenant, system) -> None:
    tenant = make_tenant()
    admin_client.post("/tenants/switch/", {"tenant": tenant.pk}, secure=True)
    assert admin_client.get("/library/", secure=True).status_code == 200
    response = admin_client.post(
        "/library/new/",
        {
            "vendor_name": "Acme",
            "model": "AP-1",
            "form": "floor",
            "category": "access_point",
            "u_height": 0,
            "full_depth": "",
            "shared": "",
        },
        secure=True,
    )
    assert response.status_code == 302
    with system():
        ap = DeviceType.objects.get(model="AP-1")
    assert ap.tenant_id == tenant.pk
    url = f"/library/{ap.pk}/"
    assert admin_client.get(url, secure=True).status_code == 200
    assert admin_client.get(url + "ports/new/", secure=True).status_code == 200
    admin_client.post(
        url + "ports/new/",
        {
            "name_pattern": "eth{n}",
            "kind": "interface",
            "connector": "rj45",
            "count": 2,
            "start": 0,
            "positions": 1,
        },
        secure=True,
    )
    with system():
        template = ap.port_templates.get()
    admin_client.post(
        url,
        {
            "action": "edit",
            "vendor_name": "Acme",
            "model": "AP-1 Pro",
            "form": "floor",
            "category": "access_point",
            "u_height": 0,
        },
        secure=True,
    )
    admin_client.post(url, {"action": "remove_ports", "template": template.pk}, secure=True)
    admin_client.post(url, {"action": "remove_ports", "template": "bogus"}, secure=True)
    assert admin_client.get(url + "export/?format=json", secure=True).json()["model"] == "AP-1 Pro"
    response = admin_client.post(url, {"action": "clone"}, secure=True)
    assert response.status_code == 302
    admin_client.post(url, {"action": "delete"}, secure=True)
    from django.core.files.uploadedfile import SimpleUploadedFile

    admin_client.post(
        "/library/import/",
        {"file": SimpleUploadedFile("t.yaml", SWITCH.encode()), "shared": "on"},
        secure=True,
    )
    admin_client.post(
        "/library/import/", {"file": SimpleUploadedFile("t.yaml", b"\xff\xfe")}, secure=True
    )
    with system():
        assert DeviceType.objects.filter(model="EX-48T", tenant__isnull=True).exists()
        assert not DeviceType.objects.filter(model="AP-1 Pro").exists()


def test_other_tenant_cannot_open_private_type(
    client: Client, make_user, make_tenant, grant, make_type
) -> None:
    a, b = make_tenant(), make_tenant()
    private = make_type("Secret", tenant=a)
    user = make_user()
    grant(user, "editor", b)
    login(client, user)
    assert client.get(f"/library/{private.pk}/", secure=True).status_code == 404


def test_type_form_height_only_for_racks(admin_client: Client, make_tenant) -> None:
    tenant = make_tenant()
    admin_client.post("/tenants/switch/", {"tenant": tenant.pk}, secure=True)
    page = admin_client.get("/library/new/", secure=True).content.decode()
    assert 'data-show-if="form=rack"' in page and "Add or rename entries" in page
    labels = [line for line in page.splitlines() if '<option value="' in line and "Patch" in line]
    assert labels  # built in names are capitalised
    base = {"vendor_name": "Acme", "category": "outlet", "shared": "", "mounting": "wall"}
    floor = admin_client.post(
        "/library/new/", {**base, "model": "Outlet", "form": "floor", "u_height": ""}, secure=True
    )
    assert floor.status_code == 302
    rack = admin_client.post(
        "/library/new/",
        {**base, "model": "Rack thing", "form": "rack", "u_height": ""},
        secure=True,
    )
    assert "Rack mounted devices need a height." in rack.content.decode()
    with rls.system_context():
        outlet = DeviceType.objects.get(model="Outlet")
    assert (outlet.u_height, outlet.full_depth, outlet.mounting) == (0, False, "wall")
