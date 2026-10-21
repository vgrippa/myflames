#!/usr/bin/env bash
# scripts/ga-matrix.sh
#
# Bring up every GA MySQL, Percona Server and MariaDB version in Docker with
# realistic data, plus one replica pair per engine family, and create saved
# myflames connections to them in an ISOLATED home (never the real
# ~/.myflames). For review and compatibility testing only: the servers are
# throwaway and mf_admin has ALL PRIVILEGES.
#
# Usage:
#   scripts/ga-matrix.sh up [NAME...]        pull, start, configure, seed, workload (idempotent)
#   scripts/ga-matrix.sh down [NAME...]      remove containers and anonymous volumes
#   scripts/ga-matrix.sh status              one line per server
#   scripts/ga-matrix.sh seed NAME           seed testdb + review schema if absent
#   scripts/ga-matrix.sh workload NAME       warm-up statements (digests) + one deadlock
#   scripts/ga-matrix.sh locks NAME SECONDS  row lock held by one session, a second waits
#   scripts/ga-matrix.sh idle-trx NAME SECONDS   open transaction left idle
#   scripts/ga-matrix.sh mdl NAME SECONDS    metadata lock wait (open trx + waiting ALTER)
#   scripts/ga-matrix.sh profiles            create the saved connections in $MYFLAMES_HOME
#   scripts/ga-matrix.sh env                 print the matrix as JSON
#
# Names: mysql-8.0 mysql-8.4 mysql-9.7 mysql-26.7 percona-8.0 percona-8.4
#        mariadb-10.6 mariadb-10.11 mariadb-11.4 mariadb-11.8 mariadb-12.3
#        mariadb-13.0 mysql-9.7-replica percona-8.4-replica mariadb-11.8-replica
#
# Percona Server for MySQL is a MySQL flavor: it uses the mysql client and
# MySQL rules, from the official percona/percona-server images (arm64 and
# amd64). percona-8.0 runs the thread pool (thread_handling=pool-of-threads)
# and the slow log with log_slow_verbosity=full; percona-8.4 runs with
# userstat=ON (INFORMATION_SCHEMA.INDEX_STATISTICS, TABLE_STATISTICS) and
# GTIDs, replicated to percona-8.4-replica.
#
# Settings (environment):
#   MYFLAMES_HOME   isolated HOME for myflames profiles (default local/ga-matrix/home)
#   CREDS_FILE      default local/ga-matrix/credentials.env (gitignored, chmod 600)
#
# All ports are bound to 127.0.0.1. Requirements: Docker, openssl, python3.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAB="$REPO/local/ga-matrix"
CREDS_FILE="${CREDS_FILE:-$LAB/credentials.env}"
MYFLAMES_HOME="${MYFLAMES_HOME:-$LAB/home}"
STATE="$LAB/state"
LOGS="$LAB/logs"
NET="myflames-ga-net"
ALL="mysql-8.0 mysql-8.4 mysql-9.7 mysql-26.7 percona-8.0 percona-8.4 mariadb-10.6 mariadb-10.11 mariadb-11.4 mariadb-11.8 mariadb-12.3 mariadb-13.0 mysql-9.7-replica percona-8.4-replica mariadb-11.8-replica"

log() { echo "[ga-matrix] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# name -> "image engine port role server-id slow-log source"
spec() {
  case "$1" in
    mysql-8.0)          echo "mysql:8.0.46 mysql 33080 primary 101 on -" ;;
    mysql-8.4)          echo "mysql:8.4.11 mysql 33084 primary 102 off -" ;;
    mysql-9.7)          echo "mysql:9.7.2 mysql 33097 primary 103 on -" ;;
    mysql-26.7)         echo "mysql:26.7.0 mysql 33267 primary 104 off -" ;;
    percona-8.0)        echo "percona/percona-server:8.0.46-37.1 mysql 33180 primary 113 on -" ;;
    percona-8.4)        echo "percona/percona-server:8.4.11-11.1 mysql 33184 primary 114 off -" ;;
    mariadb-10.6)       echo "mariadb:10.6.28 mariadb 33106 primary 105 on -" ;;
    mariadb-10.11)      echo "mariadb:10.11.19 mariadb 33111 primary 106 off -" ;;
    mariadb-11.4)       echo "mariadb:11.4.13 mariadb 33114 primary 107 on -" ;;
    mariadb-11.8)       echo "mariadb:11.8.9 mariadb 33118 primary 108 off -" ;;
    mariadb-12.3)       echo "mariadb:12.3.3 mariadb 33123 primary 109 on -" ;;
    mariadb-13.0)       echo "mariadb:13.0.2 mariadb 33130 primary 110 off -" ;;
    mysql-9.7-replica)  echo "mysql:9.7.2 mysql 33098 replica 111 off mysql-9.7" ;;
    mariadb-11.8-replica) echo "mariadb:11.8.9 mariadb 33119 replica 112 off mariadb-11.8" ;;
    percona-8.4-replica) echo "percona/percona-server:8.4.11-11.1 mysql 33185 replica 115 off percona-8.4" ;;
    *) return 1 ;;
  esac
}
valid() { spec "$1" > /dev/null 2>&1 || die "unknown server '$1' (known: $ALL)"; }
cname() { echo "myflames-ga-${1//./-}"; }
# f NAME index (1=image 2=engine 3=port 4=role 5=server-id 6=slow-log 7=source)
f() { local n="$1" i="$2" s; s="$(spec "$n")"; set -- $s; eval "echo \${$i}"; }
# The product family for reports: percona (a MySQL flavor), mysql or mariadb.
flavor() { case "$(f "$1" 1)" in percona/*) echo percona ;; *) f "$1" 2 ;; esac; }
client() { [ "$(f "$1" 2)" = mariadb ] && echo mariadb || echo mysql; }
is_up() { docker ps --format '{{.Names}}' | grep -Fxq "$(cname "$1")"; }

load_creds() {
  [ -f "$CREDS_FILE" ] || create_creds
  # shellcheck disable=SC1090
  . "$CREDS_FILE"
}
create_creds() {
  mkdir -p "$(dirname "$CREDS_FILE")"
  chmod 700 "$LAB"
  umask 077
  printf 'ROOT_PASSWORD=%s\nMF_ADMIN_PASSWORD=%s\nMF_READ_PASSWORD=%s\nREPL_PASSWORD=%s\n' \
    "$(openssl rand -hex 12)" "$(openssl rand -hex 12)" "$(openssl rand -hex 12)" "$(openssl rand -hex 12)" > "$CREDS_FILE"
  chmod 600 "$CREDS_FILE"
}

# sqlx NAME USER PASSWORD [client args]: SQL on stdin; the password travels in
# the container environment, never on a command line.
sqlx() {
  local n="$1" user="$2" pw="$3"; shift 3
  ( export MYSQL_PWD="$pw"; docker exec -i -e MYSQL_PWD "$(cname "$n")" "$(client "$n")" -u"$user" "$@" )
}
rootsql() { sqlx "$1" root "$ROOT_PASSWORD" "${@:2}"; }
rq() { local n="$1"; shift; rootsql "$n" --batch --skip-column-names -e "$*" 2> /dev/null; }

wait_ready() {
  local n="$1" i
  for i in $(seq 1 240); do
    if is_up "$n" && ( export MYSQL_PWD="$ROOT_PASSWORD"; docker exec -e MYSQL_PWD "$(cname "$n")" "$(client "$n")" \
        -uroot -h127.0.0.1 --protocol=TCP -e "SELECT 1" > /dev/null 2>&1 ); then
      log "$n ready (${i}s)"; return 0
    fi
    is_up "$n" || { log "$n exited:"; docker logs --tail 15 "$(cname "$n")" >&2 2>&1 || true; return 1; }
    sleep 1
  done
  return 1
}

start_container() {
  local n="$1" image engine port role sid slow source cn
  read -r image engine port role sid slow source <<< "$(spec "$n")"
  cn="$(cname "$n")"
  if is_up "$n"; then log "$n already running"; return 0; fi
  if docker ps -a --format '{{.Names}}' | grep -Fxq "$cn"; then docker start "$cn" > /dev/null; return 0; fi
  local sl=0; [ "$slow" = on ] && sl=1
  local args=()
  local envname=MYSQL_ROOT_PASSWORD
  local extra_env=()
  # Percona images send telemetry unless told not to.
  [ "$(flavor "$n")" = percona ] && extra_env=(-e PERCONA_TELEMETRY_DISABLE=1)
  if [ "$engine" = mysql ]; then
    args=(--performance-schema=ON --innodb-buffer-pool-size=128M --log-bin=mysql-bin --server-id="$sid"
          --slow-query-log="$sl" --long-query-time=0.2 --slow-query-log-file=/var/lib/mysql/slow.log)
    case "$n" in mysql-9.7|mysql-9.7-replica|percona-8.4|percona-8.4-replica) args+=(--gtid-mode=ON --enforce-gtid-consistency=ON) ;; esac
    # Percona Server features the views read when the server reports them.
    case "$n" in
      percona-8.0) args+=(--thread-handling=pool-of-threads --log-slow-verbosity=full) ;;
      percona-8.4) args+=(--userstat=ON) ;;
    esac
  else
    envname=MARIADB_ROOT_PASSWORD
    args=(--performance-schema=ON --innodb-buffer-pool-size=128M --log-bin=mysql-bin --server-id="$sid"
          --log-error=/var/lib/mysql/mariadb-error.log
          --max-recursive-iterations=1000000
          --slow-query-log="$sl" --long-query-time=0.2 --slow-query-log-file=/var/lib/mysql/slow.log
          "--performance-schema-instrument=statement/%=ON"
          --performance-schema-consumer-events-statements-current=ON
          --performance-schema-consumer-events-statements-history=ON
          --performance-schema-consumer-statements-digest=ON)
  fi
  docker network inspect "$NET" > /dev/null 2>&1 || docker network create "$NET" > /dev/null 2>&1 || true
  # -e NAME without a value copies it from this process: no password in ps.
  ( export "$envname=$ROOT_PASSWORD"
    docker run -d --name "$cn" --network "$NET" -p "127.0.0.1:$port:3306" -e "$envname" \
      ${extra_env[@]+"${extra_env[@]}"} --label myflames-ga=1 "$image" "${args[@]}" > /dev/null )
}

configure_perf() {
  rootsql "$1" <<'SQL' > /dev/null
SET sql_log_bin=0;
UPDATE performance_schema.setup_consumers SET ENABLED='YES'
  WHERE NAME IN ('events_statements_current','events_statements_history','statements_digest');
UPDATE performance_schema.setup_instruments SET ENABLED='YES', TIMED='YES' WHERE NAME LIKE 'statement/%';
SQL
}

create_accounts() {
  local n="$1"
  rootsql "$n" > /dev/null <<SQL
CREATE USER IF NOT EXISTS 'mf_admin'@'%' IDENTIFIED BY '$MF_ADMIN_PASSWORD';
GRANT ALL PRIVILEGES ON *.* TO 'mf_admin'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'mf_read'@'%' IDENTIFIED BY '$MF_READ_PASSWORD';
GRANT SELECT, PROCESS, REPLICATION CLIENT, SHOW VIEW ON *.* TO 'mf_read'@'%';
CREATE USER IF NOT EXISTS 'repl'@'%' IDENTIFIED BY '$REPL_PASSWORD';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
SQL
}

is_seeded() { [ "$(rq "$1" "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='review' AND table_name='repl_probe'")" = 1 ]; }

review_sql() {
  local cte="SET SESSION cte_max_recursion_depth=1000000;"
  [ "$(f "$1" 2)" = mariadb ] && cte="SET SESSION max_recursive_iterations=1000000;"
  cat <<SQL
$cte
CREATE DATABASE IF NOT EXISTS review;
USE review;

-- A table without a primary key.
CREATE TABLE no_pk_events (
  event_type VARCHAR(30) NOT NULL, user_id INT NOT NULL, payload VARCHAR(100), created_at DATETIME NOT NULL
) ENGINE=InnoDB;
INSERT INTO no_pk_events
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 3000)
SELECT ELT(1 + MOD(n, 3), 'click', 'view', 'buy'), 1 + MOD(n * 7, 500), CONCAT('payload-', n),
       DATE_ADD('2024-01-01', INTERVAL MOD(n, 300) DAY) FROM c;

-- A redundant index: idx_customer is a prefix of idx_customer_status.
CREATE TABLE redundant_idx (
  id INT AUTO_INCREMENT PRIMARY KEY, customer_id INT NOT NULL, status TINYINT NOT NULL, note VARCHAR(50),
  KEY idx_customer (customer_id), KEY idx_customer_status (customer_id, status)
) ENGINE=InnoDB;
INSERT INTO redundant_idx (customer_id, status, note)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 3000)
SELECT 1 + MOD(n * 3, 200), MOD(n, 4), CONCAT('note-', n) FROM c;

-- A foreign key.
CREATE TABLE parent_accounts (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50) NOT NULL) ENGINE=InnoDB;
CREATE TABLE child_invoices (
  id INT AUTO_INCREMENT PRIMARY KEY, account_id INT NOT NULL, amount DECIMAL(10,2) NOT NULL,
  CONSTRAINT fk_invoice_account FOREIGN KEY (account_id) REFERENCES parent_accounts (id)
) ENGINE=InnoDB;
INSERT INTO parent_accounts (name)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 100) SELECT CONCAT('Account ', n) FROM c;
INSERT INTO child_invoices (account_id, amount)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 1000)
SELECT 1 + MOD(n, 100), ROUND(10 + MOD(n * 13, 400) + 0.5, 2) FROM c;

-- Reusable free space: a few thousand rows, then a large DELETE.
CREATE TABLE churn (
  id INT AUTO_INCREMENT PRIMARY KEY, bucket INT NOT NULL, payload VARCHAR(400) NOT NULL, KEY idx_bucket (bucket)
) ENGINE=InnoDB;
INSERT INTO churn (bucket, payload)
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n < 20000)
SELECT MOD(n, 50), REPEAT('x', 350) FROM c;
DELETE FROM churn WHERE id <= 18000;

CREATE TABLE lock_probe (id INT PRIMARY KEY, v INT NOT NULL) ENGINE=InnoDB;
INSERT INTO lock_probe VALUES (1, 0), (2, 0);
CREATE TABLE mdl_probe (id INT PRIMARY KEY) ENGINE=InnoDB;
INSERT INTO mdl_probe VALUES (1);

CREATE VIEW v_top_customers AS
  SELECT customer_id, COUNT(*) AS n FROM redundant_idx GROUP BY customer_id;

DELIMITER //
CREATE PROCEDURE sp_recent_orders(IN p_user INT)
BEGIN
  SELECT id, status, total, created_at FROM testdb.orders WHERE user_id = p_user ORDER BY created_at DESC LIMIT 10;
END//
DELIMITER ;

CREATE TABLE repl_probe (id INT PRIMARY KEY, note VARCHAR(30)) ENGINE=InnoDB;
SQL
}

seed_one() {
  local n="$1"
  [ "$(f "$n" 4)" = primary ] || die "$n is a replica; it receives its data through replication"
  if is_seeded "$n"; then log "$n already seeded"; return 0; fi
  log "$n seeding testdb and review..."
  # Reuse the fixture generator's schema so the two never drift apart.
  local sql
  sql="$(awk "/^mysql_exec << 'SQL'/{f=1;next} /^SQL\$/{f=0} f" "$REPO/scripts/generate-fixtures.sh")"
  # MariaDB names the recursion limit max_recursive_iterations (default 1000).
  [ "$(f "$n" 2)" = mariadb ] && sql="$(printf '%s\n' "$sql" | sed 's/cte_max_recursion_depth *=/max_recursive_iterations =/')"
  printf '%s\n' "$sql" | rootsql "$n" > /dev/null || die "$n: testdb seed failed"
  review_sql "$n" | rootsql "$n" > /dev/null || die "$n: review schema failed"
  # Let purge finish so DATA_FREE reflects the DELETE.
  sleep 3
  rootsql "$n" -e "ANALYZE TABLE review.churn, review.no_pk_events, review.redundant_idx" > /dev/null 2>&1 || true
}

workload_sql() {
  cat <<'SQL'
SELECT COUNT(*) FROM testdb.orders;
SELECT * FROM testdb.users WHERE id = 42;
SELECT u.name, o.total FROM testdb.users u JOIN testdb.orders o ON o.user_id = u.id WHERE u.country = 'US' LIMIT 100;
SELECT p.name, SUM(oi.quantity) q FROM testdb.products p JOIN testdb.order_items oi ON oi.product_id = p.id GROUP BY p.id, p.name ORDER BY q DESC LIMIT 20;
SELECT country, COUNT(*) FROM testdb.users GROUP BY country;
SELECT * FROM testdb.orders ORDER BY total DESC LIMIT 50;
SELECT YEAR(created_at) y, COUNT(*) FROM testdb.orders GROUP BY y ORDER BY COUNT(*) DESC;
SELECT * FROM testdb.reviews WHERE body IS NULL AND rating = 3 LIMIT 100;
SELECT * FROM testdb.users WHERE name LIKE '%99%';
SELECT * FROM testdb.products WHERE name LIKE 'Product-1%' ORDER BY price;
SELECT o.id, u.email, SUM(oi.quantity * oi.unit_price) s FROM testdb.orders o JOIN testdb.users u ON u.id = o.user_id JOIN testdb.order_items oi ON oi.order_id = o.id WHERE o.status = 'shipped' GROUP BY o.id, u.email LIMIT 50;
SELECT category_id, AVG(price) FROM testdb.products GROUP BY category_id HAVING AVG(price) > 100;
SELECT * FROM testdb.order_items WHERE quantity > 8 ORDER BY unit_price DESC LIMIT 25;
SELECT DISTINCT user_id FROM testdb.orders WHERE total > 900;
SELECT u.country, COUNT(DISTINCT o.id) FROM testdb.users u LEFT JOIN testdb.orders o ON o.user_id = u.id GROUP BY u.country;
SELECT * FROM testdb.reviews r JOIN testdb.products p ON p.id = r.product_id WHERE r.rating = 5 ORDER BY r.created_at DESC LIMIT 30;
SELECT COUNT(*) FROM testdb.order_items oi JOIN testdb.orders o ON o.id = oi.order_id WHERE o.created_at >= '2023-01-01';
SELECT * FROM testdb.orders WHERE user_id IN (SELECT id FROM testdb.users WHERE country = 'JP') LIMIT 100;
SELECT status, COUNT(*), SUM(total) FROM testdb.orders GROUP BY status;
SELECT * FROM testdb.categories c LEFT JOIN testdb.products p ON p.category_id = c.id WHERE c.parent_id IS NULL LIMIT 50;
SELECT * FROM review.no_pk_events WHERE user_id = 7;
SELECT * FROM review.redundant_idx WHERE customer_id = 5 AND status = 1;
SELECT * FROM review.v_top_customers ORDER BY n DESC LIMIT 5;
CALL review.sp_recent_orders(10);
SELECT i.id, a.name FROM review.child_invoices i JOIN review.parent_accounts a ON a.id = i.account_id WHERE i.amount > 50;
SELECT COUNT(*) FROM review.churn WHERE bucket = 3;
SELECT MAX(total), MIN(total), AVG(total) FROM testdb.orders WHERE created_at BETWEEN '2022-03-01' AND '2022-06-01';
SELECT name FROM testdb.users ORDER BY name DESC LIMIT 20;
SELECT CONCAT(name, '@', country) FROM testdb.users WHERE LOWER(email) LIKE 'user1%';
SELECT SLEEP(0.3);
UPDATE review.lock_probe SET v = v + 1 WHERE id = 1;
SQL
}

deadlock_one() {
  local n="$1" a b
  a="SET SESSION innodb_lock_wait_timeout=20; BEGIN; UPDATE review.lock_probe SET v=v+1 WHERE id=1; SELECT SLEEP(2); UPDATE review.lock_probe SET v=v+1 WHERE id=2; COMMIT;"
  b="SET SESSION innodb_lock_wait_timeout=20; SELECT SLEEP(0.5); BEGIN; UPDATE review.lock_probe SET v=v+1 WHERE id=2; UPDATE review.lock_probe SET v=v+1 WHERE id=1; COMMIT;"
  rootsql "$n" -e "$a" > /dev/null 2>&1 &
  local pa=$!
  rootsql "$n" -e "$b" > /dev/null 2>&1 &
  local pb=$!
  wait "$pa" || true; wait "$pb" || true
}

workload_one() {
  local n="$1" i
  [ "$(f "$n" 4)" = primary ] || die "$n is a replica; run the workload on its primary"
  log "$n workload..."
  for i in 1 2 3 4 5 6; do workload_sql; done | rootsql "$n" --force > /dev/null 2>&1 || true
  deadlock_one "$n"
  local t found=0 st
  for t in 1 2 3 4 5; do
    st="$(rootsql "$n" -E -e "SHOW ENGINE INNODB STATUS" 2> /dev/null || true)"
    case "$st" in *"LATEST DETECTED DEADLOCK"*) found=1; break ;; esac
    sleep 1
  done
  if [ "$found" = 1 ]; then
    log "$n deadlock recorded"
  else
    log "WARN $n: no deadlock section after the attempt"
  fi
}

replica_status() {   # prints key: value lines
  local n="$1"
  if [ "$(f "$n" 2)" = mysql ]; then rootsql "$n" -E -e "SHOW REPLICA STATUS" 2> /dev/null
  else rootsql "$n" -E -e "SHOW ALL SLAVES STATUS" 2> /dev/null; fi
}
rs() { replica_status "$1" | awk -F': ' -v k="$2" '{gsub(/^ +/, "", $1)} $1==k {print $2; exit}'; }

setup_replication() {
  local n="$1" src cn_src i
  src="$(f "$n" 7)"; cn_src="$(cname "$src")"
  for i in $(seq 1 300); do
    [ "$(rq "$src" "SELECT COUNT(*) FROM mysql.user WHERE user='repl'" || echo 0)" = 1 ] && break
    sleep 2
  done
  if [ -n "$(replica_status "$n")" ]; then log "$n replication already configured"; return 0; fi
  if [ "$(f "$n" 2)" = mysql ]; then
    rootsql "$n" > /dev/null <<SQL
CHANGE REPLICATION SOURCE TO SOURCE_HOST='$cn_src', SOURCE_PORT=3306, SOURCE_USER='repl',
  SOURCE_PASSWORD='$REPL_PASSWORD', SOURCE_AUTO_POSITION=1, GET_SOURCE_PUBLIC_KEY=1;
START REPLICA;
SQL
  else
    rootsql "$n" > /dev/null <<SQL
CHANGE MASTER TO MASTER_HOST='$cn_src', MASTER_PORT=3306, MASTER_USER='repl',
  MASTER_PASSWORD='$REPL_PASSWORD', MASTER_USE_GTID=slave_pos;
START SLAVE;
SQL
  fi
  # Wait for the primary's seed to arrive through replication.
  for i in $(seq 1 300); do
    is_seeded "$n" && break
    sleep 2
  done
  is_seeded "$n" || log "WARN $n: seed did not arrive within 10 minutes"
}

induce_error() {   # MariaDB replica: a local row, then the same key from the primary
  local n="$1" src
  src="$(f "$n" 7)"
  [ -f "$STATE/$n.induced" ] && { log "$n duplicate-key error already induced"; return 0; }
  rootsql "$n" -e "SET sql_log_bin=0; INSERT INTO review.repl_probe VALUES (1, 'replica only')" > /dev/null
  rootsql "$src" -e "INSERT INTO review.repl_probe VALUES (1, 'from primary')" > /dev/null
  local i err=""
  for i in $(seq 1 30); do
    err="$(rs "$n" Last_SQL_Errno)"
    [ -n "$err" ] && [ "$err" != 0 ] && break
    sleep 1
  done
  log "$n induced SQL thread error: ${err:-none} $(rs "$n" Last_SQL_Error)"
  echo "$err" > "$STATE/$n.induced"
}

up_one() {
  local n="$1" image role
  image="$(f "$n" 1)"; role="$(f "$n" 4)"
  rm -f "$STATE/$n.fail"
  if ! is_up "$n" && ! docker ps -a --format '{{.Names}}' | grep -Fxq "$(cname "$n")"; then
    docker image inspect "$image" > /dev/null 2>&1 || docker pull -q "$image" > /dev/null || { echo "pull failed for $image" > "$STATE/$n.fail"; return 1; }
  fi
  start_container "$n" || { echo "docker run failed" > "$STATE/$n.fail"; return 1; }
  wait_ready "$n" || { echo "did not become ready (see logs/$n.log)" > "$STATE/$n.fail"; docker logs --tail 40 "$(cname "$n")" > "$LOGS/$n.docker.log" 2>&1 || true; return 1; }
  configure_perf "$n" || log "WARN $n: performance_schema configuration failed"
  if [ "$role" = primary ]; then
    create_accounts "$n" || { echo "account creation failed" > "$STATE/$n.fail"; return 1; }
    seed_one "$n" || { echo "seed failed" > "$STATE/$n.fail"; return 1; }
    if [ ! -f "$STATE/$n.workload" ]; then workload_one "$n" && touch "$STATE/$n.workload"; fi
  else
    setup_replication "$n"
    [ "$(f "$n" 2)" = mariadb ] && induce_error "$n"
  fi
  touch "$STATE/$n.ready"
}

selected() { if [ "$#" -gt 0 ]; then for x in "$@"; do valid "$x"; echo "$x"; done; else for x in $ALL; do echo "$x"; done; fi; }

cmd_up() {
  command -v docker > /dev/null || die "docker is required"
  mkdir -p "$STATE" "$LOGS"; load_creds
  local names; names="$(selected "$@")"
  local n image
  # Pull every distinct image in parallel first.
  for n in $names; do rm -f "$STATE/$n.ready"; done
  for image in $(for n in $names; do f "$n" 1; done | sort -u); do
    ( docker image inspect "$image" > /dev/null 2>&1 || { log "pulling $image"; docker pull -q "$image" > /dev/null 2>"$LOGS/pull-${image//[:\/]/_}.log" || log "pull failed: $image"; } ) &
  done
  wait
  local pids=""
  for n in $names; do
    ( up_one "$n" ) > "$LOGS/$n.log" 2>&1 &
    pids="$pids $!:$n"
  done
  local p rc failed=0
  for p in $pids; do
    rc=0; wait "${p%%:*}" || rc=$?
    if [ "$rc" -ne 0 ] || [ -f "$STATE/${p##*:}.fail" ]; then failed=$((failed + 1)); log "FAILED ${p##*:}: $(cat "$STATE/${p##*:}.fail" 2> /dev/null || echo "exit $rc")"; fi
  done
  cmd_status
  [ "$failed" -eq 0 ] || { log "$failed server(s) failed; the rest are usable."; return 1; }
}

cmd_down() {
  local n names; names="$(selected "$@")"
  for n in $names; do
    docker rm -fv "$(cname "$n")" > /dev/null 2>&1 && log "removed $(cname "$n")" || true
    rm -f "$STATE/$n".*
  done
  if [ "$#" -eq 0 ]; then docker network rm "$NET" > /dev/null 2>&1 || true; fi
}

cmd_status() {
  local n image engine port role sid slow source v
  printf '%-22s %-22s %-8s %-6s %-9s %s\n' NAME IMAGE STATE PORT ROLE VERSION
  load_creds
  for n in $ALL; do
    read -r image engine port role sid slow source <<< "$(spec "$n")"
    if is_up "$n"; then v="$(rq "$n" "SELECT VERSION()" || true)"; printf '%-22s %-22s %-8s %-6s %-9s %s\n' "$n" "$image" running "$port" "$role" "${v:-?}"
    else printf '%-22s %-22s %-8s %-6s %-9s %s\n' "$n" "$image" down "$port" "$role" "-"; fi
  done
}

scenario_check() {
  valid "$1"; load_creds; is_up "$1" || die "$1 is not running"
  [ "$(f "$1" 4)" = primary ] || die "$1 is a replica; run scenarios on its primary"
  case "${2:-}" in ''|*[!0-9]*) die "SECONDS must be a whole number" ;; esac
}
# bg_session NAME SECONDS-before-start SQL-first SECONDS-hold SQL-last: runs inside the container, detached.
bg_session() {
  local n="$1" delay="$2" first="$3" hold="$4" last="$5"
  ( export MYSQL_PWD="$ROOT_PASSWORD"
    docker exec -d -e MYSQL_PWD "$(cname "$n")" sh -c \
      'sleep "$1"; (printf "%s\n" "$2"; sleep "$3"; printf "%s\n" "$4") | '"$(client "$n")"' -uroot' _ "$delay" "$first" "$hold" "$last" )
}
cmd_locks() {
  scenario_check "$1" "${2:-}"; local s="$2"
  bg_session "$1" 0 "BEGIN; UPDATE review.lock_probe SET v=v+1 WHERE id=1;" "$s" "COMMIT;"
  bg_session "$1" 1 "SET SESSION innodb_lock_wait_timeout=$((s + 10)); UPDATE review.lock_probe SET v=v+1 WHERE id=1;" 1 "SELECT 1;"
  log "$1: row lock held for ${s}s, one session waiting (both clean up)."
}
cmd_idle_trx() {
  scenario_check "$1" "${2:-}"; local s="$2"
  bg_session "$1" 0 "BEGIN; UPDATE review.lock_probe SET v=v+1 WHERE id=2;" "$s" "COMMIT;"
  log "$1: transaction open and idle for ${s}s."
}
cmd_mdl() {
  scenario_check "$1" "${2:-}"; local s="$2"
  bg_session "$1" 0 "BEGIN; SELECT COUNT(*) FROM review.mdl_probe;" "$s" "COMMIT;"
  bg_session "$1" 1 "SET SESSION lock_wait_timeout=$((s + 10)); ALTER TABLE review.mdl_probe ADD COLUMN probe_col INT; ALTER TABLE review.mdl_probe DROP COLUMN probe_col;" 1 "SELECT 1;"
  log "$1: metadata lock held for ${s}s, ALTER waiting (it drops its column again)."
}

profile_names() {   # name -> "profile user type readonly password-var"
  local n="$1"
  echo "ga-$n mf_admin development 0 MF_ADMIN_PASSWORD"
  [ "$n" = mysql-8.4 ] && echo "ga-mysql-8.4-prod mf_admin production 0 MF_ADMIN_PASSWORD"
  [ "$n" = mariadb-11.8 ] && echo "ga-mariadb-11.8-readonly mf_read production 1 MF_READ_PASSWORD"
  return 0
}

mf() { ( cd "$REPO" && HOME="$MYFLAMES_HOME" MYFLAMES_SECRET_BACKEND=vault python3 -m myflames "$@" ); }

# physical_path PATH: PATH with symlinks, "." and ".." resolved by the kernel
# (cd -P, pwd -P), also when it does not exist yet: the physical path of its
# nearest existing directory plus the missing names. Fails for a missing "."
# or ".." name or a symlink that does not lead to a directory.
physical_path() {
  local path="$1" rest="" name
  case "$path" in /*) ;; *) path="$PWD/$path" ;; esac
  while [ ! -d "$path" ]; do
    [ -L "$path" ] && return 1
    name="${path##*/}"
    case "$name" in .|..) return 1 ;; esac
    [ -n "$name" ] && rest="/$name$rest"
    path="${path%/*}"
    [ -n "$path" ] || path=/
  done
  path="$(cd -P "$path" && pwd -P)" || return 1
  echo "${path%/}$rest"
}

# under DIR PATH: PATH is DIR or inside it (both physical).
under() { case "${2%/}/" in "${1%/}/"*) return 0 ;; esac; return 1; }

# Refuse a MYFLAMES_HOME that is the real home (or contains it), or whose
# store ($MYFLAMES_HOME/.myflames, what myflames writes) or itself is the real
# ~/.myflames or inside it, by physical path ("$HOME/.", symlinks and ".."
# included), before anything is created or its mode changed. MYFLAMES_HOME
# becomes that physical path, so a relative value means the same directory
# after the cd "$REPO" below.
check_myflames_home() {
  local target home store lab
  target="$(physical_path "$MYFLAMES_HOME")" || die "MYFLAMES_HOME must be a directory path: $MYFLAMES_HOME"
  home="$(cd -P "$HOME" && pwd -P)" || die "cannot resolve HOME"
  under "$target" "$home" && die "MYFLAMES_HOME must not be your real home"
  store="$(physical_path "$HOME/.myflames")" || die "cannot resolve $HOME/.myflames"
  under "$store" "$target" && die "MYFLAMES_HOME must not be inside your real ~/.myflames"
  lab="$(physical_path "$target/.myflames")" || die "MYFLAMES_HOME/.myflames must be a directory path"
  under "$store" "$lab" && die "MYFLAMES_HOME/.myflames must not be your real ~/.myflames"
  MYFLAMES_HOME="$target"
}

cmd_profiles() {
  check_myflames_home
  load_creds; mkdir -p "$MYFLAMES_HOME" "$STATE"; chmod 700 "$MYFLAMES_HOME"
  local n p user type ro pwvar port
  for n in $ALL; do
    port="$(f "$n" 3)"
    profile_names "$n" | while read -r p user type ro pwvar; do
      ( export PW="${!pwvar}"; cd "$REPO" && HOME="$MYFLAMES_HOME" MYFLAMES_SECRET_BACKEND=vault PYTHONPATH="$REPO" \
        python3 - "$p" "$port" "$user" "$type" "$ro" <<'PY'
import os, sys
from myflames.workspace_store import WorkspaceStore
name, port, user, ctype, ro = sys.argv[1:6]
store = WorkspaceStore()
existing = [p for p in store.list('profile') if p['name'] == name]
# ssl_mode is saved explicitly: the workspace sends PREFERRED, and a saved
# password is only released to the exact TLS settings it was saved with (an
# empty mode means the client default and does not match). Without it the
# picker shows Production and every request is refused.
store.save('profile', {'name': name, 'host': '127.0.0.1', 'port': int(port), 'user': user,
                       'ssl_mode': 'PREFERRED', 'ssl_ca': '',
                       'type': ctype, 'read_only': ro == '1'},
           key=existing[0]['id'] if existing else None, password=os.environ['PW'], remember=True)
PY
      ) || log "profile $p failed"
    done
  done
  # Smoke: a read-only command per profile, in parallel.
  mkdir -p "$STATE/smoke"
  rm -f "$STATE"/smoke/*.result
  for n in $ALL; do
    is_up "$n" || continue
    # The pipe runs the loop in a subshell: it waits for its own checks, and
    # the subshell itself is a job of this shell, so the wait below covers all.
    profile_names "$n" | {
      while read -r p _; do
        ( mf health --profile "$p" --interval 1 --json -q -o "$STATE/smoke/$p.json" > "$STATE/smoke/$p.out" 2>&1 \
            && echo pass > "$STATE/smoke/$p.result" || echo fail > "$STATE/smoke/$p.result" ) &
      done
      wait
    } &
  done
  wait
  for f in "$STATE"/smoke/*.result; do [ -e "$f" ] && log "smoke $(basename "$f" .result): $(cat "$f")"; done
}

cmd_env() {
  check_myflames_home
  load_creds
  local n image engine port role sid slow source v rows=""
  for n in $ALL; do
    read -r image engine port role sid slow source <<< "$(spec "$n")"
    v=""; is_up "$n" && v="$(rq "$n" "SELECT VERSION()" || true)"
    local profs; profs="$(profile_names "$n" | awk '{print $1}' | paste -sd, -)"
    local ierr=""; [ -f "$STATE/$n.induced" ] && ierr="$(cat "$STATE/$n.induced")"
    rows="$rows$n	$(cname "$n")	$image	$engine	$v	$port	$role	$slow	$source	$profs	$ierr	$(flavor "$n")
"
  done
  ROWS="$rows" HOMEDIR="$MYFLAMES_HOME" CREDS="$CREDS_FILE" STATEDIR="$STATE" python3 - <<'PY'
import json, os
out = {"env": {"HOME": os.environ["HOMEDIR"], "MYFLAMES_SECRET_BACKEND": "vault"},
       "credentials_env": os.environ["CREDS"], "servers": []}
for line in os.environ["ROWS"].splitlines():
    n, c, image, engine, v, port, role, slow, source, profs, ierr, flavor = (line.split("\t") + [""] * 12)[:12]
    smoke = {}
    for p in profs.split(","):
        r = os.path.join(os.environ["STATEDIR"], "smoke", p + ".result")
        res = open(r).read().strip() if os.path.exists(r) else "not run"
        j = os.path.join(os.environ["STATEDIR"], "smoke", p + ".json")
        try:
            res += " (health reports " + json.load(open(j))["server_version"] + ")"
        except Exception:
            pass
        smoke[p] = res
    s = {"name": n, "container": c, "image": image, "engine": engine, "flavor": flavor or engine, "version": v or None,
         "host": "127.0.0.1", "port": int(port), "role": role, "slow_log": slow,
         "profiles": profs.split(","), "smoke": smoke, "up": bool(v)}
    if source != "-":
        s["source"] = source
    if ierr:
        s["induced_sql_error_code"] = ierr
    out["servers"].append(s)
print(json.dumps(out, indent=2))
PY
}

main() {
  local cmd="${1:-}"; [ "$#" -gt 0 ] && shift
  case "$cmd" in
    up) cmd_up "$@" ;;
    down) cmd_down "$@" ;;
    status) cmd_status ;;
    seed) [ "$#" -eq 1 ] || die "usage: seed NAME"; valid "$1"; load_creds; seed_one "$1" ;;
    workload) [ "$#" -eq 1 ] || die "usage: workload NAME"; valid "$1"; load_creds; workload_one "$1" ;;
    locks) [ "$#" -eq 2 ] || die "usage: locks NAME SECONDS"; cmd_locks "$@" ;;
    idle-trx) [ "$#" -eq 2 ] || die "usage: idle-trx NAME SECONDS"; cmd_idle_trx "$@" ;;
    mdl) [ "$#" -eq 2 ] || die "usage: mdl NAME SECONDS"; cmd_mdl "$@" ;;
    profiles) cmd_profiles ;;
    env) cmd_env ;;
    *) sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; [ -z "$cmd" ] || exit 2 ;;
  esac
}
main "$@"
