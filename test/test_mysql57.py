"""MySQL 5.7 outside the Server place's read views: privileges, Users and
roles, Run analysis, the Query editor's transaction probe and plans.

Recordings come from a throwaway MySQL 5.7.44 container (linux/amd64, see
test/fixtures/accounts/PROVENANCE.txt and the mysql-5.7-*.json plans in
test/fixtures). MariaDB is not part of these tests: every branch here is
gated on a MySQL version before 8.0 (or 8.0.16 / 8.0.18), and the MariaDB
answers are covered by test_privileges, test_accounts and test_live.
"""
import argparse
import ast
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import _accounts_replay as replay  # noqa: E402

from myflames import accounts as a, live, privileges as p  # noqa: E402
from myflames.connector import ConnectorError, MySQLConnection  # noqa: E402
from myflames.parser import flatten_nodes, parse_explain  # noqa: E402

SERVER = 'mysql-5.7'
FIXTURES = Path(__file__).parent / 'fixtures'


def recorded():
    return replay.load(SERVER)


def session_conn(key):
    data = recorded()
    return replay.ReplayConn(data['sessions'][key]['calls'], data['server']['version']), data


class Privileges57(unittest.TestCase):
    """privileges.read_privileges and the action map on MySQL 5.7.44."""

    def test_every_session_reads_again_without_current_role(self):
        data = recorded()
        self.assertEqual(data['server']['version'], '5.7.44-log')
        codes = set()
        for key, entry in data['sessions'].items():
            with self.subTest(session=key):
                conn, _ = session_conn(key)
                r = p.read_privileges(conn)
                self.assertEqual(r['account'], entry['login'] + '@%')
                self.assertEqual((r['engine'], r['version']), ('mysql', '5.7.44-log'))
                self.assertTrue(r['checked'])
                self.assertEqual((r['unparsed'], r['roles'], r['current_roles'], r['warnings']), ([], [], [], []))
                # First the usual read, then the same read with NULL for CURRENT_ROLE(); no
                # APPLICABLE_ROLES read (8.0.19+) and nothing from mysql.* or SHOW CREATE.
                self.assertEqual(len(conn.sent), 2)
                self.assertIn('CURRENT_USER(), CURRENT_ROLE(), VERSION()', conn.sent[0][0])
                self.assertIn('CURRENT_USER(), NULL, VERSION()', conn.sent[1][0])
                self.assertTrue(all(sql.startswith(p.TAG) for sql, _ in conn.sent))
                codes.add(int(entry['calls'][0]['error'].split('ERROR ')[1].split(' ')[0]))
                text = json.dumps(r, allow_nan=False)
                for secret in replay.secrets(data):
                    self.assertNotIn(secret, text)
        # No default database, a database without EXECUTE, a database with it (all live).
        self.assertEqual(codes, {1046, 1305, 1370})

    def test_other_errors_are_not_retried(self):
        for error in ('ERROR 1045 (28000): Access denied for user', 'ERROR 1305 (42000) at line 1: FUNCTION '
                      'shop.OTHER does not exist', 'ERROR 2003 (HY000): Can\'t connect'):
            with self.subTest(error=error):
                conn = mock.Mock()
                conn.run.side_effect = ConnectorError('mysql client exited 1: ' + error)
                with self.assertRaises(ConnectorError):
                    p.read_privileges(conn)
                self.assertEqual(conn.run.call_count, 1)

    def test_features_57_lacks_are_unavailable_with_their_reason(self):
        conn, _ = session_conn('acct_admin')
        r = p.read_privileges(conn)
        notes = {'run_analysis': p.NO_ANALYZE_NOTE, 'set_persist': p.NO_PERSIST_NOTE,
                 'set_persist_only': p.NO_PERSIST_NOTE, 'reset_persist': p.NO_PERSIST_NOTE,
                 'create_role': p.NO_ROLES_NOTE, 'drop_role': p.NO_ROLES_NOTE, 'grant_role': p.NO_ROLES_NOTE,
                 'revoke_role': p.NO_ROLES_NOTE, 'default_roles': p.NO_ROLES_NOTE,
                 'backup_lock': p.NO_BACKUP_LOCK_NOTE, 'clone_donor': 'CLONE needs MySQL 8.0.17 or later.',
                 'clone_recipient': 'CLONE needs MySQL 8.0.17 or later.',
                 'error_log': 'performance_schema.error_log needs MySQL 8.0.22 or later.'}
        for key, note in notes.items():
            with self.subTest(action=key):
                answer = r['actions'][key]
                self.assertEqual((answer['available'], answer['allowed'], answer['grey_out']), (False, False, False))
                self.assertEqual(answer['note'], note)
                self.assertTrue(answer['reason'].startswith('Not available on this engine or version.'))
        # Everything else this account holds (ALL PRIVILEGES) stays allowed, kill without a SYSTEM_USER caveat.
        for key in ('kill', 'set_global', 'create_user', 'grant', 'explain_connection', 'read_only_bypass',
                    'skip_counter', 'replica_start_stop', 'insert', 'alter_table', 'dump_shell', 'dump_mysqldump'):
            with self.subTest(action=key):
                self.assertEqual(r['actions'][key]['state'], 'allowed', r['actions'][key]['reason'])

    def test_only_static_alternatives_remain(self):
        conn, _ = session_conn('acct_ops')
        ops = p.read_privileges(conn)
        self.assertEqual(ops['global'], ['RELOAD', 'PROCESS', 'SUPER', 'CREATE USER'])
        self.assertEqual(ops['actions']['kill']['via'], 'SUPER')
        conn, _ = session_conn('acct_app')
        app = p.read_privileges(conn)
        for key, need in (('kill', 'SUPER'), ('set_global', 'SUPER'), ('read_only_bypass', 'SUPER'),
                          ('set_session_restricted', 'SUPER'), ('replica_start_stop', 'SUPER'),
                          ('skip_gtid', 'SUPER')):
            with self.subTest(action=key):
                answer = app['actions'][key]
                self.assertEqual((answer['state'], answer['need'], answer['missing']), ('denied', need, [need]))
                self.assertNotIn('ADMIN', answer['reason'])
        # The same rules on an unknown version keep the dynamic alternatives.
        self.assertIn([{'privilege': 'CONNECTION_ADMIN', 'level': 'global'}],
                      p.requirements('kill', 'mysql', '')['alternatives'])
        self.assertTrue(p.requirements('set_persist', 'mysql', '')['available'])

    def test_no_system_user_before_8016(self):
        model = p.parse_grants(['GRANT ALL PRIVILEGES ON *.* TO `x`@`%`'], 'mysql', '5.7.44')
        model['account'] = 'x@%'
        self.assertIs(p.can(model, 'kill')['allowed'], True)
        self.assertIs(p.can(dict(model, version='8.0.15'), 'kill')['allowed'], True)
        # From 8.0.16 a target session may hold SYSTEM_USER, which ALL alone does not grant.
        self.assertIsNone(p.can(dict(model, version='8.4.0'), 'kill')['allowed'])
        # The reason text never names SYSTEM_USER where it does not exist.
        self.assertNotIn('SYSTEM_USER', p.requirements('kill', 'mysql', '5.7.44')['note'])
        self.assertNotIn('SYSTEM_USER', p.requirements('kill', 'mysql', '8.0.15')['note'])
        self.assertIn('SYSTEM_USER', p.requirements('kill', 'mysql', '8.4.0')['note'])

    def test_static_privileges_and_all_on_57(self):
        old = p.static_privileges('mysql', '5.7.44', 'global')
        self.assertFalse({'CREATE ROLE', 'DROP ROLE'} & old)
        self.assertTrue({'CREATE ROLE', 'DROP ROLE'} <= p.static_privileges('mysql', '8.0.11', 'global'))
        self.assertTrue({'CREATE ROLE', 'DROP ROLE'} <= p.static_privileges('mysql', '', 'global'))
        spec = p.requirements('grant', 'mysql', '5.7.44', privileges=['ALL'], level='global', all_privileges=[])
        self.assertTrue(spec['available'])
        names = {atom['privilege'] for atom in spec['alternatives'][0]}
        self.assertEqual(names, set(old))
        need = p.requirements('explain_connection', 'mysql', '5.7.44')['alternatives'][0]
        self.assertEqual({atom['privilege'] for atom in need}, set(old))
        # MySQL Shell checks BACKUP_ADMIN only from 8.0.0 (mysql-shell dumper.cc).
        shell = p.requirements('dump_shell', 'mysql', '5.7.44', single_transaction=True)
        self.assertTrue(shell['available'])
        held = {atom['privilege'] for alternative in shell['alternatives'] for atom in alternative}
        self.assertFalse({'BACKUP_ADMIN', 'FLUSH_TABLES'} & held)
        self.assertTrue({'RELOAD', 'REPLICATION CLIENT'} <= held)


class Accounts57(unittest.TestCase):
    """Users and roles on MySQL 5.7.44: no roles, the 5.7 forms of each change."""

    def reads(self):
        data = recorded()
        return replay.ReplayConn(data['reads']['calls'], data['server']['version']), data

    def test_list_without_role_tables(self):
        conn, _ = self.reads()
        listing = a.list_accounts(conn)
        self.assertEqual((listing['roles_supported'], listing['roles_note']), (False, p.NO_ROLES_NOTE))
        self.assertEqual(listing['warnings'], [p.NO_ROLES_NOTE])
        self.assertTrue(listing['complete'])
        self.assertFalse(any('role_edges' in sql or 'default_roles' in sql for sql, _ in conn.sent))
        rows = {(r['name'], r['host']): r for r in listing['accounts']}
        self.assertFalse(any(r['role'] for r in rows.values()))
        self.assertTrue(rows[('locked', '%')]['locked'])
        self.assertTrue(rows[('mysql.sys', 'localhost')]['system'])
        self.assertEqual((listing['role_grants'], listing['default_roles']), ([], []))

    def test_read_accounts(self):
        conn, data = self.reads()
        proxy = a.read_account(conn, 'acct_proxy', '%', include_catalog=False)
        self.assertEqual(proxy['attributes']['limits'], {'MAX_USER_CONNECTIONS': '3'})
        self.assertEqual(proxy['attributes']['password_options'], {'expire_days': 30})
        self.assertEqual(proxy['model']['proxies'][0]['account'], {'name': 'acct_app', 'host': '%', 'role': False})
        sha = a.read_account(conn, 'acct_sha', '%', include_catalog=False)
        self.assertEqual(sha['attributes']['plugins'], ['sha256_password'])
        self.assertTrue(a.read_account(conn, 'locked', '%', include_catalog=False)['attributes']['locked'])
        root = a.read_account(conn, 'root', 'localhost', include_catalog=False)
        self.assertEqual(root['protected'], 'root account')
        self.assertEqual((root['default_roles'], root['roles']), ([], []))
        self.assertFalse(a.read_account(conn, 'no_such', '%', include_catalog=False)['exists'])
        app = a.read_account(conn, 'acct_app', '%')
        self.assertEqual(app['privilege_catalog_meta']['source'], 'server')
        names = {e['name'] for e in app['privilege_catalog']['global']}
        self.assertFalse({'CREATE ROLE', 'DROP ROLE'} & names)
        self.assertFalse(any(e['dynamic'] for e in app['privilege_catalog']['global']))
        # `db\_x` (literal, escaped) and `db_x` (a pattern) stay two scopes.
        self.assertEqual(sorted(s.get('pattern') for s in app['grant_scopes'] if s['label'] == '`db_x`.*'),
                         [False, True])
        self.assertEqual(app['model']['unparsed'], [])
        text = json.dumps([proxy, sha, root, app])
        for secret in replay.secrets(data):
            self.assertNotIn(secret, text)
        self.assertNotIn("AS '", text)

    def plan(self, key):
        data = recorded()
        entry = data['plans'][key]
        conn = replay.ReplayConn(entry['calls'], data['server']['version'])
        options = dict(entry['options'])
        if key == 'create_typed_locked':
            options['password'] = 'Typed57!pw'
        return a.plan_change(conn, entry['operation'], entry['name'], entry['host'], **options), entry

    def test_recorded_plans(self):
        plan, entry = self.plan('create_random')
        self.assertEqual(plan['statements'], entry['statements'])
        self.assertIn("CREATE USER `newbie`@`%` IDENTIFIED BY '[redacted]';", plan['statements'][0])
        self.assertNotIn('RANDOM PASSWORD', plan['statements'][0])
        self.assertTrue(plan['generates_password'])
        self.assertIn(plan._generated, plan._statements[0])
        self.assertNotIn(plan._generated, json.dumps(plan))
        self.assertIn('myflames generates the password and shows it once; copy it then, myflames does not '
                      'store it.', plan['warnings'])
        plan, entry = self.plan('create_typed_locked')
        self.assertEqual(plan['statements'], entry['statements'])
        self.assertIn("IDENTIFIED BY '[redacted]' ACCOUNT LOCK;", plan['statements'][0])
        plan, entry = self.plan('grant_all_global')
        self.assertEqual(plan['statements'], [a.TAG + 'GRANT ALL ON *.* TO `acct_app`@`%`'])
        self.assertEqual((plan['status'], plan['privileges']['state']), ('ready', 'allowed'))
        plan, entry = self.plan('lock_app')
        self.assertEqual(plan['statements'], [a.TAG + 'ALTER USER `acct_app`@`%` ACCOUNT LOCK'])
        plan, entry = self.plan('drop_definer')
        self.assertEqual(plan['statements'], [a.TAG + 'DROP USER `dfn2`@`%`'])
        definer = [w for w in plan['warnings'] if 'DEFINER' in w]
        self.assertEqual(len(definer), 1)
        self.assertIn('This MySQL version does not check', definer[0])
        self.assertIn('1449', definer[0])
        self.assertNotIn('4006', definer[0])
        self.assertEqual(plan['effects']['definers'], [{'kind': 'view', 'database': 'lab_definer', 'name': 'v2'}])
        data = recorded()
        self.assertEqual(data['plans']['options_history']['refused'],
                         'That password option is not available on this server version.')
        with self.assertRaisesRegex(ValueError, 'not available on this server version'):
            self.plan('options_history')

    def test_role_changes_are_refused_before_any_read(self):
        conn = mock.Mock()
        conn.server_version.return_value = '5.7.44-log'
        for operation, role, options in (('create', True, {}), ('drop', True, {}),
                                         ('roles', False, {'mode': 'grant', 'roles': [{'name': 'r'}]})):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, '^This MySQL version has no roles; MySQL added them in 8.0.$'):
                    a.plan_change(conn, operation, 'r', '%', role=role, **options)
        conn.run.assert_not_called()

    def test_server_floor_is_576(self):
        conn = mock.Mock()
        conn.server_version.return_value = '5.7.5-m15'
        with self.assertRaisesRegex(ValueError, 'MySQL 5.7.6 or later'):
            a.list_accounts(conn)
        conn.run.assert_not_called()


class Analyze57(unittest.TestCase):
    """Run analysis is refused before anything is sent on MySQL before 8.3 (no EXPLAIN ANALYZE FORMAT=JSON)."""

    def test_analyze_unsupported(self):
        for version, refused in (('5.7.44-log', True), ('5.7.44', True), ('8.0.17', True), ('8.0.18', True),
                                 ('8.2.0', True), ('8.3.0', False), ('8.4.11', False), ('', False), ('garbage', False)):
            with self.subTest(version=version):
                self.assertEqual(live.analyze_unsupported(version) is not None, refused)
        self.assertIsNone(live.analyze_unsupported('10.6.20-MariaDB', maria=True))
        self.assertEqual(live.analyze_unsupported('5.7.44-log'),
                         'Run analysis needs EXPLAIN ANALYZE FORMAT=JSON, which MySQL has from 8.3 (8.4 LTS and later), '
                         'and this server is MySQL 5.7.44; nothing was sent, so use Explain for the estimated plan.')

    def test_capture_job_sends_nothing(self):
        class FakeConn(object):
            _version_comment_cache = 'MySQL Community Server (GPL)'

            def __init__(self, **options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def server_version(self):
                return '5.7.44-log'

            def is_mariadb(self):
                return False

        job = live.LiveJob({'connection': {'host': '127.0.0.1', 'port': 3306, 'user': 'root', 'password': 'pw'},
                            'query': 'SELECT 1', 'mode': 'analyze'})
        with mock.patch.object(live, 'MySQLConnection', FakeConn), \
                mock.patch.object(live.LiveJob, '_run_script') as script:
            job.run()
        script.assert_not_called()
        self.assertEqual(job.state, 'failed')
        self.assertEqual(job.error, live.analyze_unsupported('5.7.44-log'))

    def test_connector_sends_nothing(self):
        conn = MySQLConnection('127.0.0.1', 3306, 'root', 'pw')
        with mock.patch.object(conn, 'server_version', return_value='5.7.44-log'), \
                mock.patch.object(conn, 'is_mariadb', return_value=False), \
                mock.patch.object(conn, 'run') as run:
            with self.assertRaisesRegex(ConnectorError, 'nothing was sent'):
                conn.explain_analyze('SELECT 1')
        run.assert_not_called()


CLI_HAS_GATE = 'analyze_unsupported' in (Path(__file__).parent.parent / 'myflames' / 'cli.py').read_text(
    encoding='utf-8')


@unittest.skipUnless(CLI_HAS_GATE, 'cli._run_live_explain gets the MySQL 5.7 gate when the coordinator applies the '
                                   'cli.py change described in docs/_lane_notes/MY57B.md; this test runs from then on.')
class LiveRender57(unittest.TestCase):
    """The default command with live flags (cli._run_live_explain)."""

    def run_with(self, version):
        from myflames import cli
        sent = []

        class FakeConn(object):
            def __init__(self, **options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def is_mariadb(self):
                return False

            def server_version(self):
                return version

            def run_stdin(self, sql, extra_flags=None, timeout=None):
                sent.append(sql)
                return '{}'

        args = argparse.Namespace(execute='SELECT 1', password=None, database=None, no_collect_schema=True,
                                  no_collect_stats=True, no_collect_variables=True)
        err = io.StringIO()
        with mock.patch('myflames.connector.MySQLConnection', FakeConn), \
                mock.patch('myflames.workspace_cli._resolve_connection', return_value={'password': None}), \
                mock.patch('myflames.workspace_cli._connection_options', return_value={}), \
                mock.patch('sys.stderr', err):
            try:
                cli._run_live_explain(args)
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue(), sent

    def test_57_is_refused_before_sending(self):
        code, err, sent = self.run_with('5.7.44-log')
        self.assertEqual((code, sent), (2, []))
        self.assertIn('nothing was sent', err)

    def test_json_format_version_only_from_83(self):
        for version in ('8.0.46', '8.2.0'):
            with self.subTest(version=version):
                code, _err, sent = self.run_with(version)
                self.assertEqual((code, len(sent)), (2, 0), 'refused before sending')
        for version in ('8.3.0', '8.4.11'):
            with self.subTest(version=version):
                code, _err, sent = self.run_with(version)
                self.assertEqual((code, len(sent)), (0, 1))
                self.assertIn('explain_json_format_version', sent[0])


class EditorTransactionProbe57(unittest.TestCase):
    """PS_CURRENT_THREAD_ID() is MySQL 8.0.16+; 5.7 finds its thread by connection id."""

    def probe_sql(self, version):
        from myflames import editor_session as ed
        session = ed.EditorSession('t', 'identity', {'host': '127.0.0.1', 'port': 3306, 'user': 'root'})
        session.engine, session.server_version = 'mysql', version
        sent = []

        class Step(object):
            entries = []
            marker = True  # the batch completed (editor_insights.helper_rows)

            def real_errors(self):
                return []

        def helper(send, results=1, timeout=None):
            sent.append(send)
            return Step()
        with mock.patch.object(session, '_helper', side_effect=helper), \
                mock.patch.object(session, '_helper_row', return_value={'consumer': 'YES', 'instrument': 'YES',
                                                                         'instrumented': 'YES', 'state': 'ACTIVE',
                                                                         'autocommit': '1', 'sql_mode': '',
                                                                         'sql_safe_updates': '0',
                                                                         'connection_id': '7'}):
            session.connection_id = 7
            session.refresh_state()
        return sent[0], session

    def test_thread_lookup_by_version(self):
        old, session = self.probe_sql('5.7.44-log')
        self.assertNotIn('PS_CURRENT_THREAD_ID', old)
        self.assertEqual(old.count('(SELECT THREAD_ID FROM performance_schema.threads WHERE PROCESSLIST_ID = '
                                   'CONNECTION_ID())'), 2)
        self.assertIs(session.in_transaction, True)
        for version in ('8.0.16', '8.4.11', '', None):
            with self.subTest(version=version):
                new, _ = self.probe_sql(version)
                self.assertEqual(new.count('PS_CURRENT_THREAD_ID()'), 2)


class Plans57(unittest.TestCase):
    """EXPLAIN FORMAT=JSON from MySQL 5.7.44 (the query_block format) goes
    through the single parser like MySQL 8.0's format version 1."""

    EXPECTED = {
        'block-nested-loop': ['Nested loop inner join', 'Table scan [a]', 'Table scan [b]'],
        'impossible-where': ['Impossible WHERE'],
        'no-tables': ['No tables used'],
        'range-covering': ['Index range scan [orders.idx_customer]'],
        'union': ['Union', 'Index lookup [customers.idx_country]', 'Table scan [orders]'],
        'join-group-order': ['Sort', 'Group', 'Nested loop inner join', 'Index lookup [c.idx_country]',
                             'Index lookup [o.idx_customer]'],
    }

    def test_every_57_plan_parses(self):
        files = sorted(FIXTURES.glob('mysql-5.7-*.json'))
        self.assertEqual(len(files), 10)
        for path in files:
            with self.subTest(plan=path.name):
                text = path.read_text(encoding='utf-8')
                self.assertIn('"query_block"', text)
                root = parse_explain(text)
                labels = [n.get('short_label') for n in flatten_nodes(root)]
                self.assertTrue(labels and all(labels))
                expected = self.EXPECTED.get(path.stem[len('mysql-5.7-'):])
                if expected is not None:
                    self.assertEqual(labels, expected)


class Syntax(unittest.TestCase):
    def test_python37_syntax(self):
        root = Path(__file__).parent.parent / 'myflames'
        for name in ('privileges.py', 'accounts.py', 'live.py', 'connector.py', 'editor_session.py',
                     'alter_algorithm.py', 'csv_import_api.py'):
            # feature_version needs Python 3.8; on 3.7 the parse itself checks it.
            ast.parse((root / name).read_text(encoding='utf-8'),
                      **({'feature_version': (3, 7)} if sys.version_info >= (3, 8) else {}))


if __name__ == '__main__':
    unittest.main()
