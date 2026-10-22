"""Changing the instance settings (super admins)."""

import io
import ipaddress
import logging
import secrets
import socket
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.files.base import ContentFile
from django.http import HttpRequest
from django.utils.translation import gettext as _
from PIL import Image, UnidentifiedImageError

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.core import crypto
from patchmanager.instance import config as instance_config
from patchmanager.instance import state
from patchmanager.instance.models import InstanceSettings

logger = logging.getLogger(__name__)
LOGO_MAX_BYTES = 512 * 1024
LOGO_MAX_SIDE = 512


def _require(actor: User) -> InstanceSettings:
    if not policy.has_perm(actor, "system.settings.change"):
        raise PermissionDeniedError(_("You do not have permission to do that."))
    row = InstanceSettings.objects.first()
    return row or InstanceSettings()


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


def public_host(url: str) -> None:
    """An endpoint must be https and must not point into the host itself."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ServiceError(_("Enter an https address without credentials."))
    try:
        answers = socket.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise ServiceError(_("The host cannot be resolved.")) from exc
    for answer in answers:
        address = ipaddress.ip_address(answer[4][0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address.is_reserved
        ):
            raise ServiceError(_("This address cannot be used."))


def _probe(config: instance_config.Config) -> None:
    """Write and delete a small object, so a wrong bucket fails here and not on upload."""
    from patchmanager.instance.runtime import s3_storage

    store = s3_storage(config)
    try:
        name = store.save(f"health/probe-{secrets.token_hex(8)}.txt", ContentFile(b"ok"))
        store.delete(name)
    except Exception as exc:  # botocore raises many unrelated types
        logger.warning("bucket check failed: %s", type(exc).__name__)
        raise ServiceError(
            _("The bucket could not be written (%(error)s). Check name, endpoint and key.")
            % {"error": type(exc).__name__}
        ) from exc


# Destination settings and the secrets that belong to them: a stored secret is only sent
# to the destination it was entered for.
DESTINATIONS = (
    (("s3_endpoint_url", "s3_bucket"), ("s3_secret_access_key",)),
    (("offsite_repository",), ("offsite_secret_access_key", "offsite_password")),
)


def _keeps_secret_for_new_destination(
    before: instance_config.Config, values: dict[str, Any]
) -> bool:
    for targets, secrets_of_target in DESTINATIONS:
        moved = any(name in values and values[name] != getattr(before, name) for name in targets)
        kept = any(
            getattr(before, name) and values.get(name, "") == "" for name in secrets_of_target
        )
        if moved and kept:
            return True
    return False


def _merge(stored: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """Apply ``values`` to the stored form in place; returns the names that changed."""
    if _keeps_secret_for_new_destination(instance_config.from_stored(stored), values):
        raise ServiceError(_("A new destination needs its secret key and password entered again."))
    changed = []
    for name, value in values.items():
        if name not in instance_config.NAMES:
            raise ServiceError(_("Unknown setting."))
        if name in instance_config.SECRETS:
            if value == "":
                continue
            value = crypto.encrypt(value) if value else ""
        if stored.get(name) != value:
            changed.append(name)
        stored[name] = value
    return changed


def test_bucket(actor: User, values: dict[str, Any]) -> None:
    """Check bucket settings with a test object without saving them."""
    stored = dict(_require(actor).config)
    _merge(stored, values)
    effective = instance_config.from_stored(stored)
    if not effective.s3_bucket:
        raise ServiceError(_("Enter a bucket to test."))
    if effective.s3_endpoint_url:
        public_host(effective.s3_endpoint_url)
    _probe(effective)


def save_config(request: HttpRequest, actor: User, values: dict[str, Any]) -> None:
    """Store operational settings. Empty secrets keep the stored one; ``None`` clears it."""
    row = _require(actor)
    stored = dict(row.config)
    changed = _merge(stored, values)
    effective = instance_config.from_stored(stored)
    if effective.s3_endpoint_url and "s3_endpoint_url" in values:
        # botocore resolves again when it connects; only holders of system.settings.change
        # can set this, so the remaining rebinding window is accepted (ADR 0035).
        public_host(effective.s3_endpoint_url)
    if effective.s3_bucket and any(n.startswith("s3_") for n in changed):
        _probe(effective)
    if effective.offsite_repository and "offsite_repository" in values:
        from patchmanager.instance.offsite import check_repository

        check_repository(effective.offsite_repository)
    row.config = stored
    if row.pk is None:
        row.save()
    else:
        row.save(update_fields=["config", "updated_at"])
    instance_config.forget()
    state.forget()
    audit.record(
        "instance.settings.changed",
        request=request,
        data={"changed": sorted(changed)},  # names only: values may be secrets
    )
