"""Connect ports, edit cables, trace paths."""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.cabling import services, trace
from patchmanager.cabling.models import Cable
from patchmanager.core.utils import parse_uuid
from patchmanager.dcim.models import Device, Port
from patchmanager.locations.views import node_url

BOX_W, BOX_H, GAP = 150, 54, 46


class CableForm(forms.ModelForm):  # type: ignore[type-arg]
    """Cable attributes."""

    class Meta:
        model = Cable
        fields = list(services.CABLE_FIELDS)
        widgets = {
            "colour": forms.TextInput(attrs={"type": "color"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class ConnectForm(CableForm):
    """Far end of a new cable."""

    device = forms.ModelChoiceField(label=gettext_lazy("Device"), queryset=Device.objects.none())
    port = forms.ModelChoiceField(label=gettext_lazy("Port"), queryset=Port.objects.none())

    class Meta(CableForm.Meta):
        fields = ["device", "port", *services.CABLE_FIELDS]

    def __init__(self, *args: Any, source: Port, **kwargs: Any) -> None:
        """Devices of the same tenant; ports without cable of the chosen device."""
        super().__init__(*args, **kwargs)
        device_field = self.fields["device"]
        port_field = self.fields["port"]
        assert isinstance(device_field, forms.ModelChoiceField)  # noqa: S101  # nosec B101
        assert isinstance(port_field, forms.ModelChoiceField)  # noqa: S101  # nosec B101
        device_field.queryset = Device.objects.filter(tenant_id=source.tenant_id).order_by("name")
        device_id = parse_uuid(self.data.get("device")) if self.is_bound else None
        port_field.queryset = (
            free_ports(device_id, exclude=source.pk) if device_id else Port.objects.none()
        )


def free_ports(device_id: Any, *, exclude: Any = None) -> Any:
    """Ports of a device that carry no cable."""
    return (
        Port.objects.filter(device_id=device_id, cable_end__isnull=True)
        .exclude(pk=exclude)
        .order_by("order")
    )


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _visible_port(request: HttpRequest, pk: Any) -> Port:
    port = get_object_or_404(Port.objects.select_related("device"), pk=pk)
    if not policy.has_perm(request.user, "dcim.device.view", port.device):
        raise Http404
    return port


@login_required
@require_GET
def port_options(request: HttpRequest) -> HttpResponse:
    """``<option>`` list of free ports of a device (HTMX)."""
    device_id = parse_uuid(request.GET.get("device"))
    device = Device.objects.filter(pk=device_id).first() if device_id else None
    if device is None or not policy.has_perm(request.user, "dcim.device.view", device):
        raise Http404
    return render(
        request,
        "cabling/_port_options.html",
        {"ports": free_ports(device.pk, exclude=parse_uuid(request.GET.get("exclude")))},
    )


@login_required
@require_http_methods(["GET", "POST"])
def connect(request: HttpRequest, port_pk: str) -> HttpResponse:
    """Connect a port to another one."""
    source = _visible_port(request, port_pk)
    if not policy.has_perm(request.user, "cabling.cable.add", source.device):
        raise PermissionDenied
    form = ConnectForm(request.POST or None, source=source)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            cable = services.connect(
                request,
                _actor(request),
                source,
                data["port"],
                **{k: data[k] for k in services.CABLE_FIELDS},
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Connected."))
            return redirect("cabling:cable", cable.pk)
    return render(request, "cabling/connect.html", {"source": source, "form": form})


def _cable(request: HttpRequest, pk: Any) -> Cable:
    cable = get_object_or_404(Cable, pk=pk)
    if not policy.has_perm(request.user, "cabling.cable.view", cable):
        raise Http404
    return cable


@login_required
@require_http_methods(["GET", "POST"])
def cable_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Cable attributes and ends."""
    cable = _cable(request, pk)
    actor = _actor(request)
    form = CableForm(request.POST or None, instance=cable)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "delete":
                first = cable.ends.select_related("port").first()
                services.disconnect(request, actor, cable)
                messages.success(request, _("Cable removed."))
                return (
                    redirect("devices:port", first.port_id) if first else redirect("cabling:cables")
                )
            if form.is_valid():
                services.update(request, actor, cable, **form.cleaned_data)
                messages.success(request, _("Saved."))
                return redirect("cabling:cable", pk)
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("cabling:cable", pk)
    ends = list(cable.ends.select_related("port__device"))
    return render(
        request,
        "cabling/cable.html",
        {
            "cable": cable,
            "form": form,
            "ends": ends,
            "reach": services.scope(ends[0].port, ends[1].port) if len(ends) == 2 else "",
            "can_change": policy.has_perm(actor, "cabling.cable.change", cable),
            "can_delete": policy.has_perm(actor, "cabling.cable.delete", cable),
        },
    )


@login_required
def cable_list(request: HttpRequest) -> HttpResponse:
    """Cables of the current tenant, filterable by label, kind and media."""
    tenant = getattr(request, "tenant", None)
    cables = policy.scope_queryset(
        request.user, "cabling.cable.view", Cable.objects.filter(tenant=tenant)
    )
    query = request.GET.get("q", "").strip()
    if query:
        cables = cables.filter(
            Q(label__icontains=query) | Q(ends__port__device__name__icontains=query)
        ).distinct()
    for key in ("kind", "media", "status"):
        if request.GET.get(key):
            cables = cables.filter(**{key: request.GET[key]})
    cables = cables.prefetch_related("ends__port__device")[:500]
    return render(
        request,
        "cabling/cables.html",
        {
            "cables": cables,
            "filters": request.GET,
            "kinds": Cable.Kind.choices,
            "medias": Cable.Media.choices,
        },
    )


def full_name(port: Port) -> str:
    """Location path of a port, for example ``HQ / B1 / F1 / R1 / A01 / SW01 / Gi1/0/1``."""
    return " / ".join([*(str(n) for n in port.device.ancestors()), str(port.device), port.name])


def layout(path: trace.Path, hidden: frozenset[Any] = frozenset()) -> dict[str, Any]:
    """Boxes and connectors of the trace graph, left to right; ``hidden`` ports are anonymised."""
    boxes = [
        {
            "x": i * (BOX_W + GAP),
            "port": p,
            "passive": p.kind in trace.PASSIVE,
            "hidden": p.pk in hidden,
        }
        for i, p in enumerate(path.ports)
    ]
    lines = []
    for i, link in enumerate(path.links):
        x1 = i * (BOX_W + GAP) + BOX_W
        lines.append(
            {
                "x1": x1,
                "x2": x1 + GAP,
                "link": link,
                "colour": (link.cable.colour or "#64748b") if link.cable else "#94a3b8",
                "dash": "6 4"
                if link.cable is not None and link.cable.kind == "permanent"
                else ("2 4" if link.kind == "internal" else ""),
            }
        )
    width = max(len(boxes) * (BOX_W + GAP) - GAP, BOX_W)
    return {
        "boxes": boxes,
        "lines": lines,
        "width": width,
        "height": BOX_H + 30,
        "box_w": BOX_W,
        "box_h": BOX_H,
    }


@login_required
def trace_view(request: HttpRequest, port_pk: str) -> HttpResponse:
    """End to end path through a port, as a graph and as a hop list."""
    port = _visible_port(request, port_pk)
    if not policy.has_perm(request.user, "cabling.cable.trace", port.device):
        raise PermissionDenied
    path = trace.trace(port)
    hidden = [
        p for p in path.ports if not policy.has_perm(request.user, "dcim.device.view", p.device)
    ]
    return render(
        request,
        "cabling/trace.html",
        {
            "port": port,
            "path": path,
            "graph": layout(path, frozenset(p.pk for p in hidden)),
            "hidden": hidden,
            "hops": [(p, full_name(p)) for p in path.ports],
            "start_url": node_url(port.device),
        },
    )


@login_required
@require_POST
def disconnect_port(request: HttpRequest, port_pk: str) -> HttpResponse:
    """Remove the cable of a port."""
    port = _visible_port(request, port_pk)
    end = getattr(port, "cable_end", None)
    if end is None:
        return redirect("devices:port", port.pk)
    try:
        services.disconnect(request, _actor(request), end.cable)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    messages.success(request, _("Cable removed."))
    return redirect("devices:port", port.pk)
