#!/bin/sh
# Local throwaway PostgreSQL for development and tests (docs/adr/0004).
# The cluster superuser is "postgres"; the application connects as the
# non-superuser "patchmanager" so Row Level Security applies to it.
set -eu

start() {
    pg_ctl -D "$PGDATA" status >/dev/null 2>&1 && return 0
    pg_ctl -D "$PGDATA" -o "-k $PGHOST -p $PGPORT -c listen_addresses=''" \
        -l "$PIXI_PROJECT_ROOT/.pixi/pg.log" -w start
}

case "${1:-}" in
    init)
        [ -d "$PGDATA" ] && exit 0
        initdb --auth=trust --username=postgres --encoding=UTF8 --locale=C.UTF-8 -D "$PGDATA"
        start
        psql -U postgres -d postgres -v ON_ERROR_STOP=1 -c \
            "CREATE ROLE patchmanager LOGIN NOSUPERUSER NOBYPASSRLS CREATEDB;" -c \
            "CREATE DATABASE patchmanager OWNER patchmanager;"
        ;;
    start) start ;;
    stop) pg_ctl -D "$PGDATA" -w stop ;;
    *) echo "usage: db.sh init|start|stop" >&2; exit 2 ;;
esac
