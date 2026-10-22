"""The rack elevation of the web page (devices, every port with its state, cables) for PDFs.

``dcim.elevation`` lays out the drawing in SVG units with y pointing down; this module
draws the same layout with ReportLab shapes, so the PDF matches what the page shows.
"""

import re
from typing import Any

from reportlab.graphics.shapes import Circle, Drawing, Group, Line, Path, Rect, String
from reportlab.lib import colors

from patchmanager.dcim.elevation import Elevation, PortGlyph

FONT = "PMSans"
SLATE = {
    50: "#f8fafc",
    100: "#f1f5f9",
    200: "#e2e8f0",
    300: "#cbd5e1",
    400: "#94a3b8",
    500: "#64748b",
    700: "#334155",
    950: "#020617",
}
BRAND = "#0d9488"
LEAVES = {"face": "#fbbf24", "rack": "#d946ef"}
_TOKEN = re.compile(r"[MLQHVhvz]|-?\d+(?:\.\d+)?")


def _c(hex_colour: str, alpha: float = 1.0) -> Any:
    colour = colors.HexColor(hex_colour)
    return colors.Color(colour.red, colour.green, colour.blue, alpha=alpha)


def _path(d: str, height: float, **style: Any) -> Path:
    """The SVG path subset the layout writes (M, L, Q, h, v, z), flipped for PDF."""
    path = Path(**style)
    tokens = _TOKEN.findall(d)
    x = y = 0.0
    i = 0

    def num() -> float:
        nonlocal i
        i += 1
        return float(tokens[i - 1])

    while i < len(tokens):
        op = tokens[i]
        i += 1
        if op in ("M", "L"):
            x, y = num(), num()
            (path.moveTo if op == "M" else path.lineTo)(x, height - y)
        elif op == "Q":
            cx, cy, ex, ey = num(), num(), num(), num()
            # A quadratic bend as the equivalent cubic curve.
            path.curveTo(
                x + 2 / 3 * (cx - x),
                height - (y + 2 / 3 * (cy - y)),
                ex + 2 / 3 * (cx - ex),
                height - (ey + 2 / 3 * (cy - ey)),
                ex,
                height - ey,
            )
            x, y = ex, ey
        elif op == "h":
            x += num()
            path.lineTo(x, height - y)
        elif op == "v":
            y += num()
            path.lineTo(x, height - y)
        elif op == "z":
            path.closePath()
    return path


def _port(glyph: PortGlyph, height: float) -> Group:
    group = Group()
    if glyph.state in ("complete", "cabled"):
        fill, stroke = _c(glyph.colour), None
    elif glyph.state == "partial":
        fill, stroke = _c(glyph.colour, 0.45), None
    elif glyph.state == "ready":
        fill, stroke = _c(SLATE[950]), None
    elif glyph.passive:
        fill, stroke = colors.white, _c(SLATE[400])
    else:
        fill, stroke = _c(SLATE[300]), None
    group.add(_path(glyph.shape, height, fillColor=fill, strokeColor=stroke, strokeWidth=1.5))
    if not glyph.copper:
        group.add(_path(glyph.inner, height, fillColor=colors.white, strokeColor=None))
    if glyph.label_y is not None:
        group.add(
            String(
                glyph.centre_x,
                height - glyph.label_y,
                glyph.short,
                fontName=FONT + "-Bold",
                fontSize=glyph.font,
                fillColor=_c(SLATE[500]),
                textAnchor="middle",
            )
        )
    if glyph.leaves:
        group.add(
            Circle(
                glyph.mark_x,
                height - glyph.y,
                4.5,
                fillColor=_c(LEAVES.get(glyph.leaves, SLATE[500])),
                strokeColor=None,
            )
        )
    return group


def drawing(e: Elevation, width: float, height: float) -> Drawing:
    """One face of a rack, scaled to fit ``width`` x ``height`` points."""
    full_w, full_h = float(e.width), float(e.top + e.height + 16)
    scale = min(width / full_w, height / full_h)
    canvas = Drawing(full_w * scale, full_h * scale)
    group = Group()
    group.scale(scale, scale)
    h = full_h
    group.add(
        Rect(
            e.frame_x,
            h - e.top - e.height,
            e.frame_w,
            e.height,
            rx=6,
            ry=6,
            fillColor=_c(SLATE[100]),
            strokeColor=_c(SLATE[300]),
            strokeWidth=2,
        )
    )
    for row in e.rows:
        y = h - row["y"]
        group.add(Line(e.area_x, y, e.area_x + e.area_w, y, strokeColor=_c(SLATE[200])))
        for x, anchor in ((e.left_numbers_x, "end"), (e.right_numbers_x, "start")):
            group.add(
                String(
                    x,
                    h - row["y"] - 44,
                    str(row["unit"]),
                    fontName=FONT,
                    fontSize=15,
                    fillColor=_c(SLATE[500]),
                    textAnchor=anchor,
                )
            )
    for band in e.bands:
        top, size = h - band.y - 3, band.height - 6
        if band.space.device_id:
            fill, stroke, dash = _c(SLATE[50] if band.passive else "#ffffff"), _c(SLATE[300]), None
        else:
            fill, stroke, dash = _c("#f0fdfa"), _c(BRAND), [6, 4]
        group.add(
            Rect(
                e.area_x + 2,
                top - size,
                e.area_w - 4,
                size,
                rx=8,
                ry=8,
                fillColor=fill,
                strokeColor=stroke,
                strokeWidth=1.5,
                strokeDashArray=dash,
            )
        )
        group.add(
            Rect(
                e.area_x + 2,
                top - size,
                32,
                size,
                rx=8,
                ry=8,
                fillColor=_c(SLATE[100]),
                strokeColor=None,
            )
        )
        name = Group(
            String(
                0,
                0,
                band.label,
                fontName=FONT + "-Bold",
                fontSize=band.name_font,
                fillColor=_c(SLATE[700]),
                textAnchor="middle",
            )
        )
        # Written upwards in the strip, like on the page.
        name.translate(e.area_x + 23, h - band.y - band.half)
        name.rotate(90)
        group.add(name)
        for glyph in band.ports:
            group.add(_port(glyph, h))
    for wire in e.wires:
        group.add(
            _path(
                wire.d,
                h,
                fillColor=None,
                strokeColor=_c(wire.colour, 0.9),
                strokeWidth=2.2,
                strokeLineCap=1,
                strokeLineJoin=1,
            )
        )
    canvas.add(group)
    return canvas
