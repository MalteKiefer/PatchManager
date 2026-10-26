from pathlib import Path

import pytest

from patchmanager.networks import platforms, switchconfig
from patchmanager.networks.platforms import common, lancom, routeros

pytestmark = pytest.mark.django_db

SAMPLES = Path(__file__).parent / "configs"


def _read(name: str) -> str:
    return (SAMPLES / name).read_text()


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("cisco_ios.txt", "cisco"),
        ("fortios.txt", "fortios"),
        ("junos.txt", "junos"),
        ("routeros.rsc", "routeros"),
        ("comware.txt", "comware"),
        ("lancom.lcs", "lancom"),
    ],
)
def test_detection(name, key) -> None:
    detected = platforms.detect(_read(name))
    assert detected is not None and detected.key == key
    assert platforms.detect("hello world") is None


def test_cisco() -> None:
    config = platforms.parse(_read("cisco_ios.txt"))
    assert (config.hostname, config.domain, config.dns_servers) == (
        "ACC-01",
        "office.lan",
        ["10.0.0.53", "1.1.1.1"],
    )
    assert (config.stp_mode, config.stp_priority) == ("rpvst", 8192)
    assert config.vlans[10].untagged == ["GigabitEthernet1/0/1"]
    assert config.vlans[10].tagged == ["Port-channel1"] and config.vlans[99].untagged == [
        "Port-channel1"
    ]
    assert config.vlans[10].addresses == ["10.10.0.1/24"] and config.vlans[10].helpers == [
        "10.0.0.5"
    ]
    assert config.trunks["Port-channel1"].protocol == "lacp"
    pool = config.pools["USERS"]
    assert (pool.start, pool.end, pool.lease_seconds) == ("10.10.0.21", "10.10.0.254", 28800)
    assert pool.reservations == [("00:11:22:33:44:55", "10.10.0.30", "PRINTER-1")]
    assert (
        config.secrets_skipped >= 3
        and "$9$" not in repr(config)
        and "cisco" not in repr(config.interfaces)
    )


def test_fortios() -> None:
    config = platforms.parse(_read("fortios.txt"))
    assert config.hostname == "FW-01" and config.vlans[20].tagged == ["internal1"]
    assert config.interfaces["wan1"].addresses == ["192.0.2.10/24"]
    assert config.trunks["agg1"].members == ["internal3", "internal4"]
    assert config.pools["USERS"].reservations[0][2] == "Printer"
    assert config.secrets_skipped == 2 and "ENC" not in repr(config)


def test_junos_braces() -> None:
    config = platforms.parse(_read("junos.txt"))
    assert config.vlans[10].name == "USERS" and config.vlans[10].untagged == ["ge-0/0/0"]
    assert config.vlans[10].tagged == ["ae0"] and config.vlans[20].tagged == ["ae0"]
    assert config.trunks["ae0"].protocol == "lacp" and not config.interfaces["ge-0/0/1"].enabled
    assert config.bgp_asn == 65030 and config.interfaces["ge-0/0/0"].edge
    assert config.stp_priority == 8192 and config.vlans[10].ospf_passive


def test_routeros() -> None:
    config = platforms.parse(_read("routeros.rsc"))
    assert config.hostname == "RB-01" and config.vlans[10].tagged == ["bond1"]
    assert config.pools["10.40.0.0/24"].gateway == "10.40.0.1"  # continued line
    assert config.secrets_skipped == 1 and "secret123" not in repr(config)
    with pytest.raises(common.ParseError):
        routeros.parse("/ip address\nadd address=\\\n" + "x\\\n" * 5000 + "1.1.1.1/24\n")


def test_comware() -> None:
    config = platforms.parse(_read("comware.txt"))
    assert (config.hostname, config.stp_mode, config.stp_priority) == ("CORE-H3C", "rstp", 4096)
    assert config.routes[0].next_hop == "192.0.2.1" and config.members[1].priority == 32
    assert (
        config.vlans[20].name == "VOICE" and config.trunks["Bridge-Aggregation1"].protocol == "lacp"
    )
    huawei = platforms.parse(
        " sysname HW-1\n#\ninterface Eth-Trunk1\n port link-type trunk\n"
        " port trunk allow-pass vlan 10 to 12\n mode lacp\n#\ninterface GigabitEthernet0/0/1\n"
        " eth-trunk 1\n#\ninterface Vlanif10\n ip address 10.1.0.1 24\n#\nreturn\n",
        "comware",
    )
    assert huawei.vendor == "huawei" and huawei.vlans[11].tagged == ["Eth-Trunk1"]
    assert huawei.trunks["Eth-Trunk1"].members == ["GigabitEthernet0/0/1"]
    assert huawei.vlans[10].addresses == ["10.1.0.1/24"]


def test_lancom() -> None:
    config = platforms.parse(_read("lancom.lcs"))
    assert config.platform == "LCOS 10.80.0123" and config.hostname == "LC-01"
    assert config.vlans[60].untagged == ["LAN-2"] and config.vlans[60].tagged == ["LAN-3"]
    assert config.interfaces["LAN-4"].mode == "access"  # Ingress-mixed never sends tags
    assert config.interfaces["LAN-1"].addresses == ["192.168.1.1/24"]
    assert config.routes[0].prefix == "0.0.0.0/0" and config.routes[0].interface == "INTERNET"
    assert config.pools["GUESTS"].lease_seconds == 480 * 60
    assert config.pools["GUESTS"].reservations[0][0] == "00:a0:57:00:11:aa"
    assert config.secrets_skipped == 1
    with pytest.raises(common.ParseError, match="English"):
        lancom.parse("lang Deutsch\n")
    with pytest.raises(common.ParseError, match="numeric"):
        lancom.parse("cd /2/7/30\n")


def test_names_and_budget() -> None:
    assert common.canonical("Gi1/0/1") == common.canonical("GigabitEthernet1/0/1")
    assert common.canonical("Te1/1/1") == "tengigabitethernet1/1/1"
    assert common.canonical("Po1") == common.canonical("port-channel1")
    assert common.mask_to_prefix("0.0.0.255") == 24 and common.mask_to_prefix("255.255.0.0") == 16
    with pytest.raises(common.ParseError):
        common.mask_to_prefix("255.0.255.0")
    many = "".join(
        f"interface GigabitEthernet1/0/{n}\n switchport mode trunk\n" for n in range(1, 30)
    ) + "".join(f"vlan {v}\n" for v in range(1, 1000))
    with pytest.raises(common.TooLargeError):
        platforms.parse("version 17.9\n" + many, "cisco")


def test_apply_cisco_with_dhcp_dns_and_routed_port(
    make_tree, make_type, super_admin, system
) -> None:
    from patchmanager.dcim import devices
    from patchmanager.ipam.models import DHCPScope, IPAddress, Prefix
    from patchmanager.networks.models import StaticRoute, SwitchSettings

    tree = make_tree()
    with system():
        model = make_type(
            "C9300-48",
            ports=[{"name_pattern": "GigabitEthernet1/0/{n}", "count": 48, "speed_mbps": 1000}],
        )
        device = devices.create(
            tree["request"], super_admin, device_type=model, room=tree["room"], name="ACC-01"
        )
        Prefix.objects.create(tenant=tree["tenant"], prefix="10.10.0.0/24")
        text = _read("cisco_ios.txt").replace(
            "interface GigabitEthernet1/0/2\n shutdown",
            "interface Gi1/0/2\n no switchport\n ip address 192.0.2.50 255.255.255.252",
        )
        result = switchconfig.apply(
            tree["request"], super_admin, device, platforms.parse(text), commit=True, source="test"
        )
        assert "New DHCP scope USERS" in result.changes
        scope = DHCPScope.objects.get()
        assert (str(scope.start).split("/")[0], scope.relays, scope.server_id) == (
            "10.10.0.21",
            "10.0.0.5",
            device.pk,
        )
        assert scope.reservations.get().mac_address == "00:11:22:33:44:55"
        settings = SwitchSettings.objects.get(device=device)
        assert settings.dns_servers == ["10.0.0.53", "1.1.1.1"] and settings.domain == "office.lan"
        routed = IPAddress.objects.get(host="192.0.2.50")
        assert routed.port.name == "GigabitEthernet1/0/2"  # Gi1/0/2 matched
        assert StaticRoute.objects.filter(device=device).count() == 1
        again = switchconfig.apply(
            tree["request"], super_admin, device, platforms.parse(text), commit=True, source="test"
        )
        assert again.changes == []
        from patchmanager.audit.models import AuditEvent

        # The unchanged scope of the second apply is not audited again.
        assert AuditEvent.objects.filter(action__startswith="ipam.dhcpscope.").count() == 1


def test_apply_reports_a_rejected_dhcp_scope_only_as_skipped(
    make_tree, make_type, super_admin, system
) -> None:
    from patchmanager.dcim import devices
    from patchmanager.ipam.models import Prefix

    tree = make_tree()
    with system():
        device = devices.create(
            tree["request"], super_admin, device_type=make_type("C9300"), room=tree["room"]
        )
        Prefix.objects.create(tenant=tree["tenant"], prefix="10.10.0.0/24")
        text = _read("cisco_ios.txt").replace(
            "default-router 10.10.0.1", "default-router 10.99.0.1"
        )
        result = switchconfig.apply(
            tree["request"], super_admin, device, platforms.parse(text), commit=False
        )
    assert not any("DHCP scope" in change for change in result.changes)
    assert any(item.startswith("USERS:") for item in result.skipped)
    with system():
        config = platforms.parse(_read("cisco_ios.txt"))
        config.pools["USERS"].reservations.append(("00:11:22:33:44:66", "10.99.0.9", "x"))
        result = switchconfig.apply(tree["request"], super_admin, device, config, commit=False)
    # The reservation outside the prefix is skipped, not counted.
    assert "Reservations of USERS: 1" in result.changes
    assert any(item.startswith("10.99.0.9:") for item in result.skipped)


def test_arubacx() -> None:
    text = _read("arubacx.txt")
    assert platforms.detect(text).key == "aruba-aos-cx"
    config = platforms.parse(text)
    assert config.platform == "Version ArubaOS-CX GL.10.13.1005" and config.hostname == "CX-01"
    assert config.dns_servers == ["10.0.0.53"] and config.domain == "office.lan"
    assert config.members[1].priority == 255 and config.members[2].model == "jl658a"
    assert (config.stp_mode, config.stp_priority) == ("rpvst", 8192)
    assert config.vlans[10].untagged == ["1/1/1"] and config.vlans[10].tagged == ["lag1"]
    assert config.vlans[1].untagged == ["lag1"]
    assert config.interfaces["1/1/1"].edge and config.interfaces["1/1/1"].bpdu_guard
    assert not config.interfaces["1/1/2"].enabled  # AOS-CX ports are shut by default
    assert config.trunks["lag1"].members == ["1/1/47", "1/1/48"]
    assert config.trunks["lag1"].protocol == "lacp"
    assert config.interfaces["1/1/49"].addresses == ["192.0.2.6/30"]
    assert config.vlans[10].helpers == ["10.0.0.5"]
    assert config.vlans[10].ospf_area == "0.0.0.0"  # noqa: S104  # an OSPF area
    assert config.routes[0].distance == 5 and config.neighbors["192.0.2.5"].description == "Uplink"
    pool = config.pools["USERS"]
    assert (pool.network, pool.lease_seconds) == ("10.70.0.0/24", 28800)
    assert pool.reservations == [("00:11:22:33:44:66", "10.70.0.20", "")]
    assert config.secrets_skipped >= 1 and "AQBapW" not in repr(config)


def test_junos_set_format() -> None:
    config = platforms.parse(
        "set system host-name SRX-1\n"
        "set interfaces ge-0/0/0 unit 0 family inet address 192.0.2.2/30\n"
        "set interfaces ge-0/0/1 unit 0 family ethernet-switching port-mode trunk\n"
        "set interfaces ge-0/0/1 unit 0 family ethernet-switching vlan members all\n"
        "set interfaces ge-0/0/1 native-vlan-id 99\n"
        "set vlans MGMT vlan-id 99\n"
        "set protocols mstp configuration-name CAMPUS\n"
        "set protocols mstp msti 1 vlan 10-20\n"
        "set protocols bgp group ISP neighbor 192.0.2.1 peer-as 65000\n"
        "set protocols bgp group ISP neighbor 192.0.2.1 description ISP\n"
        "set access address-assignment pool P1 family inet network 10.0.0.0/24\n"
        "set access address-assignment pool P1 family inet range r1 low 10.0.0.10\n"
        "set access address-assignment pool P1 family inet range r1 high 10.0.0.20\n"
        "set access address-assignment pool P1 family inet dhcp-attributes router 10.0.0.1\n"
        'set system root-authentication encrypted-password "$6$x"\n'
    )
    assert config.hostname == "SRX-1" and config.interfaces["ge-0/0/0"].addresses == [
        "192.0.2.2/30"
    ]
    assert config.vlans[99].untagged == ["ge-0/0/1"]
    assert config.mst_region == "CAMPUS" and config.mst_instances[1][0] == list(range(10, 21))
    assert config.neighbors["192.0.2.1"].description == "ISP"
    pool = config.pools["P1"]
    assert (pool.start, pool.end, pool.gateway) == ("10.0.0.10", "10.0.0.20", "10.0.0.1")
    assert config.secrets_skipped == 1


def test_cisco_mst_and_nxos_details() -> None:
    config = platforms.parse(
        "feature lacp\nhostname NX-1\nspanning-tree mode mst\n"
        "spanning-tree mst configuration\n name CAMPUS\n revision 2\n instance 1 vlan 10-20\n"
        "spanning-tree mst 0 priority 4096\nspanning-tree mst 1 priority 8192\n"
        "vlan 10,20\ninterface Ethernet1/1\n"
        " switchport mode trunk\n switchport trunk allowed vlan 10\n"
        " switchport trunk allowed vlan add 20\n spanning-tree guard root\n"
        " channel-group 7 mode on\n"
        "interface Vlan20\n ip address dhcp\n ip ospf 1 area 0.0.0.1\n"
        "ipv6 route 2001:db8::/32 2001:db8:1::1\nip route vrf MGMT 0.0.0.0 0.0.0.0 10.0.0.1\n"
        "switch 1 provision c9300-48p\nrouter bgp 65001\n neighbor 192.0.2.9 remote-as 65002\n"
        " neighbor 192.0.2.9 shutdown\n",
        "cisco",
    )
    assert (config.mst_region, config.mst_revision, config.stp_priority) == ("CAMPUS", 2, 4096)
    assert config.mst_instances[1] == (list(range(10, 21)), 8192)
    assert config.trunks["Port-channel7"].protocol == "static"
    assert config.interfaces["Ethernet1/1"].root_guard and config.vlans[20].dhcp
    assert config.vlans[20].ospf_area == "0.0.0.1" and config.routes[0].prefix == "2001:db8::/32"
    assert len(config.routes) == 1 and config.members[1].model == "c9300-48p"
    assert not config.neighbors["192.0.2.9"].enabled


def test_comware_region_and_huawei_pool() -> None:
    config = platforms.parse(
        " sysname H\n#\nstp region-configuration\n region-name CAMPUS\n revision-level 4\n"
        " instance 1 vlan 10 to 12\n#\nip pool LAN\n"
        " gateway-list 10.9.0.1\n network 10.9.0.0 mask 24\n"
        " static-bind ip-address 10.9.0.9 mac-address 0011-2233-4477\n#\n"
        "interface GigabitEthernet0/0/2\n port link-type access\n port default vlan 30\n"
        " stp root-protection\n stp bpdu-protection\n#\n"
        " ip route-static 10.8.0.0 16 10.9.0.254 preference 70\n#\n"
        "interface Vlanif10\n#\n",
        "comware",
    )
    assert (config.mst_region, config.mst_revision) == ("CAMPUS", 4)
    assert config.mst_instances[1][0] == [10, 11, 12]
    pool = config.pools["LAN"]
    assert (pool.network, pool.start, pool.reservations[0][0]) == (
        "10.9.0.0/24",
        "10.9.0.1",
        "00:11:22:33:44:77",
    )
    assert config.vlans[30].untagged == ["GigabitEthernet0/0/2"]
    assert config.routes[0].distance == 70 and config.interfaces["GigabitEthernet0/0/2"].bpdu_guard


def test_apply_leaves_scopes_of_other_servers(make_tree, make_type, super_admin, system) -> None:
    from patchmanager.dcim import devices
    from patchmanager.ipam.models import DHCPScope, Prefix

    tree = make_tree()
    with system():
        model = make_type("C9300-8", ports=[{"name_pattern": "GigabitEthernet1/0/{n}", "count": 8}])
        device = devices.create(
            tree["request"], super_admin, device_type=model, room=tree["room"], name="A"
        )
        other = devices.create(
            tree["request"], super_admin, device_type=model, room=tree["room"], name="B"
        )
        prefix = Prefix.objects.create(tenant=tree["tenant"], prefix="10.10.0.0/24")
        DHCPScope.objects.create(
            tenant=tree["tenant"],
            name="USERS",
            prefix=prefix,
            server=other,
            start="10.10.0.50",
            end="10.10.0.60",
        )
        result = switchconfig.apply(
            tree["request"],
            super_admin,
            device,
            platforms.parse(_read("cisco_ios.txt")),
            commit=True,
        )
        assert any("another server" in s for s in result.skipped)
        scope = DHCPScope.objects.get()
        assert scope.server_id == other.pk and str(scope.start).split("/")[0] == "10.10.0.50"


def test_junos_brace_lists_cannot_expand_without_limit() -> None:
    from patchmanager.networks.platforms import junos

    deep = "".join(f"{'b' * 1900}{i} {{\n" for i in range(30))
    values = "a [ " + " ".join(["v"] * 900) + " ];\n"
    with pytest.raises(common.TooLargeError):
        junos.to_set((deep + values * 30).split("\n"))


def test_repeated_allowed_vlan_adds_stay_within_the_budget() -> None:
    from patchmanager.networks.platforms import cisco

    text = "interface Gi1/0/1\n switchport mode trunk\n"
    port = cisco.parse(text + " switchport trunk allowed vlan add 1-4094\n" * 200)
    assert len(port.vlans) == 4094 and port.vlans[4094].tagged == ["Gi1/0/1"]
    with pytest.raises(common.TooLargeError):  # the expansion work stays bounded
        cisco.parse(text + " switchport trunk allowed vlan add 1-4094\n" * 2000)


def test_allowed_vlan_lists_are_charged_once_per_membership() -> None:
    from patchmanager.networks.platforms import arubacx, cisco

    aruba = "".join(
        f"interface 1/1/{n}\n vlan trunk native 1\n vlan trunk allowed 2-401\n"
        for n in range(1, 49)
    )
    assert len(arubacx.parse(aruba).vlans[401].tagged) == 48
    ios = "".join(
        f"interface Gi1/0/{n}\n switchport mode trunk\n"
        + "".join(f" switchport trunk allowed vlan add {v}\n" for v in range(2, 252))
        for n in range(1, 49)
    )
    assert len(cisco.parse(ios).vlans[251].tagged) == 48


def test_cisco_excluded_ranges_routes_and_vrf() -> None:
    from patchmanager.networks.platforms import cisco

    many = "".join(f"ip dhcp excluded-address 10.0.{i // 250}.{i % 250 + 1}\n" for i in range(3000))
    config = cisco.parse(
        many
        + "ip dhcp excluded-address 10.0.0.240 10.0.0.254\n"
        + "ip dhcp excluded-address vrf GUEST 10.9.0.1 10.9.0.9\n"
        + "ip dhcp pool A\n network 10.0.0.0 255.255.0.0\n"
        + "ip route 0.0.0.0/0 10.0.0.1\nip route 10.2.0.0 255.255.0.0 10.1.1.2 5\n"
        + "ip route 172.16.0.0/16 10.1.1.3 250\n"
    )
    pool = config.pools["A"]
    assert (pool.start, pool.end) == ("10.0.0.255", "10.0.255.254")  # 10.0.0.1-254 excluded
    routes = [(r.prefix, r.next_hop, r.distance) for r in config.routes]
    assert routes == [
        ("0.0.0.0/0", "10.0.0.1", None),
        ("10.2.0.0/16", "10.1.1.2", 5),
        ("172.16.0.0/16", "10.1.1.3", 250),
    ]
    assert not config.warnings


def test_junos_group_peer_as_and_default_access_mode() -> None:
    from patchmanager.networks.platforms import junos

    config = junos.parse(
        "set protocols bgp group EXT peer-as 65001\n"
        "set protocols bgp group EXT neighbor 192.0.2.1\n"
        "set protocols bgp group EXT neighbor 192.0.2.2 peer-as 65002\n"
        "set vlans USERS vlan-id 100\n"
        "set interfaces ge-0/0/1 unit 0 family ethernet-switching vlan members USERS\n"
    )
    assert {a: n.remote_asn for a, n in config.neighbors.items()} == {
        "192.0.2.1": 65001,
        "192.0.2.2": 65002,
    }
    assert config.vlans[100].untagged == ["ge-0/0/1"]


def test_fortios_reads_values_spanning_lines() -> None:
    from patchmanager.networks.platforms import fortios

    config = fortios.parse(
        "#config-version=FGT60F-7.2.5-FW-build1517:opmode=0:vdom=0\n"
        'config system global\n    set hostname "fw1"\nend\n'
        'config vpn certificate local\n    edit "Factory"\n'
        '        set certificate "-----BEGIN CERTIFICATE-----\nMIIDdzCCAl+gAwIB\n'
        '-----END CERTIFICATE-----"\n    next\nend\n'
        'config system interface\n    edit "port1"\n        set ip 192.0.2.1 255.255.255.0\n'
        "    next\nend\n"
    )
    assert config.hostname == "fw1" and "port1" in config.interfaces
    with pytest.raises(platforms.ParseError, match="unbalanced quotes"):
        fortios.parse('config system global\n set hostname "x\n' + "a\n" * 10)


def test_interface_routes_keep_their_interface_and_are_not_exported_as_discards() -> None:
    from patchmanager.networks import aruba
    from patchmanager.networks.platforms import cisco

    config = cisco.parse(
        "ip route 10.20.0.0 255.255.0.0 Vlan20\n"
        "ip route 10.30.0.0 255.255.0.0 GigabitEthernet1/0/1 10.0.0.2 5\n"
        "ip route 10.40.0.0 255.255.0.0 Null0 250\n"
    )
    routes = [(r.prefix, r.next_hop, r.distance, r.interface) for r in config.routes]
    assert routes == [
        ("10.20.0.0/16", None, None, "Vlan20"),
        ("10.30.0.0/16", "10.0.0.2", 5, "GigabitEthernet1/0/1"),
        ("10.40.0.0/16", None, 250, ""),
    ]
    rendered = aruba.render(config)
    assert "ip route 10.20.0.0/16" not in rendered
    assert "ip route 10.40.0.0/16 blackhole distance 250" in rendered


def test_junos_trunks_from_groups_and_group_neighbors() -> None:
    from patchmanager.networks.platforms import junos

    config = junos.parse(
        "set vlans v10 vlan-id 10\nset vlans v20 vlan-id 20\n"
        "set interfaces xe-0/0/1 apply-groups TRUNKS\n"
        "set interfaces xe-0/0/1 unit 0 family ethernet-switching vlan members v10\n"
        "set interfaces xe-0/0/1 unit 0 family ethernet-switching vlan members v20\n"
        "set interfaces xe-0/0/2 unit 0 family ethernet-switching vlan members v10\n"
        "set protocols bgp group EXT peer-as 64500\n"
        'set protocols bgp group EXT neighbor 192.0.2.1 description "Transit A"\n'
        "set protocols bgp group EMPTY peer-as\n"
        "set protocols bgp group EXT neighbor\n"
    )
    assert config.interfaces["xe-0/0/1"].mode != "access"  # mode set by the group
    assert config.interfaces["xe-0/0/2"].access_vlan == 10
    neighbor = config.neighbors["192.0.2.1"]
    assert (neighbor.remote_asn, neighbor.description) == (64500, "Transit A")


def test_bgp_peer_groups_do_not_drop_the_other_peers() -> None:
    from patchmanager.networks.platforms import arubacx, comware

    config = comware.parse(
        "bgp 65050\n group EXT external\n peer EXT as-number 65000\n"
        " peer 192.0.2.9 as-number 65009\n#\n"
    )
    assert list(config.neighbors) == ["192.0.2.9"]
    aruba = arubacx.parse(
        "!Version ArubaOS-CX\nrouter bgp 65050\n neighbor SPINE remote-as 65000\n"
        " neighbor 192.0.2.10 remote-as 65010\n"
    )
    assert list(aruba.neighbors) == ["192.0.2.10"]


def test_mst_instances_are_charged_and_bounded() -> None:
    from patchmanager.networks.platforms import comware

    text = "stp region-configuration\n" + " instance 1 vlan 1 to 4094\n" * 200
    with pytest.raises(common.TooLargeError):
        comware.parse(text)
    config = common.Config(vendor="test")
    with pytest.raises(common.ParseError):
        config.add_mst_vlans(5000, [1])
    for number in range(common.MAX_MST):
        config.add_mst_vlans(number, [1])
    with pytest.raises(common.ParseError, match="MST instances"):  # stored lists stay bounded
        config.add_mst_vlans(4000, [1])


def test_routeros_bridge_vlans_without_ports_count() -> None:
    from patchmanager.networks.platforms import routeros

    text = "/interface bridge vlan\n" + "add bridge=bridge1 vlan-ids=1-4094\n" * 10
    with pytest.raises(common.TooLargeError):
        routeros.parse(text)
    config = routeros.parse(
        "/interface bridge vlan\nadd bridge=bridge1 tagged=ether2 vlan-ids=10,10,10\n"
    )
    assert config.vlans[10].tagged == ["ether2"]


@pytest.mark.parametrize(
    ("vendor", "text"),
    [
        ("cisco", "vlan ²\n"),
        ("comware", "ip route-static 10.8.0.0 16 10.9.0.254 description lower preference\n"),
    ],
)
def test_odd_digits_are_warnings_not_errors(vendor, text) -> None:
    from patchmanager.networks import platforms

    platforms.parse(text, vendor)  # no exception other than ParseError escapes
