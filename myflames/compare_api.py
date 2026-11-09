"""Read-only parameter/account side resolution and workspace endpoints.

Sides follow Schema versions: {live: {connection: {...}}}. Named saved
connections use {profile: NAME_OR_ID}; parameters additionally accept
{snapshot: ID_OR_latest} and {file: TEXT}, a JSON snapshot's contents (never
an arbitrary backend filesystem path). Each live side fills its own saved
password. A failed read becomes a side error, preserving the other side.
"""
import json

from . import account_compare, param_compare, dbops
from .connector import ConnectorError, MySQLConnection
from .live import connection_options
from .schema_versions_api import _connection
from .workspace_store import PROFILE_FIELDS

SNAPSHOT_PREFIX = '/api/config/snapshots/'
PATHS = tuple(SNAPSHOT_PREFIX + s for s in ('list', 'save', 'get', 'delete')) + (
    '/api/config/compare', '/api/accounts/compare', '/api/accounts/export')
LIVE_PATHS = (SNAPSHOT_PREFIX + 'save', '/api/config/compare', '/api/accounts/compare', '/api/accounts/export')


def parse_file(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > param_compare.MAX_FILE_BYTES:
        raise ValueError('Read a parameter-snapshot-1.0 JSON file of at most 8 MiB.')
    try:
        return param_compare.validate_snapshot(json.loads(text))
    except (ValueError, TypeError, RecursionError):
        raise ValueError('Invalid parameter-snapshot-1.0 file.') from None


def profile_connection(store, ref):
    if not isinstance(ref, str) or not ref or len(ref) > 200:
        raise ValueError('Choose a saved connection by name or ID.')
    profile = store.find_profile(ref)
    return dict({f: profile[f] for f in PROFILE_FIELDS if f != 'name' and profile.get(f) not in (None, '')},
                profile_id=profile['id'])


def read_live(value, store, with_saved_password, kind='parameters'):
    if not isinstance(value, dict):
        raise ValueError('Choose a connection.')
    connection = _connection(value.get('connection'))
    if with_saved_password is None:
        raise ValueError('Reading a server needs a running workspace.')
    password = connection.get('password')
    try:
        filled = with_saved_password({'connection': connection})['connection']
        password = filled.get('password')
        options = connection_options(filled)
        options['query_timeout'] = 30
        server = {'host': options['host'], 'port': options['port']}
        profile_id = connection.get('profile_id')
        if profile_id:
            server['profile_id'] = profile_id
            profile = store.get('profile', profile_id)
            if profile:
                server['profile_name'] = profile['name']
        module = account_compare if kind == 'accounts' else param_compare
        with MySQLConnection(**options) as conn:
            return dbops.redact_value(module.capture(conn, server), password)
    except (ConnectorError, ValueError, OSError) as exc:
        message = dbops.redact(str(exc), password)
        raise ValueError(message) from None


def side(value, store, with_saved_password, kind='parameters', source=None):
    if not isinstance(value, dict) or len(value) != 1:
        raise ValueError('Choose one live, profile, snapshot or file side.')
    if 'live' in value:
        return read_live(value['live'], store, with_saved_password, kind), {'kind': 'live'}
    if 'profile' in value:
        connection = profile_connection(store, value['profile'])
        return read_live({'connection': connection}, store, with_saved_password, kind), {'kind': 'live'}
    if kind == 'parameters':
        if 'snapshot' in value:
            if not isinstance(value['snapshot'], str):
                raise ValueError('Choose a snapshot ID or latest.')
            item = store.parameter_snapshot_find(value['snapshot'], source)
            return item['snapshot'], dict(kind='snapshot', **{k: item[k] for k in ('id', 'number', 'label', 'saved_at')})
        if 'file' in value:
            return parse_file(value['file']), {'kind': 'file'}
    raise ValueError('Accounts use live or profile sides; parameters also accept snapshot or file sides.')


def read_pair(source, target, store, with_saved_password, kind='parameters'):
    values = [source, target]
    if source == target == {'snapshot': 'latest'}:
        raise ValueError('Both sides cannot be latest; choose a snapshot, file or live side.')
    resolved = [None, None]
    # Resolve latest relative to the opposite endpoint, not the newest snapshot of another server.
    order = [1, 0] if source == {'snapshot': 'latest'} else [0, 1]
    for i in order:
        endpoint = None
        other = resolved[1 - i]
        if values[i] == {'snapshot': 'latest'} and other and other[0].get('server'):
            endpoint = param_compare.source_key(other[0])
        try:
            if values[i] == {'snapshot': 'latest'} and other and not endpoint:
                raise ValueError('Choose a snapshot ID because the other side could not identify its server.')
            resolved[i] = side(values[i], store, with_saved_password, kind, endpoint)
        except (ValueError, ConnectorError, OSError) as exc:
            resolved[i] = (param_compare.failed_side(str(exc)), {'kind': 'unavailable'})
    return resolved


def response(path, payload, store, with_saved_password=None):
    if not isinstance(payload, dict) or store is None:
        raise ValueError('Comparisons need a request object and a running workspace.')
    if path == SNAPSHOT_PREFIX + 'list':
        return store.parameter_snapshot_list(payload.get('source_key'), payload.get('limit', 1000))
    if path == SNAPSHOT_PREFIX + 'save':
        label = payload.get('label', '')
        if not isinstance(label, str) or len(label) > 200:
            raise ValueError('Use a snapshot label of at most 200 characters.')
        return store.parameter_snapshot_save(read_live(payload, store, with_saved_password), label)
    if path in (SNAPSHOT_PREFIX + 'get', SNAPSHOT_PREFIX + 'delete'):
        if not isinstance(payload.get('id'), str) or not payload['id']:
            raise ValueError('Choose a parameter snapshot ID.')
        item = store.parameter_snapshot_find(payload['id'])
        return store.parameter_snapshot_delete(item['id']) if path.endswith('/delete') else item
    if path in ('/api/config/compare', '/api/accounts/compare'):
        kind = 'accounts' if path == '/api/accounts/compare' else 'parameters'
        pair = read_pair(payload.get('source'), payload.get('target'), store, with_saved_password, kind)
        module = account_compare if kind == 'accounts' else param_compare
        return module.compare(pair[0][0], pair[1][0], pair[0][1], pair[1][1])
    if path == '/api/accounts/export':
        value, meta = side(payload.get('side'), store, with_saved_password, 'accounts')
        return {'filename': 'account-grants.sql', 'text': account_compare.export_sql(value, payload.get('accounts')),
                'side': param_compare.side_metadata(value, meta)}
    raise ValueError('Unknown comparison operation.')
