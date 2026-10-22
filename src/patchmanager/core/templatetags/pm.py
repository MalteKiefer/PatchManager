"""Template helpers."""

from typing import Any

from django import template
from django.urls import reverse

register = template.Library()


@register.filter
def node_url(node: Any) -> str:
    """Detail URL of a location tree node or rack."""
    from patchmanager.locations.views import node_url as url

    return url(node)


@register.filter
def markdown(text: str) -> Any:
    """Render a note's Markdown as sanitised HTML."""
    from django.utils.safestring import mark_safe

    from patchmanager.metadata.services import render_markdown

    return mark_safe(render_markdown(text))  # noqa: S308  # nosec B308, B703: nh3 sanitised


@register.simple_tag
def history_url(target: Any) -> str:
    """Link to the change history of ``target`` (empty if it has none)."""
    from patchmanager.history.registry import TRACKED

    label = target._meta.label_lower
    return reverse("history:object", args=[label, target.pk]) if label in TRACKED else ""


@register.inclusion_tag("directory/_links.html", takes_context=True)
def links_panel(context: Any, target: Any) -> dict[str, Any]:
    """Systems and organisations ``target`` is linked to (nothing for other kinds)."""
    from patchmanager.accounts import policy
    from patchmanager.directory import links

    request = context["request"]
    label = target._meta.label_lower
    if label not in links.TARGETS:
        return {"hide": True}
    return {
        "request": request,
        "target": target,
        "label": label,
        "links": links.for_target(request.user, target),
        "can_change": policy.has_perm(request.user, f"{links.TARGETS[label]}.change", target),
        "csrf_token": context.get("csrf_token"),
    }


@register.inclusion_tag("metadata/_panel.html", takes_context=True)
def metadata_panel(context: Any, target: Any) -> dict[str, Any]:
    """Photos, files and notes of ``target`` with upload and note forms."""
    from patchmanager.metadata import services
    from patchmanager.metadata.views import NoteForm, UploadForm

    request = context["request"]
    attachments, notes = services.for_target(target)
    return {
        "request": request,
        "target": target,
        "label": target._meta.label_lower,
        "attachments": attachments,
        "notes": notes,
        "can_change": services.can(request.user, target, "change"),
        "upload_form": UploadForm(),
        "note_form": NoteForm(),
        "csrf_token": context.get("csrf_token"),
    }
