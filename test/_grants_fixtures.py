"""Synthetic same-login grants replies, separate from live recordings."""
from _perf_replay import batch


UUID = '11111111-2222-3333-4444-555555555555'


def grants_batch(grants, account='mf_admin@localhost', login='mf_admin@client', version='8.4.11',
                 variables=None, after_variables=None, after_identity=None):
    """The client's framed rows, with independent before/after identity data."""
    before = {'server_uuid': UUID, 'partial_revokes': 'OFF'}
    before.update(variables or {})
    before = {k: v for k, v in before.items() if v is not None}
    after = dict(before)
    after.update(after_variables or {})
    after = {k: v for k, v in after.items() if v is not None}
    identity = [login, account, version, '73', '1', 'fixture-db', '3306']
    ending = list(identity)
    for index, value in (after_identity or {}).items():
        ending[index] = value
    return batch([['myflames_grants_begin'] + identity] + sorted(before.items()) +
                 [['myflames_grants_rows']] + [[g] for g in grants] +
                 [['myflames_grants_end'] + ending] + sorted(after.items()) + [['myflames_grants_done']])
