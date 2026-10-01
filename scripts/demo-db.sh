#!/usr/bin/env bash
# scripts/demo-db.sh
#
# Start a local MySQL demo server for trying live capture and `myflames ui`.
# Loads three databases and creates a read-only `demo` user:
#
#   testdb     the scripts/generate-fixtures.sh schema, filled by
#              scripts/demo-db-seed.sql with ~9M skewed rows
#   sakila     MySQL's DVD-rental sample (downloaded from dev.mysql.com)
#   employees  the datacharmer/test_db employees sample, ~4M rows
#              (downloaded from GitHub)
#
# Downloads are cached in local/demo-lab/datasets/. The first start takes
# a few minutes, mostly loading testdb.
#
# Usage:
#   ./scripts/demo-db.sh            # start (or reuse) the demo container
#   ./scripts/demo-db.sh stop       # remove the container and its data
#
# Settings (environment):
#   CONTAINER_NAME  default myflames-demo
#   MYSQL_IMAGE     default mysql:8.4
#   DEMO_PORT       default 3406 (bound to 127.0.0.1 only)
#   CREDS_FILE      default local/demo-lab/credentials.env (gitignored)
#   DEMO_DATASETS   default "testdb sakila employees"
#
# Requirements: Docker, openssl, curl

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER_NAME="${CONTAINER_NAME:-myflames-demo}"
MYSQL_IMAGE="${MYSQL_IMAGE:-mysql:8.4}"
DEMO_PORT="${DEMO_PORT:-3406}"
CREDS_FILE="${CREDS_FILE:-$REPO/local/demo-lab/credentials.env}"
DEMO_DATASETS="${DEMO_DATASETS:-testdb sakila employees}"
CACHE_DIR="$REPO/local/demo-lab/datasets"
SAKILA_URL="https://downloads.mysql.com/docs/sakila-db.tar.gz"
EMPLOYEES_URL="https://github.com/datacharmer/test_db/archive/refs/heads/master.tar.gz"

log() { echo "[demo-db] $*" >&2; }

print_credentials() {
  # shellcheck disable=SC1090
  . "$CREDS_FILE"
  cat <<EOF

Demo database is ready ($MYSQL_IMAGE, container $CONTAINER_NAME).

  Host:     127.0.0.1
  Port:     $DEMO_PORT
  User:     demo  (SELECT only)
  Password: $DEMO_PASSWORD
  Databases: $DEMO_DATASETS

Credentials are stored in $CREDS_FILE.

  myflames ui
  myflames -h 127.0.0.1 -P $DEMO_PORT -u demo -p -D testdb \\
    -e "SELECT u.id, SUM(o.total) AS spend FROM users u JOIN orders o ON o.user_id = u.id WHERE u.country = 'NZ' GROUP BY u.id ORDER BY spend DESC LIMIT 10" \\
    --type workbench -o report.html
EOF
}

if [ "${1:-}" = "stop" ]; then
  docker rm -fv "$CONTAINER_NAME" > /dev/null && log "Removed $CONTAINER_NAME."
  exit 0
fi

if docker ps --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  if [ -f "$CREDS_FILE" ]; then
    log "$CONTAINER_NAME is already running."
    print_credentials
    exit 0
  fi
  log "ERROR: $CONTAINER_NAME is running but $CREDS_FILE is missing."
  log "Run '$0 stop' and start again."
  exit 1
fi
if docker ps -a --format '{{.Names}}' | grep -Fxq "$CONTAINER_NAME"; then
  log "ERROR: stopped container $CONTAINER_NAME exists. Run '$0 stop' first."
  exit 1
fi

mkdir -p "$(dirname "$CREDS_FILE")"
chmod 700 "$(dirname "$CREDS_FILE")"
ROOT_PASSWORD="$(openssl rand -hex 12)"
DEMO_PASSWORD="$(openssl rand -hex 8)"
umask 077
printf 'ROOT_PASSWORD=%s\nDEMO_PASSWORD=%s\n' "$ROOT_PASSWORD" "$DEMO_PASSWORD" > "$CREDS_FILE"

log "Starting $MYSQL_IMAGE on 127.0.0.1:$DEMO_PORT..."
docker run -d --name "$CONTAINER_NAME" \
  -p "127.0.0.1:$DEMO_PORT:3306" \
  -e MYSQL_ROOT_PASSWORD="$ROOT_PASSWORD" \
  -e MYSQL_DATABASE=testdb \
  "$MYSQL_IMAGE" > /dev/null

# Probe over TCP: the image's temporary init server accepts socket
# connections before the real server is listening (see generate-fixtures.sh).
for i in $(seq 1 90); do
  if docker exec "$CONTAINER_NAME" mysql -uroot -p"$ROOT_PASSWORD" \
       -h 127.0.0.1 --protocol=TCP -e "SELECT 1" > /dev/null 2>&1; then
    log "MySQL is ready (${i}s)."
    break
  fi
  if [ "$i" -eq 90 ]; then
    log "ERROR: MySQL did not become ready in 90s."
    exit 1
  fi
  sleep 1
done

root_mysql() {
  docker exec -i "$CONTAINER_NAME" mysql -uroot -p"$ROOT_PASSWORD" "$@" 2> /dev/null
}

fetch() {
  # fetch URL FILE: download once into the cache.
  mkdir -p "$CACHE_DIR"
  if [ ! -s "$CACHE_DIR/$2" ]; then
    log "Downloading $1..."
    curl -fsSL -o "$CACHE_DIR/$2.part" "$1"
    mv "$CACHE_DIR/$2.part" "$CACHE_DIR/$2"
  fi
}

load_testdb() {
  log "Loading testdb (about 9M rows; this takes a few minutes)..."
  # Schema from the fixture generator, so the two never drift apart;
  # its small seed is skipped in favor of demo-db-seed.sql.
  awk "/^mysql_exec << 'SQL'/{f=1;next} /^-- Seed:/{f=0} /^SQL\$/{f=0} f" \
    "$REPO/scripts/generate-fixtures.sh" | root_mysql --silent > /dev/null
  root_mysql --silent < "$REPO/scripts/demo-db-seed.sql" > /dev/null
}

load_sakila() {
  log "Loading sakila..."
  fetch "$SAKILA_URL" sakila-db.tar.gz
  tar -xzf "$CACHE_DIR/sakila-db.tar.gz" -C "$CACHE_DIR"
  root_mysql < "$CACHE_DIR/sakila-db/sakila-schema.sql"
  root_mysql < "$CACHE_DIR/sakila-db/sakila-data.sql"
}

load_employees() {
  log "Loading employees (about 4M rows)..."
  fetch "$EMPLOYEES_URL" test_db.tar.gz
  tar -xzf "$CACHE_DIR/test_db.tar.gz" -C "$CACHE_DIR"
  # employees.sql sources its .dump files by relative path.
  docker cp "$CACHE_DIR/test_db-master" "$CONTAINER_NAME:/tmp/test_db" > /dev/null
  docker exec "$CONTAINER_NAME" sh -c \
    "cd /tmp/test_db && mysql -uroot -p'$ROOT_PASSWORD' < employees.sql" \
    > /dev/null 2>&1
  docker exec "$CONTAINER_NAME" rm -rf /tmp/test_db
}

for ds in $DEMO_DATASETS; do
  case "$ds" in
    testdb) load_testdb ;;
    sakila) load_sakila ;;
    employees) load_employees ;;
    *) log "ERROR: unknown dataset '$ds' (expected testdb, sakila, employees)."; exit 1 ;;
  esac
done

{
  echo "CREATE USER 'demo'@'%' IDENTIFIED BY '$DEMO_PASSWORD';"
  for ds in $DEMO_DATASETS; do
    echo "GRANT SELECT, SHOW VIEW ON \`$ds\`.* TO 'demo'@'%';"
  done
  echo "GRANT SELECT ON performance_schema.* TO 'demo'@'%';"
} | root_mysql

print_credentials
