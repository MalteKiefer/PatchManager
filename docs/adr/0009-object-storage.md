# 9. Object storage backend

* Status: accepted (Phase 3)
* Date: 2026-10-21

## Context

Uploads (photos, PDFs, device images) must be stored encrypted at rest, never
executed or served as active content, and survive container rebuilds. The brief
names MinIO for development, but MinIO no longer publishes container images on
Docker Hub (verified: the repository returns 404).

## Decision

* The application encrypts every upload with the Fernet keys from
  `PATCHMANAGER_ENCRYPTION_KEYS` before storing it, so the storage backend does
  not need to be trusted.
* Storage is Django's `STORAGES["attachments"]`. The default is the local
  filesystem below `PATCHMANAGER_MEDIA_ROOT` (`/app/media`, a named volume in
  compose), files `0600`, directories `0700`.
* Files are never exposed by URL. `metadata.views.download` checks the view
  permission on the target object, decrypts, and responds with `nosniff`, a
  sandboxing CSP and `attachment` disposition for non images.
* An S3 compatible backend can be configured through `STORAGES` with
  `django-storages`; it is not bundled until an operator needs it, because no
  maintained, freely distributed S3 server image is pinned in the dev stack
  yet.

## Consequences

Backups must include the media volume (the backup job archives it) and the
encryption keys (kept outside the host). Without the keys uploads cannot be
read.
