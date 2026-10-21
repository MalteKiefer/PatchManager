# 11. Authorization model and policy engine

* Status: accepted
* Date: 2026-10-21

## Context

The brief asks for fine grained permissions, roles, nested groups, scoped
assignments with inheritance, attribute conditions, explicit denies and one
central decision point used by UI, API, search and exports.

## Decision

* Permissions are code: `<app>.<object>.<action>`, registered by each app in
  `AppConfig.ready` (`accounts.permissions`). They are never stored as rows, so
  they cannot drift from the code.
* System roles (Super Admin, Tenant Owner, Admin, Editor, Viewer, Auditor) are
  defined as glob include/exclude patterns in `accounts.roles` and resolved
  against the registry at runtime. Permissions added by later phases flow into
  them automatically. Their database rows only anchor assignments. Custom roles
  store explicit permission lists.
* An assignment grants (or denies) one role to one user or group at a scope.
  Scope is `NULL` (global) or an `ltree` path; a path covers itself and every
  descendant. Phase 1 objects are global or tenant scoped; later phases add
  site, building, floor, room and rack paths below the tenant label.
* Conditions are a small JSON clause list (`eq`, `ne`, `in`, `not_in`,
  `contains`, `is_actor`) over attributes each object type whitelists. Every
  clause evaluates on an object and translates to a `Q` filter, so single
  checks and list queries agree.
* Evaluation order: unusable actor denies; global Super Admin allows; any
  matching deny denies; any matching allow allows; otherwise deny. Without an
  object, conditional allows never match and conditional denies always do.
* `accounts.policy` is the only decision point: `has_perm`, `explain`,
  `scope_queryset`, `accessible_tenants`. Grants are cached per user in Redis
  and invalidated by a global version counter that every RBAC change bumps.
* No escalation: granting a role at a scope requires holding every permission
  of the role at that scope; only super admins grant Super Admin; the last
  active super admin cannot be removed.

## Consequences

Users are instance wide objects (`policy_path = NULL`): only global grants can
lock, reset or impersonate them. Tenant owners invite users into their tenant
and manage assignments there, and see only users with assignments in their
tenants.
