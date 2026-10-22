"""Racks and the units they hold."""

from typing import Any

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import IntegerRangeField, RangeOperators
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.backends.postgresql.psycopg_any import NumericRange
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from patchmanager.core.hierarchy import HierarchyNode
from patchmanager.library.models import Connector, PortKind
from patchmanager.locations.models import Room
from patchmanager.tenancy.models import TenantScopedModel

MAX_RACK_UNITS = 100


class Rack(HierarchyNode):
    """A rack in a room."""

    class Width(models.TextChoices):
        IN19 = "19", '19"'
        IN21 = "21", '21"'
        IN23 = "23", '23"'

    class Status(models.TextChoices):
        PLANNED = "planned", _("planned")
        ACTIVE = "active", _("active")
        RETIRED = "retired", _("retired")

    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="racks")
    name = models.CharField(_("name"), max_length=150)
    u_height = models.PositiveSmallIntegerField(
        _("height (U)"),
        default=42,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_RACK_UNITS)],
    )
    width = models.CharField(_("width"), max_length=2, choices=Width.choices, default=Width.IN19)
    depth_mm = models.PositiveIntegerField(_("depth (mm)"), null=True, blank=True)
    descending_units = models.BooleanField(_("number units from the top"), default=False)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.ACTIVE
    )
    serial = models.CharField(_("serial number"), max_length=100, blank=True)
    asset_tag = models.CharField(_("asset tag"), max_length=100, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    plan_x = models.FloatField(null=True, blank=True)
    plan_y = models.FloatField(null=True, blank=True)
    plan_rotation = models.PositiveSmallIntegerField(
        default=0, choices=[(0, "0°"), (90, "90°"), (180, "180°"), (270, "270°")]
    )

    parent_field = "room"
    policy_object_type = "dcim.rack"
    condition_attrs = frozenset({"created_by", "name", "status"})

    class Meta(HierarchyNode.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint("room", Lower("name"), name="rack_name_unique_per_room"),
            models.CheckConstraint(
                condition=models.Q(u_height__gte=1, u_height__lte=MAX_RACK_UNITS),
                name="rack_height_range",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def occupied_units(self) -> dict[str, set[int]]:
        """Units taken on each face."""
        taken: dict[str, set[int]] = {"front": set(), "rear": set()}
        for space in self.spaces.all():
            units = set(range(space.start, space.end + 1))
            if space.face in ("front", "both"):
                taken["front"] |= units
            if space.face in ("rear", "both"):
                taken["rear"] |= units
        return taken

    def utilisation(self) -> dict[str, int]:
        """Used units per face and in percent of the rack height (busier face)."""
        taken = self.occupied_units()
        used = max(len(taken["front"]), len(taken["rear"]))
        return {
            "front": len(taken["front"]),
            "rear": len(taken["rear"]),
            "used": used,
            "free": self.u_height - used,
            "percent": round(100 * used / self.u_height),
        }


class RackSpace(TenantScopedModel):
    """Units of a rack taken by something, on the front, the rear or both faces.

    ``faces`` is ``[0,1)`` front, ``[1,2)`` rear or ``[0,2)`` both, so one
    exclusion constraint forbids every overlap. Devices (next release) take
    space the same way; until then spaces are reservations.
    """

    class Face(models.TextChoices):
        FRONT = "front", _("front")
        REAR = "rear", _("rear")
        BOTH = "both", _("front and rear")

    FACE_RANGES = {Face.FRONT: (0, 1), Face.REAR: (1, 2), Face.BOTH: (0, 2)}

    rack = models.ForeignKey(Rack, on_delete=models.CASCADE, related_name="spaces")
    device = models.OneToOneField(
        "Device", null=True, blank=True, on_delete=models.CASCADE, related_name="space"
    )
    face = models.CharField(_("face"), max_length=5, choices=Face.choices, default=Face.BOTH)
    faces = IntegerRangeField(editable=False)
    units = IntegerRangeField(editable=False)
    label = models.CharField(_("label"), max_length=150)
    note = models.TextField(_("note"), blank=True)

    policy_object_type = "dcim.rack"

    class Meta:
        ordering = ["rack", "units"]
        constraints = [
            ExclusionConstraint(
                name="rackspace_no_overlap",
                expressions=[
                    ("rack", RangeOperators.EQUAL),
                    ("faces", RangeOperators.OVERLAPS),
                    ("units", RangeOperators.OVERLAPS),
                ],
            ),
        ]

    def __str__(self) -> str:
        return self.label

    def place(self, start: int, size: int, face: str | None = None) -> None:
        """Set the unit range ``start .. start + size - 1`` and the face(s)."""
        if face is not None:
            self.face = face
        self.units = NumericRange(start, start + size)
        self.faces = NumericRange(*self.FACE_RANGES[self.Face(self.face)])

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Belong to the rack's tenant."""
        self.tenant_id = self.rack.tenant_id
        super().save(*args, **kwargs)

    @property
    def start(self) -> int:
        """Lowest unit (1 based)."""
        return int(self.units.lower)

    @property
    def size(self) -> int:
        """Number of units."""
        return int(self.units.upper - self.units.lower)

    @property
    def end(self) -> int:
        """Highest unit, inclusive."""
        return self.start + self.size - 1

    @property
    def policy_path(self) -> str:
        """Governed by its rack."""
        return str(self.rack.path)


class Device(HierarchyNode):
    """A device instance: in a rack (with a unit position) or loose in a room (floor device)."""

    class Status(models.TextChoices):
        PLANNED = "planned", _("planned")
        ACTIVE = "active", _("active")
        OFFLINE = "offline", _("offline")
        DECOMMISSIONED = "decommissioned", _("decommissioned")

    device_type = models.ForeignKey(
        "library.DeviceType",
        on_delete=models.PROTECT,
        related_name="devices",
        verbose_name=_("device type"),
    )
    name = models.CharField(_("name"), max_length=150)
    status = models.CharField(
        _("status"), max_length=15, choices=Status.choices, default=Status.ACTIVE
    )
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="devices")
    rack = models.ForeignKey(
        Rack, null=True, blank=True, on_delete=models.PROTECT, related_name="devices"
    )
    serial = models.CharField(_("serial number"), max_length=100, blank=True)
    asset_tag = models.CharField(_("asset tag"), max_length=100, blank=True)
    purchase_date = models.DateField(_("purchase date"), null=True, blank=True)
    in_service_date = models.DateField(_("in service since"), null=True, blank=True)
    responsible = models.CharField(_("responsible person"), max_length=150, blank=True)
    supplier = models.ForeignKey(
        "directory.Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="supplied_devices",
        verbose_name=_("supplier"),
    )
    warranty_end = models.DateField(_("warranty until"), null=True, blank=True)
    support_contract = models.CharField(_("support contract"), max_length=200, blank=True)
    support_end = models.DateField(_("support until"), null=True, blank=True)
    end_of_life = models.DateField(
        _("end of life"), null=True, blank=True, help_text=_("When the vendor stops updates.")
    )
    notes = models.TextField(_("notes"), blank=True)
    plan_x = models.FloatField(null=True, blank=True)
    plan_y = models.FloatField(null=True, blank=True)

    policy_object_type = "dcim.device"
    condition_attrs = frozenset({"created_by", "name", "status"})

    class Meta(HierarchyNode.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint("tenant", Lower("name"), name="device_name_unique_per_tenant")
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def parent(self) -> Any:
        """The rack if mounted, otherwise the room."""
        return self.rack if self.rack_id else self.room


class Port(TenantScopedModel):
    """One port of a device. Front ports of passive devices map to a rear port position."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="ports")
    name = models.CharField(_("name"), max_length=100)
    kind = models.CharField(
        _("kind"), max_length=10, choices=PortKind.choices, default=PortKind.INTERFACE
    )
    connector = models.CharField(
        _("connector"), max_length=10, choices=Connector.choices, default=Connector.RJ45
    )
    speed_mbps = models.PositiveIntegerField(_("speed (Mbit/s)"), null=True, blank=True)
    enabled = models.BooleanField(_("enabled"), default=True)
    label = models.CharField(_("label"), max_length=100, blank=True)
    description = models.TextField(_("description"), blank=True)
    mac_address = models.CharField(_("MAC address"), max_length=17, blank=True)
    positions = models.PositiveSmallIntegerField(_("positions"), default=1)
    rear_port = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="front_ports",
        verbose_name=_("rear port"),
    )
    rear_position = models.PositiveSmallIntegerField(_("rear position"), null=True, blank=True)
    vlan_mode = models.CharField(
        _("VLAN mode"),
        max_length=6,
        blank=True,
        choices=[("access", _("access")), ("trunk", _("trunk"))],
    )
    untagged_vlan = models.ForeignKey(
        "networks.VLAN",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="untagged_on",
        verbose_name=_("untagged / native VLAN"),
    )
    lag = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="lag_members",
        verbose_name=_("link aggregation"),
        limit_choices_to={"kind": "lag"},
    )
    lag_protocol = models.CharField(
        _("aggregation protocol"),
        max_length=6,
        blank=True,
        choices=[("lacp", "LACP"), ("static", _("static"))],
    )
    mtu = models.PositiveIntegerField(
        _("MTU"),
        null=True,
        blank=True,
        validators=[MinValueValidator(576), MaxValueValidator(65535)],
    )
    tunneled = models.BooleanField(
        _("tunnelled to a gateway"), default=False, help_text=_("Traffic goes to the gateway.")
    )
    stp_edge = models.BooleanField(_("edge port"), default=False)
    stp_bpdu_guard = models.BooleanField(_("BPDU guard"), default=False)
    stp_root_guard = models.BooleanField(_("root guard"), default=False)
    ospf_area = models.CharField(
        _("OSPF area"),
        max_length=15,
        blank=True,
        validators=[
            RegexValidator(
                r"^(\d{1,10}|\d{1,3}(\.\d{1,3}){3})$", _("Use a number or a dotted area ID.")
            )
        ],
    )
    ospf_passive = models.BooleanField(_("OSPF passive"), default=False)
    order = models.PositiveIntegerField(default=0)

    policy_object_type = "dcim.device"

    class Meta:
        ordering = ["device", "order", "name"]
        constraints = [
            models.UniqueConstraint("device", Lower("name"), name="port_name_unique_per_device"),
            models.UniqueConstraint("rear_port", "rear_position", name="port_rear_position_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.device}: {self.name}"

    @property
    def policy_path(self) -> str:
        """Governed by its device."""
        return str(self.device.path)

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Belong to the device's tenant."""
        self.tenant_id = self.device.tenant_id
        super().save(*args, **kwargs)
