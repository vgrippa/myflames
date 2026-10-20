"""Remembered connection passwords: OS stores, the encrypted vault, and profiles.

Every test runs with a temporary HOME and MYFLAMES_SECRET_BACKEND=vault (or a
mocked OS tool), so the real keychain is never touched.
"""
import contextlib
import json
import ctypes
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from myflames import secret_store
from myflames.secret_store import SecretStoreError, Vault
from myflames.workspace_store import WorkspaceStore

SECRET = 'S3cr3t pw "quoted" \\ é\nnext-line'


def saved_vault_password(profile_id):
    """The password saved for *profile_id* in the vault under ``$HOME``, or
    None. Profiles keep it under ``<id>.<endpoint fingerprint>.<random>``
    (workspace_store); more than one entry would be an orphan."""
    vault = Vault(Path.home() / '.myflames')
    entries = (vault._read() or {}).get('entries') or {}
    names = [name for name in entries if name.split('.')[0] == profile_id]
    if len(names) > 1:
        raise AssertionError('several saved passwords for %s: %s' % (profile_id, names))
    return vault.get(names[0]) if names else None


class IsolatedHome(unittest.TestCase):
    backend = 'vault'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = {'HOME': str(self.home), 'USERPROFILE': str(self.home), 'MYFLAMES_SECRET_BACKEND': self.backend}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.vault = Vault(self.home / '.myflames')


class TestVault(IsolatedHome):
    def test_windows_protects_existing_key_vault_and_workspace_files(self):
        self.vault.save('p1', SECRET)
        files = ['workspace.sqlite3', 'workspace.sqlite3-wal', 'workspace.sqlite3-shm', 'workspace.sqlite3-journal']
        for name in files:
            (self.vault.directory / name).write_bytes(b'existing data')
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(secret_store, '_windows_owner_only', create=True) as protect:
            self.assertEqual(self.vault.get('p1'), SECRET)
        calls = {(str(c[0][0]), bool(c[1].get('directory'))) for c in protect.call_args_list}
        self.assertIn((str(self.vault.directory), True), calls)
        for name in files + ['vault.key', 'credentials.vault', 'vault.lock']:
            self.assertIn((str(self.vault.directory / name), False), calls)

    def test_windows_protects_new_files_before_writing_secrets(self):
        protected = []

        def protect(path, directory=False):
            path = Path(path)
            if not directory:
                protected.append((path.name, path.stat().st_size))

        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(secret_store, '_windows_owner_only', side_effect=protect, create=True):
            self.vault.save('p1', SECRET)
        self.assertIn(('vault.key', 0), protected)
        self.assertTrue(any(name.startswith('.credentials.') and size == 0 for name, size in protected))

    def test_windows_acl_failure_stops_reads_and_writes(self):
        self.vault.save('p1', SECRET)
        before = self.vault.path.read_bytes()
        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(secret_store, '_windows_owner_only', create=True,
                                  side_effect=SecretStoreError('Cannot enforce owner-only Windows permissions')):
            for operation in (lambda: self.vault.get('p1'), lambda: self.vault.save('p2', SECRET)):
                with self.assertRaisesRegex(SecretStoreError, 'Cannot enforce owner-only'):
                    operation()
        self.assertEqual(self.vault.path.read_bytes(), before)

    def test_windows_acl_failure_removes_empty_new_key_and_temp_file(self):
        self.vault._ensure_dir()

        def reject_key(path, directory=False):
            if Path(path).name == 'vault.key':
                raise SecretStoreError('key protection failed')

        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(secret_store, '_windows_owner_only', side_effect=reject_key, create=True):
            with self.assertRaisesRegex(SecretStoreError, 'key protection failed'):
                self.vault._key_file(True)
        self.assertFalse(self.vault.key_path.exists())
        self.vault.save('p1', SECRET)
        before = self.vault.path.read_bytes()

        def reject_temp(path, directory=False):
            if Path(path).name.startswith('.credentials.'):
                raise SecretStoreError('temp protection failed')

        with mock.patch.object(sys, 'platform', 'win32'), \
                mock.patch.object(secret_store, '_windows_owner_only', side_effect=reject_temp, create=True):
            with self.assertRaisesRegex(SecretStoreError, 'temp protection failed'):
                self.vault.save('p2', SECRET)
        self.assertEqual(self.vault.path.read_bytes(), before)
        self.assertFalse(list(self.vault.directory.glob('.credentials.*')))

    @unittest.skipUnless(os.name == 'posix', 'POSIX storage safety')
    def test_security_vault_rejects_symlinks_without_touching_targets(self):
        for name in ('directory', 'credentials.vault', 'vault.key', 'vault.lock'):
            with self.subTest(name=name):
                vault = Vault(self.home / ('vault-' + name))
                vault.save('p1', SECRET)
                target = self.home / ('target-' + name)
                if name == 'directory':
                    vault.directory.rename(target)
                    vault.directory.symlink_to(target, target_is_directory=True)
                    target.chmod(0o755)
                else:
                    source = vault.directory / name
                    source.rename(target)
                    source.symlink_to(target)
                before_mode = target.stat().st_mode
                before_bytes = None if name == 'directory' else target.read_bytes()
                for action in (lambda: vault.get('p1'), lambda: vault.save('p2', SECRET)):
                    with self.assertRaisesRegex(ValueError, 'link|symlink'):
                        action()
                self.assertEqual(target.stat().st_mode, before_mode)
                if before_bytes is not None:
                    self.assertEqual(target.read_bytes(), before_bytes)

    @unittest.skipUnless(os.name == 'posix', 'POSIX storage ownership')
    def test_security_vault_rejects_foreign_owned_paths(self):
        self.vault.save('p1', SECRET)
        real_lstat = os.lstat
        for target in (self.vault.directory, self.vault.path, self.vault.key_path,
                       self.vault.directory / 'vault.lock'):
            def foreign(path, *args, **kwargs):
                result = real_lstat(path, *args, **kwargs)
                if str(path) == str(target):
                    fields = list(result)
                    fields[4] = os.geteuid() + 1
                    return os.stat_result(fields)
                return result
            with self.subTest(target=target), mock.patch.object(os, 'lstat', side_effect=foreign):
                with self.assertRaisesRegex(ValueError, 'owner|owned'):
                    self.vault.get('p1')

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits')
    def test_security_existing_vault_directory_and_lock_are_tightened(self):
        self.vault.save('p1', SECRET)
        lock = self.vault.directory / 'vault.lock'
        self.vault.directory.chmod(0o755)
        lock.chmod(0o666)
        self.assertEqual(self.vault.get('p1'), SECRET)
        self.assertEqual(self.vault.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == 'posix', 'POSIX no-follow opens')
    def test_security_vault_rejects_a_symlink_installed_during_open(self):
        self.vault.save('p1', SECRET)
        target = self.home / 'external-vault'
        target.write_bytes(self.vault.path.read_bytes())
        target.chmod(0o644)
        original = target.read_bytes()
        real_open = os.open
        swapped = []
        def swap(path, flags, *args, **kwargs):
            if str(path) == str(self.vault.path) and not swapped:
                self.vault.path.unlink()
                self.vault.path.symlink_to(target)
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, 'open', side_effect=swap):
            with self.assertRaises(ValueError):
                self.vault.get('p1')
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    @unittest.skipUnless(os.name == 'posix', 'POSIX descriptor ownership')
    def test_security_opened_descriptor_owner_is_checked_before_chmod(self):
        self.vault.save('p1', SECRET)
        self.vault.path.chmod(0o644)
        inode = self.vault.path.stat().st_ino
        real_fstat = os.fstat
        def foreign(fd):
            info = real_fstat(fd)
            if info.st_ino == inode:
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info
        with mock.patch.object(os, 'fstat', side_effect=foreign):
            with self.assertRaisesRegex(ValueError, 'owned'):
                self.vault.get('p1')
        self.assertEqual(self.vault.path.stat().st_mode & 0o777, 0o644)

    @unittest.skipUnless(os.name == 'posix', 'POSIX exclusive key creation')
    def test_security_key_creation_race_preserves_existing_key_permissions(self):
        self.vault._ensure_dir()
        real_open = os.open
        raced = []
        def create_other_key(path, flags, *args, **kwargs):
            if str(path) == str(self.vault.key_path) and flags & os.O_EXCL and not raced:
                self.vault.key_path.write_bytes(b'x' * 32)
                self.vault.key_path.chmod(0o644)
                raced.append(True)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(os, 'open', side_effect=create_other_key):
            with self.assertRaisesRegex(ValueError, 'chmod 600'):
                self.vault._key_file(True)
        self.assertEqual(self.vault.key_path.read_bytes(), b'x' * 32)
        self.assertEqual(self.vault.key_path.stat().st_mode & 0o777, 0o644)

    def test_round_trip_update_and_delete(self):
        self.assertIsNone(self.vault.get('p1'))
        self.vault.save('p1', SECRET)
        self.vault.save('p2', 'other')
        self.assertEqual(self.vault.get('p1'), SECRET)
        self.assertEqual(Vault(self.home / '.myflames').get('p2'), 'other')
        self.vault.save('p1', 'changed')
        self.assertEqual(self.vault.get('p1'), 'changed')
        self.vault.delete('p1')
        self.vault.delete('p1')
        self.assertIsNone(self.vault.get('p1'))
        self.assertEqual(self.vault.get('p2'), 'other')

    def test_file_holds_only_ciphertext_with_fresh_nonces(self):
        self.vault.save('p1', SECRET)
        first = json.loads(self.vault.path.read_text())['entries']['p1']
        self.vault.save('p1', SECRET)
        second = json.loads(self.vault.path.read_text())['entries']['p1']
        self.assertNotEqual(first['nonce'], second['nonce'])
        self.assertNotEqual(first['ciphertext'], second['ciphertext'])
        raw = self.vault.path.read_bytes()
        for needle in (b'S3cr3t', SECRET.encode('utf-8').hex().encode()):
            self.assertNotIn(needle, raw)
        self.assertEqual(len(self.vault.key_path.read_bytes()), 32)

    def _entries(self):
        return json.loads(self.vault.path.read_text())

    def _rewrite(self, data):
        self.vault.path.write_text(json.dumps(data))

    def test_tampering_is_detected_and_never_returns_garbage(self):
        self.vault.save('p1', SECRET)
        self.vault.save('p2', 'other')
        original = self._entries()
        for field in ('ciphertext', 'tag', 'nonce'):
            with self.subTest(field=field):
                data = json.loads(json.dumps(original))
                value = bytearray.fromhex(data['entries']['p1'][field])
                value[0] ^= 1
                data['entries']['p1'][field] = value.hex()
                self._rewrite(data)
                with self.assertRaisesRegex(ValueError, 'integrity'):
                    self.vault.get('p1')
        # An entry moved to another profile ID fails: the tag binds the key.
        data = json.loads(json.dumps(original))
        data['entries']['p2'] = data['entries']['p1']
        self._rewrite(data)
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.vault.get('p2')
        data['entries']['p2'] = {'nonce': 'zz'}
        self._rewrite(data)
        with self.assertRaisesRegex(ValueError, 'corrupt'):
            self.vault.get('p2')
        self.vault.path.write_text('{not json')
        with self.assertRaisesRegex(ValueError, 'corrupt'):
            self.vault.get('p1')

    def test_a_different_or_missing_key_file_cannot_decrypt(self):
        self.vault.save('p1', SECRET)
        self.vault.key_path.write_bytes(os.urandom(32))
        with self.assertRaisesRegex(ValueError, 'integrity'):
            self.vault.get('p1')
        self.vault.key_path.unlink()
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.vault.get('p1')

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_owner_only_permissions(self):
        self.vault.save('p1', SECRET)
        self.assertEqual(stat.S_IMODE(self.vault.key_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.vault.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.vault.directory.stat().st_mode), 0o700)
        os.chmod(str(self.vault.key_path), 0o644)
        with self.assertRaisesRegex(ValueError, 'chmod 600'):
            self.vault.get('p1')

    def test_atomic_write_keeps_the_previous_vault_on_failure(self):
        self.vault.save('p1', SECRET)
        before = self.vault.path.read_bytes()
        with mock.patch('myflames.secret_store.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.vault.save('p2', 'other')
        self.assertEqual(self.vault.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.vault.directory.iterdir()),
                         ['credentials.vault', 'vault.key', 'vault.lock'])
        self.assertEqual(self.vault.get('p1'), SECRET)

    def test_passphrase_mode_wrong_and_missing_passphrase(self):
        with mock.patch.object(secret_store, 'PBKDF2_ITERATIONS', 1000), \
                mock.patch.dict(os.environ, {'MYFLAMES_VAULT_PASSPHRASE': 'correct horse'}):
            self.vault.save('p1', SECRET)
            self.assertEqual(self.vault.get('p1'), SECRET)
            header = self._entries()['kdf']
            self.assertEqual((header['type'], header['iterations']), ('pbkdf2-sha256', 1000))
        self.assertFalse(self.vault.key_path.exists())
        with mock.patch.dict(os.environ, {'MYFLAMES_VAULT_PASSPHRASE': 'wrong'}):
            with self.assertRaisesRegex(ValueError, 'Wrong MYFLAMES_VAULT_PASSPHRASE'):
                self.vault.get('p1')
        with self.assertRaisesRegex(ValueError, 'protected by a passphrase'):
            self.vault.get('p1')

    def test_default_iterations_follow_current_guidance(self):
        self.assertGreaterEqual(secret_store.PBKDF2_ITERATIONS, 600000)

    def test_hkdf_matches_rfc_5869_case_1(self):
        okm = secret_store.hkdf_sha256(b'\x0b' * 22, bytes(range(13)), bytes(range(0xf0, 0xfa)), 42)
        self.assertEqual(okm.hex(), '3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf'
                                    '34007208d5b887185865')

    def test_rejects_unsafe_keys_and_empty_or_huge_secrets(self):
        for key in ('', '-U', 'a b', 'x' * 201, None, 'a\n'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                secret_store.save_secret(key, 'pw')
        for secret in ('', None, 'x' * 4097):
            with self.subTest(secret=str(secret)[:5]), self.assertRaises(ValueError):
                secret_store.save_secret('p1', secret)


class TestBackendSelection(IsolatedHome):
    def patch_native(self, mac=False, linux=False, windows=False):
        stack = [mock.patch.object(secret_store.MacKeychain, 'available', classmethod(lambda cls: mac)),
                 mock.patch.object(secret_store.SecretService, 'available', classmethod(lambda cls: linux)),
                 mock.patch.object(secret_store.WindowsCredential, 'available', classmethod(lambda cls: windows))]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_overrides(self):
        self.patch_native(mac=True)
        for value, expected in (('vault', 'vault'), ('none', 'none'), ('keychain', 'macos-keychain'),
                                ('auto', 'macos-keychain'), ('', 'macos-keychain'), ('VAULT', 'vault')):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'MYFLAMES_SECRET_BACKEND': value}):
                self.assertEqual(secret_store.backend_name(), expected)
        with mock.patch.dict(os.environ, {'MYFLAMES_SECRET_BACKEND': 'plaintext'}):
            with self.assertRaisesRegex(ValueError, 'keychain, vault, or none'):
                secret_store.backend_name()
            self.assertFalse(secret_store.describe()['available'])

    def test_platform_detection_and_labels(self):
        os.environ['MYFLAMES_SECRET_BACKEND'] = 'auto'
        for flags, name, label in (({'linux': True}, 'secret-service', 'Secret Service'),
                                   ({'windows': True}, 'windows-credential', 'Windows Credential Manager'),
                                   ({}, 'vault', 'encrypted file')):
            with self.subTest(name=name):
                self.patch_native(**flags)
                self.assertEqual(secret_store.describe(), {'backend': name, 'label': label, 'available': True})
        self.patch_native(mac=True)
        self.assertEqual(secret_store.describe()['label'], 'macOS Keychain')

    def test_none_disables_remembering(self):
        os.environ['MYFLAMES_SECRET_BACKEND'] = 'none'
        with self.assertRaisesRegex(ValueError, 'disabled'):
            secret_store.save_secret('p1', 'pw')
        self.assertIsNone(secret_store.get_secret('p1', 'vault'))
        self.assertFalse((self.home / '.myflames').exists())

    def test_forced_keychain_without_an_os_store_fails_clearly(self):
        self.patch_native()
        os.environ['MYFLAMES_SECRET_BACKEND'] = 'keychain'
        self.assertEqual(secret_store.backend_name(), 'none')
        with self.assertRaisesRegex(ValueError, 'MYFLAMES_SECRET_BACKEND=vault'):
            secret_store.save_secret('p1', 'pw')

    def test_auto_falls_back_to_the_vault_when_the_os_store_fails(self):
        self.patch_native(mac=True)
        os.environ['MYFLAMES_SECRET_BACKEND'] = 'auto'
        with mock.patch.object(secret_store.MacKeychain, 'save', side_effect=SecretStoreError('locked')):
            self.assertEqual(secret_store.save_secret('p1', SECRET), 'vault')
        self.assertEqual(secret_store.get_secret('p1', 'vault'), SECRET)
        os.environ['MYFLAMES_SECRET_BACKEND'] = 'keychain'
        with mock.patch.object(secret_store.MacKeychain, 'save', side_effect=SecretStoreError('locked')):
            with self.assertRaisesRegex(ValueError, 'locked'):
                secret_store.save_secret('p2', SECRET)

    def test_vault_backend_end_to_end(self):
        self.assertEqual(secret_store.save_secret('p1', SECRET), 'vault')
        self.assertEqual(secret_store.get_secret('p1'), SECRET)
        secret_store.delete_secret('p1', 'vault')
        self.assertIsNone(secret_store.get_secret('p1'))


class TestWindowsACL(unittest.TestCase):
    """Mock Win32 calls, while exercising ctypes pointer handling and SDDL."""
    def test_protected_acl_replaces_all_grants_and_failures_are_reported(self):
        try:
            from ctypes import wintypes  # noqa: F401
        except ValueError:
            # Off Windows, older Pythons (3.7 here) reject wintypes.VARIANT_BOOL's 'v' type
            # on import; the production code only runs this on Windows.
            self.skipTest('ctypes.wintypes cannot be imported on this non-Windows Python %d.%d' % sys.version_info[:2])
        for directory, error in ((False, 0), (True, 0), (False, 5)):
            with self.subTest(directory=directory, error=error):
                advapi, kernel = mock.Mock(), mock.Mock()
                kernel.GetCurrentProcess.return_value = -1

                def open_token(process, access, out):
                    out._obj.value = 99
                    return 1

                def token_info(token, kind, buffer, size, needed):
                    needed._obj.value = 64
                    if buffer is not None:
                        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0] = 123
                        return 1
                    return 0

                def sid_string(sid, out):
                    out._obj.value = 'S-1-5-21-1234-5678-1001'
                    return 1

                def descriptor(sddl, revision, out, size):
                    out._obj.value = 456
                    return 1

                def dacl(sd, present, out, defaulted):
                    present._obj.value = 1
                    out._obj.value = 789
                    return 1

                advapi.OpenProcessToken.side_effect = open_token
                advapi.GetTokenInformation.side_effect = token_info
                advapi.ConvertSidToStringSidW.side_effect = sid_string
                advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = descriptor
                advapi.GetSecurityDescriptorDacl.side_effect = dacl
                advapi.SetNamedSecurityInfoW.return_value = error
                with mock.patch.object(ctypes, 'WinDLL', create=True,
                                       side_effect=lambda name, **kw: advapi if name == 'advapi32' else kernel):
                    if error:
                        with self.assertRaisesRegex(SecretStoreError, 'Cannot enforce owner-only Windows'):
                            secret_store._windows_owner_only('C:\\private data\\vault.key', directory=directory)
                    else:
                        secret_store._windows_owner_only('C:\\private data\\vault.key', directory=directory)
                sddl = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.call_args[0][0]
                self.assertEqual(sddl, 'D:P(A;%s;FA;;;S-1-5-21-1234-5678-1001)' % ('OICI' if directory else ''))
                args = advapi.SetNamedSecurityInfoW.call_args[0]
                self.assertEqual(args[:3], ('C:\\private data\\vault.key', 1, 0x80000004))
                self.assertEqual(args[5].value, 789)
                self.assertEqual(kernel.LocalFree.call_count, 2)
                kernel.CloseHandle.assert_called_once()


def completed(returncode=0, stdout=b'', stderr=b''):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class TestMacKeychain(unittest.TestCase):
    def test_secret_goes_through_stdin_never_argv(self):
        with mock.patch('myflames.secret_store.subprocess.run', return_value=completed()) as run:
            secret_store.MacKeychain().save('p1', SECRET)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ['/usr/bin/security', '-i'])
        stdin = run.call_args[1]['input'].decode('ascii')
        self.assertTrue(stdin.startswith('add-generic-password -U -a p1 -s myflames '))
        self.assertNotIn('S3cr3t', stdin)
        self.assertNotIn('\n', stdin.rstrip('\n'))
        hex_data = stdin.split(' -X ')[1].strip()
        self.assertEqual(secret_store._decode(bytes.fromhex(hex_data).decode('ascii')), SECRET)

    def test_lookup_decodes_and_maps_not_found(self):
        stored = (secret_store._encode(SECRET) + '\n').encode('ascii')
        with mock.patch('myflames.secret_store.subprocess.run', return_value=completed(stdout=stored)) as run:
            self.assertEqual(secret_store.MacKeychain().get('p1'), SECRET)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ['/usr/bin/security', 'find-generic-password', '-a', 'p1', '-s', 'myflames', '-w'])
        with mock.patch('myflames.secret_store.subprocess.run', return_value=completed(44)):
            self.assertIsNone(secret_store.MacKeychain().get('p1'))
            secret_store.MacKeychain().delete('p1')
        with mock.patch('myflames.secret_store.subprocess.run', return_value=completed(51)):
            with self.assertRaisesRegex(ValueError, 'denied or keychain locked'):
                secret_store.MacKeychain().get('p1')
            with self.assertRaises(ValueError):
                secret_store.MacKeychain().delete('p1')

    def test_failures_redact_the_secret(self):
        hex_data = secret_store._encode('pw-123').encode('ascii').hex()
        failure = completed(2, stderr=('bad -X %s pw-123' % hex_data).encode())
        with mock.patch('myflames.secret_store.subprocess.run', return_value=failure):
            with self.assertRaises(ValueError) as caught:
                secret_store.MacKeychain().save('p1', 'pw-123')
        self.assertNotIn('pw-123', str(caught.exception))
        self.assertNotIn(hex_data, str(caught.exception))
        with mock.patch('myflames.secret_store.subprocess.run', side_effect=OSError('no tool')):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                secret_store.MacKeychain().get('p1')

    def test_stderr_is_never_echoed(self):
        chunk = secret_store._encode('pw-123').encode('ascii').hex()[10:60]
        failure = completed(1, stderr=('unknown command "%s"' % chunk).encode())
        with mock.patch('myflames.secret_store.subprocess.run', return_value=failure):
            with self.assertRaises(ValueError) as caught:
                secret_store.MacKeychain().save('p1', 'pw-123')
        self.assertNotIn(chunk, str(caught.exception))

    def test_long_passwords_never_reach_security(self):
        with mock.patch('myflames.secret_store.subprocess.run') as run:
            with self.assertRaisesRegex(ValueError, 'too long'):
                secret_store.MacKeychain().save('p1', 'x' * 1200)
        run.assert_not_called()


class TestSecretService(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch('myflames.secret_store.shutil.which', return_value='/usr/bin/secret-tool')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_store_lookup_clear_keep_the_secret_off_argv(self):
        with mock.patch('myflames.secret_store.subprocess.run', return_value=completed()) as run:
            secret_store.SecretService().save('p1', SECRET)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:2], ['/usr/bin/secret-tool', 'store'])
        self.assertEqual(argv[-4:], ['service', 'myflames', 'account', 'p1'])
        self.assertFalse(any('S3cr3t' in part or SECRET in part for part in argv))
        self.assertEqual(secret_store._decode(run.call_args[1]['input'].decode('ascii')), SECRET)
        stored = secret_store._encode(SECRET).encode('ascii')
        with mock.patch('myflames.secret_store.subprocess.run', return_value=completed(stdout=stored)) as run:
            self.assertEqual(secret_store.SecretService().get('p1'), SECRET)
        self.assertEqual(run.call_args[0][0][1:], ['lookup', 'service', 'myflames', 'account', 'p1'])
        with mock.patch('myflames.secret_store.subprocess.run', return_value=completed(1)) as run:
            self.assertIsNone(secret_store.SecretService().get('p1'))
            secret_store.SecretService().delete('p1')
        self.assertEqual(run.call_args[0][0][1], 'clear')
        locked = completed(1, stderr=b'Cannot autolaunch D-Bus')
        with mock.patch('myflames.secret_store.subprocess.run', return_value=locked):
            with self.assertRaises(ValueError):
                secret_store.SecretService().get('p1')
            with self.assertRaises(ValueError):
                secret_store.SecretService().save('p1', SECRET)


@unittest.skipUnless(sys.platform == 'win32', 'Windows Credential Manager exists only on Windows')
class TestWindowsCredential(unittest.TestCase):
    def test_round_trip(self):
        key = 'myflames-test-' + uuid.uuid4().hex
        backend = secret_store.WindowsCredential()
        self.addCleanup(backend.delete, key)
        backend.save(key, SECRET)
        self.assertEqual(backend.get(key), SECRET)
        backend.delete(key)
        self.assertIsNone(backend.get(key))

    def test_unicode_password_round_trip(self):
        key = 'myflames-test-' + uuid.uuid4().hex
        backend = secret_store.WindowsCredential()
        self.addCleanup(backend.delete, key)
        backend.save(key, 'pässwörd-\u00e9-"quote"-\\back')
        self.assertEqual(backend.get(key), 'pässwörd-\u00e9-"quote"-\\back')


class TestWindowsCredentialLimit(IsolatedHome):
    """Passwords over CRED_MAX_CREDENTIAL_BLOB_SIZE never reach CredWriteW."""
    backend = 'auto'

    def test_long_password_is_refused_before_the_api(self):
        with mock.patch.object(secret_store.WindowsCredential, '_api') as api:
            with self.assertRaisesRegex(ValueError, 'too long'):
                secret_store.WindowsCredential().save('p1', 'x' * 1281)
        api.assert_not_called()

    @unittest.skipUnless(sys.platform == 'win32', 'Windows Credential Manager exists only on Windows')
    def test_auto_mode_falls_back_to_the_vault_for_long_passwords(self):
        key = 'myflames-test-' + uuid.uuid4().hex
        self.addCleanup(secret_store.delete_secret, key, 'vault')
        self.assertEqual(secret_store.save_secret(key, 'x' * 2000), 'vault')
        self.assertEqual(secret_store.get_secret(key, 'vault'), 'x' * 2000)
        short = 'myflames-test-' + uuid.uuid4().hex
        self.addCleanup(secret_store.delete_secret, short, 'windows-credential')
        self.assertEqual(secret_store.save_secret(short, SECRET), 'windows-credential')
        self.assertEqual(secret_store.get_secret(short, 'windows-credential'), SECRET)


class TestProfilePasswords(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.db = self.home / '.myflames' / 'workspace.sqlite3'
        self.store = WorkspaceStore(self.db)
        self.settings = {'name': 'prod', 'host': 'db.internal', 'port': 3307, 'user': 'app'}

    def save(self, **kwargs):
        return self.store.save('profile', dict(self.settings), **kwargs)

    def test_remembered_password_never_reaches_sqlite_or_records(self):
        saved = self.save(password=SECRET, remember=True)
        self.assertEqual((saved['remember_password'], saved['secret_backend']), (True, 'vault'))
        self.assertEqual((saved['has_saved_password'], saved['secret_backend_label']), (True, 'encrypted file'))
        self.assertNotIn('password', saved)
        raw = self.db.read_bytes()
        for needle in (b'S3cr3t', SECRET.encode('utf-8'), SECRET.encode('utf-8').hex().encode()):
            self.assertNotIn(needle, raw)
        self.assertNotIn('S3cr3t', json.dumps(self.store.list('profile')))
        self.assertEqual(self.store.list('profile'), [saved])
        self.assertEqual(saved_vault_password(saved['id']), SECRET)
        self.assertNotIn('secret_ref', saved)

    def test_fill_password_for_the_same_endpoint_only(self):
        saved = self.save(password=SECRET, remember=True)
        request = {'profile_id': saved['id'], 'host': 'db.internal', 'port': 3307, 'user': 'app'}
        self.assertEqual(self.store.connection_with_saved_password(request)['password'], SECRET)
        self.assertNotIn('password', request)
        typed = dict(request, password='typed')
        self.assertEqual(self.store.connection_with_saved_password(typed)['password'], 'typed')
        for change in ({'host': 'evil.example'}, {'port': 3306}, {'user': 'root'},
                       {'ssl_mode': 'DISABLED'}, {'ssl_ca': '/tmp/attacker-ca.pem'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'own host, port, user, and TLS'):
                self.store.connection_with_saved_password(dict(request, **change))
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.store.connection_with_saved_password(dict(request, profile_id='missing'))
        plain = self.store.save('profile', {'host': 'other'})
        unchanged = {'profile_id': plain['id'], 'host': 'other'}
        copied = self.store.connection_with_saved_password(unchanged)
        self.assertIsNot(copied, unchanged)
        self.assertEqual((copied['profile_id'], copied['host']), (plain['id'], 'other'))
        for name in list(self.vault._read()['entries']):
            self.vault.delete(name)
        with self.assertRaisesRegex(ValueError, 'missing from the encrypted file'):
            self.store.connection_with_saved_password(request)

    def test_security_connection_options_are_checked_before_secret_lookup(self):
        saved = self.save(password=SECRET, remember=True)
        request = dict(self.settings, profile_id=saved['id'])
        for field in ('binary', 'socket', 'ssl_cert', 'ssl_key', 'init_command', 'defaults_file', 'extra_flags'):
            with self.subTest(field=field), mock.patch.object(secret_store, 'get_secret') as get:
                with self.assertRaisesRegex(ValueError, field):
                    self.store.connection_with_saved_password(dict(request, **{field: '/attacker/value'}))
                get.assert_not_called()
        for changes in ({'port': 0}, {'read_only': 'yes'}, {'type': 'other'}, {'profile_id': []}):
            with self.subTest(changes=changes), mock.patch.object(secret_store, 'get_secret') as get:
                with self.assertRaises(ValueError):
                    self.store.connection_with_saved_password(dict(request, **changes))
                get.assert_not_called()

    def test_security_connection_sanitization_always_copies(self):
        plain = self.save()
        for request in ({'host': 'other', 'ignored': 'drop'},
                        dict(self.settings, profile_id=plain['id'], ignored='drop'),
                        dict(self.settings, profile_id=plain['id'], password='typed', ignored='drop')):
            with self.subTest(request=request):
                output = self.store.connection_with_saved_password(request)
                self.assertIsNot(output, request)
                self.assertNotIn('ignored', output)
                self.assertNotIn('name', output)
                self.assertEqual(output['host'], request['host'])
                self.assertIn('ignored', request)

    def test_update_forget_and_delete_manage_the_secret(self):
        with self.assertRaisesRegex(ValueError, 'Enter the password'):
            self.save(remember=True)
        saved = self.save(password=SECRET, remember=True)
        key = saved['id']
        # Updating other settings keeps the remembered password.
        again = self.store.save('profile', dict(self.settings, database='shop'), key)
        self.assertTrue(again['has_saved_password'])
        # Moving the profile to another host requires the password again.
        with self.assertRaisesRegex(ValueError, 'previous host'):
            self.store.save('profile', dict(self.settings, host='new.internal'), key)
        moved = self.store.save('profile', dict(self.settings, host='new.internal'), key, password='new', remember=True)
        self.assertEqual(saved_vault_password(key), 'new')
        self.assertTrue(moved['has_saved_password'])
        forgotten = self.store.forget_password(key)
        self.assertEqual((forgotten['has_saved_password'], forgotten['secret_backend']), (False, ''))
        self.assertEqual(forgotten['host'], 'new.internal')
        self.assertIsNone(saved_vault_password(key))
        self.store.save('profile', dict(self.settings), key, password='again', remember=True)
        off = self.store.save('profile', dict(self.settings), key, remember=False)
        self.assertFalse(off['has_saved_password'])
        self.assertIsNone(saved_vault_password(key))
        self.store.save('profile', dict(self.settings), key, password='again', remember=True)
        self.store.delete('profile', key)
        self.assertIsNone(saved_vault_password(key))
        self.assertEqual(self.store.list('profile'), [])
        with self.assertRaisesRegex(ValueError, 'no longer exists'):
            self.store.forget_password(key)

    def test_failed_record_write_removes_the_new_secret(self):
        bundle_id = 'shared-id'
        self.store._connect().close()
        with contextlib.closing(sqlite3.connect(str(self.db))) as conn, conn:
            conn.execute("INSERT INTO records VALUES (?, 'investigation', '', '{}')", (bundle_id,))
        with self.assertRaisesRegex(ValueError, 'type'):
            self.store.save('profile', dict(self.settings), bundle_id, password=SECRET, remember=True)
        self.assertIsNone(saved_vault_password(bundle_id))
        self.assertEqual(self.vault._read()['entries'], {})

    def request(self, saved, **change):
        return dict({'profile_id': saved['id'], 'host': saved['host'], 'port': saved['port'],
                     'user': saved['user']}, **change)

    def interleave(self, first, second, finish_second_first):
        """Save *first* and *second* (settings, password) to one profile with
        the second save running inside the first: after the first save wrote
        its secret, and before (or, with *finish_second_first* False, after)
        the first save's record is committed."""
        key = self.save(password='original', remember=True)['id']
        real_save = secret_store.save_secret
        real_transaction = WorkspaceStore._transaction
        state = {'done': False}

        def run_second():
            if not state['done']:
                state['done'] = True
                WorkspaceStore(self.db).save('profile', second[0], key, password=second[1], remember=True)

        def save_secret(ref, password):
            backend = real_save(ref, password)
            if finish_second_first and password == first[1]:
                run_second()
            return backend

        def transaction(store):
            conn = real_transaction(store)
            if not finish_second_first and not state['done'] and store is self.store:
                # The first save holds the write lock: the second save's
                # secret is written, then its transaction waits for this one.
                thread = threading.Thread(target=run_second)
                thread.start()
                self.addCleanup(thread.join, 30)
                state['thread'] = thread
            return conn

        with mock.patch.object(secret_store, 'save_secret', save_secret), \
                mock.patch.object(WorkspaceStore, '_transaction', transaction):
            self.store.save('profile', first[0], key, password=first[1], remember=True)
        if 'thread' in state:
            state['thread'].join(30)
        self.assertTrue(state['done'])
        return key

    def test_interleaved_saves_never_pair_one_endpoint_with_the_other_password(self):
        a = (dict(self.settings, host='a.internal'), 'password-for-a')
        b = (dict(self.settings, host='b.internal'), 'password-for-b')
        for finish_second_first in (True, False):
            with self.subTest(finish_second_first=finish_second_first):
                key = self.interleave(a, b, finish_second_first)
                profile = self.store.get('profile', key)
                winner, loser = (a, b) if finish_second_first else (b, a)
                self.assertEqual(profile['host'], winner[0]['host'])
                request = self.request(profile)
                self.assertEqual(self.store.connection_with_saved_password(request)['password'], winner[1])
                with self.assertRaisesRegex(ValueError, 'own host'):
                    self.store.connection_with_saved_password(dict(request, host=loser[0]['host']))
                # One entry remains: the losing save's secret and the original are gone.
                self.assertEqual(saved_vault_password(key), winner[1])
                self.store.delete('profile', key)
                self.assertIsNone(saved_vault_password(key))

    def test_password_is_released_only_to_the_endpoint_in_its_key(self):
        """Editing the host in workspace.sqlite3 (or a record whose
        secret_ref belongs to another endpoint) never redirects the password."""
        saved = self.save(password=SECRET, remember=True)
        with contextlib.closing(sqlite3.connect(str(self.db))) as conn, conn:
            payload = json.loads(conn.execute('SELECT payload FROM records WHERE id=?', (saved['id'],)).fetchone()[0])
            payload['host'] = 'evil.example'
            conn.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(payload), saved['id']))
        with self.assertRaisesRegex(ValueError, 'saved for another host'):
            self.store.connection_with_saved_password(self.request(saved, host='evil.example'))

    def test_password_saved_before_secret_ref_is_still_read_and_moved_on_save(self):
        saved = self.save()
        secret_store.save_secret(saved['id'], 'legacy-password')
        with contextlib.closing(sqlite3.connect(str(self.db))) as conn, conn:
            payload = json.loads(conn.execute('SELECT payload FROM records WHERE id=?', (saved['id'],)).fetchone()[0])
            payload.update(remember_password=True, secret_backend='vault')
            conn.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(payload), saved['id']))
        request = self.request(saved)
        self.assertEqual(self.store.connection_with_saved_password(request)['password'], 'legacy-password')
        with self.assertRaisesRegex(ValueError, 'own host'):
            self.store.connection_with_saved_password(dict(request, host='evil.example'))
        # Editing other fields keeps it where it is.
        self.store.save('profile', dict(self.settings, database='shop'), saved['id'])
        self.assertEqual(self.store.connection_with_saved_password(request)['password'], 'legacy-password')
        # Saving a password moves it to the bound key and removes the old entry.
        self.store.save('profile', dict(self.settings), saved['id'], password='new-password', remember=True)
        self.assertEqual(list(self.vault._read()['entries'])[0].split('.')[0], saved['id'])
        self.assertIsNone(secret_store.get_secret(saved['id'], 'vault'))
        self.assertEqual(saved_vault_password(saved['id']), 'new-password')

    def test_find_profile_by_id_or_name(self):
        saved = self.save()
        self.assertEqual(self.store.find_profile(saved['id'])['id'], saved['id'])
        self.assertEqual(self.store.find_profile('prod')['id'], saved['id'])
        with self.assertRaisesRegex(ValueError, 'myflames profiles list'):
            self.store.find_profile('staging')
        self.save()
        with self.assertRaisesRegex(ValueError, 'Several'):
            self.store.find_profile('prod')


class TestVaultCrossProcessLock(IsolatedHome):
    def test_concurrent_processes_keep_every_entry(self):
        script = (
            "import sys; from myflames.secret_store import Vault\n"
            "v = Vault(sys.argv[1])\n"
            "for i in range(40): v.save('%s-%d' % (sys.argv[2], i), 'pw')\n")
        procs = [subprocess.Popen([sys.executable, '-c', script, str(self.vault.directory), name],
                                  cwd=str(Path(__file__).resolve().parents[1]))
                 for name in ('a', 'b', 'c')]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=120), 0)
        for name in ('a', 'b', 'c'):
            for i in range(40):
                self.assertEqual(self.vault.get('%s-%d' % (name, i)), 'pw')


if __name__ == '__main__':
    unittest.main()
