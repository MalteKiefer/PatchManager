"""Upload a plan, set its scale and drag a rack onto it, in the browser."""

import io

import pytest
from PIL import Image
from playwright.sync_api import Page, expect

from patchmanager.dcim.models import Rack
from patchmanager.floorplans.models import FloorPlan
from patchmanager.tenancy import rls
from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def _click(page: Page, x: float, y: float) -> None:
    """Click at plan pixel (x, y); the SVG is drawn 1:1."""
    box = page.locator("#plan").bounding_box()
    assert box is not None
    page.mouse.click(box["x"] + x, box["y"] + y)


def test_upload_scale_and_place(page: Page, live_server, super_admin, make_tree, tmp_path) -> None:
    tree = make_tree()
    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/plans/{tree['floor'].pk}/")
    plan = tmp_path / "plan.png"
    buffer = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(buffer, "PNG")
    plan.write_bytes(buffer.getvalue())
    page.locator("input[type=file]").set_input_files(str(plan))
    page.get_by_role("button", name="Upload").click()
    expect(page.get_by_text("no scale yet")).to_be_visible()
    expect(page.locator("#plan[data-ready]")).to_have_count(1)

    page.get_by_role("button", name="Set scale").click()
    _click(page, 100, 100)
    _click(page, 300, 100)
    page.get_by_label("Distance between the two points (m)").fill("4")
    page.get_by_role("button", name="Save scale").click()
    expect(page.get_by_text("Scale: 50.0 px per metre")).to_be_visible()
    expect(page.locator("#plan[data-ready]")).to_have_count(1)

    page.get_by_role("button", name="▭ A01").click()
    _click(page, 200, 150)
    expect(page.locator(".plan-item[data-kind=rack]")).to_have_count(1)
    with rls.system_context():
        rack = Rack.objects.get(pk=tree["rack"].pk)
    assert rack.plan_x is not None and rack.plan_y is not None
    assert (rack.plan_x, rack.plan_y) == pytest.approx((200, 150), abs=2)

    item = page.locator(".plan-item[data-kind=rack]")
    item.focus()
    page.keyboard.press("ArrowRight")
    expect(item).to_have_attribute("transform", f"translate({rack.plan_x + 10:g} {rack.plan_y:g})")
    with rls.system_context():
        assert Rack.objects.get(pk=tree["rack"].pk).plan_x == pytest.approx(
            rack.plan_x + 10, abs=0.2
        )
        assert FloorPlan.objects.get(floor=tree["floor"]).px_per_metre == 50
    assert not [e for e in errors if "Content-Security-Policy" in e]
