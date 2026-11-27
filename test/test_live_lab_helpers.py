"""Lab error redaction works without loading profiles or contacting a server."""
import contextlib
import io
import os
import sys
import tempfile
import traceback
import types
import unittest
from unittest import mock

from live import _runner

_lab = _runner._lab


class LiveLabHelpersTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.credentials = os.path.join(temporary.name, 'credentials.env')
        for patcher in (
                mock.patch.dict(os.environ, CLUSTER_LAB=temporary.name,
                                HOME=temporary.name, USERPROFILE=temporary.name,
                                MYFLAMES_LIVE_HOME=temporary.name,
                                MYFLAMES_SECRET_BACKEND='vault'),
                mock.patch.object(_lab, '_PASSWORD', None),
                mock.patch.object(_lab, '_CLUSTER_PASSWORDS', None),
                mock.patch.object(_lab, '_GA_PASSWORDS', None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.passwords = ('fake-admin-secret', 'fake-admin-secret-long')
        with open(self.credentials, 'w', encoding='utf-8') as handle:
            handle.write("MF_ADMIN_PASSWORD='%s'\nSST_PASSWORD=\"%s\"\n"
                         "NOT_A_SECRET=public-value\nEMPTY_PASSWORD=''\n" % self.passwords)
        self.lab = object.__new__(_lab.Lab)
        self.lab.ws = types.SimpleNamespace()

    def test_redact_loads_every_cluster_password_without_lab_password(self):
        with mock.patch.object(_lab, 'lab_password') as load_password:
            text = _lab.redact('%s / %s / public-value' % self.passwords)
        self.assertEqual(text, '[redacted] / [redacted] / public-value')
        self.assertIsNone(_lab._PASSWORD)
        load_password.assert_not_called()

    def test_redact_caches_the_first_credentials_read(self):
        _lab.redact('first error')
        with mock.patch('builtins.open') as read:
            self.assertEqual(_lab.redact(' / '.join(self.passwords)),
                             '[redacted] / [redacted]')
        read.assert_not_called()

    def test_redact_tolerates_missing_credentials_and_caches_the_result(self):
        os.rename(self.credentials, self.credentials + '.aside')
        self.assertEqual(_lab.redact('no lab here'), 'no lab here')
        with mock.patch('builtins.open') as read:
            self.assertEqual(_lab.redact('another error'), 'another error')
        read.assert_not_called()

    def test_redact_tolerates_unreadable_credentials(self):
        with mock.patch('builtins.open', side_effect=PermissionError('unreadable')):
            self.assertEqual(_lab.redact('cannot read credentials'), 'cannot read credentials')

    def test_redact_tolerates_invalid_credentials_encoding(self):
        with open(self.credentials, 'wb') as handle:
            handle.write(b'\xff')
        self.assertEqual(_lab.redact('cannot decode credentials'), 'cannot decode credentials')

    def test_redact_retains_loaded_admin_and_ga_passwords_without_a_cluster_file(self):
        os.rename(self.credentials, self.credentials + '.aside')
        with mock.patch.object(_lab, '_PASSWORD', self.passwords[0]), \
                mock.patch.object(_lab, '_GA_PASSWORDS', {'GA_PASSWORD': self.passwords[1]}):
            self.assertEqual(_lab.redact(' / '.join(self.passwords)),
                             '[redacted] / [redacted]')
        self.assertEqual(_lab.redact(None), '')

    def test_credentials_path_honors_creds_file(self):
        override = os.path.join(os.path.dirname(self.credentials), 'override.env')
        self.assertEqual(_lab.credentials_path(), self.credentials)
        with mock.patch.dict(os.environ, CREDS_FILE=override):
            self.assertEqual(_lab.credentials_path(), override)

    def test_redact_masks_creds_file_and_default_passwords(self):
        override = os.path.join(os.path.dirname(self.credentials), 'override.env')
        with open(override, 'w', encoding='utf-8') as handle:
            handle.write("MF_ADMIN_PASSWORD='override-secret'\n")
        with mock.patch.dict(os.environ, CREDS_FILE=override):
            text = _lab.redact(' / '.join(self.passwords + ('override-secret',)))
        self.assertEqual(text, '[redacted] / [redacted] / [redacted]')

    def test_runner_redacts_skip_reasons(self):
        result = _runner._Result()
        self.addCleanup(result.stream.close)
        case = unittest.FunctionTestCase(lambda: None)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result.addSkip(case, 'probe failed with password ' + self.passwords[0])
        self.assertIn('  reason: probe failed with password [redacted]', stdout.getvalue())
        self.assertNotIn(self.passwords[0], stdout.getvalue())
        self.assertEqual(result.records[-1]['reason'],
                         'probe failed with password [redacted]')

    def test_api_failure_suppresses_the_raw_exception_in_formatted_tracebacks(self):
        message = 'login failed: ' + ' / '.join(self.passwords)
        with mock.patch('myflames.ui.api_response', side_effect=ValueError(message)):
            try:
                self.lab.api('/api/live/cluster')
            except _lab.LabError as exc:
                formatted = ''.join(traceback.format_exception(*sys.exc_info()))
                self.assertEqual(str(exc), 'login failed: [redacted] / [redacted]')
                self.assertTrue(exc.__suppress_context__)
            else:
                self.fail('the failed API call did not raise LabError')
        self.assertIn('LabError: login failed: [redacted] / [redacted]', formatted)
        self.assertNotIn('During handling of the above exception', formatted)
        for password in self.passwords:
            self.assertNotIn(password, formatted)

    def test_runner_redacts_api_and_raw_failure_tracebacks(self):
        lab = self.lab
        message = 'login failed: ' + ' / '.join(self.passwords)

        class FailingSuite(unittest.TestCase):
            def test_api_failure(self):
                lab.api('/api/live/cluster')

            def test_raw_failure(self):
                # Exercise the runner's own redaction, independent of Lab.api.
                raise RuntimeError(message)

        module = types.ModuleType('fake_live_suite')
        module.FailingSuite = FailingSuite
        stdout, stderr = io.StringIO(), io.StringIO()
        result = _runner._Result()
        self.addCleanup(result.stream.close)
        with mock.patch.object(_runner, 'load_suite_module', return_value=module), \
                mock.patch.object(_runner, '_Result', return_value=result), \
                mock.patch('myflames.ui.api_response', side_effect=ValueError(message)), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = _runner.main(['fake_live_suite'])
        self.assertEqual(status, 1)
        self.assertEqual(len(result.errors), 2)
        self.assertIn('LabError: login failed: [redacted] / [redacted]', stderr.getvalue())
        self.assertIn('RuntimeError: login failed: [redacted] / [redacted]', stderr.getvalue())
        self.assertIn('SUMMARY ', stdout.getvalue())
        for password in self.passwords:
            self.assertNotIn(password, stdout.getvalue() + stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
