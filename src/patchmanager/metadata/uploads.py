"""Untrusted file handling: allow list sniffing, re-encoding, SVG sanitising, AV scan.

Nothing from the client is trusted: not the file name extension, not the
declared content type. Images are decoded and re-encoded (drops metadata and
polyglot payloads), SVGs are rebuilt from an element and attribute allow list,
PDFs are kept as is but only ever served as downloads.
"""

import io
import re
import socket
import struct
from dataclasses import dataclass
from xml.etree.ElementTree import Element, SubElement, tostring  # nosec B405: only used to serialise our own tree

from defusedxml import ElementTree as SafeET
from django.utils.translation import gettext as _
from PIL import Image, ImageOps, UnidentifiedImageError

from patchmanager.accounts.services import ServiceError
from patchmanager.instance import config as instance_config

Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression bomb guard

SVG_NS = "http://www.w3.org/2000/svg"
_SVG_ELEMENTS = frozenset(
    {
        "svg",
        "g",
        "defs",
        "title",
        "desc",
        "path",
        "rect",
        "circle",
        "ellipse",
        "line",
        "polyline",
        "polygon",
        "text",
        "tspan",
        "linearGradient",
        "radialGradient",
        "stop",
        "clipPath",
        "symbol",
    }
)
_SVG_ATTRIBUTES = frozenset(
    {
        "viewBox",
        "width",
        "height",
        "x",
        "y",
        "x1",
        "y1",
        "x2",
        "y2",
        "cx",
        "cy",
        "r",
        "rx",
        "ry",
        "d",
        "points",
        "fill",
        "fill-opacity",
        "fill-rule",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "opacity",
        "transform",
        "font-family",
        "font-size",
        "font-weight",
        "text-anchor",
        "dominant-baseline",
        "offset",
        "stop-color",
        "stop-opacity",
        "gradientUnits",
        "gradientTransform",
        "clip-path",
        "clip-rule",
        "id",
        "preserveAspectRatio",
        "version",
    }
)
# url(#id) references only; nothing that could load external content or script.
_SAFE_VALUE = re.compile(
    r"^(?!.*(?:javascript|data:|expression|@import|url\((?!#)))[^<>]*$", re.IGNORECASE
)


@dataclass(frozen=True)
class CleanFile:
    """Result of validation: safe bytes plus what we determined about them."""

    data: bytes
    mime_type: str
    kind: str  # "image" or "document"
    extension: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def sanitise_svg(data: bytes) -> bytes:
    """Rebuild an SVG from allowed elements and attributes only."""
    try:
        root = SafeET.fromstring(data, forbid_dtd=True)
    except Exception as exc:  # defusedxml raises several parse and security errors
        raise ServiceError(_("The SVG file is not valid.")) from exc
    if _local(root.tag) != "svg":
        raise ServiceError(_("The SVG file is not valid."))

    def copy(source: Element, target: Element | None) -> Element | None:
        name = _local(source.tag)
        if name not in _SVG_ELEMENTS:
            return None
        element = (
            Element(f"{{{SVG_NS}}}{name}")
            if target is None
            else SubElement(target, f"{{{SVG_NS}}}{name}")
        )
        for key, value in source.attrib.items():
            local = _local(key)
            if local in _SVG_ATTRIBUTES and _SAFE_VALUE.match(value):
                element.set(local, value)
        if name in {"text", "tspan", "title", "desc"} and source.text:
            element.text = source.text
        for child in source:
            copied = copy(child, element)
            if copied is not None and child.tail and name in {"text", "tspan"}:
                copied.tail = child.tail
        return element

    clean = copy(root, None)
    assert clean is not None  # noqa: S101  # nosec B101: root checked above
    return bytes(tostring(clean, encoding="utf-8", xml_declaration=True))


def _reencode(data: bytes) -> CleanFile:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            source_format = image.format
            picture: Image.Image = ImageOps.exif_transpose(image)
            out = io.BytesIO()
            if source_format == "JPEG":
                picture.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
                return CleanFile(out.getvalue(), "image/jpeg", "image", "jpg")
            if source_format == "WEBP":
                picture.save(out, "WEBP", quality=90)
                return CleanFile(out.getvalue(), "image/webp", "image", "webp")
            if picture.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                picture = picture.convert("RGBA")
            picture.save(out, "PNG", optimize=True)  # PNG and GIF (first frame)
            return CleanFile(out.getvalue(), "image/png", "image", "png")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError) as exc:
        raise ServiceError(_("The image could not be read.")) from exc


def is_raster(data: bytes) -> bool:
    """JPEG, PNG, GIF or WebP by magic bytes."""
    head = data[:16]
    return head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")) or (
        head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    )


def clean(data: bytes) -> CleanFile:
    """Validate by content and return safe bytes, or raise :class:`ServiceError`."""
    if len(data) > instance_config.get().upload_max_bytes:
        raise ServiceError(_("The file is too large."))
    if is_raster(data):
        result = _reencode(data)
    elif data.startswith(b"%PDF-"):
        result = CleanFile(data, "application/pdf", "document", "pdf")
    elif data.lstrip()[:5] in (b"<?xml", b"<svg ", b"<svg>") or b"<svg" in data[:1024]:
        result = CleanFile(sanitise_svg(data), "image/svg+xml", "image", "svg")
    else:
        raise ServiceError(_("Only JPEG, PNG, GIF, WebP, SVG and PDF files are accepted."))
    scan(result.data)
    return result


def scan(data: bytes) -> None:
    """Ask clamd (INSTREAM) when a scanner is configured. Fails closed."""
    scanner = instance_config.get()
    if not scanner.clamd_host:
        return
    try:
        with socket.create_connection((scanner.clamd_host, scanner.clamd_port), timeout=30) as sock:
            sock.sendall(b"zINSTREAM\0")
            for offset in range(0, len(data), 65536):
                chunk = data[offset : offset + 65536]
                sock.sendall(struct.pack("!L", len(chunk)) + chunk)
            sock.sendall(struct.pack("!L", 0))
            reply = sock.recv(4096).rstrip(b"\0").decode(errors="replace")
    except OSError as exc:
        raise ServiceError(_("The virus scanner is not reachable. Try again later.")) from exc
    if not reply.endswith("OK"):
        raise ServiceError(_("The file was rejected by the virus scanner."))
