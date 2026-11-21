"""Explicit, bounded reads of saved connection groups for the Topology place.

Each member owns one standard connector for the request. Every query uses
the connector's guarded client path; no peer address discovery, background
polling or server writes are permitted here.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

from . import connection_groups, dbops
from .connector import ConnectorError
from .group_members import open_group_member
from .perf_common import Reader, now
from .server_info import is_readyset_comment, product_identity, server_version_tuple

SCHEMA_VERSION = 'topology-group-1.0'
SECTIONS = ('members', 'parameters', 'accounts', 'plugins')  # Map gets its own collector in the next slice.
MAX_CONCURRENCY = 4
QUERY_SECONDS = 30
REQUEST_SECONDS = 120
TAG = '/*myflames:topology*/ '
_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


class _NotReached(ConnectorError):
    """The request budget refused a login that has not started."""


def _merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1] + 1:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def parse_mysql_gtids(text):
    """UUID/tag streams as disjoint inclusive intervals, without enumerating IDs.

    MySQL 8.3+: sql/rpl_gtid_set.cc:517-529,823-837 permits several
    tag/interval blocks per UUID. libs/mysql/gtid/tag.cpp:43-47 lowercases
    tags; current source tag.cpp:113-137 constrains them to 32 characters.
    """
    streams = {}
    for block in (text or '').split(','):
        block = block.strip()
        if not block:
            continue
        parts = [p.strip() for p in block.split(':')]
        if not _UUID.fullmatch(parts[0]) or len(parts) < 2:
            raise ValueError('Invalid MySQL GTID set.')
        uuid, tag, interval_seen = parts[0].lower(), '', False
        for part in parts[1:]:
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,31}', part):
                tag, interval_seen = part.lower(), False
                continue
            match = re.fullmatch(r'([0-9]+)(?:-([0-9]+))?', part)
            if not match:
                raise ValueError('Invalid MySQL GTID interval.')
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if not 1 <= start <= end <= 9223372036854775806:
                raise ValueError('Invalid MySQL GTID interval.')
            streams.setdefault((uuid, tag), []).append((start, end))
            interval_seen = True
        if not interval_seen:
            raise ValueError('A MySQL GTID stream needs an interval.')
    return {key: _merge(value) for key, value in streams.items()}


def subtract_mysql_gtids(source, member):
    """Transactions present at the source and absent at this member."""
    result = {}
    for key, intervals in source.items():
        remaining = []
        removed = _merge(member.get(key, ()))
        offset = 0
        for start, end in _merge(intervals):
            cursor = start
            while offset < len(removed) and removed[offset][1] < cursor:
                offset += 1
            index = offset
            while index < len(removed):
                low, high = removed[index]
                if low > end:
                    break
                if low > cursor:
                    remaining.append((cursor, min(end, low - 1)))
                cursor = max(cursor, high + 1)
                if cursor > end:
                    break
                index += 1
            offset = index
            if cursor <= end:
                remaining.append((cursor, end))
        if remaining:
            result[key] = remaining
    return result


def count_mysql_gtids(streams):
    return sum(end - start + 1 for intervals in streams.values() for start, end in intervals)


def parse_mariadb_gtids(text):
    """MariaDB sql/rpl_gtid.cc:1074-1086,1321-1345: domain-server-sequence."""
    result = {}
    for block in (text or '').split(','):
        if not block.strip():
            continue
        match = re.fullmatch(r'\s*([0-9]+)-([0-9]+)-([0-9]+)\s*', block)
        if not match:
            raise ValueError('Invalid MariaDB GTID position.')
        domain, server, sequence = (int(v) for v in match.groups())
        if domain > 4294967295 or server > 4294967295 or sequence > 18446744073709551615 or domain in result:
            raise ValueError('Invalid MariaDB GTID position.')
        result[domain] = {'server_id': server, 'sequence': sequence}
    return result


def mariadb_behind(source_text, member_text):
    source, member = parse_mariadb_gtids(source_text), parse_mariadb_gtids(member_text)
    # gtid_current_pos is a per-domain position, not a transaction set
    # (MariaDB sql/sys_vars.cc:2154-2159). Differences are estimates.
    return {str(domain): max(0, pos['sequence'] - member.get(domain, {}).get('sequence', 0))
            for domain, pos in sorted(source.items())}


def _on(value):
    if value is None:
        return None
    return str(value).upper() in ('ON', '1', 'YES', 'TRUE')


def _number(value):
    try:
        result = float(value)
        if result < 0 or not result < float('inf'):
            return None
        return int(result) if result.is_integer() else result
    except (TypeError, ValueError, OverflowError):
        return None


def _optional(reader, sql, notes, named=False):
    try:
        return reader.named(sql) if named else reader.rows(sql)
    except ConnectorError as exc:
        notes.append(connection_groups.safe_problem(exc))
        return None


def _identity(conn, member):
    """Identify proxies before asking database-specific identity questions."""
    from . import proxysql
    reader = Reader(conn, TAG)
    notes = []
    comment_rows = _optional(reader, 'SELECT @@version_comment', notes)
    comment = comment_rows[0][0] if comment_rows and comment_rows[0] else ''
    if is_readyset_comment(comment):
        from .topology_links import parse_readyset_version
        rows = _optional(reader, 'SHOW READYSET VERSION', notes, named=True)
        version = parse_readyset_version(rows or []) or 'Readyset'
        member.update(engine='mysql', product='Readyset', version=version, version_comment=comment,
                      role='cache', state='reachable')
        return {}, 'Readyset is read for identity only.'
    # The existing admin identity probe verifies admin-version against
    # @@version; a backend exposed through a proxy must not supply its UUID.
    proxy, version = proxysql.identify(conn)
    if proxy:
        member.update(engine='mysql', product='ProxySQL Admin', version=version, version_comment=comment,
                      role='proxy', state='reachable')
        return {}, 'ProxySQL Admin is read for identity only.'
    values = reader.pairs("SHOW GLOBAL VARIABLES WHERE Variable_name IN "
        "('version','version_comment','server_uuid','server_id','hostname','read_only','super_read_only',"
        "'gtid_executed','gtid_mode','gtid_current_pos','group_replication_group_name',"
        "'group_replication_single_primary_mode','wsrep_on','wsrep_auto_increment_control')")
    version, comment = values.get('version'), values.get('version_comment') or comment
    if not version:
        raise ConnectorError('The member identity could not be read.')
    identity = product_identity(version, comment)
    member.update(engine=identity['engine'], product=identity['product'], version=version,
                  version_comment=comment, server_id=_number(values.get('server_id')),
                  hostname=values.get('hostname'), read_only=_on(values.get('read_only')),
                  super_read_only=_on(values.get('super_read_only')) if identity['engine'] == 'mysql' else None)
    uuid = values.get('server_uuid')
    member['server_uuid'] = uuid if identity['engine'] == 'mysql' and isinstance(uuid, str) and _UUID.fullmatch(uuid) else None
    return values, None


def _channel_facts(row, engine):
    """Project only topology facts; status rows also contain login details."""
    uuid = row.get('Source_UUID', row.get('Master_UUID')) if engine == 'mysql' else None
    return {
        'name': row.get('Channel_Name') or row.get('Connection_name') or '',
        'source_host': row.get('Source_Host', row.get('Master_Host')),
        'source_port': _number(row.get('Source_Port', row.get('Master_Port'))),
        'source_uuid': uuid if isinstance(uuid, str) and _UUID.fullmatch(uuid) else None,
        'source_server_id': _number(row.get('Source_Server_Id', row.get('Master_Server_Id'))),
        'io_running': row.get('Replica_IO_Running', row.get('Slave_IO_Running')),
        'sql_running': row.get('Replica_SQL_Running', row.get('Slave_SQL_Running')),
        'seconds_behind': _number(row.get('Seconds_Behind_Source', row.get('Seconds_Behind_Master'))),
    }


def _member_facts(conn, member, variables):
    from .replication import status_statement
    reader, notes = Reader(conn, TAG), []
    engine, version = member['engine'], server_version_tuple(member['version'])
    uuid = member['server_uuid']
    member.update(role=None, state=None, lag=None, behind=None, behind_reason=None)
    position = variables.get('gtid_current_pos') if engine == 'mariadb' else variables.get('gtid_executed')
    # An empty set with GTID_MODE OFF does not show that replication is caught
    # up. Only fully GTID-enabled MySQL members can supply transaction counts.
    if engine == 'mysql' and variables.get('gtid_mode') != 'ON':
        position = None
        notes.append('GTID transaction comparison needs gtid_mode ON on both members.')
    if engine == 'mysql' and uuid:
        # Same own-member filter as ui._member_live_facts, with 5.7 fallback.
        role_column = 'MEMBER_ROLE, MEMBER_STATE' if version >= (8, 0, 0) else 'MEMBER_STATE'
        rows = _optional(reader, 'SELECT ' + role_column +
            ' FROM performance_schema.replication_group_members WHERE MEMBER_ID = @@server_uuid', notes)
        if rows:
            if version >= (8, 0, 0):
                role = str(rows[0][0]).lower()
                member.update(role=role if role in ('primary', 'secondary') else None, state=rows[0][1])
            else:
                primary = _optional(reader, "SHOW GLOBAL STATUS LIKE 'group_replication_primary_member'", notes)
                primary_uuid = primary[0][1] if primary and len(primary[0]) > 1 else None
                # mysql-5.7.44 rapid/plugin/group_replication/src/member_info.cc:
                # 887-890 can return UNDEFINED. Offline/error nodes have no role.
                if not isinstance(primary_uuid, str) or not _UUID.fullmatch(primary_uuid):
                    primary_uuid = None
                role = None
                single = _on(variables.get('group_replication_single_primary_mode'))
                if rows[0][0] in ('ONLINE', 'RECOVERING', 'UNREACHABLE'):
                    if single is False:
                        role = 'primary'
                    elif single and primary_uuid:
                        role = 'primary' if primary_uuid == uuid else 'secondary'
                member.update(role=role, state=rows[0][0])
            # COUNT_TRANSACTIONS_IN_QUEUE is certification, not applier lag.
            # MySQL plugin/group_replication/src/ps_information.cc:284-290;
            # storage/perfschema/table_replication_group_member_stats.cc:161,168.
            rows = _optional(reader, 'SELECT COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE FROM '
                'performance_schema.replication_group_member_stats WHERE MEMBER_ID = @@server_uuid', notes) if version >= (8, 0, 0) else None
            member['lag'] = {'kind': 'gr_applier_queue', 'value': _number(rows[0][0]) if rows else None,
                             'message': 'Transactions waiting in this member\'s applier queue.' if rows else 'The applier queue is unavailable on this member.'}
            return position, notes
    if _on(variables.get('wsrep_on')):
        # MariaDB sql/wsrep_var.cc:1098-1126 exposes provider status;
        # mysql-test/suite/wsrep/r/variables.result:25 names wsrep_local_recv_queue.
        rows = _optional(reader, "SHOW GLOBAL STATUS WHERE Variable_name IN "
                         "('wsrep_local_state_comment','wsrep_local_recv_queue')", notes)
        status = {r[0]: r[1] for r in rows or [] if len(r) > 1}
        member.update(role='node', state=status.get('wsrep_local_state_comment'),
                      lag={'kind': 'galera_recv_queue', 'value': _number(status.get('wsrep_local_recv_queue')),
                           'message': 'Write sets waiting in this node\'s receive queue.'})
        return position, notes
    channels = _optional(reader, status_statement(engine, version), notes, named=True)
    if channels:
        member['channels'] = [_channel_facts(row, engine) for row in channels]
        values = [ch['seconds_behind'] for ch in member['channels']]
        running = [(_on(ch['io_running']), _on(ch['sql_running'])) for ch in member['channels']]
        member.update(role='replica', state='running' if all(io and sql for io, sql in running) else 'stopped',
                      lag={'kind': 'replica_seconds', 'value': max(values) if all(v is not None for v in values) else None,
                           'message': 'Seconds_Behind_Source is an estimate (Seconds_Behind_Master on older MySQL and MariaDB); the largest channel estimate is shown.'})
    elif channels is not None:
        replicas = _optional(reader, 'SHOW REPLICAS' if engine == 'mysql' and version >= (8, 0, 22) else 'SHOW SLAVE HOSTS', notes, named=True)
        member.update(role='source' if replicas else 'standalone' if replicas is not None else None, state='reachable')
        if replicas == []:
            notes.append('No registered replicas were visible; disconnected replicas may be absent.')
    return position, notes


def _primary(entries, profiles, multi_primary=False):
    """Prefer observed GR leadership, then the root of in-group channels."""
    read = [entry for entry in entries if entry['outcome'] == 'read']
    primaries = [entry['profile_id'] for entry in read
                 if entry['role'] == 'primary' and entry['state'] == 'ONLINE']
    ambiguous = 'This group has more than one primary or source.'
    if multi_primary or len(primaries) > 1:
        return None, ambiguous
    if primaries:
        return primaries[0], None

    by_uuid, by_server_id = {}, {}
    for entry in read:
        uuid, server_id = entry['server_uuid'], entry['server_id']
        if uuid:
            by_uuid.setdefault(uuid.lower(), []).append(entry['profile_id'])
        if isinstance(server_id, int) and server_id > 0:
            by_server_id.setdefault(server_id, []).append(entry['profile_id'])
    sources, replicas = set(), set()
    for entry in read:
        for channel in entry['channels']:
            if entry['engine'] == 'mysql':
                matches = by_uuid.get((channel['source_uuid'] or '').lower(), [])
            else:
                matches = by_server_id.get(channel['source_server_id'], [])
            # Unknown or duplicated identities cannot establish an edge. A
            # reported address alone never identifies another saved member.
            if len(matches) != 1 or matches[0] == entry['profile_id']:
                continue
            sources.add(matches[0])
            replicas.add(entry['profile_id'])
    roots = sources - replicas
    if len(roots) > 1:
        return None, ambiguous
    if roots:
        return next(iter(roots)), None
    stored = connection_groups.group_primary(profiles)
    if stored:
        return stored['id'], None
    return None, 'No primary or source member was identified.'


def _plugins(conn, member):
    reader = Reader(conn, TAG)
    # Both engines: sql/sql_show.cc, MySQL:5654-5664, MariaDB:10595-10605.
    rows = reader.rows('SELECT PLUGIN_NAME, PLUGIN_STATUS, PLUGIN_TYPE, PLUGIN_VERSION, '
                       'PLUGIN_LIBRARY, LOAD_OPTION FROM information_schema.PLUGINS')
    items = [dict(name=r[0], kind='plugin', status=r[1], type=r[2], version=r[3], library=r[4], load_option=r[5])
             for r in rows if len(r) == 6]
    if member['engine'] == 'mysql' and server_version_tuple(member['version']) >= (8, 0, 0):
        # mysql-8.0.11 scripts/mysql_system_tables.sql:214 defines component_urn.
        for row in reader.rows('SELECT component_urn FROM mysql.component'):
            if row:
                items.append(dict(name=row[0], kind='component', status='registered', version=None))
    return items


def compare_plugins(snapshots):
    ids = sorted(snapshots)
    indexed = {pid: {(item['kind'], item['name']): item for item in snapshots[pid]} for pid in ids}
    keys = set(key for items in indexed.values() for key in items)
    rows = []
    for kind, name in sorted(keys):
        values = {pid: ({k: indexed[pid][(kind, name)].get(k) for k in ('status', 'version')}
                        if (kind, name) in indexed[pid] else None) for pid in ids}
        present = [value for value in values.values() if value is not None]
        status = ('missing' if len(present) != len(ids) else
                  'different' if any(value != present[0] for value in present[1:]) else 'same')
        rows.append(dict(name=name, kind=kind, status=status, values=values))
    return sorted(rows, key=lambda r: ({'different': 0, 'missing': 1, 'same': 2}[r['status']], r['kind'], r['name']))


def _entry(profile):
    entry = dict(profile_id=profile['id'], profile_name=profile.get('name') or profile['id'],
                 host=profile.get('host') or '', port=profile.get('port') or 3306,
                 safety={'type': profile.get('type') if profile.get('type') in dbops.CONNECTION_TYPES else dbops.DEFAULT_CONNECTION_TYPE,
                         'read_only': bool(profile.get('read_only'))},
                 outcome='not_reached', message='Not read: the time budget did not allow this member to start.', elapsed_s=0)
    entry.update(dict.fromkeys(('engine', 'product', 'version', 'version_comment', 'server_uuid', 'server_id',
                               'hostname', 'read_only', 'super_read_only', 'role', 'state', 'lag', 'behind', 'behind_reason')))
    entry['channels'] = []
    return entry


def read_group(store, group_name, section, active_member_profile_id):
    """Read only the stored members, once each, on an explicit tab request."""
    from .accounts_api import _group_login_guard
    from .workspace_store import ENDPOINT_FIELDS
    from . import param_compare, account_compare
    if not isinstance(section, str) or section not in SECTIONS:
        raise ValueError('Choose a topology section: members, parameters, accounts or plugins.')
    group_name = connection_groups.validate_group(group_name)
    profiles = [p for p in store.list('profile') if isinstance(p, dict) and isinstance(p.get('id'), str)]
    members = [p for p in profiles if group_name and p.get('group') == group_name]
    active = next((p for p in members if p['id'] == active_member_profile_id), None)
    if active is None:
        raise ValueError('This saved connection is outside the current connection group.')
    if len(members) > connection_groups.GROUP_MAX_MEMBERS:
        raise ValueError('A topology read supports at most 16 saved group members.')
    route = dict({key: active.get(key) for key in ENDPOINT_FIELDS}, profile_id=active['id'], group_name=group_name)
    active_guard = _group_login_guard(store, route)
    started, taken_at = time.monotonic(), now()
    deadline = started + REQUEST_SECONDS

    def read_one(profile):
        entry, snapshot, position, exclusion = _entry(profile), None, None, None
        multi_primary = False
        began = time.monotonic()
        # Reserve a complete query window. Queued members never log in with
        # too little time left for their first query, and are never retried.
        if deadline - began < QUERY_SECONDS:
            return entry, snapshot, position, entry['message'], multi_primary
        try:
            active_guard()
            with open_group_member(store, group_name, profile, active_guard,
                                   {'query_timeout': QUERY_SECONDS, 'connect_timeout': QUERY_SECONDS}) as opened:
                member_conn = opened[0]
                consent_guard = member_conn._login_guard

                def login_guard():
                    consent_guard()
                    remaining = deadline - time.monotonic()
                    # The connector rechecks this after transport setup too.
                    if not member_conn._group_login_started and remaining < QUERY_SECONDS:
                        raise _NotReached(entry['message'])
                    if remaining <= 0:
                        raise ConnectorError('The topology read time budget expired.')

                member_conn._login_guard = login_guard
                remaining = deadline - time.monotonic()
                if remaining < QUERY_SECONDS:
                    raise _NotReached(entry['message'])
                conn = param_compare.BoundedConnection(member_conn, seconds=min(remaining, REQUEST_SECONDS))
                variables, exclusion = _identity(conn, entry)
                if exclusion is None:
                    if section == 'members':
                        position, notes = _member_facts(conn, entry, variables)
                        multi_primary = (entry['role'] in ('primary', 'secondary')
                                         and _on(variables.get('group_replication_single_primary_mode')) is False)
                        if notes:
                            entry['message'] = 'Read with unavailable facts: ' + ' '.join(dict.fromkeys(notes))
                    elif section == 'parameters':
                        snapshot = param_compare.capture(conn, server=entry)
                    elif section == 'accounts':
                        snapshot = account_compare.capture(conn, server=entry)
                    elif section == 'plugins':
                        snapshot = _plugins(conn, entry)
                entry['outcome'] = 'read'
                if entry['message'].startswith('Not read:'):
                    entry['message'] = exclusion or 'Read from this member.'
        except _NotReached:
            exclusion = entry['message']
        except Exception as exc:
            entry.update(outcome='failed', message=connection_groups.probe_note(profile, exc))
            snapshot, position, exclusion = None, None, connection_groups.safe_problem(exc)
            multi_primary = False
        entry['elapsed_s'] = round(time.monotonic() - began, 3)
        return entry, snapshot, position, exclusion, multi_primary

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
        results = list(pool.map(read_one, members))
    entries = [result[0] for result in results]
    primary_id, primary_reason = _primary(entries, members, multi_primary=any(result[4] for result in results))
    if section == 'members':
        positions = {entry['profile_id']: position for entry, snapshot, position, reason, multi in results}
        primary_entry = next((entry for entry in entries if entry['profile_id'] == primary_id), None)
        for entry in entries:
            reason = None
            source = positions.get(primary_id)
            current = positions.get(entry['profile_id'])
            if primary_id is None:
                reason = primary_reason
            elif source is None or current is None:
                reason = 'The primary or member GTID position was unavailable.'
            elif primary_entry['engine'] != entry['engine']:
                reason = 'GTID positions from different engines cannot be compared.'
            else:
                try:
                    if entry['engine'] == 'mysql':
                        value = count_mysql_gtids(subtract_mysql_gtids(parse_mysql_gtids(source), parse_mysql_gtids(current)))
                        entry['behind'] = {'kind': 'mysql_gtid_transactions', 'value': value,
                                           'message': 'Transactions in the primary or source sample absent from this member sample.'}
                    else:
                        domains = mariadb_behind(source, current)
                        entry['behind'] = {'kind': 'mariadb_domain_sequence', 'value': sum(domains.values()), 'domains': domains,
                                           'message': 'An estimate from per-domain sequence differences; this is not an exact transaction count.'}
                except ValueError:
                    reason = 'A GTID position could not be parsed safely.'
            entry['behind_reason'] = reason
    summary = {outcome: sum(entry['outcome'] == outcome for entry in entries) for outcome in ('read', 'failed', 'not_reached')}
    summary['versions'] = sorted(set(entry['version'] for entry in entries if entry.get('version')))
    summary['text'] = '%d read, %d failed, %d not reached.' % (summary['read'], summary['failed'], summary['not_reached'])
    report = dict(schema_version=SCHEMA_VERSION, section=section, group_name=group_name, taken_at=taken_at,
                  elapsed_s=round(time.monotonic() - started, 3),
                  effective_safety={'type': connection_groups.group_effective_safety(profiles, group_name) or dbops.DEFAULT_CONNECTION_TYPE,
                                    'read_only': bool(active.get('read_only'))},
                  active_member_profile_id=active['id'], primary_profile_id=primary_id, members=entries, summary=summary)
    if section != 'members':
        snapshots = {entry['profile_id']: snapshot for entry, snapshot, position, reason, multi in results
                     if entry['outcome'] == 'read' and snapshot is not None and reason is None}
        not_compared = [{'profile_id': entry['profile_id'], 'reason': reason or entry['message']}
                        for entry, snapshot, position, reason, multi in results if entry['profile_id'] not in snapshots]
        compare = {'parameters': param_compare.compare_many, 'accounts': account_compare.compare_many,
                   'plugins': compare_plugins}[section]
        report[section] = {'rows': compare(snapshots), 'compared': sorted(snapshots), 'not_compared': not_compared}
    return report
