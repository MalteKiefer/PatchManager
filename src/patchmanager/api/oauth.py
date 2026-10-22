"""OAuth 2.0 token endpoint for the client credentials grant (RFC 6749 section 4.4)."""

import base64
import binascii
from urllib.parse import unquote

from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.api import tokens
from patchmanager.audit.services import client_ip
from patchmanager.core import ratelimit

ATTEMPTS_PER_IP = 30  # per 10 minutes


def _error(code: str, status: int = 400) -> JsonResponse:
    response = JsonResponse({"error": code}, status=status)
    if code == "invalid_client":
        response["WWW-Authenticate"] = 'Basic realm="oauth"'
    return _no_store(response)


def _no_store(response: JsonResponse) -> JsonResponse:
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def _credentials(request: HttpRequest) -> tuple[str, str] | None:
    """client_secret_basic (preferred) or client_secret_post."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode()
        except binascii.Error, UnicodeDecodeError:
            return None
        client_id, _, secret = decoded.partition(":")
        return unquote(client_id), unquote(secret)  # form encoded per RFC 6749 section 2.3.1
    if "client_id" in request.POST and "client_secret" in request.POST:
        return request.POST["client_id"], request.POST["client_secret"]
    return None


@csrf_exempt  # no cookies involved: the client authenticates with its own credentials
@require_POST
def token(request: HttpRequest) -> JsonResponse:
    """Exchange client credentials for a one hour bearer token."""
    if not ratelimit.hit(f"oauth:{client_ip(request)}", limit=ATTEMPTS_PER_IP, window=600):
        return _error("slow_down", 429)
    from patchmanager.instance import editions

    if not editions.enabled("api"):
        return _error("unauthorized_client", 403)
    if request.POST.get("grant_type") != "client_credentials":
        return _error("unsupported_grant_type")
    credentials = _credentials(request)
    if credentials is None:
        return _error("invalid_client", 401)
    try:
        access, plain = tokens.issue_access_token(request, *credentials, request.POST.get("scope"))
    except PermissionDeniedError:
        return _error("invalid_client", 401)
    except ServiceError:
        return _error("invalid_scope")
    return _no_store(
        JsonResponse(
            {
                "access_token": plain,
                "token_type": "Bearer",  # nosec B105: RFC 6749 token type, not a password
                "expires_in": tokens.OAUTH_SECONDS,
                "scope": " ".join(access.scopes),
            }
        )
    )
