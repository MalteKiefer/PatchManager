"""Instance wide settings that super admins change at runtime (one row)."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.models import TimestampedModel


class InstanceSettings(TimestampedModel):
    """Branding and defaults."""

    name = models.CharField(_("instance name"), max_length=100, blank=True)
    logo = models.BinaryField(null=True, blank=True)  # PNG, re-encoded on upload
    brand_hue = models.PositiveSmallIntegerField(_("brand colour (hue)"), default=190)
    default_language = models.CharField(_("default language"), max_length=8, default="en")
    # Operational settings (instance.config); secrets inside are encrypted.
    config = models.JSONField(default=dict, blank=True)
    # Last run of the off-site copy: ok, at, message (instance.offsite).
    offsite_status = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("instance settings")

    def __str__(self) -> str:
        return self.name or "PatchManager"


class JobRun(models.Model):
    """One run of a background job (kept a few days for the job monitor)."""

    class Status(models.TextChoices):
        RUNNING = "running", _("running")
        SUCCESS = "success", _("done")
        FAILURE = "failure", _("failed")

    task_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField(db_index=True)
    finished_at = models.DateTimeField(null=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"{self.name} {self.status}"
