"""LANCOM LCOS configuration scripts (``.lcs``, from LANconfig or ``readscript``).

Scripts set single values with ``set /Setup/<path> <value>`` and fill tables in
blocks::

    cd /Setup/TCP-IP/Network-list
    tab Network-name IP-Address IP-Netmask VLAN-ID Interface ...
    add "INTRANET" 192.168.1.1 255.255.255.0 0 any ...
    cd /

Columns are matched by the names in the ``tab`` line, never by position, because
their order differs between firmware versions.
"""

import ipaddress
import re
import shlex

from patchmanager.networks.platforms.common import (
    SECRET,
    Config,
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
)

_ANY = "0.0.0.0"  # noqa: S104  # nosec B104: LCOS writes it for "not set", not a bind address
_DEFAULT_ROUTE = ("255.255.255.255", "0.0.0.0")  # noqa: S104  # nosec B104: LCOS spelling of 0/0


def detect(text: str) -> int:
    """How sure the text is a LANCOM script (0 to 100)."""
    score = 0
    if re.search(r"^# Script \(", text, re.M):
        score += 70
    if re.search(r"^(set|cd) /Setup/", text, re.M):
        score += 50
    if re.search(r"^lang (English|Deutsch)", text, re.M):
        score += 20
    return min(score, 100)


def read(
    lines: list[str], config: Config
) -> tuple[dict[str, str], dict[str, list[dict[str, str]]]]:
    """Single values by path and table rows by path (secret values left out)."""
    values: dict[str, str] = {}
    tables: dict[str, list[dict[str, str]]] = {}
    current = "/"
    columns: list[str] = []
    for number, raw in enumerate(lines, 1):
        line = raw.lstrip("﻿").strip()
        if not line or line.startswith("#"):
            continue
        try:
            words = shlex.split(line, posix=True)
        except ValueError:
            config.warnings.append(f"line {number}: unbalanced quotes")
            continue
        verb = words[0].lower()
        if verb == "lang" and len(words) == 2 and words[1].lower() != "english":
            raise ParseError("only scripts in English (lang English) can be read")
        if verb == "cd" and len(words) == 2:
            target = words[1]
            current = target if target.startswith("/") else f"{current.rstrip('/')}/{target}"
            if re.fullmatch(r"/[\d/]+", current):
                raise ParseError("numeric paths (readscript -n) cannot be read; export with names")
            columns = []
        elif verb == "tab":
            columns = [c.lower() for c in words[1:]]
        elif verb == "add" and columns:
            row = {}
            for column, value in zip(columns, words[1:], strict=False):
                if SECRET.search(column) or value == "*":
                    config.secrets_skipped += SECRET.search(column) is not None
                    continue
                row[column] = value
            tables.setdefault(current.lower().rstrip("/"), []).append(row)
        elif verb == "set" and len(words) >= 3 and "{" not in line:
            path = words[1] if words[1].startswith("/") else f"{current.rstrip('/')}/{words[1]}"
            if SECRET.search(path):
                config.secrets_skipped += 1
                continue
            values[path.lower()] = " ".join(words[2:])
        elif verb == "set" and "{" in line:
            config.unknown.append(line[:120])  # row edits in brace syntax are not read
    return values, tables


def parse(text: str) -> Config:
    """Read a LANCOM LCOS script."""
    config = Config(vendor="lancom")
    lines = lines_of(text)
    for raw in lines[:3]:
        if raw.lstrip("﻿").startswith("# Script ("):
            config.platform = "LCOS " + raw.split("(", 1)[1].split("/")[0].strip()[:40]
    values, tables = read(lines, config)
    config.hostname = values.get("/setup/name", "")[:100]
    config.dns_servers = [
        _ip(v)
        for k in ("/setup/tcp-ip/dns-default", "/setup/tcp-ip/dns-backup")
        if (v := values.get(k, "")) and v != _ANY
    ]
    config.domain = values.get("/setup/dns/domain", "")[:253]
    for row in tables.get("/setup/tcp-ip/network-list", []):
        try:
            _network(config, row)
        except TooLargeError:
            raise  # the whole text is refused, not one line
        except ParseError as exc:
            config.warnings.append(f"network {row.get('network-name', '')}: {exc}")
    _vlans(config, tables)
    for row in tables.get("/setup/ip-router/ip-routing-table", []):
        try:
            config.routes.append(_route(row))
        except TooLargeError:
            raise  # the whole text is refused, not one line
        except ParseError as exc:
            config.warnings.append(f"route: {exc}")
    networks = {
        r.get("network-name", "").lower(): r for r in tables.get("/setup/tcp-ip/network-list", [])
    }
    for row in tables.get("/setup/dhcp/network-list", []):
        try:
            _dhcp(config, row, networks)
        except TooLargeError:
            raise  # the whole text is refused, not one line
        except ParseError as exc:
            config.warnings.append(f"DHCP {row.get('network-name', '')}: {exc}")
    index = config.pool_index()
    for row in tables.get("/setup/dhcp/hosts", []):
        try:
            _host(config, row, index)
        except TooLargeError:
            raise  # the whole text is refused, not one line
        except ParseError as exc:
            config.warnings.append(f"DHCP host: {exc}")
    known = {
        "/setup/tcp-ip/network-list",
        "/setup/vlan/networks",
        "/setup/vlan/port-table",
        "/setup/ip-router/ip-routing-table",
        "/setup/dhcp/network-list",
        "/setup/dhcp/hosts",
    }
    config.unknown += [path for path in tables if path not in known][:200]
    return finish(config)


def _network(config: Config, row: dict[str, str]) -> None:
    address, mask = row.get("ip-address", ""), row.get("ip-netmask", "")
    if not address or address == _ANY:
        return
    interface_address = address_of([address, mask])
    vid = row.get("vlan-id", "0")
    if vid.isdecimal() and 1 <= int(vid) <= 4094:
        vlan = config.vlans.setdefault(int(vid), Vlan(int(vid)))
        vlan.name = vlan.name or row.get("network-name", "")[:64]
        vlan.addresses.append(interface_address)
        return
    interface = row.get("interface", "any")
    if interface.lower() != "any":
        port = config.interface(interface)
        port.mode = "routed"
        port.label = port.label or row.get("network-name", "")[:100]
        port.addresses.append(interface_address)
    else:
        config.warnings.append(
            f"network {row.get('network-name', '')} ({interface_address}) is on no single port"
        )


def _vlans(config: Config, tables: dict[str, list[dict[str, str]]]) -> None:
    """VLAN networks name their ports; the port table says how each port tags."""
    modes = {}
    for row in tables.get("/setup/vlan/port-table", []):
        port = row.get("port", "")
        mode = row.get("tagging-mode", "").lower()
        pvid = row.get("port-vlan-id", "")
        if not port:
            continue
        # Ingress-mixed (the default) accepts tags but never sends them: untagged only.
        untagged = mode in ("access", "never", "ingress-mixed")
        modes[port.lower()] = "access" if untagged else "trunk"
        interface = config.interface(port)
        interface.mode = modes[port.lower()]
        if pvid.isdecimal() and 1 <= int(pvid) <= 4094:
            if interface.mode == "access":
                interface.access_vlan = int(pvid)
            else:
                interface.native_vlan = int(pvid)
    for row in tables.get("/setup/vlan/networks", []):
        vid = row.get("vlan-id", "")
        if not vid.isdecimal() or not 1 <= int(vid) <= 4094:
            continue
        vlan = config.vlans.setdefault(int(vid), Vlan(int(vid)))
        vlan.name = vlan.name or row.get("name", "")[:64]
        for port in filter(None, (p.strip() for p in row.get("ports", "").split(","))):
            interface = config.interface(port)
            if modes.get(port.lower(), "trunk") == "trunk":
                interface.mode = "trunk"
                config.allow(port, [int(vid)])
            else:
                interface.access_vlan = int(vid)


def _route(row: dict[str, str]) -> Route:
    address, mask = row.get("ip-address", ""), row.get("ip-netmask", "")
    prefix = "0.0.0.0/0" if (address, mask) == _DEFAULT_ROUTE else network_of([address, mask])
    peer = row.get("peer-or-ip", "")
    distance = row.get("admin-distance", "")
    try:
        next_hop = str(ipaddress.ip_address(peer))
        interface = ""
    except ValueError:
        next_hop, interface = None, peer[:100]
    return Route(
        prefix,
        next_hop,
        int(distance) if distance.isdecimal() and int(distance) <= 255 else None,
        interface,
    )


def _dhcp(config: Config, row: dict[str, str], networks: dict[str, dict[str, str]]) -> None:
    name = row.get("network-name", "")[:100]
    start = row.get("start-address-pool") or row.get("start", "")
    end = row.get("end-address-pool") or row.get("end", "")
    if not start or start == _ANY:
        return
    pool = config.pools.setdefault(name, Pool(name))
    pool.start, pool.end = _ip(start), _ip(end)
    network = networks.get(name.lower(), {})
    mask = row.get("netmask", "") or network.get("ip-netmask", "")
    if mask and mask != _ANY:
        pool.network = network_of([pool.start, mask])
    gateway = row.get("gateway-address") or row.get("gateway", "")
    pool.gateway = _ip(gateway) if gateway and gateway != _ANY else network.get("ip-address", "")
    pool.dns = [_ip(row[k]) for k in ("dns-default", "dns-backup") if row.get(k) and row[k] != _ANY]
    lease = row.get("def.-lease", "")
    pool.lease_seconds = int(lease) * 60 if lease.isdecimal() and int(lease) else None  # minutes


def _host(config: Config, row: dict[str, str], index: dict[str, Pool]) -> None:
    mac = re.sub(r"[^0-9a-f]", "", row.get("mac-address", "").lower())
    ip = row.get("ip-address", "")
    if len(mac) != 12 or not ip:
        return
    pool = config.pools.get(row.get("network-name", "")[:100]) or config.pool_for(ip, index)
    if pool is not None:
        pool.reservations.append(
            (
                ":".join(mac[i : i + 2] for i in range(0, 12, 2)),
                _ip(ip),
                row.get("hostname", "")[:100],
            )
        )
