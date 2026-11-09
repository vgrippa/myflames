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
import ntpath
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .server_info import detect_flavor, server_version_tuple

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

# The desktop app names the folder of the MySQL client it bundles (mysql and
# mysqldump, apps/desktop/backend.cjs), which it also appends to PATH last.
BUNDLED_CLIENT_ENV = "MYFLAMES_BUNDLED_CLIENT_DIR"


def bundled_client_dir():
    """The desktop app's bundled client folder, or None (an absolute path only)."""
    value = os.environ.get(BUNDLED_CLIENT_ENV) or ""
    return value if os.path.isabs(value) else None


def _same_dir(a, b):
    """Whether two spellings name one folder: by file identity when both exist
    (case-insensitive volumes, junctions), else by their resolved paths."""
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        pass
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def installed_search_path(bundled):
    """PATH without the bundled folder under any spelling (a trailing
    separator, another case, a symlink or junction), for finding installed
    clients first."""
    return os.pathsep.join(entry for entry in (os.environ.get("PATH") or "").split(os.pathsep)
                           if entry and not _same_dir(entry, bundled))


def bundled_program(bundled, name):
    """The bundled *name* (mysql, mysqldump) as an absolute file, or None.

    Built directly, never searched: shutil.which would split a folder whose
    name holds the PATH separator, and on Windows would also look in the
    current directory first.
    """
    candidate = os.path.join(bundled, name + (".exe" if os.name == "nt" else ""))
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


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


def _run_bounded(argv, timeout, input=None, env=None, tail_timeout=None, on_tail_start=None,
                 stdout_limit=None, stderr_limit=None, kill_tree=False):
    """Drain both pipes concurrently on POSIX and Windows with fixed byte caps.

    A separate stdin writer prevents a large script from blocking output
    drainage. On timeout, overflow or interruption, kill and reap the client
    and join every pipe worker before returning to the caller. A read-only
    caller may give (raw marker row, seconds) to start a separate tail budget
    once that complete row arrives. Only that mode retains stdout on timeout.
    Its optional on_tail_start callback runs once in the stdout reader at
    that boundary, before waiting for the tail; it must not block.
    Probes opt into a private process group so descendants holding a pipe
    cannot extend the deadline after the original process exits.
    """
    stdout_limit = MAX_STDOUT_BYTES if stdout_limit is None else stdout_limit
    stderr_limit = MAX_STDERR_BYTES if stderr_limit is None else stderr_limit
    tail_marker = (re.compile(rb'(?:^|\n)' + re.escape(tail_timeout[0].encode('ascii')) + rb'\r?\n')
                   if tail_timeout is not None else None)
    deadline = time.monotonic() + timeout
    process_options = {}
    if kill_tree:
        if os.name == 'nt':
            process_options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options['start_new_session'] = True
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else None,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, bufsize=0,
                            **process_options)
    changed = threading.Event()
    failed = []
    buffers = [bytearray(), bytearray()]
    workers = []
    tail_deadline = []
    timed_out = None

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
                old_size = len(buffers[index])
                buffers[index].extend(chunk)
                if index == 0 and tail_marker is not None and not tail_deadline:
                    # Include a split marker, but never rescan a large result.
                    start = max(0, old_size - len(tail_timeout[0]) - 3)
                    if tail_marker.search(buffers[0], start):
                        tail_deadline.append(time.monotonic() + tail_timeout[1])
                        if on_tail_start is not None:
                            on_tail_start()
                        changed.set()
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
        for args in ((proc.stdout, 0, stdout_limit, "stdout"),
                     (proc.stderr, 1, stderr_limit, "stderr")):
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
            remaining = (tail_deadline[0] if tail_deadline else deadline) - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, tail_timeout[1] if tail_deadline else timeout)
            if not any(worker.is_alive() for worker in workers):
                proc.wait(timeout=remaining)
                break
            changed.wait(min(remaining, 0.05))
        result = subprocess.CompletedProcess(argv, proc.returncode, bytes(buffers[0]), bytes(buffers[1]))
        # Lifecycle control reads reuse argv assembly but have their own result
        # handling. Normalize this client login failure before either caller
        # wraps it. The thread-local holds public program metadata only.
        invocation = getattr(_CLIENT_INVOCATION, 'program', None)
        if (result.returncode and invocation and invocation[0] == argv[0] and
                len(argv) > 1 and argv[1].startswith('--defaults-file=')):
            auth = _client_auth_error(result.stderr.decode('utf-8', 'replace'), argv[0], invocation[1])
            if auth is not None:
                raise auth
        return result
    except subprocess.TimeoutExpired as exc:
        timed_out = exc
        raise
    finally:
        if kill_tree:
            # Kill even when the leader exited: its descendants can still
            # hold stdout/stderr open and keep the pipe workers blocked.
            if os.name == 'nt':
                command = [ntpath.join(os.environ.get('SYSTEMROOT', r'C:\Windows'),
                                       'System32', 'taskkill.exe'),
                           '/PID', str(proc.pid), '/T', '/F']
                try:
                    subprocess.run(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=1, env=env)
                except (OSError, subprocess.TimeoutExpired):
                    pass  # Still kill/reap the direct child below.
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for worker in workers:
            worker.join()
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        if timed_out is not None and tail_timeout is not None:
            # The child is reaped and readers joined before exposing output.
            timed_out.output = bytes(buffers[0])

#: A statement error the client prints in batch mode: "ERROR 1146 (42S02) at line 3: ...".
_STATEMENT_ERROR = re.compile(r"^ERROR \d+(?: \([^)]*\))? at line \d+", re.M)

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


class ClientMissingError(ConnectorError):
    """A missing or unusable application client selection can be corrected."""

    code = "client-missing"
    MESSAGE = "myflames could not find the MySQL or MariaDB command-line client (mysql or mariadb)."

    def __init__(self, message=None):
        super().__init__(self.MESSAGE if message is None else message)


def client_missing_error(exc):
    """Find the typed cause through redaction/prefix wrappers, without text matching.

    An unrelated cleanup failure must not inherit a missing-client diagnosis:
    only myflames' wrapper types (ConnectorError, ValueError) pass on their
    context, suppressed or not, because they suppress it only to redact.
    """
    pending, seen = [exc], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ClientMissingError):
            return current
        if isinstance(current, BaseException):
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            elif isinstance(current, (ConnectorError, ValueError)) and current.__context__ is not None:
                # myflames' own wrappers re-raise with "from None" to keep
                # secrets out of tracebacks (ui._lifecycle_reraise, _redact),
                # not to change the cause; other types never inherit it.
                pending.append(current.__context__)
    return None


_CLIENT_CHOICES = {'mysql': None, 'mariadb': None}
_CLIENT_CHOICES_LOCK = threading.RLock()
_CLIENT_CHOICES_GENERATION = 0
_CONTACT_LOCK = threading.Lock()
_CONTACT_FLIGHTS = {}
_CLIENT_INVOCATION = threading.local()


def set_client_choices(choices):
    """Publish per-engine choices; existing connections follow the generation."""
    global _CLIENT_CHOICES, _CLIENT_CHOICES_GENERATION
    values = {engine: (choices or {}).get(engine) for engine in ('mysql', 'mariadb')}
    with _CLIENT_CHOICES_LOCK:
        if values != _CLIENT_CHOICES:
            _CLIENT_CHOICES = values
            _CLIENT_CHOICES_GENERATION += 1


def client_choices():
    """Return an independent copy of both engine choices."""
    with _CLIENT_CHOICES_LOCK:
        return dict(_CLIENT_CHOICES)


class ClientAuthPluginError(ClientMissingError):
    """The selected client cannot load the account's authentication plugin."""

    code = 'client-auth-plugin'


def _auth_plugin_error(stderr, binary, used=None):
    """Recognize the client error number, never a server SQL error mentioning it."""
    if not re.search(r'\bERROR\s+2059\b', stderr, re.I):
        return None
    match = re.search(r"Authentication plugin ['\"]([^'\"]+)['\"] cannot be loaded", stderr, re.I)
    plugin = match.group(1) if match else 'required authentication'
    used = used or {}
    return ClientAuthPluginError(
        '%s %s (%s) cannot sign in to this server: the account uses the %s plugin, '
        'which these client tools do not include. Choose client tools that include it '
        'in Settings > Client tools.' % (used.get('program') or os.path.basename(binary),
                                       used.get('version') or 'unknown version',
                                       used.get('origin_label') or 'Explicit client', plugin))


def _client_auth_error(stderr, binary, used=None):
    """Describe even the unprobed fast-path client, only after a 2059 failure."""
    if not re.search(r'\bERROR\s+2059\b', stderr, re.I):
        return None
    if used and os.path.realpath(used['path']) != os.path.realpath(binary):
        used = None
    if used is None:
        from . import client_tools
        found = client_tools.discover_choices(client_choices())
        used = next((client_tools._use(g, 'client', name, 'unknown_server')
                     for g in found['installations'] for name, program in g['programs'].items()
                     if name in ('mysql', 'mariadb') and
                     os.path.realpath(program['path']) == os.path.realpath(binary)), None)
    return _auth_plugin_error(stderr, binary, used)


_MACOS_ADMIN_GID = 80


def _check_client_trust(path):
    """Refuse chosen clients whose file or containing folders are untrusted."""
    message = "The database client or a folder above it can be changed by other users."
    if os.name == 'nt':
        # Do not even resolve a UNC/device path: that can contact a server.
        def local_path(value):
            return not value.replace('/', '\\').startswith('\\\\')

        if (not local_path(path) or
                ntpath.basename(path).lower() not in ('mysql.exe', 'mariadb.exe')):
            raise ValueError("Choose a local client named mysql.exe or mariadb.exe.")
        resolved = os.path.realpath(path)
        if not local_path(resolved):
            raise ValueError("Choose a local client named mysql.exe or mariadb.exe.")
        resolved = ntpath.normcase(ntpath.normpath(resolved))
        # This location restriction is a minimum Windows trust check. An
        # ACL check via ctypes is a later improvement, not a version check.
        # The desktop app runs the backend with an isolated USERPROFILE and
        # names the real one in MYFLAMES_USER_PROFILE.
        for key in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432',
                    'LOCALAPPDATA', 'MYFLAMES_USER_PROFILE', 'USERPROFILE'):
            root = os.environ.get(key)
            if not root or not ntpath.isabs(root) or not local_path(root):
                continue
            root = os.path.realpath(root)
            if not local_path(root):
                continue
            root = ntpath.normcase(ntpath.normpath(root))
            try:
                if ntpath.commonpath((root, resolved)) == root:
                    return
            except ValueError:
                continue  # Different drive letters cannot share a root.
        raise ValueError("Choose a database client in Program Files or your user profile.")

    seen = set()
    if not os.path.lexists(path):
        raise ValueError("The database client must be an existing regular file.")
    try:
        for current in (path, os.path.realpath(path)):
            while current and current not in seen:
                seen.add(current)
                info = os.lstat(current)
                if info.st_uid not in (0, os.geteuid()):
                    raise ValueError(message)
                # Symlink modes do not govern replacement; their owner and
                # parent do. Check the resolved file and its parents as well.
                if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022:
                    # macOS: the admin group (gid 80) can already act as root,
                    # and Homebrew (/opt/homebrew/bin) and /Applications give it
                    # write access; any write for others is still refused.
                    admin_only = (sys.platform == 'darwin' and info.st_gid == _MACOS_ADMIN_GID and
                                  not info.st_mode & 0o002)
                    if not admin_only and not (stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
                                               info.st_mode & stat.S_ISVTX):
                        raise ValueError(message)
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
    except OSError as exc:
        raise ValueError(message) from exc


def probe_client(path, check_trust=True, chosen=None):
    """Validate one absolute client path and return its bounded version banner.

    *check_trust* False only describes the automatic PATH client, which the
    connector uses as PATH finds it; a client chosen in the workspace is
    always checked. *chosen* is the path the person chose when *path* is its
    resolved file: the name rule applies to that choice (a ``mysql`` link to
    ``mysql-8.4`` is valid), and the resolved file is the one probed."""
    if (not isinstance(path, str) or not path or len(path) > 4096 or
            any(c in path for c in '\0\r\n') or not os.path.isabs(path)):
        raise ValueError("Choose an absolute path to the database client.")
    name = os.path.basename(chosen or path)
    if (name.lower() if os.name == 'nt' else name) not in ('mysql', 'mariadb', 'mysql.exe', 'mariadb.exe'):
        raise ValueError("Choose a client named mysql, mariadb, mysql.exe or mariadb.exe.")
    if check_trust:
        _check_client_trust(path)
    if not os.path.isfile(path):
        raise ValueError("The database client must be an existing regular file.")
    try:
        with client_environment() as env:
            result = _run_bounded([path, '--no-defaults', '--version'], timeout=5, input=b'', env=env,
                                  stdout_limit=16384, stderr_limit=16384, kill_tree=True)
    except subprocess.TimeoutExpired:
        raise ValueError("The database client version check timed out after 5 seconds.") from None
    except (OSError, ConnectorError):
        raise ValueError("Could not run the database client version check.") from None
    version = (result.stdout + b'\n' + result.stderr).decode('utf-8', 'replace').strip()
    if result.returncode != 0:
        raise ValueError("The database client version check exited unsuccessfully.")
    if re.search(r'\b(?:mysql|mariadb)\b', version, re.I) is None:
        raise ValueError("The database client version output must name MySQL or MariaDB.")
    return {'path': path, 'name': name, 'version': version[:4096]}


class PartialReadError(ConnectorError):
    """A framed, read-only batch failed; its caller must validate completion.

    Output is deliberately absent from the error message and never accepted
    as a successful reading by the ordinary connector methods.
    """

    def __init__(self, message, stdout):
        super().__init__(message)
        self.stdout = stdout


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
        ssh_host=None,
        ssh_user=None,
        ssh_port=None,
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
        # Through SSH (ssh_tunnel.py): every client invocation connects to the
        # local end of the shared tunnel instead of host and port, which are
        # then what the SSH host connects to.
        self.ssh_host = ssh_host or None
        self.ssh_user = ssh_user or None
        self.ssh_port = ssh_port or None
        self._legacy_lease = None
        self._held_lease = None
        # Binary autodetection happens lazily in _resolve_binary so the
        # constructor can be unit-tested without the binary installed.
        self._binary_override = binary
        self._binary_cached = None
        self._binary_generation = None
        self._defaults_file_path = None
        self._version_cache = None
        self._version_comment_cache = None
        self._is_mariadb_cache = None

    # ---- binary + defaults-file plumbing --------------------------------

    @staticmethod
    def _which(name, path=None):
        """Wrapper around shutil.which so tests can monkeypatch it."""
        return shutil.which(name, path=path)

    def _endpoint_key(self):
        from . import client_tools
        return client_tools.endpoint_key(vars(self))

    def _first_contact(self, discovered, keys):
        """Single flight per endpoint, using this object's credentials/transport.

        Only valid server facts persist. All waiters share failures as well as
        successes, and a later request may retry an unknown or failed endpoint.
        """
        from . import client_tools
        endpoint = self._endpoint_key()
        with _CONTACT_LOCK:
            facts = client_tools.server_facts(endpoint)
            if facts is not None:
                return facts
            flight = _CONTACT_FLIGHTS.get(endpoint)
            owner = flight is None
            if owner:
                flight = {'done': threading.Event(), 'facts': None, 'error': None}
                _CONTACT_FLIGHTS[endpoint] = flight
        if not owner:
            flight['done'].wait()
            if flight['error'] is not None:
                raise flight['error']
            return flight['facts']
        try:
            used = client_tools.select(discovered, None, keys)['client']
            if used is None:
                raise ClientMissingError()
            for attempt in range(2):
                try:
                    binary = self._checked_client(used, keys)
                    text = self._run_with_binary('SELECT @@version, @@version_comment', binary, used=used)
                    break
                except ClientAuthPluginError:
                    if attempt:
                        raise
                    other = 'mariadb' if used['engine'] == 'mysql' else 'mysql'
                    # The other engine's pure ranking selects its best copy.
                    candidates = dict(discovered, installations=[g for g in discovered['installations']
                                                                 if g['engine'] == other])
                    used = client_tools.select(candidates, dict(engine=other, version='5.7.0'), keys)['client']
                    if used is None:
                        raise
                except ConnectorError as exc:
                    # Login/transport errors must not cause a second connection.
                    # SQL errors mean this endpoint cannot answer this read.
                    # Both clients print "at line" only after a statement
                    # starts (client/mysql.cc put_info). Client 2xxx failures
                    # such as a lost connection remain connection failures.
                    if re.search(r'\bERROR\s+(?!2\d{3}\b)\d+(?:\s+\([^)]*\))? at line \d+', str(exc)):
                        return None
                    raise
            row = text.strip().split('\n')[0].split('\t') if text.strip() else []
            if len(row) >= 2:
                version, comment = (self._unescape_mysql(v) for v in row[:2])
                client_tools.remember_server(endpoint, detect_flavor(version, comment), version)
                flight['facts'] = client_tools.server_facts(endpoint)
                if flight['facts'] is not None:
                    self._version_cache, self._version_comment_cache = version, comment
            return flight['facts']
        except BaseException as exc:
            flight['error'] = exc
            raise
        finally:
            with _CONTACT_LOCK:
                _CONTACT_FLIGHTS.pop(endpoint, None)
                flight['done'].set()

    @staticmethod
    def _checked_client(used, keys):
        """Recheck chosen files and real folders before every credential use."""
        path = used['path']
        if used.get('installation') in [v for v in keys.values() if v]:
            try:
                _check_client_trust(path)
                real = os.path.realpath(path)
                _check_client_trust(real)
                if not os.path.isfile(real) or not os.access(real, os.X_OK):
                    raise ValueError('The chosen client is not executable.')
                return real
            except (OSError, ValueError) as exc:
                raise ClientMissingError(
                    'The client tools chosen in Settings > Client tools are no longer at %s.' % path) from exc
        return path

    def _resolve_binary(self):
        """Resolve a query client by endpoint facts and per-engine Settings."""
        if self._binary_override:
            self._binary_cached = self._binary_override
            return self._binary_cached
        from . import client_tools
        while True:
            with _CLIENT_CHOICES_LOCK:
                generation = _CLIENT_CHOICES_GENERATION
                choices = dict(_CLIENT_CHOICES)
            candidates = [c for c in client_tools.snapshot() if c['name'] in ('mysql', 'mariadb')]
            if not any(choices.values()) and len(candidates) <= 1:
                # No version process on the common one-client path. The wrapper
                # also preserves the CLI's longstanding which test seam.
                path = candidates[0]['path'] if candidates else None
                if path is None:
                    path = self._which('mysql') or self._which('mariadb')
                if path is None:
                    raise ClientMissingError()
                used = None
            else:
                discovered = client_tools.discover_choices(choices)
                keys, errors = client_tools.choice_keys(discovered, choices)
                with _CLIENT_CHOICES_LOCK:
                    if generation != _CLIENT_CHOICES_GENERATION:
                        continue
                facts = client_tools.server_facts(self._endpoint_key())
                relevant = (facts['engine'],) if facts else ('mysql', 'mariadb')
                for engine in relevant:
                    if errors[engine]:
                        raise ClientMissingError(errors[engine])
                client_count = len({p['realpath'] for g in discovered['installations']
                                    for name, p in g['programs'].items() if name in ('mysql', 'mariadb')})
                if facts is None and max(len(candidates), client_count) >= 2:
                    facts = self._first_contact(discovered, keys)
                relevant = (facts['engine'],) if facts else ('mysql', 'mariadb')
                for engine in relevant:
                    if errors[engine]:
                        raise ClientMissingError(errors[engine])
                used = client_tools.select(discovered, facts, keys)['client']
                # Unknown servers still honor an explicit choice for the
                # fallback client's engine (select intentionally remains pure).
                if facts is None:
                    engine = used['engine'] if used else next((e for e in keys if keys[e]), None)
                    if engine and keys.get(engine):
                        used = client_tools.select(discovered, dict(engine=engine, version='5.7.0'), keys)['client']
                if used is None:
                    raise ClientMissingError()
                try:
                    path = self._checked_client(used, keys)
                except ClientMissingError:
                    with _CLIENT_CHOICES_LOCK:
                        if generation != _CLIENT_CHOICES_GENERATION:
                            continue
                    raise
            with _CLIENT_CHOICES_LOCK:
                if generation != _CLIENT_CHOICES_GENERATION:
                    continue
                self._binary_generation = generation
                self._binary_cached = path
                self._client_use = used
                return path

    def _build_defaults_file_content(self, endpoint=None):
        """Build the text that goes into the defaults-file.

        Only the ``[client]`` section is used so both ``mysql`` and
        ``mariadb`` read it. Values with a ``#`` or ``=`` inside are rare
        but we quote defensively to avoid the CLI parser tripping.
        *endpoint* is ``(host, port)`` of an SSH tunnel's local end, which
        replaces this connection's host and port (always over TCP).
        """
        # A port in an option file does not prevent localhost from selecting
        # the client's default Unix socket. Pin the reviewed transport instead.
        # LifecycleConnection also accepts an explicit socket; keep it usable
        # by inherited write methods as well as its control-query path.
        socket = getattr(self, "socket", None)
        if endpoint is not None and socket:
            raise ConnectorError("A connection through SSH uses TCP, not a socket.")
        if endpoint is None and getattr(self, "ssh_host", None):
            # Never connect directly to a server that is reached through SSH.
            raise ConnectorError("This connection goes through SSH host %s, but no tunnel was opened; "
                                 "nothing was sent." % getattr(self, "ssh_host", None))
        lines = ["[client]", "protocol=" + ("SOCKET" if socket else "TCP")]
        host, port = endpoint if endpoint is not None else (self.host, self.port)
        host = "localhost" if socket else host
        if host:
            lines.append("host=" + _option_value(host))
        if socket:
            lines.append("socket=" + _option_value(socket))
        if port:
            lines.append("port=" + str(port))
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
        lease = getattr(self, "_legacy_lease", None)
        if lease is not None and not lease.alive():
            # Its ssh ended, so the port in the file may now be another
            # program's: never connect there again. A new tunnel, a new file.
            self._legacy_lease = None
            lease.release()
            if self._defaults_file_path:
                try:
                    os.unlink(self._defaults_file_path)
                except OSError:
                    pass
                self._defaults_file_path = None
        if self._defaults_file_path and os.path.exists(self._defaults_file_path):
            return self._defaults_file_path
        if getattr(self, "ssh_host", None):
            # The tunnel stays held until close(), like the file.
            if getattr(self, "_legacy_lease", None) is None:
                self._legacy_lease = self._acquire_tunnel()
            self._defaults_file_path = self._create_defaults_file(("127.0.0.1", self._legacy_lease.port))
        else:
            self._defaults_file_path = self._create_defaults_file()
        return self._defaults_file_path

    def _create_defaults_file(self, endpoint=None):
        """Write a fresh, private option file without storing shared state."""
        # mkstemp creates the file with mode 0600 on POSIX. Windows has no
        # os.fchmod and ignores the mode; the file sits in the user's own
        # %TEMP% directory, whose ACL keeps other accounts out.
        fd, path = tempfile.mkstemp(prefix="myflames-", suffix=".cnf")
        try:
            with os.fdopen(fd, "wb") as handle:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), 0o600)
                content = (self._build_defaults_file_content() if endpoint is None
                           else self._build_defaults_file_content(endpoint))
                handle.write(content.encode("utf-8"))
        except BaseException:
            # Never leave a file that may hold the password behind.
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return path

    def _acquire_tunnel(self):
        """A lease on this connection's SSH tunnel (ssh_tunnel.TUNNELS)."""
        from .ssh_tunnel import TUNNELS, spec_of
        try:
            spec = spec_of(self)
        except ValueError as exc:
            raise ConnectorError(str(exc)) from None
        return TUNNELS.acquire(spec)

    @contextmanager
    def _invocation_defaults(self):
        """Keep credentials local to one invocation, including spawn failures.

        Through SSH the option file names the tunnel's local end, and the
        invocation holds the tunnel until it ends."""
        lease = self._acquire_tunnel() if getattr(self, "ssh_host", None) else None
        try:
            path = (self._create_defaults_file() if lease is None
                    else self._create_defaults_file(("127.0.0.1", lease.port)))
            try:
                yield path
            finally:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
        finally:
            if lease is not None:
                lease.release()

    def hold_transport(self):
        """Keep this connection's SSH tunnel open until close(), for a client
        that outlives one invocation (a Query editor session). No-op without SSH."""
        if getattr(self, "ssh_host", None) and getattr(self, "_held_lease", None) is None:
            self._held_lease = self._acquire_tunnel()

    def _tunnel_note(self, since):
        """What ssh logged about a forward that could not reach the database
        during an invocation that started at *since*, as a sentence, or ''."""
        if not getattr(self, "ssh_host", None):
            return ""
        from .ssh_tunnel import channel_note
        return channel_note(self, since)

    def close(self):
        """Delete only the legacy helper's file. Idempotent; calls own theirs.
        Releases the SSH tunnel this object held (legacy file, editor session)."""
        if self._defaults_file_path and os.path.exists(self._defaults_file_path):
            try:
                os.unlink(self._defaults_file_path)
            except OSError:
                pass
        self._defaults_file_path = None
        for name in ("_legacy_lease", "_held_lease"):
            lease = getattr(self, name, None)
            if lease is not None:
                setattr(self, name, None)
                lease.release()

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

    def _build_argv(self, sql, extra_flags=None, defaults_path=None, binary=None):
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
        binary = binary or self._resolve_binary()
        _CLIENT_INVOCATION.program = (binary, getattr(self, '_client_use', None))
        argv = [
            binary,
            "--defaults-file=" + defaults_path,
            # Refuse LOCAL file requests from a hostile server; the CSV
            # import session alone overrides this later in argv.
            "--local-infile=0",
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

    def run(self, sql, extra_flags=None, timeout=None, tail_timeout=None, on_tail_start=None):
        """Run SQL through the resolved client and return its decoded stdout."""
        return self._run_with_binary(sql, None, extra_flags, timeout, tail_timeout, on_tail_start)

    def _run_with_binary(self, sql, binary, extra_flags=None, timeout=None, tail_timeout=None,
                         on_tail_start=None, used=None):
        """Run *sql* and return the raw stdout as a ``str``.

        Raises :class:`ConnectorError` with stderr attached on failure.
        ``tail_timeout=(marker, seconds)`` is for framed read-only batches:
        unbuffered output starts a separate deadline at the complete marker
        row, and errors retain stdout in :class:`PartialReadError`.
        Optional on_tail_start runs at that marker, even if the tail fails.
        """
        started = time.monotonic()
        try:
            with self._invocation_defaults() as defaults_path, client_environment() as env:
                flags = list(extra_flags or [])
                options = {}
                if tail_timeout is not None:
                    flags.append('--unbuffered')
                    options['tail_timeout'] = tail_timeout
                    if on_tail_start is not None:
                        options['on_tail_start'] = on_tail_start
                argv = self._build_argv(sql, extra_flags=flags, defaults_path=defaults_path, binary=binary)
                _CLIENT_INVOCATION.program = (argv[0], used or getattr(self, '_client_use', None))
                proc = _run_bounded(argv, timeout=timeout or self.query_timeout, env=env, **options)
        except subprocess.TimeoutExpired as exc:
            from .dbops import redact
            message = (
                "Query timed out after {}s: {}".format(
                    exc.timeout, redact(sql or "", self.password)[:120]
                )
            )
            if tail_timeout is not None:
                raise PartialReadError(message, _client_text(exc.output)) from None
            raise ConnectorError(message) from None
        except FileNotFoundError as e:
            raise ConnectorError(
                "Cannot execute MySQL client binary: {}".format(e)
            ) from e
        if proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            auth = _client_auth_error(stderr, argv[0], used or getattr(self, '_client_use', None))
            if auth is not None:
                raise auth
            message = (
                "mysql client exited {}: {}".format(proc.returncode, stderr or "no stderr")
                + self._tunnel_note(started)
            )
            if tail_timeout is not None:
                raise PartialReadError(message, _client_text(proc.stdout))
            raise ConnectorError(message)
        return _client_text(proc.stdout)

    def run_script(self, sql, extra_flags=None, timeout=None):
        """Run read-only *sql* sent on standard input with ``--force`` and
        return ``(stdout, stderr)`` as text.

        Unlike :meth:`run`, a failing statement neither stops the client nor
        raises: its ``ERROR n (state) at line N: ...`` line stays in stderr
        for the caller, so one client process can read many definitions and
        still tell which ones failed (schema_versions.py). The client exits 0
        after statement errors under ``--force``; a non-zero exit without
        such a line (a refused login) raises. ``--skip-reconnect`` keeps the
        session settings made at the start of the script from vanishing.
        """
        try:
            with self._invocation_defaults() as defaults_path:
                argv = [self._resolve_binary(), "--defaults-file=" + defaults_path,
                        "--local-infile=0",
                        "--batch", "--skip-column-names", "--silent", "--skip-reconnect", "--force",
                        "--comments", "--binary-mode"]
                argv.extend(CLIENT_CHARSET_FLAGS)
                if self.database:
                    argv += ["-D", self.database]
                if extra_flags:
                    argv += list(extra_flags)
                with client_environment() as env:
                    proc = _run_bounded(argv, input=(sql or "").encode("utf-8"),
                                        timeout=timeout or self.query_timeout, env=env)
        except subprocess.TimeoutExpired:
            raise ConnectorError("Query timed out after {}s.".format(timeout or self.query_timeout)) from None
        except FileNotFoundError as e:
            raise ConnectorError("Cannot execute MySQL client binary: {}".format(e)) from e
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
        if proc.returncode != 0:
            auth = _client_auth_error(stderr, argv[0], getattr(self, '_client_use', None))
            if auth is not None:
                raise auth
        if proc.returncode != 0 and not _STATEMENT_ERROR.search(stderr):
            raise ConnectorError(
                "mysql client exited {}: {}".format(proc.returncode, stderr.strip() or "no stderr"))
        return _client_text(proc.stdout), stderr

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
                "--local-infile=0",
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
            auth = _client_auth_error(stderr, argv[0], getattr(self, '_client_use', None))
            if auth is not None:
                raise auth
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
        from . import client_tools
        client_tools.remember_server(self._endpoint_key(), detect_flavor(
            self._version_cache, self._version_comment_cache or ''), self._version_cache)
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
            from . import client_tools
            client_tools.remember_server(self._endpoint_key(), detect_flavor(ver, comment), ver)
        return self._is_mariadb_cache

    def explain_analyze(self, sql):
        """Run an EXPLAIN ANALYZE FORMAT=JSON against *sql* and return the
        raw JSON text (already stripped of MySQL CLI decorations because we
        use --batch --raw --skip-column-names).

        - MySQL: prefixes ``SET explain_json_format_version=2`` on 8.3+
          (where the variable was introduced), then runs ``EXPLAIN ANALYZE
          FORMAT=JSON``.
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
        maria = self.is_mariadb()
        version = self.server_version()
        from .live import analyze_unsupported
        refused = analyze_unsupported(version, maria)
        if refused:
            # MySQL before 8.3 refuses EXPLAIN ANALYZE FORMAT=JSON: send nothing.
            raise ConnectorError(refused)
        if maria:
            stmt = "ANALYZE FORMAT=JSON " + sql
            prefix = ""
        else:
            stmt = "EXPLAIN ANALYZE FORMAT=JSON " + sql
            prefix = ("SET explain_json_format_version=2; "
                      if server_version_tuple(version) >= (8, 3, 0) else "")
        script = (prefix + "SET SESSION TRANSACTION READ ONLY; START TRANSACTION READ ONLY; "
                  + stmt + "\n;\nROLLBACK")
        try:
            return self.run(script, extra_flags=["--raw"])
        except ConnectorError as exc:
            from .live import client_error_text
            message = client_error_text(str(exc), mode="analyze", version=version, maria=maria)
            if message == str(exc):
                raise
            error = ConnectorError(message)
            if hasattr(exc, "server_error_code"):
                error.server_error_code = exc.server_error_code
            raise error from None


#: Module-level name for the batch-mode unescape, so modules that import it
#: keep the real function even when a test replaces the connection class.
unescape_mysql = MySQLConnection._unescape_mysql
