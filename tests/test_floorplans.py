import io
import json
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from patchmanager.accounts.models import Assignment, Role
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.cabling import services as cabling
from patchmanager.dcim import devices
from patchmanager.dcim.models import Device, Rack
from patchmanager.floorplans import geometry, services
from patchmanager.floorplans.models import FloorPlan
from patchmanager.locations import services as tree
from patchmanager.locations.models import Room
from patchmanager.metadata.models import Attachment
from patchmanager.tenancy import rls
from tests.conftest import login

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]


def _png(size: tuple[int, int] = (800, 600)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "PNG")
    return out.getvalue()


def _pdf() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(out, "PDF", resolution=72)
    return out.getvalue()


def test_contains_edges_and_area() -> None:
    assert geometry.contains(SQUARE, 5, 5)
    assert geometry.contains(SQUARE, 0, 5) and geometry.contains(SQUARE, 10, 10)
    assert not geometry.contains(SQUARE, 11, 5)
    assert not geometry.contains([[0, 0], [10, 0]], 1, 0)
    assert geometry.area(SQUARE) == 100
    assert geometry.distance((0, 0), (3, 4)) == 5


@given(st.integers(-500, 500), st.integers(-500, 500))
def test_contains_matches_box(xi: int, yi: int) -> None:
    x, y = xi / 10, yi / 10  # plan points are rounded to 0.1
    assert geometry.contains(SQUARE, x, y) == (0 <= x <= 10 and 0 <= y <= 10)


@pytest.fixture
def kit(make_tree, make_type, super_admin):
    t = make_tree()
    t["ap"] = make_type(
        "AP", form="floor", u_height=0, ports=[{"name_pattern": "eth0", "count": 1}]
    )
    return t


def _upload(kit, actor, data=None, name="plan.png") -> FloorPlan:
    with rls.system_context():
        return services.upload(kit["request"], actor, kit["floor"], name=name, data=data or _png())


@pytest.mark.django_db
def test_upload_replace_and_scale(kit, super_admin) -> None:
    plan = _upload(kit, super_admin)
    assert (plan.width_px, plan.height_px) == (800, 600)
    with rls.system_context():
        services.set_scale(kit["request"], super_admin, plan, [0, 0], [300, 400], 10)
        assert plan.px_per_metre == 50
        first = plan.image_id
        again = _upload(kit, super_admin, _png((400, 300)))
        assert again.pk == plan.pk and again.px_per_metre is None
        assert not Attachment.objects.filter(pk=first).exists()
        with pytest.raises(ServiceError):
            services.set_scale(kit["request"], super_admin, again, [0, 0], [1, 1], 1)
        with pytest.raises(ServiceError, match="outside"):
            services.set_scale(kit["request"], super_admin, again, [0, 0], [500, 0], 1)


@pytest.mark.django_db
def test_pdf_is_rasterised_and_svg_or_junk_refused(kit, super_admin) -> None:
    plan = _upload(kit, super_admin, _pdf(), "plan.pdf")
    assert plan.image.mime_type == "image/png" and plan.image.original_name == "plan.png"
    assert (plan.width_px, plan.height_px) == (625, 417)  # 150 dpi
    with pytest.raises(ServiceError, match="PDF"):
        _upload(kit, super_admin, b"%PDF-1.7 broken", "x.pdf")
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
    with pytest.raises(ServiceError):
        _upload(kit, super_admin, svg, "x.svg")


@pytest.mark.django_db
def test_outlines_validated(kit, super_admin) -> None:
    plan = _upload(kit, super_admin)
    with rls.system_context():
        room = services.set_outline(
            kit["request"], super_admin, plan, kit["room"], [[10, 10], [200, 10], [200, 200]]
        )
        assert len(room.outline) == 3
        for bad in ([[0, 0], [1, 1]], [[0, 0], [1, 0], [1, 1]], [[0, 0], [9000, 0], [0, 10]], "x"):
            with pytest.raises(ServiceError):
                services.set_outline(kit["request"], super_admin, plan, kit["room"], bad)
        services.set_outline(kit["request"], super_admin, plan, kit["room"], [])
        kit["room"].refresh_from_db()
        assert kit["room"].outline == []


@pytest.mark.django_db
def test_place_rack_and_device_moves_room(kit, super_admin) -> None:
    plan = _upload(kit, super_admin)
    with rls.system_context():
        r1 = services.set_outline(
            kit["request"], super_admin, plan, kit["room"], [[0, 0], [400, 0], [400, 600], [0, 600]]
        )
        r2 = tree.create(kit["request"], super_admin, Room, parent=kit["floor"], name="R2")
        services.set_outline(
            kit["request"], super_admin, plan, r2, [[400, 0], [800, 0], [800, 600], [400, 600]]
        )
        out = services.place(kit["request"], super_admin, plan, kit["rack"], [100, 100])
        assert out["outside_room"] is False
        out = services.place(kit["request"], super_admin, plan, kit["rack"], [600, 100])
        assert out["outside_room"] is True and Rack.objects.get(pk=kit["rack"].pk).plan_x == 600

        ap = devices.create(kit["request"], super_admin, device_type=kit["ap"], room=r1, name="ap1")
        other = devices.create(
            kit["request"], super_admin, device_type=kit["ap"], room=r2, name="ap2"
        )
        cable = cabling.connect(kit["request"], super_admin, ap.ports.get(), other.ports.get())
        assert str(cable.path) == str(kit["floor"].path)
        out = services.place(kit["request"], super_admin, plan, ap, [700.04, 300])
        assert out["moved"] is True and out["x"] == 700.0
        ap.refresh_from_db()
        assert ap.room_id == r2.pk and str(ap.path).startswith(str(r2.path))
        cable.refresh_from_db()
        assert str(cable.path) == str(r2.path)  # both ends now in R2

        # on the shared edge both rooms match: placed, room unchanged
        out = services.place(kit["request"], super_admin, plan, ap, [400, 300])
        assert out["moved"] is False and len(out["candidates"]) == 2
        services.unplace(kit["request"], super_admin, ap)
        assert Device.objects.get(pk=ap.pk).plan_x is None


@pytest.mark.django_db
def test_place_needs_permissions(kit, super_admin, make_user) -> None:
    plan = _upload(kit, super_admin)
    user = make_user()
    with rls.system_context():
        r2 = tree.create(kit["request"], super_admin, Room, parent=kit["floor"], name="R2")
        services.set_outline(
            kit["request"], super_admin, plan, r2, [[400, 0], [800, 0], [800, 600], [400, 600]]
        )
        ap = devices.create(
            kit["request"], super_admin, device_type=kit["ap"], room=kit["room"], name="ap1"
        )
        Assignment.objects.create(
            role=Role.objects.get(key="editor"),
            user=user,
            tenant=kit["tenant"],
            scope_path=kit["room"].path,
        )
        services.set_outline(
            kit["request"], super_admin, plan, kit["room"], [[0, 0], [400, 0], [400, 600], [0, 600]]
        )
        out = services.place(kit["request"], user, plan, ap, [400, 10])  # on the shared edge
        assert sorted(out["candidates"]) == ["R1", "hidden room"]
        assert services.room_label(user, r2) == "hidden room"
        services.place(kit["request"], user, plan, ap, [10, 10])
        with pytest.raises(PermissionDeniedError):
            services.place(kit["request"], user, plan, ap, [600, 10])  # would move into R2
        assert Device.objects.get(pk=ap.pk).plan_x == 10
        with pytest.raises(PermissionDeniedError):
            services.upload(kit["request"], user, kit["floor"], name="p.png", data=_png())
        with pytest.raises(PermissionDeniedError):
            services.set_outline(kit["request"], user, plan, r2, SQUARE)


@pytest.mark.django_db
def test_editor_and_json_endpoints(kit, admin_client: Client, super_admin) -> None:
    base = f"/plans/{kit['floor'].pk}/"
    page = admin_client.get(base, secure=True)
    assert page.status_code == 200 and b"no plan yet" in page.content
    node = admin_client.get(reverse("locations:node", args=["floor", kit["floor"].pk]), secure=True)
    assert base.encode() in node.content
    upload = admin_client.post(base + "upload/", {"file": io.BytesIO(_png())}, secure=True)
    assert upload.status_code == 302

    def call(action: str, body: object) -> tuple[int, dict[str, Any]]:
        response = admin_client.post(
            base + action + "/", json.dumps(body), content_type="application/json", secure=True
        )
        return response.status_code, response.json()

    assert call("scale", {"a": [0, 0], "b": [100, 0], "metres": 2}) == (200, {"px_per_metre": 50.0})
    assert call("scale", {"a": [0, 0], "b": [100, 0], "metres": "x"})[0] == 400
    status, data = call(
        "outline", {"room": str(kit["room"].pk), "points": [[0, 0], [300, 0], [300, 300]]}
    )
    assert status == 200 and len(data["points"]) == 3
    assert call("place", {"kind": "rack", "id": str(kit["rack"].pk), "point": [50, 50]})[0] == 200
    status, data = call("place", {"kind": "rack", "id": str(kit["rack"].pk), "point": [500, 500]})
    assert data["outside_room"] and "outside" in data["notice"]
    assert call("place", {"kind": "cable", "id": "x", "point": [1, 1]})[0] == 400
    assert call("unplace", {"kind": "rack", "id": str(kit["rack"].pk)}) == (200, {"ok": True})
    assert (
        admin_client.post(
            base + "place/", "[]", content_type="application/json", secure=True
        ).status_code
        == 400
    )
    body = admin_client.get(base, secure=True).content.decode()
    assert 'id="plan-scene"' in body and "A01" in body


@pytest.mark.django_db
def test_other_tenant_and_viewer(kit, make_tree, make_user, client: Client) -> None:
    other = make_tree(name="Elsewhere")
    user = make_user()
    with rls.system_context():
        Assignment.objects.create(
            role=Role.objects.get(key="viewer"),
            user=user,
            tenant=kit["tenant"],
            scope_path=kit["tenant"].policy_path,
        )
    login(client, user)
    assert client.get(f"/plans/{other['floor'].pk}/", secure=True).status_code == 404
    page = client.get(f"/plans/{kit['floor'].pk}/", secure=True)
    assert page.status_code == 200 and b"Upload plan" not in page.content
    response = client.post(
        f"/plans/{kit['floor'].pk}/upload/", {"file": io.BytesIO(_png())}, secure=True
    )
    assert response.status_code == 403


@pytest.mark.django_db
def test_review_guards(kit, super_admin) -> None:
    from patchmanager.metadata import services as metadata

    plan = _upload(kit, super_admin)
    with rls.system_context():
        with pytest.raises(ServiceError, match="in use"):
            metadata.remove(kit["request"], super_admin, plan.image)
        assert FloorPlan.objects.get(pk=plan.pk).image_id == plan.image_id
        for metres in (float("nan"), float("inf"), -1, 1e9):
            with pytest.raises(ServiceError):
                services.set_scale(kit["request"], super_admin, plan, [0, 0], [100, 0], metres)

        ap = devices.create(
            kit["request"], super_admin, device_type=kit["ap"], room=kit["room"], name="ap1"
        )
        services.place(kit["request"], super_admin, plan, ap, [50, 50])
        room2 = tree.create(kit["request"], super_admin, Room, parent=kit["floor"], name="R2")
        devices.place(
            kit["request"],
            super_admin,
            Device.objects.get(pk=ap.pk),
            room=room2,
            rack=None,
            position=None,
        )
        assert Device.objects.get(pk=ap.pk).plan_x == 50  # same floor keeps its spot
        floor2 = tree.create(
            kit["request"],
            super_admin,
            type(kit["floor"]),
            parent=kit["building"],
            name="F2",
            level=2,
        )
        room3 = tree.create(kit["request"], super_admin, Room, parent=floor2, name="R3")
        devices.place(
            kit["request"],
            super_admin,
            Device.objects.get(pk=ap.pk),
            room=room3,
            rack=None,
            position=None,
        )
        assert Device.objects.get(pk=ap.pk).plan_x is None


def test_huge_pdf_page_is_capped() -> None:
    import pypdfium2

    document = pypdfium2.PdfDocument.new()
    document.new_page(14400, 14400)  # 5 m square page
    out = io.BytesIO()
    document.save(out)
    document.close()
    with Image.open(io.BytesIO(services.rasterise_pdf(out.getvalue()))) as image:
        assert image.width * image.height <= services.MAX_PIXELS


@pytest.mark.django_db
def test_plan_links_and_add_device_from_plan(admin_client, kit, make_type, super_admin) -> None:
    from patchmanager.cabling import services as cabling
    from patchmanager.dcim import devices
    from patchmanager.dcim.models import Device

    _upload(kit, super_admin)
    request = kit["request"]
    with rls.system_context():
        switch_type = make_type("Plan switch", ports=[{"name_pattern": "ge{n}", "count": 2}])
        switch = devices.create(
            request,
            super_admin,
            device_type=switch_type,
            room=kit["room"],
            rack=kit["rack"],
            position=1,
            name="sw-plan",
        )
        ap = devices.create(
            request,
            super_admin,
            device_type=kit["ap"],
            room=kit["room"],
            name="ap-1",
            plan_x=200,
            plan_y=150,
        )
        rack = kit["rack"]
        rack.plan_x, rack.plan_y = 100, 100
        rack.save()
        cabling.connect(request, super_admin, ap.ports.get(), switch.ports.get(name="ge1"))
    response = admin_client.get(f"/plans/{kit['floor'].pk}/", secure=True)
    links = response.context["scene"]["links"]
    assert links == [
        {
            "from": ["device", str(ap.pk)],
            "to": ["rack", str(kit["rack"].pk)],
            "dashed": False,
            "colour": "#2563eb",
            "count": 1,
        }
    ]
    url = f"/devices/new/?room={kit['room'].pk}&plan_x=40.5&plan_y=60"
    assert 'name="plan_x" value="40.5"' in admin_client.get(url, secure=True).content.decode()
    created = admin_client.post(
        url,
        {
            "device_type": kit["ap"].pk,
            "name": "ap-2",
            "status": "active",
            "plan_x": "40.5",
            "plan_y": "60",
        },
        secure=True,
    )
    assert created.url == f"/plans/{kit['floor'].pk}/"
    with rls.system_context():
        assert (Device.objects.get(name="ap-2").plan_x, Device.objects.get(name="ap-2").plan_y) == (
            40.5,
            60,
        )
    bad = admin_client.get(f"/devices/new/?room={kit['room'].pk}&plan_x=nan&plan_y=1", secure=True)
    assert 'name="plan_x"' not in bad.content.decode()


@pytest.mark.django_db
def test_new_room_from_drawn_outline(admin_client: Client, kit, super_admin) -> None:
    from patchmanager.locations.models import Room

    _upload(kit, super_admin)
    floor = kit["floor"]
    url = f"/locations/floor/{floor.pk}/new/"
    outline = json.dumps([[10, 10], [200, 10], [200, 150], [10, 150]])
    page = admin_client.get(url, {"outline": outline}, secure=True).content.decode()
    assert 'name="outline"' in page and "outline you drew" in page
    created = admin_client.post(
        url, {"name": "Lab", "kind": "office", "description": "", "outline": outline}, secure=True
    )
    assert created.url == f"/plans/{floor.pk}/"
    with rls.system_context():
        assert Room.objects.get(name="Lab").outline[2] == [200.0, 150.0]
    for bad in ("[[1, 1], [2, 2]]", "[[0, 0], [9000, 0], [0, 10]]", "nope"):
        response = admin_client.post(
            url, {"name": "X", "kind": "office", "description": "", "outline": bad}, secure=True
        )
        assert response.status_code == 200
    with rls.system_context():
        assert not Room.objects.filter(name="X").exists()
