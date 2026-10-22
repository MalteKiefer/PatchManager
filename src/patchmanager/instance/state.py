"""Cached snapshot of the instance settings (read on many requests, changed rarely)."""

import logging
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError

logger = logging.getLogger(__name__)
CACHE_KEY = "instance:state"
CACHE_SECONDS = 60


@dataclass(frozen=True)
class Snapshot:
    """What pages and checks need, without the logo bytes."""

    name: str = ""
    brand_hue: int = 190
    has_logo: bool = False
    default_language: str = "en"
    version: str = ""  # changes with every save, for cache busting of branding assets


def get() -> Snapshot:
    """The current settings (defaults while none are stored)."""
    snapshot: Snapshot | None = cache.get(CACHE_KEY)
    if snapshot is None:
        from patchmanager.instance.models import InstanceSettings

        try:
            row = InstanceSettings.objects.defer("logo").first()
        except DatabaseError:
            logger.exception("instance settings unreadable, using defaults")
            return Snapshot(default_language=settings.LANGUAGE_CODE[:2])  # error pages still render
        snapshot = (
            Snapshot(
                name=row.name,
                brand_hue=row.brand_hue,
                has_logo=InstanceSettings.objects.filter(pk=row.pk, logo__isnull=False).exists(),
                default_language=row.default_language,
                version=str(int(row.updated_at.timestamp())),
            )
            if row
            else Snapshot(default_language=settings.LANGUAGE_CODE[:2])
        )
        cache.set(CACHE_KEY, snapshot, CACHE_SECONDS)
    return snapshot


def forget() -> None:
    """Drop the cached snapshot after a change."""
    cache.delete(CACHE_KEY)


def instance_name() -> str:
    """Name shown in the layout, mails and reports."""
    return get().name or settings.INSTANCE_NAME
