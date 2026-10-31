"""Slow query logs: parse a log file, fingerprint statements, aggregate them.

Python 3.7+, standard library only. Nothing here connects to a server: the
file reader (:func:`read_file`) and the mysql.slow_log table reader
(slow_queries.py) feed the same :class:`Aggregator`, which builds the
``slow-log-1.0`` report Query digest and ``myflames slow-log`` show.

The format, as the servers write it (paths relative to the mysql-server and
mariadb-server checkouts):

* MySQL 5.7 to 9.x, ``File_query_log::write_slow`` (sql/log.cc): ``# Time:``
  in ISO 8601 with microseconds, ``Z`` under log_timestamps=UTC (the
  default) or ``+hh:mm`` under SYSTEM (the offset comes from localtime_r,
  sql/server_component/mysql_timestamp_imp.cc); the time is ``current_utime``,
  taken when the statement is logged, after it ended
  (Query_logger::slow_log_write). Then ``# User@Host: priv[user] @ host [ip]
  Id: N``, ``# Query_time: s  Lock_time: s Rows_sent: n  Rows_examined: n``
  (log_slow_extra, 8.0.14+, adds Thread_id, Errno, Killed, Bytes_received,
  Bytes_sent, the Read_* handler counts, Sort_*, Created_tmp_disk_tables,
  Created_tmp_tables, from 9.1 Count_hit_tmp_table_size, then Start and End
  on the same line), ``use db;`` only when the database differs from the
  last one written to the file (one buffer for the whole log, cleared when
  the file is reopened, and nothing for a session without a database), ``SET
  [last_insert_id=n,][insert_id=n,]timestamp=n;`` (the statement's start
  from 8.0.14 on, the logging time in 5.7 and 8.0.11 to 8.0.13), ``# administrator
  command: `` before a command name, then the statement and ``;``. A file
  opened by the server starts with ``<path>, Version: <version> (<comment>).
  started with:`` and two more lines (``Tcp port: …`` and ``Time Id Command
  Argument``); FLUSH LOGS and restarts write them again in the middle.
* MariaDB (``MYSQL_QUERY_LOG::write``, sql/log.cc): ``# Time: YYMMDD
  H:MM:SS`` (the hour padded with a space) in local time, written only when
  the second differs from the last one written, and taken from the
  statement's start (LOGGER::slow_log_print); ``# User@Host:`` without Id;
  ``# Thread_id: n  Schema: db  QC_hit: No``; the Query_time line;
  ``# Rows_affected: n  Bytes_sent: n``; with log_slow_verbosity, engine,
  tmp table and plan lines (``# Full_scan: Yes  Full_join: No …``, printed
  only when one of them is Yes and only for sessions with query_plan in
  their log_slow_verbosity, a session variable), ``# explain:`` lines and
  warnings;
  ``use `db`;`` with backquotes.
* Percona Server writes MySQL's lines plus its own (``# Schema: db
  Last_errno: n  Killed: n``, ``# Bytes_sent``, ``# QC_Hit: …  Full_scan: …``,
  ``#   InnoDB_…``, ``# Log_slow_rate_type``); every ``# Key: value`` pair is
  read the same way, whichever line holds it.
* The mysql.slow_log table's start_time is that same ``current_utime`` on
  MySQL (Log_to_csv_event_handler::log_slow), the start on MariaDB, with
  microseconds on both.

Times are kept as the log writes them and never moved to another zone: a
range compares the digits as written (``clock`` in the report says which
clock that is). Fingerprints are myflames' own normalization: comments
dropped, literals and numbers as ``?``, lists of values (IN, VALUES, rows of
a multi-row INSERT) as ``(...)``, white space collapsed, keywords in upper
case, names kept as written.
"""
import collections
import datetime
import hashlib
import math
import os
import re

SCHEMA_VERSION = "slow-log-1.0"
#: The largest file read (bytes).
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024
#: Longer lines are cut at this many bytes (the rest of the line is skipped).
MAX_LINE_BYTES = 1024 * 1024
#: Statement text kept per entry (characters); the rest is cut.
MAX_TEXT_CHARS = 1024 * 1024
#: Sample text kept per statement (characters).
SAMPLE_CHARS = 64 * 1024
#: Distinct statements (database and fingerprint) kept; later ones are summed apart.
MAX_FINGERPRINTS = 50000
#: Statements past MAX_FINGERPRINTS are told apart by a hash, up to this many;
#: past it the count says "at least" (memory stays bounded).
MAX_OVERFLOW_KEYS = 200000
#: Entries held while they wait for the log's first # Time line (count, characters).
MAX_WAITING = 100000
MAX_WAITING_CHARS = 64 * 1024 * 1024
#: Problems listed by line; all are counted.
MAX_PROBLEMS = 100
#: Users and databases listed per report (the busiest first).
MAX_NAMES = 1000
#: Users and hosts listed per statement.
MAX_STATEMENT_NAMES = 10
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
#: Rankings: the report key each one sorts on, largest first.
ORDERS = collections.OrderedDict([
    ("total", "total_ms"), ("count", "count"), ("avg", "avg_ms"), ("max", "max_ms"), ("p95", "p95_ms"),
    ("lock", "lock_time_ms"), ("examined", "rows_examined"), ("sent", "rows_sent"),
])
#: Statements myflames sends carry this tag; they are left out and counted.
OWN_TAG = "/*myflames:"

# Performance Schema's histogram (storage/perfschema/pfs_histogram.cc):
# bucket 0 below 10 microseconds, then bounds growing by 10^(1/50), 450
# buckets, the last one open-ended.
BUCKETS = 450
_BUCKET_BASE_MS = 0.01


def bucket_low(index):
    return 0.0 if index < 1 else round(_BUCKET_BASE_MS * 10 ** ((index - 1) / 50.0), 6)


def bucket_high(index):
    return None if index >= BUCKETS - 1 else round(_BUCKET_BASE_MS * 10 ** (index / 50.0), 6)


def bucket_of(ms):
    """The histogram bucket of a time in milliseconds."""
    if ms < _BUCKET_BASE_MS:
        return 0
    index = int(math.floor(50 * math.log10(ms / _BUCKET_BASE_MS))) + 1
    # Rounding of log10 can land one bucket off at an exact bound.
    if index > 1 and ms < bucket_low(index):
        index -= 1
    elif index < BUCKETS - 1 and bucket_high(index) is not None and ms >= bucket_high(index):
        index += 1
    return max(1, min(BUCKETS - 1, index))


def quantile(histogram, count, numerator, denominator):
    """The upper bound of the bucket that holds the quantile, as Performance
    Schema computes QUANTILE_95 (table_esms_by_digest.cc): the first bucket
    whose running count reaches ceil(count * q)."""
    if not count:
        return None
    target = (count * numerator + denominator - 1) // denominator
    running = 0
    for index in sorted(histogram):
        running += histogram[index]
        if running >= target:
            return bucket_high(index)
    return None


# ---- times -------------------------------------------------------------------------------------

_ISO_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|[+-]\d{2}:\d{2})?$")
_OLD_TIME = re.compile(r"^(\d{2})(\d{2})(\d{2})\s+(\d{1,2}):(\d{2}):(\d{2})$")
_BOUND = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?$")


def _valid(year, month, day, hour, minute, second):
    try:
        datetime.datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    return True


def parse_log_time(text):
    """``(time, zone)`` from a ``# Time:`` value, or None when unreadable.

    *time* is ``YYYY-MM-DD HH:MM:SS[.ffffff]`` with the digits as written;
    *zone* is ``"Z"``, an offset (``"-03:00"``) or None (MariaDB and MySQL
    5.6 and earlier: the server's local time, zone not written)."""
    text = (text or "").strip()
    m = _ISO_TIME.match(text)
    if m:
        y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
        if not _valid(y, mo, d, h, mi, s):
            return None
        value = "%04d-%02d-%02d %02d:%02d:%02d" % (y, mo, d, h, mi, s)
        if m.group(7):
            value += "." + m.group(7).ljust(6, "0")
        return value, m.group(8)
    m = _OLD_TIME.match(text)
    if m:
        y, mo, d, h, mi, s = (int(g) for g in m.groups())
        y += 2000
        if not _valid(y, mo, d, h, mi, s):
            return None
        return "%04d-%02d-%02d %02d:%02d:%02d" % (y, mo, d, h, mi, s), None
    return None


def time_key(value):
    """A time as a string that compares in time order: microseconds always written."""
    if value is None:
        return None
    return value if len(value) > 19 else value + ".000000"


def normalize_bound(value, end=False, name="From"):
    """A From or To value as a comparable key, or None when empty.

    Accepts ``YYYY-MM-DD``, ``YYYY-MM-DD HH:MM`` (or with ``T``), seconds and
    microseconds. Both ends are inclusive to the precision written: a To
    without seconds includes that whole minute, a date alone the whole day.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("%s must be a date and time such as 2026-10-31 14:30." % name)
    text = value.strip()
    if not text:
        return None
    m = _BOUND.match(text)
    if not m:
        raise ValueError("%s must be a date and time such as 2026-10-31 14:30." % name)
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    parts = [m.group(4), m.group(5), m.group(6)]
    h = int(parts[0]) if parts[0] is not None else (23 if end else 0)
    mi = int(parts[1]) if parts[1] is not None else (59 if end else 0)
    s = int(parts[2]) if parts[2] is not None else (59 if end else 0)
    if not _valid(y, mo, d, h, mi, s):
        raise ValueError("%s is not a real date and time: %s." % (name, text))
    frac = m.group(7)
    if frac is None:
        frac = "999999" if end else "000000"
    else:
        frac = frac.ljust(6, "9" if end else "0")
    return "%04d-%02d-%02d %02d:%02d:%02d.%s" % (y, mo, d, h, mi, s, frac)


def check_range(range_from, range_to):
    """``(from_key, to_key)``, validated; ValueError when From is after To."""
    low = normalize_bound(range_from, False, "From")
    high = normalize_bound(range_to, True, "To")
    if low and high and low > high:
        raise ValueError("From must be before To.")
    return low, high


def _offset_label(zone):
    if zone in ("Z", "+00:00", "-00:00"):
        return "UTC"
    return "UTC" + zone


def clock_summary(zones, timed, local_offset=None):
    """What the times of a log mean: ``{kind, label, offsets, text}``.

    *local_offset* is the UTC offset a zone-less log shows through SET
    timestamp (see :class:`Parser`), or None."""
    offsets = sorted(z for z in zones if z and z != "Z")
    utc = "Z" in zones
    local = None in zones
    if not timed:
        return {"kind": "none", "label": None, "offsets": [],
                "text": "The log has no # Time lines, so entries have no time and a time range cannot apply."}
    if utc and not offsets and not local:
        return {"kind": "utc", "label": "UTC", "offsets": [],
                "text": "Times are UTC, as the log writes them (log_timestamps=UTC)."}
    if offsets and not utc and not local and len(offsets) == 1:
        label = _offset_label(offsets[0])
        return {"kind": "offset", "label": label, "offsets": offsets,
                "text": "Times are the server's local time (%s), as the log writes them (log_timestamps=SYSTEM)."
                        % label}
    if local and not utc and not offsets:
        if local_offset:
            label = _offset_label(local_offset)
            return {"kind": "local", "label": "Server local time (%s)" % label, "offsets": [local_offset],
                    "text": "Times are the server's local time, as the log writes them. The log does not record "
                            "the time zone; its SET timestamp values put it at %s." % label}
        return {"kind": "local", "label": "Server local time", "offsets": [],
                "text": "Times are the server's local time, as the log writes them; the log does not record "
                        "the time zone."}
    parts = []
    if utc:
        parts.append("UTC")
    parts.extend(_offset_label(z) for z in offsets)
    if local:
        parts.append("local time without a zone")
    return {"kind": "mixed", "label": "As written", "offsets": offsets,
            "text": "The log mixes times written as %s (the setting or the time zone changed while it was "
                    "written). Times are compared as written, not moved to one zone." % ", ".join(parts)}


# ---- fingerprints ------------------------------------------------------------------------------

#: Words written in upper case in fingerprints (keywords and built-in
#: functions); every other word keeps its letter case.
KEYWORDS = frozenset("""
ACCESSIBLE ACCOUNT ACTION ADD AFTER AGAINST AGGREGATE ALGORITHM ALL ALTER ALWAYS ANALYZE AND ANY AS ASC ASCII
ASENSITIVE AT AUTO_INCREMENT AVG AVG_ROW_LENGTH BEFORE BEGIN BETWEEN BIGINT BINARY BINLOG BIT BLOB BOOL BOOLEAN BOTH
BTREE BY CACHE CALL CASCADE CASCADED CASE CAST CHANGE CHAR CHARACTER CHARSET CHECK CHECKSUM CLOSE COALESCE COLLATE
COLLATION COLUMN COLUMNS COMMENT COMMIT COMMITTED COMPRESSED CONCAT CONCURRENT CONDITION CONNECTION CONSISTENT
CONSTRAINT CONTINUE CONVERT COPY COUNT CREATE CROSS CUBE CURDATE CURRENT CURRENT_DATE CURRENT_TIME
CURRENT_TIMESTAMP CURRENT_USER CURSOR CURTIME DATA DATABASE DATABASES DATE DATETIME DATE_ADD DATE_FORMAT DATE_SUB
DAY DAY_HOUR DAY_MICROSECOND DAY_MINUTE DAY_SECOND DEALLOCATE DEC DECIMAL DECLARE DEFAULT DEFINER DELAYED DELETE
DENSE_RANK DESC DESCRIBE DETERMINISTIC DISTINCT DISTINCTROW DIV DO DOUBLE DROP DUAL DUPLICATE DYNAMIC EACH ELSE
ELSEIF ENABLE ENCLOSED END ENGINE ENGINES ENUM ESCAPE ESCAPED EVENT EVENTS EXCEPT EXECUTE EXISTS EXIT EXPLAIN
EXTENDED EXTRACT FALSE FETCH FIELDS FIRST FIRST_VALUE FLOAT FLUSH FOLLOWING FOR FORCE FOREIGN FORMAT FOUND FROM
FULL FULLTEXT FUNCTION GLOBAL GRANT GRANTS GREATEST GROUP GROUPING GROUP_CONCAT HANDLER HASH HAVING HIGH_PRIORITY
HOUR HOUR_MICROSECOND HOUR_MINUTE HOUR_SECOND IF IFNULL IGNORE IMMEDIATE IN INDEX INDEXES INFILE INNER INOUT INSERT
INSTANT INT INTEGER INTERSECT INTERVAL INTO INVOKER IS ISNULL ISOLATION ITERATE JOIN JSON JSON_ARRAYAGG
JSON_EXTRACT JSON_OBJECT JSON_OBJECTAGG JSON_TABLE KEY KEYS KILL LAG LAST LAST_VALUE LATERAL LEAD LEADING LEAST
LEAVE LEFT LENGTH LEVEL LIKE LIMIT LINES LOAD LOCAL LOCALTIME LOCALTIMESTAMP LOCK LOCKED LOGS LONGBLOB LONGTEXT
LOOP LOWER LOW_PRIORITY MASTER MATCH MAX MEDIUMBLOB MEDIUMINT MEDIUMTEXT MERGE MICROSECOND MIN MINUTE
MINUTE_MICROSECOND MINUTE_SECOND MOD MODE MODIFY MONTH NAMES NATURAL NEXT NO NOT NOW NOWAIT NTH_VALUE NTILE NULL
NULLIF NULLS NUMERIC OF OFFSET ON ONLY OPEN OPTIMIZE OPTION OPTIONALLY OR ORDER OUT OUTER OUTFILE OVER PARTITION
PARTITIONS PASSWORD PERCENT_RANK PERSIST PERSIST_ONLY POSITION PRECEDING PRECISION PREPARE PRIMARY PRIVILEGES
PROCEDURE PROCESSLIST PURGE QUARTER QUERY QUICK RANGE RANK READ REAL RECURSIVE REFERENCES REGEXP RELEASE RENAME
REPAIR REPEAT REPEATABLE REPLACE REPLICA REQUIRE RESET RESIGNAL RESTRICT RETURN RETURNING RETURNS REVOKE RIGHT
RLIKE ROLE ROLLBACK ROLLUP ROUND ROW ROWS ROW_NUMBER SAVEPOINT SCHEMA SCHEMAS SECOND SECOND_MICROSECOND SECURITY
SELECT SENSITIVE SEPARATOR SEQUENCE SERIALIZABLE SESSION SET SHARE SHOW SIGNAL SIGNED SKIP SLAVE SMALLINT
SNAPSHOT SOME SPATIAL SQL SQL_BIG_RESULT SQL_BUFFER_RESULT SQL_CALC_FOUND_ROWS SQL_NO_CACHE SQL_SMALL_RESULT
SQLEXCEPTION SQLSTATE SQLWARNING START STARTING STATUS STD STDDEV STOP STORAGE STRAIGHT_JOIN SUBSTR SUBSTRING SUM
SYSDATE TABLE TABLES TEMPORARY TEMPTABLE TERMINATED TEXT THEN TIME TIMESTAMP TIMESTAMPDIFF TINYBLOB TINYINT
TINYTEXT TO TRAILING TRANSACTION TRIGGER TRIGGERS TRIM TRUE TRUNCATE TYPE UNCOMMITTED UNDEFINED UNDO UNION UNIQUE
UNKNOWN UNLOCK UNSIGNED UNTIL UPDATE UPPER USAGE USE USER USING UTC_DATE UTC_TIME UTC_TIMESTAMP VALUE VALUES
VARBINARY VARCHAR VARIABLES VARYING VIEW VIRTUAL WAIT WARNINGS WEEK WHEN WHERE WHILE WINDOW WITH WORK WRITE XA
XOR YEAR YEAR_MONTH ZEROFILL
""".split())

_TOKEN = re.compile(r"""
 (?P<ws>\s+)
|(?P<comment>/\*.*?(?:\*/|\Z)|(?:--(?=[ \t\r\n\f\v]|\Z)|\#)[^\n]*)
|(?P<string>(?:[nN]|_[A-Za-z0-9]+)?'(?:''|\\.|[^'\\])*'?|"(?:""|\\.|[^"\\])*"?)
|(?P<quoted>`(?:``|[^`])*`?)
|(?P<hex>(?:0[xX][0-9A-Fa-f]+|[xX]'[0-9A-Fa-f]*'|0[bB][01]+|[bB]'[01]*')(?![\w$]))
|(?P<number>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?![\w$]))
|(?P<variable>@@?(?:`(?:``|[^`])*`?|'(?:''|\\.|[^'\\])*'?|"(?:""|\\.|[^"\\])*"?|[\w$.]+))
|(?P<word>[\w$]+)
|(?P<marker>\?)
|(?P<op><=>|<=|>=|<>|!=|:=|->>|->|\|\||&&|<<|>>|[^\s\w])
""", re.S | re.X)

#: The tokenizer with a backslash as a plain character in strings.
_TOKEN_PLAIN = re.compile(_TOKEN.pattern.replace(r"""'(?:''|\\.|[^'\\])*'?|"(?:""|\\.|[^"\\])*"?)""",
                                                 r"""'(?:''|[^'])*'?|"(?:""|[^"])*"?)""", 1), re.S | re.X)

#: Words after which "(" starts a list of values or a subquery, not a call.
_SPACE_BEFORE_PAREN = frozenset((
    "IN", "VALUES", "VALUE", "AS", "ON", "USING", "EXISTS", "FROM", "JOIN", "WHERE", "AND", "OR", "NOT", "SELECT",
    "INTO", "BY", "SET", "WHEN", "THEN", "ELSE", "UNION", "ALL", "ANY", "SOME", "WITH", "OVER", "TABLE", "LIKE",
    "HAVING", "RETURN", "IS", "BETWEEN", "CASE", "ROW", "LATERAL", "DISTINCT", "ON", "XOR", "IF", "EXCEPT",
    "INTERSECT", "PARTITION", "KEY", "INDEX",
))
_OPERATOR_BEFORE_SIGN = frozenset(("=", "<", ">", "<=", ">=", "<>", "!=", "<=>", "(", ",", "+", "-", "*", "/", "%",
                                   ":=", "AND", "OR", "NOT", "XOR", "IN", "BETWEEN", "LIKE", "IS", "WHEN", "THEN",
                                   "ELSE", "SELECT", "WHERE", "SET", "VALUES", "VALUE", "LIMIT", "OFFSET", "RETURN",
                                   "INTERVAL", "BY", "HAVING", "ON", "DIV", "MOD", None))
_CACHE_LIMIT = 20000


#: A string literal closed by its own quote (backslash escapes read).
_CLOSED_STRING = re.compile(r"(?:[nN]|_[A-Za-z0-9]+)?'(?:''|\\.|[^'\\])*'" + r'|"(?:""|\\.|[^"\\])*"', re.S)


def _unclosed(text):
    """Whether a string token runs to the end of the text without its quote."""
    return _CLOSED_STRING.fullmatch(text) is None


def _tokens(sql):
    """``[(kind, text, spaced)]`` with comments and white space dropped and
    literals as ``?``; *spaced* says white space or a comment came before.
    A string a backslash leaves open is read again with the backslash as a
    plain character (sql_mode NO_BACKSLASH_ESCAPES: 'C:\\' is complete)."""
    pattern = _TOKEN
    if "\\" in sql and any(_unclosed(m.group()) for m in _TOKEN.finditer(sql) if m.lastgroup == "string"):
        pattern = _TOKEN_PLAIN
    out = []
    pending_sign = False
    spaced = False
    for m in pattern.finditer(sql):
        kind = m.lastgroup
        text = m.group()
        if kind in ("ws", "comment"):
            spaced = True
            continue
        if kind in ("string", "hex", "number"):
            if pending_sign:
                spaced = out.pop()[2]
            pending_sign = False
            out.append(("value", "?", spaced))
        elif kind == "word":
            pending_sign = False
            upper = text.upper()
            out.append(("keyword", upper, spaced) if upper in KEYWORDS else ("name", text, spaced))
        elif kind == "marker":
            pending_sign = False
            out.append(("value", "?", spaced))
        elif kind == "op":
            previous = out[-1][1] if out else None
            pending_sign = text in ("-", "+") and previous in _OPERATOR_BEFORE_SIGN
            out.append(("op", text, spaced))
        else:
            pending_sign = False
            out.append(("name", text, spaced))
        spaced = False
    return out


def _is_call(previous):
    """Whether "(" after *previous* opens a function call's arguments."""
    if previous is None:
        return False
    kind, text, _ = previous
    if kind == "name":
        return True
    return kind == "keyword" and text not in _SPACE_BEFORE_PAREN


def _collapse(tokens):
    """Lists of values as ``(...)``: a parenthesized list of only values that
    is not a call's arguments (IN (?, ?), a row (?, ?)), and every row after
    the first of a VALUES list."""
    out = []
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        kind, text, spaced = token
        if text == "(" and not _is_call(out[-1] if out else None):
            end = _value_list_end(tokens, i)
            if end is not None:
                out.append(("list", "(...)", spaced))
                i = end + 1
                continue
        if kind == "keyword" and text in ("VALUES", "VALUE"):
            out.append(token)
            i += 1
            first = True
            while i < n:
                row = i
                if tokens[i][1] == "ROW" and i + 1 < n and tokens[i + 1][1] == "(":
                    i += 1
                if tokens[i][1] != "(":
                    i = row
                    break
                close = _matching(tokens, i)
                if close is None:
                    i = row
                    break
                if first:
                    out.extend(_collapse(tokens[row:close + 1]))
                first = False
                i = close + 1
                if i + 1 < n and tokens[i][1] == "," and (
                        tokens[i + 1][1] == "(" or (tokens[i + 1][1] == "ROW" and i + 2 < n and tokens[i + 2][1] == "(")):
                    i += 1
                    continue
                break
            continue
        out.append(token)
        i += 1
    return out


def _matching(tokens, start):
    depth = 0
    for j in range(start, len(tokens)):
        if tokens[j][1] == "(":
            depth += 1
        elif tokens[j][1] == ")":
            depth -= 1
            if depth == 0:
                return j
    return None


_LIST_WORDS = ("NULL", "TRUE", "FALSE", "DEFAULT")


#: Lists nested deeper than this are not collapsed (a deeply nested statement
#: is read in linear time and never runs out of stack).
_LIST_DEPTH = 64


def _value_list_end(tokens, start):
    """The index of the ")" closing a list of only values at *start*, or None.
    A nested list of values counts as one value of the list around it."""
    j = start + 1
    depth = 0
    expect_value = True
    seen = False
    while j < len(tokens):
        kind, text, _ = tokens[j]
        if expect_value:
            if kind in ("value", "list") or (kind == "keyword" and text in _LIST_WORDS):
                seen = True
                expect_value = False
                j += 1
                continue
            if text == "(" and depth < _LIST_DEPTH:
                depth += 1
                seen = False
                j += 1
                continue
            return None
        if text == ",":
            expect_value = True
            j += 1
            continue
        if text == ")" and seen:
            if depth == 0:
                return j
            depth -= 1
            j += 1
            continue
        return None
    return None


def _join(tokens):
    parts = []
    previous = None
    for kind, text, spaced in tokens:
        if previous is not None:
            p_kind, p_text = previous
            if text in (",", ")", ".", ";"):
                space = False
            elif p_text in ("(", ".", "@"):
                space = False
            elif text == "(" or kind == "list":
                # A call keeps its name and "(" together; a name written apart
                # from "(" (a table's column list) keeps a space.
                space = not (p_kind == "keyword" and p_text not in _SPACE_BEFORE_PAREN) and \
                    not (p_kind == "name" and not spaced)
            else:
                space = True
            if space:
                parts.append(" ")
        parts.append(text)
        previous = (kind, text)
    return "".join(parts)


#: The cache key: the text with string literals and plain numbers as ?, and
#: comments, backquoted names and variables kept as written. Texts with the
#: same shape differ only in literal values, which the tokenizer turns into ?
#: anyway, so they share a fingerprint (a log of one statement with many
#: values is tokenized once).
_SHAPE = re.compile(r"""(/\*.*?(?:\*/|\Z)|--[^\n]*|\#[^\n]*|`(?:``|[^`])*`?"""
                    r"""|@@?(?:`(?:``|[^`])*`?|'(?:''|\\.|[^'\\])*'?|"(?:""|\\.|[^"\\])*"?|[\w$.]+))"""
                    r"""|'(?:''|\\.|[^'\\])*'?|"(?:""|\\.|[^"\\])*"?|(?<![\w$])\d+(?:\.\d+)?(?![\w$])""", re.S)


def _shape_part(match):
    kept = match.group(1)
    return kept if kept is not None else "?"


def fingerprint(sql):
    """The normalized text of a statement (see the module docstring)."""
    if sql is None:
        return ""
    tokens = _tokens(sql)
    try:
        collapsed = _collapse(tokens)
    except RecursionError:
        # VALUES rows nested past the stack: literals stay ?, lists stay open.
        collapsed = tokens
    return _join(collapsed).rstrip(";").rstrip()


class Fingerprints(object):
    """:func:`fingerprint` with a bounded cache keyed by the statement's shape
    (one per read: reads in other threads keep their own)."""

    def __init__(self, limit=_CACHE_LIMIT):
        self.limit = limit
        self.cache = collections.OrderedDict()

    def __call__(self, sql):
        if sql is None:
            return ""
        # A backslash can mean two things in a string (sql_mode decides), so
        # such texts are not cached by shape.
        key = _SHAPE.sub(_shape_part, sql) if len(sql) <= 8192 and "\\" not in sql else None
        if key is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache.move_to_end(key)
                return cached
        text = fingerprint(sql)
        if key is not None:
            self.cache[key] = text
            if len(self.cache) > self.limit:
                self.cache.popitem(last=False)
        return text


def fingerprint_id(text):
    """A short stable id of a fingerprint (16 hex characters, not a server digest)."""
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]


# ---- parsing ------------------------------------------------------------------------------------

_SERVER_HEADER = re.compile(r"^(?P<path>\S.*?), Version: (?P<version>\S+)(?: \((?P<comment>.*)\))?\. started with:\s*$")
_TCP_LINE = re.compile(r"^Tcp port: \d+")
_COLUMNS_LINE = re.compile(r"^Time\s+Id\s+Command\s+Argument\s*$")
_USER_HOST = re.compile(r"^# User@Host: (?P<priv>[^\[]*)\[(?P<user>[^\]]*)\] @ (?P<host>[^\[]*?) ?\[(?P<ip>[^\]]*)\]"
                        r"(?:\s+Id:\s*(?P<id>\d+))?")
_PAIR = re.compile(r"([A-Za-z_][A-Za-z0-9_]*): ?(\S*)")
_USE = re.compile(r"^use (?P<db>.+);\s*$")
_SET = re.compile(r"^SET (?P<body>(?:last_insert_id=-?\d+,)?(?:insert_id=-?\d+,)?timestamp=(?P<ts>\d+))(?P<rest>.*);\s*$")
_ADMIN = "# administrator command: "
_MICROS = re.compile(r"^(\d+)(?:\.(\d{1,6})\d*)?$")
_YES = ("yes", "on", "true", "1")
#: Header keys read into the report, by the name they get.
_INT_KEYS = {
    "Rows_sent": "rows_sent", "Rows_examined": "rows_examined", "Rows_affected": "rows_affected",
    "Bytes_sent": "bytes_sent", "Bytes_received": "bytes_received",
    "Created_tmp_tables": "tmp_tables", "Tmp_tables": "tmp_tables",
    "Created_tmp_disk_tables": "tmp_disk_tables", "Tmp_disk_tables": "tmp_disk_tables",
    "Sort_merge_passes": "merge_passes", "Merge_passes": "merge_passes",
    "Errno": "errno", "Last_errno": "errno", "Killed": "killed",
}
_FLAG_KEYS = {"Full_scan": "full_scan", "Full_join": "full_join", "Filesort": "filesort",
              "Filesort_on_disk": "filesort_on_disk", "QC_hit": "qc_hit", "QC_Hit": "qc_hit"}


def micros(text):
    """Seconds as the log writes them ("1.000123") in whole microseconds, or None."""
    m = _MICROS.match((text or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 1000000 + int((m.group(2) or "0").ljust(6, "0"))


class ParseStats(object):
    """What reading a log found, apart from its statements."""

    def __init__(self):
        self.bytes = 0
        self.lines = 0
        self.entries = 0
        self.malformed = 0
        self.problems = []
        self.long_lines = 0
        self.truncated = 0
        self.admin = 0
        self.own = 0
        self.no_time = 0
        self.inferred = 0
        self.local_offset = None
        self.versions = []
        self.hints = set()
        self.zones = collections.Counter()
        self.present = set()
        self.first = None
        self.last = None

    def problem(self, line, reason):
        self.malformed += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append({"line": line, "reason": reason})

    def as_dict(self):
        return {"bytes": self.bytes, "lines": self.lines, "entries": self.entries,
                "malformed": self.malformed, "problems": list(self.problems),
                "long_lines": self.long_lines, "truncated_statements": self.truncated,
                "admin_commands": self.admin, "excluded_own": self.own, "no_time": self.no_time,
                "inferred_times": self.inferred}


class Entry(object):
    """One logged statement."""
    __slots__ = ("line", "time", "zone", "timestamp", "user", "host", "thread_id", "db", "query_us", "lock_us",
                 "values", "flags", "text", "truncated", "admin")

    def __init__(self):
        self.line = 0
        self.time = None
        self.zone = None
        self.timestamp = None
        self.user = None
        self.host = None
        self.thread_id = None
        self.db = None
        self.query_us = None
        self.lock_us = None
        self.values = {}
        self.flags = {}
        self.text = ""
        self.truncated = False
        self.admin = False


class _Pending(object):
    __slots__ = ("entry", "phase", "has_user", "has_query", "pairs", "schema", "parts", "chars", "timed", "bad",
                 "lines")

    def __init__(self, line):
        self.entry = Entry()
        self.entry.line = line
        self.phase = "header"
        self.has_user = False
        self.has_query = False
        self.pairs = {}
        self.schema = None
        self.parts = []
        self.chars = 0
        self.timed = False
        self.bad = None
        self.lines = 0


class Parser(object):
    """Turns log lines into :class:`Entry` objects (``on_entry``), line by line."""

    def __init__(self, on_entry, stats=None):
        self.on_entry = on_entry
        self.stats = stats or ParseStats()
        self.current = None
        self.db = None
        self.last_time = None
        self.header_lines = 0
        self.stray_start = None
        self.stray_count = 0
        # Local (zone-less) logs: the server's UTC offset, learned from an
        # entry with both its own # Time and SET timestamp, and the entries
        # read before the first # Time line, which wait for it.
        self.offset = None
        self.seen_time = False
        self.waiting = []
        self.waiting_chars = 0

    # Lines outside any entry are reported once per run.
    def _stray(self, number):
        if self.stray_start is None:
            self.stray_start = number
        self.stray_count += 1

    def _flush_stray(self):
        if self.stray_start is not None:
            n = self.stray_count
            self.stats.problem(self.stray_start, "%d %s outside any entry (no # Time, # User@Host or # Query_time "
                                                 "line before %s)" % (n, "line" if n == 1 else "lines",
                                                                      "it" if n == 1 else "them"))
            self.stray_start = None
            self.stray_count = 0

    def _start(self, number):
        self._close()
        self._flush_stray()
        self.current = _Pending(number)
        return self.current

    def line(self, number, text, long_line=False):
        stats = self.stats
        stats.lines = number
        if long_line:
            stats.long_lines += 1
        m = _SERVER_HEADER.match(text) if ", Version: " in text else None
        if m:
            self._close()
            self._flush_stray()
            version = m.group("version")
            label = version + (" (%s)" % m.group("comment") if m.group("comment") else "")
            if label not in stats.versions:
                stats.versions.append(label)
            self.header_lines = 2
            return
        if self.header_lines:
            self.header_lines -= 1
            if _TCP_LINE.match(text) or _COLUMNS_LINE.match(text):
                return
            self.header_lines = 0
        current = self.current
        if text.startswith("# Time:"):
            pending = self._start(number)
            parsed = parse_log_time(text[7:])
            if parsed is None:
                pending.bad = "the # Time value is not a date and time"
                self.last_time = None
            else:
                self.last_time = parsed
                pending.entry.time, pending.entry.zone = parsed
                pending.timed = True
                if "T" in text[7:]:
                    stats.hints.add("iso")
                else:
                    stats.hints.add("old_time")
            pending.lines += 1
            return
        if text.startswith("# User@Host:"):
            if current is None or current.phase != "header" or current.has_user or current.has_query:
                current = self._start(number)
            current.has_user = True
            current.lines += 1
            m = _USER_HOST.match(text)
            if m:
                login = m.group("user").strip()
                current.entry.user = login or m.group("priv").strip() or None
                current.entry.host = m.group("host").strip() or m.group("ip").strip() or None
                if m.group("id"):
                    current.entry.thread_id = int(m.group("id"))
            else:
                current.bad = current.bad or "the # User@Host line cannot be read"
            return
        if text.startswith("# Thread_id:"):
            stats.hints.add("mariadb")
            if current is None or current.phase != "header" or current.has_query or "Thread_id" in current.pairs:
                current = self._start(number)
            current.lines += 1
            self._pairs(current, text)
            return
        if text.startswith("# Query_time:"):
            if current is None or current.phase != "header" or current.has_query:
                current = self._start(number)
            current.has_query = True
            current.lines += 1
            self._pairs(current, text)
            return
        if current is not None and current.phase == "header" and text.startswith("#") and not text.startswith(_ADMIN):
            current.lines += 1
            if text.startswith("# explain:") or text.startswith("# Warnings") or not text.startswith("# "):
                return
            if text.startswith("# Schema:") or text.startswith("# Log_slow_rate") or "InnoDB_" in text:
                stats.hints.add("percona")
            self._pairs(current, text)
            return
        if current is not None and current.phase in ("header", "preamble"):
            m = _USE.match(text)
            if m:
                db = m.group("db").strip()
                if len(db) >= 2 and db[0] == "`" and db[-1] == "`":
                    db = db[1:-1].replace("``", "`")
                self.db = db or None
                current.phase = "preamble"
                current.lines += 1
                return
            m = _SET.match(text)
            if m:
                current.entry.timestamp = int(m.group("ts"))
                current.phase = "text"
                current.lines += 1
                return
            if text.startswith(_ADMIN):
                current.entry.admin = True
                current.phase = "text"
                self._text(current, text[len(_ADMIN):])
                return
        if current is not None:
            current.phase = "text"
            if not current.parts and text.startswith(_ADMIN):
                current.entry.admin = True
                text = text[len(_ADMIN):]
            self._text(current, text)
            return
        if text.strip():
            self._stray(number)

    def _pairs(self, pending, text):
        for key, value in _PAIR.findall(text[1:]):
            if key == "Schema":
                pending.schema = value
            if key not in pending.pairs:
                pending.pairs[key] = value

    def _text(self, pending, text):
        pending.lines += 1
        if pending.chars >= MAX_TEXT_CHARS:
            pending.entry.truncated = True
            return
        room = MAX_TEXT_CHARS - pending.chars
        if len(text) > room:
            text = text[:room]
            pending.entry.truncated = True
        pending.parts.append(text)
        pending.chars += len(text) + 1

    def finish(self):
        self._close()
        self._flush_stray()
        self._release()

    def _close(self):
        pending = self.current
        self.current = None
        if pending is None:
            return
        stats = self.stats
        entry = pending.entry
        pairs = pending.pairs
        if not pending.timed and self.last_time is not None and "old_time" in stats.hints:
            # MariaDB writes # Time only when the second changes: the entry
            # belongs to the last time written.
            entry.time, entry.zone = self.last_time
        if pending.bad:
            stats.problem(entry.line, "Entry skipped: %s." % pending.bad)
            return
        if not pending.has_query:
            stats.problem(entry.line, "Entry skipped: no # Query_time line.")
            return
        query_us = micros(pairs.get("Query_time"))
        if query_us is None:
            stats.problem(entry.line, "Entry skipped: Query_time %r is not a number of seconds."
                          % pairs.get("Query_time", "")[:40])
            return
        entry.query_us = query_us
        entry.lock_us = micros(pairs.get("Lock_time")) if "Lock_time" in pairs else None
        if "Lock_time" in pairs and entry.lock_us is None:
            stats.problem(entry.line, "Entry skipped: Lock_time %r is not a number of seconds."
                          % pairs.get("Lock_time", "")[:40])
            return
        for key, name in _INT_KEYS.items():
            if key in pairs:
                value = pairs[key]
                if not value.isdigit():
                    stats.problem(entry.line, "Entry skipped: %s %r is not a whole number." % (key, value[:40]))
                    return
                entry.values[name] = int(value)
                stats.present.add(name)
        for key, name in _FLAG_KEYS.items():
            if key in pairs:
                entry.flags[name] = pairs[key].lower() in _YES
                stats.present.add(name)
        if entry.thread_id is None and "Thread_id" in pairs and pairs["Thread_id"].isdigit():
            entry.thread_id = int(pairs["Thread_id"])
        text = "\n".join(pending.parts).strip()
        if text.endswith(";"):
            text = text[:-1].rstrip()
        if not text:
            stats.problem(entry.line, "Entry skipped: no statement text.")
            return
        entry.text = text
        if pending.schema is not None:
            entry.db = pending.schema or None
        else:
            entry.db = self.db
        if entry.truncated:
            stats.truncated += 1
        if entry.admin:
            stats.admin += 1
        if pending.timed and entry.zone is None and entry.timestamp is not None and self.offset is None:
            self._learn_offset(entry)
        if entry.time is None and not self.seen_time and entry.timestamp is not None:
            # MariaDB (and MySQL 5.6) write # Time only when the second
            # changes: entries before the file's first # Time line belong to
            # a second written before the file began. Their SET timestamp
            # gives it once the log shows its UTC offset.
            self.waiting.append(entry)
            self.waiting_chars += len(entry.text)
            if len(self.waiting) > MAX_WAITING or self.waiting_chars > MAX_WAITING_CHARS:
                self._release()
            return
        if pending.timed:
            self.seen_time = True
            self._release()
        self._emit(entry)

    def _learn_offset(self, entry):
        try:
            local = datetime.datetime.strptime(entry.time[:19], "%Y-%m-%d %H:%M:%S")
            utc = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=entry.timestamp)
        except (ValueError, OverflowError):
            return
        offset = local - utc
        seconds = offset.days * 86400 + offset.seconds
        if abs(seconds) <= 14 * 3600 and seconds % 900 == 0:
            self.offset = offset
            sign = "-" if seconds < 0 else "+"
            self.stats.local_offset = "%s%02d:%02d" % (sign, abs(seconds) // 3600, abs(seconds) % 3600 // 60)

    def _release(self):
        """Emit the entries that waited for a time, with the time their SET
        timestamp gives at the learned offset when there is one."""
        waiting, self.waiting, self.waiting_chars = self.waiting, [], 0
        for entry in waiting:
            if self.offset is not None and entry.timestamp is not None:
                try:
                    local = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=entry.timestamp) + self.offset
                except OverflowError:
                    local = None
                if local is not None:
                    entry.time = local.strftime("%Y-%m-%d %H:%M:%S")
                    self.stats.inferred += 1
            self._emit(entry)

    def _emit(self, entry):
        stats = self.stats
        if entry.time is not None:
            stats.zones[entry.zone] += 1
            key = time_key(entry.time)
            if stats.first is None or key < time_key(stats.first):
                stats.first = entry.time
            if stats.last is None or key > time_key(stats.last):
                stats.last = entry.time
        else:
            stats.no_time += 1
        stats.entries += 1
        self.on_entry(entry)


# ---- aggregation -------------------------------------------------------------------------------

class _Group(object):
    __slots__ = ("db", "text", "count", "total", "lock", "lock_count", "low", "high", "values", "flags", "errors",
                 "killed", "first", "last", "hist", "sample", "sample_us", "users", "hosts", "admin")

    def __init__(self, db, text, admin):
        self.db = db
        self.text = text
        self.count = 0
        self.total = 0
        self.lock = 0
        self.lock_count = 0
        self.low = None
        self.high = 0
        self.values = collections.Counter()
        self.flags = collections.Counter()
        self.errors = 0
        self.killed = 0
        self.first = None
        self.last = None
        self.hist = collections.Counter()
        self.sample = None
        self.sample_us = -1
        self.users = collections.Counter()
        self.hosts = collections.Counter()
        self.admin = admin


class Aggregator(object):
    """Sums entries by (database, fingerprint) within an optional range and
    user and database filters."""

    def __init__(self, range_from=None, range_to=None, user=None, database=None, keep_own=False):
        self.range_from = range_from
        self.range_to = range_to
        self.user = user
        self.database = database
        self.keep_own = keep_own
        self.groups = collections.OrderedDict()
        self.overflow = {"statements": 0, "executions": 0, "total_us": 0}
        self.overflow_keys = set()
        self.overflow_more = False
        self.in_range = 0
        self.matched = 0
        self.out_of_range = 0
        self.untimed_skipped = 0
        self.own = 0
        self.users = collections.Counter()
        self.databases = collections.Counter()
        self.span_first = None
        self.span_last = None
        self.fingerprint = Fingerprints()

    def add(self, entry):
        if not self.keep_own and OWN_TAG in entry.text:
            self.own += 1
            return
        if self.range_from or self.range_to:
            if entry.time is None:
                self.untimed_skipped += 1
                return
            key = time_key(entry.time)
            if (self.range_from and key < self.range_from) or (self.range_to and key > self.range_to):
                self.out_of_range += 1
                return
        self.in_range += 1
        self.users[entry.user or ""] += 1
        self.databases[entry.db or ""] += 1
        if self.user is not None and (entry.user or "") != self.user:
            return
        if self.database is not None and (entry.db or "") != self.database:
            return
        self.matched += 1
        if entry.time is not None:
            key = time_key(entry.time)
            if self.span_first is None or key < time_key(self.span_first):
                self.span_first = entry.time
            if self.span_last is None or key > time_key(self.span_last):
                self.span_last = entry.time
        text = ("administrator command: " + entry.text) if entry.admin else self.fingerprint(entry.text)
        group_key = (entry.db, text)
        group = self.groups.get(group_key)
        if group is None:
            if len(self.groups) >= MAX_FINGERPRINTS:
                key_hash = hash(group_key)
                if key_hash not in self.overflow_keys:
                    if len(self.overflow_keys) < MAX_OVERFLOW_KEYS:
                        self.overflow_keys.add(key_hash)
                        self.overflow["statements"] += 1
                    else:
                        self.overflow_more = True
                self.overflow["executions"] += 1
                self.overflow["total_us"] += entry.query_us
                return
            group = self.groups[group_key] = _Group(entry.db, text, entry.admin)
        group.count += 1
        group.total += entry.query_us
        if entry.lock_us is not None:
            group.lock += entry.lock_us
            group.lock_count += 1
        if group.low is None or entry.query_us < group.low:
            group.low = entry.query_us
        if entry.query_us > group.high:
            group.high = entry.query_us
        for name, value in entry.values.items():
            if name == "errno":
                if value:
                    group.errors += 1
            elif name == "killed":
                if value:
                    group.killed += 1
            else:
                group.values[name] += value
        for name, value in entry.flags.items():
            if value:
                group.flags[name] += 1
        if entry.time is not None:
            key = time_key(entry.time)
            if group.first is None or key < time_key(group.first):
                group.first = entry.time
            if group.last is None or key > time_key(group.last):
                group.last = entry.time
        group.hist[bucket_of(entry.query_us / 1000.0)] += 1
        if entry.query_us > group.sample_us:
            group.sample_us = entry.query_us
            text = entry.text
            group.sample = {"text": text[:SAMPLE_CHARS], "truncated": entry.truncated or len(text) > SAMPLE_CHARS,
                            "db": entry.db, "user": entry.user, "host": entry.host, "time": entry.time,
                            "thread_id": entry.thread_id, "query_time_ms": round(entry.query_us / 1000.0, 3),
                            "line": entry.line or None}
        if entry.user is not None:
            group.users[entry.user] += 1
        if entry.host is not None:
            group.hosts[entry.host] += 1

    def statements(self, present):
        """Every group as a report statement (unsorted, without analysis fields)."""
        return [_statement(group, present) for group in self.groups.values()]


def _ms(us):
    return None if us is None else round(us / 1000.0, 3)


def _names(counter, limit):
    return [{"name": name, "count": n} for name, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def _statement(group, present):
    count = group.count
    values = group.values
    hist = group.hist
    max_ms = _ms(group.high)

    def capped(value):
        if value is None:
            return max_ms
        return min(value, max_ms) if max_ms is not None else value

    def summed(name):
        return values.get(name, 0) if name in present else None

    def flagged(name):
        return group.flags.get(name, 0) if name in present else None
    rows_sent = values.get("rows_sent", 0)
    rows_examined = values.get("rows_examined", 0)
    return {
        "digest": None,
        "fingerprint_id": fingerprint_id(group.text),
        "digest_text": group.text,
        "schema": group.db,
        "count": count,
        "total_ms": _ms(group.total),
        "avg_ms": round(group.total / 1000.0 / count, 3) if count else 0.0,
        "min_ms": _ms(group.low),
        "max_ms": max_ms,
        "p95_ms": capped(quantile(hist, count, 95, 100)),
        "p99_ms": capped(quantile(hist, count, 99, 100)),
        "p999_ms": capped(quantile(hist, count, 999, 1000)),
        "lock_time_ms": _ms(group.lock) if group.lock_count else None,
        "rows_sent": rows_sent if "rows_sent" in present else None,
        "rows_examined": rows_examined if "rows_examined" in present else None,
        "examined_per_sent": round(rows_examined / float(rows_sent), 2) if rows_sent else None,
        "rows_affected": summed("rows_affected"),
        "bytes_sent": summed("bytes_sent"),
        "no_index_used": None,
        "no_good_index_used": None,
        "tmp_tables": summed("tmp_tables"),
        "tmp_disk_tables": summed("tmp_disk_tables"),
        "sort_merge_passes": summed("merge_passes"),
        "select_scan": flagged("full_scan"),
        "select_full_join": flagged("full_join"),
        "select_range_check": None,
        "filesort": flagged("filesort"),
        "filesort_on_disk": flagged("filesort_on_disk"),
        "errors": group.errors if "errno" in present else None,
        "killed": group.killed if "killed" in present else None,
        "sample_text": group.sample["text"] if group.sample else None,
        "sample_truncated": bool(group.sample and group.sample["truncated"]),
        "sample": dict(group.sample) if group.sample else None,
        "first_seen": group.first,
        "last_seen": group.last,
        "users": _names(group.users, MAX_STATEMENT_NAMES),
        "hosts": _names(group.hosts, MAX_STATEMENT_NAMES),
        "histogram": [{"bucket": index, "low_ms": bucket_low(index), "high_ms": bucket_high(index), "count": n}
                      for index, n in sorted(hist.items())],
        "admin": group.admin,
        "flags": [],
        "cpu_ms": None,
        "new": False,
    }


def _sort_value(statement, key):
    value = statement.get(key)
    return -1 if value is None else value


def check_options(order="total", limit=DEFAULT_LIMIT, user=None, database=None):
    """Validate ranking and filter choices (before any file or server is read)."""
    if order not in ORDERS:
        raise ValueError("Rank by one of: %s." % ", ".join(ORDERS))
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("Choose a limit from 1 to %d." % MAX_LIMIT)
    for name, value in (("User", user), ("Database", database)):
        if value is not None and (not isinstance(value, str) or len(value) > 256 or "\x00" in value):
            raise ValueError("%s must be a name of at most 256 characters." % name)


def build_report(aggregator, stats, source, order="total", limit=DEFAULT_LIMIT, engine=None, server_version=None,
                 flavor=None, server_product=None, anchor=None, clock=None, extra_notes=()):
    """The ``slow-log-1.0`` report from an :class:`Aggregator` and its
    :class:`ParseStats`."""
    from .analysis_query import prepare
    present = stats.present | {"rows_sent", "rows_examined"} if stats.entries else stats.present
    items = aggregator.statements(present)
    key = ORDERS[order]
    items.sort(key=lambda s: (-_sort_value(s, key), -s["total_ms"], s["digest_text"]))
    shown = items[:limit]
    rest = items[limit:]
    for item in shown:
        text = item["sample_text"] or item["digest_text"]
        if item["admin"]:
            item["capture_problem"] = "Administrator commands are not statements a plan can be read for."
            item["analysis_sql"] = None
            item["analysis_warning"] = ""
        else:
            projection = prepare(text if not item["sample_truncated"] else item["digest_text"])
            item["capture_problem"] = projection["problem"] or None
            item["analysis_sql"] = projection["sql"] if projection["converted"] else None
            item["analysis_warning"] = projection["warning"]
        item["source"] = source.get("kind_name")
    total_us = sum(g.total for g in aggregator.groups.values()) + aggregator.overflow["total_us"]
    executions = aggregator.matched
    others = None
    if rest or aggregator.overflow["executions"]:
        others = {
            "statements": len(rest) + aggregator.overflow["statements"],
            "executions": sum(s["count"] for s in rest) + aggregator.overflow["executions"],
            "total_ms": round(sum(g.total for g in list(aggregator.groups.values())) / 1000.0
                              - sum(s["total_ms"] for s in shown) + aggregator.overflow["total_us"] / 1000.0, 3),
        }
        others["total_ms"] = max(0.0, others["total_ms"])
    if clock is None:
        clock = clock_summary(set(stats.zones), sum(stats.zones.values()), stats.local_offset)
    notes = list(extra_notes)
    if stats.inferred:
        notes.append("%s %s before the log's first # Time line got %s time from SET timestamp at %s." % (
            _n(stats.inferred), "entry" if stats.inferred == 1 else "entries",
            "its" if stats.inferred == 1 else "their", _offset_label(stats.local_offset or "Z")))
    if aggregator.range_from or aggregator.range_to:
        if aggregator.untimed_skipped:
            notes.append("%s %s no time and %s left out of the range." % (
                _n(aggregator.untimed_skipped), "entry has" if aggregator.untimed_skipped == 1 else "entries have",
                "is" if aggregator.untimed_skipped == 1 else "are"))
    if stats.malformed:
        first = stats.problems[0]["line"] if stats.problems else None
        notes.append("%s %s could not be read%s." % (
            _n(stats.malformed), "part of the log" if stats.malformed == 1 else "parts of the log",
            " (the first at line %s)" % _n(first) if first else ""))
    if stats.long_lines:
        notes.append("%s %s longer than %s; the text past that was skipped." % (
            _n(stats.long_lines), "line was" if stats.long_lines == 1 else "lines were",
            "1 MiB"))
    if stats.truncated:
        notes.append("%s %s cut at %s characters; their fingerprint is of the part kept." % (
            _n(stats.truncated), "statement was" if stats.truncated == 1 else "statements were",
            _n(MAX_TEXT_CHARS)))
    if aggregator.own:
        notes.append("%s %s myflames sent %s left out." % (
            _n(aggregator.own), "statement" if aggregator.own == 1 else "statements",
            "is" if aggregator.own == 1 else "are"))
    if aggregator.overflow["statements"]:
        notes.append("More than %s different statements: %s%s more were summed apart and are counted under other "
                     "statements." % (_n(MAX_FINGERPRINTS), "at least " if aggregator.overflow_more else "",
                                      _n(aggregator.overflow["statements"])))
    report = {
        "schema_version": SCHEMA_VERSION,
        "source": {k: v for k, v in source.items() if k != "kind_name"},
        "engine": engine,
        "flavor": flavor,
        "server_product": server_product,
        "server_version": server_version,
        "clock": clock,
        "time_meaning": time_meaning(engine, source.get("kind") == "table"),
        "range": {"from": aggregator.range_from, "to": aggregator.range_to,
                  "applied": bool(aggregator.range_from or aggregator.range_to)},
        "filters": {"user": aggregator.user, "database": aggregator.database},
        "order": order,
        "limit": limit,
        "span": {"first": stats.first, "last": stats.last},
        "anchor": anchor if anchor is not None else stats.last,
        "totals": {"statements": len(aggregator.groups) + aggregator.overflow["statements"],
                   "executions": executions, "total_ms": round(total_us / 1000.0, 3),
                   "in_range": aggregator.in_range, "out_of_range": aggregator.out_of_range},
        "others": others,
        "users": _names(aggregator.users, MAX_NAMES),
        "databases": _names(aggregator.databases, MAX_NAMES),
        "parse": stats.as_dict(),
        "notes": notes,
        "statements": shown,
    }
    report["parse"]["excluded_own"] = aggregator.own
    return report


def _n(value):
    return "{:,}".format(value)


def time_meaning(engine, table=False):
    """What a logged time stands for, by engine and source (see the module
    docstring): the file's # Time, or mysql.slow_log's start_time."""
    if engine == "mariadb":
        if table:
            return {"event": "start", "text": "MariaDB stores the time each statement started in start_time."}
        return {"event": "start",
                "text": "MariaDB writes the time a statement started, and writes # Time only when the second changes."}
    if engine == "mysql":
        if table:
            return {"event": "end",
                    "text": "MySQL stores the time it logged each statement, after the statement ended, in start_time."}
        return {"event": "end",
                "text": "MySQL writes the time it logged each statement, after the statement ended."}
    return {"event": None,
            "text": "MySQL writes the time a statement ended; MariaDB the time it started."}


def detect_engine(stats):
    """``(engine, version, flavor, product)`` from the log's own lines."""
    from .server_info import product_identity
    for label in reversed(stats.versions):
        m = re.match(r"^(\S+)(?: \((.*)\))?$", label)
        version, comment = (m.group(1), m.group(2) or "") if m else (label, "")
        identity = product_identity(version, comment)
        number = re.match(r"^(\d+\.\d+\.\d+)", version)
        return identity["engine"], number.group(1) if number else version, identity["flavor"], identity["product"]
    if "mariadb" in stats.hints:
        return "mariadb", None, "mariadb", "MariaDB"
    if "percona" in stats.hints:
        return "mysql", None, "percona", "Percona Server"
    if "iso" in stats.hints:
        return "mysql", None, "mysql", "MySQL"
    return None, None, None, None


# ---- files ---------------------------------------------------------------------------------------

class Stopped(Exception):
    """Reading was stopped on request."""


def iter_lines(stream, stats, progress=None, stop=None, every=4096, every_bytes=4 * 1024 * 1024):
    """``(number, text, long)`` for each line of a binary *stream*, decoded as
    UTF-8 (bytes that are not are replaced). Lines over MAX_LINE_BYTES are
    cut there and the rest of the line skipped. Stop and progress are checked
    every *every* lines and every *every_bytes* bytes, so a log of long lines
    stops as soon as a log of short ones."""
    number = 0
    checked = [0]

    def check():
        checked[0] = stats.bytes
        if stop is not None and stop():
            raise Stopped()
        if progress is not None:
            progress(stats.bytes)
    while True:
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        number += 1
        stats.bytes += len(raw)
        long_line = False
        if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
            long_line = True
            raw = raw[:MAX_LINE_BYTES]
            while True:
                rest = stream.readline(MAX_LINE_BYTES)
                if not rest:
                    break
                stats.bytes += len(rest)
                if stats.bytes - checked[0] >= every_bytes:
                    check()
                if rest.endswith(b"\n"):
                    break
        if number % every == 0 or stats.bytes - checked[0] >= every_bytes:
            check()
        yield number, raw.decode("utf-8", "replace").rstrip("\r\n"), long_line


def read_stream(stream, source, range_from=None, range_to=None, user=None, database=None, order="total",
                limit=DEFAULT_LIMIT, progress=None, stop=None):
    """Read a whole log from a binary stream into a ``slow-log-1.0`` report.

    *range_from* and *range_to* are the user's values (see
    :func:`normalize_bound`); *source* is the report's ``source`` object."""
    check_options(order, limit, user, database)
    low, high = check_range(range_from, range_to)
    stats = ParseStats()
    aggregator = Aggregator(low, high, user, database)
    parser = Parser(aggregator.add, stats)
    for number, text, long_line in iter_lines(stream, stats, progress, stop):
        parser.line(number, text, long_line)
    parser.finish()
    if progress is not None:
        progress(stats.bytes)
    engine, version, flavor, product = detect_engine(stats)
    notes = []
    if not stats.entries:
        notes.append("No slow log entries were found. A slow query log has # Query_time lines; check that this "
                     "is the slow log and not the general or error log.")
    if engine == "mysql" and stats.entries:
        notes.append("MySQL writes use db only when the database changes, so a statement run without a "
                     "database may be counted under the one before it.")
    if engine == "mariadb" and "full_scan" in stats.present:
        notes.append("MariaDB writes the Full_scan line only for statements with a full scan, a temporary table or "
                     "a filesort, in sessions with log_slow_verbosity=query_plan: other statements count as No.")
    return build_report(aggregator, stats, source, order, limit, engine, version, flavor, product,
                        extra_notes=notes)


def check_path(path, label=None):
    """The resolved path of a readable regular file of at most MAX_FILE_BYTES.
    Messages name *label* (a file name) instead of the path when given."""
    import stat
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Enter the full path of a slow query log file.")
    if len(path) > 4096 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise ValueError("The file path contains characters a file name cannot have.")
    if path != path.strip() and not os.path.lexists(path) and os.path.lexists(path.strip()):
        path = path.strip()
    if not os.path.isabs(path):
        raise ValueError("Enter the full path of the file, starting at the root of the disk.")
    resolved = os.path.realpath(path)
    shown = label or path
    try:
        info = os.stat(resolved)
    except FileNotFoundError:
        raise ValueError("No file exists at %s." % shown if label is None else
                         "%s no longer exists (moved, rotated or deleted). Choose the file again." % shown)
    except OSError as exc:
        raise ValueError("The file cannot be opened (%s)." % (exc.strerror or exc))
    if stat.S_ISDIR(info.st_mode):
        raise ValueError("%s is a folder. Choose the slow query log file." % shown)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("%s is not a regular file (a device, socket or pipe cannot be read)." % shown)
    if info.st_size > MAX_FILE_BYTES:
        raise ValueError("The file is larger than 4 GiB. Split it, or copy the part you need.")
    if not os.access(resolved, os.R_OK):
        raise ValueError("myflames cannot read %s. Check its permissions." % shown)
    return resolved


def file_source(path, name=None):
    info = os.stat(path)
    modified = datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc)
    return {"kind": "file", "name": name or os.path.basename(path), "size_bytes": info.st_size,
            "modified": modified.strftime("%Y-%m-%dT%H:%M:%SZ"), "kind_name": "slow_log_file"}


def read_file(path, range_from=None, range_to=None, user=None, database=None, order="total",
              limit=DEFAULT_LIMIT, progress=None, stop=None, label=None):
    """Read the slow log at *path* (checked with :func:`check_path`; *label*
    names it in messages instead of its path)."""
    check_options(order, limit, user, database)
    check_range(range_from, range_to)
    resolved = check_path(path, label)
    source = file_source(resolved, os.path.basename(path))
    try:
        with open(resolved, "rb") as stream:
            return read_stream(stream, source, range_from, range_to, user, database, order, limit, progress, stop)
    except OSError as exc:
        raise ValueError("The file cannot be read (%s)." % (exc.strerror or exc))


def parse_text(text, **options):
    """Read a log held in a string (tests and tools)."""
    import io
    source = {"kind": "file", "name": options.pop("name", "slow.log"), "size_bytes": len(text.encode("utf-8")),
              "modified": None, "kind_name": "slow_log_file"}
    return read_stream(io.BytesIO(text.encode("utf-8")), source, **options)
