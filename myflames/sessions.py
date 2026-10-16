"""Sessions: every connection on a MySQL or MariaDB server, post-processed.

:func:`sessions_report` returns the ``sessions-1.0`` envelope shared by
``myflames sessions`` and the workspace's Server > Sessions view: the process
list joined with InnoDB's open transactions, each session flagged
(``long_running``, ``idle_in_trx``, ``lock_wait``, ``system_thread``,
``own_connection``) and graded ok / watch / problem, plus a server-wide
summary (connections against max_connections, counts by command, state,
user, client host and database, the oldest open transaction and the InnoDB
history list length).

Every statement is a SELECT or SHOW, tagged ``/*myflames:sessions*/``: nothing
is written and nothing is killed (KILL lives in locks.py, behind a
confirmation).

Statuses are rules of thumb, not verdicts (see ``RULES``). A statement
running for 10 s is worth a look on an OLTP server and 60 s is usually a
problem there, but a report or a batch job can run longer by design.

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"; tags where noted):

* The process list source follows discover.py: ``performance_schema.
  processlist`` on MySQL 8.0.22 and later with Performance Schema on, else
  ``information_schema.PROCESSLIST``. Without PROCESS both list only the
  sessions of the caller's own user, and none for an anonymous user
  (M/storage/perfschema/table_processlist.cc:98-121, 241-249; M/sql/
  sql_show.cc:3693-3709; Ma/sql/sql_show.cc:3453-3466). The
  Performance Schema copy of a statement (INFO) is cut at 1024 bytes
  (COL_INFO_SIZE, M/storage/perfschema/pfs_column_types.h:69-71,
  pfs.cc:3415-3417); information_schema keeps up to 65535.
* Background threads without a process list id are not listed
  (table_processlist.cc:177-178); system threads show as "system user", the
  event scheduler as "event_scheduler" with command Daemon (190-201).
* MariaDB ``information_schema.PROCESSLIST`` adds TIME_MS, STAGE, MAX_STAGE
  and PROGRESS (the progress of ALTER TABLE, LOAD DATA and similar
  statements that report it, filled only while ``progress.max_counter`` is
  set), MEMORY_USED and EXAMINED_ROWS (Ma/sql/sql_show.cc:10565-10590,
  3532-3567; the same columns at mariadb-10.11.16, which lacks SENT_ROWS).
* ``information_schema.INNODB_TRX`` needs PROCESS on both engines
  (M/storage/innobase/handler/i_s.cc:797, Ma/storage/innobase/handler/
  i_s.cc:177). It lists transactions that have started inside InnoDB, so a
  session that ran BEGIN and nothing else is not in it yet. trx_state is
  RUNNING, LOCK WAIT, ROLLING BACK or COMMITTING; LOCK WAIT is any InnoDB lock
  wait, a row lock or an InnoDB table lock such as AUTO-INC (M/storage/
  innobase/lock/lock0lock.cc:3438-3476). trx_started is stored in the OS time
  zone (M/storage/innobase/handler/i_s.cc:325-348, Ma i_s.cc:211-235), so the
  ages are computed with this session's time_zone set to SYSTEM.
* The history list length is ``trx_rseg_history_len`` in
  ``information_schema.INNODB_METRICS`` on MySQL (enabled by default, needs
  PROCESS) and the ``Innodb_history_list_length`` status variable on MariaDB:
  committed transactions whose undo logs are not purged yet. Purge stops at
  the oldest open read view (M/storage/innobase/read/read0read.cc:685-715).
  READ COMMITTED and READ UNCOMMITTED close the view after each statement
  (M/storage/innobase/handler/ha_innodb.cc:19161-19168, 19754-19762), so an
  idle transaction holds purge back only under REPEATABLE READ, after its
  first consistent read; a running statement holds its own view.
* The last statement of an idle session comes from
  ``performance_schema.events_statements_current``: MySQL enables that
  consumer by default (M/sql/sys_vars.cc:650-655), MariaDB ships it off even
  with Performance Schema on (Ma/sql/mysqld.cc:861-865).
* Metadata lock waits are process list states (locks.is_mdl_wait_state,
  M/sql/mdl.cc:117-126, Ma/sql/mdl.cc:103); "Waiting for table level lock"
  is a table lock (MyISAM, MEMORY, LOCK TABLES) taken through thr_lock.
* Connections: ``Threads_connected`` includes this tool's own session;
  ``Max_used_connections`` is the peak since startup or the last status
  reset (see health.py for both engines' refusal counters).
"""
import re

from .connector import ConnectorError
from .discover import _processlist_source
from .health import CONNECTIONS_USED_PCT, _grade
from .perf_common import _count_text, _duration_text, _number, _round
from .locks import is_mdl_wait_state
from .perf_common import Reader, Server, base_report, denied, reason, show_variables

SCHEMA_VERSION = "sessions-1.0"
#: Sessions statements carry the myflames prefix so ``top`` excludes them.
SESSIONS_TAG = "/*myflames:sessions*/ "
#: Statements tagged by any myflames tool (capture, discovery, server views).
MYFLAMES_TAG = "/*myflames:"
#: Long-running statement and open-transaction ages, in seconds (watch, problem).
LONG_RUNNING_S = (10, 60)
TRX_AGE_S = (10, 60)
DEFAULT_LIMIT, MAX_LIMIT = 1000, 10000
#: The most process list and INNODB_TRX rows one report reads.
READ_CAP = 10000
#: Statement text kept per session (characters), and the one-line preview.
INFO_CAP = 16384
SHORT_WIDTH = 160
#: Breakdowns keep this many entries; the rest is summed as "other".
TOP_N = 10
#: P_S processlist INFO is cut at this many bytes.
PS_INFO_BYTES = 1024
MAX_SECONDS = 31536000

STATEMENT_COMMANDS = ("Query", "Execute", "Fetch", "Prepare")
REPLICATION_COMMANDS = ("Binlog Dump", "Binlog Dump GTID", "Register Slave", "Register Replica",
                        "Slave_IO", "Slave_SQL", "Slave_worker")
SYSTEM_USERS = ("system user", "event_scheduler")
TABLE_LOCK_STATE = "Waiting for table level lock"
_REPLICATION_STATE = re.compile(r"replica|source|relay|slave|master|coordinator|worker|applier", re.I)
_HOST_PORT = re.compile(r"^(.+):(\d+)$")

RULES = {
    "long_running": ("a statement running for %d s or more is watched, %d s or more is a problem (rules of thumb "
                     "for OLTP; reports and batch jobs can run longer by design)" % LONG_RUNNING_S),
    "idle_in_trx": ("a session idle (Sleep) inside an open InnoDB transaction keeps its row locks; under REPEATABLE "
                    "READ it also keeps the read view from its first consistent read, which holds back purge; "
                    "watched when the transaction is %d s old, a problem at %d s (rules of thumb)" % TRX_AGE_S),
    "lock_wait": ("a session waiting for an InnoDB lock (INNODB_TRX trx_state LOCK WAIT: a row lock or an InnoDB "
                  "table lock such as AUTO-INC), a metadata lock, or a table-level lock is watched; the "
                  "long-running rule raises it to a problem"),
    "oldest_transaction": ("the oldest open transaction is watched at %d s and a problem at %d s; under REPEATABLE "
                           "READ (or while a statement runs) its read view also holds back purge, so the history "
                           "list length grows behind it" % TRX_AGE_S),
    "connections": "watch at %g%% of max_connections, problem at %g%% (Threads_connected)" % CONNECTIONS_USED_PCT,
}
SEVERITY = {"ok": 0, "watch": 1, "problem": 2}
_LINKS = {
    "locks": {"view": "locks", "label": "Open Locks"},
    "innodb": {"view": "innodb", "label": "Open InnoDB"},
}


def _check_args(active_only, user, host, db, min_seconds, limit):
    if not isinstance(active_only, bool):
        raise ValueError("active_only must be true or false.")
    for name, value in (("user", user), ("host", host), ("db", db)):
        if value is not None and (not isinstance(value, str) or len(value) > 255 or "\x00" in value):
            raise ValueError("Choose a %s filter of at most 255 characters." % name)
    if isinstance(min_seconds, bool) or not isinstance(min_seconds, (int, float)) \
            or min_seconds != min_seconds or not 0 <= min_seconds <= MAX_SECONDS:
        raise ValueError("Minimum seconds must be a number from 0 to %d." % MAX_SECONDS)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("Choose a limit from 1 to %d." % MAX_LIMIT)
    return {"active_only": active_only, "user": user or None, "host": host or None, "db": db or None,
            "min_seconds": min_seconds}


def split_host(host):
    """(client host without the port, port) from a process list HOST value.

    TCP clients show as ``host:port`` (IPv6 too, as ``::1:51234``); socket and
    named-pipe clients as ``localhost`` with no port."""
    if not host:
        return None, None
    match = _HOST_PORT.match(host)
    if match and match.group(1) not in ("", ":"):
        return match.group(1), int(match.group(2))
    return host, None


def thread_kind(user, command, state):
    """'client', 'daemon' or 'replication' (see the module docstring)."""
    command = command or ""
    if command in REPLICATION_COMMANDS:
        return "replication"
    if user == "system user" and (_REPLICATION_STATE.search(state or "") or command == "Connect"):
        return "replication"
    if command == "Daemon" or user in SYSTEM_USERS:
        return "daemon"
    return "client"


def _short(text, width=SHORT_WIDTH):
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _grade_seconds(seconds, limits):
    if seconds is None:
        return "ok"
    return "problem" if seconds >= limits[1] else "watch" if seconds >= limits[0] else "ok"


def _worst(statuses):
    worst = "ok"
    for status in statuses:
        if SEVERITY.get(status, 0) > SEVERITY[worst]:
            worst = status
    return worst


# ---- reading ------------------------------------------------------------------

def _processlist_sql(engine, source):
    if engine == "mariadb":
        cols = ("ID, USER, HOST, DB, COMMAND, TIME_MS / 1000, STATE, LEFT(INFO, %d), LENGTH(INFO), "
                "STAGE, MAX_STAGE, PROGRESS, MEMORY_USED, EXAMINED_ROWS" % INFO_CAP)
    else:
        cols = "ID, USER, HOST, DB, COMMAND, TIME, STATE, LEFT(INFO, %d), LENGTH(INFO)" % INFO_CAP
    time_col = "TIME_MS" if engine == "mariadb" else "TIME"
    return ("SELECT %s FROM %s WHERE ID <> CONNECTION_ID() ORDER BY %s DESC LIMIT %d"
            % (cols, source, time_col, READ_CAP + 1))


#: trx_started is in the OS time zone; NOW() follows the session's.
SYSTEM_TIME_ZONE = "SET SESSION time_zone = 'SYSTEM'; "
_TRX_SQL = (SYSTEM_TIME_ZONE + SESSIONS_TAG + "SELECT trx_mysql_thread_id, trx_id, trx_state, trx_started, "
            "TIMESTAMPDIFF(SECOND, trx_started, NOW()), trx_wait_started, "
            "TIMESTAMPDIFF(SECOND, trx_wait_started, NOW()), trx_rows_locked, trx_rows_modified, "
            "trx_lock_structs, trx_tables_locked, trx_isolation_level, trx_is_read_only, trx_operation_state "
            "FROM information_schema.INNODB_TRX WHERE trx_mysql_thread_id <> CONNECTION_ID() "
            "ORDER BY trx_started, trx_id LIMIT %d" % (READ_CAP + 1))


def _needs_process(exc):
    text = str(exc)
    return "PROCESS" in text or "ERROR 1227" in text


def _read_transactions(reader, notes):
    """{connection id: trx}, every trx (oldest first), and whether PROCESS was granted."""
    try:
        rows = reader.rows(_TRX_SQL)
    except ConnectorError as exc:
        if "ERROR 2002" in str(exc) or "ERROR 2003" in str(exc) or "ERROR 2013" in str(exc) \
                or "timed out" in str(exc):
            raise
        if _needs_process(exc) or denied(exc):
            return {}, [], False
        notes.append("Could not read information_schema.INNODB_TRX: %s" % reason(exc))
        return {}, [], None
    if len(rows) > READ_CAP:
        rows = rows[:READ_CAP]
        notes.append("More than %s open transactions; only the %s oldest were read." % (
            "{:,}".format(READ_CAP), "{:,}".format(READ_CAP)))
    by_conn, every = {}, []
    for row in rows:
        row = list(row) + [None] * (14 - len(row))
        cid = _int(row[0])
        trx = {
            "id": row[1], "state": row[2], "started": row[3], "age_s": _int(row[4]),
            "wait_started": row[5], "wait_s": _int(row[6]) if row[5] else None,
            "rows_locked": _int(row[7]), "rows_modified": _int(row[8]), "lock_structs": _int(row[9]),
            "tables_locked": _int(row[10]), "isolation_level": row[11],
            "read_only": (row[12] or "") in ("1", "ON"), "operation_state": row[13],
        }
        every.append(dict(trx, connection_id=cid or None))
        if cid and cid not in by_conn:
            by_conn[cid] = trx
    return by_conn, every, True


def _int(value):
    number = _number(value)
    return int(number) if number is not None else None


def _history_list(reader, server, status, notes, process):
    """{value, source} or None. See the module docstring for the sources."""
    if server.engine == "mariadb":
        value = _int(status.get("Innodb_history_list_length"))
        return {"value": value, "source": "Innodb_history_list_length"} if value is not None else None
    if process is False:
        return None
    try:
        rows = reader.rows("SELECT COUNT, STATUS FROM information_schema.INNODB_METRICS "
                           "WHERE NAME = 'trx_rseg_history_len'")
    except ConnectorError as exc:
        if "ERROR 2002" in str(exc) or "ERROR 2013" in str(exc) or "timed out" in str(exc):
            raise
        return None
    if not rows or len(rows[0]) < 2:
        return None
    if (rows[0][1] or "").lower() != "enabled":
        notes.append("The InnoDB metric trx_rseg_history_len is disabled, so the history list length is "
                     "unknown here (the InnoDB view reads it from SHOW ENGINE INNODB STATUS).")
        return None
    return {"value": _int(rows[0][0]), "source": "information_schema.INNODB_METRICS trx_rseg_history_len"}


LAST_STATEMENT_CAP = 200


def _last_statements(reader, server, idle, notes):
    """The last statement of sessions idle in a transaction, from
    performance_schema.events_statements_current (MySQL enables the consumer by
    default; MariaDB ships Performance Schema off). Best effort."""
    if not idle:
        return
    if not server.ps_on:
        notes.append("The last statement of a session idle in a transaction needs Performance Schema "
                     "(events_statements_current)%s." % (
                         ", which MariaDB ships off (and its events_statements_current consumer is off by default "
                         "even with Performance Schema on)" if server.engine == "mariadb" else ""))
        return
    ids = ", ".join(str(s["id"]) for s in idle[:LAST_STATEMENT_CAP] if s["id"])
    if not ids:
        return
    try:
        rows = reader.rows("SELECT t.PROCESSLIST_ID, LEFT(s.SQL_TEXT, %d) FROM "
                           "performance_schema.events_statements_current s JOIN performance_schema.threads t "
                           "ON t.THREAD_ID = s.THREAD_ID WHERE t.PROCESSLIST_ID IN (%s) AND "
                           "(s.NESTING_EVENT_TYPE IS NULL OR s.NESTING_EVENT_TYPE = 'TRANSACTION') "
                           "ORDER BY s.EVENT_ID" % (INFO_CAP, ids))
    except ConnectorError as exc:
        if "ERROR 2002" in str(exc) or "ERROR 2013" in str(exc) or "timed out" in str(exc):
            raise
        return
    by_id = {s["id"]: s for s in idle}
    for row in rows:
        session = by_id.get(_int(row[0]) if row else None)
        if session is not None and len(row) > 1 and row[1]:
            session["last_statement"] = row[1]
    if not rows and server.engine == "mariadb":
        notes.append("The last statement of a session idle in a transaction needs the events_statements_current "
                     "consumer, which MariaDB ships off even with Performance Schema on.")


# ---- shaping ------------------------------------------------------------------

def _session(row, engine, source, trx):
    if engine == "mariadb":
        row = list(row) + [None] * (14 - len(row))
    else:
        row = list(row) + [None] * (9 - len(row))
    user, host, command, state = row[1], row[2], row[4] or "", row[6] or None
    seconds = _number(row[5])
    info = row[7]
    length = _int(row[8])
    client_host, client_port = split_host(host)
    kind = thread_kind(user, command, state)
    truncated = bool(info and length is not None and length > len(info.encode("utf-8"))) or bool(
        info and source == "performance_schema.processlist" and len(info.encode("utf-8")) >= PS_INFO_BYTES)
    session = {
        "id": _int(row[0]), "user": user, "host": host, "client_host": client_host,
        "client_port": client_port, "db": row[3], "command": command,
        "time_s": _round(float(seconds), 3) if seconds is not None else None, "state": state,
        "info": info, "info_short": _short(info) if info else None, "info_truncated": truncated,
        "thread_kind": kind, "trx": None, "lock_wait": None, "last_statement": None, "progress": None,
        "memory_used": None, "rows_examined": None,
        "explainable": command == "Query" and bool(info) and kind == "client",
    }
    if engine == "mariadb":
        max_stage = _int(row[10])
        if max_stage:
            session["progress"] = {"stage": _int(row[9]), "max_stage": max_stage,
                                   "pct": _round(_number(row[11]), 1)}
        session["memory_used"] = _int(row[12])
        session["rows_examined"] = _int(row[13])
    if trx is not None:
        session["trx"] = dict(trx)
    return session


def _assess(session):
    """Flags, status and why for one session (see RULES)."""
    flags, statuses, why = [], [], []
    trx = session["trx"]
    seconds = session["time_s"]
    system = session["thread_kind"] != "client"
    own = bool(session["info"] and MYFLAMES_TAG in session["info"])
    if system:
        flags.append("system_thread")
    if own:
        flags.append("own_connection")
    state = session["state"] or ""
    if trx and trx["state"] == "LOCK WAIT":
        session["lock_wait"] = {"kind": "innodb", "wait_s": trx["wait_s"], "detail": trx["operation_state"]}
    elif is_mdl_wait_state(state):
        session["lock_wait"] = {"kind": "metadata", "wait_s": _int(seconds), "detail": state}
    elif state == TABLE_LOCK_STATE:
        session["lock_wait"] = {"kind": "table", "wait_s": _int(seconds), "detail": state}
    # The process list and INNODB_TRX are two reads, so a session that went
    # idle (or started its next statement) between them can look idle in a
    # transaction: require a second of idleness and a transaction at least as
    # old as the idle time (whole seconds, so one second of slack).
    idle_in_trx = (session["command"] == "Sleep" and trx is not None and seconds is not None and seconds >= 1
                   and trx["age_s"] is not None and trx["age_s"] + 1 >= seconds)
    running = session["command"] in STATEMENT_COMMANDS and not system
    if running and seconds is not None and seconds >= LONG_RUNNING_S[0]:
        flags.append("long_running")
        if not own:
            statuses.append(_grade_seconds(seconds, LONG_RUNNING_S))
            why.append("running a statement for %s" % _duration_text(seconds))
    if idle_in_trx:
        flags.append("idle_in_trx")
        if not own:
            statuses.append(_grade_seconds(trx["age_s"], TRX_AGE_S))
            why.append("idle for %s inside a transaction open for %s" % (
                _duration_text(seconds or 0), _duration_text(trx["age_s"] or 0)))
    if session["lock_wait"] is not None:
        flags.append("lock_wait")
        if not own:
            statuses.append("watch")
            wait = session["lock_wait"]
            why.append("waiting for %s%s" % (
                {"innodb": "an InnoDB lock (row or table)", "metadata": "a metadata lock",
                 "table": "a table-level lock"}[wait["kind"]],
                " for %s" % _duration_text(wait["wait_s"]) if wait["wait_s"] is not None else ""))
    if trx and not idle_in_trx and not system and not own and trx["age_s"] is not None \
            and trx["age_s"] >= TRX_AGE_S[0] and "long_running" not in flags:
        statuses.append(_grade_seconds(trx["age_s"], TRX_AGE_S))
        why.append("inside a transaction open for %s" % _duration_text(trx["age_s"]))
    session["flags"] = flags
    session["status"] = _worst(statuses)
    session["why"] = ("; ".join(why)[:1].upper() + "; ".join(why)[1:] + ".") if why else None
    session["active"] = (session["command"] != "Sleep" and not system) or idle_in_trx \
        or session["lock_wait"] is not None
    return session


def _matches(session, filters):
    if filters["active_only"] and not session["active"]:
        return False
    if filters["user"] is not None and session["user"] != filters["user"]:
        return False
    if filters["host"] is not None:
        wanted = filters["host"].lower()
        if wanted not in ((session["client_host"] or "").lower(), (session["host"] or "").lower()):
            return False
    if filters["db"] is not None and session["db"] != filters["db"]:
        return False
    if filters["min_seconds"] and (session["time_s"] is None or session["time_s"] < filters["min_seconds"]):
        return False
    return True


def _breakdown(values, top=TOP_N):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], "" if kv[0] is None else str(kv[0])))
    out = [{"key": k, "count": c} for k, c in ordered[:top]]
    rest = sum(c for _, c in ordered[top:])
    if rest:
        out.append({"key": None, "other": True, "count": rest})
    return out


def _connections(server, status, listed):
    current = _int(status.get("Threads_connected"))
    maximum = _int(server.var("max_connections"))
    peak = _int(status.get("Max_used_connections"))
    used = 100.0 * current / maximum if current is not None and maximum else None
    return {
        "current": current, "max": maximum, "max_used": peak, "running": _int(status.get("Threads_running")),
        "listed": listed, "used_pct": _round(used, 1),
        "peak_pct": _round(100.0 * peak / maximum, 1) if peak is not None and maximum else None,
        "status": _grade(used, CONNECTIONS_USED_PCT) if used is not None else "unknown",
        "rule": RULES["connections"],
    }


def _oldest(every, sessions_by_id):
    if not every:
        return None
    trx = min(every, key=lambda t: (t["age_s"] is None, -(t["age_s"] or 0)))
    session = sessions_by_id.get(trx["connection_id"]) or {}
    return {
        "connection_id": trx["connection_id"], "trx_id": trx["id"], "age_s": trx["age_s"],
        "state": trx["state"], "read_only": trx["read_only"], "isolation_level": trx["isolation_level"],
        "rows_locked": trx["rows_locked"], "rows_modified": trx["rows_modified"],
        "session_command": session.get("command"), "user": session.get("user"),
        "client_host": session.get("client_host"),
        "idle": session.get("command") == "Sleep",
        "status": _grade_seconds(trx["age_s"], TRX_AGE_S), "rule": RULES["oldest_transaction"],
    }


def _summary_text(summary):
    c = summary["connections"]
    parts = []
    if c["current"] is not None and c["max"]:
        parts.append("%s connection%s of %s" % (_count_text(c["current"]), "" if c["current"] == 1 else "s",
                                                _count_text(c["max"])))
    else:
        parts.append("%s session%s listed" % (_count_text(summary["total"]), "" if summary["total"] == 1 else "s"))
    parts.append("%s active" % _count_text(summary["active"]))
    if summary["idle_in_trx"]:
        parts.append("%s idle in transaction" % _count_text(summary["idle_in_trx"]))
    if summary["lock_wait"]["total"]:
        parts.append("%s waiting for a lock" % _count_text(summary["lock_wait"]["total"]))
    long_running = summary["long_running"]["watch"] + summary["long_running"]["problem"]
    if long_running:
        parts.append("%s running 10 s or more" % _count_text(long_running))
    if summary["oldest_transaction"] and summary["oldest_transaction"]["age_s"] is not None:
        parts.append("oldest transaction %s" % _duration_text(summary["oldest_transaction"]["age_s"]))
    return " · ".join(parts)


def sessions_report(conn, active_only=True, user=None, host=None, db=None, min_seconds=0,
                    limit=DEFAULT_LIMIT, identity=False):
    """Return the ``sessions-1.0`` report. Read-only; see the module docstring.

    *active_only*, *user*, *host* (client host, with or without the port),
    *db* and *min_seconds* filter the ``sessions`` list; ``summary`` and the
    flag counts always cover every session this account can see.
    """
    filters = _check_args(active_only, user, host, db, min_seconds, limit)
    reader = Reader(conn, SESSIONS_TAG)
    notes = []
    try:
        variables = show_variables(reader, ("version", "version_comment", "performance_schema",
                                            "max_connections"))
        status = reader.pairs("SHOW GLOBAL STATUS WHERE Variable_name IN ('Threads_connected', "
                              "'Threads_running', 'Max_used_connections', 'Uptime', "
                              "'Innodb_history_list_length')")
    except ConnectorError as exc:
        raise ValueError("Could not read the server variables: " + reason(exc))
    server = Server(variables, status)
    # Capture the same identity kill_session rechecks, while reading this report.
    keys = ("server_id", "hostname", "version") if server.engine == "mariadb" else ("server_uuid",)
    server_identity = {}
    if identity:  # the workspace's Kill sheet; the report and CLI do not need it
        try:
            identity_rows = reader.rows("SELECT " + ", ".join("@@" + key for key in keys))
            if identity_rows and len(identity_rows[0]) == len(keys) and all(identity_rows[0]):
                server_identity = dict(zip(keys, identity_rows[0]))
        except ConnectorError:
            notes.append("Could not read the server identity; refresh before using Kill.")
    source = _processlist_source(server.engine, server.version_tuple, server.ps_on)
    try:
        rows = reader.rows(_processlist_sql(server.engine, source))
    except ConnectorError as exc:
        if denied(exc):
            raise ValueError("This user cannot read %s: %s" % (source, reason(exc)))
        raise ValueError("Could not read the process list: " + reason(exc))
    truncated_read = len(rows) > READ_CAP
    rows = rows[:READ_CAP]
    by_conn, every, process = _read_transactions(reader, notes)
    history = _history_list(reader, server, status, notes, process)

    sessions = [_assess(_session(row, server.engine, source, by_conn.get(_int(row[0]))))
                for row in rows if row]
    by_id = {s["id"]: s for s in sessions}
    _last_statements(reader, server, [s for s in sessions if "idle_in_trx" in s["flags"]], notes)
    clients = [s for s in sessions if s["thread_kind"] == "client"]

    lock_kinds = {"innodb": 0, "metadata": 0, "table": 0}
    for s in sessions:
        if s["lock_wait"] is not None:
            lock_kinds[s["lock_wait"]["kind"]] += 1
    long_counts = {"watch": 0, "problem": 0}
    for s in sessions:
        if "long_running" in s["flags"] and "own_connection" not in s["flags"]:
            grade = _grade_seconds(s["time_s"], LONG_RUNNING_S)
            if grade in long_counts:
                long_counts[grade] += 1
    oldest = _oldest(every, by_id)
    from .innodb_status import history_status
    history_item = None
    if history is not None:
        hstatus, hrule = history_status(history["value"])
        history_item = dict(history, status=hstatus, rule=hrule)
    connections = _connections(server, status, len(sessions))
    summary = {
        "total": len(sessions),
        "active": sum(1 for s in sessions if s["active"]),
        "idle": sum(1 for s in clients if s["command"] == "Sleep" and "idle_in_trx" not in s["flags"]),
        "system": len(sessions) - len(clients),
        "running_statements": sum(1 for s in clients if s["command"] in STATEMENT_COMMANDS),
        "long_running": long_counts,
        "idle_in_trx": sum(1 for s in sessions if "idle_in_trx" in s["flags"]),
        "lock_wait": dict(lock_kinds, total=sum(lock_kinds.values())),
        "connections": connections,
        "by_command": _breakdown(s["command"] for s in sessions),
        "by_state": _breakdown(s["state"] for s in sessions if s["state"]),
        "by_user": _breakdown(s["user"] for s in sessions),
        "by_host": _breakdown(s["client_host"] for s in sessions),
        "by_db": _breakdown(s["db"] for s in sessions),
        "oldest_transaction": oldest,
        "history_list_length": history_item,
    }
    summary["status"] = _worst([s["status"] for s in sessions] + [connections["status"]]
                               + ([oldest["status"]] if oldest else [])
                               + ([history_item["status"]] if history_item else []))
    summary["text"] = _summary_text(summary)

    matched = [s for s in sessions if _matches(s, filters)]
    matched.sort(key=lambda s: (-SEVERITY.get(s["status"], 0), -(s["time_s"] or 0), s["id"] or 0))
    listed = matched[:limit]

    def ids(flag, seconds):
        chosen = [s for s in matched if flag in s["flags"] and "own_connection" not in s["flags"]]
        chosen.sort(key=lambda s: -(seconds(s) or 0))
        return [{"id": s["id"], "seconds": seconds(s), "status": s["status"]} for s in chosen]

    attention = {
        "long_running": ids("long_running", lambda s: s["time_s"]),
        "idle_in_trx": ids("idle_in_trx", lambda s: s["trx"]["age_s"]),
        "lock_wait": [dict(item, kind=by_id[item["id"]]["lock_wait"]["kind"])
                      for item in ids("lock_wait", lambda s: s["lock_wait"]["wait_s"])],
    }

    if process is False:
        notes.append("Without the PROCESS privilege only your own user's sessions are listed, and open InnoDB "
                     "transactions (information_schema.INNODB_TRX) cannot be read, so idle-in-transaction "
                     "sessions, row lock waits and the oldest transaction are not detected. Grant it with: "
                     "GRANT PROCESS ON *.* TO <user>;")
    if truncated_read:
        notes.append("More than %s sessions; only the %s with the longest time were read, so the summary is "
                     "partial." % ("{:,}".format(READ_CAP), "{:,}".format(READ_CAP)))
    if any(s["info_truncated"] for s in sessions):
        if source == "performance_schema.processlist":
            notes.append("Some statements are cut off: performance_schema.processlist keeps only the first %d "
                         "bytes of each statement." % PS_INFO_BYTES)
        else:
            notes.append("Some statements are cut off at %s characters." % "{:,}".format(INFO_CAP))
    if oldest and oldest["status"] != "ok":
        running = (by_id.get(oldest["connection_id"]) or {}).get("command") in STATEMENT_COMMANDS
        if oldest["isolation_level"] == "REPEATABLE READ":
            notes.append("The oldest open transaction (connection %s, %s) runs under REPEATABLE READ, so after its "
                         "first consistent read it keeps a read view until it ends: InnoDB cannot purge the undo "
                         "logs that view may still need, and the history list length grows."
                         % (oldest["connection_id"] or "none", _duration_text(oldest["age_s"])))
        elif running:
            notes.append("The oldest open transaction (connection %s, %s) is running a statement, whose read view "
                         "holds back purge until the statement ends." % (
                             oldest["connection_id"] or "none", _duration_text(oldest["age_s"])))
    notes.append("Statuses are rules of thumb, not verdicts. The summary counts every session this account can "
                 "see; the filters narrow only the list. This tool's own session is left out.")

    report = base_report(SCHEMA_VERSION, server)
    report.update({
        "server_identity": server_identity,
        "source": source,
        "capabilities": {"process_privilege": process, "transactions": process is True,
                         "history_list_length": history is not None},
        "filters": filters, "limit": limit,
        "thresholds": {"long_running_s": list(LONG_RUNNING_S), "trx_age_s": list(TRX_AGE_S)},
        "rules": dict(RULES),
        "summary": summary, "attention": attention,
        "matched": len(matched), "truncated": len(matched) > limit or truncated_read,
        "sessions": listed, "notes": notes, "links": dict(_LINKS),
    })
    return report


def at_or_worse(report, level):
    floor = SEVERITY[level]
    return [s for s in report["sessions"] if SEVERITY.get(s["status"], -1) >= floor]


# ---- text output for the CLI ----------------------------------------------------

_MARK = {"ok": "", "watch": "WATCH", "problem": "PROBLEM", "unknown": "?"}


def _secs(value):
    return "—" if value is None else ("%.1f s" % value if value < 100 else _duration_text(value))


def format_text(report):
    from .perf_common import table_text
    s = report["summary"]
    head = "%s %s, %s" % ("MariaDB" if report["engine"] == "mariadb" else "MySQL",
                          report["server_version"] or "?", report["source"])
    lines = [head, "Status: %s. %s" % (s["status"].upper() if s["status"] != "ok" else "ok", s["text"]), ""]
    c = s["connections"]
    lines.append("SUMMARY")
    if c["max"]:
        lines.append("  Connections   %s of max_connections %s (%.0f%%), peak %s, running %s%s" % (
            _count_text(c["current"]), _count_text(c["max"]), c["used_pct"] or 0,
            _count_text(c["max_used"]), _count_text(c["running"]),
            "  [%s]" % c["status"].upper() if c["status"] in ("watch", "problem") else ""))
    lines.append("  Sessions      %d listed: %d active, %d idle, %d system; %d idle in transaction; %d waiting "
                 "for a lock" % (s["total"], s["active"], s["idle"], s["system"], s["idle_in_trx"],
                                 s["lock_wait"]["total"]))
    for key, label in (("by_command", "By command"), ("by_state", "By state"), ("by_user", "By user"),
                       ("by_host", "By host"), ("by_db", "By database")):
        items = s[key]
        if items:
            lines.append("  %-13s %s" % (label, ", ".join(
                "%s %d" % ("other" if i.get("other") else ("(none)" if i["key"] is None else i["key"]), i["count"])
                for i in items[:6])))
    oldest = s["oldest_transaction"]
    if oldest:
        lines.append("  Oldest trx    connection %s, %s%s%s" % (
            oldest["connection_id"] or "?", _duration_text(oldest["age_s"]),
            " (idle)" if oldest["idle"] else "",
            "  [%s]" % oldest["status"].upper() if oldest["status"] != "ok" else ""))
    history = s["history_list_length"]
    if history:
        lines.append("  History list  %s%s" % (_count_text(history["value"]),
                                              "  [%s]" % history["status"].upper()
                                              if history["status"] in ("watch", "problem") else ""))
    flagged = [x for x in report["sessions"] if x["status"] in ("watch", "problem")]
    if flagged:
        lines += ["", "FLAGGED"]
        for x in flagged:
            lines.append("  %-7s %s %s@%s: %s" % (_MARK[x["status"]], x["id"], x["user"] or "?",
                                                  x["client_host"] or "?", x["why"] or ""))
    lines.append("")
    rows = [[str(x["id"]), _secs(x["time_s"]), x["user"] or "—", x["client_host"] or "—", x["db"] or "—",
             x["command"] or "—", _short(x["state"] or "", 28) or "—", ",".join(x["flags"]) or "—",
             _short(x["info"], 60) or ("last: " + _short(x["last_statement"], 54) if x["last_statement"] else "—")]
            for x in report["sessions"]]
    if rows:
        lines.append(table_text(['>ID', '>TIME', 'USER', 'HOST', 'DB', 'COMMAND', 'STATE', 'FLAGS', 'SQL'], rows))
    else:
        lines.append("No sessions match%s." % (" (active only; --all shows idle and system sessions)"
                                               if report["filters"]["active_only"] else ""))
    if report["truncated"]:
        lines.append("(%d of %d matching sessions shown; raise --limit to see more)" % (
            len(report["sessions"]), report["matched"]))
    return "\n".join(lines) + "\n"
