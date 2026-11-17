#!/usr/bin/env bash
# scripts/live-tests.sh
#
# Run the live lab suites (test/live/) against the Docker cluster labs, the
# way mtr runs its suites: repeatable, one line per test, a summary with
# durations, non-zero on any failure.
#
# Usage:
#   scripts/live-tests.sh [ic|galera|proxysql|shell|all] [--record]
#
# Brings up only the labs the chosen suites need (idempotent
# cluster-matrix.sh up, then check) and leaves them running for the next run.
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
# credentials.env there stays 0600 and is never printed). Suites always use a
# throwaway HOME with MYFLAMES_SECRET_BACKEND=vault, never the real
# ~/.myflames. Requires MYFLAMES_LIVE_LABS=1 semantics: this script sets it
# for the suites it starts.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
cd "$REPO"

usage() { sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; }

selection="all"
record=0
for arg in "$@"; do
  case "$arg" in
    ic|galera|proxysql|shell|all) selection="$arg" ;;
    --record) record=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
done

export CLUSTER_LAB="${CLUSTER_LAB:-$REPO/local/cluster-matrix}"
CREDS="$CLUSTER_LAB/credentials.env"
[ -r "$CREDS" ] || { echo "no lab credentials at $CREDS (set CLUSTER_LAB)"; exit 2; }
export MYFLAMES_LIVE_LABS=1

# Labs and lanes per selection. The ic lane also carries the suites that share
# the InnoDB Cluster (MySQL Shell rejoin, Query editor through ProxySQL);
# the galera lane mutates only mf-galera-*. Lanes run in parallel, suites in
# a lane in order.
labs=""
ic_lane=""
galera_lane=""
case "$selection" in
  ic)       labs="ic";                  ic_lane="live_ic_group_routing" ;;
  galera)   labs="galera";              galera_lane="live_galera" ;;
  proxysql) labs="ic galera proxysql";  ic_lane="live_editor_proxysql" ;;
  shell)    labs="ic";                  ic_lane="live_shell" ;;
  all)      labs="ic galera proxysql"
            ic_lane="live_ic_group_routing live_shell live_editor_proxysql"
            galera_lane="live_galera" ;;
esac

started=$(python3 -c 'import time; print(time.time())')

# Bring up only the labs the chosen suites need; labs already healthy stay
# untouched, so a second run never restarts them.
need_up=""
for lab in $labs; do
  bash scripts/cluster-matrix.sh check "$lab" > /dev/null 2>&1 || need_up="$need_up $lab"
done
for lab in $need_up; do
  bash scripts/cluster-matrix.sh up "$lab"
done
bash scripts/cluster-matrix.sh check $labs

tmp=$(mktemp -d)
trap 'rm -r "$tmp" 2>/dev/null || true' EXIT

# The evidence record is the only file a run writes under the repository.
# cluster-matrix.sh profiles smokes each saved connection into
# $CLUSTER_LAB/state/smoke, so the lanes and the evidence env read work
# against a shadow lab dir in the throwaway tmp whose only content is a
# symlink to the real credentials; the labs themselves live in docker, so
# check and the suites read the same servers through the shadow.
shadow="$tmp/cluster-lab"
mkdir -p "$shadow"
ln -s "$CREDS" "$shadow/credentials.env"
export CLUSTER_LAB="$shadow"

run_lane() {  # NAME MODULES...
  local name="$1"; shift
  local lane_rc=0
  MYFLAMES_LIVE_HOME="$tmp/home-$name" \
    python3 test/live/_runner.py --json-out "$tmp/$name.json" "$@" \
    > "$tmp/$name.log" 2>&1 || lane_rc=$?
  echo "$lane_rc" > "$tmp/$name.rc"
}

pids=""
[ -n "$ic_lane" ]     && { run_lane ic $ic_lane &         pids="$pids $!"; }
[ -n "$galera_lane" ] && { run_lane galera $galera_lane & pids="$pids $!"; }
rc=0
for pid in $pids; do wait "$pid" || rc=1; done

for lane in ic galera; do
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
record_labs=""
for lab in $labs; do
  case "$lab" in ic|galera|proxysql) record_labs="$record_labs $lab" ;; esac
done
if [ "$record" = 1 ]; then
  MYFLAMES_LIVE_HOME="$tmp/home-expected" \
    python3 test/live/_expected.py record $record_labs || rc=1
elif ls test/live/expected/*.json > /dev/null 2>&1; then
  MYFLAMES_LIVE_HOME="$tmp/home-expected" \
    python3 test/live/_runner.py --json-out "$tmp/expected.json" \
    live_expected > "$tmp/expected.log" 2>&1 || rc=1
  grep -E '^(PASS|FAIL|ERROR|SKIP) ' "$tmp/expected.log" || true
  [ -f "$tmp/expected.log" ] && [ "$rc" != 0 ] && { echo "--- expected output ---"; cat "$tmp/expected.log"; }
fi

# Evidence record for the release verification.
mkdir -p local/live-evidence
bash scripts/cluster-matrix.sh env --home "$tmp/home-env" > "$tmp/env.json" 2>/dev/null || echo '{}' > "$tmp/env.json"
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
