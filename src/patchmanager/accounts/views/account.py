"""Self service: profile, password, second factors, sessions."""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import translation
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts.forms import ChangePasswordForm, CodeForm, NameForm, ProfileForm
from patchmanager.accounts.models import TOTPDevice, User
from patchmanager.accounts.services import ServiceError, auth, mfa, users

RECOVERY_CODES = "_pm_new_recovery_codes"
logger = logging.getLogger(__name__)


def _user(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: login_required guarantees a User
    return request.user


@login_required
@require_http_methods(["GET", "POST"])
def profile_view(request: HttpRequest) -> HttpResponse:
    """Display name, language and password."""
    user = _user(request)
    profile = ProfileForm(
        request.POST if request.POST.get("action") == "profile" else None, instance=user
    )
    password = ChangePasswordForm(
        request.POST if request.POST.get("action") == "password" else None
    )
    if request.POST.get("action") == "profile" and profile.is_valid():
        users.update_profile(
            request,
            user,
            display_name=profile.cleaned_data["display_name"],
            language=profile.cleaned_data["language"],
        )
        translation.activate(user.language)
        messages.success(request, _("Profile saved."))
        response = redirect("accounts:profile")
        response.set_cookie(
            "django_language", user.language, secure=request.is_secure(), samesite="Lax"
        )
        return response
    if request.POST.get("action") == "password" and password.is_valid():
        try:
            users.change_password(
                request,
                user,
                current=password.cleaned_data["current"],
                new=password.cleaned_data["password"],
            )
        except ServiceError as exc:
            password.add_error("current", str(exc))
        except ValidationError as exc:
            password.add_error("password", exc)
        else:
            messages.success(request, _("Password changed. Other sessions were signed out."))
            return redirect("accounts:profile")
    return render(request, "accounts/profile.html", {"profile": profile, "password": password})


@login_required
def security_view(request: HttpRequest) -> HttpResponse:
    """Second factors, recovery codes and active sessions."""
    user = _user(request)
    context = {
        "totp_devices": user.totp_devices.filter(confirmed=True),
        "credentials": user.webauthn_credentials.all(),
        "recovery_left": user.recovery_codes.filter(used_at__isnull=True).count(),
        "new_codes": request.session.pop(RECOVERY_CODES, None),
        "sessions": user.sessions.order_by("-last_activity"),
        "current_key": request.session.session_key,
        "mfa_required": auth.mfa_required(user),
        "name_form": NameForm(),
    }
    return render(request, "accounts/security.html", context)


def _after_first_factor(request: HttpRequest, user: User) -> None:
    auth.mfa_satisfied(request)
    if not user.recovery_codes.filter(used_at__isnull=True).exists():
        request.session[RECOVERY_CODES] = mfa.generate_recovery_codes(request, user)


@login_required
@require_POST
def totp_setup_view(request: HttpRequest) -> HttpResponse:
    """POST a name: show a QR code. POST a code: confirm the device."""
    user = _user(request)
    if "code" not in request.POST:
        name = NameForm(request.POST)
        label = name.cleaned_data["name"] if name.is_valid() else _("Authenticator app")
        device, uri = mfa.totp_begin(user, label)
        request.session["_pm_totp_device"] = str(device.pk)
        return render(
            request,
            "accounts/totp_setup.html",
            {"qr": mfa.qr_svg(uri), "secret_uri": uri, "form": CodeForm()},
        )
    device = get_object_or_404(
        TOTPDevice, pk=request.session.get("_pm_totp_device"), user=user, confirmed=False
    )
    form = CodeForm(request.POST)
    if form.is_valid() and mfa.totp_confirm(request, device, form.cleaned_data["code"]):
        request.session.pop("_pm_totp_device", None)
        _after_first_factor(request, user)
        messages.success(request, _("Authenticator app added."))
        return redirect("accounts:security")
    messages.error(request, _("That code did not match. Scan the code again and retry."))
    return redirect("accounts:security")


@login_required
@require_POST
def webauthn_register_options(request: HttpRequest) -> JsonResponse:
    """Creation options for a new security key or passkey."""
    return JsonResponse(mfa.webauthn_registration_options(request, _user(request)), safe=False)


@login_required
@require_POST
def webauthn_register(request: HttpRequest) -> JsonResponse:
    """Store a verified credential."""
    user = _user(request)
    name = request.GET.get("name", "")[:100]
    try:
        mfa.webauthn_register(request, user, name, request.body.decode())
    except ServiceError as exc:
        logger.info("webauthn registration rejected", extra={"reason": str(exc)})
        return JsonResponse({"error": _("The security key could not be verified.")}, status=400)
    _after_first_factor(request, user)
    messages.success(request, _("Security key added."))
    return JsonResponse({"redirect": "/account/security/"})


@login_required
@require_POST
def recovery_codes_view(request: HttpRequest) -> HttpResponse:
    """Replace recovery codes; they are shown once on the security page."""
    user = _user(request)
    if not auth.has_mfa(user):
        messages.error(request, _("Add a second factor first."))
    else:
        request.session[RECOVERY_CODES] = mfa.generate_recovery_codes(request, user)
    return redirect("accounts:security")


@login_required
@require_POST
def remove_factor_view(request: HttpRequest, kind: str, factor_id: str) -> HttpResponse:
    """Remove one factor. The last factor cannot be removed while MFA is required."""
    user = _user(request)
    remaining = user.totp_devices.filter(confirmed=True).count() + user.webauthn_credentials.count()
    if remaining <= 1 and auth.mfa_required(user):
        messages.error(
            request,
            _("MFA is required for your account. Add another factor before removing this one."),
        )
        return redirect("accounts:security")
    try:
        mfa.remove_factor(request, user, kind, factor_id)
    except ServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Factor removed."))
    return redirect("accounts:security")


@login_required
@require_POST
def revoke_session_view(request: HttpRequest, session_key: str) -> HttpResponse:
    """Sign out one session of the own account."""
    try:
        users.revoke_session(request, _user(request), session_key)
    except ServiceError as exc:
        messages.error(request, str(exc))
    return redirect("accounts:security")


@login_required
@require_POST
def revoke_all_view(request: HttpRequest) -> HttpResponse:
    """Sign out every other session."""
    count = users.revoke_sessions(request, _user(request), _user(request))
    messages.success(request, _("%(count)s other sessions were signed out.") % {"count": count})
    return redirect("accounts:security")


@login_required
@require_POST
def stop_impersonation_view(request: HttpRequest) -> HttpResponse:
    """Return to the own account."""
    users.stop_impersonation(request)
    return redirect("manage:users")
