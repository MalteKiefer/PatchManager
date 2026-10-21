"""Connect two ports through a patch panel and trace the path in the browser."""

import re

import pytest
from playwright.sync_api import Page, expect

from patchmanager.dcim import devices
from patchmanager.library import services as library
from patchmanager.tenancy import rls
from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def test_connect_with_port_picker_and_trace(
    page: Page, live_server, super_admin, make_tree, make_type
) -> None:
    tree = make_tree()
    request = tree["request"]
    with rls.system_context():
        switch = make_type("Switch", ports=[{"name_pattern": "ge{n}", "count": 4}])
        panel = make_type("Panel", ports=[{"name_pattern": "R{n}", "kind": "rear", "count": 4}])
        library.add_port_template(
            request,
            super_admin,
            panel,
            name_pattern="F{n}",
            kind="front",
            count=4,
            rear_template=panel.port_templates.get(),
        )
        sw = devices.create(request, super_admin, device_type=switch, room=tree["room"], name="sw1")
        devices.create(request, super_admin, device_type=panel, room=tree["room"], name="pp1")
        source = sw.ports.get(name="ge1")
    errors: list[str] = []
    page.on(
        "console", lambda m: errors.append(m.text) if "Content-Security-Policy" in m.text else None
    )
    _sign_in(page, live_server, super_admin)

    page.goto(f"{live_server.url}/devices/ports/{source.pk}/")
    page.get_by_role("link", name="Connect").click()
    page.wait_for_load_state("load")  # HTMX is deferred; it binds the picker on DOMContentLoaded
    page.wait_for_function("() => window.htmx !== undefined")
    page.locator("#id_device").select_option(label="pp1")
    port_select = page.locator("#id_port")
    expect(port_select.locator("option", has_text="F2")).to_have_count(1)  # filled by HTMX
    port_select.select_option(label="F2 (front port (passive))")
    page.get_by_role("button", name="Connect").click()
    expect(page.get_by_text("Connected.")).to_be_visible()

    page.get_by_role("link", name="Trace").first.click()
    expect(page.get_by_text("incomplete")).to_be_visible()
    expect(page.get_by_role("img", name="Path graph")).to_be_visible()
    expect(page.locator("ol")).to_contain_text(re.compile(r"pp1 / R2"))
    assert errors == []
