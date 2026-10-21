"""Webhooks and their deliveries. Deliveries are created by a trigger on the history."""

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import path_label
from patchmanager.core.models import TimestampedModel


class Webhook(TimestampedModel):
    """POSTs signed change events of one tenant to a URL."""

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.CASCADE, related_name="+")
    name = models.CharField(_("name"), max_length=100)
    url = models.URLField(_("URL"), max_length=500)
    encrypted_secret = models.TextField()
    events = ArrayField(
        models.CharField(max_length=60), default=list
    )  # patterns like "dcim.device.*"
    active = models.BooleanField(_("active"), default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    policy_object_type = "integrations.webhook"

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    @property
    def policy_path(self) -> str:
        """Webhooks belong to their tenant."""
        return path_label(self.tenant_id)


class Delivery(TimestampedModel):
    """One event for one webhook, retried with backoff."""

    class Status(models.TextChoices):
        PENDING = "pending", _("pending")
        SENT = "sent", _("sent")
        FAILED = "failed", _("failed")

    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.CASCADE, related_name="+")
    webhook = models.ForeignKey(Webhook, on_delete=models.CASCADE, related_name="deliveries")
    change_id = models.UUIDField(null=True)  # None for test pings
    event = models.CharField(max_length=80)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(db_index=True)
    last_status_code = models.PositiveSmallIntegerField(null=True)
    last_error = models.CharField(max_length=300, blank=True)
    delivered_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self) -> str:
        return f"{self.event} → {self.webhook_id}"
