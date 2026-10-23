"""Managing webhooks and delivering their events."""

import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from datetime import timedelta
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager import __version__
from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.core import crypto
from patchmanager.history.models import Change
from patchmanager.integrations import http
from patchmanager.integrations.models import Delivery, Webhook
from patchmanager.tenancy import rls
from patchmanager.tenancy.models import Tenant

logger = logging.getLogger(__name__)
OPERATIONS = ("insert", "update", "delete")  # actions of history changes, the event suffix
PATTERN = re.compile(r"^(\*|[a-z]+(\.[a-z]+){0,2}(\.\*)?)$")
BACKOFF = (60, 300, 1800, 7200, 21600, 86400)  # seconds after attempt 1, 2, ...
LEASE = timedelta(minutes=5)  # longer than a batch can take (BATCH x total timeout)
BATCH = 10
SEND_WINDOW = (
    120  # seconds; no new request starts later, and the task is killed before the lease ends
)
KEEP_DAYS = 30


def sees_everything(user: Any, tenant: Tenant, events: list[str] | None = None) -> bool:
    """True if ``user`` may view every tracked object type in the whole tenant, without exceptions.

    Webhooks send every change with before and after values, so whoever sets one
    up must be able to see all of it: a tenant wide view grant per type and no
    deny rule anywhere inside the tenant. With ``events`` only the types those
    patterns can send count, so object types added later do not stop a webhook
    that never sends them.
    """
    from patchmanager.history.registry import TRACKED

    if user is None or not user.is_active:
        return False
    path = tenant.policy_path
    views = {
        f"{t.policy_type}.view"
        for label, t in TRACKED.items()
        if events is None
        or any(fnmatchcase(f"{label}.{op}", e) for e in events for op in OPERATIONS)
    }
    if not all(policy.has_perm(user, perm, scope_path=path) for perm in views):
        return False
    return not any(
        g.effect == "deny"
        and any(g.grants(p) for p in views)
        and (g.scope_path is None or g.covers(path) or g.scope_path.startswith(path + "."))
        for g in policy.grants_for(user)
    )


def require_manage(actor: User, tenant: Tenant) -> None:
    """Managing webhooks needs the manage grant for the tenant and sight of all its data."""
    allowed = policy.has_perm(actor, "integrations.webhook.manage", tenant)
    if not allowed or not sees_everything(actor, tenant):
        raise PermissionDeniedError(_("You do not have permission to do that."))


def _redacted(url: str) -> str:
    """Scheme and host only: paths and queries of webhook URLs often hold secrets."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}"


def _events(value: Any) -> list[str]:
    events = sorted({e.strip() for e in (value or []) if e.strip()})
    if not events or not all(PATTERN.match(e) for e in events):
        raise ServiceError(_("Events are patterns like dcim.device.*, cabling.cable.insert or *."))
    return events


def create(
    request: HttpRequest,
    actor: User,
    tenant: Tenant,
    *,
    name: str,
    url: str,
    events: Any,
    active: bool = True,
) -> tuple[Webhook, str]:
    """A webhook with a new signing secret (returned once)."""
    require_manage(actor, tenant)
    http.check(url)
    secret = secrets.token_urlsafe(32)
    hook = Webhook.objects.create(
        tenant=tenant,
        name=name.strip()[:100] or _redacted(url),
        url=url,
        active=active,
        encrypted_secret=crypto.encrypt(secret),
        events=_events(events),
        created_by=actor,
    )
    audit.record(
        "integrations.webhook.created",
        request=request,
        obj=hook,
        data={"url": _redacted(url), "events": hook.events},
    )
    return hook, secret


def update(
    request: HttpRequest,
    actor: User,
    hook: Webhook,
    *,
    name: str,
    url: str,
    events: Any,
    active: bool,
) -> Webhook:
    """Change a webhook (the secret stays)."""
    require_manage(actor, hook.tenant)
    http.check(url)
    hook.name, hook.url, hook.events, hook.active = (
        name.strip()[:100] or _redacted(url),
        url,
        _events(events),
        active,
    )
    hook.save(update_fields=["name", "url", "events", "active", "updated_at"])
    audit.record(
        "integrations.webhook.changed",
        request=request,
        obj=hook,
        data={"url": _redacted(url), "events": hook.events, "active": active},
    )
    return hook


def rotate(request: HttpRequest, actor: User, hook: Webhook) -> str:
    """A new signing secret."""
    require_manage(actor, hook.tenant)
    secret = secrets.token_urlsafe(32)
    Webhook.objects.filter(pk=hook.pk).update(encrypted_secret=crypto.encrypt(secret))
    audit.record("integrations.webhook.rotated", request=request, obj=hook)
    return secret


def delete(request: HttpRequest, actor: User, hook: Webhook) -> None:
    """Remove a webhook and its deliveries."""
    require_manage(actor, hook.tenant)
    audit.record(
        "integrations.webhook.deleted", request=request, obj=hook, data={"url": _redacted(hook.url)}
    )
    hook.delete()


def ping(request: HttpRequest, actor: User, hook: Webhook) -> Delivery:
    """Queue a test event."""
    require_manage(actor, hook.tenant)
    return Delivery.objects.create(
        tenant=hook.tenant, webhook=hook, event="ping", next_attempt_at=timezone.now()
    )


def redeliver(request: HttpRequest, actor: User, delivery: Delivery) -> None:
    """Send a delivery again, as soon as possible."""
    require_manage(actor, delivery.webhook.tenant)
    Delivery.objects.filter(pk=delivery.pk).update(
        status=Delivery.Status.PENDING, next_attempt_at=timezone.now()
    )


# ---------------------------------------------------------------- delivery


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """``v1`` signature: HMAC-SHA256 over ``<timestamp>.<body>``."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def payload(delivery: Delivery) -> dict[str, Any]:
    """What the receiver gets."""
    body: dict[str, Any] = {
        "id": str(delivery.pk),
        "event": delivery.event,
        "tenant": str(delivery.tenant_id),
    }
    change = Change.objects.filter(pk=delivery.change_id).first() if delivery.change_id else None
    if change is not None:
        body |= {
            "occurred_at": change.occurred_at.isoformat(),
            "object": {
                "type": change.object_type,
                "id": str(change.object_id),
                "label": change.label,
            },
            "action": change.action,
            "fields": change.fields,
            "before": change.before,
            "after": change.after,
            "actor": change.actor_label,
            "request_id": change.request_id,
        }
    return body


def _send(delivery: Delivery) -> None:
    hook = delivery.webhook
    body = json.dumps(payload(delivery), default=str, separators=(",", ":")).encode()
    secret = crypto.decrypt(hook.encrypted_secret)
    stamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"PatchManager-Webhook/{__version__}",
        "PatchManager-Event": delivery.event,
        "PatchManager-Delivery": str(delivery.pk),
        "PatchManager-Signature": f"t={stamp},v1={sign(secret, stamp, body)}",
    }
    attempts = delivery.attempts + 1
    code: int | None = None
    try:
        code = http.post(hook.url, body, headers)
    except ServiceError as exc:
        error = str(exc)[:300]
    else:
        error = "" if 200 <= code < 300 else f"HTTP {code}"
    now = timezone.now()
    if not error:
        update = {"status": Delivery.Status.SENT, "delivered_at": now}
    elif attempts > len(BACKOFF):
        update = {"status": Delivery.Status.FAILED}
    else:
        update = {"next_attempt_at": now + timedelta(seconds=BACKOFF[attempts - 1])}
    Delivery.objects.filter(pk=delivery.pk).update(
        attempts=attempts, last_status_code=code, last_error=error, **update
    )


def deliver_due(limit: int = BATCH) -> int:
    """Send pending deliveries that are due. Safe to run in parallel workers."""
    with rls.system_context():
        now = timezone.now()
        with transaction.atomic():  # claim a batch: other workers skip locked rows and leased ones
            batch = list(
                Delivery.objects.select_for_update(skip_locked=True)
                .filter(status=Delivery.Status.PENDING, next_attempt_at__lte=now)
                .order_by("next_attempt_at")[:limit]
            )
            Delivery.objects.filter(pk__in=[d.pk for d in batch]).update(
                next_attempt_at=now + LEASE
            )
        started = time.monotonic()
        allowed: dict[Any, bool] = {}
        claimed = Delivery.objects.filter(pk__in=[d.pk for d in batch])
        for delivery in claimed.select_related("webhook__created_by", "webhook__tenant"):
            if (
                time.monotonic() - started > SEND_WINDOW
            ):  # leave the rest to the next run, inside the lease
                Delivery.objects.filter(pk=delivery.pk).update(next_attempt_at=timezone.now())
                continue
            hook = delivery.webhook
            if hook.pk not in allowed:  # the creator must still see everything the webhook sends
                allowed[hook.pk] = sees_everything(hook.created_by, hook.tenant, hook.events)
            if not hook.active:
                Delivery.objects.filter(pk=delivery.pk).update(
                    status=Delivery.Status.FAILED, last_error="The webhook is inactive."
                )
            elif allowed[hook.pk]:
                try:
                    _send(delivery)
                except Exception:  # one broken delivery must not stop the others; it is retried
                    logger.exception(
                        "webhook delivery failed", extra={"delivery": str(delivery.pk)}
                    )
                    Delivery.objects.filter(pk=delivery.pk).update(
                        attempts=delivery.attempts + 1,
                        last_error="Internal error, see the server log.",
                        next_attempt_at=timezone.now() + timedelta(seconds=BACKOFF[0]),
                    )
            else:
                Delivery.objects.filter(pk=delivery.pk).update(
                    status=Delivery.Status.FAILED,
                    last_error="The creator of this webhook lost access.",
                )
        return len(batch)


def prune() -> int:
    """Forget deliveries older than 30 days."""
    with rls.system_context():
        deleted, _rows = Delivery.objects.filter(
            created_at__lt=timezone.now() - timedelta(days=KEEP_DAYS)
        ).delete()
        return deleted
