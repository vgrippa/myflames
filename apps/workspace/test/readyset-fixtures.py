"""Write the readyset-1.0 reports, dry-run explains, exports, plans and results
the Readyset browser suite replays (readyset.mjs).

Every file is computed by the real backend (myflames/readyset.py and
myflames/topology_links.py) over test/test_readyset.FakeReadyset and
test/test_topology_links.FakeProbeConn. Never written by hand. ``captured_at``
and review tokens are fixed so the files are deterministic.

Run from the repository root after the backend changes its output:
    python3 apps/workspace/test/readyset-fixtures.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test"))
os.environ.setdefault("MYFLAMES_SECRET_BACKEND", "vault")

from test_readyset import FakeReadyset, SCHEMA, assert_matches_schema, xml_rows  # noqa: E402
from test_topology_links import FakeProbeConn  # noqa: E402
from myflames import dbops, readyset, reviewed_writes, topology_links  # noqa: E402

OUT = os.path.join(ROOT, "apps", "workspace", "test", "fixtures")
AT = "2026-10-20T14:40:00+00:00"


class _DummyCase(object):
    def assertIn(self, a, b, msg=""):
        assert a in b, "%s: %r not in %r" % (msg, a, b)

    def assertTrue(self, expr, msg=""):
        assert expr, msg

    def assertEqual(self, a, b, msg=""):
        assert a == b, "%s: %r != %r" % (msg, a, b)


class _NotReadysetConn(object):
    def run(self, sql, extra_flags=None, timeout=None):
        return xml_rows([{"@@version_comment": "MySQL Community Server - GPL"}])


def write(name, value):
    path = os.path.join(OUT, "readyset-%s.json" % name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    print(path)


def _lab_topology_env():
    profiles = [
        {"id": "p-px", "name": "ProxySQL lab", "host": "127.0.0.1", "port": 6032, "remember_password": True},
        {"id": "p-rs", "name": "Readyset lab", "host": "127.0.0.1", "port": 3307, "remember_password": True},
        {"id": "p-pri", "name": "MySQL Primary", "host": "mysql-primary", "port": 3306, "remember_password": True},
        {"id": "p-rep", "name": "MySQL Replica", "host": "mysql-replica", "port": 3306, "remember_password": True},
    ]
    conns = {
        "p-px": FakeProbeConn(
            "proxysql",
            servers=[
                {"hostgroup_id": "10", "hostname": "mysql-primary", "port": "3306", "status": "ONLINE", "weight": "100", "comment": "writer"},
                {"hostgroup_id": "20", "hostname": "mysql-replica", "port": "3306", "status": "ONLINE", "weight": "100", "comment": "reader"},
                {"hostgroup_id": "30", "hostname": "127.0.0.1", "port": "3307", "status": "ONLINE", "weight": "100", "comment": "readyset cache"},
            ],
            rules=[
                {"rule_id": "100", "active": "1", "destination_hostgroup": "30", "mirror_hostgroup": None,
                 "comment": 'readyset-meta: {"query_id":"q_1111aaaa","mode":"cached"}'},
                {"rule_id": "101", "active": "1", "destination_hostgroup": "20", "mirror_hostgroup": "30",
                 "comment": 'readyset-meta: {"mode":"mirror"}'},
            ],
        ),
        "p-pri": FakeProbeConn(
            "database", hostname="mysql-primary", uuid="aaaa-1111", server_id=1, read_only=False,
            replicas=[{"Host": "mysql-replica", "Port": "3306", "Replica_UUID": "bbbb-2222", "Server_id": "2"}],
        ),
        "p-rep": FakeProbeConn(
            "database", hostname="mysql-replica", uuid="bbbb-2222", server_id=2, read_only=True,
            channels=[{
                "Channel_Name": "", "Source_Host": "mysql-primary", "Source_Port": "3306",
                "Source_UUID": "aaaa-1111", "Replica_IO_Running": "Yes", "Replica_SQL_Running": "Yes",
                "Seconds_Behind_Source": "0",
            }],
        ),
    }
    return profiles, lambda p: conns[p["id"]]


def report(conn, with_topology=False):
    topology_links._reset_cache_for_tests()
    if with_topology:
        profiles, opener = _lab_topology_env()
        value = readyset.read_status(
            conn,
            profiles=profiles,
            open_profile=opener,
            reached_host="127.0.0.1",
            current_profile_id="p-rs",
        )
    else:
        value = readyset.read_status(conn)
    value["captured_at"] = AT
    assert_matches_schema(_DummyCase(), value, SCHEMA)
    return value


def plan(conn, action, target=None, value=None, ctype="development", read_only=False, label="Readyset lab"):
    built = readyset.plan_action(readyset.read_status(conn), action, target, value)
    decision = dbops.decide(reviewed_writes.worst_kind(built["statements"]), ctype, read_only, label)
    built["policy"] = dict(
        (k, decision.get(k))
        for k in ("action", "tone", "title", "text", "connection_type", "read_only")
    )
    built["review_token"] = "fixture-token-%s" % action
    return built


def progress(phase, states, status=None):
    item = reviewed_writes.Progress(len(states), readyset.ACTION_TIMEOUT)
    item.set(phase, states, status)
    value = item.snapshot()
    value.update(elapsed_s=1.1, waiting_s=0.3 if phase == "sending" else None)
    return value


def main():
    with tempfile.TemporaryDirectory() as home:
        os.environ["HOME"] = os.environ["USERPROFILE"] = home
        write("lab", report(FakeReadyset(port=3307, database="shop"), with_topology=True))
        write("file-lag", report(FakeReadyset(port=3307, database="shop", repl_mode="mysql_file")))
        write("admin", report(FakeReadyset(port=6036, database=None, admin=True)))

        maint = FakeReadyset(port=3307, database="shop")
        maint.status_text = "Maintenance Mode"
        maint.repl_status = "Stopped"
        write("maintenance", report(maint))

        prob = FakeReadyset(port=3307, database="shop")
        prob.repl_status = "Error"
        prob.last_error = "Binlog purged on upstream"
        write("problem", report(prob))

        write("not", report(_NotReadysetConn()))

        write("explain-both", readyset.explain_cache(FakeReadyset(), "q_3333cccc", cache_type="both"))
        write("explain-collision", readyset.explain_cache(FakeReadyset(), "q_1111aaaa", cache_type="both"))
        write("explain-unsupported", readyset.explain_cache(FakeReadyset(), "SELECT RAND(), id FROM orders", cache_type="both"))
        write("export-caches", readyset.export_caches(FakeReadyset()))

        write("plan-create-cache", plan(
            FakeReadyset(),
            "create_cache",
            None,
            {
                "cache_type": "shallow",
                "query_or_id": "q_3333cccc",
                "name": "c_orders_status",
                "ttl_seconds": 60,
                "refresh_seconds": 30,
                "coalesce_ms": 50,
                "adaptive": True,
            },
        ))
        write("plan-flush-cache", plan(
            FakeReadyset(),
            "flush_cache",
            {"kind": "cache", "name": "c_recent_shallow", "query_id": "q_2222bbbb"},
        ))
        write("plan-drop-cache", plan(
            FakeReadyset(),
            "drop_cache",
            {"kind": "cache", "name": "c_orders_deep", "query_id": "q_1111aaaa"},
        ))
        write("plan-resnapshot", plan(
            FakeReadyset(),
            "resnapshot_table",
            {"kind": "table", "table": "`shop`.`orders`"},
        ))
        write("plan-log-level", plan(
            FakeReadyset(),
            "set_log_level",
            None,
            {"directives": "info,readyset_adapter=debug"},
        ))
        write("plan-allow-fn", plan(
            FakeReadyset(),
            "add_shallow_allowed",
            {"allow_kind": "function"},
            {"allow_kind": "function", "name": "COALESCE"},
        ))
        write("plan-drop-fn", plan(
            FakeReadyset(),
            "drop_shallow_allowed",
            {"kind": "function", "allow_kind": "function", "name": "NOW"},
            {"allow_kind": "function", "name": "NOW"},
        ))
        write("plan-enter-maint", plan(FakeReadyset(), "enter_maintenance_mode"))
        write("plan-stop-repl", plan(FakeReadyset(), "stop_replication"))
        write("plan-shutdown", plan(
            FakeReadyset(port=6036, database=None, admin=True),
            "shutdown",
            label="Readyset Admin",
        ))
        write("plan-read-only", plan(
            FakeReadyset(),
            "flush_cache",
            {"kind": "cache", "name": "c_recent_shallow", "query_id": "q_2222bbbb"},
            ctype="production",
            read_only=True,
        ))

        after_flush = FakeReadyset()
        reviewed_flush = plan(
            after_flush,
            "flush_cache",
            {"kind": "cache", "name": "c_recent_shallow", "query_id": "q_2222bbbb"},
        )
        reviewed_flush.pop("review_token")
        write("result-flush-done", readyset.execute_action(after_flush, reviewed_flush))

        after_create = FakeReadyset()
        reviewed_create = plan(
            after_create,
            "create_cache",
            None,
            {
                "cache_type": "shallow",
                "query_or_id": "q_3333cccc",
                "name": "c_orders_status",
                "ttl_seconds": 60,
                "refresh_seconds": 30,
                "coalesce_ms": 50,
                "adaptive": True,
            },
        )
        reviewed_create.pop("review_token")
        write("result-create-done", readyset.execute_action(after_create, reviewed_create))

        write("progress-checking", progress("checking", ["waiting"]))
        write("progress-sending", progress("sending", ["sent"]))
        write("progress-finished-unknown", progress("finished", ["unknown"], "unknown"))


if __name__ == "__main__":
    main()
