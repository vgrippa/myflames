"""Saved SSH connections and explicit host-key trust, without network access.

All files and vault entries use temporary homes. The ssh and ssh-keygen calls
are mocked; only the separate askpass tests execute a shell helper.
"""
import copy
import io
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

from myflames import secret_store, sftp, sftp_api, ssh_connections as ssh, ui
from myflames.workspace_store import WorkspaceStore


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myflames-ssh-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = os.path.realpath(self.temp.name)
        patch = mock.patch.dict(os.environ, HOME=self.root, USERPROFILE=self.root,
                                MYFLAMES_SSH_HOME=self.root, MYFLAMES_SECRET_BACKEND='vault')
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        patch = mock.patch.object(tempfile, 'tempdir', self.root)
        patch.start()
        self.addCleanup(patch.stop)
        self.store = WorkspaceStore(os.path.join(self.root, 'store', 'workspace.sqlite3'))
        self.draft = dict(name='Logs', host='example.test', port=None, user='', auth='agent',
                          key_path='', remote_dir='', local_dir='')
        self.addCleanup(sftp_api.close_sessions)
        patch = mock.patch.dict(ssh._TOKENS, {}, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.network = mock.patch.object(ssh.subprocess, 'run', side_effect=AssertionError('Unexpected subprocess'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def local(self, name, content=b'private key content must never be read'):
        path = os.path.join(self.root, name)
        with open(path, 'wb') as stream:
            stream.write(content)
        return path

    def save(self, changes=None, **kwargs):
        return ssh.save(self.store, dict(connection=dict(self.draft, **(changes or {})), **kwargs))['connection']

    def raw(self, key):
        return self.store.get(ssh.KIND, key)


class TestRecords(Isolated):
    def test_every_field_validation_and_no_persistence_on_failure(self):
        key = self.local('id_key')
        changes = [
            {'name': ''}, {'name': ' '}, {'name': 'x' * 81}, {'name': 5}, {'name': 'bad\n'},
            {'host': ''}, {'host': None}, {'host': True}, {'host': '-option'}, {'host': 'a;b'},
            {'host': 'a@b'}, {'host': 'a/b'}, {'host': 'a:b'}, {'host': 'a\n'}, {'host': 'a b'},
            {'user': False}, {'user': '-option'}, {'user': 'a\r'}, {'user': 'a b'}, {'user': 'a/b'},
            {'port': True}, {'port': 0}, {'port': 65536}, {'port': '22'}, {'port': 1.5},
            {'auth': None}, {'auth': 'interactive'}, {'auth': True},
            {'key_path': key}, {'auth': 'key', 'key_path': ''}, {'auth': 'key', 'key_path': 'relative'},
            {'auth': 'key', 'key_path': self.root}, {'auth': 'key', 'key_path': key + 'missing'},
            {'auth': 'key', 'key_path': key + '"'}, {'auth': 'key', 'key_path': key + '\x00'},
            {'remote_dir': 'relative'}, {'remote_dir': None}, {'remote_dir': '/x\n'},
            {'local_dir': 'relative'}, {'local_dir': 1}, {'local_dir': '/x\x7f'},
            {'id': 'bad'}, {'secret_ref': 'injected'}, {'secret_backend': 'keychain'}, {'secret': 'injected'},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.save(change)
        self.assertEqual(self.store.list(ssh.KIND), [])
        self.assertFalse(Path(self.root, '.myflames', 'vault.json').exists())
        good = self.save({'name': '  Alias  ', 'host': '  dev-alias  ', 'user': '  reader  ',
                          'port': 65535, 'auth': 'key', 'key_path': key,
                          'remote_dir': '/var/log', 'local_dir': self.root})
        self.assertEqual((good['name'], good['host'], good['user']), ('Alias', 'dev-alias', 'reader'))
        self.assertEqual(good['port'], 65535)
        self.assertEqual(good['key_path'], key)

    def test_sorted_unique_names_public_shape_and_persistence(self):
        first = self.save({'name': 'zebra'})
        second = self.save({'name': 'Alpha', 'auth': 'password'}, secret='stored-value')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.save({'name': 'ALPHA'})
        reopened = WorkspaceStore(self.store.path)
        answer = ssh.list_connections(reopened)
        self.assertEqual([item['id'] for item in answer['connections']], [second['id'], first['id']])
        expected = {'id', 'name', 'host', 'port', 'user', 'auth', 'key_path', 'has_secret',
                    'remote_dir', 'local_dir', 'updated'}
        self.assertEqual(set(answer), {'connections', 'known_hosts_file'})
        for item in answer['connections']:
            self.assertEqual(set(item), expected)
            self.assertRegex(item['id'], r'^[0-9a-f]{32}$')
            self.assertRegex(item['updated'], r'^\d{4}-\d\d-\d\dT')
        self.assertTrue(answer['connections'][0]['has_secret'])
        dumped = json.dumps(answer)
        for forbidden in ('stored-value', 'secret_ref', 'secret_backend', 'sshconn.'):
            self.assertNotIn(forbidden, dumped)
        with mock.patch.object(sftp, 'SftpSession') as session:
            ssh.open_session(ssh.get(reopened, second['id']))
        self.assertEqual(session.call_args[1]['secret'], 'stored-value')

    def test_binding_fields_edit_drops_secret(self):
        key1, key2 = self.local('key1'), self.local('key2')
        fields = [('host', 'other.test'), ('user', 'other'), ('port', 22),
                  ('auth', 'password'), ('key_path', key2)]
        for index, (field, new) in enumerate(fields):
            base = dict(self.draft, name='connection%d' % index, auth='key', key_path=key1)
            saved = ssh.save(self.store, {'connection': base, 'secret': 'old-secret'})['connection']
            old = self.raw(saved['id'])
            changes = dict(base, id=saved['id'])
            changes[field] = new
            if field == 'auth':
                changes['key_path'] = ''
            result = ssh.save(self.store, {'connection': changes})['connection']
            self.assertFalse(result['has_secret'], field)
            self.assertIsNone(secret_store.get_secret(old['secret_ref'], old['secret_backend']))
            self.assertNotIn('secret_ref', self.raw(saved['id']))

    def test_binding_guard_rejects_record_id_or_endpoint_tampering_before_read(self):
        saved = self.save({'auth': 'password'}, secret='never-release')
        record = ssh.get(self.store, saved['id'])
        self.assertRegex(record['secret_ref'], r'^sshconn\.' + saved['id'] + r'\.[0-9a-f]{24}\.[0-9a-f]{16}$')
        # Independently derived fingerprint for this fixed endpoint.
        import hashlib
        fingerprint = hashlib.sha256(b'example.test\x00\x000\x00password\x00').hexdigest()[:24]
        self.assertIn('.' + fingerprint + '.', record['secret_ref'])
        for change in ({'host': 'evil.test'}, {'user': 'other'}, {'port': 22}, {'auth': 'key'},
                       {'key_path': '/another/key'}, {'id': 'f' * 32}, {'secret_ref': 'profile.secret'}):
            with mock.patch.object(secret_store, 'get_secret') as read, mock.patch.object(sftp, 'SftpSession') as session:
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'different connection details'):
                    ssh.open_session(dict(record, **change))
                read.assert_not_called()
                session.assert_not_called()

    def test_non_endpoint_edit_keeps_secret_and_replacement_forget_delete_remove_it(self):
        saved = self.save({'auth': 'password'}, secret='first-secret')
        first = self.raw(saved['id'])
        draft = dict(self.draft, id=saved['id'], auth='password', name='Renamed', remote_dir='/logs', local_dir=self.root)
        ssh.save(self.store, {'connection': draft})
        self.assertEqual(self.raw(saved['id'])['secret_ref'], first['secret_ref'])
        changed = dict(draft, host='new.test')
        ssh.save(self.store, {'connection': changed, 'secret': 'second-secret'})
        second = self.raw(saved['id'])
        self.assertIsNone(secret_store.get_secret(first['secret_ref'], first['secret_backend']))
        self.assertEqual(secret_store.get_secret(second['secret_ref'], second['secret_backend']), 'second-secret')
        result = ssh.save(self.store, {'connection': changed, 'forget_secret': True})
        self.assertFalse(result['connection']['has_secret'])
        self.assertIsNone(secret_store.get_secret(second['secret_ref'], second['secret_backend']))
        ssh.save(self.store, {'connection': changed, 'secret': 'third-secret'})
        third = self.raw(saved['id'])
        self.assertEqual(ssh.delete(self.store, saved['id']), {'deleted': True})
        self.assertIsNone(secret_store.get_secret(third['secret_ref'], third['secret_backend']))
        self.assertEqual(self.store.list(ssh.KIND), [])

    def test_secret_validation_and_store_errors_never_leak_secret(self):
        for bad in (None, True, 123, '', 'x' * 1025, 'two\nlines', 'nul\x00', '\ud800'):
            with self.subTest(bad=repr(bad)[:40]), self.assertRaises(ValueError):
                self.save({'auth': 'password'}, secret=bad)
        for changes in ({'forget_secret': 1}, {'forget_secret': True, 'secret': 'value'}):
            with self.assertRaises(ValueError):
                self.save({'auth': 'password'}, **changes)
        with self.assertRaises(ValueError):
            self.save(secret='value')
        with mock.patch.object(secret_store, 'save_secret', side_effect=ValueError('private-secret leaked')):
            with self.assertRaises(ValueError) as caught:
                self.save({'auth': 'password'}, secret='private-secret')
        self.assertNotIn('private-secret', str(caught.exception))
        saved = self.save({'auth': 'password'}, secret='private-secret')
        record = ssh.get(self.store, saved['id'])
        with mock.patch.object(secret_store, 'get_secret', side_effect=ValueError('private-secret leaked')):
            with self.assertRaises(ValueError) as caught:
                ssh.open_session(record)
        self.assertNotIn('private-secret', str(caught.exception))
        with mock.patch.object(secret_store, 'delete_secret', side_effect=ValueError('private-secret leaked')):
            with self.assertRaises(ValueError) as caught:
                ssh.delete(self.store, saved['id'])
        self.assertNotIn('private-secret', str(caught.exception))
        self.assertIn('could not be removed from the credential store', str(caught.exception))
        # The record went first: no connection is left claiming a secret it lost.
        self.assertIsNone(self.raw(saved['id']))

    def test_failed_record_write_keeps_the_old_secret(self):
        saved = self.save({'auth': 'password'}, secret='old-secret')
        before = ssh.get(self.store, saved['id'])
        with mock.patch.object(self.store, '_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.save({'auth': 'password', 'id': saved['id']}, secret='new-secret')
        after = ssh.get(self.store, saved['id'])
        self.assertEqual(after['secret_ref'], before['secret_ref'])
        self.assertEqual(secret_store.get_secret(after['secret_ref'], after.get('secret_backend')), 'old-secret')
        # A successful replacement retires the old secret only after the write.
        replaced = self.save({'auth': 'password', 'id': saved['id']}, secret='new-secret')
        current = ssh.get(self.store, replaced['id'])
        self.assertEqual(secret_store.get_secret(current['secret_ref'], current.get('secret_backend')), 'new-secret')
        self.assertIsNone(secret_store.get_secret(before['secret_ref'], before.get('secret_backend')))

    def test_saved_secret_missing_and_no_secret_password_fail_closed(self):
        saved = self.save({'auth': 'password'}, secret='private-secret')
        record = ssh.get(self.store, saved['id'])
        secret_store.delete_secret(record['secret_ref'], record['secret_backend'])
        with mock.patch.object(sftp, 'SftpSession') as factory:
            with self.assertRaisesRegex(ValueError, 'missing'):
                ssh.open_session(record)
        factory.assert_not_called()
        with self.assertRaises(sftp.SftpAuthError):
            sftp.SftpSession({'ssh_host': 'example.test'}, auth='password')

    def test_draft_test_ignores_saved_secret_and_never_persists(self):
        saved = self.save({'auth': 'password'}, secret='saved-secret')
        draft = dict(self.draft, id=saved['id'], auth='password', host='draft.test')
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.realpath.return_value = '/home/reader'
        session.safe.side_effect = lambda value: value
        before = copy.deepcopy(self.store.list(ssh.KIND))
        with mock.patch.object(sftp, 'SftpSession', return_value=session) as factory, \
                mock.patch.object(secret_store, 'get_secret') as read, \
                mock.patch.object(ssh.time, 'monotonic', side_effect=[10.0, 10.125]):
            result = ssh.test(self.store, {'connection': draft, 'secret': 'draft-secret'})
        self.assertEqual(result, {'ok': True, 'home': '/home/reader', 'elapsed_ms': 125})
        self.assertEqual(factory.call_args[1]['secret'], 'draft-secret')
        self.assertEqual(factory.call_args[1]['spec']['ssh_host'], 'draft.test')
        read.assert_not_called()
        self.assertEqual(self.store.list(ssh.KIND), before)
        session.__exit__.assert_called_once()
        with self.assertRaises(ValueError):
            ssh.test(self.store, {'id': saved['id'], 'secret': 'injected'})

    def test_known_hosts_file_is_private_and_refuses_symlink(self):
        path = ssh.known_hosts_file(self.store)
        self.assertEqual(path, str(self.store.path.parent / 'ssh_known_hosts'))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        os.chmod(path, 0o644)
        ssh.known_hosts_file(self.store)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        os.unlink(path)
        victim = self.local('victim', b'do not touch')
        os.symlink(victim, path)
        with self.assertRaises(ValueError):
            ssh.known_hosts_file(self.store)
        with open(victim, 'rb') as stream:
            self.assertEqual(stream.read(), b'do not touch')


class TestHostKeys(Isolated):
    LINE = 'example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey'

    def scan(self, lines=None, fingerprint=None, user_known=None, keygen_status=None):
        captured = []
        user_file = os.path.join(self.root, 'user_known_hosts')
        if user_known is not None:
            with open(user_file, 'w', encoding='ascii') as stream:
                stream.write(user_known + '\n')

        def run(argv, **kwargs):
            if '-G' in argv:  # The known_hosts files ssh would consult.
                return subprocess.CompletedProcess(argv, 0, ('userknownhostsfile %s\nglobalknownhostsfile '
                                                              '/nonexistent/global\n' % user_file).encode(), b'')
            if '-F' in argv:  # ssh-keygen -F name -f file, emulated on plain lines.
                if keygen_status is not None:
                    return subprocess.CompletedProcess(argv, keygen_status, b'', b'')
                name, path = argv[argv.index('-F') + 1], argv[argv.index('-f') + 1]
                with open(path, encoding='ascii') as stream:
                    hits = [(i, l.strip()) for i, l in enumerate(stream, 1) if name in l.split(' ', 1)[0].split(',')]
                out = ''.join('# Host %s found: line %d\n%s\n' % (name, i, l) for i, l in hits)
                return subprocess.CompletedProcess(argv, 0 if hits else 1, out.encode(), b'')
            captured.append((argv, kwargs))
            self.assertEqual(kwargs['env']['HOME'], self.root)
            self.assertNotIn('SSH_ASKPASS', kwargs['env'])
            if len(captured) == 1:
                self.assertEqual(argv[0], '/safe/ssh')
                option = next(arg for arg in argv if arg.startswith('UserKnownHostsFile='))
                path = option.split('=', 1)[1][1:-1]
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
                with open(path, 'w', encoding='ascii') as stream:
                    stream.write((self.LINE if lines is None else lines) + '\n')
                return subprocess.CompletedProcess(argv, 255, b'', b'Permission denied')
            self.assertEqual(argv[:4], ['/safe/ssh-keygen', '-l', '-E', 'sha256'])
            self.assertEqual(argv[4], '-f')
            return subprocess.CompletedProcess(argv, 0, fingerprint or b'256 SHA256:abcdEF123 example.test (ED25519)\n', b'')

        with mock.patch.object(sftp.ssh_tunnel, 'find_ssh', return_value='/safe/ssh'), \
                mock.patch.object(ssh.subprocess, 'run', side_effect=run):
            result = ssh.scan(self.store, {'connection': dict(self.draft, port=2222, user='reader')})
        return result, captured

    def test_scan_refuses_a_new_key_for_a_host_trusted_with_another(self):
        # Security review: an impersonator offering RSA where ED25519 is known
        # is "unknown" to ssh; the scan must call it a changed key, not a first
        # contact, and issue no token.
        trusted = 'example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIRealKey'
        offered = 'example.test ssh-rsa AAAAB3NzaC1yc2EAAAADAQABImpostor'
        before = dict(ssh._TOKENS)
        with self.assertRaises(sftp.SftpHostKeyChanged) as caught:
            self.scan(lines=offered, fingerprint=b'3072 SHA256:imp example.test (RSA)\n', user_known=trusted)
        self.assertIn('already trusted with a different host key', str(caught.exception))
        self.assertIn('line 1', str(caught.exception))
        self.assertEqual(ssh._TOKENS, before, 'no token is issued')
        # The same key already trusted is no change: the scan proceeds.
        result, _ = self.scan(user_known=self.LINE)
        self.assertRegex(result['token'], r'^[0-9a-f]{32}$')

    def test_scan_searches_exactly_the_files_a_session_uses(self):
        with mock.patch.object(sftp, 'known_hosts_config', return_value=(['/u/known_hosts'], ['/g/custom'])) as config:
            files = ssh._known_hosts_files('/usr/bin/ssh', dict(ssh.validate(self.draft), _known_hosts='/m/ssh_known_hosts'), {})
        self.assertEqual(files, ['/u/known_hosts', '/g/custom', '/m/ssh_known_hosts'])
        self.assertEqual(config.call_args[0][1]['ssh_host'], self.draft['host'])

    def test_scan_refuses_when_a_known_hosts_file_cannot_be_searched(self):
        # Security re-review: a file ssh-keygen cannot search (exit 255) is not
        # "nothing trusted"; no token is issued.
        before = dict(ssh._TOKENS)
        with self.assertRaisesRegex(ValueError, 'could not be checked'):
            self.scan(user_known=self.LINE, keygen_status=255)
        self.assertEqual(ssh._TOKENS, before)

    def test_scan_auth_disabled_and_fingerprint_shape_and_temp_removed(self):
        result, captured = self.scan()
        self.assertEqual(result['host'], 'example.test')
        self.assertEqual(result['keys'], [{'type': 'ED25519', 'bits': 256, 'fingerprint': 'SHA256:abcdEF123'}])
        self.assertRegex(result['token'], r'^[0-9a-f]{32}$')
        argv = captured[0][0]
        self.assertEqual(argv[-3:], ['--', 'example.test', 'true'])
        for method in ('Pubkey', 'Password', 'KbdInteractive', 'GSSAPI', 'Hostbased'):
            self.assertIn(method + 'Authentication=no', argv)
        for option in ('StrictHostKeyChecking=accept-new', 'GlobalKnownHostsFile=/dev/null', 'HashKnownHosts=no',
                       'BatchMode=yes', 'ControlMaster=no', 'ControlPath=none', 'PermitLocalCommand=no'):
            self.assertIn(option, argv)
        self.assertIn('2222', argv)
        self.assertIn('reader', argv)
        temp_file = captured[1][0][-1]
        self.assertFalse(os.path.exists(temp_file))
        self.assertEqual(self.store.list(ssh.KIND), [])

    def test_trust_exact_append_dedup_permissions_and_single_use(self):
        result, _ = self.scan()
        path = ssh.known_hosts_file(self.store)
        with open(path, 'wb') as stream:
            stream.write(b'old.test ssh-rsa AAAAold')
        self.assertEqual(ssh.trust(self.store, result['token']), {'trusted': True, 'host': 'example.test'})
        expected = b'old.test ssh-rsa AAAAold\n' + self.LINE.encode('ascii') + b'\n'
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), expected)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with self.assertRaisesRegex(ValueError, 'expired or was already used'):
            ssh.trust(self.store, result['token'])
        result, _ = self.scan()
        ssh.trust(self.store, result['token'])
        with open(path, 'rb') as stream:
            self.assertEqual(stream.read(), expected)
        self.assertFalse(Path(self.root, '.ssh', 'known_hosts').exists())

    def test_token_expiry_boundary_and_workspace_binding(self):
        with mock.patch.object(ssh.time, 'monotonic', return_value=100):
            result, _ = self.scan()
        other = WorkspaceStore(os.path.join(self.root, 'other', 'workspace.sqlite3'))
        with mock.patch.object(ssh.time, 'monotonic', return_value=399):
            with self.assertRaises(ValueError):
                ssh.trust(other, result['token'])
            self.assertIn(result['token'], ssh._TOKENS)
        with mock.patch.object(ssh.time, 'monotonic', return_value=400):
            with self.assertRaisesRegex(ValueError, 'expired'):
                ssh.trust(self.store, result['token'])
        self.assertEqual(ssh._TOKENS, {})
        with open(ssh.known_hosts_file(self.store), 'rb') as stream:
            self.assertEqual(stream.read(), b'')

    def test_malformed_or_missing_scan_key_and_fingerprint_rejected(self):
        for line in ('', '# comment', '@revoked ' + self.LINE, self.LINE + '\x1b', 'host type invalid!'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.scan(lines=line)
        for output in (b'not a fingerprint\n', b'256 MD5:abc host (RSA)\n', b''):
            # Empty stdout needs a dedicated run mock because the helper uses a default.
            if not output:
                continue
            with self.subTest(output=output), self.assertRaises(ValueError):
                self.scan(fingerprint=output)
        self.assertEqual(ssh._TOKENS, {})

    def test_key_picker_reads_only_pub_files(self):
        directory = os.path.join(self.root, '.ssh')
        os.mkdir(directory)
        private = self.local('.ssh/id_ed25519')
        self.local('.ssh/id_ed25519.pub', b'ssh-ed25519 AAAA public comment\n')
        self.local('.ssh/no-public')
        self.local('.ssh/only-public.pub', b'ssh-rsa AAAA absent\n')
        self.local('.ssh/bad.pub', b'not a key')
        self.local('.ssh/bad')
        os.symlink(private, directory + '/symlink')
        self.local('.ssh/symlink.pub', b'ssh-ed25519 AAAA linked')
        original = open
        reads = []

        def reading(path, *args, **kwargs):
            reads.append(str(path))
            if not str(path).endswith('.pub'):
                self.fail('Private key contents were opened')
            return original(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=reading):
            answer = ssh.keys()
        self.assertEqual(answer, {'keys': [{'path': private, 'type': 'ssh-ed25519', 'comment': 'public comment'}]})
        self.assertTrue(reads)
        self.assertTrue(all(path.endswith('.pub') for path in reads))


class TestRoutes(Isolated):
    def test_all_ssh_routes_shapes(self):
        workspace = mock.Mock(store=self.store)
        call = lambda path, payload: ui.api_response('/api/ssh/' + path, payload, workspace)
        result = call('connections/save', {'connection': self.draft})
        self.assertEqual(set(result), {'connection'})
        key = result['connection']['id']
        self.assertEqual(call('connections/list', {})['connections'], [result['connection']])
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.realpath.return_value = '/home'
        session.safe.side_effect = lambda text: text
        with mock.patch.object(ssh, 'open_session', return_value=session):
            tested = call('connections/test', {'id': key})
        self.assertEqual(set(tested), {'ok', 'home', 'elapsed_ms'})
        self.assertIs(tested['ok'], True)
        self.assertEqual(tested['home'], '/home')
        self.assertIsInstance(tested['elapsed_ms'], int)
        scan = {'host': 'example.test', 'keys': [{'type': 'ED25519', 'bits': 256, 'fingerprint': 'SHA256:abc'}],
                'token': 'f' * 32}
        with mock.patch.object(ssh, 'scan', return_value=scan) as scanner:
            self.assertEqual(call('hostkey/scan', {'id': key}), scan)
        scanner.assert_called_once_with(self.store, {'id': key})
        with mock.patch.object(ssh, 'trust', return_value={'trusted': True, 'host': 'example.test'}) as trust:
            self.assertEqual(call('hostkey/trust', {'token': scan['token']}), {'trusted': True, 'host': 'example.test'})
        trust.assert_called_once_with(self.store, scan['token'])
        self.assertEqual(call('keys', {}), {'keys': []})
        self.assertEqual(call('connections/delete', {'id': key}), {'deleted': True})

    def handler(self, path, payload):
        handler = object.__new__(ui.WorkspaceHandler)
        handler.path = path
        handler.server = mock.Mock(token='token', store=self.store)
        handler.headers = Message()
        handler.headers['X-Myflames-Token'] = 'token'
        handler.headers['Content-Type'] = 'application/json'
        raw = json.dumps(payload).encode('utf-8')
        handler.headers['Content-Length'] = str(len(raw))
        handler._read_body = mock.Mock(return_value=raw)
        handler._local_request = mock.Mock(return_value=True)
        handler._authenticated = mock.Mock(return_value=True)
        handler._send = mock.Mock()
        return handler

    def test_post_allowlist_and_reason_code_before_generic_mapping(self):
        for path in ui._SSH_CONNECTION_PATHS + ui._FILES_PATHS:
            handler = self.handler(path, {})
            with mock.patch.object(ui, 'api_response', return_value={'ok': True}):
                handler.do_POST()
            handler._send.assert_called_once_with(200, {'ok': True})
        for error in (sftp.SftpHostKeyUnknown('not trusted'), sftp.SftpHostKeyChanged('key changed'),
                      sftp.SftpAuthError('not accepted'), sftp.SftpSshMissing('ssh missing')):
            handler = self.handler('/api/ssh/connections/test', {})
            with mock.patch.object(ui, 'api_response', side_effect=error):
                handler.do_POST()
            handler._send.assert_called_once_with(400, {'error': str(error), 'code': error.reason})
        # Only SSH errors carry codes: a reason attribute on another exception
        # (UnicodeDecodeError, SSLError, URLError) never becomes one.
        for reason in ('custom', '', None, 3):
            error = ValueError('plain')
            error.reason = reason
            handler = self.handler('/api/ssh/connections/test', {})
            with mock.patch.object(ui, 'api_response', side_effect=error):
                handler.do_POST()
            status, answer = handler._send.call_args[0]
            self.assertEqual(status, 400)
            self.assertNotIn('code', answer)
        handler = self.handler('/api/ssh/connections/test', {})
        try:
            b'\xff'.decode('utf-8')
        except UnicodeDecodeError as exc:
            decode_error = exc
        with mock.patch.object(ui, 'api_response', side_effect=decode_error):
            handler.do_POST()
        status, answer = handler._send.call_args[0]
        self.assertEqual((status, 'code' in answer), (400, False))
        for path in ('/api/live/sftp/list', '/api/live/sftp/ssh-auth/save'):
            handler = self.handler(path, {})
            handler.do_POST()
            self.assertEqual(handler._send.call_args[0][0], 404)

    def test_secret_request_field_is_redacted_from_unexpected_logs(self):
        handler = self.handler('/api/ssh/connections/save', {'secret': 'private-secret'})
        with mock.patch.object(ui, 'api_response', side_effect=RuntimeError('leaked private-secret')), \
                mock.patch.object(ui.sys, 'stderr', new_callable=io.StringIO) as log:
            handler.do_POST()
        self.assertEqual(handler._send.call_args[0][0], 500)
        self.assertNotIn('private-secret', log.getvalue())
        self.assertIn('[redacted]', log.getvalue())


if __name__ == '__main__':
    unittest.main()
