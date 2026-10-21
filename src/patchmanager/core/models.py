"""Abstract base models shared by every domain app."""

import uuid

from django.db import models


class UUIDModel(models.Model):
    """Primary key is a time-ordered UUIDv7 (RFC 9562), generated in Python."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)

    class Meta:
        abstract = True


class TimestampedModel(UUIDModel):
    """Adds creation and modification timestamps."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
