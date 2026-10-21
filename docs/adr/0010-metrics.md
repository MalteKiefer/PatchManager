# 10. Prometheus metrics

* Status: proposed (decided in Phase 1)
* Date: 2026-10-21

## Context

`django-prometheus` 2.5.0, the latest release, requires Django below 6.0.

## Decision

Phase 0 exposes health and readiness endpoints only. Phase 1 adds a `/metrics` endpoint
built on `prometheus-client` directly (request count and latency middleware, database
and job metrics), served on a separate internal port so the proxy never exposes it.

## Consequences

One small middleware we maintain ourselves instead of an incompatible dependency.
