"""Reading the change history and restoring earlier field values."""

import json
from typing import Any

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.history.models import Change
from patchmanager.history.registry import TRACKED, Tracked


def visible(user: Any, changes: QuerySet[Change] | None = None) -> QuerySet[Change]:
    """Changes of objects the user may view, judged by the object's scope at that time."""
    changes = changes if changes is not None else Change.objects.all()
    result = changes.none()
    for t in TRACKED.values():
        result |= policy.scope_queryset(
            user, f"{t.policy_type}.view", changes.filter(object_type=t.label),
            strict_conditions=True,
        )
    return result


def tracked(object_type: str) -> Tracked:
    """Registry entry, or ServiceError for unknown types."""
    if object_type not in TRACKED:
        raise ServiceError(_("Unknown object type."))
    return TRACKED[object_type]


def for_object(user: Any, object_type: str, object_id: Any) -> QuerySet[Change]:
    """History of one object (also after it was deleted)."""
    tracked(object_type)
    return visible(user, Change.objects.filter(object_type=object_type, object_id=object_id))


def _restorable(t: Tracked) -> dict[str, Any]:
    """Columns that may be set back: plain values, not links, scope or identity."""
    skip = {"id", "tenant_id", "path", "created_at", "updated_at", "created_by_id"}
    return {
        f.column: f
        for f in t.model._meta.concrete_fields
        if f.column not in skip and not f.is_relation
    }


def restore(request: HttpRequest, actor: User, change: Change) -> tuple[list[str], list[str]]:
    """Set the fields of an update back to their earlier values.

    Refused if any of those fields changed again since. Links (room, rack,
    VLAN ...) are not restored; they are returned as skipped.
    """
    if change.action != Change.Action.UPDATE:
        raise ServiceError(_("Only changes can be restored, not creation or deletion."))
    t = tracked(change.object_type)
    obj = t.model._default_manager.filter(pk=change.object_id).first()
    if obj is None:
        raise ServiceError(_("The object no longer exists."))
    if not policy.has_perm(actor, f"{t.policy_type}.change", obj):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    columns = _restorable(t)
    fields = [f for f in change.fields if f in columns]
    skipped = [f for f in change.fields if f not in columns]
    if not fields:
        raise ServiceError(_("None of these fields can be restored."))
    with connection.cursor() as cursor:  # table name comes from the registry, not the request
        cursor.execute(f'SELECT to_jsonb(t) FROM "{t.model._meta.db_table}" t WHERE id = %s', [obj.pk])
        raw = cursor.fetchone()[0]
    current = json.loads(raw) if isinstance(raw, str) else raw
    if any(current.get(f) != change.after.get(f) for f in fields):
        raise ServiceError(_("These fields were changed again since. Restore the later change first."))
    for column in fields:
        field = columns[column]
        setattr(obj, field.attname, field.to_python(change.before.get(column)))
    try:
        obj.full_clean(exclude=[f.name for f in t.model._meta.concrete_fields if f.column not in fields])
    except ValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    with transaction.atomic():
        obj.save(update_fields=[columns[c].name for c in fields])
    audit.record("history.restored", request=request, obj=obj, data={"change": str(change.pk), "fields": fields})
    return fields, skipped
