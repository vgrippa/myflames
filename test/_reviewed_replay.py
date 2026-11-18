"""Synthetic identity supplements for recordings made before the target guard.

Keep recorded writes unchanged. The adapter unwraps guarded query packets,
records their full input, and maps recorded errors to packet start lines.
The supplements below are fake evidence, never claims about the live capture.
"""
import re

from _perf_replay import ReplayConn as RecordedReplayConn
from myflames.connector import ConnectorError
from myflames import dbops

UUID = '11111111-2222-3333-4444-555555555555'
PREFIX = "SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g WHERE "


def guarded_packets(sql):
    """Split the client's custom delimiter, which occurs nowhere in SQL.

    This is a fake transport, not the production script builder. Checking
    every buffer's guard catches an unguarded packet in replay fixtures too.
    """
    header, body = sql.split('\n', 1)
    assert header.startswith('DELIMITER '), sql
    delimiter = header[len('DELIMITER '):]
    chunks = body.split(delimiter + '\n')
    assert chunks[-1] == '', sql
    out, line = [], 2
    for chunk in chunks[:-1]:
        assert chunk.startswith(PREFIX), chunk
        guard, separator, statement = chunk.partition('; ')
        assert separator and guard.endswith(')'), chunk
        marker = re.match(r"SELECT '(myflames_guard_ok_[0-9a-f]{32}_[0-9]+)'; ", statement)
        assert marker, chunk
        statement = statement[marker.end():]
        assert chunk.endswith('\n'), chunk
        out.append((guard + ';', statement[:-1], line))
        line += chunk.count('\n') + 1
    return out


def recorded_body(sql):
    packets = guarded_packets(sql)
    statements = [p[1] for p in packets]
    if any(s.startswith('/*myflames:replication-skip*/ ') for s in statements):
        units = [unit['text'] for body in statements for unit in dbops.split_statements(body)]
        return ';\n'.join(units) + ';'
    if any(s.startswith('/*myflames:variable*/ ') for s in statements):
        units = [unit['text'] for body in statements for unit in dbops.split_statements(body)]
        return '; '.join(units)
    return statements[0] if len(statements) == 1 else ';\n'.join(statements) + ';'


def packet_error(exc, sql):
    """Put an old fixture's statement-index diagnostic on its packet."""
    packets = guarded_packets(sql)
    starts = []
    for _, body, start in packets:
        starts.extend([start] * len(dbops.split_statements(body)))
    original = re.search(r'\bat line (\d+)\b', str(exc))
    packet = (next((i for i, p in enumerate(packets) if p[2] == starts[int(original.group(1)) - 1]), 0)
              if original and 1 <= int(original.group(1)) <= len(starts) else 0)
    def shift(match):
        index = int(match.group(1)) - 1
        return 'at line %d' % (starts[index] if 0 <= index < len(starts) else 0)
    out = ConnectorError(re.sub(r'\bat line (\d+)\b', shift, str(exc)))
    # Recorded errors came from the protected SQL, not the new guard.
    markers = re.findall(r"SELECT '(myflames_guard_ok_[0-9a-f]{32}_[0-9]+)';", sql)
    out.guard_markers = tuple(markers[:packet + 1])
    return out


def acknowledged_error(exc, sql):
    """An error from SQL after the guard, already at a packet start line."""
    packets = guarded_packets(sql)
    location = re.search(r'\bat line (\d+)\b', str(exc))
    if location:
        packet = next((i for i, p in enumerate(packets) if p[2] == int(location.group(1))), None)
        if packet is not None:
            markers = re.findall(r"SELECT '(myflames_guard_ok_[0-9a-f]{32}_[0-9]+)';", sql)
            exc.guard_markers = tuple(markers[:packet + 1])
    return exc


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
            names.update(('server_uuid', 'server_id', 'hostname', 'port', 'wsrep_node_name', 'wsrep_on'))
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
        body = recorded_body(sql)
        start = len(self.calls)
        try:
            return self.run(body, extra_flags=extra_flags, timeout=timeout)
        except ConnectorError as exc:
            raise packet_error(exc, sql) from None
        finally:
            if len(self.calls) > start:
                self.calls[-1] = (sql, list(extra_flags or []))
