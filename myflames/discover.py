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
* A sample cut at that limit ends with "..." in place of its last three
  characters (format_sqltext, storage/perfschema/table_helper.cc).
* MySQL's digest table also has ``SUM_CPU_TIME``, ``QUANTILE_95``,
  ``QUANTILE_99`` and ``QUANTILE_999`` (table_esms_by_digest.cc); MariaDB's
  has none of them. Each quantile is the upper bound of the histogram bucket
  that holds it (table_esms_by_digest.cc, ``m_bucket_timer[index + 1]``).
  The columns are read defensively (a server that lacks one answers Unknown
  column and the read is sent again without it), never chosen by version
  number. ``SUM_CPU_TIME`` grows only while the ``events_statements_cpu``
  consumer is ENABLED (pfs.cc sets STATE_FLAG_CPU only then); the consumer
  is off by default (sql/sys_vars.cc, performance_schema_consumer_
  events_statements_cpu DEFAULT(false)).
* The latency histogram behind the quantiles,
  ``performance_schema.events_statements_histogram_by_digest``, keeps 450
  buckets per digest (pfs_histogram.h): bucket 0 below 10 microseconds, then
  bounds growing by 10^(1/50), the last one open-ended (pfs_histogram.cc).
  It is MySQL-only; only buckets with ``COUNT_BUCKET > 0`` are read.
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

from .connector import ConnectorError, MySQLConnection, unescape_mysql
from .live import capture_problem
# Compatibility exports for one release; these primitives no longer belong to discovery.
from .perf_common import SERVER_STATE_SQL, _denied, _reason, _server, _version_tuple, conn_identity, text_rows

ORDERS = {
    "total": "SUM_TIMER_WAIT",
    "time": "SUM_TIMER_WAIT",
    "avg": "AVG_TIMER_WAIT",
    "examined": "SUM_ROWS_EXAMINED",
    "rows": "SUM_ROWS_EXAMINED",
    "count": "COUNT_STAR",
    "exec_count": "COUNT_STAR",
    # Workload attribution (see the module docstring): the statements behind
    # Health's "Joins without an index" (Select_full_join), "Full scans of the
    # first table" (Select_scan), and executions that read a table without an
    # index. Only digests whose counter is above zero are listed.
    "full_join": "SUM_SELECT_FULL_JOIN",
    "scan": "SUM_SELECT_SCAN",
    "scans": "SUM_SELECT_SCAN",
    "no_index": "SUM_NO_INDEX_USED",
    "tmp_tables": "SUM_CREATED_TMP_DISK_TABLES",
    "locks": "SUM_LOCK_TIME",
}
#: The attribution orders list only digests whose counter is above zero.
#: SUM_SELECT_RANGE_CHECK (a later table read with a range check for each
#: row: no usable index when the plan was made) is listed with full joins.
ORDER_FILTERS = {
    "full_join": "(SUM_SELECT_FULL_JOIN > 0 OR SUM_SELECT_RANGE_CHECK > 0)",
    "scan": "SUM_SELECT_SCAN > 0",
    "scans": "SUM_SELECT_SCAN > 0",
    "no_index": "SUM_NO_INDEX_USED > 0",
    "tmp_tables": "SUM_CREATED_TMP_DISK_TABLES > 0",
    "locks": "SUM_LOCK_TIME > 0",
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
_CONSUMERS_SQL = ("SELECT NAME, ENABLED FROM performance_schema.setup_consumers "
                  "WHERE NAME IN ('global_instrumentation', 'statements_digest')")
_MAX_TEXT_SQL = "SELECT @@performance_schema_max_sql_text_length"
_OVERFLOW_SQL = ("SELECT COUNT_STAR, SUM_TIMER_WAIT FROM performance_schema.events_statements_summary_by_digest "
                 "WHERE DIGEST IS NULL")
_COVERAGE_SQL = "SHOW GLOBAL STATUS WHERE Variable_name IN ('Uptime', 'Performance_schema_digest_lost')"
#: The two best-effort reads behind the text_limits envelope field (never one
#: read per row): the sizing variables and the connection high-water mark in
#: one, the measured buffer bytes in the other.
_TEXT_LIMIT_VARS_SQL = (
    "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('max_digest_length', 'performance_schema_max_digest_length', "
    "'performance_schema_digests_size', 'performance_schema_events_statements_history_long_size', "
    "'performance_schema_events_statements_history_size', 'performance_schema_max_thread_instances', "
    "'performance_schema_max_statement_stack', 'max_connections')")
#: SHOW, not performance_schema.global_status: MariaDB has no such table
#: (ERROR 1146 on 11.8), and SHOW reads the same on MySQL 5.7 and later.
_TEXT_LIMIT_STATUS_SQL = "SHOW GLOBAL STATUS WHERE Variable_name = 'Max_used_connections'"
#: memory_summary events whose bytes scale with the SQL text length
#: (storage/perfschema/pfs_builtin_memory.cc). The digest sample instrument
#: (builtin_memory_digest_sample_sqltext) is declared but never registered,
#: so that part is computed from performance_schema_digests_size instead.
_SQL_TEXT_MEMORY = (
    "memory/performance_schema/events_statements_current.sql_text",
    "memory/performance_schema/events_statements_history.sql_text",
    "memory/performance_schema/events_statements_history_long.sql_text")
#: The token buffers behind stored digest text (pfs_builtin_memory.cc).
_DIGEST_TEXT_MEMORY = (
    "memory/performance_schema/events_statements_summary_by_digest.digest_text",
    "memory/performance_schema/events_statements_current.digest_text",
    "memory/performance_schema/events_statements_history.digest_text",
    "memory/performance_schema/events_statements_history_long.digest_text")
_TEXT_LIMIT_MEMORY_SQL = (
    "SELECT EVENT_NAME, CURRENT_NUMBER_OF_BYTES_USED "
    "FROM performance_schema.memory_summary_global_by_event_name WHERE EVENT_NAME IN (%s)"
    % ", ".join("'%s'" % name for name in _SQL_TEXT_MEMORY + _DIGEST_TEXT_MEMORY))
#: Raising a text length goes to the smallest of these steps above its
#: current value (the variables cap at 1048576: sql/sys_vars.cc).
_TEXT_LENGTH_STEPS = (4096, 8192, 16384, 32768, 65536)
#: What a list that ranks against the whole server (top_report with
#: server_context) reads in one more aggregate read: every digest row (the
#: overflow row included) with its total statement time and executions, the
#: oldest FIRST_SEEN and its age computed on the server (TIMESTAMPDIFF, so no
#: client clock or time zone is involved), and the events_statements_cpu
#: consumer (NULL where the server has none: MariaDB, MySQL before 8.0.28).
#: This scans allocated digest capacity, not just populated rows or top-N
#: (both engines: table_esms_by_digest::rnd_next loops to digest_max).
_CONTEXT_SQL = ("SELECT COUNT(*), SUM(SUM_TIMER_WAIT), SUM(COUNT_STAR), MIN(FIRST_SEEN), "
                "TIMESTAMPDIFF(SECOND, MIN(FIRST_SEEN), NOW()), "
                "(SELECT ENABLED FROM performance_schema.setup_consumers WHERE NAME = 'events_statements_cpu') "
                "FROM performance_schema.events_statements_summary_by_digest")
#: MySQL digest columns read with server_context; a server that lacks one
#: answers Unknown column and the read is sent again without it, so the
#: list is never chosen by version number (SUM_CPU_TIME arrived after the
#: quantiles). MariaDB has none of them.
_OPTIONAL_COLUMNS = ("SUM_CPU_TIME", "QUANTILE_95", "QUANTILE_99", "QUANTILE_999")
#: The New badge: first seen in the last 24 hours, decided on the server
#: (NOW()) so no client clock is involved. _mark_new gates it on the window.
_NEW_FLAG_SQL = "FIRST_SEEN > NOW() - INTERVAL 24 HOUR"
#: A statement first seen this recently is New, and the badge is shown only
#: when the counters cover more than this (otherwise every statement is new).
NEW_WITHIN_S = 86400
#: "Counting since" is noted when the oldest FIRST_SEEN began more than this
#: long after the server started.
COUNTING_GAP_S = 3600
#: What every digest list reads besides its digest rows, exactly as sent:
#: server state, the two consumers, the sample text limit, the overflow row
#: and the coverage counters. None depends on the list's order or filter, so
#: a caller that reads several lists in one pass (Full analysis attribution,
#: report._AttributionContext) may share one reply to each. The attribution
#: reads its lists without server_context, so _CONTEXT_SQL is not among them.
SHARED_METADATA_SQL = frozenset(DISCOVER_TAG + sql for sql in (
    SERVER_STATE_SQL, _CONSUMERS_SQL, _MAX_TEXT_SQL, _OVERFLOW_SQL, _COVERAGE_SQL))
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
        rows = _rows(conn, _CONSUMERS_SQL)
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


def _top(conn, limit=20, schema=None, order="total", table=None, extra_metrics=False,
         server_context=True, with_identity=False):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("Choose a limit from 1 to 200.")
    if order not in ORDERS:
        raise ValueError("Order by one of: " + ", ".join(ORDERS) + ".")
    if order in ("locks", "tmp_tables"):
        extra_metrics = True
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
    reading = {} if with_identity else None
    engine, version, has_sample, max_text, items, kept = _digest_rows(
        conn, where, tail, exclude=True, extra_metrics=extra_metrics, detail=server_context, reading=reading)
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
    overflow = _digest_overflow(conn, reading["overflow"] if with_identity else None)
    context = _server_context(conn) if server_context or with_identity else None
    coverage = _coverage(conn, items, overflow, context, reading["coverage"] if with_identity else None)
    if with_identity:
        # Independent of the one-hour presentation threshold and top-N.
        coverage["oldest_first_seen"] = context["first_seen"] if context else None
    if server_context:
        _mark_new(items, coverage["uptime_s"], context)
    totals = context["totals"] if context and server_context else None
    notes = ["Counters accumulate since the server started or the digest table was last truncated."]
    if order in _ORDER_NOTES and not (table is not None and order in ("no_index", "scan", "scans")):
        notes.append(_ORDER_NOTES[order])
    if not has_sample:
        notes.append("This server keeps no sample statement text; digest text shows ? where "
                     "literal values were, so fill those in before capturing.")
    elif any(s["sample_truncated"] for s in items):
        notes.append("Sample text was cut at %d bytes: raise "
                     "performance_schema_max_sql_text_length (now %d; needs a restart) to keep whole "
                     "sample statements." % (max_text, max_text))
    if any(_digest_text_cut(s.get("digest_text")) for s in items):
        notes.append("Digest text was cut at the digest length limit: raise max_digest_length and "
                     "performance_schema_max_digest_length together (both need a restart) to keep whole "
                     "normalized statements.")
    if coverage["counting_since"]:
        notes.append("Counting since %s: the digest table was reset or statements began after the "
                     "server started." % coverage["counting_since"][:19])
    if overflow:
        count, total_ms = overflow
        notes.append(
            "The digest table is full: %s more executions (%s) were summed into one overflow row "
            "without a digest, so they are not listed. Raise performance_schema_digests_size "
            "(needs a restart) or truncate the table to see them; Performance_schema_digest_lost "
            "counts them too." % ("{:,}".format(count), _ms_text(total_ms)))
    report = {"schema_version": "top-1.0", "engine": engine, "server_version": version,
              **conn_identity(conn, engine, version),
              "order": order, "limit": limit, "schema": schema, "notes": notes,
              "statements": items,
              "table": {"schema": table[0], "table": table[1]} if table else None,
              "totals": totals, "others": _others(totals, items),
              "cpu": _cpu_state(engine, kept, context) if server_context else None,
              "coverage": coverage,
              "text_limits": (_text_limits(conn, engine, version, max_text, items)
                              if server_context else None)}
    if with_identity:
        report["reading_server"] = reading["identity"]
        if reading["identity"].get("verified"):
            server = reading["identity"]["server"]
            report.update(flavor=server["flavor"], server_product=server["product"])
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
    "scans": ("Listed: statements whose first table was read with a full table or index scan (SUM_SELECT_SCAN, "
              "the per-statement form of Select_scan; MariaDB also counts a full index scan of any table here)."),
    "no_index": ("Listed: statements that read some table with a table scan (SUM_NO_INDEX_USED, once per "
                 "execution; MySQL also counts a range check, MariaDB a single-table UPDATE or DELETE without a "
                 "range)."),
    "tmp_tables": ("Listed: statements that created on-disk temporary tables (SUM_CREATED_TMP_DISK_TABLES > 0)."),
    "locks": ("Listed: statements with significant lock wait time (SUM_LOCK_TIME > 0)."),
}


def _coverage(conn, items, overflow, context=None, rows=None):
    """What the list covers: Uptime, the oldest FIRST_SEEN listed, the
    overflow row, and Performance_schema_digest_lost (best effort).

    ``counting_since`` is the oldest FIRST_SEEN of the whole digest table
    when it began more than COUNTING_GAP_S after the server started (the
    table was reset, or statements began later), else None. Both numbers
    come from the server: Uptime, and the age of that FIRST_SEEN from
    TIMESTAMPDIFF in *context* (:func:`_server_context`)."""
    seen = [item["first_seen"] for item in items if item.get("first_seen")]
    coverage = {"uptime_s": None, "first_seen": min(seen) if seen else None, "digest_lost": None,
                "counting_since": None,
                "overflow": {"executions": overflow[0], "total_ms": round(overflow[1], 3)} if overflow else None}
    if rows is None:
        try:
            rows = _rows(conn, _COVERAGE_SQL)
        except Exception:
            rows = []
    values = {row[0]: row[1] for row in rows if len(row) >= 2}
    if "Uptime" in values:
        coverage["uptime_s"] = _int(values["Uptime"])
    if "Performance_schema_digest_lost" in values:
        coverage["digest_lost"] = _int(values["Performance_schema_digest_lost"])
    if (context and context["age_s"] is not None and coverage["uptime_s"] is not None
            and coverage["uptime_s"] - context["age_s"] > COUNTING_GAP_S):
        coverage["counting_since"] = context["first_seen"]
    return coverage


_COUNT = re.compile(r"^-?\d+$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


CONTEXT_TIMEOUT = 3


def _server_context(conn):
    """The whole digest table in one aggregate read (:data:`_CONTEXT_SQL`):
    ``{"totals": {digests, total_ms, executions}, "first_seen", "age_s",
    "cpu_enabled"}``, or None when the read fails or its reply is not the one
    aggregate row (best effort: the list itself stands without it).
    ``cpu_enabled`` is None where the server has no events_statements_cpu
    consumer."""
    try:
        # This scans the allocated digest capacity. Keep its time budget and
        # failures separate from the required reading and its identity.
        text = conn.run(DISCOVER_TAG + _CONTEXT_SQL, extra_flags=['--comments', '--skip-reconnect'],
                        timeout=CONTEXT_TIMEOUT)
        rows = [[unescape_mysql(cell) for cell in line.split('\t')]
                for line in text.replace('\r\n', '\n').split('\n') if line]
    except Exception:
        return None
    return _context_from_rows(rows)


def _context_from_rows(rows):
    """Parse the one aggregate row, whether batched or read separately."""
    if len(rows) != 1 or len(rows[0]) != 6:
        return None
    count, wait, calls, first, age, cpu = [_null(cell) for cell in rows[0]]
    if count is None or not _COUNT.match(str(count)) or int(count) < 0:
        return None
    if any(cell is not None and not _COUNT.match(str(cell)) for cell in (wait, calls, age)):
        return None
    first = first if first and _TIMESTAMP.match(str(first)) else None
    age_s = int(age) if first and age is not None and int(age) >= 0 else None
    return {"totals": {"digests": int(count), "total_ms": round(int(wait or 0) / _PICO_PER_MS, 3),
                       "executions": int(calls or 0)},
            "first_seen": first, "age_s": age_s,
            "cpu_enabled": None if cpu is None else str(cpu).strip().upper() == "YES"}


def _mark_new(items, uptime_s, context):
    """``new``: first seen in the last NEW_WITHIN_S (the server's own
    ``new_24h`` flag), shown only while the counters cover more than that:
    the server is up longer and the oldest digest is older. Otherwise every
    statement would be new and the badge would say nothing."""
    age_s = context["age_s"] if context else None
    window = uptime_s is not None and uptime_s > NEW_WITHIN_S and (age_s is None or age_s > NEW_WITHIN_S)
    for item in items:
        flagged = item.pop("new_24h", False)
        item["new"] = bool(window and flagged)


def _others(totals, items):
    """The digests the list does not hold, for its closing "N other
    statements" row: ``{digests, total_ms, executions}``, or None when the
    list holds every digest or the whole-table totals were not read."""
    if not totals:
        return None
    digests = totals["digests"] - len(items)
    if digests <= 0:
        return None
    # The aggregate and the list are two reads: counters that grew between
    # them never make the remainder negative.
    total_ms = max(0.0, round(totals["total_ms"] - sum(item["total_ms"] for item in items), 3))
    executions = max(0, totals["executions"] - sum(item["count"] for item in items))
    return {"digests": digests, "total_ms": total_ms, "executions": executions}


def _cpu_state(engine, kept, context):
    """``{"collected": bool or None}`` when the digest rows carried
    SUM_CPU_TIME, else None (MariaDB, or a MySQL without the column).
    False: the events_statements_cpu consumer is off, so the column does
    not grow ("Not collected"); None: the consumer could not be read."""
    if engine != "mysql" or "SUM_CPU_TIME" not in kept:
        return None
    return {"collected": context["cpu_enabled"] if context else None}


def _digest_overflow(conn, rows=None):
    """``(executions, total_ms)`` summed into the digest table's overflow row.

    When events_statements_summary_by_digest is full, statements with a new
    digest are added to one row whose DIGEST is NULL (M/storage/perfschema/
    pfs_digest.cc:300-310, table_helper.cc:710-715). The main query skips that
    row, so report it. Best effort: any error or unexpected shape is ignored.
    """
    if rows is None:
        try:
            rows = _rows(conn, _OVERFLOW_SQL)
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


def _sample_cut(sample, max_text):
    """Whether a QUERY_SAMPLE_TEXT was cut at performance_schema_max_sql_text_length.

    MySQL replaces the last three characters of a cut sample with "..."
    (format_sqltext, storage/perfschema/table_helper.cc), after trimming it
    to whole characters, so a cut sample ends with "..." a few bytes short of
    the limit; a sample at the limit counts too."""
    if not sample or not max_text:
        return False
    size = len(sample.encode("utf-8"))
    return size >= max_text or (sample.endswith("...") and size >= max_text - 12)


def _digest_text_cut(digest_text):
    """True when the digest text itself was cut: the parser stops recording
    tokens at max_digest_length (and Performance Schema stores at most
    performance_schema_max_digest_length) and appends "..."
    (M/sql/sql_digest.cc:333)."""
    text = (digest_text or "").rstrip()
    return text.endswith("...")


def _suggest_text_length(value):
    """The smallest step of _TEXT_LENGTH_STEPS above the current value."""
    for step in _TEXT_LENGTH_STEPS:
        if step > value:
            return step
    return None


def _read_error_text(exc):
    if isinstance(exc, ConnectorError):
        return _reason(exc)
    return str(exc) or exc.__class__.__name__


def _text_limit_reads(conn):
    """(variables, variables_reason, memory, memory_reason) for the
    text-limit estimates, best effort: a failed read leaves empty values and
    a reason, never an error."""
    variables, variables_reason = {}, None
    for sql in (_TEXT_LIMIT_VARS_SQL, _TEXT_LIMIT_STATUS_SQL):
        try:
            for row in _rows(conn, sql):
                if len(row) >= 2 and row[0] is not None:
                    variables[str(row[0])] = _int(_null(row[1]))
        except Exception as exc:
            variables_reason = variables_reason or _read_error_text(exc)
    memory, memory_reason = None, None
    try:
        memory = {}
        for row in _rows(conn, _TEXT_LIMIT_MEMORY_SQL):
            if len(row) >= 2 and row[0] in _SQL_TEXT_MEMORY + _DIGEST_TEXT_MEMORY:
                memory[row[0]] = max(0, _int(_null(row[1])))
    except Exception as exc:
        memory, memory_reason = None, _read_error_text(exc)
    return variables, variables_reason, memory, memory_reason


def _thread_rows(variables):
    """The threads the per-thread history and current buffers allocate for:
    the configured instance cap, else the connection high-water mark, else
    max_connections."""
    for name in ("performance_schema_max_thread_instances", "Max_used_connections", "max_connections"):
        value = variables.get(name)
        if value is not None and value > 0:
            return value
    return None


def _stack(variables):
    """The current-statement table keeps one text buffer per statement stack
    level per thread (performance_schema_max_statement_stack, default 10;
    M/storage/perfschema/pfs_buffer_container.cc:666-676)."""
    value = variables.get("performance_schema_max_statement_stack")
    return value if value is not None and value > 0 else 10


def _connections(variables):
    """(count, source) for per-connection buffers: the high-water mark when
    known, else the configured cap."""
    for name in ("Max_used_connections", "max_connections"):
        value = variables.get(name)
        if value is not None and value > 0:
            return value, name
    return None, None


def _sql_text_estimate(old, new, variables, memory, memory_reason):
    """(bytes, basis) of the Performance Schema memory added by raising
    performance_schema_max_sql_text_length from old to new: the measured
    buffer bytes scaled by new/old plus the digest samples (digests_size x
    (new - old), whose instrument is never registered), or the same from the
    sizing variables. (None, reason) when neither can be computed."""
    digests_size = variables.get("performance_schema_digests_size")
    samples = digests_size * (new - old) if digests_size is not None and digests_size > 0 else None
    if memory is not None and all(name in memory for name in _SQL_TEXT_MEMORY) and samples is not None:
        measured = sum(memory[name] for name in _SQL_TEXT_MEMORY)
        return int(measured * (new / float(old) - 1)) + samples, "measured"
    long_size = variables.get("performance_schema_events_statements_history_long_size")
    history_size = variables.get("performance_schema_events_statements_history_size")
    threads = _thread_rows(variables)
    if (samples is None or long_size is None or long_size < 0
            or history_size is None or history_size < 0 or threads is None):
        return None, "no estimate: " + (memory_reason or "the sizing variables could not be read")
    stored = (long_size + threads * (history_size + _stack(variables))) * (new - old)
    return stored + samples, "formula"


def _digest_text_estimate(old, new, values, variables, memory, memory_reason):
    """(bytes, basis) of raising the smaller digest length limit to *new*,
    each parameter keeping its value when already larger: the stored digest
    text buffers grow with performance_schema_max_digest_length alone, and
    each connection's parser buffer with max_digest_length
    (M/sql/sql_class.cc:894-896)."""
    connections, source = _connections(variables)
    mdl = values.get("max_digest_length")
    stored_size = values.get("performance_schema_max_digest_length")
    per_connection = None
    if connections is not None and mdl is not None and mdl > 0:
        per_connection = connections * max(0, max(mdl, new) - mdl)
    stored_new = max(stored_size, new) if stored_size is not None and stored_size > 0 else None
    if memory is not None and all(name in memory for name in _DIGEST_TEXT_MEMORY) and stored_new is not None:
        if per_connection is None:
            return None, "no estimate: the connection count could not be read"
        measured = sum(memory[name] for name in _DIGEST_TEXT_MEMORY)
        return (int(measured * (stored_new / float(stored_size) - 1)) + per_connection,
                "measured, plus per-connection buffers (%s)" % source)
    digests_size = variables.get("performance_schema_digests_size")
    long_size = variables.get("performance_schema_events_statements_history_long_size")
    history_size = variables.get("performance_schema_events_statements_history_size")
    threads = _thread_rows(variables)
    if (per_connection is None or stored_new is None
            or digests_size is None or digests_size <= 0
            or long_size is None or long_size < 0
            or history_size is None or history_size < 0 or threads is None):
        return None, "no estimate: " + (memory_reason or "the sizing variables could not be read")
    stored = (digests_size + long_size + threads * (history_size + _stack(variables))) * (stored_new - stored_size)
    return stored + per_connection, "formula, plus per-connection buffers (%s)" % source


def _sql_text_limit(max_text, cut_rows, variables, memory, memory_reason):
    """The sql_text part of text_limits (the sample text cap)."""
    entry = {"name": "performance_schema_max_sql_text_length", "value": max_text,
             "cut_rows": cut_rows, "suggested": _suggest_text_length(max_text),
             "estimate_bytes": None, "estimate_basis": None}
    if entry["suggested"] is None:
        entry["estimate_basis"] = "no change suggested"
    else:
        entry["estimate_bytes"], entry["estimate_basis"] = _sql_text_estimate(
            max_text, entry["suggested"], variables, memory, memory_reason)
    return entry


def _digest_text_limit(cut_rows, variables, variables_reason, memory, memory_reason):
    """The digest part of text_limits: digest text is limited by the smaller
    of max_digest_length and performance_schema_max_digest_length, so both
    are raised together."""
    names = ["max_digest_length", "performance_schema_max_digest_length"]
    values = {name: variables.get(name) for name in names}
    known = [value for value in values.values() if value is not None and value > 0]
    current = min(known) if known else None
    suggested = _suggest_text_length(current) if current is not None else None
    # Raise only what is below the suggestion: a larger value is kept.
    targets = ({name: max(value or 0, suggested) for name, value in values.items()}
               if suggested is not None else None)
    entry = {"names": names, "values": values, "cut_rows": cut_rows, "suggested": suggested,
             "suggested_values": targets, "estimate_bytes": None, "estimate_basis": None}
    if suggested is None:
        entry["estimate_basis"] = ("no change suggested" if current is not None else
                                   "no estimate: the digest length variables could not be read"
                                   + (" (%s)" % variables_reason if variables_reason else ""))
    else:
        entry["estimate_bytes"], entry["estimate_basis"] = _digest_text_estimate(
            current, suggested, values, variables, memory, memory_reason)
    return entry


def _text_limits(conn, engine, version, max_text, items):
    """The text_limits field of top-1.0: which startup parameter cut the
    sample or digest text on screen, its current value, a suggested value,
    the memory the raise costs and whether SET PERSIST_ONLY can stage it
    (MySQL 8.0+). None when nothing listed was cut. Two reads at most, both
    best effort; never one read per row."""
    cut_samples = sum(1 for s in items if s.get("sample_truncated"))
    cut_digests = sum(1 for s in items if _digest_text_cut(s.get("digest_text")))
    if not (cut_samples or cut_digests):
        return None
    variables, variables_reason, memory, memory_reason = _text_limit_reads(conn)
    limits = {"sql_text": None, "digest": None,
              "persist_only": engine == "mysql" and _version_tuple(version) >= (8, 0)}
    if cut_samples:
        limits["sql_text"] = _sql_text_limit(max_text, cut_samples, variables, memory, memory_reason)
    if cut_digests:
        limits["digest"] = _digest_text_limit(cut_digests, variables, variables_reason,
                                              memory, memory_reason)
    return limits


def _digest_rows(conn, where, tail, exclude=False, extra_metrics=False, detail=False, reading=None):
    """Read digest rows: (engine, version, has_sample, max_text, items, kept).

    *where* adds conditions to the shared ones; *tail* is the ORDER BY/LIMIT.
    With *exclude*, myflames' own captures are left out (see _excluded).
    With *detail* (top_report's server_context), each item also gets
    ``cpu_ms``, ``p95_ms``, ``p99_ms`` and ``p999_ms`` from the optional
    MySQL columns (:data:`_OPTIONAL_COLUMNS`, None when absent) and the
    server's ``new_24h`` flag; *kept* names the optional columns the server
    accepted. Without *detail* the statement is exactly what it always was.
    An optional *reading* dict receives the identity and metadata from the
    successful digest batch, keeping the return tuple unchanged for callers.
    """
    engine, version, ver, enabled = _server(conn)
    if not enabled:
        if reading is not None:
            default = " (MariaDB's default)" if engine == "mariadb" else ""
            raise ValueError("Performance Schema is off%s; open Server > Instruments to see what Query digest needs."
                             % default)
        raise ValueError(_perfschema_off_message(engine))
    _check_digest_consumer(conn)
    has_sample = engine == "mysql" and ver >= (8, 0, 3)
    max_text = 0
    if has_sample:
        try:
            rows = _rows(conn, _MAX_TEXT_SQL)
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
    # replayed answers) keep their column positions. With *detail*, the
    # optional MySQL columns and the New flag follow them.
    if extra_metrics:
        columns += ", SUM_SELECT_FULL_JOIN, SUM_SELECT_RANGE_CHECK, SUM_SELECT_SCAN, SUM_LOCK_TIME, SUM_CREATED_TMP_TABLES"
    else:
        columns += ", SUM_SELECT_FULL_JOIN, SUM_SELECT_RANGE_CHECK, SUM_SELECT_SCAN"
    extra_at = 16 if has_sample else 15
    conditions.extend(where)
    kept = list(_OPTIONAL_COLUMNS) if detail and engine == "mysql" else []

    def digest_sql():
        selected = columns + "".join(", " + c for c in kept) + (", " + _NEW_FLAG_SQL if detail else "")
        return ("SELECT %s FROM performance_schema.events_statements_summary_by_digest WHERE %s %s"
                % (selected, " AND ".join(conditions), tail))

    while True:
        try:
            if reading is not None:
                from .baselines import read_with_identity
                groups, identity = read_with_identity(
                    conn, [digest_sql(), _COVERAGE_SQL, _OVERFLOW_SQL], DISCOVER_TAG)
                rows, coverage_rows, overflow_rows = groups
                if identity.get("verified") and (identity["server"]["engine"] != engine
                                                 or identity["server"]["version"] != version):
                    raise ValueError("The server changed while reading statement digests. Refresh to read again.")
                reading.update(identity=identity, coverage=coverage_rows, overflow=overflow_rows)
            else:
                rows = _rows(conn, digest_sql())
            break
        except ConnectorError as exc:
            missing = re.search(r"Unknown column '([^']+)'", str(exc))
            if missing and missing.group(1).upper() in kept:
                kept.remove(missing.group(1).upper())
                continue
            if _denied(exc):
                raise ValueError(
                    "This user cannot read performance_schema.events_statements_summary_by_digest. "
                    "Grant it with: GRANT SELECT ON performance_schema.* TO <user>;")
            raise ValueError("Could not read statement digests: " + _reason(exc))
    items = []
    # After the attribution counters: the kept optional columns, then the flag.
    optional_at = extra_at + (5 if extra_metrics else 3)
    new_at = optional_at + len(kept)
    pad_len = new_at + 1 if detail else optional_at
    for row in rows:
        row = list(row) + [""] * (pad_len - len(row))
        digest_text = _null(row[2])
        sample = _null(row[15]) if has_sample else None
        if exclude and _excluded(digest_text, sample):
            continue
        lock_ms = 0.0
        tmp_tbls = 0
        if extra_metrics and len(row) > extra_at + 3 and row[extra_at + 3] != "":
            lock_ms = round(_int(row[extra_at + 3]) / _PICO_PER_MS, 3)
        if extra_metrics and len(row) > extra_at + 4 and row[extra_at + 4] != "":
            tmp_tbls = _int(row[extra_at + 4])
        item = {
            "digest": _null(row[1]),
            "digest_text": digest_text,
            "schema": _null(row[0]),
            "count": _int(row[3]),
            "total_ms": round(_int(row[4]) / _PICO_PER_MS, 3),
            "avg_ms": round(_int(row[5]) / _PICO_PER_MS, 3),
            "max_ms": round(_int(row[6]) / _PICO_PER_MS, 3),
            "lock_time_ms": lock_ms,
            "rows_examined": _int(row[7]),
            "rows_sent": _int(row[8]),
            "examined_per_sent": None,
            "no_index_used": _int(row[9]),
            "no_good_index_used": _int(row[10]),
            "tmp_disk_tables": _int(row[11]),
            "tmp_tables": tmp_tbls,
            "sort_merge_passes": _int(row[12]),
            "select_full_join": _int(row[extra_at]),
            "select_range_check": _int(row[extra_at + 1]),
            "select_scan": _int(row[extra_at + 2]),
            "sample_text": sample,
            "sample_truncated": _sample_cut(sample, max_text),
            "first_seen": _null(row[13]),
            "last_seen": _null(row[14]),
        }
        if detail:
            for name, key in (("SUM_CPU_TIME", "cpu_ms"), ("QUANTILE_95", "p95_ms"),
                              ("QUANTILE_99", "p99_ms"), ("QUANTILE_999", "p999_ms")):
                cell = _null(row[optional_at + kept.index(name)]) if name in kept else None
                item[key] = round(_int(cell) / _PICO_PER_MS, 3) if cell not in (None, "") else None
            item["new_24h"] = _null(row[new_at]) == "1"
        if item["rows_sent"]:
            item["examined_per_sent"] = round(item["rows_examined"] / float(item["rows_sent"]), 2)
        item["flags"] = _statement_flags(item)
        from .analysis_query import prepare
        projection = prepare(digest_query(item)["sql"])
        item["capture_problem"] = projection["problem"] or None
        item["analysis_sql"] = projection["sql"] if projection["converted"] else None
        item["analysis_warning"] = projection["warning"]
        from .index_hint import no_predicate
        hint = no_predicate(item)
        if hint:
            item["index_hint"] = hint
        items.append(item)
    return engine, version, has_sample, max_text, items, kept


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


def digest_capture_problem(query):
    """Why capture refuses a :func:`digest_query` result, or None.

    The check reads ``query["sql"]``, not ``query["text"]``: digest text
    writes every keyword token with a trailing space (MariaDB
    sql/gen_lex_token.cc:81,103 sets m_append_space; MySQL
    sql/gen_lex_token.cc:181), so a built-in aggregate prints as ``SUM (``.
    A stored or user-defined function name is an identifier token, which
    sql_digest.cc prints quoted (`name`, MariaDB sql_digest.cc:256-262),
    so an unquoted ``SUM (`` in digest text can only be the built-in.
    :func:`join_function_calls` therefore restores ``SUM(`` for statements
    that came from digest text; raw SQL keeps the strict dbops check.
    """
    return capture_problem(query["sql"])


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
    engine, version, _, _, items, _ = _digest_rows(
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


def top_report(conn, limit=20, schema=None, order="total", table=None, extra_metrics=False,
               server_context=True, with_identity=False):
    """The ``top-1.0`` JSON envelope shared by the CLI and the UI.

    *table* (``"schema.table"``, a pair, or ``{"schema", "table"}``) keeps
    only digests that read a table without an index and whose text names
    that table; each gets ``table_match`` (see :func:`table_reference`).

    With *server_context* (the default) the list is ranked against the whole
    server in one more aggregate read: ``totals`` (every digest),
    ``others`` (the digests the list does not hold, or None),
    ``coverage.counting_since``, ``cpu`` (``{"collected"}`` on MySQL with
    SUM_CPU_TIME, else None) and, per statement, ``cpu_ms``, ``p95_ms``,
    ``p99_ms``, ``p999_ms`` (None where the server has no such column) and
    ``new``. The Full analysis attribution reads its lists without it.

    With *with_identity*, the digest rows, coverage, overflow and server
    identity share one client invocation. The optional context aggregate is
    a separate best-effort read with its own three-second timeout. Adds
    ``reading_server`` and ``coverage.oldest_first_seen`` (the whole table's
    MIN(FIRST_SEEN), independent of the counting_since display threshold).
    Without the flag the report shape and statements stay unchanged.
    """
    return _top(conn, limit, schema, order, table, extra_metrics=extra_metrics,
                server_context=server_context, with_identity=with_identity)


#: Buckets of one digest with at least one call, in bucket order.
_HISTOGRAM_SQL = ("SELECT BUCKET_NUMBER, BUCKET_TIMER_LOW, BUCKET_TIMER_HIGH, COUNT_BUCKET "
                  "FROM performance_schema.events_statements_histogram_by_digest "
                  "WHERE %s AND DIGEST = '%s' AND COUNT_BUCKET > 0 ORDER BY BUCKET_NUMBER")
#: The last bucket's upper bound is UINT64_MAX picoseconds (pfs_histogram.cc):
#: no bound at all, sent as null.
_OPEN_BUCKET_PS = 2 ** 63


DIGEST_TABLE_LIMIT = 20
DIGEST_INDEX_PART_LIMIT = 4096


def check_digest_tables(schema, text):
    """Validate bounded statement text before opening a connection."""
    from .perf_common import check_schema
    schema = check_schema(schema)
    if not isinstance(text, str) or len(text) > 1000000 or "\x00" in text:
        raise ValueError("Pass statement text of at most 1,000,000 characters without NUL.")
    return schema


def digest_tables(conn, schema, text):
    """Metadata for at most 20 names in statement text, never row data.

    Exactly two scoped metadata reads at most, with no engine preflight or
    schema inventory. Like schema_browser.indexes_of, named STATISTICS rows
    read engine-specific columns without a probe or retry: IS_VISIBLE and
    EXPRESSION on MySQL, absent on MariaDB. Unknown visibility stays null.
    """
    from .collectors import extract_table_references
    from .perf_common import Reader
    schema = check_digest_tables(schema, text)
    pairs, notes = extract_table_references(text, schema, limit=DIGEST_TABLE_LIMIT)
    tables = [{"schema": db, "name": name, "found": False, "type": None, "engine": None,
               "rows_estimate": None, "data_bytes": None, "index_bytes": None, "indexes": []}
              for db, name in pairs]
    result = {"schema_version": "digest-tables-1.0", "method": "statement text", "tables": tables, "notes": notes}
    lookup = {(t["schema"], t["name"]): t for t in tables if t["schema"] is not None
              and len(t["schema"]) <= 64 and len(t["name"]) <= 64}
    for table in tables:
        if table["schema"] is None:
            notes.append("No default schema was recorded for %s; its metadata was not read." % table["name"])
        elif (table["schema"], table["name"]) not in lookup:
            notes.append("Invalid table name %s.%s; its metadata was not read." % (table["schema"], table["name"]))
    if not lookup:
        return result
    # Each OR branch fixes BOTH fields. Never expand to every table in a
    # schema (the Tables/Indexes schema-size consent still belongs there).
    where = " OR ".join(
        "(TABLE_SCHEMA = %s AND TABLE_NAME = %s AND (@@lower_case_table_names <> 0 OR "
        "(BINARY TABLE_SCHEMA = BINARY %s AND BINARY TABLE_NAME = BINARY %s)))"
        % (_literal(db), _literal(name), _literal(db), _literal(name)) for db, name in lookup)
    reader = Reader(conn, "/*myflames:monitor*/ ")
    try:
        metadata = reader.named("SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH "
                                ", @@lower_case_table_names AS MYFLAMES_LOWER_CASE_TABLE_NAMES "
                                "FROM information_schema.TABLES WHERE %s LIMIT %d" % (where, DIGEST_TABLE_LIMIT + 1))
        indexes = reader.named("SELECT * FROM information_schema.STATISTICS WHERE %s "
                               "ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME = 'PRIMARY' DESC, INDEX_NAME, SEQ_IN_INDEX "
                               "LIMIT %d" % (where, DIGEST_INDEX_PART_LIMIT + 1))
    except ConnectorError as exc:
        raise ValueError("Could not read statement table metadata: " + _reason(exc))

    fold_names = any(str(row.get("MYFLAMES_LOWER_CASE_TABLE_NAMES")) in ("1", "2") for row in metadata)

    def targets(row):
        key = (row.get("TABLE_SCHEMA"), row.get("TABLE_NAME"))
        if not fold_names:
            return [lookup[key]] if key in lookup else []
        folded = tuple((part or "").casefold() for part in key)
        return [value for pair, value in lookup.items() if tuple(p.casefold() for p in pair) == folded]

    def size(value):
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None

    for row in metadata:
        kind = (row.get("TABLE_TYPE") or "").upper()
        for table in targets(row):
            table.update(found=True, type={"VIEW": "view", "SYSTEM VIEW": "system view", "SEQUENCE": "sequence"}.get(kind, "table"),
                         engine=row.get("ENGINE"), rows_estimate=size(row.get("TABLE_ROWS")),
                         data_bytes=size(row.get("DATA_LENGTH")), index_bytes=size(row.get("INDEX_LENGTH")))
    if len(metadata) > DIGEST_TABLE_LIMIT:
        notes.append("Table metadata reached the 20-table result limit; some matches may be missing.")
    if len(indexes) > DIGEST_INDEX_PART_LIMIT:
        notes.append("Index metadata was limited to 4,096 key parts; the index list may be incomplete.")
    grouped = {}
    for row in indexes[:DIGEST_INDEX_PART_LIMIT]:
        name = row.get("INDEX_NAME")
        if name is None:
            continue
        for table in targets(row):
            if not table["found"]:
                continue
            key = (table["schema"], table["name"], name)
            if key not in grouped:
                visible = row.get("IS_VISIBLE")
                item = {"name": name, "unique": str(row.get("NON_UNIQUE")) == "0", "columns": [],
                        "visible": None if visible is None else str(visible).upper() == "YES"}
                grouped[key] = item
                table["indexes"].append(item)
            # Functional parts have NULL COLUMN_NAME on MySQL.
            column = row.get("COLUMN_NAME") or row.get("EXPRESSION") or "(expression unavailable)"
            grouped[key]["columns"].append(column)
    for table in tables:
        if table["schema"] is not None and (table["schema"], table["name"]) in lookup and not table["found"]:
            notes.append("%s.%s was not found or is not visible to this account." % (table["schema"], table["name"]))
    return result


def digest_histogram(conn, schema, digest):
    """The latency histogram of one digest (``digest-histogram-1.0``).

    *schema* is the digest's default schema (None for a row recorded without
    one) and *digest* its full hex digest; both come from a ``top-1.0``
    statement. Each bucket that holds a call carries its bounds in
    milliseconds (``high_ms`` null for the open-ended last bucket) and its
    count, cumulative since the digest was first seen. MariaDB keeps no
    histogram, and a MySQL without the table answers ``available`` False with
    the reason, so the workspace omits the section.
    """
    if not isinstance(digest, str) or not re.match(r"^[0-9a-fA-F]{32,64}$", digest):
        raise ValueError("Pass a full statement digest: 32 to 64 hexadecimal characters.")
    if schema is not None and (not isinstance(schema, str) or not schema or len(schema) > 64
                               or "\x00" in schema):
        raise ValueError("Schema names are 1 to 64 characters.")
    engine, version, _, enabled = _server(conn)
    base = {"schema_version": "digest-histogram-1.0", "engine": engine, "server_version": version,
            **conn_identity(conn, engine, version),
            "schema": schema, "digest": digest.lower(), "available": False, "reason": None,
            "buckets": []}
    if engine == "mariadb":
        base["reason"] = "MariaDB keeps no statement latency histogram."
        return base
    if not enabled:
        raise ValueError(_perfschema_off_message(engine))
    where = "SCHEMA_NAME IS NULL" if schema is None else "SCHEMA_NAME = " + _literal(schema)
    try:
        rows = _rows(conn, _HISTOGRAM_SQL % (where, digest.lower()))
    except ConnectorError as exc:
        if _denied(exc):
            raise ValueError(
                "This user cannot read performance_schema.events_statements_histogram_by_digest. "
                "Grant it with: GRANT SELECT ON performance_schema.* TO <user>;")
        if "doesn't exist" in str(exc):
            base["reason"] = "This server keeps no statement latency histogram."
            return base
        raise ValueError("Could not read the statement latency histogram: " + _reason(exc))
    for row in rows:
        if len(row) < 4:
            continue
        high = _int(_null(row[2]))
        base["buckets"].append({
            "bucket": _int(row[0]),
            "low_ms": round(_int(_null(row[1])) / _PICO_PER_MS, 6),
            "high_ms": None if high >= _OPEN_BUCKET_PS else round(high / _PICO_PER_MS, 6),
            "count": _int(_null(row[3])),
        })
    base["available"] = True
    return base


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
            **conn_identity(conn, engine, version),
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
