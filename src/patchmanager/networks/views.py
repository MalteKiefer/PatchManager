"""VLAN and WLAN pages."""

from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.core.utils import parse_uuid
from patchmanager.dcim.models import Port
from patchmanager.locations.models import Site
from patchmanager.networks import services
from patchmanager.networks.models import VLAN, WLAN

VLAN_FIELDS = ["vid", "name", "site", "status", "description"]
WLAN_FIELDS = ["ssid", "vlan", "security", "hidden", "description"]


class VLANForm(forms.ModelForm):  # type: ignore[type-arg]
    """VLAN attributes."""

    class Meta:
        model = VLAN
        fields = VLAN_FIELDS
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args: Any, tenant: Any, **kwargs: Any) -> None:
        """Sites of the tenant."""
        super().__init__(*args, **kwargs)
        self.fields["site"].queryset = Site.objects.filter(tenant=tenant)  # type: ignore[attr-defined]
        self.fields["site"].empty_label = _("All sites of the tenant")  # type: ignore[attr-defined]


class WLANForm(forms.ModelForm):  # type: ignore[type-arg]
    """WLAN attributes."""

    class Meta:
        model = WLAN
        fields = WLAN_FIELDS
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args: Any, tenant: Any, **kwargs: Any) -> None:
        """VLANs of the tenant."""
        super().__init__(*args, **kwargs)
        self.fields["vlan"].queryset = VLAN.objects.filter(tenant=tenant)  # type: ignore[attr-defined]


def _visible_ports(user: Any, ports: Any, perm: str = "dcim.device.view") -> list[Port]:
    """Ports whose device ``user`` may access with ``perm``."""
    return [p for p in ports if policy.has_perm(user, perm, p.device)]


class BroadcastForm(forms.Form):
    """Add a radio to a WLAN."""

    port = forms.ModelChoiceField(label=gettext_lazy("Wireless port"), queryset=Port.objects.none())

    def __init__(self, *args: Any, tenant: Any, user: Any, **kwargs: Any) -> None:
        """Wireless ports of the tenant on devices the user may change."""
        super().__init__(*args, **kwargs)
        field = self.fields["port"]
        assert isinstance(field, forms.ModelChoiceField)  # noqa: S101  # nosec B101
        radios = Port.objects.filter(tenant=tenant, kind="wireless").select_related("device")
        allowed = _visible_ports(user, radios, "dcim.device.change")
        field.queryset = Port.objects.filter(pk__in=[p.pk for p in allowed])


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _tenant(request: HttpRequest) -> Any:
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    return tenant


@login_required
@require_http_methods(["GET", "POST"])
def vlan_list(request: HttpRequest) -> HttpResponse:
    """VLANs of the current tenant; create new ones."""
    tenant = _tenant(request)
    form = VLANForm(request.POST or None, tenant=tenant)
    if request.method == "POST" and form.is_valid():
        try:
            vlan = services.create_vlan(request, _actor(request), tenant, **form.cleaned_data)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("VLAN created."))
            return redirect("networks:vlan", vlan.pk)
    candidates = (
        VLAN.objects.filter(tenant=tenant)
        .select_related("site")
        .annotate(
            access=Count("untagged_on", distinct=True), trunks=Count("tagged_on", distinct=True)
        )
    )
    vlans = [v for v in candidates if policy.has_perm(request.user, "networks.vlan.view", v)]
    return render(
        request,
        "networks/vlans.html",
        {
            "vlans": vlans,
            "form": form,
            "can_add": policy.has_perm(request.user, "networks.vlan.add", tenant),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def vlan_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit a VLAN and see where it is used."""
    vlan = get_object_or_404(VLAN.objects.select_related("site"), pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "networks.vlan.view", vlan):
        raise Http404
    form = VLANForm(request.POST or None, instance=vlan, tenant=vlan.tenant)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "delete":
                services.delete_vlan(request, actor, vlan)
                messages.success(request, _("Deleted."))
                return redirect("networks:vlans")
            if form.is_valid():
                services.update_vlan(request, actor, vlan, **form.cleaned_data)
                messages.success(request, _("Saved."))
                return redirect("networks:vlan", pk)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("networks:vlan", pk)
    return render(
        request,
        "networks/vlan.html",
        {
            "vlan": vlan,
            "form": form,
            "access_ports": _visible_ports(
                actor,
                vlan.untagged_on.select_related("device").order_by("device__name", "order")[:500],
            ),
            "trunk_ports": _visible_ports(
                actor,
                Port.objects.filter(tagged__vlan=vlan)
                .select_related("device")
                .order_by("device__name")[:500],
            ),
            "wlans": vlan.wlans.all(),
            "can_change": policy.has_perm(actor, "networks.vlan.change", vlan),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def wlan_list(request: HttpRequest) -> HttpResponse:
    """WLANs of the current tenant."""
    tenant = _tenant(request)
    form = WLANForm(request.POST or None, tenant=tenant)
    if request.method == "POST" and form.is_valid():
        try:
            wlan = services.create_wlan(request, _actor(request), tenant, **form.cleaned_data)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("networks:wlan", wlan.pk)
    if not policy.has_perm(request.user, "networks.wlan.view", tenant):
        raise PermissionDenied
    wlans = (
        WLAN.objects.filter(tenant=tenant)
        .select_related("vlan")
        .annotate(radio_count=Count("broadcasts"))
    )
    return render(
        request,
        "networks/wlans.html",
        {
            "wlans": wlans,
            "form": form,
            "can_add": policy.has_perm(request.user, "networks.wlan.add", tenant),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def wlan_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit a WLAN and the radios broadcasting it."""
    wlan = get_object_or_404(WLAN.objects.select_related("vlan"), pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "networks.wlan.view", wlan):
        raise Http404
    action = request.POST.get("action")
    form = WLANForm(request.POST if action == "edit" else None, instance=wlan, tenant=wlan.tenant)
    radio_form = BroadcastForm(
        request.POST if action == "radio" else None, tenant=wlan.tenant, user=actor
    )
    if request.method == "POST":
        try:
            if action == "edit" and form.is_valid():
                services.update_wlan(request, actor, wlan, **form.cleaned_data)
            elif action == "radio" and radio_form.is_valid():
                services.set_broadcast(
                    request, actor, wlan, radio_form.cleaned_data["port"], on=True
                )
            elif action == "remove_radio":
                port = get_object_or_404(Port, pk=parse_uuid(request.POST.get("port")))
                services.set_broadcast(request, actor, wlan, port, on=False)
            elif action == "delete":
                services.delete_wlan(request, actor, wlan)
                return redirect("networks:wlans")
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            messages.error(request, str(exc))
        return redirect("networks:wlan", pk)
    return render(
        request,
        "networks/wlan.html",
        {
            "wlan": wlan,
            "form": form,
            "radio_form": radio_form,
            "radios": _visible_ports(
                actor, wlan.radios.select_related("device").order_by("device__name")
            ),
            "can_change": policy.has_perm(actor, "networks.wlan.change", wlan),
        },
    )
