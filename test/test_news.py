"""Home news (myflames/news.py): fixed feeds, bounded reads, safe parsing, cache.

No test reaches the network: feeds come from test/fixtures/news, and the
HTTPS handler is replaced by one that answers from a table.
"""
import calendar
import http.client
import io
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

from myflames import news, tls
from myflames.workspace_store import WorkspaceStore

from _tls_servers import SilentServer, TrickleTLSServer, anonymous_contexts  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'news'
PLANET, MARIADB = news.SOURCES
REAL_HTTPS = news._HTTPS  # FakeNetwork replaces it
PLANET_URL, MARIADB_URL = PLANET['url'], MARIADB['url']
# 2026-10-20T12:00:00Z, after every fixture date.
T0 = calendar.timegm((2026, 10, 20, 12, 0, 0, 0, 0, 0))


def fixture(name):
    return (FIXTURES / name).read_bytes()


class FakeHTTPS(urllib.request.BaseHandler):
    """Stands in for the HTTPS handler: answers from ``routes`` and records requests."""
    routes = {}
    bodies = {}
    opened = []

    def __init__(self, context=None):
        self.context = context

    def https_open(self, req):
        FakeHTTPS.opened.append((req.full_url, dict(req.header_items())))
        code, headers, body = FakeHTTPS.routes[req.full_url]
        message = http.client.HTTPMessage()
        for key, value in headers.items():
            message[key] = value
        stream = FakeHTTPS.bodies.get(req.full_url) or io.BytesIO(body)
        response = urllib.response.addinfourl(stream, message, req.full_url, code)
        response.msg = http.client.responses.get(code, '')
        return response


class FakeNetwork(unittest.TestCase):
    def setUp(self):
        FakeHTTPS.routes = {}
        FakeHTTPS.bodies = {}
        FakeHTTPS.opened = []
        patcher = mock.patch.object(news, '_HTTPS', lambda watchdog: FakeHTTPS())
        patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, url, body=b'', code=200, headers=None):
        FakeHTTPS.routes[url] = (code, headers or {}, body)


class TestFetch(FakeNetwork):
    def test_sources_are_a_fixed_https_allowlist(self):
        self.assertEqual([s['id'] for s in news.SOURCES], ['planet-mysql', 'mariadb-foundation'])
        for source in news.SOURCES:
            self.assertTrue(source['url'].startswith('https://'))
        self.assertEqual(news.FEED_HOSTS, frozenset(('planet.mysql.com', 'mariadb.org')))
        for url in ('https://example.com/feed', 'http://mariadb.org/feed/', 'https://mariadb.org/feed/?x=1',
                    'file:///etc/passwd', ''):
            with self.subTest(url=url), self.assertRaisesRegex(news.NewsError, 'Unsupported news source'):
                news.fetch_feed(url)
        self.assertEqual(FakeHTTPS.opened, [])

    def test_request_is_neutral(self):
        self.route(MARIADB_URL, fixture('mariadb-foundation.xml'))
        self.assertEqual(news.fetch_feed(MARIADB_URL), fixture('mariadb-foundation.xml'))
        url, headers = FakeHTTPS.opened[0]
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(url, MARIADB_URL)
        self.assertEqual(headers['user-agent'], news.USER_AGENT)
        self.assertRegex(news.USER_AGENT, r'^Python-urllib/\d+\.\d+ myflames/\d+\.\d+\.\d+$')
        self.assertEqual(headers['accept-encoding'], 'identity')
        self.assertNotIn('cookie', headers)
        self.assertNotIn('authorization', headers)

    def test_opener_has_https_only_and_no_cookies(self):
        kinds = [type(handler) for handler in news._opener(None).handlers]
        self.assertIn(FakeHTTPS, kinds)
        for refused in (urllib.request.HTTPHandler, urllib.request.FileHandler, urllib.request.FTPHandler,
                        urllib.request.HTTPCookieProcessor, urllib.request.ProxyHandler, urllib.request.DataHandler):
            self.assertNotIn(refused, kinds)

    def test_redirects_stay_on_feed_hosts(self):
        self.route(MARIADB_URL, code=301, headers={'Location': 'https://mariadb.org/feed/rss2/'})
        self.route('https://mariadb.org/feed/rss2/', fixture('mariadb-foundation.xml'))
        self.assertEqual(news.fetch_feed(MARIADB_URL), fixture('mariadb-foundation.xml'))
        for target in ('https://evil.example/feed/', 'http://mariadb.org/feed/', 'https://user@mariadb.org/feed/',
                       'https://mariadb.org:8443/feed/', 'https://mariadb.org.evil.example/feed/',
                       'https://planet.mysql.com.evil.example/'):
            with self.subTest(target=target):
                FakeHTTPS.opened = []
                self.route(MARIADB_URL, code=302, headers={'Location': target})
                with self.assertRaises((news.NewsError, urllib.error.URLError)):
                    news.fetch_feed(MARIADB_URL)
                self.assertEqual([url for url, _ in FakeHTTPS.opened], [MARIADB_URL])

    def test_redirects_are_counted_bounded_and_never_read(self):
        reads = []

        class Body(io.BytesIO):
            def read(self, n=-1):
                reads.append(n)
                return super().read(n)
            read1 = read
        hops = [PLANET_URL] + ['https://planet.mysql.com/hop-%d' % n for n in range(1, 5)]
        for here, there in zip(hops, hops[1:]):
            self.route(here, code=307, headers={'Location': there})
            FakeHTTPS.bodies[here] = Body(b'z' * (2 * news.MAX_BYTES))
        self.route(hops[3], fixture('planet-mysql.xml'))
        del FakeHTTPS.bodies[hops[3]]
        self.assertEqual(news.fetch_feed(PLANET_URL), fixture('planet-mysql.xml'), 'three redirects are followed')
        self.assertEqual(reads, [], "a redirect's body is never read")
        self.route(hops[3], code=308, headers={'Location': hops[4]})
        FakeHTTPS.bodies[hops[3]] = Body(b'')
        self.route(hops[4], fixture('planet-mysql.xml'))
        FakeHTTPS.opened = []
        with self.assertRaisesRegex(news.NewsError, 'too many times'):
            news.fetch_feed(PLANET_URL)
        self.assertEqual(len(FakeHTTPS.opened), 4)
        # The feed's deadline covers its redirects.
        with self.assertRaises(socket.timeout):
            news.fetch_feed(PLANET_URL, deadline=time.monotonic() - 1)

    def test_response_cap(self):
        # Never more than 1 MiB is read, whatever the body or Content-Length says.
        reads = []
        body = io.BytesIO(b'x' * (3 * news.MAX_BYTES))
        original = body.read
        body.read = body.read1 = lambda n=-1: reads.append(n) or original(n)
        self.route(PLANET_URL, b'', headers={'Content-Length': str(3 * news.MAX_BYTES)})
        FakeHTTPS.bodies = {PLANET_URL: body}
        self.assertEqual(len(news.fetch_feed(PLANET_URL)), news.MAX_BYTES)
        self.assertTrue(all(0 < n <= 65536 for n in reads))
        self.assertEqual(sum(reads), news.MAX_BYTES, 'nothing past the cap is read')
        FakeHTTPS.bodies = {}
        self.route(PLANET_URL, b'x' * news.MAX_BYTES)
        self.assertEqual(len(news.fetch_feed(PLANET_URL)), news.MAX_BYTES)

    def test_compressed_answer_and_http_errors(self):
        self.route(PLANET_URL, b'\x1f\x8b', headers={'Content-Encoding': 'gzip'})
        with self.assertRaisesRegex(news.NewsError, 'encoding'):
            news.fetch_feed(PLANET_URL)
        self.route(PLANET_URL, b'<html>Forbidden</html>', code=403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            news.fetch_feed(PLANET_URL)
        self.assertEqual(news._failure(caught.exception), ('The news source answered HTTP 403.', False))

    def test_total_deadline(self):
        self.route(PLANET_URL, fixture('planet-mysql.xml'))
        with self.assertRaises(socket.timeout) as caught:
            news.fetch_feed(PLANET_URL, deadline=time.monotonic() - 1)
        self.assertEqual(news._failure(caught.exception), (news.OFFLINE_MESSAGE, True))

    def test_the_deadline_cuts_whatever_is_still_reading(self):
        # A server that never finishes: the watchdog ends the read at the
        # deadline, whatever phase it is in, and reports a timeout.
        def stalled(req):
            time.sleep(0.4)
            raise http.client.RemoteDisconnected('closed')
        with mock.patch.object(FakeHTTPS, 'https_open', lambda self, req: stalled(req)):
            started = time.monotonic()
            with self.assertRaises(socket.timeout) as caught:
                news.fetch_feed(PLANET_URL, deadline=time.monotonic() + 0.1)
            self.assertEqual(news._failure(caught.exception), (news.OFFLINE_MESSAGE, True))
            self.assertLess(time.monotonic() - started, 2)

    def test_watchdog_shuts_the_socket_down_at_the_deadline(self):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        created = []

        def create_connection(*args, **kwargs):
            created.append(args)
            return left
        watchdog = news._Watchdog(time.monotonic() + 0.2)
        self.addCleanup(watchdog.stop)
        with mock.patch('socket.create_connection', create_connection):
            connection = news._connection_class(watchdog)('planet.mysql.com', 443, timeout=5,
                                                          context=ssl.create_default_context())
            self.assertIs(connection._create_connection(('planet.mysql.com', 443), 5), left)
        left.settimeout(5)
        started = time.monotonic()
        self.assertEqual(left.recv(1), b'', 'a blocked read ends at the deadline')
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(watchdog.expired)
        # A socket opened after the deadline is shut down at once.
        late_left, late_right = socket.socketpair()
        self.addCleanup(late_left.close)
        self.addCleanup(late_right.close)
        watchdog.watch(late_left)
        late_left.settimeout(5)
        self.assertEqual(late_left.recv(1), b'')

    def test_the_real_https_handler_watches_its_connections(self):
        handler = REAL_HTTPS(news._Watchdog(time.monotonic() + 60))
        self.addCleanup(handler._watchdog.stop)
        self.assertTrue(handler._context.check_hostname)
        self.assertEqual(handler._context.verify_mode, ssl.CERT_REQUIRED)
        with mock.patch.object(handler, 'do_open', return_value='response') as do_open:
            self.assertEqual(handler.https_open(urllib.request.Request(PLANET_URL)), 'response')
        self.assertTrue(issubclass(do_open.call_args[0][0], http.client.HTTPSConnection))

    def test_failure_kinds(self):
        self.assertEqual(news._failure(urllib.error.URLError(socket.gaierror(8, 'nodename'))), (news.OFFLINE_MESSAGE, True))
        self.assertEqual(news._failure(ConnectionRefusedError()), (news.OFFLINE_MESSAGE, True))
        self.assertFalse(news._failure(urllib.error.URLError(ssl.SSLError(1, 'certificate verify failed')))[1])
        self.assertFalse(news._failure(news.NewsError('The feed is not valid XML.'))[1])
        self.assertFalse(news._failure(http.client.IncompleteRead(b''))[1])


class TestTlsDeadline(unittest.TestCase):
    """The feed's deadline closes the TLS socket, not only the TCP socket it
    wraps (wrapping detaches that one). Real handler, real TLS socket; nothing
    leaves 127.0.0.1 because socket.create_connection reaches a local server."""

    def fetch(self, server, context=None):
        """fetch_feed against *server*, 0.5 s deadline, 30 s socket timeouts:
        (exception, seconds, sockets the watchdog held)."""
        self.addCleanup(server.close)
        watched = []

        class Recording(tls.Watchdog):
            def watch(self, sock):
                watched.append(sock)
                super().watch(sock)
        patches = [mock.patch('socket.create_connection', server.connect),
                   mock.patch.object(news, '_Watchdog', Recording),
                   mock.patch.object(news, 'SOCKET_TIMEOUT', 30.0)]
        if context is not None:
            patches.append(mock.patch.object(news, 'https_context', return_value=context))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        started = time.monotonic()
        with self.assertRaises(socket.timeout) as caught:
            news.fetch_feed(PLANET_URL, deadline=time.monotonic() + 0.5)
        return caught.exception, time.monotonic() - started, watched

    def test_the_deadline_ends_a_stalled_tls_handshake(self):
        error, elapsed, watched = self.fetch(SilentServer())
        self.assertLess(elapsed, 3, 'the TLS handshake outlived the deadline')
        self.assertTrue(any(isinstance(sock, ssl.SSLSocket) for sock in watched), watched)
        self.assertEqual(news._failure(error), (news.OFFLINE_MESSAGE, True))

    def test_the_deadline_closes_a_tls_read_of_headers_sent_a_byte_at_a_time(self):
        # Every byte resets the 30 s socket timeout, and the deadline check
        # between body reads never runs: only the watchdog can end this.
        contexts = anonymous_contexts()
        if contexts is None:
            self.skipTest('this OpenSSL has no anonymous TLS cipher')
        server_context, client_context = contexts
        server = TrickleTLSServer(server_context, b'HTTP/1.1 200 OK\r\n' + b'X-Slow: 1\r\n' * 200)
        _, elapsed, watched = self.fetch(server, client_context)
        self.assertLess(elapsed, 3)
        self.assertTrue(any(isinstance(sock, ssl.SSLSocket) for sock in watched), watched)
        gone = server.wait(3)
        self.assertIsNotNone(gone, 'the server could still send after the deadline')
        self.assertLess(gone, 3)


class TestParse(unittest.TestCase):
    def test_rss_keeps_official_https_links_only(self):
        items = news.parse_feed(fixture('planet-mysql.xml'), PLANET, T0)
        self.assertEqual([item['link'] for item in items], [
            'https://blogs.oracle.com/mysql/troubleshooting-mysql-with-opentelemetry-metrics',
            'https://blogs.oracle.com/mysql/upgrading-mysql-heatwave-high-availability',
            'https://blogs.oracle.com/mysql/visualizing-mysql-telemetry-with-grafana',
            'https://blogs.oracle.com/mysql/mysql-select-star-best-practices',
        ])
        first = items[0]
        self.assertEqual(first['title'], 'Troubleshooting MySQL with OpenTelemetry Metrics')
        self.assertEqual(first['summary'], 'In the first post, we exported native MySQL telemetry.')
        self.assertEqual(first['published'], '2026-10-19T23:48:23Z')
        self.assertEqual((first['source'], first['source_id'], first['engine']), ('Planet MySQL', 'planet-mysql', 'mysql'))
        self.assertEqual(set(first), {'title', 'link', 'source', 'source_id', 'engine', 'published', 'summary'})
        # -0700 to UTC, dc:date as a fallback, an unreadable date kept as None (sorted last).
        self.assertEqual([item['published'] for item in items[1:]],
                         ['2026-10-19T19:50:31Z', '2026-10-15T04:10:08Z', None])
        self.assertEqual(items[1]['summary'], 'Planning, compatibility testing & validation.')
        self.assertEqual(items[3]['title'], 'Just Because You Can SELECT Everything Doesn\N{RIGHT SINGLE QUOTATION MARK}t Mean You Should')

    def test_rss_from_mariadb_foundation(self):
        items = news.parse_feed(fixture('mariadb-foundation.xml'), MARIADB, T0)
        self.assertEqual([item['title'] for item in items], [
            'Amazic joins MariaDB Foundation as a Silver Sponsor',
            'MariaDB Foundation: Exploring TidesDB Performance with TAF',
            'MariaDB Foundation Newsletter \N{EN DASH} 15 October 2026',
            'Community Spotlight: SQLtest.online',
        ])
        self.assertEqual(items[0]['summary'],
                         'MariaDB Foundation is pleased to welcome Amazic as a Silver Sponsor\N{HORIZONTAL ELLIPSIS} Continue reading')
        self.assertEqual(items[1]['summary'], 'TidesDB now exists inside MariaDB as a pluggable storage engine.')
        self.assertEqual(items[3]['published'], '2026-10-12T18:16:35Z')
        self.assertTrue(all(item['engine'] == 'mariadb' for item in items))

    def test_atom_dates_links_and_titles(self):
        items = news.parse_feed(fixture('atom.xml'), MARIADB, T0)
        self.assertEqual([(item['title'], item['link'], item['published']) for item in items], [
            ('MariaDB 12.1 released', 'https://mariadb.org/mariadb-12-1-released/', '2026-10-18T08:00:00Z'),
            ('A relative link', 'https://mariadb.org/relative-post/', '2026-10-17T09:30:00Z'),
            ('Galera 4 notes', 'https://mariadb.org/galera-4-notes/', '2026-10-16T13:30:00Z'),
        ])
        self.assertEqual(items[0]['summary'], 'The release notes are here.')
        self.assertEqual(items[1]['summary'], 'Body')

    def test_doctype_and_entity_are_refused_before_parsing(self):
        with mock.patch.object(news.ET, 'XMLPullParser', side_effect=AssertionError('parsed')):
            for body in (fixture('doctype.xml'), b'<?xml version="1.0"?><!ENTITY x "y"><rss/>',
                         b'<?xml version="1.0"?>\n<!doctype rss><rss/>', b'<rss><!-- --><! DOCTYPE x></rss>'):
                with self.subTest(body=body[:40]), self.assertRaisesRegex(news.NewsError, 'document type'):
                    news.parse_feed(body, MARIADB, T0)

    def test_only_ascii_compatible_encodings_are_read(self):
        doc = ('<?xml version="1.0" encoding="%s"?><!DOCTYPE rss [<!ENTITY a "AAAA">]><rss><channel><item>'
               '<title>&a;</title><link>https://mariadb.org/x/</link></item></channel></rss>')
        with mock.patch.object(news.ET, 'XMLPullParser', side_effect=AssertionError('parsed')):
            for encoding, codec in (('UTF-16', 'utf-16'), ('UTF-16LE', 'utf-16-le'), ('UTF-16BE', 'utf-16-be'),
                                    ('UTF-32', 'utf-32'), ('cp037', 'cp037')):
                with self.subTest(encoding=encoding), self.assertRaisesRegex(news.NewsError, 'text encoding|not valid XML'):
                    news.parse_feed((doc % encoding).encode(codec), MARIADB, T0)
            for body in (b'<?xml version="1.0" encoding="x-unknown"?><rss/>', b'<rss>\x00</rss>'):
                with self.subTest(body=body), self.assertRaisesRegex(news.NewsError, 'text encoding'):
                    news.parse_feed(body, MARIADB, T0)
        latin = ('<?xml version="1.0" encoding="ISO-8859-1"?><rss><channel><item><title>Caf\xe9</title>'
                 '<link>https://mariadb.org/cafe/</link></item></channel></rss>').encode('latin-1')
        self.assertEqual(news.parse_feed(latin, MARIADB, T0)[0]['title'], 'Caf\xe9')
        bom = b'\xef\xbb\xbf' + fixture('mariadb-foundation.xml')
        self.assertEqual(len(news.parse_feed(bom, MARIADB, T0)), 4)

    def test_other_documents_are_refused(self):
        for body, message in ((b'<html><body>Captive portal</body></html>', 'not an RSS or Atom feed'),
                              (b'<rss><channel>', 'not valid XML'), (b'', 'not valid XML'),
                              (b'<rss><channel><item><title>x</title><link>https://mariadb.org/x/</link>', 'not valid XML'),
                              (b'x' * (news.MAX_BYTES + 1), 'not valid XML')):
            with self.subTest(body=body[:20]), self.assertRaisesRegex(news.NewsError, message):
                news.parse_feed(body, MARIADB, T0)

    def test_a_feed_longer_than_the_cap_keeps_its_first_entries(self):
        def item(n, size):
            return ('<item><title>Post %d</title><link>https://mariadb.org/post-%d/</link>'
                    '<pubDate>Mon, %02d Oct 2026 10:00:00 +0000</pubDate><description>%s</description></item>'
                    % (n, n, 20 - n, 'y' * size)).encode()
        head = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Long</title>'
        body = head + item(1, 100) + item(2, 100) + item(3, 2 * news.MAX_BYTES) + b'</channel></rss>'
        items = news.parse_feed(body, MARIADB, T0)
        self.assertEqual([i['title'] for i in items], ['Post 1', 'Post 2'])
        many = head + b''.join(item(n % 19 + 1, 30000) for n in range(60)) + b'</channel></rss>'
        self.assertGreater(len(many), news.MAX_BYTES)
        before_cut = many[:news.MAX_BYTES].count(b'</item>')
        self.assertLess(before_cut, 60)
        self.assertEqual(len(news.parse_feed(many, MARIADB, T0)), min(before_cut, news.MAX_PARSED))
        # A complete document with a broken tail keeps what ended before it.
        broken = head + item(1, 10) + b'<item><title>bad</ti></item></channel></rss>'
        self.assertEqual([i['title'] for i in news.parse_feed(broken, MARIADB, T0)], ['Post 1'])

    def test_plain_text(self):
        text = 'a' + chr(0x202e) + 'b' + chr(0x200b) + 'c\x07 <script>evil()</script><b>bold</b>&amp;&lt;x&gt;\n\t done'
        self.assertEqual(news.plain_text(text, 200), 'abc bold&<x> done')
        self.assertEqual(news.plain_text('<p>one</p><p>two</p><br/>three', 200), 'one two three')
        self.assertEqual(news.plain_text(None, 10), '')
        long = news.plain_text('word ' * 100, 50)
        self.assertLessEqual(len(long), 50)
        self.assertTrue(long.endswith('\N{HORIZONTAL ELLIPSIS}'))
        self.assertEqual(len(news.plain_text('x' * 500, news.TITLE_MAX)), news.TITLE_MAX)

    def test_parse_date(self):
        cases = {
            'Mon, 19 Oct 2026 23:48:23 +0000': '2026-10-19T23:48:23Z',
            'Mon, 19 Oct 2026 23:48:23 GMT': '2026-10-19T23:48:23Z',
            'Mon, 19 Oct 2026 16:48:23 -0700': '2026-10-19T23:48:23Z',
            '19 Oct 2026 23:48:23 -0000': '2026-10-19T23:48:23Z',
            '2026-10-19T23:48:23Z': '2026-10-19T23:48:23Z',
            '2026-10-20T01:48:23+02:00': '2026-10-19T23:48:23Z',
            '2026-10-19T23:48:23.5-00:00': '2026-10-19T23:48:23Z',
            '2026-10-19': '2026-10-19T00:00:00Z',
            '2026-10-19T23:48': '2026-10-19T23:48:00Z',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(news.parse_date(value, T0), expected)
        for value in ('', '   ', None, 'yesterday', '2026-13-40T00:00:00Z', 'Mon, 99 Foo 2026', '1990-01-01T00:00:00Z',
                      '2026-10-19T10:00:00+99:99', '2026-10-19T10:00:00-24:00', '2026-10-19T10:00:00+05:60',
                      'Mon, 19 Oct 2026 10:00:00 +9999',
                      '2027-01-01T00:00:00Z', 'x' * 100, 42):
            with self.subTest(value=value):
                self.assertIsNone(news.parse_date(value, T0))

    def test_headline_links(self):
        good = 'https://blogs.oracle.com/mysql/post?a=1#b'
        self.assertEqual(news.headline_link(PLANET, good), good)
        self.assertEqual(news.headline_link(PLANET, ' https://BLOGS.oracle.com:443/mysql/x '), 'https://blogs.oracle.com/mysql/x')
        for value in ('http://blogs.oracle.com/mysql/x', 'https://blogs.oracle.com/cloud/x', 'https://evil.example/mysql/x',
                      'https://blogs.oracle.com.evil.example/mysql/x', 'https://u:p@blogs.oracle.com/mysql/x',
                      'https://blogs.oracle.com:444/mysql/x', 'javascript:alert(1)', 'data:text/html,x',
                      'https://blogs.oracle.com/mysql/a b', 'https://blogs.oracle.com/mysql/\x00', '//evil.example/mysql/',
                      'https://blogs.oracle.com/mysql/' + 'x' * news.LINK_MAX, '', None, 7):
            with self.subTest(value=value):
                self.assertIsNone(news.headline_link(PLANET, value))
        self.assertIsNone(news.headline_link(MARIADB, 'https://mariadb.com/x'))
        self.assertEqual(news.headline_link(MARIADB, '/x/'), 'https://mariadb.org/x/')


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Reader:
    """A feed reader that answers from fixtures, or raises *fail*."""

    def __init__(self, fail=None, delay=0):
        self.calls = []
        self.fail = fail or {}
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, url, deadline):
        with self.lock:
            self.calls.append(url)
        if self.delay:
            time.sleep(self.delay)
        if url in self.fail:
            raise self.fail[url]
        return fixture('planet-mysql.xml' if url == PLANET_URL else 'mariadb-foundation.xml')


class TestCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'news-cache.json'
        self.clock = Clock()

    def read(self, reader, refresh=False, wait=5):
        return news.read_news(self.path, reader, refresh=refresh, wait=wait, clock=self.clock)

    def test_first_read_fills_a_private_cache_and_balances_sources(self):
        reader = Reader()
        result = self.read(reader)
        self.assertEqual(sorted(reader.calls), sorted([PLANET_URL, MARIADB_URL]))
        self.assertEqual(result['schema_version'], 'news-1.0')
        self.assertTrue(result['enabled'])
        self.assertFalse(result['stale'])
        self.assertNotIn('error', result)
        self.assertEqual(result['fetched_at'], '2026-10-20T12:00:00Z')
        self.assertEqual(len(result['items']), 6)
        self.assertEqual([item['engine'] for item in result['items']].count('mysql'), 3)
        dates = [item['published'] or '' for item in result['items']]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertEqual([(s['id'], s['items']) for s in result['sources']], [('planet-mysql', 4), ('mariadb-foundation', 4)])
        if os.name == 'posix':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))['schema_version'], 'news-cache-1.0')

    def test_fresh_cache_is_served_without_reading(self):
        self.read(Reader())
        for hours in (1, 5.9):
            self.clock.now = T0 + hours * 3600
            reader = Reader()
            result = self.read(reader)
            self.assertEqual(reader.calls, [])
            self.assertEqual(result['fetched_at'], '2026-10-20T12:00:00Z')
            self.assertEqual(len(result['items']), 6)
        self.clock.now = T0 + 6 * 3600
        reader = Reader()
        self.assertEqual(self.read(reader)['fetched_at'], '2026-10-20T18:00:00Z')
        self.assertEqual(len(reader.calls), 2)

    def test_offline_serves_the_cache_with_its_age(self):
        self.read(Reader())
        self.clock.now = T0 + 7 * 3600
        offline = urllib.error.URLError(socket.gaierror(8, 'nodename nor servname provided'))
        result = self.read(Reader(fail={PLANET_URL: offline, MARIADB_URL: offline}))
        self.assertTrue(result['stale'])
        self.assertTrue(result['offline'])
        self.assertEqual(result['error'], 'News is unavailable offline.')
        self.assertEqual(result['fetched_at'], '2026-10-20T12:00:00Z')
        self.assertEqual(len(result['items']), 6)
        # A failed read is tried again after ten minutes, not on every Home visit.
        self.clock.now += 5 * 60
        reader = Reader()
        self.assertTrue(self.read(reader)['stale'])
        self.assertEqual(reader.calls, [])
        self.clock.now += 6 * 60
        result = self.read(reader)
        self.assertEqual(len(reader.calls), 2)
        self.assertFalse(result['stale'])

    def test_offline_without_a_cache(self):
        offline = socket.timeout('timed out')
        result = self.read(Reader(fail={PLANET_URL: offline, MARIADB_URL: offline}))
        self.assertEqual((result['items'], result['fetched_at'], result['stale'], result['error']),
                         ([], None, True, 'News is unavailable offline.'))

    def test_one_source_failing(self):
        forbidden = urllib.error.HTTPError(PLANET_URL, 403, 'Forbidden', {}, io.BytesIO(b''))
        self.addCleanup(forbidden.close)
        result = self.read(Reader(fail={PLANET_URL: forbidden}))
        self.assertEqual(result['error'], 'Planet MySQL could not be read.')
        self.assertFalse(result['offline'])
        self.assertTrue(result['stale'])
        self.assertEqual({item['engine'] for item in result['items']}, {'mariadb'})
        self.assertEqual(result['sources'][0]['error'], 'The news source answered HTTP 403.')
        broken = news.NewsError('The feed is not valid XML.')
        self.clock.now += 7 * 3600  # both sources are due again
        result = self.read(Reader(fail={PLANET_URL: broken, MARIADB_URL: broken}))
        self.assertEqual(result['error'], 'News could not be read.')

    def test_refresh_reads_again_at_most_once_a_minute(self):
        self.read(Reader())
        self.clock.now = T0 + 30
        reader = Reader()
        self.read(reader, refresh=True)
        self.assertEqual(reader.calls, [])
        self.clock.now = T0 + 120
        self.read(reader, refresh=True)
        self.assertEqual(len(reader.calls), 2)

    def test_a_read_in_progress_is_shared(self):
        reader = Reader(delay=0.3)
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.read(reader))) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(reader.calls), sorted([PLANET_URL, MARIADB_URL]))
        self.assertEqual([len(r['items']) for r in results], [6, 6, 6, 6])

    def test_the_wait_is_bounded(self):
        release = threading.Event()

        def slow(url, deadline):
            release.wait(5)
            return fixture('mariadb-foundation.xml')
        started = time.monotonic()
        result = self.read(slow, wait=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result['error'], 'News is taking longer than usual.')
        self.assertEqual(result['items'], [])
        release.set()
        for _ in range(100):
            if str(self.path) not in news._INFLIGHT:
                break
            time.sleep(0.05)
        self.assertNotIn(str(self.path), news._INFLIGHT)

    def test_without_a_reader_nothing_is_read_or_written(self):
        result = self.read(None)
        self.assertEqual((result['items'], result['stale'], result['error']), ([], True, 'News is unavailable offline.'))
        self.assertFalse(self.path.exists())

    def test_cache_is_checked_again_when_loaded(self):
        self.read(Reader())
        value = json.loads(self.path.read_text(encoding="utf-8"))
        entry = value['sources']['planet-mysql']
        entry['items'] = [dict(entry['items'][0], link='javascript:alert(1)'),
                          dict(entry['items'][0], link='http://blogs.oracle.com/mysql/x'),
                          dict(entry['items'][0], title='<b>Safe</b> title', link='https://blogs.oracle.com/mysql/ok'),
                          'not an item']
        entry['checked_at'] = 'garbage'
        self.path.write_text(json.dumps(value))
        cache = news.load_cache(self.path, T0)
        self.assertEqual([(i['title'], i['link']) for i in cache['sources']['planet-mysql']['items']],
                         [('Safe title', 'https://blogs.oracle.com/mysql/ok')])
        self.assertIsNone(cache['sources']['planet-mysql']['checked_at'])
        for text in ('not json', json.dumps({'schema_version': 'other'}), json.dumps([1])):
            self.path.write_text(text)
            self.assertEqual(news.load_cache(self.path, T0)['sources'], {})

    def test_cache_fields_of_any_type(self):
        self.path.write_text(json.dumps({'schema_version': 'news-cache-1.0', 'sources': {'mariadb-foundation': {
            'checked_at': 7, 'fetched_at': [], 'error': {}, 'items': [
                {'title': 'Kept', 'link': 'https://mariadb.org/kept/', 'summary': 7, 'published': 5},
                {'title': ['x'], 'link': {'a': 1}}]}, 'planet-mysql': 'nonsense'}}))
        cache = news.load_cache(self.path, T0)
        self.assertEqual([(i['title'], i['summary'], i['published']) for i in cache['sources']['mariadb-foundation']['items']],
                         [('Kept', '', None)])
        self.assertIsNone(cache['sources']['mariadb-foundation']['error'])

    def test_a_failed_cache_write_keeps_the_read_in_memory(self):
        reader = Reader()
        with mock.patch.object(news, '_write_cache', side_effect=OSError('read-only disk')):
            result = self.read(reader)
            self.assertEqual((len(result['items']), result['stale']), (6, False))
            self.clock.now += 3600
            self.assertEqual(len(self.read(reader)['items']), 6)
        self.assertEqual(len(reader.calls), 2, 'no second read within six hours')
        self.assertFalse(self.path.exists())

    def test_a_read_that_finished_meanwhile_is_not_repeated(self):
        self.read(Reader())
        fresh = news.load_cache(self.path, T0)
        stale = news._empty_cache()
        reader = Reader()
        # The first snapshot predates a read that another request finished.
        with mock.patch.object(news, '_snapshot', side_effect=[stale, fresh, fresh, fresh]):
            result = self.read(reader)
        self.assertEqual(reader.calls, [])
        self.assertEqual(len(result['items']), 6)

    def test_a_stuck_read_is_never_doubled(self):
        release = threading.Event()
        calls = []

        def stuck(url, deadline):
            calls.append(url)
            release.wait(10)
            raise socket.timeout('timed out')
        self.addCleanup(release.set)
        with mock.patch.object(news, 'FEED_DEADLINE', 0.05), mock.patch.object(news, 'SOCKET_TIMEOUT', 0.05):
            result = self.read(stuck, wait=2)
            self.assertEqual(result['error'], 'News is unavailable offline.')
            self.assertEqual(len(calls), 2)
            for _ in range(3):
                self.clock.now += news.RETRY_AFTER + 1
                self.assertTrue(self.read(stuck, refresh=True, wait=2)['stale'])
            self.assertEqual(len(calls), 2, 'still stuck: no second worker per source')
            release.set()
            for worker in list(news._WORKERS.values()):
                worker.join(5)
            self.clock.now += news.RETRY_AFTER + 1
            self.read(Reader(), wait=2)
        self.assertEqual(len(calls), 2)

    def test_fetched_at_belongs_to_the_headlines_shown(self):
        planet = news.parse_feed(fixture('planet-mysql.xml'), PLANET, T0)
        copy = dict(planet[0], source='MariaDB Foundation', source_id='mariadb-foundation', engine='mariadb')
        cache = {'schema_version': 'news-cache-1.0', 'sources': {
            'planet-mysql': {'fetched_at': '2026-10-20T12:00:00Z', 'checked_at': '2026-10-20T12:00:00Z', 'items': planet},
            'mariadb-foundation': {'fetched_at': '2026-10-17T12:00:00Z', 'checked_at': '2026-10-20T12:00:00Z',
                                   'items': [copy]}}}
        result = news.report(cache)
        self.assertEqual({i['source_id'] for i in result['items']}, {'planet-mysql'})
        self.assertEqual(result['fetched_at'], '2026-10-20T12:00:00Z')

    @unittest.skipUnless(hasattr(os, 'symlink') and os.name == 'posix', 'POSIX symbolic links')
    def test_a_symbolic_link_is_not_read(self):
        target = Path(self.temp.name) / 'elsewhere.json'
        target.write_text(json.dumps({'schema_version': 'news-cache-1.0', 'sources': {'mariadb-foundation': {
            'items': [{'title': 'Planted', 'link': 'https://mariadb.org/planted/'}]}}}))
        os.symlink(str(target), str(self.path))
        self.assertEqual(news.load_cache(self.path, T0)['sources'], {})


class TestSettingAndEndpoint(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = WorkspaceStore(Path(self.temp.name) / 'workspace.sqlite3')

    def test_setting_defaults_on_and_persists(self):
        self.assertTrue(self.store.news_enabled())
        self.assertFalse(self.store.path.exists(), 'reading the default creates no database')
        self.assertEqual(news.api_response('/api/news/settings', {}, self.store), {'enabled': True})
        self.assertEqual(news.api_response('/api/news/settings', {'enabled': False}, self.store), {'enabled': False})
        self.assertFalse(WorkspaceStore(self.store.path).news_enabled())
        for value in ('false', 0, None, 'off'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'true or false'):
                news.api_response('/api/news/settings', {'enabled': value}, self.store)
        self.assertTrue(news.api_response('/api/news/settings', {'enabled': True}, self.store)['enabled'])

    def test_switch_off_makes_no_request(self):
        self.store.set_news_enabled(False)

        def reader(url, deadline):
            raise AssertionError('the network was read')
        result = news.api_response('/api/news', {}, self.store, reader)
        self.assertEqual((result['enabled'], result['items'], result['stale']), (False, [], False))
        self.assertEqual(news.api_response('/api/news', {'refresh': True}, self.store, reader)['enabled'], False)
        self.assertFalse(news.cache_path(self.store).exists())

    def test_endpoint_reads_through_the_cache_next_to_the_store(self):
        reader = Reader()
        result = news.api_response('/api/news', {}, self.store, reader)
        self.assertEqual(len(result['items']), 6)
        self.assertEqual(news.cache_path(self.store), Path(self.temp.name) / 'news-cache.json')
        self.assertTrue(news.cache_path(self.store).exists())
        with self.assertRaisesRegex(ValueError, 'true or false'):
            news.api_response('/api/news', {'refresh': 'yes'}, self.store, reader)
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            news.api_response('/api/news/other', {}, self.store, reader)

    def test_http_endpoint_uses_the_workspace_authentication(self):
        import inspect
        from myflames import ui
        from myflames.ui import WorkspaceServer
        self.assertIn('news_reader=fetch_feed', inspect.getsource(ui.cmd_ui))
        server = WorkspaceServer(store_path=str(self.store.path))
        self.assertIsNone(server.news_reader, 'test servers never read feeds')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(path, payload, authenticated=True):
            headers = {'Content-Type': 'application/json', 'Origin': server.origin}
            if authenticated:
                headers.update({'X-Myflames-Token': server.token,
                                'Cookie': server.cookie_name + '=' + server.session_secret})
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request('POST', path, body=json.dumps(payload), headers=headers)
                response = conn.getresponse()
                return response.status, json.loads(response.read().decode('utf-8'))
            finally:
                conn.close()

        for path in ('/api/news', '/api/news/settings'):
            self.assertEqual(post(path, {}, authenticated=False)[0], 403)
        self.assertEqual(post('/api/news/settings', {}), (200, {'enabled': True}))
        status, body = post('/api/news', {})
        self.assertEqual(status, 200)
        self.assertEqual((body['schema_version'], body['items'], body['error']), ('news-1.0', [], 'News is unavailable offline.'))
        self.assertEqual(post('/api/news', {'refresh': 'yes'})[0], 400)
        self.assertEqual(post('/api/news/elsewhere', {})[0], 404)
        self.assertEqual(post('/api/news/settings', {'enabled': False}), (200, {'enabled': False}))
        self.assertEqual(post('/api/news', {})[1]['enabled'], False)


if __name__ == '__main__':
    unittest.main()
