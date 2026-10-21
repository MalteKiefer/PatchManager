# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-10-21

### Added

* Location tree: sites (address, map position), buildings, floors, rooms, racks,
  each tenant isolated by Row Level Security and kept consistent by database
  triggers (parent tenant, scope path).
* Map view with Leaflet and OpenStreetMap (configurable tile server, can be disabled);
  pick coordinates on the map.
* Site page with the full building, floor and room tree; breadcrumbs everywhere.
* Racks with height, width, depth, unit numbering, status, serial and asset tag;
  interactive front and rear elevation; reservations by unit range and face with
  drag and drop and keyboard moves; utilisation per face.
* Overlap free rack space enforced by a PostgreSQL exclusion constraint; property
  based test against a Python model.
* Move a site with everything in it to another tenant.
* Role assignments scoped to a site, building, floor, room or rack.

## [0.2.0] - 2026-10-21

### Added

* Tenants with archive, restore and confirmed delete; tenant switcher; per tenant MFA requirement.
* PostgreSQL Row Level Security on tenant data (`FORCE`, non-superuser application role,
  per request tenant context) with isolation tests.
* Email login with Argon2id, password policy, account lockout and per IP rate limits.
* MFA: TOTP, WebAuthn security keys and passkeys (including passwordless sign in),
  single use recovery codes; enforcement per role, tenant or instance.
* Server side sessions with idle and absolute timeouts, session list and revocation.
* RBAC: code defined permissions, six immutable system roles, custom roles with
  clone and JSON/YAML import/export, nested groups with cycle protection, global or
  tenant scoped assignments, deny rules, attribute conditions, central cached policy
  engine with explainer and permission matrix.
* Append only, SHA-256 hash chained audit log with viewer, CSV export and verifier.
* First run setup with a one time token; invitations by email.
* Admin area: users, groups, roles, assignments, tenants, audit log, system info;
  impersonation with banner, time limit and audit.
* UI shell: Tailwind CSS 4, HTMX 4, Alpine.js (CSP build), dark mode, English and German.
* Celery worker and scheduler, Prometheus metrics endpoint, Playwright end to end tests.

### Changed

* Production compose: separate database superuser, worker, scheduler, new secrets
  (`postgres_superuser_password`, `encryption_keys`, `email_password`).

## [0.1.0] - 2026-10-21

### Added

* Project plan: architecture, data model, phase backlog, feature scope notes.
* pixi workspace with `default`, `dev`, `test`, `lint`, `docs` and `prod` environments.
* Django 6.1 skeleton: environment driven settings with Docker secret files, security
  headers and CSP, JSON logging, `/health/live` and `/health/ready`.
* Multi-stage Dockerfile (pixi build, distroless non-root runtime) and dev and prod
  compose stacks with Caddy and a daily backup job.
* CI: lint, typecheck, tests on PostgreSQL 18, bandit, pip-audit, gitleaks, Trivy,
  multi-arch GHCR images with SBOM, provenance and cosign signatures; CodeQL.
* pre-commit hooks, Renovate configuration.
* Documentation site, ADRs 0001 to 0010, STRIDE threat model draft, security policy.

[0.3.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.3.0
[0.2.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.2.0
[0.1.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.1.0
