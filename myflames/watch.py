"""Lightweight server watch and condition-triggered evidence capture.

:func:`watch_report` returns the ``watch-1.0`` envelope used by ``/api/live/watch``
while Health is on screen: ``threads_running``, ``longest_statement``
(``{id, seconds, user, db}``), ``longest_lock_wait`` (``{id, seconds}``: how
long an InnoDB row lock has been waited for), ``longest_waiting_statement``
(``{id, seconds, state}``: a statement waiting for a metadata or table lock,
with how long the statement has run) and ``read_at``, from one bounded read
reusing the processlist and lock-wait helpers in :mod:`myflames.sessions` and
:mod:`myflames.locks`. A source that could not be read is named in ``errors``
and ``notes`` for the field it feeds.

:func:`capture_evidence` reads the sessions, InnoDB status, and locks reports
and formats each report's text with the module's own ``format_text`` helper for
``investigation-1.0`` evidence items.
"""
import json

from .connector import ConnectorError
from .discover import _processlist_source
from . import innodb_status
from . import locks
from .perf_common import Reader, _number, _round, _server, check_limit, denied, now, reason
from .server_info import product_name
from . import sessions

SCHEMA_VERSION = "watch-1.0"
CAPTURE_SCHEMA_VERSION = "watch-capture-1.0"
WATCH_TAG = "/*myflames:watch*/ "
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
EVIDENCE_KINDS = ("sessions", "innodb", "locks", "watch")


def _int(value):
    number = _number(value)
    return int(number) if number is not None else None


def _add_note(notes, text):
    if text and text not in notes:
        notes.append(text)


def _read_threads_running(reader, errors, notes):
    try:
        pairs = reader.pairs("SHOW GLOBAL STATUS LIKE 'Threads_running'")
    except ConnectorError as exc:
        msg = "Could not read Threads_running: " + reason(exc)
        errors["threads_running"] = msg
        _add_note(notes, msg)
        return None
    value = _int(pairs.get("Threads_running"))
    if value is None:
        msg = "Threads_running was not returned by SHOW GLOBAL STATUS."
        errors["threads_running"] = msg
        _add_note(notes, msg)
        return None
    return value


def _read_processlist_sessions(reader, engine, ver, ps_on, errors, notes):
    source = _processlist_source(engine, ver, ps_on)
    try:
        rows = reader.rows(sessions._processlist_sql(engine, source))
    except ConnectorError as exc:
        if denied(exc):
            msg = "This user cannot read %s: %s" % (source, reason(exc))
        else:
            msg = "Could not read the process list: " + reason(exc)
        errors["longest_statement"] = msg
        _add_note(notes, msg)
        return None
    rows = rows[:sessions.READ_CAP]
    return [sessions._session(row, engine, source, None) for row in rows if row]


def _pick_longest_statement(parsed_sessions):
    if parsed_sessions is None:
        return None
    best = None
    for s in parsed_sessions:
        if s.get("thread_kind") != "client":
            continue
        info = s.get("info")
        if info and sessions.MYFLAMES_TAG in info:
            continue
        if s.get("command") not in sessions.STATEMENT_COMMANDS:
            continue
        cid = s.get("id")
        seconds = s.get("time_s")
        if cid is None or seconds is None:
            continue
        candidate = {
            "id": cid,
            "seconds": _round(float(seconds), 3),
            "user": s.get("user"),
            "db": s.get("db"),
        }
        if best is None or (candidate["seconds"], -candidate["id"]) > (best["seconds"], -best["id"]):
            best = candidate
    return best


def _read_longest_lock_wait(conn, engine, version, ver, ps_on, limit, errors, notes):
    """The longest InnoDB row lock wait: ``({id, seconds} or None, whether
    PROCESS was granted, or None when unknown)``.

    The seconds are how long the transaction has waited for its row lock,
    from InnoDB's own wait start (INNODB_TRX.trx_wait_started), the one wait
    time the server records. Never a statement's or a transaction's running
    time: process list TIME counts from the statement's start (MySQL
    THD::set_time, MariaDB TIME_MS from start_utime), so a statement that ran
    for minutes and only just began waiting must not trip "a lock wait older
    than N s". A wait without a recorded start has no measured age and is not
    judged. Without INNODB_TRX (no PROCESS) row lock waits are not judged, and
    the reason is kept: performance_schema.data_lock_waits records no wait
    start, and reading it holds InnoDB's lock-system latch on older versions,
    so a watch never reads it. Metadata and table lock waits have no recorded
    start either; they are :func:`_pick_longest_waiting_statement`.
    """
    lock_r = locks._Report(engine, version, ver, ps_on, limit, "waits")
    unread = ""
    try:
        locks._read_transactions(conn, lock_r)
    except ValueError as exc:
        unread = str(exc)
    if not unread and not lock_r.caps["transactions"]:
        unread = ("Row lock wait times need the PROCESS privilege (information_schema.INNODB_TRX)."
                  if lock_r.caps["process_privilege"] is False else
                  lock_r.notes[0] if lock_r.notes else "Could not read information_schema.INNODB_TRX.")
    if unread:
        errors["longest_lock_wait"] = unread
        _add_note(notes, unread)
        return None, lock_r.caps["process_privilege"]
    if lock_r.truncated["transactions"]:
        _add_note(notes, "Only the %d oldest transactions were checked for row lock waits." % limit)
    best = None
    for trx in lock_r.trx:
        cid = trx.get("connection_id")
        wait_s = trx.get("wait_s")
        if trx.get("state") != "LOCK WAIT" or cid is None or wait_s is None:
            continue
        item = {"id": cid, "seconds": _round(float(wait_s), 3)}
        if best is None or (item["seconds"], -item["id"]) > (best["seconds"], -best["id"]):
            best = item
    return best, lock_r.caps["process_privilege"]


def _pick_longest_waiting_statement(parsed_sessions):
    """The longest-running statement waiting for a metadata or table lock:
    ``{id, seconds, state}``. The seconds are how long the statement has run
    (process list TIME, from its start), not how long it has waited: the
    server records no start for these waits (an ALTER TABLE that copied rows
    for ten minutes and then waits for its metadata lock upgrade shows 600 s).
    It is reported, never judged as a lock wait."""
    if parsed_sessions is None:
        return None
    best = None
    for s in parsed_sessions:
        cid = s.get("id")
        seconds = s.get("time_s")
        state = s.get("state") or ""
        if cid is None or seconds is None:
            continue
        if not (locks.is_mdl_wait_state(state) or state == sessions.TABLE_LOCK_STATE):
            continue
        item = {"id": cid, "seconds": _round(float(seconds), 3), "state": state}
        if best is None or (item["seconds"], -item["id"]) > (best["seconds"], -best["id"]):
            best = item
    return best


def watch_report(conn, limit=DEFAULT_LIMIT):
    """Return the ``watch-1.0`` report from one bounded read."""
    limit = check_limit(limit, MAX_LIMIT)
    read_at = now()
    errors = {
        "threads_running": None,
        "longest_statement": None,
        "longest_lock_wait": None,
        "longest_waiting_statement": None,
    }
    notes = []
    try:
        engine, version, ver, ps_on = _server(conn, WATCH_TAG)
    except ConnectorError as exc:
        msg = "Could not read the server version: " + reason(exc)
        for key in errors:
            errors[key] = msg
        _add_note(notes, msg)
        return {
            "schema_version": SCHEMA_VERSION,
            "engine": None,
            "server_version": None,
            "server_product": None,
            "threads_running": None,
            "longest_statement": None,
            "longest_lock_wait": None,
            "longest_waiting_statement": None,
            "read_at": read_at,
            "reason": msg,
            "errors": errors,
            "notes": notes,
        }

    comment_reader = getattr(conn, "server_version_comment", None)
    comment = comment_reader() if callable(comment_reader) else ""
    product = product_name({"engine": engine, "server_version": version, "version_comment": comment})
    reader = Reader(conn, WATCH_TAG)
    threads_running = _read_threads_running(reader, errors, notes)
    parsed_sessions = _read_processlist_sessions(reader, engine, ver, ps_on, errors, notes)
    longest_statement = _pick_longest_statement(parsed_sessions)
    longest_waiting_statement = _pick_longest_waiting_statement(parsed_sessions)
    errors["longest_waiting_statement"] = errors["longest_statement"]
    longest_lock_wait, process = _read_longest_lock_wait(
        conn, engine, version, ver, ps_on, limit, errors, notes)
    if process is False and parsed_sessions is not None:
        # Without PROCESS the process list shows only this account's sessions
        # (MySQL and MariaDB filter it by the caller's user).
        msg = ("Without the PROCESS privilege the process list shows only this account's "
               "sessions, so a longer statement of another account is not seen.")
        errors["longest_statement"] = errors["longest_waiting_statement"] = msg
        _add_note(notes, msg)

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "server_version": version,
        "server_product": product,
        "threads_running": threads_running,
        "longest_statement": longest_statement,
        "longest_lock_wait": longest_lock_wait,
        "longest_waiting_statement": longest_waiting_statement,
        "read_at": read_at,
        "reason": "; ".join(notes) if notes else None,
        "errors": errors,
        "notes": notes,
    }


def format_text(report):
    """Plain-text summary of a ``watch-1.0`` report."""
    if not isinstance(report, dict):
        return "Watch snapshot unavailable.\n"
    head = "%s %s" % (product_name(report), report.get("server_version") or "?")
    if report.get("read_at"):
        head += " at %s" % report["read_at"]
    lines = [head]
    tr = report.get("threads_running")
    lines.append("  Threads running: %s" % (tr if tr is not None else "unknown"))
    stmt = report.get("longest_statement")
    if isinstance(stmt, dict) and stmt.get("id") is not None:
        who = stmt.get("user") or "?"
        db = (" on %s" % stmt["db"]) if stmt.get("db") else ""
        lines.append("  Longest statement: connection %s (%s%s), %s s" % (
            stmt["id"], who, db, stmt.get("seconds")))
    else:
        lines.append("  Longest statement: none")
    wait = report.get("longest_lock_wait")
    errors = report.get("errors") if isinstance(report.get("errors"), dict) else {}
    if isinstance(wait, dict) and wait.get("id") is not None:
        lines.append("  Longest row lock wait: connection %s, waiting %s s" % (
            wait["id"], wait.get("seconds")))
    elif errors.get("longest_lock_wait"):
        lines.append("  Longest row lock wait: not read")
    else:
        lines.append("  Longest row lock wait: none")
    waiting = report.get("longest_waiting_statement")
    if isinstance(waiting, dict) and waiting.get("id") is not None:
        lines.append("  Statement waiting for a metadata or table lock: connection %s (%s), running %s s" % (
            waiting["id"], waiting.get("state") or "?", waiting.get("seconds")))
    for note in report.get("notes") or []:
        lines.append("  Note: %s" % note)
    return "\n".join(lines) + "\n"


def format_evidence_text(kind, data):
    """Format a report dict with the matching module's ``format_text`` helper.

    A report the module cannot format (an older or partial report) is shown
    as its JSON, so an evidence item with data never has empty text.
    """
    if not isinstance(data, dict):
        return ""
    formatters = {"sessions": sessions.format_text, "innodb": innodb_status.format_text,
                  "locks": locks.format_text, "watch": format_text}
    text = ""
    try:
        if kind in formatters:
            text = formatters[kind](data)
    except Exception:
        text = ""
    if not text or not text.strip():
        text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    return text


def capture_evidence(conn, watch_snapshot=None, captured_at=None):
    """Capture sessions, InnoDB status, and locks reports as investigation evidence."""
    stamp = captured_at or now()
    items = []
    errors = []
    if isinstance(watch_snapshot, dict):
        items.append({
            "id": "watch-1",
            "kind": "watch",
            "captured_at": watch_snapshot.get("read_at") or stamp,
            "title": "Watch trigger snapshot",
            "text": format_text(watch_snapshot),
            "data": watch_snapshot,
        })

    specs = (
        ("sessions-1", "sessions", "Processlist (Sessions)",
         lambda: sessions.sessions_report(conn, active_only=False)),
        ("innodb-1", "innodb", "InnoDB status",
         lambda: innodb_status.innodb_report(conn)),
        ("locks-1", "locks", "Locks and blocking transactions",
         lambda: locks.lock_report(conn)),
    )
    for item_id, kind, title, reader_fn in specs:
        item_stamp = now()
        try:
            report = reader_fn()
            text = format_evidence_text(kind, report)
            items.append({
                "id": item_id,
                "kind": kind,
                "captured_at": item_stamp,
                "title": title,
                "text": text or ("%s captured at %s.\n" % (title, item_stamp)),
                "data": report,
            })
        except (ConnectorError, ValueError) as exc:
            msg = reason(exc)
            errors.append("%s: %s" % (title, msg))
            items.append({
                "id": item_id,
                "kind": kind,
                "captured_at": item_stamp,
                "title": title,
                "text": "Could not read %s: %s\n" % (title, msg),
                "data": None,
            })

    return {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "captured_at": stamp,
        "evidence": items,
        "errors": errors,
    }
