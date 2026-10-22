"""Take a platform's inventory into the documentation, and the page that starts it."""

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
from patchmanager.networks.models import VLAN


@dataclass
class Result:
    """What an import did."""

    cluster: Cluster | None = None
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
    request: HttpRequest,
    actor: User,
    tenant: Any,
    data: sources.Inventory,
    *,
    nas: Device | None = None,
) -> Result:
    """Create or update the cluster, its hosts, guests and storage (nothing is deleted)."""
    result = Result(notes=list(data.notes))
    with transaction.atomic():
        cluster = None
        if data.platform in ("proxmox", "vmware"):
            cluster = _cluster(actor, tenant, data, result)
            _hosts(actor, tenant, cluster, data, result)
            _guests(actor, tenant, cluster, data, result)
        for item in data.storage:
            _storage(actor, tenant, cluster, nas, item, result)
        result.cluster = cluster
    audit.record(
        "compute.inventory.imported",
        request=request,
        obj=cluster or nas,
        tenant_id=tenant.pk,
        data={
            "platform": data.platform,
            "created": result.created,
            "updated": result.updated,
            "storage": result.storage,
        },
    )
    return result


def _cluster(actor: User, tenant: Any, data: sources.Inventory, result: Result) -> Cluster:
    name = data.cluster or data.platform
    cluster = Cluster.objects.filter(tenant=tenant, name__iexact=name).first()
    if cluster is None:
        _need(actor, "compute.cluster.add", tenant)
        cluster = Cluster(tenant=tenant, created_by=actor, name=name[:100], kind=data.platform)
        cluster.full_clean(exclude=["tenant"])
        cluster.save()
    else:
        _need(actor, "compute.cluster.change", tenant, cluster)
    return cluster


def _hosts(
    actor: User, tenant: Any, cluster: Cluster, data: sources.Inventory, result: Result
) -> None:
    devices = {d.name.lower(): d for d in Device.objects.filter(tenant=tenant, name__in=data.hosts)}
    for name in data.hosts:
        device = (
            devices.get(name.lower())
            or Device.objects.filter(tenant=tenant, name__iexact=name).first()
        )
        if device is None or not policy.has_perm(actor, "dcim.device.view", device):
            result.notes.append(_("Host %(name)s is not documented as a device.") % {"name": name})
            continue
        member = ClusterHost.objects.filter(device=device).first()
        if member is None:
            ClusterHost.objects.create(tenant=tenant, cluster=cluster, device=device)
        elif member.cluster_id != cluster.pk:
            result.notes.append(_("Host %(name)s belongs to another cluster.") % {"name": name})


def _guests(
    actor: User, tenant: Any, cluster: Cluster, data: sources.Inventory, result: Result
) -> None:
    hosts = {
        d.name.lower(): d
        for d in Device.objects.filter(tenant=tenant, cluster_host__cluster=cluster)
    }
    vlans = {v.vid: v for v in VLAN.objects.filter(tenant=tenant)}
    for guest in data.guests:
        vm = VirtualMachine.objects.filter(
            tenant=tenant, cluster=cluster, external_id=guest.external_id
        ).first()
        vm = (
            vm
            or VirtualMachine.objects.filter(
                tenant=tenant, cluster=cluster, name__iexact=guest.name
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
        VMInterface.objects.filter(vm=vm).delete()
        for nic in guest.nics:
            vid = int(nic["vlan"]) if nic.get("vlan", "").isdigit() else None
            interface = VMInterface(
                tenant=tenant,
                vm=vm,
                name=nic["name"],
                mac_address=nic["mac"],
                bridge=nic["bridge"],
                addresses=nic["addresses"][:300],
                vlan=vlans.get(vid) if vid else None,
            )
            try:
                interface.full_clean(exclude=["tenant"])
                interface.save()
            except ValidationError:
                result.notes.append(
                    _("%(vm)s: network card %(nic)s was left out.")
                    % {"vm": vm.name, "nic": nic["name"]}
                )
        VirtualDisk.objects.filter(vm=vm).delete()
        for name, size, storage in guest.disks:
            volume = StorageVolume.objects.filter(
                tenant=tenant, cluster=cluster, name=storage
            ).first()
            VirtualDisk.objects.create(
                tenant=tenant, vm=vm, name=name, size_gb=size, storage=volume
            )


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
        label=gettext_lazy("User (vCenter, Synology)"), required=False, max_length=150
    )
    password = forms.CharField(
        label=gettext_lazy("Password (vCenter, Synology)"),
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
        label=gettext_lazy("NAS device (Synology)"), queryset=Device.objects.none(), required=False
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
        if platform in ("vmware", "synology") and not (
            data.get("username") and data.get("password")
        ):
            raise forms.ValidationError(_("Enter user and password."))
        if platform == "synology" and data.get("nas") is None:
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
    if not (
        policy.has_perm(actor, "compute.vm.add", scope_path=path_label(tenant.pk))
        or policy.has_perm(actor, "compute.storage.add", scope_path=path_label(tenant.pk))
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
                found = sources.synology(
                    data["url"], data["username"], data["password"], data["fingerprint"]
                )
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
                " vCenter, or volumes and shares from a Synology NAS. Credentials are used once"
                " and not stored; give an account with read rights only."
            ),
            "form": form,
            "back_url": reverse("objects:index", args=["vms"]),
            "submit": _("Import"),
        },
    )
