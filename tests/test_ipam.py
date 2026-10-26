import pytest

from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.dcim import devices
from patchmanager.history.models import Change
from patchmanager.ipam import services
from patchmanager.ipam.models import IPAddress, Prefix
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db


@pytest.fixture
def net(make_tree, make_type):
    t = make_tree()
    with rls.system_context():
        switch = make_type("IP switch", ports=[{"name_pattern": "ge{n}", "count": 4}])
        device = devices.create(
            t["request"], t["request"].user, device_type=switch, room=t["room"], name="sw1"
        )
    return {**t, "device": device, "actor": t["request"].user}


def _prefix(net, value, **fields):
    return services.create_prefix(
        net["request"], net["actor"], net["tenant"], prefix=value, **fields
    )


def _address(net, value, **fields):
    return services.create_address(
        net["request"], net["actor"], net["tenant"], address=value, **fields
    )


def test_values_are_validated_and_normalised(net) -> None:
    with rls.system_context():
        prefix = _prefix(net, "2001:DB8::/48")
        assert prefix.prefix == "2001:db8::/48"
        with pytest.raises(ServiceError):
            _prefix(net, "10.0.0.1/24")  # host bits set
        with pytest.raises(ServiceError):
            _prefix(net, "not a network")
        assert _address(net, "10.0.0.5").address == "10.0.0.5/32"
        with pytest.raises(ServiceError, match="already used"):
            _address(net, "10.0.0.5/24")  # same host in the same VRF
        with pytest.raises(ServiceError, match="already exists"):
            _prefix(net, "2001:db8::/48")
        with pytest.raises(ServiceError):
            _prefix(net, "192.168.0.0/24", gateway="192.168.1.1/24")  # outside


def test_tree_usage_and_free_addresses(net) -> None:
    with rls.system_context():
        parent = _prefix(net, "10.0.0.0/16", status=Prefix.Status.CONTAINER)
        a = _prefix(net, "10.0.1.0/24")
        _prefix(net, "10.0.2.0/24")
        _prefix(net, "10.0.1.0/26")  # inside a, not a direct child of parent
        assert {p.prefix for p in services.children(parent, Prefix.objects.all())} == {
            "10.0.1.0/24",
            "10.0.2.0/24",
        }
        for host in ("10.0.1.1/24", "10.0.1.2/24", "10.0.1.10/24"):
            _address(net, host)
        taken = list(services.addresses(a, IPAddress.objects.all()).values_list("host", flat=True))
        assert services.next_free(a, taken) == "10.0.1.3/24"
        assert services.free_ranges(a, taken)[:2] == [
            ("10.0.1.3", "10.0.1.9", 7),
            ("10.0.1.11", "10.0.1.254", 244),
        ]
        usage = services.usage(a, [], taken)
        assert (usage.used, usage.size, usage.percent) == (3, 254, 1)
        kids = services.children(parent, Prefix.objects.all())
        assert services.usage(parent, kids, []).used == 512
        point = _prefix(net, "10.9.9.0/31")
        assert services.next_free(point, []) == "10.9.9.0/31"  # point to point: both usable


def test_address_on_a_port_follows_the_device(net) -> None:
    with rls.system_context():
        port = net["device"].ports.get(name="ge1")
        address = _address(net, "10.0.0.7/24", port=port)
        assert address.path == net["device"].path
        assert list(port.ip_addresses.values_list("address", flat=True)) == ["10.0.0.7/24"]
        assert Change.objects.filter(object_type="ipam.ipaddress", label="10.0.0.7").exists()


def test_permissions_and_tenant_boundaries(net, make_user, grant, make_tree) -> None:
    viewer = make_user()
    grant(viewer, "viewer", net["tenant"])
    with pytest.raises(PermissionDeniedError):
        services.create_prefix(net["request"], viewer, net["tenant"], prefix="10.1.0.0/24")
    other = make_tree(name="Other")
    with rls.system_context():
        device_type = net["device"].device_type
        foreign = devices.create(
            other["request"],
            other["request"].user,
            device_type=device_type,
            room=other["room"],
            name="sw-other",
        )
        with pytest.raises(ServiceError, match="does not fit"):
            _address(net, "10.0.0.9/24", port=foreign.ports.get(name="ge1"))


def test_pages(admin_client, net, system) -> None:
    admin_client.post("/tenants/switch/", {"tenant": net["tenant"].pk}, secure=True)
    for url in ("/ipam/", "/ipam/addresses/", "/ipam/vrfs/", "/ipam/prefixes/new/"):
        assert admin_client.get(url, secure=True).status_code == 200
    admin_client.post("/ipam/vrfs/new/", {"name": "Mgmt", "rd": "65000:1"}, secure=True)
    admin_client.post(
        "/ipam/prefixes/new/", {"prefix": "10.0.0.0/16", "status": "container"}, secure=True
    )
    created = admin_client.post(
        "/ipam/prefixes/new/", {"prefix": "10.0.5.0/24", "status": "active"}, secure=True
    )
    with system():
        child = Prefix.objects.get(prefix="10.0.5.0/24")
    assert created.url == f"/ipam/prefixes/{child.pk}/"
    tree = admin_client.get("/ipam/", secure=True).content.decode()
    assert 'data-depth="1"' in tree and "10.0.5.0/24" in tree
    page = admin_client.get(created.url, secure=True).content.decode()
    assert "Add 10.0.5.1/24" in page and "10.0.5.1" in page
    with system():
        port = net["device"].ports.get(name="ge2")
    form = admin_client.get(f"/ipam/addresses/new/?port={port.pk}&address=10.0.5.1/24", secure=True)
    assert "New address on sw1 ge2" in form.content.decode()
    done = admin_client.post(
        "/ipam/addresses/new/",
        {"port": port.pk, "address": "10.0.5.1/24", "status": "active", "dns_name": "sw1"},
        secure=True,
    )
    assert done.url == f"/devices/ports/{port.pk}/"
    assert "10.0.5.1/24" in admin_client.get(done.url, secure=True).content.decode()
    assert (
        "10.0.5.1"
        in admin_client.get(f"/devices/{net['device'].pk}/ports/", secure=True).content.decode()
    )
    assert "Add 10.0.5.2/24" in admin_client.get(created.url, secure=True).content.decode()
    with system():
        address = IPAddress.objects.get(host="10.0.5.1")
    url = f"/ipam/addresses/{address.pk}/"
    assert "10.0.5.0/24" in admin_client.get(url, secure=True).content.decode()
    admin_client.post(url, {"action": "unassign"}, secure=True)
    with system():
        assert IPAddress.objects.get(pk=address.pk).port_id is None
    found = admin_client.get("/ipam/addresses/?q=sw1", secure=True).content.decode()
    assert "10.0.5.1/24" in found


def test_pages_respect_permissions(client, net, make_user, grant, system) -> None:
    from tests.conftest import login

    with system():
        prefix = _prefix(net, "10.7.0.0/24")
    viewer = make_user()
    grant(viewer, "viewer", net["tenant"])
    login(client, viewer)
    client.post("/tenants/switch/", {"tenant": net["tenant"].pk}, secure=True)
    assert client.get(f"/ipam/prefixes/{prefix.pk}/", secure=True).status_code == 200
    assert (
        client.post(
            "/ipam/prefixes/new/", {"prefix": "10.8.0.0/24", "status": "active"}, secure=True
        ).status_code
        == 403
    )
    from django.test import Client

    other = Client()
    login(other, make_user())
    assert other.get(f"/ipam/prefixes/{prefix.pk}/", secure=True).status_code == 404


def test_api(net, system) -> None:
    from tests.test_api import _api, _get, _send, _token

    api = _api(_token(net["actor"]))
    tenant = str(net["tenant"].pk)
    vrf = _send(api, "post", "/api/v1/vrfs/", {"tenant": tenant, "name": "Core"})
    assert vrf.status_code == 201, vrf.content
    prefix = _send(
        api,
        "post",
        "/api/v1/prefixes/",
        {"tenant": tenant, "vrf": vrf.json()["id"], "prefix": "10.20.0.0/24", "status": "active"},
    )
    assert prefix.status_code == 201, prefix.content
    bad = _send(api, "post", "/api/v1/prefixes/", {"tenant": tenant, "prefix": "10.20.0.1/24"})
    assert bad.status_code == 400
    with system():
        port = net["device"].ports.get(name="ge3")
    address = _send(
        api,
        "post",
        "/api/v1/ip-addresses/",
        {
            "tenant": tenant,
            "vrf": vrf.json()["id"],
            "address": "10.20.0.9/24",
            "port": str(port.pk),
        },
    )
    assert address.status_code == 201, address.content
    assert address.json()["address"] == "10.20.0.9/24"
    inside = _get(api, "/api/v1/ip-addresses/", within="10.20.0.0/24").json()["results"]
    assert [a["address"] for a in inside] == ["10.20.0.9/24"]
    assert _get(api, "/api/v1/ip-addresses/", within="nonsense").status_code == 400
    assert _get(api, "/api/v1/prefixes/", q="10.20").json()["count"] == 1
    moved = _send(api, "patch", f"/api/v1/ip-addresses/{address.json()['id']}/", {"port": None})
    assert moved.status_code == 200 and moved.json()["port"] is None
    assert _send(api, "delete", f"/api/v1/vrfs/{vrf.json()['id']}/", {}).status_code == 400


def test_search_finds_addresses_and_prefixes(net, system) -> None:
    from patchmanager.search import services as search

    with system():
        _prefix(net, "172.16.40.0/24", description="Printers")
        _address(net, "172.16.40.12/24", dns_name="printer-2.example.test")
        results, counts = search.search(net["actor"], net["tenant"], "172.16.40")
        assert counts["prefix"] == 1 and counts["ipaddress"] == 1
        results, counts = search.search(net["actor"], net["tenant"], "printer-2")
        assert [str(h.obj) for h in results["ipaddress"]] == ["172.16.40.12/24"]


def test_import_and_export(net, system) -> None:
    from patchmanager.exchange import importer
    from patchmanager.exchange.tables import TABLES

    def run(kind: str, rows: list[dict[str, str]]) -> list[str]:
        report = importer.run(net["request"], net["actor"], net["tenant"], kind, rows, apply=True)
        return [r.outcome for r in report]

    good = {"prefix": "10.30.0.0/24", "status": "active", "site": "HQ", "gateway": "10.30.0.1/24"}
    with system():
        assert run("prefixes", [good, {"prefix": "10.30.0.1/24"}])[1] == "error"
        assert not Prefix.objects.filter(prefix="10.30.0.0/24").exists()  # all or nothing
        assert run("prefixes", [good]) == ["created"]
        assert Prefix.objects.filter(prefix="10.30.0.0/24", site__name="HQ").exists()
        addresses = [
            {"address": "10.30.0.20/24", "dns_name": "ap1", "device": "sw1", "port": "ge4"}
        ]
        assert run("addresses", addresses) == ["created"]
        assert IPAddress.objects.get(host="10.30.0.20").port.name == "ge4"
        exported = list(TABLES["addresses"].export(net["actor"], net["tenant"], None))
        assert exported[0]["device"] == "sw1" and exported[0]["port"] == "ge4"
        assert run("addresses", addresses) == ["unchanged"]
        assert run("addresses", [{**addresses[0], "port": "ge99"}]) == ["error"]


def test_hidden_devices_stay_hidden_next_to_addresses(client, net, make_user, grant, system):
    from patchmanager.accounts.models import Assignment, Role
    from patchmanager.search import services as search
    from tests.conftest import login

    with system():
        prefix = _prefix(net, "10.50.0.0/24")
        _address(net, "10.50.0.5/24", port=net["device"].ports.get(name="ge1"))
    user = make_user()
    grant(user, "viewer", net["tenant"])
    hide = Role.objects.create(
        key="hide-devices", name="Hide devices", permissions=["dcim.device.view"]
    )
    Assignment.objects.create(
        role=hide, user=user, tenant=net["tenant"], scope_path=net["device"].path, effect="deny"
    )
    login(client, user)
    client.post("/tenants/switch/", {"tenant": net["tenant"].pk}, secure=True)
    for url in ("/ipam/addresses/", f"/ipam/prefixes/{prefix.pk}/"):
        page = client.get(url, secure=True).content.decode()
        assert "10.50.0.5/24" in page and "sw1" not in page, url
        assert "not visible to you" in page
    with system():
        results, _counts = search.search(user, net["tenant"], "10.50.0")
        hits = results["ipaddress"]
        assert hits and all("sw1" not in h.context for h in hits)


def test_choices_only_show_what_the_user_may_see(client, net, make_user, grant, make_tree):
    from patchmanager.accounts.models import Assignment, Role
    from patchmanager.locations import services as tree
    from patchmanager.locations.models import Site
    from tests.conftest import login

    with rls.system_context():
        secret = tree.create(
            net["request"], net["actor"], Site, parent=net["tenant"], name="Secret site"
        )
    user = make_user()
    grant(user, "editor", net["tenant"])
    hide = Role.objects.create(key="hide-sites", name="Hide", permissions=["locations.site.view"])
    Assignment.objects.create(
        role=hide, user=user, tenant=net["tenant"], scope_path=secret.path, effect="deny"
    )
    login(client, user)
    client.post("/tenants/switch/", {"tenant": net["tenant"].pk}, secure=True)
    for url in ("/ipam/prefixes/new/", "/networks/vlans/new/"):
        page = client.get(url, secure=True).content.decode()
        assert "Secret site" not in page and "HQ" in page, url
