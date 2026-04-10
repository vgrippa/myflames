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
variables inherited by children. Instead we write a ``--defaults-extra-file``
to a mode-0600 temp file, point the client at it, and unlink it afterwards
(even on exceptions). The same file carries ``ssl-*`` options so there is a
single source of truth.
"""
import os
import shutil
import subprocess
import sys
import tempfile


class ConnectorError(RuntimeError):
    """Raised when the CLI binary returns a non-zero exit code or malformed
    output. The message preserves stderr from the binary verbatim so users can
    copy/paste it when debugging RDS / firewall / SSL issues."""


class MySQLConnection:
    """A thin wrapper around the ``mysql`` / ``mariadb`` CLI binary.

    Instances are reusable — each call to :meth:`run` spawns a new subprocess
    but the defaults-extra-file is written once in the constructor and reused
    across calls. Remember to call :meth:`close` (or use ``with``) to remove
    the temp file.
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
        """Build the text that goes into the defaults-extra-file.

        Only the ``[client]`` section is used so both ``mysql`` and
        ``mariadb`` read it. Values with a ``#`` or ``=`` inside are rare
        but we quote defensively to avoid the CLI parser tripping.
        """
        lines = ["[client]"]
        if self.host:
            lines.append("host=" + self.host)
        if self.port:
            lines.append("port=" + str(self.port))
        if self.user:
            lines.append("user=" + self.user)
        if self.password is not None:
            # Quote the password so special chars (#, ;, spaces, equals)
            # do not confuse the CLI's INI parser.
            lines.append('password="' + self.password.replace('"', '\\"') + '"')
        if self.ssl_mode:
            lines.append("ssl-mode=" + self.ssl_mode)
        if self.ssl_ca:
            lines.append("ssl-ca=" + self.ssl_ca)
        if self.ssl_cert:
            lines.append("ssl-cert=" + self.ssl_cert)
        if self.ssl_key:
            lines.append("ssl-key=" + self.ssl_key)
        if self.ssl_cipher:
            lines.append("ssl-cipher=" + self.ssl_cipher)
        if self.connect_timeout:
            lines.append("connect-timeout=" + str(int(self.connect_timeout)))
        # Let caching_sha2_password users authenticate over a plaintext
        # connection by fetching the server's RSA public key. This is a
        # MySQL-specific flag that MariaDB silently ignores. It does NOT
        # weaken TLS connections — it only kicks in when TLS is off.
        lines.append("get-server-public-key=ON")
        return "\n".join(lines) + "\n"

    def _ensure_defaults_file(self):
        """Lazily create the mode-0600 defaults file the CLI reads."""
        if self._defaults_file_path and os.path.exists(self._defaults_file_path):
            return self._defaults_file_path
        fd, path = tempfile.mkstemp(prefix="myflames-", suffix=".cnf")
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, self._build_defaults_file_content().encode("utf-8"))
        finally:
            os.close(fd)
        self._defaults_file_path = path
        return path

    def close(self):
        """Delete the defaults-extra-file. Safe to call multiple times."""
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

    def _build_argv(self, sql, extra_flags=None):
        """Assemble the full argv for a ``mysql -e '<sql>'`` invocation.

        The first argument is ``--defaults-extra-file=...`` and it MUST be
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
        """
        defaults_path = self._ensure_defaults_file()
        binary = self._resolve_binary()
        argv = [
            binary,
            "--defaults-extra-file=" + defaults_path,
            "--batch", "--skip-column-names", "--silent",
        ]
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
        argv = self._build_argv(sql, extra_flags=extra_flags)
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.query_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise ConnectorError(
                "Query timed out after {}s: {}".format(
                    timeout or self.query_timeout, (sql or "")[:120]
                )
            ) from e
        except FileNotFoundError as e:
            raise ConnectorError(
                "Cannot execute MySQL client binary: {}".format(e)
            ) from e
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise ConnectorError(
                "mysql client exited {}: {}".format(proc.returncode, stderr or "no stderr")
            )
        return (proc.stdout or b"").decode("utf-8", errors="replace")

    def query_rows(self, sql):
        """Run *sql* and return a list of list-of-strings (one per row).

        Empty rows are skipped. Values are un-escaped (``\\n`` → newline,
        etc.) so callers can work with multi-line text like SHOW CREATE
        TABLE output directly. No type coercion is done — callers convert
        to int/float as needed.
        """
        text = self.run(sql)
        rows = []
        for line in text.splitlines():
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

    def is_mariadb(self):
        """Return True if the server is MariaDB (cached)."""
        if self._is_mariadb_cache is None:
            ver = self.server_version() or ""
            self._is_mariadb_cache = "mariadb" in ver.lower()
        return self._is_mariadb_cache

    def explain_analyze(self, sql):
        """Run an EXPLAIN ANALYZE FORMAT=JSON against *sql* and return the
        raw JSON text (already stripped of MySQL CLI decorations because we
        use --batch --raw --skip-column-names).

        - MySQL: prefixes ``SET explain_json_format_version=2`` and runs
          ``EXPLAIN ANALYZE FORMAT=JSON``.
        - MariaDB: runs ``ANALYZE FORMAT=JSON``.
        """
        if self.is_mariadb():
            stmt = "ANALYZE FORMAT=JSON " + sql
        else:
            stmt = "SET explain_json_format_version=2; EXPLAIN ANALYZE FORMAT=JSON " + sql
        return self.run(stmt)
