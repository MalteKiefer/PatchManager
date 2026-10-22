# 36. Off-site backups configured in the admin area

* Status: accepted
* Date: 2026-10-22
* Supersedes the off-site part of [ADR 0034](0034-object-storage-and-offsite-backups.md)

## Context

ADR 0034 ran restic in its own compose service with credentials in files on the
server, so the web process never held the keys of the backup bucket. Administrators
asked to configure everything in the web interface, without shell access.

## Decision

* The repository (`s3:https://host/bucket/path` only), the key, the repository
  password and the retention are operational settings (ADR 0035), stored encrypted.
* The worker runs restic (bundled in the image from conda-forge) every hour when a dump
  newer than the last successful copy exists, and on *Copy now*: init when needed,
  backup of the read only backup volume, forget with the retention and prune, check.
  Secrets reach restic only through its environment, never its arguments; error
  messages are cut to one line with secrets masked. No local cache (read only
  containers keep `/tmp` in memory).
* The last result is stored on the instance settings row and shown on the system page
  and the settings page.
* Fetching a dump back stays a command (`offsite_fetch`), since the application mounts
  the backup volume read only.

## Consequences

Whoever can change the settings, or takes over the web or worker process, can read
the backup credentials. Operators should give the off-site key no right to delete old
versions (object lock or a lifecycle rule) so the copies survive that. The compose
`offsite` profile and `deploy/offsite.sh` are removed.
