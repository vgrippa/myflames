"""Explicit, bounded reads of saved connection groups for the Topology place.

Each member owns one standard connector for the request. Every query uses
the connector's guarded client path. Reported peers are drawn without logging
in to them; background polling and server writes are not permitted here.
"""
import contextlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import islice

from . import connection_groups, dbops
from .connector import ConnectorError
from .group_members import open_group_member
from .perf_common import Reader, now
from .server_info import is_readyset_comment, product_identity, server_version_tuple

SCHEMA_VERSION = 'topology-group-1.0'
SECTIONS = ('map', 'members', 'parameters', 'accounts', 'plugins')
MAX_CONCURRENCY = 4
QUERY_SECONDS = 30
DUMP_QUERY_SECONDS = 5
REQUEST_SECONDS = 120
ACCOUNTS_REQUEST_SECONDS = 300
MAP_PEERS_PER_MEMBER = 256
MAP_EDGES_PER_MEMBER = 1024
MAP_ROWS_PER_MEMBER = 2048
MAP_MAX_NODES = 4096
MAP_DUMP_ATTR_ROWS = 4096
MAP_USER_MAX_CHARS = 32
MAP_NOTES_PER_EDGE = 4
TAG = '/*myflames:topology*/ '
_UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')
_DUMP_ATTRS_LOST_SQL = "SHOW GLOBAL STATUS LIKE 'Performance_schema_session_connect_attrs_lost'"
# Collect every Binlog Dump thread on a MySQL source with its replica/slave
# UUID, client host, client user, and ALL connect attributes (never filtering
# with NOT EXISTS so lost or non-mysqld attributes are classified explicitly).
_DUMP_THREADS_SQL = (
    "SELECT DISTINCT t.PROCESSLIST_ID, LOWER(CAST(u.VARIABLE_VALUE AS CHAR(64))), "
    "t.PROCESSLIST_HOST, t.PROCESSLIST_USER, a.ATTR_NAME, a.ATTR_VALUE "
    "FROM performance_schema.threads t "
    "JOIN performance_schema.user_variables_by_thread u "
    "ON u.THREAD_ID = t.THREAD_ID "
    "AND u.VARIABLE_NAME IN ('replica_uuid', 'slave_uuid') "
    "LEFT JOIN performance_schema.session_connect_attrs a "
    "ON a.PROCESSLIST_ID = t.PROCESSLIST_ID "
    "WHERE t.PROCESSLIST_COMMAND LIKE 'Binlog Dump%' "
    "ORDER BY t.PROCESSLIST_ID "
    "LIMIT " + str(MAP_DUMP_ATTR_ROWS + 1)
)
# Active client (host, user) pairs carrying READYSET% connect attributes, used
# only when a Readyset member exposes no upstream user in upstream_servers.
_READYSET_CLIENTS_SQL = (
    "SELECT DISTINCT t.PROCESSLIST_HOST, t.PROCESSLIST_USER "
    "FROM performance_schema.threads t "
    "JOIN performance_schema.session_connect_attrs a "
    "ON a.PROCESSLIST_ID = t.PROCESSLIST_ID "
    "WHERE a.ATTR_NAME IN ('_program_name', 'program_name', '_client_name') "
    "AND a.ATTR_VALUE LIKE 'READYSET%' "
    "AND t.PROCESSLIST_HOST IS NOT NULL AND t.PROCESSLIST_HOST != '' "
    "LIMIT " + str(MAP_PEERS_PER_MEMBER)
)


class _NotReached(ConnectorError):
    """The request budget refused a login that has not started."""


class _ReadConnection(object):
    """Latch login and transport failures so optional probes cannot retry them."""
    def __init__(self, conn):
        self.connection, self.failure = conn, None
        self.authenticated = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def check(self):
        if self.failure is not None:
            raise self.failure

    def _handle_error(self, exc):
        from .reviewed_writes import client_error
        message = str(exc)
        code = dbops.error_code(message)
        # ProxySQL Admin uses 1045 for unsupported SQL, including the
        # initial optional version-comment probe. This is a SQL reply.
        admin_sql_error = code == 1045 and 'proxysql admin error:' in message.lower()
        sql_reply = (admin_sql_error or re.search(r'\bat line \d+\b', message, re.I) or
                     getattr(exc, 'guard_markers', ()))
        # Before any successful statement, a coded failure without SQL
        # position evidence can be a login refusal outside our inventory.
        # Later invocations still log in again, so explicit login failures
        # must also latch after an earlier successful statement.
        login_refused = not sql_reply and (
            (code is not None and not self.authenticated) or
            code in (1040, 1129, 1130, 1251, 1698, 1862, 3118, 3159, 3955, 4151) or
            (code == 1226 and re.search(r'\bmax_user_connections\b', message, re.I)) or
            (code in (1044, 1045, 1049) and
             re.search(r'\bAccess denied for user\b', message, re.I) and
             not re.search(r'\bto database\b', message, re.I)))
        # SQL permission/unsupported-feature errors may be optional. A
        # refused login, client error or timeout must end this member read.
        if code is None or client_error(code) or login_refused:
            self.failure = exc

    def run(self, sql, **kwargs):
        self.check()
        try:
            result = self.connection.run(sql, **kwargs)
            self.authenticated = True
            return result
        except ConnectorError as exc:
            self._handle_error(exc)
            raise

    def server_version(self):
        from .connector import unescape_mysql
        return unescape_mysql(self.run('SELECT @@version').strip().split('\n')[0])


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


def _read_only(value):
    # MariaDB 12.0.1 also has NO_LOCK and NO_LOCK_NO_ADMIN. Both refuse writes.
    return None if value is None else str(value).strip().upper() not in ('OFF', '0', 'FALSE')


def _number(value):
    try:
        result = float(value)
        if result < 0 or not result < float('inf'):
            return None
        return int(result) if result.is_integer() else result
    except (TypeError, ValueError, OverflowError):
        return None


def _optional(reader, sql, notes, named=False, timeout=None):
    try:
        return reader.named(sql) if named else reader.rows(sql, timeout=timeout)
    except ConnectorError as exc:
        notes.append(connection_groups.safe_problem(exc))
        return None


def _identity(conn, member, read_position=True):
    """Identify proxies before asking database-specific identity questions."""
    from . import proxysql
    conn = conn if isinstance(conn, (_ReadConnection, dbops.TargetEvidenceConnection)) else _ReadConnection(conn)
    reader = Reader(conn, TAG)
    notes = []
    comment_rows = _optional(reader, 'SELECT @@version_comment', notes)
    conn.check()
    comment = comment_rows[0][0] if comment_rows and comment_rows[0] else ''
    if is_readyset_comment(comment):
        from .topology_links import parse_readyset_version
        rows = _optional(reader, 'SHOW READYSET VERSION', notes, named=True)
        conn.check()
        version = parse_readyset_version(rows or []) or 'Readyset'
        member.update(engine='mysql', product='Readyset', version=version, version_comment=comment,
                      role='cache', state='reachable')
        return {}, 'Readyset is read for identity only.'
    # The existing admin identity probe verifies admin-version against
    # @@version; a backend exposed through a proxy must not supply its UUID.
    proxy, version = proxysql.identify(conn)
    conn.check()
    if proxy:
        member.update(engine='mysql', product='ProxySQL Admin', version=version, version_comment=comment,
                      role='proxy', state='reachable')
        return {}, 'ProxySQL Admin is read for identity only.'
    values = reader.pairs("SHOW GLOBAL VARIABLES WHERE Variable_name IN "
        "('version','version_comment','server_uuid','server_id','hostname','port','report_host','read_only','super_read_only',"
        "'gtid_mode','performance_schema','group_replication_group_name',"
        "'group_replication_single_primary_mode','wsrep_on','wsrep_auto_increment_control',"
        "'wsrep_provider','wsrep_cluster_name','wsrep_node_name','wsrep_node_incoming_address')")
    version, comment = values.get('version'), values.get('version_comment') or comment
    if not version:
        raise ConnectorError('The member identity could not be read.')
    if is_readyset_comment(comment):
        raise ConnectorError('The member identity changed during the read.')
    identity = product_identity(version, comment)
    member.update(engine=identity['engine'], product=identity['product'], version=version,
                  version_comment=comment, server_id=_number(values.get('server_id')),
                  hostname=values.get('hostname'), read_only_value=values.get('read_only'),
                  read_only=_read_only(values.get('read_only')),
                  super_read_only=_on(values.get('super_read_only')) if identity['engine'] == 'mysql' else None)
    member['server_uuid'] = _uuid(values.get('server_uuid')) if identity['engine'] == 'mysql' else None
    values.pop('gtid_executed', None)
    values.pop('gtid_current_pos', None)
    if read_position:
        # SHOW reads a capped catalog column (MySQL sql/sql_show_status.cc:297;
        # MariaDB sql/sql_show.cc:10528). Even a valid prefix is not a GTID set.
        position_name, sql = (('gtid_current_pos', 'SELECT @@GLOBAL.gtid_current_pos')
                              if identity['engine'] == 'mariadb' else
                              ('gtid_executed', 'SELECT @@GLOBAL.gtid_executed'))
        # XML retains an empty GTID set; the batch reader skips empty rows.
        position = _optional(reader, sql, notes, named=True)
        conn.check()
        values[position_name] = (next(iter(position[0].values()))
                                 if position and len(position) == 1 and len(position[0]) == 1 else None)
    return values, None


def _channel_facts(row, engine):
    """Project only topology facts; status rows also contain login details."""
    uuid = row.get('Source_UUID', row.get('Master_UUID')) if engine == 'mysql' else None
    return {
        'name': row.get('Channel_Name') or row.get('Connection_name') or '',
        'source_host': row.get('Source_Host', row.get('Master_Host')),
        'source_port': _number(row.get('Source_Port', row.get('Master_Port'))),
        'source_uuid': _uuid(uuid),
        'source_server_id': _number(row.get('Source_Server_Id', row.get('Master_Server_Id'))),
        'io_running': row.get('Replica_IO_Running', row.get('Slave_IO_Running')),
        'sql_running': row.get('Replica_SQL_Running', row.get('Slave_SQL_Running')),
        'seconds_behind': _number(row.get('Seconds_Behind_Source', row.get('Seconds_Behind_Master'))),
    }


def _replica_state(channels):
    # MySQL sql/rpl_replica.cc:3545; MariaDB sql/slave.cc:2863-2869.
    if any(str(ch.get('io_running') or '').upper() in ('CONNECTING', 'PREPARING') for ch in channels):
        return 'connecting'
    running = [(_on(ch.get('io_running')), _on(ch.get('sql_running'))) for ch in channels]
    if any(False in pair for pair in running):
        return 'stopped'
    return 'running' if running and all(all(pair) for pair in running) else None


def _read_routed(conn, member):
    """Read router attributes on every section so alias election is consistent."""
    reader, notes = Reader(conn, TAG), []
    is_mysql_db = (member['engine'] == 'mysql' and member.get('product') not in ('ProxySQL Admin', 'Readyset') and
                   member.get('role') not in ('proxy', 'cache'))
    if is_mysql_db:
        rows = _optional(
            reader,
            "SELECT COUNT(*) FROM performance_schema.session_connect_attrs "
            "WHERE PROCESSLIST_ID = CONNECTION_ID() AND ATTR_NAME IN ('_client_ip','_client_socket')",
            notes,
        )
        count = _number(rows[0][0]) if rows and rows[0] else None
        member['routed'] = None if count is None else count > 0
    return notes


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
        role_column = 'MEMBER_ROLE, MEMBER_STATE' if version >= (8, 0, 2) else 'MEMBER_STATE'
        rows = _optional(reader, 'SELECT ' + role_column +
            ' FROM performance_schema.replication_group_members WHERE MEMBER_ID = @@server_uuid', notes)
        # A loaded or stopped plugin can expose its own OFFLINE row without
        # being a member (MySQL ps_information.cc:45-76,96-104).
        own_state = rows[0][1 if version >= (8, 0, 2) else 0] if rows else None
        if rows and own_state != 'OFFLINE':
            if version >= (8, 0, 2):
                role = str(rows[0][0]).lower()
                member.update(role=role if role in ('primary', 'secondary') else None, state=rows[0][1])
            else:
                primary = _optional(reader, "SHOW GLOBAL STATUS LIKE 'group_replication_primary_member'", notes)
                primary_uuid = primary[0][1] if primary and len(primary[0]) > 1 else None
                # mysql-5.7.44 rapid/plugin/group_replication/src/member_info.cc:
                # 887-890 can return UNDEFINED. Offline/error nodes have no role.
                primary_uuid = _uuid(primary_uuid)
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
                'performance_schema.replication_group_member_stats WHERE MEMBER_ID = @@server_uuid', notes) if version >= (8, 0, 2) else None
            member['lag'] = {'kind': 'gr_applier_queue', 'value': _number(rows[0][0]) if rows else None,
                             'message': 'Transactions waiting in this member\'s applier queue.' if rows else 'The applier queue is unavailable on this member.'}
            return position, notes
    if (_on(variables.get('wsrep_on')) or
            variables.get('wsrep_provider') and str(variables['wsrep_provider']).lower() != 'none'):
        # MariaDB sql/wsrep_var.cc:1098-1126 exposes provider status;
        # mysql-test/suite/wsrep/r/variables.result:25 names wsrep_local_recv_queue.
        rows = _optional(reader, "SHOW GLOBAL STATUS WHERE Variable_name IN "
                         "('wsrep_local_state_comment','wsrep_local_recv_queue','wsrep_cluster_status')", notes)
        status = {r[0]: r[1] for r in rows or [] if len(r) > 1}
        variables['wsrep_cluster_status'] = status.get('wsrep_cluster_status')
        member.update(role='node', state=status.get('wsrep_local_state_comment'),
                      lag={'kind': 'galera_recv_queue', 'value': _number(status.get('wsrep_local_recv_queue')),
                           'message': 'Write sets waiting in this node\'s receive queue.'})
        member['behind_reason'] = 'Galera nodes apply write sets from every node; see the receive queue.'
        # MariaDB rpl_gtid.cc:1151-1154 excludes other nodes' binlog GTIDs.
        # wsrep_gtid_mode is OFF by default (sys_vars.cc:6780).
        return None, notes
    channels = _optional(reader, status_statement(engine, version), notes, named=True)
    if channels:
        member['channels'] = [_channel_facts(row, engine) for row in channels]
        values = [ch['seconds_behind'] for ch in member['channels']]
        member.update(role='replica', state=_replica_state(member['channels']),
                      lag={'kind': 'replica_seconds', 'value': max(values) if all(v is not None for v in values) else None,
                           'message': 'Seconds_Behind_Source is an estimate (Seconds_Behind_Master on older MySQL and MariaDB); the largest channel estimate is shown.'})
    elif channels is not None:
        replicas = _optional(reader, 'SHOW REPLICAS' if engine == 'mysql' and version >= (8, 0, 22) else 'SHOW SLAVE HOSTS', notes, named=True)
        member.update(role='source' if replicas else 'standalone' if replicas is not None else None, state='reachable')
        if replicas == []:
            notes.append('No registered replicas were visible; disconnected replicas may be absent.')
    return position, notes


def _replication_links(entries, profiles, facts=None):
    """Live identity links, with False for MariaDB's uncorroborated ID match.

    A server ID is useful display evidence but is not a unique server identity.
    Keep that distinction through source selection and every hop of Behind.
    """
    from . import replication, topology_links
    read = [entry for entry in entries if entry['outcome'] == 'read' and not entry.get('duplicate_of')]
    stored = {profile['id']: profile for profile in profiles}
    candidates = [dict(kind='database', profile_id=entry['profile_id'],
                       profile_host=entry['host'], profile_port=entry['port'],
                       ssh_host=stored[entry['profile_id']].get('ssh_host'),
                       hostname=entry.get('hostname'),
                       port=(facts or {}).get(entry['profile_id'], {}).get('port') or entry['port'],
                       server_uuid=entry.get('server_uuid')) for entry in read
                  if entry['role'] not in ('proxy', 'cache')]
    links, unresolved = {}, False
    for entry in read:
        pid = entry['profile_id']
        for channel in entry['channels']:
            if entry['engine'] == 'mysql':
                uuid = _uuid(channel.get('source_uuid'))
                matches = [other for other in read if uuid and _uuid(other.get('server_uuid')) == uuid]
            else:
                sid = channel.get('source_server_id')
                matches = [other for other in read if isinstance(sid, int) and sid > 0 and
                           other['engine'] == 'mariadb' and other['server_id'] == sid]
            if len(matches) != 1 or matches[0]['profile_id'] == pid:
                unresolved = True
                continue
            source = matches[0]['profile_id']
            verified = entry['engine'] == 'mysql'
            if not verified:
                found, evidence = topology_links.match_database_endpoint(
                    channel.get('source_host'), channel.get('source_port'), candidates,
                    observer_host=replication.reached_host(stored[pid]))
                verified = bool(found and found['profile_id'] == source)
            sources = links.setdefault(pid, {})
            sources[source] = sources.get(source, False) or verified
    return links, unresolved


def _primary(entries, profiles, multi_primary=False):
    """Prefer observed GR leadership, then the root of in-group channels."""
    read = [entry for entry in entries if entry['outcome'] == 'read' and not entry.get('duplicate_of')]
    primaries = [entry['profile_id'] for entry in read
                 if entry['role'] == 'primary' and entry['state'] == 'ONLINE']
    ambiguous = 'This group has more than one primary or source.'
    if multi_primary or len(primaries) > 1:
        return None, ambiguous
    if primaries:
        return primaries[0], None

    links, unresolved = _replication_links(entries, profiles)
    sources = {source for upstreams in links.values() for source in upstreams}
    replicas = set(links)
    roots = sources - replicas
    if len(roots) > 1:
        return None, ambiguous
    if roots:
        return next(iter(roots)), None
    stored = connection_groups.group_primary(profiles)
    if stored:
        if unresolved or links:
            return None, 'The stored source no longer matches the live replication channels.'
        return stored['id'], None
    return None, 'No primary or source member was identified.'


def _related_members(entries, profiles, primary_id, facts):
    """Reachability to the source, preserving any server-ID-only hop."""
    links, unused = _replication_links(entries, profiles, facts)
    related = {primary_id: True} if primary_id else {}
    # GR membership supplies a relationship only when both own rows are live
    # and the group UUID agrees. Configured group names alone are insufficient.
    groups = {}
    for pid, fact in facts.items():
        group = fact.get('gr_group')
        if group and fact.get('state') in ('ONLINE', 'RECOVERING') and fact.get('role') in ('primary', 'secondary'):
            groups.setdefault(group, []).append(pid)
    changed = True
    while changed:
        before = dict(related)
        for peers in groups.values():
            if any(pid in related for pid in peers):
                verified = any(related.get(pid, False) for pid in peers)
                for pid in peers:
                    related[pid] = related.get(pid, False) or verified
        for replica, sources in links.items():
            for source, verified in sources.items():
                if source in related:
                    related[replica] = related.get(replica, False) or (verified and related[source])
        changed = before != related
    for entry in entries:
        if entry.get('duplicate_of') in related:
            related[entry['profile_id']] = related[entry['duplicate_of']]
    return related


def _plugins(conn, member):
    reader = Reader(conn, TAG)
    notes = []
    # Both engines: sql/sql_show.cc, MySQL:5654-5664, MariaDB:10595-10605.
    rows = reader.rows('SELECT PLUGIN_NAME, PLUGIN_STATUS, PLUGIN_TYPE, PLUGIN_VERSION, '
                       'PLUGIN_LIBRARY, LOAD_OPTION FROM information_schema.PLUGINS')
    items = [dict(name=r[0], kind='plugin', status=r[1], type=r[2], version=r[3], library=r[4], load_option=r[5])
             for r in rows if len(r) == 6]
    if member['engine'] == 'mysql' and server_version_tuple(member['version']) >= (8, 0, 0):
        # mysql-8.0.11 scripts/mysql_system_tables.sql:214 defines component_urn.
        notes.append('mysql.component lists components installed with INSTALL COMPONENT; '
                     'components loaded from a manifest file are not listed.')
        component_notes = []
        rows = _optional(reader, 'SELECT component_urn FROM mysql.component', component_notes)
        notes.extend('mysql.component: ' + note for note in component_notes)
        for row in rows or []:
            if row:
                items.append(dict(name=row[0], kind='component', status='registered', version=None))
    return {'items': items, 'notes': notes}


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


def _uuid(value):
    return (value.lower() if isinstance(value, str) and _UUID.fullmatch(value)
            and value != '00000000-0000-0000-0000-000000000000' else None)


def _classify_dump_attrs(attrs, attrs_lost, attrs_unreadable=False):
    """Classify one Binlog Dump session from its full connect attributes."""
    has_mysqld = any(
        (name in ('program_name', '_program_name') and (value or '').strip().lower() == 'mysqld')
        or (name == '_client_role' and (value or '').strip().lower() == 'binary_log_listener')
        for name, value in attrs
    )
    if has_mysqld:
        return 'mysqld'
    if attrs_unreadable or attrs_lost is None or attrs_lost > 0:
        return 'unknown'
    if not attrs:
        return 'none'
    return 'other'


def _read_dump_threads(conn, reader, variables):
    """Collect and classify Binlog Dump sessions on a MySQL source (5 s budget)."""
    if variables.get('performance_schema') is not None and _on(variables.get('performance_schema')) is False:
        return [], [], True
    dump_notes = []
    lost_rows = _optional(reader, _DUMP_ATTRS_LOST_SQL, dump_notes, timeout=DUMP_QUERY_SECONDS)
    conn.check()
    attrs_lost = None
    if lost_rows:
        for row in lost_rows:
            if len(row) >= 2 and str(row[0] or '').lower() == 'performance_schema_session_connect_attrs_lost':
                val = _number(row[1])
                if isinstance(val, int):
                    attrs_lost = val
                break
    rows = _optional(reader, _DUMP_THREADS_SQL, dump_notes, timeout=DUMP_QUERY_SECONDS)
    conn.check()
    if rows is None:
        return [], [], True
    truncated = len(rows) > MAP_DUMP_ATTR_ROWS
    if truncated:
        rows = rows[:MAP_DUMP_ATTR_ROWS]
    last_pid = str(rows[-1][0]) if truncated and rows else None

    sessions = {}
    order = []
    for row in rows:
        if len(row) < 4:
            continue
        pid_key = str(row[0]) if row[0] is not None else ''
        duuid = _uuid(row[1])
        dhost = _map_host(row[2])
        if not duuid or not dhost:
            continue
        duser = _map_user(row[3])
        key = (pid_key, duuid, dhost, duser)
        if key not in sessions:
            if len(order) >= MAP_PEERS_PER_MEMBER:
                continue
            sessions[key] = {'uuid': duuid, 'host': dhost, 'user': duser, 'attrs': [],
                             'unreadable': bool(truncated and pid_key == last_pid)}
            order.append(key)
        attr_name = row[4] if len(row) > 4 else None
        attr_value = row[5] if len(row) > 5 else None
        if attr_name is not None:
            sessions[key]['attrs'].append((str(attr_name).strip().lower(),
                                           None if attr_value is None else str(attr_value)))

    dump_threads = []
    has_none_class = False
    for key in order:
        item = sessions[key]
        dclass = _classify_dump_attrs(item['attrs'], attrs_lost, attrs_unreadable=item['unreadable'])
        if dclass == 'none':
            has_none_class = True
        dump_threads.append({
            'uuid': item['uuid'],
            'host': item['host'],
            'user': item['user'],
            'class': dclass,
        })

    readyset_clients = []
    if has_none_class:
        client_rows = _optional(reader, _READYSET_CLIENTS_SQL, dump_notes, timeout=DUMP_QUERY_SECONDS)
        conn.check()
        for row in (client_rows or [])[:MAP_PEERS_PER_MEMBER]:
            if len(row) < 2:
                continue
            chost = _map_host(row[0])
            cuser = _map_user(row[1])
            if chost and cuser:
                readyset_clients.append({'host': chost, 'user': cuser})
    dump_read_failed = bool(attrs_lost is None and not dump_threads)
    return dump_threads, readyset_clients, dump_read_failed


def _map_probe(conn, profile, member, variables, has_readyset=False):
    """Collect topology on this member only; keep the raw probe private.

    The shared probe supplies routes and peers. Cluster identity comes from
    this connection too, never from another node's membership list or a
    remembered saved-connection identity.
    """
    from . import cluster, topology_links
    probe = topology_links._probe_connection(conn, profile)
    notes, reader = [], Reader(conn, TAG)
    expected = ('readyset' if member['product'] == 'Readyset' else
                'proxysql' if member['product'] == 'ProxySQL Admin' else 'database')
    if probe['kind'] != expected:
        raise ConnectorError('The member identity changed during the topology read.')
    if probe['kind'] == 'proxysql':
        # runtime_mysql_servers collapses the pool's replication-lag status
        # to SHUNNED (ProxySQL MySQL_HostGroups_Manager.h). Match all three
        # endpoint fields so the same server in another hostgroup cannot
        # change this route's state. Configuration fallback proves no health.
        pool = _optional(reader, 'SELECT hostgroup, srv_host, srv_port, status FROM '
                         'stats_mysql_connection_pool LIMIT %d' % (MAP_ROWS_PER_MEMBER + 1), notes)
        statuses = {(str(r[0]), str(r[1]), str(r[2])): r[3]
                    for r in (pool or [])[:MAP_ROWS_PER_MEMBER] if len(r) >= 4}
        for backend in probe.get('servers', []):
            if backend.get('status_source') == 'config':
                continue
            key = tuple(str(backend.get(k)) for k in ('hostgroup_id', 'hostname', 'port'))
            status = statuses.get(key)
            if (backend.get('status') in ('ONLINE', 'SHUNNED') and
                    status in ('ONLINE', 'SHUNNED', 'SHUNNED_REPLICATION_LAG', 'SHUNNED_AWS_BGD',
                               'OFFLINE_SOFT', 'OFFLINE_HARD')):
                backend['status'] = status
        offline = set()
        for table in ('runtime_mysql_group_replication_hostgroups', 'runtime_mysql_galera_hostgroups'):
            rows = _optional(reader, 'SELECT offline_hostgroup FROM %s WHERE active = 1 LIMIT %d'
                             % (table, MAP_ROWS_PER_MEMBER + 1), notes)
            offline.update(_number(r[0]) for r in (rows or [])[:MAP_ROWS_PER_MEMBER] if r)
        probe['offline_hostgroups'] = [hg for hg in offline if isinstance(hg, int)]
    if probe['kind'] == 'readyset':
        # Readyset forwards SELECT @@server_uuid and SELECT @@hostname, @@port
        # to the MySQL upstream that serves queries. Keep them private to the
        # probe; never store them as the Readyset member's own identity.
        proxied_notes = []
        rows = _optional(reader, 'SELECT @@server_uuid', proxied_notes)
        conn.check()
        probe['proxied_server_uuid'] = _uuid(rows[0][0]) if rows and rows[0] else None
        probe['proxied_hostname'] = None
        probe['proxied_port'] = None
        if probe['proxied_server_uuid']:
            hp_rows = _optional(reader, 'SELECT @@hostname, @@port', proxied_notes)
            conn.check()
            if hp_rows and hp_rows[0] and len(hp_rows[0]) >= 2:
                probe['proxied_hostname'] = _map_host(hp_rows[0][0])
                probe['proxied_port'] = _map_port(hp_rows[0][1])
    if probe['kind'] != 'database':
        return probe, notes
    if (_uuid(probe.get('server_uuid')) and member['server_uuid'] and
            _uuid(probe['server_uuid']) != _uuid(member['server_uuid'])):
        raise ConnectorError('The member identity changed during the topology read.')
    if has_readyset and member['engine'] == 'mysql' and probe.get('replicas'):
        dump_threads, readyset_clients, dump_read_failed = _read_dump_threads(conn, reader, variables)
        probe['dump_threads'] = dump_threads
        probe['readyset_clients'] = readyset_clients
        probe['dump_read_failed'] = dump_read_failed
    group = _uuid(variables.get('group_replication_group_name'))
    provider = variables.get('wsrep_provider')
    wsrep = _on(variables.get('wsrep_on')) or (provider and str(provider).lower() != 'none')
    if wsrep:
        kind = 'pxc' if member['product'] == 'Percona XtraDB Cluster' else 'galera'
        if probe.get('cluster_kind') not in ('galera', 'pxc'):
            # A loaded provider can remain connected while wsrep_on is OFF.
            # The shared probe only reads this table for wsrep_on ON.
            rows = _optional(reader, topology_links._WSREP_MEMBERS_SQL, notes)
            probe['cluster_members'] = []
            for row in rows or []:
                if len(row) >= 3:
                    host, port = topology_links.split_host_port(row[2], 3306)
                    probe['cluster_members'].append(dict(uuid=row[0], name=row[1], host=host, port=port))
        status = _optional(reader, "SHOW GLOBAL STATUS WHERE Variable_name IN "
                           "('wsrep_cluster_state_uuid','wsrep_gcomm_uuid','wsrep_cluster_status')", notes)
        values = {r[0]: r[1] for r in status or [] if len(r) > 1}
        probe.update(cluster_kind=kind, cluster_name=variables.get('wsrep_cluster_name'),
                     cluster_identity=_uuid(values.get('wsrep_cluster_state_uuid')),
                     wsrep_node_uuid=_uuid(values.get('wsrep_gcomm_uuid')),
                     wsrep_cluster_status=values.get('wsrep_cluster_status'))
        if kind == 'pxc':
            rows = _optional(reader, cluster.PXC_VIEW_SQL, notes)
            members = probe.setdefault('cluster_members', [])
            for row in rows or []:
                if len(row) >= 2:
                    # HOST_NAME is a node name, not a SQL endpoint. Preserve
                    # wsrep_cluster_members incoming addresses (cluster.py:1013).
                    found = next((m for m in members if _uuid(m.get('uuid')) and
                                  _uuid(m['uuid']) == _uuid(row[1])), None)
                    if found is None:
                        found = next((m for m in members if m.get('name') == row[0]), None)
                    if found is not None:
                        found['uuid'] = found.get('uuid') or row[1]
                    else:
                        host, port = row[0], None
                        if row[0] == variables.get('wsrep_node_name'):
                            incoming, incoming_port = topology_links.split_host_port(
                                variables.get('wsrep_node_incoming_address'), 3306)
                            if incoming and incoming.upper() != 'AUTO':
                                host, port = incoming, incoming_port
                        members.append({'uuid': row[1], 'name': row[0], 'host': host, 'port': port})
    elif group:
        probe['cluster_identity'] = group
        if server_version_tuple(member['version']) < (8, 0, 2):
            rows = _optional(reader, 'SELECT MEMBER_ID, MEMBER_HOST, MEMBER_PORT, MEMBER_STATE '
                             'FROM performance_schema.replication_group_members', notes)
            probe['cluster_members'] = [dict(uuid=r[0], host=r[1], port=_number(r[2]), state=r[3])
                                        for r in rows or [] if len(r) >= 4]
        if probe.get('cluster_kind') == 'innodb_cluster':
            rows = _optional(reader, cluster._MD_CLUSTERS_SQL, notes)
            own = [r for r in rows or [] if len(r) > 3 and _uuid(r[3]) == group]
            if len(own) == 1:
                probe['cluster_name'] = own[0][1]
    return probe, notes


def _map_host(value):
    """Accept endpoint text, never a URL, login string or arbitrary SQL."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 255:
        return None
    return value if re.fullmatch(r'[A-Za-z0-9._:%\[\]-]{1,255}', value) else None


def _map_user(value):
    """Accept bounded MySQL account names (up to 32 characters)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAP_USER_MAX_CHARS:
        return None
    return value


def _map_port(value):
    number = _number(value)
    return number if isinstance(number, int) and 1 <= number <= 65535 else None


def _is_truthy(value):
    return value is not None and str(value).strip().lower() in ('1', 'true', 't', 'yes', 'on')


def _attach_replica_note(edges, edge_key, source_name, server_id, estimate=False, note_state=None):
    """Attach a bounded replica-registration note onto a Readyset upstream edge."""
    target_edge = edges.get(edge_key)
    if target_edge is None:
        return
    sid = _number(server_id)
    if isinstance(sid, int) and sid > 0:
        note = 'Also registered on %s as a replica (server ID %d)' % (source_name, sid)
    else:
        note = 'Also registered on %s as a replica' % source_name
    if estimate:
        note += ' (estimate)'
    if note_state is None:
        note_state = {}
    state = note_state.get(edge_key)
    if state is None:
        existing = target_edge.get('reason')
        state = {
            'notes': [existing] if existing else [],
            'seen': {existing} if existing else set(),
            'extra': 0,
        }
        note_state[edge_key] = state
    if note in state['seen']:
        return
    state['seen'].add(note)
    if len(state['notes']) < MAP_NOTES_PER_EDGE:
        state['notes'].append(note)
    else:
        state['extra'] += 1
    reason = '; '.join(state['notes'])
    if state['extra']:
        reason += '; and %d more registration%s' % (state['extra'], '' if state['extra'] == 1 else 's')
    target_edge['reason'] = reason


def _build_map(entries, probes, profiles):
    """Project only display facts and join them using explicit evidence.

    Saved endpoints may establish an address match even for a failed member.
    Only successful own-connection reads supply identities. Cluster boxes
    additionally require each saved member to attest its own membership.
    """
    from . import replication, topology_links
    by_id = {entry['profile_id']: entry for entry in entries}
    stored = {profile['id']: profile for profile in profiles}
    nodes = [dict(id=entry['profile_id'], kind='member', profile_id=entry['profile_id'],
                  label=entry['profile_name'], cluster_id=None, duplicate_of=entry.get('duplicate_of'),
                  **{key: entry[key] for key in ('host', 'port', 'engine', 'product', 'version',
                                                'role', 'state', 'outcome', 'server_uuid')}) for entry in entries]
    node_index = {node['id']: node for node in nodes}
    peers, edges, boxes, notes = {}, {}, {}, []
    peer_counts, edge_counts, capped = {}, {}, set()

    def cap(observer, message):
        key = (observer, message)
        if key not in capped:
            capped.add(key)
            notes.append({'profile_id': observer, 'message': message})

    # Bound traversal as well as the output: repeated/invalid rows must not
    # consume unbounded CPU after the visible map has reached its limits.
    bounded = {}
    for pid, probe in probes.items():
        remaining = MAP_ROWS_PER_MEMBER
        bounded[pid] = dict(probe)
        for key in ('cluster_members', 'channels', 'replicas', 'servers', 'upstreams'):
            rows = list(islice(iter(probe.get(key) or []), remaining + 1))
            if len(rows) > remaining:
                cap(pid, 'Map rows capped at %d per member; some reported topology is omitted.' % MAP_ROWS_PER_MEMBER)
                if key == 'servers':
                    bounded[pid]['servers_incomplete'] = True
                rows = rows[:remaining]
            bounded[pid][key] = rows
            remaining -= len(rows)
        for aux_key in ('dump_threads', 'readyset_replicas', 'readyset_clients'):
            if aux_key in probe:
                bounded[pid][aux_key] = list(islice(iter(probe.get(aux_key) or []), MAP_PEERS_PER_MEMBER))
    probes = bounded
    candidates, galera_candidates = [], []
    for entry in entries:
        pid = entry['profile_id']
        own = probes.get(pid, {})
        candidate = dict(own, kind='database', profile_id=pid, profile_host=entry['host'],
                         profile_port=entry['port'], ssh_host=stored[pid].get('ssh_host'),
                         server_uuid=_uuid(entry['server_uuid']) if own.get('kind') == 'database' else None,
                         duplicate_of=entry.get('duplicate_of'))
        candidates.append(candidate)
        if own.get('wsrep_node_uuid'):
            galera_candidates.append(dict(candidate, server_uuid=_uuid(own['wsrep_node_uuid'])))
        if entry['outcome'] != 'read':
            notes.append({'profile_id': pid, 'message': entry['message']})

    def peer(host, port, uuid, observer, server_id=None, is_replica=False):
        sid = _number(server_id)
        sid = sid if isinstance(sid, int) and sid > 0 else None
        if not uuid and not host and not (is_replica and sid is not None):
            return None
        if host:
            label = topology_links.format_endpoint(host, port)
        else:
            details = []
            if is_replica and sid is not None:
                details.append('server ID %d' % sid)
            if port is not None:
                details.append('port %d' % port)
            base = 'Replica without a reported address' if is_replica else 'Server without a reported address'
            label = '%s (%s)' % (base, ', '.join(details)) if details else base
        if uuid:
            key = 'peer:' + uuid
        elif host:
            key = 'peer:' + topology_links.format_endpoint(host, port).lower()
        else:
            key = 'peer:%s:server-id:%d' % (observer, sid) + (':%d' % port if port is not None else '')
        if key not in peers:
            if peer_counts.get(observer, 0) >= MAP_PEERS_PER_MEMBER:
                cap(observer, 'Map peers capped at %d per member; some reported peers are omitted.' % MAP_PEERS_PER_MEMBER)
                return None
            if len(nodes) + len(peers) >= MAP_MAX_NODES:
                cap(observer, 'Map nodes capped at %d; some reported peers are omitted.' % MAP_MAX_NODES)
                return None
            peer_counts[observer] = peer_counts.get(observer, 0) + 1
            peers[key] = dict(id=key, kind='peer', profile_id=None, label=label,
                              host=host, port=port, server_uuid=uuid, seen_by=[], reason='Not in this group')
        elif not peers[key]['host'] and host:
            peers[key]['host'] = host
            peers[key]['port'] = port
            peers[key]['label'] = label
        if observer not in peers[key]['seen_by']:
            peers[key]['seen_by'].append(observer)
        return key

    target_cache = {}

    def target(observer, host, port, uuid=None, server_id=None, identity_only=False, galera=False,
               allow_peer=True, database_only=False):
        host, port, uuid = _map_host(host), _map_port(port), _uuid(uuid)
        sid = _number(server_id)
        cache_key = None
        if not allow_peer:
            cache_key = (observer, host, port, uuid, sid, identity_only, galera, database_only)
            if cache_key in target_cache:
                return target_cache[cache_key]

        def _finish(result):
            if cache_key is not None:
                target_cache[cache_key] = result
            return result

        matching = galera_candidates if galera else candidates
        if database_only:
            matching = [candidate for candidate in matching
                        if probes.get(candidate['profile_id'], {}).get('kind') not in ('readyset', 'proxysql')]
        observer_host = replication.reached_host(stored[observer])
        # Aliases attest the canonical server's identity, never a second
        # server. Address-only routes can still name their saved endpoint.
        if uuid:
            matching = [candidate for candidate in matching if not candidate.get('duplicate_of')]
            found, evidence = topology_links.match_database_endpoint(
                host, port, matching, uuid=uuid, observer_host=observer_host)
        else:
            # Resolve saved endpoints before considering reported hostnames.
            # A hostname fallback must not bypass tied saved endpoints, even
            # when their aliases all represent the same canonical server.
            found, evidence = None, None
            matches = []
            for candidate in matching:
                match, reason = topology_links.match_database_endpoint(
                    host, port, [candidate], observer_host=observer_host)
                if match is not None:
                    matches.append((match, reason))
            saved_matches = [(match, reason) for match, reason in matches if reason == 'endpoint']
            if saved_matches:
                matches = saved_matches
            canonical_ids = {match.get('duplicate_of') or match['profile_id'] for match, _ in matches}
            if len(canonical_ids) == 1:
                canonical_id = next(iter(canonical_ids))
                resolved = next(((match, reason) for match, reason in matches
                                 if match['profile_id'] == canonical_id), None)
                if resolved is None:
                    if len(saved_matches) == 1:
                        resolved = saved_matches[0]
                if resolved is not None:
                    found, evidence = resolved
        if evidence == 'server_uuid':
            return _finish((found['profile_id'], evidence, True))
        # IDs can collide (including the default 1), so retain an ID-only
        # match as an explicit estimate, not verified identity evidence.
        mariadb_sid = not uuid and isinstance(sid, int) and sid > 0 and by_id[observer]['engine'] == 'mariadb'
        if mariadb_sid:
            identities = [pid for pid, probe in probes.items() if probe.get('kind') == 'database'
                          and by_id[pid]['engine'] == 'mariadb' and by_id[pid]['server_id'] == sid]
            if len(identities) == 1:
                return _finish((identities[0], 'server_id', bool(found and found['profile_id'] == identities[0])))
        # A contrary UUID is stronger than a matching address. Do not draw
        # that known different server as this saved member.
        contrary_uuid = found and uuid and found.get('server_uuid') and _uuid(found['server_uuid']) != uuid
        contrary_sid = (found and mariadb_sid and
                        probes.get(found['profile_id'], {}).get('kind') == 'database' and
                        by_id[found['profile_id']].get('server_id') is not None and
                        by_id[found['profile_id']]['server_id'] != sid)
        if found and not identity_only and not contrary_uuid and not contrary_sid:
            return _finish((found['profile_id'], evidence, False))
        if not allow_peer:
            return _finish((None, None, False))
        return peer(host, port, uuid, observer), None, False

    def edge(observer, source, destination, kind, evidence, verified, state, label, reason=None, target_key=('', 0)):
        if not source or not destination or source == destination:
            return None
        key = (source, destination, kind, '' if kind == 'replication' else label, target_key)
        if kind == 'replication' and evidence == 'server_id' and not verified:
            label = 'server ID match, not verified'
        value = dict(**{'from': source, 'to': destination}, kind=kind, evidence=evidence,
                     verified=verified, state=state, label=label)
        if reason:
            value['reason'] = reason
        old = edges.get(key)
        if old is None:
            if edge_counts.get(observer, 0) >= MAP_EDGES_PER_MEMBER:
                cap(observer, 'Map edges capped at %d per member; some reported links are omitted.' % MAP_EDGES_PER_MEMBER)
                return None
            edge_counts[observer] = edge_counts.get(observer, 0) + 1
        if old is None or verified and not old['verified']:
            if old is not None and state is None:
                value['state'] = old['state']
            if old is not None and 'reason' not in value and 'reason' in old:
                value['reason'] = old['reason']
            edges[key] = value
        else:
            if old['state'] is None and state is not None:
                old['state'] = state
            if reason and 'reason' not in old:
                old['reason'] = reason
        return key

    attestations, observations = {}, {}
    for pid, probe in probes.items():
        if by_id[pid].get('duplicate_of'):
            continue
        kind = probe.get('cluster_kind')
        cluster_uuid = _uuid(probe.get('cluster_identity'))
        galera = kind in ('galera', 'pxc')
        if kind not in ('group_replication', 'innodb_cluster', 'galera', 'pxc'):
            continue
        if cluster_uuid:
            key = ('wsrep:' if galera else 'gr:') + cluster_uuid
            observations[pid] = key
            if key not in boxes:
                boxes[key] = dict(id=key, kind=kind, name=probe.get('cluster_name') or cluster_uuid,
                                  members=[], seen_from=[])
            elif kind == 'innodb_cluster':
                boxes[key].update(kind=kind, name=probe.get('cluster_name') or cluster_uuid)
            boxes[key]['seen_from'].append(pid)
        own_uuid = _uuid(probe.get('wsrep_node_uuid') if galera else by_id[pid]['server_uuid'])
        # A view must be Primary for wsrep; an own GR row must be active.
        # MariaDB wsrep_server_service.cc:356-378, MySQL ps_information.cc:96.
        if cluster_uuid and own_uuid and any(
                _uuid(row.get('uuid')) == own_uuid and
                (probe.get('wsrep_cluster_status') == 'Primary' if galera else
                 row.get('state') in ('ONLINE', 'RECOVERING'))
                for row in probe.get('cluster_members', [])):
            attestations[pid] = observations[pid]
            node_index[pid]['cluster_id'] = observations[pid]
        else:
            notes.append({'profile_id': pid, 'message': 'This member could not confirm its own cluster membership.'})

    host_key_cache = {}
    loopback_cache = {}

    def _cached_host_key(raw):
        clean = _map_host(raw)
        if not clean:
            return ''
        key = host_key_cache.get(clean)
        if key is None:
            key = topology_links._host_key(clean)
            host_key_cache[clean] = key
        return key

    def _cached_is_loopback(hkey):
        if not hkey:
            return False
        res = loopback_cache.get(hkey)
        if res is None:
            res = topology_links.is_loopback(hkey)
            loopback_cache[hkey] = res
        return res

    # Compute host keys once per member so attribution stays linear in rows.
    member_host_keys = {entry['profile_id']: _cached_host_key(entry['host']) for entry in entries}
    member_ssh_keys = {entry['profile_id']: _cached_host_key(stored[entry['profile_id']].get('ssh_host'))
                       for entry in entries}
    member_is_loopback = {pid: _cached_is_loopback(hkey) for pid, hkey in member_host_keys.items()}

    # Pass 1: resolve Readyset upstreams first so database SHOW REPLICAS /
    # SHOW SLAVE HOSTS rows can be attributed to their Readyset member.
    readyset_cdc = {}
    rs_deploy_keys = {}
    valid_health = ('up', 'down', 'healthy', 'unhealthy', 'unknown', 'Online', 'Offline')
    for pid, probe in probes.items():
        if by_id[pid].get('duplicate_of') or probe.get('kind') != 'readyset':
            continue
        proxied_uuid = _uuid(probe.get('proxied_server_uuid'))
        proxied_matches = [
            db_pid for db_pid, db_probe in probes.items()
            if db_probe.get('kind') == 'database'
            and by_id[db_pid]['outcome'] == 'read'
            and not by_id[db_pid].get('duplicate_of')
            and _uuid(by_id[db_pid].get('server_uuid')) == proxied_uuid
        ] if proxied_uuid else []
        proxied_target = proxied_matches[0] if len(proxied_matches) == 1 else None
        proxied_host = _map_host(probe.get('proxied_hostname'))
        proxied_host_key = _cached_host_key(proxied_host) if proxied_host else ''
        proxied_port = _map_port(probe.get('proxied_port'))

        raw_upstreams = [u for u in (probe.get('upstreams') or ()) if isinstance(u, dict)]
        cdc_raw_host, cdc_raw_port = topology_links.split_host_port(probe.get('cdc_server'), 3306)
        cdc_host = _map_host(cdc_raw_host)
        cdc_host_key = _cached_host_key(cdc_host) if cdc_host else ''
        cdc_port = _map_port(cdc_raw_port) if cdc_host else None

        upstreams = [dict(u, _from_cdc_server=False) for u in raw_upstreams]
        has_explicit_cdc = any(
            _is_truthy(u.get('active_cdc')) or ('cdc' in str(u.get('purpose') or '').lower())
            for u in upstreams
        )
        has_single_no_purpose = (
            len(upstreams) == 1 and not str(upstreams[0].get('purpose') or '').strip()
        )
        if cdc_host and not has_explicit_cdc and not has_single_no_purpose:
            already_present = any(
                _cached_host_key(u.get('host')) == cdc_host_key and
                (_map_port(u.get('port')) or 3306) == (cdc_port or 3306)
                for u in upstreams
            )
            if not already_present:
                upstreams.append({
                    'host': cdc_host, 'port': cdc_port, 'purpose': 'cdc',
                    'active_cdc': '1', '_from_cdc_server': True,
                })

        ups_sig = tuple(sorted(
            (_cached_host_key(u.get('host')), _map_port(u.get('port')) or 3306)
            for u in raw_upstreams if _map_host(u.get('host'))
        ))
        rs_deploy_keys[pid] = (
            member_host_keys[pid],
            member_ssh_keys[pid],
            ups_sig,
            str(probe.get('cdc_server') or '').strip().lower(),
        )
        replication_running = str(probe.get('replication_status') or '').strip().lower() == 'running'

        resolved = []
        for upstream in upstreams:
            dest, ev, ver = target(pid, upstream.get('host'), upstream.get('port'),
                                   allow_peer=False, database_only=True)
            health = upstream.get('health')
            state = health if health in valid_health else None
            purpose = str(upstream.get('purpose') or '').strip().lower()
            no_purpose = not purpose
            has_cdc_purpose = 'cdc' in purpose
            has_proxy_purpose = ('proxy' in purpose) or ('query' in purpose)
            is_cdc = _is_truthy(upstream.get('active_cdc')) or has_cdc_purpose
            is_proxy = no_purpose or has_proxy_purpose
            is_cdc_and_proxy = has_cdc_purpose and has_proxy_purpose
            user = _map_user(upstream.get('user'))
            uhkey = _cached_host_key(upstream.get('host'))
            uport = _map_port(upstream.get('port'))
            resolved.append({
                'host': upstream.get('host'), 'port': upstream.get('port'),
                'host_key': uhkey, 'port_val': uport,
                'dest': dest, 'evidence': ev, 'verified': ver, 'state': state,
                'is_cdc': is_cdc, 'active_cdc': _is_truthy(upstream.get('active_cdc')),
                'is_proxy': is_proxy, 'is_cdc_and_proxy': is_cdc_and_proxy,
                'no_purpose': no_purpose, 'from_cdc_server': bool(upstream.get('_from_cdc_server')),
                'user': user,
            })

        cdc_server_dest = None
        if cdc_host and cdc_port is not None:
            cdc_server_dest, _, _ = target(pid, cdc_host, cdc_port, allow_peer=False, database_only=True)

        covered = set()
        if proxied_target is not None:
            # Affirmative CDC evidence for Readyset -> proxied_target:
            # (a) one real upstream row whose purpose includes both cdc and proxy
            #     (or one upstream with no purpose column), not contradicted by
            #     a resolved endpoint to another member;
            # (b) CDC Server equals the proxied @@hostname:@@port read in the
            #     same pass;
            # (c) CDC address resolves by endpoint to proxied_target.
            cond_a = (
                len(resolved) == 1
                and not resolved[0]['from_cdc_server']
                and (resolved[0]['is_cdc_and_proxy'] or resolved[0]['no_purpose'])
                and resolved[0]['dest'] in (None, proxied_target)
                and cdc_server_dest in (None, proxied_target)
            )
            cond_b = (
                bool(cdc_host_key) and cdc_port is not None
                and bool(proxied_host_key) and proxied_port is not None
                and cdc_host_key == proxied_host_key
                and cdc_port == proxied_port
                and cdc_server_dest in (None, proxied_target)
            )
            other_active_cdc = any(
                item['active_cdc'] and item['dest'] != proxied_target
                for item in resolved
            )
            cond_c = (
                cdc_server_dest == proxied_target
                or (cdc_server_dest is None and not other_active_cdc and any(
                    item['is_cdc'] and item['dest'] == proxied_target for item in resolved
                ))
            )
            cdc_matches_proxied = bool(cond_a or cond_b or cond_c)

            for idx, item in enumerate(resolved):
                if item['dest'] == proxied_target:
                    covered.add(idx)
                elif (item['dest'] is None and proxied_host_key and proxied_port is not None
                      and item['host_key'] == proxied_host_key
                      and (item['port_val'] or 3306) == proxied_port):
                    covered.add(idx)
            if cond_a and resolved[0]['dest'] is None:
                covered.add(0)
            elif not covered:
                # Never let an unmatched CDC-only upstream be swallowed here.
                unmatched_proxy = [
                    idx for idx, item in enumerate(resolved)
                    if item['dest'] is None and item['is_proxy'] and not item['is_cdc']
                ]
                if len(unmatched_proxy) == 1:
                    covered.add(unmatched_proxy[0])

            proxied_label = 'Upstream' if cdc_matches_proxied else 'Upstream (queries)'
            proxied_state = next((resolved[idx]['state'] for idx in sorted(covered)
                                  if resolved[idx]['state'] is not None), None)
            edge_key = edge(pid, pid, proxied_target, 'cache_upstream', 'proxied_identity',
                            True, proxied_state, proxied_label)
            if cdc_matches_proxied and edge_key is not None:
                cdc_users = {resolved[idx]['user'] for idx in covered if resolved[idx]['user']}
                readyset_cdc.setdefault(proxied_target, []).append({
                    'profile_id': pid, 'verified_cdc': True, 'edge_key': edge_key,
                    'users': cdc_users, 'replication_running': replication_running,
                })

        any_explicit_cdc = any(item['is_cdc'] for item in resolved)
        for idx, item in enumerate(resolved):
            if idx in covered:
                continue
            dest, ev, ver = item['dest'], item['evidence'], item['verified']
            if dest is None:
                dest = peer(_map_host(item['host']), _map_port(item['port']), None, pid)
            ek = edge(pid, pid, dest, 'cache_upstream', ev, ver, item['state'], 'Upstream')
            if (item['dest'] is not None and ek is not None and
                    (item['is_cdc'] or (len(resolved) == 1 and not any_explicit_cdc))):
                existing = readyset_cdc.setdefault(item['dest'], [])
                if not any(entry['profile_id'] == pid for entry in existing):
                    existing.append({
                        'profile_id': pid, 'verified_cdc': False, 'edge_key': ek,
                        'users': {item['user']} if item['user'] else set(),
                        'replication_running': replication_running,
                    })

    edge_note_state = {}
    class_rank = {'none': 0, 'unknown': 1, 'other': 2, 'mysqld': 3}

    for pid, probe in probes.items():
        alias = by_id[pid].get('duplicate_of')
        if alias:
            edge(pid, pid, alias, 'alias', 'server_uuid', False, None, 'same server (alias)')
            continue
        cluster_id = observations.get(pid)
        if probe.get('cluster_kind') in ('group_replication', 'innodb_cluster', 'galera', 'pxc'):
            box = boxes.get(cluster_id)
            for row in probe.get('cluster_members', []):
                node_id, evidence, verified = target(pid, row.get('host'), row.get('port'), row.get('uuid'),
                    identity_only=True, galera=probe.get('cluster_kind') in ('galera', 'pxc'))
                if box and node_id and (node_id in peers or attestations.get(node_id) == cluster_id):
                    if node_id not in box['members']:
                        box['members'].append(node_id)
        for channel in probe.get('channels', []):
            source, evidence, verified = target(pid, channel.get('source_host'), channel.get('source_port'),
                                                channel.get('source_uuid'), channel.get('source_server_id'),
                                                database_only=True)
            state = _replica_state([channel])
            edge(pid, source, pid, 'replication', evidence, verified, state, 'Replication')

        unmatched_replicas = []
        for replica in probe.get('replicas', []):
            destination, evidence, verified = target(pid, replica.get('host'), replica.get('port'),
                                                     replica.get('uuid'), replica.get('server_id'),
                                                     allow_peer=False, database_only=True)
            if destination is not None:
                edge(pid, pid, destination, 'replication', evidence, verified, None, 'Replication')
            else:
                unmatched_replicas.append(replica)
        if unmatched_replicas:
            rs_users = readyset_cdc.get(pid, [])
            adapter_keys = {
                rs_deploy_keys.get(item['profile_id']) for item in rs_users
                if probes.get(item['profile_id'], {}).get('port_role') == 'adapter'
            }
            rs_owners = [
                item for item in rs_users
                if not (probes.get(item['profile_id'], {}).get('port_role') == 'admin'
                        and rs_deploy_keys.get(item['profile_id']) in adapter_keys)
            ]

            # Build normalized READYSET% client (host_key, user) set once per source.
            rs_client_pairs = set()
            for crow in probe.get('readyset_clients') or ():
                if not isinstance(crow, dict):
                    continue
                chkey = _cached_host_key(crow.get('host'))
                cuser = _map_user(crow.get('user'))
                if chkey and cuser:
                    rs_client_pairs.add((chkey, cuser))

            # Re-validate dump rows in _build_map: drop rejected hosts before
            # calling _host_key, cap users at 32 chars, and ignore any UUID
            # seen with more than one dump host.
            legacy_dump = 'dump_threads' not in probe and 'readyset_replicas' in probe
            raw_dumps = probe.get('readyset_replicas') if legacy_dump else probe.get('dump_threads')
            dump_by_uuid = {}
            all_dump_classes = []
            for row in raw_dumps or ():
                if not isinstance(row, dict):
                    continue
                duuid = _uuid(row.get('uuid'))
                dhost = _map_host(row.get('host'))
                if not duuid or not dhost:
                    continue
                dhkey = _cached_host_key(dhost)
                if not dhkey:
                    continue
                duser = _map_user(row.get('user'))
                dclass = row.get('class')
                if dclass not in class_rank:
                    dclass = 'none' if legacy_dump else 'unknown'
                all_dump_classes.append(dclass)
                has_rs_client = bool(row.get('readyset_client')) or (
                    bool(duser) and (dhkey, duser) in rs_client_pairs
                )
                existing = dump_by_uuid.get(duuid)
                if existing is None:
                    dump_by_uuid[duuid] = {
                        'host_key': dhkey,
                        'is_loopback': _cached_is_loopback(dhkey),
                        'ambiguous_host': False,
                        'users': {duser} if duser else set(),
                        'class': dclass,
                        'readyset_client': has_rs_client,
                    }
                else:
                    if existing['host_key'] != dhkey:
                        existing['ambiguous_host'] = True
                    if duser:
                        existing['users'].add(duser)
                    if has_rs_client:
                        existing['readyset_client'] = True
                    if class_rank[dclass] > class_rank[existing['class']]:
                        existing['class'] = dclass

            observer_host = replication.reached_host(stored[pid])
            source_name = by_id[pid]['profile_name']
            rs_host_meta = {}
            for rs in rs_owners:
                rs_pid = rs['profile_id']
                rs_host_meta[rs_pid] = {
                    'host_key': member_host_keys[rs_pid],
                    'loopback_ok': (
                        topology_links._loopback_scope_matches(observer_host, stored[rs_pid].get('ssh_host'))
                        and member_is_loopback[rs_pid]
                    ),
                }
            sole_verified_cdc = len(rs_owners) == 1 and bool(rs_owners[0]['verified_cdc'])
            match_cache = {}

            def _resolve_candidate(replica):
                rep_uuid = _uuid(replica.get('uuid'))
                rep_host = _map_host(replica.get('host'))
                rep_port = _map_port(replica.get('port'))
                ckey = (rep_uuid, rep_host, rep_port)
                if ckey in match_cache:
                    return match_cache[ckey]
                result = None
                if rs_owners and rep_uuid and rep_uuid in dump_by_uuid:
                    info = dump_by_uuid[rep_uuid]
                    if not info['ambiguous_host'] and info['class'] == 'none':
                        user_candidates = []
                        for rs in rs_owners:
                            if rs['users']:
                                ok = bool(info['users'] & rs['users'])
                            else:
                                ok = info['readyset_client']
                            if ok:
                                user_candidates.append(rs)
                        if user_candidates:
                            host_candidates = []
                            dhkey = info['host_key']
                            dh_loop = info['is_loopback']
                            for rs in user_candidates:
                                meta = rs_host_meta[rs['profile_id']]
                                rkey = meta['host_key']
                                if dh_loop:
                                    hmatch = meta['loopback_ok'] and (
                                        dhkey == rkey or (
                                            dhkey in topology_links._LOCAL_NAMES
                                            and rkey in topology_links._LOCAL_NAMES
                                        )
                                    )
                                else:
                                    hmatch = bool(rkey) and dhkey == rkey
                                if hmatch:
                                    host_candidates.append(rs)
                            if len(host_candidates) == 1:
                                result = ('host', host_candidates[0])
                            elif len(host_candidates) > 1:
                                result = None
                            elif sole_verified_cdc and len(user_candidates) == 1:
                                result = ('sole', user_candidates[0])
                match_cache[ckey] = result
                return result

            # Pass A claims direct host matches before Pass B (sole verified CDC),
            # and each Readyset member claims at most one registration per source.
            evaluated = [(replica, _resolve_candidate(replica)) for replica in unmatched_replicas]
            claimed_rs = set()
            matched_indices = set()
            for tier in ('host', 'sole'):
                for idx, (replica, match) in enumerate(evaluated):
                    if idx in matched_indices or match is None or match[0] != tier:
                        continue
                    owner = match[1]
                    if owner['profile_id'] in claimed_rs:
                        continue
                    claimed_rs.add(owner['profile_id'])
                    matched_indices.add(idx)
                    _attach_replica_note(edges, owner['edge_key'], source_name,
                                         replica.get('server_id'), estimate=False,
                                         note_state=edge_note_state)
            remaining_unmatched = [
                replica for idx, (replica, _) in enumerate(evaluated) if idx not in matched_indices
            ]

            # Estimate fallback: only when dump evidence is unavailable for S
            # (every dump session is unknown or the dump read failed), S is R's
            # verified CDC upstream, S has one unmatched row with no reported
            # Host, one Readyset uses S, R's Replication Status is running, and
            # the row's dump class is neither mysqld nor other.
            dump_unavailable = bool(probe.get('dump_read_failed')) or (
                bool(all_dump_classes) and all(c == 'unknown' for c in all_dump_classes)
            )
            if (dump_unavailable and len(unmatched_replicas) == 1 and len(remaining_unmatched) == 1
                    and len(rs_owners) == 1 and rs_owners[0]['verified_cdc']
                    and rs_owners[0]['replication_running']):
                only_row = remaining_unmatched[0]
                raw_host = str(only_row.get('host') or '').strip()
                row_uuid = _uuid(only_row.get('uuid'))
                row_class = dump_by_uuid[row_uuid]['class'] if (row_uuid and row_uuid in dump_by_uuid) else None
                if not raw_host and row_class not in ('mysqld', 'other'):
                    _attach_replica_note(edges, rs_owners[0]['edge_key'], source_name,
                                         only_row.get('server_id'), estimate=True,
                                         note_state=edge_note_state)
                    remaining_unmatched = []
            for replica in remaining_unmatched:
                dest = peer(_map_host(replica.get('host')), _map_port(replica.get('port')),
                            _uuid(replica.get('uuid')), pid,
                            server_id=replica.get('server_id'), is_replica=True)
                edge(pid, pid, dest, 'replication', None, False, None, 'Replication')

        if probe.get('kind') == 'proxysql':
            servers = probe.get('servers', [])
            states = {}
            for backend in servers:
                hg = _number(backend.get('hostgroup_id'))
                state = None if backend.get('status_source') == 'config' else backend.get('status')
                states.setdefault(hg, []).append(state)
            offline = set(probe.get('offline_hostgroups') or [])
            no_online = {hg for hg, group_states in states.items()
                         if isinstance(hg, int) and group_states and
                         all(s in ('SHUNNED', 'SHUNNED_REPLICATION_LAG', 'SHUNNED_AWS_BGD',
                                   'OFFLINE_SOFT', 'OFFLINE_HARD') for s in group_states)}
            for backend in servers:
                raw_host = backend.get('hostname')
                backend_host = _map_host(raw_host)
                if backend_host is None:
                    backend_host = raw_host.strip() if isinstance(raw_host, str) else ''
                backend_port = _map_port(backend.get('port')) or 0
                destination, evidence, verified = target(pid, raw_host, backend.get('port'))
                hostgroup = _number(backend.get('hostgroup_id'))
                raw_state = None if backend.get('status_source') == 'config' else backend.get('status')
                state = (raw_state if isinstance(raw_state, str) and (
                    raw_state in ('ONLINE', 'OFFLINE_SOFT', 'OFFLINE_HARD') or
                    re.fullmatch(r'SHUNNED[A-Za-z0-9_]{0,56}', raw_state)) else None)
                reasons = []
                if state is not None and hostgroup in offline:
                    reasons.append("In the monitor's offline hostgroup.")
                    if state == 'ONLINE':
                        state = 'offline'
                if state == 'OFFLINE_SOFT':
                    reasons.append('No new traffic; existing queries and transactions may finish.')
                elif state == 'OFFLINE_HARD':
                    reasons.append('ProxySQL has taken this backend out of traffic.')
                elif state == 'SHUNNED_REPLICATION_LAG':
                    reasons.append('Shunned for replication lag.')
                if hostgroup in no_online and not probe.get('servers_incomplete'):
                    reasons.append('No ONLINE member in this hostgroup.')
                label = 'hostgroup %s' % (hostgroup if isinstance(hostgroup, int) else '?')
                edge(pid, pid, destination, 'proxy_route', evidence, verified, state, label,
                     ' '.join(reasons) if reasons else None,
                     target_key=(backend_host.lower(), backend_port))
    for node in peers.values():
        node['seen_by'].sort()
    for box in boxes.values():
        box['members'].sort()
        box['seen_from'].sort()
    return dict(nodes=nodes + [peers[key] for key in sorted(peers)],
                edges=[edges[key] for key in sorted(edges)], clusters=[boxes[key] for key in sorted(boxes)], notes=notes)


def _duplicate_notes(entries, profiles=None, facts=None):
    """Annotate aliases of an observed MySQL server without merging members."""
    seen, notes = {}, []
    stored = {profile['id']: profile for profile in profiles or []}
    facts = facts or {}
    for entry in entries:
        server = _uuid(entry['server_uuid']) if entry['engine'] == 'mysql' and entry['outcome'] == 'read' else None
        if server is None:
            continue
        seen.setdefault(server, []).append(entry)
    for aliases in seen.values():
        if len(aliases) < 2:
            continue
        direct, matched, unknown = [], [], []
        reported_port = None
        for entry in aliases:
            own = facts.get(entry['profile_id'], {})
            port = _map_port(own.get('port'))
            if port is None:
                unknown.append(entry)
                continue
            if reported_port is None:
                reported_port = port
            if entry['port'] == port:
                matched.append(entry)
                hosts = {str(host).lower().rstrip('.') for host in
                         (own.get('hostname') or entry.get('hostname'), own.get('report_host')) if host}
                if hosts and entry['host'].lower().rstrip('.') in hosts:
                    direct.append(entry)
        # Prefer (1) matching reported port and host, (2) matching reported port
        # (or unknown server port, which provides no contrary evidence), then
        # (3) every alias when port forwarding leaves no port match. Within
        # each step, prefer direct sessions (routed is False) over routed or
        # unreadable ones, then break ties by the oldest saved update
        # timestamp, then profile id.
        eligible = direct or matched or unknown
        fallback_note = not eligible
        first = min(eligible or aliases, key=lambda entry: (
                    0 if entry.get('routed') is False else 1,
                    stored.get(entry['profile_id'], {}).get('updated_at') or '',
                    entry['profile_id']))
        fallback_port = _map_port(facts.get(first['profile_id'], {}).get('port')) or reported_port
        for entry in aliases:
            if entry is first:
                continue
            message = ('%s and %s reach the same server (same server_uuid); one may be a router or proxy port.' %
                       (first['profile_name'], entry['profile_name']))
            if fallback_note and fallback_port is not None:
                message += (" No saved connection uses the server's reported port %d; "
                            "%s was kept as the server and the others are treated as aliases." %
                            (fallback_port, first['profile_name']))
            entry['duplicate_of'] = first['profile_id']
            entry['message'] += ' ' + message
            notes.append({'profile_id': entry['profile_id'], 'message': message})
    return notes


def _entry(profile):
    entry = dict(profile_id=profile['id'], profile_name=profile.get('name') or profile['id'],
                 host=profile.get('host') or '', port=profile.get('port') or 3306,
                 safety={'type': profile.get('type') if profile.get('type') in dbops.CONNECTION_TYPES else dbops.DEFAULT_CONNECTION_TYPE,
                         'read_only': bool(profile.get('read_only'))},
                 outcome='not_reached', message='Not read: the time budget did not allow this member to start.', elapsed_s=0,
                 duplicate_of=None)
    entry.update(dict.fromkeys(('engine', 'product', 'version', 'version_comment', 'server_uuid', 'server_id',
                               'hostname', 'read_only', 'read_only_value', 'super_read_only', 'role', 'state', 'lag', 'behind', 'behind_reason', 'routed')))
    entry['channels'] = []
    return entry


def _comparison_inputs(results, section):
    """Keep unavailable inventories separate from trustworthy absence evidence."""
    from . import account_compare, accounts
    snapshots, not_compared, unreadable = {}, [], set()
    for entry, snapshot, position, reason, multi in results:
        pid = entry['profile_id']
        if entry['outcome'] != 'read' or snapshot is None or reason is not None:
            not_compared.append({'profile_id': pid, 'reason': reason or entry['message']})
            continue
        if section in ('parameters', 'accounts'):
            if (snapshot.get('complete') is not True or snapshot.get('error') or
                    not isinstance(snapshot.get('items'), list)):
                reason = 'The %s inventory is incomplete.' % ('parameter' if section == 'parameters' else 'account')
                if snapshot.get('error'):
                    reason += ' ' + connection_groups.safe_problem(snapshot['error'])
                not_compared.append({'profile_id': pid, 'reason': reason})
                continue
        if section == 'accounts':
            if any(not isinstance(item, dict) or not isinstance(item.get('name'), str) or
                   not isinstance(item.get('host'), str) for item in snapshot['items']):
                not_compared.append({'profile_id': pid, 'reason': 'The account inventory could not be read.'})
                continue
            excluded = {(item['name'], item['host']) for item in snapshot['items']
                        if account_compare.incomplete_account(item)}
            unreadable.update(excluded)
            if excluded:
                # Account names are display data. Never include diagnostic or
                # grant text, and escape controls before adding them to prose.
                labels = [json.dumps(dbops.redact(accounts._strip_account_credentials('%s@%s' % key)), ensure_ascii=True)
                          for key in sorted(excluded)]
                not_compared.append({'profile_id': pid, 'reason':
                    'Accounts not compared because their reads were incomplete: %s. '
                    'These accounts are omitted from all group rows; other readable accounts are compared.' %
                    ', '.join(labels)})
        snapshots[pid] = snapshot
    if section == 'accounts' and unreadable:
        # Drop the same keys on every member. Removing a failed account on
        # only one side would invent an absent account and false drift.
        snapshots = {pid: dict(snapshot, items=[item for item in snapshot['items']
                         if (item['name'], item['host']) not in unreadable])
                     for pid, snapshot in snapshots.items()}
    return snapshots, not_compared


def read_group(store, group_name, section, active_member_profile_id,
               expected_current, expected_generation):
    """Read only the stored members, once each, on an explicit tab request."""
    from .workspace_store import ENDPOINT_FIELDS, _same_endpoint
    from . import param_compare, account_compare
    if not isinstance(section, str) or section not in SECTIONS:
        raise ValueError('Choose a topology section: map, members, parameters, accounts or plugins.')
    group_name = connection_groups.validate_group(group_name)
    profiles = [p for p in store.list('profile') if isinstance(p, dict) and isinstance(p.get('id'), str)]
    members = [p for p in profiles if group_name and p.get('group') == group_name]
    active = next((p for p in members if p['id'] == active_member_profile_id), None)
    if (active is None or not isinstance(expected_current, dict) or
            not isinstance(expected_generation, str) or not expected_generation or
            not _same_endpoint(active, expected_current) or
            any(p.get('group_generation') != expected_generation for p in members)):
        raise ValueError('This saved connection is outside the current connection group.')
    if len(members) > connection_groups.GROUP_MAX_MEMBERS:
        raise ValueError('A topology read supports at most 16 saved group members.')
    # The endpoint's validated snapshot remains the authority, including if a
    # group is deleted and recreated before this helper or a queued login.
    route = dict({key: expected_current.get(key) for key in ENDPOINT_FIELDS},
                 profile_id=active_member_profile_id, group_name=group_name)

    def active_guard():
        _profiles, current = connection_groups.request_profiles(store, route)
        if current is None or current.get('group_generation') != expected_generation:
            raise connection_groups.ProfileReadError(
                'This saved connection is outside the current connection group.')

    active_guard()
    started, taken_at = time.monotonic(), now()
    request_seconds = ACCOUNTS_REQUEST_SECONDS if section == 'accounts' else REQUEST_SECONDS
    deadline = started + request_seconds
    facts = {}

    def _record_member_failure(profile, exc, began):
        if not isinstance(exc, (ConnectorError, ValueError, OSError)):
            # A programming error belongs to this member, not its peers.
            # Reuse HTTP traceback redaction, including the saved group
            # credentials, without exposing exception text in the report.
            from .ui import _log_unexpected_error
            _log_unexpected_error('/api/live/topology', sys.exc_info(), route, store)
            exc = ConnectorError('The connection could not be read.')
        # No observed identity, role or inventory from a rejected read
        # may influence the group summary or another member's comparison.
        entry = _entry(profile)
        entry.update(outcome='failed', message=connection_groups.probe_note(profile, exc))
        entry['elapsed_s'] = round(time.monotonic() - began, 3)
        return entry, None, None, connection_groups.safe_problem(exc), False

    def _finalize_member(profile, entry, variables, conn, transport, exclusion):
        # Optional inventory readers may catch a failure, but cannot
        # make mixed-server data or a failed transport usable again.
        if isinstance(conn, dbops.TargetEvidenceConnection):
            conn.check()
        transport.check()
        entry['outcome'] = 'read'
        provider = str(variables.get('wsrep_provider') or '').strip().lower()
        active_cluster = None
        if (entry['role'] in ('primary', 'secondary') and entry['state'] in ('ONLINE', 'RECOVERING')
                and _uuid(variables.get('group_replication_group_name'))):
            active_cluster = 'group_replication'
        elif (_on(variables.get('wsrep_on')) and provider and provider != 'none'
              and variables.get('wsrep_cluster_status') == 'Primary'):
            active_cluster = 'galera'
        facts[profile['id']] = dict(role=entry['role'], state=entry['state'],
            gr_group=_uuid(variables.get('group_replication_group_name')), active_cluster=active_cluster,
            hostname=variables.get('hostname'), report_host=variables.get('report_host'),
            port=_map_port((conn.variables or {}).get('port'))
            if isinstance(conn, dbops.TargetEvidenceConnection) else None)
        if entry['message'].startswith('Not read:'):
            entry['message'] = exclusion or 'Read from this member.'

    def _open_and_identify(profile, stack):
        entry, position, exclusion = _entry(profile), None, None
        multi_primary = False
        began = time.monotonic()
        # Reserve a complete query window. Queued members never log in with
        # too little time left for their first query, and are never retried.
        if deadline - began < QUERY_SECONDS:
            return {'ok': False, 'result': (entry, None, None, entry['message'], False)}
        try:
            active_guard()
            opened = stack.enter_context(open_group_member(
                store, group_name, profile, active_guard,
                {'query_timeout': QUERY_SECONDS, 'connect_timeout': QUERY_SECONDS},
            ))
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
            transport = _ReadConnection(param_compare.BoundedConnection(
                member_conn, seconds=min(remaining, request_seconds)))
            conn = transport
            preflight, exclusion = _identity(conn, entry, read_position=False)
            variables = preflight
            if exclusion is None:
                conn = dbops.TargetEvidenceConnection(transport)
                read_position = section in ('members', 'map', 'parameters')
                variables, second_exclusion = _identity(conn, entry, read_position=read_position)
                conn.check()
                observed = conn.variables or {}
                if second_exclusion is not None or is_readyset_comment(observed.get('version_comment') or ''):
                    raise ConnectorError('The member identity changed during the read.')
                keys = (('server_id', 'hostname', 'version') if entry['engine'] == 'mariadb'
                        else ('server_uuid',))
                if any(str(preflight.get(key)) != str(observed.get(key)) or
                       str(variables.get(key)) != str(observed.get(key)) for key in keys):
                    raise ConnectorError('The member identity changed during the read.')
                notes = _read_routed(conn, entry)
                if section in ('members', 'map', 'parameters'):
                    position, member_notes = _member_facts(conn, entry, variables)
                    notes.extend(member_notes)
                    multi_primary = (entry['role'] in ('primary', 'secondary')
                                     and _on(variables.get('group_replication_single_primary_mode')) is False)
                if notes:
                    entry['message'] = 'Read with unavailable facts: ' + ' '.join(dict.fromkeys(notes))
            return {
                'ok': True, 'profile': profile, 'entry': entry, 'position': position,
                'exclusion': exclusion, 'multi_primary': multi_primary, 'conn': conn,
                'transport': transport, 'variables': variables, 'began': began, 'stack': stack,
            }
        except _NotReached:
            entry['elapsed_s'] = round(time.monotonic() - began, 3)
            return {'ok': False, 'result': (entry, None, None, entry['message'], False)}
        except Exception as exc:
            return {'ok': False, 'result': _record_member_failure(profile, exc, began)}

    def read_one(profile):
        with contextlib.ExitStack() as stack:
            state = _open_and_identify(profile, stack)
            if not state['ok']:
                return state['result']
            entry, position, exclusion = state['entry'], state['position'], state['exclusion']
            multi_primary, conn, transport = state['multi_primary'], state['conn'], state['transport']
            variables, began = state['variables'], state['began']
            snapshot = None
            try:
                if exclusion is None:
                    if section == 'parameters':
                        snapshot = param_compare.capture(conn, server=entry, full_provider_options=True)
                    elif section == 'accounts':
                        accounts_began = time.monotonic()
                        snapshot = account_compare.capture(conn, server=entry)
                        accounts_seconds = time.monotonic() - accounts_began
                        if accounts_seconds >= QUERY_SECONDS:
                            entry['message'] = ('Accounts are read one at a time; %d accounts took %.1f s '
                                                'on this server.' % (len(snapshot['items']), accounts_seconds))
                    elif section == 'plugins':
                        snapshot = _plugins(conn, entry)
                _finalize_member(profile, entry, variables, conn, transport, exclusion)
            except _NotReached:
                exclusion = entry['message']
            except Exception as exc:
                return _record_member_failure(profile, exc, began)
            entry['elapsed_s'] = round(time.monotonic() - began, 3)
            return entry, snapshot, position, exclusion, multi_primary

    from . import read_cancel
    request_cancel = read_cancel.current()
    request_guard = read_cancel.current_guard()

    def read_member(profile):
        # Thread-local request state is intentionally not inherited by pools.
        # Carry only this read's trusted event, never data from request JSON.
        with read_cancel.scope(request_cancel, request_guard):
            return read_one(profile)

    if section == 'map':
        phase1_states = []

        def phase1_member(profile):
            with read_cancel.scope(request_cancel, request_guard):
                stack = contextlib.ExitStack()
                state = _open_and_identify(profile, stack)
                if not state['ok']:
                    stack.close()
                return state

        try:
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
                phase1_states = list(pool.map(phase1_member, members))
                has_readyset = any(
                    state['ok'] and state['entry']['product'] == 'Readyset'
                    for state in phase1_states
                )

                def phase2_member(state):
                    with read_cancel.scope(request_cancel, request_guard):
                        if not state['ok']:
                            return state['result']
                        stack = state['stack']
                        profile, entry = state['profile'], state['entry']
                        position, multi_primary = state['position'], state['multi_primary']
                        conn, transport = state['conn'], state['transport']
                        variables, began = state['variables'], state['began']
                        try:
                            probe, notes = _map_probe(conn, profile, entry, variables,
                                                      has_readyset=has_readyset)
                            snapshot, exclusion = {'probe': probe, 'notes': notes}, None
                            _finalize_member(profile, entry, variables, conn, transport, exclusion)
                            entry['elapsed_s'] = round(time.monotonic() - began, 3)
                            return entry, snapshot, position, exclusion, multi_primary
                        except _NotReached:
                            entry['elapsed_s'] = round(time.monotonic() - began, 3)
                            return entry, None, None, entry['message'], False
                        except Exception as exc:
                            return _record_member_failure(profile, exc, began)
                        finally:
                            state['stack'] = None
                            stack.close()

                results = list(pool.map(phase2_member, phase1_states))
        finally:
            for state in phase1_states:
                stack = state.get('stack')
                if stack is not None:
                    state['stack'] = None
                    stack.close()
    else:
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as pool:
            results = list(pool.map(read_member, members))
    entries = [result[0] for result in results]
    duplicate_notes = _duplicate_notes(entries, members, facts)
    primary_id, primary_reason = _primary(entries, members, multi_primary=any(result[4] for result in results))
    if section in ('members', 'map'):
        positions = {entry['profile_id']: position for entry, snapshot, position, reason, multi in results}
        primary_entry = next((entry for entry in entries if entry['profile_id'] == primary_id), None)
        related = _related_members(entries, members, primary_id, facts)
        for entry in entries:
            reason = None
            source = positions.get(primary_id)
            current = positions.get(entry['profile_id'])
            if entry['role'] in ('proxy', 'cache'):
                reason = 'Not a database server.'
            elif entry['role'] == 'node':
                reason = 'Galera nodes apply write sets from every node; see the receive queue.'
            elif primary_entry is not None and primary_entry['role'] == 'node':
                reason = 'The primary or source is a Galera node; GTID positions cannot establish how far behind a member is.'
            elif primary_id is None:
                reason = primary_reason
            elif entry['profile_id'] not in related:
                reason = 'No replication relationship to the primary or source was found.'
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
                        if not related[entry['profile_id']]:
                            entry['behind']['message'] = ('Per-domain sequence difference estimate; source matched by server ID. '
                                                          'This is not an exact transaction count.')
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
    if section == 'map':
        probes = {entry['profile_id']: snapshot['probe'] for entry, snapshot, position, reason, multi in results
                  if entry['outcome'] == 'read' and snapshot is not None}
        report['map'] = _build_map(entries, probes, members)
        report['map']['notes'].extend(duplicate_notes)
        report['map']['notes'].extend({'profile_id': entry['profile_id'], 'message': note}
            for entry, snapshot, position, reason, multi in results if snapshot is not None
            for note in snapshot['notes'])
    elif section != 'members':
        snapshots, not_compared = _comparison_inputs(results, section)
        compare = {'parameters': param_compare.compare_many, 'accounts': account_compare.compare_many,
                   'plugins': compare_plugins}[section]
        plugin_notes = [{'profile_id': pid, 'message': note} for pid in sorted(snapshots)
                        for note in snapshots[pid]['notes']] if section == 'plugins' else []
        if section == 'plugins':
            snapshots = {pid: value['items'] for pid, value in snapshots.items()}
        canonical = {entry['profile_id']: entry for entry in entries}
        role_settings = {}
        for entry in entries:
            pid = entry.get('duplicate_of') or entry['profile_id']
            role = canonical[pid]['role']
            role_settings[entry['profile_id']] = 'source' if pid == primary_id and role == 'standalone' else role
        active_clusters = {pid: fact['active_cluster'] for pid, fact in facts.items()}
        rows = (compare(snapshots, role_settings=role_settings, active_clusters=active_clusters)
                if section == 'parameters' else compare(snapshots))
        report[section] = {'rows': rows, 'compared': sorted(snapshots), 'not_compared': not_compared}
        if section == 'plugins':
            report[section]['notes'] = plugin_notes
    return report
