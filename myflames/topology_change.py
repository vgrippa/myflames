"""One reviewed, rolling variable change over explicitly saved group members.

The registry retains the member plans, never credentials. ReviewTokens binds
the complete JSON review without extending its shared format. Each write uses
variables.apply_change's drift checks and same-packet server identity guard.
Stop is cooperative between members; it never cancels an in-flight statement.
"""
import base64
import binascii
import copy
import json
import re
import threading
import time
import uuid
from collections import OrderedDict

from . import connection_groups, dbops, parameters, param_compare, reviewed_writes, topology_group, variables
from .connector import ConnectorError
from .perf_common import Reader, _version_tuple, literal
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
    def _prune(self):
        cutoff = time.monotonic() - self.TTL
        owner = getattr(self, '_topology_owner', None)
        for key, value in list(self._items.items()):
            # An ordinary request can die before its first state change.
            # Rolling entries belong to a live Stop handle even in received.
            run = owner.runs.get(key) if owner is not None else None
            abandoned = value.phase == 'received' and (run is None or run.finished)
            if (value.phase == 'finished' or abandoned) and value.touched < cutoff:
                del self._items[key]
        while len(self._items) >= self.LIMIT:
            old = next((key for key, value in self._items.items() if value.phase == 'finished'), None)
            if old is None:
                break
            del self._items[old]

    def _start(self, progress_id, count, timeout, factory, idle_timeout=None):
        if not isinstance(progress_id, str) or not reviewed_writes._PROGRESS_ID.fullmatch(progress_id):
            return None
        with self._lock:
            self._prune()
            if progress_id in self._items:
                return None
            if len(self._items) >= self.LIMIT:
                return None
            value = (factory(count, timeout, idle_timeout=idle_timeout) if idle_timeout is not None
                     else factory(count, timeout))
            self._items[progress_id] = value
            return value

    def start(self, progress_id, count, timeout=None, idle_timeout=None):
        owner = getattr(self, '_topology_owner', None)
        if owner is not None:
            with owner.lock:
                if isinstance(progress_id, str) and progress_id in owner.runs:
                    return None
                return self._start(progress_id, count, timeout, reviewed_writes.Progress, idle_timeout)
        return self._start(progress_id, count, timeout, reviewed_writes.Progress, idle_timeout)

    def start_member(self, progress_id, count, timeout=None):
        return self._start(progress_id, count, timeout, _MemberProgress)

    def get(self, progress_id):
        owner = getattr(self, '_topology_owner', None)
        if owner is not None:
            with owner.lock:
                return super(_ProgressRegistry, self).get(progress_id)
        return super(_ProgressRegistry, self).get(progress_id)


class _Run(object):
    def __init__(self, plan, progress):
        self.group = plan['group_name']
        self.generation = plan['expect']['group_generation']
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
        self.progress._topology_owner = self

    def prune(self):
        cutoff = time.monotonic() - PLAN_TTL
        for key, entry in list(self.plans.items()):
            if entry['created'] < cutoff:
                del self.plans[key]
        for key, run in list(self.runs.items()):
            if run.finished and run.progress.touched < time.monotonic() - self.progress.TTL:
                del self.runs[key]
        while len(self.runs) > self.progress.LIMIT:
            old = next((key for key, run in self.runs.items() if run.finished), None)
            if old is None:
                break
            del self.runs[old]


def _registry(store):
    with _LOCK:
        registry = getattr(store, '_topology_changes', None)
        if registry is None:
            registry = store._topology_changes = _Registry()
        return registry


def progress(store, progress_id):
    """Used by the existing action/progress reader, without any server read."""
    return _registry(store).progress.get(progress_id)


def share_progress(store, action_progress):
    """Keep ordinary actions and rolls in one ID namespace, including old handles.

    Upgrade the existing registry in place under its lock: an action that
    already holds a reference must also see reservations and safe pruning.
    No operation or progress object is replaced.
    """
    registry = _registry(store)
    with registry.lock:
        if registry.progress is action_progress:
            return action_progress
        with action_progress._lock:
            with registry.progress._lock:
                if set(action_progress._items).intersection(registry.progress._items):
                    raise ValueError('A progress_id is already owned by another action.')
                action_progress._items.update(registry.progress._items)
                action_progress._topology_owner = registry
                action_progress.__class__ = _ProgressRegistry
                registry.progress = action_progress
    return action_progress


def _endpoint(profile):
    return {key: endpoint_value(profile, key) for key in ENDPOINT_FIELDS}


def _route(profile, group):
    return dict(_endpoint(profile), profile_id=profile['id'], group_name=group)


def _snapshot(store, group, active_id):
    profiles = store.list('profile')
    members = [p for p in profiles if isinstance(p, dict) and p.get('group') == group]
    active = next((p for p in members if p.get('id') == active_id), None)
    if not group or active is None:
        raise reviewed_writes.Refusal('This saved connection is outside the current connection group.', 'changed_target')
    generation = active.get('group_generation')
    if not generation or any(p.get('group_generation') != generation for p in members):
        raise reviewed_writes.Refusal('The connection group changed. Review the change again.', 'changed_target')
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
        raise reviewed_writes.Refusal('The connection group or active connection changed. Review again.', 'changed_target')
    if safety != reviewed['effective_safety']:
        raise reviewed_writes.Refusal('The group write policy changed. Review the change again.', 'policy_changed')
    if member is not None:
        current = next((p for p in members if p['id'] == member['profile_id']), None)
        if current is None or _endpoint(current) != member['endpoint']:
            raise reviewed_writes.Refusal('The saved member or its endpoint changed. Review the change again.', 'changed_target')
        return current
    return active


def _session_guard(engine, identity):
    guard = dbops.target_session_guard(engine, identity)
    if engine != 'mariadb':
        return guard
    uid = identity.get('server_uid')
    try:
        valid = isinstance(uid, str) and len(uid) == 28 and len(base64.b64decode(uid, validate=True)) == 20
    except (ValueError, binascii.Error):
        valid = False
    version = identity.get('version')
    if not valid or not isinstance(version, str) or not version or len(version) > 256:
        raise reviewed_writes.Refusal(
            'MariaDB server_uid and version are required to identify this member. '
            'Use MariaDB 10.11.9 or 11.4.3 or later with an available server_uid.', 'identity_unavailable')
    # MariaDB 10.11.9 sql/sys_vars.cc:3464 and 11.4.3:3498 introduce
    # server_uid (MDEV-34494). mysqld.cc:9967 / :10022 hashes MAC + port.
    # It is stable only while those inputs stay fixed, so keep the complete
    # identity tuple and reject missing/unknown values, never use UID alone.
    extra = ''.join(' OR NOT (BINARY @@global.%s <=> BINARY %s)' % (key, literal(identity[key]))
                    for key in ('server_uid', 'version'))
    return [guard[0][:-2] + extra + ');']


class _IdentityConnection(object):
    """Bind MariaDB review reads to UID in the same evidence query packet."""
    def __init__(self, conn, identity):
        self.conn, self.identity = conn, identity
        self.guard = _session_guard('mariadb', identity)[0]

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def run(self, sql, **kwargs):
        # TargetEvidenceConnection owns framing and expects no rows from SET.
        # Add the guard to its existing packet, preserving packet line numbers.
        head, separator, body = sql.partition('\n')
        if not head.startswith('DELIMITER ') or not separator:
            raise reviewed_writes.Refusal('The member read has no identity packet.', 'identity_unavailable')
        return self.conn.run(head + separator + self.guard + ' ' + body, **kwargs)


def _facts(conn, profile, name=None, value=None, mode=None):
    """One member snapshot, with role and variable reads on one proven node.

    Identify proxy protocols before using SQL evidence packets. Afterwards
    TargetEvidenceConnection refuses a router switching nodes between reads.
    """
    member = topology_group._entry(profile)
    preflight, exclusion = topology_group._identity(conn, member)
    if exclusion is not None:
        raise reviewed_writes.Refusal('Proxy members cannot receive a rolling variable change.', 'proxy')
    maria_identity = None
    if member['engine'] == 'mariadb':
        rows = Reader(conn, '/*myflames:topology*/ ').rows(
            "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('server_uid', 'server_id', 'hostname', 'port', 'version')")
        maria_identity = {row[0].lower(): row[1] for row in rows if len(row) == 2}
        _session_guard('mariadb', maria_identity)
        conn = _IdentityConnection(conn, maria_identity)
    evidence = dbops.TargetEvidenceConnection(conn)
    # Proxy detection must precede database identity packets. Repeat the
    # snapshot inside the scope so role inputs and the separate GTID SELECT
    # have the same node evidence as all subsequent variable reads. GTIDs
    # remain optional data, never fields in the stable server identity.
    initial, exclusion = topology_group._identity(evidence, member)
    if exclusion is not None:
        raise reviewed_writes.Refusal('The member identity changed during the read. Review again.', 'identity_mismatch')
    topology_group._member_facts(evidence, member, initial)
    if member['role'] in ('primary', 'secondary', 'node'):
        # Cluster roles do not exclude an incoming asynchronous channel.
        from .replication import GROUP_REPLICATION_CHANNELS, status_statement
        channels = Reader(evidence, '/*myflames:topology*/ ').named(
            status_statement(member['engine'], _version_tuple(member['version'])))
        member['channels'] = [topology_group._channel_facts(row, member['engine']) for row in channels]
        member['channels'] = [row for row in member['channels'] if row['name'] not in GROUP_REPLICATION_CHANNELS]
    evidence.check()
    observed = evidence.variables or {}
    keys = ('server_id', 'hostname', 'port', 'version') if member['engine'] == 'mariadb' else ('server_uuid',)
    identity = dbops.target_galera_identity(evidence, member['engine'],
                                           {key: observed.get(key) for key in keys}, observed)
    if maria_identity is not None:
        if any(str(maria_identity.get(key)) != str(observed.get(key)) for key in keys):
            raise reviewed_writes.Refusal('The member identity changed during the read.', 'identity_mismatch')
        identity['server_uid'] = maria_identity['server_uid']
    if member['engine'] == 'mariadb' and observed.get('wsrep_node_name'):
        identity['wsrep_node_name'] = observed['wsrep_node_name']
    if (member['engine'] == 'mysql' and not topology_group._uuid(identity.get('server_uuid')) or
            dbops.target_has_galera(observed, member['engine']) and
            not topology_group._uuid(identity.get('wsrep_gcomm_uuid'))):
        raise reviewed_writes.Refusal('The member identity is missing or invalid. Review after it can be read.',
                                     'identity_unavailable')
    _session_guard(member['engine'], identity)
    for key in ('server_uuid',) if member['engine'] == 'mysql' else ('server_id', 'hostname', 'version'):
        if str(preflight.get(key)) != str(observed.get(key)) or str(initial.get(key)) != str(observed.get(key)):
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
        variable = plan.get('variable', {})
        if variable.get('server_identity') != {key: val for key, val in identity.items() if key != 'server_uid'}:
            raise reviewed_writes.Refusal('The member identity changed during the variable read.', 'identity_mismatch')
        values = [variable.get('global_value')]
        if plan['mode'] in ('persist', 'persist_only'):
            values.append((variable.get('persisted') or {}).get('value'))
        truncated = any(parameters.value_truncated(str(val), member['engine']) for val in values if val is not None)
        current = variable.get('global_value')
        if (not truncated and member['engine'] == 'mariadb' and current is not None and
                parameters.value_truncated(str(current), 'mariadb', catalog=True)):
            # describe() can fall back to SYSTEM_VARIABLES (2048 characters)
            # when SHOW has no value (4096 characters). Prove the longer
            # value came from SHOW without adding fields to variable-1.0.
            rows = Reader(evidence, variables.TAG).rows(
                'SHOW GLOBAL VARIABLES WHERE Variable_name = ' + variables._quote_name(name))
            truncated = len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != current
        if truncated:
            raise reviewed_writes.Refusal(
                'The current variable value reaches the source length limit and may be truncated. '
                'A rolling change cannot verify this value.', 'truncated')
    return identity, facts, plan


def _order(entries):
    """One precedence graph combines cluster roles and replica dependencies."""
    def saved(entry):
        return (entry['profile_name'].lower(), entry['profile_name'], entry['profile_id'])

    facts = {entry['profile_id']: entry.get('_facts') or {} for entry in entries}
    identities = {entry['profile_id']: _identity_key(entry.get('identity')) for entry in entries}
    by_uuid, by_id = {}, {}
    for entry in entries:
        identity = entry.get('identity') or {}
        for index, key in ((by_uuid, 'server_uuid'), (by_id, 'server_id')):
            if identity.get(key):
                index.setdefault(str(identity[key]).lower(), []).append(entry['profile_id'])
    parents = {entry['profile_id']: set() for entry in entries}
    gr = any(f.get('role') in ('primary', 'secondary') for f in facts.values())
    async_roles = any(f.get('channels') or f.get('role') == 'source' for f in facts.values())
    galera = any(f.get('role') == 'node' for f in facts.values())
    server_id_match = False
    for pid, fact in facts.items():
        if fact.get('role') == 'secondary':
            for other, peer in facts.items():
                if peer.get('role') == 'primary' and peer.get('gr_group_name') == fact.get('gr_group_name'):
                    parents[pid].add(other)
        for channel in fact.get('channels', []):
            key = channel.get('source_uuid') or channel.get('source_server_id')
            index = by_uuid if channel.get('source_uuid') else by_id
            candidates = index.get(str(key).lower(), []) if key else []
            if candidates:
                if None in {identities[candidate] for candidate in candidates} or len(
                        {identities[candidate] for candidate in candidates}) != 1:
                    raise ValueError('The replication source matches different servers. Review a different member set.')
                parents[pid].update(candidates)
                server_id_match = server_id_match or not channel.get('source_uuid')

    def depth(pid, path):
        if pid in path:
            raise ValueError('A replication cycle or conflicting role constraints have no safe rolling order. '
                             'Review a different member set.')
        return max([1 + depth(parent, path | {pid}) for parent in parents[pid]] or [0])

    depths = {pid: depth(pid, set()) for pid in parents}
    rules = []
    if async_roles:
        rules.append('Asynchronous replicas deepest first, then the root source')
    if gr:
        rules.append('Group Replication and InnoDB Cluster secondaries before their primary')
    if server_id_match:
        rules.append('MariaDB sources matched by server ID')
    if galera:
        rules.append('Galera and PXC ties use node name order')
    rule = '; '.join(rules) + '; ties use saved name order.' if rules else 'Saved name order.'
    return sorted(entries, key=lambda entry: (-depths[entry['profile_id']],
        (facts[entry['profile_id']].get('node_name') or entry['profile_name']).lower() if galera else '',
        saved(entry))), rule


def _member(profile):
    return {'profile_id': profile['id'], 'profile_name': profile.get('name') or profile['id'],
            'host': profile.get('host') or '', 'port': profile.get('port') or 3306,
            'identity': None, 'duplicate_of': None, 'status': 'refused', 'statement': None, 'before': None, 'after': None,
            'effective_mode': None, 'effect': None, 'caveats': [], 'privileges': None, 'config_line': None, 'reason': None}


def _identity_key(identity):
    """Compare complete identities, never saved endpoints or cluster UUIDs."""
    identity = identity or {}
    server = topology_group._uuid(identity.get('server_uuid'))
    if server:
        return ('mysql', server, topology_group._uuid(identity.get('wsrep_gcomm_uuid')))
    fields = ('server_uid', 'server_id', 'hostname', 'port', 'version')
    if all(identity.get(key) is not None for key in fields):
        return ('mariadb', identity['server_uid'], int(identity['server_id']), identity['hostname'],
                int(identity['port']), identity['version'], topology_group._uuid(identity.get('wsrep_gcomm_uuid')))
    return None


def _binding(profile, entry, change=None):
    endpoint = _endpoint(profile)
    return {'profile_id': entry['profile_id'], 'endpoint': endpoint,
            'endpoint_fingerprint': ReviewTokens.fingerprint('endpoint', None, endpoint),
            'identity': entry['identity'], 'statement': entry['statement'], 'before': entry['before'],
            'order_facts': entry.get('_facts'), 'variable_expect': change.get('expect') if change else None}


def plan(store, group_name, active_member_profile_id, name, value, mode, member_ids,
         active_endpoint=None, group_generation=None):
    """Read current members and issue a single immutable review. Never write."""
    group = connection_groups.validate_group(group_name)
    name = variables.check_name(name)
    if param_compare.sensitive(name):
        raise ValueError('Sensitive variables cannot be included in a rolling change.')
    if isinstance(value, str) and value.strip().upper() == 'DEFAULT':
        raise ValueError('DEFAULT cannot be verified across servers; set an explicit value.')
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
    if ((active_endpoint is not None and _endpoint(active) != active_endpoint) or
            (group_generation is not None and generation != group_generation)):
        raise reviewed_writes.Refusal('The validated group or active endpoint changed. Review again.', 'changed_target')
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
    entries, private, proxy_ids = [], {}, set()
    deadline = time.monotonic() + topology_group.REQUEST_SECONDS
    def planning_guard():
        _guard(store, reviewed)
        if deadline - time.monotonic() < topology_group.QUERY_SECONDS:
            raise reviewed_writes.Refusal('The planning time budget expired before this member was read.', 'timeout')

    for profile in members:
        entry = _member(profile)
        try:
            planning_guard()
            with open_group_member(store, group, profile, planning_guard,
                                   {'query_timeout': topology_group.QUERY_SECONDS,
                                    'connect_timeout': topology_group.QUERY_SECONDS}) as opened:
                planning_guard()  # Saved-password and transport waits use the same absolute deadline.
                conn = BoundedConnection(opened[0], seconds=max(0, deadline - time.monotonic()))
                conn.deadline = deadline
                selected = profile['id'] in member_ids
                identity, facts, change = _facts(conn, profile, name if selected else None, value, mode)
                entry.update(identity=identity, _facts=facts)
                if selected:
                    private[profile['id']] = change
                    entry['effective_mode'] = change['mode']
                    entry.update({key: copy.deepcopy(change.get(key)) for key in
                                  ('before', 'effect', 'caveats', 'privileges', 'config_line')})
                    if change['engine'] == 'mariadb':
                        entry['caveats'].append(_NOT_PERSISTED)
                    if (change['mode'] in ('global', 'persist') and
                            change['variable'].get('type') in ('set', 'flagset') and
                            isinstance(value, str) and re.search(r'\bdefault\b', value, re.I)):
                        raise reviewed_writes.Refusal(
                            'DEFAULT values cannot be verified across servers; set explicit values.', 'invalid')
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
            if exc.outcome == 'proxy':
                proxy_ids.add(profile['id'])
        except Exception as exc:
            # Never return server/client text, even if it quotes a statement
            # or a password belonging to another member.
            entry['reason'] = 'Could not plan this member: ' + connection_groups.safe_problem(exc)
        entries.append(entry)
    _guard(store, reviewed)
    ordered, reviewed['order_rule'] = _order(entries)
    if reviewed['order_rule'] != 'Saved name order.':
        # An unselected relay can change the relative depths of selected
        # replicas. Its own live identity and channels are review evidence,
        # even though no variable statement will ever be sent to it. Confirmed
        # proxies and caches do not participate in database replication order.
        dependencies = [entry for entry in ordered if entry['profile_id'] not in member_ids
                        and entry['profile_id'] not in proxy_ids]
        reviewed['expect']['order_dependencies'] = [_binding(
            next(p for p in members if p['id'] == entry['profile_id']), entry) for entry in dependencies]
        if any(not entry.get('_facts') or not entry.get('identity') for entry in dependencies):
            for entry in ordered:
                if entry['profile_id'] in member_ids:
                    entry.update(status='refused', statement=None,
                                 reason='The replication order could not be verified on every group member.')
    seen = {}
    for entry in ordered:
        if entry['profile_id'] not in member_ids:
            continue
        identity_key = _identity_key(entry['identity'])
        if identity_key is not None:
            first = seen.setdefault(identity_key, entry)
            if first is not entry:
                entry.update(status='refused', statement=None, after=None, duplicate_of=first['profile_id'],
                             reason='This saved connection reaches the same server as %s.' % first['profile_name'])
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
    if status == 'unknown' or outcome in ('unknown', 'timeout'):
        return 'The statement may have run. Check this server before reviewing another change.'
    if (status != 'refused' and code is not None and not reviewed_writes.client_error(code) and
            outcome not in ('identity_mismatch', 'identity_unavailable', 'recheck_failed', 'connect_failed',
                            'role_changed', 'dependency_changed', 'changed', 'changed_target', 'changed_variable', 'invalid') and
            not dbops.target_not_connected(error)):
        return 'The server returned error %d. No later member was sent.' % code
    if status == 'refused' and outcome == 'changed':
        outcome = 'changed_target'
    return {'changed': 'The reviewed value was applied.', 'unchanged': 'The requested value is already set.',
            'not_matched': 'The server did not take the exact reviewed value. No later member was sent.',
            'applied_unverified': 'The statement was sent but its effect could not be verified. Check this server.',
            'not_sent': 'Not sent because this rolling change stopped.',
            'invalid_plan': 'Untick the refused servers and review again.',
            'stale': 'The review changed or expired. Review the change again.',
            'needs_confirmation': 'Confirm the reviewed change before applying it.',
            'needs_typed_name': 'Type the connection group name exactly to confirm.',
            'read_only': 'A member of this group is Read-only. Nothing more was sent.',
            'identity_mismatch': 'The server identity changed. Nothing was sent to this member.',
            'identity_unavailable': 'The server identity could not be verified. Nothing was sent to this member.',
            'role_changed': 'The live role or replication source changed. Review the rolling order again.',
            'dependency_changed': 'A server the rolling order depends on changed. Review again.',
            'changed_target': 'The saved member, group or endpoint changed. Review again.',
            'changed_variable': 'The variable changed after the review. Nothing was sent to this member.',
            'policy_changed': 'The group write policy changed. Review again.',
            'recheck_failed': 'The member could not be rechecked. Nothing was sent to this member.',
            'connect_failed': 'Could not connect or log in to this member. Nothing was sent to this member.',
            'timeout': 'The statement may have run. Check this server before reviewing another change.',
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
        self.target.update(change_set_id=reviewed['change_set_id'], group_name=reviewed['group_name'],
                           name=reviewed['name'], mode=member.get('effective_mode') or reviewed['mode'])
        self.variable_plan = None
        self.final_status = self.final_outcome = None

    def attempt(self, statements, target=None, verified=None):
        if self.variable_plan is not None:
            engine = self.variable_plan['engine']
            base = dbops.target_session_guard(engine, self.target['identity'])[0]
            guard = _session_guard(engine, self.target['identity'])[0]
            statements = [guard if statement == base else statement for statement in statements]
        return super(_MemberAudit, self).attempt(statements, target=self.target, verified=verified)

    def complete(self, status, outcome=None, error=None, message=None, **kwargs):
        code = dbops.error_code(str(error)) if error is not None else None
        change = self.variable_plan
        if change is not None and outcome in ('changed', 'unchanged') and kwargs.get('after'):
            matched = variables._matches(change['variable'], change['mode'],
                variables.normalize_value(change['variable'], change['requested']), kwargs['after'])
            if matched is not True:
                status, outcome = 'error', 'not_matched'
        self.final_status, self.final_outcome = status, outcome
        return super(_MemberAudit, self).complete(status, outcome=outcome,
            error='ERROR %d' % code if code is not None else None,
            message=_message(status, outcome, error), **kwargs)

    def outcome(self, statements, status, outcome=None, message=None, error=None, target=None, verified=None):
        code = dbops.error_code(str(error)) if error is not None else None
        return super(_MemberAudit, self).outcome(statements, status, outcome=outcome,
            message=_message(status, outcome, error), error='ERROR %d' % code if code is not None else None,
            target=self.target, verified=verified)


class _SendingConnection(object):
    def __init__(self, conn, before_send, progress, states, index, engine, identity, after_identity=None):
        self.conn, self.before_send = conn, before_send
        self.progress, self.states, self.index = progress, states, index
        self.engine, self.identity = engine, identity
        self.after_identity = after_identity
        self.read_conn = _IdentityConnection(conn, identity) if engine == 'mariadb' else conn
        self.sent = False
        self.failure = None

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def run(self, sql, **kwargs):
        return self.read_conn.run(sql, **kwargs)

    def run_write(self, sql, **kwargs):
        try:
            self.before_send()
        except reviewed_writes.Refusal:
            raise
        except (ConnectorError, connection_groups.ProfileReadError) as exc:
            # This read failed before the write transport was invoked. Do not
            # let variables.apply_change treat its error as the SET's reply.
            raise reviewed_writes.Refusal(_message('refused', 'recheck_failed'), 'recheck_failed') from exc
        base = dbops.target_session_guard(self.engine, self.identity)[0]
        guarded = _session_guard(self.engine, self.identity)[0]
        if sql.count(base) != 1:
            raise reviewed_writes.Refusal('The reviewed identity packet changed.', 'identity_mismatch')
        sql = sql.replace(base, guarded, 1)
        self.states[self.index] = 'sent'
        self.progress.set('sending', self.states)
        self.sent = True
        try:
            answer = self.conn.run_write(sql, **kwargs)
        except ConnectorError as exc:
            self.failure = exc
            if dbops.target_not_connected(exc):
                self.sent = False
                raise reviewed_writes.Refusal(_message('refused', 'connect_failed'), 'connect_failed') from exc
            raise
        if self.after_identity is not None:
            # SET GLOBAL server_id intentionally changes part of MariaDB's
            # tuple. Only an acknowledged write permits the reviewed new ID
            # on readback; the write packet still binds the original tuple.
            self.read_conn = _IdentityConnection(self.conn, self.after_identity)
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
            message = member['reason'] if member['status'] == 'refused' else out['message']
            out['members'].append(dict(member, status='not_sent', after=None, message=message, audit_id=aid))
            if aid:
                out['audit_ids'].append(aid)
        return out

    try:
        _, _, _, safety = _snapshot(store, reviewed['group_name'], reviewed['expect']['active_member_profile_id'])
        if connection is not None and connection.get('read_only') is True:
            safety = dict(safety, read_only=True)
        if connection is not None and (connection.get('group_name') != reviewed['group_name'] or
                connection.get('profile_id') != reviewed['expect']['active_member_profile_id'] or
                _endpoint(connection) != reviewed['expect']['active_endpoint']):
            return refuse_all('refused', 'changed_target')
        if ReviewTokens.fingerprint(OPERATION, None, expect) != ReviewTokens.fingerprint(OPERATION, None, reviewed['expect']):
            return refuse_all('stale', 'stale')
        if any(member['status'] == 'refused' for member in reviewed['members']):
            return refuse_all('refused', 'invalid_plan')
        blocked = dbops.gate(_decision(reviewed['name'], reviewed['mode'], safety, reviewed['group_name']), confirmed)
        if blocked:
            return refuse_all('refused', 'read_only' if blocked[0] == 'refused' else 'needs_confirmation')
        registry.tokens.check(plan_id, OPERATION, reviewed['expect'], details)
        _guard(store, reviewed)
        if reviewed['confirmation']['typed_name'] is not None and typed_name != reviewed['group_name']:
            return refuse_all('refused', 'needs_typed_name')
    except reviewed_writes.Refusal as exc:
        return refuse_all('refused', exc.outcome)
    except (ValueError, TypeError):
        return refuse_all('stale', 'stale')
    with registry.lock:
        # Confirmation and safety failures leave a review usable. Once a run
        # starts, it owns the token even if its first recheck or Stop refuses.
        if plan_id not in registry.plans or progress_id in registry.runs:
            return refuse_all('stale', 'stale')
        try:
            _guard(store, reviewed)
            registry.tokens.check(plan_id, OPERATION, reviewed['expect'], details)
        except reviewed_writes.Refusal as exc:
            return refuse_all('refused', exc.outcome)
        except (ValueError, TypeError):
            return refuse_all('stale', 'stale')
        running = registry.progress.start_member(progress_id, len(reviewed['members']))
        if running is None:
            return refuse_all('refused', 'stale')
        try:
            registry.tokens.check(plan_id, OPERATION, reviewed['expect'], details, consume=True)
        except (ValueError, TypeError):
            with registry.progress._lock:
                registry.progress._items.pop(progress_id, None)
            return refuse_all('stale', 'stale')
        del registry.plans[plan_id]
        run = registry.runs[progress_id] = _Run(reviewed, running)
        # Readers and Stop acquire registry.lock: neither sees half a run.
        registry.prune()
    states = ['waiting'] * len(reviewed['members'])
    stop_reason, applied, uncertain = None, False, False

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
                    running.set('checking', states)
                    with open_group_member(store, reviewed['group_name'], current,
                            lambda: _guard(store, reviewed, binding),
                            {'query_timeout': 30, 'connect_timeout': 10}) as opened:
                        conn = BoundedConnection(opened[0], seconds=120)
                        trail.password = opened[1].get('password')
                        trail.connection = {key: opened[1].get(key) for key in
                                            ('host', 'port', 'user', 'ssh_host', 'ssh_user', 'ssh_port')}

                        def check_target(variable=False):
                            _guard(store, reviewed, binding)
                            dependencies = list(reviewed['expect'].get('order_dependencies', []))
                            later = []
                            if reviewed['order_rule'] != 'Saved name order.':
                                # Later relays and cluster members may change
                                # the current member's reviewed precedence.
                                later = reviewed['expect']['members'][index + 1:]
                                dependencies.extend(later)
                            for dependency in dependencies:
                                candidate = store.get('profile', dependency['profile_id'])
                                if (not isinstance(candidate, dict) or
                                        candidate.get('group') != reviewed['group_name'] or
                                        candidate.get('group_generation') != reviewed['expect']['group_generation'] or
                                        _endpoint(candidate) != dependency['endpoint'] or
                                        dependency['order_facts'] is None or dependency['identity'] is None):
                                    raise reviewed_writes.Refusal(_message('dependency_changed'), 'dependency_changed')
                                try:
                                    peer = _guard(store, reviewed, dependency)
                                    with open_group_member(store, reviewed['group_name'], peer,
                                            lambda: _guard(store, reviewed, dependency),
                                            {'query_timeout': 10, 'connect_timeout': 10}) as dependency_opened:
                                        peer_conn = BoundedConnection(dependency_opened[0], seconds=30)
                                        peer_identity, peer_facts, _ = _facts(peer_conn, peer)
                                        if (peer_identity != dependency['identity'] or
                                                peer_facts != dependency['order_facts']):
                                            raise reviewed_writes.Refusal(_message('dependency_changed'), 'dependency_changed')
                                except (reviewed_writes.Refusal, connection_groups.ProfileReadError, ConnectorError) as exc:
                                    raise reviewed_writes.Refusal(_message('dependency_changed'), 'dependency_changed') from exc
                            identity, facts, _ = _facts(conn, current)
                            if identity != binding['identity']:
                                raise reviewed_writes.Refusal(_message('identity_mismatch'), 'identity_mismatch')
                            if facts != binding['order_facts']:
                                raise reviewed_writes.Refusal(_message('role_changed'), 'role_changed')
                            fresh = None
                            if variable:
                                # Read reviewed scopes after the potentially slow
                                # dependency and role checks, at the send boundary.
                                reader = (_IdentityConnection(conn, identity)
                                          if change['engine'] == 'mariadb' else conn)
                                variable_now = variables.read_variable(reader, reviewed['name'])
                                if any(variable_now.get(key) != binding['variable_expect'].get(key) for key in
                                       ('engine', 'server_version', 'server_identity')):
                                    raise reviewed_writes.Refusal(_message('identity_mismatch'), 'identity_mismatch')
                                fresh = variables.build_plan(variable_now, change['requested'], change['mode'])
                                if fresh.get('status') != 'ready' or fresh.get('expect') != binding['variable_expect']:
                                    raise reviewed_writes.Refusal(_message('changed_variable'), 'changed_variable')
                            _guard(store, reviewed, binding)
                            return fresh

                        change = changes[member['profile_id']]
                        if member['status'] == 'unchanged':
                            fresh = check_target(variable=True)
                            # No statement was reviewed for this member. A
                            # fresh match is required before claiming success.
                            after = {key: fresh['before'].get(key) for key in member['after']}
                            result.update(status='unchanged', after=after, message=_message('unchanged'))
                            trail.outcome([], 'ok', outcome='unchanged')
                            states[index] = 'done'
                        else:
                            check_target()
                            after_identity = (dict(binding['identity'], server_id=change['value'])
                                if change['engine'] == 'mariadb' and reviewed['name'] == 'server_id' and
                                change['mode'] == 'global' else None)
                            sent = _SendingConnection(conn, lambda: check_target(variable=True), running, states, index,
                                                      change['engine'], binding['identity'], after_identity)
                            trail.variable_plan = change
                            answer = variables.apply_change(sent, change,
                                binding['variable_expect'], decision=_decision(reviewed['name'], reviewed['mode'],
                                    safety, reviewed['group_name']), confirmed=True, audit=trail)
                            status = answer['status']
                            matched = answer.get('after') and variables._matches(change['variable'], change['mode'],
                                variables.normalize_value(change['variable'], change['requested']), answer['after']) is True
                            if status in ('changed', 'unchanged') and matched:
                                result['status'] = 'applied' if status == 'changed' else 'unchanged'
                                states[index] = 'done'
                                applied = applied or status == 'changed'
                            else:
                                result['status'] = 'unknown' if status == 'applied_unverified' else 'refused'
                                states[index] = 'unknown' if status == 'applied_unverified' else 'failed' if answer.get('sent') else 'not_sent'
                                stop_reason = 'refused'
                                uncertain = uncertain or bool(answer.get('sent'))
                            after = answer.get('after')
                            result.update(after={key: after.get(key) for key in member['after']} if after else None,
                                message=_message('not_matched' if status in ('changed', 'unchanged') and not matched else status,
                                                 answer.get('outcome'), error=sent.failure))
                except Exception as exc:
                    outcome = getattr(exc, 'identity_outcome', None) or getattr(exc, 'outcome', None)
                    refused = isinstance(exc, (reviewed_writes.Refusal, connection_groups.ProfileReadError))
                    failure = sent.failure if sent is not None and sent.failure is not None else exc
                    unknown = not refused and bool(sent and sent.sent and
                        (trail.final_status == 'unknown' or not trail.finished and dbops.may_have_run(failure)))
                    outcome = outcome or ('changed_target' if refused else 'unknown' if unknown else 'refused')
                    result.update(status='unknown' if unknown else 'refused', message=_message('unknown' if unknown else 'refused', outcome, error=failure))
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
        out['status'] = ('stopped' if stop_reason == 'stopped' else 'partial' if stop_reason and (applied or uncertain)
                         else 'refused' if stop_reason else 'applied')
        out['message'] = ('Every selected member has the reviewed value.' if out['status'] == 'applied' else
                          _message('stopped') if out['status'] == 'stopped' else
                          'The rolling change stopped. Check each member result before reviewing another change.')
        return out
    finally:
        with registry.lock:
            running.set('finished', states, out.get('status') or 'refused')
            with run.lock:
                run.finished = True


def stop(store, progress_id, group_name=None, active_member_profile_id=None):
    """Prevent the next member starting; never interrupt a statement sent."""
    registry = _registry(store)
    registry.progress.get(progress_id)  # Validate the id before looking up a run.
    with registry.lock:
        run = registry.runs.get(progress_id)
    if run is None or group_name is not None and (run.group != group_name):
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
