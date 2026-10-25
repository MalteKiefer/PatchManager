"""Switching and routing of one device: overview, reading a configuration, writing one."""

import json
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.configs import services as configs
from patchmanager.configs.models import DeviceConfig
from patchmanager.dcim.models import Device, Port
from patchmanager.library.models import PortKind
from patchmanager.locations.views import node_url
from patchmanager.networks import aruba, platforms, switchconfig
from patchmanager.networks.models import StackMember, SwitchSettings

# A fixed palette for VLAN colours in the port matrix (picked by VLAN ID).
PALETTE = (
    "#2563eb", "#16a34a", "#d97706", "#9333ea", "#0891b2", "#dc2626", "#4d7c0f", "#c026d3",
    "#0f766e", "#b45309", "#4f46e5", "#be123c",
)  # fmt: skip


class ReadForm(forms.Form):
    """A configuration as text, file or stored version, with template variables."""

    platform = forms.ChoiceField(
        label=gettext_lazy("Platform"),
        required=False,
        choices=[("", gettext_lazy("recognise automatically"))]
        + [(p.key, p.name) for p in platforms.PLATFORMS.values()],
    )
    content = forms.CharField(
        label=gettext_lazy("Configuration"),
        widget=forms.Textarea(attrs={"rows": 16, "spellcheck": "false", "class": "font-mono"}),
        required=False,
        strip=False,
    )
    file = forms.FileField(label=gettext_lazy("or a file"), required=False)
    version = forms.TypedChoiceField(
        label=gettext_lazy("or a stored version"), coerce=int, required=False, empty_value=None
    )
    variables = forms.CharField(
        label=gettext_lazy("Template variables (JSON)"),
        widget=forms.Textarea(attrs={"rows": 3, "spellcheck": "false", "class": "font-mono"}),
        required=False,
        help_text=gettext_lazy('For Aruba Central templates, for example {"hostname": "SW-01"}.'),
    )

    def __init__(self, *args: Any, versions: list[int], **kwargs: Any) -> None:
        """Offer the stored versions the user may read."""
        super().__init__(*args, **kwargs)
        field: Any = self.fields["version"]
        field.choices = [("", "-"), *((v, f"v{v}") for v in versions)]

    def clean_variables(self) -> dict[str, Any]:
        """A JSON object (or the first object of a list, as Aruba Central exports it)."""
        raw = self.cleaned_data.get("variables", "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise forms.ValidationError(_("The variables are not valid JSON.")) from exc
        if isinstance(data, list) and data and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            raise forms.ValidationError(_("The variables must be a JSON object."))
        return {str(k): v for k, v in data.items() if isinstance(v, str | int | float | bool)}

    def clean(self) -> dict[str, Any]:
        """Exactly one source; files must be UTF-8 text of at most 2 MB."""
        data = super().clean() or {}
        upload = data.get("file")
        if upload is not None:
            if upload.size > configs.MAX_BYTES:
                raise forms.ValidationError(_("The configuration is larger than 2 MB."))
            try:
                data["content"] = upload.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise forms.ValidationError(_("The file must be UTF-8 text.")) from exc
        elif not data.get("content", "").strip() and data.get("version") is None:
            raise forms.ValidationError(
                _("Paste a configuration, choose a file or a stored version.")
            )
        if len(data.get("content", "")) > configs.MAX_BYTES:
            raise forms.ValidationError(_("The configuration is larger than 2 MB."))
        return data


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _device(request: HttpRequest, pk: Any) -> Device:
    device = get_object_or_404(
        Device.objects.select_related("device_type", "room__floor__building__site"), pk=pk
    )
    if not policy.has_perm(request.user, "dcim.device.view", device):
        raise Http404
    return device


def _crumbs(device: Device) -> list[tuple[Any, str]]:
    return [(a, node_url(a)) for a in [*device.ancestors(), device]]


@login_required
def overview(request: HttpRequest, pk: str) -> HttpResponse:
    """VLANs, port matrix, aggregations, spanning tree, routing and stack of a device."""
    device = _device(request, pk)
    config = switchconfig.document(device, request.user)
    ports = list(
        Port.objects.filter(device=device, kind__in=[PortKind.INTERFACE, PortKind.LAG])
        .select_related("untagged_vlan", "lag")
        .prefetch_related("tagged__vlan")
    )

    def colour(vid: int) -> str:
        return PALETTE[vid % len(PALETTE)]

    matrix = []
    for p in ports:
        carrier = p.lag or p  # members of an aggregation carry its VLANs
        untagged = carrier.untagged_vlan
        matrix.append(
            {
                "port": p,
                "untagged": untagged,
                "colour": colour(untagged.vid) if untagged else "",
                "tagged": [(t.vlan.vid, colour(t.vlan.vid)) for t in carrier.tagged.all()],
            }
        )
    member = StackMember.objects.filter(device=device).select_related("stack").first()
    return render(
        request,
        "networks/switching.html",
        {
            "device": device,
            "config": config,
            "settings": SwitchSettings.objects.filter(device=device).first(),
            "vlans": [
                (v, colour(v.vid), aruba.compress(v.untagged), aruba.compress(v.tagged))
                for v in sorted(config.vlans.values(), key=lambda v: v.vid)
            ],
            "matrix": matrix,
            "lags": list(config.trunks.values()),
            "member": member,
            "stack_members": (
                member.stack.members.select_related("device") if member is not None else []
            ),
            "can_change": all(  # reading a configuration is an import
                policy.has_perm(request.user, f"dcim.device.{action}", device)
                for action in ("change", "import")
            ),
            "breadcrumbs": _crumbs(device),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def read(request: HttpRequest, pk: str) -> HttpResponse:
    """Read a switch configuration: preview the changes, then apply them."""
    device = _device(request, pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "dcim.device.change", device):
        raise PermissionDenied
    stored = [
        c
        for c in DeviceConfig.objects.filter(device=device).order_by("-version")[:50]
        if policy.has_perm(actor, "configs.config.view", c)
    ]
    form = ReadForm(
        request.POST or None,
        request.FILES or None,
        versions=[c.version for c in stored],
        initial={"version": request.GET.get("version")},
    )
    result = None
    read_as = ""
    if request.method == "POST" and form.is_valid():
        text = form.cleaned_data.get("content", "")
        source = _("pasted text")
        if form.cleaned_data.get("file") is not None:
            source = form.cleaned_data["file"].name
        elif form.cleaned_data.get("version") is not None and not text.strip():
            version = next(c for c in stored if c.version == form.cleaned_data["version"])
            text = configs.read(request, actor, version)
            source = f"v{version.version}"
        commit = request.POST.get("action") == "apply"
        try:
            parsed = platforms.parse(
                text, form.cleaned_data.get("platform", ""), form.cleaned_data["variables"]
            )
            read_as = platforms.VENDOR_NAMES.get(parsed.vendor, parsed.vendor)
            result = switchconfig.apply(
                request, actor, device, parsed, commit=commit, source=source
            )
        except platforms.ParseError as exc:
            form.add_error(None, str(exc))
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            if commit:
                messages.success(
                    request,
                    _("Configuration read: %(n)s changes.") % {"n": len(result.changes)},
                )
                return redirect("switching:overview", device.pk)
            # Keep the text for the apply step (a file is not sent twice by the browser).
            form = ReadForm(
                {**request.POST.dict(), "content": text},
                versions=[c.version for c in stored],
            )
            form.is_valid()
    return render(
        request,
        "networks/switch_read.html",
        {
            "device": device,
            "form": form,
            "result": result,
            "read_as": read_as,
            "breadcrumbs": _crumbs(device),
        },
    )


@login_required
def export(request: HttpRequest, pk: str) -> HttpResponse:
    """The documentation of a device written as an ArubaOS-Switch configuration."""
    device = _device(request, pk)
    try:
        text = aruba.render(switchconfig.document(device, request.user))
    except ServiceError as exc:
        raise Http404 from exc
    response = HttpResponse(text, content_type="text/plain; charset=utf-8")
    if request.GET.get("download"):
        name = slugify(device.name) or "device"
        response["Content-Disposition"] = f'attachment; filename="{name}.cfg"'
    return response
