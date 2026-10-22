"""API representations. Writes are validated here and executed by the domain services."""

from typing import Any

from rest_framework import serializers

from patchmanager.cabling.models import Cable
from patchmanager.configs.models import DeviceConfig
from patchmanager.dcim.models import Device, Port, Rack
from patchmanager.history.models import Change
from patchmanager.ipam.models import VRF, IPAddress, Prefix
from patchmanager.library.models import DeviceType, Vendor
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.networks.models import VLAN, WLAN
from patchmanager.tenancy.models import Tenant

BASE = ["id", "created_at", "updated_at"]


class TenantSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Tenant as stored and as accepted on writes."""

    class Meta:
        model = Tenant
        fields = [*BASE, "slug", "name", "address", "contact_name", "contact_email"]
        read_only_fields = fields


class SiteSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Site as stored and as accepted on writes."""

    tenant = serializers.UUIDField(source="tenant_id", required=True)

    class Meta:
        model = Site
        fields = [
            *BASE,
            "tenant",
            "name",
            "code",
            "address",
            "latitude",
            "longitude",
            "description",
        ]
        read_only_fields = BASE


class BuildingSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Building as stored and as accepted on writes."""

    site = serializers.UUIDField(source="site_id")

    class Meta:
        model = Building
        fields = [*BASE, "site", "name", "description"]
        read_only_fields = BASE


class FloorSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Floor as stored and as accepted on writes."""

    building = serializers.UUIDField(source="building_id")

    class Meta:
        model = Floor
        fields = [*BASE, "building", "name", "level"]
        read_only_fields = BASE


class RoomSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Room as stored and as accepted on writes."""

    floor = serializers.UUIDField(source="floor_id")

    class Meta:
        model = Room
        fields = [*BASE, "floor", "name", "kind", "description"]
        read_only_fields = BASE


class RackSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Rack as stored and as accepted on writes."""

    room = serializers.UUIDField(source="room_id")

    class Meta:
        model = Rack
        fields = [
            *BASE,
            "room",
            "name",
            "u_height",
            "width",
            "depth_mm",
            "descending_units",
            "status",
            "serial",
            "asset_tag",
            "notes",
        ]
        read_only_fields = BASE


class VendorSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Vendor as stored and as accepted on writes."""

    class Meta:
        model = Vendor
        fields = [*BASE, "name", "website"]
        read_only_fields = fields


class DeviceTypeSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """DeviceType as stored and as accepted on writes."""

    vendor = serializers.UUIDField(source="vendor_id", read_only=True)
    vendor_name = serializers.CharField(source="vendor.name", read_only=True)
    tenant = serializers.UUIDField(source="tenant_id", read_only=True, allow_null=True)

    class Meta:
        model = DeviceType
        fields = [
            *BASE,
            "vendor",
            "vendor_name",
            "tenant",
            "model",
            "form",
            "category",
            "u_height",
            "full_depth",
            "part_number",
            "description",
        ]
        read_only_fields = fields


class DeviceSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Device as stored and as accepted on writes."""

    device_type = serializers.UUIDField(source="device_type_id")
    room = serializers.UUIDField(source="room_id")
    rack = serializers.UUIDField(source="rack_id", required=False, allow_null=True)
    position = serializers.SerializerMethodField()
    face = serializers.SerializerMethodField()

    class Meta:
        model = Device
        fields = [
            *BASE,
            "device_type",
            "room",
            "rack",
            "position",
            "face",
            "name",
            "status",
            "serial",
            "asset_tag",
            "purchase_date",
            "in_service_date",
            "responsible",
            "notes",
        ]
        read_only_fields = BASE

    def get_position(self, obj: Device) -> int | None:
        """Lowest rack unit, for mounted devices."""
        space = getattr(obj, "space", None) if obj.rack_id else None
        return space.start if space else None

    def get_face(self, obj: Device) -> str | None:
        """Mounted face, for mounted devices."""
        space = getattr(obj, "space", None) if obj.rack_id else None
        return space.face if space else None


class PlacementSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """Where a device goes."""

    room = serializers.UUIDField()
    rack = serializers.UUIDField(required=False, allow_null=True)
    position = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    face = serializers.ChoiceField(choices=["front", "rear", "both"], default="front")


class PortSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Port as stored and as accepted on writes."""

    device = serializers.UUIDField(source="device_id", read_only=True)
    untagged_vlan = serializers.UUIDField(
        source="untagged_vlan_id", read_only=True, allow_null=True
    )

    class Meta:
        model = Port
        fields = [
            *BASE,
            "device",
            "name",
            "kind",
            "connector",
            "speed_mbps",
            "enabled",
            "label",
            "mac_address",
            "description",
            "positions",
            "vlan_mode",
            "untagged_vlan",
        ]
        read_only_fields = [
            *BASE,
            "device",
            "name",
            "kind",
            "positions",
            "vlan_mode",
            "untagged_vlan",
        ]


class CableSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Cable as stored and as accepted on writes."""

    a = serializers.UUIDField(write_only=True, help_text="Port id of end A (on create)")
    b = serializers.UUIDField(write_only=True, help_text="Port id of end B (on create)")
    ends = serializers.SerializerMethodField()

    class Meta:
        model = Cable
        fields = [
            *BASE,
            "a",
            "b",
            "ends",
            "kind",
            "media",
            "colour",
            "length_m",
            "label",
            "status",
            "notes",
        ]
        read_only_fields = BASE

    def get_ends(self, obj: Cable) -> dict[str, str]:
        """Port id per side."""
        return {e.side: str(e.port_id) for e in obj.ends.all()}


class VLANSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """VLAN as stored and as accepted on writes."""

    tenant = serializers.UUIDField(source="tenant_id")
    site = serializers.UUIDField(source="site_id", required=False, allow_null=True)

    class Meta:
        model = VLAN
        fields = [*BASE, "tenant", "site", "vid", "name", "status", "description"]
        read_only_fields = BASE


class WLANSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """WLAN as stored and as accepted on writes."""

    tenant = serializers.UUIDField(source="tenant_id")
    vlan = serializers.UUIDField(source="vlan_id", required=False, allow_null=True)

    class Meta:
        model = WLAN
        fields = [*BASE, "tenant", "ssid", "vlan", "security", "hidden", "description"]
        read_only_fields = BASE


class VRFSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """VRF as stored and as accepted on writes."""

    tenant = serializers.UUIDField(source="tenant_id")

    class Meta:
        model = VRF
        fields = [*BASE, "tenant", "name", "rd", "description"]
        read_only_fields = BASE


class PrefixSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Prefix as stored and as accepted on writes."""

    tenant = serializers.UUIDField(source="tenant_id")
    vrf = serializers.UUIDField(source="vrf_id", required=False, allow_null=True)
    site = serializers.UUIDField(source="site_id", required=False, allow_null=True)
    vlan = serializers.UUIDField(source="vlan_id", required=False, allow_null=True)
    prefix = serializers.CharField(max_length=50)
    gateway = serializers.CharField(max_length=50, required=False, allow_null=True)

    class Meta:
        model = Prefix
        fields = [
            *BASE,
            "tenant",
            "vrf",
            "prefix",
            "status",
            "site",
            "vlan",
            "gateway",
            "description",
        ]
        read_only_fields = BASE


class IPAddressSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Address as stored and as accepted on writes."""

    tenant = serializers.UUIDField(source="tenant_id")
    vrf = serializers.UUIDField(source="vrf_id", required=False, allow_null=True)
    port = serializers.UUIDField(source="port_id", required=False, allow_null=True)
    address = serializers.CharField(max_length=50)

    class Meta:
        model = IPAddress
        fields = [*BASE, "tenant", "vrf", "address", "status", "dns_name", "description", "port"]
        read_only_fields = BASE


class ConfigSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """A stored configuration version (without its text)."""

    class Meta:
        model = DeviceConfig
        fields = ["id", "version", "created_at", "sha256", "size", "lines", "source", "comment"]
        read_only_fields = fields


class ConfigContentSerializer(ConfigSerializer):
    """A version with its text."""

    content = serializers.CharField()

    class Meta(ConfigSerializer.Meta):
        fields = [*ConfigSerializer.Meta.fields, "content"]
        read_only_fields = fields


class ConfigWriteSerializer(serializers.Serializer):  # type: ignore[type-arg]
    """A configuration to store."""

    content = serializers.CharField(trim_whitespace=False)
    comment = serializers.CharField(max_length=200, required=False, allow_blank=True)


class ChangeSerializer(serializers.ModelSerializer):  # type: ignore[type-arg]
    """Change as stored and as accepted on writes."""

    class Meta:
        model = Change
        fields = [
            "id",
            "occurred_at",
            "tenant_id",
            "object_type",
            "object_id",
            "label",
            "action",
            "fields",
            "before",
            "after",
            "actor_id",
            "actor_label",
            "request_id",
        ]
        read_only_fields = fields


def values(serializer: serializers.Serializer, *names: str) -> dict[str, Any]:  # type: ignore[type-arg]
    """Validated plain fields (by attribute name), only those sent."""
    data = serializer.validated_data
    return {name: data[name] for name in names if name in data}
