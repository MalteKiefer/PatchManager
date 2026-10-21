"""PDF reports: site report, rack elevation, port list, patch list and bill of materials."""

import html
import io
from collections import Counter
from functools import cache
from pathlib import Path
from typing import Any

import reportlab
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from patchmanager.accounts import policy
from patchmanager.cabling.models import Cable
from patchmanager.dcim.models import Device, Port, Rack, RackSpace
from patchmanager.locations.models import Building, Floor, Room, Site
from patchmanager.networks.models import VLAN

FONT = "PMSans"
LOGO_MAX_BYTES = 2 * 1024 * 1024


@cache
def _fonts() -> None:
    # Vera ships with reportlab and covers Western European characters (umlauts, ß).
    folder = Path(reportlab.__file__).parent / "fonts"
    pdfmetrics.registerFont(TTFont(FONT, str(folder / "Vera.ttf")))
    pdfmetrics.registerFont(TTFont(FONT + "-Bold", str(folder / "VeraBd.ttf")))
    pdfmetrics.registerFontFamily(
        FONT, normal=FONT, bold=FONT + "-Bold", italic=FONT, boldItalic=FONT + "-Bold"
    )


def logo_problem() -> str | None:
    """Why the configured logo cannot be used, or None (also None if none is set)."""
    if not settings.BRAND_LOGO:
        return None
    path = Path(settings.BRAND_LOGO)
    if not path.is_file():
        return "PATCHMANAGER_BRAND_LOGO is not a readable file."
    with path.open("rb") as handle:
        head = handle.read(8)
    if not head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return "PATCHMANAGER_BRAND_LOGO must be a PNG or JPEG file."
    if path.stat().st_size > LOGO_MAX_BYTES:
        return "PATCHMANAGER_BRAND_LOGO is larger than 2 MB."
    return None


STYLES = {
    "title": ParagraphStyle(
        "title", fontName=FONT + "-Bold", fontSize=16, leading=20, spaceAfter=6
    ),
    "h2": ParagraphStyle(
        "h2", fontName=FONT + "-Bold", fontSize=11, leading=14, spaceBefore=10, spaceAfter=4
    ),
    "body": ParagraphStyle("body", fontName=FONT, fontSize=8.5, leading=11),
    "small": ParagraphStyle("small", fontName=FONT, fontSize=7, leading=9, textColor=colors.grey),
}


def _p(text: Any, style: str = "body") -> Paragraph:
    _fonts()
    return Paragraph(
        html.escape("" if text is None else str(text)), STYLES[style]
    )  # data is text, never markup


def _table(header: list[str], rows: list[list[Any]], widths: list[float] | None = None) -> Table:
    data = [[_p(h, "body") for h in header], *[[_p(c) for c in row] for row in rows]]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#94a3b8")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]
        )
    )
    return table


def _render(title: str, subtitle: str, story: list[Any], user: Any, *, wide: bool = False) -> bytes:
    _fonts()
    out = io.BytesIO()
    size = landscape(A4) if wide else A4
    stamp = _("%(name)s · %(user)s · %(time)s") % {
        "name": settings.INSTANCE_NAME,
        "user": str(user),
        "time": date_format(timezone.localtime(), "DATETIME_FORMAT"),
    }

    def frame(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setFont(FONT, 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(15 * mm, 10 * mm, stamp)
        canvas.drawRightString(size[0] - 15 * mm, 10 * mm, _("Page %(n)s") % {"n": doc.page})
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out,
        pagesize=size,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title=title,
        author=settings.INSTANCE_NAME,
    )
    head: list[Any] = []
    if settings.BRAND_LOGO and logo_problem() is None:
        logo = Image(settings.BRAND_LOGO)
        ratio = 12 * mm / logo.imageHeight
        logo.drawHeight, logo.drawWidth = 12 * mm, logo.imageWidth * ratio
        logo.hAlign = "LEFT"
        head.append(logo)
    head += [_p(title, "title"), _p(subtitle, "small"), Spacer(1, 4 * mm)]
    doc.build(head + story, onFirstPage=frame, onLaterPages=frame)
    return out.getvalue()


def _visible(user: Any, permission: str, queryset: Any) -> Any:
    return policy.scope_queryset(user, permission, queryset)


# ---------------------------------------------------------------- reports


def site_report(user: Any, site: Site) -> bytes:
    """Overview of a site: locations with rack and device counts, VLANs."""
    story: list[Any] = [
        _table(
            [_("Code"), _("Address"), _("Description")],
            [[site.code, site.address, site.description]],
        )
    ]
    racks = Counter(
        _visible(
            user, "dcim.rack.view", Rack.objects.filter(path__descendant_of=str(site.path))
        ).values_list("room_id", flat=True)
    )
    devs = Counter(
        _visible(
            user, "dcim.device.view", Device.objects.filter(path__descendant_of=str(site.path))
        ).values_list("room_id", flat=True)
    )
    rows = []
    buildings = _visible(
        user, "locations.building.view", Building.objects.filter(site=site)
    ).order_by("name")
    for building in buildings:
        for floor in _visible(
            user, "locations.floor.view", Floor.objects.filter(building=building)
        ).order_by("level", "name"):
            for room in _visible(
                user, "locations.room.view", Room.objects.filter(floor=floor)
            ).order_by("name"):
                rows.append(
                    [
                        building.name,
                        floor.name,
                        room.name,
                        room.get_kind_display(),
                        racks[room.pk],
                        devs[room.pk],
                    ]
                )
    story += [
        _p(_("Locations"), "h2"),
        _table([_("Building"), _("Floor"), _("Room"), _("Kind"), _("Racks"), _("Devices")], rows),
        _p(
            _("Totals: %(r)s racks, %(d)s devices")
            % {"r": sum(racks.values()), "d": sum(devs.values())}
        ),
    ]
    vlans = [
        v
        for v in VLAN.objects.filter(Q(site=site) | Q(site=None), tenant=site.tenant)
        .select_related("site")
        .order_by("vid")
        if policy.has_perm(user, "networks.vlan.view", v)
    ]
    story += [
        _p(_("VLANs"), "h2"),
        _table(
            [_("VID"), _("Name"), _("Scope"), _("Status")],
            [
                [v.vid, v.name, v.site.name if v.site else _("tenant"), v.get_status_display()]
                for v in vlans
            ],
        ),
    ]
    return _render(_("Site report: %(name)s") % {"name": site.name}, str(site.tenant), story, user)


def rack_elevation(user: Any, rack: Rack) -> bytes:
    """Units of a rack, front and rear."""
    spaces = list(RackSpace.objects.filter(rack=rack).select_related("device"))
    front: dict[int, str] = {}
    rear: dict[int, str] = {}
    for space in spaces:
        visible = space.device is None or policy.has_perm(user, "dcim.device.view", space.device)
        label = (space.label or str(space.device or _("reserved"))) if visible else _("not visible")
        for unit in range(space.start, space.end + 1):
            if space.face in ("front", "both"):
                front[unit] = label
            if space.face in ("rear", "both"):
                rear[unit] = label
    units = range(1, rack.u_height + 1)
    order = units if rack.descending_units else reversed(units)
    rows = [[u, front.get(u, ""), rear.get(u, "")] for u in order]
    table = _table([_("Unit"), _("Front"), _("Rear")], rows, widths=[14 * mm, 80 * mm, 80 * mm])
    used = len(set(front) | set(rear))
    subtitle = f"{rack.room} · {rack.u_height} U · " + _("%(n)s units used") % {"n": used}
    return _render(_("Rack %(name)s") % {"name": rack.name}, subtitle, [table], user)


def port_list(user: Any, device: Device) -> bytes:
    """Ports of a device with their peer and VLAN."""
    from patchmanager.cabling.models import CableEnd

    ports = list(device.ports.select_related("untagged_vlan").order_by("order"))
    ends = {e.port_id: e.cable_id for e in CableEnd.objects.filter(port__in=ports)}
    peers = {
        e.cable_id: e.port
        for e in CableEnd.objects.filter(cable_id__in=ends.values())
        .exclude(port__in=ports)
        .select_related("port__device")
    }
    rows = []
    for port in ports:
        cable_id = ends.get(port.pk)
        peer = peers.get(cable_id) if cable_id else None
        if peer is not None and not policy.has_perm(user, "dcim.device.view", peer.device):
            peer_text = _("not visible")
        else:
            peer_text = f"{peer.device} · {peer.name}" if peer else ""
        rows.append(
            [
                port.name,
                port.get_kind_display(),
                port.get_connector_display(),
                port.speed_mbps or "",
                port.untagged_vlan.vid if port.untagged_vlan else "",
                port.label,
                peer_text,
            ]
        )
    table = _table(
        [
            _("Port"),
            _("Kind"),
            _("Connector"),
            _("Mbit/s"),
            _("VLAN"),
            _("Label"),
            _("Connected to"),
        ],
        rows,
    )
    return _render(
        _("Ports of %(name)s") % {"name": device.name},
        str(device.device_type),
        [table],
        user,
        wide=True,
    )


def patch_list(user: Any, site: Site) -> bytes:
    """Cables within a site with both ends."""
    cables = _visible(
        user, "cabling.cable.view", Cable.objects.filter(path__descendant_of=str(site.path))
    )
    rows = []
    for cable in cables.prefetch_related("ends__port__device").order_by("label", "pk"):
        ends = {e.side: e.port for e in cable.ends.all()}

        def show(port: Port | None) -> str:
            if port is None:
                return ""
            if not policy.has_perm(user, "dcim.device.view", port.device):
                return _("not visible")
            return f"{port.device} · {port.name}"

        rows.append(
            [
                cable.label or str(cable.pk)[:8],
                cable.get_kind_display(),
                cable.get_media_display(),
                cable.length_m or "",
                show(ends.get("a")),
                show(ends.get("b")),
            ]
        )
    table = _table([_("Cable"), _("Kind"), _("Media"), _("Length (m)"), _("A"), _("B")], rows)
    return _render(
        _("Patch list: %(name)s") % {"name": site.name}, str(site.tenant), [table], user, wide=True
    )


def bill_of_materials(user: Any, site: Site) -> bytes:
    """Device types in a site with counts."""
    found = _visible(
        user, "dcim.device.view", Device.objects.filter(path__descendant_of=str(site.path))
    )
    counts = Counter(
        found.values_list(
            "device_type__vendor__name", "device_type__model", "device_type__part_number"
        )
    )
    rows = [
        [v, m, p, n] for (v, m, p), n in sorted(counts.items(), key=lambda i: (i[0][0], i[0][1]))
    ]
    table = _table([_("Vendor"), _("Model"), _("Part number"), _("Quantity")], rows)
    return _render(
        _("Bill of materials: %(name)s") % {"name": site.name},
        str(site.tenant),
        [table, _p(_("Total: %(n)s devices") % {"n": sum(counts.values())})],
        user,
    )
