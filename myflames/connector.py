"""
Live MySQL / MariaDB connector used by the myflames CLI when the user passes
connection flags (``-h``, ``-P``, ``-u``, ``-p``, ``--ssl-*``, ``-D``).

Design notes
------------
The project rule is "stdlib only" — Python has no stdlib MySQL driver. Rather
than reimplementing the wire protocol, this module shells out to the real
``mysql`` or ``mariadb`` CLI binary the user already has on their PATH. This
has three concrete benefits:

1. Authentication plugins (``mysql_native_password``, ``caching_sha2_password``,
   ``auth_gssapi``, etc.) are handled by the real client — we never touch
   handshake code.
2. TLS / SSL verification (``--ssl-mode=VERIFY_IDENTITY``, ``--ssl-ca``, client
   certs) works exactly as it does when the user runs ``mysql`` by hand.
3. Zero third-party dependencies.

Secrets handling
----------------
Passwords MUST NOT appear on argv (visible in ``ps``) or in environment
variables inherited by children. Instead we write a ``--defaults-file``
to a mode-0600 temp file, point the client at it, and unlink it afterwards
(even on exceptions). The same file carries ``ssl-*`` options so there is a
single source of truth.
"""
from contextlib import contextmanager
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .server_info import detect_flavor

# Bound both pipes independently: stderr can be large even for a tiny result.
MAX_STDOUT_BYTES = 64 * 1024 * 1024
MAX_STDERR_BYTES = 1024 * 1024

# Keep client byte encoding independent of the locale. SET NAMES also fixes
# MariaDB 10.11's server-default fallback when a MySQL client negotiates an
# unknown utf8mb4 collation. Apply it on every connection: the server engine
# is not known at first connect, and a binary named mysql may be either client.
# The init command returns no result set and runs before stdin is consumed,
# preserving batch output and the editor's DELIMITER/marker line numbering.
CLIENT_CHARSET_FLAGS = ("--default-character-set=utf8mb4",
                        "--init-command=SET NAMES utf8mb4")



# Client configuration must come only from the reviewed connection. In
# particular, login files are loaded separately even with --defaults-file.
_CLIENT_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL", "LC_CTYPE",
               "TZ", "TMPDIR", "TEMP", "TMP")


@contextmanager
def client_environment():
    """Keep a private, empty home alive for the entire client invocation."""
    with tempfile.TemporaryDirectory(prefix="myflames-client-") as home:
        env = {key: os.environ[key] for key in _CLIENT_ENV if key in os.environ}
        env.update(HOME=home, USERPROFILE=home,
                   MYSQL_TEST_LOGIN_FILE=os.path.join(home, "no-login-file"))
        yield env


def _option_value(value):
    """Encode one MySQL option-file string, including Windows path separators."""
    value = str(value)
    if "\0" in value:
        raise ValueError("An option-file value cannot contain NUL.")
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    for char, escape in (("\n", "\\n"), ("\r", "\\r"), ("\t", "\\t"), ("\b", "\\b")):
        value = value.replace(char, escape)
    return '"' + value + '"'


def _client_text(data):
    """Decode client stdout. On Windows the mysql/mariadb client writes in text
    mode, so every newline arrives as CRLF; turn those back into LF there. On
    other systems a CRLF can only come from the data itself, so it is kept."""
    text = (data or b"").decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n") if os.name == "nt" else text


def _run_bounded(argv, timeout, input=None, env=None):
    """Drain both pipes concurrently on POSIX and Windows with fixed byte caps.

    A separate stdin writer prevents a large script from blocking output
    drainage. On timeout, overflow or interruption, kill and reap the client
    and join every pipe worker before returning to the caller.
    """
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, bufsize=0)
    changed = threading.Event()
    failed = []
    buffers = [bytearray(), bytearray()]
    workers = []

    def drain(pipe, index, limit, label):
        try:
            while True:
                chunk = pipe.read(min(65536, limit - len(buffers[index]) + 1))
                if not chunk:
                    break
                if len(buffers[index]) + len(chunk) > limit:
                    failed.append(ConnectorError("Lost connection to the MySQL client: %s response exceeds "
                                                 "the %d-byte limit; "
                                                 "the client was stopped and the outcome is unknown."
                                                 % (label, limit)))
                    break
                buffers[index].extend(chunk)
        except OSError:
            failed.append(ConnectorError("Lost connection to the MySQL client while reading; "
                                         "the outcome is unknown."))
        finally:
            pipe.close()
            changed.set()

    def send():
        try:
            # FileIO.write can be short (notably on Windows).
            data = memoryview(input)
            while data:
                written = proc.stdin.write(data[:65536])
                if not written:
                    raise BrokenPipeError()
                data = data[written:]
        except BrokenPipeError:
            pass  # The return code/stderr explains an early client exit.
        except OSError:
            failed.append(ConnectorError("Lost connection to the MySQL client while sending SQL; "
                                         "the outcome is unknown."))
        finally:
            proc.stdin.close()
            changed.set()

    try:
        for args in ((proc.stdout, 0, MAX_STDOUT_BYTES, "stdout"),
                     (proc.stderr, 1, MAX_STDERR_BYTES, "stderr")):
            worker = threading.Thread(target=drain, args=args)
            worker.start()
            workers.append(worker)
        if input is not None:
            worker = threading.Thread(target=send)
            worker.start()
            workers.append(worker)
        while True:
            changed.clear()
            if failed:
                raise failed[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            if not any(worker.is_alive() for worker in workers):
                proc.wait(timeout=remaining)
                break
            changed.wait(min(remaining, 0.05))
        return subprocess.CompletedProcess(argv, proc.returncode, bytes(buffers[0]), bytes(buffers[1]))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for worker in workers:
            worker.join()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()

# C0 controls except tab and newline, DEL, and C1 controls (U+0080-U+009F).
_TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def terminal_text(text):
    """Return *text* safe to print on a terminal.

    Text from a server (another session's SQL, SHOW ENGINE INNODB STATUS,
    error messages) can carry ESC, BEL, a carriage return or the 8-bit CSI
    and OSC characters, which a terminal runs as commands: move the cursor,
    rewrite a confirmation prompt, or set the clipboard (OSC 52). Each such
    character becomes a visible ``\\uXXXX`` escape; newline and tab stay.
    JSON from ``json.dumps`` keeps its meaning: it already escapes C0
    characters, and ``\\u007f`` and ``\\u0080``-``\\u009f`` are JSON escapes
    for the same characters, so a JSON parser reads the original values.
    """
    if not text:
        return text
    return _TERMINAL_CONTROLS.sub(lambda m: "\\u%04x" % ord(m.group()), text)


class ConnectorError(RuntimeError):
    """Raised when the CLI binary returns a non-zero exit code or malformed
    output. Read errors preserve stderr for connection diagnostics; write
    errors redact credentials before exposing the client's message."""


class MySQLConnection:
    """A thin wrapper around the ``mysql`` / ``mariadb`` CLI binary.

    Instances are reusable across threads. Each client invocation owns a
    separate defaults-file and removes it as soon as the subprocess exits.
    """

    # Options recognised by every version of the mysql/mariadb CLI that we
    # care about.  Listed here so _build_defaults_file() can whitelist them.
    _SSL_KEYS = ("ssl_mode", "ssl_ca", "ssl_cert", "ssl_key", "ssl_cipher")

    def __init__(
        self,
        host,
        port=3306,
        user=None,
        password=None,
        database=None,
        ssl_mode=None,
        ssl_ca=None,
        ssl_cert=None,
        ssl_key=None,
        ssl_cipher=None,
        binary=None,
        connect_timeout=10,
        query_timeout=30,
    ):
        self.host = host
        self.port = int(port) if port is not None else 3306
        self.user = user
        self.password = password
        self.database = database
        self.ssl_mode = ssl_mode
        self.ssl_ca = ssl_ca
        self.ssl_cert = ssl_cert
        self.ssl_key = ssl_key
        self.ssl_cipher = ssl_cipher
        self.connect_timeout = connect_timeout
        self.query_timeout = query_timeout
        # Binary autodetection happens lazily in _resolve_binary so the
        # constructor can be unit-tested without the binary installed.
        self._binary_override = binary
        self._binary_cached = None
        self._defaults_file_path = None
        self._version_cache = None
        self._version_comment_cache = None
        self._is_mariadb_cache = None

    # ---- binary + defaults-file plumbing --------------------------------

    @staticmethod
    def _which(name):
        """Wrapper around shutil.which so tests can monkeypatch it."""
        return shutil.which(name)

    def _resolve_binary(self):
        """Return the path to the mysql or mariadb client binary.

        Prefers an explicit override, then ``mysql`` (which MariaDB also
        ships under that name on most distros), then ``mariadb``.
        """
        if self._binary_cached:
            return self._binary_cached
        if self._binary_override:
            self._binary_cached = self._binary_override
            return self._binary_cached
        for name in ("mysql", "mariadb"):
            path = self._which(name)
            if path:
                self._binary_cached = path
                return path
        raise ConnectorError(
            "Cannot find 'mysql' or 'mariadb' on PATH. Install the client "
            "binary or pass --mysql-binary /path/to/mysql."
        )

    def _build_defaults_file_content(self):
        """Build the text that goes into the defaults-file.

        Only the ``[client]`` section is used so both ``mysql`` and
        ``mariadb`` read it. Values with a ``#`` or ``=`` inside are rare
        but we quote defensively to avoid the CLI parser tripping.
        """
        lines = ["[client]"]
        if self.host:
            lines.append("host=" + _option_value(self.host))
        if self.port:
            lines.append("port=" + str(self.port))
        if self.user:
            lines.append("user=" + _option_value(self.user))
        if self.password is not None:
            lines.append("password=" + _option_value(self.password))
        for key in self._SSL_KEYS:
            value = getattr(self, key)
            if value:
                lines.append(key.replace("_", "-") + "=" + _option_value(value))
        if self.connect_timeout:
            lines.append("connect-timeout=" + str(int(self.connect_timeout)))
        # Preserve public-key fetching for default/TLS modes, but never
        # request an unauthenticated key when TLS is explicitly disabled.
        if (self.ssl_mode or "").upper() != "DISABLED":
            lines.append("get-server-public-key=ON")
        return "\n".join(lines) + "\n"

    def _ensure_defaults_file(self):
        """Legacy option-file helper; callers own it until :meth:`close`.

        Client invocations use :meth:`_invocation_defaults` instead, so
        close() cannot remove a file being used by another invocation.
        """
        if self._defaults_file_path and os.path.exists(self._defaults_file_path):
            return self._defaults_file_path
        self._defaults_file_path = self._create_defaults_file()
        return self._defaults_file_path

    def _create_defaults_file(self):
        """Write a fresh, private option file without storing shared state."""
        # mkstemp creates the file with mode 0600 on POSIX. Windows has no
        # os.fchmod and ignores the mode; the file sits in the user's own
        # %TEMP% directory, whose ACL keeps other accounts out.
        fd, path = tempfile.mkstemp(prefix="myflames-", suffix=".cnf")
        try:
            with os.fdopen(fd, "wb") as handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(self._build_defaults_file_content().encode("utf-8"))
        except BaseException:
            # Never leave a file that may hold the password behind.
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    @contextmanager
    def _invocation_defaults(self):
        """Keep credentials local to one invocation, including spawn failures."""
        path = self._create_defaults_file()
        try:
            yield path
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def close(self):
        """Delete only the legacy helper's file. Idempotent; calls own theirs."""
        if self._defaults_file_path and os.path.exists(self._defaults_file_path):
            try:
                os.unlink(self._defaults_file_path)
            except OSError:
                pass
        self._defaults_file_path = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        # Safety net: make sure the temp file is not left behind if the
        # caller forgot to call close().
        try:
            self.close()
        except Exception:
            pass

    # ---- command assembly -----------------------------------------------

    def _build_argv(self, sql, extra_flags=None, defaults_path=None):
        """Assemble the full argv for a ``mysql -e '<sql>'`` invocation.

        The first argument is ``--defaults-file=...`` and it MUST be
        first per the mysql CLI documentation. Subsequent flags enable a
        clean machine-readable output format:
          -B/--batch          tab-separated, no borders
          -N/--skip-column-names
          -s/--silent         suppress decorations

        Note: ``--raw`` is deliberately NOT used. Without ``--raw`` the
        client escapes embedded newlines/tabs as ``\\n`` / ``\\t`` so each
        row stays on one physical line — which is what :meth:`query_rows`
        expects. Callers that want raw bytes (like :meth:`explain_analyze`)
        use :meth:`run` directly and unescape there if needed.

        Pass an invocation-owned ``defaults_path`` when starting a client.
        Omitting it retains the legacy helper's close()-managed lifetime.
        """
        if defaults_path is None:
            defaults_path = self._ensure_defaults_file()
        binary = self._resolve_binary()
        argv = [
            binary,
            "--defaults-file=" + defaults_path,
            "--batch", "--skip-column-names", "--silent",
        ]
        argv.extend(CLIENT_CHARSET_FLAGS)
        if self.database:
            argv += ["-D", self.database]
        if extra_flags:
            argv += list(extra_flags)
        argv += ["-e", sql]
        return argv

    @staticmethod
    def _unescape_mysql(s):
        """Reverse the escaping the mysql CLI applies in non-raw batch mode.

        Sequences produced by the client are (per ``mysql --help``):
          \\n  → newline
          \\t  → tab
          \\0  → NUL
          \\\\ → backslash
        The transformation is deliberately minimal — we don't touch other
        backslash sequences because the client doesn't emit them.
        """
        if s is None:
            return s
        out = []
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch == "\\" and i + 1 < n:
                nxt = s[i + 1]
                if nxt == "n":
                    out.append("\n"); i += 2; continue
                if nxt == "t":
                    out.append("\t"); i += 2; continue
                if nxt == "0":
                    out.append("\0"); i += 2; continue
                if nxt == "\\":
                    out.append("\\"); i += 2; continue
            out.append(ch)
            i += 1
        return "".join(out)

    # ---- query execution -------------------------------------------------

    def run(self, sql, extra_flags=None, timeout=None):
        """Run *sql* and return the raw stdout as a ``str``.

        Raises :class:`ConnectorError` with stderr attached on failure.
        """
        try:
            with self._invocation_defaults() as defaults_path, client_environment() as env:
                argv = self._build_argv(sql, extra_flags=extra_flags, defaults_path=defaults_path)
                proc = _run_bounded(argv, timeout=timeout or self.query_timeout, env=env)
        except subprocess.TimeoutExpired:
            from .dbops import redact
            raise ConnectorError(
                "Query timed out after {}s: {}".format(
                    timeout or self.query_timeout, redact(sql or "", self.password)[:120]
                )
            ) from None
        except FileNotFoundError as e:
            raise ConnectorError(
                "Cannot execute MySQL client binary: {}".format(e)
            ) from e
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise ConnectorError(
                "mysql client exited {}: {}".format(proc.returncode, stderr or "no stderr")
            )
        return _client_text(proc.stdout)

    # Compatibility for callers that inspect the write environment allowlist.
    _STDIN_ENV = _CLIENT_ENV

    def run_stdin(self, sql, extra_flags=None, timeout=None):
        """Compatibility name for :meth:`run_write` (SQL never on argv)."""
        return self.run_write(sql, extra_flags=extra_flags, timeout=timeout)

    def run_write(self, sql, extra_flags=None, timeout=None):
        """Run *sql* sent on the client's standard input and return stdout.

        For any statement that carries a secret (a password in CREATE USER,
        ALTER USER or SET PASSWORD): :meth:`run` passes SQL as ``-e SQL``,
        which every local user can read in ``ps``. Here argv holds only the
        option file and flags. The client runs with an empty temporary HOME
        and an allowlisted environment (no ``MYSQL_PWD``, ``MYSQL_HOST``,
        ``~/.my.cnf`` startup SQL or login path) and without automatic
        reconnect, so a session setting made earlier in the script (such as
        ``sql_mode``) cannot vanish halfway. Batch mode stops at the first
        error. Errors redact the connection password and SQL credential
        clauses before they leave the connector.
        """
        from .dbops import redact
        try:
            return self._run_write(sql, extra_flags, timeout)
        except subprocess.TimeoutExpired:
            raise ConnectorError("Query timed out after {}s; the outcome is unknown.".format(
                timeout or self.query_timeout)) from None
        except (ConnectorError, OSError) as exc:
            raise ConnectorError(redact(str(exc), self.password)) from None

    def _run_write(self, sql, extra_flags, timeout):
        with self._invocation_defaults() as defaults_path:
            return self._run_write_with_defaults(sql, extra_flags, timeout, defaults_path)

    def _run_write_with_defaults(self, sql, extra_flags, timeout, defaults_path):
        from .dbops import WRITE_CLIENT_FLAGS
        argv = [self._resolve_binary(), "--defaults-file=" + defaults_path,
                "--batch", "--skip-column-names", "--silent", "--skip-reconnect"]
        argv.extend(CLIENT_CHARSET_FLAGS)
        if self.database:
            argv += ["-D", self.database]
        if extra_flags:
            argv += list(extra_flags)
        argv += list(WRITE_CLIENT_FLAGS)
        with client_environment() as env:
            proc = _run_bounded(argv, input=(sql or "").encode("utf-8"),
                                timeout=timeout or self.query_timeout, env=env)
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise ConnectorError(
                "mysql client exited {}: {}".format(proc.returncode, stderr or "no stderr")
            )
        return _client_text(proc.stdout)

    def query_rows(self, sql):
        """Run *sql* and return a list of list-of-strings (one per row).

        Empty rows are skipped. Values are un-escaped (``\\n`` → newline,
        etc.) so callers can work with multi-line text like SHOW CREATE
        TABLE output directly. No type coercion is done — callers convert
        to int/float as needed.
        """
        text = self.run(sql)
        rows = []
        # Batch mode escapes only \0, \t, \n and \\ inside values, so a raw
        # \r (a CRLF statement) or other line-break characters can appear in
        # a value; only \n separates rows. str.splitlines() would split those.
        for line in text.replace("\r\n", "\n").split("\n"):
            if not line:
                continue
            rows.append([self._unescape_mysql(col) for col in line.split("\t")])
        return rows

    def query_kv(self, sql):
        """Run a two-column SELECT and return a ``{key: value}`` dict.

        Used by SHOW VARIABLES and SHOW STATUS which both return the same
        Variable_name / Value shape.
        """
        out = {}
        for row in self.query_rows(sql):
            if len(row) >= 2:
                out[row[0]] = row[1]
            elif row:
                out[row[0]] = ""
        return out

    def query_dicts(self, sql, columns):
        """Run a SELECT and return rows as list of dicts using *columns*
        as field names. The CLI is invoked with ``--skip-column-names`` so
        we need the caller to supply the expected column order."""
        result = []
        for row in self.query_rows(sql):
            d = {}
            for i, col in enumerate(columns):
                d[col] = row[i] if i < len(row) else ""
            result.append(d)
        return result

    # ---- server metadata -------------------------------------------------

    def server_version(self):
        """Return ``@@version`` string; cached after the first call."""
        if self._version_cache is None:
            rows = self.query_rows("SELECT @@version")
            self._version_cache = rows[0][0] if rows and rows[0] else ""
        return self._version_cache

    def server_version_comment(self):
        """Return ``@@version_comment``; cached independently of the version."""
        if self._version_comment_cache is None:
            rows = self.query_rows("SELECT @@version_comment")
            self._version_comment_cache = rows[0][0] if rows and rows[0] else ""
        return self._version_comment_cache

    def is_mariadb(self):
        """Return True if the server is MariaDB (cached)."""
        if self._is_mariadb_cache is None:
            ver = self.server_version() or ""
            # A version suffix is sufficient; otherwise the comment can identify
            # MariaDB even when the version contains only digits.
            comment = "" if detect_flavor(ver) == "mariadb" else self.server_version_comment()
            self._is_mariadb_cache = detect_flavor(ver, comment) == "mariadb"
        return self._is_mariadb_cache

    def explain_analyze(self, sql):
        """Run an EXPLAIN ANALYZE FORMAT=JSON against *sql* and return the
        raw JSON text (already stripped of MySQL CLI decorations because we
        use --batch --raw --skip-column-names).

        - MySQL: prefixes ``SET explain_json_format_version=2`` and runs
          ``EXPLAIN ANALYZE FORMAT=JSON``.
        - MariaDB: runs ``ANALYZE FORMAT=JSON``.

        EXPLAIN ANALYZE runs the statement, so it runs inside the same
        read-only transaction as capture (live.LiveJob.run): ``SET SESSION
        TRANSACTION READ ONLY; START TRANSACTION READ ONLY; ...; ROLLBACK``.
        The client stops at the first failing statement (no ``--force``), so
        a failed wrapper never lets the query run outside the transaction; on
        any error the session ends with the client and its transaction rolls
        back. The newline before ``;`` ends a trailing ``--`` comment.
        Callers validate *sql* first (``live.bind_query``: one SELECT, no
        backslash commands or DELIMITER lines, every comment closed); it is
        sent with ``-e`` and without ``--binary-mode``.
        """
        if self.is_mariadb():
            stmt = "ANALYZE FORMAT=JSON " + sql
            prefix = ""
        else:
            stmt = "EXPLAIN ANALYZE FORMAT=JSON " + sql
            prefix = "SET explain_json_format_version=2; "
        script = (prefix + "SET SESSION TRANSACTION READ ONLY; START TRANSACTION READ ONLY; "
                  + stmt + "\n;\nROLLBACK")
        return self.run(script, extra_flags=["--raw"])
