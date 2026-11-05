#!/usr/bin/env bash
# scripts/readyset-matrix.sh
#
# Automated live Docker integration pipeline for Readyset + ProxySQL + MySQL 8.4
# (Primary + GTID Replica). Exercises live multi-tier topology discovery across
# ProxySQL, Readyset and Replication, live EXPLAIN CREATE CACHE, CREATE/FLUSH/DROP
# SHALLOW & DEEP CACHE, collision detection, cached vs upstream execution and
# latency telemetry, dynamic ALTER READYSET parameters, allowlists, maintenance
# mode, table re-snapshot, CLI (`myflames readyset`), MCP (`readyset_status`),
# and Admin SQL port (6036) shutdown.
#
# Never touches the developer's real ~/.myflames or OS keychain: all profiles
# and vault secrets live inside a throwaway directory cleaned up on exit.
#
# Usage:
#   scripts/readyset-matrix.sh              # up -> test -> down (full CI gate)
#   scripts/readyset-matrix.sh --keep       # up -> test (keep containers up; skips shutdown step)
#   scripts/readyset-matrix.sh up           # start Primary + Replica + Readyset + ProxySQL
#   scripts/readyset-matrix.sh test         # run live Python/CLI/MCP/HTTP assertions
#   scripts/readyset-matrix.sh status       # print container states
#   scripts/readyset-matrix.sh down         # remove containers, volumes and network

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAB="${READYSET_LAB:-$REPO/local/readyset-matrix}"
CREDS_FILE="$LAB/credentials.env"

MYSQL_IMAGE="${MYSQL_IMAGE:-mysql:8.4}"
READYSET_IMAGE="${READYSET_IMAGE:-public.ecr.aws/readyset/readyset:latest-stable}"
PROXYSQL_IMAGE="${PROXYSQL_IMAGE:-proxysql/proxysql:3.0.11}"

NET="mf-rs-net"
C_PRIMARY="mf-rs-primary"
C_REPLICA="mf-rs-replica"
C_READYSET="mf-rs-readyset"
C_PROXYSQL="mf-rs-proxysql"

PORT_PRIMARY="${PORT_PRIMARY:-34501}"
PORT_REPLICA="${PORT_REPLICA:-34502}"
PORT_RS_SQL="${PORT_RS_SQL:-34507}"
PORT_RS_ADMIN="${PORT_RS_ADMIN:-34536}"
PORT_PX_ADMIN="${PORT_PX_ADMIN:-34532}"
PORT_PX_MYSQL="${PORT_PX_MYSQL:-34533}"

log() { echo "[readyset-matrix] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

ensure_creds() {
  mkdir -p "$LAB"
  if [ -f "$CREDS_FILE" ]; then
    # shellcheck disable=SC1090
    . "$CREDS_FILE"
    return 0
  fi
  umask 077
  ROOT_PW="rsRoot_$(openssl rand -hex 8)"
  ADMIN_PW="rsAdmin_$(openssl rand -hex 8)"
  REPL_PW="rsRepl_$(openssl rand -hex 8)"
  PX_ADMIN_PW="pxAdmin_$(openssl rand -hex 8)"
  PX_MON_PW="pxMon_$(openssl rand -hex 8)"
  RS_ADMIN_PW="rsPort6036_$(openssl rand -hex 8)"
  cat > "$CREDS_FILE" <<EOF
ROOT_PW='$ROOT_PW'
ADMIN_PW='$ADMIN_PW'
REPL_PW='$REPL_PW'
PX_ADMIN_PW='$PX_ADMIN_PW'
PX_MON_PW='$PX_MON_PW'
RS_ADMIN_PW='$RS_ADMIN_PW'
EOF
  # shellcheck disable=SC1090
  . "$CREDS_FILE"
}

ensure_images() {
  local img
  for img in "$MYSQL_IMAGE" "$READYSET_IMAGE" "$PROXYSQL_IMAGE"; do
    docker image inspect "$img" > /dev/null 2>&1 || {
      log "pulling $img"
      docker pull -q "$img" > /dev/null
    }
  done
}

mysql_exec() {
  local cname="$1" pw="$2" user="${3:-root}"
  shift 3 || true
  docker exec -i -e MYSQL_PWD="$pw" "$cname" mysql -h 127.0.0.1 -P 3306 --protocol=TCP -u"$user" "$@"
}

wait_mysql() {
  local cname="$1" pw="$2" i
  for i in $(seq 1 60); do
    if mysql_exec "$cname" "$pw" root --batch --skip-column-names -e "SELECT 1" < /dev/null > /dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  docker logs "$cname" | tail -n 40 >&2 || true
  return 1
}

cmd_down() {
  local c
  for c in "$C_PROXYSQL" "$C_READYSET" "$C_REPLICA" "$C_PRIMARY"; do
    docker rm -fv "$c" > /dev/null 2>&1 && log "removed $c" || true
  done
  docker network rm "$NET" > /dev/null 2>&1 || true
  rm -rf "$LAB"
}

cmd_up() {
  command -v docker > /dev/null || die "docker is required"
  ensure_creds
  ensure_images

  docker network inspect "$NET" > /dev/null 2>&1 || docker network create --ipv6=false "$NET" > /dev/null

  # 1. Start MySQL 8.4 Primary
  docker rm -fv "$C_PRIMARY" > /dev/null 2>&1 || true
  log "starting $C_PRIMARY ($MYSQL_IMAGE) on 127.0.0.1:$PORT_PRIMARY"
  docker run -d --name "$C_PRIMARY" --hostname "$C_PRIMARY" --network "$NET" \
    -p "127.0.0.1:$PORT_PRIMARY:3306" \
    -e MYSQL_ROOT_PASSWORD="$ROOT_PW" \
    "$MYSQL_IMAGE" \
    --server-id=101 \
    --log-bin=mysql-bin \
    --binlog-format=ROW \
    --gtid-mode=ON \
    --enforce-gtid-consistency=ON \
    --report-host="$C_PRIMARY" \
    --report-port=3306 > /dev/null

  wait_mysql "$C_PRIMARY" "$ROOT_PW" || die "$C_PRIMARY did not become ready"

  log "seeding accounts and shop schema on $C_PRIMARY"
  mysql_exec "$C_PRIMARY" "$ROOT_PW" root <<SQL
CREATE USER IF NOT EXISTS 'mf_admin'@'%' IDENTIFIED WITH caching_sha2_password BY '$ADMIN_PW';
GRANT ALL PRIVILEGES ON *.* TO 'mf_admin'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'mf_repl'@'%' IDENTIFIED WITH caching_sha2_password BY '$REPL_PW';
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.* TO 'mf_repl'@'%';
CREATE USER IF NOT EXISTS 'mf_monitor'@'%' IDENTIFIED WITH caching_sha2_password BY '$PX_MON_PW';
GRANT REPLICATION CLIENT, PROCESS, SELECT ON *.* TO 'mf_monitor'@'%';
FLUSH PRIVILEGES;

CREATE DATABASE IF NOT EXISTS shop;
USE shop;
CREATE TABLE IF NOT EXISTS customers (
  id INT PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  tier VARCHAR(24) NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id INT PRIMARY KEY,
  customer_id INT NOT NULL,
  status VARCHAR(24) NOT NULL,
  total_cents INT NOT NULL,
  KEY idx_status (status),
  KEY idx_customer (customer_id)
);
REPLACE INTO customers (id, name, tier) VALUES
  (1, 'Ada Lovelace', 'enterprise'),
  (2, 'Grace Hopper', 'pro'),
  (3, 'Donald Knuth', 'enterprise'),
  (4, 'Barbara Liskov', 'pro');
REPLACE INTO orders (id, customer_id, status, total_cents) VALUES
  (101, 1, 'paid', 12900),
  (102, 1, 'paid', 4500),
  (103, 2, 'open', 8200),
  (104, 3, 'paid', 19900),
  (105, 4, 'refunded', 3100);
SQL

  # 2. Start MySQL 8.4 GTID Replica
  docker rm -fv "$C_REPLICA" > /dev/null 2>&1 || true
  log "starting $C_REPLICA ($MYSQL_IMAGE) on 127.0.0.1:$PORT_REPLICA"
  docker run -d --name "$C_REPLICA" --hostname "$C_REPLICA" --network "$NET" \
    -p "127.0.0.1:$PORT_REPLICA:3306" \
    -e MYSQL_ROOT_PASSWORD="$ROOT_PW" \
    "$MYSQL_IMAGE" \
    --server-id=102 \
    --log-bin=mysql-bin \
    --binlog-format=ROW \
    --gtid-mode=ON \
    --enforce-gtid-consistency=ON \
    --report-host="$C_REPLICA" \
    --report-port=3306 > /dev/null

  wait_mysql "$C_REPLICA" "$ROOT_PW" || die "$C_REPLICA did not become ready"

  mysql_exec "$C_REPLICA" "$ROOT_PW" root <<SQL
STOP REPLICA;
RESET REPLICA ALL;
CHANGE REPLICATION SOURCE TO
  SOURCE_HOST='$C_PRIMARY',
  SOURCE_PORT=3306,
  SOURCE_USER='mf_repl',
  SOURCE_PASSWORD='$REPL_PW',
  SOURCE_AUTO_POSITION=1,
  GET_SOURCE_PUBLIC_KEY=1;
START REPLICA;
SET GLOBAL super_read_only = ON;
SET GLOBAL read_only = ON;
SQL

  local repl_ok=0 i
  for i in $(seq 1 30); do
    local status_xml
    status_xml="$(mysql_exec "$C_REPLICA" "$ADMIN_PW" mf_admin --xml -e "SHOW REPLICA STATUS" < /dev/null 2>/dev/null || true)"
    if echo "$status_xml" | grep -q '<field name="Replica_IO_Running">Yes</field>' &&
       echo "$status_xml" | grep -q '<field name="Replica_SQL_Running">Yes</field>' &&
       mysql_exec "$C_REPLICA" "$ADMIN_PW" mf_admin --batch --skip-column-names -e "SELECT COUNT(*) FROM shop.orders" < /dev/null 2>/dev/null | grep -Fxq "5"; then
      repl_ok=1
      break
    fi
    sleep 1
  done
  [ "$repl_ok" = 1 ] || die "$C_REPLICA replication did not converge"
  log "$C_REPLICA GTID replication converged"

  # 3. Start Readyset connected to Primary
  docker rm -fv "$C_READYSET" > /dev/null 2>&1 || true
  log "starting $C_READYSET ($READYSET_IMAGE) on SQL 127.0.0.1:$PORT_RS_SQL and Admin 127.0.0.1:$PORT_RS_ADMIN"
  docker run -d --name "$C_READYSET" --hostname "$C_READYSET" --network "$NET" \
    -p "127.0.0.1:$PORT_RS_SQL:3307" \
    -p "127.0.0.1:$PORT_RS_ADMIN:6036" \
    -e UPSTREAM_DB_URL="mysql://mf_admin:${ADMIN_PW}@${C_PRIMARY}:3306/shop" \
    -e CDC_DB_URL="mysql://mf_admin:${ADMIN_PW}@${C_PRIMARY}:3306/shop" \
    -e ADMIN_CREDENTIALS="radmin:${RS_ADMIN_PW}" \
    "$READYSET_IMAGE" \
    --address=0.0.0.0:3307 \
    --admin-address=0.0.0.0:6036 \
    --mysql-authentication-method=caching_sha2_password \
    --cache-mode=deep-then-shallow \
    --require-gtid \
    --replication-server-id=103 \
    --report-host="$C_READYSET" \
    --report-port=3307 \
    --ignore-ulimit-check \
    --disable-telemetry > /dev/null

  local rs_ok=0
  for i in $(seq 1 60); do
    local rs_out
    rs_out="$(docker exec -i -e MYSQL_PWD="$ADMIN_PW" "$C_PRIMARY" mysql --ssl-mode=PREFERRED --get-server-public-key -h "$C_READYSET" -P 3307 -umf_admin shop --batch --skip-column-names -e "SHOW READYSET STATUS" < /dev/null 2>/dev/null || true)"
    if echo "$rs_out" | grep -q "^Status	Online$" && echo "$rs_out" | grep -q "^Database Connection	Connected$"; then
      rs_ok=1
      break
    fi
    sleep 1
  done
  if [ "$rs_ok" != 1 ]; then
    docker logs "$C_READYSET" | tail -n 60 >&2 || true
    die "$C_READYSET did not complete base-table snapshot"
  fi
  log "$C_READYSET snapshot completed and SQL/Admin ports ready"

  # 4. Start ProxySQL fronting Primary (HG10), Replica (HG12), and Readyset (HG20)
  docker rm -fv "$C_PROXYSQL" > /dev/null 2>&1 || true
  log "starting $C_PROXYSQL ($PROXYSQL_IMAGE) on Admin 127.0.0.1:$PORT_PX_ADMIN and MySQL 127.0.0.1:$PORT_PX_MYSQL"
  docker run -d --name "$C_PROXYSQL" --hostname "$C_PROXYSQL" --network "$NET" \
    -p "127.0.0.1:$PORT_PX_ADMIN:6032" \
    -p "127.0.0.1:$PORT_PX_MYSQL:6033" \
    "$PROXYSQL_IMAGE" > /dev/null

  local px_ok=0
  for i in $(seq 1 40); do
    if docker exec -i -e MYSQL_PWD="admin" "$C_PROXYSQL" mysql -h 127.0.0.1 -P 6032 -uadmin --batch --skip-column-names -e "SELECT 1" < /dev/null > /dev/null 2>&1; then
      px_ok=1
      break
    fi
    sleep 1
  done
  [ "$px_ok" = 1 ] || die "$C_PROXYSQL Admin did not start"

  docker exec -i -e MYSQL_PWD="admin" "$C_PROXYSQL" mysql -h 127.0.0.1 -P 6032 -uadmin <<SQL
UPDATE global_variables SET variable_value='admin:admin;radmin:${PX_ADMIN_PW}' WHERE variable_name='admin-admin_credentials';
UPDATE global_variables SET variable_value='mf_monitor' WHERE variable_name='mysql-monitor_username';
UPDATE global_variables SET variable_value='${PX_MON_PW}' WHERE variable_name='mysql-monitor_password';
UPDATE global_variables SET variable_value='8.4.0' WHERE variable_name='mysql-server_version';
LOAD ADMIN VARIABLES TO RUNTIME;
SAVE ADMIN VARIABLES TO DISK;
LOAD MYSQL VARIABLES TO RUNTIME;
SAVE MYSQL VARIABLES TO DISK;

REPLACE INTO mysql_users (username, password, default_hostgroup, default_schema, transaction_persistent, active, comment)
VALUES ('mf_admin', '${ADMIN_PW}', 10, 'shop', 1, 1, 'Readyset matrix app user');
LOAD MYSQL USERS TO RUNTIME;
SAVE MYSQL USERS TO DISK;

DELETE FROM mysql_servers;
INSERT INTO mysql_servers (hostgroup_id, hostname, port, weight, use_ssl, comment) VALUES
  (10, '${C_PRIMARY}', 3306, 1000, 1, 'MySQL primary'),
  (12, '${C_REPLICA}', 3306, 1000, 1, 'MySQL replica'),
  (20, '${C_READYSET}', 3307, 1000, 0, 'Readyset cache');

DELETE FROM mysql_replication_hostgroups;
INSERT INTO mysql_replication_hostgroups (writer_hostgroup, reader_hostgroup, check_type, comment)
VALUES (10, 12, 'read_only', 'Primary + GTID replica');

DELETE FROM mysql_query_rules;
INSERT INTO mysql_query_rules (rule_id, active, match_digest, destination_hostgroup, mirror_hostgroup, apply, comment) VALUES
  (10, 1, '^SELECT .* FROM orders WHERE status', 20, NULL, 1, 'Readyset cached order counts'),
  (20, 1, '^SELECT .* FROM customers', 12, 20, 1, 'QueryPilot mirror customers to Readyset'),
  (30, 1, '^SELECT .* FOR UPDATE', 10, NULL, 1, 'Locking reads go to primary'),
  (40, 1, '^SELECT', 12, NULL, 1, 'Other reads go to MySQL replica');

LOAD MYSQL SERVERS TO RUNTIME;
SAVE MYSQL SERVERS TO DISK;
LOAD MYSQL QUERY RULES TO RUNTIME;
SAVE MYSQL QUERY RULES TO DISK;
SQL

  # Warm a first login on Primary and Replica so ProxySQL caching_sha2_password fast-auth works,
  # and wait for runtime_mysql_servers to mark HG 10, 12, and 20 ONLINE.
  local hg_ok=0
  for i in $(seq 1 30); do
    local online_hgs
    online_hgs="$(docker exec -i -e MYSQL_PWD="$PX_ADMIN_PW" "$C_PRIMARY" mysql -h "$C_PROXYSQL" -P 6032 -uradmin --batch --skip-column-names -e "SELECT DISTINCT hostgroup_id FROM runtime_mysql_servers WHERE status='ONLINE' ORDER BY hostgroup_id" < /dev/null 2>/dev/null | tr '\n' ',' || true)"
    if [ "$online_hgs" = "10,12,20," ]; then
      hg_ok=1
      break
    fi
    sleep 1
  done
  [ "$hg_ok" = 1 ] || die "ProxySQL hostgroups 10, 12, 20 did not all come ONLINE"
  log "ProxySQL hostgroups 10 (writer), 12 (reader), and 20 (Readyset) are ONLINE"
}

cmd_status() {
  docker ps --filter "name=mf-rs-" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
}

cmd_test() {
  local allow_shutdown="${1:-1}" rc=0
  ensure_creds
  local tmp_home
  tmp_home="$(mktemp -d)"

  log "running live end-to-end verification in isolated HOME=$tmp_home (allow_shutdown=$allow_shutdown)"
  HOME="$tmp_home" \
  USERPROFILE="$tmp_home" \
  MYFLAMES_SECRET_BACKEND="vault" \
  REPO_ROOT="$REPO" \
  PORT_PRIMARY="$PORT_PRIMARY" \
  PORT_REPLICA="$PORT_REPLICA" \
  PORT_RS_SQL="$PORT_RS_SQL" \
  PORT_RS_ADMIN="$PORT_RS_ADMIN" \
  PORT_PX_ADMIN="$PORT_PX_ADMIN" \
  PORT_PX_MYSQL="$PORT_PX_MYSQL" \
  ADMIN_PW="$ADMIN_PW" \
  PX_ADMIN_PW="$PX_ADMIN_PW" \
  RS_ADMIN_PW="$RS_ADMIN_PW" \
  ALLOW_SHUTDOWN="$allow_shutdown" \
  python3 - <<'PY' || rc=$?
import json
import os
import subprocess
import sys
import time

repo = os.environ["REPO_ROOT"]
sys.path.insert(0, repo)
sys.path.insert(0, os.path.join(repo, "test"))

from test_readyset import assert_matches_schema
import unittest

from myflames import connector, dbops, mcp_server, proxysql, readyset, replication, reviewed_writes, ui
from myflames.live import connection_options
from myflames.workspace_store import WorkspaceStore

tc = unittest.TestCase()
tc.maxDiff = None

with open(os.path.join(repo, "docs", "schemas", "readyset-1.0.json"), "r", encoding="utf-8") as fh:
    RS_SCHEMA = json.load(fh)
with open(os.path.join(repo, "docs", "schemas", "proxysql-1.0.json"), "r", encoding="utf-8") as fh:
    PX_SCHEMA = json.load(fh)

port_primary = int(os.environ["PORT_PRIMARY"])
port_replica = int(os.environ["PORT_REPLICA"])
port_rs_sql = int(os.environ["PORT_RS_SQL"])
port_rs_admin = int(os.environ["PORT_RS_ADMIN"])
port_px_admin = int(os.environ["PORT_PX_ADMIN"])
port_px_mysql = int(os.environ["PORT_PX_MYSQL"])
admin_pw = os.environ["ADMIN_PW"]
px_admin_pw = os.environ["PX_ADMIN_PW"]
rs_admin_pw = os.environ["RS_ADMIN_PW"]
allow_shutdown = os.environ.get("ALLOW_SHUTDOWN") == "1"

store = WorkspaceStore()

def save_conn(name, port, user, password, database=None, ssl_mode="PREFERRED"):
    saved = store.save(
        "profile",
        {
            "name": name,
            "host": "127.0.0.1",
            "port": port,
            "user": user,
            "database": database or "",
            "ssl_mode": ssl_mode,
            "type": "development",
            "read_only": False,
        },
        password=password,
        remember=True,
    )
    return store.connection_with_saved_password({"profile_id": saved["id"], **saved})

c_primary = save_conn("rs-primary", port_primary, "mf_admin", admin_pw, "shop", "PREFERRED")
c_replica = save_conn("rs-replica", port_replica, "mf_admin", admin_pw, "shop", "PREFERRED")
c_rs_sql = save_conn("rs-adapter", port_rs_sql, "mf_admin", admin_pw, "shop", "PREFERRED")
c_rs_admin = save_conn("rs-admin", port_rs_admin, "radmin", rs_admin_pw, "", "PREFERRED")
c_px_admin = save_conn("rs-proxysql", port_px_admin, "radmin", px_admin_pw, "", "PREFERRED")

ws = type("Workspace", (), {"store": store})()

print("[1/7] Sending live SQL traffic through Readyset and ProxySQL...")
with connector.MySQLConnection(host="127.0.0.1", port=port_rs_sql, user="mf_admin", password=admin_pw, database="shop", ssl_mode="PREFERRED") as rs_client:
    for _ in range(3):
        rs_client.query_rows("SELECT COUNT(*) FROM orders WHERE status = 'paid'")
        rs_client.query_rows("SELECT name, tier FROM customers WHERE id = 1")

with connector.MySQLConnection(host="127.0.0.1", port=port_px_mysql, user="mf_admin", password=admin_pw, database="shop", ssl_mode="PREFERRED") as px_client:
    for _ in range(3):
        px_client.query_rows("SELECT COUNT(*) FROM orders WHERE status = 'paid'")
        px_client.query_rows("SELECT name, tier FROM customers WHERE id = 2")

print("[2/7] Verifying multi-tier topology discovery across ProxySQL, Readyset, and Replication...")
px_rep = ui.api_response("/api/live/proxysql", {"connection": c_px_admin}, ws)
assert_matches_schema(tc, px_rep, PX_SCHEMA)
tc.assertTrue(px_rep["available"])
hg_roles = {hg["id"]: hg["role"] for hg in px_rep["hostgroups"]}
tc.assertEqual(hg_roles.get(10), "writer")
tc.assertEqual(hg_roles.get(12), "reader")
tc.assertTrue(px_rep["topology"]["has_readyset"], f"Expected has_readyset=True: {px_rep['topology']}")
rs_nodes = px_rep["topology"].get("readyset_nodes") or []
tc.assertTrue(any(n["hostgroup_id"] == 20 and n["routing_mode"] == "cached_and_mirrored" for n in rs_nodes), f"Expected HG 20 cached_and_mirrored Readyset node: {rs_nodes}")

rs_rep = ui.api_response("/api/live/readyset", {"connection": c_rs_sql}, ws)
assert_matches_schema(tc, rs_rep, RS_SCHEMA)
tc.assertTrue(rs_rep["available"])
tc.assertTrue(rs_rep["is_readyset"])
tc.assertEqual(rs_rep["port_role"], "adapter")
tc.assertEqual(rs_rep["status_fields"]["status"], "Online")
tc.assertEqual(rs_rep["replication"]["mode"], "mysql_gtid")
tc.assertEqual(rs_rep["replication"]["lag_unit"], "transactions")
tc.assertTrue(any("orders" in t["table"] and t["bucket"] == "snapshotted" for t in rs_rep["tables"]), rs_rep["tables"])
tc.assertTrue(any("customers" in t["table"] and t["bucket"] == "snapshotted" for t in rs_rep["tables"]), rs_rep["tables"])
tc.assertTrue(len(rs_rep["topology"]["upstreams"]) >= 1, f"Expected upstreams in Readyset topology: {rs_rep['topology']}")
tc.assertTrue(len(rs_rep["topology"]["proxysql_nodes"]) >= 1, f"Expected ProxySQL front-door in Readyset topology: {rs_rep['topology']}")
peer_hosts = [p.get("host") for p in (rs_rep["topology"].get("database_peers") or [])]
peer_names = [p.get("profile_name") for p in (rs_rep["topology"].get("database_peers") or [])]
tc.assertTrue("mf-rs-replica" in peer_hosts or "rs-replica" in peer_names, f"Expected GTID replica in Readyset database_peers: {rs_rep['topology']}")
tc.assertNotIn("mf-rs-readyset", peer_hosts, "Readyset binlog follower must not appear as a downstream MySQL replica peer")
tc.assertTrue(len(rs_rep["proxied_queries"]) >= 2, f"Expected proxied queries in Readyset: {rs_rep['proxied_queries']}")

repl_primary = ui.api_response("/api/live/replication", {"connection": c_primary}, ws)
tc.assertTrue(repl_primary["available"])
tiers = repl_primary.get("topology_tiers") or {}
tc.assertTrue(len(tiers.get("proxysql") or []) >= 1, f"Expected ProxySQL tier in primary Replication view: {tiers}")
tc.assertEqual(len(tiers.get("readyset") or []), 1, f"Expected deduplicated Readyset SQL tier in primary Replication view: {tiers}")

repl_replica = ui.api_response("/api/live/replication", {"connection": c_replica}, ws)
tc.assertTrue(repl_replica["available"])
tc.assertEqual(repl_replica["channels"][0]["source"]["host"], "mf-rs-primary")
rep_tiers = repl_replica.get("topology_tiers") or {}
tc.assertTrue(len(rep_tiers.get("proxysql") or []) >= 1, f"Expected ProxySQL tier in replica Replication view: {rep_tiers}")
tc.assertEqual(len(rep_tiers.get("readyset") or []), 1, f"Expected deduplicated Readyset SQL tier in replica Replication view: {rep_tiers}")

print("[3/7] Verifying live EXPLAIN CREATE CACHE (supported, unsupported, and collisions)...")
exp_ok = ui.api_response("/api/live/readyset/explain-cache", {
    "connection": c_rs_sql,
    "query_or_id": "SELECT COUNT(*) FROM orders WHERE status = 'paid'",
    "cache_type": "both",
}, ws)
tc.assertEqual(exp_ok["shallow"]["supported"], "yes", exp_ok)
tc.assertEqual(exp_ok["deep"]["supported"], "yes", exp_ok)

exp_asym = ui.api_response("/api/live/readyset/explain-cache", {
    "connection": c_rs_sql,
    "query_or_id": "SELECT RAND(), id FROM orders",
    "cache_type": "both",
}, ws)
tc.assertEqual(exp_asym["shallow"]["supported"], "yes", exp_asym)
tc.assertEqual(exp_asym["deep"]["supported"], "unsupported", exp_asym)

with tc.assertRaises(ValueError):
    ui.api_response("/api/live/readyset/explain-cache", {
        "connection": c_rs_sql,
        "query_or_id": "SELECT id FROM orders FOR UPDATE",
        "cache_type": "both",
    }, ws)

def run_reviewed_action(conn_dict, action, target=None, value=None, typed=None, allowed_statuses=("done",)):
    plan_payload = {"connection": conn_dict, "action": action, "target": target or {}}
    if value is not None:
        plan_payload["value"] = value
    plan = ui.api_response("/api/live/readyset/plan", plan_payload, ws)
    plan_copy = dict(plan)
    token = plan_copy.pop("review_token", None)
    sent_typed = plan_copy.get("typed_guard") if typed is None else typed
    res = ui.api_response("/api/live/readyset/action", {
        "connection": conn_dict,
        "plan": plan_copy,
        "review_token": token,
        "typed": sent_typed,
        "confirmed": True,
    }, ws)
    tc.assertIn(res["status"], allowed_statuses, f"Action {action} unexpected status: {res}")
    return res

print("[4/7] Creating live Shallow & Deep caches, checking collisions, telemetry, and DDL export...")
run_reviewed_action(c_rs_sql, "create_cache", target={
    "cache_type": "shallow",
    "name": "c_live_shallow",
    "query": "SELECT name, tier FROM customers WHERE id = 1",
    "ttl_seconds": 30,
    "coalesce_ms": 100,
})
run_reviewed_action(c_rs_sql, "create_cache", target={
    "cache_type": "deep",
    "name": "c_live_deep",
    "query": "SELECT COUNT(*) FROM orders WHERE status = 'paid'",
})

# Collision check against live c_live_deep
exp_col = ui.api_response("/api/live/readyset/explain-cache", {
    "connection": c_rs_sql,
    "query_or_id": "SELECT COUNT(*) FROM orders WHERE status = 'paid'",
    "cache_type": "both",
}, ws)
tc.assertTrue(any(c["name"] == "c_live_deep" for c in exp_col["collisions"]), f"Expected collision on c_live_deep: {exp_col}")

# Execute cached reads through both Readyset and ProxySQL
with connector.MySQLConnection(host="127.0.0.1", port=port_rs_sql, user="mf_admin", password=admin_pw, database="shop", ssl_mode="PREFERRED") as rs_client:
    for _ in range(8):
        rows_d = rs_client.query_rows("SELECT COUNT(*) FROM orders WHERE status = 'paid'")
        tc.assertEqual(int(rows_d[0][0]), 3)
        rows_s = rs_client.query_rows("SELECT name, tier FROM customers WHERE id = 1")
        tc.assertEqual(rows_s[0][0], "Ada Lovelace")

with connector.MySQLConnection(host="127.0.0.1", port=port_px_mysql, user="mf_admin", password=admin_pw, database="shop", ssl_mode="PREFERRED") as px_client:
    for _ in range(5):
        rows_px = px_client.query_rows("SELECT COUNT(*) FROM orders WHERE status = 'paid'")
        tc.assertEqual(int(rows_px[0][0]), 3)

rs_cached = ui.api_response("/api/live/readyset", {"connection": c_rs_sql}, ws)
assert_matches_schema(tc, rs_cached, RS_SCHEMA)
caches_by_name = {c["name"]: c for c in rs_cached["caches"]}
tc.assertIn("c_live_shallow", caches_by_name)
tc.assertIn("c_live_deep", caches_by_name)
tc.assertEqual(caches_by_name["c_live_shallow"]["cache_type"], "shallow")
tc.assertEqual(caches_by_name["c_live_deep"]["cache_type"], "deep")
tc.assertGreater(rs_cached["telemetry"]["cached_executions"], 0)
tc.assertGreater(rs_cached["telemetry"]["shallow_hits"], 0)

ddl = ui.api_response("/api/live/readyset/export-caches", {"connection": c_rs_sql}, ws)
tc.assertEqual(ddl["count"], 2, ddl)
tc.assertIn("c_live_shallow", ddl["sql"])
tc.assertIn("c_live_deep", ddl["sql"])

print("[5/7] Verifying CLI (`myflames readyset`) and MCP (`readyset_status`) against live Readyset...")
cli_json = subprocess.check_output(
    [sys.executable, "-m", "myflames", "readyset", "--profile", "rs-adapter", "--json"],
    cwd=repo,
    text=True,
)
cli_parsed = json.loads(cli_json)
assert_matches_schema(tc, cli_parsed, RS_SCHEMA)
tc.assertNotIn(admin_pw, cli_json)

cli_export = subprocess.check_output(
    [sys.executable, "-m", "myflames", "readyset", "--profile", "rs-adapter", "--export-caches"],
    cwd=repo,
    text=True,
)
tc.assertIn("c_live_shallow", cli_export)
tc.assertIn("c_live_deep", cli_export)

mcp_compact = mcp_server.readyset_status_tool(profile="rs-adapter")
tc.assertIsInstance(mcp_compact, str)
tc.assertIn("Readyset", mcp_compact)
tc.assertNotIn(admin_pw, mcp_compact)
mcp_full = mcp_server.readyset_status_tool(profile="rs-adapter", detail="full")
assert_matches_schema(tc, mcp_full, RS_SCHEMA)
tc.assertNotIn(admin_pw, json.dumps(mcp_full))

print("[6/7] Exercising dynamic ALTER READYSET parameters, allowlists, maintenance mode, resnapshot, and flush/drop...")
run_reviewed_action(c_rs_sql, "set_log_level", target={"directives": "info"})
run_reviewed_action(c_rs_sql, "set_eviction_memory_limit", target={"bytes": "2147483648"})
run_reviewed_action(c_rs_sql, "set_eviction_period", target={"milliseconds": 500})
run_reviewed_action(c_rs_sql, "add_shallow_allowed", target={"kind": "function", "identifier": "NOW"})
rs_allow = ui.api_response("/api/live/readyset", {"connection": c_rs_sql}, ws)
tc.assertTrue(
    any(fn.upper() == "NOW" for fn in rs_allow["shallow_allowlists"]["functions"]),
    f"Expected NOW in shallow_allowlists.functions: {rs_allow['shallow_allowlists']}",
)
run_reviewed_action(c_rs_sql, "drop_shallow_allowed", target={"kind": "function", "identifier": "NOW"})

run_reviewed_action(c_rs_sql, "enter_maintenance_mode")
rs_maint = ui.api_response("/api/live/readyset", {"connection": c_rs_sql}, ws)
tc.assertEqual(rs_maint["status_fields"]["status"], "Maintenance Mode")
run_reviewed_action(c_rs_sql, "exit_maintenance_mode")
rs_unmaint = ui.api_response("/api/live/readyset", {"connection": c_rs_sql}, ws)
tc.assertEqual(rs_unmaint["status_fields"]["status"], "Online")

run_reviewed_action(c_rs_sql, "flush_privileges")
run_reviewed_action(c_rs_sql, "flush_cache", target={"name": "c_live_shallow"})
run_reviewed_action(c_rs_sql, "flush_all_shallow_caches")

run_reviewed_action(c_rs_sql, "resnapshot_table", target={"table": "`shop`.`orders`"})
for _ in range(30):
    rs_snap = ui.api_response("/api/live/readyset", {"connection": c_rs_sql}, ws)
    if (
        rs_snap["table_counts"]["snapshotting"] == 0
        and rs_snap["status_fields"].get("status") == "Online"
        and any("orders" in t["table"] and t["status"] == "Online" for t in rs_snap["tables"])
    ):
        break
    time.sleep(1)

# Shallow caches survive base-table resnapshotting; dependent deep caches are dropped by Noria's
# base-table reset and can be re-created once the table returns Online.
rs_after_snap = ui.api_response("/api/live/readyset", {"connection": c_rs_sql}, ws)
after_snap_names = {c["name"] for c in rs_after_snap["caches"]}
tc.assertIn("c_live_shallow", after_snap_names)
if "c_live_deep" not in after_snap_names:
    run_reviewed_action(
        c_rs_sql,
        "create_cache",
        target={
            "cache_type": "deep",
            "name": "c_live_deep",
            "query": "SELECT COUNT(*) FROM orders WHERE status = 'paid'",
        },
    )

run_reviewed_action(c_rs_sql, "drop_cache", target={"name": "c_live_shallow"})
run_reviewed_action(c_rs_sql, "drop_cache", target={"name": "c_live_deep"})

print("[7/7] Verifying Admin SQL port (6036) capabilities, audit trail, and shutdown...")
rs_admin_rep = ui.api_response("/api/live/readyset", {"connection": c_rs_admin}, ws)
assert_matches_schema(tc, rs_admin_rep, RS_SCHEMA)
tc.assertEqual(rs_admin_rep["port_role"], "admin")
tc.assertTrue(rs_admin_rep["capabilities"]["can_shutdown"])
tc.assertFalse(rs_admin_rep["capabilities"]["can_create_cache"])

audit_log = ui.api_response("/api/audit/list", {}, ws)
tc.assertGreaterEqual(len(audit_log["records"]), 12)
for secret in (admin_pw, px_admin_pw, rs_admin_pw):
    tc.assertNotIn(secret, json.dumps(audit_log))

if allow_shutdown:
    run_reviewed_action(c_rs_admin, "shutdown", allowed_statuses=("done", "unknown"))

print("ALL LIVE READYSET + PROXYSQL + MYSQL TOPOLOGY & ACTION CHECKS PASSED!")
PY
  rm -rf "$tmp_home"
  return "$rc"
}

main() {
  case "${1:-run}" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    test) cmd_test "${2:-0}" ;;
    --keep)
      cmd_up
      cmd_test 0
      log "containers left running (--keep). Tear down with: scripts/readyset-matrix.sh down"
      ;;
    run)
      trap 'cmd_down' EXIT
      cmd_up
      cmd_test 1
      ;;
    *)
      die "unknown command '$1' (expected: up | test | status | down | --keep)"
      ;;
  esac
}

main "$@"
