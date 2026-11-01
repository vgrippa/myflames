"""Credential-free parameter snapshots and read-only source/target comparison."""
import copy
import datetime
import json
import re
import time
from decimal import Decimal, InvalidOperation

from . import dbops
from .connector import ConnectorError
from .perf_common import now

SNAPSHOT_SCHEMA = 'parameter-snapshot-1.0'
COMPARE_SCHEMA = 'parameter-compare-1.0'
READ_SECONDS = 120
MAX_ITEMS = 10000
MAX_FILE_BYTES = 8 * 1024 * 1024
STATUS_TEXT = {'different': 'Different', 'only_in_source': 'Only in source',
               'only_in_target': 'Only in target', 'identical': 'Identical', 'not_compared': 'Not compared'}
# One inventory for folding server identity and installation-specific locations.
IDENTITY_NAMES = frozenset(('server_id', 'server_uuid', 'hostname', 'port', 'extra_port', 'admin_port',
    'pid_file', 'socket', 'datadir', 'basedir', 'tmpdir', 'report_host', 'report_port', 'report_user',
    'gtid_executed', 'gtid_purged', 'gtid_binlog_pos', 'gtid_current_pos', 'gtid_slave_pos',
    'log_error', 'log_bin_basename', 'log_bin_index', 'relay_log', 'relay_log_basename', 'relay_log_index',
    'general_log_file', 'slow_query_log_file', 'ssl_ca', 'ssl_capath', 'ssl_cert', 'ssl_key', 'ssl_crl', 'ssl_crlpath', 'secure_file_priv', 'plugin_dir', 'lc_messages_dir',
    'innodb_data_home_dir', 'innodb_log_group_home_dir', 'innodb_undo_directory', 'wsrep_node_name',
    'wsrep_node_address', 'wsrep_cluster_address', 'wsrep_data_home_dir'))
_ITEM_FIELDS = ('name', 'value', 'default', 'type', 'values', 'apply_type', 'dynamic', 'source',
                'source_label', 'scope', 'modifiable', 'group', 'truncated', 'redacted')
_SERVER_FIELDS = ('host', 'port', 'engine', 'flavor', 'server_product', 'server_version', 'profile_id', 'profile_name')


class BoundedConnection(object):
    """Keep every client call inside one wall-clock budget, including role expansion."""
    def __init__(self, conn, seconds=READ_SECONDS):
        self.conn, self.deadline = conn, time.monotonic() + seconds

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def run(self, sql, extra_flags=None, timeout=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorError('The comparison read time budget expired.')
        return self.conn.run(sql, extra_flags=extra_flags,
                             timeout=min(remaining, timeout or getattr(self.conn, 'query_timeout', 30) or 30))

    def server_version(self):
        # Do not delegate to the underlying method: it would bypass the budget.
        from .connector import unescape_mysql
        text = self.run('SELECT @@version').strip().split('\n')[0]
        return unescape_mysql(text)


# Names describe policy, paths and plugin tuning too, so never infer a secret
# from a name fragment. This explicit inventory includes plugin credentials;
# even a server-masked value remains unavailable for comparison.
_SECRET_VARIABLES = frozenset((
    'wsrep_sst_auth', 'report_password', 'audit_log_encryption_password',
    'keyring_encrypted_file_password', 'keyring_hashicorp_secret_id', 's3_secret_key',
    'handlersocket_plain_secret', 'handlersocket_plain_secret_wr', 'utility_user_password',
    'telemetry.otel_exporter_otlp_logs_secret_headers',
    'telemetry.otel_exporter_otlp_metrics_secret_headers',
    'telemetry.otel_exporter_otlp_traces_secret_headers'))
_CREDENTIAL_URL = re.compile(r'\b[a-z][a-z0-9+.-]*://[^\s/@:]*:[^\s/@]+@', re.I)
_CREDENTIAL_PAIR = re.compile(r'[^\s:/\\;=@]+:[^\s:/\\;]+')

# Earlier snapshots inherited these config-review explanations. Remove the
# known boilerplate on import as well as fixing capture's note provenance.
_CONFIG_NOTE_PREFIXES = ('Counters run from server start.', 'Statuses are rules of thumb,',
                         'The server started ', 'innodb_log_file_size is set explicitly;')


def sensitive(name):
    return name.lower() in _SECRET_VARIABLES


def _redact_parameter_value(value):
    if not isinstance(value, str):
        return value
    # Bare host:port and Windows paths are not evidence of credentials. Secret
    # variables still redact numeric passwords through the explicit inventory.
    pair = _CREDENTIAL_PAIR.fullmatch(value.strip())
    if _CREDENTIAL_URL.search(value) or pair and not pair.group().split(':', 1)[1].isdigit():
        return dbops.REDACTED
    return dbops.redact(dbops.strip_credentials(value))


def normalize(value, kind=None):
    """Normalize typed settings, keeping case-sensitive strings and paths intact."""
    if value is None:
        return None
    text = str(value)
    if kind in ('boolean', 'bool', 'bytes', 'integer', 'double', 'numeric', 'enum', 'set'):
        text = text.strip()
    upper = text.strip().upper()
    if kind in ('boolean', 'bool') or (kind not in ('string', 'enum', 'set') and upper in ('ON', 'OFF', 'TRUE', 'FALSE')):
        if upper in ('ON', 'TRUE', '1'):
            return ('boolean', True)
        if upper in ('OFF', 'FALSE', '0'):
            return ('boolean', False)
    if kind in ('bytes', 'integer', 'double', 'numeric'):
        match = re.fullmatch(r'\s*([+-]?\d+(?:\.\d+)?)\s*([KMGTPE](?:I?B)?)?\s*', text, re.I)
        if match and (not match.group(2) or kind == 'bytes'):
            try:
                amount = Decimal(match.group(1))
                if match.group(2):
                    amount *= 1024 ** ('KMGTPE'.index(match.group(2)[0].upper()) + 1)
                return ('number', amount)
            except InvalidOperation:
                pass
    if kind in ('enum', 'set'):
        return ('text', text.casefold())
    return ('text', text)


def validate_snapshot(value):
    """Validate and project untrusted files and stored values through a secret-free allowlist."""
    if not isinstance(value, dict) or value.get('schema_version') != SNAPSHOT_SCHEMA:
        raise ValueError('Expected a parameter-snapshot-1.0 object.')
    server, items = value.get('server'), value.get('items')
    if not isinstance(server, dict) or not isinstance(server.get('host'), str) or type(server.get('port')) is not int:
        raise ValueError('A parameter snapshot needs a server host and port.')
    if not 1 <= server['port'] <= 65535 or server.get('engine') not in ('mysql', 'mariadb'):
        raise ValueError('Invalid parameter snapshot server.')
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError('A parameter snapshot needs at most %d items.' % MAX_ITEMS)
    clean = {'schema_version': SNAPSHOT_SCHEMA, 'server': {k: server[k] for k in _SERVER_FIELDS if k in server},
             'captured_at': value.get('captured_at') or now(), 'label': value.get('label') or '',
             'complete': value.get('complete') is True, 'notes': list(value.get('notes') or []), 'items': []}
    if not isinstance(clean['label'], str) or len(clean['label']) > 200:
        raise ValueError('Use a snapshot label of at most 200 characters.')
    if not isinstance(clean['captured_at'], str) or not isinstance(clean['notes'], list) or not all(isinstance(n, str) for n in clean['notes']):
        raise ValueError('Invalid snapshot timestamp or notes.')
    for key, item in clean['server'].items():
        if key != 'port' and item is not None and not isinstance(item, str):
            raise ValueError('Invalid server metadata.')
    if not isinstance(value.get('notes', []), list) or type(value.get('complete')) is not bool:
        raise ValueError('Invalid snapshot notes or completeness.')
    clean['notes'] = [n for n in clean['notes'] if not n.startswith(_CONFIG_NOTE_PREFIXES)]
    try:
        captured = datetime.datetime.fromisoformat(clean['captured_at'].replace('Z', '+00:00'))
        if captured.tzinfo is None:
            raise ValueError()
        clean['captured_at'] = captured.astimezone(datetime.timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise ValueError('Use an ISO 8601 snapshot timestamp with a time zone.') from None
    names = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not re.fullmatch(r'[a-zA-Z0-9_.]+', item['name']):
            raise ValueError('Invalid parameter name.')
        name = item['name'].lower()
        if name in names:
            raise ValueError('Duplicate parameter name.')
        names.add(name)
        row = {k: copy.deepcopy(item[k]) for k in _ITEM_FIELDS if k in item}
        row['name'] = name
        if 'value' not in row:
            raise ValueError('Every parameter needs a raw value; use null for SQL NULL.')
        row.setdefault('default', None)
        if 'dynamic' not in row and row.get('apply_type') in ('dynamic', 'static'):
            row['dynamic'] = row['apply_type'] == 'dynamic'
        for key in ('type', 'apply_type', 'source', 'source_label', 'scope', 'modifiable', 'group'):
            if row.get(key) is not None and not isinstance(row[key], str):
                raise ValueError('Invalid parameter metadata.')
        if row.get('values') is not None and (not isinstance(row['values'], list) or
                not all(isinstance(v, str) for v in row['values'])):
            raise ValueError('Invalid parameter allowed values.')
        for key in ('dynamic', 'truncated', 'redacted'):
            if row.get(key) is not None and type(row[key]) is not bool:
                raise ValueError('Invalid parameter state.')
        if sensitive(name):
            row['values'] = None
        for key in ('value', 'default'):
            if row.get(key) is not None and not isinstance(row[key], (str, int, float, bool)):
                raise ValueError('Parameter values must be scalar.')
            if sensitive(name):
                row[key], row['redacted'] = '[redacted]', True
            elif isinstance(row.get(key), str):
                row[key] = _redact_parameter_value(row[key])
                if row[key] != item.get(key):
                    row['redacted'] = True
        if row.get('values') is not None:
            if any(_redact_parameter_value(v) != v for v in row['values']):
                row['values'], row['redacted'] = None, True
        clean['items'].append(row)
    # Reject excessive or non-JSON data before persistence or comparison.
    if len(json.dumps(clean, allow_nan=False).encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('The parameter snapshot exceeds 8 MiB.')
    return dbops.redact_value(clean)


def capture(conn, server=None, label=''):
    from .config_review import config_report
    report = config_report(BoundedConnection(conn), parameters=True)
    identity = {k: (server or {}).get(k) for k in ('host', 'port')}
    identity.update(host=identity['host'] or getattr(conn, 'host', 'localhost'),
                    port=identity['port'] or getattr(conn, 'port', 3306))
    identity.update(engine=report['engine'], flavor=report.get('flavor'),
                    server_product=report.get('server_product') or report.get('product'),
                    server_version=report.get('server_version'))
    identity.update({k: server[k] for k in ('profile_id', 'profile_name') if server and k in server})
    catalog = report['parameters']
    # parameters-1.0 has check associations rather than a group field today.
    groups = {c['key']: c['group'] for c in report.get('checks', [])}
    items = [dict(i) for i in catalog['items']]
    for item in items:
        group = next((groups[k] for k in item.get('checks', []) if k in groups), None)
        if group is not None:
            item['group'] = group
    return validate_snapshot({'schema_version': SNAPSHOT_SCHEMA, 'server': identity,
                              'captured_at': now(), 'label': label, 'complete': catalog['complete'],
                              'notes': catalog.get('notes', []), 'items': items})


def source_key(snapshot):
    return json.dumps([snapshot['server']['host'], snapshot['server']['port']], separators=(',', ':'))


def failed_side(error):
    return {'error': str(error), 'complete': False, 'items': [], 'notes': []}


def side_metadata(side, meta=None):
    out = dict(meta or {})
    out.update({k: copy.deepcopy(side[k]) for k in ('server', 'captured_at', 'label', 'complete', 'error') if k in side})
    return out


def compare(source, target, source_meta=None, target_meta=None):
    sides = [validate_snapshot(s) if not s.get('error') else s for s in (source, target)]
    notes = []
    for label, side in zip(('Source', 'Target'), sides):
        notes.extend(label + ': ' + n for n in side.get('notes', []))
        if side.get('error'):
            notes.append(label + ': ' + side['error'])
    if all(s.get('server') for s in sides) and sides[0]['server']['engine'] != sides[1]['server']['engine']:
        notes.append('Source and target use different engines (MySQL and MariaDB); matching names may have different meanings.')
    indexed = [{i['name']: i for i in s.get('items', [])} for s in sides]
    rows = []
    for name in sorted(set(indexed[0]) | set(indexed[1])):
        a, b = (i.get(name) for i in indexed)
        reason = None
        if any(s.get('error') for s in sides):
            reason = 'A comparison side could not be read.'
        elif any(i and (i.get('truncated') or i.get('redacted')) for i in (a, b)):
            reason = 'A value was truncated or redacted.'
        elif a is None and not sides[0].get('complete') or b is None and not sides[1].get('complete'):
            reason = 'The missing side has an incomplete parameter inventory.'
        kind = (a or {}).get('type') or (b or {}).get('type')
        status = ('not_compared' if reason else 'only_in_target' if a is None else 'only_in_source' if b is None else
                  'identical' if normalize(a.get('value'), kind) == normalize(b.get('value'), kind) else 'different')
        row = {'name': name, 'status': status, 'status_text': STATUS_TEXT[status], 'identity': name in IDENTITY_NAMES,
               'group': (a or {}).get('group') or (b or {}).get('group'), 'source': a, 'target': b}
        for label, item in (('source', a), ('target', b)):
            row[label + '_at_default'] = (normalize(item.get('value'), item.get('type')) == normalize(item['default'], item.get('type'))
                                          if item and item.get('default') is not None and not item.get('redacted') else None)
        if reason:
            row['reason'] = reason
        rows.append(row)
    return {'schema_version': COMPARE_SCHEMA, 'compared_at': now(), 'source': side_metadata(sides[0], source_meta),
            'target': side_metadata(sides[1], target_meta), 'items': rows, 'notes': notes,
            'counts': {s: sum(r['status'] == s for r in rows) for s in STATUS_TEXT},
            'identical': all(s.get('complete') and not s.get('error') for s in sides) and all(r['status'] == 'identical' for r in rows),
            'complete': all(s.get('complete') and not s.get('error') for s in sides)}
