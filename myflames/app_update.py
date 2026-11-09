"""Application update check and binary update management.

Queries official GitHub release metadata for myflames and handles downloading
update assets. Stdlib only; safe error handling. Both reads use one opener
shape (as tool_updates.py does): HTTPS only, no proxy from the environment,
and every redirect checked against the official GitHub addresses before it is
followed, its body closed unread. The check reads at most 1 MiB and answers
within CHECK_DEADLINE seconds: it runs in a worker thread the caller waits for
at most that long, and tls.Watchdog shuts the worker's sockets at the same
deadline. The download reads at most 150 MiB with a per-read timeout and a
DOWNLOAD_DEADLINE watchdog covering its TLS handshake, headers and body.
"""
import json
import os
import re
import socket
import ssl
import string
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from . import __version__
from .tls import Watchdog, WatchedHTTPSHandler, https_context

GITHUB_RELEASES_API = "https://api.github.com/repos/vgrippa/myflames/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/vgrippa/myflames/releases"
ASSET_HOSTS = ("objects.githubusercontent.com", "release-assets.githubusercontent.com")
REQUEST_TIMEOUT = 5.0   # connect, and each read of the socket
CHECK_DEADLINE = 10.0   # the caller's wait for the whole check; its sockets are shut then
DOWNLOAD_DEADLINE = 300.0  # total network-read budget, including redirects
MAX_RELEASE_BYTES = 1024 * 1024
CACHE_TTL = 300.0  # 5 minutes in-memory cache

_CACHE = {}
_LOCK = threading.Lock()
_WORKER = None  # (thread, outcome, deadline) of the latest check's read
_VERSION_REGEX = r'^(?:v)?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-(.+))?$'


def parse_version_tuple(version_str):
    if not isinstance(version_str, str):
        return (0, 0, 0)
    match = re.match(_VERSION_REGEX, version_str.strip())
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer_version(latest_str, current_str):
    return parse_version_tuple(latest_str) > parse_version_tuple(current_str)


def detect_system_target():
    platform = sys.platform
    import platform as plt
    machine = plt.machine().lower()
    return platform, machine


def match_asset_for_platform(assets, platform, machine):
    if not isinstance(assets, list):
        return None
    if platform == "darwin":
        systems = ("mac", "macos", "darwin", "apple")
    elif platform.startswith("linux"):
        systems = ("linux",)
    elif platform in ("win32", "cygwin"):
        systems = ("win", "windows")
    else:
        return None
    machine = machine.lower()
    if machine in ("arm64", "aarch64"):
        architectures = ("arm64", "aarch64")
    elif machine in ("x86_64", "amd64", "x64"):
        architectures = ("x86_64", "amd64", "x64")
    else:
        return None
    if platform == "darwin":
        architectures += ("universal",)

    def named(name, labels):
        # Match whole labels: "darwin" must never count as "win".
        return re.search(r"(?<![a-z0-9])(?:" + "|".join(labels) + r")(?![a-z0-9])", name)

    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            continue
        name = asset.get("name", "").lower()
        if (named(name, systems) and named(name, architectures)
                and name.endswith((".zip", ".tar.gz", ".dmg", ".exe"))):
            return asset
    return None


def _utc_now():
    """The current time as an ISO 8601 UTC string with a Z suffix."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _CheckDeadline(socket.timeout):
    """The whole check outlasted CHECK_DEADLINE."""


def _failure_reason(exc):
    """A short, plain reason for a failed check (never a raw traceback)."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return "GitHub limited requests, try again later"
        if exc.code == 404:
            return "no published release was found"
        return "GitHub answered with status %d" % exc.code
    reason = getattr(exc, "reason", exc)
    # Before ValueError: a certificate failure is also a ValueError.
    if isinstance(exc, ssl.SSLCertVerificationError) or isinstance(reason, ssl.SSLCertVerificationError):
        return "a secure connection to GitHub could not be verified"
    if isinstance(exc, _CheckDeadline):
        return "GitHub took longer than %d seconds to answer" % int(CHECK_DEADLINE)
    if isinstance(exc, socket.timeout) or isinstance(reason, socket.timeout):
        return "GitHub did not answer in %d seconds" % int(REQUEST_TIMEOUT)
    if isinstance(exc, _InvalidUpdateURL):
        return "GitHub sent the check to an unexpected address"
    if isinstance(exc, ValueError):
        return "the release information was unreadable"
    return "GitHub could not be reached"


def _failed_result(reason):
    return {
        "schema_version": "app-update-1.0",
        "current_version": __version__,
        "latest_version": __version__,
        "update_available": False,
        "release_title": "myflames v%s" % __version__,
        "release_notes": "",
        "published_at": "",
        "html_url": GITHUB_RELEASES_URL,
        "asset_name": None,
        "asset_url": None,
        "asset_size": None,
        "checked_at": _utc_now(),
        "reason": reason,
        "error": "Could not check for updates: %s." % reason,
    }


def check_for_updates(force=False):
    """Ask GitHub for the latest release (app-update-1.0).

    Runs only when the workspace asks for it (the Check for updates button);
    one bounded HTTPS read of the official releases API. ``checked_at`` is
    when the release metadata was read, so a cached result keeps its time.
    A failed check is never cached and carries ``error`` and ``reason``."""
    now = time.time()
    with _LOCK:
        if not force and "result" in _CACHE and (now - _CACHE["timestamp"]) < CACHE_TTL:
            return dict(_CACHE["result"])

    test_override = os.environ.get("MYFLAMES_TEST_UPDATE")
    if test_override:
        simulated = {
            "schema_version": "app-update-1.0",
            "current_version": __version__,
            "latest_version": test_override,
            "update_available": is_newer_version(test_override, __version__),
            "release_title": "v%s: Simulated Test Release" % test_override,
            "release_notes": "Simulated release notes for testing binary update.",
            "published_at": "2026-10-19T12:00:00Z",
            "html_url": "https://github.com/vgrippa/myflames/releases/tag/v" + test_override,
            "asset_name": "myflames-mac-arm64.zip",
            "asset_url": "https://github.com/vgrippa/myflames/releases/download/v%s/myflames-mac-arm64.zip" % test_override,
            "asset_size": 24000000,
            "checked_at": _utc_now(),
        }
        with _LOCK:
            _CACHE["result"] = simulated
            _CACHE["timestamp"] = now
        return dict(simulated)

    try:
        body = _read_release()
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("release metadata is not an object")
        tag = data.get("tag_name")
        if not isinstance(tag, str) or not re.match(_VERSION_REGEX, tag.strip()):
            raise ValueError("release has no version tag")
    except Exception as exc:
        # Offline, rate limited or unreadable: report it, never as up to date.
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        return _failed_result(_failure_reason(exc))

    tag_name = tag.strip().lstrip("v")
    is_newer = is_newer_version(tag_name, __version__)
    plat, mach = detect_system_target()
    matched = match_asset_for_platform(data.get("assets", []), plat, mach)

    def text(key):
        value = data.get(key)
        return value if isinstance(value, str) else ""

    result = {
        "schema_version": "app-update-1.0",
        "current_version": __version__,
        "latest_version": tag_name,
        "update_available": is_newer,
        "release_title": text("name") or text("tag_name") or ("v" + tag_name),
        "release_notes": text("body"),
        "published_at": text("published_at"),
        "html_url": text("html_url") or GITHUB_RELEASES_URL,
        "asset_name": matched.get("name") if matched else None,
        "asset_url": matched.get("browser_download_url") if matched else None,
        "asset_size": matched.get("size") if matched else None,
        "checked_at": _utc_now(),
    }

    with _LOCK:
        _CACHE["result"] = result
        _CACHE["timestamp"] = now

    return dict(result)


def _opener(validate, watchdog=None):
    """HTTPS only, no proxy from the environment, and every redirect checked
    with *validate* before it is followed (tool_updates.py builds the same).
    With *watchdog*, every connection is shut at its deadline."""
    https = (WatchedHTTPSHandler(watchdog, https_context()) if watchdog is not None
             else urllib.request.HTTPSHandler(context=https_context()))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), https, _ReleaseRedirectHandler(validate))


def _read_release():
    """The latest release's metadata: at most MAX_RELEASE_BYTES, within CHECK_DEADLINE.

    The read runs in a worker thread and the caller waits for it at most
    until the deadline, so nothing in it keeps the caller longer: building the
    TLS context (which may read the macOS roots with /usr/bin/security), the
    DNS lookup or a TCP connect. A read still running then ends in the
    background (the watchdog shuts its sockets; the other steps have their
    own timeouts). No second read starts while it runs: a check meanwhile
    waits for it until its deadline, or reports the deadline at once."""
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER[0].is_alive():
            deadline = time.monotonic() + CHECK_DEADLINE
            outcome = {}
            worker = threading.Thread(target=_read_into, args=(outcome, deadline),
                                      name="myflames-update-check", daemon=True)
            _WORKER = (worker, outcome, deadline)
            worker.start()
        worker, outcome, deadline = _WORKER
    worker.join(max(0.0, deadline - time.monotonic()))
    if "body" in outcome:
        return outcome["body"]
    if "error" in outcome:
        raise outcome["error"]
    raise _CheckDeadline("The update check took too long.")


def _read_into(outcome, deadline):
    """The worker: the release metadata in ``outcome["body"]``, or the failure in ``outcome["error"]``."""
    try:
        outcome["body"] = _fetch_release(deadline)
    except Exception as exc:
        outcome["error"] = exc


def _fetch_release(deadline):
    """Read the release metadata: at most MAX_RELEASE_BYTES, the sockets shut at *deadline*."""
    watchdog = Watchdog(deadline)
    request = urllib.request.Request(
        GITHUB_RELEASES_API,
        headers={"User-Agent": "myflames/%s" % __version__, "Accept": "application/vnd.github.v3+json"})
    try:
        with _opener(_validate_api_url, watchdog).open(request, timeout=REQUEST_TIMEOUT) as response:
            _validate_api_url(response.geturl())
            status = getattr(response, "status", None)
            status = status if isinstance(status, int) else response.getcode()
            if status != 200:
                raise ValueError("GitHub answered with status %s" % status)
            chunks, size = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise _CheckDeadline("The update check took too long.")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RELEASE_BYTES:
                    raise ValueError("The release information exceeded 1 MiB.")
                chunks.append(chunk)
    except Exception as exc:
        if watchdog.expired:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            raise _CheckDeadline("The update check took too long.")
        raise
    finally:
        watchdog.stop()
    if watchdog.expired:  # an answer cut by the shutdown is not the release
        raise _CheckDeadline("The update check took too long.")
    return b"".join(chunks)


def _valid_checked_at(value):
    """An ISO 8601 UTC time as check_for_updates writes it (a real date)."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", value):
        return False
    try:
        time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def last_check_record(result):
    """The part of a check result kept on this computer (app-update-1.0
    ``last_check``): when, what was found, and why it failed. Never the
    release notes or download URLs; those come only from a fresh check."""
    if not isinstance(result, dict):
        raise ValueError("Expected an update check result.")
    checked_at = result.get("checked_at")
    if not _valid_checked_at(checked_at):
        raise ValueError("An update check result needs checked_at.")
    error = result.get("error")
    if error:
        reason = result.get("reason") if isinstance(result.get("reason"), str) else str(error)
        return {"checked_at": checked_at, "outcome": "failed", "current_version": __version__,
                "latest_version": None, "reason": reason[:200]}
    latest = result.get("latest_version")
    if not isinstance(latest, str) or not re.match(_VERSION_REGEX, latest):
        raise ValueError("An update check result needs latest_version.")
    newer = is_newer_version(latest, __version__)
    return {"checked_at": checked_at, "outcome": "available" if newer else "up_to_date",
            "current_version": __version__, "latest_version": latest, "reason": None}


def stored_last_check(record):
    """A stored ``last_check`` record when it is well formed and was made by
    this version of myflames; None otherwise (an upgrade makes it moot)."""
    if not isinstance(record, dict) or record.get("current_version") != __version__:
        return None
    if record.get("outcome") not in ("up_to_date", "available", "failed"):
        return None
    if not _valid_checked_at(record.get("checked_at")):
        return None
    keys = ("checked_at", "outcome", "current_version", "latest_version", "reason")
    return {key: record.get(key) for key in keys}


class _InvalidUpdateURL(ValueError):
    """A refused URL must not fall back to opening it in a browser."""


def _https_url(url, message):
    """(parsed, unquoted path) of a plain HTTPS URL on port 443, without
    credentials, a fragment, control characters or dot segments."""
    if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise _InvalidUpdateURL(message)
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname and parsed.port in (None, 443)
                 and parsed.username is None and parsed.password is None and not parsed.fragment)
    except ValueError:
        raise _InvalidUpdateURL(message)
    path = urllib.parse.unquote(parsed.path)
    if (not valid or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part in (".", "..") for part in path.split("/"))):
        raise _InvalidUpdateURL(message)
    return parsed, path


def _validate_api_url(url):
    """The release API and the redirects GitHub sends for it when a repository
    is renamed or transferred: HTTPS on api.github.com, at
    ``/repos/<owner>/<name>/releases/latest`` or
    ``/repositories/<id>/releases/latest``."""
    message = "GitHub sent the update check outside its release API."
    parsed, path = _https_url(url, message)
    if (parsed.hostname == urllib.parse.urlsplit(GITHUB_RELEASES_API).hostname
            and re.fullmatch(r"/(?:repos/[^/]+/[^/]+|repositories/[0-9]+)/releases/latest", path)):
        return parsed
    raise _InvalidUpdateURL(message)


def _validate_update_url(url, asset=True, initial=False):
    """Restrict downloads and every redirect to the official release sources.

    *initial* is the first URL of a download: only the project's own
    ``github.com/<repo>/releases/download/<tag>/<name>`` path qualifies. The
    asset hosts serve every repository's release files, so they are accepted
    only as a redirect target or as the final response URL."""
    message = "Use an official HTTPS myflames release URL."
    parsed, path = _https_url(url, message)
    if asset and not initial and parsed.hostname in ASSET_HOSTS and path not in ("", "/"):
        return parsed
    releases_path = urllib.parse.urlsplit(GITHUB_RELEASES_URL).path
    if parsed.hostname == "github.com":
        if asset:
            prefix = releases_path + "/download/"
            parts = path[len(prefix):].split("/") if path.startswith(prefix) else []
            if len(parts) == 2 and all(parts):
                return parsed
        elif (path in (releases_path, releases_path + "/", releases_path + "/latest")
              or path.startswith(releases_path + "/tag/") and path[len(releases_path + "/tag/"):]):
            return parsed
    raise _InvalidUpdateURL(message)


class _ReleaseRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to an address *validate* accepts, and closes
    the redirect response without reading its body: urllib's own handler
    reads that body whole, with no size limit, before it follows
    (news._Redirects closes it the same way). urllib's limits on repeated
    addresses and on the number of redirects still apply."""

    def __init__(self, validate=None):
        self._validate = validate or _validate_update_url

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if location is None:
            return None  # nowhere to go: urllib raises HTTPError for the answer
        try:
            # urllib rejects schemes such as file: before redirect_request.
            # Check here too so every unsafe Location raises the same plain ValueError.
            self._validate(urllib.parse.urljoin(req.full_url, location))
            # As urllib does: http.client decoded the header as ISO-8859-1, so
            # percent-encode those bytes (and any space) before resolving.
            newurl = urllib.parse.urljoin(req.full_url, urllib.parse.quote(
                location, encoding="iso-8859-1", safe=string.punctuation))
            new = self.redirect_request(req, fp, code, msg, headers, newurl)
            if new is None:
                return None
            visited = getattr(req, "redirect_dict", None)
            if visited is None:
                visited = req.redirect_dict = {}
            elif visited.get(newurl, 0) >= self.max_repeats or len(visited) >= self.max_redirections:
                raise urllib.error.HTTPError(req.full_url, code, self.inf_msg + msg, headers, fp)
            new.redirect_dict = visited
            visited[newurl] = visited.get(newurl, 0) + 1
        finally:
            fp.close()  # never read: a redirect's body can be any size
        return self.parent.open(new, timeout=req.timeout)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._validate(newurl)
        return super(_ReleaseRedirectHandler, self).redirect_request(req, fp, code, msg, headers, newurl)


def _checked_asset(asset_url):
    """The size of *asset_url* from the last update check, or None.

    When a check found a release asset, only that asset may be downloaded:
    a different client URL is refused, never substituted."""
    with _LOCK:
        result = _CACHE.get("result")
    expected = result.get("asset_url") if isinstance(result, dict) else None
    if not expected:
        return None
    if asset_url != expected:
        raise _InvalidUpdateURL("This is not the release asset the last update check found. "
                                "Check for updates again.")
    size = result.get("asset_size")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return None
    return size


def _content_length(resp):
    """The response's declared Content-Length as an int, or None."""
    headers = getattr(resp, "headers", None)
    value = headers.get("Content-Length") if headers is not None and hasattr(headers, "get") else None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value.strip()):
        return None
    return int(value.strip())


def _downloads_directory():
    """Where a downloaded update is saved. The desktop app runs the backend
    with an isolated HOME, so it names the user's real Downloads folder in
    MYFLAMES_DOWNLOADS_DIR (an absolute, existing directory); otherwise
    ~/Downloads, else the temporary directory."""
    named = os.environ.get("MYFLAMES_DOWNLOADS_DIR", "")
    if named and os.path.isabs(named) and os.path.isdir(named):
        return Path(named)
    downloads_dir = Path.home() / "Downloads"
    if downloads_dir.is_dir():
        return downloads_dir
    return Path(os.environ.get("TMPDIR", "/tmp"))


def download_binary_update(asset_url=None, release_url=None):
    """Download update asset or return release page url for user."""
    if release_url:
        _validate_update_url(release_url, asset=False)
    parsed = _validate_update_url(asset_url, initial=True) if asset_url else None
    expected_size = _checked_asset(asset_url) if asset_url else None
    target_url = release_url or GITHUB_RELEASES_URL + "/latest"
    if not asset_url:
        import webbrowser
        try:
            webbrowser.open(target_url)
        except Exception:
            pass
        return {
            "status": "release_page",
            "message": "Opened release page: %s" % target_url,
            "html_url": target_url,
        }

    # Bounded download of asset into user's Downloads or temp directory
    downloads_dir = _downloads_directory()

    filename = urllib.parse.unquote(parsed.path).rsplit("/", 1)[-1]
    if not filename or len(filename) > 100 or any(c in filename for c in '\\/:*?"<>|'):
        filename = "myflames-update.zip"

    temporary = None
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    watchdog = Watchdog(deadline)
    try:
        req = urllib.request.Request(asset_url, headers={"User-Agent": "myflames/%s" % __version__})
        max_bytes = 150 * 1024 * 1024  # 150 MiB limit
        downloaded = 0
        with _opener(_validate_update_url, watchdog).open(req, timeout=15.0) as resp:
            _validate_update_url(resp.geturl())
            fd, temporary = tempfile.mkstemp(prefix=".myflames-update-", suffix=".part", dir=str(downloads_dir))
            with os.fdopen(fd, "wb") as out:
                while True:
                    if watchdog.expired or time.monotonic() >= deadline:
                        raise socket.timeout("The update download took too long.")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ValueError("Update binary exceeded 150 MiB limit.")
                    out.write(chunk)
            # A watchdog shutdown may look like a clean EOF without a
            # Content-Length. Never publish a response cut at the deadline.
            if watchdog.expired or time.monotonic() >= deadline:
                raise socket.timeout("The update download took too long.")
            # http.client returns b"" at an early EOF even when Content-Length
            # promised more (and TLS ragged EOFs are suppressed), so an empty
            # read alone does not mean the file is complete.
            remaining = getattr(resp, "length", None)
            declared = _content_length(resp)
            if ((isinstance(remaining, int) and not isinstance(remaining, bool) and remaining > 0)
                    or (declared is not None and downloaded < declared)
                    or (declared is None and expected_size is not None and downloaded < expected_size)):
                raise ValueError("The download ended early.")
            if any(size is not None and downloaded != size for size in (declared, expected_size)):
                raise ValueError("The download size does not match the release asset.")
        stem, extension = os.path.splitext(filename)
        watchdog.stop()
        if watchdog.expired or time.monotonic() >= deadline:
            raise socket.timeout("The update download took too long.")
        if filename.endswith(".tar.gz"):
            stem, extension = filename[:-7], ".tar.gz"
        dest_file = downloads_dir / filename
        index = 0
        while True:
            try:
                # A hard link publishes the completed file atomically and
                # refuses existing files, including symlinks and racing writers.
                os.link(temporary, str(dest_file))
                break
            except FileExistsError:
                index += 1
                dest_file = downloads_dir / ("%s (%d)%s" % (stem, index, extension))
        return {
            "status": "downloaded",
            "download_path": str(dest_file),
            "message": "Update downloaded to %s" % dest_file,
            "html_url": target_url,
        }
    except _InvalidUpdateURL:
        raise
    except Exception as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        if watchdog.expired:
            exc = socket.timeout("The update download took too long.")
        watchdog.stop()
        import webbrowser
        try:
            webbrowser.open(target_url)
        except Exception:
            pass
        return {
            "status": "fallback_browser",
            "message": "Could not download directly (%s). Opened release page." % exc,
            "html_url": target_url,
        }
    finally:
        watchdog.stop()
        if temporary is not None:
            os.unlink(temporary)
