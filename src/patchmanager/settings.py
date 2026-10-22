"""Django settings for PatchManager.

All configuration comes from environment variables. Every secret can also be
read from a file by setting ``<NAME>_FILE`` (Docker secrets).
"""

import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent


def env(name: str, default: str | None = None) -> str:
    """Return an environment value, preferring ``<name>_FILE`` when set."""
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        return Path(file_path).read_text(encoding="utf-8").strip()
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f"Environment variable {name} is required.")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment value."""
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    """Parse a comma separated environment value."""
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


def database_from_url(url: str) -> dict[str, object]:
    """Build a Django ``DATABASES`` entry from a ``postgresql://`` URL."""
    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql"}:
        raise ImproperlyConfigured("DATABASE_URL must use the postgresql:// scheme.")
    query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
    host = query.pop("host", parts.hostname or "")
    port = query.pop("port", str(parts.port or ""))
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or "") or env("DATABASE_PASSWORD", ""),
        "HOST": host,
        "PORT": port,
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": query,
    }


DEBUG = env_bool("PATCHMANAGER_DEBUG")
SECRET_KEY = env("DJANGO_SECRET_KEY")
if len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be at least 50 characters.")

# "localhost" stays allowed for the container HEALTHCHECK; the proxy only
# forwards requests for the configured public host.
ALLOWED_HOSTS = [*env_list("PATCHMANAGER_ALLOWED_HOSTS", "127.0.0.1"), "localhost"]
CSRF_TRUSTED_ORIGINS = env_list("PATCHMANAGER_CSRF_TRUSTED_ORIGINS")

TESTING = env_bool("PATCHMANAGER_TESTING")
BASE_URL = env(
    "PATCHMANAGER_BASE_URL", "http://localhost:8000" if DEBUG or TESTING else None
).rstrip("/")
INSTANCE_NAME = env("PATCHMANAGER_INSTANCE_NAME", "PatchManager")
BRAND_LOGO = env("PATCHMANAGER_BRAND_LOGO", "")  # PNG or JPEG file shown on exported PDFs
BACKUP_DIR = env(
    "PATCHMANAGER_BACKUP_DIR", ""
)  # read only mount of the backup volume, for the status page
SOURCE_URL = env(
    "PATCHMANAGER_SOURCE_URL", "https://github.com/MalteKiefer/PatchManager"
)  # AGPL section 13
DJANGO_ADMIN_ENABLED = env_bool("PATCHMANAGER_DJANGO_ADMIN", DEBUG)

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "patchmanager.core",
    "patchmanager.tenancy",
    "patchmanager.accounts",
    "patchmanager.audit",
    "patchmanager.locations",
    "patchmanager.metadata",
    "patchmanager.library",
    "patchmanager.dcim",
    "patchmanager.networks",
    "patchmanager.ipam",
    "patchmanager.cabling",
    "patchmanager.floorplans",
    "patchmanager.history",
    "patchmanager.search",
    "patchmanager.exchange",
    "rest_framework",
    "drf_spectacular",
    "patchmanager.api",
    "patchmanager.integrations",
    "patchmanager.sso",
    "patchmanager.scim",
    "patchmanager.instance",
]
if DJANGO_ADMIN_ENABLED:
    INSTALLED_APPS.insert(0, "django.contrib.admin")

MIDDLEWARE = [
    "patchmanager.core.middleware.ClientIPMiddleware",
    "patchmanager.core.middleware.RequestIDMiddleware",
    "patchmanager.core.metrics.MetricsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "patchmanager.core.middleware.PrivateCacheMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "patchmanager.accounts.middleware.SessionPolicyMiddleware",
    "patchmanager.accounts.middleware.SetupRequiredMiddleware",
    "patchmanager.accounts.middleware.MFAEnforcementMiddleware",
    "patchmanager.tenancy.middleware.TenantContextMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# Identity
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["patchmanager.accounts.backends.EmailBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:home"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",  # Argon2id
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",  # verify legacy hashes only
]
PASSWORD_MIN_LENGTH = int(env("PATCHMANAGER_PASSWORD_MIN_LENGTH", "12"))
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": PASSWORD_MIN_LENGTH},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
SESSION_ENGINE = "patchmanager.accounts.sessions"
SESSION_COOKIE_NAME = "__Host-pm_session" if not DEBUG else "pm_session"
CSRF_COOKIE_NAME = "__Host-pm_csrf" if not DEBUG else "pm_csrf"
SESSION_MAX_AGE = int(env("PATCHMANAGER_SESSION_MAX_AGE", str(12 * 3600)))
SESSION_IDLE_TIMEOUT = int(env("PATCHMANAGER_SESSION_IDLE_TIMEOUT", str(30 * 60)))
SESSION_COOKIE_AGE = SESSION_MAX_AGE
MFA_REQUIRED_FOR_ALL = env_bool("PATCHMANAGER_MFA_REQUIRED")
LOGIN_MAX_FAILURES = int(env("PATCHMANAGER_LOGIN_MAX_FAILURES", "10"))
LOGIN_LOCKOUT_SECONDS = int(env("PATCHMANAGER_LOGIN_LOCKOUT_SECONDS", "900"))
LOGIN_RATE_PER_IP = int(env("PATCHMANAGER_LOGIN_RATE_PER_IP", "30"))  # per 10 minutes
INVITATION_TTL_HOURS = int(env("PATCHMANAGER_INVITATION_TTL_HOURS", "72"))
PATCHMANAGER_ENCRYPTION_KEYS = env_list("PATCHMANAGER_ENCRYPTION_KEYS")
if not PATCHMANAGER_ENCRYPTION_KEYS:
    raise ImproperlyConfigured("PATCHMANAGER_ENCRYPTION_KEYS must contain at least one Fernet key.")
WEBAUTHN_RP_ID = env("PATCHMANAGER_WEBAUTHN_RP_ID", urlsplit(BASE_URL).hostname or "localhost")
WEBAUTHN_ORIGIN = BASE_URL

# Cache, rate limits and task broker
REDIS_URL = env("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}
    }
elif DEBUG or TESTING:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
else:
    raise ImproperlyConfigured("REDIS_URL is required in production (rate limits, cache, jobs).")
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_TASK_ALWAYS_EAGER = not REDIS_URL
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BEAT_SCHEDULE = {
    "clear-expired-sessions": {
        "task": "patchmanager.accounts.tasks.clear_expired_sessions",
        "schedule": 86400,
    },
    "deliver-webhooks": {"task": "patchmanager.integrations.tasks.deliver", "schedule": 10},
    "prune-webhook-deliveries": {
        "task": "patchmanager.integrations.tasks.prune",
        "schedule": 86400,
    },
    "prune-job-runs": {"task": "patchmanager.instance.tasks.prune_job_runs", "schedule": 86400},
}

# Webhooks: only public addresses unless listed here (CIDRs, e.g. "10.0.0.0/8")
WEBHOOK_ALLOWED_NETWORKS = env_list("PATCHMANAGER_WEBHOOK_ALLOWED_NETWORKS")
WEBHOOK_ALLOW_HTTP = env_bool("PATCHMANAGER_WEBHOOK_ALLOW_HTTP", False)

# Mail
_MAIL_BACKEND = env(
    "PATCHMANAGER_EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
    if DEBUG
    else "django.core.mail.backends.smtp.EmailBackend",
)
MAILERS = {
    "default": {
        "BACKEND": _MAIL_BACKEND,
        "OPTIONS": {
            "host": env("PATCHMANAGER_EMAIL_HOST", "localhost"),
            "port": int(env("PATCHMANAGER_EMAIL_PORT", "587")),
            "username": env("PATCHMANAGER_EMAIL_USER", ""),
            "password": env("PATCHMANAGER_EMAIL_PASSWORD", ""),
            "use_tls": env_bool("PATCHMANAGER_EMAIL_USE_TLS", True),
            "timeout": 20,
        }
        if _MAIL_BACKEND.endswith("smtp.EmailBackend")
        else {},
    }
}
DEFAULT_FROM_EMAIL = env("PATCHMANAGER_EMAIL_FROM", "patchmanager@localhost")

ROOT_URLCONF = "patchmanager.urls"
WSGI_APPLICATION = "patchmanager.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "patchmanager.core.context_processors.ui",
            ],
        },
    },
]

DATABASES = {"default": database_from_url(env("DATABASE_URL"))}
# Each request runs in one transaction: changes and their audit events commit together.
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en"
LANGUAGES = [("en", "English"), ("de", "Deutsch")]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(env("PATCHMANAGER_STATIC_ROOT", str(BASE_DIR.parent.parent / "staticfiles")))
MEDIA_ROOT = Path(env("PATCHMANAGER_MEDIA_ROOT", str(BASE_DIR.parent.parent / "media")))
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
    # Encrypted uploads. Never served directly: see metadata.views.download.
    "attachments": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {
            "location": MEDIA_ROOT / "attachments",
            "base_url": None,
            "file_permissions_mode": 0o600,
            "directory_permissions_mode": 0o700,
        },
    },
}
UPLOAD_MAX_BYTES = int(env("PATCHMANAGER_UPLOAD_MAX_MB", "20")) * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = UPLOAD_MAX_BYTES + 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
CLAMD_HOST = env("PATCHMANAGER_CLAMD_HOST", "")
CLAMD_PORT = int(env("PATCHMANAGER_CLAMD_PORT", "3310"))

# Map view. Tiles load directly from the browser; set an empty URL to disable maps.
MAP_TILE_URL = env("PATCHMANAGER_MAP_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
MAP_ATTRIBUTION = env("PATCHMANAGER_MAP_ATTRIBUTION", "© OpenStreetMap contributors")
_tiles = urlsplit(MAP_TILE_URL)
_MAP_ORIGIN = f"{_tiles.scheme}://{_tiles.netloc.replace('{s}', '*')}" if MAP_TILE_URL else ""

# Security headers. TLS terminates at the reverse proxy, which sets
# X-Forwarded-Proto; only trust it behind that proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Proxies whose X-Forwarded-For names the client (same variable gunicorn reads; "*" = any).
FORWARDED_ALLOW_IPS = env_list("PATCHMANAGER_FORWARDED_ALLOW_IPS", "127.0.0.1")
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REDIRECT_EXEMPT = [r"^health/", r"^metrics$"]
SECURE_HSTS_SECONDS = 0 if DEBUG else 63072000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = env_bool("PATCHMANAGER_HSTS_PRELOAD")
# Preload affects every subdomain of the operator's domain, so it is opt-in.
SILENCED_SYSTEM_CHECKS = [] if SECURE_HSTS_PRELOAD else ["security.W021"]
# Emails are stored lowercased and unique through a functional constraint on lower(email).
SILENCED_SYSTEM_CHECKS.append("auth.W004")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_HTTPONLY = True
CSRF_FAILURE_VIEW = "patchmanager.core.views.csrf_failure"
X_FRAME_OPTIONS = "DENY"
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", *([_MAP_ORIGIN] if _MAP_ORIGIN else [])],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
    "connect-src": [CSP.SELF],
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "patchmanager.core.logging.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": env("PATCHMANAGER_LOG_LEVEL", "INFO")},
    # pysaml2 logs whole assertions (personal data) below WARNING; xmlschema is chatty
    "loggers": {"saml2": {"level": "WARNING"}, "xmlschema": {"level": "WARNING"}},
}

# REST API (ADR 0019)
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "patchmanager.api.authentication.TokenAuthentication",
        "rest_framework.authentication.SessionAuthentication",  # enforces CSRF
    ],
    "DEFAULT_PERMISSION_CLASSES": ["patchmanager.api.core.TokenScopes"],
    "DEFAULT_PAGINATION_CLASS": "patchmanager.api.core.Pagination",
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": env("PATCHMANAGER_API_RATE", "1200/minute")},
    "EXCEPTION_HANDLER": "patchmanager.api.core.exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
}
SPECTACULAR_SETTINGS = {
    "TITLE": "PatchManager API",
    "VERSION": "1",
    "OAS_VERSION": "3.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/v1",
}
