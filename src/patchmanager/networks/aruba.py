"""Read and write ArubaOS-Switch (ProCurve, AOS-S) configurations.

Only the parts that belong in the documentation are read: VLANs and their ports,
VLAN interface addresses, trunks (link aggregations), port names and states,
spanning tree, static routes, OSPF, BGP, device profiles, tunnelled ports and
stacking members. Secrets (passwords, SNMP communities, keys) are never read.
Aruba Central templates (``%variable%``, ``%if%`` ... ``%endif%``) are filled
from a variables file first.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any

MAX_LINES = 50_000
MAX_RANGE = 1024  # ports in one range like 1-48
MAX_LINE = 2000  # characters; longer lines are not configuration
MAX_NAME = 64  # characters of one port name

_SECRET = re.compile(
    r"^(password|snmp-server community|snmpv3|radius-server.*key|tacacs-server.*key|"
    r"aaa .*key|key-chain|include-credentials|crypto|ssh .*key|https-server)",
    re.I,
)
# Commands that are fine to leave out of the documentation (not reported as unread).
_IGNORED = frozenset(
    (
        "jumbo",
        "aruba-central",
        "no",
        "console",
        "time",
        "timesync",
        "sntp",
        "logging",
        "module",
        "ip",
        "oobm",
        "loop-protect",
        "lldp",
        "cwmp",
        "web-management",
        "rest-interface",
        "ntp",
        "banner",
        "max-vlans",
        "qos",
        "igmp",
        "tftp",
        "autorun",
        "dhcp",
        "snmp-server",
    )
)
_TEMPLATE = re.compile(r"%([A-Za-z0-9_.-]+)%")
_RANGE_END = re.compile(r"^(.*?)(\d+)$")


@dataclass
class Vlan:
    """One ``vlan`` block."""

    vid: int
    name: str = ""
    untagged: list[str] = field(default_factory=list)
    tagged: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)  # 10.0.0.1/24
    dhcp: bool = False
    jumbo: bool = False
    ospf_area: str = ""
    ospf_passive: bool = False


@dataclass
class Interface:
    """One port with the settings the configuration gives it."""

    name: str
    label: str = ""
    enabled: bool = True
    tunneled: bool = False
    edge: bool = False
    bpdu_guard: bool = False
    root_guard: bool = False


@dataclass
class Trunk:
    """A link aggregation (``trunk 1-2 trk1 lacp``)."""

    name: str
    members: list[str]
    protocol: str  # lacp or static


@dataclass
class Route:
    """A static route."""

    prefix: str
    next_hop: str | None
    distance: int | None = None


@dataclass
class Neighbor:
    """A BGP peer."""

    address: str
    remote_asn: int
    description: str = ""
    enabled: bool = True


@dataclass
class Profile:
    """A device profile (port settings applied when a kind of device is detected)."""

    name: str
    untagged: int | None = None
    tagged: list[int] = field(default_factory=list)
    jumbo: bool = False
    detects: str = ""


@dataclass
class Member:
    """A stacking or VSF member."""

    member_id: int
    priority: int | None = None
    model: str = ""


@dataclass
class Config:
    """What a configuration says, in documentation terms."""

    hostname: str = ""
    platform: str = ""  # from the header, for example "JL258A ... WC.16.10.0009"
    vlans: dict[int, Vlan] = field(default_factory=dict)
    interfaces: dict[str, Interface] = field(default_factory=dict)
    trunks: dict[str, Trunk] = field(default_factory=dict)
    stp_mode: str = ""  # stp, rstp, mstp, rpvst, off
    stp_priority: int | None = None
    mst_region: str = ""
    mst_revision: int | None = None
    mst_instances: dict[int, tuple[list[int], int | None]] = field(default_factory=dict)
    ip_routing: bool = False
    routes: list[Route] = field(default_factory=list)
    ospf_router_id: str = ""
    bgp_asn: int | None = None
    bgp_router_id: str = ""
    neighbors: dict[str, Neighbor] = field(default_factory=dict)
    profiles: dict[str, Profile] = field(default_factory=dict)
    gateways: list[str] = field(default_factory=list)
    stack_kind: str = ""  # vsf or stack
    members: dict[int, Member] = field(default_factory=dict)
    secrets_skipped: int = 0
    unknown: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def interface(self, name: str) -> Interface:
        """The interface entry of ``name`` (created on first use)."""
        return self.interfaces.setdefault(name, Interface(name))


class ParseError(ValueError):
    """The text cannot be read as a configuration."""


# --- Templates ----------------------------------------------------------------


def fill_template(text: str, variables: dict[str, Any], warnings: list[str]) -> list[str]:
    """Resolve Aruba Central template syntax; unknown variables drop their line."""
    lines: list[str] = []
    active = [True]
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if match := re.fullmatch(r"%if\s+([A-Za-z0-9_.-]+)\s*(?:(!?=)\s*(.*?))?\s*%", stripped):
            name, op, expected = match.groups()
            value = str(variables.get(name, "")).strip()
            if op:
                hit = (value == expected.strip()) == (op == "=")
            else:
                hit = value.lower() not in ("", "false", "0", "no")
            active.append(active[-1] and hit)
            continue
        if stripped == "%else%" and len(active) > 1:
            active[-1] = active[-2] and not active[-1]
            continue
        if stripped == "%endif%" and len(active) > 1:
            active.pop()
            continue
        if not active[-1]:
            continue

        def put(match: re.Match[str]) -> str:
            value = variables.get(match.group(1))
            return match.group(0) if value is None else str(value)

        line = _TEMPLATE.sub(put, raw)
        if missing := _TEMPLATE.findall(line):
            if not all(name.startswith("_sys_") for name in missing):
                warnings.append(f"line {number}: no value for %{missing[0]}%, line skipped")
            continue
        lines.append(line)
    return lines


# --- Helpers -------------------------------------------------------------------


def expand(spec: str) -> list[str]:
    """Port lists like ``1-7,9,A1-A4,1/1-1/24,Trk1`` as single names."""
    names: list[str] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        if len(part) > 2 * MAX_NAME + 1:  # also keeps the regular expression linear
            raise ParseError(f"cannot read the port range {part[:20]}...")
        if "-" not in part:
            names.append(part)
            continue
        first, _sep, last = part.partition("-")
        a, b = _RANGE_END.match(first), _RANGE_END.match(last)
        if not a or not b or a.group(1).lower() != b.group(1).lower():
            raise ParseError(f"cannot read the port range {part}")
        start, end = int(a.group(2)), int(b.group(2))
        if end < start or end - start >= MAX_RANGE:
            raise ParseError(f"cannot read the port range {part}")
        names.extend(f"{a.group(1)}{n}" for n in range(start, end + 1))
    return names


def compress(names: list[str]) -> str:
    """The reverse of :func:`expand`: ``1,2,3,5`` becomes ``1-3,5``."""
    parts: list[str] = []
    run: list[tuple[str, int]] = []

    def flush() -> None:
        if run:
            first, last = run[0], run[-1]
            parts.append(
                f"{first[0]}{first[1]}"
                if len(run) == 1
                else f"{first[0]}{first[1]}-{last[0]}{last[1]}"
            )
            run.clear()

    for name in names:
        match = _RANGE_END.match(name)
        if match is None:
            flush()
            parts.append(name)
            continue
        prefix, number = match.group(1), int(match.group(2))
        if run and run[-1][0] == prefix and run[-1][1] + 1 == number:
            run.append((prefix, number))
        else:
            flush()
            run.append((prefix, number))
    flush()
    return ",".join(parts)


def vids(spec: str) -> list[int]:
    """VLAN lists like ``10,20-25``."""
    result: list[int] = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        first, _sep, last = part.partition("-")
        if not first.isdigit() or (last and not last.isdigit()):
            raise ParseError(f"cannot read the VLAN list {spec}")
        start, end = int(first), int(last or first)
        if not 1 <= start <= end <= 4094:
            raise ParseError(f"cannot read the VLAN list {spec}")
        result.extend(range(start, end + 1))
    return result


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _address(address: str, mask: str | None) -> str:
    """``10.0.0.1 255.255.255.0`` or ``10.0.0.1/24`` as ``10.0.0.1/24``."""
    try:
        if "/" in address:
            return str(ipaddress.ip_interface(address))
        if mask is None:
            raise ValueError(address)
        return str(ipaddress.ip_interface(f"{address}/{mask}"))
    except ValueError as exc:
        raise ParseError(f"cannot read the address {address}") from exc


def _network(address: str, mask: str | None) -> str:
    try:
        if "/" in address:
            return str(ipaddress.ip_network(address, strict=False))
        return str(ipaddress.ip_network(f"{address}/{mask}", strict=False))
    except ValueError as exc:
        raise ParseError(f"cannot read the network {address}") from exc


def _ip(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ParseError(f"cannot read the address {value}") from exc


def _blocks(lines: list[str]) -> list[tuple[str, list[str], int]]:
    """Top level commands with their indented sub-commands (up to ``exit``)."""
    blocks: list[tuple[str, list[str], int]] = []
    for number, raw in enumerate(lines, 1):
        if len(raw) > MAX_LINE:
            raise ParseError(f"line {number} is longer than {MAX_LINE} characters")
        line = raw.split("//", 1)[0].rstrip()  # template comments
        if not line.strip() or line.lstrip().startswith((";", "!", "#")):
            continue
        if line[0].isspace() and blocks:
            if line.strip() != "exit":
                blocks[-1][1].append(line.strip())
            continue
        blocks.append((line.strip(), [], number))
    return blocks


# --- Parser --------------------------------------------------------------------


def parse(text: str, variables: dict[str, Any] | None = None) -> Config:
    """Read an ArubaOS-Switch configuration (or Aruba Central template)."""
    if "\x00" in text:
        raise ParseError("the file is not a text configuration")
    config = Config()
    lines = fill_template(text, variables or {}, config.warnings)
    if len(lines) > MAX_LINES:
        raise ParseError(f"the configuration has more than {MAX_LINES} lines")
    for raw in lines[:5]:
        if raw.startswith(";") and "Configuration Editor" in raw:
            config.platform = raw.lstrip("; ").strip()[:200]
    for head, body, number in _blocks(lines):
        try:
            _command(config, head, body)
        except ParseError as exc:
            config.warnings.append(f"line {number}: {exc}")
    for vlan in config.vlans.values():  # a port is untagged in one VLAN only
        for name in vlan.untagged:
            others = [v.vid for v in config.vlans.values() if v is not vlan and name in v.untagged]
            if others:
                config.warnings.append(
                    f"port {name} is untagged in VLAN {vlan.vid} and {others[0]}"
                )
    return config


def _command(config: Config, head: str, body: list[str]) -> None:
    words = head.split()
    key = words[0].lower()
    if _SECRET.match(head):
        config.secrets_skipped += 1
        return
    if key == "hostname" and len(words) > 1:
        config.hostname = _unquote(head.split(None, 1)[1])[:100]
    elif key == "vlan" and len(words) == 2 and words[1].isdigit():
        _vlan(config, int(words[1]), body)
    elif key == "interface" and len(words) == 2:
        for name in expand(words[1]):
            _interface(config, name, body)
    elif key == "trunk" and len(words) >= 3:
        protocol = "lacp" if len(words) > 3 and words[3].lower() == "lacp" else "static"
        name = words[2][0].upper() + words[2][1:].lower()  # trk1 -> Trk1
        config.trunks[name] = Trunk(name, expand(words[1]), protocol)
    elif key == "spanning-tree":
        _spanning_tree(config, words[1:])
        for line in body:
            _spanning_tree(config, line.split())
    elif key == "ip" and words[1:2] == ["routing"]:
        config.ip_routing = True
    elif key == "ip" and words[1:2] == ["route"] and len(words) >= 4:
        _route(config, words[2:])
    elif key == "ip" and words[1:2] == ["default-gateway"] and len(words) == 3:
        config.routes.append(Route("0.0.0.0/0", _ip(words[2])))
    elif key == "router" and words[1:2] == ["ospf"]:
        for line in body:
            parts = line.split()
            if parts[:1] == ["router-id"] and len(parts) == 2:
                config.ospf_router_id = _ip(parts[1])
    elif key == "ip" and words[1:2] == ["router-id"] and len(words) == 3:
        config.ospf_router_id = config.ospf_router_id or _ip(words[2])
    elif key == "router" and words[1:2] == ["bgp"] and len(words) == 3 and words[2].isdigit():
        config.bgp_asn = int(words[2])
        _bgp(config, body)
    elif key == "device-profile" and words[1:2] == ["name"]:
        _profile(config, _unquote(head.split("name", 1)[1]), body)
    elif key == "device-profile" and words[1:2] == ["type"]:
        kind = _unquote(head.split("type", 1)[1])
        for line in body:
            if line.startswith("associate"):
                profile = config.profiles.get(_unquote(line.split(None, 1)[1]))
                if profile is not None:
                    profile.detects = kind[:64]
    elif key == "tunneled-node-server" and not words[1:]:
        for line in body:
            parts = line.split()
            if parts[0] in ("controller-ip", "backup-controller-ip") and len(parts) == 2:
                config.gateways.append(_ip(parts[1]))
    elif key in ("vsf", "stacking"):
        _stack(config, key, words[1:], body)
    elif key not in _IGNORED:
        config.unknown.append(head[:120])


def _vlan(config: Config, vid: int, body: list[str]) -> None:
    if not 1 <= vid <= 4094:
        raise ParseError(f"VLAN {vid} is out of range")
    vlan = config.vlans.setdefault(vid, Vlan(vid))
    for line in body:
        parts = line.split()
        if parts[0] == "name" and len(parts) > 1:
            vlan.name = _unquote(line.split(None, 1)[1])[:64]
        elif parts[0] in ("untagged", "tagged") and len(parts) == 2:
            target = vlan.untagged if parts[0] == "untagged" else vlan.tagged
            target.extend(n for n in expand(parts[1]) if n not in target)
        elif parts[:2] in (["no", "untagged"], ["no", "tagged"]) and len(parts) == 3:
            target = vlan.untagged if parts[1] == "untagged" else vlan.tagged
            gone = set(expand(parts[2]))
            target[:] = [n for n in target if n not in gone]
        elif parts[:2] == ["ip", "address"] and len(parts) >= 3:
            if parts[2] == "dhcp-bootp":
                vlan.dhcp = True
            else:
                vlan.addresses.append(_address(parts[2], parts[3] if len(parts) > 3 else None))
        elif parts[:2] == ["ip", "ospf"]:
            if "area" in parts:
                vlan.ospf_area = parts[parts.index("area") + 1][:15]
            if "passive" in parts:
                vlan.ospf_passive = True
        elif parts[0] == "jumbo":
            vlan.jumbo = True
        elif parts[:3] == ["no", "ip", "address"]:
            vlan.addresses.clear()
            vlan.dhcp = False


def _interface(config: Config, name: str, body: list[str]) -> None:
    port = config.interface(name)
    for line in body:
        parts = line.split()
        if parts[0] == "name" and len(parts) > 1:
            port.label = _unquote(line.split(None, 1)[1])[:100]
        elif parts[0] == "disable":
            port.enabled = False
        elif parts[0] == "enable":
            port.enabled = True
        elif parts[0] == "tunneled-node-server":
            port.tunneled = True
        elif parts[0] == "spanning-tree":
            _port_stp(port, parts[1:])


def _port_stp(port: Interface, options: list[str]) -> None:
    if "admin-edge-port" in options:
        port.edge = True
    if "bpdu-protection" in options or "bpdu-guard" in options:
        port.bpdu_guard = True
    if "root-guard" in options:
        port.root_guard = True


def _spanning_tree(config: Config, words: list[str]) -> None:
    if not words:
        config.stp_mode = config.stp_mode or "mstp"  # the AOS-S default
        return
    if words[0] == "mode" and len(words) == 2:
        config.stp_mode = {"rapid-pvst": "rpvst", "mstp": "mstp"}.get(words[1], words[1][:5])
    elif words[0] == "force-version" and len(words) == 2:
        config.stp_mode = {"stp-compatible": "stp", "rstp-operation": "rstp"}.get(
            words[1], config.stp_mode
        )
    elif words[0] == "priority" and len(words) == 2 and words[1].isdigit():
        config.stp_priority = int(words[1]) * 4096  # AOS-S counts in steps of 4096
    elif words[0] == "config-name" and len(words) >= 2:
        config.mst_region = _unquote(" ".join(words[1:]))[:32]
    elif words[0] == "config-revision" and len(words) == 2 and words[1].isdigit():
        config.mst_revision = int(words[1])
    elif words[0] == "instance" and len(words) >= 4 and words[1].isdigit():
        number = int(words[1])
        vlans, priority = config.mst_instances.get(number, ([], None))
        if words[2] == "vlan":
            vlans = sorted(set(vlans) | set(vids(words[3])))
        elif words[2] == "priority" and words[3].isdigit():
            priority = int(words[3]) * 4096
        config.mst_instances[number] = (vlans, priority)
    elif words[0] not in (
        "mode",
        "priority",
        "instance",
        "vlan",
        "trap",
        "legacy-path-cost",
        "legacy-mode",
        "pathcost",
        "hello-time",
        "max-hops",
        "bpdu-throttle",
    ):
        for name in expand(words[0]):  # spanning-tree 1-24 admin-edge-port
            _port_stp(config.interface(name), words[1:])


def _route(config: Config, words: list[str]) -> None:
    if "/" in words[0]:
        prefix, rest = _network(words[0], None), words[1:]
    else:
        prefix, rest = _network(words[0], words[1]), words[2:]
    if not rest:
        raise ParseError("the route has no next hop")
    next_hop = None if rest[0] in ("reject", "blackhole") else _ip(rest[0])
    distance = None
    if "distance" in rest and rest.index("distance") + 1 < len(rest):
        value = rest[rest.index("distance") + 1]
        distance = int(value) if value.isdigit() and int(value) <= 255 else None
    config.routes.append(Route(prefix, next_hop, distance))


def _bgp(config: Config, body: list[str]) -> None:
    for line in body:
        parts = line.split()
        if parts[:2] == ["bgp", "router-id"] and len(parts) == 3:
            config.bgp_router_id = _ip(parts[2])
        elif parts[0] == "neighbor" and len(parts) >= 3:
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


def _profile(config: Config, name: str, body: list[str]) -> None:
    profile = config.profiles.setdefault(name[:64], Profile(name[:64]))
    for line in body:
        parts = line.split()
        if parts[0] == "untagged-vlan" and len(parts) == 2 and parts[1].isdigit():
            profile.untagged = int(parts[1])
        elif parts[0] == "tagged-vlan" and len(parts) == 2:
            profile.tagged = vids(parts[1])
        elif parts[0] == "allow-jumbo-frames":
            profile.jumbo = True


def _stack(config: Config, key: str, words: list[str], body: list[str]) -> None:
    config.stack_kind = "vsf" if key == "vsf" else "stack"
    lines = [" ".join(words)] if words else []
    for line in [*lines, *body]:
        parts = line.split()
        if parts[:1] == ["member"] and len(parts) >= 2 and parts[1].isdigit():
            member = config.members.setdefault(int(parts[1]), Member(int(parts[1])))
            rest = parts[2:]
            if rest[:1] == ["priority"] and len(rest) == 2 and rest[1].isdigit():
                member.priority = int(rest[1])
            elif rest[:1] == ["type"] and len(rest) >= 2:
                member.model = _unquote(rest[1])[:40]
        elif (
            parts[:1] == ["priority"]
            and len(parts) == 2
            and parts[1].isdigit()
            and words[:1] == ["member"]
        ):
            config.members[int(words[1])].priority = int(parts[1])
        elif parts[:1] == ["type"] and len(parts) >= 2 and words[:1] == ["member"]:
            config.members[int(words[1])].model = _unquote(parts[1])[:40]


# --- Writer --------------------------------------------------------------------


def render(config: Config) -> str:
    """An ArubaOS-Switch configuration with what :func:`parse` reads."""
    out: list[str] = []
    if config.hostname:
        out.append(f'hostname "{config.hostname}"')
    for trunk in config.trunks.values():
        mode = "lacp" if trunk.protocol == "lacp" else "trunk"
        out.append(f"trunk {compress(trunk.members)} {trunk.name.lower()} {mode}")
    if config.ip_routing:
        out.append("ip routing")
    for route in config.routes:
        out.append(
            f"ip route {route.prefix} {route.next_hop or 'blackhole'}"
            + (f" distance {route.distance}" if route.distance else "")
        )
    if config.gateways:
        out.append("tunneled-node-server")
        out.append(f"   controller-ip {config.gateways[0]}")
        if len(config.gateways) > 1:
            out.append(f"   backup-controller-ip {config.gateways[1]}")
        out.append("   exit")
    for port in config.interfaces.values():
        lines = []
        if port.label:
            lines.append(f'name "{port.label}"')
        if not port.enabled:
            lines.append("disable")
        if port.tunneled:
            lines.append("tunneled-node-server")
        if lines:
            out.append(f"interface {port.name}")
            out.extend(f"   {line}" for line in lines)
            out.append("   exit")
    for vid in sorted(config.vlans):
        vlan = config.vlans[vid]
        out.append(f"vlan {vid}")
        if vlan.name:
            out.append(f'   name "{vlan.name}"')
        if vlan.untagged:
            out.append(f"   untagged {compress(vlan.untagged)}")
        if vlan.tagged:
            out.append(f"   tagged {compress(vlan.tagged)}")
        out.extend(f"   ip address {address}" for address in vlan.addresses)
        if vlan.dhcp:
            out.append("   ip address dhcp-bootp")
        if not vlan.addresses and not vlan.dhcp:
            out.append("   no ip address")
        if vlan.ospf_area:
            out.append(f"   ip ospf area {vlan.ospf_area}")
        if vlan.ospf_passive:
            out.append("   ip ospf passive")
        if vlan.jumbo:
            out.append("   jumbo")
        out.append("   exit")
    if config.stp_mode and config.stp_mode != "off":
        out.append("spanning-tree")
        if config.stp_mode == "rpvst":
            out.append("spanning-tree mode rapid-pvst")
        elif config.stp_mode in ("stp", "rstp"):
            version = "stp-compatible" if config.stp_mode == "stp" else "rstp-operation"
            out.append(f"spanning-tree force-version {version}")
        if config.stp_priority is not None:
            out.append(f"spanning-tree priority {config.stp_priority // 4096}")
        if config.mst_region:
            out.append(f'spanning-tree config-name "{config.mst_region}"')
        if config.mst_revision is not None:
            out.append(f"spanning-tree config-revision {config.mst_revision}")
        for number, (vlans, priority) in sorted(config.mst_instances.items()):
            if vlans:
                out.append(f"spanning-tree instance {number} vlan {_vid_ranges(vlans)}")
            if priority is not None:
                out.append(f"spanning-tree instance {number} priority {priority // 4096}")
        for flag, word in (
            ("edge", "admin-edge-port"),
            ("bpdu_guard", "bpdu-protection"),
            ("root_guard", "root-guard"),
        ):
            names = [p.name for p in config.interfaces.values() if getattr(p, flag)]
            if names:
                out.append(f"spanning-tree {compress(names)} {word}")
    if config.ospf_router_id:
        out.extend(["router ospf", f"   router-id {config.ospf_router_id}", "   enable", "   exit"])
    if config.bgp_asn:
        out.append(f"router bgp {config.bgp_asn}")
        if config.bgp_router_id:
            out.append(f"   bgp router-id {config.bgp_router_id}")
        for neighbor in config.neighbors.values():
            out.append(f"   neighbor {neighbor.address} remote-as {neighbor.remote_asn}")
            if neighbor.description:
                out.append(f'   neighbor {neighbor.address} description "{neighbor.description}"')
            if not neighbor.enabled:
                out.append(f"   neighbor {neighbor.address} shutdown")
        out.extend(["   enable", "   exit"])
    for profile in config.profiles.values():
        out.append(f'device-profile name "{profile.name}"')
        if profile.untagged:
            out.append(f"   untagged-vlan {profile.untagged}")
        if profile.tagged:
            out.append(f"   tagged-vlan {_vid_ranges(profile.tagged)}")
        if profile.jumbo:
            out.append("   allow-jumbo-frames")
        out.append("   exit")
        if profile.detects:
            out.extend(
                [
                    f'device-profile type "{profile.detects}"',
                    f'   associate "{profile.name}"',
                    "   enable",
                    "   exit",
                ]
            )
    return "\n".join(out) + "\n"


def _vid_ranges(numbers: list[int]) -> str:
    return compress([f"v{n}" for n in sorted(set(numbers))]).replace("v", "")
