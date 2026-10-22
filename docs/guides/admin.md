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
* **Assignments** connect a user or group to a role, globally, within a
  tenant, or below a site, building, floor, room or rack (the scope covers
  everything inside it). A **deny** assignment removes the role's permissions even if another
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

**Anonymise** answers an erasure request: email and name are replaced, the account
is deactivated, and its sessions, second factors, tokens, sign in links, groups and
roles are deleted. Its changes stay in the history under the anonymous account. The
audit log keeps its entries as the security record; see
[ADR 0024](../adr/0024-personal-data.md). This cannot be undone.

## Single sign on

*Administration → Single sign on* (super admins) adds identity providers:

* **OpenID Connect**: enter the issuer URL, client id and secret, and register the
  redirect URI shown on the provider page at your provider.
* **SAML 2.0**: paste the IdP metadata XML, then import the SP metadata from the
  URL shown on the provider page into your IdP. Assertions must be signed.
* **LDAP**: `ldaps://` URI (or `ldap://` with StartTLS), bind DN and password, user
  base and a filter with `{email}`. Users of the provider's domains then sign in
  with their directory password in the normal form.

*Email domains* tell which users a provider is responsible for. Only for these
domains may a provider take over existing accounts; with *Create accounts on
first sign in* new people get an account automatically. *Group mapping* lines
like `network-admins = Network team` keep local groups in step with the provider.
*Enforce* sends users of these domains to the provider instead of the password
form. Mark at least one super admin as *break glass account* (on the user page)
so you can still sign in when the provider is down. Unless *The provider
enforces MFA* is set, users still need their second factor here.

## SCIM provisioning

*Administration → SCIM provisioning* (super admins) creates tokens for your
identity provider. Enter the base URL `https://<host>/scim/v2/` and the token in
the provider's provisioning settings (Microsoft Entra ID, Okta and others).
Provisioned users have no password and sign in through single sign on. SCIM only
manages accounts it created; a token can be limited to email domains.
Deprovisioned users are deactivated and keep their history. Provisioned groups
start without rights: assign roles to them under *Groups*. SCIM never touches
super admins, break glass accounts, API clients or groups created here.

## Lists

*Administration → Lists* holds the device categories and room types everyone
chooses from. Add your own entries or rename existing ones. A category has a short
sign and a colour for floor plan pins. A room type lists the devices a room of that
type should have; rooms can change the numbers for themselves. Built in entries keep
their key, entries in use cannot be deleted.

## Branding

*Administration → Branding* sets the instance name, logo, brand colour and the
default language of new accounts. Every feature is available on every
installation; there are no editions, limits or licence keys.

## Settings

*Administration → Settings* holds what used to need the environment, in five parts:

* **Sign in**: minimum password length, a second factor for everyone, session
  lifetime and idle timeout, lockout after failed sign ins, attempts per address and
  how long invitation links stay valid.
* **Mail**: SMTP server, port, STARTTLS, user, password and sender, with a test mail.
* **Files**: largest upload (up to 25 MB), the clamd virus scanner and S3 compatible
  object storage (AWS S3, Backblaze B2, Wasabi). Saving a bucket writes and deletes a
  test file first, so a wrong name or key is reported right away. Files stored before
  stay readable from the media volume.
* **Maps**: tile server and attribution, or none.
* **Integrations**: private networks webhooks may reach, plain http webhooks and the
  API rate per user.

Changes apply to all processes within a request and are recorded in the audit log
(setting names only). Passwords and keys are stored encrypted with the instance keys
and never shown again; leave the field empty to keep them. The off-site copy of
backups keeps its own credentials on the server (see the deployment guide), so a
compromised web process cannot delete backups.

## System

*Administration → System* checks the database, migrations, cache, file storage
and background workers, lists recent background jobs and failures, shows backup
restore points (with the backup volume mounted) and the mail settings, and sends
a test mail.

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
