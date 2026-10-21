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


def test_env_reads_secret_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    secret = tmp_path / "secret"
    secret.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("SOME_SECRET_FILE", str(secret))
    assert env("SOME_SECRET") == "from-file"


def test_env_missing_required_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    with pytest.raises(ImproperlyConfigured):
        env("NOT_SET_ANYWHERE")
