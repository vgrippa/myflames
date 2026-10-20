import http.client
import http.server
import io
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import patch, MagicMock
from myflames.app_update import (
    parse_version_tuple,
    is_newer_version,
    match_asset_for_platform,
    check_for_updates,
    download_binary_update,
)

# The real opener factory, for transports built while another patch is active.
_BUILD_OPENER = urllib.request.build_opener


class TestAppUpdate(unittest.TestCase):
    def test_version_parsing(self):
        self.assertEqual(parse_version_tuple("3.0.0"), (3, 0, 0))
        self.assertEqual(parse_version_tuple("v3.0.1"), (3, 0, 1))
        self.assertEqual(parse_version_tuple("v2.10.5-beta1"), (2, 10, 5))
        self.assertEqual(parse_version_tuple("invalid"), (0, 0, 0))

    def test_version_comparison(self):
        self.assertTrue(is_newer_version("3.0.1", "3.0.0"))
        self.assertTrue(is_newer_version("v3.1.0", "3.0.9"))
        self.assertTrue(is_newer_version("4.0.0", "3.9.9"))
        self.assertFalse(is_newer_version("3.0.0", "3.0.0"))
        self.assertFalse(is_newer_version("2.9.9", "3.0.0"))

    def test_asset_matching(self):
        assets = [
            {"name": "myflames-3.1.0.tar.gz", "browser_download_url": "https://example.com/source.tar.gz"},
            {"name": "myflames-mac-arm64.zip", "browser_download_url": "https://example.com/mac-arm64.zip"},
            {"name": "myflames-win-x64.zip", "browser_download_url": "https://example.com/win-x64.zip"},
        ]
        matched = match_asset_for_platform(assets, "darwin", "arm64")
        self.assertIsNotNone(matched)
        self.assertEqual(matched["name"], "myflames-mac-arm64.zip")

        matched_win = match_asset_for_platform(assets, "win32", "x86_64")
        self.assertIsNotNone(matched_win)
        self.assertEqual(matched_win["name"], "myflames-win-x64.zip")

    def test_check_for_updates_simulated(self):
        with patch.dict("os.environ", {"MYFLAMES_TEST_UPDATE": "3.1.0"}), \
                patch.dict("myflames.app_update._CACHE", clear=True):
            res = check_for_updates(force=True)
            self.assertEqual(res["schema_version"], "app-update-1.0")
            self.assertEqual(res["latest_version"], "3.1.0")
            self.assertTrue(res["update_available"])

    def test_download_binary_update_without_asset(self):
        with patch("webbrowser.open") as mock_open:
            res = download_binary_update(asset_url=None, release_url="https://github.com/vgrippa/myflames/releases")
            self.assertEqual(res["status"], "release_page")
            mock_open.assert_called_once()

    def test_regression_asset_requires_os_and_architecture(self):
        cases = [
            ('darwin', 'arm64', 'linux-arm64.tar.gz', 'mac-arm64.zip'),
            ('darwin', 'arm64', 'mac-x64.zip', 'darwin-aarch64.dmg'),
            ('darwin', 'x86_64', 'mac-arm64.zip', 'macos-amd64.zip'),
            ('linux', 'aarch64', 'linux-x86_64.tar.gz', 'linux-arm64.tar.gz'),
            ('linux', 'amd64', 'linux-arm64.tar.gz', 'linux-x64.tar.gz'),
            ('win32', 'ARM64', 'windows-x64.exe', 'win-arm64.zip'),
            ('win32', 'AMD64', 'darwin-x64.zip', 'windows-x86_64.exe'),
        ]
        for platform, machine, wrong, right in cases:
            with self.subTest(platform=platform, machine=machine, wrong=wrong):
                assets = [{'name': 'myflames-' + wrong}, {'name': 'myflames-' + right}]
                self.assertIs(match_asset_for_platform(assets, platform, machine), assets[1])

    def test_regression_asset_has_no_incompatible_fallback(self):
        for platform, machine, name in [
                ('darwin', 'arm64', 'mac-x64.zip'),
                ('darwin', 'x86_64', 'linux-x64.tar.gz'),
                ('linux', 'arm64', 'linux-amd64.tar.gz'),
                ('win32', 'x64', 'darwin-x64.zip'),
                ('linux', 'riscv64', 'linux-x64.tar.gz'),
                ('freebsd', 'x64', 'linux-x64.tar.gz'),
                ('darwin', 'arm64', 'myflames.zip'),
                ('linux', 'x64', 'linux-universal.tar.gz')]:
            with self.subTest(platform=platform, machine=machine, name=name):
                self.assertIsNone(match_asset_for_platform([{'name': name}], platform, machine))

    def test_regression_mac_universal_and_architecture_aliases(self):
        for os_name in ('mac', 'macos', 'darwin', 'apple'):
            for machine in ('arm64', 'aarch64', 'x86_64', 'amd64', 'x64'):
                with self.subTest(os_name=os_name, machine=machine):
                    wrong = {'name': 'myflames-linux-arm64.tar.gz'}
                    asset = {'name': 'myflames-' + os_name + '-universal.zip'}
                    self.assertIs(match_asset_for_platform([wrong, asset], 'darwin', machine), asset)


class TestUpdateDownloadRegression(unittest.TestCase):
    URL = 'https://github.com/vgrippa/myflames/releases/download/v3.1.0/myflames-mac-arm64.zip'

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.downloads = Path(temp.name) / 'Downloads'
        self.downloads.mkdir()
        env = patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                     'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        browser = patch('webbrowser.open')
        self.browser = browser.start()
        self.addCleanup(browser.stop)
        # No update check ran in these tests unless one sets the cache itself.
        cache = patch.dict('myflames.app_update._CACHE', clear=True)
        cache.start()
        self.addCleanup(cache.stop)

    def response(self, chunks, final_url=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = final_url or self.URL
        response.read.side_effect = chunks
        # Like http.client without a Content-Length: no declared or remaining length.
        response.length = None
        response.headers = Message()
        # Exercise the same fake transport on the original urlopen path and
        # on a downloader with its own redirect-aware opener.
        for target, kw in [('urlopen', {'return_value': response}),
                           ('build_opener', {})]:
            patcher = patch('myflames.app_update.urllib.request.' + target, **kw)
            patched = patcher.start()
            self.addCleanup(patcher.stop)
            if target == 'build_opener':
                patched.return_value.open.return_value = response
        return response

    def test_regression_download_preserves_existing_files_and_symlinks(self):
        original = self.downloads / 'myflames-mac-arm64.zip'
        original.write_bytes(b'original')
        collision = self.downloads / 'myflames-mac-arm64 (1).zip'
        collision.symlink_to(original)
        self.response([b'new archive', b''])
        result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'downloaded')
        self.assertEqual(original.read_bytes(), b'original')
        self.assertTrue(collision.is_symlink())
        destination = Path(result['download_path'])
        self.assertEqual(destination.name, 'myflames-mac-arm64 (2).zip')
        self.assertEqual(destination.read_bytes(), b'new archive')
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(len(list(self.downloads.iterdir())), 3)

    def test_regression_download_failure_removes_partial_without_truncating(self):
        for exists in (False, True):
            with self.subTest(existing=exists):
                destination = self.downloads / 'myflames-mac-arm64.zip'
                if exists:
                    destination.write_bytes(b'original')
                self.response([b'partial', OSError('connection lost')])
                result = download_binary_update(self.URL)
                self.assertEqual(result['status'], 'fallback_browser')
                self.assertEqual(list(self.downloads.iterdir()), [destination] if exists else [])
                if exists:
                    self.assertEqual(destination.read_bytes(), b'original')

    def test_regression_download_size_limit_cleans_partial_file(self):
        import itertools
        self.response(itertools.repeat(b'x' * (64 * 1024), 2401))
        result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'fallback_browser')
        self.assertIn('150 MiB', result['message'])
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_regression_download_refuses_untrusted_urls_before_io(self):
        from myflames.ui import api_response
        invalid = ['file:///etc/passwd', 'http://github.com/vgrippa/myflames/releases/download/v1/a.zip',
                   'https://example.com/a.zip', 'https://github.com/other/myflames/releases/download/v1/a.zip',
                   'https://github.com/vgrippa/myflames-other/releases/download/v1/a.zip',
                   'https://github.com.evil.test/vgrippa/myflames/releases/download/v1/a.zip',
                   'https://user@github.com/vgrippa/myflames/releases/download/v1/a.zip',
                   'https://github.com:444/vgrippa/myflames/releases/download/v1/a.zip',
                   'https://github.com/vgrippa/myflames/releases/download/../a.zip',
                   'https://github.com/vgrippa/myflames/releases/download/v1/%2e%2e',
                   'https://github.com/vgrippa/myflames/releases/download/v1/a.zip\n',
                   {'url': self.URL}]
        response = self.response([b'', b''])
        with patch('myflames.app_update.urllib.request.urlopen', return_value=response) as direct, \
                patch('myflames.app_update.urllib.request.build_opener') as opener:
            for url in invalid:
                for through_api in (False, True):
                    with self.subTest(url=url, through_api=through_api), \
                            self.assertRaisesRegex(ValueError, 'official HTTPS'):
                        if through_api:
                            api_response('/api/app/update/download', {'asset_url': url})
                        else:
                            download_binary_update(url)
            direct.assert_not_called()
            opener.assert_not_called()
        self.browser.assert_not_called()
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_regression_download_rejects_unsafe_final_response_url(self):
        response = self.response([b'unsafe', b''], final_url='https://example.com/asset.zip')
        with self.assertRaisesRegex(ValueError, 'official HTTPS'):
            download_binary_update(self.URL)
        response.read.assert_not_called()
        self.browser.assert_not_called()
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_regression_download_redirect_checked_before_following(self):
        for destination in ('file:///etc/passwd', 'https://example.com/asset.zip',
                            'http://objects.githubusercontent.com/asset.zip',
                            'https://github.com/other/repo/releases/download/v1/a.zip'):
            with self.subTest(destination=destination):
                visited = []

                class Transport(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
                    def https_open(self, request):
                        url = request.full_url
                        visited.append(url)
                        headers = Message()
                        if url == TestUpdateDownloadRegression.URL:
                            headers['Location'] = destination
                        response = urllib.response.addinfourl(io.BytesIO(b'archive'), headers, url,
                                                             302 if url == TestUpdateDownloadRegression.URL else 200)
                        response.msg = 'Found' if response.code == 302 else 'OK'
                        return response

                    http_open = https_open

                build = urllib.request.build_opener
                old_opener = build(Transport())
                with patch('myflames.app_update.urllib.request.urlopen', side_effect=old_opener.open), \
                        patch('myflames.app_update.urllib.request.build_opener',
                              side_effect=lambda *handlers: build(Transport(), *handlers)):
                    with self.assertRaisesRegex(ValueError, 'official HTTPS'):
                        download_binary_update(self.URL)
                self.assertEqual(visited, [self.URL])
        self.browser.assert_not_called()
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_regression_download_asset_host_query_is_not_a_filename(self):
        for host in ('objects.githubusercontent.com', 'release-assets.githubusercontent.com'):
            with self.subTest(host=host):
                url = 'https://' + host + '/release/asset.zip?token=secret'
                self.response([b'archive', b''], final_url=url)
                result = download_binary_update(self.URL)
                self.assertEqual(result['status'], 'downloaded')
                path = Path(result['download_path'])
                self.assertNotIn('?', path.name)
                self.assertNotIn('secret', path.name)
                self.assertEqual(path.name[:len('myflames-mac-arm64')], 'myflames-mac-arm64')
                self.assertEqual(path.read_bytes(), b'archive')

    def redirecting_transport(self, location, body=b'archive'):
        """Patch the downloader's opener: self.URL answers 302 to *location*,
        which answers 200 with *body*. Returns the list of visited URLs."""
        visited = []
        first = self.URL

        class Transport(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
            def https_open(self, request):
                url = request.full_url
                visited.append(url)
                headers = Message()
                if url == first:
                    headers['Location'] = location
                else:
                    headers['Content-Length'] = str(len(body))
                code = 302 if url == first else 200
                response = urllib.response.addinfourl(io.BytesIO(b'' if code == 302 else body), headers, url, code)
                response.msg = 'Found' if code == 302 else 'OK'
                return response

            http_open = https_open

        patcher = patch('myflames.app_update.urllib.request.build_opener',
                        side_effect=lambda *handlers: _BUILD_OPENER(Transport(), *handlers))
        patcher.start()
        self.addCleanup(patcher.stop)
        return visited

    def test_regression_first_url_must_be_the_project_release_path(self):
        """The asset hosts serve every repository's release files, so a client
        URL on them is refused before any I/O; they stay valid as the redirect
        target of the project's own release download URL."""
        from myflames.ui import api_response
        foreign = ['https://objects.githubusercontent.com/github-production-release-asset/1/other.zip',
                   'https://release-assets.githubusercontent.com/github-production-release-asset/1/other.zip',
                   'https://objects.githubusercontent.com/vgrippa/myflames/releases/download/v1/a.zip',
                   'https://release-assets.githubusercontent.com/release/asset.zip?token=secret']
        with patch('myflames.app_update.urllib.request.urlopen') as direct, \
                patch('myflames.app_update.urllib.request.build_opener') as opener:
            for url in foreign:
                for through_api in (False, True):
                    with self.subTest(url=url, through_api=through_api), \
                            self.assertRaisesRegex(ValueError, 'official HTTPS'):
                        if through_api:
                            api_response('/api/app/update/download', {'asset_url': url})
                        else:
                            download_binary_update(url)
            direct.assert_not_called()
            opener.assert_not_called()
        self.browser.assert_not_called()
        self.assertEqual(list(self.downloads.iterdir()), [])
        for host in ('objects.githubusercontent.com', 'release-assets.githubusercontent.com'):
            with self.subTest(redirect=host):
                target = 'https://' + host + '/github-production-release-asset/1/asset?token=secret'
                visited = self.redirecting_transport(target)
                result = download_binary_update(self.URL)
                self.assertEqual(visited, [self.URL, target])
                self.assertEqual(result['status'], 'downloaded')
                path = Path(result['download_path'])
                self.assertEqual(path.read_bytes(), b'archive')
                path.unlink()

    def test_regression_download_must_be_the_checked_release_asset(self):
        """After a check found an asset, another release URL is refused (no
        substitution, no I/O); the checked one downloads."""
        from myflames import app_update
        other = 'https://github.com/vgrippa/myflames/releases/download/v2.0.0/myflames-mac-arm64.zip'
        app_update._CACHE['result'] = {'asset_url': self.URL, 'asset_size': 7}
        app_update._CACHE['timestamp'] = 0
        with patch('myflames.app_update.urllib.request.build_opener') as opener:
            with self.assertRaisesRegex(ValueError, 'last update check'):
                download_binary_update(other)
            opener.assert_not_called()
        self.browser.assert_not_called()
        self.redirecting_transport('https://objects.githubusercontent.com/asset', body=b'archive')
        self.assertEqual(download_binary_update(self.URL)['status'], 'downloaded')

    def serve_loopback(self, declared, body):
        """A 127.0.0.1 HTTP server whose one answer declares *declared* bytes
        and sends *body*, then closes; the downloader's HTTPS requests reach it."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(declared))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        port = server.server_address[1]

        class Loopback(urllib.request.HTTPSHandler):
            def https_open(self, request):
                return self.do_open(lambda host, **kw: http.client.HTTPConnection('127.0.0.1', port, **kw),
                                    request)

        patcher = patch('myflames.app_update.urllib.request.build_opener',
                        side_effect=lambda *handlers: _BUILD_OPENER(Loopback(), *handlers))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_regression_download_ended_early_is_not_published(self):
        """http.client returns b'' at an early EOF although Content-Length
        promised more: the partial file must not reach Downloads."""
        self.serve_loopback(200000, b'x' * 1000)
        result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'fallback_browser')
        self.assertIn('ended early', result['message'])
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_regression_complete_loopback_download_is_published(self):
        self.serve_loopback(1000, b'x' * 1000)
        result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'downloaded')
        self.assertEqual(Path(result['download_path']).read_bytes(), b'x' * 1000)

    def test_regression_download_short_of_checked_asset_size_is_not_published(self):
        """Without a Content-Length (chunked), the size from the update check
        is the only measure of a complete file."""
        from myflames import app_update
        app_update._CACHE['result'] = {'asset_url': self.URL, 'asset_size': 200000}
        app_update._CACHE['timestamp'] = 0
        self.response([b'x' * 1000, b''])
        result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'fallback_browser')
        self.assertIn('ended early', result['message'])
        self.assertEqual(list(self.downloads.iterdir()), [])
        app_update._CACHE['result'] = {'asset_url': self.URL, 'asset_size': 500}
        self.response([b'x' * 1000, b''])
        result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'fallback_browser')
        self.assertIn('does not match', result['message'])
        self.assertEqual(list(self.downloads.iterdir()), [])
        # A complete response whose Content-Length differs from the release
        # asset is a mismatch, not an early end.
        app_update._CACHE['result'] = {'asset_url': self.URL, 'asset_size': 200000}
        self.serve_loopback(1000, b'x' * 1000)
        result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'fallback_browser')
        self.assertIn('does not match', result['message'])
        self.assertEqual(list(self.downloads.iterdir()), [])

    def test_regression_content_length_must_be_ascii_digits(self):
        from myflames.app_update import _content_length
        for value, expected in (('1000', 1000), (' 42 ', 42), ('\u00b2', None), ('1e3', None), ('-1', None),
                                ('', None), (None, None)):
            with self.subTest(value=value):
                headers = Message()
                if value is not None:
                    headers['Content-Length'] = value
                self.assertEqual(_content_length(MagicMock(headers=headers)), expected)

    def test_regression_release_page_refuses_untrusted_url(self):
        with self.assertRaisesRegex(ValueError, 'official HTTPS'):
            download_binary_update(release_url='file:///etc/passwd')
        self.browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
