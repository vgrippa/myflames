# Copyright 2026 Vinicius Grippa / myflames contributors
# SPDX-License-Identifier: Apache-2.0
"""Saved groups authorize reads; topology evidence only suggests membership.

Nothing in this module retrieves credentials or performs a database query.
The decorator scopes every public topology reader, including early returns.
"""
import datetime
import functools
import inspect
import re
import unicodedata


IDENTITY_FIELDS = ('server_uuid', 'gr_group_name', 'cluster_kind', 'cluster_name',
                   'cluster_id', 'clusterset_id', 'clusterset_name', 'wsrep_cluster_uuid', 'wsrep_node_uuid')
CLUSTER_IDENTITY_FIELDS = tuple(key for key in IDENTITY_FIELDS if key != 'server_uuid')
GROUP_MAX_LEN = 80
GROUP_MAX_MEMBERS = 16

#: Strictness order of connection types for a group's effective safety:
#: production first, then staging, development, local; anything else is unset.
_TYPE_STRICTNESS = {'production': 4, 'staging': 3, 'development': 2, 'local': 1}


def strictest_type(types):
    """The strictest connection type in *types*; None when every value is unset."""
    best, best_rank = None, 0
    for value in types or ():
        rank = _TYPE_STRICTNESS.get(value, 0) if isinstance(value, str) else 0
        if rank > best_rank:
            best, best_rank = value, rank
    return best


def group_effective_safety(profiles, group_name):
    """The strictest connection type across the current members of *group_name*.

    Read-only stays each member's own flag; only the environment type is
    shared. Member names and endpoints always come from the store record,
    never from topology evidence.
    """
    return strictest_type(profile.get('type') for profile in profiles or ()
                          if isinstance(profile, dict) and (profile.get('group') or '') == group_name)


def group_primary(members):
    """The member the stored topology identity marks primary or source, or None.

    A member is a replication source when another member's recorded identity
    lists its server UUID as a source. Topology labels never decide this and
    never rename a member.
    """
    members = [m for m in members or () if isinstance(m, dict)]
    sources = set()
    for profile in members:
        identity = profile.get('topology_identity') or {}
        sources.update(identity.get('source_uuids') or ())
    for profile in members:
        uuid = (profile.get('topology_identity') or {}).get('server_uuid')
        if uuid and uuid in sources:
            return profile
    return None


class ProfileReadError(ValueError):
    """An app-built refusal, safe to show without copying database/client text."""


def validate_group(value):
    if not isinstance(value, str) or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value):
        raise ValueError('A connection group must be text without control characters.')
    if len(value) > 200:
        raise ValueError('A connection group must have at most 80 characters.')
    value = value.strip()
    if len(value) > GROUP_MAX_LEN:
        raise ValueError('A connection group must have at most 80 characters.')
    return value


def list_groups(profiles, preferred_order=None):
    """Distinct non-empty connection groups across *profiles*, ordered by name."""
    by_name = {}
    for profile in profiles or ():
        if not isinstance(profile, dict) or not isinstance(profile.get('id'), str):
            continue
        group = (profile.get('group') or '').strip()
        if not group:
            continue
        by_name.setdefault(group, []).append(profile)
    groups = []
    for name in sorted(by_name, key=lambda g: (g.lower(), g)):
        group_profiles = by_name[name]
        order = (preferred_order or {}).get(name)
        if order:
            rank = {pid: idx for idx, pid in enumerate(order)}
            group_profiles = sorted(group_profiles, key=lambda p: rank.get(p['id'], len(rank)))
        ctype = 'production' if any(p.get('type') != 'development' for p in group_profiles) else 'development'
        groups.append({
            'name': name,
            'profile_ids': [p['id'] for p in group_profiles],
            'count': len(group_profiles),
            'type': ctype,
            'read_only': all(bool(p.get('read_only')) for p in group_profiles),
        })
    return groups


def current_profile(profiles, profile_id, current=None):
    if not profile_id:
        return None
    if isinstance(current, dict) and current.get('id') == profile_id:
        return current
    return next((p for p in profiles or () if isinstance(p, dict) and p.get('id') == profile_id), None)


def members(profiles, current):
    group = (current or {}).get('group')
    if not isinstance(group, str) or not group.strip():
        return []
    return [p for p in profiles or () if isinstance(p, dict) and p.get('id')
            and p.get('id') != current.get('id') and p.get('group') == group]


def safe_problem(exc):
    """Never copy a client error: it can contain SQL, credentials or server text."""
    text = str(exc).lower()
    match = re.search(r'(?:after|timeout(?: of)?)\s*(\d+(?:\.\d+)?)\s*s', text)
    if 'timed out' in text or 'timeout' in text:
        return 'timed out after %s s.' % match.group(1) if match else 'timed out.'
    if 'denied' in text or '1045' in text:
        return 'access was denied.'
    if 'budget' in text or 'limit' in text or 'this read' in text or re.search(r'more than \d+ saved connections', text):
        return 'the read budget was reached.'
    if 'password' in text:
        return 'the saved password was unavailable.'
    if 'group' in text or 'consent' in text:
        return 'group membership changed; refresh and try again.'
    return 'the connection could not be read.'


def probe_note(profile, exc):
    return 'Saved connection %s in group %s could not be read: %s' % (
        profile.get('name') or profile.get('id') or 'unnamed', profile.get('group') or '', safe_problem(exc))


def record_read(opener, profile, outcome, message='', identity=None):
    scope = getattr(opener, 'group_scope', None)
    if scope is not None:
        scope.record(profile, outcome, message, identity)


class _ProfileRead(dict):
    """Public read outcome with private evidence from this profile's probe.

    Attributes are deliberately outside the JSON report. In particular, a
    reported peer UUID or an address suggestion cannot supply this evidence.
    """
    def __init__(self, profile):
        super(_ProfileRead, self).__init__(id=profile['id'], name=profile.get('name') or profile['id'],
                                           type=profile.get('type') or 'production')
        self.profile = dict(profile)
        self.identity = {}


class GroupScope(object):
    """A request-local consent snapshot, shared by source and tier discovery."""
    def __init__(self, profiles, current):
        self.current = current
        self.profiles = members(profiles, current)
        self.read = []
        self.attempted = set()

    def record(self, profile, outcome, message='', identity=None):
        if profile.get('id') not in {p['id'] for p in self.profiles}:
            return
        item = next((r for r in self.read if r['id'] == profile['id']), None)
        if item is None:
            item = _ProfileRead(profile)
            self.read.append(item)
        if outcome == 'read' and identity:
            item.identity.update(identity)
        if item.get('outcome') == 'read':
            return
        item.update(outcome=outcome, message=message)

    def opener(self, original):
        if original is None or not self.profiles:
            return None
        allowed = {p['id']: p for p in self.profiles}
        consent = getattr(original, 'consent', None)

        def check(profile):
            prof = {'id': profile} if isinstance(profile, str) else profile
            if not isinstance(prof, dict) or prof.get('id') not in allowed or (
                    'group' in prof and prof.get('group') != self.current.get('group')):
                raise ProfileReadError('This saved connection is outside the current connection group.')
            if callable(consent):
                consent(profile)

        def attach_guard(conn, profile):
            guard = getattr(conn, '_login_guard', None)
            if callable(guard):
                def login_guard():
                    check(profile)
                    guard()
                conn._login_guard = login_guard
            return conn

        def open_profile(profile):
            check(profile)
            self.attempted.add(profile['id'] if isinstance(profile, dict) else profile)
            return attach_guard(original(profile), profile)
        prepare = getattr(original, 'prepare', None)
        if callable(prepare):
            def prepared(profile):
                check(profile)
                fingerprint, connect = prepare(profile)
                def guarded():
                    check(profile)
                    self.attempted.add(profile['id'] if isinstance(profile, dict) else profile)
                    return attach_guard(connect(), profile)
                return fingerprint, guarded
            open_profile.prepare = prepared
        open_profile.check = check
        open_profile.consent = check
        open_profile.group_scope = self
        open_profile.current_profile = self.current
        return open_profile


def grouped_read(function):
    """Scope profiles before any reader code; attach the same report on all returns.

    ``current_profile`` supplies the trusted store snapshot when the caller has
    already removed the current connection from its candidate list.
    """
    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        explicit_current = kwargs.pop('current_profile', None)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        # The replication reader has identity evidence that is not a public
        # report field. Callers may retain this sink for finish_read().
        identity = values.get('_identity')
        if '_identity' in values and identity is None:
            identity = values['_identity'] = {}
        profiles = values.get('profiles') or ()
        opener = values.get('open_profile')
        parent = getattr(opener, 'group_scope', None)
        pid = values.get('current_profile_id')
        current = current_profile(profiles, pid, explicit_current or getattr(opener, 'current_profile', None))
        if current and identity is not None and '_topology_observation' not in identity:
            # Callers without a store-issued token still retain the read's
            # start order, just like saved-peer probes. Never re-date it when
            # finish_read publishes the result.
            from .workspace_store import _topology_probe_observation
            identity['_topology_observation'] = _topology_probe_observation(current)
        scope = parent or GroupScope(profiles, current)
        if 'profiles' in values:
            values['profiles'] = members(profiles, scope.current)
        if 'open_profile' in values:
            values['open_profile'] = opener if parent else scope.opener(opener)
        report = function(*bound.args, **bound.kwargs)
        if isinstance(report, dict):
            # Cluster metadata helpers collect this ID while assembling the
            # report. Remove it at the reader boundary, including early returns.
            cluster_set = report.get('clusterset') or {}
            cluster_set_id = cluster_set.pop('id', None)
            if cluster_set_id and identity is not None:
                identity['clusterset_id'] = cluster_set_id
            report['connection_group'] = describe(report, profiles, scope.current, scope.read,
                values.get('reached_host') or values.get('replica_host'), identity=identity)
        return report
    return wrapped


def identity_from_report(report, extra=None):
    """Extract this connection's identity; None clears a field inspected here.

    A proxy can expose its backend's UUID. It must never become the proxy
    profile's identity, including when an auxiliary query supplied it.
    """
    server = report.get('server') or {}
    products = (report.get('server_product'), report.get('flavor'),
                server.get('product'), server.get('flavor'))
    if (report.get('is_proxysql') or report.get('is_readyset') or report.get('readyset_hint') or
            any(isinstance(value, str) and value.lower() in ('proxysql', 'proxysql admin', 'readyset')
                for value in products)):
        return {}
    cluster = report.get('cluster') or {}
    metadata = report.get('metadata') or {}
    cs = report.get('clusterset') or {}
    kind = report.get('kind') or report.get('cluster_kind')
    cluster_read = report.get('schema_version') in ('cluster-1.0', 'cluster-1.1') or kind in (
        'none', 'group_replication', 'innodb_cluster', 'galera', 'pxc')
    identity = dict.fromkeys(CLUSTER_IDENTITY_FIELDS) if cluster_read else {}
    uuid = server.get('server_uuid') or report.get('server_uuid')
    if uuid:
        identity['server_uuid'] = uuid
    if cluster_read:
        identity.update(cluster_kind=kind if kind and kind != 'none' else None,
                        cluster_name=cluster.get('name'), cluster_id=metadata.get('cluster_id'),
                        clusterset_id=cs.get('id'), clusterset_name=cs.get('name'))
    if kind in ('group_replication', 'innodb_cluster') and metadata.get('local_copy') != 'read_replica':
        identity['gr_group_name'] = cluster.get('uuid')
    if kind in ('galera', 'pxc'):
        identity['wsrep_cluster_uuid'] = cluster.get('uuid')
        identity['wsrep_node_uuid'] = (report.get('galera') or {}).get('gcomm_uuid')
    for key in IDENTITY_FIELDS:
        if (extra or {}).get(key):
            identity[key] = extra[key]
    # Keep absent Cluster fields as explicit clears. Other views leave them
    # out so, for example, a Replication read preserves verified GR evidence.
    if metadata.get('local_copy') == 'read_replica':
        identity['gr_group_name'] = None
    sources = [ch.get('source', {}).get('uuid') for ch in report.get('channels') or ()
               if ch.get('source', {}).get('uuid')]
    if 'channels' in report:
        identity['source_uuids'] = sources
    if identity:
        identity['recorded_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return identity


def _addresses(report):
    """Only addresses named by the open server, not addresses from probed peers."""
    from .topology_links import split_host_port
    rows = list(report.get('members') or ()) + list(report.get('routers') or ())
    rows += list((report.get('metadata') or {}).get('instances') or ())
    rows += [ch.get('source') or {} for ch in report.get('channels') or ()]
    rows += list(report.get('replicas') or ()) + list(report.get('servers') or ())
    rows += list(report.get('upstreams') or ())
    for cluster in (report.get('clusterset') or {}).get('clusters') or ():
        rows += list(cluster.get('instances') or ())
    addresses = []
    for row in rows:
        host, port = row.get('host') or row.get('hostname'), row.get('port')
        if not host:
            host, port = split_host_port(row.get('endpoint') or row.get('address'), 3306)
        if host and port:
            addresses.append((host, port))
        # Router metadata exposes its SQL listeners separately.
        for port in (row.get('ports') or {}).values():
            if host and port:
                addresses.append((host, port))
        for key in ('rw_port', 'ro_port', 'rw_split_port'):
            if host and row.get(key):
                addresses.append((host, row[key]))
    return addresses


def suggested_name(report, current=None):
    cluster = report.get('cluster') or {}
    if report.get('kind') == 'innodb_cluster' and cluster.get('name'):
        name = cluster['name']
    elif (report.get('clusterset') or {}).get('name'):
        name = report['clusterset']['name']
    elif report.get('kind') in ('galera', 'pxc') and cluster.get('name'):
        name = cluster['name']
    elif any(ch.get('source', {}).get('host') for ch in report.get('channels') or ()):
        name = next(ch['source']['host'] for ch in report['channels'] if ch.get('source', {}).get('host')) + ' replication'
    elif report.get('is_proxysql') or report.get('is_readyset'):
        name = (current or {}).get('name') or ''
    else:
        name = ''
    return ''.join(c for c in name if unicodedata.category(c) not in ('Cc', 'Cf', 'Cs')).strip()[:80]


def describe(report, profiles=(), current=None, reads=(), observer_host=None, identity=None):
    from .topology_links import _endpoint_matches_profile, format_endpoint, split_host_port
    identity = identity_from_report(report, identity)
    name = (current or {}).get('group') or ''
    suggestions, joins = [], []
    equalities = [('gr_group_name', 'same_group_replication_group', 'Same Group Replication group'),
                  ('cluster_id', 'same_innodb_cluster', 'Same InnoDB Cluster'),
                  ('clusterset_id', 'same_clusterset', 'Same InnoDB ClusterSet'),
                  ('wsrep_cluster_uuid', 'same_galera_cluster', 'Same Galera cluster')]
    addresses = _addresses(report)
    for profile in profiles or ():
        if profile.get('id') == (current or {}).get('id') or (name and profile.get('group') == name):
            continue
        other = profile.get('topology_identity') or {}
        match = None
        for rank, (key, evidence, label) in enumerate(equalities):
            if identity.get(key) and identity[key] == other.get(key):
                match = (rank, evidence, '%s (%s).' % (label, identity[key]))
                break
        if match is None and other.get('server_uuid') and other['server_uuid'] in identity.get('source_uuids', ()):
            match = (4, 'replication_source', 'This saved connection is a replication source of this server.')
        if match is None and identity.get('server_uuid') and identity['server_uuid'] in other.get('source_uuids', ()):
            match = (5, 'replica_of_this_server', 'This saved connection replicates from this server.')
        if match is None and identity.get('server_uuid') and identity['server_uuid'] == other.get('server_uuid'):
            match = (6, 'same_server', 'Both saved connections reach the same server.')
        endpoint = dict(profile_host=profile.get('host'), profile_port=profile.get('port') or 3306,
                        ssh_host=profile.get('ssh_host'))
        if match is None:
            for host, port in addresses:
                if _endpoint_matches_profile(host, port, endpoint, observer_host):
                    match = (7, 'address_named_by_this_server', 'Its address %s was reported by this server.' % format_endpoint(host, port))
                    break
        if match is None:
            for client in (report.get('topology_tiers') or {}).get('live_footprint', {}).get('clients') or ():
                program = client.get('program', '').lower()
                label = (profile.get('name') or '').lower()
                if not (('proxysql' in program and ('proxy' in label or profile.get('port') in (6032, 6033))) or
                        ('readyset' in program and ('readyset' in label or profile.get('port') in (3307, 6036)))):
                    continue
                host, unused_port = split_host_port(client.get('host'), None)
                if _endpoint_matches_profile(host, endpoint['profile_port'], endpoint, observer_host):
                    match = (7, 'address_named_by_this_server', 'A %s client connected from this saved connection\'s host.' % client['program'])
                    break
        if match is None:
            continue
        rank, evidence, evidence_text = match
        suggestions.append((rank, {'id': profile['id'], 'name': profile.get('name') or profile['id'],
            'type': profile.get('type') or 'production', 'address': format_endpoint(profile.get('host') or '127.0.0.1', profile.get('port') or 3306),
            'group': profile.get('group') or '', 'evidence': evidence, 'evidence_text': evidence_text}))
    suggestions.sort(key=lambda pair: pair[0])
    for _, item in suggestions:
        if item['group'] and item['group'] != name and not any(j['group'] == item['group'] for j in joins):
            joins.append({k: item[k] for k in ('group', 'evidence', 'evidence_text')})
    return {'name': name, 'read': list(reads) if name else [], 'suggestions': [s for _, s in suggestions],
            'join': joins, 'suggested_name': suggested_name(report, current)}


def request_profiles(store, connection):
    """An edited endpoint cannot borrow a saved connection's group consent."""
    from .workspace_store import _same_endpoint
    try:
        profiles = store.list('profile')
        # Also fail closed for an incomplete store adapter. Never infer consent
        # from a request profile ID or an unconfigured mock's return value.
        if not isinstance(profiles, (list, tuple)):
            return [], None
        profiles = [p for p in profiles if isinstance(p, dict) and isinstance(p.get('id'), str)]
        current = current_profile(profiles, (connection or {}).get('profile_id'))
        if current and not _same_endpoint(connection, current):
            current = None
        if current and isinstance(connection, dict) and connection.get('group_name') is not None:
            group_name = validate_group(connection.get('group_name'))
            if not group_name or current.get('group') != group_name:
                current = None
        return profiles, current
    except Exception:
        # Optional saved topology evidence must never fail the open-server read.
        return [], None


def begin_read(store, current):
    """Private identity sink with an observation reserved before the first read.

    Saving optional topology evidence may fail without preventing a status
    read. An invalid token then prevents a later save from acquiring a newer
    timestamp for evidence that was actually collected earlier.
    """
    if not current:
        return {}
    try:
        observation = store.begin_topology_observation()
    except Exception:
        observation = False
    return {'_topology_observation': observation}


def finish_read(store, report, connection, profiles, current, identity=None):
    """Persist direct observations only; suggestions never cause a login."""
    from .replication import reached_host
    reads = (report.get('connection_group') or {}).get('read') or []
    report['connection_group'] = describe(report, profiles, current, reads,
                                          reached_host(connection), identity=identity)
    if current:
        try:
            store.record_topology_identity(current['id'], identity_from_report(report, identity), expected=current,
                                            observation=(identity or {}).get('_topology_observation'))
        except Exception:
            # Saving non-secret evidence is best effort, just like reading it.
            pass
        if any(isinstance(read, _ProfileRead) and read.identity for read in reads):
            # Recheck consent after the probes, and bind each observation to
            # the endpoint it actually read. Revoked or edited profiles cannot
            # gain evidence from another member's report or from a stale read.
            fresh_profiles, fresh_current = request_profiles(store, connection)
            allowed = {p['id']: p for p in members(fresh_profiles, fresh_current)}
            for read in reads:
                if (not isinstance(read, _ProfileRead) or not read.identity or
                        read['id'] not in allowed or read.profile.get('group') != fresh_current.get('group')):
                    continue
                try:
                    store.record_topology_identity(read['id'], read.identity, expected=read.profile)
                except Exception:
                    pass
    return report
