"""ProxySQL query digests, rules cross-checked with them, and the progress of
a reviewed send (myflames/proxysql_digests.py, myflames/reviewed_writes.py
Progress, /api/live/proxysql/digests and /api/live/action/progress).

FakeAdmin (test_proxysql.py) is the SQLite stand-in for ProxySQL Admin with the
live lab's rules and digests (ProxySQL 3.0.11, 2026-10-21). Routing cases use
rule rows shaped like runtime_mysql_query_rules. Every test runs with a
temporary HOME and the vault backend.
"""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from myflames import proxysql, proxysql_digests as pd, reviewed_writes, ui
from test_proxysql import DIGESTS, Factory, Home, NotProxy

ROOT = Path(__file__).resolve().parent.parent
DIGEST_SCHEMA = json.loads((ROOT / 'docs' / 'schemas' / 'proxysql-digests-1.0.json').read_text(encoding='utf-8'))
PROXY_SCHEMA = json.loads((ROOT / 'docs' / 'schemas' / 'proxysql-1.0.json').read_text(encoding='utf-8'))


def rule(rule_id, **fields):
    row = {'rule_id': str(rule_id), 'active': '1', 'flagIN': '0', 're_modifiers': 'CASELESS', 'apply': '0',
           'negate_match_pattern': '0'}
    row.update(dict((k, None if v is None else str(v)) for k, v in fields.items()))
    return row


def digest(text, hostgroup=30, user='app', schema='shop', client='', hash_='0x1'):
    return {'digest_text': text, 'hostgroup': hostgroup, 'username': user, 'schemaname': schema,
            'client_address': client, 'digest': hash_}


USERS = [{'username': 'app', 'active': '1', 'default_hostgroup': '30', 'transaction_persistent': '1', 'frontend': '1'},
         {'username': 'app', 'active': '1', 'default_hostgroup': '99', 'transaction_persistent': '1', 'frontend': '0'}]


def routing(rules, fast=(), users=USERS, variables=None):
    values = {'mysql-query_processor_regex': '1', 'mysql-query_processor_iterations': '0'}
    values.update(variables or {})
    return pd.Routing(rules, list(fast), users, values)


class TestRouting(unittest.TestCase):
    def test_unread_iterations_only_leave_affected_walks_not_judged(self):
        rules = [rule(1, flagIN=5, destination_hostgroup=50, apply=1),
                 rule(2, match_digest='^SELECT', flagOUT=5)]
        for variables in (None, {}, {'mysql-query_processor_regex': '1'},
                          {'mysql-query_processor_regex': '1', 'mysql-query_processor_iterations': 'bad'}):
            with self.subTest(variables=variables):
                r = pd.Routing(rules, [], USERS, variables)
                for hg in (30, 50, 90):
                    out = r.evaluate(digest('SELECT 1', hostgroup=hg))
                    self.assertEqual(out['verdict'], 'unknown', out)
                    self.assertIn('mysql-query_processor_iterations', ' '.join(out['why']))
                    self.assertIn('not judged', out['text'])
        self.assertEqual(routing(rules, variables={'mysql-query_processor_iterations': '1'}).evaluate(
            digest('SELECT 1', hostgroup=50))['verdict'], 'match')
        stopped = pd.Routing([rule(1, destination_hostgroup=30, flagOUT=5, apply=1)], [], USERS, None)
        self.assertEqual(stopped.evaluate(digest('SELECT 1'))['verdict'], 'match', 'apply needs no restart budget')
        self.assertEqual(pd.Routing([], [], USERS, None).evaluate(digest('SELECT 1'))['verdict'], 'match')

    def test_unread_regex_variable_names_the_uncertainty_when_a_pattern_is_tested(self):
        r = pd.Routing([rule(1, match_digest='^SELECT', destination_hostgroup=50, apply=1)], [], USERS,
                       {'mysql-query_processor_iterations': '0'})
        out = r.evaluate(digest('SELECT 1', hostgroup=30))
        self.assertEqual(out['verdict'], 'unknown', out)
        self.assertIn('mysql-query_processor_regex', ' '.join(out['why']))
        self.assertIn('not judged', out['text'])

    def test_match_digest_is_exact_and_caseless_follows_re_modifiers(self):
        r = routing([rule(1, match_digest='^select', destination_hostgroup=32, apply=1)])
        out = r.evaluate(digest('SELECT 1', hostgroup=32))
        self.assertEqual((out['verdict'], out['expected'], out['source'], out['rules'], out['approximate']),
                         ('match', 32, 'rule', [1], False))
        self.assertEqual(out['text'], 'Rule 1 sends it to hostgroup 32, where it ran.')
        exact = routing([rule(1, match_digest='^select', destination_hostgroup=32, apply=1, re_modifiers='')])
        out = exact.evaluate(digest('SELECT 1', hostgroup=30))
        self.assertEqual((out['verdict'], out['source'], out['expected']), ('match', 'default', 30),
                         'without CASELESS ^select does not match SELECT: the frontend row default hostgroup')

    def test_negate_turns_either_regex_around(self):
        # rule_matches_regex applies negate_match_pattern to match_digest too.
        r = routing([rule(1, match_digest='^SELECT', negate_match_pattern=1, destination_hostgroup=10, apply=1)])
        self.assertEqual(r.evaluate(digest('UPDATE t SET a = ?', hostgroup=10))['rules'], [1])
        self.assertEqual(r.evaluate(digest('SELECT 1', hostgroup=10))['rules'], [])

    def test_apply_stops_and_the_last_destination_wins_without_it(self):
        rules = [rule(1, match_digest='^SELECT', destination_hostgroup=31),
                 rule(2, match_digest='FROM orders', destination_hostgroup=32),
                 rule(3, match_digest='.', destination_hostgroup=33, apply=1),
                 rule(4, match_digest='.', destination_hostgroup=34, apply=1)]
        out = routing(rules).evaluate(digest('SELECT * FROM orders', hostgroup=33))
        self.assertEqual((out['rules'], out['decisive'], out['expected'], out['verdict']), ([1, 2, 3], 3, 33, 'match'))

    def test_flag_out_chains_and_iterations_restart(self):
        rules = [rule(1, flagIN=5, match_digest='.', destination_hostgroup=50, apply=1),
                 rule(2, match_digest='^SELECT', flagOUT=5)]
        out = routing(rules).evaluate(digest('SELECT 1', hostgroup=30))
        self.assertEqual((out['rules'], out['expected'], out['source']), ([2], 30, 'default'),
                         'flagIN 5 comes before the rule that sets it: not reached without iterations')
        out = routing(rules, variables={'mysql-query_processor_iterations': '1'}).evaluate(digest('SELECT 1', hostgroup=50))
        self.assertEqual((out['rules'], out['expected'], out['verdict']), ([2, 1], 50, 'match'))
        chained = [rule(1, match_digest='^SELECT', flagOUT=7), rule(2, flagIN=7, destination_hostgroup=70, apply=1)]
        self.assertEqual(routing(chained).evaluate(digest('SELECT 1', hostgroup=70))['rules'], [1, 2])

    def test_fast_routing_then_the_default_hostgroup(self):
        fast = [{'username': 'app', 'schemaname': 'shop', 'flagIN': '0', 'destination_hostgroup': '40'}]
        out = routing([rule(1, match_digest='^SELECT', destination_hostgroup=31)], fast).evaluate(
            digest('SELECT 1', hostgroup=40))
        self.assertEqual((out['expected'], out['source'], out['verdict']), (40, 'fast_routing', 'match'))
        stopped = routing([rule(1, match_digest='^SELECT', destination_hostgroup=31, apply=1)], fast)
        self.assertEqual(stopped.evaluate(digest('SELECT 1', hostgroup=31))['source'], 'rule', 'apply skips fast routing')
        out = routing([]).evaluate(digest('SELECT 1', hostgroup=30))
        self.assertEqual((out['source'], out['default_hostgroup'], out['verdict']), ('default', 30, 'match'))
        self.assertIn("app's default hostgroup 30", out['text'])
        unknown = routing([], users=None).evaluate(digest('SELECT 1'))
        self.assertEqual(unknown['verdict'], 'unknown')

    def test_mismatch_names_transaction_persistence(self):
        out = routing([rule(1, match_digest='^SELECT', destination_hostgroup=32, apply=1)]).evaluate(
            digest('SELECT 1', hostgroup=30))
        self.assertEqual(out['verdict'], 'mismatch')
        self.assertEqual(out['text'], 'Rule 1 sends it to hostgroup 32, but it ran on hostgroup 30.')
        self.assertTrue(any('transaction_persistent is 1 for app' in w for w in out['why']), out['why'])

    def test_match_pattern_is_approximate_and_can_explain_another_hostgroup(self):
        rules = [rule(10, match_pattern=r'^/\* galera \*/', destination_hostgroup=10, apply=1),
                 rule(30, match_digest='^SELECT', destination_hostgroup=32, apply=1)]
        out = routing(rules).evaluate(digest('SELECT @@hostname', hostgroup=10))
        self.assertEqual((out['verdict'], out['possible'], out['expected']), ('possible', [10], 32))
        self.assertIn('where rule 10 may have sent it', out['text'])
        self.assertIn('match_pattern', out['why'][0])
        approx = routing([rule(5, match_pattern='^SELECT', destination_hostgroup=32, apply=1)]).evaluate(
            digest('SELECT 1', hostgroup=32))
        self.assertEqual((approx['verdict'], approx['approximate']), ('match', True))

    def test_what_a_digest_does_not_record_is_unknown(self):
        rules = [rule(1, proxy_port=6033, match_digest='.', destination_hostgroup=10, apply=1),
                 rule(2, client_addr='10.0.0.%', destination_hostgroup=11, apply=1),
                 rule(3, match_digest='(?<name>x)', destination_hostgroup=12, apply=1)]
        out = routing(rules).evaluate(digest('SELECT 1', hostgroup=10))
        self.assertEqual([u['rule_id'] for u in out['unknown_rules']], [1, 2, 3])
        self.assertEqual((out['verdict'], out['possible']), ('possible', [1]))
        self.assertIn('Python cannot read', out['unknown_rules'][2]['why'])

    def test_conditions_on_user_schema_client_and_digest(self):
        rules = [rule(1, username='other', destination_hostgroup=1, apply=1),
                 rule(2, schemaname='other', destination_hostgroup=2, apply=1),
                 rule(3, client_addr='10.0.0.9', destination_hostgroup=3, apply=1),
                 rule(4, digest='0x00000000000000AB', destination_hostgroup=4, apply=1),
                 rule(5, digest='0xAB', destination_hostgroup=5, apply=1)]
        out = routing(rules).evaluate(digest('SELECT 1', hostgroup=5, client='10.0.0.8', hash_='0xAB'))
        self.assertEqual((out['rules'], out['expected'], out['unknown_rules']), ([4], 4, []),
                         'the rule digest is a number (strtoull base 0), so 0x...AB equals 0xAB')

    def test_statements_proxysql_answers_itself(self):
        # select @@version_comment limit 1 (the mysql client's first query) is
        # answered by handler_special_queries: live on 3.0.11 its digest named
        # mf_admin's default hostgroup although rule 30 sends SELECT to 32.
        r = routing([rule(30, match_digest='^SELECT', destination_hostgroup=32, apply=1)])
        out = r.evaluate(digest('select @@version_comment limit ?', hostgroup=30))
        self.assertEqual((out['verdict'], out['expected']), ('local', 32))
        self.assertIn('answers this statement itself', out['text'])
        # SELECT VERSION() is ProxySQL's own answer only by mysql-select_version_forwarding
        # (MySQL_Session.cpp: 1 always forwards; 0, 2 and 3, the default, can answer).
        self.assertEqual(r.evaluate(digest('SELECT VERSION()', hostgroup=30))['verdict'], 'mismatch')
        for mode, verdict in (('3', 'local'), ('0', 'local'), ('1', 'mismatch')):
            forwarding = routing([rule(30, match_digest='^SELECT', destination_hostgroup=32, apply=1)],
                                 variables={'mysql-select_version_forwarding': mode})
            self.assertEqual(forwarding.evaluate(digest('SELECT VERSION()', hostgroup=30))['verdict'], verdict, mode)
        self.assertEqual(r.evaluate(digest('select @@version_comment limit ?', hostgroup=31))['verdict'], 'mismatch',
                         'only the default hostgroup is the local answer')
        self.assertEqual(r.evaluate(digest('SELECT @@hostname', hostgroup=30))['verdict'], 'mismatch')

    def test_a_worker_that_ends_lines_with_crlf_is_read(self):
        # The worker's text stdout ends lines with \r\n on Windows; its ready
        # line and answers must still be read.
        import sys
        child = [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); "
                 "sys.stdout.buffer.write(b'ready\\r\\n10\\r\\n'); sys.stdout.buffer.flush()"]
        with mock.patch.object(pd, "_worker_argv", return_value=child):
            self.assertEqual(pd.test_patterns([("x", False)], ["x", "y"]), {0: "10"})

    def test_a_pattern_that_backtracks_without_end_never_holds_up_the_workspace(self):
        # Patterns run in a separate process; one that takes too long is unknown
        # and the worker starts again for the rules after it.
        rules = [rule(1, match_digest='(a+)+$', destination_hostgroup=10, apply=1),
                 rule(2, match_digest='^SELECT', destination_hostgroup=32, apply=1)]
        import time
        with mock.patch.object(pd, 'PATTERN_TIMEOUT', 0.5):
            started = time.monotonic()
            out = routing(rules).evaluate(digest('SELECT ' + 'a' * 40 + 'b FROM t', hostgroup=32))
            took = time.monotonic() - started
        self.assertLess(took, 5)
        self.assertEqual([u['rule_id'] for u in out['unknown_rules']], [1])
        self.assertIn('took too long', out['unknown_rules'][0]['why'])
        self.assertEqual((out['decisive'], out['expected'], out['verdict']), (2, 32, 'match'))

    def test_a_worker_that_cannot_start_leaves_patterns_unknown(self):
        r = routing([rule(1, match_digest='^SELECT', destination_hostgroup=32, apply=1)])
        with mock.patch.object(pd, '_worker_argv', lambda: ['/nonexistent/python']):
            out = r.evaluate(digest('SELECT 1', hostgroup=32))
        self.assertEqual([u['rule_id'] for u in out['unknown_rules']], [1])
        self.assertIn('could not be tested here', r.context()['note'])

    def test_the_worker_answers_one_line_per_pattern(self):
        import io
        answer = io.StringIO()
        pd.regex_worker(io.StringIO(json.dumps({'patterns': [['^sel', True], ['^sel', False], ['(', False]],
                                                'texts': ['SELECT 1', 'UPDATE t']})), answer)
        self.assertEqual(answer.getvalue(), 'ready\n10\n00\nee\n')

    def test_flag_out_weights_follow_every_choice(self):
        # attributes.flagOUTs (MySQL_Query_Processor.cpp new_query_rule).
        one = [rule(1, match_digest='^SELECT', attributes='{"flagOUTs": [{"id": 7, "weight": 1}]}'),
               rule(2, flagIN=7, destination_hostgroup=70, apply=1)]
        out = routing(one).evaluate(digest('SELECT 1', hostgroup=70))
        self.assertEqual((out['verdict'], out['expected'], out['rules']), ('match', 70, [1, 2]))
        two = [rule(1, match_digest='^SELECT', attributes='{"flagOUTs": [{"id": 7, "weight": 1}, {"id": 8, "weight": 3}]}'),
               rule(2, flagIN=7, destination_hostgroup=70, apply=1), rule(3, flagIN=8, destination_hostgroup=80, apply=1)]
        out = routing(two).evaluate(digest('SELECT 1', hostgroup=80))
        self.assertEqual(out['verdict'], 'match')
        self.assertIn('another flagOUT weight', out['text'])
        self.assertTrue(any('at random by weight' in w for w in out['why']))
        self.assertEqual(routing(two).evaluate(digest('SELECT 1', hostgroup=90))['verdict'], 'mismatch')
        flag_out_wins = [rule(1, match_digest='^SELECT', flagOUT=8, attributes='{"flagOUTs": [{"id": 7, "weight": 1}]}'),
                         rule(2, flagIN=7, destination_hostgroup=70, apply=1), rule(3, flagIN=8, destination_hostgroup=80, apply=1)]
        self.assertEqual(routing(flag_out_wins).evaluate(digest('SELECT 1', hostgroup=80))['expected'], 80)

    def test_fast_routing_falls_back_to_any_user_of_the_schema(self):
        # search_rules_fast_routing_dest_hg: user, schema, flagIN, then the key without the user.
        fast = [{'username': '', 'schemaname': 'shop', 'flagIN': '0', 'destination_hostgroup': '40'}]
        out = routing([], fast).evaluate(digest('SELECT 1', hostgroup=40))
        self.assertEqual((out['source'], out['expected'], out['verdict']), ('fast_routing', 40, 'match'))
        mine = fast + [{'username': 'app', 'schemaname': 'shop', 'flagIN': '0', 'destination_hostgroup': '41'}]
        self.assertEqual(routing([], mine).evaluate(digest('SELECT 1', hostgroup=41))['expected'], 41)

    def test_an_uncertain_rule_keeps_the_path_it_would_open(self):
        # A comment rule (match_pattern, not on the digest text) sets flagOUT 7,
        # and the flagIN 7 rule sends to 70: a digest on 70 may come from it.
        rules = [rule(1, match_pattern=r'^/\* reports \*/', flagOUT=7),
                 rule(2, flagIN=7, destination_hostgroup=70, apply=1),
                 rule(3, match_digest='^SELECT', destination_hostgroup=32, apply=1)]
        out = routing(rules).evaluate(digest('SELECT 1', hostgroup=70))
        self.assertEqual((out['verdict'], out['possible'], out['expected']), ('possible', [1], 32))

    def test_a_worker_that_never_reads_cannot_hold_up_the_read(self):
        # stdin is written by its own thread: a child that never reads it
        # still lets the start timeout fire, and the child is reaped.
        import sys
        import time
        with mock.patch.object(pd, '_worker_argv', lambda: [sys.executable, '-c', 'import time; time.sleep(30)']), \
                mock.patch.object(pd, 'WORKER_START', 0.5):
            started = time.monotonic()
            with self.assertRaises(OSError):
                pd.test_patterns([['^x', False]], ['y' * 4096] * 400)
            self.assertLess(time.monotonic() - started, 5)

    def test_what_the_walk_leaves_out_is_never_a_mismatch(self):
        # Twelve restarts reach hostgroup 70; myflames follows ten.
        rules = [rule(100 - n, flagIN=n, flagOUT=n + 1) for n in range(1, 12)]
        rules += [rule(1, flagIN=12, destination_hostgroup=70, apply=1), rule(200, match_digest='.', flagOUT=1)]
        out = routing(rules, variables={'mysql-query_processor_iterations': '50'}).evaluate(digest('SELECT 1', hostgroup=70))
        self.assertEqual(out['verdict'], 'unknown')
        self.assertTrue(any('restarts' in w for w in out['why']), out['why'])
        within = routing(rules, variables={'mysql-query_processor_iterations': '12'})
        self.assertEqual(within.iterations, pd.ITERATION_LIMIT)
        # More flagOUT choices than BRANCH_LIMIT: the ones left out are not judged.
        many = [rule(1, match_digest='^SELECT', attributes=json.dumps(
            {'flagOUTs': [{'id': n, 'weight': 1} for n in range(1, 71)]}))]
        many += [rule(1000 + n, flagIN=n, destination_hostgroup=100 + n, apply=1) for n in range(1, 71)]
        out = routing(many).evaluate(digest('SELECT 1', hostgroup=170))
        self.assertEqual(out['verdict'], 'unknown')

    def test_unread_fast_routing_is_not_judged(self):
        r = pd.Routing([rule(1, match_digest='^SELECT', destination_hostgroup=32)], None, USERS,
                       {'mysql-query_processor_regex': '1'})
        out = r.evaluate(digest('SELECT 1', hostgroup=40))
        self.assertEqual(out['verdict'], 'unknown')
        self.assertIn('fast_routing could not be read', out['text'])

    def test_next_query_flag_in_opens_another_start(self):
        rules = [rule(1, match_digest='^SET', next_query_flagIN=7, apply=1),
                 rule(2, flagIN=7, destination_hostgroup=70, apply=1),
                 rule(3, match_digest='^SELECT', destination_hostgroup=32, apply=1)]
        out = routing(rules).evaluate(digest('SELECT 1', hostgroup=70))
        self.assertEqual((out['verdict'], out['possible']), ('possible', [1]))
        self.assertIn("sets the next query's flagIN", out['text'])
        self.assertEqual(routing(rules).evaluate(digest('SELECT 1', hostgroup=90))['verdict'], 'mismatch')

    def test_query_cache_and_too_many_rules(self):
        out = routing([rule(1, match_digest='.', destination_hostgroup=32, apply=1)]).evaluate(
            digest('SELECT 1', hostgroup=-1))
        self.assertEqual(out['verdict'], 'cache')
        self.assertIn('query cache', out['text'])
        many = routing([rule(i, match_digest='.') for i in range(pd.RULE_LIMIT + 1)])
        self.assertFalse(many.context()['evaluated'])
        self.assertEqual(many.evaluate(digest('SELECT 1'))['verdict'], 'unknown')
        self.assertEqual(routing(None).context()['note'],
                         'runtime_mysql_query_rules could not be read, so routing is not evaluated.')

    def test_regex_engine_is_named(self):
        self.assertEqual(routing([], variables={'mysql-query_processor_regex': '2'}).context()['regex_engine'], 'RE2')
        self.assertEqual(routing([]).context()['regex_engine'], 'PCRE')


class TestReport(Home):
    def test_the_lab_cross_check(self):
        from test_chart_data import validate
        report = proxysql.read_status(self.admin())
        validate(report, PROXY_SCHEMA)
        by = dict(((d['hostgroup'], d['digest']), d['routing']) for d in report['query_digests'])
        self.assertEqual(by[(32, '0xA8027DF9CE26E2FF')]['verdict'], 'match')
        self.assertEqual(by[(10, '0xA8027DF9CE26E2FF')]['verdict'], 'possible', 'tagged /* galera */, rule 10')
        self.assertEqual(by[(30, '0x7499BD50A3EBE746')]['decisive'], 20, 'FOR UPDATE stops at rule 20')
        self.assertEqual(by[(30, '0x48BE884F70034786')]['source'], 'default', 'SHOW: mf_admin default 30')
        rules = dict((r['rule_id'], r) for r in report['query_rules'])
        self.assertEqual((rules[10]['digest_count'], len(rules[10]['possible_digests'])), (0, 2))
        self.assertEqual(rules[20]['digest_count'], 1)
        self.assertEqual(rules[30]['digest_count'], 5)
        self.assertEqual(report['routing']['regex_engine'], 'PCRE')
        self.assertTrue(all(sql.startswith('SELECT') for sql in report['statements']))
        self.assertIn('SELECT username, active, default_hostgroup, transaction_persistent, frontend FROM '
                      'runtime_mysql_users', report['statements'], 'named columns: never the password')


class TestDigests(Home):
    def test_unread_runtime_variables_survive_the_admin_read(self):
        conn = self.admin()
        conn.db.execute("DELETE FROM runtime_mysql_query_rules")
        conn.db.executemany("INSERT INTO runtime_mysql_query_rules "
                            "(rule_id, active, flagIN, flagOUT, destination_hostgroup, apply) VALUES (?,?,?,?,?,?)",
                            [(1, 1, 5, None, 50, 1), (2, 1, 0, 5, None, 0)])
        conn.db.execute("DELETE FROM runtime_global_variables WHERE variable_name = 'mysql-query_processor_iterations'")
        out = pd.read_digests(conn)
        self.assertTrue(out['digests'])
        for d in out['digests']:
            self.assertEqual(d['routing']['verdict'], 'unknown')
            self.assertIn('mysql-query_processor_iterations', ' '.join(d['routing']['why']))
        unread = pd.read_digests(self.admin(missing=('runtime_global_variables',)))
        selected = next(d for d in unread['digests'] if d['digest_text'] == 'SELECT @@hostname')
        self.assertEqual(selected['routing']['verdict'], 'unknown')
        self.assertIn('mysql-query_processor_regex', ' '.join(selected['routing']['why']))

    def test_digest_keys_encode_the_identity_without_delimiter_collisions(self):
        identities = [('a|b', 'c', ''), ('a', 'b|c', ''), ('a', 'b', 'c|'),
                      ('a', 'b|', 'c'), ('a"\\\u00e7', 'b', '\n')]
        rows = [digest('SELECT ?', schema=s, user=u, client=c) for s, u, c in identities]
        keys = [pd.digest_key(r) for r in rows]
        self.assertEqual(len(set(keys)), len(rows), keys)
        for row, key in zip(rows, keys):
            self.assertEqual(json.loads(key[2:]), [row[k] for k in
                             ('hostgroup', 'schemaname', 'username', 'client_address', 'digest')])

    def test_digest_hash_filter_normalizes_lowercase_and_mixed_case(self):
        expected = pd.read_digests(self.admin(), digest='0xA8027DF9CE26E2FF')['digests']
        self.assertEqual(len(expected), 2)
        for value in ('0xa8027df9ce26e2ff', '0xa8027Df9cE26e2Ff'):
            out = pd.read_digests(self.admin(), digest=value)
            self.assertEqual(out['digests'], expected)
            self.assertEqual(out['filters']['digest'], '0xA8027DF9CE26E2FF')

    def read(self, conn=None, **filters):
        conn = conn or self.admin()
        return pd.read_digests(conn, **filters), conn

    def test_every_order_is_bounded_in_admin_and_validates(self):
        from test_chart_data import validate
        expected = {'slowest': '0x48BE884F70034786', 'time': '0x48BE884F70034786', 'frequent': '0x9C012E222EE5AA4E',
                    'rate': '0x9C012E222EE5AA4E', 'rows': '0x9C012E222EE5AA4E'}
        for order in pd.ORDER_IDS:
            for limit in pd.LIMITS:
                out, conn = self.read(order=order, limit=limit)
                validate(out, DIGEST_SCHEMA)
                self.assertEqual(out['filters']['order'], order)
                self.assertEqual(out['digests'][0]['digest'], expected[order], order)
                sql = [c for c in conn.calls if 'ORDER BY' in c and 'GROUP BY' not in c][0]
                self.assertTrue(sql.endswith('LIMIT %d' % (limit + 1)), sql)
                self.assertLessEqual(len(out['digests']), limit)
        out, _ = self.read(order='rate')
        self.assertEqual(out['digests'][0]['rate_qps'], 20.0, '20 executions within one second')
        out, _ = self.read(order='slowest', limit=10)
        averages = [d['avg_time_us'] for d in out['digests']]
        self.assertEqual(averages, sorted(averages, reverse=True))
        self.assertEqual((out['totals']['digests'], out['totals']['executions']), (10, 80))
        self.assertEqual(out['window'], {'first_seen': 1792537331, 'last_seen': 1792543106})
        self.assertEqual([f['value'] for f in out['facets']['hostgroups']], [32, 30, 10])

    def test_nothing_typed_reaches_the_statement_and_reset_is_never_named(self):
        conn = self.admin()
        typed = "stats_mysql_query_digest_reset' OR '1'='1"
        out = pd.read_digests(conn, text=typed, schema=typed, user=typed)
        self.assertEqual(out['digests'], [])
        for sql in conn.calls:
            self.assertNotIn('_reset', sql)
            self.assertNotIn("'1'='1", sql)
        out = pd.read_digests(self.admin(), text='ORDERS')
        self.assertEqual(sorted(d['digest'] for d in out['digests']),
                         ['0x4A8BCC5DA1E63EFB', '0x7499BD50A3EBE746', '0x9C012E222EE5AA4E'], 'ASCII case folded')
        self.assertEqual(len(pd.read_digests(self.admin(), hostgroup=10)['digests']), 2)
        self.assertEqual(len(pd.read_digests(self.admin(), user='mf_admin', schema='information_schema')['digests']), 10)
        self.assertEqual([d['hostgroup'] for d in pd.read_digests(self.admin(), digest='0xA8027DF9CE26E2FF')['digests']],
                         [32, 10])

    def test_an_empty_schema_is_a_filter_of_its_own(self):
        conn = self.admin()
        conn.db.execute("INSERT INTO stats.stats_mysql_query_digest VALUES (30, '', 'mf_admin', '', '0xE1', "
                        "'SELECT ?', 2, 1, 2, 10, 5, 5, 0, 2)")
        out = pd.read_digests(conn, schema='')
        self.assertEqual([d['digest'] for d in out['digests']], ['0xE1'])
        self.assertEqual(len(pd.read_digests(self.admin(), schema=None)['digests']), 10)
        self.assertEqual(pd.check_filters(schema='')['schema'], '')
        self.assertIsNone(pd.check_filters(text='')['text'])

    def test_more_and_limits(self):
        out, _ = self.read(limit=10)
        self.assertFalse(out['more'])
        conn = self.admin()
        conn.db.executemany('INSERT INTO stats.stats_mysql_query_digest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            [(30, 's', 'u', '', '0xF%d' % i, 'SELECT %d' % i, 1, 1, 1, 1, 1, 1, 0, 1) for i in range(5)])
        out, _ = self.read(conn, limit=10)
        self.assertTrue(out['more'])
        self.assertEqual(len(out['digests']), 10)

    def test_by_rule_scans_a_bounded_set(self):
        out, conn = self.read(rule_id=30, limit=10)
        self.assertEqual(len(out['digests']), 5)
        self.assertTrue(all(30 in d['routing']['rules'] for d in out['digests']))
        self.assertEqual(out['scanned'], 10)
        self.assertTrue([c for c in conn.calls if c.endswith('LIMIT %d' % pd.RULE_SCAN)])
        with self.assertRaises(ValueError):
            pd.read_digests(self.admin(), rule_id=99)

    def test_rows_sent_needs_the_column(self):
        conn = self.admin()
        conn.db.execute('DROP TABLE stats.stats_mysql_query_digest')
        conn.db.execute('CREATE TABLE stats.stats_mysql_query_digest (hostgroup INT, schemaname VARCHAR, '
                        'username VARCHAR, client_address VARCHAR, digest VARCHAR, digest_text VARCHAR, '
                        'count_star INT, first_seen INT, last_seen INT, sum_time INT, min_time INT, max_time INT)')
        conn.db.executemany('INSERT INTO stats.stats_mysql_query_digest VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            [r[:12] for r in DIGESTS])
        with self.assertRaises(ValueError) as caught:
            pd.read_digests(conn, order='rows')
        self.assertIn('2.0.7', str(caught.exception))
        out = pd.read_digests(conn, order='time')
        self.assertEqual(out['digests'][0]['rows_sent'], None, 'a column this version lacks is unknown')

    def test_inputs_are_validated(self):
        for bad in ({'order': 'fastest'}, {'limit': 7}, {'limit': True}, {'hostgroup': '10'}, {'hostgroup': -2},
                    {'text': 'x' * 201}, {'digest': '9C01'}, {'rule_id': True}, {'schema': 5}):
            with self.assertRaises(ValueError, msg=bad):
                pd.check_filters(**bad)

    def test_not_proxysql(self):
        out = pd.read_digests(NotProxy())
        self.assertEqual((out['is_proxysql'], out['digests']), (False, []))
        self.assertIn('not a ProxySQL Admin interface', out['message'])


class Endpoints(Home):
    def setUp(self):
        super().setUp()
        from myflames.workspace_store import WorkspaceStore
        self.workspace = type('Workspace', (), {'store': WorkspaceStore(os.path.join(self.tmp.name, 'ws.sqlite3'))})()
        self.conn = self.admin()
        self.connection = {'host': '127.0.0.1', 'port': 6032, 'user': 'radmin', 'password': 'pw-secret'}

    def api(self, path, body, conn=None):
        with mock.patch('myflames.connector.MySQLConnection', Factory(conn or self.conn)):
            return ui.api_response(path, body, self.workspace)

    def plan(self, action, target=None, value=None):
        body = {'connection': self.connection, 'action': action, 'target': target}
        if value is not None:
            body['value'] = value
        return self.api('/api/live/proxysql/plan', body)

    def send(self, plan, progress_id=None, typed=None, conn=None):
        plan = dict(plan)
        token = plan.pop('review_token', None)
        return self.api('/api/live/proxysql/action', {'connection': self.connection, 'plan': plan,
                                                       'review_token': token, 'typed': typed, 'confirmed': True,
                                                       'progress_id': progress_id}, conn)

    def progress(self, progress_id):
        return ui.api_response('/api/live/action/progress', {'progress_id': progress_id}, self.workspace)

    def test_digests_endpoint_and_paths(self):
        out = self.api('/api/live/proxysql/digests', {'connection': self.connection, 'order': 'frequent', 'limit': 25,
                                                      'hostgroup': 32})
        self.assertEqual((out['filters']['order'], out['filters']['limit'], len(out['digests'])), ('frequent', 25, 3))
        self.assertIn('/api/live/proxysql/digests', ui._SERVER_VIEW_PATHS)
        self.assertNotIn('/api/live/proxysql/digests', ui._WRITE_PATHS)
        import inspect
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        self.assertIn('"/api/live/proxysql/digests"', source)
        self.assertIn('_ACTION_PROGRESS_PATH', source)
        self.assertNotIn(ui._ACTION_PROGRESS_PATH, ui._CONNECTION_PATHS, 'progress opens no connection')
        with self.assertRaises(ValueError):
            self.api('/api/live/proxysql/digests', {'connection': self.connection, 'order': 'nope'})

    def test_progress_follows_the_send_and_done_waits_for_the_answer(self):
        seen = []
        conn = self.admin()
        plan = self.plan('server_weight', {'config': [30, 'mf-ic-2', 3306]}, 50)
        self.assertEqual(plan['timeout_s'], proxysql.ACTION_TIMEOUT)
        self.assertTrue(plan['checks'] and 'Refresh this place' in plan['checks'][0])
        pid = 'p' * 32
        original = conn.run_write

        def run_write(sql, extra_flags=None, timeout=None):
            # While the server works on it: sent, never done.
            seen.append(self.progress(pid))
            return original(sql, extra_flags, timeout)
        conn.run_write = run_write
        result = self.send(plan, pid, conn=conn)
        self.assertEqual(result['status'], 'done', result)
        (during,) = seen
        self.assertEqual((during['phase'], during['states']), ('sending', ['sent', 'sent']))
        self.assertIsNotNone(during['waiting_s'])
        after = self.progress(pid)
        self.assertEqual((after['phase'], after['states'], after['status']), ('finished', ['done', 'done'], 'done'))
        self.assertEqual(result['states'], ['done', 'done'])
        self.assertEqual(result['effect'][0], ['Weight in memory', '100', '50'])
        self.assertEqual(result['effect'][1], ['Runtime', 'hostgroup 32 ONLINE, weight 100',
                                               'hostgroup 32 ONLINE, weight 50'])
        self.assertEqual(result['effect'][-1], ['Memory versus disk', 'Match', '1 difference'])
        self.assertEqual(result['checks'], plan['checks'])
        self.assertTrue(result['audit_id'])

    def test_failed_partial_unknown_and_refused_states(self):
        target = {'config': [30, 'mf-ic-2', 3306]}
        partial = self.admin(fail_line=2)
        result = self.send(self.plan('server_weight', target, 50), 'q' * 20, conn=partial)
        self.assertEqual((result['status'], result['states']), ('partial', ['done', 'failed']))
        self.assertEqual(self.progress('q' * 20)['states'], ['done', 'failed'])
        failed = self.admin(fail_line=1)
        result = self.send(self.plan('server_weight', target, 50), conn=failed)
        self.assertEqual((result['status'], result['states']), ('failed', ['failed', 'not_run']))
        lost = self.admin(timeout_line=1)
        result = self.send(self.plan('server_weight', target, 50), 'r' * 20, conn=lost)
        self.assertEqual((result['status'], result['states'], result['effect']), ('unknown', ['unknown', 'unknown'], None))
        self.assertEqual(self.progress('r' * 20)['status'], 'unknown')
        plan = self.plan('server_weight', target, 50)
        plan = dict(plan, review_token='forged')
        result = self.send(plan, 's' * 20)
        self.assertEqual((result['status'], result['states']), ('refused', ['not_sent', 'not_sent']))
        self.assertEqual(self.progress('s' * 20)['phase'], 'finished')

    def test_progress_ids(self):
        self.assertEqual(self.progress('z' * 32)['phase'], 'unknown')
        with self.assertRaises(ValueError):
            self.progress('../x')
        registry = reviewed_writes.ProgressRegistry()
        self.assertIsNone(registry.start(None, 2))
        self.assertIsNone(registry.start('short', 2))
        self.assertIsNotNone(registry.start('a' * 16, 2))
        self.assertIsNone(registry.start('a' * 16, 2), 'a reused id shows no progress')
        for i in range(registry.LIMIT + 5):
            registry.start('id-%016d' % i, 1)
        self.assertLessEqual(len(registry._items), registry.LIMIT)
        snapshot = registry.get('id-%016d' % (registry.LIMIT + 4))
        self.assertEqual(snapshot['states'], ['waiting'])
        self.assertFalse(re.search('SELECT|UPDATE|password', json.dumps(snapshot)))

    def test_rule_and_module_effects(self):
        result = self.send(self.plan('rule_disable', {'rule_id': 30}))
        self.assertEqual(result['effect'][:2], [['Rule 30 in memory', 'Active', 'Not active'],
                                                ['Rule 30 in runtime', 'Active', 'Not active']])
        result = self.send(self.plan('query_rules_save'))
        self.assertEqual(result['effect'], [['Memory versus disk', '1 difference', 'Match']])


if __name__ == '__main__':
    unittest.main()
