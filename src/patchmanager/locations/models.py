"""Sites, buildings, floors and rooms."""

from decimal import Decimal

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _

from patchmanager.core.hierarchy import HierarchyNode


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


class Room(HierarchyNode):
    """A room on a floor that can hold racks and floor devices."""

    class Kind(models.TextChoices):
        SERVER = "server", _("Server room")
        DISTRIBUTION = "distribution", _("Distribution room")
        OFFICE = "office", _("Office")
        OTHER = "other", _("Other")

    floor = models.ForeignKey(Floor, on_delete=models.PROTECT, related_name="rooms")
    name = models.CharField(_("name"), max_length=150)
    kind = models.CharField(_("kind"), max_length=20, choices=Kind.choices, default=Kind.OTHER)
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
