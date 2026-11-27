"""Synthetic preflight aliases preserve exact replay SQL and consumption."""
import unittest

from _perf_replay import ReplayConn, expanded_preflight_sql


NAMES = ('performance_schema', 'userstat', 'version', 'version_comment')
RESPONSE = 'performance_schema\tON\nversion\t8.4.11\nversion_comment\tSource distribution\n'


def statement(names=NAMES, tag='test'):
    return ('/*myflames:%s*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN (%s)'
            % (tag, ', '.join("'%s'" % name for name in names)))


class TestExpandedPreflightSQL(unittest.TestCase):
    def test_four_identity_names(self):
        self.assertEqual(expanded_preflight_sql(statement(), RESPONSE),
                         statement(NAMES + ('villagesql_server_version',)))

    def test_extra_names_and_any_view_tag(self):
        for tag, extras in (('report', ()), ('waits', ('datadir',)),
                            ('io', ('datadir', 'innodb_data_home_dir')),
                            ('config', ('max_connections', 'innodb_buffer_pool_size'))):
            with self.subTest(tag=tag):
                names = tuple(sorted(NAMES + extras))
                self.assertEqual(expanded_preflight_sql(statement(names, tag), RESPONSE),
                                 statement(sorted(names + ('villagesql_server_version',)), tag))

    def test_existing_name_is_unchanged(self):
        sql = statement(NAMES + ('villagesql_server_version',))
        self.assertEqual(expanded_preflight_sql(sql, RESPONSE), sql)

    def test_villagesql_recording_is_unchanged(self):
        for response, version in (
                (RESPONSE.replace('8.4.11', '8.4.11-villagesql-0.0.8'), ''),
                (RESPONSE.replace('Source distribution', 'VillageSQL Server - GPL'), ''),
                (RESPONSE, '8.4.11-villagesql-0.0.8')):
            with self.subTest(response=response, version=version):
                self.assertEqual(expanded_preflight_sql(statement(), response, version), statement())
                conn = ReplayConn([[statement(), response]], version=version)
                self.assertEqual(conn.aliases, {})

    def test_other_statements_are_unchanged(self):
        for sql in ('/*myflames:test*/ SELECT @@version',
                    '/*myflames:test*/ SHOW GLOBAL VARIABLES',
                    statement(('version', 'version_comment')),
                    statement() + ' LIMIT 1',
                    statement().replace(', ', ',')):
            with self.subTest(sql=sql):
                self.assertEqual(expanded_preflight_sql(sql, RESPONSE), sql)

    def test_noncanonical_recordings_cannot_alias_sorted_or_deduplicated_sql(self):
        for names in (tuple(reversed(NAMES)), NAMES + ('version',),
                      tuple(reversed(NAMES)) + ('version',)):
            with self.subTest(names=names):
                original = statement(names)
                self.assertEqual(expanded_preflight_sql(original, RESPONSE), original)
                conn = ReplayConn([[original, RESPONSE]])
                self.assertEqual(conn.aliases, {})
                with self.assertRaisesRegex(AssertionError, 'unexpected SQL'):
                    conn.run(statement(sorted(NAMES + ('villagesql_server_version',))))

    def test_alias_consumes_original_and_preserves_other_unseen_answers(self):
        original = statement()
        expanded = expanded_preflight_sql(original, RESPONSE)
        other = '/*myflames:test*/ SHOW GLOBAL STATUS'
        calls = [[original, RESPONSE], [other, 'Uptime\t100\n']]
        conn = ReplayConn(calls)
        self.assertEqual(conn.aliases, {expanded: original})
        self.assertEqual(conn.run(expanded), RESPONSE)
        self.assertEqual(conn.seen, {original: 1, expanded: 1})
        self.assertEqual([sql for sql in conn.answers if sql not in conn.seen], [other])
        self.assertEqual(calls, [[original, RESPONSE], [other, 'Uptime\t100\n']])
        with self.assertRaisesRegex(AssertionError, 'unexpected SQL'):
            conn.run(statement(NAMES + ('unrelated_variable',)))

    def test_original_and_alias_share_recorded_response_order(self):
        original = statement()
        expanded = expanded_preflight_sql(original, RESPONSE)
        conn = ReplayConn([[original, RESPONSE], [original, 'second response']])
        self.assertEqual(conn.run(original), RESPONSE)
        self.assertEqual(conn.run(expanded), 'second response')
        self.assertEqual(conn.seen, {original: 2, expanded: 2})

    def test_real_expanded_recording_keeps_its_independent_answer_stream(self):
        original = statement()
        expanded = expanded_preflight_sql(original, RESPONSE)
        for calls in ([[original, RESPONSE], [expanded, 'expanded first'],
                       [original, 'original second'], [expanded, 'expanded second']],
                      [[expanded, 'expanded first'], [original, RESPONSE],
                       [expanded, 'expanded second'], [original, 'original second']]):
            with self.subTest(calls=calls):
                conn = ReplayConn(calls)
                self.assertNotIn(expanded, conn.aliases)
                self.assertEqual(conn.run(expanded), 'expanded first')
                self.assertEqual(conn.run(original), RESPONSE)
                self.assertEqual(conn.run(expanded), 'expanded second')
                self.assertEqual(conn.run(original), 'original second')
                self.assertEqual(conn.seen, {original: 2, expanded: 2})


if __name__ == '__main__':
    unittest.main()
