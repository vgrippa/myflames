"""Write the replication and maintenance reports the browser suites replay
(replication.mjs, maintenance.mjs).

Every file is computed by the real backend (myflames/replication.py and
myflames/lifecycle.py), never written by hand:

- replication-1.0 reports are the live recordings in
  test/fixtures/perf/replication-*.json (MySQL 8.4.11 and MariaDB 11.4.13
  replicas, 2026-10-15); the skip result is skip_one replayed over the
  recorded skip, and the loop snapshots come from SkipLoop replaying the
  recorded two-skip run (one snapshot while it runs, one after it stopped).
- lifecycle reports come from the fake tools and replayed connections of
  test/test_lifecycle.py (tool detection, plans, the dump job and its cancel),
  and the upgrade checks from the live recordings in test/fixtures/lifecycle/
  (the native subset 8.0.46 to 8.4.0 and the MySQL Shell 9.6.0 JSON).

Temporary paths are rewritten to /Users/dba/... so the files are stable.

Run from the repository root after the backend changes its output:
    python3 apps/workspace/test/ops-fixtures.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "test"))

from _perf_replay import FakeTime, fixture, replay  # noqa: E402
from myflames import lifecycle as lc, replication as rp  # noqa: E402
import test_lifecycle as tl  # noqa: E402
import test_replication as tr  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
STABLE = "/Users/dba/maintenance"


def write(name, report, temp=None):
    text = json.dumps(report, indent=1, sort_keys=False)
    if temp:
        for prefix in (os.path.realpath(str(temp)), str(temp)):
            text = text.replace(prefix, STABLE)
    assert "secret'with" not in text and "donor'private" not in text, name
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    print(name)


# ---- Replication -----------------------------------------------------------

for server in ("mysql-8.4-gtid-error", "mysql-8.4-healthy", "mysql-8.4-multi-error", "mariadb-11.4-error",
               "mysql-8.4-source", "mysql-8.4-denied", "mysql-8.4-mta-3030-error"):
    # Read again with the current code, so the report matches today's backend.
    write("replication-%s.json" % server, rp.read_status(replay("replication", server)[0]))

error = fixture("replication", "mysql-8.4-gtid-error")["report"]
plan = rp.find_channel(error, "")["skip_plan"]
conn, _ = replay("replication", "mysql-8.4-gtid-skip")
write("replication-mysql-8.4-skip.json",
      rp.skip_one(conn, "", plan["identity"], confirmed=True, statements=plan["statements"], settle_s=0))

data = fixture("replication", "mysql-8.4-loop-max")
clock = FakeTime()
running = []


def on_event(event):
    if event.get("event") == "skip" and not running:
        running.append(loop.status())


loop = rp.SkipLoop(tr.StrictReplay(data["calls"]), "", data["args"]["codes"], data["args"]["max_skips"],
                   clock=clock.monotonic, sleep=clock.sleep, settle_s=1.0, poll_s=1, on_event=on_event)
final = loop.run()
write("replication-loop-running.json", running[0])
write("replication-loop-stopped.json", final)

# Source versions (live, 2026-10-20: MySQL 9.7.2 and MariaDB 11.8.9 replicas
# reporting their source as 127.0.0.1:<port>): from the relay log, unknown
# without REPLICATION SLAVE, and, where the relay log gives none (no
# REPLICATION SLAVE; MariaDB before the source's first event), from the saved
# connections "Source 9.7" (server ID and UUID compared) and "Source 11.8"
# (server ID only). These use the synthetic @@hostname answers of
# test_replication_source_version.
import test_replication_source_version as tsv  # noqa: E402
for name, server, profile, source in (
        ("mysql-9.7-srcver-relay", "mysql-9.7-srcver-relay", None, None),
        ("mysql-9.7-srcver-profile", "mysql-9.7-srcver-denied", tsv.MYSQL_SOURCE, "mysql-9.7-srcver-profile"),
        ("mysql-9.7-srcver-denied", "mysql-9.7-srcver-denied", None, None),
        ("mariadb-11.8-srcver-relay", "mariadb-11.8-srcver-relay", None, None),
        ("mariadb-11.8-srcver-profile", "mariadb-11.8-srcver-unknown", tsv.MARIADB_SOURCE,
         "mariadb-11.8-srcver-profile")):
    rp.reset_source_cache()
    conn = tsv.source_conn(source) if source else None
    write("replication-%s.json" % name, rp.read_status(
        tr.ReplayConn(tsv.replica_calls(server)), source_versions=True, profiles=[profile] if profile else (),
        open_profile=lambda _p, conn=conn: conn, replica_host="127.0.0.1"))


# ---- Maintenance -----------------------------------------------------------

work = tempfile.mkdtemp(prefix="myflames-ops-")
cwd = os.getcwd()
os.chdir(work)
case = tl.LifecycleTests("test_tool_detection_and_engine_from_banner")
try:
    case.setUp()
    temp = case.root
    # Tools as /api/lifecycle/tools answers with a connection: every tool, the
    # server's engine and version, and what is offered for it.
    shown = copy.deepcopy(case.tools)
    for name, tool in shown["tools"].items():
        if tool.get("path"):
            tool["path"] = "/usr/local/bin/" + name
    server = lc.read_server(tl.ReplayConnection(), tl.CONNECTION)
    write("lifecycle-tools-mysql.json", dict(shown, server={k: server[k] for k in ("engine", "version")},
                                             offered=lc.offered_tools(server, case.tools)))
    maria = lc.read_server(tl.ReplayConnection(tl.server_variables("mariadb")), tl.CONNECTION)
    write("lifecycle-tools-mariadb.json", dict(shown, server={k: maria[k] for k in ("engine", "version")},
                                               offered=lc.offered_tools(maria, case.tools)))

    # Upgrade checks: the native subset and MySQL Shell, both from live recordings.
    def upgrade(report):
        report["server"] = {k: report["server"][k] for k in ("engine", "version", "identity")}
        return report
    native, _conn = case.native_live("native-8.0.46-to-8.4.0.json")
    write("upgrade-native-8.0.46-to-8.4.0.json", upgrade(native), temp)
    (case.bin / "upgrade.json").write_text((tl.FIXTURES / "shell-upgrade-8.0.46-to-8.4.0.json").read_text())
    shell = lc.upgrade_check(tl.ReplayConnection(tl.server_variables(version="8.0.46")), "8.4.0", tl.CONNECTION,
                             case.tools)
    write("upgrade-shell-8.0.46-to-8.4.0.json", upgrade(shell), temp)

    # Dump: a plan into an absent folder, the refusal for a folder with a file
    # in it, a running snapshot, the cancelled result, and a completed dump.
    tools = case.native_tools()
    with mock.patch.object(lc, "detect_tools", return_value=tools):
        dump = lc.prepare_dump(case.conn, tl.CONNECTION, case.output, schemas=["app"], tools=tools)
        write("lifecycle-plan-dump.json", dump, temp)
        busy = temp / "busy"
        busy.mkdir()
        (busy / "keep.sql").write_text("SELECT 1;\n")
        try:
            lc.prepare_dump(case.conn, tl.CONNECTION, str(busy), schemas=["app"], tools=tools)
            raise AssertionError("a folder with a file in it must be refused")
        except lc.Refused as exc:
            write("lifecycle-refused-dump.json", {"error": str(exc)}, temp)
        (case.bin / "mode").write_text("slow")
        seen = threading.Event()
        job = lc.dump_job(tl.CONNECTION, dump, confirmed=True, typed_target=dump["target"],
                          audit=case.audit("dump"), factory=lambda **kw: case.conn, start=False)
        case.jobs.append(job)
        emit = job.emit

        def saw_progress(text, stream="status"):
            emit(text, stream)
            if "25%" in text:
                seen.set()
        job.emit = saw_progress
        job.start()
        assert seen.wait(8), job.snapshot()
        write("lifecycle-job-dump-running.json", job.snapshot(), temp)
        job.cancel()
        write("lifecycle-job-dump-cancelled.json", job.wait(8), temp)
        (case.bin / "mode").unlink()
        shutil.rmtree(case.output)
    unused, done = case.make_dump()
    write("lifecycle-job-dump-done.json", done, temp)

    # Restore of that dump into another server: a ready plan with its typed target.
    with mock.patch.object(lc, "detect_tools", return_value=tools):
        restore = lc.prepare_restore(case.target(), tl.CONNECTION, case.output, tools=tools)
    write("lifecycle-plan-restore.json", restore, temp)

    # Clone: a preflight with failed checks (no clone plugin on the donor, the
    # donor missing from clone_valid_donor_list) and a ready one.
    donor_options = dict(tl.CONNECTION, password=tl.DONOR_PASSWORD)
    donor, recipient = tl.ReplayConnection(), case.target()
    donor.plugins = []
    recipient.variables["clone_valid_donor_list"] = ""
    write("lifecycle-plan-clone-failed.json",
          lc.prepare_clone(donor, recipient, donor_options, tl.CONNECTION, tools=case.tools), temp)
    write("lifecycle-plan-clone.json",
          lc.prepare_clone(tl.ReplayConnection(), case.target(), donor_options, tl.CONNECTION, tools=case.tools),
          temp)
finally:
    case.doCleanups()
    os.chdir(cwd)
    shutil.rmtree(work, ignore_errors=True)
