"""Read-only, conservative schema lint over schema-snapshot-1.0.

Syntax comes from schema_ddl's SHOW CREATE helpers. Quoted values and
comments are masked before matching SQL words; this is not a SQL parser or
an assurance that a schema has no other problems. Live checks never write.

Source evidence (checked 2026-10-27): mysql-server sql/sys_vars.cc defines
sql_mode's strict/NO_ZERO_DATE default and restrict_fk_on_non_standard_key
DEFAULT(true); storage/innobase/dict/dict0dict.cc, dict_foreign_qualify_index,
requires matching leftmost full columns. Both engines' ha_myisam.cc set
HA_NO_TRANSACTIONS. mysql-server share/messages_to_clients.txt deprecates
UTF8MB3; strings/ctype-utf8.cc limits its encoding to three bytes. Reserved
word version evidence lives beside lifecycle.RESERVED_SINCE.
Further references:
https://dev.mysql.com/doc/refman/8.4/en/myisam-storage-engine.html
https://dev.mysql.com/doc/refman/8.4/en/fixed-point-types.html
https://dev.mysql.com/doc/refman/8.4/en/floating-point-types.html
https://dev.mysql.com/doc/refman/8.4/en/create-table-foreign-keys.html
MariaDB's Unicode documentation covers its configurable utf8 alias:
https://mariadb.com/docs/server/reference/data-types/string-data-types/character-sets/unicode
"""
import re
import time

from . import accounts, schema_ddl, schema_versions
from .connector import ConnectorError
from .lifecycle import RESERVED_SINCE
from .perf_common import Reader, reason
from .server_info import detect_flavor, server_version_tuple

SCHEMA_VERSION = 'schema-lint-1.0'
TAG = '/*myflames:schema-lint*/ '
READ_SECONDS = 30
MAX_KEYWORDS = 10000
RULES = ('money_float', 'utf8mb3', 'myisam', 'zero_date', 'reserved_word',
         'fk_index', 'fk_parent_key', 'definer_missing')
_TABLE_RULES = RULES[:4] + ('fk_index', 'fk_parent_key')
_MONEY = re.compile(r'price|amount|cost|total|balance|salary|fee|tax|payment|money|revenue', re.I)
_ZERO = re.compile(r'0000-00-00(?:[ T]00:00:00(?:\.0+)?)?\Z')


def _syntax(text):
    """Same-length SQL with literals, quoted identifiers and comments masked."""
    out, pos, size = [], 0, len(text)
    while pos < size:
        start, ch = pos, text[pos]
        if ch in "'\"`":
            if ch == '`':
                found = schema_ddl.read_identifier(text, pos)
                pos = found[1] if found else size
            else:
                pos = schema_ddl._literal_end(text, pos)
        elif text.startswith('/*', pos):
            end = text.find('*/', pos + 2)
            pos = size if end < 0 else end + 2
        elif ch == '#' or (text.startswith('--', pos) and
                           (pos + 2 == size or text[pos + 2].isspace())):
            end = text.find('\n', pos)
            pos = size if end < 0 else end
        else:
            out.append(ch)
            pos += 1
            continue
        out.append(' ' * (pos - start))
    return ''.join(out)


def _label(obj):
    return '%s %s' % (schema_versions.TYPE_LABELS[obj['type']], obj['name'])


def _watch(out, rule, obj, text, detail, column=None):
    out['findings'].append({'rule': rule, 'status': 'watch', 'object': _label(obj),
                            'column': column, 'text': text, 'detail': detail})


def _unknown(out, rule, obj, why, column=None):
    item = {'rule': rule, 'object': _label(obj), 'column': column, 'reason': why}
    out['not_judged'].append(item)


def _column_type(text):
    ident = schema_ddl.read_identifier(text, 0)
    match = re.match(r'\s+([A-Za-z]+)\b', text[ident[1]:]) if ident else None
    return match.group(1).upper() if match else None


def _zero_default(text):
    """Only a top-level DEFAULT literal, never text inside a COMMENT/expression."""
    masked = _syntax(text)
    for match in re.finditer(r'\bDEFAULT\b', masked, re.I):
        before = masked[:match.start()]
        if before.count('(') != before.count(')'):
            continue
        tail = text[match.end():].lstrip()
        # MySQL permits a parenthesized constant as an expression default.
        enclosed = tail.startswith('(')
        tail = tail[1:].lstrip() if enclosed else tail
        if tail.startswith(("'", '"')):
            end = schema_ddl._literal_end(tail, 0)
            value = tail[1:end - 1]
            rest = tail[end:].lstrip()
            if enclosed and not rest.startswith(')'):
                continue
            if _ZERO.fullmatch(value):
                return True
        elif re.match(r'0(?:\s|\)|$)', tail) and (not enclosed or tail[1:].lstrip().startswith(')')):
            return True
    return False


def _indexes(parts):
    """Keep key-part positions, including expressions and prefix lengths.

    schema_ddl.index_columns deliberately drops expressions, so using its
    list for a leftmost-key check would incorrectly shift later columns.
    """
    result = []
    for item in parts['elements']:
        if item['kind'] != 'index':
            continue
        text = item['text']
        masked = _syntax(text)
        if re.match(r'(?:FULLTEXT|SPATIAL|VECTOR)\b', masked) or re.search(r'\bUSING\s+HASH\b', masked):
            continue
        pos = masked.find('(')
        found = schema_ddl.name_list(text, pos) if pos >= 0 else None
        if not found:
            continue
        start, depth, columns = pos + 1, 1, []
        for at in range(start, found[1]):
            ch = masked[at]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            if (ch == ',' and depth == 1) or depth == 0:
                part = text[start:at].strip()
                ident = schema_ddl.read_identifier(part, 0)
                suffix = _syntax(part[ident[1]:]).strip().upper() if ident else None
                columns.append(ident[0].lower() if ident and suffix in ('', 'ASC', 'DESC') else None)
                start = at + 1
        result.append((text.startswith(('PRIMARY KEY', 'UNIQUE ')), columns))
    return result


def _foreign_key(text):
    # A constraint name can itself contain " FOREIGN KEY ". Remove its
    # identifier before calling the shared helper, which searches that marker.
    name = schema_ddl.read_identifier(text, len('CONSTRAINT '))
    return schema_ddl.foreign_key('CONSTRAINT `fk`' + text[name[1]:]) if name else None


def _charset(out, obj, charset, engine, column=None):
    if (charset or '').lower() not in ('utf8', 'utf8mb3'):
        return
    text = 'utf8mb3 cannot store 4-byte characters such as emoji; use utf8mb4.'
    if engine == 'mysql':
        text += ' MySQL deprecates utf8mb3.'
    _watch(out, 'utf8mb3', obj, text,
           '%s character set: %s.' % ('Column' if column else 'Table default', charset), column)


def _table(out, snapshot, obj, parts, tables):
    engine = snapshot['server']['engine']
    version = server_version_tuple(snapshot['server'].get('server_version') or '')
    if parts is None:
        why = obj.get('reason') or 'The table definition is unavailable or is not recognized SHOW CREATE TABLE output.'
        for rule in _TABLE_RULES:
            _unknown(out, rule, obj, why)
        return
    syntax = _syntax(parts['options'])
    charset = re.search(r'\b(?:CHARSET|CHARACTER\s+SET)\s*=\s*(\w+)', syntax, re.I)
    default_charset = charset.group(1) if charset else None
    _charset(out, obj, default_charset, engine)
    if re.search(r'\bENGINE\s*=\s*MyISAM\b', syntax, re.I):
        _watch(out, 'myisam', obj, 'MyISAM has no transactions or crash recovery.',
               'MyISAM cannot roll back changes. Table repair is not transactional crash recovery; '
               'consider InnoDB when those guarantees are required.')
    for item in parts['elements']:
        if item['kind'] != 'column':
            continue
        name, text = item['name'], item['text']
        kind = _column_type(text)
        if kind in ('FLOAT', 'DOUBLE', 'REAL') and _MONEY.search(name):
            _watch(out, 'money_float', obj, 'FLOAT is approximate; money usually needs DECIMAL.',
                   '%s uses %s. The column name suggests money; confirm its meaning and required precision.' %
                   (name, kind), name)
        pair = obj['columns'].get(name)
        if pair:
            # Effective metadata wins over raw utf8, whose alias is configurable.
            col_charset = pair[0]
        else:
            explicit = re.search(r'\bCHARACTER\s+SET\s+(\w+)', _syntax(text), re.I)
            col_charset = explicit.group(1) if explicit else None
        _charset(out, obj, col_charset, engine, name)
        if kind in ('DATE', 'DATETIME', 'TIMESTAMP') and _zero_default(text):
            text = 'A zero date is not a real date.'
            detail = 'Review existing data and choose a valid date or NULL when absence is intended. '
            if engine == 'mysql':
                text += ' MySQL\'s default sql_mode (NO_ZERO_DATE with strict mode) rejects it.'
            _watch(out, 'zero_date', obj, text, detail +
                   'This checks the definition, not the connection\'s active sql_mode.', name)
    keys = _indexes(parts)
    fold_names = snapshot['server'].get('lower_case_table_names') in ('1', '2')
    for item in parts['elements']:
        if item['kind'] != 'foreign_key':
            continue
        fk = _foreign_key(item['text'])
        if not fk or not fk['columns'] or not fk['ref_columns']:
            for rule in ('fk_index', 'fk_parent_key'):
                _unknown(out, rule, obj, 'Foreign key %s could not be parsed.' % item['name'])
            continue
        child = [name.lower() for name in fk['columns']]
        col_label = ', '.join(fk['columns'])
        if not any(cols[:len(child)] == child for _, cols in keys):
            _watch(out, 'fk_index', obj, 'Foreign-key columns need a matching leftmost index.',
                   '%s uses (%s); no index begins with these full columns in this order. '
                   'Column-prefix indexes are not sufficient.' % (item['name'], col_label), col_label)
        parent_schema = fk['schema'] or snapshot['database']
        same_schema = parent_schema == snapshot['database'] or (fold_names and
                       parent_schema.lower() == snapshot['database'].lower())
        parent_name = fk['table'].lower() if fold_names else fk['table']
        parent = tables.get(parent_name) if same_schema else None
        if parent is None or parent[1] is None:
            _unknown(out, 'fk_parent_key', obj,
                     'Foreign key %s references %s.%s; its parent definition is not available in this snapshot.' %
                     (item['name'], parent_schema, fk['table']), col_label)
            continue
        wanted = [name.lower() for name in fk['ref_columns']]
        if not any(unique and cols == wanted for unique, cols in parent[2]):
            detail = '%s references %s (%s). Non-unique or partial parent keys are non-standard; '
            detail = detail % (item['name'], fk['table'], ', '.join(fk['ref_columns']))
            detail += 'check the intended relationship.'
            if engine == 'mysql' and version >= (8, 4, 0):
                detail += (' MySQL 8.4+ defaults restrict_fk_on_non_standard_key to ON, '
                           'so creating this non-standard reference is rejected unless that restriction is disabled. '
                           'The active setting was not read.')
            _watch(out, 'fk_parent_key', obj,
                   'The foreign key does not reference a complete primary or unique key.',
                   detail, col_label)


class _LiveRead(object):
    """A shared deadline around the connector, including account-list reads."""
    def __init__(self, conn):
        self.conn = conn
        self.deadline = time.monotonic() + READ_SECONDS
        self.version = None
        self.engine = None

    def run(self, sql, extra_flags=None, timeout=None):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ConnectorError('Schema lint live-read time limit reached.')
        return self.conn.run(sql, extra_flags=extra_flags, timeout=min(left, timeout or READ_SECONDS))

    def metadata(self):
        if self.version is None:
            rows = Reader(self, TAG).rows('SELECT @@version, @@version_comment')
            if len(rows) != 1 or len(rows[0]) != 2 or not rows[0][0]:
                raise ConnectorError('The server did not return its engine and version.')
            self.version = rows[0][0]
            self.engine = detect_flavor(self.version, rows[0][1] or '')
        return self.engine, self.version

    def server_version(self):
        engine, version = self.metadata()
        # accounts._server identifies MariaDB from its version string.
        return version + '-MariaDB' if engine == 'mariadb' and 'mariadb' not in version.lower() else version


def _server_arg(server):
    if server is None:
        return None
    if not isinstance(server, dict):
        raise ValueError('Schema lint server needs an open connector connection.')
    host, port = server.get('host'), server.get('port')
    if not isinstance(host, str) or not host.strip() or len(host) > 255 or any(ch.isspace() for ch in host.strip()):
        raise ValueError('Schema lint server needs a validated host.')
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError('Schema lint server needs a port from 1 to 65535.')
    if server.get('connection') is None and isinstance(server.get('error'), str) and server['error'].strip():
        return None
    if not callable(getattr(server.get('connection'), 'run', None)):
        raise ValueError('Schema lint server needs an open connector connection.')
    return _LiveRead(server['connection'])


def _reserved(out, snapshot, names, live, unavailable=None):
    if not names:
        return
    engine = snapshot['server']['engine']
    version = server_version_tuple(snapshot['server'].get('server_version') or '')
    current, why, live_version = set(), None, None
    if engine != 'mysql':
        why = 'MariaDB does not expose the MySQL information_schema.KEYWORDS reserved-word flag.'
    elif live is None:
        why = unavailable or 'Current reserved words require a live MySQL 8.0.11+ connection.'
    else:
        try:
            live_engine, live_version = live.metadata()
            if live_engine != 'mysql' or server_version_tuple(live_version) < (8, 0, 11):
                why = 'Current reserved words require a live MySQL 8.0.11+ connection.'
            else:
                rows = Reader(live, TAG).rows('SELECT WORD FROM information_schema.KEYWORDS '
                                              'WHERE RESERVED = 1 LIMIT %d' % (MAX_KEYWORDS + 1))
                # Seen live on MySQL 8.4: one KEYWORDS row read back with an empty WORD; skip it
                # rather than distrust the whole list.
                words = [r[0] for r in rows if len(r) == 1 and r[0]]
                if not words or len(rows) > MAX_KEYWORDS or any(len(r) != 1 for r in rows):
                    raise ConnectorError('The reserved-word list was empty, incomplete or exceeded its limit.')
                current = {word.upper() for word in words}
        except (ConnectorError, ValueError, IndexError, TypeError) as exc:
            why = 'Could not read reserved words: ' + reason(exc)[:1000]
    future = {word: release for release, words in RESERVED_SINCE if release > version for word in words} \
        if engine == 'mysql' and version != (0, 0, 0) else {}
    if engine == 'mysql' and version == (0, 0, 0):
        _unknown(out, 'reserved_word', {'type': 'database', 'name': snapshot['database']},
                 'The snapshot has no recognizable server version; future reserved words cannot be checked.')
    for obj, column, name in names:
        word = name.upper()
        # A newer live server must not hide when a captured name became
        # reserved: the known release transition is the more useful finding.
        if word in future:
            release = '.'.join(str(part) for part in future[word])
            _watch(out, 'reserved_word', obj, 'This name becomes reserved in a later MySQL release.',
                   '%s becomes reserved in MySQL %s, after the snapshot\'s %s. '
                   'Review quoting before an upgrade.' % (name, release, snapshot['server']['server_version']), column)
        elif word in current:
            _watch(out, 'reserved_word', obj, 'This name is a reserved word in MySQL.',
                   '%s is reserved by the connected MySQL %s. Keep it quoted or consider a different name.' %
                   (name, live_version), column)
    if why:
        # One reason for the whole check, not one per name.
        _unknown(out, 'reserved_word', {'type': 'database', 'name': snapshot['database']}, why)


def _definers(out, snapshot, live, server):
    objects = [obj for obj in snapshot['objects'] if obj['type'] in schema_versions.DEFINER_TYPES]
    if not objects:
        return
    why, known = None, set()
    captured = snapshot['server']
    if live is None:
        why = (server or {}).get('error') or 'Definer existence requires a live connection to the snapshot\'s server.'
    elif not captured.get('host') or not captured.get('port') or \
            captured['host'].strip().lower() != server['host'].strip().lower() or captured['port'] != server['port']:
        why = 'The live host and port do not match the snapshot\'s server; its definers were not checked.'
    elif schema_versions.ssh_target(captured) != schema_versions.ssh_target(server):
        # The same host and port behind another SSH host (or with SSH on one
        # side only) is another server.
        why = ('The snapshot\'s server was read %s and the live connection goes %s; its definers were not checked.'
               % (schema_versions.ssh_text(captured).strip() or 'directly',
                  schema_versions.ssh_text(server).strip() or 'directly'))
    else:
        try:
            engine, _ = live.metadata()
            if engine != captured['engine']:
                why = 'The live server engine does not match the snapshot; its definers were not checked.'
            else:
                report = accounts.list_accounts(live)
                if report.get('complete') is not True:
                    why = 'The account list is incomplete; a definer not listed cannot be called missing.'
                elif not isinstance(report.get('accounts'), list) or any(
                        not isinstance(item, dict) or not isinstance(item.get('name'), str) or
                        not isinstance(item.get('host'), str) for item in report['accounts']):
                    why = 'The account list could not be verified.'
                else:
                    known = {(item['name'], item['host'].lower()) for item in report['accounts']}
        except (ConnectorError, ValueError, IndexError, TypeError) as exc:
            why = 'Could not read the account list: ' + reason(exc)[:1000]
    for obj in objects:
        if why:
            _unknown(out, 'definer_missing', obj, why)
            continue
        definer = schema_ddl.split_definer(obj['ddl'])[0]
        if not definer:
            _unknown(out, 'definer_missing', obj, obj.get('reason') or 'No readable definer appears in the definition.')
            continue
        name, host = accounts.parse_account(definer)
        if (name, host.lower()) not in known:
            _watch(out, 'definer_missing', obj, 'The definer account is missing from this server.',
                   'DEFINER=%s was absent from the complete account list read on the snapshot\'s host and port. '
                   'The account may have changed since the snapshot was captured.' % definer)


def lint(snapshot, server=None):
    """Return schema-lint-1.0. ``server`` is optional and never serialized.

    With a live read, pass ``{'connection': open_connector, 'host': host,
    'port': port}`` using validated connection options, plus ``ssh_host``,
    ``ssh_user`` and ``ssh_port`` for a connection through SSH
    (``schema_versions.read_source``). Host/port and the SSH target must match
    the snapshot for definer checks. Keyword checks use the live MySQL
    version; future-word checks use the captured version. All warnings are
    review prompts, not migration instructions or generated SQL.
    """
    snapshot = schema_versions.validate_snapshot(snapshot)
    live = _server_arg(server)
    out = {'schema_version': SCHEMA_VERSION, 'database': snapshot['database'],
           'server': snapshot['server'], 'findings': [], 'not_judged': []}
    fold_names = snapshot['server'].get('lower_case_table_names') in ('1', '2')
    tables, parsed, names = {}, {}, []
    for obj in snapshot['objects']:
        if obj['type'] == 'table':
            parts = schema_ddl.split_table(obj['ddl'])
            parsed[obj['name']] = parts
            tables[obj['name'].lower() if fold_names else obj['name']] = (obj, parts, _indexes(parts) if parts else [])
            names.append((obj, None, obj['name']))
            if parts:
                names.extend((obj, item['name'], item['name']) for item in parts['elements']
                             if item['kind'] == 'column')
            else:
                _unknown(out, 'reserved_word', obj, 'Column names could not be read from the table definition.')
        elif obj['type'] in schema_versions.ROUTINE_TYPES:
            names.append((obj, None, obj['name']))
    for obj in snapshot['objects']:
        if obj['type'] == 'table':
            _table(out, snapshot, obj, parsed[obj['name']], tables)
    _reserved(out, snapshot, names, live, (server or {}).get('error'))
    _definers(out, snapshot, live, server)
    database = {'type': 'database', 'name': snapshot['database']}
    for kind, rules in (('tables', _TABLE_RULES + ('reserved_word', 'definer_missing')),
                        ('routines', ('reserved_word', 'definer_missing')),
                        ('triggers', ('definer_missing',)), ('events', ('definer_missing',))):
        coverage = snapshot['coverage'].get(kind, {})
        if coverage.get('state') == 'unknown':
            for rule in rules:
                _unknown(out, rule, database, 'The snapshot inventory may omit %s: %s' %
                         (kind, coverage.get('reason') or 'complete visibility was not established.'))
    return out
