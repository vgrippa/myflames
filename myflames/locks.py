"""Who is blocking whom: a lock report for MySQL and MariaDB, plus one guarded KILL.

:func:`lock_report` returns the ``locks-1.1`` envelope shared by
``myflames locks`` and the workspace's Locks tab: open InnoDB transactions,
row locks, row lock waits, metadata lock waits, one blocking tree over all of
them with the root blockers marked, DDL statements waiting for a metadata lock
with their blockers and the sessions queued behind them, whether this account
may end other sessions, and the latest detected deadlock.

The report only reads: every statement is a SELECT or SHOW, with no
instrument or consumer updates and no ``innodb_status_output_locks``. (SHOW
ENGINE INNODB STATUS restarts the per-second averages it prints, as it does
for every reader.) The one write is :func:`kill_session`, which runs a single
``KILL`` or ``KILL QUERY`` only when a person asks for it and confirms, and
only after re-reading the session and checking it is still the one they saw.
Nothing here kills automatically or in bulk.

Verified against server source (mysql-server 9.7 trunk "M/", mariadb-server
13.0 trunk "Ma/"; older tags where noted):

* MySQL lists every InnoDB table and record lock, granted and waiting, in
  ``performance_schema.data_locks`` (M/storage/perfschema/table_data_locks.cc:
  54-68) and the waits in ``data_lock_waits``. Both need only SELECT on the
  table. ``ENGINE`` is ``INNODB`` (M/storage/innobase/handler/p_s.cc:134).
* Reading data_locks or data_lock_waits (scan and lookup by lock id) holds
  the global lock-system latch per batch on MySQL
  before 8.0.40, on 8.1.0 to 8.4.2, and on 9.0.x (``Global_exclusive_latch_guard``
  in p_s.cc at tags mysql-8.0.39, 8.3.0, 8.4.2, 9.0.1; 8.0.20 and earlier take
  the single lock_sys mutex). 8.0.40, 8.4.3 and 9.1.0 latch one shard at a
  time (Bug#36302624).
* ``information_schema.INNODB_TRX``, and on MariaDB ``INNODB_LOCKS`` and
  ``INNODB_LOCK_WAITS``, raise an error without PROCESS
  (``check_global_access(thd, PROCESS_ACL)``: M/storage/innobase/handler/
  i_s.cc:797, Ma/storage/innobase/handler/i_s.cc:177,975,1213,1473).
  ``trx_query`` is cut at 1024 bytes and LOCK_DATA at 8192 bytes
  (Ma/storage/innobase/include/trx0i_s.h:42-46; M/.../trx0i_s.cc).
* MariaDB ``INNODB_LOCKS`` lists only waiting locks and the locks blocking
  them; ``lock_mode`` is one of S, S,GAP, X, X,GAP, IS, IS,GAP, IX, IX,GAP,
  AUTO_INC (Ma/storage/innobase/handler/i_s.cc:573-584), so record-only and
  next-key locks both read X, and insert intention reads X,GAP.
* Statement rollback (KILL QUERY) does not release InnoDB row locks; they are
  freed at commit or rollback of the whole transaction (``lock_release``;
  MariaDB ``lock_release_on_rollback`` only serves CREATE TABLE rollback,
  Ma/storage/innobase/lock/lock0lock.cc:5127-5129).
* Table lock compatibility (IS, IX, S, X, AUTO-INC):
  M/storage/innobase/include/lock0priv.h:593-599 (same in MariaDB).
  Gap locks only block insert intention; insert intention blocks nothing:
  M/storage/innobase/lock/lock0lock.cc:590-615.
* Metadata lock compatibility (granted and default priority matrices) for
  object and scoped locks: M/sql/mdl.cc:2078-2290, Ma/sql/mdl.cc:2155-2270.
  MySQL scoped namespaces: M/sql/mdl.cc:822-836; MariaDB uses the scoped
  strategy for SCHEMA only and its own strategy for BACKUP (Ma/sql/mdl.cc:
  1110-1124). P_S names: M/storage/perfschema/pfs_column_types.cc:42-62,
  table_helper.cc:1345-1390; Ma/storage/perfschema/table_helper.cc:520-660.
* MariaDB ships the ``wait/lock/metadata/sql/mdl`` instrument disabled
  (Ma/storage/perfschema/pfs_instr_class.cc:233-236) and the
  ``events_statements_current`` consumer off (Ma/sql/mysqld.cc:861-865);
  MySQL enables both (M/sql/sys_vars.cc:650-655). Metadata locks are recorded
  only when ``global_instrumentation`` and the instrument are on
  (Ma/storage/perfschema/pfs.cc:6795-6798).
* Deadlock report text: M/storage/innobase/lock/lock0lock.cc:4250-4320,
  6060-6097 (``*** (n) TRANSACTION``, ``HOLDS THE LOCK(S)``, ``WAITING FOR
  THIS LOCK TO BE GRANTED``, ``WE ROLL BACK TRANSACTION (n)``); MariaDB adds
  ``*** CONFLICTING WITH:`` and prints ``MariaDB thread id``
  (Ma/storage/innobase/lock/lock0lock.cc:7182-7290, Ma/sql/sql_show.cc:11498).
* KILL privileges. MySQL ends another user's session for SUPER or
  CONNECTION_ADMIN, and any session whose login user name equals the
  killer's (``Security_context::user_matches`` compares names only, not
  hosts); a session of a SYSTEM_USER account also needs SYSTEM_USER
  (M/sql/sql_parse.cc ``kill_one_thread``). MariaDB needs CONNECTION ADMIN
  (``PRIV_KILL_OTHER_USER_PROCESS``): SUPER also counts from 10.5.2 to 10.11,
  and from 11.0 only for accounts whose grants were stored by an earlier
  version (``ALLOWED_BY_SUPER_BEFORE_110000``, Ma/sql/privilege.h,
  Ma/sql/sql_acl.cc). Plain SHOW GRANTS includes the roles active in the
  session (M/sql/sql_show.cc ``Sql_cmd_show_grants``; MariaDB
  ``current_user_and_current_role``), and every myflames statement opens a new
  session, so it sees the same default roles as the session that runs KILL.
* KILL CONNECTION rolls back the open transaction in ``THD::cleanup`` and only
  then releases its metadata locks (M/sql/sql_class.cc, Ma/sql/sql_class.cc),
  so the locks stay held while a large rollback runs; a prepared XA
  transaction is detached instead (MySQL keeps its metadata locks in
  MDL_context_backup_manager; MariaDB's trans_xa_detach releases them,
  Ma/sql/xa.cc). On MySQL 8.0.29+ ``xa_detach_on_prepare`` (ON by default)
  detaches it at XA PREPARE, and P_S keeps naming the session as the owner of
  its metadata locks, so ending that session releases nothing; myflames
  recognizes such a session by its last statement, XA PREPARE (from
  events_statements_current). KILL QUERY on an idle
  session does nothing: the flag is reset when the next command starts
  (M/sql/sql_class.cc ``THD::awake``, M/sql/sql_parse.cc ``dispatch_command``,
  Ma ``reset_kill_query``).
* The process list's "Killed" command is not proof that a session is ending:
  MariaDB also shows it after KILL QUERY on an idle session, and MySQL's
  performance_schema.threads never shows it (Ma/sql/sql_show.cc,
  M/sql/sql_show.cc). INNODB_TRX trx_state 'ROLLING BACK' is, and a second
  KILL is harmless on both engines.
* trx_rows_modified is InnoDB's undo record count (trx->undo_no): roughly the
  rows inserted, updated or deleted, where a row changed twice counts twice.
* ALTER TABLE opens the table with SHARED_UPGRADABLE and upgrades it to
  EXCLUSIVE (at the start, the end, or both, depending on the algorithm;
  M/sql/sql_table.cc ``mysql_inplace_alter_table``), waiting up to
  ``lock_wait_timeout`` (default 31536000 s on MySQL, 86400 s on MariaDB,
  sys_vars.cc). A pending EXCLUSIVE request has priority, so new SHARED_READ
  and SHARED_WRITE requests queue behind it (the waiting matrices above).
* MariaDB's optional METADATA_LOCK_INFO plugin lists granted metadata locks
  only, with THREAD_ID as the connection id and LOCK_MODE as ``MDL_*``, and no
  privilege check (Ma/plugin/metadata_lock_info/metadata_lock_info.cc).
"""
import datetime
import re
import sys
import time

from .connector import ConnectorError, MySQLConnection, terminal_text
from .perf_common import _denied, _reason, _server

SCHEMA_VERSION = "locks-1.1"
KILL_SCHEMA_VERSION = "kill-1.0"
#: The one write: KILL statements carry their own tag (and reach the server with --comments).
KILL_TAG = "/*myflames:kill*/ "
#: A blocker that changed at least this many rows gets a long-rollback warning.
LARGE_ROLLBACK_ROWS = 10000
#: Lock statements carry the capture-tag prefix so ``top`` excludes them.
LOCKS_TAG = "/*myflames:locks*/ "
MAX_LIMIT = 5000
MAX_MDL_EDGES = 5000
MAX_TREE_DEPTH = 128
MAX_TREE_NODES = 5000
MAX_TREE_EDGES = 5000
TEXT_CAP = 4096
LOCK_DATA_CAP = 512
SERVER_LOCK_DATA_MAX = 8192
SERVER_TRX_QUERY_MAX = 1024
RAW_DEADLOCK_CAP = 65536
DEADLOCK_RECORDS_CAP = 5

KILL_NOTE = ("myflames ends a session only when you choose Kill blocker (or run myflames locks "
             "--kill ID) and confirm. KILL QUERY n stops only the running statement; inside a "
             "transaction (after START TRANSACTION or BEGIN, or with autocommit=0), the transaction stays open and keeps its row and metadata "
             "locks until it commits or rolls back (in autocommit mode the statement is the "
             "whole transaction). KILL n closes the connection and rolls back its open "
             "transaction, which can take a while after many changes; its locks stay until the "
             "rollback ends. A prepared XA transaction survives both; end it with XA COMMIT or "
             "XA ROLLBACK.")
IMPLICIT_NOTE = ("Rows a transaction inserted, and index entries its updates wrote, are "
                 "protected by implicit locks, which InnoDB lists only after another session "
                 "conflicts with them. A transaction can hold more locks than the list shows.")
MARIADB_PS_OFF_MDL_NOTE = (
    "Performance Schema is off (the MariaDB default), so metadata locks, such as a table held open "
    "while ALTER TABLE waits, are not visible. To see them, add performance_schema=ON and "
    "performance-schema-instrument='wait/lock/metadata/sql/mdl=ON' under [mysqld] in my.cnf and "
    "restart.")
SNAPSHOT_NOTE = ("Each section comes from a separate query, so a lock can appear or "
                 "disappear between sections.")

# ---- metadata lock compatibility (see module docstring) --------------------

_X, _S, _SH, _SR, _SW, _SWLP, _SU, _SRO, _SNW, _SNRW, _IX = (
    "EXCLUSIVE", "SHARED", "SHARED_HIGH_PRIO", "SHARED_READ", "SHARED_WRITE",
    "SHARED_WRITE_LOW_PRIO", "SHARED_UPGRADABLE", "SHARED_READ_ONLY",
    "SHARED_NO_WRITE", "SHARED_NO_READ_WRITE", "INTENTION_EXCLUSIVE")
#: A request of type K cannot be granted while another session holds one of these.
OBJECT_GRANTED_CONFLICTS = {
    _S: {_X}, _SH: {_X}, _SR: {_X, _SNRW},
    _SW: {_X, _SNRW, _SNW, _SRO}, _SWLP: {_X, _SNRW, _SNW, _SRO},
    _SU: {_X, _SNRW, _SNW, _SU}, _SRO: {_X, _SNRW, _SWLP, _SW},
    _SNW: {_X, _SNRW, _SNW, _SU, _SWLP, _SW},
    _SNRW: {_X, _SNRW, _SNW, _SRO, _SU, _SWLP, _SW, _SR},
    _X: {_X, _SNRW, _SNW, _SRO, _SU, _SWLP, _SW, _SR, _SH, _S},
}
#: ...and it queues behind a waiting request of one of these (default priority
#: matrix; max_write_lock_count defaults to the maximum, so it rarely changes).
OBJECT_WAITING_CONFLICTS = {
    _S: {_X}, _SH: set(), _SR: {_X, _SNRW}, _SW: {_X, _SNRW, _SNW},
    _SWLP: {_X, _SNRW, _SNW, _SRO}, _SU: {_X}, _SRO: {_X, _SNRW, _SW},
    _SNW: {_X}, _SNRW: {_X}, _X: set(),
}
SCOPED_GRANTED_CONFLICTS = {_IX: {_X, _S}, _S: {_X, _IX}, _X: {_X, _S, _IX}}
SCOPED_WAITING_CONFLICTS = {_IX: {_X, _S}, _S: {_X}, _X: set()}
MYSQL_SCOPED = ("GLOBAL", "TABLESPACE", "SCHEMA", "COMMIT", "BACKUP LOCK",
                "RESOURCE_GROUPS", "FOREIGN KEY", "CHECK CONSTRAINT")
MDL_SHORT = {_X: "X", _S: "S", _SH: "SH", _SR: "SR", _SW: "SW", _SWLP: "SWLP",
             _SU: "SU", _SRO: "SRO", _SNW: "SNW", _SNRW: "SNRW", _IX: "IX"}
MDL_EXPLAIN = {
    _SR: "Shared read: a statement is reading the table, or a transaction read it and has not "
         "ended (on MariaDB, also LOCK TABLES ... READ on InnoDB tables).",
    _SW: "Shared write: a statement is changing rows or running SELECT ... FOR UPDATE, or a "
         "transaction did so and has not ended.",
    _SWLP: "Shared write, low priority (LOW_PRIORITY writes).",
    _SU: "Shared upgradable: ALTER TABLE and similar DDL take this first, then upgrade it.",
    _SRO: "Shared read only: LOCK TABLES ... READ (MySQL).",
    _SNW: "Shared no write: lets others read but not write (for example ALTER TABLE's copy phase).",
    _SNRW: "Shared no read write: LOCK TABLES ... WRITE.",
    _X: "Exclusive: DDL such as ALTER, DROP, RENAME or TRUNCATE needs the object to itself.",
    _S: "Shared: reads only the definition.",
    _SH: "Shared high priority: reads the definition (for example information_schema queries).",
    _IX: "Intention exclusive: a statement that may change data or metadata (scoped lock).",
}


def _mdl_rules(engine, object_type, lock_type):
    """(granted conflicts, waiting conflicts) or (None, None) when unknown."""
    if engine == "mysql" and object_type in MYSQL_SCOPED or engine == "mariadb" and object_type == "SCHEMA":
        table = SCOPED_GRANTED_CONFLICTS, SCOPED_WAITING_CONFLICTS
    elif engine == "mariadb" and object_type == "BACKUP":
        return None, None
    else:
        table = OBJECT_GRANTED_CONFLICTS, OBJECT_WAITING_CONFLICTS
    if lock_type not in table[0]:
        return None, None
    return table[0][lock_type], table[1].get(lock_type, set())


# ---- row lock modes in plain language --------------------------------------

def explain_lock_mode(engine, lock_type, mode, lock_data=None):
    """Return (short label, one-sentence plain-language explanation)."""
    mode = (mode or "").upper().replace(" ", "")
    flags = set(mode.split(",")[1:]) if mode else set()
    base = mode.split(",")[0] if mode else ""
    strength = {"S": "shared", "X": "exclusive"}.get(base, "")
    if (lock_type or "").upper() == "TABLE":
        table = {
            "IS": ("intention shared", "The transaction is read-locking some rows of this table "
                   "(SELECT ... FOR SHARE). It conflicts only with a whole-table exclusive lock."),
            "IX": ("intention exclusive", "The transaction is write-locking some rows of this table "
                   "(UPDATE, DELETE, INSERT, SELECT ... FOR UPDATE). Intention locks never block each "
                   "other; this one conflicts only with whole-table S and X locks."),
            "S": ("table shared", "The whole table is read-locked (for example LOCK TABLES ... READ). "
                  "Writers to any row must wait."),
            "X": ("table exclusive", "The whole table is locked exclusively; every other lock on it waits."),
            "AUTO_INC": ("AUTO-INC", "Hands out AUTO_INCREMENT values. It is released when the inserting "
                         "statement ends, not at commit, and conflicts with other AUTO-INC, S and X table locks."),
        }
        return table.get(base, (mode.lower() or "table lock", "A table-level lock."))
    supremum = bool(lock_data) and "supremum pseudo-record" in lock_data
    tail = (" This one sits on the supremum, the marker after the last record of the page, "
            "so it guards the open-ended gap at the end of the range.") if supremum else ""
    if "PREDICATE" in flags or "PRDT_PAGE" in flags:
        return "predicate", "A predicate lock on a spatial (R-tree) index."
    if engine == "mariadb":
        if "GAP" in flags:
            return ("gap or insert intention", "MariaDB shows gap locks and insert-intention locks "
                    "both as %s,GAP. A gap lock stops other transactions from inserting before this "
                    "record without locking the record; an insert intention is an INSERT waiting to "
                    "add a row in that gap.%s" % (base, tail))
        return ("record or next-key", "MariaDB shows record-only and next-key locks both as %s. "
                "It locks this index record (%s) and, for a next-key lock, the gap before it.%s"
                % (base, strength, tail))
    if "INSERT_INTENTION" in flags:
        return ("insert intention", "An INSERT waiting to add a row in the gap before this record. "
                "It waits only for gap or next-key locks other transactions hold there; inserts into "
                "the same gap do not block each other.%s" % tail)
    if "GAP" in flags:
        return ("gap", "Locks the gap before this record, not the record, so no other transaction "
                "can insert there. Gap locks never conflict with each other, only with inserts.%s" % tail)
    if "REC_NOT_GAP" in flags:
        return ("record only", "Locks only this index record (%s); the gap before it stays open."
                % (strength or "lock"))
    if base in ("S", "X"):
        return ("next-key", "Locks this index record (%s) and the gap before it, so no other "
                "transaction can change it or insert just before it. REPEATABLE READ uses these "
                "for locking reads and range scans.%s" % (strength, tail))
    return (mode.lower() or "lock", "A record lock.")


# ---- helpers ---------------------------------------------------------------

def _null(value):
    return None if value in (None, "NULL") else value


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _q(conn, sql):
    """Run tagged SQL; return rows of unescaped strings with NULL as None."""
    reader = getattr(conn, "query_lock_rows", None)
    if reader is not None:
        return reader(LOCKS_TAG + sql)
    text = conn.run(LOCKS_TAG + sql, extra_flags=["--comments"])
    rows = []
    # Batch mode escapes only \0, \t, \n and \\; a raw \r (a CRLF statement)
    # must not split a row, so split on \n alone, not str.splitlines().
    for line in text.replace("\r\n", "\n").split("\n"):
        if line:
            rows.append([_null(MySQLConnection._unescape_mysql(c)) for c in line.split("\t")])
    return rows


def _needs_process(exc):
    text = str(exc)
    return "PROCESS" in text or "1227" in text


def _cap(text, limit):
    """(text cut to *limit* characters, whether it was cut)."""
    if text is None:
        return None, False
    if len(text) > limit:
        return text[:limit - 1] + "…", True
    return text, False


def data_locks_latch_caution(engine, ver):
    """True when reading data_locks holds InnoDB's global lock-system latch."""
    if engine != "mysql":
        return False
    return (ver < (8, 0, 40) or (8, 1, 0) <= ver < (8, 4, 3) or (9, 0, 0) <= ver < (9, 1, 0))


def _ids(values):
    out = []
    for v in values:
        if isinstance(v, int) and not isinstance(v, bool) and v > 0 and v not in out:
            out.append(v)
    return out


_TABLE_NAME = re.compile(r"^`((?:[^`]|``)*)`\.`((?:[^`]|``)*)`(?:\s*/\*\s*Partition\s*`((?:[^`]|``)*)`)?")


def _split_table(name):
    """`db`.`t` (MariaDB INNODB_LOCKS / status output) -> (schema, table, partition)."""
    if not name:
        return None, None, None
    match = _TABLE_NAME.match(name)
    if not match:
        return None, name, None
    unq = [g.replace("``", "`") if g is not None else None for g in match.groups()]
    return unq[0], unq[1], unq[2]


def _lock(engine, lock_id, trx_id, schema, table, partition, index, lock_type, mode, status,
          data, data_len, ps_thread=None):
    data, cut = _cap(data, LOCK_DATA_CAP)
    length = _int(data_len)
    label, why = explain_lock_mode(engine, lock_type, mode, data)
    record = (lock_type or "").upper() == "RECORD"
    return {
        "lock_id": lock_id, "trx_id": trx_id, "connection_id": None, "ps_thread_id": ps_thread,
        "schema": schema, "table": table, "partition": partition, "index": index,
        "lock_type": (lock_type or "").upper() or None, "mode": mode, "status": status,
        "lock_data": data,
        # The server silently cuts LOCK_DATA at 8 KB; myflames shows 512 characters.
        "lock_data_truncated": bool(cut or (length is not None and length >= SERVER_LOCK_DATA_MAX - 4)),
        "lock_data_missing": record and data is None,
        "mode_label": label, "explain": why,
    }


# ---- the report ------------------------------------------------------------

class _Report(object):
    def __init__(self, engine, version, ver, ps_on, limit, scope):
        self.engine, self.version, self.ver, self.ps_on = engine, version, ver, ps_on
        self.server_identity = {}
        self.limit, self.scope = limit, scope
        self.notes = []
        self.caps = {
            "performance_schema": ps_on, "process_privilege": None,
            "transactions": False, "row_locks": "none", "lock_waits": False,
            "metadata_locks": False, "mdl_instrument": None, "last_statement": False,
            "deadlock": False, "data_locks_global_latch": data_locks_latch_caution(engine, ver),
            "metadata_lock_info": None,
        }
        self.trx = []
        self.trx_by_id = {}
        self.waits = []
        self.locks = []
        self.mdl_locks = []
        self.mdl_waits = []
        self.sessions = {}
        self.ps_threads = {}
        #: Sessions in a metadata lock wait state (from the process list).
        self.mdl_waiters = []
        #: Granted table metadata locks from MariaDB's METADATA_LOCK_INFO plugin.
        self.mli_locks = []
        self.kill_privilege = None
        #: connection id -> its prepared XA transaction (MySQL; see _read_xa_states).
        self.xa = {}
        self.truncated = {"transactions": False, "row_locks": False, "waits": False,
                          "metadata_locks": False}

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)

    def no_process(self, exc):
        self.caps["process_privilege"] = False
        if self.engine == "mariadb":
            self.note("Without the PROCESS privilege MariaDB hides information_schema.INNODB_TRX, "
                      "INNODB_LOCKS and INNODB_LOCK_WAITS and SHOW ENGINE INNODB STATUS, so "
                      "transactions, row locks, row lock waits and the latest deadlock are missing. "
                      "Grant it with: "
                      "GRANT PROCESS ON *.* TO <user>;")
        else:
            self.note("Without the PROCESS privilege MySQL hides information_schema.INNODB_TRX and "
                      "SHOW ENGINE INNODB STATUS, so transaction ages, isolation levels and the "
                      "latest deadlock are missing. Row locks and waits still come from "
                      "performance_schema; their owner is the lock's thread, which can be another "
                      "session when InnoDB converted an implicit lock. Grant it with: "
                      "GRANT PROCESS ON *.* TO <user>;")


def _check_limit(limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("Choose a limit from 1 to %d." % MAX_LIMIT)
    return limit


def lock_report(conn, limit=500, waits_only=False, deadlock_only=False, identity=False):
    """Return the ``locks-1.1`` report. Read-only; see the module docstring.

    ``waits_only`` skips the full lock list, held metadata locks and the
    deadlock (cheaper on a busy server); ``deadlock_only`` reads only
    SHOW ENGINE INNODB STATUS. Sections the server or the user's privileges
    do not allow stay empty, and ``notes`` says why.
    """
    limit = _check_limit(limit)
    if waits_only and deadlock_only:
        raise ValueError("Choose either waits only or the deadlock only.")
    scope = "deadlock" if deadlock_only else "waits" if waits_only else "all"
    engine, version, ver, ps_on = _server(conn)
    r = _Report(engine, version, ver, ps_on, limit, scope)
    # Bind a later Kill confirmation to the server that supplied this report.
    # Only the workspace asks (identity=True); the CLI Kill path re-reads it.
    keys = ("server_id", "hostname", "version") if engine == "mariadb" else ("server_uuid",)
    r.server_identity = {}
    if identity:
        try:
            rows = _q(conn, "SELECT " + ", ".join("@@" + key for key in keys))
            if rows and len(rows[0]) == len(keys) and all(rows[0]):
                r.server_identity = dict(zip(keys, rows[0]))
        except ConnectorError:
            r.note("Could not read the server identity; refresh before using Kill.")
    if not deadlock_only:
        _read_transactions(conn, r)
        _read_waits(conn, r)
        if not waits_only:
            _read_locks(conn, r)
        _read_mdl(conn, r, waits_only)
        _read_mdl_waiters(conn, r)
        _read_metadata_lock_info(conn, r)
        _read_sessions(conn, r)
        for waiter in r.mdl_waiters:
            r.sessions.setdefault(waiter["connection_id"], waiter)
        _resolve_owners(r)
        _read_last_statements(conn, r)
        _read_xa_states(conn, r)
        r.kill_privilege = read_kill_privilege(conn, engine, ver)
    deadlock = None
    if not waits_only:
        deadlock = _read_deadlock(conn, r)
    return _assemble(r, deadlock, waits_only)


def alter_table_preflight(conn, schema, table, engine, version, ps_on, limit=500):
    """A bounded read-only ALTER snapshot using the Locks report readers.

    Transactions are attributed by their granted table metadata locks, never
    by searching SQL text. Unattributed transactions remain explicit; an empty
    or truncated instrument cannot establish that a table has no blockers.
    """
    from .server_info import server_version_tuple
    r = _Report(engine, version, server_version_tuple(version), ps_on, _check_limit(limit), "alter")
    r.preflight_target = (schema, table)
    result = {"schema": schema, "table": table, "engine": engine, "version": version,
              "table_engine": None, "approx_bytes": None, "metadata_locks": [], "transactions": [],
              "unattributed_transactions": [], "sessions": [], "metadata_known": False,
              "transactions_known": False, "truncated": False, "warnings": r.notes}
    if not schema:
        r.note("No current database could be verified; qualify the ALTER target as database.table.")
        return result
    try:
        rows = _q(conn, "SELECT ENGINE, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES "
                  "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s" % (_lit(schema), _lit(table)))
        if rows and len(rows[0]) >= 3:
            result["table_engine"] = rows[0][0]
            sizes = [_int(v) for v in rows[0][1:3]]
            if all(v is not None for v in sizes):
                result["approx_bytes"] = sum(sizes)
        else:
            r.note("Table size and engine are unknown: the table is absent, temporary, or not visible to this account.")
    except ConnectorError as exc:
        r.note("Table size and engine could not be read: " + _reason(exc))
    for reader in (_read_transactions, lambda c, report: _read_mdl(c, report, False)):
        try:
            reader(conn, r)
        except (ConnectorError, ValueError) as exc:
            r.note("Lock preflight is incomplete: " + str(exc))
    # _read_mdl scopes the SQL to this table. Keep the returned spelling:
    # lower_case_table_names can normalize the user's identifier casing.
    target_locks = list(r.mdl_locks)
    owners = {m["connection_id"] for m in target_locks if m["status"] == "GRANTED"}
    result.update(metadata_locks=target_locks,
                  transactions=[t for t in r.trx if t["connection_id"] in owners],
                  unattributed_transactions=[t for t in r.trx if t["connection_id"] not in owners],
                  metadata_known=r.caps["metadata_locks"], transactions_known=r.caps["transactions"],
                  truncated=any(r.truncated.values()))
    try:
        _read_sessions(conn, r)
        result["sessions"] = [v for k, v in r.sessions.items() if k in owners]
    except (ConnectorError, ValueError) as exc:
        r.note("Current statements could not be read: " + str(exc))
    if not result["metadata_known"] or not result["transactions_known"] or result["truncated"]:
        r.note("The lock snapshot is incomplete; an empty list does not prove the table is free of blockers.")
    if result["unattributed_transactions"]:
        r.note("Other open transactions were found; their table ownership is not established by this snapshot.")
    r.note("Table size is an approximate DATA_LENGTH + INDEX_LENGTH snapshot; locks can change after review.")
    return result


def _read_transactions(conn, r):
    sql = ("SELECT trx_id, trx_state, trx_started, TIMESTAMPDIFF(SECOND, trx_started, NOW()), "
           "trx_requested_lock_id, trx_wait_started, TIMESTAMPDIFF(SECOND, trx_wait_started, NOW()), "
           "trx_mysql_thread_id, LEFT(trx_query, %d), trx_operation_state, trx_tables_in_use, "
           "trx_tables_locked, trx_lock_structs, trx_rows_locked, trx_rows_modified, "
           "trx_isolation_level, trx_is_read_only FROM information_schema.INNODB_TRX "
           "WHERE trx_mysql_thread_id <> CONNECTION_ID() ORDER BY trx_started, trx_id LIMIT %d"
           % (TEXT_CAP, r.limit + 1))
    try:
        rows = _q(conn, sql)
    except ConnectorError as exc:
        if _needs_process(exc):
            r.no_process(exc)
            return
        if _denied(exc):
            r.note("This user cannot read information_schema.INNODB_TRX: " + _reason(exc))
            return
        raise ValueError("Could not read information_schema.INNODB_TRX: " + _reason(exc))
    r.caps["transactions"] = True
    r.caps["process_privilege"] = True
    if len(rows) > r.limit:
        rows = rows[:r.limit]
        r.truncated["transactions"] = True
    for row in rows:
        row = list(row) + [None] * (17 - len(row))
        query = row[8]
        thread = _int(row[7])
        item = {
            "trx_id": row[0], "state": row[1], "started": row[2], "age_s": _int(row[3]),
            "connection_id": thread or None, "requested_lock_id": row[4],
            "wait_started": row[5], "wait_s": _int(row[6]) if row[5] else None,
            "query": query,
            "query_truncated": bool(query and len(query.encode("utf-8")) >= SERVER_TRX_QUERY_MAX - 4),
            "operation_state": row[9], "tables_in_use": _int(row[10]),
            "tables_locked": _int(row[11]), "lock_structs": _int(row[12]),
            "rows_locked": _int(row[13]), "rows_modified": _int(row[14]),
            "isolation_level": row[15], "read_only": row[16] in ("1", "ON"),
        }
        r.trx.append(item)
        r.trx_by_id[item["trx_id"]] = item


def _read_waits(conn, r):
    if r.engine == "mysql":
        if not r.ps_on:
            r.note("Performance Schema is off. MySQL lists row locks and lock waits only in "
                   "performance_schema.data_locks and data_lock_waits, and metadata locks in "
                   "metadata_locks. Set performance_schema=ON in my.cnf and restart. "
                   "Transactions and the latest deadlock are still shown.")
            return
        cols = ("%(a)s.OBJECT_SCHEMA, %(a)s.OBJECT_NAME, %(a)s.PARTITION_NAME, %(a)s.INDEX_NAME, "
                "%(a)s.LOCK_TYPE, %(a)s.LOCK_MODE, %(a)s.LOCK_STATUS, LEFT(%(a)s.LOCK_DATA, %(n)d), "
                "LENGTH(%(a)s.LOCK_DATA)")
        # InnoDB lists a waiter as waiting for every conflicting request queued
        # ahead of it too: 400 sessions waiting for one row give 80,200 rows,
        # 400 of them on the granted lock (measured live on MySQL 8.4.11). Edges
        # on granted locks come first so a capped read keeps every real blocker
        # (an unordered LIMIT showed the holder blocking 32 of 400 sessions).
        # The sort cost 0.23 s instead of 0.06 s at 80,200 rows.
        queries = [("SELECT w.REQUESTING_ENGINE_TRANSACTION_ID, w.REQUESTING_THREAD_ID, "
                    "w.REQUESTING_ENGINE_LOCK_ID, w.BLOCKING_ENGINE_TRANSACTION_ID, w.BLOCKING_THREAD_ID, "
                    "w.BLOCKING_ENGINE_LOCK_ID, %s, %s FROM performance_schema.data_lock_waits w "
                    "LEFT JOIN performance_schema.data_locks r ON r.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID "
                    "AND r.ENGINE = w.ENGINE "
                    "LEFT JOIN performance_schema.data_locks b ON b.ENGINE_LOCK_ID = w.BLOCKING_ENGINE_LOCK_ID "
                    "AND b.ENGINE = w.ENGINE ORDER BY b.LOCK_STATUS <> 'GRANTED'"
                    % (cols % {"a": "r", "n": LOCK_DATA_CAP + 1}, cols % {"a": "b", "n": LOCK_DATA_CAP + 1}))]
        waiting_ids = None
        source = "performance_schema.data_lock_waits"
    else:
        if r.caps["process_privilege"] is False:
            return
        cols = ("%(a)s.lock_table, %(a)s.lock_index, %(a)s.lock_type, %(a)s.lock_mode, "
                "LEFT(%(a)s.lock_data, %(n)d), LENGTH(%(a)s.lock_data)")
        base = ("SELECT w.requesting_trx_id, w.requested_lock_id, w.blocking_trx_id, w.blocking_lock_id, "
                "%s, %s FROM information_schema.INNODB_LOCK_WAITS w "
                "LEFT JOIN information_schema.INNODB_LOCKS r ON r.lock_id = w.requested_lock_id "
                "LEFT JOIN information_schema.INNODB_LOCKS b ON b.lock_id = w.blocking_lock_id"
                % (cols % {"a": "r", "n": LOCK_DATA_CAP + 1}, cols % {"a": "b", "n": LOCK_DATA_CAP + 1}))
        # MariaDB has no lock status column: a blocking lock is waiting when it
        # is some transaction's requested lock. Read those ids first and take
        # the edges on granted locks in a first pass, the queue edges after.
        # (An unordered LIMIT turned 63 waiting sessions into "root blockers"
        # with 400 waiters on one row, live on 11.4.13; ORDER BY over the
        # join cost 2.7 s there, the two filtered passes 0.13 s.)
        waiting_ids = None
        try:
            ids = [row[0] for row in _q(conn, "SELECT trx_requested_lock_id FROM information_schema.INNODB_TRX "
                                              "WHERE trx_requested_lock_id IS NOT NULL LIMIT %d" % (MAX_LIMIT + 1))
                   if row and row[0]]
            if len(ids) <= MAX_LIMIT:
                waiting_ids = ids
        except ConnectorError:
            waiting_ids = None
        if waiting_ids:
            listed = ", ".join(_lit(i) for i in waiting_ids)
            queries = [base + " WHERE w.blocking_lock_id NOT IN (%s)" % listed,
                       base + " WHERE w.blocking_lock_id IN (%s)" % listed]
        else:
            queries = [base]
        source = "information_schema.INNODB_LOCK_WAITS"
    try:
        rows = []
        for sql in queries:
            need = r.limit + 1 - len(rows)
            if need <= 0:
                break
            rows += _q(conn, sql + " LIMIT %d" % need)
    except ConnectorError as exc:
        if _needs_process(exc):
            r.no_process(exc)
            return
        if _denied(exc):
            r.note("This user cannot read %s: %s%s" % (
                source, _reason(exc), ". Grant it with: GRANT SELECT ON performance_schema.* TO <user>;"
                if source.startswith("performance_schema") else ""))
            return
        raise ValueError("Could not read %s: %s" % (source, _reason(exc)))
    r.caps["lock_waits"] = True
    if len(rows) > r.limit:
        rows = rows[:r.limit]
        r.truncated["waits"] = True
    for row in rows:
        if r.engine == "mysql":
            row = list(row) + [None] * (24 - len(row))
            req = _lock("mysql", row[2], row[0], row[6], row[7], row[8], row[9], row[10], row[11],
                        row[12] or "WAITING", row[13], row[14], _int(row[1]))
            blk = _lock("mysql", row[5], row[3], row[15], row[16], row[17], row[18], row[19], row[20],
                        row[21] or "GRANTED", row[22], row[23], _int(row[4]))
        else:
            row = list(row) + [None] * (16 - len(row))
            schema, table, part = _split_table(row[4])
            req = _lock("mariadb", row[1], row[0], schema, table, part, row[5], row[6], row[7],
                        "WAITING", row[8], row[9])
            schema, table, part = _split_table(row[10])
            # A blocking lock can itself be waiting (queued earlier); MariaDB has no status column.
            blk = _lock("mariadb", row[3], row[2], schema, table, part, row[11], row[12], row[13],
                        None, row[14], row[15])
        r.waits.append({"requesting": req, "blocking": blk})
    if r.engine == "mariadb":
        waiting = set(waiting_ids) if waiting_ids is not None else {w["requesting"]["lock_id"] for w in r.waits}
        for w in r.waits:
            w["blocking"]["status"] = "WAITING" if w["blocking"]["lock_id"] in waiting else "GRANTED"


def _read_locks(conn, r):
    if r.engine == "mysql":
        if not r.ps_on:
            return
        sql = ("SELECT ENGINE_LOCK_ID, ENGINE_TRANSACTION_ID, THREAD_ID, OBJECT_SCHEMA, OBJECT_NAME, "
               "PARTITION_NAME, INDEX_NAME, LOCK_TYPE, LOCK_MODE, LOCK_STATUS, LEFT(LOCK_DATA, %d), "
               "LENGTH(LOCK_DATA) FROM performance_schema.data_locks LIMIT %d"
               % (LOCK_DATA_CAP + 1, r.limit + 1))
    else:
        if r.caps["process_privilege"] is False:
            return
        sql = ("SELECT lock_id, lock_trx_id, NULL, lock_table, lock_index, lock_type, lock_mode, "
               "LEFT(lock_data, %d), LENGTH(lock_data) FROM information_schema.INNODB_LOCKS LIMIT %d"
               % (LOCK_DATA_CAP + 1, r.limit + 1))
    try:
        rows = _q(conn, sql)
    except ConnectorError as exc:
        if _needs_process(exc):
            r.no_process(exc)
            return
        if _denied(exc):
            r.note("This user cannot read the lock list: " + _reason(exc))
            return
        raise ValueError("Could not read the lock list: " + _reason(exc))
    r.caps["row_locks"] = "all" if r.engine == "mysql" else "waits_only"
    if len(rows) > r.limit:
        rows = rows[:r.limit]
        r.truncated["row_locks"] = True
    waiting = {w["requesting"]["lock_id"] for w in r.waits}
    for row in rows:
        if r.engine == "mysql":
            row = list(row) + [None] * (12 - len(row))
            r.locks.append(_lock("mysql", row[0], row[1], row[3], row[4], row[5], row[6], row[7],
                                 row[8], row[9], row[10], row[11], _int(row[2])))
        else:
            row = list(row) + [None] * (9 - len(row))
            schema, table, part = _split_table(row[3])
            status = "WAITING" if row[0] in waiting else "GRANTED"
            r.locks.append(_lock("mariadb", row[0], row[1], schema, table, part, row[4], row[5],
                                 row[6], status, row[7], row[8]))


def _read_mdl(conn, r, waits_only):
    if not r.ps_on:
        if r.engine == "mariadb":
            r.note(MARIADB_PS_OFF_MDL_NOTE)
        return
    try:
        rows = _q(conn, "SELECT 'instrument', ENABLED FROM performance_schema.setup_instruments "
                        "WHERE NAME = 'wait/lock/metadata/sql/mdl' UNION ALL "
                        "SELECT NAME, ENABLED FROM performance_schema.setup_consumers "
                        "WHERE NAME IN ('global_instrumentation', 'thread_instrumentation', "
                        "'events_statements_current')")
    except ConnectorError as exc:
        r.note("Could not read the Performance Schema setup, so metadata locks are not shown: "
               + _reason(exc))
        return
    setup = {row[0]: (row[1] or "").upper() == "YES" for row in rows if len(row) > 1}
    r.caps["mdl_instrument"] = setup.get("instrument", False)
    r.caps["last_statement"] = setup.get("events_statements_current", False) and \
        setup.get("global_instrumentation", True) and setup.get("thread_instrumentation", True)
    if not setup.get("global_instrumentation", True):
        r.note("The global_instrumentation consumer is disabled, so Performance Schema records "
               "no metadata locks or statements. myflames only reads, so it leaves this setting "
               "alone; enable it with: UPDATE performance_schema.setup_consumers SET "
               "ENABLED='YES' WHERE NAME='global_instrumentation';")
        r.caps["mdl_instrument"] = False
        return
    if not r.caps["mdl_instrument"]:
        r.note("The metadata lock instrument (wait/lock/metadata/sql/mdl) is disabled%s, so "
               "metadata locks are not recorded. Enable it at startup with performance-schema-"
               "instrument='wait/lock/metadata/sql/mdl=ON' in my.cnf, or now with: UPDATE "
               "performance_schema.setup_instruments SET ENABLED='YES', TIMED='YES' WHERE "
               "NAME='wait/lock/metadata/sql/mdl'; (locks taken before that stay invisible). "
               "myflames only reads, so it never changes this setting."
               % (" (the MariaDB default)" if r.engine == "mariadb" else ""))
        return
    cols = ("m.OBJECT_TYPE, m.OBJECT_SCHEMA, m.OBJECT_NAME, m.LOCK_TYPE, m.LOCK_DURATION, "
            "m.LOCK_STATUS, m.SOURCE, m.OWNER_THREAD_ID, t.PROCESSLIST_ID")
    base = ("SELECT %s FROM performance_schema.metadata_locks m LEFT JOIN performance_schema.threads t "
            "ON t.THREAD_ID = m.OWNER_THREAD_ID WHERE (t.PROCESSLIST_ID IS NULL OR "
            "t.PROCESSLIST_ID <> CONNECTION_ID()) AND m.LOCK_STATUS IN ('GRANTED', 'PENDING')" % cols)
    if getattr(r, "preflight_target", None):
        schema, table = r.preflight_target
        base += " AND m.OBJECT_TYPE = 'TABLE' AND m.OBJECT_SCHEMA = %s AND m.OBJECT_NAME = %s" % (_lit(schema), _lit(table))
    try:
        rows = _q(conn, base + " ORDER BY m.LOCK_STATUS = 'PENDING' DESC LIMIT %d" % (r.limit + 1))
        if len(rows) > r.limit:
            r.truncated["metadata_locks"] = True
            rows = rows[:r.limit]
            pending = {(row[0], row[1], row[2]) for row in rows if row[5] == "PENDING"}
            if pending:
                # Make sure every granted lock on a contested object is present.
                where = " OR ".join(
                    "(m.OBJECT_TYPE = %s AND m.OBJECT_SCHEMA <=> %s AND m.OBJECT_NAME <=> %s)"
                    % (_lit(o[0]), _lit(o[1]), _lit(o[2])) for o in sorted(pending, key=str)[:200])
                seen = {tuple(row) for row in rows}
                for row in _q(conn, base + " AND (%s) LIMIT %d" % (where, r.limit * 4)):
                    if tuple(row) not in seen:
                        rows.append(row)
    except ConnectorError as exc:
        if _denied(exc):
            r.note("This user cannot read performance_schema.metadata_locks: %s. Grant it with: "
                   "GRANT SELECT ON performance_schema.* TO <user>;" % _reason(exc))
            return
        raise ValueError("Could not read performance_schema.metadata_locks: " + _reason(exc))
    r.caps["metadata_locks"] = True
    for row in rows:
        row = list(row) + [None] * (9 - len(row))
        lock_type = row[3]
        r.mdl_locks.append({
            "object_type": row[0], "schema": row[1], "name": row[2], "lock_type": lock_type,
            "short": MDL_SHORT.get(lock_type, lock_type), "duration": row[4], "status": row[5],
            "source": row[6], "ps_thread_id": _int(row[7]), "connection_id": _int(row[8]),
            "explain": MDL_EXPLAIN.get(lock_type, "A metadata lock."),
        })
    _compute_mdl_waits(r)
    if waits_only:
        contested = {(w["waiting"]["object_type"], w["waiting"]["schema"], w["waiting"]["name"])
                     for w in r.mdl_waits}
        r.mdl_locks = [m for m in r.mdl_locks if (m["object_type"], m["schema"], m["name"]) in contested]


def _lit(value):
    if value is None:
        return "NULL"
    return "CONVERT(X'%s' USING utf8mb4)" % value.encode("utf-8").hex()


def _mdl_blockers(selected, owner, start=0):
    """Each (group, lock) that keeps a pending request of *owner* waiting,
    each group's owners read from *start* (mod their count), so requests
    that share blockers spread a capped budget over different owners."""
    for group, _count in selected:
        owners = group.setdefault("owner_list", list(group["owners"].items()))
        for step in range(len(owners)):
            other_owner, locks = owners[(start + step) % len(owners)]
            if other_owner == owner:
                continue
            for lock in locks:
                yield group, lock


def _compute_mdl_waits(r):
    """Pair each PENDING lock with the locks that keep it waiting.

    A pending request waits for granted locks of other sessions on the same
    object whose type conflicts with it, and also queues behind waiting
    requests that have priority over it (a waiting ALTER TABLE's EXCLUSIVE
    request makes later SELECTs wait even though nothing granted conflicts).
    sys.schema_table_lock_waits instead pairs every pending lock with every
    granted one, which overstates the blockers.
    """
    # Bucket once by object, compatibility class and owner. Counts exclude the
    # request's owner in O(1), even when one owner holds thousands of locks.
    by_object = {}
    r.mdl_groups = []
    r.mdl_edge_total = 0
    for lock in r.mdl_locks:
        key = (lock["object_type"], lock["schema"], lock["name"])
        classes = by_object.setdefault(key, {})
        group = classes.setdefault((lock["status"], lock["lock_type"]),
                                   {"owners": {}, "total": 0})
        group["owners"].setdefault(lock["ps_thread_id"], []).append(lock)
        group["total"] += 1
    for key, classes in by_object.items():
        for (status, kind), group in classes.items():
            group["id"] = len(r.mdl_groups)
            r.mdl_groups.append({"id": group["id"], "object_type": key[0], "schema": key[1],
                                 "name": key[2], "status": status, "lock_type": kind,
                                 "locks": group["total"], "owners": len(group["owners"])})
    pending = []
    for p in r.mdl_locks:
        if p["status"] != "PENDING":
            continue
        key = (p["object_type"], p["schema"], p["name"])
        classes = by_object[key]
        granted_rule, waiting_rule = _mdl_rules(r.engine, key[0], p["lock_type"])
        certain = granted_rule is not None
        owner = p["ps_thread_id"]

        def groups(status, rule, classes=classes, owner=owner):
            return [(g, g["total"] - len(g["owners"].get(owner, ())))
                    for (st, kind), g in classes.items()
                    if st == status and (rule is None or kind in rule)
                    and g["total"] > len(g["owners"].get(owner, ()))]

        selected = groups("GRANTED", granted_rule)
        reason = "granted"
        if not selected and certain:
            selected = groups("PENDING", waiting_rule)
            reason = "queued"
        pending.append({"waiting": p, "selected": selected, "reason": reason, "certain": certain,
                        "total": sum(count for _, count in selected), "blockers": [], "copied": {},
                        "next": _mdl_blockers(selected, owner, len(pending) * 7919)})
    # Share the edge budget round-robin, waits on granted locks first: every
    # pending request gets an edge to what it waits for before any request gets
    # a second one, and a queued edge never points away from a waiting session
    # whose own wait was left out (that session would look like a root
    # blocker). Live on MySQL 8.4.11, 100 holders, 60 LOCK TABLES WRITE and 150
    # readers made 15,000 edges; spending the budget in list order left 43
    # writers as false roots.
    active = [w for w in pending if w["reason"] == "granted"] + [w for w in pending if w["reason"] != "granted"]
    remaining = MAX_MDL_EDGES
    while remaining and active:
        still = []
        for w in active:
            if not remaining:
                break
            item = next(w["next"], None)
            if item is None:
                continue
            group, other = item
            w["blockers"].append({"lock": other, "reason": w["reason"]})
            w["copied"][group["id"]] = w["copied"].get(group["id"], 0) + 1
            remaining -= 1
            still.append(w)
        active = still
    for w in pending:
        summaries = [{"group_id": group["id"], "reason": w["reason"], "total": count,
                      "omitted": count - w["copied"].get(group["id"], 0)} for group, count in w["selected"]]
        total, blockers = w["total"], w["blockers"]
        r.mdl_edge_total += total
        r.mdl_waits.append({"waiting": w["waiting"], "blocked_by": blockers, "certain": w["certain"],
                            "blocker_groups": summaries, "blockers_total": total,
                            "blockers_omitted": total - len(blockers), "truncated": total > len(blockers)})
    if any(w["truncated"] for w in r.mdl_waits):
        r.note("Metadata blocker edges capped at %d; blocker_groups summarize omitted edges. "
               "Totals cover only the collected metadata locks." % MAX_MDL_EDGES)


def _read_sessions(conn, r):
    conn_ids = [t["connection_id"] for t in r.trx]
    ps_ids = []
    for w in r.waits:
        ps_ids += [w["requesting"]["ps_thread_id"], w["blocking"]["ps_thread_id"]]
    ps_ids += [lock["ps_thread_id"] for lock in r.locks]
    conn_ids += [m["connection_id"] for m in r.mdl_locks]
    conn_ids += [m["connection_id"] for m in r.mli_locks]
    conn_ids, ps_ids = _ids(conn_ids)[:r.limit * 2], _ids(ps_ids)[:r.limit * 2]
    if not conn_ids and not ps_ids:
        return
    # performance_schema.threads shows every session without PROCESS on both
    # engines; information_schema.PROCESSLIST is the fallback when P_S is off.
    if r.ps_on:
        where = []
        if conn_ids:
            where.append("PROCESSLIST_ID IN (%s)" % ", ".join(str(i) for i in conn_ids))
        if ps_ids:
            where.append("THREAD_ID IN (%s)" % ", ".join(str(i) for i in ps_ids))
        sql = ("SELECT THREAD_ID, PROCESSLIST_ID, PROCESSLIST_USER, PROCESSLIST_HOST, PROCESSLIST_DB, "
               "PROCESSLIST_COMMAND, PROCESSLIST_TIME, PROCESSLIST_STATE, LEFT(PROCESSLIST_INFO, %d) "
               "FROM performance_schema.threads WHERE %s" % (TEXT_CAP, " OR ".join(where)))
    else:
        if not conn_ids:
            return
        time_expr = "TIME_MS / 1000" if r.engine == "mariadb" else "TIME"
        sql = ("SELECT NULL, ID, USER, HOST, DB, COMMAND, %s, STATE, LEFT(INFO, %d) FROM "
               "information_schema.PROCESSLIST WHERE ID IN (%s)"
               % (time_expr, TEXT_CAP, ", ".join(str(i) for i in conn_ids)))
    try:
        rows = _q(conn, sql)
    except ConnectorError as exc:
        r.note("Could not read the sessions behind the locks: " + _reason(exc))
        return
    for row in rows:
        session = _session_from_row(row)
        if session["ps_thread_id"]:
            r.ps_threads[session["ps_thread_id"]] = session
        if session["connection_id"]:
            r.sessions[session["connection_id"]] = session
    if not r.ps_on and r.caps["process_privilege"] is False:
        r.note("Without PROCESS the process list shows only your own sessions.")


def _session_from_row(row):
    """THREAD_ID, ID, USER, HOST, DB, COMMAND, TIME, STATE, INFO -> a session dict."""
    row = list(row) + [None] * (9 - len(row))
    seconds = None
    try:
        seconds = round(float(row[6]), 3) if row[6] is not None else None
    except ValueError:
        seconds = None
    return {"connection_id": _int(row[1]) or None, "ps_thread_id": _int(row[0]), "user": row[2],
            "host": row[3], "db": row[4], "command": row[5], "time_s": seconds,
            "state": row[7], "info": row[8]}


def _session_sql(engine, ps_on, where, order_limit=""):
    """The sessions query: performance_schema.threads (every session, no PROCESS
    needed) when Performance Schema is on, else information_schema.PROCESSLIST."""
    if ps_on:
        cols = ("THREAD_ID, PROCESSLIST_ID, PROCESSLIST_USER, PROCESSLIST_HOST, PROCESSLIST_DB, "
                "PROCESSLIST_COMMAND, PROCESSLIST_TIME, PROCESSLIST_STATE, LEFT(PROCESSLIST_INFO, %d)"
                % TEXT_CAP)
        return "SELECT %s FROM performance_schema.threads WHERE %s%s" % (
            cols, where.replace("{ID}", "PROCESSLIST_ID").replace("{STATE}", "PROCESSLIST_STATE")
            .replace("{TIME}", "PROCESSLIST_TIME"), order_limit.replace("{TIME}", "PROCESSLIST_TIME"))
    time_expr = "TIME_MS / 1000" if engine == "mariadb" else "TIME"
    return "SELECT NULL, ID, USER, HOST, DB, COMMAND, %s, STATE, LEFT(INFO, %d) FROM " \
           "information_schema.PROCESSLIST WHERE %s%s" % (
               time_expr, TEXT_CAP, where.replace("{ID}", "ID").replace("{STATE}", "STATE")
               .replace("{TIME}", "TIME"), order_limit.replace("{TIME}", "TIME"))


def _resolve_owners(r):
    """Owner connection of each lock: via the transaction (INNODB_TRX), else its thread.

    MariaDB reports trx_id 0 for every transaction that has not written yet,
    so an id shared by several sessions is not used to pick an owner.
    """
    trx_conn, shared = {}, set()
    for t in r.trx:
        if t["trx_id"] in trx_conn and trx_conn[t["trx_id"]] != t["connection_id"]:
            shared.add(t["trx_id"])
        trx_conn[t["trx_id"]] = t["connection_id"]
    for trx_id in shared:
        trx_conn.pop(trx_id, None)
        r.trx_by_id.pop(trx_id, None)
    locks = list(r.locks) + [w[k] for w in r.waits for k in ("requesting", "blocking")]
    for lock in locks:
        cid = trx_conn.get(lock["trx_id"])
        if cid is None and lock["ps_thread_id"] in r.ps_threads:
            cid = r.ps_threads[lock["ps_thread_id"]]["connection_id"]
        lock["connection_id"] = cid
    for m in r.mdl_locks:
        if m["connection_id"] is None and m["ps_thread_id"] in r.ps_threads:
            m["connection_id"] = r.ps_threads[m["ps_thread_id"]]["connection_id"]


def _read_last_statements(conn, r):
    """Last statement of idle sessions with an open transaction (P_S statements_current)."""
    owners = [t["connection_id"] for t in r.trx]
    owners += [w["blocking"]["connection_id"] for w in r.waits]
    owners += [b["lock"]["connection_id"] for w in r.mdl_waits for b in w["blocked_by"]]
    owners += [m["connection_id"] for m in r.mli_locks]
    idle = [cid for cid in _ids(owners)
            if (r.sessions.get(cid) or {}).get("command") == "Sleep"
            and not (r.sessions.get(cid) or {}).get("info")]
    r.last_statements = {}
    if not idle or not r.ps_on:
        if idle and r.engine == "mariadb" and not r.ps_on:
            r.note("An idle transaction's last statement needs Performance Schema with the "
                   "events_statements_current consumer; MariaDB has both off by default.")
        return
    if not r.caps["last_statement"]:
        if r.caps["mdl_instrument"] is not None:
            r.note("An idle transaction's last statement needs the events_statements_current "
                   "and thread_instrumentation consumers%s. Start the server with "
                   "performance-schema-consumer-events-statements-current=ON to see it." % (
                       "; MariaDB disables the first by default" if r.engine == "mariadb" else ""))
        return
    sql = ("SELECT t.PROCESSLIST_ID, LEFT(s.SQL_TEXT, %d), s.EVENT_ID FROM "
           "performance_schema.events_statements_current s JOIN performance_schema.threads t "
           "ON t.THREAD_ID = s.THREAD_ID WHERE t.PROCESSLIST_ID IN (%s) AND "
           "(s.NESTING_EVENT_TYPE IS NULL OR s.NESTING_EVENT_TYPE = 'TRANSACTION') ORDER BY s.EVENT_ID"
           % (TEXT_CAP, ", ".join(str(i) for i in _ids(idle)[:r.limit])))
    try:
        rows = _q(conn, sql)
    except ConnectorError:
        r.caps["last_statement"] = False
        return
    for row in rows:
        cid = _int(row[0])
        if cid and row[1]:
            r.last_statements[cid] = row[1]


# ---- DDL waiting for a metadata lock -------------------------------------------

#: Besides "Waiting for <namespace> metadata lock": MySQL's global read, backup
#: and commit lock waits (M/sql/mdl.cc:117-126). MariaDB has only "Waiting for
#: backup lock" (Ma/sql/mdl.cc:103), which FLUSH TABLES WITH READ LOCK also uses.
MDL_WAIT_STATES = ("Waiting for global read lock", "Waiting for backup lock", "Waiting for commit lock")


def is_mdl_wait_state(state):
    state = state or ""
    return (state.startswith("Waiting for ") and state.endswith("metadata lock")) or state in MDL_WAIT_STATES


_LEADING = re.compile(r"(?:\s+|/\*.*?\*/|--[^\n]*(?:\n|\Z)|#[^\n]*(?:\n|\Z))*", re.S)
_ID = r"(?:`(?:[^`]|``)+`|[\w$]+)"
_NAME = r"(%s)(?:\s*\.\s*(%s))?" % (_ID, _ID)
_DDL = tuple((kind, re.compile(pattern, re.I)) for kind, pattern in (
    ("ALTER TABLE", r"ALTER\s+(?:ONLINE\s+)?(?:IGNORE\s+)?TABLE\s+(?:IF\s+EXISTS\s+)?" + _NAME),
    ("CREATE INDEX", r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:ONLINE\s+|OFFLINE\s+)?(?:UNIQUE\s+|FULLTEXT\s+|"
                     r"SPATIAL\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?" + _ID + r"(?:\s+USING\s+\w+)?\s+ON\s+"
                     + _NAME),
    ("DROP INDEX", r"DROP\s+(?:ONLINE\s+|OFFLINE\s+)?INDEX\s+(?:IF\s+EXISTS\s+)?" + _ID + r"\s+ON\s+" + _NAME),
    ("DROP TABLE", r"DROP\s+(?:TEMPORARY\s+)?TABLES?\s+(?:IF\s+EXISTS\s+)?" + _NAME),
    ("RENAME TABLE", r"RENAME\s+TABLES?\s+" + _NAME),
    ("TRUNCATE TABLE", r"TRUNCATE\s+(?:TABLE\s+)?" + _NAME),
    ("OPTIMIZE TABLE", r"OPTIMIZE\s+(?:NO_WRITE_TO_BINLOG\s+|LOCAL\s+)?TABLES?\s+" + _NAME),
    ("REPAIR TABLE", r"REPAIR\s+(?:NO_WRITE_TO_BINLOG\s+|LOCAL\s+)?TABLES?\s+" + _NAME),
    ("CREATE TRIGGER", r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*\S+\s+)?TRIGGER\s+(?:IF\s+NOT\s+"
                       r"EXISTS\s+)?" + _ID + r"(?:\s*\.\s*" + _ID + r")?\s+(?:BEFORE|AFTER)\s+\w+"
                       r"(?:\s+OR\s+\w+)*\s+ON\s+" + _NAME),
))
_DDL_ANY = re.compile(r"(ALTER|CREATE|DROP|RENAME|TRUNCATE|OPTIMIZE|REPAIR)\s+(?:(?:OR\s+REPLACE|ONLINE|"
                      r"OFFLINE|TEMPORARY|UNIQUE|FULLTEXT|SPATIAL|IGNORE|DEFINER\s*=\s*\S+)\s+)*([A-Za-z]+)", re.I)


def _unquote(name):
    if name and name.startswith("`") and name.endswith("`"):
        return name[1:-1].replace("``", "`")
    return name


def ddl_statement(sql):
    """``(kind, schema or None, table or None)`` when *sql* is DDL, else None.

    The table is the first one the statement names (ALTER TABLE t, CREATE
    INDEX i ON t, DROP/RENAME/TRUNCATE/OPTIMIZE TABLE t); the schema is None
    when the statement does not qualify it.
    """
    if not sql:
        return None
    text = sql[_LEADING.match(sql).end():]
    for kind, pattern in _DDL:
        match = pattern.match(text)
        if match:
            first, second = _unquote(match.group(1)), _unquote(match.group(2))
            return (kind, first, second) if second else (kind, None, first)
    match = _DDL_ANY.match(text)
    if match:
        return "%s %s" % (match.group(1).upper(), match.group(2).upper()), None, None
    return None


def _read_mdl_waiters(conn, r):
    """Sessions whose state is a metadata lock wait: DDL statements and what queues behind them.

    This works without the metadata lock instrument (the MariaDB default), so
    a waiting ALTER TABLE is found on every engine. Without PROCESS and with
    Performance Schema off, the process list shows only this user's sessions.
    """
    states = ", ".join("'%s'" % s for s in MDL_WAIT_STATES)
    where = ("({STATE} LIKE 'Waiting for %%metadata lock' OR {STATE} IN (%s)) AND {ID} <> CONNECTION_ID()"
             % states)
    sql = _session_sql(r.engine, r.ps_on, where, " ORDER BY {TIME} DESC LIMIT %d" % (r.limit + 1))
    try:
        rows = _q(conn, sql)
    except ConnectorError as exc:
        r.note("Could not read the sessions waiting for a metadata lock: " + _reason(exc))
        return
    for row in rows[:r.limit]:
        session = _session_from_row(row)
        if session["connection_id"] and is_mdl_wait_state(session["state"]):
            r.mdl_waiters.append(session)


def _read_metadata_lock_info(conn, r):
    """MariaDB's optional METADATA_LOCK_INFO plugin: granted table metadata locks.

    Read only when Performance Schema does not record metadata locks and a DDL
    statement is waiting. A server without the plugin answers "Unknown table"
    (1109), which is the normal case and needs no note.
    """
    if r.engine != "mariadb" or r.caps["metadata_locks"]:
        return
    if not any(ddl_statement(w["info"]) for w in r.mdl_waiters):
        return
    sql = ("SELECT THREAD_ID, LOCK_MODE, LOCK_TYPE, TABLE_SCHEMA, TABLE_NAME FROM "
           "information_schema.METADATA_LOCK_INFO WHERE LOCK_TYPE = 'Table metadata lock' "
           "AND THREAD_ID <> CONNECTION_ID() LIMIT %d" % (r.limit * 4))
    try:
        rows = _q(conn, sql)
    except ConnectorError as exc:
        r.caps["metadata_lock_info"] = False
        if "1109" not in str(exc) and "Unknown table" not in str(exc):
            r.note("Could not read information_schema.METADATA_LOCK_INFO: " + _reason(exc))
        return
    r.caps["metadata_lock_info"] = True
    for row in rows:
        row = list(row) + [None] * (5 - len(row))
        mode = row[1] or ""
        lock_type = mode[4:] if mode.startswith("MDL_") else mode
        r.mli_locks.append({"connection_id": _int(row[0]), "lock_type": lock_type,
                            "short": MDL_SHORT.get(lock_type, lock_type), "duration": None,
                            "schema": row[3], "name": row[4],
                            "explain": MDL_EXPLAIN.get(lock_type, "A metadata lock.")})


# ---- prepared XA transactions (MySQL) ----------------------------------------------

#: One part of an xid as written in XA statements: a quoted string, a hex
#: literal, or the integer format id.
_XID_PART = r"(?:'(?:[^'\\\n]|''|\\[^\n])*'|[Xx]'[0-9A-Fa-f]*'|0x[0-9A-Fa-f]+|\d+)"
_XA_PREPARE = re.compile(r"XA\s+PREPARE\s+(%s(?:\s*,\s*%s){0,2})\s*;?\s*$" % (_XID_PART, _XID_PART), re.I)
_XA_PREPARE_ANY = re.compile(r"XA\s+PREPARE\b", re.I)


def xa_prepared_from(statement):
    """``{"xid", "commit", "rollback"}`` when *statement* is XA PREPARE, else None.

    The xid is copied from the statement only when it is a well-formed xid
    (quoted strings, hex literals, a numeric format id), so the commands shown
    can be pasted as they are; otherwise ``xid`` is None.
    """
    if not statement:
        return None
    text = statement[_LEADING.match(statement).end():]
    if not _XA_PREPARE_ANY.match(text):
        return None
    match = _XA_PREPARE.match(text)
    xid = match.group(1).strip() if match else None
    return {"xid": xid, "commit": "XA COMMIT %s" % xid if xid else None,
            "rollback": "XA ROLLBACK %s" % xid if xid else None}


def _read_xa_states(conn, r):
    """Idle MySQL sessions whose last statement was XA PREPARE.

    Their metadata locks belong to the prepared XA transaction, which survives
    the end of the session (MySQL keeps them in MDL_context_backup_manager), so
    the answer is XA COMMIT or XA ROLLBACK, not a KILL. The last statement
    comes from events_statements_current (on by default on MySQL). MariaDB
    releases a detached XA transaction's metadata locks, so it needs no check.
    """
    if r.engine != "mysql":
        return
    for cid, text in getattr(r, "last_statements", {}).items():
        xa = xa_prepared_from(text)
        if xa:
            r.xa[cid] = xa


XA_NOTE = ("On MySQL 8.0.29 and later, the session that ran XA PREPARE stays listed as the owner of that "
           "XA transaction's metadata locks, and ending the session does not release them. End the "
           "transaction with XA COMMIT or XA ROLLBACK.")


# ---- may this account end other sessions? ------------------------------------------

def parse_kill_privilege(engine, ver, grants, user=None, account=None):
    """Whether this account may KILL other users' sessions, from SHOW GRANTS lines.

    A hint for showing or hiding the Kill action; the server has the last
    word, and its refusal is reported as such. Only global (ON *.*) grants
    count. Everyone may end sessions with their own login user name.
    """
    from .dbops import MARIADB_SUPER_SPLIT_END, global_privileges
    privs = global_privileges(grants)
    every = bool(privs & {"ALL", "ALL PRIVILEGES"})
    if engine == "mariadb":
        order = ("CONNECTION ADMIN",) + (("SUPER",) if ver < MARIADB_SUPER_SPLIT_END else ())
        system_user = None
    else:
        order = ("CONNECTION_ADMIN", "SUPER")
        system_user = True if "SYSTEM_USER" in privs else (None if every else False)
    via = next((p for p in order if p in privs), None) or ("ALL PRIVILEGES" if every else None)
    return {"checked": True, "user": user, "account": account, "can_kill_others": via is not None,
            "via": via, "system_user": system_user,
            "super_only": engine == "mariadb" and via is None and "SUPER" in privs}


def read_kill_privilege(conn, engine, ver):
    """Read SHOW GRANTS (with the roles active at login) and the login user name."""
    try:
        who = _q(conn, "SELECT USER(), CURRENT_USER() LIMIT 1")
        grants = [row[0] for row in _q(conn, "SHOW GRANTS") if row and row[0]]
    except ConnectorError as exc:
        return {"checked": False, "user": None, "account": None, "can_kill_others": None, "via": None,
                "system_user": None, "super_only": False, "error": _reason(exc)}
    login = (who[0][0] or "") if who and who[0] else ""
    user = login.rsplit("@", 1)[0] if "@" in login else (login or None)
    account = who[0][1] if who and len(who[0]) > 1 else None
    return parse_kill_privilege(engine, ver, grants, user, account)


def kill_allowed(privilege, user):
    """True when this account may end a session of *user*; None when unknown."""
    if not privilege or not privilege.get("checked"):
        return None
    if privilege.get("can_kill_others"):
        return True
    if user and privilege.get("user") is not None and user == privilege["user"]:
        return True
    return False if user else None


def _privilege_notes(engine, privilege):
    p = privilege or {}
    notes = []
    if not p.get("checked"):
        if p.get("error"):
            notes.append("Could not read this account's grants (%s), so Kill blocker is hidden. Run "
                         "KILL n yourself if you may." % p["error"])
        return notes
    if p.get("can_kill_others"):
        if engine == "mysql" and p.get("system_user") is False:
            notes.append("This account can end other users' sessions (%s) but lacks SYSTEM_USER, so "
                         "MySQL refuses to end a session of an account that has SYSTEM_USER (root has it)."
                         % p["via"])
        return notes
    need = ("CONNECTION ADMIN (SUPER also works before MariaDB 11.0)" if engine == "mariadb"
            else "CONNECTION_ADMIN or SUPER")
    text = ("This account (%s) can end only sessions of its own user, %s. Ending another user's session "
            "needs %s. myflames read SHOW GRANTS, which includes the roles active at login."
            % (p.get("account") or "?", p.get("user") or "?", need))
    if p.get("super_only"):
        text += " On MariaDB 11.0 and later, SUPER granted on 11.0 or later no longer covers it."
    notes.append(text)
    return notes


# ---- DDL blockers ------------------------------------------------------------------------

def _same_name(a, b):
    return a is not None and b is not None and (a == b or a.casefold() == b.casefold())


def _mentions(sql, table):
    if not sql or not table:
        return False
    return re.search(r"(?<![\w$])`?%s`?(?![\w$])" % re.escape(table), sql, re.I) is not None


def _view(r, cid, last, trx_by_conn, holds_trx_lock=False):
    """One session as the DDL section and the Kill dialog show it."""
    session = r.sessions.get(cid) or {}
    trx = trx_by_conn.get(cid) or {}
    info = session.get("info") or trx.get("query")
    command = session.get("command")
    return {
        "connection_id": cid, "user": session.get("user"), "host": session.get("host"),
        "db": session.get("db"), "command": command, "state": session.get("state"),
        "time_s": session.get("time_s"), "statement": info,
        "last_statement": last.get(cid) if not info else None,
        "ps_thread_id": session.get("ps_thread_id"),
        "trx_id": trx.get("trx_id"), "trx_state": trx.get("state"), "trx_started": trx.get("started"),
        "trx_age_s": trx.get("age_s"), "rows_locked": trx.get("rows_locked"),
        "rows_modified": trx.get("rows_modified"),
        "idle_in_transaction": command == "Sleep" and not info and (bool(trx) or holds_trx_lock),
        "running": command not in (None, "Sleep", "Killed") and bool(info),
        "kill_allowed": kill_allowed(r.kill_privilege, session.get("user")),
        "xa_prepared": r.xa.get(cid),
    }


def _upstream_roots(nodes, key):
    """Root blockers above *key* in the blocking tree (empty when it waits for nobody)."""
    roots, stack, seen = [], list((nodes.get(key) or {}).get("_parents", ())), set()
    while stack:
        k = stack.pop()
        if k in seen or k not in nodes:
            continue
        seen.add(k)
        parents = nodes[k]["_parents"]
        if not parents or nodes[k]["root"]:
            roots.append(k)
        stack.extend(parents)
    return roots


def _ddl_waits(r, nodes, last):
    trx_by_conn = {t["connection_id"]: t for t in r.trx if t["connection_id"]}
    pending = {}
    for w in r.mdl_waits:
        pending.setdefault(w["waiting"]["connection_id"], []).append(w)
    candidates = {}
    for s in r.mdl_waiters:
        if ddl_statement(s["info"]):
            candidates[s["connection_id"]] = s
    for cid in pending:
        info = (r.sessions.get(cid) or {}).get("info")
        if cid and cid not in candidates and ddl_statement(info):
            candidates[cid] = r.sessions[cid]
    waiter_ids = {s["connection_id"] for s in r.mdl_waiters}
    waiter_ids |= {cid for cid in pending if cid}
    out = []
    for cid in sorted(candidates, key=lambda c: (-(candidates[c].get("time_s") or 0), c)):
        session = candidates[cid]
        kind, schema, table = ddl_statement(session["info"])
        entry = {
            "connection_id": cid, "user": session.get("user"), "host": session.get("host"),
            "db": session.get("db"), "statement": session.get("info"), "kind": kind,
            "state": session.get("state"), "wait_s": session.get("time_s"),
            "object_type": "TABLE" if table else None, "schema": schema or (session.get("db") if table else None),
            "table": table, "statement_schema": schema or (session.get("db") if table else None),
            "statement_table": table, "requested": None, "requested_short": None,
            "precision": "likely", "blockers": [], "blockers_truncated": False, "queued": [],
        }
        exact = [w for w in pending.get(cid, []) if w["blocked_by"]] or pending.get(cid, [])
        if exact:
            _ddl_exact(r, nodes, last, trx_by_conn, entry, exact)
        elif r.mli_locks and table:
            _ddl_granted(r, last, trx_by_conn, entry)
        if not entry["blockers"] and entry["precision"] != "exact":
            _ddl_likely(r, last, trx_by_conn, entry, waiter_ids)
        _ddl_queued(r, entry, pending, len(candidates))
        out.append(entry)
    return out


def _ddl_exact(r, nodes, last, trx_by_conn, entry, waits):
    entry["precision"] = "exact"
    w = next((w for w in waits if w["waiting"]["object_type"] == "TABLE"), waits[0])
    entry["blockers_total"] = w.get("blockers_total", len(w["blocked_by"]))
    entry["blockers_truncated"] = w.get("truncated", False)
    p = w["waiting"]
    entry.update({"object_type": p["object_type"], "schema": p["schema"], "table": p["name"],
                  "requested": p["lock_type"], "requested_short": p["short"]})
    seen, threads = {}, {}
    for b in w["blocked_by"]:
        lock = b["lock"]
        bcid = lock["connection_id"]
        if not bcid:
            # No client connection owns it: a background thread or (MySQL) a
            # prepared XA transaction no longer attached to a session.
            held = {"lock_type": lock["lock_type"], "short": lock["short"],
                    "duration": lock["duration"], "status": lock["status"]}
            if lock["ps_thread_id"] in threads:
                threads[lock["ps_thread_id"]]["locks"].append(held)
                continue
            threads[lock["ps_thread_id"]] = {
                "connection_id": None, "ps_thread_id": lock["ps_thread_id"], "user": None, "host": None,
                "db": None, "command": None, "state": None, "time_s": None, "statement": None,
                "last_statement": None, "trx_id": None, "trx_state": None, "trx_started": None,
                "trx_age_s": None, "rows_locked": None, "rows_modified": None,
                "idle_in_transaction": False, "running": False, "kill_allowed": None, "xa_prepared": None,
                "reason": "holds" if b["reason"] == "granted" else "queued", "certain": w["certain"],
                "locks": [held], "started_before_ddl": None, "via": None, "waits_for": []}
            entry["blockers"].append(threads[lock["ps_thread_id"]])
            continue
        if bcid in seen:
            seen[bcid]["locks"].append({"lock_type": lock["lock_type"], "short": lock["short"],
                                        "duration": lock["duration"], "status": lock["status"]})
            continue
        view = _view(r, bcid, last, trx_by_conn, lock["duration"] == "TRANSACTION")
        view.update({"reason": "holds" if b["reason"] == "granted" else "queued", "certain": w["certain"],
                     "locks": [{"lock_type": lock["lock_type"], "short": lock["short"],
                                "duration": lock["duration"], "status": lock["status"]}],
                     "started_before_ddl": None, "via": None,
                     "waits_for": [nodes[k]["connection_id"] for k in sorted(
                         (nodes.get("c%d" % bcid) or {}).get("_parents", ())) if nodes[k]["connection_id"]]})
        seen[bcid] = view
        entry["blockers"].append(view)
    # A blocker that itself waits: its root blockers are part of the answer.
    for view in list(entry["blockers"]):
        if not view["connection_id"]:
            continue
        for key in _upstream_roots(nodes, "c%d" % view["connection_id"]):
            root_cid = nodes[key]["connection_id"]
            if not root_cid or root_cid in seen or root_cid == entry["connection_id"]:
                continue
            up = _view(r, root_cid, last, trx_by_conn, nodes[key]["_in_trx"])
            up.update({"reason": "chain", "certain": True, "locks": [], "started_before_ddl": None,
                       "via": view["connection_id"], "waits_for": []})
            seen[root_cid] = up
            entry["blockers"].append(up)
    for view in entry["blockers"]:
        view["culprit"] = view["idle_in_transaction"]


def _ddl_granted(r, last, trx_by_conn, entry):
    """METADATA_LOCK_INFO: the sessions holding any lock on the table the DDL names."""
    seen = {}
    for m in r.mli_locks:
        mcid = m["connection_id"]
        if not mcid or mcid == entry["connection_id"]:
            continue
        if not (_same_name(m["name"], entry["table"]) and _same_name(m["schema"], entry["schema"])):
            continue
        lock = {"lock_type": m["lock_type"], "short": m["short"], "duration": None, "status": "GRANTED"}
        if mcid in seen:
            seen[mcid]["locks"].append(lock)
            continue
        view = _view(r, mcid, last, trx_by_conn)
        view.update({"reason": "holds", "certain": False, "locks": [lock], "started_before_ddl": None,
                     "via": None, "waits_for": [], "culprit": view["idle_in_transaction"]})
        seen[mcid] = view
        entry["blockers"].append(view)
    if entry["blockers"]:
        entry["precision"] = "granted_locks"
        entry["requested"], entry["requested_short"] = None, None


DDL_LIKELY_MAX = 20


def _ddl_likely(r, last, trx_by_conn, entry, waiter_ids):
    """Without metadata lock data: open transactions, idle ones and older ones first."""
    entry["precision"] = "likely"
    wait = entry["wait_s"]
    found = []
    for t in r.trx:
        tcid = t["connection_id"]
        if not tcid or tcid == entry["connection_id"] or tcid in waiter_ids:
            continue
        view = _view(r, tcid, last, trx_by_conn)
        before = None if t["age_s"] is None or wait is None else t["age_s"] >= wait - 1
        view.update({"reason": "open_transaction", "certain": False, "locks": [],
                     "started_before_ddl": before, "via": None, "waits_for": [],
                     "culprit": view["idle_in_transaction"] and before is not False})
        found.append(view)
    found.sort(key=lambda v: (not v["culprit"], not v["idle_in_transaction"],
                              v["started_before_ddl"] is not True, -(v["trx_age_s"] or 0)))
    entry["blockers"] = found[:DDL_LIKELY_MAX]
    entry["blockers_truncated"] = len(found) > DDL_LIKELY_MAX


def _ddl_queued(r, entry, pending, ddl_count):
    """Sessions waiting because the DDL waits: exact from metadata_locks, else by state and statement."""
    cid = entry["connection_id"]
    queued = []
    if entry["precision"] == "exact":
        for w in r.mdl_waits:
            wcid = w["waiting"]["connection_id"]
            if not wcid or wcid == cid:
                continue
            if any(b["lock"]["connection_id"] == cid for b in w["blocked_by"]):
                session = r.sessions.get(wcid) or {}
                queued.append({"connection_id": wcid, "user": session.get("user"),
                               "host": session.get("host"), "statement": session.get("info"),
                               "wait_s": session.get("time_s"), "lock_type": w["waiting"]["lock_type"],
                               "short": w["waiting"]["short"], "certain": True})
    else:
        for s in r.mdl_waiters:
            scid = s["connection_id"]
            if scid == cid or ddl_statement(s["info"]):
                continue
            if entry["wait_s"] is not None and s["time_s"] is not None and s["time_s"] > entry["wait_s"] + 1:
                continue  # it waited before the DDL started, so it is not queued behind it
            if not (_mentions(s["info"], entry["table"]) or (ddl_count == 1 and not entry["table"])):
                continue
            queued.append({"connection_id": scid, "user": s.get("user"), "host": s.get("host"),
                           "statement": s.get("info"), "wait_s": s.get("time_s"), "lock_type": None,
                           "short": None, "certain": False})
    queued.sort(key=lambda q: (-(q["wait_s"] or 0), q["connection_id"]))
    entry["queued"] = queued


DDL_QUEUE_NOTE = ("While a DDL statement such as ALTER TABLE waits for its exclusive metadata lock, new "
                  "reads and writes of that table queue behind it, even when the lock the blocker holds "
                  "(usually SHARED_READ or SHARED_WRITE from an open transaction) would let them through. "
                  "One open transaction and one waiting DDL statement can stop every read and write of the "
                  "table until the transaction ends or the DDL gives up after lock_wait_timeout (by default "
                  "one year on MySQL, one day on MariaDB). Stopping the DDL statement (KILL QUERY on its "
                  "connection) also releases the queue, but an ALTER TABLE stopped at its final step loses "
                  "the work it has done and must run again.")


NON_INNODB = ("Transactions that used only non-InnoDB tables (MyISAM, MEMORY, Aria) hold metadata locks "
              "too but are not in INNODB_TRX, so they are missing from this list.")


def _ddl_likely_note(r):
    how = ("add performance_schema=ON and performance-schema-instrument='wait/lock/metadata/sql/mdl=ON' "
           "under [mysqld] in my.cnf and restart")
    if r.engine == "mariadb":
        return ("MariaDB is not recording metadata locks here (Performance Schema and its "
                "wait/lock/metadata/sql/mdl instrument are off by default), so myflames cannot see "
                "exactly which session holds the table a DDL statement waits for. The likely blockers "
                "are open transactions, idle ones and ones that started before the DDL first: a "
                "transaction keeps the metadata locks of every table it used until it commits or rolls "
                "back. A transaction that started after a long ALTER TABLE began can also block its last "
                "step. %s For exact blockers, %s, or install the METADATA_LOCK_INFO plugin (INSTALL SONAME "
                "'metadata_lock_info'), which lists granted metadata locks. myflames changes neither."
                % (NON_INNODB, how))
    return ("Performance Schema is not recording metadata locks here (it is off, or the "
            "wait/lock/metadata/sql/mdl instrument is disabled; MySQL enables both by default), so "
            "myflames cannot see exactly which session holds the table a DDL statement waits for. The "
            "likely blockers are open transactions, idle ones and ones that started before the DDL "
            "first: a transaction keeps the metadata locks of every table it used until it commits or "
            "rolls back. An ALTER TABLE that adds a foreign key or renames the table also locks the "
            "parent tables, so the blocker may be using a related table. %s For exact blockers, %s. "
            "myflames changes neither." % (NON_INNODB, how))


MLI_NOTE = ("information_schema.METADATA_LOCK_INFO lists granted metadata locks but not waits, so the "
            "blockers shown for a DDL statement are the sessions holding a lock on the table it names.")


# ---- deadlock ----------------------------------------------------------------

_SECTION = re.compile(r"\n-{3,}\n([A-Z][A-Z /]+)\n-{3,}\n")
_TRX_HEAD = re.compile(r"^TRANSACTION (\d+|\(0x[0-9a-fA-F]+\)), ACTIVE (?:\(PREPARED\) )?(\d+) sec\s*(.*)$")
_IP = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-fA-F]*:[0-9a-fA-F:.]*)$")
_THREAD = re.compile(r"^(?:MySQL|MariaDB) thread id (\d+), OS thread handle \d+, query id (\d+)\s*(.*)$")
_LOCK_HEAD = re.compile(
    r"^(RECORD LOCKS space id (\d+) page no (\d+) n bits \d+ index (.+?) of table|TABLE LOCK table) "
    r"(.+?) trx id (\d+) (lock_mode|lock mode) (\S+)(.*)$")
# " 0: len 4; hex 80000001; asc     ;;", "...; (total 100 bytes);", " 1: SQL NULL;",
# " 1: SQL NULL, size 4 ;" (REDUNDANT rows), " 2: SQL DEFAULT;" (instant ADD COLUMN).
_FIELD = re.compile(r"^\s*(\d+):(?: len \d+; hex ([0-9a-f]+); asc (.*?);(?:;| \(total)| SQL (?:NULL|DEFAULT))")
_HEAP = re.compile(r"^Record lock, heap no (\d+)")


def _status_lock(line, engine):
    match = _LOCK_HEAD.match(line.strip())
    if not match:
        return None
    record = match.group(1).startswith("RECORD")
    schema, table, part = _split_table(match.group(5))
    rest = match.group(9) or ""
    flags = []
    if "locks gap before rec" in rest:
        flags.append("GAP")
    if "locks rec but not gap" in rest:
        flags.append("REC_NOT_GAP")
    if "insert intention" in rest:
        flags.append("INSERT_INTENTION")
    base = match.group(8).replace("AUTO-INC", "AUTO_INC")
    if record and "GAP" not in flags and "INSERT_INTENTION" in flags:
        flags.insert(0, "GAP")
    mode = ",".join([base] + flags)
    # The status text always distinguishes gap, record-only and insert intention,
    # also on MariaDB, so explain it with MySQL's richer vocabulary.
    label, why = explain_lock_mode("mysql", "RECORD" if record else "TABLE", mode)
    return {"lock_type": "RECORD" if record else "TABLE", "schema": schema, "table": table,
            "partition": part, "index": match.group(4) if record else None,
            "space_id": _int(match.group(2)), "page_no": _int(match.group(3)),
            "trx_id": match.group(6), "mode": mode, "waiting": " waiting" in rest,
            "mode_label": label, "explain": why, "records": []}


def parse_latest_deadlock(status, engine="mysql"):
    """Parse the LATEST DETECTED DEADLOCK section of SHOW ENGINE INNODB STATUS.

    Returns None when the server has not recorded a deadlock since it started.
    """
    if not status:
        return None
    # The section header is the title between two dashed lines, so the phrase
    # inside a statement elsewhere in the output does not count.
    head = re.search(r"(?:^|\n)-{3,}\nLATEST DETECTED DEADLOCK\n-{3,}\n", status)
    if not head:
        return None
    body = status[head.end():]
    nxt = _SECTION.search("\n" + body)
    if nxt:
        body = body[:max(nxt.start() - 1, 0)]
    raw, raw_cut = _cap(body.rstrip("\n"), RAW_DEADLOCK_CAP)
    when = re.match(r"\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", body)
    result = {"detected_at": when.group(1) if when else None, "transactions": [],
              "victim": None, "victim_trx_id": None, "raw": raw, "raw_truncated": raw_cut}
    current = None
    section = None
    lock = None
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        title = re.match(r"^\*\*\* \((\d+)\) (TRANSACTION|HOLDS THE LOCK\(S\)|WAITING FOR THIS LOCK TO BE GRANTED):", line)
        if title:
            n = int(title.group(1))
            if title.group(2) == "TRANSACTION":
                current = {"n": n, "trx_id": None, "active_s": None, "operation": None,
                           "connection_id": None, "query_id": None, "host": None, "user": None,
                           "state": None, "tables_in_use": None, "tables_locked": None,
                           "lock_structs": None, "row_locks": None, "lock_wait": False,
                           "statement": None, "holds": [], "waiting_for": None,
                           "conflicts_with": [], "victim": False}
                result["transactions"].append(current)
                section = "trx"
                i = _parse_trx_block(lines, i + 1, current)
                continue
            current = next((t for t in result["transactions"] if t["n"] == n), current)
            section = "holds" if title.group(2).startswith("HOLDS") else "waiting"
            i += 1
            continue
        if line.startswith("*** WAITING FOR THIS LOCK TO BE GRANTED:"):
            section = "waiting"
            i += 1
            continue
        if line.startswith("*** CONFLICTING WITH:"):
            section = "conflicts"
            i += 1
            continue
        victim = re.match(r"^\*\*\* WE ROLL BACK TRANSACTION \((\d+)\)", line)
        if victim:
            result["victim"] = int(victim.group(1))
            i += 1
            continue
        parsed = _status_lock(line, engine) if current is not None else None
        if parsed:
            lock = parsed
            if section == "holds":
                current["holds"].append(lock)
            elif section == "waiting":
                current["waiting_for"] = lock
            elif section == "conflicts":
                current["conflicts_with"].append(lock)
            i += 1
            continue
        heap = _HEAP.match(line)
        if heap and lock is not None and len(lock["records"]) < DEADLOCK_RECORDS_CAP:
            record = {"heap_no": int(heap.group(1)), "fields": []}
            lock["records"].append(record)
            j = i + 1
            while j < len(lines) and _FIELD.match(lines[j]):
                field = _FIELD.match(lines[j])
                if len(record["fields"]) < 3:
                    record["fields"].append({"hex": field.group(2), "asc": field.group(3)}
                                            if field.group(2) is not None else {"hex": None, "asc": None})
                j += 1
            first = record["fields"][0] if record["fields"] else {}
            record["supremum"] = first.get("hex") == "73757072656d756d"
            record["as_signed_int"] = None if record["supremum"] else _signed_int(first.get("hex"))
            i = j
            continue
        i += 1
    by_trx = {trx["trx_id"]: trx for trx in result["transactions"]}
    for trx in result["transactions"]:
        trx["victim"] = trx["n"] == result["victim"]
        if trx["victim"]:
            result["victim_trx_id"] = trx["trx_id"]
        # MariaDB prints the locks in the way under "CONFLICTING WITH"; the
        # ones owned by another transaction in the cycle are what it holds.
        for lock in trx["conflicts_with"]:
            owner = by_trx.get(lock["trx_id"])
            if owner is not None and owner is not trx and not lock["waiting"] \
                    and lock not in owner["holds"]:
                owner["holds"].append(lock)
    return result


def _signed_int(hex_text):
    """InnoDB stores a signed integer with its sign bit flipped. If the first
    key column is a signed INT type, this is its value; for other types it is
    meaningless, so callers label it as a guess."""
    if not hex_text or len(hex_text) not in (2, 4, 6, 8, 16):
        return None
    return int(hex_text, 16) - (1 << (len(hex_text) * 4 - 1))


def _parse_trx_block(lines, i, trx):
    """Fill *trx* from the TRANSACTION block; return the index after it."""
    statement = []
    after_thread = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("*** "):
            break
        head = _TRX_HEAD.match(line)
        thread = _THREAD.match(line)
        if head:
            trx["trx_id"], trx["active_s"] = head.group(1), int(head.group(2))
            trx["operation"] = head.group(3).strip() or None
        elif line.startswith("mysql tables in use"):
            nums = re.findall(r"\d+", line)
            if len(nums) >= 2:
                trx["tables_in_use"], trx["tables_locked"] = int(nums[0]), int(nums[1])
        elif "lock struct(s)" in line and not after_thread:
            trx["lock_wait"] = line.startswith("LOCK WAIT")
            structs = re.search(r"(\d+) lock struct\(s\)", line)
            rows = re.search(r"(\d+) row lock\(s\)", line)
            trx["lock_structs"] = int(structs.group(1)) if structs else None
            trx["row_locks"] = int(rows.group(1)) if rows else None
        elif thread:
            trx["connection_id"], trx["query_id"] = int(thread.group(1)), int(thread.group(2))
            # host, then the client IP for TCP connections, then user, then state
            # (M/sql/sql_thd_api.cc:516-536, Ma/sql/sql_show.cc:11497-11520).
            parts = thread.group(3).split(" ")
            at = 2 if len(parts) > 2 and _IP.match(parts[1]) else 1
            trx["host"] = parts[0] or None
            trx["user"] = parts[at] if len(parts) > at else None
            trx["state"] = " ".join(parts[at + 1:]) or None
            after_thread = True
        elif after_thread:
            statement.append(line)
        i += 1
    text = "\n".join(statement).strip()
    trx["statement"], _ = _cap(text, TEXT_CAP) if text else (None, False)
    return i


def _read_deadlock(conn, r):
    if r.caps["process_privilege"] is False:
        return None
    try:
        rows = _q(conn, "SHOW ENGINE INNODB STATUS")
    except ConnectorError as exc:
        if _needs_process(exc):
            r.no_process(exc)
            return None
        r.note("Could not read SHOW ENGINE INNODB STATUS: " + _reason(exc))
        return None
    r.caps["deadlock"] = True
    r.caps["process_privilege"] = True
    status = rows[0][2] if rows and len(rows[0]) > 2 else ""
    return parse_latest_deadlock(status or "", r.engine)


# ---- blocking tree -------------------------------------------------------------

def _key(connection_id, trx_id):
    if connection_id:
        return "c%d" % connection_id
    return "t%s" % trx_id if trx_id else None


def _short_lock(lock):
    return {k: lock.get(k) for k in ("lock_type", "mode", "mode_label", "schema", "table",
                                     "index", "lock_data", "lock_id", "status")}


def _edge_wait(edge):
    """Seconds waited: the lock wait start when known, else the statement's running time."""
    return edge.get("wait_s") if edge.get("wait_s") is not None else edge.get("statement_s")


def _build_tree(r, rows_waits, mdl_waits, last_statements):
    nodes = {}
    trx_by_conn = {t["connection_id"]: t for t in r.trx if t["connection_id"]}

    def node(cid, trx_id):
        key = _key(cid, trx_id)
        if key is None:
            return None
        if key not in nodes:
            trx = r.trx_by_id.get(trx_id) if trx_id else None
            if trx is None and cid:
                trx = trx_by_conn.get(cid)
            session = r.sessions.get(cid) or {}
            command = session.get("command")
            query = session.get("info") or (trx or {}).get("query")
            nodes[key] = {
                "key": key, "connection_id": cid, "trx_id": (trx or {}).get("trx_id") or trx_id,
                "user": session.get("user"), "host": session.get("host"), "db": session.get("db"),
                "command": command, "state": session.get("state"), "time_s": session.get("time_s"),
                "query": query, "last_statement": last_statements.get(cid) if not query else None,
                "idle_in_transaction": False,
                "trx_state": (trx or {}).get("state"), "trx_age_s": (trx or {}).get("age_s"),
                "trx_started": (trx or {}).get("started"),
                "rows_locked": (trx or {}).get("rows_locked"),
                "rows_modified": (trx or {}).get("rows_modified"),
                "ps_thread_id": session.get("ps_thread_id"),
                "kill_allowed": kill_allowed(r.kill_privilege, session.get("user")) if cid else None,
                "xa_prepared": r.xa.get(cid) if cid else None,
                "root": False, "cycle": False, "blocked_count": 0, "kill": None,
                "_edges": [], "_parents": set(), "_in_trx": bool(trx or trx_id),
            }
        return nodes[key]

    for w in rows_waits:
        req, blk = w["requesting"], w["blocking"]
        waiter = node(req["connection_id"], req["trx_id"])
        blocker = node(blk["connection_id"], blk["trx_id"])
        if waiter is None or blocker is None or waiter is blocker:
            continue
        trx = r.trx_by_id.get(req["trx_id"]) or {}
        blocker["_edges"].append({"to": waiter["key"], "kind": "row", "reason": "granted"
                                  if blk["status"] != "WAITING" else "queued",
                                  "lock": _short_lock(req), "blocking_lock": _short_lock(blk),
                                  "wait_s": trx.get("wait_s"),
                                  "statement_s": (r.sessions.get(req["connection_id"]) or {}).get("time_s")})
        waiter["_parents"].add(blocker["key"])
    for w in mdl_waits:
        p = w["waiting"]
        waiter = node(p["connection_id"], None)
        for b in w["blocked_by"]:
            blocker = node(b["lock"]["connection_id"], None)
            if waiter is None or blocker is None or waiter is blocker:
                continue
            if b["lock"]["duration"] == "TRANSACTION" and b["reason"] == "granted":
                blocker["_in_trx"] = True
            session = r.sessions.get(p["connection_id"]) or {}
            blocker["_edges"].append({
                "to": waiter["key"], "kind": "metadata", "reason": b["reason"],
                "lock": {"lock_type": p["lock_type"], "mode": p["short"], "object_type": p["object_type"],
                         "schema": p["schema"], "table": p["name"]},
                "blocking_lock": {"lock_type": b["lock"]["lock_type"], "mode": b["lock"]["short"],
                                  "status": b["lock"]["status"]},
                "wait_s": None, "statement_s": session.get("time_s"), "certain": w["certain"]})
            waiter["_parents"].add(blocker["key"])

    for n in nodes.values():
        # Sleep with an open transaction: it waits for its client, not the server.
        n["idle_in_transaction"] = n["_in_trx"] and n["command"] == "Sleep" and not n["query"]

    # Roots: blockers that wait for nobody. A cycle (deadlock detection off) has
    # no such node, so its oldest transaction stands in as the root.
    blockers = [n for n in nodes.values() if n["_edges"]]
    roots = [n for n in blockers if not n["_parents"]]
    reach = set()

    def walk(start):
        stack, seen = [start], set()
        while stack:
            k = stack.pop()
            if k in seen:
                continue
            seen.add(k)
            stack.extend(e["to"] for e in nodes[k]["_edges"])
        return seen

    for root in roots:
        reach |= walk(root["key"])
    leftover = [n for n in blockers if n["key"] not in reach]
    while leftover:
        pick = max(leftover, key=lambda n: (n["trx_age_s"] or 0, -(n["connection_id"] or 0)))
        pick["cycle"] = True
        roots.append(pick)
        covered = walk(pick["key"])
        for k in covered:
            nodes[k]["cycle"] = True
        reach |= covered
        leftover = [n for n in leftover if n["key"] not in reach]
    for root in roots:
        root["root"] = True
        root["blocked_count"] = len(walk(root["key"])) - 1
        cid = root["connection_id"]
        if cid:
            running = root["command"] not in (None, "Sleep") and bool(root["query"])
            root["kill"] = {"query": "KILL QUERY %d" % cid if running else None,
                            "connection": "KILL %d" % cid}
    roots.sort(key=lambda n: (-n["blocked_count"], -(n["trx_age_s"] or 0)))

    # Bound representation independently of the graph. A modest depth also
    # keeps JSON encoders and consumers safe without changing recursion limits.
    node_limit = min(MAX_TREE_NODES, max(r.limit, 50))
    edge_limit = MAX_TREE_EDGES
    stats = {"node_limit": node_limit, "edge_limit": edge_limit, "depth_limit": MAX_TREE_DEPTH,
             "nodes": 0, "edges": 0, "omitted_roots": 0, "omitted_branches": 0, "truncated": False}
    tree = []
    path = set()
    # Tasks carry destinations rather than recursively returning child lists.
    stack = [("enter", n, None, tree, 0, None) for n in reversed(roots)]
    while stack:
        action, n, edge, dest, depth, parent = stack.pop()
        if action == "leave":
            path.remove(n["key"])
            continue
        edge_count = len(edge or ())
        if stats["nodes"] >= node_limit or stats["edges"] + edge_count > edge_limit:
            stats["truncated"] = True
            if parent is None:
                stats["omitted_roots"] += 1
            else:
                parent["truncated"] = True
                parent["omitted_branches"] = parent.get("omitted_branches", 0) + 1
                stats["omitted_branches"] += 1
            continue
        out = {k: v for k, v in n.items() if not k.startswith("_")}
        out.update({"waits_for": edge, "blocked": []})
        dest.append(out)
        stats["nodes"] += 1
        stats["edges"] += edge_count
        if n["key"] in path:
            out["repeat"] = True
            continue
        grouped = {}
        for e in n["_edges"]:
            grouped.setdefault(e["to"], []).append({k: v for k, v in e.items() if k != "to"})
        if depth >= MAX_TREE_DEPTH and grouped:
            out.update({"truncated": True, "omitted_branches": len(grouped), "truncation_reason": "depth"})
            stats["truncated"] = True
            stats["omitted_branches"] += len(grouped)
            continue
        children = sorted(grouped.items(), key=lambda item: -max(
            [_edge_wait(e) or 0 for e in item[1]] or [0]))
        path.add(n["key"])
        stack.append(("leave", n, None, None, depth, None))
        for to, edges in reversed(children):
            stack.append(("enter", nodes[to], edges, out["blocked"], depth + 1, out))
    r.tree_stats = stats
    return tree, roots, nodes


def _assemble(r, deadlock, waits_only):
    last = getattr(r, "last_statements", {})
    tree, roots, nodes = _build_tree(r, r.waits, r.mdl_waits, last)
    ddl_waits = _ddl_waits(r, nodes, last) if r.scope != "deadlock" else []
    involved = set()
    for n in nodes.values():
        if n["_edges"] or n["_parents"]:
            involved.add(n["key"])
    for d in ddl_waits:
        involved.add(_key(d["connection_id"], None))
        involved |= {_key(b["connection_id"], None) for b in d["blockers"] if b["connection_id"]}
    transactions = []
    for t in r.trx:
        session = r.sessions.get(t["connection_id"]) or {}
        key = _key(t["connection_id"], t["trx_id"])
        if waits_only and key not in involved:
            continue
        item = dict(t)
        command = session.get("command")
        item.update({
            "user": session.get("user"), "host": session.get("host"), "db": session.get("db"),
            "command": command, "time_s": session.get("time_s"), "session_state": session.get("state"),
            "current_statement": session.get("info") or t["query"],
            "last_statement": last.get(t["connection_id"]) if not (session.get("info") or t["query"]) else None,
            "idle_in_transaction": command == "Sleep" and not (session.get("info") or t["query"]),
            "blocking": sorted(e["to"] for e in (nodes.get(key) or {}).get("_edges", [])),
            "blocked_by": sorted((nodes.get(key) or {}).get("_parents", set())),
            "kill": None, "ps_thread_id": session.get("ps_thread_id"),
            "kill_allowed": kill_allowed(r.kill_privilege, session.get("user")) if t["connection_id"] else None,
        })
        if t["connection_id"]:
            running = command not in (None, "Sleep") and bool(item["current_statement"])
            item["kill"] = {"query": "KILL QUERY %d" % t["connection_id"] if running else None,
                            "connection": "KILL %d" % t["connection_id"]}
        transactions.append(item)
    # Every lock taking part in a wait is listed, even past the row limit.
    locks = list(r.locks)
    seen = {lock["lock_id"] for lock in locks}
    for w in r.waits:
        for k in ("requesting", "blocking"):
            if w[k]["lock_id"] not in seen:
                seen.add(w[k]["lock_id"])
                locks.append(w[k])
    status_rank = {"WAITING": 0, "GRANTED": 1}
    locks.sort(key=lambda lock: (status_rank.get(lock["status"], 2), lock["schema"] or "",
                                 lock["table"] or "", lock["lock_type"] != "TABLE",
                                 lock["index"] or "", str(lock["trx_id"])))
    waits = []
    for w in r.waits:
        trx = r.trx_by_id.get(w["requesting"]["trx_id"]) or {}
        session = r.sessions.get(w["requesting"]["connection_id"]) or {}
        waits.append({"requesting": w["requesting"], "blocking": w["blocking"],
                      "wait_s": trx.get("wait_s"), "wait_started": trx.get("wait_started"),
                      "statement": session.get("info"), "statement_s": session.get("time_s")})
    waits.sort(key=lambda w: -(w["wait_s"] if w["wait_s"] is not None else w["statement_s"] or 0))
    mdl_waits = []
    for w in r.mdl_waits:
        p = w["waiting"]
        session = r.sessions.get(p["connection_id"]) or {}
        mdl_waits.append({
            "waiting": p, "statement": session.get("info"), "state": session.get("state"),
            "statement_s": session.get("time_s"), "certain": w["certain"],
            "blocker_groups": w.get("blocker_groups", []),
            "blockers_total": w.get("blockers_total", len(w["blocked_by"])),
            "blockers_omitted": w.get("blockers_omitted", 0), "truncated": w.get("truncated", False),
            "blocked_by": [{"reason": b["reason"], "lock": b["lock"],
                            "statement": (r.sessions.get(b["lock"]["connection_id"]) or {}).get("info")}
                           for b in w["blocked_by"]],
        })
    mdl = sorted(r.mdl_locks, key=lambda m: (m["status"] != "PENDING", m["object_type"] or "",
                                              m["schema"] or "", m["name"] or ""))

    notes = list(r.notes)
    if r.caps["data_locks_global_latch"] and r.ps_on and r.scope != "deadlock":
        notes.insert(0, "Reading performance_schema.data_locks and data_lock_waits on MySQL %s "
                        "holds InnoDB's global lock-system latch while each batch is copied, which "
                        "stalls other sessions' locking on a busy server (fixed in 8.0.40, 8.4.3 and "
                        "9.1.0). Refresh by hand and avoid frequent automatic refresh; --waits-only "
                        "skips the full lock list, so it holds the latch for less time."
                     % r.version)
    if r.engine == "mariadb" and r.caps["lock_waits"]:
        notes.append("MariaDB lists only the row locks involved in a wait "
                     "(information_schema.INNODB_LOCKS). Locks that block nobody are not visible "
                     "through SQL; rows locked and lock structs in the transactions table count them. "
                     "MariaDB also shows record-only and next-key locks both as X, and gap and "
                     "insert-intention locks both as X,GAP.")
    if r.caps["row_locks"] != "none" or r.caps["lock_waits"]:
        notes.append(IMPLICIT_NOTE)
    if r.mdl_waits:
        notes.append("Metadata lock blockers follow the server's compatibility rules: a waiting "
                     "request waits for conflicting locks other sessions hold on the same object, or "
                     "queues behind a waiting request with priority over it (for example SELECTs "
                     "queued behind a waiting ALTER TABLE). Their wait time is the waiting "
                     "statement's running time.")
    if any(not w["certain"] for w in r.mdl_waits):
        notes.append("Some metadata locks (MariaDB BACKUP locks) follow rules myflames does not "
                     "model, so every lock other sessions hold on that object is listed as a "
                     "possible blocker.")
    if ddl_waits:
        notes.append(DDL_QUEUE_NOTE)
    if any(d["precision"] == "likely" for d in ddl_waits):
        # The specific note replaces the general "Performance Schema is off" one.
        notes = [n for n in notes if not n.startswith(MARIADB_PS_OFF_MDL_NOTE[:60])]
        notes.append(_ddl_likely_note(r))
        if r.caps["process_privilege"] is False:
            notes.append("Without PROCESS, open transactions are hidden, so myflames cannot list "
                         "likely blockers for a waiting DDL statement.")
    if any(d["precision"] == "granted_locks" for d in ddl_waits):
        notes.append(MLI_NOTE)
    if r.engine == "mysql" and (r.xa and any(n["xa_prepared"] for n in nodes.values()) or any(
            b["xa_prepared"] or not b["connection_id"] for d in ddl_waits for b in d["blockers"])):
        notes.append(XA_NOTE)
    if roots or ddl_waits:
        notes.extend(_privilege_notes(r.engine, r.kill_privilege))
    for section, name in (("transactions", "transactions"), ("row_locks", "row locks"),
                          ("waits", "lock waits"), ("metadata_locks", "metadata locks")):
        if r.truncated[section]:
            notes.append("Showing the first %d %s; raise the limit to see more." % (r.limit, name))
    if any(lock["lock_data_truncated"] for lock in locks):
        notes.append("Long LOCK_DATA values are cut: the server keeps 8 KB and myflames shows "
                     "%d characters." % LOCK_DATA_CAP)
    if any(lock["lock_data_missing"] for lock in locks):
        notes.append("LOCK_DATA is empty for a record lock when its page is not in the buffer pool; "
                     "the server does not read the page from disk just to show the value.")
    if roots:
        notes.append(KILL_NOTE)
    if r.scope == "all" and (r.waits or r.mdl_waits):
        notes.append(SNAPSHOT_NOTE)

    longest = [w["wait_s"] if w["wait_s"] is not None else w["statement_s"] for w in waits
               if w["wait_s"] is not None or w["statement_s"] is not None] + \
              [w["statement_s"] for w in mdl_waits if w["statement_s"] is not None]
    blocked_keys = {n["key"] for n in nodes.values() if n["_parents"]}
    summary = {
        "transactions": len(transactions), "row_lock_waits": len(waits),
        "metadata_lock_waits": len(mdl_waits), "blocked_sessions": len(blocked_keys),
        "root_blockers": len(roots),
        "idle_in_transaction_blockers": sum(1 for n in roots if n["idle_in_transaction"]),
        "longest_wait_s": max(longest) if longest else None,
        "oldest_transaction_s": max([t["age_s"] for t in transactions if t["age_s"] is not None] or [0])
        if transactions else None,
        "row_locks": len(locks), "deadlock": bool(deadlock and deadlock.get("transactions")),
        "ddl_waits": len(ddl_waits),
    }
    return {
        "schema_version": SCHEMA_VERSION, "engine": r.engine, "server_version": r.version,
        "server_identity": r.server_identity,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": r.scope, "limit": r.limit, "capabilities": r.caps, "notes": notes,
        "summary": summary, "ddl_waits": ddl_waits, "kill_privilege": r.kill_privilege,
        "blocking_tree": tree, "blocking_tree_budget": r.tree_stats, "transactions": transactions,
        "row_locks": locks, "row_locks_truncated": r.truncated["row_locks"],
        "waits": waits, "waits_truncated": r.truncated["waits"],
        "metadata_locks": {"locks": mdl, "waits": mdl_waits,
                           "truncated": r.truncated["metadata_locks"],
                           "blocker_groups": getattr(r, "mdl_groups", []),
                           "total_edges": getattr(r, "mdl_edge_total", 0),
                           "totals_scope": "collected_locks",
                           "materialized_edges": sum(len(w["blocked_by"]) for w in r.mdl_waits),
                           "edge_limit": MAX_MDL_EDGES,
                           "waits_truncated": any(w.get("truncated") for w in r.mdl_waits)},
        "deadlock": deadlock,
    }


# ---- text output for the CLI ---------------------------------------------------

def _one_line(text, width):
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


def _secs(value):
    if value is None:
        return "?"
    value = float(value)
    if value >= 3600:
        return "%dh%02dm" % (value // 3600, (value % 3600) // 60)
    if value >= 60:
        return "%dm%02ds" % (value // 60, value % 60)
    return "%ds" % value


def _who(n):
    if n.get("connection_id"):
        name = "connection %d" % n["connection_id"]
    else:
        name = "transaction %s (no session)" % n.get("trx_id")
    extra = [x for x in (n.get("user") and "%s@%s" % (n["user"], n.get("host") or "?"), n.get("db")) if x]
    return name + (" (%s)" % ", ".join(extra) if extra else "")


def _rows(count):
    """'1 row', '2,500 rows', '? rows'."""
    if count is None:
        return "? rows"
    return "%s row%s" % ("{:,}".format(count), "" if count == 1 else "s")


def _lock_text(lock):
    if not lock:
        return "?"
    where = ".".join(x for x in (lock.get("schema"), lock.get("table")) if x) or "?"
    if lock.get("lock_type") == "RECORD":
        data = lock.get("lock_data")
        return "%s (%s) on %s index %s%s" % (lock.get("mode"), lock.get("mode_label"), where,
                                              lock.get("index") or "?",
                                              " [%s]" % _one_line(data, 40) if data else "")
    return "%s (%s) on %s" % (lock.get("mode"), lock.get("mode_label") or lock.get("lock_type"), where)


def _tree_lines(node, depth, out):
    pad = "  " * depth
    if depth == 0:
        head = "%s%s ROOT BLOCKER" % (pad, _who(node).upper()[:1] + _who(node)[1:])
        if node.get("cycle"):
            head += " (in a wait cycle)"
        out.append(head + ", blocks %d" % node.get("blocked_count", 0))
    else:
        for e in node.get("waits_for") or []:
            lock = e["lock"]
            if e["kind"] == "metadata":
                what = "metadata %s on %s %s" % (lock.get("lock_type"), lock.get("object_type"),
                                                 ".".join(x for x in (lock.get("schema"), lock.get("table")) if x))
                if e["reason"] == "queued":
                    what += " (queued behind a waiting request)"
            else:
                what = "row lock " + _lock_text(lock)
            seconds = _secs(_edge_wait(e)) if _edge_wait(e) is not None else "?"
            if e.get("wait_s") is None and e.get("statement_s") is not None:
                seconds = "~" + seconds
            out.append("%s└─ %s waits %s for %s" % (pad[:-2], _who(node), seconds, what))
            break
    detail = pad + "   "
    if node.get("trx_id"):
        parts = ["trx %s" % node["trx_id"]]
        if node.get("trx_state"):
            parts.append(node["trx_state"])
        if node.get("trx_age_s") is not None:
            parts.append("open " + _secs(node["trx_age_s"]))
        if node.get("rows_locked") is not None:
            parts.append("%d rows locked" % node["rows_locked"])
        out.append(detail + ", ".join(parts))
    if node.get("idle_in_transaction"):
        out.append("%sidle in transaction for %s (command Sleep)" % (detail, _secs(node.get("time_s"))))
    if node.get("query"):
        out.append("%srunning: %s" % (detail, _one_line(node["query"], 100)))
    elif node.get("last_statement"):
        out.append("%slast statement: %s" % (detail, _one_line(node["last_statement"], 100)))
    if node.get("kill"):
        kill = node["kill"]
        if not node.get("xa_prepared"):
            out.append("%sto end it yourself: %s" % (detail, " or ".join(
                x for x in (kill.get("query"), kill.get("connection")) if x)))
        if node.get("xa_prepared"):
            xa = node["xa_prepared"]
            out.append("%sprepared XA transaction: ending the session releases nothing; end it with %s" % (
                detail, "%s or %s" % (xa["commit"], xa["rollback"]) if xa.get("xid")
                else "XA COMMIT or XA ROLLBACK"))
        elif node.get("kill_allowed"):
            out.append("%sor, after a confirmation: myflames locks --kill %d" % (detail, node["connection_id"]))
    if node.get("repeat"):
        out.append("%s(shown above)" % detail)
    if node.get("truncated"):
        out.append("%s(omitted branches: %d)" % (detail, node.get("omitted_branches", 0)))
    for child in node.get("blocked") or []:
        _tree_lines(child, depth + 1, out)


def no_connection_text(engine):
    """What owns a metadata lock that no client connection holds."""
    if engine == "mariadb":
        return "It belongs to a background server thread."
    return ("It belongs to a background server thread, or to a prepared XA transaction that is no longer "
            "attached to a session. XA RECOVER lists prepared XA transactions (it needs XA_RECOVER_ADMIN), "
            "and XA COMMIT 'xid' or XA ROLLBACK 'xid' ends one.")


_PRECISION_HEAD = {"exact": "blocked by", "granted_locks": "sessions holding a lock on the table",
                   "likely": "likely blockers (no metadata lock data; see notes)"}


def _ddl_lines(d, report, out):
    where = ".".join(x for x in (d["schema"], d["table"]) if x) or "?"
    named = ".".join(x for x in (d.get("statement_schema"), d.get("statement_table")) if x)
    wants = " for %s" % d["requested"] if d["requested"] else ""
    if named and d["table"] and not (_same_name(d.get("statement_table"), d["table"])
                                     and _same_name(d.get("statement_schema"), d["schema"])):
        # A foreign key or RENAME also locks other tables (MySQL).
        wants += " on %s" % where
        where = named
    out.append("Connection %d (%s@%s) %s on %s waits ~%s%s: %s" % (
        d["connection_id"], d["user"] or "?", d["host"] or "?", d["kind"], where, _secs(d["wait_s"]),
        wants, _one_line(d["statement"], 90)))
    if not d["blockers"]:
        out.append("  no blocker found%s" % (" (see notes)" if d["precision"] != "exact" else ""))
    else:
        out.append("  %s:" % _PRECISION_HEAD[d["precision"]])
    for b in d["blockers"]:
        if not b["connection_id"]:
            out.append("  - a thread with no client connection (server thread %s)%s" % (
                b.get("ps_thread_id") or "?", ", holds %s" % ", ".join(l["lock_type"] for l in b["locks"])
                if b["locks"] else ""))
            out.append("    " + no_connection_text(report["engine"]))
            continue
        parts = ["connection %d (%s@%s)" % (b["connection_id"], b["user"] or "?", b["host"] or "?")]
        if b["locks"]:
            parts.append("holds %s" % ", ".join("%s%s" % (l["lock_type"], " (%s)" % l["duration"].lower()
                                                           if l.get("duration") else "") for l in b["locks"]))
        if b["reason"] == "queued":
            parts.append("its own request waits ahead of the DDL")
        if b["reason"] == "chain":
            parts.append("blocks connection %d, which the DDL waits for" % b["via"])
        if b["idle_in_transaction"]:
            parts.append("idle in transaction %s" % _secs(b["time_s"]))
        elif b["running"]:
            parts.append("running %s" % _secs(b["time_s"]))
        if b["trx_age_s"] is not None:
            parts.append("transaction open %s" % _secs(b["trx_age_s"]))
        if b["rows_modified"] is not None:
            parts.append("%s changed" % _rows(b["rows_modified"]))
        if b["started_before_ddl"] is True:
            parts.append("started before the DDL")
        elif b["started_before_ddl"] is False:
            parts.append("started after the DDL")
        if b["waits_for"]:
            parts.append("itself waits for connection %s" % ", ".join(str(i) for i in b["waits_for"]))
        out.append("  - " + ", ".join(parts))
        if b["culprit"]:
            out.append("    usual culprit: %s" % (
                "an idle transaction at the start of the chain" if b["reason"] == "chain"
                else "a transaction left open after using the table"))
        if b["statement"]:
            out.append("    running: %s" % _one_line(b["statement"], 100))
        elif b["last_statement"]:
            out.append("    last statement: %s" % _one_line(b["last_statement"], 100))
        if b.get("xa_prepared"):
            xa = b["xa_prepared"]
            out.append("    prepared XA transaction: ending the session releases nothing; end it with %s" % (
                "%s or %s" % (xa["commit"], xa["rollback"]) if xa.get("xid") else "XA COMMIT or XA ROLLBACK"))
        elif b["kill_allowed"]:
            out.append("    end it: myflames locks --kill %d%s" % (
                b["connection_id"], "" if not b["running"] else "  (or --kill %d --query)" % b["connection_id"]))
        elif b["kill_allowed"] is False:
            out.append("    ending it needs another account (see notes): KILL %d" % b["connection_id"])
    if d["blockers_truncated"]:
        out.append("  (showing the first %d)" % DDL_LIKELY_MAX)
    if d["queued"]:
        out.append("  queued behind it%s: %s" % (
            "" if d["precision"] == "exact" else " (likely)", "; ".join(
                "connection %d %s (%s)" % (q["connection_id"], _one_line(q["statement"], 50) or "?",
                                           _secs(q["wait_s"])) for q in d["queued"][:10])
            + ("; and %d more" % (len(d["queued"]) - 10) if len(d["queued"]) > 10 else "")))


def format_text(report):
    """Human output: DDL waits, blocking tree, transactions, row locks, metadata waits, deadlock."""
    from .text_format import _table
    out = []
    if report.get("ddl_waits"):
        out.append("DDL WAITING FOR A METADATA LOCK")
        for d in report["ddl_waits"]:
            _ddl_lines(d, report, out)
            out.append("")
    if report["scope"] != "deadlock":
        out.append("BLOCKING TREE")
        if report["blocking_tree"]:
            for root in report["blocking_tree"]:
                _tree_lines(root, 0, out)
                out.append("")
        else:
            out.append("No session is waiting for a lock.\n")
        trx = report["transactions"]
        out.append("TRANSACTIONS (%d)" % len(trx))
        if trx:
            rows = [[t["trx_id"] or "?", str(t["connection_id"] or "-"), t["state"] or "?",
                     _secs(t["age_s"]), "idle" if t["idle_in_transaction"] else (t["command"] or "?"),
                     "-" if t["rows_locked"] is None else str(t["rows_locked"]),
                     (t["isolation_level"] or "?").replace("REPEATABLE READ", "RR").replace("READ COMMITTED", "RC"),
                     _one_line(t["current_statement"] or (t["last_statement"] and "last: " + t["last_statement"]) or "", 60) or "-"]
                    for t in trx]
            out.append(_table(["TRX", ">CONN", "STATE", ">AGE", "SESSION", ">ROWS", "ISO", "STATEMENT"], rows).rstrip("\n"))
        elif not report["capabilities"]["transactions"]:
            out.append("Not available (see notes).")
        out.append("")
        locks = report["row_locks"]
        out.append("ROW LOCKS (%d%s)" % (len(locks), ", waits only" if report["capabilities"]["row_locks"] == "waits_only" else ""))
        if locks:
            rows = [[lock["status"] or "?", str(lock["trx_id"]), str(lock["connection_id"] or "-"),
                     ".".join(x for x in (lock["schema"], lock["table"]) if x) or "?",
                     lock["index"] or "-", lock["mode"] or "?", lock["mode_label"] or "",
                     _one_line(lock["lock_data"], 40) if lock["lock_data"] else "-"]
                    for lock in locks]
            out.append(_table(["STATUS", "TRX", ">CONN", "TABLE", "INDEX", "MODE", "MEANING", "LOCK_DATA"], rows).rstrip("\n"))
        out.append("")
        mdl = report["metadata_locks"]["waits"]
        if mdl:
            out.append("METADATA LOCK WAITS (%d)" % len(mdl))
            for w in mdl:
                p = w["waiting"]
                blockers = ", ".join("%s by connection %s%s" % (b["lock"]["short"], b["lock"]["connection_id"] or "?",
                                                               " (queued)" if b["reason"] == "queued" else "")
                                     for b in w["blocked_by"]) or "no conflicting lock found"
                out.append("connection %s waits %s for %s on %s %s; blocked by %s" % (
                    p["connection_id"] or "?", _secs(w["statement_s"]), p["short"], p["object_type"],
                    ".".join(x for x in (p["schema"], p["name"]) if x), blockers))
            out.append("")
    dl = report.get("deadlock")
    if report["scope"] == "deadlock" or dl:
        out.append("LATEST DETECTED DEADLOCK")
        if not dl:
            out.append("None recorded since the server started." if report["capabilities"]["deadlock"]
                       else "Not available (see notes).")
        else:
            out.append("at %s; rolled back transaction (%s)" % (dl["detected_at"] or "?", dl["victim"] or "?"))
            for t in dl["transactions"]:
                out.append("(%d) trx %s, connection %s%s: %s" % (
                    t["n"], t["trx_id"], t["connection_id"], " [VICTIM]" if t["victim"] else "",
                    _one_line(t["statement"], 90) or "?"))
                for lock in t["holds"]:
                    out.append("    holds   %s" % _lock_text(lock))
                if t["waiting_for"]:
                    out.append("    waits   %s" % _lock_text(t["waiting_for"]))
            if report["scope"] == "deadlock":
                out.append("")
                out.append(dl["raw"])
    return "\n".join(out).rstrip("\n") + "\n"


# ---- the one write: end a blocker after a confirmation ------------------------------

KILL_MODES = ("connection", "query")
_SERVER_THREAD_USERS = ("system user", "event_scheduler")


def _check_kill_args(connection_id, mode, expect):
    if isinstance(connection_id, bool) or not isinstance(connection_id, int) or \
            not 0 < connection_id < 2 ** 63:
        raise ValueError("Choose the connection to end: a positive connection id.")
    if mode not in KILL_MODES:
        raise ValueError("Choose mode 'connection' (KILL) or 'query' (KILL QUERY).")
    message = ("Send the session you confirmed (expect.user and expect.host) so myflames can check "
               "it is still the same session.")
    if not isinstance(expect, dict):
        raise ValueError(message)
    user, host = expect.get("user"), expect.get("host")
    if not isinstance(user, str) or not user or not isinstance(host, str) or not host:
        raise ValueError(message)
    started, thread = expect.get("trx_started"), expect.get("ps_thread_id")
    if started is not None and not isinstance(started, str):
        raise ValueError("expect.trx_started must be text or null.")
    if thread is not None and (isinstance(thread, bool) or not isinstance(thread, int)):
        raise ValueError("expect.ps_thread_id must be an integer or null.")
    return {"user": user, "host": host, "trx_started": started, "ps_thread_id": thread}


def _same_host(a, b):
    """Hosts match, allowing one side to carry the client port (host:port)."""
    if a is None or b is None:
        return a == b
    return a == b or a.startswith(b + ":") or b.startswith(a + ":")


def _running(session):
    return (session or {}).get("command") not in (None, "Sleep", "Killed") and bool((session or {}).get("info"))


def read_target(conn, engine, ps_on, connection_id):
    """Read one session and its open InnoDB transaction now.

    Returns ``(session or None, the connection id of the session that read it)``.
    The session comes from the same source as the lock report (threads with
    Performance Schema on, else the process list), so user and host compare
    exactly. ``trx_checked`` is False when INNODB_TRX was not readable (PROCESS).
    """
    rows = None
    for source_ps in ((True, False) if ps_on else (False,)):
        sql = _session_sql(engine, source_ps, "{ID} = %d" % connection_id, " LIMIT 1")
        sql = sql.replace(" FROM ", ", CONNECTION_ID() FROM ", 1)
        try:
            rows = _q(conn, sql)
            break
        except ConnectorError as exc:
            if not (source_ps and _denied(exc)):
                raise
    if not rows:
        return None, None
    session = _session_from_row(rows[0][:9])
    own = _int(rows[0][9]) if len(rows[0]) > 9 else None
    session["trx"], session["trx_checked"] = None, False
    try:
        trx = _q(conn, "SELECT trx_id, trx_state, trx_started, TIMESTAMPDIFF(SECOND, trx_started, NOW()), "
                       "trx_rows_locked, trx_rows_modified FROM information_schema.INNODB_TRX "
                       "WHERE trx_mysql_thread_id = %d LIMIT 1" % connection_id)
        session["trx_checked"] = True
        if trx:
            t = list(trx[0]) + [None] * (6 - len(trx[0]))
            session["trx"] = {"trx_id": t[0], "state": t[1], "started": t[2], "age_s": _int(t[3]),
                              "rows_locked": _int(t[4]), "rows_modified": _int(t[5])}
    except ConnectorError as exc:
        if not (_needs_process(exc) or _denied(exc)):
            raise
    # MySQL: a prepared XA transaction keeps its metadata locks after the session ends.
    session["xa_prepared"] = None
    if engine == "mysql" and ps_on and session.get("ps_thread_id") and session.get("command") == "Sleep" \
            and not session.get("info"):
        try:
            last = _q(conn, "SELECT LEFT(SQL_TEXT, %d) FROM performance_schema.events_statements_current "
                            "WHERE THREAD_ID = %d AND (NESTING_EVENT_TYPE IS NULL OR NESTING_EVENT_TYPE = "
                            "'TRANSACTION') ORDER BY EVENT_ID DESC LIMIT 1" % (TEXT_CAP, session["ps_thread_id"]))
        except ConnectorError:
            last = []
        if last and last[0]:
            session["xa_prepared"] = xa_prepared_from(last[0][0])
    return session, own


def _kill_problem(target, own, cid, mode, expect):
    """(status, message) when myflames must not send the KILL, else None."""
    if target is None:
        return "gone", ("Connection %d is no longer connected, so there is nothing to end. Refresh the "
                        "lock report." % cid)
    if own is not None and own == cid:
        return "refused", "Connection %d is the session myflames is using; it never ends itself." % cid
    if (target.get("info") or "").lstrip().startswith("/*myflames:"):
        return "refused", ("Connection %d is running a myflames statement. Stop it from the myflames "
                           "workspace or command that started it." % cid)
    user, command = target.get("user"), target.get("command") or ""
    if not user or user in _SERVER_THREAD_USERS or command == "Daemon" or command.startswith("Binlog Dump"):
        return "refused", ("Connection %d is a server thread (%s), not a client session; myflames does "
                           "not end server threads." % (cid, user or command or "no user"))
    if expect is not None:
        changed = []
        if user != expect["user"]:
            changed.append("user %s, not %s" % (user, expect["user"]))
        if not _same_host(target.get("host"), expect["host"]):
            changed.append("host %s, not %s" % (target.get("host"), expect["host"]))
        if expect["ps_thread_id"] is not None and target.get("ps_thread_id") is not None \
                and target["ps_thread_id"] != expect["ps_thread_id"]:
            changed.append("another server thread")
        if changed:
            return "refused", ("Connection %d is now a different session (%s): the one you saw ended, or "
                               "the connection switched user. Nothing was ended. Refresh the lock report."
                               % (cid, "; ".join(changed)))
        if expect["trx_started"] is not None and target.get("trx_checked"):
            trx = target.get("trx")
            if trx is None or trx.get("started") != expect["trx_started"]:
                now = ("it has no open transaction now" if trx is None
                       else "its open transaction now started at %s" % trx.get("started"))
                return "refused", ("The transaction you saw on connection %d (started %s) has ended: %s. "
                                   "It may no longer block anything, so nothing was ended. Refresh the "
                                   "lock report." % (cid, expect["trx_started"], now))
    xa = target.get("xa_prepared")
    if xa:
        how = ("XA COMMIT %s or XA ROLLBACK %s" % (xa["xid"], xa["xid"]) if xa.get("xid")
               else "XA COMMIT or XA ROLLBACK (XA RECOVER lists the xid; it needs XA_RECOVER_ADMIN)")
        return "refused", ("Connection %d ran XA PREPARE: its locks belong to the prepared XA transaction, "
                           "which survives the end of the session, so ending it would release nothing. "
                           "End the transaction with %s. Nothing was sent." % (cid, how))
    if ((target.get("trx") or {}).get("state") or "") == "ROLLING BACK":
        return "refused", ("Connection %d is already rolling back its transaction and keeps its locks "
                           "until that finishes. Nothing more was sent." % cid)
    if command == "Killed" and mode == "query":
        return "refused", ("Connection %d already received a KILL. If it was KILL CONNECTION, it is rolling "
                           "back its transaction and keeps its locks until that finishes. On MariaDB it may "
                           "instead have received KILL QUERY while idle, and its transaction is still open. "
                           "Choose Kill connection to end it." % cid)
    if mode == "query" and not _running(target):
        return "refused", ("Connection %d is not running a statement, so KILL QUERY would do nothing: its "
                           "open transaction would keep its locks. Choose Kill connection to roll the "
                           "transaction back and release them." % cid)
    return None


def _kill_error(engine, exc, cid):
    """Map the server's KILL error to (status, message); unexpected errors raise."""
    text = str(exc)
    if "1094" in text or "Unknown thread id" in text:
        return "gone", "Connection %d ended before the KILL arrived; nothing was ended." % cid
    if "1095" in text or "not owner of thread" in text or "1227" in text or "Access denied" in text:
        if engine == "mariadb":
            need = ("CONNECTION ADMIN (or SUPER before MariaDB 11.0); on a Galera node, applier and "
                    "high-priority threads cannot be ended at all")
        else:
            need = ("CONNECTION_ADMIN or SUPER, and ending a session of an account that has SYSTEM_USER "
                    "also needs SYSTEM_USER")
        return "denied", ("The server refused to end connection %d (%s). Ending another user's session "
                          "needs %s." % (cid, _reason(exc), need))
    raise ValueError("Could not end connection %d: %s" % (cid, _reason(exc)))


def _after_view(session):
    if session is None:
        return {"present": False}
    trx = session.get("trx") or {}
    return {"present": True, "command": session.get("command"), "state": session.get("state"),
            "statement": session.get("info"), "trx_state": trx.get("state"),
            "rows_modified": trx.get("rows_modified")}


def _kill_message(mode, cid, target, after):
    rows = ((target or {}).get("trx") or {}).get("rows_modified")
    if mode == "query":
        if after.get("present") and _running(_after_session(after)) and \
                after.get("statement") == (target or {}).get("info"):
            return ("Sent KILL QUERY %d. The statement is still stopping (it rolls back its own changes "
                    "first). If it ran inside a transaction, that transaction stays open with its locks."
                    % cid)
        return ("Stopped the running statement on connection %d. If it ran inside a transaction, that "
                "transaction stays open with its locks until it commits or rolls back." % cid)
    if not after.get("present"):
        return ("Ended connection %d. Its open transaction was rolled back and its locks were released."
                % cid)
    if after.get("command") == "Killed" or after.get("trx_state") == "ROLLING BACK":
        return ("Connection %d is ending: it is rolling back its transaction%s and keeps its locks until "
                "the rollback finishes." % (cid, " (%s changed)" % _rows(rows) if rows else ""))
    return "Sent KILL %d. The connection is still listed; refresh in a moment." % cid


def _after_session(after):
    """An after-view in the shape _running() reads."""
    return {"command": after.get("command"), "info": after.get("statement")}


def _confirmed(expect, connection_id, mode):
    """The identity the person confirmed, as recorded in the audit trail."""
    target = {"connection_id": connection_id, "mode": mode}
    for key in ("user", "host", "trx_started", "ps_thread_id"):
        if (expect or {}).get(key) is not None:
            target[key] = expect[key]
    return target


def refused_result(connection_id, mode, message, status="refused", engine=None, server_version=None):
    """A kill-1.0 result for a KILL myflames did not send (read-only connection, a refusal)."""
    return {"schema_version": KILL_SCHEMA_VERSION, "engine": engine, "server_version": server_version,
            "connection_id": connection_id, "mode": mode, "statement": None, "status": status,
            "message": message, "verified": [], "target": None, "after": None}


def kill_session(conn, connection_id, mode="connection", expect=None, settle_s=0.3, audit=None,
                 decision=None, confirmed=False, connection=None, before_send=None):
    """End one session: ``KILL CONNECTION n`` (default) or ``KILL QUERY n``.

    Only for an explicit, confirmed request. *expect* is the session the
    person confirmed (``user``, ``host``, and when known ``trx_started`` and
    ``ps_thread_id``), plus ``connection_identity`` from dbops and
    ``server_identity`` (MySQL's server_uuid; MariaDB's server_id, hostname
    and version, all text). ``confirmed`` must be literally True. Policy and
    identity gates also apply to direct callers. The session is re-read; myflames refuses when it
    is gone, is now a different session (an id reused, or COM_CHANGE_USER),
    its transaction changed, it is this session or a myflames statement, it
    is a server thread, it holds a prepared XA transaction (MySQL: ending the
    session releases nothing), it is already rolling back, or KILL QUERY would
    not help (an idle session). A "Killed" command alone does not refuse KILL
    CONNECTION: MariaDB also shows it after KILL QUERY on an idle session, and
    a second KILL is harmless. Connection ids are unique among live sessions,
    and neither server reuses a freed id until its 32-bit counter wraps around
    (after about 4.29 billion connections), so the separate session that sends
    the KILL cannot have the target's id. Returns the ``kill-1.0`` result; the
    server's own refusal is status ``denied``, not an exception.

    *audit* (a :class:`myflames.dbops.AuditTrail`) records every attempt:
    a refusal as ``refused``; otherwise a ``pending`` record is written
    before the KILL is sent (nothing is sent when that fails) and finished
    as ``ok``, ``error`` or ``unknown`` after an interrupted send.
    ``audit_id`` is added to the result.
    """
    from . import dbops
    bound_expect = expect
    expect = _check_kill_args(connection_id, mode, expect)
    statement = ("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % connection_id
    safety = (audit.safety if audit is not None else None) or dbops.connection_safety(connection=connection)
    if connection is None:
        connection = {key: getattr(conn, key, None) for key in
                      ("host", "port", "user", "ssl_mode", "ssl_ca")}
        connection["profile_id"] = safety.get("profile_id")
    policy = dbops.decide(dbops.classify(statement), safety["type"], safety["read_only"],
                          safety.get("profile_name"))
    # A supplied decision may strengthen the policy, never turn KILL into an
    # allowed read or bypass the saved connection's read-only restriction.
    blocked = dbops.gate(policy, confirmed=confirmed) or (dbops.gate(decision, confirmed=confirmed)
                                                        if decision is not None else None)
    seen = _confirmed(expect, connection_id, mode)
    for key in ("connection_identity", "server_identity"):
        seen[key] = bound_expect.get(key)

    def refuse(message, outcome="identity", status="refused"):
        result = refused_result(connection_id, mode, message, status=status)
        if audit is not None:
            result["audit_id"] = audit.outcome([KILL_TAG + statement], "refused", outcome=outcome,
                                               message=message, target=seen)
        return result

    if blocked:
        return refuse(blocked[1], "read_only" if blocked[0] == "refused" else blocked[0], blocked[0])
    if bound_expect.get("connection_identity") != dbops.connection_identity(connection, safety):
        return refuse("The connection or its write policy changed after confirmation; nothing was sent.")
    engine, version, _ver, ps_on = _server(conn)
    if audit is not None:
        audit.server(engine, version)
    keys = ("server_id", "hostname", "version") if engine == "mariadb" else ("server_uuid",)
    rows = _q(conn, "SELECT " + ", ".join("@@" + key for key in keys))
    identity = dict(zip(keys, rows[0])) if rows and len(rows[0]) == len(keys) else {}
    if not all(identity.get(key) for key in keys) or bound_expect.get("server_identity") != identity:
        return refuse("The server identity is missing or changed after confirmation; nothing was sent.")
    result = {"schema_version": KILL_SCHEMA_VERSION, "engine": engine, "server_version": version,
              "connection_id": connection_id, "mode": mode, "statement": None, "status": None,
              "message": None, "verified": [], "target": None, "after": None}
    confirmed_target = seen
    target, own = read_target(conn, engine, ps_on, connection_id)
    result["target"] = target
    problem = _kill_problem(target, own, connection_id, mode, expect)
    if problem:
        result["status"], result["message"] = problem
        if audit is not None:
            result["audit_id"] = audit.outcome([KILL_TAG + statement], "refused", outcome=problem[0],
                                               message=problem[1], target=confirmed_target)
        return result
    verified = ["connection", "server identity", "user", "host", "not this session"]
    if expect["ps_thread_id"] is not None and target.get("ps_thread_id") is not None:
        verified.append("server thread")
    if expect["trx_started"] is not None and target.get("trx_checked"):
        verified.append("transaction")
    result["verified"] = verified
    result["statement"] = statement
    if before_send is not None:
        before_send()
    if audit is not None:
        try:
            result["audit_id"] = audit.attempt([KILL_TAG + statement], target=confirmed_target, verified=verified)
        except Exception as exc:
            raise ValueError("Could not write the audit record (%s), so nothing was sent." % exc)
    from .dbops import WRITE_CLIENT_FLAGS, may_have_run
    try:
        # --binary-mode: the client runs no backslash command of its own.
        conn.run(KILL_TAG + statement, extra_flags=list(WRITE_CLIENT_FLAGS))
    except KeyboardInterrupt as exc:
        if audit is not None:
            audit.fail(exc)
        raise
    except ConnectorError as exc:
        try:
            result["status"], result["message"] = _kill_error(engine, exc, connection_id)
        except ValueError as failure:
            unknown = may_have_run(exc)
            message = str(failure) + (". The KILL may have reached the server, so its outcome is unknown; check the "
                                      "session list." if unknown else "")
            if audit is not None:
                audit.complete("unknown" if unknown else "error", outcome="timeout" if unknown else "error",
                               error=str(exc), message=message)
            raise ValueError(message)
        if audit is not None:
            audit.complete("error", outcome=result["status"], error=str(exc), message=result["message"])
        return result
    result["status"] = "killed"
    try:
        if settle_s:
            time.sleep(settle_s)
        try:
            after, _ = read_target(conn, engine, ps_on, connection_id)
            result["after"] = _after_view(after)
        except (ConnectorError, ValueError):
            result["after"] = None
    except KeyboardInterrupt:
        # The KILL was sent and accepted; only the check afterwards was cut short.
        if audit is not None:
            audit.complete("ok", outcome="killed", message="Sent %s; interrupted before checking the session again."
                           % statement)
        raise
    result["message"] = _kill_message(mode, connection_id, target, result["after"] or {"present": True})
    if audit is not None:
        audit.complete("ok", outcome="killed", message=result["message"], after=result["after"])
    return result


def blocked_by(report, connection_id):
    """Connection ids waiting on *connection_id*: below it in the blocking tree, and DDL
    statements that list it as a blocker. Returns (ids, the DDL entries)."""
    ids = []

    def walk(node, under):
        mine = under or node.get("connection_id") == connection_id
        for child in node.get("blocked") or []:
            cid = child.get("connection_id")
            if mine and cid and cid != connection_id and cid not in ids:
                ids.append(cid)
            if not child.get("repeat"):
                walk(child, mine)
    for root in report.get("blocking_tree") or []:
        walk(root, False)
    ddls = [d for d in report.get("ddl_waits") or []
            if any(b.get("connection_id") == connection_id for b in d["blockers"])]
    for d in ddls:
        if d["connection_id"] not in ids:
            ids.append(d["connection_id"])
    return ids, ddls


def kill_outcome(report, waiting_ids, ddl_kinds=None):
    """After a KILL: for each session that waited, is it still waiting, and on whom?

    *ddl_kinds* maps the waiting DDL statements' connection ids (from the
    report read before the KILL) to their kind, such as ALTER TABLE.
    """
    ddl_kinds = ddl_kinds or {}
    parents = {}

    def walk(node, parent):
        cid = node.get("connection_id")
        if parent and cid:
            parents.setdefault(cid, set()).add(parent)
        if not node.get("repeat"):
            for child in node.get("blocked") or []:
                walk(child, cid)
    for root in report.get("blocking_tree") or []:
        walk(root, None)
    ddl = {d["connection_id"]: d for d in report.get("ddl_waits") or []}
    out = []
    for cid in waiting_ids:
        blockers = set(parents.get(cid, ()))
        entry = ddl.get(cid)
        if entry:
            blockers |= {b["connection_id"] for b in entry["blockers"] if b.get("connection_id")}
        out.append({"connection_id": cid, "waiting": cid in parents or entry is not None,
                    "blocked_by": sorted(blockers), "ddl": entry["kind"] if entry else ddl_kinds.get(cid),
                    "precision": entry["precision"] if entry else None})
    return out


def _find_view(report, cid):
    """The richest view of a connection in a report (DDL blocker, transaction, tree node)."""
    for d in report.get("ddl_waits") or []:
        for b in d["blockers"]:
            if b.get("connection_id") == cid:
                return b
    for t in report.get("transactions") or []:
        if t.get("connection_id") == cid:
            return t
    return {}


def describe_target(report, target, cid, mode):
    """Lines that describe a session before ending it (shared by the CLI prompt)."""
    view = _find_view(report, cid)
    trx = (target or {}).get("trx")
    lines = ["Connection %d: %s@%s%s" % (cid, target.get("user") or "?", target.get("host") or "?",
                                         ", db %s" % target["db"] if target.get("db") else "")]
    if _running(target):
        lines.append("  running for %s: %s" % (_secs(target.get("time_s")), _one_line(target.get("info"), 100)))
    else:
        lines.append("  %s for %s%s" % (target.get("command") or "?", _secs(target.get("time_s")),
                                       " (idle in transaction)" if trx and target.get("command") == "Sleep" else ""))
    if trx:
        rows = trx.get("rows_modified")
        lines.append("  transaction open %s, %s, %s locked, %s changed" % (
            _secs(trx.get("age_s")), trx.get("state") or "?", _rows(trx.get("rows_locked")), _rows(rows)))
        if rows is not None and rows >= LARGE_ROLLBACK_ROWS and mode == "connection":
            lines.append("  WARNING: it changed %s rows. Rolling them back can take a long time, and it "
                         "keeps its locks until the rollback finishes." % "{:,}".format(rows))
    elif target.get("trx_checked"):
        lines.append("  no open InnoDB transaction")
    else:
        lines.append("  transaction unknown (reading INNODB_TRX needs PROCESS)")
    if not target.get("info") and view.get("last_statement"):
        lines.append("  last statement: %s" % _one_line(view["last_statement"], 100))
    ids, ddls = blocked_by(report, cid)
    if ddls:
        for d in ddls:
            lines.append("  %s DDL: connection %d, %s" % (
                "may be blocking" if d["precision"] == "likely" else "blocking", d["connection_id"],
                _one_line(d["statement"], 80)))
    others = [i for i in ids if i not in {d["connection_id"] for d in ddls}]
    if others:
        lines.append("  blocking connection%s %s" % ("s" if len(others) > 1 else "",
                                                      ", ".join(str(i) for i in others)))
    if not ids:
        lines.append("  not blocking any session right now")
    if mode == "query":
        lines.append("  KILL QUERY %d stops only the running statement; its transaction stays open with "
                     "its locks unless it runs in autocommit mode." % cid)
    else:
        lines.append("  KILL CONNECTION %d closes the connection and rolls back its open transaction." % cid)
    return lines


def kill_cli(conn, connection_id, mode="connection", assume_yes=False, quiet=False, stdin=None, stderr=None,
             audit=None, policy_text=None):
    """``myflames locks --kill ID``: show the session, confirm, end it, show the outcome.

    Returns ``(exit code, kill-1.0 result)``: 0 ended, 1 declined or refused
    (by myflames or by the server). Bad input and connection failures raise,
    and the caller maps them to exit 2. Without a terminal it refuses unless
    *assume_yes*. *audit* records every attempt, declined and refused ones
    included; *policy_text* (the connection type's confirmation rule) is shown
    with the description.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stderr = stderr if stderr is not None else sys.stderr
    from . import dbops
    safety = (audit.safety if audit is not None else None) or dbops.connection_safety()
    connection = {key: getattr(conn, key, None) for key in ("host", "port", "user", "ssl_mode", "ssl_ca")}
    connection["profile_id"] = safety.get("profile_id")
    statement = KILL_TAG + ("KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d") % connection_id
    decision = dbops.decide(dbops.classify(statement), safety["type"], safety["read_only"],
                            safety.get("profile_name"))
    # Confirmation follows the target description; read-only is enforced here
    # too, for callers other than workspace_cli._kill_command.
    blocked = dbops.gate(decision, confirmed=assume_yes)
    if blocked and blocked[0] == "refused":
        result = refused_result(connection_id, mode, blocked[1])
        if audit is not None:
            result["audit_id"] = audit.outcome([statement], "refused", outcome="read_only", message=blocked[1],
                                               target={"connection_id": connection_id, "mode": mode})
        return 1, result
    report = lock_report(conn, waits_only=True, identity=True)
    engine, ps_on = report["engine"], report["capabilities"]["performance_schema"]
    if audit is not None:
        audit.server(engine, report["server_version"])
    target, own = read_target(conn, engine, ps_on, connection_id)
    result = {"schema_version": KILL_SCHEMA_VERSION, "engine": engine, "server_version": report["server_version"],
              "connection_id": connection_id, "mode": mode, "statement": None, "status": None, "message": None,
              "verified": [], "target": target, "after": None}
    seen = _confirmed({"user": (target or {}).get("user"), "host": (target or {}).get("host"),
                       "ps_thread_id": (target or {}).get("ps_thread_id"),
                       "trx_started": ((target or {}).get("trx") or {}).get("started")}, connection_id, mode)

    def unsent(status, outcome, code=1):
        if audit is not None:
            result["audit_id"] = audit.outcome([statement], status, outcome=outcome, message=result["message"],
                                               target=seen)
        return code, result
    problem = _kill_problem(target, own, connection_id, mode, None)
    if problem:
        result["status"], result["message"] = problem
        return unsent("refused", problem[0])
    if kill_allowed(report["kill_privilege"], target.get("user")) is False:
        notes = _privilege_notes(engine, report["kill_privilege"])
        result["status"] = "refused"
        result["message"] = ("Not sent: connection %d belongs to user %s. %s" % (
            connection_id, target.get("user"), notes[0] if notes else ""))
        return unsent("refused", "privilege")
    if not target.get("host"):
        result["status"] = "refused"
        result["message"] = ("Connection %d has no host in the process list, so myflames cannot confirm it. "
                             "Nothing was sent." % connection_id)
        return unsent("refused", "identity")
    seen["connection_identity"] = dbops.connection_identity(connection, safety)
    keys = ("server_id", "hostname", "version") if engine == "mariadb" else ("server_uuid",)
    rows = _q(conn, "SELECT " + ", ".join("@@" + key for key in keys))
    seen["server_identity"] = dict(zip(keys, rows[0])) if rows and len(rows[0]) == len(keys) else {}
    if not all(seen["server_identity"].get(key) for key in keys):
        result["status"], result["message"] = "refused", "Could not read the server identity; nothing was sent."
        return unsent("refused", "identity")
    lines = describe_target(report, target, connection_id, mode)
    if policy_text:
        lines.append("  " + policy_text)
    verb = "KILL QUERY %d" if mode == "query" else "KILL CONNECTION %d"
    problem = dbops.cli_confirm(
        "\n".join(lines), "End it with %s? [y/N] " % (verb % connection_id),
        assume_yes=assume_yes, quiet=quiet, stdin=stdin, stderr=stderr,
        passwords=(audit.password,) if audit is not None else (),
        no_terminal_message="Not sent: stdin is not a terminal, so myflames cannot ask you to confirm. "
                            "Re-run with --yes to end connection %d." % connection_id,
        decline_message="Nothing was ended.")
    if problem:
        result["status"], result["message"] = problem[0], problem[2]
        return unsent(problem[0], problem[1])
    waiting, _ddls = blocked_by(report, connection_id)
    expect = dict(seen, user=target["user"], host=target["host"], ps_thread_id=target.get("ps_thread_id"),
                  trx_started=(target.get("trx") or {}).get("started"))
    try:
        result = kill_session(conn, connection_id, mode, expect, audit=audit, decision=decision, confirmed=True,
                              connection=connection)
    except KeyboardInterrupt:
        # kill_session has already finished the audit, so the outer CLI's
        # pending-record check cannot detect this uncertain send anymore.
        raise ValueError("Cancelled. The KILL may already have reached the server; check with: "
                         "myflames sessions --all") from None
    if result["status"] != "killed":
        return 1, result
    if waiting:
        kinds = {d["connection_id"]: d["kind"] for d in report.get("ddl_waits") or []}
        result["outcome"] = kill_outcome(lock_report(conn, waits_only=True), waiting, kinds)
    return 0, result


def format_kill(result):
    """Human text for a kill-1.0 result."""
    out = [result["message"] or result["status"]]
    for item in result.get("outcome") or []:
        what = ("The %s on connection %d" % (item["ddl"], item["connection_id"]) if item["ddl"]
                else "Connection %d" % item["connection_id"])
        if not item["waiting"]:
            out.append("%s no longer waits%s." % (what, " for a lock; it is running or done" if item["ddl"] else ""))
        elif item["blocked_by"]:
            out.append("%s still waits, %s connection%s %s." % (
                what, "probably for" if item["precision"] == "likely" else "now for",
                "s" if len(item["blocked_by"]) > 1 else "", ", ".join(str(i) for i in item["blocked_by"])))
        else:
            out.append("%s still waits." % what)
    return "\n".join(out) + "\n"
