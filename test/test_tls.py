"""myflames.tls: the HTTPS context used by release checks, tool update checks and news."""
import hashlib
import os
import plistlib
import ssl
import subprocess
import sys
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from myflames import app_update, news, tls, tool_updates  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'tls', 'test-root-ca.pem')
with open(FIXTURE) as _handle:
    ROOT_PEM = _handle.read().strip()
#: Stands in for an exported intermediate or identity: not a root in any trust domain.
OTHER_PEM = '-----BEGIN CERTIFICATE-----\nTUlJQg==\n-----END CERTIFICATE-----'


def fingerprint(pem):
    return hashlib.sha1(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()


class FakeSecurity:
    """/usr/bin/security: keychain exports and trust settings per domain (None = export fails)."""

    def __init__(self, keychains=None, trust=None, fail=None):
        self.keychains = keychains if keychains is not None else {tls.KEYCHAINS[0]: ROOT_PEM + '\n' + OTHER_PEM + '\n'}
        self.trust = trust if trust is not None else {'-s': {fingerprint(ROOT_PEM): {}}, '-d': None, '-u': None}
        self.fail = fail
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.fail is not None:
            raise self.fail
        self.assert_safe(argv, kwargs)
        if argv[1] == 'find-certificate':
            text = self.keychains.get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if text is not None else 44, (text or '').encode('ascii'), b'')
        flags = argv[2:-1]
        domain = flags[0] if flags else '-u'
        listed = self.trust.get(domain)
        if listed is None:
            return subprocess.CompletedProcess(argv, 1, b'', b'')
        entries = {}
        for key, settings in listed.items():
            entry = {'issuerName': b'', 'serialNumber': b''}
            if settings != {}:
                entry['trustSettings'] = settings
            entries[key] = entry
        with open(argv[-1], 'wb') as handle:
            plistlib.dump({'trustList': entries, 'trustVersion': 1}, handle)
        return subprocess.CompletedProcess(argv, 0, b'', b'')

    @staticmethod
    def assert_safe(argv, kwargs):
        assert argv[0] == '/usr/bin/security', argv
        assert not kwargs.get('shell'), kwargs
        assert kwargs.get('timeout') == tls.EXPORT_TIMEOUT, kwargs


def empty_client_context():
    """What the packaged app gets on a Mac without the build machine's certificate files."""
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class TlsTestCase(unittest.TestCase):

    def setUp(self):
        tls._ROOTS = None
        self.addCleanup(setattr, tls, '_ROOTS', None)

    def roots(self, fake):
        with mock.patch.object(tls.subprocess, 'run', side_effect=fake):
            return tls._system_roots()


class TrustSettingsEntryTests(unittest.TestCase):

    def test_unconstrained_settings_are_a_trusted_root(self):
        for settings in (None, [], [{'kSecTrustSettingsResult': tls.TRUST_ROOT}],
                         [{'kSecTrustSettingsResult': tls.TRUST_AS_ROOT}], [{}]):
            with self.subTest(settings=settings):
                self.assertIs(tls._entry_trusted(settings), True)

    def test_a_deny_for_any_policy_marks_the_certificate_denied(self):
        for settings in ([{'kSecTrustSettingsResult': tls.DENY}],
                         [{'kSecTrustSettingsPolicyName': 'sMIME', 'kSecTrustSettingsResult': tls.TRUST_ROOT},
                          {'kSecTrustSettingsPolicyName': 'sslServer', 'kSecTrustSettingsResult': tls.DENY}]):
            with self.subTest(settings=settings):
                self.assertIsNone(tls._entry_trusted(settings))

    def test_restricted_or_unspecified_settings_are_not_trusted_here(self):
        for settings in ([{'kSecTrustSettingsPolicyName': 'sslServer', 'kSecTrustSettingsResult': tls.TRUST_ROOT}],
                         [{'kSecTrustSettingsApplication': b'app', 'kSecTrustSettingsResult': tls.TRUST_ROOT}],
                         [{'kSecTrustSettingsResult': 4}], 'not a list', ['not a dict']):
            with self.subTest(settings=settings):
                self.assertIs(tls._entry_trusted(settings), False)


class SystemRootsTests(TlsTestCase):

    def test_only_roots_listed_as_trusted_are_kept(self):
        fake = FakeSecurity()
        self.assertEqual(self.roots(fake), ROOT_PEM)
        commands = [call[0][1:3] for call in fake.calls]
        self.assertIn(['trust-settings-export', '-s'], commands)
        self.assertIn(['trust-settings-export', '-d'], commands)
        self.assertIn(['find-certificate', '-a'], commands)

    def test_admin_keychain_certificate_needs_trust_settings(self):
        keychains = {tls.KEYCHAINS[0]: '', tls.KEYCHAINS[1]: ROOT_PEM + '\n'}
        self.assertEqual(self.roots(FakeSecurity(keychains=keychains)), ROOT_PEM)
        tls._ROOTS = None
        untrusted = {'-s': {'00': {}}, '-d': None, '-u': None}
        self.assertEqual(self.roots(FakeSecurity(keychains=keychains, trust=untrusted)), '')

    def test_admin_or_user_deny_overrides_system_trust(self):
        for domain in ('-d', '-u'):
            tls._ROOTS = None
            trust = {'-s': {fingerprint(ROOT_PEM): {}}, '-d': None, '-u': None}
            trust[domain] = {fingerprint(ROOT_PEM): [{'kSecTrustSettingsResult': tls.DENY}]}
            with self.subTest(domain=domain):
                self.assertEqual(self.roots(FakeSecurity(trust=trust)), '')

    def test_unreadable_system_trust_settings_add_nothing(self):
        trust = {'-s': None, '-d': {fingerprint(ROOT_PEM): {}}, '-u': None}
        fake = FakeSecurity(trust=trust)
        self.assertEqual(self.roots(fake), '')
        self.assertNotIn('find-certificate', [call[0][1] for call in fake.calls])

    def test_security_failures_add_nothing(self):
        for failure in (OSError('no such file'), subprocess.TimeoutExpired('security', 10)):
            tls._ROOTS = None
            with self.subTest(failure=type(failure).__name__):
                self.assertEqual(self.roots(FakeSecurity(fail=failure)), '')

    def test_roots_are_read_once_per_process(self):
        fake = FakeSecurity()
        with mock.patch.object(tls.subprocess, 'run', side_effect=fake):
            tls._system_roots()
            count = len(fake.calls)
            tls._system_roots()
        self.assertEqual(len(fake.calls), count)


class HttpsContextTests(TlsTestCase):

    def assertVerifies(self, context):
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_empty_trust_on_macos_loads_the_trusted_roots(self):
        with mock.patch.object(tls.ssl, 'create_default_context', side_effect=empty_client_context), \
                mock.patch.object(tls.sys, 'platform', 'darwin'), \
                mock.patch.object(tls.subprocess, 'run', side_effect=FakeSecurity()):
            context = tls.https_context()
        self.assertEqual(context.cert_store_stats()['x509_ca'], 1)
        self.assertVerifies(context)

    def test_failed_fallback_still_verifies(self):
        for roots in ('', '-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n-----END CERTIFICATE-----'):
            with self.subTest(roots=roots[:20]), \
                    mock.patch.object(tls.ssl, 'create_default_context', side_effect=empty_client_context), \
                    mock.patch.object(tls.sys, 'platform', 'darwin'), \
                    mock.patch.object(tls, '_system_roots', return_value=roots):
                context = tls.https_context()
            self.assertEqual(context.cert_store_stats()['x509_ca'], 0)
            self.assertVerifies(context)

    def test_default_trust_is_used_as_is(self):
        with mock.patch.object(tls.sys, 'platform', 'darwin'), \
                mock.patch.object(tls, '_trusts_nothing', return_value=False), \
                mock.patch.object(tls, '_system_roots') as roots:
            self.assertVerifies(tls.https_context())
        roots.assert_not_called()

    def test_empty_trust_off_macos_is_left_alone(self):
        with mock.patch.object(tls.ssl, 'create_default_context', side_effect=empty_client_context), \
                mock.patch.object(tls.sys, 'platform', 'linux'), \
                mock.patch.object(tls, '_system_roots') as roots:
            context = tls.https_context()
        roots.assert_not_called()
        self.assertVerifies(context)

    def test_real_context_verifies_certificates_and_host_names(self):
        self.assertVerifies(tls.https_context())


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
