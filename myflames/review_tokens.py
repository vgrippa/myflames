"""Bounded, expiring, one-use proof of the exact action reviewed in this UI."""
import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict


class ReviewTokens:
    def __init__(self, ttl=300, capacity=256, clock=None):
        self.ttl = ttl
        self.capacity = capacity
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.entries = OrderedDict()

    @staticmethod
    def fingerprint(action, connection_identity, details):
        value = json.dumps([action, connection_identity, details], sort_keys=True,
                           separators=(',', ':'), ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(value.encode('ascii')).hexdigest()

    def _prune(self):
        now = self.clock()
        for token, (_, expiry) in list(self.entries.items()):
            if expiry <= now:
                del self.entries[token]

    def issue(self, action, connection_identity, details):
        digest = self.fingerprint(action, connection_identity, details)
        token = secrets.token_urlsafe(32)
        with self.lock:
            self._prune()
            while len(self.entries) >= self.capacity:
                self.entries.popitem(last=False)
            self.entries[token] = (digest, self.clock() + self.ttl)
        return token

    def check(self, token, action, connection_identity, details, consume=False):
        digest = self.fingerprint(action, connection_identity, details)
        with self.lock:
            self._prune()
            entry = self.entries.get(token) if isinstance(token, str) and len(token) <= 128 else None
            if entry is None or not secrets.compare_digest(entry[0], digest):
                raise ValueError('This review expired, changed or was already used. Review the action again; nothing was sent.')
            if consume:
                del self.entries[token]

    def consumer(self, token, action, connection_identity, details):
        # Freeze the fingerprint now; mutable request dictionaries cannot
        # change what the callback checks at the eventual send boundary.
        frozen = json.loads(json.dumps(details, allow_nan=False))
        self.check(token, action, connection_identity, frozen)
        return lambda: self.check(token, action, connection_identity, frozen, consume=True)
