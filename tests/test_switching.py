import pytest
from django.test import Client

from patchmanager.dcim import devices
from patchmanager.dcim.models import Port
from patchmanager.ipam.models import IPAddress
from patchmanager.networks import aruba, switchconfig
from patchmanager.networks.models import (
    VLAN,
    BGPNeighbor,
    MSTInstance,
    PortProfile,
    StaticRoute,
    SwitchSettings,
    TaggedVLAN,
)
from patchmanager.tenancy import rls

# The Aruba Central sample "PortBasedTunnel-Template" (MIT), shortened.
TEMPLATE = """%_sys_template_header%
hostname %hostname%
include-credentials
password manager user-name "admin" sha1 "HASH_PASSWORD"
tunneled-node-server
   controller-ip %tunneldst1%
   backup-controller-ip %tunneldst2%
   exit
interface 1
   tunneled-node-server
   exit
snmp-server community "COMMUNITY" unrestricted
vlan 1
   name "DEFAULT_VLAN"
   no untagged 1-7
   untagged 8-10
   ip address dhcp-bootp
   exit
vlan 177
   name "GUESTS"
   no ip address
%if tunneldst2%
   tagged 8    //Only with a second gateway.
%endif%
   jumbo
   exit
vlan 100
   name "USERS"
   untagged 1-7
   no ip address
   exit
device-profile name "ARUBA-AP"
   untagged-vlan 100
   tagged-vlan 177
   allow-jumbo-frames
   exit
device-profile type "aruba-ap"
   associate "ARUBA-AP"
   enable
   exit
"""

ROUTED = """; JL258A Configuration Editor; Created on release #WC.16.10.0009
hostname "CORE-1"
trunk 9-10 trk1 lacp
ip routing
ip route 0.0.0.0 0.0.0.0 192.0.2.1
ip route 10.20.0.0/16 192.0.2.2 distance 20
interface 2
   name "Printer floor 2"
   disable
   exit
vlan 20
   name "SERVERS"
   untagged 3-4
   tagged Trk1
   ip address 10.20.0.1 255.255.255.0
   ip ospf area 0.0.0.0
   exit
spanning-tree
spanning-tree 1-8 admin-edge-port
spanning-tree 1 bpdu-protection
spanning-tree priority 2
spanning-tree config-name "CAMPUS"
spanning-tree config-revision 3
spanning-tree instance 1 vlan 20-21
router ospf
   router-id 10.255.0.1
   area backbone
   enable
   exit
router bgp 65001
   bgp router-id 10.255.0.1
   neighbor 192.0.2.9 remote-as 65002
   neighbor 192.0.2.9 description "Provider"
   enable
   exit
vsf member 1
   type "JL258A" mac-address 001122-334455
   priority 255
   exit
logging 192.0.2.50
radius-server host 192.0.2.60 key "s3cret"
"""


def test_template_with_variables() -> None:
    config = aruba.parse(TEMPLATE, {"hostname": "SW-01", "tunneldst1": "10.1.1.1"})
    assert config.hostname == "SW-01" and config.gateways == ["10.1.1.1"]
    assert config.vlans[100].untagged == [str(n) for n in range(1, 8)]
    assert config.vlans[1].untagged == ["8", "9", "10"] and config.vlans[1].dhcp
    assert config.vlans[177].tagged == []  # %if tunneldst2% is false
    assert config.vlans[177].jumbo and config.interfaces["1"].tunneled
    assert config.profiles["ARUBA-AP"].detects == "aruba-ap"
    assert config.secrets_skipped == 3
    assert "%tunneldst2%" in " ".join(config.warnings)
    with_backup = aruba.parse(
        TEMPLATE, {"hostname": "x", "tunneldst1": "10.1.1.1", "tunneldst2": "10.1.1.2"}
    )
    assert with_backup.vlans[177].tagged == ["8"] and len(with_backup.gateways) == 2


def test_routed_switch_and_roundtrip() -> None:
    config = aruba.parse(ROUTED)
    assert config.platform.startswith("JL258A")
    assert config.trunks["Trk1"].members == ["9", "10"] and config.trunks["Trk1"].protocol == "lacp"
    assert [(r.prefix, r.next_hop, r.distance) for r in config.routes] == [
        ("0.0.0.0/0", "192.0.2.1", None),
        ("10.20.0.0/16", "192.0.2.2", 20),
    ]
    assert (
        config.vlans[20].addresses == ["10.20.0.1/24"] and config.vlans[20].ospf_area == "0.0.0.0"  # noqa: S104  # a route, not a bind address
    )
    assert config.interfaces["2"].label == "Printer floor 2" and not config.interfaces["2"].enabled
    assert config.interfaces["5"].edge and config.interfaces["1"].bpdu_guard
    assert (config.stp_mode, config.stp_priority, config.mst_region) == ("mstp", 8192, "CAMPUS")
    assert config.mst_instances == {1: ([20, 21], None)}
    assert config.bgp_asn == 65001 and config.neighbors["192.0.2.9"].description == "Provider"
    assert config.members[1].priority == 255 and config.stack_kind == "vsf"
    assert config.secrets_skipped == 1 and "s3cret" not in repr(config)
    again = aruba.parse(aruba.render(config))
    for name in (
        "vlans",
        "trunks",
        "routes",
        "stp_priority",
        "mst_instances",
        "neighbors",
        "bgp_asn",
        "ospf_router_id",
        "hostname",
    ):
        assert getattr(again, name) == getattr(config, name), name
    assert again.interfaces["2"] == config.interfaces["2"]


@pytest.mark.parametrize(
    ("spec", "names"),
    [
        ("1-3,5", ["1", "2", "3", "5"]),
        ("A1-A3", ["A1", "A2", "A3"]),
        ("1/1-1/3,Trk2", ["1/1", "1/2", "1/3", "Trk2"]),
    ],
)
def test_port_ranges(spec, names) -> None:
    assert aruba.expand(spec) == names
    assert aruba.expand(aruba.compress(names)) == names


@pytest.mark.parametrize("spec", ["1-A3", "5-1", "1-5000", "1" * 100_000 + "x"])
def test_bad_ranges(spec) -> None:
    with pytest.raises(aruba.ParseError):
        aruba.expand(spec)


def test_long_lines_are_refused() -> None:
    with pytest.raises(aruba.ParseError):
        aruba.parse("vlan 1\n   untagged " + "1" * 5000 + "\n")


def test_bad_lines_become_warnings() -> None:
    config = aruba.parse("vlan 10\n   untagged 1-A2\n   exit\nip route 300.0.0.0/8 1.1.1.1\n")
    assert len(config.warnings) == 2 and config.vlans[10].untagged == []


@pytest.fixture
def switch(make_tree, make_type, super_admin):
    t = make_tree()
    with rls.system_context():
        model = make_type(
            "2930F-10G", ports=[{"name_pattern": "{n}", "count": 10, "speed_mbps": 1000}]
        )
        device = devices.create(
            t["request"],
            super_admin,
            device_type=model,
            room=t["room"],
            rack=t["rack"],
            position=1,
            name="SW-01",
        )
    return {**t, "device": device}


def _apply(switch, text, commit=True, variables=None):
    config = aruba.parse(text, variables)
    return switchconfig.apply(
        switch["request"],
        switch["request"].user,
        switch["device"],
        config,
        commit=commit,
        source="test",
    )


def test_apply_preview_then_commit(switch, system) -> None:
    with system():
        preview = _apply(switch, ROUTED, commit=False)
        assert preview.changes and not VLAN.objects.filter(vid=20).exists()
        assert not SwitchSettings.objects.exists()
        result = _apply(switch, ROUTED)
        assert result.changes == preview.changes
        device = switch["device"]
        port3 = Port.objects.get(device=device, name="3")
        assert port3.untagged_vlan.vid == 20 and port3.vlan_mode == "access"
        lag = Port.objects.get(device=device, name="Trk1")
        assert lag.kind == "lag" and lag.lag_protocol == "lacp"
        assert set(Port.objects.filter(lag=lag).values_list("name", flat=True)) == {"9", "10"}
        assert TaggedVLAN.objects.filter(port=lag, vlan__vid=20).exists()
        svi = Port.objects.get(device=device, name="vlan 20")
        assert IPAddress.objects.get(port=svi).host == "10.20.0.1" and svi.ospf_area == "0.0.0.0"  # noqa: S104  # a route, not a bind address
        port2 = Port.objects.get(device=device, name="2")
        assert port2.label == "Printer floor 2" and not port2.enabled
        settings = SwitchSettings.objects.get(device=device)
        assert (settings.hostname, settings.bgp_asn, settings.stp_priority) == (
            "CORE-1",
            65001,
            8192,
        )
        assert StaticRoute.objects.filter(device=device).count() == 2
        assert BGPNeighbor.objects.get(device=device).remote_asn == 65002
        assert MSTInstance.objects.get(device=device).vlans == [20, 21]
        assert _apply(switch, ROUTED).changes == []  # nothing left to change
        exported = aruba.parse(aruba.render(switchconfig.document(device, switch["request"].user)))
        assert exported.vlans[20].untagged == ["3", "4"] and exported.vlans[20].tagged == ["Trk1"]
        assert exported.trunks["Trk1"].members == ["9", "10"]


def test_apply_reports_what_it_leaves(switch, system) -> None:
    with system():
        VLAN.objects.create(tenant=switch["tenant"], site=switch["site"], vid=100, name="Staff")
        result = _apply(switch, TEMPLATE, variables={"hostname": "SW", "tunneldst1": "10.1.1.1"})
        skipped = " ".join(result.skipped)
        assert "VLAN 100 is called Staff" in skipped  # documented name wins
        assert "secrets" in skipped
        assert VLAN.objects.get(vid=100).name == "Staff"
        assert PortProfile.objects.get(device=switch["device"]).untagged_vid == 100
        wide = _apply(switch, "vlan 5\n   untagged 48\n   exit\n")
        assert "Port 48 is not on this device." in wide.skipped


def test_pages(admin_client: Client, switch, system) -> None:
    device = switch["device"]
    admin_client.post("/tenants/switch/", {"tenant": switch["tenant"].pk}, secure=True)
    base = f"/devices/{device.pk}/network/"
    assert "Nothing documented yet" in admin_client.get(base, secure=True).content.decode()
    preview = admin_client.post(
        base + "read/", {"content": ROUTED, "variables": "", "action": "preview"}, secure=True
    )
    page = preview.content.decode()
    assert "Apply changes" in page and "New VLAN 20" in page
    with system():
        assert not VLAN.objects.filter(vid=20).exists()
    applied = admin_client.post(
        base + "read/", {"content": ROUTED, "variables": "", "action": "apply"}, secure=True
    )
    assert applied.url == base
    page = admin_client.get(base, secure=True).content.decode()
    assert "SERVERS" in page and "192.0.2.9" in page and "CAMPUS" in page
    text = admin_client.get(base + "config.txt", secure=True).content.decode()
    assert 'hostname "CORE-1"' in text and "trunk 9-10 trk1 lacp" in text
    bad = admin_client.post(
        base + "read/", {"content": "x", "variables": "[1", "action": "preview"}, secure=True
    )
    assert "not valid JSON" in bad.content.decode()


def test_read_needs_change_permission(client: Client, switch, make_user, grant) -> None:
    from tests.conftest import login

    viewer = make_user()
    grant(viewer, "viewer", switch["tenant"])
    login(client, viewer)
    base = f"/devices/{switch['device'].pk}/network/"
    assert client.get(base, secure=True).status_code == 200
    assert client.get(base + "read/", secure=True).status_code == 403


def test_switching_follows_a_site_to_another_tenant(switch, make_tenant, system) -> None:
    from patchmanager.locations import services as tree
    from patchmanager.networks.models import Stack, StackMember

    target = make_tenant()
    with system():
        # Addresses keep a site in place (checked elsewhere), so this one has none.
        _apply(switch, ROUTED.replace("   ip address 10.20.0.1 255.255.255.0\n", ""))
        device = switch["device"]
        stack = Stack.objects.create(tenant=switch["tenant"], site=switch["site"], name="Core")
        StackMember.objects.create(tenant=switch["tenant"], stack=stack, device=device, member_id=1)
        tree.move_site(switch["request"], switch["request"].user, switch["site"], target)
        assert SwitchSettings.objects.get(device=device).tenant_id == target.pk
        assert StaticRoute.objects.filter(device=device, tenant=target).count() == 2
        assert StackMember.objects.get(device=device).tenant_id == target.pk


def test_stacks(admin_client: Client, switch, make_type, super_admin, system) -> None:
    from patchmanager.networks.models import Stack, StackMember

    with system():
        second = devices.create(
            switch["request"],
            super_admin,
            device_type=switch["device"].device_type,
            room=switch["room"],
            rack=switch["rack"],
            position=3,
            name="SW-02",
        )
    admin_client.post("/tenants/switch/", {"tenant": switch["tenant"].pk}, secure=True)
    created = admin_client.post(
        "/networks/stacks/new/",
        {
            "name": "Core",
            "kind": "vsf",
            "site": switch["site"].pk,
            "domain": "1",
            "description": "",
        },
        secure=True,
    )
    with system():
        stack = Stack.objects.get(name="Core")
    assert created.url == f"/networks/stacks/{stack.pk}/"
    for device, number, role in ((switch["device"], 1, "primary"), (second, 2, "standby")):
        admin_client.post(
            f"/networks/stacks/{stack.pk}/members/new/",
            {"device": device.pk, "member_id": number, "role": role, "priority": ""},
            secure=True,
        )
    page = admin_client.get(f"/networks/stacks/{stack.pk}/", secure=True).content.decode()
    assert "SW-01" in page and "SW-02" in page
    overview = admin_client.get(f"/devices/{second.pk}/network/", secure=True).content.decode()
    assert "Core" in overview and "SW-01" in overview
    again = admin_client.post(
        f"/networks/stacks/{stack.pk}/members/new/",
        {"device": second.pk, "member_id": 3, "role": "member", "priority": ""},
        secure=True,
    )
    assert again.status_code == 200  # already in a stack: not offered
    with system():
        member = StackMember.objects.get(device=second)
    admin_client.post(
        f"/networks/stacks/{stack.pk}/", {"action": "remove", "member": member.pk}, secure=True
    )
    admin_client.post(f"/networks/stacks/{stack.pk}/", {"action": "delete"}, secure=True)
    with system():
        assert not Stack.objects.exists() and not StackMember.objects.exists()


def test_port_joins_only_own_lag(switch, make_tree, make_type, super_admin, system) -> None:
    from patchmanager.accounts.services import ServiceError

    with system():
        _apply(switch, ROUTED)
        device = switch["device"]
        port = Port.objects.get(device=device, name="5")
        lag = Port.objects.get(device=device, name="Trk1")
        devices.update_port(switch["request"], super_admin, port, lag=lag, stp_edge=True)
        assert Port.objects.get(pk=port.pk).lag_id == lag.pk
        other = make_tree(name="Other")
        foreign = devices.create(
            other["request"],
            super_admin,
            device_type=device.device_type,
            room=other["room"],
            name="SW-X",
        )
        with pytest.raises(ServiceError):
            devices.update_port(switch["request"], super_admin, foreign.ports.first(), lag=lag)


def test_limits_hold_after_template_variables() -> None:
    long_value = {"x": "1" * 900}
    with pytest.raises(aruba.ParseError):  # 3 variables of 900 characters in one line
        aruba.parse("hostname %x%%x%%x%\n", long_value)
    with pytest.raises(aruba.ParseError):
        aruba.parse("hostname a\n" * (aruba.MAX_LINES + 1))
    many = "".join(f"vlan {v}\n   untagged 1-1000\n   exit\n" for v in range(1, 30))
    with pytest.raises(aruba.TooLargeError):
        aruba.parse(many)


def test_rendered_values_cannot_inject_lines() -> None:
    config = aruba.Config(hostname='SW"\nip routing')
    config.vlans[5] = aruba.Vlan(5, name="Guests\nvlan 6", untagged=["1\n2"])
    config.interfaces["1"] = aruba.Interface("1", label='x"\r\ndisable')
    text = aruba.render(config)
    assert "\nip routing" not in text and "\nvlan 6" not in text and "\ndisable" not in text
    again = aruba.parse(text)
    assert not again.ip_routing and list(again.vlans) == [5] and again.interfaces["1"].enabled
    with pytest.raises(aruba.ParseError):
        aruba._vlan(aruba.Config(), 7, ["ip ospf area 1;reload"])


def test_other_line_breaks_stay_inside_a_value() -> None:
    config = aruba.Config(hostname="SW\N{LINE SEPARATOR}ip routing\x85vlan 9\x1cdisable")
    text = aruba.render(config)
    assert text.count("\n") == 1  # only the end of the hostname line
    again = aruba.parse(text)
    assert not again.ip_routing and not again.vlans
    with pytest.raises(aruba.ParseError):  # a variable cannot stretch a line past the limit
        aruba.parse("hostname %a%%a%%a%\n", {"a": "x" * 999})


def test_interface_block_applies_to_its_whole_range() -> None:
    config = aruba.parse('interface 1-3\n   name "Desk"\n   disable\n   exit\n')
    assert [(p.label, p.enabled) for p in config.interfaces.values()] == [("Desk", False)] * 3


def test_read_from_file_and_stored_version(admin_client: Client, switch, system) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    from patchmanager.configs import services as configs

    device = switch["device"]
    admin_client.post("/tenants/switch/", {"tenant": switch["tenant"].pk}, secure=True)
    base = f"/devices/{device.pk}/network/"
    upload = SimpleUploadedFile("core.cfg", ROUTED.encode())
    page = admin_client.post(
        base + "read/", {"file": upload, "action": "preview", "platform": ""}, secure=True
    ).content.decode()
    assert "Read as ArubaOS-Switch" in page and "New VLAN 20" in page
    with system():
        configs.add(switch["request"], switch["request"].user, device, ROUTED)
    stored = admin_client.post(
        base + "read/", {"version": 1, "action": "apply", "platform": "aruba-aos-s"}, secure=True
    )
    assert stored.url == base
    with system():
        assert SwitchSettings.objects.get(device=device).source == "v1"
    download = admin_client.get(base + "config.txt?download=1", secure=True)
    assert download["Content-Disposition"] == 'attachment; filename="sw-01.cfg"'
    unknown = admin_client.post(
        base + "read/", {"content": "hello", "action": "preview"}, secure=True
    )
    assert "not recognised" in unknown.content.decode()


def test_aruba_if_lines_are_rejected_in_linear_time() -> None:
    import time

    started = time.monotonic()
    aruba.parse(("%if a =" + " " * 1990 + "x\n") * 50, {})
    assert time.monotonic() - started < 2
