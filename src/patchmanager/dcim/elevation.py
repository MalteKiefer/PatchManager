"""Drawing layout of rack elevations: devices with every port, connection states and cables.

Everything is computed in drawing units for an SVG ``viewBox``; the template only
draws. One rack is laid out by ``layout``; a room puts several next to each other
(``room_layout``) and routes cables between racks over a cable tray above them.
"""

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.urls import reverse
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _

from patchmanager.accounts import policy
from patchmanager.cabling.models import CableEnd
from patchmanager.cabling.trace import Path, trace_many
from patchmanager.dcim.models import Device, Port, Rack, RackSpace

UNIT = 76  # height of one rack unit: 44.45 mm against 450 mm of mounting width
GUTTER = 56  # cable channel beside the rack, each side
RULER = 34  # unit numbers
AREA = 760  # width of the mounting area (19 inch)
LABEL = 34  # strip with the device name, written upwards, at the left of each device
GRIP = 26  # strip at the right of each device (drag handle)
WIDTH = 2 * GUTTER + 2 * RULER + AREA
PORT_MAX = 26
GROUP = 8  # ports drawn in groups of eight
GROUP_GAP = 14
CORNER = 6  # radius of cable bends
REAR_KINDS = frozenset({"rear", "power"})  # ports on the back panel; all others on the front
TRAY = 26  # height of one cable lane above the racks in a room drawing
RACK_GAP = 48


@dataclass
class PortGlyph:
    """One port drawn on a device."""

    port: Port
    x: float
    y: float
    size: float
    short: str
    state: str  # none, ready, partial, complete; "cabled" when not traced (room drawing)
    colour: str
    path_id: str
    title: str
    label_y: float | None  # number above the top row and below the bottom row, else none
    lane: float  # where its cable runs along the edge of the device
    flipped: bool  # bottom row: the connector is drawn upside down, like on the hardware
    passive: bool
    leaves: str = ""  # "face" or "rack" when the cable continues out of sight
    href: str = ""
    panel: str = ""

    @property
    def centre_x(self) -> float:
        """Horizontal centre (text anchor)."""
        return round(self.x + self.size / 2, 1)

    @property
    def font(self) -> float:
        """Font size of the number."""
        return round(max(self.size * 0.5, 8), 1)

    @property
    def svg(self) -> str:
        """The glyph as SVG markup (built here: a room drawing has thousands of them)."""
        fill = ""
        if self.state in ("complete", "cabled"):
            fill = f' fill="{escape(self.colour)}"'
        elif self.state == "partial":
            fill = f' fill="{escape(self.colour)}" fill-opacity="0.45"'
        parts = [
            f'<a href="{escape(self.href)}" data-panel="{escape(self.panel)}" '
            f'class="rack-port is-{self.state}{" on-passive" if self.passive else ""}" '
            f'data-path="{escape(self.path_id)}"><title>{escape(self.title)}</title>'
            f'<path d="{self.shape}"{fill}/>'
        ]
        if not self.copper:
            parts.append(f'<path d="{self.inner}" class="rack-port-inner"/>')
        if self.label_y is not None:
            parts.append(
                f'<text x="{self.centre_x}" y="{self.label_y}" font-size="{self.font}" '
                f'text-anchor="middle">{escape(self.short)}</text>'
            )
        if self.leaves:
            parts.append(
                f'<circle cx="{self.mark_x}" cy="{self.y}" r="4.5" '
                f'class="rack-port-leaves is-{self.leaves}"/>'
            )
        parts.append("</a>")
        return mark_safe("".join(parts))  # noqa: S308  # nosec B308 B703: every value escaped or numeric

    @property
    def copper(self) -> bool:
        """Drawn as an RJ45 socket; anything else as a cage."""
        return self.port.connector in ("rj45", "rj11")

    @property
    def inner(self) -> str:
        """The opening of a cage (fibre, SFP), drawn on top of the outline."""
        x, y, s = self.x + self.size * 0.3, self.y + self.size * 0.3, self.size * 0.4
        return f"M{x:.1f} {y:.1f}h{s:.1f}v{s:.1f}h{-s:.1f}z"

    @property
    def shape(self) -> str:
        """Outline of the socket: copper with the latch notch, other connectors square."""
        x, y, s = self.x, self.y, self.size
        if not self.copper:
            return f"M{x:.1f} {y:.1f}h{s:.1f}v{s:.1f}h{-s:.1f}z"
        notch, deep = s * 0.5, s * 0.28
        if self.flipped:
            return (
                f"M{x:.1f} {y:.1f}h{s:.1f}v{s - deep:.1f}h{-(s - notch) / 2:.1f}v{deep:.1f}"
                f"h{-notch:.1f}v{-deep:.1f}h{-(s - notch) / 2:.1f}z"
            )
        return (
            f"M{x:.1f} {y + deep:.1f}h{(s - notch) / 2:.1f}v{-deep:.1f}h{notch:.1f}v{deep:.1f}"
            f"h{(s - notch) / 2:.1f}v{s - deep:.1f}h{-s:.1f}z"
        )

    @property
    def mark_x(self) -> float:
        """Corner marker for cables that continue out of sight."""
        return round(self.x + self.size, 1)


@dataclass
class Band:
    """A device or reservation over its units."""

    space: RackSpace
    y: int
    height: int
    ports: list[PortGlyph] = field(default_factory=list)
    image: Any = None
    passive: bool = False  # patch panel or cassette: only front and rear ports
    grip: list[tuple[int, int]] = field(default_factory=list)  # drag handle dots
    hidden: bool = False  # a device the viewer may not see: drawn without name or ports

    @property
    def label(self) -> str:
        """Name shown on the band."""
        return _("not visible") if self.hidden else self.space.label

    @property
    def half(self) -> int:
        """Vertical centre, for the upright name."""
        return self.height // 2

    @property
    def name_font(self) -> float:
        """Font size that fits the name upright in the strip (long names get smaller)."""
        return round(min(15, (self.height - 12) / max(len(self.label), 1) / 0.62), 1)


@dataclass
class Wire:
    """A cable drawn between two visible ports."""

    d: str
    colour: str
    path_id: str
    title: str


@dataclass
class Elevation:
    """One face of one rack, ready to draw."""

    rack: Rack
    face: str
    x: int  # left edge in a room drawing
    top: int  # top edge: racks in a room stand on the same floor
    height: int
    rows: list[dict[str, int]]
    bands: list[Band]
    wires: list[Wire]
    free: list[dict[str, int]]  # rows of empty units (for "add a device here")

    @property
    def area_x(self) -> int:
        """Left edge of the mounting area."""
        return self.x + GUTTER + RULER

    @property
    def frame_x(self) -> int:
        """Left edge of the rack frame (rails and unit numbers)."""
        return self.x + GUTTER

    @property
    def frame_w(self) -> int:
        """Width of the rack frame."""
        return 2 * RULER + AREA

    @property
    def left_numbers_x(self) -> int:
        """Right aligned unit numbers on the left rail."""
        return self.x + GUTTER + RULER - 8

    @property
    def right_numbers_x(self) -> int:
        """Left aligned unit numbers on the right rail."""
        return self.x + GUTTER + RULER + AREA + 8

    @property
    def area_w(self) -> int:
        """Width of the mounting area."""
        return AREA

    @property
    def centre_x(self) -> int:
        """Centre of the rack (title)."""
        return self.x + WIDTH // 2

    @property
    def width(self) -> int:
        """Total width including the cable channels."""
        return WIDTH


def port_face(port: Port) -> str:
    """Panel a port sits on."""
    return "rear" if port.kind in REAR_KINDS else "front"


def _prefix(name: str) -> str:
    return re.sub(r"\d+$", "", name)


def short_name(name: str, common: str = "") -> str:
    """What fits on the glyph: the number for the device's usual ports, else the name."""
    match = re.search(r"(\d+)$", name)
    if match and _prefix(name) == common:
        return match.group(1)[-3:]
    return name[-4:] if match else name[:4]


def path_id(path: Path) -> str:
    """Same id for every port and cable of one end to end path."""
    key = ",".join(sorted(str(p.pk) for p in path.ports))
    return hashlib.blake2s(key.encode(), digest_size=6).hexdigest()


def _allowed(user: Any, permission: str, device_ids: Iterable[Any]) -> set[Any]:
    """Devices among ``device_ids`` on which ``user`` holds ``permission``."""
    devices = Device.objects.filter(pk__in=set(device_ids))
    return set(policy.scope_queryset(user, permission, devices).values_list("pk", flat=True))


def _wired(ports: list[Port]) -> set[Any]:
    """Ports with a path to follow: cabled, or a panel port whose other side is cabled."""
    ids = [p.pk for p in ports]
    cabled = set(CableEnd.objects.filter(port_id__in=ids).values_list("port_id", flat=True))
    rears = {p.rear_port_id for p in ports if p.rear_port_id}
    cabled_rears = set(CableEnd.objects.filter(port_id__in=rears).values_list("port_id", flat=True))
    behind = {p.pk for p in ports if p.rear_port_id in cabled_rears}
    fronts_cabled = set(
        Port.objects.filter(
            rear_port_id__in=[p.pk for p in ports if p.kind == "rear"], cable_end__isnull=False
        ).values_list("rear_port_id", flat=True)
    )
    return cabled | behind | fronts_cabled


def _paths(user: Any, ports: list[Port]) -> dict[Any, Path]:
    """End to end paths where the viewer may trace; elsewhere the port alone."""
    wired = _wired(ports)
    candidates = [p for p in ports if p.pk in wired]
    traceable = _allowed(user, "cabling.cable.trace", (p.device_id for p in candidates))
    paths = trace_many([p for p in candidates if p.device_id in traceable])
    return {p.pk: paths.get(p.pk) or Path([p]) for p in ports}


def _state(port: Port, path: Path, cabled: set[Any]) -> str:
    if len(path.ports) == 1:
        return "partial" if port.pk in cabled else "none"  # cabled, but not traced
    if port.pk not in cabled:
        return "ready"  # nothing plugged in here, but the port is wired behind
    return "complete" if path.complete else "partial"


def _block(rows: int, size: float) -> float:
    """Height of ``rows`` rows of glyphs with the numbers above and below."""
    font = max(size * 0.5, 8)
    return rows * size + (rows - 1) * size * 0.3 + (2 if rows > 1 else 1) * (font + 1.5)


def _grid(count: int, width: float, height: float) -> tuple[int, int, float]:
    """Rows, columns and the largest glyph size that fit ``count`` ports and their numbers."""
    best = (1, count, 0.0)
    for rows in range(1, 9):
        cols = math.ceil(count / rows)
        groups = math.ceil(cols / max(1, GROUP // rows))
        size = min((width - (groups - 1) * GROUP_GAP) / cols * 0.8, PORT_MAX)
        while size > 4 and _block(rows, size) > height:
            size -= 0.5
        if size > best[2]:
            best = (rows, cols, size)
    return best


def _place_ports(band: Band, ports: list[Port], area_x: int) -> list[dict[str, Any]]:
    """Positions like on the hardware: numbered down each column, in groups of eight."""
    if not ports:
        return []
    left = area_x + LABEL + 14
    width = AREA - LABEL - GRIP - 28
    rows, cols, size = _grid(len(ports), width, band.height - 12)  # 6 each edge: cable lanes
    per_group = max(1, GROUP // rows)
    groups = math.ceil(cols / per_group)
    step_x = (width - (groups - 1) * GROUP_GAP) / cols
    font = max(size * 0.5, 8)
    top = band.y + (band.height - _block(rows, size)) / 2 + font + 1.5
    placed = []
    for index, port in enumerate(ports):
        col, row = divmod(index, rows)
        x = left + col * step_x + (col // per_group) * GROUP_GAP + (step_x - size) / 2
        y = top + row * size * 1.3
        last = row == rows - 1 and rows > 1
        label = y - 2 if row == 0 else (y + size + font - 1 if last else None)
        lane = band.y + 5 if row == 0 and rows > 1 else band.y + band.height - 5
        placed.append(
            {
                "port": port,
                "x": x,
                "y": y,
                "size": size,
                "label_y": label,
                "lane": lane,
                "flipped": last,
            }
        )
    return placed


def _label(port: Port | None, visible: bool) -> str:
    if port is None:
        return ""
    return f"{port.device} {port.name}" if visible else "…"


def _y_of(rack: Rack, unit: int, top: int = 0) -> int:
    index = unit - 1 if rack.descending_units else rack.u_height - unit
    return top + index * UNIT


def _bands(
    spaces: list[RackSpace], rack: Rack, face: str, top: int, backed: set[Any]
) -> list[Band]:
    shown = [s for s in spaces if s.face in (face, RackSpace.Face.BOTH)]
    if face == "rear":
        # A shallow device with ports on its back (a patch panel) is seen from the rear
        # too, unless something else is mounted behind it.
        behind = {u for s in shown for u in range(s.start, s.end + 1)}
        shown += [
            s
            for s in spaces
            if s.face == RackSpace.Face.FRONT
            and s.device_id in backed
            and not behind & set(range(s.start, s.end + 1))
        ]
    bands = []
    for space in shown:
        edge = space.start if rack.descending_units else space.end
        device_type = space.device.device_type if space.device is not None else None
        image = None
        if device_type is not None:
            image = device_type.front_image_id if face == "front" else device_type.rear_image_id
        band = Band(space, _y_of(rack, edge, top), space.size * UNIT, image=image)
        grip_x = AREA - GRIP // 2  # relative to the mounting area, made absolute below
        band.grip = [
            (grip_x + dx, band.y + band.height // 2 + dy)
            for dx in (-4, 4)
            for dy in range(-12, 13, 8)
        ]
        bands.append(band)
    return bands


def layout(user: Any, rack: Rack, face: str) -> Elevation:
    """Elevation of one face with ports, states and in-rack cables."""
    return room_layout(user, [rack], face)[0][0]


def room_layout(
    user: Any, racks: Iterable[Rack], face: str, *, trace: bool = True
) -> tuple[list[Elevation], list[Wire], int]:
    """Elevations side by side, the cables inside each rack, and cables between racks.

    Returns the elevations, the wires between racks (over the tray) and the tray height.
    """
    racks = list(racks)
    floor = max((r.u_height for r in racks), default=0) * UNIT
    spaces: dict[Any, list[RackSpace]] = {r.pk: [] for r in racks}
    for space in RackSpace.objects.filter(rack__in=racks).select_related("device__device_type"):
        spaces[space.rack_id].append(space)
    backed: set[Any] = set()
    if face == "rear":
        backed = set(
            Port.objects.filter(device__space__rack__in=racks, kind__in=REAR_KINDS).values_list(
                "device_id", flat=True
            )
        )
    elevations: list[Elevation] = []
    by_device: dict[Any, tuple[Elevation, Band]] = {}
    for index, rack in enumerate(racks):
        top = floor - rack.u_height * UNIT
        rows = [{"unit": u, "y": _y_of(rack, u, top)} for u in range(1, rack.u_height + 1)]
        bands = _bands(spaces[rack.pk], rack, face, top, backed)
        taken = {u for b in bands for u in range(b.space.start, b.space.end + 1)}
        elevation = Elevation(
            rack=rack,
            face=face,
            x=index * (WIDTH + RACK_GAP),
            top=top,
            height=rack.u_height * UNIT,
            rows=rows,
            bands=bands,
            wires=[],
            free=[r for r in rows if r["unit"] not in taken],
        )
        elevations.append(elevation)
        for band in bands:
            band.grip = [(elevation.area_x + x, y) for x, y in band.grip]
            if band.space.device_id:
                by_device[band.space.device_id] = (elevation, band)
    visible = _allowed(user, "dcim.device.view", by_device)
    for device_id in set(by_device) - visible:
        by_device.pop(device_id)[1].hidden = True

    ports = [
        p
        for p in Port.objects.filter(device_id__in=by_device).select_related("device")
        if port_face(p) == face
    ]
    # Without ``trace`` (a whole room) a port shows its own cable only; the side panel
    # still traces the path of a selected port.
    paths = _paths(user, ports) if trace else {p.pk: Path([p]) for p in ports}
    ends = {
        e.port_id: e
        for e in CableEnd.objects.filter(cable__ends__port__in=ports).select_related(
            "cable", "port"
        )
    }  # both ends of every cable on a drawn port
    cabled = set(ends)
    by_owner: dict[Any, list[Port]] = {}
    for p in ports:
        by_owner.setdefault(p.device_id, []).append(p)
    seen: dict[Any, bool] = {}

    def can_see(device: Any) -> bool:
        if device.pk not in seen:
            seen[device.pk] = policy.has_perm(user, "dcim.device.view", device)
        return seen[device.pk]

    # One reverse() per kind, not per port: thousands of ports in a room drawing.
    marker = "00000000-0000-0000-0000-000000000000"
    port_url = reverse("devices:port", args=[marker])
    panel_url = reverse("devices:port-panel", args=[marker])
    glyphs: dict[Any, tuple[Elevation, PortGlyph]] = {}
    for device_id, (elevation, band) in by_device.items():
        own = sorted(by_owner.get(device_id, []), key=lambda p: (p.order, p.name))
        band.passive = bool(own) and all(p.kind in ("front", "rear") for p in own)
        prefixes = [_prefix(p.name) for p in own]
        common = max(set(prefixes), key=prefixes.count) if prefixes else ""
        for spot in _place_ports(band, own, elevation.area_x):
            port = spot["port"]
            path = paths[port.pk]
            end = ends.get(port.pk)
            colour = end.cable.colour if end is not None and end.cable.colour else ""
            if not colour:
                colour = next((c.colour for c in path.cables if c.colour), "")
            far = path.ports[-1] if path.ports[0].pk == port.pk else path.ports[0]
            far_visible = far.pk == port.pk or can_see(far.device)
            title = f"{port.device} {port.name}"
            if len(path.ports) > 1:
                title += f" → {_label(far, far_visible)}"
            glyph = PortGlyph(
                port=port,
                x=round(spot["x"], 1),
                y=round(spot["y"], 1),
                size=round(spot["size"], 1),
                label_y=round(spot["label_y"], 1) if spot["label_y"] is not None else None,
                lane=round(spot["lane"], 1),
                flipped=spot["flipped"],
                passive=band.passive,
                href=port_url.replace(marker, str(port.pk)),
                panel=panel_url.replace(marker, str(port.pk)),
                short=short_name(port.name, common),
                state=_state(port, path, cabled) if trace or end is None else "cabled",
                colour=colour or "#2563eb",
                path_id=path_id(path) if trace or end is None else f"c{end.cable_id.hex[:12]}",
                title=title,
            )
            band.ports.append(glyph)
            glyphs[port.pk] = (elevation, glyph)

    between: list[Wire] = []
    peers: dict[Any, list[CableEnd]] = {}
    for end in ends.values():
        peers.setdefault(end.cable_id, []).append(end)
    for pair in peers.values():
        drawn = [glyphs[e.port_id] for e in pair if e.port_id in glyphs]
        if len(drawn) == 2:
            (rack_a, a), (rack_b, b) = drawn
            title = f"{pair[0].cable}: {a.port.device} {a.port.name}, {b.port.device} {b.port.name}"
            # Cables between the same two devices share a lane, like a bundle.
            pair_key = "".join(sorted((str(a.port.device_id), str(b.port.device_id))))
            lane = int(hashlib.blake2s(pair_key.encode(), digest_size=2).hexdigest(), 16) % 8
            if rack_a is rack_b:
                rack_a.wires.append(Wire(_in_rack(a, b, lane, rack_a), a.colour, a.path_id, title))
            else:
                between.append(
                    Wire(
                        _over_tray(a, b, len(between) + 1, rack_a, rack_b, lane),
                        a.colour,
                        a.path_id,
                        title,
                    )
                )
        elif len(drawn) == 1 and len(pair) == 2:
            other = next(e for e in pair if e.port_id not in glyphs)
            # The far end is on the other face of a drawn device, or outside these racks.
            drawn[0][1].leaves = "face" if other.port.device_id in by_device else "rack"
    return elevations, between, (len(between) + 1) * TRAY if len(racks) > 1 else 0


def _centre(glyph: PortGlyph) -> tuple[float, float]:
    return glyph.x + glyph.size / 2, glyph.y + glyph.size / 2


def _rounded(points: list[tuple[float, float]]) -> str:
    """A right angled line through ``points`` with rounded bends."""
    d = f"M{points[0][0]:.1f} {points[0][1]:.1f}"
    for (px, py), (x, y), (nx, ny) in zip(points, points[1:], points[2:], strict=False):
        r = min(CORNER, abs(x - px) / 2 + abs(y - py) / 2, abs(nx - x) / 2 + abs(ny - y) / 2)
        ix = x - math.copysign(r, x - px) if x != px else x
        iy = y - math.copysign(r, y - py) if y != py else y
        ox = x + math.copysign(r, nx - x) if nx != x else x
        oy = y + math.copysign(r, ny - y) if ny != y else y
        d += f" L{ix:.1f} {iy:.1f} Q{x:.1f} {y:.1f} {ox:.1f} {oy:.1f}"
    return d + f" L{points[-1][0]:.1f} {points[-1][1]:.1f}"


def _channel(rack: Elevation, side: str, lane: int) -> float:
    """A lane in the cable channel beside the rack; parallel cables keep apart."""
    shift = 8 + (lane % 8) * 5
    return rack.frame_x - shift if side == "left" else rack.frame_x + rack.frame_w + shift


def _side(rack: Elevation, x: float) -> str:
    return "left" if x < rack.area_x + AREA / 2 else "right"


def _in_rack(a: PortGlyph, b: PortGlyph, lane: int, rack: Elevation) -> str:
    """From a port to the edge of its device, along it, down the channel, and in to the other."""
    (ax, ay), (bx, by) = _centre(a), _centre(b)
    x = _channel(rack, _side(rack, (ax + bx) / 2), lane)
    points = [(ax, ay), (ax, a.lane), (x, a.lane), (x, b.lane), (bx, b.lane), (bx, by)]
    if a.lane == b.lane:  # same edge of the same device: no detour through the channel
        points = [(ax, ay), (ax, a.lane), (bx, a.lane), (bx, by)]
    return _rounded(points)


def _over_tray(
    a: PortGlyph, b: PortGlyph, tray: int, rack_a: Elevation, rack_b: Elevation, lane: int
) -> str:
    """Out to the cable channel, up to the tray above the racks, across, and down the other."""
    (ax, ay), (bx, by) = _centre(a), _centre(b)
    out = _channel(rack_a, _side(rack_a, ax), lane)
    into = _channel(rack_b, _side(rack_b, bx), lane)
    y = min(rack_a.top, rack_b.top) - tray * TRAY
    return _rounded(
        [
            (ax, ay),
            (ax, a.lane),
            (out, a.lane),
            (out, y),
            (into, y),
            (into, b.lane),
            (bx, b.lane),
            (bx, by),
        ]
    )


@dataclass
class Connection:
    """One cabled port of a rack with where its path ends."""

    port: Port
    cable: Any
    far: Port | None  # None: not visible to the viewer
    complete: bool
    hops: int
    path_id: str


def connections(user: Any, rack: Rack) -> list[Connection]:
    """Every cabled port of the devices in ``rack`` (both faces), in rack order."""
    cabled = Port.objects.filter(device__space__rack=rack, cable_end__isnull=False)
    visible = _allowed(user, "dcim.device.view", cabled.values_list("device_id", flat=True))
    ports = list(
        cabled.filter(device_id__in=visible)
        .select_related("device", "cable_end__cable")
        .order_by("-device__space__units", "device__name", "order", "name")
    )
    paths = _paths(user, ports)
    rows = []
    for port in ports:
        path = paths[port.pk]
        far = path.ports[-1] if path.ports[0].pk == port.pk else path.ports[0]
        shown = far.device_id in visible or policy.has_perm(user, "dcim.device.view", far.device)
        rows.append(
            Connection(
                port=port,
                cable=port.cable_end.cable,
                far=far if shown and far.pk != port.pk else None,
                complete=path.complete,
                hops=len(path.cables),
                path_id=path_id(path),
            )
        )
    return rows
