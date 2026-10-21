# PatchManager

Self-hosted documentation for network infrastructure: sites, buildings, floors, rooms,
racks, devices, ports, patch panels, cabling, VLANs and WLANs. Multi-tenant, with fine
grained permissions, SSO and an API.

> Status: Phase 4 (cabling and networks). Tenants, users, MFA, roles, the audit log,
> locations, racks, devices, ports, cabling with path tracing, VLANs and WLANs work;
> floor plans follow. See the [phase backlog](docs/plan/backlog.md).

## Quick start (development)

```bash
pixi install -e dev
pixi run -e dev test
pixi run -e dev python -m django setup_token   # first run only
pixi run -e dev dev                            # http://127.0.0.1:8000
```

Browser tests: `pixi run -e dev e2e-install` once, then `pixi run -e dev e2e`.

Or with Docker: `docker compose up --build`.

## Production

See the [deployment guide](docs/guides/deployment.md). Images are published to
`ghcr.io/maltekiefer/patchmanager`, signed with cosign.

## Documentation

* [Architecture](docs/plan/architecture.md) and [data model](docs/plan/data-model.md)
* [Technology stack](docs/STACK.md)
* [Architecture decisions](docs/adr/)
* [Threat model](docs/security/threat-model.md)
* [Contributing](CONTRIBUTING.md) and [security policy](SECURITY.md)

Build the docs site with `pixi run -e docs docs`.

## License

[AGPL-3.0-only](LICENSE).
