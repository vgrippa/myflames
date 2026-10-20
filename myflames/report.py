"""Full analysis: one read-only report over every Server view, with ranked
findings, per-area sections, and the statements behind the numbers.

:func:`analysis_report` returns the ``report-1.0`` envelope shared by
``myflames report`` and ``POST /api/live/report``. It runs the existing
views once on one connection (each statement is its own short client call):
Health, Waits and I/O first (they sample over a window, so the total time is
the longest window, not the sum), then Parameters, InnoDB, Sessions,
Instruments, the server checks (server_checks.py), Tables and Indexes, at
most :data:`MAX_WORKERS` at a time. Then the workload attribution reads the
statement digests behind "Joins without an index" and behind the tables read
mostly without an index (discover.top_report). A view that fails keeps the
rest of the report: its error is in ``header.failures`` and the sections it
feeds say what is missing. :func:`run_single_flight` lets one analysis run
per connection; a second identical request joins it.

Every threshold lives with the module that judges it (health.RULES,
config_review.RULES, innodb_status.RULES, sessions, schema_health,
server_checks.RULES); this module only composes and ranks. Nothing is
written to the server and no recommendation is applied: a suggested SET
names its scope and needs the db-operations confirmation.

Check families (MySQLTuner's public feature list used as a checklist only;
MySQLTuner is GPL-3.0, and none of its code, text or thresholds is used):

Covered:

* Server identity and uptime (preflight; short-uptime notes).
* Memory: buffer pool full or warming, hit ratio, waits for a free page,
  dirty pages, buffer pool size against the data, sort and join buffers
  (health, config_review), plus a caveated total memory scenario (server_checks).
* Redo and durability: redo size against the write rate and checkpoint age,
  log buffer waits, innodb_flush_log_at_trx_commit, sync_binlog, the binary
  log cache, binary log expiry (health, config_review, server_checks).
* Connections and threads: limit used and refusals, threads running, failed
  and dropped connections, thread cache, long-running statements, idle
  transactions, lock waits (health, config_review, sessions).
* Temporary tables and sorts, joins without an index, full scans (health,
  config_review), with the statements behind joins without an index.
* Table open cache, table definition cache, open files (health,
  config_review).
* Query cache, MariaDB only; MyISAM key cache and MariaDB Aria page cache
  when the scoped engine inventory contains those tables (server_checks).
* Dated version support table; Galera/wsrep status when present (server_checks).
* Indexes: unused, redundant, tables read mostly by full scans, with the
  statements that name them (indexes, discover).
* Schema health: tables without a primary key or usable unique key,
  fragmentation, storage engine inventory (schema_health).
* Waits and I/O hot spots (waits, io_stats).
* InnoDB status: semaphores, transactions and purge (history list length),
  latest deadlock and foreign key error, file I/O, row operations
  (innodb_status).
* Replication status on a replica, read-only (server_checks).
* Security basics: accounts without a password, anonymous accounts, root
  from any host (server_checks; needs SELECT on mysql.user).
* Performance Schema coverage, lost counters, the slow query log
  (ps_instruments, config_review).

Not checked (listed in ``checklist.not_checked``): OS memory, swap, CPU and
disk (not generally readable over SQL; a memory budget is computed but RAM
is never guessed); per-table engine conversion advice beyond the inventory;
lock waits from the lock tables (Locks reads the data_locks table, which
holds the lock-system latch on some versions, so the report uses Sessions
and InnoDB status instead).

MySQL 8.0 (for example Percona Server 8.0.43) works through the same views:
each gates version-specific reads (INNODB_TABLESPACES' AUTOEXTEND_SIZE from
8.0.23, the Performance Schema process list from 8.0.22, SHOW REPLICA STATUS
from 8.0.22, setup_instruments PROPERTIES) and degrades with a note.
"""
import concurrent.futures
import copy
import hashlib
import hmac
import html
import json
import os
import re
import textwrap
import threading
import time

from . import __version__
from .connector import ConnectorError, terminal_text
from .perf_common import (ALL_SCHEMAS_TABLE_LIMIT, Reader, _bytes_text, _count_text, _duration_text,
                          check_schemas, check_window, lost_note, now, preflight, ps_off_text, reason, require_choice,
                          scope_text)

SCHEMA_VERSION = "report-1.0"
REPORT_TAG = "/*myflames:report*/ "
#: Views read at the same time, each a client process and a server
#: connection (the server may enforce max_user_connections).
MAX_WORKERS = 3
DEFAULT_INTERVAL = 10
MIN_INTERVAL, MAX_INTERVAL = 2, 60
#: Rows each sub-view keeps per list (the report shows summaries only).
LIST_LIMIT = 50
#: Tables read mostly without an index whose statements are looked up.
ATTRIBUTION_TABLES = 3
#: Statements listed per attribution.
ATTRIBUTION_LIMIT = 5
STATUSES = ("problem", "watch", "unknown", "info", "ok")
SEVERITY = {"ok": 0, "watch": 1, "problem": 2}
STATUS_RANK = {"problem": 0, "watch": 1, "unknown": 2, "info": 3, "ok": 4}

#: Sections, in display order: (key, title, views it draws from).
SECTIONS = (
    ("memory", "Memory and buffer pool", ("health", "config", "innodb", "checks")),
    ("redo", "Redo and durability", ("health", "config", "innodb", "checks")),
    ("connections", "Connections and threads", ("health", "config", "sessions")),
    ("temp_sorts", "Temporary tables, sorts and joins", ("health", "config")),
    ("caches", "Table and engine caches", ("health", "config", "checks", "tables")),
    ("query_cache", "Query cache", ("checks",)),
    ("indexes", "Indexes", ("indexes",)),
    ("schema", "Schema health", ("tables",)),
    ("waits", "Waits", ("waits",)),
    ("io", "I/O", ("io", "innodb", "config")),
    ("innodb", "InnoDB status", ("innodb", "health")),
    ("replication", "Replication", ("checks",)),
    ("security", "Security basics", ("checks",)),
    ("observability", "Coverage and version support", ("instruments", "config", "waits", "checks")),
)
SECTION_TITLES = {key: title for key, title, _ in SECTIONS}
#: Ranking order of families inside a status: a stopped replica or a redo
#: log near its limit affects every client; coverage and version lifecycle
#: findings follow the directly observed workload problems.
FAMILY_RANK = {key: i for i, key in enumerate((
    "replication", "redo", "memory", "connections", "innodb", "security", "temp_sorts", "caches", "schema",
    "indexes", "query_cache", "io", "waits", "observability"))}
VIEW_LABELS = {
    "health": "Health", "waits": "Waits", "io": "I/O", "config": "Parameters", "innodb": "InnoDB",
    "sessions": "Sessions", "instruments": "Instruments", "checks": "Server checks", "tables": "Tables",
    "indexes": "Indexes", "top": "Top statements",
}
#: MySQLTuner-style families this report covers (see the module docstring).
COVERED = (
    ("identity", "Server identity and uptime"), ("memory", "Buffer pool, session buffers and total memory budget"),
    ("redo", "Redo log, durability, binary log cache and expiry"), ("connections", "Connections and threads"),
    ("temp_sorts", "Temporary tables, sorts, joins without an index, full scans"),
    ("caches", "Table open cache, definition cache, open files, MyISAM and Aria caches"),
    ("query_cache", "Query cache (MariaDB)"),
    ("indexes", "Unused and redundant indexes, tables read by full scans"),
    ("schema", "Tables without a primary key, fragmentation, storage engines"), ("waits", "Wait events"),
    ("io", "Table and file I/O"), ("innodb", "InnoDB status: purge, deadlocks, semaphores"),
    ("replication", "Replication and Galera/wsrep status (read-only)"),
    ("security", "Accounts without a password, anonymous accounts, root from any host"),
    ("observability", "Performance Schema coverage, slow query log and dated version support"),
)
NOT_CHECKED = (
    {"label": "OS memory, swap, CPU and disk", "reason": "Not readable over SQL."},
    {"label": "Lock tables (who blocks whom)", "reason": ("Locks reads the data_locks table, which holds "
                                                          "the lock-system latch on some versions; open Locks when "
                                                          "Sessions or InnoDB show lock waits.")},
)

# ---- running the views ------------------------------------------------------------------------


def _redact(text, password):
    text = " ".join(str(text).split())
    return text.replace(password, "[redacted]") if password else text


def _timed(key, fn, conn, password):
    started_at, t0 = now(), time.monotonic()
    report, error = None, None
    try:
        report = fn(conn)
    except (ValueError, ConnectorError) as exc:
        error = _redact(reason(exc), password)
    except Exception as exc:  # one failing view must not lose the others
        error = _redact("internal error (%s): %s" % (type(exc).__name__, exc), password)
    return {"key": key, "report": report, "error": error, "started_at": started_at, "taken_at": now(),
            "elapsed_s": round(time.monotonic() - t0, 3)}


class _Guarded(object):
    """The connection the views share. Once the run is cancelled it refuses
    new statements. Running statements finish with their own client option
    files; queued statements never create one after cancellation is observed."""

    def __init__(self, conn, cancel):
        self._conn = conn
        self._cancel = cancel

    def run(self, sql, extra_flags=None, timeout=None):
        if self._cancel.is_set():
            raise ConnectorError("Cancelled.")
        return self._conn.run(sql, extra_flags=extra_flags, timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _run_all(conn, jobs, workers, cancel, password):
    """Run (key, fn) jobs at most *workers* at a time, in order of submission.
    Ctrl+C sets *cancel*: sleeping windows wake up, queued jobs are dropped,
    running ones stop at their next statement, and the interrupt is raised
    once they have (at most the statement each one is running)."""
    results = {}
    if not jobs:
        return results
    guarded = _Guarded(conn, cancel)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    futures = [(key, pool.submit(_timed, key, fn, guarded, password)) for key, fn in jobs]
    try:
        for key, future in futures:
            results[key] = future.result()
    except BaseException:
        cancel.set()
        for _, future in futures:
            future.cancel()
        pool.shutdown(wait=True)
        raise
    pool.shutdown(wait=True)
    return results


def _plan(server, interval, schemas, schema_gate, sleep, clock):
    from . import config_review, health, indexes, innodb_status, io_stats, ps_instruments, schema_health, \
        server_checks, sessions, waits
    one = schemas[0] if schemas and len(schemas) == 1 else None
    jobs = [
        ("health", lambda c: health.health_report(c, interval, sleep=sleep, clock=clock)),
        ("waits", lambda c: waits.waits_report(c, interval, 20, sleep=sleep, clock=clock)),
        ("io", lambda c: io_stats.io_report(c, interval, one, 20, sleep=sleep, clock=clock)),
        ("config", config_review.config_report),
        ("innodb", innodb_status.innodb_report),
        ("sessions", sessions.sessions_report),
        ("instruments", ps_instruments.instruments_report),
        ("checks", server_checks.server_checks),
    ]
    if schema_gate is None:
        jobs.append(("tables", lambda c: schema_health.table_report(
            c, schemas, False, LIST_LIMIT, require_choice_above=None)))
        jobs.append(("indexes", lambda c: indexes.index_report(
            c, schemas=schemas, limit=LIST_LIMIT, require_choice_above=None)))
    return jobs


def analysis_report(conn, interval=DEFAULT_INTERVAL, schemas=None, require_choice_above=ALL_SCHEMAS_TABLE_LIMIT,
                    workers=MAX_WORKERS, sleep=None, clock=None, cancel=None):
    """Return the ``report-1.0`` report. Read-only; see the module docstring.

    *interval* (2 to 60 s) is the window of Health, Waits and I/O. *schemas*
    limits Indexes and Tables; without it every user schema is read. If the
    selected scope holds more than *require_choice_above* tables (None: no
    limit), those two views are skipped with a note. *sleep* and *clock*
    default to the time module's; *cancel* is a threading.Event.
    """
    interval = check_window(interval, MIN_INTERVAL, MAX_INTERVAL)
    schemas = check_schemas(schemas)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("Choose 1 to %d views at a time." % MAX_WORKERS)
    cancel = cancel or threading.Event()
    clock = clock or time.monotonic
    if sleep is None:
        def sleep(seconds):
            cancel.wait(seconds)
    password = getattr(conn, "password", None)
    started_at, t0 = now(), time.monotonic()
    reader = Reader(conn, REPORT_TAG)
    schema_gate = None
    try:
        # Read server identity before starting the views. Each subprocess
        # owns its option file, including calls on this shared connection.
        server = preflight(reader)
        if require_choice_above is not None:
            try:
                require_choice(reader, server, schemas, require_choice_above, "the full analysis")
            except ValueError as exc:
                schema_gate = str(exc) + " Indexes and Tables were skipped; choose databases (or use --schema on the command line) to include them."
    except ConnectorError as exc:
        raise ValueError(_redact("Could not read the server: " + reason(exc), password))
    runs = _run_all(conn, _plan(server, interval, schemas, schema_gate, sleep, clock),
                    workers, cancel, password)
    attribution = _attribution(conn, server, runs, workers, cancel, password)
    return compose_report(server, runs, attribution, interval=interval, schemas=schemas, schema_gate=schema_gate,
                          started_at=started_at, elapsed_s=round(time.monotonic() - t0, 3))

# ---- workload attribution ---------------------------------------------------------------------


class _AttributionContext(object):
    """The connection the attribution's digest lists share for one analysis.

    Each ``discover.top_report`` reads the same server state and coverage
    (``discover.SHARED_METADATA_SQL``) before its own digest rows. Within one
    attribution pass those replies are read once, an error included, and
    every other statement (the digest rows) still reaches the connection, so
    discover's parsing and error handling stay as they are. The lock gives
    concurrent lists one read per statement.
    """

    def __init__(self, conn):
        from .discover import SHARED_METADATA_SQL
        self._conn = conn
        self._shared = SHARED_METADATA_SQL
        self._answers = {}
        self._lock = threading.Lock()

    def run(self, sql, extra_flags=None, timeout=None):
        if sql not in self._shared:
            return self._conn.run(sql, extra_flags=extra_flags, timeout=timeout)
        key = (sql, tuple(extra_flags or ()), timeout)
        with self._lock:
            if key not in self._answers:
                try:
                    self._answers[key] = (self._conn.run(sql, extra_flags=extra_flags, timeout=timeout), None)
                except ConnectorError as exc:
                    self._answers[key] = (None, str(exc))
            answer, error = self._answers[key]
        if error is not None:
            raise ConnectorError(error)
        return answer

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _attribution_tables(indexes_report):
    """The tables read mostly without an index, by read time without one."""
    if not indexes_report or not indexes_report.get("available"):
        return []
    rows = [f for f in indexes_report.get("full_scans") or [] if f.get("mostly")]
    rows.sort(key=lambda f: (-(f.get("fetch_ms_without_index") or 0), -(f.get("rows_without_index") or 0)))
    return rows[:ATTRIBUTION_TABLES]


def _feature(instruments_report, key):
    for f in (instruments_report or {}).get("features") or []:
        if f.get("key") == key:
            return f
    return None


def _attribution(conn, server, runs, workers, cancel, password):
    from . import discover
    out = {"available": False, "gate": None, "fix": None, "label": discover.TEXT_MATCH_LABEL,
           "caveats": list(discover.TEXT_MATCH_CAVEATS), "full_joins": None, "full_scans": [], "coverage": None,
           "notes": []}
    health = (runs.get("health") or {}).get("report")
    if health:
        metric = next((m for m in health.get("metrics") or [] if m.get("key") == "select_full_join"), None)
        if metric:
            out["health_window"] = {"select_full_join": metric.get("delta"), "rate": metric.get("rate"),
                                    "elapsed_s": health.get("elapsed_s"), "status": metric.get("status")}
    if not server.ps_on:
        out["gate"] = "Performance Schema is off, so no statement can be linked to these numbers."
        out["fix"] = ps_off_text(server, "statement digests")
        return out
    top = _feature((runs.get("instruments") or {}).get("report"), "top")
    if top and top.get("status") == "off":
        out["gate"] = "Statement digests are not being collected, so no statement can be linked to these numbers."
        out["fix"] = "Missing: %s. Instruments shows the statements that turn them on." % "; ".join(top["missing"])
        return out
    tables = _attribution_tables((runs.get("indexes") or {}).get("report"))
    jobs = [("full_join", lambda c: discover.top_report(c, ATTRIBUTION_LIMIT, None, "full_join"))]
    for entry in tables:
        name = (entry["schema"], entry["table"])
        jobs.append(("table:%s.%s" % name,
                     lambda c, name=name: discover.top_report(c, ATTRIBUTION_LIMIT, None, "no_index", name)))
    # The inner guard checks again after a metadata waiter acquires the
    # context lock; cancellation must not start a queued server read.
    results = _run_all(_AttributionContext(_Guarded(conn, cancel)), jobs, workers, cancel, password)
    first = results.get("full_join") or {}
    if first.get("error"):
        out["gate"] = first["error"].split(". ")[0].rstrip(".") + "."
        out["fix"] = first["error"]
        return out
    out["available"] = True
    report = first["report"]
    out["coverage"] = report.get("coverage")
    out["notes"] = [n for n in report.get("notes") or []]
    out["full_joins"] = {"order": "full_join", "statements": report["statements"], "taken_at": first["taken_at"]}
    for entry in tables:
        key = "table:%s.%s" % (entry["schema"], entry["table"])
        result = results.get(key) or {}
        item = {"schema": entry["schema"], "table": entry["table"], "label": discover.TEXT_MATCH_LABEL,
                "rows_without_index": entry.get("rows_without_index"),
                "fetch_ms_without_index": entry.get("fetch_ms_without_index"), "share_time": entry.get("share_time"),
                "statements": [], "error": result.get("error")}
        if result.get("report"):
            item["statements"] = result["report"]["statements"]
            item["capped"] = bool((result["report"].get("match") or {}).get("capped"))
        out["full_scans"].append(item)
    return out

# ---- composing -------------------------------------------------------------------------------


def _finding(fid, family, status, title, headline="", verdict="", why="", rule=None, caveat=None, action=None,
             numbers=(), settings=(), links=(), source=None, learn=None, impact=None, attribution=None):
    verdict = verdict or ""
    if rule:
        # The views end their observation with the rule; the finding keeps
        # the rule in its own field.
        suffix = "Rule of thumb: %s." % rule.rstrip(".")
        if verdict.endswith(suffix):
            verdict = verdict[:-len(suffix)].rstrip()
    unique = []
    for link in links:
        if link and link not in unique:
            unique.append(link)
    return {"id": fid, "family": family, "status": status if status in STATUS_RANK else "unknown", "title": title,
            "headline": headline or "", "verdict": verdict, "why": why or "", "rule": rule, "caveat": caveat,
            "action": action, "numbers": [n for n in numbers if n], "settings": [s for s in settings if s],
            "links": unique, "source": source, "learn": learn,
            "impact": impact if isinstance(impact, (int, float)) and not isinstance(impact, bool) else None,
            "attribution": attribution}


def _num(label, text, value=None, unit=None):
    item = {"label": label, "text": text if text is not None else "?"}
    if value is not None:
        item["value"] = value
    if unit:
        item["unit"] = unit
    return item


def _view_link(view, key=None, label=None):
    link = {"view": view, "label": label or "Open %s" % VIEW_LABELS.get(view, view)}
    if key:
        link["key"] = key
    return link


_DIAG_FAMILY = {"buffer_pool": "memory", "redo": "redo", "table_cache": "caches", "table_definitions": "caches",
                "thread_cache": "connections", "binlog_cache": "redo", "open_files": "caches",
                "full_scans": "temp_sorts", "tmp_tables": "temp_sorts", "sorts": "temp_sorts"}
_METRIC_FAMILY = {"connections_used": "connections", "threads_running": "connections",
                  "aborted_connects": "connections", "aborted_clients": "connections", "bp_hit_ratio": "memory",
                  "bp_wait_free": "memory", "bp_dirty": "memory", "row_lock_waits": "innodb",
                  "row_lock_time": "innodb", "row_lock_avg": "innodb", "row_lock_current": "innodb",
                  "select_full_join": "temp_sorts", "opened_tables": "caches", "log_waits": "redo"}
#: Metrics shown even when ok: the anchors other findings and links use.
_ALWAYS_METRICS = ("connections_used", "select_full_join")
#: Fields of each Health metric kept in ``health_metrics`` (every status).
_HEALTH_METRIC_FIELDS = ("key", "group", "label", "explain", "unit", "value", "delta", "rate", "max", "status",
                         "why", "rule", "detail", "inputs")


def health_metrics(rep):
    """Every Health metric of the window with its status, ok, info and unknown
    included. Findings keep only the watch and problem metrics (plus
    _ALWAYS_METRICS), so a consumer that scores each metric (the workspace's
    MySQL Tuning view) can tell "measured and ok" from "not measured" without
    guessing from an absent finding. An empty list when Health did not run."""
    out = []
    for m in (rep or {}).get("metrics") or []:
        if m.get("key") and m.get("status"):
            out.append({k: m.get(k) for k in _HEALTH_METRIC_FIELDS})
    return out


def _health_findings(rep):
    from .health import format_value
    out = []
    for d in rep.get("diagnostics") or []:
        family = _DIAG_FAMILY.get(d["key"])
        if not family:
            continue
        rec = d.get("recommendation") or {}
        links = [_view_link("health", d["key"])]
        if d.get("link"):
            links.append(dict(d["link"]))
        out.append(_finding(
            "health.%s" % d["key"], family, d["status"], d["title"], d.get("headline"), d.get("verdict"),
            d.get("why"), d.get("rule"), rec.get("caveat"), {"text": rec["text"]} if rec.get("text") else None,
            [_num(n["label"], n["text"], n.get("value"), n.get("unit")) for n in d.get("numbers") or []],
            d.get("settings") or [], links, {"view": "health", "kind": "diagnostic", "key": d["key"]},
            d.get("learn"), d.get("value")))
        # Applicability and informational states must survive the projection.
        out[-1]["state"] = d.get("state")
    for m in rep.get("metrics") or []:
        family = _METRIC_FAMILY.get(m["key"])
        if not family or (m["status"] not in ("watch", "problem") and m["key"] not in _ALWAYS_METRICS):
            continue
        links = [_view_link("health", m["key"])]
        if m.get("link"):
            links.append(dict(m["link"]))
        attribution = None
        if m["key"] == "select_full_join":
            links = [l for l in links if l.get("view") != "top"]
            links.append({"view": "top", "order": "full_join",
                          "label": "Open Top statements by joins without an index"})
            attribution = "full_joins"
        numbers = [_num("Value", format_value(m), m.get("value"), m.get("unit"))]
        if m.get("detail"):
            numbers.append(_num("Detail", m["detail"]))
        out.append(_finding(
            "health.%s" % m["key"], family, m["status"], m["label"], format_value(m), m.get("why"), m.get("explain"),
            m.get("rule"), None, None, numbers, (), links, {"view": "health", "kind": "metric", "key": m["key"]},
            m.get("learn"), m.get("rate") if m.get("rate") is not None else m.get("value"), attribution))
    return out


_CONFIG_FAMILY = {"buffer_pool": "memory", "sort_buffer_size": "memory", "join_buffer_size": "memory",
                  "dedicated_server": "memory", "tmp_tables": "temp_sorts", "flush_log": "redo",
                  "sync_binlog": "redo", "flush_method": "redo", "redo": "redo", "max_connections": "connections",
                  "thread_handling": "connections", "table_open_cache": "caches",
                  "performance_schema": "observability", "slow_log": "observability", "io_capacity": "io"}


def _check_findings(view, checks, families):
    """config-1.0 and checks-1.0 checks (same shape)."""
    out = []
    for c in checks:
        family = families.get(c["key"]) or c.get("section")
        if not family:
            continue
        numbers = list(c.get("numbers") or [])
        for s in c.get("settings") or []:
            text = s.get("display") if s.get("display") is not None else s.get("value")
            if s.get("default") is not None and s.get("default") != text:
                text = "%s (default %s)" % (text, s["default"])
            numbers.append(_num(s["name"], text))
        # Server checks have no view of their own; the report is their home.
        links = [_view_link(view, c["key"])] if view != "checks" else []
        if c.get("link"):
            links.append(dict(c["link"]))
        out.append(_finding(
            "%s.%s" % (view, c["key"]), family, c["status"], c["label"], c.get("value") or "", c.get("why"),
            c.get("explain"), c.get("rule"), c.get("caveat") or None, c.get("action"), numbers, (), links,
            {"view": view, "kind": "check", "key": c["key"]}))
    return out


_INNODB_FAMILY = {"semaphores": "innodb", "transactions": "innodb", "deadlock": "innodb",
                  "foreign_key_error": "innodb", "file_io": "io", "insert_buffer": "innodb",
                  "adaptive_hash_index": "innodb", "row_operations": "innodb", "log": "redo",
                  "buffer_pool": "memory"}


def _innodb_findings(rep, health_ok):
    if not rep.get("available"):
        error = rep.get("error") or {}
        return [_finding("innodb.unavailable", "innodb", "unknown", "InnoDB status",
                         "Not readable", error.get("message") or "SHOW ENGINE INNODB STATUS was not readable.",
                         links=[_view_link("innodb")], source={"view": "innodb", "kind": "section"})]
    out = []
    for s in rep.get("sections") or []:
        family = _INNODB_FAMILY.get(s["key"])
        if not family or not s.get("present", True):
            continue
        # Health measures these over its own window; keep InnoDB's snapshot
        # only when Health is missing.
        if health_ok and s["key"] in ("log", "buffer_pool"):
            continue
        graded = [i for i in s.get("items") or [] if i.get("status") in ("watch", "problem")]
        rule = next((i.get("rule") for i in graded if i.get("rule")), None)
        why = " ".join("%s: %s." % (i["label"], i["text"]) for i in graded)
        numbers = [_num(i["label"], i.get("text"), i.get("value") if isinstance(i.get("value"), (int, float))
                        else None, i.get("unit")) for i in (s.get("items") or [])[:8]]
        links = [_view_link("innodb", s["key"])] + [dict(l) for l in s.get("links") or []]
        headline = "%s: %s" % (graded[0]["label"], graded[0]["text"]) if graded else s.get("summary")
        out.append(_finding("innodb.%s" % s["key"], family, s["status"], s["title"], headline,
                            s.get("summary"), why, rule, None, None, numbers, (), links,
                            {"view": "innodb", "kind": "section", "key": s["key"]}))
    return out


def _sessions_findings(rep):
    summary = rep.get("summary") or {}
    attention = rep.get("attention") or {}
    rules = rep.get("rules") or {}
    out = []
    for key, title, rule_key, count in (
            ("long_running", "Long-running statements", "long_running",
             sum((summary.get("long_running") or {}).get(s, 0) for s in ("watch", "problem"))),
            ("idle_in_trx", "Sessions idle in a transaction", "idle_in_trx", summary.get("idle_in_trx") or 0),
            ("lock_wait", "Sessions waiting for a lock", "lock_wait", (summary.get("lock_wait") or {}).get("total", 0))):
        items = attention.get(key) or []
        statuses = [i.get("status") for i in items]
        status = "problem" if "problem" in statuses else "watch" if (count or "watch" in statuses) else "ok"
        numbers = [_num("Connection %s" % i.get("id"), "%s%s" % (
            _duration_text(i.get("seconds")) if i.get("seconds") is not None else "?",
            " (%s)" % i["kind"] if i.get("kind") else ""), i.get("seconds"), "s") for i in items[:5]]
        headline = "%d now" % count if count else "none now"
        longest = max(items, key=lambda i: i.get("seconds") or 0) if items else None
        verdict = ("%d session%s at this moment%s." % (
            count, "" if count == 1 else "s", "; the longest for %s (connection %s)" % (
                _duration_text(longest["seconds"]), longest.get("id")) if longest and longest.get("seconds") is not None
            else "")) if count else "No session at this moment."
        out.append(_finding(
            "sessions.%s" % key, "connections", status, title, headline, verdict, rules.get(rule_key), rules.get(rule_key),
            None, {"text": "Open Sessions to see what they run and what they hold; Locks shows who blocks whom."}
            if status != "ok" else None, numbers, (), [_view_link("sessions", key)] + (
                [_view_link("locks")] if key == "lock_wait" and count else []),
            {"view": "sessions", "kind": "summary", "key": key}, impact=count))
    return out


def _tables_use(count, one, many):
    return "%d user table %s" % (count, one) if count == 1 else "%d user tables %s" % (count, many)


def _names(entries, limit=5):
    return ["%s.%s" % (e["schema"], e["table"]) for e in entries[:limit]]


def _tables_findings(rep):
    s = rep.get("summary") or {}
    why = {w["key"]: w["text"] for w in rep.get("why") or []}
    missing = rep.get("missing_primary_keys") or []
    out = []
    no_key = [m for m in missing if m.get("kind") == "no_usable_key"]
    standin = [m for m in missing if m.get("kind") == "not_null_unique"]
    caveat = next((m.get("caveat") for m in missing if m.get("caveat")), None)
    rules = rep.get("rules") or {}
    for key, entries, count, status, title, rule in (
            ("no_primary_key", no_key, s.get("no_usable_key", 0), "problem", "Tables without a usable key",
             rules.get("no_usable_key")),
            ("unique_stand_in", standin, s.get("not_null_unique", 0), "watch",
             "Tables clustered on a unique key, not a primary key", rules.get("not_null_unique"))):
        out.append(_finding(
            "tables.%s" % key, "schema", status if count else "ok", title, "%d table%s" % (
                count, "" if count == 1 else "s"),
            ("%d table%s in %s: %s." % (count, "" if count == 1 else "s", scope_text(rep["scope"]["schemas"]),
                                         ", ".join(_names(entries)) + (", ..." if count > 5 else "")))
            if count else "None in %s." % scope_text(rep["scope"]["schemas"]),
            " ".join(t for t in (why.get("clustering"), why.get("replication")) if t), rule,
            caveat if count else None,
            {"text": "Add a primary key; Tables has the checks to run first and the statement for each table."}
            if count else None, [_num("%s.%s" % (e["schema"], e["table"]), e.get("clustered_on") or e.get("kind"),
                                      e.get("size_bytes"), "B") for e in entries[:5]], (),
            [_view_link("tables", key)], {"view": "tables", "kind": "summary", "key": key}, impact=count))
    frag = [f for f in rep.get("fragmentation") or [] if f.get("status") in ("info", "watch", "problem")]
    frag.sort(key=lambda f: -(f.get("free_bytes") or 0))
    free_total = sum(f.get("free_bytes") or 0 for f in frag)
    # Tables whose tablespace could not be told apart (PROCESS denied, or a
    # name the map did not have) are not judged: never ok for them.
    unjudged = s.get("unknown_tablespace") or 0
    known = rep.get("tablespaces_known", True)
    reason_note = next((n for n in rep.get("notes") or [] if "PROCESS privilege" in n), None)
    unknown_text = ""
    if not known or unjudged:
        unknown_text = (" %s" % reason_note) if reason_note else (
            " %d table%s could not be matched to a tablespace, so %s not judged." % (
                unjudged, "" if unjudged == 1 else "s", "it is" if unjudged == 1 else "they are"))
    if frag:
        worst = "unknown" if unknown_text else "info"
        verdict = "%d file-per-table tablespace%s report reusable allocation (%s in all)." % (
            len(frag), "" if len(frag) == 1 else "s", _bytes_text(free_total))
    elif unknown_text:
        worst = "unknown"
        verdict = "Free space was not judged."
    else:
        worst = "ok"
        verdict = "No reusable allocation was reported for the file-per-table tablespaces read."
    out.append(_finding(
        "tables.fragmentation", "schema", worst, "Free space inside tablespaces",
        "%d table%s, %s free" % (len(frag), "" if len(frag) == 1 else "s", _bytes_text(free_total)) if frag else (
            "not judged" if worst == "unknown" else "none"),
        verdict + unknown_text,
        "DATA_FREE reports reusable allocation. Its amount does not establish a capacity problem or a need to rebuild.",
        rules.get("free"), frag[0].get("caveat") if frag else None,
        {"text": "Review reusable allocation and optional maintenance in Tables."} if frag else None,
        [_num("%s.%s" % (f["schema"], f["table"]), "%s free%s" % (
            _bytes_text(f.get("free_bytes")) if f.get("free_bytes") is not None else "unknown",
            " (%s%%)" % f["free_pct"] if f.get("free_pct") is not None else " (percentage unknown)"),
            f.get("free_bytes"), "B")
         for f in frag[:5]], (), [_view_link("tables", "fragmentation")],
        {"view": "tables", "kind": "summary", "key": "fragmentation"}, impact=free_total))
    engines = s.get("engines") or []
    other = [e for e in engines if (e["engine"] or "").lower() != "innodb"]
    myisam = [e for e in other if (e["engine"] or "").lower() == "myisam"]
    out.append(_finding(
        "tables.engines", "schema", "watch" if myisam else "info", "Storage engines",
        ", ".join("%s %d" % (e["engine"], e["tables"]) for e in engines) or "no tables",
        ("%s MyISAM: no transactions, writes lock the whole table, and after a crash a table may need a "
         "repair (%s)." % (_tables_use(myisam[0]["tables"], "uses", "use"),
                           "MariaDB repairs it when it is opened, by default" if rep.get("engine") == "mariadb"
                           else "REPAIR TABLE; myisam_recover_options is off by default")) if myisam else
        ("%s an engine other than InnoDB." % _tables_use(sum(e["tables"] for e in other), "uses", "use")
         if other else "Every table read uses InnoDB."),
        "Tables per storage engine in the schemas read.", "watch when user tables use MyISAM" if myisam else None,
        "Converting rebuilds the table and changes locking and crash behavior; test it first." if myisam else None,
        {"text": "Convert MyISAM tables to InnoDB where possible (ALTER TABLE ... ENGINE=InnoDB); each needs the "
                 "db-operations confirmation."} if myisam else None,
        [_num(e["engine"], "%d table%s, %s" % (e["tables"], "" if e["tables"] == 1 else "s", _bytes_text(e["bytes"])),
              e["tables"]) for e in engines], (), [_view_link("tables")],
        {"view": "tables", "kind": "summary", "key": "engines"}))
    return out


def _indexes_findings(rep, attribution):
    out = []
    if not rep.get("available"):
        note = next((n for n in rep.get("notes") or [] if "Performance Schema" in n or "instrument" in n), None)
        out.append(_finding("indexes.usage", "indexes", "unknown", "Index usage", "Not readable",
                            note or "Index usage could not be read.", links=[_view_link("indexes")],
                            source={"view": "indexes", "kind": "summary", "key": "usage"}))
    else:
        unused = [u for u in rep.get("unused") or [] if u.get("verdict") == "candidate"]
        count = (rep.get("summary") or {}).get("unused_candidates", len(unused))
        uptime = rep.get("uptime_s")
        from .indexes import SHORT_UPTIME_S, UNUSED_CAVEAT
        young = uptime is not None and uptime < SHORT_UPTIME_S
        status = ("info" if young else "watch") if count else "ok"
        out.append(_finding(
            "indexes.unused", "indexes", status, "Unused indexes", "%d candidate%s" % (count, "" if count == 1 else "s"),
            ("%d index%s had no reads or writes since the server started %s ago%s." % (
                count, "" if count == 1 else "es", _duration_text(uptime) if uptime is not None else "?",
                "; too little uptime to judge" if young else "")) if count else "Every index was used.",
            "Each index slows writes and takes buffer pool memory; an index nothing reads only costs.",
            "watch when an index has no use since startup, after at least %s of uptime" % _duration_text(SHORT_UPTIME_S),
            UNUSED_CAVEAT if count else None,
            {"text": "Make a candidate invisible first, watch a full business cycle, then drop it.",
             "sql": unused[0].get("test_sql")} if count and unused else None,
            [_num("%s.%s" % (u["schema"], u["table"]), u["index"]) for u in unused[:5]], (),
            [_view_link("indexes", "unused")], {"view": "indexes", "kind": "summary", "key": "unused"}, impact=count))
        tables = {(f["schema"], f["table"]): f for f in (attribution or {}).get("full_scans") or []}
        scans = [f for f in rep.get("full_scans") or [] if f.get("mostly")]
        scans.sort(key=lambda f: -(f.get("fetch_ms_without_index") or 0))
        links = [_view_link("indexes", "full_scans")]
        for f in scans[:ATTRIBUTION_TABLES]:
            links.append({"view": "top", "order": "no_index", "table": "%s.%s" % (f["schema"], f["table"]),
                          "label": "Open Top statements that name %s.%s" % (f["schema"], f["table"])})
        numbers = []
        for f in scans[:5]:
            matched = tables.get((f["schema"], f["table"]))
            text = "%s rows without an index, %s" % (_count_text(f.get("rows_without_index")),
                                                    _ms(f.get("fetch_ms_without_index")))
            if matched is not None:
                n = len(matched["statements"])
                text += "; %s (text match, not proof)" % (
                    "1 statement names it" if n == 1 else "%d statements name it" % n)
            numbers.append(_num("%s.%s" % (f["schema"], f["table"]), text, f.get("fetch_ms_without_index"), "ms"))
        out.append(_finding(
            "indexes.full_scans", "indexes", "info", "Tables read mostly without an index",
            "%d table%s" % (len(scans), "" if len(scans) == 1 else "s"),
            ("%d table%s spent most of their read time reading rows without an index since startup." % (
                len(scans), "" if len(scans) == 1 else "s")) if scans else "No table is read mostly without an index.",
            "Rows read without an index are table scans and row-position reads. A scan of a small lookup table is "
            "normal; the statements that name the table say which queries to look at.", None, None,
            {"text": "Look at the statements behind each table (Top statements, filtered to the table) and capture "
                     "their plans."} if scans else None, numbers, (), links,
            {"view": "indexes", "kind": "summary", "key": "full_scans"},
            impact=scans[0].get("fetch_ms_without_index") if scans else None,
            attribution="full_scans" if scans else None))
    redundant = rep.get("redundant") or []
    count = (rep.get("summary") or {}).get("redundant", len(redundant))
    out.append(_finding(
        "indexes.redundant", "indexes", "watch" if count else "ok", "Redundant indexes",
        "%d index%s" % (count, "" if count == 1 else "es"),
        ("1 index is a duplicate or a leftmost prefix of another index on its table." if count == 1 else
         "%d indexes are duplicates or leftmost prefixes of other indexes on their tables." % count)
        if count else "No index duplicates another.",
        redundant[0].get("why") if redundant else "Each extra index slows writes and uses buffer pool memory.",
        "watch when an index is a duplicate or a leftmost prefix of another index on the same table",
        redundant[0].get("caveat") if redundant else None,
        {"text": "Make it invisible first, then drop it.", "sql": redundant[0].get("test_sql")} if redundant else None,
        [_num("%s.%s" % (r["schema"], r["table"]), "%s (%s) %s %s" % (r["index"], r.get("columns"), r.get("reason"),
                                                                     r.get("dominant"))) for r in redundant[:5]], (),
        [_view_link("indexes", "redundant")], {"view": "indexes", "kind": "summary", "key": "redundant"}, impact=count))
    return out


def _ms(value):
    from .perf_common import ms_text
    return ms_text(value)


def _waits_findings(rep):
    if not rep.get("available"):
        text = next(iter(rep.get("notes") or []), "Wait events could not be read.")
        return [_finding("waits.top", "waits", "unknown", "Top wait events", "Not readable", text,
                         links=[_view_link("waits")], source={"view": "waits", "kind": "summary", "key": "top"})]
    events = ((rep.get("window") or {}).get("events") or [])[:3]
    numbers = [_num(e.get("event"), "%s per second of wall time (%s waits)" % (_ms(e.get("wait_ms_per_s")),
                                                                                 _count_text(e.get("count"))),
                    e.get("wait_ms_per_s"), "ms/s") for e in events]
    out = [_finding(
        "waits.top", "waits", "info", "Top wait events in the window",
        events[0]["event"] if events else "none timed",
        ("The longest waits in the %s s window: %s." % (_count_text(rep.get("elapsed_s")), ", ".join(
            "%s %s/s" % (e["event"], _ms(e.get("wait_ms_per_s"))) for e in events))) if events else
        "No timed wait in the window.",
        "Wait time is summed across threads and classes overlap, so it is never added up.", None, None,
        {"text": "Open Waits for the classes and the events since startup."} if events else None, numbers, (),
        [_view_link("waits")], {"view": "waits", "kind": "summary", "key": "top"},
        impact=events[0].get("wait_ms_per_s") if events else None)]
    off = [c for c in rep.get("coverage") or [] if c.get("state") in ("off", "partial")]
    if off:
        out.append(_finding(
            "waits.coverage", "observability", "info", "Wait classes not instrumented",
            "%d class%s" % (len(off), "" if len(off) == 1 else "es"),
            "Not timed: %s. Their waits are not in this report; no mutex or rwlock waits never means no contention."
            % ", ".join("%s (%d of %d on)" % (c["label"], c.get("enabled") or 0, c.get("total") or 0) for c in off),
            "Waits shows only instrumented classes.", None, None,
            {"text": "Instruments has the statements that turn a class on for an investigation."},
            [_num(c["label"], "%d of %d on" % (c.get("enabled") or 0, c.get("total") or 0)) for c in off], (),
            [_view_link("waits"), _view_link("instruments")], {"view": "waits", "kind": "coverage", "key": "coverage"}))
    return out


def _io_findings(rep):
    window = rep.get("window") or {}
    tables = (window.get("tables") or [])[:3]
    files = (window.get("files") or [])[:3]
    numbers = []
    for t in tables:
        label = "%s.%s" % (t["schema"], t["table"])
        if t.get("total_ms_per_s") is None:
            numbers.append(_num(label, "%s rows read, %s rows written (timing unavailable)" % (
                _count_text(t.get("read_rows")), _count_text(t.get("write_rows")))))
        else:
            numbers.append(_num(label, "%s per second, %s rows" % (
                _ms(t["total_ms_per_s"]), _count_text(t.get("ops"))), t["total_ms_per_s"], "ms/s"))
    numbers += [_num(f.get("label") or f.get("kind"), "%s (%s read, %s written)" % (
        _ms(f.get("total_ms")), _bytes_text(f.get("read_bytes") or 0), _bytes_text(f.get("write_bytes") or 0)),
        f.get("total_ms"), "ms") for f in files]
    verdict = []
    if tables:
        verdict.append("Busiest tables in the %s s window: %s." % (_count_text(rep.get("elapsed_s")), ", ".join(
            "%s.%s" % (t["schema"], t["table"]) for t in tables)))
    if files:
        verdict.append("Busiest files: %s." % ", ".join(f.get("label") or f.get("kind") or "?" for f in files))
    if not verdict:
        verdict.append("Per-table and per-file I/O was not readable (%s); server-wide InnoDB counters are in I/O."
                       % ("Performance Schema is off" if not rep.get("performance_schema") else "no activity"))
    return [_finding("io.top", "io", "info", "I/O hot spots in the window", tables[0]["table"] if tables else "",
                     " ".join(verdict), "Where reads and writes went; no threshold applies.", None, None, None,
                     numbers, (), [_view_link("io")], {"view": "io", "kind": "summary", "key": "top"},
                     impact=tables[0].get("total_ms_per_s") if tables else None)]


def _instruments_findings(rep):
    out = []
    status = rep.get("status") or {}
    if not rep.get("available"):
        text = status.get("text") if rep.get("unavailable") == "performance_schema_off" else \
            "Performance Schema setup tables were not readable (%s)." % (rep.get("unavailable") or "?")
        out.append(_finding("instruments.views", "observability",
                            "watch" if rep.get("unavailable") == "performance_schema_off" else "unknown",
                            "Views that need Performance Schema", "Performance Schema off"
                            if rep.get("unavailable") == "performance_schema_off" else "Not readable", text,
                            "Top statements, Waits, I/O per table and file, and unused indexes need it.",
                            "watch when off: Top statements, Waits, I/O and unused indexes need it", None,
                            {"text": "Turn it on in my.cnf and restart (it cannot change at runtime).",
                             "config": "performance_schema = ON  # under [mysqld]"}
                            if rep.get("unavailable") == "performance_schema_off" else None,
                            links=[_view_link("instruments", "ps")],
                            source={"view": "instruments", "kind": "features", "key": "views"}))
    else:
        # Advisory features (an optional guard such as the replication receiver
        # check) are listed in Instruments but never make a view "not ready".
        features = [f for f in rep.get("features") or []
                    if f.get("status") not in ("not_needed",) and not f.get("advisory")]
        off = [f for f in features if f.get("status") in ("off", "partial")]
        worst = "watch" if any(f.get("status") == "off" for f in off) else "info" if off else "ok"
        out.append(_finding(
            "instruments.views", "observability", worst, "Views fed by Performance Schema",
            "%d of %d ready" % (len(features) - len(off), len(features)),
            ("Not fully collected: %s." % "; ".join("%s (%s)" % (f["label"], "; ".join(f.get("missing") or [])
                                                                  or f["status"]) for f in off))
            if off else "Every myflames view that reads Performance Schema has what it needs.",
            "Each view needs its consumers and instruments on; Instruments shows what each one costs.",
            "watch when a view's required consumer or instrument is off", None,
            {"text": "Instruments has a Minimal preset with the statements to run (myflames never runs them)."}
            if off else None,
            [_num(f["label"], f["status"]) for f in features], (), [_view_link("instruments")],
            {"view": "instruments", "kind": "features", "key": "views"}))
    lost = status.get("lost") or []
    if lost:
        out.append(_finding(
            "instruments.lost", "observability", "watch", "Performance Schema lost data", ", ".join(lost),
            "%s above zero: some rows could not be recorded, so absent data does not prove there was no activity."
            % ", ".join(lost), "The *_lost counters count events or objects the fixed-size buffers had no room for.",
            "watch when any Performance_schema_*_lost counter is above zero", None,
            {"text": "Raise the matching performance_schema_max_* or *_size setting (needs a restart)."},
            [_num(n, "above zero") for n in lost], (), [_view_link("instruments", "ps")],
            {"view": "instruments", "kind": "status", "key": "lost"}))
    return out


def _view_status(run):
    if run is None:
        return "skipped"
    if run.get("error"):
        return "failed"
    rep = run.get("report") or {}
    if rep.get("available") is False:
        return "limited"
    return "ok"


#: Aggregation order for sections and the report: problem > watch > unknown > info > ok.
_PRECEDENCE = ("problem", "watch", "unknown", "info", "ok")


def _worst(statuses):
    present = set(statuses)
    return next((s for s in _PRECEDENCE if s in present), "ok")


def _counts_text(counts, statuses):
    words = {"problem": ("problem", "problems"), "watch": ("to watch", "to watch"),
             "unknown": ("unknown", "unknown")}
    return ", ".join("%d %s" % (counts[s], words[s][0] if counts[s] == 1 else words[s][1])
                     for s in statuses if counts[s])


def summarize(findings, failures=()):
    """Counts per status, the overall status (problem > watch > unknown >
    ok: unknown findings or views that did not run keep a report from ok;
    info never raises it), and one line of text. *findings* must be ranked."""
    summary = {s: sum(1 for f in findings if f["status"] == s) for s in STATUSES}
    summary["findings"] = len(findings)
    summary["status"] = _worst([f["status"] for f in findings if f["status"] != "info"]
                               + (["unknown"] if failures else []) + ["ok"])
    first = look_first(findings)
    first_title = next((f["title"] for f in findings if f["id"] == first), None)
    summary["text"] = "%d problem%s, %d to watch, %d unknown, %d ok%s%s" % (
        summary["problem"], "" if summary["problem"] == 1 else "s", summary["watch"], summary["unknown"],
        summary["ok"], "; %d view%s not read" % (len(failures), "" if len(failures) == 1 else "s") if failures else "",
        "; look first at %s" % first_title if first_title else "")
    return summary


def rank_findings(findings):
    """Problems first, then watch, unknown, info, ok; inside a status by
    family (FAMILY_RANK), then by measured impact, larger first. The one
    ranking shared by every output."""
    def key(f):
        impact = f.get("impact")
        return (STATUS_RANK.get(f["status"], 2), FAMILY_RANK.get(f["family"], 99),
                -(impact if isinstance(impact, (int, float)) else -1), f["id"])
    return sorted(findings, key=key)


def look_first(findings):
    """The id of the one finding to look at first (a problem, else a watch),
    or None. *findings* must be ranked."""
    for f in findings:
        if f["status"] in ("problem", "watch"):
            return f["id"]
    return None


def compose_report(server, runs, attribution, interval=DEFAULT_INTERVAL, schemas=None, schema_gate=None,
                   started_at=None, elapsed_s=None):
    """The ``report-1.0`` report from the views' results. Pure."""
    def rep(key):
        run = runs.get(key) or {}
        return run.get("report") if not run.get("error") else None

    findings = []
    health = rep("health")
    if health:
        findings += _health_findings(health)
    config = rep("config")
    if config:
        findings += _check_findings("config", config.get("checks") or [], _CONFIG_FAMILY)
    checks = rep("checks")
    if checks:
        from .server_checks import engine_cache_checks
        inventory_run = runs.get("tables") or {}
        cache_checks = engine_cache_checks(checks, rep("tables"), inventory_run.get("error") or schema_gate)
        findings += _check_findings("checks", list(checks.get("checks") or []) + cache_checks, {})
    innodb = rep("innodb")
    if innodb:
        findings += _innodb_findings(innodb, health is not None)
    sessions = rep("sessions")
    if sessions:
        findings += _sessions_findings(sessions)
    tables = rep("tables")
    if tables:
        findings += _tables_findings(tables)
    indexes = rep("indexes")
    if indexes:
        findings += _indexes_findings(indexes, attribution)
    waits = rep("waits")
    if waits:
        findings += _waits_findings(waits)
    io = rep("io")
    if io:
        findings += _io_findings(io)
    instruments = rep("instruments")
    if instruments:
        findings += _instruments_findings(instruments)
    findings = rank_findings(findings)

    views, failures = [], []
    for key in ("health", "waits", "io", "config", "innodb", "sessions", "instruments", "checks", "tables",
                "indexes"):
        run = runs.get(key)
        report = (run or {}).get("report") or {}
        status = _view_status(run)
        entry = {"key": key, "label": VIEW_LABELS[key], "status": status,
                 "schema_version": report.get("schema_version"), "started_at": (run or {}).get("started_at"),
                 "taken_at": report.get("taken_at") or (run or {}).get("taken_at"),
                 "elapsed_s": (run or {}).get("elapsed_s"), "error": (run or {}).get("error"),
                 "notes": list(report.get("notes") or [])}
        if status == "skipped":
            entry["error"] = schema_gate or "Not run."
        views.append(entry)
        if status in ("failed", "skipped"):
            failures.append({"view": key, "label": VIEW_LABELS[key], "status": status, "error": entry["error"]})

    by_family = {}
    for f in findings:
        by_family.setdefault(f["family"], []).append(f)
    view_status = {v["key"]: v["status"] for v in views}
    sections = []
    for key, title, sources in SECTIONS:
        items = by_family.get(key, [])
        counts = {s: sum(1 for f in items if f["status"] == s) for s in STATUSES}
        missing = [VIEW_LABELS[v] for v in sources if view_status.get(v) in ("failed", "skipped")]
        notes = []
        if missing:
            notes.append("Missing because a view failed or was skipped: %s." % ", ".join(missing))
        if key == "query_cache" and server.engine == "mysql":
            notes.append("MySQL 8.0 and later have no query cache.")
        # Precedence: problem > watch > unknown > info > ok. A view that
        # failed or was skipped makes the section unknown, never ok.
        status = _worst([f["status"] for f in items] + (["unknown"] if missing else []))
        if not items and not missing:
            status = "info"
        flagged = [f["title"] for f in items if f["status"] in ("problem", "watch", "unknown")]
        if flagged:
            # Titles stay under their own status: an unknown (not judged)
            # finding never reads as one to watch beside a WATCH section.
            text = "; ".join("%s: %s" % (_counts_text(counts, (s,)),
                                         ", ".join(f["title"] for f in items if f["status"] == s))
                             for s in ("problem", "watch", "unknown") if counts[s])
            if counts["ok"]:
                text += "; %d ok" % counts["ok"]
        elif items and status == "info":
            text = "%d for information, no rule applies" % len(items)
        elif items:
            text = "%d checked, nothing to look at" % len(items)
        else:
            text = notes[0] if notes else "Nothing to check on this server."
        if missing and items:
            text += ". " + notes[0]
        sections.append({"key": key, "title": title, "status": status, "summary": text, "counts": counts,
                         "findings": [f["id"] for f in items], "views": list(sources), "notes": notes})

    summary = summarize(findings, failures)
    first = look_first(findings)

    notes = ["Statuses are rules of thumb, not verdicts: each finding names its rule. myflames never applies a "
             "recommendation; a suggested SET names its scope and needs your confirmation before anyone runs it."]
    if server.uptime is not None and server.uptime < 86400:
        notes.append("The server started %s ago, so counters since startup cover only that time."
                     % _duration_text(server.uptime))
    note = lost_note(server)
    if note:
        notes.append(note)
    if schema_gate:
        notes.append(schema_gate)
    if failures:
        notes.append("%d of %d views did not run or failed: %s." % (
            len(failures), len(views), "; ".join("%s: %s" % (f["label"], f["error"]) for f in failures)))
    comment = server.var("version_comment")
    return {
        "schema_version": SCHEMA_VERSION,
        "myflames_version": __version__,
        "engine": server.engine,
        "server_version": server.version or None,
        "taken_at": now(),
        "uptime_s": server.uptime,
        "performance_schema": server.ps_on,
        "header": {
            "server": {"engine": server.engine, "version": server.version or None, "version_comment": comment,
                       "uptime_s": server.uptime, "performance_schema": server.ps_on},
            "started_at": started_at, "elapsed_s": elapsed_s, "interval": interval,
            "scope": {"schemas": list(schemas) if schemas else None, "all_user_schemas": not schemas,
                      "text": scope_text(schemas)},
            "views": views, "failures": failures,
        },
        "summary": summary,
        "look_first": first,
        "findings": findings,
        "sections": sections,
        "attribution": attribution,
        "checklist": {"covered": [{"key": k, "label": l} for k, l in COVERED], "not_checked": list(NOT_CHECKED)},
        "health_metrics": health_metrics(health),
        "notes": notes,
    }


def at_or_worse(report, level):
    """Findings at *level* ('watch' or 'problem') or worse."""
    floor = SEVERITY[level]
    return [f for f in report["findings"] if SEVERITY.get(f["status"], -1) >= floor]

# ---- single flight -----------------------------------------------------------------------------


class _Flight(object):
    def __init__(self, options):
        self.options = options
        self.done = threading.Event()
        self.result = None
        self.error = None


_FLIGHTS = {}
_FLIGHTS_LOCK = threading.Lock()
#: Per-process key for the credential fingerprint in flight keys; never
#: stored or returned.
_FLIGHT_SECRET = os.urandom(32)


def flight_key(options, profile_id=None, safety=None):
    """The identity of one connection for :func:`run_single_flight`: the
    profile and every effective connection and TLS setting, plus an HMAC of
    the password with a per-process random key, so only requests that would
    authenticate the same way share a run (the password itself never enters
    the key). *safety* (``type`` and ``read_only``, as in
    dbops.connection_identity) is part of the key when given."""
    password = options.get("password")
    fingerprint = None
    if password is not None:
        fingerprint = hmac.new(_FLIGHT_SECRET, password.encode("utf-8"), hashlib.sha256).hexdigest()
    fields = tuple(sorted((k, str(v)) for k, v in options.items()
                          if k not in ("password", "connect_timeout", "query_timeout") and v is not None))
    key = ("profile", str(profile_id) if profile_id else None, "settings", fields, "password", fingerprint)
    if isinstance(safety, dict):
        key += ("type", safety.get("type") or "production", "read_only", safety.get("read_only") is True)
    return key


def run_single_flight(key, options, work):
    """Run *work()* once per *key* (a connection identity) at a time. A
    second call with the same *options* while it runs waits and gets the same
    result as its own deep copy; a call with different options is refused."""
    with _FLIGHTS_LOCK:
        flight = _FLIGHTS.get(key)
        owner = flight is None
        if owner:
            flight = _FLIGHTS[key] = _Flight(options)
    if not owner:
        if flight.options != options:
            raise ValueError("A full analysis with other settings is already running for this connection; wait "
                             "for it to finish.")
        flight.done.wait()
        if flight.error is not None:
            raise flight.error
        return copy.deepcopy(flight.result)
    try:
        flight.result = work()
        return copy.deepcopy(flight.result)
    except BaseException as exc:
        flight.error = exc
        raise
    finally:
        with _FLIGHTS_LOCK:
            _FLIGHTS.pop(key, None)
        flight.done.set()

# ---- text, digest, Markdown and HTML ------------------------------------------------------------


_MARK = {"ok": "ok", "watch": "WATCH", "problem": "PROBLEM", "info": "-", "unknown": "?"}
_WRAP = 100


def _server_line(report):
    h = report["header"]["server"]
    engine = "MariaDB" if report["engine"] == "mariadb" else "MySQL"
    line = "%s %s" % (engine, report["server_version"] or "?")
    if h.get("version_comment"):
        line += " (%s)" % h["version_comment"]
    if report.get("uptime_s") is not None:
        line += ", up %s" % _duration_text(report["uptime_s"])
    return line


#: Fields an export may name its connection with (never a password).
SOURCE_FIELDS = ("label", "host", "port", "user", "type")


def normalize_source(source):
    """The connection a report was read from, as an export names it: label,
    host, port, user and connection type (production or development), from
    the workspace. Any other field (a password included) is dropped; None
    when nothing usable is left."""
    if not isinstance(source, dict):
        return None
    out = {}
    for name in ("label", "host", "user"):
        value = source.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = value.strip()[:200]
    port = source.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
        out["port"] = port
    if source.get("type") in ("production", "development"):
        out["type"] = source["type"]
    return out or None


def _source_line(report):
    """"Production (app@db1:3306), production connection", or "" without a source."""
    src = report.get("source")
    if not src:
        return ""
    target = "%s%s:%s" % ("%s@" % src["user"] if src.get("user") else "", src.get("host") or "?", src.get("port") or "?")
    label = src.get("label") or target
    text = label if target in label else "%s (%s)" % (label, target)
    if src.get("type"):
        text += ", %s connection" % src["type"]
    return text


def _views_line(report):
    views = report["header"]["views"]
    ran = sum(1 for v in views if v["status"] in ("ok", "limited"))
    return "%d of %d views read" % (ran, len(views))


def _action_lines(action):
    if not action:
        return []
    lines = []
    if action.get("text"):
        lines.append(("Suggestion", action["text"]))
    if action.get("sql"):
        lines.append(("SQL", action["sql"]))
    if action.get("config"):
        lines.append(("my.cnf", action["config"]))
    return lines


def _statement_text(item, width):
    text = item.get("sample_text") or item.get("digest_text") or ""
    text = " ".join(text.split())
    return text if len(text) <= width else text[:width - 1] + "…"


def coverage_lines(attribution):
    """The attribution's scope and limits as sentences, shared by every
    output: cumulative digest counters against Health's window, uptime and the
    oldest FIRST_SEEN, the overflow row and lost digests, the list caps. The
    gate and its fix when statements could not be linked."""
    a = attribution or {}
    if not a.get("available"):
        out = [a.get("gate") or "Statements could not be linked to these numbers."]
        if a.get("fix") and a.get("fix") != a.get("gate"):
            out.append(a["fix"])
        return out
    from .discover import TABLE_CANDIDATES
    cov = a.get("coverage") or {}
    out = ["Digest counters are cumulative since the server started or the digest table was truncated (uptime %s%s), "
           "not over Health's window." % (_duration_text(cov.get("uptime_s")),
                                          "; the oldest digest listed was first seen %s" % cov["first_seen"]
                                          if cov.get("first_seen") else "")]
    window = a.get("health_window") or {}
    if window.get("elapsed_s") is not None and window.get("select_full_join") is not None:
        out.append("Health counted %s joins without an index in its %s s window." % (
            _count_text(window["select_full_join"]), _count_text(window["elapsed_s"])))
    overflow = cov.get("overflow")
    if overflow:
        out.append("The digest table is full: %s executions (%s) were summed into its overflow row and are not "
                   "attributed." % (_count_text(overflow.get("executions")), _ms(overflow.get("total_ms"))))
    else:
        out.append("The digest table has no overflow row, so every recorded statement can be listed.")
    if cov.get("digest_lost"):
        out.append("Performance_schema_digest_lost is %s: those statements were not recorded at all."
                   % _count_text(cov["digest_lost"]))
    out.append("At most %d statements are listed for each number." % ATTRIBUTION_LIMIT)
    for t in a.get("full_scans") or []:
        if t.get("capped"):
            out.append("For %s.%s only the %d heaviest digests that name it were checked." % (
                t["schema"], t["table"], TABLE_CANDIDATES))
    return out


def _table_rows_state(t):
    """('error', reason), ('empty', text) or ('rows', None) for one table's attribution."""
    if t.get("error"):
        return "error", "Could not read: %s" % t["error"]
    if not t.get("statements"):
        return "empty", "No recorded statement names it and reads without an index."
    return "rows", None


def format_text(report):
    """The default terminal output (server strings are escaped by the CLI)."""
    wrap = textwrap.TextWrapper(width=_WRAP, initial_indent="    ", subsequent_indent="    ")
    s = report["summary"]
    h = report["header"]
    lines = ["Full analysis: %s" % _server_line(report)]
    if _source_line(report):
        lines.append("Connection: %s" % _source_line(report))
    lines += [
             "Collected %s in %s s (windows of %d s); %s; %s." % (
                 report["taken_at"][:19].replace("T", " ") + " UTC", _count_text(h.get("elapsed_s")),
                 h["interval"], _views_line(report), h["scope"]["text"]),
             "Status: %s (%s)" % (s["status"].upper() if s["status"] != "ok" else "ok", s["text"]), ""]
    by_id = {f["id"]: f for f in report["findings"]}
    flagged = [f for f in report["findings"] if f["status"] in ("problem", "watch")]
    if report.get("look_first"):
        f = by_id[report["look_first"]]
        lines.append("LOOK AT FIRST")
        lines.append("  %s  %s: %s" % (_MARK[f["status"]], f["title"], f["headline"]))
        lines += wrap.wrap(f["verdict"])
        lines.append("")
    if flagged:
        lines.append("FINDINGS (problem and watch, ranked)")
        for f in flagged:
            lines.append("  %s  %s: %s  [%s]" % (_MARK[f["status"]], f["title"], f["headline"],
                                                SECTION_TITLES.get(f["family"], f["family"])))
            lines += wrap.wrap(f["verdict"])
            if f.get("numbers"):
                lines += wrap.wrap("Evidence: " + "; ".join("%s %s" % (n["label"], n["text"])
                                                           for n in f["numbers"][:4]))
            if f.get("rule"):
                lines += wrap.wrap("Rule of thumb: %s." % f["rule"].rstrip("."))
            for label, text in _action_lines(f.get("action")):
                for i, part in enumerate(text.split("\n")):
                    lines += wrap.wrap(("%s: %s" % (label, part)) if i == 0 else "  " + part)
            if f.get("caveat"):
                lines += wrap.wrap("Caveat: %s" % f["caveat"])
        lines.append("")
    unknown = [f for f in report["findings"] if f["status"] == "unknown"]
    if unknown:
        lines.append("NOT JUDGED (unknown)")
        for f in unknown:
            lines.append("  ?  %s" % f["title"])
            lines += wrap.wrap(f["verdict"] or f["headline"])
        lines.append("")
    lines.append("SECTIONS")
    width = max(len(sec["title"]) for sec in report["sections"])
    for sec in report["sections"]:
        lines.append(("  %-7s  %s  %s" % (_MARK[sec["status"]], sec["title"].ljust(width), sec["summary"])).rstrip())
    lines.append("")
    lines += _attribution_text(report)
    failures = h.get("failures") or []
    if failures:
        lines.append("VIEWS NOT READ")
        for f in failures:
            lines += textwrap.wrap("%s %s: %s" % (f["label"], f["status"], f["error"]), width=_WRAP,
                                   initial_indent="  ", subsequent_indent="    ")
        lines.append("")
    lines.append("NOT CHECKED")
    for item in report["checklist"]["not_checked"]:
        lines += textwrap.wrap("%s: %s" % (item["label"], item["reason"]), width=_WRAP, initial_indent="  ",
                               subsequent_indent="    ")
    return "\n".join(lines) + "\n"


def _attribution_text(report):
    a = report.get("attribution") or {}
    lines = ["STATEMENTS BEHIND THE NUMBERS"]
    for sentence in coverage_lines(a):
        lines += textwrap.wrap(sentence, width=_WRAP, initial_indent="  ", subsequent_indent="    ")
    if not a.get("available"):
        return lines + [""]
    joins = (a.get("full_joins") or {}).get("statements") or []
    lines.append("  Joins without an index (SUM_SELECT_FULL_JOIN, SUM_SELECT_RANGE_CHECK):")
    if not joins:
        lines.append("    none recorded")
    for item in joins:
        lines.append("    %s full joins, %s range checks, %s calls  %s  %s" % (
            _count_text(item["select_full_join"]), _count_text(item["select_range_check"]), _count_text(item["count"]),
            (item.get("digest") or "")[:12], _statement_text(item, 60)))
    for t in a.get("full_scans") or []:
        lines.append("  Statements that name %s.%s and read without an index (%s):" % (
            t["schema"], t["table"], t["label"]))
        state, text = _table_rows_state(t)
        if state != "rows":
            lines.append("    %s" % text)
        for item in t["statements"]:
            lines.append("    %s no-index executions, %s calls, %s match  %s  %s" % (
                _count_text(item["no_index_used"]), _count_text(item["count"]), item["table_match"]["kind"],
                (item.get("digest") or "")[:12], _statement_text(item, 50)))
    for caveat in a.get("caveats") or []:
        lines += textwrap.wrap("Caveat: " + caveat, width=_WRAP, initial_indent="  ", subsequent_indent="    ")
    return lines + [""]


def format_digest(report):
    """Token-cheap text for an AI agent: the server line, one line per
    non-ok finding with its numbers and rule, ok findings counted, and the
    coverage notes."""
    s = report["summary"]
    out = ["server: %s; %s; %s" % (_server_line(report), _views_line(report), report["header"]["scope"]["text"])]
    if _source_line(report):
        out.append("connection: %s" % _source_line(report))
    out.append("status: %s (%s)" % (s["status"], s["text"]))
    if report.get("look_first"):
        out.append("look_first: %s" % report["look_first"])
    for f in report["findings"]:
        if f["status"] in ("ok",):
            continue
        if f["status"] == "info" and not f.get("attribution"):
            continue
        numbers = "; ".join("%s=%s" % (n["label"], n["text"]) for n in f["numbers"][:4])
        line = "[%s] %s %s: %s" % (f["status"], f["id"], f["title"], f["headline"] or f["verdict"])
        if numbers:
            line += " | %s" % numbers
        if f.get("rule") and f["status"] in ("problem", "watch"):
            line += " | rule: %s" % f["rule"]
        out.append(" ".join(line.split()))
    out.append("ok: %d; info: %d (not listed)" % (s["ok"], s["info"]))
    a = report.get("attribution") or {}
    out.append("attribution: %s" % " ".join(coverage_lines(a)))
    if a.get("available"):
        out.append("attribution caveat: table matches are a text match, not proof (views, routines, aliases, other "
                   "schemas and digest text cut at performance_schema_max_digest_length break them; the counters "
                   "are per statement, not per table)")
        for item in (a.get("full_joins") or {}).get("statements") or []:
            out.append("full_join digest %s: %s joins, %s calls: %s" % (
                (item.get("digest") or "")[:12], item["select_full_join"], item["count"], _statement_text(item, 120)))
        for t in a.get("full_scans") or []:
            state, text = _table_rows_state(t)
            if state != "rows":
                out.append("no_index %s.%s: %s" % (t["schema"], t["table"], text))
            for item in t["statements"]:
                out.append("no_index %s.%s (text match, not proof) digest %s: %s executions: %s" % (
                    t["schema"], t["table"], (item.get("digest") or "")[:12], item["no_index_used"],
                    _statement_text(item, 120)))
    for f in report["header"].get("failures") or []:
        out.append("not read: %s: %s" % (f["view"], f["error"]))
    out.append("not checked: %s" % ", ".join(i["label"] for i in report["checklist"]["not_checked"]))
    return "\n".join(out) + "\n"


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]|~])")
_MD_BLOCK_START = re.compile(r"^([#=+-])")
_MD_ORDERED = re.compile(r"^(\d+)([.)])(?=\s|$)")


def md(text):
    """Markdown-safe text for a table cell, a list item or a paragraph: HTML
    characters as entities, inline Markdown punctuation backslash-escaped, a
    leading block marker (#, -, +, =, "1.") escaped, line breaks as spaces."""
    text = html.escape(" ".join(str(text if text is not None else "").split()), quote=True)
    text = _MD_SPECIAL.sub(r"\\\1", text)
    text = _MD_BLOCK_START.sub(r"\\\1", text)
    return _MD_ORDERED.sub(r"\1\\\2", text)


def md_code(text):
    """A fenced block longer than any backtick run in *text*."""
    text = str(text or "")
    longest = max([len(r) for r in re.findall(r"`+", text)] + [2])
    fence = "`" * (longest + 1)
    return "%s\n%s\n%s" % (fence, text.replace("\r", ""), fence)


def format_markdown(report):
    s = report["summary"]
    h = report["header"]
    out = ["# Full analysis: %s" % md(_server_line(report)), ""]
    if _source_line(report):
        out += ["Connection: %s" % md(_source_line(report)), ""]
    out += [
           "Collected %s in %s s (windows of %d s); %s; %s." % (
               md(report["taken_at"]), md(_count_text(h.get("elapsed_s"))), h["interval"], md(_views_line(report)),
               md(h["scope"]["text"])), "",
           "**Status: %s.** %s" % (md(s["status"]), md(s["text"])), ""]
    flagged = [f for f in report["findings"] if f["status"] in ("problem", "watch")]
    if flagged:
        out += ["## Findings to look at", ""]
        for f in flagged:
            first = " (look at first)" if f["id"] == report.get("look_first") else ""
            out.append("### %s: %s%s" % (md(f["status"].upper()), md(f["title"]), md(first)))
            out.append("")
            out.append(md(f["verdict"]))
            out.append("")
            if f.get("rule"):
                out += ["*Rule of thumb:* %s" % md(f["rule"]), ""]
            if f.get("numbers"):
                out += ["| Evidence | Value |", "| --- | --- |"]
                out += ["| %s | %s |" % (md(n["label"]), md(n["text"])) for n in f["numbers"]]
                out.append("")
            action = f.get("action") or {}
            if action.get("text"):
                out += ["*Suggestion:* %s" % md(action["text"]), ""]
            if action.get("sql"):
                out += [md_code(action["sql"]), ""]
            if action.get("config"):
                out += [md_code(action["config"]), ""]
            if f.get("caveat"):
                out += ["*Caveat:* %s" % md(f["caveat"]), ""]
    unknown = [f for f in report["findings"] if f["status"] == "unknown"]
    if unknown:
        out += ["## Not judged", ""]
        out += ["- **%s:** %s" % (md(f["title"]), md(f["verdict"] or f["headline"])) for f in unknown]
        out.append("")
    out += ["## Sections", "", "| Status | Section | Summary |", "| --- | --- | --- |"]
    out += ["| %s | %s | %s |" % (md(sec["status"]), md(sec["title"]), md(sec["summary"])) for sec in report["sections"]]
    out.append("")
    a = report.get("attribution") or {}
    out += ["## Statements behind the numbers", ""]
    out += ["- %s" % md(line) for line in coverage_lines(a)] + [""]
    if a.get("available"):
        out += ["| Digest | Full joins | Range checks | Calls | Statement |", "| --- | --- | --- | --- | --- |"]
        for item in (a.get("full_joins") or {}).get("statements") or []:
            out.append("| %s | %s | %s | %s | %s |" % (md((item.get("digest") or "")[:12]), item["select_full_join"],
                                                      item["select_range_check"], item["count"],
                                                      md(_statement_text(item, 120))))
        out.append("")
        for t in a.get("full_scans") or []:
            out += ["Statements that name %s and read without an index (%s):" % (
                md("%s.%s" % (t["schema"], t["table"])), md(t["label"])), ""]
            state, text = _table_rows_state(t)
            if state != "rows":
                out += [md(text), ""]
                continue
            out += ["| Digest | No-index executions | Match | Statement |", "| --- | --- | --- | --- |"]
            out += ["| %s | %s | %s | %s |" % (md((i.get("digest") or "")[:12]), i["no_index_used"],
                                               md(i["table_match"]["kind"]), md(_statement_text(i, 120)))
                    for i in t["statements"]]
            out.append("")
        out += ["Caveats:", ""] + ["- %s" % md(c) for c in a.get("caveats") or []] + [""]
    out += ["## Not checked", ""]
    out += ["- %s: %s" % (md(i["label"]), md(i["reason"])) for i in report["checklist"]["not_checked"]]
    for f in report["header"].get("failures") or []:
        out.append("- %s %s: %s" % (md(f["label"]), md(f["status"]), md(f["error"])))
    out += ["", "## Notes", ""] + ["- %s" % md(n) for n in report["notes"]]
    out += ["", "Made with myflames %s. Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan "
                "FlameGraphs." % md(report["myflames_version"]), ""]
    return "\n".join(out)


_BADGE = {"problem": "badge-error", "watch": "badge-warn", "info": "badge-info", "ok": "badge-ok", "unknown": ""}
_SEV = {"problem": "sev-error", "watch": "sev-warn", "info": "sev-info", "ok": "", "unknown": ""}
_HTML_EXTRA = """
.report-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.report-table th, .report-table td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border);
  vertical-align: top; overflow-wrap: anywhere; }
.finding h3 { margin: 0; font-size: 14px; grid-column: 2; }
.finding p { margin: 0; grid-column: 2; }
.finding .badge { align-self: start; }
.finding { grid-template-columns: auto minmax(0, 1fr); }
.finding .kv-table, .finding pre, .finding details { grid-column: 1 / -1; min-width: 0; }
.finding pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; padding: 10px 14px; border-radius: 6px;
  background: var(--code-bg); color: var(--code-fg); font-family: var(--font-mono); font-size: 12.5px; }
.finding .kv-table { width: auto; max-width: 100%; }
.finding .kv-table td { word-break: normal; overflow-wrap: anywhere; }
.muted { color: var(--muted); }
@media (max-width: 640px) { main.report { padding: 0 12px; } .report-table { font-size: 12px; } }
"""


def _e(text):
    return html.escape(str(text if text is not None else ""), quote=True)


def _badge(status):
    return '<span class="badge %s">%s</span>' % (_BADGE.get(status, ""), _e(status))


def format_html(report):
    """A static, self-contained page: no script runs; every server string is
    escaped; the report JSON is embedded as data."""
    from .output_html_report import _CSS
    s = report["summary"]
    h = report["header"]
    parts = []
    parts.append('<section aria-labelledby="summary"><h2 id="summary">Summary</h2>'
                 '<p>%s <strong>%s</strong></p><p class="muted">Collected %s in %s s (windows of %d s); %s; %s.</p>'
                 '</section>' % (_badge(s["status"]), _e(s["text"]), _e(report["taken_at"]),
                                 _e(_count_text(h.get("elapsed_s"))), h["interval"], _e(_views_line(report)),
                                 _e(h["scope"]["text"])))
    if _source_line(report):
        parts[-1] = parts[-1].replace("</section>", '<p class="muted">Connection: %s</p></section>'
                                      % _e(_source_line(report)), 1)
    flagged = [f for f in report["findings"] if f["status"] in ("problem", "watch")]
    if flagged:
        items = []
        for f in flagged:
            body = ['<li class="warning finding %s">%s<h3>%s%s</h3>' % (
                _SEV.get(f["status"], ""), _badge(f["status"]), _e(f["title"]),
                " (look at first)" if f["id"] == report.get("look_first") else "")]
            body.append("<p>%s</p>" % _e(f["verdict"]))
            if f.get("rule"):
                body.append('<p class="muted">Rule of thumb: %s</p>' % _e(f["rule"]))
            if f.get("numbers"):
                body.append('<table class="kv-table"><tbody>%s</tbody></table>' % "".join(
                    "<tr><th>%s</th><td>%s</td></tr>" % (_e(n["label"]), _e(n["text"])) for n in f["numbers"]))
            action = f.get("action") or {}
            if action.get("text"):
                body.append("<p>Suggestion: %s</p>" % _e(action["text"]))
            for key in ("sql", "config"):
                if action.get(key):
                    body.append('<pre class="sql-action">%s</pre>' % _e(action[key]))
            if f.get("caveat") or f.get("why"):
                body.append('<details class="why-details"><summary>Why and caveat</summary><p>%s</p>%s</details>' % (
                    _e(f.get("why")), "<p>Caveat: %s</p>" % _e(f["caveat"]) if f.get("caveat") else ""))
            items.append("".join(body) + "</li>")
        parts.append('<section aria-labelledby="findings"><h2 id="findings">Findings to look at '
                     '<span class="count">%d</span></h2><ul class="warning-list">%s</ul></section>'
                     % (len(flagged), "".join(items)))
    unknown = [f for f in report["findings"] if f["status"] == "unknown"]
    if unknown:
        parts.append('<section aria-labelledby="not-judged"><h2 id="not-judged">Not judged <span class="count">%d'
                     '</span></h2><ul class="warning-list">%s</ul></section>' % (len(unknown), "".join(
                         '<li class="warning finding">%s<h3>%s</h3><p>%s</p></li>' % (
                             _badge("unknown"), _e(f["title"]), _e(f["verdict"] or f["headline"])) for f in unknown)))
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (_badge(sec["status"]), _e(sec["title"]),
                                                                   _e(sec["summary"])) for sec in report["sections"])
    parts.append('<section aria-labelledby="sections"><h2 id="sections">Sections</h2><table class="report-table">'
                 '<thead><tr><th>Status</th><th>Section</th><th>Summary</th></tr></thead><tbody>%s</tbody></table>'
                 '</section>' % rows)
    a = report.get("attribution") or {}
    if a.get("available"):
        rows = "".join("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (
            _e((i.get("digest") or "")[:12]), _e(i["select_full_join"]), _e(i["select_range_check"]), _e(i["count"]),
            _e(_statement_text(i, 200))) for i in (a.get("full_joins") or {}).get("statements") or [])
        body = ('<h3>Joins without an index</h3><table class="report-table"><thead><tr><th>Digest</th>'
                '<th>Full joins</th><th>Range checks</th><th>Calls</th><th>Statement</th></tr></thead><tbody>%s'
                '</tbody></table>' % (rows or '<tr><td colspan="5">None recorded.</td></tr>'))
        for t in a.get("full_scans") or []:
            body += '<h3>Statements that name %s.%s <span class="muted">(%s)</span></h3>' % (
                _e(t["schema"]), _e(t["table"]), _e(t["label"]))
            state, text = _table_rows_state(t)
            if state != "rows":
                body += "<p>%s</p>" % _e(text)
                continue
            rows = "".join("<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (
                _e((i.get("digest") or "")[:12]), _e(i["no_index_used"]), _e(i["table_match"]["kind"]),
                _e(_statement_text(i, 200))) for i in t["statements"])
            body += ('<table class="report-table"><thead><tr><th>Digest</th><th>No-index executions</th><th>Match</th>'
                     '<th>Statement</th></tr></thead><tbody>%s</tbody></table>' % rows)
        body += '<details class="why-details"><summary>Caveats</summary><ul>%s</ul></details>' % "".join(
            "<li>%s</li>" % _e(c) for c in a.get("caveats") or [])
    else:
        body = ""
    body = "<ul>%s</ul>" % "".join("<li>%s</li>" % _e(line) for line in coverage_lines(a)) + body
    parts.append('<section aria-labelledby="attribution"><h2 id="attribution">Statements behind the numbers</h2>%s'
                 '</section>' % body)
    lines = "".join("<li>%s: %s</li>" % (_e(i["label"]), _e(i["reason"])) for i in report["checklist"]["not_checked"])
    lines += "".join("<li>%s %s: %s</li>" % (_e(f["label"]), _e(f["status"]), _e(f["error"]))
                     for f in h.get("failures") or [])
    parts.append('<section aria-labelledby="not-checked"><h2 id="not-checked">Not checked</h2><ul>%s</ul></section>'
                 % lines)
    parts.append('<section aria-labelledby="notes"><h2 id="notes">Notes</h2><ul>%s</ul></section>'
                 % "".join("<li>%s</li>" % _e(n) for n in report["notes"]))
    data = json.dumps(report, ensure_ascii=False, allow_nan=False)
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    title = "Full analysis: %s" % _server_line(report)
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>%s</title>\n<style>%s%s</style>\n"
            "<script type=\"application/json\" id=\"myflames-report\">%s</script>\n</head>\n<body>\n"
            "<header class=\"site-header\"><div class=\"site-header-main\"><h1>%s</h1></div>"
            "<div class=\"site-header-meta\">%s</div></header>\n"
            "<main id=\"main-content\" class=\"report\">%s</main>\n"
            "<footer class=\"site-footer\"><p>Made with myflames %s. Inspired by Brendan Gregg&rsquo;s FlameGraph and "
            "Tanel Poder&rsquo;s SQL Plan FlameGraphs.</p></footer>\n</body>\n</html>\n"
            % (_e(title), _CSS, _HTML_EXTRA, data, _e(title), _e(s["text"]), "\n".join(parts),
               _e(report["myflames_version"])))


FORMATS = {"text": format_text, "digest": format_digest, "markdown": format_markdown, "html": format_html}


def export(report, fmt, source=None):
    """*report* (a report-1.0 dict) as text, digest, markdown, html or json.
    *source* names the connection it was read from (normalize_source); it is
    added as ``source`` and shown in every format."""
    if not isinstance(report, dict) or report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Expected a report-1.0 report.")
    src = normalize_source(source if source is not None else report.get("source"))
    report = dict(report)
    report.pop("source", None)
    if src:
        report["source"] = src
    if fmt == "json":
        return json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    if fmt not in FORMATS:
        raise ValueError("Choose a format: json, %s." % ", ".join(FORMATS))
    # Server strings (statement text, object names, errors) can carry
    # terminal control characters; they print as visible \uXXXX escapes in
    # every text format, as on the CLI (connector.terminal_text).
    return terminal_text(FORMATS[fmt](report))


def taken_at_name(report, fmt):
    """A file name for an export: myflames-report-<date>.<ext>."""
    stamp = re.sub(r"[^0-9]", "", (report.get("taken_at") or now())[:19])
    return "myflames-report-%s.%s" % (stamp, {"markdown": "md", "html": "html", "json": "json", "text": "txt",
                                              "digest": "txt"}[fmt])
