"""Baselines: "is this normal?" for Server > Health and Query digest.

A baseline is one Health reading (``health-1.0``) or the Performance Schema
statement digest totals Query digest shows (``top-1.0``), kept on this
computer in the workspace database (``WorkspaceStore.baseline_*``) so a later
reading can be compared with it. The comparison runs in the workspace
(``apps/workspace/src/baselines.ts``); this module checks and trims what is
kept, names the server it belongs to, and answers ``/api/baselines/list``,
``save``, ``get`` and ``delete``.

The server is named by its own identity, never by the address that reached
it (an SSH tunnel, a proxy or another host name can reach the same server).
It is read with each reading (``with_identity: true`` on ``/api/live/health``
and ``/api/live/top`` adds ``reading_server``, :func:`read_with_identity`, in the
same client invocation), and again when a baseline is saved or listed. A save
refuses a reading whose server is not the one the connection reaches now, so an endpoint
that switched to another server of the same version never files one server's
reading under the other's name:

* ``@@server_uuid`` on MySQL and Percona Server (MySQL sql/sys_vars.cc
  ``Sys_server_uuid``, 5.6 and later). MariaDB has no such variable (no
  ``server_uuid`` in mariadb-server sql/sys_vars.cc), so it is named by
  ``@@hostname`` and ``@@port`` (both engines: MySQL sys_vars.cc
  ``Sys_hostname``, ``Sys_port``; MariaDB sys_vars.cc ``Sys_hostname``,
  ``Sys_port``).
* The engine is part of the name; the version is kept and shown, so a
  baseline taken before an upgrade still matches the upgraded server.

One client run reads it: ``SHOW GLOBAL VARIABLES`` for those five names
(no privilege needed: performance_schema.global_variables is readable by
every user on MySQL, MariaDB fills it from its own arrays), ``SHOW GLOBAL
STATUS`` for ``Uptime`` (MySQL sql/mysqld.cc and MariaDB sql/mysqld.cc
``{"Uptime", ... show_starttime}``) and ``NOW(6)``. Uptime and NOW(6) place a
Query digest reading on the server's clock (``server_time``): Performance
Schema's FIRST_SEEN is the server's clock in the session time zone, like
NOW(), so "first seen after the baseline" needs no client clock. Each run is
three identity statements; reading batches add fixed result-set markers and
the measured statements. The identity tail has its own three-second timeout;
a tail failure keeps completed data explicitly unverified. Standalone
list/save identity reads time out after three seconds. Every read changes nothing.

What is kept: Health keeps the numbers, statuses and headlines of its metrics
and diagnostics (no explanations, which a newer myflames may word
differently). Query digest keeps, per statement, the digest, schema, counts
and times, and the normalized text with every literal value hidden
(``plan_privacy.hide_literals``); sample statements, which hold real values,
are never kept.
"""
import datetime
import math
import re

from . import dbops
from .connector import ConnectorError, MySQLConnection, PartialReadError, unescape_mysql
from .live import connection_options
from .plan_privacy import hide_literals
from .schema_versions_api import _connection
from .server_info import product_identity
from .workspace_store import BASELINE_KINDS, BASELINE_MAX_PER_SERVER, _baseline_fields

PREFIX = '/api/baselines/'
PATHS = tuple(PREFIX + operation for operation in ('list', 'save', 'get', 'delete'))
#: Statements carry the myflames prefix so Top statements and Query digest
#: leave them out (discover.CAPTURE_TAG).
TAG = '/*myflames:baselines*/ '
IDENTITY_SQL = (TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                "('hostname', 'port', 'server_uuid', 'version', 'version_comment'); "
                + TAG + "SHOW GLOBAL STATUS WHERE Variable_name = 'Uptime'; "
                + TAG + "SELECT 'now', NOW(6)")
QUERY_TIMEOUT = 3
#: Statements a Query digest baseline keeps (the view loads at most 100).
MAX_STATEMENTS = 500
MAX_DIGEST_TEXT = 4096
#: A reading may be this many seconds of Uptime ahead of the identity read
#: (Uptime has one-second resolution; the status and identity statements
#: are separate observations, including the later save-time identity read).
UPTIME_SLACK_S = 2

_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_TIMESTAMP = re.compile(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?$')
_KEY = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
_DIGEST = re.compile(r'^[0-9A-Fa-f]{1,64}$')
_STATUSES = ('ok', 'watch', 'problem', 'info', 'unknown')
_SERVER_FIELDS = ('max_connections', 'table_open_cache', 'buffer_pool_bytes', 'page_size', 'redo_capacity_bytes')


# ---- server identity -------------------------------------------------------------


def identity_from(values):
    """The server's identity from the identity read's name/value pairs
    (names lower-cased). Raises ValueError when it names no server."""
    version = (values.get('version') or '').strip()
    if not version:
        raise ValueError('Could not name this server: it reports no version.')
    product = product_identity(version, values.get('version_comment'))
    server_uuid = (values.get('server_uuid') or '').strip().lower()
    if not _UUID.match(server_uuid):
        server_uuid = ''
    hostname = (values.get('hostname') or '').strip()
    port = (values.get('port') or '').strip()
    port = int(port) if port.isdigit() and 0 < int(port) < 65536 else None
    if not server_uuid and not (hostname and port):
        raise ValueError('Could not name this server: it reports neither server_uuid nor hostname and port.')
    uptime = (values.get('uptime') or '').strip()
    now = (values.get('now') or '').strip()
    return {'engine': product['engine'], 'flavor': product['flavor'], 'product': product['product'],
            'version': version, 'server_uuid': server_uuid or None, 'hostname': hostname or None, 'port': port,
            'uptime_s': int(uptime) if uptime.isdigit() else None,
            'server_time': now if _TIMESTAMP.match(now) else None}


def read_identity(conn):
    """One client run (IDENTITY_SQL): the identity, Uptime and NOW(6)."""
    text = conn.run(IDENTITY_SQL, extra_flags=['--comments', '--skip-reconnect'], timeout=QUERY_TIMEOUT)
    values = {}
    # Only \n separates rows (see connector.query_rows).
    for line in text.replace('\r\n', '\n').split('\n'):
        cells = [unescape_mysql(cell) for cell in line.split('\t')] if line else []
        if len(cells) >= 2:
            values[cells[0].strip().lower()] = cells[1]
    return identity_from(values)


def server_key(identity):
    """The key baselines of one server share: engine, then server_uuid, else
    host name and port. Never the address of the connection."""
    if identity.get('server_uuid'):
        return '%s/uuid/%s' % (identity['engine'], identity['server_uuid'].lower())
    return '%s/host/%s:%d' % (identity['engine'], identity['hostname'].lower().rstrip('.'), identity['port'])


def public_identity(identity):
    """What lists and the comparison show about the server."""
    return {key: identity.get(key) for key in ('engine', 'flavor', 'product', 'version', 'server_uuid',
                                               'hostname', 'port')}


def read_with_identity(conn, statements, tag=TAG, on_data=None):
    """Read app-built statements and identity in one client invocation.

    Reconnection is disabled: a dropped session cannot resume on another server.
    Each data statement returns at least two columns. Single-column markers
    delimit result sets, including empty ones, without mistaking escaped
    server text for framing. No value from a server enters the SQL. A broken
    frame fails closed; missing identity fields leave the data usable but
    explicitly unverified. The identity tail has its own three-second budget.
    A failure after its marker keeps completed data with unverified identity;
    earlier failures propagate. No later connection supplies an identity.
    Optional on_data records data completion at the tail marker, before
    waiting for identity; the connector calls it in its stdout reader.
    """
    markers = ['myflames_reading_%d' % i for i in range(len(statements) + 1)]
    sql = []
    for marker, statement in zip(markers, list(statements) + [IDENTITY_SQL]):
        sql.extend([tag + "SELECT '%s'" % marker, tag + statement])
    failure = None
    options = {'on_tail_start': on_data} if on_data is not None else {}
    try:
        text = conn.run('; '.join(sql), extra_flags=['--comments', '--skip-reconnect'],
                        tail_timeout=(markers[-1], QUERY_TIMEOUT), **options)
    except PartialReadError as exc:
        failure, text = exc, exc.stdout
        # Only complete lines can prove the previous statement finished.
        text = text[:text.rfind('\n') + 1]
    groups, current = [], None
    for line in text.replace('\r\n', '\n').split('\n'):
        if not line:
            continue
        # Compare the raw row: a marker embedded in escaped data is not one.
        if len(groups) < len(markers) and line == markers[len(groups)]:
            current = []
            groups.append(current)
        elif failure is not None and len(groups) == len(markers):
            # A failed identity is never trusted, even if some fields arrived.
            continue
        elif current is None or '\t' not in line:
            raise ValueError('Could not read the server: incomplete reading. Refresh, then try again.')
        else:
            current.append([unescape_mysql(cell) for cell in line.split('\t')])
    if len(groups) != len(markers):
        if failure is not None:
            raise failure
        raise ValueError('Could not read the server: incomplete reading. Refresh, then try again.')
    if failure is not None:
        return groups[:-1], {'verified': False, 'error': 'Could not verify the server identity. Refresh, then try again.'}
    try:
        identity = identity_from({row[0].strip().lower(): row[1] for row in groups[-1] if len(row) == 2})
    except ValueError:
        reading = {'verified': False, 'error': 'The server identity is incomplete. Refresh, then try again.'}
    else:
        reading = {'verified': True, 'server_key': server_key(identity), 'server': public_identity(identity),
                   'uptime_s': identity['uptime_s'], 'server_time': identity['server_time']}
    return groups[:-1], reading


def describe(server):
    """A server in a sentence: "MySQL 8.4.3 (server_uuid 3e11fa47-...)" or
    "MariaDB 11.4.5 at db2:3306"."""
    name = '%s %s' % (server.get('product') or server.get('engine') or '?', server.get('version') or '?')
    if server.get('server_uuid'):
        return '%s (server_uuid %s)' % (name, server['server_uuid'])
    return '%s at %s:%s' % (name, server.get('hostname') or '?', server.get('port') or '?')


_NO_READING_SERVER = ("Could not save the baseline: the server's identity was not read with this reading. Refresh, "
                      "then save again.")


def reading_server(report):
    """The identity a reading carries (``report["reading_server"]``), checked:
    the server's fields only, its key computed here. Refuses a reading
    without one, or whose identity names another engine or version than the
    reading itself."""
    value = report.get('reading_server') if isinstance(report, dict) else None
    server = value.get('server') if isinstance(value, dict) else None
    if not isinstance(server, dict) or value.get('verified') is not True:
        raise ValueError(_NO_READING_SERVER)
    clean = {'engine': server.get('engine'), 'version': server.get('version'),
             'product': _text(server.get('product'), 64, 'product'), 'flavor': _text(server.get('flavor'), 16, 'flavor'),
             'server_uuid': server.get('server_uuid'), 'hostname': server.get('hostname'), 'port': server.get('port')}
    if clean['engine'] not in ('mysql', 'mariadb') or not isinstance(clean['version'], str) or not clean['version']:
        raise ValueError(_NO_READING_SERVER)
    uuid_ = clean['server_uuid']
    clean['server_uuid'] = uuid_.lower() if isinstance(uuid_, str) and _UUID.match(uuid_.lower()) else None
    host = clean['hostname']
    clean['hostname'] = host if isinstance(host, str) and host and len(host) <= 255 and '\x00' not in host else None
    port = clean['port']
    clean['port'] = port if type(port) is int and 0 < port < 65536 else None
    if not clean['server_uuid'] and not (clean['hostname'] and clean['port']):
        raise ValueError(_NO_READING_SERVER)
    if clean['engine'] != report.get('engine') or clean['version'] != (report.get('server_version') or ''):
        raise ValueError("Could not save the baseline: the server's identity read with this reading names %s, not %s %s. "
                         "Refresh, then save again." % (describe(clean), report.get('engine'), report.get('server_version')))
    uptime, now = value.get('uptime_s'), value.get('server_time')
    return {'server': clean, 'key': server_key(clean),
            'uptime_s': uptime if type(uptime) is int and uptime >= 0 else None,
            'server_time': now if isinstance(now, str) and _TIMESTAMP.match(now) else None}


def read_server(value, with_saved_password):
    """The identity of the server behind *value* (a connection), filling its
    saved password for this read only. Errors never carry the password."""
    connection = _connection(value)
    if with_saved_password is None:
        raise ValueError('Reading a server needs a running workspace.')
    password = connection.get('password')
    try:
        filled = with_saved_password({'connection': connection})['connection']
        password = filled.get('password')
        options = connection_options(filled)
        options['query_timeout'] = QUERY_TIMEOUT
        with MySQLConnection(**options) as conn:
            return read_identity(conn)
    except (ConnectorError, ValueError, OSError) as exc:
        raise ValueError(dbops.redact(str(exc), password)) from None


# ---- what a baseline keeps ---------------------------------------------------------


def _number(value, field, minimum=None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('%s must be a number.' % field)
    if minimum is not None and value < minimum:
        raise ValueError('%s cannot be below %s.' % (field, minimum))
    return value


def _count(value, field):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('%s must be a whole number.' % field)
    return value


def _text(value, limit, field, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or '\x00' in value or (required and not value.strip()):
        raise ValueError('%s must be text.' % field)
    return value[:limit]


def _timestamp(value, field):
    if value in (None, ''):
        return None
    if not isinstance(value, str) or not _TIMESTAMP.match(value):
        raise ValueError('%s must be a date and time.' % field)
    return value


def _key(value, field):
    if not isinstance(value, str) or not _KEY.match(value):
        raise ValueError('%s must be a key.' % field)
    return value


def _status(value, field):
    if value not in _STATUSES:
        raise ValueError('%s must be one of %s.' % (field, ', '.join(_STATUSES)))
    return value


def _list(value, limit, field):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('%s must be a list of at most %d items.' % (field, limit))
    return value


def _summary(summary, counts):
    out = {'status': summary['status']}
    for status in _STATUSES:
        out[status] = _count(summary.get(status, 0), 'summary.' + status) or 0
    out['diagnostics'] = {status: _count(counts.get(status, 0), 'summary.diagnostics.' + status) or 0
                          for status in _STATUSES}
    return out


def clean_health(report):
    """The part of a ``health-1.0`` report a baseline keeps: numbers,
    statuses and headlines. Raises ValueError on anything else."""
    if not isinstance(report, dict) or report.get('schema_version') != 'health-1.0':
        raise ValueError('Save a Health reading (health-1.0).')
    engine = report.get('engine')
    if engine not in ('mysql', 'mariadb'):
        raise ValueError('A Health reading names its engine: mysql or mariadb.')
    server = report.get('server') if isinstance(report.get('server'), dict) else {}
    summary = report.get('summary') if isinstance(report.get('summary'), dict) else {}
    counts = summary.get('diagnostics') if isinstance(summary.get('diagnostics'), dict) else {}
    metrics, diagnostics, seen = [], [], set()
    for i, metric in enumerate(_list(report.get('metrics'), 300, 'metrics')):
        if not isinstance(metric, dict):
            raise ValueError('Every metric must be an object.')
        key = _key(metric.get('key'), 'metrics[%d].key' % i)
        if ('metric', key) in seen:
            raise ValueError('Metric %s is listed twice.' % key)
        seen.add(('metric', key))
        metrics.append({'key': key, 'group': _text(metric.get('group'), 64, 'group') or '',
                        'label': _text(metric.get('label'), 200, 'label', required=True),
                        'unit': _text(metric.get('unit'), 32, 'unit') or '',
                        'value': _number(metric.get('value'), 'metrics[%d].value' % i),
                        'status': _status(metric.get('status'), 'metrics[%d].status' % i)})
    for i, item in enumerate(_list(report.get('diagnostics') or [], 100, 'diagnostics')):
        if not isinstance(item, dict):
            raise ValueError('Every diagnostic must be an object.')
        key = _key(item.get('key'), 'diagnostics[%d].key' % i)
        if ('diag', key) in seen:
            raise ValueError('Diagnostic %s is listed twice.' % key)
        seen.add(('diag', key))
        diagnostics.append({'key': key, 'title': _text(item.get('title'), 200, 'title', required=True),
                            'status': _status(item.get('status'), 'diagnostics[%d].status' % i),
                            'state': _text(item.get('state'), 32, 'state'),
                            'headline': _text(item.get('headline'), 300, 'headline') or '',
                            'value': _number(item.get('value'), 'diagnostics[%d].value' % i),
                            'unit': _text(item.get('unit'), 32, 'unit') or ''})
    if summary.get('status') not in ('ok', 'watch', 'problem'):
        raise ValueError('A Health reading has an overall status.')
    return {
        'schema_version': 'health-1.0', 'engine': engine,
        'flavor': _text(report.get('flavor'), 16, 'flavor'),
        'server_product': _text(report.get('server_product'), 64, 'server_product'),
        'server_version': _text(report.get('server_version'), 100, 'server_version'),
        'interval': _count(report.get('interval'), 'interval'),
        'elapsed_s': _number(report.get('elapsed_s'), 'elapsed_s', 0),
        'started_at': _text(report.get('started_at'), 64, 'started_at'),
        'taken_at': _text(report.get('taken_at'), 64, 'taken_at', required=True),
        'uptime_s': _number(report.get('uptime_s'), 'uptime_s', 0),
        'restarted': report.get('restarted') is True,
        'server': {field: _number(server.get(field), field) for field in _SERVER_FIELDS},
        'summary': _summary(summary, counts),
        'metrics': metrics, 'diagnostics': diagnostics,
    }


def clean_digests(report):
    """The part of a Query digest reading (``top-1.0`` from Performance
    Schema) a baseline keeps. Normalized text keeps its shape with every
    literal value hidden; samples and analysis SQL are dropped."""
    if not isinstance(report, dict) or report.get('schema_version') != 'top-1.0':
        raise ValueError('Save Performance Schema statement digests (top-1.0).')
    engine = report.get('engine')
    if engine not in ('mysql', 'mariadb'):
        raise ValueError('A Query digest reading names its engine: mysql or mariadb.')
    coverage = report.get('coverage') if isinstance(report.get('coverage'), dict) else {}
    totals = report.get('totals') if isinstance(report.get('totals'), dict) else None
    statements, seen = [], set()
    for i, row in enumerate(_list(report.get('statements'), MAX_STATEMENTS, 'statements')):
        if not isinstance(row, dict):
            raise ValueError('Every statement must be an object.')
        digest = row.get('digest')
        if digest is not None and (not isinstance(digest, str) or not _DIGEST.match(digest)):
            raise ValueError('statements[%d].digest must be a digest hash.' % i)
        schema = _text(row.get('schema'), 256, 'statements[%d].schema' % i)
        if (schema, digest) in seen and digest is not None:
            raise ValueError('A statement is listed twice.')
        seen.add((schema, digest))
        text = _text(row.get('digest_text'), MAX_DIGEST_TEXT * 4, 'statements[%d].digest_text' % i)
        statements.append({
            'digest': digest, 'schema': schema,
            'digest_text': hide_literals(text)[:MAX_DIGEST_TEXT] if text else None,
            'count': _count(row.get('count'), 'statements[%d].count' % i) or 0,
            'total_ms': _number(row.get('total_ms'), 'statements[%d].total_ms' % i, 0) or 0,
            'avg_ms': _number(row.get('avg_ms'), 'statements[%d].avg_ms' % i, 0),
            'max_ms': _number(row.get('max_ms'), 'statements[%d].max_ms' % i, 0),
            'rows_examined': _count(row.get('rows_examined'), 'rows_examined'),
            'rows_sent': _count(row.get('rows_sent'), 'rows_sent'),
            'first_seen': _timestamp(row.get('first_seen'), 'first_seen'),
            'last_seen': _timestamp(row.get('last_seen'), 'last_seen'),
        })
    return {
        'schema_version': 'top-1.0', 'engine': engine,
        'server_version': _text(report.get('server_version'), 100, 'server_version'),
        'server_product': _text(report.get('server_product'), 64, 'server_product'),
        'order': _text(report.get('order'), 32, 'order') or 'time',
        'limit': _count(report.get('limit'), 'limit'),
        'read_at': _text(report.get('read_at'), 64, 'read_at', required=True),
        'coverage': {'uptime_s': _count(coverage.get('uptime_s'), 'uptime_s'),
                     'oldest_first_seen': _timestamp(coverage.get('oldest_first_seen'), 'oldest_first_seen'),
                     'counting_since': _timestamp(coverage.get('counting_since'), 'counting_since'),
                     'digest_lost': _count(coverage.get('digest_lost'), 'digest_lost')},
        'totals': None if totals is None else {
            'digests': _count(totals.get('digests'), 'digests'),
            'executions': _count(totals.get('executions'), 'executions'),
            'total_ms': _number(totals.get('total_ms'), 'total_ms', 0)},
        'statements': statements,
    }


def reading_time(identity, uptime_s):
    """The server's clock when a reading with *uptime_s* was taken: NOW(6)
    of the identity read, minus the Uptime that passed since. None when
    either is unknown. Raises when the server restarted after the reading."""
    now, uptime = identity.get('server_time'), identity.get('uptime_s')
    if uptime_s is None or uptime is None:
        return None
    if uptime_s > uptime + UPTIME_SLACK_S:
        raise ValueError('Could not save the baseline: the server restarted after this reading. Refresh, then '
                         'save again.')
    if now is None:
        return None
    clock = datetime.datetime.strptime(now.replace('T', ' ')[:19], '%Y-%m-%d %H:%M:%S')
    fraction = now[20:26] if len(now) > 20 else ''
    clock += datetime.timedelta(microseconds=int(fraction.ljust(6, '0')) if fraction else 0)
    clock -= datetime.timedelta(seconds=max(0, uptime - uptime_s))
    return clock.strftime('%Y-%m-%d %H:%M:%S.%f')


def _same_server(kept, identity):
    """Refuse a reading the server in use did not produce (another engine or
    version: the connection now reaches another server)."""
    if kept['engine'] != identity['engine'] or (kept.get('server_version') or '') != identity['version']:
        was = '%s %s' % (kept.get('server_product') or kept['engine'], kept.get('server_version') or '?')
        raise ValueError('Could not save the baseline: this reading came from %s, but the connection now reaches '
                         '%s %s. Refresh, then save again.' % (was, identity['product'], identity['version']))


def baseline_summary(kind, kept, identity):
    """What a list shows about one baseline (never the payload)."""
    out = {'server': public_identity(identity)}
    if kind == 'health':
        summary = kept['summary']
        diag = summary['diagnostics']
        out.update({'taken_at': kept['taken_at'], 'uptime_s': kept['uptime_s'], 'interval': kept['interval'],
                    'status': summary['status'], 'problem': summary['problem'] + diag['problem'],
                    'watch': summary['watch'] + diag['watch'], 'ok': summary['ok'] + diag['ok']})
    else:
        rows = kept['statements']
        out.update({'read_at': kept['read_at'], 'server_time': kept.get('server_time'),
                    'uptime_s': kept['coverage']['uptime_s'], 'order': kept['order'], 'limit': kept['limit'],
                    'statements': len(rows), 'executions': sum(r['count'] for r in rows),
                    'total_ms': round(sum(r['total_ms'] for r in rows), 3)})
    return out


# ---- endpoints ---------------------------------------------------------------------------


def _kind(payload):
    kind = payload.get('kind')
    if kind not in BASELINE_KINDS:
        raise ValueError('Choose a baseline kind: health or digest.')
    return kind


def _id(payload):
    key = payload.get('id')
    if not isinstance(key, str) or not re.match(r'^[0-9a-f]{32}$', key):
        raise ValueError('Choose a saved baseline.')
    return key


def response(path, payload, store, with_saved_password=None):
    """``/api/baselines/{list,save,get,delete}``. List and save read the
    server's identity through *payload['connection']*; get and delete open
    no connection."""
    if not isinstance(payload, dict) or store is None:
        raise ValueError('Baselines need a request object and a running workspace.')
    if path == PREFIX + 'list':
        kind = _kind(payload)
        identity = read_server(payload.get('connection'), with_saved_password)
        key = server_key(identity)
        return {'schema_version': 'baseline-list-1.0', 'kind': kind, 'server': public_identity(identity),
                'server_key': key, 'max': BASELINE_MAX_PER_SERVER, 'items': store.baseline_list(key, kind)}
    if path == PREFIX + 'save':
        kind = _kind(payload)
        name = payload.get('name')
        # Everything is checked before the server is read.
        kept = clean_health(payload.get('report')) if kind == 'health' else clean_digests(payload.get('report'))
        reading = reading_server(payload.get('report'))
        _baseline_fields(kind, 'check', name)
        identity = read_server(payload.get('connection'), with_saved_password)
        # The server the reading came from must be the one reached now: an
        # endpoint that switched servers (same version or not) is refused.
        if reading['key'] != server_key(identity):
            raise ValueError('Could not save the baseline: this reading came from %s, but the connection now reaches '
                             '%s. Refresh, then save again.' % (describe(reading['server']), describe(identity)))
        _same_server(kept, identity)
        uptime = kept['uptime_s'] if kind == 'health' else kept['coverage']['uptime_s']
        server_time = reading_time(identity, uptime)
        if kind == 'digest':
            # The server's clock at the reading: from the identity read with
            # it when there is one (closest), else from this read.
            kept['server_time'] = reading_time(reading, uptime) if reading['server_time'] else server_time
        kept['server'] = public_identity(identity)
        item = store.baseline_save(server_key(identity), kind, name, baseline_summary(kind, kept, identity), kept)
        item['max'] = BASELINE_MAX_PER_SERVER
        return item
    if path == PREFIX + 'get':
        item = store.baseline_get(_id(payload))
        if item is None:
            raise ValueError('Could not open the baseline: it is no longer saved on this computer.')
        return item
    if path == PREFIX + 'delete':
        key = _id(payload)
        if not store.baseline_delete(key):
            raise ValueError('Could not delete the baseline: it is no longer saved on this computer.')
        return {'deleted': True, 'id': key}
    raise ValueError('Unknown baseline operation.')
