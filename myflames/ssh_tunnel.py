"""SSH tunnels for saved connections (stdlib, Python 3.7+).

A connection with ``ssh_host`` reaches its database through a local port
forward of the system ``ssh``, with the user's own ``~/.ssh/config``, keys
and agent (no password or passphrase prompt: ``BatchMode=yes``). Every
database client of such a connection (reads, editor sessions, writes and
lifecycle tools) connects over TCP to ``127.0.0.1`` and the tunnel's local
port; the database host and port are what the SSH host connects to.

Contract:

- Fields: ``ssh_host`` (a host name, an IP address or a ``Host`` alias of
  ``~/.ssh/config``), optional ``ssh_user`` and ``ssh_port``; validated by
  :func:`ssh_fields` (strict patterns, never starting with ``-``).
- One ``ssh`` per (SSH host, SSH user, SSH port, database host, database
  port), started as an argument list (never a shell) in its own process
  group: ``ssh -N -T -o BatchMode=yes -o ExitOnForwardFailure=yes
  -o LogLevel=DEBUG1 ... -L 127.0.0.1:LOCAL:DBHOST:DBPORT -- TARGET``. The
  local port is picked by binding 127.0.0.1:0; a forward that fails to bind
  is retried once.
- Ready only on ssh's own words, never on a TCP accept: ssh binds the local
  port after it signed in, and another local program could listen there
  first (the port is visible in ``ps``). ssh logs at DEBUG1 "Local forwarding
  listening on 127.0.0.1 port LOCAL." just before its bind, "channel N: new
  ... [port listener]" after the bind and listen succeeded, and "Entering
  interactive session." after every forward was set up (with
  ExitOnForwardFailure a failed bind ends ssh before that line). All three,
  in that order, within ``READY_TIMEOUT``, while ssh runs: ssh holds the port
  from then on. Otherwise ssh is stopped and its message mapped to plain
  words. Debug lines are only matched, never kept or shown. No probe
  connection is made (it would reach the database as an aborted connect).
- On POSIX each ssh is started by a small watchdog (``ssh_watchdog.py``)
  that leads the tunnel's process group and stops ssh when its pipe from
  this process closes, so a tunnel never outlives the process that opened
  it, even one killed with SIGKILL. On Windows each ssh.exe starts
  suspended, joins this process's job object (``win_job``, kill on job
  close) and only then runs, so it ends with this process however that ends
  (``taskkill /F`` included); when Windows refuses the job, ssh is stopped
  and the tunnel refused.
- Shared and reference counted by the code paths that use it (a lease per
  client invocation, per editor session, per lifecycle tool run); closed
  after ``IDLE_SECONDS`` without users, and always on shutdown. A tunnel
  that died is started again on the next use, never replaced under a
  running statement (that statement gets the client's own error).
- The process group of each tunnel is listed in the workspace's
  ``tool-groups-1.0`` file (lifecycle.ToolGroups, as a service group, within
  its ``MAX_GROUPS``), so the desktop app can stop it if it must kill the
  backend.
- ssh gets an allowlisted environment with the user's real home and agent
  socket (``MYFLAMES_SSH_HOME`` and ``MYFLAMES_SSH_AUTH_SOCK`` from the
  desktop app, whose backend runs with an isolated HOME); never a database
  password, which stays in the clients' option files.
"""
import atexit
import collections
import glob
import ntpath
import os
import posixpath
import re
import signal
import socket
import subprocess
import sys
import threading
import time

from .connector import ConnectorError

SSH_FIELDS = ('ssh_host', 'ssh_user', 'ssh_port')
#: Seconds to wait for the local end of a new tunnel to accept a connection.
READY_TIMEOUT = 15.0
#: Seconds a tunnel without users stays open.
IDLE_SECONDS = 300.0
#: Tunnels open at once. Their process groups count toward
#: lifecycle.ToolGroups.MAX_GROUPS together with dump, restore and clone tools.
MAX_TUNNELS = 8
#: Bytes of ssh's standard error kept for messages.
STDERR_KEEP = 16 * 1024

#: An SSH host name, address or Host alias: no option, user, port, path or
#: token syntax can get through. ``\Z``, not ``$`` (which also matches
#: before a final newline).
_SSH_HOST = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,252}\Z')
_SSH_USER = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._@-]{0,127}\Z')
#: The database host as the SSH host resolves it (ssh's -L syntax).
_DB_NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]{0,252}\Z')
_DB_IPV6 = re.compile(r'[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]*(%[A-Za-z0-9_.-]+)?\Z')

_SSH_HOST_RULE = ('Enter the SSH host as a host name, an IP address or a Host alias from ~/.ssh/config: '
                  'letters, digits, dots, hyphens and underscores, not starting with a hyphen.')
_SSH_USER_RULE = ('Enter the SSH user as letters, digits, dots, hyphens, underscores or @, not starting with a '
                  'hyphen or @.')

#: Environment ssh may see: what the system needs, never database or cloud secrets.
_SSH_ENV = ('PATH', 'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL', 'LC_CTYPE',
            'TZ', 'TMPDIR', 'TEMP', 'TMP', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'USERNAME', 'USER', 'LOGNAME')


class TunnelError(ConnectorError):
    """An SSH tunnel could not be opened (the message says why in plain words)."""


Spec = collections.namedtuple('Spec', 'ssh_host ssh_user ssh_port db_host db_port')


# ---- validation ------------------------------------------------------------------------

def _given(value):
    return value not in (None, '', 0) and value is not False


def ssh_fields(value):
    """The validated SSH fields of connection settings *value*: ``{}`` without
    SSH, else ``ssh_host`` and, when given, ``ssh_user`` and ``ssh_port``.
    Pasted spaces around the values are dropped, like the host field's;
    anything else outside the patterns is refused with what is allowed."""
    from .live import _blank, integer, trim
    host, user, port = value.get('ssh_host'), value.get('ssh_user'), value.get('ssh_port')
    for name, item in (('ssh_host', host), ('ssh_user', user)):
        if item is not None and not isinstance(item, str):
            raise ValueError('Invalid connection field: ' + name)
    host = trim(host or '')
    user = trim(user or '')
    if not host:
        if user or _given(port):
            raise ValueError('SSH user and SSH port need an SSH host. Enter the SSH host, or clear them.')
        return {}
    if any(_blank(c) for c in host):
        raise ValueError('The SSH host %r has a space in it. %s' % (host, _SSH_HOST_RULE))
    if '@' in host:
        raise ValueError('Enter the SSH user in SSH user, not in SSH host: %r.' % host)
    if ':' in host or '/' in host:
        raise ValueError('Enter only the SSH host name or alias in SSH host, and the port in SSH port: %r.' % host)
    if not _SSH_HOST.match(host):
        raise ValueError('%s Got %r.' % (_SSH_HOST_RULE, host))
    out = {'ssh_host': host}
    if user:
        if not _SSH_USER.match(user):
            raise ValueError('%s Got %r.' % (_SSH_USER_RULE, user))
        out['ssh_user'] = user
    if _given(port):
        number = integer(port, 'SSH port')
        if not 1 <= number <= 65535:
            raise ValueError('SSH port must be between 1 and 65535.')
        out['ssh_port'] = number
    return out


def forward_host(host):
    """The database host as written in ssh's ``-L`` argument: a name or IPv4
    address as is, an IPv6 address in brackets."""
    if isinstance(host, str) and _DB_NAME.match(host):
        return host
    if isinstance(host, str) and _DB_IPV6.match(host):
        return '[' + host + ']'
    raise ValueError('The database host %r cannot be used through SSH. Enter a host name or an IP address, as '
                     'the SSH host reaches the database.' % (host,))


def check_connection(options):
    """Refuse settings that cannot work through SSH, once *options* (validated
    connection fields) name an SSH host."""
    if not options.get('ssh_host'):
        return
    forward_host(options.get('host') or '127.0.0.1')
    if options.get('socket'):
        raise ValueError('A connection through SSH uses TCP; clear the socket path.')
    if str(options.get('ssl_mode') or '').upper() == 'VERIFY_IDENTITY':
        raise ValueError('VERIFY_IDENTITY cannot be used through SSH: the client checks the certificate against '
                         '127.0.0.1, the local end of the tunnel, not the database host. Choose VERIFY_CA.')


def spec_of(value):
    """The tunnel a connection (a dict or a connector object) needs, or None
    without SSH. Validates again, so a connection object built without
    live.connection_options fails closed rather than connecting directly."""
    get = value.get if isinstance(value, dict) else (lambda name: getattr(value, name, None))
    fields = ssh_fields({name: get(name) for name in SSH_FIELDS})
    if not fields:
        return None
    from .live import clean_host
    db_host = clean_host(get('host') or '127.0.0.1')
    forward_host(db_host)
    try:
        db_port = int(get('port') or 3306)
    except (TypeError, ValueError):
        raise ValueError('Port must be between 1 and 65535.')
    if not 1 <= db_port <= 65535:
        raise ValueError('Port must be between 1 and 65535.')
    return Spec(fields['ssh_host'], fields.get('ssh_user', ''), fields.get('ssh_port', 0), db_host, db_port)


def target_text(value):
    """"deploy@bastion:2222", "bastion": the SSH target of connection
    settings for labels and the audit trail, or None without SSH."""
    get = value.get if isinstance(value, dict) else (lambda name: getattr(value, name, None))
    host = get('ssh_host')
    if not isinstance(host, str) or not host.strip():
        return None
    user, port = get('ssh_user'), get('ssh_port')
    text = host.strip()
    if isinstance(user, str) and user.strip():
        text = user.strip() + '@' + text
    if _given(port) and not isinstance(port, bool):
        text += ':%s' % port
    return text


def through_text(value):
    """"Through SSH host bastion" for connection settings with SSH, else None."""
    host = value.get('ssh_host') if isinstance(value, dict) else getattr(value, 'ssh_host', None)
    if not isinstance(host, str) or not host.strip():
        return None
    return 'Through SSH host %s' % host.strip()


# ---- the ssh program and its environment -----------------------------------------------

#: Where ssh usually is on macOS and Linux when PATH does not name it.
_POSIX_FOLDERS = ('/usr/bin', '/usr/local/bin', '/opt/homebrew/bin', '/opt/local/bin')


def _is_program(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _path_folders(path):
    """The absolute folders of a PATH value. Empty, ``.`` and other relative
    entries name the current folder (wherever myflames was started), so they
    are never searched; on Windows a folder needs a drive or a UNC share."""
    windows = os.name == 'nt'
    paths = ntpath if windows else posixpath
    for entry in (path or '').split(';' if windows else ':'):
        entry = entry.strip().strip('"') if windows else entry
        if not entry or not paths.isabs(entry):
            continue
        if windows and not paths.splitdrive(entry)[0]:
            continue
        yield entry


def find_ssh(path=None):
    """The ssh client, as an absolute path. Raises TunnelError.

    Windows: the OpenSSH client Windows ships (System32\\OpenSSH), else an
    ``ssh.exe`` (never ssh.bat, ssh.cmd or another PATHEXT name) in an
    absolute PATH folder. Elsewhere: ``ssh`` in an absolute PATH folder, else
    the usual system folders. *path* replaces the PATH value."""
    path = os.environ.get('PATH', '') if path is None else path
    if os.name == 'nt':
        root = (os.environ.get('SYSTEMROOT') or os.environ.get('SystemRoot') or os.environ.get('WINDIR')
                or 'C:\\Windows')
        candidates = []
        if ntpath.isabs(root) and ntpath.splitdrive(root)[0]:
            # Sysnative: a 32-bit process sees the 64-bit System32 under this name.
            candidates = [ntpath.join(root, 'System32', 'OpenSSH', 'ssh.exe'),
                          ntpath.join(root, 'Sysnative', 'OpenSSH', 'ssh.exe')]
        candidates += [ntpath.join(folder, 'ssh.exe') for folder in _path_folders(path)]
    else:
        folders = list(_path_folders(path))
        folders += [folder for folder in _POSIX_FOLDERS if folder not in folders]
        candidates = [posixpath.join(folder, 'ssh') for folder in folders]
    for candidate in candidates:
        if _is_program(candidate):
            return candidate
    raise TunnelError('ssh was not found. Install the OpenSSH client (on Windows: Settings, Optional features, '
                      'OpenSSH Client) and try again.')


def ssh_home():
    """The user's real home folder, where ~/.ssh lives: MYFLAMES_SSH_HOME
    (set by the desktop app, whose backend has an isolated HOME), else HOME
    (USERPROFILE on Windows)."""
    home = os.environ.get('MYFLAMES_SSH_HOME')
    if home and os.path.isabs(home):
        return home
    if os.name == 'nt':
        return os.environ.get('USERPROFILE') or os.path.expanduser('~')
    home = os.environ.get('HOME')
    if home and os.path.isabs(home):
        return home
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return os.path.expanduser('~')


def ssh_environment():
    """The environment of the ssh process: an allowlist plus the real home
    and agent socket. No MYSQL_PWD or other database setting, no cloud or
    vault secrets, no DISPLAY or SSH_ASKPASS (nothing may prompt)."""
    env = {key: os.environ[key] for key in _SSH_ENV if key in os.environ}
    home = ssh_home()
    env['HOME'] = home
    if os.name == 'nt':
        env['USERPROFILE'] = home
    agent = os.environ.get('MYFLAMES_SSH_AUTH_SOCK') or os.environ.get('SSH_AUTH_SOCK')
    if agent:
        env['SSH_AUTH_SOCK'] = agent
    return env


def ssh_argv(binary, spec, local_port):
    """The ssh command line of one tunnel, as a list. ``--`` ends the
    options, so the target is never read as one (it cannot start with ``-``
    anyway)."""
    argv = [binary, '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
            # The readiness signal (see the module contract); a command-line
            # option wins over any LogLevel in ssh_config.
            '-o', 'LogLevel=DEBUG1',
            # myflames never changes known_hosts, and runs no LocalCommand.
            '-o', 'UpdateHostKeys=no', '-o', 'PermitLocalCommand=no',
            '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
    if os.name == 'posix':
        # A shared master from the user's ControlMaster settings would own the
        # forward and could outlive this process; every tunnel has its own.
        argv += ['-o', 'ControlMaster=no', '-o', 'ControlPath=none']
    if spec.ssh_port:
        argv += ['-p', str(int(spec.ssh_port))]
    if spec.ssh_user:
        argv += ['-l', spec.ssh_user]
    argv += ['-L', '127.0.0.1:%d:%s:%d' % (int(local_port), forward_host(spec.db_host), int(spec.db_port)),
             '--', spec.ssh_host]
    return argv


def free_port():
    """A free TCP port on 127.0.0.1, as the system hands one out."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


# ---- what ssh said ---------------------------------------------------------------------

_FORWARD_FAILED = ('Address already in use', 'cannot listen to port', 'Could not request local forwarding')
#: ssh's debug lines ("debug1: ..."): matched for readiness, never kept or shown.
_DEBUG = re.compile(r'debug\d?:')
#: OpenSSH's DEBUG1 lines around a local forward (channels.c, ssh.c,
#: clientloop.c; checked against OpenSSH 10.3). The channel line reads
#: "channel 0: new port-listener [port listener] (inactive timeout: 0)" in
#: 9.x and later, "channel 0: new [port listener]" before.
_LISTENING = 'Local forwarding listening on 127.0.0.1 port %d.'
_LISTENER = re.compile(r'channel \d+: new (?:[\w-]+ )?\[port listener\]')
_SESSION = 'Entering interactive session.'


def _is_debug(line):
    return bool(_DEBUG.match(line))


def _without_debug(text):
    return '\n'.join(line for line in (text or '').splitlines() if not _is_debug(line.strip()))


def _lines(text):
    from .connector import terminal_text
    out = []
    for line in terminal_text(text or '').splitlines():
        line = line.strip()
        if line and not _is_debug(line) and not line.startswith('@@@') and not line.startswith('Warning: Permanently added'):
            out.append(line)
    return out


def forward_failed(text):
    return any(marker in (text or '') for marker in _FORWARD_FAILED)


def failure_message(text, spec):
    """ssh's standard error at an exit before the tunnel was ready, in plain
    words. Debug lines never decide the message or appear in it."""
    text = _without_debug(text)
    host = spec.ssh_host
    port = ' on port %d' % spec.ssh_port if spec.ssh_port else ''
    lowered = text.lower()
    if 'remote host identification has changed' in lowered:
        return ('The SSH host key of %s has changed since it was saved in known_hosts. Check with the host\'s '
                'administrator before you connect; myflames never changes known_hosts.' % host)
    if 'host key verification failed' in lowered:
        # known_hosts keys a non-default port as [host]:port: check that one.
        command = 'ssh -p %d %s' % (spec.ssh_port, host) if spec.ssh_port and spec.ssh_port != 22 else 'ssh ' + host
        return ('The SSH host key of %s is not known yet. Run `%s` once in Terminal to check and accept it.'
                % (host, command))
    if 'permission denied' in lowered:
        return ('SSH host %s refused the sign-in (Permission denied). myflames signs in only with your SSH keys '
                'or agent, never a password: add the key to your agent (ssh-add), or set User and IdentityFile '
                'for %s in ~/.ssh/config.' % (host, host))
    if 'could not resolve hostname' in lowered:
        return ('Could not resolve the SSH host %s. Check the name, or the Host alias in ~/.ssh/config.' % host)
    connect = [line for line in _lines(text) if 'open failed' not in line]
    joined = '\n'.join(connect).lower()
    if 'connection refused' in joined:
        return ('SSH host %s refused the connection%s. Check the SSH port and that its SSH server is running.'
                % (host, port))
    if 'timed out' in joined:
        return 'Timed out connecting to SSH host %s%s. Check the network, VPN or firewall.' % (host, port)
    if 'no route to host' in joined or 'network is unreachable' in joined:
        return 'SSH host %s cannot be reached from this computer (no route). Check the network or VPN.' % host
    if forward_failed(text):
        return 'Could not open the local end of the SSH tunnel through %s (%s).' % (
            host, next((line for line in connect if forward_failed(line)), 'port in use'))
    detail = connect[-1] if connect else 'ssh stopped without a message'
    return 'The SSH tunnel through %s did not open: %s' % (host, detail[:300])


def channel_message(line, spec):
    """A forward that could not reach the database, as ssh logs it
    ("channel 2: open failed: connect failed: Connection refused")."""
    lowered = line.lower()
    where = '%s:%d' % (spec.db_host, spec.db_port)
    if 'administratively prohibited' in lowered:
        return ('SSH host %s does not allow port forwarding for this account (AllowTcpForwarding), so the database '
                'at %s cannot be reached through it.' % (spec.ssh_host, where))
    reason = line.split('connect failed:', 1)[1].strip() if 'connect failed:' in line else line.strip()
    if 'name or service not known' in lowered or 'nodename nor servname' in lowered or 'not known' in lowered:
        return 'SSH host %s could not resolve the database host %s.' % (spec.ssh_host, spec.db_host)
    return 'SSH host %s could not reach the database at %s (%s).' % (spec.ssh_host, where, reason[:200])


# ---- tunnels ---------------------------------------------------------------------------

def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _signal_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class Announcement(object):
    """What ssh said about one start's forward on *port* (see the module
    contract): step 0 nothing yet, 1 listening line, 2 listener channel. When
    ssh enters its session, ``settled`` is set and ``ready`` tells whether
    that came after step 2. -1: the listening line was not followed by its
    channel (the bind failed), never ready."""

    def __init__(self, port):
        self.port = port
        self.step = 0
        self.ready = False
        self.settled = threading.Event()

    def hear(self, text):
        """One debug message (without its "debug1: " prefix)."""
        if self.settled.is_set():
            return
        if text == _SESSION:
            # Every forward was set up by now. Without this one's listener
            # (ClearAllForwardings in ssh_config, say) it never will be.
            self.ready = self.step == 2
            self.settled.set()
        elif self.step == 0 and text == _LISTENING % self.port:
            self.step = 1
        elif self.step == 1:
            if _LISTENER.match(text):
                self.step = 2
            elif text.startswith('Local forwarding listening on'):
                self.step = -1


class Tunnel(object):
    """One ssh process and the local port it listens on."""

    def __init__(self, spec):
        self.spec = spec
        self.users = 0
        self.process = None
        self.port = None
        self.ready = threading.Event()
        self.error = None
        self.idle_since = None
        self.stderr = bytearray()
        self.failures = collections.deque(maxlen=20)
        self.reader = None
        self.lock = threading.Lock()
        self.recorded = False

    def alive(self):
        process = self.process
        return process is not None and process.poll() is None

    def stderr_text(self):
        """ssh's standard error without its debug lines (the last STDERR_KEEP bytes)."""
        with self.lock:
            return bytes(self.stderr).decode('utf-8', 'replace')

    def _keep(self, raw):
        with self.lock:
            self.stderr.extend(raw + b'\n')
            if len(self.stderr) > STDERR_KEEP:
                del self.stderr[:len(self.stderr) - STDERR_KEEP]

    def _line(self, raw, announcement):
        line = raw.decode('utf-8', 'replace').strip()
        if _is_debug(line):
            # Matched for readiness, then dropped: debug output names key
            # files and fingerprints, and is never kept or shown.
            if announcement is not None:
                announcement.hear(line.split(':', 1)[1].strip())
            return
        self._keep(raw.rstrip(b'\r'))
        if 'open failed' in line:
            self.failures.append((time.monotonic(), line))

    def _read(self, pipe, announcement=None):
        """Drain ssh's standard error until it closes (ssh would block on a
        full pipe): debug lines go to *announcement*, the rest is kept."""
        pending = b''
        try:
            while True:
                chunk = pipe.read1(4096) if hasattr(pipe, 'read1') else pipe.read(4096)
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b'\n')
                if len(pending) > 8192:
                    pending = pending[-8192:]  # A line without end: keep its tail.
                for raw in lines:
                    try:
                        self._line(raw, announcement)
                    except Exception:  # pragma: no cover - never stop draining
                        pass
            if pending:
                self._line(pending, announcement)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except OSError:
                pass


class Lease(object):
    """One user's hold on a tunnel; release() once (later calls do nothing)."""

    host = '127.0.0.1'

    def __init__(self, manager, tunnel):
        self._manager, self._tunnel = manager, tunnel
        self.port = tunnel.port
        self.spec = tunnel.spec
        self._released = False
        self._lock = threading.Lock()

    def alive(self):
        """Whether its ssh still runs (and so still holds the port). Once it
        ended, another program may listen on that port: never connect to it."""
        tunnel = self._tunnel
        return tunnel.error is None and tunnel.port == self.port and tunnel.alive()

    def release(self):
        with self._lock:
            if self._released:
                return
            self._released = True
        self._manager._release(self._tunnel)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


class TunnelManager(object):
    """The tunnels of this process (see the module contract)."""

    def __init__(self, idle_seconds=IDLE_SECONDS, ready_timeout=READY_TIMEOUT, max_tunnels=MAX_TUNNELS,
                 clock=time.monotonic, binary=None):
        self.idle_seconds = idle_seconds
        self.ready_timeout = ready_timeout
        self.max_tunnels = max_tunnels
        self.clock = clock
        self.binary = binary
        self.lock = threading.Lock()
        self.tunnels = {}
        self.groups = None
        self._reaper = None
        self._wake = threading.Event()
        self._closing = threading.Lock()

    # -- process groups -------------------------------------------------------------

    def attach_groups(self, groups):
        """List tunnel process groups in *groups* (lifecycle.ToolGroups) from now on."""
        with self.lock:
            self.groups = groups

    def detach_groups(self, groups):
        with self.lock:
            if self.groups is groups:
                self.groups = None

    # -- leases ---------------------------------------------------------------------

    def acquire(self, spec):
        """A Lease on the tunnel for *spec*, started when needed. Raises TunnelError."""
        stale, evicted = [], []
        with self.lock:
            tunnel = self.tunnels.get(spec)
            if tunnel is not None and tunnel.ready.is_set() and (tunnel.error is not None or not tunnel.alive()):
                # Died (or failed to start): never reused. Its own users keep
                # their client's error; the next use starts a new one.
                del self.tunnels[spec]
                stale.append(tunnel)
                tunnel = None
            starter = tunnel is None
            if starter:
                if len(self.tunnels) >= self.max_tunnels:
                    idle = sorted((t for t in self.tunnels.values() if t.users == 0 and t.ready.is_set()),
                                  key=lambda t: t.idle_since or 0)
                    if not idle:
                        raise TunnelError('%d SSH tunnels are in use. Close a Query editor tab or wait for a '
                                          'running read to finish, then try again.' % self.max_tunnels)
                    for t in idle[:len(self.tunnels) - self.max_tunnels + 1]:
                        del self.tunnels[t.spec]
                        evicted.append(t)
                tunnel = Tunnel(spec)
                self.tunnels[spec] = tunnel
            tunnel.users += 1
            tunnel.idle_since = None
        for old in stale + evicted:
            self._stop(old)
        if starter:
            try:
                self._open(tunnel)
            except BaseException as exc:
                tunnel.error = exc if isinstance(exc, TunnelError) else TunnelError(str(exc) or type(exc).__name__)
                with self.lock:
                    if self.tunnels.get(spec) is tunnel:
                        del self.tunnels[spec]
                    tunnel.users -= 1
                tunnel.ready.set()
                self._stop(tunnel)
                if isinstance(exc, TunnelError):
                    raise
                if isinstance(exc, Exception):
                    raise tunnel.error from None
                raise
            with self.lock:
                kept = self.tunnels.get(spec) is tunnel
                if not kept:
                    tunnel.users -= 1
            if not kept:
                # close_all() ran while it opened (the workspace is closing):
                # never hand out a tunnel nothing would close.
                tunnel.error = TunnelError('The SSH tunnel through %s was closed while it opened; nothing was '
                                           'sent.' % spec.ssh_host)
                tunnel.ready.set()
                self._stop(tunnel)
                raise tunnel.error
            tunnel.ready.set()
            self._ensure_reaper()
        else:
            tunnel.ready.wait(self.ready_timeout * 2 + 10)
            if tunnel.error is not None or not tunnel.ready.is_set() or not tunnel.alive():
                self._release(tunnel)
                if tunnel.error is not None:
                    raise TunnelError(str(tunnel.error))
                raise TunnelError('The SSH tunnel through %s stopped. Try again.' % spec.ssh_host)
        return Lease(self, tunnel)

    def lease(self, spec):
        """acquire() for a with statement."""
        return self.acquire(spec)

    def _release(self, tunnel):
        with self.lock:
            tunnel.users = max(0, tunnel.users - 1)
            if tunnel.users == 0:
                tunnel.idle_since = self.clock()
        self._wake.set()

    def channel_problem(self, spec, since):
        """What ssh said about a forward that could not reach the database
        since *since* (time.monotonic()), in plain words, or None."""
        with self.lock:
            tunnel = self.tunnels.get(spec)
        if tunnel is None:
            return None
        lines = [line for at, line in list(tunnel.failures) if at >= since]
        return channel_message(lines[-1], spec) if lines else None

    # -- starting and stopping ------------------------------------------------------

    def _spawn(self, argv):
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                      env=ssh_environment())
        if os.name == 'posix':
            # Its own process group: a Ctrl+C in the terminal, or a signal to
            # the workspace's group, never ends the tunnel under a KILL QUERY.
            kwargs['start_new_session'] = True
            watchdog = watchdog_argv()
            if watchdog:
                # The watchdog leads the group and starts ssh in it; its
                # standard input is a pipe only this process writes to (and
                # never does): it closes when this process ends, however.
                argv = watchdog + ['--'] + list(argv)
                kwargs['stdin'] = subprocess.PIPE
        else:
            kwargs['creationflags'] = (getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
                                       | getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if os.name == 'nt':
                # Suspended until it is in this process's job (_join_job).
                from .win_job import CREATE_SUSPENDED
                kwargs['creationflags'] |= CREATE_SUSPENDED

        def spawn():
            return subprocess.Popen(argv, **kwargs)
        with self.lock:
            groups = self.groups
        if groups is None or os.name != 'posix':
            process = spawn()
            if os.name == 'nt':
                _join_job(process)
            return process, False
        try:
            process, recorded = groups.start(spawn, service=True)
        except ConnectorError as exc:
            if getattr(exc, 'limit', False):
                raise TunnelError(str(exc)) from None
            raise TunnelError('The workspace is closing; the SSH tunnel was not opened.') from None
        if not recorded:
            _signal_group(process.pid, 9)
            process.wait()
            _close_pipes(process)
            groups.settle(process.pid)
            raise TunnelError('Could not record the SSH tunnel for a safe shutdown, so it was stopped right after '
                              'it started.')
        return process, True

    def _open(self, tunnel):
        spec = tunnel.spec
        binary = self.binary or find_ssh()
        for attempt in (1, 2):
            port = free_port()
            try:
                process, recorded = self._spawn(ssh_argv(binary, spec, port))
            except OSError as exc:
                raise TunnelError('Could not start ssh (%s): %s' % (binary, exc)) from None
            tunnel.process, tunnel.port, tunnel.recorded = process, port, recorded
            with tunnel.lock:
                tunnel.stderr = bytearray()
            announcement = Announcement(port)
            tunnel.reader = threading.Thread(target=tunnel._read, args=(process.stderr, announcement),
                                             name='ssh-tunnel-stderr', daemon=True)
            tunnel.reader.start()
            state = self._wait_ready(tunnel, announcement)
            if state == 'ready':
                return
            self._stop(tunnel, keep_state=True)
            text = tunnel.stderr_text()
            if state == 'exited' and attempt == 1 and forward_failed(text):
                continue  # Another program took the port first: once more with a new one.
            if state == 'no-forward':
                raise TunnelError('ssh signed in to %s but did not open the local end of the tunnel. Check '
                                  '~/.ssh/config for a setting such as ClearAllForwardings on that host.'
                                  % spec.ssh_host)
            if state == 'timeout':
                lines = _lines(text)
                raise TunnelError('The SSH tunnel through %s did not open within %d seconds.%s' % (
                    spec.ssh_host, int(self.ready_timeout),
                    (' ssh said: ' + lines[-1][:300]) if lines else ' Check the SSH host and the network.'))
            raise TunnelError(failure_message(text, spec))
        raise TunnelError(failure_message(tunnel.stderr_text(), spec))

    def _wait_ready(self, tunnel, announcement):
        """'ready', 'exited', 'no-forward' (ssh signed in without setting up
        this forward) or 'timeout' (real time, whatever the idle clock).

        Ready only once ssh itself said it listens on this start's port and
        entered its session (Announcement), while it still runs. A TCP
        connection to the port proves nothing: another local program may have
        taken it before ssh's bind, and a probe would reach the database as
        an aborted connect."""
        deadline = time.monotonic() + self.ready_timeout
        process = tunnel.process
        while True:
            settled = announcement.settled.wait(0.05)
            if process.poll() is not None:
                if tunnel.reader is not None:
                    tunnel.reader.join(2)
                return 'exited'
            if settled:
                return 'ready' if announcement.ready else 'no-forward'
            if time.monotonic() >= deadline:
                return 'timeout'

    def _stop(self, tunnel, keep_state=False):
        process = tunnel.process
        if process is None:
            return
        try:
            if process.poll() is None:
                if os.name == 'posix':
                    # The whole group: the watchdog and ssh (and a ProxyCommand).
                    _signal_group(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if os.name == 'posix':
                        _signal_group(process.pid, 9)
                    else:
                        process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            else:
                process.wait()
            # A ProxyCommand child can outlive ssh in its group; the group id
            # cannot be reused while any member is left.
            if os.name == 'posix' and _group_alive(process.pid):
                _signal_group(process.pid, 9)
        finally:
            if tunnel.reader is not None:
                tunnel.reader.join(2)
            if process.stdin:
                try:
                    process.stdin.close()  # The watchdog's pipe.
                except OSError:
                    pass
            with self.lock:
                groups = self.groups
            if tunnel.recorded and groups is not None:
                # Unlisted once the group is confirmed gone (ToolGroups.settle).
                groups.settle(process.pid)
            tunnel.recorded = False
            if not keep_state:
                tunnel.process = None

    # -- idle close and shutdown ----------------------------------------------------

    def _ensure_reaper(self):
        with self.lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, name='ssh-tunnel-idle', daemon=True)
            self._reaper.start()

    def _reap_loop(self):
        while True:
            self._wake.wait(min(max(self.idle_seconds / 4.0, 1.0), 30.0))
            self._wake.clear()
            self.reap()
            with self.lock:
                if not self.tunnels:
                    self._reaper = None
                    return

    def reap(self):
        """Stop tunnels idle for idle_seconds, and dead ones nobody uses."""
        now = self.clock()
        done = []
        with self.lock:
            for spec, tunnel in list(self.tunnels.items()):
                if not tunnel.ready.is_set() or tunnel.users:
                    continue
                if not tunnel.alive() or (tunnel.idle_since is not None and now - tunnel.idle_since >= self.idle_seconds):
                    del self.tunnels[spec]
                    done.append(tunnel)
        for tunnel in done:
            self._stop(tunnel)
        return len(done)

    def close_all(self):
        """Stop every tunnel now (shutdown). Leases still held keep their
        client's errors; a later acquire starts a new tunnel. Signals every
        tunnel first, then waits for each; a second caller (the workspace's
        shutdown deadline and its final close) waits for the first."""
        with self._closing:
            with self.lock:
                tunnels = list(self.tunnels.values())
                self.tunnels.clear()
            for tunnel in tunnels:
                process = tunnel.process
                if os.name == 'posix' and process is not None and process.poll() is None:
                    _signal_group(process.pid, signal.SIGTERM)
            for tunnel in tunnels:
                # One still starting is stopped too: its starter then sees ssh
                # exit and reports the tunnel as not opened.
                self._stop(tunnel, keep_state=not tunnel.ready.is_set())

    def owned_groups(self):
        """Process groups to kill when this process must end at once (the
        desktop backend after a hung shutdown, backend-entry.py): every
        tunnel's, and every group listed in the attached ToolGroups (dump,
        restore and clone tools too). POSIX only; never waits on a lock for
        more than a moment."""
        found = set()
        locked = self.lock.acquire(timeout=0.5)
        try:
            tunnels, groups = list(self.tunnels.values()), self.groups
        finally:
            if locked:
                self.lock.release()
        for tunnel in tunnels:
            process = tunnel.process
            if process is not None and process.poll() is None:
                found.add(process.pid)
        if groups is not None:
            found.update(groups.snapshot(timeout=0.5))
        return sorted(found)

    def snapshot(self):
        """[(spec, users, port, alive)] for tests and diagnostics."""
        with self.lock:
            return [(t.spec, t.users, t.port, t.alive()) for t in self.tunnels.values()]


def watchdog_argv():
    """The command that starts ssh_watchdog.py, which the tunnel's ssh
    argument list follows (after ``--``), or None to start ssh directly."""
    from . import ssh_watchdog
    if getattr(sys, 'frozen', False):
        # The packaged backend (apps/desktop/scripts/backend-entry.py).
        return [sys.executable, ssh_watchdog.ARG] if sys.executable else None
    script = getattr(ssh_watchdog, '__file__', None)
    if not sys.executable or not script or not os.path.isfile(script):
        return None
    # -I: no PYTHON* variables, user site or script folder on sys.path.
    return [sys.executable, '-I', os.path.abspath(script)]


def _close_pipes(process):
    for pipe in (process.stdin, process.stderr):
        if pipe:
            try:
                pipe.close()
            except OSError:
                pass


def _join_job(process):
    """Windows: put *process* (an ssh.exe started suspended) in this
    process's job object and let it run, so it ends with this process however
    that ends (win_job). When Windows refuses, ssh is stopped before it ran
    and the tunnel is refused: never an ssh nothing would end."""
    from . import win_job
    try:
        win_job.JOB.adopt(process)
    except BaseException as exc:  # OSError from Windows, what ctypes raised, or Ctrl+C.
        # Nothing else knows this process yet (_spawn has not returned), so it
        # is stopped here whatever interrupted the adoption.
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        _close_pipes(process)
        if not isinstance(exc, Exception):
            raise
        reason = (getattr(exc, 'strerror', None) or str(exc) or type(exc).__name__).strip().rstrip('.')
        raise TunnelError('Windows could not tie the SSH tunnel to myflames (job object: %s), so ssh was stopped '
                          'before it connected. Nothing was sent.' % reason) from None


#: The tunnels of this process. The workspace attaches its ToolGroups and
#: closes them on shutdown; the CLI closes them at exit (cli.main makes
#: SIGTERM and SIGHUP exit normally, so atexit runs); the watchdog (POSIX)
#: or the job object (Windows) covers an exit without either.
TUNNELS = TunnelManager()
atexit.register(TUNNELS.close_all)


def channel_note(value, since):
    """" SSH host x could not reach ..." for a failed client invocation of
    connection *value* that started at *since*, or ''."""
    try:
        spec = spec_of(value)
    except ValueError:
        return ''
    if spec is None:
        return ''
    note = TUNNELS.channel_problem(spec, since)
    return ('; ' + note) if note else ''


# ---- ~/.ssh/config Host aliases --------------------------------------------------------

_MAX_CONFIG_FILES = 32
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_HOSTS = 500


def _config_words(line):
    """Keyword and arguments of one ssh_config line ("Host a b", "Include=x", quoted words)."""
    line = line.strip()
    if not line or line.startswith('#'):
        return None, []
    match = re.match(r'^([A-Za-z]+)\s*(?:=\s*|\s+)(.*)$', line)
    if not match:
        return None, []
    words, rest = [], match.group(2)
    for quoted, plain in re.findall(r'"([^"]*)"|(\S+)', rest):
        word = quoted if quoted else plain
        if word.startswith('#') and not quoted:
            break
        words.append(word)
    return match.group(1).lower(), words


def config_hosts(home=None):
    """Host aliases of ~/.ssh/config and the files it Includes, sorted: the
    names a Host line gives, without patterns (``*``, ``?``), negations
    (``!``) or anything the SSH host field would refuse. Bounded: at most 32
    files of 256 KiB, 500 names."""
    home = home or ssh_home()
    base = os.path.join(home, '.ssh')
    seen_files, names = set(), {}

    def read(path, depth):
        if depth > 4 or len(seen_files) >= _MAX_CONFIG_FILES:
            return
        real = os.path.realpath(path)
        if real in seen_files or not os.path.isfile(real):
            return
        seen_files.add(real)
        try:
            with open(real, 'rb') as handle:
                data = handle.read(_MAX_CONFIG_BYTES)
        except OSError:
            return
        for line in data.decode('utf-8', 'replace').splitlines():
            keyword, words = _config_words(line)
            if keyword == 'host':
                for word in words:
                    if len(names) >= _MAX_HOSTS:
                        return
                    if any(c in word for c in '*?!') or not _SSH_HOST.match(word):
                        continue
                    names.setdefault(word.lower(), word)
            elif keyword == 'include':
                for word in words:
                    if word.startswith('~/') or word.startswith('~\\'):
                        pattern = os.path.join(home, word[2:])
                    elif word.startswith('~'):
                        continue  # Another user's home: not this user's configuration.
                    else:
                        # ssh reads a relative Include of a user file from ~/.ssh.
                        pattern = word if os.path.isabs(word) else os.path.join(base, word)
                    for match in sorted(glob.glob(pattern))[:_MAX_CONFIG_FILES]:
                        read(match, depth + 1)

    read(os.path.join(base, 'config'), 0)
    return sorted(names.values(), key=lambda name: name.lower())


def hosts_response():
    """``ssh-hosts-1.0``: the Host aliases for the connection form's SSH host
    pick list, and whether ssh is installed (with what to do when not)."""
    result = {'schema_version': 'ssh-hosts-1.0', 'hosts': [], 'ssh_found': True, 'message': ''}
    try:
        find_ssh()
    except TunnelError as exc:
        result.update(ssh_found=False, message=str(exc))
    try:
        result['hosts'] = config_hosts()
    except (OSError, ValueError):
        result['hosts'] = []
    return result
