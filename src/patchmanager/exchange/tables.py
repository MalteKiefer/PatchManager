"""Table kinds: their columns, export rows and import handlers.

Export and import use the same columns, so an exported file can be edited and
imported again. Import handlers go through the domain services, so permissions,
validation and audit apply to every row.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
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
from patchmanager.ipam import services as ipam
from patchmanager.ipam.models import VRF, IPAddress, Prefix
from patchmanager.library.models import DeviceType
from patchmanager.locations import services as tree
from patchmanager.locations.models import Building, Floor, Room, RoomType, Site
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
    object_type: str  # "<type>.export" and "<type>.import" are required
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


def _seen[O](actor: Any, permission: str, obj: O, name: str) -> O:
    """Objects the actor may not view are reported like missing ones."""
    if obj is not None and not policy.has_perm(actor, permission, obj):
        raise ServiceError(_("%(name)s not found.") % {"name": name})
    return obj


def _changed(obj: Any, values: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if getattr(obj, k) != v}


def location(request: HttpRequest, actor: User, tenant: Tenant, row: Row, *, create: bool) -> Any:
    """The deepest location named in the row; with ``create`` missing levels are added."""
    parent: Any = tenant
    node: Any = None
    named = " / ".join(row[level] for level in LOCATION if row.get(level))
    missing = ServiceError(_("Location %(path)s not found.") % {"path": named})
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
                raise missing
            if parent is not tenant and not policy.has_perm(
                actor, f"{parent.policy_object_type}.view", parent
            ):
                raise missing  # never create below a location the actor cannot see
            extra: dict[str, Any] = {}
            if model is Site and row.get("site_code"):
                extra["code"] = row["site_code"]
            if model is Floor:
                extra["level"] = _int(row, "floor_level") or 0
            if model is Room and row.get("room_kind"):
                extra["kind_id"] = _choice(
                    row, "room_kind", [(t.key, t.label) for t in RoomType.objects.all()]
                )
            node = tree.create(request, actor, model, parent=parent, name=name, **extra)
        parent = node
    if node is None:
        raise ServiceError(_("The site is missing."))
    # Ancestors are shown as context of visible objects; the named location itself must be visible.
    if not policy.has_perm(actor, f"{node.policy_object_type}.view", node):
        raise missing
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
    rooms = _scoped(user, "locations.room.export", Room.objects.filter(tenant=tenant), site)
    seen_floors = set()
    for room in rooms.select_related("floor__building__site").order_by("path")[:EXPORT_LIMIT]:
        seen_floors.add(room.floor_id)
        yield {
            **_names(room),
            "site_code": room.floor.building.site.code,
            "floor_level": room.floor.level,
            "room_kind": room.kind_id,
        }
    floors = _scoped(user, "locations.floor.export", Floor.objects.filter(tenant=tenant), site)
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
    racks = _scoped(user, "dcim.rack.export", Rack.objects.filter(tenant=tenant), site)
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
    rack = _seen(
        actor,
        "dcim.rack.view",
        Rack.objects.filter(room=room, name__iexact=row["name"]).first(),
        row["name"],
    )
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
    found = _scoped(user, "dcim.device.export", Device.objects.filter(tenant=tenant), site)
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
        rack = _seen(
            actor,
            "dcim.rack.view",
            Rack.objects.filter(room=room, name__iexact=row["rack"]).first(),
            f"Rack {row['rack']}",
        )
        if rack is None:
            raise ServiceError(_("Rack %(name)s not found.") % {"name": row["rack"]})
    position = _int(row, "position")
    face = _choice(row, "face", RackSpace.Face.choices)
    values = _fields(row, DEVICE_FIELDS)
    device = _seen(
        actor,
        "dcim.device.view",
        Device.objects.filter(tenant=tenant, name__iexact=row["name"]).first(),
        row["name"],
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
        if policy.has_perm(user, "networks.vlan.export", vlan):
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
        site = _seen(
            actor,
            "locations.site.view",
            Site.objects.filter(tenant=tenant, name__iexact=row["site"]).first(),
            f"Site {row['site']}",
        )
        if site is None:
            raise ServiceError(_("Site %(name)s not found.") % {"name": row["site"]})
    vid = _int(row, "vid")
    if vid is None:
        raise ServiceError(_("vid must be a number."))
    values = _fields(row, VLAN_FIELDS)
    vlan = _seen(
        actor,
        "networks.vlan.view",
        VLAN.objects.filter(tenant=tenant, site=site, vid=vid).first(),
        f"VLAN {vid}",
    )
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
    """Cables with both ends (ends on devices the user may not view are masked)."""
    cables = _scoped(user, "cabling.cable.export", Cable.objects.filter(tenant=tenant), site)
    for cable in cables.prefetch_related("ends__port__device").order_by("pk")[:EXPORT_LIMIT]:
        ends = {e.side: e.port for e in cable.ends.all()}
        if len(ends) != 2:
            continue
        row: dict[str, Any] = {key: getattr(cable, key) for key in CABLE_FIELDS}
        for side, port in ends.items():
            shown = policy.has_perm(user, "dcim.device.view", port.device)
            row[f"device_{side}"] = port.device.name if shown else "…"
            row[f"port_{side}"] = port.name if shown else ""
        yield row


def _port(actor: User, tenant: Tenant, row: Row, side: str) -> Port:
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
    _seen(
        actor,
        "dcim.device.view",
        port.device,
        f"Port {row[f'device_{side}']} {row[f'port_{side}']}",
    )
    return port


def import_cable(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Connect two ports, or update the cable between them."""
    a, b = _port(actor, tenant, row, "a"), _port(actor, tenant, row, "b")
    values = _fields(row, CABLE_FIELDS)
    end = CableEnd.objects.filter(port=a).select_related("cable").first()
    if end is not None and CableEnd.objects.filter(cable=end.cable, port=b).exists():
        _seen(actor, "cabling.cable.view", end.cable, f"Port {a.device} {a.name}")
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
    allowed = _scoped(user, "dcim.device.export", Device.objects.filter(tenant=tenant), site)
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


# ---------------------------------------------------------------- IP address management


def _vrf(actor: Any, tenant: Tenant, row: Row) -> VRF | None:
    name = row.get("vrf", "")
    if not name:
        return None
    vrf = _seen(
        actor, "ipam.vrf.view", VRF.objects.filter(tenant=tenant, name__iexact=name).first(), name
    )
    if vrf is None:
        raise ServiceError(_("VRF %(name)s not found.") % {"name": name})
    return vrf


PREFIX_FIELDS: dict[str, Callable[[Row, str], Any]] = {
    "status": lambda r, k: _choice(r, k, Prefix.Status.choices),
    "gateway": _text,
    "description": _text,
}


def export_prefixes(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """Prefixes of the tenant (and site)."""
    rows = Prefix.objects.filter(tenant=tenant).select_related("vrf", "site", "vlan")
    if site is not None:
        rows = rows.filter(site=site)
    for prefix in rows.order_by("vrf__name", "prefix")[:EXPORT_LIMIT]:
        if policy.has_perm(user, "ipam.prefix.export", prefix):
            yield {
                "vrf": prefix.vrf.name if prefix.vrf else "",
                "prefix": prefix.prefix,
                "status": prefix.status,
                "site": prefix.site.name if prefix.site else "",
                "vlan": prefix.vlan.vid if prefix.vlan else "",
                "gateway": prefix.gateway or "",
                "description": prefix.description,
            }


def import_prefix(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Create or update a prefix by VRF and network."""
    vrf = _vrf(actor, tenant, row)
    values = _fields(row, PREFIX_FIELDS)
    refs: dict[str, Any] = {}
    if row.get("site"):
        refs["site"] = _seen(
            actor,
            "locations.site.view",
            Site.objects.filter(tenant=tenant, name__iexact=row["site"]).first(),
            f"Site {row['site']}",
        )
        if refs["site"] is None:
            raise ServiceError(_("Site %(name)s not found.") % {"name": row["site"]})
    if row.get("vlan"):
        vid = _int(row, "vlan") or 0
        vlans = VLAN.objects.filter(tenant=tenant, vid=vid)
        site = refs.get("site")
        refs["vlan"] = _seen(
            actor,
            "networks.vlan.view",
            (vlans.filter(site=site).first() if site else None) or vlans.filter(site=None).first(),
            f"VLAN {vid}",
        )
        if refs["vlan"] is None:
            raise ServiceError(_("VLAN %(vid)s not found.") % {"vid": vid})
    try:
        network = Prefix._meta.get_field("prefix").to_python(row["prefix"])
    except DjangoValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    prefix = _seen(
        actor,
        "ipam.prefix.view",
        Prefix.objects.filter(tenant=tenant, vrf=vrf, prefix=network).first(),
        network,
    )
    if prefix is None:
        ipam.create_prefix(request, actor, tenant, vrf=vrf, prefix=network, **refs, **values)
        return "created"
    changes = _changed(prefix, {**values, **refs})
    if not changes:
        return "unchanged"
    ipam.update_prefix(request, actor, prefix, **changes)
    return "updated"


ADDRESS_FIELDS: dict[str, Callable[[Row, str], Any]] = {
    "status": lambda r, k: _choice(r, k, IPAddress.Status.choices),
    "dns_name": _text,
    "description": _text,
}


def export_addresses(user: Any, tenant: Tenant, site: Site | None) -> Iterator[dict[str, Any]]:
    """Addresses of the tenant with the port they are on."""
    rows = IPAddress.objects.filter(tenant=tenant).select_related("vrf", "port__device")
    if site is not None:
        rows = rows.filter(path__descendant_of=str(site.path))
    for address in rows.order_by("vrf__name", "address")[:EXPORT_LIMIT]:
        if not policy.has_perm(user, "ipam.ipaddress.export", address):
            continue
        port = address.port
        shown = port is not None and policy.has_perm(user, "dcim.device.view", port.device)
        yield {
            "vrf": address.vrf.name if address.vrf else "",
            "address": address.address,
            "status": address.status,
            "dns_name": address.dns_name,
            "description": address.description,
            "device": port.device.name if shown and port else ("…" if port else ""),
            "port": port.name if shown and port else "",
        }


def import_address(request: HttpRequest, actor: User, tenant: Tenant, row: Row) -> str:
    """Create or update an address by VRF and host; device and port assign it."""
    vrf = _vrf(actor, tenant, row)
    values = _fields(row, ADDRESS_FIELDS)
    try:
        value = IPAddress._meta.get_field("address").to_python(row["address"])
    except DjangoValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    if row.get("device"):
        device = _seen(
            actor,
            "dcim.device.view",
            Device.objects.filter(tenant=tenant, name__iexact=row["device"]).first(),
            row["device"],
        )
        port = device.ports.filter(name__iexact=row.get("port", "")).first() if device else None
        if port is None:
            raise ServiceError(
                _("Port %(port)s of %(device)s not found.")
                % {"port": row.get("port", ""), "device": row["device"]}
            )
        values["port"] = port
    address = _seen(
        actor,
        "ipam.ipaddress.view",
        IPAddress.objects.filter(tenant=tenant, vrf=vrf, host=value.split("/")[0]).first(),
        value,
    )
    if address is None:
        ipam.create_address(request, actor, tenant, vrf=vrf, address=value, **values)
        return "created"
    changes = _changed(address, {"address": value, **values})
    if not changes:
        return "unchanged"
    ipam.update_address(request, actor, address, **changes)
    return "updated"


TABLES = {
    t.kind: t
    for t in (
        Table(
            "locations",
            gettext_lazy("Locations"),
            "locations.site",
            ["site", "site_code", "building", "floor", "floor_level", "room", "room_kind"],
            ["site"],
            export_locations,
            import_location,
        ),
        Table(
            "racks",
            gettext_lazy("Racks"),
            "dcim.rack",
            [*LOCATION, "name", "u_height", "status", "serial", "asset_tag"],
            [*LOCATION, "name"],
            export_racks,
            import_rack,
        ),
        Table(
            "devices",
            gettext_lazy("Devices"),
            "dcim.device",
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
            "networks.vlan",
            ["site", "vid", "name", "status", "description"],
            ["vid", "name"],
            export_vlans,
            import_vlan,
        ),
        Table(
            "prefixes",
            gettext_lazy("Prefixes"),
            "ipam.prefix",
            ["vrf", "prefix", "status", "site", "vlan", "gateway", "description"],
            ["prefix"],
            export_prefixes,
            import_prefix,
        ),
        Table(
            "addresses",
            gettext_lazy("IP addresses"),
            "ipam.ipaddress",
            ["vrf", "address", "status", "dns_name", "description", "device", "port"],
            ["address"],
            export_addresses,
            import_address,
        ),
        Table(
            "cables",
            gettext_lazy("Cables"),
            "cabling.cable",
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
            "dcim.device",
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
