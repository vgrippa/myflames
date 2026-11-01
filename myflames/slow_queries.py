"""mysql.slow_log as a Query digest source (``slow-log-1.0``).

The rows of the slow log table inside a time range, summed by statement
fingerprint exactly like a slow log file (slowlog.py), with the same report.
Read-only: one context SELECT and one bounded SELECT of the table, both
tagged so myflames' own reads are left out of later reports.

Verified against server source (mysql-server and mariadb-server checkouts):

* MySQL stores ``current_utime`` (taken when the statement is logged, after it
  ended) in start_time (Log_to_csv_event_handler::log_slow, sql/log.cc);
  MariaDB stores the statement's start (LOGGER::slow_log_print passes
  thd->start_time, sql/log.cc).
* start_time is a TIMESTAMP(6) of a CSV table, which stores text: each row's
  digits are in the time zone of the session that ran the logged statement
  (Field_timestampf val_str, storage/csv/ha_tina.cc), and every reader gets the
  same digits. The range compares those digits; the report names this
  session's zone and says that applications setting their own differ.
* MySQL 8.0.28 and later log a statement whose whole time exceeds
  long_query_time; 5.7 and 8.0 before 8.0.28 count the time after tables are
  locked (utime_after_lock, sql/sql_class.h); MariaDB counts after locks too
  and logs at or above the threshold (sql/sql_class.h).
* query_time and lock_time are TIME(6); sql_text is a MEDIUMBLOB on MySQL and
  MEDIUMTEXT on MariaDB (MySQL scripts/mysql_system_tables.sql; MariaDB
  scripts/mariadb_system_tables.sql). LEFT and CHAR_LENGTH count bytes of
  the blob and characters of the text alike, so the cut check uses both.
* With the default CSV engine the table has no index, so every read scans it.
"""
import re

from . import slowlog
from .connector import ConnectorError
from .perf_common import _server, conn_identity, denied, reason, text_rows

TAG = "/*myflames:slow_log*/ "
#: Rows read per report (the newest in the range).
MAX_ROWS = 20000
#: sql_text read per row (bytes of MySQL's blob, characters of MariaDB's
#: text); a longer statement is marked cut.
TEXT_CHARS = 8192
#: Output one read may produce: the client is stopped at
#: connector.MAX_STDOUT_BYTES (64 MiB), so long statements read fewer rows.
READ_BUDGET = 40 * 1024 * 1024
#: Estimated output of one row besides its text, and the worst growth of the
#: text (four bytes a character, each byte escaped by the client).
ROW_BYTES = 512
TEXT_GROWTH = 2

_CONTEXT_SQL = ("SELECT DATE_FORMAT(NOW(6), '%Y-%m-%d %H:%i:%s.%f'), @@session.time_zone, @@system_time_zone, "
                "TIMESTAMPDIFF(MINUTE, UTC_TIMESTAMP(), NOW()), @@global.log_output, @@global.slow_query_log, "
                "@@global.long_query_time")
_SPAN_SQL = ("SELECT DATE_FORMAT(MIN(start_time), '%Y-%m-%d %H:%i:%s.%f'), "
             "DATE_FORMAT(MAX(start_time), '%Y-%m-%d %H:%i:%s.%f'), COUNT(*) FROM mysql.slow_log")
_TIME = re.compile(r"^(-)?(\d+):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$")
_USER_HOST = re.compile(r"^(?P<priv>[^\[]*)\[(?P<user>[^\]]*)\] @ (?P<host>[^\[]*?) ?\[(?P<ip>[^\]]*)\]")


def check_request(range_from=None, range_to=None, order="total", limit=slowlog.DEFAULT_LIMIT, user=None,
                  database=None):
    """Validate a request before connecting; returns the range keys."""
    slowlog.check_options(order, limit, user, database)
    return slowlog.check_range(range_from, range_to)


def time_micros(text):
    """A TIME value ("00:00:01.000123", "838:59:59") in microseconds, or None."""
    m = _TIME.match((text or "").strip())
    if not m or m.group(1):
        return None
    seconds = int(m.group(2)) * 3600 + int(m.group(3)) * 60 + int(m.group(4))
    return seconds * 1000000 + int((m.group(5) or "0").ljust(6, "0"))


def _null(cell):
    return None if cell is None or cell == "NULL" else cell


def _offset_text(minutes):
    sign = "-" if minutes < 0 else "+"
    minutes = abs(minutes)
    return "%s%02d:%02d" % (sign, minutes // 60, minutes % 60)


def _clock(time_zone, system_zone, offset_minutes):
    zone = "time_zone=%s" % (time_zone or "unknown")
    if (time_zone or "").upper() == "SYSTEM" and system_zone:
        zone += ", system time zone %s" % system_zone
    each = (" Each row keeps the time zone of the session that ran the statement: a client that sets its own "
            "time_zone logs in that zone.")
    if offset_minutes is None:
        return {"kind": "server", "label": "Server time", "offsets": [],
                "text": "Times are as the server wrote them; this session's zone is %s.%s" % (zone, each)}
    offset = _offset_text(offset_minutes)
    label = "UTC" if offset_minutes == 0 else "UTC" + offset
    return {"kind": "server", "label": "Server time (%s)" % label, "offsets": [offset],
            "text": "Times are as the server wrote them; this session's zone is %s, now %s.%s" % (zone, label, each)}


def _threshold_sentence(engine, version_tuple, seconds):
    """When the server logs a statement, as its own source compares it."""
    if engine == "mariadb":
        return ("MariaDB logs a statement whose time after its tables are locked is at least long_query_time "
                "(%s s)." % seconds)
    if isinstance(version_tuple, (tuple, list)) and (0, 0, 0) < tuple(version_tuple)[:3] < (8, 0, 28):
        return ("This MySQL version logs a statement whose time after its tables are locked exceeds "
                "long_query_time (%s s); waits for table locks are not counted." % seconds)
    return "MySQL logs a statement whose whole time, lock waits included, exceeds long_query_time (%s s)." % seconds


def _rows_that_fit(sizes, max_rows):
    """How many of the newest rows (their text's byte lengths, newest first)
    fit READ_BUDGET; at least one, at most *max_rows*."""
    used, fit = 0, 0
    for size in sizes[:max_rows]:
        length = int(size) if size and str(size).isdigit() else TEXT_CHARS * 4
        used += ROW_BYTES + length * TEXT_GROWTH
        if fit and used > READ_BUDGET:
            break
        fit += 1
    return max(fit, 1) if sizes else max_rows


def slow_log_table_report(conn, range_from=None, range_to=None, user=None, database=None, order="total",
                          limit=slowlog.DEFAULT_LIMIT, max_rows=MAX_ROWS):
    """The ``slow-log-1.0`` report of mysql.slow_log rows inside the range."""
    low, high = check_request(range_from, range_to, order, limit, user, database)
    engine, version, version_tuple, _ = _server(conn, TAG)
    notes = []
    try:
        context = text_rows(conn, _CONTEXT_SQL, TAG)
    except ConnectorError:
        context = []
    row = list(context[0]) + [None] * 7 if context else [None] * 7
    now, time_zone, system_zone, offset, log_output, enabled, long_query = (_null(c) for c in row[:7])
    try:
        offset_minutes = int(offset) if offset is not None else None
    except ValueError:
        offset_minutes = None
    where = ["LOCATE('/*myflames:', sql_text) = 0"]
    if low:
        where.append("start_time >= '%s'" % low)
    if high:
        where.append("start_time <= '%s'" % high)
    condition = " AND ".join(where)
    try:
        span = text_rows(conn, _SPAN_SQL, TAG)
        span_rows = _null((list(span[0]) + [None] * 3)[2]) if span else None
        fit = max_rows
        if not (span_rows and str(span_rows).isdigit()
                and int(span_rows) * (ROW_BYTES + TEXT_CHARS * 4 * TEXT_GROWTH) <= READ_BUDGET):
            # Long statements could pass the client's output limit: read the
            # length of each row's text first (a small read) and take the
            # newest rows that fit.
            sizes = text_rows(conn, "SELECT OCTET_LENGTH(CONVERT(LEFT(sql_text, %d) USING utf8mb4)) FROM mysql.slow_log "
                                    "WHERE %s ORDER BY start_time DESC LIMIT %d" % (TEXT_CHARS, condition, max_rows + 1),
                              TAG)
            fit = _rows_that_fit([_null(r[0]) if r else None for r in sizes], max_rows)
        sql = ("SELECT DATE_FORMAT(start_time, '%%Y-%%m-%%d %%H:%%i:%%s.%%f'), user_host, query_time, lock_time, "
               "rows_sent, rows_examined, db, thread_id, CHAR_LENGTH(sql_text), "
               "CONVERT(LEFT(sql_text, %d) USING utf8mb4) FROM mysql.slow_log WHERE %s ORDER BY start_time DESC LIMIT %d"
               % (TEXT_CHARS, condition, fit + 1))
        rows = text_rows(conn, sql, TAG)
    except ConnectorError as exc:
        if denied(exc):
            raise ValueError("This user cannot read mysql.slow_log. Grant it with: "
                             "GRANT SELECT ON mysql.slow_log TO <user>;")
        raise ValueError("Could not read mysql.slow_log: " + reason(exc))
    capped = len(rows) > fit
    rows = rows[:fit]
    stats = slowlog.ParseStats()
    aggregator = slowlog.Aggregator(low, high, user, database, keep_own=True)
    for number, cells in enumerate(reversed(rows), 1):
        cells = list(cells) + [None] * (10 - len(cells))
        start, user_host, query_time, lock_time, sent, examined, db, thread, length, text = \
            (_null(c) for c in cells[:10])
        entry = slowlog.Entry()
        entry.line = None
        entry.time = start[:26] if start else None
        query_us = time_micros(query_time)
        if query_us is None or text is None or not str(text).strip():
            stats.problem(None, "A row has an unreadable query_time or no sql_text.")
            continue
        entry.query_us = query_us
        entry.lock_us = time_micros(lock_time)
        for name, value in (("rows_sent", sent), ("rows_examined", examined)):
            if value is not None and str(value).isdigit():
                entry.values[name] = int(value)
        m = _USER_HOST.match(user_host or "")
        if m:
            entry.user = m.group("user").strip() or m.group("priv").strip() or None
            entry.host = m.group("host").strip() or m.group("ip").strip() or None
        entry.db = db or None
        entry.thread_id = int(thread) if thread and str(thread).isdigit() else None
        text = str(text).strip()
        if text.endswith(";"):
            text = text[:-1].rstrip()
        entry.text = text
        entry.truncated = bool(length and str(length).isdigit() and int(length) > TEXT_CHARS)
        if entry.truncated:
            stats.truncated += 1
        stats.entries += 1
        if entry.time is not None:
            stats.zones[None] += 1
        aggregator.add(entry)
    span_row = list(span[0]) + [None] * 3 if span else [None] * 3
    stats.first, stats.last = _null(span_row[0]), _null(span_row[1])
    table_rows = _null(span_row[2])
    if not rows and log_output and "TABLE" not in str(log_output).upper():
        notes.append("The server writes its slow log to %s (log_output), not to this table. Open the log file "
                     "instead: choose Slow log file as the source." % log_output)
    if enabled is not None and str(enabled).upper() in ("0", "OFF"):
        notes.append("The slow query log is off (slow_query_log=OFF): the table gets no new rows.")
    try:
        threshold = float(long_query) if long_query is not None else None
    except ValueError:
        threshold = None
    if threshold:
        notes.append("%s Sessions can set their own threshold, and log_queries_not_using_indexes adds faster "
                     "statements." % _threshold_sentence(engine, version_tuple, "%g" % threshold))
    if capped:
        notes.append("Only the newest %s rows in the range were read%s%s. Narrow the range to see earlier ones."
                     % ("{:,}".format(fit),
                        " (their statements' text fills what one read may return)" if fit < max_rows else "",
                        "; the User and Database choices apply to those rows" if user is not None or database is not None
                        else ""))
    source = {"kind": "table", "table": "mysql.slow_log", "rows": int(table_rows) if table_rows and
              str(table_rows).isdigit() else None, "rows_read": len(rows), "capped": capped,
              "kind_name": "mysql.slow_log"}
    identity = conn_identity(conn, engine, version)
    report = slowlog.build_report(aggregator, stats, source, order, limit, engine, version,
                                  identity.get("flavor"), identity.get("server_product"), anchor=now,
                                  clock=_clock(time_zone, system_zone, offset_minutes), extra_notes=notes)
    return report
