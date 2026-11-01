"""Replay of recorded schema version reads (test/fixtures/schema_versions).

``capture-<server>-<which>.json`` holds every client call one live
``schema_versions.capture`` sent (``run`` and ``run_script``) with what the
client printed, recorded on 2026-10-26 from throwaway MySQL 8.4.11 and MariaDB
11.4.13 containers: ``before`` (database sv_m) and ``after`` (sv_b, changed
from sv_m) read by root, and ``limited`` (sv_b read by an account with only
SELECT, SHOW VIEW and EXECUTE on sv_b.*: routine bodies unreadable, triggers
and events not listed). ``<server>-<which>.json`` is the snapshot each read
produced. Fixture names keep their recorded before/after names: before is
the sync Target and after is the Source. Container host names are synthetic.
"""
import json
import os

from myflames import schema_versions
from myflames.connector import ConnectorError

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'schema_versions')
SERVERS = ('mysql-8.4', 'mariadb-11.4')
WHICH = ('before', 'after', 'limited')


def load(name):
    with open(os.path.join(FIXTURES, name + '.json'), encoding='utf-8') as handle:
        return json.load(handle)


def snapshot(server, which):
    """A recorded snapshot, validated as the store would read it back."""
    return schema_versions.validate_snapshot(load('%s-%s' % (server, which)))


def retained_child_pair(server):
    """Snapshots reproducing BEFORE/CHANGES' parent2/child2/child3 case.

    The recorded snapshots predate this addition to the live fixture. Keep
    their other objects, and add the server-normalized definitions here.
    """
    target, source = snapshot(server, 'before'), snapshot(server, 'after')
    def table(name, elements):
        return {'type': 'table', 'name': name, 'columns': {},
                'ddl': 'CREATE TABLE `%s` (\n  %s\n) ENGINE=InnoDB' % (name, ',\n  '.join(elements))}
    target['objects'].extend([
        table('parent2', ['`id` int NOT NULL', 'PRIMARY KEY (`id`)']),
        table('child2', ['`pid` int NOT NULL', 'KEY `k_pid` (`pid`)',
                         'CONSTRAINT `fk_c2` FOREIGN KEY (`pid`) REFERENCES `parent2` (`id`)'])])
    source['objects'].extend([
        table('parent2', ['`id` bigint NOT NULL', 'PRIMARY KEY (`id`)']),
        table('child3', ['`pid` bigint NOT NULL', 'KEY `k_pid` (`pid`)',
                         'CONSTRAINT `fk_c2` FOREIGN KEY (`pid`) REFERENCES `parent2` (`id`)'])])
    return schema_versions.validate_snapshot(source), schema_versions.validate_snapshot(target)


def recording(server, which):
    return load('capture-%s-%s' % (server, which))


class ReplayConn(object):
    """Answers the recorded statements; anything else raises ConnectorError
    ``unexpected SQL``. *edit(method, sql, call)* may return a changed copy
    of a recorded answer (another server state); *error* makes every call
    raise it."""

    def __init__(self, record, edit=None, error=None):
        self.record = record
        self.edit = edit
        self.error = error
        self.sent = []

    def _answer(self, method, sql, flags):
        self.sent.append((method, sql, list(flags or [])))
        if self.error is not None:
            raise self.error
        for call in self.record['calls']:
            if call['method'] == method and call['sql'] == sql:
                return self.edit(method, sql, dict(call)) if self.edit else call
        raise ConnectorError('unexpected SQL: %s' % sql[:300])

    def run(self, sql, extra_flags=None, timeout=None):
        return self._answer('run', sql, extra_flags)['stdout']

    def run_script(self, sql, extra_flags=None, timeout=None):
        call = self._answer('run_script', sql, extra_flags)
        return call['stdout'], call['stderr']

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
