"""InnoDB: SHOW ENGINE INNODB STATUS, parsed and explained, never shown raw.

:func:`innodb_report` returns the ``innodb-1.0`` envelope shared by
``myflames innodb`` and the workspace's Server > InnoDB view. The text the
server prints is split into its sections (:func:`parse_status`, pure, needs no
server); each section becomes structured numbers (``data``), a few key rows
(``items``) with an ok / watch / problem status and the rule of thumb behind
it, a one-line plain-language ``summary``, and ``links`` to the related views
(Locks, Health, Sessions, I/O). The raw text is returned only when asked for
(``raw=True``, ``myflames innodb --raw``), for support cases.

Reading the status needs PROCESS and changes nothing, except that InnoDB
restarts its "per second averages" window for every reader (MariaDB also
wakes the purge coordinator, which is harmless). Unknown or
missing lines are tolerated: a section that cannot be read keeps what parsed
and the report says so in ``notes`` and ``parse``.

Statuses are rules of thumb, not verdicts (``RULES``). One snapshot has no
trend: a history list or pending I/O that stays high across refreshes means
more than any single value.

Verified against server source (mysql-server 9.7 trunk "M/", mariadb-server
13.0 trunk "Ma/"; tags where noted) and live output from MySQL 8.4.11, 9.7.2
and 26.7.0 and MariaDB 11.4.13 and 13.0.2:

* Section order and header: M/storage/innobase/srv/srv0srv.cc:1331-1540
  (``srv_printf_innodb_monitor``); Ma/storage/innobase/srv/srv0srv.cc:643-825.
  The window is the seconds since the previous printout by any reader
  (srv0srv.cc:1339-1356). Both engines also restart it in the background, so
  it covers at most about 60 s (M srv0srv.cc:1282-1305, 1806-1814; Ma
  srv0srv.cc:613-636), and every 15 s when innodb_status_output or
  innodb-status-file is on (M srv0srv.cc:1773-1803). MySQL divides the ROW
  OPERATIONS and adaptive hash index rates by whole seconds plus 0.001
  (srv0srv.cc:1342-1347), MariaDB every rate, so a short window overstates
  them and a 0 s window inflates them up to 1000 times; FILE I/O and the
  buffer pool use fractional seconds on MySQL (os0file.cc:7531-7535,
  buf0buf.cc:6664-6668). The output is cut at 1 MiB, dropping the start of the
  transaction list behind "... truncated..." or the end of the text
  (M/storage/innobase/handler/ha_innodb.cc:19193-19275, counted in
  Innodb_truncated_status_writes).
* BACKGROUND THREAD: M srv0srv.cc:851-859 (MariaDB prints no shutdown loops).
* SEMAPHORES: MySQL prints the wait array (M/storage/innobase/sync/
  sync0arr.cc:905-977) with one "--Thread ... has waited at FILE line N for S
  seconds the semaphore:" block per reserved cell (375-440; a cell that
  prints "wait has ended" is not waiting, 439-441); InnoDB logs "A long
  semaphore wait" after 240 s (sync0arr.cc:781-850) and stops the server once
  a wait passes 600 s and stays there for 10 more checks (M srv0srv.cc:116,
  1863-1884; fixed in release builds, the timeout variable exists only in
  debug builds, ha_innodb.cc:22305-22311). The RW-shared / RW-excl / RW-sx spin counters
  are deprecated and always print 0 (M/storage/innobase/sync/sync0sync.cc:
  185-204), as does "srv_master_thread log flush and writes" (never
  incremented, srv0srv.cc:639, 858). MariaDB leaves the section empty since
  10.6 (Ma srv0srv.cc:683-687; mariadb-10.6.0 srv0srv.cc:768-772); it has
  innodb_fatal_semaphore_wait_threshold (default 600) but prints no wait
  array.
* LATEST FOREIGN KEY ERROR: written by row_ins_foreign_report_err and
  row_ins_foreign_report_add_err (M/storage/innobase/row/row0ins.cc:693-850,
  1540-1570; Ma/storage/innobase/row/row0ins.cc:740-850, 1575-1620). MySQL
  9.6.0 and later check foreign keys in the SQL layer unless
  innodb_native_foreign_keys=ON (read-only, default OFF; M/sql/sys_vars.cc:
  7685-7691), so InnoDB records no foreign key error there by default.
* LATEST DETECTED DEADLOCK: parsed by locks.parse_latest_deadlock.
* TRANSACTIONS: M/storage/innobase/lock/lock0lock.cc:4374-4440 (counter,
  purge, history list length), 4560-4597 and 4705-4770 (the list; MySQL lists
  sessions' not-started transactions and read-write transactions, so a
  read-only transaction holding an old read view is not listed), trx line
  format M/storage/innobase/trx/trx0trx.cc:2520-2620; MariaDB lists every
  transaction and prints the lock wait in microseconds (Ma/storage/innobase/
  lock/lock0lock.cc:5325-5480, trx/trx0trx.cc:1775-1870). The history list
  length counts committed transactions' update undo logs not yet purged
  (M/storage/innobase/trx/trx0purge.cc:352-406; Ma trx0sys.cc:177-235). Purge
  stops at the oldest open read view (M/storage/innobase/read/read0read.cc:
  685-715); READ COMMITTED and READ UNCOMMITTED close the view after each
  statement (ha_innodb.cc:19161-19168, 19754-19762), so an idle transaction
  holds purge back only under REPEATABLE READ, after its first consistent
  read ("Trx read view will not see ..." in its entry).
* FILE I/O: M/storage/innobase/os/os0file.cc:7440-7575 (pending AIO per
  segment, pending fsyncs, OS file counters and rates; a one-segment array
  prints no count); Ma/storage/innobase/os/os0file.cc:3355-3402.
* INSERT BUFFER AND ADAPTIVE HASH INDEX: M/storage/innobase/ibuf/
  ibuf0ibuf.cc:4378-4415, ha/ha0ha.cc:326-372, M srv0srv.cc:1428-1446.
  MariaDB 10.11 prints an INSERT BUFFER section only when the change buffer
  is not empty (mariadb-10.11.16 ibuf0ibuf.cc:4477-4506) and removed the
  change buffer in 11.0; it prints ADAPTIVE HASH INDEX only when the adaptive
  hash index is on (Ma srv0srv.cc:735-760). Defaults: innodb_adaptive_hash_
  index is OFF from MySQL 8.4.0 (ON in 8.0) and OFF on MariaDB;
  innodb_change_buffering is ``none`` on MySQL 8.4.0 to 9.4 and ``all`` from
  9.5.0 (and in 8.0), ``none`` on MariaDB 10.11 (ha_innodb.cc sysvars at the
  tags).
* LOG: M/storage/innobase/log/log0log.cc:1137-1210; Ma/storage/innobase/log/
  log0log.cc:1517-1537. "Log capacity" (the variable) and "Log capacity used"
  (the capacity in use) appear from MySQL 8.4.8 and 9.6.0; with a legacy
  innodb_log_file_size x innodb_log_files_in_group only the second is right
  (mysql-8.4.9 ha_innodb.cc:4670-4675), so the capacity comes from
  Innodb_redo_log_capacity_resized (log0files_capacity.cc:398), then "Log
  capacity used", then config_review.redo_capacity. The checkpoint age is the
  log sequence number minus the last checkpoint, as health.py computes it
  (the points strictly apply to the oldest dirty page's age, ``dirty_age``,
  which is at most the checkpoint age, log0chkp.cc:736-744); the pressure
  points are health.py's (``REDO_POINTS_
  MYSQL``: adaptive flushing at its age limit near 73% of the redo capacity,
  forced flushing near 79%, user writes wait near 84%; ``REDO_POINTS_
  MARIADB``: flushing ahead at 7/8 of Innodb_checkpoint_max_age, about 71% of
  innodb_log_file_size, writes wait at the max age, about 81%).
* BUFFER POOL AND MEMORY: M/storage/innobase/buf/buf0buf.cc:6750-6880
  (totals, then INDIVIDUAL BUFFER POOL INFO when there is more than one
  instance); Ma/storage/innobase/buf/buf0buf.cc:3962-4140 (one instance, plus
  the dirty percentage and innodb_max_dirty_pages_pct). The hit rate is per
  1000 page requests in the window (1000 minus pages read x 1000 / requests);
  MySQL computes it unsigned, so read-ahead can make it wrap past 1000
  (buf0buf.cc:6789-6797), which is reported as unknown.
* ROW OPERATIONS: M srv0srv.cc:1462-1530; MariaDB prints only the read views
  and the main thread state (Ma srv0srv.cc:796-812).
"""
import re

from .connector import ConnectorError
from .health import (HIT_RATIO_PCT, MIN_DISK_READS_PER_S, MIN_PAGE_REQUESTS, REDO_POINTS_MARIADB,
                     REDO_POINTS_MYSQL)
from .perf_common import _bytes_text, _count_text, _duration_text, _number, _round
from .perf_common import Reader, Server, base_report, reason, show_variables
from .sessions import TRX_AGE_S

SCHEMA_VERSION = "innodb-1.0"
INNODB_TAG = "/*myflames:innodb*/ "
SEVERITY = {"ok": 0, "watch": 1, "problem": 2}
STATUSES = ("ok", "watch", "problem", "info", "unknown")
#: History list length: (watch, problem). A trend-free rule of thumb.
HISTORY_LIST = (100000, 1000000)
#: A latch wait in the SEMAPHORES section (seconds): watch, problem (InnoDB's
#: own long-wait warning).
SEMAPHORE_WAIT_S = (10, 240)
#: Below this window the per-second averages are noisy.
MIN_WINDOW_S = 5
#: Transactions kept in the list (oldest first) and statement text per transaction.
TOP_TRANSACTIONS = 20
TEXT_CAP = 4096
RAW_CAP = 1 << 20

RULES = {
    "history_list": ("a rule of thumb without a trend: watch at %s committed transactions whose undo logs purge "
                     "has not processed yet, problem at %s. The size alone says little; a list that keeps growing "
                     "across refreshes means purge is falling behind, most often because an old read view (a "
                     "REPEATABLE READ transaction or a long statement) is still open"
                     % ("{:,}".format(HISTORY_LIST[0]), "{:,}".format(HISTORY_LIST[1]))),
    "trx_age": "the oldest active transaction is watched at %d s and a problem at %d s (as in Sessions)" % TRX_AGE_S,
    "lock_wait": "watch when any transaction is waiting for a lock",
    "purge_state": "watch when purge is not running (stopped or disabled)",
    "semaphore": ("watch when a thread has waited %d s for an InnoDB latch, problem at %d s, where InnoDB logs "
                  "a long semaphore wait (MySQL stops the server once a wait passes 600 s; that limit is fixed "
                  "in release builds)" % SEMAPHORE_WAIT_S),
    "pending_io": ("watch when reads or fsyncs are pending in this snapshot (pending writes are shown but not "
                   "judged: the page cleaner always has some in flight). One snapshot cannot tell a burst from a "
                   "backlog: pending reads on every refresh mean queries wait for the storage"),
    "redo_mysql": ("watch when the checkpoint age reaches about %d%% of the redo capacity (adaptive flushing at "
                   "its age limit), problem at about %d%% (forced flushing); user writes wait near %d%%"
                   % tuple(int(round(p * 100)) for p in REDO_POINTS_MYSQL)),
    "redo_mariadb": ("watch when the checkpoint age reaches 7/8 of Innodb_checkpoint_max_age (InnoDB flushes "
                     "ahead, about 71% of innodb_log_file_size), problem at Innodb_checkpoint_max_age (writes "
                     "wait, about 81%)"),
    "hit_rate": ("watch below %g%%, problem below %g%% with at least %g pages read from disk per second; judged "
                 "only with about %s page requests or more in the window"
                 % (HIT_RATIO_PCT[0], HIT_RATIO_PCT[1], MIN_DISK_READS_PER_S, "{:,}".format(MIN_PAGE_REQUESTS))),
    "dirty": ("watch at innodb_max_dirty_pages_pct (90 by default); a high share alone is not a stall, waits for "
              "a free page and a nearly full redo log are"),
    "queue": ("watch when statements wait in the InnoDB queue, which happens only when innodb_thread_concurrency "
              "is above 0 and that many threads are already inside InnoDB"),
}
_LINK = {
    "locks": {"view": "locks", "label": "Open Locks"},
    "sessions": {"view": "sessions", "label": "Open Sessions"},
    "health": {"view": "health", "label": "Open Health"},
    "io": {"view": "io", "label": "Open I/O"},
    "config": {"view": "config", "label": "Open Configuration"},
}
SECTION_KEYS = {
    "BACKGROUND THREAD": "background",
    "SEMAPHORES": "semaphores",
    "LATEST FOREIGN KEY ERROR": "foreign_key_error",
    "LATEST DETECTED DEADLOCK": "deadlock",
    "TRANSACTIONS": "transactions",
    "FILE I/O": "file_io",
    "INSERT BUFFER AND ADAPTIVE HASH INDEX": "insert_buffer_ahi",
    "INSERT BUFFER": "insert_buffer",
    "ADAPTIVE HASH INDEX": "adaptive_hash_index",
    "LOG": "log",
    "BUFFER POOL AND MEMORY": "buffer_pool",
    "INDIVIDUAL BUFFER POOL INFO": "buffer_pool_instances",
    "ROW OPERATIONS": "row_operations",
}
#: The sections of the report, in the order the UI shows them.
ORDER = (("semaphores", "Semaphores"), ("transactions", "Transactions and purge"),
         ("deadlock", "Latest deadlock"), ("foreign_key_error", "Latest foreign key error"),
         ("file_io", "File I/O"), ("insert_buffer", "Change buffer"),
         ("adaptive_hash_index", "Adaptive hash index"), ("log", "Redo log and checkpoint"),
         ("buffer_pool", "Buffer pool and memory"), ("row_operations", "Row operations"),
         ("background", "Background thread"))

VARIABLES = ("version", "version_comment", "performance_schema", "innodb_page_size",
             "innodb_max_dirty_pages_pct", "innodb_redo_log_capacity", "innodb_log_file_size",
             "innodb_log_files_in_group", "innodb_adaptive_hash_index", "innodb_change_buffering",
             "innodb_buffer_pool_instances", "innodb_native_foreign_keys", "innodb_thread_concurrency")
STATUS_NAMES = ("Uptime", "Innodb_redo_log_capacity_resized", "Innodb_checkpoint_age",
                "Innodb_checkpoint_max_age", "Innodb_deadlocks", "Innodb_truncated_status_writes")


def history_status(value):
    """(status, rule) for a history list length; shared with sessions.py."""
    if value is None:
        return "unknown", RULES["history_list"]
    return ("problem" if value >= HISTORY_LIST[1] else "watch" if value >= HISTORY_LIST[0] else "ok"), \
        RULES["history_list"]


def _grade_seconds(seconds, limits):
    if seconds is None:
        return "ok"
    return "problem" if seconds >= limits[1] else "watch" if seconds >= limits[0] else "ok"


def _int(text):
    if text is None:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        number = _number(text)
        return int(number) if number is not None else None


def _float(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if value == value and value not in (float("inf"), float("-inf")) else None


def _cap(text, limit):
    if text is None:
        return None, False
    return (text[:limit - 1] + "…", True) if len(text) > limit else (text, False)


# ---- splitting -------------------------------------------------------------------

_DASHES = re.compile(r"^-{3,}$")
_TITLE = re.compile(r"^[A-Z][A-Z /]*[A-Z]$")
_HEADER_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}) (\S+) INNODB MONITOR OUTPUT")
_WINDOW = re.compile(r"Per second averages calculated from the last (\d+) seconds")
TRUNCATED_MARK = "... truncated..."


def split_sections(text):
    """(header lines, [(title, body lines)], ended) from the status text.

    A section starts with a title between two dashed lines; ``ended`` says the
    "END OF INNODB MONITOR OUTPUT" line was seen (False: the text was cut)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    header, sections, current, ended = [], [], None, False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip() == "END OF INNODB MONITOR OUTPUT":
            ended = True
            if current is not None and current[1] and _DASHES.match(current[1][-1].strip()):
                current[1].pop()
            break
        if (_DASHES.match(line.strip()) and i + 2 < len(lines) and _TITLE.match(lines[i + 1].strip())
                and _DASHES.match(lines[i + 2].strip())):
            current = (lines[i + 1].strip(), [])
            sections.append(current)
            i += 3
            continue
        if current is None:
            header.append(line)
        else:
            current[1].append(line)
        i += 1
    return header, sections, ended


def _header(lines):
    out = {"printed_at": None, "window_s": None}
    for line in lines:
        match = _HEADER_TIME.match(line.strip())
        if match:
            out["printed_at"] = match.group(1)
        match = _WINDOW.search(line)
        if match:
            out["window_s"] = int(match.group(1))
    return out


# ---- section parsers (pure) ---------------------------------------------------------

def parse_background(lines):
    text = "\n".join(lines)
    out = {"active_loops": None, "shutdown_loops": None, "idle_loops": None, "log_flush_and_writes": None}
    match = re.search(r"srv_master_thread loops: (\d+) srv_active, (?:(\d+) srv_shutdown, )?(\d+) srv_idle", text)
    if match:
        out["active_loops"], out["shutdown_loops"], out["idle_loops"] = (_int(g) for g in match.groups())
    match = re.search(r"srv_master_thread log flush and writes: (\d+)", text)
    if match:
        out["log_flush_and_writes"] = _int(match.group(1))
    return out


_WAIT_HEAD = re.compile(r"^--Thread (\S+) has waited at (\S+) line (\d+) for ([\d.]+) seconds the semaphore:")
_SPINS = re.compile(r"^(RW-shared|RW-excl|RW-sx|Mutex) spins (\d+), rounds (\d+), OS waits (\d+)")


def parse_semaphores(lines):
    out = {"reservation_count": None, "signal_count": None, "waits": [], "spins": {},
           "spin_rounds_per_wait": {}, "empty": not any(l.strip() for l in lines)}
    wait = None
    for raw in lines:
        line = raw.strip()
        match = re.match(r"^OS WAIT ARRAY INFO: reservation count (\d+)", line)
        if match:
            out["reservation_count"] = (out["reservation_count"] or 0) + int(match.group(1))
            wait = None
            continue
        match = re.match(r"^OS WAIT ARRAY INFO: signal count (\d+)", line)
        if match:
            out["signal_count"] = int(match.group(1))
            wait = None
            continue
        match = _WAIT_HEAD.match(line)
        if match:
            wait = {"thread": match.group(1), "file": match.group(2), "line": int(match.group(3)),
                    "seconds": _float(match.group(4)), "latch": None, "created_at": None, "writer": None,
                    "readers": None, "waiters_flag": None, "ended": False}
            out["waits"].append(wait)
            continue
        match = _SPINS.match(line)
        if match:
            out["spins"][match.group(1)] = {"spins": int(match.group(2)), "rounds": int(match.group(3)),
                                            "os_waits": int(match.group(4))}
            wait = None
            continue
        if line.startswith("Spin rounds per wait:"):
            for value, name in re.findall(r"([\d.]+) (RW-shared|RW-excl|RW-sx|mutex)", line):
                out["spin_rounds_per_wait"][name] = _float(value)
            continue
        if wait is not None:
            latch = re.match(r"^(S-lock|X-lock \(wait_ex\)|X-lock|SX-lock) on RW-latch at \S+ created in file (\S+) "
                             r"line (\d+)", line)
            mutex = re.match(r"^Mutex at \S+, (.*?), locked by (\S+)", line)
            if latch:
                wait["latch"] = latch.group(1) + " on an RW-latch"
                wait["created_at"] = "%s line %s" % (latch.group(2), latch.group(3))
            elif mutex:
                wait["latch"] = "mutex %s" % mutex.group(1)
                wait["writer"] = None if mutex.group(2) == "nobody" else mutex.group(2)
            elif line.startswith("a writer (thread id"):
                found = re.match(r"^a writer \(thread id (\S+)\) has reserved it in mode (.+)$", line)
                if found:
                    wait["writer"] = "thread %s (%s)" % (found.group(1), found.group(2))
            elif line.startswith("number of readers"):
                found = re.match(r"^number of readers (\d+).*?waiters flag (\d+)", line)
                if found:
                    wait["readers"], wait["waiters_flag"] = int(found.group(1)), int(found.group(2))
            elif line == "wait has ended":
                wait["ended"] = True
    return out


_STATEMENT_STOP = re.compile(r"^(Trx read view|------- TRX HAS BEEN WAITING|---TRANSACTION |TABLE LOCK table|"
                             r"RECORD LOCKS |Record lock, heap no|\d+ LOCKS PRINTED FOR THIS TRX|"
                             r"Foreign key constraint fails|------------------$)")


def _thread_line(line):
    from .locks import _IP, _THREAD
    match = _THREAD.match(line)
    if not match:
        return None
    # host, then the client IP for TCP connections, then user, then state
    # (as locks._parse_trx_block).
    parts = match.group(3).rstrip().split(" ") if match.group(3).strip() else []
    at = 2 if len(parts) > 2 and _IP.match(parts[1]) else 1
    return {"connection_id": int(match.group(1)), "query_id": int(match.group(2)),
            "host": parts[0] if parts else None, "user": parts[at] if len(parts) > at else None,
            "thread_state": " ".join(parts[at + 1:]) or None}


def _counts_line(line, trx):
    """'[LOCK WAIT |ROLLING BACK |COMMITTING ]N lock struct(s), heap size N, N row lock(s)[, undo log entries N]'."""
    if line.startswith("LOCK WAIT"):
        trx["lock_wait"] = True
    if line.startswith("ROLLING BACK"):
        trx["rolling_back"] = True
    if line.startswith("COMMITTING"):
        trx["committing"] = True
    match = re.search(r"(\d+) lock struct\(s\), heap size (\d+), (\d+) row lock\(s\)", line)
    if match:
        trx["lock_structs"], trx["heap_size"], trx["row_locks"] = (int(g) for g in match.groups())
    match = re.search(r"undo log entries (\d+)", line)
    if match:
        trx["undo_entries"] = int(match.group(1))


_TRX_LINE = re.compile(r"^---TRANSACTION (\d+|\([^)]*\)), (.*)$")


def _trx_state(rest, trx):
    rest = rest.strip()
    inside = re.search(r", thread declared inside InnoDB (\d+)$", rest)
    if inside:
        rest = rest[:inside.start()]
    if rest.startswith("not started"):
        trx["state"] = "not started"
        return
    match = re.match(r"^(ACTIVE \(PREPARED\)|ACTIVE) (\d+) sec(?: (.*))?$", rest)
    if match:
        trx["state"] = "PREPARED" if "PREPARED" in match.group(1) else "ACTIVE"
        trx["active_s"] = int(match.group(2))
        operation = (match.group(3) or "").strip()
        if operation.endswith("recovered trx"):
            trx["recovered"] = True
            operation = operation[:-len("recovered trx")].strip()
        trx["operation"] = operation or None
        return
    trx["state"] = rest.split(",")[0] or None


def parse_transactions(lines, engine="mysql"):
    from .locks import _status_lock
    text = "\n".join(lines)
    out = {"trx_id_counter": None, "purge": None, "history_list_length": None, "transactions": [],
           "truncated_by_server": TRUNCATED_MARK in text, "list_seen": False}
    match = re.search(r"Trx id counter (\d+)", text)
    if match:
        out["trx_id_counter"] = _int(match.group(1))
    match = re.search(r"Purge done for trx's n:o < (\d+) undo n:o < (\d+) state: ([^\n]*)", text)
    if match:
        out["purge"] = {"trx_no": _int(match.group(1)), "undo_no": _int(match.group(2)),
                        "state": match.group(3).strip() or None}
    match = re.search(r"History list length (\d+)", text)
    if match:
        out["history_list_length"] = _int(match.group(1))
    trx = None
    in_statement = False
    statement = []
    waiting_block = False

    def close():
        if trx is not None:
            body = "\n".join(statement).strip()
            trx["statement"], trx["statement_truncated"] = _cap(body, TEXT_CAP) if body else (None, False)

    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("LIST OF TRANSACTIONS FOR EACH SESSION"):
            out["list_seen"] = True
            continue
        head = _TRX_LINE.match(line)
        if head:
            close()
            trx = {"trx_id": head.group(1), "state": None, "active_s": None, "operation": None,
                   "recovered": False, "tables_in_use": None, "tables_locked": None, "lock_wait": False,
                   "rolling_back": False, "committing": False, "lock_structs": None, "heap_size": None,
                   "row_locks": None, "undo_entries": None, "connection_id": None, "query_id": None,
                   "host": None, "user": None, "thread_state": None, "statement": None,
                   "statement_truncated": False, "read_view": None, "wait_s": None, "waiting_for": None}
            _trx_state(head.group(2), trx)
            out["transactions"].append(trx)
            statement, in_statement, waiting_block = [], False, False
            continue
        if trx is None:
            continue
        if waiting_block:
            if line.strip() == "------------------":
                waiting_block = False
            elif trx["waiting_for"] is None:
                lock = _status_lock(line, engine)
                if lock:
                    lock.pop("records", None)
                    trx["waiting_for"] = lock
            continue
        if in_statement and not _STATEMENT_STOP.match(line) and line != TRUNCATED_MARK:
            statement.append(line)
            continue
        in_statement = False
        match = re.match(r"^mysql tables in use (\d+), locked (\d+)", line)
        if match:
            trx["tables_in_use"], trx["tables_locked"] = int(match.group(1)), int(match.group(2))
            continue
        if "lock struct(s)" in line:
            _counts_line(line, trx)
            continue
        thread = _thread_line(line)
        if thread:
            trx.update(thread)
            in_statement = True
            continue
        match = re.match(r"^Trx read view will not see trx with id >= (\d+), sees < (\d+)", line)
        if match:
            trx["read_view"] = {"low_limit": _int(match.group(1)), "up_limit": _int(match.group(2))}
            continue
        match = re.match(r"^------- TRX HAS BEEN WAITING (\d+) (SEC|us) FOR THIS LOCK TO BE GRANTED:", line)
        if match:
            value = int(match.group(1))
            trx["wait_s"] = value if match.group(2) == "SEC" else _round(value / 1e6, 1)
            trx["lock_wait"] = True
            waiting_block = True
            continue
    close()
    return out


_FK_TABLE = re.compile(r"Foreign key constraint fails for table (\S+?):\s*$", re.M)
_FK_CONSTRAINT = re.compile(r"CONSTRAINT\s+(`(?:[^`]|``)+`|\S+)\s+FOREIGN KEY\s*\(([^)]*)\)\s+REFERENCES\s+(\S+)\s*"
                            r"\(([^)]*)\)((?:\s+ON (?:DELETE|UPDATE) (?:CASCADE|SET NULL|SET DEFAULT|NO ACTION|"
                            r"RESTRICT))*)")


def _unquote(name):
    if name and name.startswith("`") and name.endswith("`"):
        return name[1:-1].replace("``", "`")
    return name


def parse_foreign_key_error(lines, engine="mysql"):
    """The LATEST FOREIGN KEY ERROR section: when, who, the statement, the
    constraint and the cause, without the record dumps."""
    text = "\n".join(lines)
    out = {"at": None, "connection_id": None, "user": None, "host": None, "statement": None,
           "table": None, "constraint": None, "columns": None, "referenced_table": None,
           "referenced_columns": None, "on_actions": None, "kind": None, "operation": None,
           "parent_table": None, "child_table": None, "index": None, "message": None}
    first = next((l for l in lines if l.strip()), "")
    match = re.match(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", first.strip())
    if match:
        out["at"] = match.group(1)
    statement, collecting = [], False
    for line in lines:
        thread = _thread_line(line)
        if thread:
            out.update({"connection_id": thread["connection_id"], "user": thread["user"], "host": thread["host"]})
            collecting = True
            continue
        if collecting:
            if line.startswith("Foreign key constraint fails") or line.startswith("Error in foreign key") \
                    or not line.strip():
                collecting = False
                continue
            statement.append(line)
    body = "\n".join(statement).strip()
    out["statement"] = _cap(body, TEXT_CAP)[0] if body else None
    match = _FK_TABLE.search(text)
    if match:
        out["table"] = match.group(1)
    match = _FK_CONSTRAINT.search(text)
    if match:
        out["constraint"] = _unquote(match.group(1))
        out["columns"] = [_unquote(c.strip()) for c in match.group(2).split(",") if c.strip()]
        out["referenced_table"] = match.group(3)
        out["referenced_columns"] = [_unquote(c.strip()) for c in match.group(4).split(",") if c.strip()]
        out["on_actions"] = " ".join(match.group(5).split()) or None
    parent = re.search(r"But in parent table (\S+?), in index (\S+?),", text)
    child = re.search(r"But in child table (\S+?), in index (\S+?)(?:,|\s*$)", text, re.M)
    operation = re.search(r"(Trying to (?:delete or update|delete|update|add|modify))", text)
    if parent:
        out["kind"] = "no_parent_row"
        out["parent_table"], out["index"] = parent.group(1), parent.group(2)
        out["child_table"] = out["table"]
        out["operation"] = "insert or update in the child table"
    elif child:
        out["kind"] = "child_rows_exist"
        out["child_table"], out["index"] = child.group(1), child.group(2)
        out["parent_table"] = out["referenced_table"]
        verb = (operation.group(1) if operation else "Trying to delete or update")[len("Trying to "):]
        out["operation"] = "%s in the parent table" % verb
    elif "does not currently exist" in text or "does\nnot currently exist" in text:
        out["kind"] = "missing_table"
        missing = re.search(r"But the (?:parent|referencing) table (\S+)", text)
        out["parent_table"] = missing.group(1) if missing else out["referenced_table"]
    elif out["table"] or out["constraint"]:
        out["kind"] = "other"
    else:
        out["kind"] = "other"
        out["message"] = next((l.strip() for l in lines[1:] if l.strip()), None)
    return out


def _pending_value(text, name):
    match = re.search(re.escape(name) + r":\s*(\d+)?\s*(\[[\d,\s]*\])?", text)
    if not match:
        return None
    if match.group(1) is not None:
        return int(match.group(1))
    if match.group(2) is not None:
        return sum(int(x) for x in re.findall(r"\d+", match.group(2)))
    return None


def parse_file_io(lines):
    text = "\n".join(lines)
    threads = {}
    for match in re.finditer(r"^I/O thread \d+ state: (.+?) \((.+?)\)", text, re.M):
        purpose = match.group(2)
        entry = threads.setdefault(purpose, {"purpose": purpose, "threads": 0, "states": {}})
        entry["threads"] += 1
        entry["states"][match.group(1)] = entry["states"].get(match.group(1), 0) + 1
    out = {"io_threads": list(threads.values()),
           "pending": {"normal_aio_reads": _pending_value(text, "Pending normal aio reads"),
                       "aio_writes": _pending_value(text, "aio writes"),
                       "ibuf_aio_reads": _pending_value(text, "ibuf aio reads"),
                       "log_ios": _pending_value(text, "log i/o's"),
                       "sync_ios": _pending_value(text, "sync i/o's"),
                       "reads": None, "writes": None, "fsync_log": None, "fsync_buffer_pool": None,
                       "fsync": None},
           "os": {"reads": None, "writes": None, "fsyncs": None},
           "rates": {"reads_per_s": None, "avg_bytes_per_read": None, "writes_per_s": None, "fsyncs_per_s": None}}
    pending = out["pending"]
    match = re.search(r"Pending flushes \(fsync\) log: (\d+); buffer pool: (\d+)", text)
    if match:
        pending["fsync_log"], pending["fsync_buffer_pool"] = int(match.group(1)), int(match.group(2))
    match = re.search(r"Pending flushes \(fsync\): (\d+)", text)
    if match:
        pending["fsync"] = int(match.group(1))
    match = re.search(r"(\d+) pending p?reads, (\d+) pending p?writes", text)
    if match:
        pending["reads"], pending["writes"] = int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+) OS file reads, (\d+) OS file writes, (\d+) OS fsyncs", text)
    if match:
        out["os"] = {"reads": int(match.group(1)), "writes": int(match.group(2)), "fsyncs": int(match.group(3))}
    match = re.search(r"([\d.]+) reads/s, (\d+) avg bytes/read, ([\d.]+) writes/s, ([\d.]+) fsyncs/s", text)
    if match:
        out["rates"] = {"reads_per_s": _float(match.group(1)), "avg_bytes_per_read": int(match.group(2)),
                        "writes_per_s": _float(match.group(3)), "fsyncs_per_s": _float(match.group(4))}
    values = [v for k, v in pending.items() if v is not None]
    out["pending_total"] = sum(values) if values else None
    return out


def _ops(text, title):
    match = re.search(re.escape(title) + r"\s*\n?\s*insert (\d+), delete mark (\d+), delete (\d+)", text)
    if not match:
        return None
    return {"insert": int(match.group(1)), "delete_mark": int(match.group(2)), "delete": int(match.group(3))}


def parse_insert_buffer(lines):
    text = "\n".join(lines)
    match = re.search(r"(?:Ibuf: )?size (\d+), free list len (\d+), seg size (\d+), (\d+) merges", text)
    if not match:
        return None
    return {"size_pages": int(match.group(1)), "free_list_len": int(match.group(2)),
            "seg_size": int(match.group(3)), "merges": int(match.group(4)),
            "merged": _ops(text, "merged operations:"), "discarded": _ops(text, "discarded operations:")}


def parse_ahi(lines):
    text = "\n".join(lines)
    parts = [{"cells": int(m.group(1)), "buffers": int(m.group(2))}
             for m in re.finditer(r"Hash table size (\d+),(?: used cells \d+,)? node heap has (\d+) buffer\(s\)", text)]
    match = re.search(r"([\d.]+) hash searches/s, ([\d.]+) non-hash searches/s", text)
    if not parts and not match:
        return None
    hash_rate = _float(match.group(1)) if match else None
    other = _float(match.group(2)) if match else None
    total = (hash_rate or 0) + (other or 0)
    return {"parts": len(parts), "cells": sum(p["cells"] for p in parts) if parts else None,
            "node_heap_buffers": sum(p["buffers"] for p in parts) if parts else None,
            "hash_searches_per_s": hash_rate, "non_hash_searches_per_s": other,
            "hit_pct": _round(100.0 * hash_rate / total, 1) if match and total > 0 else None}


_LOG_LABELS = {
    "log capacity": "capacity", "log capacity used": "capacity_used", "log sequence number": "lsn",
    "log buffer assigned up to": "buffer_assigned", "log buffer completed up to": "buffer_completed",
    "log written up to": "written_up_to", "log flushed up to": "flushed_up_to",
    "added dirty pages up to": "dirty_pages_added_up_to", "pages flushed up to": "pages_flushed_up_to",
    "last checkpoint at": "last_checkpoint", "log minimum file id is": "file_min_id",
    "log maximum file id is": "file_max_id", "max checkpoint age": "max_checkpoint_age",
    "checkpoint age target": "checkpoint_age_target", "modified age": "modified_age",
    "checkpoint age": "checkpoint_age_printed",
}


def parse_log(lines):
    out = {k: None for k in set(_LOG_LABELS.values())}
    out.update({"log_ios": None, "log_ios_per_s": None, "pending_log_flushes": None, "pending_checkpoint_writes": None})
    for raw in lines:
        line = raw.strip()
        match = re.match(r"^([A-Za-z][A-Za-z ]*?)\s+(\d+)$", line)
        if match and match.group(1).lower() in _LOG_LABELS:
            out[_LOG_LABELS[match.group(1).lower()]] = int(match.group(2))
            continue
        match = re.match(r"^(\d+) log i/o's done, ([\d.]+) log i/o's/second", line)
        if match:
            out["log_ios"], out["log_ios_per_s"] = int(match.group(1)), _float(match.group(2))
            continue
        match = re.match(r"^(\d+) pending log (?:flushes|writes), (\d+) pending chkp writes", line)
        if match:
            out["pending_log_flushes"], out["pending_checkpoint_writes"] = int(match.group(1)), int(match.group(2))
    lsn, checkpoint = out["lsn"], out["last_checkpoint"]
    out["checkpoint_age"] = lsn - checkpoint if lsn is not None and checkpoint is not None else None
    out["unflushed"] = lsn - out["flushed_up_to"] if lsn is not None and out["flushed_up_to"] is not None else None
    out["dirty_age"] = lsn - out["pages_flushed_up_to"] \
        if lsn is not None and out["pages_flushed_up_to"] is not None else None
    return out


def _pool_block(lines):
    text = "\n".join(lines)
    out = {}

    def num(pattern, key, cast=int):
        match = re.search(pattern, text, re.M)
        out[key] = cast(match.group(1)) if match else None

    num(r"^Buffer pool size\s+(\d+)", "pool_size_pages")
    num(r"^Buffer pool size, bytes\s+(\d+)", "pool_size_bytes")
    num(r"^Free buffers\s+(\d+)", "free_pages")
    num(r"^Database pages\s+(\d+)", "database_pages")
    num(r"^Old database pages\s+(\d+)", "old_database_pages")
    num(r"^Modified db pages\s+(\d+)", "modified_pages")
    num(r"^Percent of dirty pages\(LRU & free pages\): ([\d.]+)", "dirty_pct_printed", _float)
    num(r"^Max dirty pages percent: ([\d.]+)", "max_dirty_pct", _float)
    num(r"^Pending reads\s+(\d+)", "pending_reads")
    match = re.search(r"^Pending writes: LRU (\d+), flush list (\d+)(?:, single page (\d+))?", text, re.M)
    out["pending_writes"] = {"lru": int(match.group(1)), "flush_list": int(match.group(2)),
                             "single_page": _int(match.group(3))} if match else None
    match = re.search(r"^Pages made young (\d+), not young (\d+)", text, re.M)
    out["made_young"], out["not_young"] = (int(match.group(1)), int(match.group(2))) if match else (None, None)
    match = re.search(r"^([\d.]+) youngs/s, ([\d.]+) non-youngs/s", text, re.M)
    out["youngs_per_s"], out["non_youngs_per_s"] = (_float(match.group(1)), _float(match.group(2))) \
        if match else (None, None)
    match = re.search(r"^Pages read (\d+), created (\d+), written (\d+)", text, re.M)
    out["pages_read"], out["pages_created"], out["pages_written"] = (int(g) for g in match.groups()) \
        if match else (None, None, None)
    match = re.search(r"^([\d.]+) reads/s, ([\d.]+) creates/s, ([\d.]+) writes/s", text, re.M)
    out["reads_per_s"], out["creates_per_s"], out["writes_per_s"] = (_float(g) for g in match.groups()) \
        if match else (None, None, None)
    match = re.search(r"^Buffer pool hit rate (\d+) / 1000, young-making rate (\d+) / 1000 not (\d+) / 1000",
                      text, re.M)
    out["hit_rate_per_1000"], out["young_per_1000"], out["not_young_per_1000"] = (int(g) for g in match.groups()) \
        if match else (None, None, None)
    out["no_page_gets"] = "No buffer pool page gets since the last printout" in text
    match = re.search(r"^Pages read ahead ([\d.]+)/s, evicted without access ([\d.]+)/s, Random read ahead "
                      r"([\d.]+)/s", text, re.M)
    out["read_ahead_per_s"], out["evicted_without_access_per_s"], out["random_read_ahead_per_s"] = \
        (_float(g) for g in match.groups()) if match else (None, None, None)
    match = re.search(r"^LRU len: (\d+), unzip_LRU len: (\d+)", text, re.M)
    out["lru_len"], out["unzip_lru_len"] = (int(match.group(1)), int(match.group(2))) if match else (None, None)
    match = re.search(r"^I/O sum\[(\d+)\]:cur\[(\d+)\], unzip sum\[(\d+)\]:cur\[(\d+)\]", text, re.M)
    out["io_sum"], out["io_cur"], out["unzip_sum"], out["unzip_cur"] = (int(g) for g in match.groups()) \
        if match else (None, None, None, None)
    return out


def parse_buffer_pool(lines, instance_lines=()):
    text = "\n".join(lines)
    out = {"total_large_memory": None, "dictionary_memory": None}
    match = re.search(r"Total large memory allocated (\d+)", text)
    if match:
        out["total_large_memory"] = int(match.group(1))
    match = re.search(r"Dictionary memory allocated (\d+)", text)
    if match:
        out["dictionary_memory"] = int(match.group(1))
    out["pool"] = _pool_block(lines)
    instances, current = [], None
    for line in instance_lines:
        match = re.match(r"^---BUFFER POOL (\d+)", line.strip())
        if match:
            current = (int(match.group(1)), [])
            instances.append(current)
        elif current is not None:
            current[1].append(line)
    out["instances"] = [dict(_pool_block(body), instance=n) for n, body in instances]
    return out


def parse_row_operations(lines):
    text = "\n".join(lines)
    out = {"queries_inside": None, "queries_in_queue": None, "read_views": None, "reserved_extents": None,
           "main_thread_state": None, "process_id": None, "rows": None, "rows_per_s": None,
           "system_rows": None, "system_rows_per_s": None}
    match = re.search(r"(\d+) queries inside InnoDB, (\d+) queries in queue", text)
    if match:
        out["queries_inside"], out["queries_in_queue"] = int(match.group(1)), int(match.group(2))
    match = re.search(r"(\d+) read views open inside InnoDB", text)
    if match:
        out["read_views"] = int(match.group(1))
    match = re.search(r"(\d+) tablespace extents now reserved", text)
    if match:
        out["reserved_extents"] = int(match.group(1))
    match = re.search(r"Process ID=(\d+), Main thread ID=\S+\s*, state=?\s*(.*)$", text, re.M) or \
        re.search(r"Main thread process no\. (\d+), id \d+, state: (.*)$", text, re.M)
    if match:
        out["process_id"], out["main_thread_state"] = int(match.group(1)), match.group(2).strip() or None
    else:
        match = re.search(r"^state: (.*)$", text, re.M)
        if match:
            out["main_thread_state"] = match.group(1).strip() or None
    pending = None
    for line in lines:
        match = re.match(r"^Number of (system )?rows inserted (\d+), updated (\d+), deleted (\d+), read (\d+)", line)
        if match:
            key = "system_rows" if match.group(1) else "rows"
            out[key] = {"inserted": int(match.group(2)), "updated": int(match.group(3)),
                        "deleted": int(match.group(4)), "read": int(match.group(5))}
            pending = key + "_per_s"
            continue
        match = re.match(r"^([\d.]+) inserts/s, ([\d.]+) updates/s, ([\d.]+) deletes/s, ([\d.]+) reads/s", line)
        if match and pending:
            out[pending] = {"inserted": _float(match.group(1)), "updated": _float(match.group(2)),
                            "deleted": _float(match.group(3)), "read": _float(match.group(4))}
            pending = None
    return out


def parse_status(text, engine="mysql"):
    """Parse the whole SHOW ENGINE INNODB STATUS text: {header, sections: {key:
    parsed data}, found, unknown, ended, truncated_by_server}. Pure."""
    from .locks import parse_latest_deadlock
    header_lines, sections, ended = split_sections(text)
    found, unknown, bodies = [], [], {}
    for title, body in sections:
        key = SECTION_KEYS.get(title)
        if key is None:
            unknown.append(title)
            continue
        found.append(key)
        bodies.setdefault(key, body)
    parsed = {}
    if "background" in bodies:
        parsed["background"] = parse_background(bodies["background"])
    if "semaphores" in bodies:
        parsed["semaphores"] = parse_semaphores(bodies["semaphores"])
    if "foreign_key_error" in bodies:
        parsed["foreign_key_error"] = parse_foreign_key_error(bodies["foreign_key_error"], engine)
    if "deadlock" in bodies:
        deadlock = parse_latest_deadlock(text, engine)
        if deadlock is not None:
            deadlock = {k: v for k, v in deadlock.items() if k not in ("raw", "raw_truncated")}
        parsed["deadlock"] = deadlock
    if "transactions" in bodies:
        parsed["transactions"] = parse_transactions(bodies["transactions"], engine)
    if "file_io" in bodies:
        parsed["file_io"] = parse_file_io(bodies["file_io"])
    ibuf_lines = bodies.get("insert_buffer_ahi") or bodies.get("insert_buffer")
    if ibuf_lines is not None:
        parsed["insert_buffer"] = parse_insert_buffer(ibuf_lines)
    ahi_lines = bodies.get("insert_buffer_ahi") or bodies.get("adaptive_hash_index")
    if ahi_lines is not None:
        parsed["adaptive_hash_index"] = parse_ahi(ahi_lines)
    if "log" in bodies:
        parsed["log"] = parse_log(bodies["log"])
    if "buffer_pool" in bodies:
        parsed["buffer_pool"] = parse_buffer_pool(bodies["buffer_pool"], bodies.get("buffer_pool_instances") or ())
    if "row_operations" in bodies:
        parsed["row_operations"] = parse_row_operations(bodies["row_operations"])
    return {"header": _header(header_lines), "sections": parsed, "found": found, "unknown": unknown,
            "ended": ended, "truncated_by_server": TRUNCATED_MARK in (text or "")}


# ---- post-processing ----------------------------------------------------------------

def _item(key, label, value=None, unit="", text=None, status=None, rule=None):
    return {"key": key, "label": label, "value": value, "unit": unit,
            "text": text if text is not None else ("—" if value is None else _count_text(value)),
            "status": status, "rule": rule}


def _section(key, title, present, status, summary, items=(), data=None, links=()):
    graded = [i["status"] for i in items if i and i.get("status") in SEVERITY]
    if status is None:
        status = max(graded, key=lambda s: SEVERITY[s]) if graded else "info"
    return {"key": key, "title": title, "present": present, "status": status, "summary": summary,
            "items": [i for i in items if i], "data": data, "links": [_LINK[l] for l in links]}


class _Context(object):
    def __init__(self, server, variables, status, parsed, extra):
        self.server = server
        self.engine = server.engine
        self.vars = variables
        self.status = status
        self.parsed = parsed
        self.window = parsed["header"]["window_s"]
        self.extra = extra

    def var(self, name):
        return self.vars.get(name)

    def page_size(self):
        return _int(self.var("innodb_page_size")) or 16384


def _semaphores(c):
    data = c.parsed["sections"].get("semaphores")
    if data is None:
        return _section("semaphores", "Semaphores", False, "unknown", "The status had no SEMAPHORES section.")
    if c.engine == "mariadb" and data["empty"]:
        return _section("semaphores", "Semaphores", True, "info",
                        "MariaDB leaves this section empty (since 10.6), so latch waits are not shown here.",
                        data=data)
    waits = [w for w in data["waits"] if not w["ended"]]
    longest = max([w["seconds"] for w in waits if w["seconds"] is not None] or [None]) if waits else None
    status = _grade_seconds(longest, SEMAPHORE_WAIT_S) if waits else "ok"
    if waits:
        summary = "%d thread%s waiting for an InnoDB latch, the longest for %s." % (
            len(waits), "" if len(waits) == 1 else "s", _duration_text(longest or 0))
    else:
        summary = "No thread is waiting for an InnoDB latch."
    items = [
        _item("latch_waits", "Threads waiting for a latch", len(waits),
              text="%d%s" % (len(waits), ", longest %s" % _duration_text(longest) if longest is not None else ""),
              status=status, rule=RULES["semaphore"]),
        _item("reservation_count", "OS wait array reservations (since startup)", data["reservation_count"]),
        _item("signal_count", "OS wait array signals (since startup)", data["signal_count"]),
    ]
    zeros = data["spins"] and all(not any(v.values()) for v in data["spins"].values())
    if c.engine == "mysql" and zeros:
        data["spin_counters_note"] = ("MySQL no longer counts RW-latch spins and prints them as 0, so they are "
                                      "not shown.")
    elif data["spins"]:
        for name, value in sorted(data["spins"].items()):
            items.append(_item("spins_" + name.lower().replace("-", "_"), "%s spins / rounds / OS waits" % name,
                               value["os_waits"], text="%s / %s / %s" % (
                                   _count_text(value["spins"]), _count_text(value["rounds"]),
                                   _count_text(value["os_waits"]))))
    return _section("semaphores", "Semaphores", True, status, summary, items, data,
                    links=("health",) if waits else ())


def _short_table(name):
    return (name or "").replace("`", "") or None


def _foreign_key_error(c):
    data = c.parsed["sections"].get("foreign_key_error")
    if data is None:
        native = (c.var("innodb_native_foreign_keys") or "").upper()
        if c.engine == "mysql" and c.server.version_tuple >= (9, 6, 0) and native in ("OFF", "0", ""):
            summary = ("MySQL 9.6 and later check foreign keys in the SQL layer by default "
                       "(innodb_native_foreign_keys=OFF), so InnoDB does not record foreign key errors here.")
        else:
            summary = "No foreign key error since the server started."
        return _section("foreign_key_error", "Latest foreign key error", False, "info", summary)
    child, parent = _short_table(data["child_table"] or data["table"]), _short_table(data["parent_table"])
    constraint = data["constraint"]
    if data["kind"] == "no_parent_row":
        cause = "an insert or update in %s had no matching row in %s" % (child or "the child table",
                                                                          parent or "the parent table")
    elif data["kind"] == "child_rows_exist":
        cause = "a %s would leave rows in %s without a parent" % (
            (data["operation"] or "delete or update in the parent table"), child or "the child table")
    elif data["kind"] == "missing_table":
        cause = "the table %s, or its index, does not exist" % (parent or "it references")
    else:
        cause = data["message"] or "see the constraint"
    summary = "%s: %s%s." % (data["at"] or "Last error", cause,
                             " (constraint %s)" % constraint if constraint else "")
    data["cause"] = cause[:1].upper() + cause[1:] + "."
    items = [
        _item("at", "When (server time)", data["at"], text=data["at"] or "—"),
        _item("table", "Table", data["table"], text=_short_table(data["table"]) or "—"),
        _item("constraint", "Constraint", constraint, text="%s%s" % (
            constraint or "—", " (%s) references %s (%s)%s" % (
                ", ".join(data["columns"] or []), _short_table(data["referenced_table"]),
                ", ".join(data["referenced_columns"] or []),
                " " + data["on_actions"] if data["on_actions"] else "") if data["referenced_table"] else "")),
        _item("cause", "Cause", data["kind"], text=data["cause"]),
        _item("statement", "Statement", data["statement"], text=" ".join((data["statement"] or "—").split())[:200]),
        _item("connection", "Connection", data["connection_id"], text="%s%s" % (
            data["connection_id"] or "—", " (%s@%s)" % (data["user"], data["host"]) if data["user"] else "")),
    ]
    return _section("foreign_key_error", "Latest foreign key error", True, "info", summary, items, data)


def _deadlock(c):
    data = c.parsed["sections"].get("deadlock")
    count = c.extra.get("deadlocks")
    count_item = _item("deadlocks", "Deadlocks since startup", count, text=_count_text(count)
                       if count is not None else "—") if count is not None else None
    if not data:
        return _section("deadlock", "Latest deadlock", False, "info",
                        "No deadlock since the server started." if not count else
                        "%s deadlocks since startup; none is printed now." % _count_text(count),
                        [count_item], links=("locks",))
    tables = []
    for trx in data["transactions"]:
        for lock in (trx["holds"] or []) + ([trx["waiting_for"]] if trx["waiting_for"] else []):
            name = "%s.%s" % (lock.get("schema"), lock.get("table")) if lock.get("table") else None
            if name and name not in tables:
                tables.append(name)
    victim = next((t for t in data["transactions"] if t["victim"]), None)
    summary = "%s: %d transactions on %s; InnoDB rolled back %s." % (
        data["detected_at"] or "Last deadlock", len(data["transactions"]), ", ".join(tables) or "unknown tables",
        "transaction %s (connection %s)" % (victim["trx_id"], victim["connection_id"] or "?") if victim
        else "one of them")
    items = [
        _item("detected_at", "When (server time)", data["detected_at"], text=data["detected_at"] or "—"),
        _item("transactions", "Transactions in the cycle", len(data["transactions"])),
        _item("tables", "Tables", len(tables), text=", ".join(tables) or "—"),
        _item("victim", "Rolled back", data["victim"], text="transaction %s, connection %s" % (
            victim["trx_id"], victim["connection_id"] or "?") if victim else "—"),
        count_item,
    ]
    return _section("deadlock", "Latest deadlock", True, "info", summary, items, data, links=("locks",))


def _transactions(c):
    data = c.parsed["sections"].get("transactions")
    if data is None:
        return _section("transactions", "Transactions and purge", False, "unknown",
                        "The status had no TRANSACTIONS section.", links=("sessions",))
    every = data.pop("transactions")
    started = [t for t in every if t["state"] not in ("not started", None)]
    started.sort(key=lambda t: -(t["active_s"] or 0))
    waiting = [t for t in started if t["lock_wait"]]
    oldest = started[0] if started else None
    oldest_any = c.extra.get("oldest_trx")
    data.update({
        "listed": len(every), "not_started": sum(1 for t in every if t["state"] == "not started"),
        "active": len(started), "lock_waits": len(waiting),
        "rolling_back": sum(1 for t in started if t["rolling_back"]),
        "prepared": sum(1 for t in started if t["state"] == "PREPARED"),
        "transactions": started[:TOP_TRANSACTIONS], "transactions_truncated": len(started) > TOP_TRANSACTIONS,
        "oldest": oldest, "oldest_any": oldest_any,
    })
    hll = data["history_list_length"]
    hstatus, hrule = history_status(hll)
    age = oldest["active_s"] if oldest else None
    owner = oldest["connection_id"] if oldest else None
    if oldest_any and oldest_any.get("age_s") is not None and (age is None or oldest_any["age_s"] > age):
        age, owner = oldest_any["age_s"], oldest_any.get("connection_id")
    data["oldest_age_s"], data["oldest_connection_id"] = age, owner
    age_status = _grade_seconds(age, TRX_AGE_S) if age is not None else "ok"
    purge = data["purge"] or {}
    purge_state = purge.get("state")
    purge_status = None
    if purge_state:
        purge_status = "ok" if purge_state in ("running", "running but idle") else "watch"
    items = [
        _item("history_list_length", "History list length", hll, status=hstatus if hll is not None else "unknown",
              rule=hrule),
        _item("oldest_trx", "Oldest active transaction", age, "s", text="%s%s" % (
            _duration_text(age) if age is not None else "none",
            " (connection %s)" % owner if age is not None and owner else ""),
            status=age_status, rule=RULES["trx_age"]),
        _item("lock_waits", "Transactions waiting for a lock", len(waiting),
              status="watch" if waiting else "ok", rule=RULES["lock_wait"]),
        _item("active", "Active transactions listed", len(started)),
        _item("purge_state", "Purge", purge_state, text=purge_state or "—", status=purge_status,
              rule=RULES["purge_state"] if purge_status else None),
        _item("trx_id_counter", "Trx id counter", data["trx_id_counter"]),
    ]
    parts = []
    if hll is not None:
        parts.append("history list length %s" % _count_text(hll))
    if age is not None:
        parts.append("oldest transaction open %s" % _duration_text(age))
    if waiting:
        parts.append("%d waiting for a lock" % len(waiting))
    summary = ("%d active transaction%s" % (len(started), "" if len(started) == 1 else "s")) + (
        "; " + ", ".join(parts) if parts else "") + "."
    viewers = [t for t in started if t["read_view"] is not None]
    holder = viewers[0] if viewers else None
    data["oldest_read_view"] = {"trx_id": holder["trx_id"], "connection_id": holder["connection_id"],
                                "active_s": holder["active_s"]} if holder else None
    if hstatus in ("watch", "problem"):
        if holder is not None and holder["active_s"] is not None and holder["active_s"] >= TRX_AGE_S[0]:
            summary += (" Transaction %s%s has kept a read view for %s, so purge cannot remove the undo logs that "
                        "view may still need." % (holder["trx_id"], " (connection %s)" % holder["connection_id"]
                                                  if holder["connection_id"] else "",
                                                  _duration_text(holder["active_s"])))
        elif age is not None and age_status != "ok":
            summary += (" If the oldest transaction runs under REPEATABLE READ (or a long statement is running), "
                        "its read view keeps purge from removing undo logs; Sessions shows it.")
    if c.engine == "mysql":
        data["list_note"] = ("MySQL lists read-write transactions and sessions' not-started ones; a read-only "
                             "transaction with an old read view is not listed (oldest_any comes from "
                             "information_schema.INNODB_TRX and includes it).")
    links = ["sessions"] + (["locks"] if waiting else [])
    return _section("transactions", "Transactions and purge", True, None, summary, items, data, links)


def _file_io(c):
    data = c.parsed["sections"].get("file_io")
    if data is None:
        return _section("file_io", "File I/O", False, "unknown", "The status had no FILE I/O section.")
    pending = data["pending_total"]
    p = data["pending"]
    judged = [p[k] for k in ("normal_aio_reads", "ibuf_aio_reads", "reads", "sync_ios", "fsync_log",
                             "fsync_buffer_pool", "fsync") if p[k] is not None]
    status = "watch" if any(judged) else "ok"
    data["pending_judged"] = sum(judged) if judged else None
    rates = data["rates"]
    summary = "%s pending I/O request%s" % (_count_text(pending) if pending else "No",
                                            "" if pending == 1 else "s")
    if rates["reads_per_s"] is not None:
        summary += "; %s reads/s, %s writes/s, %s fsyncs/s." % (
            _count_text(rates["reads_per_s"]), _count_text(rates["writes_per_s"]), _count_text(rates["fsyncs_per_s"]))
    else:
        summary += "; no per-second rates for this window."
    pending_parts = ["%s: %s" % (label, _count_text(v)) for v, label in (
        (p["normal_aio_reads"], "aio reads"), (p["aio_writes"], "aio writes"),
        (p["ibuf_aio_reads"], "change buffer aio reads"), (p["log_ios"], "log I/O"), (p["sync_ios"], "sync I/O"),
        (p["reads"], "reads"), (p["writes"], "writes"), (p["fsync_log"], "redo log fsyncs"),
        (p["fsync_buffer_pool"], "data file fsyncs"), (p["fsync"], "fsyncs")) if v]
    items = [
        _item("pending", "Pending I/O now", pending, text=", ".join(pending_parts) if pending_parts else
              ("0" if pending is not None else "—"), status=status if pending is not None else "unknown",
              rule=RULES["pending_io"]),
        _item("reads_per_s", "OS file reads per second", rates["reads_per_s"], "/s"),
        _item("writes_per_s", "OS file writes per second", rates["writes_per_s"], "/s"),
        _item("fsyncs_per_s", "OS fsyncs per second", rates["fsyncs_per_s"], "/s"),
        _item("avg_bytes_per_read", "Average bytes per read", rates["avg_bytes_per_read"], "B",
              text=_bytes_text(rates["avg_bytes_per_read"]) if rates["avg_bytes_per_read"] is not None else "—"),
        _item("os_totals", "OS reads / writes / fsyncs (since startup)", data["os"]["reads"], text="%s / %s / %s" % (
            _count_text(data["os"]["reads"]), _count_text(data["os"]["writes"]), _count_text(data["os"]["fsyncs"]))),
    ]
    return _section("file_io", "File I/O", True, status if pending is not None else "info", summary, items, data,
                    links=("io",))


def _change_buffer(c):
    data = c.parsed["sections"].get("insert_buffer")
    setting = c.var("innodb_change_buffering")
    if c.engine == "mariadb" and c.server.version_tuple >= (11, 0, 0):
        return _section("insert_buffer", "Change buffer", False, "info",
                        "MariaDB 11.0 and later have no change buffer.", data=None)
    if data is None:
        summary = ("The change buffer is empty." if c.engine == "mariadb" else "The status had no change buffer "
                   "lines.")
        return _section("insert_buffer", "Change buffer", False, "info", summary,
                        [_item("setting", "innodb_change_buffering", setting, text=setting or "—")])
    data["setting"] = setting
    if c.engine == "mysql":
        data["default"] = "none" if (8, 4, 0) <= c.server.version_tuple < (9, 5, 0) else "all"
    else:
        data["default"] = "none"
    merged = data["merged"] or {}
    summary = "%s page%s, %s merge%s since startup; innodb_change_buffering=%s (default %s on this version)." % (
        _count_text(data["size_pages"]), "" if data["size_pages"] == 1 else "s", _count_text(data["merges"]),
        "" if data["merges"] == 1 else "s", setting or "?", data["default"])
    items = [
        _item("setting", "innodb_change_buffering", setting, text="%s (default %s)" % (setting or "?", data["default"])),
        _item("size", "Size (pages)", data["size_pages"]),
        _item("free_list", "Free list length (pages)", data["free_list_len"]),
        _item("merges", "Merges (since startup)", data["merges"]),
        _item("merged_ops", "Merged inserts / delete marks / deletes", merged.get("insert"), text="%s / %s / %s" % (
            _count_text(merged.get("insert")), _count_text(merged.get("delete_mark")),
            _count_text(merged.get("delete")))) if merged else None,
    ]
    return _section("insert_buffer", "Change buffer", True, "info", summary, items, data)


def _ahi(c):
    data = c.parsed["sections"].get("adaptive_hash_index")
    setting = (c.var("innodb_adaptive_hash_index") or "").upper()
    on = setting in ("ON", "1")
    default = "OFF"
    if c.engine == "mysql" and c.server.version_tuple < (8, 4, 0):
        default = "ON"
    if data is None or not on:
        if setting and not on:
            summary = "The adaptive hash index is off (innodb_adaptive_hash_index=%s; the default is %s on this " \
                      "version)." % (setting, default)
        elif on:
            summary = "innodb_adaptive_hash_index is ON, but the status printed no adaptive hash index lines."
        else:
            summary = "The status had no adaptive hash index lines."
        return _section("adaptive_hash_index", "Adaptive hash index", data is not None, "info", summary,
                        [_item("setting", "innodb_adaptive_hash_index", setting or None, text=setting or "—")],
                        data)
    data["setting"], data["default"] = setting, default
    hit = data["hit_pct"]
    if data["hash_searches_per_s"] is None:
        summary = "On; no per-second search rates for this window."
    elif hit is None:
        summary = "On, but no index searches in the window."
    else:
        summary = "On: %.1f%% of index searches in the window used the hash index (%s hash, %s other per second)." \
            % (hit, _count_text(data["hash_searches_per_s"]), _count_text(data["non_hash_searches_per_s"]))
    items = [
        _item("setting", "innodb_adaptive_hash_index", setting, text="ON (default %s)" % default),
        _item("hit_pct", "Searches using the hash index", hit, "%", text="%.1f%%" % hit if hit is not None else "—"),
        _item("hash_per_s", "Hash searches per second", data["hash_searches_per_s"], "/s"),
        _item("non_hash_per_s", "Other index searches per second", data["non_hash_searches_per_s"], "/s"),
        _item("cells", "Hash table cells (all parts)", data["cells"]),
        _item("buffers", "Node heap buffers (pages)", data["node_heap_buffers"]),
    ]
    return _section("adaptive_hash_index", "Adaptive hash index", True, "info", summary, items, data)


def _redo_points(c, capacity):
    """(capacity used for the percentage, watch point, problem point, label) in bytes."""
    if c.engine == "mysql":
        if not capacity:
            return None, None, None
        return capacity, REDO_POINTS_MYSQL[0] * capacity, REDO_POINTS_MYSQL[1] * capacity
    max_age = _int(c.status.get("Innodb_checkpoint_max_age"))
    if max_age:
        return capacity, REDO_POINTS_MARIADB[0] * max_age, REDO_POINTS_MARIADB[1] * max_age
    if capacity:
        return capacity, 0.71 * capacity, 0.81 * capacity
    return None, None, None


def _log(c):
    data = c.parsed["sections"].get("log")
    if data is None:
        return _section("log", "Redo log and checkpoint", False, "unknown", "The status had no LOG section.",
                        links=("health",))
    from .config_review import redo_capacity
    resized = _int(c.status.get("Innodb_redo_log_capacity_resized")) if c.engine == "mysql" else None
    if resized:
        capacity, source = resized, "Innodb_redo_log_capacity_resized"
    elif c.engine == "mysql" and data.get("capacity_used"):
        capacity, source = data["capacity_used"], "Log capacity used in the status"
    else:
        capacity, source = redo_capacity(c.engine, c.vars, c.status)
    data["capacity_bytes"], data["capacity_source"] = capacity, source
    age = data["checkpoint_age"]
    _, watch_at, problem_at = _redo_points(c, capacity)
    pct = 100.0 * age / capacity if age is not None and capacity else None
    data["checkpoint_age_pct"] = _round(pct, 1)
    data["watch_at_bytes"] = int(watch_at) if watch_at else None
    data["problem_at_bytes"] = int(problem_at) if problem_at else None
    if age is None or watch_at is None:
        status = "unknown"
    else:
        status = "problem" if age >= problem_at else "watch" if age >= watch_at else "ok"
    rule = RULES["redo_mysql" if c.engine == "mysql" else "redo_mariadb"]
    if pct is not None:
        summary = "Checkpoint age %s, %.0f%% of the %s redo capacity%s." % (
            _bytes_text(age), pct, _bytes_text(capacity),
            {"ok": "", "watch": ": InnoDB is flushing harder to free redo space",
             "problem": ": flushing is forced and writes may stall"}.get(status, ""))
    else:
        summary = "Checkpoint age %s; the redo capacity is unknown." % (_bytes_text(age) if age is not None else "?")
    items = [
        _item("checkpoint_age", "Checkpoint age", age, "B", text="%s%s" % (
            _bytes_text(age) if age is not None else "—", " (%.0f%% of capacity)" % pct if pct is not None else ""),
            status=status, rule=rule),
        _item("capacity", "Redo capacity", capacity, "B", text="%s%s" % (
            _bytes_text(capacity) if capacity else "—", " (%s)" % source if source else "")),
        _item("unflushed", "Redo not yet flushed to disk", data["unflushed"], "B",
              text=_bytes_text(data["unflushed"]) if data["unflushed"] is not None else "—"),
        _item("lsn", "Log sequence number", data["lsn"]),
        _item("last_checkpoint", "Last checkpoint at", data["last_checkpoint"]),
        _item("log_ios_per_s", "Log I/O per second", data["log_ios_per_s"], "/s") if data["log_ios_per_s"] is not None
        else None,
        _item("pending", "Pending log flushes / checkpoint writes", data["pending_log_flushes"], text="%s / %s" % (
            _count_text(data["pending_log_flushes"]), _count_text(data["pending_checkpoint_writes"])))
        if data["pending_log_flushes"] is not None else None,
    ]
    return _section("log", "Redo log and checkpoint", True, status, summary, items, data, links=("health", "config"))


def _hit_status(pool, window):
    """(status, estimated page requests) for the window's hit rate (see RULES['hit_rate'])."""
    hit, reads = pool.get("hit_rate_per_1000"), pool.get("reads_per_s")
    if hit is None:
        return ("ok" if pool.get("no_page_gets") else "unknown"), 0
    if hit > 1000:
        return "unknown", None
    if hit == 1000:
        return "ok", None
    if reads is None or not window:
        return "unknown", None
    requests = reads * window / (1 - hit / 1000.0)
    if requests < MIN_PAGE_REQUESTS:
        return "ok", requests
    pct = hit / 10.0
    if pct < HIT_RATIO_PCT[1] and reads >= MIN_DISK_READS_PER_S:
        return "problem", requests
    if pct < HIT_RATIO_PCT[0]:
        return "watch", requests
    return "ok", requests


def _derive_pool(pool, page):
    size, free = pool.get("pool_size_pages"), pool.get("free_pages")
    data_pages, modified = pool.get("database_pages"), pool.get("modified_pages")
    hit = pool.get("hit_rate_per_1000")
    # MySQL computes the rate unsigned: more pages read than requested wraps past 1000.
    pool["hit_pct"] = _round(hit / 10.0, 1) if hit is not None and hit <= 1000 else None
    base = (data_pages or 0) + (free or 0)
    pool["dirty_pct"] = _round(100.0 * modified / base, 1) if modified is not None and base else None
    pool["free_pct"] = _round(100.0 * free / size, 1) if free is not None and size else None
    pool["size_bytes"] = size * page if size is not None else None
    return pool


def _buffer_pool(c):
    data = c.parsed["sections"].get("buffer_pool")
    if data is None:
        return _section("buffer_pool", "Buffer pool and memory", False, "unknown",
                        "The status had no BUFFER POOL AND MEMORY section.", links=("health",))
    page = c.page_size()
    pool = _derive_pool(data["pool"], page)
    for instance in data["instances"]:
        _derive_pool(instance, page)
    hit_status, requests = _hit_status(pool, c.window)
    pool["estimated_page_requests"] = int(requests) if requests else None
    max_dirty = _number(c.var("innodb_max_dirty_pages_pct")) or pool.get("max_dirty_pct") or 90
    dirty_status = None if pool["dirty_pct"] is None else ("watch" if pool["dirty_pct"] >= max_dirty else "ok")
    hit_text = "%.1f%%" % pool["hit_pct"] if pool["hit_pct"] is not None else (
        "no page requests in the window" if pool.get("no_page_gets") else
        "unknown (more pages read than requested)" if (pool.get("hit_rate_per_1000") or 0) > 1000 else "—")
    if requests is not None and pool["hit_pct"] is not None and pool["hit_pct"] < 100 \
            and requests < MIN_PAGE_REQUESTS:
        hit_text += " (too few requests to judge)"
    summary = "%s pool, %s free, %s dirty; hit rate %s%s." % (
        _bytes_text(pool["size_bytes"]) if pool["size_bytes"] else "?",
        "%.0f%%" % pool["free_pct"] if pool["free_pct"] is not None else "?",
        "%.1f%%" % pool["dirty_pct"] if pool["dirty_pct"] is not None else "?",
        "%.1f%%" % pool["hit_pct"] if pool["hit_pct"] is not None else "n/a",
        "; %s pages read/s from disk" % _count_text(pool["reads_per_s"]) if pool.get("reads_per_s") is not None
        else "")
    young = pool.get("young_per_1000")
    items = [
        _item("hit_pct", "Hit rate (this window)", pool["hit_pct"], "%", text=hit_text,
              status=hit_status if pool.get("hit_rate_per_1000") is not None else None, rule=RULES["hit_rate"]),
        _item("dirty_pct", "Dirty pages", pool["dirty_pct"], "%", text="%s (%s pages)" % (
            "%.1f%%" % pool["dirty_pct"] if pool["dirty_pct"] is not None else "—",
            _count_text(pool.get("modified_pages"))), status=dirty_status, rule=RULES["dirty"]),
        _item("free_pct", "Free pages", pool["free_pct"], "%", text="%s (%s pages)" % (
            "%.0f%%" % pool["free_pct"] if pool["free_pct"] is not None else "—", _count_text(pool.get("free_pages")))),
        _item("size", "Buffer pool size", pool["size_bytes"], "B", text="%s (%s pages)" % (
            _bytes_text(pool["size_bytes"]) if pool["size_bytes"] else "—", _count_text(pool.get("pool_size_pages")))),
        _item("reads_per_s", "Pages read from disk per second", pool.get("reads_per_s"), "/s"),
        _item("writes_per_s", "Pages written per second", pool.get("writes_per_s"), "/s"),
        _item("young", "Made young / not young per 1000 requests", young, text="%s / %s" % (
            _count_text(young), _count_text(pool.get("not_young_per_1000")))) if young is not None else None,
        _item("read_ahead", "Read ahead / evicted unused per second", pool.get("read_ahead_per_s"), "/s",
              text="%s / %s" % (_count_text(pool.get("read_ahead_per_s")),
                                _count_text(pool.get("evicted_without_access_per_s")))
              if pool.get("read_ahead_per_s") is not None else "—"),
        _item("pending", "Pending reads / writes (LRU, flush list)", pool.get("pending_reads"), text="%s / %s, %s" % (
            _count_text(pool.get("pending_reads")),
            _count_text((pool.get("pending_writes") or {}).get("lru")),
            _count_text((pool.get("pending_writes") or {}).get("flush_list")))),
        _item("dictionary_memory", "Dictionary memory", data["dictionary_memory"], "B",
              text=_bytes_text(data["dictionary_memory"]) if data["dictionary_memory"] is not None else "—"),
        _item("instances", "Buffer pool instances", len(data["instances"]) or 1),
    ]
    return _section("buffer_pool", "Buffer pool and memory", True, None, summary, items, data, links=("health",))


def _row_operations(c):
    data = c.parsed["sections"].get("row_operations")
    if data is None:
        return _section("row_operations", "Row operations", False, "unknown",
                        "The status had no ROW OPERATIONS section.")
    queue = data["queries_in_queue"]
    queue_status = None if queue is None else ("watch" if queue > 0 else "ok")
    rates = data["rows_per_s"] or {}
    if data["rows"] is not None and not rates:
        summary = "No per-second row rates for this window; %s read views open." % _count_text(data["read_views"])
    elif data["rows"] is not None:
        summary = "%s rows read, %s inserted, %s updated, %s deleted per second; %s read views open." % (
            _count_text(rates.get("read")), _count_text(rates.get("inserted")), _count_text(rates.get("updated")),
            _count_text(rates.get("deleted")), _count_text(data["read_views"]))
    else:
        summary = "%s read views open; main thread %s. MariaDB prints no row counts here (Health shows them)." % (
            _count_text(data["read_views"]), data["main_thread_state"] or "?")
    items = [
        _item("queue", "Queries inside InnoDB / in queue", queue, text="%s / %s" % (
            _count_text(data["queries_inside"]), _count_text(queue)), status=queue_status,
            rule=RULES["queue"] if queue_status else None) if queue is not None else None,
        _item("read_views", "Read views open", data["read_views"]),
        _item("rows_read", "Rows read per second", rates.get("read"), "/s") if rates else None,
        _item("rows_changed", "Rows inserted / updated / deleted per second", rates.get("inserted"), "/s",
              text="%s / %s / %s" % (_count_text(rates.get("inserted")), _count_text(rates.get("updated")),
                                    _count_text(rates.get("deleted")))) if rates else None,
        _item("main_thread", "Main thread state", data["main_thread_state"], text=data["main_thread_state"] or "—"),
    ]
    return _section("row_operations", "Row operations", True, None, summary, items, data, links=("health",))


def _background(c):
    data = c.parsed["sections"].get("background")
    if data is None:
        return _section("background", "Background thread", False, "info", "The status had no BACKGROUND THREAD section.")
    summary = "The master thread ran %s active and %s idle loops since startup." % (
        _count_text(data["active_loops"]), _count_text(data["idle_loops"]))
    items = [_item("active_loops", "Active loops", data["active_loops"]),
             _item("idle_loops", "Idle loops", data["idle_loops"]),
             # MySQL never increments this counter (always 0); MariaDB does.
             _item("log_flush_and_writes", "Log flushes and writes", data["log_flush_and_writes"])
             if c.engine == "mariadb" else None]
    return _section("background", "Background thread", True, "info", summary, items, data)


BUILDERS = {"semaphores": _semaphores, "transactions": _transactions, "deadlock": _deadlock,
            "foreign_key_error": _foreign_key_error, "file_io": _file_io, "insert_buffer": _change_buffer,
            "adaptive_hash_index": _ahi, "log": _log, "buffer_pool": _buffer_pool,
            "row_operations": _row_operations, "background": _background}


_RATE_KEYS = ("rates", "rows_per_s", "system_rows_per_s")


def _drop_rates(value):
    """Null every per-second figure (keys ending in _per_s, and the rate
    dicts) in parsed section data, in place."""
    if isinstance(value, dict):
        for key in list(value):
            if key.endswith("_per_s") or key == "hit_pct" and "hash_searches_per_s" in value:
                value[key] = None
            elif key in _RATE_KEYS and isinstance(value[key], dict):
                value[key] = {k: None for k in value[key]} if key == "rates" else None
            else:
                _drop_rates(value[key])
    elif isinstance(value, list):
        for item in value:
            _drop_rates(item)


def _flag_text(section):
    item = next((i for i in section["items"] if i.get("status") == section["status"]), None)
    if item is None:
        return section["title"]
    return "%s, %s %s" % (section["title"], item["label"][:1].lower() + item["label"][1:], item["text"])


def compute_report(text, variables, status, extra=None, raw=False):
    """The ``innodb-1.0`` sections from status *text* plus the variables and
    status read beside it. Pure: the server-reading part is innodb_report."""
    server = Server(variables, status)
    parsed = parse_status(text, server.engine)
    window = parsed["header"]["window_s"]
    if window is not None and window < 1:
        # InnoDB divides by the whole seconds since the previous printout plus
        # 0.001 s, so a sub-second window inflates every rate up to 1000 times.
        _drop_rates(parsed["sections"])
    c = _Context(server, variables, status, parsed, extra or {})
    sections = [BUILDERS[key](c) for key, _ in ORDER]
    notes = []
    header = parsed["header"]
    if window is not None and window < 1:
        notes.append("Another reader printed the status less than a second ago, so InnoDB's per-second averages "
                     "are meaningless (it divides by about 0.001 s) and are left out; refresh in a few seconds.")
    elif window is not None and window < MIN_WINDOW_S:
        notes.append("The per-second averages cover only the last %d s, since the previous reader of SHOW ENGINE "
                     "INNODB STATUS. InnoDB divides some of them by whole seconds, so over such a short window the "
                     "rates are overstated; refresh in a few seconds." % window)
    if not parsed["ended"]:
        notes.append("The status text ended early (the server cuts it at 1 MiB), so later sections may be missing.")
    if parsed["truncated_by_server"]:
        notes.append("The status reached 1 MiB, so InnoDB left out part of the transaction list "
                     "(Innodb_truncated_status_writes counts this).")
    if parsed["unknown"]:
        notes.append("Sections myflames does not read yet were skipped: %s." % ", ".join(parsed["unknown"]))
    missing = [title for key, title in ORDER if not next(s for s in sections if s["key"] == key)["present"]
               and key in ("transactions", "file_io", "log", "buffer_pool", "row_operations")]
    if missing:
        notes.append("Parsed partially: no %s section was found." % ", ".join(missing))
    notes.append("Each read of SHOW ENGINE INNODB STATUS restarts InnoDB's per-second averages for every reader. "
                 "Statuses are rules of thumb from one snapshot, not verdicts; values that stay high across "
                 "refreshes matter more than one reading.")
    counts = {s: 0 for s in STATUSES}
    for section in sections:
        counts[section["status"]] = counts.get(section["status"], 0) + 1
    worst = "ok"
    for section in sections:
        if SEVERITY.get(section["status"], -1) > SEVERITY[worst]:
            worst = section["status"]
    flagged = [s for s in sections if s["status"] in ("watch", "problem")]
    flagged.sort(key=lambda s: -SEVERITY[s["status"]])
    text_summary = ("%d need attention: %s." % (len(flagged), "; ".join(_flag_text(s) for s in flagged))
                    if flagged else "Nothing needs attention.")
    report = base_report(SCHEMA_VERSION, server)
    report.update({
        "available": True, "error": None, "status": worst,
        "header": header,
        "summary": {"status": worst, "problem": counts["problem"], "watch": counts["watch"], "ok": counts["ok"],
                    "info": counts["info"], "unknown": counts["unknown"], "text": text_summary},
        "sections": sections,
        "parse": {"complete": parsed["ended"] and not missing, "sections_found": parsed["found"],
                  "unknown_sections": parsed["unknown"], "truncated_by_server": parsed["truncated_by_server"]},
        "rules": dict(RULES), "notes": notes,
        "raw": {"text": _cap(text, RAW_CAP)[0], "truncated": len(text or "") > RAW_CAP} if raw else None,
    })
    return report


# ---- reading the server ---------------------------------------------------------------

def _needs_process(exc):
    text = str(exc)
    return "PROCESS" in text or "ERROR 1227" in text


def no_process_message(engine):
    return ("SHOW ENGINE INNODB STATUS needs the PROCESS privilege, which this account does not have. Grant it "
            "with: GRANT PROCESS ON *.* TO <user>; (it also lets the account see every session in the process "
            "list and information_schema.INNODB_TRX).")


def _unavailable(server, message):
    report = base_report(SCHEMA_VERSION, server)
    report.update({"available": False, "error": {"code": "no_process", "message": message}, "status": "unknown",
                   "header": {"printed_at": None, "window_s": None},
                   "summary": {"status": "unknown", "problem": 0, "watch": 0, "ok": 0, "info": 0, "unknown": 0,
                               "text": message},
                   "sections": [], "parse": {"complete": False, "sections_found": [], "unknown_sections": [],
                                             "truncated_by_server": False},
                   "rules": dict(RULES), "notes": [message], "raw": None})
    return report


def _extra(reader, server, status):
    """The oldest transaction from INNODB_TRX (every one, read-only too) and
    the deadlock count; both optional."""
    extra = {"deadlocks": _int(status.get("Innodb_deadlocks"))}
    try:
        from .sessions import SYSTEM_TIME_ZONE
        rows = reader.rows(SYSTEM_TIME_ZONE + INNODB_TAG + "SELECT trx_mysql_thread_id, "
                           "TIMESTAMPDIFF(SECOND, trx_started, NOW()), trx_state, "
                           "trx_is_read_only, trx_id FROM information_schema.INNODB_TRX "
                           "WHERE trx_mysql_thread_id <> CONNECTION_ID() ORDER BY trx_started, trx_id LIMIT 1")
        if rows and len(rows[0]) >= 5:
            extra["oldest_trx"] = {"connection_id": _int(rows[0][0]) or None, "age_s": _int(rows[0][1]),
                                   "state": rows[0][2], "read_only": (rows[0][3] or "") in ("1", "ON"),
                                   "trx_id": rows[0][4], "source": "information_schema.INNODB_TRX"}
    except ConnectorError as exc:
        if "ERROR 2002" in str(exc) or "ERROR 2013" in str(exc) or "timed out" in str(exc):
            raise
    if server.engine == "mysql":
        try:
            rows = reader.rows("SELECT COUNT, STATUS FROM information_schema.INNODB_METRICS "
                               "WHERE NAME = 'lock_deadlocks'")
            if rows and len(rows[0]) >= 2 and (rows[0][1] or "").lower() == "enabled":
                extra["deadlocks"] = _int(rows[0][0])
        except ConnectorError as exc:
            if "ERROR 2002" in str(exc) or "ERROR 2013" in str(exc) or "timed out" in str(exc):
                raise
    return extra


def innodb_report(conn, raw=False):
    """Return the ``innodb-1.0`` report. Read-only; see the module docstring.

    Without PROCESS the report has ``available: false`` and an ``error``
    with code ``no_process``. *raw* adds the status text as ``raw.text``.
    """
    if not isinstance(raw, bool):
        raise ValueError("raw must be true or false.")
    reader = Reader(conn, INNODB_TAG)
    try:
        variables = show_variables(reader, VARIABLES)
        status = reader.pairs("SHOW GLOBAL STATUS WHERE Variable_name IN (%s)"
                              % ", ".join("'%s'" % n for n in STATUS_NAMES))
    except ConnectorError as exc:
        raise ValueError("Could not read the server variables: " + reason(exc))
    server = Server(variables, status)
    try:
        rows = reader.rows("SHOW ENGINE INNODB STATUS")
    except ConnectorError as exc:
        if _needs_process(exc):
            return _unavailable(server, no_process_message(server.engine))
        raise ValueError("Could not read SHOW ENGINE INNODB STATUS: " + reason(exc))
    text = rows[0][2] if rows and len(rows[0]) > 2 else ""
    if not text or not text.strip():
        raise ValueError("SHOW ENGINE INNODB STATUS returned nothing. InnoDB may be in read-only mode "
                         "(innodb_read_only), which prints no monitor output.")
    extra = _extra(reader, server, status)
    return compute_report(text, variables, status, extra, raw=raw)


def at_or_worse(report, level):
    """Items, then sections without a graded item, at *level* or worse."""
    floor = SEVERITY[level]
    out = []
    for section in report["sections"]:
        if SEVERITY.get(section["status"], -1) < floor:
            continue
        items = [i for i in section["items"] if SEVERITY.get(i.get("status"), -1) >= floor]
        for item in items or [None]:
            out.append({"section": section["key"], "title": section["title"], "status":
                        item["status"] if item else section["status"],
                        "label": item["label"] if item else section["title"],
                        "text": item["text"] if item else section["summary"],
                        "rule": item["rule"] if item else None})
    return out


# ---- text output for the CLI ----------------------------------------------------

_MARK = {"ok": "ok", "watch": "WATCH", "problem": "PROBLEM", "info": "-", "unknown": "?"}


def format_text(report):
    import textwrap
    head = "%s %s" % ("MariaDB" if report["engine"] == "mariadb" else "MySQL", report["server_version"] or "?")
    header = report["header"]
    if header.get("printed_at"):
        head += ", status printed %s" % header["printed_at"]
    if header.get("window_s") is not None:
        head += ", averages over %d s" % header["window_s"]
    s = report["summary"]
    lines = [head, "Status: %s. %s" % (s["status"].upper() if s["status"] not in ("ok",) else "ok", s["text"]), ""]
    wrap = textwrap.TextWrapper(width=100, initial_indent="      ", subsequent_indent="      ")
    for section in report["sections"]:
        lines.append("%-7s %s" % (_MARK.get(section["status"], "?"), section["title"].upper()))
        lines += wrap.wrap(section["summary"])
        width = max([len(i["label"]) for i in section["items"]] or [0])
        for item in section["items"]:
            mark = _MARK.get(item.get("status"), "") if item.get("status") in ("watch", "problem") else ""
            lines.append(("        %s  %s  %s" % (item["label"].ljust(width), item["text"], mark)).rstrip())
        for item in section["items"]:
            if item.get("status") in ("watch", "problem") and item.get("rule"):
                lines += textwrap.wrap("Rule of thumb: %s." % item["rule"], width=100,
                                       initial_indent="        ", subsequent_indent="        ")
        if section["key"] == "transactions" and section["data"]:
            for trx in section["data"]["transactions"][:5]:
                lines.append("        trx %s%s, %s%s%s" % (
                    trx["trx_id"], " (connection %s)" % trx["connection_id"] if trx["connection_id"] else "",
                    "active %s" % _duration_text(trx["active_s"]) if trx["active_s"] is not None else trx["state"],
                    ", LOCK WAIT %s" % _duration_text(trx["wait_s"]) if trx["lock_wait"] and trx["wait_s"] is not None
                    else ", LOCK WAIT" if trx["lock_wait"] else "",
                    ": " + " ".join(trx["statement"].split())[:70] if trx["statement"] else ""))
        lines.append("")
    return "\n".join(lines)
