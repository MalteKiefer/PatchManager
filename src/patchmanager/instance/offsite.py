"""Encrypted off-site copy of the backup volume with restic, configured in the admin area.

The backup service writes the database dumps; the worker copies the volume to S3
compatible storage (AWS S3, Backblaze B2, Wasabi) after each new dump. restic
encrypts on this host with the repository password, deduplicates, applies the
retention and checks the repository. Credentials come from the operational settings
and only reach restic through its environment, never its command line.
"""

import logging
import os
import shutil
import subprocess  # nosec B404: fixed restic arguments, no shell
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager.accounts.services import ServiceError
from patchmanager.instance import config as instance_config

logger = logging.getLogger(__name__)
TIMEOUT = 6 * 3600  # one run, in seconds
DUMP_GLOB = "patchmanager-*.dump"
LOCK = "instance:offsite:running"
STEPS = 5  # restic calls of one copy: cat config, init, backup, forget, check
PARTIAL_AGE = 3600  # seconds since a partial file last grew before it counts as left over


def check_repository(repository: str) -> None:
    """Only ``s3:https://host/bucket[/path]``; the host must not point into this machine."""
    from patchmanager.instance.services import public_host

    if not repository.startswith("s3:https://") or len(repository) > 300:
        raise ServiceError(_("Enter the repository as s3:https://host/bucket/path."))
    url = repository.removeprefix("s3:")
    if url.count("/") < 3 or any(c.isspace() for c in url):
        raise ServiceError(_("Enter the repository as s3:https://host/bucket/path."))
    public_host(url)


def _restic() -> str:
    path = shutil.which("restic")
    if path is None:
        raise ServiceError(_("restic is not installed in this image."))
    return path


def _environment(config: instance_config.Config) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/tmp",  # noqa: S108  # nosec B108: read only containers write only to /tmp
        "RESTIC_REPOSITORY": config.offsite_repository,
        "RESTIC_PASSWORD": config.offsite_password,
        "AWS_ACCESS_KEY_ID": config.offsite_access_key_id,
        "AWS_SECRET_ACCESS_KEY": config.offsite_secret_access_key,
    }


def _scrub(text: str, config: instance_config.Config) -> str:
    """The last line of restic's error output, without any secret."""
    line = (text.strip().splitlines() or [""])[-1]
    for secret in (config.offsite_password, config.offsite_secret_access_key):
        if secret:
            line = line.replace(secret, "***")
    return line[:200]


def _run(config: instance_config.Config, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec B603: fixed arguments, no shell
        # No local cache: /tmp is memory in the read only containers.
        [_restic(), "--no-cache", *args],
        env=_environment(config),
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )


def copy(config: instance_config.Config) -> str:
    """Upload the backup volume once; raise :class:`ServiceError` with restic's reason."""
    check_repository(config.offsite_repository)
    if not (config.offsite_password and config.offsite_access_key_id):
        raise ServiceError(_("Enter the key and the repository password."))
    folder = str(settings.BACKUP_DIR)
    if not folder or not Path(folder).is_dir():
        raise ServiceError(_("The backup volume is not mounted (PATCHMANAGER_BACKUP_DIR)."))
    if _run(config, "cat", "config").returncode != 0:
        initialised = _run(config, "init")
        if initialised.returncode != 0:
            raise ServiceError(_scrub(initialised.stderr, config) or _("restic init failed."))
    steps = [
        [
            "backup",
            "--host",
            "patchmanager",
            "--tag",
            "patchmanager",
            "--exclude",
            "*.partial",
            folder,
        ],
        [
            "forget",
            "--host",
            "patchmanager",
            "--keep-daily",
            str(config.offsite_keep_daily),
            "--keep-weekly",
            str(config.offsite_keep_weekly),
            "--keep-monthly",
            str(config.offsite_keep_monthly),
            "--prune",
        ],
        ["check"],
    ]
    for step in steps:
        result = _run(config, *step)
        if result.returncode != 0:
            raise ServiceError(_scrub(result.stderr, config) or f"restic {step[0]} failed")
    newest = _newest(folder)
    return _("copied %(name)s") % {"name": newest.name if newest else "-"}


def fetch(config: instance_config.Config, name: str, target: str) -> None:
    """Restore one dump and its uploads archive from the latest copy into ``target``."""
    if not name.startswith("patchmanager-") or not name.endswith(".dump") or "/" in name:
        raise ServiceError(_("Not a PatchManager dump: %(name)s") % {"name": name})
    folder = str(settings.BACKUP_DIR or "/backups")
    # "latest:<folder>" restores relative to the backup folder, so the files land directly
    # in ``target`` where the restore service finds them.
    result = _run(
        config,
        "restore",
        f"latest:{folder}",
        "--host",
        "patchmanager",
        "--target",
        target,
        "--include",
        f"/{name}",
        "--include",
        f"/{name}.media.tar",
    )
    if result.returncode != 0:
        raise ServiceError(_scrub(result.stderr, config) or "restic restore failed")


def _newest(folder: str) -> Path | None:
    dumps = sorted(Path(folder).glob(DUMP_GLOB), key=lambda p: p.stat().st_mtime)
    return dumps[-1] if dumps else None


def _record(ok: bool, message: str) -> None:
    from patchmanager.instance.models import InstanceSettings

    data = {"ok": ok, "at": timezone.now().isoformat(), "message": message[:200]}
    # Only this column: an administrator may be saving the settings at the same time.
    if not InstanceSettings.objects.update(offsite_status=data):
        InstanceSettings.objects.create(offsite_status=data)


def status() -> dict[str, Any] | None:
    """Last run for the system page, or None when it never ran."""
    from patchmanager.instance.models import InstanceSettings

    row = InstanceSettings.objects.only("offsite_status").first()
    data = row.offsite_status if row else {}
    if not data:
        return None
    try:
        at = datetime.fromisoformat(data["at"])
    except KeyError, TypeError, ValueError:
        at = None
    return {"ok": data.get("ok") is True, "at": at, "message": str(data.get("message", ""))}


def due(config: instance_config.Config) -> bool:
    """Enabled, and a dump newer than the last successful copy exists."""
    if not config.offsite_enabled or not settings.BACKUP_DIR:
        return False
    folder = Path(str(settings.BACKUP_DIR))
    # A partial file still growing means the backup service is writing; copy the complete
    # set later. One left behind by a failed run is old and must not stop the copies.
    recent = time.time() - PARTIAL_AGE
    if any(p.stat().st_mtime > recent for p in folder.glob("patchmanager-*.partial")):
        return False
    newest = _newest(str(folder))
    if newest is None:
        return False
    last = status()
    if last is None or not last["ok"] or last["at"] is None:
        return True
    copied: datetime = last["at"]
    # The uploads archive is written after its dump, so it decides when the set is new.
    parts = [newest, newest.with_name(newest.name + ".media.tar")]
    written = max(p.stat().st_mtime for p in parts if p.exists())
    return datetime.fromtimestamp(written, tz=UTC) > copied


def run(force: bool = False) -> bool:
    """Copy when due (or forced), record the outcome; True when a copy succeeded.

    Only one copy runs at a time: a copy can take longer than the hourly schedule, and
    "Copy now" or a redelivered task must not start a second restic on the repository.
    """
    config = instance_config.get()
    if not force and not due(config):
        return False
    token = timezone.now().isoformat()
    # A copy runs up to five restic steps of TIMEOUT each; the lock must outlast them all.
    if not cache.add(LOCK, token, STEPS * TIMEOUT + 60):
        logger.info("off-site copy already running")
        return False
    try:
        message = copy(config)
    except (ServiceError, subprocess.TimeoutExpired, OSError) as exc:
        reason = str(exc) if isinstance(exc, ServiceError) else type(exc).__name__
        logger.warning("off-site copy failed: %s", reason)
        _record(False, reason)
        return False
    finally:
        # ponytail: get and delete are two calls; an expired lock retaken in between is
        # freed early. Use a Redis compare-and-delete script if that window ever matters.
        if cache.get(LOCK) == token:
            cache.delete(LOCK)
    _record(True, message)
    return True
