# Phase backlog

Each phase ends with a squash merged PR, a GitHub release and a phase report. Items
become GitHub issues under the phase milestone when the phase starts.

## Phase 0: Foundation (v0.1.0)

* Plan: architecture, data model, backlog, feature scope
* pixi workspace with `default`, `dev`, `test`, `lint`, `docs`, `prod` environments, committed lock
* Django skeleton: env driven settings, `*_FILE` secrets, JSON logging, health/readiness
* Dockerfile (pixi build, distroless runtime, non-root, healthcheck, pinned digests)
* `compose.yaml` (dev) and `compose.prod.yaml` (prod with Caddy and backup job)
* CI: lint, typecheck, test, security (bandit, pip-audit, gitleaks), image build with Trivy,
  multi-arch push to GHCR, cosign signing, SBOM and provenance; CodeQL
* pre-commit, Renovate
* Docs site skeleton, ADRs, STRIDE threat model draft, SECURITY.md, CONTRIBUTING.md
* Branch protection on `main`

## Phase 1: Identity and tenancy (v0.2.0)

* `tenancy`: Tenant model, tenant context middleware, RLS policies and roles, tenant switcher
* `accounts`: custom User (email login), Argon2id, password policy, lockout and rate limits
* MFA: TOTP, WebAuthn and passkeys, recovery codes; enforcement per role and tenant
* Sessions: server side, idle and absolute timeout, revocation, list of active sessions
* RBAC: permission registry, roles (system and custom), nested groups, scoped assignments,
  deny rules, conditions, policy service with cache and explainer, permission matrix tests
* `audit`: hash chained security event log, verifier command
* First run setup wizard (no default credentials)
* Base UI shell: Tailwind 4, HTMX 4, Alpine, layout, dark mode toggle, i18n plumbing
* Celery worker and scheduler, mail via SMTP (Mailpit in dev)
* Playwright e2e job in CI; cross tenant isolation test suite (UI, API)

## Phase 2: Locations and racks (v0.3.0)

* Site, building, floor, room with `ltree` paths and breadcrumbs
* Map view (Leaflet, OpenStreetMap tiles, operator configurable tile URL)
* Rack model, rack occupancy with exclusion constraint, utilisation stats
* Interactive SVG rack elevation (front and rear)
* Three click navigation map, site, room

## Phase 3: Device library and devices (v0.4.0)

* Vendors, device types (rack and floor), port templates, images
* YAML/JSON template import and export, community library format adapter (license checked)
* Device instances from templates, ports generated from templates, per port fields
* Custom rack and floor devices, naming conventions
* Attachments (photos, rich text notes) on any object, upload hardening, object storage
  backend decision ([ADR 0009](../adr/0009-object-storage.md))

## Phase 4: Cabling and networks (v0.5.0)

* Cables (permanent link and patch cord) with media, colour, length, label
* Patch panels: front to rear port mapping
* Connections within rack, between racks, rooms, floors, buildings
* Path trace service, trace graph (SVG), dependency view; property based tests
* VLANs (tenant or site scope), access and tagged assignment per port
* WLANs linked to VLANs and access point radios

## Phase 5: Floor plans (v0.6.0)

* Upload image or PDF plan, rasterise PDF (SVG refused, see ADR 0017)
* Scale by known distance, room outlines (rectangle and polygon)
* Drag and drop placement of racks and floor devices, link to rooms

## Phase 6: History, search, import and export (v0.7.0)

* Change history with before/after diff and restore, per object and global activity log
* Global search (FTS and trigram) with facets and saved filters
* CSV/XLSX import wizard with validation and dry run
* Exports: PDF (site report, rack elevations, port and patch lists, BOM), CSV, XLSX, JSON,
  branded with operator logo

## Phase 7: API and integrations (v0.8.0)

* Full REST API (DRF, OpenAPI 3.1), personal API tokens (scoped, expiring), OAuth 2.0
  client credentials
* Signed webhooks for change events
* SSO: OIDC (code + PKCE), SAML 2.0, multiple IdPs, JIT provisioning, group mapping,
  domain discovery, enforce SSO with break glass admin, Single Logout, optional LDAP
* SCIM 2.0 (Users, Groups, discovery endpoints, filter, PATCH, ETags), SCIM tokens

## Phase 8: Editions and polish (v0.9.0)

* Edition presets as feature flags and limits (Free, SMB, Integrator, Enterprise);
  removed in Phase 9, every feature is free (ADR 0023)
* i18n EN and DE complete, responsive layouts, dark mode, WCAG 2.2 AA review
* Admin area polish: system settings, branding, webhooks, jobs, health and version

## Phase 9: Hardening and release (v1.0.0)

* ASVS 5 Level 2 self audit, pen test checklist
* Performance tests: 500 racks, 50 000 ports
* Backup encryption and off site shipping, restore drill in CI, PITR guide
* GDPR export and anonymisation workflow (ADR 0024)
* Remove editions (ADR 0023)
* Final docs, production compose review, v1.0.0
