"""User lifecycle: first run setup, invitations, locking, sessions, impersonation."""

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import password_validation, update_session_auth_hash
from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.middleware import IMPERSONATION_STARTED, IMPERSONATOR
from patchmanager.accounts.models import Assignment, Invitation, Role, SetupToken, User, UserSession
from patchmanager.accounts.roles import SUPER_ADMIN
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.tenancy.models import Tenant

SETUP_TOKEN_TTL = timedelta(hours=1)


def require(actor: Any, permission: str, obj: Any = None, *, scope_path: str | None = None) -> None:
    """Raise :class:`PermissionDeniedError` unless the policy allows."""
    if not policy.has_perm(actor, permission, obj, scope_path=scope_path):
        raise PermissionDeniedError(_("You do not have permission to do that."))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- First run setup ------------------------------------------------------


def issue_setup_token() -> str:
    """Create a one hour setup token (printed by ``manage.py setup_token``)."""
    if User.objects.exists():
        raise ServiceError("Setup is already complete.")
    token = secrets.token_urlsafe(24)
    SetupToken.objects.all().delete()
    SetupToken.objects.create(token_hash=_hash(token), expires_at=timezone.now() + SETUP_TOKEN_TTL)
    return token


def run_setup(
    request: HttpRequest, *, token: str, email: str, display_name: str, password: str
) -> User:
    """Create the first account as Super Admin. Only possible while no user exists."""
    with transaction.atomic():
        valid = SetupToken.objects.select_for_update().filter(
            token_hash=_hash(token), expires_at__gt=timezone.now()
        )
        if not valid.exists() or User.objects.exists():
            raise ServiceError(_("The setup token is invalid or expired."))
        candidate = User(email=email.lower(), display_name=display_name)
        password_validation.validate_password(password, candidate)
        user = User.objects.create_user(email=email, password=password, display_name=display_name)
        Assignment.objects.create(
            role=Role.objects.get(key=SUPER_ADMIN), user=user, scope_label="global"
        )
        SetupToken.objects.all().delete()
        policy.bump_version()
        audit.record("system.setup.completed", request=request, actor=user, obj=user)
    return user


# --- Invitations ----------------------------------------------------------


def invite(
    request: HttpRequest,
    actor: User,
    *,
    email: str,
    display_name: str,
    role: Role,
    tenant: Tenant | None,
) -> tuple[User, str]:
    """Create an inactive account with a role assignment and an invitation link."""
    scope = tenant.policy_path if tenant else None
    require(actor, "accounts.user.add", scope_path=scope)
    from patchmanager.accounts.services import rbac

    rbac.check_can_grant(actor, role, scope)
    token = secrets.token_urlsafe(32)
    try:
        with transaction.atomic():
            user = User.objects.create_user(email=email, password=None, display_name=display_name)
            Assignment.objects.create(
                role=role,
                user=user,
                scope_path=scope,
                tenant=tenant,
                scope_label=str(tenant) if tenant else "global",
                created_by=actor,
            )
            Invitation.objects.create(
                user=user,
                token_hash=_hash(token),
                invited_by=actor,
                expires_at=timezone.now() + timedelta(hours=settings.INVITATION_TTL_HOURS),
            )
    except IntegrityError as exc:
        raise ServiceError(_("A user with this email already exists.")) from exc
    policy.bump_version()
    audit.record(
        "accounts.user.invited",
        request=request,
        obj=user,
        data={"role": role.key, "tenant": str(scope)},
    )
    from patchmanager.accounts.tasks import send_invitation

    link = f"{settings.BASE_URL}/invite/{token}/"
    transaction.on_commit(lambda: send_invitation.delay(str(user.pk), link))
    return user, link


def invitation_user(token: str) -> User | None:
    """The user a valid invitation token belongs to."""
    invitation = (
        Invitation.objects.select_related("user")
        .filter(token_hash=_hash(token), expires_at__gt=timezone.now())
        .first()
    )
    return invitation.user if invitation else None


def accept_invitation(request: HttpRequest, token: str, password: str) -> User:
    """Set the password and consume the invitation."""
    user = invitation_user(token)
    if user is None:
        raise ServiceError(_("This invitation is invalid or expired."))
    password_validation.validate_password(password, user)
    user.set_password(password)
    user.save(update_fields=["password", "updated_at"])
    Invitation.objects.filter(user=user).delete()
    audit.record("accounts.user.invitation_accepted", request=request, actor=user, obj=user)
    return user


# --- Administration -------------------------------------------------------


def set_locked(request: HttpRequest, actor: User, user: User, *, locked: bool) -> None:
    """Lock (indefinitely) or unlock an account."""
    require(actor, "accounts.user.lock", user)
    if user.pk == actor.pk:
        raise ServiceError(_("You cannot lock your own account."))
    user.locked_until = timezone.now() + timedelta(days=36500) if locked else None
    user.failed_login_count = 0
    user.save(update_fields=["locked_until", "failed_login_count", "updated_at"])
    if locked:
        _revoke(user)
    audit.record(
        "accounts.user.locked" if locked else "accounts.user.unlocked", request=request, obj=user
    )


def set_break_glass(request: HttpRequest, actor: User, user: User, *, on: bool) -> None:
    """Allow (or stop allowing) a password sign in where SSO is enforced. Super admins only."""
    if not policy.has_perm(actor, "system.sso.manage"):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    if user.is_service:
        raise ServiceError(_("API clients do not sign in."))
    User.objects.filter(pk=user.pk).update(break_glass=on)
    audit.record("accounts.user.break_glass", request=request, obj=user, data={"on": on})


def set_active(request: HttpRequest, actor: User, user: User, *, active: bool) -> None:
    """Deactivate (deprovision) or reactivate an account."""
    require(actor, "accounts.user.change", user)
    if user.pk == actor.pk:
        raise ServiceError(_("You cannot deactivate your own account."))
    user.is_active = active
    user.deactivated_at = None if active else timezone.now()
    user.save(update_fields=["is_active", "deactivated_at", "updated_at"])
    if not active:
        _revoke(user)
    audit.record(
        "accounts.user.reactivated" if active else "accounts.user.deactivated",
        request=request,
        obj=user,
    )


def reset_mfa(request: HttpRequest, actor: User, user: User) -> None:
    """Remove every second factor; the user must enrol again."""
    require(actor, "accounts.user.reset_mfa", user)
    user.totp_devices.all().delete()
    user.webauthn_credentials.all().delete()
    user.recovery_codes.all().delete()
    _revoke(user)
    audit.record("accounts.user.mfa_reset", request=request, obj=user)


def _revoke(user: User, *, keep: str | None = None) -> int:
    sessions = UserSession.objects.filter(user=user)
    if keep:
        sessions = sessions.exclude(session_key=keep)
    deleted, _rows = sessions.delete()
    return deleted


def revoke_sessions(request: HttpRequest, actor: User, user: User) -> int:
    """Sign a user out everywhere."""
    if actor.pk != user.pk:
        require(actor, "accounts.user.revoke_sessions", user)
    count = _revoke(user, keep=request.session.session_key if actor.pk == user.pk else None)
    audit.record("accounts.user.sessions_revoked", request=request, obj=user, data={"count": count})
    return count


def revoke_session(request: HttpRequest, user: User, session_key: str) -> None:
    """Sign out one of the user's own sessions."""
    deleted, _rows = UserSession.objects.filter(user=user, session_key=session_key).delete()
    if not deleted:
        raise ServiceError(_("Session not found."))
    audit.record("accounts.user.session_revoked", request=request, obj=user)


def change_password(request: HttpRequest, user: User, *, current: str, new: str) -> None:
    """Change the own password; other sessions end."""
    if not user.check_password(current):
        audit.record("accounts.user.password_change_failed", request=request, obj=user)
        raise ServiceError(_("The current password is incorrect."))
    password_validation.validate_password(new, user)
    user.set_password(new)
    user.save(update_fields=["password", "updated_at"])
    update_session_auth_hash(request, user)
    _revoke(user, keep=request.session.session_key)
    audit.record("accounts.user.password_changed", request=request, obj=user)


def update_profile(request: HttpRequest, user: User, *, display_name: str, language: str) -> None:
    """Change own display name and language."""
    user.display_name = display_name
    user.language = language
    user.save(update_fields=["display_name", "language", "updated_at"])
    audit.record("accounts.user.profile_changed", request=request, obj=user)


# --- Impersonation --------------------------------------------------------


def require_rights_of(actor: User, target: User) -> None:
    """Acting as ``target`` (or holding its credentials) must not yield more than ``actor`` holds.

    The target's grants include nested groups, and filters such as the MFA rule
    for API clients are ignored: those grants count as soon as the rule changes.
    """
    if policy.is_super_admin(actor):
        return
    for grant in policy._compile(target, unfiltered=True):
        if grant.effect != "allow":
            continue
        if grant.role_key == SUPER_ADMIN or any(
            not policy.has_perm(actor, perm, scope_path=grant.scope_path)
            for perm in grant.permissions
        ):
            raise PermissionDeniedError(_("You cannot act as a user with rights you do not hold."))


def start_impersonation(request: HttpRequest, actor: User, target: User, reason: str) -> None:
    """Act as ``target`` for at most 30 minutes, with a visible banner. Audited."""
    require(actor, "accounts.user.impersonate", target)
    if not reason.strip():
        raise ServiceError(_("A reason is required."))
    if target.pk == actor.pk or policy.is_super_admin(target):
        raise ServiceError(_("This user cannot be impersonated."))
    if not target.is_active or target.is_locked:
        raise ServiceError(_("Inactive or locked users cannot be impersonated."))
    require_rights_of(actor, target)
    from django.contrib.auth import login

    from patchmanager.accounts.services.auth import BACKEND

    audit.record(
        "accounts.user.impersonation_started",
        request=request,
        obj=target,
        data={"reason": reason[:500]},
    )
    session = {
        k: request.session[k]
        for k in ("_pm_created", "_pm_last", "_pm_ip", "_pm_ua")
        if k in request.session
    }
    login(request, target, backend=BACKEND)
    request.session.update(session)
    request.session[IMPERSONATOR] = str(actor.pk)
    request.session[IMPERSONATION_STARTED] = timezone.now().isoformat()


def stop_impersonation(request: HttpRequest) -> User | None:
    """Return to the impersonator's own account (signs out if it is gone)."""
    from django.contrib.auth import login, logout

    from patchmanager.accounts.services.auth import BACKEND

    impersonator = (
        User.objects.filter(pk=str(request.session.get(IMPERSONATOR, ""))).first()
        if request.session.get(IMPERSONATOR)
        else None
    )
    target = request.user
    audit.record(
        "accounts.user.impersonation_stopped", request=request, actor=impersonator, obj=target
    )
    if impersonator is None or not impersonator.is_active:
        logout(request)
        return None
    session = {
        k: request.session[k]
        for k in ("_pm_created", "_pm_last", "_pm_ip", "_pm_ua")
        if k in request.session
    }
    login(request, impersonator, backend=BACKEND)
    request.session.update(session)
    request.session.pop(IMPERSONATOR, None)
    request.session.pop(IMPERSONATION_STARTED, None)
    return impersonator
