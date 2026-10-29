"""Schema CLI contracts using recorded reads; no database server is contacted."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
from _schema_replay import ReplayConn, recording, snapshot  # noqa: E402
from myflames import cli, schema_versions, schema_versions_cli  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.workspace_store import WorkspaceStore  # noqa: E402


class Stdin(io.StringIO):
    def __init__(self, text='', tty=False):
        super(Stdin, self).__init__(text)
        self.tty = tty

    def isatty(self):
        return self.tty


class SchemaVersionsCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()
        self.connections = []
        patcher = mock.patch.object(schema_versions, 'now', return_value='2026-10-26T12:00:00+00:00')
        patcher.start()
        self.addCleanup(patcher.stop)

        def connect(**options):
            server = 'mariadb-11.4' if options.get('user', '').startswith('maria') else 'mysql-8.4'
            which = 'before' if options.get('database') == 'sv_m' else 'after'
            if options.get('user', '').endswith('limited'):
                which = 'limited'
            conn = ReplayConn(recording(server, which))
            self.connections.append((options, conn))
            return conn
        patcher = mock.patch.object(schema_versions_cli, 'MySQLConnection', side_effect=connect)
        self.connection_factory = patcher.start()
        self.addCleanup(patcher.stop)

    def test_file_read_is_bounded_in_bytes_before_decoding(self):
        path = Path(os.environ['HOME']) / 'oversized.json'
        # More bytes than characters; the invalid tail must never be decoded.
        path.write_bytes(('é' * 100).encode('utf-8') + b'\xff')
        real_open = open
        for args in (['import', str(path)], ['compare', 'file:' + str(path), 'file:' + str(path)],
                     ['lint', 'file:' + str(path)]):
            with self.subTest(args=args):
                handles = []
                def tracked_open(*args, **kwargs):
                    handle = real_open(*args, **kwargs)
                    spy = mock.MagicMock(wraps=handle)
                    spy.__enter__.return_value = spy
                    spy.__exit__.side_effect = lambda *unused: handle.close()
                    handles.append(spy)
                    return spy
                with mock.patch.object(schema_versions, 'MAX_SNAPSHOT_BYTES', 128), \
                        mock.patch.object(schema_versions_cli, 'open', side_effect=tracked_open, create=True):
                    code, out, err = self.run_cli(args)
                self.assertEqual(code, 2, (out, err))
                self.assertIn('schema version file of at most', err)
                for handle in handles:
                    handle.read.assert_called_once_with(129)
                self.connection_factory.assert_not_called()
        self.assertEqual(self.store.schema_version_list()['items'], [])

    def test_file_read_decodes_an_exact_byte_limit(self):
        text = '{"note":"é"}'
        data = text.encode('utf-8')
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = data
        with mock.patch.object(schema_versions_cli, 'open', return_value=handle, create=True) as opened, \
                mock.patch.object(schema_versions, 'MAX_SNAPSHOT_BYTES', len(data)), \
                mock.patch.object(schema_versions, 'parse_file', return_value={'validated': True}) as parse:
            self.assertEqual(schema_versions_cli._read_file('snapshot.json'), {'validated': True})
        opened.assert_called_once_with('snapshot.json', 'rb')
        handle.__enter__.return_value.read.assert_called_once_with(len(data) + 1)
        parse.assert_called_once_with(text)

    def run_cli(self, argv, stdin=None, dispatch=False):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(sys, 'stdin', stdin if stdin is not None else Stdin()):
            try:
                if dispatch:
                    with mock.patch.object(sys, 'argv', ['myflames', 'schema'] + argv):
                        cli.main()
                else:
                    schema_versions_cli.cmd_schema(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def success(self, argv, **kwargs):
        code, out, err = self.run_cli(argv, **kwargs)
        self.assertEqual(code, 0, err)
        return out, err

    def failure(self, argv, contains, **kwargs):
        code, out, err = self.run_cli(argv, **kwargs)
        self.assertEqual(code, 2, err)
        self.assertEqual(out, '')
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn(contains, err)
        return err

    def save(self, database='sv_m', user='root', name='baseline', **kwargs):
        argv = ['save', '-D', database, '-u', user, '--name', name, '--json']
        for key, value in kwargs.items():
            argv.extend(['--' + key.replace('_', '-'), value])
        return json.loads(self.success(argv)[0])

    def export(self, item, filename):
        path = Path(self.temp.name) / filename
        out, err = self.success(['export', item['id'], '-o', str(path)])
        self.assertEqual(out, '')
        self.assertIn('Written to', err)
        return path

    def test_read_text_and_json_both_engines_and_dispatch(self):
        for user, engine in (('root', 'mysql'), ('maria', 'mariadb')):
            with self.subTest(engine=engine):
                out, err = self.success(['read', '-D', 'sv_m', '-u', user], dispatch=True)
                self.assertIn('Schema of sv_m', out)
                self.assertIn('customers', out)
                out, err = self.success(['read', '-D', 'sv_m', '-u', user, '--json'])
                result = json.loads(out)
                self.assertEqual(result['schema_version'], 'schema-snapshot-1.0')
                self.assertEqual(result['server']['engine'], engine)
                self.assertEqual(schema_versions.validate_snapshot(result)['checksum'], result['checksum'])
                self.assertEqual(err, '')
                self.assertEqual(self.connections[-1][0]['query_timeout'], schema_versions.READ_SECONDS)
        self.assertEqual(self.store.schema_version_list()['items'], [])

    def test_live_preserves_cli_connection_options(self):
        out, _ = self.success(['read', '-D', 'sv_m', '-h', 'db.local', '-P', '3310', '-u', 'root',
                               '-psecret', '--ssl-mode', 'REQUIRED', '--ssl-ca', '/ca.pem',
                               '--ssl-cert', '/cert.pem', '--ssl-key', '/key.pem',
                               '--mysql-binary', '/client/mysql', '--json'])
        options = self.connections[-1][0]
        self.assertEqual(options['host'], 'db.local')
        self.assertEqual(options['port'], 3310)
        self.assertEqual(options['password'], 'secret')
        for key, expected in (('ssl_mode', 'REQUIRED'), ('ssl_ca', '/ca.pem'), ('ssl_cert', '/cert.pem'),
                              ('ssl_key', '/key.pem'), ('binary', '/client/mysql')):
            self.assertEqual(options[key], expected)
        self.assertEqual(json.loads(out)['server']['host'], 'db.local')

    def test_save_twice_text_json_and_same_as_name(self):
        first = self.save()
        self.assertEqual(first['number'], 1)
        out, _ = self.success(['save', '-D', 'sv_m'])
        self.assertIn('Saved version 2 of sv_m (127.0.0.1:3306, MySQL 8.4.11)', out)
        self.assertIn('7 tables', out)
        self.assertIn('No changes since version 1 ("baseline").', out)
        items = self.store.schema_version_list()['items']
        self.assertEqual([i['number'] for i in items], [2, 1])
        out, _ = self.success(['save', '-D', 'sv_m', '--json'])
        third = json.loads(out)
        self.assertEqual(third['number'], 3)
        self.assertEqual(third['same_as']['number'], 2)
        out, _ = self.success(['save', '-D', 'sv_m'])
        self.assertIn('No changes since version 3.', out)

    def test_list_empty_table_filter_and_json(self):
        out, _ = self.success(['list'])
        self.assertEqual(out, 'No saved schema versions. Save one with: myflames schema save --profile NAME -D DATABASE\n')
        item = self.save()
        self.save(database='sv_b', name='after')
        out, _ = self.success(['list', '--database', 'sv_m'])
        for field in ('Version', 'Name', 'Database', 'Server', 'Product', 'Saved', 'Objects', 'ID',
                      'baseline', 'sv_m', '127.0.0.1:3306', 'MySQL 8.4.11', 'UTC', item['id'][:12]):
            self.assertIn(field, out)
        self.assertNotIn('sv_b', out)
        result = json.loads(self.success(['list', '--database', 'sv_m', '--json'])[0])
        self.assertEqual(result['schema_version'], 'schema-version-list-1.0')
        self.assertEqual([i['id'] for i in result['items']], [item['id']])
        self.assertEqual(json.loads(self.success(['list', '--json'])[0])['items'], self.store.schema_version_list()['items'])

    def test_incomplete_list_marker_and_notes_quiet_json(self):
        out, err = self.success(['read', '-D', 'sv_b', '-u', 'limited'])
        self.assertIn('note:', err)
        out, err = self.success(['save', '-D', 'sv_b', '-u', 'limited', '--name', 'limited'])
        self.assertIn('note:', err)
        item = self.store.schema_version_find('limited')
        out, err = self.success(['list'])
        count = sum(n for k, n in item['counts'].items() if k not in ('database', 'unreadable'))
        self.assertIn(str(count) + '*', out)
        self.assertIn('* The account that read it may not see every object; see: myflames schema show ID', out)
        for argv in (['read', '-D', 'sv_b', '-u', 'limited', '-q'],
                     ['save', '-D', 'sv_b', '-u', 'limited', '-q'],
                     ['read', '-D', 'sv_b', '-u', 'limited', '--json']):
            out, err = self.success(argv)
            self.assertTrue(out)
            self.assertEqual(err, '')
        out, err = self.success(['compare', 'live', item['id'], '-D', 'sv_b', '-u', 'limited'])
        # Coverage notes belong to the comparison: once, in its Notes section.
        self.assertIn('Notes:', out)
        self.assertIn('may not list every trigger', out)
        self.assertNotIn('note:', err)
        out, err = self.success(['compare', 'live', item['id'], '-D', 'sv_b', '-u', 'limited', '--json'])
        self.assertEqual(err, '')

    def test_show_prefix_name_json_and_objects(self):
        item = self.save()
        out, _ = self.success(['show', item['id'][:6]])
        self.assertIn('Version 1 of sv_m "baseline"', out)
        result = json.loads(self.success(['show', 'baseline', '--json'])[0])
        self.assertEqual(result, self.store.schema_version_get(item['id'])['snapshot'])
        out, _ = self.success(['show', item['id'], '--object', 'customers'])
        self.assertTrue(out.startswith('-- table customers\nCREATE TABLE'))
        self.failure(['show', item['id'], '--object', 'not-here'], 'No object named')
        limited = self.save(database='sv_b', user='limited', name='limited')
        unreadable = next(o for o in self.store.schema_version_get(limited['id'])['snapshot']['objects'] if o.get('ddl') is None)
        out, _ = self.success(['show', limited['id'], '--object', unreadable['name']])
        self.assertIn(unreadable['reason'], out)

    def test_show_all_objects_with_exact_same_name(self):
        value = snapshot('mysql-8.4', 'before')
        # Tables and procedures have separate namespaces and may share a name.
        next(o for o in value['objects'] if o['type'] == 'procedure')['name'] = 'customers'
        item = self.store.schema_version_save(value, 'shared-name')
        out, _ = self.success(['show', item['id'], '--object', 'customers'])
        self.assertIn('-- table customers\n', out)
        self.assertIn('-- procedure customers\n', out)

    def test_compare_positions_and_help_name_the_direction(self):
        args = schema_versions_cli._parser().parse_args(['compare', 'desired', 'deployed'])
        self.assertEqual((args.source, args.target), ('desired', 'deployed'))
        out, _ = self.success(['compare', '--help'])
        self.assertIn('SOURCE TARGET', out)
        self.assertIn('the schema to match', out)
        self.assertIn('the schema the sync script changes', out)
        self.assertIn('Emit only the sync script for review; never run it.', out)

    def test_compare_saved_to_live_all_formats_both_engines(self):
        for user, engine in (('root', 'mysql'), ('maria', 'mariadb')):
            with self.subTest(engine=engine):
                item = self.save(user=user, name=engine)
                argv = ['compare', 'live', item['id'], '-D', 'sv_b', '-u', user]
                out, _ = self.success(argv)
                self.assertIn('Schema comparison', out)
                self.assertIn('Different', out)
                self.assertNotIn('\nIdentical\n', out)
                result = json.loads(self.success(argv + ['--json'])[0])
                self.assertEqual(result['schema_version'], 'schema-compare-1.0')
                self.assertEqual(result['target']['id'], item['id'])
                self.assertEqual(result['source']['kind'], 'live')
                self.assertEqual(result['source']['server']['engine'], engine)
                self.assertFalse(result['identical'])
                out, _ = self.success(argv + ['--markdown'])
                self.assertEqual(out, schema_versions.format_markdown(result))
                out, _ = self.success(argv + ['--sql'])
                self.assertTrue(out.startswith('-- myflames schema sync'))
                self.assertEqual(out, result['sync']['script'])
                out, _ = self.success(argv + ['--unchanged'])
                self.assertIn('\nIdentical\n', out)
                out, _ = self.success(argv + ['--json', '--ignore-definers'])
                self.assertTrue(json.loads(out)['options']['ignore_definers'])

    def test_compare_exported_json_files_and_explicit_file(self):
        target = self.export(self.save(), 'before.json')
        source = self.export(self.save(database='sv_b', name='after'), 'after.json')
        self.connections.clear()
        report = json.loads(self.success(['compare', str(source), str(target), '--json'])[0])
        self.assertEqual(report['target']['kind'], 'file')
        self.assertEqual(report['target']['file'], 'before.json')
        self.assertEqual(report['source']['file'], 'after.json')
        self.assertFalse(report['identical'])
        renamed = target.with_suffix('.txt')
        renamed.write_text(target.read_text(encoding='utf-8'), encoding='utf-8')
        report = json.loads(self.success(['compare', str(target), 'file:' + str(renamed), '--json'])[0])
        self.assertTrue(report['identical'])
        self.assertEqual(self.connections, [])

    def test_latest_is_scoped_to_other_side_in_both_positions(self):
        item = self.save()
        self.save(database='sv_b', name='other-database')
        self.save(host='elsewhere', name='other-server')
        for target, source in (('latest', 'live'), ('live', 'latest')):
            report = json.loads(self.success(['compare', source, target, '-D', 'sv_m', '--json'])[0])
            version = report['target'] if target == 'latest' else report['source']
            self.assertEqual(version['id'], item['id'])
            self.assertTrue(report['identical'])

    def test_saved_profiles_default_database_password_and_source(self):
        profile = self.store.save('profile', {'name': 'Prod', 'host': 'db1', 'port': 3307, 'user': 'root',
                                              'database': 'sv_m'}, password='saved-secret', remember=True)
        item = self.save()
        report = json.loads(self.success(['compare', 'profile:Prod/sv_b', item['id'], '--json'])[0])
        options = self.connections[-1][0]
        self.assertEqual(options['password'], 'saved-secret')
        self.assertEqual(options['host'], 'db1')
        self.assertEqual(options['port'], 3307)
        self.assertEqual(report['source']['database'], 'sv_b')
        self.assertEqual(report['source']['connection'], {'profile_id': profile['id'], 'profile_name': 'Prod'})
        report = json.loads(self.success(['compare', 'profile:' + profile['id'], item['id'], '--json'])[0])
        self.assertEqual(report['source']['database'], 'sv_m')
        out, _ = self.success(['read', '--profile', 'Prod', '--json'])
        read = json.loads(out)
        self.assertEqual(read['database'], 'sv_m')
        self.assertEqual(read['connection']['profile_id'], profile['id'])
        out, _ = self.success(['save', '--profile', profile['id'], '--json'])
        self.assertEqual(json.loads(out)['connection']['profile_name'], 'Prod')
        out, _ = self.success(['read', '--profile', 'Prod', '-D', 'sv_b', '--json'])
        self.assertEqual(json.loads(out)['database'], 'sv_b')

    def test_two_profile_sides_keep_their_connections_and_redact_saved_password(self):
        self.store.save('profile', {'name': 'MySQL', 'host': 'mysql-host', 'database': 'sv_m', 'user': 'root'},
                        password='mysql-secret', remember=True)
        self.store.save('profile', {'name': 'MariaDB', 'host': 'maria-host', 'database': 'sv_b', 'user': 'maria'},
                        password='maria-secret', remember=True)
        result = json.loads(self.success(['compare', 'profile:MariaDB', 'profile:MySQL', '--json'])[0])
        self.assertEqual(result['target']['server']['engine'], 'mysql')
        self.assertEqual(result['source']['server']['engine'], 'mariadb')
        self.assertEqual([(o['host'], o['password']) for o, _ in self.connections],
                         [('mysql-host', 'mysql-secret'), ('maria-host', 'maria-secret')])
        conn = ReplayConn(recording('mysql-8.4', 'before'), error=ConnectorError('cannot read mysql-secret'))
        self.connection_factory.side_effect = None
        self.connection_factory.return_value = conn
        err = self.failure(['compare', 'profile:MariaDB', 'profile:MySQL'], 'cannot read [redacted]')
        self.assertNotIn('mysql-secret', err)

    def test_profile_missing_password_and_database_refuse_target_connect(self):
        self.store.save('profile', {'name': 'NoPassword', 'database': 'sv_m'})
        item = self.save()
        count = len(self.connections)
        self.failure(['compare', 'profile:NoPassword/sv_b', item['id']], '--profile NoPassword -p')
        self.store.save('profile', {'name': 'NoDatabase'}, password='saved-secret', remember=True)
        self.failure(['compare', 'profile:NoDatabase', item['id']], 'Choose a database with -D NAME.')
        self.assertEqual(len(self.connections), count)

    def test_fail_on_any_differences_identical_and_quiet(self):
        item = self.save()
        argv = ['compare', 'live', item['id']]
        code, out, err = self.run_cli(argv + ['-D', 'sv_b', '--fail-on', 'any'])
        self.assertEqual(code, 1)
        self.assertIn('Schema comparison', out)
        self.assertIn('FAIL:', err)
        out, err = self.success(argv + ['-D', 'sv_m', '--fail-on', 'any'])
        self.assertIn('OK:', err)
        code, out, err = self.run_cli(argv + ['-D', 'sv_b', '--fail-on', 'any', '-q', '--json'])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)['identical'])
        self.assertEqual(err, '')

    def test_fail_on_drops_removed_objects_and_column_data_loss(self):
        item = self.save()
        code, out, err = self.run_cli(['compare', 'live', item['id'], '-D', 'sv_b', '--fail-on', 'drops', '--json'])
        self.assertEqual(code, 1)
        self.assertTrue(any(o['status'] == 'only_in_target' for o in json.loads(out)['objects']))
        self.assertIn('FAIL:', err)
        self.success(['compare', item['id'], item['id'], '--fail-on', 'drops'])
        value = self.store.schema_version_get(item['id'])['snapshot']
        table = next(o for o in value['objects'] if o['type'] == 'table' and o['name'] == 'customers')
        # A column removal must trip even when no whole object was removed.
        column = "  `score` decimal(10,2) DEFAULT '0.00',\n"
        self.assertIn(column, table['ddl'])
        table['ddl'] = table['ddl'].replace(column, '')
        self.assertNotIn('`score` decimal(10,2)', table['ddl'])
        changed = self.store.schema_version_save(value, 'dropped-column')
        code, out, err = self.run_cli(['compare', changed['id'], item['id'], '--fail-on', 'drops', '--json'])
        self.assertEqual(code, 1, err)
        result = json.loads(out)
        self.assertFalse(any(o['status'] == 'only_in_target' for o in result['objects']))
        self.assertTrue(any('drops_data' in s['risks'] for s in result['sync']['statements']))

    def test_fail_on_drops_passes_a_non_destructive_change(self):
        item = self.save()
        value = self.store.schema_version_get(item['id'])['snapshot']
        # Use a database collation change: a difference with no removed object
        # and no statement with drops_data risk.
        database = next(o for o in value['objects'] if o['type'] == 'database')
        self.assertIn('utf8mb4_0900_ai_ci', database['ddl'])
        database['ddl'] = database['ddl'].replace('utf8mb4_0900_ai_ci', 'utf8mb4_bin')
        changed = self.store.schema_version_save(value, 'non-destructive')
        out, err = self.success(['compare', changed['id'], item['id'], '--fail-on', 'drops', '--json'])
        self.assertFalse(json.loads(out)['identical'])
        self.assertIn('OK: no objects only in target or sync statements that drop data', err)
        code, out, err = self.run_cli(['compare', changed['id'], item['id'], '--fail-on', 'any'])
        self.assertEqual(code, 1)

    def test_output_file_and_quiet_keep_stdout_empty(self):
        path = Path(self.temp.name) / 'read.json'
        out, err = self.success(['read', '-D', 'sv_m', '--json', '-o', str(path)])
        self.assertEqual(out, '')
        self.assertIn('Written to', err)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['schema_version'], 'schema-snapshot-1.0')
        out, err = self.success(['read', '-D', 'sv_m', '--json', '-o', str(path), '-q'])
        self.assertEqual((out, err), ('', ''))
        item = self.save()
        code, out, err = self.run_cli(['compare', 'live', item['id'], '-D', 'sv_b', '--fail-on', 'any',
                                       '--json', '-o', str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['schema_version'], 'schema-compare-1.0')
        self.assertIn('FAIL:', err)

    def test_edit_name_note_json_and_required_option(self):
        item = self.save(note='old')
        out, _ = self.success(['edit', item['id'], '--name', 'renamed'])
        self.assertEqual(out, 'Version 1 of sv_m is now named renamed.\n')
        out, _ = self.success(['edit', 'renamed', '--note', 'new note'])
        self.assertEqual(out, 'Version 1 of sv_m note updated.\n')
        edited = json.loads(self.success(['edit', 'renamed', '--name', '', '--note', '', '--json'])[0])
        self.assertEqual(edited['name'], '')
        self.assertEqual(edited['note'], '')
        self.failure(['edit', item['id']], '--name or --note')

    def test_delete_yes_and_non_tty_refusal(self):
        item = self.save()
        self.failure(['delete', item['id']], '--yes')
        self.assertIsNotNone(self.store.schema_version_get(item['id']))
        out, err = self.success(['delete', 'baseline', '--yes', '-q'])
        self.assertEqual(out, 'Deleted version 1 of sv_m.\n')
        self.assertEqual(err, '')
        self.assertIsNone(self.store.schema_version_get(item['id']))

    def test_delete_tty_yes_decline_and_eof(self):
        item = self.save()
        for answer in ('n\n', '\n', 'anything\n', ''):
            code, out, err = self.run_cli(['delete', item['id']], stdin=Stdin(answer, tty=True))
            self.assertEqual(code, 2)
            self.assertEqual(out, '')
            self.assertIn('Delete version 1 of sv_m (saved ', err)
            self.assertIn('? [y/N] ', err)
            self.assertIn('Not deleted.', err)
            self.assertIsNotNone(self.store.schema_version_get(item['id']))
        out, err = self.success(['delete', item['id']], stdin=Stdin('YeS\n', tty=True))
        self.assertIn('Deleted version 1', out)
        self.assertIn('? [y/N]', err)
        self.assertIsNone(self.store.schema_version_get(item['id']))

    def test_import_export_preserves_name_note_and_allows_overrides(self):
        item = self.save(name='release', note='reviewed changes')
        path = self.export(item, 'release.json')
        out, _ = self.success(['import', str(path)])
        self.assertIn('Imported as version 2 of sv_m', out)
        self.assertIn('No changes since version 1 ("release").', out)
        imported = self.store.schema_version_find('latest')
        self.assertEqual(imported['name'], 'release')
        self.assertEqual(imported['note'], 'reviewed changes')
        result = json.loads(self.success(['import', str(path), '--name', 'override', '--note', 'overridden', '--json'])[0])
        self.assertEqual(result['name'], 'override')
        self.assertEqual(result['note'], 'overridden')
        result = json.loads(self.success(['import', str(path), '--name', '', '--note', '', '--json'])[0])
        self.assertEqual((result['name'], result['note']), ('', ''))

    def test_expected_failures_one_line_no_stdout_or_output_file(self):
        invalid = Path(self.temp.name) / 'bad.json'
        invalid.write_text('not JSON', encoding='utf-8')
        binary = Path(self.temp.name) / 'binary.json'
        binary.write_bytes(b'\xff')
        output = Path(self.temp.name) / 'must-not-exist.json'
        cases = (
            (['show', 'missing'], 'No saved schema version matches'),
            (['compare', 'live', 'live', '-D', 'sv_m'], 'At most one side'),
            (['compare', 'latest', 'latest'], 'Both sides cannot be latest'),
            (['compare', 'live', 'file:/missing.json', '-D', 'sv_m'], '/missing.json'),
            (['compare', 'live', str(invalid), '-D', 'sv_m'], 'not JSON'),
            (['compare', 'live', str(binary), '-D', 'sv_m'], 'decode'),
            (['read'], 'Choose a database with -D NAME.'),
            (['save'], 'Choose a database with -D NAME.'),
            (['compare', 'live', 'latest'], 'Choose a database with -D NAME.'),
            (['list', '--unknown'], 'unrecognized arguments'),
            (['compare', 'b', 'a', '--json', '--sql'], 'not allowed'),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                self.failure(argv + ['-o', str(output)], message)
                self.assertFalse(output.exists())
        self.failure(['import', str(invalid)], 'not JSON')
        self.failure([], 'Choose an action')
        self.assertEqual(self.connections, [])

    def test_connection_error_redacts_password_and_preserves_existing_output(self):
        path = Path(self.temp.name) / 'existing.txt'
        path.write_text('original', encoding='utf-8')
        conn = ReplayConn(recording('mysql-8.4', 'before'), error=ConnectorError('cannot connect\npassword secret'))
        self.connection_factory.side_effect = None
        self.connection_factory.return_value = conn
        err = self.failure(['read', '-D', 'sv_m', '-psecret', '-o', str(path)], 'cannot connect')
        self.assertIn('[redacted]', err)
        self.assertNotIn('password secret', err)
        self.assertEqual(path.read_text(encoding='utf-8'), 'original')
        self.assertTrue(conn.sent)

    def test_password_prompt_eof_and_keyboard_interrupt_are_exit_two(self):
        for error, message in ((EOFError(), 'Password prompt cancelled.'), (KeyboardInterrupt(), 'Cancelled.')):
            with self.subTest(error=type(error).__name__), \
                    mock.patch('myflames.workspace_cli.getpass.getpass', side_effect=error):
                self.failure(['read', '-D', 'sv_m', '-p'], message)
        self.assertEqual(self.connections, [])

    def test_help_read_only_exit_codes_and_host_flag(self):
        for argv in (['--help'], ['compare', '--help']):
            out, err = self.success(argv)
            self.assertIn('never changes a server', out)
            self.assertIn('myflames never runs it', out)
            self.assertIn('0 success, 1 --fail-on tripped, 2 bad input or connection failure', out)
            self.assertEqual(err, '')
        self.assertEqual(self.connections, [])


if __name__ == '__main__':
    unittest.main()
