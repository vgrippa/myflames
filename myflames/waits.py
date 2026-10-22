"""Wait events: where server threads spent their waiting time.

:func:`waits_report` returns the ``waits-1.0`` envelope shared by ``myflames
waits`` and the workspace's Server > Waits tab. It reads
``performance_schema.events_waits_summary_global_by_event_name`` twice,
*interval* seconds apart (2 to 60, default 10), and reports the events with
the most wait time over that window, the same events since the server
started, and which wait instrument classes are switched off.

Sampling (:func:`waits_report`) is separate from the arithmetic
(:func:`compute_waits`), which is pure. Nothing is written: no TRUNCATE (it
would also reset the per-table and per-index statistics Index health uses),
no ``setup_instruments`` or ``setup_consumers`` updates. The statements that
turn a class on are shown as text for the user to run.

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"; the fact sheet in the review notes has every citation):

* Timer columns are picoseconds on both engines (M/storage/perfschema/
  pfs_timer.cc:100-110, 171-183; Ma/storage/perfschema/pfs_timer.cc:38-44).
  They are cumulative since startup, so a delta of two snapshots is the
  window. A negative delta means a TRUNCATE, a restart or a wrap, never
  negative time.
* ``idle`` is a real row named ``idle`` (no ``wait/`` prefix), so
  ``EVENT_NAME LIKE 'wait/%'`` excludes it, as the sys views do.
* Defaults, both engines: ``wait/synch/*`` (mutex, rwlock, sxlock, prlock,
  cond) and ``wait/io/socket/*`` are off; ``wait/io/file/*``,
  ``wait/io/table/sql/handler`` and ``wait/lock/table/sql/handler`` are on
  and timed. ``wait/lock/metadata/sql/mdl`` is on in MySQL and off in MariaDB
  (M/storage/perfschema/pfs_instr_class.cc:327-354, 1419-1942;
  Ma/storage/perfschema/pfs_instr_class.cc:214-237, 1089-1515). So "no mutex
  waits" never means there was no mutex contention. A runtime UPDATE of
  setup_instruments re-applies to existing instances (M/storage/perfschema/
  table_setup_instruments.cc:503-514, pfs_instr.cc:2436-2468).
* ``wait/lock/table/sql/handler`` records THR_LOCK requests (M/mysys/
  thr_lock.cc:481) and each ``ha_external_lock`` call (M/sql/handler.cc:8152);
  InnoDB takes no THR_LOCK, so for it this is the per-statement external
  lock, LOCK TABLES waits included. InnoDB row lock waits have no wait event
  on either engine (M/storage/innobase/lock/lock0wait.cc:297,
  Ma/storage/innobase/lock/lock0lock.cc:2378-2382); they sit inside the
  table I/O time.
* ``innodb_temp_file`` is temporary tablespaces and index-build sort files on
  MySQL (M/storage/innobase/fil/fil0fil.cc:5572, ddl/ddl0ddl.cc:150) but only
  index-build merge files on MariaDB (Ma/storage/innobase/row/row0merge.cc:
  4354), where ibtmp1 is an ``innodb_data_file``.
* Summary tables only need the ``global_instrumentation`` consumer.
* Nested time: ``wait/io/table/sql/handler`` times the whole storage engine
  call, so file I/O and synch waits inside it are counted again under their
  own events (M/sql/handler.cc:155-191, M/storage/perfschema/pfs.cc:
  4185-4217). Classes are therefore never summed into a total and never
  shown as a share of one.
* Wait time is summed over all threads, background ones included, so it can
  exceed wall-clock time.
* InnoDB asynchronous I/O (read-ahead, page cleaner writes) is timed only for
  submitting the request, so its latency is understated
  (M/storage/innobase/include/os0file.ic:141-162;
  Ma/storage/innobase/os/os0file.cc:3291-3345).
* MAX_TIMER_WAIT is the longest single wait since startup, never per window.
"""
from .server_info import product_name
from .connector import ConnectorError
from .perf_common import (DEFAULT_WINDOW, Reader, _count_text, _duration_text, _number, _round, base_report,
                          check_limit, check_window, consumer_note, consumer_state, denied, elapsed, lost_note,
                          ms_text, now, preflight, ps_off_text, ps_time_ms, reason, restarted, sample_twice,
                          table_text)

SCHEMA_VERSION = "waits-1.0"
WAITS_TAG = "/*myflames:waits*/ "
DEFAULT_LIMIT = 20

_COVERAGE_SQL = ("SELECT SUBSTRING_INDEX(NAME, '/', 3) AS wait_class, SUM(ENABLED = 'YES'), "
                 "SUM(TIMED = 'YES'), COUNT(*) FROM performance_schema.setup_instruments "
                 "WHERE NAME LIKE 'wait/%' GROUP BY wait_class")
_SNAPSHOT_SQL = ("SELECT EVENT_NAME, COUNT_STAR, SUM_TIMER_WAIT, MAX_TIMER_WAIT "
                 "FROM performance_schema.events_waits_summary_global_by_event_name "
                 "WHERE EVENT_NAME LIKE 'wait/%' AND COUNT_STAR > 0")

#: Wait classes (the first three name parts) in display order, with a label.
CLASSES = (
    ("wait/io/file", "File I/O"),
    ("wait/io/table", "Table I/O"),
    ("wait/lock/table", "Table locks"),
    ("wait/lock/metadata", "Metadata locks"),
    ("wait/synch/mutex", "Mutexes"),
    ("wait/synch/rwlock", "Read/write locks"),
    ("wait/synch/sxlock", "Shared/exclusive locks"),
    ("wait/synch/prlock", "Priority read/write locks"),
    ("wait/synch/cond", "Condition waits"),
    ("wait/io/socket", "Socket I/O"),
)
CLASS_LABELS = dict(CLASSES)
CLASS_EXPLAIN = {
    "wait/io/file": "Reads, writes, syncs and opens of files: data files, redo and undo logs, binary logs, temporary files.",
    "wait/io/table": ("Row operations through the storage engine interface (fetch, insert, update, delete). "
                      "Each wait covers the whole engine call, so file I/O and internal lock waits inside it "
                      "are counted here and again under their own events. InnoDB row lock waits have no event "
                      "of their own and are inside this time too (Locks shows them)."),
    "wait/lock/table": ("Table lock requests: THR_LOCK table locks (MyISAM, MEMORY, LOCK TABLES) and each "
                        "storage engine external lock call. For InnoDB this is the per-statement table lock "
                        "call, which includes waits for LOCK TABLES; row lock waits are not here."),
    "wait/lock/metadata": ("Waits for metadata locks: DDL waiting for open transactions, or statements "
                           "waiting behind DDL, LOCK TABLES or FLUSH TABLES WITH READ LOCK."),
    "wait/synch/mutex": "Short-term internal locks that protect shared memory structures.",
    "wait/synch/rwlock": "Internal read/write locks.",
    "wait/synch/sxlock": "InnoDB's shared/exclusive internal locks, such as an index tree's lock.",
    "wait/synch/prlock": "Internal read/write locks that prefer readers.",
    "wait/synch/cond": "Threads waiting to be signalled by another thread (for example a group commit).",
    "wait/io/socket": "Network reads and writes on client and replication connections.",
}

#: Plain-language meaning of common events (exact names).
MEANINGS = {
    "wait/io/table/sql/handler": CLASS_EXPLAIN["wait/io/table"],
    "wait/lock/table/sql/handler": CLASS_EXPLAIN["wait/lock/table"],
    "wait/lock/metadata/sql/mdl": CLASS_EXPLAIN["wait/lock/metadata"],
    "wait/io/file/innodb/innodb_data_file": ("InnoDB tablespace files (.ibd, the system tablespace, undo "
                                             "tablespaces). Read-ahead and page cleaner writes are timed only "
                                             "for submitting the request, so their latency is understated."),
    "wait/io/file/innodb/innodb_log_file": ("The InnoDB redo log: writes and syncs. Commit latency depends on "
                                            "it when innodb_flush_log_at_trx_commit is 1."),
    "wait/io/file/innodb/innodb_dblwr_file": "The InnoDB doublewrite buffer files (MySQL).",
    "wait/io/file/sql/binlog": "Binary log writes and syncs (sync_binlog controls how often they sync).",
    "wait/io/file/sql/binlog_index": "The binary log index file.",
    "wait/io/file/sql/relaylog": "Relay log reads and writes on a replica.",
    "wait/io/file/sql/relaylog_index": "The relay log index file on a replica.",
    "wait/io/file/sql/io_cache": "Temporary files written through the server's I/O cache, such as filesort merge files.",
    "wait/io/file/sql/FRM": "Table definition (.frm) files (MariaDB).",
    "wait/io/file/myisam/dfile": "MyISAM data files (.MYD).",
    "wait/io/file/myisam/kfile": "MyISAM index files (.MYI).",
    "wait/io/file/aria/MAD": "Aria data files (.MAD), used by MariaDB's system tables and on-disk temporary tables.",
    "wait/io/file/aria/MAI": "Aria index files (.MAI).",
    "wait/io/socket/sql/client_connection": "Network I/O on client connections.",
}


#: Events whose meaning differs by engine.
ENGINE_MEANINGS = {
    ("mysql", "wait/io/file/innodb/innodb_temp_file"): (
        "InnoDB temporary tablespaces (internal temporary tables on disk) and the sort files InnoDB writes "
        "while building an index."),
    ("mariadb", "wait/io/file/innodb/innodb_temp_file"): (
        "The merge-sort files InnoDB writes while building an index (ALTER TABLE, CREATE INDEX). MariaDB's "
        "temporary tablespace (ibtmp1) is counted under innodb_data_file."),
}


def meaning(event, engine="mysql"):
    """The plain-language meaning of *event*, or its class's."""
    if (engine, event) in ENGINE_MEANINGS:
        return ENGINE_MEANINGS[(engine, event)]
    if event in MEANINGS:
        return MEANINGS[event]
    klass = event_class(event)
    if klass == "wait/io/file":
        return "A file read, write or sync (%s)." % "/".join(event.split("/")[3:])
    return CLASS_EXPLAIN.get(klass, "")


def event_class(event):
    return "/".join((event or "").split("/")[:3])


def enable_sql(klass):
    """The statements that turn a wait class on, as text for the user to run."""
    return {
        "runtime": ("UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', TIMED = 'YES' "
                    "WHERE NAME LIKE '%s/%%';" % klass),
        "config": "performance-schema-instrument = '%s/%%=ON'" % klass,
    }


ENABLE_CAVEAT = ("The UPDATE takes effect at once, for existing and new instances, and lasts until the server "
                 "restarts; the my.cnf line (under [mysqld]) keeps it after a restart. Waits already in "
                 "progress when you turn it on are missed, and file I/O on files that were already open "
                 "(such as InnoDB tablespaces) is recorded only after they are reopened. Synch instruments "
                 "add overhead on busy servers, so enable them for an investigation and turn them off "
                 "afterwards.")

# ---- sampling (talks to the server) ------------------------------------------


def _take(reader):
    def take(clock):
        uptime = reader.pairs("SHOW GLOBAL STATUS WHERE Variable_name = 'Uptime'").get("Uptime")
        rows = {}
        for row in reader.rows(_SNAPSHOT_SQL):
            if len(row) >= 4 and row[0]:
                rows[row[0]] = (_number(row[1]) or 0, _number(row[2]) or 0, _number(row[3]) or 0)
        return {"uptime": _number(uptime), "rows": rows, "t": clock(), "taken_at": now()}
    return take


def waits_report(conn, interval=DEFAULT_WINDOW, limit=DEFAULT_LIMIT, sleep=None, clock=None):
    """Return the ``waits-1.0`` report. Read-only; see the module docstring."""
    interval = check_window(interval)
    limit = check_limit(limit, 200)
    reader = Reader(conn, WAITS_TAG)
    notes = []
    try:
        server = preflight(reader)
        if not server.ps_on:
            return compute_waits(server, None, None, None, None, interval, limit, notes)
        coverage = reader.optional(_COVERAGE_SQL, "performance_schema.setup_instruments", notes)
        consumers = consumer_state(reader, notes)
        try:
            first, second = sample_twice(_take(reader), interval, sleep, clock)
        except ConnectorError as exc:
            if not denied(exc):
                raise
            notes.append("Could not read performance_schema.events_waits_summary_global_by_event_name: %s. "
                         "Grant SELECT on performance_schema to this user." % reason(exc))
            return compute_waits(server, None, None, coverage, consumers, interval, limit, notes,
                                 unavailable="denied")
    except ConnectorError as exc:
        raise ValueError("Could not read wait events: " + reason(exc))
    return compute_waits(server, first, second, coverage, consumers, interval, limit, notes)

# ---- arithmetic (pure) ----------------------------------------------------------


def _event_row(event, count, ps, per_s=None, max_ps=None, engine="mysql"):
    wait_ms = ps / 1e9
    klass = event_class(event)
    row = {
        "event": event, "class": klass, "class_label": CLASS_LABELS.get(klass, klass),
        "meaning": meaning(event, engine), "count": count, "wait_ms": _round(wait_ms, 3),
        "avg_ms": _round(wait_ms / count, 4) if count else None, "timed": ps > 0,
    }
    if per_s is not None:
        row["wait_ms_per_s"] = _round(per_s, 3)
    if max_ps is not None:
        row["max_ms"] = _round(max_ps / 1e9, 4)
    return row


def _classes(rows, order_by):
    """Per-class sums of *rows* (events of one class never nest in each other)."""
    out = {}
    for row in rows:
        entry = out.setdefault(row["class"], {"class": row["class"],
                                              "label": CLASS_LABELS.get(row["class"], row["class"]),
                                              "explain": CLASS_EXPLAIN.get(row["class"], ""),
                                              "events": 0, "count": 0, "wait_ms": 0.0})
        entry["events"] += 1
        entry["count"] += row["count"]
        entry["wait_ms"] += row["wait_ms"] or 0
        if order_by in row:
            entry.setdefault(order_by, 0.0)
            entry[order_by] += row[order_by] or 0
    result = sorted(out.values(), key=lambda c: (-c["wait_ms"], c["class"]))
    for entry in result:
        entry["wait_ms"] = _round(entry["wait_ms"], 3)
        if order_by in entry:
            entry[order_by] = _round(entry[order_by], 3)
    return result


def _coverage(rows):
    if rows is None:
        return None
    out = []
    order = [k for k, _ in CLASSES]
    for row in rows:
        if len(row) < 4 or not row[0] or row[0] == "idle":
            continue
        enabled, timed, total = _number(row[1]) or 0, _number(row[2]) or 0, _number(row[3]) or 0
        state = "on" if enabled == total and timed == total else ("off" if enabled == 0 else "partial")
        entry = {"class": row[0], "label": CLASS_LABELS.get(row[0], row[0]),
                 "explain": CLASS_EXPLAIN.get(row[0], ""), "enabled": enabled, "timed": timed,
                 "total": total, "state": state}
        if state != "on":
            entry["enable"] = enable_sql(row[0])
        out.append(entry)
    out.sort(key=lambda c: (order.index(c["class"]) if c["class"] in order else len(order), c["class"]))
    return out


def compute_waits(server, first, second, coverage_rows, consumers, interval=DEFAULT_WINDOW,
                  limit=DEFAULT_LIMIT, notes=None, unavailable=None):
    """The ``waits-1.0`` report from two snapshots. Pure.

    *first*/*second* are ``{uptime, rows: {event: (count, sum_ps, max_ps)},
    t, taken_at}``; *coverage_rows* the setup_instruments class counts.
    """
    notes = list(notes or [])
    report = base_report(SCHEMA_VERSION, server, (second or {}).get("taken_at"))
    report.update({"interval": interval, "elapsed_s": _round(elapsed(first, second), 3),
                   "started_at": (first or {}).get("taken_at"), "restarted": restarted(first, second),
                   "available": False, "unavailable": None, "coverage": _coverage(coverage_rows),
                   "enable_caveat": ENABLE_CAVEAT,
                   "window": None, "since_start": None, "notes": notes})
    if not server.ps_on:
        report["unavailable"] = "performance_schema_off"
        notes.insert(0, ps_off_text(server, "wait events"))
        return report
    if unavailable or not first or not second:
        report["unavailable"] = unavailable or "no_data"
        return report
    report["available"] = True
    span = report["elapsed_s"]
    a, b = first["rows"], second["rows"]
    went_down = sorted(e for e, (count, ps, _) in a.items()
                       if e not in b or b[e][0] < count or b[e][1] < ps)
    window = {"status": "ok", "events": [], "classes": [], "untimed": []}
    if report["restarted"]:
        window["status"] = "restarted"
        notes.append("The server restarted between the two snapshots, so there is no window this time. "
                     "Refresh to sample again.")
    elif went_down:
        window["status"] = "reset"
        notes.append("Wait counters went down between the snapshots (%s%s): someone truncated a "
                     "Performance Schema summary table, or a counter wrapped. The window is dropped; "
                     "refresh to sample again." % (", ".join(went_down[:3]),
                                                    " and %d more" % (len(went_down) - 3) if len(went_down) > 3 else ""))
    else:
        rows = []
        for event, (count, ps, _) in b.items():
            before = a.get(event, (0, 0, 0))
            d_count, d_ps = count - before[0], ps - before[1]
            if d_count <= 0:
                continue
            rows.append(_event_row(event, d_count, d_ps, per_s=(d_ps / 1e9) / span if span else None,
                                   engine=server.engine))
        timed = sorted((r for r in rows if r["timed"]), key=lambda r: (-r["wait_ms"], -r["count"], r["event"]))
        window["events"] = timed[:limit]
        window["classes"] = _classes(timed, "wait_ms_per_s")
        window["untimed"] = sorted(({"event": r["event"], "count": r["count"]} for r in rows if not r["timed"]),
                                   key=lambda r: -r["count"])
        window["more"] = max(0, len(timed) - limit)
    report["window"] = window
    total = sorted((_event_row(e, c, ps, max_ps=mx, engine=server.engine) for e, (c, ps, mx) in b.items() if ps > 0),
                   key=lambda r: (-r["wait_ms"], r["event"]))
    report["since_start"] = {"uptime_s": _number(second.get("uptime")), "events": total[:limit],
                             "classes": _classes(total, "_"), "more": max(0, len(total) - limit)}
    _notes(server, report, consumers, notes)
    return report


def _notes(server, report, consumers, notes):
    note = consumer_note(consumers)
    if note:
        notes.append(note)
    note = lost_note(server)
    if note:
        notes.append(note)
    coverage = report["coverage"] or []
    off = [c for c in coverage if c["state"] != "on"]
    if off:
        notes.append("Some wait classes are not instrumented (%s), so their waits are not in this report. "
                     "No mutex or rwlock waits here never means no contention." % ", ".join(
                         "%s %d of %d on" % (c["label"].lower(), c["enabled"], c["total"]) for c in off))
    if server.engine == "mariadb":
        notes.append("MariaDB turns the metadata lock instrument (wait/lock/metadata/sql/mdl) off by default; "
                     "MySQL turns it on.")
    window = report["window"] or {}
    if window.get("untimed"):
        notes.append("%d event(s) were counted but not timed, so they are not ranked: %s." % (len(window["untimed"]),
                                         ", ".join(u["event"] for u in window["untimed"][:5])))
    notes.append("Wait time is summed across all threads, background threads included, so it can exceed "
                 "the wall-clock window. Table I/O includes the file I/O and internal lock waits inside each "
                 "call, which are counted again under their own events, so the classes are never added up.")
    notes.append("InnoDB asynchronous I/O (read-ahead, page cleaner writes) is timed only for submitting the "
                 "request, so its latency is understated. Bytes are exact (see I/O).")
    if server.uptime is not None:
        notes.append("Since server start covers %s of uptime; the longest single wait (max) is since start too, "
                     "never per window." % _duration_text(server.uptime))

# ---- text output for the CLI -------------------------------------------------------


def format_text(report):
    head = "%s %s" % (product_name(report), report["server_version"] or "?")
    if report["uptime_s"] is not None:
        head += ", up %s" % _duration_text(report["uptime_s"])
    lines = [head]
    if not report["available"]:
        lines.append("Wait events are not available (see notes).")
        return "\n".join(lines) + "\n"
    window = report["window"]
    lines[0] += ", sampled over %s s" % _count_text(report["elapsed_s"])
    lines.append("")
    lines.append("TOP WAITS OVER THE WINDOW (aggregate across threads)")
    if window["status"] != "ok":
        lines.append("  No window: %s (see notes)." % ("the server restarted" if window["status"] == "restarted"
                                                      else "counters went down"))
    elif not window["events"]:
        lines.append("  No timed waits in the window.")
    else:
        rows = [[e["event"], _count_text(e["count"]), ms_text(e["wait_ms"]),
                 "%s ms/s" % _count_text(e.get("wait_ms_per_s")), ms_text(e["avg_ms"])]
                for e in window["events"]]
        lines.extend("  " + l for l in table_text(["EVENT", ">WAITS", ">TOTAL", ">PER SECOND", ">AVG"],
                                                  rows).split("\n"))
        lines.append("")
        lines.append("BY CLASS (each class on its own; never add them up)")
        rows = [[c["label"], c["class"], _count_text(c["count"]), ms_text(c["wait_ms"])] for c in window["classes"]]
        lines.extend("  " + l for l in table_text(["CLASS", "NAME", ">WAITS", ">TOTAL"], rows).split("\n"))
    since = report["since_start"]
    if since and since["events"]:
        lines.append("")
        lines.append("SINCE SERVER START")
        rows = [[e["event"], _count_text(e["count"]), ms_text(e["wait_ms"]), ms_text(e["avg_ms"]),
                 ms_text(e.get("max_ms"))] for e in since["events"][:10]]
        lines.extend("  " + l for l in table_text(["EVENT", ">WAITS", ">TOTAL", ">AVG", ">MAX"], rows).split("\n"))
    off = [c for c in report["coverage"] or [] if c["state"] != "on"]
    if off:
        lines.append("")
        lines.append("NOT INSTRUMENTED")
        for c in off:
            lines.append("  %s (%s): %d of %d on. Turn on with:" % (c["label"], c["class"], c["enabled"], c["total"]))
            lines.append("    " + c["enable"]["runtime"])
    return "\n".join(lines) + "\n"
