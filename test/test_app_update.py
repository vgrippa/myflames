import http.client
import http.server
import io
import json
import os
from pathlib import Path
import stat
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import patch, MagicMock
from myflames import app_update, tls
from myflames.app_update import (
    parse_version_tuple,
    is_newer_version,
    match_asset_for_platform,
    check_for_updates,
    download_binary_update,
)

from _tls_servers import SilentServer, TrickleTLSServer, anonymous_contexts  # noqa: E402

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
        with patch.dict("os.environ", {"MYFLAMES_TEST_UPDATE": "99.0.0"}), \
                patch.dict("myflames.app_update._CACHE", clear=True):
            res = check_for_updates(force=True)
            self.assertEqual(res["schema_version"], "app-update-1.0")
            self.assertEqual(res["latest_version"], "99.0.0")
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

    def test_regression_download_goes_to_the_named_downloads_folder(self):
        """The desktop backend has an isolated HOME: MYFLAMES_DOWNLOADS_DIR
        names the user's real Downloads folder, and a relative or missing
        path falls back to ~/Downloads."""
        named = Path(self.downloads.parent) / 'Real Downloads'
        named.mkdir()
        with patch.dict(os.environ, {'MYFLAMES_DOWNLOADS_DIR': str(named)}):
            self.response([b'archive', b''])
            result = download_binary_update(self.URL)
        self.assertEqual(result['status'], 'downloaded')
        self.assertEqual(Path(result['download_path']).parent, named)
        for value in ('relative/Downloads', str(named / 'missing')):
            with patch.dict(os.environ, {'MYFLAMES_DOWNLOADS_DIR': value}):
                self.response([b'archive', b''])
                result = download_binary_update(self.URL)
            self.assertEqual(Path(result['download_path']).parent, self.downloads, value)

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

    def test_failed_download_opens_and_returns_release_page_never_asset(self):
        for release in (None, '', app_update.GITHUB_RELEASES_URL + '/tag/v3.1.0'):
            with self.subTest(release=release):
                self.browser.reset_mock()
                self.response([b'partial', OSError('connection lost')])
                result = download_binary_update(self.URL, release_url=release)
                expected = release or app_update.GITHUB_RELEASES_URL + '/latest'
                self.assertEqual(result['status'], 'fallback_browser')
                self.assertEqual(result['html_url'], expected)
                self.browser.assert_called_once_with(expected)
                self.assertEqual(list(self.downloads.iterdir()), [])

    def test_download_cut_at_eof_is_not_published(self):
        response = self.response([])
        with patch.object(app_update, 'Watchdog') as watchdog:
            guard = watchdog.return_value
            guard.expired = False
            chunks = iter([b'partial', b''])

            def read(size):
                chunk = next(chunks)
                if not chunk:
                    guard.expired = True
                return chunk

            response.read.side_effect = read
            result = download_binary_update(self.URL)
            self.assertEqual(result['status'], 'fallback_browser')
            self.assertIn('took too long', result['message'])
            guard.stop.assert_called()
        self.assertEqual(list(self.downloads.iterdir()), [])

    @staticmethod
    def socket_pair():
        class Client(socket.socket):
            def setsockopt(self, level, name, *args):
                # HTTPConnection enables this TCP optimization; an AF_UNIX
                # socketpair carries the same TLS bytes but has no TCP layer.
                if level == socket.IPPROTO_TCP and name == socket.TCP_NODELAY:
                    return
                return super().setsockopt(level, name, *args)

        client, peer = socket.socketpair()
        return Client(fileno=client.detach()), peer

    def test_download_deadline_interrupts_stalled_tls_handshake(self):
        client, peer = self.socket_pair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)

        def connect(*args, **kwargs):
            client.settimeout(3)  # also bounds this regression on the old code
            return client

        with patch('socket.create_connection', connect), \
                patch.object(app_update, 'DOWNLOAD_DEADLINE', 0.5, create=True):
            started = time.monotonic()
            result = download_binary_update(self.URL)
            elapsed = time.monotonic() - started
        self.assertEqual(result['status'], 'fallback_browser')
        self.assertIn('took too long', result['message'])
        self.assertLess(elapsed, 2, 'the handshake outlived the total download deadline')
        self.assertEqual(list(self.downloads.iterdir()), [])

    def trickle_peer(self, context, head, answer):
        """Real TLS over a socketpair, so this test needs no listening port.

        Bytes arrive faster than the per-read timeout, but a whole header
        or 64 KiB body read cannot complete before the overall deadline.
        """
        client, peer = self.socket_pair()
        client.settimeout(3)
        peer.settimeout(3)
        stop = threading.Event()
        sent, disconnected = [], threading.Event()

        def serve():
            wrapped = None
            try:
                wrapped = context.wrap_socket(peer, server_side=True)
                request = b''
                while b'\r\n\r\n' not in request:
                    chunk = wrapped.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                if head:
                    wrapped.sendall(head)
                # The finite peer also bounds this test on the unfixed code.
                for i in range(80):
                    wrapped.sendall(answer[i % len(answer):i % len(answer) + 1])
                    sent.append(i)
                    if stop.wait(0.05):
                        return
            except OSError:
                disconnected.set()
            finally:
                (wrapped or peer).close()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()

        def cleanup():
            stop.set()
            client.close()
            peer.close()
            worker.join(5)
            self.assertFalse(worker.is_alive(), 'the test TLS peer did not stop')

        self.addCleanup(cleanup)
        return client, worker, sent, disconnected

    def test_download_deadline_interrupts_trickled_headers_and_body(self):
        contexts = anonymous_contexts()
        if contexts is None:
            self.skipTest('this OpenSSL has no anonymous TLS cipher')
        server_context, client_context = contexts
        for part, head, answer in (
                ('headers', b'', b'HTTP/1.1 200 OK\r\n' + b'X-Slow: 1\r\n' * 200),
                ('body', b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n', b'x')):
            with self.subTest(part=part):
                client, worker, sent, disconnected = self.trickle_peer(server_context, head, answer)
                with patch('socket.create_connection', return_value=client), \
                        patch.object(app_update, 'https_context', return_value=client_context), \
                        patch.object(app_update, 'DOWNLOAD_DEADLINE', 0.5, create=True):
                    started = time.monotonic()
                    result = download_binary_update(self.URL)
                    elapsed = time.monotonic() - started
                self.assertEqual(result['status'], 'fallback_browser')
                self.assertIn('took too long', result['message'])
                self.assertLess(elapsed, 2, 'a trickled response outlived the total deadline')
                worker.join(2)
                self.assertFalse(worker.is_alive(), 'the server could still send after the deadline')
                self.assertTrue(disconnected.is_set())
                self.assertGreaterEqual(len(sent), 3)
                self.assertEqual(list(self.downloads.iterdir()), [])

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

    def redirecting_transport(self, location, body=b'archive', redirect_body=None):
        """Patch the downloader's opener: self.URL answers 302 to *location*
        (with *redirect_body*, a file object), which answers 200 with *body*.
        Returns the list of visited URLs."""
        visited = []
        first = self.URL
        redirect_body = io.BytesIO(b'') if redirect_body is None else redirect_body

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
                response = urllib.response.addinfourl(redirect_body if code == 302 else io.BytesIO(body),
                                                     headers, url, code)
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

    def test_regression_download_redirect_body_is_closed_unread(self):
        # urllib's own handler reads a redirect's body whole, with no limit.
        reads = []

        class RedirectBody(io.BytesIO):
            def read(self, *args):
                reads.append(args)
                return super().read(*args)
        moved = RedirectBody(b'x' * (4 * 1024 * 1024))
        target = 'https://objects.githubusercontent.com/github-production-release-asset/1/asset'
        visited = self.redirecting_transport(target, redirect_body=moved)
        result = download_binary_update(self.URL)
        self.assertEqual((visited, result['status']), ([self.URL, target], 'downloaded'))
        self.assertEqual(reads, [], 'the redirect body was read')
        self.assertTrue(moved.closed)

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


class TestUpdateCheckOutcome(unittest.TestCase):
    """The Check for updates button: checked_at, plain failure reasons, and
    the record kept in the workspace store (never the network on status)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                     'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_TEST_UPDATE', None)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        cache = patch.dict('myflames.app_update._CACHE', clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        from myflames.workspace_store import WorkspaceStore
        self.workspace = MagicMock()
        self.workspace.store = WorkspaceStore(Path(temp.name) / 'store' / 'workspace.sqlite3')

    @staticmethod
    def opener(response=None, error=None):
        """Patch the check's opener: open() answers *response* or raises *error*."""
        opener = MagicMock()
        if error is not None:
            opener.open.side_effect = error
        else:
            opener.open.return_value = response
        return patch('myflames.app_update.urllib.request.build_opener', return_value=opener)

    def release(self, payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = app_update.GITHUB_RELEASES_API
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode('utf-8')
        response.read.side_effect = [body, b'']
        return self.opener(response)

    def no_network(self):
        return patch('myflames.app_update.urllib.request.build_opener', side_effect=AssertionError('network'))

    def test_successful_check_carries_checked_at(self):
        with self.release({'tag_name': 'v99.1.0', 'name': 'myflames 99.1.0', 'assets': []}):
            result = check_for_updates(force=True)
        self.assertRegex(result['checked_at'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
        self.assertEqual(result['latest_version'], '99.1.0')
        self.assertTrue(result['update_available'])
        self.assertNotIn('error', result)
        # A cached answer keeps the time its metadata was read.
        with self.no_network():
            self.assertEqual(check_for_updates()['checked_at'], result['checked_at'])

    def test_failures_have_a_plain_reason_and_are_never_cached(self):
        def http_error(code):
            return urllib.error.HTTPError(app_update.GITHUB_RELEASES_API, code, 'x', None, io.BytesIO())
        verify = 'a secure connection to GitHub could not be verified'
        cases = [
            (urllib.error.URLError('nodename nor servname provided'), 'GitHub could not be reached'),
            (urllib.error.URLError(socket.timeout('timed out')), 'did not answer in 5 seconds'),
            (socket.timeout('timed out'), 'did not answer in 5 seconds'),
            (http_error(403), 'GitHub limited requests'),
            (http_error(500), 'status 500'),
            # A certificate failure is not "offline", and (a ValueError too)
            # not unreadable release information either.
            (urllib.error.URLError(ssl.SSLCertVerificationError(
                1, 'certificate verify failed: unable to get local issuer certificate')), verify),
            (ssl.SSLCertVerificationError(1, 'certificate verify failed'), verify),
            (urllib.error.URLError(ssl.SSLEOFError(8, 'EOF occurred in violation of protocol')),
             'GitHub could not be reached'),
            (app_update._InvalidUpdateURL('x'), 'GitHub sent the check to an unexpected address'),
            (app_update._CheckDeadline('slow'), 'GitHub took longer than 10 seconds to answer'),
        ]
        for error, reason in cases:
            with self.subTest(error=error), self.opener(error=error):
                result = check_for_updates(force=True)
            self.assertIn(reason, result['reason'])
            self.assertTrue(result['error'].startswith('Could not check for updates: '))
            self.assertFalse(result['update_available'])
            self.assertIn('checked_at', result)
            self.assertNotIn('result', app_update._CACHE)
        for payload in (b'not json', [], {'name': 'no tag'}, {'tag_name': 'nightly'},
                        b' ' * app_update.MAX_RELEASE_BYTES + b'{}'):
            with self.subTest(payload=repr(payload)[:40]), self.release(payload):
                result = check_for_updates(force=True)
            self.assertIn('unreadable', result['reason'])
            self.assertNotIn('result', app_update._CACHE)

    def test_last_check_record_keeps_only_the_outcome(self):
        from myflames import __version__
        when = '2026-10-20T10:42:00Z'
        record = app_update.last_check_record({'checked_at': when, 'latest_version': '99.0.0',
                                               'release_notes': 'long', 'asset_url': 'https://x'})
        self.assertEqual(record, {'checked_at': when, 'outcome': 'available', 'current_version': __version__,
                                  'latest_version': '99.0.0', 'reason': None})
        self.assertEqual(app_update.last_check_record({'checked_at': when, 'latest_version': __version__})['outcome'],
                         'up_to_date')
        failed = app_update.last_check_record({'checked_at': when, 'error': 'Could not', 'reason': 'offline'})
        self.assertEqual((failed['outcome'], failed['reason'], failed['latest_version']), ('failed', 'offline', None))
        for bad in (None, {}, {'checked_at': 'yesterday', 'latest_version': '1.0.0'},
                    {'checked_at': when, 'latest_version': '<b>'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app_update.last_check_record(bad)
        # A record made by another myflames version (before an upgrade) is moot.
        self.assertIsNone(app_update.stored_last_check(dict(record, current_version='0.0.1')))
        self.assertIsNone(app_update.stored_last_check(dict(record, outcome='maybe')))
        for when_bad in ('not a timestamp', '2026-13-45T10:00:00Z', 20261020, None):
            with self.subTest(checked_at=when_bad):
                self.assertIsNone(app_update.stored_last_check(dict(record, checked_at=when_bad)))
        self.assertEqual(app_update.stored_last_check(dict(record, extra='x')), record)

    def test_check_persists_and_status_reads_without_the_network(self):
        from myflames.ui import api_response
        with self.no_network() as net:
            status = api_response('/api/app/update/status', {}, self.workspace)
            net.assert_not_called()
        self.assertEqual(status['schema_version'], 'app-update-status-1.0')
        self.assertIsNone(status['last_check'])
        self.assertFalse(self.workspace.store.path.exists(), 'status never creates the store')
        with patch.dict(os.environ, {'MYFLAMES_TEST_UPDATE': '99.0.0'}):
            checked = api_response('/api/app/update/check', {'force': True}, self.workspace)
        self.assertEqual(checked['last_check']['outcome'], 'available')
        self.assertEqual(checked['last_check']['checked_at'], checked['checked_at'])
        # A new store object on the same file: the record survives a restart.
        from myflames.workspace_store import WorkspaceStore
        self.workspace.store = WorkspaceStore(self.workspace.store.path)
        with self.no_network():
            status = api_response('/api/app/update/status', {}, self.workspace)
        self.assertEqual(status['last_check'], checked['last_check'])
        with self.opener(error=urllib.error.URLError('offline')):
            failed = api_response('/api/app/update/check', {'force': True}, self.workspace)
        self.assertEqual(failed['last_check']['outcome'], 'failed')
        # A certificate failure is stored and shown with its own reason.
        refused = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))
        with self.opener(error=refused):
            unverified = api_response('/api/app/update/check', {'force': True}, self.workspace)
        self.assertEqual((unverified['last_check']['outcome'], unverified['last_check']['reason']),
                         ('failed', 'a secure connection to GitHub could not be verified'))
        with self.no_network():
            status = api_response('/api/app/update/status', {}, self.workspace)
        self.assertEqual(status['last_check'], unverified['last_check'])
        # A store that cannot be written does not hide the check's answer.
        import sqlite3
        with patch.dict(os.environ, {'MYFLAMES_TEST_UPDATE': '99.0.0'}), \
                patch.object(self.workspace.store, 'set_app_update_last_check',
                             side_effect=sqlite3.OperationalError('database is locked')):
            unsaved = api_response('/api/app/update/check', {'force': True}, self.workspace)
        self.assertEqual((unsaved['latest_version'], unsaved['last_check']['outcome']), ('99.0.0', 'available'))
        self.assertEqual(api_response('/api/app/update/status', {}, self.workspace)['last_check']['outcome'], 'failed')
        self.assertIsNone(api_response('/api/app/update/status', {}, None)['last_check'])


class TestUpdateCheckTransport(unittest.TestCase):
    """The check's opener: HTTPS only, no proxy from the environment,
    redirects only within the release API, a total deadline. Nothing leaves
    127.0.0.1: socket.create_connection reaches a local server."""

    API = app_update.GITHUB_RELEASES_API

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                     'MYFLAMES_SECRET_BACKEND': 'vault',
                                     'HTTPS_PROXY': 'http://127.0.0.1:9', 'https_proxy': 'http://127.0.0.1:9'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_TEST_UPDATE', None)
        cache = patch.dict('myflames.app_update._CACHE', clear=True)
        cache.start()
        self.addCleanup(cache.stop)
        worker = patch.object(app_update, '_WORKER', None)
        worker.start()
        self.addCleanup(worker.stop)

    def check_worker(self):
        """The thread of this test's latest check (it outlives a check that ran past its deadline)."""
        self.assertIsNotNone(app_update._WORKER)
        return app_update._WORKER[0]

    def handlers(self, read):
        """The handlers *read* ('check' or 'download') builds its opener from."""
        built = []

        def build(*handlers):
            built.extend(handlers)
            opener = _BUILD_OPENER(*handlers)
            opener.open = MagicMock(side_effect=OSError('offline'))
            return opener
        with patch('myflames.app_update.urllib.request.build_opener', side_effect=build), \
                patch('webbrowser.open'):
            if read == 'check':
                check_for_updates(force=True)
            else:
                with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'MYFLAMES_DOWNLOADS_DIR': folder}):
                    download_binary_update(TestUpdateDownloadRegression.URL)
        return built

    def test_both_reads_ignore_proxies_from_the_environment(self):
        for read in ('check', 'download'):
            with self.subTest(read=read):
                built = self.handlers(read)
                proxies = [h for h in built if isinstance(h, urllib.request.ProxyHandler)]
                self.assertEqual([h.proxies for h in proxies], [{}])
                self.assertEqual(len([h for h in built if isinstance(h, app_update._ReleaseRedirectHandler)]), 1)
        check = self.handlers('check')
        self.assertTrue(any(isinstance(h, tls.WatchedHTTPSHandler) for h in check), 'the check has a deadline')
        redirect = [h for h in check if isinstance(h, app_update._ReleaseRedirectHandler)][0]
        self.assertIs(redirect._validate, app_update._validate_api_url)

    def redirect(self, location, body=b''):
        """The API answers 301 to *location* with *body*, which answers a
        release. Returns (result, visited, the redirect body's reads, whether it was closed)."""
        visited, reads = [], []
        api = self.API
        release = json.dumps({'tag_name': 'v99.2.0', 'assets': []}).encode('utf-8')

        class RedirectBody(io.BytesIO):
            def read(self, *args):
                reads.append(args)
                return super().read(*args)
        moved = RedirectBody(body)

        class Transport(urllib.request.HTTPSHandler, urllib.request.HTTPHandler):
            def https_open(self, request):
                url = request.full_url
                visited.append(url)
                headers = Message()
                if url == api:
                    headers['Location'] = location
                response = urllib.response.addinfourl(moved if url == api else io.BytesIO(release), headers, url,
                                                     301 if url == api else 200)
                response.msg = 'Moved' if url == api else 'OK'
                return response

            http_open = ftp_open = https_open
        with patch('myflames.app_update.urllib.request.build_opener',
                   side_effect=lambda *handlers: _BUILD_OPENER(Transport(), *handlers)):
            return check_for_updates(force=True), visited, reads, moved.closed

    def test_redirects_stay_on_the_release_api(self):
        # GitHub redirects a renamed or transferred repository's API address.
        for target in ('https://api.github.com/repositories/123456/releases/latest',
                       'https://api.github.com/repos/newowner/myflames/releases/latest'):
            with self.subTest(target=target):
                app_update._CACHE.clear()
                result, visited, _, _ = self.redirect(target)
                self.assertEqual(visited, [self.API, target])
                self.assertEqual(result['latest_version'], '99.2.0')
        for target in ('https://evil.example/repos/vgrippa/myflames/releases/latest',
                       'http://api.github.com/repos/vgrippa/myflames/releases/latest',
                       'ftp://api.github.com/repos/vgrippa/myflames/releases/latest',
                       'https://api.github.com:8443/repos/vgrippa/myflames/releases/latest',
                       'https://user@api.github.com/repos/vgrippa/myflames/releases/latest',
                       'https://api.github.com.evil.example/repos/vgrippa/myflames/releases/latest',
                       'https://github.com/vgrippa/myflames/releases/latest',
                       'https://api.github.com/user',
                       'https://api.github.com/repos/vgrippa/releases/latest',
                       'https://api.github.com/repositories/abc/releases/latest',
                       'https://api.github.com/repos/vgrippa/myflames/releases/latest/extra',
                       'https://api.github.com/repos/vgrippa/myflames/releases/../../../user/releases/latest'):
            with self.subTest(target=target):
                result, visited, reads, closed = self.redirect(target)
                self.assertEqual(visited, [self.API])
                self.assertEqual(result['reason'], 'GitHub sent the check to an unexpected address')
                self.assertEqual((reads, closed), ([], True))

    def test_a_redirect_body_is_closed_unread(self):
        # urllib's own handler reads a redirect's body whole, with no limit.
        target = 'https://api.github.com/repositories/123456/releases/latest'
        result, visited, reads, closed = self.redirect(target, body=b'x' * (2 * app_update.MAX_RELEASE_BYTES))
        self.assertEqual((visited, result['latest_version']), ([self.API, target], '99.2.0'))
        self.assertEqual(reads, [], 'the redirect body was read')
        self.assertTrue(closed)

    def test_a_redirect_loop_still_ends(self):
        target = 'https://api.github.com/repositories/1/releases/latest'
        visited = []

        class Transport(urllib.request.HTTPSHandler):
            def https_open(self, request):
                visited.append(request.full_url)
                headers = Message()
                headers['Location'] = target
                response = urllib.response.addinfourl(io.BytesIO(b''), headers, request.full_url, 302)
                response.msg = 'Found'
                return response
        with patch('myflames.app_update.urllib.request.build_opener',
                   side_effect=lambda *handlers: _BUILD_OPENER(Transport(), *handlers)):
            result = check_for_updates(force=True)
        self.assertEqual(result['reason'], 'GitHub answered with status 302')
        handler = app_update._ReleaseRedirectHandler
        self.assertEqual(visited, [self.API] + [target] * handler.max_repeats)

    def deadline_check(self, connect, context=None):
        """Run the check with a 1 s deadline and 30 s socket timeouts, every
        connection made by *connect*: (result, seconds the caller waited)."""
        patches = [patch('socket.create_connection', connect),
                   patch.object(app_update, 'CHECK_DEADLINE', 1.0),
                   patch.object(app_update, 'REQUEST_TIMEOUT', 30.0)]
        if context is not None:
            patches.append(patch.object(app_update, 'https_context', return_value=context))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        # Before the patches stop: a read left running must end first.
        self.addCleanup(lambda: app_update._WORKER and app_update._WORKER[0].join(10))
        started = time.monotonic()
        result = check_for_updates(force=True)
        return result, time.monotonic() - started

    def assertDeadline(self, result, elapsed):
        self.assertEqual(result['reason'], 'GitHub took longer than 1 seconds to answer')
        self.assertLess(elapsed, 2, 'the check answered after its deadline')
        self.assertNotIn('result', app_update._CACHE)

    def test_the_deadline_ends_a_stalled_tls_handshake(self):
        server = SilentServer()
        self.addCleanup(server.close)
        result, elapsed = self.deadline_check(server.connect)
        self.assertDeadline(result, elapsed)
        worker = self.check_worker()
        worker.join(2)
        self.assertFalse(worker.is_alive(), 'the watchdog did not shut the TLS socket at the deadline')

    def test_the_deadline_ends_an_answer_sent_a_byte_at_a_time(self):
        # Every byte resets the 30 s socket timeout. Headers sent a byte at a
        # time stall the open; a body stalls inside one read of 64 KiB, where
        # the check between reads never runs. Only the watchdog ends either.
        contexts = anonymous_contexts()
        if contexts is None:
            self.skipTest('this OpenSSL has no anonymous TLS cipher')
        server_context, client_context = contexts
        for part, head, answer in (
                ('headers', b'', b'HTTP/1.1 200 OK\r\n' + b'X-Slow: 1\r\n' * 200),
                ('body', b'HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n{"tag_name": "v99.0.0", "body": "', b'x')):
            with self.subTest(part=part):
                app_update._WORKER = None
                server = TrickleTLSServer(server_context, answer, head=head)
                self.addCleanup(server.close)
                result, elapsed = self.deadline_check(server.connect, client_context)
                self.assertDeadline(result, elapsed)
                worker = self.check_worker()
                worker.join(2)
                self.assertFalse(worker.is_alive(), 'the read outlived the deadline')
                gone = server.wait(3)
                self.assertIsNotNone(gone, 'the server could still send after the deadline')
                self.assertLess(gone, 3)
                # The client was reading what the server dripped when it went away.
                self.assertGreaterEqual(server.dripped, 5)

    def hung(self):
        """(a stand in for a step that hangs until released, its calls, release)."""
        release, calls = threading.Event(), []

        def step(*args, **kwargs):
            calls.append(args)
            release.wait(30)
            raise OSError('released')
        # Release before the read's worker is joined (cleanups run last first).
        self.addCleanup(lambda: app_update._WORKER and app_update._WORKER[0].join(10))
        self.addCleanup(release.set)
        return step, calls, release

    def test_the_check_answers_by_its_deadline_whatever_hangs(self):
        # A TCP connect (several addresses, 5 s each) and building the TLS
        # context (the macOS roots, read with /usr/bin/security) happen before
        # the watchdog holds a socket; the caller's wait is bounded anyway.
        for step in ('connect', 'context'):
            with self.subTest(step=step):
                app_update._WORKER = None
                hang, calls, release = self.hung()
                target = 'socket.create_connection' if step == 'connect' else 'myflames.app_update.https_context'
                with patch(target, hang), patch.object(app_update, 'CHECK_DEADLINE', 1.0):
                    started = time.monotonic()
                    result = check_for_updates(force=True)
                    self.assertDeadline(result, time.monotonic() - started)
                    # The read still hangs: another check reports the deadline
                    # at once and starts no second read.
                    started = time.monotonic()
                    again = check_for_updates(force=True)
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertEqual(again['reason'], result['reason'])
                    self.assertEqual(len(calls), 1)
                    worker = self.check_worker()
                    release.set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())

    def test_a_check_started_meanwhile_shares_the_running_read(self):
        hang, calls, release = self.hung()
        answers = []
        with patch('socket.create_connection', hang), patch.object(app_update, 'CHECK_DEADLINE', 5.0):
            first = threading.Thread(target=lambda: answers.append(check_for_updates(force=True)))
            first.start()
            for _ in range(200):
                if calls:
                    break
                time.sleep(0.01)
            second = threading.Thread(target=lambda: answers.append(check_for_updates(force=True)))
            second.start()
            time.sleep(0.2)
            release.set()
            first.join(5)
            second.join(5)
        self.assertEqual(len(calls), 1, 'a second read started')
        self.assertEqual([answer['reason'] for answer in answers], ['GitHub could not be reached'] * 2)
        # Once that read ended, the next check reads again.
        with self.opener_error(urllib.error.URLError('offline')) as opened:
            check_for_updates(force=True)
        opened.assert_called_once()

    @staticmethod
    def opener_error(error):
        opener = MagicMock()
        opener.open.side_effect = error
        return patch('myflames.app_update.urllib.request.build_opener', return_value=opener)


if __name__ == "__main__":
    unittest.main()
