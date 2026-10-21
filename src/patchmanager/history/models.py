"""Change history, written by database triggers (see migration 0002)."""

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import LtreeField


class Change(models.Model):
    """One insert, update or delete of a tracked row, with before and after state."""

    class Action(models.TextChoices):
        INSERT = "insert", _("created")
        UPDATE = "update", _("changed")
        DELETE = "delete", _("deleted")

    id = models.UUIDField(primary_key=True)  # uuidv7() in the trigger
    occurred_at = models.DateTimeField(db_index=True)
    tenant_id = models.UUIDField(null=True, db_index=True)
    object_type = models.CharField(max_length=50)  # model label, for example "dcim.port"
    object_id = models.UUIDField()
    label = models.CharField(max_length=300, blank=True)
    path = LtreeField(null=True)  # policy scope of the object at that time
    action = models.CharField(max_length=10, choices=Action.choices)
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)
    fields = ArrayField(models.CharField(max_length=100), default=list)
    actor_id = models.UUIDField(null=True)
    actor_label = models.CharField(max_length=254, blank=True)
    request_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id", "occurred_at"])]

    def __str__(self) -> str:
        return f"{self.object_type} {self.object_id} {self.action}"
