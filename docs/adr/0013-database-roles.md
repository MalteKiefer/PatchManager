# 13. Database roles for Row Level Security

* Status: accepted
* Date: 2026-10-21

## Context

PostgreSQL skips RLS for superusers and `BYPASSRLS` roles. The official image
makes `POSTGRES_USER` a superuser.

## Decision

* The cluster superuser is `postgres`; it is used only by the init script and
  the backup job (`pg_dump` must see all rows).
* The application connects as `patchmanager`: `NOSUPERUSER NOBYPASSRLS`, owner
  of its database. Tables use `FORCE ROW LEVEL SECURITY`, so policies bind the
  table owner too. Migrations run as the same role; `ltree` and `pg_trgm` are
  trusted extensions the owner may create.
* Tenant visibility is a session setting set at the start of each request
  (`TenantContextMiddleware`) and cleared at its end and on every new
  connection. Background work uses `tenant_context` or `system_context`.
* Local development and tests use the same split (`scripts/db.sh`), and a test
  asserts the application role is neither superuser nor `BYPASSRLS`.

## Consequences

A separate migration owner role (so the runtime role could not alter tables)
is a further hardening step for Phase 9.
