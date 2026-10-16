"""Write the sessions-1.0 and innodb-1.0 fixtures the browser suites replay.

They are computed by the real backend (myflames/sessions.py and
myflames/innodb_status.py) from the statement-by-statement recordings in
test/fixtures/perf/, which were captured from throwaway MySQL 8.4 and
MariaDB 11.4 containers while a scenario held an idle transaction with a row
lock, made sessions wait for it and for a metadata lock, and ran a load.

Run from the repository root after the backend changes its output:
    python3 apps/workspace/test/server-fixtures.py
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test"))

from _perf_replay import ReplayConn, fixture  # noqa: E402
from myflames.innodb_status import innodb_report  # noqa: E402
from myflames.sessions import sessions_report  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def write(name, report):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=False)
        handle.write("\n")
    print(name)


def sessions(server, **kwargs):
    return sessions_report(ReplayConn(fixture("sessions", server)["calls"]), **kwargs)


def innodb(server, **kwargs):
    return innodb_report(ReplayConn(fixture("innodb", server)["calls"]), **kwargs)


write("sessions-mysql-8.4.json", sessions("mysql-8.4", active_only=False))
write("sessions-mysql-8.4-active.json", sessions("mysql-8.4", active_only=True))
write("sessions-mariadb-11.4.json", sessions("mariadb-11.4", active_only=False))
write("sessions-mysql-8.4-noproc.json", sessions("mysql-8.4-noproc", active_only=False))
write("innodb-mysql-8.4.json", innodb("mysql-8.4"))
write("innodb-mariadb-11.4.json", innodb("mariadb-11.4"))
write("innodb-mysql-8.4-noproc.json", innodb("mysql-8.4-noproc"))
write("innodb-mysql-8.4-raw.json", innodb("mysql-8.4", raw=True))

# Configuration (config_review.py) and its Change sheet (variables.py), from the
# live recordings in test/fixtures/perf/config-*.json and variable-*.json.
from myflames import dbops, variables  # noqa: E402
from myflames.config_review import config_report  # noqa: E402


def config(server):
    return config_report(ReplayConn(fixture("config", server)["calls"]))


def variable(server, scenario):
    """variable-1.0, the plan (with the policy /api/live/variable/plan adds
    for an unsaved, so Production, connection) and the applied change."""
    data = fixture("variable", "%s-%s" % (server, scenario))
    conn, args = ReplayConn(data["calls"]), data["args"]
    plan = variables.plan_change(conn, args["name"], args["value"], args["mode"])
    decision = dbops.decide(dbops.classify(variables.representative_statement(args["name"], args["mode"])))
    plan["policy"] = {k: decision.get(k) for k in ("action", "tone", "title", "text", "connection_type", "read_only")}
    change = None
    if plan["status"] == "ready" and args.get("apply"):
        change = variables.apply_change(conn, plan, plan["expect"], confirmed=True)
    return {"variable": plan["variable"], "plan": plan, "change": change}


write("config-mysql-8.4.json", config("mysql-8.4"))
write("config-mariadb-13.0-default.json", config("mariadb-13.0-default"))
write("config-mariadb-11.4.json", config("mariadb-11.4"))
for server, scenario in (("mysql-8.4", "global"), ("mysql-8.4", "persist-only"), ("mariadb-11.4", "global"),
                         ("mariadb-11.4", "persist"), ("mariadb-11.4", "readonly")):
    write("variable-%s-%s.json" % (server, scenario), variable(server, scenario))
