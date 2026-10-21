from unittest import mock

import pytest
from django.db.utils import OperationalError
from django.test import Client


@pytest.mark.django_db
def test_ready_reports_database_ok(client: Client) -> None:
    response = client.get("/health/ready", secure=True)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_live_reports_version(client: Client) -> None:
    response = client.get("/health/live", secure=True)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.django_db
def test_ready_returns_503_when_database_fails(client: Client) -> None:
    with mock.patch("patchmanager.core.views.connection.cursor", side_effect=OperationalError):
        response = client.get("/health/ready", secure=True)
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "fail"


def test_health_rejects_post(client: Client) -> None:
    assert client.post("/health/live", secure=True).status_code == 405


def test_security_headers_present(client: Client) -> None:
    response = client.get("/health/live", secure=True)
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response["Content-Security-Policy"]
    assert response["Strict-Transport-Security"].startswith("max-age=63072000")


def test_plain_http_redirects_to_https(client: Client) -> None:
    assert client.get("/").status_code == 301
