# 1. Record architecture decisions

* Status: accepted
* Date: 2026-10-21

## Context

PatchManager is built phase by phase by changing contributors. Decisions and their
reasons must survive the people who made them.

## Decision

We keep Architecture Decision Records in `docs/adr/`, numbered, one decision per file,
using the sections Context, Decision, Consequences. A superseded ADR stays and links to
its successor.

The project is a clean room implementation: we study only public behaviour of existing
products and never copy their code, text, UI, names or assets.

## Consequences

Any deviation from the documented stack needs an ADR in the same PR.
