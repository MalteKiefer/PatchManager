"""Template helpers."""

from typing import Any

from django import template

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
