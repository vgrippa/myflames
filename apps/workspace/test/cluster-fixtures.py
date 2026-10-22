"""Write the cluster-1.0 reports the Cluster browser suite replays (cluster.mjs).

Every file is computed by the real backend (myflames/cluster.py, with the
member actions the workspace endpoint adds: cluster.with_actions) over the
answers in test/_cluster_fixtures.py: the live recordings of
test/fixtures/cluster (MariaDB 11.8.9 Galera, Percona XtraDB Cluster 8.4.11,
MySQL 8.4.11 InnoDB Cluster, 2026-10-20), the states derived from them, and
the synthetic servers. Never written by hand. ``captured_at`` is fixed so the
files are stable.

Run from the repository root after the backend changes its output:
    python3 apps/workspace/test/cluster-fixtures.py
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test"))

import _cluster_fixtures as F  # noqa: E402
from myflames.cluster import with_actions  # noqa: E402

NAMES = ("galera-11.8-healthy", "galera-11.8-donor", "galera-11.8-two-of-three", "galera-11.8-non-primary",
         "pxc-8.4-healthy", "pxc-8.4-donor", "pxc-8.4-member-donor", "ic-8.4-healthy", "ic-8.4-degraded",
         "ic-8.4-unreachable", "ic-8.4-no-quorum", "ic-8.4-member-offline", "ic-8.4-local-offline",
         "gr-8.4-multi-primary", "gr-8.4-denied", "mysql-8.4-standalone", "mariadb-11.8-standalone",
         "pxc-8.4-two-of-three", "galera-11.8-two-nodes", "galera-11.8-partial-seeds", "galera-mysql-8.4-healthy",
         "galera-11.8-wsrep-off", "ic-8.4-read-replica", "ic-8.4-read-replica-local")
OUT = os.path.join(ROOT, "apps", "workspace", "test", "fixtures")


def main():
    for name in NAMES:
        report = with_actions(F.report(name))
        report["captured_at"] = "2026-10-20T14:40:00+00:00"
        path = os.path.join(OUT, "cluster-%s.json" % name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
        print(path, report["kind"], report["status"])
    # One reviewed plan, as /api/live/cluster/plan answers it (policy and a fixed token).
    from myflames import dbops, reviewed_writes
    from myflames.cluster import plan_action
    report = F.report("ic-8.4-healthy")
    target = next(m for m in report["members"] if m["role"] == "SECONDARY" and m["state"] == "ONLINE")
    plan = plan_action(report, "switchover", target["key"])
    decision = dbops.decide(reviewed_writes.worst_kind(plan["statements"]), "production", False, "Node A")
    plan["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text", "connection_type", "read_only")}
    plan["review_token"] = "fixture-token-switchover"
    path = os.path.join(OUT, "cluster-plan-switchover.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(plan, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print(path)
    # The send of that plan, answered (cluster.execute_action over the
    # recording; the write goes to a recorder) and its progress while the
    # server works on it (reviewed_writes.Progress).
    import tempfile
    from myflames import cluster
    from test_cluster_actions import Writes
    reviewed = dict(plan)
    reviewed.pop("review_token")
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        done = cluster.execute_action(Writes(F.conn("ic-8.4-healthy")), reviewed)
    progress = reviewed_writes.Progress(len(plan["statements"]), cluster.ACTION_TIMEOUT)
    progress.set("sending", ["sent"] * len(plan["statements"]))
    sending = dict(progress.snapshot(), elapsed_s=1.0, waiting_s=0.3)
    for name, value in (("result-switchover", done), ("progress-sending", sending)):
        path = os.path.join(OUT, "cluster-%s.json" % name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=1, ensure_ascii=False)
            handle.write("\n")
        print(path)


if __name__ == "__main__":
    main()
