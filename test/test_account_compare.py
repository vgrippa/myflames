"""Credential-free grant comparisons and SQL exports for both engines."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from myflames import account_compare as A, accounts, param_compare as P, privileges
from myflames.connector import ConnectorError
from test_accounts import FakeConn
from test_chart_data import validate


def account(name='app', host='%', grants=None, role=False, **kw):
    return dict({'name': name, 'host': host, 'grants': grants or [], 'role': role, 'exists': True,
                 'attributes_checked': True, 'defaults_checked': True, 'tls_checked': True,
                 'roles': [], 'default_roles': [], 'attributes': {'plugins': ['caching_sha2_password'],
                    'locked': False, 'expired': False, 'limits': {}, 'password_options': {}}}, **kw)


def side(items=(), engine='mysql', complete=True):
    return {'server': {'host':'db1','port':3306,'engine':engine, 'flavor':engine, 'server_version':'test',
                       'server_product':engine}, 'items':list(items), 'complete':complete, 'notes':[],
            'captured_at':'2026-10-27T12:00:00+00:00'}


class CaptureConn(FakeConn):
    query_timeout = 30
    def run(self, sql, extra_flags=None, timeout=None):
        if sql == 'SELECT @@version':
            return self.version + '\n'
        if 'version_comment' in sql:
            return 'version_comment\tMariaDB Server\n' if self.engine == 'mariadb' else 'version_comment\tPercona Server (GPL)\n'
        if sql.startswith(accounts.TAG + 'SELECT User, Host, ssl_type'):
            return self.name + '\t%\tANY\t\t\t\n'
        if 'FROM mysql.user ORDER BY User, Host' in sql:
            return self.name + '\t%\tcaching_sha2_password\tN\tN\tNULL\tNULL\t0\t0\n'
        if 'FROM mysql.global_priv ORDER BY User, Host' in sql:
            return self.name + '\t%\tmysql_native_password\t0\t123456\tNULL\t0\tNULL\t0\n'
        if 'FROM mysql.role_edges' in sql or 'FROM mysql.default_roles' in sql or 'FROM mysql.roles_mapping' in sql:
            return ''
        return super(CaptureConn, self).run(sql, extra_flags, timeout)


class CompareTest(unittest.TestCase):
    def test_system_accounts_remain_compared_with_boolean_flags(self):
        system_names = sorted(accounts.SYSTEM_ACCOUNTS)
        source = side([account(n, host='localhost') for n in system_names] + [account('root')])
        target = side([account('root')])
        report = json.loads(json.dumps(A.compare(source, target)))
        rows = {r['name']: r for r in report['items']}
        for name in system_names:
            self.assertTrue(rows[name]['system'])
            self.assertTrue(rows[name]['source']['system'])
            self.assertEqual(rows[name]['status'], 'only_in_source')
        self.assertFalse(rows['root']['system'])
        self.assertFalse(rows['root']['source']['system'])
        reverse = A.compare(target, source)
        self.assertTrue(all(r['system'] for r in reverse['items'] if r['name'] in system_names))
        schema = json.loads((Path(__file__).resolve().parent.parent / 'docs/schemas/account-compare-1.0.json').read_text())
        validate(report, schema)
        validate(reverse, schema)

    def test_capture_preserves_existing_system_account_classification(self):
        for engine, name in (('mysql', 'mysql.session'), ('mariadb', 'mariadb.sys')):
            with self.subTest(engine=engine):
                result = A.capture(CaptureConn(engine, name=name))
                self.assertTrue(result['items'][0]['system'])
                self.assertTrue(A.compare(result, result)['items'][0]['system'])

    def test_privilege_spacing_quoting_and_column_order(self):
        pairs = [
          ('GRANT SELECT, INSERT ON `shop`.`t` TO `app`@`%`', "grant INSERT,SELECT on 'shop'.'t' to 'app'@'%' ;"),
          ('GRANT UPDATE (`a`,`b`), SELECT ON `shop`.`t` TO `app`@`%` WITH GRANT OPTION',
           'GRANT SELECT, UPDATE (`b`, `a`) ON `shop`.`t` TO `app`@`%` WITH GRANT OPTION'),
          ('GRANT `r2`@`%`, `r1`@`%` TO `app`@`%`', "GRANT 'r1'@'%', 'r2'@'%' TO 'app'@'%'"),
        ]
        for a,b in pairs:
            with self.subTest(a=a):
                self.assertEqual(A.normalize_grant(a), A.normalize_grant(b))
        self.assertNotEqual(A.normalize_grant(pairs[0][0]), A.normalize_grant(pairs[0][0].replace('shop','Shop')))
        self.assertNotEqual(A.normalize_grant(pairs[0][0]), A.normalize_grant(pairs[0][0]+' WITH GRANT OPTION'))

    def test_every_status_and_specific_changes(self):
        a = account('changed', grants=['GRANT SELECT ON *.* TO `changed`@`%`'])
        b = account('changed', grants=['GRANT INSERT ON *.* TO `changed`@`%`'])
        a['roles'] = [{'name':'r','host':'%','admin_option':True}]
        a['default_roles'] = [{'name':'r','host':'%'}]
        b['attributes']['locked'] = True
        source = side([a, account('source_only'), account('same'), account('denied',error='Access denied')])
        target = side([b, account('target_only'), account('same'), account('denied')])
        report = A.compare(source,target)
        rows = {r['name']:r for r in report['items']}
        self.assertEqual(set(r['status'] for r in rows.values()), set(P.STATUS_TEXT))
        changes = rows['changed']['differences']
        self.assertEqual(changes['grants']['only_in_source'],a['grants'])
        self.assertEqual(changes['grants']['only_in_target'],b['grants'])
        self.assertEqual(changes['roles']['only_in_source'],a['roles'])
        self.assertEqual(changes['default_roles']['only_in_source'],a['default_roles'])
        self.assertEqual(changes['attributes'],[{'name':'locked','source':False,'target':True}])
        self.assertEqual(rows['denied']['reason'],'Access denied')
        self.assertEqual(json.loads(json.dumps(report)),report)
        self.assertTrue(A.compare(side([a]),side([a]))['identical'])

    def test_incomplete_unreadable_and_mismatched_engines(self):
        for other in (side([],complete=False),P.failed_side('Access denied')):
            report = A.compare(side([account()]), other)
            self.assertEqual(report['items'][0]['status'],'not_compared')
            self.assertIsNotNone(report['items'][0]['source'])
            self.assertFalse(report['complete'])
        for field in ('attributes_checked','defaults_checked','tls_checked'):
            report = A.compare(side([account(**{field:False})]), side([account()]))
            self.assertEqual(report['items'][0]['status'],'not_compared')
        self.assertTrue(any('different engines' in n for n in A.compare(side(),side(engine='mariadb'))['notes']))

    def test_hashes_and_unrecognized_attributes_never_present_in_json(self):
        hashes = ["*0123456789ABCDEF0123456789ABCDEF01234567", '012345ABCDEF012345']
        for suffix in (" IDENTIFIED BY PASSWORD '%s'" % hashes[0], ' IDENTIFIED WITH plugin AS 0x'+hashes[1]):
            a = account(grants=['GRANT USAGE ON *.* TO `app`@`%`'+suffix])
            a['authentication_string']=hashes[0]
            a['attributes']['authentication_string']=hashes[0]
            a['model']={'password':hashes[0]}
            report = A.compare(side([a]),side([a]))
            output = json.dumps(report)
            for value in hashes:
                self.assertNotIn(value,output)
            self.assertNotIn('authentication_string',output)
            self.assertNotIn('IDENTIFIED',output)

    def test_real_account_reader_capture_both_engines_and_tls(self):
        for engine in ('mysql','mariadb'):
            conn = CaptureConn(engine)
            report = A.capture(conn)
            self.assertEqual(report['items'][0]['attributes']['tls']['type'],'ANY')
            self.assertEqual(report['server']['flavor'],'percona' if engine=='mysql' else 'mariadb')
            self.assertNotIn('0123456789ABCDEF0123456789ABCDEF01234567',json.dumps(report))
            self.assertNotIn('48415348534543524554',json.dumps(report))
            self.assertEqual(A.compare(report,report)['items'][0]['status'],'identical')
            self.assertEqual(conn.stdin,[])

    def test_capture_unreadable_account(self):
        conn = CaptureConn(exists=False)
        result = A.capture(conn)
        self.assertIn('disappeared',result['items'][0]['error'])
        self.assertEqual(A.compare(result,result)['items'][0]['status'],'not_compared')

    def test_schema_roundtrip(self):
        report=A.compare(side([account()]),side([account()]))
        schema=json.loads((Path(__file__).resolve().parent.parent/'docs/schemas/account-compare-1.0.json').read_text())
        validate(report,schema)
        with self.assertRaises(AssertionError):
            validate(dict(report,authentication_string='not allowed'),schema)


class ExportTest(unittest.TestCase):
    def test_default_export_omits_system_accounts_and_names_them_once(self):
        for engine, names in (('mysql', ['mysql.infoschema', 'mysql.session', 'mysql.sys']),
                              ('mariadb', ['mariadb.sys'])):
            with self.subTest(engine=engine):
                # An unreadable system account must not block the ordinary export.
                report = side([account(n, host='localhost', error='Access denied') for n in names] +
                              [account('app'), account('root')], engine)
                sql = A.export_sql(report)
                self.assertIn('CREATE USER IF NOT EXISTS `app`@`%`;', sql)
                self.assertIn('CREATE USER IF NOT EXISTS `root`@`%`;', sql)
                omitted = [line for line in sql.splitlines() if line.startswith('-- Omitted system accounts:')]
                self.assertEqual(len(omitted), 1)
                for name in names:
                    self.assertIn('`%s`@`localhost`' % name, omitted[0])
                    self.assertNotIn(name, '\n'.join(line for line in sql.splitlines() if not line.startswith('--')))

    def test_system_accounts_export_only_with_explicit_selection(self):
        for engine, name in (('mysql', 'mysql.session'), ('mariadb', 'mariadb.sys')):
            with self.subTest(engine=engine):
                system = account(name, host='localhost', grants=[
                    'GRANT SELECT ON *.* TO `%s`@`localhost`' % name])
                report = side([system, account('app')], engine)
                sql = A.export_sql(report, [name + '@localhost'])
                self.assertIn('CREATE USER IF NOT EXISTS `%s`@`localhost`;' % name, sql)
                self.assertIn('GRANT SELECT', sql)
                self.assertNotIn('CREATE USER IF NOT EXISTS `app`', sql)
                self.assertNotIn('Omitted system accounts', sql)
                self.assertNotIn(name, A.export_sql(report, ['app@%']))
                self.assertNotIn('CREATE USER', A.export_sql(report, []))

    def test_required_system_role_needs_its_own_explicit_selection(self):
        system = account('mysql.session', role=True)
        user = account(roles=[{'name': 'mysql.session', 'host': '%'}])
        report = side([user, system])
        for chosen in (None, ['app@%']):
            with self.subTest(chosen=chosen), self.assertRaisesRegex(ValueError, 'choose it explicitly'):
                A.export_sql(report, chosen)
        sql = A.export_sql(report, ['app@%', 'mysql.session@%'])
        self.assertIn('CREATE ROLE IF NOT EXISTS `mysql.session`@`%`;', sql)
        self.assertIn('GRANT `mysql.session`@`%` TO `app`@`%`;', sql)

    def test_default_export_with_only_system_accounts_is_comment_only(self):
        report = side([account('mysql.session', host='localhost', error='Access denied')])
        sql = A.export_sql(report)
        self.assertIn('-- Omitted system accounts: `mysql.session`@`localhost`.', sql)
        self.assertTrue(all(line.startswith('--') for line in sql.splitlines()))

    def test_mysql_dependency_order_escaping_and_defaults(self):
        name = 'a`b'
        user = account(name, grants=['GRANT SELECT ON `sh``op`.`t` TO `a``b`@`%` WITH GRANT OPTION'])
        user['roles']=[{'name':'read`role','host':'%','admin_option':True}]
        user['default_roles']=[{'name':'read`role','host':'%'}]
        role=account('read`role',role=True,grants=['GRANT SELECT ON *.* TO `read``role`@`%`'])
        sql=A.export_sql(side([user,role]),['a`b@%'])
        self.assertLess(sql.index('CREATE ROLE'),sql.index('CREATE USER'))
        self.assertLess(sql.index('CREATE USER'),sql.index('GRANT SELECT'))
        self.assertLess(sql.index('GRANT `read``role`'),sql.index('SET DEFAULT ROLE'))
        self.assertIn('CREATE USER IF NOT EXISTS `a``b`@`%`;',sql)
        self.assertIn('SET DEFAULT ROLE `read``role`@`%` TO `a``b`@`%`;',sql)
        self.assertIn('WITH ADMIN OPTION',sql)
        self.assertNotIn('IDENTIFIED',sql)
        self.assertIn('Set the password separately',sql)

    def test_mariadb_hostless_roles_defaults_public_and_no_credentials(self):
        user=account('app',grants=["GRANT SELECT ON `d`.* TO `app`@`%` IDENTIFIED BY PASSWORD '*SECRETHASH'"])
        role=account('reader',host='',role=True,grants=['GRANT SELECT ON `d`.* TO `reader`'])
        public=account('PUBLIC',host='',role=True)
        user['roles']=[{'name':'reader','host':'','admin_option':False}]
        user['default_roles']=[{'name':'reader','host':''}]
        sql=A.export_sql(side([user,role,public],'mariadb'))
        self.assertIn('CREATE ROLE IF NOT EXISTS `reader`;',sql)
        self.assertIn('GRANT `reader` TO `app`@`%`;',sql)
        self.assertIn('SET DEFAULT ROLE `reader` FOR `app`@`%`;',sql)
        self.assertNotIn('`reader`@',sql)
        self.assertNotIn('CREATE ROLE IF NOT EXISTS PUBLIC',sql)
        self.assertNotIn('SECRETHASH',sql)
        self.assertNotIn('IDENTIFIED',sql)

    def test_role_cycles_all_creates_precede_grants(self):
        a,b=account('a',role=True),account('b',role=True)
        a['roles']=[{'name':'b','host':'%'}]; b['roles']=[{'name':'a','host':'%'}]
        sql=A.export_sql(side([a,b]),['a@%'])
        self.assertEqual(sql.count('CREATE ROLE'),2)
        self.assertLess(sql.rfind('CREATE ROLE'),sql.find('GRANT'))

    def test_missing_unreadable_or_unknown_selection_refuses(self):
        user=account(roles=[{'name':'not_listed','host':'%'}])
        for report, chosen in [(side([user]),['app@%']), (P.failed_side('denied'),None),
                               (side([account(error='denied')]),None), (side([account()]),['unknown@%']),
                               (side([account()],complete=False),None)]:
            with self.subTest(report=report), self.assertRaises(ValueError):
                A.export_sql(report,chosen)
        self.assertIn('CREATE USER',A.export_sql(side([account()],complete=False),['app@%']))

    def test_foreign_role_grants_not_copied_as_direct_grants(self):
        role=account('r',host='',role=True,grants=['GRANT SELECT ON *.* TO `r`','GRANT INSERT ON *.* TO `other`'])
        sql=A.export_sql(side([role],'mariadb'))
        self.assertIn('SELECT',sql)
        self.assertNotIn('INSERT',sql)

    def test_line_break_account_name_refuses(self):
        with self.assertRaises(ValueError):
            A.export_sql(side([account('x\nDROP USER y')]))

class ExportEdgeTest(unittest.TestCase):
    def test_mariadb_package_mode_is_restored(self):
        a=account(grants=['GRANT EXECUTE ON PACKAGE BODY `d`.`p` TO `app`@`%`'])
        sql=A.export_sql(side([a],'mariadb'))
        self.assertIn("'ORACLE'",sql)
        self.assertIn('GRANT EXECUTE ON PACKAGE BODY `d` . `p` TO `app`@`%`;',sql)
        self.assertIn('SET SESSION sql_mode = @myflames_accounts_sql_mode;',sql)

    def test_all_identifiers_use_builders_and_restrictions_follow_grants(self):
        a=account(grants=['REVOKE SELECT ON d.* FROM app@%', 'GRANT SELECT (col) ON d.t TO app@%',
                          'GRANT SELECT ON *.* TO app@%'])
        sql=A.export_sql(side([a]))
        self.assertIn('GRANT SELECT ( `col` ) ON `d` . `t` TO `app`@`%`;',sql)
        self.assertLess(sql.index('GRANT SELECT ON'),sql.index('REVOKE SELECT'))

    def test_unknown_grant_cannot_be_exported_as_executable_text(self):
        a=account(grants=['GRANT SELECT ON d.t; DROP USER root TO app@%'])
        with self.assertRaises(ValueError):
            A.export_sql(side([a]))

    def test_missing_direct_default_grant_refuses(self):
        a=account(default_roles=[{'name':'r','host':'%'}])
        with self.assertRaises(ValueError):
            A.export_sql(side([a,account('r',role=True)]))
