"""Complete grant catalogs from recorded SHOW PRIVILEGES, with synthetic targets.

The recordings are real server evidence. Account state and object names are
explicitly synthetic, so the browser can exercise every checkbox without writes.
"""
import json
from pathlib import Path
from myflames import accounts, privileges
from test_accounts import FakeConn, actor

FIXTURES = Path(__file__).parent / 'fixtures/accounts'

def cases():
    result = []
    for path in sorted(FIXTURES.glob('show-privileges-*.json')):
        data = json.loads(path.read_text(encoding="utf-8"))
        server = data['server']
        call = next(c for c in data['sessions']['root']['calls'] if c['sql'] == 'SHOW PRIVILEGES')
        rows = [line.split('\t') for line in call['output'].splitlines()]
        catalog, meta = privileges.privilege_catalog(server['engine'], server['version'], rows)
        result.append(dict(id=path.stem, engine=server['engine'], version=server['version'],
                           catalog=catalog, meta=meta, rows=rows))
    return result

def scope(level, kind='PROCEDURE', anonymous=False):
    if level == 'global': return dict(level=level)
    if level == 'proxy': return dict(level=level, proxy=dict(name='' if anonymous else 'proxied', host='%'))
    value = dict(level=level, database='grant_db')
    if level in ('table', 'column'): value['table'] = 'grant_table'
    if level == 'column': value['columns'] = ['grant_column']
    if level == 'routine': value.update(routine='grant_routine', kind=kind)
    return value

def statement(case, level, kind='PROCEDURE', names=None, role=False, grant_option=False, anonymous=False):
    names = names or [p['name'] for p in case['catalog'][level]]
    params = scope(level, kind, anonymous)
    target = accounts._scope(case['engine'], False, **params)
    named = ', '.join(p + (' (`grant_column`)' if level == 'column' else '') for p in names)
    return ('GRANT ' + named + ' ON ' + target + ' TO ' + accounts._name('subject', '%', case['engine'], role)
            + (' WITH GRANT OPTION' if grant_option else ''))

def plan(case, request, present=False):
    level = request.get('level', 'global')
    grants = [statement(case, level, request.get('kind', 'PROCEDURE'), role=request.get('role', False),
                        anonymous=request.get('proxy', {}).get('name') == '')] if present else ['GRANT USAGE ON *.* TO `subject`@`%`']
    if request['operation'] == 'roles' and present:
        grants.append('GRANT ' + accounts._name('r', '%', case['engine'], True) + ' TO ' + accounts._name('subject', '%', case['engine'], request.get('role',False)))
    conn = FakeConn(case['engine'], name='subject', grants=grants)
    conn.version = case['version']
    original = conn.run
    def read(sql, extra_flags=None, timeout=None):
        if sql == accounts.TAG + 'SHOW PRIVILEGES':
            return ''.join('\t'.join(row) + '\n' for row in case['rows'])
        return original(sql, extra_flags, timeout)
    conn.run = read
    options = {k:v for k,v in request.items() if k not in ('connection', 'user', 'host', 'operation', 'role')}
    value = accounts.plan_change(conn, request['operation'], 'subject', role=request.get('role', False),
                                actor=actor(case['engine'], grants=['GRANT ALL PRIVILEGES ON *.* TO `admin`@`localhost` WITH GRANT OPTION']),
                                **options)
    value['plan_id'] = 'synthetic-reviewed-plan'
    return dict(value)

if __name__ == '__main__':
    import sys
    if '--plan' in sys.argv:
        payload = json.load(sys.stdin)
        case = next(c for c in cases() if c['id'] == payload['case'])
        print(json.dumps(plan(case, payload['request'], payload.get('present', False))))
    else:
        print(json.dumps(cases()))
