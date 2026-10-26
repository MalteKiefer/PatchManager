"""Read a UniFi OS console backup (``.unifi``) into what PatchManager documents.

The backup is AES-256-CBC with a static key (the first 16 bytes are the IV); inside is a
gzip'd tar whose ``network/db.gz`` is the UniFi Network database as a stream of BSON
documents, ``{"__cmd": "select", "collection": ...}`` before the rows of each collection.
Ubiquiti does not document the format; the key is the one public tools use.

Only sites, devices, networks, WLANs and clients are read, and only the fields listed
here: passwords, keys and every ``x_`` field are never kept.
"""

import io
import ipaddress
import struct
import tarfile
import zlib
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.utils.translation import gettext as _

KEY = bytes.fromhex("e383b7c53698b36d4baea4ed22181ef73676bfd5d5b90005d9845ffd5dce985f")
MAX_FILE = 64 * 1024 * 1024  # the backup as uploaded
MAX_ARCHIVE = 512 * 1024 * 1024  # the tar after decompression
MAX_DATABASE = 256 * 1024 * 1024  # network/db.gz after decompression
MAX_ROWS = 100_000  # rows read from the collections below
COLLECTIONS = ("site", "device", "networkconf", "wlanconf", "user")


class ReadError(ValueError):
    """The file is not a UniFi OS backup this reader understands."""


@dataclass
class Network:
    """A network (``networkconf``): a VLAN with its subnet and DHCP server."""

    id: str
    site: str
    name: str
    purpose: str  # corporate, guest, wan, remote-user-vpn, ...
    vid: int | None  # None: not a LAN; 1: the untagged default network
    subnet: str = ""  # gateway with prefix length, 192.168.1.1/24
    dhcp: bool = False
    dhcp_start: str = ""
    dhcp_stop: str = ""
    dns: list[str] = field(default_factory=list)
    domain: str = ""
    lease_seconds: int | None = None


@dataclass
class Wlan:
    """A wireless network (``wlanconf``), without its passphrase."""

    site: str
    ssid: str
    security: str  # open, wpa2-personal, ... as in networks.WLAN.Security
    network: str = ""  # Network.id
    hidden: bool = False


@dataclass
class PortOverride:
    """Settings of one switch port that differ from the default (all networks)."""

    native: str = ""  # Network.id carried untagged
    tagged: str = "auto"  # auto: all other networks, block_all: none, custom: all but excluded
    excluded: list[str] = field(default_factory=list)
    name: str = ""
    enabled: bool = True


@dataclass
class Device:
    """An adopted UniFi device."""

    site: str
    mac: str
    kind: str  # usw, uap, udm, uxg, ugw, ...
    model: str
    name: str = ""
    serial: str = ""
    ip: str = ""
    ports: int = 0  # highest port index
    overrides: dict[int, PortOverride] = field(default_factory=dict)
    uplink_mac: str = ""
    uplink_port: int | None = None  # port on this device
    uplink_remote_port: int | None = None  # port on the device it is plugged into

    @property
    def is_gateway(self) -> bool:
        """Gateways route the networks and serve DHCP."""
        return self.kind in ("udm", "uxg", "ugw")


@dataclass
class Client:
    """A client with a fixed address."""

    site: str
    mac: str
    ip: str
    name: str = ""


@dataclass
class Backup:
    """What a backup documents, by UniFi site id."""

    sites: dict[str, str] = field(default_factory=dict)  # id: description
    networks: list[Network] = field(default_factory=list)
    wlans: list[Wlan] = field(default_factory=list)
    devices: list[Device] = field(default_factory=list)
    clients: list[Client] = field(default_factory=list)

    def only(self, site: str) -> Backup:
        """The part of the backup that belongs to one site."""
        return Backup(
            sites={site: self.sites.get(site, site)},
            networks=[n for n in self.networks if n.site == site],
            wlans=[w for w in self.wlans if w.site == site],
            devices=[d for d in self.devices if d.site == site],
            clients=[c for c in self.clients if c.site == site],
        )

    def busiest_site(self) -> str:
        """The site with the most devices (most backups hold one real site)."""
        counts = {s: sum(d.site == s for d in self.devices) for s in self.sites}
        return max(counts, key=lambda s: counts[s], default="")


def read(data: bytes) -> Backup:
    """Decrypt and read a ``.unifi`` backup."""
    if len(data) > MAX_FILE:
        raise ReadError(_("The backup is larger than 64 MB."))
    if len(data) < 32:
        raise ReadError(_("This is not a UniFi OS backup."))
    body = data[16:]
    body = body[: len(body) - len(body) % 16]
    decryptor = Cipher(algorithms.AES(KEY), modes.CBC(data[:16])).decryptor()
    plain = decryptor.update(body) + decryptor.finalize()
    if plain[:2] != b"\x1f\x8b":
        raise ReadError(_("This is not a UniFi OS backup, or it uses an unknown encryption."))
    archive = _gunzip(plain, MAX_ARCHIVE)  # one gzip member, then CBC padding
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            member = next(
                (m for m in tar.getmembers() if m.isfile() and m.name.endswith("network/db.gz")),
                None,
            )
            handle = tar.extractfile(member) if member is not None else None
            if handle is None:
                raise ReadError(_("The backup holds no UniFi Network database."))
            database = _gunzip(handle.read(), MAX_DATABASE)
    except tarfile.TarError as exc:
        raise ReadError(_("The backup archive is damaged.")) from exc
    return parse(database)


def _gunzip(data: bytes, limit: int) -> bytes:
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        out = inflater.decompress(data, limit)
    except zlib.error as exc:
        raise ReadError(_("The backup archive is damaged.")) from exc
    if inflater.unconsumed_tail:
        raise ReadError(_("The backup is too large to read."))
    return out


def parse(database: bytes) -> Backup:
    """Read the rows of the collections PatchManager uses from a BSON stream."""
    rows: dict[str, list[dict[str, Any]]] = {name: [] for name in COLLECTIONS}
    collection = None
    total = 0
    offset = 0
    try:
        while offset < len(database):
            (length,) = struct.unpack_from("<i", database, offset)
            if length < 5 or offset + length > len(database):
                raise ReadError(_("The UniFi Network database is damaged."))
            doc = _document(database, offset)
            offset += length
            if "__cmd" in doc:
                collection = doc.get("collection")
                continue
            if collection in rows:
                total += 1
                if total > MAX_ROWS:
                    raise ReadError(_("The backup holds too many entries."))
                rows[collection].append(doc)
    except (struct.error, IndexError, UnicodeDecodeError, RecursionError) as exc:
        raise ReadError(_("The UniFi Network database is damaged.")) from exc
    return _backup(rows)


def _size(buf: bytes, i: int, end: int, minimum: int, extra: int = 4) -> int:
    """A length field at ``i``, checked to fit before ``end``: a wrong one is damage, not a loop."""
    size: int = struct.unpack_from("<i", buf, i)[0]
    if size < minimum or i + extra + size > end:
        raise ReadError(_("The UniFi Network database is damaged."))
    return size


def _document(buf: bytes, i: int, limit: int | None = None) -> dict[str, Any]:
    length = _size(buf, i, len(buf) if limit is None else limit, 5, extra=0)
    end = i + length - 1
    i += 4
    out: dict[str, Any] = {}
    while i < end:
        kind = buf[i]
        name_end = buf.index(0, i + 1)
        key = buf[i + 1 : name_end].decode()
        i = name_end + 1
        value: Any = None
        if kind == 0x01:
            (value,) = struct.unpack_from("<d", buf, i)
            i += 8
        elif kind in (0x02, 0x0D, 0x0E):
            size = _size(buf, i, end, 1)
            value = buf[i + 4 : i + 3 + size].decode()
            i += 4 + size
        elif kind in (0x03, 0x04):
            size = _size(buf, i, end, 5, extra=0)
            value = _document(buf, i, end)
            if kind == 0x04:
                value = list(value.values())
            i += size
        elif kind == 0x05:
            i += 5 + _size(buf, i, end, 0, extra=5)
        elif kind == 0x07:
            value = buf[i : i + 12].hex()
            i += 12
        elif kind == 0x08:
            value = buf[i] == 1
            i += 1
        elif kind in (0x09, 0x11, 0x12):
            (value,) = struct.unpack_from("<q", buf, i)
            i += 8
        elif kind == 0x10:
            (value,) = struct.unpack_from("<i", buf, i)
            i += 4
        elif kind == 0x13:
            i += 16
        elif kind not in (0x06, 0x0A, 0x7F, 0xFF):
            raise ReadError(_("The UniFi Network database is damaged."))
        if not key.startswith("x_"):  # x_ fields are secrets: passwords, keys
            out[key] = value
    return out


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return value.strip() if isinstance(value, str) else ""


def _int(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _mac(value: str) -> str:
    return value.lower().replace("-", ":")


def _backup(rows: dict[str, list[dict[str, Any]]]) -> Backup:
    backup = Backup()
    for row in rows["site"]:
        if _text(row, "_id"):
            backup.sites[_text(row, "_id")] = _text(row, "desc") or _text(row, "name")
    for row in rows["networkconf"]:
        backup.networks.append(_network(row))
    for row in rows["wlanconf"]:
        if _text(row, "name"):
            backup.wlans.append(_wlan(row))
    for row in rows["device"]:
        if _text(row, "mac") and row.get("adopted", True):
            backup.devices.append(_device(row))
    for row in rows["user"]:
        ip = _text(row, "fixed_ip")
        if row.get("use_fixedip") and ip and _text(row, "mac"):
            backup.clients.append(
                Client(
                    site=_text(row, "site_id"),
                    mac=_mac(_text(row, "mac")),
                    ip=ip,
                    name=_text(row, "name") or _text(row, "hostname"),
                )
            )
    return backup


def _network(row: dict[str, Any]) -> Network:
    purpose = _text(row, "purpose")
    vid = None
    if purpose in ("corporate", "guest"):
        vid = _int(row, "vlan") if row.get("vlan_enabled") else 1
    dns = [_text(row, f"dhcpd_dns_{n}") for n in range(1, 5) if row.get("dhcpd_dns_enabled")]
    return Network(
        id=_text(row, "_id"),
        site=_text(row, "site_id"),
        name=_text(row, "name"),
        purpose=purpose,
        vid=vid,
        subnet=_text(row, "ip_subnet"),
        dhcp=bool(row.get("dhcpd_enabled")),
        dhcp_start=_text(row, "dhcpd_start"),
        dhcp_stop=_text(row, "dhcpd_stop"),
        dns=[d for d in dns if d],
        domain=_text(row, "domain_name"),
        lease_seconds=_int(row, "dhcpd_leasetime"),
    )


def _wlan(row: dict[str, Any]) -> Wlan:
    security = _text(row, "security")
    wpa3 = bool(row.get("wpa3_support")) and not row.get("wpa3_transition")
    if security == "open":
        kind = "open"
    elif security == "wpaeap":
        kind = "wpa3-enterprise" if wpa3 else "wpa2-enterprise"
    else:
        kind = "wpa3-personal" if wpa3 else "wpa2-personal"
    return Wlan(
        site=_text(row, "site_id"),
        ssid=_text(row, "name")[:32],
        security=kind,
        network=_text(row, "networkconf_id"),
        hidden=bool(row.get("hide_ssid")),
    )


def _device(row: dict[str, Any]) -> Device:
    ports = [p for p in row.get("port_table") or [] if isinstance(p, dict)]
    overrides = {}
    for item in row.get("port_overrides") or []:
        idx = _int(item, "port_idx") if isinstance(item, dict) else None
        if idx is None:
            continue
        overrides[idx] = PortOverride(
            native=_text(item, "native_networkconf_id"),
            tagged=_text(item, "tagged_vlan_mgmt") or "auto",
            excluded=[x for x in item.get("excluded_networkconf_ids") or [] if isinstance(x, str)],
            name=_text(item, "name")[:100],
            enabled=_text(item, "forward") != "disabled",
        )
    raw_uplink = row.get("last_uplink")
    uplink: dict[str, Any] = raw_uplink if isinstance(raw_uplink, dict) else {}
    wired = uplink.get("type") != "wireless"
    return Device(
        site=_text(row, "site_id"),
        mac=_mac(_text(row, "mac")),
        kind=_text(row, "type"),
        model=_text(row, "model")[:150],
        name=_text(row, "name")[:100],
        serial=_text(row, "serial")[:100],
        ip=_text(row, "ip"),
        ports=max([_int(p, "port_idx") or 0 for p in ports], default=0),
        overrides=overrides,
        uplink_mac=_mac(_text(uplink, "uplink_mac")) if wired else "",
        uplink_port=_int(uplink, "port_idx") if wired else None,
        uplink_remote_port=_int(uplink, "uplink_remote_port") if wired else None,
    )


def prefix_of(subnet: str) -> str:
    """192.168.1.1/24 is the network 192.168.1.0/24 (empty when it is not one)."""
    try:
        return str(ipaddress.ip_interface(subnet).network)
    except ValueError:
        return ""
