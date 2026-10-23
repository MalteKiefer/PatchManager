"""Fortinet FortiOS (FortiGate, FortiSwitch in FortiLink) full configurations."""

import re
import shlex
from typing import Any

from patchmanager.networks.platforms.common import (
    SECRET,
    Config,
    Neighbor,
    ParseError,
    Pool,
    Route,
    Vlan,
    _ip,
    address_of,
    finish,
    lines_of,
    network_of,
)

MAX_VALUE = 256 * 1024  # characters of one value spanning lines
MAX_DEPTH = 32
# FortiOS writes the any address for values that are not set (and for the default route).
UNSET = "0.0.0.0"  # noqa: S104  # nosec B104: a value, not a bind address


def detect(text: str) -> int:
    """How sure the text is a FortiOS configuration (0 to 100)."""
    score = 0
    if re.search(r"^#config-version=F", text, re.M):
        score += 90
    if re.search(r"^config system (global|interface)", text, re.M):
        score += 60
    if re.search(r"^\s+edit \"", text, re.M):
        score += 20
    return min(score, 100)


def _quote_state(text: str, state: str) -> str:
    """Whether ``text`` leaves a quote open ('' when not), continuing ``state`` (shlex rules)."""
    escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif char == "\\" and state != "'":
            escaped = True
        elif not state and char in "\"'":
            state = char
        elif char == state:
            state = ""
    return state


def tree(lines: list[str], config: Config) -> dict[str, Any]:
    """``config``/``edit``/``set`` blocks as nested dictionaries (secrets left out).

    Quoted values may span lines (certificates, keys, message texts): they are joined
    before the line is split, up to MAX_VALUE characters.
    """
    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    pending: list[str] = []
    state, start, size = "", 0, 0
    for number, raw in enumerate(lines, 1):
        if pending:
            pending.append(raw)
            size += len(raw)
            state = _quote_state("\n" + raw, state)
            if size > MAX_VALUE:
                raise ParseError(f"line {start}: unbalanced quotes")
            if state:
                continue
            line, pending = "\n".join(pending), []
        else:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            state, start, size = _quote_state(line, ""), number, len(line)
            if state:
                pending = [line]
                continue
        try:
            words = shlex.split(line, posix=True)
        except ValueError as exc:
            raise ParseError(f"line {start}: unbalanced quotes") from exc
        key = words[0]
        if key == "config":
            node = stack[-1].setdefault(" ".join(words[1:]), {})
            stack.append(node)
        elif key == "edit" and len(words) >= 2:
            node = stack[-1].setdefault(words[1], {})
            stack.append(node)
        elif key in ("next", "end"):
            if len(stack) > 1:
                stack.pop()
        elif key == "set" and len(words) >= 2:
            if SECRET.search(words[1]) or any(w.startswith("ENC ") for w in words):
                config.secrets_skipped += 1
                continue
            stack[-1][words[1]] = words[2:]
        elif key in ("unset", "append"):
            continue
        if len(stack) > MAX_DEPTH:
            raise ParseError(f"line {number}: blocks nested too deeply")
    if pending:
        raise ParseError(f"line {start}: unbalanced quotes")
    return root


def _one(node: dict[str, Any], key: str, default: str = "") -> str:
    value = node.get(key)
    return value[0] if isinstance(value, list) and value else default


def parse(text: str) -> Config:
    """Read a FortiOS configuration."""
    config = Config(vendor="fortios")
    lines = lines_of(text)
    for raw in lines[:2]:
        if raw.startswith("#config-version="):
            config.platform = raw[16:].split(":")[0][:200]
    root = tree(lines, config)
    system = root.get("system global", {})
    config.hostname = _one(system, "hostname")[:100]
    dns = root.get("system dns", {})
    config.dns_servers = [
        _ip(v) for v in (_one(dns, "primary"), _one(dns, "secondary")) if v and v != UNSET
    ]
    config.domain = _one(dns, "domain")[:253]
    for name, node in root.get("system interface", {}).items():
        try:
            _interface(config, name, node)
        except ParseError as exc:
            config.warnings.append(f"interface {name}: {exc}")
    for node in root.get("router static", {}).values():
        try:
            dst = node.get("dst", [UNSET, UNSET])
            gateway = _one(node, "gateway")
            config.routes.append(
                Route(network_of(dst), _ip(gateway) if gateway and gateway != UNSET else None)
            )
        except ParseError as exc:
            config.warnings.append(f"static route: {exc}")
    bgp = root.get("router bgp", {})
    if _one(bgp, "as").isdigit():
        config.bgp_asn = int(_one(bgp, "as"))
        if _one(bgp, "router-id"):
            config.bgp_router_id = _ip(_one(bgp, "router-id"))
        for address, node in bgp.get("neighbor", {}).items():
            if _one(node, "remote-as").isdigit():
                config.neighbors[_ip(address)] = Neighbor(
                    _ip(address),
                    int(_one(node, "remote-as")),
                    _one(node, "description")[:200],
                    _one(node, "shutdown", "disable") != "enable",
                )
    ospf = root.get("router ospf", {})
    if _one(ospf, "router-id") and _one(ospf, "router-id") != UNSET:
        config.ospf_router_id = _ip(_one(ospf, "router-id"))
    for node in root.get("system dhcp server", {}).values():
        try:
            _dhcp(config, node)
        except ParseError as exc:
            config.warnings.append(f"DHCP server: {exc}")
    config.unknown = [
        k
        for k in root
        if k
        not in (
            "system global",
            "system dns",
            "system interface",
            "router static",
            "router bgp",
            "router ospf",
            "system dhcp server",
        )
    ][:200]
    return finish(config)


def _interface(config: Config, name: str, node: dict[str, Any]) -> None:
    ip = node.get("ip", [])
    address = address_of(ip) if len(ip) >= 2 and ip[0] != UNSET else ""
    vid = _one(node, "vlanid")
    if vid.isdigit() and 1 <= int(vid) <= 4094:  # a VLAN on a parent port
        vlan = config.vlans.setdefault(int(vid), Vlan(int(vid), name=name[:64]))
        if address:
            vlan.addresses.append(address)
        parent = _one(node, "interface")
        if parent:
            port = config.interface(parent)
            port.mode = "trunk"
            config.allow(parent, [int(vid)])
        return
    kind = _one(node, "type", "physical")
    port = config.interface(name)
    port.label = (_one(node, "alias") or _one(node, "description"))[:100]
    port.enabled = _one(node, "status", "up") != "down"
    if address:
        port.mode = "routed"
        port.addresses.append(address)
    if kind == "aggregate":
        protocol = "lacp" if _one(node, "lacp-mode", "active") != "static" else "static"
        for member in node.get("member", []):
            config.interface(member).lag = name
            config.interface(member).lag_protocol = protocol
        port.lag_protocol = protocol


def _dhcp(config: Config, node: dict[str, Any]) -> None:
    interface = _one(node, "interface")
    pool = config.pools.setdefault(
        interface or f"pool{len(config.pools) + 1}", Pool(interface[:100])
    )
    gateway, mask = _one(node, "default-gateway"), _one(node, "netmask")
    if gateway and mask:
        pool.gateway = _ip(gateway)
        pool.network = network_of([gateway, mask])
    for item in node.get("ip-range", {}).values():
        pool.start, pool.end = _ip(_one(item, "start-ip")), _ip(_one(item, "end-ip"))
        break
    pool.dns = [
        _ip(_one(node, k)) for k in ("dns-server1", "dns-server2", "dns-server3") if _one(node, k)
    ]
    pool.domain = _one(node, "domain")[:253]
    lease = _one(node, "lease-time")
    pool.lease_seconds = int(lease) if lease.isdigit() and int(lease) >= 60 else pool.lease_seconds
    for item in node.get("reserved-address", {}).values():
        if _one(item, "ip") and _one(item, "mac"):
            pool.reservations.append(
                (_one(item, "mac").lower(), _ip(_one(item, "ip")), _one(item, "description")[:100])
            )
