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
#: InnoDB ClusterSet servers (synthetic), with the actions MySQL Shell runs as
#: they are offered when it is installed (SHELL stands for it).
CLUSTERSET = ("ic-8.4-clusterset-primary", "ic-8.4-clusterset-replica", "ic-8.4-clusterset-replica-channel-off",
              "ic-8.4-clusterset-invalidated", "ic-8.4-read-replica-clusterset",
              "ic-8.4-clusterset-primary-fenced", "ic-8.4-clusterset-primary-no-quorum")
CLUSTERSET_VARIANTS = {"ic-8.4-clusterset-primary-fenced": "ic-8.4-clusterset-primary",
                       "ic-8.4-clusterset-primary-no-quorum": "ic-8.4-clusterset-primary"}
SHELL = {"path": "/usr/bin/mysqlsh", "realpath": "/usr/bin/mysqlsh", "version": "8.4.6", "version_tuple": [8, 4, 6]}
OUT = os.path.join(ROOT, "apps", "workspace", "test", "fixtures")

#: The group the ClusterSet fixtures are read with: the connection in use and
#: one saved route into the other cluster, so every fixture carries the new
#: per-cluster route, read, members, quorum, channel and status fields
#: (cluster.py _read_clusterset_routes). The peer is the other cluster of
#: each scenario (cluster-fixtures' synthetic servers).
GROUP = ({"id": "local", "name": "Local member", "group": "production", "host": "mf-ic-1", "port": 3306},
         {"id": "remote", "name": "DR member", "group": "production", "host": "mf-dr-1", "port": 3306})
CLUSTERSET_PEER = {
    "ic-8.4-clusterset-primary": (("cid-2", "mf-dr", 30), "ic-8.4-clusterset-primary"),
    "ic-8.4-clusterset-replica": (("cid-0", "mf-main", 20), "ic-8.4-clusterset-replica"),
    "ic-8.4-clusterset-replica-channel-off": (("cid-0", "mf-main", 20), "ic-8.4-clusterset-replica"),
    "ic-8.4-clusterset-invalidated": (("cid-2", "mf-dr", 30), "ic-8.4-clusterset-primary"),
    "ic-8.4-read-replica-clusterset": (("cid-2", "mf-dr", 30), "ic-8.4-clusterset-primary"),
}


def clusterset_source(name):
    """Derive live SQL answers, never the computed status words."""
    source = F.conn(CLUSTERSET_VARIANTS.get(name, name))
    # The synthetic normal primary keeps automatic SRO management enabled;
    # replica primaries have it disabled, as configured by MySQL Shell.
    source.scenario["tables"]["performance_schema.replication_group_member_actions"] = [
        [1 if source.scenario["variables"].get("super_read_only") == "OFF" else 0]]
    if name == "ic-8.4-clusterset-primary-fenced":
        # Shell fenceWrites disables automatic SRO management, then enables
        # SRO (modules/adminapi/cluster/cluster_impl.cc:1899-1910). Its fence
        # check requires both (2043-2067), and ClusterSet maps FENCED_WRITES
        # to OK_FENCED_WRITES (cluster_set/cluster_set_impl.cc:549-554).
        source.scenario["variables"]["super_read_only"] = "ON"
        # SELECT enabled ... WHERE name = 'mysql_disable_super_read_only_if_primary'
        # (mysqlshdk/libs/mysql/group_replication.cc:1268-1287).
        source.scenario["tables"]["performance_schema.replication_group_member_actions"] = [[0]]
        source.scenario["tables"]["performance_schema.replication_connection_status"] = []
    elif name == "ic-8.4-clusterset-primary-no-quorum":
        # One reachable member out of three: Shell returns NOT_OK for
        # NO_QUORUM (modules/adminapi/cluster_set/cluster_set_impl.cc:520-522).
        for row in source.scenario["tables"]["performance_schema.replication_group_members"][1:]:
            row[4] = "UNREACHABLE"
    return source


def clusterset_report(name):
    """The ClusterSet scenario read as the workspace reads a Group connection:
    the other cluster through its saved route."""
    from myflames.cluster import read_status
    peer_args, scenario = CLUSTERSET_PEER[CLUSTERSET_VARIANTS.get(name, name)]

    def opener(profile):
        peer = F.clusterset_peer(*peer_args, scenario_name=scenario)
        peer.scenario["tables"]["performance_schema.replication_group_member_actions"] = [
            [1 if peer.scenario["variables"]["super_read_only"] == "OFF" else 0]]
        return peer

    # Route discovery only contacts classified members or exact metadata
    # endpoints. Replica scenarios use mf-main, not the normal mf-dr peer.
    profiles = [dict(profile) for profile in GROUP]
    profiles[1]["host"] = peer_args[1] + "-1"
    source = clusterset_source(name)
    profiles[0]["host"] = source.scenario["variables"]["hostname"]
    return read_status(source, profiles=profiles, open_profile=opener, current_profile_id="local")


def main():
    for name in NAMES + CLUSTERSET:
        # Whether MySQL Shell is installed where the fixtures are written must
        # not change them: without it, none of the Shell actions is offered.
        raw = clusterset_report(name) if name in CLUSTERSET else F.report(name)
        report = with_actions(raw, shell=SHELL if name in CLUSTERSET else False)
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
