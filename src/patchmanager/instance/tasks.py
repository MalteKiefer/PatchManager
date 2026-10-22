"""Housekeeping jobs of the instance app."""

from celery import shared_task

from patchmanager.instance import jobs


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]
def prune_job_runs() -> int:
    """Remove old job monitor rows."""
    return jobs.prune()
