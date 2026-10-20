"""myflames.tls: the HTTPS context used by release checks, tool update checks and news."""
import os
import ssl
import subprocess
import sys
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from myflames import app_update, news, tls, tool_updates  # noqa: E402

PEM = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n'


def _context(ca_count):
    context = mock.MagicMock(spec=ssl.SSLContext)
    context.cert_store_stats.return_value = {'x509': ca_count, 'crl': 0, 'x509_ca': ca_count}
    return context


def _done(returncode=0, stdout=PEM.encode('ascii')):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b'')


class HttpsContextTests(unittest.TestCase):

    def setUp(self):
        tls._ROOTS = None
        self.addCleanup(setattr, tls, '_ROOTS', None)

    def test_default_trust_is_used_as_is(self):
        context = _context(150)
        with mock.patch.object(tls.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(tls.sys, 'platform', 'darwin'), \
                mock.patch.object(tls.subprocess, 'run') as run:
            self.assertIs(tls.https_context(), context)
        run.assert_not_called()
        context.load_verify_locations.assert_not_called()

    def test_empty_trust_off_macos_is_left_alone(self):
        context = _context(0)
        with mock.patch.object(tls.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(tls.sys, 'platform', 'linux'), \
                mock.patch.object(tls.subprocess, 'run') as run:
            self.assertIs(tls.https_context(), context)
        run.assert_not_called()
        context.load_verify_locations.assert_not_called()

    def test_empty_trust_on_macos_loads_each_system_keychain(self):
        context = _context(0)
        with mock.patch.object(tls.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(tls.sys, 'platform', 'darwin'), \
                mock.patch.object(tls.subprocess, 'run', return_value=_done()) as run:
            tls.https_context()
        self.assertEqual(
            [c[0][0] for c in run.call_args_list],
            [['/usr/bin/security', 'find-certificate', '-a', '-p', keychain] for keychain in tls.KEYCHAINS])
        for c in run.call_args_list:
            self.assertEqual(c[1]['timeout'], tls.EXPORT_TIMEOUT)
            self.assertFalse(c[1].get('shell', False))
        self.assertEqual(context.load_verify_locations.call_args_list, [mock.call(cadata=PEM)] * len(tls.KEYCHAINS))

    def test_system_roots_are_exported_once_per_process(self):
        with mock.patch.object(tls.ssl, 'create_default_context', side_effect=lambda: _context(0)), \
                mock.patch.object(tls.sys, 'platform', 'darwin'), \
                mock.patch.object(tls.subprocess, 'run', return_value=_done()) as run:
            tls.https_context()
            tls.https_context()
        self.assertEqual(run.call_count, len(tls.KEYCHAINS))

    def test_export_failures_leave_a_verifying_context(self):
        for failure in (OSError('no such file'), subprocess.TimeoutExpired('security', 10)):
            tls._ROOTS = None
            context = _context(0)
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(tls.ssl, 'create_default_context', return_value=context), \
                    mock.patch.object(tls.sys, 'platform', 'darwin'), \
                    mock.patch.object(tls.subprocess, 'run', side_effect=failure):
                self.assertIs(tls.https_context(), context)
            context.load_verify_locations.assert_not_called()

    def test_failed_or_empty_exports_are_skipped(self):
        context = _context(0)
        outputs = [_done(returncode=44, stdout=PEM.encode('ascii')), _done(stdout=b'')]
        with mock.patch.object(tls.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(tls.sys, 'platform', 'darwin'), \
                mock.patch.object(tls.subprocess, 'run', side_effect=outputs):
            tls.https_context()
        context.load_verify_locations.assert_not_called()

    def test_one_unreadable_keychain_does_not_cost_the_other(self):
        context = _context(0)
        context.load_verify_locations.side_effect = [ssl.SSLError('bad entry'), None]
        with mock.patch.object(tls.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(tls.sys, 'platform', 'darwin'), \
                mock.patch.object(tls.subprocess, 'run', return_value=_done()):
            self.assertIs(tls.https_context(), context)
        self.assertEqual(context.load_verify_locations.call_count, 2)

    def test_real_context_verifies_certificates_and_host_names(self):
        context = tls.https_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class CallSitesUseHttpsContextTests(unittest.TestCase):
    """Every HTTPS read goes through tls.https_context."""

    def test_release_check_passes_the_context(self):
        marker = object()
        with mock.patch.object(app_update, 'https_context', return_value=marker), \
                mock.patch.dict(os.environ, {'MYFLAMES_TEST_UPDATE': ''}), \
                mock.patch.object(app_update.urllib.request, 'urlopen', side_effect=OSError('offline')) as urlopen:
            app_update._CACHE.clear()
            try:
                app_update.check_for_updates(force=True)
            except Exception:
                pass
        self.assertTrue(urlopen.called, 'the release check did not reach urlopen')
        self.assertIs(urlopen.call_args[1].get('context'), marker)

    def test_tool_update_fetch_uses_the_context(self):
        handlers = []
        real = urllib.request.build_opener

        def build(*args):
            handlers.extend(args)
            opener = real(*args)
            opener.open = mock.Mock(side_effect=OSError('offline'))
            return opener
        marker = ssl.create_default_context()
        with mock.patch.object(tool_updates, 'https_context', return_value=marker), \
                mock.patch.object(tool_updates.urllib.request, 'build_opener', side_effect=build):
            with self.assertRaises(OSError):
                tool_updates._fetch(tool_updates.SHELL_DOWNLOAD)
        https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
        self.assertEqual(len(https), 1)
        self.assertIs(https[0]._context, marker)

    def test_news_handler_uses_the_context(self):
        marker = ssl.create_default_context()
        with mock.patch.object(news, 'https_context', return_value=marker):
            handler = news._HTTPS(watchdog=mock.Mock())
        self.assertIs(handler._context, marker)


if __name__ == '__main__':
    unittest.main()
