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
