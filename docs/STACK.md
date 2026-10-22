# Technology stack

Versions verified on 2026-10-21 against conda-forge (`pixi search`), PyPI, Docker Hub,
GHCR, Quay and GitHub releases. The pixi lockfile (`pixi.lock`) is the source of truth
for Python dependencies; Renovate keeps it current.

## Runtime

| Component | Version | Source | Notes |
|---|---|---|---|
| Python | 3.14.8 | conda-forge | 3.15.0 exists but Django 6.1 does not list it yet, see [ADR 0005](adr/0005-python-3-14.md) |
| Django | 6.1.2 | conda-forge | built-in CSP middleware (since 6.0) |
| psycopg | 3.3.x (C build) | conda-forge | |
| gunicorn | 26.2.0 | conda-forge | WSGI server in the image |
| whitenoise | 6.12.0 | conda-forge | static files |
| PostgreSQL | 18.6 | conda-forge (local), `postgres:18.6-trixie` (containers) | only supported database |
| Redis | 8.10.2 | conda-forge (local), `redis:8.10.2-alpine` | cache, rate limits, task broker |

Added in 0.2.0: argon2-cffi 25.1.0, pyotp 2.10.0, webauthn 3.0.1, qrcode 8.2,
cryptography (Fernet), celery 5.6.3, redis-py 8.1.0, prometheus_client 0.26.0,
pyyaml 6.0.3, gettext 0.25, playwright 1.64.1, pytest-playwright 0.10.0.

Added in 0.4.0: Pillow 13.0.0 (image re-encoding), nh3 0.3.7 (HTML sanitising),
markdown-it-py 4.2.0 (notes), defusedxml 0.7.1 (SVG parsing).

Added in 0.6.0: pypdfium2 5.13.0 (PDF floor plans to PNG).

Added in 0.7.0: reportlab 5.0.1 (PDF reports), openpyxl 3.1.5 (XLSX import and export).

Added in 0.8.0: djangorestframework 3.18.3, drf-spectacular 0.30.0 (OpenAPI 3.1),
djangorestframework-stubs 3.18.1 (type checking), joserfc 1.7.7 (OIDC ID tokens),
requests 2.34.2, pysaml2 7.5.5 with libxmlsec1 1.3.12 (SAML), python-ldap 3.4.8.

Test only since 0.9.0: axe-core 4.12.1 (MPL-2.0, `tests/e2e/vendor/axe-4.12.1.min.js`,
SHA-256 `66a8aaa95a8b044a7fd74a5435873bf04ff65a1ca75567c921b7509742085a14`, matches the
cdnjs SRI `sha512-7Y+2K9ZP/j7Cahfb4lm6+HC7pkGsAf9i2sI2FzHKQwFBzoUXC0is8UXcm5o05wgsSCSnbwvkn/G0aoXbJDKRwQ==`).

Front end assets (self hosted, no CDN at runtime):

| Asset | Version | Integrity |
|---|---|---|
| HTMX | 4.0.0 | `sha256:e484d9171a9db30a39c8f16e3d709d4137f3211c659f8e6125816635033d593f` (vendored) |
| Alpine.js CSP build | 3.17.5 | `sha256:48c170c92032e527cbae71b8ce75cf1f96ca06fa869a6120ef0fe438d8e92c36` (vendored) |
| Tailwind CSS standalone CLI | 4.3.3 | checksums pinned in `scripts/tailwind.py` |
| Leaflet (JS, CSS, images) | 1.9.4 | JS `sha256:db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a`, CSS `sha256:a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6`, source map `sha256:600a10dc5cd110de0699510d322afcbe01c7ca90b4c5f48adc20314c70aac753` (vendored) |

`django-otp` was not used: TOTP needs a few lines on top of pyotp, and WebAuthn
comes from py_webauthn directly.

## Planned per phase (not yet in the lockfile)

| Component | Latest stable | Phase |
|---|---|---|
| Django REST Framework | 3.18.3 | 7 |
| drf-spectacular (OpenAPI 3.1) | 0.30.0 | 7 |
| WeasyPrint | 69.0 | 6 |
| openpyxl | 3.1.5 | 6 |
| mozilla-django-oidc / python3-saml | 5.0.2 / 1.16.0 | 7 |

## Tooling

| Tool | Version |
|---|---|
| pixi | 0.82.1 |
| ruff | 0.17.1 |
| mypy / django-stubs | 2.4.0 / 6.1.2 |
| pytest / pytest-django / pytest-cov | 9.1.1 / 4.13.0 / 7.1.0 |
| hypothesis / factory_boy | 6.170.5 / 3.3.3 |
| bandit / pip-audit | 1.9.4 / 2.10.1 |
| pre-commit / gitleaks | 4.7.0 / 8.30.1 |
| mkdocs-material | 9.7.7 |

## Containers and infrastructure

| Image | Tag | Use |
|---|---|---|
| `ghcr.io/prefix-dev/pixi` | 0.82.1-trixie-slim | build stage |
| `gcr.io/distroless/base-debian13` | nonroot | runtime stage |
| `caddy` | 2.11.7-alpine | reverse proxy, automatic TLS |
| `axllent/mailpit` | v1.31.6 | dev mail sink |
| `quay.io/keycloak/keycloak` | 26.8.1 | dev identity provider |

All images are pinned by digest in `Dockerfile`, `compose.yaml` and `compose.prod.yaml`.
MinIO no longer publishes container images, see [ADR 0009](adr/0009-object-storage.md).

## GitHub Actions (pinned by commit SHA)

actions/checkout v7.0.1, prefix-dev/setup-pixi v0.11.2, docker/setup-qemu-action v4.4.0,
docker/setup-buildx-action v4.4.1, docker/metadata-action v6.2.0,
docker/build-push-action v7.4.0, docker/login-action v4.6.0,
sigstore/cosign-installer v4.1.2, aquasecurity/trivy-action v0.36.0,
gitleaks/gitleaks-action v3.0.0, github/codeql-action v4.39.0,
actions/upload-artifact v7.0.2.
