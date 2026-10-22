#!/bin/sh
# Encrypted off-site copy of the backup volume with restic to S3 compatible storage
# (AWS S3, Backblaze B2, Wasabi, ...). Runs inside the restic image, hourly; uploads
# only when the backup service wrote a newer dump than the last copy.
set -eu
: "${RESTIC_REPOSITORY:?set PATCHMANAGER_OFFSITE_REPOSITORY, for example s3:https://s3.eu-central-003.backblazeb2.com/bucket/patchmanager}"
for file in offsite_password offsite_access_key_id offsite_secret_access_key; do
    if [ ! -s "/run/secrets/$file" ]; then
        echo "missing secret: secrets/$file" >&2
        exit 1
    fi
done
export RESTIC_PASSWORD_FILE=/run/secrets/offsite_password
AWS_ACCESS_KEY_ID="$(cat /run/secrets/offsite_access_key_id)"
AWS_SECRET_ACCESS_KEY="$(cat /run/secrets/offsite_secret_access_key)"
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

status=/backups/.offsite.json
write_status() {
    printf '{"ok": %s, "at": "%s", "message": "%s"}\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" > "$status.partial"
    mv "$status.partial" "$status"
}

copy() {
    restic cat config >/dev/null 2>&1 || restic init
    restic backup --host patchmanager --tag patchmanager --exclude '*.partial' --exclude '.offsite.json' /backups
    restic forget --host patchmanager --keep-daily "${OFFSITE_KEEP_DAILY:-14}" \
        --keep-weekly "${OFFSITE_KEEP_WEEKLY:-8}" --keep-monthly "${OFFSITE_KEEP_MONTHLY:-12}" --prune
    restic check
}

run_once() {
    newest="$(find /backups -maxdepth 1 -name 'patchmanager-*.dump' -type f | sort | tail -1)"
    [ -n "$newest" ] || return 0
    if [ -f "$status" ] && [ ! "$newest" -nt "$status" ]; then
        return 0
    fi
    if copy; then
        write_status true "copied $(basename "$newest")"
    else
        write_status false "restic failed, see the offsite container log"
    fi
}

# offsite.sh restore patchmanager-<stamp>.dump: fetch a dump and its uploads back
# into the backups volume, then restore it with the restore service.
if [ "${1:-}" = "restore" ]; then
    name="${2:?name of the dump, for example patchmanager-20261022T030000Z.dump}"
    case "$name" in
        */* | .* | *[!A-Za-z0-9._-]*) echo "invalid dump name: $name" >&2; exit 2 ;;
        patchmanager-*.dump) ;;
        *) echo "not a PatchManager dump: $name" >&2; exit 2 ;;
    esac
    restic restore latest --host patchmanager --target / \
        --include "/backups/$name" --include "/backups/$name.media.tar"
    exit 0
fi
if [ "${1:-}" = "once" ]; then
    run_once
    exit 0
fi
while true; do
    run_once || true
    sleep 3600
done
