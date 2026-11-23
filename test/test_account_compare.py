"""Credential-free grant comparisons and SQL exports for both engines."""
import copy
import json
from pathlib import Path
import re
import unittest
from unittest import mock

from myflames import account_compare as A, accounts, dbops, param_compare as P, privileges
from myflames.connection_groups import ProfileReadError
from myflames.connector import ConnectorError, PartialReadError
from test_accounts import FakeConn
from test_chart_data import validate
from _reviewed_replay import evidence_options, answer_evidence


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
    @evidence_options
    def run(self, sql, extra_flags=None, timeout=None):
        evidence = answer_evidence(sql, extra_flags, timeout,
                                   lambda body, flags, limit: self.run(body, extra_flags=flags, timeout=limit),
                                   self.evidence_identity())
        if evidence is not None:
            return evidence
        units = dbops.split_statements(sql)
        if len(units) > 1:
            out = []
            for unit in units:
                text = unit['text'].strip()
                stripped = text[len(accounts.TAG):].strip() if text.startswith(accounts.TAG) else text
                if stripped == 'SET SESSION print_identified_with_as_hex = ON':
                    continue
                if stripped.startswith("SELECT '") and stripped.endswith("'") and stripped.count("'") == 2:
                    out.append(stripped[8:-1] + '\n')
                    continue
                out.append(self.run(text, extra_flags=extra_flags, timeout=timeout))
            return ''.join(out)
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
    def test_oversized_grants_are_not_compared_or_exported(self):
        hostile = 'PASSWORD' + ' ' * accounts.MAX_ACCOUNT_TEXT_CHARS + 'never-return-this-secret'
        original = dbops.strip_credentials

        def bounded(text):
            self.assertLessEqual(len(text), accounts.MAX_ACCOUNT_TEXT_CHARS)
            return original(text)

        source = side([account(grants=[hostile])])
        with mock.patch.object(dbops, 'strip_credentials', side_effect=bounded):
            report = A.compare(source, copy.deepcopy(source))
            self.assertEqual(A.normalize_grant(hostile), A.normalize_grant(accounts.OMITTED_ACCOUNT_TEXT))
            with self.assertRaisesRegex(ValueError, 'completely read'):
                A.compare_many({'a': source, 'b': copy.deepcopy(source)})
            with self.assertRaisesRegex(ValueError, 'could not be read'):
                A.export_sql(source)
            with self.assertRaisesRegex(ValueError, 'safely exported'):
                A._grant_sql(hostile, account(), 'mysql')
        row = report['items'][0]
        self.assertEqual(row['status'], 'not_compared')
        self.assertEqual(row['source']['grants'], [accounts.OMITTED_ACCOUNT_TEXT])
        self.assertIn('omitted: too long', row['reason'])
        self.assertFalse(report['identical'])
        self.assertFalse(report['complete'])
        self.assertNotIn('never-return-this-secret', json.dumps(report))

    def test_capture_marks_oversized_grants_unreadable(self):
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                conn = CaptureConn(engine)
                conn.grants = ['PASSWORD' + ' ' * accounts.MAX_ACCOUNT_TEXT_CHARS + 'x']
                result = A.capture(conn)
                self.assertEqual(result['items'][0]['grants'], [accounts.OMITTED_ACCOUNT_TEXT])
                self.assertTrue(A.incomplete_account(result['items'][0]))
                self.assertEqual(A.compare(result, result)['items'][0]['status'], 'not_compared')

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


class _BatchServerState(object):
    """In-memory MySQL/MariaDB account fixture shared by per-account and script fakes."""
    def __init__(self, engine='mysql', version=None, version_comment=None, partial_revokes='OFF',
                 entries=None, role_edges=None, default_roles=None):
        self.engine = engine
        self.version = version or ('11.4.13-MariaDB' if engine == 'mariadb' else '8.4.11')
        self.version_comment = version_comment or (
            'MariaDB Server' if engine == 'mariadb' else 'Percona Server (GPL)')
        self.partial_revokes = partial_revokes
        self.entries = list(entries or [])
        self.role_edges = list(role_edges or [])
        self.default_roles = list(default_roles or [])
        self.by_quoted = {}
        for entry in self.entries:
            quoted = accounts._name(entry['name'], entry['host'], engine, entry.get('role', False))
            self.by_quoted[quoted] = entry

    def evidence_identity(self):
        from _reviewed_replay import UUID
        values = {'server_id': '42', 'hostname': 'batch-server', 'port': '3306',
                  'version': self.version, 'version_comment': self.version_comment}
        if self.engine == 'mysql':
            values['server_uuid'] = UUID
        return values

    @staticmethod
    def _escape_cell(value):
        if value is None:
            return 'NULL'
        return str(value).replace('\\', '\\\\').replace('\0', '\\0').replace('\t', '\\t').replace('\n', '\\n')

    def _emit_rows(self, rows):
        return ''.join('\t'.join(self._escape_cell(c) for c in row) + '\n' for row in rows)

    def execute_statement(self, raw_sql, session):
        sql = raw_sql.strip().rstrip(';').strip()
        sql = re.sub(r'^/\*myflames:[^*]*\*/\s*', '', sql)
        if sql.startswith('SET SESSION print_identified_with_as_hex'):
            if self.engine != 'mysql' or privileges.version_tuple(self.version) < (8, 0, 17):
                raise ConnectorError("ERROR 1193 (HY000) at line 1: Unknown system variable 'print_identified_with_as_hex'")
            session['hex_auth'] = True
            return ''
        if sql == 'SELECT @@version':
            return self.version + '\n'
        if sql.startswith("SELECT '") and sql.endswith("'") and sql.count("'") == 2:
            return sql[8:-1] + '\n'
        if sql.startswith('SHOW GLOBAL VARIABLES WHERE Variable_name IN ('):
            identity = self.evidence_identity()
            rows = []
            for key in dbops._EVIDENCE_VARIABLES:
                if ("'" + key + "'") in sql and key in identity:
                    rows.append([key, identity[key]])
            if "'partial_revokes'" in sql and self.engine == 'mysql' and privileges.version_tuple(self.version) >= (8, 0, 16):
                rows.append(['partial_revokes', self.partial_revokes])
            return self._emit_rows(rows)
        if sql == "SHOW GLOBAL STATUS WHERE Variable_name = 'wsrep_gcomm_uuid'":
            return ''
        if sql == "SHOW GLOBAL VARIABLES WHERE Variable_name = 'version_comment'":
            return self._emit_rows([['version_comment', self.version_comment]])
        if sql == "SHOW GLOBAL VARIABLES WHERE Variable_name = 'partial_revokes'":
            if self.engine == 'mysql' and privileges.version_tuple(self.version) >= (8, 0, 16):
                return self._emit_rows([['partial_revokes', self.partial_revokes]])
            return ''
        if sql.startswith('SELECT User, Host, ssl_type, HEX(ssl_cipher), HEX(x509_issuer), HEX(x509_subject) FROM mysql.user'):
            rows = []
            for e in self.entries:
                if e.get('tls') is not None:
                    t = e['tls']
                    rows.append([e['name'], e['host'], t['type'], t['cipher_hex'], t['issuer_hex'], t['subject_hex']])
            return self._emit_rows(rows)
        if 'FROM mysql.user ORDER BY User, Host' in sql:
            include_tls = 'ssl_type' in sql
            inline_granted = 'FROM mysql.role_edges' in sql
            granted = {(r[0], r[1]) for r in self.role_edges}
            rows = []
            for e in self.entries:
                row = [
                    e['name'], e['host'], e.get('plugin', 'caching_sha2_password'),
                    'Y' if e.get('locked') else 'N', 'Y' if e.get('expired') else 'N',
                    e.get('password_last_changed'), e.get('password_lifetime'),
                    '1' if e.get('no_password') else '0', '1' if e.get('empty_auth') else '0',
                ]
                if include_tls:
                    t = e.get('tls') or {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''}
                    row.extend([t['type'], t['cipher_hex'], t['issuer_hex'], t['subject_hex']])
                if inline_granted:
                    row.append('1' if (e['name'], e['host']) in granted else '0')
                rows.append(row)
            return self._emit_rows(rows)
        if 'FROM mysql.role_edges' in sql:
            return self._emit_rows(self.role_edges)
        if 'FROM mysql.default_roles' in sql:
            return self._emit_rows(self.default_roles)
        if 'FROM mysql.global_priv ORDER BY User, Host' in sql:
            rows = []
            for e in self.entries:
                rows.append([
                    e['name'], e['host'], e.get('plugin', 'mysql_native_password'),
                    '1' if e.get('locked') else '0',
                    '0' if e.get('expired') and not e.get('role') else '123456',
                    e.get('password_lifetime'),
                    '1' if e.get('role') else '0',
                    e.get('default_role_name', ''),
                    '1' if e.get('no_password') else '0',
                ])
            return self._emit_rows(rows)
        if 'FROM mysql.roles_mapping' in sql:
            return self._emit_rows(self.role_edges)
        if sql.startswith('SHOW GRANTS FOR '):
            target = sql[len('SHOW GRANTS FOR '):].strip()
            entry = self.by_quoted.get(target)
            if entry is None:
                raise ConnectorError('ERROR 1141 (42000) at line 1: There is no such grant defined for user')
            if entry.get('grants_error'):
                raise ConnectorError(entry['grants_error'])
            return self._emit_rows([[line] for line in entry.get('grants', [])])
        if sql.startswith('SHOW CREATE USER '):
            target = sql[len('SHOW CREATE USER '):].strip()
            entry = self.by_quoted.get(target)
            if entry is None:
                raise ConnectorError('ERROR 1396 (HY000) at line 1: Operation SHOW CREATE USER failed')
            if entry.get('create_error'):
                raise ConnectorError(entry['create_error'])
            create = entry.get('create', '')
            return self._emit_rows([[create]]) if create else ''
        raise AssertionError('Unexpected SQL in _BatchServerState: ' + raw_sql)


class _PerAccountFakeConn(object):
    """Fake without run_script: always exercises the per-account capture path."""
    host, port, user = '127.0.0.1', 3306, 'admin'
    query_timeout = 30

    def __init__(self, state):
        self.state = state
        self.calls = []

    def server_version(self):
        return self.state.version

    def is_mariadb(self):
        return self.state.engine == 'mariadb'

    @evidence_options
    def run(self, sql, extra_flags=None, timeout=None):
        evidence = answer_evidence(sql, extra_flags, timeout,
                                   lambda body, flags, limit: self.run(body, extra_flags=flags, timeout=limit),
                                   self.state.evidence_identity())
        if evidence is not None:
            return evidence
        self.calls.append(sql)
        session = {}
        out = []
        for unit in dbops.split_statements(sql):
            out.append(self.state.execute_statement(unit['text'], session))
        return ''.join(out)


class _ScriptFakeConn(_PerAccountFakeConn):
    """Fake with run_script (--force, line-numbered stderr) for non-evidence batched capture."""
    def __init__(self, state):
        super(_ScriptFakeConn, self).__init__(state)
        self.scripts = []

    def run_script(self, sql, extra_flags=None, timeout=None):
        self.scripts.append(sql)
        session = {}
        stdout_parts, stderr_lines = [], []
        lines = sql.replace('\r\n', '\n').split('\n')
        for lineno, line in enumerate(lines, 1):
            stmt = line.strip()
            if not stmt:
                continue
            try:
                stdout_parts.append(self.state.execute_statement(stmt, session))
            except ConnectorError as exc:
                msg = str(exc)
                if 'at line ' in msg:
                    msg = re.sub(r'at line \d+:', 'at line %d:' % lineno, msg)
                else:
                    msg = msg.replace(': ', ' at line %d: ' % lineno, 1)
                stderr_lines.append(msg)
        return ''.join(stdout_parts), ('\n'.join(stderr_lines) + '\n' if stderr_lines else '')


class _SlowScriptFakeConn(_ScriptFakeConn):
    """Fake transport whose cost per statement makes oversized batches time out."""
    def __init__(self, state, seconds_per_statement):
        super(_SlowScriptFakeConn, self).__init__(state)
        self.seconds_per_statement = seconds_per_statement
        self.timeouts = 0

    def run_script(self, sql, extra_flags=None, timeout=None):
        limit = timeout if timeout is not None else self.query_timeout
        statements = len([line for line in sql.split('\n') if line.strip()])
        if statements * self.seconds_per_statement > limit:
            self.timeouts += 1
            raise ConnectorError('Query timed out after %ss.' % limit)
        return super(_SlowScriptFakeConn, self).run_script(sql, extra_flags=extra_flags, timeout=timeout)


class _EvidenceFakeConn(object):
    """Fake transport (no run_script) executing real TargetEvidenceConnection DELIMITER packets."""
    host, port, user = '127.0.0.1', 3306, 'admin'
    query_timeout = 30

    def __init__(self, state, identity_by_packet=None, error_by_packet=None, corrupt_packet=None):
        self.state = state
        self.identity_by_packet = dict(identity_by_packet or {})
        self.error_by_packet = dict(error_by_packet or {})
        self.corrupt_packet = dict(corrupt_packet or {})
        self.raw_packets = []
        self.packet_statements = []

    def run(self, sql, extra_flags=None, timeout=None, retain_partial_stdout=False):
        packet_index = len(self.raw_packets)
        self.raw_packets.append((sql, list(extra_flags or []), retain_partial_stdout))
        if packet_index in self.error_by_packet:
            raise self.error_by_packet[packet_index]
        assert sql.startswith('DELIMITER '), sql
        header, rest = sql.split('\n', 1)
        delimiter = header[len('DELIMITER '):]
        assert rest.endswith(delimiter + '\n'), sql
        assert rest.count(delimiter) == 1, 'evidence prefix and chunk must travel in one COM_QUERY packet'
        packet_body = rest[:-len(delimiter) - 1]
        stmts = [unit['text'] for unit in dbops.split_statements(packet_body)]
        self.packet_statements.append(stmts)
        identity = dict(self.state.evidence_identity())
        if packet_index in self.identity_by_packet:
            identity.update(self.identity_by_packet[packet_index])
        session = {}
        stdout_parts = []
        for idx, stmt in enumerate(stmts):
            try:
                if idx == 0 and 'SHOW GLOBAL VARIABLES WHERE Variable_name IN (' in stmt:
                    rows = [[k, identity[k]] for k in dbops._EVIDENCE_VARIABLES
                            if ("'" + k + "'") in stmt and k in identity]
                    stdout_parts.append(self.state._emit_rows(rows))
                else:
                    stdout_parts.append(self.state.execute_statement(stmt, session))
            except ConnectorError as exc:
                code = dbops.error_code(str(exc))
                msg = re.sub(r'\bat line \d+:', 'at line 2:', str(exc))
                if not re.search(r'\bat line 2:', msg):
                    msg = re.sub(r'(ERROR\s+\d+(?:\s+\([^)]*\))?):', r'\1 at line 2:', msg)
                if retain_partial_stdout and code is not None and not (2000 <= code <= 2999):
                    raise PartialReadError(msg, ''.join(stdout_parts)) from None
                raise ConnectorError(msg) from None
        text = ''.join(stdout_parts)
        if packet_index in self.corrupt_packet:
            return self.corrupt_packet[packet_index](text)
        return text


def _mysql84_parity_state():
    oversized = 'PASSWORD' + ' ' * accounts.MAX_ACCOUNT_TEXT_CHARS + 'never-leak-oversized'
    entries = [
        {
            'name': 'app', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': False,
            'tls': {'type': 'SPECIFIED', 'cipher_hex': '414553', 'issuer_hex': '', 'subject_hex': ''},
            'grants': [
                'GRANT USAGE ON *.* TO `app`@`%`',
                'GRANT SELECT, INSERT ON `shop`.* TO `app`@`%`',
                'REVOKE SELECT ON `mysql`.* FROM `app`@`%`',
                'GRANT `reader`@`%` TO `app`@`%` WITH ADMIN OPTION',
            ],
            'create': ("CREATE USER `app`@`%` IDENTIFIED WITH 'caching_sha2_password' AS 0x48415348534543524554 "
                       "DEFAULT ROLE `reader`@`%` REQUIRE CIPHER 'AES' WITH MAX_QUERIES_PER_HOUR 100 "
                       "PASSWORD EXPIRE INTERVAL 90 DAY ACCOUNT UNLOCK PASSWORD HISTORY 5 "
                       "FAILED_LOGIN_ATTEMPTS 3 PASSWORD_LOCK_TIME 2"),
        },
        {
            'name': 'a`b', 'host': '10.0.%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': True,
            'tls': {'type': 'ANY', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT SELECT ON *.* TO `a``b`@`10.0.%`'],
            'create': "CREATE USER `a``b`@`10.0.%` IDENTIFIED WITH 'caching_sha2_password' AS 0xDEADBEEF REQUIRE SSL PASSWORD EXPIRE ACCOUNT UNLOCK",
        },
        {
            'name': 'reader', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': True, 'expired': True, 'empty_auth': True, 'role': True,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT SELECT ON `shop`.* TO `reader`@`%`'],
            'create': "CREATE USER `reader`@`%` IDENTIFIED WITH 'caching_sha2_password' REQUIRE NONE PASSWORD EXPIRE ACCOUNT LOCK",
        },
        {
            'name': 'mysql.session', 'host': 'localhost', 'plugin': 'caching_sha2_password',
            'locked': True, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT SUPER ON *.* TO `mysql.session`@`localhost`'],
            'create': "CREATE USER `mysql.session`@`localhost` IDENTIFIED WITH 'caching_sha2_password' REQUIRE NONE PASSWORD EXPIRE DEFAULT ACCOUNT LOCK",
        },
        {
            'name': 'gone_1141', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants_error': 'ERROR 1141 (42000) at line 1: There is no such grant defined for user',
        },
        {
            'name': 'gone_1133', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants_error': "ERROR 1133 (42000) at line 1: Can't find any matching row in the user table",
        },
        {
            'name': 'denied_grants', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants_error': 'ERROR 1227 (42000) at line 1: Access denied; you need SELECT privilege',
        },
        {
            'name': 'denied_create_user', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT USAGE ON *.* TO `denied_create_user`@`%`'],
            'create_error': 'ERROR 1142 (42000) at line 1: SELECT command denied for table user',
        },
        {
            'name': 'denied_create_role', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': True, 'expired': True, 'empty_auth': True, 'role': True,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT USAGE ON *.* TO `denied_create_role`@`%`'],
            'create_error': 'ERROR 1227 (42000) at line 1: Access denied',
        },
        {
            'name': 'huge_grant', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': [oversized],
            'create': "CREATE USER `huge_grant`@`%` IDENTIFIED WITH 'caching_sha2_password' REQUIRE NONE PASSWORD EXPIRE DEFAULT ACCOUNT UNLOCK",
        },
    ]
    role_edges = [['reader', '%', 'app', '%', 'Y']]
    default_roles = [['app', '%', 'reader', '%']]
    return _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'ON',
                             entries, role_edges, default_roles)


def _mysql57_parity_state():
    entries = [
        {
            'name': 'legacy', 'host': '%', 'plugin': 'mysql_native_password',
            'locked': False, 'expired': False,
            'tls': {'type': 'ANY', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ["GRANT USAGE ON *.* TO 'legacy'@'%'", "GRANT SELECT ON `db`.* TO 'legacy'@'%'"],
            'create': ("CREATE USER 'legacy'@'%' IDENTIFIED WITH 'mysql_native_password' AS '*0123456789ABCDEF0123456789ABCDEF01234567' "
                       "REQUIRE SSL PASSWORD EXPIRE NEVER ACCOUNT UNLOCK"),
        },
        {
            'name': 'missing57', 'host': '%', 'plugin': 'mysql_native_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants_error': 'ERROR 1141 (42000) at line 1: There is no such grant',
        },
        {
            'name': 'nocreate57', 'host': '%', 'plugin': 'mysql_native_password',
            'locked': True, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ["GRANT USAGE ON *.* TO 'nocreate57'@'%'"],
            'create_error': 'ERROR 1142 (42000) at line 1: SELECT command denied',
        },
    ]
    return _BatchServerState('mysql', '5.7.44', 'MySQL Community Server (GPL)', 'OFF', entries)


def _mariadb_parity_state():
    entries = [
        {
            'name': 'app', 'host': '%', 'plugin': 'mysql_native_password',
            'locked': False, 'expired': False, 'default_role_name': 'reader',
            'tls': {'type': 'ANY', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': [
                "GRANT USAGE ON *.* TO `app`@`%` IDENTIFIED BY PASSWORD '*0123456789ABCDEF0123456789ABCDEF01234567'",
                'GRANT `reader` TO `app`@`%` WITH ADMIN OPTION',
                'SET DEFAULT ROLE `reader` FOR `app`@`%`',
            ],
            'create': ("CREATE USER `app`@`%` IDENTIFIED BY PASSWORD '*0123456789ABCDEF0123456789ABCDEF01234567' "
                       "WITH MAX_STATEMENT_TIME 2.500000 ACCOUNT UNLOCK"),
        },
        {
            'name': 'reader', 'host': '', 'plugin': '', 'role': True,
            'locked': False, 'expired': False,
            'grants': ['GRANT SELECT ON `shop`.* TO `reader`', 'GRANT INSERT ON `shop`.* TO `other_role`'],
        },
        {
            'name': 'PUBLIC', 'host': '', 'plugin': '', 'role': True,
            'locked': False, 'expired': False,
            'grants': ['GRANT USAGE ON *.* TO PUBLIC'],
        },
        {
            'name': 'mariadb.sys', 'host': 'localhost', 'plugin': 'mysql_native_password',
            'locked': True, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT USAGE ON *.* TO `mariadb.sys`@`localhost`'],
            'create': "CREATE USER `mariadb.sys`@`localhost` IDENTIFIED VIA mysql_native_password USING 'invalid' ACCOUNT LOCK",
        },
        {
            'name': 'gone_maria', 'host': '%', 'plugin': 'mysql_native_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants_error': 'ERROR 1141 (42000) at line 1: There is no such grant defined for user',
        },
        {
            'name': 'denied_maria_grants', 'host': '%', 'plugin': 'mysql_native_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants_error': 'ERROR 1142 (42000) at line 1: SELECT command denied',
        },
        {
            'name': 'denied_maria_create', 'host': '%', 'plugin': 'mysql_native_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT USAGE ON *.* TO `denied_maria_create`@`%`'],
            'create_error': 'ERROR 1227 (42000) at line 1: Access denied',
        },
    ]
    role_edges = [['%', 'app', 'reader', 'Y']]
    return _BatchServerState('mariadb', '11.4.13-MariaDB', 'mariadb.org binary distribution', 'OFF',
                             entries, role_edges)


def _healthy_mysql_entries(count, prefix='u'):
    return [{
        'name': '%s%03d' % (prefix, i), 'host': '%', 'plugin': 'caching_sha2_password',
        'locked': (i % 7 == 0), 'expired': (i % 11 == 0),
        'tls': {'type': 'ANY' if i % 5 == 0 else '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
        'grants': ['GRANT SELECT ON *.* TO `%s%03d`@`%%`' % (prefix, i)],
        'create': ("CREATE USER `%s%03d`@`%%` IDENTIFIED WITH 'caching_sha2_password' AS 0xCAFEBABE%04d "
                   "REQUIRE %s PASSWORD EXPIRE%s ACCOUNT %s" % (
                       prefix, i, i, 'SSL' if i % 5 == 0 else 'NONE',
                       '' if i % 11 == 0 else ' DEFAULT',
                       'LOCK' if i % 7 == 0 else 'UNLOCK')),
    } for i in range(count)]


def _capture_unchecked(conn):
    """Exercise the legacy script/per-account paths after connection selection.

    Public capture now selects evidence on ordinary servers. Keep the existing
    parity tests covering the unchecked fallback's readers independently.
    """
    return A._capture(P.BoundedConnection(conn), conn)


class BatchedCaptureParityTest(unittest.TestCase):
    def test_evidence_batch_uses_remaining_member_budget(self):
        from myflames.topology_group import _ReadConnection
        state = _BatchServerState(entries=_healthy_mysql_entries(50))
        for seconds, succeeds in ((90, True), (50, False)):
            with self.subTest(seconds=seconds):
                clock = [1000.0]

                class SlowEvidenceConn(_EvidenceFakeConn):
                    def __init__(self):
                        super(SlowEvidenceConn, self).__init__(state)
                        self.batch_limits = []

                    def run(self, sql, extra_flags=None, timeout=None, retain_partial_stdout=False):
                        # The 100 account reads take 60 s together, but each
                        # individual statement fits the normal 30 s limit.
                        reads = sql.count('SHOW GRANTS FOR ') + sql.count('SHOW CREATE USER ')
                        cost = reads * 0.6 if reads else 1.0
                        limit = self.query_timeout if timeout is None else timeout
                        if reads:
                            self.batch_limits.append((reads, limit))
                        clock[0] += min(cost, limit)
                        if cost > limit:
                            raise ConnectorError('Query timed out after %ss.' % limit)
                        return super(SlowEvidenceConn, self).run(
                            sql, extra_flags=extra_flags, timeout=timeout,
                            retain_partial_stdout=retain_partial_stdout)

                with mock.patch.object(A.time, 'monotonic', side_effect=lambda: clock[0]):
                    transport = SlowEvidenceConn()
                    bounded = P.BoundedConnection(transport, seconds=seconds)
                    # Use the same wrapper chain as a Group member read.
                    evidence = dbops.TargetEvidenceConnection(_ReadConnection(bounded))
                    if succeeds:
                        result = A.capture(evidence)
                        evidence.check()
                        self.assertEqual(len(result['items']), 50)
                        self.assertFalse(any(A.incomplete_account(item) for item in result['items']))
                    else:
                        with self.assertRaises(ConnectorError):
                            A.capture(evidence)
                        with self.assertRaises(ConnectorError):
                            evidence.check()
                    self.assertEqual(transport.batch_limits, [(100, seconds - 2)])
                    self.assertLessEqual(clock[0], bounded.deadline)

    def test_compare_capture_checks_an_ordinary_server(self):
        state = _BatchServerState(entries=_healthy_mysql_entries(2))
        transport = _ScriptFakeConn(state)
        result = A.capture(transport)
        self.assertEqual([item['name'] for item in result['items']], ['u000', 'u001'])
        self.assertFalse(any(A.incomplete_account(item) for item in result['items']))
        self.assertEqual(transport.scripts, [])
        self.assertFalse(any('without the single-server check' in note for note in result['notes']))

    def test_compare_capture_refuses_a_router_that_changes_identity(self):
        state = _BatchServerState(entries=_healthy_mysql_entries(2))

        class RouterConn(_ScriptFakeConn):
            packets = 0

            @evidence_options
            def run(self, sql, extra_flags=None, timeout=None):
                identity = state.evidence_identity()
                if sql.startswith('DELIMITER '):
                    self.packets += 1
                    if self.packets > 1:
                        identity['server_uuid'] = 'bbbbbbbb-2222-3333-4444-555555555555'
                output = answer_evidence(
                    sql, extra_flags, timeout,
                    lambda body, flags, limit: self.run(body, extra_flags=flags, timeout=limit), identity)
                if output is not None:
                    return output
                return super(RouterConn, self).run(sql, extra_flags=extra_flags, timeout=timeout)

        transport = RouterConn(state)
        with self.assertRaises(ConnectorError):
            A.capture(transport)
        self.assertEqual(transport.packets, 2)
        self.assertEqual(transport.scripts, [])

    def test_compare_capture_identity_refusal_keeps_unchecked_reads_with_note(self):
        state = _BatchServerState(entries=_healthy_mysql_entries(2))

        class UnidentifiedConn(_ScriptFakeConn):
            @evidence_options
            def run(self, sql, extra_flags=None, timeout=None):
                output = answer_evidence(
                    sql, extra_flags, timeout,
                    lambda body, flags, limit: self.run(body, extra_flags=flags, timeout=limit), {})
                if output is not None:
                    return output
                return super(UnidentifiedConn, self).run(sql, extra_flags=extra_flags, timeout=timeout)

        transport = UnidentifiedConn(state)
        result = A.capture(transport)
        self.assertEqual([item['name'] for item in result['items']], ['u000', 'u001'])
        self.assertEqual(len(transport.scripts), 1)
        self.assertIn('Accounts were read without the single-server check.', result['notes'])

    def test_compare_capture_keeps_proxy_sides_without_account_catalogs_unavailable(self):
        class ProxySideConn(object):
            query_timeout = 30

            def __init__(self, version):
                self.version = version
                self.calls = []

            def run(self, sql, extra_flags=None, timeout=None, **kwargs):
                self.calls.append(sql)
                if sql.startswith('DELIMITER '):
                    # Admin/cache endpoints cannot supply the database node
                    # identity prefix. Their ordinary reads remain usable.
                    raise ConnectorError('ERROR 1064 (42000) at line 2: Unsupported statement')
                if sql == 'SELECT @@version':
                    return self.version + '\n'
                if 'FROM mysql.user ORDER BY User, Host' in sql:
                    raise ConnectorError("ERROR 1146 (42S02) at line 1: Table 'mysql.user' does not exist")
                raise AssertionError('Unexpected proxy-side SQL: ' + sql)

        for product, version, error_type, message in (
                ('ProxySQL Admin', '3.0.11-797-g7c91137', ValueError,
                 'Users and roles needs MySQL 5.7.6 or later, or MariaDB 10.11 or later.'),
                ('Readyset', '8.0.26', ConnectorError, 'Could not list accounts (error 1146).')):
            with self.subTest(product=product):
                # Check the old reader's outcome as well as public capture:
                # failed identity setup must not fabricate an empty inventory.
                for reader in (_capture_unchecked, A.capture):
                    transport = ProxySideConn(version)
                    with self.assertRaises(error_type) as caught:
                        reader(transport)
                    self.assertEqual(str(caught.exception), message)
                    self.assertFalse(any('SHOW GRANTS FOR ' in sql or 'SHOW CREATE USER ' in sql
                                         for sql in transport.calls))

    def test_batch_markers_have_independent_nonces_and_match_exactly(self):
        chunk = _healthy_mysql_entries(2)
        _results, _valid, _lines, _spans, markers, slots = A._build_batch_plan(
            chunk, 'mysql', '8.4.11', False)
        tokens = [marker.split(':', 1)[0] for marker in markers]
        self.assertEqual(len(set(tokens)), len(markers))
        # Knowing the first marker and the next slot label must not let a
        # server end the grants slot by printing a predicted later marker.
        predicted = tokens[0] + ':0:C'
        output = [markers[0], predicted, 'grant row', markers[1], 'create row',
                  markers[2], 'second grant', markers[3], 'second create', markers[4]]
        parsed = A._parse_framed_output('\n'.join(output) + '\n', markers, slots)
        self.assertEqual(parsed[('grants', 0)], [predicted, 'grant row'])
        self.assertEqual(parsed[('create', 0)], ['create row'])

    def test_batched_capture_parity_with_per_account_path(self):
        scenarios = (
            ('mysql84', _mysql84_parity_state(), False),
            ('mysql84_evidence', _mysql84_parity_state(), True),
            ('mysql57', _mysql57_parity_state(), False),
            ('mysql57_evidence', _mysql57_parity_state(), True),
            ('mariadb114', _mariadb_parity_state(), False),
            ('mariadb114_evidence', _mariadb_parity_state(), True),
        )
        for label, state, use_evidence in scenarios:
            with self.subTest(scenario=label):
                per_account_conn = _PerAccountFakeConn(state)
                if use_evidence:
                    transport = _EvidenceFakeConn(state)
                    c2 = dbops.TargetEvidenceConnection(P.BoundedConnection(transport))
                else:
                    transport = _ScriptFakeConn(state)
                    c2 = transport
                with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
                    expected = _capture_unchecked(per_account_conn)
                    actual = A.capture(c2) if use_evidence else _capture_unchecked(c2)
                if use_evidence:
                    c2.check()
                    self.assertFalse(hasattr(transport, 'run_script'))
                    self.assertEqual(sum(
                        any("Variable_name = 'version_comment'" in s for s in stmts)
                        for stmts in transport.packet_statements), 0)
                    if state.engine == 'mysql':
                        self.assertEqual(sum(
                            any('SELECT User, Host, ssl_type' in s for s in stmts)
                            for stmts in transport.packet_statements), 0)
                else:
                    self.assertEqual(len(transport.scripts), 1)
                    self.assertEqual(sum('partial_revokes' in s for s in transport.calls), 0)
                    if state.engine == 'mysql':
                        self.assertEqual(sum('SELECT User, Host, ssl_type' in s for s in transport.calls), 0)
                self.assertEqual(actual, expected)
                encoded = json.dumps(actual)
                for secret in ('48415348534543524554', 'DEADBEEF',
                               '0123456789ABCDEF0123456789ABCDEF01234567',
                               'never-leak-oversized'):
                    self.assertNotIn(secret, encoded)
                by_name = {i['name']: i for i in actual['items']}
                if state.engine == 'mysql' and privileges.version_tuple(state.version) >= (8, 0, 0):
                    self.assertFalse(by_name['gone_1141']['exists'])
                    self.assertIn('disappeared', by_name['gone_1141']['error'])
                    self.assertFalse(by_name['gone_1133']['exists'])
                    self.assertIn('disappeared', by_name['gone_1133']['error'])
                    self.assertIn('Could not read this account', by_name['denied_grants']['error'])
                    self.assertFalse(by_name['denied_create_user']['attributes_checked'])
                    self.assertFalse(by_name['denied_create_user']['defaults_checked'])
                    self.assertFalse(by_name['denied_create_role']['attributes_checked'])
                    self.assertTrue(by_name['denied_create_role']['defaults_checked'])
                    self.assertEqual(by_name['huge_grant']['grants'], [accounts.OMITTED_ACCOUNT_TEXT])
                    self.assertIn('too long', by_name['huge_grant']['error'])

    def test_evidence_batch_sends_one_run_packet_per_chunk_with_identity_prefix_and_all_statements(self):
        for label, engine, version, comment, count, expect_hex in (
                ('mysql84_two_chunks', 'mysql', '8.4.11', 'MySQL Community Server - GPL', 55, True),
                ('mysql57_single_chunk', 'mysql', '5.7.44', 'MySQL Community Server (GPL)', 4, False),
                ('mariadb114_single_chunk', 'mariadb', '11.4.13-MariaDB', 'mariadb.org binary distribution', 4, False)):
            with self.subTest(case=label):
                if engine == 'mariadb':
                    entries = [
                        {'name': 'app', 'host': '%', 'plugin': 'mysql_native_password',
                         'locked': False, 'expired': False, 'default_role_name': 'reader',
                         'tls': {'type': 'ANY', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
                         'grants': ["GRANT USAGE ON *.* TO `app`@`%` IDENTIFIED BY PASSWORD '*0123456789ABCDEF0123456789ABCDEF01234567'",
                                    'GRANT `reader` TO `app`@`%`', 'SET DEFAULT ROLE `reader` FOR `app`@`%`'],
                         'create': "CREATE USER `app`@`%` IDENTIFIED BY PASSWORD '*0123456789ABCDEF0123456789ABCDEF01234567' ACCOUNT UNLOCK"},
                        {'name': 'reader', 'host': '', 'plugin': '', 'role': True,
                         'locked': False, 'expired': False,
                         'grants': ['GRANT SELECT ON `shop`.* TO `reader`']},
                    ]
                    state = _BatchServerState(engine, version, comment, 'OFF', entries, [['%', 'app', 'reader', 'N']])
                else:
                    entries = _healthy_mysql_entries(count)
                    state = _BatchServerState(engine, version, comment, 'ON' if expect_hex else 'OFF', entries)
                per_account = _PerAccountFakeConn(state)
                transport = _EvidenceFakeConn(state)
                evidence = dbops.TargetEvidenceConnection(P.BoundedConnection(transport))
                with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
                    expected = _capture_unchecked(per_account)
                    actual = A.capture(evidence)
                evidence.check()
                self.assertEqual(actual, expected)
                batch_packets = [
                    stmts for stmts in transport.packet_statements
                    if any('SHOW GRANTS FOR ' in s for s in stmts)
                ]
                expected_chunks = (len(entries) + A.BATCH_CHUNK_SIZE - 1) // A.BATCH_CHUNK_SIZE
                self.assertEqual(len(batch_packets), expected_chunks)
                for chunk_idx, stmts in enumerate(batch_packets):
                    # Identity prefix is always the first 3 statements of the same COM_QUERY packet.
                    self.assertIn('SHOW GLOBAL VARIABLES WHERE Variable_name IN (', stmts[0])
                    self.assertIn("Variable_name = 'wsrep_gcomm_uuid'", stmts[1])
                    self.assertRegex(stmts[2], r"SELECT 'myflames_evidence_[0-9a-f]{32}'")
                    if expect_hex:
                        self.assertIn('SET SESSION print_identified_with_as_hex = ON', stmts[3])
                    else:
                        self.assertFalse(any('print_identified_with_as_hex' in s for s in stmts))
                    has_partial = any("Variable_name = 'partial_revokes'" in s for s in stmts)
                    self.assertEqual(has_partial, engine == 'mysql' and chunk_idx == 0)
                    chunk_entries = entries[chunk_idx * A.BATCH_CHUNK_SIZE:(chunk_idx + 1) * A.BATCH_CHUNK_SIZE]
                    grants_stmts = [s for s in stmts if 'SHOW GRANTS FOR ' in s]
                    create_stmts = [s for s in stmts if 'SHOW CREATE USER ' in s]
                    self.assertEqual(len(grants_stmts), len(chunk_entries))
                    expected_creates = sum(not (e.get('role') and engine == 'mariadb') for e in chunk_entries)
                    self.assertEqual(len(create_stmts), expected_creates)
                    self.assertTrue(any("SELECT 'myflames_acct_" in s for s in stmts))
                # Zero direct accesses to private TargetEvidenceConnection fields or run_script.
                self.assertFalse(hasattr(transport, 'run_script'))

    def test_evidence_packet_error_falls_back_to_per_account_reads_for_that_chunk(self):
        entries = _healthy_mysql_entries(55)
        entries[17]['grants_error'] = 'ERROR 1141 (42000) at line 1: There is no such grant'
        entries[25]['create_error'] = 'ERROR 1227 (42000) at line 1: Access denied'
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'ON', entries)
        per_account = _PerAccountFakeConn(state)
        transport = _EvidenceFakeConn(state)
        evidence = dbops.TargetEvidenceConnection(P.BoundedConnection(transport))
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(per_account)
            actual = A.capture(evidence)
        evidence.check()
        self.assertEqual(actual, expected)
        # Chunk 0's batch failed at entry 17 and fell back to per-account reads;
        # chunk 1 (entries 50..54) still ran in a single batched evidence packet.
        multi_grant_packets = [
            stmts for stmts in transport.packet_statements
            if sum('SHOW GRANTS FOR ' in s for s in stmts) > 1
        ]
        self.assertEqual(len(multi_grant_packets), 2)
        self.assertEqual(sum('SHOW GRANTS FOR ' in s for s in multi_grant_packets[0]), 50)
        self.assertEqual(sum('SHOW GRANTS FOR ' in s for s in multi_grant_packets[1]), 5)
        single_grant_packets = [
            stmts for stmts in transport.packet_statements
            if sum('SHOW GRANTS FOR ' in s for s in stmts) == 1
        ]
        self.assertEqual(len(single_grant_packets), 50)
        # partial_revokes is still read only once during the fallback (and not on chunk 1).
        self.assertEqual(sum(
            1 for stmts in transport.packet_statements
            if len(stmts) == 4 and "Variable_name = 'partial_revokes'" in stmts[3]), 1)

    def test_evidence_incomplete_markers_never_attribute_partial_output(self):
        entries = _healthy_mysql_entries(4)
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'OFF', entries)
        per_account = _PerAccountFakeConn(state)
        # Packet 0 = server_version, Packet 1 = list_accounts, Packet 2 = chunk 0 batch.
        # Truncate packet 2 after the second account's grants, dropping the remaining markers.
        def truncate(text):
            idx = text.find('u001')
            return text[:idx + 4] + '\n'
        transport = _EvidenceFakeConn(state, corrupt_packet={2: truncate})
        evidence = dbops.TargetEvidenceConnection(P.BoundedConnection(transport))
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(per_account)
            actual = A.capture(evidence)
        evidence.check()
        self.assertEqual(actual, expected)
        single_grant_packets = [
            stmts for stmts in transport.packet_statements
            if sum('SHOW GRANTS FOR ' in s for s in stmts) == 1
        ]
        self.assertEqual(len(single_grant_packets), 4)

    def test_evidence_identity_change_between_chunks_fails_without_fallback(self):
        entries = _healthy_mysql_entries(55)
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'OFF', entries)
        # Packet 0 = server_version, Packet 1 = list_accounts, Packet 2 = chunk 0, Packet 3 = chunk 1.
        transport = _EvidenceFakeConn(
            state, identity_by_packet={3: {'server_uuid': 'bbbbbbbb-2222-3333-4444-555555555555'}})
        evidence = dbops.TargetEvidenceConnection(P.BoundedConnection(transport))
        with self.assertRaises(ConnectorError) as ctx:
            A.capture(evidence)
        self.assertEqual(getattr(ctx.exception, 'identity_outcome', None), 'identity_unavailable')
        with self.assertRaises(ConnectorError):
            evidence.check()
        # Only the 4 packets ran (no per-account fallback after chunk 1's identity change).
        self.assertEqual(len(transport.raw_packets), 4)

    def test_evidence_consent_error_and_timeout_surface_without_fallback(self):
        entries = _healthy_mysql_entries(3)
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'OFF', entries)
        # Packet 2 is chunk 0 batch.
        consent = ProfileReadError('This saved connection is outside the current connection group.')
        transport = _EvidenceFakeConn(state, error_by_packet={2: consent})
        evidence = dbops.TargetEvidenceConnection(P.BoundedConnection(transport))
        with self.assertRaises(ProfileReadError) as ctx:
            A.capture(evidence)
        self.assertIs(ctx.exception, consent)
        with self.assertRaises(ProfileReadError):
            evidence.check()
        self.assertEqual(len(transport.raw_packets), 3)

        timeout_err = ConnectorError('Query timed out after 30s: SHOW GRANTS')
        transport2 = _EvidenceFakeConn(state, error_by_packet={2: timeout_err})
        evidence2 = dbops.TargetEvidenceConnection(P.BoundedConnection(transport2))
        with self.assertRaises(ConnectorError) as ctx2:
            A.capture(evidence2)
        self.assertEqual(getattr(ctx2.exception, 'identity_outcome', None), 'identity_unavailable')
        with self.assertRaises(ConnectorError):
            evidence2.check()
        self.assertEqual(len(transport2.raw_packets), 3)

    def test_batched_capture_chunking_parity_over_50_accounts(self):
        entries = _healthy_mysql_entries(55)
        entries[17]['grants_error'] = 'ERROR 1141 (42000) at line 1: There is no such grant'
        entries[51]['create_error'] = 'ERROR 1227 (42000) at line 1: Access denied'
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'OFF', entries)
        c1, c2 = _PerAccountFakeConn(state), _ScriptFakeConn(state)
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(c1)
            actual = _capture_unchecked(c2)
        self.assertEqual(actual, expected)
        self.assertEqual(len(c2.scripts), 2)
        self.assertNotIn('CAFEBABE', json.dumps(actual))

    def test_batched_capture_falls_back_when_error_cannot_be_isolated(self):
        state = _mysql84_parity_state()
        expected_conn = _PerAccountFakeConn(state)
        broken_script_conn = _ScriptFakeConn(state)
        broken_script_conn.run_script = lambda sql, extra_flags=None, timeout=None: (
            'garbled-unframed-output\n', 'ERROR 1064: unnumbered syntax error\n')
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(expected_conn)
            actual = _capture_unchecked(broken_script_conn)
        self.assertEqual(actual, expected)
        # Fallback still reads partial_revokes only once per capture.
        self.assertEqual(sum('partial_revokes' in s for s in broken_script_conn.calls), 1)

    def test_evidence_batch_splits_when_names_cover_every_delimiter_candidate(self):
        # Server data: four valid account names that together contain every
        # _TARGET_DELIMITERS candidate, so no client delimiter can frame a
        # single packet for all of them. The batch must split instead of
        # failing the member.
        entries = []
        for index, candidate in enumerate(dbops._TARGET_DELIMITERS):
            entry = _healthy_mysql_entries(1, prefix='d%d' % index)[0]
            name = 'u%d%sx' % (index, candidate)
            assert candidate in name
            entry['name'] = name
            entry['grants'] = ['GRANT SELECT ON *.* TO `%s`@`%%`' % name]
            entry['create'] = ("CREATE USER `%s`@`%%` IDENTIFIED WITH 'caching_sha2_password' "
                               "REQUIRE NONE PASSWORD EXPIRE ACCOUNT UNLOCK" % name)
            entries.append(entry)
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'ON', entries)
        per_account = _PerAccountFakeConn(state)
        transport = _EvidenceFakeConn(state)
        evidence = dbops.TargetEvidenceConnection(P.BoundedConnection(transport))
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(per_account)
            actual = A.capture(evidence)
        # The member did not fail: the scope is intact and every account read.
        evidence.check()
        self.assertEqual(actual, expected)
        self.assertFalse(any(item.get('error') for item in actual['items']))
        self.assertEqual({item['name'] for item in actual['items']},
                         {entry['name'] for entry in entries})
        # Split halves travel as evidence packets of two accounts each; no
        # account needed the per-account fallback.
        batch_packets = [stmts for stmts in transport.packet_statements
                         if any('SHOW GRANTS FOR ' in s for s in stmts)]
        self.assertEqual([sum('SHOW GRANTS FOR ' in s for s in stmts) for stmts in batch_packets], [2, 2])

    def test_script_batch_timeout_splits_and_retries_inside_the_deadline(self):
        # 0.25s per statement: a 50-account batch (~204 statements) exceeds
        # the 30s call timeout while its halves and 10-account batches fit.
        entries = _healthy_mysql_entries(50)
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'ON', entries)
        per_account = _PerAccountFakeConn(state)
        slow = _SlowScriptFakeConn(state, 0.25)
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(per_account)
            actual = _capture_unchecked(slow)
        self.assertEqual(actual, expected)
        self.assertFalse(any(item.get('error') for item in actual['items']))
        # The 50-account batch timed out once; the two 25-account halves fit
        # the same call timeout, so no per-account fallback was needed.
        self.assertEqual(slow.timeouts, 1)
        self.assertEqual([sum('SHOW GRANTS FOR ' in line for line in script.split('\n'))
                          for script in slow.scripts], [25, 25])
        self.assertEqual(sum('partial_revokes' in call for call in slow.calls), 0)

    def test_script_batch_timeout_falls_back_to_per_account_reads(self):
        # 5.5s per statement makes even a one-account batch exceed the call
        # timeout; each statement then gets its own timeout per account.
        entries = _healthy_mysql_entries(10)
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'ON', entries)
        per_account = _PerAccountFakeConn(state)
        slow = _SlowScriptFakeConn(state, 5.5)
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(per_account)
            actual = _capture_unchecked(slow)
        self.assertEqual(actual, expected)
        self.assertFalse(any(item.get('error') for item in actual['items']))
        self.assertEqual(len(slow.scripts), 0)
        self.assertGreater(slow.timeouts, 0)
        # The per-account fallback still reads partial_revokes exactly once.
        self.assertEqual(sum('partial_revokes' in call for call in slow.calls), 1)

    def test_batched_capture_bounds_oversized_show_create_user(self):
        secret = 'SUPERSECRETHASH999999'
        entries = [{
            'name': 'bigcreate', 'host': '%', 'plugin': 'caching_sha2_password',
            'locked': False, 'expired': False,
            'tls': {'type': '', 'cipher_hex': '', 'issuer_hex': '', 'subject_hex': ''},
            'grants': ['GRANT USAGE ON *.* TO `bigcreate`@`%`'],
            'create': "CREATE USER `bigcreate`@`%` IDENTIFIED WITH 'caching_sha2_password' AS '" + ('A' * accounts.MAX_ACCOUNT_TEXT_CHARS) + secret + "'",
        }]
        state = _BatchServerState('mysql', '8.4.11', 'MySQL Community Server - GPL', 'OFF', entries)
        c1, c2 = _PerAccountFakeConn(state), _ScriptFakeConn(state)
        with mock.patch.object(A, 'now', return_value='2026-11-23T09:00:00+00:00'):
            expected = _capture_unchecked(c1)
            actual = _capture_unchecked(c2)
        self.assertEqual(actual, expected)
        self.assertFalse(actual['items'][0]['attributes_checked'])
        self.assertFalse(actual['items'][0]['defaults_checked'])
        self.assertNotIn(secret, json.dumps(actual))

