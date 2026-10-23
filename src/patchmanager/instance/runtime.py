"""Django and DRF hooks that read the operational settings at use time (instance.config).

Each one replaces a value Django would otherwise fix at start: password length, the mail
server, the API rate, the attachment storage and the map tile origin in the CSP.
"""

import re
import threading
from collections.abc import Callable
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.password_validation import MinimumLengthValidator
from django.core.files.storage import FileSystemStorage, Storage
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.core.mail.backends.smtp import EmailBackend as SmtpBackend
from django.http import HttpRequest, HttpResponse
from django.utils.csp import CSP
from rest_framework.throttling import UserRateThrottle

from patchmanager.instance import config as instance_config


class ConfiguredMinimumLength(MinimumLengthValidator):
    """Minimum password length from the sign in settings."""

    def __init__(self, min_length: int = 12) -> None:
        """Keep Django's signature; the length is read on use."""
        super().__init__(min_length)

    def validate(self, password: str, user: Any = None) -> None:
        """Check against the current minimum length."""
        self.min_length = instance_config.get().password_min_length
        super().validate(password, user)

    def get_help_text(self) -> str:
        """Help text with the current minimum length."""
        self.min_length = instance_config.get().password_min_length
        return str(super().get_help_text())


class ConfiguredMailBackend(SmtpBackend):
    """SMTP with the server from the mail settings; the console while none is set in DEBUG."""

    def __init__(self, *, alias: str | None = None, **options: Any) -> None:
        """Connect with the current mail settings."""
        mail = instance_config.get()
        self.sender = mail.mail_from
        self.console = (
            ConsoleBackend(alias=alias) if settings.DEBUG and not mail.mail_host else None
        )
        super().__init__(
            alias=alias,
            host=mail.mail_host or "localhost",
            port=mail.mail_port,
            username=mail.mail_user,
            password=mail.mail_password,
            use_tls=mail.mail_use_tls,
            timeout=20,
            **options,
        )

    def send_messages(self, email_messages: Any) -> int:
        """Send with the configured sender address."""
        for message in email_messages:
            if message.from_email == settings.DEFAULT_FROM_EMAIL:
                message.from_email = self.sender
        if self.console is not None:
            return int(self.console.send_messages(email_messages) or 0)
        return int(super().send_messages(email_messages) or 0)


class ConfiguredUserRateThrottle(UserRateThrottle):
    """API requests per user and time unit from the integration settings."""

    def get_rate(self) -> str:
        """The current API rate."""
        return instance_config.get().api_rate


def s3_storage(config: instance_config.Config) -> Storage:
    """The bucket storage for these settings."""
    from storages.backends.s3 import S3Storage

    store: Storage = S3Storage(
        bucket_name=config.s3_bucket,
        endpoint_url=config.s3_endpoint_url or None,
        region_name=config.s3_region or None,
        access_key=config.s3_access_key_id,
        secret_key=config.s3_secret_access_key,
        location=config.s3_prefix,
        # B2 and most other providers reject ACL headers; access is the bucket policy.
        default_acl=None,
        file_overwrite=False,
        addressing_style=config.s3_addressing_style or None,
    )
    return store


class AttachmentStorage(Storage):
    """Encrypted uploads in the media volume, or in a bucket once one is configured.

    Files written before a bucket was set stay in the volume and are still read from
    there, so switching never hides existing attachments.
    """

    def __init__(self, **local_options: Any) -> None:
        """The media volume storage; the bucket is built on first use."""
        self.local = FileSystemStorage(**local_options)
        self._remote: Storage | None = None
        self._key: tuple[str, ...] = ()
        self._lock = threading.Lock()

    def primary(self) -> Storage:
        """Where new files go: the bucket when one is configured."""
        config = instance_config.get()
        if not config.s3_bucket:
            return self.local
        key = config.storage_key()
        with self._lock:
            if self._remote is None or key != self._key:
                self._remote, self._key = s3_storage(config), key
            return self._remote

    def _holder(self, name: str) -> Storage:
        primary = self.primary()
        if primary is not self.local and not primary.exists(name) and self.local.exists(name):
            return self.local
        return primary

    def save(self, name: str | None, content: Any, max_length: int | None = None) -> str:
        """Store in the primary storage."""
        return str(self.primary().save(name, content, max_length=max_length))

    def open(self, name: str, mode: str = "rb") -> Any:
        """Open from wherever the file is."""
        return self._holder(name).open(name, mode)

    def delete(self, name: str) -> None:
        """Remove from the bucket and the volume."""
        primary = self.primary()
        primary.delete(name)
        if primary is not self.local:
            self.local.delete(name)

    def exists(self, name: str) -> bool:
        """Whether the file exists in either place."""
        return bool(self._holder(name).exists(name))

    def size(self, name: str) -> int:
        """Size of the stored file."""
        return int(self._holder(name).size(name))

    def url(self, name: str | None) -> str:
        """Attachments have no public URL."""
        raise NotImplementedError("attachments are served through the permission checked view")


@lru_cache(maxsize=8)
def _csp_with_tiles(tile_url: str) -> dict[str, list[str]]:
    base: dict[str, Any] = settings.SECURE_CSP
    policy = {key: list(values) for key, values in base.items()}
    parts = urlsplit(tile_url)
    images = [v for v in policy["img-src"] if v in (CSP.SELF, "data:")]
    # Only a plain host (as the form allows), so the value cannot add CSP directives.
    if parts.scheme == "https" and re.fullmatch(r"[A-Za-z0-9.{}*-]+(:[0-9]{1,5})?", parts.netloc):
        images.append(f"https://{parts.netloc.replace('{s}', '*')}")
    policy["img-src"] = images
    return policy


class MapTilesCspMiddleware:
    """Allow images from the map tile server set in the admin area (and no other)."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        """Standard middleware constructor."""
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        """Set the CSP with the current tile server unless a view chose one."""
        response = self.get_response(request)
        # Only pages load map tiles; metrics and health checks must not need the database.
        is_page = response.get("Content-Type", "").startswith("text/html")
        if is_page and not hasattr(response, "_csp_config"):
            response._csp_config = _csp_with_tiles(  # type: ignore[attr-defined]
                instance_config.get().map_tile_url
            )
        return response
