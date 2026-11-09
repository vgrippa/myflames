import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from myflames import connection_import as ci, ui

FIXTURES = Path(__file__).parent / 'fixtures' / 'connection-import'


class TestConnectionImport(unittest.TestCase):
    def test_workbench_keeps_mysql_ssh_and_tool_names_without_secrets(self):
        items = ci.parse_workbench((FIXTURES / 'workbench.xml').read_bytes())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0], {'name': 'Production & reporting', 'host': 'db.internal', 'port': 3307,
                                    'user': 'reporter', 'database': 'sales', 'type': 'production',
                                    'ssh_host': 'bastion.example', 'ssh_port': 2222, 'ssh_user': 'deploy'})
        self.assertEqual(items[1]['name'], 'Maria')
        self.assertNotIn('DO_NOT_IMPORT', json.dumps(items))
        self.assertNotIn('/never/open', json.dumps(items))

    def test_dbeaver_filters_provider_and_driver_and_keeps_mariadb(self):
        items = ci.parse_dbeaver((FIXTURES / 'dbeaver.json').read_bytes())
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0], {'name': 'MySQL SSH', 'host': 'db.internal', 'port': 3308,
                                    'user': 'app', 'database': 'shop', 'type': 'production',
                                    'ssh_host': 'bastion.example', 'ssh_port': 2222, 'ssh_user': 'deploy'})
        self.assertEqual(items[1]['name'], 'MariaDB')
        self.assertNotIn('DO_NOT_IMPORT', json.dumps(items))
        self.assertNotIn('/never/open', json.dumps(items))
        self.assertNotIn('ignored', json.dumps(items))

    def test_workbench_refuses_xxe_and_internal_entities_in_all_encodings(self):
        attack = '<!DOCTYPE data [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><data>&xxe;</data>'
        for encoding in ('utf-8', 'utf-16', 'utf-32'):
            with self.subTest(encoding=encoding), self.assertRaises(ValueError):
                ci.parse_workbench(attack.encode(encoding))
        with self.assertRaisesRegex(ValueError, 'DTD'):
            ci.parse_workbench(b'<!DOCTYPE data [<!ENTITY a "x">]><data>&a;</data>')

    def test_malformed_inputs_and_invalid_shapes_are_useful_errors(self):
        for parser, data in ((ci.parse_workbench, b'<data>'), (ci.parse_dbeaver, b'{'),
                             (ci.parse_dbeaver, b'[]'), (ci.parse_dbeaver, b'{"connections": []}'),
                             (ci.parse_dbeaver, b'{"connections": {"x": null}}')):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parser(data)

    def test_both_parsers_refuse_oversize(self):
        for parser, valid in ((ci.parse_workbench, b'<data/>'), (ci.parse_dbeaver, b'{"connections":{}}')):
            with self.subTest(parser=parser), self.assertRaises(ValueError):
                parser(valid + b' ' * (ci.MAX_BYTES + 1 - len(valid)))

    def test_invalid_ports_and_no_host_never_silently_target_localhost(self):
        for values in ({'host': 'db', 'port': 65536}, {'host': 'db', 'port': 'bad'},
                       {'url': 'jdbc:mysql://secret:password@other.example/'}, {'host': 'db\nother'}):
            data = {'connections': {'one': {'provider': 'mysql', 'driver': 'mysql8', 'configuration': values}}}
            with self.subTest(values=values), self.assertRaises(ValueError):
                ci.parse_dbeaver(json.dumps(data).encode())

    def test_disabled_ssh_and_ipv6_ssh(self):
        data = json.loads((FIXTURES / 'dbeaver.json').read_text())
        handler = data['connections']['mysql-id']['configuration']['handlers']['ssh_tunnel']
        handler['enabled'] = False
        self.assertNotIn('ssh_host', ci.parse_dbeaver(json.dumps(data).encode())[0])
        xml = (FIXTURES / 'workbench.xml').read_bytes().replace(b'bastion.example:2222', b'[2001:db8::1]:2223')
        item = ci.parse_workbench(xml)[0]
        self.assertEqual(item['ssh_host'], '2001:db8::1')
        self.assertEqual(item['ssh_port'], 2223)
        self.assertIn('Enter only the SSH host name or alias in SSH host', item['error'])

    def test_preview_validates_against_store_and_atomic_save_rejects_bad_rows(self):
        from myflames.workspace_store import WorkspaceStore
        with tempfile.TemporaryDirectory() as directory:
            store = WorkspaceStore(Path(directory) / 'workspace.sqlite3')
            wb_items = ci.parse_workbench((FIXTURES / 'workbench.xml').read_bytes())
            db_items = ci.parse_dbeaver((FIXTURES / 'dbeaver.json').read_bytes())
            for item in wb_items + db_items:
                self.assertNotIn('error', item)
            saved = ci.save_imported(store, wb_items + db_items)
            self.assertEqual(len(saved), 4)
            self.assertEqual(len(store.list('profile')), 4)

            long_name = 'n' * 201
            data = {'connections': {
                'good': {'name': 'Valid row', 'provider': 'mysql', 'driver': 'mysql8', 'configuration': {'host': 'valid.example', 'port': 3306, 'user': 'reader'}},
                'long': {'name': long_name, 'provider': 'mysql', 'driver': 'mysql8', 'configuration': {'host': 'long.example', 'port': 3306, 'user': 'reader'}},
            }}
            parsed = ci.parse_dbeaver(json.dumps(data).encode())
            self.assertNotIn('error', parsed[0])
            self.assertEqual(parsed[1]['error'], 'Connection names must be text of at most 200 characters.')
            with self.assertRaisesRegex(ValueError, 'at most 200 characters'):
                store.save('profile', parsed[1], remember=False)
            with self.assertRaisesRegex(ValueError, 'at most 200 characters'):
                ci.save_imported(store, parsed)
            self.assertEqual(len(store.list('profile')), 4)

            xml = (FIXTURES / 'workbench.xml').read_bytes().replace(b'bastion.example:2222', b'[2001:db8::1]:2223')
            bad_ssh = ci.parse_workbench(xml)
            self.assertIn('error', bad_ssh[0])
            self.assertNotIn('error', bad_ssh[1])
            with self.assertRaisesRegex(ValueError, 'SSH host'):
                store.save('profile', bad_ssh[0], remember=False)
            with self.assertRaisesRegex(ValueError, 'SSH host'):
                ci.save_imported(store, [bad_ssh[1], bad_ssh[0]])
            self.assertEqual(len(store.list('profile')), 4)

    def test_unsupported_transports_never_become_direct_connections(self):
        base = json.loads((FIXTURES / 'dbeaver.json').read_text())
        config = base['connections']['mysql-id']['configuration']
        config['handlers']['ssh_tunnel']['properties']['jumpServer.count'] = 2
        with self.assertRaisesRegex(ValueError, 'jump chains'):
            ci.parse_dbeaver(json.dumps(base).encode())
        config['handlers']['ssh_tunnel']['properties'].pop('jumpServer.count')
        config['configurationType'] = 'URL'
        with self.assertRaisesRegex(ValueError, 'URL-only'):
            ci.parse_dbeaver(json.dumps(base).encode())
        xml = (FIXTURES / 'workbench.xml').read_bytes().replace(b'native_sshtun', b'native_socket')
        with self.assertRaisesRegex(ValueError, 'TCP/IP'):
            ci.parse_workbench(xml)
        xml = (FIXTURES / 'workbench.xml').read_bytes().replace(b'bastion.example:2222', b'')
        with self.assertRaisesRegex(ValueError, 'no SSH host'):
            ci.parse_workbench(xml)

    def test_legacy_network_handler_and_auth_field_names_are_supported(self):
        base = json.loads((FIXTURES / 'dbeaver.json').read_text())
        config = base['connections']['mysql-id']['configuration']
        config['network-handlers'] = config.pop('handlers')
        config['user'] = config.pop('auth-properties')['userName']
        self.assertEqual(ci.parse_dbeaver(json.dumps(base).encode())[0]['ssh_user'], 'deploy')

    def test_leading_schema_and_user_spaces_are_preserved(self):
        base = json.loads((FIXTURES / 'dbeaver.json').read_text())
        config = base['connections']['mysql-id']['configuration']
        config['database'] = ' leading schema'
        config['auth-properties']['userName'] = ' reader '
        item = ci.parse_dbeaver(json.dumps(base).encode())[0]
        self.assertEqual(item['database'], ' leading schema')
        self.assertEqual(item['user'], ' reader ')

    def test_excessive_connections_are_refused(self):
        data = {'connections': {str(n): {'provider': 'mysql', 'driver': 'mysql', 'configuration': {'host': 'db'}}
                                for n in range(1001)}}
        with self.assertRaisesRegex(ValueError, '1,000'):
            ci.parse_dbeaver(json.dumps(data).encode())

    def test_only_fixed_platform_default_paths(self):
        with mock.patch.object(ci.sys, 'platform', 'darwin'), mock.patch.object(ci.Path, 'home', return_value=Path('/users/fake')):
            self.assertEqual(str(ci.default_path('workbench')), '/users/fake/Library/Application Support/MySQL/Workbench/connections.xml')
            self.assertEqual(str(ci.default_path('dbeaver')), '/users/fake/Library/DBeaverData/workspace6/General/.dbeaver/data-sources.json')
        with mock.patch.object(ci.sys, 'platform', 'win32'), mock.patch.dict(os.environ, {'APPDATA': '/fake/appdata'}):
            self.assertEqual(str(ci.default_path('workbench')), '/fake/appdata/MySQL/Workbench/connections.xml')
            self.assertEqual(str(ci.default_path('dbeaver')), '/fake/appdata/DBeaverData/workspace6/General/.dbeaver/data-sources.json')

    def test_default_and_selected_file_routes_read_no_password_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'connections.xml'
            target.write_bytes((FIXTURES / 'workbench.xml').read_bytes())
            with mock.patch.object(ci, 'default_path', return_value=target):
                result = ui.api_response('/api/connections/import', {'tool': 'workbench'})
                self.assertEqual(len(result['items']), 2)
                self.assertTrue(result['found'])
            with mock.patch.object(ci, 'choose_file', return_value=str(target)):
                self.assertEqual(len(ci.response({'tool': 'workbench', 'choose': True})['items']), 2)
            target.unlink()
            with mock.patch.object(ci, 'default_path', return_value=target):
                self.assertFalse(ci.response({'tool': 'workbench'})['found'])

    def test_native_chooser_uses_fixed_argv_and_has_cancel_and_timeout_paths(self):
        with mock.patch.object(ci.sys, 'platform', 'darwin'), mock.patch('myflames.folder_picker._run_dialog', return_value=None) as run:
            self.assertTrue(ci.response({'tool': 'workbench', 'choose': True})['cancelled'])
            self.assertEqual(run.call_args[0][0], ['/usr/bin/osascript'])
            self.assertIn('choose file', run.call_args[1]['input_text'])
        with mock.patch.object(ci.sys, 'platform', 'win32'), mock.patch('myflames.folder_picker._run_dialog', return_value=None) as run:
            ci.choose_file()
            self.assertIn('-NoProfile', run.call_args[0][0])
            self.assertIn('OpenFileDialog', run.call_args[0][0][-1])
        with mock.patch.object(ci.sys, 'platform', 'darwin'), mock.patch('myflames.folder_picker._run_dialog', side_effect=ValueError('timed out')):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                ci.choose_file()
            self.assertTrue(ci._DIALOG_LOCK.acquire(blocking=False))
            ci._DIALOG_LOCK.release()

    def test_unknown_tools_arbitrary_paths_and_bad_flags_are_refused(self):
        for request in ({'tool': 'unknown'}, {'tool': []}, {'tool': 'workbench', 'path': '/etc/passwd'},
                        {'tool': 'workbench', 'choose': 'yes'}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                ci.response(request)

    def test_selected_file_rejects_secret_names_relative_controls_symlink_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            secret = folder / 'credentials-config.json'
            secret.write_text('secret')
            link = folder / 'connections.xml'
            link.symlink_to(secret)
            for path in (secret, Path('connections.xml'), link, folder / 'data-sources.json\n'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    ci.read_file(path, 'workbench')
            link.unlink()
            link.mkdir()
            with self.assertRaises(ValueError):
                ci.read_file(link, 'workbench')

    def test_file_size_is_checked_before_read_and_fifo_does_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'connections.xml'
            with path.open('wb') as stream:
                stream.truncate(ci.MAX_BYTES + 1)
            with self.assertRaisesRegex(ValueError, '2 MB'):
                ci.read_file(path, 'workbench')
            if hasattr(os, 'mkfifo'):
                path.unlink()
                os.mkfifo(str(path))
                with self.assertRaisesRegex(ValueError, 'regular'):
                    ci.read_file(path, 'workbench')


if __name__ == '__main__':
    unittest.main()
