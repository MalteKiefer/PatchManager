"""Scheduled synchronisation of platform connections (Celery beat, see CELERY_BEAT_SCHEDULE)."""

from celery import shared_task

from patchmanager.compute import connections


# Each import stops after its own deadline; the limit covers several connections in a row.
@shared_task(ignore_result=True, soft_time_limit=1800, time_limit=1900)  # type: ignore[untyped-decorator]
def sync_connections() -> int:
    """Update every connection whose interval has passed."""
    return connections.sync_due()
