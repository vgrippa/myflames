"""Ties one SSH tunnel to the myflames process that opened it (POSIX).

ssh_tunnel.py starts ``python -I ssh_watchdog.py -- ssh ...`` (in the
packaged backend: ``myflames-backend __myflames_ssh_watchdog -- ssh ...``)
as the leader of a new process group, with a pipe on standard input whose
writing end only that myflames process holds. This process starts ssh in the
same group, leaves ssh's standard error going straight to myflames, and ends
with ssh's exit status.

When the pipe closes, the process that opened the tunnel has ended, possibly
without any cleanup of its own (SIGKILL, a crash, a supervisor that killed
it): ssh gets SIGTERM, then SIGKILL after GRACE seconds, and whatever is left
of the group (a ProxyCommand) is killed with it. SIGTERM, SIGHUP or SIGINT
sent to this process stop ssh the same way.

Standard library only, and nothing from myflames, so a source checkout runs
it as a plain script in isolated mode.
"""
import os
import signal
import subprocess
import sys
import threading
import time

#: The packaged backend's first argument for this mode (backend-entry.py).
ARG = '__myflames_ssh_watchdog'
#: Seconds between SIGTERM and SIGKILL.
GRACE = 2.0


def _stop(child):
    if child.poll() is not None:
        return
    try:
        child.terminate()
    except OSError:
        return
    deadline = time.monotonic() + GRACE
    while child.returncode is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if child.returncode is None:
        try:
            child.kill()
        except OSError:
            pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--']:
        argv = argv[1:]
    if not argv:
        sys.stderr.write('No ssh command was given.\n')
        return 2
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    except OSError as exc:
        sys.stderr.write('Could not start ssh (%s): %s\n' % (argv[0], exc.strerror or exc))
        return 255
    stopping = threading.Event()
    orphaned = threading.Event()

    def stop():
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=_stop, args=(child,), daemon=True).start()

    def on_signal(signum, frame):
        stop()

    for name in ('SIGTERM', 'SIGHUP', 'SIGINT'):
        signal.signal(getattr(signal, name), on_signal)

    def watch_owner():
        # Nothing is ever written: read() returns b'' once the owner's end
        # closes, whatever ended the owner.
        try:
            while os.read(0, 4096):
                pass
        except OSError:
            pass
        orphaned.set()
        stop()

    threading.Thread(target=watch_owner, name='owner-watch', daemon=True).start()
    code = child.wait()
    if orphaned.is_set() and os.getpgid(0) == os.getpid():
        # Nobody is left to settle this group: end what remains of it,
        # this process included.
        try:
            os.killpg(os.getpid(), signal.SIGKILL)
        except OSError:
            pass
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main())
