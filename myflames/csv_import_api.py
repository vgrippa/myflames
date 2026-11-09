"""Import CSV endpoints: choose or open a file, inspect it, plan an import
and apply it (db-operations skill: every import is a write).

Endpoints (POST, JSON):

* ``/api/import/csv/choose`` ``{}`` and ``/api/import/csv/open-path``
  ``{path}``: ``{file_id, name, size, modified}`` or ``{cancelled: true}``.
  File ids map to resolved paths in this process's memory only; no later
  request names a path.
* ``/api/import/csv/inspect`` ``{file_id, format?}``: ``csv-inspect-1.0``.
* ``/api/import/csv/plan`` ``{connection, file_id, format, target, method,
  transaction, on_error, progress_id?}``: ``csv-import-plan-1.0``. It reads
  the server (variables, the target's metadata, the account's privileges)
  in a private session, checks every row of the file, and issues a review
  token bound to the whole plan (connection identity with type and
  read-only, the file's size, mtime and SHA-256, the format, target and its
  metadata, mapping, method, transaction and the statements) when nothing
  blocks it.
* ``/api/import/csv/apply`` ``{connection, token, progress_id}``:
  ``csv-import-result-1.0``. Before anything is sent: the saved policy
  (Read-only refuses, audited, before any connection opens), the token, the
  file (size, mtime and a full SHA-256), the target's metadata, the policy
  again, privileges from SHOW GRANTS (a denial refuses; unknown goes on),
  @@GLOBAL.local_infile for LOAD DATA, then the pending audit record. A
  refusal there is a 400 and nothing was sent. After that every outcome is
  a 200 result: done, failed, stopped or unknown (only when a COMMIT, a
  CREATE TABLE or a statement on a non-transactional table got no reply).
  Nothing is ever retried.
* ``/api/import/csv/progress`` and ``/api/import/csv/stop``
  ``{progress_id}``. Stop ends an INSERT import between statements; a LOAD
  DATA statement in flight is stopped with KILL QUERY from a second
  connection. All or nothing then rolls back; Commit every N keeps what was
  committed.

The session is an :class:`editor_session.EditorSession` of its own (never a
Query editor tab): a fresh client with the connection's options, the
statements on stdin, LOCAL INFILE enabled only for a LOAD DATA import and
restricted to its private folder when the reviewed client supports it. It
sends ``SET SESSION sql_mode`` with STRICT_ALL_TABLES added,
``lock_wait_timeout = 5`` and ``innodb_lock_wait_timeout = 5`` first, then
CREATE TABLE (it commits on its own), then START TRANSACTION, the data
statements and COMMIT (once, or every N rows). LOAD DATA LOCAL reads a
checked copy of the rows that myflames writes to a private temporary folder
(UTF-8, tab separated, ``\\N`` for NULL), never the original file: the
server treats LOCAL as IGNORE (mysql-server sql/parse_tree_nodes.cc and
mariadb-server sql/sql_load.cc, "We can't give an error in the middle when
using LOCAL files"), turning data errors into warnings and skipping
duplicate keys, so myflames checks every value first and compares the
row count and the warnings after each statement; any difference rolls the
statement's transaction back. myflames never changes local_infile (MySQL's
default is OFF, MariaDB's ON: mysql-server sql/sys_vars.cc and
mariadb-server sql/sys_vars.cc ``Sys_local_infile``).
"""
import collections
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from . import csv_import as ci
from . import dbops, editor_session as ed, privileges
from .connector import ClientMissingError, ConnectorError
from .live import connection_options
from .perf_common import literal
from .review_tokens import ReviewTokens

PATHS = ("/api/import/csv/choose", "/api/import/csv/open-path", "/api/import/csv/inspect",
         "/api/import/csv/plan", "/api/import/csv/apply", "/api/import/csv/progress", "/api/import/csv/stop")
WRITE_PATHS = ("/api/import/csv/apply",)
PLAN_SCHEMA = "csv-import-plan-1.0"
RESULT_SCHEMA = "csv-import-result-1.0"
PROGRESS_SCHEMA = "csv-import-progress-1.0"
OPERATION = "import_csv"
FILE_TTL = 3600
FILE_LIMIT = 32
PLAN_TTL = 600
PLAN_LIMIT = 8
RUNNING_LIMIT = 2
PROGRESS_LIMIT = 64
PROGRESS_TTL = 3600
EVERY_LIMIT = 1000000
#: Seconds one INSERT statement or COMMIT may take before the session is ended.
STATEMENT_TIMEOUT = 300.0
#: Seconds one LOAD DATA chunk may take.
LOAD_TIMEOUT = 3600.0
LOAD_CHUNK_ROWS = 50000
LOAD_CHUNK_BYTES = 256 * 1024 * 1024
#: SELECT COUNT(*) after the import only up to this many rows.
COUNT_LIMIT = 1000000
#: The mysql and mariadb clients' default max_allowed_packet (16 MiB) also
#: bounds a statement they send.
CLIENT_PACKET = 16 * 1024 * 1024
#: Server errors that reject one row; with "Skip and report" the statement is
#: sent again row by row (transactional tables only: InnoDB rolls back only
#: the failed statement) and the rows the server rejects are skipped.
ROW_ERRORS = (1048, 1062, 1263, 1264, 1265, 1292, 1364, 1366, 1406, 1452, 1690, 3140, 3819, 4025)
#: The import session: STRICT_ALL_TABLES added and MariaDB's
#: EMPTY_STRING_IS_NULL taken out of sql_mode (an empty string must stay one;
#: other modes, NO_BACKSLASH_ESCAPES included, stay as the server has them),
#: and lock waits of at most 5 s. Checked with a read before any row is sent.
SETUP = ["SET SESSION sql_mode = TRIM(BOTH ',' FROM REPLACE(CONCAT(',', @@SESSION.sql_mode, ',STRICT_ALL_TABLES,'), "
         "',EMPTY_STRING_IS_NULL,', ','))",
         "SET SESSION lock_wait_timeout = 5", "SET SESSION innodb_lock_wait_timeout = 5"]
_PROGRESS_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_AT_ROW = re.compile(r"\bat row (\d+)\b")
_LOCK = threading.Lock()
_CREATE_LOCK = threading.Lock()


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso(mtime):
    return datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


# ---- registry ----------------------------------------------------------------------------------

class Progress(object):
    """What one plan check or import is doing now (no SQL, no credentials)."""

    def __init__(self, kind):
        self.kind = kind
        self.lock = threading.Lock()
        self.phase = "checking"
        self.rows_total = None
        self.rows_checked = 0
        self.rows_sent = 0
        self.rows_committed = 0
        self.batches_done = 0
        self.batches_total = None
        self.in_flight = False
        self.started_at = _now()
        self.started = time.monotonic()
        self.touched = time.monotonic()
        self.stop = threading.Event()
        self._killer = None

    def set(self, **values):
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)
            self.touched = time.monotonic()

    def killer(self, function):
        with self.lock:
            self._killer = function

    def request_stop(self):
        with self.lock:
            already = self.stop.is_set()
            self.stop.set()
            killer = self._killer if self.phase not in ("finished", "committing", "rolling_back") else None
            phase = self.phase
        if phase == "finished":
            return "This import has already finished."
        if killer is not None and not already:
            outcome, problem = killer()
            if outcome == "sent":
                return ("Stopping: KILL QUERY was sent for the statement in flight. The server reads the rest of this "
                        "chunk before it answers, so this can take as long as one chunk.")
            if outcome == "failed":
                return "Stopping after the statement in flight (KILL QUERY failed: %s)." % problem
        if self.kind == "plan":
            return "Stopping the check."
        return "Stopping before the next statement."

    def snapshot(self):
        with self.lock:
            return {"schema_version": PROGRESS_SCHEMA, "kind": self.kind, "phase": self.phase,
                    "rows_total": self.rows_total, "rows_checked": self.rows_checked, "rows_sent": self.rows_sent,
                    "rows_committed": self.rows_committed, "batches_done": self.batches_done,
                    "batches_total": self.batches_total, "in_flight": self.in_flight, "started_at": self.started_at,
                    "elapsed_s": round(time.monotonic() - self.started, 1), "stop_requested": self.stop.is_set()}


class PlanRecord(object):
    def __init__(self, token, identity, details, internal, public):
        self.token = token
        self.identity = identity
        self.details = details
        self.internal = internal
        self.public = public
        self.created = time.monotonic()


class CsvImports(object):
    """Files, plans and progress of this workspace instance (memory only)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.files = collections.OrderedDict()
        self.plans = collections.OrderedDict()
        self.progress = collections.OrderedDict()
        self.tokens = ReviewTokens(ttl=PLAN_TTL, capacity=PLAN_LIMIT)
        self.running = 0
        self.closed = False

    # files
    def add_file(self, path):
        info = os.stat(path)
        entry = {"file_id": secrets.token_urlsafe(18), "path": path, "name": os.path.basename(path),
                 "size": info.st_size, "mtime_ns": info.st_mtime_ns, "modified": _iso(info.st_mtime),
                 "added": time.monotonic()}
        with self.lock:
            self._prune()
            while len(self.files) >= FILE_LIMIT:
                self.files.popitem(last=False)
            self.files[entry["file_id"]] = entry
        return {"file_id": entry["file_id"], "name": entry["name"], "size": entry["size"],
                "modified": entry["modified"]}

    def file(self, file_id):
        with self.lock:
            self._prune()
            entry = self.files.get(file_id) if isinstance(file_id, str) else None
        if entry is None:
            raise ValueError("This file is no longer open in myflames. Choose it again.")
        return entry

    # plans
    def add_plan(self, record):
        with self.lock:
            self._prune()
            while len(self.plans) >= PLAN_LIMIT:
                _discard(self.plans.popitem(last=False)[1])
            self.plans[record.token] = record

    def plan(self, token):
        with self.lock:
            self._prune()
            record = self.plans.get(token) if isinstance(token, str) and len(token) <= 128 else None
        if record is None:
            raise ValueError("This review expired, changed or was already used. Review the import again; "
                             "nothing was sent.")
        return record

    def claim(self, record):
        """True for the first apply of a plan; a second one is refused."""
        with self.lock:
            if record.internal.get("claimed"):
                return False
            record.internal["claimed"] = True
            record.internal["in_use"] = True
            return True

    def drop_plan(self, token):
        with self.lock:
            record = self.plans.pop(token, None)
        if record is not None:
            _discard(record)

    # progress
    def start_progress(self, progress_id, kind):
        progress = Progress(kind)
        if not isinstance(progress_id, str) or not _PROGRESS_ID.match(progress_id):
            return progress
        with self.lock:
            self._prune()
            if progress_id in self.progress:
                return progress
            while len(self.progress) >= PROGRESS_LIMIT:
                self.progress.popitem(last=False)
            self.progress[progress_id] = progress
        return progress

    def get_progress(self, progress_id):
        if not isinstance(progress_id, str) or not _PROGRESS_ID.match(progress_id):
            raise ValueError("Invalid progress_id.")
        with self.lock:
            return self.progress.get(progress_id)

    # running imports
    def slot(self):
        with self.lock:
            if self.closed:
                raise ValueError("The workspace is closing. Nothing was sent.")
            if self.running >= RUNNING_LIMIT:
                raise ValueError("Two imports are already running. Wait for one to finish; nothing was sent.")
            self.running += 1

    def release(self):
        with self.lock:
            self.running -= 1

    def _prune(self):
        now = time.monotonic()
        for key in [k for k, v in self.files.items() if now - v["added"] > FILE_TTL]:
            self.files.pop(key, None)
        for key in [k for k, v in self.plans.items() if now - v.created > PLAN_TTL]:
            _discard(self.plans.pop(key))
        for key in [k for k, v in self.progress.items() if now - v.touched > PROGRESS_TTL]:
            self.progress.pop(key, None)

    def close(self):
        with self.lock:
            self.closed = True
            progresses = list(self.progress.values())
            plans = list(self.plans.values())
            self.plans.clear()
        for progress in progresses:
            progress.stop.set()
        for record in plans:
            _discard(record)


CHUNK_PREFIX = "myflames-csv-"
_CHUNK_DIR = re.compile(r"myflames-csv-([0-9]+)-[A-Za-z0-9_]+\Z", re.ASCII)
_CHUNK_FILE = re.compile(r"chunk-[0-9]{6}\.tsv\Z", re.ASCII)


def chunk_folder():
    """A private folder (0700) for checked copies, named with this process id."""
    return tempfile.mkdtemp(prefix="%s%d-" % (CHUNK_PREFIX, os.getpid()))


def _alive(pid):
    if os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def sweep_chunk_folders(root=None, now=None):
    """Remove checked-copy folders an earlier myflames process left behind
    (it was killed during a LOAD DATA import): only folders named like ours,
    owned by this user, holding nothing but chunk files, whose process is
    gone (on Windows: a day old). Returns the folders removed."""
    root = root or tempfile.gettempdir()
    now = time.time() if now is None else now
    removed = []
    try:
        names = os.listdir(root)
    except OSError:
        return removed
    for name in names:
        match = _CHUNK_DIR.match(name)
        if not match or int(match.group(1)) == os.getpid():
            continue
        path = os.path.join(root, name)
        try:
            info = os.lstat(path)
            if not stat.S_ISDIR(info.st_mode):
                continue
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                continue
            alive = _alive(int(match.group(1)))
            if alive or (alive is None and now - info.st_mtime < 86400):
                continue
            entries = os.listdir(path)
            if any(not _CHUNK_FILE.match(entry) or not os.path.isfile(os.path.join(path, entry))
                   or os.path.islink(os.path.join(path, entry)) for entry in entries):
                continue
            shutil.rmtree(path)
            removed.append(path)
        except OSError:
            continue
    return removed


def _discard(record):
    folder = record.internal.get("chunk_dir") if isinstance(record.internal, dict) else None
    if folder and not record.internal.get("in_use"):
        shutil.rmtree(folder, ignore_errors=True)


def registry(workspace):
    with _CREATE_LOCK:
        imports = getattr(workspace, "csv_imports", None)
        if imports is None:
            imports = workspace.csv_imports = CsvImports()
            sweep_chunk_folders()
        return imports


# ---- session helpers ----------------------------------------------------------------------------

def _redact(text, password):
    return dbops.redact(str(text), password)


def _send(session, sql, timeout=STATEMENT_TIMEOUT, progress=None, options=None):
    """Send one statement; returns ``(step, None)``, ``(step, error)`` or
    ``(step, "unknown")`` when no answer arrived (the session is then ended)."""
    try:
        step = session._send("helper", sql, results=1)
    except ConnectorError:
        return None, "unknown"
    if progress is not None and options is not None:
        progress.killer(lambda: session._kill_query(options, step))
    try:
        ok = session._wait(lambda: step.marker and (step.closed or bool(step.real_errors())), timeout)
    finally:
        if progress is not None:
            progress.killer(None)
    if not ok or not step.marker or step.unfinished:
        if not session.ended:
            session.end("protocol", "The MySQL client stopped answering. " + ed.SESSION_ENDED_TEXT)
        return step, "unknown"
    errors = step.real_errors()
    if errors and errors[0].get("code") in ed.LOST_CONNECTION_CODES:
        # The client lost the server (2013, 2006, ...): the statement may have
        # run. The batch client does not reconnect; end the session.
        session.end("connection_lost", ed.SESSION_ENDED_TEXT)
        return step, "unknown"
    return step, (errors[0] if errors else None)


def _ok_entry(step):
    return next((e for e in step.entries if e.get("type") == "ok"), None) if step is not None else None


def _query(session, sql, timeout=30.0):
    """Rows of one tagged read as dicts; raises ConnectorError or ValueError."""
    step, error = _send(session, ci.TAG + sql, timeout)
    if error == "unknown":
        raise ConnectorError("The import session ended while reading the server.")
    if error:
        raise ConnectorError("ERROR %s: %s" % (error.get("code"), error.get("message")))
    if any(entry.get("truncated") for entry in step.entries):
        raise ValueError("A server answer exceeded a safety limit.")
    result = next((entry for entry in step.entries if entry.get("type") == "rows"), None)
    if result is None:
        return []
    keys = [c["name"] for c in result["columns"]]
    return [dict(zip(keys, row)) for row in result["rows"]]


def _variables(session, names, scope="SESSION"):
    rows = _query(session, "SHOW %s VARIABLES WHERE Variable_name IN (%s)" % (
        scope, ", ".join("'%s'" % n for n in names)))
    return {(r.get("Variable_name") or "").lower(): r.get("Value") for r in rows}


def read_metadata(session, schema, table, exact=True):
    """The target as the server describes it now, or ``{"exists": False}``.

    *exact* (lower_case_table_names = 0) compares names with utf8mb4_bin:
    information_schema's columns compare case- and accent-insensitively
    against a utf8mb4 literal, so `Orders` would also find `orders` and merge
    their columns. With lower_case_table_names 1 or 2 the server itself
    compares names without case, and so does this read."""
    def lit(value):
        return literal(value) + (" COLLATE utf8mb4_bin" if exact else "")
    where = "TABLE_SCHEMA=%s AND TABLE_NAME=%s" % (lit(schema), lit(table))
    tables = _query(session, "SELECT TABLE_TYPE, ENGINE, TABLE_ROWS FROM information_schema.TABLES WHERE " + where)
    schemas = _query(session, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s"
                     % lit(schema))
    if not tables:
        return {"exists": False, "schema_exists": bool(schemas)}
    table_row = tables[0]
    rows = _query(session, "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA, "
                  "CHARACTER_SET_NAME, COLLATION_NAME, CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, "
                  "DATETIME_PRECISION FROM information_schema.COLUMNS WHERE " + where +
                  " ORDER BY ORDINAL_POSITION LIMIT 4097")
    keys = _query(session, "SELECT COLUMN_NAME FROM information_schema.STATISTICS WHERE " + where +
                  " AND INDEX_NAME='PRIMARY' ORDER BY SEQ_IN_INDEX")
    triggers = _query(session, "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE EVENT_OBJECT_SCHEMA=%s "
                      "AND EVENT_OBJECT_TABLE=%s AND EVENT_MANIPULATION='INSERT' ORDER BY TRIGGER_NAME LIMIT 101"
                      % (lit(schema), lit(table)))
    foreign = _query(session, "SELECT COUNT(*) AS n FROM information_schema.REFERENTIAL_CONSTRAINTS WHERE "
                     "CONSTRAINT_SCHEMA=%s AND TABLE_NAME=%s" % (lit(schema), lit(table)))
    checks = []
    try:
        if session.engine == "mariadb":
            checks = _query(session, "SELECT CONSTRAINT_NAME, CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS "
                            "WHERE CONSTRAINT_SCHEMA=%s AND TABLE_NAME=%s ORDER BY CONSTRAINT_NAME"
                            % (lit(schema), lit(table)))
        else:
            checks = _query(session, "SELECT c.CONSTRAINT_NAME, c.CHECK_CLAUSE FROM information_schema.TABLE_CONSTRAINTS t "
                            "JOIN information_schema.CHECK_CONSTRAINTS c ON c.CONSTRAINT_SCHEMA=t.CONSTRAINT_SCHEMA "
                            "AND c.CONSTRAINT_NAME=t.CONSTRAINT_NAME WHERE t.TABLE_SCHEMA=%s AND t.TABLE_NAME=%s "
                            "AND t.CONSTRAINT_TYPE='CHECK' ORDER BY c.CONSTRAINT_NAME" % (lit(schema), lit(table)))
    except ConnectorError:
        checks = []
    # MariaDB's JSON is LONGTEXT with CHECK (json_valid(`col`)).
    json_columns = set()
    for check in checks:
        match = re.match(r"^\s*json_valid\(`((?:[^`]|``)+)`\)\s*$", check.get("CHECK_CLAUSE") or "", re.I)
        if match:
            json_columns.add(match.group(1).replace("``", "`"))
    columns = [ci.from_information_schema(r, json_columns) for r in rows]
    try:
        estimate = int(table_row.get("TABLE_ROWS")) if table_row.get("TABLE_ROWS") not in (None, "") else None
    except ValueError:
        estimate = None
    engine = table_row.get("ENGINE") or ""
    return {"exists": True, "schema_exists": True, "table_type": table_row.get("TABLE_TYPE"), "engine": engine,
            "transactional": (engine.lower() in ci.TRANSACTIONAL_ENGINES) if engine else None,
            "columns": columns, "rows_estimate": estimate, "primary_key": [k["COLUMN_NAME"] for k in keys],
            "triggers": [t["TRIGGER_NAME"] for t in triggers], "foreign_keys": int((foreign or [{}])[0].get("n") or 0),
            "checks": [{"name": c.get("CONSTRAINT_NAME"), "clause": c.get("CHECK_CLAUSE")} for c in checks]}


def metadata_fingerprint(meta):
    if not meta.get("exists"):
        return {"exists": False}
    return {"exists": True, "table_type": meta["table_type"], "engine": meta["engine"],
            "columns": [c.fingerprint() for c in meta["columns"]], "primary_key": meta["primary_key"],
            "triggers": meta["triggers"], "foreign_keys": meta["foreign_keys"],
            "checks": [[c["name"], c["clause"]] for c in meta["checks"]]}


_LOAD_DIR_SUPPORT = {}
_LOAD_DIR_LOCK = threading.Lock()


def client_binary(full):
    """The mysql or mariadb client a session with these options runs."""
    from .connector import MySQLConnection
    # Pin symlinks too: an installer may replace the selected link between
    # checking the client's file policy and starting the import session.
    return os.path.realpath(MySQLConnection(**full)._resolve_binary())


def limits_local_files(binary):
    """True when this client has --load-data-local-dir (MySQL clients from
    8.0.21: mysql-server client/mysql.cc "load_data_local_dir"). With
    --local-infile=0 and that folder, the client sends only files whose real
    path starts with the folder's (libmysql/libmysql.cc
    is_valid_local_infile_name) and still tells the server it can send local
    files (sql-common/client.cc "Always send CLIENT_LOCAL_FILES"). The
    MariaDB client (client/mysql.cc) has only --local-infile, so with it the
    client sends any file the server asks for during that session."""
    def identity():
        info = os.stat(binary)
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    failure = ("The database client's local-file policy could not be checked. "
               "Check the database client in Settings and review the import again.")
    try:
        key = identity()
    except OSError as exc:
        raise ClientMissingError(failure) from exc
    with _LOAD_DIR_LOCK:
        if key in _LOAD_DIR_SUPPORT:
            return _LOAD_DIR_SUPPORT[key]
    home = tempfile.mkdtemp(prefix="myflames-client-")
    try:
        proc = subprocess.run([binary, "--no-defaults", "--help"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10, check=False, env=ed._clean_env(home))
        if proc.returncode != 0 or identity() != key:
            raise ClientMissingError(failure)
        text = (proc.stdout or b"").decode("utf-8", "replace")
        supported = bool(re.search(r"^\s*--load[-_]data[-_]local[-_]dir\b", text, re.M))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClientMissingError(failure) from exc
    finally:
        shutil.rmtree(home, ignore_errors=True)
    with _LOAD_DIR_LOCK:
        _LOAD_DIR_SUPPORT[key] = supported
    return supported


def load_flags(full, folder, binary=None):
    """The client options of a LOAD DATA session and whether the client limits
    which files it sends to *folder*."""
    # A failed client check is not evidence that it lacks a folder limit.
    # Propagate it before any client can connect with unrestricted LOCAL.
    restricted = limits_local_files(binary=binary if binary is not None else client_binary(full))
    if restricted:
        return ("--local-infile=0", "--load-data-local-dir=" + folder), True
    return ("--local-infile=1",), False


def _open_session(full, identity, safety, label, load_dir=None, client_flags=None):
    full = dict(full)
    if "binary" not in full:
        full["binary"] = client_binary(full)
    session = ed.EditorSession("csv-" + secrets.token_hex(12), identity, full, safety["type"], False, True,
                               PLAN_TTL, label)
    if load_dir:
        # Both clients ask the server for LOAD DATA LOCAL only when allowed to
        # send local files: the MySQL client with --local-infile (default off,
        # mysql-server client/mysql.cc) or a --load-data-local-dir folder; the
        # MariaDB client with --local-infile (its default comes from the build,
        # ENABLED_LOCAL_INFILE; libmariadb is not in the checkout to cite). The
        # server refuses LOAD DATA LOCAL otherwise (MySQL 3948, MySQL 5.7 1148,
        # MariaDB 4166, seen in the lab). Where the client can, only the private folder of
        # checked copies may be sent. This session runs only the reviewed import.
        # Apply passes the flags it checked against the review. Do not probe
        # again here and silently choose a different file policy.
        session.client_flags = (client_flags if client_flags is not None else
                                load_flags(full, load_dir, binary=full["binary"])[0])
    session.start(full)
    return session


def _actor(session):
    try:
        return privileges.read_privileges(session._conn)
    except (ConnectorError, ValueError):
        return {"checked": False, "grants": [], "roles": [], "engine": session.engine, "version": session.server_version}


def _privilege_answers(actor, mode, schema, table):
    actions = ["create_table", "insert"] if mode == "new" else ["insert"]
    answers = []
    for action in actions:
        answer = privileges.can(actor, action, database=schema, table=table)
        answers.append({"action": action, "state": answer["state"], "reason": answer["reason"]})
    return answers


# ---- request checks ----------------------------------------------------------------------------

def _check_target(value):
    if not isinstance(value, dict):
        raise ValueError("Choose the table to import into.")
    mode = value.get("mode")
    if mode not in ("existing", "new"):
        raise ValueError("Choose an existing table or a new table.")
    schema = ci.check_name(value.get("schema"), "database name")
    table = ci.check_name(value.get("table"), "table name")
    mapping = value.get("mapping")
    if not isinstance(mapping, list) or not mapping or len(mapping) > 4096:
        raise ValueError("Map at least one CSV column to a table column.")
    pairs, seen_columns, seen_indexes = [], set(), set()
    for item in mapping:
        if not isinstance(item, dict) or set(item) - {"csv_index", "column"}:
            raise ValueError("Each mapping entry names csv_index and column.")
        index = item.get("csv_index")
        if index is not None and (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < ci.MAX_COLUMNS):
            raise ValueError("csv_index must be a CSV column number or null.")
        column = ci.check_name(item.get("column"), "column name")
        if column.lower() in seen_columns:
            raise ValueError("Column %s is mapped twice." % column)
        if index is not None and index in seen_indexes:
            raise ValueError("CSV column %d is mapped twice." % (index + 1))
        seen_columns.add(column.lower())
        if index is not None:
            seen_indexes.add(index)
        pairs.append((index, column))
    if not seen_indexes:
        raise ValueError("Map at least one CSV column to a table column.")
    new_columns, add_id = [], False
    if mode == "new":
        columns = value.get("new_columns")
        if not isinstance(columns, list) or not columns or len(columns) > 1000:
            raise ValueError("A new table needs 1 to 1,000 columns.")
        names = set()
        for item in columns:
            if not isinstance(item, dict) or set(item) - {"name", "sql_type", "nullable"}:
                raise ValueError("Each new column has a name, a type and NULL allowed or not.")
            name = ci.check_name(item.get("name"), "column name")
            if name.lower() in names:
                raise ValueError("Two new columns are named %s." % name)
            names.add(name.lower())
            column = ci.parse_type(item.get("sql_type"))
            column.name = name
            column.source_type = item.get("sql_type")  # parsed again with the server's engine
            if not isinstance(item.get("nullable", True), bool):
                raise ValueError("NULL allowed must be true or false.")
            column.nullable = item.get("nullable", True)
            new_columns.append(column)
        add_id = value.get("add_id_key", False)
        if not isinstance(add_id, bool):
            raise ValueError("Add an id primary key must be true or false.")
        if add_id and "id" in names:
            raise ValueError("A column is already named id. Rename it, or turn off Add an id primary key.")
        unknown = [c for _, c in pairs if c.lower() not in names]
        if unknown:
            raise ValueError("Column %s is not one of the new table's columns." % unknown[0])
    return {"mode": mode, "schema": schema, "table": table, "mapping": pairs, "new_columns": new_columns,
            "add_id_key": add_id}


def _check_options(payload):
    method = payload.get("method", "insert")
    if method not in ("insert", "load_data"):
        raise ValueError("Choose INSERT batches or LOAD DATA LOCAL.")
    transaction = payload.get("transaction") or {"mode": "all"}
    if not isinstance(transaction, dict) or transaction.get("mode") not in ("all", "every"):
        raise ValueError("Choose all or nothing, or commit every N rows.")
    every = None
    if transaction["mode"] == "every":
        every = transaction.get("every")
        if isinstance(every, bool) or not isinstance(every, int) or not 1 <= every <= EVERY_LIMIT:
            raise ValueError("Commit every N rows takes N from 1 to 1,000,000.")
    on_error = payload.get("on_error", "stop")
    if on_error not in ("stop", "skip"):
        raise ValueError("Bad rows either stop the import or are skipped and reported.")
    return method, {"mode": transaction["mode"], "every": every}, on_error


# ---- the rows of a file ----------------------------------------------------------------------------

class _Mapped(object):
    """One target column filled from the file."""

    def __init__(self, index, column):
        self.index = index
        self.column = column


def converted_rows(records, fmt, mapped, columns_count):
    """``(line, items, problems)`` for every data record of *records* (the
    header row already taken out by :func:`csv_import.split_header`).
    *columns_count* is the file's width (None: the first good record's)."""
    width = columns_count
    for line, fields in records:
        if isinstance(fields, ci.CsvProblem):
            yield line, None, [{"line": line, "column": None, "value": None, "message": fields.message}]
            continue
        if width is None:
            width = len(fields)
        if len(fields) != width:
            if len(fields) > width and not any(f.strip() for f in fields[width:]):
                fields = fields[:width]
            else:
                yield line, None, [{"line": line, "column": None, "value": None,
                                    "message": "This line has %d fields; the file has %d columns." % (len(fields), width)}]
                continue
        items, problems = [], []
        for target in mapped:
            raw = fields[target.index] if target.index < len(fields) else ""
            try:
                items.append(ci.convert(raw, target.column, fmt))
            except ValueError as exc:
                problems.append({"line": line, "column": target.column.name, "value": ci.preview_value(raw),
                                 "message": "The value %s" % exc})
        yield line, (None if problems else items), problems


def _width(path, fmt):
    """The number of CSV columns: the header row's, or (no header, or one that
    cannot be read) the first readable data record's; None when the file
    cannot be read that far (the row check reports where)."""
    header = {}
    with open(path, "rb") as raw:
        try:
            for _line, fields in ci.split_header(ci.Records(raw, fmt), fmt, header):
                if header.get("fields"):
                    return len(header["fields"])
                if isinstance(fields, ci.CsvProblem):
                    continue
                return len(fields)
        except ci.ReadProblem:
            return None
    return len(header["fields"]) if header.get("fields") else 0


# ---- inspect -------------------------------------------------------------------------------------

def inspect(entry, fmt_value=None):
    path = entry["path"]
    info = os.stat(path)
    notes, problems = [], []
    problems_total = 0
    with open(path, "rb") as raw:
        sample = raw.read(ci.DETECT_BYTES)
        detected, detect_notes = ci.detect(sample, len(sample) >= info.st_size)
        notes.extend(detect_notes)
        fmt = ci.check_format(fmt_value, detected) if fmt_value is not None else detected
        if fmt_value is not None and "header" not in fmt_value and fmt != detected:
            # A separator, quote or encoding the person chose changes what the
            # first row looks like: decide the header row again with it.
            rows = ci.sample_rows(sample, fmt, len(sample) >= info.st_size)
            fmt["header"] = ci.detect_header(rows)
        raw.seek(0)
        records = ci.Records(raw, fmt)
        header_row = {}
        header, width, stats = None, None, []
        preview, preview_lines = [], []
        rows, counted = 0, True
        try:
            for line, fields in ci.split_header(records, fmt, header_row):
                if width is None and header_row.get("fields") is not None:
                    width = len(header_row["fields"])
                    header = header_row["fields"]
                    stats = [ci.Infer() for _ in range(min(width, ci.MAX_COLUMNS))]
                if isinstance(fields, ci.CsvProblem):
                    rows += 1
                    problems_total += 1
                    if len(problems) < ci.PROBLEM_LIMIT:
                        problems.append({"line": line, "message": fields.message})
                    continue
                if width is None:
                    width = len(fields)
                    header = ["" for _ in fields]
                    stats = [ci.Infer() for _ in range(min(width, ci.MAX_COLUMNS))]
                if width > ci.MAX_COLUMNS:
                    problems_total += 1
                    problems.append({"line": line, "message": "The file has %d columns; Import CSV reads at most "
                                                               "%d." % (width, ci.MAX_COLUMNS)})
                    counted = False
                    break
                rows += 1
                if len(fields) != width and not (len(fields) > width and not any(f.strip() for f in fields[width:])):
                    problems_total += 1
                    if len(problems) < ci.PROBLEM_LIMIT:
                        problems.append({"line": line, "message": "This line has %d fields; the file has %d columns."
                                         % (len(fields), width)})
                    continue
                for index in range(width):
                    stats[index].add(fields[index] if index < len(fields) else "", fmt)
                if len(preview) < ci.PREVIEW_ROWS:
                    preview.append([None if ci.is_null(f, fmt) else ci.preview_value(f) for f in fields[:width]])
                    preview_lines.append(line)
                if records.count >= ci.INSPECT_BYTES:
                    counted = False
                    break
        except ci.ReadProblem as exc:
            problems_total += 1
            problems.append({"line": exc.line, "message": str(exc)})
            counted = False
        if header_row.get("problem"):
            # The same rule as plan and apply: the first record is the header
            # row even when it cannot be read; the import refuses to run then.
            problems_total += 1
            problems.insert(0, {"line": header_row["line"], "message": "The header row cannot be read (%s). Fix it, or "
                                "turn off Header row." % header_row["problem"].rstrip(".")})
            del problems[ci.PROBLEM_LIMIT:]
        if width is None and header_row.get("fields") is not None:
            width = len(header_row["fields"])
            header = header_row["fields"]
            stats = [ci.Infer() for _ in range(min(width, ci.MAX_COLUMNS))]
        consumed = records.count
        blank = records.blank
    used = set()
    partial = not counted
    types = ci.fit_row([s.sql_type() for s in stats])
    columns = []
    for index, stat in enumerate(stats):
        title = header[index] if header and index < len(header) else ""
        inferred = stat.public(partial)
        inferred["sql_type"] = types[index]
        columns.append({"index": index, "header": title, "name": ci.column_name(title, index, used),
                        "inferred": inferred, "samples": stat.samples})
    if blank:
        notes.append("%s empty line%s skipped." % (ci.number_text(blank), " was" if blank == 1 else "s were"))
    if not counted and consumed:
        notes.append("Only the first %s MiB were read for this preview; the plan checks every row."
                     % (ci.INSPECT_BYTES // (1024 * 1024)))
    estimate = int(rows * info.st_size / float(consumed)) if not counted and consumed else None
    if types != [s.sql_type() for s in stats]:
        notes.append("Some text columns became TEXT so a row fits the server's 65,535-byte row size limit.")
    return {"schema_version": ci.INSPECT_SCHEMA,
            "file": {"file_id": entry["file_id"], "name": entry["name"], "size": info.st_size,
                     "modified": _iso(info.st_mtime)},
            "detected": detected, "format": fmt, "columns": columns,
            "preview": {"rows": preview, "lines": preview_lines, "truncated": rows > len(preview)},
            "rows": rows if counted else None, "rows_estimate": estimate, "rows_counted": counted,
            "problems": problems, "problems_total": problems_total, "notes": notes}


# ---- plan --------------------------------------------------------------------------------------

#: Session values the plan reads (SHOW SESSION VARIABLES also lists global-only
#: ones). A name the server does not have is simply absent.
PLAN_VARIABLES = ["max_allowed_packet", "sql_require_primary_key", "sql_generate_invisible_primary_key",
                  "innodb_force_primary_key", "time_zone", "system_time_zone", "read_only", "super_read_only",
                  "lower_case_table_names", "sql_mode", "explicit_defaults_for_timestamp", "rocksdb_max_row_locks",
                  "group_replication_transaction_size_limit", "group_replication_group_name", "wsrep_on",
                  "wsrep_max_ws_size"]


def _server_facts(session, target, method):
    variables = _variables(session, PLAN_VARIABLES)
    global_vars = _variables(session, ["local_infile"], "GLOBAL")
    meta = read_metadata(session, target["schema"], target["table"], exact=_exact_names(variables))
    return variables, global_vars, meta


def _exact_names(variables):
    """Names compare exactly only with lower_case_table_names = 0 (unknown: exactly)."""
    return str(variables.get("lower_case_table_names", "0")).strip() in ("0", "")


def _mysql_before_80(version):
    """A known MySQL version before 8.0 (5.7)."""
    match = re.match(r"^(\d+)\.(\d+)", version or "")
    return bool(match) and (5, 0) <= (int(match.group(1)), int(match.group(2))) < (8, 0)


def timestamp_max_year(engine, version):
    """2106 on MariaDB 11.5 and later (64-bit builds; the server has the last
    word), else 2038 (mariadb-server include/my_time.h TIMESTAMP_MAX_YEAR;
    mysql-server keeps 2038)."""
    match = re.match(r"^(\d+)\.(\d+)", version or "")
    if engine == "mariadb" and match and (int(match.group(1)), int(match.group(2))) >= (11, 5):
        return 2106
    return 2038


def _on(value):
    return str(value or "").upper() in ("ON", "1", "TRUE")


def _packet_limit(variables):
    try:
        server = int(variables.get("max_allowed_packet") or 0)
    except ValueError:
        server = 0
    server = server or 4 * 1024 * 1024
    return max(64 * 1024, min(server, CLIENT_PACKET) - 4096)


def _mapping(target, meta, width):
    """(mapped columns in insert order, blockers)."""
    blockers = []
    if target["mode"] == "new":
        by_name = {c.name.lower(): c for c in target["new_columns"]}
    else:
        by_name = {c.name.lower(): c for c in meta["columns"]}
    mapped = []
    for index, name in target["mapping"]:
        column = by_name.get(name.lower())
        if column is None:
            raise ValueError("Column %s is not in %s." % (name, target["table"]))
        if index is None:
            continue
        if width is not None and index >= width:
            raise ValueError("CSV column %d does not exist; the file has %d columns." % (index + 1, width))
        if column.unsupported:
            blockers.append("Column %s %s." % (column.name, column.unsupported))
        mapped.append(_Mapped(index, column))
    filled = set(m.column.name.lower() for m in mapped)
    columns = target["new_columns"] if target["mode"] == "new" else meta["columns"]
    for column in columns:
        if column.name.lower() not in filled and column.required:
            blockers.append("Column %s has no default and does not accept NULL: map a CSV column to it." % column.name)
    return mapped, blockers


def _summary(method, schema, table, mapped, rows, statements, name):
    columns = ", ".join(ci.ident(m.column.name) for m in mapped)
    if method == "load_data":
        head = "LOAD DATA LOCAL INFILE ... INTO TABLE %s.%s (%s)" % (ci.ident(schema), ci.ident(table), columns)
        tail = " (a checked copy of the rows in a private temporary file)"
    else:
        head = "INSERT INTO %s.%s (%s) VALUES ..." % (ci.ident(schema), ci.ident(table), columns)
        tail = ""
    return "%s : %s row%s in %s statement%s from %s%s" % (
        head, ci.number_text(rows), "" if rows == 1 else "s", ci.number_text(statements),
        "" if statements == 1 else "s", name, tail)


def plan(payload, workspace, fill_password):
    imports = registry(workspace)
    options, identity, safety, label = ed._connection_facts(payload, workspace.store)
    entry = imports.file(payload.get("file_id"))
    fmt = ci.check_format(payload.get("format"))
    target = _check_target(payload.get("target"))
    method, transaction, on_error = _check_options(payload)
    progress = imports.start_progress(payload.get("progress_id"), "plan")
    decision = dbops.decide("risky", safety["type"], safety["read_only"], label)
    try:
        return _plan(payload, workspace, fill_password, imports, options, identity, safety, label, entry, fmt,
                     target, method, transaction, on_error, progress, decision)
    finally:
        progress.set(phase="finished")


def _plan(payload, workspace, fill_password, imports, options, identity, safety, label, entry, fmt, target, method,
          transaction, on_error, progress, decision):
    filled = fill_password(payload)
    full = dict(connection_options(filled.get("connection")))
    full.pop("database", None)
    password = full.get("password")
    session = None
    try:
        full["binary"] = client_binary(full)
        limits_files = limits_local_files(binary=full["binary"]) if method == "load_data" else None
        session = _open_session(full, identity, safety, label)
        variables, global_vars, meta = _server_facts(session, target, method)
        actor = _actor(session)
        engine, version, product = session.engine, session.server_version, session.server_product
        backslash = session.backslash_escapes
    except (ConnectorError, ValueError) as exc:
        raise ValueError(_redact(exc, password)) from None
    finally:
        if session is not None:
            session.end("csv_plan", quiet=True)
    blockers, warnings, notes = [], [], []
    if target["mode"] == "new":
        # Parsed again with the server's own type rules (UNSIGNED DECIMAL,
        # FLOAT and DOUBLE; DECIMAL's largest scale).
        fresh_columns = []
        for column in target["new_columns"]:
            fresh = ci.parse_type(getattr(column, "source_type", None) or ci.render_type(column), engine)
            fresh.name, fresh.nullable = column.name, column.nullable
            fresh_columns.append(fresh)
        target["new_columns"] = fresh_columns
    if target["mode"] == "existing":
        if not meta["exists"]:
            raise ValueError("Table %s.%s does not exist or is not visible to this account." % (
                target["schema"], target["table"]))
        if meta["table_type"] not in ("BASE TABLE", "SYSTEM VERSIONED"):
            raise ValueError("%s.%s is a %s. Import into a base table." % (
                target["schema"], target["table"], (meta["table_type"] or "view").lower()))
    else:
        if meta["exists"]:
            blockers.append("A table named %s already exists in %s. Choose it as an existing table, or another name."
                            % (target["table"], target["schema"]))
        elif not meta["schema_exists"]:
            blockers.append("Database %s does not exist or is not visible to this account." % target["schema"])
        if not target["add_id_key"]:
            if _on(variables.get("innodb_force_primary_key")):
                blockers.append("This server requires a primary key on new tables (innodb_force_primary_key is ON). "
                                "Turn on Add an id primary key.")
            elif _on(variables.get("sql_require_primary_key")):
                if _on(variables.get("sql_generate_invisible_primary_key")):
                    # MySQL adds the invisible key before it checks for one
                    # (mysql-server sql/sql_table.cc).
                    notes.append("The server generates an invisible primary key (my_row_id) for a new table without "
                                 "one (sql_generate_invisible_primary_key is ON).")
                else:
                    blockers.append("This server requires a primary key on new tables (sql_require_primary_key is "
                                    "ON). Turn on Add an id primary key.")
        if not _on(variables.get("explicit_defaults_for_timestamp")) and "explicit_defaults_for_timestamp" in variables \
                and any(c.data_type == "timestamp" and not c.nullable for c in target["new_columns"]):
            warnings.append("explicit_defaults_for_timestamp is OFF, so the server gives the first TIMESTAMP NOT NULL "
                            "column DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP (and later ones a zero "
                            "default): the created table differs from the CREATE TABLE shown here.")
    width = _width(entry["path"], fmt)
    mapped, mapping_blockers = _mapping(target, meta, width)
    blockers.extend(mapping_blockers)
    ts_max = timestamp_max_year(engine, version)
    for item in mapped:
        item.column.timestamp_max_year = ts_max
    table_engine = "InnoDB" if target["mode"] == "new" else (meta.get("engine") or "")
    transactional = True if target["mode"] == "new" else meta.get("transactional")
    if transaction["mode"] == "all" and transactional is not True:
        blockers.append("%s is not transactional, so all or nothing is not possible: the server keeps each "
                        "statement's rows. Choose Commit every N rows." % (table_engine or "This table's engine"))
    if on_error == "skip" and table_engine.lower() in ("ndbcluster", "ndb"):
        # A failed statement marks the whole NDB transaction for rollback
        # (mysql-server storage/ndb/plugin/ha_ndbcluster.cc), so the rows sent
        # before it could not be kept while the refused rows are skipped.
        blockers.append("On NDB a refused statement rolls back the whole transaction, so Skip and report cannot "
                        "skip one row and keep the rest. Choose Stop at the first bad row.")
    local_infile = _on(global_vars.get("local_infile")) if "local_infile" in global_vars else None
    load_available = local_infile is True
    if local_infile is None:
        why = "The server's local_infile setting could not be read, so LOAD DATA LOCAL is not offered."
    elif local_infile:
        why = "The server has local_infile ON, so LOAD DATA LOCAL can be used."
    elif engine == "mariadb":
        why = ("The server has local_infile OFF (MariaDB's default is ON, so it was turned off), so it refuses LOAD "
               "DATA LOCAL. myflames never changes it. A DBA can run SET GLOBAL local_infile = ON (it takes effect at "
               "once) and add local_infile = ON under [mysqld] in the option file so it stays after a restart "
               "(MariaDB has no SET PERSIST). INSERT batches work either way.")
    elif _mysql_before_80(version):
        # MySQL 5.7 defaults local_infile to ON (mysql-5.7.44 sql/sys_vars.cc
        # Sys_local_infile DEFAULT(TRUE); 8.0 changed it to OFF) and has no SET PERSIST.
        why = ("The server has local_infile OFF, so it refuses LOAD DATA LOCAL. myflames never changes it. A DBA can "
               "run SET GLOBAL local_infile = ON (it takes effect at once) and add local_infile = ON under [mysqld] "
               "in the option file so it stays after a restart (MySQL 5.7 has no SET PERSIST). INSERT batches work "
               "either way.")
    else:
        why = ("The server has local_infile OFF (MySQL's default), so it refuses LOAD DATA LOCAL. myflames never "
               "changes it; a DBA can turn it on with SET PERSIST local_infile = ON. INSERT batches work either way.")
    if method == "load_data" and not load_available:
        blockers.append(why)
    if method == "load_data":
        if limits_files:
            notes.append("The client may send only files from myflames' private folder of checked copies "
                         "(--load-data-local-dir), whatever the server asks for.")
        else:
            warnings.append("This client (MariaDB's) has no --load-data-local-dir, so during the import session it "
                            "would send any file the server asks for. myflames sends only its own LOAD DATA "
                            "statement; use INSERT batches if you do not trust this server.")
    answers = _privilege_answers(actor, target["mode"], target["schema"], target["table"])
    for answer in answers:
        if answer["state"] == "denied":
            blockers.append(answer["reason"])
    if decision["action"] == "refuse":
        blockers.append(decision["text"])
    read_only = str(variables.get("read_only") or "").upper()
    if _on(variables.get("super_read_only")):
        warnings.append("The server has super_read_only ON, so it refuses every write, from every account.")
    elif read_only in ("NO_LOCK_NO_ADMIN",):
        warnings.append("The server has read_only = NO_LOCK_NO_ADMIN, so it refuses every write, from every account.")
    elif _on(read_only) or read_only == "NO_LOCK":
        warnings.append("The server has read_only %s, so it refuses the import unless this account has %s." % (
            "ON" if _on(read_only) else "= NO_LOCK",
            "READ ONLY ADMIN (or SUPER before MariaDB 10.11)" if engine == "mariadb" else
            # MySQL 5.7 has no CONNECTION_ADMIN (a dynamic privilege from 8.0).
            "SUPER" if _mysql_before_80(version) else "CONNECTION_ADMIN or SUPER"))
    triggers = None
    if target["mode"] == "existing":
        model = actor.get("model", actor)
        trigger_view = privileges.has(model, "TRIGGER", level="table", database=target["schema"],
                                      table=target["table"])
        if engine == "mariadb" and trigger_view is not True and any(
                privileges.has(model, p, level="table", database=target["schema"], table=target["table"]) is True
                for p in ("INSERT", "UPDATE", "DELETE")):
            # MariaDB also lists a table's triggers to an account with INSERT,
            # UPDATE or DELETE on it (sql/sql_show.cc get_schema_triggers_record).
            trigger_view = True
        if meta["triggers"] or trigger_view is True:
            triggers = len(meta["triggers"])
        else:
            # information_schema.TRIGGERS lists only triggers of tables the
            # account has TRIGGER on (mysql-server
            # sql/dd/impl/system_views/triggers.cc CAN_ACCESS_TRIGGER), so none
            # seen proves nothing.
            warnings.append("This account may not see the table's triggers (information_schema shows them only with "
                            "the TRIGGER privilege). Any INSERT trigger runs for every row.")
    else:
        triggers = 0
    columns_public = None
    if target["mode"] == "existing":
        columns_public = [c.public() for c in meta["columns"]]
        if meta["triggers"]:
            warnings.append("%d INSERT trigger%s run%s for every row: %s." % (
                len(meta["triggers"]), "" if len(meta["triggers"]) == 1 else "s",
                "s" if len(meta["triggers"]) == 1 else "", ", ".join(meta["triggers"][:5])))
        if meta["foreign_keys"]:
            warnings.append("Foreign keys are checked for every row: a row whose parent row is missing fails.")
        if transactional is not True:
            warnings.append("%s keeps every statement's rows at once; a failed statement can leave part of its rows."
                            % (table_engine or "This engine"))
    else:
        columns_public = [{"name": c.name, "type": ci.render_type(c), "nullable": c.nullable, "default": None,
                           "extra": ""} for c in target["new_columns"]]
    if method == "load_data":
        warnings.append("LOAD DATA LOCAL turns data errors into warnings and skips duplicate keys (the server treats "
                        "LOCAL as IGNORE). myflames checks every value first, loads a checked copy of the rows and "
                        "compares the row count and warnings after each statement; any difference rolls back that "
                        "statement's transaction. Stop during a LOAD DATA statement can take as long as one chunk: "
                        "the server reads the rest of the chunk before it answers.")
    drop_hint = None
    create_sql = None
    if target["mode"] == "new":
        create_sql = ci.create_table_sql(target["schema"], target["table"], target["new_columns"], target["add_id_key"])
        drop_hint = "DROP TABLE %s.%s;" % (ci.ident(target["schema"]), ci.ident(target["table"]))
        warnings.append("CREATE TABLE commits on its own. If the rows then fail or you stop the import, the new table "
                        "stays%s. myflames never drops it; the result shows the DROP TABLE statement to run yourself."
                        % (", empty" if transaction["mode"] == "all" else " with the rows committed so far"))
    if any(m.column.data_type == "timestamp" for m in mapped):
        notes.append("TIMESTAMP values are read in the session time zone (time_zone %s%s)." % (
            variables.get("time_zone") or "unknown",
            ", the server's %s" % variables["system_time_zone"] if variables.get("time_zone") == "SYSTEM"
            and variables.get("system_time_zone") else ""))
    if any(m.column.binary for m in mapped):
        notes.append("Binary columns receive the text's UTF-8 bytes; text such as 0x41 is stored as those characters.")
    if any(c.charset not in (None, "utf8mb4", "utf8mb3", "utf8", "latin1", "ascii", "binary")
           for c in (m.column for m in mapped) if c.textual):
        notes.append("Some columns use a character set myflames does not check; the server refuses a character it "
                     "cannot store.")
    if "EMPTY_STRING_IS_NULL" in str(variables.get("sql_mode") or "").upper().split(","):
        notes.append("The server's sql_mode has EMPTY_STRING_IS_NULL; the import session turns it off, so an empty "
                     "string stays an empty string.")
    notes.append("INSERT batches run with STRICT_ALL_TABLES added to the session's sql_mode (LOAD DATA LOCAL uses "
                 "IGNORE handling instead), and the session waits at most 5 s for a lock (lock_wait_timeout and "
                 "innodb_lock_wait_timeout).")
    # Every row, checked as apply will send it.
    packet = _packet_limit(variables)
    budget = min(ci.INSERT_BYTES, packet)
    check = _check_rows(entry, fmt, mapped, width, target, method, transaction, on_error, backslash, packet, budget,
                        progress)
    if check["rows"]["invalid"] and on_error == "stop":
        blockers.append("%s row%s %s problems. Fix the file or the column types, or choose Skip and report."
                        % (ci.number_text(check["rows"]["invalid"]), "" if check["rows"]["invalid"] == 1 else "s",
                           "has" if check["rows"]["invalid"] == 1 else "have"))
    if not check["rows"]["valid"]:
        blockers.append("No row can be imported.")
    if check.get("fatal"):
        blockers.append(check["fatal"])
    if check.get("header_problem"):
        blockers.append(check["header_problem"])
    if transaction["mode"] == "all":
        warnings.extend(_size_caveats(variables, table_engine, check["rows"]["valid"], check["bytes"]))
    statements_total = check["batches"]
    data_summary = _summary(method, target["schema"], target["table"], mapped, check["rows"]["valid"],
                            statements_total, entry["name"])
    chunk_dir = None
    if method == "load_data" and not blockers:
        chunk_dir = chunk_folder()
    targets, sets = ci.load_targets([m.column for m in mapped])
    data_sample = check["sample"]
    if method == "load_data":
        sample_path = os.path.join(chunk_dir or os.path.join(tempfile.gettempdir(), CHUNK_PREFIX + "..."),
                                   "chunk-000001.tsv")
        try:
            data_sample = ci.load_data_sql(sample_path, target["schema"], target["table"], targets, sets,
                                           backslash_escapes=backslash)
        except ValueError as exc:
            blockers.append(str(exc))
            data_sample = None
    if data_sample and len(data_sample) > 4000:
        data_sample = data_sample[:3999] + "…"
    commits = check["commits"]
    # The Audit log's Statements column shows the import itself; the session
    # settings go with the target.
    audit_statements = ([create_sql] if create_sql else []) + [
        data_summary, ci.TAG + ("COMMIT" if transaction["mode"] == "all" else
                                "COMMIT after every %s rows" % ci.number_text(transaction["every"]))]
    policy = {"action": decision["action"], "tone": decision["tone"], "title": decision["title"],
              "text": decision["text"], "connection_type": decision["connection_type"],
              "read_only": decision["read_only"]}
    public = {
        "schema_version": PLAN_SCHEMA, "token": None, "ready": not blockers, "blockers": blockers,
        "expires_in": PLAN_TTL,
        "file": {"file_id": entry["file_id"], "name": entry["name"], "size": check["file"]["size"],
                 "modified": _iso(check["file"]["mtime_ns"] / 1e9), "sha256": check["file"]["sha256"]},
        "format": fmt,
        "connection": {"name": label, "host": options.get("host"), "port": options.get("port"),
                       "user": options.get("user"), "type": safety["type"], "read_only": safety["read_only"],
                       "engine": engine, "server_version": version, "server_product": product},
        "target": {"mode": target["mode"], "schema": target["schema"], "table": target["table"],
                   "exists": bool(meta.get("exists")), "engine": table_engine or None,
                   "transactional": transactional, "columns": columns_public,
                   "rows_estimate": meta.get("rows_estimate"), "triggers": triggers,
                   "foreign_keys": meta.get("foreign_keys") or 0, "check_constraints": len(meta.get("checks") or []),
                   "primary_key": meta.get("primary_key") or (["id"] if target.get("add_id_key") else [])},
        "mapping": [{"csv_index": m.index, "csv_header": check["header"][m.index] if m.index < len(check["header"]) else "",
                     "column": m.column.name, "type": ci.render_type(m.column) if target["mode"] == "new"
                     else m.column.column_type} for m in mapped],
        "statements": {"create": create_sql, "data_sample": data_sample, "data_summary": data_summary,
                       "setup": list(SETUP)},
        "batches": statements_total, "rows": check["rows"], "problems": check["problems"],
        "problems_total": check["problems_total"],
        "method": {"chosen": method, "load_data_available": load_available, "local_infile": local_infile, "why": why,
                   "client_limits_files": limits_files},
        "transaction": {"mode": transaction["mode"], "every": transaction["every"], "commits": commits},
        "on_error": on_error, "privileges": answers, "policy": policy, "warnings": warnings, "notes": notes,
        "drop_hint": drop_hint}
    if blockers:
        if chunk_dir:
            shutil.rmtree(chunk_dir, ignore_errors=True)
        return public
    details = {"file": check["file"], "file_id": entry["file_id"], "format": fmt,
               "target": {"mode": target["mode"], "schema": target["schema"], "table": target["table"],
                          "metadata": metadata_fingerprint(meta), "add_id_key": target["add_id_key"],
                          "new_columns": [[c.name, ci.render_type(c), c.nullable] for c in target["new_columns"]]},
               "mapping": [[m.index, m.column.name] for m in mapped], "method": method, "transaction": transaction,
               "on_error": on_error, "backslash_escapes": backslash, "packet": packet, "budget": budget,
               "statements": {"create": create_sql, "data_summary": data_summary, "chunk_dir": chunk_dir},
               "rows": check["rows"], "engine": engine, "server_version": version,
               "client_binary": full["binary"], "client_limits_files": limits_files,
               "exact_names": _exact_names(variables)}
    token = imports.tokens.issue(OPERATION, identity, details)
    internal = {"entry": dict(entry), "fmt": fmt, "target": target, "mapped": mapped, "width": width,
                "method": method, "transaction": transaction, "on_error": on_error, "backslash": backslash,
                "client_binary": full["binary"], "client_limits_files": limits_files,
                "packet": packet, "budget": budget, "create": create_sql, "chunk_dir": chunk_dir,
                "targets": targets, "sets": sets, "meta_fp": metadata_fingerprint(meta),
                "transactional": transactional, "table_engine": table_engine, "rows_estimate": meta.get("rows_estimate"),
                "exact_names": _exact_names(variables),
                "audit_statements": audit_statements, "drop_hint": drop_hint, "check": check,
                "target_public": {"schema": target["schema"], "table": target["table"], "file": entry["name"],
                                  "sha256": check["file"]["sha256"], "rows": check["rows"]["valid"], "method": method,
                                  "transaction": transaction, "mode": target["mode"],
                                  "session_setup": [ci.TAG + s for s in SETUP]}}
    imports.add_plan(PlanRecord(token, identity, details, internal, public))
    public["token"] = token
    return public


def _size_caveats(variables, table_engine, rows, data_bytes):
    """Limits that can stop one large all-or-nothing transaction, when they apply."""
    out = []

    def number(name):
        try:
            return int(str(variables.get(name)).strip())
        except (TypeError, ValueError):
            return None
    locks = number("rocksdb_max_row_locks")
    if table_engine.lower() == "rocksdb" and locks and rows > locks // 2:
        out.append("MyRocks locks every row of a transaction, at most rocksdb_max_row_locks (%s); this import writes "
                   "%s rows in one transaction and may fail. Choose Commit every N rows below that."
                   % (ci.number_text(locks), ci.number_text(rows)))
    limit = number("group_replication_transaction_size_limit")
    if (variables.get("group_replication_group_name") or "").strip() and limit and data_bytes > limit // 2:
        out.append("In Group Replication a transaction larger than group_replication_transaction_size_limit (%s "
                   "bytes) is refused; this import sends about %s bytes in one transaction. Choose Commit every N "
                   "rows." % (ci.number_text(limit), ci.number_text(data_bytes)))
    size = number("wsrep_max_ws_size")
    if _on(variables.get("wsrep_on")) and size and data_bytes > size // 2:
        out.append("Galera refuses a write set larger than wsrep_max_ws_size (%s bytes); this import sends about %s "
                   "bytes in one transaction. Choose Commit every N rows." % (ci.number_text(size),
                                                                             ci.number_text(data_bytes)))
    return out


def _check_rows(entry, fmt, mapped, width, target, method, transaction, on_error, backslash, packet, budget, progress):
    """Read every row as apply will send it: counts, problems, statements."""
    digest = hashlib.sha256()
    info = os.stat(entry["path"])
    rows = {"total": 0, "valid": 0, "invalid": 0, "skipped": 0}
    problems, problems_total = [], 0
    every = transaction["every"] if transaction["mode"] == "every" else None
    prefix = ci.insert_prefix(target["schema"], target["table"], [m.column.name for m in mapped])
    batches = ci.Batches(prefix, ci.MAX_ROWS_PER_INSERT, budget, every)
    chunk_rows = every or LOAD_CHUNK_ROWS
    statements, sample, fatal, data_bytes = 0, None, None, 0
    header = {}
    load_rows, load_bytes = 0, 0
    progress.set(phase="checking", rows_checked=0)
    with open(entry["path"], "rb") as raw:
        records = ci.Records(raw, fmt, digest)
        try:
            for line, items, row_problems in converted_rows(ci.split_header(records, fmt, header), fmt, mapped,
                                                            width):
                # A superseded check stops at once, whatever the rows' size.
                if progress.stop.is_set():
                    raise ValueError("The check was stopped. Nothing was sent.")
                rows["total"] += 1
                if rows["total"] % 2000 == 0:
                    progress.set(rows_checked=rows["total"])
                if row_problems:
                    rows["invalid"] += 1
                    problems_total += len(row_problems)
                    for problem in row_problems:
                        if len(problems) < ci.PROBLEM_LIMIT:
                            problems.append(problem)
                    continue
                if method == "insert":
                    text = ci.row_sql(items, backslash)
                    size = len(text.encode("utf-8"))
                    if len(prefix.encode("utf-8")) + size > packet:
                        rows["invalid"] += 1
                        problems_total += 1
                        if len(problems) < ci.PROBLEM_LIMIT:
                            problems.append({"line": line, "column": None, "value": None,
                                             "message": "This row is larger than the server's max_allowed_packet "
                                                        "allows in one statement."})
                        continue
                    data_bytes += size
                    for batch in batches.add(text, line):
                        statements += 1
                        if sample is None:
                            sample = batch["sql"]
                else:
                    size = len(ci.tsv_row(items).encode("utf-8"))
                    data_bytes += size
                    if load_rows and (load_rows >= chunk_rows or load_bytes + size > LOAD_CHUNK_BYTES):
                        statements += 1
                        load_rows, load_bytes = 0, 0
                    load_rows += 1
                    load_bytes += size
                rows["valid"] += 1
        except ci.ReadProblem as exc:
            fatal = str(exc)
            problems_total += 1
            problems.append({"line": exc.line, "column": None, "value": None, "message": str(exc)})
        if fatal is None:
            records.drain()
    if method == "insert":
        for batch in batches.finish():
            statements += 1
            if sample is None:
                sample = batch["sql"]
        commits = batches.commits if every else (1 if rows["valid"] else 0)
    else:
        if load_rows:
            statements += 1
        commits = statements if every else (1 if rows["valid"] else 0)
    if on_error == "skip":
        rows["skipped"] = rows["invalid"]
    header_problem = None
    if header.get("problem"):
        header_problem = ("Line %d, the header row, cannot be read (%s). Fix it, or turn off Header row."
                          % (header["line"], header["problem"].rstrip(".")))
        problems_total += 1
        problems.insert(0, {"line": header["line"], "column": None, "value": None, "message": header_problem})
        del problems[ci.PROBLEM_LIMIT:]
    progress.set(rows_checked=rows["total"], rows_total=rows["valid"])
    return {"rows": rows, "problems": problems, "problems_total": problems_total, "batches": statements,
            "sample": sample, "commits": commits, "header": header.get("fields") or [], "fatal": fatal,
            "header_problem": header_problem, "bytes": data_bytes,
            "file": {"name": entry["name"], "size": info.st_size, "mtime_ns": info.st_mtime_ns,
                     "sha256": digest.hexdigest()}}


# ---- apply -------------------------------------------------------------------------------------

class _Stop(Exception):
    def __init__(self, message="Stopped."):
        Exception.__init__(self, message)
        self.message = message


def _sentence(text):
    """Server text as a sentence: a period added unless it ends with one."""
    text = (text or "").strip()
    return text if not text or text[-1] in ".!?" else text + "."


class _Fail(Exception):
    """A failure after sending started. *message* may quote the server (row
    values included) and goes to the result only; *audit* is the value-free
    sentence the audit record keeps (default: the message when the server
    said nothing, else the error number)."""

    def __init__(self, line, message, error=None, partial=False, audit=None, first_line=None, last_line=None):
        Exception.__init__(self, message)
        self.line = line
        self.message = message
        self.error = error
        self.partial = partial
        self.first_line = first_line if first_line is not None else line
        self.last_line = last_line if last_line is not None else line
        if audit is None:
            audit = message if not error else "The server refused a statement (error %s)." % error.get("code")
        self.audit = audit


class _Unknown(Exception):
    def __init__(self, message):
        Exception.__init__(self, message)
        self.message = message


class _Refused(ValueError):
    def __init__(self, message, outcome="refused"):
        ValueError.__init__(self, message)
        self.outcome = outcome


def apply(payload, workspace, fill_password):
    imports = registry(workspace)
    progress = imports.start_progress(payload.get("progress_id"), "apply")
    options, identity, safety, label = ed._connection_facts(payload, workspace.store)
    decision = dbops.decide("risky", safety["type"], safety["read_only"], label)
    audit = dbops.AuditTrail(workspace.store, OPERATION, "ui",
                             connection={k: options.get(k) for k in ("host", "port", "user", "ssh_host", "ssh_user",
                                                                     "ssh_port")},
                             safety=safety, policy=dbops.policy_summary(decision))
    try:
        record = imports.plan(payload.get("token"))
    except ValueError as exc:
        # An expired, used or evicted review: nothing to send, but the attempt
        # is recorded like any other refusal.
        audit.outcome([], "refused", outcome="expired", message=str(exc))
        progress.set(phase="finished")
        raise
    internal = record.internal
    statements = internal["audit_statements"]
    try:
        if decision["action"] == "refuse":
            raise _Refused(decision["text"], "read_only")
        try:
            imports.tokens.check(payload.get("token"), OPERATION, identity, record.details)
        except ValueError:
            raise _Refused("The connection or its write policy changed after the review, so nothing was sent. "
                           "Review the import again.", "changed")
        try:
            imports.slot()
        except ValueError as exc:
            raise _Refused(str(exc), "busy")
        if not imports.claim(record):
            imports.release()
            raise _Refused("This review is already being applied. Nothing more was sent.", "changed")
    except _Refused as exc:
        audit.outcome(statements, "refused", outcome=exc.outcome, message=str(exc), target=internal["target_public"])
        progress.set(phase="finished")
        raise ValueError(str(exc))
    except ValueError:
        progress.set(phase="finished")
        raise
    try:
        return _Import(record, payload, workspace, fill_password, options, identity, safety, label, audit,
                       progress, imports).run()
    finally:
        imports.release()
        # One apply per plan (claim): its checked-copy folder goes with it.
        internal["in_use"] = False
        imports.drop_plan(record.token)
        progress.set(phase="finished")


class _Import(object):
    def __init__(self, record, payload, workspace, fill_password, options, identity, safety, label, audit, progress,
                 imports):
        self.record = record
        self.plan = record.internal
        self.payload = payload
        self.workspace = workspace
        self.fill_password = fill_password
        self.options = options
        self.identity = identity
        self.safety = safety
        self.label = label
        self.audit = audit
        self.progress = progress
        self.imports = imports
        self.session = None
        self.full = None
        self.password = None
        self.started = time.monotonic()
        self.sent = 0
        self.committed = 0
        self.chunk_sent = 0
        self.skipped = 0
        self.problems = []
        self.problems_total = 0
        self.warnings = []
        self.warning_count = 0
        self.in_tx = False
        self.table_created = False
        self.last_committed_line = None
        self.first_uncommitted_line = None
        self.batches_done = 0
        self.raw = None
        self.prefix = None
        self.owns = False
        self.last_sent_line = None
        self.create_unknown = False
        self.committed_known_partial = False
        self.audit_reason = None

    # -- before sending -----------------------------------------------------------------------

    def _refuse(self, message, outcome="refused"):
        raise _Refused(message, outcome)

    def _policy(self):
        _options, identity, safety, label = ed._connection_facts(self.payload, self.workspace.store)
        if identity != self.identity or safety["read_only"]:
            self._refuse("The connection or its write policy changed. Nothing was sent.", "policy_changed")

    def _preflight(self):
        plan = self.plan
        entry = plan["entry"]
        file = self.record.details["file"]
        self.progress.set(phase="checking")
        try:
            info = os.stat(entry["path"])
        except OSError:
            self._refuse("The file %s is gone. Nothing was sent." % entry["name"], "file_changed")
        if info.st_size != file["size"] or info.st_mtime_ns != file["mtime_ns"]:
            self._refuse("The file %s changed after the review. Review the import again; nothing was sent."
                         % entry["name"], "file_changed")
        digest = hashlib.sha256()
        try:
            self.raw = open(entry["path"], "rb")
            while True:
                if self.progress.stop.is_set():
                    self._refuse("Stopped before anything was sent.", "stopped")
                data = self.raw.read(1 << 20)
                if not data:
                    break
                digest.update(data)
        except OSError as exc:
            self._refuse("The file %s cannot be read now (%s). Nothing was sent." % (
                entry["name"], exc.strerror or exc), "file_changed")
        if digest.hexdigest() != file["sha256"]:
            self._refuse("The file %s changed after the review. Review the import again; nothing was sent."
                         % entry["name"], "file_changed")
        self.raw.seek(0)
        filled = self.fill_password(self.payload)
        self.full = dict(connection_options(filled.get("connection")))
        self.full.pop("database", None)
        self.password = self.full.get("password")
        self.audit.password = self.password
        try:
            binary = client_binary(self.full)
            self.full["binary"] = binary
            flags, limits_files = (load_flags(self.full, plan["chunk_dir"], binary=binary)
                                   if plan["method"] == "load_data" else ((), None))
            # Refuse before connecting: even metadata reads must not open a
            # session with a less restrictive LOCAL policy than was reviewed.
            for reviewed in (self.record.details, plan):
                if (binary != reviewed["client_binary"] or
                        limits_files != reviewed["client_limits_files"]):
                    self._refuse("The database client or its local-file policy changed after the review. "
                                 "Review the import again; nothing was sent.", "changed")
            self.session = _open_session(self.full, self.identity, self.safety, self.label,
                                         load_dir=plan["chunk_dir"] if plan["method"] == "load_data" else None,
                                         client_flags=flags)
        except ConnectorError as exc:
            self._refuse("Could not open the import session: %s. Nothing was sent." % _redact(exc, self.password),
                         "not_connected")
        session = self.session
        self.audit.server(session.engine, session.server_version)
        try:
            meta = read_metadata(session, plan["target"]["schema"], plan["target"]["table"],
                                 exact=_exact_names(_variables(session, ["lower_case_table_names"])))
            global_vars = _variables(session, ["local_infile"], "GLOBAL") if plan["method"] == "load_data" else {}
        except (ConnectorError, ValueError) as exc:
            self._refuse("Could not read the target again: %s. Nothing was sent." % _redact(exc, self.password),
                         "unverified")
        if metadata_fingerprint(meta) != plan["meta_fp"]:
            if plan["target"]["mode"] == "new":
                self._refuse("Table %s.%s exists now. Nothing was sent." % (plan["target"]["schema"],
                                                                           plan["target"]["table"]), "changed")
            self._refuse("The table's definition changed after the review (columns, engine, keys, triggers or "
                         "checks). Review the import again; nothing was sent.", "changed")
        if plan["target"]["mode"] == "new" and not meta.get("schema_exists"):
            self._refuse("Database %s is gone. Nothing was sent." % plan["target"]["schema"], "changed")
        if session.backslash_escapes != plan["backslash"]:
            self._refuse("The server's sql_mode changed (NO_BACKSLASH_ESCAPES) after the review. Review the import "
                         "again; nothing was sent.", "changed")
        if plan["method"] == "load_data" and not _on(global_vars.get("local_infile")):
            self._refuse("The server has local_infile OFF now, so LOAD DATA LOCAL would be refused. Nothing was sent.",
                         "changed")
        actor = _actor(session)
        for answer in _privilege_answers(actor, plan["target"]["mode"], plan["target"]["schema"],
                                         plan["target"]["table"]):
            if answer["state"] == "denied":
                self._refuse(answer["reason"] + " Nothing was sent.", "denied")
        self._policy()
        try:
            self.imports.tokens.check(self.payload.get("token"), OPERATION, self.identity, self.record.details,
                                      consume=True)
            self.owns = True
        except ValueError:
            self._refuse("This review was already used. Nothing was sent.", "changed")
        try:
            audit_id = self.audit.attempt(plan["audit_statements"], target=plan["target_public"],
                                          verified=["file sha256", "table metadata", "write policy", "privileges"])
        except Exception as exc:
            self._refuse("Could not write the audit record (%s), so nothing was sent." % exc, "audit_failed")
        return audit_id

    # -- sending -------------------------------------------------------------------------------

    def _exec(self, sql, timeout=STATEMENT_TIMEOUT, killable=False, data=False):
        if data:
            self.progress.set(in_flight=True)
        try:
            step, error = _send(self.session, sql, timeout, self.progress if killable else None,
                                self.full if killable else None)
        finally:
            if data:
                self.progress.set(in_flight=False)
        if isinstance(error, dict) and error.get("code") in ed.LOST_CONNECTION_CODES:
            # Whoever printed it, a lost connection means the statement may
            # have run: unknown, and the session is over.
            if not self.session.ended:
                self.session.end("connection_lost", ed.SESSION_ENDED_TEXT)
            return step, "unknown"
        return step, error

    def _control(self, sql, phase):
        """START TRANSACTION, COMMIT or ROLLBACK; raises on an error."""
        self.progress.set(phase=phase)
        step, error = self._exec(ci.TAG + sql)
        return error

    def _begin(self):
        error = self._control("START TRANSACTION", "sending")
        if error == "unknown":
            raise _Fail(self.first_uncommitted_line, "The connection ended before the transaction started.")
        if error:
            raise _Fail(self.first_uncommitted_line, "START TRANSACTION failed: %s" % _sentence(error.get("message")),
                        error, audit="START TRANSACTION failed (error %s)." % error.get("code"))
        self.in_tx = True
        self.chunk_sent = 0

    def _commit(self, last_line):
        # A Stop that arrived while the last statement ran still wins: the
        # rows since the last commit are rolled back, never committed.
        self._check_stop()
        file = self.record.details["file"]
        info = os.fstat(self.raw.fileno())
        if info.st_size != file["size"] or info.st_mtime_ns != file["mtime_ns"]:
            raise _Fail(self.first_uncommitted_line, "The file changed while it was being imported, so the rows "
                                                     "since the last commit were rolled back.")
        error = self._control("COMMIT", "committing")
        if error == "unknown":
            raise _Unknown("COMMIT got no reply, so whether the %s rows since the last commit were kept is unknown. "
                           "Check the table before you do anything else; nothing will be sent again."
                           % ci.number_text(self.chunk_sent))
        if error:
            raise _Fail(self.first_uncommitted_line, "COMMIT failed: %s" % _sentence(error.get("message")), error,
                        audit="COMMIT failed (error %s)." % error.get("code"))
        self.in_tx = False
        self.committed += self.chunk_sent
        self.chunk_sent = 0
        self.last_committed_line = last_line
        self.first_uncommitted_line = None
        self.progress.set(rows_committed=self.committed, phase="sending")

    def _rollback(self):
        if not self.in_tx or self.session is None or self.session.ended:
            self.in_tx = False
            return
        self.progress.set(phase="rolling_back")
        self._control("ROLLBACK", "rolling_back")
        self.in_tx = False
        self.chunk_sent = 0

    def _check_stop(self):
        if self.progress.stop.is_set():
            raise _Stop()

    def _note_problem(self, problem):
        self.problems_total += 1
        if len(self.problems) < ci.PROBLEM_LIMIT:
            self.problems.append(problem)

    def _warnings(self, step):
        entry = _ok_entry(step)
        count = entry.get("warnings") if entry else 0
        if not count:
            return []
        self.warning_count += count
        try:
            rows = _query(self.session, "SHOW WARNINGS LIMIT 20")
        except (ConnectorError, ValueError):
            return []
        items = [{"level": r.get("Level"), "code": int(r["Code"]) if str(r.get("Code") or "").isdigit() else None,
                  "message": r.get("Message")} for r in rows]
        for item in items:
            if len(self.warnings) < 20:
                self.warnings.append(item)
        return items

    def _statement_failed(self, error, lines, values=None):
        """The _Fail for an INSERT the server refused. Its row comes from the
        server's "at row N" when it gives one; else, on a transactional table
        and for a row error (a duplicate key never names its row), the rows
        are sent again one at a time in the same transaction until one is
        refused: that is the line. Nothing of it is kept (the failure rolls
        back the transaction since the last commit). On a non-transactional
        table the statement's range is reported."""
        message = _sentence(error.get("message") or "The server refused the statement.")
        code = error.get("code")
        first, last = lines[0], lines[-1]
        match = _AT_ROW.search(error.get("message") or "")
        line, exact = first, len(lines) == 1
        if match and 0 < int(match.group(1)) <= len(lines):
            line, exact = lines[int(match.group(1)) - 1], True
            if self.plan["transactional"] is not True:
                # Strict mode on a non-transactional table keeps the rows before
                # the bad one (they cannot be rolled back).
                kept = int(match.group(1)) - 1
                self.sent += kept
                self.committed = self.sent
                self.committed_known_partial = True
                if kept:
                    self.last_committed_line = lines[kept - 1]
                self.progress.set(rows_sent=self.sent, rows_committed=self.committed)
        elif values and len(lines) > 1 and self.plan["transactional"] is True and code in ROW_ERRORS:
            found = self._find_row(values, lines)
            if found is not None:
                line, error = found
                message = _sentence(error.get("message") or message)
                code = error.get("code")
                exact = True
        if exact and len(lines) > 1:
            text = "The server refused line %d (in the statement with lines %d to %d): %s" % (line, first, last,
                                                                                            message)
        elif exact:
            text = "The server refused line %d: %s" % (line, message)
        else:
            text = "The server refused the statement with lines %d to %d: %s" % (first, last, message)
        audit = "The server refused %s (error %s)." % (
            "line %d" % line if exact else "the statement with lines %d to %d" % (first, last), code)
        return _Fail(line, text, error, partial=self.plan["transactional"] is not True, audit=audit,
                     first_line=first, last_line=last)

    def _find_row(self, values, lines):
        """Send the rows one at a time (same transaction, rolled back after)
        until the server refuses one: ``(line, error)``, or None."""
        for value, line in zip(values, lines):
            self._check_stop()
            step, error = self._exec(self.prefix + value, data=True)
            if error == "unknown":
                self._lost(line, line)
            if error:
                return line, error
        return None

    def _insert_batch(self, batch):
        if self.first_uncommitted_line is None:
            self.first_uncommitted_line = batch["lines"][0]
        self._check_stop()
        if not self.in_tx:
            self._begin()
        self.last_sent_line = batch["lines"][-1]
        self.progress.set(phase="sending")
        step, error = self._exec(batch["sql"], data=True)
        if error == "unknown":
            self._lost(batch["lines"][0], batch["lines"][-1])
        if error:
            if self.plan["on_error"] == "skip" and error.get("code") in ROW_ERRORS and self.plan["transactional"] is True:
                prefix = self.prefix
                for value, line in zip(batch["values"], batch["lines"]):
                    self._check_stop()
                    step, error = self._exec(prefix + value, data=True)
                    if error == "unknown":
                        self._lost(line, line)
                    if error:
                        if error.get("code") not in ROW_ERRORS:
                            raise self._statement_failed(error, [line])
                        self.skipped += 1
                        self._note_problem({"line": line, "column": None, "value": None,
                                            "message": "The server refused this row: %s"
                                                       % _sentence(error.get("message"))})
                        continue
                    self._sent(1)
                    self._warnings(step)
            else:
                raise self._statement_failed(error, batch["lines"], batch["values"])
        else:
            self._sent(batch["rows"])
            self._warnings(step)
        self.batches_done += 1
        self.progress.set(batches_done=self.batches_done)
        if self.plan["transactional"] is not True:
            self.committed = self.sent
            self.chunk_sent = 0
            self.last_committed_line = batch["lines"][-1]
            self.first_uncommitted_line = None
            self.progress.set(rows_committed=self.committed)
        if batch["commit"] and self.plan["transaction"]["mode"] == "every":
            self._commit(batch["lines"][-1])

    def _sent(self, rows):
        self.sent += rows
        self.chunk_sent += rows
        self.progress.set(rows_sent=self.sent)

    def _lost(self, first, last):
        """The connection was lost (or no reply came) during a data statement:
        the outcome is unknown, never retried (db-operations: a lost
        connection after sending is unknown)."""
        where = "line %d" % first if first == last else "lines %d to %d" % (first, last)
        if self.plan["transactional"] is True:
            raise _Unknown("The connection was lost during the statement with %s. The server rolls back a transaction "
                           "that was never committed, so the rows since the last commit should not be in the table, "
                           "but the outcome is unknown: check the table before you do anything else." % where)
        raise _Unknown("The connection was lost during the statement with %s on a non-transactional table, so "
                       "whether its rows were kept is unknown. Check the table before you do anything else." % where)

    def _load_chunk(self, path, rows, lines):
        if self.first_uncommitted_line is None:
            self.first_uncommitted_line = lines[0]
        self._check_stop()
        if not self.in_tx:
            self._begin()
        self.last_sent_line = lines[-1]
        sql = ci.load_data_sql(path, self.plan["target"]["schema"], self.plan["target"]["table"], self.plan["targets"],
                               self.plan["sets"], backslash_escapes=self.plan["backslash"])
        self.progress.set(phase="sending")
        step, error = self._exec(sql, LOAD_TIMEOUT, killable=True, data=True)
        first, last = lines[0], lines[-1]
        kept_anyway = self.plan["transactional"] is not True
        if error == "unknown":
            self._lost(first, last)
        if error:
            if self.progress.stop.is_set() and error.get("code") in ed.INTERRUPTED_CODES and not kept_anyway:
                raise _Stop("Stopped: KILL QUERY ended the LOAD DATA statement.")
            if kept_anyway:
                # A non-transactional table keeps what the statement loaded
                # before it ended; how much is not reported.
                raise _Unknown("LOAD DATA for lines %d to %d ended with an error (%s) on a non-transactional table, "
                               "so some of its rows may be in the table. Check the table before you do anything else."
                               % (first, last, error.get("code")))
            raise _Fail(first, "The server refused LOAD DATA for lines %d to %d: %s" % (
                first, last, _sentence(error.get("message"))), error,
                audit="The server refused LOAD DATA for lines %d to %d (error %s)." % (first, last, error.get("code")),
                first_line=first, last_line=last)
        entry = _ok_entry(step)
        affected = entry.get("affected_rows") if entry else None
        items = self._warnings(step)
        serious = [w for w in items if (w.get("level") or "").lower() != "note"]
        mismatch = bool(serious or (entry and entry.get("warnings") and not items) or affected != rows)
        if mismatch and kept_anyway:
            loaded = affected if isinstance(affected, int) and affected >= 0 else 0
            self._sent(loaded)
            self.committed = self.sent
            self.chunk_sent = 0
            self.last_committed_line = None
            self.progress.set(rows_committed=self.committed)
            codes = sorted(set(w.get("code") for w in serious if w.get("code")))
            raise _Fail(first, "LOAD DATA loaded %s of %s rows for lines %d to %d%s." % (
                                   loaded, rows, first, last,
                                   " with warnings (%s)" % _sentence(serious[0]["message"]).rstrip(".")
                                   if serious else ""),
                        audit="LOAD DATA loaded %s of %s rows for lines %d to %d%s; they stay (non-transactional "
                              "table)." % (loaded, rows, first, last,
                                           " with warnings %s" % ", ".join(str(c) for c in codes) if codes else ""),
                        first_line=first, last_line=last)
        if serious or (entry and entry.get("warnings") and not items):
            codes = sorted(set(w.get("code") for w in serious if w.get("code")))
            raise _Fail(first, "LOAD DATA changed or skipped rows for lines %d to %d (%s), so this statement's "
                               "transaction was rolled back." % (
                                   first, last, _sentence(serious[0]["message"]).rstrip(".") if serious
                                   else "warnings"),
                        audit="LOAD DATA reported warnings for lines %d to %d%s; rolled back." % (
                            first, last, " (%s)" % ", ".join(str(c) for c in codes) if codes else ""),
                        first_line=first, last_line=last)
        if affected != rows:
            raise _Fail(first, "LOAD DATA loaded %s of %s rows for lines %d to %d, so its transaction was rolled "
                               "back." % (affected, rows, first, last), first_line=first, last_line=last)
        self._sent(rows)
        self.batches_done += 1
        self.progress.set(batches_done=self.batches_done)
        if self.plan["transaction"]["mode"] == "every":
            self._commit(last)

    def _rows(self):
        """The rows to send, one behind: the last row comes out only after the
        whole file was read and its SHA-256 matched the review, so the last
        statement and its COMMIT never run on bytes nobody reviewed."""
        plan = self.plan
        digest = hashlib.sha256()
        records = ci.Records(self.raw, plan["fmt"], digest)
        pending = None
        for line, items, problems in converted_rows(ci.split_header(records, plan["fmt"], {}), plan["fmt"],
                                                    plan["mapped"], plan["width"]):
            if problems:
                if plan["on_error"] != "skip":
                    raise _Fail(line, "Line %d has a problem: %s" % (line, problems[0]["message"]))
                self.skipped += 1
                for problem in problems:
                    self._note_problem(problem)
                continue
            if pending is not None:
                yield pending
            pending = (line, items)
        records.drain()
        if digest.hexdigest() != self.record.details["file"]["sha256"]:
            raise _Fail(None, "The file changed while it was being imported, so the rows since the last commit were "
                              "rolled back.")
        if pending is not None:
            yield pending

    def _send_rows(self):
        plan = self.plan
        target = plan["target"]
        every = plan["transaction"]["every"] if plan["transaction"]["mode"] == "every" else None
        if plan["method"] == "insert":
            prefix = self.prefix = ci.insert_prefix(target["schema"], target["table"],
                                                    [m.column.name for m in plan["mapped"]])
            batches = ci.Batches(prefix, ci.MAX_ROWS_PER_INSERT, plan["budget"], every)
            for line, items in self._rows():
                text = ci.row_sql(items, plan["backslash"])
                if len(prefix.encode("utf-8")) + len(text.encode("utf-8")) > plan["packet"]:
                    if plan["on_error"] != "skip":
                        raise _Fail(line, "Line %d is larger than max_allowed_packet allows." % line)
                    self.skipped += 1
                    self._note_problem({"line": line, "column": None, "value": None,
                                        "message": "This row is larger than the server's max_allowed_packet allows "
                                                   "in one statement."})
                    continue
                for batch in batches.add(text, line):
                    self._insert_batch(batch)
            for batch in batches.finish():
                self._insert_batch(batch)
        else:
            chunk_rows = every or LOAD_CHUNK_ROWS
            folder = plan["chunk_dir"]
            number, handle, path, rows, size, lines = 0, None, None, 0, 0, []
            try:
                for line, items in self._rows():
                    text = ci.tsv_row(items)
                    data = text.encode("utf-8")
                    if rows and (rows >= chunk_rows or size + len(data) > LOAD_CHUNK_BYTES):
                        handle.close()
                        handle = None
                        self._load_chunk(path, rows, lines)
                        os.unlink(path)
                        rows, size, lines = 0, 0, []
                    if handle is None:
                        number += 1
                        path = os.path.join(folder, "chunk-%06d.tsv" % number)
                        handle = open(path, "wb")
                        if hasattr(os, "fchmod"):
                            os.fchmod(handle.fileno(), 0o600)
                    handle.write(data)
                    rows += 1
                    size += len(data)
                    lines.append(line)
                if handle is not None:
                    handle.close()
                    handle = None
                    self._load_chunk(path, rows, lines)
                    os.unlink(path)
            finally:
                if handle is not None:
                    handle.close()
        if self.in_tx:
            self._commit(self.last_sent_line)

    def run(self):
        try:
            self._preflight()
        except _Refused as exc:
            self.audit.outcome(self.plan["audit_statements"], "refused", outcome=exc.outcome, message=str(exc),
                               target=self.plan["target_public"])
            self._close()
            raise ValueError(str(exc))
        except BaseException:
            self._close()
            raise
        outcome, message, failed_at, server_error = "done", None, None, None
        self.audit_reason = None
        try:
            try:
                self._setup()
                if self.plan["create"]:
                    self._check_stop()
                    self.progress.set(phase="creating")
                    step, error = self._exec(self.plan["create"])
                    if error == "unknown":
                        self.create_unknown = True
                        raise _Unknown("CREATE TABLE got no reply, so whether table %s.%s exists is unknown. Check the "
                                       "database before you do anything else; if the table is there and empty, you "
                                       "can remove it with the DROP TABLE statement below." % (
                                           ci.ident(self.plan["target"]["schema"]),
                                           ci.ident(self.plan["target"]["table"])))
                    if error:
                        raise _Fail(None, "The server refused CREATE TABLE: %s" % _sentence(error.get("message")),
                                    error, audit="The server refused CREATE TABLE (error %s)." % error.get("code"))
                    self.table_created = True
                self.progress.set(rows_total=self.plan["check"]["rows"]["valid"],
                                  batches_total=self.plan["check"]["batches"])
                self._send_rows()
                message = "Imported %s row%s." % (ci.number_text(self.committed), "" if self.committed == 1 else "s")
                if self.skipped:
                    message += " %s row%s skipped." % (ci.number_text(self.skipped),
                                                       " was" if self.skipped == 1 else "s were")
                self.audit_reason = message
            except _Stop as exc:
                outcome = "stopped"
                self._rollback()
                message = self._kept(exc.message)
                self.audit_reason = message
            except _Fail as exc:
                outcome = "failed"
                if exc.line:
                    failed_at = {"line": exc.line, "first_line": exc.first_line, "last_line": exc.last_line,
                                 "message": exc.message}
                if exc.error and exc.error != "unknown":
                    server_error = {"code": exc.error.get("code"), "message": exc.error.get("message")}
                self._rollback()
                message = self._kept(exc.message)
                self.audit_reason = self._kept(exc.audit)
                if exc.partial and not self.committed_known_partial:
                    extra = (" The table is not transactional: rows of the failed statement before the bad row may "
                             "be in the table.")
                    message += extra
                    self.audit_reason += extra
            except _Unknown as exc:
                outcome = "unknown"
                message = exc.message
                self.audit_reason = exc.message
        except Exception as exc:  # anything after the first statement left: never a 400
            outcome = "unknown" if self.in_tx or self.sent else "failed"
            message = "The import stopped with an error: %s" % _redact(exc, self.password)
            self.audit_reason = "The import stopped with an error (%s)." % type(exc).__name__
        result = self._result(outcome, _redact(message, self.password), failed_at, server_error)
        self._audit_finish(result)
        self._close()
        return result

    def _setup(self):
        """The session settings, then a read that they hold, before any row."""
        for statement in SETUP:
            step, error = self._exec(ci.TAG + statement)
            if error == "unknown":
                raise _Fail(None, "The connection ended while setting up the session. Nothing was imported.")
            if error:
                raise _Fail(None, "The session could not be set up (%s): %s" % (statement, _sentence(error.get(
                    "message"))), error, audit="The session could not be set up (error %s)." % error.get("code"))
        try:
            rows = _query(self.session, "SELECT @@SESSION.sql_mode AS sql_mode")
        except (ConnectorError, ValueError):
            rows = []
        modes = str((rows[0] if rows else {}).get("sql_mode") or "").upper().split(",")
        if "STRICT_ALL_TABLES" not in modes or "EMPTY_STRING_IS_NULL" in modes \
                or ("NO_BACKSLASH_ESCAPES" not in modes) != self.plan["backslash"]:
            raise _Fail(None, "The session's sql_mode is not what the import needs (STRICT_ALL_TABLES on, "
                              "EMPTY_STRING_IS_NULL off, NO_BACKSLASH_ESCAPES as reviewed). Nothing was imported.")

    def _kept(self, text):
        text = _sentence(text)
        if self.plan["transactional"] is not True:
            if self.committed:
                return "%s %s row%s %s in the table (it is not transactional, so nothing was rolled back)." % (
                    text, ci.number_text(self.committed), "" if self.committed == 1 else "s",
                    "is" if self.committed == 1 else "are")
            return "%s No row was imported%s." % (text, ": the new table stays, empty" if self.table_created else "")
        if self.committed:
            return "%s %s row%s were committed before it; the rest was rolled back." % (
                text, ci.number_text(self.committed), "" if self.committed == 1 else "s")
        return "%s Nothing was kept%s." % (text, ": the new table stays, empty" if self.table_created else "")

    def _result(self, outcome, message, failed_at, server_error):
        table_rows = None
        if outcome != "unknown" and self.session is not None and not self.session.ended:
            estimate = self.plan.get("rows_estimate") or 0
            if estimate + self.committed <= COUNT_LIMIT:
                try:
                    rows = _query(self.session, "SELECT COUNT(*) AS n FROM %s.%s" % (
                        ci.ident(self.plan["target"]["schema"]), ci.ident(self.plan["target"]["table"])))
                    table_rows = int(rows[0]["n"]) if rows else None
                except (ConnectorError, ValueError, KeyError, TypeError):
                    table_rows = None
        valid = self.plan["check"]["rows"]["valid"]
        next_line = None
        if outcome != "done":
            next_line = self.first_uncommitted_line if self.first_uncommitted_line else (
                failed_at["first_line"] if failed_at else None)
        drop = self.plan["drop_hint"] if ((self.table_created and not self.committed and outcome in ("failed", "stopped"))
                                          or self.create_unknown) else None
        return {"schema_version": RESULT_SCHEMA, "outcome": outcome, "message": message,
                "rows": {"total": valid + self.plan["check"]["rows"]["invalid"], "sent": self.sent,
                         "committed": self.committed, "skipped": self.skipped},
                "failed_at": failed_at, "next_line": next_line, "last_committed_line": self.last_committed_line,
                "server_error": server_error,
                "warnings": {"count": self.warning_count, "items": self.warnings},
                "problems": self.problems, "problems_total": self.problems_total,
                "duration_ms": int((time.monotonic() - self.started) * 1000), "table_created": self.table_created,
                "table_rows_after": table_rows, "drop_hint": drop, "audit": {"id": self.audit.id},
                "method": self.plan["method"], "transaction": self.plan["transaction"]}

    def _audit_finish(self, result):
        """The audit keeps counts, lines and error numbers, never the server's
        text (it can quote row values: Duplicate entry 'x')."""
        status = {"done": "ok", "stopped": "stopped", "failed": "error", "unknown": "unknown"}[result["outcome"]]
        outcome = {"done": "imported", "stopped": "stopped", "failed": "failed", "unknown": "unknown"}[result["outcome"]]
        error = (result["server_error"] or {}).get("code")
        failed_at = result["failed_at"]
        self.audit.complete(status, outcome=outcome, error=error,
                            message=self.audit_reason or "Outcome %s." % result["outcome"],
                            rows_affected=result["rows"]["committed"],
                            after={"rows": result["rows"],
                                   "failed_at": {k: failed_at[k] for k in ("line", "first_line", "last_line")}
                                   if failed_at else None,
                                   "next_line": result["next_line"], "table_created": result["table_created"],
                                   "table_rows_after": result["table_rows_after"],
                                   "warnings": result["warnings"]["count"], "drop_hint": result["drop_hint"]})

    def _close(self):
        if self.session is not None:
            self.session.end("csv_import_finished", quiet=True)
        if self.raw is not None:
            try:
                self.raw.close()
            except OSError:
                pass
        folder = self.plan.get("chunk_dir")
        if folder and self.owns:
            shutil.rmtree(folder, ignore_errors=True)


# ---- dispatch ------------------------------------------------------------------------------------

def response(path, payload, workspace, fill_password):
    imports = registry(workspace)
    if path == "/api/import/csv/choose":
        from .file_picker import choose_file
        chosen = choose_file()
        if chosen["cancelled"]:
            return {"cancelled": True}
        answer = imports.add_file(chosen["path"])
        # The Files view's "Choose files..." reuses this picker and needs the
        # absolute path to start an upload; the user just chose this file in
        # the native dialog, so handing it back is no new disclosure.
        answer["path"] = chosen["path"]
        return answer
    if path == "/api/import/csv/open-path":
        from .file_picker import check_file
        return imports.add_file(check_file(payload.get("path")))
    if path in ("/api/import/csv/inspect", "/api/import/csv/plan"):
        try:
            if path == "/api/import/csv/inspect":
                return inspect(imports.file(payload.get("file_id")), payload.get("format"))
            return plan(payload, workspace, fill_password)
        except OSError as exc:
            # The file was moved, deleted or made unreadable since it was chosen.
            raise ValueError("The file cannot be read now (%s). Choose it again." % (exc.strerror or exc)) from None
    if path == "/api/import/csv/apply":
        return apply(payload, workspace, fill_password)
    if path == "/api/import/csv/progress":
        progress = imports.get_progress(payload.get("progress_id"))
        if progress is None:
            return {"schema_version": PROGRESS_SCHEMA, "kind": None, "phase": "unknown", "rows_total": None,
                    "rows_checked": 0, "rows_sent": 0, "rows_committed": 0, "batches_done": 0, "batches_total": None,
                    "in_flight": False, "started_at": None, "elapsed_s": None, "stop_requested": False}
        return progress.snapshot()
    if path == "/api/import/csv/stop":
        progress = imports.get_progress(payload.get("progress_id"))
        if progress is None:
            return {"stop_requested": False, "message": "No import or check with this id is running."}
        return {"stop_requested": True, "message": progress.request_stop()}
    raise ValueError("Unknown workspace operation.")
