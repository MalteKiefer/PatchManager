"""Cisco IOS, IOS-XE and NX-OS running configurations (also close dialects like Arista EOS)."""

import ipaddress
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
    Vlan,
    _ip,
    _unquote,
    address_of,
    blocks,
    finish,
    keywords,
    lines_of,
    network_of,
    vid_list,
    without_secrets,
)

_IGNORED = keywords(
    "version service boot logging ntp clock aaa line banner access-list control-plane"
    " end archive license redundancy diagnostic memory errdisable ip no feature lldp"
    " cdp mac-address-table mac crypto vtp system class-map policy-map snmp-server"
)


def detect(text: str) -> int:
    """How sure the text is a Cisco style configuration (0 to 100)."""
    score = 0
    if re.search(r"^(Building configuration|Current configuration)", text, re.M):
        score += 60
    if re.search(r"^version \d+\.\d+", text, re.M):
        score += 30
    if re.search(r"^ switchport ", text, re.M):
        score += 20
    if re.search(r"^!\s*$", text, re.M):
        score += 10
    if re.search(r"^feature ", text, re.M):  # NX-OS
        score += 20
    return min(score, 100)


def parse(text: str) -> Config:
    """Read a Cisco style running configuration."""
    config = Config(vendor="cisco")
    excluded: list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]] = []
    for head, body, number in blocks(lines_of(text)):
        if head.startswith(("Building configuration", "Current configuration")):
            continue
        if SECRET.search(head):
            config.secrets_skipped += 1
            continue
        try:
            _command(config, head, without_secrets(config, body), excluded)
        except ParseError as exc:
            config.warnings.append(f"line {number}: {exc}")
    _pool_ranges(config, excluded)
    return finish(config)


def _command(
    config: Config,
    head: str,
    body: list[str],
    excluded: list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]],
) -> None:
    words = head.split()
    key = words[0].lower()
    if key == "hostname" and len(words) == 2:
        config.hostname = _unquote(words[1])[:100]
    elif words[:2] == ["vlan", "configuration"]:
        return
    elif key == "vlan" and len(words) == 2:
        for vid in vid_list(words[1]):
            vlan = config.vlans.setdefault(vid, _new_vlan(vid))
            for line in body:
                if line.startswith("name "):
                    vlan.name = _unquote(line[5:])[:64]
    elif key == "interface" and len(words) >= 2:
        _interface(config, "".join(words[1:]), body)
    elif words[:2] == ["ip", "route"] and len(words) >= 5 and words[2] != "vrf":
        prefix = network_of(words[2:4])
        hop = words[4]
        next_hop = _ip(hop) if hop[:1].isdigit() or ":" in hop else None
        distance = (
            int(words[5])
            if len(words) > 5 and words[5].isdigit() and int(words[5]) <= 255
            else None
        )
        config.routes.append(Route(prefix, next_hop, distance))
    elif words[:2] == ["ipv6", "route"] and len(words) >= 4 and words[2] != "vrf":
        config.routes.append(
            Route(network_of([words[2]]), _ip(words[3]) if ":" in words[3] else None)
        )
    elif words[:2] == ["ip", "routing"] or words[:2] == ["ipv6", "unicast-routing"]:
        config.ip_routing = True
    elif words[:2] in (["ip", "name-server"], ["ip", "domain"]) or words[:2] == [
        "ip",
        "domain-name",
    ]:
        _dns(config, words)
    elif words[:3] == ["ip", "dhcp", "excluded-address"] and len(words) >= 4:
        first = ipaddress.IPv4Address(words[3])
        last = ipaddress.IPv4Address(words[4]) if len(words) > 4 else first
        excluded.append((first, last))
    elif words[:3] == ["ip", "dhcp", "pool"] and len(words) == 4:
        _pool(config, words[3], body)
    elif key == "spanning-tree":
        _spanning_tree(config, words[1:], body)
    elif key == "router" and words[1:2] == ["ospf"]:
        for line in body:
            parts = line.split()
            if parts[:1] == ["router-id"] and len(parts) == 2:
                config.ospf_router_id = _ip(parts[1])
            elif parts[:1] == ["passive-interface"] and len(parts) == 2:
                config.interface(parts[1]).mode = config.interface(parts[1]).mode
                _passive(config, parts[1])
    elif key == "router" and words[1:2] == ["bgp"] and len(words) == 3 and words[2].isdigit():
        config.bgp_asn = int(words[2])
        _bgp(config, body)
    elif key == "switch" and len(words) >= 3 and words[1].isdigit() and words[2] == "provision":
        config.stack_kind = "stack"
        config.members[int(words[1])] = Member(
            int(words[1]), model=words[3][:40] if len(words) > 3 else ""
        )
    elif key not in _IGNORED:
        config.unknown.append(head[:120])


def _new_vlan(vid: int) -> Vlan:
    return Vlan(vid)


def _dns(config: Config, words: list[str]) -> None:
    if words[1] == "name-server":
        servers = [w for w in words[2:] if w != "vrf"]
        config.dns_servers += [_ip(w) for w in servers if w[:1].isdigit() or ":" in w]
    elif len(words) >= 3:  # ip domain name x / ip domain-name x
        config.domain = words[-1][:253]


def _vlan_of_interface(name: str) -> int | None:
    match = re.fullmatch(r"(?i)vlan(\d{1,4})", name)
    return int(match.group(1)) if match else None


def _interface(config: Config, name: str, body: list[str]) -> None:
    vid = _vlan_of_interface(name)
    if vid is not None:  # a VLAN interface (SVI)
        vlan = config.vlans.setdefault(vid, _new_vlan(vid))
        for line in body:
            parts = line.split()
            if parts[:2] == ["ip", "address"] and len(parts) >= 3:
                if parts[2] == "dhcp":
                    vlan.dhcp = True
                else:
                    vlan.addresses.append(address_of(parts[2:4]))
            elif parts[:2] == ["ipv6", "address"] and len(parts) == 3 and "/" in parts[2]:
                vlan.addresses.append(address_of(parts[2:3]))
            elif parts[:2] == ["ip", "helper-address"] and len(parts) >= 3:
                vlan.helpers.append(_ip(parts[-1]))
            elif parts[:2] == ["ip", "ospf"] and "area" in parts:
                area = parts[parts.index("area") + 1]
                if OSPF_AREA.match(area):
                    vlan.ospf_area = area
        return
    port = config.interface(name)
    for line in body:
        parts = line.split()
        if parts[0] == "description" and len(parts) > 1:
            port.label = _unquote(line.split(None, 1)[1])[:100]
        elif parts == ["shutdown"]:
            port.enabled = False
        elif parts == ["no", "shutdown"]:
            port.enabled = True
        elif parts[:2] == ["switchport", "mode"] and len(parts) >= 3:
            port.mode = "trunk" if parts[2] == "trunk" else "access"
        elif (
            parts[:3] == ["switchport", "access", "vlan"] and len(parts) == 4 and parts[3].isdigit()
        ):
            port.access_vlan = int(parts[3])
            port.mode = port.mode or "access"
        elif parts[:4] == ["switchport", "trunk", "native", "vlan"] and len(parts) == 5:
            port.native_vlan = int(parts[4]) if parts[4].isdigit() else None
        elif parts[:4] == ["switchport", "trunk", "allowed", "vlan"] and len(parts) >= 5:
            spec = " ".join(parts[4:])
            if parts[4] == "add":
                port.allowed = (port.allowed or []) + vid_list(" ".join(parts[5:]))
            elif parts[4] == "none":
                port.allowed = []
            else:
                port.allowed = vid_list(spec)
        elif parts == ["no", "switchport"]:
            port.mode = "routed"
        elif parts[:1] == ["channel-group"] and len(parts) >= 2 and parts[1].isdigit():
            port.lag = f"Port-channel{parts[1]}"
            mode = parts[3] if len(parts) > 3 else "on"
            port.lag_protocol = "lacp" if mode in ("active", "passive") else "static"
        elif parts[:2] == ["ip", "address"] and len(parts) >= 3 and parts[2] != "dhcp":
            port.addresses.append(address_of(parts[2:4]))
        elif parts[:2] == ["ip", "helper-address"] and len(parts) >= 3:
            port.helpers.append(_ip(parts[-1]))
        elif parts[:2] == ["spanning-tree", "portfast"] and "disable" not in parts:
            port.edge = True
        elif parts[:2] == ["spanning-tree", "bpduguard"] and "enable" in parts:
            port.bpdu_guard = True
        elif parts[:3] == ["spanning-tree", "guard", "root"]:
            port.root_guard = True


def _passive(config: Config, name: str) -> None:
    vid = _vlan_of_interface(name)
    if vid is not None:
        config.vlans.setdefault(vid, _new_vlan(vid)).ospf_passive = True


def _spanning_tree(config: Config, words: list[str], body: list[str]) -> None:
    if words[:1] == ["mode"] and len(words) == 2:
        config.stp_mode = {"rapid-pvst": "rpvst", "mst": "mstp", "pvst": "pvst"}.get(words[1], "")
    elif words[:1] == ["vlan"] and "priority" in words:
        value = words[words.index("priority") + 1]
        if value.isdigit():
            config.stp_priority = int(value)
    elif words[:2] == ["mst", "configuration"]:
        for line in body:
            parts = line.split()
            if parts[:1] == ["name"] and len(parts) == 2:
                config.mst_region = parts[1][:32]
            elif parts[:1] == ["revision"] and len(parts) == 2 and parts[1].isdigit():
                config.mst_revision = int(parts[1])
            elif parts[:1] == ["instance"] and len(parts) >= 4 and parts[1].isdigit():
                vlans, priority = config.mst_instances.get(int(parts[1]), ([], None))
                config.mst_instances[int(parts[1])] = (
                    sorted(set(vlans) | set(vid_list(parts[3]))),
                    priority,
                )
    elif words[:1] == ["mst"] and "priority" in words and words[1].isdigit():
        number, value = int(words[1]), words[words.index("priority") + 1]
        vlans, _priority = config.mst_instances.get(number, ([], None))
        if number == 0 and value.isdigit():
            config.stp_priority = int(value)
        elif value.isdigit():
            config.mst_instances[number] = (vlans, int(value))


def _bgp(config: Config, body: list[str]) -> None:
    for line in body:
        parts = line.split()
        if parts[:2] == ["bgp", "router-id"] and len(parts) == 3:
            config.bgp_router_id = _ip(parts[2])
        elif (
            parts[:1] == ["neighbor"]
            and len(parts) >= 3
            and (parts[1][:1].isdigit() or ":" in parts[1])
        ):
            address = _ip(parts[1])
            neighbor = config.neighbors.get(address)
            if parts[2] == "remote-as" and len(parts) == 4 and parts[3].isdigit():
                if neighbor is None:
                    config.neighbors[address] = Neighbor(address, int(parts[3]))
                else:
                    neighbor.remote_asn = int(parts[3])
            elif neighbor is not None and parts[2] == "description":
                neighbor.description = _unquote(line.split("description", 1)[1])[:200]
            elif neighbor is not None and parts[2] == "shutdown":
                neighbor.enabled = False


def _pool(config: Config, name: str, body: list[str]) -> None:
    pool = config.pools.setdefault(name[:100], Pool(name[:100]))
    host = ""
    mac = ""
    for line in body:
        parts = line.split()
        if parts[:1] == ["network"] and len(parts) >= 2:
            pool.network = network_of(parts[1:3])
        elif parts[:1] == ["host"] and len(parts) >= 2:
            host = parts[1]
        elif parts[:1] in (["hardware-address"], ["client-identifier"]) and len(parts) >= 2:
            mac = _mac(parts[1])
        elif parts[:1] == ["default-router"] and len(parts) >= 2:
            pool.gateway = _ip(parts[1])
        elif parts[:1] == ["dns-server"]:
            pool.dns += [_ip(p) for p in parts[1:]]
        elif parts[:1] == ["domain-name"] and len(parts) == 2:
            pool.domain = parts[1][:253]
        elif parts[:1] == ["lease"] and len(parts) >= 2:
            if parts[1] == "infinite":
                pool.lease_seconds = None
            else:
                days, hours, minutes = ([*(int(p) for p in parts[1:4] if p.isdigit()), 0, 0, 0])[:3]
                pool.lease_seconds = max(60, days * 86400 + hours * 3600 + minutes * 60)
    if host and mac:  # a manual binding: one host pool
        pool.reservations.append((mac, _ip(host), name[:100]))


def _mac(value: str) -> str:
    digits = re.sub(r"[^0-9a-f]", "", value.lower())
    digits = digits[-12:]  # client identifiers start with 01 (Ethernet)
    if len(digits) != 12:
        raise ParseError(f"cannot read the MAC address {value[:30]}")
    return ":".join(digits[i : i + 2] for i in range(0, 12, 2))


def _pool_ranges(
    config: Config, excluded: list[tuple[ipaddress.IPv4Address, ipaddress.IPv4Address]]
) -> None:
    """Cisco pools hand out the whole network except excluded addresses: find the range.

    Manual bindings are pools of their own; they become reservations of the pool whose
    network holds their address.
    """
    for name, pool in list(config.pools.items()):
        if pool.network or not pool.reservations:
            continue
        address = ipaddress.ip_address(pool.reservations[0][1])
        home = next(
            (
                p
                for p in config.pools.values()
                if p.network and address in ipaddress.ip_network(p.network)
            ),
            None,
        )
        if home is not None:
            home.reservations += pool.reservations
            del config.pools[name]
    for pool in config.pools.values():
        if not pool.network or ":" in pool.network:
            continue
        network = ipaddress.IPv4Network(pool.network)
        if network.num_addresses < 4:
            continue
        first, last = network.network_address + 1, network.broadcast_address - 1
        changed = True
        while changed:
            changed = False
            for low, high in excluded:
                if low <= first <= high:
                    first, changed = high + 1, True
                if low <= last <= high:
                    last, changed = low - 1, True
        if first <= last:
            pool.start, pool.end = str(first), str(last)
