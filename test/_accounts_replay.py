"""Replay of the live account recordings in test/fixtures/accounts.

Each fixture holds every statement privileges.read_privileges,
accounts.list_accounts and accounts.read_account sent to a throwaway server
and the exact client output (or error) it got back (see PROVENANCE.txt).
:class:`ReplayConn` answers by exact SQL text; any other statement fails the
test with "unexpected SQL", and a write never reaches it.
"""
import json
import re
from pathlib import Path

from myflames.connector import ConnectorError
from _reviewed_replay import evidence_options

FIXTURES = Path(__file__).parent / 'fixtures' / 'accounts'
SERVERS = ('mysql-8.0', 'mysql-8.4', 'mysql-9.7', 'mariadb-10.11', 'mariadb-11.4')


def load(server):
    return json.loads((FIXTURES / (server + '.json')).read_text(encoding='utf-8'))


class ReplayConn(object):
    host, port, user = '127.0.0.1', 3306, 'root'
    ssl_mode = ssl_ca = ssl_cert = ssl_key = ssl_cipher = None

    def __init__(self, calls, version):
        self.answers = {}
        for call in calls:
            # The historical recordings predate optional wsrep variable
            # discovery. Extend only the requested names, never their output.
            if "SHOW GLOBAL VARIABLES WHERE Variable_name IN (" in call['sql']:
                call = dict(call, sql=call['sql'].replace("'hostname', 'port')", "'hostname', 'port', 'wsrep_on', 'wsrep_node_name')"))
            self.answers.setdefault(call['sql'], call)
        self.version = version
        self.sent = []
        self.wire = []

    @evidence_options
    def run(self, sql, extra_flags=None, timeout=None):
        from _reviewed_replay import evidence_body, evidence_call
        body, marker = evidence_body(sql)
        if marker:
            self.wire.append(sql)
            values = {'version': self.version}
            for call in self.answers.values():
                for line in call.get('output', '').splitlines():
                    if '\t' in line:
                        key, value = line.split('\t', 1)
                        if key in ('server_id', 'hostname', 'port', 'server_uuid'):
                            values[key] = value
            return evidence_call(lambda: self.run(body, ['--comments'], timeout), marker, values)
        self.sent.append((sql, extra_flags))
        if sql == 'SELECT @@version':
            return self.version + '\n'
        call = self.answers.get(sql)
        if call is None:
            raise AssertionError('unexpected SQL: ' + sql)
        if 'error' in call:
            raise ConnectorError(call['error'])
        return call['output']

    def run_stdin(self, sql, extra_flags=None, timeout=None):
        raise AssertionError('a replay connection never writes: ' + sql)

    def server_version(self):
        return self.version

    def is_mariadb(self):
        return 'mariadb' in self.version.lower()

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def session(server, login):
    data = load(server)
    return ReplayConn(data['sessions'][login]['calls'], data['server']['version']), data


def reads(server):
    data = load(server)
    return ReplayConn(data['reads']['calls'], data['server']['version']), data


def secrets(data):
    """Every credential the recordings hold: MariaDB hashes, USING strings,
    and MySQL hashes printed as hex."""
    text = json.dumps(data)
    found = set(re.findall(r"\*[0-9A-F]{40}", text))
    found |= set(re.findall(r"USING '([^']+)'", text))
    found |= set(m for m in re.findall(r"AS 0x([0-9A-F]{16,})", text))
    return sorted(found)
