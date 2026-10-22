"""End to end path tracing across cables and passive front/rear port mappings.

A path is a list of ports with one link between each neighbouring pair. A link
is a cable or the internal connection of a passive device (front port to its
rear port at a position). Rear ports with several positions carry one signal
per position, so the trace keeps the position it entered with and leaves the
far panel through the front port mapped to that position. A trace started on a
multi position rear port follows position 1.

The trace stops at an active port (interface, console, power, wireless), at an
unconnected port, or when it would revisit a port (loop).
"""

from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q

from patchmanager.cabling.models import CableEnd
from patchmanager.dcim.models import Port

PASSIVE = frozenset({"front", "rear"})
MAX_HOPS = 256


@dataclass(frozen=True)
class Link:
    """Connection between two neighbouring ports of a path."""

    kind: str  # "cable" or "internal"
    cable: Any = None
    position: int | None = None


@dataclass
class Path:
    """A traced path from one end to the other."""

    ports: list[Port] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    loop: bool = False

    @property
    def ends(self) -> tuple[Port, Port]:
        """First and last port."""
        return self.ports[0], self.ports[-1]

    @property
    def complete(self) -> bool:
        """Both ends reach active ports (passive hops in between are fine)."""
        first, last = self.ends
        return (
            len(self.ports) > 1
            and not self.loop
            and first.kind not in PASSIVE
            and last.kind not in PASSIVE
        )

    @property
    def cables(self) -> list[Any]:
        """Cables in path order."""
        return [link.cable for link in self.links if link.cable is not None]

    def segments(self) -> list[tuple[Port, Link, Port]]:
        """``(port, link, next port)`` triples."""
        return list(zip(self.ports, self.links, self.ports[1:], strict=True))


class _Database:
    """Look up neighbours one query at a time (a single trace)."""

    def cable_peer(self, port: Port) -> tuple[Any, Port] | None:
        other = (
            CableEnd.objects.select_related("cable", "port__device")
            .filter(cable__ends__port=port)
            .exclude(port=port)
            .first()
        )
        return (other.cable, other.port) if other is not None else None

    def rear_of(self, port: Port) -> Port:
        return Port.objects.select_related("device").get(front_ports=port)

    def fronts_of(self, port: Port) -> list[Port]:
        return list(port.front_ports.select_related("device"))


class _Preloaded:
    """Neighbours of every port reachable from a set of ports, loaded level by level."""

    def __init__(self, starts: list[Port]) -> None:
        self.ports: dict[Any, Port] = {p.pk: p for p in starts}
        self.peers: dict[Any, tuple[Any, Port]] = {}
        self.fronts: dict[Any, list[Port]] = {}
        frontier = set(self.ports)
        for _ in range(MAX_HOPS):
            if not frontier:
                break
            found: set[Any] = set()
            ends = CableEnd.objects.select_related("cable", "port__device").filter(
                cable__ends__port__in=frontier
            )
            by_cable: dict[Any, dict[Any, Any]] = {}
            for end in ends:  # the join repeats an end when both ends are in the frontier
                by_cable.setdefault(end.cable_id, {})[end.pk] = end
            for pair in (list(e.values()) for e in by_cable.values()):
                if len(pair) == 2:
                    a, b = pair
                    self.peers[a.port_id] = (a.cable, b.port)
                    self.peers[b.port_id] = (b.cable, a.port)
                    found |= {a.port_id, b.port_id}
                    self.ports.setdefault(a.port_id, a.port)
                    self.ports.setdefault(b.port_id, b.port)
            rears = {self.ports[pk].rear_port_id for pk in frontier} - {None}
            for port in Port.objects.select_related("device").filter(
                Q(pk__in=rears - set(self.ports)) | Q(rear_port_id__in=frontier)
            ):
                self.ports.setdefault(port.pk, port)
                found.add(port.pk)
            for pk in frontier:
                self.fronts.setdefault(pk, [])
            for port in self.ports.values():
                if port.rear_port_id in frontier and port not in self.fronts[port.rear_port_id]:
                    self.fronts[port.rear_port_id].append(port)
            frontier = {pk for pk in found if pk not in self.fronts}
        for fronts in self.fronts.values():
            fronts.sort(key=lambda p: (p.order, p.name))

    def cable_peer(self, port: Port) -> tuple[Any, Port] | None:
        return self.peers.get(port.pk)

    def rear_of(self, port: Port) -> Port:
        return self.ports[port.rear_port_id]

    def fronts_of(self, port: Port) -> list[Port]:
        return self.fronts.get(port.pk, [])


def _internal_peer(source: Any, port: Port, position: int | None) -> tuple[Port, int] | None:
    """Cross a passive device: front to rear, or rear to front at ``position``."""
    if port.kind == "front" and port.rear_port_id:
        return source.rear_of(port), port.rear_position or 1
    if port.kind == "rear":
        wanted = position or 1
        fronts = source.fronts_of(port)
        front = next((f for f in fronts if f.rear_position == wanted), None) or (
            fronts[0] if fronts and port.positions == 1 else None
        )
        return (front, wanted) if front is not None else None
    return None


def _walk(source: Any, start: Port, first_step: str) -> tuple[list[Port], list[Link], bool]:
    """Follow the path away from ``start``, beginning with a cable or an internal link."""
    ports, links = [start], []
    seen = {start.pk}
    port, step, position = start, first_step, None
    for _ in range(MAX_HOPS):
        if step == "cable":
            peer = source.cable_peer(port)
            if peer is None:
                break
            cable, nxt = peer
            link = Link("cable", cable=cable, position=position)
            step = "internal"
        else:
            crossing = _internal_peer(source, port, position)
            if crossing is None:
                break
            nxt, position = crossing
            link = Link("internal", position=position)
            step = "cable"
        ports.append(nxt)
        links.append(link)
        if nxt.pk in seen:
            return ports, links, True
        seen.add(nxt.pk)
        port = nxt
        if step == "internal" and nxt.kind not in PASSIVE:
            break  # reached an active port over a cable
    return ports, links, False


def _trace(source: Any, start: Port) -> Path:
    ports, links, loop = _walk(source, start, "cable")
    if start.kind not in PASSIVE:
        return Path(ports, links, loop)
    back_ports, back_links, back_loop = _walk(source, start, "internal")
    return Path(
        [*reversed(back_ports), *ports[1:]], [*reversed(back_links), *links], loop or back_loop
    )


def trace_many(starts: list[Port]) -> dict[Any, Path]:
    """Trace many ports with a few queries per hop instead of a few per port."""
    source = _Preloaded(starts)
    return {port.pk: _trace(source, port) for port in starts}


def trace(start: Port) -> Path:
    """Trace the full path through ``start`` in both directions."""
    return _trace(_Database(), start)
