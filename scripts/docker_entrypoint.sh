#!/bin/sh
# Container entrypoint for the CCAT api/worker/db-upgrade services.
#
#   1. waits until the SQLite volume directory exists and is writable
#   2. optionally runs the schema migration (CCAT_RUN_MIGRATIONS=1); this is
#      a controlled one-shot bootstrap.  docker-compose runs migrations in
#      its own `db-upgrade` service instead of racing every replica.
#   3. execs the container command (uvicorn / ccat worker / ccat db-upgrade).
#
# POSIX sh only: the runtime image has no bash.
set -eu

CONFIG="${CCAT_CONFIG:-/app/config/ccat_docker.json}"
DB_DIR="${CCAT_DB_DIR:-/data}"
DB_WAIT_SECONDS="${CCAT_DB_WAIT_SECONDS:-60}"

waited=0
while [ ! -d "$DB_DIR" ] || [ ! -w "$DB_DIR" ]; do
    if [ "$waited" -ge "$DB_WAIT_SECONDS" ]; then
        echo "ccat-entrypoint: database directory $DB_DIR is not writable" \
            "after ${DB_WAIT_SECONDS}s" >&2
        exit 1
    fi
    waited=$((waited + 1))
    sleep 1
done

if [ "${CCAT_RUN_MIGRATIONS:-0}" = "1" ]; then
    echo "ccat-entrypoint: running migrations (CCAT_RUN_MIGRATIONS=1)"
    ccat db-upgrade --config "$CONFIG"
fi

exec "$@"
