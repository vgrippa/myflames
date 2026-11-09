"""Generate browser reports from synthetic memory fixtures, never live evidence."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / 'test')]
from test_memory import collect
from myflames import memory


def write(name, report):
    report['taken_at'] = '2026-11-08T18:00:00+00:00'
    (ROOT / 'apps/workspace/test/fixtures' / ('memory-' + name + '.json')).write_text(json.dumps(report, indent=2) + '\n')


write('ok', collect()[0])
write('watch', collect('mysql-9.7')[0])
def disabled_attribution(d):
    # Builtin global accounting can be present while all attributable
    # instruments are disabled, as on the coordinator's MariaDB lab server.
    d['sessions'] = [[52, 42, 'app', 'localhost', 0, 0, 0]]
    d['accounts'] = [['app', 'localhost', 0, 0, 0]]


write('not-collected', collect('mysql-5.7', edit=disabled_attribution)[0])
write('mariadb-fallback', collect('mariadb-11.8-off')[0])
# Performance Schema on: Sort without the sort buffer, Network not collected.
write('mariadb-on', collect('mariadb-11.8-on')[0])
write('denied', collect(denied=(memory.TOTAL_SQL, memory.COVERAGE_SQL, memory.CONSUMER_SQL,
                              memory.EVENT_SQL, memory.SESSION_SQL, memory.ACCOUNT_SQL))[0])


def hostile(d):
    d['accounts'][0][0] = '<img src=x onerror=alert(1)>\u202e'
    d['accounts'][0][1] = ''
    d['sessions'][0][2] = None


write('hostile', collect(edit=hostile)[0])
