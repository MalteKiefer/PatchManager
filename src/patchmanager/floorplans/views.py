"""Floor plan editor page and its JSON endpoints."""

import json
from typing import Any

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.core.utils import parse_uuid
from patchmanager.dcim.models import Device, Rack
from patchmanager.floorplans import services
from patchmanager.floorplans.models import FloorPlan
from patchmanager.locations.models import Floor, Room
from patchmanager.locations.views import node_url


class PlanUploadForm(forms.Form):
    """Image or PDF of the floor."""

    file = forms.FileField(label=gettext_lazy("Plan (JPEG, PNG, WebP, GIF or PDF)"))


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _floor(request: HttpRequest, pk: Any) -> Floor:
    floor = get_object_or_404(Floor, pk=pk)  # RLS hides other tenants
    if not policy.has_perm(request.user, "locations.floor.view", floor):
        raise Http404
    return floor


def _plan(floor: Floor) -> FloorPlan:
    plan = FloorPlan.objects.filter(floor=floor).select_related("floor").first()
    if plan is None:
        raise ServiceError(_("Upload a plan first."))
    return plan


def _json(request: HttpRequest) -> dict[str, Any]:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError as exc:
        raise ServiceError(_("Invalid request.")) from exc
    if not isinstance(data, dict):
        raise ServiceError(_("Invalid request."))
    return data


def _scene(request: HttpRequest, floor: Floor, plan: FloorPlan | None) -> dict[str, Any]:
    """Everything the editor draws, limited to what the user may see."""
    user = request.user
    rooms = [
        r
        for r in Room.objects.filter(floor=floor)
        if policy.has_perm(user, "locations.room.view", r)
    ]
    racks = [
        r
        for r in Rack.objects.filter(room__floor=floor).select_related("room")
        if policy.has_perm(user, "dcim.rack.view", r)
    ]
    devices = [
        d
        for d in Device.objects.filter(room__floor=floor, rack__isnull=True).select_related(
            "device_type", "room"
        )
        if policy.has_perm(user, "dcim.device.view", d)
    ]
    scale = plan.px_per_metre if plan and plan.px_per_metre else None

    def rack_size(rack: Rack) -> tuple[float, float]:
        if not scale:
            return 24, 36
        return 0.6 * scale, ((rack.depth_mm or 1000) / 1000) * scale

    return {
        "rooms": [
            {
                "id": str(r.pk),
                "name": r.name,
                "points": r.outline,
                "can_change": policy.has_perm(user, "locations.room.change", r),
            }
            for r in rooms
        ],
        "racks": [
            {
                "id": str(r.pk),
                "name": r.name,
                "x": r.plan_x,
                "y": r.plan_y,
                "rotation": r.plan_rotation,
                "w": rack_size(r)[0],
                "h": rack_size(r)[1],
                "url": node_url(r),
                "can_change": policy.has_perm(user, "dcim.rack.change", r),
            }
            for r in racks
        ],
        "devices": [
            {
                "id": str(d.pk),
                "name": d.name,
                "x": d.plan_x,
                "y": d.plan_y,
                "room": services.room_label(user, d.room),
                "glyph": d.device_type.category.glyph or "D",
                "colour": d.device_type.category.colour,
                "url": node_url(d),
                "r": max(8.0, 0.3 * scale) if scale else 8.0,
                "can_change": policy.has_perm(user, "dcim.device.change", d),
            }
            for d in devices
        ],
    }


@login_required
def editor(request: HttpRequest, floor_pk: str) -> HttpResponse:
    """The plan of a floor with rooms, racks and floor devices."""
    floor = _floor(request, floor_pk)
    plan = FloorPlan.objects.filter(floor=floor).first()
    can_change = policy.has_perm(request.user, "locations.floor.change", floor)
    scene = _scene(request, floor, plan)
    return render(
        request,
        "floorplans/editor.html",
        {
            "floor": floor,
            "plan": plan,
            "scene": scene,
            "can_change": can_change,
            "upload_form": PlanUploadForm(),
            "breadcrumbs": [(a, node_url(a)) for a in [*floor.ancestors(), floor]],
            "unplaced_racks": [r for r in scene["racks"] if r["x"] is None],
            "unplaced_devices": [d for d in scene["devices"] if d["x"] is None],
            "max_upload_mb": settings.UPLOAD_MAX_BYTES // (1024 * 1024),
        },
    )


@login_required
@require_POST
def upload(request: HttpRequest, floor_pk: str) -> HttpResponse:
    """Upload or replace the plan image."""
    floor = _floor(request, floor_pk)
    form = PlanUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, _("Choose a file to upload."))
        return redirect("floorplans:editor", floor.pk)
    upload_file = form.cleaned_data["file"]
    try:
        if upload_file.size > settings.UPLOAD_MAX_BYTES:
            raise ServiceError(_("The file is too large."))
        services.upload(
            request, _actor(request), floor, name=upload_file.name, data=upload_file.read()
        )
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Plan uploaded. Set the scale next."))
    return redirect("floorplans:editor", floor.pk)


def _api(handler: Any) -> Any:
    """JSON endpoint wrapper: login, POST, uniform errors (fixed messages only)."""

    @login_required
    @require_POST
    def view(request: HttpRequest, floor_pk: str) -> JsonResponse:
        floor = _floor(request, floor_pk)
        try:
            return JsonResponse(handler(request, floor, _json(request)))
        except PermissionDeniedError:
            return JsonResponse({"error": _("You do not have permission to do that.")}, status=403)
        except ServiceError as exc:
            return JsonResponse(
                {"error": exc.args[0] if exc.args else _("Not possible.")}, status=400
            )

    return view


def _scale(request: HttpRequest, floor: Floor, data: dict[str, Any]) -> dict[str, Any]:
    try:
        metres = float(data.get("metres", 0))
    except (TypeError, ValueError) as exc:
        raise ServiceError(_("Enter the distance in metres.")) from exc
    plan = services.set_scale(
        request, _actor(request), _plan(floor), data.get("a"), data.get("b"), metres
    )
    return {"px_per_metre": plan.px_per_metre}


def _outline(request: HttpRequest, floor: Floor, data: dict[str, Any]) -> dict[str, Any]:
    pk = parse_uuid(data.get("room"))
    room = Room.objects.filter(pk=pk, floor=floor).first() if pk else None
    if room is None:
        raise ServiceError(_("Choose a room of this floor."))
    services.set_outline(request, _actor(request), _plan(floor), room, data.get("points", []))
    return {"room": str(room.pk), "points": room.outline}


def _item(floor: Floor, data: dict[str, Any]) -> Rack | Device:
    pk = parse_uuid(data.get("id"))
    model: Any = (
        Rack if data.get("kind") == "rack" else Device if data.get("kind") == "device" else None
    )
    item = (
        model.objects.filter(pk=pk, room__floor=floor).select_related("room").first()
        if model and pk
        else None
    )
    if item is None:
        raise ServiceError(_("Object not found on this floor."))
    return item  # type: ignore[no-any-return]


def _place(request: HttpRequest, floor: Floor, data: dict[str, Any]) -> dict[str, Any]:
    item = _item(floor, data)
    result = services.place(request, _actor(request), _plan(floor), item, data.get("point"))
    if result["candidates"]:
        result["notice"] = _(
            "Several rooms overlap here (%(rooms)s). The room was not changed."
        ) % {"rooms": ", ".join(result["candidates"])}
    elif result.get("outside_room"):
        result["notice"] = _("%(item)s lies outside the outline of its room.") % {"item": item.name}
    return result


def _unplace(request: HttpRequest, floor: Floor, data: dict[str, Any]) -> dict[str, Any]:
    services.unplace(request, _actor(request), _item(floor, data))
    return {"ok": True}


scale = _api(_scale)
outline = _api(_outline)
place = _api(_place)
unplace = _api(_unplace)
