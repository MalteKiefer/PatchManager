# Contributing

## Setup

Requirements: [pixi](https://pixi.sh) 0.82 or newer. Docker is optional (only for the
compose stacks and image builds).

```bash
pixi install -e dev
pixi run -e dev pre-commit install
pixi run -e dev test     # starts a local PostgreSQL under .pixi/pgdata
pixi run -e dev dev      # http://127.0.0.1:8000
```

Stop the local database with `pixi run -e dev db-stop`.

## Tasks

| Task | Purpose |
|---|---|
| `lint` / `fmt` | ruff check and format |
| `typecheck` | mypy strict with django-stubs |
| `test` | pytest against PostgreSQL, coverage gate 90 % |
| `audit` | pip-audit and bandit |
| `migrate` | apply migrations to the local database |
| `docs` / `docs-build` | MkDocs site (`-e docs`) |
| `build` | multi-arch image build (needs Docker buildx) |

## Workflow

* Branch from `main`: `feat/<phase>-<topic>`, `fix/<topic>`, `chore/<topic>`.
* [Conventional Commits](https://www.conventionalcommits.org/), small and atomic; every
  commit builds and passes tests.
* Open a PR, link its issue, fill in the checklist. CI must be green. PRs are squash
  merged.
* Architecture changes need an ADR in `docs/adr/`.

## Code rules

* Business logic lives in services, never in views. Permission checks go through the
  policy service only.
* Type hints everywhere, docstrings on public functions and classes.
* Behaviour changes come with tests. No SQLite, not even in tests.
* Never commit secrets. Never disable a security check to make CI pass.

By contributing you agree that your contribution is licensed under AGPL-3.0-only.
