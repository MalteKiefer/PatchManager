"""Configuration versions in the browser (and their accessibility)."""

import pytest
from playwright.sync_api import Page, expect

from patchmanager.configs import services
from patchmanager.dcim.management.commands.seed_scale import seed
from patchmanager.dcim.models import Device
from patchmanager.tenancy import rls
from tests.e2e.test_accessibility import _axe
from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def test_store_view_and_compare(page: Page, live_server, super_admin) -> None:
    from django.test import RequestFactory

    seed(super_admin, racks=1, slug="cfg")
    with rls.system_context():
        device = Device.objects.get(name="sw-0001")
        request = RequestFactory().post("/")
        request.user = super_admin
        services.add(request, super_admin, device, "hostname sw-0001\nntp server 10.0.0.1\n")
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/devices/{device.pk}/")
    page.get_by_role("link", name="Configuration").click()
    page.get_by_role("link", name="New version").click()
    page.get_by_label("Configuration").fill("hostname sw-0001\nntp server 10.0.0.2\n")
    page.get_by_role("button", name="Store").click()
    expect(page.get_by_role("heading", name="sw-0001 · v2")).to_be_visible()
    assert _axe(page) == []
    page.get_by_role("link", name="Changes since v1").click()
    expect(page.get_by_text("+1", exact=True)).to_be_visible()
    assert _axe(page) == []
    page.goto(f"{live_server.url}/devices/{device.pk}/configs/")
    assert _axe(page) == []
