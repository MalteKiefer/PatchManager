"""Outgoing HTTP for webhooks, guarded against server side request forgery.

The host is resolved once, every address is checked, and the connection goes to
the checked address (TLS still verifies the hostname), so a DNS answer cannot
change between the check and the request. Redirects are not followed.
"""

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import urllib3
from django.utils.translation import gettext as _

from patchmanager.accounts.services import ServiceError
from patchmanager.instance import config as instance_config

TIMEOUT = urllib3.Timeout(total=10, connect=5)  # total: a slow sender cannot hold a worker
NAT64 = ipaddress.ip_network("64:ff9b::/96")


@dataclass(frozen=True)
class Target:
    """A checked destination."""

    scheme: str
    host: str
    port: int
    path: str
    address: str


def _embedded(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    """IPv4 addresses an IPv6 address leads to (mapped, 6to4, Teredo, NAT64)."""
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    inner = [ip.ipv4_mapped, ip.sixtofour, *(ip.teredo or ())]
    if ip in NAT64:
        inner.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return [a for a in inner if a is not None]


def _allowed(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    networks = instance_config.get().allowed_networks
    allowed = [ipaddress.ip_network(net, strict=False) for net in networks]
    return all(
        any(candidate in net for net in allowed if net.version == candidate.version)
        or candidate.is_global
        for candidate in [ip, *_embedded(ip)]
    )


def check(url: str) -> Target:
    """Validate ``url`` and resolve it to an address that may be contacted."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or (
        parts.scheme == "http" and not instance_config.get().webhook_allow_http
    ):
        raise ServiceError(_("Webhook URLs must use https."))
    if not parts.hostname or parts.username or parts.password:
        raise ServiceError(_("Enter a URL with a host and without credentials."))
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ServiceError(_("Enter a valid port.")) from exc
    try:
        answers = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise ServiceError(_("The host cannot be resolved.")) from exc
    addresses = sorted({str(a[4][0]) for a in answers})
    if not addresses or not all(_allowed(a) for a in addresses):
        raise ServiceError(_("This address is not allowed for webhooks."))
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return Target(parts.scheme, parts.hostname, port, path, addresses[0])


def post(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST ``body`` and return the status code. Raises ServiceError on network problems."""
    target = check(url)
    host_header = target.host if target.port in (80, 443) else f"{target.host}:{target.port}"
    if target.scheme == "https":
        pool: urllib3.HTTPConnectionPool = urllib3.HTTPSConnectionPool(
            target.address,
            target.port,
            timeout=TIMEOUT,
            retries=False,
            maxsize=1,
            server_hostname=target.host,
            assert_hostname=target.host,
            cert_reqs="CERT_REQUIRED",
        )
    else:
        pool = urllib3.HTTPConnectionPool(
            target.address, target.port, timeout=TIMEOUT, retries=False, maxsize=1
        )
    try:
        response = pool.urlopen(
            "POST",
            target.path,
            body=body,
            headers={**headers, "Host": host_header},
            redirect=False,
            preload_content=False,
        )
        response.read(1024)  # the body is not needed; do not keep reading
        response.release_conn()
        return int(response.status)
    except urllib3.exceptions.HTTPError as exc:
        raise ServiceError(type(exc).__name__) from exc
    finally:
        pool.close()
