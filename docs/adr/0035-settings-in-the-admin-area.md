# 35. Operational settings in the admin area

* Status: accepted
* Date: 2026-10-22
* Changes [ADR 0034](0034-object-storage-and-offsite-backups.md): the bucket is set in
  the admin area

## Context

Administrators run PatchManager without access to the server's environment files.
Mail, sign in rules, upload limits, object storage, map tiles and webhook and API
limits were environment variables read once at start, so changing them needed shell
access and a restart.

## Decision

* These settings are stored as JSON on the instance settings row and edited on
  *Administration → Settings* by users with `system.settings.change`. Values not saved
  yet fall back to the environment variable of the same setting, so existing
  installations keep their configuration.
* Passwords and keys inside are encrypted with the instance Fernet keys. Only the
  stored (encrypted) form goes into the shared cache; it is dropped on every save, so
  all processes use a change from their next request.
* Django and DRF read them at use time through small adapters
  (`instance.runtime`): a password length validator, an SMTP backend, an API
  throttle, a middleware that sets the map tile origin in the CSP, and an attachment
  storage that writes to the bucket when one is set and still reads files stored in
  the volume before.
* Saving a bucket writes and deletes a test object; the endpoint must be https and must
  not resolve to loopback, link local, multicast, reserved or unspecified addresses
  (IPv4 mapped addresses included), checked on every save. botocore resolves the host
  again when it connects, so a DNS rebinding window remains; it is accepted because
  only holders of `system.settings.change` can set the endpoint, and they can already
  change the mail server and sign in rules.
* Bootstrap values stay in the environment: database, secret key, encryption keys,
  domain, trusted proxy, backup volume, log level. The off-site backup keeps its own
  credentials on the server, so the web process cannot delete backups.

## Consequences

The settings page is security relevant (it can weaken sign in rules or redirect mail);
it needs `system.settings.change` and every save is audited with the changed names.
Tests without a database use the environment defaults.
