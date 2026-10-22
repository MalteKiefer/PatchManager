"""Rack page with its elevation, and rack space actions."""

import json
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.dcim import elevation as drawing
from patchmanager.dcim import services
from patchmanager.dcim.models import Device, Rack, RackSpace
from patchmanager.locations import services as tree
from patchmanager.locations.forms import RackForm
from patchmanager.locations.models import Room
from patchmanager.locations.views import node_url


class ReserveForm(forms.Form):
    """Reserve a block of units."""

    label = forms.CharField(label=gettext_lazy("Label"), max_length=150)
    start = forms.IntegerField(label=gettext_lazy("Lowest unit"), min_value=1)
    size = forms.IntegerField(label=gettext_lazy("Height (U)"), min_value=1, initial=1)
    face = forms.ChoiceField(
        label=gettext_lazy("Face"), choices=RackSpace.Face.choices, initial=RackSpace.Face.BOTH
    )


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


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
    rows = drawing.connections(actor, rack)
    context = {
        "rack": rack,
        "face": face,
        "e": drawing.layout(actor, rack, face),
        "connections": rows,
        "cable_count": len({row.cable.pk for row in rows}),
        "add_url": (
            f"{reverse('devices:device-create')}?rack={rack.pk}"
            if policy.has_perm(actor, "dcim.device.add", rack)
            else ""
        ),
        "stats": rack.utilisation(),
        "reservations": rack.spaces.filter(device__isnull=True),
        "device_spaces": rack.spaces.filter(
            device__in=policy.scope_queryset(actor, "dcim.device.view", Device.objects.all())
        ).select_related("device__device_type__vendor"),
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


@login_required
def room_cabling(request: HttpRequest, pk: str) -> HttpResponse:
    """All racks of a room side by side, with the cables inside and between them."""
    room = get_object_or_404(Room, pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "locations.room.view", room):
        raise Http404
    face = "rear" if request.GET.get("face") == "rear" else "front"
    racks = policy.scope_queryset(actor, "dcim.rack.view", Rack.objects.filter(room=room))
    elevations, between, tray = drawing.room_layout(actor, racks.order_by("name"), face)
    height = max((e.top + e.height for e in elevations), default=0)
    width = max((e.x + e.width for e in elevations), default=0)
    return render(
        request,
        "dcim/room.html",
        {
            "room": room,
            "face": face,
            "elevations": elevations,
            "between": between,
            "box": {"x": 0, "y": -tray - 40, "w": width, "h": height + tray + 48},
            "title_y": -tray - 14,
            "breadcrumbs": [(a, node_url(a)) for a in [*room.ancestors(), room]],
        },
    )
