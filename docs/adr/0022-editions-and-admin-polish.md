# 22. Editions, instance settings and accessibility

* Status: accepted; the edition part is superseded by ADR 0023 (no editions)
* Date: 2026-10-22

## Context

The feature scope asks for the commercial tier logic of the reference product as
configurable feature flags and limits (Free: 3 racks, 1 user, connections inside
a rack, bill of materials as the only PDF; SMB: unlimited racks and users;
Integrator: several tenants; Enterprise: no limits), without billing. Phase 8
also asks for runtime system settings, branding, a job monitor, health and
version information, backup status, license notices, complete EN/DE texts,
responsive layouts, dark mode and a WCAG 2.2 AA review.

## Decision

### Editions

* Presets in code (`instance.editions`): limits for racks, active users and
  tenants (whole instance), whether connections may leave a rack, which PDF
  reports exist, and features (REST API, webhooks, SSO, SCIM, import, floor
  plans).

  | Edition | Racks | Users | Tenants | Cabling | PDF reports | Features |
  |---|---|---|---|---|---|---|
  | Free | 3 | 1 | 1 | inside one rack | bill of materials | floor plans |
  | SMB | unlimited | unlimited | 1 | everywhere | all | all but SCIM |
  | Integrator | unlimited | unlimited | 50 | everywhere | all | all |
  | Enterprise | unlimited | unlimited | unlimited | everywhere | all | all |

* Self hosted instances run **Enterprise by default**: an AGPL installation is
  not artificially limited. Super admins choose the edition and override single
  limits and features (`system.settings.change`); `PATCHMANAGER_EDITION` pins the
  edition for operators who sell hosted plans. Billing stays out of scope.
* Limits are enforced where objects come into being (racks in the tree service,
  users in the user manager, which covers invitations, JIT and SCIM, and on
  reactivation; tenants in the tenant service). Existing data is never deleted
  when an edition shrinks; only new objects are refused. Features are gated at
  their views, API permission, OAuth and SCIM endpoints, the webhook sender
  (deliveries wait) and the SSO lookup (also LDAP and enforcement). Hidden
  features disappear from menus.

### Instance settings

* One `InstanceSettings` row, cached for a minute: edition and overrides,
  instance name (falls back to `PATCHMANAGER_INSTANCE_NAME`), logo (PNG or JPEG up
  to 512 KB, re-encoded to a PNG of at most 512 pixels, served with a sandbox CSP
  and used on PDF reports), brand colour as an OKLCH hue (the lightness and
  chroma of the default scale keep the contrast, served as `/branding.css`) and
  the default language of new accounts. If the database cannot be read, pages
  fall back to the defaults so error pages still render.
* SMTP stays configured through the environment (secrets do not belong in the
  database); the system page shows backend, server and sender and sends a test
  mail.

### Operations pages

* Health: database, pending migrations, cache, file storage (write probe) and
  Celery workers (ping), each with its duration.
* Job monitor: Celery signals record every run (task, start, end, status, error
  class only, since messages may hold data), kept three days; the page shows
  recent runs, failures in 24 hours, the beat schedule and webhook backlog.
* Backups: the backup volume is mounted read only into the app
  (`PATCHMANAGER_BACKUP_DIR`); the page lists restore points and warns when the
  newest is older than 36 hours. Restoring stays a server task.
* License notices list the Python packages (from their metadata) and the
  vendored browser components; every page links the source code
  (`PATCHMANAGER_SOURCE_URL`) as AGPL section 13 requires for network use.

### Accessibility and responsiveness

* Browser tests run axe-core 4.12.1 (test only, MPL-2.0, checksum pinned) on 24
  pages in light and dark mode for WCAG 2.0, 2.1 and 2.2 A and AA rules and fail
  on serious or critical findings; another test checks the phone layout (375
  pixels, no horizontal scrolling, menu). Fixes: dark mode secondary text and
  the current navigation item reach 4.5:1, a link no longer sits in running text
  without a distinction, and the header collapses into a menu below 1024 pixels
  (progressive: without JavaScript the navigation simply wraps).
* Texts used by scripts come from a translated data element in the layout, so
  both languages cover the scripts too. A unit test fails when the German
  catalog has untranslated or fuzzy entries.

## Consequences

* The automated check covers what axe can detect. A manual review of keyboard
  paths and screen reader output is part of the Phase 9 audit.
