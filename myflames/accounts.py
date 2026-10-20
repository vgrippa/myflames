"""Users and roles: credential-free reads, reviewable plans and audited writes.

Public functions: :func:`list_accounts` (``accounts-1.0``),
:func:`read_account` (``account-1.0`` with a ``fingerprint``),
:func:`plan_change` and the ``plan_*`` wrappers (``account-plan-1.0``),
:func:`apply` (``account-change-1.0``), :func:`grant_diff` and
:func:`fingerprint`.

A plan is built from a fresh read and never sends a write. Its executable
statements and secrets live only in the in-memory :class:`Plan`; the public
projection shows them redacted. :func:`apply` takes an intact Plan built in
the same request, runs the write policy (``dbops.decide`` and ``gate``),
re-reads the actor, the server and the account, refuses with "nothing was
sent" when the account no longer matches ``expect``, sends each statement on
the client's standard input (never in argv), stops at the first error, and
returns the observed before and after grants. Every attempt is audited
(``dbops.AuditTrail``) without a password.

Account DDL is not transactional across statements, even on MySQL. On
MariaDB a new user is created locked without a password, gets its password
from SET PASSWORD (which the binary log records as a hash), then is
unlocked; with an active password validation plugin the first step uses a
temporary random password instead, because the plugin refuses an empty one
(verified live on MariaDB 10.11 and 11.4 with simple_password_check). A
failed password step leaves the new account locked.

Sources: .claude/skills/accounts/references/account-statements.md.
"""
import copy
from collections import deque
import hashlib
import json
import re
import secrets as secret_generator
import string
import time
import uuid

from . import dbops, privileges
from .connector import ConnectorError, MySQLConnection
from .perf_common import Reader, literal
from .server_info import detect_flavor

TAG = '/*myflames:accounts*/ '
PLAN_SCHEMA = 'account-plan-1.0'
CHANGE_SCHEMA = 'account-change-1.0'
OPERATIONS = ('create', 'drop', 'rename', 'lock', 'unlock', 'expire', 'password', 'grant', 'revoke', 'roles',
              'options')
SYSTEM_ACCOUNTS = frozenset(('mysql.sys', 'mysql.session', 'mysql.infoschema', 'mariadb.sys'))
DANGEROUS = frozenset(('ALL', 'ALL PRIVILEGES', 'GRANT OPTION', 'SUPER', 'CREATE USER', 'FILE',
                       'PROCESS', 'SHUTDOWN', 'RELOAD', 'SYSTEM_USER', 'CONNECTION_ADMIN',
                       'CONNECTION ADMIN', 'ROLE_ADMIN', 'SET_ANY_DEFINER',
                       'ALLOW_NONEXISTENT_DEFINER', 'SET USER', 'REPLICATION SLAVE', 'PROXY'))
COLUMNS = privileges.COLUMN_PRIVILEGES
TABLES = privileges.TABLE_PRIVILEGES
DATABASES = privileges.DATABASE_PRIVILEGES
ROUTINES = privileges.ROUTINE_PRIVILEGES
LIMITS = ('MAX_QUERIES_PER_HOUR', 'MAX_UPDATES_PER_HOUR', 'MAX_CONNECTIONS_PER_HOUR', 'MAX_USER_CONNECTIONS')
#: The warning every MariaDB password change carries (SKILL.md Decision 4).
LOG_WARNING = 'This protects the binary log only. The general and slow logs can contain the typed password.'
#: Server errors whose text never quotes the statement, so it is safe to show
#: (after redaction). Others are reported by number only: a syntax error can
#: quote the SQL around a password.
_SAFE_ERROR_TEXT = frozenset((1044, 1045, 1095, 1133, 1141, 1142, 1147, 1227, 1290, 1396, 1403, 1410, 1698,
                              1699, 1819, 1959, 1960, 1961, 1962, 3530, 3532, 3628, 3638, 3891, 3892, 3893,
                              4005, 4006))
_DENIED = (1044, 1045, 1095, 1142, 1227, 1290, 1698, 1792, 3630)
_INTERRUPTED = (1205, 1213, 1317)
MAX_ROLE_READS = 256
ROLE_READ_SECONDS = 30


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def fingerprint(account):
    """The ``expect`` token of an account: its grants (credential-free and
    order-independent), roles, default roles and non-secret attributes. A
    password change does not alter it."""
    safe = {'name': account['name'], 'host': account['host'], 'role': account.get('role', False),
            'exists': account.get('exists', True),
            'grants': sorted(set(dbops.strip_credentials(g) for g in account.get('grants', []))),
            'attributes': account.get('attributes', {}), 'roles': sorted(account.get('roles', []), key=_json),
            'default_roles': sorted(account.get('default_roles', []), key=_json),
            'attributes_checked': account.get('attributes_checked', True),
            'defaults_checked': account.get('defaults_checked', True),
            'role_grants': sorted(account.get('role_grants', []))}
    return hashlib.sha256(_json(safe).encode('utf-8')).hexdigest()


def _connection(conn):
    return {k: getattr(conn, k, None) for k in ('host', 'port', 'user', 'ssl_mode', 'ssl_ca', 'ssl_cert', 'ssl_key',
                                                 'ssl_cipher')}


def _same_sql_connection(conn, reviewed):
    # Browser forms represent unset TLS fields as empty strings, whereas the
    # connector represents them as None. Compare their actual option values;
    # retain exact nonempty TLS and endpoint values and normalize only port.
    actual = _connection(conn)
    expected = {key: reviewed.get(key) for key in actual}
    try:
        expected['port'] = int(expected.get('port', 3306) or 3306)
    except (TypeError, ValueError):
        return False
    return all((value or None) == (expected[key] or None) for key, value in actual.items())


def _name(name, host, engine, role=False):
    """The quoted account (or MariaDB role) for a statement; ValueError for a
    name the server would reject or read differently."""
    if not isinstance(name, str) or len(name) > (128 if engine == 'mariadb' else 32):
        raise ValueError('User name is too long or is not text.')
    if role and (not name or (engine == 'mariadb' and name.upper() == 'NONE') or name != name.strip()):
        raise ValueError('Invalid role name.')
    if engine == 'mysql' and name != name.strip():
        raise ValueError('MySQL trims spaces around user names; remove them.')
    if role and engine == 'mariadb' and name.upper() == 'PUBLIC':
        return 'PUBLIC'
    if role and engine == 'mariadb':
        return privileges.quote_identifier(name)
    if not role and engine == 'mariadb' and host == '':
        raise ValueError('MariaDB treats an empty host as %; specify % explicitly.')
    if not isinstance(host, str) or len(host.encode('utf-8')) > 255 or '@' in host or host != host.strip():
        raise ValueError('Invalid account host.')
    return privileges.quote_account(name, host, role, engine)


def parse_account(text):
    """``(name, host)`` from ``user@host``, ``'user'@'host'`` or
    ``\\`user\\`@\\`host\\```; a name without ``@`` gets host ``%``."""
    text = (text or '').strip()
    if not text:
        raise ValueError('Name the account as user@host.')
    parts = privileges.tokens(text)
    if len(parts) == 3 and parts[1] == '@':
        return privileges.unquote(parts[0]), privileges.unquote(parts[2])
    if len(parts) == 1 and parts[0][:1] in '`\'"':
        return privileges.unquote(parts[0]), '%'
    if '@' in text:
        name, host = text.rsplit('@', 1)
        return privileges.unquote(name), privileges.unquote(host)
    return text, '%'


def _server(conn):
    version = conn.server_version()
    engine = detect_flavor(version)
    if privileges.version_tuple(version) < ((10, 11, 0) if engine == 'mariadb' else (8, 0, 11)):
        raise ValueError('Users and roles needs MySQL 8.0.11 or later, or MariaDB 10.11 or later.')
    return engine, version


def _rows_text(rows):
    return ' '.join(cell for row in rows for cell in row if cell)


def _attributes(create):
    """Non-secret attributes from SHOW CREATE USER text (every row joined);
    the text itself is never returned."""
    out = {'limits': {}, 'password_options': {}, 'plugins': [], 'locked': False, 'expired': False}
    parts = privileges.tokens(create)
    upper = [p.upper() for p in parts]
    for i, word in enumerate(upper):
        nxt = upper[i + 1] if i + 1 < len(upper) else ''
        if word in ('WITH', 'VIA') and i > 0 and upper[i - 1] == 'IDENTIFIED' and nxt:
            out['plugins'].append(privileges.unquote(parts[i + 1]))
        if word == 'BY' and i > 0 and upper[i - 1] == 'IDENTIFIED' and nxt == 'PASSWORD':
            out['plugins'].append('mysql_native_password')  # MariaDB's default method.
        if word == 'OR' and i + 1 < len(parts) and re.match(r'^\w+$', privileges.unquote(parts[i + 1])):
            out['plugins'].append(privileges.unquote(parts[i + 1]))
        if word == 'ACCOUNT' and nxt in ('LOCK', 'UNLOCK'):
            out['locked'] = nxt == 'LOCK'
        if word == 'PASSWORD' and nxt == 'EXPIRE':
            option = upper[i + 2] if i + 2 < len(upper) else ''
            if option in ('NEVER', 'DEFAULT'):
                out['password_options']['expire'] = option
            elif option == 'INTERVAL' and i + 3 < len(parts) and parts[i + 3].isdigit():
                out['password_options']['expire_days'] = int(parts[i + 3])
            else:
                # A bare PASSWORD EXPIRE: the password is expired now.
                out['expired'] = True
        if word in LIMITS + ('MAX_STATEMENT_TIME',) and i + 1 < len(parts) and parts[i + 1].isdigit():
            # The tokenizer splits 2.500000 at the dot.
            fraction = parts[i + 3] if i + 3 < len(parts) and parts[i + 2] == '.' and parts[i + 3].isdigit() else ''
            out['limits'][word] = parts[i + 1] + ('.' + fraction if fraction else '')
        for words, key in ((('PASSWORD', 'HISTORY'), 'history'), (('PASSWORD', 'REUSE', 'INTERVAL'), 'reuse_interval'),
                           (('PASSWORD', 'REQUIRE', 'CURRENT'), 'require_current'),
                           (('FAILED_LOGIN_ATTEMPTS',), 'failed_login_attempts'), (('PASSWORD_LOCK_TIME',), 'lock_time')):
            if tuple(upper[i:i + len(words)]) == words:
                following = upper[i + len(words)] if i + len(words) < len(upper) else ''
                if following in ('DEFAULT', 'OPTIONAL', 'UNBOUNDED') or following.isdigit():
                    out['password_options'][key] = following
                elif key == 'require_current':
                    out['password_options'][key] = 'YES'
    out['plugins'] = list(dict.fromkeys(out['plugins']))
    return out


def _default_roles_clause(create, engine):
    """MySQL SHOW CREATE USER prints ``DEFAULT ROLE `r`@`h`,...``."""
    parts = privileges.tokens(create)
    upper = [p.upper() for p in parts]
    for i in range(len(parts) - 1):
        if upper[i] == 'DEFAULT' and upper[i + 1] == 'ROLE':
            end = i + 2
            while end < len(parts) and (parts[end] in (',', '@') or parts[end][:1] in '`\'"'):
                end += 1
            return [privileges.account_tokens(p) for p in privileges.split_tokens(parts[i + 2:end])]
    return []


def read_account(conn, name, host='%', role=False, engine=None, version=None, include_catalog=True):
    """``account-1.0``: the account's own grants (credential-free), its
    roles and default roles, non-secret attributes, the grants it inherits
    through roles, dangerous-grant flags and the ``fingerprint``.

    Reads SHOW GRANTS FOR, SHOW CREATE USER (MySQL 8.0.17+ with
    print_identified_with_as_hex so the hash cannot break the parse; parsed
    in Python, never returned) and SHOW GRANTS FOR every role reached. Another
    account needs SELECT on mysql. A missing account (1141) gives ``exists``
    false; other errors raise, by number only.
    """
    if engine is None or version is None:
        engine, version = _server(conn)
    account = _name(name, host, engine, role)
    reader = Reader(conn, TAG)
    out = {'schema_version': 'account-1.0', 'name': name, 'host': '' if role and engine == 'mariadb' else host,
           'role': role, 'engine': engine, 'version': version, 'exists': True, 'grants': [],
           'attributes': {}, 'roles': [], 'default_roles': [], 'warnings': [],
           'system': name in SYSTEM_ACCOUNTS}
    try:
        lines = reader.rows('SHOW GRANTS FOR ' + account)
    except ConnectorError as exc:
        code = dbops.error_code(str(exc))
        if code in (1141, 1133, 1396):
            out['exists'] = False
            out['fingerprint'] = fingerprint(out)
            return out
        raise ConnectorError('Could not read the account grants (error %s).' % (code or 'unavailable')) from None
    out['grants'] = [dbops.strip_credentials(row[0]) for row in lines if row and row[0]]
    partial = False
    if engine == 'mysql':
        partial = str(reader.pairs("SHOW GLOBAL VARIABLES WHERE Variable_name = 'partial_revokes'")
                      .get('partial_revokes', '')).upper() in ('ON', '1')
    out['partial_revokes'] = partial
    direct_lines, inherited_lines = [], []
    for line in out['grants']:
        # MariaDB FOR a named role returns the descendant role tree too.
        # Only this grantee's lines may feed direct-revoke and guard logic
        # (mariadb-server/sql/sql_acl.cc:9774). Preserve unparsed lines so
        # uncertain provenance never becomes a proven absence.
        source = None
        if engine == 'mariadb' and role:
            try:
                source = privileges.grant_source(line)
            except ValueError:
                pass
        if source and (source['name'], source['host']) != (name, out['host']):
            inherited_lines.append(line)
        else:
            direct_lines.append(line)
    out['model'] = privileges.parse_grants(direct_lines, engine, version, partial)
    out['roles'] = [{k: r[k] for k in ('name', 'host', 'admin_option')} for r in out['model']['roles']
                    if r['grantee']['name'] == name and (role and engine == 'mariadb' or r['grantee']['host'] == host)]
    create = ''
    if not (role and engine == 'mariadb'):
        try:
            sql = 'SHOW CREATE USER ' + account
            if engine == 'mysql' and privileges.version_tuple(version) >= (8, 0, 17):
                sql = 'SET SESSION print_identified_with_as_hex = ON; ' + TAG + sql
            create = _rows_text(reader.rows(sql))
            if not create:
                raise ValueError('No SHOW CREATE USER result.')
            out['attributes'] = _attributes(create)
            out['attributes_checked'] = True
        except (ConnectorError, ValueError):
            out['warnings'].append('Account attributes are unavailable; SELECT on the mysql database may be needed.')
            out['attributes_checked'] = False
    else:
        out['attributes_checked'] = True
    if engine == 'mysql':
        out['default_roles'] = _default_roles_clause(create, engine) if create else []
        out['defaults_checked'] = bool(create) or role
    else:
        defaults = [r for r in out['model']['roles'] if r['default'] and r['grantee']['name'] == name]
        out['default_roles'] = [{'name': r['name'], 'host': ''} for r in defaults]
        out['defaults_checked'] = True
    for r in out['default_roles']:
        r.pop('role', None)
    create = ''  # Never kept: it held the hash.
    out['role_grants'] = inherited_lines
    # Cache each reached role's parsed grants for this expansion only. Mark
    # it on enqueue so diamonds and cycles cannot grow the work queue.
    queue, role_cache = deque(), {}
    root_key = (name, '' if role and engine == 'mariadb' else host)
    complete = True
    deadline = time.monotonic() + ROLE_READ_SECONDS

    def enqueue(roles):
        nonlocal complete
        for r in roles:
            key = (r['name'], r['host'])
            if key == root_key or key in role_cache:
                continue
            if len(role_cache) >= MAX_ROLE_READS:
                if complete:
                    out['warnings'].append('The role graph has more than %d roles; inherited privileges are '
                                           'incomplete.' % MAX_ROLE_READS)
                complete = False
                continue
            role_cache[key] = None
            queue.append(key)

    enqueue(out['model']['roles'])
    while queue:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            complete = False
            out['warnings'].append('The role read time budget expired; inherited privileges are incomplete.')
            break
        key = queue.popleft()
        try:
            rows = reader.rows('SHOW GRANTS FOR ' + _name(key[0], key[1] or '%', engine, True),
                               timeout=min(remaining, getattr(conn, 'query_timeout', 30) or 30))
        except ConnectorError:
            complete = False
            out['warnings'].append('Could not read the privileges of role %s; inherited privileges are incomplete.'
                                   % key[0])
            continue
        lines = [dbops.strip_credentials(row[0]) for row in rows if row and row[0]]
        role_cache[key] = privileges.parse_grants(lines, engine, version, partial)
        out['role_grants'].extend(lines)
        enqueue(role_cache[key]['roles'])
        if time.monotonic() >= deadline:
            complete = False
            out['warnings'].append('The role read time budget expired; inherited privileges are incomplete.')
            break
    out['role_grants'] = list(dict.fromkeys(out['role_grants']))
    out['role_grants_complete'] = complete
    out['effective_model'] = privileges.parse_grants(out['grants'] + out['role_grants'], engine, version, partial,
                                                    checked=complete)
    out['dangerous'] = dangerous_grants(out['effective_model'])
    out['protected'] = _protected(out)
    out['fingerprint'] = fingerprint(out)
    if include_catalog:
        _grant_editor(conn, out)
    return out


def _catalog(conn, engine, version):
    try:
        rows = Reader(conn, TAG).rows('SHOW PRIVILEGES')
    except ConnectorError:
        rows = None
    return privileges.privilege_catalog(engine, version, rows)


def _grant_scopes(account, databases=()):
    """Observed grant scopes plus visible literal databases, with provenance.

    Direct cells are never inferred from an effective aggregate. Role and
    higher-level grants are display-only; REVOKE must name the original
    grant. Keep scopes even when they are absent from the visible catalog.
    """
    engine, version = account['engine'], account['version']
    scopes = {}

    def scope(level, **target):
        key = _json(dict(level=level, **target))
        if key not in scopes:
            if level == 'global':
                label = '*.*'
            elif level == 'proxy':
                label = _name(target['proxy']['name'], target['proxy']['host'], engine)
            else:
                label = privileges.quote_identifier(target['database'])
                if level == 'database':
                    label += '.*'
                elif level in ('table', 'column'):
                    label += '.' + privileges.quote_identifier(target['table'])
                    if level == 'column':
                        label += ' (' + ', '.join(privileges.quote_identifier(c) for c in target['columns']) + ')'
                else:
                    label = target['kind'] + ' ' + label + '.' + privileges.quote_identifier(target['routine'])
            scopes[key] = dict(target, id=hashlib.sha256(key.encode('utf-8')).hexdigest()[:24],
                               label=label, level=level, cells=[],
                               system=target.get('database') in ('mysql', 'sys', 'information_schema', 'performance_schema'))
        return scopes[key]

    def cell(s, name, direct, grantable, via=None, restricted=False):
        old = next((c for c in s['cells'] if c['privilege'] == name), None)
        if old is None:
            old = dict(privilege=name, direct=False, grantable=False)
            s['cells'].append(old)
        if direct:
            old['direct'] = True
            old['grantable'] = old['grantable'] or bool(grantable)
            old.pop('via', None)
        elif not old['direct'] and via:
            old['via'] = via
        if restricted:
            old['restricted'] = True

    def expanded(names, level):
        result = set(names) - privileges.ALL_NAMES - {'USAGE'}
        if set(names) & privileges.ALL_NAMES:
            result.update(privileges.static_privileges(engine, version, level))
        return result

    global_scope = scope('global')
    for database in databases:
        if database not in ('information_schema', 'performance_schema'):
            scope('database', database=database, pattern=False)
    # MariaDB SHOW GRANTS FOR a role includes its inherited role tree
    # (sql_acl.cc:9774). Group even the first response by its grantee;
    # otherwise inherited role privileges would appear editable as direct.
    grouped, _unknown = _role_models(dict(account, role_grants=account.get('grants', []) +
                                        account.get('role_grants', [])))
    own_key = (account['name'], account['host'])
    own = grouped.pop(own_key, privileges.parse_grants([], engine, version,
                                                       account['model']['partial_revokes']))
    models = [(own, True, None)] + [
        (model, False, 'role ' + _name(name, host, engine, True)) for (name, host), model in grouped.items()]
    for model, direct, via in models:
        for name in expanded(model['global'] + model['dynamic'], 'global'):
            grantable = (model['static_grant_option'] if name not in model['dynamic'] else
                         name in model['global_grantable'])
            cell(global_scope, name, direct, grantable, via)
        if model['static_grant_option']:
            cell(global_scope, 'GRANT OPTION', direct, True, via)
        for bucket, level in (('schemas', 'database'), ('tables', 'table'), ('columns', 'column'),
                              ('routines', 'routine'), ('proxies', 'proxy'), ('restrictions', 'database')):
            for entry in model[bucket]:
                if level == 'proxy':
                    target = {'proxy': {k: entry['account'][k] for k in ('name', 'host')}}
                else:
                    target = {'database': entry['pattern'] if level == 'database' and entry['wildcard']
                              else entry['database']}
                    if level == 'database':
                        target['pattern'] = bool(entry['wildcard'])
                    elif level in ('table', 'column'):
                        target['table'] = entry['table']
                        if level == 'column':
                            target['columns'] = sorted(set(entry['columns']))
                    else:
                        target.update(routine=entry['name'], kind=entry['kind'])
                s = scope(level, **target)
                restricted = bucket == 'restrictions'
                for name in expanded(entry['privs'], level):
                    cell(s, name, direct and not restricted, entry['grantable'], via, restricted)
                if entry['grantable'] and level not in ('column', 'proxy'):
                    cell(s, 'GRANT OPTION', direct, True, via)
    # Show known inheritance on each concrete scope. Do not interpret a
    # wildcard pattern as a literal database to infer higher-level access.
    for s in scopes.values():
        if s['level'] in ('global', 'proxy') or s.get('pattern'):
            continue
        target = {k: s[k] for k in ('database', 'table', 'columns', 'routine', 'kind') if k in s}
        for item in account['privilege_catalog'][s['level']]:
            name = item['name']
            existing = next((c for c in s['cells'] if c['privilege'] == name), None)
            if existing and (existing['direct'] or existing.get('via')):
                continue
            for model, direct, via in models:
                if privileges.has(model, name, s['level'], **target) is True:
                    cell(s, name, False, False, via or ('global' if privileges.has(model, name) is True else 'higher scope'))
                    break
        s['cells'].sort(key=lambda c: c['privilege'])
    return sorted(scopes.values(), key=lambda s: (privileges.GRANT_LEVELS.index(s['level']), s['label'], s['id']))


def _grant_editor(conn, account):
    catalog, meta = _catalog(conn, account['engine'], account['version'])
    for entries in catalog.values():
        for entry in entries:
            entry['dangerous'] = entry['name'] in DANGEROUS
    account['privilege_catalog'], account['privilege_catalog_meta'] = catalog, meta
    if meta['source'] == 'static':
        account['warnings'].append('The privilege list is unavailable; using built-in names for this server version. '
                                   'Plugin privileges may be missing.')
    try:
        databases = [row[0] for row in Reader(conn, TAG).rows('SHOW DATABASES') if row]
    except ConnectorError:
        databases = []
        account['warnings'].append('The database list is unavailable; enter an exact database name to add a scope.')
    account['grant_scopes'] = _grant_scopes(account, databases)


def scope_objects(conn, database):
    """At most 500 visible table/routine names, never definitions or bodies."""
    from .perf_common import check_schema
    if check_schema(database) is None:
        raise ValueError('Name the database.')
    if database.lower() in ('information_schema', 'performance_schema'):
        raise ValueError('Choose a database that accepts account grants.')
    out = {'database': database, 'tables': [], 'procedures': [], 'functions': [],
           'truncated': False, 'warnings': []}
    reader, objects = Reader(conn, TAG), []
    for sql, routine in (
        ('SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = ' + literal(database) +
         ' ORDER BY TABLE_NAME LIMIT 501', False),
        ('SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = ' +
         literal(database) + " AND ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION') ORDER BY ROUTINE_TYPE, ROUTINE_NAME LIMIT 501", True),
    ):
        try:
            rows = reader.rows(sql)
        except ConnectorError:
            out['warnings'].append('Could not read %s; enter an exact object name.' % ('routines' if routine else 'tables'))
            continue
        for row in rows[:501]:
            if row and (not routine or len(row) > 1 and row[1].upper() in ('PROCEDURE', 'FUNCTION')):
                objects.append(('tables' if not routine else 'procedures' if row[1].upper() == 'PROCEDURE'
                                else 'functions', row[0]))
    out['truncated'] = len(objects) > 500
    for bucket, name in objects[:500]:
        out[bucket].append({'name': name})
    if out['truncated']:
        out['warnings'].append('Only the first 500 objects are listed; enter an exact name for another object.')
    # Metadata visibility depends on the connected account. An empty list
    # cannot prove there are no objects, and must not disable manual entry.
    out['warnings'].append('Only objects visible to the connected account are listed.')
    return out



def _direct_system_user(account):
    """MySQL account statements check SYSTEM_USER granted directly to the
    target; roles do not count (sql_security_ctx.cc:838)."""
    return account.get('engine') == 'mysql' and 'SYSTEM_USER' in account.get('model', {}).get('dynamic', [])


def _protected(account):
    """Why an account needs its name typed before Drop, Rename, Lock or an
    admin revoke (SKILL.md rule 6), or ''."""
    model = account.get('effective_model') or account.get('model') or {}
    flags = set(model.get('global', []) + model.get('dynamic', []))
    if account['name'] == 'root' and not account.get('role'):
        return 'root account'
    if 'SYSTEM_USER' in flags:
        return 'holds SYSTEM_USER'
    for p in ('ALL', 'ALL PRIVILEGES', 'SUPER', 'CREATE USER'):
        if p in flags:
            return 'holds %s on *.*' % ('ALL' if p.startswith('ALL') else p)
    if model.get('global_grantable'):
        return 'holds GRANT OPTION on *.*'
    return ''


def list_accounts(conn):
    """``accounts-1.0``: users and roles, role grants and default roles,
    without selecting a password hash (MySQL compares
    ``authentication_string = ''`` on the server; MariaDB reads single keys
    of ``global_priv`` with JSON_VALUE, never ``Priv`` itself). Without
    SELECT on mysql: only the connected account, with a warning."""
    engine, version = _server(conn)
    reader = Reader(conn, TAG)
    out = {'schema_version': 'accounts-1.0', 'engine': engine, 'version': version,
           'accounts': [], 'role_grants': [], 'default_roles': [], 'warnings': [], 'complete': True}
    try:
        if engine == 'mysql':
            sql = ("SELECT User, Host, plugin, account_locked, password_expired, password_last_changed, "
                   "password_lifetime, (authentication_string = '' AND plugin IN "
                   "('mysql_native_password','caching_sha2_password','sha256_password')), "
                   "(authentication_string = '') FROM mysql.user ORDER BY User, Host")
            rows = reader.rows(sql)
            edges = reader.rows('SELECT FROM_USER, FROM_HOST, TO_USER, TO_HOST, WITH_ADMIN_OPTION FROM mysql.role_edges '
                                'ORDER BY FROM_USER, FROM_HOST, TO_USER, TO_HOST')
            defaults = reader.rows('SELECT USER, HOST, DEFAULT_ROLE_USER, DEFAULT_ROLE_HOST FROM mysql.default_roles '
                                   'ORDER BY USER, HOST, DEFAULT_ROLE_USER, DEFAULT_ROLE_HOST')
            granted = {(r[0], r[1]) for r in edges}
            for r in rows:
                locked, expired, empty = r[3] == 'Y', r[4] == 'Y', r[8] == '1'
                out['accounts'].append({
                    'name': r[0], 'host': r[1], 'plugin': r[2], 'locked': locked, 'expired': expired,
                    'password_last_changed': r[5], 'password_lifetime': r[6], 'no_password': r[7] == '1',
                    'role': (r[0], r[1]) in granted or (locked and expired and empty),
                    'system': r[0] in SYSTEM_ACCOUNTS})
            out['role_grants'] = [{'role': {'name': r[0], 'host': r[1]}, 'grantee': {'name': r[2], 'host': r[3]},
                                   'admin_option': r[4] == 'Y'} for r in edges]
            out['default_roles'] = [{'account': {'name': r[0], 'host': r[1]}, 'role': {'name': r[2], 'host': r[3]}}
                                    for r in defaults]
        else:
            sql = ("SELECT User, Host, JSON_VALUE(Priv, '$.plugin'), JSON_VALUE(Priv, '$.account_locked'), "
                   "JSON_VALUE(Priv, '$.password_last_changed'), JSON_VALUE(Priv, '$.password_lifetime'), "
                   "JSON_VALUE(Priv, '$.is_role'), JSON_VALUE(Priv, '$.default_role'), "
                   "(JSON_VALUE(Priv, '$.authentication_string') = '' AND JSON_VALUE(Priv, '$.plugin') IN "
                   "('mysql_native_password','mysql_old_password')) FROM mysql.global_priv ORDER BY User, Host")
            rows = reader.rows(sql)
            mapping = reader.rows('SELECT Host, User, Role, Admin_option FROM mysql.roles_mapping '
                                  'ORDER BY Host, User, Role')
            for r in rows:
                is_role = r[6] in ('1', 'true')
                out['accounts'].append({
                    'name': r[0], 'host': r[1], 'plugin': r[2], 'locked': r[3] in ('1', 'true'),
                    'expired': r[4] == '0' and not is_role, 'password_last_changed': r[4],
                    'password_lifetime': r[5], 'role': is_role, 'public': is_role and r[0] == 'PUBLIC' and r[1] == '',
                    'no_password': r[8] == '1', 'system': r[0] in SYSTEM_ACCOUNTS})
                if r[7]:
                    out['default_roles'].append({'account': {'name': r[0], 'host': r[1]},
                                                 'role': {'name': r[7], 'host': ''}})
            out['role_grants'] = [{'grantee': {'name': r[1], 'host': r[0]}, 'role': {'name': r[2], 'host': ''},
                                   'admin_option': r[3] == 'Y'} for r in mapping]
    except ConnectorError as exc:
        if dbops.error_code(str(exc)) not in (1044, 1045, 1142, 1227):
            raise ConnectorError('Could not list accounts (error %s).' % (dbops.error_code(str(exc)) or 'unavailable')) \
                from None
        own = reader.rows('SELECT CURRENT_USER()')[0][0]
        name, host = own.rsplit('@', 1)
        out.update(accounts=[{'name': name, 'host': host, 'connected': True}], role_grants=[], default_roles=[],
                   complete=False)
        out['warnings'].append('Only the connected account is listed: listing users and roles needs SELECT on the '
                               'mysql database.')
    return out


def dangerous_grants(model):
    flags = set(model['global'] + model['dynamic']) & DANGEROUS
    if model['global_grantable'] or any(e['grantable'] for key in ('schemas', 'tables', 'columns', 'routines', 'proxies')
                                        for e in model[key]):
        flags.add('GRANT OPTION')
    for e in model['schemas']:
        if privileges._matches(e['pattern'], 'mysql', model['partial_revokes']):
            flags.update('mysql: ' + p for p in e['privs'] if p in ('SELECT', 'INSERT', 'UPDATE', 'DELETE'))
    if model['proxies']:
        flags.add('PROXY')
    return sorted(flags)


def grant_diff(before, after):
    """Observed line and per-level differences, credentials stripped first."""
    def lines(a):
        return set(dbops.strip_credentials(g) for g in a.get('grants', []))
    old, new = lines(before), lines(after)
    by_level = {}
    for key in ('global', 'dynamic', 'schemas', 'tables', 'columns', 'routines', 'proxies', 'restrictions', 'roles'):
        def entries(a):
            model = a.get('model') or privileges.parse_grants(a.get('grants', []), a.get('engine', 'mysql'),
                                                              a.get('version', ''))
            return {_json(e) for e in model[key]}
        b, a = entries(before), entries(after)
        by_level[key] = {'added': [json.loads(x) for x in sorted(a - b)], 'removed': [json.loads(x) for x in sorted(b - a)]}
    return {'added': sorted(new - old), 'removed': sorted(old - new), 'levels': by_level,
            'attributes_before': before.get('attributes', {}), 'attributes_after': after.get('attributes', {}),
            'defaults_before': before.get('default_roles', []), 'defaults_after': after.get('default_roles', []),
            'exists_before': before.get('exists', True), 'exists_after': after.get('exists', True)}


class Plan(dict):
    """The public ``account-plan-1.0`` projection (safe to serialize); the
    executable statements and secrets stay in private attributes. Apply
    accepts only an intact Plan and refuses one whose projection changed."""
    def __init__(self, public, statements, passwords):
        super(Plan, self).__init__(public)
        self._statements = tuple(statements)
        self._passwords = tuple(p for p in passwords if p)
        self._seal = _json(public)
        self._options = {}
        self._consumed = False
        self._generated = None
        self._random_result = False

    def __reduce__(self):
        raise TypeError('Account plans containing credentials cannot be pickled.')


def _password(value):
    """A plain quoted string literal (the grammar takes no expression)."""
    if not isinstance(value, str) or not value or any(c in value for c in '\0\r\n'):
        raise ValueError('Passwords must be non-empty text without NUL or line breaks.')
    if len(value) > 1024:
        raise ValueError('Passwords are limited to 1,024 characters here.')
    return "'" + value.replace("'", "''") + "'"


def _with_password_mode(statement):
    """The statement inside its own session's NO_BACKSLASH_ESCAPES, restored
    after, so a password literal holds a backslash as itself. Both clients
    follow the server's status flag before parsing the next line (verified
    live on MySQL 8.0, 8.4, 9.7 and MariaDB 10.11, 11.4 with the mysql 8.0
    client: a password ending in a backslash logs in)."""
    return ("SET @myflames_accounts_sql_mode = @@SESSION.sql_mode;\n"
            "SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'NO_BACKSLASH_ESCAPES');\n" +
            TAG + statement + ';\nSET SESSION sql_mode = @myflames_accounts_sql_mode')


def _with_package_mode(statement):
    """MariaDB 10.11 recognizes PACKAGE grant syntax only in Oracle mode.
    Keep every existing mode (including NO_BACKSLASH_ESCAPES), and restore
    its exact value in this disposable client session after the statement.
    The wrapper is part of the SQL shown in the review and audit.
    """
    return ("SET @myflames_accounts_sql_mode = @@SESSION.sql_mode;\n"
            "SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'ORACLE');\n" +
            TAG + statement + ';\nSET SESSION sql_mode = @myflames_accounts_sql_mode')


def generate_password(length=24):
    """A random password with upper and lower case letters, digits and
    punctuation (so common validation rules such as simple_password_check
    accept it), without quotes, backslash or spaces."""
    lower, upper, digits, other = string.ascii_lowercase, string.ascii_uppercase, string.digits, '!#%+,-.:=?@^_'
    chars = [secret_generator.choice(s) for s in (lower, upper, digits, other)]
    alphabet = lower + upper + digits + other
    chars += [secret_generator.choice(alphabet) for _ in range(max(length, 12) - 4)]
    secret_generator.SystemRandom().shuffle(chars)
    return ''.join(chars)


def _validation_plugins(conn):
    """Active MariaDB password validation plugins, or None when unreadable."""
    try:
        return [r[0] for r in Reader(conn, TAG).rows(
            "SELECT PLUGIN_NAME FROM information_schema.PLUGINS WHERE PLUGIN_TYPE = 'PASSWORD VALIDATION' "
            "AND PLUGIN_STATUS = 'ACTIVE' ORDER BY PLUGIN_NAME") if r and r[0]]
    except ConnectorError:
        return None


def _scope(engine, partial, level, database=None, table=None, columns=None, routine=None,
           kind='PROCEDURE', pattern=False, proxy=None):
    if level == 'global':
        return '*.*'
    if level == 'proxy':
        if not proxy:
            raise ValueError('Name the account to proxy.')
        return _name(proxy['name'], proxy.get('host', '%'), engine)
    if not database:
        raise ValueError('Name the database.')
    if level == 'database':
        if pattern and engine == 'mysql' and partial:
            raise ValueError('Database patterns are unavailable while partial_revokes is ON.')
        if not pattern and (engine == 'mariadb' or not partial):
            database = database.replace('\\', '\\\\').replace('_', '\\_').replace('%', '\\%')
        return privileges.quote_identifier(database) + '.*'
    db = privileges.quote_identifier(database)
    if level in ('table', 'column'):
        if not table:
            raise ValueError('Name the table.')
        return db + '.' + privileges.quote_identifier(table)
    if level == 'routine' and routine:
        valid = ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY') if engine == 'mariadb' else ('PROCEDURE',
                                                                                                   'FUNCTION', 'LIBRARY')
        if kind not in valid:
            raise ValueError('Unsupported routine type.')
        return kind + ' ' + db + '.' + privileges.quote_identifier(routine)
    raise ValueError('Invalid grant scope.')


def _validate_privileges(engine, version, ps, level, dynamic=()):
    if isinstance(ps, str) or not ps:
        raise ValueError('Pass a non-empty privilege list.')
    ps = [privileges.normalize_privilege(p, engine) for p in ps]
    if engine == 'mariadb':
        ps = ['BINLOG MONITOR' if p == 'REPLICATION CLIENT' else p for p in ps]
    if level not in privileges.GRANT_LEVELS:
        raise ValueError('Invalid grant scope.')
    allowed = ({'PROXY'} if level == 'proxy' else
               set(privileges.static_privileges(engine, version, level)))
    if engine == 'mysql' and level == 'global':
        allowed.update(dynamic)
    for p in ps:
        if not re.fullmatch(r'[A-Z][A-Z0-9_ ]*', p) or p not in allowed | {'ALL', 'ALL PRIVILEGES', 'GRANT OPTION'}:
            raise ValueError('%s is not a privilege at this level on this server.' % p)
        if level in ('column', 'proxy') and p not in allowed:
            raise ValueError('ALL and GRANT OPTION cannot be used at this level.')
    if ('ALL' in ps or 'ALL PRIVILEGES' in ps) and len(ps) != 1:
        raise ValueError('ALL must stand alone.')
    return ps


def _effects(conn, engine, name, host, version=''):
    """Sessions and DEFINER objects of an account before Drop or Rename."""
    reader = Reader(conn, TAG)
    warnings, sessions, definers = [], [], []
    try:
        sessions = [{'id': r[0], 'host': r[1], 'command': r[2]} for r in reader.rows(
            'SELECT ID, HOST, COMMAND FROM information_schema.PROCESSLIST WHERE USER = ' + literal(name))]
        if sessions:
            warnings.append('%d session(s) of user %s keep running after this change; end them separately '
                            '(sessions are matched by user name).' % (len(sessions), name))
    except ConnectorError:
        warnings.append('Sessions could not be listed (PROCESS may be needed).')
    definer = literal(name + '@' + host)
    for table, columns in (('ROUTINES', ('ROUTINE_SCHEMA', 'ROUTINE_NAME')), ('VIEWS', ('TABLE_SCHEMA', 'TABLE_NAME')),
                           ('TRIGGERS', ('TRIGGER_SCHEMA', 'TRIGGER_NAME')), ('EVENTS', ('EVENT_SCHEMA', 'EVENT_NAME'))):
        try:
            for r in reader.rows('SELECT ' + ', '.join(columns) + ' FROM information_schema.' + table +
                                 ' WHERE DEFINER = ' + definer):
                definers.append({'kind': table[:-1].lower(), 'database': r[0], 'name': r[1]})
        except ConnectorError:
            warnings.append('Could not list %s by definer; the list may be incomplete.' % table.lower())
    if definers:
        if engine == 'mysql':
            warnings.append('%d object(s) name this account as DEFINER. MySQL refuses the statement with error 4006 '
                            'unless you hold ALLOW_NONEXISTENT_DEFINER; change the objects\' definer first. '
                            'Never grant that privilege just to get past the error.' % len(definers))
        else:
            # ER_NO_SUCH_USER (1449) through 11.8, ER_MALFORMED_DEFINER (1446)
            # from 12.0.1 (commit 78d23a3e605); 1449 seen live on 10.11, 11.4.
            code = 1446 if privileges.version_tuple(version) >= (12, 0, 1) else 1449
            warnings.append('%d object(s) name this account as DEFINER. MariaDB does not check: afterwards its '
                            'routines fail with error %d, and its views fail with %d for callers with SET USER '
                            'or SUPER and 1045 for everyone else.' % (len(definers), code, code))
    else:
        warnings.append('No object visible to you names this account as DEFINER (the list follows your metadata '
                        'privileges). Rename never rewrites definers.')
    return {'sessions': sessions, 'definers': definers, 'warnings': warnings}


def _direct_entries(model, level, options):
    """Entries granted at exactly this scope: a lower-scope REVOKE cannot
    remove a higher-scope grant (the server answers 1141/1147)."""
    database = options.get('database')
    if level == 'database':
        stored = database
        if not options.get('pattern') and (model['engine'] == 'mariadb' or not model['partial_revokes']):
            stored = database.replace('\\', '\\\\').replace('_', '\\_').replace('%', '\\%')
        return [e for e in model['schemas'] if e['pattern'] == stored]
    if level in ('table', 'column'):
        return [e for e in model['columns' if level == 'column' else 'tables']
                if e['database'] == database and e['table'] == options.get('table')]
    if level == 'routine':
        return [e for e in model['routines'] if e['database'] == database and
                e['name'] == options.get('routine') and e['kind'] == options.get('kind', 'PROCEDURE')]
    if level == 'proxy':
        proxy = options.get('proxy', {})
        return [e for e in model['proxies'] if e['account']['name'] == proxy.get('name') and
                e['account']['host'] == proxy.get('host', '%')]
    return []


def _direct_has(model, privilege, level, options):
    if level == 'global':
        if privilege == 'GRANT OPTION':
            return model['static_grant_option']
        return privilege in model['global'] + model['dynamic'] or bool(set(model['global']) & privileges.ALL_NAMES)
    entries = _direct_entries(model, level, options)
    if privilege == 'GRANT OPTION':
        return any(e['grantable'] for e in entries)
    entries = [e for e in entries if privilege in e['privs'] or set(e['privs']) & privileges.ALL_NAMES]
    if level == 'column':
        return set(options.get('columns', [])) <= {c for e in entries for c in e['columns']}
    return bool(entries)


def _revoke_direct(model, ps, level, options):
    """Mutate only the named account's direct privileges, never its roles."""
    if not ps:
        return
    remove_all = bool(set(ps) & privileges.ALL_NAMES)
    if level == 'global':
        if remove_all and model['engine'] == 'mysql':
            # ALL ON *.* dispatches to mysql_revoke_all, not a scope-only
            # revoke: mysql-server/sql/sql_yacc.yy:17099-17119,17390-17395;
            # sql/auth/sql_authorization.cc:5251-5295 clears static/dynamic
            # grants, GRANT OPTION, restrictions and direct object grants.
            # Roles and PROXY survive. MariaDB instead excludes GRANT_ACL
            # and keeps other scopes: mariadb-server/sql/grant.cc:67-89,
            # sql/sql_lex.cc:13597-13605. Do not apply this rule to MariaDB.
            for key in ('global', 'dynamic', 'global_grantable', 'schemas', 'tables',
                        'columns', 'routines', 'restrictions'):
                model[key] = []
            model['static_grant_option'] = False
            return
        values = set(model['global'])
        if values & privileges.ALL_NAMES:
            values = (values - privileges.ALL_NAMES) | privileges.static_privileges(
                model['engine'], model['version'], 'global')
        model['global'] = sorted(values - set(ps)) if not remove_all else []
        # SHOW GRANTS enumerates dynamic names; ALL must never manufacture
        # static privileges in this bucket (MySQL auth_acls.h:46-64).
        model['dynamic'] = [p for p in model['dynamic'] if p not in ps] if not remove_all else []
        if 'GRANT OPTION' in ps:
            model['global_grantable'] = []
            model['static_grant_option'] = False
        else:
            held = set(model['global'] + model['dynamic'])
            model['global_grantable'] = [p for p in model['global_grantable'] if p in held]
    else:
        for e in _direct_entries(model, level, options):
            if 'GRANT OPTION' in ps:
                e['grantable'] = False
            if level == 'column':
                if set(ps) & set(e['privs']):
                    e['columns'] = [c for c in e['columns'] if c not in options.get('columns', [])]
            else:
                values = set(e['privs'])
                if values & privileges.ALL_NAMES:
                    values = (values - privileges.ALL_NAMES) | privileges.static_privileges(
                        model['engine'], model['version'], level)
                e['privs'] = sorted(values - set(ps)) if not remove_all else []


def _role_key(account, engine):
    return account['name'], '' if engine == 'mariadb' and account.get('role', True) else account.get('host', '%')


def _role_models(before):
    """Group direct role grants by grantee using the single grants parser.

    read_account retains SHOW GRANTS FOR every reachable role, unlike plain
    MySQL SHOW GRANTS which has already merged away privilege provenance.
    """
    grouped, unknown = {}, []
    for line in before.get('role_grants', []):
        try:
            source = privileges.grant_source(line)
        except ValueError:
            unknown.append(line)
            continue
        if source is not None:
            grouped.setdefault((source['name'], source['host']), []).append(line)
        else:
            # Ignore understood metadata only; a future grant syntax must
            # not disappear from a safety simulation.
            unknown.extend(privileges.parse_grants([line], before['engine'], before['version'])['unparsed'])
    models = {key: privileges.parse_grants(lines, before['engine'], before['version'],
                                           before['model']['partial_revokes']) for key, lines in grouped.items()}
    return models, unknown


def _after_revoke(before, ps, level, options, operation='revoke', active_roles=None, target=None):
    """Rebuild the actor's grants after changing one node or role edge.

    With active_roles, only those roots and their reachable descendants count;
    a shared descendant survives while another path reaches it. PUBLIC is an
    independent root on MariaDB. Without active_roles, retain all granted roles
    for callers inspecting an account rather than a connected session.
    """
    engine = before['engine']
    own_key = (before['name'], before['host'])
    target = target or before
    target_key = (target['name'], target['host'])
    models, unknown = _role_models(before)
    models[own_key] = copy.deepcopy(before['model'])
    if target_key != own_key:
        # MariaDB SHOW GRANTS FOR a role includes its descendants' lines;
        # select the target's own contribution before revoking from it.
        target_models, target_unknown = _role_models(dict(
            target, role_grants=target['grants'] + target.get('role_grants', [])))
        models.update(target_models)
        unknown.extend(target_unknown)
        models.setdefault(target_key, privileges.parse_grants([], engine, target['version'],
                                                               before['model']['partial_revokes']))
    roots = list(models[own_key]['roles'] if active_roles is None else active_roles)
    changed = models[target_key]
    if operation == 'revoke_roles':
        removed = {_role_key(r, engine) for r in options.get('roles', [])}
        changed['roles'] = [r for r in changed['roles'] if _role_key(r, engine) not in removed]
        if target_key == own_key:
            # Mandatory roles cannot be revoked. Keep them independently of
            # the direct edge, just as an alternate inherited path survives.
            roots = [r for r in roots if _role_key(r, engine) not in removed or r.get('mandatory')]
    elif operation != 'drop':
        _revoke_direct(changed, ps, level, options)
    queue = [_role_key(r, engine) for r in roots]
    if engine == 'mariadb' and ('PUBLIC', '') in models:
        queue.append(('PUBLIC', ''))
    reached, contributions = {own_key}, [models[own_key]]
    while queue:
        key = queue.pop(0)
        # DROP removes the vertex and all incident edges, including edges
        # from other active roots. Descendants count only via a surviving
        # path (mysql-server/sql/auth/sql_authorization.cc:651-697;
        # mariadb-server/sql/sql_acl.cc:10678-10701).
        if operation == 'drop' and key == target_key:
            continue
        if key in reached:
            continue
        reached.add(key)
        node = models.get(key)
        if node is None:
            unknown.append('Privileges of role %s could not be read.' % key[0])
            continue
        contributions.append(node)
        queue.extend(_role_key(r, engine) for r in node['roles'])
    model = privileges.parse_grants([], engine, before['version'], before['model']['partial_revokes'])
    for node in contributions:
        for key in ('global', 'dynamic', 'global_grantable'):
            model[key] = list(dict.fromkeys(model[key] + node[key]))
        model['static_grant_option'] = model['static_grant_option'] or node['static_grant_option']
        model['checked'] = model['checked'] and node.get('checked', True)
        for key in ('schemas', 'tables', 'columns', 'routines', 'proxies', 'restrictions', 'roles', 'unparsed'):
            model[key].extend(copy.deepcopy(node[key]))
    model['unparsed'].extend(unknown)
    return model


def _revocation_snapshot(before, actor, conn=None):
    """Direct actor grants plus source-labelled role grants, read again at apply.

    Subtracting a role from plain MySQL SHOW GRANTS would also subtract
    overlapping grants held directly or through another role. Read the actor
    separately; missing provenance is unknown and requires the typed name.
    """
    engine = before['engine']
    current = actor.get('account', '')
    if _same_account(current, before['name'], before['host']):
        snapshot = copy.deepcopy(before)
    elif conn is not None and '@' in current:
        name, host = current.rsplit('@', 1)
        try:
            snapshot = read_account(conn, name, host, False, engine, before['version'], include_catalog=False)
        except ConnectorError:
            return None
        if not snapshot.get('exists'):
            return None
    else:
        return None
    snapshot['role_grants'] = list(snapshot.get('role_grants', []))
    if before.get('role'):
        snapshot['role_grants'].extend(before['grants'] + before.get('role_grants', []))
    # MariaDB prints its active role tree and PUBLIC as separate sources.
    # MySQL prints merged actor grants; never use those as direct evidence.
    if engine == 'mariadb':
        for line in actor.get('grants', []):
            try:
                source = privileges.grant_source(line)
            except ValueError:
                continue
            if source and source.get('role'):
                snapshot['role_grants'].append(line)
    models, _unknown = _role_models(snapshot)
    # Mandatory MySQL roots may not be in SHOW GRANTS FOR the actor.
    for role in actor.get('current_roles', []):
        if _role_key(role, engine) in models:
            continue
        if conn is None:
            continue  # _after_revoke will mark the missing role unknown.
        try:
            extra = read_account(conn, role['name'], role.get('host') or '%', True, engine, before['version'], include_catalog=False)
        except ConnectorError:
            continue
        snapshot['role_grants'].extend(extra.get('grants', []) + extra.get('role_grants', []))
    snapshot['role_grants'] = list(dict.fromkeys(snapshot['role_grants']))
    return snapshot


def _same_account(actor_account, name, host):
    """CURRENT_USER() text ``user@host`` against an account: both parts
    (own password and default roles compare user and host on both engines:
    M/sql/auth/sql_user.cc:170-172, Ma/sql/sql_acl.cc:4224)."""
    if not actor_account or '@' not in actor_account:
        return False
    user, at = actor_account.rsplit('@', 1)
    return user == name and at == host


def _guards(before, actor, operation, ps=(), level='global', options=None, conn=None):
    """Blocks (refuse), the name to type, self-lockout and warnings."""
    actor_model = actor['model'] if 'model' in actor and isinstance(actor['model'], dict) else actor
    current = actor.get('account', actor_model.get('account', ''))
    is_maria_role = before.get('role') and before['engine'] == 'mariadb'
    target = before['name'] if is_maria_role else before['name'] + '@' + before['host']
    own_account = _same_account(current, before['name'], before['host'])
    own = own_account or (
        before.get('role') and any(r['name'] == before['name'] and (is_maria_role or r['host'] == before['host'])
                                   for r in actor.get('current_roles', [])))
    if is_maria_role and before['name'].upper() == 'PUBLIC':
        own = True  # PUBLIC's privileges count for every account.
    blocks, warnings = [], []
    model = before.get('model') or privileges.parse_grants([], before['engine'], before['version'])
    if model.get('unparsed'):
        blocks.append('Some of the account\'s grant lines were not understood, so its protections cannot be checked.')
    if before.get('system') or before['name'] in SYSTEM_ACCOUNTS:
        blocks.append('System accounts are read-only.')
    if _direct_system_user(before) and privileges.has(actor_model, 'SYSTEM_USER') is not True:
        blocks.append('This account holds SYSTEM_USER, so changing it needs SYSTEM_USER.')
    protected = before.get('protected') if 'protected' in before else _protected(before)
    destructive = operation in ('drop', 'rename', 'lock') or (
        operation in ('revoke', 'revoke_roles') and (level == 'global' or bool(set(ps) & DANGEROUS)))
    required_name = target if protected and destructive else ''
    taking_admin = operation in ('drop', 'rename', 'lock', 'expire') or (
        operation in ('revoke', 'revoke_roles') and (not ps or bool(set(ps) & (DANGEROUS | {'UPDATE', 'DELETE', 'INSERT'}))))
    post_checks = {}
    # Dropping the login itself always loses login, regardless of another
    # administrative path. Only other roles need the reachability check.
    dropping_role = operation == 'drop' and before.get('role') and not own_account
    if (operation in ('revoke', 'revoke_roles') or dropping_role) and (own or before.get('role')):
        snapshot = _revocation_snapshot(before, actor, conn)
        if snapshot is not None:
            active = [dict(r, mandatory=any(e.get('mandatory') and _role_key(e, before['engine']) ==
                                            _role_key(r, before['engine']) for e in actor_model.get('roles', [])))
                      for r in actor.get('current_roles', actor_model.get('current_roles', []))]
            now_model = _after_revoke(snapshot, (), level, {}, active_roles=active, target=before)
            after_model = _after_revoke(snapshot, ps, level, options or {}, operation, active, before)
            for m in (now_model, after_model):
                m['account'] = current
                # Account writes still need the server's read_only bypass:
                # mysql-server/sql/lock.cc:211-217 and
                # sql/auth/sql_authorization.cc:1874-1880;
                # mariadb-server/sql/lock.cc:206-237. Rebuilding grants must
                # not silently turn the server writable in the simulation.
                m['variables'] = dict(actor.get('variables') or actor_model.get('variables') or {})
            for action in ('create_user', 'account_options', 'drop_user', 'grant_role'):
                post_checks[action] = privileges.can(after_model, action)['allowed']
            lost = any(privileges.can(now_model, action)['allowed'] is True and value is not True
                       for action, value in post_checks.items())
            # These paths are not fully represented by the generic account
            # actions (SYSTEM_USER target, grantable level, role management).
            scope = {k: v for k, v in (options or {}).items()
                     if k in ('database', 'table', 'routine', 'kind', 'columns', 'proxy')}
            for privilege in ('SYSTEM_USER', 'ROLE_ADMIN', 'GRANT OPTION'):
                at = level if privilege == 'GRANT OPTION' else 'global'
                if privileges.has(now_model, privilege, at, **scope) is True and \
                        privileges.has(after_model, privilege, at, **scope) is not True:
                    lost = True
            incomplete = bool(now_model['unparsed'] or after_model['unparsed'])
            # A nested role may not occur in CURRENT_ROLE(). Reachability in
            # the reconstructed graph, reflected in lost access, catches it.
            own = own or lost or incomplete
            taking_admin = lost or incomplete
        elif own or dropping_role:
            post_checks = {action: None for action in ('create_user', 'account_options', 'drop_user', 'grant_role')}
            own = True  # Unreadable provenance cannot exclude a nested role.
            taking_admin = True
    if own and taking_admin:
        required_name = target
        warnings.append('This can take away your own ability to manage accounts or to log in. Type the account name '
                        'to proceed.')
    if protected and required_name:
        warnings.append('Protected account (%s): type its name to proceed.' % protected)
    return {'blocked': blocks, 'typed_name': required_name, 'self_lockout': bool(own and taking_admin),
            'protected': protected, 'after_management': post_checks, 'warnings': warnings}


def _request(operation, name, host, role, options):
    """The non-secret request a plan was built from (the UI can send it back
    as ``plan``)."""
    clean = {k: copy.deepcopy(v) for k, v in options.items() if k not in ('password', 'current_password')}
    clean.update(operation=operation, user=name, host=host, role=bool(role))
    return clean


def plan_change(conn, operation, name, host='%', role=False, actor=None, connection=None, profile=None,
                change_set_id=None, **options):
    """Build one account change set from a fresh read. Never sends a write.

    Dynamic privilege names come from SHOW PRIVILEGES on the server, never
    from the request. ``actor`` (a privileges-1.0 report) may be passed by an
    internal caller; otherwise it is read.
    """
    if operation not in OPERATIONS:
        raise ValueError('Unknown account operation: choose one of %s.' % ', '.join(OPERATIONS))
    engine, version = _server(conn)
    a = _name(name, host, engine, role)
    if role and engine == 'mariadb' and name.upper() == 'PUBLIC' and operation in ('create', 'drop'):
        raise ValueError('PUBLIC is built in and cannot be created or dropped.')
    actor = actor or privileges.read_privileges(conn)
    actor_model = actor.get('model') if isinstance(actor.get('model'), dict) else actor
    if not actor_model.get('checked'):
        raise ValueError('The connected account\'s privileges could not be read, so no change can be planned.')
    before = read_account(conn, name, host, role, engine, version, include_catalog=False)
    if operation == 'create' and before['exists']:
        raise ValueError('That account already exists.')
    if operation != 'create' and not before['exists']:
        raise ValueError('That account does not exist.')
    if role and operation not in ('create', 'drop', 'grant', 'revoke', 'roles'):
        raise ValueError('This action is not available for roles.')
    if before['exists'] and not before.get('attributes_checked', True):
        raise ValueError('The account\'s attributes could not be read (SELECT on the mysql database is needed), so '
                         'the change cannot be checked before it is sent.')
    statements, passwords, warnings = [], [], list(before['warnings'])
    dependencies, registered, ps = [], [], []
    level = options.get('level', 'global')
    own = _same_account(actor.get('account'), name, host)
    action = operation
    generated = None
    random_result = False
    if operation in ('create', 'password'):
        random = bool(options.get('random_password'))
        password = options.get('password')
        if random and password is not None:
            raise ValueError('Choose a typed or a generated password, not both.')
        if not role and password is None and not random:
            raise ValueError('Give a password, or ask for a generated one.')
        if role and (password is not None or random):
            raise ValueError('Roles have no password.')
        if password is not None:
            _password(password)
        if random and engine == 'mysql' and privileges.version_tuple(version) < (8, 0, 18):
            raise ValueError('A generated password needs MySQL 8.0.18 or later.')
        if not role and random and engine == 'mariadb':
            password = generated = generate_password()
        random_result = bool(random and not role)
        if role:
            statements = [TAG + 'CREATE ROLE ' + a]
        elif engine == 'mariadb':
            passwords.append(password)
            pw = _password(password)
            if operation == 'create':
                plugins = _validation_plugins(conn)
                if plugins is None:
                    warnings.append('Password validation plugins could not be checked; if one is active, the first '
                                    'statement fails and nothing is created.')
                if plugins:
                    temporary = generate_password(32)
                    passwords.append(temporary)
                    statements.append(_with_password_mode('CREATE USER ' + a + ' IDENTIFIED BY ' + _password(temporary)
                                                          + ' ACCOUNT LOCK'))
                    warnings.append('A password validation plugin is active (%s), so the account is created locked '
                                    'with a temporary random password: the binary log records only that temporary '
                                    'password, which SET PASSWORD replaces before the account is unlocked. If the '
                                    'server refuses your password, the account stays locked: set a password that '
                                    'meets the policy, or drop it.' % ', '.join(plugins))
                else:
                    statements.append(TAG + 'CREATE USER ' + a + ' ACCOUNT LOCK')
            statements.append(_with_password_mode('SET PASSWORD FOR ' + a + ' = PASSWORD(' + pw + ')'))
            if operation == 'create' and not options.get('locked'):
                statements.append(TAG + 'ALTER USER ' + a + ' ACCOUNT UNLOCK')
            warnings.append(LOG_WARNING)
            if not own or operation == 'create':
                warnings.append('Setting another account\'s password on MariaDB needs UPDATE on the mysql database; '
                                'CREATE USER is not enough.')
        else:
            clause = 'RANDOM PASSWORD' if random else _password(password)
            if password is not None:
                passwords.append(password)
            sql = ('CREATE' if operation == 'create' else 'ALTER') + ' USER ' + a + ' IDENTIFIED BY ' + clause
            if options.get('current_password') is not None:
                if operation == 'create':
                    raise ValueError('The current password is only for changing your own password.')
                if not own:
                    raise ValueError('The current password is only for your own account (same user and host).')
                if privileges.version_tuple(version) < (8, 0, 13):
                    raise ValueError('REPLACE needs MySQL 8.0.13 or later.')
                sql += ' REPLACE ' + _password(options['current_password'])
                passwords.append(options['current_password'])
            if operation == 'create' and options.get('locked'):
                sql += ' ACCOUNT LOCK'
            statements = [_with_password_mode(sql) if password is not None or options.get('current_password') is not None
                          else TAG + sql]
            if random:
                warnings.append('The server generates the password and returns it once; copy it then, myflames does '
                                'not store it.')
        if operation == 'create':
            action = 'create_role' if role else 'create_user'
        else:
            action = 'set_own_password' if own else 'set_password'
    elif operation == 'drop':
        statements = [TAG + ('DROP ROLE ' if role else 'DROP USER ') + a]
        action = 'drop_role' if role else 'drop_user'
    elif operation == 'rename':
        new_name = options.get('new_name')
        new_host = options.get('new_host', host)
        if not new_name:
            raise ValueError('Give the new user name.')
        b = _name(new_name, new_host, engine)
        destination = read_account(conn, new_name, new_host, False, engine, version, include_catalog=False)
        if destination['exists']:
            raise ValueError('An account with the new name already exists.')
        statements = [TAG + 'RENAME USER ' + a + ' TO ' + b]
        action = 'rename_user'
    elif operation in ('lock', 'unlock', 'expire'):
        statements = [TAG + 'ALTER USER ' + a + (' PASSWORD EXPIRE' if operation == 'expire'
                                                 else ' ACCOUNT ' + operation.upper())]
        action = {'lock': 'lock_account', 'unlock': 'unlock_account', 'expire': 'expire_password'}[operation]
        if operation == 'expire' and engine == 'mariadb' and own:
            warnings.append('An expired MariaDB account can only run SET PASSWORD until it changes its password.')
    elif operation in ('grant', 'revoke'):
        catalog, _catalog_meta = _catalog(conn, engine, version)
        dynamic = [entry['name'] for entry in catalog['global'] if entry['dynamic']]
        registered = dynamic
        if 'privileges' not in options:
            raise ValueError('Name the privileges.')
        ps = _validate_privileges(engine, version, options['privileges'], level, dynamic)
        available = {entry['name'] for entry in catalog.get(level, [])}
        for privilege in ps:
            if privilege not in available | {'ALL', 'ALL PRIVILEGES', 'GRANT OPTION'}:
                raise ValueError('%s is not available at this level on this server.' % privilege)
        scope = _scope(engine, before.get('partial_revokes', False), level,
                       **{k: options[k] for k in ('database', 'table', 'columns', 'routine', 'kind', 'pattern', 'proxy')
                          if k in options})
        if level == 'routine' and options.get('kind') == 'LIBRARY' and privileges.version_tuple(version) < (9, 2, 0):
            raise ValueError('LIBRARY grants need MySQL 9.2 or later with the MLE component.')
        names = ', '.join(ps)
        if level == 'column':
            if not options.get('columns'):
                raise ValueError('Name the columns.')
            names = ', '.join(p + ' (' + ', '.join(privileges.quote_identifier(c) for c in options['columns']) + ')'
                              for p in ps)
        sql = operation.upper() + ' ' + names + ' ON ' + scope + (' TO ' if operation == 'grant' else ' FROM ') + a
        if operation == 'grant' and options.get('grant_option'):
            sql += ' WITH GRANT OPTION'
            warnings.append('WITH GRANT OPTION lets this account pass its privileges at this level on to others.')
        package_scope = engine == 'mariadb' and level == 'routine' and options.get('kind') in ('PACKAGE', 'PACKAGE BODY')
        statements = [_with_package_mode(sql) if package_scope else TAG + sql]
        if operation == 'revoke':
            for p in ps:
                if p in privileges.ALL_NAMES:
                    if level != 'global' and not _direct_entries(before['model'], level, options):
                        raise ValueError('Nothing is granted directly at that scope, so there is nothing to revoke.')
                elif not _direct_has(before['model'], p, level, options):
                    raise ValueError('%s is not granted directly at that scope (it may come from a role or a higher '
                                     'level), so this REVOKE would fail.' % p)
        warnings.extend('Dangerous grant: %s.' % p for p in ps if operation == 'grant' and p in DANGEROUS)
        if options.get('database') == 'mysql' and set(ps) & {'SELECT', 'INSERT', 'UPDATE', 'DELETE'}:
            warnings.append('Privileges on the mysql database expose password hashes or allow account administration.')
        if level == 'database' and options.get('pattern'):
            warnings.append('A database pattern: _ and % match any character, so it can cover databases created '
                            'later.')
    elif operation == 'roles':
        mode = options.get('mode', 'grant')
        roles = list(options.get('roles') or [])
        if any(not isinstance(r, dict) or not r.get('name') for r in roles):
            raise ValueError('Name each role.')
        if engine == 'mariadb' and any(r['name'].upper() == 'PUBLIC' for r in roles):
            raise ValueError('PUBLIC cannot be granted or set as a default role.')
        quoted = [_name(r['name'], r.get('host', '%'), engine, True) for r in roles]
        if mode == 'grant':
            for r in roles:
                dependency = read_account(conn, r['name'], r.get('host', '%'), True, engine, version, include_catalog=False)
                if not dependency['exists']:
                    raise ValueError('Role %s does not exist.' % r['name'])
                if role and r['name'] == name and (engine == 'mariadb' or r.get('host', '%') == host):
                    raise ValueError('A role cannot be granted to itself.')
                dependencies.append(dependency)
                if dependency.get('dangerous'):
                    warnings.append('Role %s carries %s.' % (r['name'], ', '.join(dependency['dangerous'])))
        if mode == 'default':
            special = options.get('special')
            if special not in (None, 'ALL', 'NONE') or (special and roles) or \
                    (engine == 'mariadb' and (special == 'ALL' or len(roles) > 1)):
                raise ValueError('Choose roles, ALL or NONE (MariaDB: one role or NONE).')
            for r in roles:
                if not any(e['name'] == r['name'] and (engine == 'mariadb' or e['host'] == r.get('host', '%'))
                           for e in before['roles']):
                    raise ValueError('A default role must already be granted to the account.')
            selection = special or ', '.join(quoted) or 'NONE'
            statements = [TAG + ('SET DEFAULT ROLE ' + selection + ' FOR ' + a if engine == 'mariadb'
                                 else 'ALTER USER ' + a + ' DEFAULT ROLE ' + selection)]
            action = 'default_roles'
        elif mode in ('grant', 'revoke') and quoted:
            # One role per statement: MariaDB keeps the successes of a
            # statement that fails for some accounts (sql_acl.cc:8112-8150).
            statements = [TAG + mode.upper() + ' ' + r + (' TO ' if mode == 'grant' else ' FROM ') + a +
                          (' WITH ADMIN OPTION' if mode == 'grant' and options.get('admin_option') else '')
                          for r in quoted]
            action = 'grant_role' if mode == 'grant' else 'revoke_role'
            if mode == 'revoke' and any(not any(e['name'] == r['name'] and (engine == 'mariadb' or
                                                                            e['host'] == r.get('host', '%'))
                                                for e in before['roles']) for r in roles):
                raise ValueError('A role that is not granted directly cannot be revoked.')
        else:
            raise ValueError('Choose roles to grant or revoke, or default roles.')
    else:  # options
        clauses = []
        for key, value in (options.get('limits') or {}).items():
            if key not in LIMITS or isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4294967295:
                raise ValueError('A resource limit is an integer from 0 to 4294967295.')
            clauses.append(key + ' ' + str(value))
        sql = 'ALTER USER ' + a + (' WITH ' + ' '.join(clauses) if clauses else '')
        expire = options.get('expire')
        if expire is not None:
            if expire in ('DEFAULT', 'NEVER'):
                sql += ' PASSWORD EXPIRE ' + expire
            elif isinstance(expire, int) and not isinstance(expire, bool) and 1 <= expire <= 65535:
                sql += ' PASSWORD EXPIRE INTERVAL %d DAY' % expire
            else:
                raise ValueError('Password expiry is DEFAULT, NEVER or a number of days from 1 to 65535.')
        extra = options.get('password_options') or {}
        if engine == 'mariadb' and extra:
            raise ValueError('These password options exist on MySQL only.')
        for key, value in extra.items():
            since = (8, 0, 19) if key in ('failed_login_attempts', 'lock_time') else (8, 0, 13) if key == 'require_current' \
                else (8, 0, 11)
            if privileges.version_tuple(version) < since:
                raise ValueError('That password option is not available on this server version.')
            keyword = {'history': 'PASSWORD HISTORY', 'reuse_days': 'PASSWORD REUSE INTERVAL',
                       'require_current': 'PASSWORD REQUIRE CURRENT', 'failed_login_attempts': 'FAILED_LOGIN_ATTEMPTS',
                       'lock_time': 'PASSWORD_LOCK_TIME'}.get(key)
            if not keyword:
                raise ValueError('Unknown password option %s.' % key)
            if key == 'require_current':
                if value not in ('DEFAULT', 'OPTIONAL', 'REQUIRED'):
                    raise ValueError('The current-password requirement is DEFAULT, OPTIONAL or REQUIRED.')
                sql += ' ' + keyword + ('' if value == 'REQUIRED' else ' ' + value)
            elif value == 'DEFAULT' and key in ('history', 'reuse_days'):
                sql += ' ' + keyword + ' DEFAULT'
            elif key == 'lock_time' and value == 'UNBOUNDED':
                sql += ' ' + keyword + ' UNBOUNDED'
            elif isinstance(value, int) and not isinstance(value, bool) and \
                    0 <= value <= (32767 if key in ('failed_login_attempts', 'lock_time') else 65535):
                sql += ' ' + keyword + ' ' + str(value) + (' DAY' if key == 'reuse_days' else '')
            else:
                raise ValueError('Invalid value for %s.' % key)
        if not clauses and expire is None and not extra:
            raise ValueError('Choose at least one account option.')
        statements = [TAG + sql]
        action = 'account_options'
    guard_operation = 'revoke_roles' if operation == 'roles' and options.get('mode') == 'revoke' else operation
    guards = _guards(before, actor, guard_operation, ps, level, options, conn=conn)
    for dependency in dependencies:
        guards['blocked'].extend(b for b in _guards(dependency, actor, 'grant_role')['blocked']
                                 if b == 'System accounts are read-only.')
    warnings.extend(guards['warnings'])
    if role and engine == 'mariadb' and name.upper() == 'PUBLIC':
        warnings.append('PUBLIC privileges apply to every account on this server.')
    if name == '' and not role:
        warnings.append('Anonymous account: anyone matching its host can try to log in.')
    effects = _effects(conn, engine, name, host, version) if operation in ('drop', 'rename') and not role else {}
    warnings.extend(effects.get('warnings', []))
    if engine == 'mysql':
        warnings.append('Account statements replicate: on a source every replica applies them; run on a replica '
                        'they create an errant GTID transaction.')
    else:
        warnings.append('Account statements replicate: on a source every replica applies them; run on a replica '
                        'they diverge it. Galera applies them on every node.')
    context = {k: options[k] for k in ('database', 'table', 'columns', 'routine', 'kind', 'level', 'proxy')
               if k in options}
    context.update(privileges=ps, all_privileges=registered, target_system_user=_direct_system_user(before),
                   own_account=own, roles=options.get('roles', []))
    hint = privileges.can(actor_model if 'account' in actor_model else dict(actor_model, account=actor.get('account')),
                          action, **context)
    safety = dbops.connection_safety(profile, connection or _connection(conn))
    bound = dbops.connection_identity(connection or _connection(conn), safety)
    display = []
    for sql in statements:
        for password in sorted((p for p in passwords if p), key=len, reverse=True):
            sql = sql.replace(_password(password), "'[redacted]'")
        display.append(privileges.redact(sql) if 'IDENTIFIED' not in sql else dbops.redact(sql))
    decision = dbops.decide({'kind': 'risky', 'category': 'account', 'statements': display}, safety['type'],
                            safety['read_only'], safety.get('profile_name'))
    public = {'schema_version': PLAN_SCHEMA, 'change_set_id': change_set_id or uuid.uuid4().hex, 'plan_id': None,
              'operation': operation, 'request': _request(operation, name, host, role, options),
              'name': name, 'host': '' if role and engine == 'mariadb' else host, 'role': bool(role),
              'engine': engine, 'version': version, 'account': a, 'action': action, 'before': before,
              'expect': before['fingerprint'], 'connection_identity': bound,
              'actor': actor.get('account'),
              'actor_server': {k: (actor_model.get('variables') or {}).get(k, '') for k in ('server_uuid', 'server_id',
                                                                                             'hostname', 'port')},
              'statements': display, 'warnings': list(dict.fromkeys(warnings)), 'guards': guards, 'privileges': hint,
              'effects': effects, 'dependencies': dependencies, 'generates_password': random_result,
              'classification': {'kind': 'risky', 'category': 'account'},
              'policy': {k: decision.get(k) for k in ('action', 'tone', 'title', 'text', 'connection_type', 'read_only')},
              'status': 'blocked' if guards['blocked'] or hint['allowed'] is False else 'ready'}
    plan = Plan(public, statements, passwords)
    plan._options = {k: copy.deepcopy(v) for k, v in options.items() if k not in ('password', 'current_password')}
    plan._context = context
    plan._random_result = random_result
    plan._generated = generated
    plan._connection = copy.deepcopy(connection or _connection(conn))
    return plan


def plan_create(conn, name, host='%', **options):
    return plan_change(conn, 'create', name, host, **options)


def plan_drop(conn, name, host='%', **options):
    return plan_change(conn, 'drop', name, host, **options)


def plan_rename(conn, name, new_name, host='%', new_host=None, **options):
    return plan_change(conn, 'rename', name, host, new_name=new_name, new_host=host if new_host is None else new_host,
                       **options)


def plan_lock(conn, name, host='%', **options):
    return plan_change(conn, 'lock', name, host, **options)


def plan_unlock(conn, name, host='%', **options):
    return plan_change(conn, 'unlock', name, host, **options)


def plan_expire(conn, name, host='%', **options):
    return plan_change(conn, 'expire', name, host, **options)


def plan_password(conn, name, password=None, host='%', **options):
    return plan_change(conn, 'password', name, host, password=password, **options)


def plan_grant(conn, name, privileges, host='%', **options):
    return plan_change(conn, 'grant', name, host, privileges=privileges, **options)


def plan_revoke(conn, name, privileges, host='%', **options):
    return plan_change(conn, 'revoke', name, host, privileges=privileges, **options)


def plan_roles(conn, name, roles=(), host='%', **options):
    return plan_change(conn, 'roles', name, host, roles=roles, **options)


def plan_options(conn, name, host='%', **options):
    return plan_change(conn, 'options', name, host, **options)


def _safe_error(exc, passwords):
    """A refusal message without secrets: the server text only for errors
    that never quote the statement, redacted anyway; else the number."""
    text = str(exc)
    code = dbops.error_code(text)
    if dbops.may_have_run(exc):
        return code, 'The client stopped before the outcome was known (%s).' % (
            'time limit' if 'timed out' in text else 'connection lost')
    if code in _SAFE_ERROR_TEXT:
        message = re.sub(r'^mysql client exited \d+:\s*', '', ' '.join(text.split()))
        return code, privileges.redact(message, passwords)
    return code, 'ERROR %s: the server refused the statement.' % (code or 'unknown')


def _run_stdin(conn, sql, passwords=()):
    """Send one account statement (or a password script) on stdin, with the
    write flags; errors come back without secrets."""
    try:
        return conn.run_stdin(sql.rstrip().rstrip(';') + ';\n', extra_flags=list(dbops.WRITE_CLIENT_FLAGS))
    except ConnectorError as exc:
        code, message = _safe_error(exc, passwords)
        if dbops.may_have_run(exc):
            raise ConnectorError('Account statement timed out; outcome unknown. ' + message) from None
        raise ConnectorError(message if message.startswith('ERROR') else 'ERROR %s: %s' % (code, message)) from None


def apply(conn, plan, expect, confirmed=False, typed_name='', store=None, surface='ui',
          connection=None, profile=None):
    """Apply an intact plan after the write policy and fresh re-reads.

    ``store`` implements the WorkspaceStore audit methods; every attempt,
    declined and refused ones included, gets a record. The caller must also
    gate before opening *conn* (the endpoint and CLI do); this is a second
    enforcement point. Returns ``account-change-1.0``.
    """
    if not isinstance(plan, Plan) or plan._consumed or _json(dict(plan)) != plan._seal:
        raise ValueError('Use an intact account plan built by myflames.')
    if store is None:
        raise ValueError('An audit store is required; nothing was sent.')
    connection = connection or _connection(conn)
    safety = dbops.connection_safety(profile, connection)
    decision = dbops.decide(dict(plan['classification'], statements=plan['statements']), safety['type'],
                            safety['read_only'], safety.get('profile_name'))
    out = {'schema_version': CHANGE_SCHEMA, 'change_set_id': plan['change_set_id'], 'operation': plan['operation'],
           'account': plan['account'], 'status': 'refused', 'message': '', 'statements': [],
           'before': plan['before'], 'after': {}, 'diff': {}, 'warnings': [], 'audit_ids': []}

    def trail():
        audit = dbops.AuditTrail(store, 'accounts.' + plan['operation'], surface, connection, safety,
                                 policy=dbops.policy_summary(decision))
        audit.server(plan['engine'], plan['version'])
        return audit
    target = {'change_set_id': plan['change_set_id'], 'account': plan['account'], 'operation': plan['operation']}

    def refuse(status, message):
        out.update(status=status, message=message,
                   statements=[{'index': i, 'statement': s, 'status': 'not_sent'}
                               for i, s in enumerate(plan['statements'])])
        audit = trail()
        out['audit_ids'].append(audit.outcome(list(plan['statements']), 'declined' if status in (
            'declined', 'needs_confirmation', 'needs_typed_name') else 'refused', outcome=status, message=message,
            target=target))
        plan._consumed = True
        return out
    gated = dbops.gate(decision, confirmed)
    if gated:
        return refuse(gated[0], gated[1])
    if dbops.connection_identity(connection, safety) != plan['connection_identity'] or expect != plan['expect']:
        return refuse('stale', 'The connection or the account changed since the review; nothing was sent.')
    if not _same_sql_connection(conn, plan._connection):
        return refuse('stale', 'The SQL connection differs from the reviewed one; nothing was sent.')
    engine, version = _server(conn)
    if engine != plan['engine'] or version != plan['version']:
        return refuse('stale', 'The server version changed; nothing was sent.')
    actor = privileges.read_privileges(conn)
    if actor.get('account') != plan['actor'] or not actor.get('checked'):
        return refuse('stale', 'The connected account could not be confirmed; nothing was sent.')
    server = {k: (actor.get('variables') or {}).get(k, '') for k in plan['actor_server']}
    if server != plan['actor_server']:
        return refuse('stale', 'The server identity changed; nothing was sent.')
    for dependency in plan['dependencies']:
        current_role = read_account(conn, dependency['name'], dependency['host'] or '%', True, engine, version, include_catalog=False)
        if fingerprint(current_role) != dependency['fingerprint']:
            return refuse('stale', 'A role changed since the review; nothing was sent.')
    before = read_account(conn, plan['name'], plan['host'] or '%', plan['role'], engine, version, include_catalog=False)
    if fingerprint(before) != expect:
        return refuse('stale', 'The account\'s grants or attributes changed since the review; nothing was sent.')
    if plan['operation'] == 'rename':
        destination = read_account(conn, plan._options['new_name'], plan._options.get('new_host', plan['host']), False,
                                   engine, version, include_catalog=False)
        if destination['exists']:
            return refuse('stale', 'An account with the new name now exists; nothing was sent.')
    guard_op = 'revoke_roles' if plan['operation'] == 'roles' and plan._options.get('mode') == 'revoke' else \
        plan['operation']
    guards = _guards(before, actor, guard_op, plan._context.get('privileges', []),
                     plan._options.get('level', 'global'), plan._options, conn=conn)
    if guards['blocked']:
        return refuse('refused', ' '.join(guards['blocked']) + ' Nothing was sent.')
    if guards['typed_name'] and typed_name != guards['typed_name']:
        return refuse('needs_typed_name', 'Type %s to proceed; nothing was sent.' % guards['typed_name'])
    hint = privileges.can(actor, plan['action'], **plan._context)
    if hint['allowed'] is False:
        return refuse('denied', hint['reason'] + ' Nothing was sent.')
    out['before'] = before
    generated = plan._generated
    try:
        for i, (sql, display) in enumerate(zip(plan._statements, plan['statements'])):
            audit = trail()
            audit_id = audit.attempt([display], target=target,
                                     verified=['policy', 'connection', 'server', 'actor', 'account', 'guards'])
            out['audit_ids'].append(audit_id)
            try:
                result = _run_stdin(conn, sql, plan._passwords)
            except (ConnectorError, KeyboardInterrupt) as exc:
                code = dbops.error_code(str(exc))
                if isinstance(exc, KeyboardInterrupt) or dbops.may_have_run(exc):
                    status = 'unknown'
                elif code in _DENIED:
                    status = 'denied'
                elif code in _INTERRUPTED:
                    status = 'interrupted'
                else:
                    status = 'refused_by_server'
                message = ('Interrupted; the outcome is unknown.' if isinstance(exc, KeyboardInterrupt)
                           else str(exc))
                audit.complete('unknown' if status == 'unknown' else 'error', outcome=status, error=code,
                               message=message)
                out['statements'].append({'index': i, 'statement': display, 'status': status, 'error': code,
                                          'message': message})
                out.update(status=status if i == 0 else 'partial' if status != 'unknown' else 'unknown',
                           message=message)
                if isinstance(exc, KeyboardInterrupt):
                    break
                break
            else:
                if plan._random_result and engine == 'mysql':
                    rows = [[MySQLConnection._unescape_mysql(c) for c in line.split('\t')]
                            for line in result.split('\n') if line]
                    if rows and len(rows[0]) >= 3:
                        generated = rows[0][2]
                audit.complete('ok', outcome='sent')
                out['statements'].append({'index': i, 'statement': display, 'status': 'ok'})
        else:
            out.update(status='applied', message='The server accepted every statement; the effect below is a '
                                                 'fresh read.')
    finally:
        plan._consumed = True
        plan._statements = ()
        plan._passwords = ()
        plan._generated = None
    if len(out['statements']) < len(plan['statements']):
        out['statements'].extend({'index': i, 'statement': s, 'status': 'not_sent'}
                                 for i, s in enumerate(plan['statements']) if i >= len(out['statements']))
    if out['status'] == 'partial' and plan['operation'] == 'create' and engine == 'mariadb':
        out['warnings'].append('The account was created but stays locked: set a password that the server accepts, '
                               'then unlock it, or drop it.')
    try:
        renamed = plan['operation'] == 'rename' and out['status'] == 'applied'
        name = plan._options.get('new_name', plan['name']) if renamed else plan['name']
        host = plan._options.get('new_host', plan['host']) if renamed else plan['host']
        after = read_account(conn, name, host or '%', plan['role'], engine, version, include_catalog=False)
        out['after'], out['diff'] = after, grant_diff(before, after)
        if out['audit_ids'] and out['status'] == 'applied':
            try:
                dbops.complete(store, out['audit_ids'][-1], 'ok', outcome='applied',
                               after={'grants': after.get('grants', []), 'attributes': after.get('attributes', {}),
                                      'exists': after.get('exists')})
            except Exception:
                out['warnings'].append('The after-state could not be attached to the audit record.')
    except (ConnectorError, ValueError):
        out['warnings'].append('The statements may have changed the account, but it could not be read afterwards.')
    if generated and out['status'] in ('applied', 'partial'):
        out['generated_password'] = generated  # Returned once; never audited or stored.
    return out
