"""Workspace endpoints for privilege awareness and Users and roles.

``ui.api_response`` sends these paths here (see docs/DEVELOPER_REFERENCE.md,
"Users and roles"):

* ``POST /api/live/privileges`` ``{connection}``: privileges-1.0 with
  ``actions`` (privileges.read_privileges).
* ``POST /api/accounts/list`` ``{connection}``: accounts-1.0.
* ``POST /api/accounts/read`` ``{connection, user, host, role?}``:
  account-1.0 with its ``fingerprint``.
* ``POST /api/accounts/plan`` ``{connection, operation, user, host, ...}``:
  account-plan-1.0 with ``plan_id`` (statements redacted, warnings, guards,
  ``expect``, ``policy``). Reads only.
* ``POST /api/accounts/apply`` ``{connection, plan_id, expect,
  confirmed, typed_name?, password?, current_password?}``:
  account-change-1.0. The connection's write policy runs before any
  connection opens; the saved password is filled only after it. The plan is
  rebuilt from a fresh read with the reviewed request and refused unless its
  ``expect`` and statements match what was reviewed.

The read paths take the saved password like the Server views; apply is a
write path. A password reaches only the plan build in page memory and the
client's stdin; it is never echoed, stored, audited or kept in a plan.
"""
import copy
import math
import sys
import re
import threading
import time

from . import accounts, dbops, privileges
from .connector import ConnectorError, MySQLConnection

PRIVILEGES_PATH = '/api/live/privileges'
READ_PATHS = (PRIVILEGES_PATH, '/api/accounts/list', '/api/accounts/read', '/api/accounts/plan',
              '/api/accounts/scope-objects')
WRITE_PATHS = ('/api/accounts/apply',)
PATHS = READ_PATHS + WRITE_PATHS
#: Reviewed plans kept for apply by plan_id: the request and the redacted
#: statements only, never a password.
PLAN_TTL_SECONDS = 1800
MAX_PLANS = 200
# Bound parsed input before copying or resolving a password, then bound the
# retained Python objects independently. The HTTP body limit also applies.
MAX_REQUEST_BYTES = 256 * 1024
MAX_PLAN_BYTES = 512 * 1024
MAX_TOTAL_PLAN_BYTES = 8 * 1024 * 1024
MAX_PLAN_STATEMENTS = 200
MAX_VALUE_DEPTH = 32
MAX_VALUE_NODES = 10000
_PLANS = {}  # Fallback for callers without a workspace server.
_LOCK = threading.Lock()
_OPTION_TYPES = {
    'random_password': bool, 'locked': bool, 'pattern': bool, 'grant_option': bool, 'admin_option': bool,
    'new_name': str, 'new_host': str, 'level': str, 'database': str, 'table': str, 'routine': str, 'kind': str,
    'mode': str, 'special': str, 'privileges': list, 'columns': list, 'roles': list, 'proxy': dict,
    'limits': dict, 'password_options': dict, 'expire': (str, int)}
_PASSWORD_OPERATIONS = ('create', 'password')


def _measure(value, byte_limit, label, memory=False):
    """Bound a JSON tree without recursion or copying its contents.

    Requests count UTF-8 JSON bytes conservatively; retained plans count
    Python object storage, including container overhead. Repeated references
    count again, so the memory estimate never relies on shared strings.
    Error messages describe limits and never include a value or key.
    """
    stack, active = [(value, 0, False)], set()
    nodes, scheduled, size = 0, 1, 0
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        nodes += 1
        if depth > MAX_VALUE_DEPTH or nodes > MAX_VALUE_NODES:
            raise ValueError(label + ' is too complex; use fewer or less deeply nested values.')
        kind = type(item)
        if kind in (dict, list):
            if id(item) in active or len(item) > MAX_VALUE_NODES:
                raise ValueError(label + ' is too complex; use fewer or less deeply nested values.')
            scheduled += len(item) * (2 if kind is dict else 1)
            if scheduled > MAX_VALUE_NODES:
                raise ValueError(label + ' is too complex; use fewer or less deeply nested values.')
            active.add(id(item))
            stack.append((item, depth, True))
            size += sys.getsizeof(item) if memory else 2 + max(0, len(item) - 1)
            if kind is dict:
                if not memory:
                    size += len(item)  # Colons between keys and values.
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError(label + ' must contain JSON values with text keys.')
                    stack.append((child, depth + 1, False))
                    stack.append((key, depth + 1, False))
            else:
                for child in item:
                    stack.append((child, depth + 1, False))
        elif kind is str:
            # Check characters first so encoding cannot allocate an unbounded
            # temporary string. UTF-8 and escaped control bytes count too.
            if len(item) > byte_limit:
                raise ValueError(label + ' is too large; split the change into smaller requests.')
            try:
                encoded = len(item.encode('utf-8'))
            except UnicodeError:
                raise ValueError(label + ' must contain valid UTF-8 text.') from None
            size += (sys.getsizeof(item) if memory else encoded + 2 + item.count('"') + item.count('\\') +
                     sum(5 for char in item if ord(char) < 32))
        elif item is None or kind is bool:
            size += sys.getsizeof(item) if memory else 5
        elif kind in (int, float):
            if (kind is int and item.bit_length() > 1024) or (kind is float and not math.isfinite(item)):
                raise ValueError(label + ' contains an unsupported numeric value.')
            size += sys.getsizeof(item) if memory else len(str(item))
        else:
            raise ValueError(label + ' must contain only JSON values.')
        if size > byte_limit:
            raise ValueError(label + ' is too large; split the change into smaller requests.')
    return size


def _check_request(payload):
    if not isinstance(payload, dict):
        raise ValueError('Send the change as a JSON object.')
    _measure(payload, MAX_REQUEST_BYTES, 'Account request')
    # Role changes produce one statement per role. Reject before reading any
    # account or copying the list; other option lists remain byte/node bounded.
    roles = payload.get('roles')
    if isinstance(roles, list) and len(roles) > MAX_PLAN_STATEMENTS:
        raise ValueError('An account change can include at most %d roles.' % MAX_PLAN_STATEMENTS)


def _review_context(plan):
    """The reviewed identity and role dependencies must survive rebuilding."""
    return {'actor': plan.get('actor'), 'server': plan.get('actor_server') or {},
            'engine': plan.get('engine'), 'version': plan.get('version'),
            'dependencies': [{key: dependency.get(key) for key in ('name', 'host', 'role', 'fingerprint')}
                             for dependency in plan.get('dependencies', [])]}


def _entry_size(entry):
    return _measure(entry, MAX_PLAN_BYTES, 'Account plan', memory=True)


def _compact_plans(plans):
    # Deleting dict entries does not release the hash table's capacity. Rebuild
    # in place so a rejected retry cannot leave extra storage behind.
    entries = list(plans.items())
    plans.clear()
    plans.update(entries)


def _registry_size(plans):
    return sys.getsizeof(plans) + sum(sys.getsizeof(key) + entry['_retained_bytes']
                                     for key, entry in plans.items())


def _secrets(payload, options=None):
    found = []
    for source in (payload, (payload or {}).get('connection'), options or {}):
        if isinstance(source, dict):
            for key in ('password', 'current_password'):
                if isinstance(source.get(key), str) and source[key]:
                    found.append(source[key])
    return found


def _reraise(exc, secrets):
    """Re-raise *exc* with every secret and credential clause redacted."""
    text = privileges.redact(str(exc), secrets)
    if text != str(exc):
        raise type(exc)(text) from None
    raise exc


def _text(value, label, limit=255, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > limit:
        raise ValueError('%s must be text of at most %d characters.' % (label, limit))
    return value


def request_from(payload):
    """The account request (operation, user, host, role, options) a plan
    payload names, type-checked; secrets are not part of it."""
    _check_request(payload)
    operation = payload.get('operation')
    if operation not in accounts.OPERATIONS:
        raise ValueError('operation must be one of %s.' % ', '.join(accounts.OPERATIONS))
    user = payload.get('user', payload.get('name'))
    _text(user, 'user', 128, allow_empty=True)
    host = payload.get('host', '%')
    _text(host, 'host', 255, allow_empty=True)
    role = payload.get('role', False)
    if not isinstance(role, bool):
        raise ValueError('role must be true or false.')
    options = {}
    for key, kind in _OPTION_TYPES.items():
        source = 'new_user' if key == 'new_name' and 'new_user' in payload else key
        if source not in payload or payload[source] is None:
            continue
        value = payload[source]
        if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
            raise ValueError('%s has the wrong type.' % source)
        options[key] = copy.deepcopy(value)
    for key in ('privileges', 'columns'):
        if key in options and any(not isinstance(v, str) for v in options[key]):
            raise ValueError('%s must be a list of names.' % key)
    if 'roles' in options:
        roles = []
        for r in options['roles']:
            if isinstance(r, str):
                r = {'name': r}
            if not isinstance(r, dict) or not isinstance(r.get('name'), str) or \
                    not isinstance(r.get('host', '%'), str):
                raise ValueError('roles must be a list of {name, host}.')
            roles.append({'name': r['name'], 'host': r.get('host', '%')})
        options['roles'] = roles
    if 'proxy' in options:
        proxy = options['proxy']
        if not isinstance(proxy.get('name'), str) or not isinstance(proxy.get('host', '%'), str):
            raise ValueError('proxy must be {name, host}.')
    return {'operation': operation, 'user': user, 'host': host, 'role': role, 'options': options}


def _password_options(payload, operation):
    out = {}
    for key in ('password', 'current_password'):
        value = payload.get(key)
        if value is None:
            continue
        if operation not in _PASSWORD_OPERATIONS:
            raise ValueError('%s is only accepted for create and password operations.' % key)
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError('%s must be text.' % key)
        out[key] = value
    return out


def _plan_store(workspace):
    """Called under _LOCK: a server owns its plans for its own lifetime."""
    if workspace is None:
        return _PLANS
    if not hasattr(workspace, '_account_plans'):
        workspace._account_plans = {}
    return workspace._account_plans


def _remember(plan, identity, workspace=None):
    statements = plan['statements']
    if not isinstance(statements, list) or len(statements) > MAX_PLAN_STATEMENTS:
        raise ValueError('An account plan can include at most %d statements.' % MAX_PLAN_STATEMENTS)
    # Validate before the deepcopy, then measure the objects actually retained.
    entry = {'request': plan['request'], 'identity': identity, 'statements': statements,
             'expect': plan['expect'], 'review_context': _review_context(plan),
             'created': time.monotonic(), '_retained_bytes': 0}
    _entry_size(entry)
    entry = copy.deepcopy(entry)
    entry['_retained_bytes'] = _entry_size(entry)
    minimum = (entry['_retained_bytes'] + sys.getsizeof(plan['change_set_id']) +
               sys.getsizeof({plan['change_set_id']: entry}))
    if minimum > MAX_TOTAL_PLAN_BYTES:
        raise ValueError('Account plan storage is full; split the change into smaller requests.')
    with _LOCK:
        plans = _plan_store(workspace)
        for key in [k for k, v in plans.items() if entry['created'] - v['created'] > PLAN_TTL_SECONDS]:
            plans.pop(key, None)
        plans[plan['change_set_id']] = entry
        # Both budgets evict the oldest review, preserving the existing policy.
        while len(plans) > MAX_PLANS or _registry_size(plans) > MAX_TOTAL_PLAN_BYTES:
            plans.pop(min(plans, key=lambda k: plans[k]['created']), None)
            _compact_plans(plans)



def _recall(plan_id, identity, workspace=None, consume=False):
    with _LOCK:
        plans = _plan_store(workspace)
        entry = plans.get(plan_id) if isinstance(plan_id, str) else None
        if entry is None or time.monotonic() - entry['created'] > PLAN_TTL_SECONDS:
            plans.pop(plan_id, None) if isinstance(plan_id, str) else None
            raise ValueError('That plan has expired or is unknown; review the change again.')
        # The endpoint must match; a change of the connection's type or
        # read-only flag still reaches the write policy (and is audited there).
        if dbops.identity_endpoint(entry['identity']) != dbops.identity_endpoint(identity):
            raise ValueError('That plan was made for another connection; review the change again.')
        if consume:
            plans.pop(plan_id)
        return copy.deepcopy(entry)


def _retry(plan_id, entry, workspace):
    """A missing typed name may retry, without extending the original TTL."""
    entry['_retained_bytes'] = _entry_size(entry)
    with _LOCK:
        plans = _plan_store(workspace)
        if time.monotonic() - entry['created'] <= PLAN_TTL_SECONDS and len(plans) < MAX_PLANS:
            plans[plan_id] = entry
            if _registry_size(plans) > MAX_TOTAL_PLAN_BYTES:
                plans.pop(plan_id, None)
                _compact_plans(plans)


def _build(conn, request, secrets, connection, profile, change_set_id=None):
    options = dict(request['options'], **secrets)
    return accounts.plan_change(conn, request['operation'], request['user'], request['host'], request['role'],
                                connection=connection, profile=profile, change_set_id=change_set_id, **options)


def response(path, payload, workspace, with_saved_password, request_profile):
    """Answer one Users and roles path. *with_saved_password* and
    *request_profile* are ui.py's helpers (saved password, profile lookup)."""
    from .live import connection_options
    _check_request(payload)
    if path == '/api/accounts/apply':
        return _apply(payload, workspace, with_saved_password, request_profile)
    if path not in READ_PATHS:
        raise ValueError('Unknown workspace operation.')
    request = request_from(payload) if path == '/api/accounts/plan' else None
    secrets = _password_options(payload, request['operation']) if request else {}
    filled = with_saved_password(payload)
    options = connection_options(filled.get('connection'))
    options['query_timeout'] = 30
    try:
        with MySQLConnection(**options) as conn:
            if path == PRIVILEGES_PATH:
                return privileges.read_privileges(conn)
            if path == '/api/accounts/scope-objects':
                return accounts.scope_objects(conn, _text(payload.get('database'), 'database', 64))
            if path == '/api/accounts/list':
                return accounts.list_accounts(conn)
            if path == '/api/accounts/read':
                user = payload.get('user', payload.get('name'))
                _text(user, 'user', 128, allow_empty=True)
                host = payload.get('host', '%')
                role = payload.get('role', False)
                if not isinstance(role, bool):
                    raise ValueError('role must be true or false.')
                return accounts.read_account(conn, user, _text(host, 'host', 255, allow_empty=True), role)
            connection = payload.get('connection')
            profile = request_profile(connection, workspace.store)
            plan = _build(conn, request, secrets, connection, profile)
    except (ValueError, ConnectorError) as exc:
        _reraise(exc, [options.get('password')] + list(secrets.values()))
    public = dict(plan)
    public['plan_id'] = plan['change_set_id']
    _remember(public, plan['connection_identity'], workspace)
    return public


def _unsent(request, status, message, audit_id=None):
    return {'schema_version': accounts.CHANGE_SCHEMA, 'change_set_id': None, 'operation': request['operation'],
            'account': None, 'status': status, 'message': message, 'statements': [], 'before': {}, 'after': {},
            'diff': {}, 'warnings': [], 'audit_ids': [audit_id] if audit_id else []}


class _ApplyAuditStore(object):
    """Confirmation omissions are refusals, never an explicit UI Cancel.

    Normalize at insertion so even a process exit leaves the correct finished
    record. accounts.apply can discover a typed-name guard on its final read.
    """
    def __init__(self, store):
        self.store = store

    def audit_insert(self, row):
        if row.get('outcome') in ('needs_confirmation', 'needs_typed_name'):
            row = dict(row, status='refused')
        return self.store.audit_insert(row)

    def audit_complete(self, audit_id, fields):
        return self.store.audit_complete(audit_id, fields)


def _apply(payload, workspace, with_saved_password, request_profile):
    from .live import connection_options
    plan_id = payload.get('plan_id')
    if not (isinstance(plan_id, str) and re.fullmatch(r'[0-9a-f]{32}', plan_id)):
        raise ValueError('plan_id must be the 32-character id the plan returned; review the change first.')
    connection = payload.get('connection')
    if not isinstance(connection, dict):
        raise ValueError('Enter connection settings.')
    store = workspace.store
    profile = request_profile(connection, store)
    safety = dbops.connection_safety(profile, connection)
    identity = dbops.connection_identity(connection, safety)
    entry = _recall(plan_id, identity, workspace)
    request_payload, reviewed = entry['request'], entry['statements']
    request = request_from(request_payload)
    secrets = _password_options(payload, request['operation'])
    expect = payload.get('expect')
    if not isinstance(expect, str) or not expect:
        raise ValueError('Send the expect value of the plan you reviewed.')
    confirmed = payload.get('confirmed') is True
    typed_name = payload.get('typed_name') or ''
    if not isinstance(typed_name, str):
        raise ValueError('typed_name must be text.')
    decision = dbops.decide({'kind': 'risky', 'category': 'account', 'statements': reviewed or []}, safety['type'],
                            safety['read_only'], safety.get('profile_name'))
    blocked = dbops.gate(decision, confirmed)
    plain = {k: connection.get(k) for k in ('host', 'port', 'user')}
    if blocked:
        audit = dbops.AuditTrail(store, 'accounts.' + request['operation'], 'ui', connection=plain, safety=safety,
                                 policy=dbops.policy_summary(decision))
        audit_id = audit.outcome(reviewed or [request['operation']], 'refused',
                                 outcome='read_only' if blocked[0] == 'refused' else blocked[0],
                                 message=blocked[1], target={'change_set_id': plan_id, 'request': request})
        return _unsent(request, blocked[0], blocked[1], audit_id)
    # Claim atomically before opening a connection: overlapping requests can
    # never send the same reviewed change twice. Policy refusals remain retryable.
    entry = _recall(plan_id, identity, workspace, consume=True)
    filled = with_saved_password(payload)
    options = connection_options(filled.get('connection'))
    options['query_timeout'] = 30
    all_secrets = [options.get('password')] + list(secrets.values())

    def stale(current, message):
        audit = dbops.AuditTrail(store, 'accounts.' + request['operation'], 'ui', connection=plain,
                                 safety=safety, policy=dbops.policy_summary(decision))
        audit.server(current['engine'], current['version'])
        account = request['user'] + ('' if request['role'] and current['engine'] == 'mariadb'
                                     else '@' + request['host'])
        audit_id = audit.outcome(reviewed, 'refused', outcome='stale', message=message,
                                 target={'change_set_id': plan_id, 'account': account})
        result = _unsent(request, 'stale', message, audit_id)
        result.update(change_set_id=plan_id, account=account,
                      statements=[{'index': i, 'statement': s, 'status': 'not_sent'}
                                  for i, s in enumerate(reviewed)])
        return result

    try:
        with MySQLConnection(**options) as conn:
            # Existence and direct-grant checks in plan_change can fail after
            # drift. Compare first so those cases are audited stale refusals,
            # rather than unaudited input errors. The client cannot replace the
            # reviewed expect with a new fingerprint to approve unseen changes.
            current = accounts.read_account(conn, request['user'], request['host'], request['role'])
            if expect != entry['expect'] or accounts.fingerprint(current) != expect:
                return stale(current, 'The account changed since the review; nothing was sent.')
            try:
                plan = _build(conn, request, secrets, connection, profile, change_set_id=plan_id)
            except ValueError:
                # The target can also change between the check and the rebuild.
                current = accounts.read_account(conn, request['user'], request['host'], request['role'])
                if accounts.fingerprint(current) != expect:
                    return stale(current, 'The account changed since the review; nothing was sent.')
                raise
            if _review_context(plan) != entry['review_context']:
                return stale(plan, 'The server, connected account or a role changed since the review; nothing was sent.')
            if reviewed is not None and list(plan['statements']) != list(reviewed):
                return stale(plan, 'The statements would differ from the ones you reviewed; nothing was sent.')
            result = accounts.apply(conn, plan, expect, confirmed=True, typed_name=typed_name,
                                    store=_ApplyAuditStore(store),
                                    surface='ui', connection=connection, profile=profile)
    except (ValueError, ConnectorError) as exc:
        _reraise(exc, all_secrets)
    if result['status'] == 'needs_typed_name':
        _retry(plan_id, entry, workspace)
    return result
