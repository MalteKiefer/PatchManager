#!/bin/sh
# Restore a dump (and its uploads) written by backup.sh. Runs inside the postgres
# image as the restore service; stop app, worker, scheduler and caddy first.
#   docker compose -f compose.prod.yaml --profile restore run --rm restore patchmanager-<stamp>.dump
set -eu
name="${1:?name of a dump in the backups volume, for example patchmanager-20261022T030000Z.dump}"
case "$name" in
    */* | .* | *[!A-Za-z0-9._-]*) echo "invalid dump name: $name" >&2; exit 2 ;;
    patchmanager-*.dump) ;;
    *) echo "not a PatchManager dump: $name" >&2; exit 2 ;;
esac
dump="/backups/$name"
[ -f "$dump" ] || { echo "no such dump: $dump" >&2; exit 2; }
pg_restore --list "$dump" >/dev/null

PGPASSWORD="$(cat /run/secrets/postgres_superuser_password)"
export PGPASSWORD
# Open sessions would block DROP during --clean.
psql -v ON_ERROR_STOP=1 -tAc "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity
    WHERE datname = current_database() AND pid <> pg_backend_pid()" >/dev/null
pg_restore --clean --if-exists --exit-on-error --single-transaction --dbname="$PGDATABASE" "$dump"
echo "database restored from $name"

if [ -f "$dump.media.tar" ]; then
    find /media -mindepth 1 -delete
    tar -C /media -xf "$dump.media.tar"
    echo "uploads restored from $name.media.tar"
fi
