# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

* API tokens and OAuth clients can be limited to kinds of objects and to one location;
  the limits apply to every check of the request, also for super admins (ADR 0033).
* "Add device here" on a free rack unit or on the floor plan also offers the room's
  devices that have no place yet (not in a rack, or not on the plan).
* Servers, virtual machines and storage: clusters with hosts, virtual machines and
  containers with network cards and disks, storage of NAS, SAN and clusters
  (ADR 0032).
* Proxmox VE connections: choose nodes, machines and storage to take over, create
  servers for new nodes, update automatically; moved machines stay one record.
* One-off imports from VMware vCenter and Synology DSM.
* Switching and routing per device: link aggregations, spanning tree (with MST
  instances), VLAN interfaces with addresses, static routes, OSPF, BGP neighbors,
  tunnelled ports, device profiles, and a port matrix coloured by VLAN (ADR 0029).
* Stacks and virtual switches (stacking, VSF, VSX, MLAG, virtual chassis).
* Read configurations of Aruba AOS-CX, Cisco IOS, IOS-XE and NX-OS, Fortinet
  FortiOS, Juniper Junos, MikroTik RouterOS, HPE Comware, H3C, Huawei VRP and LANCOM
  LCOS; the platform is recognised automatically. DNS servers, routed ports and DHCP
  pools (as DHCP scopes with reservations and relays) are taken over too (ADR 0031).
* Customers and systems: organisations with contacts, systems (telephony, building
  automation, access control ...), and links that assign devices, buildings, VLANs,
  networks, configurations and more to a system or customer (ADR 0030).
* DHCP scopes with reservations (Kea export) and DNS zones with records (zone file
  export).
* Import DNS zones from zone files and from deSEC, Cloudflare and Hetzner DNS.
* Lines from providers with bandwidth, handover port and contract; device supplier,
  warranty, support and end of life.
* Read ArubaOS-Switch configurations and Aruba Central templates into the
  documentation with a preview of every change; write the documentation back as an
  ArubaOS-Switch configuration.
* Floor plan pins: floor devices are drawn as pins at their exact spot; a click shows
  the device with its room, photos, notes and the patch target of every port.

### Fixed

* Webhook pages highlighted "Import and export" in the menu instead of
  "Administration".

## [1.0.0] - 2026-10-22

### Added

* Rack view over the full page width: every port of every device drawn like on the
  hardware, coloured by the state of its path, cables routed through the side
  channels, the whole path highlighted when pointing at a port, front and rear,
  connections table, and "add a device here" on free units.
* Room cabling: all racks of a room side by side with the cables inside them and
  between them over the cable tray.
* Every object is created on its own page.
* Rooms: planned against installed devices per category, from the room type or set
  per room; every device outside the racks with each port and where it is patched;
  surveys with date, who, counts found on site, photos and notes, compared with the
  documentation (ADR 0028).
* Floor plans draw lines between placed floor devices and the racks they are
  patched to, and "Add device here" creates a device at the clicked point.
* Device categories and room types are lists administrators can extend and rename,
  sorted by name and translated; forms link to them.
* Draw a new room on the floor plan: the outline becomes the room.
* Device types: mounting, airflow, power supplies, maximum power and weight, filled
  from the device type library; the height only for rack mounted types. USB, video
  and audio ports with USB-C, HDMI, DisplayPort, DVI, VGA and audio jack connectors.
* New layout: navigation in a sidebar with the tenant on top, search in the top bar
  (Ctrl+K or /), and an overview start page with the tenant's racks, devices, ports
  and cables, its sites with their load, the fullest racks and recent changes.
* Device configurations: versions per device, encrypted at rest, every read audited,
  view with line numbers, compare, download, store by paste, file or API (ADR 0027).
* IP address management: VRFs, prefixes as a tree with usage and free ranges, the
  next free address, addresses on ports (shown on devices), search, import and
  export, REST API and history (ADR 0026).
* Device catalog: more than 12 000 device types of the community device type library
  (CC0) bundled and searchable; one click copies a type with all its ports
  (ADR 0025).
* Personal data: everyone can download their data as JSON; administrators can
  anonymise accounts for erasure requests (ADR 0024).
* Backup and restore drill on the production stack in every CI run; guides for
  encrypted off-site copies and point in time recovery.
* ASVS 5.0 Level 2 self-audit and a penetration test checklist.
* Load test in CI with 500 racks, 50 000 ports and 12 000 cables, with time and
  query budgets per page; `seed_scale` command for demo and load data.

### Security

* Deleting an object also deletes its attachments and notes; before, they stayed
  behind.
* A session that signed in with the password alone is ended when a second factor
  is enrolled elsewhere, instead of staying on the enrolment page.
* Behind the reverse proxy every request had the proxy's address, so the login
  rate limit per IP was shared by all users (one client could block sign in for
  everyone) and the audit log recorded the proxy. The client address now comes
  from `X-Forwarded-For` of trusted proxies.
* Pages for signed in users send `Cache-Control: no-store`.
* Status messages are announced without breaking their list for screen readers.
* Audit entries written while an administrator acts as another user name the
  administrator; the personal data export leaves out their devices.

### Fixed

* Search loaded the vendor of every device hit separately.
* Path tracing needed two queries per cable hop instead of one.
* The status code on error pages was below AA contrast in light mode.
* OpenStreetMap tiles were blocked because the page sent no Referer; tiles are grey.
* The device page traced every cabled port with its own queries.

### Removed

* Editions, their limits and feature switches, `PATCHMANAGER_EDITION` and the edition
  admin page. Every feature is free and available on every installation (ADR 0023).

## [0.9.0] - 2026-10-22

### Added

* Editions (Free, SMB, Integrator, Enterprise) as feature flags and limits for racks,
  users, tenants, cross rack cabling, PDF reports, API, webhooks, SSO, SCIM, import
  and floor plans; chosen and overridden by super admins or pinned by
  `PATCHMANAGER_EDITION`. Self hosted instances default to Enterprise.
* Branding: instance name, logo (also on PDF reports), brand colour and default
  language for new accounts, changed at runtime.
* System page with health checks, a background job monitor, backup restore points,
  mail settings with a test mail and license notices; every page links the source.
* Mobile menu for the header navigation.

### Changed

* Dark mode contrast and link presentation meet WCAG 2.2 AA (checked with axe-core
  on 24 pages in both themes); script texts are translated.

## [0.8.0] - 2026-10-21

### Added

* REST API under `/api/v1/` with an OpenAPI 3.1 schema: locations, racks, devices,
  ports, cables, VLANs, WLANs, the device library, the change history and search.
* Personal API tokens (scoped, expiring, optionally per tenant) and OAuth 2.0 client
  credentials with service users.
* Signed webhooks for change events with retries and an SSRF guard.
* Single sign on with OpenID Connect (PKCE), SAML 2.0 and LDAP: per domain
  providers, account creation on first sign in, group mapping, enforcement with
  break glass accounts and single logout.
* SCIM 2.0 provisioning of users and groups (filters, PATCH, ETags) with SCIM tokens;
  protected accounts and locally managed groups stay out of reach.

### Fixed

* Exports and PDF reports now require the export permission, imports the import
  permission; viewers could export before.

## [0.7.0] - 2026-10-21

### Added

* Change history for locations, racks, devices, ports, cables, VLANs and WLANs,
  recorded by database triggers: activity log with filters, per object history,
  before and after view and restore of changed fields.
* Global search across names, serial numbers, asset tags, ports, cables, VLANs, WLANs
  and notes, typo tolerant, with type facets and saved searches.
* Import wizard for locations, racks, devices, VLANs and cables from CSV or XLSX with
  a dry run, per line results and all or nothing apply; templates per table.
* Exports as CSV, XLSX and JSON (re-importable) and PDF reports: site report, rack
  elevation, port list, patch list and bill of materials, with an optional logo.

## [0.6.0] - 2026-10-21

### Added

* Floor plans per floor: upload JPEG, PNG, WebP, GIF or PDF (first page rasterised),
  replace, and set the scale from two points with a known distance.
* Room outlines drawn as polygons on the plan.
* Drag and drop placement of racks and floor devices, with keyboard moves; a floor
  device dropped into another room's outline moves to that room.

### Fixed

* Cable scopes are recomputed when a device moves to another room or rack.
* Deleting an attachment removes the stored file only after the database change is
  committed; files in use (floor plans) cannot be deleted.

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

[1.0.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v1.0.0
[0.9.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.9.0
[0.8.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.8.0
[0.7.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.7.0
[0.6.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.6.0
[0.5.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.5.0
[0.4.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.4.0
[0.3.1]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.3.1
[0.3.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.3.0
[0.2.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.2.0
[0.1.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.1.0
