"""Connect and disconnect ports, edit cables."""

from typing import Any

from django.core.exceptions import ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpRequest
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.cabling.models import Cable, CableEnd
from patchmanager.dcim.models import Port

CABLE_FIELDS = ("kind", "media", "colour", "length_m", "label", "status", "notes")


def common_path(a: str, b: str) -> str:
    """Deepest shared ltree prefix (at least the tenant label)."""
    shared = []
    for left, right in zip(a.split("."), b.split("."), strict=False):
        if left != right:
            break
        shared.append(left)
    return ".".join(shared)


def scope(a: Port, b: Port) -> str:
    """Human readable reach of a cable: rack, room, floor, building, site or between sites."""
    depth = len(common_path(str(a.device.path), str(b.device.path)).split("."))
    if a.device_id == b.device_id:
        return _("within the device")
    labels = {
        6: _("within the rack"),
        5: _("within the room"),
        4: _("within the floor"),
        3: _("within the building"),
        2: _("within the site"),
    }
    if depth >= 6 and a.device.rack_id and a.device.rack_id == b.device.rack_id:
        return labels[6]
    return labels.get(min(depth, 5), _("between sites"))


def _require_endpoint(actor: User, port: Port) -> None:
    if not policy.has_perm(actor, "cabling.cable.add", port.device):
        raise PermissionDeniedError(_("You do not have permission to do that."))


def connect(request: HttpRequest, actor: User, a: Port, b: Port, **fields: Any) -> Cable:
    """Create a cable between two free ports of the same tenant."""
    if a.pk == b.pk:
        raise ServiceError(_("A cable needs two different ports."))
    if a.tenant_id != b.tenant_id:
        raise ServiceError(_("Ports of different tenants cannot be connected."))
    _require_endpoint(actor, a)
    _require_endpoint(actor, b)
    from patchmanager.instance import editions

    if not editions.current().cross_rack_cabling and (
        a.device.rack_id is None or a.device.rack_id != b.device.rack_id
    ):
        raise ServiceError(_("This edition only documents connections inside one rack."))
    cable = Cable(
        tenant_id=a.tenant_id,
        created_by=actor,
        path=common_path(str(a.device.path), str(b.device.path)),
        **{k: v for k, v in fields.items() if k in CABLE_FIELDS},
    )
    try:
        cable.full_clean(exclude=["tenant", "path"])
    except ValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            cable.save()
            CableEnd.objects.create(tenant_id=cable.tenant_id, cable=cable, side="a", port=a)
            CableEnd.objects.create(tenant_id=cable.tenant_id, cable=cable, side="b", port=b)
    except IntegrityError as exc:
        raise ServiceError(_("One of the ports already has a cable.")) from exc
    except DatabaseError as exc:
        raise ServiceError(_("These ports cannot be connected.")) from exc
    audit.record(
        "cabling.cable.created",
        request=request,
        obj=cable,
        data={"a": f"{a.device}:{a.name}", "b": f"{b.device}:{b.name}", "kind": cable.kind},
    )
    return cable


def update(request: HttpRequest, actor: User, cable: Cable, **fields: Any) -> Cable:
    """Change cable attributes (authorised on the stored row and the result)."""
    stored = Cable.objects.get(pk=cable.pk)
    require(actor, "cabling.cable.change", stored)
    for key, value in fields.items():
        if key in CABLE_FIELDS:
            setattr(stored, key, value)
    require(actor, "cabling.cable.change", stored)
    try:
        stored.full_clean(exclude=["tenant", "path"])
    except ValidationError as exc:
        raise ServiceError("; ".join(exc.messages)) from exc
    stored.save()
    audit.record(
        "cabling.cable.changed", request=request, obj=stored, data={"fields": sorted(fields)}
    )
    return stored


def disconnect(request: HttpRequest, actor: User, cable: Cable) -> None:
    """Remove a cable."""
    require(actor, "cabling.cable.delete", cable)
    ends = [f"{e.port.device}:{e.port.name}" for e in cable.ends.select_related("port__device")]
    audit.record("cabling.cable.deleted", request=request, obj=cable, data={"ends": ends})
    cable.delete()


def refresh_paths(device: Any) -> int:
    """Recompute the scope of every cable on ``device`` after it moved."""
    count = 0
    for end in CableEnd.objects.filter(port__device=device).select_related("cable"):
        ports = [e.port for e in end.cable.ends.select_related("port__device")]
        if len(ports) == 2:
            path = common_path(str(ports[0].device.path), str(ports[1].device.path))
            if path != str(end.cable.path):
                Cable.objects.filter(pk=end.cable_id).update(path=path)
                count += 1
    return count
