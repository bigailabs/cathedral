#!/usr/bin/env bash
set -euo pipefail

# Run postgres_verify.py against a real local PostgreSQL server without Docker
# or sudo. This unpacks Ubuntu Postgres packages under /tmp, initializes a
# throwaway cluster, runs the verifier, then stops the server.
#
# Intended for Ubuntu/WSL release validation:
#
#   scripts/verify_ephemeral_postgres.sh
#
# If publisher dependencies were installed into a target directory, the script
# auto-adds /tmp/cathedral-publisher-deps to PYTHONPATH. Override with:
#
#   CATHEDRAL_PUBLISHER_DEPS=/path/to/deps scripts/verify_ephemeral_postgres.sh

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_ROOT="${TMPDIR:-/tmp}/cathedral-pg-verify"
PG_BIN_ROOT="$TMP_ROOT/pg-bin"
PG_DEBS="$TMP_ROOT/debs"
PGDATA="$TMP_ROOT/data"
PGSOCK="$TMP_ROOT/socket"
PGLOG="$TMP_ROOT/postgres.log"
PGPORT="${PGPORT:-55440}"
PUBLISHER_DEPS="${CATHEDRAL_PUBLISHER_DEPS:-/tmp/cathedral-publisher-deps}"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 2
  }
}

need apt-get
need dpkg-deb
need python3

mkdir -p "$PG_BIN_ROOT" "$PG_DEBS"
if [ ! -x "$PG_BIN_ROOT/usr/lib/postgresql/16/bin/postgres" ]; then
  (
    cd "$PG_DEBS"
    apt-get download postgresql-16 postgresql-client-16 libpq5 >/tmp/cathedral-pg-apt-download.out 2>&1
    for deb in *.deb; do
      dpkg-deb -x "$deb" "$PG_BIN_ROOT"
    done
  )
fi

PGBIN="$PG_BIN_ROOT/usr/lib/postgresql/16/bin"
PGLIB="$PG_BIN_ROOT/usr/lib/x86_64-linux-gnu"
export LD_LIBRARY_PATH="$PGLIB:${LD_LIBRARY_PATH:-}"

rm -rf "$PGDATA" "$PGSOCK" "$PGLOG"
mkdir -p "$PGSOCK"
"$PGBIN/initdb" -D "$PGDATA" -A trust --no-locale -U postgres >/tmp/cathedral-pg-initdb.out 2>&1
cat >> "$PGDATA/postgresql.conf" <<EOF
port = $PGPORT
unix_socket_directories = '$PGSOCK'
listen_addresses = '127.0.0.1'
fsync = off
EOF

"$PGBIN/pg_ctl" -D "$PGDATA" -l "$PGLOG" start >/tmp/cathedral-pg-start.out 2>&1
cleanup() {
  "$PGBIN/pg_ctl" -D "$PGDATA" stop -m fast >/tmp/cathedral-pg-stop.out 2>&1 || true
}
trap cleanup EXIT

if [ -d "$PUBLISHER_DEPS" ]; then
  export PYTHONPATH="$PUBLISHER_DEPS:${PYTHONPATH:-}"
fi

cd "$REPO_ROOT"
export DATABASE_URL="postgresql://postgres@127.0.0.1:$PGPORT/postgres"
python3 postgres_verify.py

"$PGBIN/pg_ctl" -D "$PGDATA" stop -m fast >/tmp/cathedral-pg-stop.out 2>&1
trap - EXIT
rm -rf "$PGDATA" "$PGSOCK" "$PGLOG"
