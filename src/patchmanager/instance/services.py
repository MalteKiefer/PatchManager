"""Changing the instance settings (super admins)."""

import io
from typing import Any

from django.conf import settings
from django.http import HttpRequest
from django.utils.translation import gettext as _
from PIL import Image, UnidentifiedImageError

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.instance import editions, state
from patchmanager.instance.models import InstanceSettings

LOGO_MAX_BYTES = 512 * 1024
LOGO_MAX_SIDE = 512


def _require(actor: User) -> InstanceSettings:
    if not policy.has_perm(actor, "system.settings.change"):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    row = InstanceSettings.objects.first()
    return row or InstanceSettings()


def save_edition(
    request: HttpRequest,
    actor: User,
    *,
    edition: str,
    limits: dict[str, int | None],
    enable: list[str],
    disable: list[str],
) -> None:
    """Choose an edition and override single limits or features."""
    row = _require(actor)
    if editions.pinned():
        raise ServiceError(_("The edition is set by PATCHMANAGER_EDITION."))
    if edition not in editions.EDITIONS:
        raise ServiceError(_("Unknown edition."))
    if any(k not in editions.LIMITS or (v is not None and v < 0) for k, v in limits.items()):
        raise ServiceError(_("Limits are numbers of at least 0."))
    if not set(enable) | set(disable) <= set(editions.FEATURES) or set(enable) & set(disable):
        raise ServiceError(_("Unknown feature."))
    row.edition = edition
    row.overrides = {"limits": limits, "enable": sorted(enable), "disable": sorted(disable)}
    row.save()
    state.forget()
    audit.record(
        "instance.edition.changed", request=request, data={"edition": edition, **row.overrides}
    )


def _logo(data: bytes) -> bytes:
    """Re-encode an uploaded PNG or JPEG as a small PNG (drops metadata and anything hidden)."""
    if len(data) > LOGO_MAX_BYTES:
        raise ServiceError(_("The logo may be at most 512 KB."))
    if not data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        raise ServiceError(_("The logo must be a PNG or JPEG file."))
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.thumbnail((LOGO_MAX_SIDE, LOGO_MAX_SIDE))
            out = io.BytesIO()
            image.convert("RGBA").save(out, "PNG", optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ServiceError(_("The logo could not be read.")) from exc
    return out.getvalue()


def save_branding(
    request: HttpRequest,
    actor: User,
    *,
    name: str,
    brand_hue: int,
    default_language: str,
    logo: bytes | None = None,
    remove_logo: bool = False,
) -> None:
    """Instance name, colour, default language and logo."""
    row = _require(actor)
    if not 0 <= brand_hue <= 359:
        raise ServiceError(_("The colour hue is a number from 0 to 359."))
    if default_language not in dict(settings.LANGUAGES):
        raise ServiceError(_("Unknown language."))
    row.name, row.brand_hue, row.default_language = name.strip()[:100], brand_hue, default_language
    if remove_logo:
        row.logo = None
    elif logo is not None:
        row.logo = _logo(logo)
    row.save()
    state.forget()
    audit.record(
        "instance.branding.changed",
        request=request,
        data={
            "name": row.name,
            "brand_hue": brand_hue,
            "language": default_language,
            "logo": "removed" if remove_logo else "replaced" if logo is not None else "kept",
        },
    )


def brand_css(hue: int) -> str:
    """The brand colour scale for the chosen hue (same lightness and chroma as the default)."""
    shades = {
        50: (0.97, 0.02),
        100: (0.93, 0.04),
        500: (0.6, 0.11),
        600: (0.52, 0.1),
        700: (0.45, 0.09),
    }
    lines = [
        f"  --color-brand-{shade}: oklch({lightness} {chroma} {hue});"
        for shade, (lightness, chroma) in shades.items()
    ]
    return ":root {\n" + "\n".join(lines) + "\n}\n"


def logo_bytes() -> bytes | None:
    """The stored logo, if any."""
    if not state.get().has_logo:
        return None
    row = InstanceSettings.objects.only("logo").first()
    return bytes(row.logo) if row and row.logo else None


def summary() -> dict[str, Any]:
    """Edition, usage and limits for the admin page."""
    edition = editions.current()
    used = editions.usage()
    return {
        "edition": edition,
        "pinned": editions.pinned(),
        "used": used,
        "limits": [
            (k, label, used[k], edition.limits.get(k)) for k, label in editions.LIMITS.items()
        ],
    }
