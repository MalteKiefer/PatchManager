"""Device library to a mounted device with a photo, in the browser."""

import io
import re

import pytest
from PIL import Image
from playwright.sync_api import Page, expect

from tests.e2e.test_accessibility import _axe
from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def test_type_to_mounted_device_with_photo(
    page: Page, live_server, super_admin, make_tree, tmp_path
) -> None:
    tree = make_tree(u_height=12)
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    _sign_in(page, live_server, super_admin)
    page.goto(live_server.url + "/library/")
    page.get_by_role("link", name="New device type").click()
    page.get_by_label("Vendor").fill("Acme")
    page.get_by_label("Model").fill("Edge 8")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_role("heading", name="Acme Edge 8")).to_be_visible()

    page.get_by_role("link", name="Add ports").click()
    form = page.locator("main form")
    form.get_by_label("Name").fill("port{n}")
    form.get_by_label("Count").fill("8")
    form.get_by_role("button", name="Add").click()
    expect(page.get_by_text("port1 … port8")).to_be_visible()

    page.goto(f"{live_server.url}/racks/{tree['rack'].pk}/")
    page.get_by_role("link", name="Mount device").click()
    page.get_by_label("Device type").select_option(label="Acme Edge 8")
    page.get_by_label("Name").fill("edge-01")
    page.get_by_label("Lowest unit").fill("4")
    page.get_by_role("button", name="Create").click()
    expect(page.get_by_role("heading", name=re.compile("edge-01"))).to_be_visible()
    assert _axe(page) == []
    page.get_by_role("link", name=re.compile("^Ports")).click()
    expect(page.get_by_role("link", name="port8")).to_be_visible()
    assert _axe(page) == []
    page.get_by_role("link", name="Notes and files").click()

    photo = tmp_path / "front.png"
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "orange").save(buffer, "PNG")
    photo.write_bytes(buffer.getvalue())
    page.locator("input[type=file]").set_input_files(str(photo))
    page.get_by_label("Caption").fill("Front view")
    page.get_by_role("button", name="Upload").click()
    expect(page.get_by_alt_text("Front view")).to_be_visible()
    page.goto(f"{live_server.url}/racks/{tree['rack'].pk}/")
    expect(page.get_by_role("group", name=re.compile("edge-01, units 4 to 4"))).to_be_visible()
    assert not [e for e in errors if "Content-Security-Policy" in e]
