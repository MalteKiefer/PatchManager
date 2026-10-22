"""Configuration versions of a device: list, add, view, compare, download, delete."""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.configs import services
from patchmanager.configs.models import DeviceConfig
from patchmanager.dcim.models import Device
from patchmanager.locations.views import node_url


class ConfigForm(forms.Form):
    """Paste the configuration or choose a file."""

    content = forms.CharField(
        label=gettext_lazy("Configuration"),
        widget=forms.Textarea(attrs={"rows": 18, "spellcheck": "false", "class": "font-mono"}),
        required=False,
        strip=False,  # keep the text exactly, so an unchanged paste is no new version
    )
    file = forms.FileField(label=gettext_lazy("or a file"), required=False)
    comment = forms.CharField(label=gettext_lazy("Comment"), max_length=200, required=False)

    def clean(self) -> dict[str, Any]:
        """Exactly one of text and file; files must be UTF-8 text."""
        data = super().clean() or {}
        upload = data.get("file")
        if upload is not None:
            if upload.size > services.MAX_BYTES:
                raise forms.ValidationError(_("The configuration is larger than 2 MB."))
            try:
                data["content"] = upload.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise forms.ValidationError(_("The file must be UTF-8 text.")) from exc
            data["source"] = DeviceConfig.Source.UPLOAD
        elif not data.get("content", "").strip():
            raise forms.ValidationError(_("Paste a configuration or choose a file."))
        else:
            data["source"] = DeviceConfig.Source.MANUAL
        return data


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _device(request: HttpRequest, pk: Any) -> Device:
    device = get_object_or_404(Device.objects.select_related("device_type"), pk=pk)
    if not policy.has_perm(request.user, "dcim.device.view", device):
        raise Http404
    return device


def _config(request: HttpRequest, device: Device, version: int) -> DeviceConfig:
    config = get_object_or_404(DeviceConfig, device=device, version=version)
    if not policy.has_perm(request.user, "configs.config.view", config):
        raise Http404
    return config


def _crumbs(device: Device) -> list[tuple[Any, str]]:
    return [(a, node_url(a)) for a in [*device.ancestors(), device]]


@login_required
def config_list(request: HttpRequest, pk: str) -> HttpResponse:
    """All versions of a device's configuration."""
    device = _device(request, pk)
    actor = _actor(request)
    versions = policy.scope_queryset(
        actor, "configs.config.view", DeviceConfig.objects.filter(device=device)
    ).select_related("created_by")
    probe = DeviceConfig(device=device, tenant_id=device.tenant_id, path=device.path)
    return render(
        request,
        "configs/list.html",
        {
            "device": device,
            "versions": versions,
            "can_add": policy.has_perm(actor, "configs.config.add", probe),
            "breadcrumbs": _crumbs(device),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def config_create(request: HttpRequest, pk: str) -> HttpResponse:
    """Page that stores a new version."""
    device = _device(request, pk)
    form = ConfigForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        try:
            config, created = services.add(
                request,
                _actor(request),
                device,
                form.cleaned_data["content"],
                source=form.cleaned_data["source"],
                comment=form.cleaned_data["comment"],
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            if created:
                messages.success(request, _("Version %(n)s stored.") % {"n": config.version})
            else:
                messages.info(request, _("Unchanged since version %(n)s.") % {"n": config.version})
            return redirect("configs:version", device.pk, config.version)
    return render(
        request,
        "core/create.html",
        {
            "title": _("New configuration for %(device)s") % {"device": device},
            "form": form,
            "multipart": True,
            "submit": _("Store"),
            "back_url": reverse("configs:list", args=[device.pk]),
            "breadcrumbs": _crumbs(device),
        },
    )


@login_required
def config_version(request: HttpRequest, pk: str, version: int) -> HttpResponse:
    """One version with line numbers."""
    device = _device(request, pk)
    config = _config(request, device, version)
    text = services.read(request, _actor(request), config)
    previous = (
        DeviceConfig.objects.filter(device=device, version__lt=version).order_by("-version").first()
    )
    return render(
        request,
        "configs/version.html",
        {
            "device": device,
            "config": config,
            "lines": text.splitlines(),
            "previous": previous,
            "can_delete": policy.has_perm(request.user, "configs.config.delete", config),
            "breadcrumbs": _crumbs(device),
            "crumb": f"v{config.version}",
        },
    )


@login_required
def config_download(request: HttpRequest, pk: str, version: int) -> HttpResponse:
    """The version as a text file."""
    device = _device(request, pk)
    config = _config(request, device, version)
    text = services.read(request, _actor(request), config)
    response = HttpResponse(text, content_type="text/plain; charset=utf-8")
    name = f"{slugify(device.name) or 'device'}-v{config.version}.txt"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response


@login_required
def config_diff(request: HttpRequest, pk: str) -> HttpResponse:
    """Changes between two versions (``?a=`` older, ``?b=`` newer)."""
    device = _device(request, pk)
    try:
        a, b = int(request.GET.get("a", "")), int(request.GET.get("b", ""))
    except ValueError as exc:
        raise Http404 from exc
    old, new = _config(request, device, a), _config(request, device, b)
    actor = _actor(request)
    lines, added, removed = services.diff(
        services.read(request, actor, old), services.read(request, actor, new)
    )
    return render(
        request,
        "configs/diff.html",
        {
            "device": device,
            "old": old,
            "new": new,
            "lines": lines,
            "added": added,
            "removed": removed,
            "breadcrumbs": _crumbs(device),
        },
    )


@login_required
@require_POST
def config_delete(request: HttpRequest, pk: str, version: int) -> HttpResponse:
    """Delete one version."""
    device = _device(request, pk)
    config = _config(request, device, version)
    try:
        services.delete(request, _actor(request), config)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    messages.success(request, _("Version %(n)s deleted.") % {"n": version})
    return redirect("configs:list", device.pk)
