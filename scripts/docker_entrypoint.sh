#!/bin/sh
# Container entrypoint for the CCAT api/worker/db-upgrade services.
#
#   1. applies the config seed when the repo file is newer than the live
#      config (the live config lives on a writable volume; the repo file
#      is mounted read-only as the seed)
#   2. waits until the SQLite volume directory exists and is writable
#   3. optionally runs the schema migration (CCAT_RUN_MIGRATIONS=1); this is
#      a controlled one-shot bootstrap.  docker-compose runs migrations in
#      its own `db-upgrade` service instead of racing every replica.
#   4. execs the container command (uvicorn / ccat worker / ccat db-upgrade).
#
# POSIX sh only: the runtime image has no bash.
set -eu

CONFIG="${CCAT_CONFIG:-/app/config/ccat_docker.json}"
CONFIG_SEED="${CCAT_CONFIG_SEED:-/app/config-seed/ccat_docker.json}"
DB_DIR="${CCAT_DB_DIR:-/data}"
DB_WAIT_SECONDS="${CCAT_DB_WAIT_SECONDS:-60}"

# The newer of the two wins: an operator edit to the repo file applies on
# restart, and a Settings-tab save survives one (it is the newer write).
if [ -f "$CONFIG_SEED" ] \
  && { [ ! -f "$CONFIG" ] || [ "$CONFIG_SEED" -nt "$CONFIG" ]; }; then
    echo "ccat-entrypoint: applying config seed $CONFIG_SEED"
    cp "$CONFIG_SEED" "$CONFIG"
fi

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
