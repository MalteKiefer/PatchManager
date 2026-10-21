# 9. Object storage backend

* Status: proposed (decided in Phase 3)
* Date: 2026-10-21

## Context

Uploads go to the local filesystem or an S3 compatible store. The brief names MinIO for
development, but MinIO no longer publishes container images on Docker Hub (verified:
the repository returns 404).

## Decision

Phase 0 ships no object storage service. Phase 3 evaluates maintained S3 compatible
servers for the dev stack (for example Garage, SeaweedFS) and uses `django-storages`
against any S3 API in production. Files are encrypted at rest by the application before
upload, so the backend does not need to be trusted.

## Consequences

Upload features start in Phase 3 with a decision recorded here.
