"""The shared safety layer for every statement myflames sends that changes server state.

Every write path (``locks.kill_session`` today; the Query editor, variable
changes and replication actions next) goes through the same four parts:

* :func:`classify` reads SQL text and says whether it only reads, writes, or
  is a risky write, with the reasons. It splits scripts into statements the
  way the mysql client does (comments, quoted strings and identifiers under
  both backslash readings, executable comments ``/*! */`` and ``/*M! */``
  whose content is code, ``DELIMITER`` lines), using :func:`lex_sql` for
  every quote and comment boundary. It is a hint:
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
import bisect
import csv
import datetime
import functools
import getpass
import heapq
import io
import json
import re
import sys
import time
import uuid

from .live import DASH_CONTROL
from .server_info import parse_version_tuple
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

AMBIGUOUS_REASON = ("SQL reads differently under NO_BACKSLASH_ESCAPES, ANSI_QUOTES, MariaDB MSSQL mode, "
                    "server versions or client families; all possible readings were classified.")
_IDENT = r"(?:`\d+`|[A-Z_$][A-Z0-9_$]*)"


def sql_mode_flags(sql_mode, engine=None, server_version=None):
    """The lexical flags of a known mode, including server combination names.

    mysql-5.7.44 sql/sys_vars.cc:3794-3839; mysql-8.0.46:4896-4909.
    MariaDB sql/sys_vars.cc:3999-4039 and sql/sql_class.h:168-192.
    None means unknown, not an empty mode. Without version facts this single
    reading includes legacy aliases; lexer_readings also checks modern modes.
    """
    if not isinstance(sql_mode, str):
        return None
    names = {name.strip().upper() for name in sql_mode.split(",")}
    ansi = {"ANSI_QUOTES", "ANSI"}
    version = parse_version_tuple(server_version, leading=True, default=None)
    if engine != "mysql" or version is None or version < (8, 0, 0):
        ansi.update(("ORACLE", "MSSQL", "POSTGRESQL", "DB2", "MAXDB"))
    return {"backslash_escapes": "NO_BACKSLASH_ESCAPES" not in names,
            "ansi_quotes": bool(names & ansi),
            "mssql": engine != "mysql" and "MSSQL" in names}


def oracle_mode(sql_mode, engine):
    """Whether a known MariaDB session uses its PL/SQL stored-program grammar."""
    return engine == "mariadb" and isinstance(sql_mode, str) and "ORACLE" in {
        name.strip().upper() for name in sql_mode.split(",")}


def _synced_mariadb_ansi_quotes(engine, server_ansi_quotes):
    """Expected MariaDB-client ANSI_QUOTES status bit after sync, or None.

    MariaDB servers advertise SERVER_STATUS_ANSI_QUOTES when the mode is active
    (mariadb-11.8.9 sql/sys_vars.cc:4002-4009). MySQL servers never define or
    send that bit (mysql-8.4.0 include/mysql_com.h:838, sql/sys_vars.cc:4710-4718),
    so a MariaDB client talking to MySQL always holds False after sync.
    """
    if engine == "mariadb":
        return bool(server_ansi_quotes)
    if engine == "mysql":
        return False
    return None


def _client_ansi_statuses(family, engine, server_ansi_quotes, synced):
    """Possible client_ansi_quotes readings; single value only when testable."""
    if family == "mysql":
        return (False,)
    if synced:
        expected = _synced_mariadb_ansi_quotes(engine, server_ansi_quotes)
        if expected is not None:
            return (expected,)
    return (False, True)


def lexer_readings(backslash_escapes=None, engine=None, sql_mode=None, server_version=None,
                   client_engine=None, sql=None, client_status_synced=False):
    """Bounded complete readings, never a mixture of independently masked tokens.

    Unknown server facts need all three version-number grammars. Keep the
    actual engine unknown so a guessed dialect cannot authorize a version
    gate. The client family is independent of the server it connects to.
    """
    version = parse_version_tuple(server_version, leading=True, prefer_mariadb=engine == "mariadb", default=None)
    flags = sql_mode_flags(sql_mode, engine, server_version)
    modes = [flags] if flags is not None else [
        {"backslash_escapes": b, "ansi_quotes": a, "mssql": m}
        for b in ((True, False) if backslash_escapes is None else (bool(backslash_escapes),))
        for a in (False, True)
        for m in ((False,) if engine == "mysql" else (False, True))]
    if flags is not None and (engine not in ("mysql", "mariadb") or (engine == "mysql" and version is None)):
        for candidate in (sql_mode_flags(sql_mode, "mysql", "5.7.44"),
                          sql_mode_flags(sql_mode, "mysql", "8.1.0")):
            if candidate not in modes:
                modes.append(candidate)
    if engine == "mariadb":
        dialects = ("mariadb",)
    elif engine == "mysql" and version is not None:
        dialects = ("mysql_legacy" if version < (8, 1, 0) else "mysql",)
    else:
        dialects = ("mysql", "mysql_legacy", "mariadb")
    clients = (client_engine,) if client_engine in ("mysql", "mariadb") else ("mysql", "mariadb")
    # Stored programs restore sql_mode without refreshing the reply's quote
    # bits. MariaDB's SET STATEMENT sql_mode=... FOR does the same: its reply
    # is sent before the restore runs the sql_mode update hook
    # (mariadb-11.8.9 sql/sql_parse.cc:2467-2468,1934-1935, before the
    # restore at 2536 and 1967). Either bit can therefore disagree with
    # either value of the mode,
    # on direct connections too. Only a preceding sql_mode update reply lets
    # a caller select the synchronized reading. MySQL never advertises ANSI.
    # mysql-8.4.0:client/mysql.cc:2771-2784,sql/sys_vars.cc:4710-4718,
    # sql/sp_head.cc:2106-2107,2305;
    # mariadb-11.8.9:client/mysql.cc:2659-2663,sql/sys_vars.cc:3997-4011,
    # sql/sp_head.cc:1193-1194,1489.
    readings = [dict(mode, comment_dialect=dialect, client_engine=family, client_ansi_quotes=status,
                     client_backslash_escapes=client_backslash)
                for mode in modes for dialect in dialects for family in clients
                for status in _client_ansi_statuses(family, engine, mode["ansi_quotes"], client_status_synced)
                for client_backslash in ((mode["backslash_escapes"],) if client_status_synced else (True, False))]
    if sql is None:
        return readings
    # Do not rescan a long unquoted statement for modes with no applicable
    # characters. Executable comments keep every digit grammar; double quotes
    # with backslashes keep the unknown server's ANSI status-bit alternatives.
    has_backslash, has_double, has_bracket = "\\" in sql, '"' in sql, "[" in sql
    has_comments = "/*" in sql
    dialect_matters = has_comments or (engine not in ("mysql", "mariadb") and has_backslash and has_double)
    client_matters = (has_backslash or "/*M!" in sql or "/*+" in sql or
                      re.search(r"^[ \t\r\f\v]*delimiter", sql, re.I | re.M | re.ASCII) is not None)
    unique, seen = [], set()
    for reading in readings:
        key = (reading["backslash_escapes"] if has_backslash else None,
               reading["ansi_quotes"] if has_double else None,
               reading["mssql"] if has_bracket else None,
               reading["comment_dialect"] if dialect_matters else None,
               reading["client_engine"] if client_matters else None,
               reading["client_ansi_quotes"] if has_backslash and has_double else None,
               reading["client_backslash_escapes"] if has_backslash else None)
        if key not in seen:
            seen.add(key)
            unique.append(reading)
    return unique


CLIENT_STATUS_SYNC = "SET SESSION sql_mode = @@SESSION.sql_mode"
CLIENT_STATUS_SYNC_REASON = ("A helper SET refreshes the client's quote status immediately before this statement; "
                             "it keeps the session's current sql_mode.")
CLIENT_STATUS_SYNC_REQUIRED = ("The session quote mode is unknown or the session is not bound. "
                               "The client's NO_BACKSLASH_ESCAPES or ANSI_QUOTES status can change how this SQL is read. "
                               "Open a bound session with a known sql_mode before running it.")


def client_status_probe(delimiter, backslash_escapes, ansi_quotes=None, client_engine=None,
                        server_ansi_quotes=None):
    """Build (sql, expected_value) to confirm the client's active quote bits.

    Under the expected bits the client reads one SELECT; under any stale bit
    it splits at the embedded copy of *delimiter* and sends a prefix with an
    unclosed quote that the server rejects before later packets are scanned.
    Comments after the embedded delimiter keep trailing quotes balanced so an
    interactive client never swallows its following delimiter marker.
    """
    if (not isinstance(delimiter, str) or not delimiter or
            any(ch in delimiter for ch in "'\"\\`\r\n\t\f\v ") or "/*" in delimiter or "*/" in delimiter):
        raise ValueError("Invalid client status probe delimiter.")
    if not backslash_escapes:
        return "SELECT '\\''" + delimiter + "' /* ' */ AS mf_probe", "\\'" + delimiter
    sql = "SELECT '\\'" + delimiter + "' /* ' */ AS mf_probe"
    if ansi_quotes is not None and client_engine != "mysql":
        srv_ansi = ansi_quotes if server_ansi_quotes is None else bool(server_ansi_quotes)
        if ansi_quotes and not srv_ansi:
            raise ValueError("Invalid client status probe ANSI mode.")
        if ansi_quotes and client_engine is None:
            sql += ' /*M!999999 "\\""' + delimiter + '" */ /* " */'
        elif not ansi_quotes and srv_ansi:
            sql += ' /*!99999 "\\"' + delimiter + '" */ /* " */'
        elif ansi_quotes:
            sql += ", 1 AS \"\\\"\"" + delimiter + "\" /* \" */"
        else:
            sql += ", 1 AS \"\\\"" + delimiter + "\" /* \" */"
    return sql, "'" + delimiter


def client_status_probe_spec(engine, server_version, sql_mode, client_engine, delimiter):
    """Return (probe_sql, expected_columns, expected_row) using the synced-bit rule."""
    flags = sql_mode_flags(sql_mode, engine, server_version)
    if flags is None:
        return None, None, None
    backslash = flags["backslash_escapes"]
    probe_ansi = (_synced_mariadb_ansi_quotes(engine, flags["ansi_quotes"])
                  if backslash and client_engine != "mysql" else None)
    probe_sql, expected_value = client_status_probe(
        delimiter, backslash, ansi_quotes=probe_ansi, client_engine=client_engine,
        server_ansi_quotes=flags["ansi_quotes"])
    expected_columns = ["mf_probe"]
    expected_row = [expected_value]
    if backslash and probe_ansi is not None and client_engine != "mysql" and (
            not flags["ansi_quotes"] or (engine == "mariadb" and client_engine == "mariadb")):
        expected_columns.append(('\\"' + delimiter) if probe_ansi else ('"' + delimiter))
        expected_row.append("1")
    return probe_sql, expected_columns, expected_row


def client_status_sensitive(sql, delimiter=";", client_engine=None):
    """Whether stale reply bits can change one client's framing or bytes.

    Compare complete client scans, including newline/command/closer effects,
    independently of server modes. ANSI changes a token's label even when
    its boundaries agree, so string/identifier labels alone do not trigger a
    SET. Client-family or dollar-grammar disagreements still need their own
    validation after synchronization; a SET cannot repair those differences.
    """
    if "\\" not in sql:
        return False
    families = (client_engine,) if client_engine in ("mysql", "mariadb") else ("mysql", "mariadb")
    for family in families:
        for dollars in ((False, True) if family == "mysql" and "$" in sql else (False,)):
            reference = None
            for escaping in (True, False):
                for ansi in ((False, True) if family == "mariadb" else (False,)):
                    scan = lex_sql(sql, client=True, client_delimiter=delimiter, client_engine=family,
                                   client_backslash_escapes=escaping, client_ansi_quotes=ansi,
                                   dollar_quotes=dollars)
                    meaning = ([("quoted" if kind in ("string", "identifier") else kind, start, end)
                                for kind, start, end in scan["tokens"]], scan["unclosed"], scan["issues"])
                    if reference is None:
                        reference = meaning
                    elif meaning != reference:
                        return True
    return False


def _comment_header(sql, at, engine, version_id, wsrep_on, comment_dialect=None):
    """An executable opener and its server disposition, without reading its body."""
    maria = sql.startswith("/*M!", at)
    start = at + (4 if maria else 3)
    reversed_comment = sql[start:start + 1] == "!"
    digits_at = start + int(reversed_comment)
    dialect = comment_dialect or ("mysql_legacy" if engine == "mysql" and
                                  (version_id is None or version_id < 80100) else engine)
    # mysql-5.7.44 sql/sql_lex.cc:1923-1938 and mysql-8.0.46:1892-1910
    # always consume five digits. mysql-8.1.0:1911-1929 adds the sixth.
    pattern = {"mariadb": r"[0-9]{5,6}", "mysql_legacy": r"[0-9]{5}"}.get(
        dialect, r"(?:[0-9]{6}(?=[ \t\r\n\f\v])|[0-9]{5})")
    digits = re.match(pattern, sql[digits_at:digits_at + 7])
    required = int(digits.group()) if digits else None
    end = digits_at + (len(digits.group()) if digits else 0)
    problem = None
    if reversed_comment:
        executes, problem = None, "reversed executable comment"
    elif engine == "mysql" and maria:
        executes = False
    elif dialect == "mysql_legacy" and required is not None and required >= 80100:
        # This grammar exists only below 8.1. Even with an unknown version,
        # a server using it cannot execute a gate of 80100 or higher. Do not
        # invent a leading sixth digit of SQL in an impossible execution.
        executes = False
        if version_id is None or engine not in ("mysql", "mariadb"):
            problem = "unknown version for executable comment"
    elif engine not in ("mysql", "mariadb") and (maria or required is not None):
        executes, problem = None, "unknown engine for executable comment"
    elif engine == "mariadb" and not maria and required == 99997 and wsrep_on is not False:
        # A local wsrep session executes this otherwise skipped marker.
        # mariadb-server/sql/sql_lex.cc:2492-2500.
        executes, problem = None, "MariaDB wsrep executable comment /*!99997"
    elif engine == "mariadb" and not maria and required is not None and 50700 <= required <= 99999:
        executes = False
    elif required is not None and version_id is None:
        executes, problem = None, "unknown version for executable comment"
    else:
        executes = required is None or required <= version_id
    return {"start": at, "opener_end": end, "opener": sql[at:end],
            "executes": executes, "certain": executes is not None and problem is None, "problem": problem,
            "maria": maria, "required": required}


def _hint_quotes_closed(body):
    """Hint quotes must close before the first */, with doubling as escape.

    mysql-5.7.44 sql/sql_lex_hints.h:150-188 (quoted identifiers);
    mysql-8.0.46:110-147 (also quoted text). Clients do not see hint
    quotes, so comment markers and backslashes are checked separately,
    including inside otherwise balanced spans.
    """
    pos, quote = 0, None
    while pos < len(body):
        char = body[pos]
        if quote is None:
            if char in "'\"`":
                quote = char
        elif char == quote:
            if body[pos + 1:pos + 2] == quote:
                pos += 1
            else:
                quote = None
        pos += 1
    return quote is None


def lex_sql(sql, backslash_escapes=True, engine=None, server_version=None,
            dollar_quotes=False, client=False, unknown_execute=True, wsrep_on=None,
            client_delimiter=None, framing=(), ansi_quotes=False, mssql=False,
            sql_mode=None, comment_dialect=None, client_engine=None, client_ansi_quotes=False,
            client_backslash_escapes=None, _cache=None, _framing_key=None):
    """Read SQL once, preserving source spans and every lexical uncertainty.

    Tokens are (kind, start, end). Executed comment openers/closers are
    separate tokens: quotes and line comments in their SQL bodies own their
    complete contents, including apparent ``*/`` markers. Ordinary comments
    consume no nested level; skipped versioned comments consume one.

    ``client`` selects the framing differences in the client's add_line:
    it does not evaluate version gates, and -- needs ASCII whitespace.
    Both passes use this same quote/comment scanner. Server code must never
    be derived from the client pass. Unknown version gates can be read both
    ways by the classifier; the editor requires bound facts before sending.

    Sources: mysql-server/sql/sql_lex.cc:1037-1066,1782-1812,1931-2075;
    mariadb-server/sql/sql_lex.cc:1639-1694,1824-1860,2401-2562;
    mysql-server/client/mysql.cc:2785-2799,2838-2965.
    """
    if not isinstance(sql, str):
        raise ValueError("Pass SQL text.")
    flags = sql_mode_flags(sql_mode, engine, server_version)
    if flags is not None:
        backslash_escapes, ansi_quotes, mssql = (flags[key] for key in
                                                ("backslash_escapes", "ansi_quotes", "mssql"))
    # MariaDB add_line honors ANSI_QUOTES and never escapes inside backticks
    # (mariadb-10.11.16 client/mysql.cc:2630-2634). MySQL ignores ANSI_QUOTES
    # (mysql-8.4.0 client/mysql.cc:2771-2785). Neither knows MSSQL brackets.
    # Only MariaDB servers advertise the ANSI status bit (mariadb-11.8.9
    # sql/sys_vars.cc:4002-4009; mysql-8.4.0:4710-4718), but a proxy can
    # consume that reply. This function scans one explicit status reading;
    # lexer_readings enumerates both even with a known server and mode.
    if client:
        ansi_quotes = client_engine == "mariadb" and client_ansi_quotes
        mssql = False
        if client_backslash_escapes is not None:
            backslash_escapes = client_backslash_escapes
    version = parse_version_tuple(server_version, leading=True, prefer_mariadb=engine == "mariadb", default=None)
    version_id = None if version is None else version[0] * 10000 + version[1] * 100 + version[2]
    # The splitter supplies the retained scan's integer identity. Derive its
    # delimiter map once; equal maps still share a lexer result when other
    # client token roles differ. Arbitrary caller-owned framing stays mutable
    # and is snapshotted on every call, as on the uncached path.
    saved_framing = _cache.get(("framing", _framing_key)) if _cache is not None and _framing_key is not None else None
    if saved_framing is None:
        framing_at = {start: end for kind, start, end in framing if kind in ("delimiter_command", "delimiter")}
        framing_key = tuple(sorted(framing_at.items())) if _cache is not None else None
        if _cache is not None and _framing_key is not None:
            values = _cache.setdefault("framing_values", {})
            framing_key = values.setdefault(framing_key, len(values))
            _cache[("framing", _framing_key)] = (framing_at, framing_key)
    else:
        framing_at, framing_key = saved_framing
    cache_key = None
    if _cache is not None:
        # Cache the scanner's effective inputs, after mode/status overrides.
        # The version/comment switches are only read in the /* branch. Keep
        # the delimiter even in server scans: optimizer-hint checks use it.
        comments = (version_id, unknown_execute, wsrep_on, comment_dialect) if "/*" in sql else ()
        cache_key = ("lex", sql, backslash_escapes, engine, dollar_quotes, client,
                     client_delimiter, framing_key, ansi_quotes, mssql,
                     client_engine, comments)
        try:
            cached = _cache.get(cache_key)
        except TypeError:
            # Preserve the uncached handling of unusual, unhashable options.
            cache_key = None
        else:
            if cached is not None:
                return cached
    tokens, comments, unclosed, issues = [], [], [], []
    n, pos, code_start = len(sql), 0, 0
    active, pending = None, False
    command_start = 0
    # Unit heads inside executable comments can all look past the same
    # trailing comments. Share their landing positions within this scan.
    command_comment_skips = {} if client else None

    def token(kind, start, end):
        if code_start < start:
            tokens.append(("code", code_start, start))
        tokens.append((kind, start, end))

    def consume_comment(start, nested):
        # consume_comment(1) calls consume_comment(0), not itself: one level
        # only, even when the inner text contains more /* openers.
        at = start
        while at < n:
            if sql.startswith("*/", at):
                return at + 2, True
            if nested and sql.startswith("/*", at):
                end = sql.find("*/", at + 2)
                if end < 0:
                    return n, False
                at = end + 2
            else:
                at += 1
        return n, False

    def check_comment_commands(start, end):
        # read_and_execute checks each physical line before add_line. MySQL
        # 8.2+ can discard an unclosed special-comment buffer as comment-only
        # (mysql-8.4.0:client/mysql.cc:2455-2481,2577-2593). Older MySQL
        # and MariaDB require an empty buffer, but refuse command-looking
        # lines in every comment kind on every client. Inspect raw comment
        # text, including quotes: the pre-line lookup is not the SQL lexer.
        for match in _COMMENT_COMMAND_LINE.finditer(sql, start + 2, end):
            name = match.group(1).upper()
            after = sql[match.end():match.end() + 1]
            if (name in ("DELIMITER", "\\") or not after or after in _CLIENT_SPACE + ";" or
                    (client_delimiter and sql.startswith(client_delimiter, match.end()))):
                issues.append((match.start(1), REASON_COMMENT_COMMAND))

    def check_quoted_newline(start, end):
        # add_line tests in_string at each physical line's END, including
        # the closing line of a dollar body. Earlier lines stay quoted and
        # retain their newlines. This is conservative when another quote or
        # comment later on the closing line protects or flushes that output.
        # mysql-8.1.0:client/mysql.cc:2764-2815;
        # mysql-8.4.0:client/mysql.cc:2931-2982.
        line = sql.rfind("\n", start, end) + 1
        if client and line > start and sql[line:line + 9].lower() == "delimiter":
            issues.append((line, REASON_DELIMITER_NEWLINE))

    while pos < n:
        ch = sql[pos]
        if pos in framing_at:
            end = framing_at[pos]
            token("framing", pos, end)
            pos = code_start = end
            continue
        if client:
            line_start = pos == 0 or sql[pos - 1] == "\n"
            if pos == command_start:
                # MySQL 8.2+ also looks past earlier lines containing only
                # block comments, including executed /*! SQL */. Inspect
                # this raw prefix once per unit before its SQL body can set
                # pending or open a quote. A DELIMITER consumed by our own
                # splitter loses this issue only after the server reading
                # proves that the discarded prefix contains no SQL.
                command = _client_named_command(sql, pos, client_delimiter, across_lines=True,
                                                _comment_skips=command_comment_skips)
                if command is not None and command[2]:
                    issues.append((command[0], REASON_NAMED_CLIENT_COMMAND % command[1]))
            if client_delimiter is not None and line_start and not pending and active is None:
                command = _DELIMITER_COMMAND.match(sql, pos)
                if command:
                    value, end = _delimiter_argument(sql, command.end(), client_engine)
                    if value and "\\" not in value:
                        client_delimiter = value
                    token("delimiter_command", pos, end)
                    pos = code_start = end
                    command_start = end
                    continue
            # add_line's newline guard is independent of find_command:
            # even delimiter_x at column zero loses the line ending after
            # pending SQL. The first nine bytes suffice, without a word
            # boundary. Strings and ordinary comments are consumed whole
            # below; special-comment command lines have their own refusal.
            # mysql-8.4.0:client/mysql.cc:1233-1244,2966-2982;
            # mariadb-11.8.9:client/mysql.cc:1185-1195,2878-2892.
            if line_start and active is None and sql[pos:pos + 9].lower() == "delimiter":
                issues.append((pos, REASON_DELIMITER_NEWLINE))
            if active is None and (line_start or pos == command_start):
                # MySQL 8.4.0/HEAD looks past leading /* ... */ before
                # add_line, even when those bytes are executable SQL or
                # contain quotes. Refuse instead of changing the reviewed
                # text or trying to retain the command's discarded tail.
                command = _client_named_command(sql, pos, client_delimiter,
                                                _comment_skips=command_comment_skips)
                if command is not None:
                    at, name, after_comment = command
                    if not pending or after_comment:
                        issues.append((at, REASON_NAMED_CLIENT_COMMAND % name))
            # add_line tests the delimiter before opening a quote/dollar body.
            if client_delimiter is not None and sql.startswith(client_delimiter, pos):
                end = pos + len(client_delimiter)
                token("delimiter", pos, end)
                pos = code_start = end
                command_start = end
                pending = False
                continue
        if active is not None and sql.startswith("*/", pos):
            if client:
                check_comment_commands(active["start"], pos)
                if (pos and sql[pos - 1] == "/") or sql[pos + 2:pos + 3] == "*":
                    issues.append((pos, REASON_COMMENT_OVERLAP))
            # The conditional-comment branch in add_line clears ss_comment
            # at '*', then examines '/' on the next iteration. A delimiter
            # can start at that slash (client/mysql.cc:2946-2948).
            # A following '*' can instead reopen /*. Servers and MariaDB
            # 12.0.1 consume both closer bytes for /*! and /*+ (not /*M!),
            # so refuse that overlap for all clients
            # (mariadb-12.0.1:client/mysql.cc:2779-2792).
            close_end = pos + (1 if client else 2)
            token("executable_close", pos, close_end)
            active["end"] = pos + 2
            active = None
            pos = close_end
            code_start = pos
            continue
        if ch in "'\"`" or (ch == "[" and mssql and engine != "mysql"):
            start, quote = pos, "]" if ch == "[" else ch
            identifier = ch in "`[" or (ch == '"' and ansi_quotes)
            pos += 1
            closed = False
            while pos < n:
                if sql[pos] == quote:
                    # add_line closes at the first quote, then checks the
                    # delimiter before the next quote can reopen a string.
                    # The server pass must respect that same packet break.
                    # mysql-8.4.0:client/mysql.cc:2824-2825,2931-2947;
                    # mariadb-11.8.9:client/mysql.cc:2729,2857-2863.
                    delimiter_next = ((client and client_delimiter is not None and
                                       sql.startswith(client_delimiter, pos + 1)) or pos + 1 in framing_at)
                    if pos + 1 < n and sql[pos + 1] == quote and not delimiter_next:
                        pos += 2
                        continue
                    pos += 1
                    closed = True
                    break
                # Server identifiers escape only by doubling. The MySQL
                # client consumes backslash pairs even inside backticks,
                # except that a following backtick still closes the name.
                escapes = backslash_escapes and (not identifier or (client and client_engine != "mariadb"))
                if sql[pos] == "\\" and escapes:
                    if client and (pos + 1 == n or sql[pos + 1] == "\n" or sql.startswith("\r\n", pos + 1)):
                        issues.append((pos, REASON_TRAILING_BACKSLASH))
                    pos = min(n, pos + (1 if client and quote == "`" and sql[pos + 1:pos + 2] == "`" else 2))
                else:
                    pos += 1
            token("identifier" if identifier else "string", start, pos)
            pending = True
            if not closed:
                unclosed.append((start, pos))
            elif client:
                check_quoted_newline(start, pos)
            code_start = pos
            continue
        if dollar_quotes and ch == "$" and (pos == 0 or not (
                sql[pos - 1].isalnum() or sql[pos - 1] in "_$" or ord(sql[pos - 1]) >= 128)):
            dollar = re.match(r"\$[\w\x80-\U0010ffff]*\$", sql[pos:])
            if dollar:
                start, tag = pos, dollar.group()
                end = sql.find(tag, pos + len(tag))
                pos = n if end < 0 else end + len(tag)
                token("string", start, pos)
                pending = True
                if end < 0:
                    unclosed.append((start, pos))
                elif client:
                    check_quoted_newline(start, pos)
                code_start = pos
                continue
        dash = sql.startswith("--", pos)
        after_dash = sql[pos + 2:pos + 3]
        dash_comment = dash and (not after_dash or after_dash in _CLIENT_SPACE or
                                 (not client and (ord(after_dash) < 32 or ord(after_dash) == 127)))
        if dash and DASH_CONTROL.match(sql, pos):
            issues.append((pos, REASON_DASH_CONTROL))
        if ch == "#" or dash_comment:
            end = sql.find("\n", pos)
            end = n if end < 0 else end
            token("comment", pos, end)
            pos = code_start = end
            continue
        if sql.startswith("/*", pos):
            # mariadb-11.8.9:client/mysqldump.cc:798;
            # mariadb-10.11.16:client/mysqldump.c:750.
            if sql.startswith("/*M!999999\\- enable the sandbox mode */", pos):
                issues.append((pos, REASON_DUMP_SANDBOX))
            special = sql.startswith("/*!", pos) or (sql.startswith("/*M!", pos) and
                                                       (not client or client_engine == "mariadb"))
            if client and active is not None:
                # add_line's ml_comment and ss_comment interact:
                # the first */ can clear ss_comment while leaving ml_comment
                # open. Do not pretend the nested comment is an independent
                # token (mysql-9.7.0:client/mysql.cc:2913-2948).
                issues.append((pos, REASON_NESTED_CLIENT_COMMENT))
            header = _comment_header(sql, pos, engine, version_id, wsrep_on, comment_dialect) if special else None
            # Inside executed code an ordinary /* ... */ is scanned just
            # like any other SQL comment. It restores the enclosing state.
            if header is not None:
                comments.append(header)
                execute = client or header["executes"] is True or (
                    header["executes"] is None and unknown_execute)
                if execute:
                    if active is not None:
                        # The server has a single in_comment state, not an
                        # unbounded stack of executable comment bodies.
                        active["end"] = pos
                    # add_line does not consume a server version number or
                    # an atomic opener. Its next character can itself start
                    # a client delimiter, even the '*' or '!' in this marker.
                    opener_end = pos + 1 if client else header["opener_end"]
                    token("executable_open", pos, opener_end)
                    active = header
                    pos = code_start = opener_end
                    continue
            # MySQL sees /*M! as an ordinary comment, not consume_comment(1).
            nested = bool(header and not (engine == "mysql" and header["maria"]))
            end, closed = consume_comment(pos + 2, nested and not client)
            token("comment", pos, end)
            if client:
                check_comment_commands(pos, end - 2 if closed else end)
            if client and closed:
                # add_line's ordinary opener has no !ml_comment guard:
                # /*/ consumes the '*' before the closer can see it.
                # mysql-5.7.44:client/mysql.cc:2721-2738; mysql-8.0.46:2616-2628;
                # mysql-8.4.0:2895-2908; mysql-9.7.0:2913-2926;
                # mariadb-10.11.16:2788-2805; mariadb-11.8.9:2817-2834;
                # mariadb-12.0.1:2739-2758. A hint's closing slash can
                # also start another /* (mysql-8.4.0:2928-2930).
                if sql[end - 3:end] == "/*/" or sql[end - 1:end + 1] == "/*":
                    issues.append((pos, REASON_COMMENT_OVERLAP))
            if client and header is None and not sql.startswith("/*+", pos):
                # mysql-8.4.0:client/mysql.cc:2907-2930 sets ss_comment for
                # /*! or /*+ even while ml_comment is open. The first */
                # then clears only ss_comment, unlike the server's first-*/
                # boundary. MariaDB has the same /*! interaction
                # (mariadb-11.8.9:client/mysql.cc:2834-2859); /*M! does not
                # set ss_comment there. Refuse all three special openers on
                # every client, across lines too, without modelling version-
                # specific state. Quotes and #/-- inside this body are data.
                body = sql[pos + 2:end - 1 if closed else end]
                if any(opener in body for opener in ("/*!", "/*+", "/*M!")):
                    issues.append((pos, REASON_NESTED_ORDINARY_COMMENT))
            if header is not None:
                header["end"] = end
            if not closed:
                unclosed.append((pos, end))
            if _READYSET_CREATE_HINT.match(sql[pos:end]):
                issues.append((pos, REASON_READYSET_HINT))
            if sql.startswith("/*+", pos) and "\\" in sql[pos:end]:
                at = sql.index("\\", pos, end)
                issues.append((at, REASON_CLIENT_COMMAND % sql[at + 1:at + 2]))
            if sql.startswith("/*+", pos):
                body = sql[pos + 3:end - 2 if closed else end]
                # Include the closer's '*' only for the opener check. Keep
                # the actual body for quotes and the conservative '--' at
                # body-end check; the closing marker is not hint content.
                marker_body = sql[pos + 3:end - 1 if closed else end]
                # Nested /*! changes MySQL's SSC_HINT to SSC_CONDITIONAL
                # even on one line. It also sets conditional state in
                # MariaDB 10.11-11.8 ordinary /*+ comments. The # / -- hint
                # bug affects MySQL 5.7.44, 8.0.42, 8.1.0, 8.4.0, 8.4.5 and
                # 9.1.0; fixed in 9.2.0 (514931dba1e, Bug#30875669) and
                # 8.0.43/8.4.6 (94a29a8a117, Bug#37572191).
                # Refuse hazardous bodies on every client,
                # independent of its patch release.
                if ("/*" in marker_body or any(mark in body for mark in ("#", "\\")) or
                        re.search(r"--(?:\s|$)", body) or
                        not _hint_quotes_closed(body)):
                    issues.append((pos, REASON_HINT))
            # MySQL's ss_comment is local to add_line, so after the first
            # newline a hint's quotes/comments/delimiters become client code.
            # Refuse those forms instead of guessing how the private frame
            # survives them. The all-client hint restriction above also
            # covers the one-line nested/comment cases.
            if sql.startswith("/*+", pos):
                body = sql[pos + 3:end - 2 if closed else end]
                multiline = "\n" in body or "\r" in body
                if multiline and (any(quote in body for quote in "'\"`") or
                                  (client_delimiter is not None and client_delimiter in body) or
                                  (client and client_engine != "mariadb" and
                                   re.search(r"(?<![\w$\x80-\U0010ffff])\$[\w\x80-\U0010ffff]*\$", body))):
                    issues.append((pos, REASON_MULTILINE_HINT))
            if (client and client_engine != "mariadb" and sql.startswith("/*+", pos) and closed and client_delimiter is not None and
                    sql.startswith(client_delimiter, end - 1)):
                issues.append((end - 1, REASON_EXECUTED_FRAMING))
            pos = code_start = end
            continue
        # The client can discard a command and all its parameters, including
        # a /* that the server pass would otherwise treat as a comment.
        # Check both complete readings: a quote in skipped conditional SQL
        # must not hide a command exposed by the other pass. Quoted/comment
        # tokens have already been consumed above, so their text stays data.
        if ch == "\\" and sql[pos + 1:pos + 2] not in ("g", "G", "N"):
            issues.append((pos, REASON_CLIENT_COMMAND % sql[pos + 1:pos + 2]))
        if ch not in _CLIENT_SPACE:
            pending = True
        pos += 1
    if code_start < n:
        tokens.append(("code", code_start, n))
    if active is not None:
        if client:
            check_comment_commands(active["start"], n)
        active["end"] = n
        active["problem"] = "unterminated executable comment"
        unclosed.append((active["start"], n))
    result = {"tokens": tokens, "comments": comments, "unclosed": unclosed, "issues": issues}
    if cache_key is not None:
        # Different mode inputs can yield identical complete scans. Retain
        # one result per interned meaning, not another large token list for
        # every input key. Only canonical scans need their ids kept alive.
        scan_key = _scan_cache_key(result, _cache)
        shared = _cache.setdefault("scans", {}).setdefault(scan_key, result)
        if shared is not result:
            del _cache["scan_keys"][id(result)]
        result = shared
        _cache[cache_key] = result
    return result


def skipped_executable_comments(sql, engine, server_version, backslash_escapes=None, wsrep_on=None,
                                sql_mode=None):
    """Executable comments skipped or uncertain under the server's own lexer.

    Return source offsets, the opener (never SQL text), and ``certain``.
    Unknown quote modes inspect both readings. Executed comments with an
    unterminated body are included as uncertain, so callers can refuse them.
    """
    found = {}
    for reading in lexer_readings(backslash_escapes, engine, sql_mode, server_version, sql=sql):
        # Scan both possible dispositions when server facts are incomplete.
        for unknown_execute in (True, False):
            scan = lex_sql(sql, engine=engine, server_version=server_version,
                           unknown_execute=unknown_execute, wsrep_on=wsrep_on, **reading)
            for comment in scan["comments"]:
                if comment["executes"] is False or comment["problem"]:
                    item = {k: comment[k] for k in ("start", "end", "opener", "certain")}
                    if comment["problem"]:
                        item.update(certain=False, problem=comment["problem"])
                    found[(item["start"], item["end"])] = item
            if not any(comment["executes"] is None for comment in scan["comments"]):
                break
    return [found[span] for span in sorted(found)]


#: A line whose first word starts with "delimiter" is the client's DELIMITER
#: command in --binary-mode (client/mysql.cc find_command and
#: is_delimiter_command compare only the first 9 characters).
_DELIMITER_COMMAND = re.compile(r"[ \t\v\f\r]*(delimiter[^ \t\r\n\f\v]*)", re.I | re.ASCII)
_DELIMITER_LINE = re.compile(r"^[ \t\v\f\r]*delimiter[^ \t\r\n\f\v]*", re.I | re.M | re.ASCII)
_CLIENT_SPACE = " \t\n\r\f\v"
_NAMED_CLIENT_COMMAND = re.compile(r"delimiter|charset|sandbox", re.I | re.ASCII)
# Union of named commands in the supported client command tables, used only
# for command-looking lines *inside* block comments. Ordinary SQL keeps the
# narrower checks above. mysql-8.4.0:client/mysql.cc:404-452;
# mariadb-12.0.1:client/mysql.cc:381-426 (also costs and sandbox).
_COMMENT_COMMAND_LINE = re.compile(
    r"\n[ \t\r\f\v]*(\\|delimiter|charset|sandbox|clear|connect|edit|ego|exit|go|help|"
    r"nopager|notee|pager|print|prompt|quit|rehash|source|status|system|tee|use|warnings|"
    r"nowarning|resetconnection|query_attributes|ssl_session_data_print|costs|\?)", re.I | re.ASCII)


def _client_named_command(sql, start, delimiter, across_lines=False, _comment_skips=None):
    """A command at a line/unit head, conservatively skipping block comments.

    mysql-8.4.0 client/mysql.cc:2453-2464,2577-2593 runs this lookup
    before reading any quotes in the comments. The older MySQL and MariaDB
    clients do not skip the prefix, but the editor refuses it on both.
    The unit-head check crosses newlines too: executable comments count as
    comments here, whatever the server does with their bodies. Line-local
    checks keep their bounded lookahead while a statement is pending.
    """
    at, after_comment = start, False
    skipped = []
    while True:
        # Cache only the raw comment-prefix landing, never the command:
        # DELIMITER can change during a scan. Line-local and cross-line
        # lookups must also keep separate entries. Every recorded prefix
        # skips a comment, preserving after_comment on a cache hit.
        key = (across_lines, at)
        if _comment_skips is not None and key in _comment_skips:
            at, after_comment = _comment_skips[key], True
            break
        while at < len(sql) and sql[at] in (_CLIENT_SPACE if across_lines else " \t\r\f\v"):
            at += 1
        if across_lines and (sql.startswith("#", at) or (sql.startswith("--", at) and
                (at + 2 == len(sql) or sql[at + 2] in _CLIENT_SPACE))):
            skipped.append(key)
            end = sql.find("\n", at)
            if end < 0:
                at = len(sql)
                break
            at, after_comment = end + 1, True
            continue
        if not sql.startswith("/*", at):
            break
        skipped.append(key)
        end = sql.find("*/", at + 2)
        if end < 0:
            at = len(sql)
            break
        at, after_comment = end + 2, True
    if _comment_skips is not None:
        for key in skipped:
            _comment_skips[key] = at
    command = _NAMED_CLIENT_COMMAND.match(sql, at)
    if command is None:
        return None
    name, end = command.group().upper(), command.end()
    # add_line also looks up the completed buffer after removing its
    # delimiter. Thus charset; and charset// can be command spellings,
    # while CHARSET(...) and identifiers such as charset_name remain SQL.
    if name != "DELIMITER" and end < len(sql) and sql[end] not in _CLIENT_SPACE and not (
            delimiter is not None and sql.startswith(delimiter, end)):
        return None
    return at, name, after_comment


#: Only ASCII white space separates tokens: both lexers read a non-ASCII
#: space such as U+00A0 or U+3000 as an identifier character (a multibyte lead
#: byte), so ``CONCAT\u00a0(`` names another function; str.split() would
#: drop it.
_CODE_SPACE = re.compile("[%s]+" % re.escape(_CLIENT_SPACE))
#: Flags every write sends to the mysql or mariadb client: keep comments (the
#: myflames tag) and turn the client's backslash commands off (--binary-mode
#: disables all but \C and DELIMITER in non-interactive use; MariaDB also
#: recognizes \- (sandbox). The lexer refuses those remaining commands).
WRITE_CLIENT_FLAGS = ("--comments", "--binary-mode")
REASON_CLIENT_COMMAND = ("Client backslash commands (\\%s) are not supported outside SQL strings and comments. "
                         "Remove the command before running this SQL.")
REASON_DUMP_SANDBOX = ("Found the mariadb-dump sandbox line, a mariadb client command; "
                       "delete that line to run the rest here, or use Maintenance > Restore for a whole dump.")
REASON_NAMED_CLIENT_COMMAND = ("The client command %s is not supported here. "
                               "Put DELIMITER on its own line at the script start or after a complete statement, "
                               "with only whitespace or ordinary comments on earlier lines; use SQL instead of other client commands.")
REASON_COMMENT_COMMAND = ("A multi-line comment contains a line starting with a client command. "
                          "Move the command outside the comment or reword that line before running this SQL.")
REASON_DASH_CONTROL = ("'--' before a control character is code to the client and a comment to the server, so the "
                       "statement boundaries are uncertain.")
REASON_DELIMITER = ("A DELIMITER line needs a nonempty delimiter without backslashes, NUL, carriage returns, "
                    "or leading or trailing whitespace. Close quoted arguments and use a value both "
                    "client families read alike when the family is unknown. "
                    "Use at most 15 UTF-8 bytes for the delimiter and 255 UTF-8 bytes for the command line. "
                    "Start the delimiter with an ASCII character. "
                    "Quote the delimiter on a CRLF line, or use LF line endings.")
REASON_DELIMITER_NEWLINE = ("A SQL line starts with DELIMITER at column zero. The client can remove its newline "
                            "and join it to the next line. Indent or quote this identifier, avoid a line break "
                            "inside a string just before DELIMITER, or put a DELIMITER "
                            "command after a complete statement.")
REASON_CRLF_TRANSPORT = ("The MySQL client on Windows removes carriage returns from CRLF lines. "
                         "Use LF line endings in this statement so the reviewed and sent text agree.")
REASON_COMMENT_FRAMING = ("A client delimiter splits a comment the server skips, so its SQL cannot be read "
                          "as one complete server comment.")
REASON_EXECUTED_FRAMING = ("A client delimiter splits an executable comment before it closes. "
                           "Close the comment before the delimiter.")
REASON_QUOTE_FRAMING = ("A client delimiter splits a server string or quoted identifier. "
                        "Use quoting that the client and server can frame together.")
REASON_NESTED_CLIENT_COMMENT = ("A nested /* inside an executable comment changes the client's comment state. "
                                "Use SQL without nested executable comments.")
REASON_NESTED_ORDINARY_COMMENT = ("An ordinary comment contains a nested /*!, /*+ or /*M! opener. "
                                  "Some clients can end this comment differently from the server. "
                                  "Remove the nested opener before running this SQL.")
REASON_COMMENT_OVERLAP = ("A /* opener overlaps a comment's closing */ marker. "
                          "Some clients read a different comment boundary. Separate these markers with whitespace.")
REASON_HINT = ("An optimizer hint (/*+ ... */) contains a comment marker "
               "(/*, # or -- followed by whitespace or a line ending), a backslash, or an unclosed quote. "
               "Close quoted values before */ and remove comment markers and backslashes, including inside quotes.")
REASON_MULTILINE_HINT = ("A multi-line optimizer hint contains quotes, dollar quotes or the client delimiter. "
                         "Keep this hint on one line or remove those characters.")
REASON_TRAILING_BACKSLASH = ("A line ends with a backslash inside a string or quoted identifier. "
                            "The client can change that text; move the backslash away from the line ending.")
REASON_READYSET_HINT = "A /*rs+ ... create ... */ hint creates a Readyset cache when run on Readyset."
_READYSET_CREATE_HINT = re.compile(r"^/\*rs\+.*\bcreate\b", re.I | re.S)


def _usable_delimiter(value):
    """Reject values the client's byte buffer or scanner cannot preserve."""
    if (not isinstance(value, str) or not value or any(ch in value for ch in "\\\r\n\x00") or
            value[0] in _CLIENT_SPACE or value[-1] in _CLIENT_SPACE or ord(value[0]) >= 128):
        return False
    # add_line copies a multibyte character before checking the delimiter
    # when the client character set is multibyte (use_mb(charset_info)),
    # so a UTF-8 delimiter must start with ASCII to be recognized.
    # mysql-8.4.0:client/mysql.cc:2759-2770;
    # mariadb-11.8.9:client/mysql.cc:2643-2657.
    try:
        return len(value.encode("utf-8")) <= 15
    except UnicodeEncodeError:
        return False


def _delimiter_argument(sql, at, client_engine=None):
    """The argument of a DELIMITER command line starting at *at*, read like
    the selected client's get_arg: (delimiter or None, end of line). Text
    after a closing quote is ignored; an unquoted value ends at a space,
    not a tab. MySQL accepts an unclosed quote; MariaDB requires closure
    and unescapes doubled quotes and backslash pairs except in backticks.
    An unknown client family requires both readings to agree.

    In binary mode, batch_readline keeps a CR before LF on Unix and in
    MariaDB, while MySQL Windows removes it. get_arg keeps CR in an unquoted
    argument. Refuse CR-bearing delimiters instead of silently stripping CR
    and choosing another platform's boundary. Keep offsets in the original
    text. mysql-8.4.0:client/readline.cc:75-95,client/mysql.cc:4861-4900;
    mariadb-11.8.9:client/readline.cc:38-44,client/mysql.cc:5023-5080.
    """
    newline = sql.find("\n", at)
    stop = len(sql) if newline < 0 else newline
    # com_delimiter truncates before get_arg, and truncates its result a
    # second time. Refuse instead of silently choosing different framing.
    # Count the whole physical line, including ignored trailing text and
    # indentation (older clients pass it to com_delimiter unchanged).
    # mysql-8.4.0:client/mysql.cc:237,4617-4632;
    # mariadb-11.8.9:client/mysql.cc:302,4883-4902.
    start = sql.rfind("\n", 0, at) + 1
    try:
        if len(sql[start:stop].encode("utf-8")) > 255:
            return None, stop
    except UnicodeEncodeError:
        return None, stop
    rest = sql[at:stop]
    def argument(family):
        pos = 0
        while pos < len(rest) and rest[pos] in _CLIENT_SPACE:
            pos += 1
        quote = rest[pos:pos + 1]
        quote = quote if quote in ("'", '"', "`") else ""
        pos += bool(quote)
        value, closed = [], not quote
        while pos < len(rest):
            ch = rest[pos]
            if (ch == "\\" and pos + 1 < len(rest) and
                    (not quote if family == "mysql" else quote != "`")):
                pos += 1
                value.append(rest[pos])
            elif quote and ch == quote and family == "mariadb" and rest[pos + 1:pos + 2] == quote:
                value.append(ch)
                pos += 1
            elif ch == (quote or " "):
                closed = True
                break
            else:
                value.append(ch)
            pos += 1
        value = "".join(value)
        # Whitespace at either edge can turn an inserted comment separator
        # into a delimiter. Reject it before review rather than changing
        # statement boundaries during pre-send revalidation.
        if not _usable_delimiter(value) or (family == "mariadb" and not closed):
            return None
        return value

    if client_engine in ("mysql", "mariadb"):
        value = argument(client_engine)
    else:
        value = argument("mysql")
        if value != argument("mariadb"):
            value = None
    return value, stop


# ---- splitting -----------------------------------------------------------------------

def _scan_cache_key(scan, cache):
    """Intern immutable scan contents once per object in one classify call.

    Keep the scan alive alongside its integer key so object ids cannot be
    reused. Full tuples are hashed only when interning a new scan, never
    for each statement's fragment lookup. The optional derived ends index
    does not change a scan's meaning.
    """
    keys = cache.setdefault("scan_keys", {})
    saved = keys.get(id(scan))
    if saved is None:
        key = (tuple(scan["tokens"]),
               tuple(tuple(sorted(comment.items())) for comment in scan["comments"]),
               tuple(scan["unclosed"]), tuple(scan["issues"]))
        values = cache.setdefault("scan_values", {})
        saved = keys[id(scan)] = (scan, values.setdefault(key, len(values)))
    return saved[1]


def _masked_fragment(sql, scan, start, end, _cache=None, _scan_key=None):
    """Mask one source range using the server tokens, never rescan with a regex."""
    cache_key = None
    if _cache is not None:
        if _scan_key is None:
            _scan_key = _scan_cache_key(scan, _cache)
        cache_key = ("mask", sql, _scan_key, start, end)
        try:
            cached = _cache.get(cache_key)
        except TypeError:
            cache_key = None
        else:
            if cached is not None:
                return cached
    code, idents, spans = [], [], []
    ends = scan.get("ends")
    if ends is None:
        ends = scan["ends"] = [token[2] for token in scan["tokens"]]
    at = bisect.bisect_right(ends, start)
    while at < len(ends):
        kind, left, right = scan["tokens"][at]
        if left >= end:
            break
        left, right = max(left, start), min(right, end)
        spans.append((kind, left, right))
        if kind == "code":
            code.append(sql[left:right])
        elif kind == "identifier":
            code.append("`%d`" % len(idents))
            quote = "]" if sql[left] == "[" else sql[left]
            idents.append(sql[left + 1:right - 1].replace(quote * 2, quote))
        elif kind == "string":
            code.append("''")
        else:
            code.append(" ")
        at += 1
    result = _CODE_SPACE.sub(" ", "".join(code)).strip(" "), idents, tuple(spans)
    if cache_key is not None:
        _cache[cache_key] = result
    return result


def split_statements(sql, backslash_escapes=True, delimiter=";", dollar_quotes=False,
                     engine=None, server_version=None, unknown_execute=True, wsrep_on=None,
                     ansi_quotes=False, mssql=False, sql_mode=None, comment_dialect=None, client_engine=None,
                     client_ansi_quotes=False, discarded_prefixes=None, client_backslash_escapes=None, _cache=None):
    """Client statement boundaries with code masked by the server's SQL lexer.

    ``text`` and offsets retain the original SQL. ``code``, ``idents`` and
    ``token_spans`` come exclusively from :func:`lex_sql`'s server reading.
    The client framing pass uses that same lexer with ``client=True``:
    conditional comments do not suppress client delimiters, regardless of
    whether the server will execute their contents. An unusable DELIMITER,
    client command or unclosed token is never a safe unit.

    DELIMITER/get_arg: mysql-server/client/mysql.cc:2661-2682,4652-4671,4897-4935;
    delimiter and line-comment recognition: :2838-2912; MariaDB :2746-2833.
    Dollar bodies use the optional newer client grammar (:2715-2746).
    If supplied, ``discarded_prefixes`` collects (start, end, delimiter)
    for consumed DELIMITER lines. The editor compares both original ranges
    and argument values before any prefix disappears from a planned unit.
    """
    if not isinstance(sql, str):
        raise ValueError("Pass SQL text.")
    # add_line scans physical lines independently. A caller-supplied
    # delimiter must not cross a line or depend on CRLF translation either.
    if not _usable_delimiter(delimiter):
        raise ValueError("Pass a delimiter of 1 to 15 UTF-8 bytes starting with ASCII, without backslashes, NUL, line endings, "
                         "or leading or trailing whitespace.")
    framing = lex_sql(sql, backslash_escapes, engine, server_version, dollar_quotes, client=True,
                      client_delimiter=delimiter, sql_mode=sql_mode, ansi_quotes=ansi_quotes,
                      client_engine=client_engine, comment_dialect=comment_dialect,
                      client_ansi_quotes=client_ansi_quotes, client_backslash_escapes=client_backslash_escapes,
                      _cache=_cache)
    framing_key = _scan_cache_key(framing, _cache) if _cache is not None else None
    scan = lex_sql(sql, backslash_escapes, engine, server_version, dollar_quotes,
                   unknown_execute=unknown_execute, wsrep_on=wsrep_on, framing=framing["tokens"],
                   ansi_quotes=ansi_quotes, mssql=mssql, sql_mode=sql_mode, comment_dialect=comment_dialect,
                   _cache=_cache, _framing_key=framing_key)
    cache_key = None
    scan_key = None
    if _cache is not None:
        scan_key = _scan_cache_key(scan, _cache)
        # Equal complete scans produce equal units even when the original
        # mode arguments differ. Prefix collection has an output argument,
        # so leave that splitter path uncached.
        if discarded_prefixes is None:
            cache_key = ("split", sql, delimiter, client_engine,
                         framing_key, scan_key)
            try:
                cached = _cache.get(cache_key)
            except TypeError:
                cache_key = None
            else:
                if cached is not None:
                    return cached
        # _masked_fragment adds an ends index. Give it private dictionaries;
        # tokens/comments/issues and the resulting idents are only read by
        # the splitter and classification consumers. Populate both indexes
        # even on a fragment-cache hit, which does not run the helper body.
        framing = dict(framing, ends=[token[2] for token in framing["tokens"]])
        scan = dict(scan, ends=[token[2] for token in scan["tokens"]])
        # Pass the originals' integer keys to fragment lookups. The private
        # copies and their ends indexes can die when this split finishes;
        # retaining their object ids would keep every index alive too.

    def interval_index(ranges):
        starts, ends = [], []
        farthest = -1
        for left, right in sorted(ranges):
            starts.append(left)
            farthest = max(farthest, right)
            ends.append(farthest)
        return starts, ends

    def overlaps(index, start, end):
        starts, ends = index
        at = bisect.bisect_left(starts, end) - 1
        return at >= 0 and ends[at] > start

    # Comments can contain many client statement endings. Index their
    # intervals once instead of scanning every comment for every ending.
    # Prefix maxima also retain enclosing comments when intervals overlap.
    executed_comments = interval_index((comment["start"], comment["end"])
                                      for comment in scan["comments"] if comment["executes"] is not False)
    unclosed_tokens = interval_index(scan["unclosed"] + framing["unclosed"])
    all_issues = scan["issues"] + framing["issues"]
    issue_order = sorted(range(len(all_issues)), key=lambda at: all_issues[at][0])
    issue_positions = [all_issues[at][0] for at in issue_order]

    def issues_between(start, end):
        left = bisect.bisect_left(issue_positions, start)
        right = bisect.bisect_left(issue_positions, end)
        # Preserve the original server-then-client issue order, including
        # issues the lexer discovers out of position order when closing a
        # comment. Sorting the whole list by position would change output.
        for at in sorted(issue_order[left:right]):
            yield all_issues[at]

    out = []
    consumed_delimiters = set()
    state = {"start": 0, "pending": False, "issues": [], "unclosed": False}
    pos, token_index = 0, 0
    n = len(sql)

    def issue(reason):
        if reason not in state["issues"]:
            state["issues"].append(reason)

    def finish(end, next_start):
        start = state["start"]
        code, idents, tokens = _masked_fragment(sql, scan, start, end, _cache=_cache, _scan_key=scan_key)
        # A skipped comment cannot mask SQL across separate COM_QUERYs.
        # In --force mode a later fragment could execute after the first
        # fragment's unclosed-comment error. Refuse this framing, including
        # for classifier callers outside the editor's packet builder.
        for boundary in (start, end):
            index = bisect.bisect_right(scan["ends"], boundary)
            if index < len(scan["tokens"]):
                kind, left, right = scan["tokens"][index]
                if kind == "comment" and left < boundary < right:
                    issue(REASON_COMMENT_FRAMING)
                if kind in ("string", "identifier") and left < boundary < right:
                    issue(REASON_QUOTE_FRAMING)
            if overlaps(executed_comments, boundary, boundary):
                issue(REASON_EXECUTED_FRAMING)
        for at, reason in issues_between(start, end):
            if not (at in consumed_delimiters and reason == REASON_NAMED_CLIENT_COMMAND % "DELIMITER"):
                issue(reason)
        unclosed = state["unclosed"] or overlaps(unclosed_tokens, start, end)
        if code or state["pending"] or state["issues"] or unclosed:
            raw = sql[start:end]
            lead = len(raw) - len(raw.lstrip())
            text = raw.strip()
            # MySQL's Windows batch reader changes even CRLF inside quoted
            # data. MariaDB's binary reader preserves it. Command lines and
            # whitespace between units are already outside this sent span;
            # refuse remaining changes instead of rewriting SQL literals.
            # mysql-8.4.0:client/readline.cc:75-95.
            if sys.platform == "win32" and client_engine != "mariadb" and "\r\n" in text:
                issue(REASON_CRLF_TRANSPORT)
            unused_code, unused_idents, client_tokens = _masked_fragment(
                sql, framing, start, end, _cache=_cache, _scan_key=framing_key)
            out.append({"text": text, "start": start + lead, "end": start + lead + len(text),
                        "code": code, "idents": idents, "token_spans": tokens,
                        "client_token_spans": client_tokens,
                        "unclosed": bool(unclosed), "issues": list(state["issues"]), "delimiter": delimiter,
                        "next_start": next_start})
        state.update(start=next_start, pending=False, issues=[], unclosed=False)

    tokens = framing["tokens"]
    while pos < n:
        while token_index < len(tokens) and tokens[token_index][2] <= pos:
            token_index += 1
        if token_index == len(tokens):
            break
        kind, left, right = tokens[token_index]
        if kind == "delimiter_command":
            command = _DELIMITER_COMMAND.match(sql, left)
            value, line_end = _delimiter_argument(sql, command.end(), client_engine)
            # A MySQL client treats /*M! SQL */ as an ordinary comment, but
            # a MariaDB server executes it. Only discard a prefix the server
            # also ignores. Keep unsafe text in a refused unit, including
            # when this DELIMITER is the last line of the run.
            # mysql-8.4.0:client/mysql.cc:2455-2471,2895-2902;
            # mariadb-11.8.9:sql/sql_lex.cc:2416-2467.
            prefix_code, unused_idents, unused_tokens = _masked_fragment(
                sql, scan, state["start"], left, _cache=_cache, _scan_key=scan_key)
            ignored = not prefix_code and not overlaps(executed_comments, state["start"], left)
            if ignored:
                consumed_delimiters.add(command.start(1))
                if discarded_prefixes is not None:
                    discarded_prefixes.append((state["start"], line_end, value))
            else:
                issue("SQL before DELIMITER may run on the server. End it with the current delimiter "
                      "before changing DELIMITER.")
            # Suppress only the exact command proved safe above. Other
            # commands and problems in the prefix must still refuse.
            for at, reason in issues_between(state["start"], line_end):
                if not (at in consumed_delimiters and reason == REASON_NAMED_CLIENT_COMMAND % "DELIMITER"):
                    issue(reason)
            if not ignored:
                finish(line_end, line_end)
            if value and "\\" not in value:
                delimiter = value
            else:
                state["unclosed"] = True
                issue(REASON_DELIMITER)
            pos = line_end
            state["start"] = pos
            continue
        if kind == "delimiter":
            finish(left, right)
            pos = right
            continue
        if kind != "code":
            if kind in ("identifier", "string"):
                state["pending"] = True
            pos = right
            continue
        ch = sql[pos]
        if ch == "\\":
            following = sql[pos + 1:pos + 2]
            if following in ("g", "G"):
                finish(pos, pos + 2)
                pos += 2
                continue
            # lex_sql reports unsupported commands in both complete passes.
        if ch not in _CLIENT_SPACE:
            state["pending"] = True
        pos += 1
    finish(n, n)
    if cache_key is not None:
        _cache[cache_key] = out
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


_OBJECT_WORDS = ("READYSET", "TABLE", "TABLES", "INDEX", "VIEW", "DATABASE", "SCHEMA", "USER", "ROLE", "PROCEDURE",
                 "FUNCTION", "TRIGGER", "EVENT", "SERVER", "TABLESPACE", "LOGFILE", "SEQUENCE", "INSTANCE",
                 "PACKAGE", "RESOURCE", "UNDO", "SPATIAL", "CACHE", "CACHES", "QUERIES")


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
CONVERT CONVERT_TZ COS COT COUNT CRC32 CURDATE CURRENT_DATE CURRENT_TIME
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
WEEKOFYEAR YEAR YEARWEEK CAST ROW ROW_COUNT ROW_NUMBER RANK DENSE_RANK LAG LEAD
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


def _native_read_functions(engine, server_version):
    """Version-specific readers stay unknown until server facts establish them.

    CURRENT_ROLE: mysql-8.0.11 sql/item_create.cc:1316 and mariadb-10.11.1
    sql/sql_yacc.yy:9850. JSON_VALUE: mysql-8.0.21:sql/sql_yacc.yy:1261,
    10005-10010 adds JSON_VALUE_SYM (absent in mysql-8.0.20); MariaDB's
    checked floor is mariadb-10.11.1:sql/item_create.cc:5787. Other floors
    are checked versions, not claims about first releases.
    """
    version = parse_version_tuple(server_version, leading=True,
                                  prefer_mariadb=engine == "mariadb", default=None)
    names = set(_READ_FUNCTIONS)
    if version is not None:
        if ((engine == "mysql" and version >= (8, 0, 11)) or
                (engine == "mariadb" and version >= (10, 11, 1))):
            names.add("CURRENT_ROLE")
        if ((engine == "mysql" and version >= (8, 0, 21)) or
                (engine == "mariadb" and version >= (10, 11, 1))):
            names.add("JSON_VALUE")
    return names


def _select_function_checks(upper, idents, native_functions=None):
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
            if name in (native_functions or _READ_FUNCTIONS) and (name not in _SPACE_SENSITIVE_FUNCTIONS or
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


def _select_checks(upper, idents, into=True, native_functions=None):
    """The SELECT expression checks; *into* False skips INTO (an explained
    INSERT INTO or SELECT ... INTO writes nothing)."""
    found = _select_function_checks(upper, idents, native_functions)
    if into and re.search(r"\bINTO\s+(?:OUTFILE|DUMPFILE)\b", upper):
        found.append(_verdict("write", "file", "INTO OUTFILE or DUMPFILE writes a file on the database server host "
                                               "(it needs the FILE privilege)."))
    elif into and re.search(r"\bINTO\b", upper):
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


# The FOR that ends a MariaDB SET STATEMENT assignment list. Values are
# expressions (sql_yacc.yy set_stmt_option), and the only FOR an expression
# can hold outside parentheses is the sequence form NEXT VALUE FOR s or
# PREVIOUS VALUE FOR s (sql_yacc.yy column_default_non_parenthesized_expr).
_SET_STATEMENT_FOR = re.compile(r"(?<![@$])\bFOR\b")
_SEQUENCE_VALUE_FOR = re.compile(r"\b(?:NEXT|PREVIOUS)\s+VALUE\s*$")


def _set_statement_for(flat):
    """The match of the FOR that starts the statement after SET STATEMENT's
    assignments in *flat* (code with parentheses blanked), or None."""
    for found in _SET_STATEMENT_FOR.finditer(flat):
        if not _SEQUENCE_VALUE_FOR.search(flat[:found.start()]):
            return found
    return None


def _set_checks(upper, idents):
    head = re.match(r"\s*SET\b", upper)
    if head is None:
        return _verdict("risky", "unknown", "myflames could not read this SET statement.")
    rest = upper[head.end():]
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
        found = _set_statement_for(_flat(upper))
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


def _classify_code(upper, idents, key_columns, native_functions=None):
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
        return _select_checks(upper, idents, native_functions=native_functions)
    if word == "WITH":
        flat = _flat(text)
        main = re.search(r"\b(SELECT|TABLE|VALUES|UPDATE|DELETE|INSERT|REPLACE)\b", flat[lead.end():])
        if not main:
            return _select_checks(upper, idents, native_functions=native_functions)
        verb = main.group(1)
        at = lead.end() + main.start()
        if verb in ("UPDATE", "DELETE"):
            return _update_delete(verb, text[at:], idents, key_columns)
        if verb in ("INSERT", "REPLACE"):
            return _verdict("write", "dml", "%s adds rows." % verb)
        return _select_checks(upper, idents, native_functions=native_functions)
    if word == "HANDLER":
        # Both engines retain OPEN's MDL ticket with MDL_EXPLICIT duration
        # (sql/sql_handler.cc). Match the operation after the table name,
        # not OPEN inside a quoted name, READ predicate or alias.
        # Quoted names need no following space; bare names must stay whole.
        name = r"(?:`\d+`|''|%s+(?!%s))" % (_IDENT_CHAR, _IDENT_CHAR)
        if re.match(r"\s*%s(?:\s*\.\s*%s)?\s*OPEN\b" % (name, name), after):
            return _verdict("risky", "lock", "HANDLER OPEN holds a metadata lock on the table until "
                                            "HANDLER CLOSE or the session ends")
        # HANDLER READ evaluates its WHERE expression on the server; apply
        # the SELECT expression checks (functions, :=, named locks) to it.
        return _select_checks(upper, idents, native_functions=native_functions)
    if word == "SHOW":
        # SHOW's WHERE and expression parts are evaluated by the server
        # (sql/sql_show.cc evaluates the condition like a SELECT WHERE), so
        # SHOW TABLES WHERE GET_LOCK('x', 0) would take a named lock. Apply
        # the SELECT expression checks instead of trusting the verb.
        return _select_checks(upper, idents, native_functions=native_functions)
    if word == "GET":
        # GET DIAGNOSTICS only assigns variables (session state).
        return _verdict("write", "session", "GET DIAGNOSTICS assigns condition information to variables.")
    if word in ("HELP", "USE", "CHECKSUM", "SIGNAL", "RESIGNAL", "DEALLOCATE"):
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
        if re.match(r"\s*\([\s(]*SET\b", rest):
            # Keep an unreadable SET behind EXPLAIN on the same refusal
            # path as a parenthesized SET at the statement head.
            return _set_checks(rest, idents)
        if analyzed and rest.strip():
            inner = _classify_code(rest, idents, key_columns, native_functions)
            inner["reasons"] = ["EXPLAIN ANALYZE runs the statement."] + inner["reasons"]
            return inner
        # Plain EXPLAIN does not run the statement, but planning evaluates
        # constant parts: a derived table without tables is materialized
        # while the plan is built (sql/sql_derived.cc; sql/sql_lex.cc
        # has_tables()), so GET_LOCK, :=, sequences and, before MySQL 8.0.42,
        # 8.4.5 and 9.3.0, stored functions in it take effect (Bug#37560280).
        if re.match(r"\s*(?:CREATE|ALTER|DROP|RENAME|TRUNCATE|GRANT|REVOKE)\b", rest):
            # EXPLAIN takes no DDL on either engine: the server refuses it
            # with a syntax error and runs nothing.
            return _verdict("read", "read")
        # INSERT/REPLACE's target column list is names, not a call: drop it
        # (a column list holds no parentheses) and check the value expressions.
        target = re.match(r"\s*(?:INSERT|REPLACE)\b(?:\s+(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\b)*"
                          r"\s*(?:INTO\b)?\s*(?:`\d+`|[A-Z0-9_$]+)(?:\s*\.\s*(?:`\d+`|[A-Z0-9_$]+))?"
                          r"\s*(?:PARTITION\s*\([^()]*\)\s*)?\(", rest)
        if target and ")" in rest[target.end():]:
            close = rest.index(")", target.end())
            if "(" not in rest[target.end():close]:
                rest = rest[:target.end() - 1] + " " + rest[close + 1:]
        verdict = _select_checks(rest, idents, into=False, native_functions=native_functions)
        if verdict["kind"] != "read":
            verdict["reasons"] = ["EXPLAIN evaluates constant expressions and derived tables while it plans "
                                  "the statement."] + verdict["reasons"]
        return verdict
    if word == "ANALYZE":
        if re.match(r"\s*(?:SELECT|UPDATE|DELETE|INSERT|REPLACE|WITH|FORMAT\b|\()", after):
            inner_text = re.sub(r"^\s*FORMAT\s*=\s*[A-Z_]+", "", after)
            inner = _classify_code(inner_text, idents, key_columns, native_functions)
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
        if word == "ALTER" and second == "READYSET":
            return _verdict("risky", "server", "ALTER READYSET changes the Readyset server's replication, tables, "
                                               "memory or mode.")
        if word == "CREATE" and re.match(r"\s*(?:(?:DEEP|SHALLOW)\s+)?CACHE\b", after):
            return _verdict("risky", "server", "CREATE CACHE adds or replaces a Readyset query cache.")
        if word == "DROP" and re.match(
                r"\s*(?:CACHE\b|ALL\s+(?:(?:DEEP|SHALLOW)\s+)?CACHES\b|ALL\s+PROXIED\s+QUERIES\b)", after):
            return _verdict("risky", "server", "DROP CACHE removes Readyset query caches or recorded proxied queries.")
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
        if re.match(r"\s*(?:CACHE\b|ALL\s+SHALLOW\s+CACHES\b)", after):
            return _verdict("risky", "server", "FLUSH CACHE discards cached Readyset entries so later reads refresh "
                                               "from upstream.")
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


def _classify_statement(stmt, key_columns, native_functions=None):
    """Verdict for one client statement. Under a DELIMITER other than ';' the
    text can hold several server statements (the client connects with
    CLIENT_MULTI_STATEMENTS), so each ';'-separated part is judged."""
    if stmt["unclosed"] or stmt.get("issues"):
        reasons = list(stmt.get("issues") or [])
        if stmt["unclosed"] and REASON_DELIMITER not in reasons:
            reasons.append("An unclosed quote, comment or DELIMITER line makes the statement boundaries uncertain.")
        category = "server" if not stmt["unclosed"] and reasons == [REASON_READYSET_HINT] else "unknown"
        return {"kind": "risky", "category": category, "reasons": reasons}
    parts = [p for p in stmt["code"].upper().split(";") if p.strip()]
    verdicts = [_classify_code(p, stmt["idents"], key_columns, native_functions) for p in parts]
    verdict = _worst(verdicts)
    if len(parts) > 1:
        verdict["reasons"].append(REASON_MULTI)
    return verdict


def _classify_reading(sql, backslash_escapes, key_columns, engine=None, server_version=None,
                      unknown_execute=True, wsrep_on=None, delimiter=";", _cache=None, **quote_args):
    units = split_statements(sql, backslash_escapes, delimiter=delimiter, engine=engine,
                             server_version=server_version, unknown_execute=unknown_execute,
                             wsrep_on=wsrep_on, _cache=_cache, **quote_args)
    statements = []
    native_functions = _native_read_functions(engine, server_version)
    for stmt in units:
        verdict = _classify_statement(stmt, key_columns, native_functions)
        statements.append({"text": stmt["text"], "start": stmt["start"], "end": stmt["end"],
                           "verb": _verb(stmt["code"].upper()), "kind": verdict["kind"],
                           "category": verdict["category"], "reasons": verdict["reasons"]})
    return units, statements


def classify(sql, backslash_escapes=None, key_columns=None, engine=None, server_version=None, wsrep_on=None,
             sql_mode=None, client_engine=None, client_status_synced=False, delimiter=";", _use_cache=True):
    """Classify every modelled server reading and keep the worst effect.

    Unknown engines or MySQL versions use all version-number grammars. Unknown quote modes
    compare complete readings, not a union of independently masked tokens.
    Unknown version conditions still fail closed when mixed conditions could
    expose code that neither the all-execute nor all-skip reading exposes.
    """
    if not isinstance(sql, str):
        raise ValueError("Pass SQL text.")
    if len(sql) > 1000000:
        raise ValueError("SQL text is limited to 1,000,000 characters.")
    # Never retain SQL (which can contain credentials) between requests or
    # share mutable scanner results with concurrent classifier calls. Tests
    # can disable reuse while exercising the same complete reading logic.
    cache = {} if _use_cache else None
    readings = lexer_readings(backslash_escapes, engine, sql_mode, server_version, client_engine, sql,
                              client_status_synced=client_status_synced)
    units, statements = _classify_reading(sql, key_columns=key_columns, engine=engine,
                                         server_version=server_version, wsrep_on=wsrep_on,
                                         delimiter=delimiter, _cache=cache, **readings[0])
    reference = _mode_reading(units, _cache=cache)
    skipped_reference = None
    summary = _worst(statements)
    ambiguous = False

    def merge(alternative):
        # Preserve original source text while merging overlapping verdicts.
        first = 0
        for statement in statements:
            while first < len(alternative) and alternative[first]["end"] <= statement["start"]:
                first += 1
            end = first
            while end < len(alternative) and alternative[end]["start"] < statement["end"]:
                end += 1
            if end > first:
                statement.update(_worst([statement] + alternative[first:end]))

    for reading in readings[1:]:
        units, alternative = _classify_reading(sql, key_columns=key_columns, engine=engine,
                                              server_version=server_version, wsrep_on=wsrep_on,
                                              delimiter=delimiter, _cache=cache, **reading)
        meaning = _mode_reading(units, _cache=cache)
        if meaning != reference:
            # A future gate cannot execute with the legacy five-digit
            # grammar. If that yields exactly the existing all-skip reading,
            # it adds no lexical ambiguity: conditional execute/skip effects
            # already participate in the worst verdict below. A sixth digit
            # exposed as SQL, different quotes or client framing still does.
            if reading["comment_dialect"] == "mysql_legacy":
                if skipped_reference is None:
                    skipped, unused = _classify_reading(
                        sql, key_columns=key_columns, engine=engine, server_version=server_version,
                        unknown_execute=False, wsrep_on=wsrep_on, delimiter=delimiter, _cache=cache, **readings[0])
                    skipped_reference = _mode_reading(skipped, _cache=cache)
                ambiguous = ambiguous or meaning != skipped_reference
            else:
                ambiguous = True
        summary = _worst([summary, _worst(alternative)])
        merge(alternative)
    if ambiguous:
        summary["reasons"].append(AMBIGUOUS_REASON)
    for reading in readings if "/*" in sql else ():
        scan = lex_sql(sql, engine=engine, server_version=server_version, wsrep_on=wsrep_on, _cache=cache, **reading)
        uncertain = [c for c in scan["comments"] if c["problem"]]
        if uncertain:
            unused_units, alternative = _classify_reading(
                sql, key_columns=key_columns, engine=engine, server_version=server_version,
                unknown_execute=False, wsrep_on=wsrep_on, delimiter=delimiter, _cache=cache, **reading)
            summary = _worst([summary, _worst(alternative)])
            merge(alternative)
            if len({c["opener"] for c in uncertain}) > 1 or any(
                    c.get("problem") in ("reversed executable comment", "unterminated executable comment",
                                         "MariaDB wsrep executable comment /*!99997") for c in uncertain):
                ambiguous = True
                unknown = _verdict("risky", "unknown",
                                   "Executable comments have uncertain server conditions, so myflames cannot "
                                   "confirm which SQL executes.")
                summary = _worst([summary, unknown])
                for statement in statements:
                    statement.update(_worst([statement, unknown]))
    for reading in readings if "$" in sql else ():
        plain = split_statements(sql, delimiter=delimiter, engine=engine, server_version=server_version,
                                 wsrep_on=wsrep_on, _cache=cache, **reading)
        dollars = split_statements(sql, delimiter=delimiter, dollar_quotes=True, engine=engine,
                                   server_version=server_version, wsrep_on=wsrep_on, _cache=cache, **reading)
        if _target_reading(plain) != _target_reading(dollars):
            ambiguous = True
            summary = _verdict("risky", "unknown", "Dollar quotes change the SQL interpretation between clients.")
            statements = [dict(s, kind="risky", category="unknown",
                               reasons=list(s["reasons"]) + summary["reasons"]) for s in statements]
            break
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

# The same UUID grammar used for reviewed Cluster and replication actions.
_TARGET_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TARGET_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,255}")
_TARGET_ERROR = re.compile(r"^(?:(?:mysql client|mysqlsh) exited \d+:\s*)?ERROR\s+(\d+)"
                           r"(?:\s+\([^\r\n)]*\))?\s+at line (\d+):", re.M)
TARGET_IDENTITY_MISMATCH = ("The server answering this connection is not the one you reviewed. Nothing was sent. "
                            "Refresh and review again.")
# Both clients keep their delimiter in a 16-byte buffer. None of these
# candidates contains whitespace, quoting or a client backslash command.
_TARGET_DELIMITERS = ("$$", "//", "__mf_end_1__", "__mf_end_2__")
_TARGET_MARKER = re.compile(r"^myflames_guard_ok_[0-9a-f]{32}_[0-9]+$", re.M)


def target_guard_markers(text):
    """Private acknowledgements only, never SQL result rows or credentials."""
    return tuple(_TARGET_MARKER.findall(text or ""))


def target_script_markers(sql):
    """Only acknowledgements inserted immediately after a packet's guard."""
    return tuple(re.findall(r"^SET @myflames_target = [^\r\n]*?; SELECT '(myflames_guard_ok_[0-9a-f]{32}_[0-9]+)'; ",
                            sql or "", re.M))


def strip_target_guard_markers(text, markers=()):
    # Filter this invocation's nonce, preserving ordinary result rows that
    # happen to resemble the reserved prefix, including unguarded queries.
    private = set(markers)
    return "\n".join(row for row in text.split("\n") if row not in private)


def _target_error_match(text):
    # MariaDB can echo the entire query before its diagnostic. A newline in
    # a quoted value may itself look like ERROR ... at line ...; only search
    # after the client's closing echo separator in that case.
    boundary = text.rfind("\n--------------\n")
    return _TARGET_ERROR.search(text, boundary + len("\n--------------\n") if boundary >= 0 else 0)


def target_error_text(exc):
    """The diagnostic without echoed SQL or private acknowledgement names."""
    text = str(exc)
    match = _target_error_match(text)
    if match:
        text = text[match.start():]
    text = re.sub(r"myflames_guard_ok_[0-9a-f]{32}_[0-9]+", "[identity acknowledgement]", text)
    return re.sub(r"myflames_evidence_[0-9a-f]+", "[identity evidence]", text)


def _evidence_error_text(exc, password=None):
    """Keep one real diagnostic line, without the client's SQL echo."""
    match = re.search(r"^(?:(?:mysql client|mysqlsh) exited \d+:\s*)?(ERROR\s+\d+\b[^\r\n]*)",
                      target_error_text(exc), re.M)
    if match is None:
        return ""
    detail = re.split(r"\bnear\s+['\"`]", match.group(1), maxsplit=1, flags=re.I)[0].rstrip()
    return redact(detail, password if isinstance(password, str) else None)


def target_has_galera(variables, engine=None):
    """Provider existence is independent of this session's wsrep_on.

    MariaDB sql/wsrep_var.cc:107-108 checks a nonempty provider != 'none'.
    Keep wsrep_on as additional evidence for older/incomplete snapshots.
    """
    variables = variables or {}
    provider = str(variables.get("wsrep_provider") or "").lower()
    return (engine == "pxc" or provider not in ("", "none") or
            str(variables.get("wsrep_on", "")).upper() in ("ON", "1"))


_EVIDENCE_VARIABLES = ("server_uuid", "server_id", "hostname", "port", "version", "version_comment",
                       "wsrep_on", "wsrep_provider", "wsrep_node_name")


class _TargetIdentityError(ValueError):
    """An app-built identity explanation safe to keep in a scope refusal."""


class TargetEvidenceConnection(object):
    """Keep every successful review read on one node, even through a router.

    Each identity prefix and its read are ONE COM_QUERY, using a client
    delimiter absent from the entire payload. All reads in this scope must
    agree. No result from another node is ever returned to the caller.
    Identity failures are sticky, including when an optional reader catches
    one. A server error after the validated prefix preserves normal fallbacks.
    This binds the node, not a transactional snapshot of changing server data.
    """
    def __init__(self, conn):
        self.connection = conn
        self.variables = None
        self.problem = None
        self._consent_error = None

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def server_version(self):
        """Use this scope's packet metadata, never the transport's cache."""
        self.check()
        if self.variables is None:
            self.run("SELECT @@version", extra_flags=["--comments"])
        return self.variables.get("version", "")

    def server_version_comment(self):
        self.server_version()
        return self.variables.get("version_comment", "")

    def is_mariadb(self):
        from .server_info import detect_flavor
        return detect_flavor(self.server_version(), self.server_version_comment()) == "mariadb"

    def check(self):
        from .connector import ConnectorError
        if self._consent_error is not None:
            raise self._consent_error
        if self.problem:
            error = ConnectorError(self.problem)
            error.identity_outcome = "identity_unavailable"
            raise error

    def run_write(self, *args, **kwargs):
        self.check()
        return self.connection.run_write(*args, **kwargs)

    def run_stdin(self, *args, **kwargs):
        self.check()
        return self.connection.run_stdin(*args, **kwargs)

    def run(self, sql, extra_flags=None, **kwargs):
        from .connector import ConnectorError, PartialReadError, unescape_mysql
        from .perf_common import named_rows
        self.check()
        failure = None
        try:
            marker = "myflames_evidence_" + uuid.uuid4().hex
            units = split_statements(sql)
            # Boundary checks and transport are part of validation too. A
            # failed prefix must poison the scope even if an optional reader
            # catches the error and tries to continue with earlier evidence.
            bodies = []
            for unit in units:
                start, end = _target_statement_span(unit["text"])
                bodies.append(unit["text"][start:end])
            if not units or any(unit["delimiter"] != ";" or unit["issues"] or unit["unclosed"] for unit in units):
                raise ValueError("uncertain review evidence SQL boundaries")
            leading = re.match(r"\s*(/\*myflames:[^\r\n]*?\*/)", units[0]["text"])
            tag = (leading.group(1) if leading else "/*myflames:target-evidence*/") + " "
            body = tag + "SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s);\n" % ", ".join("'%s'" % key for key in _EVIDENCE_VARIABLES)
            body += tag + "SHOW GLOBAL STATUS WHERE Variable_name = 'wsrep_gcomm_uuid';\n"
            body += tag + "SELECT '%s';\n" % marker
            body += "\n;\n".join(bodies) + "\n"
            delimiter = next((value for value in _TARGET_DELIMITERS if value not in body), None)
            if delimiter is None:
                # Account names and other server data may contain every fixed
                # candidate. Keep framing independent of that data, checking
                # random candidates against the complete packet as well.
                # Both clients store at most 15 delimiter bytes: mysql-8.4.7
                # client/mysql.cc:226,4415 and mariadb-11.4.9:281,4597.
                import secrets
                while delimiter is None or delimiter in body:
                    delimiter = "__mf_" + secrets.token_hex(4) + "__"
            flags = list(extra_flags or [])
            if "--skip-reconnect" not in flags:
                flags.append("--skip-reconnect")
            if "--comments" not in flags:
                flags.append("--comments")
            # The connector retains stdout separately from error messages.
            # A valid prefix alone is insufficient: only a server diagnostic
            # at this packet's line proves the read, rather than transport,
            # failed. Transport errors cannot authorize an optional fallback.
            try:
                kwargs["retain_partial_stdout"] = True
                text = self.connection.run("DELIMITER %s\n%s%s\n" % (delimiter, body, delimiter),
                                           extra_flags=flags, **kwargs)
            except PartialReadError as exc:
                match = _target_error_match(str(exc))
                if not match or int(match.group(2)) != 2 or 2000 <= int(match.group(1)) <= 2999:
                    raise
                text, failure = exc.stdout, exc
            if "--xml" in flags:
                # mysql prints one XML document per result set. Preserve the
                # original result document exactly for Reader.named().
                documents = list(re.finditer(r"(?:<\?xml[^>]*>\s*)?<resultset\b[^>]*?"
                                             r"(?:/>|>[\s\S]*?</resultset>)", text))
                if len(documents) < 3:
                    raise ValueError("missing evidence result sets")
                evidence = documents[:3]
                marker_rows = named_rows(evidence[2].group())
                if len(marker_rows) != 1 or list(marker_rows[0].values()) != [marker]:
                    raise ValueError("missing evidence marker")
                values = {}
                for document in evidence[:2]:
                    for row in named_rows(document.group()):
                        row = {key.lower(): value for key, value in row.items()}
                        key = row["variable_name"].lower()
                        if key in values:
                            raise ValueError("duplicate server identity field")
                        values[key] = row["value"]
                output = text[evidence[2].end():].lstrip("\r\n")
            else:
                prefix, separator, output = text.partition(marker + "\n")
                if not separator or (prefix and not prefix.endswith("\n")):
                    raise ValueError("missing evidence marker")
                values = {}
                for line in prefix.splitlines():
                    key, value = line.split("\t")
                    if key.lower() in values:
                        raise ValueError("duplicate server identity field")
                    values[key.lower()] = unescape_mysql(value)
            if any(key not in _EVIDENCE_VARIABLES + ("wsrep_gcomm_uuid",) for key in values):
                raise ValueError("unexpected server identity field")
            required = ("server_uuid",) if "server_uuid" in values else ("server_id", "hostname", "port", "version")
            if not all(values.get(key) for key in required):
                raise ValueError("missing server identity")
            engine = "mysql" if "server_uuid" in values else "mariadb"
            identity = {key: values[key] for key in required}
            target_session_guard(engine, target_galera_identity(None, engine, identity, values))
            if self.variables is not None and self.variables != values:
                raise ValueError("the server identity changed between evidence packets")
            self.variables = values
        except BaseException as exc:
            from .connection_groups import ProfileReadError
            if isinstance(exc, ProfileReadError):
                # Optional readers may catch ValueError. Preserve this exact
                # refusal for their caller's check(), including its explanation.
                self._consent_error = exc
                self.problem = str(exc)
                raise
            cause = failure if failure is not None else exc
            detail = (_evidence_error_text(cause, getattr(self.connection, "password", None))
                      if isinstance(cause, ConnectorError) else
                      str(cause) if isinstance(cause, _TargetIdentityError) else "")
            self.problem = ("The identity check could not run: " + (detail.rstrip(".") + ". " if detail else "") +
                            "Review evidence did not come from one identified server. Nothing was sent. Refresh and review again.")
            # Cancellation must keep its normal control flow, but a caller
            # that catches it still cannot reuse this incomplete scope.
            if not isinstance(exc, Exception):
                raise
            self.check()
        if failure is not None:
            # The caller may legitimately handle 1141 (an absent account),
            # 1193 (an optional variable), or other read errors. Do not leak
            # the private prefix through the ordinary exception interface.
            raise ConnectorError(_evidence_error_text(failure, getattr(self.connection, "password", None)) or
                                 "The review read failed.") from None
        return output


def target_evidence(function):
    """Bind a complete review/recheck, including nested evidence readers."""
    @functools.wraps(function)
    def bound(conn, *args, **kwargs):
        from .connector import ConnectorError
        from .connection_groups import ProfileReadError
        evidence = conn if isinstance(conn, TargetEvidenceConnection) else TargetEvidenceConnection(conn)
        try:
            result = function(evidence, *args, **kwargs)
        except ProfileReadError:
            raise
        except (ConnectorError, ValueError):
            # Preserve the identity refusal if an inner reader replaced its
            # diagnostic with a generic grants/metadata error.
            evidence.check()
            raise
        evidence.check()
        return result
    return bound


def target_galera_identity(conn, engine, identity, variables, tag=""):
    """Use only node evidence captured in the same packet as the read state."""
    out = dict(identity or {})
    if isinstance(conn, TargetEvidenceConnection):
        conn.check()
        if conn.variables is None:
            conn.problem = "The identity check could not run: no server evidence was read. Nothing was sent. Refresh and review again."
            conn.check()
    evidence = conn.variables if isinstance(conn, TargetEvidenceConnection) else variables
    evidence = evidence or {}
    if isinstance(conn, TargetEvidenceConnection) and conn.variables:
        keys = ("server_id", "hostname", "port") if engine == "mariadb" else ("server_uuid",)
        if all(out.get(key) for key in keys) and any(str(out[key]) != str(evidence.get(key, "")) for key in keys):
            conn.problem = "The identity check could not run: the reviewed state and server identity disagree. Nothing was sent. Refresh and review again."
            conn.check()
    if not target_has_galera(evidence, engine):
        return out
    out.update(wsrep_on=evidence.get("wsrep_on", "OFF"),
               wsrep_gcomm_uuid=evidence.get("wsrep_gcomm_uuid"))
    if evidence.get("wsrep_provider"):
        out["wsrep_provider"] = evidence["wsrep_provider"]
    return out


def target_session_guard(engine, identity):
    """One SQL line bound only to a previously reviewed server identity.

    A scalar subquery returns NULL on a match and two rows on a mismatch
    (ERROR 1242). Use target_guarded_script to send it in the SAME query
    packet as each write, without --force or reconnect. Missing or unsafe
    identity values fail closed.
    Like READ_ONLY_SESSION_GUARD, the returned list is review/audit metadata.
    """
    from .collectors import _sql_string
    identity = identity if isinstance(identity, dict) else {}
    if engine in ("mysql", "percona", "percona_server", "pxc"):
        uuid = identity.get("server_uuid")
        if not isinstance(uuid, str) or not _TARGET_UUID.fullmatch(uuid):
            raise ValueError("The reviewed server UUID is missing or invalid. Nothing was sent. Refresh and review again.")
        different = "@@global.server_uuid <> %s" % _sql_string(uuid)
    elif engine == "mariadb":
        values = {}
        for key, maximum in (("server_id", 4294967295), ("port", 65535)):
            value = identity.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, str)) or \
                    len(str(value)) > 10 or not re.fullmatch(r"[0-9]+", str(value)) or not 0 <= int(value) <= maximum:
                raise ValueError("The reviewed server %s is missing or invalid. Nothing was sent. Refresh and review again."
                                 % key)
            values[key] = int(value)
        names = ["hostname"] + (["wsrep_node_name"] if "wsrep_node_name" in identity else [])
        for key in names:
            value = identity.get(key)
            if not isinstance(value, str) or not _TARGET_NAME.fullmatch(value):
                if key == "hostname" and isinstance(value, str) and value:
                    raise _TargetIdentityError(
                        "The server host name contains characters the identity check cannot compare safely. "
                        "Set a plain host name or use a MySQL server with a UUID. Nothing was sent.")
                raise ValueError("The reviewed server %s is missing or invalid. Nothing was sent. Refresh and review again."
                                 % key)
            values[key] = value
        different = " OR ".join("@@global.%s <> %s" % (key, str(values[key]) if key in ("server_id", "port")
                                                      else _sql_string(values[key]))
                                for key in ["server_id", "hostname", "port"] + names[1:])
    else:
        raise ValueError("This server has no supported reviewed identity guard. Nothing was sent.")
    if "wsrep_gcomm_uuid" in identity or target_has_galera(identity, engine):
        uuid = identity.get("wsrep_gcomm_uuid")
        if not isinstance(uuid, str) or not _TARGET_UUID.fullmatch(uuid):
            raise ValueError("The identity check could not run: the reviewed Galera node UUID is missing or invalid. Nothing was sent. "
                             "Refresh and review again.")
        # MariaDB 10.11/11.4/11.8 sql/sql_show.cc: GLOBAL_STATUS uses
        # variables_fields_info (VARIABLE_NAME, VARIABLE_VALUE). MySQL/PXC
        # exposes the same columns in performance_schema.global_status
        # (mysql-server/storage/perfschema/table_global_status.cc:65-66).
        table = "information_schema.GLOBAL_STATUS" if engine == "mariadb" else "performance_schema.global_status"
        different += (" OR NOT ((SELECT VARIABLE_VALUE FROM %s WHERE VARIABLE_NAME = 'wsrep_gcomm_uuid') <=> %s)"
                      % (table, _sql_string(uuid)))
    return ["SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g WHERE %s);" % different]


class TargetPacketLines(tuple):
    """Packet locations plus private acknowledgements and reviewed indices.

    Tuple compatibility keeps the existing SQL and Shell error consumers
    intact. A tuple of statements explicitly groups trusted, session-dependent
    units into one packet; a plain string must always be exactly one unit.
    """
    def __new__(cls, lines, markers, indices, sizes, sync_before=()):
        out = tuple.__new__(cls, lines)
        out.markers, out.indices, out.sizes = tuple(markers), tuple(indices), tuple(sizes)
        out.sync_before = tuple(sync_before)
        return out


def target_statement(sql, backslash_escapes=None, delimiter=";", engine=None, server_version=None,
                     wsrep_on=None, sql_mode=None, client_engine=None):
    """Validate one complete unit without changing its review/audit text."""
    _target_statement_span(sql, backslash_escapes, delimiter, engine, server_version, wsrep_on, sql_mode, client_engine)
    return sql


def _target_token_spans(sql, backslash_escapes=True, base=0, engine=None, server_version=None, wsrep_on=None,
                        **quote_args):
    """The shared lexer's quote, comment and code spans, including executed SQL."""
    return tuple((kind, base + start, base + end) for kind, start, end in
                 lex_sql(sql, backslash_escapes, engine, server_version, wsrep_on=wsrep_on, **quote_args)["tokens"])


def _target_reading(units):
    """All semantic and framing evidence used to compare client grammars."""
    return [(u["start"], u["end"], u["delimiter"], u["code"], u["idents"], u["unclosed"], u["issues"],
             u.get("token_spans", ()), u.get("client_token_spans", ()), u.get("next_start")) for u in units]


def _mode_reading(units, _cache=None):
    """Compare boundaries, token roles, restored identifiers and effects.

    A double-quoted identifier is not interchangeable with a string, even
    when both classify as a read or DDL. The editor needs the actual names
    for ALTER preflight, timeout guards and typed DROP confirmation.
    """
    cache_key = ("mode", id(units))
    if _cache is not None:
        cached = _cache.get(cache_key)
        if cached is not None:
            return cached[1]
    out = []
    for unit in units:
        verdict = _classify_statement(unit, None)
        code = re.sub(r"`[0-9]+`", lambda match: "`" + _name(match.group(), unit["idents"]).replace("`", "``") + "`",
                      unit["code"])
        out.append((unit["start"], unit["end"], unit["delimiter"],
                    code, unit.get("token_spans", ()),
                    unit["unclosed"], unit["issues"], verdict["kind"], verdict["category"]))
    if _cache is not None:
        # Comparison tuples are read-only. Retain the units with the result
        # so their id remains unique for the lifetime of this call's cache.
        _cache[cache_key] = (units, out)
    return out


def _target_statement_span(sql, backslash_escapes=None, delimiter=";", engine=None, server_version=None,
                           wsrep_on=None, sql_mode=None, client_engine=None, client_status_synced=False):
    """Require one complete unit with identical full readings when mode is unknown."""
    readings = lexer_readings(backslash_escapes, engine, sql_mode, server_version, client_engine, sql,
                              client_status_synced=client_status_synced)
    spans, meanings = [], []
    for reading in readings:
        units = split_statements(sql, delimiter=delimiter, engine=engine, server_version=server_version,
                                 wsrep_on=wsrep_on, **reading)
        dollar_units = split_statements(sql, delimiter=delimiter, dollar_quotes=True, engine=engine,
                                        server_version=server_version, wsrep_on=wsrep_on, **reading)
        if (len(units) != 1 or units[0]["unclosed"] or units[0]["issues"] or units[0]["delimiter"] != delimiter or
                len(dollar_units) != 1 or dollar_units[0]["unclosed"] or dollar_units[0]["issues"]):
            raise ValueError("Each reviewed statement must be one complete SQL statement with closed quotes and comments. "
                             "Nothing was sent. Review again.")
        if _target_reading(units) != _target_reading(dollar_units):
            raise ValueError("The reviewed SQL has ambiguous dollar-quote boundaries or code. Nothing was sent. Review again.")
        unit = units[0]
        # DELIMITER commands consumed by the splitter cannot hide before or
        # after the unit. Only delimiters, whitespace and comments may trail.
        tail_scan = lex_sql(sql, engine=engine, server_version=server_version, wsrep_on=wsrep_on, **reading)
        tail, unused_idents, unused_spans = _masked_fragment(sql, tail_scan, unit["end"], len(sql))
        if sql[:unit["start"]].strip() or not re.fullmatch(r"(?:[ \t\r\n]|%s)*" % re.escape(delimiter), tail):
            raise ValueError("The reviewed SQL contains a client command or uncertain boundaries. "
                             "Nothing was sent. Review again.")
        spans.append((unit["start"], unit["end"]))
        meanings.append(_mode_reading(units))
    if any(meaning != meanings[0] for meaning in meanings[1:]):
        raise ValueError("The reviewed SQL has ambiguous quote boundaries or code. Nothing was sent. Review again.")
    return spans[0]


def target_guarded_script(guard, statements, backslash_escapes=None, delimiter=";", engine=None,
                          server_version=None, wsrep_on=None, sql_mode=None, client_engine=None,
                          client_status_synced=False, defer_status_sync=False):
    """Return (client input, packet start lines) for reviewed SQL.

    DELIMITER is a client command, even in --binary-mode. Each delimited
    buffer becomes one COM_QUERY containing the identity check followed by
    its statement. The server stops that packet at its first error; batch
    mode stops before sending the next packet. Strings are single complete
    statements; internal tuples keep session setup and use in the same packet.
    A status-sensitive unit needs a known mode and a separate helper packet
    first. The editor defers that packet so it can await and check its reply
    through its ordinary helper path before writing the user unit. A caller
    may declare status synchronized only after an explicit mode-setting reply
    (the generated account password staging packet is such a caller).
    """
    statements = list(statements)
    if len(guard) != 1 or not isinstance(guard[0], str) or not guard[0].endswith(";") or not statements:
        raise ValueError("The guarded SQL script is invalid. Nothing was sent. Review again.")
    target_statement(guard[0])
    groups = [s if isinstance(s, tuple) else (s,) for s in statements]
    if any(not group or any(not isinstance(s, str) or not s.strip() for s in group) for group in groups):
        raise ValueError("The guarded SQL script is invalid. Nothing was sent. Review again.")
    wire_groups, sync_before = [], []
    for group in groups:
        wire_group = []
        sync = not client_status_synced and any(client_status_sensitive(s, delimiter, client_engine) for s in group)
        if sync and not isinstance(sql_mode, str):
            raise ValueError(CLIENT_STATUS_SYNC_REQUIRED + " Nothing was sent.")
        for statement in group:
            start, end = _target_statement_span(statement, backslash_escapes, delimiter, engine, server_version,
                                               wsrep_on, sql_mode, client_engine,
                                               client_status_synced=sync or client_status_synced)
            # Group separators belong to the builder. Copy only the parsed
            # unit, never its terminal delimiter(s) or trailing comments:
            # "SET ...;" joined with ";" is an empty server statement.
            wire_group.append(statement[start:end])
        wire_groups.append(tuple(wire_group))
        sync_before.append(sync)
    delimiter = next((d for d in _TARGET_DELIMITERS
                      if all(d not in s for s in list(guard) + [s for group in groups for s in group])), None)
    if delimiter is None:
        raise ValueError("No safe SQL packet delimiter is available for these statements. Nothing was sent. "
                         "Review statements without all of the reserved delimiters.")
    parts, starts, markers, indices, sizes, line, index = ["DELIMITER " + delimiter + "\n"], [], [], [], [], 2, 0
    nonce = uuid.uuid4().hex
    for k, group in enumerate(wire_groups):
        group_guard = guard[0]
        if sync_before[k] and not defer_status_sync:
            # The next packet can be routed independently. Assert its mode
            # too, not just the mode of the helper's backend session.
            flags = sql_mode_flags(sql_mode, engine, server_version)
            expected = [("NO_BACKSLASH_ESCAPES", not flags["backslash_escapes"])]
            if flags is not None:
                expected.append(("ANSI_QUOTES", flags["ansi_quotes"]))
                if engine == "mariadb":
                    expected.append(("MSSQL", flags["mssql"]))
            if not group_guard.endswith(");"):
                raise ValueError("The quote-status helper needs a bound session guard. Nothing was sent.")
            for name, enabled in expected:
                group_guard = (group_guard[:-2] +
                               " OR NOT ((FIND_IN_SET('%s', @@SESSION.sql_mode) > 0) <=> %d));" %
                               (name, int(enabled)))
        if sync_before[k] and not defer_status_sync:
            # End this COM_QUERY before the client reads any affected bytes.
            # Its OK reply, not merely executing SET inside the user packet,
            # updates the client's add_line status bits.
            starts.append(line)
            markers.append("myflames_guard_ok_%s_%d" % (nonce, len(markers)))
            indices.append(index)
            sizes.append(0)
            helper = (group_guard + " SELECT '" + markers[-1] + "'; " + CLIENT_STATUS_SYNC +
                      "\n" + delimiter + "\n")
            parts.append(helper)
            line += helper.count("\n")
        starts.append(line)
        markers.append("myflames_guard_ok_%s_%d" % (nonce, len(markers)))
        indices.append(index)
        sizes.append(len(group))
        index += len(group)
        # A newline before separators also closes trailing line comments.
        statement = "\n;\n".join(group) if len(group) > 1 else group[0]
        # A fresh line keeps a trailing -- or # comment from swallowing the
        # delimiter and merging this packet with the next one.
        packet = group_guard + " SELECT '" + markers[-1] + "'; " + statement + "\n" + delimiter + "\n"
        parts.append(packet)
        line += packet.count("\n")
    return "".join(parts), TargetPacketLines(starts, markers, indices, sizes, sync_before)


def target_guard_failed(exc, packet_lines=None):
    """Refusal details for a guard error, or None without definite evidence.

    With packet_lines, a server error before that packet's acknowledgement
    proves its protected statements never ran. A 1242 after acknowledgement
    belongs to the statement, even though its line names the whole packet.
    Without
    a packet map, preserve the first-line contract used by MySQL Shell.
    Transport failures never prove a guard refusal.
    """
    match = _target_error_match(str(exc))
    if packet_lines is not None:
        if not match or 2000 <= int(match.group(1)) <= 2999 or int(match.group(2)) not in packet_lines:
            return None
        packet = packet_lines.index(int(match.group(2)))
        markers = getattr(packet_lines, "markers", ())
        if markers and markers[packet] in getattr(exc, "guard_markers", ()):
            return None
        # A legacy tuple has no acknowledgement metadata; retain its old
        # 1242-only contract for existing first-line/Shell integrations.
        if not markers and int(match.group(1)) != 1242:
            return None
        index = getattr(packet_lines, "indices", range(len(packet_lines)))[packet]
        if int(match.group(1)) != 1242:
            reason = redact(target_error_text(str(exc)[match.end():]).strip()).rstrip(".")
            return {"outcome": "identity_unavailable", "statement_index": index,
                    "message": "The identity check could not run: %s. %s Refresh and review again." %
                    (reason, "Nothing was sent." if not index else "Earlier statements ran; this statement and the remaining statements were not sent.")}
        message = TARGET_IDENTITY_MISMATCH if index == 0 else (
            "The server answering this connection is not the one you reviewed. %s ran; "
            "this statement and the remaining statements were not sent. Refresh and review again."
            % ("The first statement" if index == 1 else "The first %d statements" % index))
        return {"outcome": "identity_mismatch", "message": message, "statement_index": index}
    if not match or int(match.group(2)) != 1 or 2000 <= int(match.group(1)) <= 2999:
        return None
    if int(match.group(1)) == 1242:
        return {"outcome": "identity_mismatch", "message": TARGET_IDENTITY_MISMATCH}
    # Discard any MariaDB SQL echo before the actual ERROR line. Callers
    # additionally redact their connection password before display/audit.
    message = redact(target_error_text(str(exc)[match.end():]).strip())
    return {"outcome": "identity_unavailable",
            "message": "The identity check could not run: %s. Nothing was sent. Refresh and review again."
                       % message.rstrip(".")}


def shift_guard_error(exc, packet_lines=None):
    """Map packet start lines to 1-based statement indices.

    Without a packet map, keep the one-line prefix contract used by Shell.
    An unrecognized packet line maps to zero so it cannot falsely identify
    a completed statement. Never rewrite a line quoted in the SQL echo.
    """
    from .connector import ConnectorError

    def shift(match):
        line = int(match.group(2))
        mapped = (getattr(packet_lines, "indices", range(len(packet_lines)))[packet_lines.index(line)] + 1
                  if line in packet_lines else 0) \
            if packet_lines is not None else max(1, line - 1)
        return match.group(0)[:match.start(2) - match.start()] + str(mapped) + ":"

    text = str(exc)
    if packet_lines is not None:
        match = _target_error_match(text)
        if match:
            # MariaDB echoes the whole packet before the error. That echo
            # can contain passwords, or misleading statement/line text.
            text = text[match.start():]
    out = ConnectorError(_TARGET_ERROR.sub(shift, target_error_text(text)))
    out.guard_markers = getattr(exc, "guard_markers", ())
    if packet_lines is not None:
        match = _target_error_match(str(exc))
        if match and int(match.group(2)) in packet_lines:
            packet = packet_lines.index(int(match.group(2)))
            # A server diagnostic locates the packet, not a substatement of
            # a dependent sequence. Never invent which of those completed.
            out.guarded_group = getattr(packet_lines, "sizes", (1,) * len(packet_lines))[packet] > 1
        out.guarded_unknown = not match and not target_not_connected(out)
    return out


def target_not_connected(exc):
    """Only an initial connection/login failure proves no packet was sent."""
    return (not getattr(exc, "guard_markers", ()) and not re.search(r"\bat line \d+", str(exc))
            and error_code(str(exc)) in (1044, 1045, 1049, 1129, 1130, 1698, 2002, 2003, 2005, 2026))


def redact_target_error(exc, password=None):
    """Redact diagnostics without discarding private transport evidence."""
    from .connector import ConnectorError
    out = ConnectorError(redact(str(exc), password))
    for key in ("guard_markers", "guarded_unknown", "guarded_group"):
        if hasattr(exc, key):
            setattr(out, key, getattr(exc, key))
    return out


def normalize_type(value):
    """A connection type: 'production' or 'development'. Anything else is an error."""
    if value in (None, ""):
        return DEFAULT_CONNECTION_TYPE
    if not isinstance(value, str) or value.lower() not in CONNECTION_TYPES:
        raise ValueError("Choose connection type 'production' or 'development'.")
    return value.lower()


def connection_safety(profile=None, connection=None, store=None, target_profile=None):
    """The type and read-only flag a write on this connection is judged by.

    A request never lowers the protection. An unsaved connection is
    ``production`` whatever it claims, and may only ask to be read-only
    (``connection["read_only"] is True``). A saved profile's type applies
    only when *connection* (when given) still points at the profile's host,
    port, user and TLS settings (``workspace_store.ENDPOINT_FIELDS``, as for
    the saved password); otherwise it is ``production``. The profile's
    read-only flag applies either way. Returns ``{"profile_id",
    "profile_name", "type", "read_only", "endpoint_matches"}``.

    With *store* (the workspace store), a request that names a connection
    group (``connection["group_name"]``) or a *target_profile* other than the
    active member is judged by the group's effective safety: the strictest
    type of the active member, the target and every current member of the
    group (connection_groups.group_effective_safety; production > staging >
    development > local > unset). Read-only stays the member's own flag. A
    group the store cannot confirm keeps ``production``: the protection never
    drops below it.
    """
    connection = connection if isinstance(connection, dict) else {}
    asked_read_only = connection.get("read_only") is True
    if isinstance(profile, dict):
        ctype = profile.get("type")
        ctype = ctype if ctype in CONNECTION_TYPES else DEFAULT_CONNECTION_TYPE
        matches = _same_endpoint(profile, connection) if connection else True
        if not matches:
            ctype = DEFAULT_CONNECTION_TYPE
        safety = {"profile_id": profile.get("id"), "profile_name": profile.get("name"), "type": ctype,
                  "read_only": bool(profile.get("read_only")) or asked_read_only, "endpoint_matches": matches}
    else:
        safety = {"profile_id": None, "profile_name": None, "type": DEFAULT_CONNECTION_TYPE,
                  "read_only": asked_read_only, "endpoint_matches": False}
    if store is None:
        return safety
    return _group_safety(safety, profile, connection, store, target_profile)


def _group_safety(safety, profile, connection, store, target_profile=None):
    """Raise *safety* to the group connection's effective type (never lower it).

    Applies when the request names ``group_name`` or targets a member other
    than the active one. The group's members are read from the store at every
    call, so a member joining or leaving changes the policy (and the
    :func:`connection_identity` built from it) at once.
    """
    from .connection_groups import _TYPE_STRICTNESS, group_effective_safety, strictest_type, validate_group
    group_name = connection.get("group_name")
    target_id = target_profile.get("id") if isinstance(target_profile, dict) else None
    active_id = safety.get("profile_id") or connection.get("profile_id")
    if group_name in (None, "") and (not target_id or target_id == active_id):
        return safety
    group = None
    if group_name not in (None, ""):
        try:
            group = validate_group(group_name) or None
        except ValueError:
            group = None
        if group is None:
            # An unusable group claim cannot lower the protection.
            return dict(safety, type=DEFAULT_CONNECTION_TYPE)
    try:
        profiles = store.list("profile")
    except Exception:
        profiles = None
    if not isinstance(profiles, (list, tuple)):
        return dict(safety, type=DEFAULT_CONNECTION_TYPE)
    if group is not None:
        # Consent belongs to this stored incarnation of the group. Never
        # accept a generation supplied by the request or a stale profile.
        member = next((p for p in profiles if isinstance(p, dict) and p.get("id") == active_id
                       and p.get("group") == group), None)
        safety = dict(safety, group_name=group,
                      group_generation=(member or {}).get("group_generation") or "")
    candidates = [safety["type"]]
    if group is not None:
        candidates.append(group_effective_safety(profiles, group))
    elif isinstance(profile, dict) and isinstance(profile.get("group"), str) and profile.get("group"):
        candidates.append(group_effective_safety(profiles, profile["group"]))
    if isinstance(target_profile, dict):
        target_type = target_profile.get("type")
        candidates.append(target_type if target_type in CONNECTION_TYPES else DEFAULT_CONNECTION_TYPE)
    effective = strictest_type(candidates)
    if effective is not None and _TYPE_STRICTNESS.get(effective, 0) > _TYPE_STRICTNESS.get(safety["type"], 0):
        return dict(safety, type=effective)
    return safety


def _same_endpoint(profile, connection):
    from .live import connection_options
    from .workspace_store import ENDPOINT_FIELDS, endpoint_value
    try:
        options = connection_options({k: v for k, v in connection.items() if k != "password"})
    except (ValueError, TypeError):
        return False
    return all(endpoint_value(options, f) == endpoint_value(profile, f) for f in ENDPOINT_FIELDS)


def ssh_identity(connection):
    """The SSH part of a connection identity: ``["ssh", host, user, port]``
    (user "" and port 0 when not set), or None without an SSH host. The
    same list as ``sshKey()`` in apps/workspace/src/binding.ts."""
    from .workspace_store import endpoint_value
    c = connection if isinstance(connection, dict) else {}
    host = endpoint_value(c, "ssh_host")
    if not host:
        return None
    try:
        port = int(c.get("ssh_port") or 0)
    except (TypeError, ValueError):
        port = 0
    return ["ssh", host, endpoint_value(c, "ssh_user"), port]


def connection_identity(connection, safety=None):
    """The key reports and confirmations are bound to, as JSON text: the same
    list as ``connectionKey()`` in apps/workspace/src/binding.ts (saved
    connection id, host, port, user, TLS mode and CA, then
    :func:`ssh_identity` only for a connection through SSH), followed by the
    type and read-only flag when they are not the defaults (production,
    read-write), so a Production read-write key is unchanged and a
    connection without SSH keeps its key byte for byte. Never the password
    or the default schema. *safety* is :func:`connection_safety`; without it
    the connection's own ``type`` and ``read_only`` are used."""
    from .workspace_store import endpoint_value
    c = connection if isinstance(connection, dict) else {}
    try:
        port = int(c.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    # Host and CA path trimmed like live.connection_options (and connectionKey).
    key = [c.get("profile_id") or "", endpoint_value(c, "host"), port, c.get("user") or "", c.get("ssl_mode") or "",
           endpoint_value(c, "ssl_ca")]
    ssh = ssh_identity(c)
    if ssh:
        key.append(ssh)
    source = safety if isinstance(safety, dict) else c
    ctype = source.get("type") if source.get("type") in CONNECTION_TYPES else DEFAULT_CONNECTION_TYPE
    read_only = source.get("read_only") is True
    if ctype != DEFAULT_CONNECTION_TYPE or read_only:
        key += [ctype, read_only]
    if isinstance(safety, dict) and safety.get("group_name"):
        # A mapping cannot be mistaken for the optional SSH list at index 6
        # by identity_endpoint. Cleanup remains bound only to its endpoint.
        key.append({"group_name": safety["group_name"],
                    "group_generation": safety.get("group_generation") or ""})
    return json.dumps(key, separators=(",", ":"), ensure_ascii=False)


def identity_group(identity):
    """The store-derived consent binding, apart from endpoint and policy.

    Account plans allow a policy change to reach the current policy gate,
    but must never survive changing or recreating their connection group.
    """
    try:
        key = json.loads(identity) if isinstance(identity, str) else None
    except ValueError:
        return None
    if isinstance(key, list) and key and isinstance(key[-1], dict):
        return key[-1]
    return None


#: Elements of a :func:`connection_identity` list that name the endpoint.
IDENTITY_ENDPOINT_FIELDS = 6


def identity_endpoint(identity):
    """The endpoint part of a :func:`connection_identity` text (saved
    connection id, host, port, user, TLS mode and CA, and the SSH target of a
    connection through SSH, without the type and
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
    # A connection through SSH has its SSH target (a list) right after the
    # endpoint fields; the type that may follow is text.
    size = IDENTITY_ENDPOINT_FIELDS + (1 if len(key) > IDENTITY_ENDPOINT_FIELDS
                                       and isinstance(key[IDENTITY_ENDPOINT_FIELDS], list) else 0)
    return json.dumps(key[:size], separators=(",", ":"), ensure_ascii=False)


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
# An unfinished quoted run is one token through EOF. String escapes include
# line breaks and a final lone backslash, so a failed match never retries at
# each escaped quote. Quoted identifiers keep literal backslashes: SHOW
# GRANTS escapes their quote only by doubling it (privileges.quote_identifier).
GRANT_TOKEN = re.compile(r'''`(?:``|[^`])*(?:`|\Z)|"(?:""|[^"])*(?:"|\Z)|'(?:''|\\[\s\S]|[^'\\])*(?:'|\\?\Z)|[.,@()*;]|[^\s.,@()*;`"']+''')
_GRANT_CLOSED_QUOTE = re.compile(r"""`(?:``|[^`])*`|"(?:""|[^"])*"|'(?:''|\\[\s\S]|[^'\\])*'""")
_GRANT_SECRET_CLAUSE = re.compile(r"\b(?:IDENTIFIED|PASSWORD)\b", re.I)

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
    matches, cursor = [], 0
    first_quote, unfinished = None, False
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        token = GRANT_TOKEN.match(text, cursor)
        if token is None:
            # An unterminated quoted literal consumes the remainder. Never
            # retry its escaped quotes as new literals or clause boundaries.
            unfinished = True
            break
        if text[cursor] in "'\"`":
            if first_quote is None:
                first_quote = cursor
            if _GRANT_CLOSED_QUOTE.fullmatch(token.group()) is None:
                unfinished = True
        matches.append(token)
        cursor = token.end()
    if unfinished and first_quote is not None:
        # A missing account quote can pair with the credential's opening
        # quote, leaving only its final quote unfinished. In that case the
        # apparent earlier quote boundaries cannot protect a secret clause.
        clause = _GRANT_SECRET_CLAUSE.search(text, first_quote + 1)
        if clause is not None:
            text = text[:clause.start()].rstrip()
            matches = [m for m in matches if m.end() <= len(text)]
    upper = [m.group().upper() for m in matches]
    parts, kept, index = [], 0, 0
    # Tokenize once and visit each token once. Re-tokenizing the remaining
    # text for every IDENTIFIED clause makes a long grant quadratic.
    while index < len(matches):
        if upper[index] != "IDENTIFIED":
            index += 1
            continue
        parts.append(text[kept:matches[index].start()].strip())
        index += 1
        while index < len(matches) and not any(
                tuple(upper[index:index + len(words)]) == words for words in _CREDENTIAL_ENDS):
            index += 1
        kept = matches[index].start() if index < len(matches) else len(text)
    if not parts:
        return text
    parts.append(text[kept:].strip())
    return " ".join(part for part in parts if part)


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


def _grants_read_sql(tag, galera_identity=False):
    """Frame one fresh login's account, grants and wildcard setting.

    SHOW rather than @@ reads optional variables without failing on MariaDB
    or MySQL 5.7. Both identity snapshots also include the connection id:
    a reconnect must not combine two logins, even to the same server.
    Galera node status is opt-in: MySQL materializes the global status cache
    for this read (storage/perfschema/pfs_variable.cc:1166-1210).
    """
    who = "USER(), CURRENT_USER(), VERSION(), CONNECTION_ID(), @@global.server_id, @@global.hostname, @@global.port"
    variables = "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('server_uuid', 'partial_revokes')"
    node = "SHOW GLOBAL STATUS WHERE Variable_name = 'wsrep_gcomm_uuid'"
    snapshot = [variables] + ([node] if galera_identity else [])
    return "; ".join(tag + sql for sql in (
        ["SELECT 'myflames_grants_begin', " + who] + snapshot +
        ["SELECT 'myflames_grants_rows'", "SHOW GRANTS", "SELECT 'myflames_grants_end', " + who] + snapshot +
        ["SELECT 'myflames_grants_done'"]))


def read_grants(conn, tag="/*myflames:dbops*/ ", galera_identity=False):
    """Account, SHOW GRANTS and partial_revokes from one client invocation.

    Identity/account snapshots bracket the grants in that same login. A
    changed or incomplete read returns checked=False and no account/grants,
    so it can never produce an advisory GRANT for another backend's user.
    MariaDB's password hash/plugin clauses are removed before returning.
    Request galera_identity only when the caller needs the node UUID; other
    reads, including lifecycle preflights, never materialize global status.
    """
    from .connector import ConnectorError, unescape_mysql
    from .perf_common import _reason

    def failed(error):
        return {"checked": False, "user": None, "account": None, "grants": [],
                "partial_revokes": None, "error": redact(error)}

    try:
        text = conn.run(_grants_read_sql(tag, galera_identity), extra_flags=["--comments", "--skip-reconnect"])
    except ConnectorError as exc:
        return failed(_reason(exc))
    rows = [[unescape_mysql(cell) for cell in line.split("\t")] for line in text.split("\n") if line]
    starts = [i for i, row in enumerate(rows) if len(row) == 8 and row[0] == "myflames_grants_begin"]
    middle = [i for i, row in enumerate(rows) if row == ["myflames_grants_rows"]]
    ends = [i for i, row in enumerate(rows) if len(row) == 8 and row[0] == "myflames_grants_end"]
    done = [i for i, row in enumerate(rows) if row == ["myflames_grants_done"]]
    if not all(len(indices) == 1 for indices in (starts, middle, ends, done)):
        return failed("The account and grants could not be read in one verified server session.")
    start, middle, end, done = starts[0], middle[0], ends[0], done[0]
    if not (start == 0 < middle < end < done == len(rows) - 1):
        return failed("The account and grants could not be read in one verified server session.")
    who = rows[start]
    before = {row[0].lower(): row[1] for row in rows[start + 1:middle] if len(row) == 2}
    after = {row[0].lower(): row[1] for row in rows[end + 1:done] if len(row) == 2}
    if who[1:] != rows[end][1:] or before != after:
        return failed("The server identity or account changed while reading grants. Refresh and try again.")
    # VERSION() can be overridden in MariaDB. The registered variables are
    # the engine evidence: MySQL has server_uuid, MariaDB does not.
    if (not all(who[1:]) or ("server_uuid" in before and
                            not _TARGET_UUID.fullmatch(before.get("server_uuid") or "")) or
            (galera_identity and not _TARGET_UUID.fullmatch(before.get("wsrep_gcomm_uuid") or ""))):
        return failed("The account and grants could not be read in one verified server session.")
    grant_rows = rows[middle + 1:end]
    if any(len(row) != 1 or not row[0] for row in grant_rows):
        return failed("The account and grants could not be read in one verified server session.")
    grants = [strip_credentials(row[0]) for row in grant_rows]
    partial = before.get("partial_revokes", "").upper()
    partial = True if partial in ("ON", "1") else False if partial in ("OFF", "0") else None
    login = who[1] or ""
    user = login.rsplit("@", 1)[0] if "@" in login else (login or None)
    return {"checked": True, "user": user, "account": who[2], "grants": grants,
            "partial_revokes": partial, "error": None}


# ---- redaction -------------------------------------------------------------------------

REDACTED = "[redacted]"
# Names describe policy, paths and plugin tuning too, so never infer a secret
# from a name fragment. Parameter comparison re-exports this same inventory.
_SECRET_VARIABLES = frozenset((
    'wsrep_sst_auth', 'report_password', 'audit_log_encryption_password',
    'keyring_encrypted_file_password', 'keyring_hashicorp_secret_id', 's3_secret_key',
    'handlersocket_plain_secret', 'handlersocket_plain_secret_wr', 'utility_user_password',
    'file_key_management_filekey',
    'telemetry.otel_exporter_otlp_logs_headers',
    'telemetry.otel_exporter_otlp_metrics_headers',
    'telemetry.otel_exporter_otlp_traces_headers',
    'telemetry.otel_exporter_otlp_logs_secret_headers',
    'telemetry.otel_exporter_otlp_metrics_secret_headers',
    'telemetry.otel_exporter_otlp_traces_secret_headers'))


def sensitive(name):
    """Whether a system variable's value is always treated as a secret."""
    return name.lower() in _SECRET_VARIABLES


_STRING = r"""(?:'[^'\\]*(?:(?:''|\\[\s\S])[^'\\]*)*(?:'|\\?\Z)|"[^"\\]*(?:(?:""|\\[\s\S])[^"\\]*)*(?:"|\\?\Z))"""
_STRING_NO_BACKSLASH = r"""(?:'[^']*(?:''[^']*)*(?:'|\Z)|"[^"]*(?:""[^"]*)*(?:"|\Z))"""
# Stored text includes rejected SQL too. Hide introduced/encoded literals
# even where that credential clause would reject their spelling on a server.
# Escaped openers belong to dynamic SQL and need its enclosing-string end.
_SECRET_VALUE = (
    r"(?:" + _STRING + r"|\\['\"]"
    r"|(?:(?<![\w])_[\w]+\s+)?(?:0x[0-9a-fA-F]+|0b[01]+)\b"
    r"|(?:(?<![\w])_[\w]+(?:\s+[XBN])?\s*|[XBN]\s*)(?:" + _STRING + r"|\\['\"]))"
    r"(?:\s+COLLATE\s+(?:`[^`]*`|[\w]+))?")
_SECRET_VALUE_FIRST_CHARS = frozenset("'\"\\0_xbnXBN")
_SECRET_VALUE_OPENER = re.compile(
    r"(?:(?<![\w])_[\w]+(?:\s+[XBN])?\s*|[XBN]\s*)?(\\?)(['\"])", re.I)
# A bounded inventory also protects quoted SQL and server echoes, where a
# whole-statement lexer cannot know which quote belongs to the surrounding text.
# Recognize each quoted component as well as a quoted dotted inventory name.
_SECRET_VARIABLE_NAME = r"(?:" + "|".join(
    r"(?:`" + re.escape(name) + r"`|\"" + re.escape(name) + r"\"|" +
    r"\s*\.\s*".join(r"(?:" + re.escape(part) + r"|`" + re.escape(part) +
                      r"`|\"" + re.escape(part) + r"\")" for part in name.split('.')) + r")"
    for name in sorted(_SECRET_VARIABLES)) + r")"
_SECRET_VARIABLE_LITERAL = re.compile(
    r"((?<![\w$])(?:@@\s*)?(?:(?:GLOBAL|SESSION|PERSIST_ONLY|PERSIST|LOCAL)\s*\.\s*)?" +
    _SECRET_VARIABLE_NAME + r"\s*(?::=|=)\s*)(?P<secret>" + _SECRET_VALUE + r")", re.I | re.S)
# Anchors guaranteed to appear (lowercased) in any ASCII match of
# _SECRET_VARIABLE_LITERAL. Dotted telemetry.* names all share
# 'otel_exporter_otlp_', so 'telemetry' is searched only when that tail exists.
_SECRET_VARIABLE_LITERAL_ANCHORS = tuple(sorted({
    name.split('.', 1)[0] for name in _SECRET_VARIABLES
    if name != 'handlersocket_plain_secret_wr'
}))
_SECRET_VARIABLE_LITERAL_SUBSTRINGS = tuple(sorted({
    'otel_exporter_otlp_' if name.startswith('telemetry.otel_exporter_otlp_') else name
    for name in _SECRET_VARIABLES
    if name != 'handlersocket_plain_secret_wr'
}))
# Keep original enclosing-string boundaries, including whether the final
# quote closed the string or was escaped. The ordinary literal fallback
# cannot distinguish a doubled quote in dynamic SQL from an empty value.
# Bound quotes inside ordinary comments and backtick identifiers to those
# tokens so prose apostrophes never open false strings across later SQL,
# while keeping executable comments transparent.
_SECRET_LITERAL_CONTEXT_STRING = (
    r"'[^'\\]*(?:(?:''|\\[\s\S])[^'\\]*)*(?P<single_end>'|\\?\Z)"
    r'|"[^"\\]*(?:(?:""|\\[\s\S])[^"\\]*)*(?P<double_end>"|\\?\Z)')
_SECRET_LITERAL_CONTEXT_STRING_NO_BACKSLASH = (
    r"'[^']*(?:''[^']*)*(?P<single_end>'|\Z)"
    r'|"[^"]*(?:""[^"]*)*(?P<double_end>"|\Z)')
_SECRET_LITERAL_CONTEXT_SKIP = (
    r"|(?P<backtick>`[^`]*(?:``[^`]*)*(?:`|\Z))"
    r"|(?P<block>/\*(?!!|[mM]!)[\s\S]*?(?:\*/|\Z))"
    r"|(?P<line>--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*)")
_SECRET_LITERAL_CONTEXT_PLAIN = re.compile(_SECRET_LITERAL_CONTEXT_STRING)
_SECRET_LITERAL_CONTEXT_PLAIN_NO_BACKSLASH = re.compile(
    _SECRET_LITERAL_CONTEXT_STRING_NO_BACKSLASH)
_SECRET_LITERAL_CONTEXT = re.compile(
    _SECRET_LITERAL_CONTEXT_STRING + _SECRET_LITERAL_CONTEXT_SKIP)
_SECRET_LITERAL_CONTEXT_NO_BACKSLASH = re.compile(
    _SECRET_LITERAL_CONTEXT_STRING_NO_BACKSLASH + _SECRET_LITERAL_CONTEXT_SKIP)
_REPLACE_FOLLOW = re.compile(
    r"(\s+REPLACE\s+)(?P<secret>" + _SECRET_VALUE + r")", re.I | re.S)
_REPLACE_FOLLOW_NO_BACKSLASH = re.compile(
    _REPLACE_FOLLOW.pattern.replace(_STRING, _STRING_NO_BACKSLASH)
    .replace(r"|\\['\"]", ''), _REPLACE_FOLLOW.flags)
_SECRET_CONCAT_PREFIX = re.compile(
    r"(?:\A|;)\s*SET\s+@[\w$]+\s*(?::=|=)\s*CONCAT\s*\(\s*\Z", re.I)
_SECRET_CONCAT_QUOTE = re.compile(
    r"\s*,\s*QUOTE\s*\(\s*@[\w$]+\s*\)\s*\)(?=\s*(?:;|\Z))", re.I)
_SET_PASSWORD_KEYWORD = re.compile(r"\bSET\s+PASSWORD\b", re.I)


class _SetPasswordMatch:
    """Minimal re.Match-compatible result for the character-bounded SET PASSWORD scan."""
    __slots__ = ('_start', '_secret_start', '_end')

    def __init__(self, start, secret_start, end):
        self._start = start
        self._secret_start = secret_start
        self._end = end

    def start(self, group=0):
        if group == 'secret':
            return self._secret_start
        if group == 0:
            return self._start
        raise IndexError(group)

    def end(self):
        return self._end


class _BoundedSetPasswordPattern:
    """Character-bounded SET PASSWORD prefix scan (512 chars max before '=').

    Skips closed quoted account tokens (for example FOR 'a=b'@'%') so '=' inside
    a quoted account name never ends the prefix early, while bounding the total
    account walk to 512 characters rather than 512 tokens and refusing a bare
    backslash before a quote in backslash-escape mode so repeated malformed
    prefixes stay strictly linear.
    """
    __slots__ = ('pattern', 'flags', '_account', '_tail')

    def __init__(self, backslash_escapes=True):
        if backslash_escapes:
            account_quoted = (
                r"'(?:''|\\[\s\S]|[^'\\])*'|\"(?:\"\"|\\[\s\S]|[^\"\\])*\"|`(?:``|[^`])*`")
            unquoted = r"\\(?!['\"])|[^=;'\"`\\]"
            secret_value = _SECRET_VALUE
        else:
            account_quoted = r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`"
            unquoted = r"[^=;'\"`]"
            secret_value = (_SECRET_VALUE.replace(_STRING, _STRING_NO_BACKSLASH)
                            .replace(r"|\\['\"]", ''))
        account_expr = (
            r"(?:(?=(?P<account>" + account_quoted + r"))(?P=account)|" + unquoted + r")*=")
        self.flags = re.I | re.S
        self.pattern = r"(\bSET\s+PASSWORD\b" + account_expr + r"\s*)(?P<secret>" + secret_value + r")"
        self._account = re.compile(account_expr, re.S)
        self._tail = re.compile(r"\s*(?P<secret>" + secret_value + r")", re.I | re.S)

    def search(self, text, pos=0):
        text_len = len(text)
        account_match = self._account.match
        tail_match = self._tail.match
        kw_search = _SET_PASSWORD_KEYWORD.search
        while pos < text_len:
            cmd = kw_search(text, pos)
            if cmd is None:
                return None
            start = cmd.end()
            limit = min(text_len, start + 513)
            if text.find('=', start, limit) != -1:
                prefix = account_match(text, start, limit)
                if prefix is not None:
                    tail = tail_match(text, prefix.end())
                    if tail is not None:
                        return _SetPasswordMatch(cmd.start(), tail.start('secret'), tail.end())
            pos = start
        return None


_SET_PASSWORD_BOUNDED = _BoundedSetPasswordPattern(True)
_SET_PASSWORD_BOUNDED_NO_BACKSLASH = _BoundedSetPasswordPattern(False)
_SECRET_PATTERNS = tuple(
    p if isinstance(p, _BoundedSetPasswordPattern)
    else re.compile(p + "(?P<secret>" + _SECRET_VALUE + ")", re.I | re.S)
    for p in (
        # IDENTIFIED BY 'x', IDENTIFIED WITH plugin BY|AS 'x', MariaDB IDENTIFIED BY PASSWORD 'hash';
        # CLONE INSTANCE FROM ... IDENTIFIED BY 'x'.
        r"(\bIDENTIFIED\s+(?:WITH\s+(?:`[^`]*`|'[^']*'|\"[^\"]*\"|\w+)\s+)?(?:BY|AS)\s+(?:PASSWORD\s+)?)",
        # MariaDB IDENTIFIED VIA plugin USING 'x' or USING PASSWORD('x').
        r"(\b(?:VIA|WITH)\s+(?:`[^`]*`|\w+)\s+USING\s+(?:PASSWORD\s*\(\s*)?)",
        # PASSWORD('x'), OLD_PASSWORD('x').
        r"(\b(?:OLD_)?PASSWORD\s*\(\s*)",
        # Character-bounded account walk before recognizing an assignment.
        _SET_PASSWORD_BOUNDED,
        # SOURCE_PASSWORD = 'x', MASTER_PASSWORD = 'x', PASSWORD = 'x', OPTIONS (PASSWORD 'x'). Not a
        # word inside quotes ('caching_sha2_password' is followed by its own closing quote).
        r"((?<![\w'\"`])\w*PASSWORD(?:\s*=)?\s*)",
    )
) + (
    _SECRET_VARIABLE_LITERAL,
    # ALTER USER ... RANDOM PASSWORD or SET PASSWORD ... TO RANDOM REPLACE 'current'.
    re.compile(r"((?:\bRANDOM(?:\s+PASSWORD)?|\[redacted\]['\"])\s+REPLACE\s+)(?P<secret>" +
               _SECRET_VALUE + r")", re.I | re.S),
)
# Retain every old diagnostic match, including malformed quoted accounts.
# Only the new introduced-literal alternative needs the stricter account walk.
_SET_PASSWORD_LEGACY_LITERAL = re.compile(
    r"(\bSET\s+PASSWORD\b[^=;]{0,512}=\s*)(?P<secret>" + _STRING + r")", re.I | re.S)
_SECRET_PATTERNS_NO_BACKSLASH = {
    # A backslash before an opener is only an escaped dynamic quote in the
    # escaping reading; treating it as an opener in the other mode invents a
    # value with no end and can consume subsequent, independent statements.
    pattern: (_SET_PASSWORD_BOUNDED_NO_BACKSLASH
              if pattern is _SET_PASSWORD_BOUNDED
              else re.compile(pattern.pattern.replace(_STRING, _STRING_NO_BACKSLASH)
                              .replace(r"|\\['\"]", ''), pattern.flags))
    for pattern in _SECRET_PATTERNS + (_SET_PASSWORD_LEGACY_LITERAL,)}
# Each keyword is required by its corresponding pattern, independent of SQL
# context. REPLACE must not require RANDOM: earlier substitutions supply the
# alternative [redacted] prefix. Unicode inputs always take the full path.
_SECRET_PATTERN_KEYWORDS = ('identified', 'using', 'password', 'password',
                            'password', None, 'replace')
# Quotes and comments consume their whole span, including an unfinished
# tail, before recognizing commands. Start at the beginning of the text so
# SET PASSWORD inside a literal cannot shift the quote boundaries.
_SET_PASSWORD_PART = re.compile(
    _STRING + r"|`(?:``|[^`])*(?:`|\Z)|/\*[\s\S]*?(?:\*/|\Z)"
    r"|--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*|(?P<command>\bSET\s+PASSWORD\b)|[=;]", re.I)
_SET_PASSWORD_LITERAL = re.compile(r"(\s*)(" + _STRING + ")", re.S)
_SET_PASSWORD_VALUE = re.compile(
    r"(\s*)(" + _SECRET_VALUE.replace(r"|\\['\"]", '') + ")", re.I | re.S)
_SET_PASSWORD_ACCOUNT_TOKEN = re.compile(
    r"\s+|/\*[\s\S]*?\*/|(?P<line>--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*)"
    r"|(?P<quoted>'(?:''|\\[\s\S]|[^'\\])*'|\"(?:\"\"|\\[\s\S]|[^\"\\])*\"|`(?:``|[^`])*`)"
    r"|(?P<at>@)|(?P<lp>\()|(?P<rp>\))"
    r"|(?P<word>[\w$.-]+)|(?P<bad>[\s\S])")
_SET_PASSWORD_ACCOUNT_TOKEN_NO_BACKSLASH = re.compile(
    r"\s+|/\*[\s\S]*?\*/|(?P<line>--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*)"
    r"|(?P<quoted>'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`)"
    r"|(?P<at>@)|(?P<lp>\()|(?P<rp>\))"
    r"|(?P<word>[\w$.-]+)|(?P<bad>[\s\S])")
_SET_PASSWORD_SEPARATOR = re.compile(r"\bSET\s+PASSWORD\b|[=;]", re.I)
_SET_PASSWORD_FROM_KEYWORD = re.compile(
    _STRING + r"|`(?:``|[^`])*(?:`|\Z)|/\*[\s\S]*?(?:\*/|\Z)"
    r"|--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*|[=;]")
_URL_SCHEME = re.compile(r"(?<![a-zA-Z0-9+.-])[a-zA-Z0-9+.-]+://")
_URL_SCHEME_LETTER = re.compile(r"[a-zA-Z]")
_URL_SQL_BOUNDARY = re.compile(r"[\s/'\"`]")
_URL_VALUE_BOUNDARY = re.compile(r"[\s/]")
_URL_SQL_END = re.compile(r"[\s'\"`]")
_URL_VALUE_END = re.compile(r"\s")
_URL_DATABASE_SCHEMES = frozenset(('mysql', 'mariadb', 'mysqlx', 'postgres', 'postgresql'))
_URL_IPV6_HOST = re.compile(
    r"\[[0-9a-fA-F.]*:[0-9a-fA-F.]*:[0-9a-fA-F:.]*(?:%[0-9a-zA-Z._~%-]+)?\](?::[0-9]*)?")
# Literal and comment tokens protect commas and SET words inside values.
# Keep name components separate so comments and backticks can surround dots
# or the assignment operator without hiding a sensitive system variable.
_SET_VARIABLE_NAME_PART = r"(?:[\w$]+|" + _STRING + r"|`[^`]*(?:``[^`]*)*(?:`|\Z))"
_SET_VARIABLE_COMPONENT = (
    r"(?P<word>[\w$]+)"
    r"|(?P<quoted>" + _STRING + r"|`[^`]*(?:``[^`]*)*(?:`|\Z))"
    # Reversed MariaDB markers need five digits before a version is present;
    # fewer digits are SQL, possibly the secret value. Keep the established
    # permissive normal-marker handling, and leave sixth digits to the lexer.
    r"|/\*(?:(?P<executable>(?:!|M!)(?:!(?:[0-9]{5})?|-?\d{0,5}))|(?P<comment>[\s\S]*?(?:\*/|\Z)))"
    r"|(?P<linecomment>--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*)"
    r"|@+|:=|[=.,;()]|[^\w\s'\"`@=.,;()/*]+|[^\s]")
_SET_VARIABLE_TOKEN = (
    r"(?P<word>[\w$]+)"
    # A variable's name is never a clause keyword, including @@scope.name.
    r"|(?P<variable>@@?" + _SET_VARIABLE_NAME_PART +
    r"(?:[ \t]*\.[ \t]*" + _SET_VARIABLE_NAME_PART + r")*)"
    r"|(?P<quoted>" + _STRING + r"|`[^`]*(?:``[^`]*)*(?:`|\Z))"
    r"|/\*(?:(?P<executable>(?:!|M!)(?:!(?:[0-9]{5})?|-?\d{0,5}))|(?P<comment>[\s\S]*?(?:\*/|\Z)))"
    r"|(?P<linecomment>--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*)"
    r"|@+|:=|[=.,;()]|[^\w\s'\"`@=.,;()/*]+|[^\s]")
_SET_VARIABLE_COMPONENT_PART = re.compile(_SET_VARIABLE_COMPONENT, re.I)
_SET_VARIABLE_PART = re.compile(_SET_VARIABLE_TOKEN, re.I)
_SET_VARIABLE_EXECUTABLE_PART = re.compile(r"(?P<end>\*/)|" + _SET_VARIABLE_TOKEN, re.I)
_SET_VARIABLE_COMPONENT_NO_BACKSLASH = re.compile(
    _SET_VARIABLE_COMPONENT.replace(_STRING, _STRING_NO_BACKSLASH), re.I)
_SET_VARIABLE_PART_NO_BACKSLASH = re.compile(
    _SET_VARIABLE_TOKEN.replace(_STRING, _STRING_NO_BACKSLASH), re.I)
_SET_VARIABLE_EXECUTABLE_NO_BACKSLASH = re.compile(
    r"(?P<end>\*/)|" + _SET_VARIABLE_TOKEN.replace(_STRING, _STRING_NO_BACKSLASH), re.I)
# Fast-skip pattern when outside SET / INSERT / executable comments (no custom
# boundaries): consumes strings, backticks and comments intact and stops only
# at tokens that can change parser state (SET, INSERT, executable opener, '(', ')', ';').
_SET_SCAN_OUTSIDE_PATTERN = (
    _STRING + r"|`[^`]*(?:``[^`]*)*(?:`|\Z)"
    r"|/\*(?:(?P<executable>(?:!|M!)(?:!(?:[0-9]{5})?|-?\d{0,5}))|[\s\S]*?(?:\*/|\Z))"
    r"|--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*"
    r"|(?P<word>(?<![\w$])(?:SET|INSERT)(?![\w$]))"
    r"|(?P<sym>[();])"
    r"|:=|(?P<punct>[^\w\s'\"`@=.,;()/*$][^\w\s'\"`@=.,;()/*]*)")
_SET_SCAN_OUTSIDE = re.compile(_SET_SCAN_OUTSIDE_PATTERN, re.I)
_SET_SCAN_OUTSIDE_NO_BACKSLASH = re.compile(
    _SET_SCAN_OUTSIDE_PATTERN.replace(_STRING, _STRING_NO_BACKSLASH), re.I)
# Matches a single simple non-quoted, non-commented, non-multi-assignment SET
# statement in lowercased ASCII text so plain editor scripts can skip the full
# SET lexer in C-speed str.find passes. Any quote, comment, comma, paren,
# backslash, or second '=' falls back to the full lexer.
_SIMPLE_SET_ASSIGN = re.compile(
    r"set[ \t\r\n]+(?:(?:global|session|persist_only|persist|local)[ \t\r\n]+)?"
    r"(?:@@(?:(?:global|session|persist_only|persist|local)\.)?)?"
    r"([a-z0-9_$]+)[ \t\r\n]*:?=[^=;'\"`/*#\-,()\\]*(?:;|\Z)")
_SET_VARIABLE_VERSION_TAIL = re.compile(r"(?P<word>\d+)")
_SET_VARIABLE_SCOPES = frozenset(('GLOBAL', 'SESSION', 'PERSIST', 'PERSIST_ONLY', 'LOCAL'))
_SET_VALUE_ENDS = frozenset(('WHERE', 'ORDER', 'LIMIT', 'ON', 'RETURNING', 'FOR', 'AS'))
# Recognize a complete row alias before revealing it. Unrecognized or
# unfinished suffixes stay inside the hidden value, including arithmetic
# containing an AS identifier. Ordinary comments here also fail closed.
_SET_ALIAS_IDENTIFIER = r"(?:`(?:``|[^`])+`|[A-Za-z_$][\w$]*)"
_SET_ROW_ALIAS = re.compile(
    r"AS\s+" + _SET_ALIAS_IDENTIFIER +
    r"(?:\s*\(\s*" + _SET_ALIAS_IDENTIFIER +
    r"(?:\s*,\s*" + _SET_ALIAS_IDENTIFIER + r")*\s*\))?\s*" +
    r"(?=ON\s+DUPLICATE\s+KEY\s+UPDATE\b|;|\Z)", re.I)
# Only reveal an alias after a value with an unambiguous outer shape. Infix
# expressions and unfinished operator phrases stay hidden through the alias.
# The token walk separately checks balanced parentheses and quote boundaries.
_SET_ALIAS_VALUE = re.compile(
    r"(?:" + _STRING + r"|@[\w$]+|[0-9]+|(?:[A-Za-z_$][\w$]*\s*)?\([\s\S]*\))")
# ER_WRONG_VALUE_FOR_VAR prints the rejected value verbatim, so apostrophes
# and line breaks inside it are not SQL quote or message boundaries.
_WRONG_VALUE_FOR_VAR = re.compile(
    r"\bVariable '([^'\r\n]{1,64})' can't be set to the value of '", re.I)
# Match the same client code spelling as error_code / connection_error_text,
# including wrappers and the client's optional SQLSTATE / at-line suffix.
_CLIENT_ERROR_CODE = re.compile(r"\bERROR\s+(\d{3,5})\b", re.I)
_ERROR_QUOTED_NAME = re.compile(r"(?<![A-Za-z0-9_])(['\"`])([^'\"`\r\n]{1,64})\1")
# System variable names (including component.name) are ASCII identifiers in both engines.
_ERROR_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?")
# ASCII prose apostrophes (can't, Italian puo') are not value openers, while
# non-ASCII labels (MariaDB Chinese 变量'...'无法设置为'...') can touch the quote.
_ERROR_VALUE_OPEN = re.compile(r"(?<![A-Za-z0-9_])['\"`]")
# Fail closed across a custom DELIMITER boundary when the remaining text
# contains a credential keyword, inventory variable name, URL scheme, or client
# error echo (such as ERROR 1064 near '...'), matching 3.5.1. Escaped
# whitespace in JSON/Python-repr echoes (\nIDENTIFIED) also precedes keywords.
_DELIMITER_TAIL_CREDENTIAL = re.compile(
    r"(?:\b|(?<=\\[nrtfv]))(?:IDENTIFIED|USING|REPLACE)\b|PASSWORD|://|(?:\b|(?<=\\[nrtfv]))ERROR\s+\d{3,5}\b|" +
    "|".join(r"(?:\b|(?<=\\[nrtfv]))" + re.escape(name.split(".")[-1]) + r"\b"
             for name in sorted(_SECRET_VARIABLES)),
    re.I)


#: MySQL SHOW CREATE USER prints a hash with non-printable bytes as a hex
#: literal when print_identified_with_as_hex is ON: ``AS 0x2441...``.
_HEX_SECRET = re.compile(r"(\b(?:AS|BY|USING)\s+(?:PASSWORD\s+)?)0x[0-9A-F]+\b", re.I)
def _URL_SECRET(text, quoted=True):
    """Yield password spans in URL authorities, with no credential length limit.

    A slash normally ends an authority. Before any ``@``, the value after a
    user-info colon can instead be a malformed password containing slashes;
    scan it through the last ``@`` before whitespace or a quote. Only database
    schemes treat a numeric prefix as a possible password; other schemes keep
    numeric host ports intact. Bracketed IPv6 hosts and username-only paths
    stay untouched. Only the long slash scan skips schemes it already covered,
    keeping adjacent URLs visible and nested or unfinished URLs linear.
    SQL keeps quotes as boundaries; server values can contain quotes in a
    password and use ``quoted=False``. Pool prefixes and empty users work too.
    """
    boundary = _URL_SQL_BOUNDARY if quoted else _URL_VALUE_BOUNDARY
    value_end = _URL_SQL_END if quoted else _URL_VALUE_END
    scanned = 0
    for scheme in _URL_SCHEME.finditer(text):
        if scheme.start() < scanned:
            continue
        if not _URL_SCHEME_LETTER.search(text, scheme.start(), scheme.end() - 3):
            continue
        mark = boundary.search(text, scheme.end())
        end = mark.start() if mark is not None else len(text)
        last_at = text.rfind("@", scheme.end(), end)
        colon = text.find(":", scheme.end(), last_at if last_at >= 0 else end)
        # Only a bracketed IPv6 host directly after "://" treats its colon as
        # a host colon (after "user-info@", last_at >= 0 already skips it).
        # Brackets in a user name must not disable redaction.
        if colon >= 0 and last_at < 0 and text.startswith("[", scheme.end()):
            host = _URL_IPV6_HOST.match(text, scheme.end(), end)
            if host is not None and (host.end() == end or text[host.end()] in (",", "?", "#")):
                colon = -1
        if (last_at < 0 and colon >= 0 and end < len(text) and text[end] == "/"
                # A ':' prefix is already outside the matched scheme; for '+'
                # dialect/driver or pool combinations, match any component.
                and (any(part in _URL_DATABASE_SCHEMES
                         for part in text[scheme.start():scheme.end() - 3].lower().split('+'))
                     or not text[colon + 1:end].isdigit())):
            mark = value_end.search(text, end)
            end = mark.start() if mark is not None else len(text)
            last_at = text.rfind("@", colon + 1, end)
            # A normal authority scan can end at the next scheme's slash.
            # Skip schemes only after a long scan has covered their values.
            scanned = end
        if colon >= 0 and last_at >= 0:
            yield colon + 1, last_at


def _secret_variable_boundary_plan(text):
    """Return ``(safe_boundaries, prefix_boundaries)`` proved under both readings.

    Only the shared client splitter can establish custom statement endings.
    Ignore ambiguous/unclosed units so uncertain framing still hides more.
    Both backslash readings must agree before revealing anything after it.
    Above 2 Mi characters or 4,096 delimiter tokens (semicolons plus every
    declared custom delimiter), or when no DELIMITER command line declares a
    usable non-semicolon delimiter, skip these two complete lexer/splitter
    passes: redaction also runs synchronously during editor and investigation
    saves. Count potential endings before lexing, even inside quotes/comments,
    to bound comment-heavy inputs whose client command lookahead can revisit
    the remaining text at every ending. When a later unit has an issue or an
    unclosed quote, *safe_boundaries* keeps only endings after the last bad
    unit while *prefix_boundaries* keeps clean endings before the first bad
    unit so glued delimiters (``1$$SET ...``) still separate statements while
    failing closed on any secret value that reaches them. The splitter itself
    stays unbounded for Query editor callers.
    """
    if len(text) > 2 * 1024 * 1024 or 'delimiter' not in text.lower():
        return [], []
    total = text.count(';')
    if total > 4096:
        return [], []
    declared = set()
    commands = 0
    for match in _DELIMITER_LINE.finditer(text):
        commands += 1
        if commands > 4096:
            return [], []
        value, _ = _delimiter_argument(text, match.end())
        if value and '\\' not in value and value != ';' and value not in declared:
            declared.add(value)
            if len(declared) > 64:
                return [], []
            total += text.count(value)
            if total > 4096:
                return [], []
    if not declared:
        return [], []
    safe_readings, prefix_readings = [], []
    any_bad = False
    for escapes in (True, False):
        statements = list(split_statements(text, backslash_escapes=escapes))
        safe = set()
        bad = False
        # Use a custom boundary to reveal trailing text only when both it and
        # every later unit are issue-free and closed under both readings.
        for statement in reversed(statements):
            if statement['issues'] or statement['unclosed']:
                bad = True
                break
            delimiter, end = statement['delimiter'], statement['next_start']
            start = end - len(delimiter)
            if (delimiter != ';' and statement['end'] <= start
                    and text.startswith(delimiter, start)):
                safe.add((start, end))
        prefix = set()
        if bad:
            any_bad = True
            for statement in statements:
                if statement['issues'] or statement['unclosed']:
                    break
                delimiter, end = statement['delimiter'], statement['next_start']
                start = end - len(delimiter)
                if (delimiter != ';' and statement['end'] <= start
                        and text.startswith(delimiter, start)):
                    prefix.add((start, end))
        else:
            prefix = safe
        safe_readings.append(safe)
        prefix_readings.append(prefix)
    safe_common = sorted(safe_readings[0] & safe_readings[1])
    if not any_bad:
        return safe_common, []
    prefix_common = sorted((prefix_readings[0] & prefix_readings[1]) - set(safe_common))
    return safe_common, prefix_common


def _secret_variable_boundaries(text):
    """Return custom endings proved safe under both backslash readings."""
    return _secret_variable_boundary_plan(text)[0]


def _may_have_set_secret_variables(folded):
    """Whether lowercased ASCII *folded* may contain a SET secret assignment.

    Only returns False when no executable comment is present and every
    standalone ``set`` word is a single plain assignment to a non-secret
    variable containing no quotes, comments, commas, parentheses, backslashes,
    or second ``=`` before ``;`` or EOF.
    """
    if 'set' not in folded:
        return False
    if ('/*!' in folded or '/*m!' in folded
            or ('delimiter' in folded and len(folded) <= 2 * 1024 * 1024)):
        return True
    pos = 0
    text_len = len(folded)
    simple_match = _SIMPLE_SET_ASSIGN.match
    secret_vars = _SECRET_VARIABLES
    while True:
        idx = folded.find('set', pos)
        if idx < 0:
            return False
        if idx > 0 and (folded[idx - 1].isalnum() or folded[idx - 1] == '_'):
            pos = idx + 3
            continue
        if idx + 3 < text_len and (folded[idx + 3].isalnum() or folded[idx + 3] in '_$'):
            pos = idx + 3
            continue
        match = simple_match(folded, idx)
        if match is None or match.group(1) in secret_vars:
            return True
        pos = match.end()


def _may_have_secret_variable_literals(folded):
    """Whether lowercased ASCII *folded* contains any inventory literal name."""
    return any(sub in folded for sub in _SECRET_VARIABLE_LITERAL_SUBSTRINGS)


def _may_have_secret_variables(text):
    """Skip inventory scans only when no supported name can start here."""
    if '=' not in text:
        return False
    # Python's Unicode re.I also matches dotless i, long s and Kelvin sign.
    # Keep that behavior, and leave executable version/name joins to the lexer.
    if not text.isascii():
        return True
    folded = text.lower()
    return (_may_have_secret_variable_literals(folded)
            or _may_have_set_secret_variables(folded))


def _secret_variable_spans(text, backslash_escapes=True, allow_custom_boundaries=True,
                           custom_boundaries=None, pass1_redacted=None,
                           fail_closed_boundaries=False, include_stage=False):
    """Find SET values from the shared inventory in one quote-mode reading.

    Top-level clauses end a value; commas and later SET statements start new
    assignments. Parentheses, strings and ordinary comments protect separators.
    Executable-comment markers are transparent to SQL state. Track the last
    value token to leave a trailing closing marker and whitespace intact.
    """
    if custom_boundaries is not None:
        boundaries = custom_boundaries
    else:
        boundaries = _secret_variable_boundaries(text) if allow_custom_boundaries else ()
    num_boundaries = len(boundaries)
    no_boundaries = num_boundaries == 0
    text_len = len(text)
    if backslash_escapes:
        normal_pat = _SET_VARIABLE_PART
        exec_pat = _SET_VARIABLE_EXECUTABLE_PART
        component_pattern = _SET_VARIABLE_COMPONENT_PART
        outside_pat = _SET_SCAN_OUTSIDE
    else:
        normal_pat = _SET_VARIABLE_PART_NO_BACKSLASH
        exec_pat = _SET_VARIABLE_EXECUTABLE_NO_BACKSLASH
        component_pattern = _SET_VARIABLE_COMPONENT_NO_BACKSLASH
        outside_pat = _SET_SCAN_OUTSIDE_NO_BACKSLASH
    secret_vars = _SECRET_VARIABLES
    scopes = _SET_VARIABLE_SCOPES
    value_ends = _SET_VALUE_ENDS
    version_tail_match = _SET_VARIABLE_VERSION_TAIL.match
    row_alias_match = _SET_ROW_ALIAS.match
    alias_value_fullmatch = _SET_ALIAS_VALUE.fullmatch
    delim_tail_search = _DELIMITER_TAIL_CREDENTIAL.search
    insert_reset_words = ('SELECT', 'UPDATE', 'DELETE', 'REPLACE', 'ALTER',
                          'CREATE', 'DROP', 'VALUES', 'VALUE')

    value_start, value_end = None, None
    in_set, depth, name_parts, awaiting_value = False, 0, None, False
    insert, value_can_end = False, False
    stuck_351, value_no_drop = False, False
    version_tail_at = None
    tail_clean_from = text_len
    boundary = 0
    cursor, executable = 0, False

    while cursor < text_len:
        if (no_boundaries and not executable and not in_set and not insert
                and value_start is None and not awaiting_value
                and not (cursor > 0 and text[cursor - 1] == '$')):
            for tok in outside_pat.finditer(text, cursor):
                grp = tok.lastgroup
                if grp is None:
                    continue
                if grp == 'sym':
                    ch = text[tok.start()]
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        if depth:
                            depth -= 1
                    else:
                        depth = 0
                elif grp == 'punct':
                    if text[tok.end() - 1] == '$':
                        cursor = tok.end()
                        break
                elif grp == 'word':
                    word = tok.group().upper()
                    if word == 'SET':
                        in_set, depth, name_parts = True, 0, []
                        cursor = tok.end()
                        break
                    if word == 'INSERT' and depth == 0:
                        insert = True
                        cursor = tok.end()
                        break
                else:
                    # executable opener
                    executable = True
                    cursor = tok.end()
                    tail = version_tail_match(text, cursor)
                    if tail is not None:
                        cursor = tail.end()
                    break
            else:
                cursor = text_len
            continue

        limit = boundaries[boundary][0] if boundary < num_boundaries else text_len
        pat = exec_pat if executable else normal_pat
        switched = False
        for raw_token in pat.finditer(text, cursor, limit):
            raw_grp = raw_token.lastgroup
            if raw_grp == 'comment' or raw_grp == 'linecomment':
                continue
            tok_end = raw_token.end()
            if raw_grp == 'executable':
                executable = True
                cursor = tok_end
                version_tail_at = tok_end if text[tok_end - 1].isdigit() else None
                tail = version_tail_match(text, cursor, limit)
                if tail is not None:
                    cursor = tail.end()
                    # Only skip version_tail when collecting name_parts; otherwise
                    # process it as a normal digit word token (for secret values).
                    if not (version_tail_at == tail.start() and name_parts is not None):
                        version_tail_at = None
                        if awaiting_value:
                            value_start = tail.start()
                            awaiting_value = False
                        if value_start is not None:
                            value_end = cursor
                            value_can_end = True
                        if in_set and depth == 0 and name_parts is not None:
                            name_parts.append(tail.group())
                    else:
                        version_tail_at = None
                switched = True
                break
            if raw_grp == 'end':
                executable = False
                cursor = tok_end
                switched = True
                break
            if raw_grp == 'variable' and value_start is None and not awaiting_value:
                sub_iter = component_pattern.finditer(text, raw_token.start(), tok_end)
            else:
                sub_iter = (raw_token,)
            for token in sub_iter:
                grp = token.lastgroup
                if grp == 'comment' or grp == 'linecomment':
                    continue
                t_end = token.end()
                if version_tail_at is not None:
                    is_vtail = (token.start() == version_tail_at and grp == 'word'
                                and token.group().isdigit())
                    version_tail_at = None
                    if is_vtail and name_parts is not None:
                        continue
                if grp == 'word':
                    value = token.group()
                    word = value.upper()
                    restart = (word == 'SET' and in_set and depth == 0 and not awaiting_value
                               and (not name_parts or name_parts[-1].upper() != 'CHARACTER'))
                    if restart:
                        if name_parts is not None:
                            stuck_351 = True
                        in_set = False
                        insert = False
                    if depth == 0 and not in_set:
                        if word == 'INSERT':
                            insert = True
                        elif insert and word in insert_reset_words:
                            insert = False
                    if (value_start is not None and value_end is not None and depth == 0):
                        clause_end = False
                        if not restart and word in value_ends:
                            if not (t_end < text_len and (text[t_end].isalnum() or text[t_end] in ('_', '$'))):
                                if word != 'AS':
                                    clause_end = True
                                elif insert and row_alias_match(text, token.start()):
                                    clause_end = bool(
                                        (value_can_end and alias_value_fullmatch(text, value_start, value_end))
                                        or (pass1_redacted is not None
                                            and (value_start, value_end) in pass1_redacted))
                        if restart or clause_end:
                            body_ch = text[value_start:value_start + 1]
                            quote = body_ch if body_ch in ("'", '"') else ''
                            yield (value_start, value_end, quote, value_no_drop) if include_stage else (value_start, value_end, quote)
                            value_start, value_end = None, None
                            in_set, name_parts = False, None
                            insert = False
                    if awaiting_value:
                        value_start = token.start()
                        awaiting_value = False
                    if value_start is not None:
                        value_end = t_end
                        value_can_end = True
                    if word == 'SET' and not in_set:
                        in_set, depth, name_parts = True, 0, []
                    elif in_set and depth == 0 and name_parts is not None:
                        name_parts.append(value)
                elif grp == 'variable':
                    if awaiting_value:
                        value_start = token.start()
                        awaiting_value = False
                    if value_start is not None:
                        value_end = t_end
                        value_can_end = True
                elif grp == 'quoted':
                    if awaiting_value:
                        value_start = token.start()
                        awaiting_value = False
                    if value_start is not None:
                        value_end = t_end
                        value_can_end = True
                    if name_parts is not None:
                        value = token.group()
                        quote = value[0]
                        name_parts.append(value[1:-1].replace(quote * 2, quote)
                                          if quote in ('`', '"') else value)
                else:
                    value = token.group()
                    if awaiting_value:
                        value_start = token.start() if value not in (',', ';') else None
                        awaiting_value = False
                    if value_start is not None and (value not in (',', ';') or (value == ',' and depth > 0)):
                        value_end = t_end
                        value_can_end = value == ')'
                    if value == '(':
                        depth += 1
                    elif value == ')':
                        if depth:
                            depth -= 1
                    elif value == ';' or (value == ',' and depth == 0):
                        if value_start is not None:
                            body_ch = text[value_start:value_start + 1]
                            quote = body_ch if body_ch in ("'", '"') else ''
                            yield (value_start, value_end, quote, value_no_drop) if include_stage else (value_start, value_end, quote)
                            value_start, value_end = None, None
                        if value == ';':
                            stuck_351 = False
                            in_set, depth = False, 0
                            insert = False
                        name_parts = [] if in_set else None
                    elif in_set and depth == 0 and name_parts is not None:
                        if value in ('=', ':='):
                            if name_parts and name_parts[0].upper() in scopes:
                                name_parts = name_parts[1:]
                            if name_parts[:1] == ['@@']:
                                name_parts = name_parts[1:]
                                if (len(name_parts) >= 2 and name_parts[0].upper() in scopes
                                        and name_parts[1] == '.'):
                                    name_parts = name_parts[2:]
                            if ''.join(name_parts).lower() in secret_vars:
                                awaiting_value = True
                                value_no_drop = fail_closed_boundaries or stuck_351
                            stuck_351 = False
                            name_parts = None
                        else:
                            name_parts.append(value)
                    elif depth == 0 and value in ('=', ':='):
                        stuck_351 = False
            if (no_boundaries and not executable and not in_set and not insert
                    and value_start is None and not awaiting_value):
                cursor = tok_end
                switched = True
                break

        if not switched:
            if boundary < num_boundaries:
                b_start, b_end = boundaries[boundary]
                cursor = b_end
                boundary += 1
                executable = False
                version_tail_at = None
                if value_start is not None or awaiting_value:
                    keep_hiding = fail_closed_boundaries or depth > 0 or (b_end < text_len and (
                        text[b_end] not in _CLIENT_SPACE or
                        (value_start is not None and value_end is not None and
                         text[value_start:value_start + 2] in ("''", '""') and
                         value_end - value_start > 2)))
                    if not keep_hiding and b_end < tail_clean_from:
                        if delim_tail_search(text, b_end) is not None:
                            keep_hiding = True
                        else:
                            tail_clean_from = b_end
                    if keep_hiding:
                        start = b_start if value_start is None else value_start
                        quote = text[start] if value_start is not None and text[start:start + 1] in ("'", '"') else ''
                        yield (start, text_len, quote, value_no_drop) if include_stage else (start, text_len, quote)
                        return
                if awaiting_value:
                    value_start = None
                    awaiting_value = False
                if value_start is not None:
                    body_ch = text[value_start:value_start + 1]
                    quote = body_ch if body_ch in ("'", '"') else ''
                    yield (value_start, value_end, quote, value_no_drop) if include_stage else (value_start, value_end, quote)
                    value_start, value_end = None, None
                if in_set and name_parts is not None:
                    stuck_351 = True
                in_set, depth = False, 0
                insert = False
                name_parts = None
            else:
                break

    if value_start is not None:
        body_ch = text[value_start:value_start + 1]
        quote = body_ch if body_ch in ("'", '"') else ''
        yield (value_start, value_end, quote, value_no_drop) if include_stage else (value_start, value_end, quote)


_QUOTE_CLOSE = {
    "'": re.compile(r"[^'\\]*(?:(?:''|\\[\s\S])[^'\\]*)*(?:'|\\?\Z)"),
    '"': re.compile(r'[^"\\]*(?:(?:""|\\[\s\S])[^"\\]*)*(?:"|\\?\Z)'),
}
_QUOTE_CLOSE_NO_BACKSLASH = {
    "'": re.compile(r"[^']*(?:''[^']*)*(?:'|\Z)"),
    '"': re.compile(r'[^"]*(?:""[^"]*)*(?:"|\Z)'),
}
_TRACKER_SKIP = (
    r"|`[^`]*(?:``[^`]*)*(?P<bt_end>`|\Z)"
    r"|/\*(?!!|[mM]!)[\s\S]*?(?P<blk_end>\*/|\Z)"
    r"|(?P<line>--(?=\s|\Z)[^\r\n]*|\#[^\r\n]*)")
_TRACKER_CONTEXT = re.compile(_SECRET_LITERAL_CONTEXT_STRING + _TRACKER_SKIP)
_TRACKER_CONTEXT_NO_BACKSLASH = re.compile(
    _SECRET_LITERAL_CONTEXT_STRING_NO_BACKSLASH + _TRACKER_SKIP)
_TRACKER_CONT = {
    'single_end': (re.compile(r"[^'\\]*(?:(?:''|\\[\s\S])[^'\\]*)*(?P<single_end>'|\\?\Z)"),
                   re.compile(r"[^']*(?:''[^']*)*(?P<single_end>'|\Z)")),
    'double_end': (re.compile(r'[^"\\]*(?:(?:""|\\[\s\S])[^"\\]*)*(?P<double_end>"|\\?\Z)'),
                   re.compile(r'[^"]*(?:""[^"]*)*(?P<double_end>"|\Z)')),
    'bt_end': (re.compile(r"[^`]*(?:``[^`]*)*(?P<bt_end>`|\Z)"),
               re.compile(r"[^`]*(?:``[^`]*)*(?P<bt_end>`|\Z)")),
    'blk_end': (re.compile(r"[\s\S]*?(?P<blk_end>\*/|\Z)"),
                re.compile(r"[\s\S]*?(?P<blk_end>\*/|\Z)")),
    'line': (re.compile(r"(?P<line>[^\r\n]*)"),
             re.compile(r"(?P<line>[^\r\n]*)")),
}
_PLAIN_UNQUOTED_WORD = re.compile(r"[A-Za-z0-9_$]+")
_REDACTED_BY_QUOTE = {"'": "'" + REDACTED + "'", '"': '"' + REDACTED + '"', '': REDACTED}
# 3.5.1 sequential redaction stage order:
# 0: _redact_set_password_all, 1: _redact_secret_variables,
# 2..8: _SECRET_PATTERNS[0..6] (_REPLACE_FOLLOW matches run at stage 8).
# _NO_DROP_FLAG marks 3.5.2-only stage-1 spans (prefix_boundaries or after
# assignment-free SET resets) so overlaps on their closing quote fail closed
# while still keeping stage 1 when they overlap a later-stage span.
_REPLACE_STAGE = 8
_NO_DROP_FLAG = 1 << 8
_SECRET_VALUE_INTRODUCER = re.compile(r"(?:_[\w]+(?:\s+[XBN])?\s*|[XBN]\s*)", re.I)
_AFTER_LITERAL_WS = re.compile(r"[ \t\r\n]*")
# Valid SQL continuation immediately after the closing quote of a balanced
# literal (used to distinguish a real literal terminator from a cut literal
# that stole the opening quote of a following secret).
_VALID_AFTER_LITERAL = re.compile(
    r"[ \t\r\n]*(?:;|,|\)|\b(?:"
    r"WHERE|ORDER|LIMIT|ON|AS|FOR|AND|OR|REQUIRE|WITH|REPLACE|RETAIN|"
    r"PASSWORD|ACCOUNT|COMMENT|ATTRIBUTE|FAILED_LOGIN_ATTEMPTS|"
    r"PASSWORD_LOCK_TIME|DISCARD\s+OLD\s+PASSWORD|"
    r"ADD\s+[23]\s+FACTOR|MODIFY\s+[23]\s+FACTOR|"
    r"DEFAULT_AUTH|PLUGIN_DIR|DATA\s+DIRECTORY|INDEX\s+DIRECTORY|"
    r"ENGINE|SECONDARY_ENGINE|MAX_ROWS|MIN_ROWS|AVG_ROW_LENGTH|"
    r"COMPRESSION|ENCRYPTION|AUTO_INCREMENT|PACK_KEYS|"
    r"STATS_AUTO_RECALC|STATS_PERSISTENT|STATS_SAMPLE_PAGES|"
    r"CHECKSUM|TABLE_CHECKSUM|PAGE_CHECKSUM|DELAY_KEY_WRITE|ROW_FORMAT|"
    r"UNION|CHARACTER\s+SET|CHARSET|INSERT_METHOD|TABLESPACE|STORAGE|"
    r"CONNECTION|KEY_BLOCK_SIZE|START\s+TRANSACTION|"
    r"ENGINE_ATTRIBUTE|SECONDARY_ENGINE_ATTRIBUTE|AUTOEXTEND_SIZE|"
    r"FILE_FORMAT|FILES|ALLOW_MISSING_FILES|VERIFY_KEY_CONSTRAINTS|"
    r"STRICT_LOAD|AUTO_REFRESH|AUTO_REFRESH_SOURCE|"
    r"TRANSACTIONAL|SEQUENCE|PARTITION\s+BY|"
    r"DEFAULT|RETURNING|USING|COLLATE|END"
    r")\b|/\*|\*/|--(?=\s|\Z)|#|\Z)",
    re.I)


def _classify_tracker_token(text, text_len, s_start, s_end, grp, end_tok, limit):
    if grp in ('single_end', 'double_end'):
        quote = text[s_start]
        if end_tok == quote:
            if s_end == limit and limit < text_len and text[limit] == quote:
                return '', grp, limit - 1
            return quote, None, s_end
        if s_end == limit and limit < text_len:
            return '', grp, (limit - 1 if end_tok == '\\' else limit)
        return '', None, s_end
    if grp == 'bt_end':
        if end_tok == '`':
            if s_end == limit and limit < text_len and text[limit] == '`':
                return '', grp, limit - 1
            return '', None, s_end
        if s_end == limit and limit < text_len:
            return '', grp, limit
        return '', None, s_end
    if grp == 'blk_end':
        if end_tok == '*/':
            return '', None, s_end
        if s_end == limit and limit < text_len:
            return '', grp, (limit - 1 if limit - s_start > 2 and text[limit - 1] == '*' else limit)
        return '', None, s_end
    if s_end == limit and limit < text_len and text[limit] not in '\r\n':
        return '', grp, limit
    return '', None, s_end


def _advance_tracker(tracker, text, text_len, start, limit):
    """Advance a bounded per-group context tracker through *start* within *limit*."""
    pat, is_bs, s_start, s_end, s_quote, trunc_grp, trunc_pos = tracker
    if trunc_grp is not None and s_end <= start:
        cont_pat = _TRACKER_CONT[trunc_grp][0 if is_bs else 1]
        token = cont_pat.match(text, trunc_pos, limit)
        s_end = token.end()
        s_quote, trunc_grp, trunc_pos = _classify_tracker_token(
            text, text_len, s_start, s_end, trunc_grp, token.group(trunc_grp), limit)
    while s_end <= start:
        token = pat.search(text, s_end, limit)
        if token is None:
            s_start = s_end = limit
            s_quote = ''
            trunc_grp = None
            break
        s_start, s_end = token.start(), token.end()
        grp = token.lastgroup
        if s_end == limit and limit < text_len:
            if (grp == 'blk_end' and s_end - s_start <= 3 and
                    (text.startswith('/*!', s_start) or
                     text.startswith('/*m!', s_start) or
                     text.startswith('/*M!', s_start))):
                s_end = s_start + 2
                s_quote = ''
                trunc_grp = None
                continue
            if (grp == 'line' and text.startswith('--', s_start) and
                    s_end - s_start == 2 and not text[limit].isspace()):
                s_end = s_start + 2
                s_quote = ''
                trunc_grp = None
                continue
        s_quote, trunc_grp, trunc_pos = _classify_tracker_token(
            text, text_len, s_start, s_end, grp, token.group(grp), limit)
    tracker[2] = s_start
    tracker[3] = s_end
    tracker[4] = s_quote
    tracker[5] = trunc_grp
    tracker[6] = trunc_pos
    return s_start, s_end, s_quote


def _balanced_strings(text, backslash_escapes):
    """Return closed top-level string index if all strings in *text* are closed."""
    pat = _TRACKER_CONTEXT if backslash_escapes else _TRACKER_CONTEXT_NO_BACKSLASH
    closed = set()
    outer_starts = []
    outer_spans = []
    for token in pat.finditer(text):
        grp = token.lastgroup
        if grp in ('single_end', 'double_end'):
            start, end = token.start(), token.end()
            quote = text[start]
            if token.group(grp) != quote:
                return None
            closed.add((start, end))
            outer_starts.append(start)
            outer_spans.append((start, end, quote))
    return closed, outer_starts, outer_spans


def _is_balanced_span(balance, start, end, quote):
    """Return 1 for a closed top-level string, 2 if nested inside one (and at its
    closing boundary when end == o_end - 1), else 0."""
    if balance is None:
        return 0
    closed, outer_starts, outer_spans = balance
    if (start, end) in closed:
        return 1
    at = bisect.bisect_right(outer_starts, start) - 1
    if at >= 0:
        o_start, o_end, o_quote = outer_spans[at]
        if o_start < start and end < o_end and o_quote != quote:
            return 2 if end == o_end - 1 else 1
    return 0


def _extend_swallowed_quote(text, pos, quote, has_backslash):
    """Extend *pos* to the closing *quote* under both backslash readings."""
    end = _QUOTE_CLOSE[quote].match(text, pos).end()
    if has_backslash:
        end = max(end, _QUOTE_CLOSE_NO_BACKSLASH[quote].match(text, pos).end())
    return end


_STRUCTURAL_IN_LITERAL = re.compile(r"""['"`\\=;,()\#\r\n]|--|/\*|\*/""")


def _remap_custom_boundaries(text, applied_spans, boundaries):
    """Shift Pass-1 proven custom boundaries into redacted *text*, or drop all
    if a replacement touched a boundary or broke quote/comment closure across one."""
    if not boundaries:
        return ()
    remapped = []
    span_idx = 0
    num_spans = len(applied_spans)
    delta = 0
    for b_start, b_end in boundaries:
        while span_idx < num_spans and applied_spans[span_idx][1] <= b_start:
            s_start, s_end, rep_len = applied_spans[span_idx]
            delta += rep_len - (s_end - s_start)
            span_idx += 1
        if span_idx < num_spans and applied_spans[span_idx][0] < b_end:
            return ()
        remapped.append((b_start + delta, b_end + delta))
    modes = ((_TRACKER_CONTEXT, _TRACKER_CONTEXT_NO_BACKSLASH)
             if '\\' in text else (_TRACKER_CONTEXT,))
    for pat in modes:
        b_idx = 0
        num_b = len(remapped)
        for token in pat.finditer(text):
            t_start, t_end = token.start(), token.end()
            while b_idx < num_b and remapped[b_idx][0] <= t_start:
                b_idx += 1
            if b_idx < num_b and remapped[b_idx][0] < t_end:
                return ()
    return remapped


def _redact_spans_pass(text, spans, boundaries=None, set_password_possible=False,
                       allow_custom_boundaries=False):
    """Replace ordered spans, failing closed on mis-paired overlapping quotes.

    Returns ``(redacted_text, any_overlap, needs_followup, remapped, unquoted_redacted)``,
    where *any_overlap* is True when a replacement merged non-identical
    overlapping spans or failed closed to EOF, *needs_followup* is True when
    any replacement could alter SQL quote, comment, separator, or length
    structure for a follow-up pass, *remapped* shifts proven Pass-1 custom
    boundaries into *redacted_text* when still closed, and *unquoted_redacted*
    records Pass-1 unquoted ``[redacted]`` offsets for INSERT row-alias checks.
    """
    has_backslash = '\\' in text
    text_len = len(text)
    # Each group: [start, end, quote, end_quote, swallowed_quotes_or_None, overlapped, fail_eof]
    groups = []
    last_end = -1
    trackers = None
    g_first_stage = 0
    g_first_end = 0
    g_first_bs = True
    g_stages = None
    g_bs_stages = None
    g_no_drop_end = -1
    any_swallowed = False
    any_fail_eof = False
    had_stage01 = False
    balance_cache = {}
    boundary_starts = None
    for start, end, quote, tag in spans:
        no_drop = bool(tag & _NO_DROP_FLAG)
        base_tag = tag & ~_NO_DROP_FLAG
        stage = base_tag >> 1
        if stage <= 1:
            had_stage01 = True
        bs_mode = bool(base_tag & 1)
        if start < last_end:
            group = groups[-1]
            old_start, old_end, old_quote, end_quote, swallowed, _, fail_eof = group
            if g_stages is None:
                g_stages = {g_first_stage: g_first_end}
                g_bs_stages = {g_first_stage: g_first_end} if g_first_bs else {}
            if start > old_start:
                if not fail_eof and quote in ("'", '"') and text[start] == quote:
                    if trackers is None:
                        modes = ((_TRACKER_CONTEXT, True), (_TRACKER_CONTEXT_NO_BACKSLASH, False)) if has_backslash else ((_TRACKER_CONTEXT, True),)
                        trackers = [[pat, is_bs, old_start, old_start, '', None, old_start]
                                    for pat, is_bs in modes]
                    closing_starts = []
                    all_closing = True
                    any_closing = False
                    for tracker in trackers:
                        s_start, s_end, s_quote = _advance_tracker(
                            tracker, text, text_len, start, old_end)
                        if s_quote == quote and s_start < start and start == s_end - 1:
                            any_closing = True
                            closing_starts.append(s_start)
                        else:
                            all_closing = False
                            closing_starts.append(None)
                    if any_closing:
                        earlier_stage = min(s for s, e in g_stages.items() if e > start)
                        earlier_bs_stage = (
                            min((s for s, e in g_bs_stages.items() if e > start), default=None)
                            if has_backslash else earlier_stage)
                        if stage < earlier_stage or (
                                earlier_bs_stage is not None and stage < earlier_bs_stage) or (
                                start < g_no_drop_end):
                            group[6] = True
                            any_fail_eof = True
                        elif all_closing and earlier_bs_stage is not None and not (
                                earlier_stage == 0 and stage == 0) and not (
                                earlier_stage == 0 and stage == 5) and (
                                closing_starts[0] == old_start
                                or (earlier_bs_stage == 1 and (
                                    start < old_end - 1
                                    or (closing_starts[0] > old_start
                                        and _SECRET_VALUE_INTRODUCER.fullmatch(
                                            text, old_start, closing_starts[0]) is not None)))):
                            if True not in balance_cache:
                                balance_cache[True] = _balanced_strings(text, True)
                            bal_kind = _is_balanced_span(
                                balance_cache[True], closing_starts[0], start + 1, quote)
                            if not bal_kind and has_backslash and balance_cache[True] is None:
                                if False not in balance_cache:
                                    balance_cache[False] = _balanced_strings(text, False)
                                bal_kind = _is_balanced_span(
                                    balance_cache[False], closing_starts[1], start + 1, quote)
                            valid_after = bal_kind and (
                                bal_kind == 2
                                or _VALID_AFTER_LITERAL.match(text, start + 1) is not None)
                            if bal_kind == 1 and not valid_after:
                                if boundary_starts is None:
                                    if boundaries is None and allow_custom_boundaries:
                                        boundaries = _secret_variable_boundary_plan(text)[0]
                                    boundary_starts = frozenset(
                                        b_start for b_start, _ in (boundaries or ()))
                                if boundary_starts:
                                    after_ws = _AFTER_LITERAL_WS.match(text, start + 1).end()
                                    valid_after = after_ws in boundary_starts
                            if valid_after:
                                # A later (or equal) 3.5.1 stage starting on the real
                                # closing quote of a balanced plain or introduced literal
                                # followed by a valid SQL continuation or proven custom
                                # delimiter was already removed before that stage ran in
                                # 3.5.1: drop it so valid SQL stays intact.
                                continue
                if start != old_start or end != old_end:
                    group[5] = True
                if not group[6] and end <= old_end:
                    # An earlier 3.5.1 stage strictly nested inside a later-stage
                    # span ran before the outer span in 3.5.1 and could remove
                    # separators or quotes. Fail closed when the quotes are not
                    # cleanly balanced.
                    earlier_stage = min(s for s, e in g_stages.items() if e > start)
                    earlier_bs_stage = (
                        min((s for s, e in g_bs_stages.items() if e > start), default=None)
                        if has_backslash else earlier_stage)
                    if stage < earlier_stage or (
                            earlier_bs_stage is not None and stage < earlier_bs_stage):
                        if True not in balance_cache:
                            balance_cache[True] = _balanced_strings(text, True)
                        old_is_lit = old_quote in ("'", '"') and text[old_start] == old_quote
                        ok = (quote in ("'", '"')
                              and _is_balanced_span(balance_cache[True], start, end, quote)
                              and (not old_is_lit
                                   or _is_balanced_span(balance_cache[True], old_start, old_end, old_quote)))
                        if not ok and has_backslash:
                            if False not in balance_cache:
                                balance_cache[False] = _balanced_strings(text, False)
                            ok = (quote in ("'", '"')
                                  and _is_balanced_span(balance_cache[False], start, end, quote)
                                  and (not old_is_lit
                                       or _is_balanced_span(balance_cache[False], old_start, old_end, old_quote)))
                        if not ok:
                            group[6] = True
                            any_fail_eof = True
                if end > old_end:
                    # The later span swallowed the earlier span's closing quote.
                    if end_quote in ("'", '"'):
                        if (not fail_eof and quote in ("'", '"') and quote != end_quote
                                and text[start] == quote):
                            earlier_stage = min(s for s, e in g_stages.items() if e > start)
                            earlier_bs_stage = (
                                min((s for s, e in g_bs_stages.items() if e > start), default=None)
                                if has_backslash else earlier_stage)
                            if stage < earlier_stage or (
                                    earlier_bs_stage is not None and stage < earlier_bs_stage):
                                group[6] = True
                                any_fail_eof = True
                        if swallowed is None:
                            swallowed = group[4] = set()
                        swallowed.add(end_quote)
                        any_swallowed = True
                    group[1] = end
                    group[3] = quote
                    last_end = end
            else:
                if start != old_start or end != old_end:
                    group[5] = True
                if end > old_end:
                    group[1] = end
                    group[3] = quote
                    last_end = end
                # An enclosing-string interpretation can omit a quote that the
                # standalone-literal interpretation would otherwise insert.
                group[2] = min(old_quote, quote)
            if end > g_stages.get(stage, -1):
                g_stages[stage] = end
            if bs_mode and end > g_bs_stages.get(stage, -1):
                g_bs_stages[stage] = end
            if no_drop and end > g_no_drop_end:
                g_no_drop_end = end
            continue
        groups.append([start, end, quote, quote, None, False, False])
        last_end = end
        trackers = None
        g_first_stage = stage
        g_first_end = end
        g_first_bs = bs_mode
        g_stages = None
        g_bs_stages = None
        g_no_drop_end = end if no_drop else -1

    if any_swallowed or any_fail_eof:
        merged = []
        read_idx = 0
        num_groups = len(groups)
        while read_idx < num_groups:
            group = groups[read_idx]
            read_idx += 1
            while True:
                if group[6]:
                    group[1] = text_len
                    read_idx = num_groups
                    break
                if group[4]:
                    pending_quotes = list(group[4])
                    group[4].clear()
                    extended = []
                    new_end = group[1]
                    new_quote = group[3]
                    for quote in pending_quotes:
                        if quote in ("'", '"'):
                            cand = _extend_swallowed_quote(
                                text, group[1], quote, has_backslash)
                            extended.append((quote, cand))
                            if cand > new_end:
                                new_end = cand
                                new_quote = quote
                    if new_end > group[1]:
                        group[1] = new_end
                        group[3] = new_quote
                        group[5] = True
                        if new_end < text_len:
                            for quote, cand in extended:
                                if cand < new_end:
                                    group[4].add(quote)
                        while read_idx < num_groups and groups[read_idx][0] < group[1]:
                            nxt = groups[read_idx]
                            read_idx += 1
                            if nxt[6]:
                                group[6] = True
                            if nxt[1] > group[1]:
                                if group[3] in ("'", '"'):
                                    group[4].add(group[3])
                                group[1] = nxt[1]
                                group[3] = nxt[3]
                            if nxt[4]:
                                group[4].update(nxt[4])
                            group[5] = True
                        continue
                break
            merged.append(group)
        groups = merged

    parts, kept, any_overlap, removed_semi, needs_followup = [], 0, False, False, False
    has_semi = ';' in text
    has_boundaries = bool(boundaries)
    redacted_len = len(REDACTED)
    redacted_quoted_len = redacted_len + 2
    structural_search = _STRUCTURAL_IN_LITERAL.search
    plain_word_fullmatch = _PLAIN_UNQUOTED_WORD.fullmatch
    redacted_by_quote = _REDACTED_BY_QUOTE
    parts_append = parts.append
    for start, end, quote, _, _, overlapped, fail_eof in groups:
        if start < kept:
            kept = max(kept, end)
            continue
        if overlapped or fail_eof:
            any_overlap = True
            needs_followup = True
        if has_semi and not removed_semi and ';' in text[start:end]:
            removed_semi = True
            needs_followup = True
        if not needs_followup:
            if quote in ("'", '"'):
                if (end - start < 2 or text[start] != quote or text[end - 1] != quote
                        or (set_password_possible and end - start > redacted_quoted_len)
                        or structural_search(text, start + 1, end - 1) is not None):
                    needs_followup = True
            elif (has_boundaries
                  or (set_password_possible and end - start > redacted_len)
                  or plain_word_fullmatch(text, start, end) is None
                  or (end < text_len and text[end] not in ' \t\r\n;,)')):
                needs_followup = True
        parts_append(text[kept:start])
        parts_append(redacted_by_quote[quote] if quote in redacted_by_quote else quote + REDACTED + quote)
        kept = end
    redacted = ''.join(parts) + text[kept:] if parts else text
    remapped = ()
    unquoted_redacted = None
    if needs_followup and parts:
        applied = [] if (boundaries and allow_custom_boundaries and not any_overlap and not removed_semi) else None
        unquoted = set()
        kept_pos = 0
        delta = 0
        for start, end, quote, _, _, _, _ in groups:
            if start < kept_pos:
                kept_pos = max(kept_pos, end)
                continue
            rep_len = redacted_len + 2 * len(quote)
            if not quote:
                unquoted.add((start + delta, start + delta + rep_len))
            if applied is not None:
                applied.append((start, end, rep_len))
            delta += rep_len - (end - start)
            kept_pos = end
        if unquoted:
            unquoted_redacted = unquoted
        if applied:
            remapped = _remap_custom_boundaries(redacted, applied, boundaries)
    return redacted, any_overlap, needs_followup, remapped, unquoted_redacted, had_stage01


def _redact_spans(text, spans):
    """Replace ordered spans, hiding the union before changing any offsets."""
    return _redact_spans_pass(text, ((s, e, q, 1) for s, e, q in spans))[0]


def _redact_secret_variables(text):
    """Hide SET secrets under both possible NO_BACKSLASH_ESCAPES readings.

    Both scans read the original text: a substitution in one mode must not
    remove an assignment or change the quote boundaries seen by the other.
    Inputs without backslashes need just the original linear scan.
    """
    spans = _secret_variable_spans(text)
    if '\\' in text:
        spans = heapq.merge(spans, _secret_variable_spans(text, False))
    return _redact_spans(text, spans)


def _redact_literal(match):
    literal = match.group(2)
    quote = literal[0]
    return match.group(1) + quote + REDACTED + quote


def _secret_literal_contexts(text, backslash_escapes=True):
    """Yield enclosing single/double-quoted string matches in order.

    Block comments and backtick identifiers bound their own quotes so quotes
    inside them never open false strings across later SQL. Line comments bound
    prose apostrophes (``it's``, ``don't``) and closed strings to the line,
    while allowing an unclosed standalone quote opener at line end to span
    into a following line so multiline dynamic SQL after a stray ``--`` or
    ``#`` still keeps its enclosing-string boundary. Executable comments
    remain transparent.
    """
    plain = (_SECRET_LITERAL_CONTEXT_PLAIN if backslash_escapes
             else _SECRET_LITERAL_CONTEXT_PLAIN_NO_BACKSLASH)
    if '`' not in text and '/*' not in text and '--' not in text and '#' not in text:
        for token in plain.finditer(text):
            yield token
        return
    context = (_SECRET_LITERAL_CONTEXT if backslash_escapes
               else _SECRET_LITERAL_CONTEXT_NO_BACKSLASH)
    text_len = len(text)
    cursor = 0
    while cursor < text_len:
        extended_end = None
        for token in context.finditer(text, cursor):
            group = token.lastgroup
            if group in ('single_end', 'double_end'):
                yield token
                continue
            start, end = token.span()
            if group == 'block':
                body_start = start + 2
                body_end = (end - 2 if end - start >= 4
                            and text.endswith('*/', body_start, end) else end)
            elif group == 'line':
                body_start = start + (2 if text.startswith('--', start) else 1)
                body_end = end
            else:
                body_start = start + 1
                body_end = end - 1 if end - start >= 2 and text[end - 1] == '`' else end
            if (text.find("'", body_start, body_end) == -1
                    and text.find('"', body_start, body_end) == -1):
                continue
            for inner in plain.finditer(text, body_start, body_end):
                if group == 'line' and inner.group(inner.lastgroup) != text[inner.start()]:
                    prev = text[inner.start() - 1] if inner.start() > body_start else ''
                    if not (text[inner.start()] == "'" and (prev.isalnum() or prev == '_')):
                        extended = plain.match(text, inner.start())
                        if extended is not None:
                            yield extended
                            extended_end = extended.end()
                            break
                yield inner
            if extended_end is not None:
                cursor = extended_end
                break
        else:
            return


def _secret_literal_context_spans(text, backslash_escapes=True):
    """Return parallel ``(starts, ends, closed)`` lists for enclosing strings."""
    starts, ends, closed = [], [], []
    s_app, e_app, c_app = starts.append, ends.append, closed.append
    if '`' not in text and '/*' not in text and '--' not in text and '#' not in text:
        plain = (_SECRET_LITERAL_CONTEXT_PLAIN if backslash_escapes
                 else _SECRET_LITERAL_CONTEXT_PLAIN_NO_BACKSLASH)
        items = plain.finditer(text)
    else:
        items = _secret_literal_contexts(text, backslash_escapes)
    for item in items:
        s, e = item.span()
        s_app(s)
        e_app(e)
        c_app(item.group(item.lastgroup) == text[s])
    return starts, ends, closed


_INVENTORY_SCOPE_SUFFIXES = (
    ('persist_only', 12),
    ('persist', 7),
    ('session', 7),
    ('global', 6),
    ('local', 5),
)


def _make_inventory_literal_search(pattern, text, folded):
    """Anchor-guided search for ASCII _SECRET_VARIABLE_LITERAL scans."""
    text_len = len(text)
    has_otel = 'otel_exporter_otlp_' in folded
    anchors = []
    for anchor in _SECRET_VARIABLE_LITERAL_ANCHORS:
        if anchor == 'telemetry' and not has_otel:
            continue
        idx = folded.find(anchor)
        if idx >= 0:
            anchors.append([idx, anchor, len(anchor)])
    if not anchors:
        return None
    pat_match = pattern.match
    first_chars = _SECRET_VALUE_FIRST_CHARS
    scope_suffixes = _INVENTORY_SCOPE_SUFFIXES
    folded_find = folded.find

    def search(_text, pos):
        while anchors:
            best_i = -1
            best_idx = text_len
            i = 0
            while i < len(anchors):
                entry = anchors[i]
                idx = entry[0]
                if idx < pos:
                    idx = folded_find(entry[1], pos)
                    if idx < 0:
                        anchors.pop(i)
                        continue
                    entry[0] = idx
                if idx < best_idx:
                    best_idx = idx
                    best_i = i
                i += 1
            if best_i < 0:
                return None
            entry = anchors[best_i]
            idx, anchor, alen = entry[0], entry[1], entry[2]
            next_idx = folded_find(anchor, idx + 1)
            if next_idx < 0:
                anchors.pop(best_i)
            else:
                entry[0] = next_idx
            # Quick-reject non-telemetry anchors whose tail cannot start
            # an assignment to a _SECRET_VALUE literal.
            if anchor != 'telemetry':
                p = idx + alen
                if anchor == 'handlersocket_plain_secret' and folded.startswith('_wr', p):
                    p += 3
                if p < text_len and text[p] in ('`', '"'):
                    p += 1
                while p < text_len and text[p].isspace():
                    p += 1
                if p < text_len and text[p] == ':':
                    p += 1
                if p >= text_len or text[p] != '=':
                    continue
                p += 1
                while p < text_len and text[p].isspace():
                    p += 1
                if p >= text_len or text[p] not in first_chars:
                    continue
            # Collect all valid prefix start offsets >= pos in ascending order.
            q_start = idx - 1 if idx > pos and text[idx - 1] in ('`', '"') else idx
            cands = []
            w1 = q_start
            while w1 > pos and text[w1 - 1].isspace():
                w1 -= 1
            if w1 >= pos + 2 and text[w1 - 2:w1] == '@@':
                cands.append(w1 - 2)
            elif w1 > pos and text[w1 - 1] == '.':
                w2 = w1 - 1
                while w2 > pos and text[w2 - 1].isspace():
                    w2 -= 1
                for scope, slen in scope_suffixes:
                    s_start = w2 - slen
                    if s_start >= pos and folded[s_start:w2] == scope:
                        w3 = s_start
                        while w3 > pos and text[w3 - 1].isspace():
                            w3 -= 1
                        if w3 >= pos + 2 and text[w3 - 2:w3] == '@@':
                            cands.append(w3 - 2)
                        cands.append(s_start)
                        break
            if q_start < idx:
                cands.append(q_start)
            cands.append(idx)
            for cand in cands:
                m = pat_match(text, cand)
                if m is not None:
                    return m
        return None

    return search


def _secret_literal_spans(text, pattern, backslash_escapes=True, stage=None, folded=None):
    """Extend ambiguous dynamic values through their original enclosing string.

    Ordered scans preserve the diagnostic fallback's entire hidden span.
    The sole empty-fragment exception is a SET @q = CONCAT('name = ',
    QUOTE(@value)) statement: its closing quote is not a value opener. An
    arbitrary diagnostic must still take the conservative fallback.
    """
    follow_pattern = _REPLACE_FOLLOW if backslash_escapes else _REPLACE_FOLLOW_NO_BACKSLASH
    inventory_pattern = (pattern is _SECRET_VARIABLE_LITERAL or pattern is
                         _SECRET_PATTERNS_NO_BACKSLASH.get(_SECRET_VARIABLE_LITERAL))
    if inventory_pattern and folded is not None:
        pattern_search = _make_inventory_literal_search(pattern, text, folded)
        if pattern_search is None:
            return iter(())
    else:
        pattern_search = pattern.search
    ctx_starts = ctx_ends = ctx_closed = None
    ctx_len = ctx_idx = 0
    prev_mstart = -1
    found = []
    followed = set()
    bisect_right = bisect.bisect_right
    follow_match = follow_pattern.match
    text_len = len(text)
    bs_bit = 1 if backslash_escapes else 0
    base_tag = None if stage is None else ((stage << 1) | bs_bit)
    forced_tag = (_REPLACE_STAGE << 1) | bs_bit
    cursor, forced = 0, None
    while cursor < text_len:
        was_forced = forced is not None
        match, forced = forced or pattern_search(text, cursor), None
        if match is None:
            break
        start, end = match.start("secret"), match.end()
        if ctx_ends is None:
            ctx_starts, ctx_ends, ctx_closed = _secret_literal_context_spans(
                text, backslash_escapes)
            ctx_len = len(ctx_ends)
        m_start = match.start()
        if m_start >= prev_mstart and ctx_idx < ctx_len:
            if ctx_ends[ctx_idx] <= m_start:
                ctx_idx += 1
                if ctx_idx < ctx_len and ctx_ends[ctx_idx] <= m_start:
                    ctx_idx = bisect_right(ctx_ends, m_start, ctx_idx)
            at = ctx_idx
        else:
            at = bisect_right(ctx_ends, m_start)
            ctx_idx = at
        prev_mstart = m_start
        ch = text[start]
        if ch == "'" or ch == '"':
            escaped, quote = False, ch
        else:
            opener = _SECRET_VALUE_OPENER.match(text, start, end)
            escaped = opener is not None and bool(opener.group(1))
            quote = '' if escaped else opener.group(2) if opener is not None else "'"
        if at < ctx_len and ctx_starts[at] < m_start and start < ctx_ends[at]:
            e_start = ctx_starts[at]
            e_end = ctx_ends[at]
            outer_quote = text[e_start]
            closed = ctx_closed[at]
            limit = e_end - int(closed)
            if (start == limit and closed
                    and _SECRET_CONCAT_PREFIX.search(text, ctx_ends[at - 1] if at else 0, e_start)
                    and _SECRET_CONCAT_QUOTE.match(text, e_end)):
                # Resume at the real close, not the fallback match's end:
                # that match may have swallowed a later real assignment.
                if not was_forced:
                    cursor = e_end
                continue
            # Different inner/outer quotes have an unambiguous value end.
            # Keep diagnostic punctuation after it, while preserving the
            # inventory fallback's older conservative enclosing-string rule.
            if start < limit and (inventory_pattern or escaped or quote == outer_quote):
                end = max(end, limit)
                if quote == outer_quote:
                    quote = ''
        if escaped and end == match.end():
            # An escaped opener without a known enclosing string has no
            # trustworthy end. Never leave its trailing value visible.
            end = text_len
        if base_tag is None:
            found.append((start, end, quote))
        else:
            found.append((start, end, quote, forced_tag if was_forced else base_tag))
        m_end = match.end()
        if end < text_len and m_end not in followed:
            # Capture a following REPLACE literal in the original text before
            # merged spans from another quote reading can cover its keyword,
            # and walk each follow position only once so keywords re-found
            # inside an already followed literal cannot restart a chain.
            followed.add(m_end)
            forced = follow_match(text, m_end)
        if not was_forced:
            # Resume just inside the matched value so a keyword swallowed by an
            # unterminated or mis-framed literal is still scanned; spans are
            # sorted and unioned before rewriting.
            cursor = start + 1
    found.sort()
    return iter(found)


def _secret_literal_readings(text, pattern, stage=None, folded=None):
    """Credential spans under both modes, before either reading changes text.

    This also applies to account/replication patterns, preserving the enclosing
    literal boundary for doubled and backslash-escaped dynamic SQL values.
    """
    spans = _secret_literal_spans(text, pattern, True, stage, folded=folded)
    if '\\' in text:
        spans = heapq.merge(spans, _secret_literal_spans(
            text, _SECRET_PATTERNS_NO_BACKSLASH[pattern], False, stage, folded=folded))
    return spans


def _redact_secret_variable_literals(text, pattern, folded=None):
    return _redact_spans_pass(text, _secret_literal_readings(text, pattern, _REPLACE_STAGE, folded=folded))[0]


def _redact_variable_errors(text, folded=None):
    """Hide rejected secrets without consuming another variable's diagnostic.

    Raw values can contain quotes or newlines. Use the last closing quote
    before the next diagnostic, and hide an unfinished value through its end.
    Each search visits a disjoint interval, keeping long diagnostics linear.
    """
    if folded is None or ('variable' in folded and "can't" in folded):
        messages = list(_WRONG_VALUE_FOR_VAR.finditer(text))
        parts, kept = [], 0
        for index, message in enumerate(messages):
            if not sensitive(message.group(1)):
                continue
            limit = messages[index + 1].start() if index + 1 < len(messages) else len(text)
            end = text.rfind("'", message.end(), limit)
            if end < 0:
                end = limit
            parts.extend((text[kept:message.end()], REDACTED))
            kept = end
        if parts:
            text = ''.join(parts) + text[kept:]
            if folded is not None:
                folded = text.lower() if text.isascii() else None
    # The English fallback also handles diagnostics without an error code.
    # Numbered messages cannot depend on lc_messages or the word "Variable".
    # Values are printed verbatim, not SQL-escaped: hide from the first value
    # opener through the last quote, including embedded quotes and newlines.
    # Stop at the next client diagnostic, regardless of its error number.
    if folded is None or ('1231' in folded and 'error' in folded):
        messages = list(_CLIENT_ERROR_CODE.finditer(text))
        parts, kept = [], 0
        for index, message in enumerate(messages):
            if message.group(1) != '1231':
                continue
            limit = messages[index + 1].start() if index + 1 < len(messages) else len(text)
            # In every ER_WRONG_VALUE_FOR_VAR translation in MySQL and MariaDB,
            # the quoted variable name comes first on the error line. Leave
            # ordinary recognizable variable names readable; fail closed on secret
            # or unrecognizable names, or when an earlier quote precedes the name.
            line_end = text.find('\n', message.end(), limit)
            name_limit = limit if line_end < 0 else line_end
            name = _ERROR_QUOTED_NAME.search(text, message.end(), name_limit)
            first_quote = name is not None and not any(
                quote in text[message.end():name.start()] for quote in ("'", '"', '`'))
            var_name = name.group(2) if first_quote else ''
            known_name = first_quote and sensitive(var_name)
            if first_quote and not known_name and _ERROR_VARIABLE_NAME.fullmatch(var_name):
                continue
            start = name.end() if known_name else message.end()
            opener = _ERROR_VALUE_OPEN.search(text, start, limit)
            if opener is None:
                continue
            end = max(text.rfind(quote, opener.end(), limit) for quote in ("'", '"', '`'))
            # An unknown name gives no trusted message structure. An odd quote
            # count can be a raw embedded apostrophe or an unfinished final value;
            # either way a last-quote guess could expose its trailing bytes.
            if (not known_name or end < 0 or
                    any(text.count(quote, opener.start(), limit) % 2 for quote in ("'", '"', '`'))):
                end = limit
            parts.extend((text[kept:opener.end()], REDACTED))
            kept = end
        if parts:
            text = ''.join(parts) + text[kept:]
    return text


def _redact_set_password(text):
    """Visit tokens once, redacting after the first bare assignment operator."""
    parts, kept, pending = [], 0, False
    for token in _SET_PASSWORD_PART.finditer(text):
        if token.lastgroup == 'command':
            pending = True
        elif token.group() == ';':
            pending = False
        elif pending and token.group() == '=':
            pending = False
            literal = _SET_PASSWORD_LITERAL.match(text, token.end())
            if literal is not None:
                parts.extend((text[kept:token.end()], _redact_literal(literal)))
                kept = literal.end()
    return ''.join(parts) + text[kept:] if parts else text


def _secret_value_quote(text, start, end):
    ch = text[start]
    if ch == "'" or ch == '"':
        return ch
    opener = _SECRET_VALUE_OPENER.match(text, start, end)
    return opener.group(2) if opener is not None else "'"


def _is_set_password_account(text, start, end):
    """Whether *text[start:end]* is a valid SET PASSWORD [FOR account] prefix."""
    def check(pat):
        tokens = []
        for tok in pat.finditer(text, start, end):
            grp = tok.lastgroup
            if grp is None:
                continue
            if grp == 'line':
                if tok.end() == end:
                    return False
                continue
            if grp == 'bad' or len(tokens) >= 4:
                return False
            tokens.append((grp, tok.group()))
        if not tokens:
            return True
        if tokens[0][0] != 'word' or tokens[0][1].upper() != 'FOR':
            return False
        rest = tokens[1:]
        if len(rest) == 1:
            return rest[0][0] in ('quoted', 'word')
        if len(rest) == 3:
            if (rest[0][0] == 'word' and rest[0][1].upper() == 'CURRENT_USER'
                    and rest[1][0] == 'lp' and rest[2][0] == 'rp'):
                return True
            return (rest[0][0] in ('quoted', 'word') and rest[1][0] == 'at'
                    and rest[2][0] in ('quoted', 'word'))
        return False

    if check(_SET_PASSWORD_ACCOUNT_TOKEN):
        return True
    return '\\' in text[start:end] and check(_SET_PASSWORD_ACCOUNT_TOKEN_NO_BACKSLASH)


def _set_password_lexical_spans(text):
    """Keep the lexical helper's decisions as spans in the original text."""
    pending, command_end = False, 0
    for token in _SET_PASSWORD_PART.finditer(text):
        if token.lastgroup == 'command':
            pending, command_end = True, token.end()
        elif token.group() == ';':
            pending = False
        elif pending and token.group() == '=':
            pending = False
            literal = _SET_PASSWORD_LITERAL.match(text, token.end())
            if literal is None and _is_set_password_account(text, command_end, token.start()):
                literal = _SET_PASSWORD_VALUE.match(text, token.end())
            if literal is not None:
                yield literal.start(2), literal.end(2)


def _set_password_separator_spans(text):
    """Cover diagnostic and executable-comment assignments without a length cap."""
    pending, kept, command_end = False, 0, 0
    for token in _SET_PASSWORD_SEPARATOR.finditer(text):
        if token.start() < kept:
            continue
        value = token.group()
        if value == ';':
            pending = False
        elif value == '=':
            if pending:
                literal = _SET_PASSWORD_LITERAL.match(text, token.end())
                plain = literal is not None
                if literal is None and _is_set_password_account(text, command_end, token.start()):
                    literal = _SET_PASSWORD_VALUE.match(text, token.end())
                if literal is not None and literal.group(2) not in ("'", '"', "''", '""'):
                    yield literal.start(2), literal.end(2)
                    if plain:
                        kept = literal.end()
            pending = False
        else:
            pending, command_end = True, token.end()


def _set_password_keyword_spans(text):
    """Lex from a keyword even when preceding prose has unmatched quotes."""
    cursor = 0
    while cursor < len(text):
        command = _SET_PASSWORD_KEYWORD.search(text, cursor)
        if command is None:
            return
        cursor = len(text)
        for token in _SET_PASSWORD_FROM_KEYWORD.finditer(text, command.end()):
            if token.group() not in ('=', ';'):
                continue
            cursor = token.end()
            if token.group() == '=':
                literal = _SET_PASSWORD_LITERAL.match(text, token.end())
                plain = literal is not None
                if literal is None and _is_set_password_account(text, command.end(), token.start()):
                    literal = _SET_PASSWORD_VALUE.match(text, token.end())
                if literal is not None and literal.group(2) not in ("'", '"', "''", '""'):
                    yield literal.start(2), literal.end(2)
                    if plain:
                        cursor = literal.end()
            break


def _redact_set_password_all(text):
    """Redact the union of three linear scans over the same original input.

    SQL, malformed diagnostics and executable comments need different quote
    contexts. Sequential substitutions would change the later scans' context.
    Each scan yields ordered spans, so merging three streams stays linear;
    overlapping literals consume their whole union without exposing a tail.
    """
    spans = heapq.merge(_set_password_lexical_spans(text),
                        _set_password_separator_spans(text),
                        _set_password_keyword_spans(text))
    parts, kept = [], 0
    for start, end in spans:
        if start < kept:
            kept = max(kept, end)
            continue
        quote = _secret_value_quote(text, start, end)
        parts.extend((text[kept:start], quote, REDACTED, quote))
        kept = end
    return ''.join(parts) + text[kept:] if parts else text


_UNSET = object()


def _redact_credential_pass(text, allow_custom_boundaries=True, semicolon_only=False,
                            custom_boundaries=None, pass1_redacted=None, folded=_UNSET):
    """Run credential scans on *text* and apply their merged union once."""
    if folded is _UNSET:
        folded = text.lower() if text.isascii() else None
    has_eq = '=' in text
    has_backslash = '\\' in text
    set_inventory = has_eq and (
        folded is None or _may_have_set_secret_variables(folded))
    literal_inventory = (not semicolon_only) and has_eq and (
        folded is None or _may_have_secret_variable_literals(folded))
    set_password_possible = has_eq and (
        folded is None or ('password' in folded and 'set' in folded))
    scans = []
    if set_password_possible:
        spans = heapq.merge(_set_password_lexical_spans(text),
                            _set_password_separator_spans(text),
                            _set_password_keyword_spans(text))
        scans.append((start, end, _secret_value_quote(text, start, end),
                      1 if text[start] in ("'", '"') else 11)
                     for start, end in spans)
        scans.append(_secret_literal_readings(text, _SET_PASSWORD_LEGACY_LITERAL, 5, folded=folded))
    boundaries, prefix_boundaries = None, ()
    if set_inventory:
        if custom_boundaries is not None:
            boundaries = custom_boundaries
        elif allow_custom_boundaries:
            boundaries, prefix_boundaries = _secret_variable_boundary_plan(text)
        else:
            boundaries = ()
        scans.append((start, end, quote, 3 | (_NO_DROP_FLAG if no_drop else 0))
                     for start, end, quote, no_drop in _secret_variable_spans(
                         text, True, allow_custom_boundaries, boundaries, pass1_redacted,
                         include_stage=True))
        if has_backslash:
            scans.append((start, end, quote, 2 | (_NO_DROP_FLAG if no_drop else 0))
                         for start, end, quote, no_drop in _secret_variable_spans(
                             text, False, allow_custom_boundaries, boundaries, pass1_redacted,
                             include_stage=True))
        if prefix_boundaries:
            prefix_bs_tag = 3 | _NO_DROP_FLAG
            prefix_nobs_tag = 2 | _NO_DROP_FLAG
            scans.append((start, end, quote, prefix_bs_tag)
                         for start, end, quote in _secret_variable_spans(
                             text, True, False, prefix_boundaries, pass1_redacted, True))
            if has_backslash:
                scans.append((start, end, quote, prefix_nobs_tag)
                             for start, end, quote in _secret_variable_spans(
                                 text, False, False, prefix_boundaries, pass1_redacted, True))
    elif custom_boundaries is not None:
        boundaries = custom_boundaries
    for index, (pattern, keyword) in enumerate(zip(_SECRET_PATTERNS, _SECRET_PATTERN_KEYWORDS)):
        if semicolon_only and pattern is not _SECRET_PATTERNS[3]:
            continue
        stage = 2 + index
        if pattern is _SECRET_VARIABLE_LITERAL:
            if literal_inventory:
                scans.append(_secret_literal_readings(text, pattern, stage, folded=folded))
            continue
        if folded is not None and keyword is not None and keyword not in folded:
            continue
        if pattern is _SECRET_PATTERNS[2] and '(' not in text:
            continue
        if pattern is _SECRET_PATTERNS[3] and (
                not has_eq or (folded is not None and 'set' not in folded)):
            continue
        if (pattern is _SECRET_PATTERNS[-1] and folded is not None
                and 'random' not in folded and '[redacted]' not in folded):
            continue
        scans.append(_secret_literal_readings(text, pattern, stage, folded=folded))
    # Every scan uses the original quote boundaries, so one replacement
    # cannot hide a later pattern or shorten its other SQL-mode reading.
    if not scans:
        return text, False, False, (), None, False
    merged = scans[0] if len(scans) == 1 else heapq.merge(*scans)
    return _redact_spans_pass(
        text, merged, boundaries=boundaries,
        set_password_possible=set_password_possible,
        allow_custom_boundaries=allow_custom_boundaries)


def redact(text, password=None, quoted=True):
    """*text* with secrets replaced by ``[redacted]``: the literals after
    IDENTIFIED BY / AS / USING, PASSWORD('...'), SET PASSWORD, SOURCE_PASSWORD,
    MASTER_PASSWORD and other ``*PASSWORD = '...'`` options, SET assignments
    to secret variables and their rejected values in server errors, database
    URL credentials, and the connection's own
    *password* anywhere (also with doubled or backslash-escaped SQL quotes).
    Set *quoted* to False for URL passwords in non-SQL server values."""
    if text is None:
        return None
    text = str(text)
    if password:
        for variant in sorted({password, password.replace("'", "''"),
                               password.replace("\\", "\\\\").replace("'", "\\'"),
                               password.replace('"', '""')}, key=len, reverse=True):
            if variant:
                text = text.replace(variant, REDACTED)
    # These are candidate checks, not SQL parsing. The substitutions insert
    # only [redacted] and quotes, so they cannot create a missing keyword.
    # Preserve Python re.I's non-ASCII equivalences by disabling all guards
    # on non-ASCII input, just as the inventory guard does.
    folded = text.lower() if text.isascii() else None
    if (folded is None or ('variable' in folded and "can't" in folded)
            or ('1231' in folded and 'error' in folded)):
        updated = _redact_variable_errors(text, folded=folded)
        if updated is not text:
            text = updated
            folded = text.lower() if text.isascii() else None
    text1, overlapped, needs_followup, remapped, unquoted_redacted, had_stage01 = _redact_credential_pass(
        text, folded=folded)
    if text1 is not text:
        text = text1
        folded = text.lower() if text.isascii() else None
    if needs_followup:
        # One bounded follow-up pass (without re-running split_statements to
        # open new custom delimiter boundaries) handles secrets exposed after
        # a first-pass replacement collapsed an overlap, semicolon, '=', comma,
        # or odd quote.
        text2, _, _, _, _, _ = _redact_credential_pass(
            text, allow_custom_boundaries=False, semicolon_only=not overlapped,
            custom_boundaries=remapped, pass1_redacted=unquoted_redacted, folded=folded)
        if text2 is not text:
            text = text2
            folded = text.lower() if text.isascii() else None
    if (needs_followup or had_stage01) and (
            folded is None or ('replace' in folded
                               and ('random' in folded or '[redacted]' in folded))):
        # A preceding credential now supplies the [redacted] REPLACE arm.
        updated = _redact_secret_variable_literals(text, _SECRET_PATTERNS[-1], folded=folded)
        if updated is not text:
            text = updated
            folded = text.lower() if text.isascii() else None
    parts, kept = [], 0
    if folded is None or '://' in folded:
        for start, end in _URL_SECRET(text, quoted=quoted):
            parts.extend((text[kept:start], REDACTED))
            kept = end
    if parts:
        text = "".join(parts) + text[kept:]
    if folded is None or '0x' in folded:
        text = _HEX_SECRET.sub(lambda m: m.group(1) + "'" + REDACTED + "'", text)
    return text


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


#: The connection errors any read can meet (connection_error_text), in plain
#: words with what to check. Numbers from include/errmsg.h, the same in MySQL
#: and MariaDB: 2002 CR_CONNECTION_ERROR and 2003 CR_CONN_HOST_ERROR (could
#: not connect; the MariaDB client reports a refused TCP connection as 2002
#: too), 2005 CR_UNKNOWN_HOST, 2006 CR_SERVER_GONE_ERROR and 2013
#: CR_SERVER_LOST; 1045 is the server's ER_ACCESS_DENIED_ERROR.
_CANNOT_CONNECT = "Could not connect to the server. Check that it is running and that the host and port are right."
_CANNOT_CONNECT_SOCKET = "Could not connect to the server. Check that it is running and that the socket path is right."
_UNKNOWN_HOST = "Could not find the server's host name. Check the host for a typing error, or use its IP address."
_LOST = ("Lost the connection to the server. Check that it is still running: it may have restarted, "
         "or a network or time limit ended the connection.")
_ACCESS_DENIED = ("The server refused the user name or password. Check both, and that the account may "
                  "connect from your address.")
_ACCESS_DENIED_NO_PASSWORD = "The server refused the login, and no password was sent. Enter the password, or check the user name."


def connection_error_text(text):
    """*text* led by plain words and what to check when it carries one of the
    common connection errors (could not connect, unknown host, lost
    connection, access denied), else None.

    The client's error line follows the lead unchanged from its ``ERROR n``
    on (what comes after it, such as an SSH tunnel note, stays). The client
    wrapper ("mysql client exited 1: ") is dropped. When no session was
    opened (could not connect, unknown host, access denied) the caller's
    context before the error ("Could not read the server status: ") is
    dropped too, since nothing was read or sent; a connection lost on the
    way keeps it ("Could not end connection 41: "), because what was being
    done matters there. 1045 gets the user name and password hint only when
    the message says Access denied, so no other failure reads as a password
    problem.
    """
    raw = re.sub(r"mysql client exited \d+:\s*", "", str(text or ""))
    m = re.search(r"\bERROR\s+(\d{3,5})\b", raw)
    if not m:
        return None
    code = int(m.group(1))
    tail = " ".join(raw[m.start():].split())
    if code in (2002, 2003):
        lead = _CANNOT_CONNECT_SOCKET if "through socket" in tail else _CANNOT_CONNECT
    elif code == 2005:
        lead = _UNKNOWN_HOST
    elif code in (2006, 2013):
        return _LOST + " " + " ".join(raw.split())
    elif code == 1045 and "Access denied" in tail:
        lead = _ACCESS_DENIED_NO_PASSWORD if "(using password: NO)" in tail else _ACCESS_DENIED
    else:
        return None
    return lead + " " + tail


# ---- the audit trail --------------------------------------------------------------------

AUDIT_SCHEMA_VERSION = "audit-1.0"
#: pending: written before sending, not finished (myflames stopped); unknown:
#: interrupted or timed out after the statement may have reached the server.
#: "stopped": the person stopped a write that was under way (Import CSV); what
#: was kept is in rows_affected and the message.
AUDIT_STATUSES = ("pending", "ok", "error", "declined", "refused", "unknown", "stopped")
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
    from .ssh_tunnel import target_text
    return {"profile_id": safety.get("profile_id"), "profile_name": safety.get("profile_name"),
            "connection_type": safety.get("type"), "read_only": bool(safety.get("read_only")),
            "host": connection.get("host"), "port": port if isinstance(port, int) else None,
            "db_user": connection.get("user"), "ssh": target_text(connection), "engine": engine,
            "server_version": server_version}


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
    return (bool(getattr(exc, "guarded_unknown", False)) or bool(getattr(exc, "guarded_group", False))
            or "timed out" in text or error_code(text) in (2006, 2013) or "Lost connection to" in text)


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
                     "rows_affected", "duration_ms", "ssh")


def audit_object_report(store, server, kind, key, ssh=None):
    """Local history with an exact structured target and SSH endpoint."""
    if (not isinstance(server, str) or not 1 <= len(server) <= 300 or
            not re.fullmatch(r'.+:[0-9]{1,5}', server)):
        raise ValueError('server must be the audit host:port.')
    tunnel = None
    if ssh is not None:
        if (not isinstance(ssh, dict) or set(ssh) != {'host', 'user', 'port'} or
                not isinstance(ssh['host'], str) or not ssh['host'].strip() or len(ssh['host']) > 255 or
                not isinstance(ssh['user'], str) or len(ssh['user']) > 255 or
                isinstance(ssh['port'], bool) or not isinstance(ssh['port'], int) or not 0 <= ssh['port'] <= 65535 or
                any(ch in ssh['host'] + ssh['user'] for ch in ('\x00', '\n', '\r'))):
            raise ValueError('ssh must identify one SSH host, user and port.')
        from .ssh_tunnel import target_text
        tunnel = target_text({'ssh_host': ssh['host'], 'ssh_user': ssh['user'], 'ssh_port': ssh['port'] or None})
    fields = {'parameter': ('name',), 'account': ('name', 'host'), 'index': ('schema', 'table', 'index')}
    if not isinstance(kind, str) or kind not in fields:
        raise ValueError('kind must be index, parameter or account.')
    if kind == 'parameter' and isinstance(key, str):
        key = {'name': key}
    expected_fields = set(fields[kind]) | ({'role'} if kind == 'account' else set())
    if not isinstance(key, dict) or set(key) != expected_fields:
        raise ValueError('key must identify exactly one %s.' % kind)
    if kind == 'account' and not isinstance(key['role'], bool):
        raise ValueError('key.role must be true or false.')
    for field in fields[kind]:
        value = key[field]
        if (not isinstance(value, str) or len(value) > 256 or '\x00' in value or
                (not value and not (kind == 'account' and field in ('name', 'host')))):
            raise ValueError('key.%s must be object-name text.' % field)
    return {'schema_version': AUDIT_SCHEMA_VERSION, 'records': store.audit_object_records(server, kind, key, tunnel)}


def audit_object_matches(operation, target, engine, kind, key):
    """SQLite predicate. Old, malformed or insufficient targets fail closed."""
    try:
        target = json.loads(target) if target else None
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(target, dict):
        return False
    if kind == 'parameter':
        return (operation in ('set_global', 'set_persist', 'set_persist_only', 'set_session', 'reset_persist',
                              'topology.variable') and
                target.get('name') == key['name'])
    if kind == 'account':
        from .accounts import _name
        if engine not in ('mysql', 'mariadb'):
            return False
        try:
            account = _name(key['name'], key['host'], engine, key['role'])
        except ValueError:
            return False
        return operation.startswith('accounts.') and target.get('account') == account
    if kind == 'index':
        index = target.get('alter_table')
        return (operation == 'editor_run' and isinstance(index, dict) and
                all(index.get(field) == key[field] for field in ('schema', 'table', 'index')))
    # Never infer an index from its table or stored statement text.
    return False


def audit_report(store, since_days=None, server=None, limit=None, status=None, before=None, record_id=None):
    """The ``audit-1.0`` envelope: newest records first, with the retention
    setting. Every filter (*since_days*, *server*, *status*) applies before
    *limit*; *before* (a record id) continues a list after that record, and
    ``has_more`` says whether older records match."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000):
        raise ValueError('limit must be an integer from 1 to 100000.')
    fetch = limit + 1 if limit is not None and limit < 100000 else limit
    records = store.audit_records(since_days=since_days, server=server, limit=fetch, status=status, before=before,
                                  record_id=record_id)
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
                  "user": c.get("user"), "ssh": c.get("ssh"), "engine": c.get("engine"),
                  "server_version": c.get("server_version"),
                  "statements": "\n".join(r.get("statements") or [])}
        writer.writerow([_csv_cell(values[k] if k in values else r.get(k)) for k in AUDIT_CSV_COLUMNS])
    return buffer.getvalue()


def audit_export(store, fmt="json", since_days=None, server=None, status=None, record_id=None):
    """``{"format", "content", "mime", "filename"}`` for every matching record."""
    if fmt not in ("json", "csv"):
        raise ValueError("Choose export format 'json' or 'csv'.")
    report = audit_report(store, since_days=since_days, server=server, status=status, record_id=record_id)
    report.pop("has_more", None)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "csv":
        content, mime = audit_csv(report["records"]), "text/csv"
    else:
        content, mime = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", "application/json"
    return {"format": fmt, "content": content, "mime": mime, "filename": "myflames-audit-%s.%s" % (stamp, fmt)}
