"""Replay helpers for the Performance workspace view tests (waits, indexes,
io, config).

fixtures/perf/<view>-<server>.json hold every statement one live run sent and
the ``mysql --batch`` text it got back, recorded from throwaway Docker
containers (MySQL 8.4.11, 9.7.2 and 26.7.0; MariaDB 11.4.13 and 13.0.2 with
Performance Schema on, and with the defaults; one MariaDB 11.4 with
``userstat=ON``) while a query loop and a metadata lock wait ran, over a
schema with an unused index, a redundant index, a foreign key and full scans.
:class:`ReplayConn` answers the same statements in the same order, so the
reports can be recomputed without a server.
"""
import contextlib
import io
import json
import os
import re
import sys
from unittest import mock

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'perf')
MYSQL = ('mysql-8.4', 'mysql-9.7', 'mysql-26.7')
MARIADB_PS = ('mariadb-11.4', 'mariadb-13.0')
MARIADB_OFF = ('mariadb-11.4-userstat', 'mariadb-13.0-default')
SERVERS = MYSQL + MARIADB_PS + MARIADB_OFF
PASSWORD = 'hunter2-not-real'
WRITE_WORDS = re.compile(r'\b(UPDATE|SET|INSERT|DELETE|REPLACE|KILL|GRANT|FLUSH|ALTER|CREATE|DROP|TRUNCATE)\b', re.I)


def fixture(view, server):
    with open(os.path.join(FIXTURES, '%s-%s.json' % (view, server)), encoding='utf-8') as handle:
        return json.load(handle)


def batch(rows):
    """Render rows like ``mysql --batch --skip-column-names``."""
    def cell(value):
        if value is None:
            return 'NULL'
        return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t')
    return ''.join('\t'.join(cell(v) for v in row) + '\n' for row in rows)


class FakeTime(object):
    """Stands in for the time module: sleep() advances monotonic()."""

    def __init__(self, query_s=0.05):
        self.now = 1000.0
        self.query_s = query_s
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class _Refused(str):
    """A recorded server error (replayed as ConnectorError)."""


class ReplayConn(object):
    """Answers recorded statements in order; *edit(sql, text, n)* may change
    the n-th answer to a statement. Unknown SQL fails the test."""

    def __init__(self, calls, clock=None, error=None, edit=None, version=''):
        self.answers = {}
        for call in calls:
            # [sql, text], or [sql, None, error] for a statement the server refused.
            sql, text = call[0], call[1]
            if len(call) > 2 and call[2] is not None:
                text = _Refused(call[2])
            self.answers.setdefault(sql, []).append(text)
        self.seen = {}
        self.clock = clock
        self.error = error
        self.edit = edit
        self.version = version
        self.calls = []

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or [])))
        if self.error is not None:
            raise self.error
        if self.clock is not None:
            self.clock.now += self.clock.query_s
        answers = self.answers.get(sql)
        if not answers:
            raise AssertionError('unexpected SQL: ' + sql)
        n = self.seen.get(sql, 0)
        self.seen[sql] = n + 1
        text = answers[min(n, len(answers) - 1)]
        if self.edit is not None:
            text = self.edit(sql, text, n)
        if isinstance(text, _Refused):
            from myflames.connector import ConnectorError
            raise ConnectorError(str(text))
        return text

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


class FakeFactory(object):
    def __init__(self, conn):
        self.conn = conn
        self.options = None

    def __call__(self, **options):
        self.options = options
        return self.conn


def replay(view, server, clock=None, edit=None, error=None):
    data = fixture(view, server)
    return ReplayConn(data['calls'], clock=clock, edit=edit, error=error), data


def assert_read_only(case, conn, tag):
    """Only SELECT and SHOW statements, each tagged and kept with --comments."""
    case.assertTrue(conn.calls, 'no SQL was sent')
    for sql, flags in conn.calls:
        case.assertTrue(sql.startswith(tag), sql)
        body = sql[len(tag):]
        case.assertRegex(body, r'^(SELECT|SHOW GLOBAL) ', sql)
        case.assertIn('--comments', flags, sql)
        case.assertNotRegex(body.replace('SHOW GLOBAL', ''), WRITE_WORDS)
        case.assertNotIn('@@', body, 'read variables with SHOW GLOBAL VARIABLES, never SELECT @@')


def run_cli(command, argv, conn, patches=()):
    """Run a workspace_cli command with a fake connection: (code, out, err, factory)."""
    from myflames import workspace_cli
    factory = FakeFactory(conn)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch('myflames.connector.MySQLConnection', factory))
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            getattr(workspace_cli, 'cmd_' + command)(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue(), factory


def run_main(argv, conn=None, patches=()):
    """Run ``myflames ...`` through cli.main: (code, out, err)."""
    from myflames import cli
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(sys, 'argv', argv))
        if conn is not None:
            stack.enter_context(mock.patch('myflames.connector.MySQLConnection', FakeFactory(conn)))
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def no_nan(case, report):
    json.dumps(report, allow_nan=False)
