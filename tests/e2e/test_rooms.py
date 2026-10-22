"""A room in the browser: plan against installed, patch targets, floor plan lines, a survey."""

import io
import os

import pytest
from PIL import Image
from playwright.sync_api import Page, expect

from patchmanager.cabling import services as cabling
from patchmanager.dcim import devices
from patchmanager.floorplans import services as plans
from patchmanager.tenancy import rls
from tests.e2e.test_locations import _sign_in

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=False)]


def _shot(page: Page, name: str) -> None:
    """Keep a screenshot for review when PM_SCREENSHOTS names a folder."""
    if folder := os.environ.get("PM_SCREENSHOTS"):
        page.screenshot(path=os.path.join(folder, f"{name}.png"), full_page=True)


def test_office_room(page: Page, live_server, super_admin, make_tree, make_type) -> None:
    tree = make_tree()
    request, room = tree["request"], tree["room"]
    buffer = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(buffer, "PNG")
    with rls.system_context():
        room.kind_id = "office"
        room.save()
        outlet = make_type(
            "Double outlet",
            form="floor",
            u_height=0,
            category="outlet",
            ports=[{"name_pattern": "A{n}", "kind": "front", "count": 2}],
        )
        panel = make_type(
            "Panel 2",
            category="patch_panel",
            full_depth=False,
            ports=[{"name_pattern": "R{n}", "kind": "rear", "count": 2}],
        )
        o1 = devices.create(request, super_admin, device_type=outlet, room=room, name="O-01")
        pp = devices.create(
            request,
            super_admin,
            device_type=panel,
            room=room,
            rack=tree["rack"],
            position=1,
            name="PP1",
        )
        cabling.connect(request, super_admin, o1.ports.get(name="A1"), pp.ports.get(name="R1"))
        plan = plans.upload(
            request, super_admin, tree["floor"], name="p.png", data=buffer.getvalue()
        )
        plans.set_scale(request, super_admin, plan, [0, 0], [100, 0], 2)
        plans.set_outline(
            request, super_admin, plan, room, [[10, 10], [590, 10], [590, 390], [10, 390]]
        )
        plans.place(request, super_admin, plan, tree["rack"], [100, 100])
        plans.place(request, super_admin, plan, o1, [400, 300])

    errors: list[str] = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/locations/room/{room.pk}/")
    expect(page.get_by_role("heading", name="Planned and installed")).to_be_visible()
    expect(page.get_by_text("3 missing")).to_be_visible()  # 4 outlets planned, 1 there
    expect(page.locator("text=PP1").first).to_be_visible()  # A1 is patched to the panel
    _shot(page, "room")

    page.goto(f"{live_server.url}/plans/{tree['floor'].pk}/")
    expect(page.locator("#plan[data-ready]")).to_have_count(1)
    expect(page.locator("#layer-links line, #layer-links path")).not_to_have_count(0)
    page.locator(".plan-item[data-kind=device] .plan-pin").click()
    expect(page.locator("#plan-panel")).to_contain_text("PP1")
    expect(page.locator(".plan-item[data-kind=device]")).to_have_class(
        "plan-item cursor-move is-selected"
    )
    _shot(page, "floorplan")

    page.goto(f"{live_server.url}/locations/room/{room.pk}/surveys/new/")
    page.get_by_label("by").fill("Kim")
    page.locator("input[name=found-outlet]").fill("2")
    page.get_by_role("button", name="Save survey").click()
    expect(page.get_by_text("differs")).to_be_visible()
    _shot(page, "survey")
    assert not [e for e in errors if "Content-Security-Policy" in e]


def test_draw_a_new_room(page: Page, live_server, super_admin, make_tree) -> None:
    from patchmanager.locations.models import Room

    tree = make_tree()
    buffer = io.BytesIO()
    Image.new("RGB", (600, 400), "white").save(buffer, "PNG")
    with rls.system_context():
        plans.upload(
            tree["request"], super_admin, tree["floor"], name="p.png", data=buffer.getvalue()
        )
    _sign_in(page, live_server, super_admin)
    page.goto(f"{live_server.url}/plans/{tree['floor'].pk}/")
    expect(page.locator("#plan[data-ready]")).to_have_count(1)
    page.get_by_role("button", name="Draw new room").click()
    box = page.locator("#plan").bounding_box()
    assert box is not None
    for x, y in ((50, 50), (250, 50), (250, 200), (50, 200)):
        page.mouse.click(box["x"] + x, box["y"] + y)
    page.get_by_role("button", name="Finish outline").click()
    page.get_by_label("Name").fill("Lab")
    page.get_by_role("button", name="Create").click()
    expect(page.locator("#plan[data-ready]")).to_have_count(1)
    with rls.system_context():
        room = Room.objects.get(name="Lab")
    assert room.outline[1] == pytest.approx([250, 50], abs=2)
    _shot(page, "drawn-room")
