"""Operational settings that administrators change in the web interface.

Stored as JSON on the :class:`InstanceSettings` row; secrets inside are encrypted with the
instance keys. Anything not saved yet falls back to the environment value of the same
setting, so existing installations keep working. The stored form is cached
like the branding snapshot and dropped on every save, so all processes see a change
within one request.
"""

import logging
from dataclasses import dataclass, fields, replace
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, transaction

from patchmanager.core import crypto

logger = logging.getLogger(__name__)
CACHE_KEY = "instance:config"
CACHE_SECONDS = 60
SECRETS = frozenset({"mail_password", "s3_secret_access_key"})


@dataclass(frozen=True)
class Config:
    """Effective values; secrets decrypted."""

    # Sign in and sessions
    password_min_length: int = 12
    session_max_age_hours: int = 12
    session_idle_minutes: int = 30
    mfa_required: bool = False
    login_max_failures: int = 10
    login_lockout_minutes: int = 15
    login_rate_per_ip: int = 30
    invitation_ttl_hours: int = 72
    # Mail
    mail_host: str = ""
    mail_port: int = 587
    mail_user: str = ""
    mail_password: str = ""
    mail_use_tls: bool = True
    mail_from: str = "patchmanager@localhost"
    # Uploads and file storage
    upload_max_mb: int = 20
    clamd_host: str = ""
    clamd_port: int = 3310
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_prefix: str = "attachments"
    s3_addressing_style: str = ""
    # Maps
    map_tile_url: str = ""
    map_attribution: str = ""
    # Integrations
    webhook_allowed_networks: str = ""  # comma separated CIDRs
    webhook_allow_http: bool = False
    api_rate: str = "1200/minute"

    @property
    def upload_max_bytes(self) -> int:
        """Largest upload in bytes."""
        return self.upload_max_mb * 1024 * 1024

    @property
    def session_max_age(self) -> int:
        """Session lifetime in seconds."""
        return self.session_max_age_hours * 3600

    @property
    def session_idle_timeout(self) -> int:
        """Idle timeout in seconds."""
        return self.session_idle_minutes * 60

    @property
    def login_lockout_seconds(self) -> int:
        """Lock duration in seconds."""
        return self.login_lockout_minutes * 60

    @property
    def allowed_networks(self) -> list[str]:
        """Private networks webhooks may reach, as a list."""
        return [n.strip() for n in self.webhook_allowed_networks.split(",") if n.strip()]

    def storage_key(self) -> tuple[str, ...]:
        """Changes whenever the attachment storage must be rebuilt."""
        return (
            self.s3_bucket,
            self.s3_endpoint_url,
            self.s3_region,
            self.s3_access_key_id,
            self.s3_secret_access_key,
            self.s3_prefix,
            self.s3_addressing_style,
        )


def defaults() -> Config:
    """The environment values (``PATCHMANAGER_*``), used until an administrator saves."""
    mail: dict[str, Any] = settings.MAIL_DEFAULTS
    s3: dict[str, str] = settings.S3_DEFAULTS
    return Config(
        password_min_length=settings.PASSWORD_MIN_LENGTH,
        session_max_age_hours=max(1, settings.SESSION_MAX_AGE // 3600),
        session_idle_minutes=max(1, settings.SESSION_IDLE_TIMEOUT // 60),
        mfa_required=settings.MFA_REQUIRED_FOR_ALL,
        login_max_failures=settings.LOGIN_MAX_FAILURES,
        login_lockout_minutes=max(1, settings.LOGIN_LOCKOUT_SECONDS // 60),
        login_rate_per_ip=settings.LOGIN_RATE_PER_IP,
        invitation_ttl_hours=settings.INVITATION_TTL_HOURS,
        mail_host=mail["host"],
        mail_port=mail["port"],
        mail_user=mail["username"],
        mail_password=mail["password"],
        mail_use_tls=mail["use_tls"],
        mail_from=settings.DEFAULT_FROM_EMAIL,
        upload_max_mb=max(1, settings.UPLOAD_MAX_BYTES // (1024 * 1024)),
        clamd_host=settings.CLAMD_HOST,
        clamd_port=settings.CLAMD_PORT,
        s3_bucket=s3["bucket"],
        s3_endpoint_url=s3["endpoint_url"],
        s3_region=s3["region"],
        s3_access_key_id=s3["access_key_id"],
        s3_secret_access_key=s3["secret_access_key"],
        s3_prefix=s3["prefix"] or "attachments",
        s3_addressing_style=s3["addressing_style"],
        map_tile_url=settings.MAP_TILE_URL,
        map_attribution=settings.MAP_ATTRIBUTION,
        webhook_allowed_networks=", ".join(settings.WEBHOOK_ALLOWED_NETWORKS),
        webhook_allow_http=settings.WEBHOOK_ALLOW_HTTP,
        api_rate=settings.API_RATE,
    )


NAMES = frozenset(f.name for f in fields(Config))


def from_stored(stored: dict[str, Any]) -> Config:
    """Merge stored values over the environment defaults, decrypting secrets."""
    values: dict[str, Any] = {}
    for name, value in stored.items():
        if name not in NAMES:
            continue  # a setting that was removed in a later version
        if name in SECRETS and value:
            try:
                value = crypto.decrypt(value)
            except Exception:  # rotated keys must not take the site down
                logger.exception("stored secret %s cannot be decrypted", name)
                value = ""
        values[name] = value
    return replace(defaults(), **values)


def get() -> Config:
    """The effective settings."""
    # Only the stored form (secrets still encrypted) goes into the shared cache.
    stored: dict[str, Any] | None = cache.get(CACHE_KEY)
    if stored is None:
        from patchmanager.instance.models import InstanceSettings

        try:
            # Savepoint: a failed read must not abort the request's transaction.
            with transaction.atomic():
                row = InstanceSettings.objects.only("config").first()
        except DatabaseError:
            logger.exception("instance settings unreadable, using the environment")
            return defaults()
        stored = dict(row.config) if row else {}
        cache.set(CACHE_KEY, stored, CACHE_SECONDS)
    return from_stored(stored)


def forget() -> None:
    """Drop the cached settings after a change."""
    cache.delete(CACHE_KEY)
