"""Write the proxysql-1.0 reports, plans and results the ProxySQL browser
suite replays (proxysql.mjs).

Every file is computed by the real backend (myflames/proxysql.py) over
test/test_proxysql.FakeAdmin, the SQLite stand-in for ProxySQL Admin shaped
like the live lab (ProxySQL 3.0.11 in front of MariaDB Galera and MySQL InnoDB
Cluster, 2026-10-20). Never written by hand. ``captured_at`` and the review
token are fixed so the files are stable.

Run from the repository root after the backend changes its output:
    python3 apps/workspace/test/proxysql-fixtures.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test"))
os.environ.setdefault("MYFLAMES_SECRET_BACKEND", "vault")

from test_proxysql import FakeAdmin, NotProxy  # noqa: E402
from myflames import dbops, proxysql, reviewed_writes  # noqa: E402

OUT = os.path.join(ROOT, "apps", "workspace", "test", "fixtures")
AT = "2026-10-20T14:40:00+00:00"


def write(name, value):
    path = os.path.join(OUT, "proxysql-%s.json" % name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print(path)


def report(conn):
    value = proxysql.read_status(conn)
    value["captured_at"] = AT
    return value


def plan(conn, action, target=None, value=None, ctype="development", read_only=False):
    value = proxysql.plan_action(proxysql.read_status(conn), action, target, value)
    decision = dbops.decide(reviewed_writes.worst_kind(value["statements"]), ctype, read_only, "ProxySQL lab")
    value["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text", "connection_type", "read_only")}
    value["review_token"] = "fixture-token-%s" % action
    return value


def main():
    write("lab", report(FakeAdmin()))
    pending = FakeAdmin()
    pending.db.execute("UPDATE main.mysql_servers SET weight = 7 WHERE hostname = 'mf-ic-3'")
    write("pending", report(pending))
    problem = FakeAdmin()
    problem.db.execute("UPDATE main.mysql_servers SET status = 'OFFLINE_SOFT' WHERE hostname IN ('mf-ic-2', 'mf-ic-3')")
    problem.load("servers")
    write("problem", report(problem))
    write("not", report(NotProxy()))
    write("plan-offline-soft", plan(FakeAdmin(), "server_offline_soft", {"config": [30, "mf-ic-2", 3306]}))
    write("plan-offline-hard", plan(FakeAdmin(), "server_offline_hard", {"config": [30, "mf-ic-2", 3306]}))
    write("plan-weight", plan(FakeAdmin(), "server_weight", {"config": [30, "mf-ic-2", 3306]}, 50))
    write("plan-rule-disable", plan(FakeAdmin(), "rule_disable", {"rule_id": 30}))
    write("plan-read-only", plan(FakeAdmin(), "server_offline_soft", {"config": [30, "mf-ic-2", 3306]},
                                 ctype="production", read_only=True))
    after = FakeAdmin()
    reviewed = plan(after, "server_offline_soft", {"config": [30, "mf-ic-2", 3306]})
    reviewed.pop("review_token")
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        done = proxysql.execute_action(after, reviewed)
    write("result-done", done)
    saved = plan(after, "servers_save")
    write("plan-save", saved)


if __name__ == "__main__":
    main()
