"""Client address behind the proxy, and a request correlation id for logs and audit events."""

import contextlib
import ipaddress
import re
import secrets
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

_VALID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestIDMiddleware:
    """Accept a sane ``X-Request-ID`` from the proxy or create one."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Standard middleware constructor."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Attach ``request.request_id`` and echo it in the response."""
        incoming = request.headers.get("X-Request-ID", "")
        request.request_id = incoming if _VALID.match(incoming) else secrets.token_hex(8)  # type: ignore[attr-defined]
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id  # type: ignore[attr-defined]
        return response


class ClientIPMiddleware:
    """Replace the proxy's address with the client address it reports.

    Rate limits and the audit log key on ``REMOTE_ADDR``. Behind Caddy that is the
    proxy for every request, so take the rightmost ``X-Forwarded-For`` entry (the
    peer the proxy saw), but only when the request comes from a trusted proxy.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Parse the trusted proxy networks once."""
        self.get_response = get_response
        allowed = settings.FORWARDED_ALLOW_IPS
        self.any_proxy = "*" in allowed
        self.proxies = [ipaddress.ip_network(a, strict=False) for a in allowed if a != "*"]

    def _trusted(self, peer: str) -> bool:
        if self.any_proxy:
            return True
        try:
            address = ipaddress.ip_address(peer)
        except ValueError:
            return False
        return any(address in network for network in self.proxies)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Rewrite ``REMOTE_ADDR`` for requests that came through a trusted proxy."""
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded and self._trusted(request.META.get("REMOTE_ADDR", "")):
            candidate = forwarded.rsplit(",", 1)[-1].strip()
            # A malformed header keeps the proxy address rather than trusting it.
            with contextlib.suppress(ValueError):
                request.META["REMOTE_ADDR"] = str(ipaddress.ip_address(candidate))
        return self.get_response(request)
