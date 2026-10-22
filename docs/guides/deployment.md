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

Every CI run tests this procedure on the production stack (`scripts/drill.sh`): it
writes data, takes a backup, deletes the database volume, restores the dump and
checks row counts, Row Level Security, migrations and the audit hash chain. To
restore after losing the database volume, start `postgres` (the init script creates
the role and the empty database again), restore the dump as above, then start the
rest of the stack. Restore uploads with
`tar -C /app/media -xf /backups/<file>.dump.media.tar` in a container that mounts
the `media` volume writable.

### Encrypted off-site copies

Dumps hold the documentation in plain form (uploads and MFA secrets inside are
already encrypted by the application). Keep them on an encrypted disk and copy them
off the host with a tool that encrypts on the client. With
[restic](https://restic.net), run on the host, for example daily from cron after the
backup service:

```bash
export RESTIC_REPOSITORY=s3:https://s3.example.com/backups/patchmanager
export RESTIC_PASSWORD_FILE=/root/restic-password     # keep a copy outside the host
restic init                                           # once
restic backup "$(docker volume inspect -f '{{.Mountpoint}}' patchmanager_backups)"
restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
restic check
```

To restore, `restic restore latest --target /restore` and copy the dump into the
`backups` volume, then follow the restore steps above. Keep `secrets/encryption_keys`
with the off-site copy: uploads and second factors cannot be read without it.

### Point in time recovery

The daily dump loses at most one day. For a recovery point of minutes, archive the
write ahead log. This is not part of the CI drill; rehearse it before relying on it.

1. Add a volume for the archive and enable archiving in a `compose.override.yaml`:

    ```yaml
    services:
      postgres:
        command: ["postgres", "-c", "wal_level=replica", "-c", "archive_mode=on",
                  "-c", "archive_timeout=300",
                  "-c", "archive_command=test ! -f /wal/%f && cp %p /wal/%f"]
        volumes: ["wal:/wal"]
    volumes:
      wal:
    ```

2. Take a base backup after the change and then weekly (old WAL before the oldest
   base backup can be deleted):

    ```bash
    docker compose exec postgres pg_basebackup -U postgres \
      -D /wal/base-$(date -u +%Y%m%d) -Ft -z -X stream
    ```

3. To recover to a moment: stop the stack, empty the `pgdata` volume, unpack
   `base.tar.gz` and `pg_wal.tar.gz` of the newest base backup before that moment into
   `/var/lib/postgresql/18/docker`, create an empty `recovery.signal` there and add to
   `postgresql.auto.conf`:

    ```
    restore_command = 'cp /wal/%f %p'
    recovery_target_time = '2026-10-22 09:30:00+00'
    recovery_target_action = 'promote'
    ```

   Start `postgres`, check the data, then start the rest of the stack. Copy the `wal`
   volume off-site like the dumps.

## Reverse proxy

| Variable | Default | Meaning |
|---|---|---|
| `PATCHMANAGER_FORWARDED_ALLOW_IPS` | `127.0.0.1`; `*` in `compose.prod.yaml` | Proxies trusted for `X-Forwarded-Proto` and `X-Forwarded-For`. The client address (rate limits, audit log) is the rightmost `X-Forwarded-For` entry of a trusted proxy. Use `*` only when nothing but the proxy can reach the app |

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

## Backups and source

| Variable | Default | Meaning |
|---|---|---|
| `PATCHMANAGER_BACKUP_DIR` | `/backups` in `compose.prod.yaml` | Read only backup volume shown on the system page |
| `PATCHMANAGER_SOURCE_URL` | the GitHub repository | Source code link required by the AGPL for network use; point it at your fork if you change the code |

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
