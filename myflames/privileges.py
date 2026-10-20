"""Effective privileges of the connection in use (stdlib, Python 3.7+).

``read_privileges(conn)`` returns the ``privileges-1.0`` report: the grants
of a fresh session of the connected account (plain SHOW GRANTS, which merges
the active roles on MySQL and lists the active role tree and PUBLIC on
MariaDB), parsed by :func:`parse_grants`, the single structured SHOW GRANTS
parser, plus ``actions``: the answer of :func:`can` for every key in
:data:`ACTION_KEYS`.

``can(model, action, **target)`` returns ``state`` allowed, denied or
unknown, the nullable ``allowed``, the ``missing`` alternatives and a
``reason``. Only an absent global or dynamic privilege proves a denial:
database, table, column and routine privileges can come from ``mysql.db``
rows of the same user name at another host or of the anonymous user, which
SHOW GRANTS does not print, so their absence is unknown.

Sources and limits: .claude/skills/accounts/references/reading-privileges.md
and action-privileges.md (the spec of the action map). Never reads
``mysql.*`` or SHOW CREATE USER, and never returns a credential.
"""
import re

from . import dbops
from .connector import ConnectorError
from .perf_common import Reader, now
# Public compatibility export; parsing lives below all server subsystems.
from .server_info import detect_flavor, parse_version_tuple as version_tuple

SCHEMA_VERSION = 'privileges-1.0'
TAG = '/*myflames:privileges*/ '
STATIC = frozenset(('SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|RELOAD|SHUTDOWN|'
                    'PROCESS|FILE|GRANT OPTION|REFERENCES|INDEX|ALTER|SHOW DATABASES|SUPER|'
                    'CREATE TEMPORARY TABLES|LOCK TABLES|EXECUTE|REPLICATION SLAVE|'
                    'REPLICATION CLIENT|CREATE VIEW|SHOW VIEW|CREATE ROUTINE|ALTER ROUTINE|'
                    'CREATE USER|EVENT|TRIGGER|CREATE TABLESPACE|CREATE ROLE|DROP ROLE|USAGE').split('|'))
MARIADB_EXTRA = frozenset(('DELETE HISTORY|SET USER|FEDERATED ADMIN|CONNECTION ADMIN|'
                          'READ_ONLY ADMIN|REPLICATION SLAVE ADMIN|REPLICATION MASTER ADMIN|'
                          'BINLOG ADMIN|BINLOG REPLAY|BINLOG MONITOR|SLAVE MONITOR|'
                          'SHOW CREATE ROUTINE').split('|'))
COLUMN_PRIVILEGES = frozenset(('SELECT', 'INSERT', 'UPDATE', 'REFERENCES'))
TABLE_PRIVILEGES = COLUMN_PRIVILEGES | frozenset(('DELETE', 'CREATE', 'DROP', 'INDEX', 'ALTER', 'CREATE VIEW', 'SHOW VIEW', 'TRIGGER'))
DATABASE_PRIVILEGES = TABLE_PRIVILEGES | frozenset(('CREATE TEMPORARY TABLES', 'LOCK TABLES', 'CREATE ROUTINE', 'ALTER ROUTINE', 'EXECUTE', 'EVENT'))
ROUTINE_PRIVILEGES = frozenset(('EXECUTE', 'ALTER ROUTINE'))
ALL_NAMES = frozenset(('ALL', 'ALL PRIVILEGES'))

# Quoted identifiers keep their backslashes, which tell db\_x from db_x.
_TOKEN = dbops.GRANT_TOKEN
strip_credentials = dbops.strip_credentials


def static_privileges(engine, version, level):
    """The level's ALL expansion, excluding USAGE and GRANT OPTION.

    Masks: MySQL sql/auth/auth_acls.h:103-168; MariaDB sql/privilege.h
    and the mysql.db/tables_priv columns (accounts privilege-names.md).
    Dynamic privileges are always enumerated separately by SHOW GRANTS.
    """
    names = {'global': STATIC, 'database': DATABASE_PRIVILEGES,
             'table': TABLE_PRIVILEGES, 'column': COLUMN_PRIVILEGES,
             'routine': ROUTINE_PRIVILEGES}.get(level, frozenset())
    if engine == 'mariadb':
        if level == 'global':
            names |= MARIADB_EXTRA
            names -= {'CREATE ROLE', 'DROP ROLE', 'REPLICATION CLIENT'}
        if level in ('database', 'table'):
            names |= {'DELETE HISTORY'}
        if level in ('global', 'database', 'routine'):
            if version_tuple(version) >= (11, 3, 0):
                names |= {'SHOW CREATE ROUTINE'}
            else:
                names -= {'SHOW CREATE ROUTINE'}
    return names - {'USAGE', 'GRANT OPTION'}


# First registrations verified in mysql-server tags, dynamic_privileges_impl.cc.
# This fallback deliberately excludes optional plugin/component registrations.
_MYSQL_DYNAMIC_SINCE = (
    ((8, 0, 11), 'ROLE_ADMIN SYSTEM_VARIABLES_ADMIN BINLOG_ADMIN REPLICATION_SLAVE_ADMIN '
     'GROUP_REPLICATION_ADMIN ENCRYPTION_KEY_ADMIN CONNECTION_ADMIN XA_RECOVER_ADMIN '
     'PERSIST_RO_VARIABLES_ADMIN BACKUP_ADMIN RESOURCE_GROUP_ADMIN RESOURCE_GROUP_USER SET_USER_ID'),
    ((8, 0, 14), 'SESSION_VARIABLES_ADMIN BINLOG_ENCRYPTION_ADMIN SERVICE_CONNECTION_ADMIN APPLICATION_PASSWORD_ADMIN'),
    ((8, 0, 16), 'SYSTEM_USER TABLE_ENCRYPTION_ADMIN'),
    ((8, 0, 17), 'CLONE_ADMIN AUDIT_ADMIN'),
    ((8, 0, 18), 'REPLICATION_APPLIER'),
    ((8, 0, 20), 'SHOW_ROUTINE'),
    ((8, 0, 21), 'INNODB_REDO_LOG_ENABLE'),
    ((8, 0, 23), 'FLUSH_OPTIMIZER_COSTS FLUSH_STATUS FLUSH_USER_RESOURCES FLUSH_TABLES'),
    ((8, 0, 27), 'GROUP_REPLICATION_STREAM AUTHENTICATION_POLICY_ADMIN PASSWORDLESS_USER_ADMIN'),
    ((8, 0, 29), 'SENSITIVE_VARIABLES_OBSERVER'),
    ((8, 0, 33), 'TELEMETRY_LOG_ADMIN'),
    ((8, 2, 0), 'SET_ANY_DEFINER ALLOW_NONEXISTENT_DEFINER'),
    ((8, 3, 0), 'TRANSACTION_GTID_TAG'),
    ((8, 4, 0), 'OPTIMIZE_LOCAL_TABLE FLUSH_PRIVILEGES'),
    ((9, 2, 0), 'CREATE_SPATIAL_REFERENCE_SYSTEM'),
    ((9, 7, 0), 'MANAGE_DATA_MASKING_POLICY'),
)
GRANT_LEVELS = ('global', 'database', 'table', 'column', 'routine', 'proxy')
# SHOW PRIVILEGES display aliases, never inferred from its Context column.
_MARIADB_DISPLAY_NAMES = {'REPLICATION CLIENT': 'BINLOG MONITOR',
                          'READ_ONLY ADMIN': 'READ_ONLY ADMIN',
                          'READ ONLY ADMIN': 'READ_ONLY ADMIN',
                          'REPLICA MONITOR': 'SLAVE MONITOR',
                          'DELETE VERSIONING ROWS': 'DELETE HISTORY'}
_PRIVILEGE_DESCRIPTIONS = {
    'SELECT': 'Read rows.', 'INSERT': 'Add rows.', 'UPDATE': 'Change rows.', 'DELETE': 'Delete rows.',
    'CREATE': 'Create databases and tables.', 'DROP': 'Drop databases, tables and views.',
    'INDEX': 'Create or remove indexes.', 'ALTER': 'Change table definitions.',
    'REFERENCES': 'Create foreign key references.', 'CREATE VIEW': 'Create views.',
    'SHOW VIEW': 'Read view definitions.', 'TRIGGER': 'Create and use triggers.',
    'CREATE TEMPORARY TABLES': 'Create temporary tables.', 'LOCK TABLES': 'Lock tables.',
    'CREATE ROUTINE': 'Create stored functions and procedures.', 'EXECUTE': 'Run stored routines.',
    'ALTER ROUTINE': 'Alter or drop stored routines.', 'EVENT': 'Create and manage scheduled events.',
    'DELETE HISTORY': 'Delete historical rows from versioned tables.',
    'SHOW CREATE ROUTINE': 'Read stored routine definitions.',
    'PROXY': 'Connect using another account\'s privileges.',
    'FILE': 'Read and write server files within the server\'s file access rules.',
    'PROCESS': 'See other sessions\' statement text.', 'SUPER': 'Legacy server administration.',
    'CREATE USER': 'Create, change and drop accounts.', 'CREATE ROLE': 'Create roles.',
    'DROP ROLE': 'Drop roles.', 'SHUTDOWN': 'Stop the server.',
    'RELOAD': 'Flush or refresh server resources.', 'SHOW DATABASES': 'List all databases.',
    'CREATE TABLESPACE': 'Manage tablespaces.', 'REPLICATION SLAVE': 'Read the binary log.',
    'REPLICATION CLIENT': 'Read replication status.', 'BINLOG MONITOR': 'Read binary log status.',
    'SLAVE MONITOR': 'Read replica status.', 'READ_ONLY ADMIN': 'Write while read_only permits an admin bypass.',
    'FLUSH_TABLES': 'Flush tables and acquire a global read lock.',
    'SYSTEM_USER': 'Protect this account from ordinary administrators.',
    'CONNECTION_ADMIN': 'Manage sessions and bypass read_only.',
    'CONNECTION ADMIN': 'Manage sessions.', 'ROLE_ADMIN': 'Grant roles.',
    'SET_ANY_DEFINER': 'Set another account as an object definer.',
    'ALLOW_NONEXISTENT_DEFINER': 'Allow objects with a missing definer.',
    'SET USER': 'Set another account as an object definer.',
}


def normalize_privilege(name, engine):
    name = ' '.join(str(name).upper().split())
    return _MARIADB_DISPLAY_NAMES.get(name, name) if engine == 'mariadb' else name


def builtin_dynamic_privileges(version):
    version = version_tuple(version)
    names = set()
    for since, added in _MYSQL_DYNAMIC_SINCE:
        if version >= since:
            names.update(added.split())
    if version >= (8, 4, 0):
        names.discard('SET_USER_ID')
    return names


def privilege_catalog(engine, version, rows=None):
    """Valid ON privileges by level. SHOW PRIVILEGES supplies presence only.

    ``None`` means the read failed and uses the versioned static fallback.
    Runtime MySQL registrations may contain digits; spaces are not valid
    dynamic privilege identifiers. USAGE, ALL and GRANT OPTION are separate
    statement semantics, not privilege checkboxes.
    """
    source = 'server' if rows else 'static'
    present = None if not rows else {normalize_privilege(row[0], engine) for row in rows if row}
    if engine == 'mysql':
        dynamic = (builtin_dynamic_privileges(version) if present is None else
                   {n for n in present - STATIC - {'PROXY'} if re.fullmatch(r'[A-Z][A-Z0-9_]*', n)})
    else:
        dynamic = set()
    catalog = {}
    for level in GRANT_LEVELS:
        names = {'PROXY'} if level == 'proxy' else set(static_privileges(engine, version, level))
        if present is not None:
            names &= present
        if level == 'global':
            names |= dynamic
        catalog[level] = [dict(name=name,
                               description=_PRIVILEGE_DESCRIPTIONS.get(name,
                                   'Server-registered privilege.' if name in dynamic else 'Server administration.'),
                               dynamic=name in dynamic, grantable_here=True)
                          for name in sorted(names)]
    return catalog, {'engine': engine, 'version': version, 'source': source}



def tokens(text):
    return [m.group() for m in _TOKEN.finditer(text or '')]


def unquote(token):
    if len(token) > 1 and token[0] in "`\"'" and token[-1] == token[0]:
        return token[1:-1].replace(token[0] * 2, token[0])
    return token


def split_tokens(parts, separator=','):
    result, current, depth = [], [], 0
    for token in parts:
        if token == separator and depth == 0:
            result.append(current)
            current = []
        else:
            current.append(token)
        depth += (token == '(') - (token == ')')
    if current:
        result.append(current)
    return result


def _word_index(parts, word):
    return next((i for i, p in enumerate(parts) if p.upper() == word), -1)


def redact(text, secrets=()):
    """Credential-safe SQL or error text: every known secret, every literal
    after IDENTIFIED BY/AS/USING, PASSWORD('...'), REPLACE '...', hex hashes,
    and whole IDENTIFIED clauses (plugin AS and OR chains)."""
    text = str(text or '')
    for secret in secrets:
        if secret:
            text = dbops.redact(text, secret)
    text = dbops.redact(text)
    text = re.sub(r"(?is)(\bREPLACE\s+)(?:'(?:''|\\.|[^'\\])*')", r"\1'[redacted]'", text)
    return strip_credentials(text) if re.search(r'\bIDENTIFIED\b', text, re.I) else text


def account_tokens(parts):
    """A quoted ``user@host`` or a MariaDB role (or PUBLIC), from tokens."""
    if len(parts) == 1:
        return {'name': unquote(parts[0]), 'host': '', 'role': True}
    if len(parts) == 3 and parts[1] == '@':
        return {'name': unquote(parts[0]), 'host': unquote(parts[2]), 'role': False}
    raise ValueError('Unrecognized account in SHOW GRANTS.')


def _grant_tail(tail):
    """Grantee and WITH options, excluding MariaDB TLS/resource clauses."""
    ends = [i for i, token in enumerate(tail) if token.upper() in ('REQUIRE', 'WITH')]
    source = account_tokens(tail[:min(ends)] if ends else tail)
    with_at = _word_index(tail, 'WITH')
    options = [t.upper() for t in tail[with_at + 1:]] if with_at >= 0 else []
    # sql/sql_acl.cc:9364-9418 prints REQUIRE before WITH, then GRANT
    # OPTION before resource limits; quoted TLS strings remain one token.
    return source, options


def grant_source(line):
    """The grantee of a GRANT/REVOKE, using the parser's token rules.

    Used to retain provenance when simulating changes to a role graph.
    SET DEFAULT ROLE is metadata, not a privilege contribution.
    """
    parts = tokens(strip_credentials(line).strip().rstrip(';'))
    if not parts or parts[0].upper() not in ('GRANT', 'REVOKE'):
        return None
    boundary = _word_index(parts, 'FROM' if parts[0].upper() == 'REVOKE' else 'TO')
    if boundary < 0:
        raise ValueError('Unrecognized grant source.')
    return _grant_tail(parts[boundary + 1:])[0]


def quote_identifier(value):
    """Backquoted identifier; identifiers have no backslash escapes, so the
    text means the same under any sql_mode."""
    if not isinstance(value, str) or '\0' in value or any(c in value for c in '\r\n'):
        raise ValueError('Names cannot contain NUL or line breaks.')
    return '`' + value.replace('`', '``') + '`'


def quote_account(name, host='%', role=False, engine='mysql'):
    if role and engine == 'mariadb':
        return quote_identifier(name)
    return quote_identifier(name) + '@' + quote_identifier(host)


def _pattern(pattern, literal):
    """``(name, wildcard)`` of a stored database pattern. With partial_revokes
    ON (MySQL) every name matches literally; otherwise ``\\_`` and ``\\%``
    are literal characters and a bare ``_`` or ``%`` is a wildcard."""
    if literal:
        return pattern, False
    out, wild, i = [], False, 0
    while i < len(pattern):
        if pattern[i] == '\\' and i + 1 < len(pattern):
            out.append(pattern[i + 1])
            i += 2
        else:
            wild = wild or pattern[i] in '_%'
            out.append(pattern[i])
            i += 1
    return ''.join(out), wild


def parse_grants(lines, engine='mysql', version='8.4.0', partial_revokes=False, checked=True):
    """The grant model of SHOW GRANTS lines; never holds a credential.

    Keys: ``global`` and ``dynamic`` (privilege names), ``global_grantable``
    (names held WITH GRANT OPTION at ``*.*``), ``static_grant_option``,
    ``schemas`` (``pattern`` as stored, ``database`` literal name,
    ``wildcard``), ``restrictions`` (MySQL partial revokes), ``tables``,
    ``columns``, ``routines`` (PROCEDURE, FUNCTION, LIBRARY, PACKAGE, PACKAGE
    BODY), ``proxies``, ``roles`` (``name``, ``host``, ``admin_option``,
    ``default``, ``active``, ``grantee``), ``unparsed`` and ``grants`` (the
    lines without credentials). Every entry keeps ``source`` (the grantee:
    the account, a role on MariaDB, or PUBLIC) and ``grantable``. A line the
    parser does not understand is kept in ``unparsed``, never read as an
    absence.
    """
    text_version = '.'.join(str(n) for n in version_tuple(version)) if isinstance(version, (tuple, list)) else str(version)
    result = {'engine': engine, 'version': text_version, 'checked': checked, 'fresh_session': True,
              'partial_revokes': bool(partial_revokes), 'global': [], 'dynamic': [], 'global_grantable': [],
              'static_grant_option': False, 'schemas': [], 'tables': [], 'columns': [], 'routines': [],
              'proxies': [], 'restrictions': [], 'roles': [], 'unparsed': [], 'grants': []}
    defaults = []
    for raw in lines or ():
        line = strip_credentials(raw).strip().rstrip(';').strip()
        if not line:
            continue
        result['grants'].append(redact(line))
        parts = tokens(line)
        upper = [t.upper() for t in parts]
        try:
            if upper[:3] == ['SET', 'DEFAULT', 'ROLE']:
                boundary = _word_index(parts, 'FOR')
                if boundary < 0:
                    raise ValueError()
                if upper[3:boundary] != ['NONE']:
                    defaults.extend(account_tokens(p) for p in split_tokens(parts[3:boundary]))
                continue
            if upper[0] not in ('GRANT', 'REVOKE'):
                raise ValueError()
            revoke = upper[0] == 'REVOKE'
            boundary = _word_index(parts, 'FROM' if revoke else 'TO')
            on = _word_index(parts, 'ON')
            if boundary < 0:
                raise ValueError()
            tail = parts[boundary + 1:]
            source, options = _grant_tail(tail)
            grantable = options[:2] == ['GRANT', 'OPTION']
            if on < 0 or on > boundary:
                if revoke:
                    raise ValueError()
                for item in split_tokens(parts[1:boundary]):
                    role = account_tokens(item)
                    role.update(admin_option=options[:2] == ['ADMIN', 'OPTION'],
                                default=False, active=False, grantee=source)
                    result['roles'].append(role)
                continue
            names, columns = [], []
            for item in split_tokens(parts[1:on]):
                paren = item.index('(') if '(' in item else -1
                name = ' '.join(item[:paren] if paren >= 0 else item).upper()
                if not name or not re.match(r'^[A-Z_][A-Z0-9_ ]*$', name):
                    raise ValueError()
                if engine == 'mariadb' and name == 'REPLICATION CLIENT':
                    name = 'BINLOG MONITOR'
                if paren >= 0:
                    if item[-1] != ')':
                        raise ValueError()
                    columns.append((name, [unquote(p[0]) for p in split_tokens(item[paren + 1:-1]) if len(p) == 1]))
                else:
                    names.append(name)
            target = parts[on + 1:boundary]
            kind = 'TABLE'
            if target and target[0].upper() in ('TABLE', 'PROCEDURE', 'FUNCTION', 'LIBRARY', 'PACKAGE'):
                kind = target.pop(0).upper()
                if kind == 'PACKAGE' and target and target[0].upper() == 'BODY':
                    target.pop(0)
                    kind = 'PACKAGE BODY'
            entry = {'privs': names, 'grantable': grantable, 'source': source}
            if names == ['PROXY'] and not revoke:
                entry['account'] = account_tokens(target)
                result['proxies'].append(entry)
                continue
            if len(target) != 3 or target[1] != '.':
                raise ValueError()
            database, obj = unquote(target[0]), unquote(target[2])
            if target == ['*', '.', '*'] and not revoke and not columns:
                for name in names:
                    bucket = ('dynamic' if engine == 'mysql' and name not in STATIC and name not in ALL_NAMES
                              else 'global')
                    if name not in result[bucket]:
                        result[bucket].append(name)
                    if grantable and bucket == 'global':
                        result['static_grant_option'] = True
                    if grantable and name not in result['global_grantable']:
                        result['global_grantable'].append(name)
                continue
            if target[2] == '*':
                literal, wildcard = _pattern(database, partial_revokes and engine == 'mysql')
                entry.update(pattern=database, database=literal, wildcard=wildcard)
                result['restrictions' if revoke else 'schemas'].append(entry)
            elif revoke:
                raise ValueError()
            elif kind != 'TABLE':
                entry.update(database=database, name=obj, kind=kind)
                result['routines'].append(entry)
            else:
                entry.update(database=database, table=obj)
                if names:
                    result['tables'].append(entry)
                for name, cols in columns:
                    result['columns'].append(dict(entry, privs=[name], columns=cols))
        except (ValueError, IndexError):
            result['unparsed'].append(redact(line))
    for role in result['roles']:
        role['default'] = any(d['name'] == role['name'] and (engine == 'mariadb' or d['host'] == role['host'])
                              for d in defaults)
    return result


def _matches(pattern, database, literal=False):
    if literal:
        return pattern == database
    expression, i = '', 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '\\' and i + 1 < len(pattern):
            expression += re.escape(pattern[i + 1])
            i += 2
            continue
        expression += '.*' if ch == '%' else '.' if ch == '_' else re.escape(ch)
        i += 1
    return re.fullmatch(expression, database, re.S) is not None


def _mariadb_super(model, privilege):
    """What a MariaDB SUPER grant says about a split privilege: True on
    versions where SUPER still covers it, None where it covers it only for
    grants stored by an earlier version (SHOW GRANTS omits version_id),
    False where SUPER never covered it."""
    v = version_tuple(model.get('version'))
    if privilege in dbops._MARIADB_SUPER_BEFORE_11:
        return True if v < dbops.MARIADB_SUPER_SPLIT_END else None
    if privilege == 'READ_ONLY ADMIN':
        return True if v < dbops.MARIADB_SUPER_READ_ONLY_END else None
    return False


def has(model, privilege, level='global', database=None, table=None, columns=None,
        routine=None, kind='PROCEDURE', grantable=False, proxy=None):
    """True, False or None (unknown). Only a global (or dynamic) privilege's
    absence is provably False; any other absence is unknown."""
    if not model.get('checked', True) or not model.get('fresh_session', True) or model.get('unparsed'):
        return None
    p = privilege.upper()
    have = set(model.get('global', []) + model.get('dynamic', []))
    static = static_privileges(model.get('engine'), model.get('version'), 'global') | {'GRANT OPTION', 'USAGE'}
    all_static = bool(have & ALL_NAMES) and p in static - {'GRANT OPTION', 'USAGE'}
    present = p in have or all_static or (p == 'GRANT OPTION' and model.get('static_grant_option', False))
    if grantable:
        # Static GRANT_ACL is a scope-wide bit, including across roles:
        # mysql-server/sql/auth/auth_acls.h:46-64;
        # mariadb-server/sql/sql_acl.cc:3491-3496,12712-12713.
        # Dynamic grants have their own option, never that static bit:
        # mysql-server/sql/auth/sql_authorization.cc:421-437.
        present = present and (model.get('static_grant_option', False) if p in static
                               else p in model.get('global_grantable', []))
    # MySQL filters global bits by the database's restrictions at access
    # time (mysql-server/sql/auth/sql_security_ctx.cc:323-333,1198-1209).
    # Without an object, a restricted global grant cannot prove access
    # everywhere or absence everywhere: keep the answer unknown.
    restrictions = [e for e in model.get('restrictions', []) if p in e['privs']]
    restricted = any(not database or _matches(e['pattern'], database, True) for e in restrictions)
    if level == 'global':
        if present and restricted:
            return None
        if not present and not grantable and model.get('engine') == 'mariadb' and 'SUPER' in have:
            answer = _mariadb_super(model, p)
            if answer is not False:
                return answer
        return bool(present)
    if level == 'proxy':
        if proxy and any(e['account']['name'] == proxy.get('name') and e['account']['host'] == proxy.get('host', '%')
                         and (not grantable or e['grantable']) for e in model.get('proxies', [])):
            return True
        return None
    if not database:
        return True if present and not restricted else None
    if present and not restricted:
        return True
    literal = bool(model.get('partial_revokes')) and model.get('engine') == 'mysql'
    candidates = [e for e in model.get('schemas', []) if _matches(e['pattern'], database, literal)]
    # Per grantee (the account, a MariaDB role, PUBLIC) the exact name entry
    # decides, else the matching patterns (disagreeing: unknown, the server
    # takes the first by its sort order); the grantees' results are ORed
    # (Ma/sql/sql_acl.cc:3928-3940; MySQL merges active roles into the lines).
    by_source = {}
    for e in candidates:
        source = e.get('source') or {}
        by_source.setdefault((source.get('name'), source.get('host')), []).append(e)
    results = []
    for entries in by_source.values():
        exact = [e for e in entries if not e['wildcard'] and e['database'] == database]
        values = [_entry_has(model, e, p, 'database', grantable)
                  for e in (exact or entries)]
        results.append(True if all(values) else None if any(values) else False)
    if True in results:
        return True
    for e in model.get('tables', []) + model.get('routines', []):
        if e['database'] != database or (grantable and not e['grantable']):
            continue
        if not _entry_has(model, e, p, 'table' if 'table' in e else 'routine', grantable):
            continue
        if level in ('table', 'column') and e.get('table') == table:
            return True
        if level == 'routine' and e.get('name') == routine and e.get('kind') == kind:
            return True
    if level == 'column' and columns:
        held = set()
        for e in model.get('columns', []):
            if e['database'] == database and e['table'] == table and p in e['privs'] and (not grantable or e['grantable']):
                held.update(e['columns'])
        if set(columns) <= held:
            return True
    return None


def _entry_has(model, entry, privilege, level, grantable):
    if privilege == 'GRANT OPTION':
        return bool(entry['grantable'])
    all_names = static_privileges(model.get('engine'), model.get('version'), level)
    return bool((privilege in entry['privs'] or
                 (privilege in all_names and set(entry['privs']) & ALL_NAMES)) and
                (not grantable or entry['grantable']))


# ---- the action map ------------------------------------------------------------------

#: Every action key the privileges-1.0 report answers, in display order, with
#: its kind: ``read`` (a view: never greyed, it says what it lacks), ``action``
#: (runs statements but changes no table data or account) and ``write``
#: (changes data, schema or accounts, so the server's read_only applies).
#: The spec is .claude/skills/accounts/references/action-privileges.md;
#: docs/DEVELOPER_REFERENCE.md lists the keys.
ACTION_KEYS = (
    ('privileges', 'read'), ('health', 'read'), ('config', 'read'), ('config_sources', 'read'),
    ('top_statements', 'read'), ('sessions_own', 'read'), ('sessions_all', 'read'),
    ('explain_connection', 'read'), ('locks_transactions', 'read'), ('locks_metadata', 'read'),
    ('innodb_status', 'read'), ('error_log', 'read'), ('waits', 'read'), ('io', 'read'), ('instruments', 'read'),
    ('indexes_usage', 'read'), ('table_lists', 'read'), ('tablespaces', 'read'), ('database_picker', 'read'),
    ('replication_status', 'read'), ('binlog_status', 'read'), ('relay_events', 'read'),
    ('routine_body', 'read'), ('view_definition', 'read'), ('accounts_list', 'read'), ('grants_other', 'read'),
    ('explain', 'action'), ('run_analysis', 'action'), ('kill', 'action'), ('kill_own', 'action'),
    ('set_global', 'action'), ('set_persist', 'action'), ('set_persist_only', 'action'),
    ('reset_persist', 'action'), ('set_session_restricted', 'action'), ('replica_start_stop', 'action'),
    ('skip_gtid', 'action'), ('skip_counter', 'action'), ('clone_donor', 'action'), ('clone_recipient', 'action'),
    ('dump_shell', 'action'), ('dump_mysqldump', 'action'), ('dump_mariadb', 'action'), ('restore', 'write'),
    ('insert', 'write'), ('update', 'write'), ('delete', 'write'), ('create_table', 'write'),
    ('alter_table', 'write'), ('create_index', 'write'), ('create_view', 'write'), ('drop_table', 'write'),
    ('truncate_table', 'write'), ('create_temporary_table', 'action'), ('analyze_table', 'action'),
    ('optimize_table', 'action'), ('flush', 'action'), ('backup_lock', 'action'), ('instruments_presets', 'action'),
    ('read_only_bypass', 'action'),
    ('create_user', 'write'), ('create_role', 'write'), ('drop_user', 'write'), ('drop_role', 'write'),
    ('rename_user', 'write'), ('lock_account', 'write'), ('unlock_account', 'write'),
    ('expire_password', 'write'), ('account_options', 'write'), ('set_password', 'write'),
    ('set_own_password', 'write'), ('grant', 'write'), ('revoke', 'write'), ('grant_role', 'write'),
    ('revoke_role', 'write'), ('default_roles', 'write'),
)
ACTIONS = tuple(key for key, _kind in ACTION_KEYS)
_KIND = dict(ACTION_KEYS)
#: Other names accepted by can() and requirements(): dbops operation names and
#: earlier spellings. The report uses only ACTION_KEYS.
ALIASES = {'replication': 'replica_start_stop', 'start_replica': 'replica_start_stop',
           'stop_replica': 'replica_start_stop', 'write_when_read_only': 'read_only_bypass',
           'create': 'create_table', 'alter': 'alter_table', 'drop': 'drop_table', 'index': 'create_index',
           'kill_query': 'kill', 'capture': 'explain', 'analyze': 'run_analysis', 'histograms': 'analyze_table',
           'innodb': 'innodb_status', 'error-log': 'error_log', 'top': 'top_statements', 'sessions': 'sessions_own',
           'locks_rows': 'locks_transactions', 'tables': 'table_lists', 'indexes': 'table_lists',
           'lock': 'lock_account', 'unlock': 'unlock_account', 'expire': 'expire_password',
           'rename': 'rename_user', 'password': 'set_password', 'alter_user': 'account_options',
           'options': 'account_options', 'accounts': 'accounts_list', 'table_create': 'create_table',
           'table_alter': 'alter_table', 'table_drop': 'drop_table', 'truncate': 'truncate_table',
           'temporary_table': 'create_temporary_table', 'view_create': 'create_view', 'dump': 'dump_mysqldump',
           'select': 'explain'}
#: Writes the server's read_only refuses without the bypass (account
#: statements are refused by the grant-table lock checks: M/sql/lock.cc:211-217,
#: Ma/sql/lock.cc:204-238; verified live on MySQL 8.0, 8.4, 9.7 and MariaDB
#: 10.11, 11.4 with error 1290).
ACCOUNT_ACTIONS = frozenset(('create_user', 'create_role', 'drop_user', 'drop_role', 'rename_user', 'lock_account',
                             'unlock_account', 'expire_password', 'account_options', 'set_password',
                             'set_own_password', 'grant', 'revoke', 'grant_role', 'revoke_role', 'default_roles'))
_DBOPS = {'kill': 'kill', 'set_global': 'set_global', 'set_persist': 'set_persist',
          'set_persist_only': 'set_persist_only', 'reset_persist': 'reset_persist',
          'set_session_restricted': 'set_session_restricted', 'replica_start_stop': 'replication',
          'clone_donor': 'clone_donor', 'clone_recipient': 'clone_recipient', 'read_only_bypass': 'write_when_read_only'}
_OBJECT = {'insert': ['INSERT'], 'update': ['UPDATE'], 'delete': ['DELETE'], 'create_table': ['CREATE'],
           'alter_table': ['ALTER'], 'create_index': ['INDEX'], 'drop_table': ['DROP'], 'truncate_table': ['DROP'],
           'create_view': ['CREATE VIEW', 'SELECT'], 'analyze_table': ['SELECT', 'INSERT'],
           'optimize_table': ['SELECT', 'INSERT']}


def canonical(action):
    """The ACTION_KEYS name of *action* (aliases resolved)."""
    return ALIASES.get(action, action)


def _atom(p, level='global', **target):
    return dict(target, privilege=p, level=level)


def requirements(action, engine='mysql', version='8.4.0', **target):
    """``{"available", "alternatives", "read", "note"}``: any one alternative
    (a list of privilege atoms, all needed together) is enough; an empty
    alternative needs nothing. Context-sensitive actions take the target:
    ``database``, ``table``, ``columns``, ``routine``, ``kind``, ``proxy``,
    ``privileges`` and ``level`` (grant, revoke), ``variable``, ``own_user``
    (kill: same user name), ``own_account`` (password, default roles: same
    user and host), ``definer``, ``data_directory``, ``roles``."""
    maria, v = engine == 'mariadb', version_tuple(version)
    action = canonical(action)
    obj = {k: target[k] for k in ('database', 'table', 'columns', 'routine', 'kind', 'proxy') if k in target}

    def g(*alternatives):
        return [[_atom(p) for p in a.split('+') if p] for a in alternatives]

    def o(*privs, **where):
        level = where.pop('level', 'table')
        return [[_atom(p, level, **dict(obj, **where)) for p in privs]]

    def mysql_db(p):
        # One atom: p on the mysql database (never the request's table).
        return _atom(p, 'database', database='mysql')
    available, note = True, ''
    if action not in _KIND:
        return {'available': True, 'alternatives': [], 'read': False, 'note': 'Unknown action; the server decides.'}
    if action in ('privileges', 'health', 'config', 'sessions_own', 'table_lists', 'database_picker'):
        alternatives = g('')
        if action == 'table_lists':
            note = 'Lists only tables the account has some privilege on.'
    elif action == 'config_sources':
        alternatives = g('')
        if maria:
            note = 'The option file path (GLOBAL_VALUE_PATH) needs FILE.'
    elif action in ('top_statements', 'waits', 'io', 'instruments', 'indexes_usage'):
        alternatives = o('SELECT', level='database', database='performance_schema')
    elif action == 'locks_metadata':
        alternatives = g('') if maria else o('SELECT', level='database', database='performance_schema')
    elif action in ('sessions_all', 'innodb_status', 'tablespaces'):
        alternatives = g('PROCESS')
    elif action == 'error_log':
        # MariaDB reads settings and a verified local file, never SQL file access.
        alternatives = g('') if maria else o('SELECT', database='performance_schema', table='error_log')
        if maria:
            note = 'Needs a verified local error log file readable by this process; no SQL privilege is needed.'
    elif action == 'locks_transactions':
        alternatives = g('PROCESS') if maria else [g('PROCESS')[0] + o('SELECT', level='database',
                                                                         database='performance_schema')[0]]
    elif action == 'explain_connection':
        if target.get('own_user'):
            alternatives = g('')
        elif maria:
            alternatives = g('PROCESS')
        else:
            # check_access(GLOBAL_ACLS & ~GRANT_ACL): every static global
            # privilege but GRANT OPTION (M/sql/opt_explain.cc:2456).
            alternatives = g('+'.join(sorted(STATIC - {'USAGE', 'GRANT OPTION'})))
    elif action in ('replication_status', 'binlog_status', 'relay_events'):
        if maria:
            alternatives = g('BINLOG MONITOR' if action == 'binlog_status' else 'SLAVE MONITOR')
        else:
            alternatives = g('REPLICATION SLAVE') if action == 'relay_events' else g('REPLICATION CLIENT', 'SUPER')
    elif action == 'routine_body':
        if target.get('definer'):
            alternatives = g('')
        elif maria:
            alternatives = (g('SHOW CREATE ROUTINE') if v >= (11, 3, 0) else []) + \
                o('SELECT', level='table', database='mysql', table='proc')
        else:
            alternatives = g('SELECT') + (g('SHOW_ROUTINE') if v >= (8, 0, 20) else [])
    elif action == 'view_definition':
        alternatives = o('SHOW VIEW') if maria else o('SELECT', 'SHOW VIEW')
    elif action == 'accounts_list':
        alternatives = o('SELECT', level='table', database='mysql', table='global_priv' if maria else 'user')
    elif action == 'grants_other':
        alternatives = (o('SELECT', level='database', database='mysql') if maria
                        else o('SELECT', level='table', database='mysql', table='user'))
    elif action in ('explain', 'run_analysis'):
        alternatives = o(*target.get('privileges', ['SELECT']))
    elif action == 'kill_own':
        alternatives = g('')
    elif action in _DBOPS:
        need = dbops.required_privileges(_DBOPS[action], engine, v, target.get('variable'),
                                         target.get('read_only_variable', False))
        available, note = need['available'], need['note']
        alternatives = [[_atom(p) for p in a] for a in need['any_of']]
        if maria and action == 'set_global':
            p = {'read_only': 'READ_ONLY ADMIN', 'max_connections': 'CONNECTION ADMIN',
                 'sql_slave_skip_counter': 'SUPER'}.get(target.get('variable'))
            if p:
                alternatives = g(p)
        if action == 'kill' and target.get('own_user'):
            alternatives = g('')
        if action == 'clone_recipient' and target.get('data_directory'):
            alternatives = g('BACKUP_ADMIN')
        if action in ('clone_donor', 'clone_recipient') and not maria and v < (8, 0, 17):
            available = False
    elif action in ('skip_gtid', 'skip_counter'):
        if maria:
            available = action != 'skip_gtid'
            alternatives = g('SUPER+REPLICATION SLAVE ADMIN')
        else:
            first = (g('SUPER', 'SYSTEM_VARIABLES_ADMIN', 'SESSION_VARIABLES_ADMIN', 'REPLICATION_APPLIER')
                     if action == 'skip_gtid' else g('SYSTEM_VARIABLES_ADMIN', 'SUPER'))
            if target.get('tagged_gtid'):
                first = [a + [_atom('TRANSACTION_GTID_TAG')] for a in first if a != [_atom('SUPER')]]
            alternatives = [a + b for a in first for b in g('REPLICATION_SLAVE_ADMIN', 'SUPER')]
    elif action in _OBJECT or action in ('restore', 'create_temporary_table', 'dump_shell', 'dump_mysqldump',
                                         'dump_mariadb'):
        if action == 'restore':
            alternatives = o('CREATE', 'INSERT', 'ALTER', 'INDEX', 'CREATE VIEW', 'CREATE ROUTINE', 'TRIGGER',
                             'EVENT', level='database')
            note = 'A DEFINER of another account also needs SET USER (MariaDB) or SET_ANY_DEFINER or SUPER (MySQL).'
        elif action == 'create_temporary_table':
            alternatives = o('CREATE TEMPORARY TABLES', level='database')
        elif action.startswith('dump'):
            available = action == 'dump_mariadb' if maria else action != 'dump_mariadb'
            alternatives = o('SELECT', level='database')
            if action == 'dump_shell':
                alternatives = [a + b for a in alternatives for b in g('RELOAD+BACKUP_ADMIN+REPLICATION CLIENT',
                                                                       'FLUSH_TABLES+BACKUP_ADMIN+REPLICATION CLIENT')]
            if not target.get('single_transaction'):
                alternatives = [a + o('LOCK TABLES', level='database')[0] for a in alternatives]
            for key, p in (('triggers', 'TRIGGER'), ('events', 'EVENT'), ('views', 'SHOW VIEW')):
                if target.get(key, key == 'triggers'):
                    alternatives = [a + o(p, level='database')[0] for a in alternatives]
            if target.get('source_data'):
                extra = g('RELOAD+BINLOG MONITOR') if maria else g('RELOAD+REPLICATION CLIENT',
                                                                   'FLUSH_TABLES+REPLICATION CLIENT')
                alternatives = [a + b for a in alternatives for b in extra]
            if action == 'dump_mysqldump' and not target.get('no_tablespaces'):
                alternatives = [a + [_atom('PROCESS')] for a in alternatives]
        else:
            ps = list(_OBJECT[action])
            if action == 'alter_table' and target.get('rename'):
                ps += ['DROP', 'INSERT', 'CREATE']
            elif action == 'alter_table' and target.get('drop_partition'):
                ps += ['DROP']
            if action == 'create_table' and target.get('from_select'):
                ps += ['INSERT', 'SELECT']
            if action == 'create_view' and target.get('replace'):
                ps += ['DROP']
            alternatives = o(*ps)
            if action == 'optimize_table' and target.get('local') and not maria:
                available = v >= (8, 4, 0)
                alternatives = [a + [_atom('OPTIMIZE_LOCAL_TABLE')] for a in alternatives]
    elif action in ('flush', 'backup_lock'):
        if action == 'backup_lock':
            available = not maria
            alternatives = g('BACKUP_ADMIN')
        else:
            alternatives = g('RELOAD')
            if not maria and target.get('what') in ('TABLES', 'STATUS', 'OPTIMIZER_COSTS', 'USER_RESOURCES') \
                    and v >= (8, 0, 23):
                alternatives += g('FLUSH_' + target['what'])
    elif action == 'instruments_presets':
        alternatives = o('UPDATE', 'INSERT', 'DELETE', level='database', database='performance_schema')
        note = 'myflames prints this SQL and never runs it.'
    elif action in ('create_user', 'create_role', 'drop_user', 'drop_role', 'rename_user', 'lock_account',
                    'unlock_account', 'expire_password', 'account_options', 'default_roles'):
        p = ('INSERT' if action in ('create_user', 'create_role') else
             'DELETE' if action in ('drop_user', 'drop_role') else 'UPDATE')
        alternatives = g('CREATE USER') + [[mysql_db(p)]]
        if not maria and action in ('create_role', 'drop_role'):
            alternatives = g('CREATE ROLE' if action == 'create_role' else 'DROP ROLE', 'CREATE USER')
        if maria and action == 'create_user':
            # The password step is SET PASSWORD for another account: UPDATE on
            # mysql, CREATE USER does not suffice (Ma/sql/sql_acl.cc:4224-4234).
            alternatives = [a + [mysql_db('UPDATE')] for a in alternatives]
            note = 'Setting the new account\'s password needs UPDATE on the mysql database.'
        if action == 'default_roles':
            if target.get('own_account'):
                alternatives = g('')
            elif maria:
                alternatives = [[mysql_db('UPDATE')]]
    elif action in ('set_password', 'set_own_password'):
        if action == 'set_own_password' or target.get('own_account'):
            alternatives = g('')
            note = 'Your own account (same user and host) needs no privilege.'
        elif maria:
            alternatives = [[mysql_db('UPDATE')]]
            note = 'SET PASSWORD for another account needs UPDATE on the mysql database; CREATE USER does not suffice.'
        else:
            alternatives = g('CREATE USER') + [[mysql_db('UPDATE')]]
    elif action in ('grant_role', 'revoke_role'):
        alternatives = [] if maria else g('ROLE_ADMIN', 'SUPER')
        note = 'WITH ADMIN OPTION on every role is an alternative.'
    else:  # grant, revoke
        ps = [' '.join(str(x).upper().split()) for x in target.get('privileges', [])]
        level = target.get('level', 'global')
        if not ps:
            # Without named privileges: GRANT OPTION somewhere (a database
            # grant option cannot be ruled out from SHOW GRANTS).
            alternatives = [[_atom('GRANT OPTION', 'database')]]
            note = 'The privileges granted are needed too, with GRANT OPTION at that level.'
        else:
            if set(ps) & ALL_NAMES:
                mask = {'global': STATIC, 'database': DATABASE_PRIVILEGES, 'table': TABLE_PRIVILEGES,
                        'column': COLUMN_PRIVILEGES, 'routine': ROUTINE_PRIVILEGES}.get(level, frozenset())
                if maria:
                    if level == 'global':
                        mask = (mask | MARIADB_EXTRA) - {'CREATE ROLE', 'DROP ROLE', 'REPLICATION CLIENT'}
                    elif level in ('database', 'table'):
                        mask |= {'DELETE HISTORY'}
                        if level == 'database' and v >= (11, 3, 0):
                            mask |= {'SHOW CREATE ROUTINE'}
                    if v < (11, 3, 0):
                        mask -= {'SHOW CREATE ROUTINE'}
                elif level == 'global':
                    if 'all_privileges' not in target:
                        return {'available': True, 'alternatives': [], 'read': False,
                                'note': 'ALL needs the server-registered privilege list.'}
                    mask |= set(target['all_privileges'])
                ps = sorted(mask - {'GRANT OPTION', 'USAGE', 'PROXY'})
            static, dynamic = [], []
            for privilege in ps:
                atom = _atom(privilege, level, grantable=True, **obj)
                if not maria and level == 'global' and privilege not in STATIC and privilege not in ALL_NAMES:
                    dynamic.append(atom)
                else:
                    static.append(atom)
            # UPDATE on mysql replaces the grant option of every dynamic
            # privilege together (M/sql/auth/sql_authorization.cc:432-438).
            alternatives = [static + dynamic]
            if dynamic:
                alternatives.append(static + [mysql_db('UPDATE')])
    unique = []
    for alternative in alternatives:
        atoms = []
        for atom in alternative:
            if atom not in atoms:
                atoms.append(atom)
        if atoms not in unique:
            unique.append(atoms)
    return {'available': available, 'alternatives': unique, 'read': _KIND[action] == 'read', 'note': note}


def _read_only_state(model):
    """``(blocked_for_everyone, needs_bypass, setting)`` from the server's
    read_only variables in *model*."""
    variables = model.get('variables') or {}
    super_ro = str(variables.get('super_read_only', '')).upper() in ('ON', '1')
    ro = str(variables.get('read_only', '')).upper()
    if super_ro:
        return True, False, 'super_read_only=ON'
    if ro == 'NO_LOCK_NO_ADMIN':
        return True, False, 'read_only=NO_LOCK_NO_ADMIN'
    if ro in ('ON', '1', 'NO_LOCK'):
        return False, True, 'read_only=' + ro
    return False, False, ''


def _join(alternative):
    return ' and '.join(a['privilege'] + ('' if a['level'] == 'global' or not a.get('database') else
                                           ' on ' + a['database'] + ('.' + a['table'] if a.get('table') else '.*'))
                        for a in alternative)


def can(model, action, **target):
    """Whether the account in *model* (a privileges-1.0 report or a
    :func:`parse_grants` model) may run *action*.

    Returns ``{"state", "allowed", "available", "missing", "need", "reason",
    "via", "note", "grey_out"}``. ``state`` is ``allowed``, ``denied`` or
    ``unknown`` (``allowed`` True, False or None). ``missing`` lists the
    alternatives still missing (a name, or names joined with " and "; any
    one is enough), empty when allowed. Unknown must leave a control
    enabled; ``grey_out`` is True only for an exact denial of a non-read.
    ``available`` False means the engine or version lacks the feature: do
    not offer it.
    """
    if isinstance(action, dict):
        target = dict(action, **target)
        action = target.pop('action')
    if 'model' in model and isinstance(model['model'], dict):
        model = model['model']
    action = canonical(action)
    engine = model.get('engine', 'mysql')
    spec = requirements(action, engine, model.get('version', ''), **target)
    alternatives = list(spec['alternatives'])
    if action in ('grant_role', 'revoke_role') and target.get('roles'):
        def holds(role):
            role = {'name': role, 'host': '%'} if isinstance(role, str) else role
            return any(r.get('admin_option') and r['name'] == role['name'] and
                       (engine == 'mariadb' or r['host'] == role.get('host', '%')) for r in model.get('roles', []))
        if all(holds(r) for r in target['roles']):
            alternatives = alternatives + [[]]
    checks, missing = [], []
    for alternative in alternatives:
        values = [has(model, a['privilege'], **{k: v for k, v in a.items() if k != 'privilege'}) for a in alternative]
        checks.append(True if all(v is True for v in values) else False if any(v is False for v in values) else None)
        lacking = [a for a, value in zip(alternative, values) if value is not True]
        if lacking:
            missing.append(_join(lacking))
    allowed = True if True in checks else None if not checks or None in checks else False
    if allowed is True:
        missing = []
    if action in ('grant_role', 'revoke_role') and allowed is not True:
        allowed = None  # An admin path through the role graph is not printed.
    if not model.get('checked', True) or not model.get('fresh_session', True) or model.get('unparsed'):
        allowed = None
    detail = ''
    if engine != 'mariadb' and (action in ('kill', 'kill_own') or action in ACCOUNT_ACTIONS - {'create_user',
                                                                                              'create_role'}):
        # Account statements check a direct SYSTEM_USER grant of the target
        # (roles do not count; sql_security_ctx.cc:838); KILL checks the
        # target session, active roles included (auth_common.cc:171).
        system = target.get('target_system_user', None if action in ('kill', 'kill_own') else False)
        admin = has(model, 'SYSTEM_USER')
        if system is True and admin is not True:
            allowed = False if admin is False else (None if allowed is not False else False)
            missing.append('SYSTEM_USER')
            detail = 'The target holds SYSTEM_USER, so the actor needs it too.'
        elif system is None and admin is not True and allowed is True:
            allowed = None
            missing = ['SYSTEM_USER (only when the target session has it)']
            detail = 'Allowed unless the target session has SYSTEM_USER, which also needs SYSTEM_USER.'
    if _KIND.get(action) == 'write' and action != 'read_only_bypass' and allowed is not False:
        everyone, bypass_needed, setting = _read_only_state(model)
        if everyone:
            allowed = False
            missing.append('server is read-only (%s)' % setting)
            detail = 'The server has %s, so nobody can run this.' % setting
        elif bypass_needed:
            bypass = can(dict(model, variables={}), 'read_only_bypass')
            if bypass['allowed'] is not True:
                allowed = False if bypass['allowed'] is False else None
                missing.append('read_only bypass (%s)' % bypass['need'])
                detail = 'The server has %s; writing needs %s.' % (setting, bypass['need'])
    if not spec['available']:
        allowed = False
    via = ''
    if allowed is True:
        missing = []
        held = set(model.get('global', []) + model.get('dynamic', []))
        passing = [alt for alt, check in zip(alternatives, checks) if check is True]
        # Prefer an alternative whose global privileges are held by name (not
        # through MariaDB's SUPER fallback) for the "through" text.
        direct = [alt for alt in passing if all(a['level'] != 'global' or a['privilege'] in held for a in alt)]
        via = _join((direct or passing)[0]) or 'no privilege needed'
    state = 'allowed' if allowed is True else 'denied' if allowed is False else 'unknown'
    need = ' or '.join(_join(alt) or 'no privilege' for alt in alternatives) or spec['note'] or 'unknown'
    account = model.get('account') or 'This account'
    if not spec['available']:
        reason = 'Not available on this engine or version.' + (' ' + spec['note'] if spec['note'] else '')
    elif allowed is True:
        reason = 'No privilege needed.' if via == 'no privilege needed' else 'Allowed through %s.' % via
    elif allowed is False:
        if detail:
            reason = detail
        elif len(missing) > 1:
            reason = 'Needs %s; %s has none of them.' % (' or '.join(missing), account)
        else:
            reason = 'Needs %s; %s does not have it.' % (missing[0] if missing else need, account)
    else:
        reason = detail or 'Not verified: SHOW GRANTS cannot tell; the server decides.'
        if not detail and engine == 'mariadb' and 'SUPER' in model.get('global', []):
            reason = ('Not verified: SUPER covers this only for grants stored by an earlier MariaDB version, '
                      'which SHOW GRANTS does not show; the server decides.')
    return {'state': state, 'allowed': allowed, 'available': spec['available'], 'missing': missing,
            'need': need, 'reason': reason, 'via': via, 'note': spec['note'],
            'grey_out': allowed is False and not spec['read'] and spec['available']}


def statement_privileges(model, statement, database=None):
    """Project one classified editor unit onto the existing action model.

    This is a hint, not a second SQL parser or an execution gate. Unknown
    grammar, targets and variable-specific checks stay unknown. In
    particular, a generic SET privilege must not deny a variable whose
    alternative privilege the generic action does not describe.
    """
    code = statement.get('code', '').strip()
    upper = code.upper()
    idents = statement.get('idents') or []
    actions, targets = [], []
    uncertain = False

    def add(key, **target):
        actions.append(key)
        targets.append(target)

    # The planner has already masked comments, strings and identifiers.
    # Only extract a simple target; compound SQL remains the server's job.
    name = r'(?:`\d+`|[\w$]+)'

    def obj(rest):
        match = re.match(r'\s*(' + name + r')(?:\s*\.\s*(' + name + r'))?', rest)
        if not match:
            return {}
        first = dbops._name(match.group(1), idents)
        second = dbops._name(match.group(2), idents) if match.group(2) else None
        return {'database': first if second else database, 'table': second or first}

    if statement.get('single', True) is False or statement.get('mode_sensitive') \
            or re.search(r'/\*(?:!|M!)', statement.get('send', ''), re.I):
        # Executable comments may be skipped by the server's engine/version.
        # The write classifier deliberately treats them as executable, but
        # that conservative policy classification cannot prove a denial.
        uncertain = True
    elif re.match(r'^(INSERT|REPLACE)\b', upper):
        match = re.match(r'^(INSERT|REPLACE)\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\s+)*(?:INTO\s+)?', code, re.I)
        target = obj(code[match.end():]) if match else {}
        add('insert', **target)
        if upper.startswith('REPLACE'):
            add('delete', **target)
        elif re.search(r'\bON\s+DUPLICATE\s+KEY\s+UPDATE\b', upper):
            add('update', **target)
    elif re.match(r'^UPDATE\b', upper):
        match = re.match(r'^UPDATE\s+(?:(?:LOW_PRIORITY|IGNORE)\s+)*', code, re.I)
        add('update', **obj(code[match.end():] if match else ''))
    elif re.match(r'^DELETE\b', upper):
        match = re.match(r'^DELETE\s+(?:(?:LOW_PRIORITY|QUICK|IGNORE)\s+)*FROM\s+', code, re.I)
        add('delete', **obj(code[match.end():] if match else ''))
    elif re.match(r'^(CREATE|ALTER|DROP|TRUNCATE)\b', upper):
        match = re.match(r'^(CREATE|ALTER|DROP|TRUNCATE)\s+(TEMPORARY\s+)?TABLE\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?', code, re.I)
        if match:
            verb, temporary = match.group(1).lower(), bool(match.group(2))
            if temporary and verb != 'create':
                uncertain = True  # A session's own temporary table needs no DROP privilege.
            else:
                target = obj(code[match.end():])
                target.update(from_select=bool(re.search(r'\bSELECT\b', upper)),
                              rename=bool(re.search(r'\bRENAME\b', upper)),
                              drop_partition=bool(re.search(r'\bDROP\s+PARTITION\b', upper)))
                add('create_temporary_table' if temporary else verb + '_table', **target)
        elif re.match(r'^CREATE\s+(?:(?:UNIQUE|FULLTEXT|SPATIAL)\s+)?INDEX\b', upper):
            match = re.search(r'\bON\s+', code, re.I)
            add('create_index', **obj(code[match.end():] if match else ''))
        elif re.match(r'^CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\b', upper):
            match = re.match(r'^CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+', code, re.I)
            add('create_view', replace=bool(re.match(r'^CREATE\s+OR\s+REPLACE\b', upper)),
                **obj(code[match.end():] if match else ''))
        elif re.match(r'^TRUNCATE\s+', upper):
            add('truncate_table', **obj(code[len('TRUNCATE'):]))
        else:
            match = re.match(r'^(CREATE|ALTER|DROP)\s+(USER|ROLE)\b', upper)
            if match:
                verb, what = match.group(1).lower(), match.group(2).lower()
                key = verb + '_' + what
                if verb == 'alter' and what == 'user':
                    key = ('default_roles' if re.search(r'\bDEFAULT\s+ROLE\b', upper) else
                           'lock_account' if re.search(r'\bACCOUNT\s+LOCK\b', upper) else
                           'unlock_account' if re.search(r'\bACCOUNT\s+UNLOCK\b', upper) else
                           'expire_password' if re.search(r'\bPASSWORD\s+EXPIRE\b', upper) else 'account_options')
                if key in ACTIONS:
                    add(key, target_system_user=None)
    elif re.match(r'^RENAME\s+USER\b', upper):
        add('rename_user', target_system_user=None)
    elif re.match(r'^(GRANT|REVOKE)\b', upper):
        key = upper.split()[0].lower()
        add(key if re.search(r'\bON\b', upper) else key + '_role', target_system_user=None)
    elif re.match(r'^SET\s+PASSWORD\b', upper):
        add('set_password' if re.match(r'^SET\s+PASSWORD\s+FOR\b', upper) else 'set_own_password',
            target_system_user=None)
    elif re.match(r'^SET\s+DEFAULT\s+ROLE\b', upper):
        add('default_roles', target_system_user=None)
        uncertain = True  # Own-account defaults and another account have different checks.
    elif re.match(r'^SET\b', upper) and not re.match(r'^SET\s+(?:ROLE|STATEMENT)\b', upper):
        # Limit this projection to one assignment. A comma in a function is
        # harmless but conservatively leaves the whole SET unverified too.
        rest = upper[3:]
        match = dbops._SET_ITEM.match(rest)
        if match and ',' not in rest and not match.group(4):
            variable = dbops._name(re.split(r'\s*\.\s*', match.group(5))[-1], idents).lower()
            scope = match.group(1) or match.group(2) or 'SESSION'
            from . import variables
            maria = model and model.get('engine') == 'mariadb'
            if scope in ('GLOBAL', 'PERSIST', 'PERSIST_ONLY'):
                add('set_' + scope.lower(), variable=variable)
                if maria and scope == 'GLOBAL' and variable in variables.MARIADB_GLOBAL_PRIVILEGE \
                        and variable not in ('read_only', 'max_connections', 'sql_slave_skip_counter'):
                    uncertain = True
            elif maria and variable in variables.MARIADB_RESTRICTED_SESSION:
                add('set_session_restricted', variable=variable)
                uncertain = variable == 'create_tmp_table_binlog_formats'
            elif not maria and variable in variables.MYSQL_RESTRICTED_SESSION:
                add('set_session_restricted', variable=variable)
                uncertain = variable in variables.MYSQL_RESTRICTED_NO_SUPER | variables.MYSQL_RESTRICTED_APPLIER
    elif re.match(r'^RESET\s+PERSIST\b', upper):
        add('reset_persist')
        uncertain = True  # The persisted entry's read-only flag is not in SHOW GRANTS.
    elif statement.get('kind') == 'read' and re.match(r'^SELECT\b', upper):
        add('explain')  # The existing SELECT privilege action; absence is never a denial.

    result = {'action_keys': list(dict.fromkeys(actions)), 'state': 'unknown', 'missing': [],
              'reason': 'This statement\'s privileges are not verified; the server decides.'}
    if not model:
        result['reason'] = 'No cached privileges report for this connection; the server decides.'
        return result
    if uncertain or not actions or not model.get('checked', True) or not model.get('fresh_session', True) \
            or model.get('unparsed'):
        return result
    answers = [can(model, key, **target) for key, target in zip(actions, targets)]
    result['state'] = ('denied' if any(a['state'] == 'denied' for a in answers) else
                       'allowed' if all(a['state'] == 'allowed' for a in answers) else 'unknown')
    result['missing'] = list(dict.fromkeys(p for a in answers for p in a['missing']))
    result['reason'] = ' '.join(dict.fromkeys(a['reason'] for a in answers))
    return result


_VARIABLES = ('partial_revokes', 'activate_all_roles_on_login', 'activate_mandatory_roles', 'mandatory_roles',
              'read_only', 'super_read_only', 'server_uuid', 'server_id', 'hostname', 'port')


def read_privileges(conn):
    """The privileges-1.0 report of the connected account, from one tagged
    client call in a fresh session: USER(), CURRENT_USER(), CURRENT_ROLE(),
    VERSION(), plain SHOW GRANTS and the role and read_only variables. On
    MySQL a second call reads information_schema.APPLICABLE_ROLES (8.0.19+)
    for the default and mandatory flags. Raises ConnectorError when the
    server cannot be reached; never reads mysql.* or SHOW CREATE USER.
    """
    reader = Reader(conn, TAG)
    warnings = []
    sql = ("SELECT 'myflames_who', USER(), CURRENT_USER(), CURRENT_ROLE(), VERSION(); " + TAG + "SHOW GRANTS; " +
           TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)" % ', '.join("'%s'" % v for v in _VARIABLES))
    try:
        rows = reader.rows(sql)
    except ConnectorError as exc:
        if dbops.error_code(str(exc)) in (1820, 1862):
            # An expired account may connect (sandbox mode) but run only SET
            # PASSWORD (MariaDB) or ALTER USER USER() (MySQL).
            raise ConnectorError('ERROR %d: The password of this account has expired, so it can only set a new '
                                 'one: myflames accounts password ACCOUNT.' % dbops.error_code(str(exc))) from None
        raise
    who = next((row for row in rows if len(row) == 5 and row[0] == 'myflames_who'), None)
    if who is None:
        raise ConnectorError('The server did not return the session identity.')
    _marker, user, account, role_text, version = who
    engine = detect_flavor(version)
    grants, variables = [], {}
    for row in rows:
        if len(row) == 1 and row[0]:
            grants.append(strip_credentials(row[0]))
        elif len(row) == 2 and row[0]:
            variables[row[0].lower()] = row[1]
    if engine == 'mysql':
        active = [] if role_text in (None, 'NONE', '') else [account_tokens(p) for p in split_tokens(tokens(role_text))]
    else:
        active = [{'name': role_text, 'host': '', 'role': True}] if role_text else []
    partial = engine == 'mysql' and str(variables.get('partial_revokes', '')).upper() in ('ON', '1')
    model = parse_grants(grants, engine, version, partial)
    if not grants:
        model['checked'] = False
        warnings.append('SHOW GRANTS returned no lines.')
    for role in model['roles']:
        role['active'] = any(role['name'] == r['name'] and (engine == 'mariadb' or role['host'] == r['host'])
                             for r in active)
        role['mandatory'] = False
    if engine == 'mysql' and version_tuple(version) >= (8, 0, 19):
        try:
            flags = reader.rows('SELECT ROLE_NAME, ROLE_HOST, IS_DEFAULT, IS_MANDATORY '
                                'FROM information_schema.APPLICABLE_ROLES')
            for role in model['roles']:
                for name, host, default, mandatory in (r[:4] for r in flags if len(r) >= 4):
                    if role['name'] == name and role['host'] == host:
                        role.update(default=default == 'YES', mandatory=mandatory == 'YES')
        except ConnectorError:
            warnings.append('The default and mandatory role flags could not be read.')
    if model['unparsed']:
        warnings.append('Some grant lines were not understood, so every answer is unverified.')
    report = dict(model)
    report.update({'schema_version': SCHEMA_VERSION, 'account': account, 'user': user, 'current_roles': active,
                   'engine': engine, 'version': version, 'read_at': now(), 'variables': variables,
                   'session_scope': 'fresh_client_session',
                   'session_note': ('Describes a new session of this account. A Query editor tab after SET ROLE, '
                                    'or a stored routine running as its definer, can differ.'),
                   'warnings': warnings})
    report['actions'] = {key: can(report, key) for key in ACTIONS}
    return report


read = read_privileges
