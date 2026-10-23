"""Juniper Junos configurations, as ``set`` commands or in the curly brace format."""

import re
import shlex

from patchmanager.networks.platforms.common import (
    MAX_BYTES,
    MAX_LINES,
    SECRET,
    Config,
    Neighbor,
    ParseError,
    Pool,
    Route,
    TooLargeError,
    Vlan,
    _ip,
    address_of,
    finish,
    lines_of,
    network_of,
    vid_list,
)

MAX_DEPTH = 32


def detect(text: str) -> int:
    """How sure the text is a Junos configuration (0 to 100)."""
    score = 0
    if re.search(r"^set (system|interfaces|vlans|protocols) ", text, re.M):
        score += 80
    if re.search(r"^(system|interfaces|protocols) \{", text, re.M):
        score += 70
    if re.search(r"^## Last commit", text, re.M):
        score += 30
    return min(score, 100)


def to_set(lines: list[str]) -> list[str]:
    """The curly brace format as ``set`` lines (``set`` lines pass through).

    Each list value repeats its whole block path, so the output is bounded by the same
    limits as the input; a small, deeply nested text cannot expand without limit.
    """
    result: list[str] = []
    path: list[str] = []
    size = 0

    def emit(line: str) -> None:
        nonlocal size
        size += len(line)
        if len(result) >= MAX_LINES or size > MAX_BYTES:
            raise TooLargeError("the configuration expands to too many set commands")
        result.append(line)

    for number, raw in enumerate(lines, 1):
        line = raw.split("#", 1)[0].strip() if not raw.strip().startswith('"') else raw.strip()
        if not line or line.startswith(("/*", "*")):
            continue
        if line.startswith(("set ", "delete ", "deactivate ")):
            emit(line)
            continue
        if line.endswith("{"):
            path.append(line[:-1].strip())
            if len(path) > MAX_DEPTH:
                raise ParseError(f"line {number}: blocks nested too deeply")
        elif line == "}":
            if path:
                path.pop()
        elif line.endswith(";"):
            statement = line[:-1].strip()
            if statement.startswith("inactive:"):
                continue
            if "[" in statement and statement.endswith("]"):  # a list: one set line per value
                head, _sep, values = statement.partition("[")
                for value in values[:-1].split():
                    emit("set " + " ".join([*path, head.strip(), value]))
            else:
                emit("set " + " ".join([*path, statement]))
    return result


def parse(text: str) -> Config:
    """Read a Junos configuration."""
    config = Config(vendor="junos")
    vlan_names: dict[str, int] = {}
    members: list[tuple[str, str]] = []  # (port, vlan name or ID)
    irb: dict[str, list[str]] = {}
    group_of: dict[str, str] = {}  # BGP neighbor address -> its group
    group_as: dict[str, int] = {}  # BGP group -> its peer AS
    descriptions: dict[str, str] = {}  # BGP neighbor address -> description
    for number, line in enumerate(to_set(lines_of(text)), 1):
        if SECRET.search(line):
            config.secrets_skipped += 1
            continue
        try:
            words = shlex.split(line)
        except ValueError:
            config.warnings.append(f"statement {number}: unbalanced quotes")
            continue
        if words[0] != "set" or len(words) < 3:
            continue
        w = words[1:]
        try:
            if w[:2] == ["system", "host-name"]:
                config.hostname = w[2][:100]
            elif w[:2] == ["system", "domain-name"]:
                config.domain = w[2][:253]
            elif w[:2] == ["system", "name-server"]:
                config.dns_servers.append(_ip(w[2]))
            elif w[0] == "vlans" and len(w) >= 4 and w[2] == "vlan-id" and w[3].isdecimal():
                vid = int(w[3])
                vlan_names[w[1]] = vid
                vlan = config.vlans.setdefault(vid, Vlan(vid))
                vlan.name = vlan.name or w[1][:64]
            elif w[0] == "vlans" and len(w) >= 4 and w[2] == "l3-interface":
                irb.setdefault(w[3], []).append(w[1])
            elif w[0] == "interfaces" and len(w) >= 3:
                _interface(config, w[1], w[2:], members, irb)
            elif (
                w[:3] == ["routing-options", "static", "route"]
                and len(w) >= 6
                and w[4] == "next-hop"
            ):
                config.routes.append(Route(network_of([w[3]]), _ip(w[5])))
            elif w[:2] == ["routing-options", "router-id"]:
                config.bgp_router_id = config.ospf_router_id = _ip(w[2])
            elif w[:2] == ["routing-options", "autonomous-system"] and w[2].isdecimal():
                config.bgp_asn = int(w[2])
            elif w[:2] == ["protocols", "bgp"] and "neighbor" in w[:-1]:
                address = _ip(w[w.index("neighbor") + 1])
                peer_as = _after(w, "peer-as")
                if peer_as.isdecimal():
                    config.neighbors[address] = Neighbor(address, int(peer_as))
                elif "description" in w:  # the neighbor may only be created later
                    descriptions[address] = _after(w, "description")[:200]
                if "group" in w:  # the AS may be set on the group (the usual layout)
                    group_of.setdefault(address, _after(w, "group"))
            elif w[:2] == ["protocols", "bgp"] and "group" in w and "peer-as" in w:
                peer_as = _after(w, "peer-as")
                if peer_as.isdecimal():
                    group_as[_after(w, "group")] = int(peer_as)
            elif w[:2] == ["protocols", "ospf"] and "interface" in w and w[2] == "area":
                _ospf(config, w[3], w[w.index("interface") + 1], "passive" in w)
            elif w[:1] == ["protocols"] and w[1] in ("rstp", "mstp", "vstp", "stp"):
                _stp(config, w[1:])
            elif w[:2] == ["access", "address-assignment"] and "pool" in w:
                _pool(config, w)
            else:
                config.unknown.append(" ".join(w)[:120])
        except TooLargeError:
            raise  # the whole text is refused, not one line
        except ParseError as exc:
            config.warnings.append(f"statement {number}: {exc}")
    count = {port: 0 for port, _name in members}
    for port, _name in members:
        count[port] += 1
    for port, name in members:
        member_vid = vlan_names.get(name) or (int(name) if name.isdecimal() else None)
        if member_vid is not None:
            vid = member_vid
            interface = config.interface(port)
            config.allow(port, [vid])
            if interface.mode != "trunk" and count[port] == 1:
                # Without interface-mode a switching port is an access port (Junos default),
                # which takes one VLAN; more members mean a mode set elsewhere (apply-groups).
                interface.mode, interface.access_vlan = "access", vid
    for address, group in group_of.items():
        if address not in config.neighbors and group in group_as:
            config.neighbors[address] = Neighbor(address, group_as[group])
    for address, description in descriptions.items():
        if address in config.neighbors:
            config.neighbors[address].description = description
    config.unknown = config.unknown[:200]
    return finish(config)


def _after(words: list[str], key: str) -> str:
    """The word after ``key``, or "" when ``key`` is missing or ends the statement."""
    if key not in words:
        return ""
    index = words.index(key) + 1
    return words[index] if index < len(words) else ""


def _interface(
    config: Config,
    name: str,
    w: list[str],
    members: list[tuple[str, str]],
    irb: dict[str, list[str]],
) -> None:
    if name in ("irb", "vlan") and w[:1] == ["unit"] and len(w) >= 2 and w[1].isdecimal():
        if "address" in w:
            vid = int(w[1])
            config.vlans.setdefault(vid, Vlan(vid)).addresses.append(
                address_of([w[w.index("address") + 1]])
            )
        return
    port = config.interface(name)
    if w[0] == "description":
        port.label = w[1][:100]
    elif w[0] == "disable":
        port.enabled = False
    elif w[:2] == ["ether-options", "802.3ad"] or w[:2] == ["gigether-options", "802.3ad"]:
        port.lag = w[2]
    elif w[:2] == ["aggregated-ether-options", "lacp"]:
        port.lag_protocol = "lacp"  # finish() hands it to the members
    elif w[:1] == ["unit"] and "address" in w:
        port.mode = "routed"
        port.addresses.append(address_of([w[w.index("address") + 1]]))
    elif w[:1] == ["unit"] and "interface-mode" in w:
        port.mode = "trunk" if w[w.index("interface-mode") + 1] == "trunk" else "access"
    elif w[:1] == ["unit"] and "port-mode" in w:  # older EX syntax
        port.mode = "trunk" if w[w.index("port-mode") + 1] == "trunk" else "access"
    elif w[:1] == ["unit"] and "members" in w:
        value = w[w.index("members") + 1]
        if value == "all":
            port.allowed = None
        else:
            members.append((name, value))
    elif w[:1] == ["native-vlan-id"] and w[1].isdecimal():
        port.native_vlan = int(w[1])


def _ospf(config: Config, area: str, interface: str, passive: bool) -> None:
    match = re.fullmatch(r"(?:irb|vlan)\.(\d+)", interface)
    if match is None:
        return
    vlan = config.vlans.setdefault(int(match.group(1)), Vlan(int(match.group(1))))
    vlan.ospf_area = area[:15]
    vlan.ospf_passive = vlan.ospf_passive or passive


def _stp(config: Config, w: list[str]) -> None:
    config.stp_mode = {"rstp": "rstp", "mstp": "mstp", "vstp": "rpvst", "stp": "stp"}[w[0]]
    if "bridge-priority" in w:
        value = w[w.index("bridge-priority") + 1].lower()
        priority: int | None = None
        if value.endswith("k") and value[:-1].isdecimal():
            priority = int(value[:-1]) * 1024
        elif value.isdecimal():
            priority = int(value)
        if priority is not None and "msti" not in w:
            config.stp_priority = priority
    if "configuration-name" in w:
        config.mst_region = w[w.index("configuration-name") + 1][:32]
    if "msti" in w and "vlan" in w:
        config.add_mst_vlans(int(w[w.index("msti") + 1]), vid_list(w[w.index("vlan") + 1]))
    if "interface" in w and "edge" in w:
        config.interface(w[w.index("interface") + 1]).edge = True


def _pool(config: Config, w: list[str]) -> None:
    name = w[w.index("pool") + 1][:100]
    pool = config.pools.setdefault(name, Pool(name))
    if "network" in w:
        pool.network = network_of([w[w.index("network") + 1]])
    if "low" in w:
        pool.start = _ip(w[w.index("low") + 1])
    if "high" in w:
        pool.end = _ip(w[w.index("high") + 1])
    if "router" in w:
        pool.gateway = _ip(w[w.index("router") + 1])
    if "name-server" in w:
        pool.dns.append(_ip(w[w.index("name-server") + 1]))
    if "domain-name" in w:
        pool.domain = w[w.index("domain-name") + 1][:253]
    if "maximum-lease-time" in w and w[w.index("maximum-lease-time") + 1].isdecimal():
        pool.lease_seconds = max(60, int(w[w.index("maximum-lease-time") + 1]))
    if "host" in w and "hardware-address" in w and "ip-address" in w:
        pool.reservations.append(
            (
                w[w.index("hardware-address") + 1].lower(),
                _ip(w[w.index("ip-address") + 1]),
                w[w.index("host") + 1][:100],
            )
        )
