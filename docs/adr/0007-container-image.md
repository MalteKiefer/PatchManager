# 7. Container image: pixi build stage, distroless runtime

* Status: accepted
* Date: 2026-10-21

## Context

The image must contain exactly the locked environment, run as non-root on a read only
root filesystem, and contain no shell or package manager.

## Decision

A `build` stage on `ghcr.io/prefix-dev/pixi` runs `pixi install --locked -e prod` and
`collectstatic`. The `prod` stage copies the environment into
`gcr.io/distroless/base-debian13:nonroot` (glibc, no shell). Base images are pinned by
digest. CI builds amd64 and arm64, scans with Trivy, attaches SBOM and provenance, and
signs with cosign keyless.

The healthcheck uses the bundled Python because the runtime has no shell or curl.

## Consequences

Debugging inside the prod container is not possible; use the `dev` target instead.
