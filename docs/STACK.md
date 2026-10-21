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

## Planned per phase (not yet in the lockfile)

| Component | Latest stable | Phase |
|---|---|---|
| Django REST Framework | 3.18.3 | 1 |
| drf-spectacular (OpenAPI 3.1) | 0.30.0 | 1 |
| Celery | 5.6.3 | 1 |
| django-otp / webauthn | 1.7.5 / 3.0.1 | 1 |
| HTMX | 4.0.0 | 1 |
| Alpine.js | 3.17.5 | 1 |
| Tailwind CSS | 4.3.3 | 1 |
| Playwright (Python) | 1.64.0 | 1 |
| Leaflet | 1.9.4 | 2 |
| nh3 (HTML sanitising) | 0.3.7 | 3 |
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
