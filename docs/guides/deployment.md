# Deployment

PatchManager runs as a Docker Compose stack: Caddy (TLS), app, worker, scheduler,
PostgreSQL 18, Redis and a backup job. Only Caddy is reachable from outside; everything else sits on an internal
network.

## Requirements

* Docker Engine with Compose v2
* A DNS name pointing at the host, ports 80 and 443 open (Caddy obtains certificates
  automatically via ACME)
* Encrypted storage for Docker volumes (for example LUKS on the host); the database and
  backup volumes hold tenant data

## Install

```bash
git clone https://github.com/MalteKiefer/PatchManager.git
cd PatchManager
mkdir -p secrets && chmod 700 secrets
openssl rand -base64 48 | tr -d '/+=' > secrets/postgres_password
openssl rand -base64 48 | tr -d '/+=' > secrets/postgres_superuser_password
openssl rand -base64 64 > secrets/django_secret_key
# Fernet key: 32 random bytes, URL safe base64
openssl rand -base64 32 | tr '+/' '-_' > secrets/encryption_keys
printf '%s' 'your-smtp-password' > secrets/email_password   # empty file if SMTP needs no auth
chmod 600 secrets/*
cp .env.example .env    # set PATCHMANAGER_DOMAIN, SMTP settings and the image tag
docker compose -f compose.prod.yaml up -d
docker compose -f compose.prod.yaml exec app /app/.pixi/envs/prod/bin/python -m django setup_token
```

The `migrate` service applies database migrations before `app` starts. Open
`https://<your domain>/` and complete the setup with the printed token (see the
[administration guide](admin.md)).

`encryption_keys` protects MFA secrets in the database. Keep a copy outside the
host: without it, users must re-enrol their second factors. To rotate, put the new
key first and the old one second (comma separated), then restart.

The database cluster superuser (`postgres`) is used only for initialisation and
backups. The application connects as `patchmanager`, which is subject to Row
Level Security ([ADR 0013](../adr/0013-database-roles.md)).

## Verify the image

```bash
cosign verify ghcr.io/maltekiefer/patchmanager:0.1.0 \
  --certificate-identity-regexp '^https://github.com/MalteKiefer/PatchManager/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

## Backups

The `backup` service writes a `pg_dump` custom format file to the `backups` volume once a
day and keeps 14 days. Copy that volume off the host, encrypted (for example with restic
or age). Restore:

```bash
docker compose -f compose.prod.yaml exec backup sh -c \
  'pg_restore --clean --if-exists --dbname=patchmanager /backups/<file>.dump'
```

Point in time recovery (WAL archiving) and a CI tested restore drill follow in Phase 9.

## Updating

```bash
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
```

## Uploads

Photos and files are stored encrypted in the `media` volume (`/app/media`) and are
included in the daily backup archive. Limits and scanning:

| Variable | Default | Meaning |
|---|---|---|
| `PATCHMANAGER_UPLOAD_MAX_MB` | `20` | Maximum upload size (Caddy limits request bodies to 25 MB) |
| `PATCHMANAGER_CLAMD_HOST` / `PATCHMANAGER_CLAMD_PORT` | unset / `3310` | Scan uploads with clamd; uploads fail while it is unreachable |

## API and webhooks

| Variable | Default | Meaning |
|---|---|---|
| `PATCHMANAGER_API_RATE` | `1200/minute` | Requests per user and time unit |
| `PATCHMANAGER_WEBHOOK_ALLOWED_NETWORKS` | empty | Comma separated CIDRs webhooks may reach besides public addresses |
| `PATCHMANAGER_WEBHOOK_ALLOW_HTTP` | `false` | Allow plain http webhook URLs |

Webhooks are sent by the `beat` and `worker` services; both must run.

## Reports

PDF reports show the instance name (`PATCHMANAGER_INSTANCE_NAME`). Set
`PATCHMANAGER_BRAND_LOGO` to a PNG or JPEG file (up to 2 MB) mounted into the
container to print your logo on every report; the system check fails if the file
cannot be used.

## Maps

Site maps load tiles from OpenStreetMap in the user's browser. Set
`PATCHMANAGER_MAP_TILE_URL` to your own tile server (and
`PATCHMANAGER_MAP_ATTRIBUTION`), or to an empty value to disable maps. The tile
origin is added to the Content Security Policy automatically.

## Health

`/health/live` and `/health/ready` are used by container healthchecks; `/metrics`
(Prometheus, requires `PATCHMANAGER_METRICS_TOKEN`) is for scrapers on the internal
network. All three are blocked at the proxy. Logs are JSON lines on stdout: `docker compose -f compose.prod.yaml logs app`.
