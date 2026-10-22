"""Template context shared by every page."""

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject


def ui(request: HttpRequest) -> dict[str, Any]:
    """Instance name, theme and current tenant for the layout."""
    from patchmanager.instance import state

    return {
        "instance_name": SimpleLazyObject(state.instance_name),
        "branding": SimpleLazyObject(state.get),
        "source_url": settings.SOURCE_URL,
        "theme": request.COOKIES.get("pm_theme", "system"),
        "current_tenant": getattr(request, "tenant", None),
        "available_tenants": getattr(request, "available_tenants", []),
        "impersonator": getattr(request, "impersonator", None),
    }
