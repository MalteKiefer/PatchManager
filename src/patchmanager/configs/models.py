"""Versions of device configurations, encrypted at rest."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import LtreeField
from patchmanager.tenancy.models import TenantScopedModel


class DeviceConfig(TenantScopedModel):
    """One stored version of a device's configuration."""

    class Source(models.TextChoices):
        MANUAL = "manual", _("entered by hand")
        UPLOAD = "upload", _("uploaded file")
        API = "api", _("API")

    device = models.ForeignKey(
        "dcim.Device", on_delete=models.CASCADE, related_name="configs", verbose_name=_("device")
    )
    version = models.PositiveIntegerField(_("version"))
    sha256 = models.CharField(max_length=64)
    size = models.PositiveIntegerField(_("size (bytes)"))
    lines = models.PositiveIntegerField(_("lines"))
    source = models.CharField(_("source"), max_length=10, choices=Source.choices)
    comment = models.CharField(_("comment"), max_length=200, blank=True)
    content = models.TextField()  # Fernet token of the configuration text
    # Policy scope: the device.
    path = LtreeField(editable=False)

    policy_object_type = "configs.config"

    class Meta:
        ordering = ["device", "-version"]
        constraints = [
            models.UniqueConstraint("device", "version", name="deviceconfig_version_unique")
        ]

    def __str__(self) -> str:
        return f"{self.device} v{self.version}"

    @property
    def policy_path(self) -> str:
        """The device's scope."""
        return str(self.path)
