"""Vendors, device types (templates) and their port templates."""

import re
from typing import Any

from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext, pgettext_lazy

from patchmanager.core.fields import path_label
from patchmanager.core.models import TimestampedModel, UUIDModel

_PLACEHOLDER = re.compile(r"\{n(?::0?(\d{1,2}))?\}")


def expand(pattern: str, number: int) -> str:
    """Replace ``{n}`` or ``{n:02}`` in ``pattern``. Nothing else is interpreted."""
    return _PLACEHOLDER.sub(lambda m: str(number).zfill(int(m.group(1) or 0)), pattern)


class Vendor(TimestampedModel):
    """A manufacturer. Vendors are shared by all tenants."""

    name = models.CharField(_("name"), max_length=100)
    website = models.URLField(_("website"), blank=True)

    policy_object_type = "library.vendor"
    policy_path = None

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="vendor_name_unique")]

    def __str__(self) -> str:
        return self.name


class Connector(models.TextChoices):
    """Physical connectors (grouped loosely by medium)."""

    RJ45 = "rj45", "RJ45 (8P8C)"
    RJ11 = "rj11", "RJ11"
    LC = "lc", "LC"
    SC = "sc", "SC"
    ST = "st", "ST"
    E2000 = "e2000", "E2000"
    MPO = "mpo", "MPO/MTP"
    SFP = "sfp", "SFP"
    SFP_PLUS = "sfp+", "SFP+"
    SFP28 = "sfp28", "SFP28"
    QSFP = "qsfp+", "QSFP+"
    QSFP28 = "qsfp28", "QSFP28"
    COAX = "coax", "Coax (F/BNC)"
    USB = "usb", "USB-A"
    USB_C = "usb-c", "USB-C"
    HDMI = "hdmi", "HDMI"
    DISPLAYPORT = "dp", "DisplayPort"
    MINI_DP = "mini-dp", "Mini DisplayPort"
    DVI = "dvi", "DVI"
    VGA = "vga", "VGA"
    AUDIO = "audio", _("audio jack (3.5 mm)")
    SERIAL = "serial", "Serial (DB9/RJ45 console)"
    C13 = "c13", "IEC C13"
    C14 = "c14", "IEC C14"
    C19 = "c19", "IEC C19"
    C20 = "c20", "IEC C20"
    SCHUKO = "schuko", "Schuko"
    WIRELESS = "wireless", _("wireless")
    OTHER = "other", _("other")


class PortKind(models.TextChoices):
    """What a port is for."""

    INTERFACE = "interface", _("network interface")
    FRONT = "front", _("front port (passive)")
    REAR = "rear", _("rear port (passive)")
    CONSOLE = "console", _("console")
    POWER = "power", _("power")
    WIRELESS = "wireless", _("wireless")
    USB = "usb", "USB"
    VIDEO = "video", _("video output")
    AUDIO = "audio", _("audio")


class Airflow(models.TextChoices):
    """Direction of the cooling air (as in the community device type library)."""

    FRONT_TO_REAR = "front-to-rear", _("front to rear")
    REAR_TO_FRONT = "rear-to-front", _("rear to front")
    LEFT_TO_RIGHT = "left-to-right", _("left to right")
    RIGHT_TO_LEFT = "right-to-left", _("right to left")
    SIDE_TO_REAR = "side-to-rear", _("side to rear")
    REAR_TO_SIDE = "rear-to-side", _("rear to side")
    BOTTOM_TO_TOP = "bottom-to-top", _("bottom to top")
    TOP_TO_BOTTOM = "top-to-bottom", _("top to bottom")
    PASSIVE = "passive", _("passive (no fans)")
    MIXED = "mixed", _("mixed")


class Mounting(models.TextChoices):
    """How a device is fixed in place."""

    RACK_19 = "rack-19", _("19 inch rack")
    RACK_10 = "rack-10", _("10 inch rack")
    WALL = "wall", _("wall")
    FLUSH = "flush", _("flush in wall or floor box")
    CEILING = "ceiling", _("ceiling")
    DESK = "desk", _("desk")
    FLOOR = "floor", _("standing on the floor")
    DIN_RAIL = "din-rail", _("DIN rail")
    POLE = "pole", _("pole or mast")
    SHELF = "shelf", _("shelf")
    OTHER = "other", _("other")


# Names of the built in rows, for translation catalogs (the rows hold the English text).
BUILTIN_CATEGORIES = (
    pgettext_lazy("device category", "Switch"),
    pgettext_lazy("device category", "Router"),
    pgettext_lazy("device category", "Firewall"),
    pgettext_lazy("device category", "Server"),
    pgettext_lazy("device category", "Storage"),
    pgettext_lazy("device category", "Patch panel"),
    pgettext_lazy("device category", "Power distribution"),
    pgettext_lazy("device category", "UPS"),
    pgettext_lazy("device category", "Access point"),
    pgettext_lazy("device category", "Camera"),
    pgettext_lazy("device category", "Printer"),
    pgettext_lazy("device category", "Network outlet"),
    pgettext_lazy("device category", "Phone"),
    pgettext_lazy("device category", "Other"),
)


class DeviceCategory(models.Model):
    """What a device is for (switch, access point, outlet ...); editable by administrators."""

    key = models.SlugField(_("key"), max_length=30, unique=True)
    name = models.CharField(_("name"), max_length=60)
    glyph = models.CharField(
        _("short sign"),
        max_length=2,
        blank=True,
        help_text=_("Shown on floor plan pins, for example AP."),
    )
    colour = models.CharField(
        _("colour"),
        max_length=7,
        default="#475569",
        validators=[RegexValidator(r"^#[0-9a-fA-F]{6}$", _("Use a hex colour like #2563eb."))],
    )
    order = models.PositiveSmallIntegerField(_("order"), default=100)
    builtin = models.BooleanField(default=False, editable=False)

    policy_object_type = "library.category"

    class Meta:
        ordering = ["order", "name"]

    def __str__(self) -> str:
        return self.label

    @property
    def label(self) -> str:
        """Built in names are translated; names given by administrators are shown as they are."""
        return pgettext("device category", self.name) if self.builtin else self.name

    @staticmethod
    def by_label(rows: Any) -> list[DeviceCategory]:
        """Alphabetical by shown name, "other" last."""
        return sorted(rows, key=lambda c: (c.key == "other", c.label.casefold()))


class DeviceType(TimestampedModel):
    """A template for devices. Shared (``tenant`` empty) or private to one tenant."""

    class Form(models.TextChoices):
        RACK = "rack", _("rack mounted")
        FLOOR = "floor", _("floor device")

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="+",
        verbose_name=_("private to tenant"),
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="device_types")
    model = models.CharField(_("model"), max_length=150)
    form = models.CharField(_("form"), max_length=5, choices=Form.choices, default=Form.RACK)
    category = models.ForeignKey(
        DeviceCategory,
        to_field="key",
        db_column="category",
        on_delete=models.PROTECT,
        default="other",
        related_name="device_types",
        verbose_name=_("category"),
    )
    u_height = models.PositiveSmallIntegerField(
        _("height (U)"),
        default=1,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text=_("Only for rack mounted devices."),
    )
    full_depth = models.BooleanField(
        _("full depth"), default=True, help_text=_("Occupies front and rear.")
    )
    name_pattern = models.CharField(
        _("naming convention"),
        max_length=100,
        blank=True,
        help_text=_("For example SW-{n:02}. {n} is a counter."),
    )
    part_number = models.CharField(_("part number"), max_length=100, blank=True)
    mounting = models.CharField(_("mounting"), max_length=10, choices=Mounting.choices, blank=True)
    airflow = models.CharField(_("airflow"), max_length=15, choices=Airflow.choices, blank=True)
    power_supplies = models.PositiveSmallIntegerField(
        _("power supplies"),
        null=True,
        blank=True,
        validators=[MaxValueValidator(16)],
        help_text=_("How many power supplies the device has."),
    )
    max_power_w = models.PositiveIntegerField(
        _("maximum power (W)"), null=True, blank=True, validators=[MaxValueValidator(100_000)]
    )
    weight_kg = models.DecimalField(
        _("weight (kg)"),
        max_digits=7,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
    )
    description = models.TextField(_("description"), blank=True)
    front_image = models.ForeignKey(
        "metadata.Attachment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    rear_image = models.ForeignKey(
        "metadata.Attachment", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    policy_object_type = "library.devicetype"

    class Meta:
        ordering = ["vendor__name", "model"]
        constraints = [
            models.UniqueConstraint(
                "vendor",
                Lower("model"),
                "tenant",
                nulls_distinct=False,
                name="devicetype_model_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(form="floor") | models.Q(u_height__gte=1),
                name="devicetype_rack_height",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.vendor} {self.model}"

    @property
    def policy_path(self) -> str | None:
        """Shared types are global objects; private ones belong to their tenant."""
        return path_label(self.tenant_id) if self.tenant_id else None


class PortTemplate(UUIDModel):
    """``count`` ports named by ``name_pattern``, created with every device of the type.

    Front ports of passive equipment map to rear ports: with as many rear as front
    ports one to one, with fewer rear ports in order (for example 12 LC fronts into
    one MPO rear with ``positions`` 12).
    """

    device_type = models.ForeignKey(
        DeviceType, on_delete=models.CASCADE, related_name="port_templates"
    )
    tenant = models.ForeignKey(
        "tenancy.Tenant", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    name_pattern = models.CharField(
        _("name"), max_length=100, help_text=_("For example Gi1/0/{n}.")
    )
    kind = models.CharField(
        _("kind"), max_length=10, choices=PortKind.choices, default=PortKind.INTERFACE
    )
    connector = models.CharField(
        _("connector"), max_length=10, choices=Connector.choices, default=Connector.RJ45
    )
    speed_mbps = models.PositiveIntegerField(_("speed (Mbit/s)"), null=True, blank=True)
    count = models.PositiveSmallIntegerField(
        _("count"), default=1, validators=[MinValueValidator(1), MaxValueValidator(512)]
    )
    start = models.PositiveSmallIntegerField(_("first number"), default=1)
    positions = models.PositiveSmallIntegerField(
        _("positions per rear port"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(144)],
    )
    rear_template = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="front_templates",
        verbose_name=_("maps to rear ports"),
    )
    # Explicit mapping of a single front port (imported definitions): rear port name and position.
    rear_port_name = models.CharField(max_length=100, blank=True)
    rear_position = models.PositiveSmallIntegerField(null=True, blank=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "kind", "name_pattern"]

    def __str__(self) -> str:
        return self.name_pattern

    def save(self, *args: Any, **kwargs: Any) -> None:
        """Share the device type's tenant (Row Level Security)."""
        self.tenant_id = self.device_type.tenant_id
        super().save(*args, **kwargs)

    def names(self) -> list[str]:
        """All port names this template produces."""
        numbers = range(self.start, self.start + self.count)
        if _PLACEHOLDER.search(self.name_pattern):
            return [expand(self.name_pattern, i) for i in numbers]
        if self.count == 1:
            return [self.name_pattern]
        return [f"{self.name_pattern}{i}" for i in numbers]


class CatalogType(models.Model):
    """A device type of the bundled community library (CC0), ready to copy into the library."""

    slug = models.CharField(max_length=200, unique=True)
    manufacturer = models.CharField(max_length=100)
    model = models.CharField(max_length=150)
    part_number = models.CharField(max_length=100, blank=True)
    u_height = models.FloatField(default=1)
    full_depth = models.BooleanField(default=True)
    ports = models.PositiveIntegerField(default=0)
    summary = models.CharField(max_length=300, blank=True)
    spec = models.JSONField()
    commit = models.CharField(max_length=40)

    class Meta:
        ordering = ["manufacturer", "model"]

    def __str__(self) -> str:
        return f"{self.manufacturer} {self.model}"
