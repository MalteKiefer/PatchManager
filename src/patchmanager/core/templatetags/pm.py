"""Template helpers."""

from typing import Any

from django import template

register = template.Library()


@register.filter
def node_url(node: Any) -> str:
    """Detail URL of a location tree node or rack."""
    from patchmanager.locations.views import node_url as url

    return url(node)
