"""What every provider does after it vouched for someone: link, provision, sync groups, sign in."""

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import Group, User
from patchmanager.accounts.services import ServiceError, auth
from patchmanager.audit import services as audit
from patchmanager.sso.models import ExternalIdentity, IdentityProvider

SSO_PROVIDER = "_pm_sso_provider"  # session: provider of the current sign in
SSO_LOGOUT = "_pm_sso_logout"  # session: what single logout needs (id token, NameID ...)


@dataclass
class Assertion:
    """What a provider tells us about the person."""

    subject: str
    email: str
    name: str = ""
    groups: list[str] = field(default_factory=list)
    email_verified: bool = True
    logout: dict[str, Any] = field(default_factory=dict)


def domain(email: str) -> str:
    """Lower case domain of an email address."""
    return email.rpartition("@")[2].strip().lower()


def provider_for(email: str, *kinds: str) -> IdentityProvider | None:
    """The enabled provider responsible for the email's domain."""
    providers = IdentityProvider.objects.filter(enabled=True, domains__contains=[domain(email)])
    if kinds:
        providers = providers.filter(kind__in=kinds)
    return providers.order_by("name").first()


def sso_required(user: User) -> bool:
    """True if this user must sign in at an identity provider, not with a local factor."""
    return enforced_for(user.email) is not None and not user.break_glass


def enforced_for(email: str) -> IdentityProvider | None:
    """The web SSO provider a password sign in must be replaced with, if any."""
    provider = provider_for(email, IdentityProvider.Kind.OIDC, IdentityProvider.Kind.SAML)
    return provider if provider is not None and provider.enforce else None


def _account(provider: IdentityProvider, assertion: Assertion) -> User:
    """The linked user, an existing one we may link, or a new one (JIT)."""
    identity = (
        ExternalIdentity.objects.select_related("user")
        .filter(provider=provider, subject=assertion.subject)
        .first()
    )
    if identity is not None:
        return identity.user
    email = assertion.email.strip().lower()
    if not email or "@" not in email:
        raise ServiceError(_("The identity provider sent no email address."))
    if not assertion.email_verified:
        raise ServiceError(_("The identity provider did not confirm the email address."))
    if domain(email) not in provider.domains:
        # Linking takes over an account, JIT reserves an address: only the domain's provider may.
        raise ServiceError(_("This identity provider may not sign in addresses of this domain."))
    user = User.objects.filter(email__iexact=email).first()
    if user is None and not provider.jit:
        raise ServiceError(
            _("There is no account for this email. Ask an administrator for access.")
        )
    if user is None:
        user = User.objects.create_user(
            email=email, display_name=(assertion.name or email)[:150], password=None
        )
        audit.record("sso.user.provisioned", obj=user, data={"provider": provider.slug})
    ExternalIdentity.objects.create(user=user, provider=provider, subject=assertion.subject[:500])
    audit.record("sso.identity.linked", obj=user, data={"provider": provider.slug})
    return user


def sync_groups(provider: IdentityProvider, user: User, external: list[str]) -> None:
    """Mapped groups follow the provider: added when sent, removed when not. Others stay."""
    mapped = {str(k): str(v) for k, v in provider.group_mappings.items()}
    if not mapped:
        return
    wanted = {mapped[g] for g in external if g in mapped}
    managed = set(mapped.values())
    current = {str(pk) for pk in user.pm_groups.filter(pk__in=managed).values_list("pk", flat=True)}
    if wanted == current:
        return
    user.pm_groups.add(*Group.objects.filter(pk__in=wanted - current))
    user.pm_groups.remove(*Group.objects.filter(pk__in=current - wanted))
    policy.bump_version()
    audit.record(
        "sso.groups.synced",
        obj=user,
        data={
            "provider": provider.slug,
            "added": sorted(wanted - current),
            "removed": sorted(current - wanted),
        },
    )


def sign_in(request: HttpRequest, provider: IdentityProvider, assertion: Assertion) -> str:
    """Finish a sign in vouched for by ``provider``.

    Returns ``"ok"``, or ``"mfa"`` when a second factor is still owed.
    """
    if not provider.enabled:
        raise ServiceError(_("This sign in method is disabled."))
    with transaction.atomic():
        user = _account(provider, assertion)
        if (
            not user.is_active
            or user.deactivated_at is not None
            or user.is_locked
            or user.is_service
        ):
            audit.record(
                "sso.login.refused", request=request, obj=user, data={"provider": provider.slug}
            )
            raise ServiceError(_("This account cannot sign in."))
        sync_groups(provider, user, assertion.groups)
        ExternalIdentity.objects.filter(provider=provider, user=user).update(
            last_login_at=timezone.now()
        )
    method = f"{'ldap' if provider.kind == IdentityProvider.Kind.LDAP else 'sso'}:{provider.slug}"
    if provider.mfa_trusted:
        auth.complete_login(request, user, method=method, mfa_done=True)
        outcome = "ok"
    elif auth.has_mfa(user):
        auth.begin_second_factor(request, user)
        outcome = "mfa"
    else:
        auth.complete_login(request, user, method=method)
        outcome = "ok"
    request.session[SSO_PROVIDER] = str(provider.pk)
    request.session[SSO_LOGOUT] = assertion.logout
    return outcome
