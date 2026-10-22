"""Organisations (customers, suppliers, providers), their contacts, systems and links.

A link puts any documented object (a device, a building, a VLAN, a configuration ...)
into a system or assigns it to an organisation, for example as its customer.
"""

from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import path_label
from patchmanager.tenancy.models import TenantScopedModel


class Organization(TenantScopedModel):
    """A company or department the documentation refers to."""

    class Kind(models.TextChoices):
        CUSTOMER = "customer", _("customer")
        SUPPLIER = "supplier", _("supplier")
        PROVIDER = "provider", _("provider (lines, hosting)")
        SERVICE = "service", _("service partner")
        INTERNAL = "internal", _("internal department")
        OTHER = "other", _("other")

    name = models.CharField(_("name"), max_length=150)
    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices, default=Kind.CUSTOMER)
    number = models.CharField(_("customer or account number"), max_length=64, blank=True)
    website = models.URLField(_("website"), blank=True)
    phone = models.CharField(_("phone"), max_length=50, blank=True)
    email = models.EmailField(_("email"), blank=True)
    address = models.TextField(_("address"), blank=True)
    notes = models.TextField(_("notes"), blank=True)

    policy_object_type = "directory.organization"

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint("tenant", Lower("name"), name="organization_name_unique")
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Tenant wide."""
        return path_label(self.tenant_id)


class Contact(TenantScopedModel):
    """A person at an organisation."""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="contacts",
        verbose_name=_("organization"),
    )
    name = models.CharField(_("name"), max_length=150)
    role = models.CharField(_("role"), max_length=100, blank=True)
    email = models.EmailField(_("email"), blank=True)
    phone = models.CharField(_("phone"), max_length=50, blank=True)
    notes = models.TextField(_("notes"), blank=True)

    policy_object_type = "directory.organization"

    class Meta:
        ordering = ["organization", "name"]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Governed by its organisation."""
        return path_label(self.tenant_id)


class System(TenantScopedModel):
    """Something that is made of several documented objects, for example a phone system."""

    class Kind(models.TextChoices):
        NETWORK = "network", _("network")
        TELEPHONY = "telephony", _("telephony")
        BUILDING = "building", _("building automation")
        SECURITY = "security", _("video and access control")
        PRINTING = "printing", _("printing")
        SERVERS = "servers", _("servers and storage")
        APPLICATION = "application", _("application")
        WIRELESS = "wireless", _("wireless")
        OTHER = "other", _("other")

    class Criticality(models.TextChoices):
        LOW = "low", _("low")
        NORMAL = "normal", _("normal")
        HIGH = "high", _("high")
        CRITICAL = "critical", _("critical")

    name = models.CharField(_("name"), max_length=150)
    kind = models.CharField(_("kind"), max_length=12, choices=Kind.choices, default=Kind.OTHER)
    customer = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="systems",
        verbose_name=_("customer"),
    )
    site = models.ForeignKey(
        "locations.Site",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("site"),
    )
    criticality = models.CharField(
        _("criticality"), max_length=8, choices=Criticality.choices, default=Criticality.NORMAL
    )
    contact = models.ForeignKey(
        Contact,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("contact person"),
    )
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "directory.system"

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint("tenant", Lower("name"), name="system_name_unique")]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Tenant wide."""
        return path_label(self.tenant_id)


class Link(TenantScopedModel):
    """An object that belongs to a system, or to an organisation in a role."""

    class Role(models.TextChoices):
        PART = "part", _("part of")
        CUSTOMER = "customer", _("customer")
        OWNER = "owner", _("owner")
        OPERATOR = "operator", _("operator")
        MAINTAINER = "maintainer", _("maintenance")
        SUPPLIER = "supplier", _("supplier")
        USER = "user", _("user")

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, related_name="+")
    object_id = models.UUIDField(db_index=True)
    organization = models.ForeignKey(
        Organization,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="links",
        verbose_name=_("organization"),
    )
    system = models.ForeignKey(
        System,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="links",
        verbose_name=_("system"),
    )
    role = models.CharField(_("role"), max_length=10, choices=Role.choices, default=Role.PART)

    class Meta:
        ordering = ["role"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(organization__isnull=True, system__isnull=False)
                | models.Q(organization__isnull=False, system__isnull=True),
                name="link_one_owner",
            ),
            models.UniqueConstraint(
                "content_type",
                "object_id",
                "organization",
                "system",
                "role",
                nulls_distinct=False,
                name="link_unique",
            ),
        ]
        indexes = [models.Index(fields=["content_type", "object_id"], name="link_target")]

    def __str__(self) -> str:
        return f"{self.get_role_display()} {self.organization or self.system}"
