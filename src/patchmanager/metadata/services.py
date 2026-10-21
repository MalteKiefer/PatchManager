"""Attach files and notes to objects; permissions follow the target object."""

import hashlib
import secrets
from typing import Any

import nh3
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.http import HttpRequest
from django.utils.translation import gettext as _
from markdown_it import MarkdownIt

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.core import crypto
from patchmanager.metadata import uploads
from patchmanager.metadata.models import Attachment, Note

# Objects that can carry attachments and notes: model label -> permission object type.
TARGETS = {
    "locations.site": "locations.site",
    "locations.building": "locations.building",
    "locations.floor": "locations.floor",
    "locations.room": "locations.room",
    "dcim.rack": "dcim.rack",
    "dcim.device": "dcim.device",
    "dcim.port": "dcim.device",
    "library.devicetype": "library.devicetype",
}

_markdown = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
_ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "em",
    "code",
    "pre",
    "ul",
    "ol",
    "li",
    "blockquote",
    "h1",
    "h2",
    "h3",
    "h4",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
}


def render_markdown(text: str) -> str:
    """Markdown to HTML (raw HTML disabled), then sanitised with an allow list."""
    return nh3.clean(
        _markdown.render(text),
        tags=_ALLOWED_TAGS,
        attributes={"a": {"href", "title"}},
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
    )


def resolve(label: str, object_id: Any) -> Any:
    """Load an allowed target (Row Level Security applies)."""
    if label not in TARGETS:
        raise ServiceError(_("Files cannot be attached here."))
    app_label, model = label.split(".")
    model_class = ContentType.objects.get_by_natural_key(app_label, model).model_class()
    target = model_class._default_manager.filter(pk=object_id).first() if model_class else None
    if target is None:
        raise ServiceError(_("Object not found."))
    return target


def _perm(target: Any, action: str) -> str:
    return f"{TARGETS[target._meta.label_lower]}.{action}"


def can(actor: Any, target: Any, action: str) -> bool:
    """``view`` to see attachments, ``change`` on the target to add or remove them."""
    return policy.has_perm(actor, _perm(target, action), target)


def _require(actor: Any, target: Any, action: str) -> None:
    if not can(actor, target, action):
        raise PermissionDeniedError(_("You do not have permission to do that."))


def _tenant_id(target: Any) -> Any:
    return getattr(target, "tenant_id", None)


def for_target(target: Any) -> tuple[Any, Any]:
    """Attachments and notes of ``target``."""
    content_type = ContentType.objects.get_for_model(target)
    query = {"content_type": content_type, "object_id": target.pk}
    return Attachment.objects.filter(**query), Note.objects.filter(**query).select_related("author")


def attach(
    request: HttpRequest, actor: User, target: Any, *, name: str, data: bytes, caption: str = ""
) -> Attachment:
    """Validate, encrypt and store an upload."""
    _require(actor, target, "change")
    cleaned = uploads.clean(data)
    storage_name = f"{secrets.token_hex(2)}/{secrets.token_hex(16)}.bin"
    stored = storages["attachments"].save(
        storage_name, ContentFile(crypto.encrypt_bytes(cleaned.data))
    )
    attachment = Attachment.objects.create(
        tenant_id=_tenant_id(target),
        content_type=ContentType.objects.get_for_model(target),
        object_id=target.pk,
        kind=cleaned.kind,
        original_name=_safe_name(name, cleaned.extension),
        mime_type=cleaned.mime_type,
        size=len(cleaned.data),
        sha256=hashlib.sha256(cleaned.data).hexdigest(),
        storage_name=stored,
        caption=caption[:300],
        uploaded_by=actor,
    )
    audit.record(
        "metadata.attachment.added",
        request=request,
        obj=target,
        tenant_id=_tenant_id(target),
        data={
            "file": attachment.original_name,
            "sha256": attachment.sha256,
            "size": attachment.size,
        },
    )
    return attachment


def _safe_name(name: str, extension: str) -> str:
    stem = (
        "".join(c for c in name.rsplit(".", 1)[0] if c.isalnum() or c in " -_()")[:100].strip()
        or "file"
    )
    return f"{stem}.{extension}"


def read(actor: Any, attachment: Attachment) -> tuple[Any, bytes]:
    """Return the target and decrypted content if the actor may view the target."""
    model_class = attachment.content_type.model_class()
    target = (
        model_class._default_manager.filter(pk=attachment.object_id).first()
        if model_class
        else None
    )
    if target is None or not can(actor, target, "view"):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    with storages["attachments"].open(attachment.storage_name, "rb") as handle:
        return target, crypto.decrypt_bytes(handle.read())


def remove(request: HttpRequest, actor: User, attachment: Attachment) -> None:
    """Delete an attachment and its stored file."""
    target = resolve(
        attachment.content_type.app_label + "." + attachment.content_type.model,
        attachment.object_id,
    )
    _require(actor, target, "change")
    storages["attachments"].delete(attachment.storage_name)
    audit.record(
        "metadata.attachment.removed",
        request=request,
        obj=target,
        tenant_id=_tenant_id(target),
        data={"file": attachment.original_name},
    )
    attachment.delete()


def add_note(request: HttpRequest, actor: User, target: Any, body: str) -> Note:
    """Add a Markdown note."""
    _require(actor, target, "change")
    if not body.strip():
        raise ServiceError(_("The note is empty."))
    note = Note.objects.create(
        tenant_id=_tenant_id(target),
        content_type=ContentType.objects.get_for_model(target),
        object_id=target.pk,
        body=body[:20000],
        author=actor,
    )
    audit.record("metadata.note.added", request=request, obj=target, tenant_id=_tenant_id(target))
    return note


def remove_note(request: HttpRequest, actor: User, note: Note) -> None:
    """Delete a note (authors and editors of the target)."""
    target = resolve(note.content_type.app_label + "." + note.content_type.model, note.object_id)
    _require(actor, target, "change")
    audit.record("metadata.note.removed", request=request, obj=target, tenant_id=_tenant_id(target))
    note.delete()
