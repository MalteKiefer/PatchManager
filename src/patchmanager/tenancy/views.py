"""Tenant switcher and tenant administration."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.decorators import permission_required
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.tenancy import services
from patchmanager.tenancy.middleware import CURRENT_TENANT
from patchmanager.tenancy.models import Tenant


class TenantForm(forms.ModelForm):  # type: ignore[type-arg]
    """Tenant details."""

    class Meta:
        model = Tenant
        fields = list(services.FIELDS)


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: guarded by decorators
    return request.user


@login_required
@require_POST
def switch(request: HttpRequest) -> HttpResponse:
    """Choose the current tenant (must be visible to the user)."""
    tenants = getattr(request, "available_tenants", [])
    tenant = next((t for t in tenants if str(t.pk) == request.POST.get("tenant")), None)
    if tenant is not None:
        request.session[CURRENT_TENANT] = str(tenant.pk)
    target = request.POST.get("next", "/")
    if not url_has_allowed_host_and_scheme(
        target, {request.get_host()}, require_https=request.is_secure()
    ):
        target = "/"
    return redirect(target)


@permission_required("tenancy.tenant.view", any_scope=True)
def tenant_list(request: HttpRequest) -> HttpResponse:
    """Tenants the user may see, including archived ones."""
    tenants = [
        t for t in Tenant.objects.all() if policy.has_perm(request.user, "tenancy.tenant.view", t)
    ]
    return render(
        request,
        "manage/tenants.html",
        {"tenants": tenants, "can_add": policy.has_perm(request.user, "tenancy.tenant.add")},
    )


@permission_required("tenancy.tenant.add")
@require_http_methods(["GET", "POST"])
def tenant_create(request: HttpRequest) -> HttpResponse:
    """New tenant."""
    form = TenantForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            tenant = services.create_tenant(request, _actor(request), **form.cleaned_data)
        except (ServiceError, ValidationError) as exc:
            form.add_error(None, exc if isinstance(exc, ValidationError) else str(exc))
        else:
            messages.success(request, _("Tenant created."))
            return redirect("manage:tenant", tenant.pk)
    return render(request, "manage/tenant_detail.html", {"form": form, "tenant": None})


@login_required
@require_http_methods(["GET", "POST"])
def tenant_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit, archive, restore or delete a tenant."""
    tenant = get_object_or_404(Tenant, pk=pk)  # RLS: 404 for foreign tenants
    actor = _actor(request)
    if not policy.has_perm(actor, "tenancy.tenant.view", tenant):
        return render(
            request, "core/error.html", {"title": _("Not found"), "code": 404}, status=404
        )
    form = TenantForm(request.POST if "save" in request.POST else None, instance=tenant)
    if request.method == "POST":
        try:
            if "save" in request.POST:
                if form.is_valid():
                    services.update_tenant(request, actor, tenant, **form.cleaned_data)
                    messages.success(request, _("Tenant saved."))
                    return redirect("manage:tenant", pk)
            elif "archive" in request.POST or "restore" in request.POST:
                services.set_archived(request, actor, tenant, archived="archive" in request.POST)
                return redirect("manage:tenant", pk)
            elif "delete" in request.POST:
                services.delete_tenant(
                    request, actor, tenant, confirm_slug=request.POST.get("confirm", "")
                )
                messages.success(request, _("Tenant deleted."))
                return redirect("manage:tenants")
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("manage:tenant", pk)
    return render(
        request,
        "manage/tenant_detail.html",
        {
            "form": form,
            "tenant": tenant,
            "can_change": policy.has_perm(actor, "tenancy.tenant.change", tenant),
            "can_delete": policy.has_perm(actor, "tenancy.tenant.delete", tenant),
        },
    )
