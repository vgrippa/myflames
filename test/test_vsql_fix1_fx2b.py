"""FX2b lifecycle regressions: isolated fake tools, exact SQL, no live server.

The live VillageSQL dump checks are run separately. The paired cases here
exercise review/rebuild/execution through the existing lifecycle harness.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_lifecycle as harness
import test_upgrade_targets as upgrade_harness
from _perf_replay import expanded_preflight_sql
from myflames import lifecycle as lc, server_info, upgrade_targets as ut

VERSION = '8.4.11-villagesql-0.0.8'
PERCONA_COMMENT = 'Percona Server (GPL), Release 11, Revision 57878ff8'
PRODUCTS = (
    ('mysql', '8.4.11', 'MySQL Community Server - GPL', 'mysql'),
    ('percona', '8.4.11-11', PERCONA_COMMENT, 'mysql'),
    ('mariadb', '11.4.13-MariaDB', 'MariaDB Server', 'mariadb'),
    ('readyset', '8.0.26', 'Readyset', 'mysql'),
)
SHELL_REASON = ('MySQL Shell cannot dump VillageSQL custom data types '
                '(for example vsql_vector.SVECTOR); use mysqldump.')


# Frozen before FX2b edits by running the lifecycle fake-tool harness on
# MySQL, Percona, MariaDB and Readyset. These synthetic recordings
# assert every SQL byte, statement order and client flag through review,
# rebuild, dump, restore and verification; no expected SQL comes from
# current lifecycle constants. Existing test_lifecycle also replays live
# native-upgrade recordings from test/fixtures/lifecycle.
BASELINE_READS = (
    ((
        "/*myflames:lifecycle*/ SHOW GLOBAL VARIABLES WHERE Variable_name IN (CONVERT(X'76657273696f6e' USING utf8mb4),"
        "CONVERT(X'76657273696f6e5f636f6d6d656e74' USING utf8mb4),CONVERT(X'686f73746e616d65' USING utf8mb4),CONVERT(X'"
        "706f7274' USING utf8mb4),CONVERT(X'7365727665725f75756964' USING utf8mb4),CONVERT(X'7365727665725f6964' USING "
        "utf8mb4),CONVERT(X'64617461646972' USING utf8mb4),CONVERT(X'736f636b6574' USING utf8mb4),CONVERT(X'6c6f63616c5"
        "f696e66696c65' USING utf8mb4),CONVERT(X'73716c5f6d6f6465' USING utf8mb4),CONVERT(X'76657273696f6e5f636f6d70696"
        "c655f6f73' USING utf8mb4),CONVERT(X'76657273696f6e5f636f6d70696c655f6d616368696e65' USING utf8mb4),CONVERT(X'6"
        "368617261637465725f7365745f736572766572' USING utf8mb4),CONVERT(X'6368617261637465725f7365745f66696c6573797374"
        "656d' USING utf8mb4),CONVERT(X'636f6c6c6174696f6e5f736572766572' USING utf8mb4),CONVERT(X'696e6e6f64625f706167"
        "655f73697a65' USING utf8mb4),CONVERT(X'6d61785f616c6c6f7765645f7061636b6574' USING utf8mb4),CONVERT(X'636c6f6e"
        "655f76616c69645f646f6e6f725f6c697374' USING utf8mb4),CONVERT(X'677469645f6578656375746564' USING utf8mb4))"
    ), ['--comments']),
    ((
        '/*myflames:lifecycle*/ SELECT TABLE_SCHEMA,TABLE_NAME,TABLE_TYPE,ENGINE,COALESCE(DATA_LENGTH,0),COALESCE(INDEX'
        "_LENGTH,0) FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT IN ('performance_schema','information_schema'"
        ')'
    ), ['--comments']),
    ((
        "/*myflames:lifecycle*/ SELECT 'myflames_grants_begin', USER(), CURRENT_USER(), VERSION(), CONNECTION_ID(), @@g"
        'lobal.server_id, @@global.hostname, @@global.port; /*myflames:lifecycle*/ SHOW GLOBAL VARIABLES WHERE Variable'
        "_name IN ('server_uuid', 'partial_revokes'); /*myflames:lifecycle*/ SELECT 'myflames_grants_rows'; /*myflames:"
        "lifecycle*/ SHOW GRANTS; /*myflames:lifecycle*/ SELECT 'myflames_grants_end', USER(), CURRENT_USER(), VERSION("
        '), CONNECTION_ID(), @@global.server_id, @@global.hostname, @@global.port; /*myflames:lifecycle*/ SHOW GLOBAL V'
        "ARIABLES WHERE Variable_name IN ('server_uuid', 'partial_revokes'); /*myflames:lifecycle*/ SELECT 'myflames_gr"
        "ants_done'"
    ), ['--comments', '--skip-reconnect']),
    ((
        "/*myflames:lifecycle*/ SELECT ID,USER,HOST,TIME,STATE FROM information_schema.PROCESSLIST WHERE COMMAND <> 'Sl"
        "eep' AND TIME >= 30 AND ID <> CONNECTION_ID() LIMIT 20"
    ), ['--comments']),
    ((
        '/*myflames:lifecycle*/ SELECT SCHEMA_NAME,DEFAULT_CHARACTER_SET_NAME,DEFAULT_COLLATION_NAME FROM information_s'
        "chema.SCHEMATA WHERE SCHEMA_NAME NOT IN ('performance_schema','information_schema')"
    ), ['--comments']),
    ((
        '/*myflames:lifecycle*/ SELECT COUNT(*) FROM `app`.`t`'
    ), ['--comments']),
    ((
        '/*myflames:lifecycle*/ SET SESSION information_schema_stats_expiry=0;\n/*myflames:lifecycle*/ SELECT TABLE_SCHE'
        'MA,TABLE_NAME,TABLE_TYPE,ENGINE,COALESCE(DATA_LENGTH,0) FROM information_schema.TABLES WHERE TABLE_SCHEMA NOT '
        "IN ('performance_schema','information_schema')"
    ), ['--comments']),
)
BASELINE_DUMP = (0, 1, 2, 3, 0, 0, 1, 2, 3, 0, 1, 4, 5, 6, 5)
BASELINE_MARIA_DUMP = (0, 1, 2, 3, 0, 0, 1, 2, 3, 0, 1, 4, 5, 5)
BASELINE_RESTORE = (0, 1, 2, 0, 0, 1, 2, 0, 0, 1, 5)
BASELINE_MYSQL_ARGS = ['--single-transaction', '--quick', '--routines', '--events', '--triggers', '--hex-blob',
                       '--add-drop-table', '--set-gtid-purged=OFF', '--all-databases']
BASELINE_MARIA_ARGS = ['--single-transaction', '--quick', '--routines', '--events', '--triggers', '--hex-blob',
                       '--add-drop-table', '--all-databases']


def village_variables(base='mysql-8.4_0.0.8', identity='donor'):
    values = harness.server_variables(version=VERSION, identity=identity)
    values.update(version_comment='Source distribution')
    if base is not None:
        values['villagesql_server_version'] = base
    return values


@unittest.skipIf(os.name == 'nt', 'POSIX executable fixtures; Windows trust tested separately')
class LifecycleFixTests(unittest.TestCase):
    setUp = harness.LifecycleTests.setUp
    stop_detect = harness.LifecycleTests.stop_detect
    close_jobs = harness.LifecycleTests.close_jobs
    audit = harness.LifecycleTests.audit
    native_tools = harness.LifecycleTests.native_tools
    calls = harness.LifecycleTests.calls
    make_dump = harness.LifecycleTests.make_dump

    def test_l1_automatic_villagesql_dump_uses_mysqldump(self):
        conn = harness.ReplayConnection(village_variables())
        server = lc.read_server(conn)
        self.assertEqual(lc.offered_tools(server, self.tools)['dump'], ['mysqldump'])
        plan = lc.prepare_dump(conn, harness.CONNECTION, self.output, tools=self.tools)
        self.assertEqual(plan['tool']['name'], 'mysqldump')
        self.assertIn('--all-databases', plan['commands'][0])
        self.assertEqual(self.calls(), [], 'automatic preflight must not run Shell dryRun')

    def test_l1_shell_only_selection_refuses_every_scope_before_catalog_or_tool(self):
        tools = {'tools': {'mysqlsh': self.tools['tools']['mysqlsh']}}
        for schemas, tables in (([], []), (['app'], []), (['app'], ['t'])):
            with self.subTest(schemas=schemas, tables=tables):
                conn = harness.ReplayConnection(village_variables())
                with self.assertRaises(lc.Refused) as refusal:
                    lc.prepare_dump(conn, harness.CONNECTION, self.output,
                                    schemas=schemas, tables=tables, tools=tools)
                self.assertEqual(str(refusal.exception), SHELL_REASON)
                self.assertEqual(len(conn.calls), 1, 'only identity is needed for this refusal')
                self.assertEqual(self.calls(), [], 'no Shell dryRun or dump may execute')

    def test_l2_restore_rebuild_and_verification_keep_fresh_target_flavor(self):
        self.make_dump()
        manifest_path = Path(self.output, lc.MANIFEST)
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['scope'] = {'schemas': [], 'tables': []}
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        target = harness.ReplayConnection(village_variables(identity='recipient'), catalog=[],
                                          restored_marker=str(self.bin / 'restored'))
        self.assertFalse(hasattr(target, '_version_cache'))
        tools = self.native_tools()
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            plan = lc.prepare_restore(target, harness.CONNECTION, self.output, tools=tools,
                                      existing_objects='drop')
            job = lc.restore_job(harness.CONNECTION, plan, True, plan['target'], self.audit(),
                                 factory=lambda **kw: target, start=False,
                                 typed_acknowledgement=plan['required_acknowledgement'])
            self.jobs.append(job)
            result = job.run()
        self.assertEqual(result['state'], 'done', result)
        catalogs = [sql for sql, _flags in target.calls if 'FROM information_schema.TABLES WHERE' in sql]
        self.assertEqual(len(catalogs), 3, 'initial review, rebuilt review and final verification')
        for sql in catalogs:
            self.assertTrue(sql.endswith("TABLE_SCHEMA NOT IN ('performance_schema','information_schema','villagesql')"), sql)
        self.assertEqual(result['result']['status'], 'completed')

    def test_l3_explicit_internal_schema_refuses_before_catalog_or_tool_detection(self):
        for schemas, tables in ((['villagesql'], []), (['app', 'villagesql'], []),
                                (['VillageSQL'], []), (['villagesql'], ['t'])):
            for prefer_shell in (True, False):
                with self.subTest(schemas=schemas, tables=tables, prefer_shell=prefer_shell):
                    conn = harness.ReplayConnection(village_variables())
                    with mock.patch.object(lc, 'detect_tools', return_value=self.tools) as detect:
                        with self.assertRaises(lc.Refused) as refusal:
                            lc.prepare_dump(conn, harness.CONNECTION, self.output, schemas=schemas,
                                            tables=tables, prefer_shell=prefer_shell)
                    self.assertIn('internal', str(refusal.exception))
                    self.assertIn('villagesql', str(refusal.exception))
                    self.assertEqual(len(conn.calls), 1, 'no catalog or sampling query is allowed')
                    detect.assert_not_called()
                    self.assertEqual(self.calls(), [])

    def test_l3_other_products_keep_a_user_database_named_villagesql(self):
        for product, version, comment, engine in PRODUCTS:
            with self.subTest(product=product):
                values = harness.server_variables(engine, version=version)
                values['version_comment'] = comment
                conn = harness.ReplayConnection(values, catalog=[])
                plan = lc.prepare_dump(conn, harness.CONNECTION, self.output, schemas=['villagesql'],
                                       tools=self.native_tools(engine), prefer_shell=False)
                self.assertTrue(plan['ready'])
                self.assertEqual(lc._dump_args(plan['request'], plan['tool'])[-2:],
                                 ['--databases', 'villagesql'])
                catalog_sql = [sql for sql, _flags in conn.calls if 'information_schema.TABLES' in sql]
                self.assertEqual(len(catalog_sql), 1)
                self.assertIn("TABLE_SCHEMA IN (CONVERT(X'76696c6c61676573716c' USING utf8mb4))", catalog_sql[0])

    def test_l5_identity_keeps_known_base_and_does_not_guess_unknown_base(self):
        # The harness can return additional codebase metadata; those first
        # two cases cover propagation only. Real lifecycle identity reads
        # keep their SQL unchanged and otherwise use the comment or None.
        cases = (('mysql-8.4_0.0.8', 'Source distribution', 'mysql'),
                 ('percona-8.4_0.0.8', 'Source distribution', 'percona'),
                 (None, PERCONA_COMMENT, 'percona'),
                 (None, 'Source distribution', None),
                 ('unrecognized', 'Source distribution', None))
        for codebase, comment, expected in cases:
            with self.subTest(codebase=codebase, comment=comment):
                values = dict(village_variables(codebase), version_comment=comment)
                conn = harness.ReplayConnection(values)
                report = lc.read_server(conn)
                self.assertIn('server_base', report)
                self.assertEqual(report['server_base'], expected)
                label = server_info.server_label(report, version_key='version')
                if expected:
                    self.assertIn('(%s 8.4.11)' % ('MySQL' if expected == 'mysql' else 'Percona Server'), label)
                else:
                    self.assertEqual(label, 'VillageSQL 0.0.8 (8.4.11)')
                self.assertEqual(len(conn.calls), 1, 'carrying identity must not add a probe')
        for product, version, comment, engine in PRODUCTS:
            with self.subTest(product=product):
                values = dict(harness.server_variables(engine, version=version), version_comment=comment)
                conn = harness.ReplayConnection(values)
                self.assertNotIn('server_base', lc.read_server(conn))
                self.assertEqual(len(conn.calls), 1)

    def test_l5_base_survives_dump_plan_rebuild_and_manifest(self):
        # The Percona comment is part of the existing identity query; this
        # proves propagation without inventing a codebase metadata read.
        values = dict(village_variables(None), version_comment=PERCONA_COMMENT)
        conn = harness.ReplayConnection(values)
        tools = self.native_tools()
        with mock.patch.object(lc, 'detect_tools', return_value=tools):
            plan = lc.prepare_dump(conn, harness.CONNECTION, self.output, tools=tools)
            job = lc.dump_job(harness.CONNECTION, plan, True, plan['target'], self.audit('dump'),
                              factory=lambda **kw: conn, start=False)
            self.jobs.append(job)
            result = job.run()
        self.assertEqual(result['state'], 'done', result)
        self.assertIn('server_base', plan['server'])
        self.assertEqual(plan['server']['server_base'], 'percona')
        self.assertEqual(result['result']['manifest']['source']['server_base'], 'percona')
        saved = json.loads(Path(self.output, lc.MANIFEST).read_text(encoding='utf-8'))
        self.assertEqual(saved['source']['server_base'], 'percona')

    def test_recorded_statement_and_argv_parity_on_other_products(self):
        for product, version, comment, engine in PRODUCTS:
            with self.subTest(product=product):
                self.output = str(self.root / product)
                values = dict(harness.server_variables(engine, version=version), version_comment=comment)
                conn = harness.ReplayConnection(values)
                tools = self.native_tools(engine)
                with mock.patch.object(lc, 'detect_tools', return_value=tools):
                    plan = lc.prepare_dump(conn, harness.CONNECTION, self.output, tools=tools, prefer_shell=False)
                    dump = lc.dump_job(harness.CONNECTION, plan, True, plan['target'], self.audit('dump'),
                                       factory=lambda **kw: conn, start=False)
                    self.jobs.append(dump)
                    result = dump.run()
                self.assertEqual(result['state'], 'done', result)
                order = BASELINE_MARIA_DUMP if engine == 'mariadb' else BASELINE_DUMP
                # Retain every baseline byte except the strict optional-name alias.
                self.assertEqual(conn.calls, [(expanded_preflight_sql(BASELINE_READS[i][0]),
                                               BASELINE_READS[i][1]) for i in order])
                args = BASELINE_MARIA_ARGS if engine == 'mariadb' else BASELINE_MYSQL_ARGS
                self.assertEqual(lc._dump_args(plan['request'], plan['tool']), args)
                executed = [call['argv'][2:] for call in self.calls() if call['name'] == plan['tool']['name']]
                self.assertEqual(executed[-1], args, 'the reviewed dump options must be executed unchanged')
                target_values = dict(values, hostname='recipient', server_id='2', server_uuid='recipient')
                target = harness.ReplayConnection(target_values, catalog=[],
                                                  restored_marker=str(self.bin / 'restored'))
                with mock.patch.object(lc, 'detect_tools', return_value=tools):
                    restore_plan = lc.prepare_restore(target, harness.CONNECTION, self.output, tools=tools,
                                                      existing_objects='drop')
                    restore = lc.restore_job(harness.CONNECTION, restore_plan, True, restore_plan['target'],
                                             self.audit(), factory=lambda **kw: target, start=False,
                                             typed_acknowledgement=restore_plan['required_acknowledgement'])
                    self.jobs.append(restore)
                    result = restore.run()
                self.assertEqual(result['state'], 'done', result)
                self.assertEqual(result['result']['status'], 'completed')
                self.assertEqual(target.calls, [(expanded_preflight_sql(BASELINE_READS[i][0]),
                                                 BASELINE_READS[i][1]) for i in BASELINE_RESTORE])

    def test_l1_other_products_keep_automatic_tool_selection(self):
        for product, version, comment, engine in PRODUCTS:
            with self.subTest(product=product):
                values = dict(harness.server_variables(engine, version=version), version_comment=comment)
                conn = harness.ReplayConnection(values)
                plan = lc.prepare_dump(conn, harness.CONNECTION, str(self.root / product), tools=self.tools)
                self.assertEqual(plan['tool']['name'], 'mariadb-dump' if engine == 'mariadb' else 'mysqlsh')
                if engine == 'mysql':
                    self.assertTrue(plan['commands'][0].startswith('util.dump_instance('))
                    self.assertNotIn('excludeSchemas', plan['commands'][0])


class UpgradeTargetsFixTests(unittest.TestCase):
    setUp = upgrade_harness.Base.setUp

    def test_l4_villagesql_withholds_upstream_targets_and_package_guidance(self):
        for comment in ('Source distribution', PERCONA_COMMENT):
            with self.subTest(comment=comment):
                layout = dict(village_variables(), version_comment=comment,
                              basedir='/usr/', plugin_dir='/usr/lib64/mysql/plugin/')
                report = ut.build_report(layout)
                self.assertEqual(report['server']['flavor'], 'villagesql')
                self.assertEqual(report['targets'], [])
                self.assertIsNone(report['recommended'])
                self.assertEqual(report['source']['kind'], 'none')
                self.assertEqual(report['source']['urls'], [])
                self.assertIn('VillageSQL publishes its own releases (0.0.x)', report['source']['label'])
                self.assertIsNone(report['guidance']['command'])
                self.assertIsNone(report['guidance']['url'])
                self.assertEqual(report['not_direct'], [])
                self.assertEqual(report['unclassified'], [])

    def test_l4_villagesql_does_not_fetch_oracle_or_percona_releases(self):
        for comment in ('Source distribution', PERCONA_COMMENT):
            with self.subTest(comment=comment):
                conn = upgrade_harness.LayoutConnection(dict(village_variables(), version_comment=comment))
                fetch = upgrade_harness.FakeReader()
                report = ut.upgrade_targets(conn, fetch=fetch)
                self.assertEqual(fetch.calls, [])
                self.assertEqual(len(conn.calls), 1, 'the existing layout read is sufficient')
                self.assertEqual(report['targets'], [])


if __name__ == '__main__':
    unittest.main()
