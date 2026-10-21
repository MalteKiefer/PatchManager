"""Rack space: reserve, move and free units."""

from django.db import DatabaseError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.dcim.models import Rack, RackSpace


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
    """Free the units."""
    require(actor, "dcim.rack.change", space.rack)
    audit.record(
        "dcim.rackspace.deleted", request=request, obj=space.rack, data={"label": space.label}
    )
    space.delete()
