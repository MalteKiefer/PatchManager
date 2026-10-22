"""Rack elevation with ports and cables, and the room drawing, in the browser."""

import os

import pytest
from playwright.sync_api import Page, expect

from patchmanager.dcim.management.commands.seed_scale import seed
from patchmanager.dcim.models import Rack
from patchmanager.tenancy import rls
from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]
SHOTS = os.environ.get("PM_SCREENSHOTS", "")  # folder for review screenshots, unset in CI


def _shot(page: Page, name: str) -> None:
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, f"{name}.png"), full_page=False)


def test_rack_shows_every_port_and_traces_cables(page: Page, live_server, super_admin) -> None:
    seed(super_admin, racks=2, slug="view")
    with rls.system_context():
        rack = Rack.objects.filter(tenant__slug="view").order_by("name").first()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.set_viewport_size({"width": 1440, "height": 1000})
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/racks/{rack.pk}/")
    ports = page.locator("#rack-elevation .rack-port")
    expect(ports).to_have_count(52 + 24)  # switch interfaces, panel fronts (rears are on the back)
    expect(page.locator("#rack-elevation .rack-wire")).to_have_count(24)
    _shot(page, "rack-front")
    page.locator("#rack-elevation .rack-port.is-partial path").first.hover()
    hot = page.locator("#rack-elevation .is-hot")
    expect(hot).to_have_count(3)  # switch port, panel port and the cord between them
    _shot(page, "rack-hover")
    page.get_by_role("button", name="Whole rack").click()
    expect(page.locator("#rack-elevation")).to_have_attribute("data-fit", "height")
    _shot(page, "rack-whole")
    page.goto(f"{live_server.url}/racks/{rack.pk}/?face=rear")
    expect(page.locator("#rack-elevation .rack-port")).to_have_count(24)  # panel rears
    _shot(page, "rack-rear")
    assert errors == []


def test_room_shows_racks_side_by_side_with_tray_cables(
    page: Page, live_server, super_admin
) -> None:
    from django.test import RequestFactory

    from patchmanager.cabling import services as cabling
    from patchmanager.dcim.models import Port

    seed(super_admin, racks=3, slug="room")
    with rls.system_context():
        request = RequestFactory().post("/")
        request.user = super_admin
        a = Port.objects.get(device__name="sw-0001", name="te1")
        b = Port.objects.get(device__name="sw-0003", name="te1")
        cabling.connect(request, super_admin, a, b, colour="#16a34a")
        room = Rack.objects.filter(tenant__slug="room").first().room
    page.set_viewport_size({"width": 1440, "height": 1000})
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/locations/room/{room.pk}/")
    page.get_by_role("link", name="Racks and cabling").click()
    expect(page.locator(".rack-svg .rack")).to_have_count(3)
    expect(page.locator(".rack-svg .rack-wire.is-tray")).to_have_count(1)
    page.locator(".rack-svg .rack-port.is-complete path").first.hover()
    expect(page.locator(".rack-svg .rack-wire.is-tray.is-hot")).to_have_count(1)
    _shot(page, "room")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_overview_and_shell(page: Page, live_server, super_admin, scheme) -> None:
    seed(super_admin, racks=3, slug="shell")
    page.emulate_media(color_scheme=scheme)
    page.set_viewport_size({"width": 1440, "height": 900})
    _sign_in(page, live_server, super_admin)
    page.goto(live_server.url + "/")
    expect(page.get_by_role("link", name="Overview")).to_have_attribute("aria-current", "page")
    page.keyboard.press("Control+k")
    expect(page.locator("#global-search")).to_be_focused()
    _shot(page, f"overview-{scheme}")
    page.get_by_role("link", name="Locations and racks").click()
    expect(page.get_by_role("link", name="Locations and racks")).to_have_attribute(
        "aria-current", "page"
    )
    _shot(page, f"locations-{scheme}")
