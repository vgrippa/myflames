"""Synthetic same-login grants replies, separate from live recordings."""
import re

from _perf_replay import batch


UUID = '11111111-2222-3333-4444-555555555555'


def grants_batch(grants, account='mf_admin@localhost', login='mf_admin@client', version='8.4.11',
                 variables=None, after_variables=None, after_identity=None, server=None):
    """Concatenate each statement's result set in client batch-output order.

    Optional variables that an engine does not register produce no rows.
    Overrides still allow explicit incomplete reads and before/after drift.
    """
    before = {}
    if 'mariadb' not in version.lower():
        before['server_uuid'] = UUID
        match = re.match(r'(\d+)\.(\d+)\.(\d+)', version)
        if match and tuple(int(n) for n in match.groups()) >= (8, 0, 16):
            before['partial_revokes'] = 'OFF'
    before.update(variables or {})
    before = {k: v for k, v in before.items() if v is not None}
    after = dict(before)
    after.update(after_variables or {})
    after = {k: v for k, v in after.items() if v is not None}
    server = server or {}
    identity = [login, account, version, server.get('connection_id', '73'), server.get('server_id', '1'),
                server.get('hostname', 'fixture-db'), server.get('port', '3306')]
    ending = list(identity)
    for index, value in (after_identity or {}).items():
        ending[index] = value
    def snapshot(values):
        # SHOW GLOBAL VARIABLES and SHOW GLOBAL STATUS are separate results.
        variable_rows = sorted((k, v) for k, v in values.items() if k in ('server_uuid', 'partial_revokes'))
        status_rows = [['wsrep_gcomm_uuid', values['wsrep_gcomm_uuid']]] if 'wsrep_gcomm_uuid' in values else []
        return batch(variable_rows) + batch(status_rows)

    return (batch([['myflames_grants_begin'] + identity]) + snapshot(before) +
            batch([['myflames_grants_rows']]) + batch([[g] for g in grants]) +
            batch([['myflames_grants_end'] + ending]) + snapshot(after) + batch([['myflames_grants_done']]))
