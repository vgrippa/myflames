"""Explicit local investigation/profile storage. Passwords never belong here."""
import datetime
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import uuid

PROFILE_FIELDS = ('name', 'host', 'port', 'user', 'database', 'ssl_mode', 'ssl_ca')


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
    from .ui import _plan
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


class WorkspaceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path.home() / '.myflames' / 'workspace.sqlite3'

    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Exclusive creation avoids broad default permissions even briefly.
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, updated TEXT NOT NULL, payload TEXT NOT NULL)')
        return conn

    def list(self, kind):
        if not self.path.exists():
            return []
        with closing(self._connect()) as conn, conn:
            rows = conn.execute('SELECT id, updated, payload FROM records WHERE kind=? ORDER BY updated DESC', (kind,)).fetchall()
        return [dict(id=key, updated_at=updated, **json.loads(payload)) for key, updated, payload in rows]

    def save(self, kind, payload, key=None):
        if kind == 'investigation':
            value = validate_bundle(payload)
        elif kind == 'profile':
            from .live import connection_options
            options = connection_options(payload)
            name = payload.get('name') or options['host']
            if not isinstance(name, str) or len(name) > 200:
                raise ValueError('Connection names must be text of at most 200 characters.')
            value = {field: options[field] for field in PROFILE_FIELDS if field in options}
            value['name'] = name
        else:
            raise ValueError('Unknown record type.')
        key = str(key or uuid.uuid4().hex)
        if len(key) > 200:
            raise ValueError('Invalid record ID.')
        updated = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with closing(self._connect()) as conn, conn:
            existing = conn.execute('SELECT kind FROM records WHERE id=?', (key,)).fetchone()
            if existing and existing[0] != kind:
                raise ValueError('Record type does not match.')
            conn.execute('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)', (key, kind, updated, json.dumps(value, allow_nan=False)))
        return dict(id=key, updated_at=updated, **value)

    def delete(self, kind, key):
        if self.path.exists():
            with closing(self._connect()) as conn, conn:
                conn.execute('DELETE FROM records WHERE id=? AND kind=?', (str(key), kind))
        return {'deleted': True}
