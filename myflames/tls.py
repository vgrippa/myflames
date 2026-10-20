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
context also loads the roots the system trusts, exported from its keychains by
``/usr/bin/security`` (part of macOS, called by absolute path). Verification
and host name checks stay on, and nothing the system does not trust is added.
``SSL_CERT_FILE`` and ``SSL_CERT_DIR`` keep working as OpenSSL defines them.
"""

import ssl
import subprocess
import sys
import threading

SECURITY = '/usr/bin/security'
#: The system root store, then the administrator's System keychain (where an
#: organization installs its own roots).
KEYCHAINS = (
    '/System/Library/Keychains/SystemRootCertificates.keychain',
    '/Library/Keychains/System.keychain',
)
EXPORT_TIMEOUT = 10.0

_LOCK = threading.Lock()
_ROOTS = None  # list of PEM texts, one per keychain that exported


def _export_system_roots():
    """PEM text of every certificate in each keychain, read once per process."""
    global _ROOTS
    with _LOCK:
        if _ROOTS is None:
            roots = []
            for keychain in KEYCHAINS:
                try:
                    done = subprocess.run(
                        [SECURITY, 'find-certificate', '-a', '-p', keychain],
                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=EXPORT_TIMEOUT, check=False)
                except (OSError, subprocess.SubprocessError):
                    continue
                text = done.stdout.decode('ascii', 'replace') if done.returncode == 0 else ''
                if '-----BEGIN CERTIFICATE-----' in text:
                    roots.append(text)
            _ROOTS = roots
        return list(_ROOTS)


def _trusts_nothing(context):
    return not context.cert_store_stats().get('x509_ca')


def https_context():
    """A verifying client context; on macOS, with the system roots when OpenSSL found none."""
    context = ssl.create_default_context()
    if sys.platform == 'darwin' and _trusts_nothing(context):
        for pem in _export_system_roots():
            try:
                context.load_verify_locations(cadata=pem)
            except (ssl.SSLError, ValueError):
                # One unreadable entry must not cost the other keychain.
                continue
    return context
