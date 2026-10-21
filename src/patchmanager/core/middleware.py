"""Request correlation id, propagated to logs and audit events."""

import re
import secrets
from collections.abc import Callable

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
