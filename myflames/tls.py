"""TLS context for myflames' few HTTPS reads.

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
Identity certificates); they have no trust settings and are left out. When the
trust settings cannot be read, nothing is added, so a failure never widens
trust. Verification and host name checks stay on. ``SSL_CERT_FILE`` and
``SSL_CERT_DIR`` keep working as OpenSSL defines them.
"""

import hashlib
import os
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading

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

# SecTrustSettings result values (Security/SecTrustSettings.h).
TRUST_ROOT = 1
TRUST_AS_ROOT = 2
DENY = 3

_PEM = re.compile(r'-----BEGIN CERTIFICATE-----\r?\n.+?\r?\n-----END CERTIFICATE-----', re.S)
_LOCK = threading.Lock()
_ROOTS = None  # PEM text of the trusted roots, read once per process


def _security(argv):
    """stdout of /usr/bin/security, or None when it fails."""
    try:
        done = subprocess.run([SECURITY] + list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=EXPORT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


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


def _trust_settings():
    """(trusted, denied): upper-case SHA-1 fingerprints from every trust domain.

    trusted is None when the system domain could not be read."""
    trusted, denied = set(), set()
    tmp = tempfile.mkdtemp(prefix='myflames-trust-')
    try:
        for flags in TRUST_DOMAINS:
            path = os.path.join(tmp, 'trust%s.plist' % (flags[0] if flags else '-u'))
            if _security(('trust-settings-export',) + flags + (path,)) is None or not os.path.isfile(path):
                if flags == ('-s',):
                    return None, denied
                continue  # No admin or user trust settings is the common case.
            try:
                with open(path, 'rb') as handle:
                    listed = plistlib.load(handle).get('trustList') or {}
            except (OSError, ValueError, plistlib.InvalidFileException):
                if flags == ('-s',):
                    return None, denied
                continue
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


def _system_roots():
    """PEM text of the keychain certificates macOS trusts as roots, read once per process."""
    global _ROOTS
    with _LOCK:
        if _ROOTS is None:
            kept = []
            trusted, denied = _trust_settings()
            if trusted:
                for keychain in KEYCHAINS:
                    out = _security(('find-certificate', '-a', '-p', keychain))
                    for pem in _PEM.findall(out.decode('ascii', 'replace') if out else ''):
                        try:
                            fingerprint = hashlib.sha1(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
                        except ValueError:
                            continue
                        if fingerprint in trusted and fingerprint not in denied:
                            kept.append(pem)
            _ROOTS = '\n'.join(kept)
        return _ROOTS


def _trusts_nothing(context):
    return not context.cert_store_stats().get('x509_ca')


def https_context():
    """A verifying client context; on macOS, with the system's trusted roots when OpenSSL found none."""
    context = ssl.create_default_context()
    if sys.platform == 'darwin' and _trusts_nothing(context):
        roots = _system_roots()
        if roots:
            try:
                context.load_verify_locations(cadata=roots)
            except (ssl.SSLError, ValueError):
                pass
    return context
