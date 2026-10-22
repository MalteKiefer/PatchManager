"""Login, second factor, logout, first run setup and invitations."""

import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts.forms import CodeForm, LoginForm, SetPasswordForm, SetupForm
from patchmanager.accounts.services import ServiceError, auth, mfa, users
from patchmanager.audit import services as audit
from patchmanager.core import ratelimit

NEXT = "_pm_next"
logger = logging.getLogger(__name__)


def _safe_next(request: HttpRequest) -> str:
    target = request.session.pop(NEXT, "") or request.GET.get("next", "")
    if target and url_has_allowed_host_and_scheme(
        target, {request.get_host()}, require_https=request.is_secure()
    ):
        return str(target)
    return "/"


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    """Password step."""
    if request.user.is_authenticated:
        return redirect("core:home")
    if request.GET.get("next"):
        request.session[NEXT] = request.GET["next"]
    form = LoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        sso_start = _enforced_sso(form.cleaned_data["email"])
        if sso_start is not None and not _may_use_password(form.cleaned_data["email"]):
            return redirect(sso_start)  # domain discovery: this domain signs in at its provider
        result = auth.password_step(
            request, form.cleaned_data["email"], form.cleaned_data["password"]
        )
        if sso_start is not None and result.outcome not in (auth.Outcome.OK, auth.Outcome.MFA):
            return redirect(sso_start)  # a wrong break glass password looks like any other account
        if result.outcome == auth.Outcome.OK:
            return redirect(_safe_next(request))
        if result.outcome == auth.Outcome.MFA:
            return redirect("accounts:mfa")
        form.add_error(None, result.message)
    from patchmanager.instance import editions
    from patchmanager.sso.models import IdentityProvider

    providers = IdentityProvider.objects.filter(enabled=True).exclude(
        kind=IdentityProvider.Kind.LDAP
    )
    if not editions.enabled("sso"):
        providers = providers.none()
    return render(request, "accounts/login.html", {"form": form, "providers": providers})


def _enforced_sso(email: str) -> str | None:
    """Start URL of the identity provider enforced for this email's domain."""
    from patchmanager.sso.services import enforced_for

    provider = enforced_for(email)
    if provider is None:
        return None
    return f"{reverse('sso:login', args=[provider.slug])}?{urlencode({'login_hint': email})}"


def _may_use_password(email: str) -> bool:
    """Break glass accounts keep their password where SSO is enforced."""
    from patchmanager.accounts.models import User

    return User.objects.filter(email__iexact=email.strip(), break_glass=True).exists()


@require_http_methods(["GET", "POST"])
def mfa_view(request: HttpRequest) -> HttpResponse:
    """Second factor step: TOTP, recovery code or security key."""
    user = auth.pending_user(request)
    if user is None:
        messages.info(request, _("Please sign in again."))
        return redirect("accounts:login")
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not ratelimit.hit(f"mfa:{user.pk}", limit=5, window=300):
            auth.clear_pending(request)
            audit.record("auth.mfa.throttled", request=request, obj=user)
            messages.error(request, _("Too many attempts. Please sign in again."))
            return redirect("accounts:login")
        code = form.cleaned_data["code"]
        method = (
            "totp"
            if mfa.totp_verify(user, code)
            else "recovery"
            if mfa.use_recovery_code(user, code)
            else ""
        )
        if method:
            ratelimit.reset(f"mfa:{user.pk}")
            auth.complete_login(request, user, method=method)
            if method == "recovery":
                messages.warning(request, _("You used a recovery code. Generate new ones soon."))
            return redirect(_safe_next(request))
        audit.record("auth.mfa.failed", request=request, obj=user)
        form.add_error("code", _("This code is not valid."))
    context = {
        "form": form,
        "has_totp": user.totp_devices.filter(confirmed=True).exists(),
        "has_webauthn": user.webauthn_credentials.exists(),
    }
    return render(request, "accounts/mfa.html", context)


@require_POST
def webauthn_login_options(request: HttpRequest) -> JsonResponse:
    """Assertion options: for the pending user (2FA) or any passkey (passwordless)."""
    pending = auth.pending_user(request)
    return JsonResponse(mfa.webauthn_authentication_options(request, pending), safe=False)


@require_POST
def webauthn_login_verify(request: HttpRequest) -> JsonResponse:
    """Verify an assertion and finish the login."""
    pending = auth.pending_user(request)
    try:
        user = mfa.webauthn_authenticate(request, pending, request.body.decode())
    except ServiceError as exc:
        logger.info("webauthn sign in rejected", extra={"reason": str(exc)})
        return JsonResponse({"error": _("The security key could not be verified.")}, status=400)
    if not user.is_active or user.is_locked or user.deactivated_at:
        return JsonResponse({"error": _("This account cannot sign in.")}, status=403)
    from patchmanager.sso.services import sso_required

    if pending is None and sso_required(user):  # a passkey must not bypass enforced SSO
        return JsonResponse(
            {"error": _("Sign in with your organisation's identity provider.")}, status=403
        )
    auth.complete_login(request, user, method="webauthn" if pending else "passkey")
    return JsonResponse({"redirect": _safe_next(request)})


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    """End the session, and the identity provider's session if it supports single logout."""
    from patchmanager.sso.views import single_logout_url

    upstream = single_logout_url(request)
    auth.logout_user(request)
    messages.info(request, _("You have been signed out."))
    return redirect(upstream) if upstream else redirect("accounts:login")


@require_http_methods(["GET", "POST"])
def setup_view(request: HttpRequest) -> HttpResponse:
    """First run wizard; only reachable while no account exists."""
    form = SetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            user = users.run_setup(
                request,
                token=form.cleaned_data["token"],
                email=form.cleaned_data["email"],
                display_name=form.cleaned_data["display_name"],
                password=form.cleaned_data["password"],
            )
        except ServiceError as exc:
            form.add_error("token", str(exc))
        except ValidationError as exc:  # password policy
            form.add_error("password", exc)
        else:
            auth.complete_login(request, user, method="password")
            messages.success(
                request, _("Setup complete. Now protect your account with a second factor.")
            )
            return redirect("accounts:security")
    return render(request, "accounts/setup.html", {"form": form})


@require_http_methods(["GET", "POST"])
def invitation_view(request: HttpRequest, token: str) -> HttpResponse:
    """Accept an invitation by choosing a password."""
    invited = users.invitation_user(token)
    if invited is None:
        return render(
            request,
            "core/error.html",
            {"title": _("Invitation invalid or expired"), "code": 404},
            status=404,
        )
    form = SetPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            users.accept_invitation(request, token, form.cleaned_data["password"])
        except ValidationError as exc:
            form.add_error("password", exc)
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Your password is set. Please sign in."))
            return redirect("accounts:login")
    return render(request, "accounts/invitation.html", {"form": form, "invited": invited})
