"""Server lifecycle backend (stdlib, Python 3.7+).

Public workflow: detect_tools -> prepare_dump/prepare_restore/prepare_clone ->
*_job(..., confirmed=True, typed_target=plan['target'], audit=AuditTrail(...)).
Plans are JSON values, never authority: execution rebuilds the preflight and
compares its binding before sending anything. Callers must use the trusted
profile safety from dbops.connection_safety, not a policy supplied by a client.
No CLI, HTTP endpoint, global registry, or process starts on import.

Wire contracts:
  lifecycle-tools-1.0: tools[name] {available, path, version, engine, deprecated}
  upgrade-check-1.0: server, target_version, method, partial, checks[], counts,
    manual_checks[], limitations[], status (findings are NOT a process failure).
  lifecycle-plan-1.0: operation, request, server, connection_key, tool, target,
    commands[], warnings[], checks[], ready, binding.
  lifecycle-job-1.0: id, operation, state, progress, events[], result, error;
    result: status, verification[] and (for dumps) manifest.
    Failures include failure_category (input, connection, operation).
  Restore plans carry manifest, manifest_hash, required_acknowledgement and a
    staging_space check. Start requires typed_acknowledgement for mysql table
    replacement, in addition to typed_target and existing_objects=drop.
  lifecycle-manifest-1.0: source, scope, tool, options, started_at, finished_at,
    files[{path,size_bytes,sha256}], samples[], verification[], complete.
Verification statuses: passed, failed, not_checked. A successful child process
alone is never evidence that a restored dataset matches its source.

Database contracts and source locations: .claude/skills/server-lifecycle/SKILL.md.
Native checks deliberately cover a small documented set, not Shell's checker.
No MySQL Shell implementation is vendored or translated here.
"""
import collections
import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid

from . import dbops, privileges, dump_options
from .connector import ConnectorError, MySQLConnection, terminal_text
from .perf_common import Reader, ident, literal, now
# Public compatibility export, including MariaDB tool banner precedence.
from .server_info import detect_flavor, tool_version_tuple as version_tuple, unsupported_version_note

TAG = '/*myflames:lifecycle*/ '
TOOLS = ('mysqlsh', 'mysqldump', 'mysqlpump', 'mariadb-dump', 'mariadb-backup',
         'mysql', 'mariadb', 'mariadb-upgrade')
COMMON_BIN_DIRS = ('/opt/local/bin', '/usr/sbin', '/opt/mysqlsh/bin', '/opt/homebrew/bin', '/usr/local/bin', '/usr/bin',
                   '/usr/local/mysql/bin', '/opt/homebrew/opt/mysql-client/bin',
                   '/opt/homebrew/opt/mysql-shell/bin', '/usr/local/opt/mysql-client/bin',
                   '/opt/homebrew/opt/mariadb/bin', '/usr/local/opt/mariadb/bin')
MANIFEST = 'myflames-manifest.json'
# Bound unauthenticated-by-confirmation restore preflights, including empty
# directory trees and files that grow while being read. Confirmed dumps do
# not use these limits when writing their manifests.
MAX_RESTORE_FILES = 10000
MAX_RESTORE_BYTES = 10 * 1024 * 1024 * 1024
MAX_RESTORE_DIRECTORIES = 10000
DOC = 'https://dev.mysql.com/doc/refman/8.4/en/'
SHELL_DOC = 'https://dev.mysql.com/doc/mysql-shell/9.6/en/mysql-shell-utilities-upgrade.html'
# Intentionally limited metadata checks; dates verified in the versioned manual.
# https://dev.mysql.com/doc/refman/8.4/en/added-deprecated-removed.html
# A variable counts when source < removal version <= target.
REMOVED_VARIABLES = {'query_cache_size': (8, 0, 0), 'query_cache_type': (8, 0, 0),
                     'expire_logs_days': (8, 2, 0),
                     'default_authentication_plugin': (8, 4, 0)}
#: Words that became reserved in a release. 8.0.14 and later: the
#: <lexer.keyword> declarations in sql/sql_yacc.yy per release tag (what
#: gen_keyword_list.cc turns into information_schema.KEYWORDS), checked against
#: KEYWORDS read live on MySQL 8.0.46, 8.4.11 and 9.7.2 (8.4 added QUALIFY and
#: TABLESAMPLE, 9.7 EXTERNAL and LIBRARY; see test/fixtures/lifecycle/
#: PROVENANCE.txt). 8.0.11: the 8.0 keyword manual's new reserved words, each
#: reserved on 8.0.46 live. A word counts when source < version <= target; on
#: an 8.0+ source the source's own KEYWORDS list drops words it already reserves.
RESERVED_SINCE = (
    ((8, 0, 11), ('CUBE', 'CUME_DIST', 'DENSE_RANK', 'EMPTY', 'EXCEPT', 'FIRST_VALUE', 'FUNCTION',
                  'GROUPING', 'GROUPS', 'JSON_TABLE', 'LAG', 'LAST_VALUE', 'LEAD', 'NTH_VALUE', 'NTILE',
                  'OF', 'OVER', 'PERCENT_RANK', 'RANK', 'RECURSIVE', 'ROW', 'ROWS', 'ROW_NUMBER',
                  'SYSTEM', 'WINDOW')),
    ((8, 0, 14), ('LATERAL',)),
    ((8, 0, 31), ('INTERSECT',)),
    ((8, 4, 0), ('QUALIFY', 'TABLESAMPLE')),
    ((9, 2, 0), ('LIBRARY',)),
    ((9, 4, 0), ('EXTERNAL',)),
)
SYSTEM_SCHEMAS = ('mysql', 'sys', 'performance_schema', 'information_schema')
_SERVER_VARS = ('version', 'version_comment', 'hostname', 'port', 'server_uuid',
                'server_id', 'datadir', 'socket', 'local_infile', 'sql_mode',
                'version_compile_os', 'version_compile_machine', 'character_set_server',
                'character_set_filesystem', 'collation_server', 'innodb_page_size',
                'max_allowed_packet', 'clone_valid_donor_list', 'gtid_executed')


_ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


class LifecycleError(ConnectorError):
    """A lifecycle operation failed before a verified result was obtained."""


class Refused(LifecycleError):
    """A preflight, identity, policy or confirmation refused the operation."""


class RestoreTargetMismatch(Refused):
    """The typed restore target differs from the reviewed target; nothing ran."""


_ANSI = re.compile(r'(?:\x1b|\\u001[bB]|\\x1[bB])\[[0-9;?]*[ -/]*[@-~]')
_ERROR_WORDS = re.compile(r'error|unsupported|not found|not exist|denied|cannot|can\'t|unknown|failed|refused',
                          re.I)


def strip_ansi(text):
    """Drop terminal colour codes that MySQL Shell writes even to a pipe."""
    return _ANSI.sub('', text or '')


def tool_error_line(stderr):
    """The one meaningful line of a failed tool's stderr.

    Colour codes are removed, NOTE and WARNING lines and Shell's trailing
    "at STDIN:2" position are dropped, and the first line that reads like an
    error wins (else the first remaining line). Empty output stays empty.
    """
    lines = []
    for raw in strip_ansi(stderr).splitlines():
        line = re.sub(r'\s+at STDIN:\d+\s*$', '', ' '.join(raw.split()))
        if line and not re.match(r'(?:NOTE|WARNING|INFO):', line, re.I):
            lines.append(line)
    for line in lines:
        if _ERROR_WORDS.search(line):
            return line
    return lines[0] if lines else ''


def shell_version_gap(tool, server_version):
    """How MySQL Shell's version compares with the server's: 'major' when the
    server's major version is higher (the Shell refuses the dump), 'minor'
    when only the minor is higher (the Shell warns and may fail), else None.

    MySQL Shell modules/util/dump/dumper.cc:5311-5317 throws "Unsupported
    MySQL Server ... please upgrade the MySQL Shell first" for a higher
    major; :5319-5334 warns for a server at or above the next minor.
    """
    shell = tuple((tool or {}).get('version_tuple') or ())[:2]
    server = version_tuple(server_version)[:2]
    if len(shell) < 2 or server == (0, 0):
        return None
    if server[0] > shell[0]:
        return 'major'
    return 'minor' if server[0] == shell[0] and server[1] > shell[1] else None


def shell_version_message(tool, server_version, gap):
    shell = '.'.join(str(n) for n in (tool.get('version_tuple') or ())[:2])
    return ('MySQL Shell %s is older than the server (%s) and %s; update MySQL Shell, or rerun with --no-shell '
            'to use mysqldump.' % (shell, str(server_version).split('-')[0],
                                   'refuses it' if gap == 'major' else 'may fail on it'))


def _clean_env(home):
    # Allowlist rather than deleting MYSQL_PWD alone: no AWS, vault, API, or
    # arbitrary application secrets are inherited by external lifecycle tools.
    result = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'WINDIR',
              'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'TMPDIR', 'TEMP', 'TMP') if key in os.environ}
    result.update(HOME=home, USERPROFILE=home, MYSQLSH_USER_CONFIG_HOME=home)
    return result


def _safe(value, passwords=()):
    value = dbops.redact_value(value)
    # Remove longer secrets first when one password contains the other. Tools
    # may echo a JSON/option-file escaped value instead of the raw password.
    variants = {variant for password in passwords if isinstance(password, str) and password
                for variant in (password, json.dumps(password)[1:-1])}
    for password in sorted(variants, key=len, reverse=True):
        value = dbops.redact_value(value, password)
    return value


def _report_safe(value, passwords=(), field=''):
    """Redact free text, preserving schema IDs, filenames and checksum bytes.

    Blind substring replacement over a manifest corrupts hashes when, for
    example, a password is a single hex character. Structured records never
    contain credentials in the first place; only free text needs scrubbing.
    """
    if isinstance(value, dict):
        return {key: _report_safe(item, passwords, key) for key, item in value.items()
                if str(key).lower() not in ('password', 'source_password', 'master_password')}
    if isinstance(value, (list, tuple)):
        # These are raw server rows, including clone_status.ERROR_MESSAGE,
        # rather than typed report fields. Scrub every cell at every depth;
        # scalar status codes and manifest identifiers stay unchanged below.
        if field in ('status', 'progress'):
            return _safe(value, passwords)
        return [_report_safe(item, passwords, field) for item in value]
    if isinstance(value, str) and field in ('text', 'error', 'message', 'reason', 'description',
                                           'warnings', 'limitations', 'partial_state', 'cancel_error',
                                           'commands', 'tool_options', 'statement', 'notes', 'manual_checks'):
        return _safe(value, passwords)
    return value


def detect_tools(path=None, common_dirs=None, job=None, check_updates=False):
    """Discover each external tool, including deprecated mysqlpump (never offered).

    common_dirs=() makes PATH-only discovery deterministic for callers/tests.
    An unrecognized or failing version probe is unavailable, never guessed.
    """
    found = {}
    for name in TOOLS:
        if job:
            job.check_cancel()
        binary = shutil.which(name, path=path)
        if not binary:
            for folder in COMMON_BIN_DIRS if common_dirs is None else common_dirs:
                candidate = os.path.join(folder, name)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    binary = candidate
                    break
        entry = {'available': False, 'deprecated': name == 'mysqlpump'}
        if binary:
            entry['path'] = os.path.abspath(binary)
            try:
                with tempfile.TemporaryDirectory(prefix='myflames-probe-') as home:
                    child = subprocess.run([entry['path'], '--version'], stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, timeout=8, env=_clean_env(home))
                output = child.stdout.decode('utf-8', 'replace').strip()[:4096]
                entry['version'] = output
                entry['version_tuple'] = list(version_tuple(output))
                entry['engine'] = detect_flavor(output)
                entry['available'] = child.returncode == 0 and version_tuple(output) != (0, 0, 0)
                if not entry['available']:
                    entry['error'] = 'Version probe failed or returned an unrecognized version.'
            except (OSError, subprocess.TimeoutExpired) as exc:
                entry['error'] = type(exc).__name__
        found[name] = entry
    from .tool_updates import enrich_tools
    return enrich_tools({'schema_version': 'lifecycle-tools-1.0', 'tools': found},
                        check_updates=check_updates)


def _tool(tools, name):
    return (tools.get('tools', tools).get(name) or {})


def _pick(tools, names, engine=None):
    for name in names:
        entry = _tool(tools, name)
        if entry.get('available') and not entry.get('deprecated') and (not engine or entry.get('engine') == engine):
            return dict(entry, name=name)
    other = [name for name in names if _tool(tools, name).get('available')]
    note = (' (%s here is from %s, which this %s server does not use)' % (
        ' and '.join(other), 'MariaDB' if engine == 'mysql' else 'MySQL', 'MySQL' if engine == 'mysql' else 'MariaDB')
        if other and engine else '')
    raise Refused('No usable %s on this machine%s. Install %s client tools and run it again.' % (
        ' or '.join(names), note, 'MariaDB' if engine == 'mariadb' else 'MySQL'))


def offered_tools(server, tools):
    """Return capabilities, never offer mysqlpump or untested cross-engine loads."""
    engine, version = server['engine'], version_tuple(server['version'])
    shell = _tool(tools, 'mysqlsh').get('available', False)
    logical = []
    if engine == 'mysql' and shell and version >= (5, 7, 0):
        logical.append('mysqlsh')
    for name in (('mariadb-dump', 'mysqldump') if engine == 'mariadb' else ('mysqldump',)):
        entry = _tool(tools, name)
        if entry.get('available') and entry.get('engine') == engine:
            logical.append(name)
    return {'dump': logical, 'upgrade': 'mysqlsh' if engine == 'mysql' and shell else
            ('native_subset' if engine == 'mysql' else 'mariadb_upgrade_guidance'),
            'clone': engine == 'mysql' and version >= (8, 0, 17),
            'physical_backup': engine == 'mariadb' and _tool(tools, 'mariadb-backup').get('available', False),
            'physical_backup_requires_local_files': True}


def read_server(conn, connection=None):
    """Read identity freshly; missing UUID falls back to a composite identity."""
    rows = Reader(conn, TAG).rows('SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)' %
                                  ','.join(literal(v) for v in _SERVER_VARS))
    variables = {str(row[0]).lower(): row[1] for row in rows if len(row) == 2}
    if not variables.get('version') or not variables.get('hostname') or not variables.get('server_id'):
        raise LifecycleError('Could not establish server identity (version, hostname, server_id).')
    return {'engine': detect_flavor(variables['version'], variables.get('version_comment')),
            'version': variables['version'], 'variables': variables,
            'identity': {key: variables.get(key, '') for key in
                         ('server_uuid', 'server_id', 'hostname', 'port', 'version', 'datadir', 'socket')}}


def _options(connection):
    """Validate all option-file fields, including those not accepted by live.py."""
    allowed = ('host', 'user', 'password', 'database', 'ssl_mode', 'ssl_ca',
               'ssl_cert', 'ssl_key', 'ssl_cipher', 'socket')
    out = {}
    for key in allowed:
        value = connection.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > 4096 or '\x00' in value or (key != 'password' and any(c in value for c in '\r\n')):
                raise ValueError('Invalid connection field: ' + key)
            out[key] = value
    port = connection.get('port', 3306)
    if isinstance(port, bool) or not str(port).isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('Port must be 1..65535.')
    out['port'] = int(port)
    out.setdefault('host', '127.0.0.1')
    return out


def _connect(factory, connection):
    options = _options(connection)
    # The new transport supports explicit sockets; custom factories keep
    # the original connector's constructor contract.
    if factory is not LifecycleConnection:
        options.pop('socket', None)
    return factory(**options)


@contextlib.contextmanager
def _credentials(connection, tool):
    """0600 option file first on argv, removed even after cancellation/spawn errors.

    Reuses the connector's option-file escaping pattern, quoting every value
    rather than only the password. Never put a credential in a URI or env.
    """
    options = _options(connection)
    if options.get('socket'):
        options['host'] = 'localhost'
    with tempfile.TemporaryDirectory(prefix='myflames-lifecycle-') as home:
        path = os.path.join(home, 'connection.cnf')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write('[client]\n')
                for key, value in options.items():
                    if key == 'database':
                        continue  # database is a client argument, not a shared tool option
                    if key == 'ssl_mode' and tool.get('engine') == 'mariadb':
                        mode = value.upper()
                        if mode == 'DISABLED':
                            handle.write('skip-ssl\n')
                        elif mode in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY'):
                            handle.write('ssl\n')
                            if mode.startswith('VERIFY'):
                                handle.write('ssl-verify-server-cert\n')
                        elif mode != 'PREFERRED':
                            raise ValueError('Invalid TLS mode.')
                        continue
                    text = str(value).replace('\\', '\\\\').replace('"', '\\"')
                    for char, escaped in (('\n', '\\n'), ('\r', '\\r'), ('\t', '\\t'), ('\b', '\\b')):
                        text = text.replace(char, escaped)
                    handle.write('%s="%s"\n' % (key.replace('_', '-'), text))
            yield [tool['path'], '--defaults-file=' + path], _clean_env(home)
        finally:
            if os.path.exists(path):
                os.unlink(path)


class LifecycleConnection(MySQLConnection):
    """Connector transport with an allowlisted environment and SQL on stdin."""
    def __init__(self, socket=None, **options):
        self.socket = socket
        super(LifecycleConnection, self).__init__(**options)

    def run(self, sql, extra_flags=None, timeout=None):
        with self._invocation_defaults() as defaults_path:
            return self._run_with_defaults(sql, extra_flags, timeout, defaults_path)

    def _run_with_defaults(self, sql, extra_flags, timeout, defaults_path):
        argv = self._build_argv('', extra_flags, defaults_path=defaults_path)[:-2]
        if self.socket:
            argv += ['--protocol=SOCKET', '--socket=' + self.socket]
        with tempfile.TemporaryDirectory(prefix='myflames-control-') as home:
            try:
                proc = subprocess.run(argv, input=sql.encode('utf-8'), stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, timeout=timeout or self.query_timeout,
                                      env=_clean_env(home))
            except subprocess.TimeoutExpired:
                raise ConnectorError('Lifecycle control query timed out.')
        if proc.returncode:
            raw = proc.stderr.decode('utf-8', 'replace')
            error = ConnectorError(dbops.redact(raw, self.password))
            error.server_error_code = dbops.error_code(raw)
            raise error
        from .connector import _client_text
        return _client_text(proc.stdout)


def _verification(check, status, reason, **extra):
    return dict(extra, check=check, status=status, reason=reason)


class _BoundedConnection(object):
    """Metadata calls have a deadline and observe the owning job's cancel.

    The connector's subprocess timeout reaps the client. Reads therefore
    delay cancellation by at most five seconds, rather than a full scan or
    the connection's usual query timeout.
    """
    def __init__(self, connection, job=None, deadline=None):
        self.connection, self.job, self.deadline = connection, job, deadline

    def run(self, sql, extra_flags=None, timeout=None):
        if self.job:
            self.job.check_cancel()
        seconds = min(timeout or 5, 5)
        if self.deadline is not None:
            seconds = min(seconds, self.deadline - time.monotonic())
            if seconds <= 0:
                raise LifecycleError('Metadata sampling deadline reached.')
        try:
            return self.connection.run(sql, extra_flags=extra_flags, timeout=seconds)
        finally:
            if self.job:
                self.job.check_cancel()


class LifecycleJob(object):
    """LiveJob-style owner of one operation, child process group and bounded log.

    start() is optional: run() executes synchronously in tests/CLI. wait() joins
    the worker; close() cancels and joins. Callers must close jobs on shutdown.
    Every external stream is drained concurrently; logs are capped at 200 events.
    A percent is absent until actually reported; no invented time estimate.
    """
    def __init__(self, operation, work, connection=None, factory=LifecycleConnection, passwords=()):
        self.id = uuid.uuid4().hex
        self.created = time.monotonic()
        self.operation, self.work = operation, work
        self.connection, self.factory = copy.deepcopy(connection or {}), factory
        self.passwords = tuple(passwords) + (self.connection.get('password'),)
        self.state, self.error, self.result = 'queued', '', None
        self.progress = None
        self.cancelled = threading.Event()
        self.events = collections.deque(maxlen=200)
        self.lock = threading.RLock()
        self.process = None
        self.connection_id = None
        self.thread = None
        self.marker = 'myflames_job_' + self.id + ':'
        self._started = False
        self.cancel_error = ''
        #: What a failure or cancel may leave behind; the operation sets it.
        self.partial_state = 'Partial dump, restore or clone data may remain; inspect the target.'
        #: Latest structured progress (clone: performance_schema.clone_progress).
        self.detail = None
        #: Events emitted so far (the log keeps the last 200).
        self.emitted = 0
        self.audit = None
        self.audit_statements = []

    def emit(self, text, stream='status'):
        # Control data must be consumed before any password replacement. Even
        # a one-character password may overlap the marker or session number.
        raw = str(text)
        if stream in ('stdout', 'status') and re.fullmatch(re.escape(self.marker) + r'[0-9]+', raw):
            with self.lock:
                self.connection_id = int(raw[len(self.marker):])
            if self.cancelled.is_set():
                self._kill_query()
            return
        # Tools such as mysqlsh color their output even without a terminal.
        text = terminal_text(_safe(_ANSI.sub('', str(text)), self.passwords))[:8192]
        with self.lock:
            match = re.search(r'(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%', text)
            if match and 0 <= float(match.group(1)) <= 100:
                self.progress = float(match.group(1))
            self.events.append({'at': now(), 'stream': stream, 'text': text})
            self.emitted += 1

    def snapshot(self):
        with self.lock:
            return _report_safe({'schema_version': 'lifecycle-job-1.0', 'id': self.id,
                          'operation': self.operation, 'state': self.state,
                          'progress': self.progress, 'detail': copy.deepcopy(self.detail),
                          'events': list(self.events),
                          'result': copy.deepcopy(self.result), 'error': self.error}, self.passwords)

    def start(self):
        with self.lock:
            if self._started:
                raise RuntimeError('Job was already started.')
            self._started = True
            self.thread = threading.Thread(target=self._run, name='lifecycle-' + self.id, daemon=True)
            self.thread.start()
        return self

    def run(self):
        with self.lock:
            if self._started:
                raise RuntimeError('Job was already started.')
            self._started = True
        self._run()
        return self.snapshot()

    def _run(self):
        self.state = 'running'
        try:
            self.check_cancel()
            self.result = _report_safe(self.work(self), self.passwords)
            self.state = 'cancelled' if self.cancelled.is_set() else 'done'
        except (Exception, KeyboardInterrupt) as exc:
            code = getattr(exc, 'server_error_code', None) or dbops.error_code(str(exc))
            self.error = _safe(str(exc), self.passwords)
            self.state = 'cancelled' if self.cancelled.is_set() else ('refused' if isinstance(exc, Refused) else 'failed')
            status = ('denied' if code in (1227, 1095, 3630, 1290, 1792) else
                      'interrupted' if code in (1317, 1205, 1213) else
                      'unknown' if dbops.may_have_run(exc) else self.state)
            self.result = {'status': status, 'verification': [
                _verification('operation', 'not_checked', self.error)],
                           'partial_state': self.partial_state, 'cancel_error': self.cancel_error}
            self.result['failure_category'] = (
                'operation' if self.cancelled.is_set() or isinstance(exc, Refused) else
                'connection' if code in (1045, 2002, 2003, 2005, 2006, 2013, 2026) else
                'input' if isinstance(exc, (ValueError, TypeError, OSError)) else 'operation')
        finally:
            if self.audit is not None:
                if not self.audit.finished:
                    self.audit.outcome(_safe(self.audit_statements, self.passwords), 'declined',
                                       outcome='cancelled', message='Cancelled before work began; nothing was sent.')
                if isinstance(self.result, dict):
                    self.result['audit_id'] = self.audit.id
            self.process, self.connection_id = None, None

    def check_cancel(self):
        if self.cancelled.is_set():
            raise LifecycleError('Cancelled; partial files or server changes may remain. Check the target before retrying.')

    def _kill_query(self):
        # Only the job-owned session identified on its own stdout. Never use
        # a saved processlist id supplied by a caller. This is cancellation of
        # an already authorized operation, not an arbitrary kill endpoint.
        connection_id = self.connection_id
        if connection_id:
            try:
                with _connect(self.factory, self.connection) as conn:
                    conn.run(TAG + 'KILL QUERY %d' % connection_id,
                             extra_flags=list(dbops.WRITE_CLIENT_FLAGS), timeout=5)
            except Exception as exc:
                self.cancel_error = _safe(str(exc), self.passwords)
                self.emit('Server-side cancellation could not be confirmed: ' + self.cancel_error)

    def _signal(self, sig):
        process = self.process
        if process is not None:
            try:
                if os.name == 'posix':
                    os.killpg(process.pid, sig)
                else:
                    self._terminate_tree(force=sig != signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                # Group already gone or exiting: macOS answers EPERM when
                # the group holds only a zombie that is not reaped yet.
                pass

    def _terminate_tree(self, force=False):
        process = self.process
        if process is None:
            return
        if os.name == 'posix':
            # POSIX signal 9; never resolve a Windows-missing signal constant.
            self._signal(9 if force else signal.SIGTERM)
            return
        # taskkill /T also reaches descendants that inherited our pipes. Use
        # the system binary, not a user-controlled PATH executable.
        command = [os.path.join(os.environ.get('SYSTEMROOT', r'C:\Windows'), 'System32', 'taskkill.exe'),
                   '/PID', str(process.pid), '/T']
        if force:
            command.append('/F')
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            if result.returncode and process.poll() is None:
                process.kill() if force else process.terminate()
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill() if force else process.terminate()

    def cancel(self):
        self.cancelled.set()
        self._kill_query()
        self._signal(signal.SIGTERM)

    def wait(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return self.snapshot()

    def close(self):
        if not self._started:
            self.cancel()
            return self.run()
        if self.thread and self.thread.is_alive():
            self.cancel()
            self.thread.join(8)
            if self.thread.is_alive():
                self._terminate_tree(force=True)
                # All owned subprocesses and bounded metadata work must finish
                # before the CLI exits or the workspace closes.
                self.thread.join()
        return self.snapshot()

    def process_tool(self, argv, env, input_data=None, input_file=None, output_file=None, timeout=None):
        """Stream both pipes without deadlock; bound captured text to 16 MiB.

        SQL dump stdout goes directly to its binary file, never a log. SQL and
        Shell scripts containing secrets go only to stdin, on a writer thread.
        """
        self.check_cancel()
        proc = subprocess.Popen(argv, stdin=input_file if input_file else subprocess.PIPE,
                                stdout=output_file if output_file else subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, start_new_session=os.name == 'posix')
        with self.lock:
            self.process = proc
        captured = {'stdout': bytearray(), 'stderr': bytearray()}
        overflow = threading.Event()
        def drain(pipe, stream):
            pending, omitted = bytearray(), False
            try:
                while True:
                    data = pipe.read1(4096)
                    if not data:
                        break
                    if len(captured[stream]) + len(data) <= 16 * 1024 * 1024:
                        captured[stream].extend(data)
                    else:
                        overflow.set()
                    # Process CR-only progress immediately, without waiting for
                    # a newline. Do not emit fragments of oversized lines: a
                    # secret could straddle a fragment boundary.
                    for byte in data:
                        if byte in (10, 13):
                            if omitted:
                                self.emit('[tool line longer than 8192 bytes omitted]', stream)
                            elif pending:
                                self.emit(strip_ansi(pending.decode('utf-8', 'replace')), stream)
                            pending, omitted = bytearray(), False
                        elif not omitted:
                            pending.append(byte)
                            if len(pending) > 8192:
                                pending, omitted = bytearray(), True
                if omitted:
                    self.emit('[tool line longer than 8192 bytes omitted]', stream)
                elif pending:
                    self.emit(strip_ansi(pending.decode('utf-8', 'replace')), stream)
            finally:
                pipe.close()
        readers = []
        for stream in ('stdout', 'stderr'):
            pipe = getattr(proc, stream)
            if pipe:
                thread = threading.Thread(target=drain, args=(pipe, stream), daemon=True)
                thread.start()
                readers.append(thread)
        def feed():
            try:
                if input_data:
                    proc.stdin.write(input_data.encode('utf-8'))
            except (BrokenPipeError, OSError):
                pass
            finally:
                proc.stdin.close()
        writer = None
        if not input_file:
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
        started = time.monotonic()
        terminating = None
        try:
            while proc.poll() is None:
                if timeout and time.monotonic() - started > timeout:
                    self.cancel()
                if self.cancelled.is_set():
                    if terminating is None:
                        terminating = time.monotonic()
                        self._signal(signal.SIGTERM)
                    elif time.monotonic() - terminating > 2:
                        self._terminate_tree(force=True)
                self.cancelled.wait(0.05) if not self.cancelled.is_set() else time.sleep(0.01)
            proc.wait()
        except BaseException:
            self.cancel()
            self._terminate_tree(force=True)
            proc.wait()
            raise
        finally:
            # Kill surviving grandchildren before joining their inherited pipes.
            self._signal(signal.SIGTERM)
            for thread in readers:
                thread.join(3)
            if any(thread.is_alive() for thread in readers):
                self._terminate_tree(force=True)
                for thread in readers:
                    thread.join(3)
            if writer:
                writer.join(3)
            with self.lock:
                self.process, self.connection_id = None, None
        self.check_cancel()
        stdout = captured['stdout'].decode('utf-8', 'replace')
        stderr = captured['stderr'].decode('utf-8', 'replace')
        if overflow.is_set():
            raise LifecycleError('Tool output exceeded the 16 MiB capture limit.')
        if proc.returncode:
            error = LifecycleError('Tool exited %d: %s' % (proc.returncode, _safe(tool_error_line(stderr) or strip_ansi(stderr)[-4000:], self.passwords)))
            error.server_error_code = dbops.error_code(stderr)
            raise error
        return stdout, stderr


def _invoke(job, tool, connection, args, script=None, **kwargs):
    _check_profile_policy(job)
    with _credentials(connection, tool) as (argv, env):
        return job.process_tool(argv + args, env, input_data=script, **kwargs)


def _shell_script(job, expression):
    # mysqlsh's JavaScript print() adds no line break: without one the marker
    # runs into the tool's next output and the session id is never read.
    return 'print(%s + session.getConnectionId() + "\\n");\n%s;\n' % (json.dumps(job.marker), expression)


def parse_upgrade_json(text):
    """Parse Shell's JSON report, including exit-0 errors and manual checks.

    Schema evidence: mysql-shell/modules/util/upgrade_checker/
    upgrade_check_formatter.cc (JSON formatter, checksPerformed/detectedProblems).
    Non-JSON banners are not silently accepted as a successful checker result.
    """
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise LifecycleError('MySQL Shell did not return a JSON upgrade report.')
    if not isinstance(value, dict) or not isinstance(value.get('checksPerformed'), list):
        raise LifecycleError('MySQL Shell upgrade report is missing checksPerformed.')
    if not isinstance(value.get('manualChecks', []), list):
        raise LifecycleError('Invalid MySQL Shell manualChecks.')
    checks = []
    for item in value['checksPerformed']:
        if not isinstance(item, dict) or not item.get('id') or not isinstance(item.get('detectedProblems', []), list):
            raise LifecycleError('Invalid check in MySQL Shell upgrade report.')
        problems = item.get('detectedProblems', [])
        for problem in problems:
            if not isinstance(problem, dict) or problem.get('level', '').upper() not in ('ERROR', 'WARNING', 'NOTICE'):
                raise LifecycleError('Invalid problem in MySQL Shell upgrade report.')
        levels = [p['level'].upper() for p in problems]
        severity = 'error' if item.get('status') in ('ERROR', 'CONFIGURATION_ERROR') or 'ERROR' in levels else (
            'warning' if 'WARNING' in levels else 'notice')
        checks.append({'id': item['id'], 'title': item.get('title', item['id']),
                       'status': item.get('status', 'OK'), 'severity': severity,
                       'affected_objects': [p.get('dbObject', '') for p in problems],
                       'problems': problems, 'doc_link': item.get('documentationLink', SHELL_DOC),
                       'description': item.get('description', ''), 'details': item})
    counts = {}
    for name in ('error', 'warning', 'notice'):
        count = value.get(name + 'Count')
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise LifecycleError('MySQL Shell upgrade report has invalid ' + name + 'Count.')
        counts[name] = count
    failed = counts['error'] > 0 or any(c['severity'] == 'error' for c in checks)
    return {'checks': checks, 'counts': counts, 'manual_checks': value.get('manualChecks', []),
            'status': 'issues_found' if failed else ('warnings' if counts['warning'] else 'completed')}


def _account(row):
    return "'%s'@'%s'" % (row[0], row[1]) if len(row) > 1 else str(row[0])


def _dotted(row):
    return '.'.join(str(c) for c in row if c not in (None, ''))


def reserved_words(source, target, source_reserved=None):
    """Words reserved in *target* but not in *source* (RESERVED_SINCE).

    *source_reserved*: the source's own information_schema.KEYWORDS reserved
    words (8.0+), which removes words the source already reserves.
    """
    words = []
    for version, added in RESERVED_SINCE:
        if source < version <= target:
            words.extend(w for w in added if w not in words)
    if source_reserved:
        words = [w for w in words if w not in source_reserved]
    return words


def _native_upgrade(conn, server, target):
    """A small, labelled subset of upgrade checks over metadata only.

    Each check names what it read. Version gates: a removed variable or a
    newly reserved word counts only when source < change <= target. System
    schemas are skipped: the upgrade itself converts them.
    """
    reader = Reader(conn, TAG)
    source = version_tuple(server['version'])
    checks = []
    user = 'NOT IN (' + ','.join("'%s'" % name for name in SYSTEM_SCHEMAS) + ')'

    def check(check_id, title, sql, severity, doc, description, formatter=_dotted):
        try:
            rows = reader.rows(sql)
            checks.append({'id': check_id, 'title': title, 'severity': severity,
                           'status': 'findings' if rows else 'passed',
                           'affected_objects': [formatter(r) for r in rows],
                           'description': description, 'doc_link': doc})
        except ConnectorError as exc:
            if dbops.error_code(str(exc)) in (2002, 2003, 2006, 2013) or 'timed out' in str(exc):
                raise
            checks.append({'id': check_id, 'title': title, 'severity': 'notice',
                           'status': 'not_checked', 'affected_objects': [],
                           'description': str(exc), 'doc_link': doc})
    removed = [name for name, gone in sorted(REMOVED_VARIABLES.items()) if source < gone <= target]
    if removed:
        values = ','.join(literal(name) for name in removed)
        doc = DOC + 'added-deprecated-removed.html'
        if source >= (8, 0, 0):
            # performance_schema.variables_info says where each value came
            # from; COMPILED is the default and DYNAMIC (SET GLOBAL) ends at
            # the restart into the new binaries.
            info = 'SELECT VARIABLE_NAME,VARIABLE_SOURCE,VARIABLE_PATH FROM performance_schema.variables_info WHERE VARIABLE_NAME IN (' + values + ') AND VARIABLE_SOURCE '
            check('removed_variables', 'Removed variables set in an option file or on the command line',
                  info + "NOT IN ('COMPILED','DYNAMIC','PERSISTED')", 'error', doc,
                  'The target does not have these variables. An unknown option in an option file or on the '
                  'command line stops mysqld from starting (unless written with the loose- prefix): remove them first.',
                  lambda r: '%s (%s%s)' % (r[0], r[1], (' ' + r[2]) if len(r) > 2 and r[2] else ''))
            check('removed_persisted_variables', 'Removed variables persisted with SET PERSIST',
                  'SELECT VARIABLE_NAME FROM performance_schema.persisted_variables WHERE VARIABLE_NAME IN (' + values + ')',
                  'warning', DOC + 'persisted-system-variables.html',
                  'The target logs a warning and ignores them at startup; RESET PERSIST removes them.',
                  lambda r: r[0])
        else:
            check('removed_variables', 'Removed variables (running values)',
                  'SHOW GLOBAL VARIABLES WHERE Variable_name IN (' + values + ')', 'warning', doc,
                  'MySQL 5.7 cannot say whether a value came from an option file: check the option files and '
                  'the command line for these names before upgrading.', lambda r: r[0])
    source_reserved = None
    if source >= (8, 0, 0):
        try:
            source_reserved = {row[0].upper() for row in reader.rows(
                'SELECT WORD FROM information_schema.KEYWORDS WHERE RESERVED = 1') if row and row[0]}
        except ConnectorError:
            source_reserved = None
    words = reserved_words(source, target, source_reserved)
    if words:
        listed = ','.join(literal(word) for word in words)
        check('reserved_identifiers', 'Names that are reserved words in the target',
              "SELECT 'schema',SCHEMA_NAME,'','' FROM information_schema.SCHEMATA WHERE SCHEMA_NAME " + user +
              ' AND UPPER(SCHEMA_NAME) IN (' + listed + ')'
              " UNION ALL SELECT LOWER(TABLE_TYPE),TABLE_SCHEMA,TABLE_NAME,'' FROM information_schema.TABLES WHERE TABLE_SCHEMA " + user +
              ' AND UPPER(TABLE_NAME) IN (' + listed + ')'
              " UNION ALL SELECT 'column',TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA " + user +
              ' AND UPPER(COLUMN_NAME) IN (' + listed + ')'
              " UNION ALL SELECT LOWER(ROUTINE_TYPE),ROUTINE_SCHEMA,ROUTINE_NAME,'' FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA " + user +
              ' AND UPPER(ROUTINE_NAME) IN (' + listed + ')'
              " UNION ALL SELECT 'trigger',TRIGGER_SCHEMA,TRIGGER_NAME,'' FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA " + user +
              ' AND UPPER(TRIGGER_NAME) IN (' + listed + ')'
              " UNION ALL SELECT 'event',EVENT_SCHEMA,EVENT_NAME,'' FROM information_schema.EVENTS WHERE EVENT_SCHEMA " + user +
              ' AND UPPER(EVENT_NAME) IN (' + listed + ')',
              'warning', DOC + 'keywords.html',
              'Reserved in the target (checked: ' + ', '.join(words) + '). Statements must quote these names with '
              'backticks; names in routine, view and trigger bodies are not parsed here.',
              lambda r: '%s (%s)' % (_dotted(r[1:]), r[0]))
    if source < (8, 0, 0) <= target:
        check('partition_engines', 'Partitioned tables without native partitioning',
              'SELECT DISTINCT p.TABLE_SCHEMA,p.TABLE_NAME,t.ENGINE FROM information_schema.PARTITIONS p '
              'JOIN information_schema.TABLES t ON t.TABLE_SCHEMA = p.TABLE_SCHEMA AND t.TABLE_NAME = p.TABLE_NAME '
              "WHERE p.PARTITION_NAME IS NOT NULL AND t.ENGINE NOT IN ('InnoDB','NDB','NDBCLUSTER')",
              'error', 'https://dev.mysql.com/doc/refman/8.0/en/partitioning-limitations-storage-engines.html',
              'MySQL 8.0 partitions only InnoDB and NDB tables.')
    if target >= (8, 0, 0):
        old = source < (8, 0, 0)
        check('utf8mb3', 'utf8 / utf8mb3 columns',
              'SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA ' + user +
              " AND CHARACTER_SET_NAME IN ('utf8','utf8mb3')",
              'warning' if old else 'notice', DOC + 'charset-unicode-utf8mb3.html',
              'utf8mb3 is deprecated and still supported in the target; plan a conversion to utf8mb4.')
        check('zero_date_defaults', 'Zero date defaults',
              'SELECT TABLE_SCHEMA,TABLE_NAME,COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA ' + user +
              " AND DATA_TYPE IN ('date','datetime','timestamp')"
              " AND (COLUMN_DEFAULT LIKE '0000-%' OR COLUMN_DEFAULT LIKE '%-00%')",
              'warning' if old else 'notice', DOC + 'sql-mode.html',
              'CREATE TABLE or ALTER TABLE with these defaults fails under the default sql_mode (strict, with '
              'NO_ZERO_DATE and NO_ZERO_IN_DATE). Stored row data is not scanned.')
    if target >= (8, 4, 0) and source < (9, 0, 0):
        check('native_password_users', 'mysql_native_password accounts',
              "SELECT User,Host FROM mysql.user WHERE plugin='mysql_native_password'",
              'error' if target >= (9, 0, 0) else 'warning', DOC + 'native-pluggable-authentication.html',
              'MySQL 8.4 disables mysql_native_password by default (mysql_native_password=ON enables it) and '
              '9.0 removes it. Needs SELECT on mysql.user.', _account)
    checks.append({'id': 'old_temporal', 'title': 'Old binary temporal formats', 'severity': 'notice',
                   'status': 'not_checked', 'affected_objects': [], 'doc_link': SHELL_DOC,
                   'description': 'Not reliably detectable from cheap column metadata. Use the full Shell checker; no CHECK TABLE or table scan was run.'})
    return checks


def upgrade_check(conn, target_version, connection=None, tools=None, config_path=None, timeout=300,
                  prefer_shell=True):
    """Read-only upgrade report. Shell exit status never substitutes for findings.

    MySQL Shell runs when it is installed and *prefer_shell*; otherwise the
    native subset (method ``native_subset``, ``partial: true``).
    """
    server = read_server(conn, connection)
    target = version_tuple(target_version)
    if target == (0, 0, 0):
        raise ValueError('Target version must contain major.minor.patch.')
    report = {'schema_version': 'upgrade-check-1.0', 'server': server,
              'target_version': '.'.join(str(n) for n in target), 'checks': [],
              'counts': {'error': 0, 'warning': 0, 'notice': 0}, 'manual_checks': []}
    if server['engine'] == 'mariadb':
        # MariaDB releases are numbered 10.x and later; 5.x through 9.x are
        # MySQL's numbers, and no tool upgrades MariaDB into MySQL.
        installed = version_tuple(re.sub(r'^\s*5\.5\.5-', '', server['version']))
        if target[0] < 10:
            raise Refused('%s is a MySQL version number, and MariaDB to MySQL is not an upgrade path. '
                          'Give a MariaDB target at or above the version of this server (%s).'
                          % (report['target_version'], '.'.join(str(n) for n in installed)))
        if target < installed:
            raise Refused('MariaDB upgrade checks need a target at or above the server version: '
                          'the server is %s and %s is older.'
                          % ('.'.join(str(n) for n in installed), report['target_version']))
        report.update(method='mariadb_upgrade_guidance', partial=True, status='guidance', limitations=[
            'MySQL Shell does not support MariaDB. Remote metadata is not a substitute for mariadb-upgrade.',
            'After installing upgraded binaries on the server host, run mariadb-upgrade with a 0600 option file. '
            'It repairs system tables and views and checks other databases; it is a write and needs separate confirmation.',
            '--check-if-upgrade-is-needed exits 0 when upgrade IS needed, 1 otherwise.'])
        return report
    source = version_tuple(server['version'])
    if source < (5, 7, 0) or source >= target or target < (8, 0, 0):
        raise Refused('MySQL upgrade checks require source >=5.7, target >=8.0 and source older than target.')
    if prefer_shell:
        tools = detect_tools() if tools is None else tools
    shell = _tool(tools, 'mysqlsh') if prefer_shell else {}
    if shell.get('available'):
        shell_version = version_tuple(shell.get('version', ''))
        if source >= shell_version or target[:2] > shell_version[:2]:
            raise Refused('MySQL Shell %s cannot check this pair: the server must be older than the Shell and the '
                          'target at most %d.%d. Install a newer MySQL Shell.' % ('.'.join(str(n) for n in shell_version), shell_version[0],
                                                  shell_version[1]))
        if connection is None:
            raise ValueError('Connection options are required to invoke MySQL Shell.')
        args = ['--js', '--', 'util', 'check-for-server-upgrade',
                '--target-version=' + report['target_version'], '--output-format=JSON']
        if config_path:
            args.append('--config-path=' + os.path.abspath(config_path))
        runner = LifecycleJob('upgrade_check', lambda job: None, connection)
        stdout, unused = _invoke(runner, dict(shell, name='mysqlsh'), connection, args, timeout=timeout)
        report.update(parse_upgrade_json(stdout))
        report.update(method='mysqlsh', partial=False, tool=dict(shell, name='mysqlsh'),
                      limitations=['Manual checks require review; this report does not perform an upgrade.'])
    else:
        checks = _native_upgrade(conn, server, target)
        counts = {severity: sum(len(c['affected_objects']) for c in checks if c['severity'] == severity)
                  for severity in ('error', 'warning', 'notice')}
        report.update(method='native_subset', partial=True, checks=checks, counts=counts,
                      status='issues_found' if counts['error'] else 'partial', limitations=[
            'Subset; install MySQL Shell for the full check. This does not establish that an upgrade is safe.',
            'Not checked: option file contents (only where MySQL 8.0+ says a removed variable came from), '
            'routine, view and trigger bodies, row data, old temporal storage, foreign keys, removed functions, '
            'and variables or keywords outside the short lists in myflames/lifecycle.py.',
            'Global sql_mode: ' + server['variables'].get('sql_mode', '(not read)')])
    return _report_safe(report, ((connection or {}).get('password'),))


def _absolute(path):
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if any(c in path for c in '\x00\n\r'):
        raise ValueError('Invalid directory path.')
    return path


def _output_path(path):
    """Absolute output directory with the links in its parent resolved.

    macOS /tmp and /var are links, so ancestors are resolved once here and the
    plan binds the real path the person confirms; the directory itself must
    not be a link (_empty_directory).
    """
    path = _absolute(path)
    return os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))


def _empty_directory(path):
    # The final component must not be a link, and the parent must still be the
    # real path resolved at preflight: a folder swapped for a link afterwards
    # (into another backup tree, say) refuses instead of being followed.
    if os.path.islink(path):
        raise Refused('The output directory is a symbolic link; choose the real path instead: ' + path)
    if os.path.realpath(os.path.dirname(path)) != os.path.dirname(path):
        raise Refused('A folder in the output path is a symbolic link that was not resolved at preflight; '
                      'nothing was written: ' + path)
    if os.path.lexists(path) and (not os.path.isdir(path) or os.listdir(path)):
        raise Refused('Output directory must not exist or must be empty; refusing to overwrite: ' + path)
    if not os.path.isdir(os.path.dirname(path)):
        raise Refused('Output parent directory must exist.')


def _names(values, label):
    result = []
    for value in values or ():
        if not isinstance(value, str) or not value or len(value) > 64 or any(c in value for c in '\x00\n\r'):
            raise ValueError('Invalid ' + label)
        if value.startswith('-'):
            raise ValueError(label + ' cannot begin with an option prefix for external tools.')
        if value not in result:
            result.append(value)
    return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _plan_binding(plan, binding_extra=None):
    return _hash({'operation': plan['operation'], 'request': plan['request'],
                  'identity': plan['server']['identity'], 'connection_key': plan['connection_key'],
                  'tool': plan['tool'], 'extra': binding_extra})


def _plan(operation, server, connection, request, tool, target, commands, checks, warnings, binding_extra=None):
    plan = {'schema_version': 'lifecycle-plan-1.0', 'operation': operation, 'request': request,
            'server': server, 'connection_key': dbops.connection_identity(connection), 'tool': tool,
            'target': target, 'commands': commands, 'checks': checks, 'warnings': warnings,
            'ready': not any(c['status'] == 'failed' for c in checks)}
    note = unsupported_version_note(server.get('version'), (server.get('variables') or {}).get('version_comment'))
    if note:
        plan['warnings'] = [note] + list(plan['warnings'])
    plan['binding'] = _plan_binding(plan, binding_extra)
    return plan


def _grants(conn):
    result = dbops.read_grants(conn, TAG)
    return result, dbops.global_privileges(result['grants'])


def _privileges_check(conn, groups):
    grants, have = _grants(conn)
    missing = [list(group) for group in groups if not any(name in have for name in group)]
    # SHOW GRANTS does not expand all active role/dynamic/object grants. A
    # missing grant is unknown, not a proven refusal. Tools preflight too.
    return _verification('privileges', 'passed' if not missing and grants['checked'] else 'not_checked',
                         'Privilege hint only; server/tool has the final decision.', missing_any_of=missing)


def _catalog(conn, schemas=None, tables=None, include_system=False):
    where = "TABLE_SCHEMA NOT IN ('mysql','sys','performance_schema','information_schema')"
    if include_system:
        where = "TABLE_SCHEMA NOT IN ('performance_schema','information_schema')"
    if schemas:
        where = 'TABLE_SCHEMA IN (' + ','.join(literal(s) for s in schemas) + ')'
    if tables:
        where += ' AND TABLE_NAME IN (' + ','.join(literal(t) for t in tables) + ')'
    return Reader(conn, TAG).rows('SELECT TABLE_SCHEMA,TABLE_NAME,TABLE_TYPE,ENGINE,COALESCE(DATA_LENGTH,0),'
                                  'COALESCE(INDEX_LENGTH,0) FROM information_schema.TABLES WHERE ' + where)


def _samples(conn, catalog, limit=3, max_bytes=1024 * 1024, seconds=5, job=None):
    """Bound exact COUNT(*) sampling by table size, never use TABLE_ROWS as proof.

    Metadata size is an estimate used ONLY to decide whether to attempt a
    count. Connector query timeouts still bound each actual scan.
    """
    samples, attempts = [], 0
    deadline = time.monotonic() + seconds
    reader = Reader(_BoundedConnection(conn, job, deadline), TAG)
    for row in catalog:
        if job:
            job.check_cancel()
        if attempts >= limit or time.monotonic() >= deadline:
            break
        if len(row) < 6 or row[2] != 'BASE TABLE':
            continue
        try:
            small = int(row[4]) + int(row[5]) <= max_bytes
        except (TypeError, ValueError):
            small = False
        if not small:
            continue
        attempts += 1
        try:
            counts = reader.rows('SELECT COUNT(*) FROM ' + ident(row[0]) + '.' + ident(row[1]))
            if not counts or not counts[0] or not str(counts[0][0]).isdigit():
                raise LifecycleError('COUNT(*) did not return an integer.')
            samples.append({'schema': row[0], 'table': row[1], 'count': int(counts[0][0])})
        except ConnectorError as exc:
            if job:
                job.check_cancel()
            samples.append({'schema': row[0], 'table': row[1], 'status': 'not_checked', 'reason': str(exc)})
    return samples


def _shell_dump_expression(request, dry_run=False):
    opts = {'threads': request['threads'], 'consistent': True, 'showProgress': True, 'dryRun': dry_run}
    opts.update(dump_options.shell_options(request.get('options', {})))
    directory = json.dumps(request['directory'])
    schemas, tables = request['schemas'], request['tables']
    if tables:
        return 'util.dumpTables(%s,%s,%s,%s)' % (json.dumps(schemas[0]), json.dumps(tables), directory, json.dumps(opts))
    if schemas:
        return 'util.dumpSchemas(%s,%s,%s)' % (json.dumps(schemas), directory, json.dumps(opts))
    opts['users'] = False  # account dumps can include credential hashes; explicit scope in manifest
    return 'util.dumpInstance(%s,%s)' % (directory, json.dumps(opts))


def prepare_dump(conn, connection, directory, schemas=None, tables=None, threads=4, tools=None, prefer_shell=True,
                 job=None, options=None):
    """Build a reviewable logical dump plan; Shell dryRun runs before confirmation."""
    server = read_server(conn, connection)
    directory = _output_path(directory)
    _empty_directory(directory)
    if isinstance(schemas, str) or isinstance(tables, str):
        raise ValueError('Schemas and tables must be lists, not strings.')
    schemas, tables = _names(schemas, 'schema'), _names(tables, 'table')
    if tables and len(schemas) != 1:
        raise ValueError('Table dumps require exactly one schema.')
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 64:
        raise ValueError('Threads must be 1..64.')
    tools = detect_tools() if tools is None else tools
    offers = offered_tools(server, tools)['dump']
    if not prefer_shell:
        offers = [name for name in offers if name != 'mysqlsh']
    tool = _pick(tools, offers, None) if offers else _pick(tools, ('mariadb-dump',) if server['engine'] == 'mariadb' else ('mysqldump',), server['engine'])
    request = dict(directory=directory, schemas=schemas, tables=tables, threads=threads, prefer_shell=prefer_shell)
    selected_options = dump_options.normalize(options, tool, bool(tables))
    if selected_options:
        request['options'] = selected_options
    catalog = _catalog(conn, schemas, tables, include_system=tool['name'] != 'mysqlsh' and not schemas)
    estimate = sum(int(row[4] or 0) + int(row[5] or 0) for row in catalog)
    available = shutil.disk_usage(os.path.dirname(directory)).free
    required = [('SELECT', 'ALL', 'ALL PRIVILEGES')]
    if selected_options.get('events', not (tool['name'] == 'mysqlsh' and tables)):
        required.append(('EVENT', 'ALL', 'ALL PRIVILEGES'))
    if selected_options.get('triggers', True):
        required.append(('TRIGGER', 'ALL', 'ALL PRIVILEGES'))
    checks = [_verification('free_space', 'passed' if available >= estimate else 'failed',
                            'Uncompressed table/index estimate; dump size can differ.',
                            estimated_bytes=estimate, available_bytes=available),
              _privileges_check(conn, required)]
    nontransactional = ['%s.%s' % (r[0], r[1]) for r in catalog if r[2] == 'BASE TABLE' and r[3] != 'InnoDB']
    warnings = ['A consistent snapshot protects InnoDB data only. Concurrent DDL must be avoided.',
                'The reviewed tool options determine whether routines, events and triggers are included. '
                'User accounts are not included in scoped Shell dumps.']
    if selected_options.get('network_compression'):
        warnings.append('Connection compression affects network traffic only; the backup file is not compressed.')
    if tool['name'] != 'mysqlsh' and not schemas:
        warnings.append('SQL instance dumps include mysql system tables and account hashes. Restore is scoped to the entire server.')
    if nontransactional:
        warnings.append('Non-InnoDB tables require a separate consistency strategy: ' + ', '.join(nontransactional))
    if tool['name'] == 'mysqlsh':
        gap = shell_version_gap(tool, server['version'])
        if gap == 'major':
            raise Refused(shell_version_message(tool, server['version'], gap))
        if gap:
            warnings.append(shell_version_message(tool, server['version'], gap))
        runner = job or LifecycleJob('dump_dry_run', lambda job: None, connection)
        # Some tool versions create their output directory even for dryRun.
        # Never give an unconfirmed tool the user's requested destination.
        with tempfile.TemporaryDirectory(prefix='myflames-dry-run-') as scratch:
            dry_request = dict(request, directory=os.path.join(scratch, 'dump'))
            _invoke(runner, tool, connection, ['--js'],
                    _shell_script(runner, _shell_dump_expression(dry_request, True)), timeout=120)
        checks.append(_verification('dry_run', 'passed', 'MySQL Shell dryRun completed.'))
        commands = [_shell_dump_expression(request)]
        warnings.append('Shell briefly takes a global read lock; writers wait behind long statements. '
                        'It may need RELOAD/FLUSH_TABLES, BACKUP_ADMIN, REPLICATION CLIENT or LOCK TABLES.')
    else:
        commands = [tool['name'] + ' --defaults-file=<0600 file> ' + ' '.join(_dump_args(request, tool)) +
                    ' > ' + os.path.join(directory, 'dump.sql')]
    try:
        running = Reader(conn, TAG).rows("SELECT ID,USER,HOST,TIME,STATE FROM information_schema.PROCESSLIST "
                                         "WHERE COMMAND <> 'Sleep' AND TIME >= 30 AND ID <> CONNECTION_ID() LIMIT 20")
        checks.append(_verification('long_statements', 'passed', 'Review long statements before locks.', sessions=running))
    except ConnectorError:
        checks.append(_verification('long_statements', 'not_checked', 'PROCESS access unavailable.'))
    return _plan('dump', server, connection, request, tool, directory, commands, checks, warnings)


def _dump_args(request, tool):
    args = ['--single-transaction', '--quick'] + dump_options.sql_flags(request.get('options', {}))
    if tool.get('engine') == 'mysql':
        args += ['--set-gtid-purged=OFF']
    if request['tables']:
        args += [request['schemas'][0]] + request['tables']
    elif request['schemas']:
        args += ['--databases'] + request['schemas']
    else:
        args += ['--all-databases']
    return args


def _manifest_path(path):
    """Portable relative manifest name; never interpret drives or traversal."""
    if (not isinstance(path, str) or not path or '\\' in path or ':' in path or
            any(c in path for c in '\x00\r\n') or any(p in ('', '.', '..') for p in path.split('/'))):
        raise Refused('Backup contains an unsafe relative file path.')
    return path.split('/')


def _file_records(directory, job=None, max_files=None, max_bytes=None, max_directories=None):
    records = []
    total_bytes, directories = 0, 0
    for root, folders, files in os.walk(directory, followlinks=False):
        directories += 1
        if max_directories is not None and directories > max_directories:
            raise Refused('Restore preflight exceeds the directory limit (%d).' % max_directories)
        if job:
            job.check_cancel()
        if any(os.path.islink(os.path.join(root, name)) for name in folders):
            raise Refused('Backup contains a directory symlink.')
        counted_files = len(files) - int(root == directory and MANIFEST in files)
        if max_files is not None and len(records) + counted_files > max_files:
            raise Refused('Restore preflight exceeds the file limit (%d).' % max_files)
        for name in sorted(files):
            if job:
                job.check_cancel()
            path = os.path.join(root, name)
            if path == os.path.join(directory, MANIFEST):
                continue
            if max_files is not None and len(records) >= max_files:
                raise Refused('Restore preflight exceeds the file limit (%d).' % max_files)
            if os.path.islink(path) or not os.path.isfile(path):
                raise Refused('Backup contains a symlink or non-regular file.')
            if max_bytes is not None and total_bytes + os.path.getsize(path) > max_bytes:
                raise Refused('Restore preflight exceeds the byte limit (%d bytes).' % max_bytes)
            digest = hashlib.sha256()
            size = 0
            with open(path, 'rb') as handle:
                while True:
                    # Read at most one byte beyond the remaining budget, even
                    # when a file grows after the initial stat.
                    chunk = 1024 * 1024 if max_bytes is None else min(1024 * 1024, max_bytes - total_bytes + 1)
                    data = handle.read(chunk)
                    if not data:
                        break
                    total_bytes += len(data)
                    size += len(data)
                    if max_bytes is not None and total_bytes > max_bytes:
                        raise Refused('Restore preflight exceeds the byte limit (%d bytes).' % max_bytes)
                    if job:
                        job.check_cancel()
                    digest.update(data)
            relative = '/'.join(os.path.relpath(path, directory).split(os.sep))
            _manifest_path(relative)
            records.append({'path': relative, 'size_bytes': size,
                            'sha256': digest.hexdigest()})
    return sorted(records, key=lambda r: r['path'])


def _save_manifest(directory, manifest):
    path = os.path.join(directory, MANIFEST)
    # Never replace an existing manifest, even after a failed operation.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write('\n')


def _read_manifest_value(directory):
    """Read only the manifest; checksums are validated separately or on copy."""
    directory = _absolute(directory)
    if os.path.islink(os.path.join(directory, MANIFEST)):
        raise Refused('Manifest cannot be a symlink.')
    with open(os.path.join(directory, MANIFEST), 'r', encoding='utf-8') as handle:
        text = handle.read(16 * 1024 * 1024 + 1)
        if len(text) > 16 * 1024 * 1024:
            raise Refused('Backup manifest exceeds the 16 MiB metadata limit.')
        manifest = json.loads(text)
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 'lifecycle-manifest-1.0' or manifest.get('complete') is not True:
        raise Refused('Backup manifest is unsupported or marks a partial dump.')
    if not isinstance(manifest.get('files'), list) or not manifest['files']:
        raise Refused('Backup contains no files.')
    seen = set()
    for record in manifest['files']:
        if not isinstance(record, dict):
            raise Refused('Invalid backup file record.')
        _manifest_path(record.get('path'))
        if (record['path'] == MANIFEST or record['path'] in seen or
                type(record.get('size_bytes')) is not int or record['size_bytes'] < 0 or
                not re.fullmatch('[0-9a-f]{64}', str(record.get('sha256', '')))):
            raise Refused('Invalid or duplicate backup file record.')
        seen.add(record['path'])
    return manifest


def read_manifest(directory, job=None):
    """Validate a completed backup and every file checksum before restore."""
    manifest = _read_manifest_value(directory)
    if manifest['files'] != _file_records(directory, job, MAX_RESTORE_FILES, MAX_RESTORE_BYTES,
                                         MAX_RESTORE_DIRECTORIES):
        raise Refused('Backup file list, sizes or checksums no longer match the manifest.')
    return manifest


def _server_resource(server):
    identity = server['identity']
    value = (['uuid', str(identity['server_uuid']).lower()] if identity.get('server_uuid') else
             ['server', identity.get('hostname'), identity.get('port'), identity.get('server_id'), identity.get('datadir')])
    return 'server:' + _hash(value)


def _resources(plan, server=None):
    # SQL is not confined to a default schema. All maintenance operations use
    # an exclusive whole-server reservation, independent of displayed target.
    result = {_server_resource(server or plan['server'])}
    directory = plan.get('request', {}).get('directory')
    if directory:
        result.add('directory:' + os.path.normcase(os.path.realpath(directory)))
    if plan.get('donor'):
        result.add(_server_resource(plan['donor']))
    return result


class _Reservations(object):
    """Process-wide reservations shared by CLI jobs and workspace registries."""
    def __init__(self):
        self.lock, self.owners = threading.Lock(), {}

    @contextlib.contextmanager
    def hold(self, owner, resources):
        with self.lock:
            if any(key in self.owners and self.owners[key] != owner for key in resources):
                raise Refused('Another lifecycle operation is using this server or backup directory.')
            for key in resources:
                self.owners[key] = owner
        try:
            yield
        finally:
            with self.lock:
                for key in resources:
                    if self.owners.get(key) == owner:
                        del self.owners[key]


_RESERVATIONS = _Reservations()


def _policy(connection, safety):
    # Adapters may supply a live policy lookup for work queued in a thread.
    if callable(safety):
        safety = safety()
        if not isinstance(safety, dict):
            raise Refused('The connection write policy is unavailable; nothing was sent.')
    safety = safety if safety is not None else dbops.connection_safety(connection=connection)
    return dbops.decide('risky', safety.get('type', 'production'), safety.get('read_only') is True,
                        safety.get('profile_name'))


def bind_profile_policy(job, connection, safety, get_profile, write=True):
    """Re-read trusted saved policy at job and tool boundaries.

    The adapter supplies its store lookup; the engine never imports a UI or
    CLI store. Donors are bound too, but their read-only flag allows reads.
    A rename is harmless; endpoint or policy edits require a fresh review.
    """
    connection, safety = copy.deepcopy(connection), copy.deepcopy(safety)
    profile_id = connection.get('profile_id')
    profile = get_profile(profile_id) if profile_id else None
    fields = ('host', 'port', 'user', 'ssl_mode', 'ssl_ca')
    endpoint = tuple((profile or {}).get(key) for key in fields)
    previous = getattr(job, 'policy_check', None)

    def check():
        if previous is not None:
            previous()
        if profile_id:
            current = get_profile(profile_id)
            if not current:
                raise Refused('The saved connection was removed. Review the operation again.')
            fresh = dbops.connection_safety(current, connection)
            if (tuple(current.get(key) for key in fields) != endpoint or
                    any(fresh.get(key) != safety.get(key) for key in
                        ('type', 'read_only', 'endpoint_matches'))):
                raise Refused('The saved connection or its write policy changed. Review the operation again.')
        else:
            fresh = safety
        if write and fresh.get('read_only'):
            raise Refused('The connection is read-only; the operation was refused.')

    job.policy_check = check


def _check_profile_policy(job):
    check = getattr(job, 'policy_check', None)
    if check is not None:
        check()


def restore_target_mismatch(target):
    """Shared unsent refusal text for the API, CLI and direct restore callers."""
    return 'Nothing was restored: the typed target does not match %s.' % target


def validate_restore_target(plan, typed_target):
    if not isinstance(plan.get('target'), str) or typed_target != plan['target']:
        raise RestoreTargetMismatch(restore_target_mismatch(plan.get('target')))


def _write_job(operation, connection, plan, rebuild, action, confirmed, typed_target, audit,
               safety=None, factory=LifecycleConnection, start=True, passwords=(), typed_acknowledgement=None):
    # Deep-copy the reviewed value so a caller cannot alter it during the job.
    plan = copy.deepcopy(plan)
    decision = _policy(connection, safety)
    # Reject a mistyped restore target synchronously, before creating a job or
    # opening its connection. Policy refusal still takes precedence.
    if operation == 'restore' and not dbops.gate(decision, confirmed=confirmed):
        try:
            validate_restore_target(plan, typed_target)
        except Refused as exc:
            if audit is not None:
                secrets = (connection.get('password'),) + tuple(passwords)
                audit.outcome(_safe(plan.get('commands', []), secrets),
                              'refused', outcome='target_mismatch',
                              message=_safe(str(exc), secrets),
                              target=_safe(plan.get('server', {}).get('identity'), secrets))
            raise
    def work(job):
        statements = plan.get('commands', [])
        if audit is None:
            raise Refused('An AuditTrail is required; nothing was sent.')
        def refuse(message, status='refused'):
            audit.outcome(_safe(statements, job.passwords), 'refused', outcome=status,
                          message=_safe(message, job.passwords),
                          target=_safe(plan.get('server', {}).get('identity'), job.passwords))
            raise Refused(message)
        try:
            # A queued job must not retain the decision made when it was built.
            gate = dbops.gate(_policy(connection, safety), confirmed=confirmed)
            if gate:
                refuse(gate[1], gate[0])
            if plan.get('operation') != operation or plan.get('schema_version') != 'lifecycle-plan-1.0':
                refuse('Wrong lifecycle plan; nothing was sent.')
            if typed_target != plan.get('target'):
                refuse('Type the exact target from the preflight; nothing was sent.')
            _check_profile_policy(job)
            with contextlib.ExitStack() as stack:
                conn = _BoundedConnection(stack.enter_context(_connect(factory, connection)), job)
                identity = read_server(conn, connection)
                if identity['identity'] != plan.get('server', {}).get('identity'):
                    refuse('Server identity changed before preflight; nothing was sent.')
                stack.enter_context(_RESERVATIONS.hold(job.id, _resources(plan, identity)))
                fresh = rebuild(conn, job)
                if (fresh['binding'] != plan.get('binding') or fresh['target'] != plan.get('target')
                        or fresh['commands'] != plan.get('commands')):
                    refuse('Server identity, tool, backup or request changed; nothing was sent.')
                if not fresh['ready']:
                    refuse('Preflight has failed checks; nothing was sent.')
                if fresh.get('required_acknowledgement') and typed_acknowledgement != fresh['required_acknowledgement']:
                    refuse('Type the mysql system-table acknowledgement from the preflight; nothing was sent.')
                if read_server(conn, connection)['identity'] != fresh['server']['identity']:
                    refuse('Server identity changed after preflight; nothing was sent.')
                job.check_cancel()
                _check_profile_policy(job)
                gate = dbops.gate(_policy(connection, safety), confirmed=confirmed)
                if gate:
                    refuse(gate[1], gate[0])
                review_consumer = getattr(job, 'review_consumer', None)
                if review_consumer is not None:
                    review_consumer()
                audit.server(fresh['server']['engine'], fresh['server']['version'])
                audit.attempt(_safe(fresh['commands'], job.passwords),
                              target=_safe(fresh['server']['identity'], job.passwords),
                              verified=['connection_key', 'server_identity', 'request', 'tool', 'preflight'])
                try:
                    result = action(job, conn, fresh)
                    job.check_cancel()
                except BaseException as exc:
                    if job.cancelled.is_set() or dbops.may_have_run(exc):
                        audit.complete('unknown', outcome='cancelled' if job.cancelled.is_set() else 'unknown',
                                       message=_safe(str(exc), job.passwords))
                    elif isinstance(exc, Refused):
                        audit.complete('refused', outcome='policy_changed',
                                       message=_safe(str(exc), job.passwords))
                    else:
                        audit.fail(LifecycleError(_safe(str(exc), job.passwords)))
                    raise
                failed = any(v['status'] == 'failed' for v in result.get('verification', []))
                audit.complete('error' if failed else 'ok', outcome='verification_failed' if failed else result['status'],
                               after=_safe(result.get('verification'), job.passwords))
                result['audit_id'] = audit.id
                return result
        except BaseException as exc:
            if not audit.finished:
                if job.cancelled.is_set():
                    audit.outcome(_safe(statements, job.passwords), 'declined', outcome='cancelled',
                                  message='Cancelled during preflight; nothing was sent.')
                elif isinstance(exc, Refused):
                    audit.outcome(_safe(statements, job.passwords), 'refused', outcome='preflight', message=_safe(str(exc), job.passwords))
                else:
                    audit.fail(LifecycleError(_safe(str(exc), job.passwords)), _safe(statements, job.passwords))
            raise
    job = LifecycleJob(operation, work, connection, factory, passwords)
    job.audit, job.audit_statements = audit, plan.get('commands', [])
    job.resources = _resources(plan) if plan.get('server', {}).get('identity') else set()
    return job.start() if start else job


def dump_job(connection, plan, confirmed=False, typed_target=None, audit=None, safety=None,
             factory=LifecycleConnection, start=True):
    connection = copy.deepcopy(connection)
    request = copy.deepcopy(plan.get('request', {}))
    def rebuild(conn, job):
        return prepare_dump(conn, connection, tools=detect_tools(job=job), job=job, **request)
    def action(job, conn, fresh):
        return _perform_dump(job, conn, fresh, connection)
    return _write_job('dump', connection, plan, rebuild, action, confirmed, typed_target, audit, safety, factory, start)


def _perform_dump(job, conn, plan, connection):
    request, tool = plan['request'], plan['tool']
    directory = request['directory']
    job.partial_state = ('An incomplete dump may remain in %s; its manifest marks it incomplete and restore '
                         'refuses it. Remove the directory before dumping there again.' % directory)
    _empty_directory(directory)
    if not os.path.exists(directory):
        os.mkdir(directory, 0o700)
    os.chmod(directory, 0o700)
    started = now()
    catalog, samples = [], []
    records = []
    complete = False
    verification = []
    def manifest_value(records):
        selected_options = request.get('options', {})
        return {'schema_version': 'lifecycle-manifest-1.0', 'source': plan['server'],
                'scope': {'schemas': request['schemas'], 'tables': request['tables']}, 'tool': tool,
                'options': {'consistent': True, 'threads': request['threads'], 'users': tool['name'] != 'mysqlsh' and not request['schemas'],
                            'routines': selected_options.get('routines', not (tool['name'] == 'mysqlsh' and request['tables'])),
                            'events': selected_options.get('events', not (tool['name'] == 'mysqlsh' and request['tables'])),
                            'triggers': selected_options.get('triggers', True),
                            'selected': selected_options,
                            'tool_options': _shell_dump_expression(request) if tool['name'] == 'mysqlsh' else _dump_args(request, tool)},
                'started_at': started, 'finished_at': now(), 'files': records, 'samples': samples,
                'object_catalog': catalog, 'verification': verification, 'complete': complete}
    try:
        catalog = _catalog(conn, request['schemas'], request['tables'],
                           include_system=tool['name'] != 'mysqlsh' and not request['schemas'])
        samples = _samples(conn, catalog, job=job)
        if tool['name'] == 'mysqlsh':
            _invoke(job, tool, connection, ['--js'], _shell_script(job, _shell_dump_expression(request)))
        else:
            fd = os.open(os.path.join(directory, 'dump.sql'), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                _invoke(job, tool, connection, _dump_args(request, tool), output_file=handle)
        records = _file_records(directory, job)
        present = bool(records) and any(r['size_bytes'] > 0 for r in records)
        if tool['name'] == 'mysqlsh':
            present = present and os.path.isfile(os.path.join(directory, '@.done.json'))
        verification.append(_verification('files', 'passed' if present else 'failed',
                                           'Non-empty output and Shell completion marker when applicable.'))
        after = _samples(conn, catalog, job=job)
        verification.append(_verification('source_sample_stability', 'passed' if samples and samples == after and
                                           all('count' in sample for sample in samples) else 'not_checked',
                                           'Exact counts before/after; concurrent changes may invalidate comparison.', samples=after))
        verification.append(_verification('dump_rows', 'not_checked',
                                           'Restore into an isolated target to compare row counts.'))
        complete = present
    finally:
        # An interrupted dump is unusable; do not spend hours hashing it on
        # cancellation just to write an incomplete manifest.
        complete = complete and bool(records) and not job.cancelled.is_set()
        if not complete:
            records = []
        manifest = _report_safe(manifest_value(records), job.passwords)
        _save_manifest(directory, manifest)
        if not complete:
            job.emit('Partial dump retained at ' + directory)
    return {'status': 'completed' if complete else 'verification_failed', 'directory': directory,
            'verification': verification, 'manifest': manifest}


def _same_server(left, right):
    a, b = left['identity'], right['identity']
    if a.get('server_uuid') and b.get('server_uuid'):
        return a['server_uuid'] == b['server_uuid']
    fields = ('hostname', 'port', 'server_id', 'datadir')
    return all(a.get(k) == b.get(k) for k in fields)


def _load_progress_file(request, server):
    return request['directory'] + '.load-' + _hash(server['identity'])[:16] + '.json'


def _staging_check(manifest, directory):
    # SQL needs a session-id prefix; Shell needs every immutable input file.
    required = sum(r['size_bytes'] for r in manifest['files']) + 1024
    available = shutil.disk_usage(directory).free
    return _verification('staging_space', 'passed' if available >= required else 'failed',
                         'Private restore snapshot in %s requires %d bytes; %d bytes available.' %
                         (directory, required, available), directory=directory,
                         required_bytes=required, available_bytes=available)


def _check_reviewed_manifest(directory, manifest_hash, job):
    job.check_cancel()
    if _hash(_read_manifest_value(directory)) != manifest_hash:
        raise Refused('Backup manifest changed after review; no restore statement was sent.')


def _copy_reviewed_file(directory, record, output, job):
    path = os.path.join(directory, *_manifest_path(record['path']))
    # Refuse links at every level. Validate the open descriptor as well as
    # the pathname, then check bytes against the reviewed record, not a new
    # manifest supplied by a concurrent backup replacement.
    relative = directory
    for part in _manifest_path(record['path']):
        relative = os.path.join(relative, part)
        if os.path.islink(relative):
            raise Refused('Backup input became a symbolic link.')
    digest, size = hashlib.sha256(), 0
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    with os.fdopen(os.open(path, flags), 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise Refused('Backup input is not a regular file.')
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            job.check_cancel()
            size += len(chunk)
            if size > record['size_bytes']:
                raise Refused('Backup grew while staging; no restore statement was sent.')
            digest.update(chunk)
            output.write(chunk)
    if size != record['size_bytes'] or digest.hexdigest() != record['sha256']:
        raise Refused('Backup changed while staging; no restore statement was sent.')


def prepare_restore(conn, connection, directory, schema=None, tools=None, allow_source_restore=False,
                    existing_objects='refuse', threads=4, resume=False, staging_directory=None, job=None):
    """Preflight checksum, target identity, local_infile and duplicate objects.

    SQL dumps can contain qualified DDL/USE and are NOT confined by --database.
    Confirmation always names the whole server (and schema when supplied).
    Only this backend's completed, verified manifests are accepted.
    """
    server = read_server(conn, connection)
    directory = os.path.realpath(_absolute(directory))
    manifest = read_manifest(directory, job)
    staging_directory = os.path.realpath(_absolute(staging_directory or tempfile.gettempdir()))
    if not os.path.isdir(staging_directory):
        raise ValueError('Restore staging directory must exist.')
    try:
        inside_backup = os.path.commonpath([directory, staging_directory]) == directory
    except ValueError:
        inside_backup = False  # Different Windows volumes.
    if inside_backup:
        raise ValueError('Restore staging directory must be outside the backup.')
    if manifest['tool']['name'] == 'mariadb-backup':
        raise Refused('Physical copy-back requires a stopped server and an empty datadir on the server host; use physical_backup_guidance.')
    if schema is None and manifest['scope']['tables'] and len(manifest['scope']['schemas']) == 1:
        schema = manifest['scope']['schemas'][0]
    if schema is not None:
        schema = _names([schema], 'schema')[0]
    if existing_objects not in ('refuse', 'ignore', 'drop'):
        raise ValueError('Choose refuse, ignore or drop existing objects explicitly.')
    if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 64:
        raise ValueError('Threads must be 1..64.')
    tools = detect_tools() if tools is None else tools
    shell_dump = manifest['tool']['name'] == 'mysqlsh'
    checks = [_staging_check(manifest, staging_directory)]
    warnings = ['Restore executes DDL and DML and can overwrite data. It cannot be undone.',
                'SQL dumps can address other schemas; selecting a default schema is not a sandbox.']
    source, scope = manifest['source'], manifest['scope']
    same_source = _same_server(source, server)
    checks.append(_verification('source_identity', 'failed' if same_source and allow_source_restore is not True else 'passed',
                                'Target is the source; explicitly choose allow_source_restore.' if same_source else 'Target differs from source.'))
    if source['engine'] != server['engine']:
        checks.append(_verification('engine', 'failed', 'Cross-engine restores are not validated; refused.'))
    if schema and not shell_dump and scope['schemas'] != [schema]:
        raise Refused('A SQL dump containing CREATE DATABASE/USE or qualified DDL cannot be remapped to a different schema.')
    if schema and shell_dump and (len(scope['schemas']) != 1 or not scope['tables']):
        raise Refused('Shell schema remapping is offered only for single-schema table dumps.')
    if shell_dump:
        tool = _pick(tools, ('mysqlsh',))
        if server['engine'] != 'mysql' or version_tuple(server['version']) < (5, 7, 0):
            checks.append(_verification('load_target', 'failed', 'Shell loads are offered only for MySQL >=5.7; MariaDB is untested.'))
        checks.append(_verification('local_infile', 'passed' if server['variables'].get('local_infile', '').upper() in ('ON', '1') else 'failed',
                                    'loadDump requires local_infile=ON. No SET GLOBAL is sent automatically.'))
    else:
        if not any(r['path'] == 'dump.sql' for r in manifest['files']):
            raise Refused('SQL backup manifest has no dump.sql file.')
        tool = _pick(tools, ('mariadb', 'mysql') if server['engine'] == 'mariadb' else ('mysql',), server['engine'])
        if existing_objects == 'ignore':
            raise Refused('Ignoring existing objects is supported only by Shell loads; SQL dumps include DROP TABLE.')
    instance_sql = not shell_dump and not scope['schemas']
    system_sql = not shell_dump and (instance_sql or 'mysql' in scope['schemas'])
    catalog = _catalog(conn, [schema] if schema else scope['schemas'], scope['tables'], include_system=instance_sql)
    duplicates = ['%s.%s' % (r[0], r[1]) for r in catalog]
    checks.append(_verification('existing_objects', 'failed' if duplicates and existing_objects == 'refuse' else 'passed',
                                'Existing objects require an explicit drop choice (Shell also supports ignore).', objects=duplicates))
    if system_sql:
        checks.append(_verification('mysql_system_tables', 'passed' if existing_objects == 'drop' else 'failed',
                                    'This SQL backup can replace mysql system tables and accounts. Choose drop and type the separate acknowledgement.'))
    checks.append(_privileges_check(conn, [('CREATE', 'ALL', 'ALL PRIVILEGES'), ('INSERT', 'ALL', 'ALL PRIVILEGES'),
                                         ('ALTER', 'ALL', 'ALL PRIVILEGES'), ('DROP', 'ALL', 'ALL PRIVILEGES')]))
    request = dict(directory=directory, schema=schema, allow_source_restore=allow_source_restore is True,
                   existing_objects=existing_objects, threads=threads, resume=resume is True,
                   staging_directory=staging_directory)
    target = '%s:%s' % (connection.get('host', '127.0.0.1'), connection.get('port', 3306))
    if schema:
        target += '/' + schema
    if shell_dump:
        progress_file = _load_progress_file(request, server)
        request['progress_file'] = progress_file
        progress_unsafe = os.path.islink(progress_file) or (os.path.lexists(progress_file) and resume is not True)
        checks.append(_verification('load_progress', 'failed' if progress_unsafe else 'passed',
                                    'Existing progress requires resume=True; the file is retained for cancellation recovery.'))
    commands = [_load_expression(request)] if shell_dump else [tool['name'] + ' --defaults-file=<0600 file> --binary-mode --comments < ' + os.path.join(directory, 'dump.sql')]
    if shell_dump and all(c['status'] != 'failed' for c in checks):
        runner = job or LifecycleJob('restore_dry_run', lambda job: None, connection)
        with tempfile.TemporaryDirectory(prefix='myflames-dry-run-') as scratch:
            dry_request = dict(request, progress_file=os.path.join(scratch, 'progress.json'))
            _invoke(runner, tool, connection, ['--js'],
                    _shell_script(runner, _load_expression(dry_request, True)), timeout=120)
        checks.append(_verification('dry_run', 'passed', 'MySQL Shell loadDump dryRun completed.'))
    plan = _plan('restore', server, connection, request, tool, target, commands, checks, warnings,
                 binding_extra=_hash(manifest))
    plan.update(manifest=manifest, manifest_hash=_hash(manifest),
                required_acknowledgement=('REPLACE mysql SYSTEM TABLES ON ' + target) if system_sql else None)
    return plan


def _load_expression(request, dry_run=False):
    options = {'threads': request['threads'], 'dryRun': dry_run, 'showProgress': True, 'loadUsers': False,
               # Keep resume state beside, rather than inside, the immutable backup.
               'progressFile': request.get('progress_file', '')}
    if request['existing_objects'] != 'refuse':
        options['ignoreExistingObjects' if request['existing_objects'] == 'ignore' else 'dropExistingObjects'] = True
    if request.get('schema'):
        options['schema'] = request['schema']
    return 'util.loadDump(%s,%s)' % (json.dumps(request['directory']), json.dumps(options))


def restore_job(connection, plan, confirmed=False, typed_target=None, audit=None, safety=None,
                factory=LifecycleConnection, start=True, typed_acknowledgement=None):
    connection = copy.deepcopy(connection)
    request = copy.deepcopy(plan.get('request', {}))
    prepare_request = {k: v for k, v in request.items() if k != 'progress_file'}
    def rebuild(conn, job):
        return prepare_restore(conn, connection, tools=detect_tools(job=job), job=job, **prepare_request)
    def action(job, conn, fresh):
        manifest = fresh['manifest']
        _check_reviewed_manifest(request['directory'], fresh['manifest_hash'], job)
        if _staging_check(manifest, request['staging_directory'])['status'] != 'passed':
            raise Refused('Insufficient free space for the reviewed restore snapshot.')
        tool = fresh['tool']
        if tool['name'] == 'mysqlsh':
            job.partial_state = ('Part of the dump may be loaded on %s. MySQL Shell recorded its progress in %s: '
                                 'prepare the restore again with resume to continue.' % (fresh['target'],
                                                                                         request['progress_file']))
        else:
            job.partial_state = ('Part of the dump may be loaded on %s (statements run in order and each one '
                                 'commits). Check the schemas before running it again.' % fresh['target'])
        if tool['name'] == 'mysqlsh':
            # Shell reads chunks lazily. Loading the original directory would
            # leave a TOCTOU window even after its preflight checksums passed.
            with tempfile.TemporaryDirectory(prefix='myflames-restore-', dir=request['staging_directory']) as staged:
                for record in manifest['files']:
                    destination = os.path.join(staged, *_manifest_path(record['path']))
                    os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
                    with open(destination, 'xb') as output:
                        _copy_reviewed_file(request['directory'], record, output, job)
                _check_reviewed_manifest(request['directory'], fresh['manifest_hash'], job)
                staged_request = dict(request, directory=staged)
                _invoke(job, tool, connection, ['--js'], _shell_script(job, _load_expression(staged_request)))
        else:
            args = ['--batch', '--skip-column-names', '--unbuffered'] + list(dbops.WRITE_CLIENT_FLAGS)
            if request.get('schema'):
                args += ['--database=' + request['schema']]
            # An stdin prefix prints the actual executing session id before
            # streaming the dump. No password or SQL is put in -e/argv.
            with tempfile.TemporaryFile('w+b', dir=request['staging_directory']) as sql:
                sql.write((TAG + "SELECT CONCAT('%s',CONNECTION_ID());\n" % job.marker).encode('utf-8'))
                record = next(r for r in manifest['files'] if r['path'] == 'dump.sql')
                _copy_reviewed_file(request['directory'], record, sql, job)
                _check_reviewed_manifest(request['directory'], fresh['manifest_hash'], job)
                sql.seek(0)
                _invoke(job, tool, connection, args, input_file=sql)
        after_server = read_server(conn, connection)
        if after_server['identity'] != fresh['server']['identity']:
            raise LifecycleError('Target identity changed during restore; verification refused.')
        verification = []
        catalog = _catalog(conn, [request['schema']] if request.get('schema') else manifest['scope']['schemas'], manifest['scope']['tables'],
                           include_system=tool['name'] != 'mysqlsh' and not manifest['scope']['schemas'])
        expected = {(request.get('schema') or r[0], r[1], r[2]) for r in manifest.get('object_catalog', [])}
        actual = {(r[0], r[1], r[2]) for r in catalog}
        verification.append(_verification('object_presence', 'passed' if expected <= actual else 'failed',
                                           'Schema/table/view presence; routines/events/triggers/users not verified.'))
        for sample in manifest.get('samples', []):
            job.check_cancel()
            if 'count' not in sample:
                verification.append(_verification('row_count', 'not_checked', sample.get('reason', 'Source count unavailable.'),
                                                   schema=sample['schema'], table=sample['table']))
                continue
            name = ident(request.get('schema') or sample['schema']) + '.' + ident(sample['table'])
            try:
                rows = Reader(conn, TAG).rows('SELECT COUNT(*) FROM ' + name)
                count = int(rows[0][0])
                verification.append(_verification('row_count', 'passed' if count == sample['count'] else 'failed',
                                                   'Exact count versus source sample; active source data may have changed.',
                                                   schema=sample['schema'], table=sample['table'], expected=sample['count'], actual=count))
            except (ConnectorError, ValueError, IndexError) as exc:
                verification.append(_verification('row_count', 'not_checked', str(exc), schema=sample['schema'], table=sample['table']))
        verification.append(_verification('checksums_and_other_objects', 'not_checked',
                                           'No CHECKSUM TABLE or account/routine/event/trigger comparison was run.'))
        return {'status': 'verification_failed' if any(v['status'] == 'failed' for v in verification) else 'completed',
                'verification': verification}
    return _write_job('restore', connection, plan, rebuild, action, confirmed, typed_target, audit, safety, factory, start,
                      typed_acknowledgement=typed_acknowledgement)


def physical_backup_guidance(conn, connection, directory):
    """Physical operations are host-file operations; never infer locality from IP alone.

    Detect a Unix socket + accessible datadir. This is a necessary condition,
    not proof across containers/mount namespaces; explicit same-host approval
    is still required by a host runner. This backend never copy-backs into a
    live server's datadir or silently starts/stops a server.
    """
    server = read_server(conn, connection)
    variables = server['variables']
    sock = connection.get('socket')
    datadir = variables.get('datadir', '')
    local = False
    if sock and variables.get('socket') and os.path.realpath(sock) == os.path.realpath(variables['socket']):
        try:
            local = stat.S_ISSOCK(os.stat(sock).st_mode) and os.path.isdir(datadir)
        except OSError:
            pass
    directory = _absolute(directory)
    return {'schema_version': 'lifecycle-physical-1.0', 'server': server, 'local_files_detected': local,
            'executable': False,
            'reason': 'Host filesystem identity and stopped-server restore require a host operator; no physical command was executed.',
            'commands': ['mariadb-backup --defaults-file=<0600 file> --backup --target-dir=' + directory,
                         'mariadb-backup --prepare --target-dir=' + directory,
                         'mariadb-backup --copy-back --target-dir=' + directory],
            'warnings': ['MariaDB only. Backup and mariadb-upgrade run on the server host.',
                         'Prepare before copy-back; stop the server and use an empty datadir, fix ownership, then start.',
                         'Use mariadb_backup_binlog_info to configure replication.']}


def _clone_facts(conn, connection):
    server = read_server(conn, connection)
    reader = Reader(conn, TAG)
    plugins = reader.rows("SELECT PLUGIN_NAME,PLUGIN_STATUS,PLUGIN_LIBRARY FROM information_schema.PLUGINS WHERE PLUGIN_STATUS='ACTIVE'")
    collations = reader.rows('SELECT COLLATION_NAME FROM information_schema.COLLATIONS')
    try:
        effective = privileges.read_privileges(conn)
        roles = effective.get('current_roles', [])
        if server['engine'] == 'mysql' and roles:
            try:
                # Explicit expansion also works on versions where plain SHOW
                # GRANTS lists role assignments without their privileges.
                rows = reader.rows('SHOW GRANTS FOR CURRENT_USER() USING ' + ', '.join(
                    privileges.quote_account(r['name'], r.get('host', '%')) for r in roles))
                expanded = privileges.parse_grants([r[0] for r in rows if r], server['engine'], server['version'])
                if not rows:
                    expanded['checked'] = False
                effective.update(expanded)
            except ConnectorError:
                effective['checked'] = False
    except ConnectorError:
        effective = {'checked': False}
    return {'server': server, 'plugins': plugins, 'collations': [r[0] for r in collations],
            'privileges': effective}


def _quoted_clone(value):
    # CLONE's grammar requires TEXT_STRING literals, so perf_common.literal's
    # CONVERT expression is invalid here. Reject backslashes so this quote is
    # safe under either sql_mode; normal passwords with quotes remain valid.
    if not isinstance(value, str) or any(c in value for c in '\\\x00\n\r'):
        raise ValueError('CLONE text cannot contain backslashes or control line breaks.')
    return "'" + value.replace("'", "''") + "'"


def donor_endpoint(donor_connection, donor_address=None):
    """``(host, port)`` the RECIPIENT uses to reach the donor.

    *donor_address* ("host:port") is needed when the recipient sees the donor
    under another name than myflames does (containers, NAT, a proxy or a
    tunnel); by default it is the donor connection's host and port.
    """
    if donor_address in (None, ''):
        options = _options(donor_connection)
        return options['host'], options['port']
    if not isinstance(donor_address, str) or ':' not in donor_address:
        raise ValueError('Give the donor address as host:port, as the recipient reaches it.')
    host, port = donor_address.rsplit(':', 1)
    host = host[1:-1] if host.startswith('[') and host.endswith(']') else host
    if not host or len(host) > 255 or any(c.isspace() or c in '\'"\\`@\x00' for c in host):
        raise ValueError('Invalid donor host: ' + host)
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('Donor port must be 1..65535.')
    return host, int(port)


def clone_statement(donor_connection, data_directory=None, require_ssl=True, redact=True, donor_address=None):
    """Exact CLONE syntax. Public/default rendering always redacts its password.

    redact=False is exclusively for the stdin execution path. CLONE's grammar
    does not accept general SQL expressions such as CONVERT(hex USING utf8mb4).
    """
    if not isinstance(require_ssl, bool):
        raise ValueError('require_ssl must be a boolean.')
    options = _options(donor_connection)
    user = options.get('user')
    if not user or options.get('password') is None:
        raise ValueError('CLONE donor needs a user and password.')
    host, port = donor_endpoint(donor_connection, donor_address)
    password = '[redacted]' if redact else options['password']
    statement = 'CLONE INSTANCE FROM %s@%s:%d IDENTIFIED BY %s' % (
        _quoted_clone(user), _quoted_clone(host), port, _quoted_clone(password))
    if data_directory:
        statement += ' DATA DIRECTORY = ' + _quoted_clone(data_directory)
    return TAG + statement + (' REQUIRE SSL' if require_ssl else ' REQUIRE NO SSL')


def clone_preflight(donor, recipient, donor_connection, recipient_connection, data_directory=None,
                    directory_probe=None, donor_address=None):
    """Pure facts-based CLONE matrix, also usable with recorded server facts.

    DATA DIRECTORY: the recipient server itself refuses a relative path
    (ER_WRONG_VALUE), an existing one (ER_DB_CREATE_EXISTS) and one inside its
    data directory, links resolved (ER_PATH_IN_DATADIR), before copying, and
    such a clone drops nothing. myflames checks what it can see: absolute, and
    not under @@datadir as text. directory_probe(path), when given, runs on the
    RECIPIENT host and returns {exists, realpath} for the destination and the
    datadir; without one the existence check is not_checked, never guessed
    from this machine's files. Cross-series clones are refused conservatively.
    """
    import posixpath
    source, target = donor['server'], recipient['server']
    checks = []
    def add(name, ok, text):
        checks.append(_verification(name, 'passed' if ok else 'failed', text))
    add('engine', source['engine'] == target['engine'] == 'mysql', 'MySQL only; MariaDB has no clone plugin. Use mariadb-backup on the source host.')
    sv, tv = version_tuple(source['version']), version_tuple(target['version'])
    same_series = sv[:2] == tv[:2]
    patch_ok = sv == tv or (sv[:2] == (8, 0) and min(sv, tv) >= (8, 0, 37)) or sv[:2] >= (8, 4)
    add('version', min(sv, tv) >= (8, 0, 17) and same_series and patch_ok,
        'Same major.minor required; 8.0 patches must match unless both are 8.0.37+. Cross-series clones are conservatively refused.')
    for side, facts in (('donor', donor), ('recipient', recipient)):
        add(side + '_plugin', any(str(r[0]).lower() == 'clone' and r[1] == 'ACTIVE' for r in facts['plugins']),
            'Active clone plugin required on ' + side + " (INSTALL PLUGIN clone SONAME 'mysql_clone.so').")
        operation = 'clone_donor' if side == 'donor' or data_directory else 'clone_recipient'
        needed = 'BACKUP_ADMIN' if operation == 'clone_donor' else 'CLONE_ADMIN'
        if 'privileges' in facts:
            allowed = privileges.has(facts['privileges'], needed)
        else:
            # Older recorded facts contain only plain grants: presence proves
            # permission, absence cannot prove roles lack it.
            grants = facts.get('grants', {})
            allowed = True if grants.get('checked') and needed in dbops.global_privileges(grants.get('grants', [])) else None
        checks.append(_verification(side + '_privilege', 'passed' if allowed is True else
                                    'failed' if allowed is False else 'not_checked',
                                    needed + ' required on the ' + side + ' account; effective grants are a preflight hint.'))
    svs, tvs = source['variables'], target['variables']
    for key in ('version_compile_os', 'version_compile_machine', 'character_set_server',
                'character_set_filesystem', 'collation_server', 'innodb_page_size'):
        add(key, bool(svs.get(key)) and svs.get(key) == tvs.get(key), 'Donor and recipient must match ' + key + '.')
    add('collations', bool(donor['collations']) and set(donor['collations']) <= set(recipient['collations']),
        'All donor collations must exist on recipient.')
    source_plugins = {r[0].lower() for r in donor['plugins'] if len(r) > 2 and r[2]}
    target_plugins = {r[0].lower() for r in recipient['plugins'] if len(r) > 2 and r[2]}
    add('plugins', source_plugins <= target_plugins, 'Donor dynamic plugins must already be active on recipient (conservative loadability check).')
    for side, vs in (('donor', svs), ('recipient', tvs)):
        add(side + '_packet', str(vs.get('max_allowed_packet', '')).isdigit() and int(vs['max_allowed_packet']) >= 2 * 1024 * 1024,
            'max_allowed_packet must be at least 2 MiB.')
    host, port = donor_endpoint(donor_connection, donor_address)
    endpoint = '%s:%d' % (host, port)
    valid = [p.strip() for p in (tvs.get('clone_valid_donor_list') or '').split(',')]
    add('valid_donor', endpoint in valid, 'Recipient clone_valid_donor_list must contain ' + endpoint +
        ' (the donor as the recipient reaches it); myflames never sets it.')
    add('different_server', not _same_server(source, target), 'Donor and recipient must differ.')
    if data_directory:
        absolute = data_directory.startswith('/') and '\\' not in data_directory
        add('directory_absolute', absolute, 'DATA DIRECTORY must be an absolute recipient-host path.')
        datadir_text = posixpath.normpath(tvs.get('datadir') or '/') if tvs.get('datadir') else ''
        wanted = posixpath.normpath(data_directory) if absolute else ''
        inside = bool(datadir_text and wanted) and posixpath.commonpath([wanted, datadir_text]) == datadir_text
        if absolute:
            add('directory_outside_datadir_text', not inside,
                'DATA DIRECTORY must be outside the recipient data directory (' + (tvs.get('datadir') or '?') + ').')
        checked = directory_probe(data_directory) if directory_probe and absolute else None
        if not directory_probe:
            checks.append(_verification('directory_checked', 'not_checked',
                                        'myflames cannot see the recipient host files. The server refuses an existing '
                                        'directory or one inside its data directory (links resolved) before copying.'))
        else:
            datadir_facts = directory_probe(tvs.get('datadir', '')) if absolute else None
            add('directory_checked', bool(checked) and isinstance(checked.get('exists'), bool) and bool(checked.get('realpath')),
                'The recipient-host filesystem probe must answer.')
            if checked:
                real = checked.get('realpath', '')
                datadir = posixpath.normpath((datadir_facts or {}).get('realpath', ''))
                datadir_exists = (datadir_facts or {}).get('exists') is True
                outside = datadir_exists and bool(real.startswith('/') and datadir.startswith('/')) and posixpath.commonpath([real, datadir]) != datadir
                add('directory_absent', checked.get('exists') is False, 'Destination directory must not exist.')
                add('directory_outside_datadir', outside, 'Destination must resolve outside recipient datadir, including symlinks.')
    return checks


def _clone_expect(donor_connection, recipient_connection, require_ssl=True, donor_address=None):
    """Credential-free pair displayed by the confirmation and bound to the plan."""
    def endpoint(connection):
        return {'endpoint': '%s:%s' % (connection.get('host', '127.0.0.1'), connection.get('port', 3306)),
                'connection_key': dbops.connection_identity(connection),
                'type': connection.get('type', 'production'), 'read_only': connection.get('read_only') is True}
    host, port = donor_endpoint(donor_connection, donor_address)
    return {'donor': endpoint(donor_connection), 'recipient': endpoint(recipient_connection),
            'donor_address': '%s:%d' % (host, port), 'require_ssl': require_ssl is not False}


_TLS_REQUIRED_MODES = ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY')


def require_clone_recipient_tls(donor_connection, recipient_connection, saved=False):
    """Protect the donor password on its separate client-to-recipient hop.

    Known saved credentials always require TLS. A saved profile or a donor
    requiring TLS also rejects explicit downgrades and unprotected remote
    recipients. Legacy implicit loopback connections remain accepted when
    no saved credential is being released.
    """
    recipient_mode = recipient_connection.get('ssl_mode')
    if recipient_mode in _TLS_REQUIRED_MODES:
        return
    if saved or donor_connection.get('_saved_password') is True:
        raise Refused('A saved donor password requires TLS on the recipient connection. '
                      'Choose REQUIRED, VERIFY_CA or VERIFY_IDENTITY for the recipient.')
    sensitive = (donor_connection.get('profile_id') or
                 donor_connection.get('ssl_mode') in _TLS_REQUIRED_MODES)
    host = recipient_connection.get('host') or '127.0.0.1'
    loopback = host.lower() in ('127.0.0.1', '::1', 'localhost')
    if sensitive and (recipient_mode in ('DISABLED', 'PREFERRED') or not loopback):
        raise Refused('The recipient connection must use TLS (REQUIRED, VERIFY_CA or VERIFY_IDENTITY) '
                      'to receive the donor password.')


def prepare_clone(donor_conn, recipient_conn, donor_connection, recipient_connection, data_directory=None,
                  require_ssl=True, directory_probe=None, tools=None, donor_address=None):
    if donor_connection.get('ssl_mode') in ('REQUIRED', 'VERIFY_CA', 'VERIFY_IDENTITY') and require_ssl is False:
        raise Refused('The donor requires TLS; clone must use REQUIRE SSL.')
    require_clone_recipient_tls(donor_connection, recipient_connection)
    donor, recipient = _clone_facts(donor_conn, donor_connection), _clone_facts(recipient_conn, recipient_connection)
    # Validate secret too, but never place it in a plan or its binding.
    clone_statement(donor_connection, data_directory, require_ssl, redact=False, donor_address=donor_address)
    checks = clone_preflight(donor, recipient, donor_connection, recipient_connection, data_directory,
                             directory_probe, donor_address)
    host, port = donor_endpoint(donor_connection, donor_address)
    request = {'data_directory': data_directory, 'require_ssl': require_ssl,
               'saved_donor_password': donor_connection.get('_saved_password') is True,
               'donor_key': dbops.connection_identity(donor_connection),
               'donor_address': '%s:%d' % (host, port)}
    target = '%s:%s' % (recipient_connection.get('host', '127.0.0.1'), recipient_connection.get('port', 3306))
    if data_directory:
        target += ':' + data_directory
    warnings = ['CLONE transfers physical data. A recipient may need manual recovery after cancellation.']
    if not data_directory:
        warnings.append('IN-PLACE CLONE DROPS ALL RECIPIENT USER DATA AND RESTARTS THE SERVER. '
                        'Without a restart supervisor the recipient can shut down and require a manual start. This cannot be undone.')
        warnings.append("After it, the recipient has the donor's accounts and passwords (the mysql schema is cloned): "
                        'connect to it with an account from the donor.')
    tool = _pick(detect_tools() if tools is None else tools, ('mysql',), 'mysql')
    expect = _clone_expect(donor_connection, recipient_connection, require_ssl, donor_address)
    plan = _plan('clone', recipient['server'], recipient_connection, request, tool, target,
                 [clone_statement(donor_connection, data_directory, require_ssl, donor_address=donor_address)],
                 checks, warnings, binding_extra={'donor_identity': donor['server']['identity'], 'expect': expect})
    plan['donor'] = donor['server']
    plan['expect'] = expect
    return plan


def clone_progress(conn):
    reader = Reader(conn, TAG)
    return {'schema_version': 'clone-progress-1.0',
            'progress': reader.rows('SELECT STAGE,STATE,ESTIMATE,DATA,NETWORK,DATA_SPEED FROM performance_schema.clone_progress'),
            'status': reader.rows('SELECT STATE,ERROR_NO,ERROR_MESSAGE,BINLOG_FILE,BINLOG_POSITION,GTID_EXECUTED,'
                                  'BEGIN_TIME,SOURCE,DESTINATION FROM performance_schema.clone_status')}


#: How long an in-place clone waits for the recipient to come back after its
#: restart (a supervisor such as systemd or mysqld_safe restarts it).
RESTART_WAIT = 180
#: Seconds between attempts to reach a restarting recipient.
RESTART_POLL = 2


def _restart_expected(exc):
    """After an in-place clone the server restarts: the client sees 3707
    (ER_RESTART_SERVER_FAILED, no supervisor: the server shuts down) or loses
    the connection (2013) while a supervisor restarts it."""
    text = str(exc)
    return dbops.error_code(text) in (3707, 2013) or 'Lost connection' in text or 'server has gone away' in text


def clone_job(donor_connection, recipient_connection, plan, confirmed=False, typed_target=None,
              audit=None, safety=None, factory=LifecycleConnection, directory_probe=None, start=True, poll_interval=1,
              restart_wait=RESTART_WAIT):
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or not 0.1 <= poll_interval <= 60:
        raise ValueError('Clone poll interval must be 0.1..60 seconds.')
    donor_connection, recipient_connection = copy.deepcopy(donor_connection), copy.deepcopy(recipient_connection)
    request = copy.deepcopy(plan.get('request', {}))
    donor_address = request.get('donor_address')
    def rebuild(conn, job):
        require_clone_recipient_tls(donor_connection, recipient_connection,
                                    saved=request.get('saved_donor_password') is True)
        with _connect(factory, donor_connection) as donor:
            return prepare_clone(_BoundedConnection(donor, job), conn, donor_connection, recipient_connection,
                                 request.get('data_directory'), request.get('require_ssl', True), directory_probe,
                                 tools=detect_tools(job=job), donor_address=donor_address)
    def action(job, conn, fresh):
        require_clone_recipient_tls(donor_connection, recipient_connection,
                                    saved=request.get('saved_donor_password') is True)
        tool = fresh['tool']
        if request.get('data_directory'):
            job.partial_state = ('A partial copy may remain in %s on the recipient host. Remove it before trying '
                                 'again: the server refuses an existing directory.' % request['data_directory'])
        else:
            job.partial_state = ('The recipient may already have dropped its user data. Check it, and restart it if '
                                 'it is down, before using it or cloning again.')
        stop = threading.Event()
        def monitor():
            stage = None
            while not stop.wait(poll_interval):
                try:
                    with _connect(factory, recipient_connection) as control:
                        if hasattr(control, 'query_timeout'):
                            control.query_timeout = 3
                        if hasattr(control, 'connect_timeout'):
                            control.connect_timeout = 3
                        progress = clone_progress(control)
                except ConnectorError:
                    continue  # A supervised in-place clone can restart the server.
                current = _clone_stage(progress)
                with job.lock:
                    job.detail = progress
                    if current and current[2] is not None:
                        job.progress = current[2]
                if current and current[:2] != stage:
                    stage = current[:2]
                    job.emit('Clone stage %s: %s' % stage, 'clone_progress')
        # clone_status keeps the previous clone's row until a new one starts:
        # bind the row read afterwards to this clone by its BEGIN_TIME.
        started = Reader(conn, TAG).rows('SELECT NOW(3)')
        started = started[0][0] if started and started[0] else ''
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        restarted = False
        try:
            sql = TAG + "SELECT CONCAT('%s',CONNECTION_ID());\n" % job.marker
            sql += clone_statement(donor_connection, request.get('data_directory'), request.get('require_ssl', True),
                                   redact=False, donor_address=donor_address) + ';\n'
            try:
                _invoke(job, tool, recipient_connection, ['--batch', '--skip-column-names', '--unbuffered'] + list(dbops.WRITE_CLIENT_FLAGS), sql)
            except LifecycleError as exc:
                if request.get('data_directory') or job.cancelled.is_set() or not _restart_expected(exc):
                    raise
                restarted = True
                job.emit('The recipient is restarting after the clone (%s). Waiting up to %d s for it to come back.'
                         % ('no supervisor: it shut down' if dbops.error_code(str(exc)) == 3707 else 'connection closed',
                            restart_wait))
        finally:
            stop.set()
            thread.join()
        verification = []
        after, back_error = None, ''
        deadline = time.monotonic() + (restart_wait if restarted else 0)
        while True:
            try:
                after = clone_progress(conn)
                break
            except ConnectorError as exc:
                back_error = str(exc)
                if time.monotonic() >= deadline or dbops.error_code(back_error) == 1045:
                    break
                job.cancelled.wait(RESTART_POLL)
                job.check_cancel()
        if after is None:
            if dbops.error_code(back_error) == 1045:
                reason = ("The recipient answers but refuses the recipient account: after an in-place clone it has the "
                          "donor's accounts. Connect with an account from the donor and check "
                          'performance_schema.clone_status.')
            elif restarted:
                reason = ('The recipient did not come back within %d s. Without a supervisor it shut down after the '
                          'clone: start it by hand to finish, then check performance_schema.clone_status. %s'
                          % (restart_wait, back_error))
            else:
                reason = 'Recipient unavailable after the clone: ' + back_error
            verification.append(_verification('clone_status', 'not_checked', reason))
            return {'status': 'needs_restart' if restarted and dbops.error_code(back_error) != 1045 else 'needs_verification',
                    'restarted': restarted, 'verification': verification}
        statuses = after['status']
        row = statuses[0] if statuses else []
        complete = len(row) > 1 and row[0] == 'Completed' and str(row[1]) == '0'
        destination = request.get('data_directory') or 'LOCAL INSTANCE'
        # BEGIN_TIME, SOURCE and DESTINATION (8.0.17+) tie the row to this clone.
        ours = len(row) < 9 or ((not started or str(row[6] or '') >= started)
                                and str(row[7] or '') == donor_address
                                and str(row[8] or '').rstrip('/') == destination.rstrip('/'))
        verification.append(_verification('clone_status', 'passed' if complete and ours else 'failed',
                                          'Recipient performance_schema.clone_status after the clone' +
                                          ('' if ours else ': the row is from another clone (begin time, source '
                                                           'or destination differ)') + '.',
                                          after=_safe(after, job.passwords)))
        donor_gtid = fresh['donor']['variables'].get('gtid_executed')
        recipient_gtid = row[5] if len(row) > 5 else ''
        same = bool(donor_gtid) and _gtid_text(donor_gtid) == _gtid_text(recipient_gtid)
        if request.get('data_directory') and not recipient_gtid:
            reason = ('A DATA DIRECTORY clone records no GTID_EXECUTED in clone_status; a server started on the '
                      'copy has the cloned set.')
        else:
            reason = ('Donor gtid_executed at preflight against the clone_status GTID_EXECUTED; donor commits '
                      'during the clone make them differ.')
        verification.append(_verification('gtid', 'passed' if same else 'not_checked', reason,
                                          donor=donor_gtid or '', recipient=recipient_gtid or ''))
        status = ('verification_failed' if any(v['status'] == 'failed' for v in verification) else 'completed')
        return {'status': status, 'restarted': restarted, 'verification': verification}
    return _write_job('clone', recipient_connection, plan, rebuild, action, confirmed, typed_target,
                      audit, safety, factory, start, passwords=(donor_connection.get('password'),))


def _clone_stage(progress):
    """(stage, state, percent of all stage estimates) from clone_progress
    rows (STAGE, STATE, ESTIMATE, DATA, ...), or None. The percent is absent
    until the server reports an estimate."""
    rows = [r for r in (progress or {}).get('progress') or [] if len(r) >= 4]
    if not rows:
        return None
    current = next((r for r in rows if r[1] == 'In Progress'), None) or rows[-1]
    try:
        estimate = sum(int(r[2] or 0) for r in rows)
        done = sum(min(int(r[3] or 0), int(r[2] or 0)) for r in rows)
    except (TypeError, ValueError):
        estimate, done = 0, 0
    percent = round(100.0 * done / estimate, 1) if estimate > 0 else None
    return current[0], current[1], percent


def _gtid_text(value):
    """GTID sets compare as text after removing the line breaks and spaces
    the server inserts between UUID groups."""
    return re.sub(r'\s+', '', str(value or '')).lower()


def prepare_physical_backup(conn, connection, directory, tools=None, same_host_confirmed=False):
    """Prepare MariaDB backup+prepare as one explicitly confirmed host operation.

    Locality requires an explicitly selected, matching live Unix socket and
    accessible datadir, plus operator confirmation that both are in the server's
    mount namespace. Physical copy-back is intentionally a host-operator task.
    """
    guidance = physical_backup_guidance(conn, connection, directory)
    server, directory = guidance['server'], _output_path(directory)
    _empty_directory(directory)
    tool = _pick(detect_tools() if tools is None else tools, ('mariadb-backup',), 'mariadb')
    checks = [_verification('engine', 'passed' if server['engine'] == 'mariadb' else 'failed', 'mariadb-backup requires MariaDB.'),
              _verification('local_files', 'passed' if guidance['local_files_detected'] and
                            connection.get('host') in ('localhost', '127.0.0.1', '::1') and same_host_confirmed is True else 'failed',
                            'Requires the matching local Unix socket, accessible datadir, and explicit same_host_confirmed.'),
              _verification('tool_version', 'passed' if version_tuple(tool['version'])[:2] == version_tuple(server['version'])[:2] else 'failed',
                            'Use mariadb-backup from the same server major.minor series.'),
              _privileges_check(conn, [('RELOAD', 'ALL', 'ALL PRIVILEGES'), ('PROCESS', 'ALL', 'ALL PRIVILEGES')])]
    catalog = _catalog(conn)
    estimate = sum(int(row[4] or 0) + int(row[5] or 0) for row in catalog)
    available = shutil.disk_usage(os.path.dirname(directory)).free
    checks.append(_verification('free_space', 'passed' if available >= estimate else 'failed',
                                'Table/index size estimate excludes redo and backup overhead.', estimated_bytes=estimate, available_bytes=available))
    request = {'directory': directory, 'same_host_confirmed': same_host_confirmed is True}
    commands = [tool['name'] + ' --defaults-file=<0600 file> --backup --target-dir=' + directory,
                tool['name'] + ' --defaults-file=<0600 file> --prepare --target-dir=' + directory]
    return _plan('physical_backup', server, connection, request, tool, directory, commands, checks,
                 ['The backup takes server locks; prepare is mandatory before copy-back.',
                  'Restore requires stopping the server and an empty datadir; no copy-back is run here.'])


def physical_backup_job(connection, plan, confirmed=False, typed_target=None, audit=None, safety=None,
                        factory=LifecycleConnection, start=True):
    connection = copy.deepcopy(connection)
    request = copy.deepcopy(plan.get('request', {}))
    def rebuild(conn, job):
        return prepare_physical_backup(conn, connection, tools=detect_tools(job=job), **request)
    def action(job, conn, fresh):
        directory, tool = request['directory'], fresh['tool']
        _empty_directory(directory)
        if not os.path.exists(directory):
            os.mkdir(directory, 0o700)
        os.chmod(directory, 0o700)
        started, verification, complete = now(), [], False
        try:
            _invoke(job, tool, connection, ['--backup', '--target-dir=' + directory])
            _invoke(job, tool, connection, ['--prepare', '--target-dir=' + directory])
            checkpoints = os.path.join(directory, 'xtrabackup_checkpoints')
            prepared = False
            if os.path.isfile(checkpoints):
                with open(checkpoints, 'r', encoding='utf-8') as handle:
                    prepared = bool(re.search(r'backup_type\s*=\s*full-prepared', handle.read(8192)))
            verification.append(_verification('prepared', 'passed' if prepared else 'failed',
                                               'xtrabackup_checkpoints must mark full-prepared after --prepare.'))
            verification.append(_verification('restored_rows', 'not_checked', 'Physical backup must be restored on a stopped test host to compare rows.'))
            complete = prepared
        finally:
            records = []
            try:
                if complete and not job.cancelled.is_set():
                    records = _file_records(directory, job)
            finally:
                complete = complete and bool(records) and not job.cancelled.is_set()
                manifest = {'schema_version': 'lifecycle-manifest-1.0', 'source': fresh['server'],
                            'scope': {'schemas': [], 'tables': []}, 'tool': tool,
                            'options': {'physical': True, 'prepare': True}, 'started_at': started,
                            'finished_at': now(), 'files': records, 'samples': [],
                            'verification': verification, 'complete': complete}
                _save_manifest(directory, _report_safe(manifest, job.passwords))
                if not complete:
                    job.emit('Partial physical backup retained at ' + directory)
        return {'status': 'completed' if complete else 'verification_failed', 'verification': verification,
                'directory': directory, 'manifest': manifest}
    return _write_job('physical_backup', connection, plan, rebuild, action, confirmed, typed_target,
                      audit, safety, factory, start)


class LifecycleJobs(object):
    """The workspace server's lifecycle jobs (dump, restore, clone).

    At most two run at once and never two on the same target; finished jobs
    are kept for an hour (at most 20) so a page can read the result after a
    reload. close() cancels running jobs when the server stops.
    """
    KEEP, RUNNING, AGE = 20, 2, 3600

    def __init__(self):
        self.jobs = collections.OrderedDict()
        self.targets = {}
        self.lock = threading.Lock()
        self.closed = False

    def add(self, job, target):
        with self.lock:
            if self.closed:
                raise ValueError('Lifecycle jobs are shutting down; no new operation was started.')
            active = [key for key, item in self.jobs.items() if item.state in ('queued', 'running')]
            if len(active) >= self.RUNNING:
                raise ValueError('Two lifecycle operations are running. Wait for one or cancel it first.')
            resources = getattr(job, 'resources', None) or {'target:' + target}
            if any(self.targets.get(key, set()) & resources for key in active):
                raise ValueError('An operation on %s is already running.' % target)
            finished = [key for key in self.jobs if key not in active]
            for key in finished:
                if len(self.jobs) < self.KEEP and time.monotonic() - self.jobs[key].created < self.AGE:
                    break
                del self.jobs[key]
                self.targets.pop(key, None)
            self.jobs[job.id] = job
            self.targets[job.id] = resources
            job.start()
        return job.snapshot()

    def get(self, key):
        with self.lock:
            job = self.jobs.get(key) if isinstance(key, str) else None
        if job is None:
            raise ValueError('Operation not found. It may have expired.')
        return job

    def close(self):
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
        for job in jobs:
            if job.state in ('queued', 'running'):
                job.cancel()
        for job in jobs:
            job.close()
