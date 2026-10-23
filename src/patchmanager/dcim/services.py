"""Rack space: reserve, move and free units."""

from typing import Any

from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.locations.models import Room


def _store(space: RackSpace) -> None:
    rack = space.rack
    if space.start < 1 or space.end > rack.u_height or space.size < 1:
        raise ServiceError(
            _("The units must lie between 1 and %(height)s.") % {"height": rack.u_height}
        )
    try:
        with transaction.atomic():
            space.save()
    except DatabaseError as exc:  # exclusion constraint or rack trigger
        raise ServiceError(_("These units are already taken.")) from exc


def reserve(
    request: HttpRequest,
    actor: User,
    rack: Rack,
    *,
    start: int,
    size: int,
    face: str,
    label: str,
    note: str = "",
) -> RackSpace:
    """Reserve units, for example for planned equipment."""
    require(actor, "dcim.rack.change", rack)
    space = RackSpace(rack=rack, label=label, note=note, created_by=actor)
    space.place(start, size, face)
    _store(space)
    audit.record(
        "dcim.rackspace.created",
        request=request,
        obj=rack,
        data={"label": label, "units": [space.start, space.end], "face": face},
    )
    return space


def move(
    request: HttpRequest, actor: User, space: RackSpace, *, start: int, face: str | None = None
) -> RackSpace:
    """Move a block to another start unit (and optionally another face)."""
    require(actor, "dcim.rack.change", space.rack)
    if space.device is not None:
        require(actor, "dcim.device.change", space.device)
        if space.device.device_type.full_depth:
            face = RackSpace.Face.BOTH
    before = [space.start, space.end, space.face]
    space.place(start, space.size, face)
    _store(space)
    audit.record(
        "dcim.rackspace.moved",
        request=request,
        obj=space.rack,
        data={"label": space.label, "from": before, "to": [space.start, space.end, space.face]},
    )
    return space


def release(request: HttpRequest, actor: User, space: RackSpace) -> None:
    """Free reserved units (device space goes with the device)."""
    require(actor, "dcim.rack.change", space.rack)
    if space.device_id:
        raise ServiceError(_("Move or delete the device instead."))
    audit.record(
        "dcim.rackspace.deleted", request=request, obj=space.rack, data={"label": space.label}
    )
    space.delete()


def relocate(request: HttpRequest, actor: User, rack: Rack, room: Room) -> Rack:
    """Move a rack with everything mounted in it to another room (also on another site)."""
    from patchmanager.dcim import devices as device_services
    from patchmanager.locations import services as locations

    stored = Rack.objects.select_related("room").get(pk=rack.pk)
    require(actor, "dcim.rack.change", stored)
    require(actor, "dcim.rack.add", room)
    if room.pk == stored.room_id:
        return stored
    if room.tenant_id != stored.tenant_id:
        raise ServiceError(_("The room belongs to another tenant."))
    mounted = list(Device.objects.filter(rack=stored))
    for device in mounted:
        require(actor, "dcim.device.change", device)
    if mounted:  # the devices arrive in the room, as when placing them one by one
        require(actor, "dcim.device.add", room)
    site = room.floor.building.site_id
    if site != stored.room.floor.building.site_id:
        _check_site_bound(mounted, site)
    before, old_path = str(stored.room), str(stored.path)
    try:
        with transaction.atomic():
            stored.room = room
            stored.plan_x = stored.plan_y = None  # the position was on the old floor plan
            stored.save()
            for device in mounted:
                device.room, device.rack = room, stored
                device.plan_x = device.plan_y = None
                device.save()
                device_services.follow(device)
            # Roles granted or denied on the rack (or a device in it) keep covering it.
            locations.rebase_assignments(old_path, str(stored.path))
    except IntegrityError as exc:
        raise ServiceError(_("A rack with this name already exists in that room.")) from exc
    except DatabaseError as exc:  # integrity triggers of devices and racks
        raise ServiceError(_("This change conflicts with existing data.")) from exc
    policy.bump_version()
    audit.record(
        "dcim.rack.moved",
        request=request,
        obj=stored,
        data={"from": before, "to": str(room), "devices": len(mounted)},
    )
    return stored


def _check_site_bound(devices: list[Device], site: Any) -> None:
    """Refuse a move to another site while VLANs or stacks of the old site are in use.

    The database only checks these when ports or members are written, so a moved rack
    would otherwise keep them and fail on the next port edit.
    """
    from patchmanager.networks.models import StackMember, TaggedVLAN

    foreign = Port.objects.filter(device__in=devices, untagged_vlan__site__isnull=False).exclude(
        untagged_vlan__site_id=site
    )
    tagged = TaggedVLAN.objects.filter(port__device__in=devices, vlan__site__isnull=False).exclude(
        vlan__site_id=site
    )
    if foreign.exists() or tagged.exists():
        raise ServiceError(_("Ports in this rack use VLANs of the old site; change them first."))
    if StackMember.objects.filter(device__in=devices).exclude(stack__site_id=site).exists():
        raise ServiceError(
            _("Switches in this rack belong to a stack of the old site; take them out first.")
        )
