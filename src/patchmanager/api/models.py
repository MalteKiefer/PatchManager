"""API credentials: personal tokens, OAuth clients and their access tokens."""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from patchmanager.core.models import TimestampedModel

SCOPES = (("read", _("Read")), ("write", _("Write")))


class ApiClient(TimestampedModel):
    """An OAuth 2.0 client (client credentials grant), acting as its own service user."""

    name = models.CharField(_("name"), max_length=100)
    client_id = models.CharField(max_length=40, unique=True)
    secret_hash = models.CharField(max_length=128)  # Argon2id
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_client"
    )
    scopes = models.JSONField(default=list)
    # Optional limits on top of the user's own rights: object types (empty: all) and a
    # location subtree (empty: everywhere the user may act).
    object_types = models.JSONField(default=list, blank=True)
    scope_path = models.CharField(max_length=500, blank=True)
    tenant = models.ForeignKey(
        "tenancy.Tenant", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    policy_object_type = "accounts.apiclient"
    policy_path = None

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ApiToken(TimestampedModel):
    """A bearer token. Only a hash is stored; the prefix finds the row."""

    class Kind(models.TextChoices):
        PERSONAL = "personal", _("personal token")
        OAUTH = "oauth", _("OAuth access token")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens"
    )
    client = models.ForeignKey(
        ApiClient, null=True, blank=True, on_delete=models.CASCADE, related_name="tokens"
    )
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PERSONAL)
    name = models.CharField(_("name"), max_length=100)
    prefix = models.CharField(max_length=16, unique=True)
    token_hash = models.CharField(max_length=64)
    scopes = models.JSONField(default=list)
    # Optional limits on top of the user's own rights: object types (empty: all) and a
    # location subtree (empty: everywhere the user may act).
    object_types = models.JSONField(default=list, blank=True)
    scope_path = models.CharField(max_length=500, blank=True)
    tenant = models.ForeignKey(
        "tenancy.Tenant", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "kind"])]

    def __str__(self) -> str:
        return self.name

    @property
    def usable(self) -> bool:
        """Not revoked and not expired."""
        return self.revoked_at is None and self.expires_at > timezone.now()
