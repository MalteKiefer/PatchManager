"""IP address management: VRFs, prefixes and addresses on ports."""

import ipaddress
import re
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Func
from django.db.models.functions import Cast, Lower
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


# --- DHCP and DNS ---------------------------------------------------------------


_HOST = re.compile(
    r"^(?=.{1,254}$)([A-Za-z0-9_](-*[A-Za-z0-9_])*\.)*[A-Za-z0-9_](-*[A-Za-z0-9_])*\.?$|^@$"
)


class DHCPScope(TenantScopedModel):
    """Addresses a DHCP server hands out in a prefix, with the options it sends."""

    name = models.CharField(_("name"), max_length=100)
    prefix = models.ForeignKey(
        Prefix, on_delete=models.PROTECT, related_name="dhcp_scopes", verbose_name=_("prefix")
    )
    server = models.ForeignKey(
        "dcim.Device",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("DHCP server"),
    )
    start = InetField(_("first address"))
    end = InetField(_("last address"))
    gateway = InetField(_("gateway"), null=True, blank=True)
    dns_servers = models.CharField(
        _("DNS servers"), max_length=200, blank=True, help_text=_("Separated by commas.")
    )
    domain_name = models.CharField(_("domain name"), max_length=253, blank=True)
    lease_seconds = models.PositiveIntegerField(
        _("lease time (seconds)"), default=86400, validators=[MinValueValidator(60)]
    )
    options = models.TextField(
        _("further options"),
        blank=True,
        help_text=_("One per line as code=value, for example 66=tftp.example.com."),
    )
    relays = models.CharField(
        _("relay addresses"),
        max_length=200,
        blank=True,
        help_text=_("Where routers forward requests (ip helper-address), separated by commas."),
    )
    enabled = models.BooleanField(_("enabled"), default=True)
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "ipam.dhcpscope"

    class Meta:
        ordering = ["prefix__prefix", "start"]
        constraints = [models.UniqueConstraint("tenant", "name", name="dhcpscope_name_unique")]

    def __str__(self) -> str:
        return f"{self.name} ({self.start} - {self.end})"

    @property
    def policy_path(self) -> str:
        """Tenant wide."""
        return path_label(self.tenant_id)

    def clean(self) -> None:
        """The range lies inside the prefix and is in order; lists hold addresses."""
        super().clean()
        if not (self.prefix_id and self.start and self.end):
            return
        network = ipaddress.ip_network(str(self.prefix.prefix))
        try:
            first, last = (
                ipaddress.ip_address(str(self.start).split("/")[0]),
                ipaddress.ip_address(str(self.end).split("/")[0]),
            )
        except ValueError as exc:
            raise ValidationError(_("Enter valid addresses.")) from exc
        if first not in network or last not in network or int(first) > int(last):
            raise ValidationError(_("The range must lie inside the prefix, first before last."))
        if self.gateway and ipaddress.ip_address(str(self.gateway).split("/")[0]) not in network:
            raise ValidationError(_("The gateway must lie inside the prefix."))
        for name in ("dns_servers", "relays"):
            for item in filter(None, (s.strip() for s in getattr(self, name).split(","))):
                try:
                    ipaddress.ip_address(item)
                except ValueError as exc:
                    raise ValidationError(
                        {name: _("%(value)s is not an address.") % {"value": item}}
                    ) from exc
        for line in filter(None, (s.strip() for s in self.options.splitlines())):
            code, sep, _value = line.partition("=")
            if not sep or not code.strip().isdigit() or not 1 <= int(code) <= 254:
                raise ValidationError({"options": _("Write each option as code=value.")})

    def option_list(self) -> list[tuple[int, str]]:
        """The further options as (code, value)."""
        result = []
        for line in filter(None, (s.strip() for s in self.options.splitlines())):
            code, _sep, value = line.partition("=")
            result.append((int(code), value.strip()))
        return result


class DHCPReservation(TenantScopedModel):
    """A fixed address for one MAC address in a scope."""

    scope = models.ForeignKey(
        DHCPScope, on_delete=models.CASCADE, related_name="reservations", verbose_name=_("scope")
    )
    mac_address = models.CharField(
        _("MAC address"),
        max_length=17,
        validators=[
            RegexValidator(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", _("Use aa:bb:cc:dd:ee:ff."))
        ],
    )
    address = InetField(_("address"))
    hostname = models.CharField(_("hostname"), max_length=253, blank=True)
    description = models.CharField(_("description"), max_length=200, blank=True)

    policy_object_type = "ipam.dhcpscope"

    class Meta:
        ordering = ["scope", "address"]
        constraints = [
            models.UniqueConstraint("scope", "mac_address", name="dhcpreservation_mac_unique"),
            models.UniqueConstraint("scope", "address", name="dhcpreservation_address_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.address} {self.mac_address}"

    @property
    def policy_path(self) -> str:
        """Governed by its scope."""
        return path_label(self.tenant_id)

    def clean(self) -> None:
        """The address lies in the scope's prefix; MAC addresses are stored with colons."""
        super().clean()
        self.mac_address = self.mac_address.lower().replace("-", ":")
        if self.scope_id and self.address:
            try:
                host = ipaddress.ip_address(str(self.address).split("/")[0])
            except ValueError as exc:
                raise ValidationError(_("Enter a valid address.")) from exc
            if host not in ipaddress.ip_network(str(self.scope.prefix.prefix)):
                raise ValidationError(_("The address must lie inside the prefix of the scope."))


class DNSZone(TenantScopedModel):
    """A DNS zone (forward or reverse) with its records."""

    name = models.CharField(
        _("name"),
        max_length=253,
        validators=[
            RegexValidator(
                r"^(?=.{1,253}$)([A-Za-z0-9_](-*[A-Za-z0-9_])*\.)*[A-Za-z0-9_](-*[A-Za-z0-9_])*\.?$",
                _("Enter a domain name like example.com."),
            )
        ],
    )
    primary_server = models.CharField(_("primary name server"), max_length=253, blank=True)
    admin_email = models.CharField(_("responsible email"), max_length=253, blank=True)
    default_ttl = models.PositiveIntegerField(_("default TTL (seconds)"), default=3600)
    serial = models.PositiveBigIntegerField(_("serial"), default=1)
    server = models.ForeignKey(
        "dcim.Device",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("DNS server"),
    )
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "ipam.dnszone"

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint("tenant", Lower("name"), name="dnszone_name_unique")]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Tenant wide."""
        return path_label(self.tenant_id)


class DNSRecord(TenantScopedModel):
    """One resource record of a zone."""

    class Type(models.TextChoices):
        A = "A", "A"
        AAAA = "AAAA", "AAAA"
        CNAME = "CNAME", "CNAME"
        MX = "MX", "MX"
        TXT = "TXT", "TXT"
        PTR = "PTR", "PTR"
        SRV = "SRV", "SRV"
        NS = "NS", "NS"
        CAA = "CAA", "CAA"

    zone = models.ForeignKey(
        DNSZone, on_delete=models.CASCADE, related_name="records", verbose_name=_("zone")
    )
    name = models.CharField(
        _("name"),
        max_length=253,
        help_text=_("Relative to the zone; @ for the zone itself."),
        validators=[
            RegexValidator(
                r"^(@|\*|[A-Za-z0-9_*]([A-Za-z0-9_.-]*[A-Za-z0-9_])?)$", _("Enter a host name.")
            )
        ],
    )
    type = models.CharField(_("type"), max_length=5, choices=Type.choices, default=Type.A)
    value = models.CharField(_("value"), max_length=1000)
    ttl = models.PositiveIntegerField(_("TTL (seconds)"), null=True, blank=True)
    priority = models.PositiveIntegerField(
        _("priority"), null=True, blank=True, validators=[MaxValueValidator(65535)]
    )
    address = models.ForeignKey(
        IPAddress,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="dns_records",
        verbose_name=_("documented address"),
    )

    policy_object_type = "ipam.dnszone"

    class Meta:
        ordering = ["zone", "name", "type"]

    def __str__(self) -> str:
        return f"{self.name} {self.type} {self.value}"

    @property
    def policy_path(self) -> str:
        """Governed by its zone."""
        return path_label(self.tenant_id)

    def clean(self) -> None:
        """A and AAAA hold an address of their family; no line breaks anywhere."""
        super().clean()
        if any(c in self.value for c in "\r\n\x00"):
            raise ValidationError({"value": _("The value must be one line.")})
        if self.type in (self.Type.A, self.Type.AAAA):
            try:
                host = ipaddress.ip_address(self.value)
            except ValueError as exc:
                raise ValidationError({"value": _("Enter an address.")}) from exc
            if (host.version == 4) != (self.type == self.Type.A):
                raise ValidationError({"value": _("A records take IPv4, AAAA records IPv6.")})
        if self.type in (self.Type.MX, self.Type.SRV) and self.priority is None:
            raise ValidationError({"priority": _("MX and SRV records need a priority.")})
        target = self.value.split()[-1] if self.type == self.Type.SRV else self.value
        if self.type in (
            self.Type.CNAME,
            self.Type.MX,
            self.Type.PTR,
            self.Type.NS,
            self.Type.SRV,
        ) and not _HOST.match(target):
            raise ValidationError({"value": _("Enter a host name.")})
        if self.type == self.Type.SRV and not re.fullmatch(r"\d{1,5} \d{1,5} \S+", self.value):
            raise ValidationError({"value": _("SRV records take weight, port and target.")})

    def save(self, *args: Any, **kwargs: Any) -> None:
        """A and AAAA records point at the documented address with the same host."""
        if self.type in (self.Type.A, self.Type.AAAA) and self.address_id is None:
            self.address = IPAddress.objects.filter(
                tenant_id=self.tenant_id, host=self.value
            ).first()
        super().save(*args, **kwargs)
