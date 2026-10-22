"""Local servers for HTTPS deadline tests (test_news, test_app_update).

Nothing leaves 127.0.0.1: a test patches ``socket.create_connection`` with
``server.connect``, so a read of an official HTTPS address reaches the local
server instead, through the real HTTPS handler and TLS socket.

* :class:`SilentServer` accepts TCP connections and never answers, so the
  client's TLS handshake stalls on its first read.
* :class:`TrickleTLSServer` completes a TLS handshake with an anonymous cipher
  (no certificate, so no key is kept in the repository), reads the request,
  sends an optional head at once (a status line, headers and the start of a
  body), then the rest one byte at a time, which resets every per-read socket
  timeout. :func:`anonymous_contexts` is None where the OpenSSL build has no
  anonymous cipher (LibreSSL); those tests skip.
"""
import itertools
import socket
import ssl
import threading
import time

_CREATE_CONNECTION = socket.create_connection
_ANONYMOUS = 'aNULL:@SECLEVEL=0'
_PROBED = []


class _Listener:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.address = self.listener.getsockname()

    def connect(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, *args, **kwargs):
        """Stands in for socket.create_connection: every address is this server."""
        return _CREATE_CONNECTION(self.address, timeout)

    def close(self):
        self.listener.close()


class SilentServer(_Listener):
    """Accepts connections (the kernel does) and never reads or writes."""


class TrickleTLSServer(_Listener):
    """One TLS connection: the handshake, the request, *head* at once, then
    *answer* one byte every *interval* seconds (repeating) for at most
    *limit* seconds. ``gone_after`` is how long after the first byte of
    *answer* the client went away (None while it is still reading or when the
    server stopped first); ``dripped`` counts the bytes of *answer* sent."""

    def __init__(self, context, answer, interval=0.05, limit=4.0, head=b''):
        super().__init__()
        self._context = context
        self._head = head
        self._answer = answer
        self._interval = interval
        self._limit = limit
        self._stop = threading.Event()
        self.gone_after = None
        self.dripped = 0
        self.error = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self.listener.settimeout(self._limit + 5)
        try:
            conn, _ = self.listener.accept()
        except OSError as exc:
            self.error = exc
            return
        started, tls = None, None
        try:
            conn.settimeout(self._limit + 5)
            tls = self._context.wrap_socket(conn, server_side=True)
            request = b''
            while b'\r\n\r\n' not in request:
                chunk = tls.recv(4096)
                if not chunk:
                    return
                request += chunk
            if self._head:
                tls.sendall(self._head)
            started = time.monotonic()
            for byte in itertools.cycle(self._answer):
                if self._stop.is_set() or time.monotonic() - started > self._limit:
                    return
                tls.sendall(bytes((byte,)))
                self.dripped += 1
                time.sleep(self._interval)
        except OSError as exc:  # ssl.SSLError is an OSError too
            if started is not None:
                self.gone_after = time.monotonic() - started
            else:
                self.error = exc
        finally:
            (tls or conn).close()

    def wait(self, timeout):
        """Wait up to *timeout* seconds for the server to finish; ``gone_after``."""
        self._thread.join(timeout)
        return self.gone_after

    def close(self):
        self._stop.set()
        self._thread.join(self._limit + 10)
        super().close()


def anonymous_contexts():
    """(server, client) TLS contexts with an anonymous cipher, or None when
    this OpenSSL cannot complete such a handshake. The client trusts anyone:
    only tests that need a finished handshake without a certificate use it."""
    if _PROBED:
        return _PROBED[0]
    try:
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.set_ciphers(_ANONYMOUS)
        server.maximum_version = ssl.TLSVersion.TLSv1_2
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        client.set_ciphers(_ANONYMOUS)
        client.maximum_version = ssl.TLSVersion.TLSv1_2
        _probe(server, client)
        contexts = (server, client)
    except (OSError, ValueError, AttributeError):
        contexts = None
    _PROBED.append(contexts)
    return contexts


def _probe(server, client):
    left, right = socket.socketpair()
    left.settimeout(5)
    right.settimeout(5)
    failure, opened = [], [left, right]

    def accept():
        try:
            tls = server.wrap_socket(right, server_side=True)
            opened.append(tls)
            tls.recv(1)
        except OSError as exc:
            failure.append(exc)
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        tls = client.wrap_socket(left, server_hostname='localhost')
        opened.append(tls)
        tls.sendall(b'x')
        thread.join(5)
    finally:
        for sock in opened:
            sock.close()
    if failure:
        raise failure[0]
