"""Dump option validation and native-tool mapping, without servers or user data."""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from myflames import dump_options as options, lifecycle
from test_lifecycle import ReplayConnection, CONNECTION


class DumpOptionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myflames-options-test-')
        self.addCleanup(self.temp.cleanup)
        patch = mock.patch.dict(os.environ, HOME=self.temp.name, USERPROFILE=self.temp.name,
                                MYFLAMES_SECRET_BACKEND='vault')
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.shell = {'name': 'mysqlsh', 'version': '9.6.0', 'engine': 'mysql',
                      'available': True, 'path': '/fake/mysqlsh'}
        self.sql = dict(self.shell, name='mysqldump', path='/fake/mysqldump', version='8.0.45')
        self.request = {'schemas': ['app'], 'tables': [], 'directory': '/tmp/dump', 'threads': 4}

    def test_default_sql_flags_unchanged(self):
        self.assertEqual(lifecycle._dump_args(self.request, self.sql), [
            '--single-transaction', '--quick', '--routines', '--events', '--triggers',
            '--hex-blob', '--add-drop-table', '--set-gtid-purged=OFF', '--databases', 'app'])
        self.assertEqual(options.normalize(None, self.sql), {})
        self.assertEqual(options.normalize({}, self.shell), {})

    def test_shell_compression_flags_and_sizes(self):
        values = {'compression': 'zstd', 'compression_level': 8, 'chunking': True,
                  'bytes_per_chunk': '128k', 'max_rate': '20M', 'triggers': False,
                  'routines': False, 'events': True}
        self.request['options'] = options.normalize(values, self.shell)
        expression = lifecycle._shell_dump_expression(self.request)
        args = json.loads('[' + expression[len('util.dumpSchemas('):-1] + ']')
        self.assertEqual(args[-1]['compression'], 'zstd;level=8')
        self.assertEqual(args[-1]['bytesPerChunk'], '128k')
        self.assertEqual(args[-1]['maxRate'], '20M')
        self.assertFalse(args[-1]['triggers'])
        self.assertTrue(args[-1]['consistent'])

    def test_sql_options_for_both_engines(self):
        for name, engine in [('mysqldump', 'mysql'), ('mariadb-dump', 'mariadb')]:
            tool = dict(self.sql, name=name, engine=engine)
            self.request['options'] = options.normalize({key: False for key in
                ('routines', 'events', 'triggers', 'hex_blob', 'add_drop_table')}, tool)
            self.request['options']['network_compression'] = True
            args = lifecycle._dump_args(self.request, tool)
            for flag in ('routines', 'events', 'triggers', 'hex-blob', 'add-drop-table'):
                self.assertIn('--skip-' + flag, args)
            self.assertIn('--compress', args)
            self.assertEqual('--set-gtid-purged=OFF' in args, engine == 'mysql')

    def test_unknown_keys_and_wrong_types_refuse(self):
        for values in ({'raw_flags': '--exec evil'}, {'threads': 3}, {'triggers': 1},
                       {'routines': 'false'}, [], {'compression': []},
                       {'compression': 'zstd;level=3'}, {'max_rate': '1M;evil'},
                       {'bytes_per_chunk': '127999'}, {'max_rate': '-1'},
                       {'max_rate': '2.5M'}, {'max_rate': '9' * 40},
                       {'chunking': False, 'bytes_per_chunk': '1M'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                options.normalize(values, self.shell)

    def test_levels_are_bounded_and_require_explicit_method(self):
        for method, maximum in [('gzip', 9), ('zstd', 22)]:
            for level in (0, maximum):
                self.assertEqual(options.normalize({'compression': method, 'compression_level': level},
                                                  self.shell)['compression_level'], level)
            for level in (-1, maximum + 1, True, '3'):
                with self.assertRaises(ValueError):
                    options.normalize({'compression': method, 'compression_level': level}, self.shell)
        for method in ('default', 'none'):
            with self.assertRaises(ValueError):
                options.normalize({'compression': method, 'compression_level': 3}, self.shell)
        for version in ('8.0.45', '', None):
            with self.assertRaisesRegex(ValueError, '8.4'):
                options.normalize({'compression': 'gzip', 'compression_level': 3},
                                  dict(self.shell, version=version))

    def test_tool_scope_capabilities_and_encryption(self):
        shell_keys = {d['key'] for d in options.capability_catalog('mysqlsh', True)['options']}
        self.assertNotIn('routines', shell_keys)
        self.assertNotIn('events', shell_keys)
        self.assertNotIn('compression_level', {d['key'] for d in
            options.capability_catalog('mysqlsh', tool_version='8.0.45')['options']})
        for key in ('routines', 'events'):
            with self.assertRaises(ValueError):
                options.normalize({key: False}, self.shell, table_scope=True)
        for values in ({'compression': 'gzip'}, {'chunking': False}, {'max_rate': '1M'}):
            with self.assertRaises(ValueError):
                options.normalize(values, self.sql)
        self.assertEqual(options.normalize({'encryption': 'none'}, self.shell), {})
        with self.assertRaisesRegex(ValueError, 'encryption of local backup files'):
            options.normalize({'encryption': 'aes256'}, self.shell)

    def test_catalog_groups_rows_and_short_encryption_line(self):
        groups = [key for key, _ in options.GROUPS]
        for name, scope in (('mysqlsh', False), ('mysqlsh', True), ('mysqldump', False), ('mariadb-dump', True)):
            catalog = options.capability_catalog(name, scope, tool_version='9.6.0')
            self.assertEqual([key for key, _ in catalog['groups']], groups)
            for item in catalog['options']:
                self.assertIn(item['group'], groups, item['key'])
                # Every checkbox names the row it shares; other controls label themselves.
                self.assertEqual('row' in item, item['type'] == 'boolean', item['key'])
            self.assertEqual(catalog['encryption']['summary'], 'Backup files are not encrypted.')
            self.assertEqual(catalog['encryption']['message'], options.ENCRYPTION_NOTE)
        shell = {d['key']: d for d in options.capability_catalog('mysqlsh', tool_version='9.6.0')['options']}
        self.assertEqual([k for k, d in shell.items() if d['group'] == 'performance'], ['chunking', 'bytes_per_chunk', 'max_rate'])
        self.assertEqual({shell[k]['row'] for k in ('triggers', 'routines', 'events')}, {'Include'})
        self.assertEqual(shell['max_rate']['unit'], 'per thread')
        sql = {d['key']: d for d in options.capability_catalog('mysqldump')['options']}
        self.assertEqual(sql['network_compression']['group'], 'compression')
        self.assertEqual({sql[k]['group'] for k in ('hex_blob', 'add_drop_table')}, {'output'})

    def test_prepare_binds_options_and_default_request_stays_compatible(self):
        destination = os.path.join(self.temp.name, 'dump')
        tools = {'tools': {'mysqldump': self.sql}}
        plain = lifecycle.prepare_dump(ReplayConnection(), CONNECTION, destination,
                                       schemas=['app'], tools=tools, prefer_shell=False)
        self.assertNotIn('options', plain['request'])
        changed = lifecycle.prepare_dump(ReplayConnection(), CONNECTION, destination,
            schemas=['app'], tools=tools, prefer_shell=False, options={'events': False})
        self.assertEqual(changed['request']['options'], {'events': False})
        self.assertNotEqual(plain['binding'], changed['binding'])
        self.assertIn('--skip-events', changed['commands'][0])
        tampered = copy.deepcopy(changed)
        tampered['request']['options']['events'] = True
        self.assertNotEqual(changed['binding'], lifecycle._plan_binding(tampered))
        self.assertFalse(os.path.exists(destination))

    def test_manifest_records_actual_selected_options(self):
        destination = os.path.join(self.temp.name, 'dump')
        tool = dict(self.sql)
        plan = lifecycle.prepare_dump(ReplayConnection(), CONNECTION, destination,
            schemas=['app'], tools={'tools': {'mysqldump': tool}}, prefer_shell=False,
            options={'triggers': False, 'events': False, 'network_compression': True})
        job = lifecycle.LifecycleJob('dump', lambda job: None, CONNECTION)

        def invoke(job, tool, connection, args, **kwargs):
            kwargs['output_file'].write(b'CREATE TABLE t(id INT);\n')
        with mock.patch.object(lifecycle, '_invoke', side_effect=invoke):
            result = lifecycle._perform_dump(job, ReplayConnection(), plan, CONNECTION)
        manifest = result['manifest']
        self.assertTrue(manifest['complete'])
        self.assertFalse(manifest['options']['events'])
        self.assertFalse(manifest['options']['triggers'])
        self.assertEqual(manifest['options']['selected'], plan['request']['options'])
        self.assertIn('--compress', manifest['options']['tool_options'])

    def test_privilege_hints_follow_selected_objects(self):
        for shell, tables, selected, expected in [
                (False, [], {'events': False, 'triggers': False}, ['SELECT']),
                (True, ['t'], {}, ['SELECT', 'TRIGGER']),
                (True, [], {'triggers': False}, ['SELECT', 'EVENT'])]:
            tool = self.shell if shell else self.sql
            with mock.patch.object(lifecycle, '_privileges_check', return_value={
                    'status': 'passed'}) as check, mock.patch.object(lifecycle, '_invoke'):
                lifecycle.prepare_dump(ReplayConnection(), CONNECTION,
                    os.path.join(self.temp.name, 'dump'), schemas=['app'], tables=tables,
                    tools={'tools': {tool['name']: tool}}, options=selected)
            self.assertEqual([group[0] for group in check.call_args[0][1]], expected)


if __name__ == '__main__':
    unittest.main()
