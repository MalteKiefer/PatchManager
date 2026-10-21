# Deployment

PatchManager runs as a Docker Compose stack: Caddy (TLS), app, PostgreSQL 18, Redis and a
backup job. Only Caddy is reachable from outside; everything else sits on an internal
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
openssl rand -base64 48 > secrets/postgres_password
openssl rand -base64 64 > secrets/django_secret_key
chmod 600 secrets/*
cp .env.example .env    # set PATCHMANAGER_DOMAIN and the image tag
docker compose -f compose.prod.yaml up -d
```

The `migrate` service applies database migrations before `app` starts.

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

## Health

`/health/live` and `/health/ready` are used by container healthchecks and are blocked at
the proxy. Logs are JSON lines on stdout: `docker compose -f compose.prod.yaml logs app`.
