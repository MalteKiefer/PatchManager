import json
import uuid

import pytest
from django.test import Client, RequestFactory
from django.utils import timezone

from patchmanager.accounts.models import Assignment, User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError, privacy
from patchmanager.api import tokens
from patchmanager.audit.models import AuditEvent
from patchmanager.history.models import Change
from patchmanager.tenancy import rls
from tests.conftest import PASSWORD, add_totp, login

pytestmark = pytest.mark.django_db


def _request(user: User):
    request = RequestFactory().post("/")
    request.user = user
    return request


def test_people_download_their_data_without_secrets(client: Client, make_user) -> None:
    user = make_user("me@example.com")
    totp = add_totp(user)
    tokens.create_token(_request(user), user, name="ci", scopes=["read"], days=1)
    login(client, user, totp=totp)
    response = client.post("/account/my-data/", secure=True)
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment")
    data = json.loads(response.content)
    assert data["profile"]["email"] == "me@example.com"
    assert data["second_factors"]["totp"][0]["name"] == "app"
    assert data["api_tokens"][0]["name"] == "ci"
    assert data["sessions"] and data["audit_events"]
    body = response.content.decode()
    for secret in ("encrypted_secret", "token_hash", "code_hash", "password"):
        assert secret not in body
    assert AuditEvent.objects.filter(action="accounts.user.data_exported").exists()
    assert client.get("/account/my-data/", secure=True).status_code == 405


def test_anonymising_removes_the_person(admin_client: Client, make_user, grant, make_tenant):
    target = make_user("leaving@example.com", display_name="Leaving Person")
    add_totp(target)
    tokens.create_token(_request(target), target, name="ci", scopes=["read"], days=1)
    grant(target, "editor", make_tenant())
    with rls.system_context():
        Change.objects.create(
            id=uuid.uuid4(),
            occurred_at=timezone.now(),
            object_type="dcim.rack",
            object_id=uuid.uuid4(),
            action="insert",
            actor_id=target.pk,
            actor_label=target.email,
        )
    response = admin_client.post(f"/manage/users/{target.pk}/anonymise/", secure=True)
    assert response.status_code == 302
    target.refresh_from_db()
    assert target.email.endswith("@invalid") and target.display_name == privacy.ANONYMOUS
    assert not target.is_active and not target.has_usable_password()
    assert not target.totp_devices.exists() and not target.api_tokens.exists()
    assert not Assignment.objects.filter(user=target).exists()
    assert not User.objects.filter(email__iexact="leaving@example.com").exists()
    with rls.system_context():
        assert Change.objects.get(actor_id=target.pk).actor_label == target.email
    assert AuditEvent.objects.filter(action="accounts.user.anonymised").exists()
    assert (
        Client()
        .post("/auth/login/", {"email": "leaving@example.com", "password": PASSWORD}, secure=True)
        .status_code
        == 200
    )  # the form again: no account any more


def test_anonymising_is_limited(make_user, grant, make_tenant, super_admin) -> None:
    tenant = make_tenant()
    admin = make_user()
    grant(admin, "admin", tenant)
    with pytest.raises(ServiceError):
        privacy.anonymise(_request(super_admin), super_admin, super_admin)
    with pytest.raises(PermissionDeniedError):
        privacy.anonymise(_request(admin), admin, super_admin)


def test_export_hides_the_devices_of_impersonating_admins(admin_client, make_user) -> None:
    target = make_user()
    admin_client.post(
        f"/manage/users/{target.pk}/impersonate/", {"reason": "Ticket 7"}, secure=True
    )
    admin_client.post(
        "/account/", {"action": "profile", "display_name": "X", "language": "en"}, secure=True
    )
    event = AuditEvent.objects.get(action="accounts.user.profile_changed")
    assert event.actor_id == target.pk and "impersonator" in event.data
    data = privacy.export(target)
    exported = next(
        e for e in data["audit_events"] if e["action"] == "accounts.user.profile_changed"
    )
    assert exported["by_administrator_acting_as_you"] and exported["ip_address"] is None
    assert data["sessions"] == []
