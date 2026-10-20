"""Application update check and binary update management.

Queries official GitHub release metadata for myflames and handles downloading
update assets. Stdlib only; bounded network timeouts; safe error handling.
"""
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from . import __version__

GITHUB_RELEASES_API = "https://api.github.com/repos/vgrippa/myflames/releases/latest"
GITHUB_RELEASES_URL = "https://github.com/vgrippa/myflames/releases"
ASSET_HOSTS = ("objects.githubusercontent.com", "release-assets.githubusercontent.com")
REQUEST_TIMEOUT = 5.0
CACHE_TTL = 300.0  # 5 minutes in-memory cache

_CACHE = {}
_LOCK = threading.Lock()
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


def _failure_reason(exc):
    """A short, plain reason for a failed check (never a raw traceback)."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (403, 429):
            return "GitHub limited requests, try again later"
        if exc.code == 404:
            return "no published release was found"
        return "GitHub answered with status %d" % exc.code
    if isinstance(exc, socket.timeout) or isinstance(getattr(exc, "reason", None), socket.timeout):
        return "GitHub did not answer in %d seconds" % int(REQUEST_TIMEOUT)
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
        req = urllib.request.Request(
            GITHUB_RELEASES_API,
            headers={"User-Agent": "myflames/%s" % __version__, "Accept": "application/vnd.github.v3+json"}
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200:
                raise ValueError("GitHub answered with status %d" % response.status)
            body = response.read(1024 * 1024)
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


def _validate_update_url(url, asset=True, initial=False):
    """Restrict downloads and every redirect to the official release sources.

    *initial* is the first URL of a download: only the project's own
    ``github.com/<repo>/releases/download/<tag>/<name>`` path qualifies. The
    asset hosts serve every repository's release files, so they are accepted
    only as a redirect target or as the final response URL."""
    message = "Use an official HTTPS myflames release URL."
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
    def http_error_302(self, req, fp, code, msg, headers):
        # urllib rejects schemes such as file: before redirect_request. Check
        # here too so every unsafe Location raises the same plain ValueError.
        location = headers.get("location") or headers.get("uri")
        if location is not None:
            try:
                _validate_update_url(urllib.parse.urljoin(req.full_url, location))
            except _InvalidUpdateURL:
                fp.close()
                raise
        return super(_ReleaseRedirectHandler, self).http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            _validate_update_url(newurl)
        except _InvalidUpdateURL:
            fp.close()
            raise
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
    target_url = asset_url or release_url or GITHUB_RELEASES_URL + "/latest"
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
    try:
        req = urllib.request.Request(asset_url, headers={"User-Agent": "myflames/%s" % __version__})
        max_bytes = 150 * 1024 * 1024  # 150 MiB limit
        downloaded = 0
        opener = urllib.request.build_opener(_ReleaseRedirectHandler())
        with opener.open(req, timeout=15.0) as resp:
            _validate_update_url(resp.geturl())
            fd, temporary = tempfile.mkstemp(prefix=".myflames-update-", suffix=".part", dir=str(downloads_dir))
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise ValueError("Update binary exceeded 150 MiB limit.")
                    out.write(chunk)
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
            "html_url": release_url or target_url,
        }
    except _InvalidUpdateURL:
        raise
    except Exception as exc:
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
        if temporary is not None:
            os.unlink(temporary)
