#!/usr/bin/env bash
# scripts/demo-db.sh
#
# Start a local MySQL demo database for trying live capture and `myflames ui`.
# Uses the same testdb schema and seed data as scripts/generate-fixtures.sh,
# so plans match the README join samples. Creates a read-only `demo` user.
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
#
# Requirements: Docker, openssl

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER_NAME="${CONTAINER_NAME:-myflames-demo}"
MYSQL_IMAGE="${MYSQL_IMAGE:-mysql:8.4}"
DEMO_PORT="${DEMO_PORT:-3406}"
CREDS_FILE="${CREDS_FILE:-$REPO/local/demo-lab/credentials.env}"

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
  Database: testdb

Credentials are stored in $CREDS_FILE.

  myflames ui
  myflames -h 127.0.0.1 -P $DEMO_PORT -u demo -p -D testdb \\
    -e "SELECT * FROM users u JOIN orders o ON o.user_id = u.id WHERE u.country = 'US' LIMIT 100" \\
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

log "Seeding testdb..."
# Reuse the fixture generator's schema so the two never drift apart.
awk "/^mysql_exec << 'SQL'/{f=1;next} /^SQL\$/{f=0} f" \
  "$REPO/scripts/generate-fixtures.sh" \
  | docker exec -i "$CONTAINER_NAME" mysql -uroot -p"$ROOT_PASSWORD" --silent \
      > /dev/null 2>&1

docker exec -i "$CONTAINER_NAME" mysql -uroot -p"$ROOT_PASSWORD" 2> /dev/null <<SQL
CREATE USER 'demo'@'%' IDENTIFIED BY '$DEMO_PASSWORD';
GRANT SELECT, SHOW VIEW ON testdb.* TO 'demo'@'%';
GRANT SELECT ON performance_schema.* TO 'demo'@'%';
SQL

print_credentials
