"""IP address management: VRFs, prefixes and addresses on ports."""

import ipaddress
from typing import Any

from django.db import models
from django.db.models import F, Func
from django.db.models.functions import Cast
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import CidrField, InetField, LtreeField, path_label
from patchmanager.tenancy.models import TenantScopedModel


class VRF(TenantScopedModel):
    """A routing table. Prefixes and addresses without a VRF are in the global table."""

    name = models.CharField(_("name"), max_length=100)
    rd = models.CharField(_("route distinguisher"), max_length=21, blank=True)
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "ipam.vrf"

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint("tenant", "name", name="vrf_name_unique")]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Scoped to the tenant."""
        return path_label(self.tenant_id)


class Prefix(TenantScopedModel):
    """A network, optionally used at one site and in one VLAN."""

    class Status(models.TextChoices):
        CONTAINER = "container", _("container")
        ACTIVE = "active", _("active")
        RESERVED = "reserved", _("reserved")
        DEPRECATED = "deprecated", _("deprecated")

    vrf = models.ForeignKey(
        VRF,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="prefixes",
        verbose_name=_("VRF"),
    )
    prefix = CidrField(_("prefix"))
    site = models.ForeignKey(
        "locations.Site",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="prefixes",
        verbose_name=_("site"),
    )
    vlan = models.ForeignKey(
        "networks.VLAN",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prefixes",
        verbose_name=_("VLAN"),
    )
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    description = models.CharField(_("description"), max_length=200, blank=True)
    gateway = InetField(_("gateway"), null=True, blank=True)
    # Policy scope: the site, or the tenant.
    path = LtreeField(editable=False)
    text = models.GeneratedField(
        expression=Cast("prefix", models.TextField()),
        output_field=models.TextField(),
        db_persist=True,
    )

    policy_object_type = "ipam.prefix"
    condition_attrs = frozenset({"created_by", "status"})

    class Meta:
        ordering = ["vrf__name", "prefix"]
        constraints = [
            models.UniqueConstraint(
                "tenant", "vrf", "prefix", nulls_distinct=False, name="prefix_unique"
            )
        ]

    def __str__(self) -> str:
        return str(self.prefix)

    @property
    def policy_path(self) -> str:
        """The stored scope."""
        return str(self.path)

    @property
    def network(self) -> Any:
        """As a Python network (``IPv4Network`` or ``IPv6Network``)."""
        return ipaddress.ip_network(self.prefix)


class IPAddress(TenantScopedModel):
    """An address with its prefix length, optionally on a port."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("active")
        RESERVED = "reserved", _("reserved")
        DHCP = "dhcp", _("DHCP")
        DEPRECATED = "deprecated", _("deprecated")

    vrf = models.ForeignKey(
        VRF,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="addresses",
        verbose_name=_("VRF"),
    )
    address = InetField(_("address"))
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    dns_name = models.CharField(_("DNS name"), max_length=255, blank=True)
    description = models.CharField(_("description"), max_length=200, blank=True)
    port = models.ForeignKey(
        "dcim.Port",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ip_addresses",
        verbose_name=_("port"),
    )
    # Policy scope: the device of the port, or the tenant.
    path = LtreeField(editable=False)
    host = models.GeneratedField(
        expression=Func(F("address"), function="host", output_field=models.TextField()),
        output_field=models.TextField(),
        db_persist=True,
    )

    policy_object_type = "ipam.ipaddress"
    condition_attrs = frozenset({"created_by", "status"})

    class Meta:
        ordering = ["vrf__name", "address"]
        constraints = [
            models.UniqueConstraint(
                "tenant", "vrf", "host", nulls_distinct=False, name="ipaddress_unique"
            )
        ]

    def __str__(self) -> str:
        return str(self.address)

    @property
    def policy_path(self) -> str:
        """The stored scope."""
        return str(self.path)

    @property
    def interface(self) -> Any:
        """As a Python interface (address with network)."""
        return ipaddress.ip_interface(self.address)
