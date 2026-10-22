import io

import pytest
from django.core.cache import cache
from django.test import Client, RequestFactory
from PIL import Image

from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.cabling import services as cabling
from patchmanager.dcim import devices
from patchmanager.dcim.models import Rack
from patchmanager.instance import editions, services, state
from patchmanager.locations import services as tree
from patchmanager.tenancy import rls
from patchmanager.tenancy import services as tenancy
from tests.conftest import login

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fresh():
    cache.clear()
    yield
    cache.clear()


def _edition(actor, key: str, **kw) -> None:
    request = RequestFactory().post("/")
    request.user = actor
    services.save_edition(
        request,
        actor,
        edition=key,
        limits=kw.get("limits", {}),
        enable=kw.get("enable", []),
        disable=kw.get("disable", []),
    )


def test_default_is_enterprise_without_limits() -> None:
    edition = editions.current()
    assert edition.key == "enterprise" and edition.limits == {
        "racks": None,
        "users": None,
        "tenants": None,
    }
    assert editions.enabled("scim") and edition.cross_rack_cabling


def test_free_limits(make_tree, make_type, super_admin, make_user) -> None:
    t = make_tree()
    _edition(super_admin, "free")
    with rls.system_context():
        for n in range(2):
            tree.create(t["request"], super_admin, Rack, parent=t["room"], name=f"R{n}")
        with pytest.raises(ServiceError, match="at most 3 racks"):
            tree.create(t["request"], super_admin, Rack, parent=t["room"], name="R9")
        with pytest.raises(ServiceError, match="at most 1 users"):
            make_user("second@example.com")
        with pytest.raises(ServiceError, match="at most 1 tenants"):
            tenancy.create_tenant(t["request"], super_admin, name="Two", slug="two")
        switch = make_type("Sw", u_height=1, ports=[{"name_pattern": "ge{n}", "count": 1}])
        a = devices.create(
            t["request"],
            super_admin,
            device_type=switch,
            room=t["room"],
            rack=t["rack"],
            position=1,
            name="a",
        )
        b = devices.create(
            t["request"],
            super_admin,
            device_type=switch,
            room=t["room"],
            rack=Rack.objects.get(name="R0"),
            position=1,
            name="b",
        )
        c = devices.create(
            t["request"],
            super_admin,
            device_type=switch,
            room=t["room"],
            rack=t["rack"],
            position=2,
            name="c",
        )
        with pytest.raises(ServiceError, match="inside one rack"):
            cabling.connect(t["request"], super_admin, a.ports.get(), b.ports.get())
        cabling.connect(t["request"], super_admin, a.ports.get(), c.ports.get())
    assert not editions.enabled("api") and editions.current().reports == ("bom",)


def test_overrides_and_pinning(super_admin, settings) -> None:
    _edition(super_admin, "smb", limits={"tenants": 3}, enable=["scim"], disable=["webhooks"])
    edition = editions.current()
    assert (
        edition.limits["tenants"] == 3
        and editions.enabled("scim")
        and not editions.enabled("webhooks")
    )
    with pytest.raises(ServiceError):
        _edition(super_admin, "smb", enable=["scim"], disable=["scim"])
    with pytest.raises(ServiceError):
        _edition(super_admin, "gold")
    settings.EDITION = "free"
    cache.clear()
    assert editions.current().key == "free"
    with pytest.raises(ServiceError, match="PATCHMANAGER_EDITION"):
        _edition(super_admin, "enterprise")


def test_features_are_gated(admin_client: Client, super_admin, make_tree) -> None:
    make_tree()
    _edition(super_admin, "free")
    for url in (
        "/account/tokens/",
        "/manage/api-clients/",
        "/manage/sso/",
        "/manage/scim/",
        "/integrations/webhooks/",
    ):
        response = admin_client.get(url, secure=True)
        assert response.status_code == 403 and b"not part of this edition" in response.content, url
    assert admin_client.get("/api/v1/sites/", secure=True).status_code == 403
    assert (
        Client()
        .post("/api/oauth/token/", {"grant_type": "client_credentials"}, secure=True)
        .status_code
        == 403
    )
    assert Client().get("/scim/v2/Users", secure=True).status_code == 404
    assert admin_client.post("/exchange/import/", {}, secure=True).status_code == 403
    assert (
        b"Imports are not part of this edition"
        in admin_client.get("/exchange/", secure=True).content
    )
    nav = admin_client.get("/manage/", secure=True).content
    assert b"/manage/sso/" not in nav and b"/manage/edition/" in nav
    with rls.system_context():
        site = __import__("patchmanager.locations.models", fromlist=["Site"]).Site.objects.get()
    assert admin_client.get(f"/exchange/reports/site/{site.pk}.pdf", secure=True).status_code == 403
    assert admin_client.get(f"/exchange/reports/bom/{site.pk}.pdf", secure=True).status_code == 200


def test_edition_page(admin_client: Client, make_user) -> None:
    page = admin_client.get("/manage/edition/", secure=True).content
    assert b"Enterprise" in page and b"unlimited" in page
    response = admin_client.post(
        "/manage/edition/",
        {"edition": "integrator", "limit_tenants": "5", "feature_scim": "off"},
        secure=True,
        follow=True,
    )
    assert b"Edition saved" in response.content
    assert editions.current().limits["tenants"] == 5 and not editions.enabled("scim")
    other = Client()
    login(other, make_user())
    assert other.get("/manage/edition/", secure=True).status_code == 403


def _png(size=(800, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "navy").save(out, "PNG")
    return out.getvalue()


def test_branding(admin_client: Client, make_user) -> None:
    logo = io.BytesIO(_png())
    logo.name = "logo.png"
    response = admin_client.post(
        "/manage/branding/",
        {"name": "Acme NOC", "brand_hue": "30", "default_language": "de", "logo": logo},
        secure=True,
        follow=True,
    )
    assert b"Branding saved" in response.content and b"Acme NOC" in response.content
    css = Client().get("/branding.css", secure=True)
    assert css["Content-Type"].startswith("text/css") and b"oklch(0.52 0.1 30)" in css.content
    image = Client().get("/branding/logo.png", secure=True)
    assert image["Content-Type"] == "image/png" and "sandbox" in image["Content-Security-Policy"]
    with Image.open(io.BytesIO(image.content)) as stored:
        assert max(stored.size) <= services.LOGO_MAX_SIDE
    assert state.instance_name() == "Acme NOC"
    assert make_user("new@example.com").language == "de"
    bad = io.BytesIO(b"GIF89a....")
    bad.name = "x.gif"
    assert (
        b"PNG or JPEG"
        in admin_client.post(
            "/manage/branding/",
            {"name": "", "brand_hue": "30", "default_language": "en", "logo": bad},
            secure=True,
        ).content
    )
    admin_client.post(
        "/manage/branding/",
        {"name": "", "brand_hue": "190", "default_language": "en", "remove_logo": "on"},
        secure=True,
    )
    assert Client().get("/branding/logo.png", secure=True).status_code == 404
    user = make_user("plain@example.com")
    with pytest.raises(PermissionDeniedError):
        services.save_branding(
            RequestFactory().post("/"), user, name="x", brand_hue=1, default_language="en"
        )
    with pytest.raises(ServiceError):
        services.save_branding(
            RequestFactory().post("/"),
            User.objects.get(email="root@example.com"),
            name="x",
            brand_hue=400,
            default_language="en",
        )


def test_logo_on_pdf(admin_client: Client, make_tree, super_admin) -> None:
    from patchmanager.exchange import pdf

    t = make_tree()
    request = RequestFactory().post("/")
    request.user = super_admin
    services.save_branding(
        request, super_admin, name="", brand_hue=190, default_language="en", logo=_png()
    )
    with rls.system_context():
        assert pdf.bill_of_materials(super_admin, t["site"]).startswith(b"%PDF-")
    assert pdf._stored_logo() is not None


def test_system_page_and_jobs(
    admin_client: Client, settings, tmp_path, super_admin, mailoutbox
) -> None:
    from patchmanager.instance import jobs
    from patchmanager.instance.models import JobRun

    (tmp_path / "patchmanager-20261022T000000Z.dump").write_bytes(b"x" * 10)
    settings.BACKUP_DIR = str(tmp_path)
    jobs.started(task_id="t1", task=type("T", (), {"name": "patchmanager.demo"})())
    jobs.failed(task_id="t1", exception=ValueError("secret detail"))
    jobs.finished(task_id="t1", state="FAILURE")
    run = JobRun.objects.get(task_id="t1")
    assert run.status == "failure" and run.error == "ValueError"
    page = admin_client.get("/manage/system/", secure=True).content.decode()
    for text in (
        "Database",
        "Migrations",
        "up to date",
        "patchmanager.demo",
        "patchmanager-20261022T000000Z.dump",
        "deliver-webhooks",
        "Send a test mail",
    ):
        assert text in page, text
    response = admin_client.post("/manage/system/test-mail/", secure=True, follow=True)
    assert b"test mail was sent" in response.content and mailoutbox[0].to == ["root@example.com"]
    JobRun.objects.filter(task_id="t1").update(started_at=run.started_at.replace(year=2000))
    assert jobs.prune() == 1


def test_backups_and_licenses(admin_client: Client, settings, tmp_path) -> None:
    from patchmanager.core import system

    settings.BACKUP_DIR = ""
    assert system.backups() == {"configured": False, "files": []}
    settings.BACKUP_DIR = str(tmp_path)
    assert system.backups()["stale"] is True
    page = admin_client.get("/licenses/", secure=True).content.decode()
    assert "GNU Affero" in page and "Django" in page and "htmx" in page
    assert "Source code (AGPL-3.0)" in Client().get("/auth/login/", secure=True).content.decode()
