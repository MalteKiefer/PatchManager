"""Cables: permanent links and patch cords between two ports."""

from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import LtreeField
from patchmanager.tenancy.models import TenantScopedModel


class Cable(TenantScopedModel):
    """A cable between two ports of the same tenant (anywhere in its sites)."""

    class Kind(models.TextChoices):
        PERMANENT = "permanent", _("permanent link")
        PATCH = "patch", _("patch cord")

    class Media(models.TextChoices):
        CAT5E = "cat5e", "Cat 5e"
        CAT6 = "cat6", "Cat 6"
        CAT6A = "cat6a", "Cat 6A"
        CAT7 = "cat7", "Cat 7"
        CAT8 = "cat8", "Cat 8"
        DAC = "dac", "DAC"
        OM3 = "om3", "OM3"
        OM4 = "om4", "OM4"
        OM5 = "om5", "OM5"
        OS2 = "os2", "OS2"
        COAX = "coax", _("coax")
        POWER = "power", _("power")
        OTHER = "other", _("other")

    class Status(models.TextChoices):
        PLANNED = "planned", _("planned")
        INSTALLED = "installed", _("installed")
        DECOMMISSIONED = "decommissioned", _("decommissioned")

    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices, default=Kind.PATCH)
    media = models.CharField(_("media"), max_length=10, choices=Media.choices, default=Media.CAT6A)
    colour = models.CharField(
        _("colour"),
        max_length=7,
        blank=True,
        default="#2563eb",
        validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", _("Use a hex colour like #2563eb."))],
    )
    length_m = models.DecimalField(
        _("length (m)"),
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    label = models.CharField(_("label"), max_length=100, blank=True)
    status = models.CharField(
        _("status"), max_length=15, choices=Status.choices, default=Status.INSTALLED
    )
    notes = models.TextField(_("notes"), blank=True)
    # Deepest location both ends share; the policy scope of the cable.
    path = LtreeField(editable=False)

    policy_object_type = "cabling.cable"
    condition_attrs = frozenset({"created_by", "kind", "status"})

    class Meta:
        ordering = ["label", "created_at"]

    def __str__(self) -> str:
        return self.label or f"#{str(self.pk)[:8]}"

    @property
    def policy_path(self) -> str:
        """Scoped to the deepest common location of its ends."""
        return str(self.path)


class CableEnd(TenantScopedModel):
    """One end of a cable on a port. A port carries at most one cable end."""

    class Side(models.TextChoices):
        A = "a", "A"
        B = "b", "B"

    cable = models.ForeignKey(Cable, on_delete=models.CASCADE, related_name="ends")
    side = models.CharField(max_length=1, choices=Side.choices)
    port = models.OneToOneField("dcim.Port", on_delete=models.CASCADE, related_name="cable_end")

    class Meta:
        ordering = ["cable", "side"]
        constraints = [models.UniqueConstraint("cable", "side", name="cableend_one_per_side")]

    def __str__(self) -> str:
        return f"{self.cable} {self.side}: {self.port}"
