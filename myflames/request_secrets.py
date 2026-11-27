"""Redaction context containing only passwords already obtained by a request.

This module never reads profiles or a secret backend. Request workers explicitly
borrow the same registry, as they do with ``read_cancel.scope``. The owner clears
it on exit; late workers cannot repopulate a closed registry. Shared reads own a
separate registry until their last caller leaves, and copy its values to each
caller's request before returning or raising.
"""
from contextlib import contextmanager
from functools import wraps
import threading


class Registry(object):
    """A thread-safe, in-memory set with an explicit end of lifetime."""

    def __init__(self):
        self._lock = threading.Lock()
        self._values = set()
        self._closed = False

    def capture_many(self, passwords):
        with self._lock:
            if not self._closed:
                self._values.update(p for p in passwords if isinstance(p, str) and p)

    def values(self):
        with self._lock:
            return set(self._values)

    def close(self):
        with self._lock:
            self._closed = True
            self._values.clear()


_LOCAL = threading.local()
_NEW = object()


def current():
    return getattr(_LOCAL, 'registry', None)


def capture(password):
    capture_many((password,))


def capture_many(passwords):
    registry = current()
    if registry is not None:
        registry.capture_many(passwords)


def values():
    registry = current()
    return registry.values() if registry is not None else set()


@contextmanager
def scope(registry=_NEW):
    """Own a new registry, or borrow an explicitly supplied worker context."""
    previous = current()
    owned = registry is _NEW
    if owned:
        registry = Registry()
    _LOCAL.registry = registry
    try:
        yield registry
    finally:
        _LOCAL.registry = previous
        if owned:
            registry.close()


def scoped(function):
    """Keep direct entry points scoped, reusing an enclosing HTTP request."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if current() is not None:
            return function(*args, **kwargs)
        with scope():
            return function(*args, **kwargs)
    return wrapped
