"""Instance wide settings that super admins change at runtime (one row)."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.models import TimestampedModel


class InstanceSettings(TimestampedModel):
    """Edition, limits, branding and defaults. Environment variables can pin some of them."""

    edition = models.CharField(_("edition"), max_length=20, default="enterprise")
    overrides = models.JSONField(
        default=dict, blank=True
    )  # limit and feature overrides of the edition
    name = models.CharField(_("instance name"), max_length=100, blank=True)
    logo = models.BinaryField(null=True, blank=True)  # PNG, re-encoded on upload
    brand_hue = models.PositiveSmallIntegerField(_("brand colour (hue)"), default=190)
    default_language = models.CharField(_("default language"), max_length=8, default="en")

    class Meta:
        verbose_name = _("instance settings")

    def __str__(self) -> str:
        return self.name or "PatchManager"
