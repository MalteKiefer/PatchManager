# 3. PostgreSQL only, shared schema tenancy with Row Level Security

* Status: accepted
* Date: 2026-10-21

## Context

Tenant isolation is the most important security property. We also want database
features that SQLite cannot provide: RLS, exclusion constraints, `ltree`, full text
search with `pg_trgm`, `JSONB`.

## Decision

PostgreSQL 18 is the only supported database, also in tests. Tenancy uses one schema:
every tenant scoped row has `tenant_id`, enforced by application scoping and by RLS
policies reading a per transaction setting. The application role owns no tables and has
no `BYPASSRLS`; migrations use a separate owner role.

Schema per tenant was rejected: it complicates migrations, cross tenant search for
integrators, and connection pooling, without stronger guarantees than RLS.

## Consequences

Every request runs in a transaction that sets the tenant setting with `SET LOCAL`.
Background jobs must set it explicitly. Tests cover RLS with application scoping
disabled.
