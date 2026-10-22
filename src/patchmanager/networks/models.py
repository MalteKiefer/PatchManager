"""VLANs (tenant wide or per site) and WLANs."""

from typing import Any

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import CidrField, InetField, path_label
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


# --- Switching and routing of one device ------------------------------------


class DeviceScoped(TenantScopedModel):
    """A record of one device, governed by that device's permissions."""

    device = models.ForeignKey("dcim.Device", on_delete=models.CASCADE, related_name="+")

    policy_object_type = "dcim.device"

    class Meta:
        abstract = True

    @property
    def policy_path(self) -> str:
        """Governed by its device."""
        return str(self.device.path)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Belong to the device's tenant."""
        self.tenant_id = self.device.tenant_id
        super().save(*args, **kwargs)


class SwitchSettings(DeviceScoped):
    """Device wide switching and routing settings."""

    class SpanningTree(models.TextChoices):
        OFF = "off", _("off")
        STP = "stp", "STP (802.1D)"
        RSTP = "rstp", "RSTP (802.1w)"
        MSTP = "mstp", "MSTP (802.1s)"
        RPVST = "rpvst", "Rapid PVST+"
        PVST = "pvst", "PVST+"

    device = models.OneToOneField(
        "dcim.Device", on_delete=models.CASCADE, related_name="switch_settings"
    )
    hostname = models.CharField(_("hostname"), max_length=100, blank=True)
    stp_mode = models.CharField(
        _("spanning tree"), max_length=5, choices=SpanningTree.choices, blank=True
    )
    stp_priority = models.PositiveIntegerField(
        _("bridge priority"), null=True, blank=True, validators=[MaxValueValidator(61440)]
    )
    mst_region = models.CharField(_("MST region"), max_length=32, blank=True)
    mst_revision = models.PositiveIntegerField(
        _("MST revision"), null=True, blank=True, validators=[MaxValueValidator(65535)]
    )
    ip_routing = models.BooleanField(_("IP routing"), default=False)
    bgp_asn = models.PositiveBigIntegerField(
        _("BGP AS number"),
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(4_294_967_295)],
    )
    bgp_router_id = InetField(_("BGP router ID"), null=True, blank=True)
    ospf_router_id = InetField(_("OSPF router ID"), null=True, blank=True)
    gateways = models.JSONField(
        _("tunnel gateways"), default=list, blank=True
    )  # controller addresses for tunnelled ports
    jumbo_vlans = models.JSONField(default=list, blank=True)  # VLAN IDs with jumbo frames
    dns_servers = models.JSONField(_("DNS servers"), default=list, blank=True)
    domain = models.CharField(_("domain name"), max_length=253, blank=True)
    source = models.CharField(_("source"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("switch settings")

    def __str__(self) -> str:
        return f"{self.device}: {self.hostname}"


class MSTInstance(DeviceScoped):
    """An MSTP instance and the VLANs mapped to it."""

    instance = models.PositiveSmallIntegerField(
        _("instance"), validators=[MinValueValidator(1), MaxValueValidator(64)]
    )
    vlans = models.JSONField(_("VLANs"), default=list)  # VLAN IDs
    priority = models.PositiveIntegerField(
        _("priority"), null=True, blank=True, validators=[MaxValueValidator(61440)]
    )

    class Meta:
        ordering = ["device", "instance"]
        constraints = [models.UniqueConstraint("device", "instance", name="mst_instance_unique")]

    def __str__(self) -> str:
        return f"{self.device}: MST {self.instance}"


class StaticRoute(DeviceScoped):
    """A static route of a device."""

    vrf = models.ForeignKey(
        "ipam.VRF", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    prefix = CidrField(_("destination"))
    next_hop = InetField(_("next hop"), null=True, blank=True)
    interface = models.CharField(_("interface"), max_length=100, blank=True)
    distance = models.PositiveSmallIntegerField(
        _("distance"), null=True, blank=True, validators=[MaxValueValidator(255)]
    )
    description = models.CharField(_("description"), max_length=200, blank=True)

    class Meta:
        ordering = ["device", "prefix"]

    def __str__(self) -> str:
        return f"{self.prefix} via {self.next_hop or self.interface}"


class BGPNeighbor(DeviceScoped):
    """A BGP peer of a device (the local AS is in the switch settings)."""

    vrf = models.ForeignKey(
        "ipam.VRF", null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    address = InetField(_("address"))
    remote_asn = models.PositiveBigIntegerField(
        _("remote AS"), validators=[MinValueValidator(1), MaxValueValidator(4_294_967_295)]
    )
    peer_group = models.CharField(_("peer group"), max_length=64, blank=True)
    description = models.CharField(_("description"), max_length=200, blank=True)
    enabled = models.BooleanField(_("enabled"), default=True)

    class Meta:
        ordering = ["device", "address"]
        constraints = [
            models.UniqueConstraint(
                "device", "vrf", "address", nulls_distinct=False, name="bgp_neighbor_unique"
            )
        ]

    def __str__(self) -> str:
        return f"{self.address} AS{self.remote_asn}"


class PortProfile(DeviceScoped):
    """Port settings a switch applies by itself when it detects a kind of device."""

    name = models.CharField(_("name"), max_length=64)
    detects = models.CharField(_("applied to"), max_length=64, blank=True)
    untagged_vid = models.PositiveSmallIntegerField(
        _("untagged VLAN"), null=True, blank=True, validators=[MaxValueValidator(4094)]
    )
    tagged_vids = models.JSONField(_("tagged VLANs"), default=list, blank=True)
    jumbo = models.BooleanField(_("jumbo frames"), default=False)

    class Meta:
        ordering = ["device", "name"]
        constraints = [models.UniqueConstraint("device", "name", name="port_profile_unique")]

    def __str__(self) -> str:
        return self.name


class Stack(TenantScopedModel):
    """Several switches that work as one: stacking, VSF, VSX, MLAG or a virtual chassis."""

    class Kind(models.TextChoices):
        STACK = "stack", _("stack (stacking cables)")
        VSF = "vsf", "VSF"
        VSX = "vsx", "VSX"
        MLAG = "mlag", "MLAG"
        CHASSIS = "chassis", _("virtual chassis")
        OTHER = "other", _("other")

    site = models.ForeignKey("locations.Site", on_delete=models.PROTECT, related_name="+")
    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(_("kind"), max_length=8, choices=Kind.choices, default=Kind.STACK)
    domain = models.PositiveIntegerField(_("domain ID"), null=True, blank=True)
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "dcim.device"

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint("site", "name", name="stack_name_unique")]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Governed by device permissions on its site."""
        return str(self.site.path)


class StackMember(TenantScopedModel):
    """A switch in a stack, with its member number and role."""

    class Role(models.TextChoices):
        PRIMARY = "primary", _("commander / primary")
        STANDBY = "standby", _("standby / secondary")
        MEMBER = "member", _("member")

    stack = models.ForeignKey(Stack, on_delete=models.CASCADE, related_name="members")
    device = models.OneToOneField(
        "dcim.Device", on_delete=models.CASCADE, related_name="stack_member"
    )
    member_id = models.PositiveSmallIntegerField(_("member"), validators=[MaxValueValidator(64)])
    role = models.CharField(_("role"), max_length=8, choices=Role.choices, default=Role.MEMBER)
    priority = models.PositiveSmallIntegerField(
        _("priority"), null=True, blank=True, validators=[MaxValueValidator(255)]
    )

    class Meta:
        ordering = ["stack", "member_id"]
        constraints = [models.UniqueConstraint("stack", "member_id", name="stack_member_unique")]

    def __str__(self) -> str:
        return f"{self.stack} {self.member_id}: {self.device}"
