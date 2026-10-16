"""myflames privileges and myflames accounts: exit codes, streams, the
--yes/TTY rule, the policy before connecting, and passwords kept out of argv
and output. Connections are fakes or recordings; nothing reaches a server."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import _accounts_replay as replay  # noqa: E402
from test_accounts import FakeConn, actor  # noqa: E402

from myflames import accounts_cli, cli, privileges as p  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402


class Stdin(io.StringIO):
    def __init__(self, text='', tty=False):
        super(Stdin, self).__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class AccountsCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.conn = FakeConn(exists=False, name='cli_u')
        self.connects = 0

        def connect(args):
            self.connects += 1
            return {'host': '127.0.0.1', 'port': 3306, 'user': 'admin', 'password': 'conn-secret'}, self.conn
        patcher = mock.patch.object(accounts_cli, '_connect', side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.real_read = p.read_privileges
        patcher = mock.patch.object(p, 'read_privileges', side_effect=lambda conn: actor(conn.engine))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv, stdin=''):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'stdout', out), mock.patch.object(sys, 'stderr', err), \
                mock.patch.object(sys, 'stdin', stdin if isinstance(stdin, Stdin) else Stdin(stdin)), \
                mock.patch.object(sys, 'argv', ['myflames'] + argv):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        return code, out.getvalue(), err.getvalue()

    def test_privileges_text_and_json(self):
        self.conn, _data = replay.session('mysql-9.7', 'acct_app')
        with mock.patch.object(p, 'read_privileges', self.real_read):
            code, out, err = self.run_cli(['privileges', '--json', '-q'])
            self.assertEqual(code, 0)
            report = json.loads(out)
            self.assertEqual(report['schema_version'], 'privileges-1.0')
            self.assertEqual(err, '')
            code, out, err = self.run_cli(['privileges'])
        self.assertEqual(code, 0)
        self.assertIn('Denied (the server would refuse)', out)
        self.assertIn('acct_app@% on MySQL 9.7.2', out)
        self.assertIn('new session', err)

    def test_connection_failure_exits_2(self):
        self.conn.run = mock.Mock(side_effect=ConnectorError("ERROR 2003: Can't connect"))
        with mock.patch.object(p, 'read_privileges', self.real_read):
            code, out, err = self.run_cli(['privileges'])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('2003', err)

    def test_create_with_password_stdin(self):
        sent = []

        def run_stdin(sql, extra_flags=None, timeout=None):
            sent.append(sql)
            self.conn.exists = True
            return ''
        self.conn.run_stdin = run_stdin
        code, out, err = self.run_cli(['accounts', 'create', 'cli_u@%', '--password-stdin', '--yes'],
                                      "Cli-pw\\1'x!\n")
        self.assertEqual(code, 0, err)
        self.assertIn('applied', out)
        self.assertIn("'Cli-pw\\1''x!'", sent[0])
        self.assertNotIn('Cli-pw', out + err)
        records = WorkspaceStore().audit_records()
        self.assertEqual(records[0]['surface'], 'cli')
        self.assertNotIn('Cli-pw', json.dumps(records))

    def test_non_terminal_without_yes_is_refused_and_audited(self):
        self.conn = FakeConn(name='cli_u')
        code, out, err = self.run_cli(['accounts', 'lock', 'cli_u@%'])
        self.assertEqual(code, 2)
        self.assertIn('Re-run with --yes', err)
        self.assertEqual(self.conn.stdin, [])
        self.assertEqual(WorkspaceStore().audit_records()[0]['status'], 'refused')
        self.assertEqual(self.connects, 0)

    def test_password_stdin_without_yes_refuses_before_reading_or_connecting(self):
        for tty in (False, True):
            with self.subTest(tty=tty):
                stream = Stdin('must-not-be-read\n', tty=tty)
                with mock.patch.object(stream, 'read', side_effect=AssertionError('stdin consumed')):
                    code, out, err = self.run_cli(['accounts', 'create', 'cli_u@%', '--password-stdin'], stream)
                self.assertEqual(code, 2)
                self.assertIn('--password-stdin requires --yes', err)
                self.assertEqual(self.connects, 0)
                record = WorkspaceStore().audit_records()[0]
                self.assertEqual((record['status'], record['outcome']), ('refused', 'needs_confirmation'))
                self.assertNotIn('must-not-be-read', json.dumps(record))

    def test_missing_password_input_is_audited_before_connecting(self):
        code, out, err = self.run_cli(['accounts', 'password', 'cli_u@%', '--yes'])
        self.assertEqual(code, 2)
        self.assertEqual(self.connects, 0)
        record = WorkspaceStore().audit_records()[0]
        self.assertEqual((record['status'], record['outcome']), ('refused', 'password_input'))

    def test_terminal_prompt_declined(self):
        self.conn = FakeConn(name='cli_u')
        code, out, err = self.run_cli(['accounts', 'lock', 'cli_u@%'], Stdin('n\n', tty=True))
        self.assertEqual(code, 2)
        self.assertIn('[y/N]', err)
        self.assertEqual(self.conn.stdin, [])
        self.assertEqual(WorkspaceStore().audit_records()[0]['status'], 'declined')

    def test_typed_name_for_protected_accounts(self):
        self.conn = FakeConn(name='root', host='localhost')
        code, out, err = self.run_cli(['accounts', 'lock', 'root@localhost', '--yes'])
        self.assertEqual(code, 2)
        self.assertIn('Type', err)
        self.assertEqual(self.conn.stdin, [])
        code, out, err = self.run_cli(['accounts', 'lock', 'root@localhost', '--yes',
                                       '--confirm-name', 'root@localhost'])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.conn.stdin), 1)

    def test_read_only_profile_refuses_before_connecting(self):
        store = WorkspaceStore()
        store.save('profile', {'name': 'ro', 'host': '127.0.0.1', 'port': 3306, 'user': 'admin', 'read_only': True})
        code, out, err = self.run_cli(['accounts', 'drop', 'cli_u@%', '--profile', 'ro', '--yes'])
        self.assertEqual(code, 2)
        self.assertEqual(self.connects, 0)
        self.assertIn('read-only', err)
        self.assertEqual(store.audit_records()[0]['status'], 'refused')

    def test_grant_targets_and_bad_input(self):
        self.conn = FakeConn(name='cli_u')
        for argv, expected in [
                (['--on', 'shop.*'], 'GRANT CREATE ROUTINE ON `shop`.* TO `cli_u`@`%`'),
                (['--on', 'shop.orders', '--columns', 'a,b'], 'GRANT CREATE ROUTINE'),
                (['--on', 'PROCEDURE shop.p'], None), (['--on', 'db_x.*', '--pattern'], 'ON `db_x`.*')]:
            with self.subTest(argv=argv):
                privilege = 'EXECUTE' if 'PROCEDURE' in argv[1] else 'SELECT' if '--columns' in argv else 'CREATE ROUTINE'
                code, out, err = self.run_cli(['accounts', 'grant', 'cli_u@%', privilege] + argv + ['--yes', '--json'])
                self.assertEqual(code, 0, err)
                result = json.loads(out)
                statement = result['statements'][0]['statement']
                if expected and privilege == 'CREATE ROUTINE':
                    self.assertIn(expected, statement)
                if '--columns' in argv:
                    self.assertIn('SELECT (`a`, `b`) ON `shop`.`orders`', statement)
                if 'PROCEDURE' in argv[1]:
                    self.assertIn('ON PROCEDURE `shop`.`p`', statement)
        for argv in (['accounts', 'grant', 'cli_u@%', 'SELECT', '--on', 'nonsense'],
                     ['accounts', 'grant', 'cli_u@%', 'SELECT'],
                     ['accounts', 'role', 'grant', 'cli_u@%'],
                     ['accounts', 'frobnicate', 'x']):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(argv + ['--yes'])
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith('myflames:') or 'usage' in err.lower(), err)

    def test_password_needs_a_source_without_a_terminal(self):
        self.conn = FakeConn(name='cli_u')
        code, out, err = self.run_cli(['accounts', 'password', 'cli_u@%', '--yes'])
        self.assertEqual(code, 2)
        self.assertIn('--password-stdin', err)
        self.assertEqual(self.connects, 0)

    def test_generated_password_printed_once(self):
        self.conn = FakeConn(name='cli_u')
        self.conn.run_stdin = mock.Mock(return_value='cli_u\t%\tGen-SECRET-123\t1\n')
        code, out, err = self.run_cli(['accounts', 'password', 'cli_u@%', '--random-password', '--yes'])
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count('Gen-SECRET-123'), 1)
        self.assertIn('copy the generated password now', err)
        self.assertNotIn('Gen-SECRET-123', json.dumps(WorkspaceStore().audit_records()))

    def test_list_and_show(self):
        self.conn, data = replay.reads('mariadb-11.4')
        from test_accounts import catalog_replay
        catalog_replay(self.conn, 'mariadb-11.4')
        code, out, err = self.run_cli(['accounts', 'list'])
        self.assertEqual(code, 0, err)
        self.assertIn('PUBLIC', out)
        code, out, err = self.run_cli(['accounts', 'show', 'acct_app@%', '--json', '-q'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['schema_version'], 'account-1.0')
        code, out, err = self.run_cli(['accounts', 'show', 'nobody@%', '-q'])
        self.assertEqual(code, 1)
        for secret in replay.secrets(data):
            self.assertNotIn(secret, out)

    def test_json_refusals_and_declines_are_stderr_only_and_audited(self):
        self.conn = FakeConn(name='cli_u')
        WorkspaceStore().save('profile', {'name': 'ro', 'host': '127.0.0.1', 'read_only': True})
        cases = [(['--profile', 'ro', '--yes', '-q'], Stdin(), 'refused'),
                 ([], Stdin(), 'refused'),
                 (['--password-stdin'], Stdin('secret\n'), 'refused'),
                 ([], Stdin('n\n', tty=True), 'declined')]
        for flags, stream, status in cases:
            with self.subTest(flags=flags, status=status):
                command = 'password' if '--password-stdin' in flags else 'lock'
                code, out, err = self.run_cli(['accounts', command, 'cli_u@%', '--json'] + flags, stream)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, '')
                record = WorkspaceStore().audit_records()[0]
                self.assertEqual(record['status'], status)
                self.assertIn(record['message'], err)
                self.assertTrue(record['id'])
        self.assertEqual(self.conn.stdin, [])

    def test_write_output_supports_text_json_refusals_and_io_errors(self):
        self.conn = FakeConn(name='cli_u')
        destination = Path(self.temp.name) / 'result.txt'
        for fmt in ([], ['--json']):
            for allowed in (False, True):
                with self.subTest(fmt=fmt, allowed=allowed):
                    flags = ['--yes', '-q'] if allowed else []
                    destination.write_text('previous result')
                    code, out, err = self.run_cli(['accounts', 'lock', 'cli_u@%', '--output', str(destination)]
                                                 + fmt + flags)
                    self.assertEqual(code, 0 if allowed else 2, err)
                    self.assertEqual(out, '')
                    text = destination.read_text()
                    if allowed:
                        self.assertEqual(json.loads(text)['status'] if fmt else text.split(':')[0], 'applied')
                        self.assertEqual(err, '')
                    else:
                        self.assertEqual(text, 'previous result')
                        self.assertIn('--yes', err)
        code, out, err = self.run_cli(['accounts', 'lock', 'cli_u@%', '--yes', '-o', self.temp.name])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertIn('Cannot write', err)

    def test_typed_name_prompt_interrupt_and_eof_are_audited_declines(self):
        self.conn = FakeConn(name='root', host='localhost')
        for interrupt in (KeyboardInterrupt, EOFError, None):
            with self.subTest(interrupt=interrupt):
                stream = Stdin(tty=True)
                answers = ['y\n', interrupt] if interrupt else ['y\n', '']
                with mock.patch.object(stream, 'readline', side_effect=answers):
                    code, out, err = self.run_cli(['accounts', 'lock', 'root@localhost', '--json'], stream)
                self.assertEqual(code, 2, err)
                self.assertEqual(out, '')
                record = WorkspaceStore().audit_records()[0]
                self.assertEqual((record['status'], record['outcome']), ('declined', 'declined'))
                self.assertEqual(self.conn.stdin, [])
                self.assertNotIn('Password prompt cancelled', err)

    def test_typed_name_does_not_trim_spaces(self):
        self.conn = FakeConn(name='root', host='localhost')
        for value in (' root@localhost', 'root@localhost ', 'ROOT@localhost'):
            with self.subTest(value=value):
                code, out, err = self.run_cli(['accounts', 'lock', 'root@localhost', '--json'],
                                             Stdin('y\n' + value + '\n', tty=True))
                self.assertEqual((code, out), (2, ''), err)
                self.assertEqual(WorkspaceStore().audit_records()[0]['status'], 'declined')
                self.assertEqual(self.conn.stdin, [])

    def test_new_password_prompt_interruptions_are_declined_once(self):
        for exception in (EOFError, KeyboardInterrupt):
            with self.subTest(exception=exception), mock.patch('getpass.getpass', side_effect=exception):
                before = len(WorkspaceStore().audit_records())
                code, out, err = self.run_cli(['accounts', 'password', 'cli_u@%', '--yes', '--json'],
                                             Stdin(tty=True))
                self.assertEqual((code, out), (2, ''), err)
                records = WorkspaceStore().audit_records()
                self.assertEqual(len(records), before + 1)
                self.assertEqual((records[0]['status'], records[0]['outcome']), ('declined', 'declined'))
                self.assertEqual(self.connects, 0)

    def test_role_group_and_nested_help(self):
        for suffix in ([], ['--help'], ['grant', '--help'], ['revoke', '--help'], ['default', '--help']):
            with self.subTest(suffix=suffix):
                code, out, err = self.run_cli(['accounts', 'role'] + suffix)
                self.assertEqual(code, 0, err)
                self.assertIn('myflames accounts role', out)
                if len(suffix) < 2:
                    for action in ('grant', 'revoke', 'default'):
                        self.assertIn(action, out)
                else:
                    self.assertIn('--output', out)
                self.assertEqual(err, '')
        self.assertEqual(self.connects, 0)

    def test_help_lists_the_subcommands(self):
        code, out, err = self.run_cli(['accounts', '--help'])
        self.assertEqual(code, 0)
        for word in ('create', 'grant', 'role default', '--password-stdin'):
            self.assertIn(word, out)
        code, out, err = self.run_cli(['--help'])
        self.assertIn('privileges', out)
        self.assertIn('accounts', out)


if __name__ == '__main__':
    unittest.main()
