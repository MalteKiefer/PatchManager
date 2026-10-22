import socket
from typing import Any

import pytest
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.test import Client

from patchmanager.instance import config, runtime
from patchmanager.instance.models import InstanceSettings

from .conftest import login


def _post(client: Client, section: str, data: dict[str, Any]) -> str:
    response = client.post(f"/manage/settings/{section}/", data, secure=True, follow=True)
    assert response.status_code == 200
    return response.content.decode()


SECURITY = {
    "password_min_length": 16,
    "session_max_age_hours": 8,
    "session_idle_minutes": 20,
    "login_max_failures": 5,
    "login_lockout_minutes": 30,
    "login_rate_per_ip": 40,
    "invitation_ttl_hours": 24,
}
MAIL = {"mail_host": "smtp.example.com", "mail_port": 587, "mail_use_tls": "on"}
STORAGE = {"s3_prefix": "attachments"}
UPLOADS = {"upload_max_mb": 10, "clamd_port": 3310}


def test_pages_and_permission(admin_client: Client, make_user) -> None:
    for section in ("security", "mail", "uploads", "storage", "maps", "integrations", "backups"):
        page = admin_client.get(f"/manage/settings/{section}/", secure=True).content.decode()
        assert 'href="/manage/settings/security/" aria-current="page"' in page
    assert admin_client.get("/manage/settings/nope/", secure=True).status_code == 404
    client = Client()
    login(client, make_user())
    assert client.get("/manage/settings/security/", secure=True).status_code == 403


def test_security_settings_apply_without_restart(admin_client: Client) -> None:
    assert "Settings saved" in _post(admin_client, "security", SECURITY)
    current = config.get()
    assert (current.password_min_length, current.session_idle_timeout) == (16, 1200)
    with pytest.raises(ValidationError):
        validate_password("Short-but-14ch")
    assert _post(admin_client, "security", {**SECURITY, "password_min_length": 4}).count(
        "Ensure this value is greater than or equal to 8"
    )


def test_mail_password_is_encrypted_kept_and_cleared(admin_client: Client) -> None:
    _post(admin_client, "mail", {**MAIL, "mail_password": "s3cret!", "mail_from": "pm@example.com"})
    stored = InstanceSettings.objects.get().config
    assert stored["mail_password"] and "s3cret!" not in str(stored)
    assert config.get().mail_password == "s3cret!"
    backend = runtime.ConfiguredMailBackend(alias="default")
    assert (backend.host, backend.password, backend.sender) == (
        "smtp.example.com",
        "s3cret!",
        "pm@example.com",
    )
    page = _post(admin_client, "mail", {**MAIL, "mail_from": "pm@example.com"})
    assert "A secret is stored" in page and config.get().mail_password == "s3cret!"
    clear = {**MAIL, "mail_from": "pm@example.com", "mail_password_clear": "on"}
    _post(admin_client, "mail", clear)
    assert config.get().mail_password == ""


def test_bucket_is_checked_and_old_files_stay_readable(
    admin_client: Client, monkeypatch, tmp_path
) -> None:
    store = runtime.AttachmentStorage(location=tmp_path)
    old = store.save("a/old.bin", ContentFile(b"before"))
    bucket = InMemoryStorage()
    monkeypatch.setattr(runtime, "s3_storage", lambda cfg: bucket)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("203.0.113.5", p))]
    )
    s3 = {
        **STORAGE,
        "s3_bucket": "pm-files",
        "s3_endpoint_url": "https://s3.eu-central-003.backblazeb2.com",
        "s3_access_key_id": "key",
        "s3_secret_access_key": "secret",
    }
    assert "Settings saved" in _post(admin_client, "storage", s3)
    assert config.get().s3_bucket == "pm-files"
    new = store.save("a/new.bin", ContentFile(b"after"))
    assert bucket.exists(new) and not store.local.exists(new)
    with store.open(old) as handle:
        assert handle.read() == b"before"  # still in the volume
    store.delete(old)
    assert not store.exists(old)

    def broken(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(runtime, "s3_storage", lambda cfg: type("S", (), {"save": broken})())
    page = _post(admin_client, "storage", {**s3, "s3_bucket": "other-bucket"})
    assert "could not be written (PermissionError)" in page
    assert config.get().s3_bucket == "pm-files"
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("127.0.0.1", p))])
    page = _post(admin_client, "storage", {**s3, "s3_bucket": "third-bucket"})
    assert "This address cannot be used" in page
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda h, p, **kw: [(10, 1, 6, "", ("::ffff:169.254.169.254", p))]
    )
    page = _post(admin_client, "storage", {**s3, "s3_region": "eu"})  # endpoint checked again
    assert "This address cannot be used" in page
    page = _post(admin_client, "storage", {**s3, "s3_endpoint_url": "http://minio.lan"})
    assert "must use https" in page


def test_maps_and_integrations(admin_client: Client) -> None:
    _post(
        admin_client,
        "maps",
        {"map_tile_url": "https://tiles.example.org/{z}/{x}/{y}.png", "map_attribution": "x"},
    )
    page = admin_client.get("/locations/", secure=True)
    directives = dict(
        part.strip().split(" ", 1) for part in page["Content-Security-Policy"].split(";")
    )
    assert directives["img-src"].split() == ["'self'", "data:", "https://tiles.example.org"]
    bad = _post(admin_client, "maps", {"map_tile_url": "http://tiles.example.org/a.png"})
    assert "{z}, {x} and {y}" in bad
    wrong = {"webhook_allowed_networks": "10.0.0.0/8, nope", "api_rate": "100/minute"}
    assert "Enter networks such as 10.0.0.0/8" in _post(admin_client, "integrations", wrong)
    _post(
        admin_client,
        "integrations",
        {"webhook_allowed_networks": "10.0.0.0/8", "api_rate": "100/minute"},
    )
    assert config.get().allowed_networks == ["10.0.0.0/8"]
    assert runtime.ConfiguredUserRateThrottle().get_rate() == "100/minute"


def test_uploads_save_apart_from_storage_and_bucket_test(admin_client: Client, monkeypatch) -> None:
    assert "Settings saved" in _post(admin_client, "uploads", {**UPLOADS, "upload_max_mb": 7})
    assert config.get().upload_max_mb == 7 and config.get().s3_bucket == ""
    bucket = InMemoryStorage()
    monkeypatch.setattr(runtime, "s3_storage", lambda cfg: bucket)
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("203.0.113.5", p))]
    )
    s3 = {
        **STORAGE,
        "s3_bucket": "pm-files",
        "s3_endpoint_url": "https://s3.eu-central-003.backblazeb2.com",
        "s3_access_key_id": "key",
        "s3_secret_access_key": "secret",
        "action": "test",
    }
    page = _post(admin_client, "storage", s3)
    assert "The bucket can be written and read" in page and 'value="pm-files"' in page
    assert config.get().s3_bucket == ""  # testing saves nothing
    assert "Enter a bucket to test" in _post(admin_client, "storage", {**STORAGE, "action": "test"})


def test_unreadable_settings_do_not_abort_the_request(db, monkeypatch) -> None:
    """A failed read (for example before migrating) falls back without breaking the transaction."""
    from django.db import transaction
    from django.db.models.expressions import RawSQL

    from patchmanager.instance import state

    broken = InstanceSettings.objects.annotate(x=RawSQL("no_such_column", []))
    monkeypatch.setattr(InstanceSettings.objects, "only", lambda *a: broken)
    monkeypatch.setattr(InstanceSettings.objects, "defer", lambda *a: broken)
    with transaction.atomic():
        assert config.get() == config.defaults()
        assert state.get().name == ""
        assert InstanceSettings.objects.count() >= 0  # the transaction is still usable
