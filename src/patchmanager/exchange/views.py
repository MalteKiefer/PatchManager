"""Import wizard (upload, dry run, apply) and exports (CSV, XLSX, JSON, PDF)."""

import secrets
from collections.abc import Callable
from typing import Any

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.core import ratelimit
from patchmanager.core.utils import parse_uuid
from patchmanager.dcim.models import Device, Rack
from patchmanager.exchange import formats, importer, pdf
from patchmanager.exchange.tables import TABLES
from patchmanager.locations.models import Site

PENDING_SECONDS = 1800
EXPORTS_PER_10_MIN = 60
IMPORTS_PER_10_MIN = 20
CONTENT_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "json": "application/json",
    "pdf": "application/pdf",
}
REPORTS: dict[str, tuple[Any, str, Callable[[Any, Any], bytes]]] = {
    "site": (Site, "locations.site.view", pdf.site_report),
    "patch-list": (Site, "locations.site.view", pdf.patch_list),
    "bom": (Site, "locations.site.view", pdf.bill_of_materials),
    "rack": (Rack, "dcim.rack.view", pdf.rack_elevation),
    "ports": (Device, "dcim.device.view", pdf.port_list),
}


class ImportForm(forms.Form):
    """What to import from which file."""

    kind = forms.ChoiceField(
        label=gettext_lazy("Table"), choices=[(k, t.title) for k, t in TABLES.items() if t.handler]
    )
    file = forms.FileField(label=gettext_lazy("CSV or XLSX file"))


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _tenant(request: HttpRequest) -> Any:
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    return tenant


def _download(request: HttpRequest, data: bytes, name: str, fmt: str) -> HttpResponse:
    response = HttpResponse(data, content_type=CONTENT_TYPES[fmt])
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = (
        f'attachment; filename="{slugify(name) or "export"}-{stamp}.{fmt}"'
    )
    return response


def _limited(request: HttpRequest) -> bool:
    return not ratelimit.hit(f"export:{request.user.pk}", limit=EXPORTS_PER_10_MIN, window=600)


def _import_limited(request: HttpRequest) -> bool:
    if ratelimit.hit(f"import:{request.user.pk}", limit=IMPORTS_PER_10_MIN, window=600):
        return False
    messages.error(request, _("Too many imports. Try again in a few minutes."))
    return True


def _pending_key(request: HttpRequest, token: str) -> str:
    return f"import:{request.user.pk}:{token}"


@login_required
def index(request: HttpRequest) -> HttpResponse:
    """Import form, export links and templates."""
    tenant = _tenant(request)
    sites = [
        s
        for s in Site.objects.filter(tenant=tenant).order_by("name")
        if policy.has_perm(request.user, "locations.site.view", s)
    ]
    return render(
        request,
        "exchange/index.html",
        {
            "form": ImportForm(),
            "tables": TABLES.values(),
            "sites": sites,
            "max_rows": formats.MAX_ROWS,
        },
    )


@login_required
@require_POST
def upload(request: HttpRequest) -> HttpResponse:
    """Parse the file and run a dry run."""
    tenant = _tenant(request)
    if _import_limited(request):
        return redirect("exchange:index")
    form = ImportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, _("Choose a table and a file."))
        return redirect("exchange:index")
    upload_file = form.cleaned_data["file"]
    kind = form.cleaned_data["kind"]
    try:
        if upload_file.size > settings.UPLOAD_MAX_BYTES:
            raise ServiceError(_("The file is too large."))
        rows = formats.read(upload_file.read())
        results = importer.run(request, _actor(request), tenant, kind, rows, apply=False)
    except ServiceError as exc:
        messages.error(request, str(exc))
        return redirect("exchange:index")
    token = secrets.token_urlsafe(16)
    cache.set(
        _pending_key(request, token),
        {"kind": kind, "rows": rows, "tenant": str(tenant.pk)},
        PENDING_SECONDS,
    )
    return render(
        request,
        "exchange/preview.html",
        {
            "table": TABLES[kind],
            "results": results,
            "summary": importer.summary(results),
            "token": token,
            "failed": any(r.outcome == "error" for r in results),
            "name": upload_file.name,
        },
    )


@login_required
@require_POST
def apply(request: HttpRequest) -> HttpResponse:
    """Run the checked import for real."""
    tenant = _tenant(request)
    if _import_limited(request):
        return redirect("exchange:index")
    token = request.POST.get("token", "")[:64]
    pending = cache.get(_pending_key(request, token)) if token else None
    if pending is None or pending["tenant"] != str(tenant.pk):
        messages.error(request, _("This import expired. Upload the file again."))
        return redirect("exchange:index")
    try:
        results = importer.run(
            request, _actor(request), tenant, pending["kind"], pending["rows"], apply=True
        )
    except ServiceError as exc:
        messages.error(request, str(exc))
        return redirect("exchange:index")
    cache.delete(_pending_key(request, token))
    if any(r.outcome == "error" for r in results):
        messages.error(request, _("Nothing was imported: the data changed since the check."))
        return render(
            request,
            "exchange/preview.html",
            {
                "table": TABLES[pending["kind"]],
                "results": results,
                "summary": importer.summary(results),
                "failed": True,
            },
        )
    counts = importer.summary(results)
    messages.success(
        request,
        _("Imported: %(c)s created, %(u)s updated, %(n)s unchanged.")
        % {
            "c": counts.get("created", 0),
            "u": counts.get("updated", 0),
            "n": counts.get("unchanged", 0),
        },
    )
    return redirect("exchange:index")


@login_required
def export(request: HttpRequest, kind: str, fmt: str) -> HttpResponse:
    """A table as CSV, XLSX or JSON, optionally limited to one site."""
    tenant = _tenant(request)
    table = TABLES.get(kind)
    if table is None or fmt not in ("csv", "xlsx", "json"):
        raise Http404
    if _limited(request):
        return HttpResponse(_("Too many exports. Try again in a few minutes."), status=429)
    site = None
    if request.GET.get("site"):
        site_pk = parse_uuid(request.GET["site"])
        site = Site.objects.filter(pk=site_pk, tenant=tenant).first() if site_pk else None
        if site is None or not policy.has_perm(request.user, "locations.site.view", site):
            raise Http404
    rows = table.export(request.user, tenant, site)
    if fmt == "csv":
        data = formats.write_csv(table.columns, rows)
    elif fmt == "xlsx":
        data = formats.write_xlsx(table.columns, rows, str(table.title))
    else:
        data = formats.write_json(rows)
    return _download(
        request, data, f"{tenant.slug}-{kind}" + (f"-{site.name}" if site else ""), fmt
    )


@login_required
def template(request: HttpRequest, kind: str, fmt: str) -> HttpResponse:
    """An empty file with the columns of a table."""
    table = TABLES.get(kind)
    if table is None or table.handler is None or fmt not in ("csv", "xlsx"):
        raise Http404
    data = (
        formats.write_csv(table.columns, [])
        if fmt == "csv"
        else formats.write_xlsx(table.columns, [], str(table.title))
    )
    return _download(request, data, f"template-{kind}", fmt)


@login_required
def report(request: HttpRequest, kind: str, pk: str) -> HttpResponse:
    """A PDF report about one site, rack or device."""
    if kind not in REPORTS:
        raise Http404
    model, permission, build = REPORTS[kind]
    obj = get_object_or_404(model, pk=pk)  # RLS hides other tenants
    if not policy.has_perm(request.user, permission, obj):
        raise Http404
    if _limited(request):
        return HttpResponse(_("Too many exports. Try again in a few minutes."), status=429)
    return _download(request, build(request.user, obj), f"{kind}-{obj}", "pdf")
