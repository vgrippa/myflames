"""Explicit local investigation/profile storage. Passwords never belong here.

A profile may *remember* its password: the secret goes to the OS credential
store or the encrypted vault (see secret_store.py). The SQLite record keeps
only ``remember_password``, ``secret_backend`` and ``secret_ref``, the secret
store key, which is ``<profile ID>.<endpoint fingerprint>.<random>``: a hash
of the host, port, user and TLS settings the password was saved for, and a
new random part on every save. The record and its reference are written in
one transaction, so two concurrent saves can never pair one save's endpoint
with the other's password, and a password is released only when the
fingerprint in its key matches the profile's endpoint (see
:meth:`WorkspaceStore.connection_with_saved_password`). Passwords saved before
``secret_ref`` existed stay under the bare profile ID until the next save.
"""
import datetime
from contextlib import closing
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

PROFILE_FIELDS = ('name', 'host', 'port', 'user', 'database', 'ssl_mode', 'ssl_ca')
# Write safety (dbops.py): Production confirms every write, Development only
# risky ones, read-only refuses every write. New and existing profiles are
# Production. Part of the connection identity reports are bound to.
PROFILE_SAFETY_FIELDS = ('type', 'read_only')
CONNECTION_TYPES = ('production', 'development')
#: PRAGMA user_version of the workspace database. 1: profiles carry type and
#: read_only (existing rows migrated to production, read-write).
STORE_VERSION = 1
# A saved password is released only to the endpoint and TLS settings it was
# saved with, so a request can't redirect it or downgrade its transport.
ENDPOINT_FIELDS = ('host', 'port', 'user', 'ssl_mode', 'ssl_ca')

# Session restore: one autosaved workspace snapshot (see validate_session_state).
SESSION_MAX_BYTES = 64 * 1024 * 1024
SESSION_MAX_DEPTH = 200
SECRET_KEYS = ('password',)
#: Records of the Query editor's run history (editor_session.py).
HISTORY_KIND = 'editor_history'


class SessionConflict(ValueError):
    """The client saved over an older revision than the stored one."""

    def __init__(self, revision):
        super().__init__('Your workspace changed in another browser tab.')
        self.revision = revision


class SessionTooLarge(ValueError):
    """The state exceeds SESSION_MAX_BYTES; the previous session is kept."""


def validate_session_state(state):
    """Return a password-free copy of *state* or raise ValueError.

    The state is an opaque UI snapshot, but it must be a JSON object with
    bounded nesting and finite numbers. Every ``password`` key is removed at
    any depth. SQL/text fields also redact credential clauses before saving,
    so editor restore does not persist CREATE USER or similar passwords.
    """
    if not isinstance(state, dict):
        raise ValueError('The workspace session must be a JSON object.')
    clean = {}
    # Iterative copy: (source, destination container, depth).
    stack = [(state, clean, 1)]
    while stack:
        source, target, depth = stack.pop()
        if depth > SESSION_MAX_DEPTH:
            raise ValueError('The workspace session is too deeply nested (maximum %d levels).'
                             % SESSION_MAX_DEPTH)
        if isinstance(source, dict):
            items = []
            for key, value in source.items():
                if not isinstance(key, str):
                    raise ValueError('Workspace session keys must be text.')
                if key.lower() in SECRET_KEYS:
                    continue
                items.append((key, value))
        else:
            items = list(enumerate(source))
        for key, value in items:
            if isinstance(value, dict):
                child = {}
            elif isinstance(value, list):
                child = []
            else:
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError('Workspace session numbers must be finite.')
                if value is not None and not isinstance(value, (str, int, float, bool)):
                    raise ValueError('The workspace session must contain only JSON values.')
                child = value
                if isinstance(value, str) and isinstance(key, str) and key.lower() in (
                        'sql', 'text', 'query', 'statement', 'sql_text'):
                    from .dbops import redact
                    child = redact(value)
            if isinstance(target, dict):
                target[key] = child
            else:
                target.append(child)
            if isinstance(child, (dict, list)):
                stack.append((value, child, depth + 1))
    return clean


def validate_bundle(value):
    if not isinstance(value, dict) or value.get('schema_version') != 'investigation-1.0':
        raise ValueError('Choose a myflames investigation-1.0 bundle.')
    plans = value.get('plans')
    if not isinstance(plans, list) or len(plans) > 30:
        raise ValueError('An investigation can contain up to 30 plans.')
    tags = value.get('tags', [])
    if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
        raise ValueError('Investigation tags must be a list of strings.')
    clean = {'schema_version': 'investigation-1.0', 'name': str(value.get('name') or 'Untitled investigation')[:200],
             'notes': str(value.get('notes') or '')[:100000],
             'tags': [t[:80] for t in tags[:30]],
             'baseline_id': str(value.get('baseline_id') or ''), 'plans': []}
    from .plan_input import _plan
    ids = set()
    for item in plans:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not item['id'] or len(item['id']) > 200 or item['id'] in ids):
            raise ValueError('Every plan needs a unique string ID of 1–200 characters.')
        ids.add(item['id'])
        _plan({'plan': item.get('raw')})
        clean['plans'].append({'id': item['id'], 'name': str(item.get('name') or 'Plan')[:200],
                               'raw': item['raw'], 'notes': str(item.get('notes') or '')[:10000],
                               'capture': item.get('capture')})
    if clean['baseline_id'] and clean['baseline_id'] not in ids:
        raise ValueError('The baseline must reference a plan in this investigation.')
    from .live import normalize_capture
    for item in clean['plans']:
        item['capture'] = normalize_capture(item['capture'])
    encoded = json.dumps(clean, allow_nan=False)
    if len(encoded.encode('utf-8')) > 10 * 1024 * 1024:
        raise ValueError('Investigation exceeds 10 MiB. Split it into smaller bundles.')
    return clean


#: Page size bounds of ``investigations/list`` (Home asks for 6, Library 25).
INVESTIGATION_PAGE_SIZE = 25
INVESTIGATION_PAGE_MAX = 50
INVESTIGATION_SEARCH_MAX = 512


def _investigation_summary(value):
    """``investigation-summary-1.0``: what Home and Library show, never plans.

    Name, notes and tags are kept whole because Library displays and searches
    them; each plan keeps its ID, name and capture mode. The saved
    ``investigation-1.0`` bundle stays the only source for opening, comparing
    and exporting. Raises on a record that is not a bundle.
    """
    plans = []
    for plan in value['plans']:
        item = {'id': plan['id'], 'name': plan.get('name') or 'Plan'}
        if isinstance(plan.get('capture'), dict):
            item['capture'] = {'mode': plan['capture'].get('mode')}
        plans.append(item)
    tags = [str(tag) for tag in value.get('tags') or []]
    return {'schema_version': 'investigation-summary-1.0',
            'name': str(value.get('name') or 'Untitled investigation'),
            'notes': str(value.get('notes') or ''), 'tags': tags,
            'baseline_id': str(value.get('baseline_id') or ''), 'plans': plans}


def _save_investigation_summary(conn, key, updated, value):
    summary = _investigation_summary(value)
    # Library's own filter: name, tags and notes, lowercased.
    search = ('%s %s %s' % (summary['name'], ' '.join(summary['tags']), summary['notes'])).lower()
    conn.execute('INSERT OR REPLACE INTO investigation_summaries VALUES (?, ?, ?, ?)',
                 (key, updated, json.dumps(summary, allow_nan=False), search))


# A summary is current when it carries its record's ``updated`` stamp
# (microsecond ISO time, new on every save). These read only the leading
# columns of each record, never the payload's overflow pages.
_STALE_SUMMARIES = ("SELECT r.id FROM records r LEFT JOIN investigation_summaries s ON s.id = r.id "
                    "WHERE r.kind = 'investigation' AND (s.updated IS NULL OR s.updated <> r.updated)")
_ORPHAN_SUMMARIES = ("SELECT s.id FROM investigation_summaries s LEFT JOIN records r "
                     "ON r.id = s.id AND r.kind = 'investigation' WHERE r.id IS NULL")


class WorkspaceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path.home() / '.myflames' / 'workspace.sqlite3'

    def _connect(self):
        from .secret_store import _ensure_private_directory, _open_private_file
        directory = self.path.parent
        _ensure_private_directory(directory)
        fd, _created = _open_private_file(self.path, os.O_RDWR, create=True)
        os.close(fd)
        self._protect_sidecars()
        conn = sqlite3.connect(str(self.path), timeout=10)
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS session (id INTEGER PRIMARY KEY CHECK (id = 1), '
                         'revision INTEGER NOT NULL, saved_at TEXT NOT NULL, state TEXT, '
                         'restore INTEGER NOT NULL, writer TEXT NOT NULL, writer_seq INTEGER NOT NULL)')
            # The audit trail of server writes (dbops.py). Never holds a password.
            conn.execute('CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, attempted_at TEXT NOT NULL, '
                         'finished_at TEXT, status TEXT NOT NULL, outcome TEXT, surface TEXT NOT NULL, os_user TEXT, '
                         'operation TEXT NOT NULL, profile_id TEXT, profile_name TEXT, connection_type TEXT, '
                         'read_only INTEGER, host TEXT, port INTEGER, db_user TEXT, engine TEXT, server_version TEXT, '
                         'statements TEXT NOT NULL, target TEXT, verified TEXT, policy TEXT, error_code INTEGER, '
                         'message TEXT, rows_affected INTEGER, after TEXT, duration_ms REAL)')
            conn.execute('CREATE INDEX IF NOT EXISTS audit_attempted_at ON audit (attempted_at)')
            conn.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            # A cache of investigation list metadata, derived from records and
            # refreshed by list_investigations: an older myflames sharing this
            # file writes records only, so no version bump or migration.
            conn.execute('CREATE TABLE IF NOT EXISTS investigation_summaries (id TEXT PRIMARY KEY, '
                         'updated TEXT NOT NULL, payload TEXT NOT NULL, search TEXT NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS investigation_summaries_recent '
                         'ON investigation_summaries (updated, id)')
            conn.commit()
            if conn.execute('PRAGMA user_version').fetchone()[0] < STORE_VERSION:
                self._migrate(conn)
            self._protect_sidecars()
        except BaseException:
            conn.close()
            raise
        return conn

    def _protect_sidecars(self):
        for suffix in ('-wal', '-shm', '-journal'):
            _owner_only(Path(str(self.path) + suffix), 0o600)

    def _migrate(self, conn):
        """Bring an older database up to STORE_VERSION, once, under the write lock."""
        conn.isolation_level = None
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('PRAGMA user_version').fetchone()[0] < 1:
                for key, payload in conn.execute("SELECT id, payload FROM records WHERE kind='profile'").fetchall():
                    try:
                        value = json.loads(payload)
                    except ValueError:
                        continue
                    if not isinstance(value, dict):
                        continue
                    value.update(_safety_fields(value, value))
                    conn.execute('UPDATE records SET payload=? WHERE id=?', (json.dumps(value, allow_nan=False), key))
                conn.execute('PRAGMA user_version = 1')
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.isolation_level = ''

    # -- Session restore --------------------------------------------------
    # One row (id = 1). ``revision`` increases on every save, clear, and
    # restore-setting change, so a page holding an older revision gets a
    # SessionConflict instead of silently overwriting newer work. ``writer``
    # and ``writer_seq`` let one page's own overlapping saves (an autosave
    # still in flight when the page hides) land without a false conflict,
    # and drop an older save from that page that arrives late.

    def _session_row(self, conn):
        conn.execute("INSERT OR IGNORE INTO session VALUES (1, 0, '', NULL, 1, '', 0)")
        return conn.execute('SELECT revision, saved_at, state, restore, writer, writer_seq '
                            'FROM session WHERE id = 1').fetchone()

    def _session_transaction(self):
        return self._transaction()

    def _transaction(self):
        conn = self._connect()
        conn.isolation_level = None
        # IMMEDIATE takes the write lock first, so the revision check and the
        # write are atomic across server threads and processes.
        try:
            conn.execute('BEGIN IMMEDIATE')
        except BaseException:
            # A busy timeout raises here, before any caller owns the connection.
            conn.close()
            raise
        return conn

    def load_session(self):
        """Stored session: revision, saved_at, state (dict or None), restore."""
        if not self.path.exists():
            return {'revision': 0, 'saved_at': '', 'state': None, 'restore': True}
        with closing(self._connect()) as conn, conn:
            revision, saved_at, state, restore, _, _ = self._session_row(conn)
        return {'revision': revision, 'saved_at': saved_at,
                'state': json.loads(state) if state else None, 'restore': bool(restore)}

    def save_session(self, state, revision, client_id='', seq=None):
        """Replace the stored state when *revision* is current. Returns the new
        revision and saved_at. Raises SessionConflict, SessionTooLarge, or
        ValueError; on any error the previous session stays intact."""
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError('The session revision must be a nonnegative integer.')
        if not isinstance(client_id, str) or len(client_id) > 100:
            raise ValueError('The session client ID must be text of at most 100 characters.')
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
            raise ValueError('The session sequence number must be a nonnegative integer.')
        clean = validate_session_state(state)
        encoded = json.dumps(clean, allow_nan=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > SESSION_MAX_BYTES:
            raise SessionTooLarge('The workspace is larger than 64 MiB. Remove some plans so it can be saved.')
        conn = self._session_transaction()
        try:
            current, saved_at, _, restore, writer, writer_seq = self._session_row(conn)
            own = bool(client_id) and client_id == writer
            if own and seq is not None and seq <= writer_seq:
                # An older save from this page arrived after a newer one.
                conn.execute('ROLLBACK')
                return {'revision': current, 'saved_at': saved_at, 'stale': True}
            if revision != current and not own:
                conn.execute('ROLLBACK')
                raise SessionConflict(current)
            if not restore:
                conn.execute('ROLLBACK')
                raise ValueError('Restore workspace on startup is off, so the workspace is not saved.')
            saved_at = _now()
            conn.execute('UPDATE session SET revision = ?, saved_at = ?, state = ?, writer = ?, writer_seq = ? '
                         'WHERE id = 1', (current + 1, saved_at, encoded, client_id, seq or 0))
            conn.execute('COMMIT')
        except Exception:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
        return {'revision': current + 1, 'saved_at': saved_at}

    def clear_session(self, client_id='', seq=None):
        """Forget the stored state (settings stay). Returns the new revision.

        *seq* is the page's next save sequence number: any save from the same
        page with a lower number (an autosave already in flight) is then
        dropped as stale instead of bringing the cleared state back."""
        return self._update_session(None, client_id, seq)

    def set_session_restore(self, restore, client_id='', seq=None):
        """Turn restore on startup on or off. Off also clears the stored state."""
        if not isinstance(restore, bool):
            raise ValueError('restore must be true or false.')
        return self._update_session(restore, client_id, seq)

    def _update_session(self, restore, client_id, seq=None):
        if not isinstance(client_id, str) or len(client_id) > 100:
            raise ValueError('The session client ID must be text of at most 100 characters.')
        if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
            raise ValueError('The session sequence number must be a nonnegative integer.')
        conn = self._session_transaction()
        try:
            current, _, state, enabled, writer, writer_seq = self._session_row(conn)
            if client_id and client_id == writer and seq is not None and seq <= writer_seq:
                # A clear or settings change from this page that arrives after a
                # newer request from it (a delayed or retried request): applying
                # it would delete newer state and lower writer_seq, so a retried
                # old save would then be accepted. Nothing changes.
                conn.execute('ROLLBACK')
                return {'revision': current, 'restore': bool(enabled), 'stale': True}
            if seq is not None:
                writer_seq = seq
            elif writer != client_id:
                writer_seq = 0
            if restore is not None:
                enabled = restore
            if restore is None or not restore:
                state = None
            conn.execute('UPDATE session SET revision = ?, saved_at = ?, state = ?, restore = ?, writer = ?, '
                         'writer_seq = ? WHERE id = 1',
                         (current + 1, '' if state is None else _now(), state, int(bool(enabled)),
                          client_id, writer_seq))
            conn.execute('COMMIT')
        except Exception:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
        return {'revision': current + 1, 'restore': bool(enabled)}

    def list(self, kind):
        if not self.path.exists():
            return []
        with closing(self._connect()) as conn, conn:
            rows = conn.execute('SELECT id, updated, payload FROM records WHERE kind=? ORDER BY updated DESC', (kind,)).fetchall()
        return [_present(kind, dict(id=key, updated_at=updated, **json.loads(payload)))
                for key, updated, payload in rows]

    def list_investigations(self, limit=INVESTIGATION_PAGE_SIZE, cursor=None, search=''):
        """``investigation-list-1.0``: one page of summaries, newest first.

        Pages are keyed on (updated, id), so a delete never skips a row and
        ``next_cursor`` is null on the last page. *search* keeps the rows
        whose name, tags or notes contain it, ignoring case (Library's filter).
        Never decodes a saved bundle except to refresh a stale summary, one at
        a time; get() returns the complete bundle.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= INVESTIGATION_PAGE_MAX:
            raise ValueError('Ask for 1 to %d investigations at a time.' % INVESTIGATION_PAGE_MAX)
        if not isinstance(search, str) or len(search) > INVESTIGATION_SEARCH_MAX:
            raise ValueError('Search text must be at most %d characters.' % INVESTIGATION_SEARCH_MAX)
        if cursor is not None and (not isinstance(cursor, dict) or any(
                not isinstance(cursor.get(k), str) or not cursor[k] or len(cursor[k]) > 200
                for k in ('updated_at', 'id'))):
            raise ValueError('That page of investigations is no longer valid. Reload the list.')
        result = {'schema_version': 'investigation-list-1.0', 'items': [], 'next_cursor': None}
        if not self.path.exists():
            return result
        where, args = [], []
        if cursor is not None:
            where.append('(updated < ? OR (updated = ? AND id < ?))')
            args.extend((cursor['updated_at'], cursor['updated_at'], cursor['id']))
        needle = search.strip().lower()
        if needle:
            where.append('instr(search, ?) > 0')
            args.append(needle)
        sql = "SELECT id, updated, payload FROM investigation_summaries WHERE payload <> 'null'"
        for clause in where:
            sql += ' AND ' + clause
        sql += ' ORDER BY updated DESC, id DESC LIMIT ?'
        args.append(limit + 1)
        self._sync_investigation_summaries()
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(sql, args).fetchall()
        if len(rows) > limit:
            rows = rows[:limit]
            result['next_cursor'] = {'updated_at': rows[-1][1], 'id': rows[-1][0]}
        result['items'] = [dict(id=key, updated_at=updated, **json.loads(payload)) for key, updated, payload in rows]
        return result

    def _sync_investigation_summaries(self):
        """Bring the summary cache in line with the saved records.

        This version writes both in one transaction; an older myflames using
        the same file writes records only. Missing or outdated summaries are
        rebuilt one bundle at a time, summaries of deleted records dropped. A
        record that is not a readable bundle is marked (payload ``null``) and
        left out of the list.
        """
        with closing(self._connect()) as conn:
            if (conn.execute(_STALE_SUMMARIES + ' LIMIT 1').fetchone() is None
                    and conn.execute(_ORPHAN_SUMMARIES + ' LIMIT 1').fetchone() is None):
                return
        conn = self._transaction()
        try:
            conn.execute('DELETE FROM investigation_summaries WHERE id IN (%s)' % _ORPHAN_SUMMARIES)
            for (key,) in conn.execute(_STALE_SUMMARIES).fetchall():
                updated, payload = conn.execute(
                    "SELECT updated, payload FROM records WHERE id = ? AND kind = 'investigation'", (key,)).fetchone()
                try:
                    _save_investigation_summary(conn, key, updated, json.loads(payload))
                except (ValueError, TypeError, KeyError, AttributeError):
                    # Not listed, and not decoded again until it changes.
                    conn.execute("INSERT OR REPLACE INTO investigation_summaries VALUES (?, ?, 'null', '')",
                                 (key, updated))
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()

    def get(self, kind, key):
        """One record by ID, or None. Never creates the database."""
        if not self.path.exists() or not isinstance(key, str):
            return None
        with closing(self._connect()) as conn, conn:
            row = conn.execute('SELECT id, updated, payload FROM records WHERE id=? AND kind=?', (key, kind)).fetchone()
        return _present(kind, dict(id=row[0], updated_at=row[1], **json.loads(row[2]))) if row else None

    def find_profile(self, name_or_id):
        """Resolve a profile by ID, then by exact name. Raises ValueError if absent or ambiguous."""
        profile = self.get('profile', name_or_id)
        if profile is not None:
            return profile
        matches = [p for p in self.list('profile') if p.get('name') == name_or_id]
        if len(matches) > 1:
            raise ValueError('Several connection profiles are named %r; use the profile ID instead.' % name_or_id)
        if not matches:
            raise ValueError('No saved connection profile named %r. List them with: myflames profiles list' % name_or_id)
        return matches[0]

    def save(self, kind, payload, key=None, password=None, remember=None):
        """Save a record. For profiles, *remember* True stores *password* in the
        secret store, False forgets any stored one, None keeps the current state."""
        if kind == 'profile':
            return self._save_profile(payload, key, password, remember)
        if kind == 'investigation':
            value = validate_bundle(payload)
        else:
            raise ValueError('Unknown record type.')
        return self._write(kind, value, key)

    def _write(self, kind, value, key):
        key = str(key or uuid.uuid4().hex)
        if len(key) > 200:
            raise ValueError('Invalid record ID.')
        updated = _now()
        with closing(self._connect()) as conn, conn:
            existing = conn.execute('SELECT kind FROM records WHERE id=?', (key,)).fetchone()
            if existing and existing[0] != kind:
                raise ValueError('Record type does not match.')
            conn.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)', (key, kind, updated, json.dumps(value, allow_nan=False)))
            if kind == 'investigation':
                _save_investigation_summary(conn, key, updated, value)
        return _present(kind, dict(id=key, updated_at=updated, **value))

    def _save_profile(self, payload, key, password, remember):
        from . import secret_store
        from .live import connection_options
        options = connection_options(payload)
        name = payload.get('name') or options['host']
        if not isinstance(name, str) or len(name) > 200:
            raise ValueError('Connection names must be text of at most 200 characters.')
        if password is not None and not isinstance(password, str):
            raise ValueError('Invalid connection field: password')
        if remember not in (None, True, False):
            raise ValueError('remember_password must be true or false.')
        safety = _check_safety(payload.get('type'), payload.get('read_only'))
        value = {field: options[field] for field in PROFILE_FIELDS if field in options}
        value['name'] = name
        key = str(key or uuid.uuid4().hex)
        if len(key) > 200:
            raise ValueError('Invalid record ID.')
        before = self._profile(key)
        if remember is None:
            remember = bool(before and before.get('remember_password'))
        if remember and not password and not (before and before.get('remember_password')):
            raise ValueError('Enter the password to remember it.')
        if not remember and before and before.get('remember_password'):
            # Delete first: if that fails, the record still says what is saved.
            secret_store.delete_secret(_stored_ref(key, before), before.get('secret_backend') or None)
        ref = backend = None
        if remember and password:
            # A new key on every save: a concurrent save writes its own entry
            # and the record committed last names the one that goes with it.
            ref = _new_secret_ref(key, value)
            backend = secret_store.save_secret(ref, password)
        replaced = None
        try:
            conn = self._transaction()
            try:
                kind, existing = self._record(conn, key)
                if kind not in (None, 'profile'):
                    raise ValueError('Record type does not match.')
                remembered = bool(existing and existing.get('remember_password'))
                if ref:
                    value.update(remember_password=True, secret_backend=backend, secret_ref=ref)
                    replaced = existing if remembered else None
                elif not remember:
                    value.update(remember_password=False, secret_backend='')
                    # Saved by another request since the delete above.
                    replaced = existing if remembered else None
                elif not remembered:
                    raise ValueError('Enter the password to remember it.')
                elif not _same_endpoint(value, existing):
                    # A saved password is tied to the endpoint and TLS settings it was saved for.
                    raise ValueError('The saved password belongs to the previous host, port, user, and TLS '
                                     'settings. Enter the password again, or turn off Remember password.')
                else:
                    value.update(remember_password=True, secret_backend=existing.get('secret_backend') or '')
                    if existing.get('secret_ref'):
                        value['secret_ref'] = existing['secret_ref']
                # Type and read-only are kept unless this save sets them; a
                # save that points the profile at another endpoint without
                # choosing a type makes it production again.
                requested = dict(safety)
                if requested['type'] is None and existing and not _same_endpoint(value, existing):
                    requested['type'] = 'production'
                value.update(_safety_fields(requested, existing or {}))
                updated = _now()
                conn.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)',
                             (key, 'profile', updated, json.dumps(value, allow_nan=False)))
                conn.execute('COMMIT')
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
            finally:
                conn.close()
        except BaseException:
            if ref:
                _delete_quietly(ref, backend)
            raise
        if replaced and _stored_ref(key, replaced) != ref:
            _delete_quietly(_stored_ref(key, replaced), replaced.get('secret_backend'))
        return _present('profile', dict(id=key, updated_at=updated, **value))

    def _record(self, conn, key):
        """(kind, payload dict) of one record inside an open transaction, or (None, None)."""
        row = conn.execute('SELECT kind, payload FROM records WHERE id=?', (key,)).fetchone()
        return (row[0], json.loads(row[1])) if row else (None, None)

    def _profile(self, key):
        """A stored profile with its ``secret_ref``, or None. Never creates the database."""
        if not self.path.exists() or not isinstance(key, str):
            return None
        with closing(self._connect()) as conn, conn:
            kind, value = self._record(conn, key)
        return value if kind == 'profile' else None

    def _remove_profile_secret(self, key, delete_record):
        """Forget a profile's saved password, or delete the profile with it.
        Returns the stored record after the change (None when deleted)."""
        from . import secret_store
        before = self._profile(key)
        if before is None:
            if delete_record:
                return None
            raise ValueError('The connection profile no longer exists.')
        deleted = _stored_ref(key, before) if before.get('remember_password') else None
        if deleted:
            secret_store.delete_secret(deleted, before.get('secret_backend') or None)
        conn = self._transaction()
        try:
            kind, existing = self._record(conn, key)
            if kind != 'profile':
                conn.execute('ROLLBACK')
                if delete_record:
                    return None
                raise ValueError('The connection profile no longer exists.')
            value = None
            if delete_record:
                conn.execute("DELETE FROM records WHERE id=? AND kind='profile'", (key,))
            else:
                value = {field: existing[field] for field in PROFILE_FIELDS if field in existing}
                value.update(_safety_fields(existing, existing))
                value.update(remember_password=False, secret_backend='')
                conn.execute('UPDATE records SET updated=?, payload=? WHERE id=?',
                             (_now(), json.dumps(value, allow_nan=False), key))
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
        if existing.get('remember_password') and _stored_ref(key, existing) != deleted:
            # Saved by another request between the delete above and this transaction.
            _delete_quietly(_stored_ref(key, existing), existing.get('secret_backend'))
        return value

    def set_profile_safety(self, key, connection_type=None, read_only=None):
        """Change a profile's type ('production' or 'development') and/or
        read-only flag; the saved password and endpoint stay as they are."""
        safety = _check_safety(connection_type, read_only)
        if connection_type is None and read_only is None:
            raise ValueError('Choose a connection type or the read-only setting to change.')
        if not isinstance(key, str) or not self.path.exists():
            raise ValueError('The connection profile no longer exists.')
        conn = self._transaction()
        try:
            kind, existing = self._record(conn, key)
            if kind != 'profile':
                raise ValueError('The connection profile no longer exists.')
            existing.update(_safety_fields(safety, existing))
            conn.execute('UPDATE records SET updated=?, payload=? WHERE id=?',
                         (_now(), json.dumps(existing, allow_nan=False), key))
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
        return self.get('profile', key)

    def forget_password(self, key):
        """Delete a profile's saved password and record that none is saved."""
        self._remove_profile_secret(key, delete_record=False)
        return self.get('profile', key)

    def delete(self, kind, key):
        if self.path.exists():
            if kind == 'profile':
                self._remove_profile_secret(str(key), delete_record=True)
                return {'deleted': True}
            with closing(self._connect()) as conn, conn:
                conn.execute('DELETE FROM records WHERE id=? AND kind=?', (str(key), kind))
                if kind == 'investigation':
                    conn.execute('DELETE FROM investigation_summaries WHERE id=?', (str(key),))
        return {'deleted': True}

    # -- Application update checks (app_update.py) ----------------------------

    def app_update_last_check(self):
        """The stored result of the last update check (a dict), or None.
        Never creates the database."""
        if not self.path.exists():
            return None
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'app_update_last_check'").fetchone()
        value = _loads(row[0]) if row else None
        return value if isinstance(value, dict) else None

    def set_app_update_last_check(self, record):
        """Keep *record* (app_update.last_check_record) as the last check."""
        if not isinstance(record, dict) or not isinstance(record.get('checked_at'), str):
            raise ValueError('Expected an update check record.')
        text = json.dumps(record, allow_nan=False, sort_keys=True)
        if len(text) > 4096:
            raise ValueError('The update check record is too large.')
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('app_update_last_check', ?)", (text,))
        return record

    # -- Home news (news.py): Settings, "Show MySQL and MariaDB news on Home" --

    def news_enabled(self):
        """True unless the user turned news off (the default is on). Never creates the database."""
        if not self.path.exists():
            return True
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT value FROM settings WHERE key = 'home_news'").fetchone()
        return not row or row[0] != 'off'

    def set_news_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('enabled must be true or false.')
        with closing(self._connect()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('home_news', ?)", ('on' if enabled else 'off',))
        return enabled

    # -- Audit trail of server writes (rows shaped by dbops.py) ----------------

    def audit_retention_days(self):
        """Days audit records are kept (default 90). Never creates the database."""
        if not self.path.exists():
            return AUDIT_DEFAULT_RETENTION_DAYS
        with closing(self._connect()) as conn, conn:
            return _retention(conn)

    def set_audit_retention_days(self, days):
        """Keep audit records for *days* (1 to 3650) and purge older ones now."""
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= AUDIT_MAX_RETENTION_DAYS:
            raise ValueError('Keep audit records for 1 to %d days.' % AUDIT_MAX_RETENTION_DAYS)
        conn = self._transaction()
        try:
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('audit_retention_days', ?)", (str(days),))
            conn.execute('DELETE FROM audit WHERE attempted_at < ?', (_cutoff(days),))
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
        return days

    def audit_insert(self, row):
        """Insert one audit row, first purging rows older than the retention."""
        columns = [c for c in AUDIT_COLUMNS if c in row]
        conn = self._transaction()
        try:
            conn.execute('DELETE FROM audit WHERE attempted_at < ?', (_cutoff(_retention(conn)),))
            conn.execute('INSERT INTO audit (%s) VALUES (%s)' % (', '.join(columns), ', '.join('?' * len(columns))),
                         [row[c] for c in columns])
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
        return row['id']

    def audit_complete(self, audit_id, fields):
        """Finish a pending row. Returns False when it is gone or already finished."""
        columns = [c for c in AUDIT_COLUMNS if c in fields and c != 'id']
        if not self.path.exists() or not columns:
            return False
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute('UPDATE audit SET %s WHERE id = ? AND status = ?'
                                  % ', '.join('%s = ?' % c for c in columns),
                                  [fields[c] for c in columns] + [audit_id, 'pending'])
            return cursor.rowcount == 1

    def audit_records(self, since_days=None, server=None, limit=None, status=None, before=None):
        """Audit records, newest first (audit-1.0 shape). *server* matches the
        saved connection name, the host, or host:port, ignoring case. *status*
        keeps one audit status; like every filter it applies before *limit*.
        *before* is the id of a record already listed: only records older than
        it are returned (the next page)."""
        from .dbops import AUDIT_STATUSES
        if status is not None and status not in AUDIT_STATUSES:
            raise ValueError('status must be one of: %s.' % ', '.join(AUDIT_STATUSES))
        if before is not None and (not isinstance(before, str) or not 0 < len(before) <= 64):
            raise ValueError('before must be the id of an audit record.')
        if since_days is not None and (isinstance(since_days, bool) or not isinstance(since_days, (int, float))
                                       or not 0 < since_days <= 36500):
            raise ValueError('since_days must be a number of days from 1 to 36500.')
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000):
            raise ValueError('limit must be an integer from 1 to 100000.')
        if server is not None and (not isinstance(server, str) or len(server) > 300):
            raise ValueError('server must be text.')
        if not self.path.exists():
            return []
        where, args = [], []
        if since_days is not None:
            where.append('attempted_at >= ?')
            args.append(_cutoff(since_days))
        if server:
            where.append("(lower(profile_name) = ? OR lower(host) = ? OR lower(host || ':' || port) = ?)")
            args.extend([server.lower()] * 3)
        if status is not None:
            where.append('status = ?')
            args.append(status)
        with closing(self._connect()) as conn, conn:
            if before is not None:
                # Keyset paging in the list's own order (attempted_at, then rowid).
                mark = conn.execute('SELECT attempted_at, rowid FROM audit WHERE id = ?', (before,)).fetchone()
                if mark is None:
                    raise ValueError('That audit record is no longer kept. Refresh the list.')
                where.append('(attempted_at < ? OR (attempted_at = ? AND rowid < ?))')
                args.extend([mark[0], mark[0], mark[1]])
            sql = 'SELECT %s FROM audit%s ORDER BY attempted_at DESC, rowid DESC' % (
                ', '.join(AUDIT_COLUMNS), ' WHERE ' + ' AND '.join(where) if where else '')
            if limit:
                sql += ' LIMIT %d' % limit
            rows = conn.execute(sql, args).fetchall()
        return [_audit_record(dict(zip(AUDIT_COLUMNS, row))) for row in rows]

    def audit_purge(self, older_than_days):
        """Delete audit records older than *older_than_days* (0 deletes all). Returns the count."""
        if isinstance(older_than_days, bool) or not isinstance(older_than_days, (int, float)) \
                or not 0 <= older_than_days <= 36500:
            raise ValueError('Choose a number of days from 0 to 36500.')
        if not self.path.exists():
            return 0
        with closing(self._connect()) as conn, conn:
            if older_than_days == 0:
                return conn.execute('DELETE FROM audit').rowcount
            return conn.execute('DELETE FROM audit WHERE attempted_at < ?', (_cutoff(older_than_days),)).rowcount

    # -- Query editor history (records of kind 'editor_history', editor_session.py) ----

    def history_add(self, entry, max_entries, max_bytes):
        """Add one run to the Query editor history and drop the oldest runs
        beyond *max_entries* or *max_bytes* (stored JSON). Returns its ID.
        The entry is built by editor_session.history_entry: redacted SQL, the
        connection's name, host and port, never a password."""
        if not isinstance(entry, dict):
            raise ValueError('A history entry must be an object.')
        clean = {k: v for k, v in entry.items() if str(k).lower() not in SECRET_KEYS}
        payload = json.dumps(clean, allow_nan=False, ensure_ascii=False)
        key = uuid.uuid4().hex
        conn = self._transaction()
        try:
            conn.execute('INSERT INTO records VALUES (?, ?, ?, ?)', (key, HISTORY_KIND, _now(), payload))
            rows = conn.execute('SELECT id, length(payload) FROM records WHERE kind = ? '
                                'ORDER BY updated DESC, rowid DESC', (HISTORY_KIND,)).fetchall()
            total, drop = 0, []
            for index, (record_id, size) in enumerate(rows):
                total += size or 0
                if index >= max_entries or (total > max_bytes and record_id != key):
                    drop.append(record_id)
            for start in range(0, len(drop), 500):
                part = drop[start:start + 500]
                conn.execute('DELETE FROM records WHERE kind = ? AND id IN (%s)' % ', '.join('?' * len(part)),
                             [HISTORY_KIND] + part)
            conn.execute('COMMIT')
        except BaseException:
            if conn.in_transaction:
                conn.execute('ROLLBACK')
            raise
        finally:
            conn.close()
        return key

    def history_list(self, limit=200, profile_id=None):
        """Query editor history, newest first; *profile_id* keeps one saved connection's runs."""
        if not self.path.exists():
            return []
        with closing(self._connect()) as conn, conn:
            rows = conn.execute('SELECT id, updated, payload FROM records WHERE kind = ? '
                                'ORDER BY updated DESC, rowid DESC', (HISTORY_KIND,)).fetchall()
        items = []
        for key, updated, payload in rows:
            try:
                value = json.loads(payload)
            except ValueError:
                continue
            if profile_id and (value.get('connection') or {}).get('profile_id') != profile_id:
                continue
            value['id'] = key
            items.append(value)
            if len(items) >= limit:
                break
        return items

    def history_clear(self):
        """Delete the whole Query editor history. Returns the count."""
        if not self.path.exists():
            return 0
        with closing(self._connect()) as conn, conn:
            return conn.execute('DELETE FROM records WHERE kind = ?', (HISTORY_KIND,)).rowcount

    def connection_with_saved_password(self, value):
        """Fill ``password`` from the profile named by ``value['profile_id']``.

        Only when the request has no password, the profile remembers one, and
        host, port, user, TLS mode, and CA path still match the profile, and
        the endpoint fingerprint in the secret's key matches them too. Returns
        a new dict; the caller uses it for one request and must not persist it.
        """
        from .live import connection_options
        if not isinstance(value, dict):
            raise ValueError('Enter connection settings.')
        for field in ('binary', 'socket', 'ssl_cert', 'ssl_key', 'init_command', 'defaults_file', 'extra_flags'):
            if field in value:
                raise ValueError('Unsupported saved connection field: ' + field)
        clean = connection_options(value)
        safety = _check_safety(value.get('type'), value.get('read_only'))
        for field in PROFILE_SAFETY_FIELDS:
            if safety[field] is not None:
                clean[field] = safety[field]
        profile_id = value.get('profile_id')
        if profile_id not in (None, ''):
            if not isinstance(profile_id, str) or len(profile_id) > 200:
                raise ValueError('Invalid connection field: profile_id')
            clean['profile_id'] = profile_id
        value = clean
        if not profile_id:
            return value
        if value.get('password'):
            return value
        # One read: the endpoint and the secret reference come from the same
        # committed record.
        profile = self._profile(profile_id)
        if profile is None:
            raise ValueError('The saved connection profile no longer exists. Choose it again or enter the password.')
        if not profile.get('remember_password'):
            return value
        from . import secret_store
        name = profile.get('name') or profile_id
        if not _same_endpoint(value, profile):
            raise ValueError('The saved password for %r is only sent to its own host, port, user, and '
                             'TLS settings. Save the profile or enter the password.' % name)
        ref = profile.get('secret_ref')
        if ref is not None and not _ref_matches(ref, profile_id, profile):
            raise ValueError('The saved password for %r was saved for another host, port, user, or TLS '
                             'settings. Enter the password and save the profile again.' % name)
        backend = profile.get('secret_backend') or None
        secret = secret_store.get_secret(ref or profile_id, backend)
        if secret is None:
            raise ValueError('The saved password for %r is missing from the %s. Enter it and save the profile again.'
                             % (name, secret_store.backend_label(backend)))
        result = dict(value)
        result['password'] = secret
        return result


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


AUDIT_COLUMNS = ('id', 'attempted_at', 'finished_at', 'status', 'outcome', 'surface', 'os_user', 'operation',
                 'profile_id', 'profile_name', 'connection_type', 'read_only', 'host', 'port', 'db_user', 'engine',
                 'server_version', 'statements', 'target', 'verified', 'policy', 'error_code', 'message',
                 'rows_affected', 'after', 'duration_ms')
AUDIT_DEFAULT_RETENTION_DAYS = 90
AUDIT_MAX_RETENTION_DAYS = 3650


def _cutoff(days):
    moment = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    return moment.isoformat(timespec='microseconds')


def _retention(conn):
    row = conn.execute("SELECT value FROM settings WHERE key = 'audit_retention_days'").fetchone()
    try:
        days = int(row[0]) if row else AUDIT_DEFAULT_RETENTION_DAYS
    except (TypeError, ValueError):
        days = AUDIT_DEFAULT_RETENTION_DAYS
    return days if 1 <= days <= AUDIT_MAX_RETENTION_DAYS else AUDIT_DEFAULT_RETENTION_DAYS


def _loads(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _audit_record(row):
    """One audit row in the audit-1.0 record shape."""
    return {
        'id': row['id'], 'attempted_at': row['attempted_at'], 'finished_at': row['finished_at'],
        'status': row['status'], 'outcome': row['outcome'],
        # pending: myflames stopped before it could record the result; unknown:
        # it was interrupted (or timed out) after the statement may have been
        # sent. Either way, whether the server ran it is unknown.
        'outcome_unknown': row['status'] in ('pending', 'unknown'),
        'surface': row['surface'], 'os_user': row['os_user'], 'operation': row['operation'],
        'connection': {'profile_id': row['profile_id'], 'profile_name': row['profile_name'],
                       'type': row['connection_type'],
                       'read_only': None if row['read_only'] is None else bool(row['read_only']),
                       'host': row['host'], 'port': row['port'], 'user': row['db_user'],
                       'engine': row['engine'], 'server_version': row['server_version']},
        'statements': _loads(row['statements']) or [], 'target': _loads(row['target']),
        'verified': _loads(row['verified']), 'policy': _loads(row['policy']),
        'error_code': row['error_code'], 'message': row['message'], 'rows_affected': row['rows_affected'],
        'after': _loads(row['after']), 'duration_ms': row['duration_ms'],
    }


def _check_safety(connection_type, read_only):
    """Validated {'type', 'read_only'} from a request; None where not given."""
    if connection_type is not None and (not isinstance(connection_type, str)
                                        or connection_type.lower() not in CONNECTION_TYPES):
        raise ValueError("Choose connection type 'production' or 'development'.")
    if read_only is not None and not isinstance(read_only, bool):
        raise ValueError('read_only must be true or false.')
    return {'type': connection_type.lower() if connection_type else None, 'read_only': read_only}


def _safety_fields(requested, existing):
    """type and read_only to store: the requested values, else the existing
    ones, else production and read-write."""
    ctype = requested.get('type') if requested.get('type') in CONNECTION_TYPES else existing.get('type')
    read_only = requested.get('read_only')
    if not isinstance(read_only, bool):
        read_only = bool(existing.get('read_only'))
    return {'type': ctype if ctype in CONNECTION_TYPES else 'production', 'read_only': read_only}


def _owner_only(path, mode):
    """Enforce owner-only storage on POSIX; Windows has no POSIX mode contract."""
    if os.name != 'posix':
        return
    from .secret_store import _protect_private_path
    _protect_private_path(path, mode, directory=mode == 0o700)


def _same_endpoint(a, b):
    return all((a.get(f) or '') == (b.get(f) or '') for f in ENDPOINT_FIELDS)


def _endpoint_fingerprint(value):
    """Hash of the host, port, user and TLS settings (ENDPOINT_FIELDS)."""
    text = '\x00'.join(str(value.get(f) or '') for f in ENDPOINT_FIELDS)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]


def _ref_base(key):
    # Secret store keys are at most 200 characters (secret_store.KEY_PATTERN).
    return key if len(key) <= 150 else hashlib.sha256(key.encode('utf-8')).hexdigest()[:32]


def _new_secret_ref(key, value):
    """Secret store key for a password saved now for *value*'s endpoint."""
    return '%s.%s.%s' % (_ref_base(key), _endpoint_fingerprint(value), uuid.uuid4().hex[:12])


def _ref_matches(ref, key, value):
    """True when *ref* belongs to profile *key* and was saved for *value*'s endpoint."""
    parts = ref.rsplit('.', 2) if isinstance(ref, str) else []
    return (len(parts) == 3 and parts[0] == _ref_base(key)
            and hmac.compare_digest(parts[1], _endpoint_fingerprint(value)))


def _stored_ref(key, record):
    """Where a record's saved password is: its secret_ref, or the profile ID
    for passwords saved before secret_ref existed."""
    return record.get('secret_ref') or key


def _delete_quietly(ref, backend):
    """Remove a secret no record refers to any more; a failure leaves an
    unreferenced entry, never a wrong pairing."""
    from . import secret_store
    try:
        secret_store.delete_secret(ref, backend or None)
    except ValueError:
        pass


def _present(kind, record):
    """Derived, secret-free fields for API/CLI consumers."""
    if kind == 'profile':
        from .secret_store import backend_label
        record.pop('secret_ref', None)
        record.setdefault('remember_password', False)
        record.setdefault('secret_backend', '')
        record['has_saved_password'] = bool(record['remember_password'])
        record['secret_backend_label'] = backend_label(record['secret_backend']) if record['secret_backend'] else ''
        record.update(_safety_fields(record, record))
    return record
