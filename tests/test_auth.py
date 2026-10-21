from datetime import timedelta

import pytest
from django.contrib.auth.hashers import identify_hasher
from django.core import mail
from django.test import Client, override_settings
from django.utils import timezone

from patchmanager.accounts import sessions
from patchmanager.accounts.models import User, UserSession
from patchmanager.accounts.services import mfa, users
from patchmanager.audit.models import AuditEvent
from tests.conftest import PASSWORD, add_totp, login

pytestmark = pytest.mark.django_db


def actions() -> list[str]:
    return list(AuditEvent.objects.order_by("seq").values_list("action", flat=True))


def test_passwords_use_argon2id(make_user) -> None:
    user = make_user()
    assert identify_hasher(user.password).algorithm == "argon2"
    assert "$argon2id$" in user.password


def test_login_success_and_logout(client: Client, make_user) -> None:
    user = make_user()
    login(client, user)
    assert client.get("/", secure=True).status_code == 200
    session = UserSession.objects.get(user=user)
    assert session.ip_address == "127.0.0.1"
    client.post("/auth/logout/", secure=True)
    assert not UserSession.objects.filter(user=user).exists()
    assert actions()[-2:] == ["auth.login.success", "auth.logout"]


def test_wrong_password_same_message_as_unknown_user(client: Client, make_user) -> None:
    make_user("known@example.com")
    wrong = client.post(
        "/auth/login/", {"email": "known@example.com", "password": "nope"}, secure=True
    )
    unknown = client.post(
        "/auth/login/", {"email": "ghost@example.com", "password": "nope"}, secure=True
    )
    assert "Email or password is incorrect." in wrong.content.decode()
    assert "Email or password is incorrect." in unknown.content.decode()
    assert actions().count("auth.login.failed") == 2


@override_settings(LOGIN_MAX_FAILURES=3)
def test_lockout_after_repeated_failures(client: Client, make_user) -> None:
    user = make_user()
    for _ in range(3):
        client.post("/auth/login/", {"email": user.email, "password": "bad"}, secure=True)
    user.refresh_from_db()
    assert user.is_locked
    response = client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    assert "temporarily locked" in response.content.decode()
    assert "auth.account.locked" in actions()


@override_settings(LOGIN_RATE_PER_IP=2)
def test_ip_rate_limit(client: Client, make_user) -> None:
    user = make_user()
    for _ in range(2):
        client.post("/auth/login/", {"email": user.email, "password": "bad"}, secure=True)
    response = client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    assert "Too many attempts" in response.content.decode()


def test_inactive_user_cannot_login(client: Client, make_user) -> None:
    user = make_user(is_active=False)
    response = client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    assert response.status_code == 200


def test_totp_second_factor_and_replay(client: Client, make_user) -> None:
    user = make_user()
    totp = add_totp(user)
    response = client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    assert response.url == "/auth/mfa/"
    assert client.get("/", secure=True).status_code == 302  # not logged in yet
    code = totp.now()
    assert (
        client.post(
            "/auth/mfa/", {"code": "000000" if code != "000000" else "111111"}, secure=True
        ).status_code
        == 200
    )
    assert client.post("/auth/mfa/", {"code": code}, secure=True).status_code == 302
    assert client.get("/", secure=True).status_code == 200
    # The same code cannot be used again (replay).
    assert not mfa.totp_verify(user, code)


def test_mfa_attempts_are_limited(client: Client, make_user) -> None:
    user = make_user()
    add_totp(user)
    client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    for _ in range(5):
        client.post("/auth/mfa/", {"code": "123456"}, secure=True)
    response = client.post("/auth/mfa/", {"code": "123456"}, secure=True)
    assert response.url == "/auth/login/"


def test_mfa_page_without_pending_login_redirects(client: Client) -> None:
    User.objects.create_user(email="x@example.com", password=PASSWORD, display_name="x")
    assert client.get("/auth/mfa/", secure=True).url == "/auth/login/"


def test_recovery_code_login_once(client: Client, make_user, rf) -> None:
    user = make_user()
    add_totp(user)
    request = rf.post("/")
    request.user = user
    codes = mfa.generate_recovery_codes(request, user)
    client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    assert client.post("/auth/mfa/", {"code": codes[0].upper()}, secure=True).status_code == 302
    assert not mfa.use_recovery_code(user, codes[0])
    assert mfa.use_recovery_code(user, codes[1])


def test_super_admin_must_enrol_mfa(client: Client, super_admin) -> None:
    login(client, super_admin)
    response = client.get("/manage/", secure=True)
    assert response.url == "/account/security/"
    assert client.get("/account/security/", secure=True).status_code == 200


def test_tenant_requiring_mfa_forces_enrolment(
    client: Client, make_user, make_tenant, grant
) -> None:
    tenant = make_tenant(requires_mfa=True)
    user = make_user()
    grant(user, "viewer", tenant)
    login(client, user)
    assert client.get("/", secure=True).url == "/account/security/"


def test_totp_enrolment_lifts_gate(client: Client, super_admin) -> None:
    import pyotp

    from patchmanager.accounts.models import TOTPDevice
    from patchmanager.core import crypto

    login(client, super_admin)
    page = client.post("/account/mfa/totp/", {"name": "Phone"}, secure=True)
    assert b"<svg" in page.content
    device = TOTPDevice.objects.get(user=super_admin, confirmed=False)
    code = pyotp.TOTP(crypto.decrypt(device.encrypted_secret)).now()
    response = client.post("/account/mfa/totp/", {"code": code}, secure=True)
    assert response.url == "/account/security/"
    assert client.get("/manage/", secure=True).status_code == 200
    security = client.get("/account/security/", secure=True).content.decode()
    assert "Your recovery codes" in security


def test_last_factor_cannot_be_removed_when_required(admin_client: Client, super_admin) -> None:
    device = super_admin.totp_devices.get()
    response = admin_client.post(f"/account/mfa/totp/{device.pk}/remove/", secure=True)
    assert response.status_code == 302
    assert super_admin.totp_devices.exists()


def test_idle_timeout(client: Client, make_user) -> None:
    user = make_user()
    login(client, user)
    session = client.session
    session[sessions.LAST_ACTIVITY] = (timezone.now() - timedelta(hours=2)).isoformat()
    session.save()
    assert client.get("/", secure=True).url == "/auth/login/"


def test_absolute_timeout(client: Client, make_user) -> None:
    user = make_user()
    login(client, user)
    session = client.session
    session[sessions.CREATED] = (timezone.now() - timedelta(days=2)).isoformat()
    session.save()
    assert client.get("/", secure=True).url == "/auth/login/"


def test_revoke_other_sessions(make_user) -> None:
    user = make_user()
    first, second = Client(), Client()
    login(first, user)
    login(second, user)
    assert UserSession.objects.filter(user=user).count() == 2
    first.post("/account/sessions/revoke-others/", secure=True)
    assert first.get("/", secure=True).status_code == 200
    assert second.get("/", secure=True).status_code == 302


def test_password_change_signs_out_other_sessions(make_user) -> None:
    user = make_user()
    first, second = Client(), Client()
    login(first, user)
    login(second, user)
    response = first.post(
        "/account/",
        {
            "action": "password",
            "current": PASSWORD,
            "password": "a new long passphrase 77",
            "password_confirm": "a new long passphrase 77",
        },
        secure=True,
    )
    assert response.status_code == 302
    assert first.get("/", secure=True).status_code == 200
    assert second.get("/", secure=True).status_code == 302


def test_setup_wizard(client: Client, db) -> None:
    assert client.get("/", secure=True).url == "/setup/"
    token = users.issue_setup_token()
    bad = client.post(
        "/setup/",
        {
            "token": "wrong",
            "email": "a@example.com",
            "display_name": "A",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
        },
        secure=True,
    )
    assert "invalid or expired" in bad.content.decode()
    weak = client.post(
        "/setup/",
        {
            "token": token,
            "email": "a@example.com",
            "display_name": "A",
            "password": "short",
            "password_confirm": "short",
        },
        secure=True,
    )
    assert weak.status_code == 200 and not User.objects.exists()
    ok = client.post(
        "/setup/",
        {
            "token": token,
            "email": "Admin@Example.com",
            "display_name": "Admin",
            "password": PASSWORD,
            "password_confirm": PASSWORD,
        },
        secure=True,
    )
    assert ok.url == "/account/security/"
    admin = User.objects.get()
    assert admin.email == "admin@example.com"
    from patchmanager.accounts import policy

    assert policy.is_super_admin(admin)
    assert client.get("/setup/", secure=True).status_code == 404
    assert "system.setup.completed" in actions()


def test_setup_token_command(db, capsys) -> None:
    from django.core.management import call_command

    call_command("setup_token")
    assert len(capsys.readouterr().out.strip()) > 20


def test_invitation_flow(
    admin_client: Client, make_tenant, django_capture_on_commit_callbacks
) -> None:
    from patchmanager.accounts.models import Role

    tenant = make_tenant()
    viewer = Role.objects.get(key="viewer")
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client.post(
            "/manage/users/invite/",
            {
                "email": "new@example.com",
                "display_name": "New",
                "role": viewer.pk,
                "tenant": tenant.pk,
            },
            secure=True,
        )
    assert response.status_code == 302
    assert len(mail.outbox) == 1
    link = next(line for line in mail.outbox[0].body.splitlines() if "/invite/" in line)
    path = link.split("localhost:8000", 1)[1]
    guest = Client()
    assert guest.get(path, secure=True).status_code == 200
    assert (
        guest.post(path, {"password": PASSWORD, "password_confirm": PASSWORD}, secure=True).url
        == "/auth/login/"
    )
    assert guest.get(path, secure=True).status_code == 404
    user = User.objects.get(email="new@example.com")
    login(guest, user)


def test_duplicate_invitation_rejected(admin_client: Client, make_user) -> None:
    from patchmanager.accounts.models import Role

    make_user("taken@example.com")
    response = admin_client.post(
        "/manage/users/invite/",
        {
            "email": "taken@example.com",
            "display_name": "X",
            "role": Role.objects.get(key="viewer").pk,
        },
        secure=True,
    )
    assert "already exists" in response.content.decode()


def test_open_redirect_is_blocked(client: Client, make_user) -> None:
    user = make_user()
    client.get("/auth/login/?next=https://evil.example/", secure=True)
    response = client.post("/auth/login/", {"email": user.email, "password": PASSWORD}, secure=True)
    assert response.url == "/"


def test_profile_update(client: Client, make_user) -> None:
    user = make_user()
    login(client, user)
    response = client.post(
        "/account/", {"action": "profile", "display_name": "Renamed", "language": "de"}, secure=True
    )
    assert response.status_code == 302
    user.refresh_from_db()
    assert (user.display_name, user.language) == ("Renamed", "de")
