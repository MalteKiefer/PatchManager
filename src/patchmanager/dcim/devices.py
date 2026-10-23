"""Device and port lifecycle: create from a type, place in racks, edit ports."""

import math
from typing import Any

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.library.models import DeviceType, PortTemplate, expand
from patchmanager.locations.models import Room


def suggest_name(device_type: DeviceType, tenant_id: Any) -> str:
    """Next free name from the type's naming convention (empty if it has none)."""
    if not device_type.name_pattern:
        return ""
    taken = {
        n.lower() for n in Device.objects.filter(tenant_id=tenant_id).values_list("name", flat=True)
    }
    for number in range(1, 10000):
        candidate = expand(device_type.name_pattern, number)
        if candidate.lower() not in taken:
            return candidate
    return ""


def _ports_from(device: Device, templates: list[PortTemplate]) -> None:
    """Create ports in template order; map front ports onto rear port positions."""
    created: dict[Any, list[Port]] = {}
    by_name: dict[str, Port] = {}
    order = 0
    for template in sorted(templates, key=lambda t: (t.kind != "rear", t.order)):
        ports = []
        rears = created.get(template.rear_template_id, []) if template.kind == "front" else []
        per_rear = math.ceil(template.count / len(rears)) if rears else 0
        for index, name in enumerate(template.names()):
            port = Port(
                device=device,
                name=name,
                kind=template.kind,
                connector=template.connector,
                speed_mbps=template.speed_mbps,
                positions=template.positions if template.kind == "rear" else 1,
                order=order,
            )
            if template.kind == "front" and template.rear_port_name:
                port.rear_port = by_name.get(template.rear_port_name)
                port.rear_position = template.rear_position or 1 if port.rear_port else None
            elif rears:
                port.rear_port = rears[index // per_rear]
                port.rear_position = index % per_rear + 1
            port.save()
            ports.append(port)
            by_name[port.name] = port
            order += 1
        created[template.pk] = ports


def _save(device: Device) -> None:
    try:
        device.full_clean(exclude=["path", "tenant", "room", "rack"])
    except ValidationError as exc:
        if any("unique" in m for m in exc.messages):
            raise ServiceError(_("A device with this name already exists in this tenant.")) from exc
        raise ServiceError("; ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            device.save()
    except IntegrityError as exc:
        raise ServiceError(_("A device with this name already exists in this tenant.")) from exc
    except DatabaseError as exc:
        raise ServiceError(_("This device type cannot be used here.")) from exc


def _mount(device: Device, rack: Rack, position: int, face: str) -> None:
    """Take the units for a rack mounted device."""
    size = device.device_type.u_height
    space = RackSpace.objects.filter(device=device).first() or RackSpace(
        device=device, rack=rack, label=device.name
    )
    space.rack, space.label = rack, device.name
    space.place(position, size, RackSpace.Face.BOTH if device.device_type.full_depth else face)
    if position < 1 or space.end > rack.u_height:
        raise ServiceError(_("The device does not fit at this position."))
    try:
        with transaction.atomic():
            space.save()
    except DatabaseError as exc:
        raise ServiceError(_("These units are already taken.")) from exc


def create(
    request: HttpRequest | None,
    actor: User,
    *,
    device_type: DeviceType,
    room: Room,
    rack: Rack | None = None,
    position: int | None = None,
    face: str = "front",
    **fields: Any,
) -> Device:
    """Create a device with the type's ports; mount it if a rack and position are given."""
    parent = rack or room
    require(actor, "dcim.device.add", parent)
    if device_type.tenant_id and device_type.tenant_id != room.tenant_id:
        raise ServiceError(_("This device type is private to another tenant."))
    if rack is not None and (device_type.form != DeviceType.Form.RACK or position is None):
        raise ServiceError(_("Rack devices need a rack mounted type and a position."))
    with transaction.atomic():
        device = Device(device_type=device_type, room=room, rack=rack, created_by=actor, **fields)
        if not device.name:
            device.name = suggest_name(device_type, room.tenant_id) or str(device_type)
        _save(device)
        if rack is not None and position is not None:
            _mount(device, rack, position, face)
        _ports_from(device, list(device_type.port_templates.all()))
    audit.record(
        "dcim.device.created",
        actor=actor,
        request=request,
        obj=device,
        data={"type": str(device_type), "rack": str(rack) if rack else None, "position": position},
    )
    return device


def update(request: HttpRequest, actor: User, device: Device, **fields: Any) -> Device:
    """Change device attributes (not its placement)."""
    stored = Device.objects.get(pk=device.pk)
    require(actor, "dcim.device.change", stored)
    for key, value in fields.items():
        setattr(stored, key, value)
    require(actor, "dcim.device.change", stored)
    _save(stored)
    RackSpace.objects.filter(device=stored).update(label=stored.name)
    audit.record(
        "dcim.device.changed", request=request, obj=stored, data={"fields": sorted(fields)}
    )
    return stored


def place(
    request: HttpRequest,
    actor: User,
    device: Device,
    *,
    room: Room,
    rack: Rack | None,
    position: int | None,
    face: str = "front",
) -> Device:
    """Move a device to another room or rack position (or out of a rack)."""
    require(actor, "dcim.device.change", device)
    require(actor, "dcim.device.add", rack or room)
    if rack is not None and (device.device_type.form != DeviceType.Form.RACK or position is None):
        raise ServiceError(_("Rack devices need a rack mounted type and a position."))
    if rack is not None and rack.room_id != room.pk:
        raise ServiceError(_("The rack is not in this room."))
    from patchmanager.dcim.services import check_site_bound
    from patchmanager.locations import services as locations

    site = room.floor.building.site_id
    if site != device.room.floor.building.site_id:
        check_site_bound([device], site)
    old_path = str(device.path)
    with transaction.atomic():
        RackSpace.objects.filter(device=device).delete()
        if rack is not None or room.floor_id != device.room.floor_id:
            device.plan_x = device.plan_y = None  # position was on the old floor plan
        device.room, device.rack = room, rack
        _save(device)
        if rack is not None and position is not None:
            _mount(device, rack, position, face)
        follow(device)
        # Roles granted or denied on the device keep covering it at its new place.
        locations.rebase_assignments(old_path, str(device.path))
    if old_path != str(device.path):
        policy.bump_version()
    audit.record(
        "dcim.device.moved",
        request=request,
        obj=device,
        data={"room": str(room), "rack": str(rack) if rack else None, "position": position},
    )
    return device


def follow(device: Device) -> None:
    """Re-scope what hangs on ``device`` (cables, addresses, configurations) after it moved."""
    from patchmanager.cabling.services import refresh_paths
    from patchmanager.configs.models import DeviceConfig
    from patchmanager.ipam import services as ipam

    refresh_paths(device)
    ipam.refresh_paths(device)
    DeviceConfig.objects.filter(device=device).update(path=str(device.path))


def delete(request: HttpRequest, actor: User, device: Device) -> None:
    """Delete a device with its ports and rack space."""
    require(actor, "dcim.device.delete", device)
    audit.record("dcim.device.deleted", request=request, obj=device, data={"name": device.name})
    from patchmanager.metadata import services as metadata

    with transaction.atomic():
        metadata.purge(device)
        for port in device.ports.all():
            metadata.purge(port)
        device.delete()


PORT_FIELDS = (
    "name",
    "kind",
    "connector",
    "speed_mbps",
    "enabled",
    "label",
    "description",
    "mac_address",
    "lag",
    "lag_protocol",
    "mtu",
    "tunneled",
    "stp_edge",
    "stp_bpdu_guard",
    "stp_root_guard",
    "ospf_area",
    "ospf_passive",
)


def add_port(request: HttpRequest, actor: User, device: Device, **fields: Any) -> Port:
    """Add a port that the type does not define."""
    require(actor, "dcim.device.change", device)
    port = Port(
        device=device,
        order=device.ports.count(),
        **{k: v for k, v in fields.items() if k in PORT_FIELDS},
    )
    _save_port(port)
    audit.record("dcim.port.created", request=request, obj=device, data={"port": port.name})
    return port


def update_port(request: HttpRequest, actor: User, port: Port, **fields: Any) -> Port:
    """Document a port."""
    require(actor, "dcim.device.change", port.device)
    rear = fields.pop("rear_port", port.rear_port)
    if rear is not None and rear.device_id != port.device_id:
        raise ServiceError(_("The rear port must belong to the same device."))
    lag = fields.get("lag")
    if lag is not None and (lag.device_id != port.device_id or lag.kind != "lag" or lag == port):
        raise ServiceError(_("A port joins only a link aggregation of its own device."))
    for key, value in fields.items():
        if key in PORT_FIELDS or key == "rear_position":
            setattr(port, key, value)
    port.rear_port = rear
    _save_port(port)
    audit.record("dcim.port.changed", request=request, obj=port.device, data={"port": port.name})
    return port


def delete_port(request: HttpRequest, actor: User, port: Port) -> None:
    """Remove a port."""
    require(actor, "dcim.device.change", port.device)
    audit.record("dcim.port.deleted", request=request, obj=port.device, data={"port": port.name})
    from patchmanager.metadata import services as metadata

    with transaction.atomic():
        metadata.purge(port)
        port.delete()


def _save_port(port: Port) -> None:
    try:
        port.full_clean(exclude=["tenant", "device", "rear_port"])
    except ValidationError as exc:
        if any("unique" in m.lower() or "rear" in m.lower() for m in exc.messages):
            raise ServiceError(_("This port name or rear position is already used.")) from exc
        raise ServiceError("; ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            port.save()
    except DatabaseError as exc:
        raise ServiceError(_("This port name or rear position is already used.")) from exc
