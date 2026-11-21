"""One reviewed, rolling variable change over explicitly saved group members.

The registry retains the member plans, never credentials. ReviewTokens binds
the complete JSON review without extending its shared format. Each write uses
variables.apply_change's drift checks and same-packet server identity guard.
Stop is cooperative between members; it never cancels an in-flight statement.
"""
import copy
import json
import threading
import time
import uuid
from collections import OrderedDict

from . import connection_groups, dbops, reviewed_writes, topology_group, variables
from .connector import ConnectorError
from .group_members import open_group_member
from .param_compare import BoundedConnection
from .review_tokens import ReviewTokens
from .workspace_store import ENDPOINT_FIELDS, endpoint_value


PLAN_SCHEMA = 'topology-change-plan-1.0'
CHANGE_SCHEMA = 'topology-change-1.0'
OPERATION = 'topology.variable'
MODES = ('global', 'persist', 'persist_only')
PLAN_TTL = 300
PLAN_LIMIT = 64
PLAN_BYTES = 8 * 1024 * 1024
_LOCK = threading.Lock()
_NOT_PERSISTED = "Not persisted: add this line to the server's option file"


class _MemberProgress(reviewed_writes.Progress):
    def __init__(self, count, timeout=None):
        super(_MemberProgress, self).__init__(count, timeout)
        self.profile_id = self.profile_name = None

    def member(self, item):
        with self._lock:
            self.profile_id, self.profile_name = item['profile_id'], item['profile_name']

    def snapshot(self):
        out = super(_MemberProgress, self).snapshot()
        with self._lock:
            out.update(profile_id=self.profile_id, profile_name=self.profile_name)
        out['message'] = ('%s: %s.' % (out['profile_name'], out['phase'])) if out['profile_name'] else None
        return out


class _ProgressRegistry(reviewed_writes.ProgressRegistry):
    def start(self, progress_id, count, timeout=None):
        # Keep the existing id validation, bounds and expiry policy.
        progress = super(_ProgressRegistry, self).start(progress_id, count, timeout)
        if progress is not None:
            with self._lock:
                progress = self._items[progress_id] = _MemberProgress(count, timeout)
        return progress


class _Run(object):
    def __init__(self, plan, progress):
        self.group = plan['group_name']
        self.generation = plan['expect']['group_generation']
        self.active = plan['expect']['active_member_profile_id']
        self.progress = progress
        self.lock = threading.Lock()
        self.stopped = False
        self.finished = False

    def start_member(self, member):
        with self.lock:
            if self.stopped:
                return False
            self.progress.member(member)
            return True


class _Registry(object):
    def __init__(self):
        self.lock = threading.RLock()
        self.tokens = ReviewTokens(ttl=PLAN_TTL, capacity=PLAN_LIMIT)
        self.plans = OrderedDict()
        self.runs = OrderedDict()
        self.progress = _ProgressRegistry()

    def prune(self):
        cutoff = time.monotonic() - PLAN_TTL
        for key, entry in list(self.plans.items()):
            if entry['created'] < cutoff:
                del self.plans[key]
        for key, run in list(self.runs.items()):
            if run.finished and run.progress.touched < time.monotonic() - self.progress.TTL:
                del self.runs[key]


def _registry(store):
    with _LOCK:
        registry = getattr(store, '_topology_changes', None)
        if registry is None:
            registry = store._topology_changes = _Registry()
        return registry


def progress(store, progress_id):
    """Used by the existing action/progress reader, without any server read."""
    return _registry(store).progress.get(progress_id)


def _endpoint(profile):
    return {key: endpoint_value(profile, key) for key in ENDPOINT_FIELDS}


def _route(profile, group):
    return dict(_endpoint(profile), profile_id=profile['id'], group_name=group)


def _snapshot(store, group, active_id):
    profiles = store.list('profile')
    members = [p for p in profiles if isinstance(p, dict) and p.get('group') == group]
    active = next((p for p in members if p.get('id') == active_id), None)
    if not group or active is None:
        raise reviewed_writes.Refusal('This saved connection is outside the current connection group.', 'changed')
    generation = active.get('group_generation')
    if not generation or any(p.get('group_generation') != generation for p in members):
        raise reviewed_writes.Refusal('The connection group changed. Review the change again.', 'changed')
    if len(members) > connection_groups.GROUP_MAX_MEMBERS:
        raise ValueError('A rolling change supports at most 16 saved group members.')
    # The shared helper keeps Read-only per member. This batch has a stricter
    # contract: any Read-only member, including an unselected one, blocks it.
    policies = [dbops.connection_safety(p, _route(p, group), store=store) for p in members]
    ctype = connection_groups.strictest_type([connection_groups.group_effective_safety(profiles, group)] +
                                             [p['type'] for p in policies]) or dbops.DEFAULT_CONNECTION_TYPE
    safety = {'type': ctype, 'read_only': any(p['read_only'] for p in policies)}
    return members, active, generation, safety


def _decision(name, mode, safety, group):
    return dbops.decide(dbops.classify(variables.representative_statement(name, mode)),
                        safety['type'], safety['read_only'], group)


def _guard(store, reviewed, member=None):
    expect = reviewed['expect']
    members, active, generation, safety = _snapshot(store, reviewed['group_name'],
                                                     expect['active_member_profile_id'])
    blocked = dbops.gate(_decision(reviewed['name'], reviewed['mode'], safety, reviewed['group_name']), True)
    if blocked:
        raise reviewed_writes.Refusal('A member of this group is Read-only. Nothing more was sent.', 'read_only')
    if generation != expect['group_generation'] or _endpoint(active) != expect['active_endpoint']:
        raise reviewed_writes.Refusal('The connection group or active connection changed. Review again.', 'changed')
    if safety != reviewed['effective_safety']:
        raise reviewed_writes.Refusal('The group write policy changed. Review the change again.', 'policy_changed')
    if member is not None:
        current = next((p for p in members if p['id'] == member['profile_id']), None)
        if current is None or _endpoint(current) != member['endpoint']:
            raise reviewed_writes.Refusal('The saved member or its endpoint changed. Review the change again.', 'changed')
        return current
    return active


def _facts(conn, profile, name=None, value=None, mode=None):
    """One member snapshot, with role and variable reads on one proven node.

    Identify proxy protocols before using SQL evidence packets. Afterwards
    TargetEvidenceConnection refuses a router switching nodes between reads.
    """
    member = topology_group._entry(profile)
    initial, exclusion = topology_group._identity(conn, member)
    if exclusion is not None:
        raise reviewed_writes.Refusal('Proxy members cannot receive a rolling variable change.', 'proxy')
    evidence = dbops.TargetEvidenceConnection(conn)
    topology_group._member_facts(evidence, member, initial)
    evidence.check()
    observed = evidence.variables or {}
    keys = ('server_id', 'hostname', 'port', 'version') if member['engine'] == 'mariadb' else ('server_uuid',)
    identity = dbops.target_galera_identity(evidence, member['engine'],
                                           {key: observed.get(key) for key in keys}, observed)
    if member['engine'] == 'mariadb' and observed.get('wsrep_node_name'):
        identity['wsrep_node_name'] = observed['wsrep_node_name']
    dbops.target_session_guard(member['engine'], identity)
    for key in ('server_uuid',) if member['engine'] == 'mysql' else ('server_id', 'hostname'):
        if str(initial.get(key)) != str(observed.get(key)):
            raise reviewed_writes.Refusal('The member identity changed during the read. Review again.', 'identity_mismatch')
    channels = [{key: row.get(key) for key in ('name', 'source_uuid', 'source_server_id', 'source_host', 'source_port')}
                for row in member['channels']]
    facts = {'role': member['role'], 'channels': sorted(channels, key=lambda row: json.dumps(row, sort_keys=True)),
             'gr_group_name': initial.get('group_replication_group_name'),
             'gr_single_primary': initial.get('group_replication_single_primary_mode'),
             'node_name': observed.get('wsrep_node_name')}
    # An unavailable role must not silently turn an unknown primary into a
    # standalone server. Optional lag and GTID reads do not affect ordering.
    if member['role'] is None:
        raise reviewed_writes.Refusal('The live member role is unavailable. Review after it can be read.', 'role_unavailable')
    plan = None
    if name is not None:
        plan = variables.plan_change(evidence, name, value, mode)
        if member['engine'] == 'mariadb' and mode in ('persist', 'persist_only'):
            # The same review explicitly shows the GLOBAL fallback and the
            # manual option-file step. Never execute a PERSIST on MariaDB.
            alternative = plan.get('alternative') or {}
            plan = variables.plan_change(evidence, name, value, 'global')
            plan['config_line'] = alternative.get('config_line') or plan.get('config_line')
        if plan.get('variable', {}).get('server_identity') != identity:
            raise reviewed_writes.Refusal('The member identity changed during the variable read.', 'identity_mismatch')
    return identity, facts, plan


def _order(entries):
    """Choose a deterministic order from this call's live facts, never cache."""
    def saved(entry):
        return (entry['profile_name'].lower(), entry['profile_name'], entry['profile_id'])

    facts = {entry['profile_id']: entry.get('_facts') or {} for entry in entries}
    if any(f.get('role') in ('primary', 'secondary') for f in facts.values()):
        rule = 'Group Replication and InnoDB Cluster secondaries first, then the primary; ties use saved name order.'
        return sorted(entries, key=lambda e: ({'secondary': 0, 'primary': 2}.get(facts[e['profile_id']].get('role'), 1), saved(e))), rule
    if any(f.get('role') == 'node' for f in facts.values()):
        rule = 'Galera and PXC nodes in node name order; ties and unnamed nodes use saved name order.'
        return sorted(entries, key=lambda e: ((facts[e['profile_id']].get('node_name') or e['profile_name']).lower(), saved(e))), rule
    if any(f.get('channels') or f.get('role') == 'source' for f in facts.values()):
        by_uuid, by_id = {}, {}
        for entry in entries:
            identity = entry.get('identity') or {}
            for index, key in ((by_uuid, 'server_uuid'), (by_id, 'server_id')):
                if identity.get(key):
                    index.setdefault(str(identity[key]).lower(), []).append(entry['profile_id'])
        parents = {}
        for entry in entries:
            pid = entry['profile_id']
            parents[pid] = []
            for channel in facts[pid].get('channels', []):
                key = channel.get('source_uuid') or channel.get('source_server_id')
                index = by_uuid if channel.get('source_uuid') else by_id
                candidates = index.get(str(key).lower(), []) if key else []
                if len(candidates) == 1:
                    parents[pid].append(candidates[0])

        def depth(pid, path):
            if pid in path:
                raise ValueError('A replication cycle has no deepest-first rolling order. Review a different member set.')
            return max([1 + depth(parent, path | {pid}) for parent in parents[pid]] or [0])

        depths = {pid: depth(pid, set()) for pid in parents}
        rule = 'Asynchronous replicas deepest first, then the root source; ties use saved name order.'
        return sorted(entries, key=lambda e: (-depths[e['profile_id']], saved(e))), rule
    return sorted(entries, key=saved), 'Saved name order.'


def _member(profile):
    return {'profile_id': profile['id'], 'profile_name': profile.get('name') or profile['id'],
            'host': profile.get('host') or '', 'port': profile.get('port') or 3306,
            'identity': None, 'status': 'refused', 'statement': None, 'before': None, 'after': None,
            'effect': None, 'caveats': [], 'privileges': None, 'config_line': None, 'reason': None}


def _binding(profile, entry, change=None):
    endpoint = _endpoint(profile)
    return {'profile_id': entry['profile_id'], 'endpoint': endpoint,
            'endpoint_fingerprint': ReviewTokens.fingerprint('endpoint', None, endpoint),
            'identity': entry['identity'], 'statement': entry['statement'], 'before': entry['before'],
            'order_facts': entry.get('_facts'), 'variable_expect': change.get('expect') if change else None}


def plan(store, group_name, active_member_profile_id, name, value, mode, member_ids):
    """Read current members and issue a single immutable review. Never write."""
    group = connection_groups.validate_group(group_name)
    name = variables.check_name(name)
    if mode not in MODES:
        raise ValueError('Choose global, persist or persist_only for a rolling change.')
    if isinstance(value, (dict, list)) or not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError('Send a scalar variable value.')
    if len(json.dumps(value, allow_nan=False)) > variables.MAX_VALUE_LENGTH * 6:
        raise ValueError('The variable value is too long.')
    if (not isinstance(member_ids, list) or not 1 <= len(member_ids) <= connection_groups.GROUP_MAX_MEMBERS
            or any(not isinstance(pid, str) or not 0 < len(pid) <= 200 for pid in member_ids)
            or len(set(member_ids)) != len(member_ids)):
        raise ValueError('Choose 1 to 16 distinct saved member IDs.')
    members, active, generation, safety = _snapshot(store, group, active_member_profile_id)
    if set(member_ids) - {p['id'] for p in members}:
        raise ValueError('Every selected server must be a current member of this connection group.')
    decision = _decision(name, mode, safety, group)
    blocked = dbops.gate(decision, True)
    if blocked:
        raise ValueError('A member of this group is Read-only. No connections were opened.')
    reviewed = {'schema_version': PLAN_SCHEMA, 'change_set_id': uuid.uuid4().hex, 'group_name': group,
                'name': name, 'value': value, 'mode': mode, 'order_rule': None, 'effective_safety': safety,
                'confirmation': {'required': decision['action'] == 'confirm',
                                 'typed_name': group if safety['type'] == 'production' else None},
                'members': [], 'expect': {'group_name': group, 'group_generation': generation,
                    'active_member_profile_id': active['id'], 'active_endpoint': _endpoint(active),
                    'effective_safety': safety, 'members': []}}
    entries, private = [], {}
    deadline = time.monotonic() + topology_group.REQUEST_SECONDS
    for profile in members:
        entry = _member(profile)
        try:
            _guard(store, reviewed)
            remaining = deadline - time.monotonic()
            if remaining < topology_group.QUERY_SECONDS:
                raise reviewed_writes.Refusal('The planning time budget expired before this member was read.', 'timeout')
            with open_group_member(store, group, profile, lambda: _guard(store, reviewed),
                                   {'query_timeout': topology_group.QUERY_SECONDS,
                                    'connect_timeout': topology_group.QUERY_SECONDS}) as opened:
                conn = BoundedConnection(opened[0], seconds=remaining)
                selected = profile['id'] in member_ids
                identity, facts, change = _facts(conn, profile, name if selected else None, value, mode)
                entry.update(identity=identity, _facts=facts)
                if selected:
                    private[profile['id']] = change
                    entry.update({key: copy.deepcopy(change.get(key)) for key in
                                  ('before', 'effect', 'caveats', 'privileges', 'config_line')})
                    if change['engine'] == 'mariadb':
                        entry['caveats'].append(_NOT_PERSISTED)
                    if change['status'] != 'ready':
                        entry['reason'] = ' '.join(p['message'] for p in change['problems'])
                    else:
                        normalized = variables.normalize_value(change['variable'], change['requested'])
                        unchanged = variables._matches(change['variable'], change['mode'], normalized, change['before']) is True
                        entry.update(status='unchanged' if unchanged else 'planned',
                                     statement=None if unchanged else change['statement'],
                                     after={key: change['value'] for key in
                                            (('global', 'persisted') if change['mode'] == 'persist' else
                                             ('persisted',) if change['mode'] == 'persist_only' else ('global',))},
                                     reason='The requested value is already set.' if unchanged else None)
        except reviewed_writes.Refusal as exc:
            entry['reason'] = str(exc)
        except Exception as exc:
            # Never return server/client text, even if it quotes a statement
            # or a password belonging to another member.
            entry['reason'] = 'Could not plan this member: ' + connection_groups.safe_problem(exc)
        entries.append(entry)
    _guard(store, reviewed)
    ordered, reviewed['order_rule'] = _order(entries)
    if reviewed['order_rule'].startswith('Asynchronous'):
        # An unselected relay can change the relative depths of selected
        # replicas. Its own live identity and channels are review evidence,
        # even though no variable statement will ever be sent to it.
        dependencies = [entry for entry in ordered if entry['profile_id'] not in member_ids]
        reviewed['expect']['order_dependencies'] = [_binding(
            next(p for p in members if p['id'] == entry['profile_id']), entry) for entry in dependencies]
        if any(not entry.get('_facts') or not entry.get('identity') for entry in dependencies):
            for entry in ordered:
                if entry['profile_id'] in member_ids:
                    entry.update(status='refused', statement=None,
                                 reason='The replication order could not be verified on every group member.')
    for entry in ordered:
        if entry['profile_id'] not in member_ids:
            continue
        profile = next(p for p in members if p['id'] == entry['profile_id'])
        change = private.get(entry['profile_id'])
        reviewed['expect']['members'].append(_binding(profile, entry, change))
        entry.pop('_facts', None)
        reviewed['members'].append(entry)
    # Token and retained plans share an immutable deep copy. The request can
    # never replace a stored statement, identity, policy or before value.
    details = {'reviewed': reviewed, 'variables': private}
    retained = json.dumps(details, allow_nan=False)
    if len(retained.encode('utf-8')) > PLAN_BYTES:
        raise ValueError('This review is too large. Select fewer members.')
    registry = _registry(store)
    with registry.lock:
        registry.prune()
        while registry.plans and (len(registry.plans) >= PLAN_LIMIT or
                sum(e['bytes'] for e in registry.plans.values()) + len(retained.encode('utf-8')) > PLAN_BYTES):
            registry.plans.popitem(last=False)
        token = registry.tokens.issue(OPERATION, reviewed['expect'], details)
        registry.plans[token] = {'created': time.monotonic(), 'bytes': len(retained.encode('utf-8')),
                                 'details': json.loads(retained)}
    return dict(copy.deepcopy(reviewed), plan_id=token)


def _message(status, outcome=None, error=None):
    code = dbops.error_code(str(error)) if error is not None else None
    if code is not None:
        return 'The server returned error %d. No later member was sent.' % code
    if status == 'refused' and outcome == 'changed':
        outcome = 'changed_target'
    return {'changed': 'The reviewed value was applied.', 'unchanged': 'The requested value is already set.',
            'not_matched': 'The server did not take the exact reviewed value. No later member was sent.',
            'applied_unverified': 'The statement was sent but its effect could not be verified. Check this server.',
            'not_sent': 'Not sent because this rolling change stopped.',
            'stale': 'The review changed or expired. Review the change again.',
            'needs_confirmation': 'Confirm the reviewed change before applying it.',
            'needs_typed_name': 'Type the connection group name exactly to confirm.',
            'read_only': 'A member of this group is Read-only. Nothing more was sent.',
            'identity_mismatch': 'The server identity changed. Nothing was sent to this member.',
            'role_changed': 'The live role or replication source changed. Review the rolling order again.',
            'changed_target': 'The saved member, group or endpoint changed. Review again.',
            'policy_changed': 'The group write policy changed. Review again.',
            'stopped': 'Stop requested. No later member will start.',
            'unknown': 'The statement may have run. Check this server before reviewing another change.'}.get(
                outcome or status, 'This member could not apply the reviewed change. No later member was sent.')


class _MemberAudit(dbops.AuditTrail):
    """One durable record per member; no untrusted diagnostic text is kept."""
    def __init__(self, template, store, reviewed, member, safety):
        route = {'host': member['host'], 'port': member['port']}
        super(_MemberAudit, self).__init__(getattr(template, 'store', store), OPERATION,
            getattr(template, 'surface', 'ui'), connection=route,
            safety=dict(safety, profile_id=member['profile_id'], profile_name=member['profile_name']),
            policy=dbops.policy_summary(_decision(reviewed['name'], reviewed['mode'], safety, reviewed['group_name'])))
        self.target = {key: copy.deepcopy(member[key]) for key in ('profile_id', 'profile_name', 'host', 'port', 'identity')}
        self.target.update(change_set_id=reviewed['change_set_id'], group_name=reviewed['group_name'])
        self.variable_plan = None

    def attempt(self, statements, target=None, verified=None):
        return super(_MemberAudit, self).attempt(statements, target=self.target, verified=verified)

    def complete(self, status, outcome=None, error=None, message=None, **kwargs):
        code = dbops.error_code(str(error)) if error is not None else None
        change = self.variable_plan
        if change is not None and outcome in ('changed', 'unchanged') and kwargs.get('after'):
            matched = variables._matches(change['variable'], change['mode'],
                variables.normalize_value(change['variable'], change['requested']), kwargs['after'])
            if matched is not True:
                status, outcome = 'error', 'not_matched'
        return super(_MemberAudit, self).complete(status, outcome=outcome,
            error='ERROR %d' % code if code is not None else None,
            message=_message(status, outcome, error), **kwargs)

    def outcome(self, statements, status, outcome=None, message=None, error=None, target=None, verified=None):
        code = dbops.error_code(str(error)) if error is not None else None
        return super(_MemberAudit, self).outcome(statements, status, outcome=outcome,
            message=_message(status, outcome, error), error='ERROR %d' % code if code is not None else None,
            target=self.target, verified=verified)


class _SendingConnection(object):
    def __init__(self, conn, before_send, progress, states, index):
        self.conn, self.before_send = conn, before_send
        self.progress, self.states, self.index = progress, states, index
        self.sent = False

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def run_write(self, sql, **kwargs):
        self.before_send()
        self.states[self.index] = 'sent'
        self.progress.set('sending', self.states)
        self.sent = True
        answer = self.conn.run_write(sql, **kwargs)
        self.progress.set('reading', self.states)
        return answer


def _result(reviewed):
    return {'schema_version': CHANGE_SCHEMA, 'plan_id': None, 'change_set_id': reviewed['change_set_id'],
            'group_name': reviewed['group_name'], 'name': reviewed['name'], 'value': reviewed['value'],
            'mode': reviewed['mode'], 'status': None, 'message': None, 'members': [], 'audit_ids': []}


def apply(store, audit, plan_id, expect, confirmed, typed_name, progress_id, connection=None):
    """Execute only retained reviewed plans, sequentially, stopping on failure.

    The optional connection is the HTTP caller's Group connection. It adds an
    ownership check; direct callers already name the bound group in expect.
    """
    registry = _registry(store)
    with registry.lock:
        registry.prune()
        entry = registry.plans.get(plan_id) if isinstance(plan_id, str) else None
        entry = copy.deepcopy(entry) if entry else None
    if entry is None:
        message = _message('stale')
        audit_id = audit.outcome([], 'refused', outcome='stale', message=message) if audit is not None else None
        return dict(schema_version=CHANGE_SCHEMA, plan_id=plan_id if isinstance(plan_id, str) else None,
                    change_set_id=None, group_name=None, name=None, value=None, mode=None,
                    status='stale', message=message, members=[], audit_ids=[audit_id] if audit_id else [])
    details = entry['details']
    reviewed, changes = details['reviewed'], details['variables']
    out = _result(reviewed)
    out['plan_id'] = plan_id
    safety = reviewed['effective_safety']

    def refuse_all(status, outcome):
        out.update(status=status, message=_message(status, outcome))
        for member in reviewed['members']:
            trail = _MemberAudit(audit, store, reviewed, member, safety)
            aid = trail.outcome([], 'refused', outcome=outcome)
            out['members'].append(dict(member, status='not_sent', after=None, message=out['message'], audit_id=aid))
            if aid:
                out['audit_ids'].append(aid)
        return out

    try:
        _, _, _, safety = _snapshot(store, reviewed['group_name'], reviewed['expect']['active_member_profile_id'])
        if connection is not None and connection.get('read_only') is True:
            safety = dict(safety, read_only=True)
        blocked = dbops.gate(_decision(reviewed['name'], reviewed['mode'], safety, reviewed['group_name']), confirmed)
        if blocked:
            return refuse_all('refused', 'read_only' if blocked[0] == 'refused' else 'needs_confirmation')
        if connection is not None and (connection.get('group_name') != reviewed['group_name'] or
                connection.get('profile_id') != reviewed['expect']['active_member_profile_id'] or
                _endpoint(connection) != reviewed['expect']['active_endpoint']):
            return refuse_all('refused', 'changed_target')
        if ReviewTokens.fingerprint(OPERATION, None, expect) != ReviewTokens.fingerprint(OPERATION, None, reviewed['expect']):
            return refuse_all('stale', 'stale')
        consume = registry.tokens.consumer(plan_id, OPERATION, reviewed['expect'], details)
        _guard(store, reviewed)
        if reviewed['confirmation']['typed_name'] is not None and typed_name != reviewed['group_name']:
            return refuse_all('refused', 'needs_typed_name')
    except (ValueError, TypeError):
        return refuse_all('refused', 'changed_target')
    with registry.lock:
        if sum(not run.finished for run in registry.runs.values()) >= registry.progress.LIMIT:
            return refuse_all('refused', 'stale')
        running = registry.progress.start(progress_id, len(reviewed['members']))
    if running is None:
        return refuse_all('refused', 'stale')
    run = _Run(reviewed, running)
    with registry.lock:
        registry.runs[progress_id] = run
        # Never discard an active Stop handle. The progress registry itself
        # bounds retained snapshots; finished handles can be evicted too.
        while len(registry.runs) > registry.progress.LIMIT:
            old = next((key for key, value in registry.runs.items() if value.finished), None)
            if old is None:
                break
            del registry.runs[old]
    states = ['waiting'] * len(reviewed['members'])
    consumed, stop_reason, applied, uncertain = False, None, False, False

    def consume_once():
        nonlocal consumed
        if not consumed:
            consume()
            consumed = True

    try:
        for index, member in enumerate(reviewed['members']):
            trail = _MemberAudit(audit, store, reviewed, member, safety)
            result = dict(member, status='not_sent', after=None, message=None, audit_id=None)
            sent = None
            if stop_reason is None and not run.start_member(member):
                stop_reason = 'stopped'
            if stop_reason is not None:
                trail.outcome([], 'refused', outcome='not_sent')
                result['message'] = _message('not_sent')
                states[index] = 'not_sent'
            elif member['status'] == 'refused':
                result.update(status='refused', message=member['reason'])
                trail.outcome([], 'refused', outcome='invalid')
                states[index], stop_reason = 'not_sent', 'refused'
            else:
                binding = reviewed['expect']['members'][index]
                try:
                    current = _guard(store, reviewed, binding)
                    if member['status'] == 'unchanged':
                        # No statement was reviewed for this member.
                        result.update(status='unchanged', after=member['before'], message=_message('unchanged'))
                        trail.outcome([], 'ok', outcome='unchanged')
                        states[index] = 'done'
                    else:
                        running.set('checking', states)
                        with open_group_member(store, reviewed['group_name'], current,
                                lambda: _guard(store, reviewed, binding),
                                {'query_timeout': 30, 'connect_timeout': 10}) as opened:
                            conn = BoundedConnection(opened[0], seconds=120)
                            trail.password = opened[1].get('password')
                            trail.connection = {key: opened[1].get(key) for key in
                                                ('host', 'port', 'user', 'ssh_host', 'ssh_user', 'ssh_port')}

                            def check_target():
                                _guard(store, reviewed, binding)
                                dependencies = list(reviewed['expect'].get('order_dependencies', []))
                                later = []
                                if reviewed['order_rule'].startswith('Asynchronous'):
                                    # A selected relay may sort after its own
                                    # descendants. Its new source can change
                                    # their order before its turn arrives.
                                    later = reviewed['expect']['members'][index + 1:]
                                    dependencies.extend(later)
                                for dependency in dependencies:
                                    if dependency in later:
                                        candidate = store.get('profile', dependency['profile_id'])
                                        if (not isinstance(candidate, dict) or
                                                candidate.get('group') != reviewed['group_name'] or
                                                _endpoint(candidate) != dependency['endpoint'] or
                                                dependency['order_facts'] is None):
                                            # Removal or an edited endpoint
                                            # refuses that selected member on
                                            # its turn, without borrowing a new
                                            # destination for an order read.
                                            continue
                                    peer = _guard(store, reviewed, dependency)
                                    with open_group_member(store, reviewed['group_name'], peer,
                                            lambda: _guard(store, reviewed, dependency),
                                            {'query_timeout': 10, 'connect_timeout': 10}) as dependency_opened:
                                        peer_conn = BoundedConnection(dependency_opened[0], seconds=30)
                                        peer_identity, peer_facts, _ = _facts(peer_conn, peer)
                                        if (peer_identity != dependency['identity'] or
                                                peer_facts != dependency['order_facts']):
                                            raise reviewed_writes.Refusal(_message('role_changed'), 'role_changed')
                                identity, facts, _ = _facts(conn, current)
                                if identity != binding['identity']:
                                    raise reviewed_writes.Refusal(_message('identity_mismatch'), 'identity_mismatch')
                                if facts != binding['order_facts']:
                                    raise reviewed_writes.Refusal(_message('role_changed'), 'role_changed')
                                _guard(store, reviewed, binding)

                            check_target()

                            def before_send():
                                check_target()
                                consume_once()

                            sent = _SendingConnection(conn, before_send, running, states, index)
                            trail.variable_plan = changes[member['profile_id']]
                            answer = variables.apply_change(sent, changes[member['profile_id']],
                                binding['variable_expect'], decision=_decision(reviewed['name'], reviewed['mode'],
                                    safety, reviewed['group_name']), confirmed=True, audit=trail)
                            status = answer['status']
                            # Variables reports a warning-only no-op as unchanged.
                            # It is success here only if the requested scopes match.
                            change = changes[member['profile_id']]
                            matched = answer.get('after') and variables._matches(change['variable'], change['mode'],
                                variables.normalize_value(change['variable'], change['requested']), answer['after']) is True
                            if status in ('changed', 'unchanged') and matched:
                                result['status'] = 'applied' if status == 'changed' else 'unchanged'
                                states[index] = 'done'
                                applied = applied or status == 'changed'
                            else:
                                result['status'] = 'unknown' if status == 'applied_unverified' else 'refused'
                                states[index] = 'unknown' if status == 'applied_unverified' else 'failed' if sent.sent else 'not_sent'
                                stop_reason = 'refused'
                                uncertain = uncertain or bool(answer.get('sent'))
                            result.update(after=answer.get('after'), message=_message(
                                'not_matched' if status in ('changed', 'unchanged') and not matched else status,
                                answer.get('outcome')))
                except Exception as exc:
                    outcome = getattr(exc, 'outcome', None)
                    refused = isinstance(exc, (reviewed_writes.Refusal, connection_groups.ProfileReadError))
                    unknown = bool(sent and sent.sent and not refused)
                    outcome = outcome or ('changed_target' if refused else 'unknown' if unknown else 'refused')
                    result.update(status='unknown' if unknown else 'refused', message=_message(outcome, error=exc))
                    states[index] = 'unknown' if unknown else 'not_sent'
                    uncertain = uncertain or unknown
                    stop_reason = 'refused'
                    if trail.pending:
                        trail.complete('unknown' if unknown else 'refused', outcome=outcome, error=exc)
                    elif not trail.finished:
                        trail.outcome([], 'refused', outcome=outcome, error=exc)
            result['audit_id'] = trail.id
            out['members'].append(result)
            if trail.id:
                out['audit_ids'].append(trail.id)
        if stop_reason is None:
            consume_once()  # An all-unchanged review is also one use.
        out['status'] = ('stopped' if stop_reason == 'stopped' else 'partial' if stop_reason and (applied or uncertain)
                         else 'refused' if stop_reason else 'applied')
        out['message'] = ('Every selected member has the reviewed value.' if out['status'] == 'applied' else
                          _message('stopped') if out['status'] == 'stopped' else
                          'The rolling change stopped. Check each member result before reviewing another change.')
        return out
    finally:
        with run.lock:
            run.finished = True
        running.set('finished', states, out.get('status') or 'refused')


def stop(store, progress_id, group_name=None, active_member_profile_id=None):
    """Prevent the next member starting; never interrupt a statement sent."""
    registry = _registry(store)
    registry.progress.get(progress_id)  # Validate the id before looking up a run.
    with registry.lock:
        run = registry.runs.get(progress_id)
    if run is None or group_name is not None and (run.group != group_name or run.active != active_member_profile_id):
        raise ValueError('No such rolling change.')
    if group_name is not None:
        _, _, generation, _ = _snapshot(store, group_name, active_member_profile_id)
        if generation != run.generation:
            raise ValueError('No such rolling change.')
    with run.lock:
        run.stopped = True
        finished = run.finished
    return {'progress_id': progress_id, 'status': 'finished' if finished else 'stopping',
            'message': 'Stop prevents the next member from starting. A statement already sent is not interrupted.'}
