"""VLANs (tenant wide or per site) and WLANs."""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import path_label
from patchmanager.tenancy.models import TenantScopedModel


class VLAN(TenantScopedModel):
    """An 802.1Q VLAN. Without a site it is valid in the whole tenant."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("active")
        RESERVED = "reserved", _("reserved")
        DEPRECATED = "deprecated", _("deprecated")

    site = models.ForeignKey(
        "locations.Site",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="vlans",
        verbose_name=_("site"),
    )
    vid = models.PositiveSmallIntegerField(
        _("VLAN ID"), validators=[MinValueValidator(1), MaxValueValidator(4094)]
    )
    name = models.CharField(_("name"), max_length=64)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "networks.vlan"

    class Meta:
        ordering = ["vid", "name"]
        constraints = [
            models.UniqueConstraint(
                "tenant", "site", "vid", nulls_distinct=False, name="vlan_vid_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(vid__gte=1, vid__lte=4094), name="vlan_vid_range"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vid} {self.name}"

    @property
    def policy_path(self) -> str:
        """Site VLANs are scoped to their site, others to the tenant."""
        return str(self.site.path) if self.site is not None else path_label(self.tenant_id)


class WLAN(TenantScopedModel):
    """A wireless network, optionally bridged into a VLAN, broadcast by access point radios."""

    class Security(models.TextChoices):
        OPEN = "open", _("open")
        WPA2_PERSONAL = "wpa2-personal", "WPA2 Personal"
        WPA3_PERSONAL = "wpa3-personal", "WPA3 Personal"
        WPA2_ENTERPRISE = "wpa2-enterprise", "WPA2 Enterprise"
        WPA3_ENTERPRISE = "wpa3-enterprise", "WPA3 Enterprise"

    ssid = models.CharField(_("SSID"), max_length=32)
    vlan = models.ForeignKey(
        VLAN,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="wlans",
        verbose_name=_("VLAN"),
    )
    security = models.CharField(
        _("security"), max_length=16, choices=Security.choices, default=Security.WPA3_PERSONAL
    )
    hidden = models.BooleanField(_("hidden SSID"), default=False)
    description = models.TextField(_("description"), blank=True)
    radios = models.ManyToManyField(
        "dcim.Port", through="WLANBroadcast", related_name="wlans", blank=True
    )

    policy_object_type = "networks.wlan"

    class Meta:
        ordering = ["ssid"]
        constraints = [models.UniqueConstraint("tenant", "ssid", name="wlan_ssid_unique")]

    def __str__(self) -> str:
        return self.ssid

    @property
    def policy_path(self) -> str:
        """WLANs are tenant wide."""
        return path_label(self.tenant_id)


class WLANBroadcast(TenantScopedModel):
    """A wireless port (radio) that broadcasts a WLAN."""

    wlan = models.ForeignKey(WLAN, on_delete=models.CASCADE, related_name="broadcasts")
    port = models.ForeignKey("dcim.Port", on_delete=models.CASCADE, related_name="broadcasts")

    class Meta:
        constraints = [models.UniqueConstraint("wlan", "port", name="wlan_broadcast_unique")]


class TaggedVLAN(TenantScopedModel):
    """A VLAN carried tagged on a trunk port."""

    port = models.ForeignKey("dcim.Port", on_delete=models.CASCADE, related_name="tagged")
    vlan = models.ForeignKey(VLAN, on_delete=models.CASCADE, related_name="tagged_on")

    class Meta:
        constraints = [models.UniqueConstraint("port", "vlan", name="tagged_vlan_unique")]
