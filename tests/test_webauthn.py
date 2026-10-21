import json
from types import SimpleNamespace
from unittest import mock

import pytest
from django.test import Client

from patchmanager.accounts.models import WebAuthnCredential
from tests.conftest import PASSWORD, login

pytestmark = pytest.mark.django_db

ASSERTION = json.dumps({"id": "AQID", "rawId": "AQID", "type": "public-key", "response": {}})


def _credential(user):
    return WebAuthnCredential.objects.create(
        user=user, name="Key", credential_id=b"\x01\x02\x03", public_key=b"pk"
    )


def test_registration_options_and_register(client: Client, make_user) -> None:
    user = make_user()
    login(client, user)
    options = json.loads(client.post("/account/mfa/webauthn/options/", secure=True).json())
    assert options["rp"]["id"] == "localhost"
    assert options["user"]["name"] == user.email
    verified = SimpleNamespace(
        credential_id=b"\x09\x09",
        credential_public_key=b"key",
        sign_count=0,
        credential_backed_up=False,
    )
    body = json.dumps({"id": "CQk", "rawId": "CQk", "response": {"transports": ["usb"]}})
    with mock.patch(
        "patchmanager.accounts.services.mfa.verify_registration_response", return_value=verified
    ):
        response = client.post(
            "/account/mfa/webauthn/register/?name=YubiKey",
            body,
            content_type="application/json",
            secure=True,
        )
    assert response.json() == {"redirect": "/account/security/"}
    credential = user.webauthn_credentials.get()
    assert credential.name == "YubiKey" and credential.transports == ["usb"]


def test_register_without_challenge_fails(client: Client, make_user) -> None:
    login(client, make_user())
    response = client.post(
        "/account/mfa/webauthn/register/", "{}", content_type="application/json", secure=True
    )
    assert response.status_code == 400


def test_register_invalid_attestation(client: Client, make_user) -> None:
    login(client, make_user())
    client.post("/account/mfa/webauthn/options/", secure=True)
    with mock.patch(
        "patchmanager.accounts.services.mfa.verify_registration_response", side_effect=ValueError
    ):
        response = client.post(
            "/account/mfa/webauthn/register/", "{}", content_type="application/json", secure=True
        )
    assert response.status_code == 400


def test_second_factor_with_security_key(client: Client, make_user) -> None:
    user = make_user()
    credential = _credential(user)
    client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    options = json.loads(client.post("/auth/webauthn/options/", secure=True).json())
    assert options["allowCredentials"][0]["id"] == "AQID"
    with mock.patch(
        "patchmanager.accounts.services.mfa.verify_authentication_response",
        return_value=SimpleNamespace(new_sign_count=5),
    ):
        response = client.post(
            "/auth/webauthn/verify/", ASSERTION, content_type="application/json", secure=True
        )
    assert response.json() == {"redirect": "/"}
    credential.refresh_from_db()
    assert credential.sign_count == 5
    assert client.get("/", secure=True).status_code == 200


def test_passwordless_passkey(client: Client, make_user) -> None:
    user = make_user()
    _credential(user)
    options = json.loads(client.post("/auth/webauthn/options/", secure=True).json())
    assert options["userVerification"] == "required"
    with mock.patch(
        "patchmanager.accounts.services.mfa.verify_authentication_response",
        return_value=SimpleNamespace(new_sign_count=1),
    ):
        response = client.post(
            "/auth/webauthn/verify/", ASSERTION, content_type="application/json", secure=True
        )
    assert response.status_code == 200
    assert client.get("/", secure=True).status_code == 200


def test_unknown_or_foreign_credential_rejected(client: Client, make_user) -> None:
    owner, other = make_user(), make_user()
    _credential(owner)
    client.post("/auth/login/", {"email": other.email, "password": PASSWORD}, secure=True)
    client.post("/auth/webauthn/options/", secure=True)
    response = client.post(
        "/auth/webauthn/verify/", ASSERTION, content_type="application/json", secure=True
    )
    assert response.status_code == 400
    assert (
        client.post(
            "/auth/webauthn/verify/", "garbage", content_type="application/json", secure=True
        ).status_code
        == 400
    )


def test_failed_assertion_is_audited(client: Client, make_user) -> None:
    from patchmanager.audit.models import AuditEvent

    user = make_user()
    _credential(user)
    client.post("/auth/webauthn/options/", secure=True)
    with mock.patch(
        "patchmanager.accounts.services.mfa.verify_authentication_response", side_effect=ValueError
    ):
        assert (
            client.post(
                "/auth/webauthn/verify/", ASSERTION, content_type="application/json", secure=True
            ).status_code
            == 400
        )
    assert AuditEvent.objects.filter(action="auth.mfa.webauthn_failed").exists()


def test_locked_owner_cannot_sign_in_with_passkey(client: Client, make_user) -> None:
    user = make_user(is_active=False)
    _credential(user)
    client.post("/auth/webauthn/options/", secure=True)
    with mock.patch(
        "patchmanager.accounts.services.mfa.verify_authentication_response",
        return_value=SimpleNamespace(new_sign_count=1),
    ):
        response = client.post(
            "/auth/webauthn/verify/", ASSERTION, content_type="application/json", secure=True
        )
    assert response.status_code == 403


def test_security_page_factor_management(client: Client, make_user) -> None:
    user = make_user()
    login(client, user)
    credential = _credential(user)
    assert client.post("/account/mfa/recovery/", secure=True).status_code == 302
    assert user.recovery_codes.count() == 10
    client.post(f"/account/mfa/webauthn/{credential.pk}/remove/", secure=True)
    assert not user.webauthn_credentials.exists()
    client.post(f"/account/mfa/webauthn/{credential.pk}/remove/", secure=True)  # already gone
    assert (
        client.post("/account/mfa/recovery/", secure=True).status_code == 302
    )  # no factor: refused
    other = Client()
    login(other, user)
    key = user.sessions.exclude(session_key=client.session.session_key).get().session_key
    client.post(f"/account/sessions/{key}/revoke/", secure=True)
    assert other.get("/", secure=True).status_code == 302
    client.post("/account/sessions/doesnotexist/revoke/", secure=True)


def test_totp_wrong_confirmation_code(client: Client, make_user) -> None:
    user = make_user()
    login(client, user)
    client.post("/account/mfa/totp/", {"name": "Phone"}, secure=True)
    response = client.post("/account/mfa/totp/", {"code": "000000"}, secure=True)
    assert response.url == "/account/security/"
    assert not user.totp_devices.filter(confirmed=True).exists()
