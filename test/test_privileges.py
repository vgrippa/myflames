"""privileges.py: SHOW GRANTS model, has/can rules and the privileges-1.0
report, on live recordings (test/fixtures/accounts, see PROVENANCE.txt) and
on hand-written lines for the edges a recording cannot hold."""
import ast
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parent))
import _accounts_replay as replay  # noqa: E402

from myflames import dbops, privileges as p  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402

LOGINS = {'mysql': ('acct_admin', 'acct_ops', 'acct_app', 'acct_pr', 'acct_cu'),
          'mariadb': ('acct_admin', 'acct_ops', 'acct_app', 'acct_super')}


def report(server, login):
    conn, data = replay.session(server, login)
    return p.read_privileges(conn), data, conn


class RecordedSessions(unittest.TestCase):
    """read_privileges on what five live servers returned."""

    def each(self):
        for server in replay.SERVERS:
            engine = 'mariadb' if server.startswith('mariadb') else 'mysql'
            for login in LOGINS[engine]:
                if server == 'mariadb-11.4' and login == 'acct_super':
                    continue  # expired password: see test_expired_account
                yield server, engine, login

    def test_every_session_parses_without_credentials(self):
        for server, engine, login in self.each():
            with self.subTest(server=server, login=login):
                r, data, conn = report(server, login)
                self.assertEqual(r['schema_version'], 'privileges-1.0')
                self.assertEqual(r['account'], login + '@%')
                self.assertEqual(r['engine'], engine)
                self.assertEqual(r['unparsed'], [])
                self.assertTrue(r['checked'])
                text = json.dumps(r, allow_nan=False)
                for secret in replay.secrets(data):
                    self.assertNotIn(secret, text)
                self.assertNotIn('IDENTIFIED', text)
                self.assertEqual(set(r['actions']), set(p.ACTIONS))
                for key, answer in r['actions'].items():
                    self.assertIn(answer['state'], ('allowed', 'denied', 'unknown'), key)
                    self.assertIsInstance(answer['missing'], list)
                    self.assertTrue(answer['reason'])
                    if answer['state'] == 'allowed':
                        self.assertEqual(answer['missing'], [])
                # One tagged call for the grants (+ APPLICABLE_ROLES on MySQL); never mysql.* or SHOW CREATE.
                self.assertTrue(all(sql.startswith(p.TAG) for sql, _ in conn.sent))
                self.assertFalse(any('mysql.' in sql or 'SHOW CREATE' in sql for sql, _ in conn.sent))
                self.assertLessEqual(len(conn.sent), 2)

    def test_admin_ops_and_plain_accounts(self):
        for server, engine, login in self.each():
            r, _data, _conn = report(server, login)
            a = r['actions']
            with self.subTest(server=server, login=login):
                if login == 'acct_admin':
                    self.assertEqual(a['create_user']['state'], 'allowed')
                    self.assertEqual(a['kill']['state'], 'allowed')
                    self.assertEqual(a['innodb_status']['state'], 'allowed')
                elif login == 'acct_ops':
                    self.assertEqual(a['create_user']['state'], 'unknown' if engine == 'mariadb' else 'allowed')
                    # MySQL: CONNECTION_ADMIN without SYSTEM_USER cannot end a SYSTEM_USER session.
                    self.assertEqual(a['kill']['state'], 'allowed' if engine == 'mariadb' else 'unknown')
                    self.assertEqual(a['set_password']['state'], 'unknown' if engine == 'mariadb' else 'allowed')
                elif login == 'acct_app':
                    self.assertEqual(a['kill']['state'], 'denied')
                    self.assertTrue(a['kill']['grey_out'])
                    self.assertEqual(a['create_user']['state'], 'unknown')
                    self.assertFalse(a['create_user']['grey_out'])
                    self.assertEqual(a['set_own_password']['state'], 'allowed')
                elif login == 'acct_cu':
                    self.assertEqual(a['create_user']['state'], 'allowed')
                    self.assertEqual(a['kill']['state'], 'denied')
                    self.assertEqual(a['grant_role']['state'], 'unknown')

    def test_mysql_roles_active_default_and_mandatory(self):
        for server in ('mysql-8.0', 'mysql-8.4', 'mysql-9.7'):
            with self.subTest(server=server):
                r, _data, _conn = report(server, 'acct_app')
                roles = {x['name']: x for x in r['roles']}
                self.assertTrue(roles['r_read']['active'] and roles['r_read']['default'])
                self.assertTrue(roles['r_mand']['mandatory'])
                self.assertFalse(roles['r_sys']['active'])
                # SYSTEM_USER comes only through r_sys, which is not active.
                self.assertNotIn('SYSTEM_USER', r['dynamic'])
                nine = server == 'mysql-9.7'
                # 9.5+: activate_mandatory_roles defaults ON, so r_mand (PROCESS) is active.
                self.assertEqual(roles['r_mand']['active'], nine)
                self.assertEqual(r['actions']['sessions_all']['state'], 'allowed' if nine else 'denied')
                self.assertIs(p.has(r, 'SELECT', 'database', database='shop'), True)  # merged from r_read

    def test_mariadb_role_tree_public_and_wildcards(self):
        for server in ('mariadb-10.11', 'mariadb-11.4'):
            with self.subTest(server=server):
                r, _data, _conn = report(server, 'acct_app')
                self.assertEqual([x['name'] for x in r['current_roles']], ['r_read'])
                roles = {x['name']: x for x in r['roles']}
                self.assertTrue(roles['r_read']['default'] and roles['r_read']['active'])
                self.assertTrue(roles['r_write']['admin_option'])
                self.assertEqual(roles['r_nested']['grantee']['name'], 'r_read')
                self.assertIn('SLAVE MONITOR', r['global'])  # from PUBLIC
                self.assertEqual(r['actions']['replication_status']['state'], 'allowed')
                self.assertIs(p.has(r, 'SHOW VIEW', 'database', database='shop'), True)  # nested role
                self.assertIs(p.has(r, 'SELECT', 'database', database='db_x'), True)   # literal db\_x
                self.assertIsNone(p.has(r, 'SELECT', 'database', database='dbAx'))     # only db_x matches dbAx
                self.assertIs(p.has(r, 'INSERT', 'database', database='dbAx'), True)   # wildcard db_x
                self.assertIs(p.has(r, 'SELECT', 'column', database='shop', table='orders', columns=['a', 'b']), True)
                self.assertIs(p.has(r, 'EXECUTE', 'routine', database='shop', routine='f', kind='FUNCTION'), True)
                self.assertTrue(p.can(r, 'grant_role', roles=[{'name': 'r_write'}])['allowed'])

    def test_mysql_partial_revokes(self):
        for server in ('mysql-8.0', 'mysql-8.4', 'mysql-9.7'):
            with self.subTest(server=server):
                r, _data, _conn = report(server, 'acct_pr')
                self.assertTrue(r['partial_revokes'])
                self.assertEqual({e['database'] for e in r['restrictions']}, {'mysql', 'db_x'})
                self.assertIsNot(p.has(r, 'INSERT', 'database', database='mysql'), True)
                self.assertIsNot(p.has(r, 'SELECT', 'database', database='db_x'), True)
                self.assertIs(p.has(r, 'SELECT', 'database', database='dbAx'), True)  # literal names
                self.assertIs(p.has(r, 'INSERT', 'database', database='shop'), True)

    def test_partial_revokes_leave_global_and_unscoped_checks_unknown(self):
        # Exact recorded restrictions; acct_pr lacks GRANT OPTION. The
        # grantable variant adds that bit explicitly. MariaDB has no
        # partial revokes.
        for server in ('mysql-8.0', 'mysql-8.4', 'mysql-9.7'):
            r, _data, _conn = report(server, 'acct_pr')
            grantable = p.parse_grants(r['grants'] + ['GRANT USAGE ON *.* TO acct_pr@% WITH GRANT OPTION'],
                                      r['engine'], r['version'], partial_revokes=True)
            for privilege in ('SELECT', 'INSERT', 'UPDATE'):
                for level in ('global', 'database', 'table', 'column', 'routine'):
                    with self.subTest(server=server, privilege=privilege, level=level):
                        self.assertIsNone(p.has(r, privilege, level))
                        self.assertIsNone(p.has(grantable, privilege, level, grantable=True))
                self.assertFalse(p.has(r, privilege, grantable=True))
            for action, target in (
                    ('routine_body', {}), ('insert', {}), ('update', {}),
                    ('dump_mysqldump', {'single_transaction': True, 'triggers': False, 'no_tablespaces': True})):
                with self.subTest(server=server, action=action):
                    answer = p.can(r, action, **target)
                    self.assertIsNone(answer['allowed'])
                    self.assertFalse(answer['grey_out'])
            for action in ('grant', 'revoke'):
                self.assertFalse(p.can(r, action, privileges=['SELECT'], level='global')['allowed'])
                answer = p.can(grantable, action, privileges=['SELECT'], level='global')
                self.assertIsNone(answer['allowed'])
                self.assertFalse(answer['grey_out'])
            for action in ('routine_body', 'insert', 'update'):
                self.assertEqual(r['actions'][action]['state'], 'unknown')
            # Other globals stay exact. MySQL 9.7 activates r_mand's
            # PROCESS; on 8.x its absence makes default mysqldump impossible.
            self.assertFalse(p.has(r, 'GRANT OPTION'))
            self.assertTrue(p.has(grantable, 'GRANT OPTION'))
            self.assertFalse(p.has(r, 'BACKUP_ADMIN'))
            self.assertIs(p.has(r, 'PROCESS'), server == 'mysql-9.7')
            self.assertIs(p.can(r, 'dump_mysqldump')['allowed'], None if server == 'mysql-9.7' else False)
            self.assertTrue(p.can(r, 'insert', database='shop', table='orders')['allowed'])
            self.assertIsNone(p.can(r, 'insert', database='mysql', table='user')['allowed'])

    def test_mariadb_super_on_10_11_versus_11_4(self):
        r, _data, _conn = report('mariadb-10.11', 'acct_super')
        # 10.11: SUPER still covers CONNECTION ADMIN (verified live: KILL of
        # another user's query worked), but not READ_ONLY ADMIN from 10.11.1
        # (verified live: CREATE USER under read_only gave 1290).
        self.assertEqual(r['actions']['kill']['state'], 'allowed')
        self.assertEqual(r['actions']['kill']['via'], 'SUPER')
        self.assertEqual(r['actions']['read_only_bypass']['state'], 'unknown')
        model = p.parse_grants(['GRANT SUPER ON *.* TO `u`@`%`'], 'mariadb', '11.4.13-MariaDB')
        # 11.4: SUPER covers it only for grants stored before 11.0 (live: 1095).
        self.assertIsNone(p.can(model, 'kill')['allowed'])

    def test_expired_account(self):
        conn, _data = replay.session('mariadb-11.4', 'acct_super')
        with self.assertRaisesRegex(ConnectorError, 'expired'):
            p.read_privileges(conn)


class GrantLines(unittest.TestCase):
    def model(self, lines=(), engine='mysql', version='8.4.0', **kwargs):
        return p.parse_grants(lines, engine, version, **kwargs)

    def test_mariadb_tls_and_grant_option_before_resource_limits(self):
        # Recorded resource clause; TLS and GRANT OPTION variants follow
        # MariaDB sql/sql_acl.cc:9364-9418 (not present together in the fixture).
        data = replay.load('mariadb-11.4')
        line = next(c['output'].splitlines()[0] for c in data['reads']['calls']
                    if c['sql'].endswith('SHOW GRANTS FOR `acct_proxy`@`%`'))
        for tls in ('', ' REQUIRE SSL', ' REQUIRE X509',
                    " REQUIRE ISSUER 'WITH GRANT OPTION' SUBJECT 'client'"):
            for grant in (False, True):
                with self.subTest(tls=tls, grant=grant):
                    variant = line.replace(' WITH ', tls + ' WITH ' + ('GRANT OPTION ' if grant else ''))
                    m = self.model([variant], 'mariadb', data['server']['version'])
                    self.assertEqual(m['unparsed'], [])
                    self.assertEqual(m['static_grant_option'], grant)
                    self.assertIs(p.has(m, 'GRANT OPTION'), grant)
                    self.assertNotIn('IDENTIFIED', json.dumps(m))
        m = self.model(['GRANT SELECT ON *.* TO u@h REQUIRE SSL'], 'mariadb')
        self.assertEqual(m['unparsed'], [])

    def test_scoped_grant_option_is_separate_from_all(self):
        for engine in ('mysql', 'mariadb'):
            for level, scope, privilege, target in (
                    ('database', '`shop`.*', 'SELECT', {'database': 'shop'}),
                    ('table', '`shop`.`orders`', 'SELECT', {'database': 'shop', 'table': 'orders'}),
                    ('routine', 'PROCEDURE `shop`.`p`', 'EXECUTE', {'database': 'shop', 'routine': 'p'})):
                for names in (privilege, 'USAGE', 'ALL PRIVILEGES'):
                    for option in ('', ' WITH GRANT OPTION'):
                        with self.subTest(engine=engine, level=level, names=names, option=option):
                            m = self.model(['GRANT ' + names + ' ON ' + scope + ' TO u@h' + option], engine)
                            for grantable in (False, True):
                                self.assertIs(p.has(m, 'GRANT OPTION', level, grantable=grantable, **target),
                                              True if option else None)
                            if names == 'ALL PRIVILEGES':
                                self.assertIs(p.has(m, privilege, level, **target), True)
                                self.assertIsNone(p.has(m, 'SUPER', level, **target))

    def test_explicit_grants_survive_database_pattern_disagreement(self):
        for server in replay.SERVERS:
            conn, data = replay.reads(server)
            from myflames import accounts
            recorded = accounts.read_account(conn, 'acct_app', include_catalog=False)['grants']
            # Keep exact recorded column/table/routine lines; add overlapping
            # database patterns with conflicting rights for the same grantee.
            explicit = [g for g in recorded if '`shop`.`' in g]
            explicit += ['GRANT SELECT, UPDATE, EXECUTE ON `sh%`.* TO `acct_app`@`%`',
                         'GRANT INSERT ON `sho_`.* TO `acct_app`@`%`']
            engine = 'mariadb' if server.startswith('mariadb') else 'mysql'
            m = self.model(explicit, engine, data['server']['version'])
            with self.subTest(server=server):
                self.assertIsNone(p.has(m, 'UPDATE', 'database', database='shop'))
                self.assertIs(p.has(m, 'UPDATE', 'table', database='shop', table='orders'), True)
                self.assertIs(p.has(m, 'SELECT', 'column', database='shop', table='orders', columns=['a', 'b']), True)
                self.assertIs(p.has(m, 'EXECUTE', 'routine', database='shop', routine='p'), True)
                self.assertIsNone(p.has(m, 'SELECT', 'column', database='shop', table='orders', columns=['absent']))
                self.assertIsNone(p.has(m, 'UPDATE', 'table', database='shop', table='absent'))

    def test_global_static_grant_option_combines_sources_but_dynamic_does_not(self):
        for engine in ('mysql', 'mariadb'):
            m = self.model(['GRANT SELECT ON *.* TO u@h',
                            'GRANT INSERT ON *.* TO r WITH GRANT OPTION'], engine)
            self.assertTrue(p.has(m, 'SELECT', grantable=True))
            self.assertTrue(p.can(m, 'grant', privileges=['SELECT'])['allowed'])
            self.assertFalse(p.has(m, 'DELETE', grantable=True))
        m = self.model(['GRANT SELECT ON *.* TO u@h',
                        'GRANT USAGE ON *.* TO r WITH GRANT OPTION',
                        'GRANT BACKUP_ADMIN ON *.* TO u@h',
                        'GRANT CLONE_ADMIN ON *.* TO r WITH GRANT OPTION'])
        self.assertTrue(p.has(m, 'SELECT', grantable=True))
        self.assertFalse(p.has(m, 'BACKUP_ADMIN', grantable=True))
        self.assertTrue(p.has(m, 'CLONE_ADMIN', grantable=True))
        m = self.model(['GRANT SELECT ON *.* TO u@h',
                        'GRANT CLONE_ADMIN ON *.* TO r WITH GRANT OPTION'])
        self.assertFalse(p.has(m, 'SELECT', grantable=True))

    def test_wildcard_literal_and_partial_revokes(self):
        m = self.model(['GRANT SELECT ON `db\\_x`.* TO `u`@`%`', 'GRANT INSERT ON `db_x`.* TO `u`@`%`'])
        self.assertEqual([(e['pattern'], e['database'], e['wildcard']) for e in m['schemas']],
                         [('db\\_x', 'db_x', False), ('db_x', 'db_x', True)])
        self.assertIs(p.has(m, 'SELECT', 'database', database='db_x'), True)
        # The exact name entry decides; the wildcard's INSERT is not added.
        self.assertIsNone(p.has(m, 'INSERT', 'database', database='db_x'))
        self.assertIs(p.has(m, 'INSERT', 'database', database='dbAx'), True)
        m = self.model(['GRANT SELECT ON `d%`.* TO u@h', 'GRANT INSERT ON `db_`.* TO u@h'])
        self.assertIsNone(p.has(m, 'SELECT', 'database', database='dbx'))  # patterns disagree
        m = self.model(['GRANT SELECT ON `db_x`.* TO u@h'], partial_revokes=True)
        self.assertFalse(m['schemas'][0]['wildcard'])
        self.assertIsNone(p.has(m, 'SELECT', 'database', database='dbAx'))

    def test_quoted_names_columns_routines_proxy_and_unparsed(self):
        m = self.model(['GRANT SELECT (`a,b`, `c``d`) ON `d.b`.`t t` TO `u@x`@`%`',
                        'GRANT EXECUTE ON FUNCTION "d.b"."f f" TO "u@x"@"%"',
                        "GRANT PROXY ON 'p@x'@'h' TO 'u@x'@'%'", 'GRANT FUTURE magic syntax'])
        self.assertEqual(m['columns'][0]['columns'], ['a,b', 'c`d'])
        self.assertEqual(m['routines'][0]['name'], 'f f')
        self.assertEqual(m['proxies'][0]['account']['name'], 'p@x')
        self.assertEqual(len(m['unparsed']), 1)
        self.assertIsNone(p.has(m, 'PROCESS'))  # an unparsed line may grant it
        m['unparsed'] = []
        self.assertIs(p.has(m, 'SELECT', 'column', database='d.b', table='t t', columns=['a,b']), True)
        self.assertIsNone(p.has(m, 'SELECT', 'column', database='d.b', table='t t', columns=['missing']))

    def test_package_library_and_role_lines_are_kept(self):
        m = self.model(['GRANT EXECUTE ON PACKAGE `shop`.`pkg` TO `app`@`%`',
                        'GRANT ALTER ROUTINE ON PACKAGE BODY `shop`.`pkg` TO `app`@`%`',
                        'GRANT EXECUTE ON FUNCTION `shop`.`f` TO `reader`',
                        'GRANT PROCESS ON *.* TO PUBLIC', 'GRANT `reader` TO `app`@`%` WITH ADMIN OPTION',
                        'SET DEFAULT ROLE `reader` FOR `app`@`%`'], 'mariadb', '11.4.13-MariaDB')
        self.assertEqual({r['kind'] for r in m['routines']}, {'PACKAGE', 'PACKAGE BODY', 'FUNCTION'})
        self.assertIn('PROCESS', m['global'])
        self.assertTrue(m['roles'][0]['default'] and m['roles'][0]['admin_option'])
        m = self.model(['GRANT EXECUTE ON LIBRARY `shop`.`lib` TO `app`@`%`'], 'mysql', '9.7.0')
        self.assertEqual(m['routines'][0]['kind'], 'LIBRARY')
        self.assertEqual(m['unparsed'], [])

    def test_credentials_removed_before_any_projection(self):
        shapes = ["IDENTIFIED BY PASSWORD '*HASH'", "IDENTIFIED VIA ed25519 USING 'SEKRIT' OR unix_socket",
                  "IDENTIFIED WITH 'caching_sha2_password' AS 0x414243",
                  "IDENTIFIED VIA p USING 'WITH GRANT OPTION secret' OR q AS 'another'"]
        for auth in shapes:
            with self.subTest(auth=auth):
                m = self.model(['GRANT SELECT ON *.* TO u@h ' + auth + ' WITH GRANT OPTION'], engine='mariadb')
                encoded = json.dumps(m)
                for forbidden in ('HASH', 'SEKRIT', '414243', 'secret', 'another', 'IDENTIFIED'):
                    self.assertNotIn(forbidden, encoded)
                self.assertEqual(m['global_grantable'], ['SELECT'])
        self.assertNotIn('oldsecret', p.redact("ALTER USER u IDENTIFIED BY 'newsecret' REPLACE 'oldsecret'"))


class Rules(unittest.TestCase):
    def model(self, lines, engine='mysql', version='8.4.0', **extra):
        m = p.parse_grants(lines, engine, version)
        m.update(extra)
        return m

    def test_mariadb_super_boundaries(self):
        """SUPER covers the split privileges through all of 10.11 (privilege.h
        at mariadb-10.11.16), not from 11.0.1; READ_ONLY ADMIN only before
        10.11.1 (sql_acl.cc:1548-1551 at mariadb-10.11.1)."""
        cases = [('10.11.0', 'READ_ONLY ADMIN', True), ('10.11.1', 'READ_ONLY ADMIN', None),
                 ('10.11.19', 'READ_ONLY ADMIN', None), ('10.11.19', 'CONNECTION ADMIN', True),
                 ('10.11.19', 'BINLOG MONITOR', True), ('11.0.0', 'CONNECTION ADMIN', True),
                 ('11.0.1', 'CONNECTION ADMIN', None), ('11.4.3', 'SLAVE MONITOR', None),
                 ('11.4.3', 'PROCESS', False)]
        for version, privilege, expected in cases:
            with self.subTest(version=version, privilege=privilege):
                m = self.model(['GRANT SUPER ON *.* TO u@h'], 'mariadb', version + '-MariaDB')
                self.assertIs(p.has(m, privilege), expected)
        self.assertEqual(dbops.required_privileges('kill', 'mariadb', (11, 0, 0))['any_of'],
                         [['CONNECTION ADMIN'], ['SUPER']])
        self.assertEqual(dbops.required_privileges('kill', 'mariadb', (11, 0, 1))['any_of'], [['CONNECTION ADMIN']])
        self.assertIn(['SUPER'], dbops.required_privileges('write_when_read_only', 'mariadb', (10, 11, 0))['any_of'])
        self.assertNotIn(['SUPER'], dbops.required_privileges('write_when_read_only', 'mariadb', (10, 11, 1))['any_of'])

    def test_own_password_compares_user_and_host(self):
        m = self.model(['GRANT USAGE ON *.* TO `app`@`%`'], account='app@%')
        self.assertEqual(p.can(m, 'set_password', own_account=True)['state'], 'allowed')
        # Another account: CREATE USER is absent (exact), UPDATE on mysql cannot be ruled out.
        self.assertEqual(p.can(m, 'set_password', own_account=False)['state'], 'unknown')
        self.assertEqual(p.can(m, 'create_role')['state'], 'denied')
        self.assertEqual(p.can(m, 'set_own_password')['state'], 'allowed')
        self.assertEqual(p.can(m, 'default_roles', own_account=True)['state'], 'allowed')
        # KILL compares the user name only.
        self.assertEqual(p.can(m, 'kill', own_user=True, target_system_user=False)['state'], 'allowed')

    def test_mariadb_password_steps_need_update_on_mysql(self):
        """SET PASSWORD for another account needs UPDATE on mysql; CREATE USER
        does not suffice (Ma/sql/sql_acl.cc:4224-4234)."""
        m = self.model(['GRANT CREATE USER ON *.* TO u@h'], 'mariadb', '11.4.3-MariaDB')
        self.assertEqual(p.can(m, 'create_user')['state'], 'unknown')
        self.assertIn('UPDATE on mysql.*', ' '.join(p.can(m, 'create_user')['missing']))
        self.assertEqual(p.can(m, 'set_password')['state'], 'unknown')
        m = self.model(['GRANT CREATE USER, UPDATE ON *.* TO u@h'], 'mariadb', '11.4.3-MariaDB')
        self.assertEqual(p.can(m, 'create_user')['state'], 'allowed')
        self.assertEqual(p.can(m, 'set_password')['state'], 'allowed')
        m = self.model(['GRANT CREATE USER ON *.* TO u@h'])
        self.assertEqual(p.can(m, 'set_password')['state'], 'allowed')  # MySQL ALTER USER accepts CREATE USER

    def test_system_user_counts_only_direct_target_grants(self):
        m = self.model(['GRANT CREATE USER ON *.* TO u@h'])
        self.assertEqual(p.can(m, 'lock_account', target_system_user=True)['state'], 'denied')
        self.assertEqual(p.can(m, 'lock_account', target_system_user=False)['state'], 'allowed')
        self.assertEqual(p.can(m, 'lock_account')['state'], 'allowed')
        m = self.model(['GRANT CONNECTION_ADMIN ON *.* TO u@h'])
        self.assertEqual(p.can(m, 'kill')['state'], 'unknown')  # target session flag unknown
        self.assertEqual(p.can(m, 'kill', target_system_user=False)['state'], 'allowed')
        self.assertEqual(p.can(m, 'kill', target_system_user=True)['state'], 'denied')

    def test_kill_own_requires_known_system_user_flag(self):
        for server in replay.SERVERS:
            m, _data, _conn = report(server, 'acct_app')
            with self.subTest(server=server):
                self.assertIs(p.can(m, 'kill_own')['allowed'], True if server.startswith('mariadb') else None)
                self.assertTrue(p.can(m, 'kill_own', target_system_user=False)['allowed'])
                self.assertFalse(p.can(m, 'kill_own')['grey_out'])
                if server.startswith('mysql'):
                    self.assertFalse(p.can(m, 'kill_own', target_system_user=True)['allowed'])
                    admin, _data, _conn = report(server, 'acct_admin')
                    self.assertTrue(p.can(admin, 'kill_own')['allowed'])

    def test_read_only_server(self):
        m = self.model(['GRANT CREATE USER ON *.* TO u@h'], variables={'read_only': 'ON'})
        self.assertEqual(p.can(m, 'create_user')['state'], 'denied')  # no CONNECTION_ADMIN or SUPER (live 1290)
        self.assertEqual(p.can(m, 'kill')['state'], 'denied')
        self.assertEqual(p.can(m, 'health')['state'], 'allowed')
        m = self.model(['GRANT CREATE USER, CONNECTION_ADMIN ON *.* TO u@h'], variables={'read_only': 'ON'})
        self.assertEqual(p.can(m, 'create_user')['state'], 'allowed')
        m['variables'] = {'super_read_only': 'ON', 'read_only': 'ON'}
        self.assertEqual(p.can(m, 'create_user')['state'], 'denied')
        self.assertEqual(p.can(m, 'set_global')['state'], 'denied')  # no SYSTEM_VARIABLES_ADMIN, not read_only
        m = self.model(['GRANT READ_ONLY ADMIN, CREATE USER ON *.* TO u@h'], 'mariadb', '12.0.2-MariaDB',
                       variables={'read_only': 'NO_LOCK_NO_ADMIN'})
        self.assertEqual(p.can(m, 'drop_user')['state'], 'denied')
        m['variables'] = {'read_only': 'ON'}
        self.assertEqual(p.can(m, 'drop_user')['state'], 'allowed')

    def test_explain_connection_needs_every_static_privilege_but_grant_option(self):
        lines = ['GRANT ' + ', '.join(sorted(p.STATIC - {'USAGE', 'GRANT OPTION', 'DROP ROLE'})) + ' ON *.* TO u@h']
        self.assertEqual(p.can(self.model(lines), 'explain_connection')['state'], 'denied')
        lines = ['GRANT ' + ', '.join(sorted(p.STATIC - {'USAGE', 'GRANT OPTION'})) + ' ON *.* TO u@h']
        self.assertEqual(p.can(self.model(lines), 'explain_connection')['state'], 'allowed')
        self.assertEqual(p.can(self.model([]), 'explain_connection', own_user=True)['state'], 'allowed')

    def test_unavailable_actions_are_not_greyed(self):
        m = self.model(['GRANT ALL PRIVILEGES ON *.* TO u@h'], 'mariadb', '11.4.3-MariaDB')
        for action in ('clone_donor', 'set_persist', 'backup_lock', 'dump_shell'):
            answer = p.can(m, action)
            self.assertFalse(answer['available'])
            self.assertFalse(answer['grey_out'])
            self.assertTrue(answer['reason'].startswith('Not available'))

    def test_grant_needs_grant_option_and_dynamic_update_alternative(self):
        m = self.model(['GRANT BACKUP_ADMIN ON *.* TO u@h', 'GRANT CLONE_ADMIN ON *.* TO u@h WITH GRANT OPTION'])
        self.assertIsNone(p.can(m, 'grant', privileges=['BACKUP_ADMIN'])['allowed'])
        self.assertTrue(p.can(m, 'grant', privileges=['CLONE_ADMIN'])['allowed'])
        m = self.model(['GRANT UPDATE ON `mysql`.* TO u@h'])
        self.assertTrue(p.can(m, 'grant', privileges=['BACKUP_ADMIN'])['allowed'])
        self.assertFalse(p.can(m, 'grant', privileges=['SELECT', 'BACKUP_ADMIN'])['allowed'])
        m = self.model(['GRANT SELECT ON *.* TO u@h WITH GRANT OPTION'])
        self.assertEqual(p.can(m, 'grant')['state'], 'allowed')
        self.assertEqual(p.can(self.model(['GRANT USAGE ON *.* TO u@h']), 'grant')['state'], 'unknown')

    def test_every_action_has_an_answer_on_both_engines(self):
        for engine, version in (('mysql', '8.0.46'), ('mysql', '9.7.2'), ('mariadb', '10.11.19-MariaDB'),
                                ('mariadb', '11.4.13-MariaDB')):
            empty = self.model([], engine, version)
            unreadable = p.parse_grants([], engine, version, checked=False)
            for action in p.ACTIONS:
                with self.subTest(engine=engine, version=version, action=action):
                    answer = p.can(empty, action)
                    self.assertIn(answer['state'], ('allowed', 'denied', 'unknown'))
                    if answer['available'] and answer['state'] != 'allowed':
                        self.assertIsNone(p.can(unreadable, action)['allowed'])
                    for alt in p.requirements(action, engine, version)['alternatives']:
                        for atom in alt:
                            self.assertIsInstance(atom, dict)
                            self.assertIn(atom['level'], ('global', 'database', 'table', 'column', 'routine',
                                                          'proxy'))

    def test_aliases_resolve_to_report_keys(self):
        m = self.model(['GRANT PROCESS ON *.* TO u@h'])
        self.assertEqual(p.can(m, 'innodb'), p.can(m, 'innodb_status'))
        self.assertIn(('error_log', 'read'), p.ACTION_KEYS)
        self.assertEqual(p.can(m, 'error-log'), p.can(m, 'error_log'))
        self.assertEqual(p.can(m, 'write_when_read_only')['need'], p.can(m, 'read_only_bypass')['need'])
        for alias, key in p.ALIASES.items():
            self.assertIn(key, p.ACTIONS, alias)

    def test_error_log_uses_table_select_on_mysql_and_no_privilege_on_mariadb(self):
        spec = p.requirements('error-log', 'mysql')
        self.assertEqual(spec['alternatives'], [[{'privilege': 'SELECT', 'level': 'table',
                                                 'database': 'performance_schema', 'table': 'error_log'}]])
        for grants in (['GRANT SELECT ON `performance_schema`.`error_log` TO u@h'],
                       ['GRANT SELECT ON `performance_schema`.* TO u@h']):
            self.assertTrue(p.can(self.model(grants), 'error_log')['allowed'])
        self.assertEqual(p.requirements('error_log', 'mariadb')['alternatives'], [[]])
        self.assertTrue(p.can(self.model([], 'mariadb', '11.4.13-MariaDB'), 'error_log')['allowed'])

    def test_python37_syntax(self):
        for path in (Path(p.__file__), Path(__file__), Path(replay.__file__)):
            ast.parse(path.read_text(), **({'feature_version': 7} if sys.version_info >= (3, 8) else {}))


class EditorStatementPrivileges(unittest.TestCase):
    def answer(self, sql, grants=(), engine='mysql', **options):
        from myflames import editor_session
        model = p.parse_grants(list(grants), engine=engine, version='11.4.13' if engine == 'mariadb' else '8.4.11')
        model.update(options.pop('model', {}))
        units = [u for u in editor_session._plan_run({'sql': sql}, None)['units'] if not u.get('generated')]
        unit = units[0] if units else editor_session._plan_run({'sql': sql}, None)['units'][0]
        return p.statement_privileges(model, unit, **options)

    def test_statement_keys_and_object_absence(self):
        cases = [('INSERT INTO db.t VALUES (1)', ['insert']), ('UPDATE db.t SET id=1', ['update']),
                 ('DELETE FROM db.t WHERE id=1', ['delete']), ('REPLACE INTO db.t VALUES (1)', ['insert', 'delete']),
                 ('CREATE TABLE db.t (id int)', ['create_table']), ('ALTER TABLE db.t ADD i int', ['alter_table']),
                 ('DROP TABLE db.t', ['drop_table']), ('CREATE INDEX i ON db.t (id)', ['create_index']),
                 ('CREATE VIEW db.v AS SELECT * FROM db.t', ['create_view']), ('TRUNCATE db.t', ['truncate_table']),
                 ('TRUNCATE TABLE db.t', ['truncate_table']), ('CREATE USER a', ['create_user']),
                 ('ALTER USER a ACCOUNT LOCK', ['lock_account']), ('ALTER USER a ACCOUNT UNLOCK', ['unlock_account']),
                 ('ALTER USER a PASSWORD EXPIRE', ['expire_password']), ('RENAME USER a TO b', ['rename_user']),
                 ('SET PASSWORD FOR a = \'secret\'', ['set_password']), ('GRANT SELECT ON db.* TO a', ['grant']),
                 ('REVOKE SELECT ON db.* FROM a', ['revoke']), ('GRANT r TO a', ['grant_role'])]
        for engine in ('mysql', 'mariadb'):
            for sql, keys in cases:
                with self.subTest(engine=engine, sql=sql):
                    answer = self.answer(sql, engine=engine)
                    self.assertEqual(answer['action_keys'], keys)
                    self.assertEqual(answer['state'], 'unknown')
                    self.assertTrue(set(keys) <= set(p.ACTIONS))
                    self.assertNotIn('secret', json.dumps(answer))

    def test_scoped_grants_and_quoted_identifiers(self):
        for sql in ('INSERT INTO `db`.`t` VALUES (1)', 'INSERT INTO t VALUES (1)'):
            self.assertEqual(self.answer(sql, ['GRANT INSERT ON db.t TO a@h'], database='db')['state'], 'allowed')
        answer = self.answer('REPLACE INTO db.t VALUES (1)', ['GRANT INSERT ON db.t TO a@h'])
        self.assertEqual(answer['state'], 'unknown')
        self.assertIn('DELETE on db.t', answer['missing'])
        self.assertEqual(self.answer('INSERT INTO db.t VALUES (1) ON DUPLICATE KEY UPDATE id=2')['action_keys'],
                         ['insert', 'update'])

    def test_variable_specific_global_and_session_actions(self):
        for sql, key in [('SET GLOBAL max_connections=100', 'set_global'),
                         ('SET @@GLOBAL.`max_connections`=100', 'set_global'),
                         ('/*myflames*/ SET GLOBAL max_connections=100', 'set_global'),
                         ('SET PERSIST max_connections=100', 'set_persist'),
                         ('SET PERSIST_ONLY max_connections=100', 'set_persist_only'),
                         ('SET SESSION sql_log_bin=0', 'set_session_restricted')]:
            answer = self.answer(sql)
            self.assertEqual(answer['action_keys'], [key])
            self.assertEqual(answer['state'], 'denied')
            self.assertTrue(answer['missing'])
        self.assertEqual(self.answer('SET GLOBAL max_connections=100',
                                     ['GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO a@h'])['state'], 'allowed')
        self.assertEqual(self.answer('SET GLOBAL mandatory_roles=\'r\'',
                                     ['GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO a@h'])['state'], 'denied')
        self.assertEqual(self.answer('SET GLOBAL max_connections=100',
                                     ['GRANT CONNECTION ADMIN ON *.* TO a@h'], engine='mariadb')['state'], 'allowed')

    def test_unmapped_or_incomplete_context_never_denies(self):
        for sql in ('CALL p()', 'EXECUTE p', 'SET ROLE ALL', 'SET sort_buffer_size=1', 'SET @x=1',
                    'SET GLOBAL max_connections=100, SESSION sql_mode=\'\'', 'RESET PERSIST',
                    'SET SESSION gtid_next=\'AUTOMATIC\'', 'SET SESSION min_examined_row_limit=1',
                    '/*!99999 SET GLOBAL max_connections=100 */', '/*M! SET GLOBAL max_connections=100 */',
                    'DROP TEMPORARY TABLE t', 'SELECT \'SET GLOBAL max_connections=1\''):
            self.assertEqual(self.answer(sql)['state'], 'unknown', sql)
        self.assertEqual(self.answer('SET GLOBAL binlog_format=\'ROW\'', engine='mariadb')['state'], 'unknown')
        self.assertEqual(self.answer('SET GLOBAL max_connections=1', model={'fresh_session': False})['state'], 'unknown')
        self.assertEqual(self.answer('SET GLOBAL max_connections=1', model={'checked': False})['state'], 'unknown')
        self.assertEqual(self.answer('SET PASSWORD = \'secret\'')['state'], 'unknown')



class PrivilegeCatalogTest(unittest.TestCase):
    def capture(self, server, login='acct_low'):
        data = json.loads((replay.FIXTURES / ('show-privileges-' + server + '.json')).read_text())
        conn = replay.ReplayConn(data['sessions'][login]['calls'], data['server']['version'])
        rows = p.Reader(conn, '/*myflames:accounts*/ ').rows('SHOW PRIVILEGES')
        return data, rows

    def test_live_presence_and_authoritative_levels_on_five_servers(self):
        for server in replay.SERVERS:
            with self.subTest(server=server):
                data, rows = self.capture(server)
                engine, version = data['server']['engine'], data['server']['version']
                catalog, meta = p.privilege_catalog(engine, version, rows)
                names = {level: {entry['name'] for entry in entries} for level, entries in catalog.items()}
                self.assertEqual(meta, dict(engine=engine, version=version, source='server'))
                self.assertEqual(set(catalog), set(p.GRANT_LEVELS))
                # Their SHOW PRIVILEGES Context is not a list of valid GRANT levels.
                self.assertTrue({'CREATE ROUTINE', 'SELECT', 'CREATE VIEW', 'TRIGGER', 'EVENT'} <= names['database'])
                self.assertEqual(names['column'], {'SELECT', 'INSERT', 'UPDATE', 'REFERENCES'})
                self.assertTrue({'EXECUTE', 'ALTER ROUTINE'} <= names['routine'])
                self.assertNotIn('CREATE ROUTINE', names['routine'])
                self.assertEqual(names['proxy'], {'PROXY'})
                self.assertFalse({'USAGE', 'PROXY', 'GRANT OPTION'} & names['global'])
                self.assertTrue(all(e['description'] and e['grantable_here'] for entries in catalog.values() for e in entries))
                if engine == 'mysql':
                    self.assertIn('FLUSH_TABLES', names['global'])
                    self.assertNotIn('FLUSH_TABLES', names['database'])
                    self.assertEqual('SET_USER_ID' in names['global'], server == 'mysql-8.0')
                    for name in ('ALLOW_NONEXISTENT_DEFINER', 'FLUSH_PRIVILEGES', 'OPTIMIZE_LOCAL_TABLE',
                                 'SET_ANY_DEFINER', 'TRANSACTION_GTID_TAG'):
                        self.assertEqual(name in names['global'], server != 'mysql-8.0')
                    for name in ('CREATE_SPATIAL_REFERENCE_SYSTEM', 'MANAGE_DATA_MASKING_POLICY'):
                        self.assertEqual(name in names['global'], server == 'mysql-9.7')
                    self.assertIn('INNODB_REDO_LOG_ARCHIVE', names['global'])
                else:
                    self.assertIn('READ_ONLY ADMIN', names['global'])
                    self.assertNotIn('READ ONLY ADMIN', names['global'])
                    self.assertFalse({'CREATE ROLE', 'DROP ROLE', 'REPLICATION CLIENT'} & names['global'])
                    self.assertIn('DELETE HISTORY', names['table'])
                    for level in ('global', 'database', 'routine'):
                        self.assertEqual('SHOW CREATE ROUTINE' in names[level], server == 'mariadb-11.4')
                    self.assertFalse(any(e['dynamic'] for entries in catalog.values() for e in entries))

    def test_fallback_version_boundaries_and_optional_plugins(self):
        checks = [('8.0.22', 'FLUSH_TABLES', False), ('8.0.23', 'FLUSH_TABLES', True),
                  ('8.1.0', 'SET_ANY_DEFINER', False), ('8.2.0', 'SET_ANY_DEFINER', True),
                  ('8.3.0', 'SET_USER_ID', True), ('8.4.0', 'SET_USER_ID', False),
                  ('9.1.0', 'CREATE_SPATIAL_REFERENCE_SYSTEM', False),
                  ('9.2.0', 'CREATE_SPATIAL_REFERENCE_SYSTEM', True),
                  ('9.6.0', 'MANAGE_DATA_MASKING_POLICY', False), ('9.7.0', 'MANAGE_DATA_MASKING_POLICY', True)]
        for version, name, present in checks:
            catalog, meta = p.privilege_catalog('mysql', version)
            self.assertEqual(meta['source'], 'static')
            names = {entry['name'] for entry in catalog['global']}
            self.assertEqual(name in names, present, (version, name))
            self.assertNotIn('INNODB_REDO_LOG_ARCHIVE', names)
        for server in replay.SERVERS:
            data, _rows = self.capture(server)
            catalog, meta = p.privilege_catalog(data['server']['engine'], data['server']['version'])
            self.assertTrue(catalog['database'])
            self.assertEqual(meta['source'], 'static')

    def test_context_never_changes_levels_and_unknown_runtime_names_are_global_only(self):
        rows = [('Select', 'Server Admin', ''), ('Event', 'Tables', ''),
                ('PLUGIN_2_ADMIN', 'Columns', ''), ('BAD; SELECT', 'Server Admin', ''),
                ('Unknown words', 'Server', '')]
        catalog, _ = p.privilege_catalog('mysql', '8.4.11', rows)
        names = {level: {e['name'] for e in entries} for level, entries in catalog.items()}
        self.assertEqual(names['global'], {'SELECT', 'EVENT', 'PLUGIN_2_ADMIN'})
        self.assertEqual(names['database'], {'SELECT', 'EVENT'})
        self.assertEqual(names['column'], {'SELECT'})
        maria, _ = p.privilege_catalog('mariadb', '11.4.13', rows)
        self.assertNotIn('PLUGIN_2_ADMIN', {e['name'] for e in maria['global']})
        model = p.parse_grants(['GRANT PLUGIN_2_ADMIN ON *.* TO `u`@`%` WITH GRANT OPTION'])
        self.assertEqual(model['dynamic'], ['PLUGIN_2_ADMIN'])
        self.assertEqual(model['unparsed'], [])
        self.assertTrue(p.has(model, 'PLUGIN_2_ADMIN', grantable=True))
        self.assertEqual(p.normalize_privilege('Read_only admin', 'mariadb'), 'READ_ONLY ADMIN')
        self.assertEqual(p.normalize_privilege('Replication client', 'mariadb'), 'BINLOG MONITOR')


if __name__ == '__main__':
    unittest.main()
