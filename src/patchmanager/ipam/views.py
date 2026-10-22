"""IPAM pages: prefix tree, prefix detail with free space, addresses and VRFs."""

import ipaddress
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.core.fields import path_label
from patchmanager.core.utils import parse_uuid
from patchmanager.dcim.models import Port
from patchmanager.ipam import services
from patchmanager.ipam.models import VRF, IPAddress, Prefix
from patchmanager.locations.models import Site
from patchmanager.networks.models import VLAN

PREFIX_FIELDS = ["prefix", "vrf", "status", "site", "vlan", "gateway", "description"]
ADDRESS_FIELDS = ["address", "vrf", "status", "dns_name", "description"]
VRF_FIELDS = ["name", "rd", "description"]


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _tenant(request: HttpRequest) -> Any:
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    return tenant


def _visible(user: Any, permission: str, queryset: Any) -> Any:
    return policy.scope_queryset(user, permission, queryset)


class _TenantForm(forms.ModelForm):  # type: ignore[type-arg]
    """Choices limited to the tenant."""

    def __init__(self, *args: Any, tenant: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if "vrf" in self.fields:
            self.fields["vrf"].queryset = VRF.objects.filter(tenant=tenant)  # type: ignore[attr-defined]
            self.fields["vrf"].empty_label = _("global")  # type: ignore[attr-defined]
        if "site" in self.fields:
            self.fields["site"].queryset = Site.objects.filter(tenant=tenant)  # type: ignore[attr-defined]
        if "vlan" in self.fields:
            self.fields["vlan"].queryset = VLAN.objects.filter(tenant=tenant)  # type: ignore[attr-defined]


class PrefixForm(_TenantForm):
    """Prefix fields."""

    class Meta:
        model = Prefix
        fields = PREFIX_FIELDS


class AddressForm(_TenantForm):
    """Address fields (the port is set from the port's page)."""

    class Meta:
        model = IPAddress
        fields = ADDRESS_FIELDS


class VRFForm(forms.ModelForm):  # type: ignore[type-arg]
    """VRF fields."""

    class Meta:
        model = VRF
        fields = VRF_FIELDS


def _tree(prefixes: list[Prefix]) -> list[tuple[Prefix, int]]:
    """Prefixes in address order with their nesting depth (per VRF)."""
    rows: list[tuple[Prefix, int]] = []
    stack: list[Any] = []
    for prefix in sorted(
        prefixes, key=lambda p: (str(p.vrf_id or ""), p.network.version, p.network)
    ):
        net = prefix.network
        while stack and not (
            stack[-1][0] == prefix.vrf_id
            and stack[-1][1].version == net.version
            and net.subnet_of(stack[-1][1])
        ):
            stack.pop()
        rows.append((prefix, len(stack)))
        stack.append((prefix.vrf_id, net))
    return rows


def _with_ports(actor: Any, addresses: Any) -> list[IPAddress]:
    """Attach ``shown_port``: the port, unless its device is hidden from the viewer."""
    rows = list(addresses)
    devices = {a.port.device_id for a in rows if a.port_id}
    from patchmanager.dcim.models import Device

    visible = set(
        _visible(actor, "dcim.device.view", Device.objects.filter(pk__in=devices)).values_list(
            "pk", flat=True
        )
    )
    for row in rows:
        row.shown_port = row.port if row.port_id and row.port.device_id in visible else None
    return rows


def _usage(prefix: Prefix, prefixes: list[Prefix], hosts: list[tuple[Any, str]]) -> Any:
    # ponytail: scans every prefix and address per row; fine for thousands, push into SQL
    # (inet containment with GROUP BY) if tenants reach hundreds of thousands of addresses.
    net = prefix.network
    kids = [
        p
        for p in prefixes
        if p is not prefix
        and p.vrf_id == prefix.vrf_id
        and p.network.version == net.version
        and p.network.subnet_of(net)
    ]
    direct = [
        p for p in kids if not any(p is not o and p.network.subnet_of(o.network) for o in kids)
    ]
    taken = [
        a.split("/")[0]
        for vrf, a in hosts
        if vrf == prefix.vrf_id and ipaddress.ip_interface(a).ip in net
    ]
    return services.usage(prefix, direct, taken)


@login_required
def prefix_list(request: HttpRequest) -> HttpResponse:
    """All prefixes of the tenant as a tree, with usage."""
    tenant = _tenant(request)
    actor = _actor(request)
    prefixes = list(
        _visible(actor, "ipam.prefix.view", Prefix.objects.filter(tenant=tenant)).select_related(
            "vrf", "site", "vlan"
        )
    )
    hosts = list(
        _visible(actor, "ipam.ipaddress.view", IPAddress.objects.filter(tenant=tenant)).values_list(
            "vrf_id", "address"
        )
    )
    rows = [
        {"prefix": prefix, "depth": depth, "usage": _usage(prefix, prefixes, hosts)}
        for prefix, depth in _tree(prefixes)
    ]
    return render(
        request,
        "ipam/prefixes.html",
        {
            "rows": rows,
            "can_add": policy.has_perm(actor, "ipam.prefix.add", scope_path=path_label(tenant.pk)),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def prefix_create(request: HttpRequest) -> HttpResponse:
    """Page that creates a prefix."""
    tenant = _tenant(request)
    initial = {"prefix": request.GET.get("prefix", ""), "vrf": parse_uuid(request.GET.get("vrf"))}
    form = PrefixForm(request.POST or None, tenant=tenant, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            prefix = services.create_prefix(request, _actor(request), tenant, **form.cleaned_data)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("ipam:prefix", prefix.pk)
    return render(
        request,
        "core/create.html",
        {"title": _("New prefix"), "form": form, "back_url": reverse("ipam:prefixes")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def prefix_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """A prefix with its child prefixes, addresses and free space."""
    prefix = get_object_or_404(Prefix.objects.select_related("vrf", "site", "vlan"), pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "ipam.prefix.view", prefix):
        raise Http404
    form = PrefixForm(request.POST or None, instance=prefix, tenant=prefix.tenant)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "delete":
                services.delete_prefix(request, actor, prefix)
                messages.success(request, _("Deleted."))
                return redirect("ipam:prefixes")
            if form.is_valid():
                services.update_prefix(
                    request, actor, prefix, **{k: form.cleaned_data[k] for k in PREFIX_FIELDS}
                )
                messages.success(request, _("Saved."))
                return redirect("ipam:prefix", pk)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("ipam:prefix", pk)
    visible_prefixes = _visible(actor, "ipam.prefix.view", Prefix.objects.all())
    kids = services.children(prefix, visible_prefixes)
    addresses = services.addresses(
        prefix, _visible(actor, "ipam.ipaddress.view", IPAddress.objects.all())
    ).select_related("port__device")
    # Free space counts every address of the tenant, also those the viewer may not see.
    taken = list(
        IPAddress.objects.filter(
            tenant_id=prefix.tenant_id,
            vrf=prefix.vrf,
            address__net_within_or_equal=prefix.prefix,
        ).values_list("host", flat=True)
    )
    container = prefix.status == Prefix.Status.CONTAINER
    return render(
        request,
        "ipam/prefix.html",
        {
            "prefix": prefix,
            "form": form,
            "children": kids,
            "addresses": _with_ports(actor, addresses[:500]),
            "usage": services.usage(prefix, kids, taken),
            "free": [] if container else services.free_ranges(prefix, taken),
            "next_free": None if container else services.next_free(prefix, taken),
            "can_change": policy.has_perm(actor, "ipam.prefix.change", prefix),
            "can_add_address": policy.has_perm(
                actor, "ipam.ipaddress.add", scope_path=path_label(prefix.tenant_id)
            ),
        },
    )


@login_required
def address_list(request: HttpRequest) -> HttpResponse:
    """Addresses of the tenant, searchable."""
    tenant = _tenant(request)
    actor = _actor(request)
    rows = _visible(
        actor, "ipam.ipaddress.view", IPAddress.objects.filter(tenant=tenant)
    ).select_related("vrf", "port__device")
    query = request.GET.get("q", "").strip()[:100]
    if query:
        rows = rows.filter(host__icontains=query) | rows.filter(dns_name__icontains=query)
    page = Paginator(rows.order_by("vrf__name", "address"), 100).get_page(request.GET.get("page"))
    page.object_list = _with_ports(actor, page.object_list)
    return render(
        request,
        "ipam/addresses.html",
        {
            "page": page,
            "q": query,
            "can_add": policy.has_perm(
                actor, "ipam.ipaddress.add", scope_path=path_label(tenant.pk)
            ),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def address_create(request: HttpRequest) -> HttpResponse:
    """Page that creates an address, optionally on a port (``?port=``)."""
    tenant = _tenant(request)
    params = request.POST if request.method == "POST" else request.GET
    port = None
    if params.get("port"):
        port = get_object_or_404(
            Port.objects.select_related("device"), pk=parse_uuid(params.get("port"))
        )
        if not policy.has_perm(request.user, "dcim.device.view", port.device):
            raise Http404
    initial = {"address": request.GET.get("address", ""), "vrf": parse_uuid(request.GET.get("vrf"))}
    form = AddressForm(request.POST or None, tenant=tenant, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            address = services.create_address(
                request, _actor(request), tenant, port=port, **form.cleaned_data
            )
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            if port is not None:
                return redirect("devices:port", port.pk)
            return redirect("ipam:address", address.pk)
    title = (
        _("New address on %(port)s") % {"port": f"{port.device} {port.name}"}
        if port
        else _("New address")
    )
    return render(
        request,
        "core/create.html",
        {
            "title": title,
            "form": form,
            "back_url": reverse("devices:port", args=[port.pk])
            if port
            else reverse("ipam:addresses"),
            "hidden": {"port": port.pk} if port else {},
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def address_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit an address, take it off its port, or delete it."""
    address = get_object_or_404(IPAddress.objects.select_related("vrf", "port__device"), pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "ipam.ipaddress.view", address):
        raise Http404
    form = AddressForm(request.POST or None, instance=address, tenant=address.tenant)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "delete":
                services.delete_address(request, actor, address)
                messages.success(request, _("Deleted."))
                return redirect("ipam:addresses")
            if action == "unassign":
                services.update_address(request, actor, address, port=None)
                messages.success(request, _("The address is no longer on a port."))
                return redirect("ipam:address", pk)
            if form.is_valid():
                services.update_address(
                    request, actor, address, **{k: form.cleaned_data[k] for k in ADDRESS_FIELDS}
                )
                messages.success(request, _("Saved."))
                return redirect("ipam:address", pk)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("ipam:address", pk)
    containing = (
        _visible(actor, "ipam.prefix.view", Prefix.objects.filter(tenant_id=address.tenant_id))
        .filter(vrf=address.vrf, prefix__net_contains_or_equal=address.address.split("/")[0])
        .order_by("-prefix")
    )
    port_visible = address.port is not None and policy.has_perm(
        actor, "dcim.device.view", address.port.device
    )
    return render(
        request,
        "ipam/address.html",
        {
            "address": address,
            "form": form,
            "prefixes": containing,
            "port": address.port if port_visible else None,
            "can_change": policy.has_perm(actor, "ipam.ipaddress.change", address),
        },
    )


@login_required
def vrf_list(request: HttpRequest) -> HttpResponse:
    """VRFs of the tenant."""
    tenant = _tenant(request)
    actor = _actor(request)
    return render(
        request,
        "ipam/vrfs.html",
        {
            "vrfs": _visible(actor, "ipam.vrf.view", VRF.objects.filter(tenant=tenant)),
            "can_add": policy.has_perm(actor, "ipam.vrf.add", scope_path=path_label(tenant.pk)),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def vrf_create(request: HttpRequest) -> HttpResponse:
    """Page that creates a VRF."""
    tenant = _tenant(request)
    form = VRFForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.create_vrf(request, _actor(request), tenant, **form.cleaned_data)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("ipam:vrfs")
    return render(
        request,
        "core/create.html",
        {"title": _("New VRF"), "form": form, "back_url": reverse("ipam:vrfs")},
    )


@login_required
@require_http_methods(["GET", "POST"])
def vrf_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """Edit or delete a VRF."""
    vrf = get_object_or_404(VRF, pk=pk)
    actor = _actor(request)
    if not policy.has_perm(actor, "ipam.vrf.view", vrf):
        raise Http404
    form = VRFForm(request.POST or None, instance=vrf)
    if request.method == "POST":
        try:
            if request.POST.get("action") == "delete":
                services.delete_vrf(request, actor, vrf)
                messages.success(request, _("Deleted."))
                return redirect("ipam:vrfs")
            if form.is_valid():
                services.update_vrf(
                    request, actor, vrf, **{k: form.cleaned_data[k] for k in VRF_FIELDS}
                )
                messages.success(request, _("Saved."))
                return redirect("ipam:vrf", pk)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except ServiceError as exc:
            messages.error(request, str(exc))
            return redirect("ipam:vrf", pk)
    return render(
        request,
        "ipam/vrf.html",
        {
            "vrf": vrf,
            "form": form,
            "prefix_count": vrf.prefixes.count(),
            "address_count": vrf.addresses.count(),
            "can_change": policy.has_perm(actor, "ipam.vrf.change", vrf),
        },
    )
