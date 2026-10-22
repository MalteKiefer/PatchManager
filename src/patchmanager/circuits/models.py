"""Lines from providers: internet, MPLS, leased lines, dark fibre, mobile."""

from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import path_label
from patchmanager.tenancy.models import TenantScopedModel


class Circuit(TenantScopedModel):
    """A line a provider delivers to a site, with its contract."""

    class Kind(models.TextChoices):
        INTERNET = "internet", _("internet")
        MPLS = "mpls", "MPLS / VPN"
        LEASED = "leased", _("leased line")
        FIBRE = "fibre", _("dark fibre")
        DSL = "dsl", "DSL"
        CABLE = "cable", _("cable")
        MOBILE = "mobile", _("mobile (LTE, 5G)")
        PHONE = "phone", _("telephony (SIP)")
        OTHER = "other", _("other")

    class Status(models.TextChoices):
        PLANNED = "planned", _("planned")
        ORDERED = "ordered", _("ordered")
        ACTIVE = "active", _("active")
        CANCELLED = "cancelled", _("cancelled")

    circuit_id = models.CharField(_("line ID"), max_length=100)
    provider = models.ForeignKey(
        "directory.Organization",
        on_delete=models.PROTECT,
        related_name="circuits",
        verbose_name=_("provider"),
    )
    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices, default=Kind.INTERNET)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    site = models.ForeignKey(
        "locations.Site",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("site"),
    )
    port = models.ForeignKey(
        "dcim.Port",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="circuits",
        verbose_name=_("handover port"),
        help_text=_("Where the line ends, for example on the provider's router or our firewall."),
    )
    download_mbps = models.PositiveIntegerField(_("download (Mbit/s)"), null=True, blank=True)
    upload_mbps = models.PositiveIntegerField(_("upload (Mbit/s)"), null=True, blank=True)
    addresses = models.CharField(_("public addresses"), max_length=200, blank=True)
    contract_number = models.CharField(_("contract number"), max_length=100, blank=True)
    contract_end = models.DateField(_("contract end"), null=True, blank=True)
    notice_days = models.PositiveIntegerField(_("notice period (days)"), null=True, blank=True)
    monthly_cost = models.DecimalField(
        _("monthly cost"), max_digits=10, decimal_places=2, null=True, blank=True
    )
    support = models.CharField(_("fault hotline"), max_length=200, blank=True)
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "circuits.circuit"

    class Meta:
        ordering = ["provider__name", "circuit_id"]
        constraints = [models.UniqueConstraint("provider", "circuit_id", name="circuit_id_unique")]

    def __str__(self) -> str:
        return f"{self.provider} {self.circuit_id}"

    @property
    def policy_path(self) -> str:
        """Tenant wide."""
        return path_label(self.tenant_id)
