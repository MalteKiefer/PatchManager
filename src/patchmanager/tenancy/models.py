"""Tenant model and the abstract base for tenant scoped records."""

from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import path_label
from patchmanager.core.models import TimestampedModel

slug_validator = RegexValidator(
    r"^[a-z0-9][a-z0-9-]{1,62}$", _("Lowercase letters, digits, dashes.")
)


class Tenant(TimestampedModel):
    """A customer. Every domain record belongs to exactly one tenant."""

    slug = models.CharField(_("identifier"), max_length=63, validators=[slug_validator])
    name = models.CharField(_("name"), max_length=200)
    address = models.TextField(_("address"), blank=True)
    contact_name = models.CharField(_("contact person"), max_length=200, blank=True)
    contact_email = models.EmailField(_("contact email"), blank=True)
    requires_mfa = models.BooleanField(_("require MFA for members"), default=False)
    archived_at = models.DateTimeField(_("archived at"), null=True, blank=True)

    policy_object_type = "tenancy.tenant"

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("slug"), name="tenant_slug_unique")]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Scope path used by the policy engine."""
        return path_label(self.pk)


class TenantScopedModel(TimestampedModel):
    """Base for every tenant owned record. RLS is enabled by its migration."""

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True
