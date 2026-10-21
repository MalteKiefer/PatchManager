"""One plan per floor; coordinates everywhere are pixels of the stored plan image."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.tenancy.models import TenantScopedModel


class FloorPlan(TenantScopedModel):
    """Background image of a floor and its scale."""

    floor = models.OneToOneField("locations.Floor", on_delete=models.CASCADE, related_name="plan")
    image = models.ForeignKey("metadata.Attachment", on_delete=models.PROTECT, related_name="+")
    width_px = models.PositiveIntegerField()
    height_px = models.PositiveIntegerField()
    px_per_metre = models.FloatField(_("pixels per metre"), null=True, blank=True)

    policy_object_type = "locations.floor"

    def __str__(self) -> str:
        return f"{_('Plan')} {self.floor}"

    @property
    def policy_path(self) -> str:
        """Governed by its floor."""
        return str(self.floor.path)
