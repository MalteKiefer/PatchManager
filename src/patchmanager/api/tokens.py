"""Creating, checking and revoking API credentials.

Tokens look like ``pmat_<prefix>_<secret>`` (personal) or ``pmoa_...`` (OAuth).
The secret has 256 bits of entropy, so a plain SHA-256 is a sufficient hash;
the prefix is stored to find the row without scanning.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.api.models import ApiClient, ApiToken
from patchmanager.audit import services as audit
from patchmanager.audit.services import client_ip

PERSONAL_PREFIX = "pmat"
OAUTH_PREFIX = "pmoa"
CLIENT_PREFIX = "pmc"
MAX_DAYS = 365
OAUTH_SECONDS = 3600
VALID_SCOPES = {"read", "write"}
LAST_USED_EVERY = timedelta(minutes=5)


def digest(secret: str) -> str:
    """Hash of a token or client secret."""
    return hashlib.sha256(secret.encode()).hexdigest()


def _new(kind: str) -> tuple[str, str]:
    prefix = secrets.token_hex(6)
    return prefix, f"{kind}_{prefix}_{secrets.token_urlsafe(32)}"


def _scopes(values: Any) -> list[str]:
    scopes = sorted(set(values or []))
    if not scopes or not set(scopes) <= VALID_SCOPES:
        raise ServiceError(_("Choose read, write or both."))
    return scopes


def create_token(
    request: HttpRequest, actor: User, *, name: str, scopes: Any, days: int, tenant: Any = None
) -> tuple[ApiToken, str]:
    """A personal token for ``actor``. The plain token is returned once."""
    if actor.is_service:
        raise PermissionDeniedError(_("Service accounts use their client credentials."))
    if not name.strip():
        raise ServiceError(_("Enter a name."))
    if not 1 <= days <= MAX_DAYS:
        raise ServiceError(_("Tokens expire after 1 to %(n)s days.") % {"n": MAX_DAYS})
    prefix, plain = _new(PERSONAL_PREFIX)
    token = ApiToken.objects.create(
        user=actor,
        name=name.strip()[:100],
        prefix=prefix,
        token_hash=digest(plain),
        scopes=_scopes(scopes),
        tenant=tenant,
        expires_at=timezone.now() + timedelta(days=days),
    )
    audit.record(
        "api.token.created",
        request=request,
        obj=actor,
        data={"token": token.prefix, "scopes": token.scopes, "days": days},
    )
    return token, plain


def revoke_token(request: HttpRequest, actor: User, token: ApiToken) -> None:
    """Revoke one of the actor's tokens (or any, for identity admins)."""
    if token.user_id != actor.pk and not policy.has_perm(actor, "accounts.user.change"):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    if token.revoked_at is None:
        ApiToken.objects.filter(pk=token.pk).update(revoked_at=timezone.now())
        audit.record(
            "api.token.revoked", request=request, obj=token.user, data={"token": token.prefix}
        )


def authenticate(plain: str, request: HttpRequest | None = None) -> ApiToken | None:
    """The usable token for ``plain``, or None. Constant time on the hash."""
    parts = plain.split("_", 2)  # the secret itself may contain underscores
    if len(parts) != 3 or parts[0] not in (PERSONAL_PREFIX, OAUTH_PREFIX):
        return None
    token = ApiToken.objects.select_related("user", "client").filter(prefix=parts[1]).first()
    if (
        token is None
        or not hmac.compare_digest(token.token_hash, digest(plain))
        or not token.usable
    ):
        return None
    if token.client is not None and token.client.revoked_at is not None:
        return None
    now = timezone.now()
    if token.last_used_at is None or now - token.last_used_at > LAST_USED_EVERY:
        ApiToken.objects.filter(pk=token.pk).update(
            last_used_at=now, last_used_ip=client_ip(request)
        )
    return token


# ---------------------------------------------------------------- OAuth clients


def _require_manage(actor: User) -> None:
    if not policy.has_perm(actor, "accounts.apiclient.manage"):
        raise PermissionDeniedError(_("You do not have permission to do that."))


def create_client(
    request: HttpRequest, actor: User, *, name: str, scopes: Any, tenant: Any = None
) -> tuple[ApiClient, str]:
    """A client with its own service user (give it roles like any user).

    The secret is returned once.
    """
    _require_manage(actor)
    if not name.strip():
        raise ServiceError(_("Enter a name."))
    client_id = f"{CLIENT_PREFIX}_{secrets.token_hex(10)}"
    secret = secrets.token_urlsafe(32)
    with transaction.atomic():
        user = User.objects.create_user(
            email=f"{client_id}@clients.invalid",
            display_name=name.strip()[:150],
            password=None,
            is_service=True,
        )
        client = ApiClient.objects.create(
            name=name.strip()[:100],
            client_id=client_id,
            secret_hash=digest(secret),
            user=user,
            scopes=_scopes(scopes),
            tenant=tenant,
            created_by=actor,
        )
    audit.record(
        "api.client.created",
        request=request,
        obj=user,
        data={"client": client_id, "scopes": client.scopes},
    )
    return client, secret


def rotate_secret(request: HttpRequest, actor: User, client: ApiClient) -> str:
    """A new secret; the old one and all access tokens stop working."""
    _require_manage(actor)
    secret = secrets.token_urlsafe(32)
    with transaction.atomic():
        ApiClient.objects.filter(pk=client.pk).update(secret_hash=digest(secret))
        client.tokens.filter(revoked_at__isnull=True).update(revoked_at=timezone.now())
    audit.record(
        "api.client.rotated", request=request, obj=client.user, data={"client": client.client_id}
    )
    return secret


def revoke_client(request: HttpRequest, actor: User, client: ApiClient) -> None:
    """Disable a client and its service user for good."""
    _require_manage(actor)
    now = timezone.now()
    with transaction.atomic():
        ApiClient.objects.filter(pk=client.pk).update(revoked_at=now)
        client.tokens.filter(revoked_at__isnull=True).update(revoked_at=now)
        User.objects.filter(pk=client.user_id).update(is_active=False, deactivated_at=now)
    audit.record(
        "api.client.revoked", request=request, obj=client.user, data={"client": client.client_id}
    )


def issue_access_token(
    request: HttpRequest, client_id: str, secret: str, scope: str | None
) -> tuple[ApiToken, str]:
    """Client credentials grant (RFC 6749 section 4.4)."""
    client = ApiClient.objects.select_related("user").filter(client_id=client_id).first()
    if (
        client is None
        or not hmac.compare_digest(client.secret_hash, digest(secret))
        or client.revoked_at is not None
        or not client.user.is_active
    ):
        raise PermissionDeniedError("invalid_client")
    wanted = set(scope.split()) if scope else set(client.scopes)
    if not wanted or not wanted <= set(client.scopes):
        raise ServiceError("invalid_scope")
    prefix, plain = _new(OAUTH_PREFIX)
    token = ApiToken.objects.create(
        user=client.user,
        client=client,
        kind=ApiToken.Kind.OAUTH,
        name=client.name,
        prefix=prefix,
        token_hash=digest(plain),
        scopes=sorted(wanted),
        tenant=client.tenant,
        expires_at=timezone.now() + timedelta(seconds=OAUTH_SECONDS),
    )
    ApiClient.objects.filter(pk=client.pk).update(last_used_at=timezone.now())
    ApiToken.objects.filter(
        client=client, expires_at__lt=timezone.now() - timedelta(days=1)
    ).delete()
    return token, plain
