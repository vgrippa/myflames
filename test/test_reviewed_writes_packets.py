"""Query-packet identity binding, with a proxy that changes backend per packet.

No database is contacted. Policy, guard construction, error mapping, progress
and audit are production code; the client and two server identities are fake.
"""
import ast
import copy
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock

from myflames import accounts, dbops, locks, privileges, replication, reviewed_writes, variables
from myflames.connector import ConnectorError, MySQLConnection
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
        self.markers = []
        self.sessions = [{'mode': '', 'variables': {}, 'gtid_next': 'AUTOMATIC', 'transaction': False,
                          'channel': ''} for _ in backends]
        self.committed = []
        self.counters = []
        self.client_escaping = True

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
            slot = len(self.packets) % len(self.backends)
            backend, session = self.backends[slot], self.sessions[slot]
            if delimiter != ';':
                client_units = dbops.split_statements('DELIMITER ' + delimiter + '\n' + payload + '\n' + delimiter,
                                                     backslash_escapes=self.client_escaping)
                assert len(client_units) == 1 and not client_units[0]['unclosed'], payload
                assert delimiter not in client_units[0]['text'], payload
            self.packets.append((backend, payload))
            remaining = payload
            while remaining.strip():
                units = dbops.split_statements(remaining, backslash_escapes='NO_BACKSLASH_ESCAPES' not in session['mode'])
                if not units:
                    break
                unit = units[0]
                remaining = remaining[unit['end']:].lstrip().lstrip(';')
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
                        error = ConnectorError('ERROR 1242 (21000) at line %d: Subquery returns more than 1 row' % line)
                        error.guard_markers = tuple(self.markers)
                        raise error
                elif re.fullmatch(r"SELECT '(myflames_guard_ok_[0-9a-f]{32}_[0-9]+)'", sql):
                    self.markers.append(sql[len("SELECT '"):-1])
                else:
                    self.executed.append((backend, sql))
                    if sql == 'SET @myflames_accounts_sql_mode = @@SESSION.sql_mode':
                        session['variables']['account_mode'] = session['mode']
                    elif sql.startswith('SET SESSION sql_mode = CONCAT_WS'):
                        mode = 'NO_BACKSLASH_ESCAPES' if 'NO_BACKSLASH_ESCAPES' in sql else 'ORACLE'
                        session['mode'] += ',' + mode
                    elif sql == 'SET SESSION sql_mode = @myflames_accounts_sql_mode':
                        session['mode'] = session['variables']['account_mode']
                    elif sql.startswith('SET GTID_NEXT = '):
                        session['gtid_next'] = sql.split("'")[1]
                    elif sql == replication.SKIP_TAG + 'BEGIN':
                        session['transaction'] = True
                    elif sql == replication.SKIP_TAG + 'COMMIT':
                        assert session['transaction'], 'COMMIT reached a different backend session'
                        self.committed.append((backend, session['gtid_next']))
                        session['transaction'] = False
                    elif sql.startswith(replication.SKIP_TAG + 'SET GTID_NEXT = '):
                        session['gtid_next'] = sql.split("'")[1]
                    elif sql.startswith(replication.SKIP_TAG + 'SET default_master_connection = '):
                        session['channel'] = sql.split("'")[1]
                    elif sql == replication.SKIP_TAG + 'SET GLOBAL sql_slave_skip_counter = 1':
                        self.counters.append((backend, session['channel']))
                    assert not ('DROP USER' in unit['code']), 'a password escaped its literal'
            self.client_escaping = 'NO_BACKSLASH_ESCAPES' not in session['mode']
            offset += len(chunk) + len(delimiter)
        return '\n'.join(self.markers) + '\n'


class PacketTests(unittest.TestCase):
    setUp = guard_fixtures.GuardTests.setUp
    audit = guard_fixtures.GuardTests.audit
    records = guard_fixtures.GuardTests.records
    plan = guard_fixtures.GuardTests.plan

    def test_changed_modules_keep_python37_syntax(self):
        root = Path(__file__).resolve().parents[1]
        paths = [root / 'myflames' / (name + '.py') for name in
                 ('dbops', 'reviewed_writes', 'accounts', 'connector', 'privileges', 'locks', 'variables', 'replication')]
        paths += [root / 'test' / (name + '.py') for name in
                  ('_reviewed_replay', '_accounts_replay', 'test_cluster_shell', 'test_ui_group_consent', 'test_cluster_group_actions', 'test_variables',
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
        packets = guarded_packets(writer.writes[0])
        units = lambda body: [u['text'].rstrip(';') for u in dbops.split_statements(body, backslash_escapes=False)]
        self.assertEqual([g for g, _, _ in packets], guard * 2)
        self.assertEqual(units(packets[0][1]), [setup.rstrip(';'), mode.rstrip(';')])
        self.assertEqual(units(packets[1][1]), [setup.rstrip(';'), mode.rstrip(';')] + units(change))
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
                    # This packet may arrive in a new server session. The
                    # client's acknowledged mode still controls its scanner.
                    server_escaping = True
                    original_mode = None
                    remaining = payload
                    while remaining.strip():
                        unit = dbops.split_statements(remaining, backslash_escapes=server_escaping)[0]
                        remaining = remaining[unit['end']:].lstrip().lstrip(';')
                        sql = unit['text'].strip().rstrip(';')
                        self.assertNotIn('DROP USER', unit['code'])
                        if sql == 'SET @myflames_accounts_sql_mode = @@SESSION.sql_mode':
                            original_mode = server_escaping
                        elif sql.startswith('SET SESSION sql_mode = CONCAT_WS'):
                            server_escaping = False
                        elif sql == 'SET SESSION sql_mode = @myflames_accounts_sql_mode':
                            server_escaping = original_mode
                        elif 'ALTER USER' in unit['code']:
                            writes.append(sql)
                    escaping = server_escaping
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
        for fixture, index, committed, armed in [('mysql-8.4-gtid-error', 4, True, False),
                                                ('mariadb-11.4-error', 2, False, True)]:
            with self.subTest(fixture=fixture):
                report, _ = status(fixture)
                channel = report['channels'][0]['name']
                plan = replication.plan_skip(report, channel)
                _, starts = dbops.target_guarded_script(plan['session_guard'], replication._skip_packets(plan))
                writer = Writer('ERROR 1242 (21000) at line %d: Subquery returns more than 1 row' % starts[1])
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

    def test_incomplete_or_multiple_units_refuse_before_any_send_or_token_consumption(self):
        for sql in ("SET @note = 'unterminated", 'SET @note = "unterminated', 'SET @note = `unterminated',
                    'SET @note = 1 /* unterminated', 'SET @note = 1; /* unterminated',
                    'SET @note = 1; SET @other = 2', '/* comment only */', 'DELIMITER //\nSET @note = 1//',
                    'DELIMITER ;\nSET @note = 1', 'SET @note = 1;\nDELIMITER ;\n',
                    "SET @note = 'x\\'; SET @other = 2; -- '"):
            with self.subTest(sql=sql):
                plan, writer, consume = self.plan(), Writer(), mock.Mock()
                plan['statements'] = [sql]
                with self.assertRaises(ValueError):
                    reviewed_writes.check_reviewed(plan)
                out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', before_send=consume)
                self.assertEqual((out['status'], out['states'], writer.writes), ('refused', ['not_sent'], []))
                consume.assert_not_called()

    def test_closed_multiline_statements_share_the_apply_and_builder_contract(self):
        plan = self.plan()
        plan['statements'] = ["SET @note =\n 'a; b'; -- closed line comment", 'SET @other =\n 2 /* closed */;']
        self.assertIs(reviewed_writes.check_reviewed(plan), plan)
        writer = Writer()
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test')
        self.assertEqual(out['status'], 'done')
        self.assertEqual([s for _, s, _ in guarded_packets(writer.writes[0])], plan['statements'])

    def test_noncanonical_account_mode_changes_refuse_with_nothing_sent(self):
        canonical = accounts._with_password_mode("ALTER USER app IDENTIFIED BY 'fixture-secret'")
        bodies = [canonical.replace('SET SESSION sql_mode = CONCAT_WS', 'SET SESSION sql_mode=CONCAT_WS'),
                  canonical + ';\nSELECT 1', canonical.replace('@@SESSION.sql_mode;', '@@GLOBAL.sql_mode;', 1),
                  'SET SESSION sql_mode = "NO_BACKSLASH_ESCAPES"; ALTER USER app IDENTIFIED BY "x"',
                  "SET SESSION `sql_mode` = 'NO_BACKSLASH_ESCAPES'", "SET @@session.sql_mode = ''",
                  canonical.replace("'fixture-secret'", "'unterminated"),
                  canonical.replace("'fixture-secret'", "'x'; SELECT 1"),
                  'ALTER USER app ACCOUNT LOCK /* unterminated']
        for body in bodies:
            with self.subTest(body=body):
                writer = Writer()
                with self.assertRaisesRegex(ConnectorError, 'Nothing was sent'):
                    accounts._run_stdin(writer, dbops.target_session_guard('mysql', MYSQL)[0] + '\n' + body)
                self.assertEqual(writer.writes, [])

    def test_account_mode_is_reasserted_and_restored_in_a_fresh_backend_session(self):
        for engine, identity in [('mysql', MYSQL), ('mariadb', dict(MARIA, wsrep_on='ON', wsrep_gcomm_uuid=UUID))]:
            wrappers = [accounts._with_password_mode]
            if engine == 'mariadb':
                wrappers.append(accounts._with_package_mode)
            for wrapper in wrappers:
                with self.subTest(engine=engine, wrapper=wrapper.__name__):
                    writer = RoutingTransport([identity, dict(identity)])
                    writer.sessions[1]['mode'] = 'STRICT_TRANS_TABLES'
                    writer.run_stdin = writer.run_write
                    password = "x\\'; DROP USER root; --"
                    statement = ("ALTER USER app IDENTIFIED BY " + accounts._password(password)
                                 if wrapper is accounts._with_password_mode else
                                 'GRANT EXECUTE ON PACKAGE shop.pkg TO app')
                    output = accounts._run_stdin(writer, dbops.target_session_guard(engine, identity)[0] +
                                                 '\n' + wrapper(statement), (password,))
                    self.assertEqual(output, '', 'guard acknowledgements escaped into an account result')
                    self.assertEqual(len(writer.packets), 2)
                    self.assertEqual(writer.sessions[1]['mode'], 'STRICT_TRANS_TABLES')
                    self.assertEqual(sum(statement in sql for _, sql in writer.executed), 1)
                    self.assertTrue(all(backend == identity for backend, _ in writer.executed))

    def test_replication_dependent_sequences_use_one_backend_session(self):
        from test_replication import status
        for fixture in ('mysql-8.4-gtid-error', 'mariadb-11.4-error'):
            with self.subTest(fixture=fixture):
                report, _ = status(fixture)
                channel = report['channels'][0]['name']
                plan = replication.plan_skip(report, channel)
                identity = plan['identity']['server_identity']
                if report['engine'] == 'mariadb':
                    identity.update(wsrep_on='ON', wsrep_gcomm_uuid=UUID)
                guard = dbops.target_session_guard(report['engine'], identity)
                writer = RoutingTransport([identity, dict(identity)])
                script, starts = dbops.target_guarded_script(guard, replication._skip_packets(plan))
                writer.run_write(script)
                self.assertEqual(len(writer.packets), 2)
                self.assertEqual(starts.indices, (0, 4) if report['engine'] == 'mysql' else (0, 2))
                if report['engine'] == 'mysql':
                    self.assertEqual(writer.committed, [(identity, plan['skip']['gtid'])])
                    self.assertTrue(all(s['gtid_next'] == 'AUTOMATIC' for s in writer.sessions))
                else:
                    self.assertEqual(writer.counters, [(identity, channel)])

    def test_galera_review_capture_reads_the_engine_table_and_fails_closed(self):
        for engine, identity, table in [('mariadb', MARIA, 'information_schema.GLOBAL_STATUS'),
                                        ('mysql', MYSQL, 'performance_schema.global_status'),
                                        ('pxc', MYSQL, 'performance_schema.global_status')]:
            for response in (UUID + '\n', '', 'not-a-uuid\n', ConnectorError('ERROR 1142: SELECT denied')):
                with self.subTest(engine=engine, response=response):
                    conn = mock.Mock()
                    if isinstance(response, Exception):
                        conn.run.side_effect = response
                    else:
                        conn.run.return_value = response
                    captured = dbops.target_galera_identity(conn, engine, identity, {'wsrep_on': 'ON'})
                    self.assertIn('FROM ' + table, conn.run.call_args[0][0])
                    if response == UUID + '\n':
                        self.assertEqual(captured['wsrep_gcomm_uuid'], UUID)
                        self.assertIn(table, dbops.target_session_guard(engine, captured)[0])
                    else:
                        with self.assertRaisesRegex(ValueError, 'identity check could not run'):
                            dbops.target_session_guard(engine, captured)
        with self.assertRaisesRegex(ValueError, 'identity check could not run'):
            dbops.target_session_guard('mariadb', dict(MARIA, wsrep_on='ON'))

    def test_accounts_capture_galera_node_and_refuse_an_identically_named_peer(self):
        from test_accounts import FakeConn, actor
        conn = FakeConn('mariadb')
        original = conn.run
        def read(sql, **kwargs):
            if "WHERE VARIABLE_NAME = 'wsrep_gcomm_uuid'" in sql:
                return UUID + '\n'
            return original(sql, **kwargs)
        conn.run = read
        evidence = actor('mariadb', variables=dict(MARIA, wsrep_on='ON', wsrep_node_name='same-name'))
        with mock.patch.object(privileges, 'read_privileges', return_value=evidence):
            plan = accounts.plan_lock(conn, 'app')
            self.assertEqual(plan['actor_server']['wsrep_gcomm_uuid'], UUID)
            other = dict(plan['actor_server'], wsrep_gcomm_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
            writer = RoutingTransport([other])
            conn.run_stdin = writer.run_write
            out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
        self.assertEqual((out['status'], out['outcome']), ('refused', 'identity_mismatch'))
        self.assertEqual(writer.executed, [])

    def test_variable_and_replication_reads_capture_galera_node(self):
        from test_variables import replay as variable_replay
        from test_replication import replay as replication_replay
        for engine, replay, reader in [('variables', lambda edit: variable_replay('mariadb-11.4', 'global', edit),
                                        lambda c: variables.read_variable(c, 'max_connections')),
                                       ('replication', lambda edit: replication_replay('replication', 'mariadb-11.4-error', edit),
                                        replication.read_status)]:
            for available in (True, False):
                with self.subTest(sender=engine, available=available):
                    def edit(sql, text, n):
                        if 'SHOW GLOBAL VARIABLES WHERE' in sql and text and 'version\t' in text:
                            return 'wsrep_on\tON\n' + text
                        return text
                    conn, _ = replay(edit)
                    original = conn.run
                    def read(sql, **kwargs):
                        if "WHERE VARIABLE_NAME = 'wsrep_gcomm_uuid'" in sql:
                            if not available:
                                raise ConnectorError('ERROR 1142: SELECT denied')
                            return UUID + '\n'
                        return original(sql, **kwargs)
                    conn.run = read
                    report = reader(conn)
                    identity = (report['server_identity'] if engine == 'variables' else
                                report['settings']['server_identity'])
                    self.assertEqual(identity['wsrep_gcomm_uuid'], UUID if available else None)
                    if available:
                        self.assertIn('information_schema.GLOBAL_STATUS', dbops.target_session_guard('mariadb', identity)[0])
                    else:
                        with self.assertRaisesRegex(ValueError, 'identity check could not run'):
                            dbops.target_session_guard('mariadb', identity)

    def test_kill_captures_node_uuid_with_its_identity_and_refuses_a_peer(self):
        from test_locks import KillScenario, VERSION_SQL, bound_kill_expect, batch
        for engine in ('mysql', 'mariadb'):
            with self.subTest(engine=engine):
                scenario = KillScenario()
                if engine == 'mariadb':
                    scenario.conn.responses[0] = (VERSION_SQL, batch([['11.4.13-MariaDB', 1]]))
                    scenario.conn.responses[2] = (scenario.conn.responses[2][0],
                        batch([['1', 'server-one', '11.4.13-MariaDB', '3306', 'ON', UUID, 'same-name']]))
                else:
                    scenario.conn.responses[1] = ('SELECT @@server_uuid', batch([[UUID, 'ON', UUID]]))
                _server, identity = locks._kill_context(scenario.conn)
                self.assertEqual(identity['wsrep_gcomm_uuid'], UUID)
                sql = scenario.conn.calls[-1][0]
                self.assertIn('information_schema.GLOBAL_STATUS' if engine == 'mariadb' else
                              'performance_schema.global_status', sql)
                expect = dict(bound_kill_expect(scenario.conn), server_identity=identity)
                writer = RoutingTransport([dict(identity, wsrep_gcomm_uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')])
                scenario.conn.run_write = writer.run_write
                out = locks.kill_session(scenario.conn, 41, expect=expect, confirmed=True, audit=self.audit(), settle_s=0)
                self.assertEqual((out['status'], out['outcome']), ('refused', 'identity_mismatch'))
                self.assertEqual(writer.executed, [])

    def test_1242_after_each_packets_marker_is_a_statement_error(self):
        for index in (0, 1):
            with self.subTest(packet=index):
                plan = self.plan()
                _, starts = dbops.target_guarded_script(dbops.target_session_guard('mysql', MYSQL), plan['statements'])
                writer = Writer('ERROR 1242 (21000) at line %d: Subquery returns more than 1 row' % starts[index],
                                guard_passed=True)
                out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
                self.assertEqual(out['status'], 'partial' if index else 'failed')
                self.assertEqual(out['states'], ['done'] * index + ['failed'] + ['not_run'] * (1 - index))
                self.assertNotEqual(out.get('outcome'), 'identity_mismatch')
                self.assertNotIn('myflames_guard_ok_', repr(out) + repr(self.records()))

    def test_line_free_transport_errors_are_unknown_even_after_an_acknowledged_packet(self):
        for code in (1045, 2002, 2003, 2006, 2013, 2050):
            for acknowledge in (False, True):
                if code in (1045, 2002, 2003) and not acknowledge:
                    continue
                with self.subTest(code=code, acknowledge=acknowledge):
                    class LostConnection(Writer):
                        def run_write(self, sql, **kwargs):
                            self.writes.append(sql)
                            error = ConnectorError('ERROR %d (HY000): client transport failed' % code)
                            if acknowledge:
                                error.guard_markers = tuple(re.findall(r"SELECT '(myflames_guard_ok_[0-9a-f]{32}_[0-9]+)';", sql)[:1])
                            raise error
                    plan, writer = self.plan(), LostConnection()
                    out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
                    self.assertEqual((out['status'], out['states']), ('unknown', ['unknown', 'unknown']))
                    self.assertNotIn('not_connected', repr(self.records()))
        for code in (1045, 2002, 2003, 2005):
            plan = self.plan()
            out = reviewed_writes.execute(Writer('ERROR %d: Cannot connect or log in' % code), plan,
                                          lambda: copy.deepcopy(plan), 'test')
            self.assertEqual((out['status'], out['states']), ('failed', ['not_sent', 'not_sent']))

    def test_real_connector_preserves_only_acknowledgements_and_strips_success_output(self):
        guard = dbops.target_session_guard('mysql', MYSQL)
        script, starts = dbops.target_guarded_script(guard, ['SELECT (SELECT 1 UNION ALL SELECT 1)'])
        marker = starts.markers[0]
        with MySQLConnection('fixture-db', binary='mysql') as conn:
            failure = subprocess.CompletedProcess([], 1, stdout=(marker + '\nprivate-result\n').encode(),
                stderr=b'ERROR 1242 (21000) at line 2: Subquery returns more than 1 row')
            with mock.patch.object(conn, '_run_client', return_value=failure):
                with self.assertRaises(ConnectorError) as caught:
                    conn.run_write(script)
            self.assertEqual(caught.exception.guard_markers, (marker,))
            self.assertNotIn('private-result', str(caught.exception))
            self.assertFalse(hasattr(caught.exception, 'stdout'))
            self.assertIsNone(dbops.target_guard_failed(caught.exception, starts))
            similar = 'myflames_guard_ok_00000000000000000000000000000000_99'
            success = subprocess.CompletedProcess([], 0, stdout=(marker + '\ngenerated-password\n' + similar + '\n').encode(), stderr=b'')
            with mock.patch.object(conn, '_run_client', return_value=success):
                self.assertEqual(conn.run_write(script), 'generated-password\n' + similar + '\n')
            unguarded = subprocess.CompletedProcess([], 0, stdout=(similar + '\n').encode(), stderr=b'')
            with mock.patch.object(conn, '_run_client', return_value=unguarded):
                self.assertEqual(conn.run_write("SELECT '%s'" % similar), similar + '\n')

    def test_private_markers_and_fake_error_lines_in_sql_echo_never_reach_audit(self):
        plan = self.plan()
        plan['statements'] = ["SET @note = 'line one\nERROR 1242 (21000) at line 999: quoted text';"]
        class EchoingClient(Writer):
            def run_write(self, sql, **kwargs):
                self.writes.append(sql)
                raise ConnectorError('mysql client exited 1: --------------\n' + sql +
                                     '--------------\nERROR 1242 (21000) at line 2: Subquery returns more than 1 row')
        writer = EchoingClient()
        out = reviewed_writes.execute(writer, plan, lambda: copy.deepcopy(plan), 'test', audit=self.audit())
        self.assertEqual((out['status'], out['outcome']), ('refused', 'identity_mismatch'))
        self.assertNotIn('myflames_guard_ok_', repr(out) + repr(self.records()))
        self.assertEqual(self.records()[0]['error_code'], 1242)

    def test_all_sql_senders_distinguish_a_statement_1242_from_the_guard(self):
        from test_accounts import FakeConn, actor
        from test_variables import mysql_variable
        from test_replication import status
        from test_locks import KillScenario, bound_kill_expect
        error = 'ERROR 1242 (21000) at line 2: Subquery returns more than 1 row'
        with self.subTest(sender='accounts'):
            conn, writer = FakeConn(), Writer(error, guard_passed=True)
            conn.run_stdin = writer.run_write
            with mock.patch.object(privileges, 'read_privileges', return_value=actor()):
                plan = accounts.plan_lock(conn, 'app')
                out = accounts.apply(conn, plan, plan['expect'], confirmed=True, store=self.store)
            self.assertEqual(out['status'], 'refused_by_server')
            self.assertNotEqual(out.get('outcome'), 'identity_mismatch')
        with self.subTest(sender='variables'):
            value = mysql_variable('max_connections')
            plan = variables.build_plan(value, '200', 'global')
            with mock.patch.object(variables, 'read_variable', return_value=value):
                out = variables.apply_change(Writer(error, guard_passed=True), plan, plan['expect'],
                                             confirmed=True, audit=self.audit())
            self.assertNotEqual(out.get('outcome'), 'identity_mismatch')
            self.assertNotEqual(out['status'], 'refused')
        with self.subTest(sender='replication'):
            report, _ = status('mysql-8.4-gtid-error')
            channel = report['channels'][0]['name']
            plan = replication.plan_skip(report, channel)
            after = {'present': True, 'passed': False, 'skip_counter': 0, 'global_skip_counter': 0}
            with mock.patch.object(replication, 'read_status', return_value=report), \
                    mock.patch.object(replication, '_read_after', return_value=after) as read_after, \
                    mock.patch.object(privileges, 'read_privileges', return_value=privileges.parse_grants([], checked=False)):
                out = replication.skip_one(Writer(error, guard_passed=True), channel, plan['identity'],
                                           confirmed=True, audit=self.audit())
            self.assertEqual(out['status'], 'failed')
            self.assertNotEqual(out.get('outcome'), 'identity_mismatch')
            self.assertIsNone(out['residual_state']['completed_statements'])
            self.assertNotIn('Nothing ran before it', out['message'])
            self.assertIn('which of them completed is unknown', out['message'])
            read_after.assert_called_once()
        with self.subTest(sender='locks'):
            scenario = KillScenario()
            scenario.conn.run_write = Writer(error, guard_passed=True).run_write
            with self.assertRaises(ValueError) as caught:
                locks.kill_session(scenario.conn, 41, expect=bound_kill_expect(scenario.conn), confirmed=True,
                                   audit=self.audit(), settle_s=0)
            self.assertNotIn('not the one you reviewed', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
