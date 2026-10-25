"""REST API v1. Reads are scoped by the policy engine; writes call the domain services."""

from typing import Any, ClassVar, cast

from django.db.models import QuerySet
from django.forms.models import model_to_dict
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import exceptions, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.request import Request
from rest_framework.response import Response

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.api import serializers as s
from patchmanager.api.core import filtered, lookup, not_found, tenant_scope, visible
from patchmanager.cabling import services as cabling
from patchmanager.cabling.models import Cable
from patchmanager.circuits.models import Circuit
from patchmanager.compute.models import Cluster, StorageVolume, VirtualMachine
from patchmanager.configs import services as configs
from patchmanager.configs.models import DeviceConfig
from patchmanager.core import crud
from patchmanager.dcim import devices
from patchmanager.dcim.models import Device, Port, Rack
from patchmanager.directory.models import Organization, System
from patchmanager.history import services as history
from patchmanager.history.models import Change
from patchmanager.ipam import services as ipam
from patchmanager.ipam.models import VRF, DHCPScope, DNSZone, IPAddress, Prefix
from patchmanager.library.models import DeviceCategory, DeviceType, Vendor
from patchmanager.locations import services as tree
from patchmanager.locations.models import Building, Floor, Room, RoomType, Site
from patchmanager.networks import services as networks
from patchmanager.networks.models import VLAN, WLAN
from patchmanager.search import services as search_services
from patchmanager.tenancy.models import Tenant


class Base(viewsets.ModelViewSet):  # type: ignore[type-arg]
    """Scoped reads; subclasses implement create, update and destroy through services."""

    object_type: ClassVar[str]
    model: ClassVar[Any]
    path_field: ClassVar[str] = "path"
    filters: ClassVar[dict[str, str]] = {}
    search: ClassVar[str] = "name__icontains"  # what ?q= matches
    related: ClassVar[tuple[str, ...]] = ()
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def base_queryset(self) -> QuerySet[Any]:
        """Rows before permission scoping."""
        rows: QuerySet[Any] = self.model.objects.all()
        return rows.select_related(*self.related) if self.related else rows

    def get_queryset(self) -> QuerySet[Any]:
        """Rows the caller may view, filtered by the query parameters."""
        rows = visible(
            self.request.user,
            f"{self.object_type}.view",
            self.base_queryset(),
            path_field=self.path_field,
        )
        if getattr(self, "swagger_fake_view", False):
            return rows
        return filtered(
            rows, self.request.query_params, {"q": self.search, **self.filters}
        ).order_by("pk")

    @property
    def actor(self) -> User:
        """The authenticated user (every endpoint requires one)."""
        assert isinstance(self.request.user, User)  # noqa: S101  # nosec B101: TokenScopes requires login
        return self.request.user

    @property
    def http(self) -> Any:
        """The Django request (services take it for audit and history)."""
        return self.request._request

    def created(self, obj: Any) -> Response:
        """201 response with the new object."""
        return Response(self.get_serializer(obj).data, status=status.HTTP_201_CREATED)

    def updated(self, obj: Any) -> Response:
        """Response with the stored state after a change."""
        return Response(self.get_serializer(self.model.objects.get(pk=obj.pk)).data)

    def validated(self, partial: bool = False, instance: Any = None) -> Any:
        """Validated serializer for the request body."""
        serializer = self.get_serializer(instance, data=self.request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        return serializer


# ---------------------------------------------------------------- locations and racks


class TreeNodes(Base):
    """Sites, buildings, floors, rooms and racks share the tree services."""

    parent_model: ClassVar[Any]
    parent_field: ClassVar[str]
    parent_permission: ClassVar[str | None]
    fields: ClassVar[tuple[str, ...]]

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service (permissions, validation, audit apply)."""
        data = self.validated()
        parent = lookup(
            request.user,
            self.parent_model,
            data.validated_data[f"{self.parent_field}_id"],
            self.parent_permission,
            self.parent_field,
        )
        node = tree.create(
            self.http, self.actor, self.model, parent=parent, **s.values(data, *self.fields)
        )
        return self.created(node)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        node = self.get_object()
        data = self.validated(partial=True, instance=node)
        return self.updated(
            tree.update(self.http, self.actor, node, **s.values(data, *self.fields))
        )

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it."""
        tree.delete(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


class SiteViewSet(TreeNodes):
    """Sites of the visible tenants."""

    serializer_class = s.SiteSerializer
    model, object_type = Site, "locations.site"
    parent_model, parent_field, parent_permission = (
        Tenant,
        "tenant",
        None,
    )  # adding needs locations.site.add
    fields = ("name", "code", "address", "latitude", "longitude", "description")
    filters: ClassVar[dict[str, str]] = {"tenant": "tenant_id", "code": "code__iexact"}


class BuildingViewSet(TreeNodes):
    """Buildings."""

    serializer_class = s.BuildingSerializer
    model, object_type = Building, "locations.building"
    parent_model, parent_field, parent_permission = Site, "site", "locations.site.view"
    fields = ("name", "description")
    filters: ClassVar[dict[str, str]] = {"site": "site_id"}


class FloorViewSet(TreeNodes):
    """Floors."""

    serializer_class = s.FloorSerializer
    model, object_type = Floor, "locations.floor"
    parent_model, parent_field, parent_permission = Building, "building", "locations.building.view"
    fields = ("name", "level")
    filters: ClassVar[dict[str, str]] = {"building": "building_id"}


class RoomViewSet(TreeNodes):
    """Rooms."""

    serializer_class = s.RoomSerializer
    model, object_type = Room, "locations.room"
    parent_model, parent_field, parent_permission = Floor, "floor", "locations.floor.view"
    fields = ("name", "kind", "description")
    filters: ClassVar[dict[str, str]] = {"floor": "floor_id"}


class RackViewSet(TreeNodes):
    """Racks."""

    serializer_class = s.RackSerializer
    model, object_type = Rack, "dcim.rack"
    parent_model, parent_field, parent_permission = Room, "room", "locations.room.view"
    fields = (
        "name",
        "u_height",
        "width",
        "depth_mm",
        "descending_units",
        "status",
        "serial",
        "asset_tag",
        "notes",
    )
    filters: ClassVar[dict[str, str]] = {
        "room": "room_id",
        "status": "status",
        "serial": "serial__iexact",
    }


# ---------------------------------------------------------------- library (read only)


def _token_allows(user: Any, permission: str, rows: QuerySet[Any]) -> QuerySet[Any]:
    """No rows when the API token's object types or location leave out ``permission``.

    Shared library rows have no location of their own, so only the limits apply, as in the
    library pages every member can read.
    """
    limits = policy.token_limits(user)
    if limits is not None and not limits.allows(permission, limits.scope_path or None):
        return rows.none()
    return rows


class VendorViewSet(viewsets.ReadOnlyModelViewSet):  # type: ignore[type-arg]
    """Vendors are shared by all tenants."""

    serializer_class = s.VendorSerializer

    def get_queryset(self) -> QuerySet[Vendor]:
        """Rows the caller may view, filtered by the query parameters."""
        rows = _token_allows(self.request.user, "library.vendor.view", Vendor.objects.all())
        return filtered(rows.order_by("name"), self.request.query_params, {"q": "name__icontains"})


class DeviceTypeViewSet(viewsets.ReadOnlyModelViewSet):  # type: ignore[type-arg]
    """Shared device types and those private to tenants the caller can see."""

    serializer_class = s.DeviceTypeSerializer

    def get_queryset(self) -> QuerySet[DeviceType]:
        """Rows the caller may view, filtered by the query parameters."""
        rows = DeviceType.objects.select_related("vendor").order_by(
            "vendor__name", "model"
        )  # RLS bounds tenants
        rows = _token_allows(self.request.user, "library.devicetype.view", rows)
        return filtered(
            rows,
            self.request.query_params,
            {"q": "model__icontains", "vendor": "vendor_id", "form": "form", "tenant": "tenant_id"},
        )


# ---------------------------------------------------------------- devices and ports


class DeviceViewSet(Base):
    """Devices in rooms and racks."""

    serializer_class = s.DeviceSerializer
    model, object_type = Device, "dcim.device"
    related = ("space",)
    filters: ClassVar[dict[str, str]] = {
        "room": "room_id",
        "rack": "rack_id",
        "device_type": "device_type_id",
        "status": "status",
        "serial": "serial__iexact",
        "asset_tag": "asset_tag__iexact",
    }
    fields = (
        "name",
        "status",
        "serial",
        "asset_tag",
        "purchase_date",
        "in_service_date",
        "responsible",
        "notes",
    )

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service (permissions, validation, audit apply)."""
        data = self.validated()
        place = s.PlacementSerializer(data=request.data)
        place.is_valid(raise_exception=True)
        device_type = lookup(
            request.user,
            DeviceType,
            data.validated_data["device_type_id"],
            None,  # RLS shows shared and own library types; the service checks the fit
            "device_type",
        )
        room, rack = self._place(place)
        device = devices.create(
            self.http,
            self.actor,
            device_type=device_type,
            room=room,
            rack=rack,
            position=place.validated_data.get("position"),
            face=place.validated_data["face"],
            **s.values(data, *self.fields),
        )
        return self.created(device)

    def _place(self, place: s.PlacementSerializer) -> tuple[Room, Rack | None]:
        room = lookup(
            self.request.user, Room, place.validated_data["room"], "locations.room.view", "room"
        )
        rack_id = place.validated_data.get("rack")
        rack = (
            lookup(self.request.user, Rack, rack_id, "dcim.rack.view", "rack") if rack_id else None
        )
        return room, rack

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        device = self.get_object()
        data = self.validated(partial=True, instance=device)
        return self.updated(
            devices.update(self.http, self.actor, device, **s.values(data, *self.fields))
        )

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it."""
        devices.delete(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=s.PlacementSerializer, responses=s.DeviceSerializer)
    @action(detail=True, methods=["post"])
    def place(self, request: Request, pk: str | None = None) -> Response:
        """Move a device to a room, or mount it in a rack at a position."""
        device = self.get_object()
        place = s.PlacementSerializer(data=request.data)
        place.is_valid(raise_exception=True)
        room, rack = self._place(place)
        devices.place(
            self.http,
            self.actor,
            device,
            room=room,
            rack=rack,
            position=place.validated_data.get("position"),
            face=place.validated_data["face"],
        )
        return self.updated(device)

    @extend_schema(
        request=s.ConfigWriteSerializer,
        responses={200: s.ConfigSerializer(many=True), 201: s.ConfigSerializer},
    )
    @action(detail=True, methods=["get", "post"])
    def configs(self, request: Request, pk: str | None = None) -> Response:
        """Stored configuration versions (newest first); POST stores a new one.

        A POST that repeats the latest version stores nothing and answers 200.
        """
        device = self.get_object()
        if request.method == "POST":
            body = s.ConfigWriteSerializer(data=request.data)
            body.is_valid(raise_exception=True)
            config, created = configs.add(
                self.http,
                self.actor,
                device,
                body.validated_data["content"],
                source=DeviceConfig.Source.API,
                comment=body.validated_data.get("comment", ""),
            )
            return Response(
                s.ConfigSerializer(config).data,
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        rows = visible(
            self.request.user, "configs.config.view", device.configs.order_by("-version")
        )
        return Response(s.ConfigSerializer(rows[:500], many=True).data)

    @extend_schema(responses=s.ConfigContentSerializer)
    @action(detail=True, methods=["get"], url_path=r"configs/(?P<version>\d+)")
    def config_version(
        self, request: Request, pk: str | None = None, version: str = ""
    ) -> Response:
        """One version with its text (the read is audited)."""
        device = self.get_object()
        config = (
            visible(self.request.user, "configs.config.view", device.configs.all())
            .filter(version=int(version))
            .first()
        )
        if config is None:
            raise not_found()
        data = dict(s.ConfigSerializer(config).data)
        data["content"] = configs.read(self.http, self.actor, config)
        return Response(data)


class PortViewSet(Base):
    """Ports come from device types; they can be documented, not created here."""

    serializer_class = s.PortSerializer
    model, object_type = Port, "dcim.device"
    filters: ClassVar[dict[str, str]] = {"device": "device_id", "kind": "kind"}
    http_method_names = ["get", "patch", "head", "options"]
    fields = ("connector", "speed_mbps", "enabled", "label", "mac_address", "description")

    def get_queryset(self) -> QuerySet[Port]:
        """Rows the caller may view, filtered by the query parameters."""
        allowed = visible(
            self.request.user, "dcim.device.view", Device.objects.all()
        )  # judged by the device
        rows = Port.objects.filter(device__in=allowed)
        return filtered(
            rows, self.request.query_params, {"q": "name__icontains", **self.filters}
        ).order_by("pk")

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        port = self.get_object()
        data = self.validated(partial=True, instance=port)
        return self.updated(
            devices.update_port(self.http, self.actor, port, **s.values(data, *self.fields))
        )

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)


# ---------------------------------------------------------------- cables and networks


class CableViewSet(Base):
    """Cables between two ports."""

    serializer_class = s.CableSerializer
    model, object_type = Cable, "cabling.cable"
    search = "label__icontains"
    filters: ClassVar[dict[str, str]] = {
        "kind": "kind",
        "media": "media",
        "status": "status",
        "label": "label__iexact",
    }
    fields = cabling.CABLE_FIELDS

    def base_queryset(self) -> QuerySet[Cable]:
        """Rows before permission scoping."""
        return Cable.objects.prefetch_related("ends")

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service (permissions, validation, audit apply)."""
        data = self.validated()
        ends = [
            lookup(
                request.user,
                Port,
                data.validated_data[side],
                "dcim.device.view",
                side,
                judge=lambda p: p.device,
            )
            for side in "ab"
        ]
        cable = cabling.connect(
            self.http, self.actor, ends[0], ends[1], **s.values(data, *self.fields)
        )
        return self.created(cable)

    def get_serializer(self, *args: Any, **kwargs: Any) -> Any:
        """Serializer for the action (cable ends are only required on create)."""
        serializer: Any = super().get_serializer(*args, **kwargs)
        if self.action in ("partial_update", "update"):
            for side in "ab":
                serializer.fields[side].required = False
        return serializer

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        cable = self.get_object()
        data = self.validated(partial=True, instance=cable)
        return self.updated(
            cabling.update(self.http, self.actor, cable, **s.values(data, *self.fields))
        )

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it."""
        cabling.disconnect(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


class VLANViewSet(Base):
    """VLANs of a tenant or site."""

    serializer_class = s.VLANSerializer
    model, object_type, path_field = VLAN, "networks.vlan", "scope"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "site": "site_id",
        "vid": "vid",
        "status": "status",
    }
    fields = ("vid", "name", "status", "description")

    def base_queryset(self) -> QuerySet[VLAN]:
        """Rows before permission scoping."""
        return tenant_scope(VLAN.objects.all(), "site")

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service (permissions, validation, audit apply)."""
        data = self.validated()
        tenant = lookup(request.user, Tenant, data.validated_data["tenant_id"], None, "tenant")
        site_id = data.validated_data.get("site_id")
        site = (
            lookup(request.user, Site, site_id, "locations.site.view", "site") if site_id else None
        )
        vlan = networks.create_vlan(
            self.http, self.actor, tenant, site=site, **s.values(data, *self.fields)
        )
        return self.created(vlan)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        vlan = self.get_object()
        data = self.validated(partial=True, instance=vlan)
        return self.updated(
            networks.update_vlan(self.http, self.actor, vlan, **s.values(data, *self.fields))
        )

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it."""
        networks.delete_vlan(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


class WLANViewSet(Base):
    """WLANs and the VLAN they bridge into."""

    serializer_class = s.WLANSerializer
    model, object_type, path_field = WLAN, "networks.wlan", "scope"
    search = "ssid__icontains"
    filters: ClassVar[dict[str, str]] = {"tenant": "tenant_id", "vlan": "vlan_id"}
    fields = ("ssid", "security", "hidden", "description")

    def base_queryset(self) -> QuerySet[WLAN]:
        """Rows before permission scoping."""
        return tenant_scope(WLAN.objects.all())

    def _vlan(self, data: Any) -> dict[str, Any]:
        if "vlan_id" not in data.validated_data:
            return {}
        vlan_id = data.validated_data["vlan_id"]
        return {
            "vlan": lookup(self.request.user, VLAN, vlan_id, "networks.vlan.view", "vlan")
            if vlan_id
            else None
        }

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service (permissions, validation, audit apply)."""
        data = self.validated()
        tenant = lookup(request.user, Tenant, data.validated_data["tenant_id"], None, "tenant")
        wlan = networks.create_wlan(
            self.http, self.actor, tenant, **s.values(data, *self.fields), **self._vlan(data)
        )
        return self.created(wlan)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        wlan = self.get_object()
        data = self.validated(partial=True, instance=wlan)
        changed = networks.update_wlan(
            self.http, self.actor, wlan, **s.values(data, *self.fields), **self._vlan(data)
        )
        return self.updated(changed)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it."""
        networks.delete_wlan(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- tenants, history, search


# ---------------------------------------------------------------- IP address management


def _vrf(user: Any, value: Any) -> Any:
    return lookup(user, VRF, value, "ipam.vrf.view", "vrf") if value else None


class VRFViewSet(Base):
    """VRFs (routing tables) of a tenant."""

    serializer_class = s.VRFSerializer
    model, object_type, path_field = VRF, "ipam.vrf", "scope"
    filters: ClassVar[dict[str, str]] = {"tenant": "tenant_id", "name": "name"}
    fields = ("name", "rd", "description")

    def base_queryset(self) -> QuerySet[VRF]:
        """Rows before permission scoping."""
        return tenant_scope(VRF.objects.all())

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service."""
        data = self.validated()
        tenant = lookup(request.user, Tenant, data.validated_data["tenant_id"], None, "tenant")
        return self.created(
            ipam.create_vrf(self.http, self.actor, tenant, **s.values(data, *self.fields))
        )

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        vrf = self.get_object()
        data = self.validated(partial=True, instance=vrf)
        return self.updated(
            ipam.update_vrf(self.http, self.actor, vrf, **s.values(data, *self.fields))
        )

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it."""
        ipam.delete_vrf(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


class PrefixViewSet(Base):
    """Prefixes; ``?within=10.0.0.0/8`` lists those inside a network."""

    serializer_class = s.PrefixSerializer
    model, object_type = Prefix, "ipam.prefix"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "vrf": "vrf_id",
        "site": "site_id",
        "vlan": "vlan_id",
        "status": "status",
        "within": "prefix__net_within_or_equal",
    }
    search = "text__icontains"
    fields = ("prefix", "status", "gateway", "description")

    def _refs(self, data: Any) -> dict[str, Any]:
        user, values = self.request.user, data.validated_data
        refs: dict[str, Any] = {}
        if "vrf_id" in values:
            refs["vrf"] = _vrf(user, values["vrf_id"])
        if "site_id" in values:
            site_id = values["site_id"]
            refs["site"] = (
                lookup(user, Site, site_id, "locations.site.view", "site") if site_id else None
            )
        if "vlan_id" in values:
            vlan_id = values["vlan_id"]
            refs["vlan"] = (
                lookup(user, VLAN, vlan_id, "networks.vlan.view", "vlan") if vlan_id else None
            )
        return refs

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service."""
        data = self.validated()
        tenant = lookup(request.user, Tenant, data.validated_data["tenant_id"], None, "tenant")
        prefix = ipam.create_prefix(
            self.http, self.actor, tenant, **self._refs(data), **s.values(data, *self.fields)
        )
        return self.created(prefix)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        prefix = self.get_object()
        data = self.validated(partial=True, instance=prefix)
        changed = ipam.update_prefix(
            self.http, self.actor, prefix, **self._refs(data), **s.values(data, *self.fields)
        )
        return self.updated(changed)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it; its addresses stay."""
        ipam.delete_prefix(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


class IPAddressViewSet(Base):
    """Addresses; ``?within=10.0.1.0/24`` lists those inside a network."""

    serializer_class = s.IPAddressSerializer
    model, object_type = IPAddress, "ipam.ipaddress"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "vrf": "vrf_id",
        "port": "port_id",
        "status": "status",
        "dns_name": "dns_name",
        "within": "address__net_within_or_equal",
    }
    search = "host__icontains"
    fields = ("address", "status", "dns_name", "description")

    def _refs(self, data: Any) -> dict[str, Any]:
        user, values = self.request.user, data.validated_data
        refs: dict[str, Any] = {}
        if "vrf_id" in values:
            refs["vrf"] = _vrf(user, values["vrf_id"])
        if "port_id" in values:
            port_id = values["port_id"]
            refs["port"] = (
                lookup(user, Port, port_id, "dcim.device.view", "port", judge=lambda p: p.device)
                if port_id
                else None
            )
        return refs

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the domain service."""
        data = self.validated()
        tenant = lookup(request.user, Tenant, data.validated_data["tenant_id"], None, "tenant")
        address = ipam.create_address(
            self.http, self.actor, tenant, **self._refs(data), **s.values(data, *self.fields)
        )
        return self.created(address)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        address = self.get_object()
        data = self.validated(partial=True, instance=address)
        changed = ipam.update_address(
            self.http, self.actor, address, **self._refs(data), **s.values(data, *self.fields)
        )
        return self.updated(changed)

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it."""
        ipam.delete_address(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- records with shared pages


class Records(Base):
    """Tenant records edited through the shared record pages (``core.crud``).

    Writes are validated by the page's form, so references are limited to the record's
    tenant and to what the caller may see, exactly as in the UI.
    """

    resource: ClassVar[str]  # key in crud.REGISTRY
    path_field = "scope"

    def base_queryset(self) -> QuerySet[Any]:
        """Rows before permission scoping (tenant wide records)."""
        return tenant_scope(super().base_queryset())

    def _body(self) -> dict[str, Any]:
        if not isinstance(self.request.data, dict):
            raise exceptions.ValidationError({"detail": "Expected a JSON object."})
        return self.request.data

    def _form(self, tenant: Tenant, instance: Any = None) -> dict[str, Any]:
        """Cleaned fields of the request body; stored values (or defaults) fill the gaps."""
        resource = crud.REGISTRY[self.resource]
        body = self._body()
        known = cast(s.RecordSerializer, self.get_serializer()).fields
        unknown = sorted(set(body) - set(known))
        if unknown:  # a misspelt field must not look like a change that was applied
            raise exceptions.ValidationError({key: ["Unknown field."] for key in unknown})
        data = model_to_dict(instance or resource.model(), fields=resource.form._meta.fields)
        data.update(body)
        form = resource.form(data, instance=instance, tenant=tenant, actor=self.actor)
        if not form.is_valid():
            raise exceptions.ValidationError({k: list(v) for k, v in form.errors.items()})
        return dict(form.cleaned_data)

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Create one through the record service (permissions, validation, audit apply)."""
        tenant = lookup(request.user, Tenant, self._body().get("tenant"), None, "tenant")
        record = crud.create(
            self.http, self.actor, crud.REGISTRY[self.resource], tenant, **self._form(tenant)
        )
        return self.created(record)

    def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Change the fields that are sent."""
        record = self.get_object()
        fields = self._form(record.tenant, record)
        return self.updated(crud.update(self.http, self.actor, record, **fields))

    def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Same as PATCH: only the fields sent change."""
        return self.partial_update(request, *args, **kwargs)

    def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        """Delete it with its photos and notes."""
        crud.delete(self.http, self.actor, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)


class CircuitViewSet(Records):
    """Lines from providers."""

    serializer_class = s.CircuitSerializer
    model, object_type, resource = Circuit, "circuits.circuit", "circuits"
    search = "circuit_id__icontains"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "provider": "provider_id",
        "site": "site_id",
        "kind": "kind",
        "status": "status",
    }


class ClusterViewSet(Records):
    """Virtualisation clusters."""

    serializer_class = s.ClusterSerializer
    model, object_type, resource = Cluster, "compute.cluster", "clusters"
    filters: ClassVar[dict[str, str]] = {"tenant": "tenant_id", "site": "site_id", "kind": "kind"}


class VMViewSet(Records):
    """Virtual machines."""

    serializer_class = s.VMSerializer
    model, object_type, resource = VirtualMachine, "compute.vm", "vms"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "cluster": "cluster_id",
        "host": "host_id",
        "status": "status",
    }


class StorageViewSet(Records):
    """Pools, volumes, LUNs, shares and datastores."""

    serializer_class = s.StorageSerializer
    model, object_type, resource = StorageVolume, "compute.storage", "storage"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "device": "device_id",
        "cluster": "cluster_id",
        "kind": "kind",
    }


class OrganizationViewSet(Records):
    """Customers, providers, vendors and other organisations."""

    serializer_class = s.OrganizationSerializer
    model, object_type, resource = Organization, "directory.organization", "organizations"
    filters: ClassVar[dict[str, str]] = {"tenant": "tenant_id", "kind": "kind"}


class SystemViewSet(Records):
    """Systems with their customer, site and contact."""

    serializer_class = s.SystemSerializer
    model, object_type, resource = System, "directory.system", "systems"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "customer": "customer_id",
        "site": "site_id",
        "kind": "kind",
    }


class DHCPScopeViewSet(Records):
    """DHCP scopes."""

    serializer_class = s.DHCPScopeSerializer
    model, object_type, resource = DHCPScope, "ipam.dhcpscope", "dhcp"
    filters: ClassVar[dict[str, str]] = {
        "tenant": "tenant_id",
        "prefix": "prefix_id",
        "server": "server_id",
    }


class DNSZoneViewSet(Records):
    """DNS zones."""

    serializer_class = s.DNSZoneSerializer
    model, object_type, resource = DNSZone, "ipam.dnszone", "dns"
    filters: ClassVar[dict[str, str]] = {"tenant": "tenant_id", "server": "server_id"}


class AdminLists(viewsets.ReadOnlyModelViewSet):  # type: ignore[type-arg]
    """Instance wide lists edited in the administration (read only here)."""

    model: ClassVar[Any]
    object_type: ClassVar[str]

    def get_queryset(self) -> QuerySet[Any]:
        """All entries, as in the pages every member reads; API token limits apply."""
        rows = _token_allows(
            self.request.user,
            f"{self.object_type}.view",
            self.model.objects.order_by("order", "name"),
        )
        return filtered(rows, self.request.query_params, {"q": "name__icontains"})


class CategoryViewSet(AdminLists):
    """Device categories."""

    serializer_class = s.CategorySerializer
    model, object_type = DeviceCategory, "library.category"


class RoomTypeViewSet(AdminLists):
    """Room types."""

    serializer_class = s.RoomTypeSerializer
    model, object_type = RoomType, "locations.roomtype"


class TenantViewSet(viewsets.ReadOnlyModelViewSet):  # type: ignore[type-arg]
    """Tenants the caller can see."""

    serializer_class = s.TenantSerializer

    def get_queryset(self) -> QuerySet[Tenant]:
        """Rows the caller may view, filtered by the query parameters."""
        rows = Tenant.objects.filter(archived_at__isnull=True)  # RLS: tenants with any grant
        if getattr(self, "swagger_fake_view", False):  # schema generation has no caller
            return rows
        # As on the tenants page: only those the caller may view (token limits included).
        seen = [t.pk for t in rows if policy.has_perm(self.request.user, "tenancy.tenant.view", t)]
        return rows.filter(pk__in=seen).order_by("name")


class ChangeViewSet(viewsets.ReadOnlyModelViewSet):  # type: ignore[type-arg]
    """Change history of objects the caller may view."""

    serializer_class = s.ChangeSerializer

    def get_queryset(self) -> QuerySet[Change]:
        """Rows the caller may view, filtered by the query parameters."""
        rows = history.visible(self.request.user)
        return filtered(
            rows,
            self.request.query_params,
            {
                "object_type": "object_type",
                "object_id": "object_id",
                "action": "action",
                "tenant": "tenant_id",
                "since": "occurred_at__gte",
            },
        ).order_by("-occurred_at", "-id")


@extend_schema(
    parameters=[
        OpenApiParameter("q", str, required=True),
        OpenApiParameter("tenant", str, required=True),
        OpenApiParameter("type", str),
    ],
    responses={200: dict},
)
@api_view(["GET"])
def search(request: Request) -> Response:
    """Search one tenant: hits per type with id and name."""
    tenant = lookup(request.user, Tenant, request.query_params.get("tenant"), None, "tenant")
    results, counts = search_services.search(
        request.user,
        tenant,
        request.query_params.get("q", ""),
        kind=request.query_params.get("type", ""),
        limit=50,
    )
    return Response(
        {
            "counts": counts,
            "results": {
                kind: [
                    {"id": str(h.obj.pk), "name": str(h.obj), "context": h.context} for h in hits
                ]
                for kind, hits in results.items()
            },
        }
    )
