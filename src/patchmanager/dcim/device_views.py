"""Device pages: create from a type, edit, place, document ports."""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.core.utils import parse_uuid
from patchmanager.dcim import devices
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.library.models import DeviceType
from patchmanager.library.views import visible_types
from patchmanager.locations.models import Room
from patchmanager.locations.views import node_url

DEVICE_FIELDS = [
    "name",
    "status",
    "serial",
    "asset_tag",
    "purchase_date",
    "in_service_date",
    "responsible",
    "notes",
]
_DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class DeviceForm(forms.ModelForm):  # type: ignore[type-arg]
    """Device attributes."""

    class Meta:
        model = Device
        fields = DEVICE_FIELDS
        widgets = {
            "purchase_date": _DATE,
            "in_service_date": _DATE,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class NewDeviceForm(DeviceForm):
    """Type and (for racks) position of a new device."""

    device_type = forms.ModelChoiceField(
        label=gettext_lazy("Device type"), queryset=DeviceType.objects.none()
    )
    position = forms.IntegerField(label=gettext_lazy("Lowest unit"), min_value=1, required=False)
    face = forms.ChoiceField(
        label=gettext_lazy("Face"),
        choices=[("front", gettext_lazy("front")), ("rear", gettext_lazy("rear"))],
        initial="front",
    )

    class Meta(DeviceForm.Meta):
        fields = ["device_type", *DEVICE_FIELDS]

    def __init__(self, *args: Any, types: Any, rack: Rack | None, **kwargs: Any) -> None:
        """Offer rack types for racks, any type for rooms."""
        super().__init__(*args, **kwargs)
        self.fields["device_type"].queryset = types.filter(form="rack") if rack else types  # type: ignore[attr-defined]
        self.fields["name"].required = False
        self.fields["name"].help_text = _("Leave empty to use the type's naming convention.")
        if rack is None:
            del self.fields["position"], self.fields["face"]


class PlaceForm(forms.Form):
    """Move a device: room, optional rack and position."""

    room = forms.ModelChoiceField(label=gettext_lazy("Room"), queryset=Room.objects.none())
    rack = forms.ModelChoiceField(
        label=gettext_lazy("Rack"), queryset=Rack.objects.none(), required=False
    )
    position = forms.IntegerField(label=gettext_lazy("Lowest unit"), min_value=1, required=False)
    face = forms.ChoiceField(
        label=gettext_lazy("Face"),
        choices=[("front", gettext_lazy("front")), ("rear", gettext_lazy("rear"))],
    )

    def __init__(self, *args: Any, device: Device, **kwargs: Any) -> None:
        """Rooms and racks of the device's tenant."""
        super().__init__(*args, **kwargs)
        self.fields["room"].queryset = Room.objects.filter(tenant_id=device.tenant_id)  # type: ignore[attr-defined]
        self.fields["rack"].queryset = Rack.objects.filter(tenant_id=device.tenant_id)  # type: ignore[attr-defined]


class PortForm(forms.ModelForm):  # type: ignore[type-arg]
    """Port documentation."""

    class Meta:
        model = Port
        fields = [
            "name",
            "kind",
            "connector",
            "speed_mbps",
            "enabled",
            "label",
            "mac_address",
            "description",
            "rear_port",
            "rear_position",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args: Any, device: Device, **kwargs: Any) -> None:
        """Rear ports of the same device only."""
        super().__init__(*args, **kwargs)
        self.fields["rear_port"].queryset = device.ports.filter(kind="rear")  # type: ignore[attr-defined]


class NewPortForm(forms.ModelForm):  # type: ignore[type-arg]
    """An extra port."""

    class Meta:
        model = Port
        fields = ["name", "kind", "connector", "speed_mbps"]


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def device_url(device: Device) -> str:
    """Detail URL of a device."""
    from django.urls import reverse

    return reverse("devices:device", args=[device.pk])


@login_required
@require_http_methods(["GET", "POST"])
def device_create(request: HttpRequest) -> HttpResponse:
    """New device in a room (floor device) or in a rack."""
    rack_id, room_id = parse_uuid(request.GET.get("rack")), parse_uuid(request.GET.get("room"))
    rack = Rack.objects.filter(pk=rack_id).select_related("room").first() if rack_id else None
    room = rack.room if rack else Room.objects.filter(pk=room_id).first() if room_id else None
    if room is None:
        raise Http404
    parent: Any = rack or room
    if not policy.has_perm(request.user, "dcim.device.add", parent):
        raise PermissionDenied
    initial = {"position": request.GET.get("position")}
    form = NewDeviceForm(
        request.POST or None, types=visible_types(request), rack=rack, initial=initial
    )
    if request.method == "POST" and form.is_valid():
        data = {k: form.cleaned_data[k] for k in DEVICE_FIELDS}
        try:
            device = devices.create(
                request,
                _actor(request),
                device_type=form.cleaned_data["device_type"],
                room=room,
                rack=rack,
                position=form.cleaned_data.get("position"),
                face=form.cleaned_data.get("face") or "front",
                **data,
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(
                request, _("Device created with %(n)s ports.") % {"n": device.ports.count()}
            )
            return redirect(device_url(device))
    breadcrumbs = [(a, node_url(a)) for a in [*parent.ancestors(), parent]]
    return render(
        request,
        "dcim/device_form.html",
        {"form": form, "parent": parent, "breadcrumbs": breadcrumbs},
    )


def _device(request: HttpRequest, pk: Any) -> Device:
    device = get_object_or_404(
        Device.objects.select_related("device_type__vendor", "room", "rack"), pk=pk
    )
    if not policy.has_perm(request.user, "dcim.device.view", device):
        raise Http404
    return device


@login_required
@require_http_methods(["GET", "POST"])
def device_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Device attributes, placement, ports, photos and notes."""
    device = _device(request, pk)
    actor = _actor(request)
    can_change = policy.has_perm(actor, "dcim.device.change", device)
    action = request.POST.get("action")
    space = RackSpace.objects.filter(device=device).first()
    form = DeviceForm(request.POST if action == "edit" else None, instance=device)
    place_form = PlaceForm(
        request.POST if action == "place" else None,
        device=device,
        initial={
            "room": device.room_id,
            "rack": device.rack_id,
            "position": space.start if space else None,
            "face": space.face if space and space.face != "both" else "front",
        },
    )
    port_form = NewPortForm(request.POST if action == "add_port" else None)
    if request.method == "POST":
        try:
            if action == "edit" and form.is_valid():
                devices.update(
                    request, actor, device, **{k: form.cleaned_data[k] for k in DEVICE_FIELDS}
                )
                messages.success(request, _("Saved."))
                return redirect(device_url(device))
            if action == "place" and place_form.is_valid():
                data = place_form.cleaned_data
                devices.place(
                    request,
                    actor,
                    device,
                    room=data["room"],
                    rack=data["rack"],
                    position=data["position"],
                    face=data["face"],
                )
                messages.success(request, _("Device moved."))
                return redirect(device_url(device))
            if action == "add_port" and port_form.is_valid():
                devices.add_port(request, actor, device, **port_form.cleaned_data)
                return redirect(device_url(device))
            if action == "delete":
                parent = device.parent
                devices.delete(request, actor, device)
                messages.success(request, _("Deleted."))
                return redirect(node_url(parent))
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect(device_url(device))
    return render(
        request,
        "dcim/device.html",
        {
            "device": device,
            "space": space,
            "form": form,
            "place_form": place_form,
            "port_form": port_form,
            "ports": device.ports.select_related("rear_port"),
            "can_change": can_change,
            "can_delete": policy.has_perm(actor, "dcim.device.delete", device),
            "breadcrumbs": [(a, node_url(a)) for a in device.ancestors()],
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def port_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Document one port."""
    port = get_object_or_404(Port.objects.select_related("device"), pk=pk)
    device = _device(request, port.device_id)
    actor = _actor(request)
    action = request.POST.get("action")
    form = PortForm(request.POST if action == "edit" else None, instance=port, device=device)
    if request.method == "POST":
        try:
            if action == "edit" and form.is_valid():
                devices.update_port(
                    request, actor, Port.objects.get(pk=port.pk), **form.cleaned_data
                )
                messages.success(request, _("Saved."))
                return redirect("devices:port", port.pk)
            if action == "delete":
                devices.delete_port(request, actor, port)
                return redirect(device_url(device))
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("devices:port", port.pk)
    return render(
        request,
        "dcim/port.html",
        {
            "port": port,
            "device": device,
            "form": form,
            "can_change": policy.has_perm(actor, "dcim.device.change", device),
            "front_ports": port.front_ports.all() if port.kind == "rear" else [],
            "breadcrumbs": [(a, node_url(a)) for a in device.ancestors()]
            + [(device, device_url(device))],
        },
    )
