import dataclasses
import os
import subprocess
from pathlib import Path

import pytest
from django.test import Client

from patchmanager.accounts.services import ServiceError
from patchmanager.instance import config, offsite

SETTINGS = {
    "offsite_enabled": "on",
    "offsite_repository": "s3:https://s3.eu-central-003.backblazeb2.com/pm-backup/patchmanager",
    "offsite_access_key_id": "key-id",
    "offsite_secret_access_key": "app-key-secret",
    "offsite_password": "repo-pass-123",
    "offsite_keep_daily": 14,
    "offsite_keep_weekly": 8,
    "offsite_keep_monthly": 12,
}


@pytest.fixture
def public(monkeypatch) -> None:
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("203.0.113.9", p))]
    )


class Calls(list[tuple[list[str], dict[str, str]]]):
    """restic invocations; ``fail`` names a step that should fail."""

    fail = ""


@pytest.fixture
def restic(monkeypatch) -> Calls:
    calls = Calls()

    def run(args, *, env, **kwargs):
        calls.append((args, env))
        step = args[2]
        code = 1 if step in (calls.fail, "cat") else 0
        stderr = f"Fatal: {step} failed for {env['RESTIC_PASSWORD']}" if code else ""
        return subprocess.CompletedProcess(args, code, "", stderr)

    monkeypatch.setattr(offsite.shutil, "which", lambda name: "/usr/bin/restic")
    monkeypatch.setattr(offsite.subprocess, "run", run)
    return calls


def test_copy_after_new_dump_with_secrets_only_in_the_environment(
    admin_client: Client, settings, tmp_path: Path, public, restic
) -> None:
    settings.BACKUP_DIR = str(tmp_path)
    page = admin_client.post("/manage/settings/backups/", SETTINGS, secure=True, follow=True)
    assert "Settings saved" in page.content.decode()
    assert offsite.run() is False  # nothing to copy yet
    (tmp_path / "patchmanager-20261022T030000Z.dump").write_bytes(b"dump")
    assert offsite.run() is True
    steps = [args[2] for args, _env in restic]
    assert steps == ["cat", "init", "backup", "forget", "check"]
    for args, env in restic:
        assert args[1] == "--no-cache"
        assert "repo-pass-123" not in " ".join(args) and "app-key-secret" not in " ".join(args)
        assert env["RESTIC_PASSWORD"] == "repo-pass-123"
        assert env["AWS_SECRET_ACCESS_KEY"] == "app-key-secret"
    status = offsite.status()
    assert status and status["ok"] and "patchmanager-20261022T030000Z.dump" in status["message"]
    assert offsite.run() is False  # copied already

    restic.fail = "check"
    assert offsite.run(force=True) is False
    status = offsite.status()
    assert status and not status["ok"]
    assert "repo-pass-123" not in status["message"] and "***" in status["message"]
    system = admin_client.get("/manage/system/", secure=True).content.decode()
    assert "Off-site copy failed" in system


def test_copy_now_button(admin_client: Client, settings, tmp_path: Path, public, restic) -> None:
    settings.BACKUP_DIR = str(tmp_path)
    (tmp_path / "patchmanager-20261022T030000Z.dump").write_bytes(b"dump")
    admin_client.post("/manage/settings/backups/", SETTINGS, secure=True)
    page = admin_client.post(
        "/manage/settings/backups/", {"action": "copy-now"}, secure=True, follow=True
    ).content.decode()
    assert "The off-site copy was started" in page and "Copied" in page


@pytest.mark.parametrize(
    "repository",
    [
        "s3:http://s3.example.com/bucket",
        "sftp:user@host:/srv/restic",
        "rest:https://host/",
        "s3:https://s3.example.com",
        "s3:https://s3.example.com/bucket path",
    ],
)
def test_only_https_s3_repositories(repository: str, public) -> None:
    with pytest.raises(ServiceError):
        offsite.check_repository(repository)


def test_repository_must_not_point_into_the_host(admin_client: Client, monkeypatch) -> None:
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda h, p, **kw: [(2, 1, 6, "", ("127.0.0.1", p))])
    page = admin_client.post("/manage/settings/backups/", SETTINGS, secure=True).content.decode()
    assert "This address cannot be used" in page
    assert config.get().offsite_repository == ""


def test_new_destination_needs_secrets_again(admin_client: Client, public, restic) -> None:
    admin_client.post("/manage/settings/backups/", SETTINGS, secure=True)
    moved = {
        **SETTINGS,
        "offsite_repository": "s3:https://evil.example.com/bucket/x",
        "offsite_secret_access_key": "",
        "offsite_password": "",
    }
    page = admin_client.post("/manage/settings/backups/", moved, secure=True).content.decode()
    assert "needs its secret key and password entered again" in page
    assert config.get().offsite_repository == SETTINGS["offsite_repository"]
    same = {
        **SETTINGS,
        "offsite_secret_access_key": "",
        "offsite_password": "",
        "offsite_keep_daily": 7,
    }
    saved = admin_client.post("/manage/settings/backups/", same, secure=True, follow=True)
    assert "Settings saved" in saved.content.decode()
    assert config.get().offsite_password == "repo-pass-123"  # kept for the same destination


def test_one_copy_at_a_time_and_only_complete_sets(
    admin_client: Client, settings, tmp_path: Path, public, restic
) -> None:
    from django.core.cache import cache

    settings.BACKUP_DIR = str(tmp_path)
    admin_client.post("/manage/settings/backups/", SETTINGS, secure=True)
    (tmp_path / "patchmanager-20261022T030000Z.dump").write_bytes(b"dump")
    partial = tmp_path / "patchmanager-20261022T030000Z.dump.media.tar.partial"
    partial.write_bytes(b"t")
    assert offsite.run() is False and restic == []  # the backup is still being written
    partial.unlink()
    stale = tmp_path / "patchmanager-20261001T030000Z.dump.partial"  # a failed earlier run
    stale.write_bytes(b"d")
    os.utime(stale, (0, 0))
    cache.add(offsite.LOCK, "x", 60)
    assert offsite.run(force=True) is False and restic == []  # another copy runs
    cache.delete(offsite.LOCK)
    assert offsite.run() is True
    assert cache.get(offsite.LOCK) is None


def test_fetch_restores_into_the_backup_folder(settings, restic) -> None:
    from django.core.management import call_command
    from django.core.management.base import CommandError

    settings.BACKUP_DIR = "/backups"
    stored = dataclasses.replace(
        config.defaults(),
        offsite_repository=SETTINGS["offsite_repository"],
        offsite_password="repo-pass-123",
    )
    offsite.fetch(stored, "patchmanager-20261022T030000Z.dump", "/restore")
    args = restic[-1][0]
    assert args[2:4] == ["restore", "latest:/backups"]
    assert args[args.index("--target") + 1] == "/restore"
    assert "/patchmanager-20261022T030000Z.dump.media.tar" in args
    with pytest.raises(ServiceError):
        offsite.fetch(stored, "../etc/passwd", "/restore")
    restic.fail = "restore"
    with pytest.raises(CommandError):
        call_command("offsite_fetch", "patchmanager-20261022T030000Z.dump")
