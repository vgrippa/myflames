"""Replication status for MySQL and MariaDB replicas, plus the guarded
writes that recover one failed transaction at a time.

:func:`read_status` returns the ``replication-1.0`` report: one entry per
replication channel (MySQL) or connection (MariaDB) with thread states, the
last IO and SQL errors, positions or GTID sets, lag with its caveats, and a
status (``ok`` / ``watch`` / ``problem`` / ``unknown``) with the reasons.
Group Replication channels are listed but not judged. A channel whose
applier stopped on an error carries ``skip_plan``: the exact statements that
would skip that one transaction.

The writes follow the db-operations contract (see :mod:`myflames.dbops`):
:func:`plan_skip` builds the exact statements to skip ONE failed transaction
for one channel, per engine and GTID mode; :func:`skip_one` checks the
connection's write policy (:func:`myflames.dbops.gate`), re-reads the channel
right before sending, refuses when the confirmed identity (channel, method,
error number/message/timestamp, failing GTID, relay and apply positions,
source) changed, sends the plan as one script in one client session
(``gtid_next`` and ``default_master_connection`` are session variables),
records the audit trail, and re-reads the channel afterwards to show whether
the transaction that was planned is the one that was skipped.

:class:`SkipLoop` is the native, opt-in "skip until stopped" loop (the
behavior users expect from pt-slave-restart, written from server source;
Percona Toolkit is GPL and neither its code nor its text is used). It skips
only the error codes the user selected, is bounded by a maximum skip count
and an optional maximum duration, waits between checks and backs off while
skips keep coming, and stops at once on: any error it was not told to skip,
the cap, the duration, Stop (before the next skip), a lost connection, a
server restart (Uptime went down), a configuration change (channel gone or
now Group Replication, source host/port/UUID, gtid_mode, AGAT or the skip
method changed), the applier stopping without an error, or no progress (the
same transaction fails again). State lives in memory only: it never resumes
after myflames restarts. :class:`SkipLoopRegistry` keys loops by connection
identity, so a loop never attaches to another server.

Verified against server source (mysql-server 9.7 trunk "M/", mariadb-server
13.0 trunk "Ma/"; older tags where noted) and live (2026-10-15, MySQL 8.4 and
8.0 replicas with GTID on and off, MariaDB 11.4 with GTID; see
test/fixtures/perf/replication-*.json):

* ``SHOW REPLICA STATUS`` columns and privilege: M/sql/rpl_replica.cc:3388-3471,
  M/sql/sql_show.cc:712-713. The REPLICA keywords are WL#14171 (8.0.22) and
  the SLAVE forms were removed by WL#15831 (8.4.0), so myflames sends
  ``SHOW SLAVE STATUS`` before 8.0.22. Needs REPLICATION CLIENT or SUPER.
  Columns are read by name from the client's ``--xml`` output, never by
  position (they differ by engine and version).
* ``Seconds_Behind_Source``: M/sql/rpl_replica.cc:3618-3678. NULL when the
  SQL thread is not running; with the IO thread stopped it is NULL only once
  the applier has caught up, otherwise it still counts; 0 when the applied
  position equals the received position, which is also true when the
  receiver is far behind the source.
* Performance Schema replication tables: table_replication_connection_status.cc:95-105
  (heartbeats, LAST_QUEUED_TRANSACTION_*), table_replication_applier_status_by_worker.cc:65-86
  (END_APPLY only for LAST_APPLIED, :71-77; APPLYING has the first three,
  :80-84), table_replication_connection_configuration.cc:72 (HEARTBEAT_INTERVAL),
  table_replication_applier_configuration.cc:68-79
  (ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS_TYPE OFF/LOCAL/UUID, 8.0.23+), all
  in M/storage/perfschema/. The worker error text "Worker N failed executing
  transaction 'GTID'" is M/sql/rpl_rli_pdb.cc:1170-1174. The source sends a
  heartbeat only after heartbeat_period without any event
  (M/sql/rpl_binlog_sender.cc:812-836), so a busy link has old heartbeats;
  the idle check uses the newer of the last heartbeat and the last queued
  transaction.
* gtid_mode names M/sql/rpl_gtid_mode.cc:35; enforce_gtid_consistency
  M/sql/sys_vars.cc:6418-6432.
* Skip counter: M/sql/sys_vars.cc:6185-6209; START REPLICA copies it only
  when the channel has AGAT or gtid_mode is not ON, and always resets it to
  0 (M/sql/rpl_replica.cc:9029-9042), so at gtid_mode=ON without AGAT it
  skips nothing; a START without FOR CHANNEL that starts SQL threads fails
  with 3086 on a multi-source replica (M/sql/rpl_replica.cc:674-681); the
  counter keeps skipping to the end of the current event group
  (M/sql/log_event.cc:1006-1049). gtid_next privilege checks:
  M/sql/sys_vars.cc:356-370, 6549-6554; tagged GTIDs (8.3.0) need
  TRANSACTION_GTID_TAG plus one of the other three, never SUPER
  (M/sql/sys_vars.cc:319-339, chosen at 1429-1432).
* MariaDB ``SHOW ALL SLAVES STATUS`` columns: Ma/sql/sql_show.cc:10847-10910
  (REPLICA is a lexer synonym since 10.5.1, Ma/sql/lex.h:538-539). Needs
  SLAVE MONITOR; SUPER also counts on 10.11 and, from 11.0, only for grants
  stored by an earlier version (Ma/sql/privilege.h:52-70, 535-542,
  Ma/sql/sql_show.cc:9118).
* MariaDB ``Seconds_Behind_Master``: Ma/sql/slave.cc:3020-3059. NULL unless
  the IO thread is connected and reading and the SQL thread runs; 0 when the
  applier is idle. The applier's error text names the event's GTID ("Gtid
  d-s-n", Ma/sql/rpl_rli.cc:2450).
* MariaDB skip counter is per connection, selected by the session variable
  ``default_master_connection``; setting it needs the SQL thread stopped
  (1198), and fails with 1966 when the connection uses GTID, parallel
  replication is on (slave_parallel_threads > 0 and a parallel mode other
  than none) and gtid_slave_pos has more than one domain
  (Ma/sql/sys_vars.cc:6143-6175, Ma/sql/rpl_mi.h:176-180). The counter
  finishes the current group (Ma/sql/log_event_server.cc:649-657,
  3383-3404). SET GLOBAL needs SUPER; START/STOP SLAVE need REPLICATION
  SLAVE ADMIN (Ma/sql/sql_repl.cc:3897, 4101).
* ``replica_skip_errors`` / ``slave_skip_errors`` are startup only and
  read-only at runtime (M/sql/sys_vars.cc:6212-6220, Ma/sql/sys_vars.cc:
  6204-6209); myflames never sets them.

Seen live (2026-10-15) and relied on:

* A failed MySQL worker keeps the failing GTID in APPLYING_TRANSACTION and
  names it and the table in LAST_ERROR_MESSAGE (8.4.11 and 8.0.46, four
  workers); the status row names the GTID, not the table.
* At gtid_mode ON_PERMISSIVE a GTID transaction skipped with the counter is
  not added to gtid_executed (8.4.11), so a GTID transaction is always
  skipped with the empty transaction when gtid_mode is not OFF.
* The skip counter passes the transaction at the applier's position; with
  a multithreaded applier that is the failed one only when commit order is
  preserved, so the relay log at Relay_Log_Pos (``SHOW RELAYLOG EVENTS``,
  REPLICATION SLAVE on MySQL, M/sql/sql_show.cc:667-669; SLAVE MONITOR on
  MariaDB, Ma/sql/privilege.h:545) must contain the failed event's
  end_log_pos.
* MariaDB 11.4 names no GTID in a row event's error text and says
  ``end_log_pos 0``; the relay log's ``BEGIN GTID d-s-n`` gives it. After a
  counter skip the domain's position in gtid_slave_pos is that GTID. The
  ``Gtid_Slave_Pos`` column of SHOW ALL SLAVES STATUS is gtid_current_pos
  (Ma/sql/sql_show.cc:9122, Ma/sql/sql_repl.cc:5680-5690), so the after
  check reads the variable.

Lag numbers comparing timestamps from two servers (worker commit
timestamps) assume the servers' clocks are in sync; the report says so where
it shows them.
"""
import contextlib
import json
import os
import re
import threading
import time
import uuid

from . import dbops, privileges
from .connector import ConnectorError, terminal_text
from .perf_common import Reader, _duration_text, _version_tuple, denied, now, preflight, reason

SCHEMA_VERSION = "replication-1.0"
SKIP_SCHEMA_VERSION = "replication-skip-1.0"
LOOP_SCHEMA_VERSION = "replication-skip-loop-1.0"
REPLICATION_TAG = "/*myflames:replication*/ "
SKIP_TAG = "/*myflames:replication-skip*/ "
#: Audit operation names: one skip, and the skips a skip loop makes.
OPERATION_SKIP = "replication_skip"
OPERATION_LOOP = "replication_skip_loop"

#: Above this many seconds of Seconds_Behind_* (beyond a configured SQL_Delay),
#: or of applying lag, a channel is "watch". A rule of thumb, not a verdict:
#: one long transaction shows as a jump.
LAG_WATCH_S = 60

GROUP_REPLICATION_CHANNELS = ("group_replication_applier", "group_replication_recovery")
GR_NOTE = "Group Replication member, not judged here."

#: Error codes the skip loop offers, with their meaning. None is preselected.
COMMON_ERRORS = {1062: "duplicate key", 1032: "row not found", 1452: "foreign key: no parent row"}

#: ER_REPLICA_WORKER_STOPPED_PREVIOUS_THD_ERROR (M/share/messages_to_clients.txt:7250,
#: raised in M/sql/rpl_replica_commit_order_manager.cc:320): with
#: replica_preserve_commit_order a worker stops because an EARLIER transaction in
#: commit order failed, and rolls its own transaction back. Seen live (8.4.11, 4
#: workers): worker 1 failed with 1062 on GTID :15, worker 2 then stopped with
#: 3030 on :16, and SHOW REPLICA STATUS reported the 3030 and :16. The
#: transaction to skip is the earlier worker's, never the 3030 one.
WORKER_STOPPED_BY_EARLIER = 3030

DRIFT_WARNING = ("A skipped transaction is missing on the replica. Rows it touched now differ from the "
                 "source, and later transactions on those rows may fail or change different data silently.")

AFTER_SKIP_STEPS = (
    "Find the cause: writes run directly on the replica (check read_only and super_read_only), "
    "non-deterministic statements, or a replica restored from an inconsistent backup.",
    "Verify the affected tables with checksums computed on both sides over the same data "
    "(CHECKSUM TABLE on a quiet table, or a chunked checksum tool in the style of "
    "pt-table-checksum). myflames recommends this and never runs it for you.",
    "Resync those tables or rebuild the replica (clone or backup restore).",
)

LOOP_MAX_SKIPS = 1000
LOOP_MAX_DURATION_S = 24 * 3600
LOOP_MIN_POLL_S, LOOP_MAX_POLL_S = 1.0, 60.0
LOOP_DEFAULT_POLL_S = 2.0
#: Seconds to let the applier move before reading the channel after a skip.
SETTLE_S = 0.5
#: Finished loops kept per registry for status reads.
LOOP_HISTORY = 20

#: Variables read with the standard preflight; a name one engine does not
#: have simply does not appear in the answer.
EXTRA_VARIABLES = ("enforce_gtid_consistency", "gtid_current_pos", "gtid_domain_id", "gtid_mode",
                   "gtid_slave_pos", "gtid_strict_mode", "read_only", "replica_parallel_workers",
                   "replica_preserve_commit_order", "slave_parallel_threads", "slave_parallel_workers",
                   "slave_preserve_commit_order", "super_read_only", "sql_replica_skip_counter", "sql_slave_skip_counter")

_PS_HEARTBEAT_SQL = ("SELECT CHANNEL_NAME, HEARTBEAT_INTERVAL FROM "
                     "performance_schema.replication_connection_configuration")
_PS_AGAT_SQL = ("SELECT CHANNEL_NAME, ASSIGN_GTIDS_TO_ANONYMOUS_TRANSACTIONS_TYPE FROM "
                "performance_schema.replication_applier_configuration")
_PS_CONNECTION_SQL = (
    "SELECT CHANNEL_NAME, SERVICE_STATE, COUNT_RECEIVED_HEARTBEATS, LAST_HEARTBEAT_TIMESTAMP, "
    "LAST_QUEUED_TRANSACTION, LAST_QUEUED_TRANSACTION_END_QUEUE_TIMESTAMP, "
    "TIMESTAMPDIFF(MICROSECOND, LAST_HEARTBEAT_TIMESTAMP, NOW(6)), "
    "TIMESTAMPDIFF(MICROSECOND, LAST_QUEUED_TRANSACTION_END_QUEUE_TIMESTAMP, NOW(6)) "
    "FROM performance_schema.replication_connection_status")
_PS_WORKER_SQL = (
    "SELECT CHANNEL_NAME, WORKER_ID, LAST_ERROR_NUMBER, LAST_ERROR_MESSAGE, LAST_ERROR_TIMESTAMP, "
    "APPLYING_TRANSACTION, APPLYING_TRANSACTION_ORIGINAL_COMMIT_TIMESTAMP, "
    "TIMESTAMPDIFF(MICROSECOND, APPLYING_TRANSACTION_ORIGINAL_COMMIT_TIMESTAMP, NOW(6)), "
    "LAST_APPLIED_TRANSACTION, LAST_APPLIED_TRANSACTION_END_APPLY_TIMESTAMP, "
    "TIMESTAMPDIFF(MICROSECOND, LAST_APPLIED_TRANSACTION_ORIGINAL_COMMIT_TIMESTAMP, "
    "LAST_APPLIED_TRANSACTION_END_APPLY_TIMESTAMP) "
    "FROM performance_schema.replication_applier_status_by_worker")

#: Relay log events read to see which transaction the applier restarts at.
RELAY_PEEK_EVENTS = 100
_RELAY_META = ("Format_desc", "Previous_gtids", "Rotate", "Stop", "Heartbeat", "Gtid_list", "Binlog_checkpoint",
               "Start_v3")

_FAILED_TRX = re.compile(r"failed executing transaction '([^']+)'")
_MARIADB_GTID_TEXT = re.compile(r"\bGtid (\d+-\d+-\d+)")
_END_LOG_POS = re.compile(r"end_log_pos (\d+)")
_TABLE_TEXT = re.compile(r"\bon table ([^\s;,]+)")
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_MYSQL_GTID = re.compile(r"^(%s)(?::([A-Za-z_][A-Za-z0-9_]{0,31}))?:(\d+)$" % _UUID)
_GTID_ITEM = re.compile(r"^(\d+)-(\d+)-(\d+)$")
_LINE = re.compile(r"\bat line (\d+)")
_FATAL = ("ERROR 2002", "ERROR 2003", "ERROR 2006", "ERROR 2013", "timed out", "Lost connection to")
_MICRO = 1000000.0

#: Server errors from a skip script, mapped to result statuses (db-operations
#: section 3). Anything else is ``failed`` with the server's text.
#: 6038 is ER_SPECIFIC_ACCESS_DENIED, raised for a tagged GTID without
#: TRANSACTION_GTID_TAG (M/sql/sys_vars.cc:332).
_DENIED = (1044, 1045, 1095, 1142, 1227, 6038)
_REFUSED_BY_SERVER = (1193, 1198, 1228, 1229, 1231, 1232, 1238, 1781, 1782, 1790, 1837, 1966, 3086)


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value):
    return value if value not in (None, "") else None


def _ts(value):
    """A Performance Schema timestamp, or None for the zero timestamp."""
    text = _text(value)
    return None if text is None or text.startswith("0000-00-00") else text


def _seconds(microseconds):
    number = _float(microseconds)
    return None if number is None else round(number / _MICRO, 3)


def _qtext(value):
    """Server-originated text (channel names, GTIDs) inside '...': quotes
    doubled, backslashes doubled (both readings of NO_BACKSLASH_ESCAPES)."""
    text = str(value)
    if "\\" in text or "\x00" in text or "\n" in text:
        raise ValueError("This name or GTID contains characters myflames will not put in a statement.")
    return text.replace("'", "''")


def _pick(row, *names):
    """The first present, non-None value among naming variants."""
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _fatal(exc):
    text = str(exc)
    return any(marker in text for marker in _FATAL)


def status_statement(engine, version_tuple):
    """The statement that lists every channel or connection on this server."""
    if engine == "mariadb":
        return "SHOW ALL SLAVES STATUS"
    return "SHOW REPLICA STATUS" if tuple(version_tuple) >= (8, 0, 22) else "SHOW SLAVE STATUS"


# ---- GTIDs ------------------------------------------------------------------------

def parse_mariadb_gtids(text):
    """``'domain-server-seq,domain-server-seq'`` as ``[{"domain", "server", "seq"}]``.

    Positions are per domain on MariaDB; the report shows them per domain.
    """
    out = []
    for part in (text or "").split(","):
        match = _GTID_ITEM.match(part.strip())
        if match:
            out.append({"domain": int(match.group(1)), "server": int(match.group(2)),
                        "seq": int(match.group(3))})
    return out


def parse_gtid_set(text):
    """A MySQL GTID set (``uuid:1-5:7,uuid2:tag:1-3``, 8.3+ tags included) as
    ``{(uuid, tag): [(start, end), ...]}``; uuid in lower case, tag ``""``
    for untagged transactions."""
    out = {}
    for part in (text or "").replace("\n", "").split(","):
        tokens = [t.strip() for t in part.strip().split(":") if t.strip()]
        if not tokens:
            continue
        source, tag = tokens[0].lower(), ""
        for token in tokens[1:]:
            match = re.match(r"^(\d+)(?:-(\d+))?$", token)
            if match:
                start = int(match.group(1))
                end = int(match.group(2) or start)
                out.setdefault((source, tag), []).append((start, end))
            else:
                tag = token
    return out


def gtid_in_set(gtid, gtid_set):
    """True when the single MySQL GTID *gtid* is in the set text *gtid_set*."""
    match = _MYSQL_GTID.match(gtid or "")
    if not match:
        return False
    key = (match.group(1).lower(), match.group(2) or "")
    number = int(match.group(3))
    return any(start <= number <= end for start, end in parse_gtid_set(gtid_set).get(key, ()))


def failing_gtid(channel):
    """The GTID of the transaction the applier failed on, when readable.

    MySQL: from the status error text or the failing worker's error text
    ("Worker N failed executing transaction 'GTID'"), else the failing
    worker's APPLYING_TRANSACTION. MariaDB: the "Gtid d-s-n" part of the
    error text. ``ANONYMOUS`` and empty values are not GTIDs.
    """
    status_error = (channel.get("sql") or {}).get("error") or {}
    sql_error = status_error.get("message") or ""
    worker = channel.get("worker") or {}
    worker_error = (worker.get("error") or {}).get("message") or ""
    texts = (sql_error, worker_error)
    if status_error.get("errno") == WORKER_STOPPED_BY_EARLIER:
        # The status names the worker that stopped behind the failure; the
        # failed transaction is the one a worker reports with another error.
        if effective_error(channel) is None:
            return None
        texts = (worker_error,)
    for text in texts:
        match = _FAILED_TRX.search(text)
        if match and match.group(1).upper() != "ANONYMOUS":
            return match.group(1)
        match = _MARIADB_GTID_TEXT.search(text)
        if match:
            return match.group(1)
    applying = worker.get("failing_applying_transaction")
    if applying and applying.upper() != "ANONYMOUS":
        return applying
    return None


def effective_error(channel):
    """The error of the transaction that failed: the status error, except
    when it is :data:`WORKER_STOPPED_BY_EARLIER`, where it is the error of the
    worker that failed first. None when only workers that stopped behind a
    failure are visible (the failed transaction cannot be named)."""
    status_error = (channel.get("sql") or {}).get("error") or {}
    if status_error.get("errno") != WORKER_STOPPED_BY_EARLIER:
        return status_error
    worker_error = (channel.get("worker") or {}).get("error") or {}
    if worker_error.get("errno") and worker_error.get("errno") != WORKER_STOPPED_BY_EARLIER:
        return worker_error
    return None


def _error_details(message):
    """The table and the source end_log_pos an applier error message names."""
    message = message or ""
    table = _TABLE_TEXT.search(message)
    end = _END_LOG_POS.search(message)
    end_pos = int(end.group(1)) if end else None
    return (table.group(1) if table else None), (end_pos or None)


# ---- reading status -----------------------------------------------------------------

def _privilege_message(server, exc):
    if server.engine == "mariadb":
        need = ("SLAVE MONITOR" if server.version_tuple >= dbops.MARIADB_SUPER_SPLIT_END
                else "SLAVE MONITOR or SUPER")
        return ("This account cannot read replication status: SHOW ALL SLAVES STATUS needs the %s "
                "privilege (the server said: %s)." % (need, reason(exc)))
    return ("This account cannot read replication status: %s needs the REPLICATION CLIENT or SUPER "
            "privilege (the server said: %s)." % (status_statement(server.engine, server.version_tuple),
                                                     reason(exc)))


def _summary():
    return {"channels": 0, "judged": 0, "ok": 0, "watch": 0, "problem": 0, "unknown": 0, "failed": 0,
            "lag_max_s": None}


def read_status(conn):
    """The ``replication-1.0`` report. Read-only.

    MySQL: ``SHOW REPLICA STATUS`` (``SHOW SLAVE STATUS`` before 8.0.22),
    one row per channel, plus ``gtid_mode`` and the Performance Schema
    connection, applier configuration and worker tables. MariaDB: ``SHOW
    ALL SLAVES STATUS`` with every connection name, plus ``gtid_slave_pos``,
    ``gtid_current_pos`` and ``gtid_domain_id`` shown per domain.

    Without the privilege (REPLICATION CLIENT or SUPER on MySQL; SLAVE
    MONITOR on MariaDB) the report has ``available: false`` and a one
    sentence ``message``. A server with no replication configured says so
    (``replica: false``). Connection failures raise ConnectorError; other
    unreadable answers raise ValueError.
    """
    reader = Reader(conn, REPLICATION_TAG)
    server = preflight(reader, EXTRA_VARIABLES)
    show_sql = status_statement(server.engine, server.version_tuple)
    report = {
        "schema_version": SCHEMA_VERSION,
        "engine": server.engine,
        "server_version": server.version or None,
        "captured_at": now(),
        "uptime_s": server.uptime,
        "available": True,
        "message": None,
        "replica": False,
        "statement": show_sql,
        "gtid": _engine_gtid(server),
        "settings": _settings(server),
        "global_skip_counter": _int(server.variables.get("sql_replica_skip_counter", server.variables.get("sql_slave_skip_counter"))),
        "channels": [],
        "capabilities": {"status": False, "performance_schema": server.ps_on, "worker_timestamps": False,
                         "agat": False},
        "notes": [],
        "summary": _summary(),
    }
    try:
        rows = reader.named(show_sql)
    except ConnectorError as exc:
        if _fatal(exc):
            raise
        if denied(exc):
            report["available"] = False
            report["message"] = _privilege_message(server, exc)
            return report
        raise ValueError("Could not read replication status: " + reason(exc))
    if not rows:
        report["message"] = ("This server is not a replica: %s returned no %s." % (
            show_sql, "connection" if server.engine == "mariadb" else "channel"))
        return report
    report["replica"] = True
    report["capabilities"]["status"] = True
    if server.engine == "mariadb":
        report["channels"] = [_mariadb_channel(row) for row in rows]
    else:
        report["channels"] = [_mysql_channel(row) for row in rows]
        if server.ps_on:
            _read_ps(reader, report, server)
        else:
            report["notes"].append("Performance Schema is off, so worker commit timestamps, heartbeats and "
                                   "whether a channel assigns GTIDs to anonymous transactions are not "
                                   "available.")
    for channel in report["channels"]:
        channel["failing_gtid"] = failing_gtid(channel) if channel["sql"]["error"]["errno"] else None
        _judge(channel, server.engine)
        summary = report["summary"]
        if channel["judged"]:
            summary["judged"] += 1
            summary[channel["status"]] += 1
        if channel["sql"]["error"]["errno"]:
            summary["failed"] += 1
        if channel["lag_s"] is not None and channel["judged"]:
            summary["lag_max_s"] = channel["lag_s"] if summary["lag_max_s"] is None \
                else max(summary["lag_max_s"], channel["lag_s"])
    report["summary"]["channels"] = len(report["channels"])
    for channel in report["channels"]:
        if channel["judged"] and channel["sql"]["error"]["errno"] and channel["sql"]["running"] != "Yes":
            channel["relay"] = _usable_relay(report, channel, _peek_relay(reader, server, channel, report["notes"]))
            if not channel["failing_gtid"] and channel["relay"] and channel["relay"].get("gtid") and \
                    channel["sql"]["error"]["errno"] != WORKER_STOPPED_BY_EARLIER:
                # MariaDB 11.4 names no GTID in the error text (seen live); the
                # relay log does, for the transaction the applier restarts at.
                channel["failing_gtid"] = channel["relay"]["gtid"]
            channel["skip_plan"] = plan_skip(report, channel["name"])
    if any(c["judged"] for c in report["channels"]):
        lag_name = "Seconds_Behind_Master" if server.engine == "mariadb" or show_sql == "SHOW SLAVE STATUS" \
            else "Seconds_Behind_Source"
        report["notes"].append(
            "%s compares the original timestamp of the event being applied with the replica's clock, "
            "adjusted by the clock difference measured when the receiver connected. A clock change on "
            "either server after that skews it, and one long transaction shows as a jump, so it is never "
            "the only lag signal." % lag_name)
    return report


def _engine_gtid(server):
    if server.engine == "mariadb":
        slave_pos = _text(server.var("gtid_slave_pos"))
        current_pos = _text(server.var("gtid_current_pos"))
        return {
            "slave_pos": slave_pos,
            "current_pos": current_pos,
            "slave_pos_domains": parse_mariadb_gtids(slave_pos),
            "current_pos_domains": parse_mariadb_gtids(current_pos),
            "domain_id": _int(server.var("gtid_domain_id")),
            "strict_mode": _text(server.var("gtid_strict_mode")),
        }
    return {"mode": _text(server.var("gtid_mode")),
            "enforce_consistency": _text(server.var("enforce_gtid_consistency"))}


def _settings(server):
    if server.engine == "mariadb":
        return {"read_only": _text(server.var("read_only")),
                "parallel_threads": _int(server.var("slave_parallel_threads"))}
    workers = _pick(server.variables, "replica_parallel_workers", "slave_parallel_workers")
    order = _pick(server.variables, "replica_preserve_commit_order", "slave_preserve_commit_order")
    return {"read_only": _text(server.var("read_only")), "super_read_only": _text(server.var("super_read_only")),
            "parallel_workers": _int(workers), "preserve_commit_order": _text(order)}


def _error(row, prefix):
    return {"errno": _int(row.get(prefix + "_Errno")) or 0, "message": _text(row.get(prefix + "_Error")),
            "timestamp": _text(row.get(prefix + "_Error_Timestamp"))}


def _channel_base(name, kind, row, source, io_running, io_state, sql_running, sql_state, lag, position):
    sql_error = _error(row, "Last_SQL")
    table, end_log_pos = _error_details(sql_error["message"])
    sql_error["table"], sql_error["end_log_pos"] = table, end_log_pos
    return {
        "name": name,
        "kind": kind,
        "judged": kind != "group_replication",
        "source": source,
        "io": {"running": _text(io_running), "state": _text(io_state), "error": _error(row, "Last_IO")},
        "sql": {"running": _text(sql_running), "state": _text(sql_state), "error": sql_error,
                "delay_s": _int(row.get("SQL_Delay")), "remaining_delay_s": _int(row.get("SQL_Remaining_Delay"))},
        "lag_s": _int(lag),
        "lag_notes": [],
        "lag_signals": {},
        "position": position,
        "worker": {},
        "agat": None,
        "failing_gtid": None,
        "relay": None,
        "status": "unknown",
        "status_why": [],
    }


def _mysql_channel(row):
    name = _text(row.get("Channel_Name")) or ""
    kind = "group_replication" if name in GROUP_REPLICATION_CHANNELS else "replication"
    source = {"host": _text(_pick(row, "Source_Host", "Master_Host")),
              "port": _int(_pick(row, "Source_Port", "Master_Port")),
              "uuid": _text(_pick(row, "Source_UUID", "Master_UUID")),
              "server_id": _int(_pick(row, "Source_Server_Id", "Master_Server_Id"))}
    position = {
        "auto_position": _pick(row, "Auto_Position") in ("1", 1),
        "retrieved_gtid_set": _text(row.get("Retrieved_Gtid_Set")),
        "executed_gtid_set": _text(row.get("Executed_Gtid_Set")),
        "source_log_file": _text(_pick(row, "Source_Log_File", "Master_Log_File")),
        "read_source_log_pos": _int(_pick(row, "Read_Source_Log_Pos", "Read_Master_Log_Pos")),
        "exec_file": _text(_pick(row, "Relay_Source_Log_File", "Relay_Master_Log_File")),
        "exec_pos": _int(_pick(row, "Exec_Source_Log_Pos", "Exec_Master_Log_Pos")),
        "relay_log_file": _text(row.get("Relay_Log_File")),
        "relay_log_pos": _int(row.get("Relay_Log_Pos")),
        "skip_counter": _int(row.get("Skip_Counter")),
    }
    channel = _channel_base(
        name, kind, row, source,
        _pick(row, "Replica_IO_Running", "Slave_IO_Running"), _pick(row, "Replica_IO_State", "Slave_IO_State"),
        _pick(row, "Replica_SQL_Running", "Slave_SQL_Running"),
        _pick(row, "Replica_SQL_Running_State", "Slave_SQL_Running_State"),
        _pick(row, "Seconds_Behind_Source", "Seconds_Behind_Master"), position)
    channel["lag_signals"] = {"applying_lag_s": None, "last_apply_lag_s": None, "heartbeat_interval_s": None,
                              "heartbeats_received": None, "heartbeat_age_s": None, "last_queued_age_s": None,
                              "receiver_idle_s": None}
    return channel


def _mariadb_channel(row):
    name = _text(row.get("Connection_name")) or ""
    gtid_io_pos = _text(row.get("Gtid_IO_Pos"))
    source = {"host": _text(row.get("Master_Host")), "port": _int(row.get("Master_Port")), "uuid": None,
              "server_id": _int(row.get("Master_Server_Id"))}
    position = {
        "using_gtid": _text(row.get("Using_Gtid")) or "No",
        "gtid_io_pos": gtid_io_pos,
        "gtid_io_domains": parse_mariadb_gtids(gtid_io_pos),
        # The Gtid_Slave_Pos column is gtid_current_pos (slave state merged with
        # the binary log; Ma/sql/sql_show.cc:9122, Ma/sql/sql_repl.cc:5680-5690).
        "gtid_current_pos": _text(row.get("Gtid_Slave_Pos")),
        "source_log_file": _text(row.get("Master_Log_File")),
        "read_source_log_pos": _int(row.get("Read_Master_Log_Pos")),
        "exec_file": _text(row.get("Relay_Master_Log_File")),
        "exec_pos": _int(row.get("Exec_Master_Log_Pos")),
        "relay_log_file": _text(row.get("Relay_Log_File")),
        "relay_log_pos": _int(row.get("Relay_Log_Pos")),
        "skip_counter": _int(row.get("Skip_Counter")),
        "parallel_mode": _text(row.get("Parallel_Mode")),
    }
    channel = _channel_base(name, "replication", row, source, row.get("Slave_IO_Running"),
                            row.get("Slave_IO_State"), row.get("Slave_SQL_Running"),
                            row.get("Slave_SQL_Running_State"), row.get("Seconds_Behind_Master"), position)
    channel["lag_signals"] = {"heartbeat_period_s": _float(row.get("Slave_heartbeat_period")),
                              "heartbeats_received": _int(row.get("Slave_received_heartbeats"))}
    return channel


def _read_ps(reader, report, server):
    """Attach Performance Schema heartbeat, AGAT and worker data (MySQL)."""
    notes = report["notes"]
    channels = {c["name"]: c for c in report["channels"]}

    def rows_for(sql, what):
        rows = reader.optional(sql, what, notes)
        return [(channels.get(_text(r[0]) or ""), r) for r in rows or () if r] if rows is not None else None
    for channel, row in rows_for(_PS_HEARTBEAT_SQL, "replication_connection_configuration") or ():
        if channel is not None and len(row) > 1:
            channel["lag_signals"]["heartbeat_interval_s"] = _float(row[1])
    if server.version_tuple >= (8, 0, 23):
        agat = rows_for(_PS_AGAT_SQL, "replication_applier_configuration")
        if agat is not None:
            report["capabilities"]["agat"] = True
            for channel, row in agat:
                if channel is not None and len(row) > 1:
                    channel["agat"] = _text(row[1])
    else:
        report["capabilities"]["agat"] = True
        for channel in report["channels"]:
            channel["agat"] = "OFF"
    for channel, row in rows_for(_PS_CONNECTION_SQL, "replication_connection_status") or ():
        if channel is None:
            continue
        row = list(row) + [None] * (8 - len(row))
        signals = channel["lag_signals"]
        signals["heartbeats_received"] = _int(row[2])
        signals["heartbeat_age_s"] = _seconds(row[6])
        signals["last_queued_age_s"] = _seconds(row[7])
        ages = [a for a in (signals["heartbeat_age_s"], signals["last_queued_age_s"]) if a is not None]
        signals["receiver_idle_s"] = min(ages) if ages else None
        channel["receiver"] = {"service_state": _text(row[1]), "last_queued_transaction": _text(row[4])}
    workers = rows_for(_PS_WORKER_SQL, "replication_applier_status_by_worker")
    if workers is None:
        return
    report["capabilities"]["worker_timestamps"] = True
    by_channel = {}
    for channel, row in workers:
        if channel is not None:
            by_channel.setdefault(channel["name"], []).append(list(row) + [None] * (11 - len(row)))
    for name, rows in by_channel.items():
        channel = channels[name]
        failing = [r for r in rows if _int(r[2])]
        # The worker that failed first, not one that stopped behind it (3030).
        failing.sort(key=lambda r: (_int(r[2]) == WORKER_STOPPED_BY_EARLIER, _text(r[4]) or ""))
        applying = [_seconds(r[7]) for r in rows if _text(r[5]) and _seconds(r[7]) is not None]
        last = max(rows, key=lambda r: _text(r[9]) or "")
        worker = {"workers": len(rows), "error": {"errno": 0, "message": None, "timestamp": None},
                  "failing_worker_id": None, "failing_applying_transaction": None,
                  "applying_transactions": [_text(r[5]) for r in rows if _text(r[5])],
                  "last_applied_transaction": _text(last[8]), "last_applied_end_apply": _ts(last[9])}
        if failing:
            row = failing[0]
            worker["error"] = {"errno": _int(row[2]) or 0, "message": _text(row[3]), "timestamp": _ts(row[4])}
            worker["failing_worker_id"] = _int(row[1])
            # Live (8.4.11, 8.0.46, 4 workers): a worker stopped on an error keeps
            # the failing GTID in APPLYING_TRANSACTION.
            worker["failing_applying_transaction"] = _text(row[5])
            sql_error = channel["sql"]["error"]
            table, end_pos = _error_details(worker["error"]["message"])
            if sql_error["errno"] == WORKER_STOPPED_BY_EARLIER and worker["error"]["errno"] != WORKER_STOPPED_BY_EARLIER:
                # The status text names the stopped worker's transaction.
                sql_error["table"], sql_error["end_log_pos"] = table, end_pos
            else:
                sql_error["table"] = sql_error["table"] or table
                sql_error["end_log_pos"] = sql_error["end_log_pos"] or end_pos
        channel["worker"] = worker
        if not failing:
            channel["lag_signals"]["applying_lag_s"] = max(applying) if applying else None
        channel["lag_signals"]["last_apply_lag_s"] = _seconds(last[10])


def relay_statement(engine, channel):
    """SHOW RELAYLOG EVENTS from the applier's position, for one channel."""
    position = channel.get("position") or {}
    relay_file, relay_pos = position.get("relay_log_file"), position.get("relay_log_pos")
    if not relay_file or relay_pos is None:
        return None
    if engine == "mariadb":
        return "SHOW RELAYLOG '%s' EVENTS IN '%s' FROM %d LIMIT %d" % (
            _qtext(channel["name"]), _qtext(relay_file), relay_pos, RELAY_PEEK_EVENTS)
    return "SHOW RELAYLOG EVENTS IN '%s' FROM %d LIMIT %d FOR CHANNEL '%s'" % (
        _qtext(relay_file), relay_pos, RELAY_PEEK_EVENTS, _qtext(channel["name"]))


_GTID_NEXT_INFO = re.compile(r"GTID_NEXT\s*=\s*'([^']+)'", re.I)
_MARIADB_GTID_INFO = re.compile(r"^(BEGIN )?GTID (\d+-\d+-\d+)")


def parse_relay_transaction(rows):
    """The transaction at the start of SHOW RELAYLOG EVENTS rows (read by
    name): ``{"gtid", "anonymous", "events", "end_log_pos", "complete",
    "event_end_positions", "tables"}``. ``end_log_pos`` is the source
    binary log position where the transaction ends (its commit event);
    ``complete`` is False when the commit was not among the rows read."""
    trx = {"gtid": None, "anonymous": False, "events": 0, "end_log_pos": None, "complete": False,
           "event_end_positions": [], "tables": []}
    started, standalone, previous = False, False, None
    for row in rows:
        etype = row.get("Event_type") or ""
        info = row.get("Info") or ""
        end_pos = _int(row.get("End_log_pos"))
        if not started and etype in _RELAY_META:
            if etype == "Rotate":
                rotated = re.match(r"^([^;\s]+);pos=\d+", info)
                if rotated:
                    trx["source_log_file"] = rotated.group(1)
            continue
        started = True
        trx["events"] += 1
        if end_pos:
            trx["event_end_positions"].append(end_pos)
        upper = info.strip().upper()
        if etype in ("Gtid", "Gtid_tagged") and trx["events"] == 1:
            match = _GTID_NEXT_INFO.search(info)
            maria = _MARIADB_GTID_INFO.match(info.strip())
            previous = "gtid"
            if maria:
                trx["gtid"] = maria.group(2)
                standalone = not maria.group(1)
                # "BEGIN GTID" opens the transaction itself: no BEGIN Query follows.
                previous = "begin" if maria.group(1) else "gtid"
            elif match:
                trx["gtid"] = match.group(1)
            continue
        if etype == "Anonymous_Gtid" and trx["events"] == 1:
            trx["anonymous"] = True
            previous = "gtid"
            continue
        if etype == "Table_map":
            table = re.search(r"\(([^)]+)\)", info)
            if table and table.group(1) not in trx["tables"]:
                trx["tables"].append(table.group(1))
        ends = etype in ("Xid", "XA_prepare") or (etype == "Query" and upper in ("COMMIT", "ROLLBACK")) or \
            (etype == "Query" and not upper.startswith("BEGIN") and (previous == "gtid" or standalone))
        previous = etype
        if ends:
            trx["end_log_pos"] = end_pos or None
            trx["complete"] = True
            break
    return trx


def _peek_relay(reader, server, channel, notes):
    """Which transaction the applier restarts at (and so which one a skip
    counter of 1 skips). None when the relay log cannot be read (MySQL needs
    REPLICATION SLAVE, M/sql/sql_show.cc:667-669; MariaDB SLAVE MONITOR,
    Ma/sql/privilege.h:545)."""
    sql = relay_statement(server.engine, channel)
    if sql is None:
        return None
    try:
        rows = reader.named(sql)
    except ConnectorError as exc:
        if _fatal(exc):
            raise
        notes.append("Could not read the relay log of %r to show the transaction a skip would pass: %s"
                     % (channel["name"], reason(exc)))
        return None
    except ValueError as exc:
        notes.append("Could not read the relay log of %r: %s" % (channel["name"], exc))
        return None
    return parse_relay_transaction(rows)


def _usable_relay(report, channel, relay):
    """The peeked transaction, or None when it cannot be the one that failed:
    nothing after the position (a GTID-based parallel applier restarts from
    gtid_slave_pos, and MariaDB then reports the relay log start; seen live
    on 11.4 with slave_parallel_threads=4), or a transaction already applied."""
    if relay is None:
        return None
    applied = False
    gtid = relay.get("gtid")
    if gtid and report["engine"] == "mariadb" and _GTID_ITEM.match(gtid):
        domain, _server, seq = (int(p) for p in gtid.split("-"))
        done = {d["domain"]: d["seq"] for d in (report.get("gtid") or {}).get("slave_pos_domains") or ()}
        applied = domain in done and done[domain] >= seq
    elif gtid and report["engine"] == "mysql":
        applied = gtid_in_set(gtid, (channel.get("position") or {}).get("executed_gtid_set"))
    if relay.get("events") and not applied:
        return relay
    report["notes"].append("The relay log at the applier's position of %r does not start with the failed "
                           "transaction, so myflames cannot show which transaction a skip counter passes."
                           % channel["name"])
    return None


def _worse(status, new):
    order = {"ok": 0, "unknown": 1, "watch": 2, "problem": 3}
    return new if order[new] > order[status] else status


def _judge(channel, engine):
    """status + reasons, and the Seconds_Behind_* caveats for this channel."""
    if channel["kind"] == "group_replication":
        channel["status"], channel["status_why"] = "unknown", [GR_NOTE]
        return
    status, why = "ok", []
    io, sql = channel["io"], channel["sql"]
    if sql["error"]["errno"]:
        status = "problem"
        why.append("The applier (SQL thread) stopped with error %d: %s" % (
            sql["error"]["errno"], sql["error"]["message"] or "?"))
    elif sql["running"] != "Yes":
        status = "problem"
        why.append("The applier (SQL thread) is not running.")
    if io["error"]["errno"]:
        status = _worse(status, "problem" if io["running"] != "Yes" else "watch")
        why.append("The receiver (IO thread) reported error %d: %s" % (
            io["error"]["errno"], io["error"]["message"] or "?"))
    elif io["running"] == "No":
        status = "problem"
        why.append("The receiver (IO thread) is not running.")
    elif io["running"] == "Connecting":
        status = _worse(status, "watch")
        why.append("The receiver is connecting to the source.")
    lag = channel["lag_s"]
    delay = sql.get("delay_s") or 0
    lag_name = "Seconds_Behind_Master" if engine == "mariadb" else "Seconds_Behind_Source"
    if lag is not None and lag - delay > LAG_WATCH_S:
        status = _worse(status, "watch")
        why.append("%s is %d s%s." % (lag_name, lag, " (SQL_Delay is %d s)" % delay if delay else ""))
    if lag is None and io["running"] == "Yes" and sql["running"] == "Yes":
        status = _worse(status, "unknown")
        why.append("%s is NULL although both threads run, so the lag cannot be told." % lag_name)
    signals = channel["lag_signals"]
    if engine == "mysql":
        _mysql_lag_notes(channel)
        applying = signals.get("applying_lag_s")
        if applying is not None and applying > LAG_WATCH_S + delay:
            status = _worse(status, "watch")
            why.append("A transaction that committed on the source %s ago is still being applied."
                       % _duration_text(applying))
        interval = signals.get("heartbeat_interval_s")
        idle = signals.get("receiver_idle_s")
        if io["running"] == "Yes" and interval and idle is not None and idle > max(2.0 * interval, 60.0):
            status = _worse(status, "watch")
            why.append("Nothing arrived from the source for %s, not even a heartbeat (interval %g s); the "
                       "link may be broken." % (_duration_text(idle), interval))
    else:
        _mariadb_lag_notes(channel)
    if delay:
        channel["lag_notes"].append("This channel delays the applier by %d s on purpose (SQL_Delay), and "
                                    "the lag includes it." % delay)
    channel["status"], channel["status_why"] = status, why


def _mysql_lag_notes(channel):
    lag = channel["lag_s"]
    notes = channel["lag_notes"]
    if lag is None:
        if channel["sql"]["running"] != "Yes":
            notes.append("Seconds_Behind_Source is NULL because the SQL thread is not running.")
        elif channel["io"]["running"] != "Yes":
            notes.append("Seconds_Behind_Source is NULL: with the receiver stopped, MySQL reports NULL once "
                         "the applier has caught up with what was received (it still counts before that).")
    elif lag == 0:
        notes.append("Seconds_Behind_Source is 0 because the applier has caught up with what the receiver "
                     "has, which is also true when the receiver is far behind the source.")
    signals = channel["lag_signals"]
    if signals.get("applying_lag_s") is not None or signals.get("last_apply_lag_s") is not None:
        notes.append("Worker lag compares the source's commit timestamp with the replica's clock: the two "
                     "servers' clocks must be in sync for it to be exact.")


def _mariadb_lag_notes(channel):
    lag = channel["lag_s"]
    notes = channel["lag_notes"]
    if lag is None:
        notes.append("Seconds_Behind_Master is NULL unless the IO thread is connected and reading and the "
                     "SQL thread runs.")
    elif lag == 0:
        notes.append("Seconds_Behind_Master is 0 while the applier is idle; one long transaction on the "
                     "source shows as a jump.")
    if channel["lag_signals"].get("heartbeat_period_s"):
        notes.append("Slave_received_heartbeats grows between two reads on a healthy, idle link; read the "
                     "view again after the heartbeat period to tell an idle source from a broken link.")


def find_channel(report, name):
    for channel in report.get("channels") or ():
        if channel.get("name") == name:
            return channel
    return None


# ---- skip one failed transaction ------------------------------------------------

_MYSQL_GTID_PRIVS = ("SET GTID_NEXT needs SUPER, SYSTEM_VARIABLES_ADMIN, SESSION_VARIABLES_ADMIN or "
                     "REPLICATION_APPLIER; START REPLICA needs REPLICATION_SLAVE_ADMIN or SUPER.")
_MYSQL_TAG_PRIVS = (" A tagged GTID (uuid:tag:n) also needs TRANSACTION_GTID_TAG, with SYSTEM_VARIABLES_ADMIN, "
                    "SESSION_VARIABLES_ADMIN or REPLICATION_APPLIER (SUPER is not accepted for it).")
_MYSQL_COUNTER_PRIVS = ("SET GLOBAL %(counter)s needs SYSTEM_VARIABLES_ADMIN or SUPER; %(start)s needs "
                        "REPLICATION_SLAVE_ADMIN or SUPER.")
_MARIADB_PRIVS = ("SET GLOBAL sql_slave_skip_counter needs SUPER; START SLAVE needs REPLICATION SLAVE ADMIN.")

_COUNTER_NOTE = ("The counter counts events, but when it reaches 1 inside an event group it keeps skipping "
                 "to the end of that group, so 1 skips the rest of the current transaction; myflames only "
                 "ever sets 1. It is global (no FOR CHANNEL): the START below copies it to this channel and "
                 "resets it to 0.")
_PERMISSIVE_GTID_NOTE = ("At gtid_mode %s the skip counter would also pass this transaction, but its GTID would "
                         "not enter gtid_executed (seen live on MySQL 8.4.11), so a source using auto-position "
                         "would send it again; the empty transaction records it.")
_PERMISSIVE_NOTE = ("gtid_mode %s honors the counter (START REPLICA ignores it only at gtid_mode ON without "
                    "AGAT).")
_MTA_NOTE = ("With a multithreaded applier and replica_preserve_commit_order OFF, the applier restarts from "
             "the oldest transaction not yet committed, which may not be the one that failed; myflames "
             "checks after the skip whether the failed transaction was passed.")
_MARIADB_COUNTER_NOTE = ("The counter counts events and finishes the current group; a GTID event opens a "
                         "group, so 1 skips one transaction. It applies to the connection named by "
                         "default_master_connection, and the SQL thread must be stopped when it is set "
                         "(error 1198 otherwise).")


def plan_skip(report, channel):
    """The exact statements to skip ONE failed transaction on *channel*.

    *report* is the ``replication-1.0`` dict from :func:`read_status`;
    *channel* is its name (``""`` for the default MySQL channel or the
    unnamed MariaDB connection). The method depends on engine and GTID mode:

    * MySQL, the failed transaction has a GTID, gtid_mode is not OFF and the
      channel has no AGAT: commit an empty transaction with that GTID and
      start the applier (``empty_gtid_transaction``). At gtid_mode=ON the
      skip counter would skip nothing (START REPLICA drops it without AGAT);
      in the permissive modes it would skip the transaction without
      recording its GTID in gtid_executed (seen live), leaving a gap that
      auto-position fills again.
    * MySQL, an anonymous failed transaction (gtid_mode OFF, OFF_PERMISSIVE
      or ON_PERMISSIVE), or a channel with AGAT: set the skip counter to 1
      and start the applier for this channel (``skip_counter``;
      ``sql_slave_skip_counter`` before 8.0.26, ``START SLAVE`` before
      8.0.22). The relay log must show that the transaction at the
      applier's position is the one that failed.
    * MariaDB: select the connection with ``default_master_connection``, set
      the skip counter to 1 and start its applier. With GTID, parallel
      replication and more than one domain in gtid_slave_pos the server
      refuses the counter (1966); the plan is then ``available: false`` and
      explains setting gtid_slave_pos, which myflames does not automate.

    Raises ValueError for a channel that is missing, a Group Replication
    channel, or one whose applier has no error. Otherwise returns the plan:
    ``available`` (False with ``message`` when no safe method exists),
    ``method``, ``statements``, ``identity`` (what :func:`skip_one` checks
    again before sending), ``skip`` (the GTID, end_log_pos and table being
    skipped), the drift warning, next steps, the privilege hint and notes.
    """
    if not isinstance(report, dict) or not isinstance(report.get("channels"), list):
        raise ValueError("Pass the replication-1.0 report from replication.read_status.")
    engine = report.get("engine")
    if engine not in ("mysql", "mariadb"):
        raise ValueError("The report does not say whether this is a MySQL or MariaDB server.")
    found = find_channel(report, channel)
    if found is None:
        raise ValueError("This server has no replication channel named %r." % (channel,))
    if found.get("kind") == "group_replication":
        raise ValueError("%r is a Group Replication channel; myflames does not skip transactions there."
                         % (channel,))
    error = found["sql"]["error"]
    if not error.get("errno"):
        raise ValueError("Channel %r has no failed transaction to skip: its applier reports no error."
                         % (channel,))
    position = found.get("position") or {}
    relay = found.get("relay") or {}
    gtid = found.get("failing_gtid") or relay.get("gtid")
    plan = {
        "channel": found["name"],
        "engine": engine,
        "server_version": report.get("server_version"),
        "available": True,
        "message": None,
        "method": None,
        "statements": [],
        "identity": {
            "channel": found["name"], "method": None,
            "last_sql_errno": error["errno"], "last_sql_error": error.get("message"),
            "last_sql_error_timestamp": error.get("timestamp"), "gtid": gtid,
            "position": {k: position.get(k) for k in ("relay_log_file", "relay_log_pos", "exec_file", "exec_pos")},
            "source": {k: (found.get("source") or {}).get(k) for k in ("host", "port", "uuid", "server_id")},
        },
        "skip": {"gtid": gtid, "exec_file": position.get("exec_file"), "exec_pos": position.get("exec_pos"),
                 "end_log_pos": relay.get("end_log_pos"), "error_end_log_pos": error.get("end_log_pos"),
                 "table": error.get("table") or (relay.get("tables") or [None])[0],
                 "tables": list(relay.get("tables") or ([error["table"]] if error.get("table") else [])),
                 "relay_checked": bool(relay)},
        "error": {"errno": error["errno"], "message": error.get("message"), "timestamp": error.get("timestamp")},
        "drift_warning": DRIFT_WARNING,
        "after": list(AFTER_SKIP_STEPS),
        "privileges": "",
        "notes": [],
    }
    if error["errno"] == WORKER_STOPPED_BY_EARLIER:
        failed = effective_error(found)
        if failed is None:
            plan["available"] = False
            plan["message"] = ("Error 3030 means this worker stopped because an earlier transaction failed, and no "
                               "worker shows that failure, so myflames cannot tell which transaction to skip. "
                               "Nothing is planned; read performance_schema.replication_applier_status_by_worker.")
            plan["identity"]["method"] = None
            return plan
        plan["error"] = {"errno": failed.get("errno"), "message": failed.get("message"),
                         "timestamp": failed.get("timestamp"), "table": error.get("table"),
                         "end_log_pos": error.get("end_log_pos")}
        plan["notes"].append("The status reports error 3030: a worker stopped because an earlier transaction "
                             "failed, and rolled its own transaction back. myflames skips the earlier one (error %s), "
                             "never the one that stopped." % failed.get("errno"))
    if engine == "mariadb":
        _mariadb_plan(plan, report, found)
    else:
        _mysql_plan(plan, report, found)
    if plan["available"] and plan["method"] == "skip_counter":
        _check_counter_target(plan, found, report)
    if engine == "mysql" and (report.get("global_skip_counter") != 0 or position.get("skip_counter") != 0):
        plan["available"] = False
        plan["message"] = ("The global skip counter is armed or unreadable. Nothing is planned: inspect "
                           "@@GLOBAL.sql_replica_skip_counter and resolve it manually before any START.")
    plan["identity"]["method"] = plan["method"]
    return plan


def _check_counter_target(plan, channel, report):
    """A counter may only pass an identified, complete failed transaction."""
    relay = channel.get("relay") or {}
    skip = plan["skip"]
    error_gtid = failing_gtid(channel)
    error_end = skip.get("error_end_log_pos")
    error_text = channel["sql"]["error"].get("message") or ""
    error_file = re.search(r"(?:source|master) log ['\"]?([^,;\s'\"]+)", error_text, re.I)
    relay_file = relay.get("source_log_file") or (channel.get("position") or {}).get("exec_file")
    problem = None
    if not relay or not relay.get("complete"):
        problem = "The relay transaction is missing or incomplete; myflames could not confirm the failed transaction."
    elif error_gtid and error_gtid != relay.get("gtid"):
        problem = "The relay log starts at a transaction that did not fail (the error names another GTID)."
    elif error_file and error_file.group(1) != relay_file:
        problem = "The relay transaction and failed event belong to different source log files."
    elif error_end and not error_gtid and not error_file:
        problem = "The failed anonymous event has no source log filename; its offset alone is ambiguous."
    elif error_end and error_end not in relay.get("event_end_positions", ()):
        problem = "The relay log starts at a transaction that did not fail (the failed event is outside it)."
    elif not error_gtid and not error_end:
        # A serial MariaDB applier reports end_log_pos 0 for row errors.
        # Its complete relay group at the stopped position identifies the failure;
        # a parallel applier may instead expose an earlier coordinator position.
        serial_maria = report["engine"] == "mariadb" and (report.get("settings") or {}).get("parallel_threads") == 0
        if not serial_maria:
            problem = "The error does not identify the transaction at the relay position."
    if (channel.get("position") or {}).get("skip_counter") != 0:
        problem = "A skip counter is already armed; another START could consume it."
    if problem:
        plan["available"] = False
        plan["message"] = (problem + " Nothing is planned. Recover manually: stop the applier, inspect the complete "
                           "relay transaction and worker error, repair or resynchronize the affected data, then "
                           "restart the applier. Do not advance a counter on a guessed position.")


def _mysql_plan(plan, report, channel):
    gtid_mode = ((report.get("gtid") or {}).get("mode") or "").upper()
    version = _version_tuple(report.get("server_version"))
    start = "START REPLICA" if version >= (8, 0, 22) else "START SLAVE"
    counter = "sql_replica_skip_counter" if version >= (8, 0, 26) else "sql_slave_skip_counter"
    name = _qtext(channel["name"])
    if gtid_mode not in ("ON", "ON_PERMISSIVE", "OFF_PERMISSIVE", "OFF"):
        plan["available"] = False
        plan["message"] = ("myflames could not read gtid_mode, so it cannot tell which skip method this "
                           "replica honors. Nothing is planned.")
        return
    agat = (channel.get("agat") or "").upper() or None
    if gtid_mode == "ON" and agat is None:
        plan["available"] = False
        plan["message"] = ("gtid_mode is ON, but myflames could not read whether this channel assigns GTIDs "
                           "to anonymous transactions (performance_schema.replication_applier_configuration), "
                           "so it cannot choose between an empty GTID transaction and the skip counter.")
        return
    agat_on = agat not in (None, "OFF")
    gtid = plan["skip"]["gtid"]
    has_gtid = bool(gtid and _MYSQL_GTID.match(gtid))
    if not agat_on and gtid_mode != "OFF" and has_gtid:
        plan["method"] = "empty_gtid_transaction"
        plan["statements"] = ["SET GTID_NEXT = '%s'" % _qtext(gtid), "BEGIN", "COMMIT",
                              "SET GTID_NEXT = 'AUTOMATIC'", "%s SQL_THREAD FOR CHANNEL '%s'" % (start, name)]
        plan["privileges"] = _MYSQL_GTID_PRIVS + (_MYSQL_TAG_PRIVS if _MYSQL_GTID.match(gtid).group(2) else "")
        plan["notes"].append("The empty transaction takes the failing transaction's GTID, so the applier "
                             "treats it as already applied and continues with the next one.")
        if gtid_mode != "ON":
            plan["notes"].append(_PERMISSIVE_GTID_NOTE % gtid_mode)
        return
    if gtid_mode == "ON" and not agat_on:
        plan["available"] = False
        plan["message"] = ("gtid_mode is ON, so the skip counter would be dropped by START REPLICA and skip "
                           "nothing, and the failing GTID could not be read from the error text, the worker's "
                           "APPLYING_TRANSACTION or the relay log. Read the channel again.")
        return
    plan["method"] = "skip_counter"
    plan["statements"] = ["SET GLOBAL %s = 1" % counter, "%s SQL_THREAD FOR CHANNEL '%s'" % (start, name)]
    plan["privileges"] = _MYSQL_COUNTER_PRIVS % {"counter": counter, "start": start}
    plan["notes"].append(_COUNTER_NOTE)
    if agat_on:
        plan["notes"].append("This channel assigns GTIDs to anonymous transactions (AGAT %s), so START "
                             "REPLICA keeps the counter." % agat)
    elif gtid_mode != "OFF":
        plan["notes"].append(_PERMISSIVE_NOTE % gtid_mode)
    settings = report.get("settings") or {}
    if (settings.get("parallel_workers") or 0) > 1 and (settings.get("preserve_commit_order") or "").upper() \
            not in ("ON", "1"):
        plan["notes"].append(_MTA_NOTE)


def _mariadb_plan(plan, report, channel):
    position = channel.get("position") or {}
    gtid = report.get("gtid") or {}
    settings = report.get("settings") or {}
    domains = gtid.get("slave_pos_domains") or []
    parallel = (settings.get("parallel_threads") or 0) > 0 and \
        (position.get("parallel_mode") or "").lower() not in ("", "none")
    if position.get("using_gtid") not in (None, "No") and parallel and len(domains) > 1:
        plan["available"] = False
        plan["message"] = (
            "This connection uses GTID with parallel replication, and gtid_slave_pos has %d domains, so "
            "MariaDB refuses the skip counter (error 1966). The fix is to set gtid_slave_pos past the failed "
            "transaction in its domain while keeping every other domain's position (now %s). That needs "
            "exact positions, and the same variable holds every connection's position, so myflames shows it "
            "and does not automate it." % (len(domains), gtid.get("slave_pos") or "?"))
        return
    name = _qtext(channel["name"])
    plan["method"] = "skip_counter"
    plan["statements"] = ["SET default_master_connection = '%s'" % name, "SET GLOBAL sql_slave_skip_counter = 1",
                          "START SLAVE '%s' SQL_THREAD" % name]
    plan["privileges"] = _MARIADB_PRIVS
    plan["notes"].append(_MARIADB_COUNTER_NOTE)


# ---- the guarded write -----------------------------------------------------------

def check_expect(expect):
    """Require the whole confirmed identity, including explicit null fields."""
    message = ("Send the complete identity you confirmed: channel, method, failure fields, source, and a "
               "validated GTID or complete relay and applied file-and-position identity.")
    required = ("channel", "method", "last_sql_errno", "last_sql_error", "last_sql_error_timestamp",
                "gtid", "position", "source")
    if not isinstance(expect, dict) or any(k not in expect for k in required):
        raise ValueError(message)
    if not isinstance(expect["channel"], str) or len(expect["channel"]) > 256:
        raise ValueError(message)
    errno = expect["last_sql_errno"]
    if isinstance(errno, bool) or not isinstance(errno, int) or errno <= 0:
        raise ValueError(message)
    if expect["method"] not in ("skip_counter", "empty_gtid_transaction"):
        raise ValueError(message)
    for key in ("last_sql_error", "last_sql_error_timestamp", "gtid"):
        if expect[key] is not None and not isinstance(expect[key], str):
            raise ValueError(message)
    position, source = expect["position"], expect["source"]
    pkeys = ("relay_log_file", "relay_log_pos", "exec_file", "exec_pos")
    if not isinstance(position, dict) or any(k not in position for k in pkeys):
        raise ValueError(message)
    if not isinstance(source, dict) or any(k not in source for k in ("host", "port", "uuid", "server_id")):
        raise ValueError(message)
    if not isinstance(source["host"], str) or not source["host"] or isinstance(source["port"], bool) or \
            not isinstance(source["port"], int) or not 1 <= source["port"] <= 65535:
        raise ValueError(message)
    if source["uuid"] is not None and not isinstance(source["uuid"], str):
        raise ValueError(message)
    if source["server_id"] is not None and (isinstance(source["server_id"], bool) or
                                           not isinstance(source["server_id"], int) or source["server_id"] < 0):
        raise ValueError(message)
    for key in ("relay_log_file", "exec_file"):
        if position[key] is not None and not isinstance(position[key], str):
            raise ValueError(message)
    for key in ("relay_log_pos", "exec_pos"):
        if position[key] is not None and (isinstance(position[key], bool) or
                                         not isinstance(position[key], int) or position[key] < 0):
            raise ValueError(message)
    gtid = expect["gtid"]
    match = (_MYSQL_GTID.fullmatch(gtid) or _GTID_ITEM.fullmatch(gtid)) if gtid else None
    valid_gtid = bool(match and int(match.group(3)) > 0)
    if gtid is not None and not valid_gtid:
        raise ValueError(message)
    if not valid_gtid and not all(position[k] is not None and position[k] != "" for k in pkeys):
        raise ValueError(message)
    return {k: dict(expect[k]) if k in ("position", "source") else expect[k] for k in required}


def verify_identity(channel, plan, expect):
    """(verified field names, refusal text or None): is it still the failure
    the person confirmed? Explicit nulls are compared too."""
    verified, diffs = [], []
    current = plan["identity"]

    def compare(label, now_value, then_value):
        if now_value != then_value:
            diffs.append("%s is now %s" % (label, now_value if now_value not in (None, "") else "empty"))
        else:
            verified.append(label)
    if channel["sql"]["running"] == "Yes":
        diffs.append("the applier is running again")
    compare("the skip method", current["method"], expect.get("method"))
    compare("the error number", current["last_sql_errno"], expect["last_sql_errno"])
    compare("the error message", current["last_sql_error"], expect.get("last_sql_error"))
    compare("the error time", current["last_sql_error_timestamp"], expect.get("last_sql_error_timestamp"))
    compare("the failing GTID", current["gtid"], expect.get("gtid"))
    for key, label in (("relay_log_file", "the relay log file"), ("relay_log_pos", "the relay log position"),
                       ("exec_file", "the applied source log file"), ("exec_pos", "the applied source position")):
        compare(label, current["position"].get(key), (expect.get("position") or {}).get(key))
    for key, label in (("host", "the source host"), ("port", "the source port"), ("uuid", "the source UUID"),
                       ("server_id", "the source server ID")):
        compare(label, current["source"].get(key), (expect.get("source") or {}).get(key))
    if diffs:
        return verified, ("Channel %r changed since you confirmed (%s): someone may have fixed or skipped it "
                          "already. Nothing was sent; read the channel again." % (channel["name"],
                                                                                 "; ".join(diffs)))
    return verified, None


def _tagged(statements):
    return [SKIP_TAG + s for s in statements]


def _script(statements):
    """One client call, one statement per line, so the client's "at line N"
    names the statement that failed."""
    return ";\n".join(_tagged(statements)) + ";"


def _sentence(text):
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?" else text + "."


def _server_error(exc, statements, privileges):
    """(status, message) for a server error from the skip script."""
    code = dbops.error_code(str(exc))
    line = _LINE.search(str(exc))
    ran = ""
    if line and 1 < int(line.group(1)) <= len(statements):
        done = statements[:int(line.group(1)) - 1]
        ran = " These statements ran before it: %s." % "; ".join(done)
    elif line and int(line.group(1)) == 1:
        ran = " Nothing ran before it."
    if code in _DENIED:
        return "denied", "The server refused it for lack of privileges (%s). %s%s" % (reason(exc), privileges, ran)
    if code in _REFUSED_BY_SERVER:
        return "refused_by_server", "The server refused it: %s.%s" % (reason(exc), ran)
    return "failed", "The server returned an error: %s.%s" % (reason(exc), ran)


def _after_view(report, channel, plan):
    """The re-read channel, and whether the planned transaction was passed."""
    if channel is None:
        return {"present": False}
    error = channel["sql"]["error"]
    position = channel.get("position") or {}
    view = {"present": True, "io_running": channel["io"]["running"], "sql_running": channel["sql"]["running"],
            "last_sql_errno": error["errno"], "last_sql_error": error.get("message"), "lag_s": channel["lag_s"],
            "status": channel["status"], "exec_file": position.get("exec_file"), "exec_pos": position.get("exec_pos"),
            "skip_counter": position.get("skip_counter"), "global_skip_counter": report.get("global_skip_counter"),
            "failing_gtid": channel.get("failing_gtid"),
            "passed": None}
    skip = plan["skip"]
    same_failure = bool(error["errno"]) and error["errno"] == plan["error"]["errno"] and (
        (skip.get("gtid") and channel.get("failing_gtid") == skip["gtid"]) or
        (not skip.get("gtid") and position.get("exec_pos") == skip.get("exec_pos")
         and position.get("exec_file") == skip.get("exec_file")))
    if plan["engine"] == "mysql" and plan["method"] == "empty_gtid_transaction":
        view["gtid_executed_has_skipped"] = gtid_in_set(skip["gtid"], position.get("executed_gtid_set"))
        view["passed"] = bool(view["gtid_executed_has_skipped"]) and not same_failure
    elif plan["engine"] == "mariadb" and skip.get("gtid") and _GTID_ITEM.match(skip["gtid"]):
        domain, _server, seq = (int(p) for p in skip["gtid"].split("-"))
        # The global gtid_slave_pos (not the Gtid_Slave_Pos column, which is
        # gtid_current_pos: Ma/sql/sql_show.cc:9122, Ma/sql/sql_repl.cc:5680-5690).
        gtid = report.get("gtid") or {}
        slave_pos = {d["domain"]: d["seq"] for d in gtid.get("slave_pos_domains") or ()}
        view["gtid_slave_pos"] = gtid.get("slave_pos")
        view["passed"] = slave_pos.get(domain) is not None and slave_pos[domain] >= seq and not same_failure
    elif (skip.get("end_log_pos") or skip.get("error_end_log_pos")) and skip.get("exec_file"):
        exec_file, exec_pos = position.get("exec_file"), position.get("exec_pos")
        end = skip.get("end_log_pos") or skip.get("error_end_log_pos")
        moved = exec_file is not None and exec_pos is not None and (exec_file != skip["exec_file"] or exec_pos >= end)
        view["passed"] = bool(moved) and not same_failure
    else:
        view["passed"] = not same_failure
    return view


def _skip_message(plan, after):
    skip = plan["skip"]
    if skip.get("gtid"):
        what = "transaction %s" % skip["gtid"]
    elif skip.get("exec_file"):
        what = "the transaction after %s:%s%s" % (skip["exec_file"], skip.get("exec_pos"), " (ending at %s)" %
                                                  skip["end_log_pos"] if skip.get("end_log_pos") else "")
    else:
        what = "one transaction"
    text = ("The skip script completed for %s on channel %r (error %s: %s)." if after.get("passed") is not True
            else "Skipped %s on channel %r (error %s: %s).") % (what, plan["channel"], plan["error"]["errno"],
                                                               plan["error"]["message"] or "?")
    if after.get("present"):
        if after["passed"] is False:
            text += (" The applier did not pass the failed transaction: it stopped again with error %s on the "
                     "same transaction." % after.get("last_sql_errno") if after.get("last_sql_errno") else
                     " myflames could not confirm that the applier passed the failed transaction.")
        if after.get("sql_running") == "Yes":
            text += " The applier is running%s." % (
                ", %s behind" % _duration_text(after["lag_s"]) if after.get("lag_s") is not None else "")
        elif after.get("last_sql_errno") and after["passed"] is not False:
            text += " The applier stopped again with error %s on the next transaction." % after["last_sql_errno"]
        if after.get("skip_counter"):
            text += (" Skip_Counter is still %s: the next START of a stopped applier will skip events. Reset it "
                     "with SET GLOBAL sql_replica_skip_counter = 0 if that is not wanted." % after["skip_counter"])
    text += (" The skipped transaction is missing on this replica now; verify the affected tables."
             if after.get("passed") is True else " Inspect replication and the counter before retrying; no progress was verified.")
    return text


def _decision_for(statements=None):
    return dbops.decide(dbops.classify(";\n".join(statements) if statements else "START REPLICA SQL_THREAD"))


def _unsent(result, audit, status, outcome, message, target, verified=None):
    result["status"], result["message"] = status, message
    if audit is not None:
        audit_status = status if status in ("refused", "declined") else "refused"
        if audit.pending:
            audit.complete(audit_status, outcome="unsent", message=message)
            result["audit_id"] = audit.id
        else:
            result["audit_id"] = audit.outcome(_tagged(result.get("statements") or []) or [SKIP_TAG + "(nothing planned)"],
                                               audit_status, outcome=outcome, message=message, target=target,
                                               verified=verified)
    result["sent"] = False
    result["stop_reason"] = outcome
    return result


_WRITE_LOCK = threading.Lock()


@contextlib.contextmanager
def _serialize_skips():
    """Serialize replication starts across channels, UI threads and CLI processes.

    Conservatively lock all servers for this OS account: endpoint aliases must
    not create different locks for the same server. External SQL clients do not
    participate; preexisting counters and lack of progress are still refused.
    """
    with _WRITE_LOCK:
        directory = os.path.expanduser("~/.myflames")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(os.path.join(directory, "replication-write.lock"), "a+b") as handle:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _policy_key(decision):
    return decision.get("connection_type"), decision.get("read_only")


def skip_one(conn, channel, expect, audit=None, decision=None, confirmed=False, statements=None,
             settle_s=SETTLE_S, sleep=None, report=None, decision_reader=None, before_send=None,
             status_guard=None, send_lock=None, status_reader=None):
    """Guard one skip. A caller's report is never authoritative for a write.

    A loop supplies a synchronized send boundary and a fresh-status guard.
    All paths complete the audit, including exceptions before any SQL is sent.
    """
    send_state = {"sent": False, "script_succeeded": False}
    try:
        with _serialize_skips():
            return _skip_one(conn, channel, expect, audit, decision, confirmed, statements, settle_s, sleep,
                             decision_reader, before_send, status_guard, send_lock, send_state, status_reader or read_status)
    except BaseException as exc:
        if audit is not None and not audit.finished:
            if send_state["sent"]:
                audit.complete("unknown", outcome="unverified" if send_state["script_succeeded"] else "error",
                               message="The script was sent; its effect is unknown: " + str(exc))
            elif audit.pending:
                audit.complete("refused", outcome="unsent", message=str(exc))
            else:
                audit.outcome(_tagged(statements or []), "refused", outcome="unsent", message=str(exc), target=expect)
        raise


def _skip_one(conn, channel, expect, audit, decision, confirmed, statements, settle_s, sleep,
              decision_reader, before_send, status_guard, send_lock, send_state, status_reader):
    if not isinstance(channel, str):
        raise ValueError("Name the channel to skip on.")
    expect = check_expect(expect)
    if expect["channel"] != channel:
        raise ValueError("The confirmed identity is for channel %r, not %r." % (expect["channel"], channel))
    if statements is not None and (not isinstance(statements, list) or
                                   any(not isinstance(s, str) for s in statements)):
        raise ValueError("statements must be the list of statements the confirmation showed.")
    result = {"schema_version": SKIP_SCHEMA_VERSION, "engine": None, "server_version": None, "channel": channel,
              "method": expect.get("method"), "statements": list(statements or []), "status": None,
              "message": None, "verified": [], "target": expect, "plan": None, "after": None}
    decision = decision if decision is not None else _decision_for(statements)
    blocked = dbops.gate(decision, confirmed)
    if blocked:
        return _unsent(result, audit, "refused" if blocked[0] == "refused" else "needs_confirmation",
                       "read_only" if blocked[0] == "refused" else "needs_confirmation", blocked[1], expect)
    report = status_reader(conn)
    result["engine"], result["server_version"] = report.get("engine"), report.get("server_version")
    if audit is not None:
        audit.server(report.get("engine"), report.get("server_version"))
    if not report.get("available"):
        return _unsent(result, audit, "refused", "unavailable", report.get("message"), expect)
    found = find_channel(report, channel)
    if status_guard is not None:
        problem = status_guard(report, found)
        if problem:
            return _unsent(result, audit, "refused", problem[0], problem[1] or STOP_REASONS[problem[0]][0], expect)
    if found is None:
        return _unsent(result, audit, "refused", "gone", "Channel %r is gone from this server: nothing was sent."
                       % channel, expect)
    try:
        plan = plan_skip(report, channel)
    except ValueError as exc:
        return _unsent(result, audit, "refused", "identity", "%s Nothing was sent." % exc, expect)
    result["plan"], result["method"] = plan, plan["method"]
    if not plan["available"]:
        return _unsent(result, audit, "refused", "unavailable", plan["message"], expect)
    result["statements"] = list(plan["statements"])
    verified, problem = verify_identity(found, plan, expect)
    if problem is None and statements is not None and list(statements) != plan["statements"]:
        problem = ("The statements myflames would send now differ from the ones you confirmed, so nothing was "
                   "sent. Read the channel again.")
    if problem:
        return _unsent(result, audit, "refused", "identity", problem, expect, verified)
    result["verified"] = verified
    script = _script(plan["statements"])
    if audit is not None:
        try:
            result["audit_id"] = audit.attempt(_tagged(plan["statements"]), target=expect, verified=verified)
        except Exception as exc:
            raise ValueError("Could not write the audit record (%s), so nothing was sent." % exc)
    # Privilege checks cover the WHOLE script, including START after COMMIT/SET.
    grants = privileges.read_privileges(conn)
    action = "skip_gtid" if plan["method"] == "empty_gtid_transaction" else "skip_counter"
    match = _MYSQL_GTID.fullmatch(plan["skip"].get("gtid") or "")
    permission = privileges.can(grants, action, tagged_gtid=bool(match and match.group(2)))
    result["privilege_check"] = permission
    if permission["allowed"] is False or not permission["available"]:
        return _unsent(result, audit, "denied", "denied", "Nothing was sent. " + permission["reason"], expect)
    # Audit storage and grants collection can block. No write uses the old snapshot.
    fresh = status_reader(conn)
    current = find_channel(fresh, channel)
    if not fresh.get("available") or current is None:
        return _unsent(result, audit, "refused", "identity", "Channel unavailable after audit preparation; nothing was sent.", expect)
    try:
        fresh_plan = plan_skip(fresh, channel)
    except ValueError as exc:
        return _unsent(result, audit, "refused", "identity", str(exc), expect)
    _verified, problem = verify_identity(current, fresh_plan, expect)
    if not fresh_plan["available"] or fresh_plan["statements"] != plan["statements"]:
        problem = fresh_plan.get("message") or "The skip configuration changed; nothing was sent."
    if status_guard is not None:
        guard_problem = status_guard(fresh, current)
        if guard_problem:
            return _unsent(result, audit, "refused", guard_problem[0], guard_problem[1] or STOP_REASONS[guard_problem[0]][0], expect)
    if problem:
        return _unsent(result, audit, "refused", "identity", problem, expect)
    if decision_reader is not None:
        current_decision = decision_reader()
        if _policy_key(current_decision) != _policy_key(decision) or dbops.gate(current_decision, confirmed):
            return _unsent(result, audit, "refused", "policy_changed", STOP_REASONS["policy_changed"][0], expect)
    # stop() uses the same lock. The transition to an in-flight send is atomic
    # with respect to Stop; a Stop after this point applies to the next send.
    with send_lock or contextlib.nullcontext():
        blocked = before_send() if before_send is not None else None
        if blocked:
            return _unsent(result, audit, "refused", blocked, STOP_REASONS[blocked][0], expect)
        result["sent"] = send_state["sent"] = True
    try:
        conn.run(script, extra_flags=list(dbops.WRITE_CLIENT_FLAGS))
    except KeyboardInterrupt:
        if audit is not None:
            audit.fail(KeyboardInterrupt())
        raise
    except ConnectorError as exc:
        if _fatal(exc):
            unknown = dbops.may_have_run(exc)
            message = ("Could not skip the transaction: %s.%s" % (
                reason(exc), " The statements may have reached the server, so the outcome is unknown; read "
                             "the channel again." if unknown else ""))
            residual = _residual_state(exc, plan, None)
            message += " " + residual["message"]
            if audit is not None:
                audit.complete("unknown" if unknown else "error", outcome="timeout" if unknown else "error",
                               error=str(exc), message=message, after={"residual_state": residual})
            raise ConnectorError(message) from exc
        result["status"], result["message"] = _server_error(exc, plan["statements"], plan["privileges"])
        result["after"] = _read_after(conn, channel, plan, 0, sleep)
        result["residual_state"] = _residual_state(exc, plan, result["after"])
        result["message"] += " " + result["residual_state"]["message"]
        result["after"]["residual_state"] = result["residual_state"]
        if audit is not None:
            audit.complete("error", outcome=result["status"], error=str(exc), message=result["message"],
                           after=result["after"])
        return result
    except Exception as exc:
        if audit is not None:
            audit.complete("unknown", outcome="error", message="The script may have run: " + str(exc))
        raise
    send_state["script_succeeded"] = True
    result["status"] = "skipped"
    try:
        result["after"] = _read_after(conn, channel, plan, settle_s, sleep)
    except BaseException as exc:
        if audit is not None:
            audit.complete("unknown", outcome="unverified", message="Script succeeded; verification failed: " + str(exc))
        raise
    after = result["after"] or {}
    if not after.get("present") or after.get("passed") is None:
        result["status"] = "unverified"
        result["message"] = "The script succeeded but its effect could not be verified: " + after.get("diagnostic", "channel missing or verification unavailable")
    elif after.get("skip_counter") or (plan["engine"] == "mysql" and after.get("global_skip_counter") != 0):
        result["status"] = "unverified"
        result["message"] = "The script succeeded, but the skip counter remains armed; stop and inspect it before any START."
    else:
        result["message"] = _skip_message(plan, after)
    if audit is not None:
        outcome = "no_progress" if after.get("passed") is False and result["status"] == "skipped" else result["status"]
        audit.complete("unknown" if outcome == "unverified" else "error" if outcome == "no_progress" else "ok",
                       outcome=outcome, message=result["message"], after=result["after"])
    return result


def _residual_state(exc, plan, after):
    """Report irreversible partial effects without attempting automatic rollback."""
    line = _LINE.search(str(exc))
    count = int(line.group(1)) - 1 if line and 1 <= int(line.group(1)) <= len(plan["statements"]) else None
    done = plan["statements"][:count] if count is not None else []
    committed = "COMMIT" in done if count is not None else None
    armed = any(s.startswith("SET GLOBAL") for s in done) if count is not None else None
    counters = [(after or {}).get("skip_counter")]
    if plan["engine"] == "mysql":
        counters.append((after or {}).get("global_skip_counter"))
    if any(value for value in counters if value is not None):
        armed = True
    elif all(value == 0 for value in counters):
        armed = False
    if (after or {}).get("gtid_executed_has_skipped"):
        committed = True
    if _fatal(exc) and not committed:
        committed = None
    messages = []
    if committed:
        messages.append("The empty GTID transaction committed; START was not confirmed. The GTID cannot be undone.")
    if armed:
        messages.append("The skip counter is armed; a later START can consume it. Inspect the channel and reset the counter to 0 manually before any unrelated START.")
    if count is None:
        messages.append("The completed statements are unknown; inspect GTID execution and the counter before retrying.")
    if not messages:
        messages.append("No committed GTID or armed counter was confirmed.")
    return {"completed_statements": done if count is not None else None, "gtid_committed": committed,
            "counter_armed": armed, "message": " ".join(messages)}


def _read_after(conn, channel, plan, settle_s, sleep):
    if settle_s:
        (sleep or time.sleep)(settle_s)
    try:
        report = read_status(conn)
        if not report.get("available"):
            return {"present": None, "passed": None, "diagnostic": report.get("message") or "Status unavailable."}
        after = _after_view(report, find_channel(report, channel), plan)
        if not after.get("present"):
            after.update(passed=None, diagnostic="The channel disappeared after the script.")
        return after
    except (ConnectorError, ValueError) as exc:
        return {"present": None, "passed": None, "diagnostic": "Could not read status: " + str(exc)}


# A completed version-check stage is counted after every receiver connection,
# including in-thread reconnects. A THREAD_ID alone is not a reconnect epoch.
_RECEIVER_SQL = (
    "SELECT t.THREAD_ID, t.INSTRUMENTED, t.PROCESSLIST_STATE, s.EVENT_NAME, s.COUNT_STAR, i.ENABLED, "
    "(SELECT ENABLED FROM performance_schema.setup_consumers WHERE NAME='global_instrumentation'), "
    "(SELECT ENABLED FROM performance_schema.setup_consumers WHERE NAME='thread_instrumentation') "
    "FROM performance_schema.threads t JOIN performance_schema.events_stages_summary_by_thread_by_event_name s "
    "ON s.THREAD_ID=t.THREAD_ID JOIN performance_schema.setup_instruments i ON i.NAME=s.EVENT_NAME "
    "WHERE t.NAME IN ('thread/sql/replica_io','thread/sql/slave_io') "
    "AND s.EVENT_NAME IN ('stage/sql/Checking source version','stage/sql/Checking master version') "
    "ORDER BY t.THREAD_ID, s.EVENT_NAME")


class ReceiverUnstable(ValueError):
    """Instrumentation cannot certify a receiver that is stopped or connecting."""


def read_receiver_identity(conn, report):
    """Conservative all-receiver fingerprint (MariaDB lacks channel thread IDs).

    Disabled/missing stage instrumentation is unknown, never an unchanged epoch.
    Changes on other channels also stop the loop. Summary resets stop it too.
    """
    if (report.get("capabilities") or {}).get("performance_schema") is False:
        raise ValueError("Performance Schema is off.")
    channels = [c for c in report.get("channels", ()) if c.get("kind") != "group_replication"]
    rows = Reader(conn, REPLICATION_TAG).rows(_RECEIVER_SQL)
    if not rows:
        raise ValueError("Receiver stage counters are unavailable (Performance Schema and stage instrumentation required).")
    identity = []
    stable = ("Waiting for source to send event", "Waiting for master to send event",
              "Queueing source event", "Queueing master event",
              "Waiting for the replica SQL thread to free enough relay log space",
              "Waiting for the slave SQL thread to free enough relay log space")
    for row in rows:
        if len(row) != 8 or any(row[i] != "YES" for i in (1, 5, 6, 7)) or _int(row[4]) is None:
            raise ValueError("Receiver stage counters, thread instrumentation or required consumers are "
                             "disabled or unreadable.")
    for row in rows:
        if not (row[2] or "").startswith(stable):
            raise ReceiverUnstable("A replication receiver is connecting or its state cannot be verified; "
                                   "nothing was sent.")
        identity.append((str(row[0]), row[3], int(row[4])))
    if not channels or any(c.get("io", {}).get("running") != "Yes" for c in channels):
        raise ReceiverUnstable("A replication receiver is absent, stopped or connecting; nothing was sent.")
    if len(set(item[0] for item in identity)) < len(channels):
        raise ValueError("Not every replication receiver has an instrumented stage counter.")
    return tuple(identity)


def _receiver_details(engine, identity=None, problem=None):
    """Public guard fields shared by the confirmation plan and loop status."""
    if engine not in ("mysql", "mariadb"):
        return {"receiver_guard": "off", "receiver_guard_reason": problem,
                "receiver_guard_instrument": None, "receiver_guard_note": "Receiver coverage has not been checked yet."}
    instrument = "stage/sql/Checking master version" if engine == "mariadb" else "stage/sql/Checking source version"
    note = ("Receiver reconnects are detected; a changed receiver epoch stops the loop." if identity else
            "Receiver reconnects are not detected: enable %s, global_instrumentation and "
            "thread_instrumentation, and allow reads of the receiver stage counters. "
            "Performance Schema must be on. All other loop guards remain active." % instrument)
    return {"receiver_guard": "on" if identity else "off", "receiver_guard_reason": problem,
            "receiver_guard_instrument": instrument, "receiver_guard_note": note}


def receiver_guard_plan(conn, report):
    """Read-only reconnect coverage for a loop confirmation; never enables it.

    This is separate from read_status so ordinary monitoring needs no extra
    stage-table reads. A loop captures its own epoch when it starts.
    """
    try:
        identity = read_receiver_identity(conn, report)
    except (ConnectorError, ValueError) as exc:
        if isinstance(exc, ConnectorError) and _fatal(exc):
            raise
        return _receiver_details(report.get("engine"), problem=str(exc))
    return _receiver_details(report.get("engine"), identity,
                             None if identity else "Receiver stage counters are unavailable.")


# ---- skip until stopped ----------------------------------------------------------

def check_codes(codes):
    """The error codes a loop may skip: at least one, none preselected."""
    if isinstance(codes, (int, bool, str)) or not isinstance(codes, (list, tuple)) or not codes:
        raise ValueError("Choose at least one error code to skip (common ones: %s). None are preselected." %
                         ", ".join("%d %s" % item for item in sorted(COMMON_ERRORS.items())))
    out = []
    for code in codes:
        if isinstance(code, bool) or not isinstance(code, int) or not 1 <= code <= 99999:
            raise ValueError("Error codes are integers from 1 to 99999; got %r." % (code,))
        if code not in out:
            out.append(code)
    if len(out) > 20:
        raise ValueError("Choose at most 20 error codes.")
    return out


def check_max_skips(max_skips):
    if isinstance(max_skips, bool) or not isinstance(max_skips, int) or not 1 <= max_skips <= LOOP_MAX_SKIPS:
        raise ValueError("Choose a maximum number of skips from 1 to %d." % LOOP_MAX_SKIPS)
    return max_skips


def check_duration(max_duration_s):
    if max_duration_s is None:
        return None
    if isinstance(max_duration_s, bool) or not isinstance(max_duration_s, (int, float)) \
            or not 1 <= max_duration_s <= LOOP_MAX_DURATION_S:
        raise ValueError("Choose a maximum duration from 1 second to %d hours." % (LOOP_MAX_DURATION_S // 3600))
    return float(max_duration_s)


def check_poll(poll_s):
    if isinstance(poll_s, bool) or not isinstance(poll_s, (int, float)) \
            or not LOOP_MIN_POLL_S <= poll_s <= LOOP_MAX_POLL_S:
        raise ValueError("Choose a check interval from %g to %g seconds." % (LOOP_MIN_POLL_S, LOOP_MAX_POLL_S))
    return float(poll_s)


#: Why a loop stopped, and whether replication still needs attention.
STOP_REASONS = {
    "user_stop": ("Stopped on request.", False),
    "max_skips": ("Reached the maximum number of skips.", False),
    "duration": ("Reached the maximum duration.", False),
    "unselected_error": ("The applier stopped on an error you did not choose to skip.", True),
    "no_progress": ("The same transaction failed again after it was skipped.", True),
    "applier_stopped": ("The applier stopped without an error (someone ran STOP REPLICA?).", True),
    "channel_gone": ("The channel is gone from the server.", True),
    "config_changed": ("The channel's source, gtid_mode, AGAT or skip method changed.", True),
    "server_restarted": ("The estimated server start time changed.", True),
    "receiver_reconnected": ("A receiver restarted or reconnected to its source.", True),
    "receiver_unverified": ("Receiver reconnection monitoring is unavailable; the loop cannot safely continue.", True),
    "unverified": ("The script succeeded but the skip could not be verified.", True),
    "connection_lost": ("myflames lost the connection or could not read the status.", True),
    "refused": ("A skip was refused (policy, identity or no safe method).", True),
    "denied": ("The server refused a skip for lack of privileges.", True),
    "failed": ("A skip failed on the server.", True),
    "error": ("The loop stopped on an unexpected error.", True),
    "policy_changed": ("The connection's type or Read-only setting changed. Start the loop again under the new "
                       "setting.", True),
}


class SkipLoop(object):
    """An opt-in, bounded "skip until stopped" job for one channel.

    Each iteration reads :func:`read_status` and acts only when the applier
    stopped with one of the selected ``Last_SQL_Errno`` values; then
    :func:`skip_one` re-verifies and skips that one transaction (the loop's
    own confirmation covers each skip; *decision* is still checked, so a
    read-only connection never skips). Every skip is one audit record when
    *audit_factory* (a callable returning a fresh
    :class:`myflames.dbops.AuditTrail`) is given. Between checks it waits
    *poll_s*; after a skip the wait doubles per consecutive skip, up to 60 s.
    It stops at once on the conditions in :data:`STOP_REASONS`; :meth:`stop`
    takes effect before the next skip and wakes a waiting loop.

    :meth:`run` runs the loop in the calling thread (the CLI);
    :meth:`start` runs it in a daemon thread (the workspace). *clock* and
    *sleep* are injectable for tests; *on_event* receives each event dict
    (``skip``, ``stopped``, and ``receiver_guard`` when starting without
    reconnect coverage). *conn* is closed at the end when *close_conn*.
    *receiver_required* preserves coverage advertised by a confirmation:
    an unavailable first receiver read then stops instead of falling back.
    """

    def __init__(self, conn, channel, codes, max_skips, max_duration_s=None, poll_s=LOOP_DEFAULT_POLL_S,
                 decision=None, audit_factory=None, identity=None, clock=None, sleep=None, on_event=None,
                 settle_s=SETTLE_S, close_conn=False, decision_reader=None, receiver_reader=None,
                 receiver_required=False):
        if not isinstance(channel, str) or len(channel) > 256:
            raise ValueError("Choose the channel to watch by its name.")
        self.conn = conn
        self.channel = channel
        self.codes = check_codes(codes)
        self.max_skips = check_max_skips(max_skips)
        self.max_duration_s = check_duration(max_duration_s)
        self.poll_s = check_poll(poll_s)
        self.decision = decision if decision is not None else _decision_for()
        self.audit_factory = audit_factory
        self.decision_reader = decision_reader
        self.receiver_reader = receiver_reader or read_receiver_identity
        self.identity = identity
        self.settle_s = settle_s
        self.close_conn = close_conn
        self.on_event = on_event
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._thread = None
        self.job_id = uuid.uuid4().hex
        self._state = "starting"
        self._reason = None
        self._stop_requested = None
        self._error_text = None
        self._engine = None
        self._server_version = None
        self._started_at = None
        self._started_mono = None
        self._stopped_at = None
        self._skips = []
        self._last_error = None
        self._baseline = None
        self._last_uptime = None
        self._start_estimate = None
        self._sample_window = None
        self._receiver_identity = None
        # Once enabled, the guard must never disappear during this run. A UI
        # confirmation that advertised it can require it at the first read too.
        self._receiver_required = receiver_required
        self._receiver_details = None
        self._last_skipped = None
        self._consecutive = 0
        self._checks = 0
        self._iteration_audit = None

    # -- control ------------------------------------------------------------------

    def start(self):
        """Read initial receiver coverage, then run in a daemon thread.

        Use the loop's connection before handing it to the worker; its
        existing connect/query timeouts bound these reads.
        """
        try:
            report = self._read_status(self.conn)
            self._engine, self._server_version = report.get("engine"), report.get("server_version")
            problem = self._receiver_guard(report)
        except (ConnectorError, ValueError) as exc:
            problem = ("connection_lost", str(exc))
        except Exception as exc:
            problem = ("error", str(exc))
        if problem:
            if self._receiver_details is None:
                self._receiver_details = _receiver_details(self._engine, problem=problem[1])
            self._finish(*problem)
            if self.close_conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
            return self
        self._thread = threading.Thread(target=self.run, name="myflames-replication-skip-loop", daemon=True)
        self._thread.start()
        return self

    def stop(self, reason=None):
        """Ask the loop to stop; it takes effect before the next skip.
        *reason* is the :data:`STOP_REASONS` key it finishes with (default
        ``user_stop``); the first request's reason wins."""
        with self._lock:
            if self._stop_requested is None:
                self._stop_requested = reason or "user_stop"
            if self._state in ("starting", "running"):
                self._state = "stopping"
        self._stop_event.set()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def active(self):
        return self.state in ("starting", "running", "stopping")

    def _wait(self, seconds):
        if self._sleep is not None:
            self._sleep(seconds)
        else:
            self._stop_event.wait(seconds)

    def _elapsed(self):
        return self._clock() - self._started_mono if self._started_mono is not None else 0.0

    def _emit(self, event):
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass

    def _finish(self, reason_key, text=None):
        if STOP_REASONS.get(reason_key, ("", True))[1] and self.audit_factory is not None and self._iteration_audit is None:
            # A refusal before a plan exists is still an attempted bulk action.
            # Do not add a second record for an iteration already owned by skip_one.
            try:
                audit = self.audit_factory()
                self._iteration_audit = audit
                audit.outcome([], "refused", outcome="unsent", message=text or STOP_REASONS.get(reason_key, (reason_key,))[0],
                              target={"channel": self.channel, "codes": list(self.codes), "stop_reason": reason_key})
            except Exception:
                pass  # Audit failure never authorizes a write or hides the refusal.
        with self._lock:
            self._state = "stopped"
            self._reason = reason_key
            if text:
                self._error_text = text
            self._stopped_at = now()
        self._emit({"event": "stopped", "reason": reason_key, "message": self._stop_message(),
                    "skips": len(self._skips)})

    # -- the loop -----------------------------------------------------------------

    def run(self):
        """Run until a stop condition; returns the final :meth:`status`."""
        self._started_at = now()
        self._started_mono = self._clock()
        with self._lock:
            if self._state == "starting":
                self._state = "running"
        try:
            self._loop()
        except KeyboardInterrupt:
            self._finish("user_stop")
        except ConnectorError as exc:
            self._finish("connection_lost", str(exc))
        except Exception as exc:  # a thread must never die silently
            self._finish("error", str(exc))
        finally:
            if self.close_conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
        return self.status()

    def _bounds_reached(self):
        if self._stop_event.is_set():
            with self._lock:
                requested = self._stop_requested or "user_stop"
            self._finish(requested)
            return True
        if self.decision_reader is not None:
            try:
                current = self.decision_reader()
                changed = _policy_key(current) != _policy_key(self.decision) or dbops.gate(current, True)
            except Exception:
                # An unavailable/deleted profile cannot authorize another skip.
                changed = True
            if changed:
                self._finish("policy_changed")
                return True
        if self.max_duration_s is not None and self._elapsed() >= self.max_duration_s:
            self._finish("duration")
            return True
        if len(self._skips) >= self.max_skips:
            self._finish("max_skips")
            return True
        return False

    def _changed(self, report, channel):
        """A stop reason when the server or the channel changed under the loop."""
        uptime = report.get("uptime_s")
        if self._last_uptime is not None and uptime is not None and uptime < self._last_uptime:
            return "server_restarted", None
        self._last_uptime = uptime
        if uptime is not None:
            start, end = self._sample_window or (self._clock(), self._clock())
            # Uptime is sampled somewhere during the read and rounded to seconds.
            # Intersect possible boot-time intervals instead of treating read
            # latency as a restart (or ignoring a restart with increasing uptime).
            lower, upper = start - uptime - 1.0, end - uptime
            if self._start_estimate is not None:
                lower = max(lower, self._start_estimate[0])
                upper = min(upper, self._start_estimate[1])
                if lower > upper:
                    return "server_restarted", None
            self._start_estimate = (lower, upper)
        else:
            return "connection_lost", "Cannot read Uptime to detect a server restart."
        if channel is None:
            return "channel_gone", None
        if channel.get("kind") == "group_replication":
            return "config_changed", GR_NOTE
        baseline = {"source": dict(channel.get("source") or {}), "gtid_mode": (report.get("gtid") or {}).get("mode"),
                    "agat": channel.get("agat"), "engine": report.get("engine"),
                    "settings": dict(report.get("settings") or {}),
                    "using_gtid": (channel.get("position") or {}).get("using_gtid"),
                    "parallel_mode": (channel.get("position") or {}).get("parallel_mode")}
        if self._baseline is None:
            self._baseline = baseline
        elif baseline != self._baseline:
            changed = [k for k in baseline if baseline[k] != self._baseline[k]]
            return "config_changed", "Changed: %s." % ", ".join(changed)
        return None

    def _read_status(self, conn):
        start = self._clock()
        report = read_status(conn)
        self._sample_window = (start, self._clock())
        return report

    def _fresh_guard(self, report, channel):
        changed = self._changed(report, channel)
        if changed:
            return changed
        failed = effective_error(channel)
        if failed is None:
            return "refused", "Error 3030: cannot tell which transaction to skip; no underlying worker failure is visible."
        error = failed.get("errno")
        if error not in self.codes:
            return "unselected_error", "The freshly read error %s is not selected." % error
        return self._receiver_guard(report)

    def _receiver_guard(self, report):
        if self._receiver_details is not None and not self._receiver_required:
            # Starting without telemetry is an explicit, fixed mode. Do not
            # silently enable monitoring partway through an unobserved epoch.
            return None
        identity, problem, unstable = None, None, False
        try:
            identity = self.receiver_reader(self.conn, report)
        except ReceiverUnstable as exc:
            problem, unstable = str(exc), True
        except (ConnectorError, ValueError) as exc:
            if isinstance(exc, ConnectorError) and _fatal(exc):
                return "connection_lost", str(exc)
            problem = str(exc)
        details = _receiver_details(report.get("engine"), identity,
                                    problem or ("Receiver stage counters are unavailable." if not identity else None))
        with self._lock:
            self._receiver_details = details
        if not identity:
            if self._receiver_required or unstable:
                return "receiver_unverified", details["receiver_guard_reason"]
            self._emit(dict(details, event="receiver_guard", message=details["receiver_guard_note"]))
            return None
        if self._receiver_identity is None:
            self._receiver_identity = identity
            self._receiver_required = True
        elif identity != self._receiver_identity:
            return "receiver_reconnected", None
        return None

    def _before_send(self):
        # Called while holding _lock, also used by stop(). No IO after this
        # boundary: the script is now in flight, even if Stop arrives next.
        if self._stop_requested is not None or self._stop_event.is_set():
            return self._stop_requested or "user_stop"
        if self.max_duration_s is not None and self._elapsed() >= self.max_duration_s:
            return "duration"
        if len(self._skips) >= self.max_skips:
            return "max_skips"
        return None

    def _failure_key(self, channel):
        position = channel.get("position") or {}
        return (channel["sql"]["error"]["errno"], channel.get("failing_gtid"), position.get("exec_file"),
                position.get("exec_pos"))

    def _loop(self):
        method = None
        while True:
            self._iteration_audit = None
            if self._bounds_reached():
                return
            try:
                report = self._read_status(self.conn)
            except (ConnectorError, ValueError) as exc:
                self._finish("connection_lost", "Could not read replication status: %s" % reason(exc))
                return
            self._checks += 1
            self._engine, self._server_version = report.get("engine"), report.get("server_version")
            if not report.get("available"):
                self._finish("refused", report.get("message"))
                return
            channel = find_channel(report, self.channel)
            changed = self._changed(report, channel)
            if changed:
                self._finish(*changed)
                return
            receiver_problem = self._receiver_guard(report)
            if receiver_problem:
                self._finish(*receiver_problem)
                return
            sql = channel["sql"]
            with self._lock:
                self._last_error = dict(sql["error"])
            if sql["running"] == "Yes":
                self._consecutive = 0
                self._wait(self.poll_s)
                continue
            errno = sql["error"]["errno"]
            if not errno:
                self._finish("applier_stopped")
                return
            failed = effective_error(channel)
            if failed is None:
                self._finish("refused", "Error 3030: a worker stopped behind a failure no worker shows.")
                return
            errno, message = failed.get("errno"), failed.get("message")
            if errno not in self.codes:
                self._finish("unselected_error", "Error %d: %s" % (errno, message or "?"))
                return
            key = self._failure_key(channel)
            if key == self._last_skipped:
                self._finish("no_progress", "Error %d again on the same transaction." % errno)
                return
            plan = plan_skip(report, self.channel)
            if not plan["available"]:
                self._finish("refused", plan["message"])
                return
            if method is None:
                method = plan["method"]
            elif plan["method"] != method:
                self._finish("config_changed", "The skip method changed from %s to %s." % (method, plan["method"]))
                return
            if self._bounds_reached():
                return
            audit = self.audit_factory() if self.audit_factory is not None else None
            self._iteration_audit = audit
            result = skip_one(self.conn, self.channel, plan["identity"], audit=audit, decision=self.decision,
                              confirmed=True, statements=plan["statements"], settle_s=self.settle_s,
                              sleep=self._sleep, decision_reader=self.decision_reader,
                              before_send=self._before_send, send_lock=self._lock, status_guard=self._fresh_guard,
                              status_reader=self._read_status)
            if result["status"] != "skipped":
                key_reason = result.get("stop_reason")
                if key_reason not in STOP_REASONS:
                    key_reason = result["status"] if result["status"] in ("denied", "failed", "unverified") else "refused"
                self._finish(key_reason, result.get("message"))
                return
            entry = {"at": now(), "gtid": plan["skip"].get("gtid"), "exec_file": plan["skip"].get("exec_file"),
                     "exec_pos": plan["skip"].get("exec_pos"), "end_log_pos": plan["skip"].get("end_log_pos"),
                     "errno": errno, "message": message, "table": plan["skip"].get("table"),
                     "passed": (result.get("after") or {}).get("passed"), "audit_id": result.get("audit_id")}
            with self._lock:
                self._skips.append(entry)
            self._last_skipped = key
            self._consecutive += 1
            self._emit(dict(entry, event="skip", count=len(self._skips)))
            if entry["passed"] is not True:
                self._finish("no_progress" if entry["passed"] is False else "unverified")
                return
            if self._bounds_reached():
                return
            self._wait(min(self.poll_s * (2 ** min(self._consecutive - 1, 6)), LOOP_MAX_POLL_S))

    # -- status -------------------------------------------------------------------

    def _stop_message(self):
        text, _attention = STOP_REASONS.get(self._reason, ("Stopped.", True))
        detail = (" %s" % _sentence(self._error_text)) if self._error_text else ""
        return "%s%s Skipped %d on channel %r." % (text, detail, len(self._skips), self.channel)

    def status(self):
        """The ``replication-skip-loop-1.0`` snapshot."""
        with self._lock:
            state, reason_key = self._state, self._reason
            skips = [dict(s) for s in self._skips]
            last_error = dict(self._last_error) if self._last_error else None
            receiver = dict(self._receiver_details or {})
            if not receiver:
                # A snapshot of an unstarted/stopped job has no active guard.
                # Do not misrepresent pending initialization as disabled coverage.
                receiver = _receiver_details(self._engine, problem=self._error_text or "The loop has not started.")
            if self._engine not in ("mysql", "mariadb"):
                receiver["receiver_guard_note"] = receiver["receiver_guard_reason"]
        elapsed = self._elapsed() if self._started_mono is not None else None
        remaining = None
        if elapsed is not None and self.max_duration_s is not None:
            remaining = max(0.0, self.max_duration_s - elapsed)
        if state == "stopped":
            message = self._stop_message()
        else:
            since = (self._started_at or "")[11:16]
            message = "Skipping %s on channel %r: %d skipped%s." % (
                ", ".join(str(c) for c in self.codes), self.channel, len(skips), " since %s UTC" % since if since else "")
        result = {
            "schema_version": LOOP_SCHEMA_VERSION,
            "job_id": self.job_id,
            "state": state,
            "stop_reason": reason_key,
            "needs_attention": STOP_REASONS.get(reason_key, ("", True))[1] if state == "stopped" else None,
            "channel": self.channel,
            "codes": list(self.codes),
            "max_skips": self.max_skips,
            "max_duration_s": self.max_duration_s,
            "poll_s": self.poll_s,
            "engine": self._engine,
            "server_version": self._server_version,
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
            "elapsed_s": round(elapsed, 3) if elapsed is not None else None,
            "remaining_s": round(remaining, 3) if remaining is not None else None,
            "checks": self._checks,
            "skips_done": len(skips),
            "skips": skips,
            "last_error": last_error,
            "error": self._error_text,
            "message": message,
            "drift_warning": DRIFT_WARNING,
        }
        result.update(receiver)
        return result


def _same_endpoint(a, b):
    """Two connection identities that reach the same server through the same
    saved connection (:func:`myflames.dbops.identity_endpoint`): only the type
    or the read-only flag may differ."""
    return dbops.identity_endpoint(a) == dbops.identity_endpoint(b)


class SkipLoopRegistry(object):
    """The in-memory loops of one workspace, keyed by connection identity
    (:func:`myflames.dbops.connection_identity`), so a status read or a Stop
    never reaches a loop on another server. Loops are matched by the
    identity's endpoint, so changing the connection's type or read-only flag
    while a loop runs keeps its status and Stop (and still allows only one
    loop per channel). A myflames restart starts with an empty registry;
    nothing is ever restarted."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = []

    def start(self, identity, loop):
        """Register *loop* for *identity* and start it in a thread. Only one
        active loop per channel and connection."""
        if not isinstance(identity, str) or not identity:
            raise ValueError("A skip loop needs the connection identity.")
        with self._lock:
            for key, job in self._jobs:
                if _same_endpoint(key, identity) and job.channel == loop.channel and job.active:
                    raise ValueError("A skip loop is already running on channel %r of this connection; stop it "
                                     "first." % (loop.channel,))
            self._jobs.append((identity, loop))
            finished = [entry for entry in self._jobs if not entry[1].active]
            for entry in finished[:max(0, len(finished) - LOOP_HISTORY)]:
                self._jobs.remove(entry)
        loop.identity = identity
        loop.start()
        return loop.status()

    def _find(self, identity, job_id):
        with self._lock:
            for key, job in self._jobs:
                if job.job_id == job_id:
                    if not _same_endpoint(key, identity):
                        raise ValueError("This skip loop belongs to another connection.")
                    return job
        raise ValueError("No skip loop with this id (loops never survive a myflames restart).")

    def status(self, identity, job_id=None):
        """One loop's status, or every loop of this connection."""
        if job_id is not None:
            return self._find(identity, job_id).status()
        with self._lock:
            jobs = [job for key, job in self._jobs if _same_endpoint(key, identity)]
        return {"schema_version": LOOP_SCHEMA_VERSION, "loops": [job.status() for job in jobs]}

    def stop(self, identity, job_id):
        job = self._find(identity, job_id)
        job.stop()
        return job.status()

    def stop_profile(self, profile_id):
        """Stop all endpoints of an edited/deleted profile; keep their history."""
        if not isinstance(profile_id, str) or not profile_id:
            return
        with self._lock:
            jobs = []
            for identity, job in self._jobs:
                try:
                    key = json.loads(identity)
                except ValueError:
                    continue
                if isinstance(key, list) and key and key[0] == profile_id:
                    jobs.append(job)
        for job in jobs:
            job.stop("policy_changed")

    def stop_all(self, wait=False):
        with self._lock:
            jobs = [job for _key, job in self._jobs]
        for job in jobs:
            job.stop()
        if wait:
            for job in jobs:
                job.join()


# ---- text ------------------------------------------------------------------------

def _yes(value):
    return value or "?"


def format_text(report):
    """Human text for a replication-1.0 report."""
    if not report.get("available"):
        return report.get("message") + "\n"
    if not report.get("replica"):
        return (report.get("message") or "This server is not a replica.") + "\n"
    out = []
    gtid = report.get("gtid") or {}
    if report.get("engine") == "mariadb":
        out.append("MariaDB %s  gtid_slave_pos %s" % (report.get("server_version") or "?",
                                                      gtid.get("slave_pos") or "(empty)"))
    else:
        out.append("MySQL %s  gtid_mode %s" % (report.get("server_version") or "?", gtid.get("mode") or "?"))
    for channel in report["channels"]:
        label = "channel %r" % channel["name"] if report.get("engine") == "mysql" else \
            "connection %r" % channel["name"]
        source = channel.get("source") or {}
        out.append("")
        out.append("%s  [%s]  source %s:%s" % (label, channel["status"], _yes(source.get("host")),
                                               _yes(source.get("port"))))
        out.append("  receiver %s, applier %s, lag %s" % (
            _yes(channel["io"]["running"]), _yes(channel["sql"]["running"]),
            "NULL" if channel["lag_s"] is None else _duration_text(channel["lag_s"])))
        for why in channel["status_why"]:
            out.append("  - " + why)
        for note in channel["lag_notes"]:
            out.append("  note: " + note)
        plan = channel.get("skip_plan")
        if plan:
            if plan["available"]:
                out.append("  To skip this one transaction (%s): myflames replication skip --channel %s" % (
                    plan["method"], _shell_word(channel["name"])))
            else:
                out.append("  No safe automatic skip: " + plan["message"])
    return terminal_text("\n".join(out)) + "\n"


def _shell_word(text):
    return text if re.match(r"^[\w.@:-]+$", text or "") else "'%s'" % (text or "").replace("'", "'\\''")


def describe_plan(plan, label="channel"):
    """The confirmation text for one skip: target, error, statements, drift."""
    lines = ["Skip one failed transaction on %s %r (%s %s)." % (label, plan["channel"], plan["engine"],
                                                               plan.get("server_version") or "?"),
             "  Error %s: %s" % (plan["error"]["errno"], plan["error"]["message"] or "?")]
    skip = plan["skip"]
    if skip.get("gtid"):
        lines.append("  Transaction: %s" % skip["gtid"])
    if skip.get("exec_file"):
        lines.append("  Applied up to: %s:%s%s" % (skip["exec_file"], skip.get("exec_pos"),
                                                    ", failed transaction ends at %s" % skip["end_log_pos"]
                                                    if skip.get("end_log_pos") else ""))
    if skip.get("table"):
        lines.append("  Table: %s" % skip["table"])
    lines.append("  Statements (one client session):")
    lines.extend("    %s;" % s for s in plan["statements"])
    lines.append("  Privileges: %s" % plan["privileges"])
    for note in plan["notes"]:
        lines.append("  Note: %s" % note)
    lines.append("  Warning: %s" % DRIFT_WARNING)
    return terminal_text("\n".join(lines))


def format_skip(result):
    """Human text for a replication-skip-1.0 result."""
    out = [result.get("message") or result.get("status") or "?"]
    if result.get("status") == "skipped":
        out.append("Next: " + " ".join(AFTER_SKIP_STEPS))
    return terminal_text("\n".join(out)) + "\n"


def format_loop_event(event):
    if event.get("event") == "skip":
        what = event.get("gtid") or ("%s:%s" % (event.get("exec_file"), event.get("exec_pos")))
        return terminal_text("%s  skip %d: error %s on %s%s" % (
            event.get("at", "")[11:19], event.get("count", 0), event.get("errno"), what,
            " (table %s)" % event["table"] if event.get("table") else "")) + "\n"
    return terminal_text(event.get("message") or "") + "\n"
