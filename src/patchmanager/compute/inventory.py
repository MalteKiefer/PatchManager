"""Read clusters, virtual machines and storage from Proxmox VE, VMware vCenter, Synology and QNAP.

The address comes from the user, so every request goes through :class:`Client`:
https only, the host is resolved once and every address checked (no loopback,
link local such as cloud metadata, multicast or unspecified addresses; private
networks are allowed, since hypervisors live there), the connection goes to the
checked address, redirects are not followed, and answers are limited in time and
size. The certificate is verified, or pinned by its SHA-256 fingerprint for self
signed certificates. Credentials are used for the one import and never stored.
"""

import base64
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import urllib3
from defusedxml import ElementTree as SafeET
from django.utils.translation import gettext as _

from patchmanager.accounts.services import ServiceError

MAX_BYTES = 20 * 1024 * 1024
MAX_GUESTS = 5000
MAX_REQUESTS = MAX_GUESTS + 100
DEADLINE = 120  # seconds for a whole import
TIMEOUT = urllib3.Timeout(total=30, connect=10)
_FINGERPRINT = re.compile(r"^([0-9a-f]{2}:){31}[0-9a-f]{2}$|^[0-9a-f]{64}$")


@dataclass
class Guest:
    """A virtual machine or container as the platform reports it."""

    external_id: str
    name: str
    kind: str = "vm"  # vm or container
    status: str = "running"
    host: str = ""
    vcpus: int | None = None
    memory_mb: int | None = None
    disk_gb: int | None = None
    operating_system: str = ""
    nics: list[dict[str, str]] = field(default_factory=list)  # name, mac, bridge, vlan, addresses
    disks: list[tuple[str, int, str]] = field(default_factory=list)  # name, size GB, storage


@dataclass
class Storage:
    """A datastore, pool, volume or share."""

    name: str
    kind: str = "datastore"
    size_gb: int | None = None
    used_gb: int | None = None
    target: str = ""
    raid: str = ""


@dataclass
class Inventory:
    """What a platform reports."""

    platform: str
    cluster: str = ""
    hosts: list[str] = field(default_factory=list)
    guests: list[Guest] = field(default_factory=list)
    storage: list[Storage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    inner = [ip]
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        inner.append(ip.ipv4_mapped)
    return any(
        a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified or a.is_reserved
        for a in inner
    )


class Client:
    """HTTPS requests to one checked host."""

    def __init__(self, url: str, fingerprint: str = "") -> None:
        """Check the address and how its certificate is trusted."""
        parts = urlsplit(url.strip())
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise ServiceError(_("Enter an https address without credentials."))
        try:
            self.port = parts.port or 443
        except ValueError as exc:
            raise ServiceError(_("Enter a valid port.")) from exc
        self.host = parts.hostname
        try:
            answers = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise ServiceError(_("The host cannot be resolved.")) from exc
        addresses = sorted({str(a[4][0]) for a in answers})
        if not addresses or any(_blocked(ipaddress.ip_address(a)) for a in addresses):
            raise ServiceError(_("This address cannot be used."))
        self.address = addresses[0]
        pin = fingerprint.strip().lower()
        if pin and not _FINGERPRINT.match(pin):
            raise ServiceError(_("Enter the SHA-256 fingerprint of the certificate."))
        options: dict[str, Any] = {"server_hostname": self.host, "assert_hostname": self.host}
        if pin:
            options = {
                "server_hostname": self.host,
                "assert_fingerprint": pin,
                "cert_reqs": "CERT_NONE",
            }
        else:
            options["cert_reqs"] = "CERT_REQUIRED"
        self.deadline = time.monotonic() + DEADLINE
        self.requests = 0
        self.pool = urllib3.HTTPSConnectionPool(
            self.address, self.port, timeout=TIMEOUT, retries=False, maxsize=2, **options
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> tuple[int, Any]:
        """Send a request and return the status and the JSON answer."""
        status, data = self.fetch(method, path, headers=headers, body=body)
        try:
            return status, json.loads(data) if data else None
        except ValueError as exc:
            raise ServiceError(_("The host did not answer with JSON.")) from exc

    def fetch(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: str | None = None,
    ) -> tuple[int, bytes]:
        """Send a request and return the status and the raw answer."""
        self.requests += 1
        if time.monotonic() > self.deadline or self.requests > MAX_REQUESTS:
            raise ServiceError(_("The platform is too large or too slow to read in one go."))
        host = self.host if self.port == 443 else f"{self.host}:{self.port}"
        try:
            response = self.pool.urlopen(
                method,
                path,
                body=body,
                headers={"Host": host, "Accept": "application/json", **(headers or {})},
                redirect=False,
                preload_content=False,
            )
            data = response.read(MAX_BYTES + 1)
            response.release_conn()
        except urllib3.exceptions.SSLError as exc:
            raise ServiceError(
                _("The certificate could not be verified; enter its fingerprint.")
            ) from exc
        except urllib3.exceptions.HTTPError as exc:
            raise ServiceError(_("The host could not be reached.")) from exc
        if len(data) > MAX_BYTES:
            raise ServiceError(_("The answer is too large."))
        if response.status in (401, 403):
            raise ServiceError(_("The host refused the credentials."))
        return int(response.status), data

    def get(self, path: str, headers: dict[str, str] | None = None) -> Any:
        """GET JSON, raising on errors."""
        status, data = self.request("GET", path, headers=headers)
        if status != 200:
            raise ServiceError(
                _("The host answered with an error (%(status)s).") % {"status": status}
            )
        return data


def _gb(value: Any, unit: int) -> int | None:
    return int(value) // unit if isinstance(value, int | float) and value >= 0 else None


# --- Proxmox VE --------------------------------------------------------------------


def _pve_nic(key: str, spec: str, kind: str) -> dict[str, str]:
    values = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
    mac = values.get("hwaddr", "")
    if kind == "qemu":  # "virtio=AA:BB:..,bridge=vmbr0,tag=10"
        mac = next((v for k, v in values.items() if re.fullmatch(r"[0-9A-Fa-f:]{17}", v)), "")
    return {
        "name": values.get("name", key)[:64],
        "mac": mac.lower(),
        "bridge": values.get("bridge", "")[:100],
        "vlan": values.get("tag", ""),
        "addresses": values.get("ip", "") if values.get("ip", "") not in ("dhcp", "manual") else "",
    }


def proxmox(url: str, token: str, fingerprint: str = "") -> Inventory:
    """A Proxmox VE cluster through its API (token ``user@realm!id=secret``)."""
    if not re.fullmatch(r"[\w.@-]+![\w.-]+=[0-9a-fA-F-]{8,64}", token.strip()):
        raise ServiceError(_("Enter the API token as user@realm!tokenid=secret."))
    client = Client(url, fingerprint)
    headers = {"Authorization": f"PVEAPIToken={token.strip()}"}
    inventory = Inventory("proxmox")
    for item in (client.get("/api2/json/cluster/status", headers) or {}).get("data", []):
        if item.get("type") == "cluster":
            inventory.cluster = str(item.get("name", ""))[:100]
        elif item.get("type") == "node":
            inventory.hosts.append(str(item.get("name", ""))[:150])
    resources = (client.get("/api2/json/cluster/resources", headers) or {}).get("data", [])
    for item in resources:
        if item.get("type") in ("qemu", "lxc") and len(inventory.guests) < MAX_GUESTS:
            kind = str(item["type"])
            guest = Guest(
                external_id=str(item.get("vmid", "")),
                name=str(item.get("name", ""))[:150],
                kind="container" if kind == "lxc" else "vm",
                status="template"
                if item.get("template")
                else ("running" if item.get("status") == "running" else "stopped"),
                host=str(item.get("node", ""))[:150],
                vcpus=item.get("maxcpu"),
                memory_mb=_gb(item.get("maxmem"), 1024 * 1024),
                disk_gb=_gb(item.get("maxdisk"), 1024**3),
            )
            node, vmid = quote(guest.host, safe=""), quote(guest.external_id, safe="")
            config = (
                client.get(f"/api2/json/nodes/{node}/{kind}/{vmid}/config", headers) or {}
            ).get("data", {})
            guest.operating_system = str(config.get("ostype", ""))[:150]
            for key, spec in config.items():
                if re.fullmatch(r"net\d+", key):
                    guest.nics.append(_pve_nic(key, str(spec), kind))
                elif re.fullmatch(r"(scsi|virtio|sata|ide|mp|rootfs)\d*", key) and "size=" in str(
                    spec
                ):
                    storage = str(spec).split(":", 1)[0]
                    size = re.search(r"size=(\d+)([GTM])", str(spec))
                    if size:
                        gb = int(size.group(1)) * {"G": 1, "T": 1024, "M": 0}[size.group(2)] or 1
                        guest.disks.append((key, gb, storage[:150]))
            inventory.guests.append(guest)
        elif item.get("type") == "storage":
            name = str(item.get("storage", ""))[:150]
            if not any(s.name == name for s in inventory.storage):
                inventory.storage.append(
                    Storage(
                        name,
                        "datastore",
                        _gb(item.get("maxdisk"), 1024**3),
                        _gb(item.get("disk"), 1024**3),
                        str(item.get("plugintype", ""))[:300],
                    )
                )
    inventory.cluster = inventory.cluster or (inventory.hosts[0] if inventory.hosts else "")
    return inventory


# --- VMware vCenter -----------------------------------------------------------------


def vmware(url: str, username: str, password: str, fingerprint: str = "") -> Inventory:
    """A vCenter through its REST API (vSphere 7.0 U2 or newer)."""
    import base64

    client = Client(url, fingerprint)
    basic = base64.b64encode(f"{username}:{password}".encode()).decode()
    status, session = client.request(
        "POST", "/api/session", headers={"Authorization": f"Basic {basic}"}
    )
    if status not in (200, 201) or not isinstance(session, str):
        raise ServiceError(_("The host refused the credentials."))
    headers = {"vmware-api-session-id": session}
    inventory = Inventory("vmware")
    try:
        clusters = client.get("/api/vcenter/cluster", headers) or []
        inventory.cluster = str(clusters[0].get("name", ""))[:100] if clusters else ""
        hosts = client.get("/api/vcenter/host", headers) or []
        inventory.hosts = [str(h.get("name", ""))[:150] for h in hosts]
        for host in hosts:
            vms = (
                client.get(f"/api/vcenter/vm?{urlencode({'hosts': host.get('host', '')})}", headers)
                or []
            )
            for vm in vms[: MAX_GUESTS - len(inventory.guests)]:
                guest = Guest(
                    external_id=str(vm.get("vm", ""))[:100],
                    name=str(vm.get("name", ""))[:150],
                    status="running" if vm.get("power_state") == "POWERED_ON" else "stopped",
                    host=str(host.get("name", ""))[:150],
                    vcpus=vm.get("cpu_count"),
                    memory_mb=vm.get("memory_size_MiB"),
                )
                detail = (
                    client.get(f"/api/vcenter/vm/{quote(guest.external_id, safe='')}", headers)
                    or {}
                )
                guest.operating_system = str(detail.get("guest_OS", ""))[:150]
                for key, nic in (detail.get("nics") or {}).items():
                    guest.nics.append(
                        {
                            "name": str(nic.get("label", key))[:64],
                            "mac": str(nic.get("mac_address", "")).lower(),
                            "bridge": str((nic.get("backing") or {}).get("network_name", ""))[:100],
                            "vlan": "",
                            "addresses": "",
                        }
                    )
                total = 0
                for key, disk in (detail.get("disks") or {}).items():
                    gb = _gb(disk.get("capacity"), 1024**3) or 0
                    total += gb
                    guest.disks.append((str(disk.get("label", key))[:64], gb, ""))
                guest.disk_gb = total or None
                inventory.guests.append(guest)
        for store in client.get("/api/vcenter/datastore", headers) or []:
            size, free = store.get("capacity"), store.get("free_space")
            used = size - free if isinstance(size, int) and isinstance(free, int) else None
            inventory.storage.append(
                Storage(
                    str(store.get("name", ""))[:150],
                    "datastore",
                    _gb(size, 1024**3),
                    _gb(used, 1024**3),
                    str(store.get("type", ""))[:300],
                )
            )
    finally:
        client.request("DELETE", "/api/session", headers=headers)
    return inventory


# --- Synology DSM -------------------------------------------------------------------


def synology(url: str, username: str, password: str, fingerprint: str = "") -> Inventory:
    """Volumes, storage pools and shared folders of a Synology NAS (DSM 7)."""
    client = Client(url, fingerprint)
    status, login = client.request(
        "POST",
        "/webapi/entry.cgi",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urlencode(
            {
                "api": "SYNO.API.Auth",
                "version": "6",
                "method": "login",
                "account": username,
                "passwd": password,
                "format": "sid",
            }
        ),
    )
    sid = (
        ((login or {}).get("data") or {}).get("sid")
        if status == 200 and (login or {}).get("success")
        else None
    )
    if not sid:
        raise ServiceError(_("The host refused the credentials."))
    inventory = Inventory("synology")
    try:

        def call(api: str, method: str, version: str = "1", **extra: str) -> dict[str, Any]:
            query = urlencode(
                {"api": api, "version": version, "method": method, "_sid": sid, **extra}
            )
            data = client.get(f"/webapi/entry.cgi?{query}") or {}
            return data.get("data") or {} if data.get("success") else {}

        storage = call("SYNO.Storage.CGI.Storage", "load_info")
        for pool in storage.get("storagePools", []):
            inventory.storage.append(
                Storage(
                    str(pool.get("id", "pool"))[:150],
                    "pool",
                    _gb(int((pool.get("size") or {}).get("total", 0) or 0), 1024**3),
                    _gb(int((pool.get("size") or {}).get("used", 0) or 0), 1024**3),
                    raid=str(pool.get("device_type", ""))[:30],
                )
            )
        for volume in storage.get("volumes", []):
            inventory.storage.append(
                Storage(
                    str(volume.get("vol_path", volume.get("id", "volume")))[:150],
                    "volume",
                    _gb(int((volume.get("size") or {}).get("total", 0) or 0), 1024**3),
                    _gb(int((volume.get("size") or {}).get("used", 0) or 0), 1024**3),
                )
            )
        for share in call("SYNO.Core.Share", "list", "1").get("shares", []):
            inventory.storage.append(
                Storage(
                    str(share.get("name", ""))[:150],
                    "smb",
                    target=str(share.get("vol_path", ""))[:300],
                )
            )
    finally:
        logout = {"api": "SYNO.API.Auth", "version": "6", "method": "logout", "_sid": sid}
        client.request("GET", f"/webapi/entry.cgi?{urlencode(logout)}")
    return inventory


# --- QNAP QTS / QuTS hero -----------------------------------------------------------


def _qnap_xml(client: Client, method: str, path: str, body: str | None = None) -> Any:
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else None
    status, data = client.fetch(method, path, headers=headers, body=body)
    if status != 200:
        raise ServiceError(_("The host answered with an error (%(status)s).") % {"status": status})
    try:
        return SafeET.fromstring(data)
    except (SafeET.ParseError, ValueError) as exc:
        raise ServiceError(_("The host did not answer with XML.")) from exc


def _text(node: Any, tag: str) -> str:
    return (node.findtext(tag) or "").strip()


def _bytes_gb(value: str) -> int | None:
    return _gb(int(value), 1024**3) if value.isdigit() else None


def qnap(url: str, username: str, password: str, fingerprint: str = "") -> Inventory:
    """Volumes and shared folders of a QNAP NAS (QTS 4.x/5.x, QuTS hero)."""
    client = Client(url, fingerprint)
    # QTS expects the password base64 encoded; sent in the body, not the URL.
    secret = base64.b64encode(password.encode()).decode()
    login = _qnap_xml(
        client,
        "POST",
        "/cgi-bin/authLogin.cgi",
        urlencode({"user": username, "pwd": secret, "serviceKey": "1"}),
    )
    sid = _text(login, "authSid")
    if _text(login, "authPassed") != "1" or not sid:
        raise ServiceError(
            _("The host refused the credentials (accounts with two step sign in cannot be used).")
        )
    inventory = Inventory("qnap")
    try:
        query = urlencode(
            {"chart_func": "disk_usage", "disk_select": "all", "include": "all", "sid": sid}
        )
        usage = _qnap_xml(client, "GET", f"/cgi-bin/management/chartReq.cgi?{query}")
        labels = {
            _text(v, "volumeValue"): _text(v, "volumeLabel")
            for v in usage.iterfind("volumeList/volume")
        }
        for use in usage.iterfind("volumeUseList/volumeUse"):
            index = _text(use, "volumeValue")
            name = (labels.get(index) or f"Volume {index}")[:150]
            total = _bytes_gb(_text(use, "total_size"))
            free = _bytes_gb(_text(use, "free_size"))
            used = total - free if total is not None and free is not None else None
            inventory.storage.append(Storage(name, "volume", total, used))
            for folder in use.iterfind("folder_element"):
                share = _text(folder, "sharename")[:150]
                if share:
                    inventory.storage.append(
                        Storage(
                            share,
                            "smb",
                            used_gb=_bytes_gb(_text(folder, "used_size")),
                            target=name[:300],
                        )
                    )
    finally:
        client.fetch("GET", f"/cgi-bin/authLogout.cgi?{urlencode({'sid': sid})}")
    return inventory
