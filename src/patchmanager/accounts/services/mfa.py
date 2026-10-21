"""Second factors: TOTP (RFC 6238), WebAuthn / passkeys and recovery codes."""

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

import pyotp
import qrcode
import qrcode.image.svg
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from patchmanager.accounts.models import RecoveryCode, TOTPDevice, User, WebAuthnCredential
from patchmanager.accounts.services import ServiceError
from patchmanager.audit import services as audit
from patchmanager.core import crypto, ratelimit

TOTP_INTERVAL = 30
RECOVERY_CODE_COUNT = 10
_WEBAUTHN_CHALLENGE = "_pm_webauthn_challenge"


# --- TOTP -----------------------------------------------------------------


def totp_begin(user: User, name: str) -> tuple[TOTPDevice, str]:
    """Create an unconfirmed device; return it with its provisioning URI."""
    secret = pyotp.random_base32(length=32)
    user.totp_devices.filter(confirmed=False).delete()
    device = TOTPDevice.objects.create(
        user=user, name=name[:100], encrypted_secret=crypto.encrypt(secret)
    )
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=settings.INSTANCE_NAME)
    return device, uri


def qr_svg(data: str) -> str:
    """Render ``data`` as an inline SVG QR code (no external service)."""
    image = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    return image.to_string(encoding="unicode")  # type: ignore[no-any-return]


def _match_step(device: TOTPDevice, code: str) -> int | None:
    """Return the matching time step within +-1 window, if not used before."""
    code = code.strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return None
    totp = pyotp.TOTP(crypto.decrypt(device.encrypted_secret))
    current = int(time.time()) // TOTP_INTERVAL
    for step in (current - 1, current, current + 1):
        if step > device.last_used_step and hmac.compare_digest(
            totp.at(step * TOTP_INTERVAL), code
        ):
            return step
    return None


def totp_confirm(request: HttpRequest, device: TOTPDevice, code: str) -> bool:
    """Confirm enrolment with a first valid code."""
    step = _match_step(device, code)
    if step is None:
        return False
    device.confirmed = True
    device.last_used_step = step
    device.save(update_fields=["confirmed", "last_used_step", "updated_at"])
    audit.record(
        "auth.mfa.totp_added", request=request, obj=device.user, data={"device": device.name}
    )
    return True


def totp_verify(user: User, code: str) -> bool:
    """Verify a login code; each code is accepted once (replay protection)."""
    with transaction.atomic():
        for device in user.totp_devices.select_for_update().filter(confirmed=True):
            step = _match_step(device, code)
            if step is not None:
                device.last_used_step = step
                device.save(update_fields=["last_used_step", "updated_at"])
                return True
    return False


# --- Recovery codes -------------------------------------------------------


def _code_hash(code: str) -> str:
    normalised = code.strip().replace("-", "").replace(" ", "").lower()
    return hmac.new(settings.SECRET_KEY.encode(), normalised.encode(), hashlib.sha256).hexdigest()


def generate_recovery_codes(request: HttpRequest, user: User) -> list[str]:
    """Replace all recovery codes; the plain codes are shown exactly once."""
    codes = [f"{secrets.token_hex(3)}-{secrets.token_hex(3)}" for _ in range(RECOVERY_CODE_COUNT)]
    with transaction.atomic():
        user.recovery_codes.all().delete()
        RecoveryCode.objects.bulk_create(
            RecoveryCode(user=user, code_hash=_code_hash(c)) for c in codes
        )
    audit.record("auth.mfa.recovery_generated", request=request, obj=user)
    return codes


def use_recovery_code(user: User, code: str) -> bool:
    """Consume a recovery code."""
    updated = user.recovery_codes.filter(code_hash=_code_hash(code), used_at__isnull=True).update(
        used_at=timezone.now()
    )
    return updated == 1


# --- WebAuthn -------------------------------------------------------------


def _user_handle(user: User) -> bytes:
    return bytes(user.pk.bytes)


def webauthn_registration_options(request: HttpRequest, user: User) -> str:
    """Options for ``navigator.credentials.create``; challenge kept in the session."""
    options = generate_registration_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        rp_name=settings.INSTANCE_NAME,
        user_id=_user_handle(user),
        user_name=user.email,
        user_display_name=user.display_name,
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=bytes(c.credential_id))
            for c in user.webauthn_credentials.all()
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    request.session[_WEBAUTHN_CHALLENGE] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def _pop_challenge(request: HttpRequest) -> bytes:
    challenge = request.session.pop(_WEBAUTHN_CHALLENGE, None)
    if not challenge:
        raise ServiceError(_("The security key request expired. Try again."))
    return base64url_to_bytes(challenge)


def webauthn_register(
    request: HttpRequest, user: User, name: str, credential_json: str
) -> WebAuthnCredential:
    """Verify an attestation and store the credential."""
    try:
        verified = verify_registration_response(
            credential=credential_json,
            expected_challenge=_pop_challenge(request),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGIN,
        )
    except ServiceError:
        raise
    except Exception as exc:  # the library raises several types for invalid input
        raise ServiceError(_("The security key could not be verified.")) from exc
    transports = json.loads(credential_json).get("response", {}).get("transports", []) or []
    credential = WebAuthnCredential.objects.create(
        user=user,
        name=name[:100] or _("Security key"),
        credential_id=verified.credential_id,
        public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        transports=[str(t)[:20] for t in transports][:8],
        discoverable=bool(
            getattr(verified, "credential_backed_up", False) or "internal" in transports
        ),
    )
    audit.record(
        "auth.mfa.webauthn_added", request=request, obj=user, data={"name": credential.name}
    )
    return credential


def webauthn_authentication_options(request: HttpRequest, user: User | None) -> str:
    """Options for ``navigator.credentials.get``. ``user=None`` allows passkey sign in."""
    allow = (
        [
            PublicKeyCredentialDescriptor(id=bytes(c.credential_id))
            for c in user.webauthn_credentials.all()
        ]
        if user
        else []
    )
    options = generate_authentication_options(
        rp_id=settings.WEBAUTHN_RP_ID,
        allow_credentials=allow,
        user_verification=UserVerificationRequirement.REQUIRED
        if user is None
        else UserVerificationRequirement.PREFERRED,
    )
    request.session[_WEBAUTHN_CHALLENGE] = bytes_to_base64url(options.challenge)
    return options_to_json(options)


def webauthn_authenticate(request: HttpRequest, user: User | None, credential_json: str) -> User:
    """Verify an assertion. Returns the credential owner.

    With ``user`` set, the credential must belong to that user (second factor).
    Without, any registered passkey verified with user verification signs in.
    """
    ip = request.META.get("REMOTE_ADDR", "unknown")
    if not ratelimit.hit(f"webauthn-ip:{ip}", limit=30, window=600):
        raise ServiceError(_("Too many attempts. Try again in a few minutes."))
    try:
        payload: dict[str, Any] = json.loads(credential_json)
        raw_id = base64url_to_bytes(payload["rawId"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ServiceError(_("The security key could not be verified.")) from exc
    credential = (
        WebAuthnCredential.objects.select_related("user").filter(credential_id=raw_id).first()
    )
    if credential is None or (user is not None and credential.user_id != user.pk):
        raise ServiceError(_("This security key is not registered."))
    try:
        verified = verify_authentication_response(
            credential=credential_json,
            expected_challenge=_pop_challenge(request),
            expected_rp_id=settings.WEBAUTHN_RP_ID,
            expected_origin=settings.WEBAUTHN_ORIGIN,
            credential_public_key=bytes(credential.public_key),
            credential_current_sign_count=credential.sign_count,
            require_user_verification=user is None,
        )
    except ServiceError:
        raise
    except Exception as exc:
        audit.record("auth.mfa.webauthn_failed", request=request, obj=credential.user)
        raise ServiceError(_("The security key could not be verified.")) from exc
    credential.sign_count = verified.new_sign_count
    credential.last_used_at = timezone.now()
    credential.save(update_fields=["sign_count", "last_used_at", "updated_at"])
    return credential.user


def remove_factor(request: HttpRequest, user: User, kind: str, factor_id: str) -> None:
    """Delete one TOTP device or WebAuthn credential of ``user``."""
    model = TOTPDevice if kind == "totp" else WebAuthnCredential
    deleted, _rows = model.objects.filter(user=user, pk=factor_id).delete()
    if not deleted:
        raise ServiceError(_("Factor not found."))
    audit.record("auth.mfa.removed", request=request, obj=user, data={"kind": kind})
