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
                   'cluster_id', 'clusterset_id', 'clusterset_name', 'wsrep_cluster_uuid')
CLUSTER_IDENTITY_FIELDS = tuple(key for key in IDENTITY_FIELDS if key != 'server_uuid')


class ProfileReadError(ValueError):
    """An app-built refusal, safe to show without copying database/client text."""


def validate_group(value):
    if not isinstance(value, str) or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value):
        raise ValueError('A connection group must be text without control characters.')
    value = value.strip()
    if len(value) > 80:
        raise ValueError('A connection group must have at most 80 characters.')
    return value


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


def record_read(opener, profile, outcome, message=''):
    scope = getattr(opener, 'group_scope', None)
    if scope is not None:
        scope.record(profile, outcome, message)


class GroupScope(object):
    """A request-local consent snapshot, shared by source and tier discovery."""
    def __init__(self, profiles, current):
        self.current = current
        self.profiles = members(profiles, current)
        self.read = []
        self.attempted = set()

    def record(self, profile, outcome, message=''):
        if profile.get('id') not in {p['id'] for p in self.profiles}:
            return
        item = next((r for r in self.read if r['id'] == profile['id']), None)
        if item is not None and item['outcome'] == 'read':
            return
        if item is None:
            item = {'id': profile['id'], 'name': profile.get('name') or profile['id'],
                    'type': profile.get('type') or 'production'}
            self.read.append(item)
        item.update(outcome=outcome, message=message)

    def opener(self, original):
        if original is None or not self.profiles:
            return None
        allowed = {p['id']: p for p in self.profiles}

        def check(profile):
            if profile.get('id') not in allowed or profile.get('group') != self.current.get('group'):
                raise ValueError('This saved connection is outside the current connection group.')

        def open_profile(profile):
            check(profile)
            self.attempted.add(profile['id'])
            return original(profile)
        prepare = getattr(original, 'prepare', None)
        if callable(prepare):
            def prepared(profile):
                check(profile)
                fingerprint, connect = prepare(profile)
                def guarded():
                    check(profile)
                    self.attempted.add(profile['id'])
                    return connect()
                return fingerprint, guarded
            open_profile.prepare = prepared
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
    cluster_read = report.get('schema_version') == 'cluster-1.0' or kind in (
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
        return profiles, current
    except Exception:
        # Optional saved topology evidence must never fail the open-server read.
        return [], None


def finish_read(store, report, connection, profiles, current, identity=None):
    """Persist only the current profile; suggestions never cause a login."""
    reads = (report.get('connection_group') or {}).get('read') or []
    report['connection_group'] = describe(report, profiles, current, reads,
                                          connection.get('ssh_host') or connection.get('host'), identity=identity)
    if current:
        try:
            store.record_topology_identity(current['id'], identity_from_report(report, identity), expected=current)
        except Exception:
            # Saving non-secret evidence is best effort, just like reading it.
            pass
    return report
