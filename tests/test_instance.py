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
from patchmanager.instance import services, state
from patchmanager.locations import services as tree
from patchmanager.tenancy import rls

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fresh():
    cache.clear()
    yield
    cache.clear()


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
    assert system.backups() == {"configured": False, "files": [], "offsite": None}
    settings.BACKUP_DIR = str(tmp_path)
    assert system.backups()["stale"] is True
    assert system.backups()["offsite"] is None
    (tmp_path / "patchmanager-20261022T030000Z.dump").write_bytes(b"x")
    page = admin_client.get("/manage/system/", secure=True).content.decode()
    assert "--profile restore run --rm restore patchmanager-20261022T030000Z.dump" in page
    page = admin_client.get("/licenses/", secure=True).content.decode()
    assert "GNU Affero" in page and "Django" in page and "htmx" in page
    assert "Source code (AGPL-3.0)" in Client().get("/auth/login/", secure=True).content.decode()


def test_everything_is_on_without_limits(
    admin_client: Client, make_tree, make_type, super_admin, make_user
) -> None:
    """No editions: every feature is reachable and nothing is counted against a limit."""
    t = make_tree()
    for url in (
        "/account/tokens/",
        "/manage/api-clients/",
        "/manage/sso/",
        "/manage/scim/",
        "/integrations/webhooks/",
        "/exchange/",
        f"/plans/{t['floor'].pk}/",
    ):
        assert admin_client.get(url, secure=True).status_code == 200, url
    assert admin_client.get("/manage/edition/", secure=True).status_code == 404
    with rls.system_context():
        for n in range(5):
            tree.create(t["request"], super_admin, Rack, parent=t["room"], name=f"R{n}")
        for n in range(3):
            make_user(f"u{n}@example.com")
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
        cabling.connect(t["request"], super_admin, a.ports.get(), b.ports.get())  # across racks
    assert not hasattr(__import__("patchmanager.instance", fromlist=["x"]), "editions")
