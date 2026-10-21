"""Saved searches (per user)."""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from patchmanager.core.models import TimestampedModel


class SavedSearch(TimestampedModel):
    """A named query and type filter a user can run again."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_searches")
    name = models.CharField(_("name"), max_length=100)
    query = models.CharField(max_length=200)
    kind = models.CharField(max_length=30, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["user", "name"], name="search_saved_unique_name")]

    def __str__(self) -> str:
        return self.name
