#!/bin/sh
# Runs once when the database volume is created (docker-entrypoint-initdb.d).
# The application role is no superuser and has no BYPASSRLS, so Row Level
# Security applies to it (docs/adr/0003). The superuser is only used here and
# by the backup job.
set -eu
app_password="$(cat /run/secrets/postgres_password)"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    --set app_password="$app_password" <<'SQL'
CREATE ROLE patchmanager LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
CREATE DATABASE patchmanager OWNER patchmanager;
SQL
