"""Personal data: a copy for the person (GDPR Art. 15, 20) and anonymisation (Art. 17).

The audit log is append only and hash chained; it keeps the email address and IP
addresses of past actions as the security record (ADR 0024).
"""

from typing import Any

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import Assignment, Invitation, User, UserSession
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require, require_rights_of
from patchmanager.audit import services as audit
from patchmanager.audit.models import AuditEvent
from patchmanager.history.models import Change
from patchmanager.tenancy import rls

ANONYMOUS = "Anonymised user"


def _when(value: Any) -> str | None:
    return value.isoformat() if value else None


def export(user: User) -> dict[str, Any]:
    """Everything stored about ``user`` as plain data (no secrets or hashes)."""
    with rls.system_context():
        events = AuditEvent.objects.filter(actor_id=user.pk).order_by("seq")
        changes = Change.objects.filter(actor_id=user.pk).count()
        return {
            "exported_at": timezone.now().isoformat(),
            "profile": {
                "id": str(user.pk),
                "email": user.email,
                "display_name": user.display_name,
                "language": user.language,
                "active": user.is_active,
                "created_at": _when(user.created_at),
                "last_login": _when(user.last_login),
                "break_glass": user.break_glass,
                "scim_external_id": user.scim_external_id,
            },
            "groups": sorted(g.name for g in user.pm_groups.all()),
            "assignments": [
                {
                    "role": a.role.name,
                    "effect": a.effect,
                    "scope": a.scope_label,
                    "expires_at": _when(a.expires_at),
                }
                for a in Assignment.objects.filter(user=user).select_related("role")
            ],
            "sessions": [
                {
                    "ip_address": s.ip_address,
                    "user_agent": s.user_agent,
                    "created_at": _when(s.created_at),
                    "last_activity": _when(s.last_activity),
                }
                for s in UserSession.objects.filter(user=user)
            ],
            "second_factors": {
                "totp": [
                    {"name": d.name, "created_at": _when(d.created_at)}
                    for d in user.totp_devices.all()
                ],
                "webauthn": [
                    {
                        "name": c.name,
                        "created_at": _when(c.created_at),
                        "last_used_at": _when(c.last_used_at),
                    }
                    for c in user.webauthn_credentials.all()
                ],
                "unused_recovery_codes": user.recovery_codes.filter(used_at=None).count(),
            },
            "api_tokens": [
                {
                    "name": t.name,
                    "scopes": t.scopes,
                    "created_at": _when(t.created_at),
                    "expires_at": _when(t.expires_at),
                    "revoked_at": _when(t.revoked_at),
                    "last_used_at": _when(t.last_used_at),
                    "last_used_ip": t.last_used_ip,
                }
                for t in user.api_tokens.all()
            ],
            "sign_in_links": [
                {"provider": i.provider.name, "subject": i.subject}
                for i in user.external_identities.select_related("provider")
            ],
            "saved_searches": [
                {"name": s.name, "query": s.query} for s in user.saved_searches.all()
            ],
            "changes_made": changes,
            "audit_events": [
                {
                    "at": _when(e.occurred_at),
                    "action": e.action,
                    "object": e.object_label,
                    "ip_address": e.ip_address,
                    "user_agent": e.user_agent,
                }
                for e in events
            ],
        }


def anonymise(request: HttpRequest, actor: User, user: User) -> None:
    """Remove the person from an account and keep the account as an anonymous author.

    Documentation the person changed keeps its history, attributed to the anonymous
    account. Sign in data, sessions, second factors, tokens, links and roles go.
    """
    require(actor, "accounts.user.delete", user)
    if user.pk == actor.pk:
        raise ServiceError(_("You cannot anonymise your own account."))
    if user.is_service:
        raise ServiceError(_("Delete the API client instead."))
    require_rights_of(actor, user)
    email = f"anonymised-{user.pk.hex[:16]}@invalid"
    now = timezone.now()
    with transaction.atomic():
        User.objects.filter(pk=user.pk).update(
            email=email,
            display_name=ANONYMOUS,
            password=make_password(None),
            is_active=False,
            deactivated_at=now,
            locked_until=None,
            break_glass=False,
            scim_provisioned=False,
            scim_external_id="",
            scim_deactivated=False,
            updated_at=now,
        )
        user.pm_groups.clear()
        Assignment.objects.filter(user=user).delete()
        UserSession.objects.filter(user=user).delete()
        Invitation.objects.filter(user=user).delete()
        user.totp_devices.all().delete()
        user.webauthn_credentials.all().delete()
        user.recovery_codes.all().delete()
        user.api_tokens.all().delete()
        user.external_identities.all().delete()
        user.saved_searches.all().delete()
        with rls.system_context():
            Change.objects.filter(actor_id=user.pk).update(actor_label=email)
    policy.bump_version()
    user.refresh_from_db()
    audit.record("accounts.user.anonymised", request=request, obj=user)
