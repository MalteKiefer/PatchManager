# 34. S3 object storage and off-site backups

* Status: accepted
* Date: 2026-10-22
* Supersedes the last point of [ADR 0009](0009-object-storage.md)

## Context

Operators want uploads in S3 compatible object storage (AWS S3, Backblaze B2) rather
than on the host, and backups that leave the host encrypted and can be restored with
one command. ADR 0009 left S3 unbundled until someone needed it.

## Decision

* `django-storages` with `boto3` is bundled. `PATCHMANAGER_S3_BUCKET` switches
  `STORAGES["attachments"]` to `S3Storage`; endpoint, region, key and prefix come from
  the environment, the secret from a file. The endpoint must be https. No ACL header is
  sent (B2 rejects them); the bucket stays private. Files are still encrypted by the
  application first, and still only reach users through the permission checked
  download view.
* Off-site copies use restic in an `offsite` compose service (profile `offsite`),
  not code in the application: restic encrypts on the host, deduplicates, prunes and
  checks the repository, and the web process gets no credentials for the backup
  bucket. The service uploads after each new dump and writes a small status file into
  the backup volume, which the system page shows.
* Restores run in a `restore` service (profile `restore`) with the database superuser,
  one transaction, and replace the uploads in the `media` volume. The CI drill uses this
  service, so the documented command is tested on every run.
* Backups and restores are not started from the web interface. The web process runs
  without database superuser rights and cannot stop the writers a consistent restore
  needs; the system page shows restore points, the off-site status and the command.

## Consequences

With a bucket, the database backup does not contain the uploads; operators turn on
versioning or a retention lock on the bucket. The off-site repository password and the
encryption keys must be kept outside the host.
