"""Read a switch configuration in the browser and look at the result."""

import pytest
from playwright.sync_api import Page, expect

from patchmanager.dcim import devices
from patchmanager.tenancy import rls
from tests.e2e.test_accessibility import _axe
from tests.e2e.test_locations import _sign_in
from tests.e2e.test_rooms import _shot
from tests.test_switching import ROUTED

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def test_read_configuration(page: Page, live_server, super_admin, make_tree, make_type) -> None:
    tree = make_tree()
    with rls.system_context():
        model = make_type("2930F", ports=[{"name_pattern": "{n}", "count": 10}])
        device = devices.create(
            tree["request"], super_admin, device_type=model, room=tree["room"], name="CORE-1"
        )
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/devices/{device.pk}/")
    page.get_by_role("link", name="Switching and routing").click()
    page.get_by_role("link", name="Read a configuration").click()
    page.get_by_label("Configuration").fill(ROUTED)
    page.get_by_role("button", name="Show changes").click()
    expect(page.get_by_text("New VLAN 20 SERVERS")).to_be_visible()
    assert _axe(page) == []
    _shot(page, "switch-read")
    page.get_by_role("button", name="Apply changes").click()
    expect(page.get_by_text("Configuration read")).to_be_visible()
    expect(page.locator(".vlan-port")).to_have_count(11)  # 10 ports and Trk1
    assert _axe(page) == []
    _shot(page, "switching")
    page.goto(f"{live_server.url}/networks/stacks/new/")
    assert _axe(page) == []
