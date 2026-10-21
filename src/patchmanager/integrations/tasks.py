"""Scheduled webhook work (Celery beat runs these, see CELERY_BEAT_SCHEDULE)."""

from celery import shared_task

from patchmanager.integrations import services


# Killed before the lease ends: a receiver that trickles bytes cannot cause a second delivery.
@shared_task(
    ignore_result=True,
    soft_time_limit=services.SEND_WINDOW + 30,
    time_limit=services.SEND_WINDOW + 60,
)  # type: ignore[untyped-decorator]
def deliver() -> int:
    """Send due webhook deliveries."""
    return services.deliver_due()


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]
def prune() -> int:
    """Remove old deliveries."""
    return services.prune()
