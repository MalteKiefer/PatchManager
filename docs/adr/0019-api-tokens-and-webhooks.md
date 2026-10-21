# 19. REST API, API credentials and webhooks

* Status: accepted
* Date: 2026-10-21

## Context

Phase 7 brings the API, API credentials, webhooks, SSO and SCIM. Shipping them
as one pull request would make review shallow, so the phase lands in three pull
requests under one milestone (API, tokens and webhooks; SSO; SCIM) and is tagged
as v0.8.0 after the last one.

## Decision

### API

* Django REST Framework under `/api/v1/`, OpenAPI 3.1 schema at `/api/schema/`
  (drf-spectacular, for authenticated callers). No bundled Swagger UI: it needs
  inline scripts our CSP forbids; any OpenAPI viewer can load the schema.
* Resources: tenants, sites, buildings, floors, rooms, racks, vendors and device
  types (read only), devices (with a `place` action), ports (document only),
  cables, VLANs, WLANs, changes (history, read only) and search.
* Reads use the policy engine (`scope_queryset`), ports are judged by their
  device, VLANs and WLANs by their site or tenant. Writes call the same domain
  services as the UI, so permissions, validation, audit and history are equal;
  referenced objects the caller cannot see are reported as not found.
* JSON only, limit/offset pagination (100, at most 1000), per user throttling
  (`PATCHMANAGER_API_RATE`, default 1200 per minute), session authentication with
  CSRF for browsers next to bearer tokens.

### Credentials

* **Personal tokens** `pmat_<prefix>_<secret>`: scopes `read` and/or `write`,
  optionally limited to one tenant, expiring after 1 to 365 days, revocable, with
  last use time and address. Only an HMAC-SHA256 of the token keyed with the
  Django secret key is stored (the secret has 256 bits of entropy, so a slow hash
  would only cost time on every request); `SECRET_KEY_FALLBACKS` keeps tokens
  valid while the key is rotated. The prefix finds the row. Client secrets are
  checked rarely and are stored with Argon2id like passwords. Tokens act with the owner's
  permissions. They cannot be created while impersonating.
* **OAuth 2.0 client credentials** (RFC 6749 section 4.4) at `/api/oauth/token/`
  with `client_secret_basic` or `client_secret_post`; access tokens are bearer
  tokens valid for one hour. Each client has its own *service user* that gets
  roles like any user. Service users have no password and no second factor, so
  roles that require MFA never apply to them (also not through groups), they
  never see tenants that require MFA, and assigning such roles directly is
  refused. Rotating a client secret requires holding every right of the client,
  because whoever sees the secret can act as the client.
* Authentication sets the database tenant context of the token's user (limited
  to the token's tenant) and the actor for the history.

### Webhooks

* Per tenant, managed by `integrations.webhook.manage` with a tenant wide grant
  (webhooks receive every change of the tenant, with before and after values).
  URLs often carry secrets, so there is no separate view permission.
* A trigger on the change history inserts one delivery per active webhook whose
  event patterns match (`dcim.device.*`, `cabling.cable.insert`, `*`) in the
  same transaction: no event is lost, none is sent for rolled back work.
* Celery beat sends due deliveries every 10 seconds: JSON POST with
  `PatchManager-Event`, `PatchManager-Delivery` and
  `PatchManager-Signature: t=<unix time>,v1=<HMAC-SHA256 of "<t>.<body>">`.
  Retries after 1 minute, 5 minutes, 30 minutes, 2, 6 and 24 hours, then failed;
  deliveries are kept 30 days. Workers claim batches with `SKIP LOCKED` and a
  lease, so several workers can run.
* SSRF: https only (http with `PATCHMANAGER_WEBHOOK_ALLOW_HTTP`), no credentials
  in the URL, every resolved address must be public unless it lies in
  `PATCHMANAGER_WEBHOOK_ALLOWED_NETWORKS`; the connection goes to the checked
  address while TLS verifies the hostname (no DNS rebinding), redirects are not
  followed, short timeouts, only the status is read.

## Consequences

* The API surface follows the services; new domain features need a serializer
  and a viewset, not new rules.
* Receivers must verify signatures and reject old timestamps themselves.
