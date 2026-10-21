"""Plans: upload (PDFs are rasterised), scale, room outlines, placing racks and floor devices."""

import io
import math
import threading
from typing import Any

import pypdfium2
from django.db import transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _
from PIL import Image

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.dcim.models import Device, Rack
from patchmanager.floorplans import geometry
from patchmanager.floorplans.models import FloorPlan
from patchmanager.locations.models import Floor, Room
from patchmanager.metadata import services as metadata
from patchmanager.metadata import uploads

MAX_SIDE = 6000
MAX_PIXELS = 25_000_000
_PDFIUM = threading.Lock()  # PDFium is not thread safe
MAX_POINTS = 64


def rasterise_pdf(data: bytes) -> bytes:
    """Render the first page of a PDF to PNG, longest side at most ``MAX_SIDE`` pixels."""
    with _PDFIUM:
        try:
            document = pypdfium2.PdfDocument(data)
        except pypdfium2.PdfiumError as exc:
            raise ServiceError(_("The PDF could not be read.")) from exc
        try:
            if len(document) < 1:
                raise ServiceError(_("The PDF has no pages."))
            page = document[0]
            width, height = (max(v, 1.0) for v in page.get_size())
            scale = min(
                150 / 72, MAX_SIDE / max(width, height), math.sqrt(MAX_PIXELS / (width * height))
            )
            image = page.render(scale=scale).to_pil()
        finally:
            document.close()
    out = io.BytesIO()
    image.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def upload(request: HttpRequest, actor: User, floor: Floor, *, name: str, data: bytes) -> FloorPlan:
    """Store a new plan image for ``floor`` (replacing the old one and its scale)."""
    require(actor, "locations.floor.change", floor)
    if data[:5] == b"%PDF-":
        data, name = rasterise_pdf(data), name.rsplit(".", 1)[0] + ".png"
    if not uploads.is_raster(data):  # checked before anything is stored
        raise ServiceError(_("Floor plans must be JPEG, PNG, WebP, GIF or PDF."))
    with transaction.atomic():
        attachment = metadata.attach(
            request, actor, floor, name=name, data=data, caption=_("Floor plan")
        )
        if attachment.kind != "image" or attachment.mime_type == "image/svg+xml":
            raise ServiceError(_("Floor plans must be JPEG, PNG, WebP, GIF or PDF."))
        _target, clean = metadata.read(actor, attachment)
        with Image.open(io.BytesIO(clean)) as image:
            width, height = image.size
        old = FloorPlan.objects.filter(floor=floor).first()
        plan, _created = FloorPlan.objects.update_or_create(
            floor=floor,
            defaults={
                "tenant_id": floor.tenant_id,
                "image": attachment,
                "width_px": width,
                "height_px": height,
                "px_per_metre": None,
            },
        )
        if old is not None and old.image_id != attachment.pk:
            metadata.remove(request, actor, old.image)
    audit.record(
        "floorplans.plan.uploaded", request=request, obj=floor, data={"size": [width, height]}
    )
    return plan


def _point(plan: FloorPlan, value: Any) -> tuple[float, float]:
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError) as exc:
        raise ServiceError(_("Invalid coordinates.")) from exc
    if not (0 <= x <= plan.width_px and 0 <= y <= plan.height_px):
        raise ServiceError(_("The point lies outside the plan."))
    return round(x, 1), round(y, 1)


def set_scale(
    request: HttpRequest, actor: User, plan: FloorPlan, a: Any, b: Any, metres: float
) -> FloorPlan:
    """Scale from two points whose real distance is known."""
    require(actor, "locations.floor.change", plan.floor)
    p, q = _point(plan, a), _point(plan, b)
    pixels = geometry.distance(p, q)
    if not (math.isfinite(metres) and 0 < metres <= 100_000) or pixels < 5:
        raise ServiceError(_("Choose two distinct points and a positive distance."))
    plan.px_per_metre = pixels / metres
    plan.save(update_fields=["px_per_metre", "updated_at"])
    audit.record(
        "floorplans.plan.scaled",
        request=request,
        obj=plan.floor,
        data={"px_per_metre": plan.px_per_metre},
    )
    return plan


def set_outline(
    request: HttpRequest, actor: User, plan: FloorPlan, room: Room, points: Any
) -> Room:
    """Draw (or with no points clear) the outline of a room of this floor."""
    require(actor, "locations.room.change", room)
    if room.floor_id != plan.floor_id:
        raise ServiceError(_("The room is not on this floor."))
    if not isinstance(points, list) or (points and not 3 <= len(points) <= MAX_POINTS):
        raise ServiceError(_("An outline needs 3 to %(max)s points.") % {"max": MAX_POINTS})
    polygon = [list(_point(plan, p)) for p in points]
    if polygon and geometry.area(polygon) < 25:
        raise ServiceError(_("The outline is too small."))
    room.outline = polygon
    room.save(update_fields=["outline", "updated_at"])
    audit.record(
        "locations.room.outline_changed", request=request, obj=room, data={"points": len(polygon)}
    )
    return room


def room_label(actor: Any, room: Room) -> str:
    """Room name, or a placeholder if the actor may not see the room."""
    return str(room) if policy.has_perm(actor, "locations.room.view", room) else _("hidden room")


def rooms_at(plan: FloorPlan, x: float, y: float) -> list[Room]:
    """Rooms of the floor whose outline contains the point."""
    return [
        r
        for r in Room.objects.filter(floor_id=plan.floor_id)
        if r.outline and geometry.contains(r.outline, x, y)
    ]


def place(
    request: HttpRequest, actor: User, plan: FloorPlan, item: Rack | Device, point: Any
) -> dict[str, Any]:
    """Put a rack or floor device at a point.

    A floor device dropped into exactly one other room's outline moves to that room;
    several matches are reported back.
    """
    x, y = _point(plan, point)
    if isinstance(item, Rack):
        require(actor, "dcim.rack.change", item)
        if item.room.floor_id != plan.floor_id:
            raise ServiceError(_("This rack is on another floor."))
        Rack.objects.filter(pk=item.pk).update(plan_x=x, plan_y=y)
        audit.record("floorplans.rack.placed", request=request, obj=item, data={"x": x, "y": y})
        rooms = rooms_at(plan, x, y)
        outside = bool(item.room.outline) and item.room not in rooms
        return {
            "x": x,
            "y": y,
            "room": room_label(actor, item.room),
            "outside_room": outside,
            "candidates": [],
        }
    require(actor, "dcim.device.change", item)
    if item.rack_id or item.room.floor_id != plan.floor_id:
        raise ServiceError(_("Only floor devices of this floor can be placed."))
    rooms = rooms_at(plan, x, y)
    with transaction.atomic():
        Device.objects.filter(pk=item.pk).update(plan_x=x, plan_y=y)
        moved = False
        if len(rooms) == 1 and rooms[0].pk != item.room_id:
            if not policy.has_perm(actor, "dcim.device.add", rooms[0]):
                raise PermissionDeniedError(_("You do not have permission to do that."))
            from patchmanager.dcim import devices

            devices.place(
                request,
                actor,
                Device.objects.get(pk=item.pk),
                room=rooms[0],
                rack=None,
                position=None,
            )
            moved = True
    audit.record(
        "floorplans.device.placed", request=request, obj=item, data={"x": x, "y": y, "moved": moved}
    )
    item.refresh_from_db()
    return {
        "x": x,
        "y": y,
        "room": room_label(actor, item.room),
        "moved": moved,
        "candidates": [room_label(actor, r) for r in rooms] if len(rooms) > 1 else [],
    }


def unplace(request: HttpRequest, actor: User, item: Rack | Device) -> None:
    """Remove an item from the plan (keeps its room)."""
    require(actor, "dcim.rack.change" if isinstance(item, Rack) else "dcim.device.change", item)
    type(item).objects.filter(pk=item.pk).update(plan_x=None, plan_y=None)
    audit.record("floorplans.item.removed", request=request, obj=item)
