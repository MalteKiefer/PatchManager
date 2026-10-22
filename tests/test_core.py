import pytest
from django.test import Client

from patchmanager.accounts.models import Assignment
from patchmanager.core import ratelimit
from patchmanager.tenancy.rls import EnableRowLevelSecurity


@pytest.mark.django_db  # the 404 page shows the instance name
def test_metrics_requires_token(client: Client, monkeypatch) -> None:
    assert client.get("/metrics", secure=True).status_code == 404
    monkeypatch.setenv("PATCHMANAGER_METRICS_TOKEN", "s3cret")
    assert (
        client.get("/metrics", secure=True, headers={"Authorization": "Bearer wrong"}).status_code
        == 404
    )
    response = client.get("/metrics", secure=True, headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200
    assert b"patchmanager_http_requests_total" in response.content


def test_metrics_multiprocess(client: Client, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PATCHMANAGER_METRICS_TOKEN", "t")
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    assert (
        client.get("/metrics", secure=True, headers={"Authorization": "Bearer t"}).status_code
        == 200
    )


def test_request_id_roundtrip(client: Client) -> None:
    assert (
        client.get("/health/live", secure=True, headers={"X-Request-ID": "abcdef1234"})[
            "X-Request-ID"
        ]
        == "abcdef1234"
    )
    assert (
        len(
            client.get("/health/live", secure=True, headers={"X-Request-ID": "bad id!"})[
                "X-Request-ID"
            ]
        )
        == 16
    )


def test_ratelimit_window() -> None:
    for _ in range(3):
        assert ratelimit.hit("k", limit=3, window=60)
    assert not ratelimit.hit("k", limit=3, window=60)
    ratelimit.reset("k")
    assert ratelimit.hit("k", limit=3, window=60)


@pytest.mark.django_db
def test_ltree_descendant_lookup(make_user, grant, make_tenant) -> None:
    tenant = make_tenant()
    grant(make_user(), "viewer", tenant)
    assert Assignment.objects.filter(scope_path__descendant_of=tenant.policy_path).count() == 1
    assert Assignment.objects.filter(scope_path__descendant_of="ffff").count() == 0


def test_rls_operation_metadata() -> None:
    op = EnableRowLevelSecurity("tenant", column="id")
    assert op.describe() == "Enable row level security on tenant"
    assert op.deconstruct() == ("EnableRowLevelSecurity", ["tenant"], {"column": "id"})


def test_error_pages(client: Client, db) -> None:
    from patchmanager.accounts.middleware import SetupRequiredMiddleware

    SetupRequiredMiddleware._done = True
    assert client.get("/does-not-exist/", secure=True).status_code == 404
    csrf_client = Client(enforce_csrf_checks=True)
    assert csrf_client.post("/auth/login/", {}, secure=True).status_code == 403


def test_collectstatic_with_manifest_storage(tmp_path, settings) -> None:
    """The image build runs this; files missing for vendored assets fail here first."""
    from django.core.management import call_command

    settings.STATIC_ROOT = tmp_path
    settings.STORAGES = {
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}
    }
    call_command("collectstatic", interactive=False, verbosity=0)
    assert (tmp_path / "staticfiles.json").exists()


def test_german_catalog_is_complete() -> None:
    """Every message has a German translation and none is left fuzzy."""
    import re
    from pathlib import Path

    import patchmanager

    po = Path(patchmanager.__file__).parent / "locale" / "de" / "LC_MESSAGES" / "django.po"
    entries = po.read_text(encoding="utf-8").split("\n\n")[1:]  # the first block is the header
    missing, fuzzy = [], []
    for entry in entries:
        if entry.lstrip().startswith("#~"):
            continue  # obsolete
        msgid = next((line for line in entry.splitlines() if line.startswith("msgid")), "")
        values = re.findall(r'^msgstr(?:\[\d\])? ((?:".*"\n?)+)', entry, re.M)
        if values and any(not "".join(re.findall(r'"(.*)"', v)) for v in values):
            missing.append(msgid)
        if re.search(r"^#, .*\bfuzzy\b", entry, re.M):
            fuzzy.append(entry[:60])
    assert missing == [] and fuzzy == []


@pytest.mark.parametrize(
    ("allowed", "peer", "header", "seen"),
    [
        (["10.0.0.0/8"], "10.1.2.3", "198.51.100.7", "198.51.100.7"),
        (["10.0.0.0/8"], "10.1.2.3", "1.1.1.1, 203.0.113.9", "203.0.113.9"),  # rightmost
        (["10.0.0.0/8"], "192.0.2.1", "198.51.100.7", "192.0.2.1"),  # untrusted peer
        (["*"], "192.0.2.1", "2001:db8::1", "2001:db8::1"),
        (["*"], "192.0.2.1", "not-an-ip", "192.0.2.1"),
        (["127.0.0.1"], "127.0.0.1", "", "127.0.0.1"),
    ],
)
def test_client_ip_comes_from_trusted_proxies_only(settings, allowed, peer, header, seen) -> None:
    from django.http import HttpResponse
    from django.test import RequestFactory

    from patchmanager.core.middleware import ClientIPMiddleware

    settings.FORWARDED_ALLOW_IPS = allowed
    request = RequestFactory().get("/", REMOTE_ADDR=peer, HTTP_X_FORWARDED_FOR=header)
    ClientIPMiddleware(lambda r: HttpResponse())(request)
    assert request.META["REMOTE_ADDR"] == seen


def test_pages_for_signed_in_users_are_not_cached(admin_client) -> None:
    assert "no-store" in admin_client.get("/locations/", secure=True)["Cache-Control"]
    assert admin_client.get("/branding.css", secure=True)["Cache-Control"].startswith("public")


def test_overview_counts_what_the_user_may_see(admin_client, super_admin, system) -> None:
    from patchmanager.dcim.management.commands.seed_scale import seed

    seed(super_admin, racks=2, slug="ov")
    with system():
        from patchmanager.tenancy.models import Tenant

        tenant = Tenant.objects.get(slug="ov")
    admin_client.post("/tenants/switch/", {"tenant": tenant.pk}, secure=True)
    page = admin_client.get("/", secure=True).content.decode()
    assert "<dd>2</dd>" in page and "<dd>4</dd>" in page  # racks, devices
    assert "96<span> / 200</span>" in page  # both ends of 48 cables, of all ports
    assert "Site 1" in page and "R01" in page
