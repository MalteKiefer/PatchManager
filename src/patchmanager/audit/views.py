"""Audit log viewer, CSV export and chain verification."""

import csv
from collections.abc import Iterator
from datetime import datetime

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q, QuerySet
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.utils.dateparse import parse_date
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.decorators import permission_required
from patchmanager.audit import services
from patchmanager.audit.models import AuditEvent
from patchmanager.tenancy.models import Tenant

EXPORT_LIMIT = 100_000


def _filtered(request: HttpRequest) -> QuerySet[AuditEvent]:
    """Events matching the query string, limited to what the viewer may see."""
    qs = AuditEvent.objects.all()
    if not policy.has_perm(request.user, "audit.event.view"):
        # Tenant scoped auditors see their tenants' events only.
        tenant_ids = [
            t.pk
            for t in Tenant.objects.all()
            if policy.has_perm(request.user, "audit.event.view", scope_path=t.policy_path)
        ]
        qs = qs.filter(tenant_id__in=tenant_ids)
    if action := request.GET.get("action", "").strip():
        qs = qs.filter(action__startswith=action)
    if actor := request.GET.get("actor", "").strip():
        qs = qs.filter(actor_label__icontains=actor)
    if obj := request.GET.get("object", "").strip():
        qs = qs.filter(Q(object_label__icontains=obj) | Q(object_id=obj))
    if since := parse_date(request.GET.get("since", "")):
        qs = qs.filter(occurred_at__date__gte=since)
    if until := parse_date(request.GET.get("until", "")):
        qs = qs.filter(occurred_at__date__lte=until)
    return qs


@permission_required("audit.event.view", any_scope=True)
def event_list(request: HttpRequest) -> HttpResponse:
    """Filterable list of events."""
    page = Paginator(_filtered(request), 50).get_page(request.GET.get("page"))
    return render(
        request,
        "manage/audit.html",
        {
            "page": page,
            "filters": request.GET,
            "can_export": policy.has_perm(request.user, "audit.event.export"),
            "can_verify": policy.has_perm(request.user, "audit.event.verify"),
        },
    )


class _Echo:
    def write(self, value: str) -> str:
        return value


@permission_required("audit.event.export")
def event_export(request: HttpRequest) -> StreamingHttpResponse:
    """Stream the filtered events as CSV."""
    services.record("audit.exported", request=request, data={"filters": dict(request.GET.items())})
    writer = csv.writer(_Echo())
    columns = [
        "seq",
        "occurred_at",
        "action",
        "actor_label",
        "tenant_id",
        "object_type",
        "object_id",
        "object_label",
        "ip_address",
        "request_id",
        "data",
        "hash",
    ]

    def rows() -> Iterator[str]:
        yield writer.writerow(columns)
        for event in _filtered(request).order_by("seq")[:EXPORT_LIMIT].iterator():
            values = [getattr(event, c) for c in columns]
            # Neutralise spreadsheet formulas (CSV injection).
            yield writer.writerow(
                ["'" + v if isinstance(v, str) and v[:1] in "=+-@" else v for v in values]
            )

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    response = StreamingHttpResponse(rows(), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="patchmanager-audit-{stamp}.csv"'
    return response


@permission_required("audit.event.verify")
@require_POST
def verify_chain(request: HttpRequest) -> HttpResponse:
    """Recompute the hash chain."""
    result = services.verify()
    services.record(
        "audit.verified", request=request, data={"ok": result.ok, "checked": result.checked}
    )
    if result.ok:
        messages.success(
            request, _("Chain intact: %(count)s events verified.") % {"count": result.checked}
        )
    else:
        messages.error(
            request,
            _("Chain broken at event %(seq)s: %(reason)s")
            % {"seq": result.broken_at, "reason": result.reason},
        )
    return redirect("manage:audit")
