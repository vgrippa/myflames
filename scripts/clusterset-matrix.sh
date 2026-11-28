#!/usr/bin/env bash
# scripts/clusterset-matrix.sh
#
# Bring up a throwaway MySQL 8.4 InnoDB ClusterSet lab in Docker for live
# tests of myflames' ClusterSet view and actions. For development and testing
# only: servers are throwaway and mf_admin is a clusterAdmin account.
#
# Usage:
#   scripts/clusterset-matrix.sh up             start, configure, form ClusterSet mfcs, bootstrap Router (idempotent)
#   scripts/clusterset-matrix.sh status         container status plus dba.getClusterSet().status({extended:1})
#   scripts/clusterset-matrix.sh shell ['<js>'] run mysqlsh --js in mf-cs-runner (stdin if omitted or -)
#   scripts/clusterset-matrix.sh down           remove mf-cs-* containers, volumes, network and lab state
#   scripts/clusterset-matrix.sh reset          down then up
#
# Lab layout (bound to 127.0.0.1; ports 34601-34607, or the next free block in
# 346xx if busy; network mf-cs-net):
#   mf-cs-a1, mf-cs-a2   mysql:8.4  cluster "csa" (PRIMARY)         ports 34601, 34602
#   mf-cs-b1, mf-cs-b2   mysql:8.4  cluster "csb" (REPLICA)         ports 34603, 34604
#   mf-cs-c1             mysql:8.4  spare configured instance       port  34605
#   mf-cs-router         mf-mysql-router:8.4  bootstrapped on mfcs  ports 34606 (RW), 34607 (RO)
#   mf-cs-runner         FROM mysql:9.7.2 + python3 (MySQL Shell 9.7) on mf-cs-net
#
# Accounts (random passwords in $CREDS_FILE, chmod 0600, never on argv):
#   root       container root password (used only for bootstrap and readiness)
#   mf_admin   clusterAdmin created by dba.configureInstance
#   mf_read    read-only monitoring account (SELECT, PROCESS, REPLICATION CLIENT, SHOW VIEW on *.*)
#
# Settings (environment):
#   CLUSTERSET_LAB   runtime state, logs, credentials (default local/clusterset-matrix)
#   CREDS_FILE       default $CLUSTERSET_LAB/credentials.env
#
# Requirements: Docker, openssl, python3.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
LAB="${CLUSTERSET_LAB:-$REPO/local/clusterset-matrix}"
CREDS_FILE="${CREDS_FILE:-$LAB/credentials.env}"
STATE="$LAB/state"
LOGS="$LAB/logs"
PORTS_FILE="$STATE/ports.env"

MYSQL_IMAGE="${MYSQL_IMAGE:-mysql:8.4}"
RUNNER_BASE_IMAGE="${RUNNER_BASE_IMAGE:-mysql:9.7.2}"
RUNNER_IMAGE="${RUNNER_IMAGE:-mf-cs-runner}"
ROUTER_OFFICIAL="mysql/mysql-router:8.4"
ROUTER_LOCAL="mf-mysql-router:8.4"

NET="mf-cs-net"
CLUSTERSET_NAME="mfcs"
PRIMARY_CLUSTER="csa"
REPLICA_CLUSTER="csb"
MF_ADMIN_USER="mf_admin"
MF_READ_USER="mf_read"
SERVER_NODES="a1 a2 b1 b2 c1"
ALL_NODES="a1 a2 b1 b2 c1 router"

log() { echo "[clusterset-matrix] $*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# --- names and metadata ----------------------------------------------------
node_idx() {
  case "$1" in
    a1) echo 1 ;;
    a2) echo 2 ;;
    b1) echo 3 ;;
    b2) echo 4 ;;
    c1) echo 5 ;;
    *) return 1 ;;
  esac
}

node_sid() { echo $((4600 + $(node_idx "$1"))); }
cname() { echo "mf-cs-$1"; }
volume() { echo "mf-cs-$1-data"; }

port_of() {
  case "$1" in
    a1) echo "${PORT_a1:-}" ;;
    a2) echo "${PORT_a2:-}" ;;
    b1) echo "${PORT_b1:-}" ;;
    b2) echo "${PORT_b2:-}" ;;
    c1) echo "${PORT_c1:-}" ;;
    router) echo "${PORT_router_rw:-}" ;;
    router-rw) echo "${PORT_router_rw:-}" ;;
    router-ro) echo "${PORT_router_ro:-}" ;;
    *) return 1 ;;
  esac
}

is_up() { docker ps --format '{{.Names}}' | grep -Fxq "$(cname "$1")"; }
exists() { docker ps -a --format '{{.Names}}' | grep -Fxq "$(cname "$1")"; }

ensure_network() {
  docker network inspect "$NET" > /dev/null 2>&1 || docker network create --ipv6=false "$NET" > /dev/null
}

# --- ports -----------------------------------------------------------------
container_host_port() {
  local cn="$1" cport="$2"
  docker inspect -f "{{with index .NetworkSettings.Ports \"$cport/tcp\"}}{{(index . 0).HostPort}}{{end}}" "$cn" 2> /dev/null || true
}

resolve_ports() {
  if [ -f "$PORTS_FILE" ]; then
    # shellcheck disable=SC1090
    . "$PORTS_FILE"
  fi
  local n p any_existing=0
  for n in $ALL_NODES; do
    if exists "$n"; then
      any_existing=1
      if [ "$n" = router ]; then
        p="$(container_host_port "$(cname router)" 6446)"
        [ -n "$p" ] && PORT_router_rw="$p"
        p="$(container_host_port "$(cname router)" 6447)"
        [ -n "$p" ] && PORT_router_ro="$p"
      else
        p="$(container_host_port "$(cname "$n")" 3306)"
        [ -n "$p" ] && eval "PORT_${n}=\"$p\""
      fi
    fi
  done

  if [ "$any_existing" = 1 ] && [ -n "${PORT_a1:-}" ] && [ -n "${PORT_a2:-}" ] && \
     [ -n "${PORT_b1:-}" ] && [ -n "${PORT_b2:-}" ] && [ -n "${PORT_c1:-}" ] && \
     [ -n "${PORT_router_rw:-}" ] && [ -n "${PORT_router_ro:-}" ]; then
    save_ports
    return 0
  fi

  local busy_docker
  busy_docker="$(docker ps -a --format '{{.Names}} {{.Ports}}' | grep -v '^mf-cs-' || true)"
  local chosen
  chosen="$(BUSY_DOCKER="$busy_docker" \
    CUR_A1="${PORT_a1:-}" CUR_A2="${PORT_a2:-}" CUR_B1="${PORT_b1:-}" CUR_B2="${PORT_b2:-}" CUR_C1="${PORT_c1:-}" \
    CUR_RW="${PORT_router_rw:-}" CUR_RO="${PORT_router_ro:-}" \
    python3 - <<'PY'
import os, re, socket, sys

busy = set()
for m in re.finditer(r':(346\d\d)->', os.environ.get("BUSY_DOCKER", "")):
    busy.add(int(m.group(1)))

def is_free(port):
    if port in busy:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()

cur = {
    "a1": os.environ.get("CUR_A1", ""),
    "a2": os.environ.get("CUR_A2", ""),
    "b1": os.environ.get("CUR_B1", ""),
    "b2": os.environ.get("CUR_B2", ""),
    "c1": os.environ.get("CUR_C1", ""),
    "rw": os.environ.get("CUR_RW", ""),
    "ro": os.environ.get("CUR_RO", ""),
}

if all(cur.values()):
    print(" ".join(cur[k] for k in ("a1", "a2", "b1", "b2", "c1", "rw", "ro")))
    sys.exit(0)

# Prefer 34601-34605 for the five MySQL nodes and 34606-34607 for Router.
if all(is_free(p) for p in range(34601, 34608)):
    print("34601 34602 34603 34604 34605 34606 34607")
    sys.exit(0)

block_start = None
for start in range(34601, 34696):
    if all(is_free(start + i) for i in range(5)):
        block_start = start
        break

if block_start is None:
    sys.stderr.write("no free block of five ports in 34601-34699\n")
    sys.exit(1)

used = set(range(block_start, block_start + 5))
rw = ro = None
if block_start + 6 <= 34699 and is_free(block_start + 5) and is_free(block_start + 6):
    rw, ro = block_start + 5, block_start + 6
else:
    free_other = [p for p in range(34601, 34700) if p not in used and is_free(p)]
    for i in range(len(free_other) - 1):
        if free_other[i + 1] == free_other[i] + 1:
            rw, ro = free_other[i], free_other[i + 1]
            break
    if rw is None and len(free_other) >= 2:
        rw, ro = free_other[0], free_other[1]

if rw is None or ro is None:
    sys.stderr.write("no two free router ports in 34601-34699\n")
    sys.exit(1)

print("%d %d %d %d %d %d %d" % (
    block_start, block_start + 1, block_start + 2, block_start + 3, block_start + 4, rw, ro
))
PY
  )" || die "could not find free ports in 346xx"

  read -r PORT_a1 PORT_a2 PORT_b1 PORT_b2 PORT_c1 PORT_router_rw PORT_router_ro <<< "$chosen"
  if [ "$PORT_a1" != "34601" ]; then
    log "default ports 34601-34607 busy; selected ports $PORT_a1-$PORT_c1 (router $PORT_router_rw/$PORT_router_ro)"
  fi
  save_ports
}

save_ports() {
  mkdir -p "$STATE"
  cat > "$PORTS_FILE" <<EOF
PORT_a1=$PORT_a1
PORT_a2=$PORT_a2
PORT_b1=$PORT_b1
PORT_b2=$PORT_b2
PORT_c1=$PORT_c1
PORT_router_rw=$PORT_router_rw
PORT_router_ro=$PORT_router_ro
EOF
}

# --- credentials and SQL helpers -------------------------------------------
create_creds() {
  mkdir -p "$(dirname "$CREDS_FILE")"
  chmod 700 "$LAB"
  ( umask 077
    printf 'ROOT_PASSWORD=%s\nMF_ADMIN_USER=%s\nMF_ADMIN_PASSWORD=%s\nMF_READ_USER=%s\nMF_READ_PASSWORD=%s\n' \
      "$(openssl rand -hex 12)" "$MF_ADMIN_USER" "$(openssl rand -hex 12)" "$MF_READ_USER" "$(openssl rand -hex 12)" > "$CREDS_FILE" )
  chmod 600 "$CREDS_FILE"
}

load_creds() {
  [ -f "$CREDS_FILE" ] || create_creds
  chmod 600 "$CREDS_FILE"
  # shellcheck disable=SC1090
  . "$CREDS_FILE"
}

# sqlx NODE USER PASSWORD [mysql args]: SQL on stdin, password in container env.
sqlx() {
  local n="$1" user="$2" pw="$3"; shift 3
  MYSQL_PWD="$pw" docker exec -i -e MYSQL_PWD "$(cname "$n")" mysql -u"$user" -h127.0.0.1 --protocol=TCP "$@"
}

rootsql() { sqlx "$1" root "$ROOT_PASSWORD" "${@:2}"; }

rq() {
  local n="$1"; shift
  rootsql "$n" --batch --skip-column-names -e "$*" < /dev/null 2> /dev/null
}

wait_ready() {
  local n="$1" limit="${2:-240}" i
  for i in $(seq 1 "$limit"); do
    if is_up "$n" && MYSQL_PWD="$ROOT_PASSWORD" docker exec -e MYSQL_PWD "$(cname "$n")" \
        mysql -uroot -h127.0.0.1 --protocol=TCP -e "SELECT 1" > /dev/null 2>&1; then
      log "$n accepts connections (${i}s)"
      return 0
    fi
    if ! is_up "$n" && [ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$(cname "$n")" 2> /dev/null)" = no ]; then
      log "$n exited:"
      docker logs --tail 30 "$(cname "$n")" >&2 2>&1 || true
      return 1
    fi
    if [ "$i" -ge 15 ] && [ "$(docker inspect -f '{{.RestartCount}}' "$(cname "$n")" 2> /dev/null || echo 0)" -ge 3 ]; then
      log "$n is crash-looping:"
      docker logs --tail 30 "$(cname "$n")" >&2 2>&1 || true
      return 1
    fi
    sleep 1
  done
  log "$n did not accept connections within ${limit}s:"
  docker logs --tail 30 "$(cname "$n")" >&2 2>&1 || true
  return 1
}

# --- images ----------------------------------------------------------------
docker_build_stdin() {
  local tag="$1" logfile="$2"
  if docker build -q -t "$tag" - > /dev/null 2> "$logfile"; then
    return 0
  fi
  # Sandboxes that cannot write ~/.docker/buildx still run the legacy builder.
  DOCKER_BUILDKIT=0 docker build -q -t "$tag" - > /dev/null 2>> "$logfile"
}

ensure_runner_image() {
  if docker image inspect "$RUNNER_IMAGE" > /dev/null 2>&1; then
    return 0
  fi
  docker image inspect "$RUNNER_BASE_IMAGE" > /dev/null 2>&1 || {
    log "pulling $RUNNER_BASE_IMAGE"
    docker pull -q "$RUNNER_BASE_IMAGE" > /dev/null
  }
  log "building $RUNNER_IMAGE from $RUNNER_BASE_IMAGE (adding python3)"
  mkdir -p "$LOGS"
  printf 'FROM %s\nRUN microdnf install -y python3 && microdnf clean all\n' "$RUNNER_BASE_IMAGE" \
    | docker_build_stdin "$RUNNER_IMAGE" "$LOGS/runner-build.log" \
    || die "runner build failed: $(tail -5 "$LOGS/runner-build.log")"
}

router_image() {
  local arch img_arch
  if docker image inspect "$ROUTER_LOCAL" > /dev/null 2>&1; then
    echo "$ROUTER_LOCAL"
    return 0
  fi
  arch="$(docker info --format '{{.Architecture}}' 2> /dev/null)"
  case "$arch" in aarch64|arm64) arch=arm64 ;; x86_64|amd64) arch=amd64 ;; esac
  if docker image inspect "$ROUTER_OFFICIAL" > /dev/null 2>&1 || docker pull -q "$ROUTER_OFFICIAL" > /dev/null 2>&1; then
    img_arch="$(docker image inspect "$ROUTER_OFFICIAL" --format '{{.Architecture}}')"
    [ "$img_arch" = "$arch" ] && { echo "$ROUTER_OFFICIAL"; return 0; }
  fi
  log "building $ROUTER_LOCAL from $MYSQL_IMAGE"
  mkdir -p "$LOGS"
  printf 'FROM %s\nRUN microdnf -y install mysql-router-community && microdnf clean all\n' "$MYSQL_IMAGE" \
    | docker_build_stdin "$ROUTER_LOCAL" "$LOGS/router-build.log" \
    || { log "router build failed: $(tail -5 "$LOGS/router-build.log")"; return 1; }
  echo "$ROUTER_LOCAL"
}

ensure_images() {
  docker image inspect "$MYSQL_IMAGE" > /dev/null 2>&1 || {
    log "pulling $MYSQL_IMAGE"
    docker pull -q "$MYSQL_IMAGE" > /dev/null
  }
  ensure_runner_image
  router_image > /dev/null || die "could not obtain router image"
}

# --- runner mysqlsh helpers ------------------------------------------------
# runner_sh USER HOST: execute mysqlsh --js inside mf-cs-runner on mf-cs-net
# with stdin JavaScript. Credentials travel strictly via the 0600 file mounted
# at /run/creds.env, never on argv or in container env.
runner_sh() {
  local db_user="$1" db_host="$2"
  ensure_runner_image
  ensure_network
  docker run -i --rm --name "mf-cs-runner-$$" --network "$NET" \
    -v "$CREDS_FILE:/run/creds.env:ro" -e LC_ALL=C "$RUNNER_IMAGE" \
    sh -c '
      set -e
      umask 077
      u="$1"; h="$2"
      if [ "$u" = root ]; then k="ROOT_PASSWORD"; else k="MF_ADMIN_PASSWORD"; fi
      pw=$(sed -n "s/^${k}=//p" /run/creds.env | sed "s/^['\''\"]//; s/['\''\"]$//")
      [ -n "$pw" ] || { echo "missing credential $k" >&2; exit 1; }
      printf "[client]\nuser=%s\npassword=%s\nhost=%s\nport=3306\n" "$u" "$pw" "$h" > /tmp/my.cnf
      mkfifo /tmp/err.pipe
      grep -v "Cannot set LC_ALL" < /tmp/err.pipe >&2 &
      err_pid=$!
      set +e
      mysqlsh --defaults-file=/tmp/my.cnf --js --no-wizard --quiet-start=2 2> /tmp/err.pipe
      rc=$?
      wait "$err_pid" || true
      exit "$rc"
    ' _ "$db_user" "$db_host"
}

RUNNER_PRELUDE='
function readCred(name) {
  var lines = os.loadTextFile("/run/creds.env").split(/\r?\n/);
  for (var i = 0; i < lines.length; i++) {
    var idx = lines[i].indexOf("=");
    if (idx > 0 && lines[i].slice(0, idx) === name) {
      return lines[i].slice(idx + 1).replace(/^['\''"]|['\''"]$/g, "");
    }
  }
  throw new Error("Missing credential: " + name);
}
var rootPw = readCred("ROOT_PASSWORD");
var adminPw = readCred("MF_ADMIN_PASSWORD");
function U(h) { return {scheme: "mysql", user: "root", host: h, port: 3306, password: rootPw}; }
function UA(h) { return {scheme: "mysql", user: "mf_admin", host: h, port: 3306, password: adminPw}; }
'

runner_setup_js() {
  local db_user="$1" db_host="$2"
  { printf '%s\n' "$RUNNER_PRELUDE"; cat; } | runner_sh "$db_user" "$db_host"
}

# --- server containers and state probes ------------------------------------
run_mysql_node() {
  local n="$1" cn sid port
  cn="$(cname "$n")"
  sid="$(node_sid "$n")"
  port="$(port_of "$n")"
  if exists "$n"; then
    is_up "$n" || docker start "$cn" > /dev/null
    return 0
  fi
  ensure_network
  docker volume create "$(volume "$n")" > /dev/null
  log "$n: starting ($MYSQL_IMAGE on 127.0.0.1:$port)"
  # Disable native AIO so sharing the Docker VM with other MySQL labs does not
  # exhaust fs.aio-max-nr (io_setup EAGAIN) on container init.
  MYSQL_ROOT_PASSWORD="$ROOT_PASSWORD" docker run -d --name "$cn" --hostname "$cn" --network "$NET" \
    -p "127.0.0.1:${port}:3306" \
    -v "$(volume "$n"):/var/lib/mysql" \
    -e MYSQL_ROOT_PASSWORD \
    --restart unless-stopped \
    --label myflames-cluster=clusterset \
    "$MYSQL_IMAGE" \
    --server-id="$sid" \
    --report-host="$cn" \
    --gtid-mode=ON \
    --enforce-gtid-consistency=ON \
    --innodb-use-native-aio=OFF \
    --innodb-buffer-pool-size=128M > /dev/null
}

node_gr_line() {
  local n="$1" out
  out="$(rq "$n" "SELECT MEMBER_STATE, MEMBER_ROLE FROM performance_schema.replication_group_members WHERE MEMBER_HOST='$(cname "$n")'" || true)"
  [ -n "$out" ] && echo "$out" | tr '\t' ' ' || echo "- -"
}

cluster_primary_for() {
  local n p
  for n in "$@"; do
    is_up "$n" || continue
    p="$(rq "$n" "SELECT MEMBER_HOST FROM performance_schema.replication_group_members WHERE MEMBER_ROLE='PRIMARY' AND MEMBER_STATE='ONLINE'" || true)"
    [ -n "$p" ] && { echo "${p#mf-cs-}"; return 0; }
  done
  return 1
}

clusterset_primary_node() {
  local n p
  for n in a1 a2 b1 b2; do
    is_up "$n" || continue
    p="$(rq "$n" "SELECT m.cluster_name FROM mysql_innodb_cluster_metadata.v2_cs_members m WHERE m.member_role='PRIMARY' AND m.invalidated=0 LIMIT 1" || true)"
    if [ "$p" = "$PRIMARY_CLUSTER" ]; then
      cluster_primary_for a1 a2 && return 0
    elif [ "$p" = "$REPLICA_CLUSTER" ]; then
      cluster_primary_for b1 b2 && return 0
    fi
  done
  cluster_primary_for a1 a2 && return 0
  cluster_primary_for b1 b2 && return 0
  for n in $SERVER_NODES; do
    is_up "$n" && { echo "$n"; return 0; }
  done
  return 1
}

has_metadata() {
  local n="$1"
  [ "$(rq "$n" "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='mysql_innodb_cluster_metadata'" || echo 0)" = 1 ]
}

ensure_read_user() {
  local n="$1" binlog_off="${2:-0}"
  {
    [ "$binlog_off" = 1 ] && echo "SET SESSION sql_log_bin = 0;"
    cat <<SQL
CREATE USER IF NOT EXISTS '$MF_READ_USER'@'%' IDENTIFIED BY '$MF_READ_PASSWORD';
GRANT SELECT, PROCESS, REPLICATION CLIENT, SHOW VIEW ON *.* TO '$MF_READ_USER'@'%';
SQL
  } | rootsql "$n" > /dev/null
}

ensure_spare_c1() {
  local has_admin
  has_admin="$(rq c1 "SELECT COUNT(*) FROM mysql.user WHERE user='$MF_ADMIN_USER' AND host='%'" || echo 0)"
  if [ "$has_admin" != 1 ]; then
    log "c1: configuring spare instance with clusterAdmin $MF_ADMIN_USER"
    runner_setup_js root mf-cs-c1 <<'JS' || die "c1: configureInstance failed"
dba.configureInstance(U("mf-cs-c1"), {
  clusterAdmin: "mf_admin",
  clusterAdminPassword: adminPw,
  restart: false
});
JS
    ensure_read_user c1 1
    # Clear local GTIDs on the spare instance so createReplicaCluster tests can
    # also run with default recoveryMethod without hitting errant transactions.
    rootsql c1 -e "RESET BINARY LOGS AND GTIDS;" < /dev/null > /dev/null
  else
    ensure_read_user c1 1
  fi
}

wait_cluster_online() {
  local c="$1" nodes_list="$2" expected="$3" limit="${4:-180}" i p online
  for i in $(seq 1 "$limit"); do
    # shellcheck disable=SC2086
    p="$(cluster_primary_for $nodes_list 2> /dev/null || true)"
    if [ -n "$p" ]; then
      online="$(rq "$p" "SELECT COUNT(*) FROM performance_schema.replication_group_members WHERE MEMBER_STATE='ONLINE'" || echo 0)"
      [ "$online" = "$expected" ] && { log "$c: $expected members ONLINE, primary $p (${i}s)"; return 0; }
    fi
    sleep 1
  done
  return 1
}

wait_clusterset_ok() {
  local p="$1" limit="${2:-120}" i ok
  for i in $(seq 1 "$limit"); do
    ok="$(runner_setup_js "$MF_ADMIN_USER" "$(cname "$p")" <<'JS' 2> /dev/null | grep '^MFCS_OK=' | tail -1 || true
var s = dba.getClusterSet().status({extended: 1});
var a = s.clusters && s.clusters.csa;
var b = s.clusters && s.clusters.csb;
var good = !!(a && b && a.clusterRole === "PRIMARY" && b.clusterRole === "REPLICA" && a.globalStatus === "OK" && b.globalStatus === "OK");
print("MFCS_OK=" + (good ? "1" : "0") + "\n");
JS
)"
    [ "$ok" = "MFCS_OK=1" ] && { log "clusterset $CLUSTERSET_NAME: csa PRIMARY OK, csb REPLICA OK (${i}s)"; return 0; }
    sleep 2
  done
  return 1
}

# --- ClusterSet formation and recovery -------------------------------------
clusterset_fresh_create() {
  log "configuring instances and creating ClusterSet $CLUSTERSET_NAME"
  # Configure a1 with clusterAdmin, create mf_read on a1, form csa and mfcs.
  runner_setup_js root mf-cs-a1 <<'JS' || die "failed to configure a1"
dba.configureInstance(U("mf-cs-a1"), {
  clusterAdmin: "mf_admin",
  clusterAdminPassword: adminPw,
  restart: false
});
JS
  ensure_read_user a1 0

  runner_setup_js "$MF_ADMIN_USER" mf-cs-a1 <<'JS' || die "failed to create cluster csa / clusterset mfcs"
var csa = dba.createCluster("csa");
var cs = csa.createClusterSet("mfcs");
JS

  # Configure a2, b1, b2 with clusterAdmin too so UA() connects before clone.
  runner_setup_js root mf-cs-a1 <<'JS' || die "failed to configure a2/b1/b2"
["mf-cs-a2", "mf-cs-b1", "mf-cs-b2"].forEach(function (h) {
  dba.configureInstance(U(h), {
    clusterAdmin: "mf_admin",
    clusterAdminPassword: adminPw,
    restart: false
  });
});
JS

  log "csa: adding mf-cs-a2 (clone)"
  runner_setup_js "$MF_ADMIN_USER" mf-cs-a1 <<'JS' || die "failed to add mf-cs-a2 to csa"
var csa = dba.getCluster("csa");
csa.addInstance(UA("mf-cs-a2"), {recoveryMethod: "clone"});
JS
  wait_cluster_online "$PRIMARY_CLUSTER" "a1 a2" 2 180 || die "csa members not ONLINE"

  log "csb: creating replica cluster on mf-cs-b1 (clone)"
  runner_setup_js "$MF_ADMIN_USER" mf-cs-a1 <<'JS' || die "failed to create replica cluster csb"
var cs = dba.getClusterSet();
cs.createReplicaCluster("mf-cs-b1:3306", "csb", {recoveryMethod: "clone"});
JS
  wait_cluster_online "$REPLICA_CLUSTER" "b1" 1 180 || die "csb primary mf-cs-b1 not ONLINE"

  log "csb: adding mf-cs-b2 (clone)"
  runner_setup_js "$MF_ADMIN_USER" mf-cs-b1 <<'JS' || die "failed to add mf-cs-b2 to csb"
var csb = dba.getCluster("csb");
csb.addInstance(UA("mf-cs-b2"), {recoveryMethod: "clone"});
JS
  wait_cluster_online "$REPLICA_CLUSTER" "b1 b2" 2 180 || die "csb members not ONLINE"

  ensure_spare_c1
}

clusterset_reconcile() {
  # Ensure primary cluster csa has quorum and all members ONLINE.
  local pa pb
  pa="$(cluster_primary_for a1 a2 || true)"
  if [ -z "$pa" ]; then
    log "csa: no member ONLINE; rebooting from complete outage"
    runner_setup_js "$MF_ADMIN_USER" mf-cs-a1 <<'JS' || die "csa: rebootClusterFromCompleteOutage failed"
dba.rebootClusterFromCompleteOutage("csa");
JS
    pa="$(cluster_primary_for a1 a2)" || die "csa: no primary after reboot"
  fi
  runner_setup_js "$MF_ADMIN_USER" "$(cname "$pa")" <<'JS' || log "WARN csa: member reconcile reported an error (checked below)"
var c = dba.getCluster();
var topo = c.status().defaultReplicaSet.topology;
["mf-cs-a1", "mf-cs-a2"].forEach(function (h) {
  var m = topo[h + ":3306"];
  if (!m) { c.addInstance(UA(h), {recoveryMethod: "clone"}); }
  else if (m.status !== "ONLINE") { c.rejoinInstance(UA(h)); }
});
JS
  wait_cluster_online "$PRIMARY_CLUSTER" "a1 a2" 2 180 || die "csa: members not ONLINE"

  # Ensure replica cluster csb has quorum and all members ONLINE.
  pb="$(cluster_primary_for b1 b2 || true)"
  if [ -z "$pb" ]; then
    log "csb: no member ONLINE; rebooting from complete outage"
    runner_setup_js "$MF_ADMIN_USER" mf-cs-b1 <<'JS' || die "csb: rebootClusterFromCompleteOutage failed"
dba.rebootClusterFromCompleteOutage("csb");
JS
    pb="$(cluster_primary_for b1 b2)" || die "csb: no primary after reboot"
  fi
  runner_setup_js "$MF_ADMIN_USER" "$(cname "$pb")" <<'JS' || log "WARN csb: member reconcile reported an error (checked below)"
var c = dba.getCluster();
var topo = c.status().defaultReplicaSet.topology;
["mf-cs-b1", "mf-cs-b2"].forEach(function (h) {
  var m = topo[h + ":3306"];
  if (!m) { c.addInstance(UA(h), {recoveryMethod: "clone"}); }
  else if (m.status !== "ONLINE") { c.rejoinInstance(UA(h)); }
});
JS
  wait_cluster_online "$REPLICA_CLUSTER" "b1 b2" 2 180 || die "csb: members not ONLINE"

  # Ensure csb is rejoined in ClusterSet if it became invalidated.
  pa="$(cluster_primary_for a1 a2)" || die "csa: no primary"
  runner_setup_js "$MF_ADMIN_USER" "$(cname "$pa")" <<'JS' || log "WARN csb: ClusterSet rejoin check reported an error (checked below)"
var cs = dba.getClusterSet();
var st = cs.status({extended: 1});
if (st.clusters && st.clusters.csb && st.clusters.csb.status === "INVALIDATED") {
  cs.rejoinCluster("csb");
}
JS

  ensure_spare_c1
}

# --- router ----------------------------------------------------------------
router_route() {
  local mode="$1" p=6446
  [ "$mode" = RO ] && p=6447
  MYSQL_PWD="$MF_READ_PASSWORD" docker exec -e MYSQL_PWD "$(cname a1)" mysql -u"$MF_READ_USER" -h"$(cname router)" -P"$p" --protocol=TCP \
      --batch --skip-column-names -e "SELECT @@hostname" 2> /dev/null || \
  MYSQL_PWD="$MF_READ_PASSWORD" docker exec -e MYSQL_PWD "$(cname b1)" mysql -u"$MF_READ_USER" -h"$(cname router)" -P"$p" --protocol=TCP \
      --batch --skip-column-names -e "SELECT @@hostname" 2> /dev/null
}

wait_router() {
  local limit="${1:-60}" i rw ro
  for i in $(seq 1 "$limit"); do
    rw="$(router_route RW || true)"
    ro="$(router_route RO || true)"
    if [ -n "$rw" ] && [ -n "$ro" ]; then
      log "router: RW -> $rw, RO -> $ro (${i}s)"
      return 0
    fi
    sleep 1
  done
  log "router: ports did not answer within ${limit}s"
  return 1
}

router_up() {
  local img p router_cn
  router_cn="$(cname router)"
  if exists router; then
    is_up router || docker start "$router_cn" > /dev/null
    if docker exec "$router_cn" test -f /router/run/mysqlrouter.conf; then
      wait_router 60
      return $?
    fi
    log "router: not bootstrapped; bootstrapping again"
  else
    img="$(router_image)" || return 1
    docker volume create "$(volume router)" > /dev/null
    log "router: starting ($img on 127.0.0.1:$PORT_router_rw RW / $PORT_router_ro RO)"
    docker run -d --name "$router_cn" --hostname "$router_cn" --network "$NET" \
      -p "127.0.0.1:${PORT_router_rw}:6446" \
      -p "127.0.0.1:${PORT_router_ro}:6447" \
      -v "$(volume router):/router" \
      --restart unless-stopped \
      --label myflames-cluster=clusterset \
      --entrypoint sh "$img" -c \
      'until [ -f /router/run/mysqlrouter.conf ]; do sleep 1; done; exec mysqlrouter -c /router/run/mysqlrouter.conf' > /dev/null
  fi
  p="$(clusterset_primary_node)" || die "no ClusterSet primary for router bootstrap"
  log "router: bootstrapping against $(cname "$p") (ClusterSet $CLUSTERSET_NAME)"
  mkdir -p "$LOGS"
  printf '%s\n' "$MF_ADMIN_PASSWORD" \
    | docker exec -i "$router_cn" mysqlrouter --bootstrap "${MF_ADMIN_USER}@$(cname "$p"):3306" \
        --directory /router/run --conf-bind-address=0.0.0.0 --conf-target-cluster=primary \
        --user=mysql --force > "$LOGS/router-bootstrap.log" 2>&1 \
    || { log "router bootstrap failed: $(tail -10 "$LOGS/router-bootstrap.log")"; return 1; }
  wait_router 60
}

# --- subcommands -----------------------------------------------------------
cmd_up() {
  command -v docker > /dev/null || die "docker is required"
  command -v openssl > /dev/null || die "openssl is required"
  command -v python3 > /dev/null || die "python3 is required"

  mkdir -p "$LAB" "$STATE" "$LOGS"
  load_creds
  resolve_ports
  ensure_images
  ensure_network

  local n
  for n in $SERVER_NODES; do
    run_mysql_node "$n"
  done
  for n in $SERVER_NODES; do
    wait_ready "$n" 300 || die "$n did not start"
  done

  if has_metadata a1 || has_metadata a2 || has_metadata b1 || has_metadata b2; then
    clusterset_reconcile
  else
    clusterset_fresh_create
  fi

  local p
  p="$(clusterset_primary_node)" || die "no ClusterSet primary"
  wait_clusterset_ok "$p" 120 || die "ClusterSet $CLUSTERSET_NAME did not reach OK status"

  router_up || die "router failed to bootstrap or route against ClusterSet $CLUSTERSET_NAME"
  cmd_status
}

cmd_status() {
  if [ ! -f "$CREDS_FILE" ]; then
    log "no credentials at $CREDS_FILE (lab is down)"
    return 1
  fi
  load_creds
  resolve_ports
  local n v detail port
  printf '%-8s %-14s %-8s %-11s %-12s %s\n' NAME CONTAINER STATE PORT VERSION DETAIL
  for n in $ALL_NODES; do
    port="$(port_of "$n")"
    if [ "$n" = router ]; then
      port="${PORT_router_rw}/${PORT_router_ro}"
    fi
    if ! is_up "$n"; then
      printf '%-8s %-14s %-8s %-11s %-12s %s\n' "$n" "$(cname "$n")" down "${port:--}" - -
      continue
    fi
    case "$n" in
      router)
        v="mysqlrouter"
        detail="RW -> $(router_route RW || echo none), RO -> $(router_route RO || echo none)"
        ;;
      c1)
        v="$(rq "$n" "SELECT VERSION()" || true)"
        detail="spare (standalone)"
        ;;
      a*)
        v="$(rq "$n" "SELECT VERSION()" || true)"
        detail="$PRIMARY_CLUSTER $(node_gr_line "$n")"
        ;;
      b*)
        v="$(rq "$n" "SELECT VERSION()" || true)"
        detail="$REPLICA_CLUSTER $(node_gr_line "$n")"
        ;;
    esac
    printf '%-8s %-14s %-8s %-11s %-12s %s\n' "$n" "$(cname "$n")" running "${port:--}" "${v:-?}" "$detail"
  done

  local p
  p="$(clusterset_primary_node 2> /dev/null || true)"
  echo
  if [ -z "$p" ] || ! is_up "$p"; then
    echo "ClusterSet status: unavailable (no running cluster member)"
    return 1
  fi
  echo "ClusterSet status (dba.getClusterSet().status({extended:1}) and listRouters() via $(cname "$p")):"
  runner_sh "$MF_ADMIN_USER" "$(cname "$p")" <<'JS'
var cs = dba.getClusterSet();
print(JSON.stringify(cs.status({extended: 1}), null, 2) + "\n");
print("ClusterSet routers (dba.getClusterSet().listRouters()):\n");
print(JSON.stringify(cs.listRouters(), null, 2) + "\n");
JS
}

cmd_shell() {
  load_creds
  local target="" js="" has_arg=0
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --target)
        [ "$#" -ge 2 ] || die "--target requires a node name or container host"
        target="$2"
        shift 2
        ;;
      --)
        shift
        break
        ;;
      -*)
        [ "$1" = "-" ] && { has_arg=1; js="-"; shift; break; }
        die "unknown option '$1' (expected: shell [--target NODE] '<js>')"
        ;;
      *)
        if [ "$has_arg" = 0 ]; then
          js="$1"
          has_arg=1
          shift
        else
          die "unexpected extra argument '$1'"
        fi
        ;;
    esac
  done
  if [ "$#" -gt 0 ] && [ "$has_arg" = 0 ]; then
    js="$1"
    has_arg=1
  fi

  local host=""
  if [ -n "$target" ]; then
    case "$target" in
      mf-cs-*) host="$target" ;;
      *) host="$(cname "$target")" ;;
    esac
  else
    local p
    p="$(clusterset_primary_node 2> /dev/null || true)"
    [ -n "$p" ] || die "no running mf-cs-* MySQL container to connect to"
    host="$(cname "$p")"
  fi

  if [ "$has_arg" = 1 ] && [ "$js" != "-" ]; then
    printf '%s\n' "$js" | runner_sh "$MF_ADMIN_USER" "$host"
  else
    runner_sh "$MF_ADMIN_USER" "$host"
  fi
}

cmd_down() {
  local c v net f
  for c in $(docker ps -a --format '{{.Names}}' | grep '^mf-cs-' || true); do
    docker rm -fv "$c" > /dev/null 2>&1 && log "removed container $c" || true
  done
  for v in $(docker volume ls --format '{{.Name}}' | grep '^mf-cs-' || true); do
    docker volume rm "$v" > /dev/null 2>&1 && log "removed volume $v" || true
  done
  for net in $(docker network ls --format '{{.Name}}' | grep '^mf-cs-' || true); do
    docker network rm "$net" > /dev/null 2>&1 && log "removed network $net" || true
  done
  for f in "$PORTS_FILE" "$STATE"/* "$LOGS"/* "$CREDS_FILE"; do
    if [ -e "$f" ]; then
      rm "$f"
    fi
  done
  rmdir "$STATE" "$LOGS" "$LAB" 2> /dev/null || true
}

cmd_reset() {
  cmd_down
  cmd_up
}

main() {
  local cmd="${1:-}"
  [ "$#" -gt 0 ] && shift
  case "$cmd" in
    up) cmd_up "$@" ;;
    status) cmd_status ;;
    shell) cmd_shell "$@" ;;
    down) cmd_down ;;
    reset) cmd_reset ;;
    *) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; [ -z "$cmd" ] || exit 2 ;;
  esac
}

main "$@"
