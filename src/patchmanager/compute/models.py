"""Virtualisation and storage: clusters of hosts, virtual machines and storage volumes."""

from django.core.validators import MaxValueValidator, RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import path_label
from patchmanager.tenancy.models import TenantScopedModel


class TenantWide(TenantScopedModel):
    """A record governed by its tenant."""

    class Meta:
        abstract = True

    @property
    def policy_path(self) -> str:
        """Tenant wide."""
        return path_label(self.tenant_id)


class Cluster(TenantWide):
    """Hosts that run virtual machines together (Proxmox, vSphere, Hyper-V ...)."""

    class Kind(models.TextChoices):
        PROXMOX = "proxmox", "Proxmox VE"
        VMWARE = "vmware", "VMware vSphere"
        HYPERV = "hyperv", "Microsoft Hyper-V"
        KVM = "kvm", "KVM / libvirt"
        XCPNG = "xcpng", "XCP-ng / XenServer"
        NUTANIX = "nutanix", "Nutanix AHV"
        KUBERNETES = "kubernetes", "Kubernetes"
        OTHER = "other", _("other")

    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(
        _("platform"), max_length=10, choices=Kind.choices, default=Kind.PROXMOX
    )
    site = models.ForeignKey(
        "locations.Site",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name=_("site"),
    )
    management_url = models.URLField(_("management address"), blank=True)
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "compute.cluster"

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint("tenant", Lower("name"), name="cluster_name_unique")]

    def __str__(self) -> str:
        return self.name


class ClusterHost(TenantWide):
    """A physical server of a cluster."""

    cluster = models.ForeignKey(
        Cluster, on_delete=models.CASCADE, related_name="hosts", verbose_name=_("cluster")
    )
    device = models.OneToOneField(
        "dcim.Device",
        on_delete=models.CASCADE,
        related_name="cluster_host",
        verbose_name=_("server"),
    )

    policy_object_type = "compute.cluster"

    class Meta:
        ordering = ["cluster", "device__name"]

    def __str__(self) -> str:
        return str(self.device)


class StorageVolume(TenantWide):
    """Storage a NAS, SAN or cluster provides: pools, volumes, LUNs, shares, datastores."""

    class Kind(models.TextChoices):
        POOL = "pool", _("pool / RAID group")
        VOLUME = "volume", _("volume")
        LUN = "lun", _("LUN (iSCSI, Fibre Channel)")
        NFS = "nfs", _("NFS share")
        SMB = "smb", _("SMB share")
        DATASTORE = "datastore", _("datastore / storage of a cluster")
        BUCKET = "bucket", _("object storage bucket")
        OTHER = "other", _("other")

    name = models.CharField(_("name"), max_length=150)
    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices, default=Kind.VOLUME)
    device = models.ForeignKey(
        "dcim.Device",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="storage_volumes",
        verbose_name=_("storage system"),
        help_text=_("The NAS or SAN that provides it."),
    )
    cluster = models.ForeignKey(
        Cluster,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="storage",
        verbose_name=_("cluster"),
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        verbose_name=_("part of"),
    )
    size_gb = models.PositiveBigIntegerField(_("size (GB)"), null=True, blank=True)
    used_gb = models.PositiveBigIntegerField(_("used (GB)"), null=True, blank=True)
    raid = models.CharField(_("RAID level"), max_length=30, blank=True)
    target = models.CharField(
        _("path or target"),
        max_length=300,
        blank=True,
        help_text=_("For example an export path, share name or iSCSI IQN."),
    )
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "compute.storage"

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class VirtualMachine(TenantWide):
    """A virtual machine or container."""

    class Status(models.TextChoices):
        RUNNING = "running", _("running")
        STOPPED = "stopped", _("stopped")
        PAUSED = "paused", _("paused")
        TEMPLATE = "template", _("template")
        PLANNED = "planned", _("planned")

    class Kind(models.TextChoices):
        VM = "vm", _("virtual machine")
        CONTAINER = "container", _("container (LXC, Docker ...)")

    name = models.CharField(_("name"), max_length=150)
    kind = models.CharField(_("kind"), max_length=10, choices=Kind.choices, default=Kind.VM)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.RUNNING
    )
    cluster = models.ForeignKey(
        Cluster,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="virtual_machines",
        verbose_name=_("cluster"),
    )
    host = models.ForeignKey(
        "dcim.Device",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="virtual_machines",
        verbose_name=_("host"),
    )
    external_id = models.CharField(
        _("ID on the platform"),
        max_length=100,
        blank=True,
        help_text=_("For example the Proxmox VM ID or the vSphere ID."),
    )
    operating_system = models.CharField(_("operating system"), max_length=150, blank=True)
    vcpus = models.PositiveSmallIntegerField(_("virtual CPUs"), null=True, blank=True)
    memory_mb = models.PositiveIntegerField(_("memory (MB)"), null=True, blank=True)
    disk_gb = models.PositiveIntegerField(_("disk (GB)"), null=True, blank=True)
    purpose = models.CharField(_("purpose"), max_length=200, blank=True)
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "compute.vm"

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                "tenant",
                "cluster",
                Lower("name"),
                nulls_distinct=False,
                name="vm_name_unique_per_cluster",
            )
        ]

    def __str__(self) -> str:
        return self.name


class VMInterface(TenantWide):
    """A network card of a virtual machine."""

    vm = models.ForeignKey(
        VirtualMachine,
        on_delete=models.CASCADE,
        related_name="interfaces",
        verbose_name=_("virtual machine"),
    )
    name = models.CharField(_("name"), max_length=64)
    mac_address = models.CharField(
        _("MAC address"),
        max_length=17,
        blank=True,
        validators=[
            RegexValidator(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", _("Use aa:bb:cc:dd:ee:ff."))
        ],
    )
    vlan = models.ForeignKey(
        "networks.VLAN",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name=_("VLAN"),
    )
    bridge = models.CharField(_("bridge or port group"), max_length=100, blank=True)
    addresses = models.CharField(
        _("addresses"),
        max_length=300,
        blank=True,
        help_text=_("Separated by commas, for example 10.0.0.5/24."),
    )
    enabled = models.BooleanField(_("enabled"), default=True)

    policy_object_type = "compute.vm"

    class Meta:
        ordering = ["vm", "name"]
        constraints = [models.UniqueConstraint("vm", "name", name="vminterface_name_unique")]

    def __str__(self) -> str:
        return f"{self.vm}: {self.name}"


class VirtualDisk(TenantWide):
    """A disk of a virtual machine and the storage it lives on."""

    vm = models.ForeignKey(
        VirtualMachine,
        on_delete=models.CASCADE,
        related_name="disks",
        verbose_name=_("virtual machine"),
    )
    name = models.CharField(_("name"), max_length=64)
    size_gb = models.PositiveIntegerField(
        _("size (GB)"), validators=[MaxValueValidator(10_000_000)]
    )
    storage = models.ForeignKey(
        StorageVolume,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="disks",
        verbose_name=_("storage"),
    )

    policy_object_type = "compute.vm"

    class Meta:
        ordering = ["vm", "name"]

    def __str__(self) -> str:
        return f"{self.vm}: {self.name}"


class Connection(TenantWide):
    """A platform PatchManager reads regularly (Proxmox VE), with what to take over.

    The API token is stored encrypted with the instance keys and only used by the
    synchronisation; it is never shown again.
    """

    class Interval(models.IntegerChoices):
        OFF = 0, _("only when started by hand")
        HOURLY = 1, _("every hour")
        SIX_HOURS = 6, _("every 6 hours")
        DAILY = 24, _("every day")

    name = models.CharField(_("name"), max_length=100)
    platform = models.CharField(_("platform"), max_length=10, default="proxmox", editable=False)
    url = models.URLField(_("address"))
    fingerprint = models.CharField(_("certificate fingerprint (SHA-256)"), max_length=95, blank=True)
    secret = models.TextField(editable=False)  # encrypted token
    cluster = models.ForeignKey(Cluster, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="connections", verbose_name=_("cluster"))
    # What to take over: names of nodes and storage, IDs of guests.
    nodes = models.JSONField(default=list, blank=True)
    guests = models.JSONField(default=list, blank=True)
    storage = models.JSONField(default=list, blank=True)
    add_new_guests = models.BooleanField(
        _("take over new machines automatically"), default=True,
        help_text=_("Machines created on the platform later are documented by the next update."),
    )
    node_room = models.ForeignKey("locations.Room", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+", verbose_name=_("room for new servers"))
    node_type = models.ForeignKey("library.DeviceType", null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+", verbose_name=_("device type for new servers"))
    interval = models.PositiveSmallIntegerField(_("automatic update"), choices=Interval.choices,
                                                default=Interval.DAILY)
    last_synced_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_result = models.TextField(blank=True, editable=False)
    last_error = models.CharField(max_length=300, blank=True, editable=False)

    policy_object_type = "compute.cluster"

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint("tenant", Lower("name"), name="connection_name_unique")]

    def __str__(self) -> str:
        return self.name
