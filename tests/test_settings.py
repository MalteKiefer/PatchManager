import json
import os
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

from patchmanager.settings import database_from_url, env


def test_database_url_with_socket_host() -> None:
    db = database_from_url("postgresql://pm:s%40cret@/pmdb?host=/run/pg&port=5433&sslmode=require")
    assert db["NAME"] == "pmdb"
    assert db["USER"] == "pm"
    assert db["PASSWORD"] == "s@cret"
    assert db["HOST"] == "/run/pg"
    assert db["PORT"] == "5433"
    assert db["OPTIONS"] == {"sslmode": "require"}


def test_database_url_rejects_other_engines() -> None:
    with pytest.raises(ImproperlyConfigured):
        database_from_url("sqlite:///db.sqlite3")


def test_env_reads_secret_file(tmp_path, monkeypatch) -> None:
    secret = tmp_path / "secret"
    secret.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("SOME_SECRET_FILE", str(secret))
    assert env("SOME_SECRET") == "from-file"


def test_env_missing_required_value(monkeypatch) -> None:
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(ImproperlyConfigured):
        env("NOT_SET_ANYWHERE")


def _settings_in_subprocess(extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Import the settings fresh: they are evaluated once per process."""
    environment = {**os.environ, **extra, "DJANGO_SETTINGS_MODULE": "patchmanager.settings"}
    code = "import json, patchmanager.settings as s;print(json.dumps(s.S3_DEFAULTS))"
    return subprocess.run(  # noqa: S603 - fixed interpreter and code, no outside input
        [sys.executable, "-c", code], env=environment, capture_output=True, text=True, check=False
    )


def test_s3_environment_gives_initial_values(tmp_path) -> None:
    secret = tmp_path / "s3_secret"
    secret.write_text("secret-value\n", encoding="utf-8")
    result = _settings_in_subprocess(
        {
            "PATCHMANAGER_S3_BUCKET": "pm-files",
            "PATCHMANAGER_S3_ENDPOINT_URL": "https://s3.eu-central-003.backblazeb2.com",
            "PATCHMANAGER_S3_ACCESS_KEY_ID": "key-id",
            "PATCHMANAGER_S3_SECRET_ACCESS_KEY_FILE": str(secret),
        }
    )
    assert result.returncode == 0, result.stderr
    s3 = json.loads(result.stdout)
    assert (s3["bucket"], s3["secret_access_key"]) == ("pm-files", "secret-value")


def test_s3_endpoint_must_use_https() -> None:
    result = _settings_in_subprocess(
        {
            "PATCHMANAGER_S3_BUCKET": "pm-files",
            "PATCHMANAGER_S3_ENDPOINT_URL": "http://minio.local:9000",
            "PATCHMANAGER_S3_ACCESS_KEY_ID": "key-id",
            "PATCHMANAGER_S3_SECRET_ACCESS_KEY": "secret",
        }
    )
    assert result.returncode != 0
    assert "must use https" in result.stderr


def test_s3_storage_writes_private_objects() -> None:
    from botocore.stub import ANY, Stubber
    from django.core.files.base import ContentFile
    from storages.backends.s3 import S3Storage

    store = S3Storage(
        bucket_name="pm-files",
        endpoint_url="https://s3.example.test",
        region_name="eu-central-003",
        access_key="key-id",
        secret_key="secret",
        location="attachments",
        default_acl=None,
        file_overwrite=False,
    )
    client = store.connection.meta.client
    with Stubber(client) as stub:
        stub.add_client_error("head_object", "404", http_status_code=404)
        # The stub rejects unexpected parameters, so this also shows no ACL header is sent.
        expected = {"Bucket": "pm-files", "Key": "attachments/a/b.bin"}
        expected |= {"Body": ANY, "ContentType": ANY, "ChecksumAlgorithm": ANY}
        stub.add_response("put_object", {}, expected)
        name = store.save("a/b.bin", ContentFile(b"ciphertext"))
        stub.assert_no_pending_responses()
    assert name == "a/b.bin"
