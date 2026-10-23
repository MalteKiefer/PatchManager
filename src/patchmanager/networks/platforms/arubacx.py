"""HPE Aruba Networking AOS-CX (ArubaOS-CX) running configurations."""

import re

from patchmanager.networks.platforms.common import (
    OSPF_AREA,
    SECRET,
    Config,
    Member,
    Neighbor,
    ParseError,
    Pool,
    Route,
    TooLargeError,
    Vlan,
    _ip,
    _unquote,
    address_of,
    blocks,
    declare_vlans,
    finish,
    is_address,
    keywords,
    lines_of,
    network_of,
    vid_list,
    without_secrets,
)

_IGNORED = keywords(
    "led ssh https-server ntp logging snmp-server snmpv3 user aaa radius-server tacacs-server"
    " banner clock lldp mirror qos profile apply class policy access-list no vrf system"
    " console-port usb loop-protect port-access dhcpv4-snooping nd-snooping"
)


def detect(text: str) -> int:
    """How sure the text is an AOS-CX configuration (0 to 100)."""
    score = 0
    if re.search(r"^!Version ArubaOS-CX", text, re.M):
        score += 90
    if re.search(r"^interface \d+/\d+/\d+", text, re.M):
        score += 30
    if re.search(r"^\s+vlan (access|trunk) ", text, re.M):
        score += 30
    return min(score, 100)


def parse(text: str) -> Config:
    """Read an AOS-CX running configuration."""
    config = Config(vendor="arubacx")
    for raw in text.split("\n", 3)[:3]:
        if raw.startswith("!Version"):
            config.platform = raw[1:].strip()[:200]
    for head, body, number in blocks(lines_of(text)):
        if SECRET.search(head):
            config.secrets_skipped += 1
            continue
        try:
            _command(config, head, without_secrets(config, body))
        except TooLargeError:
            raise  # the whole text is refused, not one line
        except ParseError as exc:
            config.warnings.append(f"line {number}: {exc}")
    return finish(config)


def _command(config: Config, head: str, body: list[str]) -> None:
    words = head.split()
    key = words[0]
    if key == "hostname" and len(words) == 2:
        config.hostname = _unquote(words[1])[:100]
    elif key == "vlan" and len(words) == 2:
        declare_vlans(config, words[1], body)
    elif key == "interface" and words[1:2] == ["vlan"] and len(words) == 3 and words[2].isdecimal():
        _svi(config, int(words[2]), body)
    elif key == "interface" and re.fullmatch(r"vlan\d+", words[1] if len(words) > 1 else ""):
        _svi(config, int(words[1][4:]), body)
    elif key == "interface" and words[1:2] == ["lag"] and len(words) == 3:
        _port(config, f"lag{words[2]}", body)
    elif key == "interface" and len(words) == 2:
        _port(config, words[1], body)
    elif words[:2] == ["ip", "route"] and len(words) >= 4:
        hop = words[3]
        distance = (
            int(words[-1])
            if len(words) > 4 and words[-2] == "distance" and words[-1].isdecimal()
            else None
        )
        config.routes.append(
            Route(
                network_of([words[2]]),
                _ip(hop) if hop[:1].isdecimal() or ":" in hop else None,
                distance,
            )
        )
    elif words[:2] == ["ip", "dns"] and len(words) >= 4 and words[2] == "server-address":
        config.dns_servers.append(_ip(words[3]))
    elif words[:3] == ["ip", "dns", "domain-name"] and len(words) == 4:
        config.domain = words[3][:253]
    elif key == "spanning-tree":
        _spanning_tree(config, words[1:], body)
    elif key == "router" and words[1:2] == ["ospf"]:
        for line in body:
            parts = line.split()
            if parts[:1] == ["router-id"] and len(parts) == 2:
                config.ospf_router_id = _ip(parts[1])
    elif key == "router" and words[1:2] == ["bgp"] and len(words) == 3 and words[2].isdecimal():
        config.bgp_asn = int(words[2])
        for line in body:
            parts = line.split()
            if parts[:2] == ["bgp", "router-id"] and len(parts) == 3:
                config.bgp_router_id = _ip(parts[2])
            elif (
                parts[:1] == ["neighbor"]
                and len(parts) >= 4
                and parts[2] == "remote-as"
                and parts[3].isdecimal()
                and is_address(parts[1])
            ):
                config.neighbors[_ip(parts[1])] = Neighbor(_ip(parts[1]), int(parts[3]))
            elif parts[:1] == ["neighbor"] and len(parts) >= 3 and parts[2] == "description":
                neighbor = config.neighbors.get(parts[1])
                if neighbor is not None:
                    neighbor.description = _unquote(line.split("description", 1)[1])[:200]
            elif parts[:1] == ["neighbor"] and len(parts) == 3 and parts[2] == "shutdown":
                neighbor = config.neighbors.get(parts[1])
                if neighbor is not None:
                    neighbor.enabled = False
    elif key == "dhcp-server" and words[1:2] == ["vrf"]:
        _dhcp(config, body)
    elif key == "vsf" and words[1:2] == ["member"] and len(words) == 3 and words[2].isdecimal():
        config.stack_kind = "vsf"
        member = config.members.setdefault(int(words[2]), Member(int(words[2])))
        for line in body:
            parts = line.split()
            if parts[:1] == ["type"] and len(parts) >= 2:
                member.model = parts[1][:40]
            elif parts[:1] == ["priority"] and len(parts) == 2 and parts[1].isdecimal():
                member.priority = int(parts[1])
    elif key == "ip" and words[1:2] == ["routing"]:
        config.ip_routing = True
    elif key not in _IGNORED and not key.startswith("!"):
        config.unknown.append(head[:120])


def _svi(config: Config, vid: int, body: list[str]) -> None:
    vlan = config.vlans.setdefault(vid, Vlan(vid))
    config.ip_routing = True  # AOS-CX routes between VLAN interfaces by default
    for line in body:
        parts = line.split()
        if parts[:2] in (["ip", "address"], ["ipv6", "address"]) and len(parts) >= 3:
            if parts[2] == "dhcp":
                vlan.dhcp = True
            else:
                vlan.addresses.append(address_of(parts[2:4]))
        elif parts[:2] == ["ip", "helper-address"] and len(parts) >= 3:
            vlan.helpers.append(_ip(parts[2]))
        elif parts[:2] == ["ip", "ospf"] and "area" in parts:
            area = parts[parts.index("area") + 1]
            if OSPF_AREA.match(area):
                vlan.ospf_area = area
        elif parts[:3] == ["ip", "ospf", "passive"]:
            vlan.ospf_passive = True


def _port(config: Config, name: str, body: list[str]) -> None:
    port = config.interface(name)
    port.enabled = False  # AOS-CX ports are shut down unless "no shutdown"
    for line in body:
        parts = line.split()
        if parts[0] == "description" and len(parts) > 1:
            port.label = _unquote(line.split(None, 1)[1])[:100]
        elif parts == ["no", "shutdown"]:
            port.enabled = True
        elif parts == ["shutdown"]:
            port.enabled = False
        elif parts[:2] == ["vlan", "access"] and len(parts) == 3 and parts[2].isdecimal():
            port.mode, port.access_vlan = "access", int(parts[2])
        elif parts[:3] == ["vlan", "trunk", "native"] and len(parts) >= 4 and parts[3].isdecimal():
            port.mode, port.native_vlan = "trunk", int(parts[3])
        elif parts[:3] == ["vlan", "trunk", "allowed"] and len(parts) >= 4:
            port.mode = "trunk"
            if parts[3] == "all":
                port.allowed = None
            else:
                config.extend_allowed(port, vid_list(parts[3]))
        elif parts[:1] == ["lag"] and len(parts) == 2 and parts[1].isdecimal():
            port.lag = f"lag{parts[1]}"
        elif parts[:2] == ["lacp", "mode"]:
            port.lag_protocol = "lacp"
        elif parts[:1] == ["routing"]:
            port.mode = "routed"
        elif parts[:2] == ["ip", "address"] and len(parts) >= 3:
            port.addresses.append(address_of(parts[2:4]))
        elif parts[:2] == ["spanning-tree", "port-type"] and parts[2:3] == ["admin-edge"]:
            port.edge = True
        elif parts[:2] == ["spanning-tree", "bpdu-guard"]:
            port.bpdu_guard = True
        elif parts[:2] == ["spanning-tree", "root-guard"]:
            port.root_guard = True


def _spanning_tree(config: Config, words: list[str], body: list[str]) -> None:
    if not words:
        config.stp_mode = config.stp_mode or "mstp"
    elif words[:1] == ["mode"] and len(words) == 2:
        config.stp_mode = {"rpvst": "rpvst", "mstp": "mstp"}.get(words[1], "")
    elif words[:1] == ["priority"] and len(words) == 2 and words[1].isdecimal():
        config.stp_priority = int(words[1]) * 4096
    elif words[:1] == ["config-name"] and len(words) == 2:
        config.mst_region = _unquote(words[1])[:32]
    elif words[:1] == ["config-revision"] and len(words) == 2 and words[1].isdecimal():
        config.mst_revision = int(words[1])
    elif words[:1] == ["instance"] and len(words) >= 4 and words[1].isdecimal():
        if words[2] == "vlan":
            config.add_mst_vlans(int(words[1]), vid_list(words[3]))
        elif words[2] == "priority" and words[3].isdecimal():
            vlans, _priority = config.mst_instances.get(int(words[1]), ([], None))
            config.mst_instances[int(words[1])] = (vlans, int(words[3]) * 4096)


def _dhcp(config: Config, body: list[str]) -> None:
    pool = None
    for line in body:
        parts = line.split()
        if parts[:1] == ["pool"] and len(parts) == 2:
            pool = config.pools.setdefault(parts[1][:100], Pool(parts[1][:100]))
        elif pool is None:
            continue
        elif parts[:1] == ["range"] and len(parts) >= 3:
            pool.start, pool.end = _ip(parts[1]), _ip(parts[2])
            if "prefix-len" in parts:
                length = parts[parts.index("prefix-len") + 1]
                pool.network = network_of([f"{pool.start}/{length}"])
        elif parts[:1] == ["default-router"] and len(parts) >= 2:
            pool.gateway = _ip(parts[1])
        elif parts[:1] == ["dns-server"]:
            pool.dns += [_ip(p) for p in parts[1:]]
        elif parts[:1] == ["domain-name"] and len(parts) == 2:
            pool.domain = parts[1][:253]
        elif parts[:1] == ["lease"] and len(parts) >= 2:
            hours, _sep, rest = parts[1].partition(":")
            if hours.isdecimal():
                pool.lease_seconds = max(
                    60,
                    int(hours) * 3600
                    + (int(rest.split(":")[0]) * 60 if rest[:2].isdecimal() else 0),
                )
        elif parts[:1] == ["static-bind"] and "ip" in parts and "mac" in parts:
            pool.reservations.append(
                (parts[parts.index("mac") + 1].lower(), _ip(parts[parts.index("ip") + 1]), "")
            )
