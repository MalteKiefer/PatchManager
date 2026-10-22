"""Scheduled synchronisation of platform connections (Celery beat, see CELERY_BEAT_SCHEDULE)."""

from celery import shared_task

from patchmanager.compute import connections


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]
def sync_connections() -> int:
    """Hand every due connection to its own task, each with its own time limit."""
    claimed = connections.due()
    for pk in claimed:
        sync_connection.delay(str(pk))
    return len(claimed)


# Each import also stops at its own deadline (inventory.DEADLINE) before this limit.
@shared_task(ignore_result=True, soft_time_limit=600, time_limit=660)  # type: ignore[untyped-decorator]
def sync_connection(pk: str) -> bool:
    """Update one connection."""
    return connections.sync_one(pk)
