"""Housekeeping jobs of the instance app."""

from celery import shared_task

from patchmanager.instance import jobs, offsite


@shared_task(ignore_result=True, time_limit=offsite.TIMEOUT + 60)  # type: ignore[untyped-decorator]
def offsite_copy(force: bool = False) -> bool:
    """Copy the backup volume off-site when a new dump is there (or when forced)."""
    return offsite.run(force=force)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]
def prune_job_runs() -> int:
    """Remove old job monitor rows."""
    return jobs.prune()
