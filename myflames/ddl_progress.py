"""Read-only DDL progress, shared by sessions and the editor's small poll.

The supplied source audit covers InnoDB's whole-ALTER work counters and
copy-to-tmp's estimated rows. MariaDB I_S PROGRESS is *per stage*, STAGE is
one-based (mariadb-11.4.9 sql/sql_show.cc:3413-3418); combine the stages before
estimating time. Never substitute the P_S stage timer for processlist TIME.
No writes, including instrumentation changes, belong in this module.
"""
from .connector import ConnectorError
from .perf_common import Reader, Server, _number, _round, base_report, reason, show_variables

TAG = "/*myflames:ddl-progress*/ "
MAX_IDS = 200
READ_CAP = 10000
INFO_CAP = 16384
CONSUMERS = ("global_instrumentation", "thread_instrumentation", "events_stages_current")
STAGES = {
    "stage/sql/copy to tmp table": "Copying rows",
    "stage/innodb/alter table (read PK and internal sort)": "Reading the primary key and sorting",
    "stage/innodb/alter table (merge sort)": "Merging sorted data",
    "stage/innodb/alter table (insert)": "Inserting index entries",
    "stage/innodb/alter table (log apply index)": "Applying index changes",
    "stage/innodb/alter table (log apply table)": "Applying table changes",
    "stage/innodb/alter table (flush)": "Flushing data",
    "stage/innodb/alter table (end)": "Finishing the ALTER",
}


def check_ids(ids):
    """Reject unbounded input and SQL fragments before opening a connection."""
    if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_IDS or any(
            isinstance(i, bool) or not isinstance(i, int) or not 1 <= i <= 2 ** 53 - 1 for i in ids):
        raise ValueError("Choose 1 to 200 connection ids as positive safe integers.")
    return list(dict.fromkeys(ids))


def _names(values):
    # Names are internal constants, never request data.
    return ", ".join("'%s'" % value for value in values)


def progress_sql(engine, ids=None, ps_on=True):
    """One bounded data read, including tracking diagnostics and native fallback.

    UNION keeps the configuration rows even when no stage is active. MariaDB's
    native rows are in this same read, so missing P_S stages need no second poll.
    Only interpolate checked numeric ids and internal stage/consumer constants.
    """
    parts = []
    if ps_on:
        stages = [s for s in STAGES if engine != "mariadb" or not s.endswith("(flush)")]
        parts += [
            "SELECT 'consumer', NULL, NAME, ENABLED, NULL, NULL, NULL, NULL, NULL "
            "FROM performance_schema.setup_consumers WHERE NAME IN (%s)" % _names(CONSUMERS),
            "SELECT 'instrument', NULL, NAME, ENABLED, TIMED, NULL, NULL, NULL, NULL "
            "FROM performance_schema.setup_instruments WHERE NAME IN (%s)" % _names(stages),
            "SELECT 'stage', t.PROCESSLIST_ID, s.EVENT_NAME, s.WORK_COMPLETED, s.WORK_ESTIMATED, "
            "t.PROCESSLIST_TIME, LEFT(t.PROCESSLIST_INFO, %d), NULL, NULL "
            "FROM performance_schema.events_stages_current s JOIN performance_schema.threads t "
            "ON t.THREAD_ID = s.THREAD_ID WHERE s.END_EVENT_ID IS NULL "
            "AND s.EVENT_NAME IN (%s) AND t.PROCESSLIST_ID IS NOT NULL "
            "AND t.PROCESSLIST_COMMAND <> 'Sleep'%s" % (
                INFO_CAP, _names(stages), " AND t.PROCESSLIST_ID IN (%s)" % ", ".join(map(str, ids)) if ids else ""),
        ]
    if engine == "mariadb":
        parts.append("SELECT 'processlist', ID, STATE, STAGE, MAX_STAGE, TIME, LEFT(INFO, %d), PROGRESS, COMMAND "
                     "FROM information_schema.PROCESSLIST WHERE MAX_STAGE > 0 AND COMMAND <> 'Sleep'%s" % (
                         INFO_CAP, " AND ID IN (%s)" % ", ".join(map(str, ids)) if ids else ""))
    return " UNION ALL ".join(parts) + " LIMIT %d" % (READ_CAP + len(STAGES) + len(CONSUMERS)) if parts else None


def _kind(sql):
    from .dbops import classify
    try:
        statements = classify(sql or "")["statements"]
        verb = statements[0]["verb"].split()[0].upper() if statements and statements[0]["verb"] else ""
    except ValueError:
        verb = ""
    return verb if verb in ("ALTER", "CREATE", "OPTIMIZE", "REPAIR", "LOAD") else "DDL"


def _counter(value):
    # Preserve exact work counts above JavaScript's safe integer range.
    return str(value) if value is not None and value > 2 ** 53 - 1 else value


def _item(cid, stage, completed, estimated, elapsed, sql, source, pct=None):
    completed, estimated, elapsed = (_number(v) for v in (completed, estimated, elapsed))
    completed = completed if completed is not None and completed >= 0 else None
    estimated = estimated if estimated is not None and estimated > 0 else None
    elapsed = elapsed if elapsed is not None and elapsed >= 0 else None
    # The server never lets its estimate fall below the work done: InnoDB
    # sets max(formula, completed) (Alter_stage, storage/innobase), and with
    # stale table statistics the estimate simply follows the work. COPY
    # estimates from stats.records and can be overtaken. Either way the
    # percentage means nothing until the estimate is ahead of the work.
    behind = pct is None and completed is not None and estimated is not None and completed >= estimated
    raw_pct = pct if pct is not None else (100.0 * completed / estimated
                                          if completed is not None and estimated and not behind else None)
    percent = min(99, max(0, raw_pct)) if raw_pct is not None else None
    # Small samples and work-estimate overruns cannot give a useful ETA.
    # Processlist TIME counts whole seconds: below one second there is no rate.
    left = (elapsed * (estimated - completed) / completed
            if elapsed is not None and elapsed >= 1 and completed and estimated and estimated > completed
            and raw_pct >= 1 else None)
    return {"id": int(cid), "stage": stage, "stage_label": STAGES.get(stage, stage or "Reporting progress"),
            "percent": min(99, _round(percent, 1)) if percent is not None else None,
            "completed": _counter(completed), "estimated": _counter(estimated),
            "elapsed_s": _round(elapsed, 3), "left_s": _round(left, 1),
            "statement_kind": _kind(sql), "source": source,
            "note": ("The server has no estimate yet: it grows with the work done "
                     "(table statistics may be out of date)." if behind else None),
            "units": "rows" if stage == "stage/sql/copy to tmp table" else (
                "work_units" if source == "performance_schema" else None)}


def read_progress(reader, server, ids=None, native_rows=None):
    """A failure only removes progress, never the enclosing sessions report."""
    result = {"available": False, "reason": None, "missing": [], "source": None, "items": []}
    sql = progress_sql(server.engine, ids, server.ps_on)
    if not sql:
        result.update(reason="Performance Schema is off. Turn on performance_schema and restart the server.",
                      missing=["performance_schema"])
        return result
    read_error = None
    try:
        # The sessions report already has MariaDB native progress. With P_S
        # off there is no reason to send another database read.
        rows = (list(native_rows) if not server.ps_on and native_rows is not None
                else reader.rows(sql, timeout=5))
    except ConnectorError as exc:
        read_error = "Could not read DDL progress: " + reason(exc)
        rows = list(native_rows or ())
    consumers, instruments, items, native = {}, {}, {}, []
    for raw in rows[:READ_CAP + len(STAGES) + len(CONSUMERS)]:
        row = list(raw) + [None] * (9 - len(raw))
        kind, cid, name = row[:3]
        if kind == "consumer":
            consumers[name] = row[3] == "YES"
        elif kind == "instrument":
            instruments[name] = (row[3] == "YES", row[4] == "YES")
        elif kind == "stage" and _number(cid) and (not ids or int(cid) in ids):
            items[int(cid)] = _item(cid, name, row[3], row[4], row[5], row[6], "performance_schema")
        elif kind in ("processlist", "processlist_total"):
            native.append(row)
    for row in native:
        kind, cid, state, stage, maximum, elapsed, text, pct, command = row
        stage, maximum, pct = (_number(v) for v in (stage, maximum, pct))
        if not _number(cid) or int(cid) in items or (ids and int(cid) not in ids) or command == "Sleep" \
                or not maximum or stage is None or not 1 <= stage <= maximum or pct is None:
            continue
        overall = 100.0 * (stage - 1 + min(100, max(0, pct)) / 100.0) / maximum
        label = (state or "Reporting progress") if kind == "processlist_total" else (
            "%s (stage %d of %d)" % (state or "Reporting progress", stage, maximum))
        # Native progress exposes percentages, not row/work counts. Keep those
        # counters unknown and derive the ETA from the overall fraction only.
        item = _item(cid, label, None, None, elapsed, text, "processlist_progress", pct=overall)
        if state == "copy to tmp table":
            item["stage_label"] = "Copying rows"
        seconds = _number(elapsed)
        if seconds is not None and seconds >= 1 and 1 <= overall < 100:
            item["left_s"] = _round(seconds * (100 - overall) / overall, 1)
        items[int(cid)] = item
    missing = []
    if server.ps_on and not read_error:
        missing += [c for c in CONSUMERS if not consumers.get(c)]
        missing += [s for s in STAGES if not (server.engine == "mariadb" and s.endswith("(flush)"))
                    and not instruments.get(s, (False, False))[0]]
    elif not server.ps_on:
        missing.append("performance_schema")
    ps_ready = server.ps_on and not missing and not read_error
    result.update(available=bool(items) or ps_ready, missing=missing, items=list(items.values()),
                  source=("performance_schema" if any(i["source"] == "performance_schema" for i in items.values())
                          or ps_ready else "processlist_progress" if items else None))
    if read_error:
        result["reason"] = read_error
    elif missing:
        result["reason"] = ("Turn on %s for DDL stage tracking." % ", ".join(missing)
                            if server.ps_on else
                            "Performance Schema is off. Turn on performance_schema and restart the server.")
        if server.engine == "mariadb":
            result["reason"] += " MariaDB native progress covers reporting copy operations, LOAD DATA and Aria, not InnoDB in-place ALTER."
    elif not items:
        result["reason"] = "No reporting DDL stage is active. Some DDL operations do not report progress."
    return result


def session_native_rows(sessions, source):
    """Reuse the MariaDB processlist columns, including SHOW's overall percent.

    SHOW reports an overall percentage (mariadb-11.4.9 sql/sql_show.cc:2881-2890); I_S exposes
    a per-stage percentage. A zero from SHOW alone cannot prove tracking, so
    do not invent a progress item in that case.
    """
    rows = []
    for s in sessions:
        progress = s.get("progress")
        if not progress or s.get("command") == "Sleep":
            continue
        if source == "show_processlist":
            pct = progress.get("pct")
            if pct is None or pct <= 0:
                continue
            stage, maximum = 1, 1
        else:
            stage, maximum, pct = progress.get("stage"), progress.get("max_stage"), progress.get("pct")
        rows.append(["processlist_total" if source == "show_processlist" else "processlist",
                     s["id"], s.get("state"), stage, maximum,
                     s.get("time_s"), s.get("info"), pct, s.get("command")])
    return rows


def ddl_progress_report(conn, connection_ids):
    """Small editor poll: metadata preflight, then one bounded progress read."""
    ids = check_ids(connection_ids)
    reader = Reader(conn, TAG)
    try:
        server = Server(show_variables(reader, ("version", "version_comment", "performance_schema")), {})
    except ConnectorError as exc:
        return {"schema_version": "ddl-progress-1.0", "available": False,
                "reason": "Could not read DDL progress: " + reason(exc), "missing": [], "source": None, "items": [],
                "engine": None, "flavor": None, "server_product": None, "server_version": None}
    result = read_progress(reader, server, ids)
    if server.engine == "mariadb" and server.ps_on and result["reason"] and result["reason"].startswith("Could not read"):
        native = read_progress(reader, Server({"version": server.version, "performance_schema": "OFF"}, {}), ids)
        if native["items"]:
            result.update(available=True, source="processlist_progress", items=native["items"])
    report = base_report("ddl-progress-1.0", server)
    report.update(result)
    return report
