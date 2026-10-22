"""What a room should have against what it has, and where its devices are patched."""

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Count
from django.http import HttpRequest

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services.users import require
from patchmanager.audit import services as audit
from patchmanager.cabling.trace import trace_many
from patchmanager.dcim.models import Device, Port
from patchmanager.library.models import DeviceCategory
from patchmanager.locations.models import Room, RoomNeed


@dataclass(frozen=True)
class PlanRow:
    """One category of a room: planned, installed and how far apart."""

    category: DeviceCategory
    planned: int | None  # None: nothing planned
    installed: int
    own: bool  # the room sets its own number

    @property
    def missing(self) -> int:
        """Devices still to install."""
        return max((self.planned or 0) - self.installed, 0)

    @property
    def status(self) -> str:
        """ "missing", "done", "extra" (more than planned) or "unplanned"."""
        if self.planned is None:
            return "unplanned"
        if self.installed < self.planned:
            return "missing"
        return "extra" if self.installed > self.planned else "done"


def plan(user: Any, room: Room) -> list[PlanRow]:
    """Planned against installed per category (installed counts devices the user may see)."""
    planned = {n.category_id: n.count for n in room.kind.needs.all()}
    own = {n.category_id: n.count for n in room.needs.all()}
    planned.update(own)
    devices = policy.scope_queryset(user, "dcim.device.view", Device.objects.filter(room=room))
    installed: dict[str, int] = {
        str(row["device_type__category"]): row["n"]
        for row in devices.values("device_type__category").annotate(n=Count("pk"))
    }
    keys = {k for k, v in planned.items() if v} | set(installed)
    categories = DeviceCategory.objects.filter(key__in=keys)
    rows = [
        PlanRow(c, planned.get(c.key), installed.get(c.key, 0), c.key in own) for c in categories
    ]
    return sorted(rows, key=lambda r: (r.status == "unplanned", r.category.order))


def set_needs(request: HttpRequest, actor: User, room: Room, counts: dict[str, int | None]) -> None:
    """Replace the room's own numbers (``None`` falls back to the room type)."""
    require(actor, "locations.room.change", room)
    with transaction.atomic():
        RoomNeed.objects.filter(room=room).delete()
        RoomNeed.objects.bulk_create(
            RoomNeed(tenant_id=room.tenant_id, room=room, category_id=key, count=count)
            for key, count in counts.items()
            if count is not None
        )
    audit.record(
        "locations.room.planned",
        request=request,
        obj=room,
        data={k: v for k, v in counts.items() if v is not None},
    )


@dataclass(frozen=True)
class Outlet:
    """A port of a device in the room (not in a rack) and where its path ends."""

    port: Port
    far: Port | None  # the other end, if visible
    hops: int
    cabled: bool
    hidden: bool  # the other end exists but is not visible to the user
    far_rack: str = ""


def cabling(user: Any, room: Room) -> list[tuple[Device, list[Outlet]]]:
    """Devices of the room outside racks with each port's patch target."""
    devices = list(
        policy.scope_queryset(
            user, "dcim.device.view", Device.objects.filter(room=room, rack__isnull=True)
        )
        .select_related("device_type__category")
        .order_by("name")
    )
    ports = list(
        Port.objects.filter(device__in=devices).select_related("device").order_by("order", "name")
    )
    cabled = set(
        Port.objects.filter(pk__in=[p.pk for p in ports], cable_end__isnull=False).values_list(
            "pk", flat=True
        )
    )
    traceable = {d.pk for d in devices if policy.has_perm(user, "cabling.cable.trace", d)}
    paths = trace_many([p for p in ports if p.pk in cabled and p.device_id in traceable])
    seen: dict[Any, bool] = {}
    rows: dict[Any, list[Outlet]] = {d.pk: [] for d in devices}
    for port in ports:
        path = paths.get(port.pk)
        far = None
        hidden = False
        if path is not None and len(path.ports) > 1:
            end = path.ports[-1] if path.ports[0].pk == port.pk else path.ports[0]
            if end.device_id not in seen:
                seen[end.device_id] = policy.has_perm(user, "dcim.device.view", end.device)
            far, hidden = (end, False) if seen[end.device_id] else (None, True)
        rows[port.device_id].append(
            Outlet(
                port=port,
                far=far,
                hops=len(path.cables) if path else 0,
                cabled=port.pk in cabled,
                hidden=hidden,
            )
        )
    far_ids = {o.far.device_id for outlets in rows.values() for o in outlets if o.far}
    racks = dict(
        Device.objects.filter(pk__in=far_ids, rack__isnull=False).values_list("pk", "rack__name")
    )
    for outlets in rows.values():
        outlets[:] = [
            Outlet(o.port, o.far, o.hops, o.cabled, o.hidden, racks.get(o.far.device_id, ""))
            if o.far
            else o
            for o in outlets
        ]
    return [(d, rows[d.pk]) for d in devices]
