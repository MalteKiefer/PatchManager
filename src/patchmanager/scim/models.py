"""Bearer tokens of SCIM clients (identity providers that provision users)."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.models import TimestampedModel


class ScimToken(TimestampedModel):
    """Only a keyed hash is stored; the prefix finds the row."""

    name = models.CharField(_("name"), max_length=100)
    prefix = models.CharField(max_length=16, unique=True)
    token_hash = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name
