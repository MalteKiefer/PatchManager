#!/bin/sh
# Backup and restore drill against the production stack (compose.prod.yaml).
# Starts the stack with throwaway secrets, writes data, takes a backup, destroys
# the database volume, restores the dump and checks that nothing was lost.
#   PATCHMANAGER_IMAGE=patchmanager:scan sh scripts/drill.sh
# Needs Docker. Leaves nothing behind (volumes and secrets are removed at the end).
set -eu

export PATCHMANAGER_DOMAIN=localhost
export PATCHMANAGER_IMAGE="${PATCHMANAGER_IMAGE:?set PATCHMANAGER_IMAGE}"
compose="docker compose -f compose.prod.yaml"
py="/app/.pixi/envs/prod/bin/python"

cleanup() {
    $compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf secrets
}
trap cleanup EXIT

mkdir -p secrets && chmod 700 secrets
openssl rand -base64 48 | tr -d '/+=\n' > secrets/postgres_password
openssl rand -base64 48 | tr -d '/+=\n' > secrets/postgres_superuser_password
openssl rand -base64 64 | tr -d '\n' > secrets/django_secret_key
openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n' > secrets/encryption_keys
: > secrets/email_password

sql() {
    $compose exec -T postgres psql -U postgres -d patchmanager -tAc "$1"
}

state() {
    sql "SELECT (SELECT count(*) FROM dcim_port), (SELECT count(*) FROM cabling_cable),
                (SELECT count(*) FROM history_change), (SELECT count(*) FROM audit_auditevent),
                (SELECT count(*) FROM pg_class WHERE relforcerowsecurity)"
}

healthy() {
    for _ in $(seq 60); do
        id="$($compose ps -q "$1")"
        if [ -n "$id" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$id")" = healthy ]; then
            return 0
        fi
        sleep 3
    done
    $compose logs --tail 50 "$1"
    return 1
}

page() {
    curl -fsSk --retry 20 --retry-all-errors --retry-delay 3 -o /dev/null https://localhost/auth/login/
}

echo "== start"
$compose up -d
healthy app
page

echo "== write data"
$compose exec -T app $py -m django shell -c "
from patchmanager.accounts.models import Assignment, Role, User
user = User.objects.create_user(email='drill@example.test', password='drill-only-0123456789', display_name='Drill')
Assignment.objects.create(user=user, role=Role.objects.get(key='super_admin'), effect='allow', conditions=[], scope_label='global')
"
$compose exec -T app $py -m django seed_scale --actor drill@example.test --racks 5

echo "== backup (writers stopped, so the dump matches the counts)"
$compose stop app worker scheduler caddy
before="$(state)"
echo "before: $before"
$compose exec -T backup /usr/local/bin/backup.sh
dump="$($compose exec -T backup sh -c 'ls -1t /backups/patchmanager-*.dump | head -1')"

echo "== lose the database"
$compose rm -sf postgres
docker volume rm patchmanager_pgdata
$compose up -d postgres
healthy postgres
test "$(sql "SELECT to_regclass('dcim_port') IS NULL")" = "t"

echo "== restore $dump"
$compose --profile restore run --rm -T restore "$(basename "$dump")"
after="$(state)"
echo "after:  $after"
test "$before" = "$after"

echo "== start again and verify"
$compose up -d
healthy app
$compose exec -T app $py -m django migrate --check
$compose exec -T app $py -m django verify_audit_chain
page
echo "drill passed"
