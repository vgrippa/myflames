"""Query-packet identity binding, with a proxy that changes backend per packet.

No database is contacted. Policy, guard construction, error mapping, progress
and audit are production code; the client and two server identities are fake.
"""
import ast
import copy
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

from myflames import accounts, dbops, privileges, replication, reviewed_writes
from myflames.connector import ConnectorError
from _reviewed_replay import guarded_packets
import test_reviewed_writes_guard as guard_fixtures
from test_reviewed_writes_guard import MYSQL, MARIA, UUID, Writer


class RoutingTransport(object):
    """Model DELIMITER, COM_QUERY routing and stop-on-error on both sides.

    A delimiter chosen by production is absent from every statement, so a
    literal split matches the client's quote-aware split. SQL within a packet
    is parsed separately by the real splitter. The small fake server evaluates
    just the identity comparisons emitted by target_session_guard.
    """
    password = None

    def __init__(self, backends):
        self.backends = backends
        self.packets = []
        self.executed = []

    def run_write(self, script, **kwargs):
        delimiter, body, first_line = ';', script, 1
        if script.startswith('DELIMITER '):
            header, body = script.split('\n', 1)
            delimiter, first_line = header.split(' ', 1)[1], 2
        offset = 0
        for chunk in body.split(delimiter):
            payload = chunk.strip()
            if not payload:
                continue
            # Account for the newline following the previous delimiter.
            line = first_line + body[:offset].count('\n') + len(chunk) - len(chunk.lstrip('\n'))
            backend = self.backends[len(self.packets) % len(self.backends)]
            self.packets.append((backend, payload))
            for unit in dbops.split_statements(payload, backslash_escapes=False):
                sql = unit['text'].strip().rstrip(';')
                if sql.startswith('SET @myflames_target = '):
                    differences = []
                    for key, quoted, integer in re.findall(
                            r"@@global\.([a-z_]+) <> (?:'([^']*)'|([0-9]+))", sql):
                        differences.append(str(backend.get(key)) != (quoted or integer))
                    node = re.search(r"VARIABLE_NAME = 'wsrep_gcomm_uuid'\) <=> '([^']+)'", sql)
                    if node:
                        differences.append(backend.get('wsrep_gcomm_uuid') != node.group(1))
                    if any(differences):
                        raise ConnectorError('ERROR 1242 (21000) at line %d: Subquery returns more than 1 row' % line)
                else:
                    self.executed.append((backend, sql))
            offset += len(chunk) + len(delimiter)
        return ''


class PacketTests(unittest.TestCase):
    setUp = guard_fixtures.GuardTests.setUp
    audit = guard_fixtures.GuardTests.audit
    records = guard_fixtures.GuardTests.records
    plan = guard_fixtures.GuardTests.plan

    def test_changed_modules_keep_python37_syntax(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / 'myflames' / (name + '.py') for name in
                 ('dbops', 'reviewed_writes', 'accounts', 'editor_session', 'locks', 'variables', 'replication')]
        paths += [root / 'test' / (name + '.py') for name in
                  ('_reviewed_replay', 'test_ui_group_consent', 'test_cluster_group_actions', 'test_variables',
                   'test_replication', 'test_locks', 'test_cluster_actions', 'test_editor_process',
                   'test_editor_session', 'test_editor_session_identity', 'test_reviewed_writes_guard',
                   'test_reviewed_writes_packets', 'test_accounts', 'test_cluster_privileges',
                   'test_cli_live_regressions', 'test_sql_cli', 'test_backend_kill_review')]
        for path in paths:
            with self.subTest(file=path.name):
                ast.parse(path.read_text(encoding='utf-8'),
                          **({'feature_version': 7} if sys.version_info >= (3, 8) else {}))

    def test_shape_preserves_multiline_sql_comments_and_avoids_first_candidate(self):
        guard = dbops.target_session_guard('mysql', MYSQL)
        statements = ['SET GLOBAL max_connections =\n 200;', "SET @note = '$$';", 'SET @other = 1 -- comment']
        script, lines = dbops.target_guarded_script(guard, statements)
        self.assertEqual(script.splitlines()[0], 'DELIMITER //')
        packets = guarded_packets(script)
        self.assertEqual([(g, s) for g, s, _ in packets], [(guard[0], s) for s in statements])
        self.assertEqual(lines, tuple(line for _, _, line in packets))
        self.assertEqual(lines, (2, 5, 7))
        self.assertEqual(script.count(guard[0]), len(statements))

    def test_all_delimiters_colliding_refuses_before_token_consumption_or_transport(self):
        plan = self.plan()
        plan['statements'] = ["SET @note = '$$ // __mf_end_1__ __mf_end_2__';"]
        writer, consume = Writer(), mock.Mock()
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test',
                                      before_send=consume, audit=self.audit())
        self.assertEqual((out['status'], out['states'], writer.writes), ('refused', ['not_sent'], []))
        self.assertIn('No safe SQL packet delimiter', out['message'])
        self.assertIn('Nothing was sent', out['message'])
        consume.assert_not_called()
        self.assertEqual(self.records()[0]['status'], 'refused')

    def test_accounts_delimiter_collision_is_redacted_and_sends_nothing(self):
        writer = Writer()
        secret = '$$ // __mf_end_1__ __mf_end_2__'
        sql = dbops.target_session_guard('mysql', MYSQL)[0] + "\nALTER USER app IDENTIFIED BY '%s';" % secret
        with self.assertRaisesRegex(ConnectorError, 'Nothing was sent') as caught:
            accounts._run_stdin(writer, sql, (secret,))
        self.assertEqual(writer.writes, [])
        self.assertNotIn(secret, str(caught.exception))

    def test_account_password_setup_is_acknowledged_before_the_guarded_change(self):
        writer = Writer()
        body = accounts._with_password_mode("ALTER USER `app`@`%` IDENTIFIED BY 'fixture-secret'")
        guard = dbops.target_session_guard('mysql', MYSQL)
        accounts._run_stdin(writer, guard[0] + '\n' + body, ('fixture-secret',))
        setup, mode, change = body.split('\n', 2)
        self.assertEqual([(g, s) for g, s, _ in guarded_packets(writer.writes[0])],
                         [(guard[0], setup + '\n' + mode), (guard[0], change)])
        self.assertIn('NO_BACKSLASH_ESCAPES', body)
        self.assertTrue(body.endswith('SET SESSION sql_mode = @myflames_accounts_sql_mode'))

    def test_account_client_quote_rules_follow_acknowledged_mode_for_backslash_passwords(self):
        # Model the client's parser, not just the server's SQL. Mode changes
        # take effect for parsing subsequent packets only after their replies.
        for password in ('ends-with-backslash\\', "x\\'; DROP USER root; --"):
            with self.subTest(password=password):
                writer = Writer()
                guard = dbops.target_session_guard('mysql', MYSQL)
                statement = 'ALTER USER `app`@`%` IDENTIFIED BY ' + accounts._password(password)
                accounts._run_stdin(writer, guard[0] + '\n' + accounts._with_password_mode(statement), (password,))
                script = writer.writes[0]
                delimiter = script.split('\n', 1)[0].split(' ', 1)[1]
                escaping, writes = True, []
                for check, body, _ in guarded_packets(script):
                    payload = check + ' ' + body
                    client = dbops.split_statements('DELIMITER ' + delimiter + '\n' + payload + '\n' +
                                                   delimiter + '\n', backslash_escapes=escaping)
                    self.assertEqual(len(client), 1)
                    self.assertNotIn(delimiter, client[0]['text'], 'the client swallowed the packet delimiter')
                    for unit in dbops.split_statements(payload, backslash_escapes=escaping):
                        sql = unit['text'].strip().rstrip(';')
                        self.assertNotIn('DROP USER', unit['code'])
                        if sql.startswith('SET SESSION sql_mode = CONCAT_WS'):
                            escaping = False
                        elif sql == 'SET SESSION sql_mode = @myflames_accounts_sql_mode':
                            escaping = True
                        elif 'ALTER USER' in unit['code']:
                            writes.append(sql)
                self.assertEqual(writes, [accounts.TAG + statement])
                self.assertTrue(escaping, 'the original SQL mode was not restored')

    def test_proxy_change_after_password_setup_never_sends_the_account_change(self):
        other = dict(MYSQL, server_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        writer = RoutingTransport([MYSQL, other])
        writer.run_stdin = writer.run_write
        guard = dbops.target_session_guard('mysql', MYSQL)
        body = accounts._with_password_mode("ALTER USER `app`@`%` IDENTIFIED BY 'fixture-secret'")
        with self.assertRaises(ConnectorError) as caught:
            accounts._run_stdin(writer, guard[0] + '\n' + body, ('fixture-secret',))
        self.assertEqual(caught.exception.identity_outcome, 'identity_mismatch')
        self.assertEqual(str(caught.exception), dbops.TARGET_IDENTITY_MISMATCH)
        self.assertEqual(len(writer.packets), 2)
        self.assertTrue(writer.executed, 'the disposable session setup must have run')
        self.assertFalse(any('ALTER USER' in sql for _, sql in writer.executed))

    def test_replication_later_guard_failure_keeps_committed_gtid_and_armed_counter_evidence(self):
        from test_replication import status
        for fixture, index, committed, armed in [('mysql-8.4-gtid-error', 3, True, False),
                                                ('mariadb-11.4-error', 2, False, True)]:
            with self.subTest(fixture=fixture):
                report, _ = status(fixture)
                channel = report['channels'][0]['name']
                plan = replication.plan_skip(report, channel)
                _, starts = dbops.target_guarded_script(plan['session_guard'],
                                                       [replication.SKIP_TAG + s for s in plan['statements']])
                writer = Writer('ERROR 1242 (21000) at line %d: Subquery returns more than 1 row' % starts[index])
                with mock.patch.object(replication, 'read_status', return_value=report), \
                        mock.patch.object(privileges, 'read_privileges',
                                          return_value=privileges.parse_grants([], checked=False)), \
                        mock.patch.object(replication, '_read_after') as after:
                    out = replication.skip_one(writer, channel, plan['identity'], confirmed=True, audit=self.audit())
                self.assertEqual((out['status'], out['outcome'], out['sent']), ('failed', 'identity_mismatch', True))
                self.assertEqual(out['residual_state']['completed_statements'], plan['statements'][:index])
                self.assertIs(out['residual_state']['gtid_committed'], committed)
                self.assertIs(out['residual_state']['counter_armed'], armed)
                after.assert_not_called()
                record = next(r for r in self.records() if r['id'] == out['audit_id'])
                self.assertEqual((record['status'], record['outcome']), ('error', 'identity_mismatch'))
                self.assertEqual(record['after']['residual_state'], out['residual_state'])

    def test_packet_one_and_packet_k_guard_failures_keep_completed_statements_and_audit(self):
        for engine, identity in [('mysql', MYSQL), ('mariadb', dict(MARIA, wsrep_gcomm_uuid=UUID))]:
            for index in (0, 2):
                with self.subTest(engine=engine, index=index):
                    plan = self.plan(engine, identity)
                    plan['statements'] += ['SET GLOBAL max_connections =\n 202;', 'SET @last = 1;']
                    guard = dbops.target_session_guard(engine, identity)
                    _, starts = dbops.target_guarded_script(guard, plan['statements'])
                    writer = Writer('ERROR 1242 (21000) at line %d: Subquery returns more than 1 row' % starts[index])
                    after, progress = mock.Mock(), reviewed_writes.Progress(4)
                    out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test',
                                                  audit=self.audit(), read_after=after, progress=progress)
                    self.assertEqual(out['status'], 'partial' if index else 'refused')
                    self.assertEqual(out['outcome'], 'identity_mismatch')
                    self.assertEqual(out['sent'], plan['statements'][:index])
                    self.assertEqual(out['not_sent'], plan['statements'][index:])
                    self.assertEqual(out['states'], ['done'] * index + ['not_sent'] * (4 - index))
                    self.assertEqual(progress.snapshot()['states'], out['states'])
                    if index:
                        self.assertNotIn('Nothing was sent', out['message'])
                    after.assert_not_called()
                    record = next(r for r in self.records() if r['id'] == out['audit_id'])
                    self.assertEqual((record['status'], record['outcome'], record['error_code']),
                                     ('error' if index else 'refused', 'identity_mismatch', 1242))

    def test_multiline_packet_statement_error_keeps_existing_partial_contract(self):
        plan = self.plan()
        plan['statements'] = ['SET GLOBAL max_connections =\n 200;',
                              'SET GLOBAL max_connections =\n 201;', 'SET @last = 1;']
        _, starts = dbops.target_guarded_script(dbops.target_session_guard('mysql', MYSQL), plan['statements'])
        writer = Writer('ERROR 1227 (42000) at line %d: denied' % starts[1])
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual(out['status'], 'partial')
        self.assertEqual(out['states'], ['done', 'failed', 'not_run'])
        self.assertEqual(out['sent'], plan['statements'][:1])
        self.assertEqual(out['not_sent'], plan['statements'][2:])
        self.assertIn(plan['statements'][1], out['message'])

    def test_unrecognized_packet_line_and_transport_error_keep_unknown_outcomes(self):
        for error in ('ERROR 1242 (21000) at line 1: invalid location',
                      'ERROR 2013 (HY000) at line 2: Lost connection'):
            plan = self.plan()
            out = reviewed_writes.execute(Writer(error), plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
            self.assertEqual((out['status'], out['states']), ('unknown', ['unknown', 'unknown']))

    def test_rotating_proxy_never_executes_a_reviewed_statement_on_another_backend(self):
        identities = [('mysql', MYSQL, dict(MYSQL, server_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')),
                      ('mariadb', MARIA, dict(MARIA, hostname='other-server')),
                      ('mariadb', dict(MARIA, wsrep_gcomm_uuid=UUID),
                       dict(MARIA, wsrep_gcomm_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'))]
        for engine, identity, other in identities:
            for first_matches in (True, False):
                with self.subTest(engine=engine, galera='wsrep_gcomm_uuid' in identity, first=first_matches):
                    plan = self.plan(engine, identity)
                    writer = RoutingTransport([identity, other] if first_matches else [other, identity])
                    out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
                    self.assertEqual(out['status'], 'partial' if first_matches else 'refused')
                    self.assertEqual(len(writer.packets), 2 if first_matches else 1)
                    self.assertEqual(len(writer.executed), 1 if first_matches else 0)
                    self.assertTrue(all(backend == identity for backend, _ in writer.executed))
                    self.assertTrue(all(packet.startswith('SET @myflames_target = ') and 'SET GLOBAL' in packet
                                        for _, packet in writer.packets))

    def test_fake_proxy_exposes_the_old_separate_packet_loophole(self):
        other = dict(MYSQL, server_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        writer = RoutingTransport([MYSQL, other])
        writer.run_write(dbops.target_session_guard('mysql', MYSQL)[0] + '\nSET GLOBAL max_connections = 200;')
        self.assertEqual(writer.executed, [(other, 'SET GLOBAL max_connections = 200')])


if __name__ == '__main__':
    unittest.main()
