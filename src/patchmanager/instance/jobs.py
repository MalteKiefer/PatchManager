"""Record background job runs for the job monitor (Celery signals)."""

from datetime import timedelta
from typing import Any

from celery.signals import task_failure, task_postrun, task_prerun
from django.utils import timezone

KEEP_DAYS = 3


def _model() -> Any:
    from patchmanager.instance.models import JobRun

    return JobRun


@task_prerun.connect(weak=False)  # type: ignore[untyped-decorator]
def started(task_id: str | None = None, task: Any = None, **kwargs: Any) -> None:
    """A job began."""
    if task_id:
        _model().objects.update_or_create(
            task_id=task_id,
            defaults={
                "name": getattr(task, "name", "?"),
                "started_at": timezone.now(),
                "status": "running",
            },
        )


@task_postrun.connect(weak=False)  # type: ignore[untyped-decorator]
def finished(task_id: str | None = None, state: str | None = None, **kwargs: Any) -> None:
    """A job ended (success or failure, see ``state``)."""
    if task_id:
        _model().objects.filter(task_id=task_id).update(
            finished_at=timezone.now(), status="failure" if state == "FAILURE" else "success"
        )


@task_failure.connect(weak=False)  # type: ignore[untyped-decorator]
def failed(
    task_id: str | None = None, exception: BaseException | None = None, **kwargs: Any
) -> None:
    """Keep the error class (messages can hold data, so only the type)."""
    if task_id:
        _model().objects.filter(task_id=task_id).update(
            status="failure", error=type(exception).__name__[:300]
        )


def prune() -> int:
    """Forget runs older than a few days."""
    deleted, _rows = (
        _model().objects.filter(started_at__lt=timezone.now() - timedelta(days=KEEP_DAYS)).delete()
    )
    return int(deleted)
