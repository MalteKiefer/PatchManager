#!/bin/sh
# Daily PostgreSQL dump with retention. Runs inside the postgres image.
set -eu
PGPASSWORD="$(cat /run/secrets/postgres_superuser_password)"
export PGPASSWORD
target="/backups/patchmanager-$(date -u +%Y%m%dT%H%M%SZ).dump"
pg_dump --format=custom --file="$target.partial"
mv "$target.partial" "$target"
# Uploads are stored encrypted by the application; archive them as they are.
if [ -d /media ]; then
    tar -C /media -cf "$target.media.tar.partial" . && mv "$target.media.tar.partial" "$target.media.tar"
fi
find /backups -name 'patchmanager-*' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
echo "backup written: $target"
