"""MikroTik RouterOS exports (``/export``), version 6 and 7."""

import ipaddress
import re
import shlex

from patchmanager.networks.platforms.common import (
    MAX_LINE,
    MAX_NAME,
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
    vid_list,
)

_FIND = re.compile(r"\[\s{0,8}find\s{1,8}([^\]]{0,200})\]")


def _names(spec: str) -> list[str]:
    """A comma separated port list, without duplicates; names are at most 64 characters."""
    names = list(dict.fromkeys(filter(None, (n.strip() for n in spec.split(",")))))
    if any(len(n) > MAX_NAME for n in names):
        raise ParseError("a port name is longer than 64 characters")
    return names


def detect(text: str) -> int:
    """How sure the text is a RouterOS export (0 to 100)."""
    score = 0
    if re.search(r"^# .*by RouterOS", text, re.M):
        score += 90
    if re.search(r"^/(interface|ip address|system identity)", text, re.M):
        score += 70
    return min(score, 100)


def statements(lines: list[str]) -> list[tuple[str, str, dict[str, str], int]]:
    """(menu, verb, key=value pairs, line) for every ``add`` and ``set`` command."""
    joined: list[tuple[str, int]] = []
    parts: list[str] = []
    size = 0
    start = 1
    for number, raw in enumerate(lines, 1):
        piece = raw.strip()
        if not parts:
            start = number
        size += len(piece)
        if size > MAX_LINE * 4:
            raise ParseError(
                f"line {start}: a continued command is longer than {MAX_LINE * 4} characters"
            )
        if piece.endswith("\\"):
            parts.append(piece[:-1])
            continue
        parts.append(piece)
        joined.append(("".join(parts), start))
        parts, size = [], 0
    result = []
    menu = ""
    for line, number in joined:
        if not line or line.startswith("#"):
            continue
        if line.startswith("/"):  # a menu, alone or followed by a command
            tokens = line.split()
            verb = next(
                (i for i, t in enumerate(tokens) if t in ("add", "set", "remove")), len(tokens)
            )
            menu = " ".join(tokens[:verb])
            if verb == len(tokens):
                continue
            line = " ".join(tokens[verb:])
        try:
            words = shlex.split(_FIND.sub(r"find:\1", line))
        except ValueError:
            continue
        if not words or words[0] not in ("add", "set"):
            continue
        pairs = {}
        for word in words[1:]:
            key, sep, value = word.partition("=")
            if sep:
                pairs[key] = value
        result.append((menu, words[0], pairs, number))
    return result


def parse(text: str) -> Config:
    """Read a RouterOS export."""
    config = Config(vendor="routeros")
    pools: dict[str, tuple[str, str]] = {}
    dhcp_servers: dict[str, str] = {}  # server name -> pool name
    names: dict[str, str] = {}  # default-name -> name
    for menu, verb, pairs, number in statements(lines_of(text)):
        if any(SECRET.search(k) or SECRET.search(v) for k, v in pairs.items()):
            config.secrets_skipped += 1  # the whole command, as on every platform
            continue
        try:
            if menu == "/system identity" and "name" in pairs:
                config.hostname = pairs["name"][:100]
            elif menu == "/interface ethernet" and verb == "set":
                name = pairs.get("name") or pairs.get("find:default-name", "")
                if name:
                    port = config.interface(name)
                    port.label = pairs.get("comment", "")[:100]
                    port.enabled = pairs.get("disabled", "no") != "yes"
            elif menu == "/interface vlan" and "vlan-id" in pairs:
                vid = int(pairs["vlan-id"])
                config.vlans.setdefault(vid, Vlan(vid, name=pairs.get("name", "")[:64]))
                names[pairs.get("name", "")] = f"vlan:{vid}"
            elif (menu == "/interface bonding" and "slaves" in pairs) or (
                menu == "/interface bonding" and "ports" in pairs
            ):
                protocol = "lacp" if pairs.get("mode") == "802.3ad" else "static"
                for member in (pairs.get("slaves") or pairs.get("ports", "")).split(","):
                    config.interface(member).lag = pairs.get("name", "bond")
                    config.interface(member).lag_protocol = protocol
            elif menu == "/interface bridge port" and "interface" in pairs:
                pvid = pairs.get("pvid", "")
                if pvid.isdigit() and pvid != "1":
                    port = config.interface(pairs["interface"])
                    port.mode, port.access_vlan = port.mode or "access", int(pvid)
            elif menu == "/interface bridge vlan" and "vlan-ids" in pairs:
                tagged = _names(pairs.get("tagged", ""))
                untagged = _names(pairs.get("untagged", ""))
                vlan_ids = vid_list(pairs["vlan-ids"])
                config.charge(len(vlan_ids) * (len(tagged) + len(untagged)))
                for vid in vlan_ids:
                    vlan = config.vlans.setdefault(vid, Vlan(vid))
                    have = set(vlan.tagged) | set(vlan.untagged)
                    vlan.tagged += [
                        m for m in tagged if m not in have and not m.startswith("bridge")
                    ]
                    vlan.untagged += [m for m in untagged if m not in have]
            elif menu == "/ip address" and "address" in pairs:
                target = pairs.get("interface", "")
                address = address_of([pairs["address"]])
                vlan_key = names.get(target, "")
                if vlan_key.startswith("vlan:"):
                    config.vlans[int(vlan_key[5:])].addresses.append(address)
                else:
                    port = config.interface(target)
                    port.mode = "routed"
                    port.addresses.append(address)
            elif menu == "/ip route" and "gateway" in pairs:
                hop = pairs["gateway"].split("%")[0]
                distance = int(pairs["distance"]) if pairs.get("distance", "").isdigit() else None
                config.routes.append(
                    Route(
                        network_of([pairs.get("dst-address", "0.0.0.0/0")]),
                        _ip(hop) if hop[:1].isdigit() else None,
                        distance,
                    )
                )
            elif menu == "/ip dns" and "servers" in pairs:
                config.dns_servers = [_ip(s) for s in pairs["servers"].split(",") if s]
            elif menu == "/ip pool" and "ranges" in pairs:
                first, _sep, last = pairs["ranges"].split(",")[0].partition("-")
                pools[pairs.get("name", "")] = (_ip(first), _ip(last or first))
            elif menu == "/ip dhcp-server" and verb == "add":
                dhcp_servers[pairs.get("name", "")] = pairs.get("address-pool", "")
            elif menu == "/ip dhcp-server network" and "address" in pairs:
                _network(config, pairs, pools)
            elif menu == "/ip dhcp-server lease" and "address" in pairs and "mac-address" in pairs:
                config.pools.setdefault("leases", Pool("leases")).reservations.append(
                    (
                        pairs["mac-address"].lower(),
                        _ip(pairs["address"]),
                        pairs.get("comment", "")[:100],
                    )
                )
            elif menu in ("/routing bgp connection", "/routing bgp peer") and (
                "remote.address" in pairs or "remote-address" in pairs
            ):
                address = _ip(
                    (pairs.get("remote.address") or pairs["remote-address"]).split("/")[0]
                )
                remote = pairs.get("remote.as") or pairs.get("remote-as", "")
                if remote.isdigit():
                    config.neighbors[address] = Neighbor(
                        address, int(remote), pairs.get("comment", "")[:200]
                    )
                local = pairs.get("as") or pairs.get("local.as", "")
                if local.isdigit():
                    config.bgp_asn = int(local)
            elif (
                menu in ("/routing bgp instance", "/routing bgp template")
                and pairs.get("as", "").isdigit()
            ):
                config.bgp_asn = int(pairs["as"])
                if pairs.get("router-id"):
                    config.bgp_router_id = _ip(pairs["router-id"])
            elif menu in ("/routing ospf instance",) and pairs.get("router-id", "")[:1].isdigit():
                config.ospf_router_id = _ip(pairs["router-id"])
        except (ParseError, ValueError, KeyError) as exc:
            config.warnings.append(f"line {number}: {exc}")
    _leases(config)
    return finish(config)


def _network(config: Config, pairs: dict[str, str], pools: dict[str, tuple[str, str]]) -> None:
    network = network_of([pairs["address"]])
    pool = config.pools.setdefault(network, Pool(network))
    pool.network = network
    if pairs.get("gateway"):
        pool.gateway = _ip(pairs["gateway"])
    pool.dns = [_ip(s) for s in pairs.get("dns-server", "").split(",") if s]
    pool.domain = pairs.get("domain", "")[:253]
    for start, end in pools.values():
        if ipaddress.ip_address(start) in ipaddress.ip_network(network):
            pool.start, pool.end = start, end


def _leases(config: Config) -> None:
    """Static leases join the pool whose network holds their address."""
    leases = config.pools.pop("leases", None)
    if leases is None:
        return
    for mac, ip, name in leases.reservations:
        for pool in config.pools.values():
            if pool.network and ipaddress.ip_address(ip) in ipaddress.ip_network(pool.network):
                pool.reservations.append((mac, ip, name))
                break
