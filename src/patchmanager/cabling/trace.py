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


def _cable_peer(port: Port) -> tuple[Any, Port] | None:
    end = CableEnd.objects.select_related("cable").filter(port=port).first()
    if end is None:
        return None
    other = (
        CableEnd.objects.select_related("port__device")
        .filter(cable=end.cable)
        .exclude(pk=end.pk)
        .first()
    )
    return (end.cable, other.port) if other is not None else None


def _internal_peer(port: Port, position: int | None) -> tuple[Port, int] | None:
    """Cross a passive device: front to rear, or rear to front at ``position``."""
    if port.kind == "front" and port.rear_port_id:
        rear = Port.objects.select_related("device").get(pk=port.rear_port_id)
        return rear, port.rear_position or 1
    if port.kind == "rear":
        wanted = position or 1
        fronts = port.front_ports.select_related("device")
        front = fronts.filter(rear_position=wanted).first() or (
            fronts.first() if port.positions == 1 else None
        )
        return (front, wanted) if front is not None else None
    return None


def _walk(start: Port, first_step: str) -> tuple[list[Port], list[Link], bool]:
    """Follow the path away from ``start``, beginning with a cable or an internal link."""
    ports, links = [start], []
    seen = {start.pk}
    port, step, position = start, first_step, None
    for _ in range(MAX_HOPS):
        if step == "cable":
            peer = _cable_peer(port)
            if peer is None:
                break
            cable, nxt = peer
            link = Link("cable", cable=cable, position=position)
            step = "internal"
        else:
            crossing = _internal_peer(port, position)
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


def trace(start: Port) -> Path:
    """Trace the full path through ``start`` in both directions."""
    ports, links, loop = _walk(start, "cable")
    if start.kind not in PASSIVE:
        return Path(ports, links, loop)
    back_ports, back_links, back_loop = _walk(start, "internal")
    return Path(
        [*reversed(back_ports), *ports[1:]], [*reversed(back_links), *links], loop or back_loop
    )
