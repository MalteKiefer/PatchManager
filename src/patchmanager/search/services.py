"""Global search: trigram matching on names and identifiers, full text on notes."""

from dataclasses import dataclass
from typing import Any

from django.contrib.postgres.search import SearchQuery, SearchVector, TrigramWordSimilarity
from django.db.models import Q, QuerySet
from django.db.models.functions import Greatest
from django.utils.translation import gettext_lazy as _

from patchmanager.accounts import policy
from patchmanager.accounts.services import ServiceError
from patchmanager.cabling.models import Cable
from patchmanager.dcim.models import Device, Port, Rack
from patchmanager.ipam.models import IPAddress, Prefix
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.metadata import services as metadata
from patchmanager.metadata.models import Note
from patchmanager.networks.models import VLAN, WLAN

MAX_QUERY = 200
CANDIDATES = 200  # rows fetched per type before permission checks


@dataclass(frozen=True)
class Source:
    """One searchable object type."""

    kind: str
    title: Any
    model: Any
    permission: str
    fields: tuple[str, ...]
    path_field: str | None = "path"  # None: check each row in Python
    related: tuple[str, ...] = ()
    parent: tuple[str, Any] | None = (
        None  # governed by this relation's object (ports: their device)
    )


SOURCES = {
    s.kind: s
    for s in (
        Source("site", _("Sites"), Site, "locations.site.view", ("name", "code", "address")),
        Source(
            "building",
            _("Buildings"),
            Building,
            "locations.building.view",
            ("name",),
            related=("site",),
        ),
        Source(
            "floor", _("Floors"), Floor, "locations.floor.view", ("name",), related=("building",)
        ),
        Source("room", _("Rooms"), Room, "locations.room.view", ("name",), related=("floor",)),
        Source(
            "rack",
            _("Racks"),
            Rack,
            "dcim.rack.view",
            ("name", "serial", "asset_tag"),
            related=("room",),
        ),
        Source(
            "device",
            _("Devices"),
            Device,
            "dcim.device.view",
            ("name", "serial", "asset_tag"),
            related=("room", "rack", "device_type__vendor"),
        ),
        Source(
            "port",
            _("Ports"),
            Port,
            "dcim.device.view",
            ("name", "label", "description", "mac_address"),
            related=("device",),
            parent=("device", Device),
        ),
        Source("cable", _("Cables"), Cable, "cabling.cable.view", ("label",)),
        Source(
            "vlan",
            _("VLANs"),
            VLAN,
            "networks.vlan.view",
            ("name", "description"),
            path_field=None,
            related=("site",),
        ),
        Source(
            "wlan", _("WLANs"), WLAN, "networks.wlan.view", ("ssid", "description"), path_field=None
        ),
        Source(
            "prefix",
            _("Prefixes"),
            Prefix,
            "ipam.prefix.view",
            ("text", "description"),
            related=("vrf",),
        ),
        Source(
            "ipaddress",
            _("IP addresses"),
            IPAddress,
            "ipam.ipaddress.view",
            ("host", "dns_name", "description"),
            related=("vrf", "port__device"),
        ),
    )
}


@dataclass
class Hit:
    """One result."""

    kind: str
    obj: Any
    context: str = ""


def _matches(source: Source, query: str, tenant: Any) -> QuerySet[Any]:
    text = Q()
    for field in source.fields:
        text |= Q(**{f"{field}__icontains": query}) | Q(**{f"{field}__trigram_word_similar": query})
    if source.kind == "vlan" and query.isascii() and query.isdecimal():
        text |= Q(vid=int(query))
    rows = source.model.objects.filter(text, tenant=tenant)
    if source.related:
        rows = rows.select_related(*source.related)
    score = [TrigramWordSimilarity(query, f) for f in source.fields]
    ranked: QuerySet[Any] = rows.annotate(score=Greatest(*score) if len(score) > 1 else score[0])
    return ranked.order_by("-score", "pk")


def _context(source: Source, obj: Any) -> str:
    """Where the object is, in a few words."""
    if source.kind == "device":
        return " · ".join(str(x) for x in (obj.device_type, obj.room, obj.rack) if x)
    if source.kind == "port":
        return str(obj.device)
    if source.kind == "ipaddress":
        return " · ".join(str(x) for x in (obj.dns_name, obj.port and obj.port.device) if x)
    if source.kind == "prefix":
        return obj.description or (str(obj.vrf) if obj.vrf else "")
    if source.kind in ("building", "floor", "room", "rack", "vlan"):
        parent = getattr(obj, source.related[0])
        return str(parent) if parent else ""
    return ""


def search(
    user: Any, tenant: Any, query: str, *, kind: str = "", limit: int = 20
) -> tuple[dict[str, list[Hit]], dict[str, int]]:
    """Results grouped by type, and the number of visible hits per type (facets)."""
    query = query.strip()[:MAX_QUERY]
    results: dict[str, list[Hit]] = {}
    counts: dict[str, int] = {}
    if len(query) < 2 or tenant is None:
        return results, counts
    for source in SOURCES.values():
        rows = _matches(source, query, tenant)
        if source.parent is not None:
            relation, model = source.parent
            allowed = policy.scope_queryset(
                user, source.permission, model.objects.filter(tenant=tenant)
            )
            visible = list(rows.filter(**{f"{relation}__in": allowed})[:CANDIDATES])
        elif source.path_field is not None:
            visible = list(
                policy.scope_queryset(user, source.permission, rows, path_field=source.path_field)[
                    :CANDIDATES
                ]
            )
        else:
            visible = [r for r in rows[:CANDIDATES] if policy.has_perm(user, source.permission, r)]
        if visible:
            counts[source.kind] = len(visible)
            if not kind or kind == source.kind:
                results[source.kind] = [
                    Hit(source.kind, r, _context(source, r)) for r in visible[:limit]
                ]
    notes = _notes(user, tenant, query)
    if notes:
        counts["note"] = len(notes)
        if not kind or kind == "note":
            results["note"] = notes[:limit]
    return results, counts


def _notes(user: Any, tenant: Any, query: str) -> list[Hit]:
    """Notes by full text (simple configuration: works for any language)."""
    found = (
        Note.objects.filter(tenant=tenant)
        .annotate(document=SearchVector("body", config="simple"))
        .filter(document=SearchQuery(query, config="simple", search_type="websearch"))
        .select_related("content_type")[:CANDIDATES]
    )
    hits = []
    for note in found:
        try:
            target = metadata.resolve(
                note.content_type.app_label + "." + note.content_type.model, note.object_id
            )
        except ServiceError:
            continue  # target gone or not visible
        if metadata.can(user, target, "view"):
            hits.append(Hit("note", target, note.body[:160]))
    return hits
