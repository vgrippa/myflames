"""Optional, bounded release awareness from official vendor download metadata.

No installation or server access occurs here. URLs are built from constants and
validated version tuples; feed-provided links are never followed. The worker
limit and caller deadline also bound DNS stalls, beyond socket timeouts.
"""
import datetime
import json
import http.client
import re
import threading
import time
import urllib.error
import urllib.request

from .tls import https_context

MYSQL_DOWNLOAD = 'https://dev.mysql.com/downloads/mysql/'
SHELL_DOWNLOAD = 'https://dev.mysql.com/downloads/shell/'
MARIA_DOWNLOAD = 'https://mariadb.org/download/'
MARIA_API = 'https://downloads.mariadb.org/rest-api/mariadb/'
PUMP_DOCUMENTATION = 'https://dev.mysql.com/doc/refman/8.0/en/mysqlpump.html'
MAX_BYTES = 1024 * 1024
REQUEST_TIMEOUT = 5.0
CHECK_TIMEOUT = 12.0
SUCCESS_TTL = 3600.0
FAILURE_TTL = 60.0
MAX_CACHE = 64
MAX_WORKERS = 8
_CACHE = {}
_INFLIGHT = {}
_LOCK = threading.Lock()
_VERSION = r'(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})\.(?:0|[1-9][0-9]{0,3})'


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Release source redirected; check the official download page.')


def _version(value):
    if not isinstance(value, str) or re.fullmatch(_VERSION, value) is None:
        raise ValueError('Unrecognized release version.')
    return tuple(int(x) for x in value.split('.'))


def _fetch(url):
    # No ambient proxy authentication, cookies, or arbitrary redirect targets.
    if (url not in (SHELL_DOWNLOAD, MARIA_API) and
            re.fullmatch(re.escape(MYSQL_DOWNLOAD) + r'\?version=[0-9]{1,4}\.[0-9]{1,4}', url) is None and
            re.fullmatch(re.escape(MARIA_API) + r'[0-9]{1,4}\.[0-9]{1,4}/latest/', url) is None):
        raise ValueError('Unsupported release source.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=https_context()), _NoRedirect())
    request = urllib.request.Request(url, headers={'Accept-Encoding': 'identity'})
    with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
        if response.geturl() != url or response.status != 200:
            raise ValueError('Unexpected release source response.')
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('Release source exceeded the size limit.')
    return body.decode('utf-8')


def _release(family, series):
    if family == 'mariadb':
        catalog = json.loads(_fetch(MARIA_API))
        rows = catalog.get('major_releases')
        if not isinstance(rows, list) or not any(isinstance(row, dict) and
                row.get('release_id') == series and row.get('release_status') == 'Stable' for row in rows):
            raise ValueError('Installed series is not listed as stable by the vendor.')
        document = json.loads(_fetch(MARIA_API + series + '/latest/'))
        releases = document.get('releases')
        if not isinstance(releases, dict) or not releases:
            raise ValueError('Release source has no release data.')
        versions = []
        today = datetime.datetime.now(datetime.timezone.utc).date()
        for label, release in releases.items():
            version = _version(label)
            if not isinstance(release, dict) or release.get('release_id') != label or '.'.join(label.split('.')[:2]) != series:
                raise ValueError('Release source returned a different series.')
            date = release.get('date_of_release', '')
            if not isinstance(date, str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', date) is None:
                raise ValueError('Release date is unavailable.')
            if datetime.datetime.strptime(date, '%Y-%m-%d').date() > today:
                raise ValueError('Release source returned a future release.')
            if not isinstance(release.get('files'), list) or not release['files']:
                raise ValueError('Release source has no downloadable files.')
            versions.append(version)
        return max(versions)
    url = SHELL_DOWNLOAD if family == 'shell' else MYSQL_DOWNLOAD + '?version=' + series
    body = _fetch(url)
    if 'General Availability (GA) Releases' not in body:
        raise ValueError('The official page did not identify GA releases.')
    product = 'MySQL Shell' if family == 'shell' else 'MySQL Community Server'
    headings = re.findall(r'<h1\b[^>]*>\s*' + product + r'\s+(' + _VERSION + r')\s*(?:LTS|Innovation)?\s*</h1>', body, re.I)
    if len(headings) != 1:
        raise ValueError('The official download version could not be verified.')
    version = _version(headings[0])
    if family != 'shell' and '%d.%d' % version[:2] != series:
        raise ValueError('The official download page returned a different series.')
    return version


def _load(key, event):
    try:
        try:
            latest = _release(*key)
            result = (latest, None, datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
            ttl = SUCCESS_TTL
        except (OSError, ValueError, TypeError, AttributeError, KeyError, urllib.error.URLError, http.client.HTTPException, RecursionError):
            result = (None, 'Could not verify the official release. Open the download page to check.',
                      datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
            ttl = FAILURE_TTL
        with _LOCK:
            if len(_CACHE) >= MAX_CACHE:
                del _CACHE[min(_CACHE, key=lambda item: _CACHE[item][0])]
            _CACHE[key] = (time.monotonic() + ttl, result)
    finally:
        with _LOCK:
            _INFLIGHT.pop(key, None)
        event.set()


def _cached(key, deadline):
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        if time.monotonic() >= deadline:
            return None, 'Release check timed out. Open the download page to check.', None
        event = _INFLIGHT.get(key)
        if event is None:
            if len(_INFLIGHT) >= MAX_WORKERS:
                return None, 'Release checks are busy. Try again shortly.', None
            event = threading.Event()
            _INFLIGHT[key] = event
            threading.Thread(target=_load, args=(key, event), daemon=True).start()
    if not event.wait(max(0, deadline - time.monotonic())):
        return None, 'Release check timed out. Open the download page to check.', None
    with _LOCK:
        cached = _CACHE.get(key)
        return cached[1] if cached and cached[0] > time.monotonic() else (None, 'Release check failed.', None)


def enrich_tools(report, check_updates=False):
    """Attach links on every call; online checks require explicit opt-in."""
    deadline = time.monotonic() + CHECK_TIMEOUT
    for name, entry in report.get('tools', {}).items():
        family = ('shell' if name == 'mysqlsh' else 'mariadb' if
                  name.startswith('mariadb') or entry.get('engine') == 'mariadb' else 'mysql')
        download = SHELL_DOWNLOAD if family == 'shell' else MARIA_DOWNLOAD if family == 'mariadb' else MYSQL_DOWNLOAD
        entry['download_url'] = PUMP_DOCUMENTATION if name == 'mysqlpump' else download
        update = {'status': 'not_checked', 'latest_version': None, 'checked_at': None,
                  'source_url': download, 'message': 'Update check not requested.'}
        entry['update'] = update
        if name == 'mysqlpump' or entry.get('deprecated'):
            update.update(status='deprecated', source_url=PUMP_DOCUMENTATION, message='Deprecated tool. Use MySQL Shell or mysqldump.')
            continue
        if not entry.get('available'):
            update.update(status='unknown' if entry.get('path') else 'not_installed',
                          message='Installed version could not be verified.' if entry.get('path') else 'Tool not found. Open the official download page.')
            continue
        if not check_updates:
            continue
        try:
            parts = entry.get('version_tuple')
            if not isinstance(parts, (list, tuple)) or len(parts) != 3 or any(type(x) is not int or x < 0 or x > 9999 for x in parts):
                raise ValueError('Unrecognized installed version.')
            installed = tuple(parts)
            if installed == (0, 0, 0) or entry.get('engine') not in ('mysql', 'mariadb'):
                raise ValueError('Unrecognized installed version.')
            banner = str(entry.get('version', '')).lower()
            if re.search(r'percona|commercial|enterprise|\b(?:(?:alpha|beta|rc|preview)[0-9]*|trunk|development)\b', banner):
                raise ValueError('This distribution or prerelease is not covered by community GA checks.')
            series = '%d.%d' % installed[:2]
            update['source_url'] = (SHELL_DOWNLOAD if family == 'shell' else MARIA_API + series + '/latest/' if family == 'mariadb' else MYSQL_DOWNLOAD + '?version=' + series)
            latest, error, checked = _cached((family, '' if family == 'shell' else series), deadline)
            update['checked_at'] = checked
            if error:
                raise ValueError(error)
            update['latest_version'] = '%d.%d.%d' % latest
            if installed > latest:
                raise ValueError('Installed version is newer than the verified download. Check its distribution.')
            update.update(status='available' if latest > installed else 'current',
                          message='A newer GA release is available.' if latest > installed else
                          'No newer GA release found.' if family == 'shell' else 'No newer release found in the installed series.')
        except ValueError as exc:
            update.update(status='unknown', message=str(exc))
    return report
