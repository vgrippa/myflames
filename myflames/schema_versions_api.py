"""Workspace endpoints of schema versions (schema_versions.py).

list, get, update, delete, export and import use the workspace store only.
save and compare read live databases, read-only: every live side names its
own connection, and that connection's saved password is filled on the server
for that read only, so one request can compare two servers. A comparison
side is ``{"version": ID}`` or ``{"live": {"connection": {...}, "database":
NAME}}``. lint checks a saved/file capture and optionally live metadata;
sync rebuilds from the bounded comparison cache without another server read.
export-sql builds the script that recreates a saved version or a database
read now (schema_export.py); a live read is cached like a comparison, so a
changed option rebuilds the script without reading the server again.
"""
import collections
import threading
import time
import uuid
import weakref

from . import schema_versions
from .connector import ConnectorError, MySQLConnection
from .live import connection_options

LIST_PATH = '/api/schema-versions/list'
SAVE_PATH = '/api/schema-versions/save'
COMPARE_PATH = '/api/schema-versions/compare'
IMPORT_PATH = '/api/schema-versions/import'
SYNC_PATH = '/api/schema-versions/sync'
LINT_PATH = '/api/schema-versions/lint'
EXPORT_SQL_PATH = '/api/schema-versions/export-sql'
PATHS = (LIST_PATH, '/api/schema-versions/get', SAVE_PATH, '/api/schema-versions/update',
         '/api/schema-versions/delete', '/api/schema-versions/export', IMPORT_PATH, COMPARE_PATH, SYNC_PATH, LINT_PATH,
         EXPORT_SQL_PATH)
#: These open connections to servers (read-only).
LIVE_PATHS = (SAVE_PATH, COMPARE_PATH, LINT_PATH, EXPORT_SQL_PATH)
#: A snapshot can be larger than the workspace's usual request limit.
LARGE_PATHS = (IMPORT_PATH,)


# A bounded in-memory cache scoped to the workspace instance. The browser gets
# only an opaque key; it never sends editable snapshots back as trusted DDL.
# Checkbox updates reconstruct the plan from the exact captured definitions.
_CACHE = weakref.WeakKeyDictionary()
_CACHE_LOCK = threading.Lock()
_CACHE_SECONDS = 600
_CACHE_BYTES = 64 * 1024 * 1024
_CACHE_ITEMS = 4


def _cache_put(store, source, target, report):
    item = (schema_versions.encode_snapshot(source), schema_versions.encode_snapshot(target),
            report['source'], report['target'], report['options']['ignore_definers'], report['compared_at'])
    size = len(item[0]) + len(item[1])
    if size > _CACHE_BYTES:
        raise ValueError('These schemas are too large to retain for sync selection.')
    key = uuid.uuid4().hex
    with _CACHE_LOCK:
        entries = _CACHE.setdefault(store, collections.OrderedDict())
        for old, (stamp, _, _) in list(entries.items()):
            if time.monotonic() - stamp > _CACHE_SECONDS:
                del entries[old]
        while entries and (len(entries) >= _CACHE_ITEMS or sum(v[1] for v in entries.values()) + size > _CACHE_BYTES):
            entries.popitem(last=False)
        entries[key] = (time.monotonic(), size, item)
    return key


def _cache_get(store, key):
    if not isinstance(key, str) or len(key) != 32:
        raise ValueError('Choose a comparison; run Compare again.')
    with _CACHE_LOCK:
        entries = _CACHE.get(store, {})
        cached = entries.get(key)
        if cached is None or time.monotonic() - cached[0] > _CACHE_SECONDS:
            entries.pop(key, None)
            raise ValueError('This comparison is no longer cached; run Compare again.')
        # The same captured definitions live for a bounded time, even while used.
        return cached[2]


# Live reads for Export as SQL, bounded like the comparison cache: the
# browser holds only the opaque key.
_EXPORTS = weakref.WeakKeyDictionary()


def _export_put(store, snapshot, meta):
    blob = schema_versions.encode_snapshot(snapshot)
    if len(blob) > _CACHE_BYTES:
        raise ValueError('This schema is too large to keep for Export as SQL.')
    key = uuid.uuid4().hex
    with _CACHE_LOCK:
        entries = _EXPORTS.setdefault(store, collections.OrderedDict())
        for old, (stamp, _, _) in list(entries.items()):
            if time.monotonic() - stamp > _CACHE_SECONDS:
                del entries[old]
        while entries and (len(entries) >= _CACHE_ITEMS or sum(v[1] for v in entries.values()) + len(blob) > _CACHE_BYTES):
            entries.popitem(last=False)
        entries[key] = (time.monotonic(), len(blob), (blob, meta))
    return key


def _export_get(store, key):
    if not isinstance(key, str) or len(key) != 32:
        raise ValueError('Read the database again to export it.')
    with _CACHE_LOCK:
        entries = _EXPORTS.get(store, {})
        cached = entries.get(key)
        if cached is None or time.monotonic() - cached[0] > _CACHE_SECONDS:
            entries.pop(key, None)
            raise ValueError('This read is no longer kept; read the database again to export it.')
        blob, meta = cached[2]
    return schema_versions.decode_snapshot(blob), dict(meta)


def _export_sql(payload, store, with_saved_password):
    """The Export as SQL script of a saved version (``id``), a database read
    now (``live``) or a read kept from before (``export_id``)."""
    from . import schema_export
    for name in ('create_database', 'keep_definers'):
        if not isinstance(payload.get(name, False), bool):
            raise ValueError('%s must be true or false.' % name)
    key = None
    if payload.get('export_id') is not None:
        key = payload['export_id']
        snapshot, meta = _export_get(store, key)
    elif payload.get('live') is not None:
        snapshot, meta = read_live(payload['live'], store, with_saved_password), {'kind': 'live'}
        key = _export_put(store, snapshot, meta)
    else:
        item = store.schema_version_get(_item(store, payload.get('id'))['id'])
        if item is None:
            raise ValueError('That saved schema version no longer exists.')
        snapshot, meta = item['snapshot'], {'kind': 'version', 'id': item['id'], 'number': item['number'],
                                            'name': item['name'], 'saved_at': item['saved_at']}
    result = schema_export.build(snapshot, database=payload.get('database'),
                                 create_database=payload.get('create_database', False),
                                 keep_definers=payload.get('keep_definers', False), meta=meta)
    # The script carries every statement; the list would send them twice.
    result.pop('statements')
    result['kind'] = meta.get('kind', 'version')
    result['server'] = dict(snapshot['server'])
    if key is not None:
        result['export_id'] = key
    return result


def _lint(snapshot, connection, with_saved_password):
    from . import schema_lint
    if connection is None:
        return schema_lint.lint(snapshot)
    connection = _connection(connection)
    if with_saved_password is None:
        raise ValueError('Reading a connection needs a running workspace.')
    filled = with_saved_password({'connection': connection}).get('connection')
    options = connection_options(filled)
    options['query_timeout'] = schema_versions.READ_SECONDS
    server = schema_versions.read_source(options)
    try:
        with MySQLConnection(**options) as conn:
            report = schema_lint.lint(snapshot, dict(server, connection=conn))
            # Lint contains failed-read diagnostics as Not judged instead of
            # raising. Apply the same secret rule as connection exceptions.
            if options.get('password'):
                for item in report['not_judged']:
                    item['reason'] = item['reason'].replace(options['password'], '[redacted]')
            return report
    except ConnectorError as exc:
        message = str(exc)
        if options.get('password'):
            message = message.replace(options['password'], '[redacted]')
        return schema_lint.lint(snapshot, dict(server, connection=None, error=message))


def _connection(value):
    if not isinstance(value, dict) or len(value) > 40:
        raise ValueError('Choose a connection.')
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64 or type(item) not in (str, int, bool, type(None)):
            raise ValueError('Invalid connection field.')
        if isinstance(item, str) and (len(item) > 4096 or '\x00' in item):
            raise ValueError('Invalid connection field: %s' % key)
        if type(item) is int and item.bit_length() > 64:
            raise ValueError('Invalid connection field: %s' % key)
    return value


def read_live(side, store, with_saved_password):
    """A fresh snapshot of ``{"connection", "database"}`` (never saved here)."""
    if not isinstance(side, dict):
        raise ValueError('Choose a connection and a database.')
    database = schema_versions.check_schema(side.get('database'))
    if database is None:
        raise ValueError('Choose a database.')
    connection = _connection(side.get('connection'))
    if with_saved_password is None:
        raise ValueError('Reading a database needs a running workspace.')
    filled = with_saved_password({'connection': connection}).get('connection')
    options = connection_options(filled)
    options['query_timeout'] = schema_versions.READ_SECONDS
    source = schema_versions.read_source(options)
    profile_id = connection.get('profile_id')
    if isinstance(profile_id, str) and profile_id:
        profile = store.get('profile', profile_id) if store is not None else None
        source['profile_id'] = profile_id
        if profile:
            source['profile_name'] = profile.get('name')
    password = options.get('password')
    try:
        with MySQLConnection(**options) as conn:
            return schema_versions.capture(conn, database, source)
    except (ConnectorError, ValueError) as exc:
        if password and password in str(exc):
            raise type(exc)(str(exc).replace(password, '[redacted]')) from None
        raise


def _side(value, store, with_saved_password):
    """(snapshot, meta) for one comparison side."""
    if not isinstance(value, dict):
        raise ValueError('Choose what to compare as source and target.')
    if 'version' in value:
        item = store.schema_version_get(value['version'])
        if item is None:
            raise ValueError('That saved schema version no longer exists.')
        return item['snapshot'], {'kind': 'version', 'id': item['id'], 'number': item['number'],
                                  'name': item['name'], 'saved_at': item['saved_at']}
    if 'live' in value:
        return read_live(value['live'], store, with_saved_password), {'kind': 'live'}
    raise ValueError('A side is a saved version or a live database.')


def _item(store, key):
    if not isinstance(key, str) or not key:
        raise ValueError('Choose a saved schema version.')
    item = store.schema_version_list_item(key)
    if item is None:
        raise ValueError('That saved schema version no longer exists.')
    return item


def response(path, payload, store, with_saved_password=None):
    if not isinstance(payload, dict):
        raise ValueError('Expected a schema versions request object.')
    if store is None:
        raise ValueError('Schema versions need a running workspace.')
    if path == LIST_PATH:
        source = payload.get('source_key')
        if source is not None and (not isinstance(source, str) or len(source) > 2000):
            raise ValueError('Invalid source_key.')
        return store.schema_version_list(source=source)
    if path == '/api/schema-versions/get':
        item = store.schema_version_get(_item(store, payload.get('id'))['id'])
        return item
    if path == '/api/schema-versions/update':
        return store.schema_version_update(_item(store, payload.get('id'))['id'], payload.get('name'),
                                           payload.get('note'))
    if path == '/api/schema-versions/delete':
        return store.schema_version_delete(_item(store, payload.get('id'))['id'])
    if path == '/api/schema-versions/export':
        item = store.schema_version_get(_item(store, payload.get('id'))['id'])
        name = 'schema-%s-v%d.json' % (''.join(c if c.isalnum() or c in '-_' else '_' for c in item['database'])[:64],
                                       item['number'])
        return {'filename': name, 'text': schema_versions.export_text(item)}
    if path == IMPORT_PATH:
        snapshot = schema_versions.parse_file(payload.get('text'))
        name = payload.get('name')
        return store.schema_version_save(snapshot, snapshot.get('name', '') if name is None else name,
                                         snapshot.get('note', ''))
    if path == SAVE_PATH:
        snapshot = read_live(payload, store, with_saved_password)
        return store.schema_version_save(snapshot, payload.get('name') or '', payload.get('note') or '')
    if path == LINT_PATH:
        ref = payload.get('id')
        if not isinstance(ref, str) or not ref:
            raise ValueError('Choose a saved version, latest or live to check.')
        if ref == 'live':
            snapshot = read_live(payload, store, with_saved_password)
        else:
            item = store.schema_version_find(ref)
            saved = store.schema_version_get(item['id'])
            if saved is None:
                raise ValueError('That saved schema version no longer exists.')
            snapshot = saved['snapshot']
        return _lint(snapshot, payload.get('connection'), with_saved_password)
    if path == EXPORT_SQL_PATH:
        return _export_sql(payload, store, with_saved_password)
    if path == SYNC_PATH:
        key = payload.get('comparison_id')
        source_blob, target_blob, source_meta, target_meta, ignore, compared_at = _cache_get(store, key)
        source = schema_versions.decode_snapshot(source_blob)
        target = schema_versions.decode_snapshot(target_blob)
        report = schema_versions.compare(source, target, source_meta, target_meta, ignore_definers=ignore,
                                         chosen_keys=payload.get('chosen_keys'))
        # Preserve the time and side labels of the original read.
        from .schema_sync import script
        report['compared_at'] = compared_at
        report['sync']['script'] = script(report['sync'], report['target']['label'], report['source']['label'], compared_at)
        report['comparison_id'] = key
        report['markdown'] = schema_versions.format_markdown(report)
        return report
    if path == COMPARE_PATH:
        ignore = payload.get('ignore_definers', False)
        if not isinstance(ignore, bool):
            raise ValueError('ignore_definers must be true or false.')
        target, target_meta = _side(payload.get('target'), store, with_saved_password)
        source, source_meta = _side(payload.get('source'), store, with_saved_password)
        report = schema_versions.compare(source, target, source_meta, target_meta, ignore_definers=ignore,
                                         chosen_keys=payload.get('chosen_keys'))
        report['comparison_id'] = _cache_put(store, source, target, report)
        if payload.get('markdown') is True:
            # The Markdown export, made here so the workspace never rebuilds it.
            report['markdown'] = schema_versions.format_markdown(report)
        return report
    raise ValueError('Unknown schema versions operation.')
