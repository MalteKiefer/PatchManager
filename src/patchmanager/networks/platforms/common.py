"""What a network configuration says, in documentation terms, independent of the vendor.

Every platform parser fills a :class:`Config`; :func:`finish` turns per interface VLAN
settings (access, trunk, aggregation) into per VLAN port lists, so applying a
configuration works the same for every vendor.
"""

import ipaddress
import re
from dataclasses import dataclass, field

MAX_LINES = 50_000
MAX_RANGE = 1024  # ports in one range like 1-48
MAX_LINE = 2000  # characters; longer lines are not configuration
MAX_NAME = 64  # characters of one port name
MAX_NAMES = 20_000  # port names in all ranges of one configuration
MAX_BYTES = 4 * 1024 * 1024  # text after template variables are filled in
MAX_VARIABLE = 1000  # characters of one template variable

_RANGE_END = re.compile(r"^(.*?)(\d+)$")
OSPF_AREA = re.compile(r"^(\d{1,10}|\d{1,3}(\.\d{1,3}){3}|backbone)$")


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
    helpers: list[str] = field(default_factory=list)  # DHCP relay targets


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
    # Per interface VLAN settings (Cisco style); finish() turns them into VLAN port lists.
    mode: str = ""  # access, trunk or routed
    access_vlan: int | None = None
    native_vlan: int | None = None
    allowed: list[int] | None = None  # None: all VLANs the device has
    lag: str = ""  # aggregation this port is a member of
    lag_protocol: str = ""
    addresses: list[str] = field(default_factory=list)  # routed port addresses
    helpers: list[str] = field(default_factory=list)


@dataclass
class Pool:
    """A DHCP pool served by the device."""

    name: str
    network: str = ""  # 10.0.10.0/24
    start: str = ""
    end: str = ""
    gateway: str = ""
    dns: list[str] = field(default_factory=list)
    domain: str = ""
    lease_seconds: int | None = None
    reservations: list[tuple[str, str, str]] = field(default_factory=list)  # mac, ip, name


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
    interface: str = ""  # when the route points at an interface or remote site


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
    vendor: str = ""  # parser that read it, for example "cisco"
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
    dns_servers: list[str] = field(default_factory=list)
    domain: str = ""
    pools: dict[str, Pool] = field(default_factory=dict)
    stack_kind: str = ""  # vsf or stack
    members: dict[int, Member] = field(default_factory=dict)
    secrets_skipped: int = 0
    unknown: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    names_left: int = field(default=MAX_NAMES, repr=False, compare=False)

    def expand(self, spec: str) -> list[str]:
        """:func:`expand`, counted against the budget of the whole configuration."""
        names = expand(spec)
        self.charge(len(names))
        return names

    def charge(self, count: int) -> None:
        """Count ``count`` port names (or memberships) against the budget."""
        self.names_left -= count
        if self.names_left < 0:
            raise TooLargeError(f"the configuration names more than {MAX_NAMES} ports")

    def interface(self, name: str) -> Interface:
        """The interface entry of ``name`` (created on first use)."""
        return self.interfaces.setdefault(name, Interface(name))


class ParseError(ValueError):
    """The text cannot be read as a configuration."""


class TooLargeError(ParseError):
    """The configuration names more ports than any switch has; reading stops."""


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


# --- Shared steps -----------------------------------------------------------------

# Long and short interface names of the common vendors, to match documented ports.
_ALIASES = (
    ("twentyfivegige", ("twe", "twentyfivegigabitethernet")),
    ("hundredgige", ("hu", "hundredgigabitethernet")),
    ("fortygigabitethernet", ("fo", "fortygige")),
    ("tengigabitethernet", ("te", "ten-gigabitethernet", "xge")),
    ("gigabitethernet", ("gi", "gig", "ge", "g")),
    ("fastethernet", ("fa", "f")),
    ("ethernet", ("eth", "et", "e")),
    ("port-channel", ("po", "bridge-aggregation", "eth-trunk", "trk", "lag", "ae", "bond")),
)
_SPLIT = re.compile(r"^([a-z][a-z-]*?)[\s-]?(\d.*)$")


def canonical(name: str) -> str:
    """A comparable form of an interface name: ``Gi1/0/1`` and ``GigabitEthernet1/0/1`` match."""
    text = name.strip().lower().replace(" ", "")
    match = _SPLIT.match(text)
    if match is None:
        return text
    prefix, rest = match.groups()
    for full, short in _ALIASES:
        if prefix == full or prefix in short:
            return f"{full}{rest}"
    return f"{prefix}{rest}"


def finish(config: Config) -> Config:
    """Turn per interface VLAN and aggregation settings into VLAN port lists and trunks.

    Every membership counts against the configuration's budget, and duplicates are
    found with sets, so the work stays linear in what the text describes.
    """
    seen = {
        (vid, tagged, name)
        for vid, vlan in config.vlans.items()
        for tagged, names in ((False, vlan.untagged), (True, vlan.tagged))
        for name in names
    }
    known = set(config.vlans)
    for trunk in config.trunks.values():  # the aggregation itself may name the protocol
        own = config.interfaces.get(trunk.name)
        if own is not None and own.lag_protocol:
            trunk.protocol = own.lag_protocol
    for port in config.interfaces.values():
        if port.lag:
            trunk = config.trunks.setdefault(
                port.lag, Trunk(port.lag, [], port.lag_protocol or "static")
            )
            if port.name not in trunk.members:
                trunk.members.append(port.name)
            own = config.interfaces.get(port.lag)
            if own is not None and own.lag_protocol:
                trunk.protocol = own.lag_protocol
            elif port.lag_protocol:
                trunk.protocol = port.lag_protocol
            continue  # members carry the aggregation's VLANs
        if port.mode == "access" and port.access_vlan:
            _member(config, seen, port.access_vlan, port.name, tagged=False)
        elif port.mode == "trunk":
            if port.native_vlan:
                _member(config, seen, port.native_vlan, port.name, tagged=False)
            allowed = known if port.allowed is None else set(port.allowed)
            for vid in sorted(allowed - {port.native_vlan}):
                _member(config, seen, vid, port.name, tagged=True)
    return config


def _member(
    config: Config, seen: set[tuple[int, bool, str]], vid: int, name: str, *, tagged: bool
) -> None:
    if not 1 <= vid <= 4094 or (vid, tagged, name) in seen:
        return
    config.charge(1)
    seen.add((vid, tagged, name))
    vlan = config.vlans.setdefault(vid, Vlan(vid))
    (vlan.tagged if tagged else vlan.untagged).append(name)


def mask_to_prefix(mask: str) -> int:
    """``255.255.255.0`` as 24 (also wildcard masks like ``0.0.0.255``)."""
    try:
        value = int(ipaddress.IPv4Address(mask))
    except ValueError as exc:
        raise ParseError(f"cannot read the mask {mask[:20]}") from exc
    if value and value & 1 and not value & 0x80000000:
        value ^= 0xFFFFFFFF  # wildcard mask
    bits = bin(value).count("1")
    if value != (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF:
        raise ParseError(f"cannot read the mask {mask[:20]}")
    return bits


def vid_list(spec: str) -> list[int]:
    """VLAN lists in any common spelling: ``10,20-30``, ``10 20 to 30``, ``all``."""
    text = spec.lower().replace(" to ", "-").replace(" ", ",")
    if text in ("all", "1-4094"):
        return list(range(1, 4095))
    return vids(",".join(p for p in text.split(",") if p and p not in ("add", "except")))


# Lines that carry secrets on any platform; they are counted, never read.
SECRET = re.compile(
    r"(password|passwd|secret|community|pre-?shared|psk|private-key|key-string|"
    r"\bkey\b|passphrase|ENC |hash|credential|authentication-key|md5|sha1|sha256)",
    re.I,
)


def lines_of(text: str) -> list[str]:
    """The lines of a configuration, with the size limits every parser shares."""
    if "\x00" in text:
        raise ParseError("the file is not a text configuration")
    if len(text) > MAX_BYTES:
        raise ParseError("the configuration is larger than 4 MB")
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if len(lines) > MAX_LINES:
        raise ParseError(f"the configuration has more than {MAX_LINES} lines")
    for number, line in enumerate(lines, 1):
        if len(line) > MAX_LINE:
            raise ParseError(f"line {number} is longer than {MAX_LINE} characters")
    return lines


def blocks(
    lines: list[str], comments: tuple[str, ...] = ("!",)
) -> list[tuple[str, list[str], int]]:
    """Top level commands with their indented sub-commands (``exit`` lines dropped)."""
    result: list[tuple[str, list[str], int]] = []
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(comments):
            continue
        if raw[0].isspace() and result:
            if stripped not in ("exit", "quit", "end"):
                result[-1][1].append(stripped)
            continue
        result.append((stripped, [], number))
    return result


def without_secrets(config: Config, body: list[str]) -> list[str]:
    """Body lines without secrets (each skipped line is counted)."""
    kept = []
    for line in body:
        if SECRET.search(line):
            config.secrets_skipped += 1
        else:
            kept.append(line)
    return kept


def address_of(words: list[str]) -> str:
    """``A.B.C.D M.M.M.M``, ``A.B.C.D/len`` or IPv6 ``x::y/len`` as ``address/len``."""
    if not words:
        raise ParseError("an address is missing")
    if "/" in words[0]:
        return _address(words[0], None)
    if len(words) > 1 and words[1][:1].isdigit():
        return _address(words[0], str(mask_to_prefix(words[1])))
    raise ParseError(f"cannot read the address {words[0][:40]}")


def network_of(words: list[str]) -> str:
    """A network in either spelling, normalised (host bits cleared)."""
    if "/" in words[0]:
        return _network(words[0], None)
    return _network(words[0], str(mask_to_prefix(words[1])) if len(words) > 1 else None)


def keywords(text: str) -> frozenset[str]:
    """A set of command words written as one string."""
    return frozenset(text.split())


def declare_vlans(
    config: Config, spec: str, body: list[str], keys: tuple[str, ...] = ("name",)
) -> None:
    """VLANs of a ``vlan`` block; its body is read once (a name only names a single VLAN)."""
    numbers = vid_list(spec)
    config.charge(len(numbers))
    name = ""
    for line in body:
        word, _sep, rest = line.partition(" ")
        if word in keys and rest:
            name = _unquote(rest)[:64]
    for vid in numbers:
        vlan = config.vlans.setdefault(vid, Vlan(vid))
        if name and len(numbers) == 1:
            vlan.name = name
