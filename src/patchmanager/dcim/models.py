"""Racks and the units they hold."""

from typing import Any

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import IntegerRangeField, RangeOperators
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.backends.postgresql.psycopg_any import NumericRange
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from patchmanager.core.hierarchy import HierarchyNode
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
