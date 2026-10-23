"""HPE Comware / H3C and Huawei VRP configurations (``display current-configuration``)."""

import ipaddress
import re

from patchmanager.networks.platforms.common import (
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
    "version clock info-center snmp-agent ntp-service ssh telnet user-interface line lldp"
    " local-user aaa radius hwtacacs domain return header undo password-recovery stelnet"
    " sftp ftp http https public-key super acl traffic qos time-range"
)


def detect(text: str) -> int:
    """How sure the text is a Comware or VRP configuration (0 to 100)."""
    score = 0
    if re.search(r"^ ?sysname \S+", text, re.M):
        score += 60
    if re.search(r"^ port link-type (access|trunk|hybrid)", text, re.M):
        score += 30
    if re.search(r"^#\s*$", text, re.M):
        score += 10
    return min(score, 100)


def parse(text: str) -> Config:
    """Read a Comware or VRP configuration."""
    vrp = bool(re.search(r"^interface (Vlanif|Eth-Trunk)", text, re.M))
    config = Config(vendor="huawei" if vrp else "comware")
    for head, body, number in _blocks(lines_of(text)):
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
    if key == "sysname" and len(words) >= 2:
        config.hostname = _unquote(head.split(None, 1)[1])[:100]
    elif key == "vlan" and len(words) >= 2 and words[1] == "batch":
        declare_vlans(config, " ".join(words[2:]), [])
    elif key == "vlan" and len(words) >= 2:
        declare_vlans(config, " ".join(words[1:]), body, ("name", "description"))
    elif key == "interface" and len(words) >= 2:
        _interface(config, "".join(words[1:]), body)
    elif key == "ip" and words[1:2] == ["route-static"] and len(words) >= 5:
        config.routes.append(_route(words[2:]))
    elif key == "dns" and words[1:2] == ["server"] and len(words) >= 3:
        config.dns_servers.append(_ip(words[2]))
    elif key == "dns" and words[1:2] == ["domain"] and len(words) >= 3:
        config.domain = words[2][:253]
    elif key == "stp" and len(words) >= 2:
        _stp(config, words[1:], body)
    elif key == "ospf":
        if "router-id" in words:
            config.ospf_router_id = _ip(words[words.index("router-id") + 1])
        for line in body:
            parts = line.split()
            if parts[:1] == ["router-id"] and len(parts) == 2:
                config.ospf_router_id = _ip(parts[1])
    elif key == "router" and words[1:2] == ["id"] and len(words) == 3:
        config.ospf_router_id = config.ospf_router_id or _ip(words[2])
    elif key == "bgp" and len(words) >= 2 and words[1].isdecimal():
        config.bgp_asn = int(words[1])
        _bgp(config, body)
    elif key == "dhcp" and words[1:3] == ["server", "ip-pool"] and len(words) >= 4:
        _pool(config, words[3], body)
    elif key == "ip" and words[1:2] == ["pool"] and len(words) >= 3:  # VRP
        _pool(config, words[2], body)
    elif key == "irf" and words[1:2] == ["member"] and len(words) >= 3 and words[2].isdecimal():
        config.stack_kind = "stack"
        member = config.members.setdefault(int(words[2]), Member(int(words[2])))
        if "priority" in words and words[-1].isdecimal():
            member.priority = int(words[-1])
    elif key not in _IGNORED:
        config.unknown.append(head[:120])


def _blocks(lines: list[str]) -> list[tuple[str, list[str], int]]:
    """Commands with their sub-commands.

    Global commands are written one space deep after a ``#`` line, sub-commands one
    space below a command at the margin (and two below a global one).
    """
    result: list[tuple[str, list[str], int]] = []
    head_depth = 0
    fresh = True  # after a "#" line the next line starts a command
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if stripped.startswith(("#", "!")):
            fresh = True
            continue
        if not stripped or stripped == "return":
            continue
        depth = len(raw) - len(raw.lstrip())
        if result and depth > head_depth and not fresh:
            result[-1][1].append(stripped)
            continue
        result.append((stripped, [], number))
        head_depth, fresh = depth, False
    return result


def _vlan_interface(name: str) -> int | None:
    match = re.fullmatch(r"(?i)(?:vlan-interface|vlanif)(\d{1,4})", name)
    return int(match.group(1)) if match else None


def _lag_name(number: str, vrp: bool) -> str:
    return f"Eth-Trunk{number}" if vrp else f"Bridge-Aggregation{number}"


def _interface(config: Config, name: str, body: list[str]) -> None:
    vid = _vlan_interface(name)
    if vid is not None:
        vlan = config.vlans.setdefault(vid, Vlan(vid))
        for line in body:
            parts = line.split()
            if parts[:2] in (["ip", "address"], ["ipv6", "address"]) and len(parts) >= 3:
                if parts[2] == "dhcp-alloc":
                    vlan.dhcp = True
                else:
                    vlan.addresses.append(address_of(_mask(parts[2:4])))
            elif parts[:3] in (["dhcp", "relay", "server-address"], ["dhcp", "relay", "server-ip"]):
                vlan.helpers.append(_ip(parts[3]))
            elif parts[:2] == ["ospf", "enable"] and "area" in parts:
                vlan.ospf_area = parts[parts.index("area") + 1][:15]
        return
    port = config.interface(name)
    for line in body:
        parts = line.split()
        if parts[0] == "description" and len(parts) > 1:
            port.label = _unquote(line.split(None, 1)[1])[:100]
        elif parts == ["shutdown"]:
            port.enabled = False
        elif parts[:2] == ["port", "link-type"] and len(parts) == 3:
            port.mode = "trunk" if parts[2] in ("trunk", "hybrid") else "access"
        elif (
            parts[:3] in (["port", "access", "vlan"], ["port", "default", "vlan"])
            and len(parts) == 4
        ):
            port.access_vlan = int(parts[3]) if parts[3].isdecimal() else None
            if port.mode != "trunk":
                port.mode = "access"
        elif (
            parts[:3] in (["port", "trunk", "permit"], ["port", "trunk", "allow-pass"])
            and len(parts) >= 5
        ):
            spec = " ".join(parts[4:])
            if spec == "all":
                port.allowed = None
            else:
                config.allow(name, vid_list(spec))
        elif parts[:3] == ["port", "trunk", "pvid"] and len(parts) == 5 and parts[4].isdecimal():
            port.native_vlan = int(parts[4])
        elif parts[:2] == ["port", "link-aggregation"] and parts[-1].isdecimal():
            port.lag = _lag_name(parts[-1], vrp=False)
        elif parts[:1] == ["eth-trunk"] and len(parts) == 2 and parts[1].isdecimal():
            port.lag = _lag_name(parts[1], vrp=True)
        elif (parts[:2] == ["link-aggregation", "mode"] and parts[2:3] == ["dynamic"]) or parts[
            :2
        ] == ["mode", "lacp"]:
            port.lag_protocol = "lacp"
        elif parts[:2] == ["ip", "address"] and len(parts) >= 3:
            port.mode = "routed"
            port.addresses.append(address_of(_mask(parts[2:4])))
        elif parts[:2] in (["stp", "edged-port"], ["stp", "edged-port", "enable"][:2]):
            port.edge = True
        elif parts[:2] == ["stp", "root-protection"]:
            port.root_guard = True
        elif parts[:2] == ["stp", "bpdu-protection"]:
            port.bpdu_guard = True


def _mask(words: list[str]) -> list[str]:
    """``A.B.C.D 24`` (length instead of a mask) as ``A.B.C.D/24``."""
    if len(words) == 2 and words[1].isdecimal():
        return [f"{words[0]}/{words[1]}"]
    return words


def _route(words: list[str]) -> Route:
    """``prefix mask|len next-hop [preference n]``; ``vpn-instance`` routes are skipped."""
    if words[0] == "vpn-instance":
        raise ParseError("routes of VPN instances are not documented")
    prefix = network_of(_mask(words[:2])) if words[1].isdecimal() else network_of(words[:2])
    hop = words[2]
    distance = None
    after = words[words.index("preference") + 1 :] if "preference" in words else []
    if after and after[0].isdecimal():  # "preference" may also end a description
        distance = min(255, int(after[0]))
    return Route(prefix, _ip(hop) if hop[:1].isdecimal() else None, distance)


def _stp(config: Config, words: list[str], body: list[str]) -> None:
    if words[:1] == ["mode"] and len(words) == 2:
        config.stp_mode = {"stp": "stp", "rstp": "rstp", "mstp": "mstp", "pvst": "pvst"}.get(
            words[1], ""
        )
    elif words[:1] == ["priority"] and len(words) == 2 and words[1].isdecimal():
        config.stp_priority = int(words[1])
    elif words[:2] == ["instance", "0"] and "priority" in words:
        config.stp_priority = int(words[-1]) if words[-1].isdecimal() else config.stp_priority
    elif words[:2] == ["global", "enable"] or words == ["enable"]:
        config.stp_mode = config.stp_mode or "mstp"
    elif words[:1] == ["region-configuration"]:
        for line in body:
            parts = line.split()
            if parts[:1] == ["region-name"] and len(parts) == 2:
                config.mst_region = parts[1][:32]
            elif parts[:1] == ["revision-level"] and parts[1:2] and parts[1].isdecimal():
                config.mst_revision = int(parts[1])
            elif parts[:1] == ["instance"] and "vlan" in parts and parts[1].isdecimal():
                spec = " ".join(parts[parts.index("vlan") + 1 :])
                config.add_mst_vlans(int(parts[1]), vid_list(spec))


def _bgp(config: Config, body: list[str]) -> None:
    for line in body:
        parts = line.split()
        if parts[:1] == ["router-id"] and len(parts) == 2:
            config.bgp_router_id = _ip(parts[1])
        elif (
            parts[:1] == ["peer"]
            and len(parts) >= 4
            and parts[2] == "as-number"
            and parts[3].isdecimal()
            and is_address(parts[1])
        ):
            config.neighbors[_ip(parts[1])] = Neighbor(_ip(parts[1]), int(parts[3]))
        elif parts[:1] == ["peer"] and len(parts) >= 4 and parts[2] == "description":
            neighbor = config.neighbors.get(parts[1])
            if neighbor is not None:
                neighbor.description = _unquote(line.split("description", 1)[1])[:200]


def _pool(config: Config, name: str, body: list[str]) -> None:
    pool = config.pools.setdefault(name[:100], Pool(name[:100]))
    for line in body:
        parts = line.split()
        if parts[:1] == ["network"] and len(parts) >= 2:
            mask = (
                parts[3]
                if len(parts) >= 4 and parts[2] == "mask"
                else (parts[2] if len(parts) > 2 else None)
            )
            pool.network = network_of(_mask([parts[1], mask]) if mask else [parts[1]])
        elif parts[:1] in (["gateway-list"], ["gateway"]) and len(parts) >= 2:
            pool.gateway = _ip(parts[1])
        elif parts[:1] == ["dns-list"]:
            pool.dns += [_ip(p) for p in parts[1:]]
        elif parts[:1] == ["domain-name"] and len(parts) == 2:
            pool.domain = parts[1][:253]
        elif parts[:2] == ["address", "range"] and len(parts) >= 4:
            pool.start, pool.end = _ip(parts[2]), _ip(parts[3])
        elif parts[:1] == ["static-bind"] and "ip-address" in parts:
            ip = _ip(parts[parts.index("ip-address") + 1])
            raw = parts[parts.index("hardware-address") + 1] if "hardware-address" in parts else ""
            raw = parts[parts.index("mac-address") + 1] if "mac-address" in parts else raw
            digits = re.sub(r"[^0-9a-f]", "", raw.lower())
            if len(digits) == 12:
                mac = ":".join(digits[i : i + 2] for i in range(0, 12, 2))
                pool.reservations.append((mac, ip, ""))
        elif parts[:1] == ["expired"] and "day" in parts:
            days = int(parts[parts.index("day") + 1])
            hours = int(parts[parts.index("hour") + 1]) if "hour" in parts else 0
            pool.lease_seconds = max(60, days * 86400 + hours * 3600)
    if pool.network and not pool.start:
        network = ipaddress.ip_network(pool.network)
        if network.num_addresses > 2:
            pool.start, pool.end = (
                str(network.network_address + 1),
                str(network.broadcast_address - 1),
            )
