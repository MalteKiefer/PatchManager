# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-21

### Added

* Project plan: architecture, data model, phase backlog, feature scope notes.
* pixi workspace with `default`, `dev`, `test`, `lint`, `docs` and `prod` environments.
* Django 6.1 skeleton: environment driven settings with Docker secret files, security
  headers and CSP, JSON logging, `/health/live` and `/health/ready`.
* Multi-stage Dockerfile (pixi build, distroless non-root runtime) and dev and prod
  compose stacks with Caddy and a daily backup job.
* CI: lint, typecheck, tests on PostgreSQL 18, bandit, pip-audit, gitleaks, Trivy,
  multi-arch GHCR images with SBOM, provenance and cosign signatures; CodeQL.
* pre-commit hooks, Renovate configuration.
* Documentation site, ADRs 0001 to 0010, STRIDE threat model draft, security policy.

[0.1.0]: https://github.com/MalteKiefer/PatchManager/releases/tag/v0.1.0
