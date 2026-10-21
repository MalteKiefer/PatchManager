"""Search, import with dry run, and the activity log, in the browser."""

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def test_import_search_and_history(page: Page, live_server, super_admin, make_tree, tmp_path) -> None:
    make_tree()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    _sign_in(page, live_server, super_admin)

    data = tmp_path / "racks.csv"
    data.write_text("site;building;floor;room;name;u_height\nHQ;B1;F1;R1;Z42;24\n", encoding="utf-8")
    page.goto(live_server.url + "/exchange/")
    page.get_by_label("Table").select_option("racks")
    page.locator("input[type=file]").set_input_files(str(data))
    page.get_by_role("button", name="Check file").click()
    expect(page.get_by_role("heading", name=re.compile("Check import"))).to_be_visible()
    page.get_by_role("button", name="Import now").click()
    expect(page.get_by_text("1 created")).to_be_visible()

    page.get_by_role("searchbox", name="Search").fill("Z42")
    page.keyboard.press("Enter")
    page.get_by_role("link", name="Z42").click()
    expect(page.get_by_role("heading", name=re.compile("Z42"))).to_be_visible()
    page.get_by_role("link", name="History").click()
    expect(page.get_by_role("cell", name="created")).to_be_visible()
    expect(page.get_by_text(super_admin.email).first).to_be_visible()

    page.goto(live_server.url + "/history/?type=dcim.rack")
    expect(page.get_by_role("link", name="Z42")).to_be_visible()
    assert not [e for e in errors if "Content-Security-Policy" in e]
