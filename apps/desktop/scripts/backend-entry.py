"""PyInstaller entry point; the regular CLI remains available in the bundle."""
import atexit
import os
import signal
import sys
import threading
import time

from myflames.cli import main


def _stop_with_desktop_parent():
    """Stop the workspace backend when the desktop app that owns it dies.

    The desktop starts `ui` as the leader of a fresh process group, names its
    own pid in MYFLAMES_DESKTOP_PARENT and stops the group on a normal quit. A
    force quit or crash of the desktop skips that cleanup, so watch for
    reparenting, stop like Ctrl+C, and on exit remove what is left of this
    owned process group (client subprocesses of requests still in flight).
    Without the marker, or when not the group leader, nothing is watched.
    """
    expected = os.environ.pop('MYFLAMES_DESKTOP_PARENT', '')
    if sys.argv[1:2] != ['ui'] or not expected.isdigit() or not hasattr(os, 'killpg'):
        return
    if os.getpgid(0) != os.getpid():
        return
    if os.getppid() != int(expected):
        sys.exit(0)  # The desktop died before this process started.
    group = os.getpid()

    def end_group():
        # Leave this process alone so its own exit status stands.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            os.killpg(group, signal.SIGTERM)
        except OSError:
            pass

    def watch():
        while os.getppid() == int(expected):
            time.sleep(1)
        atexit.register(end_group)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(5)
        os.killpg(group, signal.SIGKILL)  # Shutdown hung.

    threading.Thread(target=watch, name='desktop-parent-watch', daemon=True).start()


if __name__ == '__main__':
    if sys.argv[1:2] == ['__myflames_regex_worker']:
        # The ProxySQL place tests query rule patterns in a child process, so a
        # pattern that backtracks without end never holds up the backend
        # (myflames/proxysql_digests.py test_patterns).
        from myflames.proxysql_digests import regex_worker
        regex_worker()
        sys.exit(0)
    _stop_with_desktop_parent()
    main()
