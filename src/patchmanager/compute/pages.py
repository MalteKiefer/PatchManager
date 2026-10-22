"""Pages of clusters, virtual machines and storage (through the shared record pages)."""

from django import forms
from django.utils.translation import gettext_lazy as _

from patchmanager.compute.models import (
    Cluster,
    ClusterHost,
    StorageVolume,
    VirtualDisk,
    VirtualMachine,
    VMInterface,
)
from patchmanager.core import crud
from patchmanager.directory.pages import TenantForm


class ClusterForm(TenantForm):
    """A cluster."""

    tenant_fields = ("site",)

    class Meta:
        model = Cluster
        fields = ["name", "kind", "site", "management_url", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class HostForm(TenantForm):
    """A server of a cluster."""

    tenant_fields = ("device",)

    class Meta:
        model = ClusterHost
        fields = ["cluster", "device"]


class VMForm(TenantForm):
    """A virtual machine."""

    tenant_fields = ("cluster", "host")

    class Meta:
        model = VirtualMachine
        fields = [
            "name",
            "kind",
            "status",
            "cluster",
            "host",
            "external_id",
            "operating_system",
            "vcpus",
            "memory_mb",
            "disk_gb",
            "purpose",
            "description",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class InterfaceForm(TenantForm):
    """A network card of a virtual machine."""

    tenant_fields = ("vlan",)

    class Meta:
        model = VMInterface
        fields = ["vm", "name", "mac_address", "vlan", "bridge", "addresses", "enabled"]


class DiskForm(TenantForm):
    """A disk of a virtual machine."""

    tenant_fields = ("storage",)

    class Meta:
        model = VirtualDisk
        fields = ["vm", "name", "size_gb", "storage"]


class StorageForm(TenantForm):
    """A pool, volume, LUN, share or datastore."""

    tenant_fields = ("device", "cluster", "parent")

    class Meta:
        model = StorageVolume
        fields = [
            "name",
            "kind",
            "device",
            "cluster",
            "parent",
            "size_gb",
            "used_gb",
            "raid",
            "target",
            "description",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


for resource in (
    crud.Resource(
        key="clusters",
        model=Cluster,
        form=ClusterForm,
        title=_("Clusters"),
        new=_("New cluster"),
        group="compute",
        columns=((_("Name"), "name"), (_("Platform"), "kind"), (_("Site"), "site")),
        children=("cluster-hosts",),
        actions=((_("Connections"), "compute:connections"),),
    ),
    crud.Resource(
        key="cluster-hosts",
        model=ClusterHost,
        form=HostForm,
        title=_("Hosts"),
        new=_("Add host"),
        group="compute",
        columns=((_("Server"), "device"),),
        parent=("cluster", "clusters"),
        hidden=True,
        order=("device__name",),
    ),
    crud.Resource(
        key="vms",
        model=VirtualMachine,
        form=VMForm,
        title=_("Virtual machines"),
        new=_("New virtual machine"),
        group="compute",
        columns=(
            (_("Name"), "name"),
            (_("Status"), "status"),
            (_("Cluster"), "cluster"),
            (_("Host"), "host"),
            (_("Operating system"), "operating_system"),
            (_("CPUs"), "vcpus"),
            (_("Memory (MB)"), "memory_mb"),
        ),
        children=("vm-interfaces", "vm-disks"),
        actions=((_("Connections"), "compute:connections"), (_("Import once"), "compute:import")),
    ),
    crud.Resource(
        key="vm-interfaces",
        model=VMInterface,
        form=InterfaceForm,
        title=_("Network cards"),
        new=_("Add network card"),
        group="compute",
        columns=(
            (_("Name"), "name"),
            (_("MAC address"), "mac_address"),
            (_("VLAN"), "vlan"),
            (_("Bridge or port group"), "bridge"),
            (_("Addresses"), "addresses"),
        ),
        parent=("vm", "vms"),
        hidden=True,
    ),
    crud.Resource(
        key="vm-disks",
        model=VirtualDisk,
        form=DiskForm,
        title=_("Disks"),
        new=_("Add disk"),
        group="compute",
        columns=((_("Name"), "name"), (_("Size (GB)"), "size_gb"), (_("Storage"), "storage")),
        parent=("vm", "vms"),
        hidden=True,
    ),
    crud.Resource(
        key="storage",
        model=StorageVolume,
        form=StorageForm,
        title=_("Storage"),
        new=_("New storage"),
        group="compute",
        columns=(
            (_("Name"), "name"),
            (_("Kind"), "kind"),
            (_("Storage system"), "device"),
            (_("Size (GB)"), "size_gb"),
            (_("Used (GB)"), "used_gb"),
            (_("Path or target"), "target"),
        ),
        actions=((_("Import once"), "compute:import"),),
    ),
):
    crud.register(resource)
