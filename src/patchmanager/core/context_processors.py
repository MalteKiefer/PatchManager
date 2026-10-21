"""Template context shared by every page."""

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def ui(request: HttpRequest) -> dict[str, Any]:
    """Instance name, theme and current tenant for the layout."""
    return {
        "instance_name": settings.INSTANCE_NAME,
        "theme": request.COOKIES.get("pm_theme", "system"),
        "current_tenant": getattr(request, "tenant", None),
        "available_tenants": getattr(request, "available_tenants", []),
        "impersonator": getattr(request, "impersonator", None),
    }
