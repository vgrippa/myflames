"""Write audit records for test/audit.mjs into a temporary workspace database.

Usage: python3 apps/workspace/test/audit-seed.py STORE_PATH
       python3 apps/workspace/test/audit-seed.py STORE_PATH --bulk N

--bulk N adds N newer Done records instead (paging: the earlier records then
lie beyond the first page of 500).

Uses the real audit code (myflames/dbops.py and workspace_store.py), so the
records have exactly the shape the Audit log reads: one per status (ok,
error, unknown, pending, declined, refused), a statement with a password that
must arrive redacted, and one record 40 days old for the period filter and the
retention setting. Run from the repository root with HOME set to a temporary
directory (the store path is explicit; nothing reads ~/.myflames).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
from myflames import dbops  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

store = WorkspaceStore(sys.argv[1])
if len(sys.argv) == 4 and sys.argv[2] == "--bulk":
    import datetime
    from myflames.workspace_store import AUDIT_COLUMNS  # noqa: E402
    now = datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for n in range(int(sys.argv[3])):
        row = dbops._row("kill", ["/*myflames:kill*/ KILL CONNECTION %d" % (1000 + n)], "cli", "ok",
                         connection={"host": "batch.internal", "port": 3306, "user": "ops"},
                         safety={"profile_id": None, "profile_name": None, "type": "production", "read_only": False})
        row.update(status="ok", outcome="killed", finished_at=row["attempted_at"],
                   attempted_at=(now - datetime.timedelta(seconds=5, milliseconds=n)).isoformat(timespec="microseconds"))
        rows.append([row.get(c) for c in AUDIT_COLUMNS])
    with sqlite3.connect(sys.argv[1]) as db:
        db.executemany("INSERT INTO audit (%s) VALUES (%s)" % (", ".join(AUDIT_COLUMNS), ", ".join("?" * len(AUDIT_COLUMNS))),
                       rows)
    print(len(rows))
    sys.exit(0)
shop = {"profile_id": "p1", "profile_name": "Shop primary", "type": "production", "read_only": False,
        "endpoint_matches": True}
reports = {"profile_id": "p2", "profile_name": "Reports", "type": "development", "read_only": True,
           "endpoint_matches": True}
shop_conn = {"host": "db1.internal", "port": 3306, "user": "dba"}
reports_conn = {"host": "reports.internal", "port": 3307, "user": "analyst"}
policy = {"action": "confirm", "kind": "risky", "category": "kill", "connection_type": "production",
          "read_only": False}
target = {"connection_id": 93, "mode": "connection", "user": "app", "host": "10.0.0.5:51234"}


def trail(operation, connection, safety):
    audit = dbops.AuditTrail(store, operation, "ui", connection=connection, safety=safety, policy=policy)
    audit.server("mysql", "8.4.3")
    return audit


ids = {}
# 40 days old: outside "Last 30 days", purged by a 30-day retention.
ids["old"] = dbops.record_outcome(store, "kill", ["/*myflames:kill*/ KILL CONNECTION 7"], "cli", "declined",
                                  outcome="declined", message="Nothing was ended.", connection=shop_conn,
                                  safety=shop, target={"connection_id": 7, "mode": "connection"})
# A statement with a secret: stored redacted.
ids["declined"] = dbops.record_outcome(store, "set_password", ["ALTER USER 'app'@'%' IDENTIFIED BY 'hunter2'"],
                                       "ui", "declined", outcome="declined",
                                       message="Cancelled in the confirmation; nothing was sent.",
                                       connection=shop_conn, safety=shop)
ids["refused"] = dbops.record_outcome(store, "kill", ["/*myflames:kill*/ KILL CONNECTION 41"], "ui", "refused",
                                      outcome="read_only", message="Connection Reports is read-only in myflames, "
                                      "so this statement was not sent.", connection=reports_conn, safety=reports,
                                      target={"connection_id": 41, "mode": "connection"})
pending = trail("kill", {"host": "127.0.0.1", "port": 3310, "user": "root"},
                {"profile_id": None, "profile_name": None, "type": "production", "read_only": False})
ids["pending"] = pending.attempt(["/*myflames:kill*/ KILL CONNECTION 12"], target={"connection_id": 12,
                                                                                    "mode": "connection"})
unknown = trail("kill", reports_conn, dict(reports, read_only=False))
ids["unknown"] = unknown.attempt(["/*myflames:kill*/ KILL CONNECTION 55"], target={"connection_id": 55,
                                                                                   "mode": "connection"},
                                 verified=["user", "host", "not this session"])
unknown.fail(Exception("ERROR 2013 (HY000): Lost connection to MySQL server during query"))
error = trail("kill_query", shop_conn, shop)
ids["error"] = error.attempt(["/*myflames:kill*/ KILL QUERY 94"], target=dict(target, connection_id=94,
                                                                              mode="query"))
error.complete("error", outcome="denied", error=1095, message="ERROR 1095 (HY000): You are not owner of thread 94")
ok = trail("kill", shop_conn, shop)
ids["ok"] = ok.attempt(["/*myflames:kill*/ KILL CONNECTION 93"], target=target,
                       verified=["user", "host", "not this session", "transaction"])
ok.complete("ok", outcome="killed", message="Ended connection 93. Its open transaction was rolled back.",
            after={"present": False})

# Order by age: ok newest, then error, unknown, pending, refused, declined, old.
order = ["ok", "error", "unknown", "pending", "refused", "declined"]
with sqlite3.connect(sys.argv[1]) as db:
    for minutes, name in enumerate(order):
        db.execute("UPDATE audit SET attempted_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', ?) WHERE id = ?",
                   ("-%d minutes" % (minutes * 10 + 1), ids[name]))
    db.execute("UPDATE audit SET attempted_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', '-40 days') "
               "WHERE id = ?", (ids["old"],))
print(" ".join("%s=%s" % item for item in sorted(ids.items())))
