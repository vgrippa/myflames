"""Query editor sessions: run any SQL in one long-lived client session per tab.

The workspace's Query editor runs SQL the way Workbench or DBeaver do: every
tab owns one server session (query-editor skill). myflames has no MySQL
driver (stdlib only), so a session is one ``mysql`` or ``mariadb`` client
process with stdin kept open, started on the tab's first run and ended on
close, connection switch, idle timeout or workspace exit. It is never
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
so that line marks the end of the statement without any extra server
statement: ROW_COUNT(), FOUND_ROWS() and the warnings of one statement stay
intact for the next statement of the same run. Server errors carry the
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
transaction. A KILL is never sent while a helper statement runs, and the
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
actions. User statements are sent untagged.

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
import subprocess
import tempfile
import threading
import time
import uuid
import weakref

from .connector import ConnectorError, MySQLConnection
from .server_info import identity_from_version, product_identity

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
#: when the server supports it; myflames' splitter does not.
_DOLLAR_QUOTE = re.compile(r"(?<![A-Za-z0-9_$\u0080-\U0010ffff])\$[A-Za-z0-9_]*\$")
_READ_WRITE = re.compile(r"\bREAD\s+WRITE\b|\b(?:TRANSACTION_READ_ONLY|TX_READ_ONLY)\b\s*(?::=|=)", re.I)
_MODE_CHANGE = re.compile(r"\bSQL_MODE\b|(?:^|;)\s*(?:CALL|EXECUTE|SOURCE)\b", re.I)


class EditorError(ValueError):
    """A request the editor refuses; ``code`` names the reason for the UI."""

    def __init__(self, message, code="invalid"):
        super().__init__(message)
        self.code = code


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

def plan_statements(sql, backslash_escapes=True, engine=None, base=0, delimiter=";"):
    """The statements of *sql* as the client will run them, or raise EditorError.

    Returns a list of dicts: ``index``, ``text`` (as written), ``start`` and
    ``end`` (Python offsets in the editor text, *base* added), ``send`` (the
    text written to the client: leading line comments dropped, trailing
    white space removed), ``lead`` (offset of ``send`` in ``text``),
    ``mode_sensitive`` (strings end at different places with and without
    NO_BACKSLASH_ESCAPES) and ``code``.
    """
    from . import dbops
    from .live import TOKEN, TOKEN_NO_BACKSLASH
    units = []
    for stmt in dbops.split_statements(sql, backslash_escapes, delimiter):
        number = len(units) + 1
        if stmt["unclosed"] or stmt.get("issues"):
            reasons = list(stmt.get("issues") or [])
            if not reasons:
                reasons = ["Close every quoted string, quoted identifier and comment."]
            raise EditorError("Statement %d cannot run: %s" % (number, " ".join(reasons)), "invalid_statement")
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
        # The clients recognize the first nine characters, including suffixes
        # such as delimiterfoo. None may reach our private framing delimiter.
        if re.match(r"\s*delimiter", code, re.I):
            raise EditorError("Statement %d starts with the word DELIMITER, which the client reads as its own "
                              "command. Put DELIMITER on its own line." % number, "invalid_statement")
        if _DOLLAR_QUOTE.search(code):
            raise EditorError("Statement %d contains dollar-quoted text ($...$), which the Query editor does not "
                              "run yet." % number, "invalid_statement")
        spans = [m.span() for m in TOKEN.finditer(send)]
        mode_sensitive = spans != [m.span() for m in TOKEN_NO_BACKSLASH.finditer(send)]
        unit = {"index": len(units), "text": stmt["text"], "start": base + stmt["start"],
                "end": base + stmt["end"], "send": send, "lead": lead, "code": code,
                "mode_sensitive": mode_sensitive, "delimiter": stmt.get("delimiter"),
                "idents": list(stmt.get("idents") or [])}
        units.append(unit)
    if not units:
        raise EditorError("There is no statement to run.", "empty")
    changed = None
    for unit in units:
        # Include changes within this client statement: a custom DELIMITER can
        # group SET/CALL and later SQL into one unit. Conservatively refuse an
        # ambiguous unit rather than guess the mode halfway through its text.
        # Restore quoted identifiers so `sql_mode` cannot evade this guard.
        changes_mode = _MODE_CHANGE.search(restore_idents(unit["code"], unit.get("idents") or []))
        if changed is None and changes_mode and ";" in unit["code"]:
            changed = unit["index"]
        if unit["mode_sensitive"] and changed is not None:
            raise EditorError("Statement %d may change sql_mode and statement %d has a backslash before a quote, "
                              "which reads differently under NO_BACKSLASH_ESCAPES. Run them separately."
                              % (changed + 1, unit["index"] + 1), "invalid_statement")
        if changed is None and changes_mode:
            changed = unit["index"]
    return units


def classify_units(units, backslash_escapes, sql):
    """A dbops verdict for the run: the worst statement, with ``statements``
    being exactly what will be sent (for the confirmation)."""
    from . import dbops
    statements = []
    for unit in units:
        verdict = dbops.classify(unit["send"], backslash_escapes)
        unit["kind"], unit["category"], unit["reasons"] = verdict["kind"], verdict["category"], verdict["reasons"]
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
    offset = start + (found or 0)
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
                 history=None, alter_context=None):
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
        self.snapshot_cache = None
        self.audit_id = None
        self.history_id = None


class EditorSession(object):
    """One client process (one server session) for one editor tab."""

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
        self.proc = None
        self.delimiter = None
        self.lines = 0
        self.state = "starting"
        self.ended = False
        self.ended_reason = None
        self.end_message = None
        self.engine = None
        self.server_version = None
        self.server_product = None
        self.connection_id = None
        self.server_identity = None
        self.autocommit = None
        self.in_transaction = None
        self.transaction_source = None
        self.transaction_note = None
        self.sql_mode = None
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
        self.ready = threading.Event()

    # -- starting -------------------------------------------------------------------------

    def start(self, options, defer_safe_updates=False):
        """Start the client and read the session's identity. Raises
        ConnectorError (password redacted by the caller) when it cannot."""
        conn = MySQLConnection(**options)
        self._conn = conn
        credentials = contextlib.ExitStack()
        try:
            defaults = credentials.enter_context(conn._invocation_defaults())
            binary = conn._resolve_binary()
            self._client_home = tempfile.TemporaryDirectory(prefix="myflames-editor-")
            env = _clean_env(self._client_home.name)
            from .connector import CLIENT_CHARSET_FLAGS
            # Exclude system option files as well as the private HOME's files.
            argv = [binary, "--defaults-file=" + defaults, "--xml", "--binary-as-hex", "--binary-mode",
                    "--comments", "--force", "--quick", "--unbuffered", "-vv", "--skip-reconnect"]
            argv.extend(CLIENT_CHARSET_FLAGS)
            if _client_features(binary, env).get("skip_commands"):
                argv.append("--skip-commands")
            if options.get("database"):
                argv += ["-D", options["database"]]
            self.delimiter = "__mf%s_" % secrets.token_hex(5)
            try:
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
                raise ConnectorError(self._helper_problem(hello) or "The MySQL client did not start a session.")
            self.connection_id = _int(row.get("connection_id"))
            self.server_version = row.get("version") or ""
            # @@version_comment decides, as everywhere else: a MariaDB server whose
            # @@version was changed at startup still names itself there.
            comment = row.get("version_comment")
            identity = (product_identity(self.server_version, comment) if comment
                        else identity_from_version(self.server_version))
            self.engine = identity["engine"]
            self.server_product = identity["product"]
            self.server_identity = self._identity_row(row)
            if self.server_identity is None:
                raise ConnectorError("The server identity could not be read; no editor statements were sent.")
            self._apply_state(row)
            # The defaults file holds the password: the client has read it.
            credentials.close()
            setup = []
            if self.read_only:
                from . import dbops
                setup.extend(getattr(dbops, "READ_ONLY_SESSION_GUARD", ("SET SESSION TRANSACTION READ ONLY",)))
            if not defer_safe_updates and self.safe_updates_wanted != bool(self.safe_updates):
                setup.append("SET SESSION sql_safe_updates = %d" % (1 if self.safe_updates_wanted else 0))
            for statement in setup:
                step = self._helper(TAG + statement, results=0)
                if not step.marker or self.ended:
                    raise ConnectorError("Could not verify session setup. " + SESSION_ENDED_TEXT)
                if step.real_errors():
                    raise ConnectorError("Could not prepare the session (%s): %s"
                                         % (statement, step.real_errors()[0].get("message")))
            self.refresh_state()
            with self.lock:
                if self.ended:
                    raise ConnectorError(self.end_message or SESSION_ENDED_TEXT)
                self.state = "idle"
                self.last_used = time.monotonic()
        except BaseException:
            conn.close()
            self.end("start_failed", quiet=True)
            raise
        finally:
            try:
                credentials.close()
            finally:
                self.ready.set()

    def _helper_problem(self, step):
        errors = step.real_errors()
        if errors:
            e = errors[0]
            return "%s%s" % ("ERROR %s: " % e["code"] if e.get("code") else "", e.get("message") or "")
        with self.lock:
            notes = [n for n in self.notes if n]
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

    def _send(self, kind, send, unit=None, limit=None, results=None, kill_ok=False):
        """Write one statement; returns its Step. Holds send_lock so a KILL in
        flight is never followed by a new statement."""
        with self.send_lock:
            with self.lock:
                if self.ended:
                    raise ConnectorError(self.end_message or SESSION_ENDED_TEXT)
            if self.delimiter in send:
                self.delimiter = "__mf%s_" % secrets.token_hex(5)
                self._write("DELIMITER %s\n" % self.delimiter)
            first = self.lines + 1
            marker = first + send.count("\n") + 2
            step = Step(kind, send, first, marker, unit=unit, limit=limit, results=results)
            step.kill_ok = bool(kill_ok)
            with self.lock:
                self._osteps.append(step)
                self._esteps.append(step)
                step.sent_at = time.monotonic()
                if self.run is not None and not self.run.done.is_set():
                    self.run.steps.append(step)
                    self.run.current = step
            self._write(send + "\n" + self.delimiter + "\ndelimiter\n")
            return step

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

    def _helper(self, send, results=1, timeout=HELPER_TIMEOUT):
        step = self._send("helper", send, results=results)
        ok = self._wait(lambda: step.marker and (step.closed or bool(step.real_errors()) or results == 0),
                        timeout)
        if not ok and not self.ended:
            self.end("protocol", "The MySQL client stopped answering. " + SESSION_ENDED_TEXT)
        return step

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
        if not self.ended:
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

    def _kill_query(self, options, step):
        """KILL QUERY this session's statement from a second connection, only
        while *step* still runs. Returns ("sent" | "skipped" | "failed", text)."""
        with self.send_lock:
            with self.lock:
                if step.marker or getattr(step, "unfinished", False) or self.ended or not self.connection_id:
                    return "skipped", None
                connection_id = self.connection_id
            try:
                kill_options = dict(options)
                kill_options["query_timeout"] = KILL_TIMEOUT
                with MySQLConnection(**kill_options) as control:
                    control.run(TAG + "KILL QUERY %d" % connection_id, extra_flags=list(_write_flags()),
                                timeout=KILL_TIMEOUT)
                with self.lock:
                    step.kill_sent_at = time.monotonic()
                return "sent", None
            except ConnectorError as exc:
                text = str(exc)
                password = options.get("password")
                if password:
                    text = text.replace(password, "[redacted]")
                with self.lock:
                    self._note("KILL QUERY failed: " + text)
                return "failed", text

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

    @property
    def backslash_escapes(self):
        return "NO_BACKSLASH_ESCAPES" not in (self.sql_mode or "").upper()

    @staticmethod
    def _identity_row(row):
        keys = ("connection_id", "version", "hostname", "port", "server_id")
        if row is None or any(row.get(key) in (None, "") for key in keys):
            return None
        return {key: row[key] for key in keys}

    def _probe(self):
        """Re-read the actual session, never return the startup identity cache.

        Use variables available on both engines (MariaDB has no server_uuid).
        Call only while holding run_lock, before a run, never between its units.
        """
        step = self._helper(TAG + "SELECT CONNECTION_ID() AS connection_id, VERSION() AS version, "
                                  "@@hostname AS hostname, @@port AS port, @@server_id AS server_id")
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

    def refresh_state(self):
        """Read autocommit, the transaction state and sql_mode (tagged helpers)."""
        if self.engine == "mysql":
            probe = self._helper(TAG + "SELECT (SELECT ENABLED FROM performance_schema.setup_consumers WHERE "
                                       "NAME = 'events_transactions_current') AS consumer, (SELECT ENABLED FROM "
                                       "performance_schema.setup_instruments WHERE NAME = 'transaction') AS "
                                       "instrument, (SELECT INSTRUMENTED FROM performance_schema.threads WHERE "
                                       "THREAD_ID = PS_CURRENT_THREAD_ID()) AS instrumented, (SELECT STATE FROM "
                                       "performance_schema.events_transactions_current WHERE THREAD_ID = "
                                       "PS_CURRENT_THREAD_ID()) AS state")
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
                                  "autocommit, @@sql_mode AS sql_mode, @@sql_safe_updates AS sql_safe_updates%s"
                            % (token, extra))
        row = self._helper_row(step)
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
            reconnected = self.connection_id is not None and _int(row.get("connection_id")) != self.connection_id
            if reconnected:
                self._mark_ended("reconnected", "The client reconnected to the server. " + SESSION_ENDED_TEXT)
            else:
                self._apply_state(row)
        if reconnected:
            self._terminate()
            return False
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

    def busy(self):
        with self.lock:
            return self.run is not None and not self.run.done.is_set()

    def start_run(self, run):
        if not self.run_lock.acquire(False):
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
            if run.audit is not None:
                statements = [u["send"] for u in run.units]
                self._verify_identity(run.audit, statements)
                if run.alter_context is not None:
                    unit = _alter_unit({"units": run.units})
                    # run_lock is held: a concurrent USE cannot slip between
                    # checking the confirmed default schema and sending ALTER.
                    if unit is not None and _ALTER_TABLE.match(unit["code"]).group(2) is None:
                        step = self._helper(TAG + "SELECT DATABASE() AS database_name")
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

    def _execute(self, run):
        timer = None
        try:
            if run.timeout:
                timer = threading.Timer(run.timeout, self.stop, kwargs={"options": run.options, "by": "timeout"})
                timer.daemon = True
                timer.start()
            for unit in run.units:
                unit["status"] = "pending"
            for unit in run.units:
                with self.lock:
                    if run.stop_requested or self.ended:
                        break
                    if _changes_privilege_context(unit):
                        self.privileges_changed = True
                unit["status"] = "running"
                kill_ok = run.kind == "run" and unit.get("kind") == "read" and unit.get("single", True)
                step = self._send(run.kind == "run" and "user" or "action", unit["send"], unit=unit["index"],
                                  limit=run.limit, kill_ok=kill_ok)
                self._wait_step(step)
                for thread in list(run.kill_threads):
                    thread.join(KILL_TIMEOUT + 5)
                errors = step.real_errors()
                interrupted = bool(errors) and all(e.get("code") in INTERRUPTED_CODES for e in errors)
                unit["duration_ms"] = step.duration_ms
                if not step.marker:
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
                if self.ended or unit["status"] in ("error", "stopped", "session_ended"):
                    break
            for unit in run.units:
                if unit.get("status") == "pending":
                    unit["status"] = "not_run"
            if not self.ended:
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
            run.finished_at = _now()
            run.duration = time.monotonic() - run.started
            if self.ended:
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
                if any(u.get("status") == "session_ended" for u in run.units):
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
            if unit["lead"]:
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
                        "position": error_position(error.get("message"), unit, offsets) if run.kind == "run"
                        else None}
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
                with MySQLConnection(**kill) as control:
                    control.run(TAG + "KILL QUERY %d" % self.connection_id, extra_flags=list(_write_flags()),
                                timeout=KILL_TIMEOUT)
            except ConnectorError:
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
        self._closed = True
        with self.lock:
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
_NUMBER = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")
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
    if numeric and _NUMBER.match(value):
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


# ---- the HTTP API -----------------------------------------------------------------------

#: Every editor endpoint (ui.py adds them to its allowlist).
PATHS = ("/api/editor/open", "/api/editor/run", "/api/editor/status", "/api/editor/stop",
         "/api/editor/commit", "/api/editor/rollback", "/api/editor/close", "/api/editor/history",
         "/api/editor/export", "/api/editor/preview")
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


def _connection_facts(payload, store):
    """(options without password, identity key, safety, display name) for the request's connection."""
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
    safety = dbops.connection_safety(profile, connection)
    keyed = dict(options)
    keyed["profile_id"] = profile_id or None
    identity = dbops.connection_identity(keyed, safety)
    name = safety.get("profile_name") or "%s:%s" % (options.get("host"), options.get("port"))
    return options, identity, safety, name


_MISMATCH = "This tab's session belongs to another connection. Close it from that connection before switching."


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


def delimiter_at(sql, offset, backslash_escapes=True):
    """The delimiter in effect at *offset* (a DELIMITER line above changes it)."""
    probe = dbops_split(sql[:offset] + "\nx", backslash_escapes)
    return probe[-1]["delimiter"] if probe else ";"


def dbops_split(sql, backslash_escapes=True, delimiter=";"):
    from . import dbops
    return dbops.split_statements(sql, backslash_escapes, delimiter)


def _offset(payload, key, offsets, total):
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= total:
        raise ValueError("%s must be an integer offset inside the text." % key)
    return offsets.to_py(value)


def _selection(payload, sql, offsets, backslash=True):
    """(start, end, scope, delimiter) of what runs: the whole text; the
    selection, split with the delimiter in effect where it starts; or, with
    ``cursor``, the one statement the caret chooses, found here with the same
    rules (and the session's backslash reading) the server will split with."""
    selection = payload.get("selection")
    total = offsets.to_js(len(sql))
    if selection is None and payload.get("cursor") is not None:
        cursor = _offset(payload, "cursor", offsets, total)
        stmt = statement_at(sql, dbops_split(sql, backslash), cursor)
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
    return start, end, "selection", delimiter_at(sql, start, backslash)


# db-operations sections 6 and 9: exact destructive target, one DDL per
# confirmation, and a reviewed, bounded metadata-lock wait in this session.
# The skill specifies N, not a numeric default; five seconds is our UI policy.
ALTER_LOCK_WAIT_SECONDS = 5
ALTER_LOCK_WAIT = TAG + "SET SESSION lock_wait_timeout = %d" % ALTER_LOCK_WAIT_SECONDS
_SQL_NAME = r"(?:`\d+`|[\w$]+)"
_DROP_DATABASE = re.compile(r"^\s*DROP\s+(?:DATABASE|SCHEMA)\s+(?:IF\s+EXISTS\s+)?(" + _SQL_NAME + r")\s*$", re.I)
_ALTER_TABLE = re.compile(r"^\s*ALTER\s+(?:(?:ONLINE|OFFLINE|IGNORE)\s+)*TABLE\s+(?:IF\s+EXISTS\s+)?(" +
                          _SQL_NAME + r")(?:\s*\.\s*(" + _SQL_NAME + r"))?(?=\s|$)", re.I)
_DDL_START = re.compile(r"^\s*(?:CREATE|ALTER|DROP|RENAME|TRUNCATE)\b", re.I)
_ROUTINE_CREATE = re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*[^;]+?\s+)?(?:PROCEDURE|FUNCTION|TRIGGER|EVENT)\b", re.I | re.S)


def _ddl_codes(unit):
    """Inspect masked client text for DDL batching, without changing splitting.

    BEGIN/CASE bodies of routine definitions contain semicolons of their own.
    Recognize those blocks so a trailing statement after END cannot inherit the
    routine's confirmation. Ambiguous/unbalanced blocks fall back to refusing
    a batch; execution still uses the original shared statement splitter.
    """
    code = unit["code"]
    if not _ROUTINE_CREATE.match(code):
        return [part for part in code.split(";") if part.strip()]
    tokens = list(re.finditer(r"\b(?:BEGIN|CASE|END|IF|LOOP|WHILE|REPEAT)\b|;", code, re.I))
    blocks, parts, start, index = [], [], 0, 0
    while index < len(tokens):
        token = tokens[index]
        word = token.group(0).upper()
        if word in ("BEGIN", "CASE"):
            blocks.append(word)
        elif word == "END":
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            qualifier = (following.group(0).upper() if following and
                         not code[token.end():following.start()].strip() else None)
            if qualifier in ("IF", "LOOP", "WHILE", "REPEAT", "CASE"):
                index += 1
            if qualifier not in ("IF", "LOOP", "WHILE", "REPEAT"):
                if not blocks:
                    return [part for part in code.split(";") if part.strip()]
                blocks.pop()
        elif word == ";" and not blocks:
            parts.append(code[start:token.start()])
            start = token.end()
        index += 1
    if blocks:
        return [part for part in code.split(";") if part.strip()]
    parts.append(code[start:])
    return [part for part in parts if part.strip()]


def _ddl_parts(units):
    # Masked strings and comments contain no semicolons. A routine body is
    # part of its CREATE, not another immediately executed DDL statement.
    for unit in units:
        if unit.get("generated"):
            continue
        codes = _ddl_codes(unit)
        for code in codes:
            if _DDL_START.match(code) and not re.match(r"\s*DROP\s+PREPARE\b", code, re.I):
                yield unit, code


def _alter_unit(plan):
    return next((u for u in plan["units"] if _ALTER_TABLE.match(u["code"])), None)


def _alter_binding(session, review):
    # DATABASE() can change between requests on the same tab. Bind unqualified
    # ALTERs to that schema as well as the live session and server identity.
    return [session.connection_id, session.server_identity, review.get("schema"), review.get("table")]


def _review_digest(tab_id, identity, statements, decision):
    return _digest(tab_id, identity, statements, ["run", decision.get("alter_binding")])


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

    if not session.run_lock.acquire(False):
        raise EditorError("A statement is already running in this tab. Wait for it or press Stop.", "busy")
    try:
        reader = Reader()
        facts = reader.query_lock_rows(TAG + "SELECT DATABASE(), @@performance_schema")
        schema = names[0] if len(names) == 2 else (facts[0][0] if facts and facts[0] else None)
        ps_on = bool(facts and len(facts[0]) > 1 and str(facts[0][1]).upper() in ("1", "ON"))
        review = locks.alter_table_preflight(reader, schema, names[-1], session.engine,
                                             session.server_version, ps_on)
        review["suggestion"] = _alter_suggestion(unit, review, session.engine, session.server_version)
        if review["suggestion"]:
            review["warnings"].append("Optional rewrite: INPLACE and LOCK=NONE request an online operation; "
                "the server checks support. INPLACE can still rebuild the table. Copy the SQL into the editor "
                "and review it again to use it.")
        else:
            review["warnings"].append("No rewrite offered: this form, engine, version or existing ALGORITHM/LOCK "
                                       "clause is not covered by the verified suggestion.")
        review["warnings"].append("A COPY or rebuild can need extra disk space and delay replicas. "
                                   "Check Server > Locks while ALTER runs. The lock wait limit is per wait, "
                                   "not a limit on the ALTER's total duration.")
        return review
    finally:
        session.run_lock.release()


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
            or re.search(r"\b(?:ALGORITHM|LOCK|PARTITION|WAIT|NOWAIT|ONLINE|OFFLINE|IGNORE)\b", code, re.I)
            or not re.match(r"\s*(?:ADD\s+(?:COLUMN|(?:UNIQUE\s+)?(?:INDEX|KEY))|DROP\s+(?:COLUMN|INDEX|KEY))\b",
                            code[match.end():], re.I)
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
    backslash = session.backslash_escapes if session is not None and session.sql_mode is not None else True
    start, end, scope, delimiter = _selection(payload, sql, offsets, backslash)
    text = sql[start:end]
    engine = session.engine if session is not None else None
    units = plan_statements(text, backslash, engine, base=start, delimiter=delimiter)
    # The guard is its own visible, audited statement. Failure stops the run
    # before ALTER, using the same error handling as every user statement.
    guarded = []
    for unit in units:
        if _ALTER_TABLE.match(unit["code"]):
            guard = plan_statements(ALTER_LOCK_WAIT, base=unit["start"])[0]
            guard.update(generated=True, start=unit["start"], end=unit["start"])
            guarded.append(guard)
        guarded.append(unit)
    units = guarded
    for index, unit in enumerate(units):
        unit["index"] = index
    verdict = classify_units(units, None if session is None or session.sql_mode is None else backslash, text)
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
    return unit.get("category") == "account" or bool(re.search(r"\bSET\s+ROLE\b|\b(?:CREATE|DROP)\s+TEMPORARY\s+TABLE\b"
                          r"|^\s*(?:CALL|EXECUTE)\b", unit["code"], re.I))


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
    r"^\s*(?:(?:CREATE|DROP)\b(?!\s+(?:OR\s+REPLACE\s+)?TEMPORARY\b)|(?:ALTER|RENAME|TRUNCATE)\b"
    r"|(?:GRANT|REVOKE)\b|SET\s+PASSWORD\b|SET\s+DEFAULT\s+ROLE\b|(?:LOCK|UNLOCK)\s+(?:TABLES?|INSTANCE)\b"
    r"|START\s+TRANSACTION\b|BEGIN\b(?!\s+NOT\s+ATOMIC)"
    r"|(?:ANALYZE|OPTIMIZE|REPAIR|CHECK)\s+(?:NO_WRITE_TO_BINLOG\s+|LOCAL\s+)?TABLES?\b"
    r"|(?:FLUSH|RESET)\b|CACHE\s+INDEX\b|LOAD\s+INDEX\b|(?:INSTALL|UNINSTALL)\s+(?:PLUGIN|COMPONENT|SONAME)\b"
    r"|(?:START|STOP)\s+(?:REPLICA|SLAVE|GROUP_REPLICATION|ALL\s+SLAVES)\b|CHANGE\s+(?:REPLICATION|MASTER)\b)",
    re.I)
_AUTOCOMMIT_ON = re.compile(r"^\s*SET\b.*\bAUTOCOMMIT\s*(?::=|=)\s*(?:1|ON|TRUE)\b", re.I)


def implicit_commit(unit):
    """True when the statement commits an open transaction before it runs."""
    code = unit["code"]
    return bool(_IMPLICIT_COMMIT.match(code) or _AUTOCOMMIT_ON.match(code))


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
    ddl = list(_ddl_parts(plan["units"]))
    embedded = any(u.get("kind") != "read" and not _DDL_START.match(u["code"]) and
                   re.search(r"\b(?:ALTER\s+(?:(?:ONLINE|OFFLINE|IGNORE)\s+)*TABLE|"
                             r"DROP\s+(?:DATABASE|SCHEMA|(?:TEMPORARY\s+)?TABLE)|"
                             r"CREATE\s+(?:TEMPORARY\s+)?TABLE|RENAME\s+TABLE|TRUNCATE\s+TABLE)\b",
                             u["code"], re.I) for u in plan["units"])
    if embedded and decision["action"] != "refuse":
        decision.update(action="refuse", text="Run DDL one at a time as standalone statements so its target "
                        "and lock safeguards can be reviewed; nothing was sent.")
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
        elif re.match(r"\s*DROP\s+(?:DATABASE|SCHEMA)\b", code, re.I) and decision["action"] != "refuse":
            decision.update(action="refuse", text="The DROP database target could not be verified; nothing was sent.")
    alter = _alter_unit(plan)
    if alter is not None and decision["action"] != "refuse":
        tail = alter["code"][_ALTER_TABLE.match(alter["code"]).end():]
        wait = re.match(r"\s+WAIT\s+(\d+)\b", tail, re.I)
        if ((wait and int(wait.group(1)) > ALTER_LOCK_WAIT_SECONDS) or
                re.search(r"\bSET_VAR\s*\([^)]*\block_wait_timeout\s*=", alter["send"], re.I)):
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
                            connection={k: options.get(k) for k in ("host", "port", "user")}, safety=safety,
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
    except (ConnectorError, OSError) as exc:
        registry.remove(tab_id, session)
        raise ConnectorError(str(exc))
    return session


SAFE_UPDATES_OFF = TAG + "SET SESSION sql_safe_updates = 0"
SAFE_UPDATES_ON = TAG + "SET SESSION sql_safe_updates = 1"


def _set_safe_updates(session, statement, audit=None):
    """Send the tab's safe-updates setting (tagged); busy when a run is out."""
    if not session.run_lock.acquire(False):
        raise EditorError("A statement is running in this tab. Wait for it or press Stop.", "busy")
    try:
        session._verify_identity(audit, [statement])
        if audit is not None:
            audit.server(session.engine, session.server_version)
            try:
                audit.attempt([statement], target=session.server_identity, verified=["server_identity"])
            except Exception as exc:
                raise EditorError("The audit record could not be written, so nothing was sent: %s" % exc,
                                  "audit_failed")
        try:
            step = session._helper(statement, results=0)
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
        if not session.refresh_state() or session.ended or session.safe_updates != session.safe_updates_wanted:
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
    retain their original reasons and target requirements.
    """
    if payload.get("require_confirmation") is True and decision["action"] == "allow":
        return dict(decision, action="confirm", title="Review server change", tone="danger",
                    text="Review the exact statements and connection before applying this change.")
    return decision


def _run_api(payload, workspace, fill_password):
    store = workspace.store
    registry = _registry(workspace)
    tab_id = _tab_id(payload)
    options, identity, safety, name = _connection_facts(payload, store)
    limit = int(_number(payload, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT, "limit"))
    timeout = _number(payload, "timeout", 0, 0, 86400, "timeout") or None
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
        password = password or full_options.get("password")
        audit.password = password
        changed = _policy_changed(payload, store, identity, safety, audit, [u["send"] for u in plan["units"]])
        if changed:
            return changed
        if session is None:
            session = _open_session(registry, tab_id, full_options, identity, safety, name, payload,
                                    created=started)
        plan = _plan_run(payload, session)
        decision = _requested_review(_decision(plan, safety, session), payload)
        if decision["action"] != "refuse":
            review = _alter_review(plan, session)
            decision["alter_preflight"] = review
            decision["alter_binding"] = _alter_binding(session, review)
            decision["warnings"] += review.get("warnings", [])
    statements = [u["send"] for u in plan["units"]]
    writes = [u["send"] for u in plan["units"] if u.get("kind") != "read"]
    digest = _review_digest(tab_id, identity, statements, decision)
    audit = _audit_trail(store, "editor_run", safety, options, password, decision)
    blocked = _gate(registry, decision, payload, digest, audit, statements, writes, name, options)
    if blocked is not None:
        blocked["session"] = session.status() if session else None
        return blocked
    # The policy allowed the run: only now fill the saved password.
    filled = filled if filled is not None else fill_password(payload)
    from .live import connection_options
    full_options = connection_options(filled.get("connection"))
    password = password or full_options.get("password")
    audit.password = password
    changed = _policy_changed(payload, store, identity, safety, audit, statements)
    if changed:
        return changed
    if session is None:
        initial_decision = decision
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
        again = [u["send"] for u in plan["units"]]
        if (decision["action"] == "refuse" or again != statements
                or decision["action"] != initial_decision["action"]
                or decision.get("warnings") != initial_decision.get("warnings")):
            digest = _review_digest(tab_id, identity, again, decision)
            audit = _audit_trail(store, "editor_run", safety, options, password, decision)
            blocked = _gate(registry, decision, dict(payload, confirmed=False), digest, audit, again,
                            [u["send"] for u in plan["units"] if u.get("kind") != "read"], name, options)
            if blocked is not None:
                blocked["session"] = session.status()
                return blocked
    has_write = any(u.get("kind") != "read" for u in plan["units"])
    run = Run(plan["units"], limit, full_options, plan["text"], plan["offsets"], plan["scope"],
              audit=audit if has_write else None, timeout=timeout,
              history={"record": _history_recorder(store, session, password)},
              alter_context=decision.get("alter_preflight"))
    changed = _policy_changed(payload, store, identity, safety, audit, [u["send"] for u in plan["units"]])
    if changed:
        # Nothing was sent. A session this request started (under the old
        # policy) is ended instead of being left open until Close; an
        # existing one is kept, as is one another request holds for a run or
        # helper (run_lock is held for a whole run, so taking it reserves the
        # session: a later start_run sees it ended and refuses).
        if started.get("session") is session and session.run_lock.acquire(False):
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
    options, identity, safety, name = _connection_facts(payload, store)
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
        _set_safe_updates(session, SAFE_UPDATES_OFF, audit)
    elif wanted is True:
        _set_safe_updates(session, SAFE_UPDATES_ON, audit)
    return {"status": session.status()["state"], "session": session.status()}


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
    registry = _registry(workspace)
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
        session = None
        if isinstance(payload.get("connection"), dict):
            options, identity, safety, name = _connection_facts(payload, store)
            try:
                session = _owned_session(registry, tab_id, identity)
            except EditorError as exc:
                if exc.code != "identity_mismatch":
                    raise
    if path == "/api/editor/status":
        if session is None:
            return {"status": "none", "session": None, "run": None}
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
    options, identity, safety, name = _connection_facts(payload, store)
    if path == "/api/editor/stop":
        session = _owned_session(registry, tab_id, identity)
        if session is None:
            return {"stopped": False, "message": "This tab has no session.", "session": None}
        filled = fill_password(payload)
        from .live import connection_options
        full = connection_options(filled.get("connection"))
        result = session.stop(full, force=payload.get("force") is True)
        if full.get("password") and result.get("message"):
            result["message"] = result["message"].replace(full["password"], "[redacted]")
        result["session"] = session.status()
        return result
    raise ValueError("Unknown workspace operation.")
