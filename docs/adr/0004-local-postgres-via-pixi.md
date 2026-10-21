# 4. Local PostgreSQL and Redis from conda-forge via pixi

* Status: accepted
* Date: 2026-10-21

## Context

The brief asks for a Postgres test container. The primary development machine has no
container runtime, and pixi can provide PostgreSQL 18 and Redis 8 from conda-forge,
locked in `pixi.lock` like every other dependency.

## Decision

`pixi run test` starts a throwaway PostgreSQL cluster under `.pixi/pgdata`, listening
only on a Unix socket. CI uses the same task, so local and CI tests run against the same
locked PostgreSQL build. Container images (`compose.yaml`, `compose.prod.yaml`) use the
official `postgres:18.6` image and are exercised in CI.

## Consequences

No Docker is required to develop or test. The image build and compose files are only
verified in CI until a developer runs Docker locally. Tests never fall back to SQLite.
