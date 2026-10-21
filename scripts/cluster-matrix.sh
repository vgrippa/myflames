#!/usr/bin/env bash
# scripts/cluster-matrix.sh
#
# Bring up three 3-node clusters in Docker for building and testing cluster
# views: MariaDB Galera, Percona XtraDB Cluster and MySQL InnoDB Cluster (with
# MySQL Router), plus a ProxySQL in front of Galera and InnoDB Cluster, seed
# accounts and a small testdb, and create saved myflames connections to every
# node in an ISOLATED home (never the real ~/.myflames). For development only:
# the servers are throwaway and mf_admin has ALL PRIVILEGES.
#
# Usage:
#   scripts/cluster-matrix.sh up [galera|pxc|ic|proxysql|all]    start, form, seed, wait healthy (idempotent)
#   scripts/cluster-matrix.sh down [galera|pxc|ic|proxysql|all]  remove containers, data volumes and networks
#   scripts/cluster-matrix.sh status                     one line per node with its cluster state
#   scripts/cluster-matrix.sh check [CLUSTER...]         health gate per cluster (exit 1 when unhealthy)
#   scripts/cluster-matrix.sh profiles [--home DIR]      saved connections with passwords in DIR
#   scripts/cluster-matrix.sh env                        print the matrix as JSON
#   scripts/cluster-matrix.sh fail NAME                  docker stop one node (galera-2, ic-1, proxysql, ...)
#   scripts/cluster-matrix.sh recover NAME               start the node again and rejoin it
#   scripts/cluster-matrix.sh fixtures STATE [CLUSTER...] [--out DIR]
#                                                        record cluster outputs as JSON fixtures
#
# Clusters (everything binds to 127.0.0.1; containers, networks and data
# volumes are named after the node, e.g. mf-galera-1 and mf-galera-1-data):
#   galera  mariadb:11.8 (Galera 4, rsync SST)  mf-galera-1..3  ports 34101-34103
#           network mf-galera-net, wsrep_cluster_name mf_galera, nodes galera-1..3
#   pxc     percona/percona-xtradb-cluster:8.4  mf-pxc-1..3     ports 34201-34203
#           network mf-pxc-net, cluster mf_pxc, nodes pxc-1..3; cluster traffic
#           is encrypted with one CA and server certificate shared by all nodes
#           (volume mf-pxc-certs, config volume mf-pxc-conf)
#   ic      mysql:8.4 InnoDB Cluster            mf-ic-1..3      ports 34301-34303
#           network mf-ic-net, cluster mfcluster made with the mysqlsh inside the
#           image (configureInstance, createCluster, addInstance with clone),
#           report_host = container name; MySQL Router mf-ic-router on 34306
#           (read/write) and 34307 (read only). Docker Hub has no
#           mysql/mysql-router:8.4 tag (and Oracle's community-router:8.4 is
#           amd64 only), so when that image is missing for this architecture
#           the router image is built locally (mf-mysql-router:8.4) from
#           mysql:8.4 with mysql-router-community from the mysql-tools
#           repository the image already configures; without either, the
#           router is skipped with a warning.
#   proxysql  proxysql/proxysql:3.0.11 (ProxySQL 3.0.11, arm64 and amd64)  mf-proxysql
#           Admin interface 36032, MySQL interface 36033; attached to
#           mf-galera-net and mf-ic-net, so it starts after galera and ic (up all
#           does that; up proxysql needs both taking writes). Admin users: the
#           default admin, which ProxySQL only lets in from inside the container,
#           and radmin for remote logins. The monitor logs in as mf_monitor,
#           created once on each cluster (Galera: no privilege, since @@wsrep_*
#           and INFORMATION_SCHEMA.GLOBAL_STATUS need none on MariaDB; InnoDB
#           Cluster: REPLICATION CLIENT and SELECT on
#           performance_schema.replication_group_members and
#           replication_group_member_stats). Hostgroups: mysql_galera_hostgroups
#           writer 10, backup writer 11, reader 12, offline 19 (one writer,
#           backup writers also read); mysql_group_replication_hostgroups writer
#           30, backup writer 31, reader 32, offline 39 (the primary writes, the
#           secondaries read; use_ssl=1, since caching_sha2_password needs TLS
#           for a first login). Frontend user mf_admin, default hostgroup 30.
#           Query rules: 10 sends statements tagged /* galera */ to 10, 20 sends
#           SELECT ... FOR UPDATE to 30, 30 sends other SELECTs to 32; the rest
#           goes to 30. up writes the servers, hostgroups and rules once (on a
#           new data volume) and refreshes the passwords every time, so changes
#           made through Admin survive; down proxysql starts over. Each up then
#           sends a few reads through the MySQL interface, so
#           stats_mysql_connection_pool and stats_mysql_query_digest have rows.
#
# Galera and PXC nodes are recreated on their data volume whenever they start,
# so the bootstrap flag is never left on a node that rejoins (a restarted node
# always joins through IST or SST), and every node is given the full
# wsrep_cluster_address. When no node is running, up bootstraps from the node
# grastate.dat marks safe_to_bootstrap, else the highest seqno (after a crash,
# when grastate.dat says -1, the seqno InnoDB recovery reports with
# --wsrep-recover); it refuses when no position is known. InnoDB Cluster nodes
# restart with Docker (needed by clone) and rejoin on start; after a complete
# outage up runs dba.rebootClusterFromCompleteOutage.
#
# Accounts on every node (created once, replicated by the cluster): root, mf_admin
# (ALL WITH GRANT OPTION), mf_read (SELECT, PROCESS, REPLICATION CLIENT, SHOW VIEW
# on *.*; SELECT on *.* covers performance_schema.replication_group_* and the
# mysql_innodb_cluster_metadata schema). Passwords are random, in
# CREDS_FILE (chmod 600), and reach servers through the container environment
# or stdin, never a command line.
#
# ProxySQL's passwords (PROXYSQL_ADMIN_PASSWORD for radmin,
# PROXYSQL_MONITOR_PASSWORD for mf_monitor) are added to the same file by the
# first up proxysql.
#
# Saved connections (profiles): cluster-galera-1..3, cluster-pxc-1..3,
# cluster-ic-1..3 and cluster-ic-router-rw, user mf_admin, type development,
# ssl_mode PREFERRED, password remembered (vault backend), and, while
# mf-proxysql runs, proxysql-admin (127.0.0.1:36032, user radmin, same
# settings). profiles refuses a home that is, contains, or stores into your real
# home or ~/.myflames, then smokes each one with
# `myflames health --profile NAME --interval 1` (proxysql-admin: the read the
# workspace's ProxySQL place makes, /api/live/proxysql, since ProxySQL Admin
# answers neither the server views nor `myflames sql`).
# Use them with:
#   HOME=DIR MYFLAMES_SECRET_BACKEND=vault python3 -m myflames health --profile cluster-ic-1
#
# fixtures STATE writes one JSON file per node (name: CLUSTER-STATE-NODE[-ROLE].json)
# with the raw outputs a cluster view needs: identity (@@version, @@version_comment),
# wsrep status, variables, tables and plugins (Galera, PXC), or the
# replication_group_* tables, group_replication variables and status and the
# mysql_innodb_cluster_metadata views (InnoDB Cluster, plus the mysqlsh status
# and the router routing), with secret-like values redacted. STATE is a word
# such as healthy, degraded or recovered. Default DIR: test/fixtures/cluster.
#
# Settings (environment):
#   CLUSTER_LAB     runtime state, logs, credentials (default local/cluster-matrix, gitignored)
#   CREDS_FILE      default $CLUSTER_LAB/credentials.env
#   MYFLAMES_HOME   isolated HOME for profiles (default $CLUSTER_LAB/home; --home wins)
#   MYFLAMES_SRC    myflames checkout used for profiles and the smoke (default this checkout)
#
# Requirements: Docker, openssl, python3. Memory: about 6 GB for all three
# clusters (ProxySQL adds about 100 MB).

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LAB="${CLUSTER_LAB:-$REPO/local/cluster-matrix}"
CREDS_FILE="${CREDS_FILE:-$LAB/credentials.env}"
MYFLAMES_HOME="${MYFLAMES_HOME:-$LAB/home}"
MYFLAMES_SRC="${MYFLAMES_SRC:-$REPO}"
STATE="$LAB/state"
LOGS="$LAB/logs"
GALERA_IMAGE="mariadb:11.8"
PXC_IMAGE="percona/percona-xtradb-cluster:8.4"
IC_IMAGE="mysql:8.4"
ROUTER_OFFICIAL="mysql/mysql-router:8.4"
ROUTER_LOCAL="mf-mysql-router:8.4"
PROXYSQL_IMAGE="proxysql/proxysql:3.0.11"
PROXYSQL_ADMIN_USER="radmin"
PROXYSQL_MONITOR_USER="mf_monitor"
CLUSTERS="galera pxc ic"
ALL_GROUPS="galera pxc ic proxysql"
ALL_NODES="galera-1 galera-2 galera-3 pxc-1 pxc-2 pxc-3 ic-1 ic-2 ic-3 ic-router proxysql"

log() { echo "[cluster-matrix] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# --- names ---------------------------------------------------------------
cluster_of() { case "$1" in galera-[123]) echo galera ;; pxc-[123]) echo pxc ;; ic-[123]|ic-router) echo ic ;; proxysql) echo proxysql ;; *) return 1 ;; esac; }
valid_node() { cluster_of "$1" > /dev/null 2>&1 || die "unknown node '$1' (known: $ALL_NODES)"; }
valid_cluster() { case "$1" in galera|pxc|ic|proxysql) ;; *) die "unknown cluster '$1' (known: $ALL_GROUPS all)" ;; esac; }
cname() { echo "mf-$1"; }
volume() { echo "mf-$1-data"; }
network() { echo "mf-$1-net"; }
nodes() { echo "$1-1 $1-2 $1-3"; }
idx() { echo "${1##*-}"; }
port() {
  case "$1" in
    galera-*) echo $((34100 + $(idx "$1"))) ;;
    pxc-*) echo $((34200 + $(idx "$1"))) ;;
    ic-router) echo 34306 ;;
    ic-*) echo $((34300 + $(idx "$1"))) ;;
    proxysql) echo 36032 ;;   # Admin; the MySQL interface is 36033
  esac
}
image_of() { case "$(cluster_of "$1")" in galera) echo "$GALERA_IMAGE" ;; pxc) echo "$PXC_IMAGE" ;; ic) echo "$IC_IMAGE" ;; proxysql) echo "$PROXYSQL_IMAGE" ;; esac; }
group_image() { case "$1" in proxysql) echo "$PROXYSQL_IMAGE" ;; *) image_of "$1-1" ;; esac; }
client() { [ "$(cluster_of "$1")" = galera ] && echo mariadb || echo mysql; }
is_up() { docker ps --format '{{.Names}}' | grep -Fxq "$(cname "$1")"; }
exists() { docker ps -a --format '{{.Names}}' | grep -Fxq "$(cname "$1")"; }
# IPv4-only networks: Docker here may enable IPv6 on new networks, names then
# resolve to an IPv6 address first, and Galera listens on IPv4 only
# (gmcast "Connection refused" while joining).
ensure_network() {
  docker network inspect "$(network "$1")" > /dev/null 2>&1 || docker network create --ipv6=false "$(network "$1")" > /dev/null
}
has_volume() { docker volume inspect "$(volume "$1")" > /dev/null 2>&1; }
selected_clusters() {
  if [ "$#" -eq 0 ] || [ "$1" = all ]; then echo "$ALL_GROUPS"; return 0; fi
  local c; for c in "$@"; do valid_cluster "$c"; done; echo "$*"
}

# --- credentials and SQL ---------------------------------------------------
load_creds() {
  [ -f "$CREDS_FILE" ] || create_creds
  # shellcheck disable=SC1090
  . "$CREDS_FILE"
}
create_creds() {
  mkdir -p "$(dirname "$CREDS_FILE")"
  chmod 700 "$LAB"
  ( umask 077
    printf 'ROOT_PASSWORD=%s\nMF_ADMIN_PASSWORD=%s\nMF_READ_PASSWORD=%s\n' \
      "$(openssl rand -hex 12)" "$(openssl rand -hex 12)" "$(openssl rand -hex 12)" > "$CREDS_FILE" )
  chmod 600 "$CREDS_FILE"
}

# sqlx NAME USER PASSWORD [client args]: SQL on stdin; the password travels in
# the container environment, never on a command line.
sqlx() {
  local n="$1" user="$2" pw="$3"; shift 3
  ( export MYSQL_PWD="$pw"; docker exec -i -e MYSQL_PWD "$(cname "$n")" "$(client "$n")" -u"$user" "$@" )
}
rootsql() { sqlx "$1" root "$ROOT_PASSWORD" "${@:2}"; }
# rq NAME SQL: one query, tab-separated rows. stdin is /dev/null: docker exec -i
# would otherwise consume the input of a surrounding `while read` loop.
rq() { local n="$1"; shift; rootsql "$n" --batch --skip-column-names -e "$*" < /dev/null 2> /dev/null; }
gvar() { rq "$1" "SHOW GLOBAL STATUS LIKE '$2'" | cut -f2; }

wait_ready() {   # wait_ready NAME SECONDS
  local n="$1" limit="${2:-240}" i
  for i in $(seq 1 "$limit"); do
    if is_up "$n" && ( export MYSQL_PWD="$ROOT_PASSWORD"; docker exec -e MYSQL_PWD "$(cname "$n")" "$(client "$n")" \
        -uroot -h127.0.0.1 --protocol=TCP -e "SELECT 1" > /dev/null 2>&1 ); then
      log "$n accepts connections (${i}s)"; return 0
    fi
    # A container with no restart policy that exited will not come back.
    if ! is_up "$n" && [ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$(cname "$n")" 2> /dev/null)" = no ]; then
      log "$n exited:"; docker logs --tail 30 "$(cname "$n")" >&2 2>&1 || true; return 1
    fi
    sleep 1
  done
  log "$n did not accept connections within ${limit}s:"; docker logs --tail 30 "$(cname "$n")" >&2 2>&1 || true
  return 1
}

# --- Galera and PXC --------------------------------------------------------
wsrep_line() {   # size status state for one node, "-" when unreadable
  local n="$1" out
  out="$(rq "$n" "SELECT MAX(IF(VARIABLE_NAME='wsrep_cluster_size',VARIABLE_VALUE,NULL)), MAX(IF(VARIABLE_NAME='wsrep_cluster_status',VARIABLE_VALUE,NULL)), MAX(IF(VARIABLE_NAME='wsrep_local_state_comment',VARIABLE_VALUE,NULL)) FROM $( [ "$(cluster_of "$n")" = galera ] && echo information_schema.GLOBAL_STATUS || echo performance_schema.global_status) WHERE VARIABLE_NAME IN ('wsrep_cluster_size','wsrep_cluster_status','wsrep_local_state_comment')" || true)"
  [ -n "$out" ] && echo "$out" | tr '\t' ' ' || echo "- - -"
}
wait_synced() {   # wait_synced NAME SECONDS
  local n="$1" limit="${2:-300}" i line
  for i in $(seq 1 "$limit"); do
    if is_up "$n"; then
      line="$(wsrep_line "$n")"
      case "$line" in *" Primary Synced") log "$n synced ($line, ${i}s)"; return 0 ;; esac
    else
      log "$n stopped while joining:"; docker logs --tail 40 "$(cname "$n")" >&2 2>&1 || true; return 1
    fi
    sleep 1
  done
  log "$n not synced within ${limit}s ($(wsrep_line "$n"))"; docker logs --tail 40 "$(cname "$n")" >&2 2>&1 || true
  return 1
}

# grastate VOLUME-NODE: prints "safe seqno" from the node's grastate.dat ("0 -1" when absent).
grastate() {
  local n="$1" g
  g="$(docker run --rm -v "$(volume "$n"):/d" --entrypoint cat "$(image_of "$n")" /d/grastate.dat 2> /dev/null || true)"
  local safe seq
  safe="$(echo "$g" | awk -F': *' '$1=="safe_to_bootstrap"{print $2}')"
  seq="$(echo "$g" | awk -F': *' '$1=="seqno"{print $2}')"
  echo "${safe:-0} ${seq:--1}"
}

# recovered_seqno NODE: the last committed seqno InnoDB recovery reports for the
# node's data volume (mysqld --wsrep-recover in a throwaway container; the node
# must not be running), or -1 when it cannot be told.
recovered_seqno() {
  local n="$1" out
  if [ "$(cluster_of "$n")" = galera ]; then
    # shellcheck disable=SC2046
    out="$(docker run --rm --user mysql -v "$(volume "$n"):/var/lib/mysql" --entrypoint mariadbd "$GALERA_IMAGE" \
      $(galera_args "$n") --wsrep-recover 2>&1 || true)"
  else
    out="$(docker run --rm --user mysql -v "$(volume "$n"):/var/lib/mysql" --entrypoint sh "$PXC_IMAGE" -c \
      'mysqld --wsrep-recover --log-error-verbosity=3 --log-error=/tmp/recover.log --innodb-buffer-pool-size=64M > /dev/null 2>&1; cat /tmp/recover.log' \
      2>&1 || true)"
  fi
  out="$(echo "$out" | sed -n 's/.*Recovered position: *[0-9a-fA-F-]*:\(-\{0,1\}[0-9][0-9]*\).*/\1/p' | tail -1)"
  echo "${out:--1}"
}

# pick_bootstrap CLUSTER: the node to bootstrap a stopped Galera/PXC cluster
# from: the one grastate.dat marks safe_to_bootstrap, else the highest seqno,
# taken from grastate.dat or, after a crash (seqno -1), from InnoDB recovery.
# Refuses when no node with data has a known position.
pick_bootstrap() {
  local c="$1" n best="" bestseq=-1 safe seq any=0
  for n in $(nodes "$c"); do
    has_volume "$n" || continue
    any=1
    read -r safe seq <<< "$(grastate "$n")"
    if [ "$safe" = 1 ]; then echo "$n"; return 0; fi
    if [ "$seq" = -1 ]; then
      seq="$(recovered_seqno "$n")"
      log "$n: grastate seqno -1 (not shut down cleanly); recovered seqno $seq"
    fi
    if [ "$seq" -gt "$bestseq" ]; then best="$n"; bestseq="$seq"; fi
  done
  if [ "$any" = 0 ]; then echo "$c-1"; return 0; fi
  if [ -z "$best" ]; then
    log "$c: no node's last position is known, so the most advanced node cannot be chosen."
    log "$c: start over with '$0 down $c' (the data volumes are removed) or mark one node's grastate.dat safe_to_bootstrap: 1."
    return 1
  fi
  echo "$best"
}

mark_safe_to_bootstrap() {
  local n="$1"
  has_volume "$n" || return 0
  docker run --rm --user mysql -v "$(volume "$n"):/d" --entrypoint sh "$(image_of "$n")" -c \
    '[ ! -f /d/grastate.dat ] || sed -i "s/^safe_to_bootstrap: 0/safe_to_bootstrap: 1/" /d/grastate.dat' > /dev/null
}

pxc_certs() {   # one CA and one server certificate for every PXC node
  if docker volume inspect mf-pxc-certs > /dev/null 2>&1 && \
     docker run --rm -v mf-pxc-certs:/cert --entrypoint test "$PXC_IMAGE" -s /cert/server-cert.pem; then return 0; fi
  log "pxc: creating the shared CA and server certificate"
  docker volume create mf-pxc-certs > /dev/null
  docker volume create mf-pxc-conf > /dev/null
  docker run --rm --user root -v mf-pxc-certs:/cert -v mf-pxc-conf:/conf --entrypoint bash "$PXC_IMAGE" -c '
    set -e; cd /cert
    openssl genrsa -out ca-key.pem 2048 2> /dev/null
    openssl req -new -x509 -nodes -days 3650 -key ca-key.pem -out ca.pem -subj "/CN=mf-pxc-ca"
    for who in server client; do
      openssl req -newkey rsa:2048 -nodes -keyout $who-key.pem -out $who-req.pem -subj "/CN=mf-pxc-$who" 2> /dev/null
      openssl x509 -req -in $who-req.pem -days 3650 -CA ca.pem -CAkey ca-key.pem -set_serial $([ $who = server ] && echo 01 || echo 02) -out $who-cert.pem 2> /dev/null
    done
    rm -f ./*-req.pem
    chown -R mysql:mysql /cert; chmod 644 /cert/*.pem; chmod 600 /cert/*-key.pem
    cat > /conf/custom.cnf <<EOF
[mysqld]
ssl-ca=/cert/ca.pem
ssl-cert=/cert/server-cert.pem
ssl-key=/cert/server-key.pem
pxc-encrypt-cluster-traffic=ON
[client]
ssl-ca=/cert/ca.pem
EOF
    chmod 644 /conf/custom.cnf'
}

# galera_args NAME: the server options of a Galera node (no spaces in any), also
# used for InnoDB recovery of its volume.
galera_args() {
  echo --wsrep-on=ON --wsrep-provider=/usr/lib/galera/libgalera_smm.so \
    --wsrep-cluster-address=gcomm://mf-galera-1,mf-galera-2,mf-galera-3 \
    --wsrep-cluster-name=mf_galera --wsrep-node-name="$1" --wsrep-sst-method=rsync \
    --binlog-format=ROW --default-storage-engine=InnoDB --innodb-autoinc-lock-mode=2 \
    --innodb-buffer-pool-size=128M --performance-schema=ON \
    --log-bin=mysql-bin --log-slave-updates=ON --server-id=4100
}

# run_wsrep_node NAME bootstrap|join: (re)create the container on its data volume.
run_wsrep_node() {
  local n="$1" mode="$2" c cn i others="" o
  c="$(cluster_of "$n")"; cn="$(cname "$n")"; i="$(idx "$n")"
  for o in $(nodes "$c"); do [ "$o" = "$n" ] || others="${others:+$others,}$(cname "$o")"; done
  ensure_network "$c"
  docker rm -f "$cn" > /dev/null 2>&1 || true
  docker volume create "$(volume "$n")" > /dev/null
  log "$n: starting ($mode)"
  if [ "$c" = galera ]; then
    local boot=--wsrep-on=ON
    [ "$mode" = bootstrap ] && boot=--wsrep-new-cluster
    # shellcheck disable=SC2046
    ( export MARIADB_ROOT_PASSWORD="$ROOT_PASSWORD"
      docker run -d --name "$cn" --hostname "$cn" --network "$(network galera)" -p "127.0.0.1:$(port "$n"):3306" \
        -v "$(volume "$n"):/var/lib/mysql" -e MARIADB_ROOT_PASSWORD --label myflames-cluster=galera "$GALERA_IMAGE" \
        $(galera_args "$n") "$boot" > /dev/null )
  else
    # CLUSTER_JOIN (the image's switch) is empty only for the bootstrap node,
    # which makes the entrypoint initialize an empty datadir. Every node gets
    # the full wsrep_cluster_address on the command line, like a real
    # deployment (the image's own bootstrap address is the empty gcomm://), and
    # the bootstrap node --wsrep-new-cluster, as Galera does.
    local join="" boot=""
    if [ "$mode" = join ]; then join="$others"; else boot=--wsrep-new-cluster; fi
    ( export MYSQL_ROOT_PASSWORD="$ROOT_PASSWORD" CLUSTER_JOIN="$join"
      docker run -d --name "$cn" --hostname "$cn" --network "$(network pxc)" -p "127.0.0.1:$(port "$n"):3306" \
        -v "$(volume "$n"):/var/lib/mysql" -v mf-pxc-certs:/cert:ro -v mf-pxc-conf:/etc/percona-xtradb-cluster.conf.d:ro \
        -e MYSQL_ROOT_PASSWORD -e CLUSTER_JOIN -e CLUSTER_NAME=mf_pxc -e PERCONA_TELEMETRY_DISABLE=1 \
        --label myflames-cluster=pxc "$PXC_IMAGE" \
        --wsrep-node-name="$n" --wsrep-cluster-address="gcomm://mf-pxc-1,mf-pxc-2,mf-pxc-3" $boot \
        --innodb-buffer-pool-size=128M --server-id=$((4200 + i)) > /dev/null )
  fi
}

wsrep_up() {
  local c="$1" n running="" boot
  [ "$c" = pxc ] && pxc_certs
  for n in $(nodes "$c"); do is_up "$n" && running="$running $n"; done
  if [ -z "$running" ]; then
    boot="$(pick_bootstrap "$c")" || die "$c: cannot choose a node to bootstrap from"
    log "$c: no node running; bootstrapping from $boot"
    mark_safe_to_bootstrap "$boot"
    run_wsrep_node "$boot" bootstrap
    wait_ready "$boot" 300 && wait_synced "$boot" 120 || die "$boot did not bootstrap"
  fi
  create_accounts "$(primary_of "$c")"
  seed "$(primary_of "$c")"
  for n in $(nodes "$c"); do
    is_up "$n" && continue
    run_wsrep_node "$n" join
    wait_ready "$n" 400 && wait_synced "$n" 400 || die "$n did not join"
  done
}

# --- InnoDB Cluster --------------------------------------------------------
JS_PRELUDE='var pw = os.getenv("MF_PW");
function U(h) { return {scheme: "mysql", user: "root", host: h, port: 3306, password: pw}; }
'
# ish NODE: run the JavaScript on stdin in the mysqlsh of NODE's container.
# The root password and the optional MF_PRIMARY / MF_TARGET host names reach
# the script through the container environment (os.getenv), never argv.
# --no-defaults: the image's option files make mysqlsh connect to
# root@localhost on start.
ish() {
  local n="$1"
  { printf '%s\n' "$JS_PRELUDE"; cat; } | ( export MF_PW="$ROOT_PASSWORD"
    docker exec -i -e MF_PW -e MF_PRIMARY -e MF_TARGET -e LC_ALL=C "$(cname "$n")" mysqlsh --no-defaults --js --no-wizard \
      2> >(grep -v 'Cannot set LC_ALL' >&2) )
}

run_ic_node() {
  local n="$1" cn i
  cn="$(cname "$n")"; i="$(idx "$n")"
  if exists "$n"; then docker start "$cn" > /dev/null; return 0; fi
  ensure_network ic
  docker volume create "$(volume "$n")" > /dev/null
  log "$n: starting"
  ( export MYSQL_ROOT_PASSWORD="$ROOT_PASSWORD"
    docker run -d --name "$cn" --hostname "$cn" --network "$(network ic)" -p "127.0.0.1:$(port "$n"):3306" \
      -v "$(volume "$n"):/var/lib/mysql" -e MYSQL_ROOT_PASSWORD --restart unless-stopped \
      --label myflames-cluster=ic "$IC_IMAGE" \
      --server-id=$((4300 + i)) --report-host="$cn" --gtid-mode=ON --enforce-gtid-consistency=ON \
      --innodb-buffer-pool-size=128M > /dev/null )
}

ic_member_line() {   # STATE ROLE of NODE in the group as NODE sees it
  local n="$1" out
  out="$(rq "$n" "SELECT MEMBER_STATE, MEMBER_ROLE FROM performance_schema.replication_group_members WHERE MEMBER_HOST='$(cname "$n")'" || true)"
  [ -n "$out" ] && echo "$out" | tr '\t' ' ' || echo "- -"
}
ic_primary() {   # container name of the primary, from any running member
  local n p
  for n in $(nodes ic); do
    is_up "$n" || continue
    p="$(rq "$n" "SELECT MEMBER_HOST FROM performance_schema.replication_group_members WHERE MEMBER_ROLE='PRIMARY' AND MEMBER_STATE='ONLINE'" || true)"
    [ -n "$p" ] && { echo "${p#mf-}"; return 0; }
  done
  return 1
}
ic_has_cluster() {   # some running member reaches the cluster with quorum
  local n
  for n in $(nodes ic); do
    is_up "$n" || continue
    [ "$(ic_member_line "$n")" != "- -" ] && ic_primary > /dev/null && return 0
  done
  return 1
}

ic_up() {
  local n p
  for n in $(nodes ic); do run_ic_node "$n"; done
  for n in $(nodes ic); do wait_ready "$n" 300 || die "$n did not start"; done
  if ! ic_has_cluster; then
    if [ "$(rq ic-1 "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='mysql_innodb_cluster_metadata'" || echo 0)" = 1 ]; then
      log "ic: no member online; rebooting the cluster from a complete outage"
      ish ic-1 <<'JS' || die "ic: rebootClusterFromCompleteOutage failed"
shell.connect(U("mf-ic-1"));
dba.rebootClusterFromCompleteOutage("mfcluster");
JS
    else
      log "ic: configuring instances and creating mfcluster"
      ish ic-1 <<'JS' || die "ic: createCluster failed"
["mf-ic-1", "mf-ic-2", "mf-ic-3"].forEach(function (h) { dba.configureInstance(U(h), {restart: false}); });
shell.connect(U("mf-ic-1"));
dba.createCluster("mfcluster");
JS
    fi
  fi
  p="$(ic_primary)" || die "ic: no primary after create/reboot"
  create_accounts "$p"
  seed "$p"
  log "ic: adding or rejoining members (clone recovery)"
  ( export MF_PRIMARY="mf-$p"
    ish "$p" <<'JS' ) || log "WARN ic: addInstance/rejoinInstance reported an error (checked below)"
var c = (shell.connect(U(os.getenv("MF_PRIMARY"))), dba.getCluster());
["mf-ic-1", "mf-ic-2", "mf-ic-3"].forEach(function (h) {
  var topo = c.status().defaultReplicaSet.topology, m = topo[h + ":3306"];
  if (!m) { print("adding " + h + "\n"); c.addInstance(U(h), {recoveryMethod: "clone"}); }
  else if (m.status !== "ONLINE") { print("rejoining " + h + " (" + m.status + ")\n"); c.rejoinInstance(U(h)); }
});
JS
  wait_ic_online 300 || die "ic: members not ONLINE"
  router_up || log "WARN ic: router skipped (see above)"
}

wait_ic_online() {
  local limit="${1:-300}" i n online
  for i in $(seq 1 "$limit"); do
    n="$(ic_primary 2> /dev/null || true)"
    if [ -n "$n" ]; then
      online="$(rq "$n" "SELECT COUNT(*) FROM performance_schema.replication_group_members WHERE MEMBER_STATE='ONLINE'" || echo 0)"
      [ "$online" = 3 ] && { log "ic: 3 members ONLINE, primary $n (${i}s)"; return 0; }
    fi
    sleep 1
  done
  return 1
}

router_image() {   # prints the router image to use, or fails
  local arch img_arch
  arch="$(docker info --format '{{.Architecture}}' 2> /dev/null)"
  case "$arch" in aarch64|arm64) arch=arm64 ;; x86_64|amd64) arch=amd64 ;; esac
  if docker image inspect "$ROUTER_OFFICIAL" > /dev/null 2>&1 || docker pull -q "$ROUTER_OFFICIAL" > /dev/null 2>&1; then
    img_arch="$(docker image inspect "$ROUTER_OFFICIAL" --format '{{.Architecture}}')"
    [ "$img_arch" = "$arch" ] && { echo "$ROUTER_OFFICIAL"; return 0; }
  fi
  if ! docker image inspect "$ROUTER_LOCAL" > /dev/null 2>&1; then
    log "ic: $ROUTER_OFFICIAL has no $arch image; building $ROUTER_LOCAL from $IC_IMAGE"
    printf 'FROM %s\nRUN microdnf -y install mysql-router-community && microdnf clean all\n' "$IC_IMAGE" \
      | docker build -q -t "$ROUTER_LOCAL" - > /dev/null 2> "$LOGS/router-build.log" || { log "router build failed: $(tail -3 "$LOGS/router-build.log")"; return 1; }
  fi
  echo "$ROUTER_LOCAL"
}

router_up() {
  local img p
  if exists ic-router; then
    is_up ic-router || docker start "$(cname ic-router)" > /dev/null
    # A container whose bootstrap failed waits for a configuration forever:
    # bootstrap it again instead of reporting it as running.
    if docker exec "$(cname ic-router)" test -f /router/run/mysqlrouter.conf; then wait_router 60; return $?; fi
    log "ic-router: not bootstrapped; bootstrapping again"
  else
    img="$(router_image)" || return 1
    docker volume create "$(volume ic-router)" > /dev/null
    # The container waits for the bootstrap below; the password reaches
    # mysqlrouter on stdin only.
    docker run -d --name "$(cname ic-router)" --hostname "$(cname ic-router)" --network "$(network ic)" \
      -p 127.0.0.1:34306:6446 -p 127.0.0.1:34307:6447 -v "$(volume ic-router):/router" --restart unless-stopped \
      --label myflames-cluster=ic --entrypoint sh "$img" -c \
      'until [ -f /router/run/mysqlrouter.conf ]; do sleep 1; done; exec mysqlrouter -c /router/run/mysqlrouter.conf' > /dev/null
  fi
  p="$(ic_primary)" || return 1
  log "ic-router: bootstrapping against mf-$p"
  printf '%s\n' "$ROOT_PASSWORD" | docker exec -i "$(cname ic-router)" mysqlrouter --bootstrap "root@mf-$p:3306" \
    --directory /router/run --conf-bind-address=0.0.0.0 --user=mysql --force > "$LOGS/router-bootstrap.log" 2>&1 \
    || { log "router bootstrap failed: $(tail -5 "$LOGS/router-bootstrap.log")"; return 1; }
  wait_router 60
}

router_route() {   # router_route RW|RO: the member hostname a new session reaches
  local p=6446; [ "$1" = RO ] && p=6447
  ( export MYSQL_PWD="$ROOT_PASSWORD"
    docker exec -e MYSQL_PWD "$(cname ic-1)" mysql -uroot -h"$(cname ic-router)" -P"$p" --protocol=TCP \
      --batch --skip-column-names -e "SELECT @@hostname" 2> /dev/null ) || \
  ( export MYSQL_PWD="$ROOT_PASSWORD"
    docker exec -e MYSQL_PWD "$(cname ic-2)" mysql -uroot -h"$(cname ic-router)" -P"$p" --protocol=TCP \
      --batch --skip-column-names -e "SELECT @@hostname" 2> /dev/null )
}
wait_router() {
  local limit="${1:-60}" i rw ro
  for i in $(seq 1 "$limit"); do
    rw="$(router_route RW || true)"; ro="$(router_route RO || true)"
    if [ -n "$rw" ] && [ -n "$ro" ]; then log "ic-router: RW -> $rw, RO -> $ro (${i}s)"; return 0; fi
    sleep 1
  done
  log "ic-router: ports did not answer within ${limit}s"; return 1
}

# --- accounts and data -----------------------------------------------------
primary_of() {   # where to send writes for a cluster
  case "$1" in
    galera|pxc) local n; for n in $(nodes "$1"); do is_up "$n" && { echo "$n"; return 0; }; done; echo "$1-1" ;;
    ic) ic_primary ;;
  esac
}

create_accounts() {
  local n="$1"
  rootsql "$n" > /dev/null <<SQL
CREATE USER IF NOT EXISTS 'mf_admin'@'%' IDENTIFIED BY '$MF_ADMIN_PASSWORD';
GRANT ALL PRIVILEGES ON *.* TO 'mf_admin'@'%' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'mf_read'@'%' IDENTIFIED BY '$MF_READ_PASSWORD';
GRANT SELECT, PROCESS, REPLICATION CLIENT, SHOW VIEW ON *.* TO 'mf_read'@'%';
SQL
}

seed() {
  local n="$1"
  if [ "$(rq "$n" "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='testdb' AND table_name='orders'")" = 1 ]; then
    log "$n: testdb already seeded"; return 0
  fi
  log "$n: seeding testdb"
  # Every table has a primary key (Group Replication and pxc_strict_mode need one).
  rootsql "$n" > /dev/null <<'SQL'
CREATE DATABASE IF NOT EXISTS testdb;
CREATE TABLE testdb.customers (
  id INT PRIMARY KEY, name VARCHAR(40) NOT NULL, country CHAR(2) NOT NULL, created_at DATETIME NOT NULL,
  KEY idx_country (country)) ENGINE=InnoDB;
CREATE TABLE testdb.products (
  id INT PRIMARY KEY, name VARCHAR(40) NOT NULL, price DECIMAL(10,2) NOT NULL) ENGINE=InnoDB;
CREATE TABLE testdb.orders (
  id INT PRIMARY KEY, customer_id INT NOT NULL, product_id INT NOT NULL, quantity INT NOT NULL,
  status VARCHAR(12) NOT NULL, created_at DATETIME NOT NULL,
  KEY idx_customer (customer_id), KEY idx_status_created (status, created_at)) ENGINE=InnoDB;
INSERT INTO testdb.customers
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 200)
SELECT n, CONCAT('Customer ', n), ELT(1 + MOD(n, 5), 'US', 'BR', 'DE', 'JP', 'IN'),
       DATE_ADD('2025-01-01', INTERVAL MOD(n * 7, 365) DAY) FROM c;
INSERT INTO testdb.products
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 50)
SELECT n, CONCAT('Product ', n), ROUND(5 + MOD(n * 37, 500) + 0.99, 2) FROM c;
INSERT INTO testdb.orders
WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 800)
SELECT n, 1 + MOD(n * 13, 200), 1 + MOD(n * 7, 50), 1 + MOD(n, 5),
       ELT(1 + MOD(n, 4), 'new', 'paid', 'shipped', 'returned'),
       DATE_ADD('2025-01-01', INTERVAL MOD(n * 11, 365) DAY) FROM c;
SQL
}

# --- ProxySQL --------------------------------------------------------------
ensure_proxysql_creds() {   # adds the ProxySQL passwords to the credentials file once
  local k added=0
  for k in PROXYSQL_ADMIN_PASSWORD PROXYSQL_MONITOR_PASSWORD; do
    grep -q "^$k=" "$CREDS_FILE" && continue
    ( umask 077; printf '%s=%s\n' "$k" "$(openssl rand -hex 12)" >> "$CREDS_FILE" )
    added=1
  done
  chmod 600 "$CREDS_FILE"
  # shellcheck disable=SC1090
  [ "$added" = 0 ] || . "$CREDS_FILE"
}

# pxsql USER PASSWORD HOST PORT [client args]: SQL on stdin to ProxySQL, with the
# MariaDB client its image ships; the password travels in the container
# environment, never on a command line.
pxsql() {
  local user="$1" pw="$2" host="$3" p="$4"; shift 4
  ( export MYSQL_PWD="$pw"; docker exec -i -e MYSQL_PWD "$(cname proxysql)" mysql --skip-ssl-verify-server-cert \
      -u"$user" -h"$host" -P"$p" --protocol=TCP "$@" )
}
# Admin as ProxySQL's default admin user, which it only lets in over loopback.
pxadmin() { pxsql admin admin 127.0.0.1 6032 "$@"; }
# pxq SQL: one Admin query as the remote admin user, through the container's
# network address (a remote login, as from the host); tab-separated rows.
pxq() {
  pxsql "$PROXYSQL_ADMIN_USER" "${PROXYSQL_ADMIN_PASSWORD:-}" "$(cname proxysql)" 6032 --batch --skip-column-names \
    -e "$*" < /dev/null 2> /dev/null
}
# pxfront: SQL on stdin through the MySQL interface as mf_admin. --comments keeps
# the /* galera */ tag query rule 10 routes on (the client strips comments by default).
pxfront() { pxsql mf_admin "$MF_ADMIN_PASSWORD" 127.0.0.1 6033 --comments --batch --skip-column-names "$@"; }

# px_greeting PORT: the server version in the MySQL protocol greeting that
# 127.0.0.1:PORT sends the host (the published port works), or fails.
px_greeting() {
  python3 - "$1" <<'PY'
import socket, sys
try:
    s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
    s.settimeout(5)
    data = b""
    while len(data) < 4 or len(data) < 4 + int.from_bytes(data[:3], "little"):
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
except OSError:
    sys.exit(1)
if len(data) < 6 or data[4] != 10:   # protocol version 10
    sys.exit(1)
print(data[5:].split(b"\0", 1)[0].decode("ascii", "replace"))
PY
}

wait_proxysql() {   # wait_proxysql SECONDS: Admin answers inside the container
  local limit="${1:-60}" i
  for i in $(seq 1 "$limit"); do
    if is_up proxysql && [ "$(pxadmin --batch --skip-column-names -e "SELECT 1" < /dev/null 2> /dev/null)" = 1 ]; then
      log "proxysql: Admin answers (${i}s)"; return 0
    fi
    sleep 1
  done
  log "proxysql: Admin did not answer within ${limit}s:"; docker logs --tail 30 "$(cname proxysql)" >&2 2>&1 || true
  return 1
}

px_attach() {   # connect mf-proxysql to the Galera and InnoDB Cluster networks it is missing
  local c attached
  attached=" $(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$(cname proxysql)") "
  for c in galera ic; do
    case "$attached" in *" $(network "$c") "*) ;; *) docker network connect "$(network "$c")" "$(cname proxysql)"; log "proxysql: attached to $(network "$c")" ;; esac
  done
}

run_proxysql() {
  local cn; cn="$(cname proxysql)"
  if exists proxysql; then
    is_up proxysql || docker start "$cn" > /dev/null
  else
    docker volume create "$(volume proxysql)" > /dev/null
    log "proxysql: starting $PROXYSQL_IMAGE"
    # --no-version-check: the lab stays offline (ProxySQL asks its website for
    # the latest version on start otherwise).
    docker run -d --name "$cn" --hostname "$cn" --network "$(network galera)" \
      -p 127.0.0.1:36032:6032 -p 127.0.0.1:36033:6033 -v "$(volume proxysql):/var/lib/proxysql" \
      --restart unless-stopped --label myflames-cluster=proxysql "$PROXYSQL_IMAGE" \
      proxysql -f --idle-threads --no-version-check -D /var/lib/proxysql > /dev/null
  fi
  px_attach
}

# create_monitor CLUSTER: the account ProxySQL's monitor logs in with, created
# once on the node that takes writes and replicated by the cluster. Galera
# (MariaDB): the health check reads @@read_only, @@wsrep_* and
# INFORMATION_SCHEMA.GLOBAL_STATUS, which need no privilege. InnoDB Cluster:
# the check reads performance_schema.replication_group_members and
# replication_group_member_stats (SELECT on each); REPLICATION CLIENT for
# replica status.
create_monitor() {
  local c="$1" n
  n="$(primary_of "$c")"
  case "$c" in
    galera) rootsql "$n" > /dev/null <<SQL
CREATE USER IF NOT EXISTS '$PROXYSQL_MONITOR_USER'@'%' IDENTIFIED BY '$PROXYSQL_MONITOR_PASSWORD';
SQL
      ;;
    ic) rootsql "$n" > /dev/null <<SQL
CREATE USER IF NOT EXISTS '$PROXYSQL_MONITOR_USER'@'%' IDENTIFIED BY '$PROXYSQL_MONITOR_PASSWORD';
GRANT REPLICATION CLIENT ON *.* TO '$PROXYSQL_MONITOR_USER'@'%';
GRANT SELECT ON performance_schema.replication_group_members TO '$PROXYSQL_MONITOR_USER'@'%';
GRANT SELECT ON performance_schema.replication_group_member_stats TO '$PROXYSQL_MONITOR_USER'@'%';
SQL
      ;;
  esac
  log "proxysql: monitor account $PROXYSQL_MONITOR_USER on $c (through $n)"
}

# px_configure: the credentials every time (the admin, monitor and mf_admin
# passwords from CREDS_FILE); the servers, hostgroups and query rules only when
# the lab's hostgroups are missing (a new data volume), so later changes made
# through Admin are kept. Galera nodes are recreated whenever they start, so
# their addresses can change: the DNS cache refreshes every 10 s, not 60 s.
px_configure() {
  pxadmin > /dev/null <<SQL
UPDATE global_variables SET variable_value = 'admin:admin;$PROXYSQL_ADMIN_USER:$PROXYSQL_ADMIN_PASSWORD' WHERE variable_name = 'admin-admin_credentials';
LOAD ADMIN VARIABLES TO RUNTIME;
SAVE ADMIN VARIABLES TO DISK;
UPDATE global_variables SET variable_value = '$PROXYSQL_MONITOR_USER' WHERE variable_name = 'mysql-monitor_username';
UPDATE global_variables SET variable_value = '$PROXYSQL_MONITOR_PASSWORD' WHERE variable_name = 'mysql-monitor_password';
UPDATE global_variables SET variable_value = '10000' WHERE variable_name = 'mysql-monitor_local_dns_cache_refresh_interval';
LOAD MYSQL VARIABLES TO RUNTIME;
SAVE MYSQL VARIABLES TO DISK;
REPLACE INTO mysql_users (username, password, default_hostgroup, transaction_persistent, active, comment)
  VALUES ('mf_admin', '$MF_ADMIN_PASSWORD', 30, 1, 1, 'cluster-matrix: same account and password on both clusters');
LOAD MYSQL USERS TO RUNTIME;
SAVE MYSQL USERS TO DISK;
SQL
  local have
  have="$(pxadmin --batch --skip-column-names -e "SELECT (SELECT COUNT(*) FROM mysql_galera_hostgroups WHERE writer_hostgroup = 10) + (SELECT COUNT(*) FROM mysql_group_replication_hostgroups WHERE writer_hostgroup = 30)" < /dev/null)"
  if [ "$have" = 2 ]; then log "proxysql: servers, hostgroups and query rules kept"; return 0; fi
  log "proxysql: writing servers, hostgroups and query rules"
  # Every node starts in its writer hostgroup; the Galera and Group Replication
  # checks then move each to the writer, backup writer, reader or offline
  # hostgroup in runtime. The highest weight makes mf-galera-1 the Galera writer.
  pxadmin > /dev/null <<'SQL'
DELETE FROM mysql_servers WHERE hostgroup_id IN (10, 11, 12, 19, 30, 31, 32, 39);
INSERT INTO mysql_servers (hostgroup_id, hostname, port, weight, use_ssl, comment) VALUES
  (10, 'mf-galera-1', 3306, 1000, 0, 'galera-1'),
  (10, 'mf-galera-2', 3306, 100, 0, 'galera-2'),
  (10, 'mf-galera-3', 3306, 100, 0, 'galera-3'),
  (30, 'mf-ic-1', 3306, 100, 1, 'ic-1'),
  (30, 'mf-ic-2', 3306, 100, 1, 'ic-2'),
  (30, 'mf-ic-3', 3306, 100, 1, 'ic-3');
DELETE FROM mysql_galera_hostgroups WHERE writer_hostgroup = 10;
INSERT INTO mysql_galera_hostgroups (writer_hostgroup, backup_writer_hostgroup, reader_hostgroup, offline_hostgroup,
  active, max_writers, writer_is_also_reader, max_transactions_behind, comment)
  VALUES (10, 11, 12, 19, 1, 1, 2, 100, 'mf_galera: one writer; the backup writers also read');
DELETE FROM mysql_group_replication_hostgroups WHERE writer_hostgroup = 30;
INSERT INTO mysql_group_replication_hostgroups (writer_hostgroup, backup_writer_hostgroup, reader_hostgroup,
  offline_hostgroup, active, max_writers, writer_is_also_reader, max_transactions_behind, comment)
  VALUES (30, 31, 32, 39, 1, 1, 0, 100, 'mfcluster: the primary writes; the secondaries read');
DELETE FROM mysql_query_rules WHERE rule_id IN (10, 20, 30);
INSERT INTO mysql_query_rules (rule_id, active, match_pattern, destination_hostgroup, apply, comment)
  VALUES (10, 1, '^/\* galera \*/', 10, 1, 'statements tagged /* galera */ go to the Galera writer');
INSERT INTO mysql_query_rules (rule_id, active, match_digest, destination_hostgroup, apply, comment)
  VALUES (20, 1, '^SELECT .* FOR UPDATE', 30, 1, 'locking reads go to the InnoDB Cluster primary'),
         (30, 1, '^SELECT', 32, 1, 'other reads go to the InnoDB Cluster secondaries');
LOAD MYSQL SERVERS TO RUNTIME;
SAVE MYSQL SERVERS TO DISK;
LOAD MYSQL QUERY RULES TO RUNTIME;
SAVE MYSQL QUERY RULES TO DISK;
SQL
}

# px_traffic: a few reads through the MySQL interface, one per query rule and
# the default route, so stats_mysql_connection_pool, stats_mysql_query_rules and
# stats_mysql_query_digest have rows. Nothing is written: the locking reads are
# rolled back.
px_traffic() {
  local i sql=""
  for i in $(seq 1 20); do
    sql="${sql}SELECT COUNT(*) FROM testdb.orders WHERE status = 'paid' AND customer_id = $i;
SELECT name, country FROM testdb.customers WHERE id = $i;
"
  done
  for i in $(seq 1 5); do
    sql="${sql}START TRANSACTION;
SELECT id, status FROM testdb.orders WHERE id = $i FOR UPDATE;
ROLLBACK;
/* galera */ SELECT COUNT(*) FROM testdb.orders WHERE product_id = $i;
SHOW GLOBAL STATUS LIKE 'Uptime';
"
  done
  printf '%s' "$sql" | pxfront > /dev/null
  log "proxysql: sent $((20 * 2 + 5 * 5)) statements through the MySQL interface"
}

# px_wait_hostgroups SECONDS: until the checks put one ONLINE writer and at
# least one ONLINE reader in runtime for each cluster.
px_wait_hostgroups() {
  local limit="${1:-60}" i
  for i in $(seq 1 "$limit"); do
    px_hostgroups_ok && { log "proxysql: writers and readers ONLINE (${i}s)"; return 0; }
    sleep 1
  done
  log "proxysql: hostgroups not ready within ${limit}s:"
  pxq "SELECT hostgroup_id, hostname, port, status FROM runtime_mysql_servers ORDER BY hostgroup_id, hostname" >&2 || true
  return 1
}
px_online() {   # px_online HOSTGROUP: ONLINE runtime servers in it, one host name per line
  pxq "SELECT hostname FROM runtime_mysql_servers WHERE hostgroup_id = $1 AND status = 'ONLINE' ORDER BY hostname" || true
}
px_count() { [ -n "$1" ] && echo "$1" | wc -l | tr -d ' ' || echo 0; }
px_hostgroups_ok() {
  [ "$(px_count "$(px_online 10)")" = 1 ] && [ "$(px_count "$(px_online 30)")" = 1 ] &&
    [ "$(px_count "$(px_online 12)")" -ge 1 ] && [ "$(px_count "$(px_online 32)")" -ge 1 ]
}

proxysql_up() {
  local c n
  for c in galera ic; do
    n="$(primary_of "$c" 2> /dev/null || true)"
    if [ -z "$n" ] || ! is_up "$n" || [ "$(rq "$n" "SELECT 1")" != 1 ]; then
      die "proxysql: $c has no running node that takes writes (bring it up first: $0 up galera ic)"
    fi
  done
  ensure_proxysql_creds
  create_monitor galera
  create_monitor ic
  run_proxysql
  wait_proxysql 60 || die "proxysql: Admin did not start"
  px_configure
  px_wait_hostgroups 60 || die "proxysql: hostgroups not ready"
  px_traffic
}

proxysql_down() {
  docker rm -fv "$(cname proxysql)" > /dev/null 2>&1 && log "removed $(cname proxysql)" || true
  docker volume rm "$(volume proxysql)" > /dev/null 2>&1 || true
  rm -f "$STATE/proxysql".*
}

# px_version: ProxySQL's version (admin-version; stats_mysql_global has none).
px_version() { pxq "SELECT variable_value FROM global_variables WHERE variable_name = 'admin-version'"; }
px_summary() {   # one line: ONLINE/total per runtime hostgroup
  local s
  s="$(pxq "SELECT 'HG' || hostgroup_id || ' ' || SUM(status = 'ONLINE') || '/' || COUNT(*) FROM runtime_mysql_servers GROUP BY hostgroup_id ORDER BY hostgroup_id" | tr '\n' ' ')"
  [ -n "$s" ] && echo "${s}ONLINE; MySQL 36033" || echo "Admin does not answer as $PROXYSQL_ADMIN_USER"
}

# --- health ----------------------------------------------------------------
check_cluster() {   # prints one line per node; returns 1 when the cluster is not healthy
  local c="$1" n ok=0 line
  case "$c" in
    galera|pxc)
      for n in $(nodes "$c"); do
        if ! is_up "$n"; then echo "$c $n: down"; ok=1; continue; fi
        line="$(wsrep_line "$n")"
        echo "$c $n: wsrep_cluster_size/status/state = $line"
        [ "$line" = "3 Primary Synced" ] || ok=1
      done ;;
    ic)
      local online=0 primaries=0 st p
      for n in $(nodes ic); do
        if ! is_up "$n"; then echo "ic $n: down"; ok=1; continue; fi
        line="$(ic_member_line "$n")"; echo "ic $n: $line"
        case "$line" in "ONLINE PRIMARY") online=$((online + 1)); primaries=$((primaries + 1)) ;; "ONLINE SECONDARY") online=$((online + 1)) ;; esac
      done
      [ "$online" = 3 ] && [ "$primaries" = 1 ] || ok=1
      p="$(ic_primary 2> /dev/null || true)"
      if [ -n "$p" ]; then
        st="$( ( export MF_PRIMARY="mf-$p"; ish "$p" <<'JS' 2> /dev/null ) | grep -o '"status": *"[A-Z_]*"' | head -1 || true
shell.connect(U(os.getenv("MF_PRIMARY")));
var s = dba.getCluster().status();
print(JSON.stringify({status: s.defaultReplicaSet.status, statusText: s.defaultReplicaSet.statusText}) + "\n");
JS
)"
        echo "ic mysqlsh dba.getCluster().status(): ${st:-unavailable}"
        case "$st" in *'"OK"'*) ;; *) ok=1 ;; esac
      fi
      if is_up ic-router; then
        local rw ro
        rw="$(router_route RW || true)"; ro="$(router_route RO || true)"
        echo "ic ic-router: RW 34306 -> ${rw:-none}, RO 34307 -> ${ro:-none}"
        [ -n "$rw" ] && [ -n "$ro" ] || ok=1
      else
        echo "ic ic-router: down"; ok=1
      fi ;;
    proxysql)
      if ! is_up proxysql; then echo "proxysql: down"; return 1; fi
      local p v rows got want
      for p in 36032 36033; do
        v="$(px_greeting "$p" || true)"
        if [ -n "$v" ]; then echo "proxysql 127.0.0.1:$p: MySQL protocol greeting, server version $v"
        else echo "proxysql 127.0.0.1:$p: no MySQL protocol greeting"; ok=1; fi
      done
      rows="$(pxq "SELECT hostgroup_id, hostname, port, status FROM runtime_mysql_servers ORDER BY hostgroup_id, hostname" || true)"
      if [ -z "$rows" ]; then echo "proxysql Admin as $PROXYSQL_ADMIN_USER: no answer or no runtime servers"; return 1; fi
      echo "$rows" | while read -r hg host p st; do echo "proxysql runtime_mysql_servers HG$hg $host:$p $st"; done
      px_hostgroups_ok || { echo "proxysql: want one ONLINE writer in HG10 and HG30 and an ONLINE reader in HG12 and HG32"; ok=1; }
      # Routing through the MySQL interface: rule 30 (a SELECT reaches an
      # InnoDB Cluster secondary), rule 10 (/* galera */ reaches the Galera
      # writer) and the default hostgroup 30 (a SHOW reaches the primary).
      for want in "32 SELECT @@hostname" "10 /* galera */ SELECT @@hostname" "30 SHOW GLOBAL VARIABLES LIKE 'hostname'"; do
        got="$(printf '%s;\n' "${want#* }" | pxfront 2> /dev/null | awk '{print $NF}' | tail -1 || true)"
        if [ -n "$got" ] && px_online "${want%% *}" | grep -Fxq "$got"; then
          echo "proxysql route \"${want#* }\" -> $got (HG${want%% *})"
        else
          echo "proxysql route \"${want#* }\" -> ${got:-no answer}, want an ONLINE server of HG${want%% *}"; ok=1
        fi
      done ;;
  esac
  return "$ok"
}

# --- commands --------------------------------------------------------------
cluster_up_one() {
  local c="$1"
  case "$c" in galera|pxc) wsrep_up "$c" ;; ic) ic_up ;; proxysql) proxysql_up ;; esac
  check_cluster "$c"
}

UP_FAILED=0
up_groups() {   # up_groups GROUP...: bring the groups up in parallel, counting failures in UP_FAILED
  local c pids="" p rc
  for c in "$@"; do
    rm -f "$STATE/$c.ready" "$STATE/$c.fail"
    # Not inside && or ||: there bash ignores set -e in every function called.
    ( cluster_up_one "$c"; touch "$STATE/$c.ready" ) > "$LOGS/$c.log" 2>&1 &
    pids="$pids $!:$c"
    log "$c: starting in the background (log: $LOGS/$c.log)"
  done
  for p in $pids; do
    rc=0; wait "${p%%:*}" || rc=$?
    if [ "$rc" -ne 0 ]; then
      UP_FAILED=$((UP_FAILED + 1)); echo "see $LOGS/${p##*:}.log" > "$STATE/${p##*:}.fail"
      log "FAILED ${p##*:}: $(tail -5 "$LOGS/${p##*:}.log")"
    else log "${p##*:}: healthy"; fi
  done
}

cmd_up() {
  command -v docker > /dev/null || die "docker is required"
  mkdir -p "$STATE" "$LOGS"; load_creds
  local cs c image clusters="" proxy=""; cs="$(selected_clusters "$@")"
  for c in $cs; do
    image="$(group_image "$c")"
    docker image inspect "$image" > /dev/null 2>&1 || { log "pulling $image"; docker pull -q "$image" > /dev/null; }
    if [ "$c" = proxysql ]; then proxy=proxysql; else clusters="$clusters $c"; fi
  done
  # shellcheck disable=SC2086
  if [ -n "$clusters" ]; then up_groups $clusters; fi
  # ProxySQL fronts Galera and InnoDB Cluster, so it starts after them.
  if [ -n "$proxy" ]; then up_groups proxysql; fi
  cmd_status
  [ "$UP_FAILED" -eq 0 ] || { log "$UP_FAILED cluster(s) failed; the rest are usable."; return 1; }
}

cmd_down() {
  local cs c n; cs="$(selected_clusters "$@")"
  # ProxySQL first: it is attached to the Galera and InnoDB Cluster networks.
  case " $cs " in *" proxysql "*) proxysql_down ;; esac
  for c in $cs; do
    [ "$c" = proxysql ] && continue
    for n in $(nodes "$c") $( [ "$c" = ic ] && echo ic-router ); do
      docker rm -fv "$(cname "$n")" > /dev/null 2>&1 && log "removed $(cname "$n")" || true
      docker volume rm "$(volume "$n")" > /dev/null 2>&1 || true
    done
    [ "$c" = pxc ] && docker volume rm mf-pxc-certs mf-pxc-conf > /dev/null 2>&1 || true
    # A ProxySQL that stays leaves the network (up proxysql attaches it again).
    if exists proxysql; then docker network disconnect -f "$(network "$c")" "$(cname proxysql)" > /dev/null 2>&1 || true; fi
    docker network rm "$(network "$c")" > /dev/null 2>&1 || true
    rm -f "$STATE/$c".*
  done
}

cmd_status() {
  load_creds
  local n c v detail
  printf '%-10s %-14s %-8s %-6s %-26s %s\n' NAME CONTAINER STATE PORT VERSION DETAIL
  for n in $ALL_NODES; do
    c="$(cluster_of "$n")"
    if ! is_up "$n"; then printf '%-10s %-14s %-8s %-6s %-26s %s\n' "$n" "$(cname "$n")" down "$(port "$n")" - -; continue; fi
    case "$n" in
      ic-router) v="mysqlrouter"; detail="RW -> $(router_route RW || echo none), RO -> $(router_route RO || echo none)" ;;
      proxysql) v="$(px_version || true)"
        v="ProxySQL ${v%%-*}"; detail="$(px_summary)" ;;
      ic-*) v="$(rq "$n" "SELECT VERSION()" || true)"; detail="$(ic_member_line "$n")" ;;
      *) v="$(rq "$n" "SELECT VERSION()" || true)"; detail="size/status/state $(wsrep_line "$n")" ;;
    esac
    printf '%-10s %-14s %-8s %-6s %-26s %s\n' "$n" "$(cname "$n")" running "$(port "$n")" "${v:-?}" "$detail"
  done
}

cmd_check() {
  load_creds
  local cs c rc=0; cs="$(selected_clusters "$@")"
  for c in $cs; do check_cluster "$c" || { rc=1; log "$c: NOT healthy"; }; done
  return "$rc"
}

cmd_fail() {
  valid_node "$1"; is_up "$1" || die "$1 is not running"
  docker stop -t 30 "$(cname "$1")" > /dev/null
  log "$1 stopped (docker stop); recover with: $0 recover $1"
}

cmd_recover() {
  local n="$1" c o peer=""
  valid_node "$n"; load_creds; mkdir -p "$STATE" "$LOGS"
  is_up "$n" && { log "$n already running"; return 0; }
  c="$(cluster_of "$n")"
  case "$c" in
    proxysql)
      docker start "$(cname proxysql)" > /dev/null
      wait_proxysql 60 || die "proxysql did not start"
      px_attach
      # A restart empties the stats tables.
      px_wait_hostgroups 60 || die "proxysql: hostgroups not ready"
      px_traffic ;;
    galera|pxc)
      for o in $(nodes "$c"); do [ "$o" != "$n" ] && is_up "$o" && peer="$o"; done
      if [ -z "$peer" ]; then log "$c: no node is running; bringing the cluster up instead"; wsrep_up "$c"; return; fi
      [ "$c" = pxc ] && pxc_certs
      run_wsrep_node "$n" join
      wait_ready "$n" 400 && wait_synced "$n" 400 || die "$n did not rejoin" ;;
    ic)
      if [ "$n" = ic-router ]; then router_up; return; fi
      docker start "$(cname "$n")" > /dev/null
      wait_ready "$n" 300 || die "$n did not start"
      if ! wait_ic_online 90; then
        log "$n did not rejoin by itself; running rejoinInstance"
        local p; p="$(ic_primary)" || die "ic: no primary"
        ( export MF_TARGET="$(cname "$n")" MF_PRIMARY="mf-$p"
          ish "$p" <<'JS' ) || true
shell.connect(U(os.getenv("MF_PRIMARY")));
dba.getCluster().rejoinInstance(U(os.getenv("MF_TARGET")));
JS
        wait_ic_online 300 || die "$n did not rejoin"
      fi ;;
  esac
  log "$n recovered"
}

# --- fixtures --------------------------------------------------------------
cmd_fixtures() {
  local state="" out="$REPO/test/fixtures/cluster" cs=""
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --out) [ "$#" -ge 2 ] || die "--out needs a directory"; out="$2"; shift 2 ;;
      -*) die "unknown option $1" ;;
      *) if [ -z "$state" ]; then state="$1"; else cs="$cs $1"; fi; shift ;;
    esac
  done
  case "$state" in ''|*[!a-z0-9-]*) die "usage: fixtures STATE [CLUSTER...] [--out DIR] (STATE: lowercase word, e.g. healthy)" ;; esac
  load_creds; mkdir -p "$out"
  local c n
  # shellcheck disable=SC2086
  if [ -z "$cs" ]; then cs="$CLUSTERS"; else cs="$(selected_clusters $cs)"; fi
  for c in $cs; do
    case "$c" in proxysql) log "fixtures: proxysql is not recorded here (skipped)"; continue ;; esac
    for n in $(nodes "$c"); do
      if ! is_up "$n"; then log "fixtures: $n is down (not captured)"; continue; fi
      capture_node "$n" "$state" "$out"
    done
    if [ "$c" = ic ]; then capture_ic_extras "$state" "$out"; fi
  done
  write_fixture_index "$out"
}

capture_node() {
  local n="$1" state="$2" out="$3"
  ( export MYSQL_PWD="$ROOT_PASSWORD" ROOT_PASSWORD MF_ADMIN_PASSWORD MF_READ_PASSWORD
    python3 - "$(cname "$n")" "$(client "$n")" "$(cluster_of "$n")" "$n" "$state" "$out" "$(image_of "$n")" <<'PY'
import datetime, json, os, re, subprocess, sys
import xml.etree.ElementTree as ET

container, client, cluster, node, state, out_dir, image = sys.argv[1:8]
NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
SECRET_NAME = re.compile(r"(auth|password|passwd|secret|token)", re.I)
SECRETS = [os.environ[k] for k in ("ROOT_PASSWORD", "MF_ADMIN_PASSWORD", "MF_READ_PASSWORD") if os.environ.get(k)]


def run(sql):
    p = subprocess.run(["docker", "exec", "-e", "MYSQL_PWD", container, client, "-uroot", "--xml", "-e", sql],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        err = [l for l in p.stderr.decode("utf-8", "replace").splitlines() if l.startswith("ERROR")]
        return None, (err[-1] if err else "exit %d" % p.returncode)
    text = p.stdout.decode("utf-8", "replace").strip()
    if not text:
        return {"columns": [], "rows": []}, None
    root = ET.fromstring(text)
    columns, rows = [], []
    for row in root.findall("row"):
        fields = row.findall("field")
        if not columns:
            columns = [f.get("name") for f in fields]
        rows.append([None if f.get(NIL) == "true" else (f.text or "") for f in fields])
    return {"columns": columns, "rows": rows}, None


def scrub(result):
    """Redact secret-like variables and columns and any value containing a lab password."""
    cols = [c.lower() for c in result.get("columns", [])]
    secret_cols = set(i for i, c in enumerate(cols) if SECRET_NAME.search(c))
    for row in result.get("rows", []):
        name = row[0] if cols[:1] == ["variable_name"] else None
        for i, v in enumerate(row):
            if v is None:
                continue
            if i in secret_cols and v != "":
                row[i] = "<redacted>"
                continue
            if name is not None and i == 1 and SECRET_NAME.search(name) and v not in ("", "********"):
                row[i] = "<redacted>"
            for s in SECRETS:
                if s in v:
                    row[i] = row[i].replace(s, "<redacted>")
    return result


def table_columns(schema, table):
    r, _ = run("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='%s' AND TABLE_NAME='%s' "
               "ORDER BY ORDINAL_POSITION" % (schema, table))
    return [row[0] for row in (r or {}).get("rows", [])]


queries = []
if cluster in ("galera", "pxc"):
    queries += [
        ("identity", "SELECT @@version AS version, @@version_comment AS version_comment, @@hostname AS hostname, "
                     "@@port AS port, @@server_id AS server_id, @@read_only AS read_only, @@wsrep_on AS wsrep_on, "
                     "@@wsrep_node_name AS wsrep_node_name"),
        ("wsrep_status", "SHOW GLOBAL STATUS LIKE 'wsrep%'"),
        ("wsrep_variables", "SHOW GLOBAL VARIABLES LIKE 'wsrep%'"),
        ("wsrep_tables", "SELECT TABLE_SCHEMA, TABLE_NAME, ENGINE, TABLE_TYPE FROM information_schema.TABLES "
                         "WHERE TABLE_SCHEMA IN ('mysql', 'performance_schema', 'information_schema') "
                         "AND (TABLE_NAME LIKE '%wsrep%' OR TABLE_NAME LIKE 'pxc%') ORDER BY 1, 2"),
    ]
    if cluster == "galera":
        queries.append(("wsrep_plugins", "SELECT PLUGIN_NAME, PLUGIN_STATUS, PLUGIN_TYPE, PLUGIN_LIBRARY "
                                         "FROM information_schema.ALL_PLUGINS WHERE PLUGIN_NAME LIKE '%WSREP%' ORDER BY 1"))
    else:
        queries.append(("wsrep_plugins", "SELECT PLUGIN_NAME, PLUGIN_STATUS, PLUGIN_TYPE, PLUGIN_LIBRARY "
                                         "FROM information_schema.PLUGINS WHERE PLUGIN_NAME LIKE '%wsrep%' ORDER BY 1"))
        queries.append(("pxc_variables", "SHOW GLOBAL VARIABLES LIKE 'pxc%'"))
else:
    queries += [
        ("identity", "SELECT @@version AS version, @@version_comment AS version_comment, @@hostname AS hostname, "
                     "@@port AS port, @@server_id AS server_id, @@report_host AS report_host, @@read_only AS read_only, "
                     "@@super_read_only AS super_read_only, @@server_uuid AS server_uuid"),
        ("replication_group_members", "SELECT * FROM performance_schema.replication_group_members"),
        ("replication_group_member_stats", "SELECT * FROM performance_schema.replication_group_member_stats"),
        ("group_replication_variables", "SHOW GLOBAL VARIABLES LIKE 'group_replication%'"),
        # 8.4 names its Group Replication status counters Gr_*.
        ("group_replication_status", "SHOW GLOBAL STATUS WHERE Variable_name LIKE 'group\\_replication%' "
                                     "OR Variable_name LIKE 'Gr\\_%'"),
        ("replication_group_communication_information",
         "SELECT * FROM performance_schema.replication_group_communication_information"),
        ("replication_connection_status", "SELECT * FROM performance_schema.replication_connection_status"),
        ("replication_applier_status", "SELECT * FROM performance_schema.replication_applier_status"),
        ("gtid", "SELECT @@gtid_mode AS gtid_mode, @@gtid_executed AS gtid_executed, @@gtid_purged AS gtid_purged"),
        ("plugins", "SELECT PLUGIN_NAME, PLUGIN_STATUS, PLUGIN_TYPE, PLUGIN_LIBRARY FROM information_schema.PLUGINS "
                    "WHERE PLUGIN_NAME IN ('group_replication', 'clone') ORDER BY 1"),
        ("metadata_schema_version", "SELECT * FROM mysql_innodb_cluster_metadata.schema_version"),
        ("metadata_v2_clusters", "SELECT * FROM mysql_innodb_cluster_metadata.v2_clusters"),
        ("metadata_v2_instances", "SELECT * FROM mysql_innodb_cluster_metadata.v2_instances"),
        ("metadata_v2_routers", "SELECT * FROM mysql_innodb_cluster_metadata.v2_routers"),
        ("metadata_v2_this_instance", "SELECT * FROM mysql_innodb_cluster_metadata.v2_this_instance"),
    ]

doc = {
    "fixture": "cluster-matrix", "schema": "cluster-fixture-1", "cluster": cluster, "node": node,
    "container": container, "image": image, "state": state,
    "captured_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    "queries": {},
}
for key, sql in queries:
    result, err = run(sql)
    entry = {"sql": sql}
    if err:
        entry["error"] = err
    else:
        if not result["columns"] and sql.startswith("SELECT * FROM "):
            schema, table = sql[len("SELECT * FROM "):].split(".")
            result["columns"] = table_columns(schema, table)
        if not result["columns"] and sql.startswith("SHOW GLOBAL"):
            result["columns"] = ["Variable_name", "Value"]
        entry.update(scrub(result))
    doc["queries"][key] = entry

# Every wsrep or pxc table the server has, whole.
tables = doc["queries"].get("wsrep_tables", {}).get("rows", [])
for schema, table, _engine, _type in tables:
    sql = "SELECT * FROM `%s`.`%s`" % (schema, table)
    result, err = run(sql)
    entry = {"sql": sql}
    if err:
        entry["error"] = err
    else:
        if not result["columns"]:
            result["columns"] = table_columns(schema, table)
        entry.update(scrub(result))
    doc["queries"]["table:%s.%s" % (schema, table)] = entry

role = ""
if cluster == "ic":
    members = doc["queries"]["replication_group_members"]
    if "rows" in members and members["columns"]:
        h, r, s = (members["columns"].index(c) for c in ("MEMBER_HOST", "MEMBER_ROLE", "MEMBER_STATE"))
        for row in members["rows"]:
            if row[h] == container:
                role = (row[r] or "").lower() if row[s] == "ONLINE" else (row[s] or "").lower()
    doc["member_role"] = role or None
name = "%s-%s-%s%s.json" % (cluster, state, node, "-" + role if role else "")
path = os.path.join(out_dir, name)
with open(path, "w") as f:
    json.dump(doc, f, indent=1, sort_keys=False)
    f.write("\n")
print(path)
PY
  ) | while read -r p; do log "fixture $p"; done
}

capture_ic_extras() {   # mysqlsh cluster status and router routing for one state
  local state="$1" out="$2" p status rw="" ro=""
  p="$(ic_primary 2> /dev/null || true)"
  [ -n "$p" ] || { log "fixtures: no InnoDB Cluster primary; status not captured"; return 0; }
  status="$( ( export MF_PRIMARY="mf-$p"; ish "$p" <<'JS' 2> /dev/null ) | grep '^{' | tail -1 || true
shell.connect(U(os.getenv("MF_PRIMARY")));
print(JSON.stringify(dba.getCluster().status({extended: 1})) + "\n");
JS
)"
  if is_up ic-router; then rw="$(router_route RW || true)"; ro="$(router_route RO || true)"; fi
  ( STATUS="$status" RW="$rw" RO="$ro" ROUTER_UP="$(is_up ic-router && echo 1 || echo 0)" python3 - "$state" "$out" "mf-$p" <<'PY'
import datetime, json, os, sys
state, out, primary = sys.argv[1:4]
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
try:
    status = json.loads(os.environ["STATUS"])
except ValueError:
    status = None
docs = {
    "ic-%s-shell-status.json" % state: {
        "fixture": "cluster-matrix", "schema": "cluster-fixture-1", "cluster": "ic", "state": state,
        "captured_at": now, "source": "mysqlsh dba.getCluster().status({extended: 1}) on %s" % primary,
        "status": status},
    "ic-%s-router.json" % state: {
        "fixture": "cluster-matrix", "schema": "cluster-fixture-1", "cluster": "ic", "state": state,
        "captured_at": now, "router_running": os.environ["ROUTER_UP"] == "1",
        "source": "SELECT @@hostname through mf-ic-router:6446 (RW, host 34306) and :6447 (RO, host 34307)",
        "routes": {"rw": {"host_port": 34306, "reached": os.environ["RW"] or None},
                   "ro": {"host_port": 34307, "reached": os.environ["RO"] or None}}},
}
for name, doc in docs.items():
    with open(os.path.join(out, name), "w") as f:
        json.dump(doc, f, indent=1)
        f.write("\n")
    print(os.path.join(out, name))
PY
  ) | while read -r f; do log "fixture $f"; done
}

write_fixture_index() {
  python3 - "$1" <<'PY'
import json, os, sys
out = sys.argv[1]
files = []
for name in sorted(os.listdir(out)):
    if not name.endswith(".json") or name == "index.json":
        continue
    with open(os.path.join(out, name)) as f:
        d = json.load(f)
    if not isinstance(d, dict) or d.get("fixture") != "cluster-matrix":
        continue   # another recorder's file (e.g. cluster-<server>.json replays)
    files.append({"file": name, "cluster": d.get("cluster"), "state": d.get("state"), "node": d.get("node"),
                  "member_role": d.get("member_role"), "captured_at": d.get("captured_at")})
with open(os.path.join(out, "index.json"), "w") as f:
    json.dump({"fixture": "cluster-matrix", "generator": "scripts/cluster-matrix.sh fixtures", "files": files}, f, indent=1)
    f.write("\n")
PY
}

# --- profiles --------------------------------------------------------------
profile_rows() {   # profile node port
  local n
  for n in galera-1 galera-2 galera-3 pxc-1 pxc-2 pxc-3 ic-1 ic-2 ic-3; do echo "cluster-$n $n $(port "$n")"; done
  echo "cluster-ic-router-rw ic-router 34306"
}

mf() { ( cd "$MYFLAMES_SRC" && HOME="$MYFLAMES_HOME" MYFLAMES_SECRET_BACKEND=vault python3 -m myflames "$@" ); }

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

# Same guard as scripts/ga-matrix.sh: refuse a MYFLAMES_HOME that is the real
# home (or contains it), or whose store or itself is the real ~/.myflames or
# inside it, by physical path, before anything is created.
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

parse_home() {   # --home DIR overrides MYFLAMES_HOME
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --home) [ "$#" -ge 2 ] || die "--home needs a directory"; MYFLAMES_HOME="$2"; shift 2 ;;
      *) die "unknown argument $1" ;;
    esac
  done
}

# save_profile NAME PORT USER: a development connection to 127.0.0.1:PORT with
# the password in $PW remembered.
save_profile() {
  ( cd "$MYFLAMES_SRC" && HOME="$MYFLAMES_HOME" MYFLAMES_SECRET_BACKEND=vault PYTHONPATH="$MYFLAMES_SRC" \
    python3 - "$1" "$2" "$3" <<'PY'
import os, sys
from myflames.workspace_store import WorkspaceStore
name, port, user = sys.argv[1:4]
store = WorkspaceStore()
existing = [p for p in store.list('profile') if p['name'] == name]
# ssl_mode is saved explicitly: a saved password is only released to the
# exact TLS settings it was saved with, and the workspace sends PREFERRED.
store.save('profile', {'name': name, 'host': '127.0.0.1', 'port': int(port), 'user': user,
                       'ssl_mode': 'PREFERRED', 'ssl_ca': '', 'type': 'development', 'read_only': False},
           key=existing[0]['id'] if existing else None, password=os.environ['PW'], remember=True)
PY
  )
}

# px_smoke PROFILE OUT: the workspace's ProxySQL read (/api/live/proxysql) with
# the saved profile; writes the report to OUT and fails unless it is ProxySQL
# with servers.
px_smoke() {
  ( cd "$MYFLAMES_SRC" && HOME="$MYFLAMES_HOME" MYFLAMES_SECRET_BACKEND=vault PYTHONPATH="$MYFLAMES_SRC" \
    python3 - "$1" "$2" <<'PY'
import json, sys
from myflames import ui
from myflames.workspace_store import WorkspaceStore
name, out = sys.argv[1:3]


class Workspace(object):
    store = WorkspaceStore()


profile = [p for p in Workspace.store.list('profile') if p['name'] == name][0]
connection = dict((k, profile.get(k)) for k in ('host', 'port', 'user', 'ssl_mode', 'ssl_ca', 'type', 'read_only'))
connection['profile_id'] = profile['id']
report = ui.api_response('/api/live/proxysql', {'connection': connection}, Workspace())
with open(out, 'w') as f:
    json.dump(report, f, indent=1)
if not (report.get('is_proxysql') and report.get('servers')):
    sys.exit('not a ProxySQL Admin with servers: %s' % report.get('message'))
PY
  )
}

cmd_profiles() {
  parse_home "$@"
  check_myflames_home
  load_creds; mkdir -p "$MYFLAMES_HOME" "$STATE"; chmod 700 "$MYFLAMES_HOME"
  local p n port save_fails=0 proxy=0
  while read -r p n port; do
    if ( export PW="$MF_ADMIN_PASSWORD"; save_profile "$p" "$port" mf_admin ); then
      log "profile $p saved (127.0.0.1:$port, mf_admin, password remembered)"
    else log "profile $p NOT saved"; save_fails=$((save_fails + 1)); fi
  done < <(profile_rows)
  # ProxySQL Admin, while mf-proxysql runs.
  if is_up proxysql; then
    if [ -z "${PROXYSQL_ADMIN_PASSWORD:-}" ]; then
      log "profile proxysql-admin NOT saved: no PROXYSQL_ADMIN_PASSWORD in $CREDS_FILE (run: $0 up proxysql)"
      save_fails=$((save_fails + 1))
    elif ( export PW="$PROXYSQL_ADMIN_PASSWORD"; save_profile proxysql-admin 36032 "$PROXYSQL_ADMIN_USER" ); then
      log "profile proxysql-admin saved (127.0.0.1:36032, $PROXYSQL_ADMIN_USER, password remembered)"; proxy=1
    else log "profile proxysql-admin NOT saved"; save_fails=$((save_fails + 1)); fi
  fi
  # Smoke: a read-only command per profile, in parallel.
  mkdir -p "$STATE/smoke"
  rm -f "$STATE"/smoke/*
  profile_rows | {
    while read -r p n port; do
      is_up "$n" || { echo "skipped (node down)" > "$STATE/smoke/$p.result"; continue; }
      ( mf health --profile "$p" --interval 1 --json -q -o "$STATE/smoke/$p.json" > "$STATE/smoke/$p.out" 2>&1 \
          && echo pass > "$STATE/smoke/$p.result" || echo "fail ($(tail -1 "$STATE/smoke/$p.out"))" > "$STATE/smoke/$p.result" ) &
    done
    # ProxySQL Admin answers neither the server views nor the Query editor
    # (its SQLite dialect rejects @@ variables): read what the ProxySQL place
    # reads, through the workspace endpoint.
    if [ "$proxy" = 1 ]; then
      p=proxysql-admin
      ( px_smoke "$p" "$STATE/smoke/$p.json" > "$STATE/smoke/$p.out" 2>&1 \
          && echo pass > "$STATE/smoke/$p.result" || echo "fail ($(tail -1 "$STATE/smoke/$p.out"))" > "$STATE/smoke/$p.result" ) &
    fi
    wait
  }
  local fails=0 r
  for r in "$STATE"/smoke/*.result; do
    [ -e "$r" ] || continue
    log "smoke $(basename "$r" .result): $(cat "$r")"
    case "$(cat "$r")" in fail*) fails=$((fails + 1)) ;; esac
  done
  [ "$save_fails" -eq 0 ] || log "$save_fails profile(s) not saved"
  [ "$fails" -eq 0 ] && [ "$save_fails" -eq 0 ]
}

cmd_env() {
  parse_home "$@"
  check_myflames_home
  load_creds
  local rows="" p n port v
  while read -r p n port; do
    v=""; is_up "$n" && v="$(rq "$n" "SELECT VERSION()" 2> /dev/null || true)"
    [ "$n" = ic-router ] && is_up "$n" && v="mysqlrouter"
    rows="$rows$n	$(cluster_of "$n")	$(cname "$n")	$port	$p	$v
"
  done <<EOF
$(profile_rows)
EOF
  local pxv=""
  is_up proxysql && pxv="$(px_version || true)"
  ROWS="$rows" HOMEDIR="$MYFLAMES_HOME" CREDS="$CREDS_FILE" STATEDIR="$STATE" PX_VERSION="$pxv" \
  PX_UP="$(is_up proxysql && echo 1 || echo 0)" PX_IMAGE="$PROXYSQL_IMAGE" PX_ADMIN="$PROXYSQL_ADMIN_USER" \
  PX_MONITOR="$PROXYSQL_MONITOR_USER" \
  IMAGES="galera=$GALERA_IMAGE pxc=$PXC_IMAGE ic=$IC_IMAGE" python3 - <<'PY'
import json, os
images = dict(kv.split("=", 1) for kv in os.environ["IMAGES"].split())
names = {"galera": "mf_galera", "pxc": "mf_pxc", "ic": "mfcluster"}
out = {"env": {"HOME": os.environ["HOMEDIR"], "MYFLAMES_SECRET_BACKEND": "vault"},
       "credentials_env": os.environ["CREDS"], "clusters": {}}
smoke = os.path.join(os.environ["STATEDIR"], "smoke", "proxysql-admin.result")
out["proxysql"] = {
    "image": os.environ["PX_IMAGE"], "container": "mf-proxysql", "networks": ["mf-galera-net", "mf-ic-net"],
    "host": "127.0.0.1", "ports": {"admin": 36032, "mysql": 36033}, "admin_user": os.environ["PX_ADMIN"],
    "monitor_user": os.environ["PX_MONITOR"], "frontend_user": "mf_admin", "default_hostgroup": 30,
    "profile": "proxysql-admin", "up": os.environ["PX_UP"] == "1", "version": os.environ["PX_VERSION"] or None,
    "hostgroups": {
        "galera": {"table": "mysql_galera_hostgroups", "writer": 10, "backup_writer": 11, "reader": 12, "offline": 19},
        "ic": {"table": "mysql_group_replication_hostgroups", "writer": 30, "backup_writer": 31, "reader": 32,
               "offline": 39}},
    "query_rules": [
        {"rule_id": 10, "match_pattern": r"^/\* galera \*/", "destination_hostgroup": 10},
        {"rule_id": 20, "match_digest": "^SELECT .* FOR UPDATE", "destination_hostgroup": 30},
        {"rule_id": 30, "match_digest": "^SELECT", "destination_hostgroup": 32}],
    "smoke": open(smoke).read().strip() if os.path.exists(smoke) else "not run",
}
for line in os.environ["ROWS"].splitlines():
    node, cluster, container, port, profile, version = (line.split("\t") + [""] * 6)[:6]
    c = out["clusters"].setdefault(cluster, {"image": images[cluster], "cluster_name": names[cluster],
                                             "network": "mf-%s-net" % cluster, "nodes": []})
    r = os.path.join(os.environ["STATEDIR"], "smoke", profile + ".result")
    entry = {"name": node, "container": container, "host": "127.0.0.1", "port": int(port), "profile": profile,
             "up": bool(version), "version": version or None,
             "smoke": open(r).read().strip() if os.path.exists(r) else "not run"}
    if node == "ic-router":
        entry["ports"] = {"rw": 34306, "ro": 34307}
    c["nodes"].append(entry)
print(json.dumps(out, indent=2))
PY
}

main() {
  local cmd="${1:-}"; [ "$#" -gt 0 ] && shift
  case "$cmd" in
    up) cmd_up "$@" ;;
    down) cmd_down "$@" ;;
    status) cmd_status ;;
    check) cmd_check "$@" ;;
    profiles) cmd_profiles "$@" ;;
    env) cmd_env "$@" ;;
    fail) [ "$#" -eq 1 ] || die "usage: fail NAME"; cmd_fail "$1" ;;
    recover) [ "$#" -eq 1 ] || die "usage: recover NAME"; cmd_recover "$1" ;;
    fixtures) cmd_fixtures "$@" ;;
    *) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; [ -z "$cmd" ] || exit 2 ;;
  esac
}
main "$@"
