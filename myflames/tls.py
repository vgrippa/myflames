"""TLS context and deadline for myflames' few HTTPS reads.

Release checks (app_update.py), tool update checks (tool_updates.py) and Home
news (news.py) are the only HTTPS clients. They verify certificates and host
names with the standard library's default context.

The packaged macOS app carries its own OpenSSL, whose built-in certificate
paths name files on the machine that built it (Homebrew's
``/opt/homebrew/etc/openssl@3/cert.pem``, or a python.org framework directory
that holds no certificates until "Install Certificates" runs). On a Mac without
those files the default context trusts no certificate authority, so every
HTTPS read would fail verification. In that case, and only on macOS, the
context also loads the roots macOS itself trusts, read with ``/usr/bin/security``
(part of macOS, called by absolute path):

* the certificates in the system root keychain and the administrator's System
  keychain (``find-certificate -a -p``), kept only when
* the system, admin or user trust settings (``trust-settings-export``) list
  them as a trusted root with no policy restriction, and no domain denies them.

An exported keychain also holds intermediates and identities that macOS does
not treat as roots (Apple's Developer ID and WWDR intermediates, the System
Identity certificates); they have no trust settings and are left out. When any
trust domain cannot be read, nothing is added, so a failure never widens trust
or drops a "Never Trust" choice: an admin or user domain counts as empty only
when its export succeeds (an empty ``trustList`` on current macOS) or reports
"No Trust Settings were found" (older macOS). A complete read is kept for the
process; a failed or partial one is tried again after ``RETRY_AFTER`` seconds.
Verification and host name checks stay on. When ``SSL_CERT_FILE`` or
``SSL_CERT_DIR`` is set, the user chose the trust store: myflames uses only
what OpenSSL loads from them and adds no macOS roots.

:class:`Watchdog` and :class:`WatchedHTTPSHandler` give a read one deadline
for everything after its TCP connection is made: at the deadline the watchdog
shuts the TCP socket and the TLS socket wrapped around it (registered before
its handshake), so a TLS handshake, headers, chunk framing or a body sent a
byte at a time cannot outlast it. The DNS lookup and the TCP connect come
before the socket exists (each address tried gets the socket timeout), and so
does building the context here, so a caller that must answer by the deadline
also bounds its own wait (app_update's worker, news's join).
"""

import hashlib
import http.client
import os
import plistlib
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

SECURITY = '/usr/bin/security'
#: The system root store, then the administrator's System keychain (where an
#: organization installs its own roots).
KEYCHAINS = (
    '/System/Library/Keychains/SystemRootCertificates.keychain',
    '/Library/Keychains/System.keychain',
)
#: trust-settings-export domains: system, admin, user (no flag).
TRUST_DOMAINS = (('-s',), ('-d',), ())
EXPORT_TIMEOUT = 10.0
#: Seconds before a failed or partial read of the macOS roots is tried again.
RETRY_AFTER = 60.0
#: OpenSSL's own trust store variables: when either is set, it is the user's choice.
TRUST_STORE_VARIABLES = ('SSL_CERT_FILE', 'SSL_CERT_DIR')
#: errSecNoTrustSettings, as older macOS prints it for an empty admin or user domain.
NO_TRUST_SETTINGS = b'No Trust Settings were found'
#: errSecItemNotFound (-25300) as an exit status: a keychain with no certificate.
ITEM_NOT_FOUND = 44

# SecTrustSettings result values (Security/SecTrustSettings.h).
TRUST_ROOT = 1
TRUST_AS_ROOT = 2
DENY = 3

_PEM = re.compile(r'-----BEGIN CERTIFICATE-----\r?\n.+?\r?\n-----END CERTIFICATE-----', re.S)
_LOCK = threading.Lock()
_ROOTS = None      # PEM text of the trusted roots from the last read (None before the first)
_RETRY_AT = None   # None when that read was complete; else when to read again (time.monotonic)


def _security(argv):
    """(returncode, stdout, stderr) of /usr/bin/security, or None when it could not run or timed out."""
    env = dict(os.environ, LC_ALL='C', LANG='C')
    try:
        done = subprocess.run([SECURITY] + list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=EXPORT_TIMEOUT, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode, done.stdout or b'', done.stderr or b''


def _entry_trusted(settings):
    """True for a trusted root with no restriction; None when the entry denies; False otherwise."""
    if settings is None or settings == []:
        return True  # No constraints: a trusted root for every policy.
    if not isinstance(settings, list):
        return False
    results, restricted = [], False
    for item in settings:
        if not isinstance(item, dict):
            return False
        results.append(item.get('kSecTrustSettingsResult', TRUST_ROOT))
        # A policy, application, host name or key usage restriction is not
        # evaluated here; such a root is left to the system.
        restricted = restricted or any(
            key not in ('kSecTrustSettingsResult', 'kSecTrustSettingsAllowedError') for key in item)
    if DENY in results:
        return None  # A deny for any policy keeps the certificate out.
    if restricted:
        return False
    return all(result in (TRUST_ROOT, TRUST_AS_ROOT) for result in results)


def _trust_list(flags, path):
    """The domain's trustList: a dict, {} for an empty admin or user domain, None when unreadable."""
    done = _security(('trust-settings-export',) + flags + (path,))
    if done is None:
        return None  # could not run, or timed out
    returncode, out, err = done
    if returncode != 0:
        # Older macOS exits non-zero for an admin or user domain with no
        # settings; any other failure might hide a deny.
        return {} if flags != ('-s',) and NO_TRUST_SETTINGS in err + out else None
    try:
        with open(path, 'rb') as handle:
            document = plistlib.load(handle)
    except Exception:  # missing file, or a plist that does not parse
        return None
    listed = document.get('trustList', {}) if isinstance(document, dict) else None
    return listed if isinstance(listed, dict) else None


def _trust_settings():
    """(trusted, denied): upper-case SHA-1 fingerprints from every trust domain.

    None when any domain could not be read: a deny there would be lost."""
    trusted, denied = set(), set()
    tmp = tempfile.mkdtemp(prefix='myflames-trust-')
    try:
        for flags in TRUST_DOMAINS:
            listed = _trust_list(flags, os.path.join(tmp, 'trust%s.plist' % (flags[0] if flags else '-u')))
            if listed is None:
                return None
            for fingerprint, entry in listed.items():
                if not isinstance(entry, dict):
                    continue
                verdict = _entry_trusted(entry.get('trustSettings'))
                if verdict is None:
                    denied.add(str(fingerprint).upper())
                elif verdict:
                    trusted.add(str(fingerprint).upper())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return trusted, denied


def _keychain_certificates(keychain):
    """PEM certificates exported from *keychain*, [] when it holds none, None when the export failed."""
    done = _security(('find-certificate', '-a', '-p', keychain))
    if done is None or done[0] not in (0, ITEM_NOT_FOUND):
        return None
    return _PEM.findall(done[1].decode('ascii', 'replace')) if done[0] == 0 else []


def _read_roots():
    """(PEM text of the keychain certificates macOS trusts as roots, whether every read succeeded)."""
    settings = _trust_settings()
    if settings is None:
        return '', False
    trusted, denied = settings
    kept, complete = [], True
    if trusted:
        for keychain in KEYCHAINS:
            pems = _keychain_certificates(keychain)
            if pems is None:
                complete = False  # the other keychain's roots still help this read
                continue
            for pem in pems:
                try:
                    fingerprint = hashlib.sha1(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
                except ValueError:
                    continue
                if fingerprint in trusted and fingerprint not in denied:
                    kept.append(pem)
    return '\n'.join(kept), complete


def _system_roots():
    """PEM text of the keychain certificates macOS trusts as roots.

    A complete read is kept for the process. After a failed or partial one
    (a timed out or failing ``security``) the same answer is returned until
    RETRY_AFTER seconds have passed, then the roots are read again."""
    global _ROOTS, _RETRY_AT
    with _LOCK:
        if _ROOTS is None or (_RETRY_AT is not None and time.monotonic() >= _RETRY_AT):
            roots, complete = _read_roots()
            _ROOTS = roots
            _RETRY_AT = None if complete else time.monotonic() + RETRY_AFTER
        return _ROOTS


def _trust_store_chosen():
    """True when SSL_CERT_FILE or SSL_CERT_DIR is set (even empty, as OpenSSL reads them)."""
    return any(name in os.environ for name in TRUST_STORE_VARIABLES)


def _trusts_nothing(context):
    return not context.cert_store_stats().get('x509_ca')


def https_context():
    """A verifying client context; on macOS, with the system's trusted roots when OpenSSL found none.

    OpenSSL reads an SSL_CERT_DIR directory only on lookup, so a context that
    trusts it still counts no CA here: the fallback never runs while either
    variable is set."""
    context = ssl.create_default_context()
    if sys.platform == 'darwin' and not _trust_store_chosen() and _trusts_nothing(context):
        roots = _system_roots()
        if roots:
            try:
                context.load_verify_locations(cadata=roots)
            except (ssl.SSLError, ValueError):
                pass
    return context


# ---- Deadline -------------------------------------------------------------

class Watchdog:
    """Shuts a read's sockets down at its deadline (a time.monotonic value),
    so nothing on a connected socket (the TLS handshake, response headers,
    chunk framing, a body sent a byte at a time) outlives it. The DNS lookup
    and the TCP connect are outside its reach: a socket is held once it is
    connected, and one connected after the deadline is shut at once.
    ``expired`` tells the reader that a cut answer is not the answer."""

    def __init__(self, deadline):
        self._lock = threading.Lock()
        self._sockets = []
        self.expired = False
        self._timer = threading.Timer(max(0.0, deadline - time.monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock):
        """Hold *sock* until the deadline; one registered after it is shut at once."""
        with self._lock:
            self._sockets.append(sock)
            expired = self.expired
        if expired:
            self._shut(sock)

    def _expire(self):
        with self._lock:
            self.expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            self._shut(sock)

    @staticmethod
    def _shut(sock):
        # The base class shutdown acts on the descriptor only. SSLSocket's own
        # would also drop its TLS state under the thread still reading it. A
        # TCP socket wrapped in TLS is detached (no descriptor): nothing to do.
        try:
            if sock.fileno() >= 0:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass

    def stop(self):
        self._timer.cancel()


def watched_connection_class(watchdog):
    """HTTPSConnection whose TCP socket, then its TLS socket, the watchdog holds."""
    class Connection(http.client.HTTPSConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            create = self._create_connection

            def watched(*args, **kwargs):
                sock = create(*args, **kwargs)
                watchdog.watch(sock)
                return sock
            self._create_connection = watched

        def connect(self):
            # HTTPSConnection.connect, with the TLS socket watched before its
            # handshake: wrapping detaches the TCP socket the watchdog held.
            http.client.HTTPConnection.connect(self)
            sock = self._context.wrap_socket(self.sock, server_hostname=self._tunnel_host or self.host,
                                             do_handshake_on_connect=False)
            self.sock = sock
            watchdog.watch(sock)
            sock.do_handshake()
    return Connection


class WatchedHTTPSHandler(urllib.request.HTTPSHandler):
    """An HTTPS handler whose every connection *watchdog* holds."""

    def __init__(self, watchdog, context):
        super().__init__(context=context)
        self._watchdog = watchdog

    def https_open(self, req):
        return self.do_open(watched_connection_class(self._watchdog), req, context=self._context)
