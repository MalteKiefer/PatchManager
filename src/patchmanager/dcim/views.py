"""Rack page with its elevation, and rack space actions."""

import json
from dataclasses import dataclass
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.dcim import services
from patchmanager.dcim.models import Rack, RackSpace
from patchmanager.locations import services as tree
from patchmanager.locations.forms import RackForm
from patchmanager.locations.views import node_url

UNIT_PX = 22  # height of one rack unit in the elevation drawing


class ReserveForm(forms.Form):
    """Reserve a block of units."""

    label = forms.CharField(label=gettext_lazy("Label"), max_length=150)
    start = forms.IntegerField(label=gettext_lazy("Lowest unit"), min_value=1)
    size = forms.IntegerField(label=gettext_lazy("Height (U)"), min_value=1, initial=1)
    face = forms.ChoiceField(
        label=gettext_lazy("Face"), choices=RackSpace.Face.choices, initial=RackSpace.Face.BOTH
    )


@dataclass(frozen=True)
class Block:
    """A rack space positioned for the SVG."""

    space: RackSpace
    y: int
    height: int
    image: Any = None


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def elevation(rack: Rack, face: str) -> dict[str, Any]:
    """Unit rows and blocks for one face, in drawing coordinates."""

    def y_of(unit: int) -> int:
        # Top edge of a unit row. Unit 1 sits at the bottom unless numbered from the top.
        index = unit - 1 if rack.descending_units else rack.u_height - unit
        return index * UNIT_PX

    rows = [{"unit": u, "y": y_of(u)} for u in range(1, rack.u_height + 1)]
    blocks = []
    for space in rack.spaces.select_related("device__device_type"):
        if space.face not in (face, RackSpace.Face.BOTH):
            continue
        top_unit = space.start if rack.descending_units else space.end
        device_type = space.device.device_type if space.device is not None else None
        image = (
            (device_type.front_image_id if face == "front" else device_type.rear_image_id)
            if device_type
            else None
        )
        blocks.append(Block(space, y_of(top_unit), space.size * UNIT_PX, image))
    return {"rows": rows, "blocks": blocks, "height": rack.u_height * UNIT_PX, "unit_px": UNIT_PX}


def _rack(request: HttpRequest, pk: Any, perm: str = "dcim.rack.view") -> Rack:
    rack = get_object_or_404(Rack.objects.select_related("room__floor__building__site"), pk=pk)
    if not policy.has_perm(request.user, perm, rack):
        raise Http404
    return rack


@login_required
@require_http_methods(["GET", "POST"])
def rack_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Elevation (front or rear), utilisation, reservations, rack settings."""
    rack = _rack(request, pk)
    actor = _actor(request)
    can_change = policy.has_perm(actor, "dcim.rack.change", rack)
    action = request.POST.get("action")
    form = RackForm(request.POST if action == "edit" else None, instance=rack)
    if request.method == "POST":
        try:
            if action == "edit" and form.is_valid():
                tree.update(request, actor, rack, **form.cleaned_data)
                messages.success(request, _("Saved."))
                return redirect(node_url(rack))
            if action == "delete":
                room = rack.room
                tree.delete(request, actor, rack)
                messages.success(request, _("Deleted."))
                return redirect(node_url(room))
        except ServiceError as exc:
            messages.error(request, str(exc))
            if action == "delete":
                return redirect(node_url(rack))
    face = "rear" if request.GET.get("face") == "rear" else "front"
    rack.refresh_from_db()
    context = {
        "rack": rack,
        "face": face,
        "elevation": elevation(rack, face),
        "stats": rack.utilisation(),
        "reservations": rack.spaces.filter(device__isnull=True),
        "device_spaces": rack.spaces.filter(device__isnull=False).select_related(
            "device__device_type__vendor"
        ),
        "can_add_device": policy.has_perm(actor, "dcim.device.add", rack),
        "form": form,
        "can_change": can_change,
        "can_delete": policy.has_perm(actor, "dcim.rack.delete", rack),
        "breadcrumbs": [(a, node_url(a)) for a in rack.ancestors()],
    }
    return render(request, "dcim/rack.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def space_create(request: HttpRequest, pk: str) -> HttpResponse:
    """Page that reserves a block of units."""
    rack = _rack(request, pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "dcim.rack.change", rack):
        raise Http404
    form = ReserveForm(request.POST or None, initial={"start": request.GET.get("start")})
    if request.method == "POST" and form.is_valid():
        try:
            services.reserve(request, actor, rack, **form.cleaned_data)
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Units reserved."))
            return redirect(node_url(rack))
    return render(
        request,
        "core/create.html",
        {
            "title": _("Reserve units in %(rack)s") % {"rack": rack},
            "form": form,
            "submit": _("Reserve"),
            "back_url": node_url(rack),
            "breadcrumbs": [(a, node_url(a)) for a in [*rack.ancestors(), rack]],
        },
    )


@login_required
@require_POST
def space_move(request: HttpRequest, pk: str, space_pk: str) -> JsonResponse:
    """Move a block (drag and drop or keyboard). JSON body: ``{"start": int, "face": str?}``."""
    rack = _rack(request, pk)
    space = get_object_or_404(RackSpace, pk=space_pk, rack=rack)
    try:
        payload = json.loads(request.body or b"{}")
        start = int(payload["start"])
        face = payload.get("face")
        if face is not None and face not in RackSpace.Face.values:
            raise ValueError(face)
    except ValueError, KeyError, TypeError:
        return JsonResponse({"error": _("Invalid request.")}, status=400)
    try:
        services.move(request, _actor(request), space, start=start, face=face)
    except ServiceError:
        return JsonResponse({"error": _("These units are taken or outside the rack.")}, status=409)
    return JsonResponse({"start": space.start, "end": space.end, "face": space.face})


@login_required
@require_POST
def space_delete(request: HttpRequest, pk: str, space_pk: str) -> HttpResponse:
    """Free a reservation."""
    rack = _rack(request, pk)
    space = get_object_or_404(RackSpace, pk=space_pk, rack=rack)
    try:
        services.release(request, _actor(request), space)
    except ServiceError as exc:
        messages.error(request, str(exc))
    return redirect(node_url(rack))
