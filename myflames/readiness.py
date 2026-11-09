"""Small, read-only connection facts. No workload sampling or server writes.

The fixed first batch reads variables, wsrep status and this account's grants
in one client invocation. Engine-specific metadata probes share its 3 s budget.
Unknown topology or privileges stay unknown; an empty disabled P_S is no proof.
"""
import time

from . import privileges
from .connector import ConnectorError, unescape_mysql
from .perf_common import now
from .server_info import product_identity, server_version_tuple

TAG = '/*myflames:readiness*/ '
TIMEOUT = 3
_VARIABLES = ('version', 'version_comment', 'read_only', 'super_read_only',
              'performance_schema', 'partial_revokes', 'wsrep_on',
              'group_replication_group_name')
BASE_SQL = (TAG + "SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s);\n" %
            ','.join("'%s'" % name for name in _VARIABLES) + TAG +
            "SHOW GLOBAL STATUS WHERE Variable_name IN ('wsrep_cluster_status','wsrep_cluster_size');\n" +
            TAG + 'SHOW GRANTS;\n')


def _on(value):
    if value is None:
        return None
    return str(value).upper() in ('1', 'ON', 'YES', 'TRUE')


def read_readiness(conn):
    deadline = time.monotonic() + TIMEOUT

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise ConnectorError('Readiness check timed out after 3 s.')
        return budget

    text, errors = conn.run_script(BASE_SQL, timeout=remaining())
    variables, wsrep, grants = {}, {}, []
    for line in text.split('\n'):
        if not line:
            continue
        row = [unescape_mysql(cell) for cell in line.split('\t')]
        if len(row) == 2:
            key = row[0].lower()
            if key in _VARIABLES:
                variables[key] = row[1]
            elif key in ('wsrep_cluster_status', 'wsrep_cluster_size'):
                wsrep[key] = row[1]
        elif len(row) == 1:
            grants.append(row[0])
    version = variables.get('version')
    if not version:
        raise ConnectorError('The server did not return its readiness facts.')
    identity = product_identity(version, variables.get('version_comment', ''))
    maria = identity['engine'] == 'mariadb'
    ps = _on(variables.get('performance_schema'))
    model = privileges.parse_grants(grants, identity['engine'], version,
                                    _on(variables.get('partial_revokes')) is True,
                                    checked=bool(grants))
    gates = {name: privileges.has(model, privileges.normalize_privilege(name, identity['engine'])) for name in
             ('PROCESS', 'CONNECTION_ADMIN', 'CONNECTION ADMIN', 'SUPER', 'REPLICATION CLIENT', 'SLAVE MONITOR')}
    actions = {name: privileges.can(model, name) for name in ('sessions_all', 'kill', 'replication_status')}
    notes = []
    if not grants or errors:
        # Never return client stderr or raw SHOW GRANTS (MariaDB can include hashes).
        notes.append('Some readiness details could not be read with this account.')
    if ps is False:
        notes.append('Performance Schema is off; performance views are limited. See Server > Instruments.')
    if not maria and server_version_tuple(version) < (8, 0, 0):
        notes.append('MySQL 5.7 has no CONNECTION_ADMIN privilege; Kill uses SUPER.')
    galera = False
    if _on(variables.get('wsrep_on')) is True:
        state = wsrep.get('wsrep_cluster_status')
        galera = None if state is None else state.lower() in ('primary', 'non-primary')
    group = False if not variables.get('group_replication_group_name') else None
    replica = None

    def probe(sql):
        try:
            return conn.run(TAG + sql, extra_flags=['--comments'], timeout=remaining()).strip()
        except ConnectorError:
            return None

    if maria:
        # SHOW has no LIMIT; only configured channel metadata is enumerated.
        # This spelling works on 10.11 as well as newer MariaDB releases.
        answer = probe('SHOW ALL SLAVES STATUS')
        replica = None if answer is None else bool(answer)
    elif ps is True:
        # Internal Group Replication channels do not make this an async replica.
        answer = probe("SELECT 1 FROM performance_schema.replication_connection_configuration "
                       "WHERE CHANNEL_NAME NOT IN ('group_replication_applier','group_replication_recovery') LIMIT 1")
        replica = None if answer is None else bool(answer)
    elif actions['replication_status']['allowed'] is True:
        # SHOW has no LIMIT. It returns only configured channels, with the client
        # output cap and the same deadline; none of its columns leave this module.
        statement = 'SHOW REPLICA STATUS' if server_version_tuple(version) >= (8, 0, 22) else 'SHOW SLAVE STATUS'
        answer = probe(statement)
        replica = None if answer is None else bool(answer)
    if group is None and ps is True:
        answer = probe("SELECT 1 FROM performance_schema.replication_group_members "
                       "WHERE MEMBER_ID = @@server_uuid AND MEMBER_STATE <> 'OFFLINE' LIMIT 1")
        group = None if answer is None else bool(answer)
    if replica is None or group is None or galera is None:
        notes.append('Replication or group membership could not be verified with this account.')
    read_only = variables.get('read_only')
    if read_only is not None:
        read_only = str(read_only).upper() not in ('OFF', '0', 'FALSE')
    result = dict(identity, version=version, read_only=read_only,
                  super_read_only=_on(variables.get('super_read_only')), performance_schema=ps,
                  replica=replica, group_member=group, galera_member=galera,
                  privileges=gates, actions=actions, notes=notes, checked_at=now())
    return result


def response(payload):
    from .live import connection_options
    from .connector import MySQLConnection
    from .dbops import redact
    options = connection_options(payload.get('connection'))
    options.update(query_timeout=TIMEOUT, connect_timeout=TIMEOUT)
    try:
        with MySQLConnection(**options) as conn:
            return read_readiness(conn)
    except ConnectorError as exc:
        raise ConnectorError(redact(str(exc), options.get('password'))) from None
