"""Read-only, credential-free account comparison and SQL export.

Account enumeration and attributes come exclusively from accounts.py. The
projection below deliberately omits its parsed internal and effective models.
"""
import copy
import json

from . import accounts, dbops, privileges
from .connector import ConnectorError
from .param_compare import BoundedConnection, STATUS_TEXT, side_metadata
from .perf_common import Reader, now
from .server_info import product_identity

SCHEMA_VERSION = 'account-compare-1.0'
MAX_ACCOUNTS = 1000
_ATTRIBUTE_FIELDS = ('plugins', 'locked', 'expired', 'limits', 'password_options', 'tls')


def _tokens(parts, identifiers=False):
    # Quoted names compare by their spelling, not the quote character.
    return tuple(('name', privileges.unquote(t)) if t[:1] in ('`', "'", '"') else
                 ('name', t) if identifiers and t not in ('.', '@', '*', ',', '(', ')') else
                 ('word', t.upper()) for t in parts)


def normalize_grant(line):
    """Privilege/column order, spacing and quote-style independent grant key.

    Preserve object and account name case. Do not casefold a database name or
    erase REQUIRE/WITH options. Unusual syntax retains every token.
    """
    parts = privileges.tokens(dbops.strip_credentials(line).strip().rstrip(';'))
    upper = [t.upper() for t in parts]
    if not parts or upper[0] not in ('GRANT', 'REVOKE'):
        return _tokens(parts)
    boundary_word = 'TO' if upper[0] == 'GRANT' else 'FROM'
    try:
        boundary = upper.index(boundary_word)
        on = upper.index('ON') if 'ON' in upper[:boundary] else boundary
        groups = []
        for group in privileges.split_tokens(parts[1:on]):
            if '(' in group and group[-1] == ')':
                index = group.index('(')
                columns = sorted((_tokens(c, True) for c in privileges.split_tokens(group[index + 1:-1])))
                groups.append((_tokens(group[:index]), tuple(columns)))
            else:
                groups.append((_tokens(group, on == boundary), ()))
        # Scope and grantee positions contain identifiers. Options remain words.
        scope = parts[on + 1:boundary] if on < boundary else []
        kind = []
        while scope and scope[0].upper() in ('TABLE', 'PROCEDURE', 'FUNCTION', 'LIBRARY', 'PACKAGE', 'BODY'):
            kind.append(scope.pop(0).upper())
        tail = parts[boundary + 1:]
        option = next((i for i, t in enumerate(tail) if t.upper() in ('WITH', 'REQUIRE', 'IDENTIFIED')), len(tail))
        return (upper[0], tuple(sorted(groups)), tuple(kind), _tokens(scope, True),
                _tokens(tail[:option], True), _tokens(tail[option:]))
    except (ValueError, IndexError):
        return _tokens(parts)


def _safe_account(value):
    out = {k: copy.deepcopy(value[k]) for k in ('name', 'host', 'role', 'exists', 'attributes_checked',
           'defaults_checked', 'tls_checked', 'error') if k in value}
    out.setdefault('role', False)
    out.setdefault('exists', True)
    out['system'] = out['name'] in accounts.SYSTEM_ACCOUNTS
    out['grants'] = []
    for raw in value.get('grants', []):
        line = dbops.strip_credentials(raw)
        try:
            grantee = privileges.grant_source(line)
        except ValueError:
            grantee = None
        if grantee and (grantee['name'], grantee['host']) != (out['name'], out['host']):
            continue
        out['grants'].append(line)
    out['roles'] = [{k: r[k] for k in ('name', 'host', 'admin_option') if k in r} for r in value.get('roles', [])]
    out['default_roles'] = [{k: r[k] for k in ('name', 'host') if k in r} for r in value.get('default_roles', [])]
    attributes = value.get('attributes') or {}
    out['attributes'] = {k: copy.deepcopy(attributes[k]) for k in _ATTRIBUTE_FIELDS if k in attributes}
    # Never retain arbitrary attribute keys from a caller or a future reader.
    out['attributes']['limits'] = {k: v for k, v in attributes.get('limits', {}).items()
                                    if k in accounts.LIMITS + ('MAX_STATEMENT_TIME',)}
    out['attributes']['password_options'] = {k: v for k, v in attributes.get('password_options', {}).items()
        if k in ('expire', 'expire_days', 'history', 'reuse_interval', 'require_current', 'failed_login_attempts', 'lock_time')}
    return out


def capture(conn, server=None):
    bounded = BoundedConnection(conn)
    listing = accounts.list_accounts(bounded)
    version, engine = listing['version'], listing['engine']
    comment = Reader(bounded, accounts.TAG).pairs("SHOW GLOBAL VARIABLES WHERE Variable_name = 'version_comment'").get('version_comment')
    product = product_identity(version, comment)
    identity = {'host': (server or {}).get('host') or getattr(conn, 'host', 'localhost'),
                'port': (server or {}).get('port') or getattr(conn, 'port', 3306), 'engine': engine,
                'flavor': product['flavor'], 'server_product': product['product'], 'server_version': version}
    identity.update({k: server[k] for k in ('profile_id', 'profile_name') if server and k in server})
    result = {'server': identity, 'captured_at': now(), 'complete': listing['complete'],
              'notes': list(listing.get('warnings', [])), 'items': []}
    if len(listing['accounts']) > MAX_ACCOUNTS:
        result['complete'] = False
        result['notes'].append('Only the first %d accounts were read.' % MAX_ACCOUNTS)
    # These are non-secret columns, not SHOW CREATE text or a password hash.
    tls_rows = Reader(bounded, accounts.TAG).optional(
        'SELECT User, Host, ssl_type, HEX(ssl_cipher), HEX(x509_issuer), HEX(x509_subject) '
        'FROM mysql.user ORDER BY User, Host', 'account TLS requirements', result['notes'])
    tls = {(r[0], r[1]): {'type': r[2], 'cipher_hex': r[3], 'issuer_hex': r[4], 'subject_hex': r[5]}
           for r in tls_rows or [] if len(r) == 6}
    for item in listing['accounts'][:MAX_ACCOUNTS]:
        try:
            account = accounts.read_account(bounded, item['name'], item['host'], item.get('role', False),
                                            engine, version, include_catalog=False)
            if not account.get('exists', True):
                account['error'] = 'The account disappeared during the read.'
            key = (item['name'], item['host'])
            account['tls_checked'] = key in tls or item.get('role') and engine == 'mariadb'
            if key in tls:
                account['attributes']['tls'] = tls[key]
            # mysql.user/global_priv exposes actual expiration, including MariaDB's timestamp.
            for field in ('locked', 'expired'):
                if field in item:
                    account['attributes'][field] = item[field]
            if item.get('plugin') and not account['attributes'].get('plugins'):
                account['attributes']['plugins'] = [item['plugin']]
        except (ConnectorError, ValueError) as exc:
            account = dict(item, error='Could not read this account (error %s).' %
                           (dbops.error_code(str(exc)) or 'unavailable'))
        result['items'].append(_safe_account(account))
    return result


def _roles(value):
    return sorted(value, key=lambda r: json.dumps(r, sort_keys=True))


def _changes(a, b):
    grants = [{normalize_grant(g): g for g in side.get('grants', [])} for side in (a, b)]
    changes = {'grants': {'only_in_source': [grants[0][k] for k in sorted(set(grants[0]) - set(grants[1]), key=repr)],
                          'only_in_target': [grants[1][k] for k in sorted(set(grants[1]) - set(grants[0]), key=repr)]}}
    for field in ('roles', 'default_roles'):
        sides = [{json.dumps(r, sort_keys=True): r for r in side.get(field, [])} for side in (a, b)]
        changes[field] = {'only_in_source': [sides[0][k] for k in sorted(set(sides[0]) - set(sides[1]))],
                          'only_in_target': [sides[1][k] for k in sorted(set(sides[1]) - set(sides[0]))]}
    changes['attributes'] = []
    for key in sorted(set(a['attributes']) | set(b['attributes'])):
        x, y = (s['attributes'].get(key) for s in (a, b))
        if key == 'plugins':
            x, y = sorted(x or []), sorted(y or [])
        if x != y:
            changes['attributes'].append({'name': key, 'source': x, 'target': y})
    if a.get('role') != b.get('role'):
        changes['attributes'].append({'name': 'role', 'source': a.get('role'), 'target': b.get('role')})
    return changes


def compare(source, target, source_meta=None, target_meta=None):
    notes = []
    for label, side in (('Source', source), ('Target', target)):
        notes.extend(label + ': ' + n for n in side.get('notes', []))
        if side.get('error'):
            notes.append(label + ': ' + side['error'])
    if source.get('server', {}).get('engine') != target.get('server', {}).get('engine') and source.get('server') and target.get('server'):
        notes.append('Source and target use different engines (MySQL and MariaDB); account and role semantics may differ.')
    indexed = [{(i['name'], i['host']): _safe_account(i) for i in s.get('items', [])} for s in (source, target)]
    rows = []
    for key in sorted(set(indexed[0]) | set(indexed[1])):
        a, b = (i.get(key) for i in indexed)
        changes = _changes(a, b) if a and b else None
        reason = None
        if source.get('error') or target.get('error'):
            reason = 'A comparison side could not be read.'
        elif any(i and i.get('error') for i in (a, b)):
            reason = '; '.join(i['error'] for i in (a, b) if i and i.get('error'))
        elif a is None and not source.get('complete') or b is None and not target.get('complete'):
            reason = 'The missing side has an incomplete account inventory.'
        elif any(i and (i.get('attributes_checked') is False or i.get('defaults_checked') is False or i.get('tls_checked') is False) for i in (a, b)):
            reason = 'Account attributes, TLS requirements or default roles could not be read.'
        different = changes and (any(changes['grants'].values()) or any(changes['roles'].values()) or
                                 any(changes['default_roles'].values()) or changes['attributes'])
        status = ('not_compared' if reason else 'only_in_target' if a is None else 'only_in_source' if b is None else
                  'different' if different else 'identical')
        row = {'name': key[0], 'host': key[1], 'account': '%s@%s' % key, 'status': status,
               'status_text': STATUS_TEXT[status], 'system': (a or b)['system'],
               'source': a, 'target': b, 'differences': changes}
        if reason:
            row['reason'] = reason
        rows.append(row)
    return {'schema_version': SCHEMA_VERSION, 'compared_at': now(), 'source': side_metadata(source, source_meta),
            'target': side_metadata(target, target_meta), 'items': rows, 'notes': notes,
            'counts': {s: sum(r['status'] == s for r in rows) for s in STATUS_TEXT},
            'identical': all(s.get('complete') and not s.get('error') for s in (source, target)) and all(r['status'] == 'identical' for r in rows),
            'complete': all(s.get('complete') and not s.get('error') for s in (source, target)) and
                        not any(r['status'] == 'not_compared' for r in rows)}


def _grant_sql(line, item, engine):
    """Rebuild SHOW GRANTS names with the existing SQL quoting builders.

    Authentication, TLS and resource clauses are intentionally absent from this
    grants-only export. The comparison keeps them in its raw grant projection.
    """
    safe = dbops.strip_credentials(line).strip().rstrip(';')
    model = privileges.parse_grants([safe], engine)
    if model['unparsed']:
        raise ValueError('A grant could not be safely exported; inspect its original text.')
    parts = privileges.tokens(safe)
    upper = [t.upper() for t in parts]
    if not parts or upper[0] not in ('GRANT', 'REVOKE'):
        return None
    boundary_word = 'TO' if upper[0] == 'GRANT' else 'FROM'
    try:
        boundary = upper.index(boundary_word)
        on = upper.index('ON')
    except ValueError:
        return None  # Roles and default roles are rebuilt from structured fields.
    if on > boundary:
        return None
    grantee = privileges.grant_source(line)
    if (grantee['name'], grantee['host']) != (item['name'], item['host']):
        return None  # MariaDB SHOW GRANTS FOR role also prints inherited lines.
    prefix, depth = [upper[0]], 0
    for token in parts[1:on]:
        if token == '(':
            depth += 1
            prefix.append(token)
        elif token == ')':
            depth -= 1
            prefix.append(token)
        elif token == ',':
            prefix.append(token)
        elif depth:
            prefix.append(privileges.quote_identifier(privileges.unquote(token)))
        else:
            prefix.append(token.upper())
    prefix.append('ON')
    scope = list(parts[on + 1:boundary])
    if scope and scope[0].upper() in ('TABLE', 'PROCEDURE', 'FUNCTION', 'LIBRARY', 'PACKAGE'):
        prefix.append(scope.pop(0).upper())
        if prefix[-1] == 'PACKAGE' and scope and scope[0].upper() == 'BODY':
            prefix.append(scope.pop(0).upper())
    for token in scope:
        prefix.append(token if token in ('.', '*', '@') else privileges.quote_identifier(privileges.unquote(token)))
    # REQUIRE/IDENTIFIED/WITH MAX_* never reach the text. Retain GRANT OPTION.
    grantable = any(upper[i:i + 3] == ['WITH', 'GRANT', 'OPTION'] for i in range(boundary, len(parts) - 2))
    statement = ' '.join(prefix) + ' ' + boundary_word + ' ' + accounts._name(
        item['name'], item['host'], engine, item.get('role', False)) + (' WITH GRANT OPTION' if grantable else '')
    if engine == 'mariadb' and any(t.upper() == 'PACKAGE' for t in parts[on + 1:boundary]):
        return accounts._with_package_mode(statement) + ';'
    return statement + ';'


def export_sql(side, chosen=None):
    """Return SQL text only, with role dependencies. System accounts need an explicit choice."""
    if side.get('error'):
        raise ValueError('The export side could not be read.')
    engine = side.get('server', {}).get('engine')
    if engine not in ('mysql', 'mariadb'):
        raise ValueError('The export needs a MySQL or MariaDB side.')
    indexed = {(i['name'], i['host']): _safe_account(i) for i in side.get('items', [])}
    omitted = []
    requested = []
    if chosen is not None:
        if not isinstance(chosen, (tuple, list)) or not all(isinstance(s, str) for s in chosen):
            raise ValueError('Choose accounts as user@host.')
        for text in chosen:
            key = accounts.parse_account(text)
            if engine == 'mariadb' and (key[0], '') in indexed and ('@' not in text or len(privileges.tokens(text)) == 1):
                key = (key[0], '')
            if key not in indexed:
                raise ValueError('A chosen account is not in this side.')
            requested.append(key)
    else:
        if not side.get('complete'):
            raise ValueError('The account inventory is incomplete; choose accounts explicitly.')
        omitted = sorted(k for k, item in indexed.items() if item['system'])
        requested = [k for k, item in indexed.items() if not item['system']]
    selected, queue = set(), list(requested)
    while queue:
        key = queue.pop()
        if key in selected:
            continue
        item = indexed.get(key)
        if item and item['system'] and key not in requested:
            raise ValueError('A required role is a system account; choose it explicitly before exporting.')
        if not item or item.get('error') or item.get('attributes_checked') is False or item.get('defaults_checked') is False:
            raise ValueError('An account or required role could not be read; no SQL was exported.')
        selected.add(key)
        queue.extend((r['name'], r.get('host', '')) for r in item['roles'] + item['default_roles'])
        direct_roles = {(r['name'], r.get('host', '')) for r in item['roles']}
        if any((r['name'], r.get('host', '')) not in direct_roles for r in item['default_roles']):
            raise ValueError('A default role was not directly granted; read this side again before exporting.')
    ordered = sorted(selected, key=lambda k: (not indexed[k].get('role'), k))
    quote = lambda item: accounts._name(item['name'], item['host'], engine, item.get('role', False))
    lines = ['-- Review before running. This text was not sent to a server.',
             '-- Grants only. Authentication, TLS requirements and resource limits are not copied.']
    if omitted:
        lines.append('-- Omitted system accounts: %s.' % ', '.join(quote(indexed[k]) for k in omitted))
    for key in ordered:
        item = indexed[key]
        if item.get('role'):
            if engine == 'mariadb' and item['name'].upper() == 'PUBLIC':
                continue
            lines.append('CREATE ROLE IF NOT EXISTS %s;' % quote(item))
        else:
            lines.append('CREATE USER IF NOT EXISTS %s;' % quote(item))
            lines.append('-- Set the password separately before allowing this account to log in.')
    # All roles/users exist before any grant, so cycles do not change order.
    grants, revokes = [], []
    for key in ordered:
        item = indexed[key]
        for raw in item['grants']:
            statement = _grant_sql(raw, item, engine)
            if statement:
                (revokes if statement.upper().startswith('REVOKE ') else grants).append(statement)
    lines.extend(dict.fromkeys(grants))
    lines.extend(dict.fromkeys(revokes))
    for key in ordered:
        item = indexed[key]
        for role in _roles(item['roles']):
            role_name = accounts._name(role['name'], role.get('host', ''), engine, True)
            lines.append('GRANT %s TO %s%s;' % (role_name, quote(item), ' WITH ADMIN OPTION' if role.get('admin_option') else ''))
    for key in ordered:
        item = indexed[key]
        defaults = _roles(item['default_roles'])
        if defaults:
            if engine == 'mariadb' and len(defaults) != 1:
                raise ValueError('MariaDB permits one default role per account.')
            names = ', '.join(accounts._name(r['name'], r.get('host', ''), engine, True) for r in defaults)
            lines.append('SET DEFAULT ROLE %s %s %s;' % (names, 'FOR' if engine == 'mariadb' else 'TO', quote(item)))
    return '\n'.join(lines) + '\n'
