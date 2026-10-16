"""I/O hot spots: the tables and files with the most I/O over a short window.

:func:`io_report` returns the ``io-1.0`` envelope shared by ``myflames io``
and the workspace's Server > I/O tab. It takes two snapshots *interval*
seconds apart (2 to 60, default 10) of:

* ``SHOW GLOBAL STATUS`` InnoDB counters (bytes and operations on data files,
  fsyncs, redo, buffer pool disk reads). These work without Performance
  Schema.
* ``performance_schema.table_io_waits_summary_by_table`` (row operations and
  their time per table), limited to one schema when one is given, and
  ``file_summary_by_instance`` / ``file_summary_by_event_name`` (bytes,
  operations and time per file and per file kind).
* On MariaDB with Performance Schema off and ``userstat=ON``,
  ``information_schema.TABLE_STATISTICS`` (rows read and changed per table,
  no times).

It also reports the same tables and files since the server started, labeled
as such. The per-table read costs time proportional to the number of tables
and open table handles, so it runs only when the user asks (no automatic
refresh). The module is named io_stats so it never shadows the standard
library's io.

Verified against server source (mysql-server 9.7 "M/", mariadb-server 13.0
"Ma/"; the review notes have every citation):

* Table counts are handler row operations (fetch, insert, update, delete),
  not bytes; their time is handler time and includes the file I/O inside it.
* Temporary files and deleted files leave file_summary_by_instance when they
  are closed or deleted; their I/O stays in file_summary_by_event_name
  (M/storage/perfschema/pfs.cc:5815-5845, pfs_instr.cc:1283-1287), so both
  are shown.
* InnoDB asynchronous I/O is timed only for submitting the request
  (M/storage/innobase/include/os0file.ic:141-162;
  Ma/storage/innobase/os/os0file.cc:3291-3345): bytes are right, latency is
  understated.
* MariaDB has no Performance Schema instrument for the redo log file (no
  ``innodb_log_file`` key in storage/innobase at 10.11, 11.4 or 13.0), so its
  redo I/O appears only in ``Innodb_os_log_written``.
* File names map to tables by their last two path components relative to
  the data directory; ``#p#`` / ``#P#`` mark partitions and ``@XXXX`` encodes
  a character (``odd@0024name`` is ``odd$name``).
"""
import re
import time

from .connector import ConnectorError
from .perf_common import (DEFAULT_WINDOW, SYSTEM_SCHEMAS, Reader, _bytes_text, _count_text, _duration_text,
                          _number, _round, base_report, check_limit, check_schema, check_window, consumer_note,
                          consumer_state, denied, elapsed, literal, lost_note, ms_text, now, preflight, ps_off_text,
                          reason, restarted, sample_twice, table_text)
from .waits import ENGINE_MEANINGS

SCHEMA_VERSION = "io-1.0"
IO_TAG = "/*myflames:io*/ "
DEFAULT_LIMIT = 20
INSTANCE_LIMIT = 2000
COLLECTION_BUDGET_S = 30.0

STATUS_NAMES = ("Uptime", "Innodb_data_read", "Innodb_data_written", "Innodb_data_reads", "Innodb_data_writes",
                "Innodb_data_fsyncs", "Innodb_os_log_written", "Innodb_buffer_pool_reads",
                "Innodb_buffer_pool_read_ahead", "Innodb_buffer_pool_read_requests")
_TABLE_COLUMNS = ("COUNT_STAR", "SUM_TIMER_WAIT", "COUNT_FETCH", "SUM_TIMER_FETCH", "COUNT_INSERT",
                  "SUM_TIMER_INSERT", "COUNT_UPDATE", "SUM_TIMER_UPDATE", "COUNT_DELETE", "SUM_TIMER_DELETE")
_FILE_COLUMNS = ("COUNT_READ", "SUM_NUMBER_OF_BYTES_READ", "SUM_TIMER_READ", "COUNT_WRITE",
                 "SUM_NUMBER_OF_BYTES_WRITE", "SUM_TIMER_WRITE", "COUNT_MISC", "SUM_TIMER_MISC", "SUM_TIMER_WAIT")

FILE_KINDS = {
    "table": "Table data", "redo": "Redo log", "undo": "Undo tablespace", "system": "System tablespace",
    "dictionary": "Data dictionary", "temporary": "Temporary tablespace", "sortfile": "Index build sort file",
    "doublewrite": "Doublewrite buffer",
    "binlog": "Binary log", "relaylog": "Relay log", "other": "Other",
}
FILE_EVENT_MEANINGS = {
    "wait/io/file/innodb/innodb_data_file": "InnoDB tablespaces: tables, the system tablespace and undo.",
    "wait/io/file/innodb/innodb_log_file": "The InnoDB redo log (MySQL).",
    "wait/io/file/innodb/innodb_dblwr_file": "The InnoDB doublewrite buffer (MySQL).",
    "wait/io/file/sql/binlog": "Binary logs.",
    "wait/io/file/sql/relaylog": "Relay logs on a replica.",
    "wait/io/file/sql/io_cache": "Server temporary files, such as filesort merge files.",
    "wait/io/file/aria/MAD": "Aria data files (MariaDB system and on-disk temporary tables).",
    "wait/io/file/aria/MAI": "Aria index files.",
    "wait/io/file/myisam/dfile": "MyISAM data files.",
    "wait/io/file/myisam/kfile": "MyISAM index files.",
}
_TABLE_EXTENSIONS = (".ibd", ".myd", ".myi", ".mad", ".mai", ".csv", ".frm")
#: Whether the instruments behind the per-table and per-file figures are on.
_COVERAGE_SQL = ("SELECT SUM(NAME = 'wait/io/table/sql/handler' AND ENABLED = 'YES'), "
                 "SUM(NAME LIKE 'wait/io/file/%' AND ENABLED = 'YES'), SUM(NAME LIKE 'wait/io/file/%') "
                 "FROM performance_schema.setup_instruments "
                 "WHERE NAME = 'wait/io/table/sql/handler' OR NAME LIKE 'wait/io/file/%'")


def _decode(name):
    return re.sub(r"@([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)


def classify_file(path, event, datadir=""):
    """{kind, label, schema, table, partition} for a FILE_NAME."""
    text = (path or "").replace("\\", "/")
    root = (datadir or "").replace("\\", "/")
    relative = text[len(root):] if root and text.startswith(root) else text
    while relative.startswith("./"):
        relative = relative[2:]
    parts = [p for p in relative.split("/") if p not in ("", ".")]
    name = parts[-1] if parts else text
    lower = name.lower()
    out = {"kind": "other", "schema": None, "table": None, "partition": None}
    if event.endswith("/innodb_log_file") or "#innodb_redo" in text or re.match(r"ib_logfile\d+$", lower):
        out["kind"] = "redo"
    elif re.match(r"undo_?\d+$", lower) or lower.endswith(".ibu"):
        out["kind"] = "undo"
    elif re.match(r"ibdata\d+$", lower):
        out["kind"] = "system"
    elif lower.endswith(".ibt") or re.match(r"ibtmp\d+$", lower):
        out["kind"] = "temporary"
    elif event.endswith("/innodb_temp_file"):
        out["kind"] = "sortfile"
    elif lower.endswith(".dblwr") or event.endswith("/innodb_dblwr_file"):
        out["kind"] = "doublewrite"
    elif event.endswith("/sql/binlog") or event.endswith("/sql/binlog_index"):
        out["kind"] = "binlog"
    elif event.endswith("/sql/relaylog") or event.endswith("/sql/relaylog_index"):
        out["kind"] = "relaylog"
    elif lower == "mysql.ibd" and len(parts) == 1:
        out["kind"] = "dictionary"
    elif len(parts) >= 2 and lower.endswith(_TABLE_EXTENSIONS) and not lower.startswith("#sql"):
        stem = name.rsplit(".", 1)[0]
        table, partition = stem, ""
        match = re.match(r"^(.*?)#[pP]#(.*)$", stem)
        if match:
            table, partition = match.group(1), _decode(re.sub(r"#[sS][pP]#", " / ", match.group(2)))
        out.update({"kind": "table", "schema": _decode(parts[-2]), "table": _decode(table),
                    "partition": partition or None})
    out["label"] = FILE_KINDS[out["kind"]]
    return out

# ---- sampling (talks to the server) ------------------------------------------


def _scope(schema):
    if schema:
        return "OBJECT_SCHEMA = " + literal(schema)
    return "OBJECT_SCHEMA NOT IN (%s)" % ", ".join("'%s'" % s for s in SYSTEM_SCHEMAS)


def _file_scope(schema):
    if not schema:
        return ""
    # Compare the immediate directory component, including Windows paths.
    # SQL hex literals avoid LIKE wildcard and backslash escaping problems.
    encoded = "".join(c if c.isascii() and (c.isalnum() or c == "_") else "@%04x" % ord(c) for c in schema)
    directory = ("CASE WHEN LOCATE(CHAR(92), FILE_NAME) > 0 THEN "
                 "SUBSTRING_INDEX(SUBSTRING_INDEX(FILE_NAME, CHAR(92), -2), CHAR(92), 1) ELSE "
                 "SUBSTRING_INDEX(SUBSTRING_INDEX(FILE_NAME, '/', -2), '/', 1) END")
    return " AND %s = %s" % (directory, literal(encoded))


def _instance_queries(schema):
    return {
        "tables": ("SELECT OBJECT_SCHEMA, OBJECT_NAME, %s FROM performance_schema.table_io_waits_summary_by_table "
                   "WHERE OBJECT_TYPE = 'TABLE' AND COUNT_STAR > 0 AND %s" % (", ".join(_TABLE_COLUMNS), _scope(schema))),
        "files": ("SELECT FILE_NAME, EVENT_NAME, %s FROM performance_schema.file_summary_by_instance "
                  "WHERE COUNT_STAR > 0%s" % (", ".join(_FILE_COLUMNS), _file_scope(schema))),
        "classes": ("SELECT EVENT_NAME, %s FROM performance_schema.file_summary_by_event_name "
                    "WHERE COUNT_STAR > 0" % ", ".join(_FILE_COLUMNS)),
        "userstat": ("SELECT TABLE_SCHEMA, TABLE_NAME, ROWS_READ, ROWS_CHANGED FROM information_schema.TABLE_STATISTICS "
                     "WHERE " + (("TABLE_SCHEMA = " + literal(schema)) if schema else
                     "TABLE_SCHEMA NOT IN (%s)" % ", ".join("'%s'" % s for s in SYSTEM_SCHEMAS)))
    }


def _take(reader, server, schema, state):
    status_sql = ("SHOW GLOBAL STATUS WHERE Variable_name IN (%s)"
                  % ", ".join("'%s'" % n for n in STATUS_NAMES))
    queries = _instance_queries(schema)

    def collect(kind):
        if kind in state["omitted"]:
            return []
        remaining = state["deadline"] - time.monotonic()
        if remaining <= 0:
            state["omitted"].add(kind)
            return []
        sql = queries[kind]
        if not state["all_instances"]:
            # A sentinel detects growth since preflight. Never diff separately
            # limited lists: overflow drops this family from BOTH snapshots.
            sql += " LIMIT %d" % (INSTANCE_LIMIT + 1)
        try:
            rows = reader.rows(sql, timeout=remaining)
        except ConnectorError as exc:
            if "timed out" not in str(exc).lower():
                raise
            state["omitted"].add(kind)
            return []
        if not state["all_instances"] and len(rows) > INSTANCE_LIMIT:
            state["omitted"].add(kind)
            return []
        return rows

    def numbers(row, start):
        return tuple(_number(v) or 0 for v in row[start:])

    def take(clock):
        snap = {"status": reader.pairs(status_sql)}
        snap["uptime"] = _number(snap["status"].get("Uptime"))
        if state["source"] == "performance_schema":
            snap["tables"] = {(r[0], r[1]): numbers(r, 2) for r in collect("tables") if len(r) >= 12}
            snap["files"] = {(r[0], r[1]): numbers(r, 2) for r in collect("files") if len(r) >= 11}
            snap["classes"] = {r[0]: numbers(r, 1) for r in collect("classes") if len(r) >= 10}
        elif state["source"] == "userstat":
            snap["tables"] = {(r[0], r[1]): numbers(r, 2) for r in collect("userstat") if len(r) >= 4}
        snap["t"] = clock()
        snap["taken_at"] = now()
        return snap
    return take


def io_report(conn, interval=DEFAULT_WINDOW, schema=None, limit=DEFAULT_LIMIT, sleep=None, clock=None, all_instances=False):
    """Return io-1.0, with instance-count gates and one collection deadline.

    all_instances explicitly opts out of the INSTANCE_LIMIT count gate and
    row sentinels. Collection still has a COLLECTION_BUDGET_S deadline plus
    the requested sampling interval. An incomplete family is omitted from
    both snapshots, never diffed against a separately truncated list.
    """
    if not isinstance(all_instances, bool):
        raise ValueError("Pass all_instances as true or false.")
    interval = check_window(interval)
    schema = check_schema(schema)
    limit = check_limit(limit, 200)
    reader = Reader(conn, IO_TAG)
    notes = []
    try:
        server = preflight(reader, ("datadir",))
        state = {"source": "status", "coverage": None, "consumers": None, "all_instances": all_instances,
                 "counts": {}, "omitted": set(), "deadline": time.monotonic() + COLLECTION_BUDGET_S + interval}
        if server.ps_on:
            state["source"] = "performance_schema"
            state["coverage"] = reader.optional(_COVERAGE_SQL, "performance_schema.setup_instruments", notes)
            state["consumers"] = consumer_state(reader, notes)
        elif server.engine == "mariadb" and server.userstat:
            state["source"] = "userstat"
        queries = _instance_queries(schema)
        families = ("tables", "files") if state["source"] == "performance_schema" else (
            ("userstat",) if state["source"] == "userstat" else ())
        if not all_instances:
            for kind in families:
                count_sql = "SELECT COUNT(*) FROM " + queries[kind].split(" FROM ", 1)[1]
                remaining = state["deadline"] - time.monotonic()
                if remaining <= 0:
                    state["omitted"].add(kind)
                    continue
                try:
                    rows = reader.rows(count_sql, timeout=remaining)
                except ConnectorError as exc:
                    if denied(exc):
                        notes.append("Could not read per-table and per-file I/O: %s. Showing server-wide counters."
                                     % reason(exc))
                        state["source"] = "status"
                        break
                    if "timed out" not in str(exc).lower():
                        raise
                    state["omitted"].add(kind)
                    continue
                count = int(_number(rows[0][0]) or 0) if rows and rows[0] else 0
                state["counts"][kind] = count
                if count > INSTANCE_LIMIT:
                    state["omitted"].add(kind)
        try:
            first, second = sample_twice(_take(reader, server, schema, state), interval, sleep, clock)
        except ConnectorError as exc:
            if state["source"] == "status" or not denied(exc):
                raise
            notes.append("Could not read per-table and per-file I/O: %s. Grant SELECT on performance_schema "
                         "(or, for userstat, on the tables) to this user; server-wide counters are shown."
                         % reason(exc))
            state["source"] = "status"
            first, second = sample_twice(_take(reader, server, schema, state), interval, sleep, clock)
    except ConnectorError as exc:
        raise ValueError("Could not read I/O statistics: " + reason(exc))
    for kind in state["omitted"]:
        family = "tables" if kind == "userstat" else kind
        first.pop(family, None)
        second.pop(family, None)
    if state["omitted"]:
        notes.append("I/O instance count or collection budget exceeded for %s; both snapshots omit these "
                     "families. Limit: %d instances, collection budget: %g seconds plus the sampling interval. "
                     "Run myflames io --all-instances (all_instances in the API) for a full instance scan, still within "
                     "the budget." %
                     (", ".join(sorted(state["omitted"])), INSTANCE_LIMIT, COLLECTION_BUDGET_S))
    report = compute_io(server, first, second, state["source"], interval, schema, limit, notes,
                        coverage=state["coverage"], consumers=state["consumers"])
    report["collection"] = {"instance_limit": INSTANCE_LIMIT, "budget_s": COLLECTION_BUDGET_S,
                            "all_instances": all_instances, "counts": state["counts"],
                            "omitted": sorted(state["omitted"]), "complete": not state["omitted"]}
    return report

# ---- arithmetic (pure) ----------------------------------------------------------


def _delta_map(a, b, allow_new_instance=False):
    """({key: delta tuple}, [keys that went down])."""
    out, down = {}, []
    for key, after in b.items():
        before = a.get(key)
        if before is None:
            out[key] = after
            continue
        diff = tuple(x - y for x, y in zip(after, before))
        if any(d < 0 for d in diff):
            if allow_new_instance:
                out[key] = after
            else:
                down.append(key)
            continue
        out[key] = diff
    for key in a:
        if key not in b and not allow_new_instance:
            down.append(key)
    return out, down


def _table_row(key, v, span=None):
    count, wait, fetch, fetch_t, ins, ins_t, upd, upd_t, dele, del_t = v
    row = {"schema": key[0], "table": key[1], "ops": count, "total_ms": _round(wait / 1e9, 3),
           "read_rows": fetch, "read_ms": _round(fetch_t / 1e9, 3), "write_rows": ins + upd + dele,
           "write_ms": _round((ins_t + upd_t + del_t) / 1e9, 3), "inserts": ins, "updates": upd, "deletes": dele}
    if span:
        row["total_ms_per_s"] = _round(wait / 1e9 / span, 3)
    return row


def _userstat_row(key, v):
    return {"schema": key[0], "table": key[1], "ops": None, "total_ms": None, "read_rows": v[0],
            "read_ms": None, "write_rows": v[1], "write_ms": None, "inserts": None, "updates": None,
            "deletes": None}


def _file_numbers(v):
    reads, rbytes, rtime, writes, wbytes, wtime, misc, mtime, total = v
    return {"read_ops": reads, "read_bytes": rbytes, "read_ms": _round(rtime / 1e9, 3), "write_ops": writes,
            "write_bytes": wbytes, "write_ms": _round(wtime / 1e9, 3), "misc_ops": misc,
            "misc_ms": _round(mtime / 1e9, 3), "total_ms": _round(total / 1e9, 3)}


def _file_row(key, v, datadir):
    row = {"file": key[0], "event": key[1]}
    row.update(classify_file(key[0], key[1], datadir))
    row.update(_file_numbers(v))
    return row


def _class_row(event, v, engine="mysql"):
    row = {"event": event, "meaning": ENGINE_MEANINGS.get((engine, event)) or FILE_EVENT_MEANINGS.get(event, "")}
    row.update(_file_numbers(v))
    return row


def _top(rows, key, limit):
    ordered = sorted(rows, key=lambda r: (-(r.get(key) or 0), str(r.get("file") or r.get("event") or ""),
                                          str(r.get("schema") or ""), str(r.get("table") or "")))
    return ordered[:limit], max(0, len(ordered) - limit)


def _global(server, first, second, span):
    """Server-wide InnoDB I/O rates from the status deltas."""
    a, b = first["status"], second["status"]
    mysql = server.engine == "mysql"

    def delta(*names):
        total = 0
        for name in names:
            x, y = _number(a.get(name)), _number(b.get(name))
            if x is None or y is None or y < x:
                return None
            total += y - x
        return total
    metrics = []
    for key, label, names, unit, explain in (
            ("data_read", "Data read", ("Innodb_data_read",), "B/s",
             "Bytes InnoDB read from its data files (Innodb_data_read)."),
            ("data_written", "Data written", ("Innodb_data_written",), "B/s",
             "Bytes InnoDB wrote (Innodb_data_written)." + (
                 " On MySQL this includes redo log writes." if mysql else
                 " On MariaDB redo log writes are not included; see Redo written.")),
            ("data_reads", "Read operations", ("Innodb_data_reads",), "/s",
             "Read operations on InnoDB data files (Innodb_data_reads); one can read several pages."),
            ("data_writes", "Write operations", ("Innodb_data_writes",), "/s",
             "Write operations InnoDB issued (Innodb_data_writes)."),
            ("data_fsyncs", "Fsyncs", ("Innodb_data_fsyncs",), "/s",
             "fsync() calls InnoDB made (Innodb_data_fsyncs)."),
            ("redo_written", "Redo written", ("Innodb_os_log_written",), "B/s",
             "Redo log bytes (Innodb_os_log_written)." + (
                 " MySQL re-counts rewrites of partly filled blocks, so it overstates a little." if mysql else "")),
            ("pages_read", "Pages read from disk",
             ("Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_ahead") if mysql else ("Innodb_buffer_pool_reads",),
             "pages/s", "Buffer pool pages read from disk" + (
                 " (Innodb_buffer_pool_reads + Innodb_buffer_pool_read_ahead)." if mysql else
                 " (Innodb_buffer_pool_reads, read-ahead included on MariaDB)."))):
        d = delta(*names)
        metrics.append({"key": key, "label": label, "unit": unit, "explain": explain, "sources": list(names),
                        "delta": d, "rate": _round(d / span, 2) if d is not None and span else None})
    return metrics


def compute_io(server, first, second, source, interval=DEFAULT_WINDOW, schema=None, limit=DEFAULT_LIMIT, notes=None,
               coverage=None, consumers=None):
    """The ``io-1.0`` report from two snapshots. Pure.

    *coverage* is the setup_instruments row (table handler instrument on,
    file instruments on, file instruments in total); *consumers* the
    consumer states. Either may be None when it could not be read.
    """
    notes = list(notes or [])
    report = base_report(SCHEMA_VERSION, server, second.get("taken_at"))
    span = elapsed(first, second)
    datadir = server.var("datadir") or ""
    report.update({"interval": interval, "elapsed_s": _round(span, 3), "started_at": first.get("taken_at"),
                   "restarted": restarted(first, second), "scope": {"schema": schema},
                   "source": source, "global": [], "window": None, "since_start": None, "notes": notes})
    if report["restarted"]:
        notes.append("The server restarted between the two snapshots, so there is no window this time. "
                     "Refresh to sample again.")
    else:
        report["global"] = _global(server, first, second, span)
    window = {"status": "restarted" if report["restarted"] else "ok", "tables": [], "files": [],
              "file_classes": [], "more": {}}
    if source in ("performance_schema", "userstat") and not report["restarted"]:
        tables, down = _delta_map(first.get("tables", {}), second.get("tables", {}))
        if source == "performance_schema":
            rows = [_table_row(k, v, span) for k, v in tables.items() if v[0] > 0]
            window["tables"], window["more"]["tables"] = _top(rows, "total_ms", limit)
        else:
            rows = [_userstat_row(k, v) for k, v in tables.items() if v[0] or v[1]]
            window["tables"], window["more"]["tables"] = _top(rows, "read_rows", limit)
        if down:
            notes.append("Counters went down for %s between the snapshots (a DDL, a TRUNCATE of the summary table, "
                         "or the table was dropped), so %s left out of the window."
                         % (", ".join("%s.%s" % k for k in sorted(down)[:5]),
                            "it is" if len(down) == 1 else "they are"))
        if source == "performance_schema":
            files, _ = _delta_map(first.get("files", {}), second.get("files", {}), allow_new_instance=True)
            rows = [_file_row(k, v, datadir) for k, v in files.items() if any(v[:8])]
            window["files"], window["more"]["files"] = _top(rows, "total_ms", limit)
            classes, down = _delta_map(first.get("classes", {}), second.get("classes", {}))
            if down:
                window["status"] = "reset"
                notes.append("File I/O totals went down between the snapshots (someone truncated a Performance "
                             "Schema file summary table), so file kinds are left out of the window.")
            else:
                rows = [_class_row(k, v, server.engine) for k, v in classes.items() if any(v[:8])]
                window["file_classes"], _ = _top(rows, "total_ms", limit)
    report["window"] = window
    since = {"uptime_s": second.get("uptime"), "tables": [], "files": [], "file_classes": [], "more": {}}
    if source == "performance_schema":
        rows = [_table_row(k, v) for k, v in second.get("tables", {}).items()]
        since["tables"], since["more"]["tables"] = _top(rows, "total_ms", limit)
        rows = [_file_row(k, v, datadir) for k, v in second.get("files", {}).items()]
        since["files"], since["more"]["files"] = _top(rows, "total_ms", limit)
        rows = [_class_row(k, v, server.engine) for k, v in second.get("classes", {}).items()]
        since["file_classes"], _ = _top(rows, "total_ms", limit)
    elif source == "userstat":
        rows = [_userstat_row(k, v) for k, v in second.get("tables", {}).items()]
        since["tables"], since["more"]["tables"] = _top(rows, "read_rows", limit)
    report["since_start"] = since
    _coverage_notes(coverage, consumers, notes)
    _notes(server, report, source, schema, notes)
    return report


def _coverage_notes(coverage, consumers, notes):
    note = consumer_note(consumers)
    if note:
        notes.append(note)
    row = coverage[0] if coverage else None
    if not row or len(row) < 3:
        return
    table_on, file_on, file_total = (_number(v) or 0 for v in row[:3])
    if not table_on:
        notes.append("The wait/io/table/sql/handler instrument is off, so table I/O is not counted. To turn it "
                     "on until restart: UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', "
                     "TIMED = 'YES' WHERE NAME = 'wait/io/table/sql/handler';")
    if file_total and file_on < file_total:
        notes.append("Only %d of %d file I/O instruments are on, so some files are missing. To turn them on "
                     "until restart: UPDATE performance_schema.setup_instruments SET ENABLED = 'YES', "
                     "TIMED = 'YES' WHERE NAME LIKE 'wait/io/file/%%';" % (file_on, file_total))


def _notes(server, report, source, schema, notes):
    if source == "status":
        if not server.ps_on:
            text = ps_off_text(server, "per-table and per-file I/O")
            if server.engine == "mariadb":
                text += (" Or turn on user statistics with SET GLOBAL userstat = ON (runtime, small overhead "
                         "on every statement) for rows read and changed per table.")
            notes.insert(0, text)
        notes.append("Showing server-wide InnoDB counters only.")
    elif source == "userstat":
        notes.insert(0, "Performance Schema is off, so tables come from MariaDB user statistics (userstat=ON): "
                        "rows read and changed, with no times and no per-file view.")
    else:
        notes.append("Table figures are row operations through the storage engine (rows fetched, inserted, "
                     "updated, deleted) and their time, which includes the file I/O inside each call. They are "
                     "not bytes; the files show bytes.")
        notes.append("Temporary and deleted files leave the per-file list when closed; their I/O stays in the "
                     "file kinds, so check those for sort and temporary table spills.")
        notes.append("InnoDB asynchronous I/O (read-ahead, page cleaner writes) is timed only for submitting the "
                     "request: bytes are right, latency is understated.")
        if server.engine == "mariadb":
            notes.append("MariaDB does not instrument the redo log file in Performance Schema; its redo I/O is in "
                         "Redo written above.")
        note = lost_note(server)
        if note:
            notes.append(note)
        if schema:
            notes.append("Tables are limited to schema %s; files use that schema directory; global counters and file classes are server-wide." % schema)
    if report["uptime_s"] is not None and source != "status":
        notes.append("Since server start covers %s of uptime." % _duration_text(report["uptime_s"]))

# ---- text output for the CLI -------------------------------------------------------


def _rate_text(metric):
    if metric["rate"] is None:
        return "—"
    if metric["unit"] == "B/s":
        return _bytes_text(metric["rate"]) + "/s"
    if metric["unit"] == "pages/s":
        return "%s pages/s" % _count_text(metric["rate"])
    return "%s/s" % _count_text(metric["rate"])


def format_text(report):
    head = "%s %s" % ("MariaDB" if report["engine"] == "mariadb" else "MySQL", report["server_version"] or "?")
    if report["uptime_s"] is not None:
        head += ", up %s" % _duration_text(report["uptime_s"])
    head += ", sampled over %s s" % (_count_text(report["elapsed_s"]) if report["elapsed_s"] else "?")
    if report["scope"]["schema"]:
        head += ", schema %s" % report["scope"]["schema"]
    lines = [head, "", "SERVER-WIDE INNODB I/O"]
    for m in report["global"]:
        lines.append("  %-22s %s" % (m["label"], _rate_text(m)))
    window = report["window"] or {}
    if window.get("tables"):
        lines += ["", "TABLES OVER THE WINDOW"]
        rows = [["%s.%s" % (t["schema"], t["table"]), _count_text(t["read_rows"]), ms_text(t["read_ms"]),
                 _count_text(t["write_rows"]), ms_text(t["write_ms"]), ms_text(t["total_ms"])]
                for t in window["tables"]]
        lines.extend("  " + l for l in table_text(
            ["TABLE", ">ROWS READ", ">READ TIME", ">ROWS WRITTEN", ">WRITE TIME", ">TOTAL"], rows).split("\n"))
    if window.get("files"):
        lines += ["", "FILES OVER THE WINDOW"]
        rows = [[_file_label(f), f["label"], _bytes_text(f["read_bytes"]), _bytes_text(f["write_bytes"]),
                 ms_text(f["read_ms"]), ms_text(f["write_ms"]), ms_text(f["total_ms"])] for f in window["files"]]
        lines.extend("  " + l for l in table_text(
            ["FILE", "KIND", ">READ", ">WRITTEN", ">READ TIME", ">WRITE TIME", ">TOTAL"], rows).split("\n"))
    if window.get("file_classes"):
        lines += ["", "FILE KINDS OVER THE WINDOW (temporary files included)"]
        rows = [[c["event"], _bytes_text(c["read_bytes"]), _bytes_text(c["write_bytes"]), ms_text(c["total_ms"])]
                for c in window["file_classes"]]
        lines.extend("  " + l for l in table_text(["EVENT", ">READ", ">WRITTEN", ">TOTAL"], rows).split("\n"))
    since = report["since_start"] or {}
    if since.get("tables"):
        lines += ["", "TABLES SINCE SERVER START"]
        rows = [["%s.%s" % (t["schema"], t["table"]), _count_text(t["read_rows"]), _count_text(t["write_rows"]),
                 ms_text(t["total_ms"])] for t in since["tables"][:10]]
        lines.extend("  " + l for l in table_text(["TABLE", ">ROWS READ", ">ROWS WRITTEN", ">TOTAL"],
                                                  rows).split("\n"))
    return "\n".join(lines) + "\n"


def _file_label(f):
    if f["kind"] == "table":
        return "%s.%s%s" % (f["schema"], f["table"], " (%s)" % f["partition"] if f["partition"] else "")
    return f["file"].replace("\\", "/").rsplit("/", 1)[-1]
