"""Session lifetime, first run setup and MFA enrolment gates."""

from collections.abc import Callable
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from patchmanager.accounts import sessions

ALWAYS_OPEN = ("/static/", "/health/", "/metrics", "/i18n/")
MFA_SETUP_REQUIRED = "_pm_mfa_setup"
IMPERSONATOR = "_pm_impersonator"
IMPERSONATION_STARTED = "_pm_impersonation_started"
IMPERSONATION_SECONDS = 30 * 60

Handler = Callable[[HttpRequest], HttpResponse]


class SessionPolicyMiddleware:
    """Enforce absolute and idle timeouts; expose impersonation state."""

    def __init__(self, get_response: Handler) -> None:
        """Standard middleware constructor."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Expire stale sessions, refresh activity at most once a minute."""
        if request.user.is_authenticated:
            now = timezone.now()
            created = parse_datetime(request.session.get(sessions.CREATED, "")) or now
            last = parse_datetime(request.session.get(sessions.LAST_ACTIVITY, "")) or now
            if now - created > timedelta(
                seconds=settings.SESSION_MAX_AGE
            ) or now - last > timedelta(seconds=settings.SESSION_IDLE_TIMEOUT):
                logout(request)
                messages.info(request, _("Your session expired. Please sign in again."))
                return redirect(settings.LOGIN_URL)
            if now - last > timedelta(seconds=60):
                request.session[sessions.LAST_ACTIVITY] = now.isoformat()
            self._impersonation(request, now)
        return self.get_response(request)

    @staticmethod
    def _impersonation(request: HttpRequest, now: datetime) -> None:
        from patchmanager.accounts.models import User

        impersonator_id = request.session.get(IMPERSONATOR)
        if not impersonator_id:
            return
        started = parse_datetime(request.session.get(IMPERSONATION_STARTED, "")) or now
        if now - started > timedelta(seconds=IMPERSONATION_SECONDS):
            from patchmanager.accounts.services import users

            users.stop_impersonation(request)
            return
        request.impersonator = User.objects.filter(pk=impersonator_id).first()  # type: ignore[attr-defined]


class SetupRequiredMiddleware:
    """Until the first account exists, every page leads to the setup wizard."""

    _done = False

    def __init__(self, get_response: Handler) -> None:
        """Standard middleware constructor."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Redirect to setup, or hide setup once it is complete."""
        from patchmanager.accounts.models import User

        if request.path.startswith(ALWAYS_OPEN):
            return self.get_response(request)
        if not SetupRequiredMiddleware._done:
            SetupRequiredMiddleware._done = User.objects.exists()
        setup_path = reverse("accounts:setup")
        if SetupRequiredMiddleware._done:
            if request.path == setup_path:
                raise Http404
        elif request.path != setup_path:
            return redirect(setup_path)
        return self.get_response(request)


class MFAEnforcementMiddleware:
    """Users who must use MFA but have none can only reach enrolment."""

    def __init__(self, get_response: Handler) -> None:
        """Standard middleware constructor."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Confine the session to the security page until a factor exists."""
        if request.user.is_authenticated and request.session.get(MFA_SETUP_REQUIRED):
            from patchmanager.accounts.services import auth

            if auth.has_mfa(request.user):
                # Enrolled in another session: this one never proved the second factor.
                auth.logout_user(request)
                messages.info(request, _("Sign in again with your second factor."))
                return redirect(settings.LOGIN_URL)
            allowed = (reverse("accounts:security"), reverse("accounts:logout"), "/account/mfa/")
            if not request.path.startswith(allowed) and not request.path.startswith(ALWAYS_OPEN):
                messages.warning(request, _("Set up a second factor to continue."))
                return redirect("accounts:security")
        return self.get_response(request)
