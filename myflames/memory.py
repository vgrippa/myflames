"""Read-only memory accounting, with bounded summaries and a two-sample window.

All SQL reads raw Performance Schema or information_schema tables. Query output
is bounded; grouping thread/account summaries still scans their instrument rows.
Each read has a 5 s timeout and the collection has a 30 s read budget, excluding
sleep. An event's high-water mark in the global and account tables is an upper
bound (current bytes plus the headroom summed over threads, accounts and hosts:
MySQL storage/perfschema/pfs_stat.cc normalize(), MariaDB pfs_stat.h), not a
recorded peak and never a server RAM peak; TRUNCATE resets it to current bytes.
Engine evidence with source citations: docs/DEVELOPER_REFERENCE.md, Memory.
"""
import hashlib
import json
import time

from .connector import ConnectorError
from .dbops import redact
from .perf_common import (Reader, _number, _round, base_report, check_window,
                          elapsed, lost_note, now, preflight, restarted, sample_twice)

SCHEMA_VERSION = "memory-1.0"
DEFAULT_WINDOW = 5
TAG = "/*myflames:memory*/ "
GLOBAL_TABLE = "performance_schema.memory_summary_global_by_event_name"
THREAD_TABLE = "performance_schema.memory_summary_by_thread_by_event_name"
ACCOUNT_TABLE = "performance_schema.memory_summary_by_account_by_event_name"
PROCESS_TABLE = "information_schema.PROCESSLIST"

# Exact event-name prefixes, checked against each engine's memory key
# registrations (MySQL sql/psi_memory_key.cc, 5.7 and MariaDB sql/mysqld.cc).
# Unknown instruments stay in Other. Join memory has three names: JOIN_CACHE
# fills only on MySQL 5.7 (8.0 before 8.0.20) and MariaDB; MySQL's hash join and
# BKA buffers are hash_join on 8.0.18 to 8.1 and hashed_operation from 8.2
# (8.4 and 9.x), which also serves hashed INTERSECT and EXCEPT. A prefix names
# what a category may hold, not what every engine registers: see MARIADB_NOTES.
CATEGORIES = (
    ("buffer_pool", "InnoDB buffer pool", ("memory/innodb/buf_buf_pool",)),
    ("temporary", "Temporary tables", ("memory/temptable/",)),
    ("sort", "Sort", ("memory/sql/Filesort",)),
    ("join", "Join and hash buffers", ("memory/sql/JOIN_CACHE", "memory/sql/hash_join", "memory/sql/hashed_operation")),
    ("network", "Network", ("memory/sql/NET::buff",)),
    ("performance_schema", "Performance Schema", ("memory/performance_schema/",)),
    ("other", "Other", ()),
)
LABELS = {key: label for key, label, _ in CATEGORIES}
#: MariaDB declares these memory keys but never registers them (commented out
#: in sql/mysqld.cc all_server_memory, 10.11 to 13.0), so their allocations are
#: not instrumented: Network never appears and Sort omits the sort buffer.
MARIADB_NOTES = {
    "sort": "MariaDB does not instrument the sort buffer (Filesort_buffer::sort_keys), so Sort leaves it out.",
    "network": "MariaDB does not instrument the network buffer (NET::buff), so Network is never collected.",
}
#: Categories the 25% rule judges: memory the workload grows. The buffer pool,
#: Performance Schema and Other (fixed buffers such as the InnoDB log buffer,
#: and allocations no prefix classifies) are context, never a finding.
WORKLOAD_CATEGORIES = ("temporary", "sort", "join", "network")


def category(event):
    for key, _, prefixes in CATEGORIES:
        if prefixes and event.startswith(prefixes):
            return key
    return "other"


_CATEGORY_CASE = "CASE " + " ".join(
    "WHEN %s THEN '%s'" % (" OR ".join("LEFT(EVENT_NAME, %d) = '%s'" % (len(p), p)
                                         for p in prefixes), key)
    for key, _, prefixes in CATEGORIES if prefixes) + " ELSE 'other' END"
TOTAL_SQL = ("SELECT SUM(CURRENT_NUMBER_OF_BYTES_USED), SUM(HIGH_NUMBER_OF_BYTES_USED), COUNT(*) FROM "
             + GLOBAL_TABLE + " WHERE EVENT_NAME LIKE 'memory/%' LIMIT 1")
CATEGORY_SQL = ("SELECT " + _CATEGORY_CASE + " AS category, SUM(CURRENT_NUMBER_OF_BYTES_USED), "
                "SUM(HIGH_NUMBER_OF_BYTES_USED) FROM " + GLOBAL_TABLE +
                " WHERE EVENT_NAME LIKE 'memory/%' GROUP BY category LIMIT 7")
EVENT_SQL = ("SELECT EVENT_NAME, CURRENT_NUMBER_OF_BYTES_USED, HIGH_NUMBER_OF_BYTES_USED FROM " + GLOBAL_TABLE +
             " WHERE EVENT_NAME LIKE 'memory/%' ORDER BY CURRENT_NUMBER_OF_BYTES_USED DESC, EVENT_NAME LIMIT 15")
SESSION_SQL = ("SELECT m.THREAD_ID, t.PROCESSLIST_ID, t.PROCESSLIST_USER, t.PROCESSLIST_HOST, "
               "SUM(m.CURRENT_NUMBER_OF_BYTES_USED), SUM(m.HIGH_NUMBER_OF_BYTES_USED), "
               "SUM(i.ENABLED = 'YES') FROM " + THREAD_TABLE +
               " AS m JOIN performance_schema.threads AS t ON t.THREAD_ID = m.THREAD_ID "
               "LEFT JOIN performance_schema.setup_instruments AS i ON i.NAME = m.EVENT_NAME "
               "WHERE t.PROCESSLIST_ID IS NOT NULL AND m.EVENT_NAME LIKE 'memory/%' "
               "GROUP BY m.THREAD_ID, t.PROCESSLIST_ID, t.PROCESSLIST_USER, t.PROCESSLIST_HOST "
               "ORDER BY SUM(m.CURRENT_NUMBER_OF_BYTES_USED) DESC, m.THREAD_ID LIMIT 10")
ACCOUNT_SQL = ("SELECT m.USER, m.HOST, SUM(m.CURRENT_NUMBER_OF_BYTES_USED), SUM(m.HIGH_NUMBER_OF_BYTES_USED), "
               "SUM(i.ENABLED = 'YES') FROM " + ACCOUNT_TABLE + " AS m "
               "LEFT JOIN performance_schema.setup_instruments AS i ON i.NAME = m.EVENT_NAME "
               "WHERE m.EVENT_NAME LIKE 'memory/%' GROUP BY m.USER, m.HOST "
               "ORDER BY SUM(m.CURRENT_NUMBER_OF_BYTES_USED) DESC, m.USER, m.HOST LIMIT 10")
# One sentinel beyond the cap makes an incomplete fingerprint unknown.
MAX_INSTRUMENTS = 8192
COVERAGE_SQL = ("SELECT NAME, ENABLED FROM performance_schema.setup_instruments "
                "WHERE NAME LIKE 'memory/%' ORDER BY NAME LIMIT 8193")
CONSUMER_SQL = ("SELECT NAME, ENABLED FROM performance_schema.setup_consumers "
                "WHERE NAME IN ('global_instrumentation', 'thread_instrumentation') LIMIT 2")
STATUS_SQL = "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Memory_used')"
FALLBACK_SQL = ("SELECT ID, USER, HOST, MEMORY_USED, MAX_MEMORY_USED FROM information_schema.PROCESSLIST "
                "ORDER BY MEMORY_USED DESC, ID LIMIT 10")
# While thread_instrumentation is on, an allocation by a thread whose
# INSTRUMENTED is NO is recorded in no memory table (MySQL pfs.cc
# pfs_memory_alloc_vc, MariaDB pfs.cc pfs_memory_alloc_v1). One row: a scan of
# the thread instances, no per-thread output.
THREAD_SQL = ("SELECT SUM(INSTRUMENTED = 'NO'), COUNT(*) FROM performance_schema.threads "
              "WHERE TYPE = 'FOREGROUND' LIMIT 1")
#: Above this percentage of uninstrumented foreground threads the window is not judged.
UNINSTRUMENTED_PCT = 10
#: MariaDB releases whose Memory_used counts the InnoDB buffer pool: the pool
#: came from my_large_malloc (ut0new.h allocate_large), which adds to
#: Memory_used from these releases on (mysys/my_largepage.c update_malloc_size),
#: until it moved to my_virtual_mem_reserve (buf0buf.cc, Memory_used on AIX
#: only) in 10.11.12, 11.4.6 and 11.8.2. Checked on every release tag.
POOL_IN_MEMORY_USED = (((10, 11, 6), (10, 11, 11)), ((11, 0, 4), (11, 0, 6)), ((11, 1, 3), (11, 1, 6)),
                       ((11, 2, 2), (11, 2, 6)), ((11, 3, 1), (11, 3, 2)), ((11, 4, 0), (11, 4, 5)),
                       ((11, 5, 0), (11, 7, 2)), ((11, 8, 0), (11, 8, 1)))


def pool_in_memory_used(version):
    """Whether MariaDB *version* (a tuple) counts the buffer pool in
    Memory_used; None when the version is unknown."""
    if not version or not any(version):
        return None
    return any(low <= tuple(version) <= high for low, high in POOL_IN_MEMORY_USED)


def free_charged_to(server):
    """Where Performance Schema charges a free made by a thread other than the
    allocating one: "global" on MySQL 8.0.23 and later (pfs_memory_free_vc
    falls back to the global aggregate), "freeing_thread" on MySQL 5.7, 8.0
    before 8.0.23 and MariaDB (the thread that frees is charged), None when a
    MySQL version is unknown."""
    if server.engine == "mariadb":
        return "freeing_thread"
    version = tuple(server.version_tuple or ())
    if not any(version):
        return None
    return "global" if version >= (8, 0, 23) else "freeing_thread"


def _threads(reader, notes):
    """{"uninstrumented", "foreground"} foreground thread counts, or None."""
    rows = reader.optional(THREAD_SQL, "performance_schema.threads", notes)
    if not rows or len(rows[0]) < 2:
        return None
    foreground = _number(rows[0][1])
    if foreground is None:
        return None
    return {"uninstrumented": _number(rows[0][0]) or 0, "foreground": foreground}


class MemoryReader(Reader):
    """Bound client execution time as well as the number of returned rows."""
    def __init__(self, conn):
        super(MemoryReader, self).__init__(conn, TAG)
        self.remaining = 30.0

    def rows(self, sql, timeout=None):
        if self.remaining <= 0:
            raise ConnectorError("Memory collection exceeded its 30 s read budget.")
        start = time.monotonic()
        try:
            return super(MemoryReader, self).rows(sql, timeout=min(5.0, self.remaining))
        finally:
            self.remaining -= time.monotonic() - start


def _coverage(reader, notes):
    rows = reader.optional(COVERAGE_SQL, "memory instrument coverage", notes)
    if rows is None or len(rows) > MAX_INSTRUMENTS or any(len(r) < 2 for r in rows):
        return None
    pairs = sorted((r[0], r[1]) for r in rows)
    digest = hashlib.sha256(json.dumps(pairs, ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
    return {"enabled": sum(r[1] == "YES" for r in pairs), "total": len(pairs), "fingerprint": digest}


def _consumers(reader, notes):
    rows = reader.optional(CONSUMER_SQL, "memory consumers", notes)
    return {r[0]: r[1] == "YES" for r in rows or [] if len(r) >= 2} if rows is not None else None


def _snapshot(reader, source, notes, clock):
    status = reader.pairs(STATUS_SQL)
    total_t, taken_at = clock(), now()
    if source == "performance_schema":
        rows = reader.optional(TOTAL_SQL, GLOBAL_TABLE, notes)
        total_t, taken_at = clock(), now()
        good = bool(rows and len(rows[0]) >= 3 and (_number(rows[0][2]) or 0) > 0)
        current = _number(rows[0][0]) if good else None
        high = _number(rows[0][1]) if good else None
        groups = reader.optional(CATEGORY_SQL, GLOBAL_TABLE + " categories", notes) if good else None
        category_t = clock()
        categories = [{"key": r[0], "label": LABELS.get(r[0], "Other"), "current_bytes": _number(r[1]),
                       "high_bytes": _number(r[2]), "source": GLOBAL_TABLE, "t": category_t} for r in groups or [] if len(r) >= 3]
    else:
        current, high, categories = _number(status.get("Memory_used")), None, []
    return {"current_bytes": current, "high_bytes": high, "categories": categories,
            "uptime": _number(status.get("Uptime")), "t": total_t, "taken_at": taken_at}


def memory_report(conn, interval=DEFAULT_WINDOW, sleep=None, clock=None):
    """Collect memory-1.0. Missing/denied tables become notes, never writes."""
    interval = check_window(interval)
    reader = MemoryReader(conn)
    notes = []
    server = preflight(reader, ("innodb_buffer_pool_size",))
    source = "performance_schema" if server.ps_on else "mariadb_status" if server.engine == "mariadb" else None
    coverage = _coverage(reader, notes) if server.ps_on else None
    consumers = _consumers(reader, notes) if server.ps_on else None
    first = second = None
    if source:
        # Pick one accounting source for the entire window. Never subtract a
        # MariaDB status value from a Performance Schema value.
        taking_first = True
        def take(clock_fn):
            nonlocal source, taking_first
            snapshot = _snapshot(reader, source, notes, clock_fn)
            if taking_first and source == "performance_schema" and snapshot["current_bytes"] is None and server.engine == "mariadb":
                source = "mariadb_status"
                snapshot = _snapshot(reader, source, notes, clock_fn)
                notes.append("Performance Schema memory totals were unavailable; this window uses MariaDB Memory_used.")
            taking_first = False
            return snapshot
        first, second = sample_twice(take, interval, sleep, clock)
    after_coverage = _coverage(reader, notes) if server.ps_on else None
    after_consumers = _consumers(reader, notes) if server.ps_on else None
    events = sessions = accounts = threads = None
    if source == "performance_schema":
        events = reader.optional(EVENT_SQL, GLOBAL_TABLE, notes)
        sessions = reader.optional(SESSION_SQL, THREAD_TABLE, notes)
        accounts = reader.optional(ACCOUNT_SQL, ACCOUNT_TABLE, notes)
        # Uninstrumented threads lose allocations only while thread
        # instrumentation is on; otherwise they reach the global table.
        if (after_consumers or {}).get("thread_instrumentation") is True:
            threads = _threads(reader, notes)
    session_source = THREAD_TABLE
    if server.engine == "mariadb" and (source == "mariadb_status" or sessions is None):
        sessions = reader.optional(FALLBACK_SQL, PROCESS_TABLE, notes)
        session_source = PROCESS_TABLE
        notes.append("MariaDB process-list memory covers visible sessions; without PROCESS, only this user's sessions are visible.")
    return compute_memory(server, first, second, after_coverage, after_consumers, events, sessions, accounts,
                          source, session_source, interval, [redact(n, getattr(conn, "password", None)) for n in notes],
                          coverage != after_coverage or consumers != after_consumers, threads)


def compute_memory(server, first, second, coverage, consumers, events, sessions, accounts,
                   source, session_source=THREAD_TABLE, interval=DEFAULT_WINDOW, notes=None, changed=False,
                   threads=None):
    """Pure projection. Missing values stay null; falling allocation gauges are valid."""
    notes = list(notes or [])
    current = (second or {}).get("current_bytes")
    high = (second or {}).get("high_bytes")
    span = elapsed(first, second)
    window_status = "ok"
    if restarted(first, second):
        window_status = "restarted"
    elif changed:
        window_status = "coverage_changed"
    elif current is None or (first or {}).get("current_bytes") is None or span is None:
        window_status = "unavailable"
    growth = (current - first["current_bytes"]) / span if window_status == "ok" else None
    categories = [dict(r) for r in (second or {}).get("categories", [])]
    before = {r["key"]: r for r in (first or {}).get("categories", [])}
    share_total = sum(max(0, r["current_bytes"]) for r in categories if r["current_bytes"] is not None)
    for row in categories:
        value = row["current_bytes"]
        old = before.get(row["key"], {})
        # Categories and totals arrive from separate queries, each with its
        # own completion timestamp. Never use one query's span for the other.
        category_span = elapsed(old, row)
        row["elapsed_s"] = _round(category_span, 3)
        row["growth_bytes_per_s"] = _round((value - old["current_bytes"]) / category_span, 2) if window_status == "ok" and category_span and value is not None and old.get("current_bytes") is not None else None
        row.pop("t", None)
        row["share_pct"] = min(100, _round(max(0, value) / share_total * 100, 2)) if share_total > 0 and value is not None else None
    if server.engine == "mariadb" and source == "performance_schema" and categories:
        # The categories MariaDB cannot fill say so rather than look empty.
        for row in categories:
            if row["key"] == "sort":
                row["engine_note"] = MARIADB_NOTES["sort"]
        if not any(r["key"] == "network" for r in categories):
            categories.append({"key": "network", "label": LABELS["network"], "current_bytes": None, "high_bytes": None,
                               "source": GLOBAL_TABLE, "elapsed_s": None, "growth_bytes_per_s": None, "share_pct": None,
                               "collection_reason": MARIADB_NOTES["network"]})
    categories.sort(key=lambda r: -(r["current_bytes"] if r["current_bytes"] is not None else -float("inf")))
    event_rows = [{"event": r[0], "category": category(r[0]), "current_bytes": _number(r[1]),
                   "high_bytes": _number(r[2]), "source": GLOBAL_TABLE} for r in events or [] if len(r) >= 3 and r[0]][:15]
    def attribution(row, enabled_events):
        # Disabled instruments still expose summary rows. Preserve historical
        # nonzero accounting, but do not label their uninstrumented zero as measured.
        reason = None
        if enabled_events is None:
            reason = "Enabled attribution instruments could not be read; open Server > Instruments to check coverage."
        elif enabled_events == 0:
            reason = "Attribution instruments are disabled; open Server > Instruments to review coverage."
        elif not consumers or consumers.get("global_instrumentation") is not True or consumers.get("thread_instrumentation") is not True:
            reason = "Attribution consumers are off or unknown; open Server > Instruments to check collection."
        if reason:
            for field in ("current_bytes", "high_bytes"):
                if row[field] == 0:
                    row[field] = None
                    row["collection_reason"] = reason
        return row

    session_rows = []
    for r in sessions or []:
        if session_source == PROCESS_TABLE and len(r) >= 5:
            pid, user, host, value, peak = r[:5]
            thread = None
        elif session_source == THREAD_TABLE and len(r) >= 6:
            thread, pid, user, host, value, peak = r[:6]
        else:
            continue
        row = {"thread_id": _number(thread), "processlist_id": _number(pid), "user": user,
                             "host": host, "current_bytes": _number(value), "high_bytes": _number(peak),
                             "source": session_source}
        session_rows.append(attribution(row, _number(r[6]) if len(r) > 6 else None) if session_source == THREAD_TABLE else row)
    session_rows = session_rows[:10]
    account_rows = [attribution({"user": r[0], "host": r[1], "current_bytes": _number(r[2]), "high_bytes": _number(r[3]),
                     "source": ACCOUNT_TABLE}, _number(r[4]) if len(r) > 4 else None) for r in accounts or [] if len(r) >= 4][:10]
    total_instruments = (coverage or {}).get("total", 0)
    enabled = (coverage or {}).get("enabled", 0)
    mostly_off = coverage is not None and (not total_instruments or enabled * 2 < total_instruments)
    # Foreground threads with INSTRUMENTED = NO, counted only while thread
    # instrumentation is on (memory_report reads them only then).
    missing_threads = None
    if (source == "performance_schema" and threads and threads.get("uninstrumented")
            and (consumers or {}).get("thread_instrumentation") is True):
        missing_threads = threads
    gate = None
    gate_row = "group:memory"
    if not server.ps_on:
        gate = ("Performance Schema is off (MariaDB's default); open Server > Instruments for categories and accounts."
                if server.engine == "mariadb" else "Performance Schema is off; open Server > Instruments for memory collection.")
        gate_row = "ps"
    elif mostly_off:
        gate = "Most memory instruments are off; open Server > Instruments to review coverage."
    elif coverage is None:
        gate = "Memory instrument coverage could not be read; open Server > Instruments to check it."
    elif consumers is None or consumers.get("global_instrumentation") is not True:
        gate = "Global memory collection is off or unknown; open Server > Instruments to check it."
    elif missing_threads and missing_threads["uninstrumented"] * 100 > missing_threads["foreground"] * UNINSTRUMENTED_PCT:
        gate = ("%d of %d foreground threads are not instrumented, so their memory is recorded nowhere; "
                "open Server > Instruments to review setup_actors." % (missing_threads["uninstrumented"], missing_threads["foreground"]))
        gate_row = "actors"
    findings = []
    def finding(key, status, rule, why):
        findings.append({"key": key, "status": status, "rule": "Rule of thumb: " + rule, "reason": why})
    judged = (current is not None and current >= 0 and window_status == "ok" and not gate
              and bool(categories) and sessions is not None and accounts is not None and not lost_note(server))
    if not judged:
        finding("coverage", "not_judged", "judge only a comparable window with known collection coverage.",
                gate or "The sampling window or collection coverage is incomplete.")
    increase = current - first["current_bytes"] if growth is not None else None
    if increase is not None and increase > max(0, first["current_bytes"]) * .01 and increase > 16 * (1 << 20):
        finding("growth", "watch", "tracked bytes increased by more than both 1% of the starting total and 16 MiB across the window.",
                "A short allocation burst can be normal; this does not prove a leak.")
    if source == "performance_schema" and not mostly_off and coverage is not None:
        for row in categories:
            if row["key"] in WORKLOAD_CATEGORIES and row["share_pct"] is not None and row["current_bytes"] > share_total * .25:
                finding("category:" + row["key"], "watch", "one workload-driven category (temporary tables, sort, join and hash buffers, or network) exceeds 25% of non-negative category bytes.",
                        row["label"] + " exceeds this threshold in the current sample; the workload can explain it.")
    for row in session_rows:
        if row["current_bytes"] is not None and row["current_bytes"] > 1 << 30:
            finding("session:" + str(row["processlist_id"]), "watch",
                    "one visible session exceeds 1 GiB currently tracked; memory another thread freed can still count here.",
                    "Inspect this session's workload before changing any setting.")
    # Unknown coverage wins the overall verdict, while observable Watch rows
    # remain available in notes and the inspector.
    status = "not_judged" if not judged else "watch" if findings else "ok"
    if source == "mariadb_status":
        # mysys/my_malloc.c update_malloc_size: only my_malloc, my_realloc and
        # my_free move Memory_used; InnoDB allocates with malloc (ut0new.h).
        pool = pool_in_memory_used(server.version_tuple)
        notes.append("MariaDB Memory_used counts only allocations made through the server's own allocator (my_malloc), so %s "
                     "Categories, accounts and a global high-water mark are not collected." % (
                         "most InnoDB memory is missing, but this release does count the buffer pool." if pool
                         else "most InnoDB memory, the buffer pool included, is missing." if pool is False
                         else "most InnoDB memory is missing; the buffer pool counts only on some releases."))
    notes.extend([
        "Tracked allocations are not process RSS or machine RAM; no RAM capacity is inferred.",
        "Each event high is an upper bound (current bytes plus per-thread, account and host headroom), not a recorded peak; their sum is not a server peak.",
        "Enabled instruments do not recover earlier allocations; session/account totals need not add up to the global total.",
        "Categories use known event-name prefixes; unmatched allocations stay in Other, and TempTable may include mapped files.",
        "Reads are sequential, not an atomic snapshot; the collection itself can allocate memory.",
    ])
    charged = free_charged_to(server) if source == "performance_schema" else None
    if charged == "global":
        notes.append("Performance Schema on MySQL 8.0.23 and later charges a free made by another thread to the global total, "
                     "so the session or account that allocated the memory can be overstated.")
    elif charged == "freeing_thread":
        notes.append("Performance Schema on this server charges a free to the thread that makes it, so the session or account "
                     "that allocated the memory can be overstated and the one that freed it can drop below 0.")
    if missing_threads and gate_row != "actors":
        notes.append("%d of %d foreground threads are not instrumented (INSTRUMENTED = NO in performance_schema.threads); "
                     "their allocations are recorded nowhere, so totals, sessions and accounts are partial."
                     % (missing_threads["uninstrumented"], missing_threads["foreground"]))
    if window_status != "ok":
        notes.append("No comparable growth window (%s); refresh to sample again." % window_status)
    if consumers and consumers.get("thread_instrumentation") is False:
        notes.append("Thread instrumentation is off; per-session and per-account attribution can be incomplete.")
    if lost_note(server):
        notes.append(lost_note(server))
    buffer = next((r for r in categories if r["key"] == "buffer_pool"), None)
    report = base_report(SCHEMA_VERSION, server, (second or {}).get("taken_at"))
    report.update({"available": current is not None or bool(session_rows), "source": source, "interval": interval,
                   "elapsed_s": _round(span, 3), "window_status": window_status,
                   "coverage": {k: coverage[k] for k in ("enabled", "total")} if coverage is not None else None,
                   "consumers": consumers, "gate": gate, "gate_row": gate_row if gate else None,
                   "thread_coverage": dict(threads) if threads and source == "performance_schema" else None,
                   "status": status, "current_bytes": current,
                   "high_bytes": high, "growth_bytes_per_s": _round(growth, 2),
                   "innodb_buffer_pool_size": _number(server.var("innodb_buffer_pool_size")),
                   "buffer_pool_share_pct": buffer["share_pct"] if buffer else None,
                   "categories": categories, "events": event_rows, "sessions": session_rows, "accounts": account_rows,
                   "collection": {"events": events is not None, "sessions": sessions is not None, "accounts": accounts is not None},
                   "findings": findings, "notes": list(dict.fromkeys(notes))})
    return report
