"""Read-only, credential-free account comparison and SQL export.

Account enumeration and attributes come exclusively from accounts.py. The
projection below deliberately omits its parsed internal and effective models.
"""
import copy
import json
import re
import time
import uuid

from . import accounts, dbops, privileges
from .connector import ConnectorError, unescape_mysql
from .param_compare import BoundedConnection, STATUS_TEXT, side_metadata
from .perf_common import Reader, now
from .server_info import product_identity

SCHEMA_VERSION = 'account-compare-1.0'
MAX_ACCOUNTS = 1000
BATCH_CHUNK_SIZE = 50
_MAX_EVIDENCE_SQL_CHARS = 24000
_ATTRIBUTE_FIELDS = ('plugins', 'locked', 'expired', 'limits', 'password_options', 'tls')
_ERROR_LINE = re.compile(r'^ERROR\s+(\d+)\s*(?:\([^)]*\))?\s*at line\s+(\d+):\s*(.*)$')


class _BatchFallback(Exception):
    """Raised when a framed batch chunk must fall back to per-account reads."""
    def __init__(self, disable_batch=False):
        super(_BatchFallback, self).__init__()
        self.disable_batch = disable_batch


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
    parts = privileges.tokens(accounts._strip_account_credentials(line).strip().rstrip(';'))
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
        line = accounts._strip_account_credentials(raw)
        if line == accounts.OMITTED_ACCOUNT_TEXT:
            out['error'] = 'Account grants omitted: too long.'
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


def _unwrap_non_evidence(conn):
    """Walk non-evidence wrapper connections (.conn) without cycling."""
    chain, seen, cur = [], set(), conn
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        chain.append(cur)
        cur = getattr(cur, 'conn', None)
    return chain


def _capture_timeout(bounded, per_query=True):
    default_timeout = getattr(bounded, 'query_timeout', 30) or 30
    deadlines = [c.deadline for c in _unwrap_non_evidence(bounded) if getattr(c, 'deadline', None) is not None]
    if not deadlines:
        return default_timeout
    remaining = min(deadlines) - time.monotonic()
    if remaining <= 0:
        raise ConnectorError('The comparison read time budget expired.')
    # Evidence failures poison the scope, so its whole packet gets the
    # remaining member budget rather than one statement's timeout.
    return min(remaining, default_timeout) if per_query else remaining


def _decode_row(line):
    return [None if cell == 'NULL' else unescape_mysql(cell) for cell in line.split('\t')]


def _finalize_account(account, item, tls, engine):
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
    return _safe_account(account)


def _reuse_version_comment(bounded, conn, server):
    if server and 'version_comment' in server and server['version_comment'] is not None:
        return server['version_comment']
    if isinstance(bounded, dbops.TargetEvidenceConnection):
        comment = bounded.server_version_comment()
        if comment:
            return comment
    else:
        for node in _unwrap_non_evidence(conn):
            cached = getattr(node, '_version_comment_cache', None)
            if cached is not None:
                return cached
    return Reader(bounded, accounts.TAG).pairs("SHOW GLOBAL VARIABLES WHERE Variable_name = 'version_comment'").get('version_comment')


def _capture_chunk_per_account(bounded, chunk, engine, version, tls, partial):
    from .connection_groups import ProfileReadError
    if engine == 'mysql' and partial is None:
        partial = str(Reader(bounded, accounts.TAG).pairs(
            "SHOW GLOBAL VARIABLES WHERE Variable_name = 'partial_revokes'")
            .get('partial_revokes', '')).upper() in ('ON', '1')
    out = []
    for item in chunk:
        try:
            account = accounts.read_account(bounded, item['name'], item['host'], item.get('role', False),
                                            engine, version, include_catalog=False, partial_revokes=partial)
            out.append(_finalize_account(account, item, tls, engine))
        except ProfileReadError:
            raise
        except (ConnectorError, ValueError) as exc:
            if isinstance(bounded, dbops.TargetEvidenceConnection):
                bounded.check()
            account = dict(item, error='Could not read this account (error %s).' %
                           (dbops.error_code(str(exc)) or 'unavailable'))
            out.append(_safe_account(account))
    return out, partial


def _parse_stderr_errors(stderr, spans):
    errors = {}
    for line in (stderr or '').replace('\r\n', '\n').split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        match = _ERROR_LINE.match(stripped)
        if not match:
            if stripped.startswith('ERROR '):
                code = dbops.error_code(stripped)
                if code is not None and 2000 <= code < 3000:
                    raise ConnectorError(stripped)
                raise _BatchFallback(disable_batch=True)
            continue
        code, at, message = int(match.group(1)), int(match.group(2)), match.group(3)
        if 2000 <= code < 3000:
            raise ConnectorError('ERROR %d: %s' % (code, message))
        matched_tag = None
        for first, last, tag in spans:
            if first <= at <= last:
                matched_tag = tag
                break
        if not (isinstance(matched_tag, tuple) and matched_tag[0] in ('grants', 'create')):
            raise _BatchFallback(disable_batch=True)
        errors.setdefault(matched_tag, (code, message))
    return errors


def _build_batch_plan(chunk, engine, version, read_partial):
    results = [None] * len(chunk)
    valid = []
    for idx, item in enumerate(chunk):
        try:
            quoted = accounts._name(item['name'], item['host'], engine, item.get('role', False))
        except ValueError as exc:
            results[idx] = _safe_account(dict(item, error='Could not read this account (error %s).' %
                                              (dbops.error_code(str(exc)) or 'unavailable')))
            continue
        valid.append((idx, item, quoted))
    if not valid:
        return results, valid, [], [], [], []

    tag = accounts.TAG
    lines, spans, markers, slot_keys = [], [], [], []
    line_no = 1

    def add(stmt, span_tag):
        nonlocal line_no
        breaks = stmt.count('\n')
        lines.append(stmt)
        spans.append((line_no, line_no + breaks, span_tag))
        line_no += breaks + 1

    def add_marker(label):
        # Script clients send statements separately. An earlier marker must
        # not reveal the token that can terminate a later result slot.
        marker = 'myflames_acct_%s:%s' % (uuid.uuid4().hex, label)
        add(tag + "SELECT '%s';" % marker, 'marker')
        markers.append(marker)

    if engine == 'mysql' and privileges.version_tuple(version) >= (8, 0, 17):
        add(tag + 'SET SESSION print_identified_with_as_hex = ON;', 'set_hex')

    if read_partial:
        add_marker('PR')
        add(tag + "SHOW GLOBAL VARIABLES WHERE Variable_name = 'partial_revokes';", 'pr_query')
        slot_keys.append('partial')

    for idx, item, quoted in valid:
        add_marker('%d:G' % idx)
        add(tag + 'SHOW GRANTS FOR ' + quoted + ';', ('grants', idx))
        slot_keys.append(('grants', idx))
        if not (item.get('role', False) and engine == 'mariadb'):
            add_marker('%d:C' % idx)
            add(tag + 'SHOW CREATE USER ' + quoted + ';', ('create', idx))
            slot_keys.append(('create', idx))

    add_marker('END')
    return results, valid, lines, spans, markers, slot_keys


def _parse_framed_output(stdout, markers, slot_keys):
    out_lines = stdout.replace('\r\n', '\n').split('\n')
    if out_lines and out_lines[-1] == '':
        out_lines.pop()
    pos = 0
    while pos < len(out_lines) and out_lines[pos] == '':
        pos += 1
    if pos >= len(out_lines) or out_lines[pos] != markers[0]:
        raise _BatchFallback(disable_batch=True)
    pos += 1
    slot_lines = {}
    for idx, slot_key in enumerate(slot_keys):
        end_marker = markers[idx + 1]
        try:
            end_idx = out_lines.index(end_marker, pos)
        except ValueError:
            raise _BatchFallback(disable_batch=True)
        slot_lines[slot_key] = out_lines[pos:end_idx]
        pos = end_idx + 1
    if any(line for line in out_lines[pos:]):
        raise _BatchFallback(disable_batch=True)
    return slot_lines


def _build_batch_results(results, valid, slot_lines, errors, engine, version, tls, partial, read_partial):
    if read_partial:
        pr_pairs = {}
        for line in slot_lines['partial']:
            if line:
                row = _decode_row(line)
                if row:
                    pr_pairs[row[0]] = row[1] if len(row) > 1 else ''
        partial = str(pr_pairs.get('partial_revokes', '')).upper() in ('ON', '1')

    effective_partial = bool(partial) if engine == 'mysql' else False
    for idx, item, _quoted in valid:
        role = item.get('role', False)
        grants_key = ('grants', idx)
        if grants_key in errors:
            code = errors[grants_key][0]
            if code in (1141, 1133, 1396):
                account = {
                    'name': item['name'], 'host': '' if role and engine == 'mariadb' else item['host'],
                    'role': role, 'exists': False, 'grants': [], 'attributes': {}, 'roles': [],
                    'default_roles': [],
                }
                results[idx] = _finalize_account(account, item, tls, engine)
            else:
                results[idx] = _safe_account(dict(item, error='Could not read this account (error unavailable).'))
            continue
        grant_rows = [_decode_row(line) for line in slot_lines[grants_key] if line]
        create_rows, create_error = None, False
        if not (role and engine == 'mariadb'):
            create_key = ('create', idx)
            if create_key in errors:
                create_error = True
            else:
                create_rows = [_decode_row(line) for line in slot_lines[create_key] if line]
                if not accounts._rows_text(create_rows):
                    create_error = True
        account, _inherited = accounts._build_account_from_reads(
            item['name'], item['host'], role, engine, version, grant_rows,
            create_rows=create_rows, create_error=create_error, partial=effective_partial)
        create_rows = None  # Never kept: it held the hash.
        results[idx] = _finalize_account(account, item, tls, engine)
    return results, partial


def _is_client_timeout(exc):
    # connector.py marks its client-process timeout as
    # ConnectorError('Query timed out after ...'); the read budget expiry uses
    # a different message and must keep failing the capture.
    return str(exc).startswith('Query timed out after ')


def _evidence_packet_has_safe_delimiter(sql):
    # Prefer smaller batches with fixed delimiters. TargetEvidenceConnection
    # can generate a random delimiter if even one account exhausts them.
    return any(candidate not in sql for candidate in dbops._TARGET_DELIMITERS)


def _capture_chunk_evidence(bounded, chunk, engine, version, tls, partial):
    bounded.check()
    read_partial = engine == 'mysql' and partial is None
    results, valid, lines, _spans, markers, slot_keys = _build_batch_plan(chunk, engine, version, read_partial)
    if not valid:
        return results, partial
    sql = '\n'.join(lines)
    if len(chunk) > 1 and (len(sql) > _MAX_EVIDENCE_SQL_CHARS or not _evidence_packet_has_safe_delimiter(sql)):
        mid = len(chunk) // 2
        left, partial = _capture_chunk_evidence(bounded, chunk[:mid], engine, version, tls, partial)
        right, partial = _capture_chunk_evidence(bounded, chunk[mid:], engine, version, tls, partial)
        return left + right, partial
    try:
        stdout = bounded.run(sql, extra_flags=['--comments'],
                             timeout=_capture_timeout(bounded, per_query=False))
    except ConnectorError:
        # Identity failures, consent errors, timeouts and transport errors
        # poison TargetEvidenceConnection, so check() re-raises them unchanged.
        # Only a server SQL error after a validated identity prefix leaves the
        # scope intact and falls back to per-account reads for this chunk.
        bounded.check()
        raise _BatchFallback(disable_batch=False)
    slot_lines = _parse_framed_output(stdout, markers, slot_keys)
    return _build_batch_results(results, valid, slot_lines, {}, engine, version, tls, partial, read_partial)


def _capture_chunk_script(bounded, chunk, engine, version, tls, partial):
    read_partial = engine == 'mysql' and partial is None
    results, valid, lines, spans, markers, slot_keys = _build_batch_plan(chunk, engine, version, read_partial)
    if not valid:
        return results, partial
    script = '\n'.join(lines) + '\n'
    timeout = _capture_timeout(bounded)
    try:
        stdout, stderr = bounded.run_script(script, timeout=timeout)
    except ConnectorError as exc:
        if not _is_client_timeout(exc):
            raise
        # One packet runs every statement of the chunk under a single client
        # timeout, where the per-account path gives each statement its own.
        # A slow but healthy server fits smaller chunks inside the same call
        # timeout and the remaining overall deadline; _capture_timeout raises
        # the budget error once that deadline is spent.
        if len(chunk) <= 1:
            return _capture_chunk_per_account(bounded, chunk, engine, version, tls, partial)
        mid = len(chunk) // 2
        left, partial = _capture_chunk_script(bounded, chunk[:mid], engine, version, tls, partial)
        right, partial = _capture_chunk_script(bounded, chunk[mid:], engine, version, tls, partial)
        return left + right, partial
    errors = _parse_stderr_errors(stderr, spans)
    slot_lines = _parse_framed_output(stdout, markers, slot_keys)
    return _build_batch_results(results, valid, slot_lines, errors, engine, version, tls, partial, read_partial)


def capture(conn, server=None):
    if isinstance(conn, dbops.TargetEvidenceConnection):
        # Group reads own the evidence scope. Never downgrade their identity
        # checks, including when an optional inventory read catches a failure.
        result = _capture(conn, conn, server)
        conn.check()
        return result
    bounded = BoundedConnection(conn)
    evidence = dbops.TargetEvidenceConnection(bounded)
    try:
        evidence.server_version()
    except ConnectorError as exc:
        if getattr(exc, 'identity_outcome', None) != 'identity_unavailable':
            raise
        # Some comparison sides cannot supply a supported server identity.
        # Preserve their existing read behavior within the SAME deadline,
        # explicitly distinguishing it from a checked account inventory.
        result = _capture(bounded, conn, server)
        result['notes'].append('Accounts were read without the single-server check.')
        return result
    result = _capture(evidence, conn, server)
    evidence.check()
    return result


def _capture(bounded, conn, server=None):
    is_evidence = isinstance(bounded, dbops.TargetEvidenceConnection)
    listing = accounts.list_accounts(bounded, include_tls=True, include_role_details=False)
    version, engine = listing['version'], listing['engine']
    comment = _reuse_version_comment(bounded, conn, server)
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
    # Reuse TLS columns already selected by list_accounts on MySQL; fall back
    # to the separate optional read on MariaDB or when mysql.user was denied.
    if isinstance(listing.get('tls'), dict):
        tls = listing['tls']
    else:
        # These are non-secret columns, not SHOW CREATE text or a password hash.
        tls_rows = Reader(bounded, accounts.TAG).optional(
            'SELECT User, Host, ssl_type, HEX(ssl_cipher), HEX(x509_issuer), HEX(x509_subject) '
            'FROM mysql.user ORDER BY User, Host', 'account TLS requirements', result['notes'])
        tls = {(r[0], r[1]): {'type': r[2], 'cipher_hex': r[3], 'issuer_hex': r[4], 'subject_hex': r[5]}
               for r in tls_rows or [] if len(r) == 6}
    items = listing['accounts'][:MAX_ACCOUNTS]
    if not items:
        return result
    partial = False if engine != 'mysql' else None
    use_batch = is_evidence or callable(getattr(bounded, 'run_script', None))
    for start in range(0, len(items), BATCH_CHUNK_SIZE):
        chunk = items[start:start + BATCH_CHUNK_SIZE]
        if use_batch:
            try:
                if is_evidence:
                    batch_items, partial = _capture_chunk_evidence(bounded, chunk, engine, version, tls, partial)
                else:
                    batch_items, partial = _capture_chunk_script(bounded, chunk, engine, version, tls, partial)
                result['items'].extend(batch_items)
                continue
            except _BatchFallback as exc:
                if is_evidence:
                    bounded.check()
                if exc.disable_batch:
                    use_batch = False
        fallback_items, partial = _capture_chunk_per_account(bounded, chunk, engine, version, tls, partial)
        result['items'].extend(fallback_items)
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


def _many_privilege_grants(item):
    """Keep role membership in the structured roles comparison only."""
    out = []
    for line in item['grants']:
        parts = [part.upper() for part in privileges.tokens(line)]
        # MariaDB sql/sql_acl.cc:9829-9850 emits defaults in SHOW GRANTS.
        # They already participate in the structured default_roles comparison.
        if parts[:3] == ['SET', 'DEFAULT', 'ROLE']:
            continue
        boundary = 'FROM' if parts and parts[0] == 'REVOKE' else 'TO'
        if parts and parts[0] in ('GRANT', 'REVOKE') and boundary in parts:
            if 'ON' not in parts[:parts.index(boundary)]:
                continue
        out.append(line)
    return out


def incomplete_account(item):
    """A failed field read must never be interpreted as an absent account."""
    return bool(item.get('error') or item.get('exists') is False or any(
        key.endswith('_checked') and value is False for key, value in item.items()))


def compare_many(snapshots):
    """Compare credential-free account inventories by saved profile ID.

    Each difference names the member differing from the first present member
    in sorted profile order. Details contain fixed descriptions and counts,
    never a grant statement, authentication clause or server error.
    """
    from .param_compare import _many_snapshots
    profile_ids = _many_snapshots(snapshots)
    indexed = {}
    for pid in profile_ids:
        items = {}
        for raw in snapshots[pid]['items']:
            if (not isinstance(raw, dict) or not isinstance(raw.get('name'), str) or
                    not isinstance(raw.get('host'), str)):
                raise ValueError('Invalid account in member inventory.')
            key = (raw['name'], raw['host'])
            if key in items:
                raise ValueError('Duplicate account in member inventory.')
            if incomplete_account(raw):
                raise ValueError('Only completely read accounts can be compared.')
            item = _safe_account(raw)
            if incomplete_account(item):
                raise ValueError('Only completely read accounts can be compared.')
            item['grants'] = _many_privilege_grants(item)
            items[key] = item
        indexed[pid] = items
    keys = set().union(*(set(items) for items in indexed.values())) if indexed else set()
    rows = []
    for key in sorted(keys):
        present = [pid for pid in profile_ids if key in indexed[pid]]
        baseline_id = present[0]
        baseline = indexed[baseline_id][key]
        differences = []
        for pid in profile_ids:
            item = indexed[pid].get(key)
            if item is None:
                differences.append({'profile_id': pid, 'kind': 'attributes',
                                    'detail': 'The account is absent on this member.'})
                continue
            changes = _changes(baseline, item)
            if any(changes['grants'].values()):
                differences.append({'profile_id': pid, 'kind': 'privileges',
                    'detail': 'Privilege grants differ from profile %s (%d only there, %d only here).' % (
                        baseline_id, len(changes['grants']['only_in_source']),
                        len(changes['grants']['only_in_target']))})
            if any(changes['roles'].values()) or any(changes['default_roles'].values()):
                differences.append({'profile_id': pid, 'kind': 'roles',
                    'detail': 'Assigned roles or default roles differ from profile %s.' % baseline_id})
            if changes['attributes']:
                differences.append({'profile_id': pid, 'kind': 'attributes',
                    'detail': '%d account attributes differ from profile %s.' % (
                        len(changes['attributes']), baseline_id)})
        rows.append({'account': '%s@%s' % key,
                     'status': 'missing' if len(present) != len(profile_ids) else
                               'different' if differences else 'same',
                     'present_on': present, 'differences': differences})
    order = {'different': 0, 'missing': 1, 'same': 2}
    return sorted(rows, key=lambda row: (order[row['status']], row['account']))


def _unsafe_name(value, wildcard_schema=False):
    """True when a name cannot safely enter exported SQL.

    Follows privileges._UNSAFE_GRANT_SQL (printable ASCII 0x20-0x7E without
    backslash). SHOW GRANTS database-level patterns (`ON <schema>.*`) escape
    literal `_` and `%` as `\\_` and `\\%` when partial_revokes is OFF, so
    those two wildcard escapes are decoded before checking the allowlist.
    """
    if not isinstance(value, str):
        return True
    if wildcard_schema:
        value = re.sub(r'\\([_%])', r'\1', value)
    return bool(privileges._UNSAFE_GRANT_SQL.search(value))


def _account_unsafe(item, engine):
    if _unsafe_name(item.get('name')):
        return True
    if not (item.get('role') and engine == 'mariadb'):
        if _unsafe_name(item.get('host')):
            return True
    return False


def _role_unsafe(role, engine):
    if _unsafe_name(role.get('name')):
        return True
    if engine != 'mariadb' and _unsafe_name(role.get('host', '')):
        return True
    return False


def _safe_comment_text(value):
    """Printable ASCII without backslash for SQL comments."""
    return privileges._UNSAFE_GRANT_SQL.sub(lambda m: '<U+%04X>' % ord(m.group(0)), str(value))


def _account_comment_label(name, host, engine, role=False):
    if role and engine == 'mariadb' and str(name).upper() == 'PUBLIC':
        return 'PUBLIC'
    safe_name = _safe_comment_text(name).replace('`', '``')
    if role and engine == 'mariadb':
        return '`%s`' % safe_name
    safe_host = _safe_comment_text(host).replace('`', '``')
    return '`%s`@`%s`' % (safe_name, safe_host)


def _grant_sql_checked(line, item, engine):
    """Return ``(statement, unsafe_reason)`` for a SHOW GRANTS line.

    Authentication, TLS and resource clauses are intentionally absent from this
    grants-only export. The comparison keeps them in its raw grant projection.
    Returns ``(None, reason)`` when the account or any object, column or proxy
    name fails privileges._UNSAFE_GRANT_SQL.
    """
    safe = accounts._strip_account_credentials(line).strip().rstrip(';')
    model = privileges.parse_grants([safe], engine)
    if model['unparsed']:
        raise ValueError('A grant could not be safely exported; inspect its original text.')
    parts = privileges.tokens(safe)
    upper = [t.upper() for t in parts]
    if not parts or upper[0] not in ('GRANT', 'REVOKE'):
        return None, None
    boundary_word = 'TO' if upper[0] == 'GRANT' else 'FROM'
    try:
        boundary = upper.index(boundary_word)
        on = upper.index('ON')
    except ValueError:
        return None, None  # Roles and default roles are rebuilt from structured fields.
    if on > boundary:
        return None, None
    grantee = privileges.grant_source(safe)
    if (grantee['name'], grantee['host']) != (item['name'], item['host']):
        return None, None  # MariaDB SHOW GRANTS FOR role also prints inherited lines.
    if _account_unsafe(item, engine):
        return None, 'account name or host contains characters outside printable ASCII or a backslash'
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
            col = privileges.unquote(token)
            if _unsafe_name(col):
                return None, 'object name contains characters outside printable ASCII or a backslash'
            prefix.append(privileges.quote_identifier(col))
        else:
            if _unsafe_name(token):
                return None, 'privilege name contains characters outside printable ASCII or a backslash'
            prefix.append(token.upper())
    prefix.append('ON')
    scope = list(parts[on + 1:boundary])
    if scope and scope[0].upper() in ('TABLE', 'PROCEDURE', 'FUNCTION', 'LIBRARY', 'PACKAGE'):
        prefix.append(scope.pop(0).upper())
        if prefix[-1] == 'PACKAGE' and scope and scope[0].upper() == 'BODY':
            prefix.append(scope.pop(0).upper())
    is_wildcard_schema = len(scope) == 3 and scope[1] == '.' and scope[2] == '*'
    for idx, token in enumerate(scope):
        if token in ('.', '*', '@'):
            prefix.append(token)
        else:
            ident = privileges.unquote(token)
            if _unsafe_name(ident, wildcard_schema=is_wildcard_schema and idx == 0):
                return None, 'object name contains characters outside printable ASCII or a backslash'
            prefix.append(privileges.quote_identifier(ident))
    # REQUIRE/IDENTIFIED/WITH MAX_* never reach the text. Retain GRANT OPTION.
    grantable = any(upper[i:i + 3] == ['WITH', 'GRANT', 'OPTION'] for i in range(boundary, len(parts) - 2))
    statement = ' '.join(prefix) + ' ' + boundary_word + ' ' + accounts._name(
        item['name'], item['host'], engine, item.get('role', False)) + (' WITH GRANT OPTION' if grantable else '')
    if engine == 'mariadb' and any(t.upper() == 'PACKAGE' for t in parts[on + 1:boundary]):
        return accounts._with_package_mode(statement) + ';', None
    return statement + ';', None


def _grant_sql(line, item, engine):
    """Rebuild SHOW GRANTS names with the existing SQL quoting builders.

    Authentication, TLS and resource clauses are intentionally absent from this
    grants-only export. The comparison keeps them in its raw grant projection.
    Returns None when the account or any object name contains a character
    outside printable ASCII or a backslash.
    """
    return _grant_sql_checked(line, item, engine)[0]


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
    label = lambda item: _account_comment_label(item['name'], item['host'], engine, item.get('role', False))
    lines = ['-- Review before running. This text was not sent to a server.',
             '-- Grants only. Authentication, TLS requirements and resource limits are not copied.']
    if omitted:
        lines.append('-- Omitted system accounts: %s.' % ', '.join(label(indexed[k]) for k in omitted))
    unsafe_notes = []
    safe_ordered = []
    for key in ordered:
        item = indexed[key]
        if _account_unsafe(item, engine):
            kind_label = 'role ' if item.get('role') else ''
            what = 'name' if item.get('role') and engine == 'mariadb' else 'account name or host'
            unsafe_notes.append(
                '-- Left out %s%s: %s contains characters outside printable ASCII or a backslash.' %
                (kind_label, label(item), what))
        else:
            safe_ordered.append(key)
    creates = []
    for key in safe_ordered:
        item = indexed[key]
        if item.get('role'):
            if engine == 'mariadb' and item['name'].upper() == 'PUBLIC':
                continue
            creates.append('CREATE ROLE IF NOT EXISTS %s;' % quote(item))
        else:
            creates.append('CREATE USER IF NOT EXISTS %s;' % quote(item))
            creates.append('-- Set the password separately before allowing this account to log in.')
    # All roles/users exist before any grant, so cycles do not change order.
    grants, revokes = [], []
    for key in safe_ordered:
        item = indexed[key]
        skipped_grants = 0
        for raw in item['grants']:
            statement, unsafe_reason = _grant_sql_checked(raw, item, engine)
            if unsafe_reason:
                skipped_grants += 1
            elif statement:
                (revokes if statement.upper().startswith('REVOKE ') else grants).append(statement)
        if skipped_grants:
            noun = 'grant' if skipped_grants == 1 else 'grants'
            unsafe_notes.append(
                '-- Left out %d %s for %s: object name contains characters outside printable ASCII or a backslash.' %
                (skipped_grants, noun, label(item)))
    role_grants = []
    for key in safe_ordered:
        item = indexed[key]
        for role in _roles(item['roles']):
            if _role_unsafe(role, engine):
                role_label = _account_comment_label(role['name'], role.get('host', ''), engine, True)
                unsafe_notes.append(
                    '-- Left out role grant %s for %s: role name or host contains characters outside printable ASCII or a backslash.' %
                    (role_label, label(item)))
                continue
            role_name = accounts._name(role['name'], role.get('host', ''), engine, True)
            role_grants.append('GRANT %s TO %s%s;' % (role_name, quote(item), ' WITH ADMIN OPTION' if role.get('admin_option') else ''))
    default_role_lines = []
    for key in safe_ordered:
        item = indexed[key]
        defaults = _roles(item['default_roles'])
        if defaults:
            if engine == 'mariadb' and len(defaults) != 1:
                raise ValueError('MariaDB permits one default role per account.')
            safe_defaults = []
            for role in defaults:
                if _role_unsafe(role, engine):
                    role_label = _account_comment_label(role['name'], role.get('host', ''), engine, True)
                    unsafe_notes.append(
                        '-- Left out default role %s for %s: role name or host contains characters outside printable ASCII or a backslash.' %
                        (role_label, label(item)))
                else:
                    safe_defaults.append(role)
            if safe_defaults:
                names = ', '.join(accounts._name(r['name'], r.get('host', ''), engine, True) for r in safe_defaults)
                default_role_lines.append('SET DEFAULT ROLE %s %s %s;' % (names, 'FOR' if engine == 'mariadb' else 'TO', quote(item)))
    lines.extend(unsafe_notes)
    lines.extend(creates)
    lines.extend(dict.fromkeys(grants))
    lines.extend(dict.fromkeys(revokes))
    lines.extend(role_grants)
    lines.extend(default_role_lines)
    return '\n'.join(lines) + '\n'
