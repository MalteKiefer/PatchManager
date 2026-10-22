"""System status of the admin area: versions, health, jobs, backups, mail and licenses."""

import json
import platform
import time
from datetime import UTC, datetime, timedelta
from importlib import metadata
from pathlib import Path
from typing import Any

import django
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.core.mail import send_mail
from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from patchmanager import __version__
from patchmanager.accounts.decorators import permission_required
from patchmanager.instance import config as instance_config
from patchmanager.instance import state

VENDORED = [  # front end assets shipped in core/static/vendor
    ("htmx", "4.0.0", "0BSD"),
    ("Alpine.js (CSP build)", "3.17.5", "MIT"),
    ("Leaflet", "1.9.4", "BSD-2-Clause"),
    ("Device type catalog (netbox-community/devicetype-library)", "bundled data", "CC0-1.0"),
]


def _check(label: Any, probe: Any) -> dict[str, Any]:
    started = time.monotonic()
    try:
        detail = probe()
        ok = True
    except Exception as exc:  # a health page reports every failure instead of failing itself
        detail, ok = type(exc).__name__, False
    return {
        "label": label,
        "ok": ok,
        "detail": detail,
        "ms": round((time.monotonic() - started) * 1000),
    }


def _database() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return _("reachable")


def _migrations() -> str:
    pending = MigrationExecutor(connection).migration_plan(
        MigrationExecutor(connection).loader.graph.leaf_nodes()
    )
    if pending:
        raise RuntimeError(f"{len(pending)} pending")
    return _("up to date")


def _cache() -> str:
    cache.set("health:probe", "ok", 10)
    if cache.get("health:probe") != "ok":
        raise RuntimeError("cache lost the value")
    return _("working")


def _storage() -> str:
    store = storages["attachments"]
    name = store.save("health/probe.txt", ContentFile(b"ok"))
    store.delete(name)
    return _("writable")


def _worker() -> str:
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return _("jobs run in the web process (no broker)")
    from patchmanager.worker import app

    replies = app.control.ping(timeout=1.0)
    if not replies:
        raise RuntimeError("no worker answered")
    return _("%(n)s worker(s) answering") % {"n": len(replies)}


def health() -> list[dict[str, Any]]:
    """Checks of the services the application depends on."""
    return [
        _check(gettext_lazy("Database"), _database),
        _check(gettext_lazy("Migrations"), _migrations),
        _check(gettext_lazy("Cache and rate limits"), _cache),
        _check(gettext_lazy("File storage"), _storage),
        _check(gettext_lazy("Background workers"), _worker),
    ]


def backups() -> dict[str, Any]:
    """Restore points in the mounted backup volume, newest first."""
    if not settings.BACKUP_DIR:
        return {"configured": False, "files": []}
    folder = Path(settings.BACKUP_DIR)
    try:
        files = sorted(
            (p for p in folder.glob("patchmanager-*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return {"configured": True, "readable": False, "files": []}
    rows: list[dict[str, Any]] = [
        {
            "name": p.name,
            "size": p.stat().st_size,
            "at": datetime.fromtimestamp(p.stat().st_mtime, tz=UTC),
        }
        for p in files[:30]
    ]
    newest: datetime | None = rows[0]["at"] if rows else None
    stale = newest is None or timezone.now() - newest > timedelta(hours=36)
    return {
        "configured": True,
        "readable": True,
        "files": rows,
        "stale": stale,
        "offsite": _offsite(folder),
    }


def _offsite(folder: Path) -> dict[str, Any] | None:
    """Last run of the restic off-site copy (deploy/offsite.sh), if it ever ran."""
    try:
        raw = (folder / ".offsite.json").read_bytes()[:4096]
        data = json.loads(raw)
        at = datetime.fromisoformat(data["at"])
    except FileNotFoundError:
        return None
    except OSError, ValueError, KeyError, TypeError:
        return {"ok": False, "at": None, "message": _("The off-site status file cannot be read.")}
    return {"ok": data.get("ok") is True, "at": at, "message": str(data.get("message", ""))[:200]}


def jobs() -> dict[str, Any]:
    """Recent job runs, failures and the schedule."""
    from patchmanager.instance.models import JobRun
    from patchmanager.integrations.models import Delivery
    from patchmanager.tenancy import rls

    since = timezone.now() - timedelta(hours=24)
    with rls.system_context():
        deliveries = {s: Delivery.objects.filter(status=s).count() for s in ("pending", "failed")}
    return {
        "recent": JobRun.objects.all()[:25],
        "failures": JobRun.objects.filter(status="failure", started_at__gte=since).count(),
        "schedule": sorted(
            (name, entry["task"], entry["schedule"])
            for name, entry in settings.CELERY_BEAT_SCHEDULE.items()
        ),
        "deliveries": deliveries,
    }


@permission_required("system.settings.view")
def system_view(request: HttpRequest) -> HttpResponse:
    """Versions, health, jobs, backups, mail and the effective security configuration."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW server_version")
            pg_version = cursor.fetchone()[0]
    except DatabaseError:
        pg_version = "?"
    current = instance_config.get()
    return render(
        request,
        "manage/system.html",
        {
            "versions": {
                "PatchManager": __version__,
                "Django": django.get_version(),
                "Python": platform.python_version(),
                "PostgreSQL": pg_version,
            },
            "health": health(),
            "jobs": jobs(),
            "backups": backups(),
            "s3_bucket": current.s3_bucket,
            "mail": {
                "backend": settings.MAILERS["default"]["BACKEND"],
                "host": current.mail_host,
                "sender": current.mail_from,
            },
            "security": [
                (gettext_lazy("MFA required for everyone"), current.mfa_required),
                (gettext_lazy("Minimum password length"), current.password_min_length),
                (gettext_lazy("Session lifetime (s)"), current.session_max_age),
                (gettext_lazy("Session idle timeout (s)"), current.session_idle_timeout),
                (gettext_lazy("Lockout after failures"), current.login_max_failures),
                (gettext_lazy("Lockout duration (s)"), current.login_lockout_seconds),
                (gettext_lazy("Login attempts per IP in 10 minutes"), current.login_rate_per_ip),
                (gettext_lazy("WebAuthn relying party"), settings.WEBAUTHN_RP_ID),
            ],
        },
    )


@permission_required("system.settings.change")
@require_POST
def test_mail(request: HttpRequest) -> HttpResponse:
    """Send a test mail to the signed in admin."""
    email = str(getattr(request.user, "email", ""))
    email = str(getattr(request.user, "email", ""))
    try:
        send_mail(
            subject=_("Test mail from %(name)s") % {"name": state.instance_name()},
            message=_("Mail delivery works."),
            from_email=None,
            recipient_list=[email],
        )
    except OSError as exc:
        messages.error(
            request, _("The mail could not be sent: %(error)s") % {"error": type(exc).__name__}
        )
    else:
        messages.success(request, _("A test mail was sent to %(email)s.") % {"email": email})
    if request.POST.get("back") == "settings":
        return redirect("instance:settings", "mail")
    return redirect("manage:system")


def _license(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return str(expression)
    classifiers = [
        c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")
    ]
    if classifiers:
        return ", ".join(classifiers)
    text = str(meta.get("License") or "")
    return text if len(text) < 60 else text.splitlines()[0][:60]


@login_required
def licenses(request: HttpRequest) -> HttpResponse:
    """The application's licence, its source, and the licences of what it is built from."""
    packages = sorted(
        {
            (d.metadata["Name"], d.version, _license(d))
            for d in metadata.distributions()
            if d.metadata["Name"]
        },
        key=lambda p: p[0].lower(),
    )
    return render(
        request,
        "core/licenses.html",
        {
            "packages": packages,
            "vendored": VENDORED,
            "source_url": settings.SOURCE_URL,
            "version": __version__,
        },
    )
