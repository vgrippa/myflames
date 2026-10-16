"""Find the slow query before capturing it.

Read-only discovery over a live MySQL or MariaDB server, inspired by MySQL
Shell's ``util.debug.collectDiagnostics`` (``collect_slow_queries``) and the
DBeaver session manager:

* :func:`top_statements` ranks normalized statements from
  ``performance_schema.events_statements_summary_by_digest`` (no ``sys``
  schema dependency).
* :func:`running_statements` lists statements executing right now.
* :func:`explain_connection` asks the server for the plan of a statement
  that another connection is running (estimated on MySQL, partial runtime
  statistics on MariaDB where ``SHOW ANALYZE`` exists).

Workload attribution links a server-wide counter to the statements behind it
(``top --order full_join|scan|no_index`` and ``top --table SCHEMA.TABLE``):

* The status counters and the digest columns are incremented by the same
  calls, once per table access the plan reads in full (M/sql/sql_class.cc:
  2522-2559; Ma/sql/sql_class.cc:6502-6540). MySQL: a full table or index
  scan of the first table of a query block counts ``Select_scan`` /
  ``SUM_SELECT_SCAN``, of a later table ``Select_full_join`` /
  ``SUM_SELECT_FULL_JOIN``, and a range check for each row
  ``Select_range_check`` / ``SUM_SELECT_RANGE_CHECK``; 8.0 and 8.4 count
  when the plan is set up (make_join_readinfo, mysql-8.4.0
  sql/sql_select.cc:3365-3402, mysql-8.0.40 3319-3356), 9.x when it runs
  (M/sql/join_optimizer/access_path.cc:1914-1944, M/sql/sql_union.cc:
  1188-1190). MariaDB counts a full index scan as ``Select_scan`` at any
  join position, and only full table scans of later tables as full joins
  (Ma/sql/sql_select.cc:16480-16560). So a digest's sum is the same event
  Health counts, but only for statements the digest table recorded, and
  summed since the server started or the table was truncated, not over
  Health's window.
* ``SUM_NO_INDEX_USED`` counts executions with a table scan (not an index
  scan): a flag set once per statement (M/storage/perfschema/pfs.cc:
  6905-6907, 7215), not per table. MySQL also sets it for a range check;
  MariaDB does not, but sets it for a single-table UPDATE or DELETE without
  a range (Ma/sql/sql_update.cc:558, sql_delete.cc:660). It is set for
  EXPLAIN too (measured on MySQL 8.0 and MariaDB 11.4), so EXPLAIN and DESC
  digests stay excluded as plan diagnostics (:data:`DIAGNOSTIC_PREFIXES`).
* A full digest table sums new digests into one row whose DIGEST is NULL
  (:func:`_digest_overflow`), and ``Performance_schema_digest_lost`` counts
  them; ``FIRST_SEEN`` dates each digest (M/storage/perfschema/
  table_esms_by_digest.cc:72-87; Ma/.../table_esms_by_digest.cc:77-88).
* The table filter matches names in DIGEST_TEXT (:func:`table_reference`),
  which keeps backquoted identifiers with spaces around the dot
  (`` `shop` . `orders` ``); every match is labeled "text match, not proof"
  (:data:`TEXT_MATCH_CAVEATS`).

Nothing here changes server state: no KILL, no consumer or instrument
updates. The only session setting written is MySQL's
``explain_json_format_version`` for this tool's own short-lived session.

Verified against server source (paths relative to the mysql-server and
mariadb-server checkouts):

* Digest table columns, including ``QUERY_SAMPLE_TEXT`` (MySQL 8.0.3+):
  storage/perfschema/table_esms_by_digest.cc:56-95. MariaDB's table has no
  sample column: mariadb storage/perfschema/table_esms_by_digest.cc:60-90.
  The table needs a SELECT grant (``pfs_truncatable_acl``,
  table_esms_by_digest.cc:102, pfs_engine_table.cc:1100).
* ``QUERY_SAMPLE_TEXT`` is capped at ``performance_schema_max_sql_text_length``
  bytes (default 1024): sql/sys_vars.cc:1095-1098, pfs_digest.cc:119-157.
* MariaDB ships with ``performance_schema=OFF``: mariadb sql/sys_vars.cc:88-91.
* ``performance_schema.processlist`` exists from MySQL 8.0.22 (WL#9090); its
  rows are limited to the caller's own user unless PROCESS is granted:
  storage/perfschema/table_processlist.cc:56-66, 96-121. TIME is seconds.
* MariaDB ``information_schema.PROCESSLIST`` adds ``TIME_MS``:
  mariadb sql/sql_show.cc:10566-10590.
* MySQL ``EXPLAIN FOR CONNECTION`` (sql/opt_explain.cc:2443-2545): other users'
  connections need every global privilege except GRANT OPTION, not merely
  PROCESS (2452-2465); prepared statements, stored-routine statements and
  statements still being resolved are refused with ER_EXPLAIN_NOT_SUPPORTED
  (2505-2528); an idle connection returns OK with no rows (2512-2514).
* The JSON shape of ``EXPLAIN FORMAT=JSON FOR CONNECTION`` follows the
  *explaining* session's ``explain_json_format_version`` (or hypergraph on the
  target): sql/opt_explain_json.cc:2119-2124, sql/opt_explain.cc:2262. The
  variable exists from MySQL 8.3.0 (WL#15684) and defaults to 2 from 9.5.0
  (WL#17039, sql/sys_vars.cc:7611-7617). Version 1 output is the older
  ``query_block`` shape, so myflames sets version 2 explicitly.
* MariaDB ``SHOW EXPLAIN|ANALYZE [FORMAT=JSON] FOR n`` and ``EXPLAIN FOR
  CONNECTION`` JSON forms arrive in 10.9.1 (MDEV-10000, MDEV-27021;
  mariadb sql/sql_yacc.yy:15098-15117). Other users' threads need PROCESS
  (``PRIV_STMT_SHOW_EXPLAIN``, mariadb sql/privilege.h:667,
  sql/sql_show.cc:3297-3318).
"""
import datetime
import re

from .connector import ConnectorError, MySQLConnection
from .live import capture_problem
# Compatibility exports for one release; these primitives no longer belong to discovery.
from .perf_common import _denied, _reason, _server, _version_tuple, text_rows

ORDERS = {
    "total": "SUM_TIMER_WAIT",
    "avg": "AVG_TIMER_WAIT",
    "examined": "SUM_ROWS_EXAMINED",
    "count": "COUNT_STAR",
    # Workload attribution (see the module docstring): the statements behind
    # Health's "Joins without an index" (Select_full_join), "Full scans of the
    # first table" (Select_scan), and executions that read a table without an
    # index. Only digests whose counter is above zero are listed.
    "full_join": "SUM_SELECT_FULL_JOIN",
    "scan": "SUM_SELECT_SCAN",
    "no_index": "SUM_NO_INDEX_USED",
}
#: The attribution orders list only digests whose counter is above zero.
#: SUM_SELECT_RANGE_CHECK (a later table read with a range check for each
#: row: no usable index when the plan was made) is listed with full joins.
ORDER_FILTERS = {
    "full_join": "(SUM_SELECT_FULL_JOIN > 0 OR SUM_SELECT_RANGE_CHECK > 0)",
    "scan": "SUM_SELECT_SCAN > 0",
    "no_index": "SUM_NO_INDEX_USED > 0",
}
ORDER_TIEBREAKS = {"full_join": "SUM_SELECT_RANGE_CHECK DESC, SUM_TIMER_WAIT DESC"}
#: A table filter (top --table) lists digests that read some table without
#: an index and whose text names the table.
TABLE_FILTER = "(SUM_NO_INDEX_USED > 0 OR SUM_SELECT_SCAN > 0)"
#: Digests checked against the table name before the limit is applied.
TABLE_CANDIDATES = 500
#: Every table match is labeled with this text in the payload.
TEXT_MATCH_LABEL = "text match, not proof"
TEXT_MATCH_CAVEATS = (
    "The table is matched by name in the normalized digest text, so this is a text match, not proof: a view, a "
    "stored routine or a trigger can read the table without naming it, an alias or another schema's table with "
    "the same name can match, and digest text is cut at performance_schema_max_digest_length (1024 bytes by "
    "default).",
    "SUM_NO_INDEX_USED and SUM_SELECT_SCAN count per statement, not per table: a statement that names this table "
    "can match because it scanned another table it reads.",
    "SCHEMA_NAME is the session's default schema, not the table's: an unqualified name matches only digests whose "
    "default schema is the table's schema.",
)
#: Background and replication threads that never carry a user statement.
IDLE_COMMANDS = ("Sleep", "Daemon", "Binlog Dump", "Binlog Dump GTID")
#: Capture statements written by live.py start with this comment.
CAPTURE_TAG = "/*myflames:"
#: Discovery statements carry the same prefix so they exclude themselves.
DISCOVER_TAG = "/*myflames:discover*/ "
LOW_SELECTIVITY = 0.5
_PICO_PER_MS = 1e9


def _null(value):
    return None if value in (None, "NULL") else value


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _literal(text):
    """Quote user text as a hex literal so no escaping rules apply."""
    return "CONVERT(X'%s' USING utf8mb4)" % text.encode("utf-8").hex()


def _rows(conn, sql):
    """Read discovery-tagged batch rows without converting literal NULL."""
    return text_rows(conn, sql, DISCOVER_TAG)


def _perfschema_off_message(engine):
    if engine == "mariadb":
        return ("Performance Schema is off. MariaDB disables it by default; add "
                "performance_schema=ON under [mysqld] in my.cnf and restart the server, "
                "then run the workload again so statements are recorded.")
    return ("Performance Schema is off (performance_schema=OFF). Set "
            "performance_schema=ON in my.cnf and restart the server, then run the "
            "workload again so statements are recorded.")


def _statement_flags(item):
    flags = []
    if item["no_index_used"]:
        flags.append("no_index_used")
    if item["no_good_index_used"]:
        flags.append("no_good_index_used")
    if item["tmp_disk_tables"]:
        flags.append("tmp_disk_tables")
    if item["sort_merge_passes"]:
        flags.append("sort_merge_passes")
    # Based on MySQL Shell collect_slow_queries (python/plugins/debug/
    # sql_collector.py:~1345), which flags digests starting with "select"
    # whose sent/examined ratio is below 0.5; WITH and parenthesized SELECTs
    # are counted here too.
    text = (item["digest_text"] or "").lstrip().upper()
    if (text.startswith(("SELECT", "WITH", "(")) and item["rows_examined"] > 0
            and item["rows_sent"] / float(item["rows_examined"]) < LOW_SELECTIVITY):
        flags.append("low_selectivity")
    return flags


#: Digest prefixes of plan diagnostics (the shape of every capture statement).
#: DESCRIBE prints as EXPLAIN in digest text (one lexer token, M/sql/lex.h:
#: 213, 261); DESC stays DESC.
DIAGNOSTIC_PREFIXES = ("EXPLAIN ", "ANALYZE ", "DESC ", "SHOW ", "SET SESSION EXPLAIN_JSON_FORMAT_VERSION",
                       "SET SESSION OPTIMIZER_TRACE", "SET SESSION TRANSACTION READ ONLY",
                       "START TRANSACTION READ ONLY")
#: Monitoring and metadata reads (this tool's discovery, capture collectors,
#: lock views, dashboards) are not workload.
MONITORING_WORDS = ("PERFORMANCE_SCHEMA", "PROCESSLIST", "INFORMATION_SCHEMA",
                    "INNODB_TABLE_STATS", "COLUMN_STATS")
#: Capture helper statements as normalized digest text (backticks removed).
#: MariaDB keeps no sample text, so these exact shapes are dropped by text.
HELPER_DIGESTS = ("SELECT ?", "SELECT CONNECTION_ID ( )", "SELECT VERSION ( )",
                  # the mysql/mariadb client sends this on every connection
                  "SELECT @@VERSION_COMMENT LIMIT ?",
                  # myflames' own version check (connector.server_version)
                  "SELECT @@VERSION")


def _excluded(digest_text, sample_text):
    """True for myflames' own statements, plan diagnostics and monitoring reads.

    MySQL keeps the client comment in QUERY_SAMPLE_TEXT, so tagged capture
    and discovery statements are dropped precisely. Digest text never keeps
    comments and MariaDB has no sample column, so EXPLAIN/ANALYZE digests
    (the shape of every capture statement) and statements that read
    performance_schema or the process list (the shape of every discovery
    statement) are excluded by their normalized text as well.
    """
    if sample_text and CAPTURE_TAG in sample_text:
        return True
    # Digest text quotes identifiers (SET SESSION `optimizer_trace` = ?), so
    # compare without backticks.
    text = " ".join((digest_text or "").replace("`", "").upper().split())
    if text.startswith(DIAGNOSTIC_PREFIXES) or text in HELPER_DIGESTS:
        return True
    return any(word in text for word in MONITORING_WORDS)


def _check_digest_consumer(conn):
    try:
        rows = _rows(conn, "SELECT NAME, ENABLED FROM performance_schema.setup_consumers "
                           "WHERE NAME IN ('global_instrumentation', 'statements_digest')")
    except ConnectorError as exc:
        if _denied(exc):
            raise ValueError(
                "This user cannot read performance_schema. Grant it with: "
                "GRANT SELECT ON performance_schema.* TO <user>;")
        raise ValueError("Could not read performance_schema.setup_consumers: " + _reason(exc))
    disabled = [row[0] for row in rows if len(row) > 1 and row[1].strip().upper() != "YES"]
    if disabled:
        raise ValueError(
            "Statement digests are not being collected (consumer disabled: %s). Enable "
            "them with: UPDATE performance_schema.setup_consumers SET ENABLED='YES' WHERE "
            "NAME IN ('global_instrumentation','statements_digest'); then run the workload "
            "again." % ", ".join(disabled))


def top_statements(conn, limit=20, schema=None, order="total", table=None):
    """Return the heaviest normalized statements, heaviest first.

    Each item: digest, digest_text, schema, count, total_ms, avg_ms, max_ms,
    rows_examined, rows_sent, examined_per_sent, no_index_used,
    no_good_index_used, tmp_disk_tables, sort_merge_passes,
    select_full_join, select_range_check, select_scan, sample_text,
    sample_truncated, first_seen, last_seen, flags, capture_problem (and
    table_match with a *table* filter).

    Raises ValueError with an actionable message when Performance Schema is
    off, the digest consumer is disabled, or SELECT on performance_schema is
    missing.
    """
    return _top(conn, limit, schema, order, table)["statements"]


def parse_table_name(text):
    """``(schema, table)`` from ``schema.table`` (either part may be
    backquoted, with doubled backquotes inside). Raises ValueError."""
    if isinstance(text, (list, tuple)) and len(text) == 2:
        parts = [text[0], text[1]]
    elif isinstance(text, dict):
        parts = [text.get("schema"), text.get("table")]
    elif isinstance(text, str):
        parts, current, quoted, i = [], [], False, 0
        while i < len(text):
            ch = text[i]
            if ch == "`":
                if quoted and i + 1 < len(text) and text[i + 1] == "`":
                    current.append("`")
                    i += 2
                    continue
                quoted = not quoted
            elif ch == "." and not quoted and not parts:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
            i += 1
        if quoted:
            raise ValueError("Unclosed backquote in the table name %r." % text)
        parts.append("".join(current))
    else:
        parts = []
    if len(parts) != 2 or not all(isinstance(p, str) and p and len(p) <= 64 and "\x00" not in p
                                  and "\n" not in p for p in parts):
        raise ValueError("Name the table as SCHEMA.TABLE (backquote a part that contains a dot), "
                         "1 to 64 characters each.")
    return parts[0], parts[1]


def _ident_pattern(name):
    """The digest-text spellings of one identifier: backquoted always, bare
    when the name needs no quoting."""
    alternatives = [re.escape("`" + name.replace("`", "``") + "`")]
    if re.match(r"^[A-Za-z0-9_$]+$", name) and not name.isdigit():
        alternatives.append(re.escape(name))
    return "(?:%s)" % "|".join(alternatives)


def table_reference(digest_text, digest_schema, schema, table):
    """How *digest_text* names ``schema.table``: ``"qualified"``
    (`` `schema` . `table` `` or ``schema.table``, whatever the default
    schema), ``"unqualified"`` (the bare table name in a digest whose
    default schema is *schema*), or None. Case-insensitive, because table
    names are on case-insensitive file systems (lower_case_table_names 1 or
    2). A text match, not proof (see :data:`TEXT_MATCH_CAVEATS`)."""
    text = digest_text or ""
    t = _ident_pattern(table)
    edge_before, edge_after = r"(?<![\w$`])", r"(?![\w$`])"
    qualified = re.compile(edge_before + _ident_pattern(schema) + r"\s*\.\s*" + t + edge_after, re.I)
    if qualified.search(text):
        return "qualified"
    if (digest_schema or "").lower() != schema.lower():
        return None
    # Not the table part of another schema's name (a dot before it, with any
    # whitespace between), nor followed by a dot (that would make it a schema
    # name or a table-qualified column).
    bare = re.compile(edge_before + t + edge_after + r"(?!\s*\.)", re.I)
    for match in bare.finditer(text):
        if not text[:match.start()].rstrip().endswith("."):
            return "unqualified"
    return None


def _top(conn, limit=20, schema=None, order="total", table=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("Choose a limit from 1 to 200.")
    if order not in ORDERS:
        raise ValueError("Order by one of: " + ", ".join(ORDERS) + ".")
    if schema is not None and (not isinstance(schema, str) or not schema or len(schema) > 64
                               or "\x00" in schema):
        raise ValueError("Schema names are 1 to 64 characters.")
    if table is not None:
        table = parse_table_name(table)
    # Same rules as _excluded, in SQL so LIMIT counts only workload rows.
    # P_S text columns use case-insensitive collations, so LOCATE ignores case.
    where = ["TRIM(REPLACE(DIGEST_TEXT, '`', '')) NOT LIKE '%s%%'" % p for p in DIAGNOSTIC_PREFIXES]
    where.extend("LOCATE('%s', DIGEST_TEXT) = 0" % w.lower() for w in MONITORING_WORDS)
    where.append("TRIM(REPLACE(DIGEST_TEXT, '`', '')) NOT IN (%s)"
                 % ", ".join("'%s'" % h for h in HELPER_DIGESTS))
    if schema is not None:
        where.append("SCHEMA_NAME = " + _literal(schema))
    # A table filter has its own predicate (a table scan or a first-table
    # scan); no_index and scan only order its rows.
    if order in ORDER_FILTERS and not (table is not None and order in ("no_index", "scan")):
        where.append(ORDER_FILTERS[order])
    fetch = limit
    if table is not None:
        where.append(TABLE_FILTER)
        where.append("LOCATE(%s, DIGEST_TEXT) > 0" % _literal(table[1]))
        fetch = TABLE_CANDIDATES
    tail = "ORDER BY %s DESC LIMIT %d" % (ORDERS[order], fetch)
    if order in ORDER_FILTERS:
        tail = "ORDER BY %s DESC, %s LIMIT %d" % (
            ORDERS[order], ORDER_TIEBREAKS.get(order, "SUM_TIMER_WAIT DESC"), fetch)
    engine, version, has_sample, max_text, items = _digest_rows(conn, where, tail, exclude=True)
    candidates = len(items)
    if table is not None:
        matched = []
        for item in items:
            kind = table_reference(item["digest_text"], item["schema"], table[0], table[1])
            if kind:
                item["table_match"] = {"schema": table[0], "table": table[1], "kind": kind,
                                       "label": TEXT_MATCH_LABEL}
                matched.append(item)
        items = matched[:limit]
    notes = ["Counters accumulate since the server started or the digest table was last truncated."]
    if order in ORDER_FILTERS and not (table is not None and order in ("no_index", "scan")):
        notes.append(_ORDER_NOTES[order])
    if not has_sample:
        notes.append("This server keeps no sample statement text; digest text shows ? where "
                     "literal values were, so fill those in before capturing.")
    elif any(s["sample_truncated"] for s in items):
        notes.append("Some sample statements reached performance_schema_max_sql_text_length "
                     "(%d bytes) and may be cut off." % max_text)
    overflow = _digest_overflow(conn)
    if overflow:
        count, total_ms = overflow
        notes.append(
            "The digest table is full: %s more executions (%s) were summed into one overflow row "
            "without a digest, so they are not listed. Raise performance_schema_digests_size "
            "(needs a restart) or truncate the table to see them; Performance_schema_digest_lost "
            "counts them too." % ("{:,}".format(count), _ms_text(total_ms)))
    report = {"schema_version": "top-1.0", "engine": engine, "server_version": version,
              "order": order, "limit": limit, "schema": schema, "notes": notes,
              "statements": items,
              "table": {"schema": table[0], "table": table[1]} if table else None,
              "coverage": _coverage(conn, items, overflow)}
    if table is not None:
        report["match"] = {"label": TEXT_MATCH_LABEL, "caveats": list(TEXT_MATCH_CAVEATS),
                           "candidates": candidates, "capped": candidates >= TABLE_CANDIDATES}
        notes.append("Statements are matched to %s.%s by name in their digest text: %s." % (
            table[0], table[1], TEXT_MATCH_LABEL))
        if candidates >= TABLE_CANDIDATES:
            notes.append("Only the %d heaviest digests that name the table were checked." % TABLE_CANDIDATES)
    return report


_ORDER_NOTES = {
    "full_join": ("Listed: statements that read a later table of a join with a full scan (SUM_SELECT_FULL_JOIN, "
                  "the per-statement form of Select_full_join; on MariaDB a full table scan only) or with a range "
                  "check for each row (SUM_SELECT_RANGE_CHECK). Health counts the same events over its window; "
                  "these sums run since the digest was first seen."),
    "scan": ("Listed: statements whose first table was read with a full table or index scan (SUM_SELECT_SCAN, "
             "the per-statement form of Select_scan; MariaDB also counts a full index scan of any table here)."),
    "no_index": ("Listed: statements that read some table with a table scan (SUM_NO_INDEX_USED, once per "
                 "execution; MySQL also counts a range check, MariaDB a single-table UPDATE or DELETE without a "
                 "range)."),
}


def _coverage(conn, items, overflow):
    """What the list covers: Uptime, the oldest FIRST_SEEN listed, the
    overflow row, and Performance_schema_digest_lost (best effort)."""
    seen = [item["first_seen"] for item in items if item.get("first_seen")]
    coverage = {"uptime_s": None, "first_seen": min(seen) if seen else None, "digest_lost": None,
                "overflow": {"executions": overflow[0], "total_ms": round(overflow[1], 3)} if overflow else None}
    try:
        rows = _rows(conn, "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Performance_schema_digest_lost')")
    except Exception:
        return coverage
    values = {row[0]: row[1] for row in rows if len(row) >= 2}
    if "Uptime" in values:
        coverage["uptime_s"] = _int(values["Uptime"])
    if "Performance_schema_digest_lost" in values:
        coverage["digest_lost"] = _int(values["Performance_schema_digest_lost"])
    return coverage


def _digest_overflow(conn):
    """``(executions, total_ms)`` summed into the digest table's overflow row.

    When events_statements_summary_by_digest is full, statements with a new
    digest are added to one row whose DIGEST is NULL (M/storage/perfschema/
    pfs_digest.cc:300-310, table_helper.cc:710-715). The main query skips that
    row, so report it. Best effort: any error or unexpected shape is ignored.
    """
    try:
        rows = _rows(conn, "SELECT COUNT_STAR, SUM_TIMER_WAIT FROM "
                           "performance_schema.events_statements_summary_by_digest "
                           "WHERE DIGEST IS NULL")
    except Exception:
        return None
    if len(rows) != 1 or len(rows[0]) != 2:
        return None
    count, wait = _int(_null(rows[0][0])), _int(_null(rows[0][1]))
    if not count:
        return None
    return count, (wait or 0) / 1e9


def _ms_text(ms):
    if ms >= 1000:
        return "{:,.1f} s".format(ms / 1000.0)
    return "{:,.1f} ms".format(ms)


def _digest_rows(conn, where, tail, exclude=False):
    """Read digest rows: (engine, version, has_sample, max_text, items).

    *where* adds conditions to the shared ones; *tail* is the ORDER BY/LIMIT.
    With *exclude*, myflames' own captures are left out (see _excluded).
    """
    engine, version, ver, enabled = _server(conn)
    if not enabled:
        raise ValueError(_perfschema_off_message(engine))
    _check_digest_consumer(conn)
    has_sample = engine == "mysql" and ver >= (8, 0, 3)
    max_text = 0
    if has_sample:
        try:
            rows = _rows(conn, "SELECT @@performance_schema_max_sql_text_length")
            max_text = _int(rows[0][0]) if rows and rows[0] else 0
        except ConnectorError:
            max_text = 0
    columns = ("SCHEMA_NAME, DIGEST, DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, AVG_TIMER_WAIT, "
               "MAX_TIMER_WAIT, SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_NO_INDEX_USED, "
               "SUM_NO_GOOD_INDEX_USED, SUM_CREATED_TMP_DISK_TABLES, SUM_SORT_MERGE_PASSES, "
               "FIRST_SEEN, LAST_SEEN")
    conditions = ["DIGEST IS NOT NULL", "DIGEST_TEXT IS NOT NULL"]
    if has_sample:
        columns += ", QUERY_SAMPLE_TEXT"
        if exclude:
            conditions.append("(QUERY_SAMPLE_TEXT IS NULL OR LOCATE('%s', QUERY_SAMPLE_TEXT) = 0)"
                              % CAPTURE_TAG)
    # Attribution counters come last, so rows read before they existed (and
    # replayed answers) keep their column positions.
    columns += ", SUM_SELECT_FULL_JOIN, SUM_SELECT_RANGE_CHECK, SUM_SELECT_SCAN"
    extra_at = 16 if has_sample else 15
    conditions.extend(where)
    sql = ("SELECT %s FROM performance_schema.events_statements_summary_by_digest WHERE %s %s"
           % (columns, " AND ".join(conditions), tail))
    try:
        rows = _rows(conn, sql)
    except ConnectorError as exc:
        if _denied(exc):
            raise ValueError(
                "This user cannot read performance_schema.events_statements_summary_by_digest. "
                "Grant it with: GRANT SELECT ON performance_schema.* TO <user>;")
        raise ValueError("Could not read statement digests: " + _reason(exc))
    items = []
    for row in rows:
        row = list(row) + [""] * (extra_at + 3 - len(row))
        digest_text = _null(row[2])
        sample = _null(row[15]) if has_sample else None
        if exclude and _excluded(digest_text, sample):
            continue
        item = {
            "digest": _null(row[1]),
            "digest_text": digest_text,
            "schema": _null(row[0]),
            "count": _int(row[3]),
            "total_ms": round(_int(row[4]) / _PICO_PER_MS, 3),
            "avg_ms": round(_int(row[5]) / _PICO_PER_MS, 3),
            "max_ms": round(_int(row[6]) / _PICO_PER_MS, 3),
            "rows_examined": _int(row[7]),
            "rows_sent": _int(row[8]),
            "examined_per_sent": None,
            "no_index_used": _int(row[9]),
            "no_good_index_used": _int(row[10]),
            "tmp_disk_tables": _int(row[11]),
            "sort_merge_passes": _int(row[12]),
            "select_full_join": _int(row[extra_at]),
            "select_range_check": _int(row[extra_at + 1]),
            "select_scan": _int(row[extra_at + 2]),
            "sample_text": sample,
            "sample_truncated": bool(sample and max_text and len(sample.encode("utf-8")) >= max_text),
            "first_seen": _null(row[13]),
            "last_seen": _null(row[14]),
        }
        if item["rows_sent"]:
            item["examined_per_sent"] = round(item["rows_examined"] / float(item["rows_sent"]), 2)
        item["flags"] = _statement_flags(item)
        item["capture_problem"] = capture_problem(digest_query(item)["text"])
        items.append(item)
    return engine, version, has_sample, max_text, items


#: Quoted text and comments are copied unchanged; a digest's collapsed lists
#: ("(...)" and "?, ...", MySQL sql/gen_lex_token.cc:351-369, MariaDB
#: sql/gen_lex_token.cc:190-212) become one parameter; every other ? becomes
#: one parameter. Mirrors apps/workspace/src/placeholders.ts; both run the
#: shared cases in apps/workspace/test/fixtures/digest-placeholders.json.
_SKIP = (r"(/\*.*?(?:\*/|\Z)|--(?=\s|\Z)[^\r\n]*|#[^\r\n]*"
         r"|'(?:''|\\.|[^'\\])*'?|\"(?:\"\"|\\.|[^\"\\])*\"?|`(?:``|[^`])*`?)")
_PLACEHOLDER = re.compile(
    _SKIP + r"|(\(\s*\.\.\.\s*\)|\?\s*,\s*\.\.\.)|(\?)", re.S)
#: Names the lexer reads as a function only when "(" follows with no space
#: (SYM_FN in MySQL sql/lex.h:847-882; sql_functions[] in MariaDB
#: sql/lex.h). Digest text prints "COUNT ( * )", which is a syntax error.
_FUNCTION_ONLY = (
    "ADDDATE", "BIT_AND", "BIT_OR", "BIT_XOR", "CAST", "COUNT", "CUME_DIST", "CURDATE",
    "CURTIME", "DATE_ADD", "DATE_SUB", "DENSE_RANK", "EXTRACT", "FIRST_VALUE",
    "GROUP_CONCAT", "JSON_ARRAYAGG", "JSON_DUALITY_OBJECT", "JSON_OBJECTAGG", "LAG",
    "LEAD", "MAX", "MEDIAN", "MID", "MIN", "NOW", "NTH_VALUE", "NTILE", "PERCENT_RANK",
    "PERCENTILE_CONT", "PERCENTILE_DISC", "POSITION", "RANK", "SESSION_USER", "STD",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP", "ST_COLLECT", "SUBDATE", "SUBSTR", "SUBSTRING",
    "SUM", "SYSDATE", "SYSTEM_USER", "TRIM", "TRIM_ORACLE", "VARIANCE", "VAR_POP", "VAR_SAMP")
_FUNCTION_SPACE = re.compile(
    _SKIP + r"|(?<![\w.$@`])(%s)\s+\(" % "|".join(_FUNCTION_ONLY), re.S | re.I)


def join_function_calls(sql):
    """Drop the space digest text puts between a built-in function name and
    its "(" (``COUNT ( * )`` becomes ``COUNT( * )``), outside quotes and
    comments, so the statement parses again."""
    return _FUNCTION_SPACE.sub(
        lambda m: m.group(1) if m.group(1) is not None else m.group(2) + "(", sql or "")


def parameterize(text):
    """Turn each ``?`` placeholder of normalized statement text into ``:p1``,
    ``:p2``, ... for capture's named parameters.

    Returns ``(sql, names, lists)``; *lists* names the parameters that stand
    for a collapsed value list (``IN (...)`` becomes ``IN (:p1)``).
    """
    names, lists, out, at = [], [], [], 0
    for match in _PLACEHOLDER.finditer(text or ""):
        out.append(text[at:match.start()])
        at = match.end()
        if match.group(1) is not None:
            out.append(match.group(1))
            continue
        name = "p%d" % (len(names) + 1)
        names.append(name)
        if match.group(2) is not None:
            lists.append(name)
            out.append("(:%s)" % name if match.group(2).startswith("(") else ":" + name)
        else:
            out.append(":" + name)
    out.append((text or "")[at:])
    return "".join(out), names, lists


def digest_query(item):
    """How a capture starts from a ``top-1.0`` statement.

    The sample text is used unless it is missing or was cut off; otherwise
    the normalized digest text, with built-in function calls rejoined
    (:func:`join_function_calls`). ``reason`` says why values must be filled in
    (``normalized``: no sample on this server; ``truncated``: the sample was
    cut off; ``placeholders``: the sample itself has ``?`` markers;
    ``missing``: no text at all), or is None when the SQL runs as is.
    """
    recorded = item.get("sample_text")
    sample = recorded if recorded and not item.get("sample_truncated") else None
    text = sample or item.get("digest_text") or ""
    sql, names, lists = parameterize(text)
    if not sample:
        sql = join_function_calls(sql)
    if not text.strip():
        reason = "missing"
    elif not names:
        reason = None
    elif sample:
        reason = "placeholders"
    elif recorded:
        reason = "truncated"
    else:
        reason = "normalized"
    return {"text": text, "sql": sql.strip(), "names": names, "lists": lists,
            "from_sample": bool(sample), "reason": reason}


def short_digest(digest):
    """The first 12 hex characters: enough to tell digests apart in a list."""
    return (digest or "")[:12]


def find_statement(conn, digest, schema=None):
    """Look up one statement by its digest (or a prefix of 8+ hex characters).

    The same digest can be recorded under several default schemas; the row
    for *schema* wins, otherwise the one with the most total time. Returns
    {engine, server_version, statement, schemas} where *statement* has the
    ``top-1.0`` fields. Raises ValueError when nothing or more than one
    digest matches.
    """
    if not isinstance(digest, str) or not re.match(r"^[0-9a-fA-F]{8,64}$", digest):
        raise ValueError("Pass a statement digest: 8 to 64 hexadecimal characters "
                         "(myflames top --json lists them).")
    digest = digest.lower()
    engine, version, _, _, items = _digest_rows(
        conn, ["DIGEST LIKE '%s%%'" % digest], "ORDER BY SUM_TIMER_WAIT DESC LIMIT 50")
    if not items:
        raise ValueError(
            "No statement digest starting with %s is in performance_schema."
            "events_statements_summary_by_digest. It may have been evicted or the table "
            "truncated; run myflames top to list the current digests." % digest)
    digests = sorted(set(item["digest"] for item in items))
    if len(digests) > 1:
        raise ValueError("Digest prefix %s matches %d statements (%s); use more characters."
                         % (digest, len(digests), ", ".join(short_digest(d) for d in digests[:5])))
    chosen = next((item for item in items if schema and item["schema"] == schema), items[0])
    return {"engine": engine, "server_version": version, "statement": chosen,
            "schemas": [item["schema"] for item in items]}


def top_report(conn, limit=20, schema=None, order="total", table=None):
    """The ``top-1.0`` JSON envelope shared by the CLI and the UI.

    *table* (``"schema.table"``, a pair, or ``{"schema", "table"}``) keeps
    only digests that read a table without an index and whose text names
    that table; each gets ``table_match`` (see :func:`table_reference`)."""
    return _top(conn, limit, schema, order, table)


def _processlist_source(engine, ver, enabled):
    if engine == "mysql" and enabled and ver >= (8, 0, 22):
        return "performance_schema.processlist"
    return "information_schema.PROCESSLIST"


def _running(conn, min_seconds=0, connection_id=None):
    engine, version, ver, enabled = _server(conn)
    source = _processlist_source(engine, ver, enabled)
    time_expr = "TIME_MS / 1000" if engine == "mariadb" else "TIME"
    where = ["ID <> CONNECTION_ID()"]
    if connection_id is None:
        where.append("COMMAND NOT IN (%s)" % ", ".join("'%s'" % c for c in IDLE_COMMANDS))
        if min_seconds:
            where.append("%s >= %s" % (time_expr, repr(float(min_seconds))))
    else:
        where.append("ID = %d" % connection_id)
    sql = ("SELECT ID, USER, HOST, DB, COMMAND, %s, STATE, INFO FROM %s WHERE %s "
           "ORDER BY %s DESC LIMIT 500" % (time_expr, source, " AND ".join(where), time_expr))
    try:
        rows = _rows(conn, sql)
    except ConnectorError as exc:
        if _denied(exc):
            raise ValueError("This user cannot read %s: %s" % (source, _reason(exc)))
        raise ValueError("Could not read the process list: " + _reason(exc))
    statements = []
    for row in rows:
        row = list(row) + [""] * (8 - len(row))
        command = _null(row[4]) or ""
        info = _null(row[7])
        seconds = _float(_null(row[5]))
        statements.append({
            "id": _int(row[0]), "user": _null(row[1]), "host": _null(row[2]),
            "db": _null(row[3]), "command": command,
            "time_s": round(seconds, 3) if seconds is not None else None,
            "state": _null(row[6]), "info": info,
            # Prepared statements (Execute) are refused by EXPLAIN FOR CONNECTION
            # (sql/opt_explain.cc:2505); only text-protocol queries qualify.
            "explainable": command == "Query" and bool(info),
        })
    return engine, version, ver, source, statements


def running_statements(conn, min_seconds=0):
    """Return statements executing now, longest-running first.

    Each item: id, user, host, db, command, time_s, state, info, explainable.
    Excludes this connection and idle/background threads. Read-only.
    """
    return running_report(conn, min_seconds)["statements"]


def running_report(conn, min_seconds=0):
    """The ``running-1.0`` JSON envelope shared by the CLI and the UI."""
    if isinstance(min_seconds, bool) or not isinstance(min_seconds, (int, float)) \
            or not 0 <= min_seconds <= 31536000 or min_seconds != min_seconds:
        raise ValueError("Minimum seconds must be a number from 0 to 31536000.")
    engine, version, _, source, statements = _running(conn, min_seconds)
    notes = []
    if source == "performance_schema.processlist" or engine == "mariadb":
        notes.append("Without the PROCESS privilege only your own user's sessions are listed.")
    return {"schema_version": "running-1.0", "engine": engine, "server_version": version,
            "source": source, "min_seconds": min_seconds, "notes": notes,
            "statements": statements}


def _explain_error(exc, connection_id, engine):
    text = _reason(exc)
    if "Unknown thread id" in text:
        return "Connection %d is not running (unknown thread id). It may have finished." % connection_id
    if "EXPLAIN FOR CONNECTION command is supported only" in text:
        return ("MySQL cannot explain connection %d's current statement. EXPLAIN FOR "
                "CONNECTION skips prepared statements, statements inside stored routines, "
                "and statements still being parsed." % connection_id)
    if "Target is not executing" in text:
        return "Connection %d is not executing a statement with a query plan right now." % connection_id
    if "Access denied" in text:
        if engine == "mariadb":
            return ("Showing another user's plan needs the PROCESS privilege: " + text)
        return ("Showing another user's plan needs every global privilege except GRANT "
                "OPTION on MySQL (PROCESS alone is not enough). Connect as that user or as "
                "an administrator: " + text)
    if "Lock wait timeout" in text:
        return "Connection %d did not answer the plan request within 30 seconds." % connection_id
    return "Could not get the plan for connection %d: %s" % (connection_id, text)


def explain_connection(conn, connection_id):
    """Return the current plan of a statement another connection is running.

    Result: {schema_version, raw, mode ('explain' | 'analyze-partial'), engine,
    server_version, connection_id, query, warnings, captured_at}. ``raw`` is
    the server's JSON, already validated with the single myflames parser.
    """
    if isinstance(connection_id, bool):
        raise ValueError("Choose a numeric connection ID.")
    try:
        connection_id = int(connection_id)
    except (TypeError, ValueError):
        raise ValueError("Choose a numeric connection ID.")
    if not 0 < connection_id < 2 ** 64:
        raise ValueError("Choose a positive connection ID.")
    engine, version, ver, _, rows = _running(conn, connection_id=connection_id)
    if not rows:
        raise ValueError(
            "Connection %d is not in the process list. It may have finished, it may be "
            "this tool's own session, or it belongs to another user and you lack the "
            "PROCESS privilege." % connection_id)
    query = rows[0]["info"] or ""
    if rows[0]["command"] == "Sleep" or not query:
        raise ValueError("Connection %d is idle; there is no running statement to explain." % connection_id)
    warnings = []
    if engine == "mariadb":
        if ver < (10, 9, 1):
            raise ValueError("Showing a running statement's JSON plan needs MariaDB 10.9 or "
                             "later (SHOW EXPLAIN FORMAT=JSON); this server is %s." % version)
        attempts = (("analyze-partial", "SHOW ANALYZE FORMAT=JSON FOR %d" % connection_id),
                    ("explain", "SHOW EXPLAIN FORMAT=JSON FOR %d" % connection_id))
    else:
        if ver < (8, 3, 0):
            raise ValueError("Showing a running statement's plan needs MySQL 8.3 or later, "
                             "where EXPLAIN FOR CONNECTION can emit the JSON format myflames "
                             "reads (explain_json_format_version=2); this server is %s." % version)
        attempts = (("explain", "SET SESSION explain_json_format_version=2; "
                                "EXPLAIN FORMAT=JSON FOR CONNECTION %d" % connection_id),)
    raw, mode, last_error = "", "", None
    for attempt_mode, sql in attempts:
        try:
            raw = conn.run(sql, extra_flags=["--raw"]).strip()
            mode = attempt_mode
            break
        except ConnectorError as exc:
            last_error = exc
            text = str(exc)
            # Only fall back from SHOW ANALYZE for errors SHOW EXPLAIN might not hit.
            if ("Unknown thread id" in text or "Target is not executing" in text
                    or "Access denied" in text or "PROCESS" in text):
                break
    if not mode:
        raise ValueError(_explain_error(last_error, connection_id, engine))
    if not raw:
        raise ValueError("Connection %d finished or went idle before its plan could be read." % connection_id)
    try:
        from .plan_input import _plan
        _plan({"plan": raw})
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError("The server returned a plan myflames cannot read: %s" % exc)
    if mode == "explain":
        warnings.append("Estimated plan: the optimizer's chosen plan for a running statement. "
                        "Times are not measured; capture the query with --mode analyze for timings.")
    else:
        warnings.append("Partial runtime statistics: SHOW ANALYZE reports counters collected so "
                        "far while the statement is still running, so totals are incomplete.")
    return {"schema_version": "explain-connection-1.0", "raw": raw, "mode": mode,
            "engine": engine, "server_version": version, "connection_id": connection_id,
            "query": query, "warnings": warnings,
            "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def explain_capture(result):
    """Shape an explain_connection result as capture-1.0 for the workspace."""
    partial = result["mode"] == "analyze-partial"
    return {
        "schema_version": "capture-1.0", "raw": result["raw"], "query": result["query"],
        "mode": "analyze" if partial else "explain", "engine": result["engine"],
        "server_version": result["server_version"], "captured_at": result["captured_at"],
        "conditions": "In-flight statement on connection %d (%s)." % (
            result["connection_id"],
            "partial SHOW ANALYZE" if partial else "estimated plan, EXPLAIN FOR CONNECTION"),
        "warnings": list(result["warnings"]), "runs": [],
        "schema": {}, "table_stats": {}, "variables": {}, "trace": None,
    }
