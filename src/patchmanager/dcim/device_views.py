"""Device pages: create from a type, edit, place, document ports."""

import math
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.core.utils import parse_uuid
from patchmanager.dcim import devices
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.ipam.models import IPAddress
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
    "supplier",
    "warranty_end",
    "support_contract",
    "support_end",
    "end_of_life",
    "notes",
]


def _suppliers(form: Any, tenant_id: Any) -> None:
    """Suppliers of the device's tenant only."""
    from patchmanager.directory.models import Organization

    form.fields["supplier"].queryset = Organization.objects.filter(tenant_id=tenant_id)


_DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class DeviceForm(forms.ModelForm):  # type: ignore[type-arg]
    """Device attributes."""

    class Meta:
        model = Device
        fields = DEVICE_FIELDS
        widgets = {
            "purchase_date": _DATE,
            "in_service_date": _DATE,
            "warranty_end": _DATE,
            "support_end": _DATE,
            "end_of_life": _DATE,
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
            "lag",
            "lag_protocol",
            "mtu",
            "tunneled",
            "stp_edge",
            "stp_bpdu_guard",
            "stp_root_guard",
            "ospf_area",
            "ospf_passive",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args: Any, device: Device, **kwargs: Any) -> None:
        """Rear ports and link aggregations of the same device only."""
        super().__init__(*args, **kwargs)
        self.fields["rear_port"].queryset = device.ports.filter(kind="rear")  # type: ignore[attr-defined]
        lags = device.ports.filter(kind="lag")
        if not self.instance._state.adding:
            lags = lags.exclude(pk=self.instance.pk)
        self.fields["lag"].queryset = lags  # type: ignore[attr-defined]


class PortVLANForm(forms.Form):
    """802.1Q mode and VLANs of a port."""

    mode = forms.ChoiceField(
        label=gettext_lazy("VLAN mode"),
        required=False,
        choices=[("", "-"), ("access", gettext_lazy("access")), ("trunk", gettext_lazy("trunk"))],
    )
    untagged = forms.ModelChoiceField(
        label=gettext_lazy("Untagged / native VLAN"), queryset=Port.objects.none(), required=False
    )
    tagged = forms.ModelMultipleChoiceField(
        label=gettext_lazy("Tagged VLANs"),
        queryset=Port.objects.none(),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 6}),
    )

    def __init__(self, *args: Any, port: Port, user: Any, **kwargs: Any) -> None:
        """VLANs usable on this port that the user may see."""
        from patchmanager.networks.services import vlans_for_port

        super().__init__(*args, **kwargs)
        for name in ("untagged", "tagged"):
            self.fields[name].queryset = vlans_for_port(port, user)  # type: ignore[attr-defined]


class NewPortForm(forms.ModelForm):  # type: ignore[type-arg]
    """An extra port."""

    class Meta:
        model = Port
        fields = ["name", "kind", "connector", "speed_mbps"]


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _peer(port: Port) -> Any:
    """The port on the other end of this port's cable."""
    from patchmanager.cabling.models import CableEnd

    end = CableEnd.objects.filter(port=port).select_related("cable").first()
    if end is None:
        return None
    other = (
        CableEnd.objects.filter(cable=end.cable)
        .exclude(pk=end.pk)
        .select_related("port__device")
        .first()
    )
    return other.port if other else None


HIDDEN = "hidden"  # far end exists but the viewer may not see its device


def _visible_or_hidden(user: Any, port: Port | None) -> Any:
    if port is None or policy.has_perm(user, "dcim.device.view", port.device):
        return port
    return HIDDEN


def neighbours(user: Any, device: Device) -> list[tuple[Port, Any]]:
    """For each cabled port: the far end of its traced path (dependency view)."""
    from patchmanager.cabling.trace import trace_many

    if not policy.has_perm(user, "cabling.cable.trace", device):
        return []
    result = []
    ports = list(device.ports.filter(cable_end__isnull=False).select_related("device"))
    paths = trace_many(ports)
    for port in ports:
        path = paths[port.pk]
        far = path.ports[-1] if path.ports[0].pk == port.pk else path.ports[0]
        result.append((port, _visible_or_hidden(user, far if far.pk != port.pk else None)))
    return result


def device_url(device: Device) -> str:
    """Detail URL of a device."""
    from django.urls import reverse

    return reverse("devices:device", args=[device.pk])


def _plan_point(params: Any) -> tuple[float, float] | None:
    """A position on the floor plan passed along when a device is added from the plan."""
    try:
        x, y = float(params.get("plan_x", "")), float(params.get("plan_y", ""))
    except ValueError:
        return None
    if not (math.isfinite(x) and math.isfinite(y)) or x < 0 or y < 0 or max(x, y) > 1e6:
        return None
    return round(x, 1), round(y, 1)


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
    spot = None if rack else _plan_point(request.POST if request.method == "POST" else request.GET)
    form = NewDeviceForm(
        request.POST or None, types=visible_types(request), rack=rack, initial=initial
    )
    _suppliers(form, parent.tenant_id)
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
                **({"plan_x": spot[0], "plan_y": spot[1]} if spot else {}),
                **data,
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(
                request, _("Device created with %(n)s ports.") % {"n": device.ports.count()}
            )
            if spot:
                return redirect("floorplans:editor", room.floor_id)
            return redirect(device_url(device))
    breadcrumbs = [(a, node_url(a)) for a in [*parent.ancestors(), parent]]
    return render(
        request,
        "dcim/device_form.html",
        {
            "form": form,
            "parent": parent,
            "breadcrumbs": breadcrumbs,
            "hidden": {"plan_x": spot[0], "plan_y": spot[1]} if spot else {},
        },
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
def port_create(request: HttpRequest, pk: str) -> HttpResponse:
    """Page that adds a port the device type does not define."""
    device = _device(request, pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "dcim.device.change", device):
        raise PermissionDenied
    form = NewPortForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            devices.add_port(request, actor, device, **form.cleaned_data)
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect(device_url(device))
    return render(
        request,
        "core/create.html",
        {
            "title": _("New port on %(device)s") % {"device": device},
            "form": form,
            "back_url": device_url(device),
            "breadcrumbs": [(a, node_url(a)) for a in [*device.ancestors(), device]],
        },
    )


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
    _suppliers(form, device.tenant_id)
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
            "ports": device.ports.select_related(
                "rear_port", "untagged_vlan", "cable_end__cable"
            ).prefetch_related(
                Prefetch(
                    "ip_addresses",
                    queryset=policy.scope_queryset(
                        actor, "ipam.ipaddress.view", IPAddress.objects.all()
                    ),
                )
            ),
            "neighbours": neighbours(actor, device),
            "configs": policy.scope_queryset(
                actor, "configs.config.view", device.configs.order_by("-version")
            )[:1],
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
    vlan_form = PortVLANForm(
        request.POST if action == "vlans" else None,
        port=port,
        user=actor,
        initial={
            "mode": port.vlan_mode,
            "untagged": port.untagged_vlan_id,
            "tagged": [t.vlan_id for t in port.tagged.all()],
        },
    )
    if request.method == "POST":
        try:
            if action == "edit" and form.is_valid():
                devices.update_port(
                    request, actor, Port.objects.get(pk=port.pk), **form.cleaned_data
                )
                messages.success(request, _("Saved."))
                return redirect("devices:port", port.pk)
            if action == "vlans" and vlan_form.is_valid():
                from patchmanager.networks.services import set_port_vlans

                data = vlan_form.cleaned_data
                set_port_vlans(
                    request,
                    actor,
                    port,
                    mode=data["mode"],
                    untagged=data["untagged"],
                    tagged=list(data["tagged"]),
                )
                messages.success(request, _("VLANs saved."))
                return redirect("devices:port", port.pk)
            if action == "delete":
                devices.delete_port(request, actor, port)
                return redirect(device_url(device))
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
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
            "vlan_form": vlan_form,
            "peer": _visible_or_hidden(actor, _peer(port)),
            "wlans": port.wlans.all(),
            "addresses": policy.scope_queryset(
                actor, "ipam.ipaddress.view", port.ip_addresses.select_related("vrf")
            ),
            "can_add_address": policy.has_perm(actor, "ipam.ipaddress.add", device)
            and policy.has_perm(actor, "dcim.device.change", device),
            "can_cable": policy.has_perm(actor, "cabling.cable.add", device),
            "can_trace": policy.has_perm(actor, "cabling.cable.trace", device),
            "can_change": policy.has_perm(actor, "dcim.device.change", device),
            "front_ports": port.front_ports.all() if port.kind == "rear" else [],
            "breadcrumbs": [(a, node_url(a)) for a in device.ancestors()]
            + [(device, device_url(device))],
        },
    )


@login_required
def port_panel(request: HttpRequest, pk: str) -> HttpResponse:
    """Side panel of a selected port in a rack drawing: details, cable and the whole path."""
    from patchmanager.cabling.trace import trace
    from patchmanager.cabling.views import full_name

    port = get_object_or_404(
        Port.objects.select_related("device__device_type", "cable_end__cable"), pk=pk
    )
    if not policy.has_perm(request.user, "dcim.device.view", port.device):
        raise Http404
    hops: list[tuple[Any, str]] = []
    if policy.has_perm(request.user, "cabling.cable.trace", port.device):
        for hop in trace(port).ports:
            visible = policy.has_perm(request.user, "dcim.device.view", hop.device)
            hops.append((hop if visible else None, full_name(hop) if visible else ""))
    end = getattr(port, "cable_end", None)
    return render(
        request,
        "dcim/_port_panel.html",
        {"port": port, "cable": end.cable if end else None, "hops": hops},
    )


@login_required
def device_panel(request: HttpRequest, pk: str) -> HttpResponse:
    """Side panel of a selected device in a rack drawing."""
    device = _device(request, pk)
    ports = device.ports.all()
    return render(
        request,
        "dcim/_device_panel.html",
        {
            "device": device,
            "port_count": ports.count(),
            "cabled": ports.filter(cable_end__isnull=False).count(),
            "space": RackSpace.objects.filter(device=device).first(),
        },
    )
