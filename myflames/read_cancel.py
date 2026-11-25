"""Private cancellation context for synchronous, read-only workspace requests.

Only the HTTP read allowlist installs this context. It is never accepted from
request JSON, and writes and persistent sessions run without it. Worker pools
must explicitly carry the event and consent guard into their own threads with
``scope``. The guard keeps a shared read bound to its original group incarnation.
"""
from contextlib import contextmanager
import threading
import time


class CancelledRead(BaseException):
    """Stop a read, including readers that turn ordinary failures into notes.

    Like KeyboardInterrupt, cancellation must escape broad ``except Exception``
    fallbacks without attempting another query or returning a partial report.
    """


_LOCAL = threading.local()


def current():
    return getattr(_LOCAL, "cancel", None)


def current_guard():
    return getattr(_LOCAL, "guard", None)


def check(cancel=None):
    event = current() if cancel is None else cancel
    if event is not None and event.is_set():
        raise CancelledRead()


@contextmanager
def scope(cancel, guard=None):
    previous, previous_guard = current(), current_guard()
    _LOCAL.cancel = cancel
    _LOCAL.guard = guard
    try:
        check()
        yield
    finally:
        _LOCAL.cancel = previous
        _LOCAL.guard = previous_guard


def wait(seconds, sleep=None):
    """An interruptible sample window; preserve injected clocks/sleeps in tests."""
    check()
    cancel = current()
    if cancel is not None and sleep is None:
        cancel.wait(seconds)
    else:
        (sleep or time.sleep)(seconds)
    check()
