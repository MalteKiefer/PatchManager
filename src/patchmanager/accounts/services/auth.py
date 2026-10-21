"""Password login, lockout, rate limiting and session start."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.db.models import F
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager.accounts import sessions
from patchmanager.accounts.middleware import MFA_SETUP_REQUIRED
from patchmanager.accounts.models import Role, User
from patchmanager.accounts.roles import SYSTEM_ROLES
from patchmanager.audit import services as audit
from patchmanager.core import metrics, ratelimit
from patchmanager.tenancy.models import Tenant
from patchmanager.tenancy.rls import system_context

if TYPE_CHECKING:
    from patchmanager.sso.models import IdentityProvider

logger = logging.getLogger(__name__)

PENDING_USER = "_pm_pending_user"
PENDING_AT = "_pm_pending_at"
PENDING_TTL_SECONDS = 300
MFA_METHOD = "_pm_mfa_method"
BACKEND = "patchmanager.accounts.backends.EmailBackend"


class Outcome(StrEnum):
    """Result of the password step."""

    OK = "ok"
    MFA = "mfa"
    INVALID = "invalid"
    LOCKED = "locked"
    THROTTLED = "throttled"


@dataclass(frozen=True)
class LoginResult:
    """Password step result. ``message`` is safe to display."""

    outcome: Outcome
    message: str = ""


def _ip(request: HttpRequest) -> str:
    return str(request.META.get("REMOTE_ADDR", "unknown"))


def password_step(request: HttpRequest, email: str, password: str) -> LoginResult:
    """Check credentials. On success either log in or hand over to the MFA step."""
    if not ratelimit.hit(f"login-ip:{_ip(request)}", limit=settings.LOGIN_RATE_PER_IP, window=600):
        audit.record("auth.login.throttled", request=request, data={"email": email[:254]})
        metrics.LOGINS.labels("throttled").inc()
        return LoginResult(Outcome.THROTTLED, _("Too many attempts. Try again in a few minutes."))
    candidate = User.objects.filter(email__iexact=email.strip()).first()
    if candidate is not None and candidate.is_locked:
        audit.record("auth.login.locked", request=request, obj=candidate)
        metrics.LOGINS.labels("locked").inc()
        return LoginResult(Outcome.LOCKED, _("This account is temporarily locked."))
    directory = _directory_for(email, candidate)
    if directory is not None:
        return _directory_step(request, directory, email, password, candidate)
    user = authenticate(request, email=email, password=password)
    if user is None:
        if candidate is not None:
            _register_failure(candidate)
        audit.record(
            "auth.login.failed", request=request, obj=candidate, data={"email": email[:254]}
        )
        metrics.LOGINS.labels("failed").inc()
        return LoginResult(Outcome.INVALID, _("Email or password is incorrect."))
    assert isinstance(user, User)  # noqa: S101  # nosec B101: EmailBackend only returns User
    User.objects.filter(pk=user.pk).update(failed_login_count=0)
    if has_mfa(user):
        begin_second_factor(request, user)
        return LoginResult(Outcome.MFA)
    complete_login(request, user, method="password")
    return LoginResult(Outcome.OK)


def _directory_for(email: str, candidate: User | None) -> IdentityProvider | None:
    """The LDAP provider of the email's domain (break glass accounts keep their password)."""
    from patchmanager.sso.models import IdentityProvider
    from patchmanager.sso.services import provider_for

    if candidate is not None and candidate.break_glass:
        return None
    return provider_for(email, IdentityProvider.Kind.LDAP)


def _directory_step(
    request: HttpRequest,
    provider: IdentityProvider,
    email: str,
    password: str,
    candidate: User | None,
) -> LoginResult:
    from patchmanager.accounts.services import ServiceError
    from patchmanager.sso import ldap, services

    try:
        assertion = ldap.authenticate(provider, email, password)
        if assertion is not None:
            outcome = services.sign_in(request, provider, assertion)
            return LoginResult(Outcome.MFA if outcome == "mfa" else Outcome.OK)
    except ServiceError as exc:
        audit.record(
            "auth.login.failed",
            request=request,
            obj=candidate,
            data={"email": email[:254], "ldap": str(exc)},
        )
        metrics.LOGINS.labels("failed").inc()
        return LoginResult(Outcome.INVALID, str(exc))
    if candidate is not None:
        _register_failure(candidate)
    audit.record(
        "auth.login.failed",
        request=request,
        obj=candidate,
        data={"email": email[:254], "ldap": True},
    )
    metrics.LOGINS.labels("failed").inc()
    return LoginResult(Outcome.INVALID, _("Email or password is incorrect."))


def begin_second_factor(request: HttpRequest, user: User) -> None:
    """The first factor is done; the MFA step finishes the sign in."""
    request.session.cycle_key()
    request.session[PENDING_USER] = str(user.pk)
    request.session[PENDING_AT] = timezone.now().isoformat()


def _register_failure(user: User) -> None:
    User.objects.filter(pk=user.pk).update(failed_login_count=F("failed_login_count") + 1)
    user.refresh_from_db(fields=["failed_login_count"])
    if user.failed_login_count >= settings.LOGIN_MAX_FAILURES:
        until = timezone.now() + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
        User.objects.filter(pk=user.pk).update(locked_until=until, failed_login_count=0)
        audit.record(
            "auth.account.locked", obj=user, data={"until": until.isoformat(), "reason": "failures"}
        )


def pending_user(request: HttpRequest) -> User | None:
    """The user who passed the password step and still owes a second factor."""
    user_id = request.session.get(PENDING_USER)
    started = request.session.get(PENDING_AT)
    if not user_id or not started:
        return None
    age = timezone.now() - datetime.fromisoformat(started)
    if age.total_seconds() > PENDING_TTL_SECONDS:
        clear_pending(request)
        return None
    user = User.objects.filter(pk=user_id).first()
    return user if user is not None and user.is_active and not user.is_locked else None


def clear_pending(request: HttpRequest) -> None:
    """Forget a half finished login."""
    request.session.pop(PENDING_USER, None)
    request.session.pop(PENDING_AT, None)


def complete_login(
    request: HttpRequest, user: User, *, method: str, mfa_done: bool = False
) -> None:
    """Start the authenticated session and record it.

    Without a second factor (``password``, SSO, LDAP) users who need MFA must
    enrol one first, unless ``mfa_done`` (an identity provider that enforces MFA).
    """
    clear_pending(request)
    login(request, user, backend=BACKEND)
    now = timezone.now().isoformat()
    request.session[sessions.CREATED] = now
    request.session[sessions.LAST_ACTIVITY] = now
    request.session[sessions.IP] = request.META.get("REMOTE_ADDR")
    request.session[sessions.USER_AGENT] = request.headers.get("User-Agent", "")[:300]
    request.session[MFA_METHOD] = method
    single_factor = method == "password" or method.startswith(("sso:", "ldap:"))
    if single_factor and not mfa_done and mfa_required(user):
        request.session[MFA_SETUP_REQUIRED] = True
    ratelimit.reset(f"login-ip:{_ip(request)}")
    audit.record(
        "auth.login.success", request=request, actor=user, obj=user, data={"method": method}
    )
    metrics.LOGINS.labels("success").inc()


def logout_user(request: HttpRequest) -> None:
    """End the session and record it."""
    if request.user.is_authenticated:
        audit.record("auth.logout", request=request, obj=request.user)
    logout(request)


def has_mfa(user: User) -> bool:
    """True if the user has at least one confirmed second factor."""
    return user.totp_devices.filter(confirmed=True).exists() or user.webauthn_credentials.exists()


def mfa_required(user: User) -> bool:
    """MFA is required globally, by any assigned role, or by any tenant the user belongs to."""
    if settings.MFA_REQUIRED_FOR_ALL:
        return True
    from patchmanager.accounts import policy

    keys = {g.role_key for g in policy.grants_for(user) if g.effect == "allow"}
    if any(SYSTEM_ROLES[key].requires_mfa for key in keys if key in SYSTEM_ROLES):
        return True
    if Role.objects.filter(key__in=keys, requires_mfa=True).exists():
        return True
    all_tenants, tenant_ids = policy.accessible_tenants(user)
    with system_context():
        tenants = Tenant.objects.filter(requires_mfa=True)
        return (tenants if all_tenants else tenants.filter(pk__in=tenant_ids)).exists()


def mfa_satisfied(request: HttpRequest) -> None:
    """Called after the first factor is enrolled: lift the enrolment gate."""
    request.session.pop(MFA_SETUP_REQUIRED, None)
