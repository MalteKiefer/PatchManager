"""Table kinds: their columns, export rows and import handlers.

Export and import use the same columns, so an exported file can be edited and
imported again. Import handlers go through the domain services, so permissions,
validation and audit apply to every row.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.db.models import Q
from django.http import HttpRequest
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.cabling import services as cabling
from patchmanager.cabling.models import Cable, CableEnd
from patchmanager.dcim import devices
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.library.models import DeviceType
from patchmanager.locations import services as tree
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.networks import services as networks
from patchmanager.networks.models import VLAN
from patchmanager.tenancy.models import Tenant

LOCATION = ["site", "building", "floor", "room"]
EXPORT_LIMIT = (
    20_000  # ponytail: synchronous export; move to a background job if larger sets are needed
)

Row = dict[str, str]


@dataclass(frozen=True)
class Table:
    """One importable and exportable kind of object."""

    kind: str
    title: Any
    columns: list[str]
    required: list[str]
    export: Callable[[Any, Tenant, Site | None], Iterator[dict[str, Any]]]
    handler: Callable[[HttpRequest, User, Tenant, Row], str] | None = None  # None: export only


# ---------------------------------------------------------------- helpers


def _int(row: Row, key: str) -> int | None:
    value = row.get(key, "")
    if not value:
        return None
    try:
        number = float(value)
    except ValueError as exc:
        raise ServiceError(_("%(col)s must be a number.") % {"col": key}) from exc
    if not number.is_integer():
        raise ServiceError(_("%(col)s must be a whole number.") % {"col": key})
    return int(number)


def _float(row: Row, key: str) -> float | None:
    value = row.get(key, "").replace(",", ".")
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise ServiceError(_("%(col)s must be a number.") % {"col": key}) from exc


def _choice(row: Row, key: str, choices: Any) -> str | None:
    """Accept the stored value or its label, case insensitive."""
    value = row.get(key, "").lower()
    if not value:
        return None
    for stored, label in choices:
        if value in (str(stored).lower(), str(label).lower()):
            return str(stored)
    allowed = ", ".join(str(s) for s, _label in choices)
    raise ServiceError(_("%(col)s must be one of: %(allowed)s.") % {"col": key, "allowed": allowed})


def _fields(row: Row, spec: dict[str, Callable[[Row, str], Any]]) -> dict[str, Any]:
    """Values of the filled cells only: empty cells keep current or default values."""
    values = {key: parse(row, key) for key, parse in spec.items()}
    return {k: v for k, v in values.items() if v is not None}


def _text(row: Row, key: str) -> str | None:
    return row.get(key) or None


def _changed(obj: Any, values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if getattr(obj, k) != v}


def location(request: HttpRequest, actor: User, tenant: Tenant, row: Row, *, create: bool) -> Any:
    """The deepest location named in the row; with ``create`` missing levels are added."""
    parent: Any = tenant
    node: Any = None
    models: tuple[Any, ...] = (Site, Building, Floor, Room)
    for level, model in zip(LOCATION, models, strict=True):
        name = row.get(level, "")
        if not name:
            break
        query = model.objects.filter(name__iexact=name)
        query = (
            query.filter(tenant=tenant)
            if model is Site
            else query.filter(**{model.parent_field: parent})
        )
        node = query.first()
        if node is None:
            if not create:
                raise ServiceError(
                    _("%(level)s %(name)s not found.") % {"level": level, "name": name}
                )
            extra: dict[str, Any] = {}
            if model is Site and row.get("site_code"):
                extra["code"] = row["site_code"]
            if model is Floor:
                extra["level"] = _int(row, "floor_level") or 0
            if model is Room and row.get("room_kind"):
                extra["kind"] = _choice(row, "room_kind", Room.Kind.choices)
            node = tree.create(request, actor, model, parent=parent, name=name, **extra)
        parent = node
    if node is None:
        raise ServiceError(_("The site is missing."))
    return node


def _names(obj: Any) -> dict[str, str]:
    """Location columns of a node (or of the room of a rack or device)."""
    room = obj if isinstance(obj, Room) else getattr(obj, "room", None)
    if room is None:
        return {}
    floor = room.floor
    return {
        "site": floor.building.site.name,
        "building": floor.building.name,
        "floor": floor.name,
        "room": room.name,
    }


def _scoped(user: Any, permission: str, queryset: Any, site: Site | None) -> Any:
    if site is not None:
        queryset = queryset.filter(path__descendant_of=str(site.path))
    return policy.scope_queryset(user, permission, queryset)


# ---------------------------------------------------------------- locations


def export_locations(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """One row per room, plus empty floors, buildings and sites."""
    rooms = _scoped(user, "locations.room.view", Room.objects.filter(tenant=tenant), site)
    seen_floors = set()
    for room in rooms.select_related("floor__building__site").order_by("path")[:EXPORT_LIMIT]:
        seen_floors.add(room.floor_id)
        yield {
            **_names(room),
            "site_code": room.floor.building.site.code,
            "floor_level": room.floor.level,
            "room_kind": room.kind,
        }
    floors = _scoped(user, "locations.floor.view", Floor.objects.filter(tenant=tenant), site)
    for floor in (
        floors.select_related("building__site")
        .exclude(pk__in=seen_floors)
        .order_by("path")[:EXPORT_LIMIT]
    ):
        yield {
            "site": floor.building.site.name,
            "site_code": floor.building.site.code,
            "building": floor.building.name,
            "floor": floor.name,
            "floor_level": floor.level,
        }


def import_location(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Create the location path, missing levels included."""
    before = sum(m.objects.filter(tenant=tenant).count() for m in (Site, Building, Floor, Room))
    location(request, actor, tenant, row, create=True)
    after = sum(m.objects.filter(tenant=tenant).count() for m in (Site, Building, Floor, Room))
    return "created" if after > before else "unchanged"


# ---------------------------------------------------------------- racks

RACK_FIELDS: dict[str, Callable[[Row, str], Any]] = {
    "u_height": _int,
    "status": lambda r, k: _choice(r, k, Rack.Status.choices),
    "serial": _text,
    "asset_tag": _text,
}


def export_racks(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """Racks with their location."""
    racks = _scoped(user, "dcim.rack.view", Rack.objects.filter(tenant=tenant), site)
    for rack in racks.select_related("room__floor__building__site").order_by("path")[:EXPORT_LIMIT]:
        yield {
            **_names(rack),
            "name": rack.name,
            "u_height": rack.u_height,
            "status": rack.status,
            "serial": rack.serial,
            "asset_tag": rack.asset_tag,
        }


def import_rack(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Create or update a rack by name within its room."""
    room = location(request, actor, tenant, row, create=False)
    if not isinstance(room, Room):
        raise ServiceError(_("Racks need a room."))
    values = _fields(row, RACK_FIELDS)
    rack = Rack.objects.filter(room=room, name__iexact=row["name"]).first()
    if rack is None:
        tree.create(request, actor, Rack, parent=room, name=row["name"], **values)
        return "created"
    changes = _changed(rack, values)
    if not changes:
        return "unchanged"
    tree.update(request, actor, rack, **changes)
    return "updated"


# ---------------------------------------------------------------- devices

DEVICE_FIELDS: dict[str, Callable[[Row, str], Any]] = {
    "status": lambda r, k: _choice(r, k, Device.Status.choices),
    "serial": _text,
    "asset_tag": _text,
}


def export_devices(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """Devices with location and mounting."""
    found = _scoped(user, "dcim.device.view", Device.objects.filter(tenant=tenant), site)
    for device in found.select_related(
        "room__floor__building__site", "rack", "device_type__vendor"
    ).order_by("path")[:EXPORT_LIMIT]:
        space = RackSpace.objects.filter(device=device).first() if device.rack_id else None
        yield {
            **_names(device),
            "rack": device.rack.name if device.rack else "",
            "position": space.start if space else "",
            "face": space.face if space else "",
            "name": device.name,
            "vendor": device.device_type.vendor.name,
            "model": device.device_type.model,
            "status": device.status,
            "serial": device.serial,
            "asset_tag": device.asset_tag,
        }


def _device_type(tenant: Tenant, row: Row) -> DeviceType:
    found = DeviceType.objects.filter(
        Q(tenant=None) | Q(tenant=tenant),
        vendor__name__iexact=row["vendor"],
        model__iexact=row["model"],
    ).first()
    if found is None:
        raise ServiceError(
            _("Device type %(v)s %(m)s not found.") % {"v": row["vendor"], "m": row["model"]}
        )
    return found


def import_device(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Create a device, or update and move it by name."""
    room = location(request, actor, tenant, row, create=False)
    if not isinstance(room, Room):
        raise ServiceError(_("Devices need a room."))
    rack = None
    if row.get("rack"):
        rack = Rack.objects.filter(room=room, name__iexact=row["rack"]).first()
        if rack is None:
            raise ServiceError(_("Rack %(name)s not found.") % {"name": row["rack"]})
    position = _int(row, "position")
    face = _choice(row, "face", RackSpace.Face.choices)
    values = _fields(row, DEVICE_FIELDS)
    device = (
        Device.objects.filter(tenant=tenant, name__iexact=row["name"])
        .select_related("rack")
        .first()
    )
    if device is None:
        devices.create(
            request,
            actor,
            device_type=_device_type(tenant, row),
            room=room,
            rack=rack,
            position=position,
            face=face or "front",
            name=row["name"],
            **values,
        )
        return "created"
    outcome = "unchanged"
    space = RackSpace.objects.filter(device=device).first()
    face = face or (space.face if space else "front")  # empty cell: keep the mounting face
    current = (
        device.room_id,
        device.rack_id,
        space.start if space else None,
        space.face if space else None,
    )
    wanted = (
        room.pk,
        rack.pk if rack else None,
        position if rack else None,
        face if rack else None,
    )
    if current != wanted:
        devices.place(request, actor, device, room=room, rack=rack, position=position, face=face)
        outcome = "updated"
    changes = _changed(device, values)
    if changes:
        devices.update(request, actor, device, **changes)
        outcome = "updated"
    return outcome


# ---------------------------------------------------------------- VLANs

VLAN_FIELDS: dict[str, Callable[[Row, str], Any]] = {
    "name": _text,
    "status": lambda r, k: _choice(r, k, VLAN.Status.choices),
    "description": _text,
}


def export_vlans(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """VLANs of the tenant (and site)."""
    vlans = VLAN.objects.filter(tenant=tenant).select_related("site").order_by("site__name", "vid")
    if site is not None:
        vlans = vlans.filter(Q(site=site) | Q(site=None))
    for vlan in vlans[:EXPORT_LIMIT]:
        if policy.has_perm(user, "networks.vlan.view", vlan):
            yield {
                "site": vlan.site.name if vlan.site else "",
                "vid": vlan.vid,
                "name": vlan.name,
                "status": vlan.status,
                "description": vlan.description,
            }


def import_vlan(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Create or update a VLAN by site and VID."""
    site = None
    if row.get("site"):
        site = Site.objects.filter(tenant=tenant, name__iexact=row["site"]).first()
        if site is None:
            raise ServiceError(_("Site %(name)s not found.") % {"name": row["site"]})
    vid = _int(row, "vid")
    if vid is None:
        raise ServiceError(_("vid must be a number."))
    values = _fields(row, VLAN_FIELDS)
    vlan = VLAN.objects.filter(tenant=tenant, site=site, vid=vid).first()
    if vlan is None:
        networks.create_vlan(request, actor, tenant, site=site, vid=vid, **values)
        return "created"
    changes = _changed(vlan, values)
    if not changes:
        return "unchanged"
    networks.update_vlan(request, actor, vlan, **changes)
    return "updated"


# ---------------------------------------------------------------- cables

CABLE_FIELDS: dict[str, Callable[[Row, str], Any]] = {
    "kind": lambda r, k: _choice(r, k, Cable.Kind.choices),
    "media": lambda r, k: _choice(r, k, Cable.Media.choices),
    "colour": _text,
    "length_m": _float,
    "label": _text,
    "status": lambda r, k: _choice(r, k, Cable.Status.choices),
}


def export_cables(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """Cables with both ends."""
    cables = _scoped(user, "cabling.cable.view", Cable.objects.filter(tenant=tenant), site)
    for cable in cables.prefetch_related("ends__port__device").order_by("pk")[:EXPORT_LIMIT]:
        ends = {e.side: e.port for e in cable.ends.all()}
        if len(ends) != 2:
            continue
        yield {
            "device_a": ends["a"].device.name,
            "port_a": ends["a"].name,
            "device_b": ends["b"].device.name,
            "port_b": ends["b"].name,
            "kind": cable.kind,
            "media": cable.media,
            "colour": cable.colour,
            "length_m": cable.length_m,
            "label": cable.label,
            "status": cable.status,
        }


def _port(tenant: Tenant, row: Row, side: str) -> Port:
    port = (
        Port.objects.filter(
            tenant=tenant,
            device__name__iexact=row[f"device_{side}"],
            name__iexact=row[f"port_{side}"],
        )
        .select_related("device")
        .first()
    )
    if port is None:
        raise ServiceError(
            _("Port %(d)s %(p)s not found.")
            % {"d": row[f"device_{side}"], "p": row[f"port_{side}"]}
        )
    return port


def import_cable(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Connect two ports, or update the cable between them."""
    a, b = _port(tenant, row, "a"), _port(tenant, row, "b")
    values = _fields(row, CABLE_FIELDS)
    end = CableEnd.objects.filter(port=a).select_related("cable").first()
    if end is not None and CableEnd.objects.filter(cable=end.cable, port=b).exists():
        changes = _changed(end.cable, values)
        if not changes:
            return "unchanged"
        cabling.update(request, actor, end.cable, **changes)
        return "updated"
    cabling.connect(request, actor, a, b, **values)
    return "created"


# ---------------------------------------------------------------- ports (export only)


def export_ports(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """Ports with VLAN and connected peer."""
    allowed = _scoped(user, "dcim.device.view", Device.objects.filter(tenant=tenant), site)
    ports = (
        Port.objects.filter(device__in=allowed)
        .select_related("device", "untagged_vlan")
        .order_by("device__name", "order")
    )
    peers = {e.port_id: e.cable_id for e in CableEnd.objects.filter(port__in=ports)}
    other: dict[Any, list[Port]] = {}
    for end in CableEnd.objects.filter(cable_id__in=set(peers.values())).select_related(
        "port__device"
    ):
        other.setdefault(end.cable_id, []).append(end.port)
    for port in ports[:EXPORT_LIMIT]:
        peer = next((p for p in other.get(peers.get(port.pk), []) if p.pk != port.pk), None)
        peer_visible = peer is not None and policy.has_perm(user, "dcim.device.view", peer.device)
        peer_device = (
            peer.device.name if peer is not None and peer_visible else ("…" if peer else "")
        )
        yield {
            "device": port.device.name,
            "name": port.name,
            "kind": port.kind,
            "connector": port.connector,
            "speed_mbps": port.speed_mbps,
            "enabled": port.enabled,
            "label": port.label,
            "mac_address": port.mac_address,
            "vlan_mode": port.vlan_mode,
            "untagged_vlan": port.untagged_vlan.vid if port.untagged_vlan else "",
            "peer_device": peer_device,
            "peer_port": peer.name if peer is not None and peer_visible else "",
        }


TABLES = {
    t.kind: t
    for t in (
        Table(
            "locations",
            gettext_lazy("Locations"),
            ["site", "site_code", "building", "floor", "floor_level", "room", "room_kind"],
            ["site"],
            export_locations,
            import_location,
        ),
        Table(
            "racks",
            gettext_lazy("Racks"),
            [*LOCATION, "name", "u_height", "status", "serial", "asset_tag"],
            [*LOCATION, "name"],
            export_racks,
            import_rack,
        ),
        Table(
            "devices",
            gettext_lazy("Devices"),
            [
                *LOCATION,
                "rack",
                "position",
                "face",
                "name",
                "vendor",
                "model",
                "status",
                "serial",
                "asset_tag",
            ],
            [*LOCATION, "name", "vendor", "model"],
            export_devices,
            import_device,
        ),
        Table(
            "vlans",
            gettext_lazy("VLANs"),
            ["site", "vid", "name", "status", "description"],
            ["vid", "name"],
            export_vlans,
            import_vlan,
        ),
        Table(
            "cables",
            gettext_lazy("Cables"),
            [
                "device_a",
                "port_a",
                "device_b",
                "port_b",
                "kind",
                "media",
                "colour",
                "length_m",
                "label",
                "status",
            ],
            ["device_a", "port_a", "device_b", "port_b"],
            export_cables,
            import_cable,
        ),
        Table(
            "ports",
            gettext_lazy("Ports"),
            [
                "device",
                "name",
                "kind",
                "connector",
                "speed_mbps",
                "enabled",
                "label",
                "mac_address",
                "vlan_mode",
                "untagged_vlan",
                "peer_device",
                "peer_port",
            ],
            [],
            export_ports,
        ),
    )
}
