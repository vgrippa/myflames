"""accounts.py: credential-free reads (live recordings), plans, guards and
audited apply (fake connections; no server is ever written)."""
import ast
import copy
from collections import deque
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import _accounts_replay as replay  # noqa: E402

from myflames import accounts as a, dbops, privileges as p  # noqa: E402
from myflames.connector import ConnectorError, MySQLConnection  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402

MYSQL_VERSION = '8.4.11'
MARIADB_VERSION = '11.4.13-MariaDB'


def actor(engine='mysql', account='admin@localhost', grants=None, variables=None, roles=()):
    """A privileges-1.0 report as read_privileges returns it."""
    grants = grants if grants is not None else [
        'GRANT SELECT, INSERT, UPDATE, DELETE, PROCESS, CREATE USER, CREATE ROLE, DROP ROLE, SUPER ON *.* TO '
        'admin@localhost WITH GRANT OPTION',
        'GRANT SYSTEM_USER, ROLE_ADMIN, CONNECTION_ADMIN ON *.* TO admin@localhost WITH GRANT OPTION']
    report = p.parse_grants(grants, engine, MARIADB_VERSION if engine == 'mariadb' else MYSQL_VERSION)
    report.update(account=account, current_roles=list(roles),
                  variables=variables if variables is not None else {'server_id': '42', 'hostname': 'test-server'})
    return report


def catalog_replay(conn, server):
    """Add the independent live catalog capture, preserving both recordings."""
    data = json.loads((replay.FIXTURES / ('show-privileges-' + server + '.json')).read_text(encoding="utf-8"))
    for call in data['sessions']['root']['calls']:
        conn.answers[call['sql']] = call
    return conn


class FakeConn(object):
    """Answers the reads accounts.py sends; writes must use run_stdin."""
    host, port, user = '127.0.0.1', 3306, 'admin'
    ssl_mode = ssl_ca = ssl_cert = ssl_key = ssl_cipher = None

    def __init__(self, engine='mysql', exists=True, name='app', host='%', grants=None):
        self.engine, self.exists, self.name, self.account_host = engine, exists, name, host
        self.calls, self.locked, self.expired = [], False, False
        self.grants = grants if grants is not None else ['GRANT SELECT ON *.* TO ' + p.quote_account(name, host)]
        self.defaults = []
        self.destination_exists = False
        self.plugins = []
        self.role_grants = {'`r`@`%`': ['GRANT USAGE ON *.* TO `r`@`%`'], '`r`': ['GRANT USAGE ON *.* TO `r`']}
        self.version = MARIADB_VERSION if engine == 'mariadb' else MYSQL_VERSION
        self.stdin = []

    def server_version(self):
        return self.version

    def is_mariadb(self):
        return self.engine == 'mariadb'

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, extra_flags))
        assert sql.startswith(a.TAG), sql
        assert extra_flags == ['--comments'], extra_flags
        body = sql[len(a.TAG):]
        if body.startswith("SHOW GLOBAL VARIABLES WHERE Variable_name = 'partial_revokes'"):
            return 'partial_revokes\tOFF\n'
        if body.startswith('SET SESSION print_identified_with_as_hex = ON; ' + a.TAG):
            body = body[len('SET SESSION print_identified_with_as_hex = ON; ' + a.TAG):]
        for role, lines in self.role_grants.items():
            if body == 'SHOW GRANTS FOR ' + role:
                return '\n'.join(lines) + '\n'
        if body.startswith('SHOW GRANTS FOR '):
            quoted = p.quote_account(self.name, self.account_host)
            target = body[len('SHOW GRANTS FOR '):]
            if target in (quoted, 'PUBLIC', p.quote_identifier(self.name)) and self.exists:
                return '\n'.join(self.grants) + '\n'
            if self.destination_exists and target != quoted:
                return 'GRANT USAGE ON *.* TO ' + target + '\n'
            raise ConnectorError('mysql client exited 1: ERROR 1141 (42000) at line 1: There is no such grant')
        if body.startswith('SHOW CREATE USER '):
            if self.engine == 'mariadb':
                return ("CREATE USER `%s`@`%s` IDENTIFIED BY PASSWORD '*0123456789ABCDEF0123456789ABCDEF01234567'%s%s\n"
                        % (self.name, self.account_host, ' ACCOUNT LOCK' if self.locked else '',
                           ' PASSWORD EXPIRE' if self.expired else ''))
            roles = ','.join(p.quote_account(r['name'], r['host']) for r in self.defaults)
            return ("CREATE USER `app`@`%%` IDENTIFIED WITH 'caching_sha2_password' AS 0x48415348534543524554%s "
                    "REQUIRE NONE PASSWORD EXPIRE%s ACCOUNT %s PASSWORD HISTORY DEFAULT\n"
                    % (' DEFAULT ROLE ' + roles if roles else '', '' if self.expired else ' DEFAULT',
                       'LOCK' if self.locked else 'UNLOCK'))
        if body == 'SHOW PRIVILEGES':
            names = set(p.static_privileges(self.engine, self.version, 'global')) | {'PROXY', 'GRANT OPTION', 'USAGE'}
            if self.engine == 'mysql':
                names |= p.builtin_dynamic_privileges(self.version)
            return ''.join(name + '\tTables\tFixture name\n' for name in sorted(names))
        if body == 'SHOW DATABASES':
            return 'information_schema\nmysql\nperformance_schema\nshop\nsys\n'
        if 'information_schema.PLUGINS' in body:
            return ''.join(name + '\n' for name in self.plugins)
        if 'information_schema.PROCESSLIST' in body:
            return '17\tlocalhost\tSleep\n'
        if 'information_schema.' in body and 'DEFINER' in body:
            return 'shop\tobject_name\n'
        raise AssertionError('Unexpected SQL: ' + sql)

    def run_stdin(self, sql, extra_flags=None, timeout=None):
        self.stdin.append(sql)
        return ''

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()
        self.conn = FakeConn()
        patcher = mock.patch.object(p, 'read_privileges', side_effect=lambda conn: actor(conn.engine))
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, op, **kw):
        return a.plan_change(self.conn, op, self.conn.name, self.conn.account_host, **kw)

    def apply(self, plan, **kw):
        return a.apply(self.conn, plan, plan['expect'], store=self.store, confirmed=True, **kw)

    # ---- reads -------------------------------------------------------------------------

    def test_role_graph_deduplicates_when_enqueued_and_reads_each_role_once(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                conn = FakeConn(engine)
                role = lambda name: p.quote_account(name, '%' if engine == 'mysql' else '', True, engine)
                conn.grants += ['GRANT %s TO `app`@`%%`' % role('a')] * 1000
                conn.role_grants = {
                    role('a'): ['GRANT %s TO %s' % (role('b'), role('a'))] * 1000,
                    role('b'): ['GRANT %s TO %s' % (role('a'), role('b')),
                                'GRANT SELECT ON *.* TO ' + role('b')],
                }
                peaks = []

                class Queue(deque):
                    def append(self, item):
                        super(Queue, self).append(item)
                        peaks.append(len(self))

                with mock.patch.object(a, 'deque', Queue, create=True):
                    result = a.read_account(conn, 'app')
                self.assertTrue(peaks, 'role expansion must use the bounded deque')
                self.assertEqual(max(peaks), 1)
                for name in ('a', 'b'):
                    calls = [sql for sql, flags in conn.calls if sql == a.TAG + 'SHOW GRANTS FOR ' + role(name)]
                    self.assertEqual(len(calls), 1)
                self.assertTrue(result['role_grants_complete'])
                self.assertTrue(result['effective_model']['checked'])

    def test_role_budget_uses_remaining_deadline_and_marks_privileges_incomplete(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                conn = FakeConn(engine)
                role = lambda name: p.quote_account(name, '%' if engine == 'mysql' else '', True, engine)
                conn.grants += ['GRANT %s TO `app`@`%%`' % role('a')]
                conn.role_grants = {role('a'): ['GRANT %s TO %s' % (role('b'), role('a'))],
                                    role('b'): ['GRANT %s TO %s' % (role('c'), role('b'))],
                                    role('c'): ['GRANT SUPER ON *.* TO ' + role('c')]}
                now, timeouts = [0.0], []
                original = conn.run

                def run(sql, extra_flags=None, timeout=None):
                    if any(sql == a.TAG + 'SHOW GRANTS FOR ' + role(n) for n in ('a', 'b', 'c')):
                        timeouts.append(timeout)
                        now[0] += min(3, timeout) if timeout is not None else 3
                        if timeout is not None and timeout < 3:
                            raise ConnectorError('Query timed out')
                    return original(sql, extra_flags, timeout)

                with mock.patch.object(conn, 'run', side_effect=run), \
                        mock.patch.object(a, 'ROLE_READ_SECONDS', 5, create=True), \
                        mock.patch('time.monotonic', side_effect=lambda: now[0]):
                    result = a.read_account(conn, 'app')
                self.assertEqual(timeouts, [5, 2])
                self.assertFalse(result['role_grants_complete'])
                self.assertFalse(result['effective_model']['checked'])
                self.assertIsNone(p.has(result['effective_model'], 'SUPER'))
                self.assertTrue(any('incomplete' in warning for warning in result['warnings']))

    def test_role_count_cap_bounds_queue_and_reports_incomplete(self):
        conn = FakeConn()
        conn.grants += ['GRANT `r%d`@`%%` TO `app`@`%%`' % i for i in range(10)]
        conn.role_grants = {'`r%d`@`%%`' % i: ['GRANT USAGE ON *.* TO `r%d`@`%%`' % i] for i in range(10)}
        with mock.patch.object(a, 'MAX_ROLE_READS', 4, create=True):
            result = a.read_account(conn, 'app')
        self.assertEqual(len([sql for sql, flags in conn.calls if 'SHOW GRANTS FOR `r' in sql]), 4)
        self.assertFalse(result['role_grants_complete'])
        self.assertFalse(result['effective_model']['checked'])

    def test_read_account_never_returns_a_hash(self):
        for engine in ('mysql', 'mariadb'):
            conn = FakeConn(engine)
            if engine == 'mariadb':
                conn.grants = ["GRANT SELECT ON *.* TO `app`@`%` IDENTIFIED VIA mysql_native_password USING "
                               "'*SECRET_HASH' OR unix_socket"]
            result = a.read_account(conn, 'app')
            text = json.dumps(result)
            for secret in ('48415348534543524554', 'SECRET_HASH', '0123456789ABCDEF', 'IDENTIFIED'):
                self.assertNotIn(secret, text)
            self.assertFalse(result['attributes']['locked'])
            self.assertEqual(len(result['fingerprint']), 64)
        # MySQL 8.0.17+ reads SHOW CREATE USER with the hash as hex.
        conn = FakeConn()
        a.read_account(conn, 'app')
        self.assertTrue(any('print_identified_with_as_hex = ON' in sql for sql, _ in conn.calls))

    def test_read_errors_are_sanitized_and_missing_accounts_detected(self):
        self.conn.run = mock.Mock(side_effect=ConnectorError("ERROR 1227 IDENTIFIED BY '*HASH'"))
        with self.assertRaisesRegex(ConnectorError, '1227') as cm:
            a.read_account(self.conn, 'app')
        self.assertNotIn('HASH', str(cm.exception))
        self.conn.run.side_effect = ConnectorError('ERROR 1141 (42000): There is no such grant')
        self.assertFalse(a.read_account(self.conn, 'app')['exists'])

    def test_attributes_from_every_show_create_user_row(self):
        """MariaDB prints an expired account with an expiry policy as two
        rows (seen live on 11.4): CREATE USER ... PASSWORD EXPIRE, then ALTER
        USER ... PASSWORD EXPIRE INTERVAL 30 DAY."""
        text = ("CREATE USER `acct_proxy`@`%` IDENTIFIED BY PASSWORD '*27E2' WITH MAX_USER_CONNECTIONS 3 "
                "MAX_STATEMENT_TIME 2.500000 PASSWORD EXPIRE ALTER USER `acct_proxy`@`%` PASSWORD EXPIRE "
                "INTERVAL 30 DAY")
        attributes = a._attributes(text)
        self.assertTrue(attributes['expired'])
        self.assertEqual(attributes['password_options'], {'expire_days': 30})
        self.assertEqual(attributes['limits'], {'MAX_USER_CONNECTIONS': '3', 'MAX_STATEMENT_TIME': '2.500000'})
        self.assertEqual(attributes['plugins'], ['mysql_native_password'])
        self.assertFalse(a._attributes("CREATE USER x IDENTIFIED WITH 'p' PASSWORD EXPIRE DEFAULT ACCOUNT UNLOCK")
                         ['expired'])

    def test_recorded_reads_on_five_servers(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                conn, data = replay.reads(server)
                catalog_replay(conn, server)
                maria = server.startswith('mariadb')
                listing = a.list_accounts(conn)
                names = {x['name'] for x in listing['accounts']}
                self.assertTrue(listing['complete'])
                self.assertTrue({'acct_admin', 'acct_app', 'r_read'} <= names)
                roles = {x['name'] for x in listing['accounts'] if x.get('role')}
                self.assertIn('r_read', roles)
                self.assertNotIn('acct_app', roles)
                system = {x['name'] for x in listing['accounts'] if x.get('system')}
                self.assertIn('mariadb.sys' if maria else 'mysql.sys', system)
                if maria:
                    self.assertIn('PUBLIC', roles)
                app = a.read_account(conn, 'acct_app', '%')
                self.assertEqual(app['default_roles'], [{'name': 'r_read', 'host': '' if maria else '%'}])
                self.assertIn('r_read', [r['name'] for r in app['roles']])
                self.assertTrue(app['role_grants'])
                if not maria:
                    # SYSTEM_USER only through r_sys: flagged, but not a direct grant.
                    self.assertIn('SYSTEM_USER', app['dangerous'])
                    self.assertFalse(a._direct_system_user(app))
                admin = a.read_account(conn, 'acct_admin', '%')
                self.assertTrue(admin['protected'])
                self.assertEqual(a._direct_system_user(admin), not maria)
                proxy = a.read_account(conn, 'acct_proxy', '%')
                self.assertEqual(len(proxy['model']['proxies']), 1)
                if server == 'mariadb-11.4':
                    self.assertTrue(proxy['attributes']['expired'])
                    self.assertEqual(proxy['attributes']['password_options'], {'expire_days': 30})
                role = a.read_account(conn, 'r_read', '%', role=True)
                self.assertTrue(role['exists'])
                if not maria:
                    self.assertTrue(role['attributes']['locked'] and role['attributes']['expired'])
                self.assertFalse(a.read_account(conn, 'nobody', '%')['exists'])
                if maria:
                    self.assertTrue(a.read_account(conn, 'PUBLIC', '', role=True)['exists'])
                    ed = a.read_account(conn, 'acct_ed', '%')
                    self.assertEqual(ed['attributes']['plugins'], ['ed25519', 'unix_socket'])
                else:
                    pr = a.read_account(conn, 'acct_pr', '%')
                    self.assertTrue(pr['partial_revokes'])
                    self.assertEqual(len(pr['model']['restrictions']), 2)
                for value in (listing, app, admin, proxy, role):
                    text = json.dumps(value)
                    for secret in replay.secrets(data):
                        self.assertNotIn(secret, text)
                    self.assertNotIn('authentication_string', text)
                self.assertFalse(any('authentication_string' in sql and 'SELECT authentication_string' in sql
                                     for sql, _ in conn.sent))

    def test_list_accounts_falls_back_to_the_connected_account(self):
        conn = FakeConn()

        def denied(sql, extra_flags=None):
            if 'CURRENT_USER()' in sql:
                return 'admin@localhost\n'
            raise ConnectorError('ERROR 1142 (42000): SELECT command denied')
        conn.run = denied
        result = a.list_accounts(conn)
        self.assertFalse(result['complete'])
        self.assertEqual(result['accounts'], [{'name': 'admin', 'host': 'localhost', 'connected': True}])
        self.assertIn('SELECT on the mysql database', result['warnings'][0])

    def test_fingerprint_ignores_order_and_credentials_not_grants(self):
        before = a.read_account(self.conn, 'app')
        after = copy.deepcopy(before)
        after['grants'].append("GRANT USAGE ON *.* TO app@% IDENTIFIED BY PASSWORD '*A'")
        first = a.fingerprint(after)
        after['grants'][-1] = "GRANT USAGE ON *.* TO app@% IDENTIFIED BY PASSWORD '*B'"
        after['grants'].reverse()
        self.assertEqual(first, a.fingerprint(after))
        after['attributes']['locked'] = True
        self.assertNotEqual(first, a.fingerprint(after))

    # ---- plans -------------------------------------------------------------------------

    def test_create_statements_per_engine(self):
        for engine in ('mysql', 'mariadb'):
            self.conn = FakeConn(engine, exists=False)
            plan = self.plan('create', password="s'lash\\secret")
            rendered, actual = '\n'.join(plan['statements']), '\n'.join(plan._statements)
            self.assertNotIn('lash', rendered)
            self.assertIn('NO_BACKSLASH_ESCAPES', actual)
            self.assertIn("'s''lash\\secret'", actual)
            if engine == 'mysql':
                self.assertIn('CREATE USER `app`@`%` IDENTIFIED BY', rendered)
                self.assertEqual(len(plan['statements']), 1)
            else:
                # Without a validation plugin: no password in CREATE USER.
                self.assertEqual(plan['statements'][0], a.TAG + 'CREATE USER `app`@`%` ACCOUNT LOCK')
                self.assertIn("SET PASSWORD FOR `app`@`%` = PASSWORD('[redacted]')", rendered)
                self.assertEqual(plan['statements'][-1], a.TAG + 'ALTER USER `app`@`%` ACCOUNT UNLOCK')
                self.assertIn(a.LOG_WARNING, plan['warnings'])
                self.assertEqual(plan['privileges']['need'].count('UPDATE on mysql.*'), 2)
            role = self.plan('create', role=True)
            self.assertEqual(role['statements'], [a.TAG + ('CREATE ROLE `app`' if engine == 'mariadb'
                                                            else 'CREATE ROLE `app`@`%`')])
            with self.assertRaises(TypeError):
                import pickle
                pickle.dumps(plan)

    def test_mariadb_create_with_a_validation_plugin(self):
        """With simple_password_check active, CREATE USER without a password
        fails with 1819 (verified live on 10.11 and 11.4). myflames creates
        the account locked with a temporary random password, then SET
        PASSWORD (hashed in the binary log), then unlocks."""
        self.conn = FakeConn('mariadb', exists=False)
        self.conn.plugins = ['simple_password_check']
        plan = self.plan('create', password='Typed-pw1!z')
        self.assertEqual(len(plan['statements']), 3)
        self.assertIn("CREATE USER `app`@`%` IDENTIFIED BY '[redacted]' ACCOUNT LOCK", plan['statements'][0])
        temporary = plan._passwords[1]
        self.assertNotEqual(temporary, 'Typed-pw1!z')
        self.assertIn(temporary, plan._statements[0])
        self.assertNotIn('Typed-pw1!z', plan._statements[0])
        self.assertIn('Typed-pw1!z', plan._statements[1])
        self.assertTrue(any('simple_password_check' in w for w in plan['warnings']))
        self.assertNotIn(temporary, json.dumps(plan))
        for _ in range(20):
            generated = a.generate_password()
            self.assertTrue(any(c.isupper() for c in generated) and any(c.islower() for c in generated) and
                            any(c.isdigit() for c in generated) and any(not c.isalnum() for c in generated))
            self.assertFalse(set(generated) & set("'\"\\ `"))

    def test_mysql_random_password_and_current_password(self):
        self.conn.exists = False
        plan = self.plan('create', random_password=True)
        self.assertIn('IDENTIFIED BY RANDOM PASSWORD', plan['statements'][0])
        self.assertTrue(plan['generates_password'])
        self.conn.version = '8.0.17'
        with self.assertRaisesRegex(ValueError, '8.0.18'):
            self.plan('create', random_password=True)
        self.conn = FakeConn(name='admin', host='localhost')
        plan = self.plan('password', password='new!', current_password='old!')
        self.assertIn("REPLACE '[redacted]'", plan['statements'][0])
        self.assertNotIn('old!', json.dumps(plan))
        self.assertEqual(plan['action'], 'set_own_password')
        # Same user name at another host is another account (user AND host).
        self.conn = FakeConn(name='admin', host='%')
        with self.assertRaisesRegex(ValueError, 'same user and host'):
            self.plan('password', password='new!', current_password='old!')
        self.assertEqual(self.plan('password', password='new!')['action'], 'set_password')

    def test_names_are_quoted_and_validated(self):
        self.conn = FakeConn(name="a`'; DROP USER root;--", host='10.0.%')
        plan = self.plan('lock')
        self.assertEqual(plan['statements'][0], a.TAG + "ALTER USER `a``'; DROP USER root;--`@`10.0.%` ACCOUNT LOCK")
        self.assertEqual(dbops.classify(plan['statements'][0])['count'], 1)
        for name, host in [('x\0y', '%'), ('x', 'bad@host'), ('x', ' host'), ('x', '\n'), (' x', '%')]:
            with self.subTest(name=name, host=host), self.assertRaises(ValueError):
                a.plan_lock(self.conn, name, host)

    def test_regression_random_password_replace_uses_literal_mode(self):
        # MySQL alone supports RANDOM PASSWORD ... REPLACE.
        self.conn = FakeConn(name='admin', host='localhost')
        password = "current'\\"
        plan = self.plan('password', random_password=True, current_password=password)
        sql = plan._statements[0]
        self.assertEqual(sql, a._with_password_mode(
            "ALTER USER `admin`@`localhost` IDENTIFIED BY RANDOM PASSWORD REPLACE " + a._password(password)))
        self.assertIn('NO_BACKSLASH_ESCAPES', sql)
        self.assertNotIn(password, json.dumps(plan))
        self.assertIn("REPLACE '[redacted]'", plan['statements'][0])
        with self.assertRaisesRegex(ValueError, 'empty host'):
            a.plan_lock(FakeConn('mariadb'), 'app', '')
        for bad in ('', 'NONE', 'r '):
            with self.assertRaises(ValueError):
                a.plan_create(FakeConn('mariadb', exists=False), bad, role=True)
        self.assertEqual(a.parse_account("'app'@'10.0.%'"), ('app', '10.0.%'))
        self.assertEqual(a.parse_account('`a@b`@`h`'), ('a@b', 'h'))
        self.assertEqual(a.parse_account('app'), ('app', '%'))

    def test_parse_account_with_either_side_quoted(self):
        for text, expected in [("'app'@localhost", ('app', 'localhost')),
                               ("app@'10.0.%'", ('app', '10.0.%')),
                               ('`a@b`@localhost', ('a@b', 'localhost')),
                               ("'a''b'@localhost", ("a'b", 'localhost')),
                               ('app@"localhost"', ('app', 'localhost')),
                               ("''@localhost", ('', 'localhost')),
                               ("'app'@10.0.%", ('app', '10.0.%')),
                               ("'a@b'@10.0.%", ('a@b', '10.0.%')),
                               ("some user@'localhost'", ('some user', 'localhost')),
                               ('app@10.0.%', ('app', '10.0.%'))]:
            with self.subTest(text=text):
                self.assertEqual(a.parse_account(text), expected)

    def test_each_operation_per_engine(self):
        for engine in ('mysql', 'mariadb'):
            self.conn = FakeConn(engine)
            for op, clause in [('lock', 'ACCOUNT LOCK'), ('unlock', 'ACCOUNT UNLOCK'), ('expire', 'PASSWORD EXPIRE')]:
                self.assertEqual(self.plan(op)['statements'], [a.TAG + 'ALTER USER `app`@`%` ' + clause])
            self.assertEqual(self.plan('drop')['statements'], [a.TAG + 'DROP USER `app`@`%`'])
            self.assertEqual(self.plan('rename', new_name='new', new_host='localhost')['statements'],
                             [a.TAG + 'RENAME USER `app`@`%` TO `new`@`localhost`'])
            drop = self.plan('drop')
            self.assertTrue(drop['effects']['sessions'])
            self.assertEqual(len(drop['effects']['definers']), 4)
            definer = ' '.join(w for w in drop['warnings'] if 'DEFINER' in w)
            if engine == 'mysql':
                self.assertIn('4006', definer)
            else:
                # 1449 through MariaDB 11.8 (seen live on 10.11 and 11.4), 1446 from 12.0.1.
                self.assertIn('1449', definer)
                self.assertIn('1045', definer)
            default = self.plan('roles', mode='default', special='NONE')
            expected = 'SET DEFAULT ROLE NONE FOR `app`@`%`' if engine == 'mariadb' else 'ALTER USER `app`@`%` DEFAULT ROLE NONE'
            self.assertEqual(default['statements'], [a.TAG + expected])
            grant = self.plan('roles', mode='grant', roles=[{'name': 'r', 'host': '%'}], admin_option=True)
            self.assertEqual(grant['statements'], [a.TAG + 'GRANT `r`' + ('' if engine == 'mariadb' else '@`%`') +
                                                   ' TO `app`@`%` WITH ADMIN OPTION'])
            options = self.plan('options', limits={'MAX_USER_CONNECTIONS': 5}, expire=30)
            self.assertEqual(options['statements'],
                             [a.TAG + 'ALTER USER `app`@`%` WITH MAX_USER_CONNECTIONS 5 PASSWORD EXPIRE INTERVAL 30 DAY'])
        conn = FakeConn('mariadb')
        conn.version = '12.0.2-MariaDB'
        drop = a.plan_drop(conn, 'app', '%')
        self.assertIn('1446', ' '.join(drop['warnings']))

    def test_grant_scopes(self):
        for engine in ('mysql', 'mariadb'):
            self.conn = FakeConn(engine)
            for pattern, expected in [(False, '`db\\_x\\%`.*'), (True, '`db_x%`.*')]:
                plan = self.plan('grant', privileges=['SELECT'], level='database', database='db_x%', pattern=pattern)
                self.assertIn(' ON ' + expected, plan['statements'][0])
            plan = self.plan('grant', privileges=['CREATE ROUTINE'], level='database', database='shop')
            self.assertEqual(plan['statements'][0], a.TAG + 'GRANT CREATE ROUTINE ON `shop`.* TO `app`@`%`')
            plan = self.plan('grant', privileges=['SELECT', 'UPDATE'], level='column', database='d', table='t',
                             columns=['a,b', 'c`d'], grant_option=True)
            self.assertIn('SELECT (`a,b`, `c``d`), UPDATE (`a,b`, `c``d`) ON `d`.`t`', plan['statements'][0])
            kind = 'PACKAGE BODY' if engine == 'mariadb' else 'FUNCTION'
            plan = self.plan('grant', privileges=['EXECUTE'], level='routine', database='d', routine='r', kind=kind)
            self.assertIn('ON ' + kind + ' `d`.`r`', plan['statements'][0])
            plan = self.plan('grant', privileges=['PROXY'], level='proxy', proxy={'name': 'p`', 'host': 'h'})
            self.assertIn('GRANT PROXY ON `p```@`h` TO', plan['statements'][0])
        self.conn = FakeConn('mariadb')
        plan = self.plan('grant', privileges=['REPLICATION CLIENT'])
        self.assertIn('GRANT BINLOG MONITOR ON', plan['statements'][0])

    def test_privilege_allowlist(self):
        for opts in [dict(privileges=['SELECT; DROP USER root']),
                     dict(privileges=['CONNECTION_ADMIN'], level='table', database='d', table='t'),
                     dict(privileges=['EXECUTE'], level='column', database='d', table='t', columns=['x']),
                     dict(privileges=['SELECT'], level='routine', database='d', routine='p'),
                     dict(privileges=['NO_SUCH_DYNAMIC']), dict(privileges=['ALL', 'SELECT']),
                     dict(privileges=['USAGE'])]:
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                self.plan('grant', **opts)
        self.assertIn('BACKUP_ADMIN', self.plan('grant', privileges=['BACKUP_ADMIN'])['statements'][0])
        self.conn = FakeConn('mariadb')
        with self.assertRaises(ValueError):
            self.plan('grant', privileges=['CONNECTION_ADMIN'])
        for password in ('bad\0', 'bad\n', 'bad\r', ''):
            with self.assertRaises(ValueError):
                self.plan('password', password=password)

    def test_revoke_must_name_a_direct_grant(self):
        with self.assertRaisesRegex(ValueError, 'not granted directly'):
            self.plan('revoke', privileges=['UPDATE'])
        with self.assertRaisesRegex(ValueError, 'not granted directly'):
            self.plan('revoke', privileges=['SELECT'], level='table', database='shop', table='orders')
        with self.assertRaisesRegex(ValueError, 'Nothing is granted'):
            self.plan('revoke', privileges=['ALL'], level='database', database='shop')
        with self.assertRaises(ValueError):
            self.plan('roles', mode='revoke', roles=[{'name': 'r', 'host': '%'}])
        self.conn.grants.append('GRANT `r`@`%` TO `app`@`%`')
        self.assertEqual(self.plan('roles', mode='revoke', roles=[{'name': 'r', 'host': '%'}])['statements'],
                         [a.TAG + 'REVOKE `r`@`%` FROM `app`@`%`'])
        self.assertEqual(self.plan('revoke', privileges=['SELECT'])['statements'],
                         [a.TAG + 'REVOKE SELECT ON *.* FROM `app`@`%`'])

    # ---- guards ------------------------------------------------------------------------

    def test_protected_system_and_direct_system_user(self):
        self.conn = FakeConn(name='root', host='localhost')
        plan = self.plan('lock')
        self.assertEqual(plan['guards']['typed_name'], 'root@localhost')
        self.assertEqual(self.apply(plan)['status'], 'needs_typed_name')
        self.assertEqual(self.conn.stdin, [])
        self.conn = FakeConn(name='mysql.sys', host='localhost')
        self.assertEqual(self.plan('password', password='pw')['status'], 'blocked')
        low = actor(grants=['GRANT CREATE USER ON *.* TO admin@localhost'])
        self.conn = FakeConn(grants=['GRANT SYSTEM_USER ON *.* TO `app`@`%`'])
        plan = self.plan('unlock', actor=low)
        self.assertEqual(plan['status'], 'blocked')
        self.assertEqual(plan['privileges']['state'], 'denied')

    def test_system_user_through_a_role_does_not_block(self):
        """Account statements check only a direct SYSTEM_USER grant of the
        target (sql_security_ctx.cc:838); verified live on MySQL 8.0, 8.4
        and 9.7: an account without SYSTEM_USER locked an account that has
        it only through a role, and got 1227 for a direct grant."""
        self.conn.grants.append('GRANT `r`@`%` TO `app`@`%`')
        self.conn.role_grants['`r`@`%`'] = ['GRANT SYSTEM_USER ON *.* TO `r`@`%`']
        low = actor(grants=['GRANT CREATE USER ON *.* TO admin@localhost'])
        plan = self.plan('unlock', actor=low)
        self.assertEqual(plan['guards']['blocked'], [])
        self.assertEqual(plan['privileges']['state'], 'allowed')
        self.assertIn('SYSTEM_USER', plan['before']['dangerous'])
        # A role carrying SYSTEM_USER may be granted; the plan warns.
        plan = self.plan('roles', roles=[{'name': 'r', 'host': '%'}], actor=low)
        self.assertNotEqual(plan['status'], 'blocked')
        self.assertTrue(any('Role r carries' in w for w in plan['warnings']))

    def test_self_lockout_needs_the_typed_name(self):
        self.conn = FakeConn(name='admin', host='localhost')
        plan = self.plan('expire')
        self.assertTrue(plan['guards']['self_lockout'])
        self.assertEqual(plan['guards']['typed_name'], 'admin@localhost')
        self.conn = FakeConn(name='admin', host='localhost', grants=[
            'GRANT CREATE USER ON *.* TO admin@localhost', 'GRANT INSERT, UPDATE, DELETE ON `mysql`.* TO admin@localhost'])
        plan = self.plan('revoke', privileges=['CREATE USER'])
        self.assertFalse(plan['guards']['self_lockout'])  # mysql.* privileges keep the ability
        self.conn.grants.pop()
        plan = self.plan('revoke', privileges=['CREATE USER'])
        self.assertTrue(plan['guards']['self_lockout'])
        result = self.apply(plan)
        self.assertEqual(result['status'], 'needs_typed_name')
        self.assertEqual(self.conn.stdin, [])
        plan = self.plan('revoke', privileges=['CREATE USER'])
        self.assertEqual(self.apply(plan, typed_name='admin@localhost')['status'], 'applied')

    def test_self_revoke_update_from_database_all(self):
        for engine in ('mysql', 'mariadb'):
            self.conn = FakeConn(engine, name='admin', host='localhost', grants=[
                'GRANT USAGE ON *.* TO admin@localhost',
                'GRANT ALL PRIVILEGES ON `mysql`.* TO admin@localhost'])
            plan = self.plan('revoke', privileges=['UPDATE'], level='database', database='mysql')
            self.assertTrue(plan['guards']['self_lockout'])
            self.assertIsNone(plan['guards']['after_management']['account_options'])

    def test_revoke_expands_all_only_at_its_level(self):
        for server in replay.SERVERS:
            conn, _data = replay.reads(server)
            catalog_replay(conn, server)
            before = a.read_account(conn, 'acct_admin')
            after = a._after_revoke(before, ['SELECT'], 'global', {})
            self.assertNotIn('SELECT', after['global'])
            self.assertNotIn('USAGE', after['global'])
            self.assertNotIn('GRANT OPTION', after['global'])
            self.assertFalse(set(after['dynamic']) & p.STATIC)
            self.assertEqual(after['dynamic'], before['model']['dynamic'])
            for level, scope, revoked, remaining in (
                    ('database', '`shop`.*', 'UPDATE', p.DATABASE_PRIVILEGES - {'UPDATE'}),
                    ('table', '`shop`.`orders`', 'UPDATE', p.TABLE_PRIVILEGES - {'UPDATE'}),
                    ('routine', 'PROCEDURE `shop`.`p`', 'EXECUTE', {'ALTER ROUTINE'})):
                with self.subTest(server=server, level=level):
                    fixture = dict(before, grants=['GRANT ALL PRIVILEGES ON ' + scope + ' TO acct_admin@%'],
                                   role_grants=[])
                    fixture['model'] = p.parse_grants(fixture['grants'], before['engine'], before['version'])
                    after = a._after_revoke(fixture, [revoked], level,
                                           dict(database='shop', table='orders', routine='p'))
                    entry = after[{'database': 'schemas', 'table': 'tables', 'routine': 'routines'}[level]][0]
                    expected = set(remaining)
                    if before['engine'] == 'mariadb':
                        if level in ('database', 'table'):
                            expected.add('DELETE HISTORY')
                        if level in ('database', 'routine') and p.version_tuple(before['version']) >= (11, 3, 0):
                            expected.add('SHOW CREATE ROUTINE')
                    self.assertEqual(set(entry['privs']), expected)
                    self.assertFalse(entry['grantable'])

    def test_role_revocation_simulates_reachable_admin_paths(self):
        for engine in ('mysql', 'mariadb'):
            role = '`r`' if engine == 'mariadb' else '`r`@`%`'
            other = '`other`' if engine == 'mariadb' else '`other`@`%`'
            host = '' if engine == 'mariadb' else '%'
            self.conn = FakeConn(engine, name='admin', host='localhost', grants=[
                'GRANT USAGE ON *.* TO admin@localhost', 'GRANT ' + role + ' TO admin@localhost'])
            self.conn.role_grants[role] = ['GRANT CREATE USER, UPDATE ON *.* TO ' + role]
            active = actor(engine, roles=[{'name': 'r', 'host': host}])
            plan = self.plan('roles', mode='revoke', roles=[{'name': 'r', 'host': '%'}], actor=active)
            self.assertTrue(plan['guards']['self_lockout'])
            self.assertIsNone(plan['guards']['after_management']['create_user'])
            # A direct grant keeps administration on both engines.
            self.conn.grants.append('GRANT CREATE USER, UPDATE ON *.* TO admin@localhost')
            plan = self.plan('roles', mode='revoke', roles=[{'name': 'r', 'host': '%'}], actor=active)
            self.assertFalse(plan['guards']['self_lockout'])
            self.assertTrue(plan['guards']['after_management']['create_user'])
            self.conn.grants.pop()
            self.conn.role_grants[other] = ['GRANT CREATE USER, UPDATE ON *.* TO ' + other]
            if engine == 'mysql':  # MariaDB has only one active role root.
                self.conn.grants.append('GRANT ' + other + ' TO admin@localhost')
                active['current_roles'].append({'name': 'other', 'host': host})
                plan = self.plan('roles', mode='revoke', roles=[{'name': 'r', 'host': '%'}], actor=active)
                self.assertFalse(plan['guards']['self_lockout'])
                self.assertTrue(plan['guards']['after_management']['create_user'])
                self.conn.grants.pop()
                active['current_roles'].pop()
            # An inactive role does not keep the current session's admin path.
            self.conn.grants.append('GRANT ' + other + ' TO admin@localhost')
            plan = self.plan('roles', mode='revoke', roles=[{'name': 'r', 'host': '%'}], actor=active)
            self.assertTrue(plan['guards']['self_lockout'])

    def test_drop_nested_role_simulates_all_paths_and_requires_confirmation(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                host = '' if engine == 'mariadb' else '%'
                quote = lambda name: p.quote_account(name, host, True, engine)
                parent, child, other = [quote(name) for name in ('parent', 'child', 'other')]
                self.conn = FakeConn(engine, name='admin', host='localhost', grants=[
                    'GRANT USAGE ON *.* TO admin@localhost', 'GRANT ' + parent + ' TO admin@localhost'])
                self.conn.role_grants[parent] = ['GRANT USAGE ON *.* TO ' + parent,
                                                'GRANT ' + child + ' TO ' + parent]
                # UPDATE on mysql.* is an account-management path, but does
                # not independently mark this role as protected.
                self.conn.role_grants[child] = ['GRANT UPDATE ON `mysql`.* TO ' + child]
                active = actor(engine, roles=[{'name': 'parent', 'host': host}])
                plan = a.plan_change(self.conn, 'drop', 'child', role=True, actor=active)
                self.assertFalse(plan['guards']['protected'])
                self.assertTrue(plan['guards']['self_lockout'])
                self.assertIsNone(plan['guards']['after_management']['account_options'])
                self.assertEqual(plan['guards']['typed_name'], 'child' if engine == 'mariadb' else 'child@%')
                with mock.patch.object(p, 'read_privileges', return_value=active):
                    result = self.apply(plan)
                self.assertEqual(result['status'], 'needs_typed_name')
                self.assertEqual(self.conn.stdin, [])
                # A second incoming edge cannot preserve a dropped vertex.
                self.conn.role_grants[parent].append('GRANT ' + other + ' TO ' + parent)
                self.conn.role_grants[other] = ['GRANT USAGE ON *.* TO ' + other,
                                               'GRANT ' + child + ' TO ' + other]
                plan = a.plan_change(self.conn, 'drop', 'child', role=True, actor=active)
                self.assertTrue(plan['guards']['self_lockout'])
                # An independent grant, however, preserves administration.
                self.conn.role_grants[other].append('GRANT UPDATE ON `mysql`.* TO ' + other)
                plan = a.plan_change(self.conn, 'drop', 'child', role=True, actor=active)
                self.assertFalse(plan['guards']['self_lockout'])
                self.assertTrue(plan['guards']['after_management']['account_options'])
                self.assertEqual(plan['guards']['typed_name'], '')
                # A descendant survives dropping child only when another
                # path reaches the descendant without going through child.
                leaf = quote('leaf')
                self.conn.role_grants[other].pop()
                self.conn.role_grants[child] = ['GRANT USAGE ON *.* TO ' + child,
                                               'GRANT ' + leaf + ' TO ' + child]
                self.conn.role_grants[leaf] = ['GRANT UPDATE ON `mysql`.* TO ' + leaf]
                plan = a.plan_change(self.conn, 'drop', 'child', role=True, actor=active)
                self.assertTrue(plan['guards']['self_lockout'])
                self.conn.role_grants[other].append('GRANT ' + leaf + ' TO ' + other)
                plan = a.plan_change(self.conn, 'drop', 'child', role=True, actor=active)
                self.assertFalse(plan['guards']['self_lockout'])
                # Dropping the active root cuts off all of those paths.
                plan = a.plan_change(self.conn, 'drop', 'parent', role=True, actor=active)
                self.assertTrue(plan['guards']['self_lockout'])
                # Failure to read the actor cannot prove the role harmless.
                with mock.patch.object(a, '_revocation_snapshot', return_value=None):
                    plan = a.plan_change(self.conn, 'drop', 'child', role=True, actor=active)
                self.assertTrue(plan['guards']['self_lockout'])
                self.assertIsNone(plan['guards']['after_management']['account_options'])

    def test_drop_own_mysql_account_as_role_still_loses_login(self):
        # MySQL roles are accounts; the caller can use DROP ROLE for an
        # account it can drop even when that account is its own login.
        self.conn = FakeConn(name='admin', host='localhost', grants=['GRANT USAGE ON *.* TO admin@localhost'])
        plan = self.plan('drop', role=True)
        self.assertTrue(plan['guards']['self_lockout'])
        self.assertEqual(plan['guards']['typed_name'], 'admin@localhost')

    def test_revoke_read_only_bypass_simulates_server_state(self):
        for engine, bypass, alternate in (('mysql', 'CONNECTION_ADMIN', 'SUPER'),
                                           ('mariadb', 'READ_ONLY ADMIN', 'READ_ONLY ADMIN')):
            for from_role in (False, True):
                for read_only in ('ON', 'OFF'):
                    with self.subTest(engine=engine, from_role=from_role, read_only=read_only):
                        host = '' if engine == 'mariadb' else '%'
                        role = p.quote_account('r', host, True, engine)
                        self.conn = FakeConn(engine, name='admin', host='localhost', grants=[
                            'GRANT CREATE USER ON *.* TO admin@localhost',
                            'GRANT UPDATE ON `mysql`.* TO admin@localhost'])
                        target = role if from_role else 'admin@localhost'
                        bypass_line = 'GRANT ' + bypass + ' ON *.* TO ' + target
                        if from_role:
                            self.conn.grants.append('GRANT ' + role + ' TO admin@localhost')
                            self.conn.role_grants[role] = [bypass_line]
                        else:
                            self.conn.grants.append(bypass_line)
                        active = actor(engine, variables={'read_only': read_only},
                                       roles=[{'name': 'r', 'host': host}] if from_role else [])
                        name = 'r' if from_role else 'admin'
                        target_host = '%' if from_role else 'localhost'
                        plan = a.plan_change(self.conn, 'revoke', name, target_host, role=from_role,
                                             actor=active, privileges=[bypass])
                        self.assertEqual(plan['guards']['self_lockout'], read_only == 'ON')
                        self.assertIs(plan['guards']['after_management']['account_options'], read_only == 'OFF')
                        # Keep a bypass through an independent source.
                        if from_role:
                            self.conn.grants.append('GRANT ' + alternate + ' ON *.* TO admin@localhost')
                        else:
                            self.conn.grants.append('GRANT ' + role + ' TO admin@localhost')
                            self.conn.role_grants[role] = ['GRANT ' + alternate + ' ON *.* TO ' + role]
                            active['current_roles'] = [{'name': 'r', 'host': host}]
                        plan = a.plan_change(self.conn, 'revoke', name, target_host, role=from_role,
                                             actor=active, privileges=[bypass])
                        self.assertFalse(plan['guards']['self_lockout'])
                        self.assertTrue(plan['guards']['after_management']['account_options'])

    def test_global_revoke_all_matches_each_engine_and_preserves_roles(self):
        for server in replay.SERVERS:
            conn, _data = replay.reads(server)
            catalog_replay(conn, server)
            before = a.read_account(conn, 'acct_admin')
            for spelling in ('ALL', 'ALL PRIVILEGES'):
                with self.subTest(server=server, spelling=spelling):
                    after = a._after_revoke(before, [spelling], 'global', {})
                    self.assertEqual(after['global'], [])
                    self.assertEqual(after['dynamic'], [])
                    # MySQL routes ALL ON *.* to mysql_revoke_all, including
                    # GRANT OPTION; MariaDB masks GRANT_ACL out at this scope.
                    self.assertIs(p.has(after, 'GRANT OPTION'), before['engine'] == 'mariadb')
                    self.assertEqual(after['global_grantable'], [])
            # MySQL also removes direct object grants/restrictions, but keeps
            # roles and PROXY. MariaDB keeps every other scope unchanged.
            role = '`r`' if before['engine'] == 'mariadb' else '`r`@`%`'
            lines = list(before['grants']) + [
                'GRANT UPDATE ON `mysql`.* TO acct_admin@% WITH GRANT OPTION',
                'GRANT SELECT ON `shop`.`orders` TO acct_admin@%',
                'GRANT SELECT (`a`) ON `shop`.`columns` TO acct_admin@%',
                'GRANT EXECUTE ON PROCEDURE `shop`.`p` TO acct_admin@%',
                'GRANT PROXY ON other@% TO acct_admin@%',
                'GRANT ' + role + ' TO acct_admin@%']
            if before['engine'] == 'mysql':
                lines.append('REVOKE SELECT ON `restricted`.* FROM acct_admin@%')
            before['model'] = p.parse_grants(lines, before['engine'], before['version'], partial_revokes=True)
            before['role_grants'] = ['GRANT CREATE USER ON *.* TO ' + role]
            after = a._after_revoke(before, ['ALL'], 'global', {})
            for key in ('schemas', 'tables', 'columns', 'routines', 'restrictions'):
                self.assertEqual(after[key], before['model'][key] if before['engine'] == 'mariadb' else [])
            self.assertEqual(after['proxies'], before['model']['proxies'])
            self.assertTrue(p.has(after, 'CREATE USER'))

    def test_active_and_nested_role_privilege_revokes(self):
        for engine in ('mysql', 'mariadb'):
            host = '' if engine == 'mariadb' else '%'
            r = '`r`' if engine == 'mariadb' else '`r`@`%`'
            child = '`child`' if engine == 'mariadb' else '`child`@`%`'
            self.conn = FakeConn(engine, name='admin', host='localhost', grants=[
                'GRANT USAGE ON *.* TO admin@localhost', 'GRANT ' + r + ' TO admin@localhost'])
            self.conn.role_grants[r] = ['GRANT CREATE USER, UPDATE ON *.* TO ' + r,
                                       'GRANT ' + child + ' TO ' + r]
            self.conn.role_grants[child] = ['GRANT CREATE USER, UPDATE ON *.* TO ' + child]
            active = actor(engine, roles=[{'name': 'r', 'host': host}])
            # Revoking r's direct CREATE USER preserves its child's grant.
            plan = a.plan_change(self.conn, 'revoke', 'r', role=True, actor=active, privileges=['CREATE USER'])
            self.assertFalse(plan['guards']['self_lockout'])
            self.assertTrue(plan['guards']['after_management']['create_user'])
            # The only remaining admin path is through the nested child.
            self.conn.role_grants[r][0] = 'GRANT USAGE ON *.* TO ' + r
            plan = a.plan_change(self.conn, 'revoke', 'child', role=True, actor=active, privileges=['CREATE USER'])
            self.assertTrue(plan['guards']['self_lockout'])
            self.assertIsNone(plan['guards']['after_management']['create_user'])
            # It is equally destructive to remove the child edge from r.
            plan = a.plan_change(self.conn, 'roles', 'r', role=True, actor=active, mode='revoke',
                                 roles=[{'name': 'child', 'host': '%'}])
            self.assertTrue(plan['guards']['self_lockout'])
            self.assertIsNone(plan['guards']['after_management']['create_user'])
            # Another path inside the active root reaches the same child.
            # MariaDB still has just one active root, r.
            other = '`other`' if engine == 'mariadb' else '`other`@`%`'
            self.conn.role_grants[r].append('GRANT ' + other + ' TO ' + r)
            self.conn.role_grants[other] = ['GRANT USAGE ON *.* TO ' + other,
                                           'GRANT ' + child + ' TO ' + other]
            plan = a.plan_change(self.conn, 'roles', 'r', role=True, actor=active, mode='revoke',
                                 roles=[{'name': 'child', 'host': '%'}])
            self.assertFalse(plan['guards']['self_lockout'])
            self.assertTrue(plan['guards']['after_management']['create_user'])

    def test_public_admin_revoke_and_independent_user_grant(self):
        self.conn = FakeConn('mariadb', name='admin', host='localhost', grants=[
            'GRANT USAGE ON *.* TO admin@localhost'])
        public = 'GRANT CREATE USER, UPDATE ON *.* TO PUBLIC'
        self.conn.role_grants['PUBLIC'] = [public]
        active = actor('mariadb', grants=self.conn.grants + [public])
        plan = a.plan_change(self.conn, 'revoke', 'PUBLIC', role=True, actor=active, privileges=['CREATE USER'])
        self.assertTrue(plan['guards']['self_lockout'])
        self.assertIsNone(plan['guards']['after_management']['create_user'])
        self.conn.grants.append('GRANT CREATE USER ON *.* TO admin@localhost')
        plan = a.plan_change(self.conn, 'revoke', 'PUBLIC', role=True, actor=active, privileges=['CREATE USER'])
        self.assertFalse(plan['guards']['self_lockout'])
        self.assertTrue(plan['guards']['after_management']['create_user'])
        # PUBLIC can also preserve administration when a direct grant is lost.
        self.conn.grants.append('GRANT UPDATE ON *.* TO admin@localhost')
        active = actor('mariadb', grants=self.conn.grants + [public])
        plan = self.plan('revoke', actor=active, privileges=['CREATE USER'])
        self.assertFalse(plan['guards']['self_lockout'])

    def test_role_apply_rechecks_independent_admin_path(self):
        self.conn = FakeConn(name='admin', host='localhost', grants=[
            'GRANT `r`@`%` TO admin@localhost', 'GRANT `other`@`%` TO admin@localhost'])
        self.conn.role_grants['`r`@`%`'] = ['GRANT CREATE USER ON *.* TO `r`@`%`']
        self.conn.role_grants['`other`@`%`'] = ['GRANT CREATE USER ON *.* TO `other`@`%`']
        active = actor(roles=[{'name': 'r', 'host': '%'}, {'name': 'other', 'host': '%'}])
        plan = a.plan_change(self.conn, 'revoke', 'r', role=True, actor=active, privileges=['CREATE USER'])
        self.assertFalse(plan['guards']['self_lockout'])
        self.conn.role_grants['`other`@`%`'] = ['GRANT USAGE ON *.* TO `other`@`%`']
        # Protected roles already require their own name, so assert the fresh
        # guard itself as well as the apply refusal before any write.
        with mock.patch.object(p, 'read_privileges', return_value=active):
            result = self.apply(plan)
        self.assertEqual(result['status'], 'needs_typed_name')
        self.assertEqual(self.conn.stdin, [])
        fresh = a.read_account(self.conn, 'r', role=True)
        self.assertTrue(a._guards(fresh, active, 'revoke', ['CREATE USER'], conn=self.conn)['self_lockout'])

    def test_role_simulation_keeps_unknown_and_mandatory_paths(self):
        self.conn = FakeConn(name='admin', host='localhost', grants=[
            'GRANT CREATE USER ON *.* TO admin@localhost', 'GRANT `r`@`%` TO admin@localhost'])
        before = a.read_account(self.conn, 'admin', 'localhost')
        before['role_grants'].append('FUTURE privilege syntax')
        active = actor(roles=[{'name': 'r', 'host': '%'}])
        guards = a._guards(before, active, 'revoke', ['CREATE USER'])
        self.assertTrue(guards['self_lockout'])
        self.assertIsNone(guards['after_management']['create_user'])
        # Missing role evidence must also stay unknown, not become absence.
        before['role_grants'] = []
        guards = a._guards(before, active, 'revoke', ['CREATE USER'])
        self.assertTrue(guards['self_lockout'])
        self.assertIsNone(guards['after_management']['create_user'])
        self.conn.role_grants['`r`@`%`'] = ['GRANT CREATE USER ON *.* TO `r`@`%`']
        self.conn.grants[0] = 'GRANT USAGE ON *.* TO admin@localhost'
        before = a.read_account(self.conn, 'admin', 'localhost')
        active['roles'] = [{'name': 'r', 'host': '%', 'mandatory': True}]
        # The server refuses to revoke a mandatory role. The hypothetical
        # removal of its direct edge must not erase its mandatory root.
        guards = a._guards(before, active, 'revoke_roles', options={'roles': [{'name': 'r', 'host': '%'}]})
        self.assertFalse(guards['self_lockout'])
        self.assertTrue(guards['after_management']['create_user'])

    def test_harmless_role_revocation_is_not_self_lockout(self):
        for server in replay.SERVERS:
            conn, data = replay.reads(server)
            catalog_replay(conn, server)
            before = a.read_account(conn, 'acct_app')
            # setUp patches read_privileges, so use the unpatched recorded
            # session lines and explicit current role below.
            lines = [line for call in data['sessions']['acct_app']['calls']
                     for line in call.get('output', '').splitlines() if line.startswith('GRANT ')]
            active = actor(before['engine'], 'acct_app@%', lines,
                           roles=[{'name': 'r_read', 'host': '' if before['engine'] == 'mariadb' else '%'}])
            guards = a._guards(before, active, 'revoke_roles', options={'roles': [{'name': 'r_write', 'host': '%'}]})
            self.assertFalse(guards['self_lockout'])
            self.assertTrue(guards['after_management'])

    def test_mariadb_public_can_be_read_and_granted_but_not_assigned(self):
        self.conn = FakeConn('mariadb', name='PUBLIC')
        before = a.read_account(self.conn, 'PUBLIC', role=True)
        self.assertTrue(before['exists'])
        plan = self.plan('grant', role=True, privileges=['PROCESS'])
        self.assertTrue(plan['statements'][0].endswith('TO PUBLIC'))
        with self.assertRaisesRegex(ValueError, 'cannot be'):
            self.plan('drop', role=True)
        self.conn = FakeConn('mariadb')
        with self.assertRaisesRegex(ValueError, 'PUBLIC cannot'):
            self.plan('roles', roles=[{'name': 'PUBLIC'}])

    # ---- apply -------------------------------------------------------------------------

    def test_apply_accepts_empty_form_tls_fields_but_rejects_changed_options(self):
        connection = dict(a._connection(self.conn), ssl_mode='', ssl_ca='', port='3306')
        plan = self.plan('grant', privileges=['PROCESS'], connection=connection)
        result = self.apply(plan, connection=connection)
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(len(self.conn.stdin), 1)
        for field, value in (('host', 'other'), ('port', 3307), ('ssl_mode', 'DISABLED'),
                             ('ssl_ca', '/other/ca.pem'), ('user', 'other')):
            with self.subTest(field=field):
                self.conn = FakeConn()
                plan = self.plan('grant', privileges=['PROCESS'], connection=connection)
                setattr(self.conn, field, value)
                result = self.apply(plan, connection=connection)
                self.assertEqual(result['status'], 'stale')
                self.assertEqual(self.conn.stdin, [])

    def test_apply_policy_first_and_audited(self):
        plan = self.plan('lock')
        self.conn.calls = []
        self.assertEqual(a.apply(self.conn, plan, plan['expect'], store=self.store)['status'], 'needs_confirmation')
        self.assertEqual(self.conn.calls, [])
        plan = self.plan('lock')
        self.conn.calls = []
        result = a.apply(self.conn, plan, plan['expect'], confirmed=True, store=self.store,
                         connection={'read_only': True})
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(self.conn.calls, [])
        self.assertEqual(self.conn.stdin, [])
        self.assertEqual({r['status'] for r in self.store.audit_records()}, {'declined', 'refused'})

    def test_apply_refuses_stale_state_and_tampering(self):
        for mutation in ('grants', 'attributes', 'expect', 'server', 'connection', 'tamper'):
            with self.subTest(mutation=mutation):
                self.conn = FakeConn()
                plan = self.plan('lock')
                expect = plan['expect']
                if mutation == 'grants':
                    self.conn.grants.append('GRANT PROCESS ON *.* TO `app`@`%`')
                if mutation == 'attributes':
                    self.conn.locked = True
                if mutation == 'expect':
                    expect = 'untrusted'
                if mutation == 'server':
                    self.conn.version = '8.4.12'
                if mutation == 'connection':
                    self.conn.host = 'elsewhere'
                if mutation == 'tamper':
                    plan['statements'][0] += '; DROP USER root'
                    with self.assertRaises(ValueError):
                        a.apply(self.conn, plan, expect, confirmed=True, store=self.store)
                else:
                    result = a.apply(self.conn, plan, expect, confirmed=True, store=self.store)
                    self.assertEqual(result['status'], 'stale')
                    self.assertIn('nothing was sent', result['message'])
                self.assertEqual(self.conn.stdin, [])

    def test_apply_rechecks_actor_and_rename_destination(self):
        plan = self.plan('rename', new_name='other', new_host='%')
        self.conn.destination_exists = True
        self.assertEqual(self.apply(plan)['status'], 'stale')
        self.conn = FakeConn(grants=['GRANT SYSTEM_USER ON *.* TO `app`@`%`'])
        plan = self.plan('lock')
        with mock.patch.object(p, 'read_privileges', return_value=actor(grants=['GRANT CREATE USER ON *.* TO admin@localhost'])):
            # The re-read actor lacks SYSTEM_USER and the target holds it directly.
            self.assertEqual(self.apply(plan)['status'], 'refused')
        self.assertEqual(self.conn.stdin, [])

    def test_apply_success_diff_audit_single_use(self):
        plan = self.plan('grant', privileges=['PROCESS'])

        def execute(sql, extra_flags=None, timeout=None):
            self.assertEqual(self.store.audit_records()[0]['status'], 'pending')
            self.assertEqual(extra_flags, list(dbops.WRITE_CLIENT_FLAGS))
            self.conn.grants.append('GRANT PROCESS ON *.* TO `app`@`%`')
            return ''
        self.conn.run_stdin = execute
        result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['diff']['added'], ['GRANT PROCESS ON *.* TO `app`@`%`'])
        self.assertEqual(result['diff']['levels']['global']['added'], ['PROCESS'])
        with self.assertRaises(ValueError):
            self.apply(plan)
        records = self.store.audit_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['status'], 'ok')
        self.assertEqual(records[0]['target']['change_set_id'], plan['change_set_id'])

    def test_passwords_never_leave_memory(self):
        for engine in ('mysql', 'mariadb'):
            self.conn = FakeConn(engine)
            plan = self.plan('password', password="Quo'te\\secret")
            self.assertNotIn('secret', repr(plan))
            self.assertNotIn('secret', json.dumps(plan))
            self.conn.run_stdin = mock.Mock(side_effect=ConnectorError(
                "mysql client exited 1: ERROR 1064 (42000) at line 3: syntax error near 'Quo''te\\secret'"))
            result = self.apply(plan)
            self.assertEqual(result['status'], 'refused_by_server')
            self.assertNotIn('secret', json.dumps(result))
            rows = json.dumps(self.store.audit_records())
            self.assertNotIn('secret', rows)
            self.assertNotIn('48415348534543524554', rows)
            self.assertFalse(plan._passwords)
            self.assertFalse(plan._statements)

    def test_safe_server_messages_are_kept(self):
        self.conn = FakeConn('mysql')
        plan = self.plan('drop')
        self.conn.run_stdin = mock.Mock(side_effect=ConnectorError(
            "mysql client exited 1: ERROR 4006 (HY000) at line 1: Operation DROP USER failed for 'app'@'%' as it is "
            "referenced as a definer account in a view."))
        result = self.apply(plan)
        self.assertEqual(result['status'], 'refused_by_server')
        self.assertIn('referenced as a definer', result['message'])

    def test_mariadb_partial_create_keeps_the_account_locked(self):
        self.conn = FakeConn('mariadb', exists=False)
        plan = self.plan('create', password='s3cret')
        calls = []

        def execute(sql, extra_flags=None, timeout=None):
            calls.append(sql)
            if len(calls) == 1:
                self.conn.exists, self.conn.locked = True, True
                return ''
            raise ConnectorError('mysql client exited 1: ERROR 1819 (HY000) at line 3: Your password does not '
                                 'satisfy the current policy requirements')
        self.conn.run_stdin = execute
        result = self.apply(plan)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['after']['attributes']['locked'])
        self.assertEqual([s['status'] for s in result['statements']], ['ok', 'refused_by_server', 'not_sent'])
        self.assertTrue(any('stays locked' in w for w in result['warnings']))
        records = self.store.audit_records()
        self.assertEqual(len(records), 2)
        self.assertEqual({r['target']['change_set_id'] for r in records}, {plan['change_set_id']})
        self.assertNotIn('s3cret', json.dumps(records))

    def test_generated_password_returned_once_not_audited(self):
        for engine in ('mysql', 'mariadb'):
            self.conn = FakeConn(engine)
            plan = self.plan('password', random_password=True)
            generated = plan._generated if engine == 'mariadb' else 'server-generated-SECRET'
            self.conn.run_stdin = mock.Mock(return_value='app\t%\t' + generated + '\t1\n')
            result = self.apply(plan)
            self.assertEqual(result['generated_password'], generated)
            self.assertNotIn(generated, json.dumps(self.store.audit_records()))
            self.assertIsNone(plan._generated)

    def test_timeout_is_unknown_and_audit_failure_sends_nothing(self):
        plan = self.plan('lock')
        self.conn.run_stdin = mock.Mock(side_effect=ConnectorError('Query timed out after 30s; the outcome is unknown.'))
        self.assertEqual(self.apply(plan)['status'], 'unknown')
        self.assertEqual(self.store.audit_records()[0]['status'], 'unknown')
        plan = self.plan('lock')
        self.conn.run_stdin = mock.Mock()
        with mock.patch.object(self.store, 'audit_insert', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.apply(plan)
        self.conn.run_stdin.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'audit store'):
            a.apply(self.conn, self.plan('lock'), 'anything')

    def test_role_change_since_review_is_stale(self):
        plan = self.plan('roles', roles=[{'name': 'r', 'host': '%'}])
        self.assertEqual(len(plan['dependencies']), 1)
        self.conn.role_grants['`r`@`%`'] = ['GRANT SYSTEM_USER ON *.* TO `r`@`%`']
        self.assertEqual(self.apply(plan)['status'], 'stale')
        self.assertEqual(self.conn.stdin, [])

    def test_after_audit_failure_does_not_hide_success(self):
        plan = self.plan('unlock')
        with mock.patch.object(self.store, 'audit_complete', side_effect=OSError('disk full')):
            result = self.apply(plan)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.store.audit_records()[0]['status'], 'pending')

    # ---- transport ---------------------------------------------------------------------

    def test_oversized_write_response_is_audited_as_unknown(self):
        # A stopped client does not prove its DDL was rolled back. Exercise
        # the real transport and accounts' error mapping together.
        from myflames import connector
        real_popen = subprocess.Popen
        children = []

        def launch(argv, **kwargs):
            proc = real_popen([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x" * 8192)'], **kwargs)
            children.append(proc)
            return proc

        with MySQLConnection('h', binary='mysql') as transport, \
                mock.patch.object(subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(connector, 'MAX_STDOUT_BYTES', 1024, create=True):
            self.conn.run_stdin = transport.run_stdin
            result = self.apply(self.plan('lock'))
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.store.audit_records()[0]['status'], 'unknown')
        self.assertIsNotNone(children[0].poll())

    def test_stdin_transport_keeps_sql_and_passwords_out_of_argv(self):
        """A real subprocess (a fake client script) records argv, stdin, the
        option file mode and the environment."""
        log = Path(self.temp.name) / 'transport.json'
        client = Path(self.temp.name) / 'fake-client'
        client.write_text('#!' + sys.executable + '\n'
                          'import sys, os, json, stat\n'
                          'path = sys.argv[1].split("=", 1)[1]\n'
                          'out = {"argv": sys.argv, "stdin": sys.stdin.read(), "mode": stat.S_IMODE(os.stat(path).st_mode),'
                          ' "env": dict(os.environ)}\n'
                          'open(' + repr(str(log)) + ', "w").write(json.dumps(out))\n')
        client.chmod(0o700)
        conn = MySQLConnection('127.0.0.1', user='admin', password='connection-secret', binary=str(client))
        self.addCleanup(conn.close)
        password = "x\\'; DROP USER root; --"
        sql = a._with_password_mode('ALTER USER `app`@`%` IDENTIFIED BY ' + a._password(password))
        with mock.patch.dict(os.environ, {'MYSQL_PWD': 'must-not-inherit', 'MYSQL_HOST': 'must-not-inherit'}):
            a._run_stdin(conn, sql, [password])
        data = json.loads(log.read_text(encoding="utf-8"))
        self.assertEqual(data['mode'], 0o600)
        self.assertTrue(all(password not in arg and 'connection-secret' not in arg and 'ALTER USER' not in arg
                            for arg in data['argv']))
        self.assertNotIn('-e', data['argv'])
        for flag in dbops.WRITE_CLIENT_FLAGS + ('--skip-reconnect',):
            self.assertIn(flag, data['argv'])
        self.assertNotIn('MYSQL_PWD', data['env'])
        self.assertNotIn('MYSQL_HOST', data['env'])
        self.assertNotEqual(data['env'].get('HOME'), os.environ.get('HOME'))
        self.assertIn('NO_BACKSLASH_ESCAPES', data['stdin'])
        statements = dbops.split_statements(data['stdin'], backslash_escapes=False)
        self.assertEqual(len(statements), 4)
        self.assertFalse(any('DROP USER' in s['code'] for s in statements))

    def test_transport_errors_never_echo_credentials(self):
        conn = MySQLConnection('127.0.0.1', password='connection-secret', binary='/fake/mysql')
        self.addCleanup(conn.close)
        failure = subprocess.CompletedProcess([], 1, b'', b"ERROR 1064 (42000) near 'new-secret' AS 0x414243")
        with mock.patch('myflames.connector._run_bounded', return_value=failure):
            with self.assertRaises(ConnectorError) as cm:
                a._run_stdin(conn, "ALTER USER a IDENTIFIED BY 'new-secret'", ['new-secret'])
        self.assertEqual(str(cm.exception), 'ERROR 1064: the server refused the statement.')
        failure = subprocess.CompletedProcess([], 1, b'', b"ERROR 1819 (HY000): password 'new-secret' rejected")
        with mock.patch('myflames.connector._run_bounded', return_value=failure):
            with self.assertRaises(ConnectorError) as cm:
                a._run_stdin(conn, "ALTER USER a IDENTIFIED BY 'new-secret'", ['new-secret'])
        self.assertNotIn('new-secret', str(cm.exception))
        self.assertIn('1819', str(cm.exception))
        with mock.patch('myflames.connector._run_bounded', side_effect=subprocess.TimeoutExpired(['mysql'], 30,
                                                                                         output=b'new-secret')):
            with self.assertRaisesRegex(ConnectorError, 'outcome unknown') as cm:
                a._run_stdin(conn, 'new-secret')
            self.assertNotIn('new-secret', str(cm.exception))

    def test_python37_syntax(self):
        for path in (Path(a.__file__), Path(__file__)):
            ast.parse(path.read_text(encoding="utf-8"), **({'feature_version': 7} if sys.version_info >= (3, 8) else {}))



class GrantEditorTest(unittest.TestCase):
    setUp = AccountsTest.setUp
    plan = AccountsTest.plan
    def test_live_catalog_and_visible_scopes_do_not_hide_held_scopes(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                conn, data = replay.reads(server)
                catalog_replay(conn, server)
                low = json.loads((replay.FIXTURES / ('show-privileges-' + server + '.json')).read_text(encoding="utf-8"))
                for call in low['sessions']['acct_low']['calls']:
                    conn.answers[call['sql']] = call
                account = a.read_account(conn, 'acct_app')
                self.assertEqual(account['privilege_catalog_meta']['source'], 'server')
                scopes = account['grant_scopes']
                self.assertEqual(len([s for s in scopes if s['level'] == 'global']), 1)
                dbs = [s['database'] for s in scopes if s['level'] == 'database']
                self.assertIn('shop', dbs)
                self.assertNotIn('information_schema', dbs)
                self.assertNotIn('performance_schema', dbs)
                # Existing grants from the older account capture are retained,
                # even though the separate low-privilege catalog cannot see them.
                for level, bucket in [('database', 'schemas'), ('table', 'tables'), ('column', 'columns'), ('routine', 'routines')]:
                    for entry in account['model'][bucket]:
                        self.assertTrue(any(s['level'] == level and s.get('database') in
                                            (entry['database'], entry.get('pattern')) for s in scopes))
                self.assertEqual(len({s['id'] for s in scopes}), len(scopes))

    def test_complete_scopes_direct_inherited_patterns_columns_proxy_restrictions(self):
        self.conn.grants = [
            'GRANT SELECT ON *.* TO `app`@`%` WITH GRANT OPTION',
            'GRANT CREATE ROUTINE ON `db\\_x`.* TO `app`@`%`',
            'GRANT UPDATE ON `wild_%`.* TO `app`@`%`',
            'GRANT UPDATE ON `shop`.`orders` TO `app`@`%`',
            'GRANT SELECT (`b`, `a`) ON `hidden`.`orders` TO `app`@`%`',
            'GRANT EXECUTE ON FUNCTION `shop`.`f` TO `app`@`%`',
            'GRANT PROXY ON `proxied`@`localhost` TO `app`@`%` WITH GRANT OPTION',
            'GRANT `r`@`%` TO `app`@`%`',
            'REVOKE SELECT ON `restricted`.* FROM `app`@`%`',
        ]
        self.conn.role_grants['`r`@`%`'] = ['GRANT INSERT ON `role_db`.* TO `r`@`%`',
                                          'GRANT EXECUTE ON PROCEDURE `shop`.`p` TO `r`@`%`']
        account = a.read_account(self.conn, 'app')
        scopes = account['grant_scopes']
        find = lambda level, **target: next(s for s in scopes if s['level'] == level and all(s.get(k) == v for k, v in target.items()))
        self.assertFalse(find('database', database='db_x')['pattern'])
        self.assertTrue(find('database', database='wild_%')['pattern'])
        self.assertEqual(find('column', database='hidden')['columns'], ['a', 'b'])
        self.assertEqual(find('routine', routine='f')['kind'], 'FUNCTION')
        self.assertEqual(find('proxy')['proxy'], {'name': 'proxied', 'host': 'localhost'})
        self.assertTrue(find('proxy')['cells'][0]['grantable'])
        role_cell = next(c for c in find('database', database='role_db')['cells'] if c['privilege'] == 'INSERT')
        self.assertFalse(role_cell['direct'])
        self.assertIn('role ', role_cell['via'])
        restricted = next(c for c in find('database', database='restricted')['cells'] if c['privilege'] == 'SELECT')
        self.assertTrue(restricted['restricted'])
        self.assertFalse(restricted['direct'])
        global_select = next(c for c in find('global')['cells'] if c['privilege'] == 'SELECT')
        self.assertTrue(global_select['direct'])
        self.assertTrue(global_select['grantable'])
        self.assertTrue(find('database', database='mysql')['system'])

    def test_mariadb_role_tree_never_marks_inherited_privileges_direct(self):
        conn = FakeConn('mariadb', name='parent')
        conn.grants = ['GRANT SELECT ON *.* TO `parent`',
                       'GRANT `r` TO `parent`',
                       'GRANT INSERT ON *.* TO `r`',
                       'GRANT EXECUTE ON PROCEDURE `shop`.`p` TO `r`']
        conn.role_grants['`r`'] = conn.grants[2:]
        result = a.read_account(conn, 'parent', role=True)
        global_scope = next(s for s in result['grant_scopes'] if s['level'] == 'global')
        cells = {c['privilege']: c for c in global_scope['cells']}
        self.assertTrue(cells['SELECT']['direct'])
        self.assertFalse(cells['INSERT']['direct'])
        self.assertEqual(cells['INSERT']['via'], 'role `r`')
        routine = next(s for s in result['grant_scopes'] if s['level'] == 'routine')
        self.assertFalse(routine['cells'][0]['direct'])

    def test_mariadb_inherited_role_grants_are_not_direct_revoke_candidates(self):
        self.conn = FakeConn('mariadb', name='parent')
        inherited = ['GRANT CREATE USER ON *.* TO `r` WITH GRANT OPTION',
                     'GRANT INSERT ON `shop`.* TO `r` WITH GRANT OPTION',
                     'GRANT EXECUTE ON PROCEDURE `shop`.`p` TO `r`']
        self.conn.grants = ['GRANT SELECT ON *.* TO `parent`', 'GRANT `r` TO `parent`'] + inherited
        self.conn.role_grants['`r`'] = inherited
        before = a.read_account(self.conn, 'parent', role=True)
        self.assertEqual(before['model']['global'], ['SELECT'])
        self.assertFalse(before['model']['static_grant_option'])
        self.assertEqual(before['model']['schemas'], [])
        self.assertTrue(p.has(before['effective_model'], 'CREATE USER'))
        for level, name, options in [('global', 'CREATE USER', {}), ('global', 'GRANT OPTION', {}),
                                      ('database', 'INSERT', {'database': 'shop'}),
                                      ('routine', 'EXECUTE', {'database': 'shop', 'routine': 'p'})]:
            with self.subTest(level=level, privilege=name):
                with self.assertRaisesRegex(ValueError, 'not granted directly'):
                    self.plan('revoke', role=True, privileges=[name], level=level, **options)
                self.assertFalse(self.conn.stdin)

    def test_mariadb_role_self_guard_simulation_removes_only_direct_contributions(self):
        conn = FakeConn('mariadb', name='parent')
        child = ['GRANT CREATE USER ON *.* TO `r` WITH GRANT OPTION']
        conn.grants = ['GRANT SELECT ON *.* TO `parent`', 'GRANT `r` TO `parent`'] + child
        conn.role_grants['`r`'] = child
        before = a.read_account(conn, 'parent', role=True)
        after_direct = a._after_revoke(before, ['ALL'], 'global', {})
        self.assertTrue(p.has(after_direct, 'CREATE USER'))
        self.assertTrue(p.has(after_direct, 'GRANT OPTION'))
        after_edge = a._after_revoke(before, [], 'global', {'roles': [{'name': 'r', 'host': ''}]},
                                     operation='revoke_roles')
        self.assertFalse(p.has(after_edge, 'CREATE USER'))
        self.assertFalse(p.has(after_edge, 'GRANT OPTION'))

    def test_empty_account_has_empty_global_and_visible_database_scopes(self):
        self.conn.grants = ['GRANT USAGE ON *.* TO `app`@`%`']
        out = a.read_account(self.conn, 'app')
        self.assertEqual({s['level'] for s in out['grant_scopes']}, {'global', 'database'})
        self.assertTrue(all(not s['cells'] for s in out['grant_scopes']))
        self.assertTrue(out['privilege_catalog']['database'])

    def test_catalog_unreadable_falls_back_and_keeps_held_scopes(self):
        original = self.conn.run
        def run(sql, **kwargs):
            if sql in (a.TAG + 'SHOW PRIVILEGES', a.TAG + 'SHOW DATABASES'):
                raise ConnectorError('ERROR 1227: secret-password')
            return original(sql, **kwargs)
        self.conn.run = run
        out = a.read_account(self.conn, 'app')
        self.assertEqual(out['privilege_catalog_meta']['source'], 'static')
        self.assertNotIn('secret-password', json.dumps(out))
        self.assertTrue(out['warnings'])
        plan = self.plan('grant', privileges=['FLUSH_TABLES'])
        self.assertIn('GRANT FLUSH_TABLES ON *.*', plan['statements'][0])

    def test_plan_every_level_both_engines_and_role_target(self):
        cases = [('global', ['SELECT'], {}, '*.*'),
                 ('database', ['CREATE ROUTINE'], {'database': 'shop'}, '`shop`.*'),
                 ('table', ['TRIGGER'], {'database': 'shop', 'table': 'orders'}, '`shop`.`orders`'),
                 ('column', ['UPDATE'], {'database': 'shop', 'table': 'orders', 'columns': ['c']}, '`shop`.`orders`'),
                 ('routine', ['EXECUTE'], {'database': 'shop', 'routine': 'p'}, 'PROCEDURE `shop`.`p`'),
                 ('proxy', ['PROXY'], {'proxy': {'name': 'other', 'host': 'localhost'}}, '`other`@`localhost`')]
        for engine in ('mysql', 'mariadb'):
            self.conn = FakeConn(engine)
            for level, names, options, scope in cases:
                with self.subTest(engine=engine, level=level):
                    plan = self.plan('grant', level=level, privileges=names, **options)
                    self.assertIn(' ON ' + scope + ' TO `app`@`%`', plan['statements'][0])
                    self.assertNotIn('privilege_catalog', plan['before'])
                    self.assertFalse(self.conn.stdin)
            plan = self.plan('grant', role=True, level='database', privileges=['CREATE ROUTINE'], database='shop')
            self.assertTrue(plan['statements'][0].endswith('TO `app`' if engine == 'mariadb' else 'TO `app`@`%`'))

    def test_live_dynamic_names_and_invalid_levels_before_write(self):
        for server in replay.SERVERS:
            data = json.loads((replay.FIXTURES / ('show-privileges-' + server + '.json')).read_text(encoding="utf-8"))
            self.conn = FakeConn(data['server']['engine']); self.conn.version = data['server']['version']
            original = self.conn.run
            output = next(c['output'] for c in data['sessions']['root']['calls'] if c['sql'] == a.TAG + 'SHOW PRIVILEGES')
            self.conn.run = lambda sql, output=output, original=original, **kw: output if sql == a.TAG + 'SHOW PRIVILEGES' else original(sql, **kw)
            names = ['READ_ONLY ADMIN'] if self.conn.engine == 'mariadb' else ['FLUSH_TABLES', 'INNODB_REDO_LOG_ARCHIVE']
            for name in names:
                plan = self.plan('grant', privileges=[name])
                self.assertIn('GRANT ' + name + ' ON *.*', plan['statements'][0])
            for level, name, options in [('routine', 'CREATE ROUTINE', {'database': 'shop', 'routine': 'p'}),
                                          ('table', 'EVENT', {'database': 'shop', 'table': 't'}),
                                          ('column', 'DELETE', {'database': 'shop', 'table': 't', 'columns': ['c']}),
                                          ('database', names[0], {'database': 'shop'}),
                                          ('invalid', 'ALL', {})]:
                with self.subTest(server=server, level=level, name=name):
                    with self.assertRaises(ValueError):
                        self.plan('grant', level=level, privileges=[name], **options)
                    self.assertFalse(self.conn.stdin)

    def test_scope_objects_bound_and_quoted_schema(self):
        calls = []
        def run(sql, **kwargs):
            calls.append(sql)
            if 'information_schema.TABLES' in sql:
                return ''.join('table%d\n' % n for n in range(490))
            return ''.join('p%d\tPROCEDURE\n' % n for n in range(20)) + 'f\tFUNCTION\n'
        self.conn.run = run
        out = a.scope_objects(self.conn, "s'\\_x")
        self.assertEqual(sum(len(out[k]) for k in ('tables', 'procedures', 'functions')), 500)
        self.assertTrue(out['truncated'])
        self.assertTrue(all('LIMIT 501' in sql for sql in calls))
        self.assertTrue(all(a.literal("s'\\_x") in sql for sql in calls))
        self.assertTrue(all("s'\\_x" not in sql for sql in calls))
        with self.assertRaises(ValueError):
            a.scope_objects(self.conn, 'information_schema')

    def test_scope_objects_partial_failure_and_functions(self):
        def run(sql, **kwargs):
            if 'information_schema.TABLES' in sql:
                raise ConnectorError('secret-credential')
            return 'p\tPROCEDURE\nf\tFUNCTION\n'
        self.conn.run = run
        out = a.scope_objects(self.conn, 'shop')
        self.assertEqual(out['procedures'], [{'name': 'p'}])
        self.assertEqual(out['functions'], [{'name': 'f'}])
        self.assertFalse(out['truncated'])
        self.assertNotIn('secret-credential', json.dumps(out))
        self.assertTrue(out['warnings'])


if __name__ == '__main__':
    unittest.main()
