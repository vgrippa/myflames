"""Bounded table snapshots and reviewed, optimistic InnoDB row changes.

The private EditorSession supplies clean client configuration, credentials on
stdin/defaults files, lossless HEX rows and an identity-bound server session.
No endpoint accepts SQL, predicates, client baselines or arbitrary expressions.
"""
import copy
from contextlib import contextmanager
import decimal
import json
import re
import secrets
import threading
import time
from collections import OrderedDict

from . import dbops, editor_session as ed, privileges
from .connector import ConnectorError
from .live import connection_options
from .perf_common import ident
from .review_tokens import ReviewTokens

TAG = '/*myflames:table_data*/ '
PAGE_LIMIT = 100
COLUMN_LIMIT = 128
CELL_LIMIT = 65536
SNAPSHOT_LIMIT = 2 * 1024 * 1024
REGISTRY_LIMIT = 8 * 1024 * 1024
CHANGE_LIMIT = 20
TTL = 300
INTEGER = {'tinyint': 8, 'smallint': 16, 'mediumint': 24, 'int': 32, 'integer': 32, 'bigint': 64}
TEXT = {'char', 'varchar', 'tinytext', 'text', 'mediumtext', 'longtext'}
BINARY = {'binary', 'varbinary', 'tinyblob', 'blob', 'mediumblob', 'longblob'}
TEMPORAL = {'date', 'datetime', 'timestamp', 'time', 'year'}
SUPPORTED = set(INTEGER) | TEXT | BINARY | TEMPORAL | {'decimal', 'numeric'}
SETUP = ["SET SESSION time_zone = '+00:00'", 'SET SESSION innodb_lock_wait_timeout = 5',
         'SET SESSION lock_wait_timeout = 5', "SET SESSION sql_mode = CONCAT(@@sql_mode, ',STRICT_ALL_TABLES')"]


def name(value):
    if not isinstance(value, str) or not value or len(value) > 64 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Choose a valid database and table name.')
    return ident(value)


def text_sql(value):
    return "CONVERT(X'%s' USING utf8mb4)" % value.encode('utf-8').hex().upper()


def query(session, sql, rows=True):
    step = session._helper(TAG + sql.rstrip(';'), results=1, timeout=15)
    if not step.marker or session.ended:
        raise ConnectorError('The table session ended before its result was verified.')
    errors = step.real_errors()
    if errors:
        raise ConnectorError(errors[0].get('message') or 'The server refused this table operation.')
    if any(entry.get('truncated') for entry in step.entries):
        raise ValueError('The table result exceeded a safety limit. Narrow the table or use the Query editor.')
    if any(entry.get('warnings') for entry in step.entries):
        raise ValueError('The server returned a warning. The table change was not accepted.')
    if rows:
        result = next((entry for entry in step.entries if entry.get('type') == 'rows'), None)
        if result is None:
            raise ValueError('The server did not return the expected table result.')
        keys = [c['name'] for c in result['columns']]
        return [dict(zip(keys, row)) for row in result['rows']]
    return sum(entry.get('affected_rows') or 0 for entry in step.entries if entry.get('type') == 'ok')


def metadata(session, schema, table):
    where = 'TABLE_SCHEMA=%s AND TABLE_NAME=%s' % (text_sql(schema), text_sql(table))
    tables = query(session, 'SELECT TABLE_TYPE, ENGINE FROM information_schema.TABLES WHERE ' + where)
    if len(tables) != 1:
        raise ValueError('This table is unavailable or its metadata is not visible.')
    columns = query(session, 'SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, '
                    'EXTRA, CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS WHERE ' + where +
                    ' ORDER BY ORDINAL_POSITION LIMIT 129')
    if not columns or len(columns) > COLUMN_LIMIT:
        raise ValueError('The table viewer supports at most 128 columns.')
    for column in columns:
        name(column['COLUMN_NAME'])
    indexes = query(session, 'SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART '
                    'FROM information_schema.STATISTICS WHERE ' + where + ' ORDER BY INDEX_NAME, SEQ_IN_INDEX LIMIT 1025')
    if len(indexes) > 1024:
        raise ValueError('This table has too many index parts for the table viewer.')
    # Empty trigger metadata is only meaningful after TRIGGER authority has been verified.
    triggers = query(session, 'SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE '
                     'EVENT_OBJECT_SCHEMA=%s AND EVENT_OBJECT_TABLE=%s LIMIT 1' % (text_sql(schema), text_sql(table)))
    groups = OrderedDict()
    for index in indexes:
        groups.setdefault(index['INDEX_NAME'], []).append(index)
    by_name = {c['COLUMN_NAME']: c for c in columns}
    keys = []
    for key, parts in groups.items():
        if all(str(p['NON_UNIQUE']) == '0' and p['SUB_PART'] is None and p['COLUMN_NAME'] in by_name
               and by_name[p['COLUMN_NAME']]['IS_NULLABLE'] == 'NO' for p in parts):
            keys.append((key != 'PRIMARY', key, [p['COLUMN_NAME'] for p in parts]))
    keys.sort()
    foreign_keys = query(session, 'SELECT CONSTRAINT_SCHEMA, CONSTRAINT_NAME, TABLE_NAME, UNIQUE_CONSTRAINT_SCHEMA, '
                         'REFERENCED_TABLE_NAME, UPDATE_RULE, DELETE_RULE FROM information_schema.REFERENTIAL_CONSTRAINTS '
                         'WHERE (CONSTRAINT_SCHEMA=%s AND TABLE_NAME=%s) OR (UNIQUE_CONSTRAINT_SCHEMA=%s AND '
                         'REFERENCED_TABLE_NAME=%s) ORDER BY CONSTRAINT_SCHEMA, CONSTRAINT_NAME LIMIT 1025' %
                         (text_sql(schema), text_sql(table), text_sql(schema), text_sql(table)))
    if len(foreign_keys) > 1024:
        raise ValueError('This table has too many foreign keys for the table viewer.')
    checks = query(session, 'SELECT CONSTRAINT_NAME FROM information_schema.TABLE_CONSTRAINTS WHERE ' + where +
                   " AND CONSTRAINT_TYPE='CHECK' LIMIT 1")
    create = query(session, 'SHOW CREATE TABLE %s.%s' % (name(schema), name(table)))
    if len(create) != 1:
        raise ValueError('The table definition could not be verified.')
    return {'checks': checks, 'foreign_keys': foreign_keys, 'create': create[0], 'table': tables[0], 'columns': columns, 'indexes': indexes, 'triggers': triggers,
            'key': keys[0][2] if keys else []}


def column_kind(column):
    kind = column['DATA_TYPE'].lower()
    if kind in BINARY or kind not in SUPPORTED:
        return 'hex'
    if kind in TEXT and column.get('CHARACTER_SET_NAME') not in ('utf8', 'utf8mb3', 'utf8mb4', 'ascii'):
        return 'hex'
    return 'text'


def literal_default(value):
    """MariaDB exposes quoted literals, unlike MySQL's unquoted defaults.

    Accept only literal syntax, never functions, sequences, operators or other
    expressions which could execute outside the reviewed row changes.
    """
    if value is None:
        return True
    return isinstance(value, str) and bool(re.fullmatch(
        r"(?:NULL|[+-]?[0-9]+(?:\.[0-9]+)?|'(?:[^'\\]|''|\\.)*'|0x[0-9A-Fa-f]+|[xX]'[0-9A-Fa-f]*'|[bB]'[01]*')", value))


def edit_reason(meta, actor, schema, table):
    if meta['table']['TABLE_TYPE'] != 'BASE TABLE' or (meta['table'].get('ENGINE') or '').lower() != 'innodb':
        return 'Editing requires an InnoDB base table.'
    if not meta['key']:
        return 'Editing requires a primary key or a full-column UNIQUE key whose columns are NOT NULL.'
    model = actor.get('model', actor)
    if privileges.has(model, 'LOCK TABLES', level='database', database=schema) is not True:
        return 'Editing requires verified LOCK TABLES authority for a stable table definition.'
    if privileges.has(model, 'SELECT') is not True:
        return 'This first table editor requires verified global SELECT authority to check for hidden incoming foreign keys.'
    if meta.get('foreign_keys'):
        return 'Tables participating in foreign keys are read-only in this first table editor.'
    if privileges.has(model, 'TRIGGER', level='table', database=schema, table=table) is not True:
        return 'Editing requires verified TRIGGER authority so trigger absence can be checked safely.'
    if meta['triggers']:
        return 'Tables with triggers are read-only in this first table editor.'
    if meta.get('checks'):
        return 'Tables with CHECK constraints are read-only in this first table editor.'
    for column in meta['columns']:
        if column['DATA_TYPE'].lower() not in SUPPORTED or column['EXTRA'].strip().lower() not in ('', 'auto_increment'):
            return 'This table has generated, automatic or unsupported columns. Use the Query editor to change it.'
        if actor.get('engine', model.get('engine')) == 'mariadb' and not literal_default(column.get('COLUMN_DEFAULT')):
            return 'Tables with default expressions are read-only in this first table editor.'
        if column['DATA_TYPE'].lower() in TEXT and column_kind(column) == 'hex':
            return 'Editing text currently requires UTF-8 or ASCII columns.'
    return ''


def select_sql(meta, schema, table, tail=''):
    values = []
    for index, column in enumerate(meta['columns']):
        quoted = name(column['COLUMN_NAME'])
        # Bound server output before it reaches the XML client parser. A separate
        # byte length prevents an oversized cell from being confused with NULL.
        values += ['OCTET_LENGTH(CAST(%s AS BINARY)) AS l%d' % (quoted, index),
                   'IF(OCTET_LENGTH(CAST(%s AS BINARY)) <= %d, HEX(CAST(%s AS BINARY)), NULL) AS v%d' %
                   (quoted, CELL_LIMIT, quoted, index)]
    return 'SELECT %s FROM %s.%s%s' % (', '.join(values), name(schema), name(table), tail)


def read_rows(session, meta, schema, table, tail):
    # Helpers do not use the editor run's retention budget. Probe only numeric
    # byte lengths first, while the explicit table lock keeps these rows stable.
    # This bounds the later HEX response before the client parser retains it.
    lengths_sql = 'SELECT %s FROM %s.%s%s' % (
        ', '.join('OCTET_LENGTH(CAST(%s AS BINARY)) AS l%d' % (name(c['COLUMN_NAME']), i)
                  for i, c in enumerate(meta['columns'])), name(schema), name(table), tail)
    lengths = query(session, lengths_sql)
    total = 0
    for row in lengths:
        for index in range(len(meta['columns'])):
            length = row['l%d' % index]
            if length is not None:
                length = int(length)
                if length < 0 or length > CELL_LIMIT:
                    raise ValueError('A cell exceeds 64 KiB. Use the Query editor for this table.')
                total += length * 2
                if total > SNAPSHOT_LIMIT:
                    raise ValueError('This page exceeds the 2 MiB snapshot limit. Use a smaller page.')
    result = []
    size = 0
    encoded = query(session, select_sql(meta, schema, table, tail))
    if len(encoded) != len(lengths):
        raise ValueError('The table rows changed while reading their snapshot.')
    for ordinal, row in enumerate(encoded):
        if any(row['l%d' % i] != lengths[ordinal]['l%d' % i] for i in range(len(meta['columns']))):
            raise ValueError('The table values changed while reading their snapshot.')
        values = []
        for index, column in enumerate(meta['columns']):
            length, value = row['l%d' % index], row['v%d' % index]
            if length is not None and int(length) > CELL_LIMIT:
                raise ValueError('A cell exceeds 64 KiB. Use the Query editor for this table.')
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r'(?:[0-9A-Fa-f]{2})*', value)):
                raise ValueError('The server returned an invalid binary table value.')
            if (length is None) != (value is None) or (value is not None and len(value) // 2 != int(length)):
                raise ValueError('The server returned an incomplete table value. Reload using the Query editor.')
            size += len(value or '')
            if size > SNAPSHOT_LIMIT:
                raise ValueError('This page exceeds the 2 MiB snapshot limit. Use a smaller page.')
            values.append(None if value is None else value.upper())
        result.append(values)
    return result


def cell(column, value):
    if value is None:
        return {'kind': 'null', 'value': None}
    kind = column_kind(column)
    try:
        decoded = bytes.fromhex(value).decode('utf-8') if kind == 'text' else value
    except UnicodeError:
        return {'kind': 'hex', 'value': value}
    return {'kind': kind, 'value': decoded}


def value_sql(column, value):
    if not isinstance(value, dict) or set(value) != {'kind', 'value'}:
        raise ValueError('A cell must contain its type and value.')
    kind, data = value['kind'], value['value']
    if kind == 'null':
        if data is not None or column['IS_NULLABLE'] != 'YES':
            raise ValueError('This column does not accept NULL.')
        return 'NULL'
    if not isinstance(data, str) or len(data.encode('utf-8')) > CELL_LIMIT:
        raise ValueError('Cell values must be strings of at most 64 KiB.')
    data_type = column['DATA_TYPE'].lower()
    if kind == 'hex' and data_type in BINARY:
        if not re.fullmatch(r'(?:[0-9A-Fa-f]{2})*', data):
            raise ValueError('Binary values require an even number of hexadecimal digits.')
        return "X'%s'" % data.upper()
    if kind != 'text' or column_kind(column) != 'text':
        raise ValueError('This cell requires its supported text or binary type.')
    if data_type in INTEGER:
        if not re.fullmatch(r'-?(?:0|[1-9][0-9]*)', data):
            raise ValueError('Enter a whole integer without rounding or separators.')
        bits = INTEGER[data_type]
        unsigned = 'unsigned' in column['COLUMN_TYPE'].lower()
        low, high = (0, 2 ** bits - 1) if unsigned else (-2 ** (bits - 1), 2 ** (bits - 1) - 1)
        if not low <= int(data) <= high:
            raise ValueError('This integer is outside the column range.')
        return data
    if data_type in ('decimal', 'numeric'):
        if not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', data):
            raise ValueError('Enter an exact decimal without exponent notation.')
        if not decimal.Decimal(data).is_finite():
            raise ValueError('Enter a finite decimal.')
        return data
    return text_sql(data)


def row_where(snapshot, values):
    columns = snapshot.meta['columns']
    by_name = {c['COLUMN_NAME']: (i, c) for i, c in enumerate(columns)}
    return ' AND '.join('%s = %s' % (name(key), value_sql(by_name[key][1], cell(by_name[key][1], values[by_name[key][0]])))
                        for key in snapshot.meta['key'])


class Snapshot:
    def __init__(self, identity, safety, session, schema, table, meta, rows, actor, offset, limit):
        self.id = secrets.token_urlsafe(32)
        self.identity, self.safety, self.session = identity, safety, session
        self.schema, self.table, self.meta, self.rows, self.actor = schema, table, meta, rows, actor
        self.offset, self.limit = offset, limit
        self.created = time.monotonic()
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.state = 'ready'
        self.message = ''
        self.plan = None
        self.audit_id = None
        self.affected = 0
        self.size = len(json.dumps(rows)) + len(json.dumps(meta))
        self.row_ids = [secrets.token_urlsafe(18) for _ in rows]
        self.reason = edit_reason(meta, actor, schema, table)
        self.tokens = ReviewTokens(ttl=TTL, capacity=1)

    def public(self):
        return {'snapshot_id': self.id, 'schema': self.schema, 'table': self.table,
                'columns': [{'name': c['COLUMN_NAME'], 'type': c['COLUMN_TYPE'], 'kind': column_kind(c),
                             'nullable': c['IS_NULLABLE'] == 'YES', 'auto_increment': 'auto_increment' in c['EXTRA'].lower()}
                            for c in self.meta['columns']],
                'rows': [{'id': row_id, 'values': [cell(c, value) for c, value in zip(self.meta['columns'], row)]}
                         for row_id, row in zip(self.row_ids, self.rows)],
                'editable': not self.reason and not self.safety['read_only'],
                'reason': 'This connection is read-only.' if self.safety['read_only'] else self.reason,
                'key': self.meta['key'], 'offset': self.offset, 'limit': self.limit,
                'has_more': len(self.rows) == self.limit, 'expires_in': TTL}

    def status(self):
        return {'snapshot_id': self.id, 'status': self.state, 'message': self.message,
                'audit_id': self.audit_id, 'affected_rows': self.affected}


class TableData:
    def __init__(self):
        self.items = OrderedDict()
        self.opening = 0
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.reaper = threading.Thread(target=self._reap, daemon=True)
        self.reaper.start()

    @contextmanager
    def reserve(self):
        self.prune()
        with self.lock:
            if self.closed.is_set() or len(self.items) + self.opening >= 4:
                raise ValueError('Close another table viewer before opening a new one.')
            self.opening += 1
        try:
            yield
        finally:
            with self.lock:
                self.opening -= 1

    def _reap(self):
        while not self.closed.wait(15):
            self.prune()

    def prune(self):
        stale = []
        with self.lock:
            for key, snapshot in list(self.items.items()):
                if time.monotonic() - snapshot.created > TTL and snapshot.state != 'running':
                    stale.append(self.items.pop(key))
        for snapshot in stale:
            snapshot.session.end('table_expired', quiet=True)

    def add(self, snapshot):
        self.prune()
        with self.lock:
            if self.closed.is_set():
                raise ValueError('The workspace is closing.')
            if snapshot.size > SNAPSHOT_LIMIT or len(self.items) >= 4 or sum(s.size for s in self.items.values()) + snapshot.size > REGISTRY_LIMIT:
                raise ValueError('Close another table viewer before opening a new one.')
            self.items[snapshot.id] = snapshot

    def get(self, snapshot_id, identity):
        self.prune()
        with self.lock:
            snapshot = self.items.get(snapshot_id) if isinstance(snapshot_id, str) else None
        if snapshot is None or dbops.identity_endpoint(snapshot.identity) != dbops.identity_endpoint(identity):
            raise ValueError('This table snapshot expired or belongs to another connection. Reload the table.')
        return snapshot

    def close(self):
        self.closed.set()
        with self.lock:
            snapshots = list(self.items.values())
            self.items.clear()
        for snapshot in snapshots:
            snapshot.cancel.set()
            snapshot.session.end('table_closed', quiet=True)


def build_plan(snapshot, changes):
    if not isinstance(changes, list) or not 1 <= len(changes) <= CHANGE_LIMIT:
        raise ValueError('Review between 1 and 20 row changes at a time.')
    columns = {c['COLUMN_NAME']: c for c in snapshot.meta['columns']}
    rows = dict(zip(snapshot.row_ids, snapshot.rows))
    used, statements, checks = set(), [], []
    qualified = name(snapshot.schema) + '.' + name(snapshot.table)
    for change in changes:
        if not isinstance(change, dict) or set(change) - {'kind', 'row_id', 'values'}:
            raise ValueError('Invalid staged row change.')
        kind = change.get('kind')
        if kind not in ('insert', 'update', 'delete'):
            raise ValueError('Choose insert, update or delete.')
        values = change.get('values', {})
        if not isinstance(values, dict) or any(key not in columns for key in values):
            raise ValueError('A changed column is not part of this table.')
        if kind == 'insert':
            if change.get('row_id') is not None:
                raise ValueError('An inserted row cannot name an existing row.')
            keys = [key for key in columns if key in values]
            statements.append('INSERT INTO %s (%s) VALUES (%s)' % (qualified, ', '.join(name(key) for key in keys),
                              ', '.join(value_sql(columns[key], values[key]) for key in keys)))
            continue
        row_id = change.get('row_id')
        if not isinstance(row_id, str) or row_id not in rows or row_id in used:
            raise ValueError('Each existing row can be changed once per batch.')
        used.add(row_id)
        where = row_where(snapshot, rows[row_id])
        checks.append({'where': where, 'before': rows[row_id]})
        if kind == 'delete':
            if values:
                raise ValueError('A deletion cannot include changed values.')
            statements.append('DELETE FROM %s WHERE %s LIMIT 1' % (qualified, where))
        else:
            if not values:
                raise ValueError('Choose at least one changed column.')
            assignments = [name(key) + ' = ' + value_sql(columns[key], values[key]) for key in columns if key in values]
            statements.append('UPDATE %s SET %s WHERE %s LIMIT 1' % (qualified, ', '.join(assignments), where))
    if len(json.dumps(changes)) > SNAPSHOT_LIMIT:
        raise ValueError('Staged changes exceed the 2 MiB review limit.')
    return {'writes': statements, 'checks': checks,
            'statements': SETUP + ['SET SESSION autocommit = 0', 'LOCK TABLES %s WRITE' % qualified] + statements + ['COMMIT', 'UNLOCK TABLES']}


def _facts(payload, workspace):
    return ed._connection_facts(payload, workspace.store)


def _audit(snapshot, workspace, options):
    decision = dbops.decide('write', snapshot.safety['type'], snapshot.safety['read_only'])
    password = snapshot.session._conn.password if snapshot.session._conn is not None else None
    trail = dbops.AuditTrail(workspace.store, 'table_data', 'ui', connection={k: options.get(k) for k in ('host', 'port', 'user')},
                            safety=snapshot.safety, password=password, policy=dbops.policy_summary(decision))
    trail.server(snapshot.session.engine, snapshot.session.server_version)
    return trail


def _policy(payload, workspace, snapshot):
    _options, identity, safety, _name = _facts(payload, workspace)
    if identity != snapshot.identity or safety['read_only']:
        raise ValueError('The connection or its write policy changed. Nothing further will be written.')
    if snapshot.cancel.is_set():
        raise ValueError('The table change was cancelled.')


def _actor(session):
    try:
        return privileges.read_privileges(session._conn)
    except (ConnectorError, ValueError):
        return {'checked': False, 'grants': [], 'roles': [], 'engine': session.engine, 'version': session.server_version}


def _check_privileges(snapshot, actor, statements):
    answers = [privileges.statement_privileges(actor, dbops.classify(sql)['statements'][0], database=snapshot.schema)
               for sql in statements]
    denied = [answer for answer in answers if answer['state'] == 'denied']
    if denied:
        raise ValueError(' '.join(answer['reason'] for answer in denied))
    return answers


def _apply(snapshot, payload, workspace, audit):
    committed = False
    rollback = False
    started = False
    sent_commit = False
    session = snapshot.session
    try:
        with session.run_lock:
            _policy(payload, workspace, snapshot)
            session._verify_identity(audit, snapshot.plan['statements'])
            actor = _actor(session)
            reason = edit_reason(snapshot.meta, actor, snapshot.schema, snapshot.table)
            if reason:
                raise ValueError(reason)
            _check_privileges(snapshot, actor, snapshot.plan['writes'])
            # Fail closed when the local audit cannot durably record the attempt.
            snapshot.audit_id = audit.attempt(snapshot.plan['statements'], target={'schema': snapshot.schema, 'table': snapshot.table})
            if not snapshot.audit_id:
                raise ValueError('The audit record could not be created. Nothing was sent.')
            for statement in SETUP:
                query(session, statement, False)
            query(session, 'SET SESSION autocommit = 0', False)
            query(session, 'LOCK TABLES %s.%s WRITE' % (name(snapshot.schema), name(snapshot.table)), False)
            started = True
            # Explicit table locking also closes a concurrent base-table/view
            # replacement race. START TRANSACTION would release this lock.
            current = metadata(session, snapshot.schema, snapshot.table)
            if current != snapshot.meta:
                raise ValueError('The table definition changed. The whole batch was rolled back; reload and review again.')
            reason = edit_reason(current, _actor(session), snapshot.schema, snapshot.table)
            if reason:
                raise ValueError(reason)
            for check in sorted(snapshot.plan['checks'], key=lambda value: value['where']):
                _policy(payload, workspace, snapshot)
                actual = read_rows(session, snapshot.meta, snapshot.schema, snapshot.table,
                                   ' WHERE ' + check['where'] + ' LIMIT 2 FOR UPDATE')
                if actual != [check['before']]:
                    raise ValueError('A row changed or disappeared since it was loaded. The whole batch was rolled back; reload and review again.')
            # The table's MDL is held now. Engine, columns, keys and trigger
            # absence must still match before the first staged write is sent.
            current = metadata(session, snapshot.schema, snapshot.table)
            if current != snapshot.meta:
                raise ValueError('The table definition changed. The whole batch was rolled back; reload and review again.')
            reason = edit_reason(current, _actor(session), snapshot.schema, snapshot.table)
            if reason:
                raise ValueError(reason)
            for statement in snapshot.plan['writes']:
                _policy(payload, workspace, snapshot)
                snapshot.affected += query(session, statement, False)
            _policy(payload, workspace, snapshot)
            sent_commit = True
            query(session, 'COMMIT', False)
            committed = True
            audit.complete('ok', outcome='committed', rows_affected=snapshot.affected)
            snapshot.state, snapshot.message = 'complete', 'Changes committed. Reload the table to see the current rows.'
    except Exception as exc:
        rollback = False
        if started and not committed and not sent_commit:
            try:
                query(session, 'ROLLBACK', False)
                rollback = True
            except Exception:
                pass
        password = session._conn.password if session._conn is not None else None
        message = str(exc).replace(password, '[redacted]') if password else str(exc)
        if rollback:
            snapshot.affected = 0
        unknown = sent_commit or (started and not rollback)
        snapshot.state = 'unknown' if unknown else 'cancelled' if snapshot.cancel.is_set() else 'error'
        snapshot.message = ('The outcome is unknown. Do not retry automatically. Reload and inspect the table. ' if unknown else '') + message
        if audit.id is None:
            snapshot.audit_id = audit.outcome(snapshot.plan['statements'], 'refused', outcome='refused', message=snapshot.message)
        audit.complete('unknown' if unknown else 'error', outcome='unknown' if unknown else 'rolled_back' if rollback else 'refused', message=snapshot.message)
    finally:
        # UNLOCK TABLES can commit implicitly. An uncertain COMMIT/ROLLBACK
        # must only disconnect; never send a second statement that may commit.
        if (committed or rollback) and not session.ended:
            try:
                query(session, 'UNLOCK TABLES', False)
            except Exception:
                pass
        session.end('table_finished', quiet=True)


def _open(payload, workspace, fill_password, options, identity, safety, label):
    registry = workspace.table_data
    schema, table = payload.get('schema'), payload.get('table')
    name(schema); name(table)
    limit, offset = payload.get('limit', 50), payload.get('offset', 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_LIMIT or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10000:
        raise ValueError('Use a page size of 1 to 100 and an offset of 0 to 10000.')
    filled = fill_password(payload)
    full = connection_options(filled.get('connection'))
    if _facts(payload, workspace)[1] != identity:
        raise ValueError('The connection policy changed. Reload the table.')
    session = ed.EditorSession('table-' + secrets.token_hex(16), identity, full, safety['type'], safety['read_only'], True, TTL, label)
    try:
        session.start(full)
        query(session, SETUP[0], False)
        query(session, 'SET SESSION lock_wait_timeout = 5', False)
        actor = _actor(session)
        if privileges.has(actor.get('model', actor), 'LOCK TABLES', level='database', database=schema) is not True:
            raise ValueError('This first table viewer requires verified LOCK TABLES authority to read stable table metadata.')
        query(session, 'LOCK TABLES %s.%s READ' % (name(schema), name(table)), False)
        meta = metadata(session, schema, table)
        if meta['table']['TABLE_TYPE'] != 'BASE TABLE':
            raise ValueError('The table viewer opens base tables only. Views remain available in the Query editor.')
        if any(any(word in column['EXTRA'].upper() for word in ('VIRTUAL', 'STORED', 'PERSISTENT')) for column in meta['columns']):
            raise ValueError('The table viewer does not evaluate generated columns. Open this table explicitly in the Query editor.')
        order = meta['key'] or [meta['columns'][0]['COLUMN_NAME']]
        tail = ' ORDER BY %s LIMIT %d OFFSET %d' % (', '.join(name(key) for key in order), limit, offset)
        rows = read_rows(session, meta, schema, table, tail)
        query(session, 'UNLOCK TABLES', False)
        snapshot = Snapshot(identity, safety, session, schema, table, meta, rows, actor, offset, limit)
        registry.add(snapshot)
        return snapshot.public()
    except Exception as exc:
        session.end('table_failed', quiet=True)
        if full.get('password'):
            raise ValueError(str(exc).replace(full['password'], '[redacted]')) from None
        raise


def response(path, payload, workspace, fill_password):
    options, identity, safety, label = _facts(payload, workspace)
    registry = workspace.table_data
    operation = path.rsplit('/', 1)[-1]
    if operation == 'open':
        with registry.reserve():
            return _open(payload, workspace, fill_password, options, identity, safety, label)
    snapshot = registry.get(payload.get('snapshot_id'), identity)
    if operation == 'status':
        return snapshot.status()
    if operation == 'cancel':
        snapshot.cancel.set()
        return snapshot.status()
    if operation == 'close':
        snapshot.cancel.set()
        if snapshot.state == 'running':
            snapshot.cancel.set()
            return snapshot.status()
        with registry.lock:
            registry.items.pop(snapshot.id, None)
        snapshot.session.end('table_closed', quiet=True)
        return {'status': 'closed'}
    if not snapshot.lock.acquire(False):
        raise ValueError('Another table operation is already in progress.')
    try:
        if operation != 'decline':
            _policy(payload, workspace, snapshot)
        if snapshot.state not in ('ready', 'review'):
            raise ValueError('This snapshot already has an outcome. Reload before making another change.')
        if snapshot.reason:
            raise ValueError(snapshot.reason)
        if operation == 'prepare':
            plan = build_plan(snapshot, payload.get('changes'))
            answers = _check_privileges(snapshot, _actor(snapshot.session), plan['writes'])
            snapshot.plan = plan
            token = snapshot.tokens.issue('table_data', snapshot.identity, plan)
            snapshot.state = 'review'
            return {'snapshot_id': snapshot.id, 'review_token': token, 'statements': plan['statements'], 'privileges': answers,
                    'connection': {'name': label, 'host': options.get('host'), 'port': options.get('port'), 'user': options.get('user')},
                    'connection_type': safety['type'], 'warnings': ['A temporary table WRITE lock blocks other sessions from reading or writing this table until the batch finishes. Apply only if that interruption is acceptable.',
                    'Rows and table metadata are compared with the loaded snapshot under the lock. A conflict refuses the entire batch.',
                    'Binary and text literals use hexadecimal encoding to preserve exact values. All times use UTC.']}
        if operation in ('apply', 'decline'):
            if snapshot.plan is None:
                raise ValueError('Review the staged changes first.')
            snapshot.tokens.check(payload.get('review_token'), 'table_data', snapshot.identity, snapshot.plan, consume=True)
            audit = _audit(snapshot, workspace, options)
            if operation == 'decline':
                snapshot.audit_id = audit.outcome(snapshot.plan['statements'], 'declined', outcome='declined', message='Nothing was sent.')
                snapshot.state, snapshot.plan = 'ready', None
                return snapshot.status()
            if payload.get('confirmed') is not True:
                raise ValueError('Explicit confirmation is required before applying table changes.')
            fill_password(payload)
            _policy(payload, workspace, snapshot)
            snapshot.state = 'running'
            thread = threading.Thread(target=_apply, args=(snapshot, copy.deepcopy(payload), workspace, audit), daemon=True)
            thread.start()
            return snapshot.status()
        raise ValueError('Unknown table operation.')
    finally:
        snapshot.lock.release()
