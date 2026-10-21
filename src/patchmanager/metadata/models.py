"""Attachments and notes on arbitrary objects (site, room, rack, device, port, ...)."""

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.models import TimestampedModel


class AttachedModel(TimestampedModel):
    """Points at one target object.

    ``tenant`` is the target's tenant (``NULL`` for shared library objects).
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    object_id = models.UUIDField(db_index=True)

    class Meta:
        abstract = True
        indexes = [
            models.Index(
                fields=["content_type", "object_id"], name="%(app_label)s_%(class)s_target"
            )
        ]


class Attachment(AttachedModel):
    """An uploaded file. Content is stored encrypted; metadata here."""

    class Kind(models.TextChoices):
        IMAGE = "image", _("image")
        DOCUMENT = "document", _("document")

    kind = models.CharField(max_length=10, choices=Kind.choices)
    original_name = models.CharField(_("file name"), max_length=255)
    mime_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    storage_name = models.CharField(max_length=100, unique=True)
    caption = models.CharField(_("caption"), max_length=300, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(AttachedModel.Meta):
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.original_name


class Note(AttachedModel):
    """A Markdown note; rendered and sanitised on display."""

    body = models.TextField(_("note"), max_length=20000)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta(AttachedModel.Meta):
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.body[:50]
