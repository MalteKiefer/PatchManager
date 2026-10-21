"""Location tree to rack elevation in the browser."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.conftest import PASSWORD, add_totp

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def _sign_in(page: Page, live_server, user) -> None:
    totp = add_totp(user)
    page.goto(live_server.url + "/auth/login/")
    page.get_by_label("Email").fill(user.email)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.get_by_label("Code").fill(totp.now())
    page.get_by_role("button", name="Verify").click()


def test_site_to_rack_and_move_block(page: Page, live_server, super_admin, make_tenant) -> None:
    make_tenant(name="ACME")
    csp: list[str] = []
    page.on(
        "console", lambda m: csp.append(m.text) if "Content-Security-Policy" in m.text else None
    )
    _sign_in(page, live_server, super_admin)

    page.get_by_role("link", name="Locations").click()
    page.get_by_role("link", name="New site").click()
    page.get_by_label("Name").fill("Lab")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_role("heading", name="Lab", exact=True)).to_be_visible()

    for name in ("North wing", "Ground floor", "Server room"):
        page.get_by_text("Add", exact=True).click()
        page.locator("form:has(input[value=add])").get_by_label("Name").fill(name)
        page.locator("form:has(input[value=add])").get_by_role("button", name="Create").click()
        expect(page.get_by_role("heading", name=name)).to_be_visible()

    page.get_by_text("Add", exact=True).click()
    form = page.locator("form:has(input[value=add])")
    form.get_by_label("Name").fill("A01")
    form.get_by_label("Height (U)").fill("12")
    form.get_by_role("button", name="Create").click()
    expect(page).to_have_url(re.compile(r"/racks/"))
    expect(page.get_by_role("navigation", name="Breadcrumb")).to_contain_text("Lab")

    page.get_by_text("Reserve units").click()
    reserve = page.locator("form:has(input[value=reserve])")
    reserve.get_by_label("Label").fill("Switch")
    reserve.get_by_label("Lowest unit").fill("5")
    reserve.get_by_role("button", name="Reserve").click()
    page.locator("#rack-elevation[data-ready]").wait_for()
    block = page.get_by_role("button", name=re.compile("Switch, units 5 to 5"))
    block.focus()
    page.keyboard.press("ArrowUp")
    expect(page.get_by_role("button", name=re.compile("Switch, units 6 to 6"))).to_be_visible()
    assert csp == []
