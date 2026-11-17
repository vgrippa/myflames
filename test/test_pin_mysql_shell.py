"""Pure maintainer pinning helpers, without network or gpg."""
import copy
import importlib.util
import json
from pathlib import Path
import unittest

from myflames import managed_shell

SPEC = importlib.util.spec_from_file_location('pin_mysql_shell', str(Path(__file__).resolve().parent.parent / 'scripts/pin-mysql-shell.py'))
pin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pin)


class PinShellTests(unittest.TestCase):
    VALID = '[GNUPG:] VALIDSIG %s 2026-11-16 1794850000 0 4 0 1 10 00 %s\n' % (managed_shell.SIGNING_KEY, managed_shell.SIGNING_KEY)

    def test_accepts_only_pinned_machine_readable_validsig(self):
        self.assertTrue(pin.valid_gpg_status('[GNUPG:] NEWSIG\n' + self.VALID + '[GNUPG:] TRUST_UNDEFINED 0 pgp\n'))
        for wrong in ('', 'gpg: Good signature\n', self.VALID.replace('[GNUPG:] ', 'gpg: '),
                      self.VALID.replace(managed_shell.SIGNING_KEY, 'A' * 40),
                      '[GNUPG:] VALIDSIG ' + managed_shell.SIGNING_KEY,
                      'prefix ' + self.VALID):
            with self.subTest(status=wrong):
                self.assertFalse(pin.valid_gpg_status(wrong))

    def test_any_bad_expired_revoked_or_failed_signature_refuses(self):
        for status in ('BADSIG', 'ERRSIG', 'EXPSIG', 'EXPKEYSIG', 'REVKEYSIG', 'NO_PUBKEY', 'FAILURE', 'NODATA', 'BADARMOR'):
            with self.subTest(status=status):
                self.assertFalse(pin.valid_gpg_status(self.VALID + '[GNUPG:] ' + status + ' detail\n'))
                self.assertFalse(pin.valid_gpg_status('[GNUPG:] ' + status + '\n' + self.VALID))

    def test_render_validates_complete_lock_and_roundtrips(self):
        lock = managed_shell.load_lock()
        archives = copy.deepcopy(lock['archives'])
        rendered = pin.render_lock('9.7.0', archives)
        self.assertEqual(json.loads(rendered), lock)
        self.assertTrue(rendered.endswith('\n'))
        self.assertEqual(rendered, pin.render_lock('9.7.0', archives))
        self.assertNotIn('macos_team_id', rendered)
        self.assertEqual(archives, lock['archives'], 'Rendering must not mutate caller data')

    def test_incomplete_or_unverified_lock_cannot_render(self):
        archives = copy.deepcopy(managed_shell.load_lock()['archives'])
        del archives['win32-x64']
        with self.assertRaises(managed_shell.Refused):
            pin.render_lock('9.7.0', archives)
        archives = copy.deepcopy(managed_shell.load_lock()['archives'])
        archives['darwin-arm64']['sha256'] = '<not computed>'
        with self.assertRaises(managed_shell.Refused):
            pin.render_lock('9.7.0', archives)

    def test_version_cannot_escape_output_folder(self):
        self.assertEqual(set(pin.archive_roots('9.7.0')), set(managed_shell.PLATFORMS))
        for version in ('../9.7.0', '9.7', '9.7.0/evil', '9.7.0?x', '9.7.0\n'):
            with self.subTest(version=version), self.assertRaises(ValueError):
                pin.archive_roots(version)


if __name__ == '__main__':
    unittest.main()
