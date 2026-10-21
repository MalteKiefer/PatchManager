"""System information page of the admin area."""

import platform

import django
from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from patchmanager import __version__
from patchmanager.accounts.decorators import permission_required


@permission_required("system.settings.view")
def system_view(request: HttpRequest) -> HttpResponse:
    """Versions and effective security configuration (read only)."""
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version")
        pg_version = cursor.fetchone()[0]
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
            "security": {
                "MFA required for everyone": settings.MFA_REQUIRED_FOR_ALL,
                "Minimum password length": settings.PASSWORD_MIN_LENGTH,
                "Session lifetime (s)": settings.SESSION_MAX_AGE,
                "Session idle timeout (s)": settings.SESSION_IDLE_TIMEOUT,
                "Lockout after failures": settings.LOGIN_MAX_FAILURES,
                "Lockout duration (s)": settings.LOGIN_LOCKOUT_SECONDS,
                "Login attempts per IP / 10 min": settings.LOGIN_RATE_PER_IP,
                "WebAuthn relying party": settings.WEBAUTHN_RP_ID,
                "Background jobs": "eager (no broker)"
                if settings.CELERY_TASK_ALWAYS_EAGER
                else "Celery",
            },
        },
    )
