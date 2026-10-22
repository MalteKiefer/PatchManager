# Deployment

PatchManager runs as a Docker Compose stack: Caddy (TLS), app, worker, scheduler,
PostgreSQL 18, Redis and a backup job. Only Caddy is reachable from outside; everything else sits on an internal
network. The application containers also join an `egress` network without published
ports, so they can reach mail, SSO, webhooks, object storage and platform APIs;
restrict it with host firewall rules if your policy requires an allow list.

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
docker compose -f compose.prod.yaml stop app worker scheduler caddy
docker compose -f compose.prod.yaml --profile restore run --rm restore patchmanager-<stamp>.dump
docker compose -f compose.prod.yaml up -d
```

The `restore` service checks the dump, ends open database sessions, restores it in one
transaction (a failed restore leaves the database as it was) and replaces the uploads
in the `media` volume with the archive written next to the dump. The system page lists
the restore points with the command for the newest one.

Every CI run tests this procedure on the production stack (`scripts/drill.sh`): it
writes data, takes a backup, deletes the database volume, restores the dump and
checks row counts, Row Level Security, migrations and the audit hash chain. To
restore after losing the database volume, start `postgres` (the init script creates
the role and the empty database again), restore the dump as above, then start the
rest of the stack.

### Encrypted off-site copies

Dumps hold the documentation in plain form (uploads and MFA secrets inside are
already encrypted by the application). Keep them on an encrypted disk and copy them
off the host. The `offsite` service does this with [restic](https://restic.net): it
encrypts on the host and uploads to S3 compatible storage (AWS S3, Backblaze B2,
Wasabi, ...) after every new dump, keeps 14 daily, 8 weekly and 12 monthly snapshots
and checks the repository. The system page shows the last run.

```bash
openssl rand -base64 48 > secrets/offsite_password    # keep a copy outside the host
printf '%s' '<key id>' > secrets/offsite_access_key_id
printf '%s' '<application key>' > secrets/offsite_secret_access_key
chmod 600 secrets/offsite_*
export PATCHMANAGER_OFFSITE_REPOSITORY=s3:https://s3.eu-central-003.backblazeb2.com/<bucket>/patchmanager
docker compose -f compose.prod.yaml --profile offsite up -d offsite
```

Use a separate bucket and key from the uploads bucket, so a leaked application key
cannot delete the backups. For B2 use the S3 endpoint of the bucket's region. Retention
is set with `OFFSITE_KEEP_DAILY`, `OFFSITE_KEEP_WEEKLY` and `OFFSITE_KEEP_MONTHLY` on
the service.

To fetch a dump from the off-site copy into the `backups` volume (then restore it as
above; `restic snapshots` inside the container lists what is there):

```bash
docker compose -f compose.prod.yaml --profile offsite run --rm offsite \
  restore patchmanager-<stamp>.dump
```

Keep `secrets/encryption_keys` and `secrets/offsite_password` outside the host as
well: without the first, uploads and second factors cannot be read; without the
second, the off-site copy cannot be opened.

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
| `PATCHMANAGER_FORWARDED_ALLOW_IPS` | `127.0.0.1`; Caddy's address in `compose.prod.yaml` | Proxies trusted for `X-Forwarded-Proto` and `X-Forwarded-For`. The client address (rate limits, audit log) is the rightmost `X-Forwarded-For` entry of a trusted proxy. Use `*` only when nothing but the proxy can reach the app |
| `PATCHMANAGER_BACKEND_SUBNET` / `PATCHMANAGER_BACKEND_IP_RANGE` / `PATCHMANAGER_PROXY_IP` | `172.30.0.0/24` / `172.30.0.128/25` / `172.30.0.2` | Subnet of the internal network, the range Docker assigns from, and Caddy's fixed address outside that range, the only proxy the app trusts; change all three if the subnet is taken on the host |

## Updating

```bash
docker compose -f compose.prod.yaml pull
docker compose -f compose.prod.yaml up -d
```

## Settings in the admin area

Sign in rules, mail, uploads and file storage, maps and webhook and API limits are
set by administrators under *Administration → Settings* and take effect without a
restart; see the [administration guide](admin.md#settings). The `PATCHMANAGER_*`
variables in the following sections are only their initial values until someone
saves the page. What stays in the environment: database, secret key, encryption
keys, domain and proxy, backup volume and log level.

## Uploads

Photos and files are stored encrypted in the `media` volume (`/app/media`) and are
included in the daily backup archive. A bucket in S3 compatible object storage (AWS
S3, Backblaze B2, Wasabi, Ceph, Garage) can be used instead, set under *Settings →
Files*. The application encrypts every file before it leaves the process, so the
provider only sees ciphertext. Keep the bucket private and turn on object versioning
or a retention lock there: the database backup does not contain the files. Initial
values:

| Variable | Default | Meaning |
|---|---|---|
| `PATCHMANAGER_UPLOAD_MAX_MB` | `20` | Maximum upload size (Caddy limits request bodies to 25 MB) |
| `PATCHMANAGER_CLAMD_HOST` / `PATCHMANAGER_CLAMD_PORT` | unset / `3310` | Scan uploads with clamd; uploads fail while it is unreachable |
| `PATCHMANAGER_S3_BUCKET` | unset | Store uploads in this bucket instead of the `media` volume |
| `PATCHMANAGER_S3_ENDPOINT_URL` | AWS | Provider endpoint, https only, for example `https://s3.eu-central-003.backblazeb2.com` |
| `PATCHMANAGER_S3_REGION` | unset | Region, for example `eu-central-003` for B2 |
| `PATCHMANAGER_S3_ACCESS_KEY_ID` / `PATCHMANAGER_S3_SECRET_ACCESS_KEY_FILE` | required with a bucket | Application key; give it access to this bucket only (B2: a key restricted to the bucket) |
| `PATCHMANAGER_S3_PREFIX` | `attachments` | Folder inside the bucket |
| `PATCHMANAGER_S3_ADDRESSING_STYLE` | provider default | `path` for servers without virtual host buckets |

Switching to a bucket: new files go to the bucket, files stored before stay readable
from the `media` volume. To move them as well, copy the volume's `attachments` folder
into the bucket prefix (for example with `rclone copy`).

## Backups and source

| Variable | Default | Meaning |
|---|---|---|
| `PATCHMANAGER_BACKUP_DIR` | `/backups` in `compose.prod.yaml` | Read only backup volume shown on the system page |
| `PATCHMANAGER_SOURCE_URL` | the GitHub repository | Source code link required by the AGPL for network use; point it at your fork if you change the code |

## API and webhooks

Initial values of *Settings → Integrations*:

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

Site maps load tiles from OpenStreetMap in the user's browser. Choose another tile
server, or none, under *Settings → Maps* (initial values `PATCHMANAGER_MAP_TILE_URL`
and `PATCHMANAGER_MAP_ATTRIBUTION`). The tile origin is added to the Content Security
Policy automatically.

## Health

`/health/live` and `/health/ready` are used by container healthchecks; `/metrics`
(Prometheus, requires `PATCHMANAGER_METRICS_TOKEN`) is for scrapers on the internal
network. All three are blocked at the proxy. Logs are JSON lines on stdout: `docker compose -f compose.prod.yaml logs app`.
