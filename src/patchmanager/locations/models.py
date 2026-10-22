"""Sites, buildings, floors and rooms."""

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext, gettext_noop
from django.utils.translation import gettext_lazy as _

from patchmanager.core.hierarchy import HierarchyNode
from patchmanager.tenancy.models import TenantScopedModel


class Site(HierarchyNode):
    """A location with an address, for example a campus or data centre."""

    name = models.CharField(_("name"), max_length=150)
    code = models.CharField(_("short code"), max_length=20, blank=True)
    address = models.TextField(_("address"), blank=True)
    latitude = models.DecimalField(
        _("latitude"),
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-90")), MaxValueValidator(Decimal("90"))],
    )
    longitude = models.DecimalField(
        _("longitude"),
        max_digits=9,
        decimal_places=6,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("-180")), MaxValueValidator(Decimal("180"))],
    )
    description = models.TextField(_("description"), blank=True)

    policy_object_type = "locations.site"

    class Meta(HierarchyNode.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint("tenant", Lower("name"), name="site_name_unique_per_tenant"),
            models.CheckConstraint(
                condition=models.Q(latitude__isnull=True, longitude__isnull=True)
                | models.Q(latitude__isnull=False, longitude__isnull=False),
                name="site_coordinates_complete",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class Building(HierarchyNode):
    """A building on a site."""

    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="buildings")
    name = models.CharField(_("name"), max_length=150)
    description = models.TextField(_("description"), blank=True)

    parent_field = "site"
    policy_object_type = "locations.building"

    class Meta(HierarchyNode.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint("site", Lower("name"), name="building_name_unique_per_site")
        ]

    def __str__(self) -> str:
        return self.name


class Floor(HierarchyNode):
    """A level of a building (or any area drawn on one plan)."""

    building = models.ForeignKey(Building, on_delete=models.PROTECT, related_name="floors")
    name = models.CharField(_("name"), max_length=150)
    level = models.SmallIntegerField(
        _("level"), default=0, help_text=_("0 = ground floor, negative = basement")
    )

    parent_field = "building"
    policy_object_type = "locations.floor"

    class Meta(HierarchyNode.Meta):
        ordering = ["level", "name"]
        constraints = [
            models.UniqueConstraint(
                "building", Lower("name"), name="floor_name_unique_per_building"
            )
        ]

    def __str__(self) -> str:
        return self.name


# Names of the built in rows, for translation catalogs (the rows hold the English text).
BUILTIN_ROOM_TYPES = (
    gettext_noop("Server room"),
    gettext_noop("Distribution room"),
    gettext_noop("Office"),
    gettext_noop("Meeting room"),
    gettext_noop("Other"),
)


class RoomType(models.Model):
    """What a room is used for, with the devices such a room normally needs."""

    key = models.SlugField(_("key"), max_length=30, unique=True)
    name = models.CharField(_("name"), max_length=60)
    description = models.TextField(_("description"), blank=True)
    order = models.PositiveSmallIntegerField(_("order"), default=100)
    builtin = models.BooleanField(default=False, editable=False)

    policy_object_type = "locations.roomtype"

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        """Built in names are translated; names given by administrators are shown as they are."""
        return gettext(self.name) if self.builtin else self.name


class RoomTypeNeed(models.Model):
    """How many devices of a category a room of this type should have."""

    room_type = models.ForeignKey(RoomType, on_delete=models.CASCADE, related_name="needs")
    category = models.ForeignKey(
        "library.DeviceCategory",
        to_field="key",
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("category"),
    )
    count = models.PositiveSmallIntegerField(_("count"))

    class Meta:
        ordering = ["category__order"]
        constraints = [models.UniqueConstraint("room_type", "category", name="roomtypeneed_unique")]

    def __str__(self) -> str:
        return f"{self.room_type}: {self.count} x {self.category_id}"


class Room(HierarchyNode):
    """A room on a floor that can hold racks and floor devices."""

    floor = models.ForeignKey(Floor, on_delete=models.PROTECT, related_name="rooms")
    name = models.CharField(_("name"), max_length=150)
    kind = models.ForeignKey(
        RoomType,
        to_field="key",
        db_column="kind",
        on_delete=models.PROTECT,
        default="other",
        related_name="rooms",
        verbose_name=_("room type"),
    )
    description = models.TextField(_("description"), blank=True)
    # Polygon on the floor plan: [[x, y], ...] in plan pixels; empty when not drawn.
    outline = models.JSONField(default=list, blank=True)

    parent_field = "floor"
    policy_object_type = "locations.room"

    class Meta(HierarchyNode.Meta):
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint("floor", Lower("name"), name="room_name_unique_per_floor")
        ]

    def __str__(self) -> str:
        return self.name


class RoomNeed(TenantScopedModel):
    """A room's own planned count for a category, replacing the count of its room type."""

    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="needs")
    category = models.ForeignKey(
        "library.DeviceCategory",
        to_field="key",
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("category"),
    )
    count = models.PositiveSmallIntegerField(_("count"))

    class Meta:
        ordering = ["category__order"]
        constraints = [models.UniqueConstraint("room", "category", name="roomneed_unique")]

    def __str__(self) -> str:
        return f"{self.room}: {self.count} x {self.category_id}"

    @property
    def policy_path(self) -> str:
        """Governed by its room."""
        return str(self.room.path)
