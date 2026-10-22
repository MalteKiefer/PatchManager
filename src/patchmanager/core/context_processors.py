"""Template context shared by every page."""

from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils.functional import SimpleLazyObject

# URL namespaces and the sidebar entry they belong to.
SECTIONS = {
    "locations": "locations",
    "dcim": "locations",
    "devices": "locations",
    "configs": "locations",
    "floorplans": "locations",
    "library": "library",
    "cabling": "cables",
    "networks": "networks",
    "directory": "directory",
    "compute": "compute",
    "ipam": "ipam",
    "history": "activity",
    "search": "search",
    "exchange": "exchange",
    "integrations": "exchange",
    "manage": "admin",
    "scim": "admin",
    "sso": "admin",
    "instance": "admin",
}


def _section(request: HttpRequest) -> str:
    """The navigation entry of the current page."""
    match = request.resolver_match
    namespace = getattr(match, "namespace", "") or ""
    if namespace == "objects":  # shared record pages: DHCP and DNS belong to IP networks
        from patchmanager.core.crud import REGISTRY

        resource = REGISTRY.get(match.kwargs.get("key", "")) if match else None
        if resource is not None and resource.group == "compute":
            return "compute"
        return "ipam" if resource is not None and resource.group == "services" else "directory"
    return SECTIONS.get(namespace, "")


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
        "section": _section(request),
    }
