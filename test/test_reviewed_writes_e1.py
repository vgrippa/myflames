"""Round E1 regressions: real binders/senders, synthetic packet transport.

The strict transport below models one COM_QUERY and stop-on-error, using
explicit synthetic identity values. It is not a live-server validation.
"""
import copy
import io
import re
import subprocess
import unittest
from unittest import mock
from xml.sax.saxutils import escape

from myflames import dbops, locks, privileges, replication, variables
from myflames.connector import ConnectorError, MySQLConnection, PartialReadError
from _reviewed_replay import evidence_body, evidence_call
import test_reviewed_writes_guard as guard_fixtures
from test_reviewed_writes_guard import MYSQL, UUID
from test_reviewed_writes_packets import RoutingTransport, StrictPacketServer
from _reviewed_replay import evidence_options

OTHER_UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def resultset(rows):
    return '<resultset>' + ''.join('<row>' + ''.join('<field name="%s">%s</field>' %
        (escape(key), escape(value)) for key, value in row) + '</row>' for row in rows) + '</resultset>\n'


class StrictEvidenceTransport(object):
    """Execute the actual prefix in order, stopping at an expected read error."""
    password = None

    def __init__(self, xml=False):
        self.xml = xml
        self.identity = dict(MYSQL, version='8.4.11')
        self.packets, self.executed, self.writes = [], [], []
        self.fail_read = True
        self.error = 'ERROR 1141 (42000) at line 2: There is no such grant'
        self.corrupt = None

    def run(self, script, extra_flags=None, **kwargs):
        header, payload = script.split('\n', 1)
        delimiter = header.split(' ', 1)[1]
        assert header.startswith('DELIMITER ')
        assert payload.count(delimiter) == 1 and payload.endswith(delimiter + '\n')
        assert '--skip-reconnect' in extra_flags and '--force' not in extra_flags
        self.packets.append(script)
        remaining, output = payload[:-len(delimiter) - 1], ''
        while remaining.strip():
            unit, remaining = StrictPacketServer.next_unit(remaining, True, 2, ())
            if unit is None:
                break
            sql = re.sub(r'^/\*myflames:[^\r\n]*?\*/ ', '', unit['text'])
            self.executed.append(sql)
            if sql.startswith('SHOW GLOBAL VARIABLES'):
                if self.xml:
                    output += resultset([[('Variable_name', k), ('Value', v)] for k, v in self.identity.items()])
                else:
                    output += ''.join('%s\t%s\n' % pair for pair in self.identity.items())
            elif sql.startswith('SHOW GLOBAL STATUS'):
                output += resultset([]) if self.xml else ''
            elif sql.startswith("SELECT 'myflames_evidence_"):
                marker = re.fullmatch(r"SELECT '(myflames_evidence_[0-9a-f]{32})'", sql).group(1)
                output += resultset([[('marker', marker)]]) if self.xml else marker + '\n'
            elif self.fail_read:
                assert len(self.executed) >= 4, 'the read ran before the full identity prefix'
                if self.corrupt:
                    output = self.corrupt(output)
                raise PartialReadError(self.error, output)
            else:
                output += resultset([[('value', 'visible')]]) if self.xml else 'visible\n'
        return self.corrupt(output) if self.corrupt else output

    def run_write(self, sql, **kwargs):
        self.writes.append(sql)
        return ''

    run_stdin = run_write


class PacketRouter(object):
    """Explicitly route each evidence packet, including nested readers."""
    def __init__(self, reader, node_for, answer=None):
        self.reader, self.node_for, self.answer = reader, node_for, answer
        self.packets, self.writes = [], []

    def __getattr__(self, name):
        return getattr(self.reader, name)

    @evidence_options
    def run(self, sql, extra_flags=None, **kwargs):
        body, marker = evidence_body(sql)
        assert marker is not None, 'a review read escaped the shared binder: ' + sql
        node = self.node_for(body, len(self.packets))
        self.packets.append((body, copy.deepcopy(node)))
        flags = [flag for flag in (extra_flags or []) if flag != '--skip-reconnect']
        def read():
            return self.answer(body, flags) if self.answer else self.reader.run(body, extra_flags=flags, **kwargs)
        return evidence_call(read, marker, node, '--xml' in flags)

    def run_write(self, sql, **kwargs):
        self.writes.append(sql)
        raise AssertionError('an inconsistent review sent a write')

    run_stdin = run_write


class EvidencePrefixTests(unittest.TestCase):
    def test_all_added_statements_use_the_callers_tag_or_the_binder_tag(self):
        from _reviewed_replay import read_statements
        for tag in ('', '/*myflames:privileges*/ ', '/*myflames:test:request-123*/ '):
            with self.subTest(tag=tag):
                transport = StrictEvidenceTransport()
                transport.fail_read = False
                dbops.TargetEvidenceConnection(transport).run(tag + 'SELECT 42')
                statements = read_statements(transport.packets[0])
                self.assertEqual(len(statements), 4)
                expected = tag or '/*myflames:target-evidence*/ '
                self.assertTrue(all(statement.startswith(expected) for statement in statements[:3]))
                self.assertEqual(statements[3], tag + 'SELECT 42')

    def test_refusal_details_keep_only_redacted_error_lines_without_sql_echoes(self):
        marker = 'myflames_evidence_' + 'a' * 32
        cases = (
            ('mysql client exited 1: login conn-secret failed', False),
            ('ERROR 1064 (42000) at line 2: conn-secret syntax near \'SELECT "%s"\' at line 1' % marker, True),
            ('--------------\nSELECT \'%s\'\n--------------\nERROR 1142 (42000) at line 2: conn-secret denied' % marker, True),
        )
        for password in (None, object(), 'conn-secret'):
            for diagnostic, error_line in cases:
                with self.subTest(password_type=type(password).__name__, diagnostic=diagnostic):
                    transport = mock.Mock(password=password)
                    transport.run.side_effect = ConnectorError(diagnostic)
                    bound = dbops.TargetEvidenceConnection(transport)
                    with self.assertRaises(ConnectorError) as caught:
                        bound.run('SELECT 42')
                    message = str(caught.exception)
                    self.assertEqual('ERROR ' in message, error_line)
                    self.assertNotIn('SELECT', message)
                    self.assertNotIn('near ', message)
                    self.assertNotIn(marker, message)
                    if isinstance(password, str):
                        self.assertNotIn(password, message)
                    self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
        self.assertEqual(dbops.target_error_text(marker), '[identity evidence]')

    def test_mcp_read_wrapper_forwards_partial_output_opt_in(self):
        from myflames.mcp_server import _ReadConnection
        transport = StrictEvidenceTransport()
        with mock.patch.object(transport, 'run', wraps=transport.run) as run:
            bound = dbops.TargetEvidenceConnection(_ReadConnection(transport))
            with self.assertRaisesRegex(ConnectorError, '1141'):
                bound.run('SHOW GRANTS FOR `absent`@`%`')
        self.assertTrue(run.call_args[1]['retain_partial_stdout'])
        self.assertIsNone(bound.problem)
        self.assertEqual(bound.variables, transport.identity)

    def test_connection_adapters_forward_partial_output_opt_in(self):
        import threading
        from myflames.lifecycle import _BoundedConnection
        from myflames.param_compare import BoundedConnection
        from myflames.report import _AttributionContext, _Guarded
        from myflames.schema_lint import _LiveRead
        for wrap in (_BoundedConnection, BoundedConnection, _AttributionContext, _LiveRead,
                     lambda conn: _Guarded(conn, threading.Event())):
            with self.subTest(wrapper=getattr(wrap, '__name__', repr(wrap))):
                transport = mock.Mock(query_timeout=30)
                transport.run.return_value = 'value\n'
                self.assertEqual(wrap(transport).run('SELECT 42', timeout=2, retain_partial_stdout=True), 'value\n')
                self.assertTrue(transport.run.call_args[1]['retain_partial_stdout'])
                self.assertGreater(transport.run.call_args[1]['timeout'], 0)
                self.assertLessEqual(transport.run.call_args[1]['timeout'], 2)

    def test_expected_read_error_preserves_identity_and_fallback_in_batch_and_xml(self):
        for xml in (False, True):
            with self.subTest(xml=xml):
                transport = StrictEvidenceTransport(xml)
                bound = dbops.TargetEvidenceConnection(transport)
                flags = ['--xml'] if xml else []
                with self.assertRaisesRegex(ConnectorError, '1141') as caught:
                    bound.run('SHOW GRANTS FOR `absent`@`%`;; -- terminal comment', extra_flags=flags)
                self.assertIsNone(bound.problem)
                self.assertEqual(bound.variables, transport.identity)
                self.assertNotIn('myflames_evidence_', str(caught.exception))
                self.assertFalse(hasattr(caught.exception, 'stdout'))
                transport.fail_read = False
                expected = resultset([[('value', 'visible')]]) if xml else 'visible\n'
                self.assertEqual(bound.run('SELECT 42', extra_flags=flags), expected)
                self.assertEqual(len(transport.packets), 2)

    def test_transport_or_unlocated_errors_stay_sticky_even_after_a_valid_prefix(self):
        for error in ('ERROR 2013 (HY000) at line 2: Lost connection',
                      'ERROR 1141 (42000): There is no such grant',
                      'ERROR 1141 (42000) at line 1: There is no such grant',
                      'Query timed out after 30s'):
            transport = StrictEvidenceTransport()
            transport.error = error
            bound = dbops.TargetEvidenceConnection(transport)
            with self.subTest(error=error), self.assertRaises(ConnectorError) as caught:
                bound.run('SHOW GRANTS FOR absent')
            self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
            for send in (bound.run, bound.run_write, bound.run_stdin):
                with self.assertRaises(ConnectorError):
                    send('SELECT 1')
            self.assertEqual(len(transport.packets), 1)
            self.assertEqual(transport.writes, [])

    def test_missing_conflicting_or_duplicate_identity_is_sticky_on_a_read_error(self):
        for kind in ('missing_marker', 'missing_uuid', 'duplicate_uuid', 'changed_uuid'):
            transport = StrictEvidenceTransport()
            bound = dbops.TargetEvidenceConnection(transport)
            transport.fail_read = False
            bound.run('SELECT 1')
            transport.fail_read = True
            if kind == 'missing_marker':
                transport.corrupt = lambda output: re.sub(r'myflames_evidence_[0-9a-f]{32}\n', '', output)
            elif kind == 'missing_uuid':
                transport.identity.pop('server_uuid')
            elif kind == 'duplicate_uuid':
                transport.corrupt = lambda output: 'server_uuid\t' + OTHER_UUID + '\n' + output
            else:
                transport.identity['server_uuid'] = OTHER_UUID
            with self.subTest(kind=kind), self.assertRaises(ConnectorError) as caught:
                bound.run('SHOW GRANTS FOR absent')
            self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
            with self.assertRaises(ConnectorError):
                bound.run_write('KILL 41')
            self.assertEqual(transport.writes, [])

    def test_real_connector_retains_prefix_privately_for_expected_error(self):
        from myflames.lifecycle import LifecycleConnection
        def client(argv, **kwargs):
            script = kwargs['input'].decode() if 'input' in kwargs else argv[argv.index('-e') + 1]
            try:
                output = transport.run(script, extra_flags=argv)
            except PartialReadError as exc:
                return subprocess.CompletedProcess(argv, 1, stdout=exc.stdout.encode('utf-8'), stderr=str(exc).encode('utf-8'))
            return subprocess.CompletedProcess(argv, 0, stdout=output.encode('utf-8'), stderr=b'')
        for connection_type in (MySQLConnection, LifecycleConnection):
            transport = StrictEvidenceTransport()
            with self.subTest(connection=connection_type.__name__), \
                    connection_type(host='fixture-db', binary='mysql') as conn, \
                    mock.patch.object(conn, '_run_client', side_effect=client):
                bound = dbops.TargetEvidenceConnection(conn)
                with self.assertRaisesRegex(ConnectorError, '1141'):
                    bound.run('SHOW GRANTS FOR absent')
                self.assertIsNone(bound.problem)
                transport.fail_read = False
                self.assertEqual(bound.run('SELECT 1'), 'visible\n')

    def test_read_error_discards_mariadb_echo_and_private_evidence(self):
        transport = StrictEvidenceTransport()
        transport.error = ('mysql client exited 1: --------------\n'
                           "SHOW GLOBAL VARIABLES; SELECT 'myflames_evidence_" + 'a' * 32 + "';\n"
                           'SHOW GRANTS FOR `absent`\n--------------\n'
                           'ERROR 1141 (42000) at line 2: There is no such grant')
        bound = dbops.TargetEvidenceConnection(transport)
        with self.assertRaises(ConnectorError) as caught:
            bound.run('SHOW GRANTS FOR `absent`')
        self.assertEqual(str(caught.exception), 'ERROR 1141 (42000) at line 2: There is no such grant')
        self.assertFalse(hasattr(caught.exception, 'stdout'))
        self.assertIsNone(bound.problem)

    def test_xml_identity_conflict_and_partial_prefix_are_sticky(self):
        for kind in ('duplicate_uuid', 'missing_marker'):
            transport = StrictEvidenceTransport(xml=True)
            if kind == 'duplicate_uuid':
                transport.corrupt = lambda text: text.replace('</resultset>',
                    '<row><field name="Variable_name">server_uuid</field><field name="Value">' +
                    OTHER_UUID + '</field></row></resultset>', 1)
            else:
                transport.corrupt = lambda text: text[:text.index('<field name="marker">')]
            bound = dbops.TargetEvidenceConnection(transport)
            with self.subTest(kind=kind), self.assertRaises(ConnectorError) as caught:
                bound.run('SHOW GRANTS FOR absent', extra_flags=['--xml'])
            self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
            with self.assertRaises(ConnectorError):
                bound.run_write('KILL 41')
            self.assertEqual(transport.writes, [])

    def test_strict_server_receives_no_empty_read_or_write_units(self):
        for tail in (';', ';;', '; -- terminal comment', ';;; /* closed */', '; # comment\n;'):
            with self.subTest(tail=tail):
                transport = StrictEvidenceTransport()
                transport.fail_read = False
                result = dbops.TargetEvidenceConnection(transport).run('SELECT 1' + tail + '\nSELECT 2' + tail)
                self.assertEqual(result, 'visible\nvisible\n')
                self.assertEqual([dbops.split_statements(sql)[0]['code'] for sql in transport.executed[-2:]],
                                 ['SELECT 1', 'SELECT 2'])
                statements = ['SET @one = 1' + tail, ('SET @two = 2' + tail,)]
                script, lines = dbops.target_guarded_script(dbops.target_session_guard('mysql', MYSQL), statements)
                writer = RoutingTransport([MYSQL])
                writer.run_write(script)
                self.assertEqual(writer.executed, [(MYSQL, 'SET @one = 1'), (MYSQL, 'SET @two = 2')])
                self.assertEqual(lines.indices, (0, 1))


class ReviewScopeTests(unittest.TestCase):
    setUp = guard_fixtures.GuardTests.setUp
    audit = guard_fixtures.GuardTests.audit
    records = guard_fixtures.GuardTests.records

    def test_variable_grants_cannot_come_from_a_different_server(self):
        from test_variables import replay
        reader, _ = replay('mysql-8.4', 'global')
        first = dict(reader.evidence_values)
        second = dict(first, server_uuid=OTHER_UUID)
        router = PacketRouter(reader, lambda body, n: second if body == variables.TAG + 'SHOW GRANTS' else first)
        with self.assertRaises(ConnectorError) as caught:
            variables.plan_change(router, 'max_connections', '300', 'global')
        self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
        self.assertEqual(router.packets[-1], (variables.TAG + 'SHOW GRANTS', second))
        self.assertEqual(router.writes, [])

    def test_privilege_role_flags_share_the_initial_grants_identity(self):
        import _accounts_replay
        reader, _ = _accounts_replay.session('mysql-8.4', 'acct_app')
        first = dict(MYSQL, version=reader.version)
        second = dict(first, server_uuid=OTHER_UUID)
        router = PacketRouter(reader, lambda body, n: first if n == 0 else second)
        with self.assertRaises(ConnectorError) as caught:
            privileges.read_privileges(router)
        self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
        self.assertEqual(len(router.packets), 2)
        self.assertIn('APPLICABLE_ROLES', router.packets[-1][0])
        self.assertEqual(router.writes, [])

    def test_skip_status_privileges_and_final_status_use_one_scope(self):
        from test_replication import replay, status
        for stage in ('privileges', 'final_status'):
            report, _ = status('mysql-8.4-gtid-error')
            channel = report['channels'][0]['name']
            plan = replication.plan_skip(report, channel)
            reader, _ = replay('replication', 'mysql-8.4-gtid-error')
            first = dict(reader.evidence_values)
            second = dict(first, server_uuid=OTHER_UUID)
            grants_seen = []
            def node_for(body, n):
                if 'myflames_who' in body:
                    grants_seen.append(True)
                changed = bool(grants_seen) and (stage == 'privileges' or 'myflames_who' not in body)
                return second if changed else first
            def answer(body, flags):
                if 'myflames_who' in body:
                    return ('myflames_who\tadmin@localhost\tadmin@localhost\tNONE\t8.4.11\n'
                            'GRANT ALL PRIVILEGES ON *.* TO `admin`@`localhost` WITH GRANT OPTION\n')
                if 'APPLICABLE_ROLES' in body:
                    return ''
                return reader.run(body, extra_flags=flags)
            # Keep the optional role-flags read on A in the final-status case,
            # so this test reaches the recheck after the complete grants read.
            choose = node_for
            if stage == 'final_status':
                def choose(body, n):
                    if 'APPLICABLE_ROLES' in body:
                        return first
                    return node_for(body, n)
            router = PacketRouter(reader, choose, answer)
            with self.subTest(stage=stage), self.assertRaises(ConnectorError) as caught:
                replication.skip_one(router, channel, plan['identity'], confirmed=True, settle_s=0, audit=self.audit())
            self.assertEqual(caught.exception.identity_outcome, 'identity_unavailable')
            self.assertTrue(grants_seen)
            self.assertEqual(router.writes, [])
            self.assertEqual(self.records()[0]['status'], 'refused')
            if stage == 'final_status':
                self.assertIn(replication.REPLICATION_TAG + 'SHOW GLOBAL VARIABLES', router.packets[-1][0])

    def test_kill_cli_review_cannot_mix_report_and_target_nodes(self):
        from test_locks import KillScenario
        scenario = KillScenario()
        first, second = dict(MYSQL, version='8.4.6'), dict(server_uuid=OTHER_UUID, version='8.4.6')
        router = PacketRouter(scenario.conn, lambda body, n: second if 'CONNECTION_ID()' in body else first)
        stdin = mock.Mock()
        stdin.readline.side_effect = AssertionError('an inconsistent review must not prompt')
        code, result = locks.kill_cli(router, 41, stdin=stdin, stderr=io.StringIO(), audit=self.audit())
        self.assertEqual((code, result['status'], result['outcome'], result['sent']),
                         (1, 'refused', 'identity_unavailable', []))
        self.assertEqual(router.writes, [])
        self.assertIn('CONNECTION_ID()', router.packets[-1][0])
        self.assertEqual(self.records()[0]['outcome'], 'identity_unavailable')
        stdin.readline.assert_not_called()

    def test_skip_loop_receiver_callback_uses_the_same_node_scope(self):
        from test_replication import replay
        from _perf_replay import FakeTime
        for start_worker in (False, True):
            with self.subTest(start_worker=start_worker):
                reader, _ = replay('replication', 'mysql-8.4-gtid-error')
                first = dict(reader.evidence_values)
                second = dict(first, server_uuid=OTHER_UUID)
                receiver_sql = 'SELECT 1 /* synthetic receiver evidence */'
                router = PacketRouter(reader, lambda body, n: second if body == receiver_sql else first,
                                      lambda body, flags: '1\n' if body == receiver_sql else reader.run(body, extra_flags=flags))
                def receiver(conn, report):
                    conn.run(receiver_sql)
                    return ((1, 'version', 1),)
                clock = FakeTime()
                loop = replication.SkipLoop(router, '', [1062], 1, audit_factory=self.audit,
                                            receiver_reader=receiver, clock=clock.monotonic, sleep=clock.sleep, settle_s=0)
                if start_worker:
                    with mock.patch.object(replication.threading, 'Thread') as thread:
                        self.assertIs(loop.start(), loop)
                        thread.assert_not_called()
                    result = loop.status()
                else:
                    result = loop.run()
                self.assertEqual(result['skips_done'], 0)
                self.assertEqual(router.writes, [])
                self.assertEqual(router.packets[-1], (receiver_sql, second))
                self.assertIn('identity check could not run', result['message'])

    def test_galera_identity_without_node_uuid_is_incomplete_for_both_engines(self):
        for engine in ('mysql', 'mariadb'):
            keys, _ = locks._identity_projection(engine)
            values = dict(MYSQL, server_id='1', hostname='one', port='3306', version='11.4.13-MariaDB', wsrep_on='ON')
            for missing in (None, ''):
                values['wsrep_gcomm_uuid'] = missing
                with self.subTest(engine=engine, missing=missing):
                    self.assertEqual(locks._identity_row(keys, [values.get(key) for key in keys]), {})
            values['wsrep_gcomm_uuid'] = UUID
            identity = locks._identity_row(keys, [values.get(key) for key in keys])
            self.assertEqual(identity['wsrep_gcomm_uuid'], UUID)
            self.assertTrue(dbops.target_session_guard(engine, identity))


class DollarReadingTests(unittest.TestCase):
    def test_code_drift_is_refused_even_with_identical_closed_unit_spans(self):
        for tag in ('$q$', '$9_tag$', '$é$', '$标签$', '$e\u0301$', '$💡$'):
            sql = 'DELETE FROM t WHERE id = 1 AND %s = 1 /* %s OR 1=1 -- */' % (tag, tag)
            for escaping in (None, True, False):
                with self.subTest(tag=tag, escaping=escaping):
                    plain = dbops.split_statements(sql)
                    modern = dbops.split_statements(sql, dollar_quotes=True)
                    self.assertEqual([(u['start'], u['end']) for u in plain], [(u['start'], u['end']) for u in modern])
                    self.assertNotEqual(plain[0]['code'], modern[0]['code'])
                    verdict = dbops.classify(sql, backslash_escapes=escaping)
                    self.assertEqual(verdict['kind'], 'risky')
                    self.assertEqual(verdict['statements'][0]['kind'], 'risky')
                    self.assertTrue(verdict['ambiguous'])
                    with self.assertRaisesRegex(ValueError, 'dollar-quote'):
                        dbops.target_guarded_script(dbops.target_session_guard('mysql', MYSQL), [sql],
                                                   backslash_escapes=escaping)

    def test_multibyte_identifier_boundaries_and_quoted_tags_are_not_dollar_bodies(self):
        for sql in ('SELECT é$q$', 'SELECT 标签$q$', 'SELECT e\u0301$q$', 'SELECT 9$q$',
                    "SELECT '$标签$'", 'SELECT `$标签$`', 'SELECT 1 /* $标签$ */'):
            with self.subTest(sql=sql):
                self.assertFalse(dbops.classify(sql)['ambiguous'])
                self.assertEqual(dbops.target_statement(sql), sql)
