"""Audit events. Rows are never updated or deleted (enforced by a trigger)."""

from django.db import models


class AuditEvent(models.Model):
    """One security relevant event, chained to its predecessor by SHA-256."""

    seq = models.BigAutoField(primary_key=True)
    occurred_at = models.DateTimeField(db_index=True)
    action = models.CharField(max_length=100, db_index=True)
    actor_id = models.UUIDField(null=True, db_index=True)
    actor_label = models.CharField(max_length=254, blank=True)
    tenant_id = models.UUIDField(null=True, db_index=True)
    object_type = models.CharField(max_length=100, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    object_label = models.CharField(max_length=300, blank=True)
    data = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True)
    user_agent = models.CharField(max_length=300, blank=True)
    request_id = models.CharField(max_length=64, blank=True)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    policy_object_type = "audit.event"
    policy_path = None

    class Meta:
        ordering = ["-seq"]

    def __str__(self) -> str:
        return f"{self.seq} {self.action}"
