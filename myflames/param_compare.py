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
MAX_VALUE_CHARS = 65536
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

    def run(self, sql, extra_flags=None, timeout=None, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ConnectorError('The comparison read time budget expired.')
        return self.conn.run(sql, extra_flags=extra_flags,
                             timeout=min(remaining, timeout or getattr(self.conn, 'query_timeout', 30) or 30), **kwargs)

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
# Bound each URL field: long scheme-like text and unfinished credentials must
# not cause quadratic backtracking while processing server-controlled values.
_CREDENTIAL_URL = re.compile(r'\b[a-z][a-z0-9+.-]{0,31}://[^\s/@:]{0,255}:[^\s/@]{1,1024}@', re.I)
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
            if isinstance(row.get(key), str) and len(row[key]) > MAX_VALUE_CHARS:
                # Drop oversized values before any credential regex runs. A
                # prefix is not evidence of the complete setting's value.
                row[key], row['truncated'] = None, True
            elif sensitive(name):
                row[key], row['redacted'] = '[redacted]', True
            elif isinstance(row.get(key), str):
                row[key] = _redact_parameter_value(row[key])
                if row[key] != item.get(key):
                    row['redacted'] = True
        if row.get('values') is not None:
            if any(len(v) > MAX_VALUE_CHARS for v in row['values']):
                row['values'], row['truncated'] = None, True
            elif any(_redact_parameter_value(v) != v for v in row['values']):
                row['values'], row['redacted'] = None, True
        clean['items'].append(row)
    # Reject excessive or non-JSON data before persistence or comparison.
    if len(json.dumps(clean, allow_nan=False).encode('utf-8')) > MAX_FILE_BYTES:
        raise ValueError('The parameter snapshot exceeds 8 MiB.')
    return dbops.redact_value(clean)


def capture(conn, server=None, label='', full_provider_options=False):
    from .config_review import config_report
    from .perf_common import Reader
    conn = BoundedConnection(conn)
    report = config_report(conn, parameters=True)
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
    notes = list(catalog.get('notes', []))
    for item in items:
        if full_provider_options and item['name'] == 'wsrep_provider_options':
            # SHOW/P_S and SYSTEM_VARIABLES have narrower columns than this
            # provider's option list. Read the variable itself before parsing.
            rows = Reader(conn, '/*myflames:topology*/ ').optional(
                'SELECT @@GLOBAL.wsrep_provider_options', 'full wsrep_provider_options', notes)
            if rows and len(rows) == 1 and len(rows[0]) == 1 and rows[0][0] is not None:
                value = rows[0][0]
                oversized = len(value) > MAX_VALUE_CHARS
                item.update(value=None if oversized else value, truncated=oversized)
            else:
                # Never compare a prefix when the exact read was unavailable.
                item['truncated'] = True
        group = next((groups[k] for k in item.get('checks', []) if k in groups), None)
        if group is not None:
            item['group'] = group
    return validate_snapshot({'schema_version': SNAPSHOT_SCHEMA, 'server': identity,
                              'captured_at': now(), 'label': label, 'complete': catalog['complete'],
                              'notes': notes, 'items': items})


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


def _many_snapshots(snapshots):
    """Validate the complete inventories admitted to a group comparison.

    The group reader reports failed or incomplete members in not_compared.
    Treating an incomplete inventory as an empty server would invent missing
    settings or accounts, so the pure comparison refuses such inputs too.
    """
    if not isinstance(snapshots, dict) or any(
            not isinstance(pid, str) or not pid for pid in snapshots):
        raise ValueError('Group snapshots must be keyed by saved connection ID.')
    for side in snapshots.values():
        if (not isinstance(side, dict) or side.get('complete') is not True or
                side.get('error') or not isinstance(side.get('items'), list)):
            raise ValueError('Only complete member inventories can be compared.')
        if len(side['items']) > MAX_ITEMS:
            raise ValueError('The member inventory is too large to compare.')
    return sorted(snapshots)


def _group_auto_increment(indexed):
    """Fold generated node offsets only when every compared member opts in."""
    # MySQL plugin/group_replication/src/plugin.cc:2578-2581 supplies the
    # server ID as the offset to auto_increment.cc:61-84, but uses each
    # member's own group_replication_auto_increment_increment. An increment
    # difference is real drift; only the generated offset is folded.
    # MariaDB sql/wsrep_server_service.cc:231-236
    # derives the offset from the node index.
    for items in indexed.values():
        def value(name):
            item = items.get(name) or {}
            return None if item.get('redacted') or item.get('truncated') else item.get('value')
        multi_primary = (normalize(value('group_replication_single_primary_mode'), 'boolean') ==
                         ('boolean', False) and bool(value('group_replication_group_name')))
        wsrep_control = normalize(value('wsrep_auto_increment_control'), 'boolean') == ('boolean', True)
        if not (multi_primary or wsrep_control):
            return False
    return bool(indexed)


def _group_provider_options(value):
    """Compare provider options by key, excluding only known per-node values.

    Refuse ambiguous or malformed input, including duplicate keys. A failed
    parse must not turn two unreadable strings into an apparent agreement.
    """
    if not isinstance(value, str):
        raise ValueError('Provider options are unavailable.')
    per_node = {'base_host', 'base_port', 'base_dir', 'gcache.dir', 'gcache.name',
                'gmcast.listen_addr', 'ist.recv_addr', 'ist.recv_bind',
                'socket.ssl_cert', 'socket.ssl_key', 'pc.weight', 'gmcast.segment'}
    options = {}
    parts = value.strip().split(';')
    if not parts[-1].strip():
        parts.pop()
    for part in parts:
        match = re.fullmatch(r'\s*([A-Za-z0-9_.]+)\s*=\s*([^;]*)', part)
        if not match or match.group(1) in options:
            raise ValueError('Provider options could not be parsed safely.')
        options[match.group(1)] = match.group(2).strip()
    return tuple(sorted((key, val) for key, val in options.items() if key not in per_node))


def compare_many(snapshots, role_settings=None):
    """Compare complete member snapshots without performing a server read.

    ``snapshots`` maps saved profile IDs to capture() results. Values retain
    their raw spelling; typed normalization decides equality only. Missing
    includes an unavailable, redacted or truncated value, with a reason so a
    masked secret can never be mistaken for proof that passwords match.
    """
    profile_ids = _many_snapshots(snapshots)
    indexed = {}
    for pid in profile_ids:
        items = {}
        for item in snapshots[pid]['items']:
            if (not isinstance(item, dict) or not isinstance(item.get('name'), str) or
                    not re.fullmatch(r'[a-zA-Z0-9_.]+', item['name'])):
                raise ValueError('Invalid parameter name in member inventory.')
            name = item['name'].lower()
            if name in items:
                raise ValueError('Duplicate parameter name in member inventory.')
            raw = item.get('value')
            if raw is not None and not isinstance(raw, (str, int, float, bool)):
                raise ValueError('Parameter values must be scalar.')
            # Reapply the capture projection at this pure-function boundary.
            # Do not trust a caller to have removed a secret already.
            value = '[redacted]' if sensitive(name) else _redact_parameter_value(raw)
            json.dumps(value, allow_nan=False)
            items[name] = dict(item, value=value, redacted=bool(
                item.get('redacted') or sensitive(name) or value != raw),
                unavailable='value' not in item)
        indexed[pid] = items
    per_server = set(IDENTITY_NAMES) | {'group_replication_local_address', 'group_replication_group_seeds',
        'gtid_binlog_state', 'gtid_owned', 'bind_address', 'admin_address',
        'mysqlx_bind_address', 'mysqlx_port', 'mysqlx_socket',
        'group_replication_advertise_recovery_endpoints', 'group_replication_recovery_public_key_path',
        'group_replication_recovery_ssl_ca', 'group_replication_recovery_ssl_capath',
        'group_replication_recovery_ssl_cert', 'group_replication_recovery_ssl_key',
        'group_replication_recovery_ssl_crl', 'group_replication_recovery_ssl_crlpath',
        'wsrep_node_incoming_address', 'wsrep_sst_receive_address', 'wsrep_sst_donor',
        'wsrep_start_position', 'wsrep_status_file', 'wsrep_provider_base_host',
        'wsrep_provider_base_port', 'wsrep_provider_base_dir', 'wsrep_provider_gcache_dir',
        'wsrep_provider_gmcast_listen_addr', 'wsrep_provider_ist_recv_addr',
        'wsrep_provider_gcache_name', 'wsrep_provider_ist_recv_bind',
        'wsrep_provider_socket_ssl_cert', 'wsrep_provider_socket_ssl_key'}
    node_design = {'wsrep_provider_pc_weight', 'wsrep_provider_gmcast_segment'}
    per_server.update(node_design)
    # MySQL read_mode_handler.cc:32-60 changes read-only settings by role.
    # Live observed roles take precedence over persisted GR configuration.
    # Pure snapshot comparisons can only infer roles from their inventory.
    if role_settings is None:
        role_settings = any(
            normalize((items.get('group_replication_single_primary_mode') or {}).get('value'), 'boolean') ==
            ('boolean', True) and bool((items.get('group_replication_group_name') or {}).get('value'))
            and not any((items.get(name) or {}).get(flag) for name in
                        ('group_replication_single_primary_mode', 'group_replication_group_name')
                        for flag in ('redacted', 'truncated', 'unavailable'))
            for items in indexed.values())
    role_names = {'read_only', 'super_read_only'} if role_settings else set()
    per_server.update(role_names)
    if _group_auto_increment(indexed):
        per_server.add('auto_increment_offset')
    names = set().union(*(set(items) for items in indexed.values())) if indexed else set()
    rows = []
    for name in sorted(names):
        entries = [indexed[pid].get(name) for pid in profile_ids]
        present = [item for item in entries if item is not None]
        unavailable = any(item and (item.get('redacted') or item.get('truncated') or
                                   item.get('unavailable')) for item in entries)
        kind = next((item.get('type') for item in present if item.get('type')), None)
        normalized = [normalize(item.get('value'), kind) for item in present]
        parse_failed = False
        if name == 'wsrep_provider_options' and not unavailable:
            try:
                normalized = [_group_provider_options(item.get('value')) for item in present]
            except ValueError:
                parse_failed = True
                unavailable = True
        missing = len(present) != len(profile_ids)
        status = ('missing' if unavailable else 'per_server' if name in per_server else
                  'missing' if missing else 'different' if any(
                      value != normalized[0] for value in normalized[1:]) else 'same')

        def shared(field):
            values = [item.get(field) if item else None for item in entries]
            if field == 'dynamic':
                values = [item.get('dynamic', item.get('apply_type') == 'dynamic'
                          if item.get('apply_type') in ('dynamic', 'static') else None)
                          if item else None for item in entries]
            return values[0] if not missing and all(value == values[0] for value in values) else None

        row = {'name': name, 'status': status,
               'values': {pid: indexed[pid][name].get('value') if name in indexed[pid] else None
                          for pid in profile_ids},
               'apply_type': shared('apply_type'), 'dynamic': shared('dynamic')}
        if unavailable:
            row['reason'] = ('Provider options could not be parsed safely; they cannot be compared.' if parse_failed else
                             'A value was redacted, truncated or unavailable; it cannot be compared.')
        elif name in role_names:
            row['reason'] = "Set by the member's role"
        elif name in node_design:
            row['reason'] = 'Set per node by design'
        rows.append(row)
    order = {'different': 0, 'missing': 1, 'same': 2, 'per_server': 3}
    return sorted(rows, key=lambda row: (order[row['status']], row['name']))
