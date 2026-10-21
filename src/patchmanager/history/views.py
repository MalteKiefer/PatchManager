"""Activity log, object history, change details and restore."""

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.history import services
from patchmanager.history.models import Change
from patchmanager.history.registry import TRACKED

URLS = {
    "dcim.rack": "dcim:rack",
    "dcim.device": "devices:device",
    "dcim.port": "devices:port",
    "cabling.cable": "cabling:cable",
    "networks.vlan": "networks:vlan",
    "networks.wlan": "networks:wlan",
}


def object_url(object_type: str, object_id: Any) -> str:
    """Detail page of a tracked object."""
    if object_type in URLS:
        return reverse(URLS[object_type], args=[object_id])
    return reverse("locations:node", args=[object_type.split(".")[1], object_id])


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _rows(change: Change) -> list[dict[str, Any]]:
    """Field rows for the diff table."""
    before, after = change.before or {}, change.after or {}
    keys = change.fields or sorted(set(before) | set(after) - {"id", "tenant_id"})

    def show(value: Any) -> str:
        return "" if value is None else value if isinstance(value, str) else json.dumps(value)

    return [{"field": k, "before": show(before.get(k)), "after": show(after.get(k))} for k in keys]


@login_required
def activity(request: HttpRequest) -> HttpResponse:
    """Global activity log of the current tenant, filterable."""
    tenant = getattr(request, "tenant", None)
    changes = services.visible(request.user, Change.objects.filter(tenant_id=getattr(tenant, "pk", None)))
    params = request.GET
    if params.get("type") in TRACKED:
        changes = changes.filter(object_type=params["type"])
    if params.get("action") in Change.Action.values:
        changes = changes.filter(action=params["action"])
    for key, lookup in (("q", "label__icontains"), ("actor", "actor_label__icontains")):
        if params.get(key, "").strip():
            changes = changes.filter(**{lookup: params[key].strip()[:100]})
    for key, lookup in (("since", "occurred_at__date__gte"), ("until", "occurred_at__date__lte")):
        day = parse_date(params.get(key, "")) if params.get(key) else None
        if day:
            changes = changes.filter(**{lookup: day})
    page = Paginator(changes, 50).get_page(params.get("page"))
    return render(request, "history/activity.html", {
        "page": page, "filters": params, "types": sorted(TRACKED), "actions": Change.Action.choices,
    })


@login_required
def object_history(request: HttpRequest, object_type: str, pk: str) -> HttpResponse:
    """All changes of one object."""
    try:
        changes = services.for_object(request.user, object_type, pk)
    except ServiceError as exc:
        raise Http404 from exc
    latest = changes.first()
    if latest is None:
        raise Http404
    page = Paginator(changes, 50).get_page(request.GET.get("page"))
    exists = services.tracked(object_type).model._default_manager.filter(pk=pk).exists()
    return render(request, "history/object.html", {
        "page": page, "latest": latest, "object_url": object_url(object_type, pk) if exists else None,
    })


@login_required
def change_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Before and after of one change."""
    change = services.visible(request.user, Change.objects.filter(pk=pk)).first()
    if change is None:
        raise Http404
    t = TRACKED[change.object_type]
    obj = t.model._default_manager.filter(pk=change.object_id).first()
    can_restore = (
        change.action == Change.Action.UPDATE
        and obj is not None
        and policy.has_perm(request.user, f"{t.policy_type}.change", obj)
    )
    return render(request, "history/change.html", {
        "change": change, "rows": _rows(change), "can_restore": can_restore,
        "object_url": object_url(change.object_type, change.object_id) if obj is not None else None,
    })


@login_required
@require_POST
def restore(request: HttpRequest, pk: str) -> HttpResponse:
    """Set the fields of a change back."""
    change = services.visible(request.user, Change.objects.filter(pk=pk)).first()
    if change is None:
        raise Http404
    try:
        fields, skipped = services.restore(request, _actor(request), change)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ServiceError as exc:
        messages.error(request, str(exc))
        return redirect("history:change", change.pk)
    messages.success(request, _("Restored: %(fields)s.") % {"fields": ", ".join(fields)})
    if skipped:
        messages.warning(request, _("Not restored (links): %(fields)s.") % {"fields": ", ".join(skipped)})
    return redirect("history:object", change.object_type, change.object_id)
