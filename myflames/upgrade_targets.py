"""Upgrade targets for the Upgrade check: the versions to offer and how the server was installed.

Versions come from the vendor, read when the Upgrade check opens:

* MySQL Community Server: the official release notes. The documentation index
  (``https://dev.mysql.com/doc/``) links one release notes page per current
  series (on 2026-11-08: 8.0, 8.4, 9.7, 26.10; the 9.7 page also lists 9.0 to
  9.6 and the 26.10 page 26.7). Each "Changes in MySQL X.Y.Z (YYYY-MM-DD)"
  entry is a release; an entry whose parentheses add a label such as
  "Release Candidate" or "Development Milestone" is a pre-GA release and is
  left out.
* Percona Server for MySQL: the package listings of repo.percona.com, one
  repository per release line (``ps-80``, ``ps-84-lts``, ``ps-97-lts``,
  ``ps-8x-innovation`` for 8.1 to 8.3). An RPM install reads the Yum folders
  (``<line>/yum/release/<el>/RPMS/<arch>/``), anything else the APT pool
  (``<line>/apt/pool/main/p/percona-server/``), so the list is what the
  server's own install method can install. Percona publishes tarballs on a
  script-driven download page, so a tarball install also reads the APT pool.

Offline, MySQL falls back to the release catalog bundled with myflames
(``sysvar_history_mysql.VERSIONS``, read from official images) and says so;
Percona has no bundled list.

Network rules (as ``tool_updates``): fixed HTTPS URL patterns only
(``_ALLOWED``), no redirects, no ambient proxy, ``MAX_BYTES`` per response,
``REQUEST_TIMEOUT`` per request and ``REPORT_TIMEOUT`` per report; a result is
cached for ``SUCCESS_TTL`` (a failure for ``FAILURE_TTL``). Network text is only
matched against version and file name patterns: no link from it is followed
except release notes pages whose series the index names, and none is rendered.

Upgrade paths, MySQL 9.7 Reference Manual, "Upgrade Paths" (Table 3.1):
within an LTS or bugfix series; from an LTS or bugfix series to the next LTS
series; from an LTS or bugfix release to an Innovation release before the next
LTS series; from an Innovation series to the next LTS series; within an
Innovation series. An Innovation release after the next LTS series takes two
steps, and a bugfix or LTS series cannot be skipped (5.7 goes to 8.0 first).
Bugfix series are 5.7 and 8.0, LTS series 8.4 and 9.7 (MySQL's own terms); the kind
of each series comes from the bundled catalog's maturity, and a series it does
not know is listed apart, never placed on a path.

Install method, from the layout the server reports (``cmake/install_layout.cmake``
and ``packaging/``): RPM builds use INSTALL_LAYOUT=RPM, whose plugin folder is
``lib64/mysql/plugin`` on 64-bit systems (``packaging/rpm-oel/mysql.spec.in``;
the official Docker image is built the same way, ``packaging/rpm-docker``);
DEB builds use INSTALL_LAYOUT=DEB with ``lib/mysql/plugin``
(``packaging/deb-in/rules.in``); the generic archives keep every folder under
the base folder (``lib/plugin``). Ubuntu's own package reports a version such
as ``8.0.43-0ubuntu0.24.04.1``. Any of these can be overridden in an option
file, so the method comes with its evidence and a confidence. ``build_id``
(MySQL 8.0.31+ Linux builds, not Windows, macOS or Alpine; a SHA-1 the linker writes for each build,
``sql/sys_vars.cc`` Sys_server_build_id) is shown as evidence: it identifies
one build, but no public index maps build IDs to packages.
"""
import datetime
import html
import http.client
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait

from .perf_common import Reader, literal
from .server_info import product_identity, tool_version_tuple as version_tuple
from .tls import https_context

TAG = '/*myflames:upgrade_targets*/ '
SCHEMA = 'upgrade-targets-1.0'
DOC_INDEX = 'https://dev.mysql.com/doc/'
RELNOTES = 'https://dev.mysql.com/doc/relnotes/mysql/%s/en/'
PERCONA_ROOT = 'https://repo.percona.com/'
YUM_GUIDE = 'https://dev.mysql.com/doc/mysql-yum-repo-quick-guide/en/'
APT_GUIDE = 'https://dev.mysql.com/doc/mysql-apt-repo-quick-guide/en/'
MYSQL_DOWNLOADS = 'https://dev.mysql.com/downloads/mysql/'
PERCONA_REPO_GUIDE = 'https://docs.percona.com/percona-software-repositories/percona-release.html'
MAX_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 5.0
#: A response trickling in below REQUEST_TIMEOUT per read still ends here.
READ_TIMEOUT = 10.0
REPORT_TIMEOUT = 15.0
SUCCESS_TTL = 3600.0
FAILURE_TTL = 60.0
MAX_SERIES_PAGES = 8
MAX_EL_RELEASES = 4
WORKERS = 4
MAX_CACHE = 256
#: Release pages checked per report (the targets offered, newest first).
MAX_RELEASE_PAGES = 32

_SERIES = r'[0-9]{1,2}\.[0-9]{1,2}'
_LINE = r'ps-[0-9a-z]{2}(?:-lts|-innovation)?'
_ALLOWED = (
    re.escape(DOC_INDEX),
    re.escape('https://dev.mysql.com/doc/relnotes/mysql/') + _SERIES + '/en/',
    re.escape('https://dev.mysql.com/doc/relnotes/mysql/') + _SERIES + '/en/news-[0-9]{1,2}-[0-9]{1,2}-[0-9]{1,3}\\.html',
    re.escape(PERCONA_ROOT),
    re.escape(PERCONA_ROOT) + _LINE + '/yum/release/',
    re.escape(PERCONA_ROOT) + _LINE + '/yum/release/[0-9]{1,2}/RPMS/(?:x86_64|aarch64)/',
    re.escape(PERCONA_ROOT) + _LINE + '/apt/pool/main/p/percona-server/',
)
_VARS = ('version', 'version_comment', 'basedir', 'plugin_dir', 'character_sets_dir', 'lc_messages_dir',
         'socket', 'version_compile_os', 'version_compile_machine', 'build_id', 'hostname')
_PRE_GA = re.compile(r'release candidate|development milestone|milestone|beta|alpha|preview|not yet released', re.I)
_ENTRY = re.compile(r'<a\s+href="(news-[0-9]{1,2}-[0-9]{1,2}-[0-9]{1,3}\.html)"\s*>\s*Changes in MySQL '
                    r'([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3})\s*\(([^()<>]{1,80})\)')
_DEB = re.compile(r'percona-server-server_([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3})-([0-9]{1,4})-[0-9]{1,3}'
                  r'\.([a-z]{2,20})_(amd64|arm64)\.deb')
_RPM = re.compile(r'percona-server-server-([0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3})-([0-9]{1,4})\.[0-9]{1,3}'
                  r'\.el([0-9]{1,2})\.(x86_64|aarch64)\.rpm')
_KIND = {'GA': 'bugfix', 'LTS': 'lts', 'INNOVATION': 'innovation'}
_PATH_LABEL = {'patch': 'same series', 'next_lts': 'next LTS', 'innovation': 'Innovation',
               'lts_after_innovation': 'next LTS'}
_CACHE = {}
_INFLIGHT = {}
_LOCK = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('The release source redirected.')


def _fetch(url):
    if not any(re.fullmatch(pattern, url) for pattern in _ALLOWED):
        raise ValueError('Unsupported release source.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=https_context()), _NoRedirect())
    request = urllib.request.Request(url, headers={'Accept-Encoding': 'identity'})
    started = time.monotonic()
    with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
        if response.geturl() != url or response.status != 200:
            raise ValueError('Unexpected release source response.')
        chunks, size = [], 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError('The release source exceeded the size limit.')
            if time.monotonic() - started > READ_TIMEOUT:
                raise ValueError('The release source was too slow.')
    return b''.join(chunks).decode('utf-8', 'replace')


fetch_page = _fetch


def offline(url):
    """The reader of a server built without one (tests): nothing reaches the network."""
    raise ValueError('This server does not read release pages.')


_ERRORS = (OSError, ValueError, TypeError, KeyError, urllib.error.URLError, http.client.HTTPException)


def _now_text():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _cached(key, loader, deadline):
    """loader() once per key and TTL; concurrent callers wait for the same read."""
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        event = _INFLIGHT.get(key)
        owner = event is None
        if owner:
            event = _INFLIGHT[key] = threading.Event()
    if not owner:
        if not event.wait(max(0.0, deadline - time.monotonic())):
            return {'error': 'The release check timed out.'}
        with _LOCK:
            hit = _CACHE.get(key)
        return hit[1] if hit else {'error': 'The release check failed.'}
    try:
        try:
            value = loader(deadline)
            # A partly read result is retried soon, like a failure.
            ttl = FAILURE_TTL if value.get('error') or value.get('partial') else SUCCESS_TTL
        except _ERRORS + (RecursionError,) as exc:
            value, ttl = {'error': _error_text(exc)}, FAILURE_TTL
        value.setdefault('checked_at', _now_text())
        with _LOCK:
            if len(_CACHE) >= MAX_CACHE:
                del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0])]
            _CACHE[key] = (time.monotonic() + ttl, value)
        return value
    finally:
        with _LOCK:
            _INFLIGHT.pop(key, None)
        event.set()


def _error_text(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return 'The release source answered HTTP %d.' % exc.code
    if isinstance(exc, ValueError):
        return str(exc)
    return 'Could not reach the release source.'


def _fetch_all(urls, deadline, fetch):
    """Fetch *urls* in parallel within *deadline*: {url: text} and the first error."""
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futures = {pool.submit(fetch, url): url for url in urls}
        done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
        pages, error = {}, None
        for future in done:
            try:
                pages[futures[future]] = future.result()
            except _ERRORS as exc:
                error = error or _error_text(exc)
        if pending:
            error = error or 'The release check timed out.'
        return pages, error
    finally:
        # Python 3.7 has no cancel_futures: cancel what has not started, and
        # let a running read end at its own READ_TIMEOUT.
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False)


def _vt(text):
    return tuple(int(part) for part in text.split('.'))


def _series(version):
    return '.'.join(version.split('.')[:2])


# MySQL Community Server ---------------------------------------------------

def parse_doc_index(text):
    """Release notes series the documentation index links, newest first."""
    found = sorted(set(re.findall(r'relnotes/mysql/(' + _SERIES + ')/en/', text)), key=_vt, reverse=True)
    return found[:MAX_SERIES_PAGES]


def parse_release_notes(text, series, url, today=None):
    """GA releases on one release notes page: [{version, date, url}].

    A page can list other series of its major version (the 9.7 page lists 9.0
    to 9.6); entries from a newer series or another major are ignored, as are
    entries with a pre-GA label or a date after *today*."""
    today = today or datetime.date.today()
    page = _vt(series)
    out = []
    for href, version, inside in _ENTRY.findall(text):
        parts = [part.strip() for part in html.unescape(inside).split(',')]
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', parts[0]):
            continue
        if any(_PRE_GA.search(part) for part in parts[1:]):
            continue
        if _vt(version)[0] != page[0] or _vt(_series(version)) > page:
            continue
        try:
            if datetime.datetime.strptime(parts[0], '%Y-%m-%d').date() > today:
                continue
        except ValueError:
            continue
        out.append({'version': version, 'date': parts[0], 'url': url + href})
    return out


_REMOVED = re.compile(r'no longer available for download', re.I)
_DOCKER_ONLY = re.compile(r'docker image,?\s+only', re.I)


def parse_availability(text):
    """'removed', 'docker_only' or 'ok' from one release's notes page: Oracle
    marks a pulled release "This release is no longer available for download"
    and an image-only patch "a Critical Security Patch Update for the MySQL
    Server docker image, only"."""
    plain = re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text)))
    if _REMOVED.search(plain):
        return 'removed'
    if _DOCKER_ONLY.search(plain):
        return 'docker_only'
    # An empty or unrecognizable page proves nothing: shown, not judged.
    return 'ok' if re.search(r'Changes in MySQL [0-9]', plain) else 'unknown'


def release_availability(fetch, urls, deadline):
    """{url: 'removed' | 'docker_only' | 'ok'} for release pages, cached per
    page; a page that could not be read is left out (shown, not judged)."""
    out, missing = {}, []
    with _LOCK:
        for url in urls:
            hit = _CACHE.get(('page', fetch, url))
            if hit and hit[0] > time.monotonic():
                out[url] = hit[1]
            else:
                missing.append(url)
    if missing:
        pages, unused = _fetch_all(missing, deadline, fetch)
        with _LOCK:
            for url, text in pages.items():
                out[url] = parse_availability(text)
                if len(_CACHE) >= MAX_CACHE:
                    del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0])]
                ttl = FAILURE_TTL if out[url] == 'unknown' else SUCCESS_TTL
                _CACHE[('page', fetch, url)] = (time.monotonic() + ttl, out[url])
    return out


def _load_mysql(fetch):
    def load(deadline):
        first, error = _fetch_all([DOC_INDEX], deadline, fetch)
        if DOC_INDEX not in first:
            raise ValueError(error or 'Could not read the documentation index.')
        series = parse_doc_index(first[DOC_INDEX])
        if not series:
            raise ValueError('The documentation index lists no release notes.')
        urls = [RELNOTES % s for s in series]
        pages, error = _fetch_all(urls, deadline, fetch)
        releases = {}
        for s, url in zip(series, urls):
            entries = parse_release_notes(pages.get(url, ''), s, url)
            if url in pages and not entries:
                # A page that answers but lists nothing is a failed read too.
                error = error or 'The %s release notes page listed no releases.' % s
            for entry in entries:
                releases.setdefault(entry['version'], entry)
        if not releases:
            raise ValueError(error or 'The release notes listed no releases.')
        return {'releases': sorted(releases.values(), key=lambda r: _vt(r['version'])),
                'urls': [DOC_INDEX] + [u for u in urls if u in pages], 'partial': error}
    return load


def bundled_mysql_releases():
    from . import sysvar_history_mysql as catalog
    return [{'version': row[0], 'date': None, 'url': None} for row in catalog.VERSIONS]


def series_kinds():
    """{series: 'bugfix' | 'lts' | 'innovation'} from the bundled catalog's maturity."""
    from . import sysvar_history_mysql as catalog
    kinds = {}
    for row in catalog.VERSIONS:
        kind = _KIND.get(row[2])
        if kind:
            kinds[_series(row[0])] = kind
    return kinds


# Percona Server ------------------------------------------------------------

def parse_percona_lines(text):
    """{line: series or 'X.x'} for the Percona Server repositories the root lists."""
    lines = {}
    for name in set(re.findall(r'href="(' + _LINE + r')/"', text)):
        m = re.fullmatch(r'ps-([0-9])([0-9])(?:-lts)?', name)
        if m:
            lines[name] = '%s.%s' % m.groups()
            continue
        m = re.fullmatch(r'ps-([0-9])x-innovation', name)
        if m:
            lines[name] = m.group(1) + '.x'
    return lines


def parse_percona_deb(text):
    """{(version, build): set(codenames)} from an APT pool listing."""
    out = {}
    for version, build, codename, unused in _DEB.findall(text):
        out.setdefault((version, int(build)), set()).add(codename)
    return out


def parse_percona_rpm(text):
    """{(version, build): set('el9', ...)} from a Yum RPMS listing."""
    out = {}
    for version, build, el, unused in _RPM.findall(text):
        out.setdefault((version, int(build)), set()).add('el' + el)
    return out


def _load_root(fetch, deadline):
    pages, error = _fetch_all([PERCONA_ROOT], deadline, fetch)
    if PERCONA_ROOT not in pages:
        raise ValueError(error or 'Could not read the Percona repositories.')
    return {'lines': parse_percona_lines(pages[PERCONA_ROOT])}


def _missing_is_empty(fetch):
    """An el release without this architecture's folder answers 404 (ps-80's
    el7 on aarch64, Amazon Linux 2 in ps-8x-innovation): no packages, no error."""
    def read(url):
        try:
            return fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return ''
            raise
    return read


def _line_for(lines, series):
    for line, value in lines.items():
        if value == series:
            return line
    major, minor = _vt(series)
    for line, value in lines.items():
        if value == '%d.x' % major and value != series:
            return line
    return None


def _load_percona(fetch, lines_wanted, rpm, arch):
    def load(deadline):
        listings = {}
        for line in lines_wanted:
            listings[line] = PERCONA_ROOT + line + ('/yum/release/' if rpm else '/apt/pool/main/p/percona-server/')
        pages, error = _fetch_all(list(listings.values()), deadline, fetch)
        sources = {}
        if rpm:
            for line, url in listings.items():
                els = sorted({int(x) for x in re.findall(r'href="([0-9]{1,2})/"', pages.get(url, ''))}, reverse=True)
                for el in els[:MAX_EL_RELEASES]:
                    sources[PERCONA_ROOT + '%s/yum/release/%d/RPMS/%s/' % (line, el, arch)] = line
            rpm_pages, rpm_error = _fetch_all(list(sources), deadline, _missing_is_empty(fetch))
            pages, error = rpm_pages, error or rpm_error
        else:
            sources = {url: line for line, url in listings.items()}
        packages = {}
        for url, text in pages.items():
            parsed = parse_percona_rpm(text) if rpm else parse_percona_deb(text)
            for key, where in parsed.items():
                entry = packages.setdefault(key, {'packages': set(), 'repository': sources.get(url)})
                entry['packages'].update(where)
        if not packages:
            raise ValueError(error or 'The Percona repositories listed no packages.')
        releases = [{'version': '%s-%d' % key, 'upstream': key[0], 'build': key[1], 'repository': entry['repository'],
                     'packages': sorted(entry['packages']), 'date': None, 'url': None}
                    for key, entry in packages.items()]
        releases.sort(key=lambda r: _vt(r['upstream']) + (r['build'],))
        urls = sorted(set(listings.values()) | set(sources)) if rpm else sorted(listings.values())
        return {'releases': releases, 'urls': urls, 'partial': error}
    return load


# Install method ------------------------------------------------------------

def _plain(value):
    """Server text for display: no control or format characters (bidirectional
    overrides could make a path read as another)."""
    return ''.join(c for c in str(value) if unicodedata.category(c) not in ('Cc', 'Cf'))[:300]


def _clean_path(value):
    text = str(value or '').strip().replace('\\', '/')
    return text[:-1] if len(text) > 1 and text.endswith('/') else text


def install_method(variables):
    """How the server was installed, from the layout it reports.

    Returns {method, label, confidence, evidence[], package, build_id,
    container}. *method* is rpm, deb, ubuntu_deb, tarball, windows_msi,
    windows_zip, homebrew, macos or unknown."""
    v = {str(k).lower(): ('' if val is None else _plain(val)) for k, val in (variables or {}).items()}
    basedir, plugin = _clean_path(v.get('basedir')), _clean_path(v.get('plugin_dir'))
    version, comment = v.get('version', ''), v.get('version_comment', '')
    os_name, machine = v.get('version_compile_os', '').lower(), v.get('version_compile_machine', '').lower()
    evidence = []
    if basedir:
        evidence.append('basedir is %s' % v.get('basedir'))
    if plugin:
        evidence.append('plugin_dir is %s' % v.get('plugin_dir'))
    out = {'method': 'unknown', 'label': 'Not recognized', 'confidence': 'low', 'evidence': evidence,
           'package': None, 'build_id': v.get('build_id') or None,
           'container': False}
    if out['build_id']:
        evidence.append('build_id %s identifies this exact build (no public index maps it to a package)'
                        % out['build_id'][:16])
    archive = re.search(r'((?:mysql|Percona-Server)-[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{1,3}(?:-[0-9]{1,4})?'
                        r'-(?:linux|Linux)[A-Za-z0-9._-]{0,60})', basedir)
    if os_name.startswith('win'):
        if re.search(r'/program files/mysql/mysql server [0-9.]+$', basedir.lower()):
            out.update(method='windows_msi', label='Windows installer (MSI)', confidence='medium')
            evidence.append('the base folder is where the MySQL Installer puts the server')
        else:
            out.update(method='windows_zip', label='Windows ZIP archive or custom folder', confidence='low')
        return out
    if os_name.startswith(('macos', 'osx', 'darwin')) or basedir.startswith(('/opt/homebrew/', '/usr/local/cellar/')) \
            or '/cellar/' in basedir.lower():
        if '/cellar/' in basedir.lower() or basedir.startswith('/opt/homebrew/'):
            out.update(method='homebrew', label='Homebrew', confidence='high')
            evidence.append('the base folder is in a Homebrew Cellar')
        else:
            out.update(method='macos', label='macOS package or archive', confidence='low')
        return out
    if re.search(r'ubuntu', version, re.I) or comment.strip().lower() == '(ubuntu)':
        out.update(method='ubuntu_deb', label="Ubuntu's own mysql-server package", confidence='high')
        evidence.append('the version %s carries an Ubuntu package suffix' % version
                        if re.search(r'ubuntu', version, re.I) else 'version_comment is (Ubuntu)')
        return out
    if basedir in ('/usr', '') and plugin == '/usr/lib64/mysql/plugin':
        if comment.strip().lower() == 'source distribution':
            # Distribution builds (Fedora's mysql8.4.spec) use the RPM layout
            # without Oracle's compilation comment (cmake/mysql_version.cmake).
            out.update(method='distro_rpm', label="Your distribution's own package (RPM)", confidence='medium')
            evidence.append('lib64/mysql/plugin under /usr is the RPM layout, and version_comment "Source '
                            'distribution" is what a distribution build reports')
        else:
            out.update(method='rpm', label='RPM packages', confidence='high')
            evidence.append('lib64/mysql/plugin under /usr is the RPM layout')
    elif basedir in ('/usr', '') and plugin == '/usr/lib/mysql/plugin':
        bits64 = machine in ('x86_64', 'aarch64', 'arm64', 'amd64', 'ppc64le', 's390x')
        out.update(method='deb', label='DEB packages', confidence='high' if bits64 else 'medium')
        evidence.append('lib/mysql/plugin under /usr is the DEB layout' +
                        ('' if bits64 else ' (32-bit RPM builds use it too)'))
    elif basedir and basedir != '/usr' and plugin and plugin == basedir + '/lib/plugin':
        out.update(method='tarball', label='Generic Linux archive (tar)', confidence='high' if archive else 'medium')
        evidence.append('every folder is under the base folder, as in the generic archives')
    if archive and out['method'] in ('tarball', 'unknown'):
        out['package'] = archive.group(1)
        if out['method'] == 'unknown':
            out.update(method='tarball', label='Generic Linux archive (tar)', confidence='medium')
        evidence.append('the base folder is named after the archive %s' % archive.group(1))
    # The official MySQL and Percona images are built with the RPM layout; a
    # container host name on another layout says nothing about how to upgrade.
    out['container'] = out['method'] == 'rpm' and bool(re.fullmatch(r'[0-9a-f]{12}', v.get('hostname', '')))
    return out


# Upgrade paths -------------------------------------------------------------

def upgrade_paths(current, releases, kinds):
    """Split *releases* into direct targets, not-direct ones and unclassified series.

    *current* and each release's ``key`` are comparable tuples (Percona adds
    its build number); each release names its ``series``."""
    cur_series = '.'.join(str(n) for n in current[:2])
    cur_kind = kinds.get(cur_series)
    stops = sorted({s for s, k in kinds.items() if k in ('bugfix', 'lts')}, key=_vt)
    after = [s for s in stops if _vt(s) > _vt(cur_series)]
    before = [s for s in stops if _vt(s) < _vt(cur_series)]
    next_stop = after[0] if after else None
    prev_stop = before[-1] if before else None
    # An Innovation series is still on the manual's paths after the next LTS
    # ships, but that LTS supersedes it: it is listed as not direct then.
    superseded = bool(next_stop) and any(r['series'] == next_stop for r in releases)
    targets, blocked, unclassified = [], {}, {}
    for release in releases:
        if release['key'] <= current:
            continue
        series = release['series']
        kind = kinds.get(series)
        path = None
        if series == cur_series:
            # Within a series is always a path (Table 3.1), even for a series
            # the catalog does not classify.
            path = 'innovation' if cur_kind == 'innovation' else 'patch'
        elif kind is None:
            unclassified.setdefault(series, release)
            continue
        elif cur_kind in ('bugfix', 'lts'):
            if series == next_stop:
                path = 'next_lts'
            elif kind == 'innovation' and not superseded and (next_stop is None or _vt(series) < _vt(next_stop)):
                path = 'innovation'
        elif cur_kind == 'innovation':
            if series == next_stop:
                path = 'lts_after_innovation'
            elif kind == 'innovation' and not superseded and (prev_stop is None or _vt(series) > _vt(prev_stop)) and \
                    (next_stop is None or _vt(series) < _vt(next_stop)):
                path = 'innovation'
        if path:
            label = _PATH_LABEL[path]
            if path in ('next_lts', 'lts_after_innovation') and kinds.get(series) == 'bugfix':
                label = 'next bugfix series'
            targets.append(dict(release, path=path, path_label=label))
        else:
            newest = blocked.get(series)
            if newest is None or release['key'] > newest['key']:
                blocked[series] = release
    not_direct = []
    for series in sorted(blocked, key=_vt):
        if cur_kind is None:
            reason = 'myflames does not know the release kind of %s, so only its own series is placed on a path.' % cur_series
        elif next_stop and _vt(series) > _vt(next_stop):
            reason = 'Not a direct upgrade: go to %s first (an LTS or bugfix series cannot be skipped).' % next_stop
        elif superseded and kinds.get(series) == 'innovation' and _vt(series) < _vt(next_stop):
            reason = ('Still a supported upgrade path (MySQL manual, Table 3.1), but superseded: an Innovation release '
                      'is supported only until the next release, and the %s LTS series, a direct target, has '
                      'shipped.' % next_stop)
        else:
            reason = 'Not a supported upgrade path from %s.' % cur_series
        not_direct.append({'series': series, 'newest': blocked[series]['version'], 'reason': reason})
    return targets, not_direct, [{'series': s, 'newest': r['version'],
                                  'reason': 'myflames does not know whether %s is an LTS or Innovation series; check '
                                            "MySQL's release model before choosing it." % s}
                                 for s, r in sorted(unclassified.items(), key=lambda item: _vt(item[0]))]


def _recommended(targets):
    # Never an Innovation release by default: a server on the newest LTS
    # patch gets no suggestion and chooses.
    for path in ('next_lts', 'lts_after_innovation', 'patch'):
        found = [t for t in targets if t['path'] == path]
        if found:
            return found[-1]['version']
    return None


# Report -------------------------------------------------------------------

def read_layout(conn):
    rows = Reader(conn, TAG).rows('SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)'
                                  % ','.join(literal(name) for name in _VARS))
    return {str(row[0]).lower(): row[1] for row in rows if len(row) == 2}


def _guidance(product_flavor, install):
    method = install['method']
    if install['container']:
        prefix = 'The host name looks like a container ID: if this server runs in a container, upgrade by changing its image tag. '
    else:
        prefix = ''
    if product_flavor == 'percona':
        if method in ('rpm', 'deb'):
            rhel = (' On RHEL 8 and its rebuilds, run sudo dnf -y module disable mysql first.'
                    if method == 'rpm' else '')
            return {'text': prefix + "Enable only the target's repository with percona-release, then upgrade with the "
                            'package manager.' + rhel, 'command': 'sudo percona-release enable-only {repository} release',
                    'url': PERCONA_REPO_GUIDE}
        return {'text': prefix + 'Percona publishes each release as packages and as tarballs. The versions below are '
                        "from Percona's APT repository.", 'command': None, 'url': PERCONA_REPO_GUIDE}
    if method == 'rpm':
        return {'text': prefix + 'The MySQL Yum repository publishes the releases below; to move to another series, '
                        'enable that series in the repository first.',
                'command': None, 'url': YUM_GUIDE}
    if method == 'deb':
        return {'text': prefix + 'The MySQL APT repository publishes the releases below; to move to another series, '
                        'choose that series in its configuration first.',
                'command': None, 'url': APT_GUIDE}
    if method == 'distro_rpm':
        return {'text': prefix + 'Your distribution updates its own package only within its release; the releases '
                        'below install from the MySQL Yum repository instead.', 'command': None, 'url': YUM_GUIDE}
    if method == 'ubuntu_deb':
        return {'text': prefix + 'Ubuntu updates its own package only within its release; the releases below install '
                        'from the MySQL APT repository instead.',
                'command': None, 'url': APT_GUIDE}
    if method == 'tarball':
        kind = ' (%s)' % install['package'] if install.get('package') else ''
        return {'text': prefix + 'Download the same kind of archive%s for the target (same glibc and architecture).' % kind,
                'command': None, 'url': MYSQL_DOWNLOADS}
    return {'text': prefix + 'The install method was not recognized; the releases below are every release on the path.',
            'command': None, 'url': None}


def _shell(tools):
    entry = ((tools or {}).get('tools') or {}).get('mysqlsh') or {}
    if not entry.get('available'):
        return None
    parts = entry.get('version_tuple')
    return tuple(parts) if isinstance(parts, (list, tuple)) and len(parts) == 3 else None


def build_report(layout, mysql=None, percona=None, kinds=None, shell=None, availability=None):
    """Pure: the upgrade-targets-1.0 report from the server's variables and what was read.

    ``guidance.command`` may hold ``{repository}``: the client fills in the
    selected target's ``repository`` (Percona targets only)."""
    server = layout
    kinds = series_kinds() if kinds is None else kinds
    identity = product_identity(server.get('version', ''), server.get('version_comment', ''))
    install = install_method(layout)
    report = {'schema_version': SCHEMA, 'server': {'engine': identity['engine'], 'flavor': identity['flavor'],
                                                    'product': identity['product'], 'version': server.get('version')},
              'install': install, 'targets': [], 'recommended': None, 'not_direct': [], 'unclassified': [],
              'unavailable': [], 'notes': []}
    if identity['engine'] == 'mariadb':
        report['source'] = {'kind': 'none', 'label': 'MariaDB versions are not listed here yet; type the target.',
                            'urls': [], 'checked_at': None, 'error': None}
        report['guidance'] = {'text': '', 'command': None, 'url': None}
        return report
    upstream = version_tuple(server.get('version', ''))
    if identity['flavor'] == 'percona' and 'XtraDB Cluster' in identity['product']:
        report['source'] = {'kind': 'none', 'label': 'Percona XtraDB Cluster versions are not listed here yet; type '
                                                     'the target.', 'urls': [], 'checked_at': None, 'error': None}
        report['guidance'] = {'text': '', 'command': None, 'url': PERCONA_REPO_GUIDE}
        return report
    if identity['flavor'] == 'percona':
        m = re.match(r'\s*[0-9]+\.[0-9]+\.[0-9]+-([0-9]+)', server.get('version', ''))
        current = upstream + (int(m.group(1)) if m else 0,)
        data = percona or {}
        releases = [dict(r, series=_series(r['upstream']), key=_vt(r['upstream']) + (r['build'],),
                         check_version=r['upstream']) for r in data.get('releases') or []]
        source = {'kind': 'percona_repository' if releases else 'none',
                  'label': 'Percona repositories (%s)' % ('Yum' if install['method'] == 'rpm' else 'APT')
                  if releases else 'Could not read the Percona repositories; type the target.',
                  'urls': data.get('urls') or [], 'checked_at': data.get('checked_at'),
                  'error': data.get('error') or data.get('partial')}
    else:
        current = upstream
        data = mysql or {}
        releases = data.get('releases') or []
        source = {'kind': 'release_notes', 'label': 'MySQL release notes', 'urls': data.get('urls') or [],
                  'checked_at': data.get('checked_at'), 'error': data.get('partial')}
        if not releases:
            releases = bundled_mysql_releases()
            source = {'kind': 'bundled', 'label': 'Releases bundled with myflames (the release notes could not be read)',
                      'urls': [], 'checked_at': None, 'error': data.get('error')}
        releases = [dict(r, series=_series(r['version']), key=_vt(r['version']), check_version=r['version'])
                    for r in releases]
    unavailable = []
    if availability:
        keep = []
        for release in releases:
            state = availability.get(release.get('url'))
            if state == 'removed' or (state == 'docker_only' and not install['container']):
                unavailable.append({'version': release['version'], 'reason':
                                    'Removed from download by Oracle.' if state == 'removed' else
                                    'A Critical Security Patch Update for the MySQL Server Docker image only; '
                                    'not in the package repositories.'})
            else:
                keep.append(dict(release, docker_only=state == 'docker_only'))
        releases = keep
    targets, not_direct, unclassified = upgrade_paths(current, releases, kinds)
    for target in targets:
        check = _vt(target['check_version'])
        target['shell'] = None if shell is None else {
            'ok': upstream < shell and check[:2] <= shell[:2],
            'needs': '%d.%d' % check[:2] if check[:2] > shell[:2] else None}
        target.pop('key', None)
    report.update(source=source, targets=targets, recommended=_recommended(targets), not_direct=not_direct,
                  unclassified=unclassified, unavailable=unavailable, guidance=_guidance(identity['flavor'], install))
    if shell is not None and upstream >= shell:
        report['notes'].append('MySQL Shell %s is not newer than this server, so it cannot check any upgrade from it. '
                               'Install a newer MySQL Shell.' % '.'.join(str(n) for n in shell))
    return report


def upgrade_targets(conn, tools=None, fetch=None, deadline=None):
    """Read the server's identity and layout, then the vendor's releases (cached)."""
    # No reader means no network: only cmd_ui passes fetch_page.
    fetch = fetch or offline
    deadline = deadline or time.monotonic() + REPORT_TIMEOUT
    layout = read_layout(conn)
    if not layout.get('version'):
        raise ValueError('Could not read the server version.')
    identity = product_identity(layout['version'], layout.get('version_comment', ''))
    mysql = percona = None
    kinds = series_kinds()
    if identity['engine'] == 'mysql' and identity['flavor'] != 'percona':
        mysql = _cached(('mysql', fetch), _load_mysql(fetch), deadline)
    elif identity['flavor'] == 'percona' and 'XtraDB Cluster' not in identity['product']:
        root = _cached(('percona-root', fetch), lambda d: _load_root(fetch, d), deadline)
        lines = root.get('lines') or {}
        upstream = version_tuple(layout['version'])
        wanted = []
        for series in ['%d.%d' % upstream[:2]] + sorted({k for k in kinds if _vt(k) > upstream[:2]}, key=_vt):
            line = _line_for(lines, series)
            if line and line not in wanted:
                wanted.append(line)
        rpm = install_method(layout)['method'] == 'rpm'
        arch = 'aarch64' if layout.get('version_compile_machine', '').lower() in ('aarch64', 'arm64') else 'x86_64'
        percona = (_cached(('percona', fetch, tuple(wanted), rpm, arch), _load_percona(fetch, wanted, rpm, arch), deadline)
                   if wanted else {'error': root.get('error') or 'No Percona repository matches this server.'})
    shell = _shell(tools)
    report = build_report(layout, mysql=mysql, percona=percona, kinds=kinds, shell=shell)
    if report['source']['kind'] == 'release_notes' and report['targets']:
        urls = [t['url'] for t in reversed(report['targets']) if t.get('url')][:MAX_RELEASE_PAGES]
        urls += [n.get('url') for n in report['not_direct'] if n.get('url')]
        availability = release_availability(fetch, urls, deadline)
        report = build_report(layout, mysql=mysql, kinds=kinds, shell=shell, availability=availability)
        if len(availability) < len(set(urls)) or 'unknown' in availability.values():
            report['notes'].append('Some release pages could not be read, so a release Oracle removed or published '
                                   'only as a Docker image may still be listed.')
    return report
