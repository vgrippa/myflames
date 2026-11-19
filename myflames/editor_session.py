"""Query editor sessions: run any SQL in one long-lived client session per tab.

The workspace's Query editor runs SQL the way Workbench or DBeaver do: every
tab owns one server session (query-editor skill). myflames has no MySQL
driver (stdlib only), so a session is one ``mysql`` or ``mariadb`` client
process with stdin kept open, started on the tab's first run and ended on
close, connection switch, idle timeout or workspace exit. During startup
only, a refused identity guard closes the client and retries once on a fresh
client with no write or KILL binding. An opened session is never
reconnected silently: when the process or its connection ends, the tab reads
"Session ended. Session settings were lost and any open transaction was
rolled back." and starts over only when the user asks.

How one statement is framed
---------------------------
The client runs with ``--xml --binary-as-hex --binary-mode --comments
--force --quick --unbuffered -vv --skip-reconnect`` (plus
``--skip-commands`` where the MySQL client has it) and a per-session random
client delimiter set once with ``DELIMITER``. myflames splits the editor
text itself (:func:`myflames.dbops.split_statements`; user DELIMITER lines
are never forwarded) and writes one statement at a time::

    <statement>\\n<delimiter>\\ndelimiter\\n

The bare ``delimiter`` line is a client command with no argument: the
client prints ``ERROR at line N: DELIMITER must be followed by ...`` to
stderr without contacting the server. N is the line myflames wrote it on,
so that completion itself adds no server statement. A write's recorded
identity guard and SQL share one packet; its guard OK is hidden. The guard
is a SET, so diagnostics read by a guarded write describe that SET; a plain
diagnostic read following the write still describes the write. Server errors carry the
line their statement started on (``ERROR 1146 (42S02) at line 12: ...``),
which names the failed statement exactly.

stdout and stderr are read from separate pipes: the MariaDB client prints
an error without flushing stdout first, so a merged stream can put an error
before the rows it follows. Each pipe is ordered on its own. On stdout,
``-vv`` echoes each statement between two ``--------------`` lines before
its output; myflames skips the echo by its line count (it knows the text it
sent), then reads the XML result sets (``<field ... xsi:nil="true" />`` is
NULL, every other value is text; binary columns print as ``0x`` plus hex)
and the client's own summary lines (``N rows in set, W warnings``, ``Query
OK, N rows affected``, the server's info line such as ``Rows matched: 1
Changed: 1``). A statement's stdout block ends where the next echo starts;
the run's last helper statement (a tagged SELECT of the session state)
therefore closes the block of the last user statement.

``--force`` keeps the client alive after an error (without it the batch
client exits on the first error and the session would be lost); myflames
sends the next statement only after the previous one ended without an
error, so a script still stops at its first error.

Row limits and Stop
-------------------
Results are read with ``--quick`` (rows stream as the server sends them).
Once a result set has more rows than the limit (default 1,000), myflames
keeps reading but stops keeping rows and sends ``KILL QUERY <id>`` from a
second short connection, so the server statement does not stay open behind
a full pipe; the result is labeled "stopped after N rows". Load more runs
the statement again with a larger limit. Stop sends the same KILL QUERY for
the session's ``CONNECTION_ID()`` (read when the session starts and checked
after every run); KILL QUERY ends the statement, not the session or its
transaction. A KILL also requires that the id's current SQL starts with this
tab's private random comment, checked in the same packet as KILL. A KILL is
never sent while a helper statement runs, and the
next statement is never sent before an issued KILL has returned, so a KILL
cannot reach another statement (both servers also clear a KILL QUERY that
arrives between statements when the next command starts).

Result retention is bounded across all result sets of a run: cells (including
NULL and empty text), result entries, estimated memory with container overhead,
and JSON bytes (including escaping and column metadata). The JSON allowance is
6 MiB, leaving room for the export request envelope below ui.MAX_BODY (12 MiB).
A budget truncates the retained prefix with a reason; helpers remain readable.
CRLF is accepted only at protocol boundaries, including across pipe chunks.
The client's XML escaping covers & < > and quotes, not CR or LF in values;
those characters must stay untouched (global newline replacement is unsafe).

Transactions
------------
After every run myflames reads ``@@autocommit`` and the transaction state
with tagged helpers (``/*myflames:editor*/``): MariaDB's
``@@in_transaction``; MySQL has no such variable, so the current row of
``performance_schema.events_transactions_current`` for
``PS_CURRENT_THREAD_ID()`` is read, and the state is "unknown" (never "no
transaction") when that table cannot tell. Commit and Rollback are explicit
actions. Every statement carries a private per-session leading comment;
helpers additionally retain their public ``/*myflames:editor*/`` tag.

Safety
------
Every run goes through :mod:`myflames.dbops` (classification with the
session's backslash reading, the connection's type and read-only flag,
confirmation of the exact statements, an audit record for every attempted,
declined or refused write; reads are not audited). Production sessions
start with ``SET SESSION sql_safe_updates = 1`` (Development off; a per-tab
setting overrides it). Read-only connections start with ``SET SESSION
TRANSACTION READ ONLY`` so the server refuses a write the classifier could
not see.
"""
import atexit
import codecs
import contextlib
import csv
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import weakref

from .connector import ConnectorError, MySQLConnection
from .server_info import identity_from_version, parse_version_tuple, product_identity
from .editor_insights import EditorInsights

SCHEMA_VERSION = "editor-session-1.0"
RUN_SCHEMA_VERSION = "editor-run-1.0"
TAG = "/*myflames:editor*/ "
SESSION_ENDED_TEXT = "Session ended. Session settings were lost and any open transaction was rolled back."
STOPPED_TEXT = ("Stopped with KILL QUERY. It ended the statement and undid what that statement changed; "
                "an open transaction is still open.")
STOPPED_AFTER_TEXT = ("Stopped before the next statement: the running statement had already finished, so it "
                      "took effect. Later statements were not run.")

DEFAULT_LIMIT = 1000
MAX_LIMIT = 100000
DEFAULT_IDLE_SECONDS = 30 * 60
MAX_IDLE_SECONDS = 24 * 3600
MAX_SESSIONS = 8
MAX_SQL = 1000000
#: Aggregate per-run budgets, with one reserved entry for the truncation notice.
#: JSON is measured with ensure_ascii=True, bounding both UTF-8 and ASCII-escaped
#: requests. Memory charges deliberately include row/cell/column containers and
#: four bytes per code point, rather than pretending NULL and empty text are free.
RESULT_BYTE_CAP = 6 * 1024 * 1024
RESULT_MEMORY_CAP = 16 * 1024 * 1024
RESULT_CELL_CAP = 100000
RESULT_SET_CAP = 128
#: One row larger than this stops the result set (a single huge value).
ROW_CHAR_CAP = 4 * 1024 * 1024
HELPER_TIMEOUT = 30.0
# Allow the other pipe's reader to consume an acknowledgement before a
# negative decision. Missing replies require a graceful EOF and full drain;
# a timeout alone never proves that the guard, rather than user SQL, failed.
GUARD_REPLY_GRACE = 1.0
#: Seconds a confirmation token stays valid; each one is used once.
TOKEN_TTL = 300.0
#: Seconds a statement may run on after a KILL QUERY before the session is ended.
STOP_GRACE = 15.0
KILL_TIMEOUT = 10
NOTE_CAP = 50

#: Errors after which the client has no usable connection: end the session
#: (the batch client never reconnects; myflames never does it silently).
LOST_CONNECTION_CODES = (1053, 1927, 2006, 2013, 2055, 4031)
#: KILL QUERY interrupted the statement.
INTERRUPTED_CODES = (1317, 3169)

STATUS_ECHO = "--------------"
#: The client's answer to the bare ``delimiter`` line that ends each statement.
MARKER_PREFIX = "ERROR at line %d: DELIMITER "
_ROWS_LINE = re.compile(r"^(\d+) rows? in set(?:, (\d+) warnings?)?$")
_EMPTY_LINE = re.compile(r"^Empty set(?:, (\d+) warnings?)?$")
_OK_LINE = re.compile(r"^Query OK(?:, (-?\d+) rows? affected)?(?:, (\d+) warnings?)?$")
_ERROR_LINE = re.compile(r"^ERROR(?: (\d+)(?: \(([^)]*)\))?)?(?: at line (\d+))?(?: in file: '[^']*')?: ?(.*)$")
_FIELD = re.compile(r'\t<field name="([^"]*)"(?: xsi:nil="true" />|>([^<]*)</field>)\r?\n')
XML_HEAD = '<?xml version="1.0"?>\n\n<resultset statement="'
XML_HEAD_END = '" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
ROW_OPEN = "\n  <row>\n"
ROW_CLOSE = "  </row>\n"
RESULT_CLOSE = "</resultset>\n"
_ROW_END = re.compile(r"  </row>\r?\n")
_RESULT_END = re.compile(r"</resultset>\r?\n")
#: The client prints binary columns (charset 63) as 0x plus upper-case hex.
HEX_VALUE = re.compile(r"0x(?:[0-9A-F]{2})*\Z")
_TAB_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_NEAR = re.compile(r"near '(.*)' at line (\d+)\s*\Z", re.S)
#: A $tag$ opener outside strings: MySQL 8.1+ clients read dollar-quoted text
#: when the server supports it; myflames' splitter does not. With our utf8mb4
#: client charset, every valid multibyte character is an identifier character,
#: including inside the tag (mysql.cc:2715-2746; strings/sql_chars.cc:87-121).
_DOLLAR_QUOTE = re.compile(r"(?<![A-Za-z0-9_$\u0080-\U0010ffff])\$[A-Za-z0-9_\u0080-\U0010ffff]*\$")
# SQL identifiers include '$' and every multibyte character, not just the
# Unicode letters recognized by Python's word boundaries. A keyword cannot
# start inside a variable or after a qualification dot either.
_SQL_ID_CHARS = r"A-Za-z0-9_$\u0080-\U0010ffff"
_SQL_BOUNDARY = (r"(?:(?<![" + _SQL_ID_CHARS + r"@.])(?=[" + _SQL_ID_CHARS +
                 r"])|(?<=[" + _SQL_ID_CHARS + r"])(?![" + _SQL_ID_CHARS + r"]))")
_READ_WRITE = re.compile(
    _SQL_BOUNDARY + r"READ\s+WRITE" + _SQL_BOUNDARY +
    r"|(?<![" + _SQL_ID_CHARS + r"@.])(?:@@\s*)?(?:(?:SESSION|LOCAL|GLOBAL)\s*\.\s*)?"
    r"(?:TRANSACTION_READ_ONLY|TX_READ_ONLY)" + _SQL_BOUNDARY + r"\s*(?::=|=)", re.I | re.ASCII)
_MODE_CHANGE = re.compile(r"(?:SQL_MODE|CALL|EXECUTE|SOURCE|"
                          r"@\s*@\s*(?:(?:SESSION|LOCAL|GLOBAL|PERSIST|PERSIST_ONLY)\s*\.\s*)?"
                          r"(?:SQL_MODE|''))", re.I | re.ASCII)


class EditorError(ValueError):
    """A request the editor refuses; ``code`` names the reason for the UI."""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code


class _StartupGuardRefused(EditorError):
    """A startup guard refused; its client has exited and both pipes drained."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _ms(seconds):
    return round(seconds * 1000.0, 3) if seconds is not None else None


def xml_unescape(text):
    """Undo the client's XML escaping (it escapes only & < > ")."""
    if "&" not in text:
        return text
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")


def _frame_end(text, start, token):
    """Match an LF framing token allowing CRLF, without touching value text.

    Return the end offset, None for a partial token, or -1 for a mismatch.
    A trailing CR may be half a CRLF in the next pipe chunk.
    """
    at = start
    for char in token:
        if at == len(text):
            return None
        if char == "\n" and text[at] == "\r":
            at += 1
            if at == len(text):
                return None
        if text[at] != char:
            return -1
        at += 1
    return at


def client_rstrip(text):
    """*text* without the trailing characters the client drops (remove_cntrl:
    everything that is not a graphic ASCII character, so spaces and controls)."""
    end = len(text)
    while end and (ord(text[end - 1]) <= 32 or text[end - 1] == "\x7f"):
        end -= 1
    return text[:end]


def strip_leading_line_comments(text):
    """(offset, rest): *text* without leading white space and ``--`` / ``#``
    comment lines. The client sends a line comment that starts a statement as
    a statement of its own, which would desynchronize the echo; the server
    ignores it anyway."""
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n\f\v":
            i += 1
        if text.startswith("#", i) or (text.startswith("--", i) and (i + 2 >= n or text[i + 2] in " \t\r\n\f\v")):
            newline = text.find("\n", i)
            i = n if newline < 0 else newline + 1
            continue
        break
    return i, text[i:]


class Offsets(object):
    """Convert offsets between Python strings (code points) and JavaScript
    strings (UTF-16 code units) for one text."""

    def __init__(self, text):
        self.text = text
        self.astral = [i for i, ch in enumerate(text) if ord(ch) > 0xFFFF]

    def to_js(self, i):
        if not self.astral:
            return i
        return i + sum(1 for a in self.astral if a < i)

    def to_py(self, j):
        if not self.astral:
            return j
        i = 0
        units = 0
        for a in self.astral:
            span = a - i
            if units + span >= j:
                return i + (j - units)
            units += span + 2
            i = a + 1
            if units > j:
                return a
        return i + (j - units)

    def line_col(self, i):
        before = self.text[:i]
        line = before.count("\n") + 1
        start = before.rfind("\n") + 1
        return line, self.to_js(i) - self.to_js(start) + 1


# ---- planning a run ---------------------------------------------------------------------

def _lexer_session_context(session):
    """Only a complete backend/session binding can authorize conditional SQL."""
    if session is None:
        return {"engine": None, "server_version": None, "identity_bound": None, "wsrep_on": None,
                "sql_mode": None, "client_engine": None}
    bound = False
    identity = getattr(session, "server_identity", None) or {}
    try:
        # This builds the existing guard without probing or sending anything.
        # The routed-startup fallback deliberately has no such binding.
        EditorSession._write_session_guard(session)
        bound = (all(identity.get(key) not in (None, "") for key in
                     ("connection_id", "version", "hostname", "port", "server_id")) and
                 identity.get("version") == session.server_version and
                 not getattr(session, "_routed_startup", False))
    except (ValueError, AttributeError, TypeError):
        pass
    wsrep = (getattr(session, "_kill_identity", None) or {}).get("wsrep_on")
    wsrep_value = str(wsrep).upper()
    wsrep_on = (True if wsrep_value in ("ON", "1", "TRUE") else
                False if wsrep_value in ("OFF", "0", "FALSE") else None)
    return {"engine": session.engine, "server_version": getattr(session, "server_version", None),
            "identity_bound": bound, "wsrep_on": wsrep_on, "sql_mode": getattr(session, "sql_mode", None),
            "client_engine": getattr(session, "client_engine", None)}


def _check_executable_comments(sql, engine, server_version, backslash_escapes, identity_bound=False, wsrep_on=None,
                               sql_mode=None, client_engine=None):
    """Use the server lexer; refuse uncertainty, never a known skipped option."""
    from . import dbops
    for reading in dbops.lexer_readings(backslash_escapes, engine, sql_mode, server_version, client_engine, sql):
        scan = dbops.lex_sql(sql, engine=engine, server_version=server_version, wsrep_on=wsrep_on, **reading)
        for comment in scan["comments"]:
            reason = comment.get("problem")
            if not reason and (not identity_bound or engine not in ("mysql", "mariadb") or
                               parse_version_tuple(server_version, leading=True,
                                                   prefer_mariadb=engine == "mariadb", default=None) is None):
                reason = "the server engine and version are not bound to this session"
            if reason:
                # Only a lexer-generated opener appears here, never SQL text.
                raise EditorError("Found executable comment %s ... */; %s, so myflames cannot confirm what runs. "
                                  "Use SQL without this construct or open a tab with a confirmed session."
                                  % (comment["opener"], reason), "invalid_statement")


def bound_oracle_mode(session):
    """Bound ORACLE state for the DDL scanner, or None without a mode fact."""
    from . import dbops
    context = _lexer_session_context(session)
    if not context["identity_bound"] or context["sql_mode"] is None:
        return None
    return dbops.oracle_mode(context["sql_mode"], context["engine"])


def _literal_sql_mode(unit):
    """A single literal session assignment; expressions and multi-SETs stay unknown.

    Mode names contain only ASCII letters, underscores, digits, commas and
    spaces. Accept that grammar directly, without interpreting an expression
    or an escape sequence under a mode that the expression might change.
    """
    code = restore_idents(unit["code"], unit.get("idents") or [])
    if not re.fullmatch(r"SET\s+(?:(?:SESSION|LOCAL)\s+)?"
                        r"(?:@@\s*(?:(?:SESSION|LOCAL)\s*\.\s*)?)?"
                        r"`?sql_mode`?\s*=\s*''\s*;?", code, re.I | re.ASCII):
        return None
    values = [unit["send"][start:end] for kind, start, end in unit.get("_tokens", ()) if kind == "string"]
    literal = re.fullmatch(r"'([A-Za-z0-9_, ]*)'", values[0]) if len(values) == 1 else None
    return literal.group(1) if literal else None


_PLAN_MODE_UNSET = object()


def _server_unit_count(text, unit):
    """Count selected-delimiter units using original server token spans.

    Quoted tokens stay atomic; placeholder identifiers in masked code must
    never invent occurrences of a custom delimiter such as a backtick.
    """
    count, pending = 0, False
    for kind, start, end in unit["token_spans"]:
        if kind in ("string", "identifier"):
            pending = True
        elif kind == "code":
            for index, part in enumerate(text[start:end].split(unit["delimiter"])):
                if index:
                    count += int(pending)
                    pending = False
                pending = pending or bool(part.strip())
    return count + int(pending)


def plan_statements(sql, backslash_escapes=True, engine=None, base=0, delimiter=";", server_version=None,
                    identity_bound=None, wsrep_on=None, sql_mode=_PLAN_MODE_UNSET, client_engine=None):
    """Plan exact client units, classifying each under its server SQL mode.

    A literal SET can supply the later mode. Indirect changes keep later
    units unknown, and every modelled reading must agree before execution.
    Re-split the remaining suffix only when its mode changes, so ordinary
    dumps are scanned once between their mode-setting headers and trailers.
    """
    from . import dbops
    # Preserve the explicit legacy boolean API for module-owned SQL. Session
    # callers always supply sql_mode, including None when it is unknown.
    if sql_mode is _PLAN_MODE_UNSET:
        sql_mode = None if backslash_escapes is None else ("" if backslash_escapes else "NO_BACKSLASH_ESCAPES")
    units, offset, pending = [], 0, []
    current_mode, current_backslash = sql_mode, backslash_escapes
    # A later literal SET may fail with Continue on error. Once this run
    # could have entered ORACLE grammar, a prediction cannot rule it out.
    oracle_possible = engine == "mariadb" and (sql_mode is None or dbops.oracle_mode(sql_mode, engine))
    lexer_args = {"engine": engine, "server_version": server_version, "wsrep_on": wsrep_on,
                  "client_engine": client_engine}
    while offset < len(sql) or pending:
        if not pending:
            readings = dbops.lexer_readings(current_backslash, engine, current_mode, server_version, client_engine,
                                            sql[offset:])
            pending = dbops.split_statements(sql[offset:], delimiter=delimiter, **dict(lexer_args, **readings[0]))
            if "$" in sql[offset:]:
                dollars = dbops.split_statements(sql[offset:], delimiter=delimiter, dollar_quotes=True,
                                                 **dict(lexer_args, **readings[0]))
                if dbops._target_reading(pending) != dbops._target_reading(dollars):
                    raise EditorError("Dollar-quoted text ($...$) changes how the client reads this run. "
                                      "The Query editor does not run dollar-quoted text yet.", "invalid_statement")
            if not pending:
                break
        stmt = pending.pop(0)
        number = len(units) + 1
        send_offset = offset + stmt["start"]
        # Compare this unit independently. Do not let initial quote settings
        # mask a later unit after a literal SET changes them.
        provisional = engine is None and server_version is None and identity_bound is None
        meanings, first_frame, count_mismatch = {}, None, False
        for reading in readings:
            checked = dbops.split_statements(stmt["text"], delimiter=stmt["delimiter"],
                                             **dict(lexer_args, **reading))
            meaning = dbops._mode_reading(checked)
            frame = [(u["start"], u["end"], u["delimiter"], u["unclosed"], u["issues"]) for u in checked]
            if first_frame is None:
                first_frame = frame
            # Before a session exists, version digits can change server code
            # without changing the bytes of a client unit. classify_units
            # takes the worst verdict across these grammars; _run_api must
            # re-plan and re-check consent on the opened session before send.
            # Quote/client disagreement within a grammar still refuses here.
            key = reading["comment_dialect"] if provisional else None
            if meaning != meanings.setdefault(key, meaning) or frame != first_frame:
                raise EditorError("The session quote mode is unknown, the server version is unknown or the "
                                  "client family is unknown, and this SQL reads differently under "
                                  "NO_BACKSLASH_ESCAPES, ANSI_QUOTES or MariaDB MSSQL mode. "
                                  "Use single quotes for strings and backticks for identifiers, or run with a "
                                  "known sql_mode and client family.", "invalid_statement")
            # The server tokens can expose delimiters hidden inside a client
            # string. Compare each client unit at its selected delimiter:
            # semicolons in an intentional custom-delimiter group or stored
            # body do not make that group a client/server framing mismatch.
            # This count never replaces the server code used to classify it.
            count_mismatch = count_mismatch or any(
                _server_unit_count(stmt["text"], unit) > 1 for unit in checked)
        if stmt["unclosed"] or stmt.get("issues"):
            reasons = list(stmt.get("issues") or [])
            if not reasons:
                if engine is not None or identity_bound is not None:
                    _check_executable_comments(stmt["text"], engine, server_version, current_backslash,
                                               identity_bound, wsrep_on, current_mode)
                reasons = ["Close every quoted string, quoted identifier and comment."]
            raise EditorError("Statement %d cannot run: %s" % (number, " ".join(reasons)), "invalid_statement")
        if count_mismatch:
            raise EditorError("This text runs as several statements on the server but as one for the client. "
                              "Put each statement on its own, ending with the delimiter.", "invalid_statement")
        lead, rest = strip_leading_line_comments(stmt["text"])
        send = client_rstrip(rest)
        if not send:
            continue
        if "\x00" in send:
            raise EditorError("Statement %d contains a NUL character, which the client cannot send." % number,
                              "invalid_statement")
        try:
            send.encode("utf-8")
        except UnicodeEncodeError:
            raise EditorError("Statement %d contains characters that are not valid Unicode." % number,
                              "invalid_statement")
        code = stmt["code"]
        if re.match(r"\s*delimiter", code, re.I):
            raise EditorError("Statement %d starts with the word DELIMITER, which the client reads as its own "
                              "command. Put DELIMITER on its own line." % number, "invalid_statement")
        if "$" in send:
            dollars = dbops.split_statements(send, delimiter=stmt["delimiter"], dollar_quotes=True,
                                             **dict(lexer_args, **readings[0]))
            plain = dbops.split_statements(send, delimiter=stmt["delimiter"], **dict(lexer_args, **readings[0]))
            if _DOLLAR_QUOTE.search(code) or dbops._target_reading(plain) != dbops._target_reading(dollars):
                raise EditorError("Dollar-quoted text ($...$) changes how the client reads this run. "
                                  "The Query editor does not run dollar-quoted text yet.", "invalid_statement")
        if engine is not None or identity_bound is not None:
            _check_executable_comments(send, engine, server_version, current_backslash,
                                       identity_bound, wsrep_on, current_mode)
        all_modes = dbops.lexer_readings(None, engine, server_version=server_version, client_engine=client_engine,
                                        sql=send)
        first_mode = dbops._mode_reading(dbops.split_statements(
            send, delimiter=stmt["delimiter"], **dict(lexer_args, **all_modes[0])))
        sensitive = any(dbops._mode_reading(dbops.split_statements(
            send, delimiter=stmt["delimiter"], **dict(lexer_args, **reading))) != first_mode
                        for reading in all_modes[1:])
        unit = {"index": len(units), "text": stmt["text"], "start": base + send_offset,
                "end": base + offset + stmt["end"], "send": send, "lead": lead, "code": code,
                "mode_sensitive": sensitive, "delimiter": stmt["delimiter"],
                "idents": list(stmt.get("idents") or []), "sql_mode": current_mode, "client_engine": client_engine,
                "_tokens": tuple((kind, start - stmt["start"] - lead, end - stmt["start"] - lead)
                                 for kind, start, end in stmt["token_spans"]
                                 if start >= stmt["start"] + lead and end <= stmt["end"]),
                "mode_unknown": current_mode is None and current_backslash is None,
                "conditional_sql": bool(dbops.lex_sql(send, **dict(lexer_args, **readings[0]))["comments"])}
        if oracle_possible and re.search(
                r"(?:^|;)\s*(?:CREATE\s+(?:OR\s+REPLACE\s+)?|ALTER\s+)"
                r"(?:DEFINER\s*=\s*[^;]+?\s+)?(?:AGGREGATE\s+)?"
                r"(?:PROCEDURE|FUNCTION|TRIGGER|EVENT|PACKAGE)\b",
                code, re.I | re.ASCII):
            raise EditorError("Stored-program definitions in MariaDB ORACLE mode are not supported by the "
                              "Query editor. Use a session with a known mode without ORACLE.", "invalid_statement")
        units.append(unit)
        if _unit_changes_mode(unit):
            # A custom-delimiter packet can change mode before its own next
            # server statement. Do not infer a mode halfway through a packet.
            if ";" in code and sensitive:
                raise EditorError("Statement %d may change sql_mode within a unit with ambiguous "
                                  "NO_BACKSLASH_ESCAPES, ANSI_QUOTES or MSSQL quoting. Run them separately."
                                  % number, "invalid_statement")
            next_mode = _literal_sql_mode(unit) if identity_bound else None
            oracle_possible = oracle_possible or (engine == "mariadb" and
                                                   (next_mode is None or dbops.oracle_mode(next_mode, engine)))
            if next_mode != current_mode or current_backslash is not None:
                delimiter = stmt["delimiter"]
                offset += stmt["next_start"]
                pending = []
            elif not pending:
                break
            current_mode, current_backslash = next_mode, None
        elif not pending:
            break
    if not units:
        raise EditorError("There is no statement to run.", "empty")
    return units


def _unit_changes_mode(unit):
    """Conservatively include indirect changes and quoted sql_mode names."""
    from . import dbops
    code = unit["code"]
    # Restore delimited identifiers. An opaque string assignment target in
    # a non-ANSI reading still cannot prove that sql_mode is unchanged.
    # Inspect SETs inside wrappers/blocks too, before sending any of the run.
    # Walk once rather than repeatedly scanning the rest of a large routine.
    assignment_pattern = re.compile(dbops._SET_ITEM.pattern, re.I | re.ASCII)
    depth, setting_depth, target = 0, None, False
    for token in _MASKED_TOKEN.finditer(code):
        word = token.group()
        word = word.upper() if word.isascii() else word
        # A quoted system-variable name can be part of an atomic @@ token,
        # with whitespace between its grammar tokens (MySQL sql_yacc.yy:
        # 16674-16689). A single @ is always a user variable, including @''.
        system = word.startswith("@") and re.match(r"@\s*@", word, re.ASCII)
        if _MODE_CHANGE.fullmatch(restore_idents(word, unit.get("idents") or []) if system else word):
            return True
        if word == "SET":
            setting_depth, target = depth, True
        elif word == ";":
            setting_depth, target = None, False
        elif target:
            if word == "STATEMENT":
                continue
            target = False
            assignment = assignment_pattern.match(code, token.start())
            if assignment and not assignment.group(4):
                name = dbops._name(re.split(r"\s*\.\s*", assignment.group(5))[-1], unit.get("idents") or [])
                if name.lower() in ("sql_mode", "''"):
                    return True
        if word == "(":
            depth += 1
        elif word == ")":
            depth = max(0, depth - 1)
        elif word == "," and setting_depth == depth:
            target = True
    return False


def classify_units(units, backslash_escapes, sql, engine=None, server_version=None, wsrep_on=None):
    """A dbops verdict for the run: the worst statement, with ``statements``
    being exactly what will be sent (for the confirmation)."""
    from . import dbops
    statements = []
    for unit in units:
        verdict = dbops.classify(unit["send"], None if unit.get("mode_unknown") else backslash_escapes,
                                 engine=engine, server_version=server_version, wsrep_on=wsrep_on,
                                 sql_mode=unit.get("sql_mode"), client_engine=unit.get("client_engine"))
        # BEGIN/START TRANSACTION and enabling autocommit can commit work
        # already in this tab. Apply exactly COMMIT's policy and audit class,
        # even when the shared classifier calls the control a read.
        if verdict["kind"] == "read" and implicit_commit(unit):
            commit = dbops.classify("COMMIT")
            verdict = dict(verdict, kind=commit["kind"], category=commit["category"],
                           reasons=["This transaction control can commit this session's existing work."])
        unit["kind"], unit["category"], unit["reasons"] = verdict["kind"], verdict["category"], verdict["reasons"]
        unit["session_control"] = any(s.get("category") in ("session", "transaction")
                                      for s in verdict["statements"])
        # Several ';'-separated server statements in one client statement (a custom DELIMITER).
        unit["single"] = getattr(dbops, "REASON_MULTI", None) not in verdict["reasons"]
        unit["verb"] = verdict["statements"][0]["verb"] if verdict["statements"] else ""
        statements.append({"text": unit["send"], "kind": verdict["kind"], "category": verdict["category"],
                           "reasons": verdict["reasons"]})
    rank = {kind: i for i, kind in enumerate(dbops.KINDS)}
    worst = max(statements, key=lambda s: rank[s["kind"]])
    reasons = []
    for s in sorted(statements, key=lambda s: -rank[s["kind"]]):
        for reason in s["reasons"]:
            if reason not in reasons:
                reasons.append(reason)
    return {"kind": worst["kind"], "category": worst["category"], "reasons": reasons,
            "statements": statements, "count": len(statements)}


def _needs_session_guard(unit):
    """Stateful and mode-sensitive reads need the same bound facts as writes."""
    return (unit.get("kind") != "read" or unit.get("session_control") or
            unit.get("category") in ("session", "transaction") or unit.get("conditional_sql") or
            unit.get("mode_sensitive"))


def error_position(message, unit, offsets):
    """Where a server error points in the editor text: the ``near '...' at
    line N`` of a syntax error, else the statement start."""
    start = unit["start"] + unit["lead"]
    send = unit["send"]
    found = None
    match = _NEAR.search(message or "")
    if match:
        near, line = match.group(1), int(match.group(2))
        cut = 0
        for _ in range(max(0, line - 1)):
            nl = send.find("\n", cut)
            if nl < 0:
                break
            cut = nl + 1
        if near == "":
            found = len(send)
        else:
            probe = near[:60]
            at = send.find(probe, cut)
            if at < 0:
                at = send.find(probe)
            found = at if at >= 0 else cut
    # Explain adds a prefix to the wire statement, not to the editor text.
    offset = start + max(0, (found or 0) - unit.get("explain_prefix", 0))
    line, column = offsets.line_col(offset)
    return {"offset": offsets.to_js(offset), "line": line, "column": column, "exact": found is not None}


# ---- one statement written to the client -------------------------------------------------

class Step(object):
    """One statement sent to the client and what came back for it."""

    def __init__(self, kind, send, first_line, marker_line, unit=None, limit=None, results=None):
        self.kind = kind            # 'user', 'helper' or 'action'
        self.send = send
        self.unit = unit
        self.first_line = first_line
        self.marker_line = marker_line
        self.echo_lines = send.count("\n") + 1
        self.limit = limit
        self.expect_results = results  # helpers: result sets that close the block
        self.entries = []           # stdout: rows and ok entries, in order
        self.errors = []            # stderr: errors, in order
        self.marker = False         # the end-of-statement line arrived on stderr
        self.closed = False         # the stdout block is complete
        self.sent_at = None
        self.done_at = None
        self.kill_requested = False
        self.kill_reason = None
        self.kill_ok = False        # one plain read: the row limit may stop it with KILL QUERY
        self.kill_sent_at = None
        self.stop_sent = False
        self.unfinished = False     # the client exited before the statement's end line
        self.notes = []
        self.guarded = False
        self.guard_pending = False  # until this packet's private acknowledgement
        self.guard_ok_pending = False
        self.guard_entry = None
        self.packet_lines = None
        self.guard_unresolved = False

    @property
    def duration_ms(self):
        if self.sent_at is None or self.done_at is None:
            return None
        return _ms(self.done_at - self.sent_at)

    def real_errors(self):
        return list(self.errors)


# ---- the session ------------------------------------------------------------------------

def _clean_env(home):
    """Give external clients only runtime settings and an empty private home.

    As in lifecycle._clean_env, use an allowlist: removing MYSQL_PWD alone
    would still leak vault/API secrets and permit endpoint overrides.
    """
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE",
                                           "TZ", "TMPDIR", "TEMP", "TMP") if key in os.environ}
    env.update(HOME=home, USERPROFILE=home, MYSQL_TEST_LOGIN_FILE=os.path.join(home, "no-login-file"))
    return env


def _client_features(binary, env=None):
    """Options the client binary accepts that not every version has."""
    cached = _FEATURES.get(binary)
    if cached is not None:
        return cached
    features = {"skip_commands": False}
    try:
        proc = subprocess.run([binary, "--no-defaults", "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10, check=False, env=env)
        text = (proc.stdout or b"").decode("utf-8", "replace")
        features["skip_commands"] = bool(re.search(r"^\s*--commands\b", text, re.M))
    except (OSError, subprocess.SubprocessError):
        pass
    _FEATURES[binary] = features
    return features


_FEATURES = {}


class Run(object):
    """One run in a session: the statements, their results, its state."""

    def __init__(self, units, limit, options, sql_text, offsets, scope, kind="run", audit=None, timeout=None,
                 history=None, alter_context=None, continue_on_error=False):
        self.id = uuid.uuid4().hex
        self.units = units
        self.limit = limit
        self.options = options      # holds the password only while the run lasts
        self.sql_text = sql_text
        self.offsets = offsets
        self.scope = scope
        self.kind = kind
        self.audit = audit
        self.timeout = timeout
        self.history = history or {}
        self.alter_context = alter_context
        self.state = "running"
        self.started = time.monotonic()
        self.started_at = _now()
        self.finished_at = None
        self.duration = None
        self.steps = []
        self.current = None
        self.stop_requested = False
        self.stopped_by = None
        self.done = threading.Event()
        self.retained = 1024       # reserved truncation entry and export envelope
        self.serialized = 1024
        self.cells = 0
        self.result_sets = 0
        self.budget_reason = None
        self.limit_entry = None
        self.kill_threads = []
        self.message = None
        self.failed = None
        self.interrupted = False
        #: Run all keeps going past a statement error (the tab's On error
        #: option); Stop, a lost session and a declined review still stop it.
        self.continue_on_error = continue_on_error
        self.snapshot_cache = None
        self.audit_id = None
        self.history_id = None
        self.target_guard = None
        self.guard_failure = None


class EditorSession(EditorInsights):
    """One client process (one server session) for one editor tab."""

    #: Extra client options for a private session that needs them (Import
    #: CSV's LOAD DATA LOCAL session adds --local-infile=1); editor tabs add none.
    client_flags = ()

    def __init__(self, tab_id, identity, options, connection_type="production", read_only=False,
                 safe_updates=None, idle_seconds=DEFAULT_IDLE_SECONDS, name=None, profile_id=None):
        self.tab_id = tab_id
        self.identity = identity
        self.name = name
        self.profile_id = profile_id
        self.host = options.get("host")
        self.port = options.get("port")
        self.user = options.get("user")
        self.database = options.get("database")
        self.connection_type = connection_type
        self.read_only = bool(read_only)
        self.safe_updates_default = connection_type == "production"
        self.safe_updates_wanted = self.safe_updates_default if safe_updates is None else bool(safe_updates)
        self.idle_seconds = idle_seconds
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.run_lock = threading.Lock()
        self.send_lock = threading.Lock()
        self.ready = threading.Event()
        self._routed_startup = False
        self._retrying_startup = False
        self._closing = False  # Permanent across startup attempts.
        self._owner_registry = None
        self._init_client_state()

    def _init_client_state(self):
        """Initialize one client, preserving the tab's locks and readiness event.

        Reuse only during startup, after the previous client and both pipe
        readers have exited. No user statement has been sent at that point.
        """
        self.proc = None
        self.delimiter = None
        self.statement_tag = "/*myflames:editor:%s*/ " % secrets.token_hex(16)
        self.lines = 0
        self.state = "starting"
        self.ended = False
        self.ended_reason = None
        self.end_message = None
        self.engine = None
        self.client_engine = None
        self.server_version = None
        self.server_product = None
        self.connection_id = None
        self.server_identity = None
        self._kill_identity = None
        self.autocommit = None
        self.in_transaction = None
        self.transaction_source = None
        self.transaction_note = None
        self._init_insights()
        self.sql_mode = None
        #: The session's default database, read with DATABASE() in refresh_state.
        self.database_name = None
        # A new-session SHOW GRANTS report cannot describe this session
        # after SET ROLE (including an uncertain/failed send).
        self.privileges_changed = False
        self.safe_updates = None
        self.started_at = _now()
        self.last_used = time.monotonic()
        self.notes = []
        self.run = None
        self._osteps = []           # steps whose stdout block is still open, in send order
        self._esteps = []           # steps waiting for their end-of-statement line
        self._obuf, self._opos, self._ostate = "", 0, "start"
        self._echo_left = 0
        self._entry = None
        self._info_target = None
        self._ebuf = ""
        self._last_error = None
        self._readers = []
        self._eof = 0
        self._conn = None
        self._client_home = None

    # -- starting -------------------------------------------------------------------------

    def start(self, options, defer_safe_updates=False):
        """Start the client and read the session's identity. Raises
        ConnectorError (password redacted by the caller) when it cannot."""
        try:
            try:
                self._start_client(options, defer_safe_updates)
            except _StartupGuardRefused:
                # ProxySQL can lock a frontend to the guard's hostgroup after
                # SET @myflames_target. No later read can undo that lock. The
                # failed attempt has closed its client; start over exactly once.
                for reader in self._readers:
                    reader.join(2)
                if any(reader.is_alive() for reader in self._readers):
                    raise ConnectorError("The MySQL client's output could not be drained. " + SESSION_ENDED_TEXT)
                with self.lock:
                    if self._closing or self.ended:
                        raise ConnectorError(self.end_message or SESSION_ENDED_TEXT)
                    self._init_client_state()
                    self._routed_startup = True
                    self._retrying_startup = False
                self._start_client(options, defer_safe_updates)
        except BaseException:
            self.end("start_failed", quiet=True)
            raise
        finally:
            # Other requests must not observe readiness between attempts.
            self.ready.set()

    def _start_client(self, options, defer_safe_updates):
        """One startup attempt; a routed retry never acquires a write binding."""
        options = dict(options)
        login_guard = options.pop('_login_guard', None)
        conn = MySQLConnection(**options)
        if login_guard is not None:
            conn._login_guard = login_guard
            conn._group_login_started = False
        with self.lock:
            if self._closing or self.ended:
                raise ConnectorError(self.end_message or SESSION_ENDED_TEXT)
            self._conn = conn
        credentials = contextlib.ExitStack()
        try:
            # Through SSH, the session's client needs the tunnel for as long as
            # it runs: held until end() closes the connection object.
            conn.hold_transport()
            defaults = credentials.enter_context(conn._invocation_defaults())
            binary = getattr(defaults, "binary", None) or conn._resolve_binary()
            self._resolved_binary = binary
            self.client_engine = getattr(defaults, "client_engine", None)
            self._client_home = tempfile.TemporaryDirectory(prefix="myflames-editor-")
            env = _clean_env(self._client_home.name)
            from .connector import CLIENT_CHARSET_FLAGS
            # Exclude system option files as well as the private HOME's files.
            argv = [binary, "--defaults-file=" + defaults, "--local-infile=0", "--xml", "--binary-as-hex", "--binary-mode",
                    "--comments", "--force", "--quick", "--unbuffered", "-vv", "--skip-reconnect"]
            argv.extend(CLIENT_CHARSET_FLAGS)
            argv.extend(self.client_flags)
            if _client_features(binary, env).get("skip_commands"):
                argv.append("--skip-commands")
            if options.get("database"):
                argv += ["-D", options["database"]]
            self.delimiter = "__mf%s_" % secrets.token_hex(5)
            conn._check_login_consent()
            try:
                # Registry ownership and Close are checked atomically with
                # spawning. Shutdown may already have removed this tab before
                # it calls end(); neither that gap nor a drained retry may
                # launch an unowned replacement client.
                registry = self._owner_registry
                with registry.lock if registry is not None else contextlib.nullcontext():
                    with self.lock:
                        if (self._closing or self.ended or (registry is not None and
                                (registry._closed or registry.sessions.get(self.tab_id) is not self))):
                            raise ConnectorError(self.end_message or SESSION_ENDED_TEXT)
                        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                     stderr=subprocess.PIPE, env=env)
            except OSError as exc:
                raise ConnectorError("Cannot start the MySQL client: %s" % exc)
            _REGISTRY_PROCS.add(self.proc)
            for stream, feed in ((self.proc.stdout, self._feed_stdout), (self.proc.stderr, self._feed_stderr)):
                thread = threading.Thread(target=self._read, args=(stream, feed), daemon=True)
                thread.start()
                self._readers.append(thread)
            self._write("DELIMITER %s\n" % self.delimiter)
            hello = self._helper(TAG + "SELECT '%s' AS myflames_editor, CONNECTION_ID() AS connection_id, "
                                       "VERSION() AS version, @@version_comment AS version_comment, "
                                       "@@hostname AS hostname, @@port AS port, "
                                       "@@server_id AS server_id, @@autocommit AS autocommit, @@sql_mode AS sql_mode, "
                                       "@@sql_safe_updates AS sql_safe_updates" % secrets.token_hex(6),
                                 timeout=float(options.get("connect_timeout") or 10) + HELPER_TIMEOUT)
            row = self._helper_row(hello)
            if row is None:
                problem = self._helper_problem(hello) or "The MySQL client did not start a session."
                from .connector import _client_auth_error
                auth = _client_auth_error(problem, binary, getattr(conn, '_client_use', None))
                if auth is not None:
                    raise auth
                raise ConnectorError(problem)
            self.connection_id = _int(row.get("connection_id"))
            self.server_version = row.get("version") or ""
            # Both version strings can be customized on MariaDB. Optional
            # variables below supply engine evidence without an unknown-variable
            # error, and a failed identity read must not prevent opening a tab.
            comment = row.get("version_comment")
            identity = (product_identity(self.server_version, comment) if comment
                        else identity_from_version(self.server_version))
            self.engine = identity["engine"]
            self.server_product = identity["product"]
            # The first read establishes that a session opened, not a write
            # or KILL binding. All fields of that binding must come from one
            # COM_QUERY: a router can choose a different backend per packet.
            self.server_identity = None
            self._kill_identity = None
            identity_step = self._helper(
                TAG + "SELECT CONNECTION_ID() AS connection_id, VERSION() AS version, "
                "@@version_comment AS version_comment, @@hostname AS hostname, @@port AS port, "
                "@@server_id AS server_id, @@autocommit AS autocommit, @@sql_mode AS sql_mode, "
                "@@sql_safe_updates AS sql_safe_updates; " + TAG +
                "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('server_uuid', 'wsrep_on', 'wsrep_provider'); " +
                TAG + "SHOW GLOBAL STATUS WHERE Variable_name = 'wsrep_gcomm_uuid'", results=3)
            sets = [entry for entry in identity_step.entries if entry["type"] == "rows"]
            if (identity_step.marker and not identity_step.real_errors() and len(sets) == 3 and
                    [column["name"].lower() for column in sets[1]["columns"]] == ["variable_name", "value"] and
                    (not sets[2]["rows"] or
                     [column["name"].lower() for column in sets[2]["columns"]] == ["variable_name", "value"])):
                bound = self._helper_row(identity_step)
                self.server_identity = self._identity_row(bound)
                variables = {name.lower(): value for name, value in sets[1]["rows"]}
                # MySQL registers server_uuid; MariaDB does not. SHOW succeeds
                # with no row when an optional variable is absent.
                detected_engine = "mariadb" if "server_uuid" not in variables else "mysql"
                kill_identity = dict(self.server_identity or {})
                if detected_engine == "mysql":
                    kill_identity["server_uuid"] = variables.get("server_uuid")
                if "wsrep_on" in variables:
                    # The lexer needs the observed value, not an assumed OFF.
                    # Its guarded packet rechecks the session value before
                    # executing SQL containing MariaDB's /*!99997 marker.
                    kill_identity["wsrep_on"] = variables["wsrep_on"]
                provider = (variables.get("wsrep_provider") or "").strip().lower()
                if provider not in ("", "none") or (variables.get("wsrep_on") or "").upper() in ("ON", "1"):
                    # Galera peers can share id, hostname and port. Bind KILL
                    # to the node UUID, including a missing value to fail closed.
                    status = {name.lower(): value for name, value in sets[2]["rows"]}
                    kill_identity["wsrep_gcomm_uuid"] = status.get("wsrep_gcomm_uuid")
                if self.server_identity is not None:
                    row = bound
                    self.connection_id = _int(bound.get("connection_id"))
                    self.server_version = bound.get("version") or ""
                    product = product_identity(self.server_version, bound.get("version_comment") or "")
                    self.engine = detected_engine
                    self.server_product = "MariaDB" if detected_engine == "mariadb" else product["product"]
                    self._kill_identity = kill_identity
                    if self.engine == "mysql":
                        self.server_identity["server_uuid"] = variables.get("server_uuid")
            if self._routed_startup:
                # Keep identity fields for display only. Even a complete read
                # on this fresh client cannot authorize another guarded packet.
                self.server_identity = None
                self._kill_identity = None
            from . import client_tools
            client_tools.remember_server(client_tools.endpoint_key(options), self.engine, self.server_version)
            self._apply_state(row)
            # The defaults file holds the password: the client has read it.
            credentials.close()
            setup = []
            if self.read_only:
                from . import dbops
                setup.extend(getattr(dbops, "READ_ONLY_SESSION_GUARD", ("SET SESSION TRANSACTION READ ONLY",)))
            if not defer_safe_updates and (self._routed_startup or
                                           self.safe_updates_wanted != bool(self.safe_updates)):
                setup.append("SET SESSION sql_safe_updates = %d" % (1 if self.safe_updates_wanted else 0))
            try:
                guard = self._write_session_guard()
            except ValueError:
                # Optional identity reads must not prevent ordinary reads.
                # Apply and verify startup protections without a binding;
                # later writes, session changes and KILL still require one.
                guard = None
            self._prepare_startup(setup, guard, defer_safe_updates)
            with self.lock:
                if self.ended:
                    raise ConnectorError(self.end_message or SESSION_ENDED_TEXT)
                self.state = "idle"
                self.last_used = time.monotonic()
        except _StartupGuardRefused:
            # This is still one starting session. EOF from the first client's
            # readers must not expose an ended, replaceable tab. Close remains
            # permanent and is checked again after both readers have drained.
            with self.lock:
                self._retrying_startup = True
            self._terminate()
            conn.close()
            raise
        except BaseException:
            conn.close()
            self.end("start_failed", quiet=True)
            raise
        finally:
            credentials.close()

    def _prepare_startup(self, setup, guard, defer_safe_updates):
        """Apply and verify the required protections, with or without a binding."""
        for statement in setup:
            step = self._helper(TAG + statement, results=0, guard=guard)
            if not step.marker or self.ended:
                raise ConnectorError("Could not verify session setup. " + SESSION_ENDED_TEXT)
            if step.real_errors():
                raise ConnectorError("Could not prepare the session (%s): %s"
                                     % (statement, step.real_errors()[0].get("message")))
        if not self.refresh_state(guard=guard):
            raise ConnectorError("Could not verify session setup. " + SESSION_ENDED_TEXT)
        if not defer_safe_updates and self.safe_updates != self.safe_updates_wanted:
            raise ConnectorError("The session's safe-updates setting could not be verified. " +
                                 SESSION_ENDED_TEXT)
        if self.read_only:
            # SHOW supports both engine names without relying on version text.
            step = self._helper(TAG + "SHOW SESSION VARIABLES WHERE Variable_name IN "
                                "('transaction_read_only', 'tx_read_only')", guard=guard)
            sets = [entry for entry in step.entries if entry["type"] == "rows"]
            if (not step.marker or step.real_errors() or len(sets) != 1 or not sets[0]["rows"] or
                    any(len(row) != 2 or str(row[0]).lower() not in ("transaction_read_only", "tx_read_only") or
                        str(row[1]).upper() not in ("ON", "1") for row in sets[0]["rows"])):
                raise ConnectorError("The session's read-only transaction setting could not be verified. " +
                                     SESSION_ENDED_TEXT)

    def _helper_problem(self, step):
        errors = step.real_errors()
        if errors:
            e = errors[0]
            return "%s%s" % ("ERROR %s: " % e["code"] if e.get("code") else "", e.get("message") or "")
        with self.lock:
            notes = [n for n in self.notes if n]
            step_notes = list(step.notes)
        # A failed batch login can print its only diagnostic on stdout,
        # before any echo or result. The parser attributes that to hello.
        from .connector import _EXPIRED_PASSWORD_HINT, EXPIRED_PASSWORD_MESSAGE
        if any(_EXPIRED_PASSWORD_HINT in note for note in notes + step_notes):
            return EXPIRED_PASSWORD_MESSAGE
        if notes:
            return notes[-1]
        if self.ended:
            return self.end_message
        return None

    # -- pipes ----------------------------------------------------------------------------

    def _read(self, stream, feed):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fd = stream.fileno()
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                tail = decoder.decode(b"", final=True)
                with self.lock:
                    if tail:
                        feed(tail)
                    feed(None)
                try:
                    stream.close()
                except OSError:
                    pass
                return
            text = decoder.decode(data)
            if text:
                with self.lock:
                    feed(text)

    def _write(self, text):
        data = text.encode("utf-8")
        self.lines += text.count("\n")
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            self.end("client_exited")
            raise ConnectorError(SESSION_ENDED_TEXT)

    def _tag_statement(self, send, sql_mode=_PLAN_MODE_UNSET):
        """Tag SQL without changing its quoted text or executable comments.

        Tag suffix statements too: a custom DELIMITER can group several SQL
        statements, and PROCESSLIST exposes the currently executing suffix.
        CREATE bodies stay intact, including after an earlier statement:
        the DDL gate refuses definitions batched with trailing SQL, and
        tokens must not enter stored bodies.
        """
        from . import dbops
        context = _lexer_session_context(self)
        if sql_mode is not _PLAN_MODE_UNSET:
            context["sql_mode"] = sql_mode
        flags = dbops.sql_mode_flags(context["sql_mode"], self.engine)
        backslash = flags["backslash_escapes"] if flags is not None else None
        _check_executable_comments(send, backslash_escapes=backslash, **context)
        lexer_args = {key: context[key] for key in ("engine", "server_version", "wsrep_on", "sql_mode", "client_engine")}
        escaping = True if backslash is None else backslash
        definition = next((unit["start"] for unit in dbops.split_statements(
            send, backslash_escapes=escaping, **lexer_args)
            if re.match(r"\s*CREATE" + _SQL_BOUNDARY, unit["code"], re.I | re.ASCII) or _ROUTINE_CREATE.match(unit["code"])), len(send))
        # Keep the guard's final semicolon and all stored definition text.
        terminal = ";" if send.endswith(";") else ""
        send = send[:-1] if terminal else send
        suffix = send[definition:]
        prefix = send[:definition]
        pieces, active = [self.statement_tag], False
        for kind, start, end in dbops.lex_sql(prefix, escaping, **lexer_args)["tokens"]:
            if kind == "executable_open":
                active = True
            text = prefix[start:end]
            pieces.append(text.replace(";", "; " + self.statement_tag) if kind == "code" and not active else text)
            if kind == "executable_close":
                active = False
        pieces.extend((suffix, terminal))
        return "".join(pieces)

    def _guarded_statement(self, guard, send, delimiter=";", use_session_mode=True, sql_mode=_PLAN_MODE_UNSET):
        """Revalidate the planned unit with this session's mode and delimiter.

        A custom delimiter can contain a stored-program body or several
        reviewed server statements. Helpers keep the stricter ';' default.
        """
        from . import dbops
        context = _lexer_session_context(self)
        if sql_mode is not _PLAN_MODE_UNSET:
            context["sql_mode"] = sql_mode
        elif not use_session_mode:
            context["sql_mode"] = None
        flags = dbops.sql_mode_flags(context["sql_mode"], self.engine, self.server_version)
        backslash = flags["backslash_escapes"] if flags is not None else None
        if backslash is not None:
            # State refreshes can be routed independently of a write. Bind
            # the reading used below to this packet's executing session,
            # even when another backend reports the same connection id.
            if len(guard) != 1 or not guard[0].endswith(");"):
                raise ValueError("Invalid editor target guard")
            mode_check = "(FIND_IN_SET('NO_BACKSLASH_ESCAPES', @@SESSION.sql_mode) > 0)"
            guard = [guard[0][:-2] + " OR NOT (%s <=> %d));" % (mode_check, int(not backslash))]
            checks = [("ANSI_QUOTES", flags["ansi_quotes"])]
            if self.engine == "mariadb":
                checks.extend((("MSSQL", flags["mssql"]),
                               ("ORACLE", dbops.oracle_mode(context["sql_mode"], self.engine))))
            for name, enabled in checks:
                guard = [guard[0][:-2] + " OR NOT ((FIND_IN_SET('%s', @@SESSION.sql_mode) > 0) <=> %d));"
                         % (name, int(enabled))]
        if self.engine == "mariadb" and context["wsrep_on"] is False:
            # A cached OFF must not authorize /*!99997 after SET wsrep_on.
            # MariaDB sql/sys_vars.cc:6564-6570: session boolean wsrep_on.
            guard = [guard[0][:-2] + " OR NOT (@@SESSION.wsrep_on <=> 0));"]
        tagged = (tuple(self._tag_statement(s, context["sql_mode"]) for s in send) if isinstance(send, tuple)
                  else self._tag_statement(send, context["sql_mode"]))
        return dbops.target_guarded_script([self._tag_statement(guard[0], context["sql_mode"])], [tagged],
                                          backslash_escapes=backslash,
                                          delimiter=delimiter, engine=self.engine,
                                          server_version=self.server_version,
                                          wsrep_on=context["wsrep_on"],
                                          sql_mode=context["sql_mode"],
                                          client_engine=context["client_engine"])

    def _send(self, kind, send, unit=None, limit=None, results=None, kill_ok=False, guard=None, delimiter=";",
              setup=None, sql_mode=_PLAN_MODE_UNSET):
        """Write one statement; returns its Step. Holds send_lock so a KILL in
        flight is never followed by a new statement."""
        with self.send_lock:
            with self.lock:
                if self.ended:
                    raise ConnectorError(self.end_message or SESSION_ENDED_TEXT)
            if guard is not None:
                packet = (setup["send"], send) if setup is not None else send
                script, packet_lines = self._guarded_statement(guard, packet, delimiter, sql_mode=sql_mode)
                header, body = script.split("\n", 1)
                self.delimiter = header[len("DELIMITER "):]
                wire = body[:-(len(self.delimiter) + 2)]
                self._write(header + "\n")
            else:
                wire = self._tag_statement(send, sql_mode)
                while self.delimiter in wire:
                    self.delimiter = "__mf%s_" % secrets.token_hex(5)
                    self._write("DELIMITER %s\n" % self.delimiter)
                body = wire + "\n" + self.delimiter + "\n"
            first = self.lines + 1
            marker = first + wire.count("\n") + 2
            step = Step(kind, send, first, marker, unit=unit, limit=limit, results=results)
            step.setup = setup
            step.echo_lines = wire.count("\n") + 1
            step.guarded = step.guard_pending = guard is not None
            step.guard_ok_pending = step.guarded
            if step.guarded:
                from .dbops import TargetPacketLines
                step.packet_lines = TargetPacketLines((first,), packet_lines.markers,
                                                     (unit,) if unit is not None else packet_lines.indices,
                                                     packet_lines.sizes)
            step.kill_ok = bool(kill_ok)
            with self.lock:
                self._osteps.append(step)
                self._esteps.append(step)
                step.sent_at = time.monotonic()
                if self.run is not None and not self.run.done.is_set():
                    self.run.steps.append(step)
                    self.run.current = step
            self._write(body + "delimiter\n")
            return step

    def _attribute_history(self, run, rows, boundary):
        # Keep the public helper exclusion and exact user-SQL attribution in
        # EditorInsights, removing only this session's private leading tag.
        rows = [dict(row, sql_text=row["sql_text"][len(self.statement_tag):])
                if isinstance(row.get("sql_text"), str) and row["sql_text"].startswith(self.statement_tag)
                else row for row in rows]
        return super(EditorSession, self)._attribute_history(run, rows, boundary)

    def _wait(self, predicate, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.cond:
            while not predicate():
                if self.ended:
                    return predicate()
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self.cond.wait(0.5 if remaining is None else min(0.5, remaining))
            return True

    def _helper(self, send, results=1, timeout=HELPER_TIMEOUT, guard=None):
        step = self._send("helper", send, results=results, guard=guard)
        ok = self._wait(lambda: step.marker and (step.closed or bool(step.real_errors()) or results == 0),
                        timeout)
        if not ok and not self.ended:
            self.end("protocol", "The MySQL client stopped answering. " + SESSION_ENDED_TEXT)
        if guard is not None:
            self._check_helper_guard(step)
        return step

    def _check_helper_guard(self, step):
        """End on refusal, except for the startup fallback to unbound reads."""
        errors = step.real_errors()
        with self.lock:
            startup = self.state == "starting"
            if startup:
                # Establish this before checking the reply: an absent guard
                # acknowledgement drains the client inside that check.
                self._retrying_startup = True
        failure = self._packet_guard_failure(step, errors)
        if startup and (not failure or failure["outcome"] != "identity_mismatch"):
            with self.lock:
                self._retrying_startup = False
                if self._eof == 2:
                    self._on_eof()
        if failure:
            message = "The server session could not be confirmed. The requested operation was not sent."
            if startup and failure["outcome"] == "identity_mismatch":
                raise _StartupGuardRefused(message, failure["outcome"])
            self.end(failure["outcome"], message + " " + SESSION_ENDED_TEXT)
            raise EditorError(message, failure["outcome"])

    def _helper_row(self, step):
        rows = [e for e in step.entries if e["type"] == "rows"]
        if not rows or not rows[0]["rows"]:
            return None
        names = [c["name"] for c in rows[0]["columns"]]
        return dict(zip(names, rows[0]["rows"][0]))

    # -- stdout ---------------------------------------------------------------------------

    def _note(self, text):
        if text and len(self.notes) < NOTE_CAP:
            self.notes.append(text[:2000])

    def _line(self):
        newline = self._obuf.find("\n", self._opos)
        if newline < 0:
            return None
        line = self._obuf[self._opos:newline]
        if line.endswith("\r"):
            line = line[:-1]  # framing only; XML values never go through _line
        self._opos = newline + 1
        return line

    def _feed_stdout(self, text):
        if text is None:
            self._eof += 1
            for step in self._osteps:
                step.closed = True
            self._osteps = []
            self._on_eof()
            return
        if self._opos > 1048576:
            self._obuf, self._opos = self._obuf[self._opos:], 0
        self._obuf += text
        try:
            self._parse_stdout()
        except _Protocol as exc:
            self._note("Unexpected client output: %s" % exc)
            self._mark_ended("protocol", "myflames could not read the MySQL client's output. " + SESSION_ENDED_TEXT)
            threading.Thread(target=self._terminate, daemon=True).start()
        self.cond.notify_all()

    def _close_block(self):
        step = self._osteps.pop(0)
        step.closed = True
        self._entry = None
        self._info_target = None
        self._ostate = "start"

    def _parse_stdout(self):
        buf = self._obuf
        while True:
            state = self._ostate
            step = self._osteps[0] if self._osteps else None
            if step is None and state != "start":
                self._ostate = "start"
                continue
            if state == "start":
                line = self._line()
                if line is None:
                    return
                if step is None:
                    if line and line != "Bye":
                        self._note(line)
                    continue
                if line == STATUS_ECHO:
                    self._echo_left = step.echo_lines
                    self._ostate = "echo"
                    continue
                if line == "":
                    continue
                # The client printed nothing for this statement before (no echo).
                self._ostate = "body"
                self._body_line(step, line)
                continue
            if state == "echo":
                while self._echo_left > 0:
                    line = self._line()
                    if line is None:
                        return
                    self._echo_left -= 1
                line = self._line()
                if line is None:
                    self._echo_left = 0
                    self._ostate = "echo_end"
                    return
                if line != STATUS_ECHO:
                    raise _Protocol("the statement echo had an unexpected length")
                self._ostate = "echo_blank"
                continue
            if state == "echo_end":
                line = self._line()
                if line is None:
                    return
                if line != STATUS_ECHO:
                    raise _Protocol("the statement echo had an unexpected length")
                self._ostate = "echo_blank"
                continue
            if state == "echo_blank":
                line = self._line()
                if line is None:
                    return
                if line != "":
                    raise _Protocol("the statement echo was not followed by a blank line")
                self._ostate = "body"
                continue
            if state == "body":
                if buf.startswith("<", self._opos):
                    end = _frame_end(buf, self._opos, XML_HEAD)
                    if end is None:
                        return
                    if end >= 0:
                        self._opos = end
                        self._ostate = "xml_head"
                        continue
                line = self._line()
                if line is None:
                    return
                if line == STATUS_ECHO and len(self._osteps) > 1:
                    self._close_block()
                    self._echo_left = self._osteps[0].echo_lines
                    self._ostate = "echo"
                    continue
                self._body_line(step, line)
                continue
            if state == "xml_head":
                quote = buf.find('"', self._opos)
                if quote < 0:
                    return
                if len(buf) - quote < len(XML_HEAD_END):
                    return
                if not buf.startswith(XML_HEAD_END, quote):
                    raise _Protocol("a result set header was not recognized")
                self._opos = quote + len(XML_HEAD_END)
                entry = {"type": "rows", "columns": [], "rows": [], "truncated": False, "stopped": None,
                         "server_rows": None, "warnings": 0, "complete": False}
                if step.guard_pending:
                    # The builder's first result set is private transport
                    # evidence. Keep it outside user results and their budgets.
                    step.guard_entry = entry
                else:
                    self._append_entry(step, entry)
                self._entry = entry
                self._info_target = None
                self._ostate = "xml"
                continue
            if state == "xml":
                row_end = _frame_end(buf, self._opos, ROW_OPEN)
                result_end = _frame_end(buf, self._opos, RESULT_CLOSE)
                if row_end is not None and row_end >= 0:
                    entry = self._entry
                    if entry["truncated"] or (step.limit is not None and len(entry["rows"]) >= step.limit):
                        if not entry["truncated"]:
                            entry["truncated"] = True
                            killed = self._request_kill(step, "row_limit")
                            entry["stopped"] = "row_limit" if killed else "row_limit_read"
                        self._ostate = "discard"
                        continue
                    self._opos = row_end
                    self._ostate = "row"
                    continue
                if result_end is not None and result_end >= 0:
                    self._opos = result_end
                    self._entry["complete"] = True
                    if self._entry is step.guard_entry:
                        if self._entry["rows"] != [[step.packet_lines.markers[0]]]:
                            raise _Protocol("the server identity acknowledgement was not recognized")
                        step.guard_pending = False
                    self._ostate = "body"
                    continue
                if row_end is None or result_end is None:
                    return
                raise _Protocol("a result set was not recognized")
            if state == "row":
                end = _ROW_END.search(buf, self._opos)
                if (end is None and len(buf) - self._opos > ROW_CHAR_CAP) or (
                        end is not None and end.start() - self._opos > ROW_CHAR_CAP):
                    self._truncate(step, self._entry, "size_limit")
                    self._ostate = "discard"
                    continue
                if end is None:
                    return
                self._ostate = "xml"
                self._row(step, buf[self._opos:end.start()])
                self._opos = end.end()
                continue
            if state == "discard":
                end = _RESULT_END.search(buf, self._opos)
                if end is None:
                    self._opos = max(self._opos, len(buf) - len(RESULT_CLOSE) - 1)
                    return
                self._opos = end.end()
                self._entry["complete"] = True
                self._ostate = "body"
                continue
            raise _Protocol("unknown parser state")

    def _row(self, step, text):
        entry = self._entry
        values, names, at = [], [], 0
        for match in _FIELD.finditer(text):
            if match.start() != at:
                raise _Protocol("a row was not recognized")
            at = match.end()
            names.append(xml_unescape(match.group(1)))
            value = match.group(2)
            values.append(None if value is None else xml_unescape(value))
        if at != len(text):
            raise _Protocol("a row was not recognized")
        if entry is step.guard_entry:
            if entry["rows"] or values != [step.packet_lines.markers[0]]:
                raise _Protocol("the server identity acknowledgement was not recognized")
            entry["rows"].append(values)
            return
        columns = [] if entry["columns"] else [{"name": name} for name in names]
        # Charge the JSON actually retained, plus conservative allowances for
        # per-column export metadata and row separators. ASCII escaping is the
        # worst case, including control characters and astral Unicode.
        serialized = len(json.dumps(values, ensure_ascii=True)) + 2
        serialized += sum(128 + len(json.dumps(c["name"], ensure_ascii=True)) for c in columns)
        memory = 64 + sum(64 + (4 * len(v) if v is not None else 0) for v in values)
        memory += sum(256 + 4 * len(c["name"]) for c in columns)
        reason = self._charge(step, memory, serialized, cells=len(values))
        if reason:
            self._truncate(step, entry, reason)
            self.run.limit_entry = entry
            self._ostate = "discard"
            return
        if columns:
            entry["columns"] = columns
        entry["rows"].append(values)

    def _charge(self, step, memory, serialized, cells=0, entries=0):
        """Reserve all budgets atomically, before retaining anything."""
        run = self.run
        if run is None or step.kind != "user":
            return None
        reason = run.budget_reason
        if reason is None:
            if run.result_sets + entries > RESULT_SET_CAP - 1:
                reason = "result_limit"
            elif run.cells + cells > RESULT_CELL_CAP:
                reason = "cell_limit"
            elif run.serialized + serialized > RESULT_BYTE_CAP:
                reason = "byte_limit"
            elif run.retained + memory > RESULT_MEMORY_CAP:
                reason = "memory_limit"
        if reason:
            run.budget_reason = reason
            return reason
        run.retained += memory
        run.serialized += serialized
        run.cells += cells
        run.result_sets += entries
        return None

    def _truncate(self, step, entry, reason):
        entry["truncated"] = True
        killed = self._request_kill(step, reason)
        entry["stopped"] = "size_limit" if killed else "size_limit_read"
        entry["budget_reason"] = reason

    def _append_entry(self, step, entry):
        reason = self._charge(step, 1024, 1024, entries=1)
        if not reason:
            step.entries.append(entry)
            return True
        if entry["type"] == "rows":
            self._truncate(step, entry, reason)
        # Keep a single bounded notice even when empty sets or OK packets
        # exhaust the budget. Later sets are drained without retaining entries.
        if self.run.limit_entry is None:
            notice = entry if entry["type"] == "rows" else {
                "type": "rows", "columns": [], "rows": [], "server_rows": None,
                "warnings": 0, "complete": True}
            if notice is not entry:
                self._truncate(step, notice, reason)
            step.entries.append(notice)
            self.run.limit_entry = notice
        return False

    def _body_line(self, step, line):
        match = _ROWS_LINE.match(line) or _EMPTY_LINE.match(line)
        if match:
            if self._entry is not None:
                last = self._entry
                if line.startswith("Empty"):
                    last["server_rows"], last["warnings"] = 0, int(match.group(1) or 0)
                else:
                    last["server_rows"], last["warnings"] = int(match.group(1)), int(match.group(2) or 0)
            self._info_target = None
            return
        match = _OK_LINE.match(line)
        if match:
            if step.guard_ok_pending:
                step.guard_ok_pending = False
                return
            affected = match.group(1)
            entry = {"type": "ok", "affected_rows": int(affected) if affected is not None else None,
                     "warnings": int(match.group(2) or 0), "info": None}
            self._info_target = entry if self._append_entry(step, entry) else None
            return
        if line == "":
            self._info_target = None
            self._summary_done(step)
            return
        if line == "Bye":
            return
        if self._info_target is not None:
            info = self._info_target.get("info")
            reason = self._charge(step, 4 * len(line) + 1, len(json.dumps(line, ensure_ascii=True)) + 2)
            if reason:
                self._append_entry(step, {"type": "ok"})  # bounded truncation notice
                self._info_target = None
            else:
                self._info_target["info"] = line if not info else info + "\n" + line
            return
        if len(step.notes) < NOTE_CAP:
            step.notes.append(line[:2000])

    def _summary_done(self, step):
        """A helper's block is complete at the blank line after its summary
        (``1 row in set`` or ``Query OK``); a user statement's block ends
        where the next echo starts (it may return several result sets)."""
        if step.expect_results is None:
            return
        done = sum(1 for e in step.entries if e["type"] == "ok" or e.get("server_rows") is not None)
        if done >= max(1, step.expect_results) and self._osteps and self._osteps[0] is step:
            self._close_block()

    # -- stderr ---------------------------------------------------------------------------

    def _feed_stderr(self, text):
        if text is None:
            self._eof += 1
            if self._ebuf:
                self._stderr_line(self._ebuf)
                self._ebuf = ""
            self._on_eof()
            return
        self._ebuf += text
        while True:
            newline = self._ebuf.find("\n")
            if newline < 0:
                break
            line, self._ebuf = self._ebuf[:newline], self._ebuf[newline + 1:]
            self._stderr_line(line[:-1] if line.endswith("\r") else line)
        self.cond.notify_all()

    def _step_at(self, line):
        for step in self._esteps:
            if step.first_line <= line <= step.marker_line:
                return step
        return None

    def _stderr_line(self, line):
        """One stderr line. A statement has at most one server error: once it
        started, every line is part of its message (server messages quote
        table data, which may hold text that looks like another ERROR line)
        until the exact end-of-statement line of that statement."""
        open_error = self._last_error
        if open_error is not None:
            step, error = open_error
            if line.startswith(MARKER_PREFIX % step.marker_line):
                self._end_step(step)
                return
            if line == error.get("first_line"):
                return      # the client prints a fetch error twice
            error["message"] += "\n" + line
            return
        match = _ERROR_LINE.match(line)
        if not match:
            self._note(line)
            return
        code = int(match.group(1)) if match.group(1) else None
        at = int(match.group(3)) if match.group(3) else None
        step = self._step_at(at) if at is not None else (self._esteps[0] if self._esteps else None)
        if step is not None and code is None and line.startswith(MARKER_PREFIX % step.marker_line):
            self._end_step(step)
            return
        if step is None:
            self._note(line)
            return
        error = {"code": code, "sqlstate": match.group(2) or None, "line": at, "message": match.group(4),
                 "first_line": line}
        step.errors.append(error)
        self._last_error = (step, error)

    def _end_step(self, step):
        step.marker = True
        step.done_at = time.monotonic()
        if step in self._esteps:
            self._esteps.remove(step)
        self._last_error = None
        self.cond.notify_all()

    def _on_eof(self):
        if self._eof < 2:
            return
        # A statement still out when the client exits never got its end line:
        # whether it ran is unknown (it is not marked as finished).
        now = time.monotonic()
        for step in self._esteps:
            step.unfinished = True
            step.done_at = now
        self._esteps = []
        self._last_error = None
        if not self.ended and not self._retrying_startup:
            self._mark_ended("client_exited", SESSION_ENDED_TEXT)
        self.cond.notify_all()

    # -- kills ----------------------------------------------------------------------------

    def _request_kill(self, step, reason):
        """Called by the stdout reader once a result set passed its limit.
        Only a statement that is one plain read is stopped with KILL QUERY;
        any other (CALL, several statements, a write) keeps running and the
        rows past the limit are read and dropped, so a KILL never undoes a
        write the statement was about to make."""
        if step.kill_requested or step.kind != "user" or not step.kill_ok:
            return False
        step.kill_requested = True
        step.kill_reason = reason
        run = self.run
        if run is None or run.options is None:
            return True
        thread = threading.Thread(target=self._kill_query, args=(run.options, step), daemon=True)
        run.kill_threads.append(thread)
        thread.start()
        return True

    def _checked_helper_binary(self):
        """A tab pins its client, but a later helper must check it again."""
        from .connector import checked_program, bundled_client_dir, _same_dir
        binary = self._resolved_binary
        bundle = bundled_client_dir()
        return checked_program(binary, binary, bundled=bool(bundle and _same_dir(
            os.path.dirname(os.path.realpath(binary)), bundle)))

    def _kill_query(self, options, step):
        """KILL QUERY this session's statement from a second connection, only
        while *step* still runs. Returns (sent/skipped/finished/failed, text)."""
        with self.send_lock:
            with self.lock:
                if step.marker or getattr(step, "unfinished", False) or self.ended or not self.connection_id:
                    return "skipped", None
                connection_id = self.connection_id
            try:
                kill_options = dict(options)
                kill_options["query_timeout"] = KILL_TIMEOUT
                kill_options["binary"] = self._checked_helper_binary()
                if self._send_kill(kill_options, connection_id) is False:
                    return "finished", None
                with self.lock:
                    step.kill_sent_at = time.monotonic()
                return "sent", None
            except (ConnectorError, ValueError) as exc:
                text = str(exc)
                password = options.get("password")
                if password:
                    text = text.replace(password, "[redacted]")
                with self.lock:
                    self._note("KILL QUERY failed: " + text)
                return "failed", text

    def _send_kill(self, options, connection_id):
        """Guard the helper's packet before it can kill this tab's statement.

        A successful probe on another connection cannot authorize a KILL
        behind a router. Server identity and target-token ownership are
        checked in the KILL packet; batch mode and no reconnect stop at a
        guard error.
        """
        from . import dbops
        try:
            guard = dbops.target_session_guard(self.engine, getattr(self, "_kill_identity", None))
            guard = self._kill_statement_guard(guard, connection_id)
            # A fresh helper inherits the global mode, not the tab's mode.
            # Generated KILL SQL must validate under both readings instead.
            script, packet_lines = self._guarded_statement(guard, TAG + "KILL QUERY %d" % connection_id,
                                                           use_session_mode=False)
        except ValueError:
            raise ConnectorError("The server could not be confirmed. No KILL QUERY was sent.") from None
        try:
            options = dict(options)
            login_guard = options.pop('_login_guard', None) or getattr(self._conn, '_login_guard', None)
            control = MySQLConnection(**options)
            if login_guard is not None:
                control._login_guard = login_guard
                control._group_login_started = False
            with control:
                control.run_write(script, timeout=KILL_TIMEOUT)
        except ConnectorError as exc:
            # A processlist denial, missing table or hidden/absent target
            # cannot establish ownership either. No retry or weaker fallback.
            if dbops.target_guard_failed(exc, packet_lines):
                raise ConnectorError(
                    "The server could not be confirmed. No KILL QUERY was sent. Any running statement continues. "
                    "Stop must verify that it belongs to this tab; the server may hide or rewrite its text, "
                    "including statements containing passwords. A statement running inside a routine or "
                    "prepared statement can show its inner SQL instead; it continues when ownership "
                    "cannot be confirmed.") from None
            code = dbops.error_code(dbops.target_error_text(exc))
            acknowledged = all(m in getattr(exc, "guard_markers", ()) for m in packet_lines.markers)
            if acknowledged and code == 1094:  # ER_NO_SUCH_THREAD, after the ownership check.
                return False
            # Only generated SQL runs here. Do not surface its echoed packet,
            # private token, or server text that quotes any part of that SQL.
            if acknowledged and code is not None and not 2000 <= code <= 2999:
                raise ConnectorError("The server rejected KILL QUERY (error %d). The statement may still be running."
                                     % code) from None
            raise ConnectorError("Stop could not be completed; whether KILL QUERY ran is unknown. "
                                 "The statement may still be running.") from None

    def _kill_statement_guard(self, guard, connection_id):
        """Require this tab's currently running SQL, not merely a recycled id.

        MySQL sql/sql_show.cc:3846-3854,5642-5650 supplies ID/INFO even
        without Performance Schema. Its P_S processlist is empty when OFF
        (storage/perfschema/ha_perfschema.cc:111-112,1759-1762).
        MariaDB sql/sql_show.cc:3471,3522-3525,10565-10575 supplies ID/INFO.
        The leading token fits even a truncated INFO. Binary comparison
        avoids case folding or a LIKE wildcard changing the ownership check.
        """
        if isinstance(connection_id, bool) or not isinstance(connection_id, int) or connection_id <= 0:
            raise ValueError("Invalid editor connection id")
        table = "information_schema.PROCESSLIST"
        # statement_tag is generated locally from token_hex, never SQL input.
        prefix = self.statement_tag.rstrip()
        missing = ("NOT EXISTS (SELECT 1 FROM %s WHERE ID = %d AND "
                   "LEFT(BINARY INFO, %d) = BINARY '%s' AND ID <> CONNECTION_ID())"
                   % (table, connection_id, len(prefix), prefix))
        suffix = ");"
        if not guard[0].endswith(suffix):
            raise ValueError("Invalid editor target guard")
        return [guard[0][:-len(suffix)] + " OR " + missing + suffix]

    def _write_session_guard(self):
        """Bind writes to the recorded server and backend session together.

        A same-server proxy session switch must not move COMMIT or DML out
        of the tab's transaction. CONNECTION_ID() returns the executing THD's
        reported id (MySQL sql/item_func.cc:1532-1541; MariaDB :836-843).
        """
        from . import dbops
        identity = dict(self.server_identity or {})
        # Optional startup reads are part of the binding. Never silently
        # rebuild a weaker guard when their complete identity is unavailable.
        complete = getattr(self, "_kill_identity", None)
        unavailable = "The server could not be confirmed. Nothing was sent. Open a new tab."
        if not complete or not identity or any(complete.get(k) != v for k, v in identity.items()):
            raise ValueError(unavailable)
        value = identity.get("connection_id")
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]{1,20}", str(value)):
            raise ValueError("The tab's server session id is missing or invalid. Nothing was sent. Open a new tab.")
        connection_id = int(value)
        if not connection_id or connection_id != self.connection_id:
            raise ValueError("The tab's server session changed. Nothing was sent. Open a new tab.")
        try:
            guard = dbops.target_session_guard(self.engine, complete)
        except ValueError:
            raise ValueError(unavailable) from None
        return [guard[0][:-2] + " OR CONNECTION_ID() <> %d);" % connection_id]

    # -- state ----------------------------------------------------------------------------

    def _apply_state(self, row):
        if row.get("autocommit") is not None:
            self.autocommit = row.get("autocommit") == "1"
        if row.get("sql_mode") is not None:
            self.sql_mode = row.get("sql_mode")
        if row.get("sql_safe_updates") is not None:
            self.safe_updates = row.get("sql_safe_updates") == "1"
        if row.get("in_transaction") is not None:
            self.in_transaction = row.get("in_transaction") == "1"
            self.transaction_source = "in_transaction"
            self.transaction_note = None
        # DATABASE() is NULL when the session has no default database.
        self.database_name = row.get("database_name") or None

    @property
    def backslash_escapes(self):
        return "NO_BACKSLASH_ESCAPES" not in (self.sql_mode or "").upper()

    @staticmethod
    def _identity_row(row):
        keys = ("connection_id", "version", "hostname", "port", "server_id")
        if row is None or any(row.get(key) in (None, "") for key in keys):
            return None
        return {key: row[key] for key in keys + (("server_uuid",) if "server_uuid" in row else ())}

    def _probe(self):
        """Re-read the actual session, never return the startup identity cache.

        Use variables available on both engines (MariaDB has no server_uuid).
        Call only while holding run_lock, before a run, never between its units.
        """
        step = self._helper(TAG + "SELECT CONNECTION_ID() AS connection_id, VERSION() AS version, "
                                  "@@hostname AS hostname, @@port AS port, @@server_id AS server_id" +
                            (", @@global.server_uuid AS server_uuid" if self.engine == "mysql" else ""))
        if not step.marker or self.ended or step.real_errors():
            return None
        return self._identity_row(self._helper_row(step))

    def _verify_identity(self, audit, statements):
        """Refuse a write if its live target differs or cannot be verified."""
        try:
            current = self._probe()
        except ConnectorError:
            current = None
        if current is not None and current == self.server_identity:
            return
        code = "identity_mismatch" if current is not None else "identity_unverified"
        message = ("The server session changed." if current is not None else
                   "The server session identity could not be verified.") + " No requested statements were sent."
        if audit is not None:
            audit.outcome(statements, "refused", outcome=code, message=message, target=self.server_identity)
        self.end(code, message + " " + SESSION_ENDED_TEXT)
        raise EditorError(message, code)

    def refresh_state(self, guard=None):
        """Read autocommit, the transaction state and sql_mode (tagged helpers)."""
        guard_options = {"guard": guard} if guard is not None else {}
        if self.engine == "mysql":
            # PS_CURRENT_THREAD_ID() exists from MySQL 8.0.16 (sql/item_create.cc);
            # before it (5.7) the session's thread is found by its connection id.
            known = parse_version_tuple(self.server_version or "", leading=True, default=None)
            thread = ("(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID = CONNECTION_ID())"
                      if known is not None and known < (8, 0, 16) else "PS_CURRENT_THREAD_ID()")
            probe = self._helper(TAG + "SELECT (SELECT ENABLED FROM performance_schema.setup_consumers WHERE "
                                       "NAME = 'events_transactions_current') AS consumer, (SELECT ENABLED FROM "
                                       "performance_schema.setup_instruments WHERE NAME = 'transaction') AS "
                                       "instrument, (SELECT INSTRUMENTED FROM performance_schema.threads WHERE "
                                       "THREAD_ID = " + thread + ") AS instrumented, (SELECT STATE FROM "
                                       "performance_schema.events_transactions_current WHERE THREAD_ID = "
                                       + thread + ") AS state", **guard_options)
            row = self._helper_row(probe) if not probe.real_errors() else None
            with self.lock:
                self.transaction_source = "performance_schema"
                if row is None:
                    self.in_transaction = None
                    self.transaction_note = ("The transaction state is unknown: performance_schema."
                                             "events_transactions_current could not be read.")
                elif "YES" != (row.get("consumer") or "") or "YES" != (row.get("instrument") or "") \
                        or "YES" != (row.get("instrumented") or ""):
                    self.in_transaction = None
                    self.transaction_note = ("The transaction state is unknown: the transaction instrument, the "
                                             "events_transactions_current consumer or this thread's "
                                             "instrumentation is off in Performance Schema.")
                else:
                    self.in_transaction = row.get("state") == "ACTIVE"
                    self.transaction_note = None
            extra = ""
        else:
            extra = ", @@in_transaction AS in_transaction"
        token = secrets.token_hex(6)
        step = self._helper(TAG + "SELECT '%s' AS myflames_editor, CONNECTION_ID() AS connection_id, @@autocommit AS "
                                  "autocommit, @@sql_mode AS sql_mode, @@sql_safe_updates AS sql_safe_updates, "
                                  "DATABASE() AS database_name%s"
                            % (token, extra), **guard_options)
        row = self._helper_row(step) if step.marker and not step.real_errors() else None
        if row is not None and (row.get("autocommit") not in ("0", "1") or
                                row.get("sql_safe_updates") not in ("0", "1") or
                                not isinstance(row.get("sql_mode"), str) or "database_name" not in row):
            # A cached setting is not a successful readback of this packet.
            row = None
        lost = False
        with self.lock:
            if row is None:
                problem = step.real_errors()
                lost = bool(problem) and problem[0].get("code") in LOST_CONNECTION_CODES
        if row is None:
            if lost:
                self.end("connection_lost", SESSION_ENDED_TEXT)
            return False
        with self.lock:
            reconnected = (not self._routed_startup and self.connection_id is not None and
                           _int(row.get("connection_id")) != self.connection_id)
            if reconnected:
                self._mark_ended("reconnected", "The client reconnected to the server. " + SESSION_ENDED_TEXT)
            else:
                self._apply_state(row)
                if self._routed_startup:
                    # A routed read cannot establish the transaction state of
                    # the tab. The existing unknown-state UI displays this note.
                    self.in_transaction = None
                    self.transaction_source = None
                    self.transaction_note = (
                        "This connection sends statements to different servers or server sessions, as a proxy can. "
                        "This tab is available for reads only; changes, database switching and Stop are not "
                        "available. The transaction state cannot be confirmed.")
        if reconnected:
            self._terminate()
            return False
        self._refresh_transaction_meter(**guard_options)
        return True

    def status(self):
        with self.lock:
            idle = max(0.0, time.monotonic() - self.last_used)
            running = self.run is not None and not self.run.done.is_set()
            return {
                "schema_version": SCHEMA_VERSION, "tab_id": self.tab_id,
                "state": "ended" if self.ended else ("running" if running else self.state),
                "connection": {"profile_id": self.profile_id, "name": self.name, "host": self.host,
                               "port": self.port, "user": self.user, "type": self.connection_type,
                               "read_only": self.read_only},
                "engine": self.engine, "server_version": self.server_version,
                "server_product": self.server_product,
                "connection_id": self.connection_id, "autocommit": self.autocommit,
                "in_transaction": self.in_transaction, "transaction_source": self.transaction_source,
                "transaction_note": self.transaction_note, "sql_mode": self.sql_mode,
                "transaction_meter": self._transaction_meter_status(),
                "database_name": self.database_name,
                "backslash_escapes": self.backslash_escapes if self.sql_mode is not None else None,
                "safe_updates": self.safe_updates, "safe_updates_default": self.safe_updates_default,
                "read_only_transactions": self.read_only,
                "started_at": self.started_at, "idle_seconds": round(idle, 1),
                "idle_timeout_seconds": self.idle_seconds,
                "ended": self.ended, "ended_reason": self.ended_reason,
                "message": self.end_message if self.ended else None,
                "notes": list(self.notes[-10:]),
                "run": {"id": self.run.id, "state": self.run.state} if self.run is not None else None,
            }

    # -- running --------------------------------------------------------------------------

    def run_plan(self, sql, mode="explain", timeout=None):
        """Return JSON from this tab's client, preserving its temporary tables.

        Plans participate in Stop/KILL QUERY like editor runs. MySQL needs a
        private read-only transaction: refuse an open or unknown transaction
        rather than implicitly commit it. Executing writes that need review
        must use the normal editor run/token path, never a plan-only shortcut.
        """
        from . import dbops
        from .live import analyze_unsupported, client_error_text
        if not self._acquire_run_lock():
            raise EditorError("A statement is already running in this tab. Wait for it or press Stop.", "busy")
        previous, run, timer = None, None, None
        saved_version, rollback, problem, plan_guard = None, False, None, None
        password = self._conn.password if self._conn is not None else None

        def checked(statement, results=0):
            step = self._helper(TAG + statement, results=results, guard=plan_guard)
            if self.ended or not step.marker or step.real_errors():
                raise EditorError(self._helper_problem(step) or SESSION_ENDED_TEXT, "plan_failed")
            return step

        try:
            if self.ended:
                raise EditorError(self.end_message or SESSION_ENDED_TEXT, "session_ended")
            if mode not in ("explain", "analyze"):
                raise EditorError("Mode must be explain or analyze.")
            plan = _plan_run({"sql": sql}, self)
            if len(plan["units"]) != 1 or not plan["units"][0].get("single"):
                raise EditorError("A plan requires exactly one SQL statement.", "invalid_statement")
            inner = plan["units"][0]["send"]
            # A custom DELIMITER can hide several semicolon-separated SQL
            # statements inside one client unit. Recheck the actual inner SQL.
            if len(plan_statements(inner, self.backslash_escapes if self.sql_mode is not None else None,
                                   **_lexer_session_context(self))) != 1:
                raise EditorError("A plan requires exactly one SQL statement.", "invalid_statement")
            prefix = ("ANALYZE FORMAT=JSON " if self.engine == "mariadb" else
                      "EXPLAIN ANALYZE FORMAT=JSON ") if mode == "analyze" else "EXPLAIN FORMAT=JSON "
            statement = prefix + inner
            # EXPLAIN does not execute its inner statement. ANALYZE does, so
            # use the editor's existing classifier and policy on the inner SQL.
            gated = plan if mode == "analyze" else _plan_run({"sql": statement}, self)
            decision = _decision(gated, {"type": self.connection_type, "read_only": self.read_only})
            if decision["action"] != "allow":
                raise EditorError(decision["text"] + " Use the editor Run action to review executing writes.",
                                  "refused" if decision["action"] == "refuse" else "needs_confirmation")
            if mode == "analyze":
                unsupported = analyze_unsupported(self.server_version, self.engine == "mariadb")
                if unsupported:
                    raise EditorError(unsupported, "unsupported")
            try:
                plan_guard = self._write_session_guard()
            except ValueError as exc:
                raise EditorError(str(exc), "identity_unavailable") from None
            # The closed-transaction check must describe the same backend
            # that will receive START TRANSACTION, which can otherwise commit
            # existing work on a backend chosen by a router.
            if not self.refresh_state(guard=plan_guard):
                raise EditorError(self.end_message or "Could not read the session state.", "session_ended")
            if self.engine == "mysql" and (self.in_transaction is not False or self.autocommit is not True):
                raise EditorError("Commit or roll back the tab's transaction and enable autocommit before "
                                  "planning. The transaction must be known to be closed; nothing was sent.",
                                  "transaction_open")
            units = plan_statements(statement, self.backslash_escapes if self.sql_mode is not None else None,
                                    **_lexer_session_context(self))
            options = {key: getattr(self._conn, key) for key in (
                "host", "port", "user", "password", "database", "ssl_mode", "ssl_ca", "ssl_cert", "ssl_key",
                "ssl_cipher", "connect_timeout", "query_timeout", "ssh_host", "ssh_user", "ssh_port")}
            options["binary"] = self._checked_helper_binary()
            run = Run(units, 1, options, statement, Offsets(statement), "statement", kind="plan")
            with self.lock:
                previous, self.run = self.run, run
                self.last_used = time.monotonic()
            if self.engine == "mysql":
                version = parse_version_tuple(self.server_version or "", leading=True, default=None)
                if version is not None and version >= (8, 3, 0):
                    row = self._helper_row(checked("SELECT @@SESSION.explain_json_format_version AS plan_version", 1))
                    value = row.get("plan_version") if row else None
                    if value not in ("1", "2"):
                        raise EditorError("Could not save explain_json_format_version.", "plan_failed")
                    saved_version = value
                    checked("SET SESSION explain_json_format_version=2")
                # Set the flag before sending so an uncertain acknowledgement
                # still attempts ROLLBACK. Never change the session's defaults.
                rollback = True
                checked("START TRANSACTION READ ONLY")
            if run.stop_requested:
                raise EditorError("Plan cancelled before it started.", "stopped")
            if timeout:
                # The live credential belongs to the session, not the request.
                # KILL uses a separate client, with the session's transport.
                timer = threading.Timer(timeout, self.stop, kwargs={"options": options, "by": "timeout"})
                timer.daemon = True
                timer.start()
            units[0]["status"] = "running"
            step = self._send("user", statement, unit=0, limit=1, results=1, guard=plan_guard)
            self._wait_step(step)
            self._wait(lambda: step.closed or bool(step.real_errors()), HELPER_TIMEOUT)
            self._check_helper_guard(step)
            if self.ended or not step.marker or step.real_errors():
                raise EditorError(self._helper_problem(step) or SESSION_ENDED_TEXT, "plan_failed")
            rows = [e for e in step.entries if e["type"] == "rows"]
            if (not step.closed or len(rows) != 1 or len(rows[0]["rows"]) != 1 or
                    len(rows[0]["columns"]) != 1 or rows[0].get("truncated") or
                    rows[0]["columns"][0]["name"].upper() not in ("EXPLAIN", "ANALYZE")):
                raise EditorError("The server did not return a single JSON plan.", "plan_failed")
            raw = rows[0]["rows"][0][0]
            try:
                json.loads(raw)
            except (ValueError, TypeError):
                raise EditorError(self._helper_problem(step) or "The server did not return valid JSON.", "plan_failed")
            units[0]["status"] = "ok"
        except (EditorError, ConnectorError) as exc:
            problem = exc
        finally:
            if timer is not None:
                timer.cancel()
                timer.join()
            cleanup_problem = None
            for statement in (["ROLLBACK"] if rollback else []) + (
                    ["SET SESSION explain_json_format_version=" + saved_version] if saved_version is not None else []):
                if self.ended:
                    break
                try:
                    checked(statement)
                except (EditorError, ConnectorError) as exc:
                    cleanup_problem = exc
            if cleanup_problem is not None:
                # Do not leave a live session with an unverified setting or
                # transaction. Disconnect restores both on the server.
                self.end("plan_cleanup_failed", SESSION_ENDED_TEXT)
                problem = problem or cleanup_problem
            if run is not None:
                with self.lock:
                    run.options = None
                    run.done.set()
                    self.run = previous
                    self.last_used = time.monotonic()
            self.run_lock.release()
        if problem is not None:
            text = client_error_text(str(problem), mode, self.server_version, self.engine == "mariadb")
            raise EditorError(dbops.redact(text, password), getattr(problem, "code", "plan_failed"))
        return raw

    def busy(self):
        # A transaction meter poll is not busy: user operations wait for it
        # (_acquire_run_lock) instead of refusing.
        with self.lock:
            return self.run is not None and not self.run.done.is_set()

    def start_run(self, run):
        if not self._acquire_run_lock():
            raise EditorError("A statement is already running in this tab. Wait for it or press Stop.", "busy")
        previous_run = None
        statements = []
        try:
            with self.lock:
                previous_run = self.run
                if self.ended:
                    raise EditorError(self.end_message or SESSION_ENDED_TEXT, "session_ended")
                if self.run is not None and not self.run.done.is_set():
                    raise EditorError("A statement is already running in this tab. Wait for it or press Stop.",
                                      "busy")
            mode_unknown = self.sql_mode is None
            context = _lexer_session_context(self)
            for unit in run.units:
                mode = None if mode_unknown else self.backslash_escapes
                checked = plan_statements(unit["send"], mode, delimiter=unit.get("delimiter") or ";", **context)
                if len(checked) != 1 or any(checked[0].get(key) != unit.get(key) for key in ("code", "idents")):
                    raise EditorError("The server's SQL interpretation changed after planning. Nothing was sent. "
                                      "Run again with this session's facts.", "invalid_statement")
                unit["mode_unknown"] = mode_unknown
                unit["sql_mode"] = checked[0]["sql_mode"]
                unit["conditional_sql"] = checked[0]["conditional_sql"]
                if _unit_changes_mode(unit):
                    context["sql_mode"] = _literal_sql_mode(unit)
                    mode_unknown = context["sql_mode"] is None
            if run.audit is not None or any(_needs_session_guard(u) for u in run.units):
                statements = [u["send"] for u in run.units]
                try:
                    run.target_guard = self._write_session_guard()
                    # Refuse delimiter collisions before any user unit runs.
                    for unit in run.units:
                        if _needs_session_guard(unit):
                            setup = unit.get("packet_setup")
                            send = (setup["send"], unit["send"]) if setup is not None else unit["send"]
                            self._guarded_statement(run.target_guard, send, unit.get("delimiter") or ";",
                                                    sql_mode=unit["sql_mode"])
                except ValueError as exc:
                    if run.audit is not None:
                        run.audit.outcome(statements, "refused", outcome="identity_unavailable", message=str(exc))
                    raise EditorError(str(exc), "identity_unavailable") from None
                self._verify_identity(run.audit, statements)
            if run.audit is not None:
                if run.alter_context is not None:
                    unit = _alter_unit({"units": run.units})
                    # run_lock is held: a concurrent USE cannot slip between
                    # checking the confirmed default schema and sending ALTER.
                    if unit is not None and _ALTER_TABLE.match(unit["code"]).group(2) is None:
                        try:
                            step = self._helper(TAG + "SELECT DATABASE() AS database_name", guard=run.target_guard)
                        except EditorError as exc:
                            run.audit.outcome(statements, "refused", outcome=exc.code, message=str(exc))
                            raise
                        row = self._helper_row(step)
                        if (not step.marker or step.real_errors() or not row or
                                row.get("database_name") != run.alter_context.get("schema")):
                            message = "The ALTER database context changed or could not be verified; nothing was sent. Review it again."
                            run.audit.outcome(statements, "refused", outcome="target_changed", message=message)
                            raise EditorError(message, "target_changed")
                # The record is written before anything is sent; without it nothing is sent.
                run.audit.server(self.engine, self.server_version)
                try:
                    target = dict(self.server_identity)
                    verified = ["server_identity"]
                    if run.alter_context is not None:
                        target["alter_table"] = {k: run.alter_context.get(k) for k in ("schema", "table")}
                        index = _alter_index_name(_alter_unit({"units": run.units}))
                        if index is not None:
                            target["alter_table"]["index"] = index
                        verified.append("alter_context")
                    run.audit_id = run.audit.attempt(statements, target=target, verified=verified)
                except Exception as exc:
                    raise EditorError("The audit record could not be written, so nothing was sent: %s" % exc,
                                      "audit_failed")
            with self.lock:
                self.run = run
                self.last_used = time.monotonic()
            thread = threading.Thread(target=self._execute, args=(run,), daemon=True)
            thread.start()
        except BaseException as exc:
            try:
                if run.audit is not None and run.audit.pending:
                    run.audit.fail(exc, statements)
            finally:
                run.options = None
                run.history = {}
                if run.audit is not None:
                    run.audit.password = None
                with self.lock:
                    if self.run is run:
                        self.run = previous_run
                self.run_lock.release()
            raise
        return run

    def _wait_step(self, step):
        """Wait for a user statement's end. A statement that does not end
        within STOP_GRACE seconds after a KILL QUERY ends the session."""
        while True:
            self._wait(lambda: step.marker or step.unfinished, 1.0)
            if step.marker or step.unfinished or self.ended:
                return
            sent = step.kill_sent_at
            if sent is not None and time.monotonic() - sent > STOP_GRACE:
                self.end("unresponsive", "The statement did not end after KILL QUERY. " + SESSION_ENDED_TEXT)
                return

    def _packet_guard_failure(self, step, errors):
        """Use this packet's acknowledgement, even when stderr arrives first.

        There is no stdout end marker on an error. Without an acknowledgement,
        close stdin and drain a normal client exit before declaring it absent.
        This sends no SQL and cannot advance to another reviewed statement.
        A client that cannot drain keeps an unknown outcome, never a refusal.
        """
        from . import dbops
        if not step.guarded or not errors or not step.marker:
            return None
        code = errors[0].get("code")
        if errors[0].get("line") is None or code is None or 2000 <= code <= 2999:
            # A client diagnostic or an unlocated error cannot say whether
            # the protected write ran, even if the client read our delimiter.
            # End the session and audit it as unknown, independently of the
            # CLI's exit-2 transport contract. Do not probe or send later SQL.
            step.guard_unresolved = True
            self.end("connection_lost", SESSION_ENDED_TEXT)
            return None
        if step.guard_pending:
            self._wait(lambda: not step.guard_pending or self._eof == 2, GUARD_REPLY_GRACE)
        if step.guard_pending and self._eof < 2:
            try:
                with self.send_lock:
                    self.proc.stdin.close()
                    self.proc.wait(timeout=2)
                for reader in self._readers:
                    reader.join(2)
            except (OSError, ValueError, subprocess.SubprocessError):
                step.guard_unresolved = True
                self.end("protocol", "The server identity reply could not be read. " + SESSION_ENDED_TEXT)
                return None
            if self._eof < 2:
                step.guard_unresolved = True
                self.end("protocol", "The server identity reply could not be read. " + SESSION_ENDED_TEXT)
                return None
        if step.guard_pending and self.proc.poll() not in (0, 1):
            # A crashed/killed client can lose buffered acknowledgements.
            # EOF from that process cannot establish a negative reply.
            step.guard_unresolved = True
            self.end("client_exited", SESSION_ENDED_TEXT)
            return None
        problem = ConnectorError(errors[0]["first_line"])
        problem.guard_markers = () if step.guard_pending else step.packet_lines.markers
        return dbops.target_guard_failed(problem, step.packet_lines)

    def _execute(self, run):
        timer = None
        try:
            if run.timeout:
                timer = threading.Timer(run.timeout, self.stop, kwargs={"options": run.options, "by": "timeout"})
                timer.daemon = True
                timer.start()
            for unit in run.units:
                unit["status"] = "pending"
            history_boundary = self._history_boundary(run) if run.kind == "run" else None
            replaced = False
            for unit in run.units:
                if unit.get("generated") and unit["send"] == ALTER_LOCK_WAIT:
                    # This visible, audited setup belongs to its ALTER's
                    # packet, never an independent proxy-routing decision.
                    continue
                with self.lock:
                    if run.stop_requested or self.ended:
                        break
                    if _changes_privilege_context(unit):
                        self.privileges_changed = True
                unit["status"] = "running"
                setup = unit.get("packet_setup")
                if setup is not None:
                    setup["status"] = "running"
                kill_ok = run.kind == "run" and not _needs_session_guard(unit) and unit.get("single", True)
                step = self._send(run.kind == "run" and "user" or "action", unit["send"], unit=unit["index"],
                                  limit=run.limit, kill_ok=kill_ok,
                                  guard=run.target_guard if _needs_session_guard(unit) else None,
                                  delimiter=unit.get("delimiter") or ";", setup=setup, sql_mode=unit["sql_mode"])
                if _unit_changes_mode(unit):
                    # A CALL or multi-statement unit can change the mode
                    # before failing. Invalidate after sending, even on an
                    # error; only refresh_state may establish a new cached
                    # mode. Later packets use the revalidated prediction,
                    # and their guard checks that the server really has it.
                    with self.lock:
                        self.sql_mode = None
                self._wait_step(step)
                for thread in list(run.kill_threads):
                    thread.join(KILL_TIMEOUT + 5)
                errors = step.real_errors()
                interrupted = bool(errors) and all(e.get("code") in INTERRUPTED_CODES for e in errors)
                unit["duration_ms"] = step.duration_ms
                failure = self._packet_guard_failure(step, errors)
                if failure:
                    # --force preserves a normal editor session after SQL
                    # errors, but must never authorize a later packet here.
                    run.guard_failure = dict(failure, statement_index=unit["index"])
                    attempted = any(u.get("status") in ("ok", "error", "stopped") for u in run.units)
                    run.message = (("The server session changed. " +
                                    ("Earlier statements were sent; this statement and later statements were not sent."
                                     if attempted else "No requested statements were sent."))
                                   if failure["outcome"] == "identity_mismatch" else failure["message"])
                    unit["status"] = "not_run"
                    run.failed = unit["index"]
                    self.end(failure["outcome"], run.message + " " + SESSION_ENDED_TEXT)
                    break
                elif not step.marker or step.guard_unresolved:
                    unit["status"] = "session_ended"
                elif interrupted and step.stop_sent:
                    unit["status"] = "stopped"
                    run.interrupted = True
                elif interrupted and step.kill_requested:
                    # The row limit stopped one plain read: its rows so far are the result.
                    unit["status"] = "ok"
                elif errors:
                    unit["status"] = "error"
                    run.failed = unit["index"]
                else:
                    # Completed, also when a Stop arrived as it finished.
                    unit["status"] = "ok"
                replaced = replaced or may_replace_transaction(unit, errors)
                if self.ended or unit["status"] in ("stopped", "session_ended"):
                    break
                if unit["status"] == "error" and not (run.continue_on_error and run.kind == "run"):
                    break
            for unit in run.units:
                if unit.get("status") == "pending":
                    unit["status"] = "not_run"
            if not self.ended and run.guard_failure is None:
                if run.kind == "run":
                    self._collect_statement_history(run, history_boundary)
                # A script can replace a transaction without exposing its
                # intermediate closed state. Do not carry the previous age
                # into a new transaction when PROCESS is unavailable: the
                # observed age must stay a lower bound.
                if replaced:
                    self._transaction_seen_at = None
                # Also tells whether an error that read like a lost connection was one:
                # the helper fails with it only when the connection is really gone.
                self.refresh_state()
            # Every user block is closed by the helper echo; wait briefly otherwise.
            self._wait(lambda: all(s.closed for s in run.steps if s.kind != "helper"), 2.0)
        except ConnectorError as exc:
            run.message = str(exc)
        except Exception as exc:  # never leave a run running
            run.message = "The run failed: %s" % exc
        finally:
            if timer is not None:
                timer.cancel()
            self._finish(run)

    def _finish(self, run):
        with self.lock:
            self._finish_packet_setups(run)
            run.finished_at = _now()
            run.duration = time.monotonic() - run.started
            if run.guard_failure is not None:
                run.state = "error"
            elif self.ended:
                run.state = "session_ended"
                run.message = run.message or self.end_message or SESSION_ENDED_TEXT
            elif run.failed is not None:
                run.state = "error"
            elif run.stop_requested and any(u.get("status") in ("stopped", "not_run") for u in run.units):
                run.state = "stopped"
            elif run.message:
                run.state = "error"
            else:
                run.state = "complete"
            self.last_used = time.monotonic()
            snapshot = self._snapshot(run)
            run.snapshot_cache = snapshot
            run.options = None
        try:
            if run.audit is not None:
                counts = snapshot["counts"]
                after = {"statements_run": counts["run"], "in_transaction": self.in_transaction}
                if run.guard_failure is not None:
                    error = snapshot.get("error") or {}
                    run.audit.complete("error" if counts["run"] else "refused", outcome=run.guard_failure["outcome"],
                                       error=error.get("code"), message=run.message,
                                       rows_affected=counts["affected_rows"], after=after)
                elif any(u.get("status") == "session_ended" for u in run.units):
                    # The session ended while a statement was out: whether it ran is unknown.
                    run.audit.complete("unknown", outcome="session_ended", message=run.message,
                                       rows_affected=counts["affected_rows"], after=after)
                elif run.state == "complete" or (run.state == "stopped" and not run.interrupted):
                    run.audit.complete("ok", outcome="completed" if run.state == "complete" else "stopped_before_next",
                                       rows_affected=counts["affected_rows"], after=after)
                else:
                    error = snapshot.get("error") or next(
                        (r for r in snapshot["results"] if r["type"] == "error"), {})
                    run.audit.complete("error", outcome=run.state, error=error.get("code"),
                                       message=error.get("message") or run.message,
                                       rows_affected=counts["affected_rows"], after=after)
        except Exception:
            pass
        try:
            recorder = run.history.get("record")
            if recorder is not None:
                run.history_id = recorder(run, snapshot)
                snapshot["history_id"] = run.history_id
        except Exception:
            pass
        if run.audit is not None:
            run.audit.password = None
        run.history = {}
        run.done.set()
        self.run_lock.release()
        with self.lock:
            self.cond.notify_all()

    def _finish_packet_setups(self, run):
        """Attribute the ALTER packet's first OK to its visible setup unit.

        Wait until normal stdout draining has finished: stderr can arrive
        before the setup's OK. No extra statement is sent to infer success.
        """
        for step in list(run.steps):
            setup = getattr(step, "setup", None)
            if setup is None:
                continue
            unit = run.units[step.unit]
            setup_step = Step(step.kind, setup["send"], step.first_line, step.marker_line, unit=setup["index"])
            setup_step.sent_at, setup_step.done_at = step.sent_at, step.done_at
            setup["duration_ms"] = step.duration_ms
            if step.entries and step.entries[0]["type"] == "ok":
                setup_step.entries = [step.entries.pop(0)]
                setup["status"] = "ok"
            elif run.guard_failure is not None:
                setup["status"] = "not_run"
            elif step.closed and step.real_errors() and not step.guard_unresolved:
                # The guard succeeded but SET failed, so ALTER never ran.
                setup["status"], unit["status"] = "error", "not_run"
                setup_step.errors, step.errors = step.errors, []
                run.failed = setup["index"]
            else:
                setup["status"], unit["status"] = "session_ended", "session_ended"
                self.end("protocol", "The ALTER session setup could not be verified. " + SESSION_ENDED_TEXT)
            run.steps.insert(run.steps.index(step), setup_step)

    def _snapshot(self, run, rows=True):
        offsets = run.offsets
        statements, results = [], []
        first_error = None
        totals = {"statements": len(run.units), "run": 0, "rows": 0, "affected_rows": 0, "warnings": 0,
                  "errors": 0}
        by_unit = {}
        for step in run.steps:
            if step.kind != "helper" and step.unit is not None:
                by_unit[step.unit] = step
        for unit in run.units:
            start, end = unit["start"], unit["end"]
            entry = {"index": unit["index"], "text": unit["text"], "start": offsets.to_js(start),
                     "end": offsets.to_js(end), "verb": unit.get("verb"), "kind": unit.get("kind"),
                     "category": unit.get("category"), "status": unit.get("status", "pending"),
                     "duration_ms": unit.get("duration_ms")}
            if "server_stats" in unit:
                entry["server_stats"] = unit["server_stats"]
            if unit["lead"] or unit.get("explain_prefix"):
                entry["sent"] = unit["send"]
            statements.append(entry)
            step = by_unit.get(unit["index"])
            if step is None:
                continue
            if unit.get("status") not in ("pending", "running", "not_run"):
                totals["run"] += 1
            for item in step.entries:
                if item["type"] == "rows":
                    columns = [dict(c) for c in item["columns"]]
                    for i, column in enumerate(columns):
                        values = [r[i] for r in item["rows"] if i < len(r) and r[i] is not None]
                        # A guess: the client prints binary columns as 0x + hex, and a
                        # text column holding such strings looks the same.
                        column["hex"] = bool(values) and all(HEX_VALUE.match(v) for v in values)
                        column["type"] = None
                    stopped = item["stopped"]
                    result = {"type": "rows", "statement": unit["index"], "columns": columns,
                              "columns_known": bool(columns), "rows": item["rows"] if rows else [],
                              "row_count": len(item["rows"]), "truncated": item["truncated"], "limit": run.limit,
                              "stopped": stopped, "label": _stopped_label(stopped, len(item["rows"]), item.get("budget_reason")),
                              "server_rows": item["server_rows"], "warnings": item["warnings"],
                              "duration_ms": step.duration_ms}
                    totals["rows"] += len(item["rows"])
                    totals["warnings"] += item["warnings"] or 0
                    results.append(result)
                else:
                    result = {"type": "ok", "statement": unit["index"], "affected_rows": item["affected_rows"],
                              "info": item["info"], "warnings": item["warnings"], "duration_ms": step.duration_ms}
                    totals["affected_rows"] += max(0, item["affected_rows"] or 0)
                    totals["warnings"] += item["warnings"] or 0
                    results.append(result)
            for error in step.real_errors():
                interrupted = error.get("code") in INTERRUPTED_CODES and (run.stop_requested or step.kill_requested)
                item = {"type": "error", "statement": unit["index"], "code": error.get("code"),
                        "sqlstate": error.get("sqlstate"), "message": error.get("message"),
                        "interrupted": interrupted, "duration_ms": step.duration_ms,
                        "position": error_position(error.get("message"), unit, offsets) if run.kind == "run" and
                        not (run.guard_failure and run.guard_failure["statement_index"] == unit["index"])
                        else None}
                if error.get("code") == 9006 and re.search(
                        r"ProxySQL Error: connection is locked to hostgroup \d+ but trying to reach hostgroup \d+",
                        error.get("message") or "", re.I):
                    item["detail"] = error["message"]
                    item["message"] = ("The proxy has tied this tab to one server group after a refused change. "
                                       "Close and reopen the tab to run reads again.\n\nServer detail: " +
                                       error["message"])
                results.append(item)
                if not interrupted:
                    totals["errors"] += 1
                    if first_error is None:
                        first_error = item
        stopped = None
        if run.stop_requested:
            if run.interrupted:
                stopped = {"by": run.stopped_by or "user", "interrupted": True, "message": STOPPED_TEXT}
            elif any(u.get("status") == "not_run" for u in run.units):
                stopped = {"by": run.stopped_by or "user", "interrupted": False, "message": STOPPED_AFTER_TEXT}
        return {"schema_version": RUN_SCHEMA_VERSION, "id": run.id, "kind": run.kind, "state": run.state,
                "scope": run.scope, "started_at": run.started_at, "finished_at": run.finished_at,
                "duration_ms": _ms(run.duration), "limit": run.limit, "statements": statements,
                "results": results, "error": first_error, "stopped": stopped, "message": run.message,
                "on_error": "continue" if run.continue_on_error else "stop",
                "counts": totals, "audit_id": run.audit_id, "history_id": run.history_id}

    def snapshot(self, run=None):
        run = run or self.run
        if run is None:
            return None
        with self.lock:
            if run.snapshot_cache is not None:
                return run.snapshot_cache
            snap = self._snapshot(run, rows=False)
            snap["state"] = "running"
            return snap

    # -- stop, end ------------------------------------------------------------------------

    def stop(self, options=None, by="user", force=False):
        """Stop the running statement with KILL QUERY (from a second
        connection); *force* ends the session instead. Returns a dict."""
        with self.lock:
            run = self.run
            if run is None or run.done.is_set():
                return {"stopped": False, "message": "Nothing is running in this tab."}
            run.stop_requested = True
            run.stopped_by = run.stopped_by or by
            step = run.current
            target = step if step is not None and step.kind in ("user", "action") and not step.marker else None
        if force:
            self.end("stopped", "Stopped by ending the session. " + SESSION_ENDED_TEXT, options=options)
            return {"stopped": True, "message": self.end_message}
        if target is None:
            return {"stopped": False, "stop_requested": True,
                    "message": "No statement was running; later statements of this run will not run."}
        if options is None:
            return {"stopped": False, "stop_requested": True, "message": "Stop needs the connection to send KILL QUERY."}
        # Set before the KILL so the run sees it with the statement's error.
        target.stop_sent = True
        outcome, problem = self._kill_query(options, target)
        if outcome == "sent":
            return {"stopped": True, "stop_requested": True,
                    "message": "KILL QUERY sent. A statement it interrupts is undone; an open transaction stays "
                               "open."}
        target.stop_sent = False
        if outcome == "finished":
            return {"stopped": False, "stop_requested": True,
                    "message": "The statement already ended before KILL QUERY could interrupt it. Check the run results "
                               "for its outcome. Later statements of this run will not run."}
        if outcome == "skipped":
            return {"stopped": False, "stop_requested": True,
                    "message": "The statement had already finished, so it took effect. Later statements of this run "
                               "will not run."}
        return {"stopped": False, "stop_requested": True, "message": "KILL QUERY failed: %s" % problem}

    def _mark_ended(self, reason, message=None):
        if self.ended:
            return
        self.ended = True
        self.state = "ended"
        self.ended_reason = reason
        self.end_message = message or SESSION_ENDED_TEXT
        self.cond.notify_all()

    def _terminate(self):
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except (OSError, subprocess.SubprocessError):
                    pass
        _REGISTRY_PROCS.discard(proc)
        if self._client_home is not None:
            self._client_home.cleanup()

    def end(self, reason="closed", message=None, quiet=False, options=None):
        """End the session: the client exits and the server rolls back any
        open transaction. A statement still running is stopped with KILL
        QUERY first when the connection is known."""
        with self.lock:
            self._closing = True
            already = self.ended
            self._mark_ended(reason, message)
            run = self.run
            step = run.current if run is not None and not run.done.is_set() else None
            running = step is not None and not step.marker
            kill_options = options or (run.options if run is not None else None)
        if running and not already and kill_options is not None and self.connection_id:
            try:
                kill = dict(kill_options)
                kill["query_timeout"] = KILL_TIMEOUT
                kill["binary"] = self._checked_helper_binary()
                self._send_kill(kill, self.connection_id)
            except (ConnectorError, ValueError):
                pass
        self._terminate()
        if self._conn is not None:
            self._conn.close()
        if self._client_home is not None:
            self._client_home.cleanup()
        with self.lock:
            self.cond.notify_all()

    def idle_for(self):
        with self.lock:
            if self.ended or (self.run is not None and not self.run.done.is_set()):
                return 0.0
            return time.monotonic() - self.last_used


class _Protocol(Exception):
    pass


def _write_flags():
    from . import dbops
    return getattr(dbops, "WRITE_CLIENT_FLAGS", ("--comments", "--binary-mode"))


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stopped_label(stopped, count, budget_reason=None):
    reasons = {"cell_limit": "cell limit", "result_limit": "result-set limit",
               "byte_limit": "serialized JSON byte limit", "memory_limit": "memory limit"}
    if budget_reason in reasons:
        if stopped == "size_limit_read":
            return "first {:,} rows ({}); the statement ran to its end".format(count, reasons[budget_reason])
        return "stopped after {:,} rows ({})".format(count, reasons[budget_reason])
    if stopped == "row_limit":
        return "stopped after {:,} rows".format(count)
    if stopped == "size_limit":
        return "stopped after {:,} rows (size limit)".format(count)
    if stopped == "row_limit_read":
        return "first {:,} rows; the statement ran to its end".format(count)
    if stopped == "size_limit_read":
        return "first {:,} rows (size limit); the statement ran to its end".format(count)
    return None


# ---- the registry -----------------------------------------------------------------------

_REGISTRY_PROCS = weakref.WeakSet()


def _kill_children():
    for proc in list(_REGISTRY_PROCS):
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


atexit.register(_kill_children)


class EditorSessions(object):
    """Every open editor session of one workspace server, by tab id."""

    def __init__(self, max_sessions=MAX_SESSIONS, reap_every=15.0):
        self.sessions = {}
        self.lock = threading.Lock()
        self.tokens = {}
        self.max_sessions = max_sessions
        self.reap_every = reap_every
        self._reaper = None
        self._closed = False

    def get(self, tab_id):
        with self.lock:
            return self.sessions.get(tab_id)

    def _start_reaper(self):
        if self._reaper is None and self.reap_every:
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True)
            self._reaper.start()

    def _reap_loop(self):
        while not self._closed:
            time.sleep(self.reap_every)
            self.reap()

    def reap(self):
        """End sessions idle longer than their timeout; drop old tombstones."""
        with self.lock:
            sessions = list(self.sessions.items())
        for tab_id, session in sessions:
            if not session.ended and session.idle_for() > session.idle_seconds:
                session.end("idle", "Session ended after %d minutes without a run. Session settings were lost and "
                                    "any open transaction was rolled back." % max(1, session.idle_seconds // 60))
            elif session.ended and time.monotonic() - session.last_used > 3600:
                with self.lock:
                    if self.sessions.get(tab_id) is session:
                        del self.sessions[tab_id]

    def open(self, tab_id, make, identity=None):
        """Register the session *make()* builds and starts for *tab_id*. A
        session of another connection identity is never returned or replaced,
        including ended sessions: two racing requests cannot transfer ownership."""
        with self.lock:
            if self._closed:
                raise EditorError("The workspace is shutting down.", "closed")
            existing = self.sessions.get(tab_id)
            if existing is not None:
                if identity is not None and existing.identity != identity:
                    raise EditorError(_MISMATCH, "identity_mismatch")
                if not existing.ended:
                    return existing
            live = [s for s in self.sessions.values() if not s.ended]
            if len(live) >= self.max_sessions:
                raise EditorError("%d editor sessions are open. Close a tab's session first." % len(live),
                                  "too_many_sessions")
            session = make()
            session._owner_registry = self
            self.sessions[tab_id] = session
        self._start_reaper()
        return session

    def issue_token(self, digest):
        """A one-time confirmation token for *digest* (what was shown), valid
        TOKEN_TTL seconds."""
        token = secrets.token_urlsafe(18)
        now = time.monotonic()
        with self.lock:
            self.tokens = {k: v for k, v in self.tokens.items() if v[0] > now}
            if len(self.tokens) > 500:
                self.tokens = dict(sorted(self.tokens.items(), key=lambda kv: kv[1][0])[-250:])
            self.tokens[token] = (now + TOKEN_TTL, digest)
        return token

    def take_token(self, token, digest):
        """True once for a live token issued for *digest*; it is used up either way."""
        if not isinstance(token, str):
            return False
        with self.lock:
            entry = self.tokens.pop(token, None)
        return entry is not None and entry[0] > time.monotonic() and entry[1] == digest

    def remove(self, tab_id, session=None):
        with self.lock:
            current = self.sessions.get(tab_id)
            if current is not None and (session is None or current is session):
                del self.sessions[tab_id]

    def close(self):
        """End every session (workspace exit)."""
        with self.lock:
            self._closed = True
            sessions = list(self.sessions.values())
            self.sessions = {}
        for session in sessions:
            try:
                session.end("server_exit")
            except Exception:
                pass
        # Let runs finish their audit and history records before the store goes away.
        for session in sessions:
            run = session.run
            if run is not None:
                run.done.wait(5)


# ---- export -----------------------------------------------------------------------------

EXPORT_FORMATS = ("csv", "tsv", "json", "sql", "markdown")
_MIME = {"csv": "text/csv", "tsv": "text/tab-separated-values", "json": "application/json",
         "sql": "application/sql", "markdown": "text/markdown"}
_EXT = {"csv": "csv", "tsv": "tsv", "json": "json", "sql": "sql", "markdown": "md"}
NUMERIC_TYPES = ("tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric", "float",
                 "double", "real", "year")
# A value written unquoted must read back as the same text even if the column
# turns out not to be numeric (a type inferred from another table, a shadowing
# temporary table): no leading zeros, no exponent, no negative zero.
_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$")
_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def _check_result(result):
    """(columns, rows) from an export request; values are null, text, or numbers."""
    if not isinstance(result, dict):
        raise ValueError("Pass the result to export as {columns, rows}.")
    columns = result.get("columns")
    rows = result.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("Pass the result to export as {columns, rows}.")
    out_columns = []
    for column in columns:
        if isinstance(column, str):
            column = {"name": column}
        if not isinstance(column, dict) or not isinstance(column.get("name"), str):
            raise ValueError("Every column needs a name.")
        out_columns.append({"name": column["name"], "hex": column.get("hex") is True,
                            "binary": column.get("binary") is True,
                            "type": column.get("type") if isinstance(column.get("type"), str) else None})
    out_rows = []
    width = len(out_columns)
    for row in rows:
        if not isinstance(row, list) or len(row) != width:
            raise ValueError("Every row needs one value per column.")
        values = []
        for value in row:
            if value is None or isinstance(value, str):
                values.append(value)
            elif isinstance(value, bool):
                values.append("1" if value else "0")
            elif isinstance(value, (int, float)):
                values.append(repr(value) if isinstance(value, float) else str(value))
            else:
                raise ValueError("Values must be null, text or numbers.")
        out_rows.append(values)
    return out_columns, out_rows


def _csv_field(value, delimiter, guard):
    if value is None:
        return ""
    text = value
    if guard and text[:1] in _FORMULA:
        text = "'" + text
    if text == "" or any(c in text for c in (delimiter, '"', "\r", "\n")):
        return '"' + text.replace('"', '""') + '"'
    return text


def to_csv(columns, rows, header=True, formula_guard=False, bom=False, delimiter=","):
    """RFC 4180: CRLF between records, a field with the delimiter, a double
    quote, CR or LF in double quotes with quotes doubled. NULL is an empty
    unquoted field, an empty string is ``""``. *formula_guard* prefixes cells
    starting with = + - @ (or tab, CR) with ' against spreadsheet formulas."""
    lines = []
    if header:
        lines.append(delimiter.join(_csv_field(c["name"], delimiter, formula_guard) for c in columns))
    for row in rows:
        lines.append(delimiter.join(_csv_field(v, delimiter, formula_guard) for v in row))
    text = "".join(line + "\r\n" for line in lines)
    return ("﻿" + text) if bom else text


def to_tsv(columns, rows, header=True):
    """Tab separated for the clipboard: the CSV rules with a tab."""
    return to_csv(columns, rows, header=header, delimiter="\t")


def to_json(columns, rows, objects=False):
    """``{"columns": [...], "rows": [[...]]}`` with null for NULL and text
    otherwise (big integers, DECIMAL and binary ``0x...`` stay text). With
    *objects*, rows are objects; repeated column names get " (2)", " (3)"."""
    cols = [{"name": c["name"], "hex": c["hex"], "binary": c["binary"], "type": c["type"]} for c in columns]
    if not objects:
        body = {"columns": cols, "rows": rows}
    else:
        keys, seen = [], {}
        for c in columns:
            name = c["name"]
            seen[name] = seen.get(name, 0) + 1
            keys.append(name if seen[name] == 1 else "%s (%d)" % (name, seen[name]))
        body = {"columns": cols, "keys": keys, "rows": [dict(zip(keys, row)) for row in rows]}
    return json.dumps(body, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def quote_identifier(name):
    if not isinstance(name, str) or not name or "\x00" in name or len(name) > 256:
        raise ValueError("Name the table (1 to 256 characters, no NUL).")
    return "`" + name.replace("`", "``") + "`"


def sql_literal(value, hex_column=False, numeric=False):
    """A literal that means the same with or without NO_BACKSLASH_ESCAPES."""
    if value is None:
        return "NULL"
    if hex_column and HEX_VALUE.match(value):
        return "X'%s'" % value[2:]
    if numeric and _NUMBER.match(value) and not (value[0] == "-" and not value.strip("-0.")):
        return value
    if all(32 <= ord(c) != 127 for c in value) and "\\" not in value:
        return "'" + value.replace("'", "''") + "'"
    return "_utf8mb4 X'%s'" % value.encode("utf-8", "surrogatepass").hex().upper()


def to_sql(columns, rows, table, schema=None, engine="mysql", rows_per_insert=100):
    """INSERT statements: identifiers in backticks (doubled inside), NULL as
    NULL, numbers unquoted only for columns typed numeric, columns the caller
    marks ``binary: true`` (shown as 0x...) as X'...' (``hex`` alone is only
    a guess, so such values stay quoted text), other text as '...' with '
    doubled, or as
    _utf8mb4 X'...' when it holds a backslash or a control character. The
    output is the same for MySQL and MariaDB."""
    if engine not in ("mysql", "mariadb"):
        raise ValueError("Choose engine 'mysql' or 'mariadb'.")
    if not isinstance(rows_per_insert, int) or isinstance(rows_per_insert, bool) or not 1 <= rows_per_insert <= 10000:
        raise ValueError("rows_per_insert must be an integer from 1 to 10000.")
    target = quote_identifier(table)
    if schema:
        target = quote_identifier(schema) + "." + target
    names = ", ".join(quote_identifier(c["name"]) for c in columns)
    numeric = [(c.get("type") or "").lower().split("(")[0].split(" ")[0] in NUMERIC_TYPES for c in columns]
    out = []
    for start in range(0, len(rows), rows_per_insert):
        chunk = rows[start:start + rows_per_insert]
        values = ",\n".join("  (%s)" % ", ".join(sql_literal(v, columns[i]["binary"], numeric[i])
                                                 for i, v in enumerate(row)) for row in chunk)
        out.append("INSERT INTO %s (%s) VALUES\n%s;\n" % (target, names, values))
    return "".join(out)


_MD_ESCAPE = re.compile(r"([\\`*_\[\]|])")


def _md_cell(value):
    if value is None:
        return "*NULL*"
    text = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _MD_ESCAPE.sub(r"\\\1", text)
    return text.replace("\r\n", "<br>").replace("\r", "<br>").replace("\n", "<br>")


def to_markdown(columns, rows):
    """A pipe table: | escaped as \\|, newlines as <br>, NULL as *NULL*, an
    empty string as an empty cell, Markdown and HTML characters escaped so
    every value reads as text."""
    if not columns:
        return ""
    lines = ["| " + " | ".join(_md_cell(c["name"]) for c in columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_md_cell(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def export_result(fmt, result, table=None, schema=None, engine="mysql", options=None):
    """``{"format", "content", "mime", "filename"}``. Exports come from the
    typed values only and never include connection settings or passwords."""
    if fmt not in EXPORT_FORMATS:
        raise ValueError("Choose export format csv, tsv, json, sql or markdown.")
    options = options if isinstance(options, dict) else {}
    columns, rows = _check_result(result)
    if fmt == "csv":
        content = to_csv(columns, rows, header=options.get("header", True) is not False,
                         formula_guard=options.get("formula_guard") is True, bom=options.get("bom") is True)
    elif fmt == "tsv":
        content = to_tsv(columns, rows, header=options.get("header", True) is not False)
    elif fmt == "json":
        content = to_json(columns, rows, objects=options.get("objects") is True)
    elif fmt == "sql":
        content = to_sql(columns, rows, table, schema or None, engine or "mysql",
                         options.get("rows_per_insert", 100))
    else:
        content = to_markdown(columns, rows)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", table)[:60] if (fmt == "sql" and table) else "myflames-result"
    return {"format": fmt, "content": content, "mime": _MIME[fmt], "filename": "%s-%s.%s" % (base, stamp, _EXT[fmt])}


# ---- history ----------------------------------------------------------------------------

HISTORY_KIND = "editor_history"
HISTORY_MAX_ENTRIES = 1000
HISTORY_MAX_BYTES = 4 * 1024 * 1024
HISTORY_SQL_CAP = 20000


def history_entry(run, snapshot, session, password=None):
    """The history record of one run: redacted SQL, never credentials."""
    from . import dbops
    sql = dbops.redact(run.sql_text, password) or ""
    error = snapshot.get("error") or {}
    counts = snapshot["counts"]
    return {"at": run.started_at, "tab_id": session.tab_id,
            "connection": {"profile_id": session.profile_id, "name": session.name,
                           "host": session.host, "port": session.port},
            "engine": session.engine, "kind": run.kind, "scope": run.scope,
            "sql": sql[:HISTORY_SQL_CAP], "sql_truncated": len(sql) > HISTORY_SQL_CAP,
            "statements": counts["statements"], "statements_run": counts["run"],
            "duration_ms": snapshot["duration_ms"], "rows": counts["rows"],
            "affected_rows": counts["affected_rows"], "status": snapshot["state"],
            "error_code": error.get("code")}


# ---- saved queries ----------------------------------------------------------------------

#: Bounds like the history's: count and stored size are capped, but a full
#: store refuses a new query instead of dropping one the user chose to keep.
SAVED_QUERY_MAX_ENTRIES = 500
SAVED_QUERY_MAX_BYTES = 4 * 1024 * 1024
SAVED_QUERY_SQL_CAP = HISTORY_SQL_CAP
SAVED_QUERY_NAME_CAP = 200
SAVED_QUERY_FOLDER_CAP = 200
SAVED_QUERY_NOTE_CAP = 2000
SAVED_QUERY_DATABASE_CAP = 255


def _saved_text(value, label, cap, required=False, one_line=True):
    """*value* trimmed (and on one line), or ValueError with *label*."""
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError("%s must be text." % label)
    if one_line:
        value = " ".join(value.split())
    value = value.strip()
    if required and not value:
        raise ValueError("%s is required." % label)
    if len(value) > cap:
        raise ValueError("%s must be at most %d characters." % (label, cap))
    return value


def saved_query_name(value):
    """A saved query's name: one line, redacted like its SQL."""
    from . import dbops
    return dbops.redact(_saved_text(value, "A name", SAVED_QUERY_NAME_CAP, required=True)) or ""


def saved_query_folder(value):
    """A saved query's folder: one line; an empty string is no folder."""
    return _saved_text(value, "The folder name", SAVED_QUERY_FOLDER_CAP)


def saved_query_entry(name, folder, sql, note, database):
    """One saved-query-1.0 record. The SQL (and the name and note) go
    through dbops.redact, the function that redacts the run history, before
    anything is stored; credentials are never kept."""
    from . import dbops
    name = saved_query_name(name)
    folder = saved_query_folder(folder)
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("There is no SQL to save.")
    sql = dbops.redact(sql) or ""
    if len(sql) > SAVED_QUERY_SQL_CAP:
        raise ValueError("A saved query keeps at most {:,} characters of SQL; this one has {:,}."
                         .format(SAVED_QUERY_SQL_CAP, len(sql)))
    note = dbops.redact(_saved_text(note, "The note", SAVED_QUERY_NOTE_CAP, one_line=False)) or ""
    database = _saved_text(database, "The database", SAVED_QUERY_DATABASE_CAP) or None
    stamp = _now()
    return {"schema_version": "saved-query-1.0", "name": name, "folder": folder,
            "sql": sql, "note": note, "database": database,
            "created": stamp, "updated": stamp}


# ---- the HTTP API -----------------------------------------------------------------------

#: Every editor endpoint (ui.py adds them to its allowlist).
PATHS = ("/api/editor/open", "/api/editor/run", "/api/editor/status", "/api/editor/stop",
         "/api/editor/commit", "/api/editor/rollback", "/api/editor/close", "/api/editor/history",
         "/api/editor/export", "/api/editor/preview", "/api/editor/use",
         "/api/editor/file/open", "/api/editor/file/save",
         "/api/editor/saved/list", "/api/editor/saved/save", "/api/editor/saved/rename",
         "/api/editor/saved/move", "/api/editor/saved/delete")
#: Writes: the policy runs first, the saved password is filled only after it.
WRITE_PATHS = ("/api/editor/run", "/api/editor/commit")
#: Open a database connection with the request's connection (ui fills the saved password).
CONNECTION_PATHS = ("/api/editor/open", "/api/editor/stop")


def _tab_id(payload):
    tab_id = payload.get("tab_id")
    if not isinstance(tab_id, str) or not _TAB_ID.match(tab_id):
        raise ValueError("Pass tab_id: 1 to 100 letters, digits, '_', '.', ':' or '-'.")
    return tab_id


def _number(payload, key, default, lower, upper, label):
    value = payload.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError("%s must be a number from %s to %s." % (label, lower, upper))
    if not lower <= value <= upper:
        raise ValueError("%s must be a number from %s to %s." % (label, lower, upper))
    return value


def _registry(workspace):
    registry = getattr(workspace, "editor", None)
    if registry is None:
        registry = EditorSessions()
        try:
            workspace.editor = registry
        except AttributeError:
            raise ValueError("This operation requires a running workspace.")
    return registry


def _connection_facts(payload, store, allow_group_revoked=False):
    """(options without password, identity key, safety, display name) for the request's connection.

    With *allow_group_revoked* (Status, Rollback, Stop and Close: cleanup on
    the tab's existing session, never new statements), lost group membership
    does not raise here; session ownership (:func:`_owned_session`) still
    decides, and the safety falls back to the profile's own (no group
    effective type), so it is never looser than the group's strictest."""
    from . import dbops
    from .live import connection_options
    connection = payload.get("connection")
    if not isinstance(connection, dict):
        raise ValueError("Enter connection settings.")
    plain = {k: v for k, v in connection.items() if k != "password"}
    options = connection_options(plain)
    profile = None
    profile_id = connection.get("profile_id")
    if profile_id:
        if not isinstance(profile_id, str) or len(profile_id) > 200:
            raise ValueError("Invalid connection field: profile_id")
        profile = store.get("profile", profile_id)
    revoked = False
    if connection.get("group_name") is not None:
        from .connection_groups import validate_group
        group = validate_group(connection["group_name"])
        if not group or not isinstance(profile, dict) or profile.get("group") != group:
            if not allow_group_revoked:
                raise ValueError("This saved connection is outside the current connection group.")
            revoked = True
    safety = (dbops.connection_safety(profile, connection) if revoked
              else dbops.connection_safety(profile, connection, store=store))
    keyed = dict(options)
    keyed["profile_id"] = profile_id or None
    identity = dbops.connection_identity(keyed, safety)
    name = safety.get("profile_name") or "%s:%s" % (options.get("host"), options.get("port"))
    return options, identity, safety, name


_MISMATCH = "This tab's session belongs to another connection. Close it from that connection before switching."


def _group_options(options, payload, store, identity, safety, audit=None, statements=(), operation='editor_run'):
    """Bind a member's session and any later helper logins to current consent."""
    connection = payload.get('connection') or {}
    if connection.get('group_name') is None:
        return options
    from . import dbops
    from .ui import _group_guarded_options
    if audit is None:
        audit = dbops.AuditTrail(store, operation, 'ui', connection=connection, safety=safety,
                                 password=options.get('password'))
    return _group_guarded_options(options, connection, store, identity, audit, statements)


def _session_for(registry, tab_id, identity):
    session = registry.get(tab_id)
    if session is not None and session.identity != identity:
        raise EditorError(_MISMATCH, "identity_mismatch")
    return session


def _owned_session(registry, tab_id, identity):
    """The tab's session when it belongs to the same saved connection endpoint,
    whatever the policy (type, read-only) is now. Status, Close, Stop and
    Rollback use this so a policy change never strands a running query or an
    open transaction; writes and runs keep the strict :func:`_session_for`."""
    from . import dbops
    session = registry.get(tab_id)
    if session is not None and dbops.identity_endpoint(session.identity) != dbops.identity_endpoint(identity):
        raise EditorError(_MISMATCH, "identity_mismatch")
    return session


def _digest(tab_id, identity, statements, operation):
    """What a confirmation covers: the tab, the connection, the operation and
    the exact statements."""
    text = json.dumps([tab_id, identity, operation, statements], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tail_end(sql, end, delimiter):
    """Where the caret stops choosing the statement that ends at *end*: after
    its delimiter (or \\g), spaces and comments up to the line break belong to
    it; with more SQL on that line, right after the delimiter. The twin of
    sqlSelection.ts tailEnd."""
    n = len(sql)
    i = end
    # Only white space can stand between a statement's text and its delimiter.
    while i < n and sql[i] in " \t\n\r\f\v":
        i += 1
    if sql.startswith(delimiter, i):
        i += len(delimiter)
    elif sql[i:i + 2] in ("\\g", "\\G"):
        i += 2
    else:
        i = end
    after = i
    while i < n:
        ch = sql[i]
        if ch in "\n\r":
            return i
        if ch in " \t":
            i += 1
            continue
        if ch == "#" or (sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\n\r\f\v")):
            while i < n and sql[i] not in "\n\r":
                i += 1
            return i
        if sql.startswith("/*", i):
            close = sql.find("*/", i + 2)
            if close < 0:
                return n
            i = close + 2
            continue
        return after
    return i


def statement_at(sql, statements, cursor):
    """The statement Run uses for a caret at *cursor* (a Python offset): the
    one the caret is in or on the line of its delimiter; between statements,
    the next one; after the last, the last; None when there is none. The twin
    of sqlSelection.ts statementAt (shared cases in
    apps/workspace/test/fixtures/sql-split.json)."""
    for stmt in statements:
        if cursor <= stmt["end"] or cursor <= _tail_end(sql, stmt["end"], stmt.get("delimiter") or ";"):
            return stmt
    return statements[-1] if statements else None


def delimiter_at(sql, offset, backslash_escapes=True, **lexer_args):
    """The delimiter in effect at *offset* (a DELIMITER line above changes it)."""
    probe = dbops_split(sql[:offset] + "\nx", backslash_escapes, **lexer_args)
    return probe[-1]["delimiter"] if probe else ";"


def dbops_split(sql, backslash_escapes=True, delimiter=";", **lexer_args):
    from . import dbops
    return dbops.split_statements(sql, backslash_escapes, delimiter, **lexer_args)


def _offset(payload, key, offsets, total):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= total:
        raise ValueError("%s must be an integer offset inside the text." % key)
    return offsets.to_py(value)


def _selection(payload, sql, offsets, backslash=True, context=None):
    """(start, end, scope, delimiter) of what runs: the whole text; the
    selection, split with the delimiter in effect where it starts; or, with
    ``cursor``, the one statement the caret chooses, found here with the same
    rules (and the session's backslash reading) the server will split with."""
    from . import dbops
    lexer_args = {key: value for key, value in (context or {}).items() if key != "identity_bound"}
    escaping = True if backslash is None else backslash
    selection = payload.get("selection")
    total = offsets.to_js(len(sql))
    if selection is None and payload.get("cursor") is not None:
        cursor = _offset(payload, "cursor", offsets, total)
        stmt = statement_at(sql, dbops_split(sql, escaping, **lexer_args), cursor)
        for reading in dbops.lexer_readings(backslash, lexer_args.get("engine"), lexer_args.get("sql_mode"),
                                           lexer_args.get("server_version"), lexer_args.get("client_engine"), sql):
            other = statement_at(sql, dbops_split(sql, **dict(lexer_args, **reading)), cursor)
            if dbops._mode_reading([stmt] if stmt else []) != dbops._mode_reading([other] if other else []):
                raise EditorError("The session quote mode is unknown and the statement at the cursor reads "
                                  "differently under NO_BACKSLASH_ESCAPES, ANSI_QUOTES or MSSQL.", "invalid_statement")
        if "$" in sql:
            from . import dbops
            dollar_stmt = statement_at(sql, dbops.split_statements(sql, escaping, dollar_quotes=True, **lexer_args), cursor)
            if dbops._target_reading([stmt] if stmt else []) != dbops._target_reading(
                    [dollar_stmt] if dollar_stmt else []):
                raise EditorError("Dollar-quoted text ($...$) changes which statement is at the cursor. "
                                  "The Query editor does not run dollar-quoted text yet.", "invalid_statement")
        if stmt is None:
            raise EditorError("There is no statement at the cursor. Write SQL to run.", "empty")
        return stmt["start"], stmt["end"], "statement", stmt.get("delimiter") or ";"
    if selection is None:
        return 0, len(sql), "all", ";"
    if not isinstance(selection, dict):
        raise ValueError("selection must be {start, end}.")
    start = _offset(selection, "start", offsets, total)
    end = _offset(selection, "end", offsets, total)
    if end <= start:
        raise ValueError("The selection is empty.")
    delimiter = delimiter_at(sql, start, escaping, **lexer_args)
    for reading in dbops.lexer_readings(backslash, lexer_args.get("engine"), lexer_args.get("sql_mode"),
                                       lexer_args.get("server_version"), lexer_args.get("client_engine"), sql):
        if delimiter != delimiter_at(sql, start, **dict(lexer_args, **reading)):
            raise EditorError("The session quote mode is unknown and the selection has ambiguous delimiters.",
                              "invalid_statement")
    return start, end, "selection", delimiter


# db-operations sections 6 and 9: exact destructive target, one DDL per
# confirmation, and a reviewed, bounded metadata-lock wait in this session.
# The skill specifies N, not a numeric default; five seconds is our UI policy.
ALTER_LOCK_WAIT_SECONDS = 5
ALTER_LOCK_WAIT = TAG + "SET SESSION lock_wait_timeout = %d" % ALTER_LOCK_WAIT_SECONDS
_SQL_NAME = r"(?:`[0-9]+`|[" + _SQL_ID_CHARS + r"]+)"
_DROP_DATABASE = re.compile(r"^\s*DROP\s+(?:DATABASE|SCHEMA)\s+(?:IF\s+EXISTS\s+)?(" + _SQL_NAME + r")\s*$", re.I)
_ALTER_TABLE = re.compile(r"^\s*ALTER\s+(?:(?:ONLINE|OFFLINE|IGNORE)\s+)*TABLE\s+(?:IF\s+EXISTS\s+)?(" +
                          _SQL_NAME + r")(?:\s*\.\s*(" + _SQL_NAME + r"))?(?=\s|$)", re.I)
_DDL_START = re.compile(r"^\s*(?:CREATE|ALTER|DROP|RENAME|TRUNCATE)" + _SQL_BOUNDARY, re.I | re.ASCII)
_ROUTINE_CREATE = re.compile(
    # MariaDB AGGREGATE FUNCTION: mariadb-server/sql/sql_yacc.yy:20502-20529.
    r"\s*" + _SQL_BOUNDARY + r"(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*[^;]+?\s+)?|"
    r"ALTER\s+(?:DEFINER\s*=\s*[^;]+?\s+)?(?=EVENT" + _SQL_BOUNDARY + r"))"
    r"(?:(AGGREGATE)\s+)?(PROCEDURE|FUNCTION|TRIGGER|EVENT|PACKAGE)" + _SQL_BOUNDARY, re.I | re.S | re.ASCII)


# The mask supplies quoted identifiers as `N` and strings as ''. Keep every
# identifier, qualified name and @/@@ variable atomic. In particular, neither
# col$end nor @loop can supply a stored-program control token.
_MASKED_NAME = r"(?:`[0-9]+`|[" + _SQL_ID_CHARS + r"]+)"
_MASKED_QUALIFIED = _MASKED_NAME + r"(?:[ \t\r\n\f\v]*\.[ \t\r\n\f\v]*" + _MASKED_NAME + r")*"
_MASKED_VARIABLE_NAME = r"(?:" + _MASKED_NAME + r"|'')"
_MASKED_VARIABLE = (r"@(?:[ \t\r\n\f\v]*@)?[ \t\r\n\f\v]*" + _MASKED_VARIABLE_NAME +
                    r"(?:[ \t\r\n\f\v]*\.[ \t\r\n\f\v]*" + _MASKED_VARIABLE_NAME + r")*")
_MASKED_TOKEN = re.compile(_MASKED_VARIABLE + r"|" +
                           _MASKED_QUALIFIED + r"|''|[^ \t\r\n\f\v]")


class _DdlBoundaryScanner:
    """Recognize structural boundaries, not the server's full SQL grammar.

    Only statement positions open control blocks. Expressions have a separate
    CASE/parenthesis stack, so their THEN/END never open or close a statement.
    Unsupported headers, mismatched blocks and incomplete expressions refuse
    instead of guessing where a confirmation ends.

    Grammar: mysql-server/sql/sql_yacc.yy:4883-4908,5268-5336,5570-5833;
    mariadb-server/sql/sql_yacc.yy:19254-19265,20134-20199,20293-20378.
    """

    def __init__(self, code, offset, idents=()):
        self.code = code
        self.idents = idents
        self.tokens = list(_MASKED_TOKEN.finditer(code, offset))
        self.pos = 0
        self.level = 0
        self.embedded_ddl = False

    def word(self, ahead=0):
        index = self.pos + ahead
        if index >= len(self.tokens):
            return ""
        token = self.tokens[index].group()
        # Unicode case folding must not turn an identifier into an ASCII SQL
        # keyword (for example the dotless i in an identifier).
        return token.upper() if token.isascii() else token

    def refuse(self):
        raise EditorError("One DDL per confirmation: the stored-program boundary could not be verified. "
                          "Use a supported, complete definition on its own; nothing was sent.", "invalid_statement")

    def take(self, word):
        if self.word() == word:
            self.pos += 1
            return True
        return False

    def need(self, word):
        if not self.take(word):
            self.refuse()

    def name(self):
        if not re.fullmatch(_MASKED_QUALIFIED, self.word()):
            self.refuse()
        self.pos += 1

    def name_or_text(self):
        # ident_or_text: charset/collation and trigger ordering names may
        # be string literals. The shared lexer has already masked them.
        if not self.take("''"):
            self.name()

    @staticmethod
    def operand_follows(word):
        # END and DO are non-reserved identifiers in expression positions.
        # Track the positions needed to distinguish those names from a CASE
        # closer or WHILE's DO, without interpreting an expression's value.
        return word in ("(", ",", "+", "-", "*", "/", "%", "=", "<", ">", "!", "~", "^", "|", "&",
                        "CASE", "WHEN", "THEN", "ELSE", "AND", "OR", "XOR", "NOT", "IS", "IN", "LIKE",
                        "REGEXP", "RLIKE", "BETWEEN", "DIV", "MOD", "COLLATE", "AS", "SELECT", "RETURN",
                        "DEFAULT", "TO")

    def expression(self, stops, allow_empty=False):
        """Consume an expression up to a structural keyword at depth zero."""
        stack, start, operand = [], self.pos, True
        while self.word():
            word = self.word()
            if not stack and word in stops and (word not in ("END", "DO") or not operand or
                                                 (allow_empty and self.pos == start)):
                break
            if word == ";":
                self.refuse()
            if word == "(":
                stack.append(")")
            elif word == "CASE":
                stack.append("END")
            elif word == ")":
                if not stack or stack.pop() != ")":
                    self.refuse()
            elif word == "END" and stack and stack[-1] == "END" and not operand:
                stack.pop()
            operand = self.operand_follows(word)
            self.pos += 1
        if stack or (self.pos == start and not allow_empty):
            self.refuse()

    def parameters(self):
        if self.take("("):
            self.expression({")"}, allow_empty=True)
            self.need(")")

    def characteristics(self):
        # Complete, fixed header phrases only. AS/IS elsewhere is ordinary
        # SQL, and an unknown header never starts a declaration section.
        phrases = {"LANGUAGE": ("SQL",), "NOT": ("DETERMINISTIC",),
                   "CONTAINS": ("SQL",), "NO": ("SQL",),
                   "READS": ("SQL", "DATA"), "MODIFIES": ("SQL", "DATA")}
        while self.word():
            word = self.word()
            if word == "COMMENT":
                self.pos += 1
                self.need("''")
            elif word == "DETERMINISTIC":
                self.pos += 1
            elif word == "SQL":
                self.pos += 1
                self.need("SECURITY")
                if self.word() not in ("DEFINER", "INVOKER"):
                    self.refuse()
                self.pos += 1
            elif word in phrases:
                self.pos += 1
                for expected in phrases[word]:
                    self.need(expected)
            else:
                return

    def returns(self):
        if not (self.take("RETURNS") or self.take("RETURN")):
            self.refuse()
        # Consume a type, its size/enum list, and known attributes. Arbitrary
        # scanning to a later AS/IS would mistake a body alias for a header.
        # MySQL sql/sql_yacc.yy:7371-7409; MariaDB:6835-6879,19287-19322.
        base = self.word()
        self.name()
        if base == "NATIONAL":
            if self.word() not in ("CHAR", "CHARACTER", "VARCHAR"):
                self.refuse()
            base = self.word()
            self.pos += 1
        elif (base == "LONG" and self.word() in ("CHAR", "CHARACTER", "VARCHAR", "VARBINARY") and
              not (self.word() in ("CHAR", "CHARACTER") and self.word(1) == "SET")):
            # LONG itself is a complete type. CHARACTER SET then belongs
            # to its attributes (mysql-8.4.0 sql/sql_yacc.yy:7081-7085).
            base = self.word()
            self.pos += 1
        if base == "DOUBLE":
            self.take("PRECISION")
        elif base in ("CHAR", "CHARACTER", "NCHAR"):
            if not (base == "NCHAR" and self.take("VARCHAR")):
                self.take("VARYING")
        elif base == "TYPE" or (base == "ROW" and self.take("TYPE")):
            self.need("OF")
            self.name()
            return
        self.parameters()
        while self.word():
            if self.word() in ("UNSIGNED", "SIGNED", "ZEROFILL", "BINARY", "ASCII", "UNICODE", "BYTE"):
                self.pos += 1
            elif self.take("CHARACTER") or self.take("CHAR"):
                self.need("SET")
                self.name_or_text()
            elif self.take("CHARSET") or self.take("COLLATE"):
                self.name_or_text()
            elif self.take("COMPRESSED"):
                if self.take("="):
                    self.name()
            elif self.take("REF_SYSTEM_ID"):
                self.need("=")
                if not re.fullmatch(r"[0-9]+", self.word()):
                    self.refuse()
                self.name()
            else:
                return

    def routine(self, kind, member=False):
        if self.take("IF"):
            self.need("NOT")
            self.need("EXISTS")
        self.name()
        if kind == "FUNCTION" and not member and self.take("RETURNS"):
            # UDF declarations load a library and have no parameters/body.
            # MySQL sql_yacc.yy:18456-18497; MariaDB:20530-20536.
            if self.word() not in ("STRING", "INTEGER", "INT", "REAL", "DECIMAL"):
                self.refuse()
            self.pos += 1
            self.need("SONAME")
            self.need("''")
            return
        if not member and self.word() != "(":
            self.refuse()
        self.parameters()
        if kind == "FUNCTION":
            self.returns()
        self.characteristics()
        if member and self.word() == ";":
            return  # Package forward declaration.
        if self.take("AS") or self.take("IS"):
            # Oracle declarations precede exactly one BEGIN, and do not
            # include arbitrary executable statements or nested definitions.
            while self.word() != "BEGIN":
                if self.word() in ("", "END", "PROCEDURE", "FUNCTION"):
                    self.refuse()
                self.simple()
                self.need(";")
            self.statement()
            if self.word() not in ("", ";"):
                self.name()  # Oracle optional routine end name.
        else:
            self.statement()

    def sequence(self, stops):
        while self.word() not in stops:
            if not self.word():
                self.refuse()
            self.statement()
            self.need(";")

    def simple(self):
        # An ordinary SQL statement ends at its first semicolon, regardless
        # of IF NOT EXISTS, AS aliases, XA END, functions or variable names.
        # Expression CASEs and parentheses must balance before that boundary.
        stack, start, operand = [], self.pos, True
        while self.word() not in ("", ";"):
            word = self.word()
            if word == "(":
                stack.append(")")
            elif word == "CASE":
                stack.append("END")
            elif word == ")":
                if not stack or stack.pop() != ")":
                    self.refuse()
            elif word == "END" and stack and stack[-1] == "END" and not operand:
                stack.pop()
            operand = self.operand_follows(word)
            self.pos += 1
        if stack or self.pos == start:
            self.refuse()

    def statement(self):
        self.level += 1
        if self.level > 100:
            self.refuse()
        label = None
        if self.word(1) == ":":
            label = self.word()
            self.name()
            self.need(":")
        word = self.word()
        if self.level > 1 and word in ("CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE"):
            # DROP PREPARE deallocates a prepared statement, not an object.
            self.embedded_ddl = self.embedded_ddl or not (word == "DROP" and self.word(1) == "PREPARE")
        if word in ("BEGIN", "LOOP", "REPEAT", "WHILE", "FOR", "IF", "CASE"):
            self.pos += 1
            end_word = word
            if word == "BEGIN":
                if self.take("NOT"):
                    self.need("ATOMIC")
                self.sequence({"END", "EXCEPTION"})
                if self.take("EXCEPTION"):
                    # MariaDB sql/sql_yacc.yy:20079-20102. Recognizing the
                    # boundary does not authorize an ORACLE-mode session.
                    self.need("WHEN")
                    while True:
                        self.expression({"THEN"})
                        self.need("THEN")
                        self.sequence({"WHEN", "END"})
                        if not self.take("WHEN"):
                            break
            elif word in ("LOOP", "REPEAT"):
                self.sequence({"UNTIL" if word == "REPEAT" else "END"})
                if word == "REPEAT":
                    self.need("UNTIL")
                    self.expression({"END"})
            elif word == "WHILE":
                self.expression({"DO"})
                self.need("DO")
                self.sequence({"END"})
            elif word == "FOR":
                # MariaDB sql/sql_yacc.yy:4769-4788,19426-19429,19903-19906:
                # ranges, named/parameterized cursors and (SELECT ...) all
                # have the same header boundary, including REVERSE.
                self.name()
                self.need("IN")
                self.take("REVERSE")
                self.expression({"DO", "LOOP"})
                if not self.take("DO"):
                    self.need("LOOP")
                    end_word = "LOOP"
                self.sequence({"END"})
            elif word == "IF":
                while True:
                    self.expression({"THEN"})
                    self.need("THEN")
                    self.sequence({"ELSEIF", "ELSE", "END"})
                    if not self.take("ELSEIF"):
                        break
                if self.take("ELSE"):
                    self.sequence({"END"})
            else:  # statement CASE, distinct from expression CASE
                self.expression({"WHEN"}, allow_empty=True)
                self.need("WHEN")
                while True:
                    self.expression({"THEN"})
                    self.need("THEN")
                    self.sequence({"WHEN", "ELSE", "END"})
                    if not self.take("WHEN"):
                        break
                if self.take("ELSE"):
                    self.sequence({"END"})
            self.need("END")
            if word != "BEGIN":
                self.need(end_word)
            if label and restore_idents(self.word(), self.idents).lower() == restore_idents(label, self.idents).lower():
                self.pos += 1
        elif word in ("END", "ELSE", "ELSEIF", "WHEN", "THEN", "UNTIL", "EXCEPTION", "", ";"):
            self.refuse()
        elif word == "SET" and self.word(1) == "STATEMENT":
            self.pos += 2
            self.expression({"FOR"})
            self.need("FOR")
            if self.word() == "BEGIN" and self.word(1) in ("", ";", "WORK"):
                self.simple()  # Wrapped transaction BEGIN has no block END.
            else:
                self.statement()
        elif word == "DECLARE":
            self.pos += 1
            if self.word() in ("CONTINUE", "EXIT", "UNDO"):
                self.pos += 1
                self.need("HANDLER")
                self.need("FOR")
                while True:
                    if self.take("NOT"):
                        self.need("FOUND")
                    elif self.take("SQLSTATE"):
                        self.take("VALUE")
                        self.need("''")
                    else:
                        self.name()  # error code, named condition, SQLWARNING/SQLEXCEPTION
                    if not self.take(","):
                        break
                self.statement()
            else:
                self.simple()
        else:
            if label:  # labels only name blocks and loops
                self.refuse()
            self.simple()
        self.level -= 1

    def package(self):
        self.take("BODY")
        if self.take("IF"):
            self.need("NOT")
            self.need("EXISTS")
        self.name()
        self.characteristics()
        oracle = self.take("AS") or self.take("IS")
        while self.word() != "END":
            if self.word() in ("PROCEDURE", "FUNCTION"):
                kind = self.word()
                self.pos += 1
                self.routine(kind, member=True)
            elif oracle and self.word() == "BEGIN":
                self.statement()  # Oracle initializer shares the package END.
                break
            else:
                self.statement()  # Native initializer has its own END.
            self.need(";")
        else:
            self.need("END")
        if self.word() not in ("", ";"):
            self.name()


def _ddl_codes(unit):
    """Count boundaries without changing the shared client's SQL splitting."""
    code = unit["code"]
    routine = _ROUTINE_CREATE.match(code)
    if not routine:
        if _ROUTINE_CREATE.search(code):
            raise EditorError("One DDL per confirmation: run stored-program definitions as standalone statements; "
                              "nothing was sent.", "invalid_statement")
        return [part for part in code.split(";") if part.strip()]
    scan = _DdlBoundaryScanner(code, routine.end(), unit.get("idents") or [])
    kind = routine.group(2).upper()
    if kind == "PACKAGE":
        scan.package()
    elif kind in ("PROCEDURE", "FUNCTION"):
        scan.routine(kind)
    elif kind == "EVENT":
        if scan.take("IF"):
            scan.need("NOT")
            scan.need("EXISTS")
        scan.name()
        # Event schedule expressions may contain CASE/IF(), but its body
        # begins only at the top-level DO (MySQL sql_yacc.yy:3759-3818).
        scan.expression({"DO", ";"}, allow_empty=True)
        if scan.take("DO"):
            scan.statement()
        elif not code.lstrip().upper().startswith("ALTER"):
            scan.refuse()
    else:  # TRIGGER ... FOR EACH ROW [FOLLOWS/PRECEDES name] body
        scan.expression({"FOR"})
        scan.need("FOR")
        scan.need("EACH")
        scan.need("ROW")
        if scan.take("FOLLOWS") or scan.take("PRECEDES"):
            scan.name_or_text()
        scan.statement()
    if scan.word() not in ("", ";"):
        scan.refuse()
    end = scan.tokens[scan.pos].start() if scan.pos < len(scan.tokens) else len(code)
    # Do not parse trailing SQL as part of the definition. Even a stray END
    # or an unsupported second statement requires a separate confirmation.
    return [code[:end]] + [part for part in code[end:].split(";") if part.strip()]


def _ddl_parts(units):
    # Masked strings and comments contain no semicolons. A routine body is
    # part of its CREATE, not another immediately executed DDL statement.
    for unit in units:
        if unit.get("generated"):
            continue
        codes = _ddl_codes(unit)
        for code in codes:
            if _DDL_START.match(code) and not re.match(r"\s*DROP\s+PREPARE" + _SQL_BOUNDARY, code, re.I | re.ASCII):
                yield unit, code


_COMPOUND_START = re.compile(
    r"(?:^|;|" + _SQL_BOUNDARY + r"FOR\s+)\s*(?:" + _MASKED_NAME + r"\s*:\s*)?(?:"
    r"BEGIN" + _SQL_BOUNDARY + r"(?!\s*(?:WORK" + _SQL_BOUNDARY + r")?\s*(?:;|$))|"
    r"(?:DECLARE|IF|WHILE|LOOP|REPEAT|CASE|FOR)" + _SQL_BOUNDARY + r")", re.I | re.ASCII)


def _embedded_compound_ddl(unit):
    """DDL executed by an anonymous block cannot share the block's review.

    Use statement positions, not keyword substrings: a string, column name,
    qualified name or user variable must not become an executed DDL verb.
    An unsupported compound boundary cannot establish standalone safety.
    MariaDB sql/sql_yacc.yy:18696-18703,20812-20895.
    """
    code = unit["code"]
    if _DDL_START.match(code) or not (_COMPOUND_START.search(code) or
                                    re.search(r"(?:^|;)\s*SET\s+STATEMENT" + _SQL_BOUNDARY, code, re.I | re.ASCII)):
        return False
    scan = _DdlBoundaryScanner(code, 0, unit.get("idents") or [])
    try:
        while scan.word():
            if scan.word() == "BEGIN" and scan.word(1) in ("", ";", "WORK"):
                scan.simple()  # A transaction BEGIN, outside a compound.
            else:
                scan.statement()
            if scan.embedded_ddl:
                return True
            if not scan.word():
                break
            scan.need(";")
    except EditorError:
        return True
    return False


def _check_oracle_compounds(units, engine, sql_mode):
    """Anonymous PL/SQL has the same unsupported grammar as stored bodies."""
    from . import dbops
    possible = engine == "mariadb" and (sql_mode is None or dbops.oracle_mode(sql_mode, engine))
    for unit in units:
        if possible and not _DDL_START.match(unit["code"]) and _COMPOUND_START.search(unit["code"]):
            raise EditorError("Anonymous compound blocks in MariaDB ORACLE mode are not supported by the "
                              "Query editor. Use a session with a known mode without ORACLE.", "invalid_statement")
        if engine == "mariadb" and _unit_changes_mode(unit):
            mode = _literal_sql_mode(unit)
            # With Continue on error, a later assignment cannot prove that
            # a previously possible ORACLE mode has gone away.
            possible = possible or mode is None or dbops.oracle_mode(mode, engine)


def _alter_unit(plan):
    return next((u for u in plan["units"] if _ALTER_TABLE.match(u["code"])), None)


def _alter_index_name(unit):
    """Name one index from the reviewed, masked SQL, never client metadata.

    Fail closed for compound ALTERs. The index actions use these single
    clauses; the existing splitter owns comments and quoted identifiers.
    """
    if unit is None:
        return None
    table = _ALTER_TABLE.match(unit["code"])
    if table is None:
        return None
    clause = unit["code"][table.end():].strip().rstrip(';').strip()
    if re.fullmatch(r'DROP\s+PRIMARY\s+KEY', clause, re.I):
        return 'PRIMARY'
    match = re.fullmatch(r'(?:ALTER\s+INDEX\s+(' + _SQL_NAME +
                         r')\s+(?:VISIBLE|INVISIBLE|IGNORED|NOT\s+IGNORED)|DROP\s+(?:INDEX|KEY)\s+(' +
                         _SQL_NAME + r'))', clause, re.I)
    return restore_idents(match.group(1) or match.group(2), unit["idents"]) if match else None


def _alter_binding(session, review):
    # DATABASE() can change between requests on the same tab. Bind unqualified
    # ALTERs to that schema as well as the live session and server identity.
    return [session.connection_id, session.server_identity, review.get("schema"), review.get("table")]


def _review_digest(tab_id, identity, statements, decision, on_error, session=None, plan=None):
    # Continuing after an error can send writes a stop-mode review would not.
    # Startup can reveal a different decision for identical SQL. Other unused
    # provisional tokens must not match that newly bound review. The private
    # per-client tag also distinguishes a replacement session in the same tab.
    binding = session.statement_tag if session is not None else None
    review = _run_review_state(plan, decision) if plan is not None else None
    return _digest(tab_id, identity, statements,
                   ["run", binding, decision.get("alter_binding"), on_error, review])


def _alter_review(plan, session):
    """Read the ALTER target and reuse Locks' readers in the editor session.

    Holding run_lock prevents helpers interleaving with another run. Reads
    are advisory snapshots; unavailable instrumentation is explicitly unknown.
    """
    from . import locks
    unit = _alter_unit(plan)
    match = _ALTER_TABLE.match(unit["code"])
    names = [restore_idents(n, unit["idents"]) for n in match.groups() if n is not None]

    class Reader(object):
        def query_lock_rows(self, sql):
            step = session._helper(sql)
            if not step.marker or step.real_errors() or session.ended:
                raise ConnectorError(session._helper_problem(step) or "Preflight read did not complete.")
            return [row for entry in step.entries if entry["type"] == "rows" for row in entry["rows"]]

    if not session._acquire_run_lock():
        raise EditorError("A statement is already running in this tab. Wait for it or press Stop.", "busy")
    try:
        reader = Reader()
        facts = reader.query_lock_rows(TAG + "SELECT DATABASE(), @@performance_schema")
        schema = names[0] if len(names) == 2 else (facts[0][0] if facts and facts[0] else None)
        ps_on = bool(facts and len(facts[0]) > 1 and str(facts[0][1]).upper() in ("1", "ON"))
        review = locks.alter_table_preflight(reader, schema, names[-1], session.engine,
                                             session.server_version, ps_on)
        try:
            review["prediction"] = _alter_prediction(reader, unit["send"], schema, names[-1], session.engine,
                                                     session.server_version)
        except Exception as exc:  # advisory: a prediction problem never blocks the review
            review["prediction"] = {"predicted": False, "algorithm": None, "rebuild": None,
                                    "concurrent_dml": None, "summary": None, "text": None, "clauses": [],
                                    "notes": [], "needs": [], "source": "",
                                    "reason": "Not predicted: %s." % exc.__class__.__name__}
        if review["prediction"].get("algorithm") == "INSTANT":
            # On MySQL, ALGORITHM=INPLACE turns an instant ADD/DROP COLUMN into
            # a rebuild (handler0alter.cc:1053-1056), so never suggest it here.
            review["suggestion"] = None
            review["warnings"].append("No rewrite offered: the server is predicted to run this ALTER instantly.")
        else:
            review["suggestion"] = _alter_suggestion(unit, review, session.engine, session.server_version)
            if review["suggestion"]:
                review["warnings"].append("Optional rewrite: INPLACE and LOCK=NONE request an online operation; "
                    "the server checks support. INPLACE can still rebuild the table. Copy the SQL into the editor "
                    "and review it again to use it.")
            else:
                review["warnings"].append("No rewrite offered: this form, engine, version or existing "
                                           "ALGORITHM/LOCK clause is not covered by the verified suggestion.")
        review["warnings"].append("A COPY or rebuild can need extra disk space and delay replicas. "
                                   "Check Server > Locks while ALTER runs. The lock wait limit is per wait, "
                                   "not a limit on the ALTER's total duration.")
        return review
    finally:
        session.run_lock.release()


# Session settings alter_algorithm.predict reads, per engine. Every name
# exists on the supported versions (MySQL 8.4+, MariaDB 10.11+); MariaDB's
# alter_algorithm is read only before 11.5, which ignores it.
_PREDICTION_SETTINGS = {
    "mysql": ("foreign_key_checks", "sql_mode", "innodb_page_size", "old_alter_table"),
    "mariadb": ("foreign_key_checks", "sql_mode", "innodb_page_size", "innodb_strict_mode",
                "innodb_instant_alter_column_allowed", "old_mode", "innodb_file_per_table"),
}
_BOOLEAN_SETTINGS = ("foreign_key_checks", "old_alter_table", "innodb_strict_mode", "innodb_file_per_table")
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_]+\Z")


def _setting(name, value):
    if value is None:
        return None
    if name in _BOOLEAN_SETTINGS:
        return str(value).upper() in ("1", "ON", "TRUE")
    if name == "innodb_page_size":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return str(value)


def _alter_prediction(reader, send, schema, table, engine, version):
    """alter_algorithm.predict for the reviewed ALTER, from the table's SHOW
    CREATE TABLE and the session's settings. Read-only and bounded: SHOW
    CREATE TABLE, one SELECT of settings, then only the facts the prediction
    asks for (InnoDB's dictionary row of the table, the columns other tables'
    foreign keys reference, the default collations). A failed read leaves its
    facts unknown; a statement that depends on them is not predicted."""
    from . import alter_algorithm
    from .locks import _lit
    from .server_info import server_version_tuple

    def unread(reason):
        return {"predicted": False, "algorithm": None, "rebuild": None, "concurrent_dml": None,
                "summary": None, "text": None, "reason": reason, "source": "", "clauses": [], "notes": [],
                "needs": []}

    if engine not in ("mysql", "mariadb"):
        return unread("Only MySQL and MariaDB are predicted.")
    if not schema or not table:
        return unread("Not predicted: the table's database is not known.")
    quoted = "`%s`.`%s`" % (schema.replace("`", "``"), table.replace("`", "``"))
    try:
        rows = reader.query_lock_rows(TAG + "SHOW CREATE TABLE " + quoted)
    except ConnectorError as exc:
        return unread("Not predicted: SHOW CREATE TABLE failed: %s" % exc)
    if not rows or len(rows[0]) < 2 or not rows[0][1] or not str(rows[0][1]).lstrip().upper().startswith("CREATE"):
        return unread("Not predicted: SHOW CREATE TABLE returned no table definition.")
    ddl = rows[0][1]
    facts = {}
    names = list(_PREDICTION_SETTINGS[engine])
    if engine == "mariadb" and server_version_tuple(version or "") < (11, 5, 0):
        names.append("alter_algorithm")
    try:
        row = reader.query_lock_rows(TAG + "SELECT " + ", ".join("@@" + name for name in names))
        if row:
            facts.update((name, _setting(name, value)) for name, value in zip(names, row[0]))
    except ConnectorError:
        pass  # predict() states the defaults it assumes instead
    prediction = alter_algorithm.predict(send, ddl, engine, version, facts)
    needs = set(prediction.get("needs") or [])
    more = False
    if needs & {"total_row_versions", "fts_doc_id_index", "system_tablespace"} and _PLAIN_NAME.match(schema) \
            and _PLAIN_NAME.match(table):
        # InnoDB names a table "db/table" (lowercase with lower_case_table_names);
        # other characters are encoded, so only plain names are looked up.
        name = "%s/%s" % (schema, table)
        targets = ", ".join(_lit(n) for n in sorted({name, name.lower()}))
        if engine == "mysql":
            sql = ("SELECT t.TOTAL_ROW_VERSIONS, (SELECT COUNT(*) FROM information_schema.INNODB_INDEXES i "
                   "WHERE i.TABLE_ID = t.TABLE_ID AND i.NAME = 'FTS_DOC_ID_INDEX') "
                   "FROM information_schema.INNODB_TABLES t WHERE t.NAME IN (%s)" % targets)
        else:
            sql = ("SELECT t.SPACE, (SELECT COUNT(*) FROM information_schema.INNODB_SYS_INDEXES i "
                   "WHERE i.TABLE_ID = t.TABLE_ID AND i.NAME = 'FTS_DOC_ID_INDEX') "
                   "FROM information_schema.INNODB_SYS_TABLES t WHERE t.NAME IN (%s)" % targets)
        try:
            found = reader.query_lock_rows(TAG + sql)
        except ConnectorError:
            found = []
        if len(found) == 1 and len(found[0]) >= 2:
            first, fts = found[0][0], found[0][1]
            try:
                if engine == "mysql":
                    facts["total_row_versions"] = int(first)
                else:
                    facts["system_tablespace"] = int(first) == 0
                facts["fts_doc_id_index"] = int(fts) > 0
                more = True
            except (TypeError, ValueError):
                pass
    if "referenced_columns" in needs:
        if engine == "mysql":
            sql = ("SELECT DISTINCT REFERENCED_COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                   "WHERE REFERENCED_TABLE_SCHEMA = %s AND REFERENCED_TABLE_NAME = %s LIMIT 1001"
                   % (_lit(schema), _lit(table)))
        elif _PLAIN_NAME.match(schema) and _PLAIN_NAME.match(table):
            # InnoDB's own dictionary, rather than an information_schema scan of every table.
            name = "%s/%s" % (schema, table)
            sql = ("SELECT DISTINCT c.REF_COL_NAME FROM information_schema.INNODB_SYS_FOREIGN f "
                   "JOIN information_schema.INNODB_SYS_FOREIGN_COLS c ON c.ID = f.ID WHERE f.REF_NAME IN (%s) "
                   "LIMIT 1001" % ", ".join(_lit(n) for n in sorted({name, name.lower()})))
        else:
            sql = None
        if sql:
            try:
                found = reader.query_lock_rows(TAG + sql)
                if len(found) <= 1000:
                    facts["referenced_columns"] = [r[0] for r in found if r and r[0]]
                    more = True
            except ConnectorError:
                pass
    if "default_collations" in needs and engine == "mariadb":
        try:
            found = reader.query_lock_rows(TAG + "SELECT CHARACTER_SET_NAME, DEFAULT_COLLATE_NAME "
                                           "FROM information_schema.CHARACTER_SETS")
            facts["default_collations"] = {r[0]: r[1] for r in found if len(r) >= 2}
            more = True
        except ConnectorError:
            pass
    if more:
        prediction = alter_algorithm.predict(send, ddl, engine, version, facts)
        if "referenced_columns" in facts and engine == "mysql":
            prediction["notes"].append("Foreign keys in tables this account cannot see are not counted.")
    return prediction


def _alter_suggestion(unit, review, engine, version):
    """Offer constraints, never promise an algorithm succeeds or edit SQL.

    MySQL sql/sql_table.cc:17360-17367 rejects INSTANT with LOCK=NONE;
    :18470-18525 checks INPLACE/NONE and rejects an incompatible fallback.
    mysql-8.4.0 has the same checks at :16499-16506 and :17604-17666.
    MariaDB sql/sql_alter.cc:139-223 checks the requested algorithm/lock
    (mariadb-10.11.1:136-215).
    Restrict the offer to ordinary ADD/DROP INDEX/COLUMN on supported InnoDB.
    """
    from .server_info import server_version_tuple
    ver = server_version_tuple(version)
    supported = ((engine == "mysql" and ver >= (8, 4, 0)) or
                 (engine == "mariadb" and ver >= (10, 11, 0)))
    code = unit["code"]
    match = _ALTER_TABLE.match(code)
    if (not supported or (review.get("table_engine") or "").upper() != "INNODB" or not match
            or re.search(_SQL_BOUNDARY + r"(?:ALGORITHM|LOCK|PARTITION|WAIT|NOWAIT|ONLINE|OFFLINE|IGNORE)" + _SQL_BOUNDARY, code, re.I | re.ASCII)
            or not re.match(r"\s*(?:ADD\s+(?:COLUMN|(?:UNIQUE\s+)?(?:INDEX|KEY))|DROP\s+(?:COLUMN|INDEX|KEY))" + _SQL_BOUNDARY,
                            code[match.end():], re.I | re.ASCII)
            or "/*" in unit["send"] or "--" in unit["send"] or "#" in unit["send"]):
        return None
    return unit["send"] + ", ALGORITHM=INPLACE, LOCK=NONE"


def _policy_changed(payload, store, identity, safety, audit, statements):
    """db-operations lifecycle step 6: a reviewed policy cannot survive drift."""
    facts = _connection_facts(payload, store)
    if facts[1] == identity and facts[2] == safety:
        return None
    message = "The connection policy changed after review; nothing was sent. Run again under the new policy."
    audit_id = audit.outcome(statements, "refused", outcome="connection_changed", message=message)
    return {"status": "refused", "message": message, "audit_id": audit_id}


def _plan_run(payload, session):
    sql = payload.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise EditorError("Enter SQL to run.", "empty")
    if len(sql) > MAX_SQL:
        raise EditorError("SQL text is limited to 1,000,000 characters.", "too_long")
    offsets = Offsets(sql)
    backslash = (True if session is None else
                 session.backslash_escapes if session.sql_mode is not None else None)
    context = _lexer_session_context(session)
    start, end, scope, delimiter = _selection(payload, sql, offsets, backslash, context)
    text = sql[start:end]
    engine = session.engine if session is not None else None
    version = getattr(session, "server_version", None)
    units = plan_statements(text, backslash, base=start, delimiter=delimiter, **context)
    _check_oracle_compounds(units, engine, context["sql_mode"])
    explain_format = payload.get("explain_format")
    if explain_format is not None:
        from . import dbops
        if explain_format not in ("table", "tree", "json"):
            raise EditorError("Choose Table, Tree or JSON for Explain.", "invalid")
        # A custom DELIMITER must not smuggle an unprefixed second statement
        # into this read. Keep the normal splitter and client-command checks.
        if (len(units) != 1 or len([part for part in units[0]["code"].split(";") if part.strip()]) != 1 or
                len(dbops.split_statements(units[0]["send"], True if backslash is None else backslash,
                                          engine=engine, server_version=version,
                                          sql_mode=units[0].get("sql_mode"), client_engine=context["client_engine"])) != 1):
            raise EditorError("Explain one statement at a time.", "invalid_statement")
        unit = units[0]
        if re.match(r"\s*(?:(?:FORMAT\s*=\s*\w+|EXTENDED|PARTITIONS)\s+)*ANALYZE" + _SQL_BOUNDARY, unit["code"], re.I | re.ASCII):
            raise EditorError("ANALYZE runs the statement. Use Run analysis or Run.", "invalid_statement")
        if explain_format == "tree" and session is not None:
            explain_version = parse_version_tuple(session.server_version or "", leading=True, default=None)
            if engine != "mysql" or explain_version is None or explain_version < (8, 0, 16):
                raise EditorError("Tree format needs MySQL 8.0.16 or later. Choose Table or JSON.", "invalid")
        prefix = ("EXPLAIN " if engine == "mariadb" and explain_format == "table"
                  else "EXPLAIN FORMAT=%s " % {"table": "TRADITIONAL", "tree": "TREE", "json": "JSON"}[explain_format])
        # :name placeholders (a Top statements digest) take their Parameters
        # values as the literals Run analysis uses; a statement without one
        # is sent as written.
        parameters = payload.get("parameters")
        if parameters is not None:
            from . import live
            try:
                filled = live.fill_parameters(unit["send"], parameters)
            except ValueError as e:
                raise EditorError(str(e), "invalid")
            if filled != unit["send"]:
                meanings = [dbops._mode_reading(dbops.split_statements(
                    unit["send"], engine=engine, server_version=version, **reading))
                    for reading in dbops.lexer_readings(None, engine, server_version=version,
                                                       client_engine=context["client_engine"], sql=unit["send"])]
                if any(meaning != meanings[0] for meaning in meanings[1:]):
                    raise EditorError("A backslash before a quote is read differently when sql_mode has "
                                      "NO_BACKSLASH_ESCAPES, ANSI_QUOTES or MSSQL. Use unambiguous quoted "
                                      "text to use Parameters.", "invalid")
                unit["send"] = filled
                filled_units = plan_statements(filled, backslash, delimiter=unit.get("delimiter") or ";", **context)
                if len(filled_units) != 1:
                    raise EditorError("Explain one statement at a time.", "invalid_statement")
                for key in ("code", "idents", "mode_sensitive", "conditional_sql"):
                    unit[key] = filled_units[0][key]
        # Only prepend EXPLAIN: no SELECT projection, transaction wrapper or
        # session setting changes. The server decides whether the statement
        # can be explained.
        unit["send"] = prefix + unit["send"]
        unit["code"] = prefix + unit["code"]
        unit["explain_prefix"] = len(prefix)
    # The setup remains visible and audited, but shares the guarded packet
    # with ALTER so a proxy cannot separate the setting from its use.
    guarded = []
    for unit in units:
        if _ALTER_TABLE.match(unit["code"]):
            guard = plan_statements(ALTER_LOCK_WAIT, base=unit["start"])[0]
            guard.update(generated=True, start=unit["start"], end=unit["start"],
                         mode_unknown=unit.get("mode_unknown", False))
            unit["packet_setup"] = guard
            guarded.append(guard)
        guarded.append(unit)
    units = guarded
    for index, unit in enumerate(units):
        unit["index"] = index
    verdict = classify_units(units, None if session is None or session.sql_mode is None else backslash, text,
                             engine, version, context["wsrep_on"])
    return {"sql": sql, "text": text, "offsets": offsets, "scope": scope, "units": units, "verdict": verdict}


_PRIVILEGES_LOCK = threading.Lock()


def begin_privileges_read(workspace, payload):
    """Invalidate the old hint before a refresh, returning a generation.

    Only ui's existing privileges read populates this memory-only cache.
    Preview never loads credentials, opens a connection, or performs reads.
    """
    identity = _connection_facts(payload, workspace.store)[1]
    generation = object()
    with _PRIVILEGES_LOCK:
        cache = getattr(workspace, "_editor_privileges", {})
        if len(cache) >= 16:
            cache = {}
        cache[identity] = (generation, None)
        workspace._editor_privileges = cache
    return identity, generation


def finish_privileges_read(workspace, ticket, report):
    identity, generation = ticket
    with _PRIVILEGES_LOCK:
        cache = getattr(workspace, "_editor_privileges", {})
        if cache.get(identity, (None,))[0] is generation and report.get("schema_version") == "privileges-1.0":
            cache[identity] = (generation, report)


def invalidate_privileges(workspace):
    """Also discards in-flight reads made before an account mutation."""
    with _PRIVILEGES_LOCK:
        workspace._editor_privileges = {}


def _changes_privilege_context(unit):
    # CALL/prepared SQL may change roles indirectly. Temporary tables also
    # have session-specific privilege/read_only exceptions. These contexts
    # stay unverified until a new session; never try to infer the role graph.
    return unit.get("category") == "account" or bool(re.search(_SQL_BOUNDARY + r"SET\s+ROLE" + _SQL_BOUNDARY + r"|" + _SQL_BOUNDARY + r"(?:CREATE|DROP)\s+TEMPORARY\s+TABLE" + _SQL_BOUNDARY + r""
                          r"|^\s*(?:CALL|EXECUTE)" + _SQL_BOUNDARY, unit["code"], re.I | re.ASCII))


def _preview_api(payload, workspace):
    from . import privileges
    tab_id = _tab_id(payload)
    options, identity, safety, _name = _connection_facts(payload, workspace.store)
    registry = getattr(workspace, "editor", None)
    session = _session_for(registry, tab_id, identity) if registry is not None else None
    # Ended sessions retain their previous owner's results, never its hints.
    if session is not None and (session.ended or session.identity != identity):
        session = None
    try:
        plan = _plan_run(payload, session)
    except EditorError as exc:
        return {"status": "invalid", "code": exc.code, "message": str(exc), "statements": [],
                "aggregate": {"state": "unknown", "missing": [], "reason": str(exc), "warning": ""}}
    with _PRIVILEGES_LOCK:
        model = getattr(workspace, "_editor_privileges", {}).get(identity, (None, None))[1]
    changed = session is not None and (session.privileges_changed or session.busy())
    statements = []
    for unit in plan["units"]:
        answer = privileges.statement_privileges(model, unit, database=options.get("database"))
        if changed:
            answer.update(state="unknown", missing=[], reason=(
                "This tab may have changed roles or session privilege context (SET ROLE); "
                "a new-session report cannot verify its privileges."))
        statements.append(dict(answer, index=unit["index"], kind=unit["kind"]))
        # Even before execution, do not project old roles onto later units.
        changed = changed or _changes_privilege_context(unit)
    state = ("denied" if all(s["state"] == "denied" for s in statements) else
             "allowed" if all(s["state"] == "allowed" for s in statements) else "unknown")
    denied = [s for s in statements if s["state"] == "denied"]
    missing = list(dict.fromkeys(p for s in statements for p in s["missing"]))
    reasons = list(dict.fromkeys(s["reason"] for s in statements if s["state"] == state))
    warning = ("Some statements are denied: " + " ".join("Statement %d: %s" % (s["index"] + 1, s["reason"])
                                                        for s in denied)) if denied and state != "denied" else ""
    reason = " ".join(reasons) or "Statement privileges are not fully verified; the server decides."
    policy = _decision(plan, safety, session)
    return {"status": "ready", "scope": plan["scope"], "statements": statements,
            "aggregate": {"state": state, "missing": missing, "reason": reason, "warning": warning},
            "policy": {"action": policy["action"], "text": policy["text"]}}


def restore_idents(code, idents):
    """*code* (dbops masked code) with quoted identifiers back as names."""
    def name(match):
        index = int(match.group(1))
        return idents[index] if index < len(idents) else match.group(0)
    return re.sub(r"`(\d+)`", name, code)


def _read_only_problem(units):
    for unit in units:
        if _READ_WRITE.search(restore_idents(unit["code"], unit.get("idents") or [])):
            return ("Statement %d would let this session write. The connection is read-only in myflames, so "
                    "nothing was sent." % (unit["index"] + 1))
    return None


#: Statements that commit an open transaction first (MySQL and MariaDB
#: "statements that cause an implicit commit"); CREATE and DROP TEMPORARY
#: TABLE do not.
_IMPLICIT_COMMIT = re.compile(
    r"^\s*(?:(?:CREATE|DROP)" + _SQL_BOUNDARY + r"(?!\s+(?:OR\s+REPLACE\s+)?TEMPORARY" + _SQL_BOUNDARY + r")|(?:ALTER|RENAME|TRUNCATE)" + _SQL_BOUNDARY + r""
    r"|(?:GRANT|REVOKE)" + _SQL_BOUNDARY + r"|SET\s+PASSWORD" + _SQL_BOUNDARY + r"|SET\s+DEFAULT\s+ROLE" + _SQL_BOUNDARY + r"|(?:LOCK|UNLOCK)\s+(?:TABLES?|INSTANCE)" + _SQL_BOUNDARY + r""
    r"|START\s+TRANSACTION" + _SQL_BOUNDARY + r"|BEGIN" + _SQL_BOUNDARY + r"(?!\s+NOT\s+ATOMIC)"
    r"|(?:ANALYZE|OPTIMIZE|REPAIR|CHECK)\s+(?:NO_WRITE_TO_BINLOG\s+|LOCAL\s+)?TABLES?" + _SQL_BOUNDARY + r""
    r"|(?:FLUSH|RESET)" + _SQL_BOUNDARY + r"|CACHE\s+INDEX" + _SQL_BOUNDARY + r"|LOAD\s+INDEX" + _SQL_BOUNDARY + r"|(?:INSTALL|UNINSTALL)\s+(?:PLUGIN|COMPONENT|SONAME)" + _SQL_BOUNDARY + r""
    r"|(?:START|STOP)\s+(?:REPLICA|SLAVE|GROUP_REPLICATION|ALL\s+SLAVES)" + _SQL_BOUNDARY + r"|CHANGE\s+(?:REPLICATION|MASTER)" + _SQL_BOUNDARY + r")",
    re.I | re.ASCII)
def _may_enable_autocommit(code, idents):
    """Recognize assignment targets, not autocommit mentioned in a value.

    Reuse the classifier's SET grammar and top-level comma handling. A
    nonconstant or masked string value may enable autocommit; only literal
    0/OFF/FALSE proves otherwise. User variables and global defaults do not
    commit this session's transaction.
    """
    from . import dbops
    start = re.match(r"\s*SET" + _SQL_BOUNDARY, code, re.I | re.ASCII)
    if not start:
        return False
    rest = code[start.end():].upper()
    cuts = [-1] + [i for i, char in enumerate(dbops._flat(rest)) if char == ","] + [len(rest)]
    for a, b in zip(cuts, cuts[1:]):
        piece = rest[a + 1:b]
        assignment = dbops._SET_ITEM.match(piece)
        if not assignment or assignment.group(4):  # @user_variable
            continue
        scope = assignment.group(1) or assignment.group(2)
        if scope in ("GLOBAL", "PERSIST", "PERSIST_ONLY"):
            continue
        name = dbops._name(re.split(r"\s*\.\s*", assignment.group(5))[-1], idents).lower()
        # Double-quoted identifiers under ANSI_QUOTES are masked as strings.
        # Their name cannot prove that this is a different session variable.
        if name in ("autocommit", "''") and not re.fullmatch(r"\s*(?:0|OFF|FALSE)\s*", piece[assignment.end():]):
            return True
    return False


def _unwrap_set_statement(code):
    """Expose a MariaDB SET STATEMENT's executable SQL for transaction checks.

    Use the shared classifier's FOR boundary: expressions may contain a
    parenthesized FOR or NEXT/PREVIOUS VALUE FOR. MariaDB sql_yacc.yy:17472-17486
    accepts a directly_executable_statement after this boundary.
    """
    from . import dbops
    code = code.upper()
    while re.match(r"\s*SET\s+STATEMENT" + _SQL_BOUNDARY, code):
        found = dbops._set_statement_for(dbops._flat(code))
        if found is None:
            break
        code = code[found.end():]
    return code


def implicit_commit(unit):
    """True when the statement commits an open transaction before it runs."""
    return any(_IMPLICIT_COMMIT.match(part) or _may_enable_autocommit(part, unit.get("idents") or [])
               for part in (_unwrap_set_statement(p) for p in unit["code"].split(";")))


#: COMMIT or ROLLBACK, except ROLLBACK [WORK] TO [SAVEPOINT] name, which
#: returns to a savepoint inside the open transaction (SQLCOM_ROLLBACK_TO_
#: SAVEPOINT: M/sql/sql_yacc.yy:17994-18000, Ma/sql/sql_yacc.yy:18751-18762).
_ENDING_CONTROL = re.compile(r"\s*(?:COMMIT|ROLLBACK(?!\s+(?:WORK\s+)?TO" + _SQL_BOUNDARY + r"))" + _SQL_BOUNDARY, re.I | re.ASCII)
#: ER_PARSE_ERROR: the server rejected the text before running any of it.
_PARSE_ERROR = 1064


def may_replace_transaction(unit, errors):
    """True when *unit*, as it ran, may have ended the session's transaction,
    so an age observed before it is no longer a lower bound for the
    transaction open afterwards.

    COMMIT, ROLLBACK (not to a savepoint) and statements that commit
    implicitly end it when they run or are stopped; failed DDL may have
    committed before its error. A COMMIT or ROLLBACK that fails is kept:
    it either fails its state check first and changes nothing, or clears
    SERVER_STATUS_IN_TRANS before the engine call, which rolls back on
    failure (M/sql/transaction.cc:242-247 and 399-406, Ma/sql/transaction.cc:
    262-271 and 379-388), and the run stops at it. So the state read after
    the run decides: a transaction still open then is the same one. A
    failing BEGIN can leave a new one open (Ma/sql/handler.cc:3475-3478 after
    Ma/sql/transaction.cc:205-206), so it is not kept. A parse error ran
    nothing.
    """
    if unit.get("status") not in ("ok", "error", "stopped"):
        return False
    # Under a custom DELIMITER one client statement can hold several server
    # statements; code masks strings and comments, so ';' separates them.
    parts = [_unwrap_set_statement(part) for part in unit["code"].split(";") if part.strip()]
    if not any(_ENDING_CONTROL.match(part) or implicit_commit(dict(unit, code=part)) for part in parts):
        return False
    if unit["status"] != "error" or len(parts) > 1 or not unit.get("single", True):
        return True
    codes = [error.get("code") for error in errors or []]
    if codes and all(code == _PARSE_ERROR for code in codes):
        return False
    return not _ENDING_CONTROL.match(parts[0])


def _commit_warnings(units, session):
    """Warnings for statements that would commit a transaction that is (or
    may be) open in the session."""
    if session is None or session.in_transaction is False:
        return []
    state = "the open transaction" if session.in_transaction else "a transaction that may be open (unknown here)"
    return ["Statement %d (%s) commits %s first." % (u["index"] + 1, u.get("verb") or "this statement", state)
            for u in units if implicit_commit(u)]


def _decision(plan, safety, session=None):
    from . import dbops
    decision = dbops.decide(plan["verdict"], safety["type"], safety["read_only"], safety.get("profile_name"))
    if safety["read_only"]:
        problem = _read_only_problem(plan["units"])
        if problem:
            decision = dict(decision, action="refuse", title="Read-only connection", text=problem)
    embedded = any(_embedded_compound_ddl(u) or
                   (u.get("kind") != "read" and not _DDL_START.match(u["code"]) and
                   re.search(_SQL_BOUNDARY + r"(?:ALTER\s+(?:(?:ONLINE|OFFLINE|IGNORE)\s+)*TABLE|"
                             r"DROP\s+(?:DATABASE|SCHEMA|(?:TEMPORARY\s+)?TABLE)|"
                             r"CREATE\s+(?:TEMPORARY\s+)?TABLE|RENAME\s+TABLE|TRUNCATE\s+TABLE)" + _SQL_BOUNDARY,
                             u["code"], re.I | re.ASCII)) for u in plan["units"])
    if embedded and decision["action"] != "refuse":
        decision.update(action="refuse", text="DDL inside a compound statement cannot be reviewed safely; "
                        "submit it as standalone SQL so its target and lock safeguards can be reviewed. Nothing was sent.")
    if embedded:
        return dict(decision, warnings=_commit_warnings(plan["units"], session),
                    in_transaction=session.in_transaction if session is not None else False)
    ddl = list(_ddl_parts(plan["units"]))
    if decision["action"] != "refuse":
        if len(ddl) > 1 or any(len(_ddl_codes(u)) > 1 for u, _ in ddl):
            decision.update(action="refuse", title="One DDL per confirmation",
                            text="Run DDL statements one at a time, with a separate confirmation for each; nothing was sent.")
        elif _alter_unit(plan) and len([u for u in plan["units"] if not u.get("generated")]) != 1:
            decision.update(action="refuse", title="Review ALTER separately",
                            text="Run ALTER TABLE on its own so the preflight reviews its current target; nothing was sent.")
        elif ddl and decision["action"] == "allow":
            decision.update(action="confirm", title="Confirm DDL", tone="danger",
                            text="Each DDL statement needs its own confirmation.")
    for unit, code in ddl:
        match = _DROP_DATABASE.match(code)
        if match:
            decision["typed_target"] = restore_idents(match.group(1), unit["idents"])
        elif re.match(r"\s*DROP\s+(?:DATABASE|SCHEMA)" + _SQL_BOUNDARY, code, re.I | re.ASCII) and decision["action"] != "refuse":
            decision.update(action="refuse", text="The DROP database target could not be verified; nothing was sent.")
    # Independent of block counting: creating a stored program must never
    # hide a destructive database target. The current confirmation has one
    # typed field, so distinct targets cannot share a review.
    targets = set()
    for unit in plan["units"]:
        if not _ROUTINE_CREATE.search(unit["code"]):
            continue
        tokens = [match.group() for match in _MASKED_TOKEN.finditer(unit["code"])]
        keywords = [word.upper() if word.isascii() else word for word in tokens]
        for index in range(len(tokens) - 1):
            if keywords[index] != "DROP" or keywords[index + 1] not in ("DATABASE", "SCHEMA"):
                continue
            target = index + 2
            if keywords[target:target + 2] == ["IF", "EXISTS"]:
                target += 2
            if target >= len(tokens) or not re.fullmatch(_SQL_NAME, tokens[target]):
                decision.update(action="refuse", text="The DROP database target could not be verified; nothing was sent.")
            else:
                targets.add(restore_idents(tokens[target], unit["idents"]))
    if len(targets) > 1:
        decision.update(action="refuse", title="One database target per confirmation",
                        text="This definition drops more than one database. Review each DROP separately; nothing was sent.")
    elif targets:
        decision["typed_target"] = next(iter(targets))
    alter = _alter_unit(plan)
    if alter is not None and decision["action"] != "refuse":
        tail = alter["code"][_ALTER_TABLE.match(alter["code"]).end():]
        wait = re.match(r"\s+WAIT\s+(\d+)" + _SQL_BOUNDARY, tail, re.I | re.ASCII)
        if ((wait and int(wait.group(1)) > ALTER_LOCK_WAIT_SECONDS) or
                re.search(_SQL_BOUNDARY + r"SET_VAR\s*\([^)]*" + _SQL_BOUNDARY + r"lock_wait_timeout\s*=", alter["send"], re.I | re.ASCII)):
            decision.update(action="refuse", text="This ALTER overrides the five-second metadata-lock wait. "
                            "Remove the timeout override and review it again; nothing was sent.")
    warnings = _commit_warnings(plan["units"], session)
    decision = dict(decision, warnings=warnings,
                    in_transaction=session.in_transaction if session is not None else False)
    if warnings and decision["action"] == "allow":
        decision.update(action="confirm", title="Commits the open transaction", tone="danger",
                        text="A statement in this run commits the transaction open in this session before it runs, "
                             "so myflames asks first.")
    return decision


def _confirmation(decision, token, statements, name, options):
    return {"token": token, "expires_in": int(TOKEN_TTL), "title": decision["title"], "text": decision["text"],
            "tone": decision["tone"], "kind": decision["kind"], "category": decision["category"],
            "reasons": decision["reasons"], "warnings": list(decision.get("warnings") or []),
            "typed_target": decision.get("typed_target"), "alter_preflight": decision.get("alter_preflight"),
            "in_transaction": decision.get("in_transaction"), "statements": statements,
            "count": len(statements), "connection_type": decision["connection_type"],
            "connection": {"name": name, "host": options.get("host"), "port": options.get("port"),
                           "user": options.get("user")}}


def _audit_trail(store, operation, safety, options, password, decision):
    from . import dbops
    return dbops.AuditTrail(store, operation, "ui",
                            connection={k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user",
                                                                    "ssh_port")}, safety=safety,
                            password=password, policy=dbops.policy_summary(decision))


def _gate(registry, decision, payload, digest, audit, statements, write_statements, name, options):
    """None when the request may go ahead; else the response to send. A
    confirmation needs ``confirmed: true`` and the one-time token issued for
    exactly these statements (TOKEN_TTL seconds)."""
    if payload.get("decline") is True:
        # A decline never runs anything, whatever the policy says now (the
        # sheet may have been answered after the connection's type changed).
        audit_id = None
        if write_statements:
            audit_id = audit.outcome(statements, "declined", outcome="declined",
                                     message="The confirmation was cancelled; nothing was sent.")
        return {"status": "declined", "message": "Nothing was sent.", "audit_id": audit_id}
    if decision["action"] == "refuse":
        audit_id = audit.outcome(statements, "refused", outcome="read_only" if decision.get("read_only") else "ddl_policy", message=decision["text"])
        return {"status": "refused", "message": decision["text"], "decision": decision, "audit_id": audit_id}
    if decision["action"] == "confirm":
        target = decision.get("typed_target")
        if (payload.get("confirmed") is not True
                or (target is not None and payload.get("typed_target") != target)
                or not registry.take_token(payload.get("confirm_token"), digest)):
            audit_id = audit.outcome(statements, "refused", outcome="needs_confirmation", message=decision["text"])
            token = registry.issue_token(digest)
            return {"status": "needs_confirmation", "audit_id": audit_id,
                    "confirmation": _confirmation(decision, token, statements, name, options)}
    return None


def _start_preference(payload):
    value = payload.get("safe_updates")
    if value is not None and not isinstance(value, bool):
        raise ValueError("safe_updates must be true, false or null.")
    return value


def _open_session(registry, tab_id, options, identity, safety, name, payload, defer_safe_updates=False,
                  created=None):
    """The tab's session, started if needed. It starts with sql_safe_updates
    on for Production (or when the request asks for it); turning it off goes
    through _lower_safe_updates. *created*, a dict, gets ``session`` when
    this call started a new session."""
    wanted = _start_preference(payload)
    idle = int(_number(payload, "idle_minutes", DEFAULT_IDLE_SECONDS // 60, 1, MAX_IDLE_SECONDS // 60,
                       "idle_minutes") * 60)
    created = {} if created is None else created

    def make():
        session = EditorSession(tab_id, identity, options, safety["type"], safety["read_only"],
                                True if wanted else None, idle, name,
                                (payload.get("connection") or {}).get("profile_id") or None)
        created["session"] = session
        return session
    session = registry.open(tab_id, make, identity)
    if created.get("session") is not session:
        session.ready.wait(HELPER_TIMEOUT + 30)
        if session.identity != identity:
            raise EditorError(_MISMATCH, "identity_mismatch")
        if session.ended:
            raise ConnectorError(session.end_message or SESSION_ENDED_TEXT)
        return session
    try:
        session.start(options, defer_safe_updates=defer_safe_updates)
    except (ConnectorError, OSError, ValueError):
        registry.remove(tab_id, session)
        raise
    return session


SAFE_UPDATES_OFF = TAG + "SET SESSION sql_safe_updates = 0"
SAFE_UPDATES_ON = TAG + "SET SESSION sql_safe_updates = 1"


def _set_safe_updates(session, statement, audit=None):
    """Send the tab's safe-updates setting (tagged); busy when a run is out."""
    if not session._acquire_run_lock():
        raise EditorError("A statement is running in this tab. Wait for it or press Stop.", "busy")
    try:
        try:
            guard = session._write_session_guard()
            session._guarded_statement(guard, statement)
        except ValueError as exc:
            if audit is not None:
                audit.outcome([statement], "refused", outcome="identity_unavailable", message=str(exc))
            raise EditorError(str(exc), "identity_unavailable") from None
        session._verify_identity(audit, [statement])
        if audit is not None:
            audit.server(session.engine, session.server_version)
            try:
                audit.attempt([statement], target=session.server_identity, verified=["server_identity"])
            except Exception as exc:
                raise EditorError("The audit record could not be written, so nothing was sent: %s" % exc,
                                  "audit_failed")
        try:
            step = session._helper(statement, results=0, guard=guard)
        except EditorError as exc:
            if audit is not None:
                audit.complete("refused", outcome=exc.code, message=str(exc))
            raise
        except ConnectorError as exc:
            if audit is not None:
                audit.complete("unknown", outcome="unknown", message=str(exc))
            raise EditorError("The safe-updates change could not be verified; its outcome is unknown.",
                              "unknown")
        if not step.marker or session.ended:
            if audit is not None:
                audit.complete("unknown", outcome="unknown", message=session.end_message or SESSION_ENDED_TEXT)
            raise EditorError("The safe-updates change could not be verified; its outcome is unknown.", "unknown")
        errors = step.real_errors()
        if errors:
            if audit is not None:
                audit.complete("error", outcome="error", error=errors[0].get("code"),
                               message=errors[0].get("message"))
            from . import dbops
            raise EditorError(dbops.redact(errors[0].get("message") or "The server refused the change.",
                                           session._conn.password if session._conn is not None else None), "error")
        session.safe_updates_wanted = statement == SAFE_UPDATES_ON
        try:
            verified = session.refresh_state(guard=guard)
        except (ConnectorError, EditorError):
            # The setting already succeeded. A later guard refusal cannot
            # turn it into an unsent change or justify retrying the write.
            verified = False
        if not verified or session.ended or session.safe_updates != session.safe_updates_wanted:
            if audit is not None:
                audit.complete("unknown", outcome="unknown", message="Could not verify the safe-updates setting.")
            raise EditorError("The safe-updates setting could not be verified; its outcome is unknown.", "unknown")
        if audit is not None:
            audit.complete("ok", outcome="completed", after={"sql_safe_updates": session.safe_updates})
    except BaseException as exc:
        if audit is not None and audit.pending:
            audit.fail(exc, [statement])
        raise
    finally:
        session.run_lock.release()


def _lower_safe_updates_gate(registry, payload, tab_id, identity, safety, name, options, store, session):
    """(blocked response or None, audit) for turning sql_safe_updates off: a
    session write that lowers protection, so it follows the write policy
    (Production confirms it) and is audited."""
    from . import dbops
    verdict = dbops.classify(SAFE_UPDATES_OFF)
    verdict["statements"] = [{"text": SAFE_UPDATES_OFF, "kind": verdict["kind"], "category": verdict["category"]}]
    decision = dbops.decide(verdict, safety["type"], safety["read_only"], safety.get("profile_name"))
    decision = dict(decision, warnings=[], in_transaction=session.in_transaction if session else False)
    typed = (payload.get("connection") or {}).get("password")
    audit = _audit_trail(store, "editor_safe_updates", safety, options, typed if isinstance(typed, str) else None,
                         decision)
    digest = _digest(tab_id, identity, [SAFE_UPDATES_OFF], "safe_updates")
    blocked = _gate(registry, decision, payload, digest, audit, [SAFE_UPDATES_OFF], [SAFE_UPDATES_OFF], name,
                    options)
    return blocked, audit


def _use_database(session, name):
    """Send ``USE `name``` in the tab's session (tagged helper) and verify the
    change by reading DATABASE() again. dbops classifies USE as a read of
    category "session": no confirmation, no audit; the write policy applies
    to the statements that run in the database afterwards. Backquotes in the
    name are doubled, so the identifier is never split into SQL."""
    if not session._acquire_run_lock():
        raise EditorError("A statement is running in this tab. Wait for it or press Stop.", "busy")
    try:
        if session.ended:
            raise EditorError(session.end_message or SESSION_ENDED_TEXT, "session_ended")
        try:
            guard = session._write_session_guard()
        except ValueError as exc:
            raise EditorError(str(exc), "identity_unavailable") from None
        statement = "USE `%s`" % name.replace("`", "``")
        step = session._helper(TAG + statement, results=0, guard=guard)
        errors = step.real_errors()
        if not step.marker or session.ended:
            raise EditorError(session.end_message or SESSION_ENDED_TEXT, "session_ended")
        if errors:
            from . import dbops
            raise EditorError(dbops.redact(errors[0].get("message") or "The server refused the database change.",
                                           session._conn.password if session._conn is not None else None), "error")
        try:
            verified = session.refresh_state(guard=guard)
        except (ConnectorError, EditorError):
            # USE already succeeded. A refused readback cannot make that
            # change unsent, and the tab must not keep an unverified schema.
            verified = False
        if not verified or session.ended or session.database_name != name:
            message = "The database change could not be verified; its outcome is unknown."
            session.end("use_unverified", message + " " + SESSION_ENDED_TEXT)
            raise EditorError(message, "unknown")
    finally:
        session.run_lock.release()


def _use_api(payload, workspace):
    """Change the default database of the tab's session. With no session yet
    the choice is only kept by the tab and applies when the session opens."""
    registry = _registry(workspace)
    tab_id = _tab_id(payload)
    _, identity, _, _ = _connection_facts(payload, workspace.store)
    name = payload.get("database")
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise ValueError("Choose a database name (1 to 64 characters).")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise ValueError("The database name contains characters a schema name cannot have.")
    session = _session_for(registry, tab_id, identity)
    if session is None:
        return {"status": "none", "database": name, "session": None}
    if session.ended:
        return {"status": "session_ended", "message": session.end_message or SESSION_ENDED_TEXT,
                "session": session.status()}
    _use_database(session, name)
    return {"status": session.status()["state"], "database": session.database_name,
            "session": session.status()}


def _wait_run(run, payload, default):
    wait = _number(payload, "wait", default, 0, 60, "wait")
    if wait:
        run.done.wait(wait)


def _run_response(session, run):
    snapshot = session.snapshot(run)
    return {"status": snapshot["state"], "session": session.status(), "run": snapshot}


def _history_recorder(store, session, password):
    def record(run, snapshot):
        entry = history_entry(run, snapshot, session, password)
        return store.history_add(entry, HISTORY_MAX_ENTRIES, HISTORY_MAX_BYTES)
    return record


def _requested_review(decision, payload):
    """Dedicated Server actions always review, including Development writes.

    This can only tighten policy. Refusals and stronger risky-write reviews
    retain their original reasons and target requirements. A token alone
    does not turn an allowed read into a change requiring consent.
    """
    if payload.get("require_confirmation") is True and decision["action"] == "allow":
        return dict(decision, action="confirm", title="Review server change", tone="danger",
                    text="Review the exact statements and connection before applying this change.")
    return decision


def _run_review_state(plan, decision):
    """Consent requirements, excluding explanations and transaction telemetry.

    The overall verdict can stay unchanged when a lower-risk unit changes.
    Keep each unit's verdict and guards as well as typed targets and ALTER
    preflight. Startup warnings and reasons do not change what was approved;
    a changed action still requires new consent even for identical SQL.
    """
    decision_fields = ("action", "kind", "category", "connection_type", "read_only", "session_guard",
                       "typed_target", "alter_preflight", "alter_binding")
    fields = ("send", "kind", "category", "verb", "single", "session_control", "conditional_sql")
    return ({key: decision.get(key) for key in decision_fields},
            {key: plan["verdict"].get(key) for key in ("kind", "category")},
            [{key: unit.get(key) for key in fields} for unit in plan["units"]])


def _run_api(payload, workspace, fill_password):
    store = workspace.store
    registry = _registry(workspace)
    tab_id = _tab_id(payload)
    options, identity, safety, name = _connection_facts(payload, store)
    limit = int(_number(payload, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT, "limit"))
    timeout = _number(payload, "timeout", 0, 0, 86400, "timeout") or None
    on_error = payload.get("on_error", "stop")
    if on_error not in ("stop", "continue"):
        raise ValueError("on_error must be stop or continue.")
    if _start_preference(payload) is False:
        raise EditorError("Turn safe updates off with editor/open; on a Production connection it asks first.",
                          "invalid")
    session = _session_for(registry, tab_id, identity)
    if session is not None and session.ended:
        return {"status": "session_ended", "message": session.end_message or SESSION_ENDED_TEXT,
                "session": session.status()}
    if session is not None and session.busy():
        raise EditorError("A statement is already running in this tab. Wait for it or press Stop.", "busy")
    try:
        plan = _plan_run(payload, session)
    except EditorError as exc:
        return {"status": "invalid", "code": exc.code, "message": str(exc),
                "session": session.status() if session else None}
    typed_password = (payload.get("connection") or {}).get("password")
    typed_password = typed_password if isinstance(typed_password, str) else None
    # Reusing a session does not authenticate with this request's password.
    # Its actual credential must also be redacted before the policy gate,
    # which can persist a refused or declined attempt without password lookup.
    password = (session._conn.password if session is not None and session._conn is not None
                else typed_password)
    decision = _requested_review(_decision(plan, safety, session), payload)
    filled = None
    # A session this request starts; a final policy refusal ends it again.
    started = {}
    if _alter_unit(plan) is not None and decision["action"] != "refuse" and payload.get("decline") is not True:
        audit = _audit_trail(store, "editor_run", safety, options, password, decision)
        filled = fill_password(payload)
        from .live import connection_options
        full_options = connection_options(filled.get("connection"))
        full_options = _group_options(full_options, payload, store, identity, safety, audit,
                                      [u['send'] for u in plan['units']])
        password = password or full_options.get("password")
        audit.password = password
        changed = _policy_changed(payload, store, identity, safety, audit, [u["send"] for u in plan["units"]])
        if changed:
            return changed
        if session is None:
            session = _open_session(registry, tab_id, full_options, identity, safety, name, payload,
                                    created=started)
        try:
            plan = _plan_run(payload, session)
        except EditorError as exc:
            return {"status": "invalid", "code": exc.code, "message": str(exc), "session": session.status()}
        decision = _requested_review(_decision(plan, safety, session), payload)
        if _alter_unit(plan) is not None and decision["action"] != "refuse":
            review = _alter_review(plan, session)
            decision["alter_preflight"] = review
            decision["alter_binding"] = _alter_binding(session, review)
            decision["warnings"] += review.get("warnings", [])
    statements = [u["send"] for u in plan["units"]]
    writes = [u["send"] for u in plan["units"] if u.get("kind") != "read"]
    digest = _review_digest(tab_id, identity, statements, decision, on_error, session, plan)
    audit = _audit_trail(store, "editor_run", safety, options, password, decision)
    blocked = _gate(registry, decision, payload, digest, audit, statements, writes, name, options)
    if blocked is not None:
        blocked["session"] = session.status() if session else None
        return blocked
    # The policy allowed the run: only now fill the saved password.
    filled = filled if filled is not None else fill_password(payload)
    from .live import connection_options
    full_options = connection_options(filled.get("connection"))
    full_options = _group_options(full_options, payload, store, identity, safety, audit, statements)
    if session is not None and session._conn is not None and full_options.get('_login_guard') is not None:
        session._conn._login_guard = full_options['_login_guard']
    password = password or full_options.get("password")
    audit.password = password
    changed = _policy_changed(payload, store, identity, safety, audit, statements)
    if changed:
        return changed
    if session is None:
        initial_review = _run_review_state(plan, decision)
        try:
            session = _open_session(registry, tab_id, full_options, identity, safety, name, payload,
                                    created=started)
        except ConnectorError as exc:
            message = str(exc)
            if password:
                message = message.replace(password, "[redacted]")
            raise ConnectorError(message)
        if session.busy():
            raise EditorError("A statement is already running in this tab. Wait for it or press Stop.", "busy")
        # Another request may have opened the same identity during password
        # lookup. Redact with its credential, including any renewed policy gate.
        if session._conn is not None:
            password = session._conn.password
            audit.password = password
        # The session's sql_mode is known now: plan again with its reading.
        try:
            plan = _plan_run(payload, session)
        except EditorError as exc:
            return {"status": "invalid", "code": exc.code, "message": str(exc), "session": session.status()}
        decision = _requested_review(_decision(plan, safety, session), payload)
        if _alter_unit(plan) is not None and decision["action"] != "refuse":
            review = _alter_review(plan, session)
            decision["alter_preflight"] = review
            decision["alter_binding"] = _alter_binding(session, review)
            decision["warnings"] += review.get("warnings", [])
        again = [u["send"] for u in plan["units"]]
        audit = _audit_trail(store, "editor_run", safety, options, password, decision)
        if decision["action"] != "allow" and (
                decision["action"] == "refuse" or started.get("session") is not session
                or _run_review_state(plan, decision) != initial_review):
            # Only this request's first session can use the provisional
            # consent already consumed above. A new target, unit verdict or
            # preflight needs its own bound review; an allowed read runs.
            digest = _review_digest(tab_id, identity, again, decision, on_error, session, plan)
            blocked = _gate(registry, decision, dict(payload, confirmed=False), digest, audit, again,
                            [u["send"] for u in plan["units"] if u.get("kind") != "read"], name, options)
            if blocked is not None:
                blocked["session"] = session.status()
                return blocked
    has_write = any(u.get("kind") != "read" for u in plan["units"])
    # History must replay EXPLAIN, never the underlying UPDATE/DELETE.
    history_sql = plan["units"][0]["send"] if payload.get("explain_format") is not None else plan["text"]
    run = Run(plan["units"], limit, full_options, history_sql, plan["offsets"], plan["scope"],
              audit=audit if has_write else None, timeout=timeout,
              history={"record": _history_recorder(store, session, password)},
              alter_context=decision.get("alter_preflight"),
              continue_on_error=on_error == "continue" and plan["scope"] == "all")
    changed = _policy_changed(payload, store, identity, safety, audit, [u["send"] for u in plan["units"]])
    if changed:
        # Nothing was sent. A session this request started (under the old
        # policy) is ended instead of being left open until Close; an
        # existing one is kept, as is one another request holds for a run or
        # helper (run_lock is held for a whole run, so taking it reserves the
        # session: a later start_run sees it ended and refuses).
        if started.get("session") is session and session._acquire_run_lock():
            try:
                registry.remove(tab_id, session)
                session.end("policy_changed", quiet=True)
            finally:
                session.run_lock.release()
            changed["session"] = None
        else:
            changed["session"] = session.status()
        return changed
    session.start_run(run)
    _wait_run(run, payload, 10)
    return _run_response(session, run)


def _action_api(payload, workspace, action):
    """Commit or Rollback the tab's session transaction.

    COMMIT is an explicit ordinary write of work already chosen by the user:
    Development allows and audits it; Production confirms; read-only refuses.
    ROLLBACK remains available to release a transaction under every policy.
    """
    from . import dbops
    store = workspace.store
    registry = _registry(workspace)
    tab_id = _tab_id(payload)
    options, identity, safety, name = _connection_facts(payload, store, allow_group_revoked=action == "ROLLBACK")
    lookup = _owned_session if action == "ROLLBACK" else _session_for
    session = lookup(registry, tab_id, identity)
    if session is None:
        raise EditorError("This tab has no session, so there is nothing to %s." % action.lower(), "no_session")
    if session.ended:
        return {"status": "session_ended", "message": session.end_message, "session": session.status()}
    if session.busy():
        raise EditorError("A statement is running in this tab. Wait for it or press Stop.", "busy")
    send = TAG + action
    verdict = dbops.classify(send)
    verdict["statements"] = [{"text": send, "kind": verdict["kind"], "category": verdict["category"]}]
    decision = dbops.decide(verdict, safety["type"], safety["read_only"], safety.get("profile_name"))
    decision = dict(decision, warnings=[], in_transaction=session.in_transaction)
    if action == "ROLLBACK":
        decision = dict(decision, action="allow")
    if payload.get("decline") is True and verdict["kind"] == "read":
        return {"status": "declined", "message": "Nothing was sent.", "audit_id": None,
                "session": session.status()}
    audit = None
    # Use the authenticated session's credential, including a saved password
    # absent from this request. Action history and server errors need redaction too.
    password = session._conn.password if session._conn is not None else None
    if verdict["kind"] != "read":
        typed = (payload.get("connection") or {}).get("password")
        audit = _audit_trail(store, "editor_" + action.lower(), safety, options,
                             password or (typed if isinstance(typed, str) else None), decision)
        blocked = _gate(registry, decision, payload, _digest(tab_id, identity, [send], action), audit, [send],
                        [send], name, options)
        if blocked is not None:
            blocked["session"] = session.status()
            return blocked
    unit = {"index": 0, "text": action, "start": 0, "end": len(action), "send": send, "lead": 0, "code": action,
            "mode_sensitive": False, "kind": verdict["kind"], "category": verdict["category"],
            "verb": action, "reasons": verdict["reasons"], "idents": []}
    # Identity-checked connection settings and the authenticated credential
    # let end() cancel a blocked action even when close has no password.
    run = Run([unit], DEFAULT_LIMIT, dict(options, password=password), action, Offsets(action), "action",
              kind="action", audit=audit,
              history={"record": _history_recorder(store, session, password)})
    session.start_run(run)
    _wait_run(run, payload, 10)
    return _run_response(session, run)


def _open_api(payload, workspace, fill_password):
    """Open (or return) the tab's session and apply its safe-updates setting.
    Turning safe updates off follows the write policy and is audited."""
    store = workspace.store
    registry = _registry(workspace)
    tab_id = _tab_id(payload)
    options, identity, safety, name = _connection_facts(payload, store)
    session = _session_for(registry, tab_id, identity)
    if session is not None and session.ended:
        registry.remove(tab_id, session)
        session = None
    wanted = _start_preference(payload)
    # An explicit OFF is a write even on Development or when the cached state
    # is already off. In particular, read-only must refuse before opening.
    lower = wanted is False
    if payload.get("decline") is True and not lower:
        # Nothing to decline under the policy now; a decline still opens nothing.
        return {"status": "declined", "message": "Nothing was sent.", "audit_id": None,
                "session": session.status() if session else None}
    audit = None
    if lower:
        blocked, audit = _lower_safe_updates_gate(registry, payload, tab_id, identity, safety, name, options, store,
                                                  session)
        if blocked is not None:
            blocked["session"] = session.status() if session else None
            return blocked
    elif wanted is True:
        from . import dbops
        # Hardening is the existing policy exception: it needs no confirmation,
        # including on read-only connections, but still gets a write audit.
        decision = dbops.decide(dbops.classify(SAFE_UPDATES_ON), safety["type"], safety["read_only"])
        decision = dict(decision, action="allow")
        typed = (payload.get("connection") or {}).get("password")
        audit = _audit_trail(store, "editor_safe_updates", safety, options,
                             typed if isinstance(typed, str) else None, decision)
    filled = fill_password(payload)
    from .live import connection_options
    full = connection_options(filled.get("connection"))
    if audit is not None:
        audit.password = full.get("password") or audit.password
    guarded_statements = [SAFE_UPDATES_OFF if lower else SAFE_UPDATES_ON] if wanted is not None else []
    full = _group_options(full, payload, store, identity, safety, audit,
                          guarded_statements, operation='editor_open')
    if session is not None and session._conn is not None and full.get('_login_guard') is not None:
        session._conn._login_guard = full['_login_guard']
    if lower:
        # Same re-read as a run: a policy changed during the password lookup
        # must not send the statement that lowers a protection.
        changed = _policy_changed(payload, store, identity, safety, audit, [SAFE_UPDATES_OFF])
        if changed:
            changed["session"] = session.status() if session else None
            return changed
    started = {}
    try:
        # Apply explicit preferences only below, through the audited helper.
        session = _open_session(registry, tab_id, full, identity, safety, name, payload,
                                defer_safe_updates=wanted is not None, created=started)
    except ConnectorError as exc:
        message = str(exc)
        if full.get("password"):
            message = message.replace(full["password"], "[redacted]")
        raise ConnectorError(message)
    if lower:
        # Again right before sending: connecting takes time, and a policy
        # changed meanwhile must not see the protection lowered. A session
        # this request started (safe updates deferred, under the old policy)
        # is ended instead of being left open; nothing was sent to it.
        changed = _policy_changed(payload, store, identity, safety, audit, [SAFE_UPDATES_OFF])
        if changed:
            if started.get("session") is session:
                registry.remove(tab_id, session)
                session.end("policy_changed", quiet=True)
                changed["session"] = None
            else:
                changed["session"] = session.status()
            return changed
    try:
        if lower:
            _set_safe_updates(session, SAFE_UPDATES_OFF, audit)
        elif wanted is True:
            _set_safe_updates(session, SAFE_UPDATES_ON, audit)
    except BaseException:
        if started.get("session") is session:
            # Startup deliberately deferred this preference. A refusal or an
            # unverifiable change must not leave an unprotected new tab open.
            registry.remove(tab_id, session)
            session.end("safe_updates_failed", quiet=True)
        raise
    return {"status": session.status()["state"], "session": session.status()}


# ---- SQL files (Open SQL file, Save, Save as) ---------------------------------
#
# A tab opened from a file remembers it as an opaque token; the absolute path
# and its canonical parent's device/inode stay in this map for this backend's
# lifetime. The renderer never supplies a write path or a parent identity.
# The map is bounded; the oldest binding is dropped.

_FILE_TOKEN_MAX = 200
_FILE_TOKENS = {}
_FILE_TOKENS_LOCK = threading.Lock()


def _file_token_remember(path, parent_identity=None):
    from .file_picker import file_parent_identity
    if parent_identity is None:
        path = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        parent_identity = file_parent_identity(path)
    with _FILE_TOKENS_LOCK:
        while len(_FILE_TOKENS) >= _FILE_TOKEN_MAX:
            del _FILE_TOKENS[next(iter(_FILE_TOKENS))]
        token = secrets.token_hex(16)
        _FILE_TOKENS[token] = (path, parent_identity)
        return token


def _file_token_binding(token):
    if not isinstance(token, str) or not token or len(token) > 100:
        raise ValueError("Pass the file token of a file this workspace opened or saved.")
    with _FILE_TOKENS_LOCK:
        binding = _FILE_TOKENS.get(token)
    if binding is None:
        raise EditorError("This tab's file binding is gone (the workspace server restarted). "
                          "Use Save as to choose the file again.", "unknown_token")
    return binding


def _file_open_api(payload):
    """Choose a .sql/.txt file with the native dialog and read it (at most
    5 MiB, UTF-8; a BOM is stripped and invalid bytes become U+FFFD with a
    one-line warning). Local file only: no connection, no server."""
    from .file_picker import SQL_MAX_BYTES, choose_sql_file, file_parent_identity, open_for_read
    chosen = choose_sql_file()
    if chosen.get("cancelled"):
        return {"cancelled": True}
    # The chooser already resolved and checked the path (file_picker.check_file).
    # Resolving it again would follow a link swapped in since; instead the
    # file and its folder are opened without following one, nor waiting on a FIFO.
    path = chosen["path"]
    parent_identity = file_parent_identity(path)
    fd = open_for_read(path, parent_identity)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Choose a regular SQL file.")
        if info.st_size > SQL_MAX_BYTES:
            raise ValueError("The file is larger than 5 MiB. Open it in smaller parts.")
        raw = handle.read(SQL_MAX_BYTES + 1)
    if len(raw) > SQL_MAX_BYTES:
        raise ValueError("The file is larger than 5 MiB. Open it in smaller parts.")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    warning = None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
        warning = ("Some bytes were not valid UTF-8 and were replaced with the replacement "
                   "character (U+FFFD); saving writes the replaced text.")
    return {"cancelled": False, "name": os.path.basename(path), "sql": text,
            "token": _file_token_remember(path, parent_identity), "warning": warning}


def _file_save_api(payload):
    """Write the tab's SQL to its bound file (the token's path), or ask where
    with the native save dialog (Save as, or a tab without a binding). The
    write is atomic (temp file, fsync, rename), UTF-8 without a BOM."""
    from .file_picker import SQL_EXTENSIONS, check_save_destination, choose_save_file, write_text_atomic
    sql = payload.get("sql")
    if not isinstance(sql, str):
        raise ValueError("Pass the SQL text to save.")
    if len(sql) > 8 * 1000 * 1000:
        raise ValueError("The SQL text is too large to save (8,000,000 characters at most).")
    token = payload.get("token")
    overwrite = True
    if payload.get("save_as") is True or not token:
        chosen = choose_save_file(payload.get("name") or "query.sql")
        if chosen.get("cancelled"):
            return {"cancelled": True}
        path, parent_identity, overwrite = check_save_destination(chosen["path"])
    else:
        path, parent_identity = _file_token_binding(token)
        # Do not resolve a token's parent again and silently bind a different
        # directory. The writer compares its identity at the write boundary.
        if os.path.splitext(path)[1].lower() not in SQL_EXTENSIONS:
            raise ValueError("Save as a .sql or .txt file.")
    write_text_atomic(path, sql, parent_identity=parent_identity, overwrite=overwrite)
    return {"cancelled": False, "token": _file_token_remember(path, parent_identity),
            "name": os.path.basename(path), "path": path}


def api_response(path, payload, workspace, fill_password=None):
    """Answer one /api/editor/* request. *fill_password(payload)* returns the
    payload with the saved password filled in (ui._with_saved_password); it is
    called only after the write policy allowed the request."""
    if path not in PATHS:
        raise ValueError("Unknown workspace operation.")
    if fill_password is None:
        def fill_password(value):
            return value
    if path == "/api/editor/export":
        return export_result(payload.get("format"), payload.get("result"), payload.get("table"),
                             payload.get("schema"), payload.get("engine") or "mysql", payload.get("options"))
    if path == "/api/editor/file/open":
        # Local files, no server: they need no connection or workspace store.
        return _file_open_api(payload)
    if path == "/api/editor/file/save":
        return _file_save_api(payload)
    if path == "/api/editor/preview":
        return _preview_api(payload, workspace)
    store = workspace.store
    if path == "/api/editor/history":
        action = payload.get("action", "list")
        if action == "clear":
            return {"cleared": store.history_clear()}
        if action != "list":
            raise ValueError("history action must be list or clear.")
        limit = int(_number(payload, "limit", 200, 1, HISTORY_MAX_ENTRIES, "limit"))
        profile_id = payload.get("profile_id")
        if profile_id is not None and (not isinstance(profile_id, str) or len(profile_id) > 200):
            raise ValueError("Invalid profile_id.")
        return {"items": store.history_list(limit, profile_id or None), "max_entries": HISTORY_MAX_ENTRIES,
                "max_bytes": HISTORY_MAX_BYTES}
    if path == "/api/editor/saved/list":
        limit = int(_number(payload, "limit", SAVED_QUERY_MAX_ENTRIES, 1, SAVED_QUERY_MAX_ENTRIES, "limit"))
        return {"items": store.saved_query_list(limit), "max_entries": SAVED_QUERY_MAX_ENTRIES,
                "max_bytes": SAVED_QUERY_MAX_BYTES, "max_sql": SAVED_QUERY_SQL_CAP}
    if path == "/api/editor/saved/save":
        sql = payload.get("sql")
        entry = saved_query_entry(payload.get("name"), payload.get("folder"), sql,
                                  payload.get("note"), payload.get("database"))
        entry["id"] = store.saved_query_save(entry, SAVED_QUERY_MAX_ENTRIES, SAVED_QUERY_MAX_BYTES)
        # The SQL changed only where a credential was replaced.
        return {"saved": entry, "redacted": entry["sql"] != sql}
    if path == "/api/editor/saved/rename":
        return {"saved": store.saved_query_rename(payload.get("id"), saved_query_name(payload.get("name")))}
    if path == "/api/editor/saved/move":
        return {"saved": store.saved_query_move(payload.get("id"), saved_query_folder(payload.get("folder")))}
    if path == "/api/editor/saved/delete":
        if store.saved_query_delete(payload.get("id")) == 0:
            raise ValueError("That saved query is no longer kept on this computer.")
        return {"deleted": 1}
    registry = _registry(workspace)
    if path == "/api/editor/use":
        return _use_api(payload, workspace)
    if path == "/api/editor/run":
        return _run_api(payload, workspace, fill_password)
    if path == "/api/editor/commit":
        return _action_api(payload, workspace, "COMMIT")
    if path == "/api/editor/rollback":
        return _action_api(payload, workspace, "ROLLBACK")
    if path == "/api/editor/open":
        return _open_api(payload, workspace, fill_password)
    tab_id = _tab_id(payload)
    if path in ("/api/editor/status", "/api/editor/close"):
        # Unknown tabs and tabs owned by another connection have the same
        # answer. Even ended sessions retain results belonging to their owner.
        # A member that left its group keeps Status and Close: both only read
        # or end the tab's existing session, so its locks are never stranded.
        session = None
        if isinstance(payload.get("connection"), dict):
            options, identity, safety, name = _connection_facts(payload, store, allow_group_revoked=True)
            try:
                session = _owned_session(registry, tab_id, identity)
            except EditorError as exc:
                if exc.code != "identity_mismatch":
                    raise
    if path == "/api/editor/status":
        if session is None:
            return {"status": "none", "session": None, "run": None}
        if payload.get("refresh_insights") is True:
            session.refresh_transaction_meter()
        run = session.run
        run_id = payload.get("run_id")
        if run is not None and (run_id is None or run_id == run.id):
            _wait_run(run, payload, 0)
        return {"status": session.status()["state"], "session": session.status(), "run": session.snapshot()}
    if path == "/api/editor/close":
        if session is None:
            return {"closed": True, "session": None}
        if not session.ended and session.in_transaction is not False and payload.get("discard") is not True:
            known = session.in_transaction is True
            return {"closed": False, "reason": "transaction_open" if known else "transaction_unknown",
                    "message": ("A transaction is open." if known else
                                "A transaction may be open (myflames cannot tell on this server).")
                               + " Commit or roll it back, or close with discard to roll it back.",
                    "session": session.status()}
        session.end("closed", SESSION_ENDED_TEXT)
        registry.remove(tab_id, session)
        return {"closed": True, "session": session.status()}
    options, identity, safety, name = _connection_facts(payload, store,
                                                        allow_group_revoked=path == "/api/editor/stop")
    if path == "/api/editor/stop":
        session = _owned_session(registry, tab_id, identity)
        if session is None:
            return {"stopped": False, "message": "This tab has no session.", "session": None}
        try:
            filled = fill_password(payload)
            from .live import connection_options
            full = connection_options(filled.get("connection"))
        except ValueError:
            # Membership can be gone by now; the KILL (or force-end) is
            # cleanup of the tab's existing session, so it uses that
            # session's own authenticated run options instead.
            run = session.run
            full = dict(run.options) if run is not None and run.options is not None else None
        result = session.stop(full, force=payload.get("force") is True)
        if full and full.get("password") and result.get("message"):
            result["message"] = result["message"].replace(full["password"], "[redacted]")
        result["session"] = session.status()
        return result
    raise ValueError("Unknown workspace operation.")
