"""Synthetic identity supplements for recordings made before the target guard.

Keep recorded writes unchanged. The adapter accepts the new prefix, records
its full input, and shifts the old client's diagnostics by its added line.
The supplements below are fake evidence, never claims about the live capture.
"""
import re

from _perf_replay import ReplayConn as RecordedReplayConn
from myflames.connector import ConnectorError

UUID = '11111111-2222-3333-4444-555555555555'
PREFIX = "SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g WHERE "


def supplement(calls):
    out = []
    for original in calls:
        call = list(original)
        sql, text = call[:2]
        variable = sql.startswith('/*myflames:variable*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN (')
        replication = (sql.startswith('/*myflames:replication*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN (')
                       and "'gtid_domain_id'" in sql)
        if variable or replication:
            match = re.search(r'Variable_name IN \(([^)]*)\)', sql)
            names = set(re.findall(r"'([a-z_]+)'", match.group(1)))
            names.update(('server_uuid', 'server_id', 'hostname', 'port', 'wsrep_node_name'))
            call[0] = sql[:match.start(1)] + ', '.join("'%s'" % name for name in sorted(names)) + sql[match.end(1):]
            if text:
                values = dict(line.split('\t', 1) for line in text.split('\n') if '\t' in line)
                additions = {'server_id': '1', 'hostname': 'fixture-db', 'port': '3306'}
                if 'mariadb' not in text.lower():
                    additions['server_uuid'] = UUID
                call[1] = ''.join('%s\t%s\n' % (key, value) for key, value in additions.items()
                                  if key not in values) + text
        out.append(call)
    return out


class ReplayConn(RecordedReplayConn):
    def __init__(self, calls, *args, **kwargs):
        super(ReplayConn, self).__init__(supplement(calls), *args, **kwargs)

    def run_write(self, sql, extra_flags=None, timeout=None):
        assert sql.startswith(PREFIX), 'write is missing the target guard: ' + sql
        guard, body = sql.split('\n', 1)
        assert guard.endswith(');'), guard
        start = len(self.calls)
        try:
            return self.run(body, extra_flags=extra_flags, timeout=timeout)
        except ConnectorError as exc:
            # The original recording has no guard line.
            raise ConnectorError(re.sub(r'\bat line (\d+)\b',
                                        lambda m: 'at line %d' % (int(m.group(1)) + 1), str(exc))) from None
        finally:
            if len(self.calls) > start:
                self.calls[-1] = (sql, list(extra_flags or []))
