"""Create an API token in the browser and use it."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def test_token_ui_and_api(page: Page, live_server, super_admin, make_tree) -> None:
    make_tree()
    _sign_in(page, live_server, super_admin)
    page.goto(live_server.url + "/account/tokens/")
    page.get_by_label("Name").fill("automation")
    page.get_by_label("Write").check()
    page.get_by_role("button", name="Create token").click()
    expect(page.get_by_role("heading", name="Copy it now")).to_be_visible()
    token = page.locator("dd").first.inner_text().strip()
    assert token.startswith("pmat_")

    api = page.context.request
    headers = {"Authorization": f"Bearer {token}"}
    sites = api.get(live_server.url + "/api/v1/sites/", headers=headers).json()
    site = sites["results"][0]
    created = api.post(
        live_server.url + "/api/v1/buildings/",
        headers=headers,
        data={"site": site["id"], "name": "Annex"},
    )
    assert created.status == 201, created.text()
    schema = api.get(live_server.url + "/api/schema/?format=json", headers=headers)
    assert schema.json()["openapi"] == "3.1.0"

    page.goto(live_server.url + "/account/tokens/")
    expect(page.get_by_role("heading", name="Copy it now")).to_have_count(0)
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Revoke").click()
    expect(page.get_by_text("Token revoked.")).to_be_visible()
    assert api.get(live_server.url + "/api/v1/sites/", headers=headers).status == 401
