"""Performance Schema instruments: what is on, what each part gives myflames
and the DBA, what it costs, and the statements that change it.

:func:`instruments_report` returns the ``instruments-1.0`` envelope shared by
``myflames instruments`` and the workspace's Server > Instruments tab. It
reads ``setup_instruments``, ``setup_consumers``, ``setup_actors``,
``setup_objects``, a few variables and Performance Schema's own memory, and
nothing else. Every UPDATE, INSERT or DELETE in the report is text for the
user to run; myflames never runs one.

:data:`FEATURES` maps each myflames view to the Performance Schema tables it
reads and the instruments and consumers that fill them. The tests scan the
view modules and fail when one starts reading a table or naming an
instrument that is not mapped here.

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"; MySQL 8.4.0 and MariaDB 10.11.16, 11.4.10 and 11.8.6 tags have the
same defaults) and on live MySQL 8.4, 9.7 and 26.7 and MariaDB 11.4 and 13.0:

* ``performance_schema`` is read-only at runtime; ON by default on MySQL
  (M/sql/sys_vars.cc:570-574), OFF on MariaDB (Ma/sql/sys_vars.cc:89-92).
  With it off the setup tables exist and are empty.
* Instrument defaults are decided by class. Only two flags set by the
  registering code change them: the progress flag on stages (both engines)
  and, in MySQL, the disabled and untimed flags on statements. MySQL (M/storage/perfschema/pfs_instr_class.cc): table I/O,
  table lock, idle, metadata lock and transaction on and timed (327-372, the
  defaults of init_instr_class at 1206-1207); ``error`` on, never timed
  (361-363); mutex, rwlock, cond and socket off (1419, 1489, 1535, 1941);
  file on (1746); stages on only with the progress property (1795-1800);
  statements on unless flagged disabled (1840-1860), which only MySQL's
  stored-program control instructions are (M/sql/sp_instr.cc); memory on,
  never timed (2002). MariaDB (Ma/storage/perfschema/pfs_instr_class.cc):
  metadata lock and transaction off (233-245), synch and socket off (1089,
  1156, 1194, 1514), file on (1338), progress stages only (1384-1391), every
  statement on (1430), memory off (1566); it has no ``error`` instrument and
  no PROPERTIES column.
* Consumer defaults: MySQL M/sql/sys_vars.cc:622-733 (statements and
  transactions current and history on); MariaDB Ma/sql/mysqld.cc:846-920
  (only global_instrumentation, thread_instrumentation, statements_digest).
* History tables are written only for events the ``*_current`` consumer
  records (STATE_FLAG_EVENT: M/storage/perfschema/pfs.cc:6464-6476,
  7073-7078; Ma/storage/perfschema/pfs.cc:5000-5020, 5527), so a history
  consumer needs its current consumer and global_instrumentation. Statement,
  transaction and most wait events also need thread_instrumentation; stage
  events (M/pfs.cc:5914-5946, Ma/pfs.cc:4816-4846) and file open waits are
  recorded without it.
* UPDATE of setup_consumers re-derives every thread at once
  (M/storage/perfschema/table_setup_consumers.cc:268-274,
  pfs_instr.cc:2535-2541). UPDATE of setup_instruments re-applies mutex,
  rwlock, cond and socket instruments to existing instances at once
  (pfs_instr.cc:2425-2533) and table instruments at the next statement that
  uses each open table (pfs.cc:2860-2919); a file is recorded only if it was
  opened while its instrument was on (pfs.cc:4369-4411); metadata locks are
  recorded only when taken while the instrument is on (pfs.cc:8815-8834),
  whatever the session's INSTRUMENTED flag.
  Memory instruments count allocations made while on, and always count the
  free of an instrumented allocation (pfs.cc:8735-8750). Built-in
  ``memory/performance_schema/*`` instruments cannot be turned off, and memory
  and error instruments have no timing (table_setup_instruments.cc:320-360,
  464-478; pfs_instr_class.h:337-345).
* setup_actors is applied when a session's account is set (connect, change
  user: M/storage/perfschema/pfs.cc:3262-3290), so changes reach new
  sessions only; setup_objects changes re-derive table shares at once
  (table_setup_objects.cc:84-94).
* Startup options: ``performance-schema-instrument='pattern=value'`` (ON,
  OFF, COUNTED, TRUE, FALSE, YES, NO, 1, 0; M/storage/perfschema/
  pfs_server.cc:496-508, Ma/storage/perfschema/pfs_server.cc:340-360), the
  longest matching pattern wins regardless of order (M/pfs_instr_class.cc:
  1225-1250, Ma/:1000-1020); ``performance-schema-consumer-<name>=ON|OFF``.
* History buffers are sized by the read-only ``*_history_size`` and
  ``*_history_long_size`` variables whether the consumer is on or not
  (M/storage/perfschema/pfs_events_statements.cc:72-110,
  pfs_buffer_container.cc:485-499).
* setup_instruments and setup_consumers accept UPDATE, setup_actors and
  setup_objects INSERT, UPDATE and DELETE, each checked against ordinary
  table grants (M/storage/perfschema/pfs_engine_table.cc:1152-1210).
* Default setup_actors row ('%', '%', '%', YES, YES); default setup_objects
  disable mysql, performance_schema and information_schema for every object
  type and enable everything else (M/storage/perfschema/pfs_defaults.cc:
  80-140, Ma/storage/perfschema/pfs_defaults.cc:55-100).
"""
from .connector import ConnectorError
from .perf_common import (Reader, _bytes_text, _count_text, _duration_text, _number, base_report, denied,
                          lost_note, preflight, reason, table_text)

SCHEMA_VERSION = "instruments-1.0"
INSTRUMENTS_TAG = "/*myflames:instruments*/ "

SIZE_VARIABLES = (
    "performance_schema_events_statements_history_size", "performance_schema_events_statements_history_long_size",
    "performance_schema_events_stages_history_size", "performance_schema_events_stages_history_long_size",
    "performance_schema_events_transactions_history_size",
    "performance_schema_events_transactions_history_long_size",
    "performance_schema_events_waits_history_size", "performance_schema_events_waits_history_long_size",
    "performance_schema_digests_size", "performance_schema_max_sql_text_length",
    "performance_schema_max_digest_length",
)

def sp_control(name):
    """MySQL's stored-program control instructions (statement/sp/set, jump,
    cursor operations and the rest): flagged disabled and untimed
    (M/sql/sp_instr.cc). Only statement/sp/stmt, SQL run inside a program, is
    on by default."""
    return name.startswith("statement/sp/") and name != "statement/sp/stmt"


#: MariaDB's stages with progress information, on by default (Ma/sql/mysqld.cc:
#: 9792, Ma/storage/innobase/srv/srv0srv.cc:477-506). MySQL marks its own in
#: the PROPERTIES column.
MARIADB_PROGRESS_STAGES = (
    "stage/sql/copy to tmp table", "stage/innodb/alter table (end)", "stage/innodb/alter table (insert)",
    "stage/innodb/alter table (log apply index)", "stage/innodb/alter table (log apply table)",
    "stage/innodb/alter table (merge sort)", "stage/innodb/alter table (read PK and internal sort)",
    "stage/innodb/buffer pool load")
BUILTIN_MEMORY = "memory/performance_schema/"

# ---- SQL fragments shared by the groups and the presets ---------------------------------------

_SET_ON = "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', TIMED = 'YES' WHERE "
_SET_OFF = "UPDATE performance_schema.setup_instruments SET ENABLED = 'NO', TIMED = 'NO' WHERE "
_CONSUMERS_ON = "UPDATE performance_schema.setup_consumers SET ENABLED = 'YES' WHERE "
_SET_ON_ML = _SET_ON.replace(" WHERE ", "\nWHERE ")
_SET_OFF_ML = _SET_OFF.replace(" WHERE ", "\nWHERE ")
_CONSUMERS_ON_ML = _CONSUMERS_ON.replace(" WHERE ", "\nWHERE ")
_MYSQL_STATEMENTS = ("NAME LIKE 'statement/%' AND (NAME NOT LIKE 'statement/sp/%' "
                     "OR NAME = 'statement/sp/stmt')")


def _quote_list(names):
    return ", ".join("'%s'" % n.replace("'", "''") for n in names)


def statements_where(engine):
    """The statement instruments the engine turns on by default."""
    return _MYSQL_STATEMENTS if engine == "mysql" else "NAME LIKE 'statement/%'"

# ---- defaults (pure) -------------------------------------------------------------------------


def default_state(engine, name, properties=""):
    """``(enabled, timed)`` the engine starts *name* with, ``timed`` None when
    timing does not apply (memory, error); None for an unknown class."""
    mysql = engine == "mysql"
    if name.startswith(("wait/synch/", "wait/io/socket/")):
        return False, False
    if name.startswith("wait/io/file/") or name in ("wait/io/table/sql/handler", "wait/lock/table/sql/handler",
                                                   "idle"):
        return True, True
    if name in ("wait/lock/metadata/sql/mdl", "transaction"):
        return (True, True) if mysql else (False, False)
    if name == "error":
        return True, None
    if name.startswith("statement/"):
        if mysql and sp_control(name):
            return False, False
        return True, True
    if name.startswith("stage/"):
        progress = ("progress" in (properties or "").split(",")) if mysql else name in MARIADB_PROGRESS_STAGES
        return (True, True) if progress else (False, False)
    if name.startswith(BUILTIN_MEMORY):
        return True, None
    if name.startswith("memory/"):
        return mysql, None
    return None


CONSUMER_ORDER = (
    "global_instrumentation", "thread_instrumentation", "statements_digest",
    "events_statements_current", "events_statements_history", "events_statements_history_long",
    "events_statements_cpu",
    "events_transactions_current", "events_transactions_history", "events_transactions_history_long",
    "events_stages_current", "events_stages_history", "events_stages_history_long",
    "events_waits_current", "events_waits_history", "events_waits_history_long",
)
_MYSQL_CONSUMERS_ON = ("global_instrumentation", "thread_instrumentation", "statements_digest",
                       "events_statements_current", "events_statements_history",
                       "events_transactions_current", "events_transactions_history")
_MARIADB_CONSUMERS_ON = ("global_instrumentation", "thread_instrumentation", "statements_digest")


def default_consumer(engine, name):
    return name in (_MYSQL_CONSUMERS_ON if engine == "mysql" else _MARIADB_CONSUMERS_ON)


def consumer_parent(name):
    """The consumer *name* is checked under (None for global_instrumentation)."""
    if name == "global_instrumentation":
        return None
    if name in ("thread_instrumentation", "statements_digest", "events_statements_cpu",
                "events_stages_current"):
        # Stage events check only global_instrumentation (and, while
        # thread_instrumentation is on, the session's INSTRUMENTED flag).
        return "global_instrumentation"
    if name.endswith("_current"):
        return "thread_instrumentation"
    for kind in ("statements", "transactions", "stages", "waits"):
        if name.startswith("events_%s_history" % kind):
            return "events_%s_current" % kind
    return "global_instrumentation"


def consumer_chain(name):
    chain = []
    parent = consumer_parent(name)
    while parent:
        chain.append(parent)
        parent = consumer_parent(parent)
    return chain


def _consumer_option(name):
    return "performance-schema-consumer-%s" % name.replace("_", "-")

# ---- what myflames needs (kept in sync with the view modules by the tests) -------------------


#: Each myflames view that reads Performance Schema: the tables it reads (by
#: the source modules listed) and what fills them. ``needs`` must be on for
#: the view to work; ``optional`` adds to it.
FEATURES = (
    {"key": "top", "label": "Top statements", "view": "top", "modules": ("discover",),
     "tables": ("events_statements_summary_by_digest",),
     "needs": {"consumers": ("global_instrumentation", "statements_digest"), "instruments": ("statement/%",)},
     "explain": "The heaviest normalized statements since startup, from statement digests."},
    {"key": "capture", "label": "Capture statistics", "view": "editor", "modules": ("live",),
     "tables": ("events_statements_history", "threads"),
     "needs": {"consumers": ("global_instrumentation", "thread_instrumentation", "events_statements_current",
                             "events_statements_history"), "instruments": ("statement/%",)},
     "explain": ("Rows examined and sent, temporary tables, sort merge passes and the no-index flag saved with "
                 "each Query editor capture.")},
    {"key": "locks_mdl", "label": "Locks: metadata locks", "view": "locks", "modules": ("locks",),
     "tables": ("metadata_locks",),
     "needs": {"consumers": ("global_instrumentation",), "instruments": ("wait/lock/metadata/sql/mdl",)},
     "explain": "Who holds a table's metadata lock while ALTER TABLE or another statement waits for it."},
    {"key": "locks_last", "label": "Locks: an idle blocker's last statement", "view": "locks", "modules": ("locks",),
     "tables": ("events_statements_current", "threads"),
     "needs": {"consumers": ("global_instrumentation", "thread_instrumentation", "events_statements_current"),
               "instruments": ("statement/%",)},
     "explain": "What a session holding locks ran last, when it now sits idle in an open transaction."},
    {"key": "locks_rows", "label": "Locks: InnoDB row locks", "view": "locks", "modules": ("locks",),
     "tables": ("data_locks", "data_lock_waits"), "needs": {}, "engine": "mysql",
     "explain": ("MySQL lists row locks in performance_schema.data_locks, which needs Performance Schema on but "
                 "no instrument. MariaDB reads them from information_schema, with or without it.")},
    {"key": "waits", "label": "Waits", "view": "waits", "modules": ("waits", "perf_common"),
     "tables": ("events_waits_summary_global_by_event_name",),
     "needs": {"consumers": ("global_instrumentation",),
               "instruments": ("wait/io/file/%", "wait/io/table/sql/handler", "wait/lock/table/sql/handler")},
     "optional": {"instruments": ("wait/lock/metadata/sql/mdl", "wait/synch/%", "wait/io/socket/%")},
     "explain": "Where server threads spent their waiting time, by wait event and class."},
    {"key": "indexes", "label": "Indexes: unused indexes and full scans", "view": "indexes",
     "modules": ("indexes", "perf_common"), "tables": ("table_io_waits_summary_by_index_usage",),
     "needs": {"consumers": ("global_instrumentation",), "instruments": ("wait/io/table/sql/handler",)},
     "explain": "Row operations per index since startup: zero means unused; rows read without an index mean scans."},
    {"key": "io", "label": "I/O: per table and per file", "view": "io", "modules": ("io_stats", "perf_common"),
     "tables": ("table_io_waits_summary_by_table", "file_summary_by_instance", "file_summary_by_event_name"),
     "needs": {"consumers": ("global_instrumentation",),
               "instruments": ("wait/io/table/sql/handler", "wait/io/file/%")},
     "explain": "Row operations and time per table, and bytes and time per file."},
    {"key": "replication_receiver", "label": "Replication: receiver reconnect guard", "view": "replication",
     "modules": ("replication",), "tables": ("events_stages_summary_by_thread_by_event_name", "threads"),
     "needs": {"consumers": ("global_instrumentation", "thread_instrumentation"),
               "instruments": ("stage/sql/Checking source version", "stage/sql/Checking master version")},
     "instrument_engines": {"stage/sql/Checking source version": "mysql",
                            "stage/sql/Checking master version": "mariadb"},
     "timed": False,
     # Optional guard: shown in Instruments, never counted as a view that is not ready.
     "advisory": True,
     "explain": ("Detect receiver reconnects during Skip until stopped using completed version-check stage counts. "
                 "Off by default; loops can run without this guard and report that reconnects are not detected. "
                 "Receiver threads must also be instrumented.")},
)
FEATURE_LABELS = {f["key"]: f["label"] for f in FEATURES}
#: thread_instrumentation decides which sessions are recorded (with
#: setup_actors), but turning it off does not empty a summary table, so
#: summary views name it only to explain their data. The event table views
#: (capture, locks_last) list it in needs.
FILTER_CONSUMERS = ("thread_instrumentation",)

#: Every performance_schema table a view module reads, and the features it
#: feeds; () for tables that need no instrument or consumer.
TABLES = {
    "events_statements_summary_by_digest": ("top",),
    "events_statements_history": ("capture",),
    "events_statements_current": ("locks_last",),
    "events_waits_summary_global_by_event_name": ("waits",),
    "events_stages_summary_by_thread_by_event_name": ("replication_receiver",),
    "table_io_waits_summary_by_index_usage": ("indexes",),
    "table_io_waits_summary_by_table": ("io",),
    "file_summary_by_instance": ("io",),
    "file_summary_by_event_name": ("io",),
    "metadata_locks": ("locks_mdl",),
    "data_locks": ("locks_rows",),
    "data_lock_waits": ("locks_rows",),
    "threads": (), "processlist": (), "global_status": (), "global_variables": (), "variables_info": (),
    "setup_instruments": (), "setup_consumers": (), "setup_actors": (), "setup_objects": (),
    "memory_summary_global_by_event_name": (),
    # Replication (replication.py): filled whenever Performance Schema is on, no instrument or consumer.
    "replication_connection_configuration": (), "replication_connection_status": (),
    "replication_applier_configuration": (), "replication_applier_status_by_worker": (),
    # Server variables (variables.py): readable with Performance Schema on or off, no instrument or consumer.
    "persisted_variables": (), "variables_metadata": (),
}


def like(name, pattern):
    """SQL LIKE with only the trailing or exact forms used here."""
    if pattern.endswith("%"):
        return name.startswith(pattern[:-1])
    return name == pattern


def patterns_overlap(a, b):
    pa, pb = a.rstrip("%"), b.rstrip("%")
    if a.endswith("%") and b.endswith("%"):
        return pa.startswith(pb) or pb.startswith(pa)
    if a.endswith("%"):
        return pb.startswith(pa)
    if b.endswith("%"):
        return pa.startswith(pb)
    return a == b

# ---- groups and consumers: what you gain, lose and pay ----------------------------------------


GROUPS = (
    {"key": "statement", "label": "Statements", "match": "statement/%",
     "gain": ("Time, rows examined and sent, temporary tables, sorts and errors for every statement, summarized by "
              "digest, user, host, account and stored program; with the statement consumers, the current and "
              "recent statements of each session."),
     "lose": ("No new statement events: Top statements stops updating (rows already collected stay until a "
              "restart or a truncate), captures lose their Performance Schema statistics, and sys views built "
              "on statements stop updating."),
     "cost": ("low", "One event per statement (and per statement inside a stored program), timed with two timer "
                     "reads. On by default on both engines; worth keeping on in production.")},
    {"key": "stage", "label": "Stages", "match": "stage/%",
     "gain": ("Where time goes inside a statement (opening tables, optimizing, executing, waiting for a table "
              "metadata lock) and progress of long operations such as ALTER TABLE and buffer pool load, in "
              "events_stages_current (WORK_COMPLETED and WORK_ESTIMATED; needs the events_stages_current "
              "consumer)."),
     "lose": "No stage breakdown and no progress for ALTER TABLE.",
     "cost": ("moderate", "Each statement passes through several stages, and every enabled stage is a timed event. "
                          "The defaults turn on only the progress stages, which fire during long operations "
                          "(ALTER TABLE, buffer pool load, clone) and, on a MySQL replica, once per applied batch "
                          "of row changes; turn the rest on for an investigation rather than permanently on a busy "
                          "server.")},
    {"key": "wait/io/file", "label": "File I/O", "match": "wait/io/file/%",
     "gain": ("Bytes, operations and time per file and per file kind (tablespaces, redo and binary logs, "
              "temporary files) in file_summary_by_instance and file_summary_by_event_name."),
     "lose": ("I/O loses its per-file view and Waits its file waits; server-wide InnoDB counters (Health, I/O) "
              "remain."),
     "cost": ("low", "One timed event per file operation, which is far slower than the timer reads. On by "
                     "default on both engines.")},
    {"key": "wait/io/table", "label": "Table I/O", "match": "wait/io/table/sql/handler",
     "gain": ("Row operations (fetch, insert, update, delete) and their time per table and per index: "
              "table_io_waits_summary_by_table and _by_index_usage, the source of sys.schema_unused_indexes and "
              "schema_tables_with_full_table_scans."),
     "lose": ("Indexes cannot find unused indexes or full scans (redundant indexes still work) and I/O loses its "
              "per-table list."),
     "cost": ("moderate", "About one timed event per row operation through the storage engine interface (some "
                          "scans are timed as one batch of rows). On by default on both engines; setup_objects "
                          "limits it by schema and table.")},
    {"key": "wait/lock/table", "label": "Table locks", "match": "wait/lock/table/sql/handler",
     "gain": ("Table lock requests and waits per table (table_lock_waits_summary_by_table): LOCK TABLES, MyISAM "
              "and MEMORY table locks, and the per-statement external lock call."),
     "lose": "Table lock waits disappear from Waits and from the per-table lock summary.",
     "cost": ("low", "One event per table lock per statement, not per row. On by default on both engines.")},
    {"key": "wait/lock/metadata", "label": "Metadata locks", "match": "wait/lock/metadata/sql/mdl",
     "gain": ("Granted and pending metadata locks in performance_schema.metadata_locks: which session holds the "
              "metadata lock ALTER TABLE waits for (often an idle open transaction), and metadata lock waits in "
              "the wait summaries."),
     "lose": ("A statement stuck on \"Waiting for table metadata lock\" shows no holder; Locks cannot show "
              "metadata lock waits."),
     "cost": ("low", "One record per metadata lock taken: about one per table a statement touches, plus a few "
                     "global and schema locks for writes and DDL. On by default in MySQL, off in MariaDB. Locks "
                     "taken while it was off are never recorded.")},
    {"key": "wait/synch", "label": "Internal locks (mutex, rwlock, cond)", "match": "wait/synch/%",
     "gain": ("Contention on the server's and InnoDB's internal mutexes, read/write locks and condition waits, "
              "in the wait summaries and in mutex_instances and rwlock_instances."),
     "lose": "No internal contention data: Waits never shows mutex waits, and their absence proves nothing.",
     "cost": ("high", "Internal locks are taken extremely often (millions of times per second on a busy server), "
                      "and every timed acquisition reads the timer twice and updates statistics. Off by default on "
                      "both engines. Enable them for a short investigation, ideally a subset such as "
                      "wait/synch/mutex/innodb/%, then turn them off.")},
    {"key": "wait/io/socket", "label": "Socket I/O", "match": "wait/io/socket/%",
     "gain": ("Network read and write waits for each client connection and the listening TCP and Unix sockets, "
              "in the socket summaries and socket_instances."),
     "lose": "No network wait data.",
     "cost": ("moderate", "One timed event per network read and write. Off by default on both engines.")},
    {"key": "memory", "label": "Memory", "match": "memory/%",
     "gain": ("Memory in use and peak, per subsystem and per thread, user, host and account "
              "(memory_summary_* tables and the sys.memory_* views)."),
     "lose": ("No memory accounting by subsystem. Performance Schema's own memory "
              "(memory/performance_schema/*) is always counted."),
     "cost": ("low", "Each allocation and free updates counters; there is no timing. On by default in MySQL, off "
                     "in MariaDB. Turned on at runtime, counts start at that moment: memory allocated earlier is "
                     "not counted, so current bytes read low until it is freed.")},
    {"key": "transaction", "label": "Transactions", "match": "transaction",
     "gain": ("Transaction events with state, isolation level, access mode, autocommit and duration "
              "(events_transactions_* tables); with the transaction consumers, each session's open transaction."),
     "lose": "No transaction events or summaries.",
     "cost": ("low", "One event per transaction. On by default in MySQL (with its current and history "
                     "consumers), off in MariaDB.")},
    {"key": "idle", "label": "Idle", "match": "idle",
     "gain": "Time sessions spend idle, waiting for the client's next command (the idle wait row).",
     "lose": "Idle time is not recorded (Waits leaves it out anyway).",
     "cost": ("low", "One event each time a session waits for its next command. On by default on both engines.")},
    {"key": "error", "label": "Errors", "match": "error",
     "gain": ("Errors and warnings raised, per error code and per thread, user, host and account, with first and "
              "last seen (events_errors_summary_* tables)."),
     "lose": "Error summaries stop counting.",
     "cost": ("low", "A counter update per error or warning; there is no timing. MySQL only, on by default.")},
)
GROUP_KEYS = tuple(g["key"] for g in GROUPS)

CONSUMERS = {
    "global_instrumentation": {
        "gain": "The master switch: every summary table and every event table depends on it.",
        "lose": ("Performance Schema stops collecting: summaries freeze and no new events are recorded. Setup and "
                 "instance tables can still be read."),
        "cost": ("low", "No cost of its own. Turning it off removes nearly all collection overhead without a "
                        "restart; the memory stays allocated.")},
    "thread_instrumentation": {
        "gain": ("Collection per session: the events_* tables and the per-thread, account, user and host "
                 "summaries. While it is on, setup_actors decides which sessions are recorded."),
        "lose": ("No per-session data and no statement, transaction or wait event tables; global summaries still "
                 "collect, from every session."),
        "cost": ("low", "Per-session bookkeeping on each event.")},
    "statements_digest": {
        "gain": ("Statement digests in events_statements_summary_by_digest, the source of Top statements and "
                 "sys.statement_analysis."),
        "lose": "Top statements stops updating; existing digest rows stay until a restart or a truncate.",
        "cost": ("low", "Normalizes each statement's text and updates one row of a fixed-size table "
                        "(performance_schema_digests_size rows; when it is full, new statements land in one "
                        "catch-all row and Performance_schema_digest_lost rises).")},
    "events_statements_current": {
        "gain": ("The statement each session runs now, or ran last: Locks shows an idle blocker's last statement "
                 "from it. The statement history consumers record only what it records."),
        "lose": "No current statement per session, and both statement history tables stop filling.",
        "cost": ("low", "Keeps one row per session up to date for each statement.")},
    "events_statements_history": {
        "gain": ("The last N statements of each session (N is performance_schema_events_statements_history_size): "
                 "the Query editor reads its capture statistics here, and you can see what a blocking session ran "
                 "before it went idle."),
        "lose": ("Captures lose rows examined, temporary tables, sort merge passes and the no-index flag."),
        "cost": ("low", "Copies each finished statement into its session's ring buffer. The buffer is sized by the "
                        "history size variable (a restart setting) whether the consumer is on or off.")},
    "events_statements_history_long": {
        "gain": "The last N statements across all sessions in one table (events_statements_history_long).",
        "lose": "Nothing myflames uses.",
        "cost": ("moderate", "Every statement from every session is copied into one shared ring buffer, which busy "
                             "servers contend on.")},
    "events_statements_cpu": {
        "gain": "CPU time per statement (the CPU_TIME columns of the statement tables).",
        "lose": "CPU_TIME stays zero.",
        "cost": ("low", "Reads the thread's CPU clock at the start and end of each timed statement. MySQL only.")},
    "events_transactions_current": {
        "gain": ("Each session's open or last transaction: state, isolation level, access mode, autocommit and "
                 "how long it has run. Needs the transaction instrument."),
        "lose": "No current transaction per session; the transaction history tables stop filling.",
        "cost": ("low", "One row per session, updated at each transaction start and end.")},
    "events_transactions_history": {
        "gain": "The last N transactions of each session.",
        "lose": "No recent transactions per session.",
        "cost": ("low", "Copies each finished transaction into its session's ring buffer.")},
    "events_transactions_history_long": {
        "gain": "The last N transactions across all sessions.",
        "lose": "Nothing myflames uses.",
        "cost": ("moderate", "Every transaction is copied into one shared ring buffer.")},
    "events_stages_current": {
        "gain": ("The stage each session is in now, with WORK_COMPLETED and WORK_ESTIMATED for progress stages: "
                 "ALTER TABLE and buffer pool load progress."),
        "lose": "No current stage or progress per session.",
        "cost": ("low", "Low with the default stage instruments (progress stages only); moderate when every stage "
                        "instrument is on.")},
    "events_stages_history": {
        "gain": "The last N stages of each session.",
        "lose": "No recent stages per session.",
        "cost": ("moderate", "Copies every finished stage, several per statement, into the session's ring buffer.")},
    "events_stages_history_long": {
        "gain": "The last N stages across all sessions.",
        "lose": "Nothing myflames uses.",
        "cost": ("high", "Every stage of every statement is copied into one shared ring buffer.")},
    "events_waits_current": {
        "gain": "The wait each thread is in right now (events_waits_current). Waits does not need it.",
        "lose": "No current wait per thread; the wait history tables stop filling.",
        "cost": ("moderate", "Writes a row for every recorded wait; with the internal lock instruments on, that "
                             "is a very large number of waits.")},
    "events_waits_history": {
        "gain": "The last N waits of each thread.",
        "lose": "No recent waits per thread.",
        "cost": ("moderate", "Copies every finished wait into the thread's ring buffer.")},
    "events_waits_history_long": {
        "gain": "The last N waits across all threads.",
        "lose": "Nothing myflames uses.",
        "cost": ("high", "Every wait of every thread is copied into one shared ring buffer; with the internal lock "
                         "instruments on, this is the most expensive consumer.")},
}
_UNKNOWN_CONSUMER = {"gain": "", "lose": "", "cost": ("moderate", "Not described by myflames.")}

RULES = (
    "UPDATE of setup_instruments and setup_consumers takes effect at once and lasts until the server restarts.",
    ("Mutex, rwlock, cond and socket instruments re-apply at once to existing instances, and table I/O and table "
     "lock changes apply from the next statement that uses each open table. A file is recorded only if it was "
     "opened while its instrument was on, so files already open (such as InnoDB tablespaces) stay unrecorded until "
     "they are reopened. Metadata locks, memory and statement, stage and transaction events count from the moment "
     "the instrument is on: locks already held and memory already allocated stay invisible."),
    ("Consumers apply to every session at once. A history consumer records only what its current consumer records, "
     "under global_instrumentation; statement, transaction and wait events also need thread_instrumentation."),
    ("setup_actors changes reach new sessions only; an existing session keeps its INSTRUMENTED and HISTORY values "
     "(change those in performance_schema.threads). setup_objects changes apply at once."),
    ("performance_schema itself and the *_size variables are read-only: they need my.cnf (or SET PERSIST_ONLY on "
     "MySQL) and a restart. To keep an instrument or consumer change after a restart, add the my.cnf lines under "
     "[mysqld]; for instruments the longest matching pattern wins whatever the order of the lines, and between "
     "patterns of the same length the later line wins."),
)
PRIVILEGES = ("myflames reads with SELECT on performance_schema only. Running the statements needs UPDATE on "
              "performance_schema.setup_instruments and setup_consumers (for example GRANT UPDATE ON "
              "performance_schema.setup_instruments TO 'dba'@'%';), and INSERT plus DROP (for TRUNCATE) on "
              "setup_actors and setup_objects to restore their defaults.")
#: TRUNCATE empties only that configuration table. MariaDB's DELETE on
#: setup_objects removes the TABLE row with the same schema and name whatever
#: the row's OBJECT_TYPE (Ma/storage/perfschema/table_setup_objects.cc:323-334,
#: same at 10.11, 11.4 and 11.8), so the restore never uses DELETE.
READ_ONLY_NOTE = ("myflames only shows these statements; it never runs them. Run them yourself in a SQL client, as "
                  "a user with the privileges below.")


def _group_where(group):
    match = group["match"]
    return "NAME LIKE '%s'" % match if match.endswith("%") else "NAME = '%s'" % match


def _untimed(group):
    return group["key"] in ("memory", "error")


def group_sql(group):
    """Runtime and my.cnf statements that turn a group on or off."""
    where = _group_where(group)
    if _untimed(group):
        on = "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES' WHERE %s;" % where
        off = "UPDATE performance_schema.setup_instruments SET ENABLED = 'NO' WHERE %s;" % where
    else:
        on, off = _SET_ON + where + ";", _SET_OFF + where + ";"
    option = "performance-schema-instrument = '%s=%s'"
    return {"on": {"runtime": on, "config": option % (group["match"], "ON")},
            "off": {"runtime": off, "config": option % (group["match"], "OFF")}}


def consumer_sql(name):
    chain = [name] + consumer_chain(name)
    on_names = [n for n in CONSUMER_ORDER if n in chain] or chain
    return {"on": {"runtime": _CONSUMERS_ON + "NAME IN (%s);" % _quote_list(on_names) if len(on_names) > 1
                   else _CONSUMERS_ON + "NAME = '%s';" % name,
                   "config": "\n".join("%s = ON" % _consumer_option(n) for n in on_names)},
            "off": {"runtime": "UPDATE performance_schema.setup_consumers SET ENABLED = 'NO' WHERE NAME = '%s';" % name,
                    "config": "%s = OFF" % _consumer_option(name)}}


def _features_for_instrument(match):
    out = []
    for f in FEATURES:
        for kind in ("needs", "optional"):
            if any(patterns_overlap(match, p) for p in f.get(kind, {}).get("instruments", ())):
                out.append({"key": f["key"], "label": f["label"], "view": f["view"], "required": kind == "needs"})
                break
    return out


def _features_for_consumer(name):
    out = []
    for f in FEATURES:
        needs = f.get("needs", {}).get("consumers", ())
        optional = f.get("optional", {}).get("consumers", ())
        if name in needs or name in optional:
            out.append({"key": f["key"], "label": f["label"], "view": f["view"], "required": name in needs})
    return out

# ---- presets -------------------------------------------------------------------------------------


def _config(lines):
    return "\n".join(["[mysqld]", "performance_schema = ON"] + list(lines))


def _preset_minimal(engine):
    mysql = engine == "mysql"
    runtime = ("%s(%s)\n   OR NAME IN ('wait/io/table/sql/handler', 'wait/lock/metadata/sql/mdl');\n"
               "%sNAME IN ('global_instrumentation', 'thread_instrumentation', 'statements_digest',\n"
               "      'events_statements_current', 'events_statements_history');"
               % (_SET_ON_ML, statements_where(engine), _CONSUMERS_ON_ML))
    config = _config([] if mysql else [
        "performance-schema-instrument = 'wait/lock/metadata/sql/mdl=ON'",
        "performance-schema-consumer-events-statements-current = ON",
        "performance-schema-consumer-events-statements-history = ON"])
    if mysql:
        config += "\n# The rest of this preset is on by default in MySQL; remove any my.cnf line that turns it off."
    return {
        "key": "minimal", "label": "Minimal for myflames",
        "summary": "Exactly what Top statements, capture statistics, Locks and Indexes read.",
        "gain": ("Top statements, capture statistics, metadata locks and an idle blocker's last statement in Locks, "
                 "unused indexes and full scans in Indexes, and per-table I/O."),
        "cost": ("low", "MySQL ships with all of it on." if mysql else
                 ("Adds the metadata lock instrument (one record per lock taken) and the statement current and "
                  "history consumers (a ring buffer of the last N statements per session) to MariaDB's defaults.")),
        "runtime": runtime, "config": config, "undo": None,
        "targets": {"instruments": [("statement", True), ("wait/io/table/sql/handler", True),
                                    ("wait/lock/metadata/sql/mdl", True)],
                    "consumers": ["global_instrumentation", "thread_instrumentation", "statements_digest",
                                  "events_statements_current", "events_statements_history"]},
        "notes": [],
    }


def _preset_locks(engine):
    mysql = engine == "mysql"
    runtime = ("%sNAME IN ('wait/lock/metadata/sql/mdl', 'wait/lock/table/sql/handler', 'transaction')\n"
               "   OR (%s);\n"
               "%sNAME IN ('global_instrumentation', 'thread_instrumentation',\n"
               "      'events_statements_current', 'events_statements_history',\n"
               "      'events_transactions_current', 'events_transactions_history');"
               % (_SET_ON_ML, statements_where(engine), _CONSUMERS_ON_ML))
    config = _config([] if mysql else [
        "performance-schema-instrument = 'wait/lock/metadata/sql/mdl=ON'",
        "performance-schema-instrument = 'transaction=ON'",
        "performance-schema-consumer-events-statements-current = ON",
        "performance-schema-consumer-events-statements-history = ON",
        "performance-schema-consumer-events-transactions-current = ON",
        "performance-schema-consumer-events-transactions-history = ON"])
    if mysql:
        config += "\n# The rest of this preset is on by default in MySQL; remove any my.cnf line that turns it off."
    return {
        "key": "locks", "label": "Lock troubleshooting",
        "summary": "Metadata and table locks, open transactions, and what blocking sessions ran.",
        "gain": ("Who holds the metadata lock DDL waits for (Locks, metadata_locks), table lock waits (Waits), each "
                 "session's open transaction with its state and age (events_transactions_current), and the last "
                 "statements a blocking session ran before it went idle (events_statements_history)."),
        "cost": ("low", "One event per lock request, per transaction and per statement; history keeps N rows per "
                        "session." + (" All of it is on by default in MySQL." if mysql else "")),
        "runtime": runtime, "config": config, "undo": None,
        "targets": {"instruments": [("wait/lock/metadata/sql/mdl", True), ("wait/lock/table/sql/handler", True),
                                    ("transaction", True), ("statement", True)],
                    "consumers": ["global_instrumentation", "thread_instrumentation", "events_statements_current",
                                  "events_statements_history", "events_transactions_current",
                                  "events_transactions_history"]},
        "notes": ["InnoDB row locks need no instrument: MySQL reads them from performance_schema.data_locks "
                  "(Performance Schema on), MariaDB from information_schema.INNODB_LOCKS (needs PROCESS, works with "
                  "Performance Schema off)."],
    }


def _preset_waits(engine):
    runtime = ("%sNAME LIKE 'wait/%%';\n%sNAME = 'global_instrumentation';" % (_SET_ON_ML, _CONSUMERS_ON_ML))
    undo = _SET_OFF_ML + "NAME LIKE 'wait/synch/%' OR NAME LIKE 'wait/io/socket/%';"
    config = ("# Keep this preset temporary. Only to record waits from startup, such as during a warm-up:\n"
              + _config(["performance-schema-instrument = 'wait/%=ON'"]))
    notes = ["Run the undo statement when you are done: it turns the internal lock and socket instruments back off "
             "(their default on both engines) and leaves file, table and lock waits on."]
    if engine == "mariadb":
        notes.append("It also turns on the metadata lock instrument, which MariaDB ships off; the undo statement "
                     "leaves it on.")
    return {
        "key": "waits", "label": "Wait analysis",
        "summary": "Every wait instrument, internal locks included, for a short investigation.",
        "gain": ("Internal contention in Waits (InnoDB and server mutexes, read/write locks, condition waits) and "
                 "network waits, on top of file, table and lock waits."),
        "cost": ("high", "Every internal lock acquisition is timed: two timer reads and a statistics update, "
                         "millions of times per second on a busy server. Turn it on for minutes, compare Waits, "
                         "then run the undo statement. Rule of thumb: measure throughput before and after on that "
                         "server."),
        "runtime": runtime, "config": config, "undo": undo,
        "targets": {"instruments": [("wait/%", True)], "consumers": ["global_instrumentation"]},
        "notes": notes,
    }


def _preset_defaults(engine):
    if engine == "mysql":
        runtime = (_SET_OFF_ML + "NAME LIKE 'wait/synch/%' OR NAME LIKE 'wait/io/socket/%';\n"
                   + _SET_ON_ML + "NAME LIKE 'wait/io/file/%'\n   OR NAME IN ('wait/io/table/sql/handler', "
                   "'wait/lock/table/sql/handler', 'wait/lock/metadata/sql/mdl', 'idle', 'transaction');\n"
                   + _SET_ON_ML + _MYSQL_STATEMENTS + ";\n"
                   + _SET_OFF_ML + "NAME LIKE 'statement/sp/%' AND NAME <> 'statement/sp/stmt';\n"
                   "UPDATE performance_schema.setup_instruments\n"
                   "   SET ENABLED = IF(FIND_IN_SET('progress', PROPERTIES), 'YES', 'NO'),\n"
                   "       TIMED = IF(FIND_IN_SET('progress', PROPERTIES), 'YES', 'NO')\n"
                   " WHERE NAME LIKE 'stage/%';\n"
                   "UPDATE performance_schema.setup_instruments SET ENABLED = 'YES' "
                   "WHERE NAME LIKE 'memory/%' OR NAME = 'error';\n")
        consumers = _MYSQL_CONSUMERS_ON
    else:
        stages = _quote_list(MARIADB_PROGRESS_STAGES)
        runtime = (_SET_OFF_ML + "NAME LIKE 'wait/synch/%' OR NAME LIKE 'wait/io/socket/%'\n"
                   "   OR NAME IN ('wait/lock/metadata/sql/mdl', 'transaction');\n"
                   + _SET_ON_ML + "NAME LIKE 'wait/io/file/%' OR NAME LIKE 'statement/%'\n"
                   "   OR NAME IN ('wait/io/table/sql/handler', 'wait/lock/table/sql/handler', 'idle');\n"
                   + _SET_OFF_ML + "NAME LIKE 'stage/%%' AND NAME NOT IN (%s);\n" % stages
                   + _SET_ON_ML + "NAME IN (%s);\n" % stages
                   + "UPDATE performance_schema.setup_instruments SET ENABLED = 'NO' WHERE NAME LIKE 'memory/%';\n")
        consumers = _MARIADB_CONSUMERS_ON
    runtime += ("UPDATE performance_schema.setup_consumers\n   SET ENABLED = IF(NAME IN (%s), 'YES', 'NO');"
                % _quote_list(consumers))
    return {
        "key": "defaults", "label": "Back to defaults",
        "summary": "The engine's compiled-in defaults for every instrument and consumer.",
        "gain": "Returns to the Default column below: what %s turns on when no my.cnf line changes it."
                % ("MySQL" if engine == "mysql" else "MariaDB"),
        "cost": ("low", "MySQL's defaults are meant for production. MariaDB's collect less: no event tables, "
                        "metadata locks, transactions or memory." if engine == "mysql" else
                 "MariaDB's defaults collect statement summaries and digests, file, table I/O, table lock and idle "
                 "waits and progress stage summaries, with no event tables, metadata locks, transactions or "
                 "memory."),
        "runtime": runtime,
        "config": ("# Remove every performance-schema-instrument and performance-schema-consumer-* line under\n"
                   "# [mysqld]; the defaults then apply at the next restart. Keep performance_schema as you want it."),
        "undo": None,
        "targets": "defaults",
        "notes": ["These are the compiled defaults. A server started with performance-schema-instrument or consumer "
                  "lines in my.cnf starts differently; a restart applies that startup configuration again.",
                  "Built-in memory/performance_schema/* instruments cannot be turned off; the server ignores them "
                  "silently."],
    }


PRESET_KEYS = ("minimal", "locks", "waits", "defaults")


def presets(engine):
    return [_preset_minimal(engine), _preset_locks(engine), _preset_waits(engine), _preset_defaults(engine)]

# ---- setup_actors and setup_objects --------------------------------------------------------------


_OBJECT_TYPES = ("EVENT", "FUNCTION", "PROCEDURE", "TABLE", "TRIGGER")
_HIDDEN_SCHEMAS = ("mysql", "performance_schema", "information_schema")


def default_objects():
    rows = []
    for kind in _OBJECT_TYPES:
        for schema in _HIDDEN_SCHEMAS:
            rows.append((kind, schema, "%", "NO", "NO"))
        rows.append((kind, "%", "%", "YES", "YES"))
    return rows


ACTORS_RESTORE = ("TRUNCATE TABLE performance_schema.setup_actors;\n"
                  "INSERT INTO performance_schema.setup_actors (HOST, USER, ROLE, ENABLED, HISTORY)\n"
                  "VALUES ('%', '%', '%', 'YES', 'YES');")


def objects_restore():
    values = ",\n".join("  ('%s', '%s', '%s', '%s', '%s')" % row for row in default_objects())
    return ("TRUNCATE TABLE performance_schema.setup_objects;\n"
            "INSERT INTO performance_schema.setup_objects (OBJECT_TYPE, OBJECT_SCHEMA, OBJECT_NAME, ENABLED, TIMED)\n"
            "VALUES\n%s;" % values)


def _actors(rows):
    if rows is None:
        return None
    clean = [tuple((c or "") for c in r[:5]) for r in rows if len(r) >= 5]
    default = [("%", "%", "%", "YES", "YES")]
    state = "default" if sorted(clean) == default else ("empty" if not clean else "custom")
    return {"rows": [{"host": r[0], "user": r[1], "role": r[2], "enabled": r[3].upper() == "YES",
                      "history": r[4].upper() == "YES"} for r in clean],
            "state": state, "restore": None if state == "default" else ACTORS_RESTORE,
            "explain": ("Which sessions are recorded (ENABLED) and keep history (HISTORY), by user and host. The "
                        "default instruments everyone. Changes reach new sessions only."),
            "default_text": "One row: every user on every host, enabled, with history."}


def _objects(rows):
    if rows is None:
        return None
    clean = [tuple((c or "") for c in r[:5]) for r in rows if len(r) >= 5]
    normal = sorted((a.upper(), b, c, d.upper(), e.upper()) for a, b, c, d, e in clean)
    state = "default" if normal == sorted(default_objects()) else ("empty" if not clean else "custom")
    disabled = sorted({r[1] for r in clean if r[3].upper() != "YES" and r[0].upper() == "TABLE"})
    return {"rows": [{"object_type": r[0], "schema": r[1], "name": r[2], "enabled": r[3].upper() == "YES",
                      "timed": r[4].upper() == "YES"} for r in clean],
            "state": state, "restore": None if state == "default" else objects_restore(),
            "disabled_table_schemas": disabled,
            "explain": ("Which tables, events, triggers and stored routines are instrumented, by schema and name. "
                        "Changes apply at once."),
            "default_text": ("Everything except the mysql, performance_schema and information_schema schemas, for "
                             "every object type.")}

# ---- reading (talks to the server) ---------------------------------------------------------------


def instruments_report(conn):
    """Return the ``instruments-1.0`` report. Read-only; see the module docstring."""
    reader = Reader(conn, INSTRUMENTS_TAG)
    notes = []
    raw = {"instruments": None, "consumers": None, "actors": None, "objects": None, "memory": None}
    try:
        server = preflight(reader, SIZE_VARIABLES)
        if server.ps_on:
            columns = "NAME, ENABLED, TIMED" + (", PROPERTIES" if server.engine == "mysql" else "")
            raw["instruments"] = reader.optional("SELECT %s FROM performance_schema.setup_instruments" % columns,
                                                 "performance_schema.setup_instruments", notes)
            if raw["instruments"] is None and any("denied" in n for n in notes):
                notes.append("Grant SELECT on performance_schema to this user: GRANT SELECT ON "
                             "performance_schema.* TO <user>;")
                return compute_instruments(server, raw, notes)
            if raw["instruments"] is None and server.engine == "mysql":
                raw["instruments"] = reader.optional(
                    "SELECT NAME, ENABLED, TIMED FROM performance_schema.setup_instruments",
                    "performance_schema.setup_instruments", [])
            raw["consumers"] = reader.optional("SELECT NAME, ENABLED FROM performance_schema.setup_consumers",
                                               "performance_schema.setup_consumers", notes)
            raw["actors"] = reader.optional(
                "SELECT HOST, USER, ROLE, ENABLED, HISTORY FROM performance_schema.setup_actors",
                "performance_schema.setup_actors", notes)
            raw["objects"] = reader.optional(
                "SELECT OBJECT_TYPE, OBJECT_SCHEMA, OBJECT_NAME, ENABLED, TIMED FROM performance_schema.setup_objects",
                "performance_schema.setup_objects", notes)
            memory = reader.optional(
                "SELECT SUM(CURRENT_NUMBER_OF_BYTES_USED) FROM performance_schema.memory_summary_global_by_event_name "
                "WHERE EVENT_NAME LIKE 'memory/performance_schema/%'",
                "performance_schema.memory_summary_global_by_event_name", notes)
            raw["memory"] = _number(memory[0][0]) if memory and memory[0] else None
    except ConnectorError as exc:
        raise ValueError("Could not read the Performance Schema setup: " + reason(exc))
    return compute_instruments(server, raw, notes)

# ---- arithmetic (pure) -----------------------------------------------------------------------------


def _yes(value):
    return (value or "").upper() == "YES"


def _instrument_map(rows):
    out = {}
    for row in rows or []:
        if len(row) >= 3 and row[0]:
            props = row[3] if len(row) > 3 else ""
            out[row[0]] = (_yes(row[1]), None if row[2] is None else _yes(row[2]), props or "")
    return out


def _group_of(name):
    for group in GROUPS:
        if like(name, group["match"]):
            return group["key"]
    return "other"


def group_for_pattern(pattern):
    """The group a needed instrument pattern belongs to."""
    for group in GROUPS:
        if patterns_overlap(group["match"], pattern):
            return group["key"]
    return "other"


def _count_group(engine, group, instruments):
    names = [n for n in instruments if like(n, group["match"])]
    builtin = 0
    if group["key"] == "memory":
        builtin = sum(1 for n in names if n.startswith(BUILTIN_MEMORY))
        names = [n for n in names if not n.startswith(BUILTIN_MEMORY)]
    timed_applies = not _untimed(group)
    enabled = sum(1 for n in names if instruments[n][0])
    timed = sum(1 for n in names if instruments[n][1]) if timed_applies else None
    total = len(names)
    changed = []
    default_on = 0
    for n in names:
        d = default_state(engine, n, instruments[n][2])
        if d is None:
            continue
        default_on += 1 if d[0] else 0
        if instruments[n][0] != d[0] or (timed_applies and d[1] is not None and instruments[n][0]
                                         and instruments[n][1] != d[1]):
            changed.append(n)
    if total == 0:
        state = "absent"
    elif enabled == 0:
        state = "off"
    elif enabled == total and (not timed_applies or timed == total):
        state = "on"
    elif enabled == total:
        state = "counted"
    else:
        state = "partial"
    sub = None
    if group["key"] == "wait/synch":
        sub = []
        for kind in ("mutex", "rwlock", "sxlock", "prlock", "cond"):
            kinds = [n for n in names if n.startswith("wait/synch/%s/" % kind)]
            if kinds:
                sub.append({"kind": kind, "enabled": sum(1 for n in kinds if instruments[n][0]), "total": len(kinds)})
    return {"enabled": enabled, "timed": timed, "total": total, "state": state, "builtin": builtin,
            "default_enabled": default_on, "changed": len(changed), "changed_names": sorted(changed)[:20],
            "kinds": sub}


def _default_label(engine, key):
    mysql = engine == "mysql"
    return {
        "statement": "on (stored-program control instructions off)" if mysql else "on",
        "stage": "progress stages only",
        "wait/io/file": "on", "wait/io/table": "on", "wait/lock/table": "on",
        "wait/lock/metadata": "on" if mysql else "off",
        "wait/synch": "off", "wait/io/socket": "off",
        "memory": "on" if mysql else "off (Performance Schema's own memory always counted)",
        "transaction": "on" if mysql else "off", "idle": "on",
        "error": "on, not timed" if mysql else "not in MariaDB",
    }.get(key, "unknown")


def _groups(server, instruments):
    out = []
    for group in GROUPS:
        if group["key"] == "error" and server.engine == "mariadb":
            continue
        entry = {"key": group["key"], "id": "group:" + group["key"], "label": group["label"],
                 "match": group["match"], "gain": group["gain"], "lose": group["lose"],
                 "cost": {"level": group["cost"][0], "text": group["cost"][1]},
                 "default": _default_label(server.engine, group["key"]),
                 "timed_applies": not _untimed(group), "sql": group_sql(group),
                 "features": _features_for_instrument(group["match"])}
        if instruments is None:
            entry.update({"enabled": None, "timed": None, "total": None, "state": "unknown", "changed": None})
        else:
            entry.update(_count_group(server.engine, group, instruments))
        out.append(entry)
    if instruments:
        others = sorted(n for n in instruments if _group_of(n) == "other")
        if others:
            out.append({"key": "other", "id": "group:other", "label": "Other instruments", "match": None,
                        "enabled": sum(1 for n in others if instruments[n][0]), "total": len(others),
                        "timed": sum(1 for n in others if instruments[n][1]), "state": "partial",
                        "changed": None, "gain": "", "lose": "", "cost": None, "default": "unknown",
                        "timed_applies": True, "sql": None, "features": [], "names": others[:20]})
    return out


def _consumers(server, rows):
    current = None if rows is None else {r[0]: _yes(r[1]) for r in rows if len(r) >= 2 and r[0]}
    names = list(CONSUMER_ORDER)
    if current:
        names = [n for n in CONSUMER_ORDER if n in current] + sorted(n for n in current if n not in CONSUMER_ORDER)
    elif server.engine == "mariadb":
        names = [n for n in names if n != "events_statements_cpu"]
    out = []
    for name in names:
        info = CONSUMERS.get(name, _UNKNOWN_CONSUMER)
        chain = consumer_chain(name)
        entry = {"name": name, "id": "consumer:" + name, "default": default_consumer(server.engine, name),
                 "requires": chain, "gain": info["gain"], "lose": info["lose"],
                 "cost": {"level": info["cost"][0], "text": info["cost"][1]}, "sql": consumer_sql(name),
                 "features": _features_for_consumer(name)}
        if current is None:
            entry.update({"enabled": None, "effective": None, "blocked_by": []})
        else:
            on = current.get(name, False)
            blocked = [p for p in chain if not current.get(p, False)]
            entry.update({"enabled": on, "effective": on and not blocked, "blocked_by": blocked})
        out.append(entry)
    return out


def _instrument_check(engine, pattern, instruments, timed=True):
    """(status, text) for one needed instrument pattern."""
    names = [n for n in instruments if like(n, pattern)]
    if pattern == "statement/%":
        # Stored-program control instructions are not needed (MySQL leaves them off).
        names = [n for n in names if not sp_control(n)]
    if not names:
        return "absent", "%s is not on this server" % pattern
    off = [n for n in names if not instruments[n][0]]
    untimed = [n for n in names if instruments[n][0] and timed and instruments[n][1] is False]
    if len(off) == len(names):
        return "off", "%s is off" % (pattern if len(names) > 1 else names[0])
    if off:
        return "partial", "%d of %d %s instruments are off" % (len(off), len(names), pattern)
    if untimed:
        return "partial", "%s is counted but not timed, so times read zero" % (
            pattern if len(names) > 1 else names[0])
    return "ok", None


def _features(server, instruments, consumers, actors, objects):
    current = {c["name"]: c for c in consumers}
    out = []
    for f in FEATURES:
        engine = f.get("engine")
        entry = {"key": f["key"], "label": f["label"], "view": f["view"], "explain": f["explain"],
                 "missing": [], "optional_missing": [], "rows": []}
        if f.get("advisory"):
            entry["advisory"] = True
        if engine and engine != server.engine:
            entry["status"] = "not_needed"
            entry["missing"] = []
            out.append(entry)
            continue
        if not server.ps_on:
            entry["status"] = "unavailable"
            entry["missing"] = ["performance_schema is OFF"]
            entry["rows"] = ["ps"]
            out.append(entry)
            continue
        if instruments is None or not consumers or any(c.get("enabled") is None for c in consumers):
            entry["status"] = "unknown"
            out.append(entry)
            continue
        worst = "ready"
        for kind in ("needs", "optional"):
            spec = f.get(kind, {})
            for name in spec.get("consumers", ()):
                c = current.get(name)
                if c is None or not c["effective"]:
                    blocked = c["blocked_by"] if c else []
                    text = "consumer %s is %s" % (name, "off" if not (c and c["enabled"]) else
                                                  "on but blocked by %s" % ", ".join(blocked))
                    (entry["missing"] if kind == "needs" else entry["optional_missing"]).append(text)
                    entry["rows"].append("consumer:" + name)
                    if kind == "needs":
                        worst = "off"
            for pattern in spec.get("instruments", ()):
                if f.get("instrument_engines", {}).get(pattern, server.engine) != server.engine:
                    continue
                status, text = _instrument_check(server.engine, pattern, instruments, timed=f.get("timed", True))
                if status == "ok":
                    continue
                (entry["missing"] if kind == "needs" else entry["optional_missing"]).append(text)
                entry["rows"].append("group:" + group_for_pattern(pattern))
                if kind == "needs":
                    worst = "off" if status in ("off", "absent") or worst == "off" else "partial"
        # While thread_instrumentation is on, setup_actors decides which sessions
        # are recorded. Metadata locks ignore it, and background threads (InnoDB
        # I/O) are recorded whatever it says.
        threads_on = (current.get("thread_instrumentation") or {}).get("enabled")
        if threads_on and actors and actors["state"] != "default" and \
                f["key"] not in ("locks_rows", "locks_mdl", "replication_receiver"):
            background = f["key"] in ("waits", "io")
            if actors["state"] == "empty" and not background:
                entry["missing"].append("setup_actors is empty, so no session is recorded")
                worst = "off"
            elif actors["state"] == "empty":
                entry["optional_missing"].append("setup_actors is empty, so no session is recorded (background "
                                                 "threads still are)")
            else:
                entry["optional_missing"].append(
                    "setup_actors is not the default; sessions it does not instrument%s are not recorded"
                    % (" with HISTORY" if f["key"] == "capture" else ""))
            entry["rows"].append("actors")
        if f["key"] in ("indexes", "io") and objects and objects["state"] != "default":
            entry["optional_missing"].append("setup_objects is not the default; some tables may not be recorded")
            entry["rows"].append("objects")
        entry["status"] = worst
        entry["rows"] = sorted(set(entry["rows"]), key=entry["rows"].index)
        out.append(entry)
    return out


def _applied(preset, engine, instruments, consumers):
    """True when the server already matches the preset (None when unknown)."""
    if instruments is None or not consumers or consumers[0].get("enabled") is None:
        return None
    current = {c["name"]: c["enabled"] for c in consumers}
    targets = preset["targets"]
    if targets == "defaults":
        for name, (enabled, timed, props) in instruments.items():
            d = default_state(engine, name, props)
            if d is None or name.startswith(BUILTIN_MEMORY):
                continue
            if enabled != d[0] or (d[1] is not None and enabled and timed != d[1]):
                return False
        return all(current[n] == default_consumer(engine, n) for n in current)
    for match, _ in targets["instruments"]:
        names = [n for n in instruments if like(n, match + ("/%" if match == "statement" else ""))]
        if match == "statement" and engine == "mysql":
            names = [n for n in names if not sp_control(n)]
        if any(not instruments[n][0] or instruments[n][1] is False for n in names):
            return False
    return all(current.get(n, False) for n in targets["consumers"])


def _status(server, raw, sizes):
    mysql = server.engine == "mysql"
    on = server.ps_on
    if on:
        text = ("Performance Schema is on. It is read-only at runtime: turning it off needs "
                "performance_schema = OFF in my.cnf and a restart. Instruments and consumers below can change "
                "at runtime.")
    else:
        text = ("Performance Schema is off%s. It cannot be turned on at runtime: add performance_schema = ON under "
                "[mysqld] in my.cnf and restart the server. Once it is on, the defaults below apply."
                % (" (MariaDB's default)" if not mysql else ""))
    enable = None
    if not on:
        enable = {"config": "[mysqld]\nperformance_schema = ON", "persist": None, "persist_text": None}
        if mysql:
            enable["persist"] = "SET PERSIST_ONLY performance_schema = ON;"
            enable["persist_text"] = ("Or, on MySQL, persist it from SQL and restart: SET PERSIST_ONLY writes "
                                      "mysqld-auto.cnf, which wins over my.cnf. It needs the "
                                      "SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN privileges.")
    return {"id": "ps", "enabled": on, "default": mysql, "runtime_changeable": False, "text": text,
            "enable": enable, "disable": {"config": "[mysqld]\nperformance_schema = OFF"} if on else None,
            "memory_bytes": raw.get("memory"), "sizes": sizes, "lost": server.lost(),
            "cost": ("Some buffers (history_long tables, the digest table) are allocated at startup from the *_size "
                     "variables; the others grow as needed up to their *_max limits and are kept until shutdown. "
                     "Memory is the main fixed cost; CPU cost depends on which instruments and consumers are on.")}


def compute_instruments(server, raw, notes=None):
    """The ``instruments-1.0`` report from the raw rows. Pure."""
    notes = list(notes or [])
    report = base_report(SCHEMA_VERSION, server)
    instruments = _instrument_map(raw.get("instruments")) if raw.get("instruments") is not None else None
    if server.ps_on and raw.get("instruments") is not None and not instruments:
        instruments = None
        notes.append("performance_schema.setup_instruments returned no rows.")
    sizes = {}
    for name in SIZE_VARIABLES:
        value = _number(server.var(name))
        if value is not None:
            sizes[name] = value
    actors = _actors(raw.get("actors")) if server.ps_on else None
    objects = _objects(raw.get("objects")) if server.ps_on else None
    groups = _groups(server, instruments if server.ps_on else None)
    consumers = _consumers(server, raw.get("consumers") if server.ps_on else None)
    unavailable = None
    if not server.ps_on:
        unavailable = "performance_schema_off"
    elif instruments is None or raw.get("consumers") is None:
        unavailable = "denied" if any("denied" in n or "ERROR 1142" in n for n in notes) else "no_data"
    preset_list = []
    for preset in presets(server.engine):
        preset = dict(preset)
        preset["cost"] = {"level": preset["cost"][0], "text": preset["cost"][1]}
        preset["applied"] = _applied(preset, server.engine, instruments, consumers) if server.ps_on else None
        if preset["key"] == "defaults":
            extra = []
            if actors and actors["restore"]:
                extra.append(actors["restore"])
            if objects and objects["restore"]:
                extra.append(objects["restore"])
            preset["restore_setup"] = "\n".join(extra) or None
            if preset["applied"] and extra:
                preset["applied"] = False
        preset.pop("targets", None)
        preset_list.append(preset)
    report.update({
        "available": unavailable is None, "unavailable": unavailable,
        "status": _status(server, raw, sizes),
        "features": _features(server, instruments, consumers, actors, objects),
        "groups": groups, "consumers": consumers, "actors": actors, "objects": objects,
        "presets": preset_list, "rules": list(RULES), "privileges": PRIVILEGES, "read_only_note": READ_ONLY_NOTE,
        "notes": notes,
    })
    _notes(server, report, notes)
    return report


def _notes(server, report, notes):
    if not server.ps_on:
        notes.insert(0, report["status"]["text"])
    note = lost_note(server)
    if note:
        notes.append(note)
    if server.engine == "mariadb":
        notes.append("MariaDB ships Performance Schema off. With it on, the metadata lock, transaction and memory "
                     "instruments are off, and of the consumers only global_instrumentation, "
                     "thread_instrumentation and statements_digest are on; it has no error instrument.")
    else:
        notes.append("MySQL 8.4 and 9.x share these defaults in source, and MySQL 26.7 matched them on a live "
                     "server; newer versions add instruments, not new defaults.")
    for consumer in report["consumers"]:
        if consumer.get("enabled") and consumer.get("blocked_by"):
            blocked = consumer["blocked_by"]
            notes.append("%s is on but records %s while %s is off." % (
                consumer["name"], "little or nothing" if blocked == ["thread_instrumentation"] else "nothing",
                ", ".join(blocked)))
    actors, objects = report["actors"], report["objects"]
    if actors and actors["state"] != "default":
        notes.append("setup_actors is not the default (%s), so some sessions are not recorded."
                     % ("empty" if actors["state"] == "empty" else "%d rows" % len(actors["rows"])))
    if objects and objects["state"] != "default":
        notes.append("setup_objects is not the default; tables in disabled schemas are not recorded.")

# ---- text output for the CLI -----------------------------------------------------------------------


_STATE_TEXT = {"on": "on", "off": "off", "partial": "partly on", "counted": "on, not timed", "absent": "absent",
               "unknown": "?"}


def format_text(report):
    head = "%s %s" % ("MariaDB" if report["engine"] == "mariadb" else "MySQL", report["server_version"] or "?")
    if report["uptime_s"] is not None:
        head += ", up %s" % _duration_text(report["uptime_s"])
    status = report["status"]
    head += ". Performance Schema: %s" % ("ON" if status["enabled"] else "OFF")
    if status.get("memory_bytes") is not None:
        head += ", using %s" % _bytes_text(status["memory_bytes"])
    lines = [head + ".", status["text"]]
    lines.append("")
    lines.append("MYFLAMES VIEWS")
    rows = [[f["status"].replace("_", " "), f["label"], "; ".join(f["missing"])] for f in report["features"]]
    lines.extend("  " + l for l in table_text(["STATE", "VIEW", "MISSING"], rows).split("\n"))
    lines.append("")
    lines.append("INSTRUMENTS (setup_instruments)")
    rows = []
    for g in report["groups"]:
        if g["key"] == "other":
            continue
        counts = "?" if g["total"] is None else "%d/%d" % (g["enabled"], g["total"])
        changed = "?" if g.get("changed") is None else ("default" if not g["changed"] else "%d changed" % g["changed"])
        rows.append([g["label"], g["match"], counts, _STATE_TEXT.get(g["state"], g["state"]), changed,
                     g["cost"]["level"]])
    lines.extend("  " + l for l in table_text(["GROUP", "NAME", ">ON", "STATE", "VS DEFAULT", "COST"],
                                              rows).split("\n"))
    lines.append("")
    lines.append("CONSUMERS (setup_consumers)")
    rows = []
    for c in report["consumers"]:
        state = "?" if c["enabled"] is None else ("ON" if c["enabled"] else "OFF")
        if c.get("enabled") and c.get("blocked_by"):
            state += " (blocked by %s)" % ", ".join(c["blocked_by"])
        rows.append([c["name"], state, "ON" if c["default"] else "OFF", c["cost"]["level"]])
    lines.extend("  " + l for l in table_text(["CONSUMER", "STATE", "DEFAULT", "COST"], rows).split("\n"))
    lines.append("")
    lines.append("PRESETS (myflames instruments --preset NAME prints the statements)")
    rows = []
    for p in report["presets"]:
        state = {True: "already in place", False: "would change this server", None: "?"}[p["applied"]]
        rows.append([p["key"], p["label"], p["cost"]["level"], state])
    lines.extend("  " + l for l in table_text(["NAME", "PRESET", "COST", "STATE"], rows).split("\n"))
    lines.append("")
    lines.append(report["read_only_note"])
    lines.append(report["privileges"])
    return "\n".join(lines) + "\n"


def preset_text(report, key):
    """One preset as runnable SQL with comments, then its my.cnf lines."""
    preset = next(p for p in report["presets"] if p["key"] == key)
    engine = "MariaDB" if report["engine"] == "mariadb" else "MySQL"
    out = ["-- %s (%s %s)" % (preset["label"], engine, report["server_version"] or "?"),
           "-- %s" % preset["summary"],
           "-- Gain: %s" % preset["gain"],
           "-- Cost (%s, rule of thumb): %s" % (preset["cost"]["level"], preset["cost"]["text"])]
    if preset.get("applied"):
        out.append("-- This server already matches this preset.")
    if not report["status"]["enabled"]:
        out.append("-- Performance Schema is off: turn it on in my.cnf and restart first (see below).")
    out.append("-- Takes effect at once and lasts until the server restarts. myflames never runs it.")
    out.append(preset["runtime"])
    if preset.get("restore_setup"):
        out.append("-- setup_actors and setup_objects differ from the defaults; this restores them:")
        out.append(preset["restore_setup"])
    if preset.get("undo"):
        out.append("-- When you are done:")
        out.append(preset["undo"])
    for note in preset["notes"]:
        out.append("-- Note: %s" % note)
    out.append("")
    out.append("# my.cnf, to keep it after a restart:")
    out.append(preset["config"])
    return "\n".join(out) + "\n"
