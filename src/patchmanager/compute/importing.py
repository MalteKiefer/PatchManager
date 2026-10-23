"""Take a platform's inventory into the documentation, and the page that starts it."""

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.compute import inventory as sources
from patchmanager.compute.models import (
    Cluster,
    ClusterHost,
    StorageVolume,
    VirtualDisk,
    VirtualMachine,
    VMInterface,
)
from patchmanager.core.fields import path_label
from patchmanager.dcim.models import Device
from patchmanager.ipam import services as ipam
from patchmanager.ipam.models import IPAddress
from patchmanager.networks.models import VLAN


@dataclass
class Pick:
    """What of an inventory to take over (None: everything) and how to create servers."""

    nodes: set[str] | None = None
    guests: set[str] | None = None
    storage: set[str] | None = None
    room: Any = None  # room and device type for nodes not documented as devices yet
    device_type: Any = None


@dataclass
class Result:
    """What an import did."""

    cluster: Cluster | None = None
    servers: int = 0
    created: int = 0
    updated: int = 0
    storage: int = 0
    notes: list[str] = field(default_factory=list)


def _need(actor: User, permission: str, tenant: Any, obj: Any = None) -> None:
    allowed = (
        policy.has_perm(actor, permission, obj)
        if obj is not None
        else policy.has_perm(actor, permission, scope_path=path_label(tenant.pk))
    )
    if not allowed:
        raise PermissionDeniedError(_("You do not have permission to do that."))


def save(
    request: HttpRequest | None,
    actor: User,
    tenant: Any,
    data: sources.Inventory,
    *,
    nas: Device | None = None,
    pick: Pick | None = None,
    cluster: Cluster | None = None,
) -> Result:
    """Create or update the cluster, its hosts, guests and storage (nothing is deleted).

    Guests are found by their ID on the platform within the cluster, so a machine that
    moved to another node is updated (its host changes) and never documented twice.
    """
    pick = pick or Pick()
    result = Result(notes=list(data.notes))
    # Importing is its own right, as for tables: machines from a cluster, storage from a NAS.
    kind = "vm" if data.platform in ("proxmox", "vmware") else "storage"
    _need(actor, f"compute.{kind}.import", tenant)
    with transaction.atomic():
        if data.platform in ("proxmox", "vmware"):
            cluster = cluster or _cluster(actor, tenant, data)
            _need(actor, "compute.cluster.change", tenant, cluster)
        for item in data.storage:  # first, so the disks of guests can point at it
            if pick.storage is None or item.name in pick.storage:
                _storage(actor, tenant, cluster, nas, item, result)
        if cluster is not None:
            _hosts(request, actor, tenant, cluster, data, result, pick)
            _guests(request, actor, tenant, cluster, data, result, pick)
        result.cluster = cluster
    audit.record(
        "compute.inventory.imported",
        actor=actor,
        request=request,
        obj=cluster or nas,
        tenant_id=tenant.pk,
        data={
            "platform": data.platform,
            "servers": result.servers,
            "created": result.created,
            "updated": result.updated,
            "storage": result.storage,
        },
    )
    return result


def _cluster(actor: User, tenant: Any, data: sources.Inventory) -> Cluster:
    name = data.cluster or data.platform
    cluster = Cluster.objects.filter(tenant=tenant, name__iexact=name).first()
    if cluster is None:
        _need(actor, "compute.cluster.add", tenant)
        cluster = Cluster(tenant=tenant, created_by=actor, name=name[:100], kind=data.platform)
        cluster.full_clean(exclude=["tenant"])
        cluster.save()
    return cluster


def _hosts(
    request: HttpRequest | None,
    actor: User,
    tenant: Any,
    cluster: Cluster,
    data: sources.Inventory,
    result: Result,
    pick: Pick,
) -> None:
    """Nodes become (or are matched to) server devices of the cluster."""
    from patchmanager.dcim import devices as dcim

    for name in data.hosts:
        if pick.nodes is not None and name not in pick.nodes:
            continue
        device = Device.objects.filter(tenant=tenant, name__iexact=name).first()
        if device is None and pick.room is not None and pick.device_type is not None:
            device = dcim.create(
                request, actor, device_type=pick.device_type, room=pick.room, name=name[:150]
            )
            result.servers += 1
        if device is None or not policy.has_perm(actor, "dcim.device.view", device):
            result.notes.append(_("Host %(name)s is not documented as a device.") % {"name": name})
            continue
        member = ClusterHost.objects.filter(device=device).first()
        if member is None:
            ClusterHost.objects.create(tenant=tenant, cluster=cluster, device=device)
        elif member.cluster_id != cluster.pk:
            result.notes.append(_("Host %(name)s belongs to another cluster.") % {"name": name})


def _guests(
    request: HttpRequest | None,
    actor: User,
    tenant: Any,
    cluster: Cluster,
    data: sources.Inventory,
    result: Result,
    pick: Pick,
) -> None:
    hosts = {
        d.name.lower(): d
        for d in Device.objects.filter(tenant=tenant, cluster_host__cluster=cluster)
    }
    vlans = _vlans_by_vid(tenant, cluster)
    for guest in data.guests:
        if pick.guests is not None and guest.external_id not in pick.guests:
            continue
        vm = VirtualMachine.objects.filter(
            tenant=tenant, cluster=cluster, external_id=guest.external_id
        ).first()
        vm = (
            vm
            or VirtualMachine.objects.filter(
                tenant=tenant, cluster=cluster, external_id="", name__iexact=guest.name
            ).first()
        )
        if vm is None:
            _need(actor, "compute.vm.add", tenant)
            vm = VirtualMachine(tenant=tenant, created_by=actor, cluster=cluster)
            result.created += 1
        else:
            _need(actor, "compute.vm.change", tenant, vm)
            result.updated += 1
        vm.name, vm.kind, vm.status = guest.name or guest.external_id, guest.kind, guest.status
        vm.external_id, vm.vcpus, vm.memory_mb = guest.external_id, guest.vcpus, guest.memory_mb
        vm.disk_gb = guest.disk_gb
        vm.operating_system = guest.operating_system or vm.operating_system
        vm.host = hosts.get(guest.host.lower())
        try:
            vm.full_clean(exclude=["tenant"])
        except ValidationError as exc:
            result.notes.append(f"{guest.name}: {'; '.join(exc.messages)}")
            continue
        vm.save()
        # Cards are updated in place (by name), so addresses linked to them stay linked.
        kept = []
        for nic in guest.nics:
            name = nic["name"] or ""
            interface = VMInterface.objects.filter(vm=vm, name=name).first() or VMInterface(
                tenant=tenant, vm=vm, name=name
            )
            interface.mac_address, interface.bridge = nic["mac"] or "", nic["bridge"] or ""
            # Some platforms never report addresses or VLANs (None: Proxmox VMs, vCenter);
            # what was documented by hand stays unless the platform says otherwise.
            if nic.get("addresses") is not None:
                interface.addresses = (nic["addresses"] or "")[:300]
            if nic.get("vlan") is not None:
                tag = nic["vlan"] or ""
                vid = int(tag) if tag.isdecimal() else None
                if vid is None:
                    interface.vlan = None
                elif vid in vlans or interface.vlan is None or interface.vlan.vid != vid:
                    # A tag no VLAN can be found for keeps a VLAN with that ID set by hand.
                    interface.vlan = vlans.get(vid)
            try:
                interface.full_clean(exclude=["tenant"])
                interface.save()
            except ValidationError:
                result.notes.append(
                    _("%(vm)s: network card %(nic)s was left out.")
                    % {"vm": vm.name, "nic": nic["name"]}
                )
                continue
            kept.append(interface.pk)
            _link_addresses(request, actor, tenant, interface, result)
        VMInterface.objects.filter(vm=vm).exclude(pk__in=kept).delete()
        VirtualDisk.objects.filter(vm=vm).delete()
        for name, size, storage in guest.disks:
            volume = StorageVolume.objects.filter(
                tenant=tenant, cluster=cluster, name=storage
            ).first()
            disk = VirtualDisk(tenant=tenant, vm=vm, name=name[:64], size_gb=size, storage=volume)
            try:
                disk.full_clean(exclude=["tenant"])
            except ValidationError:
                result.notes.append(
                    _("%(vm)s: disk %(disk)s was left out.") % {"vm": vm.name, "disk": name}
                )
                continue
            disk.save()


def _vlans_by_vid(tenant: Any, cluster: Cluster) -> dict[int, VLAN]:
    """VLANs a tag on this cluster can mean: of the cluster's site, else tenant wide.

    VLAN IDs repeat between sites; a tag that matches only VLANs of other sites is left
    unlinked rather than guessed.
    """
    sites = {cluster.site_id} if cluster.site_id else set()
    sites |= set(
        Device.objects.filter(cluster_host__cluster=cluster).values_list(
            "room__floor__building__site_id", flat=True
        )
    )
    found: dict[int, VLAN] = {}
    by_vid: dict[int, list[VLAN]] = {}
    for vlan in VLAN.objects.filter(tenant=tenant).order_by("site_id"):
        by_vid.setdefault(vlan.vid, []).append(vlan)
        own = vlan.site_id is not None and vlan.site_id in sites
        # A site's own VLAN wins over a tenant wide one with the same ID.
        if own or (vlan.site_id is None and vlan.vid not in found):
            found[vlan.vid] = vlan
    if not sites:  # the cluster's site is unknown: an ID used once in the tenant is clear
        for vid, same in by_vid.items():
            if vid not in found and len(same) == 1:
                found[vid] = same[0]
    return found


def _reported(text: str) -> dict[str, str]:
    """Host address -> address with prefix length, from the card's address list."""
    found: dict[str, str] = {}
    for token in text.replace(";", ",").replace(" ", ",").split(","):
        try:
            iface = ipaddress.ip_interface(token.strip())
        except ValueError:
            continue
        if not (iface.ip.is_loopback or iface.ip.is_link_local or iface.ip.is_unspecified):
            found[str(iface.ip)] = str(iface)
    return found


def _link_addresses(
    request: HttpRequest | None, actor: User, tenant: Any, interface: VMInterface, result: Result
) -> None:
    """Link the card's addresses to IPAM (global VRF): create missing ones, never take one away.

    An address already on a port or another machine stays there and is reported.
    """
    reported = _reported(interface.addresses)
    for gone in IPAddress.objects.filter(vm_interface=interface).exclude(host__in=reported):
        try:
            ipam.update_address(request, actor, gone, vm_interface=None)
        except (ServiceError, PermissionDeniedError) as exc:
            result.notes.append(
                _("%(address)s is no longer on %(vm)s but stays linked: %(error)s")
                % {"address": gone.host, "vm": interface.vm.name, "error": exc}
            )
    if not reported:
        return
    existing = {
        a.host: a for a in IPAddress.objects.filter(tenant=tenant, vrf=None, host__in=reported)
    }
    for host, with_length in reported.items():
        address = existing.get(host)
        try:
            if address is None:
                ipam.create_address(
                    request,
                    actor,
                    tenant,
                    address=with_length,
                    vm_interface=interface,
                    description=str(interface.vm)[:200],
                )
            elif address.vm_interface_id == interface.pk:
                continue
            elif address.port_id or address.vm_interface_id:
                result.notes.append(
                    _("%(address)s of %(vm)s is already used elsewhere and was not linked.")
                    % {"address": host, "vm": interface.vm.name}
                )
            else:
                ipam.update_address(request, actor, address, vm_interface=interface)
        except (ServiceError, PermissionDeniedError) as exc:
            result.notes.append(f"{interface.vm.name} {host}: {exc}")


def _storage(
    actor: User,
    tenant: Any,
    cluster: Cluster | None,
    nas: Device | None,
    item: sources.Storage,
    result: Result,
) -> None:
    owner = {"cluster": cluster} if cluster is not None else {"device": nas}
    volume = StorageVolume.objects.filter(tenant=tenant, name=item.name, **owner).first()
    if volume is None:
        _need(actor, "compute.storage.add", tenant)
        volume = StorageVolume(tenant=tenant, created_by=actor, name=item.name, **owner)
    else:
        _need(actor, "compute.storage.change", tenant, volume)
    volume.kind, volume.size_gb, volume.used_gb = item.kind, item.size_gb, item.used_gb
    volume.target, volume.raid = item.target or volume.target, item.raid or volume.raid
    volume.full_clean(exclude=["tenant"])
    volume.save()
    result.storage += 1


# --- Page ------------------------------------------------------------------------------


class ImportForm(forms.Form):
    """Where to read from and how to sign in (nothing of it is stored)."""

    platform = forms.ChoiceField(
        label=gettext_lazy("Platform"),
        choices=[
            ("proxmox", "Proxmox VE"),
            ("vmware", "VMware vCenter"),
            ("synology", "Synology DSM"),
            ("qnap", "QNAP QTS / QuTS hero"),
        ],
    )
    url = forms.URLField(
        label=gettext_lazy("Address"),
        help_text=gettext_lazy(
            "For example https://pve.example.lan:8006 or https://vcenter.example.lan."
        ),
    )
    token = forms.CharField(
        label=gettext_lazy("API token (Proxmox)"),
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
        help_text=gettext_lazy("user@realm!tokenid=secret, with read rights (PVEAuditor)."),
    )
    username = forms.CharField(
        label=gettext_lazy("User (vCenter, NAS)"), required=False, max_length=150
    )
    password = forms.CharField(
        label=gettext_lazy("Password (vCenter, NAS)"),
        required=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
    )
    fingerprint = forms.CharField(
        label=gettext_lazy("Certificate fingerprint (SHA-256)"),
        required=False,
        max_length=95,
        help_text=gettext_lazy(
            "Only for self signed certificates; otherwise the certificate is verified."
        ),
    )
    nas = forms.ModelChoiceField(
        label=gettext_lazy("NAS device (Synology, QNAP)"),
        queryset=Device.objects.none(),
        required=False,
    )

    def __init__(self, *args: Any, tenant: Any, actor: Any, **kwargs: Any) -> None:
        """NAS devices of the tenant the user may see."""
        super().__init__(*args, **kwargs)
        field: Any = self.fields["nas"]
        field.queryset = policy.scope_queryset(
            actor, "dcim.device.view", Device.objects.filter(tenant=tenant)
        )

    def clean(self) -> dict[str, Any]:
        """The sign in each platform needs."""
        data = super().clean() or {}
        platform = data.get("platform")
        if platform == "proxmox" and not data.get("token"):
            raise forms.ValidationError(_("Enter the API token."))
        if platform in ("vmware", "synology", "qnap") and not (
            data.get("username") and data.get("password")
        ):
            raise forms.ValidationError(_("Enter user and password."))
        if platform in ("synology", "qnap") and data.get("nas") is None:
            raise forms.ValidationError(_("Choose the NAS device the storage belongs to."))
        return data


@login_required
@require_http_methods(["GET", "POST"])
def import_view(request: HttpRequest) -> HttpResponse:
    """Read clusters, guests and storage from a platform."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    actor: Any = request.user
    if not any(
        policy.has_perm(actor, f"compute.{kind}.add", scope_path=path_label(tenant.pk))
        and policy.has_perm(actor, f"compute.{kind}.import", scope_path=path_label(tenant.pk))
        for kind in ("vm", "storage")
    ):
        raise PermissionDenied
    form = ImportForm(request.POST or None, tenant=tenant, actor=actor)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            if data["platform"] == "proxmox":
                found = sources.proxmox(data["url"], data["token"], data["fingerprint"])
            elif data["platform"] == "vmware":
                found = sources.vmware(
                    data["url"], data["username"], data["password"], data["fingerprint"]
                )
            else:
                read = sources.synology if data["platform"] == "synology" else sources.qnap
                found = read(data["url"], data["username"], data["password"], data["fingerprint"])
            result = save(request, actor, tenant, found, nas=data.get("nas"))
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except (ServiceError, ValidationError) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(
                request,
                _("%(created)s new and %(updated)s updated machines, %(storage)s storage entries.")
                % {"created": result.created, "updated": result.updated, "storage": result.storage},
            )
            for note in result.notes[:10]:
                messages.warning(request, note)
            if result.cluster is not None:
                return redirect("objects:detail", "clusters", result.cluster.pk)
            return redirect("objects:index", "storage")
    return render(
        request,
        "core/create.html",
        {
            "title": _("Import from a platform"),
            "intro": _(
                "Reads clusters, hosts, virtual machines and storage from Proxmox VE and VMware"
                " vCenter, or volumes and shares from a Synology or QNAP NAS. Credentials are used"
                " once and not stored; give an account with read rights only."
            ),
            "form": form,
            "back_url": reverse("objects:index", args=["vms"]),
            "submit": _("Import"),
        },
    )
