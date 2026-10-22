"""myflames.tls: the HTTPS context used by release checks, tool update checks and news."""
import hashlib
import os
import plistlib
import ssl
import subprocess
import sys
import tempfile
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


#: How older macOS reports an admin or user domain without trust settings
#: (exit 1, errSecNoTrustSettings on stderr); macOS 27 exports an empty trustList.
NO_SETTINGS = 'no settings'
#: An export that exits 0 but writes no file, or a file that is not a plist.
NO_FILE, CORRUPT = 'no file', 'corrupt'
#: A keychain whose export fails (not "no certificate").
KEYCHAIN_FAILS = 'fails'


class FakeSecurity:
    """/usr/bin/security: keychain exports and trust settings per domain.

    A trust domain is a dict of fingerprint -> settings ({} is an empty domain,
    as macOS 27 exports it), NO_SETTINGS, NO_FILE, CORRUPT, an exception to
    raise, or None (the export fails with another error). A keychain missing
    from ``keychains`` exits 44 (no certificate); KEYCHAIN_FAILS exits 1.
    ``fail`` raises on every call, ``fail_first`` on the first call only."""

    def __init__(self, keychains=None, trust=None, fail=None, fail_first=None):
        self.keychains = keychains if keychains is not None else {tls.KEYCHAINS[0]: ROOT_PEM + '\n' + OTHER_PEM + '\n'}
        self.trust = trust if trust is not None else {'-s': {fingerprint(ROOT_PEM): {}}, '-d': {}, '-u': {}}
        self.fail = fail
        self.fail_first = fail_first
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.fail is not None:
            raise self.fail
        if self.fail_first is not None and len(self.calls) == 1:
            raise self.fail_first
        self.assert_safe(argv, kwargs)
        if argv[1] == 'find-certificate':
            text = self.keychains.get(argv[-1])
            if text == KEYCHAIN_FAILS:
                return subprocess.CompletedProcess(argv, 1, b'', b'security: SecKeychainOpen: failed\n')
            return subprocess.CompletedProcess(argv, 0 if text is not None else 44, (text or '').encode('ascii'), b'')
        flags = argv[2:-1]
        domain = flags[0] if flags else '-u'
        listed = self.trust.get(domain)
        if isinstance(listed, BaseException):
            raise listed
        if listed is None:
            return subprocess.CompletedProcess(argv, 1, b'', b'security: SecTrustSettingsCreateExternalRepresentation: '
                                                            b'Write permissions error.\n')
        if listed == NO_SETTINGS:
            return subprocess.CompletedProcess(argv, 1, b'', b'SecTrustSettingsCreateExternalRepresentation: '
                                                            b'No Trust Settings were found.\n')
        if listed == NO_FILE:
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        if listed == CORRUPT:
            with open(argv[-1], 'wb') as handle:
                handle.write(b'<?xml version="1.0"?><plist><dict><key>trustList')
            return subprocess.CompletedProcess(argv, 0, b'', b'')
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
        assert kwargs.get('env', {}).get('LC_ALL') == 'C', 'messages are matched in English'


def empty_client_context():
    """What the packaged app gets on a Mac without the build machine's certificate files."""
    return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class TlsTestCase(unittest.TestCase):

    def setUp(self):
        tls._ROOTS = tls._RETRY_AT = None
        self.addCleanup(setattr, tls, '_ROOTS', None)
        self.addCleanup(setattr, tls, '_RETRY_AT', None)

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
        untrusted = {'-s': {'00': {}}, '-d': {}, '-u': {}}
        self.assertEqual(self.roots(FakeSecurity(keychains=keychains, trust=untrusted)), '')

    def test_admin_or_user_deny_overrides_system_trust(self):
        for domain in ('-d', '-u'):
            tls._ROOTS = None
            trust = {'-s': {fingerprint(ROOT_PEM): {}}, '-d': {}, '-u': {}}
            trust[domain] = {fingerprint(ROOT_PEM): [{'kSecTrustSettingsResult': tls.DENY}]}
            with self.subTest(domain=domain):
                self.assertEqual(self.roots(FakeSecurity(trust=trust)), '')

    def test_empty_admin_and_user_domains_keep_the_roots(self):
        # macOS 27 exports an empty trustList (verified with /usr/bin/security
        # on 27.0.1); older macOS exits 1 with "No Trust Settings were found".
        for empty in ({}, NO_SETTINGS):
            tls._ROOTS = None
            with self.subTest(empty=empty):
                trust = {'-s': {fingerprint(ROOT_PEM): {}}, '-d': empty, '-u': empty}
                self.assertEqual(self.roots(FakeSecurity(trust=trust)), ROOT_PEM)

    def test_unreadable_admin_or_user_settings_add_nothing(self):
        # A deny in a domain that could not be read would be lost, so a
        # failure there adds nothing even though the system domain trusts it.
        failures = (None, NO_FILE, CORRUPT, subprocess.TimeoutExpired('security', 10), OSError('gone'))
        for domain in ('-d', '-u'):
            for failure in failures:
                tls._ROOTS = None
                with self.subTest(domain=domain, failure=failure):
                    trust = {'-s': {fingerprint(ROOT_PEM): {}}, '-d': {}, '-u': {}}
                    trust[domain] = failure
                    fake = FakeSecurity(trust=trust)
                    self.assertEqual(self.roots(fake), '')
                    self.assertNotIn('find-certificate', [call[0][1] for call in fake.calls])

    def test_unreadable_system_trust_settings_add_nothing(self):
        for failure in (None, NO_SETTINGS, NO_FILE, CORRUPT):
            tls._ROOTS = None
            with self.subTest(failure=failure):
                trust = {'-s': failure, '-d': {fingerprint(ROOT_PEM): {}}, '-u': {}}
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

    def run_clock(self, fake, steps):
        """_system_roots at each (seconds after the first call) in *steps*:
        [(roots, whether security ran), ...]."""
        now = [1000.0]
        answers = []
        with mock.patch.object(tls.subprocess, 'run', side_effect=fake), \
                mock.patch.object(tls.time, 'monotonic', side_effect=lambda: now[0]):
            for offset in steps:
                now[0] = 1000.0 + offset
                before = len(fake.calls)
                answers.append((tls._system_roots(), len(fake.calls) > before))
        return answers

    def test_a_failed_read_is_tried_again_after_a_minute(self):
        # The first export times out: nothing is added, and nothing is cached
        # for the process. Before RETRY_AFTER no security command runs again;
        # after it, the roots load and are then kept.
        fake = FakeSecurity(fail_first=subprocess.TimeoutExpired('security', tls.EXPORT_TIMEOUT))
        retry = tls.RETRY_AFTER
        self.assertEqual(self.run_clock(fake, (0, retry - 1, retry + 1, retry + 2, 10 * retry)),
                         [('', True), ('', False), (ROOT_PEM, True), (ROOT_PEM, False), (ROOT_PEM, False)])

    def test_a_failed_keychain_export_is_tried_again(self):
        # The other keychain's roots still serve this read; the read is not
        # kept, so the failed keychain is exported again after RETRY_AFTER.
        keychains = {tls.KEYCHAINS[0]: ROOT_PEM + '\n', tls.KEYCHAINS[1]: KEYCHAIN_FAILS}
        fake = FakeSecurity(keychains=keychains)
        retry = tls.RETRY_AFTER
        self.assertEqual(self.run_clock(fake, (0, retry - 1, retry + 1)),
                         [(ROOT_PEM, True), (ROOT_PEM, False), (ROOT_PEM, True)])
        fake.keychains[tls.KEYCHAINS[1]] = ''
        self.assertEqual(self.run_clock(fake, (2 * retry + 2, 10 * retry)), [(ROOT_PEM, True), (ROOT_PEM, False)])


class HttpsContextTests(TlsTestCase):

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in tls.TRUST_STORE_VARIABLES:
            os.environ.pop(name, None)

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

    def test_ssl_cert_file_or_dir_is_the_users_choice(self):
        # OpenSSL loads an SSL_CERT_DIR only on lookup, so its context counts
        # no CA; the macOS roots must still not be added beside it.
        with tempfile.TemporaryDirectory() as folder:
            missing = os.path.join(folder, 'missing.pem')
            for variables in ({'SSL_CERT_DIR': folder}, {'SSL_CERT_FILE': missing}, {'SSL_CERT_FILE': ''},
                              {'SSL_CERT_FILE': missing, 'SSL_CERT_DIR': folder}):
                with self.subTest(variables=variables), mock.patch.dict(os.environ, variables), \
                        mock.patch.object(tls.sys, 'platform', 'darwin'), \
                        mock.patch.object(tls, '_system_roots', return_value=ROOT_PEM) as roots:
                    with mock.patch.object(tls.ssl, 'create_default_context', side_effect=empty_client_context):
                        simulated = tls.https_context()
                    self.assertEqual(simulated.cert_store_stats()['x509_ca'], 0)
                    # The real default context reads the variables itself;
                    # myflames adds nothing to what OpenSSL loaded.
                    real = tls.https_context()
                    self.assertEqual(real.cert_store_stats(), ssl.create_default_context().cert_store_stats())
                    roots.assert_not_called()
                    for context in (simulated, real):
                        self.assertVerifies(context)

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

    def test_release_check_and_download_use_the_context(self):
        marker = ssl.create_default_context()
        for read in ('check', 'download'):
            handlers = []
            real = urllib.request.build_opener

            def build(*args):
                handlers.extend(args)
                opener = real(*args)
                opener.open = mock.Mock(side_effect=OSError('offline'))
                return opener
            with self.subTest(read=read), \
                    mock.patch.object(app_update, 'https_context', return_value=marker), \
                    mock.patch.dict(os.environ, {'MYFLAMES_TEST_UPDATE': ''}), \
                    mock.patch.dict(app_update._CACHE, clear=True), \
                    mock.patch('webbrowser.open'), \
                    mock.patch.object(app_update.urllib.request, 'build_opener', side_effect=build):
                if read == 'check':
                    self.assertIn('error', app_update.check_for_updates(force=True))
                else:
                    with tempfile.TemporaryDirectory() as folder, \
                            mock.patch.dict(os.environ, {'MYFLAMES_DOWNLOADS_DIR': folder}):
                        app_update.download_binary_update(
                            'https://github.com/vgrippa/myflames/releases/download/v9.9.9/myflames-mac-arm64.zip')
            https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
            self.assertEqual(len(https), 1)
            self.assertIs(https[0]._context, marker)

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
