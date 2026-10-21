# Administration

The administration area lives at `/manage/`. The raw Django admin is disabled
unless `PATCHMANAGER_DJANGO_ADMIN=true` and is then limited to super admins.

## First run

No account exists after installation. On the server run:

```bash
docker compose -f compose.prod.yaml exec app /app/.pixi/envs/prod/bin/python -m django setup_token
```

Open the instance, enter the token (valid for one hour) and create the first
administrator. This account is a Super Admin and must add a second factor
before it can do anything else.

## Tenants

A tenant is one customer. Every documented object belongs to exactly one
tenant, enforced both by the application and by PostgreSQL Row Level
Security. Tenants can require MFA for everyone who has access to them.
Archiving hides a tenant and its data; deleting requires an archived tenant and
typing its identifier.

## Users, groups and roles

* **Invite** users with an initial role, globally or for one tenant. They get
  an email link (valid 72 hours) to set a password.
* **Groups** collect users and may be nested in other groups. Assignments to a
  group apply to every member of it and of its nested groups.
* **Roles** bundle permissions. The six system roles cannot be changed; clone
  one to adapt it. Custom roles can be exported and imported as JSON or YAML.
* **Assignments** connect a user or group to a role, globally or within a
  tenant. A **deny** assignment removes the role's permissions even if another
  assignment grants them. Optional conditions restrict an assignment to
  objects whose attributes match.

| Role | Intended for |
|---|---|
| Super Admin | Instance operators. Everything, including system settings. |
| Tenant Owner | Customer owners. Everything inside their tenant, including members. |
| Admin | All documentation objects; no identity or system settings. |
| Editor | Create, change and delete documentation objects. |
| Viewer | Read documentation objects. |
| Auditor | Read everything, including the audit log; export and verify it. |

Nobody can grant permissions they do not hold at that scope, only super admins
grant Super Admin, and the last super admin cannot be removed.

**Explain access** answers "why can or can't this user do X here?" by showing
the deciding assignment and every assignment considered.

## Account actions

Lock or unlock, deactivate or reactivate, reset second factors, sign the user
out everywhere, or impersonate them (with a reason; a banner stays visible, it
ends after 30 minutes and is audited). Super admins cannot be impersonated.

## Audit log

Security events (logins, failures, lockouts, MFA changes, role and assignment
changes, tenant changes, exports, impersonation) are written to an append only
table. Each event stores the hash of its predecessor, so altering or deleting
events breaks the chain. **Verify integrity** in the audit log, or run
`python -m django verify_audit_chain`, recomputes it. The CSV export neutralises
spreadsheet formulas.

## Security settings

Set through environment variables, shown read only under **System**:

| Variable | Default | Meaning |
|---|---|---|
| `PATCHMANAGER_MFA_REQUIRED` | `false` | Require MFA for every user |
| `PATCHMANAGER_PASSWORD_MIN_LENGTH` | `12` | Minimum password length |
| `PATCHMANAGER_SESSION_MAX_AGE` | `43200` | Absolute session lifetime (s) |
| `PATCHMANAGER_SESSION_IDLE_TIMEOUT` | `1800` | Idle timeout (s) |
| `PATCHMANAGER_LOGIN_MAX_FAILURES` | `10` | Failures before lockout |
| `PATCHMANAGER_LOGIN_LOCKOUT_SECONDS` | `900` | Lockout duration (s) |
| `PATCHMANAGER_LOGIN_RATE_PER_IP` | `30` | Login attempts per IP per 10 minutes |
| `PATCHMANAGER_INVITATION_TTL_HOURS` | `72` | Invitation link validity |
| `PATCHMANAGER_METRICS_TOKEN` | unset | Bearer token for `/metrics` (404 when unset) |
