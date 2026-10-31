"""Schema versions: save one database's definitions and compare two of them.

A snapshot (``schema-snapshot-1.0``) is one database as its server prints
it: SHOW CREATE output for the database, its tables, views, sequences,
stored routines, packages, triggers and events. Everything is read in two
client calls on the connection: an information_schema inventory, then every
SHOW CREATE statement in one session started with SET SESSION sql_mode = ''
(and the other settings in :data:`SESSION_SETTINGS` the server has), so
identifiers are backquoted and options print the same way on every server.
The client runs that script with --force: a definition the account cannot
read is kept with its reason (``ddl`` null) instead of failing the read.
Nothing is written to the server. AUTO_INCREMENT=N is removed from table
options because it changes with every insert.

A comparison (``schema-compare-1.0``, :func:`compare`) pairs objects by
type and name (tables, views and sequences share one namespace, as on the
server) and says, for each, only in source, only in target, different,
identical or not compared (a definition one side could not read). Tables are compared element
by element: columns (with their effective character set and collation from
information_schema.COLUMNS, which SHOW CREATE TABLE omits when they follow
the table default), indexes, foreign keys, checks, options and partitioning.
Other objects compare their definition, definer, sql_mode and, for
triggers, their order. ``sync`` (schema_sync.py) holds the SQL
that would change Target to match Source; myflames never runs it.

Saved versions live in the workspace store (workspace_store.py).
"""
import hashlib
import json
import re
import time

from . import __version__
from . import schema_ddl
from .connector import ConnectorError
from .perf_common import (ALL_SCHEMAS_TABLE_LIMIT, Server, check_schema, literal, now, reason,
                          show_variables, Reader)
from .connector import unescape_mysql

SNAPSHOT_VERSION = 'schema-snapshot-1.0'
COMPARE_VERSION = 'schema-compare-1.0'
TAG = '/*myflames:schema-versions*/ '
#: One budget for the whole read: inventory, privileges and definitions.
READ_SECONDS = 120
MAX_TABLES = ALL_SCHEMAS_TABLE_LIMIT
MAX_OBJECTS = 5000
MAX_COLUMNS = 200000
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_NAME = 256

#: Object types in report order.
TYPES = ('database', 'table', 'view', 'sequence', 'procedure', 'function', 'package', 'package_body',
         'trigger', 'event')
TYPE_PLURALS = {'database': 'databases', 'table': 'tables', 'view': 'views', 'sequence': 'sequences',
                'procedure': 'procedures', 'function': 'functions', 'package': 'packages',
                'package_body': 'package bodies', 'trigger': 'triggers', 'event': 'events'}
TYPE_LABELS = {'database': 'database', 'table': 'table', 'view': 'view', 'sequence': 'sequence',
               'procedure': 'procedure', 'function': 'function', 'package': 'package',
               'package_body': 'package body', 'trigger': 'trigger', 'event': 'event'}
#: Tables, views and sequences share one namespace on the server.
NAMESPACE = {'table': 'relation', 'view': 'relation', 'sequence': 'relation'}
STATUSES = ('different', 'only_in_source', 'only_in_target', 'not_compared', 'identical')
ROUTINE_TYPES = ('procedure', 'function', 'package', 'package_body')
DEFINER_TYPES = ('view',) + ROUTINE_TYPES + ('trigger', 'event')
SQL_MODE_TYPES = ROUTINE_TYPES + ('trigger', 'event')

_SHOW = {'table': 'TABLE', 'view': 'VIEW', 'sequence': 'SEQUENCE', 'procedure': 'PROCEDURE',
         'function': 'FUNCTION', 'package': 'PACKAGE', 'package_body': 'PACKAGE BODY', 'trigger': 'TRIGGER',
         'event': 'EVENT'}
_TABLE_TYPES = {'BASE TABLE': 'table', 'SYSTEM VERSIONED': 'table', 'VIEW': 'view', 'SEQUENCE': 'sequence'}
_ROUTINE_TYPES = {'PROCEDURE': 'procedure', 'FUNCTION': 'function', 'PACKAGE': 'package',
                  'PACKAGE BODY': 'package_body'}
#: The SSH target a snapshot's server was read through (ssh_tunnel.py), in
#: its ``server`` only for a read through SSH, so a direct read's snapshot is
#: unchanged. Part of the server's identity: the same host and port behind
#: another SSH host, or none, is another server.
SSH_FIELDS = ('ssh_host', 'ssh_user', 'ssh_port')


def read_source(options):
    """capture()'s *source* for a read with connection *options*
    (``live.connection_options``): the database's host and port and, for a
    connection through SSH, its SSH host, user and port. Never a password."""
    source = {'host': options.get('host'), 'port': options.get('port')}
    if options.get('ssh_host'):
        source.update((key, options[key]) for key in SSH_FIELDS if options.get(key) not in (None, ''))
    return source


def ssh_target(server):
    """``(host, user, port)`` of the SSH tunnel *server* (a snapshot's server
    record, or a read's source) was reached through, user '' and port 0 when
    not given, or None for a direct connection. The same parts as
    ``dbops.ssh_identity`` and ``sshKey()`` in apps/workspace/src/binding.ts."""
    host = (server or {}).get('ssh_host')
    if not host:
        return None
    try:
        port = int(server.get('ssh_port') or 0)
    except (TypeError, ValueError):
        port = 0
    return (host, server.get('ssh_user') or '', port)


def ssh_text(server):
    """' via deploy@bastion:2222' for a server reached through SSH, else ''."""
    target = ssh_target(server)
    if target is None:
        return ''
    host, user, port = target
    return ' via %s%s%s' % (user + '@' if user else '', host, ':%d' % port if port else '')


#: Session settings that make SHOW CREATE print the same way everywhere
#: (the variables and sql_mode flags that change it: mysql-server
#: sql/sql_show.cc:1656-1669, 2499-2512, 3014-3023, 3078-3084; mariadb-server
#: sql/sql_show.cc:1768-1786, 2042-2070, 2243-2250). sql_mode '' drops
#: ANSI_QUOTES and MariaDB's NO_*_OPTIONS and ORACLE/MSSQL output forms. The
#: MySQL ones are sent only when the server has the variable (the session-only
#: show_create_table_skip_secondary_engine from 8.0.18).
SESSION_SETTINGS = (('sql_mode', "''"), ('sql_quote_show_create', '1'),
                    ('show_create_table_verbosity', 'OFF'),
                    ('show_gipk_in_create_table_and_information_schema', 'ON'),
                    ('show_create_table_skip_secondary_engine', 'OFF'))
#: The default database during the read. SHOW CREATE VIEW leaves out the
#: view's own database name when it is the session's default database
#: (mysql-server sql/sql_show.cc:3315-3346, mariadb-server :2755-2800), so a
#: connection's default database would change the text; no user database is
#: ever information_schema.
READ_DATABASE = 'information_schema'
#: Virtual schemas with no definitions to save.
UNSUPPORTED_DATABASES = ('information_schema', 'performance_schema')
_VARIABLES = ('version', 'version_comment', 'hostname', 'lower_case_table_names', 'sql_quote_show_create',
              'show_create_table_verbosity', 'show_gipk_in_create_table_and_information_schema')
_ERROR_LINE = re.compile(r'^ERROR (\d+)(?: \(([^)]*)\))? at line (\d+): ?(.*)$')
#: The object is gone: dropped between the inventory and its SHOW CREATE.
#: Not 1305: SHOW CREATE PROCEDURE answers it for a routine the account
#: cannot see as well (mysql-server sql/sp.cc:1705-1713, mariadb-server
#: sql/sp.cc:2039-2048), so that one is kept as unreadable.
_GONE = (1146, 1360, 1539, 1049)
#: Fields kept per object type besides type, name, ddl and reason.
_FIELDS = {
    'database': ('character_set', 'collation'),
    'table': ('columns',),
    'view': ('character_set_client', 'collation_connection'),
    'sequence': (),
    'procedure': ('sql_mode', 'character_set_client', 'collation_connection', 'database_collation'),
    'trigger': ('table', 'timing', 'event', 'order', 'sql_mode', 'character_set_client',
                'collation_connection', 'database_collation'),
    'event': ('sql_mode', 'time_zone', 'character_set_client', 'collation_connection', 'database_collation'),
}
for _kind in ('function', 'package', 'package_body'):
    _FIELDS[_kind] = _FIELDS['procedure']
#: Fields a comparison and the checksum look at (besides ddl).
_COMPARED = {'table': ('columns',), 'trigger': ('table', 'timing', 'event', 'order', 'sql_mode'),
             'event': ('sql_mode', 'time_zone')}
for _kind in ROUTINE_TYPES:
    _COMPARED[_kind] = ('sql_mode',)


# ---- reading a snapshot ----------------------------------------------------------------------

def _rows(text):
    """Batch rows with escapes decoded; the literal NULL stays text (names
    can be spelled NULL, and the callers only read NOT NULL columns)."""
    return [[unescape_mysql(cell) for cell in line.split('\t')]
            for line in text.replace('\r\n', '\n').split('\n') if line]


def _remaining(start, clock, what):
    left = READ_SECONDS - (clock() - start)
    if left <= 1:
        raise ValueError('Reading the database took longer than %d s (%s); try again when the server is less busy.'
                         % (READ_SECONDS, what))
    return left


def _inventory(conn, database, timeout):
    scope = literal(database)
    statements = (
        "SELECT 'S', DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME = %s" % scope,
        "SELECT 'T', TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s "
        "ORDER BY BINARY TABLE_NAME LIMIT %d" % (scope, MAX_OBJECTS + 1),
        "SELECT 'R', ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = %s "
        "ORDER BY BINARY ROUTINE_NAME, ROUTINE_TYPE LIMIT %d" % (scope, MAX_OBJECTS + 1),
        "SELECT 'G', TRIGGER_NAME, EVENT_OBJECT_TABLE, ACTION_TIMING, EVENT_MANIPULATION, ACTION_ORDER "
        "FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = %s ORDER BY BINARY TRIGGER_NAME LIMIT %d"
        % (scope, MAX_OBJECTS + 1),
        "SELECT 'E', EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA = %s "
        "ORDER BY BINARY EVENT_NAME LIMIT %d" % (scope, MAX_OBJECTS + 1),
        "SELECT 'C', TABLE_NAME, COLUMN_NAME, CHARACTER_SET_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND COLLATION_NAME IS NOT NULL ORDER BY BINARY TABLE_NAME, ORDINAL_POSITION "
        "LIMIT %d" % (scope, MAX_COLUMNS + 1),
    )
    text = conn.run('; '.join(TAG + sql for sql in statements), extra_flags=['--comments'], timeout=timeout)
    found = {'S': [], 'T': [], 'R': [], 'G': [], 'E': [], 'C': []}
    for row in _rows(text):
        if row and row[0] in found:
            found[row[0]].append(row[1:])
    return found


def _settings(server, variables):
    """The SESSION_SETTINGS this server has."""
    out = []
    for name, value in SESSION_SETTINGS:
        if name == 'show_create_table_skip_secondary_engine':
            present = server.engine == 'mysql' and server.version_tuple >= (8, 0, 18)
        else:
            present = name in ('sql_mode', 'sql_quote_show_create') or name in variables
        if present:
            out.append((name, value))
    return out


def _session_lines(settings):
    return [TAG + 'USE %s;' % schema_ddl.quote(READ_DATABASE)] + [
        TAG + 'SET SESSION %s = %s;' % (name, value) for name, value in settings]


def _statement_errors(stderr, spans):
    """{statement index: (code, message)} from the client's error lines, and
    the first client-side error (2000-2999: the session is gone) if any."""
    errors, client = {}, None
    for line in stderr.replace('\r\n', '\n').split('\n'):
        match = _ERROR_LINE.match(line)
        if not match:
            continue
        code, at, message = int(match.group(1)), int(match.group(3)), match.group(4)
        if 2000 <= code < 3000:
            client = client or 'ERROR %d: %s' % (code, message)
            continue
        for index, (first, last) in enumerate(spans):
            if first <= at <= last:
                errors.setdefault(index, (code, message))
                break
    return errors, client


def _definitions(conn, statements, settings, timeout):
    """Run the SET lines and SHOW CREATE statements in one session. Returns
    one ``(row, None)`` or ``(None, (code, message))`` per statement."""
    lines = settings + statements
    spans, line = [], 1
    for text in lines:
        breaks = text.count('\n')
        spans.append((line, line + breaks))
        line += breaks + 1
    stdout, stderr = conn.run_script('\n'.join(lines) + '\n', timeout=timeout)
    errors, client = _statement_errors(stderr, spans)
    if client:
        raise ConnectorError('Lost the session while reading definitions: %s' % client)
    failed = [index for index in errors if index < len(settings)]
    if failed:
        code, message = errors[failed[0]]
        raise ValueError('Could not prepare the session for reading definitions (ERROR %d: %s).' % (code, message))
    rows = _rows(stdout)
    ok = [index for index in range(len(settings), len(lines)) if index not in errors]
    if len(rows) != len(ok):
        raise ValueError('The client returned %d definitions for %d statements; nothing was saved.'
                         % (len(rows), len(ok)))
    results = [(None, errors[index]) if index in errors else None for index in range(len(settings), len(lines))]
    for row, index in zip(rows, ok):
        results[index - len(settings)] = (row, None)
    return results


def _unreadable(code, message):
    if code == 1305:
        return ('The server says it does not exist: it was dropped while the database was read, or the account '
                'cannot see it (ERROR 1305).')
    if code in (1044, 1142, 1143, 1227, 1370):
        return 'The account cannot read it (ERROR %d: %s).' % (code, message)
    if code == 1356:
        return 'The view refers to a table, column or function that no longer exists (ERROR 1356).'
    return 'ERROR %d: %s' % (code, message)


def _cell(row, index):
    value = row[index] if len(row) > index else None
    return None if value == 'NULL' else value


def _fill(obj, row, engine):
    """Put one SHOW CREATE row into *obj* (column layouts are fixed per
    statement; mysql-server sql/sql_show.cc, mariadb-server sql/sql_show.cc)."""
    kind = obj['type']
    if kind in ('database', 'table', 'sequence'):
        ddl = _cell(row, 1)
        if kind == 'table' and ddl:
            ddl = schema_ddl.strip_auto_increment(ddl)
    elif kind == 'view':
        ddl = _cell(row, 1)
        obj.update(character_set_client=_cell(row, 2), collation_connection=_cell(row, 3))
    elif kind == 'event':
        ddl = _cell(row, 3)
        obj.update(sql_mode=row[1] if len(row) > 1 else None, time_zone=_cell(row, 2),
                   character_set_client=_cell(row, 4), collation_connection=_cell(row, 5),
                   database_collation=_cell(row, 6))
    else:
        # Routines and packages: name, sql_mode, definition, cs, collation,
        # database collation; triggers add Created after them.
        ddl = _cell(row, 2)
        obj.update(sql_mode=row[1] if len(row) > 1 else None, character_set_client=_cell(row, 3),
                   collation_connection=_cell(row, 4), database_collation=_cell(row, 5))
    if not ddl or not ddl.startswith('CREATE'):
        obj['ddl'] = None
        obj['reason'] = ('The account cannot read this routine\'s definition: %s' % ROUTINE_HINT[engine]
                         if kind in ROUTINE_TYPES else 'The server returned no definition.')
    else:
        obj['ddl'] = ddl


#: What lets an account read a routine's definition. MySQL: the definer,
#: global SELECT or SHOW_ROUTINE (8.0.20+; sql/auth/sql_authorization.cc:
#: 2079-2090, sql/sp.cc:1507-1510). MariaDB: the definer, SELECT on
#: mysql.proc or SHOW CREATE ROUTINE (11.3+; sql/sp_head.cc:2970-3000).
ROUTINE_HINT = {
    'mysql': 'it needs to be the definer, or hold SHOW_ROUTINE or global SELECT.',
    'mariadb': 'it needs to be the definer, or hold SHOW CREATE ROUTINE (MariaDB 11.3 and later) or SELECT on '
               'mysql.proc.'}
#: Database-level privileges that list every table and view of a database
#: in information_schema.TABLES. MySQL: TABLE_OP_ACLS (sql/auth/auth_acls.h:
#: 114-117, sql/item_func.cc:9166-9180); MariaDB: TABLE_ACLS (sql/privilege.h:
#: 277-280, sql/sql_show.cc:5602-5631), which adds GRANT OPTION and DELETE
#: HISTORY.
_TABLE_LISTING = ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'REFERENCES', 'INDEX', 'ALTER',
                  'CREATE VIEW', 'SHOW VIEW', 'TRIGGER')
_TABLE_LISTING_MARIADB = _TABLE_LISTING + ('GRANT OPTION', 'DELETE HISTORY')
#: Database-level privileges that list every routine (information_schema.
#: ROUTINES: mysql-server sql/auth/sql_authorization.cc:2107-2134,
#: mariadb-server sql/sql_parse.cc:7131-7152).
_ROUTINE_LISTING = ('EXECUTE', 'CREATE ROUTINE', 'ALTER ROUTINE')
_COVERAGE_NEEDS = {
    'tables': 'a database-wide privilege on %s such as SELECT, so tables it has no privilege on are not listed',
    'triggers': 'TRIGGER on %s, which lists every trigger',
    'events': 'EVENT on %s, which lists every event',
}


def _routines_listed(model, database, engine):
    from .privileges import has
    if any(has(model, p, 'database', database) for p in _ROUTINE_LISTING):
        return True
    if engine == 'mysql':
        return bool(has(model, 'SELECT', 'global') or has(model, 'SHOW_ROUTINE', 'global'))
    return bool(has(model, 'SELECT', 'table', 'mysql', 'proc') or
                has(model, 'SHOW CREATE ROUTINE', 'database', database))


def coverage(conn, database, engine):
    """Whether SHOW GRANTS proves the account sees every object of each kind
    in information_schema: ``complete``, else ``unknown`` with the reason.
    Only a privilege that is present proves anything (privileges.has)."""
    from .privileges import has, read_privileges
    kinds = ('tables', 'routines', 'triggers', 'events')
    try:
        model = read_privileges(conn)
    except (ConnectorError, ValueError, KeyError, IndexError, TypeError) as exc:
        # Coverage is advice about the read, never a reason to fail it.
        text = 'the account\'s privileges could not be read (%s).' % reason(exc)
        return {kind: {'state': 'unknown', 'reason': text} for kind in kinds}
    listed = {
        'tables': any(has(model, p, 'database', database)
                      for p in (_TABLE_LISTING_MARIADB if engine == 'mariadb' else _TABLE_LISTING)),
        'routines': _routines_listed(model, database, engine),
        'triggers': bool(has(model, 'TRIGGER', 'database', database)),
        'events': bool(has(model, 'EVENT', 'database', database)),
    }
    out = {}
    for kind in kinds:
        if listed[kind]:
            out[kind] = {'state': 'complete', 'reason': ''}
        elif kind == 'routines':
            out[kind] = {'state': 'unknown', 'reason': 'SHOW GRANTS does not show %s.' % (
                'EXECUTE, CREATE ROUTINE or ALTER ROUTINE on %s, SHOW_ROUTINE or global SELECT' % database
                if engine == 'mysql' else
                'EXECUTE, CREATE ROUTINE or ALTER ROUTINE on %s, SHOW CREATE ROUTINE or SELECT on mysql.proc'
                % database)}
        else:
            out[kind] = {'state': 'unknown', 'reason': 'SHOW GRANTS does not show %s.'
                         % (_COVERAGE_NEEDS[kind] % database)}
    return out


def capture(conn, database, source=None, clock=time.monotonic):
    """The ``schema-snapshot-1.0`` of *database*. *source* adds where it was
    read ({host, port, profile_id, profile_name}); it is never sent to the
    server. Raises ValueError for a missing database, a database above the
    limits or an expired budget, ConnectorError when the server is
    unreachable."""
    database = check_schema(database)
    if database is None:
        raise ValueError('Choose a database.')
    if database.lower() in UNSUPPORTED_DATABASES:
        raise ValueError('%s has no definitions to save.' % database)
    start = clock()
    reader = Reader(conn, TAG)
    try:
        with _budget(conn, READ_SECONDS):
            variables = show_variables(reader, _VARIABLES)
    except ConnectorError as exc:
        raise ConnectorError('Could not read the server: %s' % reason(exc)) from None
    server = Server(variables, {})
    found = _inventory(conn, database, _remaining(start, clock, 'listing objects'))
    if not found['S']:
        raise ValueError('There is no database named %s, or the account cannot see it.' % database)
    notes = []
    objects = [{'type': 'database', 'name': database, 'character_set': found['S'][0][0],
                'collation': found['S'][0][1] if len(found['S'][0]) > 1 else None}]
    skipped = set()
    for name, kind in (row[:2] for row in found['T'] if len(row) >= 2):
        if kind in _TABLE_TYPES:
            objects.append({'type': _TABLE_TYPES[kind], 'name': name})
        else:
            skipped.add(kind)
    for name, kind in (row[:2] for row in found['R'] if len(row) >= 2):
        if kind in _ROUTINE_TYPES:
            objects.append({'type': _ROUTINE_TYPES[kind], 'name': name})
        else:
            skipped.add(kind)
    for row in found['G']:
        if len(row) >= 5:
            order = int(row[4]) if row[4].isdigit() else 0
            objects.append({'type': 'trigger', 'name': row[0], 'table': row[1], 'timing': row[2],
                            'event': row[3], 'order': order})
    objects.extend({'type': 'event', 'name': row[0]} for row in found['E'] if row)
    if skipped:
        notes.append('Not read: objects of type %s.' % ', '.join(sorted(skipped)))
    if server.engine == 'mysql' and server.version_tuple >= (9, 2):
        notes.append('JavaScript libraries (CREATE LIBRARY) are not read.')
    tables = sum(1 for obj in objects if obj['type'] == 'table')
    if tables > MAX_TABLES:
        raise ValueError('%s has %s tables, more than the %s a schema version reads.'
                         % (database, '{:,}'.format(tables), '{:,}'.format(MAX_TABLES)))
    if len(objects) > MAX_OBJECTS or len(found['C']) > MAX_COLUMNS:
        raise ValueError('%s has more than %s objects or %s text columns, more than a schema version reads.'
                         % (database, '{:,}'.format(MAX_OBJECTS), '{:,}'.format(MAX_COLUMNS)))
    columns = {}
    for row in found['C']:
        if len(row) >= 4:
            columns.setdefault(row[0], {})[row[1]] = [row[2], row[3]]
    for obj in objects:
        if obj['type'] == 'table':
            obj['columns'] = columns.get(obj['name'], {})
    with _budget(conn, _remaining(start, clock, 'reading privileges')):
        cover = coverage(conn, database, server.engine)
    statements = [TAG + 'SHOW CREATE DATABASE %s;' % schema_ddl.quote(database)]
    statements.extend(TAG + 'SHOW CREATE %s %s.%s;' % (_SHOW[obj['type']], schema_ddl.quote(database),
                                                       schema_ddl.quote(obj['name'])) for obj in objects[1:])
    settings = _settings(server, variables)
    results = _definitions(conn, statements, _session_lines(settings),
                           _remaining(start, clock, 'reading definitions'))
    kept, gone = [], 0
    for obj, (row, error) in zip(objects, results):
        if error is not None:
            code, message = error
            if code in _GONE:
                if obj['type'] == 'database':
                    raise ValueError('The database %s was dropped while it was read.' % database)
                gone += 1
                continue
            obj['ddl'] = None
            obj['reason'] = _unreadable(code, message)
        else:
            _fill(obj, row, server.engine)
        kept.append(obj)
    if gone:
        notes.append('%d object%s dropped while the database was read %s not included.'
                     % (gone, '' if gone == 1 else 's were', 'is' if gone == 1 else 'are'))
    source = source or {}
    read_from = {'engine': server.engine, 'flavor': server.flavor, 'server_product': server.product,
                 'server_version': server.version or None, 'hostname': variables.get('hostname'),
                 'host': source.get('host'), 'port': source.get('port'),
                 'lower_case_table_names': variables.get('lower_case_table_names')}
    # Through SSH, host and port are the database as the SSH host reaches it:
    # the SSH target says which server that is.
    if source.get('ssh_host'):
        read_from.update((key, source[key]) for key in SSH_FIELDS if source.get(key) not in (None, ''))
    snapshot = {
        'schema_version': SNAPSHOT_VERSION,
        'myflames_version': __version__,
        'taken_at': now(),
        'database': database,
        'server': read_from,
        'connection': {key: source.get(key) for key in ('profile_id', 'profile_name') if source.get(key)},
        'read_settings': dict([('database', READ_DATABASE)] +
                              [(name, value.strip("'")) for name, value in settings]),
        'coverage': cover,
        'objects': kept,
        'notes': notes,
    }
    return finish(_scrub(snapshot, getattr(conn, 'password', None)))


class _budget(object):
    """Limit the connection's own per-call timeout (reads that pass none,
    the variables and privileges reads) to what is left of the budget."""

    def __init__(self, conn, seconds):
        self.conn, self.seconds = conn, seconds

    def __enter__(self):
        self.saved = getattr(self.conn, 'query_timeout', None)
        if isinstance(self.saved, (int, float)):
            self.conn.query_timeout = max(1, min(self.saved, int(self.seconds)))
        return self

    def __exit__(self, *exc):
        if isinstance(self.saved, (int, float)):
            self.conn.query_timeout = self.saved


def _scrub(snapshot, secret):
    """The connection's password never stays in text a server or the client
    wrote into the snapshot (reasons, notes)."""
    if not secret:
        return snapshot
    clean = lambda text: text.replace(secret, '[redacted]') if isinstance(text, str) else text
    snapshot['notes'] = [clean(n) for n in snapshot['notes']]
    for item in snapshot['coverage'].values():
        item['reason'] = clean(item.get('reason'))
    for obj in snapshot['objects']:
        if 'reason' in obj:
            obj['reason'] = clean(obj['reason'])
    return snapshot


# ---- snapshots: validation, checksum ------------------------------------------------------------

def _order_key(obj):
    return TYPES.index(obj['type']), obj['name'], obj.get('table') or ''


def _compared(obj):
    fields = {'ddl': obj.get('ddl')}
    for name in _COMPARED.get(obj['type'], ()):
        fields[name] = obj.get(name)
    return [obj['type'], obj['name'], fields]


def checksum(snapshot):
    """sha256 over what a comparison looks at, so equal checksums mean no
    difference (definers included)."""
    data = json.dumps([_compared(obj) for obj in snapshot['objects']], sort_keys=True,
                      ensure_ascii=False, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(data.encode('utf-8')).hexdigest()


def counts(snapshot):
    out = {}
    for obj in snapshot['objects']:
        out[obj['type']] = out.get(obj['type'], 0) + 1
    out['unreadable'] = sum(1 for obj in snapshot['objects'] if obj.get('ddl') is None)
    return out


def finish(snapshot):
    """Sort objects, then fill counts and checksum."""
    snapshot['objects'].sort(key=_order_key)
    snapshot['counts'] = counts(snapshot)
    snapshot['checksum'] = checksum(snapshot)
    return snapshot


def _text(value, label, limit=MAX_NAME, empty=False, null=False):
    if value is None and null:
        return None
    if not isinstance(value, str) or len(value) > limit or (not empty and not value) or '\x00' in value:
        raise ValueError('Invalid %s in the schema version.' % label)
    return value


def _ssh_fields(ssh):
    """The SSH target of a snapshot's server, checked like a connection's
    (ssh_tunnel.ssh_fields), or ValueError."""
    from .ssh_tunnel import ssh_fields
    port = ssh.get('ssh_port')
    if any(not isinstance(ssh.get(key), (str, type(None))) for key in ('ssh_host', 'ssh_user')) or \
            (port is not None and (isinstance(port, bool) or not isinstance(port, int))):
        raise ValueError('Invalid SSH target in the schema version.')
    try:
        clean = ssh_fields(ssh)
    except ValueError:
        raise ValueError('Invalid SSH target in the schema version.') from None
    if not clean:
        raise ValueError('Invalid SSH target in the schema version.')
    return clean


def validate_snapshot(value):
    """A clean copy of a ``schema-snapshot-1.0`` document, or ValueError.
    Used for imports and for every version read back from the store."""
    if not isinstance(value, dict) or value.get('schema_version') != SNAPSHOT_VERSION:
        raise ValueError('Expected a myflames schema-snapshot-1.0 document.')
    objects = value.get('objects')
    if not isinstance(objects, list) or len(objects) > MAX_OBJECTS + 1:
        raise ValueError('A schema version holds at most %s objects.' % '{:,}'.format(MAX_OBJECTS))
    server = value.get('server')
    if not isinstance(server, dict) or server.get('engine') not in ('mysql', 'mariadb'):
        raise ValueError('The schema version does not name its server engine.')
    for key in ('connection', 'read_settings', 'coverage'):
        if value.get(key) is not None and not isinstance(value.get(key), dict):
            raise ValueError('Invalid %s in the schema version.' % key.replace('_', ' '))
    out = {'schema_version': SNAPSHOT_VERSION,
           'myflames_version': _text(value.get('myflames_version', ''), 'myflames version', 64, empty=True),
           'taken_at': _text(value.get('taken_at'), 'read time', 64),
           'database': _text(check_schema(value.get('database')), 'database name', 64),
           'server': {}, 'connection': {}, 'read_settings': {}, 'coverage': {}, 'objects': [], 'notes': []}
    for key in ('flavor', 'server_product', 'server_version', 'hostname', 'host', 'lower_case_table_names'):
        out['server'][key] = _text(server.get(key), 'server field', 255, empty=True, null=True)
    out['server']['engine'] = server['engine']
    port = server.get('port')
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536):
        raise ValueError('Invalid server port in the schema version.')
    out['server']['port'] = port
    ssh = {key: server[key] for key in SSH_FIELDS if server.get(key) is not None}
    if ssh:
        out['server'].update(_ssh_fields(ssh))
    connection = value.get('connection') or {}
    if not isinstance(connection, dict):
        raise ValueError('Invalid connection in the schema version.')
    for key in ('profile_id', 'profile_name'):
        if connection.get(key) is not None:
            out['connection'][key] = _text(connection[key], 'connection field', 200)
    for name, item in (value.get('read_settings') or {}).items():
        out['read_settings'][_text(name, 'setting', 64)] = _text(item, 'setting', 255, empty=True, null=True)
    for kind, item in (value.get('coverage') or {}).items():
        if not isinstance(item, dict) or item.get('state') not in ('complete', 'unknown'):
            raise ValueError('Invalid coverage in the schema version.')
        out['coverage'][_text(kind, 'coverage kind', 32)] = {
            'state': item['state'], 'reason': _text(item.get('reason', ''), 'coverage reason', 2000, empty=True)}
    notes = value.get('notes') or []
    if not isinstance(notes, list) or len(notes) > 200:
        raise ValueError('Invalid notes in the schema version.')
    out['notes'] = [_text(note, 'note', 4000, empty=True) for note in notes]
    for key in ('name', 'note'):
        if value.get(key) is not None:
            out[key] = _text(value[key], key, 200 if key == 'name' else 10000, empty=True)
    seen, size = set(), 0
    for obj in objects:
        if not isinstance(obj, dict) or obj.get('type') not in TYPES:
            raise ValueError('Invalid object in the schema version.')
        kind = obj['type']
        clean = {'type': kind, 'name': _text(obj.get('name'), 'object name')}
        key = (NAMESPACE.get(kind, kind), clean['name'])
        if key in seen:
            raise ValueError('The schema version lists %s %s twice.' % (TYPE_LABELS[kind], clean['name']))
        seen.add(key)
        ddl = obj.get('ddl')
        clean['ddl'] = _text(ddl, 'definition', MAX_SNAPSHOT_BYTES, null=True)
        if ddl is None:
            clean['reason'] = _text(obj.get('reason', ''), 'reason', 4000, empty=True)
        for field in _FIELDS[kind]:
            item = obj.get(field)
            if field == 'order':
                if isinstance(item, bool) or not isinstance(item, int) or not 0 <= item < 1000000:
                    raise ValueError('Invalid trigger order in the schema version.')
                clean[field] = item
            elif field == 'columns':
                if not isinstance(item, dict) or len(item) > 10000:
                    raise ValueError('Invalid columns in the schema version.')
                clean[field] = {}
                for column, pair in item.items():
                    if not isinstance(pair, list) or len(pair) != 2:
                        raise ValueError('Invalid column character set in the schema version.')
                    clean[field][_text(column, 'column name')] = [_text(p, 'character set', 64, null=True)
                                                                  for p in pair]
            else:
                clean[field] = _text(item, field.replace('_', ' '), 4000, empty=True, null=True)
        size += len(ddl or '')
        out['objects'].append(clean)
    if size > MAX_SNAPSHOT_BYTES:
        raise ValueError('The schema version is larger than %d MiB.' % (MAX_SNAPSHOT_BYTES // (1024 * 1024)))
    databases = [obj for obj in out['objects'] if obj['type'] == 'database']
    if len(databases) != 1 or databases[0]['name'] != out['database']:
        raise ValueError('A schema version holds exactly one database, the one it names.')
    return finish(out)


# ---- comparing two snapshots ---------------------------------------------------------------------

def _pairs(target, source):
    """[(target object or None, source object or None)] matched by namespace
    and exact name, then without regard to letter case when that pairs
    exactly one object on each side. The database objects always pair."""
    left = {(NAMESPACE.get(o['type'], o['type']), o['name']): o for o in target['objects']}
    right = {(NAMESPACE.get(o['type'], o['type']), o['name']): o for o in source['objects']}
    pairs = []
    for key in list(left):
        if key in right:
            pairs.append((left.pop(key), right.pop(key)))
    db_left = [k for k in left if k[0] == 'database']
    db_right = [k for k in right if k[0] == 'database']
    if db_left and db_right:
        pairs.append((left.pop(db_left[0]), right.pop(db_right[0])))
    by_lower = {}
    for key in right:
        by_lower.setdefault((key[0], key[1].lower()), []).append(key)
    lower_left = {}
    for key in left:
        lower_left.setdefault((key[0], key[1].lower()), []).append(key)
    for folded, keys in lower_left.items():
        match = by_lower.get(folded, [])
        if len(keys) == 1 and len(match) == 1:
            pairs.append((left.pop(keys[0]), right.pop(match[0])))
    pairs.extend((obj, None) for obj in left.values())
    pairs.extend((None, obj) for obj in right.values())
    pairs.sort(key=lambda pair: _order_key(pair[1] or pair[0]))
    return pairs


def _in_order(first, second):
    """Keys of *first* that keep their relative order in *second* (the
    longest common subsequence); the others moved."""
    import difflib
    matcher = difflib.SequenceMatcher(None, first, second, autojunk=False)
    kept = set()
    for block in matcher.get_matching_blocks():
        kept.update(first[block.a:block.a + block.size])
    return kept


def _change(part, name, change, target=None, source=None, **extra):
    item = {'part': part, 'name': name, 'change': change}
    if target is not None:
        item['target'] = target
    if source is not None:
        item['source'] = source
    item.update(extra)
    return item


def _effective(obj, name):
    for column, pair in (obj.get('columns') or {}).items():
        if column.lower() == name.lower():
            return pair
    return None


def _object_names(snapshot):
    return {o['name'].lower() for o in snapshot.get('objects', []) if o.get('type') != 'database'}


def _side_names(names, index):
    return names[index] if len(names) > index else None


def table_changes(target, source, names=('', '')):
    """Element changes between two table objects, in report order: columns
    (Source's order, then removed ones), indexes, foreign keys, checks,
    other constraints, periods, other lines, options, partitioning. Each
    side's own database name in a backquoted qualifier (a column default
    calling `db`.`seq`) compares as the same name (*names*: Target's, Source's)."""
    old, new = schema_ddl.split_table(target['ddl']), schema_ddl.split_table(source['ddl'])
    a = lambda text: schema_ddl.requalify(text, names[0], '', _side_names(names, 2)) if names[0] else text
    b = lambda text: schema_ddl.requalify(text, names[1], '', _side_names(names, 3)) if names[1] else text
    if old is None or new is None:
        return [] if target['ddl'] == source['ddl'] else [_change('definition', None, 'different')]
    changes = []
    old_cols = [e for e in old['elements'] if e['kind'] == 'column']
    new_cols = [e for e in new['elements'] if e['kind'] == 'column']
    old_by = {e['name'].lower(): e for e in old_cols}
    new_by = {e['name'].lower(): e for e in new_cols}
    old_order = [e['name'].lower() for e in old_cols if e['name'].lower() in new_by]
    new_order = [e['name'].lower() for e in new_cols if e['name'].lower() in old_by]
    kept = _in_order(old_order, new_order)
    for position, col in enumerate(new_cols):
        key = col['name'].lower()
        if key not in old_by:
            changes.append(_change('column', col['name'], 'only_in_source', source=col['text'], position=position))
            continue
        was = old_by[key]
        moved = key not in kept
        cs_old, cs_new = _effective(target, was['name']), _effective(source, col['name'])
        if cs_old and cs_new:
            # The effective character set and collation decide; the clauses
            # in the text depend on the column's history (column_body).
            differs = cs_old != cs_new or a(schema_ddl.column_body(was['text'])) != b(schema_ddl.column_body(col['text']))
        else:
            differs = a(was['text']) != b(col['text'])
        if differs:
            extra = {'position': position}
            if moved:
                extra['moved'] = True
            if cs_old and cs_new and cs_old != cs_new:
                extra['collation'] = {'target': cs_old, 'source': cs_new}
            changes.append(_change('column', col['name'], 'different', was['text'], col['text'], **extra))
        elif moved:
            changes.append(_change('column', col['name'], 'moved', was['text'], col['text'], position=position))
    for col in old_cols:
        if col['name'].lower() not in new_by:
            changes.append(_change('column', col['name'], 'only_in_target', target=col['text']))
    for kind in schema_ddl.KINDS[1:]:
        old_items = {schema_ddl.element_key(e): e for e in old['elements'] if e['kind'] == kind}
        new_items = {schema_ddl.element_key(e): e for e in new['elements'] if e['kind'] == kind}
        for key, item in new_items.items():
            if key not in old_items:
                changes.append(_change(kind, item['name'], 'only_in_source', source=item['text']))
            elif a(old_items[key]['text']) != b(item['text']):
                changes.append(_change(kind, item['name'], 'different', old_items[key]['text'], item['text']))
        for key, item in old_items.items():
            if key not in new_items:
                changes.append(_change(kind, item['name'], 'only_in_target', target=item['text']))
    if a(old['options']) != b(new['options']):
        changes.append(_change('options', None, 'different', old['options'], new['options']))
    if a(old['partitioning']) != b(new['partitioning']):
        change = 'only_in_source' if not old['partitioning'] else 'only_in_target' if not new['partitioning'] else 'different'
        changes.append(_change('partitioning', None, change, old['partitioning'] or None,
                               new['partitioning'] or None))
    return changes


def _database_options(ddl):
    span = schema_ddl.object_name_span(ddl or '', 'DATABASE')
    return (ddl or '')[span[1]:].strip() if span else ddl


def _definition_changes(target, source, names, ignore_definers):
    """Changes of a view, routine, package, trigger, event or sequence."""
    old_definer, old_body = schema_ddl.split_definer(target['ddl'])
    new_definer, new_body = schema_ddl.split_definer(source['ddl'])
    old_body = schema_ddl.requalify(old_body, names[0], '', _side_names(names, 2))
    new_body = schema_ddl.requalify(new_body, names[1], '', _side_names(names, 3))
    changes = []
    if old_body != new_body:
        changes.append(_change('definition', None, 'different'))
    if old_definer != new_definer and not ignore_definers:
        changes.append(_change('definer', None, 'different', old_definer, new_definer))
    if target['type'] in SQL_MODE_TYPES and target.get('sql_mode') != source.get('sql_mode'):
        changes.append(_change('sql_mode', None, 'different', target.get('sql_mode'), source.get('sql_mode')))
    if target['type'] == 'event' and target.get('time_zone') != source.get('time_zone'):
        changes.append(_change('time_zone', None, 'different', target.get('time_zone'), source.get('time_zone')))
    return changes


def _trigger_order(entries, pairs):
    """Mark triggers whose order among the triggers both sides share, for
    the same table, timing and event, is different."""
    groups = {}
    for (old, new), entry in zip(pairs, entries):
        if old is None or new is None or old['type'] != 'trigger' or entry['status'] == 'not_compared':
            continue
        group = groups.setdefault(((new.get('table') or '').lower(), new.get('timing'), new.get('event')), [])
        group.append((old, new, entry))
    for members in groups.values():
        if len(members) < 2:
            continue
        first = [m[0]['name'] for m in sorted(members, key=lambda m: m[0].get('order') or 0)]
        second = [m[0]['name'] for m in sorted(members, key=lambda m: m[1].get('order') or 0)]
        kept = _in_order(first, second)
        for old, new, entry in members:
            if old['name'] not in kept:
                entry.setdefault('changes', []).append(_change('order', None, 'different', old.get('order'),
                                                               new.get('order')))
                entry['status'] = 'different'


def describe_side(snapshot, meta=None):
    """The side of a comparison: database, server, read time, checksum and
    what it is (``kind`` live, version or file), with a label."""
    meta = dict(meta or {})
    server = snapshot['server']
    side = {'kind': meta.pop('kind', 'version'), 'database': snapshot['database'], 'server': dict(server),
            'taken_at': snapshot['taken_at'], 'checksum': snapshot['checksum'],
            'connection': dict(snapshot.get('connection') or {}), 'coverage': snapshot.get('coverage') or {}}
    side.update(meta)
    where = side['connection'].get('profile_name')
    address = '%s:%s%s' % (server.get('host'), server.get('port'), ssh_text(server)) if server.get('host') \
        else server.get('hostname')
    place = '%s (%s)' % (where, address) if where and address else where or address or 'unknown server'
    product = ' '.join(p for p in (server.get('server_product'), server.get('server_version')) if p)
    what = ''
    if side['kind'] == 'version' and side.get('number'):
        what = ', version %s' % side['number']
        if side.get('name'):
            what += ' "%s"' % side['name']
    elif side['kind'] == 'file' and side.get('file'):
        what = ', file %s' % side['file']
    side.setdefault('label', '%s on %s%s%s' % (snapshot['database'], place, ' · ' + product if product else '',
                                              what))
    return side


def _version_pair(version):
    match = re.match(r'(\d+)\.(\d+)', version or '')
    return (int(match.group(1)), int(match.group(2))) if match else None


def _notes(target, source, ignore_definers, definer_only):
    notes = []
    old, new = target['server'], source['server']
    if old.get('engine') != new.get('engine'):
        notes.append('Source is %s and Target is %s: the servers print some definitions differently (for example '
                     'int and int(11)), and those differences are listed as changes.'
                     % (new.get('server_product') or new.get('engine'), old.get('server_product') or old.get('engine')))
    elif _version_pair(old.get('server_version')) != _version_pair(new.get('server_version')):
        notes.append('Source was read from %s %s and Target from %s %s; another server version can print the same '
                     'definition differently.' % (new.get('server_product'), new.get('server_version'),
                                                  old.get('server_product'), old.get('server_version')))
    if target['database'] != source['database']:
        notes.append('The databases have different names (%s and %s): references to each one\'s own name in tables, '
                     'views and routines are compared as the same name.' % (target['database'], source['database']))
    if (old.get('lower_case_table_names') or '0') != (new.get('lower_case_table_names') or '0'):
        notes.append('The servers store table names differently (lower_case_table_names %s and %s), so names '
                     'that differ only in letter case are paired.' % (old.get('lower_case_table_names'),
                                                                      new.get('lower_case_table_names')))
    for label, snapshot in (('Source', source), ('Target', target)):
        for kind, item in sorted((snapshot.get('coverage') or {}).items()):
            if item.get('state') != 'complete':
                notes.append('%s may not list every %s: %s' % (label, kind.rstrip('s') if kind != 'tables'
                                                               else 'table or view', item.get('reason')))
    if ignore_definers:
        notes.append('Definers are ignored.')
    elif definer_only:
        notes.append('%d object%s differ%s only by DEFINER; ignoring definers leaves %s out.'
                     % (definer_only, '' if definer_only == 1 else 's', 's' if definer_only == 1 else '',
                        'it' if definer_only == 1 else 'them'))
    return notes


def compare(source, target, source_meta=None, target_meta=None, ignore_definers=False, sync=True, chosen_keys=None):
    """Compare Source with Target; sync SQL changes Target to match Source.
    Both snapshots are validated ``schema-snapshot-1.0`` documents."""
    # Each side's database name, then its object names (lowercased): a table
    # named like its database keeps `db`.`column` apart from `db`.`table`.
    names = (target['database'], source['database'], _object_names(target), _object_names(source))
    pairs = _pairs(target, source)
    entries = []
    for old, new in pairs:
        obj = new or old
        entry = {'type': obj['type'], 'name': obj['name'],
                 'key': '%s:%s' % (NAMESPACE.get(obj['type'], obj['type']), obj['name'])}
        if old is None:
            entry['status'] = 'only_in_source'
            entry['source_ddl'] = new.get('ddl')
            if new.get('ddl') is None:
                entry['reason'] = 'Source: %s' % new.get('reason')
        elif new is None:
            entry['status'] = 'only_in_target'
            entry['target_ddl'] = old.get('ddl')
            if old.get('ddl') is None:
                entry['reason'] = 'Target: %s' % old.get('reason')
        elif old.get('ddl') is None or new.get('ddl') is None:
            entry['status'] = 'not_compared'
            entry['reason'] = ' '.join('%s: %s' % (label, side.get('reason')) for label, side
                                       in (('Source', new), ('Target', old)) if side.get('ddl') is None)
            entry['target_ddl'], entry['source_ddl'] = old.get('ddl'), new.get('ddl')
        else:
            if old['name'] != new['name']:
                entry['target_name'] = old['name']
            if old['type'] != new['type']:
                changes = [_change('type', None, 'different', old['type'], new['type'])]
            elif obj['type'] == 'table':
                changes = table_changes(old, new, names)
            elif obj['type'] == 'database':
                changes = [] if _database_options(old['ddl']) == _database_options(new['ddl']) else [
                    _change('options', None, 'different', _database_options(old['ddl']), _database_options(new['ddl']))]
            else:
                changes = _definition_changes(old, new, names, ignore_definers)
            if old['name'] != new['name'] and obj['type'] != 'database':
                changes.insert(0, _change('name', None, 'different', old['name'], new['name']))
            entry['status'] = 'different' if changes else 'identical'
            entry['changes'] = changes
            entry['target_ddl'], entry['source_ddl'] = old['ddl'], new['ddl']
        entries.append(entry)
    _trigger_order(entries, pairs)
    definer_only = 0
    for entry in entries:
        if entry['status'] == 'identical':
            for key in ('target_ddl', 'source_ddl', 'changes', 'target_name'):
                entry.pop(key, None)
            continue
        if entry['status'] == 'different':
            entry['definer_only'] = all(c['part'] == 'definer' for c in entry['changes'])
            definer_only += entry['definer_only']
            entry['diff'] = schema_ddl.lines_diff(entry.get('target_ddl'), entry.get('source_ddl'))
            for change in entry['changes']:
                change['label'] = change_label(change, entry['type'])
                change['text'] = change_text(change, entry['type'])
            entry['summary'] = entry_summary(entry)
    summary = {status: sum(1 for e in entries if e['status'] == status) for status in STATUSES}
    summary['definer_only'] = definer_only
    report = {
        'schema_version': COMPARE_VERSION,
        'compared_at': now(),
        'source': describe_side(source, source_meta),
        'target': describe_side(target, target_meta),
        'options': {'ignore_definers': bool(ignore_definers)},
        'identical': not any(summary[s] for s in ('different', 'only_in_source', 'only_in_target', 'not_compared')),
        'summary': summary,
        'objects': entries,
        'notes': _notes(target, source, ignore_definers, definer_only),
    }
    from .schema_sync import annotate_risks, build
    annotate_risks(report, target['server'].get('engine'))
    if sync:
        report['sync'] = build(target, source, report, chosen_keys=chosen_keys)
    return report


# ---- storage helpers (workspace_store.py) --------------------------------------------------------

def source_key(snapshot):
    """The history a saved version belongs to: host, port, the SSH target of
    a read through SSH ("127.0.0.1:3306 via deploy@bastion/shop"; a direct
    read's key is unchanged) and database."""
    server = snapshot['server']
    host = (server.get('host') or server.get('hostname') or '?').lower().rstrip('.')
    return '%s:%s%s/%s' % (host, server.get('port') or '', ssh_text(server), snapshot['database'])


def version_summary(snapshot):
    """What a list of saved versions shows; never the definitions."""
    coverage_ = snapshot.get('coverage') or {}
    return {'database': snapshot['database'], 'taken_at': snapshot['taken_at'], 'server': dict(snapshot['server']),
            'connection': dict(snapshot.get('connection') or {}), 'counts': dict(snapshot['counts']),
            'checksum': snapshot['checksum'], 'myflames_version': snapshot.get('myflames_version') or '',
            'complete': bool(coverage_) and all(v.get('state') == 'complete' for v in coverage_.values())}


def encode_snapshot(snapshot):
    import zlib
    data = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(data) > MAX_SNAPSHOT_BYTES + 1024 * 1024:
        raise ValueError('The schema version is larger than %d MiB.' % (MAX_SNAPSHOT_BYTES // (1024 * 1024)))
    return zlib.compress(data, 6)


def decode_snapshot(blob):
    import zlib
    inflater = zlib.decompressobj()
    data = inflater.decompress(bytes(blob), MAX_SNAPSHOT_BYTES + 2 * 1024 * 1024)
    if inflater.unconsumed_tail:
        raise ValueError('A saved schema version is larger than expected.')
    return validate_snapshot(json.loads(data.decode('utf-8')))


def export_text(item):
    """A saved version as a schema-snapshot-1.0 file, with its name and note."""
    snapshot = dict(item['snapshot'])
    snapshot['name'] = item.get('name') or ''
    snapshot['note'] = item.get('note') or ''
    return json.dumps(snapshot, indent=1, ensure_ascii=False, allow_nan=False) + '\n'


def parse_file(text):
    """A snapshot from file text (JSON), validated."""
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_SNAPSHOT_BYTES + 4 * 1024 * 1024:
        raise ValueError('Choose a schema version file of at most %d MiB.' % (MAX_SNAPSHOT_BYTES // (1024 * 1024)))
    try:
        value = json.loads(text)
    except ValueError:
        raise ValueError('The file is not JSON.') from None
    return validate_snapshot(value)


# ---- text projections ------------------------------------------------------------------------------

_PART_LABELS = {'column': 'column', 'index': 'key', 'foreign_key': 'foreign key', 'check': 'check',
                'constraint': 'constraint', 'period': 'period', 'other': 'line', 'options': 'table options',
                'partitioning': 'partitioning', 'definition': 'definition', 'definer': 'definer',
                'sql_mode': 'sql_mode', 'time_zone': 'time zone', 'order': 'order', 'type': 'type', 'name': 'name'}
_STATUS_LABELS = {'different': 'Different', 'only_in_source': 'Only in source',
                  'only_in_target': 'Only in target', 'not_compared': 'Not compared', 'identical': 'Identical'}
DIFF_LINES = 60


def when_text(stamp):
    """An ISO time as "2026-10-26 23:21:23 UTC" (the stored value when unparsable)."""
    import datetime
    try:
        moment = datetime.datetime.strptime((stamp or '')[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return stamp
    return moment.strftime('%Y-%m-%d %H:%M:%S UTC') if (stamp or '')[19:].lstrip('.0123456789') in ('', 'Z', '+00:00') \
        else stamp


def summary_text(report):
    s = report['summary']
    if report['identical']:
        return 'No differences: %d object%s compared.' % (s['identical'], '' if s['identical'] == 1 else 's')
    parts = ['%d %s' % (s[k], _STATUS_LABELS[k].lower()) for k in STATUSES[:-1] if s[k]]
    return ', '.join(parts) + '.'


def _value(value):
    return '' if value is None else str(value)


def change_label(change, kind=None):
    """What a change is about: "column email", "key idx_name", "table options"."""
    label = 'database options' if kind == 'database' and change['part'] == 'options' else \
        _PART_LABELS.get(change['part'], change['part'])
    return '%s %s' % (label, change['name']) if change.get('name') and change['part'] != 'definition' else label


def change_text(change, kind=None):
    """One change as a line naming Source and Target.
    A value on several lines (partitioning) is left to change_lines."""
    head = change_label(change, kind)
    if change['change'] == 'only_in_source':
        return '%s only in source: %s' % (head, change.get('source'))
    if change['change'] == 'only_in_target':
        return '%s only in target: %s' % (head, change.get('target'))
    if change['part'] == 'definition':
        return 'definition differs'
    if change['part'] == 'options' and kind != 'database':
        # Only the table options that differ, not the whole options line.
        target = schema_ddl.table_options(_value(change.get('target')))
        source = schema_ddl.table_options(_value(change.get('source')))
        shared = [key for key in source if key in target and source[key] != target[key]]
        parts = (['source %s; target %s' % (' '.join(source[key] for key in shared),
                                          ' '.join(target[key] for key in shared))] if shared else [])
        parts.extend('%s only in source' % source[key] for key in source if key not in target)
        parts.extend('%s only in target' % target[key] for key in target if key not in source)
        return '%s: %s' % (head, '; '.join(parts) or 'reordered')
    if change['change'] == 'moved':
        return '%s at position %d in source' % (head, change.get('position', 0) + 1)
    if '\n' in _value(change.get('target')) + _value(change.get('source')):
        return '%s differs' % head
    text = '%s: source %s; target %s' % (head, change.get('source'), change.get('target'))
    if change.get('collation'):
        text += ' (character set and collation: source %s; target %s)' % (
            '/'.join(change['collation']['source']), '/'.join(change['collation']['target']))
    if change.get('moved'):
        text += ', at position %d in source' % (change.get('position', 0) + 1)
    return text


_SUMMARY_PARTS = {'column': ('column', 'columns'), 'index': ('key', 'keys'),
                  'foreign_key': ('foreign key', 'foreign keys'), 'check': ('check', 'checks'),
                  'constraint': ('constraint', 'constraints'), 'period': ('period', 'periods'),
                  'other': ('line', 'lines')}


def entry_summary(entry):
    """One line for a different object: "2 columns different, 1 key only in source, table options differ"."""
    counts_, single = {}, []
    for change in entry.get('changes') or []:
        if change['part'] in _SUMMARY_PARTS:
            key = (change['part'], change['change'])
            counts_[key] = counts_.get(key, 0) + 1
        else:
            label = 'database options' if entry['type'] == 'database' and change['part'] == 'options' \
                else _PART_LABELS.get(change['part'], change['part'])
            text = '%s %s' % (label, 'differ' if change['part'] == 'options' else 'differs')
            if text not in single:
                single.append(text)
    parts = []
    for part in ('column', 'index', 'foreign_key', 'check', 'constraint', 'period', 'other'):
        for change in ('different', 'moved', 'only_in_source', 'only_in_target'):
            n = counts_.get((part, change))
            if n:
                parts.append('%d %s %s' % (n, _SUMMARY_PARTS[part][0 if n == 1 else 1], change.replace('_', ' ')))
    return ', '.join(parts + single)


def change_lines(change, kind=None):
    """change_text, followed by a small diff when a value spans lines."""
    lines = [change_text(change, kind)]
    if change['change'] == 'different' and '\n' in _value(change.get('target')) + _value(change.get('source')):
        lines.extend('  ' + (text if op == '@' else op + ' ' + text)
                     for op, text in schema_ddl.lines_diff(change.get('target'), change.get('source')))
    return lines


def _diff_lines(entry, limit=DIFF_LINES):
    lines = ['%s %s' % (op if op != '@' else '', text) if op != '@' else text for op, text in entry.get('diff') or []]
    if len(lines) > limit:
        lines = lines[:limit] + ['... %d more lines (use --json or --markdown for all of them)' % (len(lines) - limit)]
    return lines


def format_text(report, show_unchanged=False):
    """The comparison for a terminal."""
    out = ['Schema comparison',
           'Source: %s, read %s' % (report['source']['label'], when_text(report['source']['taken_at'])),
           'Target: %s, read %s' % (report['target']['label'], when_text(report['target']['taken_at'])),
           '', summary_text(report)]
    for status in STATUSES:
        entries = [e for e in report['objects'] if e['status'] == status]
        if not entries or (status == 'identical' and not show_unchanged):
            continue
        out.extend(['', _STATUS_LABELS[status]])
        for entry in entries:
            out.append('  %s %s' % (TYPE_LABELS[entry['type']], entry['name']))
            if entry.get('reason'):
                out.append('    %s' % entry['reason'])
            for change in entry.get('changes') or []:
                out.extend('    %s' % line for line in change_lines(change, entry['type']))
            if status == 'different' and entry['type'] != 'table' and any(
                    c['part'] == 'definition' for c in entry.get('changes') or []):
                out.extend('      ' + line for line in _diff_lines(entry))
    sync = report.get('sync')
    if sync and sync['summary']['statements']:
        m = sync['summary']
        out.extend(['', 'Sync script: %d statement%s that would change Target to match Source; myflames does not run '
                        'them. Write them with --sql.' % (m['statements'], '' if m['statements'] == 1 else 's')])
        if m['skipped']:
            out.append('Not generated: %d object%s (listed in the script).' % (m['skipped'], '' if m['skipped'] == 1
                                                                                else 's'))
    if report['notes']:
        out.extend(['', 'Notes:'] + ['  %s' % note for note in report['notes']])
    return '\n'.join(out) + '\n'


def _md(text):
    return (text or '').replace('|', '\\|').replace('\n', ' ')


def format_markdown(report):
    """The comparison as Markdown, for a pull request or a ticket."""
    out = ['# Schema comparison', '',
           '- **Source:** %s, read %s' % (_md(report['source']['label']), when_text(report['source']['taken_at'])),
           '- **Target:** %s, read %s' % (_md(report['target']['label']), when_text(report['target']['taken_at'])),
           '', summary_text(report)]
    for status in STATUSES[:-1]:
        entries = [e for e in report['objects'] if e['status'] == status]
        if not entries:
            continue
        out.extend(['', '## %s' % _STATUS_LABELS[status], ''])
        for entry in entries:
            out.append('### %s `%s`' % (TYPE_LABELS[entry['type']], entry['name'].replace('`', "'")))
            if entry.get('reason'):
                out.extend(['', _md(entry['reason'])])
            if entry.get('changes'):
                out.append('')
                out.extend('- %s' % _md(change_text(c, entry['type'])) for c in entry['changes'])
            if entry.get('diff'):
                out.extend(['', '```diff'] + ['%s%s' % (op, text) if op != '@' else text
                                              for op, text in entry['diff']] + ['```'])
            elif status in ('only_in_source', 'only_in_target') and (entry.get('source_ddl') or entry.get('target_ddl')):
                out.extend(['', '```sql', entry.get('source_ddl') or entry.get('target_ddl'), '```'])
            out.append('')
    sync = report.get('sync')
    if sync and sync['statements']:
        out.extend(['', '## Sync script', '', 'Statements that would change Target to match Source. myflames does not '
                    'run them; review each one first.', '', '```sql', sync['script'].rstrip('\n'), '```'])
    if report['notes']:
        out.extend(['', '## Notes', ''] + ['- %s' % _md(note) for note in report['notes']])
    return '\n'.join(out).rstrip('\n') + '\n'


def counts_text(counts):
    """'7 tables, 2 views, 1 procedure': the objects besides the database."""
    return ', '.join('%d %s' % (counts[t], TYPE_LABELS[t] if counts[t] == 1 else TYPE_PLURALS[t])
                     for t in TYPES if counts.get(t) and t != 'database') or 'no objects'


def format_snapshot_text(snapshot, item=None):
    """A saved version or a fresh read, as a terminal listing."""
    server = snapshot['server']
    title = 'Schema of %s' % snapshot['database']
    if item:
        title = 'Version %s of %s%s' % (item['number'], snapshot['database'],
                                        ' "%s"' % item['name'] if item.get('name') else '')
    out = [title, 'Server: %s %s at %s:%s%s, read %s' % (server.get('server_product'), server.get('server_version'),
                                                           server.get('host'), server.get('port'), ssh_text(server),
                                                           when_text(snapshot['taken_at'])),
           'Checksum: %s' % snapshot['checksum'], '']
    out.append(counts_text(snapshot['counts']))
    for obj in snapshot['objects']:
        if obj['type'] == 'database':
            continue
        line = '  %-12s %s' % (TYPE_LABELS[obj['type']], obj['name'])
        if obj.get('ddl') is None:
            line += '  (not readable: %s)' % obj.get('reason')
        out.append(line)
    for kind, cover in sorted((snapshot.get('coverage') or {}).items()):
        if cover.get('state') != 'complete':
            out.append('note: %s may be incomplete: %s' % (kind, cover.get('reason')))
    out.extend('note: %s' % note for note in snapshot.get('notes') or [])
    return '\n'.join(out) + '\n'
