#!/usr/bin/env bash
# scripts/live-tests.sh
#
# Run the live lab suites (test/live/) against the Docker cluster labs, the
# way mtr runs its suites: repeatable, one line per test, a summary with
# durations, non-zero on any failure.
#
# Usage:
#   scripts/live-tests.sh [ic|galera|pxc|proxysql|shell|writes|topology|ga|all] [--record]
#
# Brings up only the labs the chosen suites need (idempotent
# cluster-matrix.sh up, then check) and leaves them running for the next run.
# The ga selection brings up nothing: it checks scripts/ga-matrix.sh status
# and runs live_writes_ga (reviewed writes on every GA server) against the GA
# matrix servers that are already running, in its own lane with its own
# throwaway home (the profiles ga-matrix.sh profiles saves there). In all,
# the ga lane is included only when the GA matrix is up.
# For the standalone InnoDB ClusterSet lab (domain mfcs, clusters csa and
# csb, spare instance mf-cs-c1, Router on 34606/34607), use
# scripts/clusterset-matrix.sh (up | status | shell '<js>' | down | reset).
# Different clusters run in parallel lanes; the scenarios on one cluster run
# in order. Every run writes an evidence record to
# local/live-evidence/<commit>.json (full commit SHA, a -dirty suffix when the
# checkout is dirty, "worktree" when it is not a git checkout; inside: commit,
# dirty flag, date, the run's wall time, lab server and MySQL Shell versions,
# each test with outcome and duration) for the release verification record.
# That evidence file is the only file a run writes under the repository: the
# lanes work against a throwaway shadow of CLUSTER_LAB, so the profiles smoke
# output lands in the run's tmp dir, not in local/cluster-matrix/state.
#
# --record saves the normalized JSON of the read-only Cluster, Replication and
# ProxySQL reports per server version under test/live/expected/ (volatile
# fields masked); a normal run compares against them when they exist
# (mtr's .result idea, for JSON).
#
# Labs and credentials come from CLUSTER_LAB (default local/cluster-matrix;
# credentials.env there stays 0600 and is never printed). The ga lane's
# servers and credentials come from the GA matrix in GA_LAB (default
# local/ga-matrix), with credentials from GA_CREDS_FILE (default
# $GA_LAB/credentials.env, also 0600 and never printed). The GA credentials
# variable is GA-only: cluster-matrix.sh reads CREDS_FILE too, so the ga
# lane hands its file to ga-matrix.sh as CREDS_FILE in the ga-matrix.sh
# environment only. Suites always use a throwaway HOME with
# MYFLAMES_SECRET_BACKEND=vault, never the real ~/.myflames. Requires
# MYFLAMES_LIVE_LABS=1 semantics: this script sets it for the suites it
# starts.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$REPO"

usage() { sed -n '2,46p' "$0" | sed 's/^# \{0,1\}//'; }

selection="all"
record=0
for arg in "$@"; do
  case "$arg" in
    ic|galera|pxc|proxysql|shell|writes|topology|ga|all) selection="$arg" ;;
    --record) record=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

export CLUSTER_LAB="${CLUSTER_LAB:-$REPO/local/cluster-matrix}"
CREDS="$CLUSTER_LAB/credentials.env"
# The GA matrix lives in GA_LAB; its credentials file has its own variable
# so a value exported for the ga lane never reaches cluster-matrix.sh.
export GA_LAB="${GA_LAB:-$REPO/local/ga-matrix}"
export GA_CREDS_FILE="${GA_CREDS_FILE:-$GA_LAB/credentials.env}"
export MYFLAMES_LIVE_LABS=1

# The GA matrix check for the ga lane: any live_writes_ga server running.
# Read-only: without the matrix credentials file it reports "not up" so
# ga-matrix.sh status never gets the chance to create a fresh one. The file
# goes to ga-matrix.sh as its own CREDS_FILE in its environment only.
ga_up() {
  [ -r "$GA_CREDS_FILE" ] || return 1
  CREDS_FILE="$GA_CREDS_FILE" bash scripts/ga-matrix.sh status 2>/dev/null | \
    awk '$3 == "running" && $1 ~ /^(mysql-5.7|mysql-8.0|mysql-8.4|mysql-9.7|percona-8.0|percona-8.4|mariadb-10.11|mariadb-11.4|mariadb-11.8)$/ { found=1 } END { exit !found }'
}

# Labs and lanes per selection, with the labs each lane may rely on (the lane's
# profile smoke checks are restricted to them through MYFLAMES_LIVE_LABS_ONLY).
# The ic lane also carries the suites that share
# the InnoDB Cluster (MySQL Shell rejoin, Query editor through ProxySQL);
# the galera lane mutates only mf-galera-*, the pxc lane only mf-pxc-*. Lanes
# run in parallel, suites in a lane in order. The writes suite (reviewed writes
# on one member of each cluster) runs only after every lane is done, so it
# never runs while another suite changes the same cluster's membership. The
# ga lane (reviewed writes on the GA matrix servers) brings up nothing:
# ga-matrix.sh manages those servers, so `all` includes it only when the
# matrix is already up.
labs=""
ic_lane=""
galera_lane=""
pxc_lane=""
writes_lane=""
ga_lane=""
ic_labs=""
galera_labs=""
pxc_labs=""
writes_labs=""
case "$selection" in
  ic)       labs="ic";                  ic_lane="live_ic_group_routing live_topology_ic"; ic_labs="ic" ;;
  galera)   labs="galera";              galera_lane="live_galera live_topology_galera"; galera_labs="galera" ;;
  pxc)      labs="pxc";                 pxc_lane="live_pxc live_topology_pxc"; pxc_labs="pxc" ;;
  proxysql) labs="ic galera proxysql";  ic_lane="live_editor_proxysql"; ic_labs="ic proxysql" ;;
  shell)    labs="ic";                  ic_lane="live_shell"; ic_labs="ic" ;;
  writes)   labs="ic galera";           writes_lane="live_writes"; writes_labs="ic galera" ;;
  topology) labs="ic galera pxc proxysql"
            ic_lane="live_topology_ic"; ic_labs="ic"
            galera_lane="live_topology_galera"; galera_labs="galera"
            pxc_lane="live_topology_pxc"; pxc_labs="pxc" ;;
  ga)       labs="";                    ga_lane="live_writes_ga" ;;
  all)      labs="ic galera pxc proxysql"
            ic_lane="live_ic_group_routing live_topology_ic live_shell live_editor_proxysql"
            ic_labs="ic proxysql"
            galera_lane="live_galera live_topology_galera"; galera_labs="galera"
            pxc_lane="live_pxc live_topology_pxc"; pxc_labs="pxc"
            writes_lane="live_writes"; writes_labs="ic galera"
            ga_up && ga_lane="live_writes_ga" ;;
esac

[ -z "$labs" ] || [ -r "$CREDS" ] || { echo "no lab credentials at $CREDS (set CLUSTER_LAB)"; exit 2; }
if [ -n "$ga_lane" ] && ! ga_up; then
  echo "note: ga-matrix.sh status shows no GA server running; live_writes_ga will skip every server" >&2
fi

started=$(python3 -c 'import time; print(time.time())')

# Bring up only the labs the chosen suites need; labs already healthy stay
# untouched, so a second run never restarts them. The ga selection has no
# cluster labs, so nothing is brought up or checked.
need_up=""
for lab in $labs; do
  bash scripts/cluster-matrix.sh check "$lab" > /dev/null 2>&1 || need_up="$need_up $lab"
done
for lab in $need_up; do
  bash scripts/cluster-matrix.sh up "$lab"
done
if [ -n "$labs" ]; then
  bash scripts/cluster-matrix.sh check $labs
fi

tmp=$(mktemp -d)
trap 'rm -r "$tmp" 2>/dev/null || true' EXIT

# The evidence record is the only file a run writes under the repository.
# cluster-matrix.sh profiles smokes each saved connection into
# $CLUSTER_LAB/state/smoke, so each lane and the evidence env read work against
# its own shadow lab dir in the throwaway tmp whose only content is a symlink
# to the real credentials; the labs themselves live in docker, so check and
# the suites read the same servers through the shadow. Per-lane shadows keep
# one lane's smoke results from being rewritten by another lane running
# profiles at the same time. The ga selection has no cluster lab, so no shadow.
shadow="$tmp/cluster-lab"
make_shadow() {  # NAME: a shadow lab dir whose only content is the credentials symlink
  mkdir -p "$tmp/cluster-lab-$1"
  ln -s "$CREDS" "$tmp/cluster-lab-$1/credentials.env"
}
if [ -n "$labs" ]; then
  mkdir -p "$shadow"
  ln -s "$CREDS" "$shadow/credentials.env"
  export CLUSTER_LAB="$shadow"
fi

run_lane() {  # NAME LABS MODULES...
  local name="$1" lane_labs="$2"; shift 2
  local lane_rc=0
  [ -z "$lane_labs" ] || make_shadow "$name"
  (
    # The lane gets its own shadow state dir and the labs it may rely on, so a
    # sick lab outside the selection never fails this lane's profile smoke.
    if [ -n "$lane_labs" ]; then
      export CLUSTER_LAB="$tmp/cluster-lab-$name" MYFLAMES_LIVE_LABS_ONLY="$lane_labs"
    fi
    MYFLAMES_LIVE_HOME="$tmp/home-$name" \
      python3 test/live/_runner.py --json-out "$tmp/$name.json" "$@"
  ) > "$tmp/$name.log" 2>&1 || lane_rc=$?
  echo "$lane_rc" > "$tmp/$name.rc"
}

pids=""
[ -n "$ic_lane" ]     && { run_lane ic "$ic_labs" $ic_lane &         pids="$pids $!"; }
[ -n "$galera_lane" ] && { run_lane galera "$galera_labs" $galera_lane & pids="$pids $!"; }
# The pxc lane touches only mf-pxc-*, so it runs in parallel with the others.
[ -n "$pxc_lane" ]    && { run_lane pxc "$pxc_labs" $pxc_lane &       pids="$pids $!"; }
# The ga lane writes only to GA matrix servers, which no cluster suite
# touches, so it can run in parallel with the cluster lanes.
[ -n "$ga_lane" ]     && { run_lane ga "" $ga_lane &                  pids="$pids $!"; }
rc=0
for pid in $pids; do wait "$pid" || rc=1; done

# Writes to one member of each cluster run only once the membership-changing
# suites on both clusters are done.
[ -n "$writes_lane" ] && run_lane writes "$writes_labs" $writes_lane

for lane in ic galera pxc writes ga; do
  [ -f "$tmp/$lane.log" ] || continue
  grep -E '^(PASS|FAIL|ERROR|SKIP) ' "$tmp/$lane.log" || true
  [ "$(cat "$tmp/$lane.rc" 2>/dev/null || echo 1)" = "0" ] || {
    rc=1
    echo "--- lane $lane output ---"
    cat "$tmp/$lane.log"
  }
done

# The recorded read-only reports (mtr's .result idea). Record after the lanes
# so the clusters are back at rest; compare likewise, once both lanes are done.
# Only ic, galera and proxysql have recorded reports. A selection without any
# of them (ga, pxc) never runs this lane: the recorded reports need those labs
# (an empty record list would record the defaults). The lane gets its own
# shadow state dir and only compares the reports of the selected labs
# (MYFLAMES_LIVE_LABS_ONLY): a run that selected galera alone never checks the
# ic or proxysql reports.
record_labs=""
for lab in $labs; do
  case "$lab" in ic|galera|proxysql) record_labs="$record_labs $lab" ;; esac
done
record_labs="${record_labs# }"
if [ -z "$record_labs" ]; then
  :
else
  make_shadow expected
  if [ "$record" = 1 ]; then
    CLUSTER_LAB="$tmp/cluster-lab-expected" MYFLAMES_LIVE_LABS_ONLY="$record_labs" \
      MYFLAMES_LIVE_HOME="$tmp/home-expected" \
      python3 test/live/_expected.py record $record_labs || rc=1
  elif ls test/live/expected/*.json > /dev/null 2>&1; then
    CLUSTER_LAB="$tmp/cluster-lab-expected" MYFLAMES_LIVE_LABS_ONLY="$record_labs" \
      MYFLAMES_LIVE_HOME="$tmp/home-expected" \
      python3 test/live/_runner.py --json-out "$tmp/expected.json" \
      live_expected > "$tmp/expected.log" 2>&1 || rc=1
    grep -E '^(PASS|FAIL|ERROR|SKIP) ' "$tmp/expected.log" || true
    [ -f "$tmp/expected.log" ] && [ "$rc" != 0 ] && { echo "--- expected output ---"; cat "$tmp/expected.log"; }
  fi
fi

# Evidence record for the release verification.
mkdir -p local/live-evidence
if [ -n "$labs" ]; then
  bash scripts/cluster-matrix.sh env --home "$tmp/home-env" > "$tmp/env.json" 2>/dev/null || echo '{}' > "$tmp/env.json"
else
  echo '{}' > "$tmp/env.json"
fi
shell_version=""
case " $labs " in *" ic "*)
  if docker image inspect myflames-lab-runner:8.4 > /dev/null 2>&1; then
    shell_version=$(docker run --rm myflames-lab-runner:8.4 mysqlsh --version 2>/dev/null | head -1 || true)
  fi ;;
esac
EVIDENCE_TMP="$tmp" SHELL_VERSION="$shell_version" STARTED="$started" python3 - <<'PY'
import datetime
import glob
import json
import os
import subprocess
import time

tmp = os.environ["EVIDENCE_TMP"]
repo = os.getcwd()
try:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True, cwd=repo).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                                text=True, check=True, cwd=repo).stdout.strip())
except (subprocess.CalledProcessError, OSError):
    commit, dirty = "worktree", True
name = commit if commit == "worktree" else commit + ("-dirty" if dirty else "")
with open(os.path.join(tmp, "env.json"), encoding="utf-8") as handle:
    try:
        env = json.load(handle)
    except ValueError:
        env = {}
labs = {}
for cluster, data in (env.get("clusters") or {}).items():
    labs[cluster] = [{"node": n.get("name"), "version": n.get("version")}
                     for n in data.get("nodes") or ()]
if env.get("proxysql"):
    labs["proxysql"] = {"version": env["proxysql"].get("version")}
if os.environ.get("SHELL_VERSION"):
    labs["mysql_shell"] = {"version": os.environ["SHELL_VERSION"]}
tests = []
for path in sorted(glob.glob(os.path.join(tmp, "*.json"))):
    if os.path.basename(path) == "env.json":
        continue
    try:
        with open(path, encoding="utf-8") as handle:
            summary = json.load(handle)
    except ValueError:
        continue
    for module, data in sorted((summary.get("modules") or {}).items()):
        for test in data.get("tests") or []:
            tests.append({"module": module, "id": test.get("id"),
                          "outcome": test.get("outcome"), "seconds": test.get("seconds")})
started = float(os.environ.get("STARTED") or 0)
record = {"commit": commit, "dirty": dirty,
          "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "wall_seconds": round(time.time() - started, 1) if started else None,
          "labs": labs, "tests": tests}
target = os.path.join(repo, "local", "live-evidence", "%s.json" % name)
temporary = target + ".part"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
os.replace(temporary, target)
print("evidence: %s" % os.path.relpath(target, repo))
PY

ended=$(python3 -c 'import time; print(time.time())')
python3 - "$started" "$ended" "$rc" <<'PY'
import sys
started, ended, rc = float(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3])
print("live-tests: %s in %.1fs" % ("FAILED" if rc else "OK", ended - started))
PY
exit "$rc"
