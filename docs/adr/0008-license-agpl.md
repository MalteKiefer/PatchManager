# 8. License: AGPL-3.0-only

* Status: accepted
* Date: 2026-10-21

## Context

PatchManager is an open alternative to a hosted service. A permissive license would
allow closed hosted forks.

## Decision

The project owner chose AGPL-3.0-only. Users who offer a modified PatchManager over a
network must publish their changes.

## Consequences

Every dependency must be compatible with AGPL-3.0. New dependencies get a license check.
Imported community device libraries are kept under their own license and are not merged
into the source tree without review.
