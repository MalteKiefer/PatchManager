# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.5.0] - 2026-10-21

### Added

* Cables (permanent links and patch cords) with media, colour, length, label and status,
  within racks and across rooms, floors, buildings and sites of a tenant; port picker
  with free ports; cable list with filters.
* End to end path tracing through patch panels, including MPO style rear ports with
  positions; graph and hop list; complete, incomplete and loop states; dependency view
  on device pages. Property based tests on random panel chains.
* VLANs per tenant or site, access and trunk assignment on ports; WLANs with SSID,
  security and VLAN, broadcast by access point radios.
* Database guards: cables, VLAN use and WLAN radios cannot cross tenants; site VLANs
  only on ports of that site.

### Changed

* Viewer and Auditor roles may trace paths.
* Moving a site between tenants is refused while cables leave the site or tenant wide
  VLANs/WLANs are in use; cables and site VLANs inside it move along.

## [0.4.0] - 2026-10-21

### Added

* Device library: vendors, shared and tenant private device types (rack and floor),
  naming conventions, port templates with front to rear mapping (one to one and
  cassette style), images, copy as custom type.
* Import and export of device types in the community device type library format
  (YAML, JSON).
* Devices in racks (unit position, face, full depth) and in rooms (floor devices) with
  serial, asset tag, dates, responsible person; ports generated from the type and
  documented individually; placement changes; devices in the rack elevation.
* Photos, PDFs and Markdown notes on sites, buildings, floors, rooms, racks, devices,
  ports and device types.
* Upload hardening: content sniffing allow list, image re-encoding (EXIF and trailing
  data removed), SVG sanitising, size and pixel limits, optional clamd scan (fails
  closed), encryption at rest, permission checked downloads with a sandboxing CSP.
* Media volume in the image and prod compose; the backup job archives uploads.

## [0.3.1] - 2026-10-21

### Fixed

* End to end test waited too little for the rack elevation script, which failed the
  0.3.0 release build; no container image was published for 0.3.0. Use 0.3.1.

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

[0.5.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.5.0
[0.4.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.4.0
[0.3.1]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.3.1
[0.3.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.3.0
[0.2.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.2.0
[0.1.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.1.0
