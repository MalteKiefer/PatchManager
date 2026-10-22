"""IP address management in the browser."""

import os

import pytest
from playwright.sync_api import Page, expect

from patchmanager.dcim.management.commands.seed_scale import seed
from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]
SHOTS = os.environ.get("PM_SCREENSHOTS", "")


def test_prefix_next_free_address(page: Page, live_server, super_admin) -> None:
    seed(super_admin, racks=1, slug="ipam")
    page.set_viewport_size({"width": 1440, "height": 900})
    _sign_in(page, live_server, super_admin)
    page.get_by_role("link", name="IP networks").click()
    page.get_by_role("link", name="New prefix").click()
    page.get_by_label("Prefix").fill("10.40.0.0/16")
    page.get_by_label("Status").select_option("container")
    page.get_by_role("button", name="Create").click()
    page.goto(live_server.url + "/ipam/prefixes/new/")
    page.get_by_label("Prefix").fill("10.40.1.0/24")
    page.get_by_label("Gateway").fill("10.40.1.1/24")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_role("heading", name="10.40.1.0/24")).to_be_visible()
    page.get_by_role("link", name="Add 10.40.1.1/24").click()
    page.get_by_label("DNS name").fill("gw.example.test")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_role("heading", name="10.40.1.1/24")).to_be_visible()
    page.goto(live_server.url + "/ipam/")
    expect(page.locator("tbody tr")).to_have_count(2)
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, "ipam-tree.png"))
        page.get_by_role("link", name="10.40.1.0/24").click()
        page.screenshot(path=os.path.join(SHOTS, "ipam-prefix.png"))
