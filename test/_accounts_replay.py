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
            self.answers.setdefault(call['sql'], call)
        self.version = version
        self.sent = []

    def run(self, sql, extra_flags=None, timeout=None):
        self.sent.append((sql, extra_flags))
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
