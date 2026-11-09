"""Every recorded catalog name remains usable at its source-defined scopes."""
import os
import tempfile
import unittest
from unittest import mock
from myflames import accounts, privileges
from _all_grant_cases import cases, scope, plan

class TestAllGrantCoverage(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        env = mock.patch.dict(os.environ, HOME=home.name, USERPROFILE=home.name, MYFLAMES_SECRET_BACKEND='vault')
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)

    def test_every_server_privilege_is_represented(self):
        for case in cases():
            with self.subTest(server=case['id']):
                present = {privileges.normalize_privilege(r[0], case['engine']) for r in case['rows']}
                shown = {p['name'] for entries in case['catalog'].values() for p in entries}
                self.assertEqual(present - {'USAGE', 'GRANT OPTION'}, shown)
                self.assertEqual({p['name'] for p in case['catalog']['column']}, {'SELECT','INSERT','UPDATE','REFERENCES'})
                self.assertEqual({p['name'] for p in case['catalog']['proxy']}, {'PROXY'})
                for level, entries in case['catalog'].items():
                    if level != 'global': self.assertFalse(any(p['dynamic'] for p in entries))

    def test_every_catalog_privilege_can_be_planned_for_grant_and_revoke(self):
        count = 0
        for case in cases():
            for level, entries in case['catalog'].items():
                for item in entries:
                    for operation in ('grant', 'revoke'):
                        with self.subTest(server=case['id'], level=level, privilege=item['name'], operation=operation):
                            request = dict(scope(level), operation=operation, privileges=[item['name']])
                            value = plan(case, request, present=operation == 'revoke')
                            self.assertTrue(value['statements'])
                            self.assertIn(operation.upper() + ' ' + item['name'], value['statements'][0])
                            self.assertEqual(value['request']['level'], level)
                            count += 1
        self.assertGreater(count, 800)

    def test_package_grants_select_oracle_mode_and_restore_exact_session_mode(self):
        for case in cases():
            if case['engine'] != 'mariadb': continue
            for kind in ('PACKAGE', 'PACKAGE BODY'):
                for operation in ('grant', 'revoke'):
                    with self.subTest(server=case['id'], kind=kind, operation=operation):
                        value = plan(case, dict(scope('routine', kind), operation=operation, privileges=['EXECUTE']), operation == 'revoke')
                        sql = value['statements'][0]
                        self.assertIn("'ORACLE'", sql)
                        self.assertIn('SET @myflames_accounts_sql_mode = @@SESSION.sql_mode;', sql)
                        self.assertTrue(sql.endswith('SET SESSION sql_mode = @myflames_accounts_sql_mode'))
                        self.assertIn(operation.upper() + ' EXECUTE ON ' + kind, sql)

    def test_library_and_anonymous_proxy_plans(self):
        for case in cases():
            request = dict(scope('proxy', anonymous=True), operation='grant', privileges=['PROXY'], grant_option=True)
            self.assertIn('PROXY ON ``@`%`', plan(case, request)['statements'][0])
            if case['engine'] == 'mysql':
                for name in ('EXECUTE', 'ALTER ROUTINE', 'GRANT OPTION'):
                    request = dict(scope('routine', 'LIBRARY'), operation='grant', privileges=[name])
                    if privileges.version_tuple(case['version']) < (9,2,0):
                        with self.assertRaisesRegex(ValueError, '9.2'): plan(case, request)
                    else:
                        self.assertIn(name + ' ON LIBRARY', plan(case, request)['statements'][0])
