"""Start page: what the current tenant holds, what is full, what changed."""

from typing import Any

from patchmanager.accounts import policy
from patchmanager.cabling.models import Cable
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.history import services as history
from patchmanager.history.models import Change
from patchmanager.history.registry import TRACKED
from patchmanager.locations.models import Site

FULLEST = 6
RECENT = 8


def _visible(user: Any, permission: str, queryset: Any) -> Any:
    return policy.scope_queryset(user, permission, queryset)


def overview(user: Any, tenant: Any) -> dict[str, Any]:
    """Counts, sites, the fullest racks and recent changes the user may see."""
    racks = list(
        _visible(user, "dcim.rack.view", Rack.objects.filter(tenant=tenant)).select_related(
            "room__floor__building"
        )
    )
    used: dict[Any, set[int]] = {r.pk: set() for r in racks}
    for space in RackSpace.objects.filter(rack__in=racks).only("rack_id", "units"):
        used[space.rack_id] |= set(range(space.start, space.start + space.size))
    load = sorted(
        ((len(used[r.pk]) / r.u_height, r) for r in racks), key=lambda x: x[0], reverse=True
    )
    devices = _visible(user, "dcim.device.view", Device.objects.filter(tenant=tenant))
    ports = Port.objects.filter(device__in=devices)
    sites = list(
        _visible(user, "locations.site.view", Site.objects.filter(tenant=tenant)).order_by("name")
    )
    per_site: dict[Any, list[float]] = {}
    for share, rack in load:  # only racks the user may see are counted
        per_site.setdefault(rack.room.floor.building.site_id, []).append(share)
    for site in sites:
        shares = per_site.get(site.pk, [])
        site.rack_count = len(shares)
        site.load = round(100 * sum(shares) / len(shares)) if shares else 0
    changes = _feed(history.visible(user, Change.objects.filter(tenant_id=tenant.pk))[:200])
    return {
        "counts": {
            "sites": len(sites),
            "racks": len(racks),
            "devices": devices.count(),
            "ports": ports.count(),
            "cabled": ports.filter(cable_end__isnull=False).count(),
            "cables": _visible(
                user, "cabling.cable.view", Cable.objects.filter(tenant=tenant)
            ).count(),
        },
        "sites": sites[:12],
        "fullest": [
            {"rack": r, "percent": round(share * 100)} for share, r in load[:FULLEST] if share
        ],
        "changes": changes,
    }


def _feed(changes: Any) -> list[dict[str, Any]]:
    """One entry per action: changes of one request, or a run of alike changes, together."""
    batches: list[list[Change]] = []
    for change in changes:
        last = batches[-1][-1] if batches else None
        same_request = (
            last is not None and change.request_id and change.request_id == last.request_id
        )
        alike = (
            last is not None
            and not change.request_id
            and (change.object_type, change.action, change.actor_id)
            == (last.object_type, last.action, last.actor_id)
        )
        if same_request or alike:
            batches[-1].append(change)
        else:
            batches.append([change])
    feed = []
    for batch in batches[:RECENT]:
        lead = next((c for c in reversed(batch) if c.label), batch[-1])
        entry = TRACKED.get(lead.object_type)
        feed.append(
            {
                "change": lead,
                "kind": entry.model._meta.verbose_name if entry else lead.object_type,
                "more": len(batch) - 1,
            }
        )
    return feed
