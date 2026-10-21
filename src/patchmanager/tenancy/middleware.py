"""Per request tenant visibility for Row Level Security and the tenant switcher."""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from patchmanager.accounts import policy
from patchmanager.accounts.middleware import ALWAYS_OPEN
from patchmanager.tenancy import rls
from patchmanager.tenancy.models import Tenant

CURRENT_TENANT = "_pm_tenant"


class TenantContextMiddleware:
    """Set the database tenant context, resolve the current tenant, always clear after."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Standard middleware constructor."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Wrap the request in the actor's tenant visibility."""
        if request.path.startswith(ALWAYS_OPEN):  # health checks must not touch the database
            return self.get_response(request)
        all_tenants, tenant_ids = policy.accessible_tenants(request.user)
        rls.set_context(tenant_ids, all_tenants=all_tenants)
        try:
            if request.user.is_authenticated:
                impersonator = getattr(request, "impersonator", None)
                label = str(request.user) + (f" (by {impersonator})" if impersonator else "")
                rls.set_actor(request.user.pk, label, getattr(request, "request_id", ""))
            request.available_tenants = []  # type: ignore[attr-defined]
            request.tenant = None  # type: ignore[attr-defined]
            if request.user.is_authenticated:
                tenants = list(Tenant.objects.filter(archived_at__isnull=True))  # RLS filtered
                chosen = request.session.get(CURRENT_TENANT)
                request.available_tenants = tenants  # type: ignore[attr-defined]
                default = tenants[0] if tenants else None
                request.tenant = next((t for t in tenants if str(t.pk) == chosen), default)  # type: ignore[attr-defined]
            return self.get_response(request)
        finally:
            rls.clear_context()
