# 23. No editions: every feature for everyone

* Status: accepted (supersedes the edition part of ADR 0022)
* Date: 2026-10-22

## Context

ADR 0022 introduced editions (Free, SMB, Integrator, Enterprise) as presets of
limits and feature flags, following the feature scope. The product owner decided
that PatchManager stays free in every respect: no payments, no licence keys, no
tiers, and nothing that could be read as a paid upgrade.

## Decision

* Editions, their limits (racks, users, tenants), the cross rack cabling switch,
  the report and feature gates, `PATCHMANAGER_EDITION` and the edition admin page
  are removed. Every installation offers every feature without limits.
* Branding, the default language, the system page, the job monitor, backup status
  and license notices from ADR 0022 stay.
* No licence checks, payment integrations or feature locks will be added.

## Consequences

* The data model drops the edition fields (migration `instance.0003`).
* Operators who want to restrict users do so with roles and permissions, which
  already scope every object.
