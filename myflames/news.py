"""MySQL and MariaDB headlines for Home, from fixed official HTTPS feeds.

The workspace reads the feeds in ``SOURCES`` and nothing else: the request
carries no URL, redirects may only stay on the feed hosts, responses stop at
1 MiB, and every read is bounded by socket timeouts, a per-feed deadline and
the time ``/api/news`` waits. A document that declares a DOCTYPE or an ENTITY
is refused before parsing, and titles and summaries keep plain text only.
Requests send no cookies and no identifiers; only a neutral User-Agent.

Headlines are cached for six hours in ``news-cache.json`` next to the
workspace store. A failed read (offline, an HTTP error, a malformed feed)
serves that cache with its age. Home reads ``/api/news`` only while it is on
screen and the Settings switch ("Show MySQL and MariaDB news on Home") is on;
with the switch off nothing is read. A workspace server reads the network
only when it was given a reader (``cmd_ui`` passes :func:`fetch_feed`), so
test servers never reach the network.
"""
import copy
import datetime
import email.utils
import html.parser
import http.client
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from . import __version__
from .tls import Watchdog, WatchedHTTPSHandler, https_context, watched_connection_class

SCHEMA_VERSION = 'news-1.0'
CACHE_VERSION = 'news-cache-1.0'
CACHE_NAME = 'news-cache.json'
#: The only feeds myflames reads. ``links`` limits headline links to the
#: official site of each source (host and path prefix): Planet MySQL also
#: aggregates community blogs, and myflames keeps only the MySQL team's posts.
SOURCES = (
    {'id': 'planet-mysql', 'name': 'Planet MySQL', 'engine': 'mysql',
     'url': 'https://planet.mysql.com/rss20.xml', 'links': (('blogs.oracle.com', '/mysql/'),)},
    {'id': 'mariadb-foundation', 'name': 'MariaDB Foundation', 'engine': 'mariadb',
     'url': 'https://mariadb.org/feed/', 'links': (('mariadb.org', '/'),)},
)
FEED_URLS = frozenset(source['url'] for source in SOURCES)
FEED_HOSTS = frozenset(urlsplit(source['url']).hostname for source in SOURCES)
MAX_BYTES = 1024 * 1024
SOCKET_TIMEOUT = 5.0     # connect, and each read of the socket
FEED_DEADLINE = 8.0      # one feed, from the request to its last byte
WAIT_TIMEOUT = 10.0      # the longest /api/news waits for a read in progress
CACHE_TTL = 6 * 3600     # a successful read is reused for six hours
RETRY_AFTER = 10 * 60    # a failed read is tried again after ten minutes
MIN_REFRESH = 60         # Refresh reads again at most once a minute
MAX_REDIRECTS = 3
MAX_ITEMS = 6            # headlines on Home
MAX_PARSED = 40          # entries read from one feed
TITLE_MAX = 200
SUMMARY_MAX = 240
SUMMARY_INPUT = 8000     # characters of HTML read for a summary
LINK_MAX = 2048
# Planet MySQL's CDN refuses a User-Agent whose first product it does not
# know; this one names the client library first and myflames second, and
# carries nothing about the user, the machine or a connection.
USER_AGENT = 'Python-urllib/%d.%d myflames/%s' % (sys.version_info[0], sys.version_info[1], __version__)
ACCEPT = 'application/rss+xml, application/atom+xml;q=0.9, application/xml;q=0.8, text/xml;q=0.7'
OFFLINE_MESSAGE = 'News is unavailable offline.'
SLOW_MESSAGE = 'News is taking longer than usual.'

_ATOM = '{http://www.w3.org/2005/Atom}'
_RSS1 = '{http://purl.org/rss/1.0/}'
_RDF = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}'
_DC = '{http://purl.org/dc/elements/1.1/}'
_CONTENT = '{http://purl.org/rss/1.0/modules/content/}'
_DECLARATION = re.compile(rb'<!\s*(?:DOCTYPE|ENTITY)', re.I)
_XML_ENCODING = re.compile(rb'\A(?:\xef\xbb\xbf)?\s*<\?xml\s[^>]*?\bencoding\s*=\s*["\']([^"\']{1,40})["\']')
# ASCII-compatible encodings only: the declaration check reads raw bytes, and
# UTF-16 or EBCDIC would hide "<!DOCTYPE" from it but not from the parser.
_ENCODINGS = frozenset(('utf-8', 'utf8', 'us-ascii', 'ascii', 'iso-8859-1', 'latin-1', 'latin1',
                        'windows-1252', 'cp1252'))
# Control characters and bidirectional overrides never reach the page.
_UNSAFE_TEXT = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]')
_ISO_DATE = re.compile(r'(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?)?\s*'
                       r'(Z|z|[+-]\d{2}(?::?\d{2})?)?')
_BLOCK_TAGS = frozenset(('p', 'br', 'div', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'td',
                         'th', 'blockquote', 'pre', 'figure', 'figcaption', 'section', 'article', 'hr'))
_SKIP_TAGS = frozenset(('script', 'style', 'template', 'noscript'))
_LOCK = threading.Lock()        # guards _INFLIGHT and _WORKERS
_WRITE_LOCK = threading.Lock()  # one cache update at a time
_INFLIGHT = {}  # cache path -> Event of the read in progress
_WORKERS = {}   # source id -> the thread reading it; never two at once, even past a deadline
_MEMORY = {}    # cache path -> the last cache this process built, kept when the disk write fails


class NewsError(ValueError):
    """A feed was refused or could not be read (never offline)."""


def _utc(moment):
    return moment.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _stamp(seconds):
    return _utc(datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc))


def _seconds(stamp):
    """Epoch seconds of a stamp written by _stamp, or None."""
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.datetime.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp()


# ---- Network -------------------------------------------------------------

def _allowed_feed_url(url):
    """True for an HTTPS URL on a feed host, without credentials or a port."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError, AttributeError):
        return False
    return (parts.scheme == 'https' and (parts.hostname or '').lower() in FEED_HOSTS and
            parts.username is None and parts.password is None and port in (None, 443))


class _Redirects(urllib.request.BaseHandler):
    """Follow at most MAX_REDIRECTS redirects, each to an HTTPS address on a
    feed host, within the feed's deadline, without reading a redirect's body
    (urllib's own handler reads it whole)."""

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get('Location') or headers.get('URI')
        fp.close()
        newurl = urljoin(req.full_url, location.strip()) if isinstance(location, str) and location.strip() else ''
        if not _allowed_feed_url(newurl):
            raise NewsError('The news source redirected outside its official site.')
        hops = getattr(req, 'news_hops', 0) + 1
        if hops > MAX_REDIRECTS:
            raise NewsError('The news source redirected too many times.')
        deadline = getattr(req, 'news_deadline', None)
        if deadline is not None and time.monotonic() > deadline:
            raise socket.timeout('The news source took too long to answer.')
        new = urllib.request.Request(newurl, headers=dict(req.headers), method='GET')
        new.news_hops = hops
        new.news_deadline = deadline
        return self.parent.open(new, timeout=req.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


# The watchdog and the watched HTTPS connection live in tls.py, shared with
# the release check (app_update.py).
_Watchdog = Watchdog
_connection_class = watched_connection_class


class _HTTPS(WatchedHTTPSHandler):
    def __init__(self, watchdog):
        super().__init__(watchdog, https_context())


def _opener(watchdog):
    # HTTPS only (an http: redirect has no handler), no cookie processor, no
    # proxy credentials from the environment, no file: or ftp: handlers.
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.UnknownHandler(), _HTTPS(watchdog),
                    urllib.request.HTTPDefaultErrorHandler(), _Redirects(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


def fetch_feed(url, deadline=None):
    """GET one allowlisted feed and return its bytes (at most 1 MiB)."""
    if url not in FEED_URLS:
        raise NewsError('Unsupported news source.')
    deadline = time.monotonic() + FEED_DEADLINE if deadline is None else deadline
    request = urllib.request.Request(url, headers={
        'User-Agent': USER_AGENT, 'Accept': ACCEPT, 'Accept-Encoding': 'identity'})
    request.news_deadline = deadline
    watchdog = _Watchdog(deadline)
    try:
        with _opener(watchdog).open(request, timeout=SOCKET_TIMEOUT) as response:
            body = _read_response(response, deadline)
    except Exception:
        if watchdog.expired:
            raise socket.timeout('The news source took too long to answer.')
        raise
    finally:
        watchdog.stop()
    if watchdog.expired:  # a body cut by the shutdown is not the feed
        raise socket.timeout('The news source took too long to answer.')
    return body


def _read_response(response, deadline):
    if not _allowed_feed_url(response.geturl()):
        raise NewsError('The news source answered from outside its official site.')
    if getattr(response, 'status', 200) != 200:
        raise NewsError('The news source answered HTTP %s.' % getattr(response, 'status', '?'))
    headers = response.headers
    encoding = (headers.get('Content-Encoding') or 'identity').strip().lower()
    if encoding not in ('', 'identity'):
        raise NewsError('The news source sent an encoding myflames does not read.')
    # Never read past MAX_BYTES. A longer feed is cut there and parse_feed
    # keeps the entries that ended before the cut (feeds list the newest first).
    # read1 returns what has arrived instead of waiting for a full chunk; the
    # watchdog in fetch_feed ends any read still going at the deadline.
    read = getattr(response, 'read1', None) or response.read
    chunks, size = [], 0
    while size < MAX_BYTES:
        if time.monotonic() > deadline:
            raise socket.timeout('The news source took too long to answer.')
        chunk = read(min(65536, MAX_BYTES - size))
        if not chunk:
            break
        size += len(chunk)
        chunks.append(chunk)
    return b''.join(chunks)


def _failure(exc):
    """(message, offline) for a failed read; offline means no answer at all."""
    if isinstance(exc, urllib.error.HTTPError):
        return 'The news source answered HTTP %d.' % exc.code, False
    if isinstance(exc, NewsError):
        return str(exc), False
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, ssl.SSLError) or isinstance(exc, ssl.SSLError):
        return 'A secure connection to the news source failed.', False
    if isinstance(exc, http.client.HTTPException):
        return 'The news source sent an incomplete answer.', False
    if isinstance(exc, (urllib.error.URLError, socket.timeout, TimeoutError, OSError)):
        return OFFLINE_MESSAGE, True
    return 'The feed could not be read.', False


# ---- Parsing -------------------------------------------------------------

class _PlainText(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(' ')

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def plain_text(value, limit):
    """Text without HTML tags, entities decoded, whitespace collapsed, at most *limit* characters."""
    if not isinstance(value, str) or not value:
        return ''
    parser = _PlainText()
    try:
        parser.feed(value)
        parser.close()
    except (AssertionError, ValueError):  # malformed markup in old Pythons
        return ''
    text = ' '.join(_UNSAFE_TEXT.sub('', ''.join(parser.parts)).split())
    if len(text) > limit:
        cut = text[:limit - 1]
        text = (cut.rsplit(' ', 1)[0] if ' ' in cut[limit // 2:] else cut).rstrip(' ,;:.-') + '\u2026'
    return text


def _text(element):
    return '' if element is None else ''.join(element.itertext())


def parse_date(value, now=None):
    """An RFC 822 (RSS) or ISO 8601 (Atom) date as UTC 'YYYY-MM-DDTHH:MM:SSZ', or None."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    value = value.strip()
    moment = None
    match = _ISO_DATE.fullmatch(value)
    try:
        if match:
            year, month, day, hour, minute, second, zone = match.groups()
            moment = datetime.datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0),
                                       int(second or 0), tzinfo=datetime.timezone.utc)
            if zone and zone not in ('Z', 'z'):
                digits = zone[1:].replace(':', '')
                hours, minutes = int(digits[:2]), int(digits[2:4] or 0)
                if hours > 23 or minutes > 59:
                    return None
                offset = datetime.timedelta(hours=hours, minutes=minutes)
                moment -= offset if zone[0] == '+' else -offset
        else:
            moment = email.utils.parsedate_to_datetime(value)
            if moment is not None and moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment is None:
        return None
    now = time.time() if now is None else now
    try:
        seconds = moment.timestamp()
    except (OverflowError, ValueError, OSError):
        return None
    # A date far in the future, or before the web, is not a publication date.
    if seconds > now + 86400 or moment.year < 1995:
        return None
    return _utc(moment)


def headline_link(source, value):
    """The headline's address on the source's official site, or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > LINK_MAX or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    try:
        parts = urlsplit(urljoin(source['url'], value))
        port = parts.port
    except (TypeError, ValueError):
        return None
    host = (parts.hostname or '').lower()
    if parts.scheme != 'https' or parts.username is not None or parts.password is not None or port not in (None, 443):
        return None
    for allowed, prefix in source['links']:
        if host == allowed and (parts.path or '/').startswith(prefix):
            return urlunsplit(('https', host, parts.path or '/', parts.query, parts.fragment))
    return None


def _item(source, title, link, date, summary, now):
    summary = summary if isinstance(summary, str) else ''
    title = plain_text(title, TITLE_MAX)
    link = headline_link(source, link)
    if not title or not link:
        return None
    return {'title': title, 'link': link, 'source': source['name'], 'source_id': source['id'],
            'engine': source['engine'], 'published': parse_date(date, now),
            'summary': plain_text(summary[:SUMMARY_INPUT], SUMMARY_MAX)}


def _atom_link(entry):
    fallback = None
    for link in entry.findall(_ATOM + 'link'):
        href = link.get('href')
        if not href:
            continue
        if link.get('rel', 'alternate') == 'alternate':
            return href
        fallback = fallback or href
    return fallback


_ENTRY_TAGS = {'rss': 'item', _RDF + 'RDF': _RSS1 + 'item', _ATOM + 'feed': _ATOM + 'entry'}


def _entry(kind, node):
    """(title, link, date, summary) of one RSS 2.0, RSS 1.0 or Atom entry."""
    if kind == 'rss':
        return (_text(node.find('title')), node.findtext('link'),
                node.findtext('pubDate') or node.findtext(_DC + 'date'),
                node.findtext('description') or node.findtext(_CONTENT + 'encoded'))
    if kind == _RDF + 'RDF':
        return (_text(node.find(_RSS1 + 'title')), node.findtext(_RSS1 + 'link'),
                node.findtext(_DC + 'date'), node.findtext(_RSS1 + 'description'))
    return (_text(node.find(_ATOM + 'title')), _atom_link(node),
            node.findtext(_ATOM + 'published') or node.findtext(_ATOM + 'updated'),
            _text(node.find(_ATOM + 'summary')) or _text(node.find(_ATOM + 'content')))


def parse_feed(body, source, now=None):
    """Headlines of one RSS 2.0, RSS 1.0 or Atom document, newest first.

    Only the first MAX_BYTES count. Entries that end before a cut (a longer
    feed) or before a malformed tail are kept; a document without one
    complete entry, or with no feed root, is refused."""
    if not isinstance(body, (bytes, bytearray)):
        raise NewsError('The feed is empty.')
    body = bytes(body[:MAX_BYTES])
    declared = _XML_ENCODING.match(body)
    if b'\x00' in body or (declared and declared.group(1).decode('ascii', 'replace').strip().lower() not in _ENCODINGS):
        raise NewsError('The feed uses a text encoding myflames does not read.')
    if not body.lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'<'):
        raise NewsError('The feed is not valid XML.')  # also EBCDIC, whose "<" is another byte
    # A document type can declare entities (expansion bombs, external files):
    # refuse it before the parser sees a byte.
    if _DECLARATION.search(body):
        raise NewsError('The feed declares a document type, which myflames does not read.')
    parser = ET.XMLPullParser(events=('start', 'end'))
    complete = True
    try:
        parser.feed(body)
        parser.close()
    except (ET.ParseError, ValueError, LookupError, UnicodeError, RecursionError):
        complete = False
    kind, nodes = None, []
    try:
        for event, element in parser.read_events():
            if kind is None:
                kind = element.tag
                if kind not in _ENTRY_TAGS:
                    break
            elif event == 'end' and element.tag == _ENTRY_TAGS[kind]:
                nodes.append(element)
                if len(nodes) >= MAX_PARSED:
                    break
    except (ET.ParseError, ValueError, LookupError, UnicodeError, RecursionError):
        complete = False
    if kind is not None and kind not in _ENTRY_TAGS:
        raise NewsError('The answer is not an RSS or Atom feed.')
    if kind is None or (not complete and not nodes):
        raise NewsError('The feed is not valid XML.')
    items = [item for item in (_item(source, *_entry(kind, node), now=now) for node in nodes) if item]
    items.sort(key=lambda item: item['published'] or '', reverse=True)
    return items


# ---- Cache ---------------------------------------------------------------

def cache_path(store):
    """news-cache.json in the directory of the workspace store."""
    return Path(store.path).parent / CACHE_NAME


def _empty_cache():
    return {'schema_version': CACHE_VERSION, 'sources': {}}


def _clean_entry(source, entry, now):
    if not isinstance(entry, dict):
        return None
    clean = {}
    for key in ('checked_at', 'fetched_at'):
        clean[key] = entry.get(key) if _seconds(entry.get(key)) is not None else None
    error = entry.get('error')
    clean['error'] = error[:200] if isinstance(error, str) and error else None
    clean['offline'] = entry.get('offline') is True
    items = []
    for item in entry.get('items') if isinstance(entry.get('items'), list) else []:
        if not isinstance(item, dict):
            continue
        # The file is local, but re-check it like a feed before it reaches the page.
        again = _item(source, item.get('title'), item.get('link'), item.get('published'), item.get('summary'), now)
        if again:
            items.append(again)
    clean['items'] = items[:MAX_PARSED]
    return clean


def load_cache(path, now=None):
    """The cached headlines, validated again; an empty cache when absent or unreadable."""
    from .secret_store import _open_private_file
    now = time.time() if now is None else now
    try:
        # No symbolic link, a regular file owned by this user.
        fd, _created = _open_private_file(Path(path), os.O_RDONLY, protect=False)
        with os.fdopen(fd, 'rb') as handle:
            raw = handle.read(4 * MAX_BYTES + 1)
        if len(raw) > 4 * MAX_BYTES:
            return _empty_cache()
        value = json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return _empty_cache()
    cache = _empty_cache()
    if not isinstance(value, dict) or value.get('schema_version') != CACHE_VERSION or not isinstance(value.get('sources'), dict):
        return cache
    try:
        for source in SOURCES:
            entry = _clean_entry(source, value['sources'].get(source['id']), now)
            if entry is not None:
                cache['sources'][source['id']] = entry
    except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
        return _empty_cache()
    return cache


def _snapshot(path, now):
    """The cache on disk, with newer entries this process holds (a write that failed)."""
    cache = load_cache(path, now)
    remembered = _MEMORY.get(str(path))
    for key, entry in (remembered or {}).get('sources', {}).items():
        disk = cache['sources'].get(key)
        if not disk or (_seconds(entry.get('checked_at')) or 0) > (_seconds(disk.get('checked_at')) or 0):
            cache['sources'][key] = copy.deepcopy(entry)
    return cache


def _write_cache(path, cache):
    from .secret_store import _ensure_private_directory
    path = Path(path)
    _ensure_private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.news-', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(cache, handle, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _due(entry, refresh, now):
    if not entry or _seconds(entry.get('checked_at')) is None:
        return True
    age = now - _seconds(entry['checked_at'])
    if age < 0:  # the clock moved back
        return True
    if refresh:
        return age >= MIN_REFRESH
    return age >= (RETRY_AFTER if entry.get('error') else CACHE_TTL)


def _read_source(source, reader, results, now):
    try:
        body = reader(source['url'], time.monotonic() + FEED_DEADLINE)
        results[source['id']] = {'items': parse_feed(body, source, now), 'error': None, 'offline': False}
    except Exception as exc:  # every failure becomes a calm per-source state
        message, offline = _failure(exc)
        results[source['id']] = {'items': None, 'error': message, 'offline': offline}


def _refresh(path, sources, reader, event, clock):
    key = str(path)
    try:
        results, threads = {}, []
        for source in sources:
            with _LOCK:
                earlier = _WORKERS.get(source['id'])
                if earlier is not None and earlier.is_alive():
                    # A read still stuck past its deadline (a DNS lookup or a TCP
                    # connect is outside the watchdog): never start a second one.
                    continue
                thread = threading.Thread(target=_read_source, args=(source, reader, results, clock()),
                                          name='myflames-news-' + source['id'], daemon=True)
                _WORKERS[source['id']] = thread
                thread.start()  # under the lock: no one sees it registered but not alive
            threads.append(thread)
        # DNS lookups and TCP connects are outside the watchdog: bound the wait as well.
        stop = time.monotonic() + FEED_DEADLINE + SOCKET_TIMEOUT
        for thread in threads:
            thread.join(max(0, stop - time.monotonic()))
        now = clock()
        with _WRITE_LOCK:
            cache = _snapshot(path, now)
            for source in sources:
                result = results.get(source['id']) or {'items': None, 'error': 'The news source took too long to answer.',
                                                       'offline': True}
                entry = cache['sources'].get(source['id']) or {'fetched_at': None, 'items': []}
                entry['checked_at'] = _stamp(now)
                entry['error'] = result['error']
                entry['offline'] = result['offline']
                if result['items'] is not None:
                    entry['items'] = result['items']
                    entry['fetched_at'] = _stamp(now)
                cache['sources'][source['id']] = entry
            # This process answers from memory when the disk write fails, so a
            # read is neither lost nor repeated at once.
            _MEMORY[key] = copy.deepcopy(cache)
            try:
                _write_cache(path, cache)
            except OSError:
                pass
    finally:
        with _LOCK:
            _INFLIGHT.pop(key, None)
        event.set()


def _due_sources(cache, refresh, now):
    return [source for source in SOURCES if _due(cache['sources'].get(source['id']), refresh, now)]


def read_news(path, reader, refresh=False, wait=WAIT_TIMEOUT, clock=time.time):
    """news-1.0 for Home: the cache, read again first when it is due.

    With *reader* None nothing is read and missing sources are reported
    offline. A read in progress is shared; the caller waits at most *wait*
    seconds and otherwise answers from the cache while the read finishes."""
    path = Path(path)
    key = str(path)
    now = clock()
    cache = _snapshot(path, now)
    due = _due_sources(cache, refresh, now)
    if due and reader is not None:
        event = None
        with _LOCK:
            event = _INFLIGHT.get(key)
            if event is None:
                # A read may have finished since the snapshot: look again
                # under the lock before starting one.
                cache = _snapshot(path, now)
                due = _due_sources(cache, refresh, now)
                if due:
                    event = threading.Event()
                    _INFLIGHT[key] = event
                    threading.Thread(target=_refresh, args=(path, due, reader, event, clock),
                                     name='myflames-news', daemon=True).start()
        if event is not None:
            finished = event.wait(wait)
            now = clock()
            cache = _snapshot(path, now)
            if not finished:
                # The read goes on and fills the cache; this answer says so.
                for source in due:
                    entry = cache['sources'].setdefault(source['id'], {'fetched_at': None, 'checked_at': None, 'items': []})
                    entry.update(error=SLOW_MESSAGE, offline=False, pending=True)
    elif due:
        for source in due:
            entry = cache['sources'].setdefault(source['id'], {'fetched_at': None, 'checked_at': None, 'items': []})
            entry.update(error=OFFLINE_MESSAGE, offline=True)
    return report(cache)


# ---- news-1.0 ------------------------------------------------------------

def _select(cache):
    """Up to MAX_ITEMS headlines, taken from each source in turn, newest first."""
    pools = []
    for source in SOURCES:
        entry = cache['sources'].get(source['id']) or {}
        pools.append(sorted(entry.get('items') or [], key=lambda item: item['published'] or '', reverse=True))
    picked, links, titles = [], set(), set()
    while len(picked) < MAX_ITEMS and any(pools):
        for pool in pools:
            while pool:
                item = pool.pop(0)
                title = item['title'].casefold()
                if item['link'] in links or title in titles:
                    continue
                links.add(item['link'])
                titles.add(title)
                picked.append(item)
                break
            if len(picked) >= MAX_ITEMS:
                break
    picked.sort(key=lambda item: item['published'] or '', reverse=True)
    return picked


def _source_rows(cache):
    rows = []
    for source in SOURCES:
        entry = cache['sources'].get(source['id']) if cache else None
        row = {'id': source['id'], 'name': source['name'], 'engine': source['engine'], 'url': source['url'],
               'fetched_at': None, 'checked_at': None, 'items': 0}
        if entry:
            row.update(fetched_at=entry.get('fetched_at'), checked_at=entry.get('checked_at'),
                       items=len(entry.get('items') or []))
            if entry.get('error'):
                row['error'] = entry['error']
        rows.append(row)
    return rows


def report(cache):
    """The news-1.0 document for a cache."""
    failed = [(source, cache['sources'][source['id']]) for source in SOURCES
              if (cache['sources'].get(source['id']) or {}).get('error')]
    items = _select(cache)
    fetched = [cache['sources'][key].get('fetched_at') for key in {item['source_id'] for item in items}]
    fetched = [stamp for stamp in fetched if stamp]
    result = {'schema_version': SCHEMA_VERSION, 'enabled': True, 'items': items,
              # When the sources shown were read at different times: the oldest.
              'fetched_at': min(fetched) if fetched else None,
              'stale': bool(failed), 'sources': _source_rows(cache)}
    if failed:
        offline = all(entry.get('offline') for _, entry in failed)
        if all(entry.get('pending') for _, entry in failed):
            result['error'] = SLOW_MESSAGE
        elif len(failed) == len(SOURCES):
            result['error'] = OFFLINE_MESSAGE if offline else 'News could not be read.'
        else:
            result['error'] = '%s could not be read.' % ' and '.join(source['name'] for source, _ in failed)
        result['offline'] = offline
    return result


def disabled_report():
    """news-1.0 while the Settings switch is off: nothing read, nothing shown."""
    return {'schema_version': SCHEMA_VERSION, 'enabled': False, 'items': [], 'fetched_at': None,
            'stale': False, 'sources': _source_rows(None)}


def api_response(path, payload, store, reader=None):
    """/api/news and /api/news/settings (ui.py)."""
    if path == '/api/news/settings':
        if 'enabled' in payload:
            return {'enabled': store.set_news_enabled(payload['enabled'])}
        return {'enabled': store.news_enabled()}
    if path == '/api/news':
        refresh = payload.get('refresh', False)
        if not isinstance(refresh, bool):
            raise ValueError('refresh must be true or false.')
        if not store.news_enabled():
            return disabled_report()
        return read_news(cache_path(store), reader, refresh=refresh)
    raise ValueError('Unknown workspace operation.')
