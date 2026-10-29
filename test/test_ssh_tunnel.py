"""SSH tunnels for saved connections (myflames/ssh_tunnel.py).

Most tests start a fake ssh (test/_fake_ssh.py, copied as an executable
named ``ssh``) that listens on the requested ``-L`` port and forwards to a
local TCP server standing in for the database, or fails with a chosen
message. Nothing here reads the real ~/.ssh or starts the real ssh, except
the optional live test at the end (MYFLAMES_SSH_LIVE=1, see its docstring).
"""
import contextlib
import hashlib
import io
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from myflames import dbops, lifecycle, ssh_tunnel  # noqa: E402
from myflames.connector import ConnectorError, MySQLConnection  # noqa: E402
from myflames.live import connection_options  # noqa: E402
from myflames.ssh_tunnel import Spec, TunnelError, TunnelManager  # noqa: E402

FAKE_SSH = Path(__file__).resolve().parent / '_fake_ssh.py'
POSIX = os.name == 'posix'


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class EchoServer(object):
    """A TCP server standing in for the database: answers each line with 'pong:<line>'."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.connections = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._answer, args=(client,), daemon=True).start()

    @staticmethod
    def _answer(client):
        try:
            data = b''
            while not data.endswith(b'\n'):
                chunk = client.recv(1024)
                if not chunk:
                    return
                data += chunk
            client.sendall(b'pong:' + data)
        except OSError:
            pass
        finally:
            client.close()

    def close(self):
        self.sock.close()


class FakeSsh(object):
    """A directory with the fake ssh and its settings."""

    def __init__(self, directory, **settings):
        self.dir = Path(directory)
        self.path = self.dir / 'ssh'
        self.path.write_text('#!%s\n%s' % (sys.executable, FAKE_SSH.read_text(encoding='utf-8')), encoding='utf-8')
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.configure(**settings)

    def configure(self, **settings):
        (self.dir / 'fake-ssh.json').write_text(json.dumps(settings), encoding='utf-8')

    def starts(self):
        path = self.dir / 'starts.jsonl'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


class ManagerCase(unittest.TestCase):
    """A fake ssh, a database stand-in and a TunnelManager of its own."""

    settings = {}
    idle_seconds = 300.0
    ready_timeout = 5.0

    def setUp(self):
        if not POSIX:
            self.skipTest('the fake ssh is a POSIX script')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.temp = Path(temp.name)
        self.fake = FakeSsh(self.temp, **self.settings)
        self.db = EchoServer()
        self.addCleanup(self.db.close)
        self.now = [1000.0]
        self.manager = TunnelManager(idle_seconds=self.idle_seconds, ready_timeout=self.ready_timeout,
                                     clock=lambda: self.now[0], binary=str(self.fake.path))
        self.addCleanup(self.manager.close_all)

    def spec(self, **changes):
        values = dict(ssh_host='bastion', ssh_user='deploy', ssh_port=2222, db_host='127.0.0.1',
                      db_port=self.db.port)
        values.update(changes)
        return Spec(**values)

    def roundtrip(self, port, text=b'ping'):
        with socket.create_connection(('127.0.0.1', port), timeout=5) as sock:
            sock.sendall(text + b'\n')
            data = b''
            while not data.endswith(b'\n'):
                chunk = sock.recv(1024)
                if not chunk:
                    break
                data += chunk
        return data


# ---- validation ------------------------------------------------------------------------

class TestValidation(unittest.TestCase):

    def test_fields_are_trimmed_and_typed(self):
        self.assertEqual(ssh_tunnel.ssh_fields({'ssh_host': ' bastion.example\u200b ', 'ssh_user': ' deploy ',
                                                'ssh_port': '2222'}),
                         {'ssh_host': 'bastion.example', 'ssh_user': 'deploy', 'ssh_port': 2222})
        self.assertEqual(ssh_tunnel.ssh_fields({}), {})
        self.assertEqual(ssh_tunnel.ssh_fields({'ssh_host': '', 'ssh_user': '', 'ssh_port': 0}), {})
        self.assertEqual(ssh_tunnel.ssh_fields({'ssh_host': 'b', 'ssh_port': None}), {'ssh_host': 'b'})

    def test_option_like_and_unsafe_hosts_are_refused(self):
        for host in ('-oProxyCommand=touch /tmp/pwned', '-oProxyCommand', '-v', 'bastion -oProxyCommand=x',
                     'bastion;id', 'bastion|id', '$(id)', '`id`', 'a b', 'bastion\n-oX', '%h', '~/x', '"q"',
                     "it's", 'ssh://bastion', 'bastion/x', '[::1]', 'bastion:22', 'deploy@bastion', '.' * 300):
            with self.subTest(host=host), self.assertRaises(ValueError) as caught:
                ssh_tunnel.ssh_fields({'ssh_host': host})
            self.assertNotIn('Invalid connection field', str(caught.exception))
        with self.assertRaisesRegex(ValueError, 'SSH user in SSH user'):
            ssh_tunnel.ssh_fields({'ssh_host': 'deploy@bastion'})
        with self.assertRaisesRegex(ValueError, 'port in SSH port'):
            ssh_tunnel.ssh_fields({'ssh_host': 'bastion:2222'})
        with self.assertRaisesRegex(ValueError, 'not starting with a hyphen'):
            ssh_tunnel.ssh_fields({'ssh_host': '-oProxyCommand=evil'})

    def test_users_and_ports_are_checked(self):
        for user in ('-oProxyCommand=x', '-l', '@root', 'a b', 'a;b', 'x' * 200, 'dom\\user', '%u'):
            with self.subTest(user=user), self.assertRaisesRegex(ValueError, 'SSH user'):
                ssh_tunnel.ssh_fields({'ssh_host': 'bastion', 'ssh_user': user})
        self.assertEqual(ssh_tunnel.ssh_fields({'ssh_host': 'b', 'ssh_user': 'jane.doe@corp'})['ssh_user'],
                         'jane.doe@corp')
        for port in (70000, -1, '22a', 1.5, True, '-p'):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, 'SSH port'):
                ssh_tunnel.ssh_fields({'ssh_host': 'bastion', 'ssh_port': port})
        for field in ('ssh_host', 'ssh_user'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'Invalid connection field'):
                ssh_tunnel.ssh_fields({'ssh_host': 'b', field: ['x']})

    def test_user_or_port_without_host_is_refused(self):
        for value in ({'ssh_user': 'deploy'}, {'ssh_port': 22}, {'ssh_host': '  ', 'ssh_port': 22}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'need an SSH host'):
                ssh_tunnel.ssh_fields(value)

    def test_connection_options_carry_ssh_and_refuse_what_cannot_work(self):
        options = connection_options({'host': '10.0.0.5', 'port': 3307, 'user': 'app', 'ssh_host': 'bastion'})
        self.assertEqual((options['host'], options['port'], options['ssh_host']), ('10.0.0.5', 3307, 'bastion'))
        self.assertNotIn('ssh_user', options)
        plain = connection_options({'host': '10.0.0.5', 'port': 3307})
        self.assertFalse(any(key.startswith('ssh_') for key in plain))
        with self.assertRaisesRegex(ValueError, 'VERIFY_IDENTITY cannot be used through SSH'):
            connection_options({'host': 'db', 'ssl_mode': 'VERIFY_IDENTITY', 'ssh_host': 'bastion'})
        self.assertEqual(connection_options({'host': 'db', 'ssl_mode': 'VERIFY_CA', 'ssh_host': 'b'})['ssl_mode'],
                         'VERIFY_CA')
        with self.assertRaisesRegex(ValueError, 'cannot be used through SSH'):
            connection_options({'host': 'db[1]', 'ssh_host': 'bastion'})
        with self.assertRaisesRegex(ValueError, 'uses TCP'):
            lifecycle._options({'host': 'db', 'socket': '/tmp/mysql.sock', 'ssh_host': 'bastion'})

    def test_patterns_end_at_the_end_of_the_value(self):
        # "$" also matches before a final newline; the patterns must not.
        for host in ('db.internal\n', 'fd00::5\n', '10.0.0.5\n'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                ssh_tunnel.forward_host(host)
        self.assertIsNone(ssh_tunnel._SSH_HOST.match('bastion\n'))
        self.assertIsNone(ssh_tunnel._SSH_USER.match('deploy\n'))
        self.assertIsNone(ssh_tunnel._DB_NAME.match('db\n'))
        self.assertIsNone(ssh_tunnel._DB_IPV6.match('::1\n'))
        self.assertTrue(ssh_tunnel._SSH_HOST.match('bastion') and ssh_tunnel._SSH_USER.match('deploy'))

    def test_forward_host_brackets_ipv6(self):
        self.assertEqual(ssh_tunnel.forward_host('db.internal'), 'db.internal')
        self.assertEqual(ssh_tunnel.forward_host('10.0.0.5'), '10.0.0.5')
        self.assertEqual(ssh_tunnel.forward_host('fd00::5'), '[fd00::5]')
        # live.clean_host strips the brackets a URL puts around an address.
        options = connection_options({'host': '[::1]', 'ssh_host': 'bastion'})
        self.assertEqual(ssh_tunnel.spec_of(options).db_host, '::1')


# ---- ssh's command line and environment -------------------------------------------------

class TestCommandLine(unittest.TestCase):

    def test_argument_list_ends_options_before_the_target(self):
        argv = ssh_tunnel.ssh_argv('/usr/bin/ssh', Spec('bastion', 'deploy', 2222, 'db.internal', 3306), 50123)
        self.assertEqual(argv[:3], ['/usr/bin/ssh', '-N', '-T'])
        options = [argv[i + 1] for i, part in enumerate(argv) if part == '-o']
        for option in ('BatchMode=yes', 'ExitOnForwardFailure=yes', 'ConnectTimeout=10', 'ServerAliveInterval=15',
                       'ServerAliveCountMax=3',
                       # Readiness comes from ssh's own words (see TestReadiness).
                       'LogLevel=DEBUG1',
                       # myflames never changes known_hosts and runs no LocalCommand.
                       'UpdateHostKeys=no', 'PermitLocalCommand=no'):
            self.assertIn(option, options)
        # ClearAllForwardings would also clear this tunnel's own -L.
        self.assertFalse(any(option.startswith('ClearAllForwardings') for option in options))
        self.assertEqual(argv[argv.index('-p') + 1], '2222')
        self.assertEqual(argv[argv.index('-l') + 1], 'deploy')
        self.assertEqual(argv[-4:], ['-L', '127.0.0.1:50123:db.internal:3306', '--', 'bastion'])
        if POSIX:
            self.assertIn('ControlPath=none', argv)
            self.assertIn('ControlMaster=no', argv)
        # Without a user or port, ~/.ssh/config decides: no -l or -p at all.
        bare = ssh_tunnel.ssh_argv('ssh', Spec('bastion', '', 0, 'fd00::5', 3306), 4000)
        self.assertNotIn('-l', bare)
        self.assertNotIn('-p', bare)
        self.assertEqual(bare[-4:], ['-L', '127.0.0.1:4000:[fd00::5]:3306', '--', 'bastion'])
        # Every element is one argument: nothing for a shell to split.
        self.assertTrue(all(isinstance(part, str) for part in argv))

    def test_environment_has_the_real_home_and_agent_but_no_database_secrets(self):
        env = {'PATH': '/usr/bin', 'HOME': '/isolated/backend-home', 'MYFLAMES_SSH_HOME': '/Users/jane',
               'MYFLAMES_SSH_AUTH_SOCK': '/private/tmp/agent.sock', 'SSH_AUTH_SOCK': '/other.sock',
               'MYSQL_PWD': 'secret', 'MYSQL_HOST': 'evil', 'AWS_SECRET_ACCESS_KEY': 'x',
               'MYFLAMES_VAULT_PASSPHRASE': 'v', 'DISPLAY': ':0', 'SSH_ASKPASS': '/bin/askpass'}
        with mock.patch.dict(os.environ, env, clear=True):
            result = ssh_tunnel.ssh_environment()
        self.assertEqual(result['HOME'], '/Users/jane' if POSIX else result['HOME'])
        self.assertEqual(result['SSH_AUTH_SOCK'], '/private/tmp/agent.sock')
        for key in ('MYSQL_PWD', 'MYSQL_HOST', 'AWS_SECRET_ACCESS_KEY', 'MYFLAMES_VAULT_PASSPHRASE', 'DISPLAY',
                    'SSH_ASKPASS', 'MYFLAMES_SSH_HOME', 'MYFLAMES_SSH_AUTH_SOCK'):
            self.assertNotIn(key, result)
        with mock.patch.dict(os.environ, {'PATH': '/usr/bin', 'HOME': '/home/jane', 'SSH_AUTH_SOCK': '/a.sock'},
                             clear=True):
            plain = ssh_tunnel.ssh_environment()
        self.assertEqual((plain['HOME'], plain['SSH_AUTH_SOCK']), ('/home/jane', '/a.sock') if POSIX else
                         (plain['HOME'], '/a.sock'))

    def test_ssh_is_found_on_path_else_reported(self):
        # SYSTEMROOT names a folder without OpenSSH, so on Windows too only PATH decides.
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'SYSTEMROOT': directory}):
            fake = os.path.join(directory, 'ssh.exe' if os.name == 'nt' else 'ssh')
            with open(fake, 'w') as handle:
                handle.write('')
            os.chmod(fake, 0o755)
            self.assertEqual(ssh_tunnel.find_ssh(path=directory), os.path.abspath(fake))
        with tempfile.TemporaryDirectory() as empty, \
                mock.patch.object(ssh_tunnel.os.path, 'isfile', return_value=False):
            with self.assertRaisesRegex(TunnelError, '^ssh was not found'):
                ssh_tunnel.find_ssh(path=empty)

    def test_path_search_skips_relative_entries(self):
        # "." or a relative PATH entry names the current folder: never search it.
        name = 'ssh.exe' if os.name == 'nt' else 'ssh'
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(ssh_tunnel, '_POSIX_FOLDERS', ()), \
                mock.patch.dict(os.environ, {'SYSTEMROOT': directory}):
            os.mkdir(os.path.join(directory, 'bin'))
            for folder in (directory, os.path.join(directory, 'bin')):
                fake = os.path.join(folder, name)
                with open(fake, 'w') as handle:
                    handle.write('')
                os.chmod(fake, 0o755)
            before = os.getcwd()
            os.chdir(directory)
            try:
                for path in ('.', 'bin', '', os.pathsep.join(['.', 'bin'])):
                    with self.subTest(path=path), self.assertRaisesRegex(TunnelError, 'ssh was not found'):
                        ssh_tunnel.find_ssh(path=path)
                self.assertEqual(ssh_tunnel.find_ssh(path=os.pathsep.join(['.', os.path.join(directory, 'bin')])),
                                 os.path.join(directory, 'bin', name))
            finally:
                os.chdir(before)

    def test_windows_prefers_system32_openssh(self):
        present = {'C:\\Windows\\System32\\OpenSSH\\ssh.exe', 'C:\\Tools\\ssh.exe'}
        with mock.patch.object(ssh_tunnel.os, 'name', 'nt'), \
                mock.patch.dict(os.environ, {'SYSTEMROOT': 'C:\\Windows'}), \
                mock.patch.object(ssh_tunnel, '_is_program', side_effect=lambda path: path in present):
            self.assertEqual(ssh_tunnel.find_ssh(path='C:\\Tools'), 'C:\\Windows\\System32\\OpenSSH\\ssh.exe')

    def test_windows_path_search_takes_only_ssh_exe_from_absolute_folders(self):
        # Without System32\OpenSSH: never the current folder or a relative entry,
        # and never ssh.bat, ssh.cmd or ssh.com (PATHEXT) in place of ssh.exe.
        present = {'.\\ssh.exe', 'tools\\ssh.exe', 'C:\\Scripts\\ssh.bat', 'C:\\Scripts\\ssh.cmd',
                   'C:\\Scripts\\ssh.com', 'C:\\Scripts\\ssh', 'C:\\Git\\usr\\bin\\ssh.exe'}
        checked = []

        def is_program(path):
            checked.append(path)
            return path in present
        with mock.patch.object(ssh_tunnel.os, 'name', 'nt'), \
                mock.patch.dict(os.environ, {'SYSTEMROOT': 'C:\\Windows', 'PATHEXT': '.COM;.EXE;.BAT;.CMD'}), \
                mock.patch.object(ssh_tunnel, '_is_program', side_effect=is_program):
            found = ssh_tunnel.find_ssh(path='.;tools;;C:\\Scripts;"C:\\Git\\usr\\bin"')
            self.assertEqual(found, 'C:\\Git\\usr\\bin\\ssh.exe')
            self.assertFalse(any(not path.lower().endswith('ssh.exe') for path in checked), checked)
            self.assertFalse(any(path.startswith(('.', 'tools')) for path in checked), checked)
            with self.assertRaisesRegex(TunnelError, 'ssh was not found'):
                ssh_tunnel.find_ssh(path='.;tools;C:\\Scripts')

    def test_windows_looks_in_system32_openssh(self):
        seen = []

        def isfile(path):
            seen.append(path)
            return False
        with mock.patch.object(ssh_tunnel.os, 'name', 'nt'), \
                mock.patch.dict(os.environ, {'SYSTEMROOT': 'C:\\Windows'}), \
                mock.patch.object(ssh_tunnel.os.path, 'isfile', side_effect=isfile):
            with self.assertRaisesRegex(TunnelError, 'ssh was not found'):
                ssh_tunnel.find_ssh()
        self.assertTrue(any('System32' in path and 'OpenSSH' in path and path.endswith('ssh.exe') for path in seen))


class TestMessages(unittest.TestCase):
    spec = Spec('bastion', 'deploy', 0, 'db.internal', 3306)

    def test_stderr_is_mapped_to_plain_words(self):
        cases = [
            ('No ED25519 host key is known for bastion and you have requested strict checking.\r\n'
             'Host key verification failed.\r\n', 'The SSH host key of bastion is not known yet. Run `ssh bastion` once'),
            ('@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n'
             '@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\nHost key verification failed.\n',
             'has changed since it was saved in known_hosts'),
            ('deploy@bastion: Permission denied (publickey).\r\n', 'refused the sign-in (Permission denied)'),
            ('ssh: Could not resolve hostname bastion: nodename nor servname provided, or not known\r\n',
             'Could not resolve the SSH host bastion'),
            ('ssh: connect to host bastion port 22: Connection refused\r\n', 'SSH host bastion refused the connection'),
            ('ssh: connect to host bastion port 22: Operation timed out\r\n', 'Timed out connecting to SSH host bastion'),
            ('ssh: connect to host bastion port 22: No route to host\r\n', 'cannot be reached from this computer'),
            ('bind [127.0.0.1]:4000: Address already in use\r\nCould not request local forwarding.\r\n',
             'Could not open the local end'),
            ('kex_exchange_identification: Connection closed by remote host\r\n',
             'did not open: kex_exchange_identification: Connection closed by remote host'),
            ('', 'ssh stopped without a message'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertIn(expected, ssh_tunnel.failure_message(text, self.spec))
        self.assertIn('never a password', ssh_tunnel.failure_message('Permission denied (publickey).', self.spec))

    def test_the_host_key_hint_names_a_non_default_port(self):
        unknown = 'Host key verification failed.\r\n'
        self.assertIn('Run `ssh -p 2222 bastion` once in Terminal',
                      ssh_tunnel.failure_message(unknown, self.spec._replace(ssh_port=2222)))
        for port in (0, 22):
            with self.subTest(port=port):
                self.assertIn('Run `ssh bastion` once in Terminal',
                              ssh_tunnel.failure_message(unknown, self.spec._replace(ssh_port=port)))

    def test_debug_lines_never_decide_or_show_in_the_message(self):
        text = ('debug1: Reading configuration data /home/jane/.ssh/config\r\n'
                'debug1: load_hostkeys: fopen /etc/ssh/ssh_known_hosts: Permission denied\r\n'
                'debug1: Connecting to bastion [192.0.2.10] port 22.\r\n'
                'ssh: connect to host bastion port 22: Connection refused\r\n')
        message = ssh_tunnel.failure_message(text, self.spec)
        self.assertIn('SSH host bastion refused the connection', message)
        self.assertNotIn('debug1', ssh_tunnel.failure_message('debug1: something\r\n', self.spec))

    def test_terminal_controls_in_stderr_are_escaped(self):
        message = ssh_tunnel.failure_message('weird \x1b]52;c;ZXZpbA==\x07 failure\n', self.spec)
        self.assertNotIn('\x1b', message)
        self.assertNotIn('\x07', message)

    def test_channel_failures_name_the_database(self):
        self.assertEqual(ssh_tunnel.channel_message('channel 2: open failed: connect failed: Connection refused',
                                                    self.spec),
                         'SSH host bastion could not reach the database at db.internal:3306 (Connection refused).')
        self.assertIn('does not allow port forwarding', ssh_tunnel.channel_message(
            'channel 3: open failed: administratively prohibited: open failed', self.spec))
        self.assertIn('could not resolve the database host db.internal', ssh_tunnel.channel_message(
            'channel 2: open failed: connect failed: Name or service not known', self.spec))


# ---- tunnels -------------------------------------------------------------------------

class TestTunnels(ManagerCase):

    def test_a_tunnel_forwards_to_the_database(self):
        with self.manager.acquire(self.spec()) as lease:
            self.assertNotEqual(lease.port, self.db.port)
            self.assertEqual(self.roundtrip(lease.port), b'pong:ping\n')
        start, = self.fake.starts()
        self.assertEqual(start['argv'][-4:], ['-L', '127.0.0.1:%d:127.0.0.1:%d' % (lease.port, self.db.port),
                                              '--', 'bastion'])
        # Its own process group (a Ctrl+C in the terminal does not end it),
        # led by the watchdog that started it (TestOwnerExit).
        leader = self.manager.tunnels[self.spec()].process.pid if self.manager.snapshot() else None
        self.assertEqual(start['pgid'], leader)
        self.assertNotEqual(start['pgid'], os.getpgid(0))
        self.assertEqual(start['ppid'], leader)
        self.assertNotIn('MYSQL_PWD', start['env'])

    def test_shared_and_reference_counted(self):
        spec = self.spec()
        first = self.manager.acquire(spec)
        second = self.manager.acquire(spec)
        self.assertEqual(first.port, second.port)
        self.assertEqual(len(self.fake.starts()), 1)
        self.assertEqual([users for _, users, _, _ in self.manager.snapshot()], [2])
        other = self.manager.acquire(self.spec(db_port=self.db.port + 1 if self.db.port < 65535 else 1))
        self.assertEqual(len(self.fake.starts()), 2)
        self.assertNotEqual(other.port, first.port)
        first.release()
        first.release()  # A second release of the same lease does nothing.
        users = {s: u for s, u, _, _ in self.manager.snapshot()}
        self.assertEqual(users[spec], 1)
        second.release()
        other.release()
        self.assertEqual(sorted(u for _, u, _, _ in self.manager.snapshot()), [0, 0])

    def test_concurrent_first_uses_start_one_ssh(self):
        spec = self.spec()
        leases, errors = [], []

        def take():
            try:
                leases.append(self.manager.acquire(spec))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=take) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(len({lease.port for lease in leases}), 1)
        self.assertEqual(len(self.fake.starts()), 1)
        for lease in leases:
            lease.release()

    def test_idle_tunnels_close_after_the_idle_period(self):
        lease = self.manager.acquire(self.spec())
        pid = self.fake.starts()[0]['pid']
        self.now[0] += 10000  # In use: never idle, however long.
        self.assertEqual(self.manager.reap(), 0)
        lease.release()
        self.now[0] += self.idle_seconds - 1
        self.assertEqual(self.manager.reap(), 0)
        self.assertTrue(_alive(pid))
        self.now[0] += 2
        self.assertEqual(self.manager.reap(), 1)
        self.assertEqual(self.manager.snapshot(), [])
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_alive(pid))

    def test_a_dead_tunnel_is_started_again_on_the_next_use(self):
        self.fake.configure(mode='die-after', lifetime=0.4)
        lease = self.manager.acquire(self.spec())
        first = lease.port
        deadline = time.monotonic() + 5
        while self.manager.snapshot()[0][3] and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.manager.snapshot()[0][3])
        # The user holding the dead one keeps it (its client gets its own
        # error); the next use gets a new ssh.
        self.fake.configure(mode='forward')
        with self.manager.acquire(self.spec()) as again:
            self.assertEqual(self.roundtrip(again.port), b'pong:ping\n')
        self.assertEqual(len(self.fake.starts()), 2)
        lease.release()
        self.assertTrue(first)

    def test_close_all_stops_every_ssh(self):
        a = self.manager.acquire(self.spec())
        self.manager.acquire(self.spec(ssh_host='other'))
        pids = [start['pid'] for start in self.fake.starts()]
        self.manager.close_all()
        self.assertEqual(self.manager.snapshot(), [])
        for pid in pids:
            self.assertFalse(_alive(pid))
        a.release()  # Releasing after shutdown is harmless.

    def test_at_most_max_tunnels_evicting_idle_ones(self):
        self.manager.max_tunnels = 1
        first = self.manager.acquire(self.spec())
        with self.assertRaisesRegex(TunnelError, 'SSH tunnels are in use'):
            self.manager.acquire(self.spec(ssh_host='other'))
        first.release()
        with self.manager.acquire(self.spec(ssh_host='other')):
            self.assertEqual([s.ssh_host for s, _, _, _ in self.manager.snapshot()], ['other'])


class TestPortRetry(ManagerCase):
    settings = {'mode': 'busy-once'}

    def test_a_port_taken_first_is_retried_once_with_another(self):
        with self.manager.acquire(self.spec()) as lease:
            self.assertEqual(self.roundtrip(lease.port), b'pong:ping\n')
        starts = self.fake.starts()
        self.assertEqual(len(starts), 2)
        ports = [s['argv'][s['argv'].index('-L') + 1].split(':')[1] for s in starts]
        self.assertEqual(ports[1], str(lease.port))

    def test_free_port_binds_loopback_port_zero(self):
        port = ssh_tunnel.free_port()
        self.assertTrue(1024 <= port <= 65535)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))  # Free again once picked.


class TestStartFailures(ManagerCase):

    def test_host_key_failure_is_reported_and_nothing_stays_open(self):
        self.fake.configure(mode='fail', stderr='Host key verification failed.\r\n')
        # The spec's SSH port is 2222: known_hosts keys it as [bastion]:2222.
        with self.assertRaisesRegex(TunnelError, r'not known yet\. Run `ssh -p 2222 bastion` once in Terminal'):
            self.manager.acquire(self.spec())
        self.assertEqual(self.manager.snapshot(), [])
        # The next use tries again (nothing cached from the failure).
        self.fake.configure(mode='forward')
        with self.manager.acquire(self.spec()):
            pass
        self.assertEqual(len(self.fake.starts()), 2)

    def test_a_forward_that_keeps_failing_is_tried_twice_only(self):
        self.fake.configure(mode='fail', stderr='bind [127.0.0.1]:4000: Address already in use\n'
                                                'Could not request local forwarding.\n')
        with self.assertRaisesRegex(TunnelError, 'Could not open the local end'):
            self.manager.acquire(self.spec())
        self.assertEqual(len(self.fake.starts()), 2)

    def test_readiness_wait_times_out_and_stops_ssh(self):
        self.manager.ready_timeout = 0.6
        self.fake.configure(mode='hang')
        started = time.monotonic()
        with self.assertRaisesRegex(TunnelError, 'did not open within 0 seconds|did not open within'):
            self.manager.acquire(self.spec())
        self.assertLess(time.monotonic() - started, 6)
        pid = self.fake.starts()[0]['pid']
        self.assertFalse(_alive(pid))
        self.assertEqual(self.manager.snapshot(), [])

    def test_missing_ssh_says_so(self):
        manager = TunnelManager(binary=None)
        with mock.patch.object(ssh_tunnel, 'find_ssh', side_effect=TunnelError('ssh was not found. Install it.')):
            with self.assertRaisesRegex(TunnelError, '^ssh was not found'):
                manager.acquire(self.spec())
        self.assertEqual(manager.snapshot(), [])


class TestToolGroups(ManagerCase):

    def test_tunnels_are_listed_as_service_groups_and_unlisted_when_closed(self):
        directory = self.temp / 'runtime'
        directory.mkdir(mode=0o700)
        path = str(directory / 'tool-groups-test.json')
        groups = lifecycle.ToolGroups(path)
        self.manager.attach_groups(groups)
        lease = self.manager.acquire(self.spec())
        pid, group = self.fake.starts()[0]['pid'], self.fake.starts()[0]['pgid']
        with open(path) as handle:
            self.assertEqual(json.load(handle), {'schema_version': 'tool-groups-1.0', 'groups': [group]})
        # A shutdown's tool terminate() leaves tunnels to their owner (the
        # jobs' KILL QUERY may still go through them) ...
        self.assertEqual(groups.terminate(term_wait=0.1, kill_wait=0.1), [])
        self.assertTrue(_alive(pid))
        lease.release()
        # ... which then closes them and unlists them.
        self.manager.close_all()
        self.assertFalse(_alive(pid))
        with open(path) as handle:
            self.assertEqual(json.load(handle)['groups'], [])
        # A closing workspace starts no new tunnel.
        groups.close_spawning()
        with self.assertRaisesRegex(TunnelError, 'workspace is closing'):
            self.manager.acquire(self.spec())

    def test_at_most_max_groups_are_listed_and_the_desktop_reads_as_many(self):
        groups = lifecycle.ToolGroups()
        limit = lifecycle.ToolGroups.MAX_GROUPS
        for pgid in range(900000, 900000 + limit):
            groups.add(pgid)
        spawn = mock.Mock()
        with self.assertRaisesRegex(lifecycle.LifecycleError, r'%d dump, restore, clone or SSH tunnel processes' % limit):
            groups.start(spawn)
        spawn.assert_not_called()
        self.manager.attach_groups(groups)
        with self.assertRaisesRegex(TunnelError, r'%d dump, restore, clone or SSH tunnel processes' % limit):
            self.manager.acquire(self.spec())
        self.assertEqual(self.fake.starts(), [])
        # One number for both sides: apps/desktop/backend.cjs accepts a list
        # of exactly as many groups.
        source = (Path(__file__).resolve().parents[1] / 'apps' / 'desktop' / 'backend.cjs').read_text()
        self.assertIn('const MAX_TOOL_GROUPS = %d;' % limit, source)
        self.assertIn('value.groups.length > MAX_TOOL_GROUPS', source)

    def test_terminate_with_services_stops_tunnels_too(self):
        groups = lifecycle.ToolGroups()
        self.manager.attach_groups(groups)
        self.manager.acquire(self.spec())
        self.assertEqual(groups.terminate(term_wait=1, kill_wait=1, services=True), [])
        self.assertFalse(self.manager.snapshot()[0][3])  # The ssh process has exited.


class TestWorkspaceShutdown(ManagerCase):

    def test_closing_the_workspace_closes_its_tunnels_and_their_listing(self):
        from myflames.ui import WorkspaceServer
        directory = self.temp / 'runtime'
        directory.mkdir(mode=0o700)
        path = str(directory / 'tool-groups-ui.json')
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager):
            server = WorkspaceServer(store_path=str(self.temp / 'workspace.sqlite3'), tool_groups_file=path)
            try:
                self.assertIs(self.manager.groups, server.lifecycle.groups)
                lease = self.manager.acquire(self.spec())
                pid, group = self.fake.starts()[0]['pid'], self.fake.starts()[0]['pgid']
                with open(path) as handle:
                    self.assertEqual(json.load(handle)['groups'], [group])
            finally:
                server.server_close()
        self.assertFalse(_alive(pid))
        self.assertEqual(self.manager.snapshot(), [])
        self.assertIsNone(self.manager.groups)
        with open(path) as handle:
            self.assertEqual(json.load(handle), {'schema_version': 'tool-groups-1.0', 'groups': []})
        lease.release()

    def test_tunnels_close_on_time_while_slow_cleanup_runs(self):
        # A supervisor kills the backend a few seconds after asking it to
        # stop; tunnels live in their own groups, so they must not wait for
        # an editor or lifecycle cleanup that can take longer.
        from myflames.ui import WorkspaceServer
        seen = {}
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager), \
                mock.patch.object(WorkspaceServer, 'TUNNEL_CLOSE_SECONDS', 0.3, create=True):
            server = WorkspaceServer(store_path=str(self.temp / 'workspace.sqlite3'))
            lease = self.manager.acquire(self.spec())
            pid = self.fake.starts()[0]['pid']
            original = server.editor.close

            def slow_close():
                deadline = time.monotonic() + 4
                while _alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                seen['closed_during_cleanup'] = not _alive(pid)
                original()
            server.editor.close = slow_close
            server.server_close()
        self.assertTrue(seen.get('closed_during_cleanup'))
        self.assertEqual(self.manager.snapshot(), [])
        lease.release()

    def test_ssh_hosts_endpoint_reads_the_real_home(self):
        from myflames.ui import api_response
        home = self.temp / 'real-home'
        (home / '.ssh').mkdir(parents=True)
        (home / '.ssh' / 'config').write_text('Host bastion *.x\n', encoding='utf-8')
        with mock.patch.dict(os.environ, {'MYFLAMES_SSH_HOME': str(home)}):
            result = api_response('/api/ssh/hosts', {}, None)
        self.assertEqual(result['hosts'], ['bastion'])


class TestConnector(ManagerCase):
    """Database clients connect to the tunnel's local end, over TCP."""

    def client(self):
        path = self.temp / 'mysql'
        script = '''import json, os, re, socket, sys
path = sys.argv[1].split('=', 1)[1]
text = open(path).read()
fields = dict(re.findall(r'^([a-z-]+)=(.*)$', text, re.M))
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'client.json'), 'w') as out:
    json.dump(fields, out)
host = fields['host'].strip('"')
try:
    sock = socket.create_connection((host, int(fields['port'])), timeout=5)
except OSError as exc:
    sys.stderr.write("ERROR 2003 (HY000): Can't connect to MySQL server on '%s' (%s)\\n" % (host, exc))
    sys.exit(1)
sock.sendall(b'ping\\n')
try:
    data = sock.recv(1024)
except OSError:
    data = b''
if not data:
    sys.stderr.write("ERROR 2013 (HY000): Lost connection to MySQL server at 'reading initial communication packet'\\n")
    sys.exit(1)
sys.stdout.write(data.decode())
'''
        path.write_text('#!%s\n%s' % (sys.executable, script), encoding='utf-8')
        path.chmod(0o755)
        return str(path)

    def connection(self, **extra):
        options = connection_options(dict({'host': '127.0.0.1', 'port': self.db.port, 'user': 'app',
                                           'password': 'db-secret', 'ssh_host': 'bastion'}, **extra))
        options['binary'] = self.client()
        return MySQLConnection(**options)

    def recorded(self):
        return json.loads((self.temp / 'client.json').read_text())

    def test_a_read_goes_through_the_tunnel_and_releases_it(self):
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager), self.connection() as conn:
            self.assertEqual(conn.run('SELECT 1'), 'pong:ping\n')
        fields = self.recorded()
        port = self.manager.snapshot()[0][2]
        self.assertEqual((fields['protocol'], fields['host'], fields['port']), ('TCP', '"127.0.0.1"', str(port)))
        self.assertNotEqual(fields['port'], str(self.db.port))
        self.assertEqual([users for _, users, _, _ in self.manager.snapshot()], [0])
        # The database password stays in the client's option file, never with ssh.
        start, = self.fake.starts()
        self.assertFalse(any('db-secret' in part for part in start['argv']))

    def test_no_tunnel_means_no_direct_connection(self):
        conn = self.connection()
        with self.assertRaisesRegex(ConnectorError, 'no tunnel was opened; nothing was sent'):
            conn._build_defaults_file_content()
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager):
            self.fake.configure(mode='fail', stderr='Permission denied (publickey).\n')
            with self.assertRaisesRegex(ConnectorError, 'refused the sign-in'):
                conn.run('SELECT 1')
        self.assertFalse((self.temp / 'client.json').exists())

    def test_a_forward_that_cannot_reach_the_database_is_explained(self):
        self.fake.configure(refuse_channel=True)
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager), self.connection() as conn:
            with self.assertRaises(ConnectorError) as caught:
                conn.run('SELECT 1')
        self.assertIn('Lost connection', str(caught.exception))
        self.assertIn('SSH host bastion could not reach the database at 127.0.0.1:%d' % self.db.port,
                      str(caught.exception))

    def test_a_held_tunnel_outlives_invocations_until_close(self):
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager):
            conn = self.connection()
            conn.hold_transport()
            self.assertEqual([users for _, users, _, _ in self.manager.snapshot()], [1])
            conn.run('SELECT 1')
            self.assertEqual([users for _, users, _, _ in self.manager.snapshot()], [1])
            conn.close()
            self.assertEqual([users for _, users, _, _ in self.manager.snapshot()], [0])

    def test_a_legacy_option_file_never_names_a_dead_tunnel(self):
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager):
            conn = self.connection()
            try:
                first = conn._ensure_defaults_file()
                port = self.manager.snapshot()[0][2]
                with open(first) as handle:
                    self.assertIn('port=%d' % port, handle.read())
                self.manager.close_all()  # ssh ended: that port may now be anyone's
                again = conn._ensure_defaults_file()
                with open(again) as handle:
                    text = handle.read()
                self.assertIn('port=%d\n' % self.manager.snapshot()[0][2], text)
                self.assertEqual(len(self.fake.starts()), 2)
            finally:
                conn.close()

    def test_lifecycle_tools_get_the_local_end_and_no_ssh_options(self):
        options = lifecycle._options({'host': '127.0.0.1', 'port': self.db.port, 'user': 'app', 'password': 'pw',
                                      'ssh_host': 'bastion', 'ssh_user': 'deploy'})
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager):
            with lifecycle._credentials(options, {'path': '/usr/bin/mysqldump', 'name': 'mysqldump'}) as (argv, env):
                with open(argv[1].split('=', 1)[1]) as handle:
                    text = handle.read()
                users = [u for _, u, _, _ in self.manager.snapshot()]
            after = [u for _, u, _, _ in self.manager.snapshot()]
        port = self.manager.snapshot()[0][2]
        self.assertIn('host="127.0.0.1"', text)
        self.assertIn('port="%d"' % port, text)
        self.assertIn('protocol=TCP', text)
        self.assertNotIn('ssh', text)
        self.assertEqual((users, after), ([1], [0]))


# ---- readiness: only ssh's own words, never a TCP accept -------------------------------

class Impostor(object):
    """Another local program that took a port first and accepts on it."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            try:
                client.sendall(b'IMPOSTOR\n')
            except OSError:
                pass
            client.close()

    def close(self):
        self.sock.close()


class TestReadiness(ManagerCase):

    def accepted(self):
        path = self.temp / 'accepted'
        return len(path.read_text().splitlines()) if path.exists() else 0

    def test_a_port_another_program_took_first_is_never_trusted(self):
        # ssh binds only after it signed in; a program that read the port
        # from ps and listens first must not get the lease (nor the client's
        # handshake). ssh's bind then fails and, with ExitOnForwardFailure,
        # ends it: both attempts fail and nothing connects to the impostor.
        impostor = Impostor()
        self.addCleanup(impostor.close)
        self.fake.configure(mode='forward', auth_delay=0.6)
        with mock.patch.object(ssh_tunnel, 'free_port', return_value=impostor.port):
            with self.assertRaisesRegex(TunnelError, 'Could not open the local end of the SSH tunnel through bastion'):
                self.manager.acquire(self.spec())
        self.assertEqual(impostor.accepted, 0)
        self.assertEqual(len(self.fake.starts()), 2)
        self.assertEqual(self.manager.snapshot(), [])

    def test_readiness_opens_no_connection(self):
        with self.manager.acquire(self.spec()):
            time.sleep(0.3)
            self.assertEqual(self.accepted(), 0)
            self.assertEqual(self.db.connections, 0)

    def test_a_listener_ssh_never_announced_is_not_trusted(self):
        # Something accepts on the port, but ssh never said it listens there.
        self.manager.ready_timeout = 0.8
        self.fake.configure(mode='silent')
        with self.assertRaisesRegex(TunnelError, 'did not open within'):
            self.manager.acquire(self.spec())
        self.assertEqual(self.accepted(), 0)
        self.assertFalse(_alive(self.fake.starts()[0]['pid']))

    def test_ready_only_once_ssh_entered_its_session(self):
        # The listening line comes before the bind; the session line comes
        # after every forward was set up (or ssh exited on a failed one).
        self.manager.ready_timeout = 0.8
        self.fake.configure(mode='no-session')
        with self.assertRaisesRegex(TunnelError, 'did not open within'):
            self.manager.acquire(self.spec())

    def test_a_session_without_this_forward_fails_at_once(self):
        self.fake.configure(mode='no-forward')
        started = time.monotonic()
        with self.assertRaisesRegex(TunnelError, 'did not open the local end of the tunnel.*ClearAllForwardings'):
            self.manager.acquire(self.spec())
        self.assertLess(time.monotonic() - started, self.ready_timeout)
        self.assertFalse(_alive(self.fake.starts()[0]['pid']))

    def test_debug_output_is_not_kept(self):
        with self.manager.acquire(self.spec()) as lease:
            self.assertEqual(self.roundtrip(lease.port), b'pong:ping\n')
            tunnel = self.manager.tunnels[self.spec()]
            deadline = time.monotonic() + 2
            while 'Authenticated' not in tunnel.stderr_text() and time.monotonic() < deadline:
                time.sleep(0.02)
            kept = tunnel.stderr_text()
        self.assertIn('Authenticated to bastion', kept)
        self.assertNotIn('debug1', kept)


# ---- the tunnel never outlives the process that opened it ------------------------------

OWNER = """import atexit, json, os, sys, time
sys.path.insert(0, {root!r})
mark = {mark!r}
def note():
    from myflames import ssh_tunnel
    with open(mark, 'w') as handle:
        json.dump({{'open': len(ssh_tunnel.TUNNELS.snapshot())}}, handle)
atexit.register(note)  # before ssh_tunnel registers close_all, so it runs after it
from myflames import ssh_tunnel
if {cli!r}:
    from myflames import cli
    cli._close_on_termination_signals()  # what cli.main() does first
ssh_tunnel.TUNNELS.binary = {binary!r}
lease = ssh_tunnel.TUNNELS.acquire(ssh_tunnel.Spec('bastion', '', 0, '127.0.0.1', {port}))
print('ready', flush=True)
time.sleep(60)
"""


@unittest.skipUnless(POSIX, 'process groups and signals are POSIX')
class TestOwnerExit(ManagerCase):

    def owner(self, cli=False):
        mark = str(self.temp / 'owner-exit.json')
        script = OWNER.format(root=str(Path(__file__).resolve().parents[1]), mark=mark, cli=cli,
                              binary=str(self.fake.path), port=self.db.port)
        env = dict(os.environ, HOME=str(self.temp), USERPROFILE=str(self.temp), MYFLAMES_SECRET_BACKEND='vault')
        owner = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE, env=env)
        self.addCleanup(lambda: owner.poll() is None and owner.kill())
        self.addCleanup(owner.stdout.close)
        self.assertEqual(owner.stdout.readline().strip(), b'ready')
        start, = self.fake.starts()
        self.addCleanup(lambda: _alive(start['pid']) and os.kill(start['pid'], 9))
        return owner, start, mark

    def gone_within(self, pid, seconds):
        deadline = time.monotonic() + seconds
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not _alive(pid)

    def test_ssh_stops_when_its_owner_is_killed(self):
        # SIGKILL: no atexit, no cleanup of its own. The watchdog that leads
        # the tunnel's group sees its pipe from the owner close.
        owner, start, _ = self.owner()
        self.assertTrue(_alive(start['pid']))
        owner.kill()
        owner.wait(5)
        self.assertTrue(self.gone_within(start['pid'], 6), 'ssh outlived the process that opened the tunnel')
        self.assertFalse(_group_alive(start['pgid']))

    def test_cli_closes_its_tunnels_on_sigterm_and_sighup(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig):
                if (self.temp / 'starts.jsonl').exists():
                    (self.temp / 'starts.jsonl').unlink()
                owner, start, mark = self.owner(cli=True)
                owner.send_signal(sig)
                self.assertEqual(owner.wait(10), 128 + sig)
                with open(mark) as handle:
                    self.assertEqual(json.load(handle), {'open': 0})  # atexit ran close_all
                self.assertTrue(self.gone_within(start['pid'], 3))

    def test_cli_main_installs_the_handlers_first(self):
        from myflames import cli
        with mock.patch.object(cli, '_close_on_termination_signals') as install, \
                mock.patch.object(cli, '_cmd_guide') as guide, mock.patch.object(sys, 'argv', ['myflames', 'guide']):
            cli.main()
        install.assert_called_once_with()
        guide.assert_called_once_with([])


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@unittest.skipUnless(POSIX, 'process groups are POSIX')
class TestDesktopEntry(ManagerCase):
    """apps/desktop/scripts/backend-entry.py: after the desktop died and the
    backend's own shutdown hung, it kills the tool and tunnel groups this
    backend owns before it kills its own group."""

    def entry(self):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / 'apps' / 'desktop' / 'scripts' / 'backend-entry.py'
        spec = importlib.util.spec_from_file_location('myflames_backend_entry_test', str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_packaged_backend_runs_the_watchdog(self):
        # PyInstaller's backend is sys.executable with sys.frozen set; it runs
        # the watchdog when its first argument is ssh_watchdog.ARG. Here a
        # shell script stands in for it, running backend-entry.py.
        root = Path(__file__).resolve().parents[1]
        backend = self.temp / 'myflames-backend'
        backend.write_text('#!/bin/sh\nPYTHONPATH=%s exec %s %s "$@"\n' % (
            root, sys.executable, root / 'apps' / 'desktop' / 'scripts' / 'backend-entry.py'))
        backend.chmod(0o755)
        with mock.patch.object(sys, 'frozen', True, create=True), \
                mock.patch.object(sys, 'executable', str(backend)):
            self.assertEqual(ssh_tunnel.watchdog_argv(), [str(backend), '__myflames_ssh_watchdog'])
            with self.manager.acquire(self.spec()) as lease:
                self.assertEqual(self.roundtrip(lease.port), b'pong:ping\n')
                leader = self.manager.tunnels[self.spec()].process.pid
        start, = self.fake.starts()
        self.assertEqual((start['pgid'], start['ppid']), (leader, leader))
        self.manager.close_all()
        self.assertFalse(_alive(start['pid']))

    def test_owned_groups_are_killed_before_the_backend_group(self):
        groups = lifecycle.ToolGroups()
        tool = subprocess.Popen(['sleep', '30'], start_new_session=True)
        self.addCleanup(lambda: tool.poll() is None and tool.kill())
        groups.add(tool.pid)
        self.manager.attach_groups(groups)
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager):
            self.manager.acquire(self.spec())
            start, = self.fake.starts()
            killed = self.entry()._kill_owned_groups(own_group=os.getpgid(0))
        self.assertEqual(sorted(killed), sorted({tool.pid, start['pgid']}))
        tool.wait(5)
        deadline = time.monotonic() + 5
        while _alive(start['pid']) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(_alive(start['pid']))
        # Never its own group, whatever a list says.
        groups.add(os.getpgid(0))
        with mock.patch.object(ssh_tunnel, 'TUNNELS', self.manager), mock.patch.object(os, 'killpg') as killpg:
            self.entry()._kill_owned_groups(own_group=os.getpgid(0))
        self.assertNotIn(os.getpgid(0), [call[0][0] for call in killpg.call_args_list])


# ---- Windows: ssh.exe ends with the process that opened it (win_job) --------------------

class FakeKernel(object):
    """Stands in for win_job.Kernel32: records each call, raises where told."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = dict(fail or {})
        self.jobs = 0

    def _call(self, name, *args):
        self.calls.append((name,) + args)
        error = self.fail.get(name)
        if error is not None:
            raise error

    def create_job(self):
        self._call('create_job')
        self.jobs += 1
        return 1000 + self.jobs

    def kill_on_close(self, job):
        self._call('kill_on_close', job)

    def not_inheritable(self, handle):
        self._call('not_inheritable', handle)

    def assign(self, job, process):
        self._call('assign', job, process)

    def resume(self, pid):
        self._call('resume', pid)

    def close(self, handle):
        self._call('close', handle)


class SuspendedProcess(object):
    """What subprocess.Popen returns on Windows for an ssh.exe started suspended."""

    def __init__(self, pid=4321, handle=777, log=None):
        self.pid, self._handle = pid, handle
        self.stdin = None
        self.stderr = io.BytesIO(b'')
        self.returncode = None
        self.log = log if log is not None else []

    def kill(self):
        self.log.append(('kill', self.pid))
        self.returncode = 1

    terminate = kill

    def wait(self, timeout=None):
        self.log.append(('wait', self.pid))
        return self.returncode

    def poll(self):
        return self.returncode


ACCESS_DENIED = OSError(13, 'Access is denied.')  # FormatMessage text ends with a period.


class TestWindowsJob(unittest.TestCase):
    """On Windows each ssh.exe starts suspended, joins one kill-on-close job
    object per process and only then runs (POSIX has ssh_watchdog.py)."""

    def setUp(self):
        from myflames import win_job
        self.win_job = win_job
        self.api = FakeKernel()
        self.log = self.api.calls

    def windows(self, job=None):
        """Patches for a Windows _spawn: os.name, the creation flags, Popen and the job."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(ssh_tunnel.os, 'name', 'nt'))
        stack.enter_context(mock.patch.object(ssh_tunnel.subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200, create=True))
        stack.enter_context(mock.patch.object(ssh_tunnel.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True))
        self.started = []

        def popen(argv, **kwargs):
            process = SuspendedProcess(pid=4321 + len(self.started), handle=777 + len(self.started), log=self.log)
            self.started.append((argv, kwargs, process))
            self.log.append(('popen', process.pid))
            return process
        stack.enter_context(mock.patch.object(ssh_tunnel.subprocess, 'Popen', side_effect=popen))
        stack.enter_context(mock.patch.object(self.win_job, 'JOB', job or self.win_job.KillOnCloseJob(self.api)))
        return stack

    def test_one_job_for_the_process_and_each_ssh_assigned_by_its_handle_then_resumed(self):
        job = self.win_job.KillOnCloseJob(self.api)
        job.adopt(SuspendedProcess(pid=11, handle=501))
        job.adopt(SuspendedProcess(pid=12, handle=502))
        self.assertEqual(self.api.calls, [
            ('create_job',), ('kill_on_close', 1001), ('not_inheritable', 1001),
            ('assign', 1001, 501), ('resume', 11),
            ('assign', 1001, 502), ('resume', 12)])
        self.assertEqual(self.api.jobs, 1)

    def test_a_job_that_could_not_be_set_up_is_closed_and_tried_again_next_time(self):
        self.api.fail['kill_on_close'] = ACCESS_DENIED
        job = self.win_job.KillOnCloseJob(self.api)
        with self.assertRaises(OSError):
            job.adopt(SuspendedProcess())
        self.assertIn(('close', 1001), self.api.calls)
        self.assertFalse(any(call[0] in ('assign', 'resume') for call in self.api.calls))
        del self.api.fail['kill_on_close']
        job.adopt(SuspendedProcess())
        self.assertEqual(self.api.calls[-2:], [('assign', 1002, 777), ('resume', 4321)])

    def test_windows_spawn_starts_ssh_suspended_and_lets_it_run_only_in_the_job(self):
        manager = TunnelManager(binary='C:\\Windows\\System32\\OpenSSH\\ssh.exe')
        with self.windows():
            process, recorded = manager._spawn(['ssh.exe', '-N', '--', 'bastion'])
            second, _ = manager._spawn(['ssh.exe', '-N', '--', 'other'])
        self.assertFalse(recorded)
        (argv, kwargs, started), _ = self.started
        self.assertIs(process, started)
        self.assertEqual(argv, ['ssh.exe', '-N', '--', 'bastion'])  # No watchdog on Windows.
        self.assertEqual(kwargs['creationflags'], 0x200 | 0x08000000 | self.win_job.CREATE_SUSPENDED)
        self.assertNotIn('start_new_session', kwargs)
        self.assertEqual(self.log, [
            ('popen', 4321), ('create_job',), ('kill_on_close', 1001), ('not_inheritable', 1001),
            ('assign', 1001, 777), ('resume', 4321),
            ('popen', 4322), ('assign', 1001, 778), ('resume', 4322)])
        self.assertIsNone(process.returncode)

    def test_a_refused_assignment_stops_ssh_and_refuses_the_tunnel(self):
        self.api.fail['assign'] = ACCESS_DENIED
        manager = TunnelManager(binary='C:\\Windows\\System32\\OpenSSH\\ssh.exe')
        spec = Spec('bastion', '', 0, '127.0.0.1', 3306)
        with self.windows():
            with self.assertRaises(TunnelError) as caught:
                manager.acquire(spec)
        self.assertEqual(str(caught.exception),
                         'Windows could not tie the SSH tunnel to myflames (job object: Access is denied), so ssh '
                         'was stopped before it connected. Nothing was sent.')
        (_, _, process), = self.started
        self.assertEqual(self.log[-3:], [('assign', 1001, 777), ('kill', 4321), ('wait', 4321)])
        self.assertNotIn(('resume', 4321), self.log)  # It never ran.
        self.assertTrue(process.stderr.closed)
        # Nothing is left to hand out, reuse or close later.
        self.assertEqual(manager.tunnels, {})
        self.assertEqual(manager.snapshot(), [])

    def test_a_failed_resume_stops_the_suspended_ssh(self):
        self.api.fail['resume'] = OSError('no thread of process 4321 was found to resume')
        manager = TunnelManager(binary='ssh.exe')
        with self.windows(), self.assertRaisesRegex(TunnelError, 'job object: no thread of process 4321'):
            manager._spawn(['ssh.exe'])
        self.assertEqual(self.log[-3:], [('resume', 4321), ('kill', 4321), ('wait', 4321)])

    def test_an_interrupted_assignment_stops_ssh_and_lets_the_interrupt_through(self):
        # Ctrl+C while ssh waits suspended: nothing else knows the process
        # yet, so it is stopped here, and the interrupt is not turned into a
        # refused tunnel.
        self.api.fail['assign'] = KeyboardInterrupt()
        manager = TunnelManager(binary='ssh.exe')
        with self.windows(), self.assertRaises(KeyboardInterrupt):
            manager._spawn(['ssh.exe'])
        self.assertEqual(self.log[-2:], [('kill', 4321), ('wait', 4321)])
        self.assertNotIn(('resume', 4321), self.log)

    def test_a_job_that_cannot_be_created_refuses_the_tunnel(self):
        self.api.fail['create_job'] = OSError(1450, 'Insufficient system resources exist')
        manager = TunnelManager(binary='ssh.exe')
        with self.windows(), self.assertRaisesRegex(TunnelError, r'^Windows could not tie the SSH tunnel'):
            manager._spawn(['ssh.exe'])
        self.assertEqual(self.log[-2:], [('kill', 4321), ('wait', 4321)])

    @unittest.skipUnless(POSIX, 'the POSIX path')
    def test_posix_spawn_is_unchanged(self):
        manager = TunnelManager(binary='/usr/bin/ssh')
        seen = []

        def popen(argv, **kwargs):
            seen.append((argv, kwargs))
            return SuspendedProcess()
        with mock.patch.object(ssh_tunnel.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(self.win_job.KillOnCloseJob, 'adopt', side_effect=AssertionError('no job on POSIX')):
            manager._spawn(['/usr/bin/ssh', '-N'])
        (argv, kwargs), = seen
        self.assertEqual(argv, ssh_tunnel.watchdog_argv() + ['--', '/usr/bin/ssh', '-N'])
        self.assertEqual(sorted(kwargs), ['env', 'start_new_session', 'stderr', 'stdin', 'stdout'])
        self.assertIs(kwargs['start_new_session'], True)
        self.assertEqual(self.api.calls, [])

    def test_structure_layouts_match_the_windows_headers(self):
        import ctypes
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest('64-bit layout')
        self.assertEqual(ctypes.sizeof(self.win_job.JOBOBJECT_BASIC_LIMIT_INFORMATION), 64)
        self.assertEqual(ctypes.sizeof(self.win_job.IO_COUNTERS), 48)
        self.assertEqual(ctypes.sizeof(self.win_job.JOBOBJECT_EXTENDED_LIMIT_INFORMATION), 144)
        self.assertEqual(self.win_job.JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags.offset, 16)
        self.assertEqual(ctypes.sizeof(self.win_job.THREADENTRY32), 28)

    def test_sshd_session_state_never_reaches_ssh(self):
        # Win32-OpenSSH's sshd hands its children this variable; an ssh.exe
        # that inherits it hangs before it signs in (seen on Windows 11 with
        # OpenSSH_for_Windows_9.5p2). The environment allowlist keeps it out.
        name = 'C28FC6F98A2C44ABBBD89D6A3037D0D9_POSIX_FD_STATE'
        with mock.patch.dict(os.environ, {'PATH': 'C:\\Windows\\System32', name: '{"fds":[]}'}, clear=True):
            self.assertNotIn(name, ssh_tunnel.ssh_environment())


WINDOWS_OWNER = """import os, subprocess, sys, time
sys.path.insert(0, {root!r})
from myflames import win_job
child = subprocess.Popen([sys.executable, '-c', 'import sys, time; open(sys.argv[1], "w").close(); time.sleep(60)',
                          {mark!r}], creationflags=win_job.CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW)
time.sleep(0.5)
ran_before = os.path.exists({mark!r})
win_job.JOB.adopt(child)
import ctypes
flags = ctypes.c_uint32()
ctypes.WinDLL('kernel32').GetHandleInformation(ctypes.c_void_p(win_job.JOB.handle()), ctypes.byref(flags))
print(child.pid, int(ran_before), flags.value & 1, flush=True)
time.sleep(60)
"""


@unittest.skipUnless(os.name == 'nt', 'job objects are Windows')
class TestWindowsJobLive(unittest.TestCase):
    """The real kernel32 calls: a child started suspended runs only after it
    joined the job, and ends when its owner is killed (TerminateProcess)."""

    def test_child_ends_when_its_owner_is_killed(self):
        import ctypes
        with tempfile.TemporaryDirectory() as directory:
            mark = os.path.join(directory, 'ran')
            script = WINDOWS_OWNER.format(root=str(Path(__file__).resolve().parents[1]), mark=mark)
            owner = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE)
            self.addCleanup(lambda: owner.poll() is None and owner.kill())
            self.addCleanup(owner.stdout.close)
            pid, ran_before, inheritable = (int(x) for x in owner.stdout.readline().split())
            self.assertEqual((ran_before, inheritable), (0, 0))  # Suspended until adopted; handle not inherited.
            deadline = time.monotonic() + 10
            while not os.path.exists(mark) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(os.path.exists(mark), 'the child never ran after it was resumed')
            kernel = ctypes.WinDLL('kernel32')
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE
            self.assertTrue(handle)
            try:
                owner.kill()  # TerminateProcess: no cleanup of its own.
                owner.wait(5)
                self.assertEqual(kernel.WaitForSingleObject(ctypes.c_void_p(handle), 5000), 0,
                                 'the child outlived the process that started it')
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))


# ---- identity, saved passwords and the audit trail -----------------------------------

class TestIdentity(unittest.TestCase):

    def test_identities_without_ssh_are_unchanged(self):
        self.assertEqual(dbops.connection_identity({'profile_id': 'p1', 'host': ' db1 ', 'port': 3306, 'user': 'app',
                                                    'ssl_mode': 'REQUIRED', 'ssl_ca': ''}),
                         '["p1","db1",3306,"app","REQUIRED",""]')
        self.assertEqual(dbops.connection_identity({'host': 'db1', 'port': '3307', 'type': 'development',
                                                    'read_only': True}),
                         '["","db1",3307,"","","","development",true]')
        # Empty SSH fields are no SSH.
        self.assertEqual(dbops.connection_identity({'host': 'db1', 'port': 3306, 'ssh_host': '', 'ssh_user': '',
                                                    'ssh_port': 0}),
                         '["","db1",3306,"","",""]')
        self.assertEqual(dbops.identity_endpoint('["p1","db1",3306,"app","","","development",false]'),
                         '["p1","db1",3306,"app","",""]')

    def test_the_ssh_target_is_part_of_the_identity(self):
        through = {'profile_id': 'p1', 'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'ssh_host': ' bastion ',
                   'ssh_user': 'deploy', 'ssh_port': 2222}
        key = dbops.connection_identity(through)
        self.assertEqual(key, '["p1","127.0.0.1",3306,"app","","",["ssh","bastion","deploy",2222]]')
        self.assertEqual(dbops.connection_identity(dict(through, type='development')),
                         '["p1","127.0.0.1",3306,"app","","",["ssh","bastion","deploy",2222],"development",false]')
        self.assertEqual(dbops.identity_endpoint(dbops.connection_identity(dict(through, read_only=True))), key)
        other = dbops.connection_identity(dict(through, ssh_host='bastion2'))
        direct = dbops.connection_identity({k: v for k, v in through.items() if not k.startswith('ssh_')})
        self.assertEqual(len({key, other, direct}), 3)
        self.assertEqual(dbops.connection_identity({'host': 'db', 'port': 3306, 'ssh_host': 'b'}),
                         '["","db",3306,"","","",["ssh","b","",0]]')

    def test_the_browser_key_has_the_same_shape(self):
        source = (Path(__file__).resolve().parents[1] / 'apps' / 'workspace' / 'src' / 'binding.ts').read_text()
        self.assertIn('["ssh", host, trimPasted(c.ssh_user || ""), Number(c.ssh_port) || 0]', source)
        self.assertIn('if (ssh) key.push(ssh);', source)

    def test_endpoint_fingerprint_without_ssh_is_unchanged(self):
        from myflames.workspace_store import _endpoint_fingerprint, _same_endpoint
        value = {'host': 'db.internal', 'port': 3307, 'user': 'app', 'ssl_mode': '', 'ssl_ca': ''}
        before_ssh = hashlib.sha256('db.internal\x003307\x00app\x00\x00'.encode('utf-8')).hexdigest()[:24]
        self.assertEqual(_endpoint_fingerprint(value), before_ssh)
        through = dict(value, ssh_host='bastion')
        self.assertNotEqual(_endpoint_fingerprint(through), before_ssh)
        self.assertNotEqual(_endpoint_fingerprint(through), _endpoint_fingerprint(dict(through, ssh_port=2222)))
        self.assertFalse(_same_endpoint(value, through))
        self.assertFalse(_same_endpoint(through, dict(through, ssh_user='root')))
        self.assertTrue(_same_endpoint(through, dict(through, ssh_host=' bastion ', ssh_port=0)))
        self.assertTrue(_same_endpoint(dict(through, ssh_port='22'), dict(through, ssh_port=22)))


class StoreCase(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patcher = mock.patch.dict(os.environ, {'HOME': str(self.home), 'USERPROFILE': str(self.home),
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        from myflames.workspace_store import WorkspaceStore
        self.store = WorkspaceStore(self.home / '.myflames' / 'workspace.sqlite3')


class TestProfiles(StoreCase):

    def save(self, **fields):
        value = {'name': 'prod', 'host': '127.0.0.1', 'port': 3306, 'user': 'app', 'ssh_host': 'bastion',
                 'ssh_user': 'deploy'}
        value.update(fields)
        return self.store.save('profile', value, password='db-secret', remember=True)

    def test_saved_password_goes_only_through_its_own_tunnel(self):
        saved = self.save()
        self.assertEqual((saved['ssh_host'], saved['ssh_user']), ('bastion', 'deploy'))
        request = {'profile_id': saved['id'], 'host': '127.0.0.1', 'port': 3306, 'user': 'app',
                   'ssh_host': 'bastion', 'ssh_user': 'deploy'}
        self.assertEqual(self.store.connection_with_saved_password(request)['password'], 'db-secret')
        for change in ({'ssh_host': '', 'ssh_user': ''}, {'ssh_host': 'other'}, {'ssh_user': 'root'},
                       {'ssh_port': 22}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'own host, port, user, and TLS'):
                self.store.connection_with_saved_password(dict(request, **change))

    def test_connection_safety_requires_the_same_tunnel(self):
        saved = self.save()
        profile = dict(saved, type='development')
        request = {'profile_id': saved['id'], 'host': '127.0.0.1', 'port': 3306, 'user': 'app',
                   'ssh_host': 'bastion', 'ssh_user': 'deploy'}
        self.assertEqual(dbops.connection_safety(profile, request)['type'], 'development')
        direct = {k: v for k, v in request.items() if not k.startswith('ssh_')}
        self.assertEqual(dbops.connection_safety(profile, direct)['type'], 'production')

    def test_set_profile_ssh_forgets_the_password_of_the_old_endpoint(self):
        saved = self.save(ssh_host='', ssh_user='', type='development')
        self.assertNotIn('ssh_host', saved)
        changed, forgot = self.store.set_profile_ssh(saved['id'], {'ssh_host': 'bastion', 'ssh_port': 2222})
        self.assertTrue(forgot)
        self.assertEqual((changed['ssh_host'], changed['ssh_port'], changed['has_saved_password'], changed['type']),
                         ('bastion', 2222, False, 'production'))
        same, forgot = self.store.set_profile_ssh(saved['id'], {'ssh_host': 'bastion', 'ssh_port': 2222},
                                                  connection_type='development')
        self.assertFalse(forgot)
        self.assertEqual(same['type'], 'development')
        back, _ = self.store.set_profile_ssh(saved['id'], None)
        self.assertFalse(any(key.startswith('ssh_') for key in back))
        with self.assertRaisesRegex(ValueError, 'not starting with a hyphen'):
            self.store.set_profile_ssh(saved['id'], {'ssh_host': '-oProxyCommand=x'})

    def test_audit_records_name_the_ssh_target(self):
        trail = dbops.AuditTrail(self.store, 'kill', 'cli', connection={'host': '127.0.0.1', 'port': 3306,
                                                                       'user': 'app', 'ssh_host': 'bastion',
                                                                       'ssh_user': 'deploy', 'ssh_port': 2222})
        trail.outcome(['KILL 5'], 'declined')
        plain = dbops.AuditTrail(self.store, 'kill', 'cli', connection={'host': 'db', 'port': 3306, 'user': 'app'})
        plain.outcome(['KILL 6'], 'declined')
        records = {r['statements'][0]: r for r in self.store.audit_records()}
        self.assertEqual(records['KILL 5']['connection']['ssh'], 'deploy@bastion:2222')
        self.assertNotIn('ssh', records['KILL 6']['connection'])
        csv_text = dbops.audit_csv(self.store.audit_records())
        self.assertTrue(csv_text.splitlines()[0].endswith(',duration_ms,ssh'))
        self.assertIn('deploy@bastion:2222', csv_text)

    def test_an_audit_table_from_before_ssh_gets_the_column(self):
        import sqlite3
        from myflames.workspace_store import WorkspaceStore
        path = self.home / 'old.sqlite3'
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            conn.execute('CREATE TABLE audit (id TEXT PRIMARY KEY, attempted_at TEXT NOT NULL, finished_at TEXT, '
                         'status TEXT NOT NULL, outcome TEXT, surface TEXT NOT NULL, os_user TEXT, operation TEXT '
                         'NOT NULL, profile_id TEXT, profile_name TEXT, connection_type TEXT, read_only INTEGER, '
                         'host TEXT, port INTEGER, db_user TEXT, engine TEXT, server_version TEXT, statements TEXT '
                         'NOT NULL, target TEXT, verified TEXT, policy TEXT, error_code INTEGER, message TEXT, '
                         'rows_affected INTEGER, after TEXT, duration_ms REAL)')
        os.chmod(str(path), 0o600)
        store = WorkspaceStore(path)
        dbops.AuditTrail(store, 'kill', 'cli', connection={'host': 'db', 'ssh_host': 'b'}).outcome(['KILL 1'],
                                                                                                  'declined')
        self.assertEqual(store.audit_records()[0]['connection']['ssh'], 'b')
        with contextlib.closing(sqlite3.connect(str(path))) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)


class TestCli(StoreCase):

    def run_cli(self, *argv):
        from myflames.workspace_cli import cmd_profiles
        err = io.StringIO()
        code = 0
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                cmd_profiles(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def test_profiles_set_ssh_and_no_ssh(self):
        saved = self.store.save('profile', {'name': 'prod', 'host': '127.0.0.1', 'port': 3306, 'user': 'app'},
                                password='db-secret', remember=True)
        code, err = self.run_cli('set', 'prod', '--ssh', 'bastion', '--ssh-user', 'deploy', '--ssh-port', '2222')
        self.assertEqual(code, 0, err)
        self.assertIn("now connects to 127.0.0.1:3306 through SSH host deploy@bastion:2222", err)
        self.assertIn('saved password was forgotten', err)
        profile = self.store.get('profile', saved['id'])
        self.assertEqual((profile['ssh_host'], profile['ssh_user'], profile['ssh_port']), ('bastion', 'deploy', 2222))
        code, err = self.run_cli('set', 'prod', '--no-ssh', '--type', 'development')
        self.assertEqual(code, 0, err)
        self.assertIn('now connects directly to 127.0.0.1:3306, without SSH', err)
        self.assertIn('is now Development', err)
        profile = self.store.get('profile', saved['id'])
        self.assertFalse(any(key.startswith('ssh_') for key in profile))

    def test_profiles_set_refuses_bad_ssh_values(self):
        self.store.save('profile', {'name': 'prod', 'host': '127.0.0.1', 'port': 3306, 'user': 'app'})
        for argv, text in ((('--ssh', '-oProxyCommand=touch /tmp/x'), 'not starting with a hyphen'),
                           (('--ssh=-oProxyCommand=x',), 'not starting with a hyphen'),
                           (('--ssh-user', 'deploy'), 'need --ssh HOST'),
                           (('--ssh', 'b', '--ssh-port', '0'), 'Choose an integer'),
                           (('--ssh', 'b', '--no-ssh'), 'not allowed with'),
                           ((), 'choose --type, --read-only, --read-write, --ssh or --no-ssh')):
            with self.subTest(argv=argv):
                code, err = self.run_cli('set', 'prod', *argv)
                self.assertEqual(code, 2)
                self.assertIn(text, err)
        self.assertNotIn('ssh_host', self.store.find_profile('prod'))

    def test_profiles_list_shows_the_ssh_target(self):
        from myflames.workspace_cli import cmd_profiles
        self.store.save('profile', {'name': 'prod', 'host': '127.0.0.1', 'port': 3306, 'ssh_host': 'bastion'})
        self.store.save('profile', {'name': 'local', 'host': '127.0.0.1', 'port': 3306})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cmd_profiles(['list'])
        self.assertIn('127.0.0.1:3306 via bastion', out.getvalue())
        self.assertEqual(out.getvalue().count('via'), 1)


# ---- features that assume the server is on this computer ------------------------------

class TestLocality(unittest.TestCase):

    def test_clone_through_ssh_is_refused_on_either_side(self):
        donor = {'host': '10.0.0.5', 'port': 3306, 'user': 'clone', 'password': 'x'}
        recipient = {'host': '127.0.0.1', 'port': 3306, 'user': 'root'}
        lifecycle.refuse_clone_through_ssh(donor, recipient)
        with self.assertRaisesRegex(lifecycle.Refused, 'donor is reached through SSH host bastion'):
            lifecycle.refuse_clone_through_ssh(dict(donor, ssh_host='bastion'), recipient)
        with self.assertRaisesRegex(lifecycle.Refused, 'recipient is reached through SSH host jump'):
            lifecycle.refuse_clone_through_ssh(donor, dict(recipient, ssh_host='jump'))
        with self.assertRaisesRegex(lifecycle.Refused, 'donor is reached through SSH'):
            lifecycle.prepare_clone(None, None, dict(donor, ssh_host='bastion'), recipient)
        with self.assertRaisesRegex(lifecycle.Refused, 'recipient is reached through SSH'):
            lifecycle.clone_job(donor, dict(recipient, ssh_host='jump'), {'request': {}}, start=False)

    def test_replication_never_treats_a_tunnel_as_this_computer(self):
        from myflames import replication
        self.assertEqual(replication.reached_host({'host': '127.0.0.1', 'ssh_host': 'bastion'}), 'ssh:bastion')
        self.assertEqual(replication.reached_host({'host': '127.0.0.1'}), '127.0.0.1')
        source = {'host': '127.0.0.1', 'port': 3307}
        profiles = [{'id': 'a', 'name': 'a', 'host': '127.0.0.1', 'port': 3307, 'ssh_host': 'bastion'},
                    {'id': 'b', 'name': 'b', 'host': '127.0.0.1', 'port': 3307}]
        found, problem = replication.match_source_profiles(source, profiles, 'ssh:bastion')
        self.assertEqual(found, [])
        self.assertIn('not on this computer', problem)
        # A replica on this computer: a profile through SSH is never its loopback source.
        found, _ = replication.match_source_profiles(source, profiles, '127.0.0.1')
        self.assertEqual([p['id'] for p in found], ['b'])

    def test_mariadb_error_log_through_ssh_is_remote(self):
        from myflames import error_log

        class Conn(object):
            host, ssh_host = '127.0.0.1', 'bastion'

        report = {'settings': {}, 'status': {}}
        server = mock.Mock()
        server.var.side_effect = lambda name: {'log_error': '/var/log/mysql/error.log', 'datadir': '/var/lib/mysql/',
                                               'hostname': 'db1', 'port': '3306', 'log_warnings': '2'}.get(name)
        server.version = '11.4.5-MariaDB'
        reader = mock.Mock()
        reader.rows.return_value = []
        with mock.patch.object(error_log, '_maria_status'), \
                mock.patch.object(error_log.os, 'stat', side_effect=AssertionError('no local file read')):
            result = error_log._mariadb_report(Conn(), server, reader, report)
        self.assertIn('through SSH host bastion', json.dumps(result))


# ---- ~/.ssh/config Host aliases --------------------------------------------------------

class TestConfigHosts(unittest.TestCase):

    def test_aliases_without_patterns_including_included_files(self):
        with tempfile.TemporaryDirectory() as home:
            ssh = Path(home) / '.ssh'
            (ssh / 'config.d').mkdir(parents=True)
            (ssh / 'config').write_text(
                '# comment\nHost bastion prod-db *.internal !blocked db?\n  HostName 10.0.0.1\n'
                'Host=jump "quoted-host" -oProxyCommand=x\nInclude config.d/*.conf ~/.ssh/extra\n'
                'Match host x\nhost lower # trailing comment\nHost *\n', encoding='utf-8')
            (ssh / 'config.d' / 'a.conf').write_text('Host from-include\nInclude ../config\n', encoding='utf-8')
            (ssh / 'extra').write_text('Host extra BASTION\n', encoding='utf-8')
            self.assertEqual(ssh_tunnel.config_hosts(home),
                             ['bastion', 'extra', 'from-include', 'jump', 'lower', 'prod-db', 'quoted-host'])
            with mock.patch.dict(os.environ, {'MYFLAMES_SSH_HOME': home}):
                response = ssh_tunnel.hosts_response()
            self.assertEqual(response['schema_version'], 'ssh-hosts-1.0')
            self.assertIn('bastion', response['hosts'])

    def test_no_config_is_an_empty_list(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(ssh_tunnel.config_hosts(home), [])


# ---- the real ssh, against a private sshd (optional) ----------------------------------

@unittest.skipUnless(os.environ.get('MYFLAMES_SSH_LIVE') == '1' and POSIX, 'set MYFLAMES_SSH_LIVE=1 to run')
class TestLiveSsh(unittest.TestCase):
    """The real OpenSSH client and a private sshd started for this test only,
    as this user, on a loopback port, with its own host key, authorized key
    and config in a temporary folder (no system SSH setting is read or
    changed; the client gets ``-F`` with a temporary config through a small
    wrapper, so ~/.ssh is never used). The forward goes to a local TCP server."""

    def test_a_real_tunnel(self):
        sshd = '/usr/sbin/sshd'
        if not os.path.exists(sshd):
            self.skipTest('no sshd')
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            for name in ('host_key', 'client_key'):
                subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(d / name)], check=True)
            (d / 'authorized_keys').write_text((d / 'client_key.pub').read_text())
            os.chmod(str(d / 'authorized_keys'), 0o600)
            port = ssh_tunnel.free_port()
            (d / 'sshd_config').write_text(
                'ListenAddress 127.0.0.1\nPort %d\nHostKey %s\nAuthorizedKeysFile %s\nPasswordAuthentication no\n'
                'KbdInteractiveAuthentication no\nUsePAM no\nStrictModes no\nPidFile %s\nAllowTcpForwarding yes\n'
                % (port, d / 'host_key', d / 'authorized_keys', d / 'sshd.pid'))
            server = subprocess.Popen([sshd, '-D', '-e', '-f', str(d / 'sshd_config')], stderr=subprocess.PIPE)
            self.addCleanup(server.wait)
            self.addCleanup(server.terminate)
            (d / 'known_hosts').write_text('[127.0.0.1]:%d %s' % (port, (d / 'host_key.pub').read_text()))
            (d / 'client_config').write_text(
                'Host live-test\n  HostName 127.0.0.1\n  Port %d\n  IdentityFile %s\n  IdentitiesOnly yes\n'
                '  UserKnownHostsFile %s\n' % (port, d / 'client_key', d / 'known_hosts'))
            wrapper = d / 'ssh'
            wrapper.write_text('#!/bin/sh\nexec /usr/bin/ssh -F %s "$@"\n' % (d / 'client_config'))
            wrapper.chmod(0o755)
            db = EchoServer()
            self.addCleanup(db.close)
            manager = TunnelManager(binary=str(wrapper), ready_timeout=15)
            self.addCleanup(manager.close_all)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
                    break
                except OSError:
                    time.sleep(0.1)
            with manager.acquire(Spec('live-test', '', 0, '127.0.0.1', db.port)) as lease:
                time.sleep(0.2)
                self.assertEqual(db.connections, 0)  # Ready without a probe connection.
                with socket.create_connection(('127.0.0.1', lease.port), timeout=5) as sock:
                    sock.sendall(b'ping\n')
                    self.assertEqual(sock.recv(64), b'pong:ping\n')
            # Another program listening on the port before ssh binds it: the
            # real ssh's bind fails, ExitOnForwardFailure ends it, both
            # attempts fail and nothing ever connects to that program.
            impostor = Impostor()
            self.addCleanup(impostor.close)
            with mock.patch.object(ssh_tunnel, 'free_port', return_value=impostor.port):
                with self.assertRaisesRegex(TunnelError, 'Could not open the local end of the SSH tunnel through '
                                                         'live-test'):
                    TunnelManager(binary=str(wrapper), ready_timeout=15).acquire(
                        Spec('live-test', '', 0, '127.0.0.1', db.port))
            self.assertEqual(impostor.accepted, 0)
            # An unknown host key is reported in plain words.
            (d / 'known_hosts').write_text('')
            with self.assertRaisesRegex(TunnelError, 'not known yet'):
                TunnelManager(binary=str(wrapper), ready_timeout=15).acquire(
                    Spec('live-test', '', 0, '127.0.0.1', db.port))


if __name__ == '__main__':
    unittest.main()
