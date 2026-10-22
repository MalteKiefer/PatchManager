from typing import Any

import pytest
from django.test import Client

from patchmanager.dcim import devices
from patchmanager.library import catalog
from patchmanager.library.models import CatalogType, DeviceType
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db


def _entry(spec: dict[str, Any], slug: str = "acme-x") -> CatalogType:
    return CatalogType.objects.create(
        slug=slug, manufacturer=spec["manufacturer"], model=spec["model"], spec=spec, commit="t"
    )


def test_bundled_catalog_is_loaded_and_searchable() -> None:
    assert CatalogType.objects.count() > 10_000  # loaded after migrate
    assert catalog.load() == 0  # same commit: nothing to do
    hits = list(catalog.search("panduit patch panel 24"))
    assert hits and all("Panduit" in h.manufacturer for h in hits)


def test_adopt_a_switch_with_all_ports(admin_client: Client, make_tree, system) -> None:
    t = make_tree()
    admin_client.post("/tenants/switch/", {"tenant": t["tenant"].pk}, secure=True)
    spec = {
        "manufacturer": "Acme",
        "model": "S52",
        "u_height": 1,
        "is_full_depth": True,
        "interfaces": [{"name": f"Gi1/0/{n}", "type": "1000base-t"} for n in range(1, 49)]
        + [{"name": f"Te1/1/{n}", "type": "10gbase-x-sfpp"} for n in range(1, 5)]
        + [{"name": "Port-channel1", "type": "lag"}, {"name": "mgmt", "type": "2.5gbase-t"}],
        "console-ports": [{"name": "con0", "type": "rj-45"}],
        "power-ports": [{"name": "PS1", "type": "iec-60320-c14"}],
    }
    entry = _entry(spec)
    assert admin_client.get("/library/catalog/?q=acme s52", secure=True).status_code == 200
    page = admin_client.get(f"/library/catalog/{entry.pk}/", secure=True).content.decode()
    assert "Gi1/0/48" in page
    response = admin_client.post(f"/library/catalog/{entry.pk}/", {"shared": "on"}, secure=True)
    assert response.status_code == 302
    with system():
        device_type = DeviceType.objects.get(model="S52")
        assert device_type.tenant_id is None
        device = devices.create(
            t["request"], t["request"].user, device_type=device_type, room=t["room"], name="sw1"
        )
        ports = {p.name: p for p in device.ports.all()}
    assert len(ports) == 48 + 4 + 1 + 1 + 1  # no logical LAG interface
    assert ports["Gi1/0/48"].connector == "rj45" and ports["Gi1/0/48"].speed_mbps == 1000
    assert ports["Te1/1/1"].connector == "sfp+" and ports["mgmt"].speed_mbps == 2500
    assert ports["con0"].kind == "console" and ports["PS1"].connector == "c14"


def test_adopt_a_patch_panel_keeps_front_to_rear(make_tree, super_admin, system) -> None:
    t = make_tree()
    spec = {
        "manufacturer": "Acme",
        "model": "P24",
        "u_height": 1,
        "is_full_depth": False,
        "rear-ports": [{"name": str(n), "type": "8p8c", "positions": 1} for n in range(1, 25)],
        "front-ports": [
            {"name": str(n), "type": "8p8c", "rear_port": str(n), "rear_port_position": 1}
            for n in range(1, 25)
        ],
    }
    with system():
        device_type = catalog.adopt(t["request"], super_admin, _entry(spec), tenant=t["tenant"])
        device = devices.create(
            t["request"], super_admin, device_type=device_type, room=t["room"], name="pp1"
        )
        fronts = device.ports.filter(kind="front").select_related("rear_port")
        assert fronts.count() == 24
        assert all(f.rear_port.name == f"R{f.name}" and f.rear_position == 1 for f in fronts)


def test_adopt_needs_permission(client: Client, make_tree, make_user, grant) -> None:
    from tests.conftest import login

    t = make_tree()
    user = make_user()
    grant(user, "viewer", t["tenant"])
    login(client, user)
    entry = _entry({"manufacturer": "Acme", "model": "Z"})
    page = client.get(f"/library/catalog/{entry.pk}/", secure=True).content.decode()
    assert "You may not add device types." in page
    client.post(f"/library/catalog/{entry.pk}/", {}, secure=True)
    with rls.system_context():
        assert not DeviceType.objects.filter(model="Z").exists()
