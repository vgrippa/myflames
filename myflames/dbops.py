"""The shared safety layer for every statement myflames sends that changes server state.

Every write path (``locks.kill_session`` today; the Query editor, variable
changes and replication actions next) goes through the same four parts:

* :func:`classify` reads SQL text and says whether it only reads, writes, or
  is a risky write, with the reasons. It splits scripts into statements the
  way the mysql client does (comments, quoted strings and identifiers under
  both backslash readings, executable comments ``/*! */`` and ``/*M! */``
  whose content is code, ``DELIMITER`` lines), reusing the tokenizer in
  :mod:`myflames.live` (``TOKEN`` and ``TOKEN_NO_BACKSLASH``). It is a hint:
  a SELECT can call a stored function that writes, and anything myflames
  cannot prove to be a read is classified as a write or a risky write.
* :func:`decide` turns a classification plus the connection's type
  (``production`` or ``development``) and read-only flag into ``refuse``,
  ``confirm`` or ``allow``, with the text to show.
* :func:`privilege_hint` maps an operation to the privileges it needs per
  engine and version and checks them against ``SHOW GRANTS`` (one parser,
  :func:`parse_grants`, for every operation). The server decides; this only
  explains a refusal before it happens.
* The audit trail: :func:`record_attempt` writes a ``pending`` record before a
  statement is sent and :func:`complete` finishes it, so a crash leaves an
  "outcome unknown" record instead of nothing. Declined and refused attempts
  are recorded too (:func:`record_outcome`). Secrets are redacted
  (:func:`redact`) and no password is ever stored. Records live in the
  ``audit`` table of ``~/.myflames/workspace.sqlite3`` (owner-only), kept for
  ``DEFAULT_RETENTION_DAYS`` days unless configured otherwise.

Classification decisions (see docs/DEVELOPER_REFERENCE.md, "Write safety"):

* ``SELECT ... FOR UPDATE``, ``FOR SHARE`` and ``LOCK IN SHARE MODE`` are
  writes (category ``locking_read``): they change no data, but they lock the
  rows they read until the transaction ends and other sessions wait.
* ``SELECT ... INTO OUTFILE | DUMPFILE`` is a write (category ``file``);
  ``INTO @var`` and ``:=`` assign user variables (write, category ``session``).
* ``SET NAMES``, ``SET [SESSION] TRANSACTION ...`` and ``SET SESSION`` of a
  short list of variables that only change how this session reads or shows
  results (:data:`SESSION_READ_VARIABLES`) are reads (category ``session``).
  Any other ``SET SESSION`` and user variable assignment is a write in
  category ``session`` (the lighter, primary tone). Session variables that
  affect replication (:data:`SESSION_RISKY_VARIABLES`, such as
  ``sql_log_bin``) are risky.
* ``CALL``, ``EXECUTE``, ``EXECUTE IMMEDIATE``, ``BEGIN NOT ATOMIC`` and any
  statement myflames does not recognize are risky (category ``unknown``):
  myflames cannot see what they run.
* UPDATE or DELETE without a top-level WHERE, or with an always-true one, is
  risky. With a WHERE, it is risky when the caller passes the key columns of
  the table (``key_columns``) and WHERE compares none of them; without that
  information it is a write and the reason says the key was not checked.
"""
import csv
import datetime
import getpass
import io
import json
import re
import sys
import time
import uuid

from .live import DASH_CONTROL, TOKEN, TOKEN_NO_BACKSLASH
from .workspace_store import (AUDIT_DEFAULT_RETENTION_DAYS as DEFAULT_RETENTION_DAYS,
                              AUDIT_MAX_RETENTION_DAYS as MAX_RETENTION_DAYS, CONNECTION_TYPES)

KINDS = ("read", "write", "risky")
_RANK = {"read": 0, "write": 1, "risky": 2}
DEFAULT_CONNECTION_TYPE = "production"
ACTIONS = ("allow", "confirm", "refuse")
#: Categories whose writes change only this session or take short row locks:
#: their confirmation uses the lighter, primary tone (db-operations skill, section 6).
LIGHT_CATEGORIES = ("session", "transaction", "locking_read")

#: SET SESSION of these only changes how this session reads or shows results,
#: so it is classified as a read.
SESSION_READ_VARIABLES = frozenset((
    "character_set_client", "character_set_connection", "character_set_results", "collation_connection",
    "time_zone", "lc_time_names", "max_execution_time", "max_statement_time", "sql_select_limit",
    "transaction_isolation", "tx_isolation",
    "div_precision_increment", "group_concat_max_len", "profiling", "profiling_history_size",
    "optimizer_trace", "optimizer_trace_features", "optimizer_trace_limit", "optimizer_trace_offset",
    "optimizer_trace_max_mem_size", "end_markers_in_json", "explain_format", "explain_json_format_version",
    "information_schema_stats_expiry", "optimizer_switch", "optimizer_search_depth", "optimizer_prune_level",
    "eq_range_index_dive_limit", "cte_max_recursion_depth", "max_recursive_iterations",
    "net_read_timeout", "net_write_timeout", "wait_timeout", "interactive_timeout",
    "lock_wait_timeout", "innodb_lock_wait_timeout",
))

#: Session variables that change what reaches the binary log or how the
#: server identifies events: replicas and point-in-time recovery depend on them.
SESSION_RISKY_VARIABLES = {
    "sql_log_bin": "With sql_log_bin off, this session's writes skip the binary log, so replicas and "
                   "point-in-time recovery miss them.",
    "binlog_format": "binlog_format changes how this session's writes are logged for replicas.",
    "binlog_row_image": "binlog_row_image changes what row events carry to replicas.",
    "binlog_row_value_options": "binlog_row_value_options changes what row events carry to replicas.",
    "binlog_annotate_row_events": "binlog_annotate_row_events changes what the binary log records.",
    "binlog_direct_non_transactional_updates": "This changes how non-transactional writes reach the binary log.",
    "pseudo_thread_id": "pseudo_thread_id is meant for replaying binary logs.",
    "pseudo_slave_mode": "pseudo_slave_mode is meant for replaying binary logs.",
    "pseudo_replica_mode": "pseudo_replica_mode is meant for replaying binary logs.",
    "gtid_next": "gtid_next sets the GTID of this session's next transaction by hand, which changes what "
                 "replicas apply.",
    "gtid_seq_no": "gtid_seq_no sets the next GTID sequence number by hand, which changes what replicas apply.",
    "gtid_domain_id": "gtid_domain_id changes the GTID domain of this session's writes.",
    "server_id": "A session server_id changes the origin recorded for this session's writes.",
    "skip_replication": "skip_replication marks this session's writes so replicas set to skip them never apply "
                        "them.",
    "wsrep_on": "wsrep_on off keeps this session's writes from replicating to the other Galera nodes.",
}

#: Session variables myflames sets to protect an editor session (read-only
#: transactions, safe updates): changing them, even for one statement with
#: MariaDB SET STATEMENT, is a write.
SESSION_GUARD_VARIABLES = frozenset(("transaction_read_only", "tx_read_only", "sql_safe_updates"))

AMBIGUOUS_REASON = ("A backslash before a quote is read differently when sql_mode has NO_BACKSLASH_ESCAPES, "
                    "so the statements could split in two ways; both readings were classified.")
_IDENT = r"(?:`\d+`|[A-Z_$][A-Z0-9_$]*)"
_VERSIONED_OPEN = re.compile(r"/\*M?!(?:\d{6}(?=\s)|\d{5})?")
#: A line whose first word starts with "delimiter" is the client's DELIMITER
#: command in --binary-mode (client/mysql.cc find_command and
#: is_delimiter_command compare only the first 9 characters).
_DELIMITER_COMMAND = re.compile(r"[ \t\v\f\r]*(delimiter[^ \t\r\n]*)", re.I)
_CLIENT_SPACE = " \t\n\r\f\v"
#: Only ASCII white space separates tokens: both lexers read a non-ASCII
#: space such as U+00A0 or U+3000 as an identifier character (a multibyte lead
#: byte), so ``CONCAT\u00a0(`` names another function; str.split() would
#: drop it.
_CODE_SPACE = re.compile("[%s]+" % re.escape(_CLIENT_SPACE))
#: Flags every write sends to the mysql or mariadb client: keep comments (the
#: myflames tag) and turn the client's backslash commands off (--binary-mode
#: disables every command except \C and DELIMITER in non-interactive use, on
#: MySQL and MariaDB clients alike).
WRITE_CLIENT_FLAGS = ("--comments", "--binary-mode")
REASON_CLIENT_COMMAND = ("A backslash command (\\%s) outside quotes is run by the mysql client itself, not sent "
                         "to the server.")
REASON_DASH_CONTROL = ("'--' before a control character is code to the client and a comment to the server, so the "
                       "statement boundaries are uncertain.")
REASON_DELIMITER = ("A DELIMITER line without a usable delimiter (none, or one with a backslash) leaves the "
                    "statement boundaries uncertain.")


def _delimiter_argument(sql, at):
    """The argument of a DELIMITER command line starting at *at*, read like
    the client's get_arg: (delimiter or None, end of line). A quoted argument
    ends at its closing quote; an unquoted one at a space (not a tab)."""
    newline = sql.find("\n", at)
    stop = len(sql) if newline < 0 else newline
    rest = sql[at:stop]
    i = 0
    while i < len(rest) and rest[i] in _CLIENT_SPACE:
        i += 1
    if i < len(rest) and rest[i] in "'\"`":
        quote = rest[i]
        close = rest.find(quote, i + 1)
        value = rest[i + 1:] if close < 0 else rest[i + 1:close]
    else:
        space = rest.find(" ", i)
        value = rest[i:] if space < 0 else rest[i:space]
    return value, stop


# ---- splitting -----------------------------------------------------------------------

def split_statements(sql, backslash_escapes=True, delimiter=";"):
    """Split *sql* into statements like the mysql client.

    Returns a list of dicts: ``text`` (the statement as written, without its
    delimiter), ``start`` and ``end`` (offsets in *sql*), ``code`` (the text
    with comments blanked, strings as ``''``, quoted identifiers as numbered
    placeholders such as ``\\`0\\```, executable comment bodies kept as code),
    ``idents`` (the quoted identifier names, by placeholder number),
    ``unclosed`` (an unterminated quote or comment, or an unusable DELIMITER),
    ``issues`` (reasons the boundaries are uncertain: a client backslash
    command, "--" before a control character), and ``delimiter``.
    ``DELIMITER x`` lines are client commands, parsed like the client
    (quotes stripped, an unquoted argument ends at a space): they change the
    delimiter and are never part of a statement. ``\\g`` and ``\\G`` outside
    quotes end a statement, as the client does without --binary-mode. A
    statement that is only comments is dropped. *backslash_escapes* False
    reads strings as under sql_mode NO_BACKSLASH_ESCAPES. *delimiter* is the
    delimiter in effect where *sql* starts (a part of a longer script that
    follows a DELIMITER line).
    """
    if not isinstance(sql, str):
        raise ValueError("Pass SQL text.")
    if not isinstance(delimiter, str) or not delimiter or "\\" in delimiter:
        raise ValueError("Pass a delimiter of one or more characters without a backslash.")
    pattern = TOKEN if backslash_escapes else TOKEN_NO_BACKSLASH
    out = []
    state = {"start": 0, "code": [], "idents": [], "pending": False, "unclosed": False, "issues": []}
    versioned = False
    n = len(sql)
    pos = 0

    def finish(end, next_start):
        if state["pending"] or state["issues"]:
            raw = sql[state["start"]:end]
            lead = len(raw) - len(raw.lstrip())
            text = raw.strip()
            out.append({"text": text, "start": state["start"] + lead, "end": state["start"] + lead + len(text),
                        "code": _CODE_SPACE.sub(" ", "".join(state["code"])).strip(" "),
                        "idents": list(state["idents"]),
                        "unclosed": state["unclosed"], "issues": list(state["issues"]), "delimiter": delimiter})
        state.update(start=next_start, code=[], idents=[], pending=False, unclosed=False, issues=[])

    def issue(reason):
        if reason not in state["issues"]:
            state["issues"].append(reason)

    while pos < n:
        match = pattern.search(sql, pos)
        stop = match.start() if match else n
        i = pos
        restart = False
        while i < stop:
            if (i == 0 or sql[i - 1] == "\n") and not state["pending"] and not versioned:
                command = _DELIMITER_COMMAND.match(sql, i)
                if command:
                    value, line_end = _delimiter_argument(sql, command.end())
                    if value and "\\" not in value:
                        delimiter = value
                    else:
                        # The client prints an error and keeps the old delimiter.
                        state["unclosed"] = True
                        issue(REASON_DELIMITER)
                    pos = line_end
                    state.update(start=pos, code=[], idents=[])
                    restart = True
                    break
            if versioned and sql.startswith("*/", i):
                state["code"].append(" ")
                versioned = False
                i += 2
                continue
            if sql.startswith(delimiter, i):
                finish(i, i + len(delimiter))
                i += len(delimiter)
                continue
            ch = sql[i]
            if ch == "\\":
                following = sql[i + 1] if i + 1 < n else ""
                if following in ("g", "G"):
                    finish(i, i + 2)
                    i += 2
                    continue
                if following != "N":
                    # \N is NULL; any other backslash command belongs to the client.
                    issue(REASON_CLIENT_COMMAND % (following or ""))
            elif ch in "'\"`" or sql.startswith("/*", i):
                # TOKEN matched no closed string, identifier or comment here.
                state["unclosed"] = True
            elif ch == "-" and DASH_CONTROL.match(sql, i):
                issue(REASON_DASH_CONTROL)
            state["code"].append(ch)
            if not ch.isspace():
                state["pending"] = True
            i += 1
        if restart:
            continue
        if not match:
            break
        token = match.group()
        if token.startswith("/*+") and "\\" in token:
            # The client runs backslash commands inside optimizer hints (they are
            # not comments to its add_line: SSC_HINT), e.g. /*+ \C latin1 */.
            at = token.index("\\")
            issue(REASON_CLIENT_COMMAND % (token[at + 1:at + 2] or ""))
        if token.startswith("/*"):
            opener = _VERSIONED_OPEN.match(token)
            if opener and not versioned:
                # The body of an executable comment is code: scan it (a
                # delimiter inside ends the statement, as in the client).
                state["code"].append(" ")
                versioned = True
                pos = match.start() + opener.end()
                continue
            state["code"].append(" ")
        elif token[0] in "-#":
            state["code"].append(" ")
        elif token[0] == "`":
            state["code"].append("`%d`" % len(state["idents"]))
            state["idents"].append(token[1:-1].replace("``", "`"))
            state["pending"] = True
        else:
            state["code"].append("''")
            state["pending"] = True
        pos = match.end()
    if versioned:
        state["unclosed"] = True
    finish(n, n)
    return out


# ---- classification -----------------------------------------------------------------

def _verdict(kind, category, reason=None):
    return {"kind": kind, "category": category, "reasons": [reason] if reason else []}


def _worst(verdicts, default=None):
    verdicts = [v for v in verdicts if v]
    if not verdicts:
        return default or _verdict("read", "read")
    # Within the same kind, a category confirmed in the danger tone wins.
    worst = max(verdicts, key=lambda v: (_RANK[v["kind"]], v["category"] not in LIGHT_CATEGORIES))
    reasons = []
    for v in sorted(verdicts, key=lambda v: -_RANK[v["kind"]]):
        for r in v["reasons"]:
            if r not in reasons:
                reasons.append(r)
    return {"kind": worst["kind"], "category": worst["category"], "reasons": reasons}


def _flat(code):
    """*code* with everything inside parentheses blanked (the parentheses stay)."""
    out, depth = [], 0
    for ch in code:
        if ch == "(":
            out.append("(" if depth == 0 else " ")
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
            out.append(")" if depth == 0 else " ")
        else:
            out.append(ch if depth == 0 else " ")
    return "".join(out)


def _name(token, idents):
    m = re.match(r"`(\d+)`$", token)
    if m:
        index = int(m.group(1))
        return idents[index] if index < len(idents) else ""
    return token


_OBJECT_WORDS = ("TABLE", "TABLES", "INDEX", "VIEW", "DATABASE", "SCHEMA", "USER", "ROLE", "PROCEDURE",
                 "FUNCTION", "TRIGGER", "EVENT", "SERVER", "TABLESPACE", "LOGFILE", "SEQUENCE", "INSTANCE",
                 "PACKAGE", "RESOURCE", "UNDO", "SPATIAL")


def _verb(upper):
    words = re.findall(r"[A-Z_]+|@@[A-Z_]+|\(", upper)
    words = [w for w in words if w != "("]
    if not words:
        return ""
    first = words[0]
    if first in ("CREATE", "ALTER", "DROP") and len(words) > 1:
        for w in words[1:8]:
            if w in _OBJECT_WORDS:
                return "%s %s" % (first, w)
        return first
    if first == "SET" and len(words) > 1:
        scope = words[1].lstrip("@")
        if scope in ("GLOBAL", "PERSIST", "PERSIST_ONLY", "SESSION", "LOCAL", "PASSWORD", "TRANSACTION",
                     "NAMES", "ROLE", "DEFAULT", "STATEMENT", "RESOURCE"):
            return "SET " + scope
        return "SET"
    if first in ("EXPLAIN", "DESCRIBE", "DESC"):
        return first + " ANALYZE" if "ANALYZE" in words[1:4] else first
    if first == "ANALYZE":
        return "ANALYZE TABLE" if len(words) > 1 and words[1] in ("TABLE", "TABLES", "NO_WRITE_TO_BINLOG",
                                                                 "LOCAL") else first
    if first in ("START", "STOP", "RESET", "CHANGE", "FLUSH", "LOCK", "UNLOCK", "LOAD", "XA", "RENAME",
                 "PURGE", "INSTALL", "UNINSTALL") and len(words) > 1:
        return "%s %s" % (first, words[1])
    if first == "KILL":
        return "KILL QUERY" if len(words) > 1 and words[1] == "QUERY" else "KILL"
    return first


# Common native readers present in MySQL 8.0.11 and MariaDB 10.11.1
# (sql/item_create.cc and sql/lex.h at those tags). Keep this deliberately
# conservative: an unknown name can resolve to a stored function or a UDF
# (MySQL 9.7 sql/parse_tree_items.cc:269-294). Do not add a version-specific
# name here: on another supported server it could resolve to a stored routine.
# Qualified and quoted names never inherit this exemption. This is a lexical
# safety gate, not a proof about arbitrary SQL or a substitute for privileges.
_READ_FUNCTIONS = frozenset("""
ABS ACOS ASIN ATAN ATAN2 AVG BIT_AND BIT_OR BIT_XOR CEIL CEILING CHAR
CHAR_LENGTH CHARACTER_LENGTH COALESCE CONCAT CONCAT_WS CONNECTION_ID CONV
CONVERT COS COT COUNT CRC32 CURDATE CURRENT_DATE CURRENT_TIME
CURRENT_TIMESTAMP CURRENT_USER CURTIME DATABASE DATE DATE_ADD DATE_FORMAT
DATE_SUB DATEDIFF DAY DAYNAME DAYOFMONTH DAYOFWEEK DAYOFYEAR DEGREES EXP EXTRACT
FIELD FIND_IN_SET FLOOR FORMAT FOUND_ROWS FROM_BASE64 FROM_DAYS FROM_UNIXTIME
GREATEST GROUP_CONCAT HEX HOUR IF IFNULL INSERT INSTR ISNULL JSON_ARRAY
JSON_ARRAYAGG JSON_CONTAINS JSON_CONTAINS_PATH JSON_EXTRACT JSON_KEYS
JSON_LENGTH JSON_OBJECT JSON_OBJECTAGG JSON_QUOTE JSON_TYPE JSON_UNQUOTE
JSON_VALID LCASE LEAST LEFT LENGTH LN LOCATE LOG LOG10 LOG2 LOWER LPAD LTRIM
MAKEDATE MAKETIME MAX MD5 MICROSECOND MID MIN MINUTE MOD MONTH MONTHNAME NOW
NULLIF OCT OCTET_LENGTH ORD PI POSITION POW POWER QUARTER QUOTE RADIANS RAND
REPEAT REPLACE REVERSE RIGHT ROUND RPAD RTRIM SCHEMA SECOND SESSION_USER SHA
SHA1 SHA2 SIGN SIN SLEEP SPACE SQRT STD STDDEV STDDEV_POP STDDEV_SAMP STRCMP
STR_TO_DATE SUBDATE SUBSTR SUBSTRING SUBSTRING_INDEX SUM SYSDATE SYSTEM_USER
TAN TIME TIME_FORMAT TIME_TO_SEC TIMEDIFF TIMESTAMP TIMESTAMPADD TIMESTAMPDIFF
TO_BASE64 TO_DAYS TO_SECONDS TRIM TRUNCATE UCASE UNHEX UNIX_TIMESTAMP UPPER USER
UTC_DATE UTC_TIME UTC_TIMESTAMP UUID UUID_SHORT VAR_POP VAR_SAMP VARIANCE VERSION WEEK WEEKDAY
WEEKOFYEAR YEAR YEARWEEK CAST ROW ROW_NUMBER RANK DENSE_RANK LAG LEAD
FIRST_VALUE LAST_VALUE NTH_VALUE NTILE CUME_DIST PERCENT_RANK
""".split())

# These names depend on IGNORE_SPACE when separated from their opening
# parenthesis (MySQL sql/sql_lex.cc:1618-1633; both engines sql/lex.h).
_SPACE_SENSITIVE_FUNCTIONS = frozenset("""
BIT_AND BIT_OR BIT_XOR CAST COUNT CUME_DIST CURDATE CURTIME
DATE_ADD DATE_FORMAT DATE_SUB DENSE_RANK EXTRACT FIRST_VALUE GROUP_CONCAT JSON_ARRAYAGG
JSON_OBJECTAGG LAG LEAD MAX MID MIN NOW NTH_VALUE
NTILE PERCENT_RANK POSITION RANK SESSION_USER STD STDDEV STDDEV_POP
STDDEV_SAMP SUBDATE SUBSTR SUBSTRING SUM SYSDATE SYSTEM_USER TRIM
VARIANCE VAR_POP VAR_SAMP
""".split())

# These grammar words can precede parentheses without calling a function.
# Only unqualified, unquoted words qualify; db.IN(...) remains unknown.
_SELECT_PARENS = frozenset("""
SELECT FROM WHERE HAVING ON USING AS IN EXISTS NOT AND OR XOR ALL ANY SOME
DISTINCT OVER PARTITION BY ORDER GROUP LIMIT OFFSET UNION INTERSECT EXCEPT
JOIN INNER LEFT RIGHT CROSS NATURAL OUTER STRAIGHT_JOIN WHEN THEN ELSE
CASE VALUES VALUE ROWS RANGE BETWEEN FOR FORCE USE IGNORE INDEX KEY
DECIMAL NUMERIC SIGNED UNSIGNED BINARY NCHAR VARCHAR CHARSET
""".split())
#: Identifier characters outside quotes: both lexers accept any byte that
#: starts a multibyte character (MySQL strings/sql_chars.cc my_ismb1st,
#: MariaDB mysys/charset.c is_multi_byte_ident), so with utf8mb4 every
#: non-ASCII character counts, symbols such as € included, not only \w.
_IDENT_CHAR = r"[\w$\u0080-\U0010FFFF]"
_FUNCTION_TOKEN = re.compile(r"`\d+`|''|%s+|[^\s]" % _IDENT_CHAR, re.UNICODE)
_ADMIN_FUNCTION_PREFIXES = ("GROUP_REPLICATION_", "ASYNCHRONOUS_CONNECTION_FAILOVER_",
                            "KEYRING_", "VERSION_TOKENS_", "SERVICE_GET_", "SERVICE_RELEASE_")


def _select_function_checks(upper, idents):
    """Inspect calls in the shared scanner's code, never strings/comments.

    Parenthesis pairs make CTE column declarations cheap to recognize without
    swallowing calls inside their bodies. Strings are empty quoted tokens;
    treating one followed by '(' as unknown also covers ANSI_QUOTES function
    names, which the shared scanner otherwise reads as strings.
    """
    matches = list(_FUNCTION_TOKEN.finditer(upper))
    tokens = [match.group() for match in matches]
    pairs, stack = {}, []
    for i, token in enumerate(tokens):
        if token == "(":
            stack.append(i)
        elif token == ")" and stack:
            pairs[stack.pop()] = i
    found = []
    for i, token in enumerate(tokens):
        quoted = token.startswith("`") or token == "''"
        if token.startswith("`"):
            name = idents[int(token[1:-1])].upper()
        else:
            name = token
        qualified = i > 0 and tokens[i - 1] == "."
        # In MariaDB ORACLE mode seq.NEXTVAL is an expression, with no ().
        # The mode is not known here, so a column with this name is ambiguous.
        if qualified and token == "''":
            found.append(_verdict("risky", "unknown", "A quoted identifier may name a sequence expression under ANSI_QUOTES."))
        if qualified and name == "NEXTVAL":
            found.append(_verdict("risky", "sequence", "A qualified NEXTVAL can advance a MariaDB sequence."))
        if i + 1 >= len(tokens) or tokens[i + 1] != "(":
            continue
        if not quoted and not re.match(r"^%s+$" % _IDENT_CHAR, token):
            continue
        end = pairs.get(i + 1)
        # WITH c(cols) AS (...) and derived-table aliases t(cols) declare
        # names, even when quoted. Calls within their bodies are still scanned.
        if not qualified and ((end is not None and tokens[end + 1:end + 3] == ["AS", "("]) or
                              (i > 0 and tokens[i - 1] in ("AS", ")"))):
            continue
        if not qualified and not quoted:
            if name in _SELECT_PARENS:
                continue
            if name in ("GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS", "NEXTVAL", "SETVAL"):
                # The existing SELECT checks supply their precise category.
                continue
            if name == "LAST_INSERT_ID":
                if i + 2 < len(tokens) and tokens[i + 2] == ")":
                    continue
                found.append(_verdict("write", "session", "LAST_INSERT_ID with an argument changes session state."))
                continue
            if name in _READ_FUNCTIONS and (name not in _SPACE_SENSITIVE_FUNCTIONS or
                                           matches[i].end() == matches[i + 1].start()):
                continue
        if not qualified and not quoted and name.startswith(_ADMIN_FUNCTION_PREFIXES):
            found.append(_verdict("risky", "server", "This administrative function may change server state."))
        else:
            found.append(_verdict("risky", "unknown", ("BENCHMARK runs an expression many times and can load the server."
                                   if not qualified and not quoted and name == "BENCHMARK" else
                                   "This function is not a verified native reader; "
                                   "it may run a stored routine or a user-defined function with side effects.")))
    return found


def _select_checks(upper, idents):
    found = _select_function_checks(upper, idents)
    if re.search(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", upper):
        found.append(_verdict("write", "file", "INTO OUTFILE or DUMPFILE writes a file on the database server host "
                                               "(it needs the FILE privilege)."))
    elif re.search(r"\bINTO\b", upper):
        found.append(_verdict("write", "session", "INTO assigns the result to variables."))
    if re.search(r"\bFOR\s+(?:UPDATE|SHARE)\b|\bLOCK\s+IN\s+SHARE\s+MODE\b", upper):
        found.append(_verdict("write", "locking_read",
                              "A locking read (FOR UPDATE, FOR SHARE, LOCK IN SHARE MODE) locks the rows it reads "
                              "until the transaction ends; other sessions that change them wait."))
    if ":=" in upper:
        found.append(_verdict("write", "session", "The := operator assigns user variables."))
    if re.search(r"\b(?:GET_LOCK|RELEASE_LOCK|RELEASE_ALL_LOCKS)\s*\(", upper):
        found.append(_verdict("write", "lock", "This built-in takes or releases a named user lock."))
    if re.search(r"\b(?:NEXTVAL|SETVAL)\s*\(|\bNEXT\s+VALUE\s+FOR\b", upper):
        found.append(_verdict("write", "sequence", "NEXTVAL, SETVAL and NEXT VALUE FOR change a sequence."))
    return _worst(found, _verdict("read", "read"))


def _always_true(where):
    text = " ".join(where.split())
    return bool(re.match(r"^\(?\s*(?:TRUE|NOT\s+FALSE|NOT\s+0|[1-9]\d*|''\s*=\s*''|(\d+)\s*=\s*\1)\s*\)?$", text))


#: Longest WHERE clause the key check reads; a longer one is judged risky.
MAX_WHERE_CHECK = 20000
_WHERE_TOKEN = re.compile(r"\s*(?:(`\d+`|[A-Z_$][A-Z0-9_$]*)|(<=>|<=|>=|<>|!=|=|<|>)|(''|-?\d+(?:\.\d+)?|\?|:[A-Z_]\w*|"
                          r"@[A-Z0-9_$.]+|@''|0X[0-9A-F]+|X'')|(\.)|(\()|(\))|(\S))")
_LITERAL_WORDS = ("NULL", "TRUE", "FALSE")


def _key_conditions(where, idents):
    """Key columns WHERE pins to values: ``col = literal``, ``literal = col``,
    ``col <=> literal`` and ``col IN (...)``, not negated. One linear pass."""
    tokens = []
    for m in _WHERE_TOKEN.finditer(where):
        if m.group(1):
            word = m.group(1)
            tokens.append(("literal" if word in _LITERAL_WORDS else "name", word))
        elif m.group(2):
            tokens.append(("op", m.group(2)))
        elif m.group(3):
            tokens.append(("literal", m.group(3)))
        elif m.group(4):
            tokens.append(("dot", "."))
        elif m.group(5):
            tokens.append(("open", "("))
        elif m.group(6):
            tokens.append(("close", ")"))
        else:
            tokens.append(("other", m.group(7)))
    # Fold qualified names (t.col, db.t.col) into their last part.
    folded = []
    for kind, value in tokens:
        if folded and folded[-1][0] == "dot" and kind == "name" and len(folded) > 1 and folded[-2][0] == "name":
            folded.pop()
            folded[-1] = ("name", value)
        else:
            folded.append((kind, value))
    used = set()
    for i, (kind, value) in enumerate(folded):
        if kind != "name" or value in ("AND", "OR", "XOR", "NOT", "IS", "IN", "LIKE", "BETWEEN"):
            continue
        negated = i > 0 and folded[i - 1] == ("name", "NOT") or \
            i > 1 and folded[i - 1][0] == "open" and folded[i - 2] == ("name", "NOT")
        if negated:
            continue
        nxt = folded[i + 1] if i + 1 < len(folded) else None
        after = folded[i + 2] if i + 2 < len(folded) else None
        prev = folded[i - 1] if i > 0 else None
        before = folded[i - 2] if i > 1 else None
        if nxt and nxt[0] == "op" and nxt[1] in ("=", "<=>") and after and after[0] == "literal":
            used.add(_name(value, idents).lower())
        elif nxt == ("name", "IN") and after and after[0] == "open" and i + 3 < len(folded) and \
                folded[i + 3][0] == "literal":
            used.add(_name(value, idents).lower())
        elif prev and prev[0] == "op" and prev[1] in ("=", "<=>") and before and before[0] == "literal" and \
                not (i > 2 and folded[i - 3] == ("name", "NOT")):
            used.add(_name(value, idents).lower())
    return used


def _update_delete(verb, upper, idents, key_columns):
    flat = _flat(upper)
    where = re.search(r"\bWHERE\b", flat)
    does = "changes" if verb == "UPDATE" else "deletes"
    if not where:
        return _verdict("risky", "dml", "%s without a WHERE clause %s every row of the table." % (verb, does))
    rest_flat = flat[where.end():]
    end = re.search(r"\b(?:ORDER\s+BY|LIMIT|RETURNING)\b", rest_flat)
    condition = upper[where.end():where.end() + end.start()] if end else upper[where.end():]
    if _always_true(condition):
        return _verdict("risky", "dml", "The WHERE clause is always true, so %s %s every row." % (verb, does))
    if key_columns is None:
        return _verdict("risky", "dml", "%s %s the rows WHERE selects, and myflames could not check whether WHERE "
                                        "pins a key column, so it may %s many rows." % (verb, does, does[:-1]))
    keys = {str(k).lower() for k in key_columns if k}
    if len(condition) > MAX_WHERE_CHECK:
        return _verdict("risky", "dml", "The WHERE clause is too long for myflames to check its key columns.")
    if re.search(r"\b(?:OR|XOR)\b|\|\|", condition):
        return _verdict("risky", "dml", "The WHERE clause uses OR, so myflames cannot tell how many rows %s %s."
                        % (verb, does))
    used = sorted(_key_conditions(condition, idents) & keys)
    if not used:
        return _verdict("risky", "dml", "The WHERE clause pins no key column (%s) to a value, so %s may %s many rows."
                        % (", ".join(sorted(keys)) or "the table has no key", verb, does[:-1]))
    return _verdict("write", "dml", "%s %s the rows WHERE selects by key column %s." % (verb, does, ", ".join(used)))


_SET_ITEM = re.compile(r"\s*(?:(GLOBAL|PERSIST_ONLY|PERSIST|SESSION|LOCAL)\s+|@@(GLOBAL|PERSIST_ONLY|PERSIST|SESSION|"
                       r"LOCAL)\s*\.\s*|(@@)|(@))?\s*(''|%s(?:\s*\.\s*%s)?|[A-Z0-9_$.]+)\s*(?::=|=)" % (_IDENT, _IDENT))


# A function call (a stored function can write; GET_LOCK takes a named lock),
# a := assignment, a subquery (SELECT or TABLE) or a MariaDB sequence advance (NEXT VALUE FOR is
# a general expression there) in a value. Any call counts, pure or not: a name
# of identifier characters (_IDENT_CHAR: letters, digits, _, $ and any
# non-ASCII character), a quoted name (`f`, or "f" under ANSI_QUOTES, which the
# scanner masks as the string '') or a string followed by "(". The value is
# the scanner's code: strings are '' and quoted identifiers `N`, so a
# parenthesis inside a string literal never counts.
_SET_VALUE_WRITES = re.compile(r"(?:%s|[`'\"])\s*\(|:=|\b(?:SELECT|TABLE)\b|\bNEXT\s+VALUE\s+FOR\b" % _IDENT_CHAR,
                               re.I)
# myflames' own text value (variables._text_literal via perf_common.literal):
# exactly one hex string converted to utf8mb4 and nothing else. Only this form,
# anchored to the whole value, is exempt from the call test above.
_SET_TEXT_LITERAL = re.compile(r"\s*CONVERT\(\s*X''\s+USING\s+UTF8MB4\s*\)\s*", re.I)
_QUALIFIED_PART = re.compile(r"\.\s*(`\d+`|''|[\w$]+)")


def _set_value_writes(value, idents):
    """True when a SET value (scanner code after ``=``) can change state."""
    if _SET_TEXT_LITERAL.fullmatch(value):
        return False
    if _SET_VALUE_WRITES.search(value):
        return True
    # In MariaDB sql_mode=ORACLE, seq.NEXTVAL (quoted or not) advances a
    # sequence without parentheses (sql_lex.cc create_item_ident); a quoted
    # part after a dot may be an ANSI_QUOTES name, so it counts too.
    for part in _QUALIFIED_PART.finditer(value):
        token = part.group(1)
        if token == "''" or _name(token, idents).upper() == "NEXTVAL":
            return True
    return False


def _set_items(rest, idents, scoped=True):
    """Verdicts for a comma list of SET assignments (*rest* is after SET).

    NAMES, CHARACTER SET and CHARSET are one item of the list, so
    ``SET NAMES utf8mb4, GLOBAL read_only = 0`` is judged by every item.
    *scoped* False reads the list of MariaDB SET STATEMENT, whose variables
    last one statement: only the risky ones count there.
    """
    found = []
    flat_rest = _flat(rest)
    cuts = [-1] + [i for i, ch in enumerate(flat_rest) if ch == ","] + [len(rest)]
    for a, b in zip(cuts, cuts[1:]):
        piece = rest[a + 1:b]
        if not piece.strip():
            found.append(_verdict("risky", "unknown", "myflames could not read this SET statement."))
            continue
        if re.match(r"\s*(?:NAMES|CHARACTER\s+SET|CHARSET)\b", piece):
            found.append(_verdict("read", "session", "Changes the character set of this session only."))
            continue
        m = _SET_ITEM.match(piece)
        if not m:
            found.append(_verdict("risky", "unknown", "myflames could not read this SET statement."))
            continue
        scope = m.group(1) or m.group(2)
        variable = _name(re.split(r"\s*\.\s*", m.group(5))[-1], idents).lower()
        if not scoped:
            # MariaDB SET STATEMENT values are expressions too (sql_yacc.yy
            # set_stmt_option: set_expr_or_default). The server refuses stored
            # functions and table references there, but built-ins such as
            # GET_LOCK and := assignments run; flagging any call stays safe.
            if _set_value_writes(piece[m.end():], idents):
                found.append(_verdict("write", "session", "The value calls a function, assigns a variable or runs a "
                                                          "subquery, which can change data."))
            if variable in SESSION_RISKY_VARIABLES:
                found.append(_verdict("risky", "replication", SESSION_RISKY_VARIABLES[variable]))
            elif variable in SESSION_GUARD_VARIABLES:
                found.append(_verdict("write", "session", "SET STATEMENT changes %s for the statement, which lifts "
                                                          "a protection of this session." % variable))
            elif scope or m.group(3) or m.group(4):
                found.append(_verdict("risky", "unknown", "myflames could not read this SET STATEMENT."))
            continue
        if m.group(4):
            found.append(_verdict("write", "session", "Assigns a user variable in this session."))
        elif scope == "GLOBAL":
            found.append(_verdict("risky", "global", "SET GLOBAL changes %s for the whole server until it restarts."
                                  % variable))
        elif scope == "PERSIST":
            found.append(_verdict("risky", "persist", "SET PERSIST changes %s now and writes it to mysqld-auto.cnf, "
                                                      "so it also applies after a restart." % variable))
        elif scope == "PERSIST_ONLY":
            found.append(_verdict("risky", "persist", "SET PERSIST_ONLY writes %s to mysqld-auto.cnf for the next "
                                                      "restart." % variable))
        elif variable in SESSION_RISKY_VARIABLES:
            found.append(_verdict("risky", "replication", SESSION_RISKY_VARIABLES[variable]))
        elif variable in SESSION_READ_VARIABLES:
            if _set_value_writes(piece[m.end():], idents):
                found.append(_verdict("write", "session", "The value calls a function, assigns a variable or runs a "
                                                          "subquery, which can change data."))
            else:
                found.append(_verdict("read", "session", "Changes %s for this session only." % variable))
        else:
            found.append(_verdict("write", "session", "Changes %s for this session only." % variable))
    return found


def _set_checks(upper, idents):
    rest = upper[re.match(r"\s*SET\b", upper).end():]
    if re.match(r"\s*PASSWORD\b", rest):
        return _verdict("risky", "account", "SET PASSWORD changes an account password.")
    if re.match(r"\s*DEFAULT\s+ROLE\b", rest):
        return _verdict("risky", "account", "SET DEFAULT ROLE changes which roles an account gets at login.")
    if re.match(r"\s*ROLE\b", rest):
        return _verdict("write", "session", "SET ROLE changes the roles active in this session.")
    if re.match(r"\s*RESOURCE\s+GROUP\b", rest):
        return _verdict("write", "server", "SET RESOURCE GROUP moves threads to another resource group.")
    m = re.match(r"\s*(?:(GLOBAL|SESSION|LOCAL|PERSIST|PERSIST_ONLY)\s+)?TRANSACTION\b", rest)
    if m:
        if m.group(1) in ("GLOBAL", "PERSIST", "PERSIST_ONLY"):
            return _verdict("risky", "global", "SET %s TRANSACTION changes the default for every new session."
                            % m.group(1))
        if re.search(r"\bREAD\s+WRITE\b", rest):
            return _verdict("write", "session", "READ WRITE lets this session's next transactions change data.")
        return _verdict("read", "session", "Sets this session's next transactions only.")
    if re.match(r"\s*STATEMENT\b", rest):
        found = re.search(r"\bFOR\b", _flat(upper))
        if not found:
            return _verdict("risky", "unknown", "myflames could not read this SET STATEMENT.")
        statement_at = re.match(r"\s*STATEMENT\b", rest).end() + (len(upper) - len(rest))
        variables = _set_items(upper[statement_at:found.start()], idents, scoped=False)
        inner = _classify_code(upper[found.end():], idents, None)
        inner = _worst([inner] + variables)
        inner["reasons"] = inner["reasons"] + ["SET STATEMENT changes variables for this one statement."]
        return inner
    return _worst(_set_items(rest, idents), _verdict("risky", "unknown", "myflames could not read this SET statement."))


_REPLICATION_WORDS = r"(?:REPLICA|SLAVE|SLAVES|GROUP_REPLICATION|ALL)"


def _classify_code(upper, idents, key_columns):
    """Verdict for one statement's masked code, upper-cased."""
    upper = upper.strip()
    text = upper
    lead = re.match(r"^[\s(]*([A-Z_]+)", text)
    if not lead:
        return _verdict("risky", "unknown", "myflames could not tell what this statement does.")
    word = lead.group(1)
    after = text[lead.end():]
    nxt = re.match(r"\s*([A-Z_]+)", after)
    second = nxt.group(1) if nxt else ""

    if word in ("SELECT", "TABLE", "VALUES"):
        return _select_checks(upper, idents)
    if word == "WITH":
        flat = _flat(text)
        main = re.search(r"\b(SELECT|TABLE|VALUES|UPDATE|DELETE|INSERT|REPLACE)\b", flat[lead.end():])
        if not main:
            return _select_checks(upper, idents)
        verb = main.group(1)
        at = lead.end() + main.start()
        if verb in ("UPDATE", "DELETE"):
            return _update_delete(verb, text[at:], idents, key_columns)
        if verb in ("INSERT", "REPLACE"):
            return _verdict("write", "dml", "%s adds rows." % verb)
        return _select_checks(upper, idents)
    if word in ("SHOW", "HELP", "USE", "CHECKSUM", "SIGNAL", "RESIGNAL", "GET", "HANDLER", "DEALLOCATE"):
        return _verdict("read", "session" if word in ("USE", "DEALLOCATE") else "read")
    if word == "PREPARE":
        return _verdict("read", "session", "PREPARE only prepares a statement; EXECUTE runs it.")
    if word in ("EXPLAIN", "DESCRIBE", "DESC"):
        rest = after
        analyzed = False
        while True:
            m = re.match(r"\s*(ANALYZE\b|EXTENDED\b|PARTITIONS\b|FORMAT\s*=\s*[A-Z_]+|INTO\s+@\S+)", rest)
            if not m:
                break
            if m.group(1).startswith("ANALYZE"):
                analyzed = True
            if m.group(1).startswith("INTO"):
                return _verdict("write", "session", "EXPLAIN ... INTO assigns the plan to a user variable.")
            rest = rest[m.end():]
        if analyzed and rest.strip():
            inner = _classify_code(rest, idents, key_columns)
            inner["reasons"] = ["EXPLAIN ANALYZE runs the statement."] + inner["reasons"]
            return inner
        return _verdict("read", "read")
    if word == "ANALYZE":
        if re.match(r"\s*(?:SELECT|UPDATE|DELETE|INSERT|REPLACE|WITH|FORMAT\b|\()", after):
            inner_text = re.sub(r"^\s*FORMAT\s*=\s*[A-Z_]+", "", after)
            inner = _classify_code(inner_text, idents, key_columns)
            inner["reasons"] = ["MariaDB ANALYZE runs the statement."] + inner["reasons"]
            return inner
        return _verdict("write", "maintenance", "ANALYZE TABLE updates index statistics.")
    if word == "CHECK":
        return _verdict("write", "maintenance", "CHECK TABLE is table maintenance, not a plain read.")
    if word == "SET":
        return _set_checks(text, idents)
    if word == "RESET":
        if second == "PERSIST":
            return _verdict("risky", "persist", "RESET PERSIST removes settings from mysqld-auto.cnf.")
        if second == "QUERY":
            return _verdict("write", "server", "RESET QUERY CACHE empties the query cache.")
        return _verdict("risky", "replication", "RESET %s discards replication or binary log state." % second)
    if word == "KILL":
        if second == "QUERY":
            return _verdict("risky", "kill", "KILL QUERY stops another session's running statement.")
        return _verdict("risky", "kill", "KILL ends another session and rolls back its open transaction.")
    if word == "START":
        if second == "TRANSACTION":
            if re.search(r"\bREAD\s+WRITE\b", after):
                return _verdict("write", "transaction", "START TRANSACTION READ WRITE allows changes even when the "
                                                        "session is read-only.")
            return _verdict("read", "transaction")
        if re.match(r"\s*" + _REPLICATION_WORDS + r"\b", after):
            return _verdict("risky", "replication", "START %s changes replication." % second)
        return _verdict("risky", "unknown", "myflames could not tell what this START statement does.")
    if word == "STOP":
        if re.match(r"\s*" + _REPLICATION_WORDS + r"\b", after):
            return _verdict("risky", "replication", "STOP %s changes replication." % second)
        return _verdict("risky", "unknown", "myflames could not tell what this STOP statement does.")
    if word == "BEGIN":
        if re.match(r"\s*NOT\s+ATOMIC\b", after):
            return _verdict("risky", "unknown", "A compound statement can run any statement inside it.")
        return _verdict("read", "transaction")
    if word in ("ROLLBACK", "SAVEPOINT", "RELEASE"):
        return _verdict("read", "transaction")
    if word == "COMMIT":
        return _verdict("write", "transaction", "COMMIT makes this session's changes permanent.")
    if word == "XA":
        if second == "RECOVER":
            return _verdict("read", "transaction")
        if second in ("COMMIT", "ROLLBACK"):
            return _verdict("risky", "transaction", "XA %s can end a prepared transaction that another session "
                                                    "started." % second)
        return _verdict("write", "transaction", "XA %s is part of a distributed transaction." % second)
    if word == "CHANGE":
        return _verdict("risky", "replication", "CHANGE %s changes replication settings." % second)
    if word == "PURGE":
        return _verdict("risky", "replication", "PURGE BINARY LOGS deletes binary log files.")
    if word == "CLONE":
        return _verdict("risky", "clone", "CLONE copies or replaces a data directory.")
    if word in ("GRANT", "REVOKE"):
        return _verdict("risky", "account", "%s changes account privileges." % word)
    if word in ("CREATE", "ALTER", "DROP", "RENAME"):
        if word == "DROP" and second == "PREPARE":
            return _verdict("read", "session")
        if re.match(r"\s*(?:OR\s+REPLACE\s+)?(?:USER|ROLE)\b", after):
            return _verdict("risky", "account", "%s %s changes accounts." % (word, re.match(
                r"\s*(?:OR\s+REPLACE\s+)?(USER|ROLE)", after).group(1)))
        if word in ("CREATE", "DROP") and re.match(r"\s*(?:OR\s+REPLACE\s+)?TEMPORARY\s+TABLES?\b", after):
            return _verdict("write", "ddl_temporary", "A temporary table exists only in this session.")
        if word == "ALTER" and second == "INSTANCE":
            return _verdict("risky", "server", "ALTER INSTANCE changes the whole server.")
        if re.match(r"\s*(?:OR\s+REPLACE\s+)?SERVER\b", after):
            return _verdict("risky", "server", "%s SERVER changes a federated server definition." % word)
        return _verdict("risky", "ddl", "DDL changes the schema, commits any open transaction, and can wait for "
                                        "a metadata lock that blocks the table's other users.")
    if word == "TRUNCATE":
        return _verdict("risky", "ddl", "TRUNCATE removes every row and cannot be rolled back.")
    if word == "OPTIMIZE":
        return _verdict("risky", "maintenance", "OPTIMIZE TABLE rebuilds the table.")
    if word == "REPAIR":
        return _verdict("risky", "maintenance", "REPAIR TABLE repairs the table.")
    if word == "LOAD":
        if second in ("DATA", "XML"):
            return _verdict("risky", "bulk", "LOAD %s inserts rows from a file." % second)
        return _verdict("write", "server", "LOAD INDEX INTO CACHE preloads indexes.")
    if word == "CACHE":
        return _verdict("write", "server", "CACHE INDEX assigns indexes to a key cache.")
    if word in ("INSERT", "REPLACE"):
        reason = ("REPLACE deletes a row with the same key, then inserts." if word == "REPLACE"
                  else "INSERT adds rows.")
        return _verdict("write", "dml", reason)
    if word in ("UPDATE", "DELETE"):
        return _update_delete(word, text[text.index(word):], idents, key_columns)
    if word == "LOCK":
        return _verdict("risky", "lock", "LOCK %s makes other sessions that need what it locks wait until it is "
                                         "released." % second)
    if word == "UNLOCK":
        return _verdict("read", "lock")
    if word == "FLUSH":
        if re.search(r"\bTABLES?\b", _flat(text)):
            return _verdict("risky", "server", "FLUSH TABLES waits for running statements and makes new ones "
                                               "wait; WITH READ LOCK blocks every write.")
        return _verdict("write", "server", "FLUSH reloads or resets server state.")
    if word == "DO":
        return _verdict("risky", "unknown", "DO evaluates expressions without returning a result, so it runs only "
                                            "for their effects; a stored function can change data.")
    if word in ("CALL", "EXECUTE"):
        return _verdict("risky", "unknown", "myflames cannot see which statements %s runs." % word)
    if word in ("INSTALL", "UNINSTALL"):
        return _verdict("risky", "server", "%s loads or removes a plugin or component." % word)
    if word in ("SHUTDOWN", "RESTART"):
        return _verdict("risky", "server", "%s stops the server." % word)
    if word == "BINLOG":
        return _verdict("risky", "server", "BINLOG applies binary log events.")
    if word == "IMPORT":
        return _verdict("risky", "server", "IMPORT TABLE adds tables from files.")
    if word == "BACKUP":
        return _verdict("risky", "server", "BACKUP takes server-wide backup locks.")
    return _verdict("risky", "unknown", "myflames does not recognize %s, so it treats it as a risky write." % word)


REASON_MULTI = ("The client sends this as one request and the server runs each ';'-separated statement in it; "
                "the worst one counts.")


def _classify_statement(stmt, key_columns):
    """Verdict for one client statement. Under a DELIMITER other than ';' the
    text can hold several server statements (the client connects with
    CLIENT_MULTI_STATEMENTS), so each ';'-separated part is judged."""
    if stmt["unclosed"] or stmt.get("issues"):
        reasons = list(stmt.get("issues") or [])
        if stmt["unclosed"] and REASON_DELIMITER not in reasons:
            reasons.append("An unclosed quote, comment or DELIMITER line makes the statement boundaries uncertain.")
        return {"kind": "risky", "category": "unknown", "reasons": reasons}
    parts = [p for p in stmt["code"].upper().split(";") if p.strip()]
    verdicts = [_classify_code(p, stmt["idents"], key_columns) for p in parts]
    verdict = _worst(verdicts)
    if len(parts) > 1:
        verdict["reasons"].append(REASON_MULTI)
    return verdict


def _classify_reading(sql, backslash_escapes, key_columns):
    statements = []
    for stmt in split_statements(sql, backslash_escapes):
        verdict = _classify_statement(stmt, key_columns)
        statements.append({"text": stmt["text"], "start": stmt["start"], "end": stmt["end"],
                           "verb": _verb(stmt["code"].upper()), "kind": verdict["kind"],
                           "category": verdict["category"], "reasons": verdict["reasons"]})
    return statements


def classify(sql, backslash_escapes=None, key_columns=None):
    """Classify SQL text as ``read``, ``write`` or ``risky`` with reasons.

    *backslash_escapes*: None reads strings both ways (with backslash
    escapes, and as under NO_BACKSLASH_ESCAPES) and keeps the worse verdict;
    True or False uses one reading (when the session's sql_mode is known).
    *key_columns*: the key column names of the table an UPDATE or DELETE
    changes, when the caller knows them.

    Returns ``{"kind", "category", "reasons", "statements": [{"text",
    "start", "end", "verb", "kind", "category", "reasons"}], "count",
    "ambiguous"}``; ``kind`` is the worst statement's.
    """
    if not isinstance(sql, str):
        raise ValueError("Pass SQL text.")
    if len(sql) > 1000000:
        raise ValueError("SQL text is limited to 1,000,000 characters.")
    readings = [True, False] if backslash_escapes is None else [bool(backslash_escapes)]
    results = [_classify_reading(sql, b, key_columns) for b in readings]
    statements = results[0]
    summary = _worst(statements)
    ambiguous = len(results) > 1 and \
        [(s["start"], s["end"]) for s in results[1]] != [(s["start"], s["end"]) for s in statements]
    if ambiguous:
        # Both readings were classified; the worse one counts.
        summary = _worst([summary, _worst(results[1])])
        summary["reasons"].append(AMBIGUOUS_REASON)
    return {"kind": summary["kind"], "category": summary["category"], "reasons": summary["reasons"],
            "statements": statements, "count": len(statements), "ambiguous": ambiguous}


# ---- the confirmation policy -----------------------------------------------------------

#: On a read-only connection the server enforces it too: run this in the
#: session before any statement as additional protection against table writes.
#: This does not sandbox administrative UDFs or named locks. The
#: classifier treats any later change of transaction_read_only, SET
#: TRANSACTION READ WRITE and START TRANSACTION READ WRITE as writes, so the
#: guard cannot be lifted on a read-only connection.
READ_ONLY_SESSION_GUARD = ("SET SESSION TRANSACTION READ ONLY",)


def normalize_type(value):
    """A connection type: 'production' or 'development'. Anything else is an error."""
    if value in (None, ""):
        return DEFAULT_CONNECTION_TYPE
    if not isinstance(value, str) or value.lower() not in CONNECTION_TYPES:
        raise ValueError("Choose connection type 'production' or 'development'.")
    return value.lower()


def connection_safety(profile=None, connection=None):
    """The type and read-only flag a write on this connection is judged by.

    A request never lowers the protection. An unsaved connection is
    ``production`` whatever it claims, and may only ask to be read-only
    (``connection["read_only"] is True``). A saved profile's type applies
    only when *connection* (when given) still points at the profile's host,
    port, user and TLS settings (``workspace_store.ENDPOINT_FIELDS``, as for
    the saved password); otherwise it is ``production``. The profile's
    read-only flag applies either way. Returns ``{"profile_id",
    "profile_name", "type", "read_only", "endpoint_matches"}``.
    """
    connection = connection if isinstance(connection, dict) else {}
    asked_read_only = connection.get("read_only") is True
    if isinstance(profile, dict):
        ctype = profile.get("type")
        ctype = ctype if ctype in CONNECTION_TYPES else DEFAULT_CONNECTION_TYPE
        matches = _same_endpoint(profile, connection) if connection else True
        if not matches:
            ctype = DEFAULT_CONNECTION_TYPE
        return {"profile_id": profile.get("id"), "profile_name": profile.get("name"), "type": ctype,
                "read_only": bool(profile.get("read_only")) or asked_read_only, "endpoint_matches": matches}
    return {"profile_id": None, "profile_name": None, "type": DEFAULT_CONNECTION_TYPE,
            "read_only": asked_read_only, "endpoint_matches": False}


def _same_endpoint(profile, connection):
    from .live import connection_options
    from .workspace_store import ENDPOINT_FIELDS
    try:
        options = connection_options({k: v for k, v in connection.items() if k != "password"})
    except (ValueError, TypeError):
        return False
    return all((options.get(f) or "") == (profile.get(f) or "") for f in ENDPOINT_FIELDS)


def connection_identity(connection, safety=None):
    """The key reports and confirmations are bound to, as JSON text: the same
    list as ``connectionKey()`` in apps/workspace/src/binding.ts (saved
    connection id, host, port, user, TLS mode and CA), followed by the type
    and read-only flag when they are not the defaults (production,
    read-write), so a Production read-write key is unchanged. Never the
    password or the default schema. *safety* is :func:`connection_safety`;
    without it the connection's own ``type`` and ``read_only`` are used."""
    c = connection if isinstance(connection, dict) else {}
    try:
        port = int(c.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    key = [c.get("profile_id") or "", c.get("host") or "", port, c.get("user") or "", c.get("ssl_mode") or "",
           c.get("ssl_ca") or ""]
    source = safety if isinstance(safety, dict) else c
    ctype = source.get("type") if source.get("type") in CONNECTION_TYPES else DEFAULT_CONNECTION_TYPE
    read_only = source.get("read_only") is True
    if ctype != DEFAULT_CONNECTION_TYPE or read_only:
        key += [ctype, read_only]
    return json.dumps(key, separators=(",", ":"), ensure_ascii=False)


#: Elements of a :func:`connection_identity` list that name the endpoint.
IDENTITY_ENDPOINT_FIELDS = 6


def identity_endpoint(identity):
    """The endpoint part of a :func:`connection_identity` text (saved
    connection id, host, port, user, TLS mode and CA, without the type and
    read-only flag), as JSON text in the same form. Two identities with the
    same endpoint reach the same server through the same saved connection;
    they differ only in the write policy. Server-held jobs (skip loops,
    lifecycle jobs) use it so a policy change never cuts a job off from its
    status and Stop or Cancel. Text that is not a JSON list comes back
    unchanged."""
    try:
        key = json.loads(identity) if isinstance(identity, str) else None
    except ValueError:
        return identity
    if not isinstance(key, list):
        return identity
    return json.dumps(key[:IDENTITY_ENDPOINT_FIELDS], separators=(",", ":"), ensure_ascii=False)


def decide(verdict, connection_type=DEFAULT_CONNECTION_TYPE, read_only=False, name=None):
    """``refuse``, ``confirm`` or ``allow`` one classified request, with the text to show.

    =========== ============= ============= ==========
    kind        production    development   read-only
    =========== ============= ============= ==========
    read        allow         allow         allow
    write       confirm       allow         refuse
    risky       confirm       confirm       refuse
    =========== ============= ============= ==========

    *verdict* is a :func:`classify` result (or a kind string). Returns
    ``{"action", "kind", "category", "connection_type", "read_only", "tone",
    "title", "text", "reasons", "statements"}``; ``tone`` is ``danger`` or
    ``primary`` for the confirmation.
    """
    if isinstance(verdict, str):
        verdict = {"kind": verdict, "category": verdict, "reasons": [], "statements": []}
    kind = verdict.get("kind")
    if kind not in KINDS:
        raise ValueError("Unknown classification kind.")
    ctype = normalize_type(connection_type)
    read_only = read_only is True
    statements = [s["text"] if isinstance(s, dict) else str(s) for s in verdict.get("statements") or []]
    label = ("Connection %s" % name) if name else "This connection"
    subject = ("Connection %s" % name) if name else "This"
    count = len(statements)
    what = "these %d statements" % count if count > 1 else "this statement"
    category = verdict.get("category") or kind
    parts = [s for s in verdict.get("statements") or [] if isinstance(s, dict) and s.get("kind") != "read"]
    light = all(s.get("category") in LIGHT_CATEGORIES for s in parts) if parts else category in LIGHT_CATEGORIES
    tone = "primary" if kind != "risky" and light else "danger"
    if kind == "read":
        action, title, text = "allow", "Read", "Reads only, so it runs without a confirmation."
    elif read_only:
        action, title = "refuse", "Read-only connection"
        how = ("turn off Read-only in its settings, or run: myflames profiles set %s --read-write" % _quote_arg(name)
               if name else "turn off Read-only in its settings")
        text = "%s is read-only in myflames, so %s %s not sent. To allow writes, %s." % (
            label, what, "were" if count > 1 else "was", how)
    elif ctype == "production":
        action, title = "confirm", "Production connection"
        text = "%s is a Production connection, so myflames asks before every write." % subject
    elif kind == "risky":
        action, title = "confirm", "Risky write"
        text = ("%s is a Development connection. myflames asks only before risky writes, and %s %s."
                % (subject, what, "include one" if count > 1 else "is one"))
    else:
        action, title = "allow", "Development connection"
        text = "%s is a Development connection, so this write runs without a confirmation." % subject
    return {"action": action, "kind": kind, "category": category, "connection_type": ctype,
            "read_only": read_only, "tone": tone, "title": title, "text": text,
            "reasons": list(verdict.get("reasons") or []), "statements": statements,
            "session_guard": list(READ_ONLY_SESSION_GUARD) if read_only else []}


def _quote_arg(value):
    value = str(value)
    return value if re.match(r"^[\w.@:-]+$", value) else "'%s'" % value.replace("'", "'\\''")


def policy_summary(decision):
    """The part of a decision the audit trail keeps."""
    return {k: decision.get(k) for k in ("action", "kind", "category", "connection_type", "read_only")}


def gate(decision, confirmed=False):
    """None when the write may be sent now; else ``(status, message)``:
    ``("refused", text)`` on a read-only connection, ``("needs_confirmation",
    text)`` when the policy asks and *confirmed* is not True. Write endpoints
    call this before opening any connection."""
    if decision["action"] == "refuse":
        return "refused", decision["text"]
    if decision["action"] == "confirm" and confirmed is not True:
        return "needs_confirmation", decision["text"]
    return None


def cli_confirm(description, prompt, assume_yes=False, quiet=False, stdin=None, stderr=None,
                ask=True, expected=None, supplied=None, passwords=(), no_terminal_message=None,
                decline_message="Nothing was sent.", mismatch_message=None, mismatch_outcome="declined"):
    """Return None on approval, else (audit status, outcome, message).

    Callers own policy checks and the audit record, including refusals before
    a plan exists. They must record this result before reporting an unsent
    failure. No execution or policy override occurs here. ``expected`` makes
    this an exact typed guard; only its line terminator is removed. ``--yes``
    bypasses yes/no input, never a typed guard without a matching ``supplied``.
    """
    from .connector import terminal_text
    stdin = sys.stdin if stdin is None else stdin
    stderr = sys.stderr if stderr is None else stderr

    def clean(text):
        text = redact(str(text))
        for password in passwords:
            text = redact(text, password)
        return terminal_text(text)

    if description and not (quiet and assume_yes):
        stderr.write(clean(description).rstrip("\n") + "\n")
    if assume_yes:
        if expected is None:
            return None
        if supplied == expected:
            return None
        return ("refused", "no_target" if supplied is None else "target_mismatch",
                clean(mismatch_message or "Not sent: the typed target must match exactly."))
    if not stdin.isatty():
        return ("refused", "no_terminal", clean(no_terminal_message or
                "Not sent: stdin is not a terminal, so myflames cannot ask you to confirm. Re-run with --yes."))
    if not ask:
        return None
    stderr.write(clean(prompt))
    stderr.flush()
    try:
        answer = stdin.readline()
    except (KeyboardInterrupt, EOFError):
        answer = ""
    if not answer:
        return ("declined", "declined", clean(decline_message))
    if expected is None:
        accepted = answer.strip().lower() in ("y", "yes")
    else:
        # Do not silently accept extra spaces in an account or directory name.
        answer = answer[:-1] if answer.endswith("\n") else answer
        answer = answer[:-1] if answer.endswith("\r") else answer
        accepted = answer == expected
    if not accepted:
        return (mismatch_outcome if expected is not None else "declined",
                mismatch_outcome if expected is not None else "declined",
                clean(mismatch_message if expected is not None and mismatch_message else decline_message))
    return None


# ---- privileges (a hint: the server decides) -----------------------------------------

#: Tokens of SHOW GRANTS and SHOW CREATE USER output: quoted identifiers and
#: strings stay whole (backslashes kept), so a keyword inside a quoted
#: credential or name is never read as SQL.
GRANT_TOKEN = re.compile(r'''`(?:``|[^`])*`|"(?:""|[^"])*"|'(?:''|\\.|[^'\\])*'|[.,@()*;]|[^\s.,@()*;`"']+''')

# Words that end an IDENTIFIED clause in SHOW GRANTS (MariaDB) and SHOW CREATE
# USER (both engines). Everything from IDENTIFIED up to the first of them is a
# credential: the plugin, hash, USING string, OR chains and AND factors.
_CREDENTIAL_ENDS = (("WITH", "GRANT"), ("WITH", "ADMIN"), ("WITH", "MAX_QUERIES_PER_HOUR"),
                    ("WITH", "MAX_UPDATES_PER_HOUR"), ("WITH", "MAX_CONNECTIONS_PER_HOUR"),
                    ("WITH", "MAX_USER_CONNECTIONS"), ("WITH", "MAX_STATEMENT_TIME"), ("REQUIRE",),
                    ("PASSWORD", "EXPIRE"), ("PASSWORD", "HISTORY"), ("PASSWORD", "REUSE"),
                    ("PASSWORD", "REQUIRE"), ("ACCOUNT", "LOCK"), ("ACCOUNT", "UNLOCK"), ("DEFAULT", "ROLE"),
                    ("FAILED_LOGIN_ATTEMPTS",), ("PASSWORD_LOCK_TIME",), ("COMMENT",), ("ATTRIBUTE",))


def strip_credentials(text):
    """*text* (a SHOW GRANTS or SHOW CREATE USER line) without its IDENTIFIED
    clauses: MariaDB ``IDENTIFIED BY PASSWORD '<hash>'`` and ``IDENTIFIED VIA
    plugin USING '...' OR ...``, MySQL ``IDENTIFIED WITH 'plugin' AS '<hash>'``
    or ``AS 0x...`` and multifactor ``AND IDENTIFIED ...``. Boundaries are
    tokens outside quoted literals, so a credential that contains ``WITH
    GRANT OPTION`` cannot end the clause early."""
    text = str(text or "")
    while True:
        matches = list(GRANT_TOKEN.finditer(text))
        upper = [m.group().upper() for m in matches]
        start = next((i for i, word in enumerate(upper) if word == "IDENTIFIED"), None)
        if start is None:
            return text
        end = len(text)
        for j in range(start + 1, len(matches)):
            if any(tuple(upper[j:j + len(words)]) == words for words in _CREDENTIAL_ENDS):
                end = matches[j].start()
                break
        text = (text[:matches[start].start()].rstrip() + " " + text[end:].lstrip()).strip()


def parse_grants(grants, engine="mysql"):
    """Privileges from SHOW GRANTS lines: ``{"global": set, "schemas": {name:
    set}, "patterns": {pattern: set}, "unparsed": [lines], "model": ...}``.

    A thin view of :func:`myflames.privileges.parse_grants` (the one SHOW
    GRANTS parser). ``global`` holds the global and dynamic privileges.
    ``schemas`` holds database grants on literal names (``my\\_db`` is the
    database ``my_db``); grants on wildcard patterns (``db_x`` and ``shop%``
    with partial_revokes OFF) are kept apart in ``patterns``, under the stored
    pattern, so ``db\\_x`` and ``db_x`` are never merged. Table, column,
    routine (including PACKAGE, PACKAGE BODY and LIBRARY), proxy and role
    lines are in ``model``; a line the parser did not understand is listed
    in ``unparsed``, never dropped silently. Names are upper case with single
    spaces (``CONNECTION_ADMIN``, MariaDB ``CONNECTION ADMIN``).
    """
    from . import privileges
    model = privileges.parse_grants(list(grants or ()), engine or "mysql")
    result = {"global": set(model["global"]) | set(model["dynamic"]), "schemas": {}, "patterns": {},
              "unparsed": list(model["unparsed"]), "model": model}
    for entry in model["schemas"]:
        if entry["wildcard"]:
            result["patterns"].setdefault(entry["pattern"], set()).update(entry["privs"])
        else:
            result["schemas"].setdefault(entry["database"], set()).update(entry["privs"])
    return result


def global_privileges(grants):
    """The set of global (ON *.*) privilege names in SHOW GRANTS lines."""
    return parse_grants(grants)["global"]


#: MariaDB privileges SUPER also covers: on every 10.11 release (privilege.h
#: at mariadb-10.11.16 lines 410, 459, 520), and from 11.0.1 only for grants
#: stored before 11.0 (ALLOWED_BY_SUPER_BEFORE_110000, mariadb-11.0.1
#: privilege.h:409). READ_ONLY ADMIN is apart: from 10.11.1 SUPER covers it
#: only for grants stored before 10.11 (ALLOWED_BY_SUPER_BEFORE_101100,
#: mariadb-10.11.1 sql_acl.cc:1548-1551). SHOW GRANTS does not print the
#: grant's version_id, so from those versions the answer is unknown.
_MARIADB_SUPER_BEFORE_11 = ("CONNECTION ADMIN", "REPLICATION SLAVE ADMIN", "BINLOG ADMIN", "BINLOG REPLAY",
                            "REPLICATION MASTER ADMIN", "SET USER", "FEDERATED ADMIN", "SLAVE MONITOR",
                            "BINLOG MONITOR")
#: First MariaDB version where SUPER no longer covers the split privileges above.
MARIADB_SUPER_SPLIT_END = (11, 0, 1)
#: First MariaDB version where SUPER no longer covers READ_ONLY ADMIN.
MARIADB_SUPER_READ_ONLY_END = (10, 11, 1)
_MARIADB_BINLOG_ADMIN = ("sql_log_bin", "binlog_format", "binlog_row_image", "binlog_annotate_row_events",
                         "binlog_direct_non_transactional_updates")
_MARIADB_BINLOG_REPLAY = ("pseudo_thread_id", "gtid_seq_no", "server_id", "gtid_domain_id")
#: MySQL SET GLOBAL / SET PERSIST of these needs more (M/sql/sys_vars.cc).
_MYSQL_GLOBAL_EXTRA = {
    "default_table_encryption": [["SUPER"], ["SYSTEM_VARIABLES_ADMIN", "TABLE_ENCRYPTION_ADMIN"]],
    "binlog_encryption": [["SUPER"], ["BINLOG_ENCRYPTION_ADMIN"]],
}
_MYSQL_GLOBAL_ALSO = {"mandatory_roles": "ROLE_ADMIN", "keyring_operations": "ENCRYPTION_KEY_ADMIN",
                      "group_replication_consistency": "GROUP_REPLICATION_ADMIN"}
_OBJECT_PRIVILEGES = {"insert": "INSERT", "update": "UPDATE", "delete": "DELETE", "create": "CREATE",
                      "alter": "ALTER", "drop": "DROP", "index": "INDEX"}
PRIVILEGE_OPERATIONS = ("kill", "set_global", "set_persist", "set_persist_only", "reset_persist",
                        "set_session_restricted", "replication", "clone_donor", "clone_recipient",
                        "write_when_read_only") + tuple(sorted(_OBJECT_PRIVILEGES))


def required_privileges(operation, engine, version=(0, 0, 0), variable=None, read_only_variable=False):
    """What *operation* needs: ``{"available", "any_of": [[privileges], ...], "note"}``.

    Any one inner list is enough (all of its privileges together). From the
    db-operations skill, section 3 (verified against server source).
    """
    mariadb = engine == "mariadb"
    variable = (variable or "").lower()
    super_ok = mariadb and tuple(version or (0, 0, 0)) < MARIADB_SUPER_SPLIT_END

    def maria(name):
        alternatives = [[name]]
        if super_ok and name in _MARIADB_SUPER_BEFORE_11:
            alternatives.append(["SUPER"])
        return alternatives
    note = ""
    if operation == "kill":
        if mariadb:
            any_of = maria("CONNECTION ADMIN")
        else:
            any_of = [["CONNECTION_ADMIN"], ["SUPER"]]
            note = "Ending a session of an account that has SYSTEM_USER also needs SYSTEM_USER."
        note = (note + " Sessions of your own user name need no privilege.").strip()
    elif operation in ("set_global", "set_persist"):
        if mariadb:
            if operation == "set_persist":
                return {"available": False, "any_of": [], "note": "MariaDB has no SET PERSIST; edit an option file."}
            any_of = [["SUPER"]]
            note = ("Some variables need their own privilege instead (BINLOG ADMIN, CONNECTION ADMIN, "
                    "READ_ONLY ADMIN, REPLICATION SLAVE ADMIN, REPLICATION MASTER ADMIN).")
        elif variable in _MYSQL_GLOBAL_EXTRA:
            any_of = [list(a) for a in _MYSQL_GLOBAL_EXTRA[variable]]
        else:
            any_of = [["SYSTEM_VARIABLES_ADMIN"], ["SUPER"]]
            if variable in _MYSQL_GLOBAL_ALSO:
                any_of = [a + [_MYSQL_GLOBAL_ALSO[variable]] for a in any_of]
    elif operation == "set_persist_only":
        if mariadb:
            return {"available": False, "any_of": [], "note": "MariaDB has no SET PERSIST_ONLY; edit an option file."}
        any_of = [["SYSTEM_VARIABLES_ADMIN", "PERSIST_RO_VARIABLES_ADMIN"]]
        note = "SUPER is not enough (error 3630)."
    elif operation == "reset_persist":
        if mariadb:
            return {"available": False, "any_of": [], "note": "MariaDB has no RESET PERSIST."}
        any_of = ([["SYSTEM_VARIABLES_ADMIN", "PERSIST_RO_VARIABLES_ADMIN"]] if read_only_variable
                  else [["SYSTEM_VARIABLES_ADMIN"], ["SUPER"]])
    elif operation == "set_session_restricted":
        if mariadb:
            if variable == "skip_replication":
                # No privilege check (Ma/sql/sys_vars.cc Sys_skip_replication); refused inside a transaction.
                return {"available": True, "any_of": [[]], "note": "Needs no privilege; it cannot change inside a "
                                                                   "transaction or a stored function."}
            if variable in _MARIADB_BINLOG_ADMIN:
                any_of = maria("BINLOG ADMIN")
            elif variable in _MARIADB_BINLOG_REPLAY:
                any_of = maria("BINLOG REPLAY")
            else:
                any_of = [["SUPER"]]
        else:
            any_of = [["SESSION_VARIABLES_ADMIN"], ["SYSTEM_VARIABLES_ADMIN"], ["SUPER"]]
    elif operation == "replication":
        any_of = maria("REPLICATION SLAVE ADMIN") if mariadb else [["REPLICATION_SLAVE_ADMIN"], ["SUPER"]]
    elif operation in ("clone_donor", "clone_recipient"):
        if mariadb:
            return {"available": False, "any_of": [], "note": "MariaDB has no CLONE; use mariadb-backup."}
        any_of = [["BACKUP_ADMIN"]] if operation == "clone_donor" else [["CLONE_ADMIN"]]
        if operation == "clone_recipient":
            note = "Cloning into another directory (DATA DIRECTORY) needs BACKUP_ADMIN instead."
    elif operation == "write_when_read_only":
        if mariadb:
            any_of = [["READ_ONLY ADMIN"]]
            if tuple(version or (0, 0, 0)) < MARIADB_SUPER_READ_ONLY_END:
                any_of.append(["SUPER"])
            note = "read_only=NO_LOCK_NO_ADMIN (12.0.1 and later) blocks even READ_ONLY ADMIN."
        else:
            any_of = [["SUPER"], ["CONNECTION_ADMIN"]]
            note = "Nobody can write while super_read_only=ON."
    elif operation in _OBJECT_PRIVILEGES:
        any_of = [[_OBJECT_PRIVILEGES[operation]]]
        note = "Granted per database or table: myflames checks global and database grants only."
    else:
        raise ValueError("Unknown operation for a privilege check: %r" % (operation,))
    return {"available": True, "any_of": any_of, "note": note}


def privilege_hint(operation, engine, version, grants, variable=None, schema=None, read_only_variable=False):
    """Whether SHOW GRANTS lines suggest this account may run *operation*.

    Returns ``{"operation", "checked", "available", "allowed", "via",
    "need", "note"}``. ``allowed`` is True, False, or None when grants
    alone cannot tell (object privileges granted per table, or SUPER on
    MariaDB 11.0 and later, which still counts for grants stored before
    11.0). A hint for showing an action and explaining a refusal; the
    server has the last word.
    """
    need = required_privileges(operation, engine, version, variable, read_only_variable)
    result = {"operation": operation, "checked": True, "available": need["available"], "allowed": None,
              "via": None, "need": " or ".join(" and ".join(a) or "no privilege" for a in need["any_of"]),
              "note": need["note"]}
    if not need["available"]:
        result["allowed"] = False
        return result
    parsed = parse_grants(grants, engine)
    have = set(parsed["global"])
    every = bool(have & {"ALL", "ALL PRIVILEGES"})
    for alternative in need["any_of"]:
        if all(p in have for p in alternative):
            result["allowed"], result["via"] = True, " and ".join(alternative) or "no privilege needed"
            return result
    if every:
        result["allowed"], result["via"] = True, "ALL PRIVILEGES"
        return result
    if schema is not None and operation in _OBJECT_PRIVILEGES:
        # Database grants: an exact name decides, wildcard patterns that
        # disagree or are absent leave it unknown (privileges.has).
        from . import privileges
        privilege = _OBJECT_PRIVILEGES[operation]
        if privileges.has(parsed["model"], privilege, "database", database=schema) is True:
            result["allowed"], result["via"] = True, "%s on %s" % (privilege, schema)
            return result
    if operation in _OBJECT_PRIVILEGES or parsed["unparsed"]:
        # Object grants live per database or table; an unparsed line may grant it.
        result["allowed"] = None
    elif engine == "mariadb" and "SUPER" in have:
        # SUPER still counts for grants stored by an earlier version; SHOW
        # GRANTS cannot tell which version stored them.
        result["allowed"] = None
        since = "10.11.1" if operation == "write_when_read_only" else "11.0.1"
        result["note"] = (result["note"] + " On MariaDB %s and later, SUPER covers this only for grants "
                                           "stored by an earlier version." % since).strip()
    else:
        result["allowed"] = False
    return result


def read_grants(conn, tag="/*myflames:dbops*/ "):
    """``{"checked", "user", "account", "grants", "error"}`` from USER(),
    CURRENT_USER() and plain SHOW GRANTS (which includes the roles active at
    login). Errors are returned, not raised. The lines carry no credential:
    MariaDB prints the account's password hash or plugin string in its
    global line, and :func:`strip_credentials` removes it here."""
    from .connector import ConnectorError
    from .perf_common import _reason, text_rows

    def rows(sql):
        return text_rows(conn, sql, tag)
    try:
        who = rows("SELECT USER(), CURRENT_USER() LIMIT 1")
        grants = [strip_credentials(row[0]) for row in rows("SHOW GRANTS") if row and row[0]]
    except ConnectorError as exc:
        return {"checked": False, "user": None, "account": None, "grants": [], "error": redact(_reason(exc))}
    login = (who[0][0] or "") if who and who[0] else ""
    user = login.rsplit("@", 1)[0] if "@" in login else (login or None)
    account = who[0][1] if who and len(who[0]) > 1 else None
    return {"checked": True, "user": user, "account": account, "grants": grants, "error": None}


# ---- redaction -------------------------------------------------------------------------

REDACTED = "[redacted]"
_STRING = r"""(?:'(?:''|\\.|[^'\\])*(?:'|$)|"(?:""|\\.|[^"\\])*(?:"|$))"""
_SECRET_PATTERNS = tuple(re.compile(p + "(" + _STRING + ")", re.I | re.S) for p in (
    # IDENTIFIED BY 'x', IDENTIFIED WITH plugin BY|AS 'x', MariaDB IDENTIFIED BY PASSWORD 'hash';
    # CLONE INSTANCE FROM ... IDENTIFIED BY 'x'.
    r"(\bIDENTIFIED\s+(?:WITH\s+(?:`[^`]*`|'[^']*'|\"[^\"]*\"|\w+)\s+)?(?:BY|AS)\s+(?:PASSWORD\s+)?)",
    # MariaDB IDENTIFIED VIA plugin USING 'x' or USING PASSWORD('x').
    r"(\b(?:VIA|WITH)\s+(?:`[^`]*`|\w+)\s+USING\s+(?:PASSWORD\s*\(\s*)?)",
    # PASSWORD('x'), OLD_PASSWORD('x').
    r"(\b(?:OLD_)?PASSWORD\s*\(\s*)",
    # SET PASSWORD [FOR user] = 'x'.
    r"(\bSET\s+PASSWORD\b[^=;]*=\s*)",
    # SOURCE_PASSWORD = 'x', MASTER_PASSWORD = 'x', PASSWORD = 'x', OPTIONS (PASSWORD 'x'). Not a
    # word inside quotes ('caching_sha2_password' is followed by its own closing quote).
    r"((?<![\w'\"`])\w*PASSWORD\s*=?\s*)",
    # ALTER USER ... IDENTIFIED BY 'new' or RANDOM PASSWORD REPLACE 'current'.
    r"((?:\bRANDOM\s+PASSWORD|\[redacted\]['\"])\s+REPLACE\s+)",
))


#: MySQL SHOW CREATE USER prints a hash with non-printable bytes as a hex
#: literal when print_identified_with_as_hex is ON: ``AS 0x2441...``.
_HEX_SECRET = re.compile(r"(\b(?:AS|BY|USING)\s+(?:PASSWORD\s+)?)0x[0-9A-F]+\b", re.I)


def _redact_literal(match):
    literal = match.group(2)
    quote = literal[0]
    return match.group(1) + quote + REDACTED + quote


def redact(text, password=None):
    """*text* with secrets replaced by ``[redacted]``: the literals after
    IDENTIFIED BY / AS / USING, PASSWORD('...'), SET PASSWORD, SOURCE_PASSWORD,
    MASTER_PASSWORD and other ``*PASSWORD = '...'`` options, and the
    connection's own *password* anywhere (also as written in SQL with a
    doubled or backslash-escaped quote)."""
    if text is None:
        return None
    text = str(text)
    if password:
        for variant in sorted({password, password.replace("'", "''"),
                               password.replace("\\", "\\\\").replace("'", "\\'"),
                               password.replace('"', '""')}, key=len, reverse=True):
            if variant:
                text = text.replace(variant, REDACTED)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redact_literal, text)
    return _HEX_SECRET.sub(lambda m: m.group(1) + "'" + REDACTED + "'", text)


def redact_value(value, password=None):
    """Redact every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact(value, password)
    if isinstance(value, dict):
        return {k: redact_value(v, password) for k, v in value.items() if str(k).lower() != "password"}
    if isinstance(value, (list, tuple)):
        return [redact_value(v, password) for v in value]
    return value


def error_code(text):
    """The server error number in a client error message, or None."""
    m = re.search(r"\bERROR\s+(\d{3,5})\b", str(text or ""))
    return int(m.group(1)) if m else None


# ---- the audit trail --------------------------------------------------------------------

AUDIT_SCHEMA_VERSION = "audit-1.0"
#: pending: written before sending, not finished (myflames stopped); unknown:
#: interrupted or timed out after the statement may have reached the server.
AUDIT_STATUSES = ("pending", "ok", "error", "declined", "refused", "unknown")
SURFACES = ("ui", "cli")
_TEXT_CAP = 20000


def os_user():
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _connection_fields(connection, safety, engine, server_version):
    connection = connection if isinstance(connection, dict) else {}
    safety = safety or connection_safety(None, connection)
    port = connection.get("port")
    return {"profile_id": safety.get("profile_id"), "profile_name": safety.get("profile_name"),
            "connection_type": safety.get("type"), "read_only": bool(safety.get("read_only")),
            "host": connection.get("host"), "port": port if isinstance(port, int) else None,
            "db_user": connection.get("user"), "engine": engine, "server_version": server_version}


def _json(value, password):
    if value is None:
        return None
    return json.dumps(redact_value(value, password), ensure_ascii=False, allow_nan=False, default=str)[:_TEXT_CAP]


def _row(operation, statements, surface, status, connection=None, safety=None, engine=None, server_version=None,
         target=None, verified=None, policy=None, password=None):
    if surface not in SURFACES:
        raise ValueError("surface must be 'ui' or 'cli'.")
    if status not in AUDIT_STATUSES:
        raise ValueError("Unknown audit status.")
    statements = [redact(s, password)[:_TEXT_CAP] for s in (statements or [])]
    row = {"id": uuid.uuid4().hex, "attempted_at": _now(), "finished_at": None, "status": status, "outcome": None,
           "surface": surface, "os_user": os_user(), "operation": str(operation)[:100],
           "statements": json.dumps(statements, ensure_ascii=False),
           "target": _json(target, password), "verified": _json(verified, password),
           "policy": _json(policy, password), "error_code": None, "message": None, "rows_affected": None,
           "after": None, "duration_ms": None}
    row.update(_connection_fields(connection, safety, engine, server_version))
    return row


def record_attempt(store, operation, statements, surface, connection=None, safety=None, engine=None,
                   server_version=None, target=None, verified=None, policy=None, password=None):
    """Write a ``pending`` record right before sending; returns its id.

    Raises when the record cannot be written, so the caller sends nothing.
    *connection* supplies host, port and user (a password in it is never
    stored); *safety* is :func:`connection_safety`; *target* is the identity
    the user confirmed and *verified* what was re-checked.
    """
    row = _row(operation, statements, surface, "pending", connection, safety, engine, server_version, target,
               verified, policy, password)
    store.audit_insert(row)
    return row["id"]


def _finish_fields(status, outcome=None, error=None, message=None, rows_affected=None, after=None,
                   duration_ms=None, password=None):
    if status not in AUDIT_STATUSES or status == "pending":
        raise ValueError("Finish an audit record with ok, error, declined, refused or unknown.")
    code = error if isinstance(error, int) and not isinstance(error, bool) else error_code(error)
    return {"finished_at": _now(), "status": status, "outcome": (str(outcome)[:100] if outcome else None),
            "error_code": code, "message": redact(message, password)[:_TEXT_CAP] if message else None,
            "rows_affected": rows_affected if isinstance(rows_affected, int) else None,
            "after": _json(after, password),
            "duration_ms": round(float(duration_ms), 3) if duration_ms is not None else None}


def complete(store, audit_id, status, outcome=None, error=None, message=None, rows_affected=None, after=None,
             duration_ms=None, password=None):
    """Finish a pending record: ``ok``, ``error``, ``declined``, ``refused``,
    or ``unknown`` (interrupted after the statement may have been sent)."""
    fields = _finish_fields(status, outcome, error, message, rows_affected, after, duration_ms, password)
    return store.audit_complete(audit_id, fields)


def record_outcome(store, operation, statements, surface, status, outcome=None, message=None, error=None,
                   connection=None, safety=None, engine=None, server_version=None, target=None, verified=None,
                   policy=None, password=None):
    """One finished record, written in one transaction, for an attempt that
    sent nothing (declined, refused) or failed before sending (error).
    Returns its id."""
    row = _row(operation, statements, surface, "pending", connection, safety, engine, server_version, target,
               verified, policy, password)
    row.update(_finish_fields(status, outcome=outcome, error=error, message=message, password=password))
    store.audit_insert(row)
    return row["id"]


def may_have_run(exc):
    """True when a failure leaves unknown whether the statement ran: an
    interrupt, a client time limit, or a connection lost during the query
    (client error 2013)."""
    if isinstance(exc, KeyboardInterrupt):
        return True
    text = str(exc)
    return "timed out" in text or error_code(text) == 2013 or "Lost connection to" in text


class AuditTrail(object):
    """The audit records of one requested write, for a write path to call
    in order: :meth:`attempt` before sending, then :meth:`complete`; or
    :meth:`outcome` when nothing is sent. :meth:`fail` records an exception
    at whatever point it happened. Completion failures leave the record
    pending ("outcome unknown") and never hide the write's own result."""

    def __init__(self, store, operation, surface, connection=None, safety=None, password=None, policy=None):
        self.store, self.operation, self.surface = store, operation, surface
        self.connection = {k: v for k, v in (connection or {}).items() if k != "password"}
        self.safety = safety
        self.password = password
        self.policy = policy
        self.id = None
        self.finished = False
        self.started = None
        self.engine = None
        self.server_version = None

    def server(self, engine, server_version):
        self.engine, self.server_version = engine, server_version

    def attempt(self, statements, target=None, verified=None):
        self.started = time.monotonic()
        self.id = record_attempt(self.store, self.operation, statements, self.surface, self.connection, self.safety,
                                 self.engine, self.server_version, target, verified, self.policy, self.password)
        return self.id

    def complete(self, status, outcome=None, error=None, message=None, rows_affected=None, after=None):
        if self.id is None or self.finished:
            return
        self.finished = True
        duration = (time.monotonic() - self.started) * 1000.0 if self.started is not None else None
        try:
            complete(self.store, self.id, status, outcome=outcome, error=error, message=message,
                     rows_affected=rows_affected, after=after, duration_ms=duration, password=self.password)
        except Exception:
            pass

    def outcome(self, statements, status, outcome=None, message=None, error=None, target=None, verified=None):
        if self.finished:
            return self.id
        self.finished = True
        try:
            self.id = record_outcome(self.store, self.operation, statements, self.surface, status, outcome=outcome,
                                     message=message, error=error, connection=self.connection, safety=self.safety,
                                     engine=self.engine, server_version=self.server_version, target=target,
                                     verified=verified, policy=self.policy, password=self.password)
        except Exception:
            pass
        return self.id

    def fail(self, exc, statements=()):
        """Record an exception. With a pending record (the statement may have
        been sent), an interrupt, time limit or lost connection finishes it as
        ``unknown`` and any other failure as ``error``; without one, an error
        record is written (nothing was sent)."""
        if self.finished:
            return
        text = "Interrupted." if isinstance(exc, KeyboardInterrupt) and not str(exc) else str(exc)
        if self.id is not None:
            if may_have_run(exc):
                self.complete("unknown", outcome="interrupted" if isinstance(exc, KeyboardInterrupt) else "timeout",
                              error=text, message=text + " The statement may have reached the server; its outcome "
                                                         "is unknown.")
            else:
                self.complete("error", outcome="error", error=text, message=text)
        else:
            self.outcome(list(statements), "error", outcome="error", message=text, error=text)

    @property
    def pending(self):
        """A record was written before sending and is not finished yet."""
        return self.id is not None and not self.finished


# ---- reading and exporting the audit trail -----------------------------------------------

AUDIT_CSV_COLUMNS = ("id", "attempted_at", "finished_at", "status", "outcome", "surface", "os_user", "operation",
                     "profile_name", "connection_type", "read_only", "host", "port", "user", "engine",
                     "server_version", "statements", "target", "verified", "error_code", "message",
                     "rows_affected", "duration_ms")


def audit_report(store, since_days=None, server=None, limit=None, status=None, before=None):
    """The ``audit-1.0`` envelope: newest records first, with the retention
    setting. Every filter (*since_days*, *server*, *status*) applies before
    *limit*; *before* (a record id) continues a list after that record, and
    ``has_more`` says whether older records match."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000):
        raise ValueError('limit must be an integer from 1 to 100000.')
    fetch = limit + 1 if limit is not None and limit < 100000 else limit
    records = store.audit_records(since_days=since_days, server=server, limit=fetch, status=status, before=before)
    more = fetch != limit and len(records) > limit
    return {"schema_version": AUDIT_SCHEMA_VERSION, "retention_days": store.audit_retention_days(),
            "records": records[:limit] if more else records, "has_more": more}


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value)
    # Spreadsheets run cells that start with these as formulas.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text


def audit_csv(records):
    """Records as CSV (one row per record; statements joined by newlines)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(AUDIT_CSV_COLUMNS)
    for r in records:
        c = r.get("connection") or {}
        values = {"profile_name": c.get("profile_name"), "connection_type": c.get("type"),
                  "read_only": c.get("read_only"), "host": c.get("host"), "port": c.get("port"),
                  "user": c.get("user"), "engine": c.get("engine"), "server_version": c.get("server_version"),
                  "statements": "\n".join(r.get("statements") or [])}
        writer.writerow([_csv_cell(values[k] if k in values else r.get(k)) for k in AUDIT_CSV_COLUMNS])
    return buffer.getvalue()


def audit_export(store, fmt="json", since_days=None, server=None, status=None):
    """``{"format", "content", "mime", "filename"}`` for every matching record."""
    if fmt not in ("json", "csv"):
        raise ValueError("Choose export format 'json' or 'csv'.")
    report = audit_report(store, since_days=since_days, server=server, status=status)
    report.pop("has_more", None)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "csv":
        content, mime = audit_csv(report["records"]), "text/csv"
    else:
        content, mime = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", "application/json"
    return {"format": fmt, "content": content, "mime": mime, "filename": "myflames-audit-%s.%s" % (stamp, fmt)}
