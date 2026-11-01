"""A stand-in for the OpenSSH client, for the SSH tunnel tests.

test_ssh_tunnel.py copies this file into a temporary directory as an
executable named ``ssh`` (with the running Python in its first line). It
reads what to do from ``fake-ssh.json`` next to itself, because ssh gets an
allowlisted environment (the tests check that too):

- ``mode``: ``forward`` (default) listens on the ``-L`` local port and
  forwards each connection to the database host and port, like ssh;
  ``fail`` prints ``stderr`` and exits 255; ``hang`` never listens;
  ``busy-once`` fails the first start like a local port already in use,
  then forwards; ``die-after`` forwards for ``lifetime`` seconds, then exits;
  ``silent`` forwards without saying so (no debug lines at all);
  ``no-session`` says it listens but never enters its session;
  ``no-forward`` enters its session without setting up the forward (as
  with ClearAllForwardings in ssh_config).
- ``auth_delay``: seconds "signing in" before the forward is set up (the
  window in which another local program could take the port first).
- ``refuse_channel``: every forwarded connection fails like
  "channel 2: open failed: connect failed: Connection refused".

With ``-o LogLevel=DEBUG1`` it logs what OpenSSH 10.3 logs at that level
around the forward (checked against the real client): "Local forwarding
listening on 127.0.0.1 port N." before the bind, then either the bind error
and "Could not request local forwarding." (exit 255, as with
ExitOnForwardFailure) or "channel 0: new port-listener [port listener]",
"Requesting no-more-sessions@openssh.com" and "Entering interactive
session.". Every start appends its argv and environment names to
``starts.jsonl``; every connection it accepts adds a line to ``accepted``.
"""
import json
import os
import re
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def config():
    try:
        with open(os.path.join(HERE, 'fake-ssh.json')) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def log(entry):
    with open(os.path.join(HERE, 'starts.jsonl'), 'a') as handle:
        handle.write(json.dumps(entry) + '\n')


def forward_spec(argv):
    value = argv[argv.index('-L') + 1]
    match = re.match(r'^127\.0\.0\.1:(\d+):(\[[^\]]+\]|[^:]+):(\d+)$', value)
    host = match.group(2)
    return int(match.group(1)), host[1:-1] if host.startswith('[') else host, int(match.group(3))


def pipe(source, target):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, target):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def debug(argv, text):
    if 'LogLevel=DEBUG1' in argv:
        sys.stderr.write('debug1: %s\r\n' % text)
        sys.stderr.flush()


def serve(listener, db_host, db_port, settings, argv):
    while True:
        client, _ = listener.accept()
        with open(os.path.join(HERE, 'accepted'), 'a') as handle:
            handle.write('1\n')
        debug(argv, 'Connection to port %d forwarding to %s port %d requested.'
              % (listener.getsockname()[1], db_host, db_port))
        debug(argv, 'channel 1: new direct-tcpip [direct-tcpip] (inactive timeout: 0)')
        if settings.get('refuse_channel'):
            sys.stderr.write('channel 2: open failed: connect failed: Connection refused\n')
            sys.stderr.flush()
            client.close()
            continue
        try:
            upstream = socket.create_connection((db_host, db_port), timeout=5)
        except OSError as exc:
            sys.stderr.write('channel 2: open failed: connect failed: %s\n' % (exc.strerror or exc))
            sys.stderr.flush()
            client.close()
            continue
        upstream.settimeout(None)
        threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()


def main():
    argv = sys.argv[1:]
    settings = config()
    log({'argv': argv, 'env': sorted(os.environ), 'pid': os.getpid(), 'pgid': os.getpgid(0) if hasattr(os, 'getpgid') else None,
         'ppid': os.getppid(), 'home': os.environ.get('HOME'), 'agent': os.environ.get('SSH_AUTH_SOCK')})
    mode = settings.get('mode', 'forward')
    say = (lambda text: None) if mode == 'silent' else (lambda text: debug(argv, text))
    say('Reading configuration data /home/test/.ssh/config')
    say('Connecting to bastion [192.0.2.10] port 22.')
    if mode == 'fail':
        sys.stderr.write(settings.get('stderr', 'ssh: unknown failure\n'))
        sys.exit(255)
    if mode == 'hang':
        time.sleep(3600)
        return
    if settings.get('auth_delay'):
        time.sleep(settings['auth_delay'])
    if 'LogLevel=DEBUG1' in argv:
        # A VERBOSE line: shown at DEBUG1 without a "debug1:" prefix.
        sys.stderr.write('Authenticated to bastion ([192.0.2.10]:22) using "publickey".\r\n')
    local, db_host, db_port = forward_spec(argv)
    if mode == 'no-forward':
        say('Requesting no-more-sessions@openssh.com')
        say('Entering interactive session.')
        time.sleep(3600)
        return
    say('Local connections to 127.0.0.1:%d forwarded to remote address %s:%d' % (local, db_host, db_port))
    say('Local forwarding listening on 127.0.0.1 port %d.' % local)
    if mode == 'busy-once':
        marker = os.path.join(HERE, 'busy-once.done')
        if not os.path.exists(marker):
            open(marker, 'w').close()
            sys.stderr.write('bind [127.0.0.1]:%d: Address already in use\r\n'
                             'channel_setup_fwd_listener_tcpip: cannot listen to port: %d\r\n'
                             'Could not request local forwarding.\r\n' % (local, local))
            sys.exit(255)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(('127.0.0.1', local))
        listener.listen(16)
    except OSError as exc:
        sys.stderr.write('bind [127.0.0.1]:%d: %s\r\n'
                         'channel_setup_fwd_listener_tcpip: cannot listen to port: %d\r\n'
                         'Could not request local forwarding.\r\n' % (local, exc.strerror or exc, local))
        sys.exit(255)
    say('channel 0: new port-listener [port listener] (inactive timeout: 0)')
    if mode != 'no-session':
        say('Requesting no-more-sessions@openssh.com')
        say('Entering interactive session.')
    threading.Thread(target=serve, args=(listener, db_host, db_port, settings, argv), daemon=True).start()
    lifetime = settings.get('lifetime') if mode == 'die-after' else None
    deadline = time.monotonic() + lifetime if lifetime else None
    while deadline is None or time.monotonic() < deadline:
        time.sleep(0.05)
    sys.exit(255)


if __name__ == '__main__':
    main()
