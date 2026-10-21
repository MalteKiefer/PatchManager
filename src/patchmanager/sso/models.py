"""Identity providers (OIDC, SAML 2.0, LDAP) and the accounts linked to them."""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.models import TimestampedModel


class IdentityProvider(TimestampedModel):
    """An external source of sign ins, configured by super admins."""

    class Kind(models.TextChoices):
        OIDC = "oidc", "OpenID Connect"
        SAML = "saml", "SAML 2.0"
        LDAP = "ldap", "LDAP"

    name = models.CharField(_("name"), max_length=100)
    slug = models.SlugField(_("identifier"), max_length=50, unique=True)
    kind = models.CharField(_("type"), max_length=10, choices=Kind.choices)
    enabled = models.BooleanField(_("enabled"), default=False)
    domains = ArrayField(models.CharField(max_length=253), default=list, blank=True)
    enforce = models.BooleanField(_("enforce for these domains"), default=False)
    jit = models.BooleanField(_("create accounts on first sign in"), default=True)
    mfa_trusted = models.BooleanField(_("the provider enforces MFA"), default=False)
    config = models.JSONField(default=dict, blank=True)  # non secret settings, see sso.forms
    encrypted_secret = models.TextField(blank=True)  # OIDC client secret or LDAP bind password
    group_mappings = models.JSONField(default=dict, blank=True)  # external group value -> Group id

    policy_object_type = "system.sso"
    policy_path = None

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ExternalIdentity(TimestampedModel):
    """A user's account at a provider (OIDC ``sub``, SAML NameID or LDAP entry id)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="external_identities")
    provider = models.ForeignKey(IdentityProvider, on_delete=models.CASCADE, related_name="identities")
    subject = models.CharField(max_length=500)
    last_login_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["provider", "subject"], name="sso_identity_unique")]

    def __str__(self) -> str:
        return f"{self.provider}: {self.subject}"
