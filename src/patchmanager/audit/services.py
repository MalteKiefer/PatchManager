"""Write and verify the audit chain.

``hash = sha256(prev_hash || canonical JSON of the event)``. Writers serialise
on a transaction level advisory lock, so the chain stays linear even with many
workers. The genesis event links to 64 zeros.
"""

import hashlib
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from django.db import connection, transaction
from django.http import HttpRequest
from django.utils import timezone

from patchmanager.audit.models import AuditEvent

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
_LOCK_ID = 0x504D_4155_4449_54  # "PMAUDIT"
_HASHED_FIELDS = (
    "seq",
    "occurred_at",
    "action",
    "actor_id",
    "actor_label",
    "tenant_id",
    "object_type",
    "object_id",
    "object_label",
    "data",
    "ip_address",
    "user_agent",
    "request_id",
)


def _canonical(event: AuditEvent) -> bytes:
    payload = {name: getattr(event, name) for name in _HASHED_FIELDS}
    payload["occurred_at"] = event.occurred_at.isoformat()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def compute_hash(event: AuditEvent) -> str:
    """Chain hash of ``event`` given its ``prev_hash``."""
    return hashlib.sha256(event.prev_hash.encode() + _canonical(event)).hexdigest()


def client_ip(request: HttpRequest | None) -> str | None:
    """Client address as seen by Django (proxy headers handled by gunicorn)."""
    return request.META.get("REMOTE_ADDR") if request else None


_deferred: ContextVar[list[tuple[str, dict[str, Any]]] | None] = ContextVar(
    "audit_deferred", default=None
)


@contextmanager
def deferred() -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Collect events instead of writing them, so a long batch does not hold the chain lock.

    The caller writes the collected events with :func:`flush` (or drops them).
    """
    events: list[tuple[str, dict[str, Any]]] = []
    token = _deferred.set(events)
    try:
        yield events
    finally:
        _deferred.reset(token)


def flush(events: list[tuple[str, dict[str, Any]]]) -> None:
    """Write events collected by :func:`deferred`."""
    for action, kwargs in events:
        record(action, **kwargs)


def record(
    action: str,
    *,
    actor: Any = None,
    request: HttpRequest | None = None,
    obj: Any = None,
    tenant_id: Any = None,
    data: dict[str, Any] | None = None,
) -> AuditEvent | None:
    """Append one event. Runs in its own atomic block and fails loudly."""
    pending = _deferred.get()
    if pending is not None:
        pending.append(
            (
                action,
                {
                    "actor": actor,
                    "request": request,
                    "obj": obj,
                    "tenant_id": tenant_id,
                    "data": data,
                },
            )
        )
        return None
    if actor is None and request is not None and request.user.is_authenticated:
        actor = request.user
    impersonator = getattr(request, "impersonator", None)
    if impersonator is not None and getattr(actor, "pk", None) != impersonator.pk:
        # Acting as someone else: keep who really did it next to whom it counts for.
        data = {**(data or {}), "impersonator": str(impersonator.pk)}
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK_ID])
            cursor.execute("SELECT nextval(pg_get_serial_sequence('audit_auditevent', 'seq'))")
            seq = cursor.fetchone()[0]
        last = AuditEvent.objects.order_by("-seq").values_list("hash", flat=True).first()
        event = AuditEvent(
            seq=seq,
            occurred_at=timezone.now(),
            action=action,
            actor_id=getattr(actor, "pk", None),
            actor_label=str(actor) if actor is not None else "",
            tenant_id=tenant_id if tenant_id is not None else getattr(obj, "tenant_id", None),
            object_type=getattr(obj, "policy_object_type", "") if obj is not None else "",
            object_id=str(obj.pk) if obj is not None else "",
            object_label=str(obj)[:300] if obj is not None else "",
            data=data or {},
            ip_address=client_ip(request),
            user_agent=request.headers.get("User-Agent", "")[:300] if request else "",
            request_id=getattr(request, "request_id", "") if request else "",
            prev_hash=last or GENESIS,
        )
        # JSONField round trip: hash what will be read back.
        event.data = json.loads(json.dumps(event.data, default=str))
        event.hash = compute_hash(event)
        event.save(force_insert=True)
    transaction.on_commit(
        lambda: logger.info("audit", extra={"audit_action": action, "audit_seq": seq})
    )
    return event


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a chain verification."""

    ok: bool
    checked: int
    broken_at: int | None = None
    reason: str = ""


def _ordered() -> Iterator[AuditEvent]:
    return AuditEvent.objects.order_by("seq").iterator(chunk_size=2000)


def verify() -> VerifyResult:
    """Recompute every hash and link; report the first break."""
    prev = GENESIS
    checked = 0
    for event in _ordered():
        if event.prev_hash != prev:
            return VerifyResult(False, checked, event.seq, "link to previous event broken")
        if compute_hash(event) != event.hash:
            return VerifyResult(False, checked, event.seq, "event content altered")
        prev = event.hash
        checked += 1
    return VerifyResult(True, checked)
