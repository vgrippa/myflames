"""Cluster privilege/version matrix and MariaDB same-session identity evidence.

Only connection I/O is synthetic. The grants parser, effective privilege
checks, write classifier, reviewed-plan validation and sender are real.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import _cluster_fixtures as F
from _perf_replay import batch
from _grants_fixtures import grants_batch
from _reviewed_replay import guarded_packets
from myflames import cluster as m, cluster_shell, dbops, privileges, reviewed_writes
from myflames.connector import ConnectorError
from test_cluster import install_clusterset_batch_replay

install_clusterset_batch_replay()


SHELL = {'path': '/opt/mysqlsh/bin/mysqlsh', 'version': '8.4.6', 'version_tuple': [8, 4, 6]}
ACCOUNT = 'monitor@%'
QUOTED = '`monitor`@`%`'
FIX = 'Run the statement below as an administrator, then Refresh.'
UNREADABLE = 'Privileges could not be read; the server refuses the change if one is missing.'
RUNTIME = 'MySQL Shell checks the privileges it needs when it runs.'
METADATA = ('mysql_innodb_cluster_metadata', 'mysql_innodb_cluster_metadata_bkp',
            'mysql_innodb_cluster_metadata_previous')
METADATA_PRIVILEGES = ('ALTER', 'ALTER ROUTINE', 'CREATE', 'CREATE ROUTINE', 'CREATE TEMPORARY TABLES',
                       'CREATE VIEW', 'DELETE', 'DROP', 'EVENT', 'EXECUTE', 'INDEX', 'INSERT', 'LOCK TABLES',
                       'REFERENCES', 'SHOW VIEW', 'TRIGGER', 'UPDATE')
SCHEMA_PRIVILEGES = [('mysql', ('DELETE', 'INSERT', 'UPDATE'))] + [(s, METADATA_PRIVILEGES) for s in METADATA]


def shell_globals(version):
    """Independent expected list, from Shell accounts.cc and review B3."""
    result = ['RELOAD', 'SHUTDOWN', 'PROCESS', 'FILE', 'SELECT', 'REPLICATION SLAVE', 'REPLICATION CLIENT',
              'CREATE USER', 'SYSTEM_VARIABLES_ADMIN', 'PERSIST_RO_VARIABLES_ADMIN', 'REPLICATION_SLAVE_ADMIN',
              'GROUP_REPLICATION_ADMIN', 'ROLE_ADMIN', 'CONNECTION_ADMIN']
    if version >= (8, 0, 17):
        result += ['EXECUTE', 'CLONE_ADMIN']
    if version >= (8, 0, 18):
        result += ['REPLICATION_APPLIER']
    if version >= (8, 3, 0):
        result += ['TRANSACTION_GTID_TAG']
    return sorted(result)


def statement(names, database=None, grantable=False, account=QUOTED, partial_revokes=False):
    if database and not partial_revokes:
        database = database.replace('\\', '\\\\').replace('_', '\\_').replace('%', '\\%')
    return 'GRANT %s ON %s TO %s%s;' % (', '.join(names), '`%s`.*' % database if database else '*.*', account,
                                       ' WITH GRANT OPTION' if grantable else '')


def shell_grants(version, omit=None, grantable=True, partial_revokes=False):
    names = shell_globals(version)
    # A global grant also satisfies a schema requirement. Remove it too when
    # isolating a scoped denial; the expected result then names both needs.
    if omit and (omit[0] is None or omit[1] in names):
        names.remove(omit[1])
    lines = [statement(names, grantable=grantable)]
    for database, names in SCHEMA_PRIVILEGES:
        names = [p for p in names if omit != (database, p)]
        lines.append(statement(names, database, grantable, partial_revokes=partial_revokes))
    return lines


def label(privilege, database=None, grantable=True):
    return privilege + (' ON `%s`.*' % database if database else '') + (' WITH GRANT OPTION' if grantable else '')


class GrantsClient(object):
    """Read real fixture topology; supply explicit grant and identity answers."""
    def __init__(self, scenario='gr-8.4-two-members', grants=None, account=ACCOUNT, denied=False, variables=None):
        self.conn = F.conn(scenario)
        self.grants = [statement(['USAGE'])] if grants is None else grants
        self.account, self.denied, self.variables = account, denied, variables or {}
        self.reads, self.batches, self.writes = [], [], []
        self.guard_mismatch = False

    def run(self, sql, extra_flags=None, timeout=None):
        self.reads.append(sql)
        if 'myflames_grants_begin' in sql:
            if self.denied:
                raise ConnectorError('ERROR 1142 (42000): SHOW GRANTS denied')
            return grants_batch(self.grants, account=self.account, login='login@client',
                                version=self.conn.server_version(), variables=self.variables)
        if sql == m.CLUSTER_TAG + 'SELECT USER(), CURRENT_USER() LIMIT 1':
            return batch([['login@client', self.account]])
        if sql == m.CLUSTER_TAG + 'SHOW GRANTS':
            if self.denied:
                raise ConnectorError('ERROR 1142 (42000): SHOW GRANTS denied')
            return batch([[line] for line in self.grants])
        answer = self.conn.run(sql, extra_flags=extra_flags, timeout=timeout)
        if sql.startswith(m.CLUSTER_TAG + 'SHOW GLOBAL VARIABLES WHERE'):
            answer = F.set_pairs(answer, self.variables)
        return answer

    def run_write(self, sql, extra_flags=None, timeout=None):
        self.batches.append(sql)
        if self.guard_mismatch:
            # Emulate refusal of the guard within the first query packet.
            raise ConnectorError('ERROR 1242 (21000) at line 2: Subquery returns more than 1 row')
        self.writes.extend(s for _, s, _ in guarded_packets(sql))
        return ''

    def __getattr__(self, name):
        return getattr(self.conn, name)


class _ClusterPrivilegeCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = mock.patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                          'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)

    def checks(self, grants, account=ACCOUNT):
        return {'shell': SHELL, 'bootstrap_group': 'OFF',
                'grants': {'checked': True, 'account': account, 'grants': grants, 'partial_revokes': False}}

    def report(self, version, scenario='gr-8.4-two-members'):
        report = F.report(scenario)
        report['server_version'] = '.'.join(str(n) for n in version)
        for member in report['members']:
            member['version'] = report['server_version']
        return report

    def action(self, report, aid, checks, target=None):
        if aid.startswith('switch_to_') or aid.split(':')[0] in (
                'set_primary_cluster', 'force_primary_cluster', 'rejoin_cluster', 'repair_cluster',
                'remove_cluster', 'remove_cluster_keep', 'set_routing_option', 'create_replica_cluster',
                'dissolve_clusterset', 'check_clusterset'):
            actions = m.overview_actions(report, checks)
        else:
            target = target or next(x for x in report['members'] if x['local'])
            actions = m.available_actions(report, target['key'], checks)
        return next(a for a in actions if a['id'] == aid)

    def assert_missing(self, action, missing, grant_sql, enabled=False):
        self.assertEqual(action['missing_privileges'], missing)
        self.assertEqual(action['grant_sql'], grant_sql)
        self.assertFalse(action['privileges_unreadable'])
        self.assertEqual(action['enabled'], enabled)
        if not enabled:
            self.assertEqual(action['why_not'], 'Needs ' + '; and '.join(' or '.join(r) for r in missing) + '.')
            self.assertEqual(action['fix'], FIX)


class TestClusterPrivileges(_ClusterPrivilegeCase):
    def test_group_replication_udfs_exact_denial_and_least_privilege_grant(self):
        for version in ((8, 0, 13), (8, 0, 17), (8, 4, 11), (9, 7, 0)):
            for aid in ('switchover', 'switch_to_multi_primary', 'switch_to_single_primary'):
                with self.subTest(version=version, action=aid):
                    report = self.report(version)
                    if aid == 'switch_to_single_primary':
                        report['cluster']['topology'] = 'multi_primary'
                    target = next(x for x in report['members'] if x['role'] == 'SECONDARY')
                    action = self.action(report, aid, self.checks([statement(['USAGE'])]), target)
                    self.assert_missing(action, [['GROUP_REPLICATION_ADMIN', 'SUPER']],
                                        'GRANT GROUP_REPLICATION_ADMIN ON *.* TO `monitor`@`%`;')
                    for held in ('GROUP_REPLICATION_ADMIN', 'SUPER'):
                        allowed = self.action(report, aid, self.checks([statement([held])]), target)
                        self.assert_missing(allowed, [], None, enabled=True)

    def test_start_stop_mysql_57_80_84_and_9(self):
        for version in ((5, 7, 44), (8, 0, 0), (8, 0, 12), (8, 0, 13), (8, 4, 11), (9, 7, 0)):
            for aid, state in (('start_member', 'OFFLINE'), ('stop_member', 'ONLINE')):
                with self.subTest(version=version, action=aid):
                    report = self.report(version)
                    me = next(x for x in report['members'] if x['local'])
                    me['state'] = state
                    needs = [['SUPER']] if version < (8, 0, 0) else [['GROUP_REPLICATION_ADMIN', 'SUPER']]
                    action = self.action(report, aid, self.checks([statement(['USAGE'])]))
                    allowed = self.action(report, aid, self.checks([statement(['SUPER'])]))
                    if version < (8, 0, 13):
                        self.assertEqual(action['missing_privileges'], needs)
                        self.assertEqual(action['grant_sql'], statement([needs[0][0]]))
                        self.assertEqual(allowed['missing_privileges'], [])
                        self.assertIsNone(allowed['grant_sql'])
                        for entry in (action, allowed):
                            self.assertFalse(entry['enabled'])
                            self.assertEqual(entry['why_not'], 'Needs MySQL 8.0.13 or later.')
                        with self.assertRaisesRegex(m.ActionUnavailable, 'Needs MySQL 8.0.13 or later'):
                            m.plan_action(report, aid, me['key'], self.checks([statement(['SUPER'])]))
                        continue
                    self.assert_missing(action, needs, statement([needs[0][0]]))
                    self.assert_missing(allowed, [], None, enabled=True)
                    plan = m.plan_action(report, aid, me['key'], self.checks([statement(['SUPER'])]))
                    self.assertEqual(plan['statements'], [m.CLUSTER_ACTION_TAG +
                                                         ('START' if aid == 'start_member' else 'STOP') +
                                                         ' GROUP_REPLICATION;'])
        report = self.report((5, 7, 44))
        unavailable = self.action(report, 'switch_to_multi_primary', self.checks([statement(['SUPER'])]))
        self.assertFalse(unavailable['enabled'])
        self.assertEqual(unavailable['why_not'], 'Needs MySQL 8.0.13 or later.')
        self.assertEqual(unavailable['missing_privileges'], [])

    def test_mariadb_1011_114_every_galera_action(self):
        for version in ((10, 11, 16), (11, 4, 13)):
            for scenario, aid in (('galera-11.8-healthy', 'desync_node'), ('galera-11.8-donor', 'resync_node'),
                                  ('galera-11.8-non-primary', 'bootstrap_pc')):
                with self.subTest(version=version, action=aid):
                    report = self.report(version, scenario)
                    self.assert_missing(self.action(report, aid, self.checks([statement(['USAGE'])])),
                                        [['SUPER']], 'GRANT SUPER ON *.* TO `monitor`@`%`;')
                    self.assert_missing(self.action(report, aid, self.checks([statement(['SUPER'])])),
                                        [], None, enabled=True)
                    for unrelated in ('CONNECTION ADMIN', 'BINLOG ADMIN', 'READ_ONLY ADMIN'):
                        self.assert_missing(self.action(report, aid, self.checks([statement([unrelated])])),
                                            [['SUPER']], statement(['SUPER']))

    def test_shell_each_missing_global_and_schema_requirement_by_version(self):
        for version in ((8, 0, 13), (8, 0, 17), (8, 0, 18), (8, 4, 11), (9, 7, 0)):
            report = self.report(version, 'ic-8.4-healthy')
            needs = [(None, p) for p in shell_globals(version)]
            needs += [(s, p) for s, names in SCHEMA_PRIVILEGES for p in names]
            self.assert_missing(self.action(report, 'switch_to_multi_primary', self.checks(shell_grants(version))),
                                [], None, enabled=True)
            for database, privilege in needs:
                with self.subTest(version=version, database=database, privilege=privilege):
                    missing, grant = [], []
                    if database is None or privilege in shell_globals(version):
                        missing.append([label(privilege)])
                        grant.append(statement([privilege], grantable=True))
                    if database is not None:
                        missing.append([label(privilege, database)])
                        grant.append(statement([privilege], database, True))
                    action = self.action(report, 'switch_to_multi_primary',
                                         self.checks(shell_grants(version, omit=(database, privilege))))
                    self.assert_missing(action, missing, '\n'.join(grant))

    def test_shell_super_only_full_exact_list_and_grant_text_for_all_topology_actions(self):
        for version in ((8, 0, 13), (8, 4, 11), (9, 7, 0)):
            missing = [[label(p)] for p in shell_globals(version)]
            missing += [[label(p, s)] for s, names in SCHEMA_PRIVILEGES for p in names]
            expected_grant = '\n'.join(shell_grants(version))
            for aid in ('switch_to_multi_primary', 'switch_to_single_primary', 'switchover'):
                with self.subTest(version=version, action=aid):
                    report = self.report(version, 'ic-8.4-healthy' if aid != 'switchover' else
                                         'ic-8.4-clusterset-replica')
                    if aid == 'switch_to_single_primary':
                        report['cluster']['topology'] = 'multi_primary'
                    target = next(x for x in report['members'] if x['role'] == 'SECONDARY')
                    action = self.action(report, aid, self.checks([statement(['SUPER'])]), target)
                    self.assert_missing(action, missing, expected_grant)

    def test_shell_held_privileges_still_need_grant_option(self):
        version = (8, 4, 11)
        report = self.report(version, 'ic-8.4-healthy')
        action = self.action(report, 'switch_to_multi_primary', self.checks(shell_grants(version, grantable=False)))
        self.assertFalse(action['enabled'])
        self.assertEqual(action['grant_sql'], '\n'.join(shell_grants(version)))
        self.assertTrue(all('WITH GRANT OPTION' in need[0] for need in action['missing_privileges']))
        # Dynamic grant options are per privilege; a static GRANT OPTION
        # supplied by another line cannot satisfy GROUP_REPLICATION_ADMIN.
        lines = shell_grants(version, omit=(None, 'GROUP_REPLICATION_ADMIN')) + [
            statement(['GROUP_REPLICATION_ADMIN'])]
        action = self.action(report, 'switch_to_multi_primary', self.checks(lines))
        self.assert_missing(action, [['GROUP_REPLICATION_ADMIN WITH GRANT OPTION']],
                            statement(['GROUP_REPLICATION_ADMIN'], grantable=True))

    def test_shell_rejoin_and_clusterset_have_no_guessed_requirement_list(self):
        cases = [('ic-8.4-member-offline', 'rejoin_instance'),
                 ('ic-8.4-clusterset-primary', 'set_primary_cluster:drcluster'),
                 ('ic-8.4-clusterset-replica', 'force_primary_cluster:mfcluster'),
                 ('ic-8.4-clusterset-invalidated', 'rejoin_cluster:drcluster')]
        for scenario, aid in cases:
            with self.subTest(action=aid):
                report = F.report(scenario)
                target = next((x for x in report['members'] if x['state'] == 'MISSING'), None)
                action = self.action(report, aid, self.checks([statement(['USAGE'])]), target)
                self.assertEqual(action['missing_privileges'], [])
                self.assertIsNone(action['grant_sql'])
                if aid == 'rejoin_instance':
                    self.assertTrue(action['enabled'], action)
                    self.assertEqual(action['why_not'], RUNTIME)
                else:
                    # Historical single-connection fixtures contain metadata
                    # for the peer, but no current-pass route into it.
                    self.assertFalse(action['enabled'], action)
                    self.assertTrue(action['why_not'])
                    self.assertTrue(action['fix'])

    def test_every_new_clusterset_method_defers_privileges_without_guessing_admin_grants(self):
        from test_cluster_actions import clusterset_checks, clusterset_report, clusterset_unreachable
        report = clusterset_report()
        checks = clusterset_checks(report)
        checks['grants'] = self.checks([statement(['USAGE'])])['grants']
        replica = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        replica['status'] = 'NOT_REPLICATING'
        replica['channel']['receiver_state'] = 'OFF'
        for aid in ('rejoin_cluster:oldcluster', 'repair_cluster:drcluster',
                    'remove_cluster:drcluster', 'remove_cluster_keep:drcluster',
                    'set_routing_option', 'set_routing_option:router-host::rw',
                    'create_replica_cluster:spare', 'dissolve_clusterset', 'check_clusterset'):
            with self.subTest(action=aid):
                entry = self.action(report, aid, checks)
                self.assertEqual(entry['missing_privileges'], [])
                self.assertIsNone(entry['grant_sql'])
                self.assertFalse(entry['privileges_unreadable'])
                self.assertEqual(m._privilege_requirements(report, entry), ([], True))
        replica['status'] = 'OK'
        replica['channel']['receiver_state'] = 'ON'
        switchover = self.action(report, 'set_primary_cluster:drcluster', checks)
        self.assertTrue(switchover['enabled'], switchover)
        self.assertEqual(m._privilege_requirements(report, switchover), ([], True))
        clusterset_unreachable(report, checks, 'maincluster')
        failover = self.action(report, 'force_primary_cluster:drcluster', checks)
        self.assertTrue(failover['enabled'], failover)
        self.assertEqual(m._privilege_requirements(report, failover), ([], True))

    def test_clusterset_option_version_failures_preserve_the_reason_and_fix(self):
        from test_cluster_actions import clusterset_checks, clusterset_report
        report = clusterset_report()
        checks = clusterset_checks(report, (8, 4, 11))
        checks['grants'] = self.checks([statement(['USAGE'])])['grants']
        for aid in ('remove_cluster_keep:drcluster', 'dissolve_clusterset'):
            with self.subTest(action=aid):
                entry = self.action(report, aid, checks)
                self.assertFalse(entry['enabled'])
                self.assertIn('9.0.0', entry['why_not'])
                self.assertIn('9.0.0', entry['fix'])
                self.assertEqual(entry['missing_privileges'], [])
                self.assertIsNone(entry['grant_sql'])
                self.assertNotIn('GRANT ', entry['fix'])

    def test_privileges_are_evaluated_even_when_another_check_disables_action(self):
        report = F.report('gr-8.4-two-members')
        target = next(x for x in report['members'] if not x['local'])
        action = self.action(report, 'stop_member', self.checks([statement(['USAGE'])]), target)
        self.assertFalse(action['enabled'])
        self.assertTrue(action['why_not'].startswith('Stop Group Replication runs on the server it stops'))
        self.assertEqual(action['missing_privileges'], [['GROUP_REPLICATION_ADMIN', 'SUPER']])
        self.assertEqual(action['grant_sql'], statement(['GROUP_REPLICATION_ADMIN']))
        with self.assertRaises(m.ActionUnavailable) as raised:
            m.plan_action(report, 'stop_member', target['key'], self.checks([statement(['USAGE'])]))
        self.assertEqual(str(raised.exception), action['why_not'] + ' ' + action['fix'] + ' ' + action['grant_sql'])

    def test_roles_use_the_existing_effective_grants_model(self):
        for scenario, lines in (
                ('gr-8.4-two-members', ['GRANT `r_cluster`@`%` TO `monitor`@`%`',
                                      'GRANT GROUP_REPLICATION_ADMIN ON *.* TO `monitor`@`%`']),
                ('galera-11.8-healthy', ['GRANT r_cluster TO `monitor`@`%`',
                                       'GRANT SUPER ON *.* TO r_cluster'])):
            report = F.report(scenario)
            aid = 'desync_node' if report['engine'] == 'mariadb' else 'switch_to_multi_primary'
            self.assert_missing(self.action(report, aid, self.checks(lines)), [], None, enabled=True)

    def test_partial_revoke_blocks_shell_schema_requirement(self):
        version = (8, 4, 11)
        report = self.report(version, 'ic-8.4-healthy')
        lines = shell_grants(version, omit=(METADATA[0], 'INSERT'), partial_revokes=True) + [
            statement(['INSERT'], grantable=True)]
        lines.append('REVOKE INSERT ON `mysql_innodb_cluster_metadata`.* FROM `monitor`@`%`')
        model = privileges.parse_grants(lines, 'mysql', '8.4.11', partial_revokes=True)
        model['account'] = ACCOUNT
        action = self.action(report, 'switch_to_multi_primary', {'shell': SHELL, 'grants': model})
        self.assert_missing(action, [['INSERT ON `mysql_innodb_cluster_metadata`.* WITH GRANT OPTION']],
                            statement(['INSERT'], METADATA[0], True, partial_revokes=True))

    def test_schema_grant_uses_the_partial_revokes_read_with_its_account(self):
        version = (8, 4, 11)
        for enabled in (False, True):
            with self.subTest(partial_revokes=enabled):
                conn = GrantsClient('ic-8.4-healthy',
                                    grants=shell_grants(version, omit=(METADATA[0], 'INSERT'),
                                                        partial_revokes=enabled),
                                    variables={'partial_revokes': 'OFF' if enabled else 'ON'})
                report = m.read_status(conn)
                # Deliberately disagree with the earlier monitoring login.
                conn.variables['partial_revokes'] = 'ON' if enabled else 'OFF'
                with mock.patch.object(cluster_shell, 'find_shell', return_value=SHELL):
                    checks = m.action_checks(conn, report, 'switch_to_multi_primary')
                action = self.action(report, 'switch_to_multi_primary', checks)
                expected = statement(['INSERT'], METADATA[0], True, partial_revokes=enabled)
                self.assert_missing(action, [['INSERT ON `mysql_innodb_cluster_metadata`.* WITH GRANT OPTION']],
                                    expected)
                # A copy of the suggestion grants only the named schema.
                model = privileges.parse_grants([expected], 'mysql', '8.4.11', partial_revokes=enabled)
                self.assertIs(privileges.has(model, 'INSERT', 'database', database=METADATA[0], grantable=True), True)
                self.assertIsNot(privileges.has(model, 'INSERT', 'database',
                                               database='mysqlXinnodbYclusterZmetadata', grantable=True), True)
                self.assertEqual(sum('SHOW GRANTS' in sql for sql in conn.reads), 1)

    def test_schema_grant_escapes_percent_and_underscore_only_when_needed(self):
        missing = [(('INSERT',), 'data_%', False)]
        self.assertEqual(m._grant_sql(missing, ACCOUNT, False),
                         'GRANT INSERT ON `data\\_\\%`.* TO `monitor`@`%`;')
        self.assertEqual(m._grant_sql(missing, ACCOUNT, True),
                         'GRANT INSERT ON `data_%`.* TO `monitor`@`%`;')
        self.assertIsNone(m._grant_sql(missing, ACCOUNT, None))

    def test_schema_grant_refuses_backslash_in_database(self):
        need = [(('INSERT',), r'data\_%', False)]
        for partial in (False, True, None):
            with self.subTest(partial=partial):
                self.assertIsNone(m._grant_sql(need, ACCOUNT, partial))

    def test_unknown_partial_revokes_never_produces_an_ambiguous_grant(self):
        conn = GrantsClient('ic-8.4-healthy', grants=[statement(['SUPER'])], variables={'partial_revokes': None})
        report = m.read_status(conn)
        with mock.patch.object(cluster_shell, 'find_shell', return_value=SHELL):
            checks = m.action_checks(conn, report, 'switch_to_multi_primary')
        action = self.action(report, 'switch_to_multi_primary', checks)
        self.assertFalse(action['enabled'])
        self.assertTrue(action['missing_privileges'])
        self.assertIsNone(action['grant_sql'])
        self.assertIn('GRANT could not be built because partial_revokes could not be read', action['fix'])
        report['cluster']['quorum'] = False
        action = self.action(report, 'switch_to_multi_primary', checks)
        self.assertIsNone(action['grant_sql'])
        self.assertIn('GRANT could not be built because partial_revokes could not be read', action['why_not'])
        self.assertIn('quorum', action['fix'])

    def test_schema_grants_before_partial_revokes_use_wildcard_escaping(self):
        missing = [(('INSERT',), 'data_%', False)]
        for engine, version in [('mysql', '5.7.44'), ('mysql', '8.0.0'), ('mysql', '8.0.15'),
                                ('percona', '5.7.44-48'), ('mysql', '8.0.15-Percona'),
                                ('mariadb', '10.11.16-MariaDB'), ('mariadb', '11.4.13-MariaDB')]:
            with self.subTest(engine=engine, version=version):
                report = {'engine': engine, 'server_version': version}
                model = m._grant_model(report, {'checked': True, 'account': ACCOUNT,
                                               'grants': [statement(['USAGE'])], 'partial_revokes': None})
                self.assertIsNotNone(model)
                self.assertIs(model['grant_partial_revokes'], False)
                suggestion = m._grant_sql(missing, model['account'], model['grant_partial_revokes'])
                self.assertEqual(suggestion, 'GRANT INSERT ON `data\\_\\%`.* TO `monitor`@`%`;')
                parsed = privileges.parse_grants([suggestion], engine, version)
                self.assertIs(privileges.has(parsed, 'INSERT', 'database', database='data_%'), True)
                self.assertIsNot(privileges.has(parsed, 'INSERT', 'database', database='dataXX'), True)

    def test_schema_grant_setting_stays_unknown_on_failed_or_supported_version_reads(self):
        for version in ('8.0.16', '8.0.36', '8.4.11', '', '8.0'):
            with self.subTest(version=version):
                report = {'engine': 'mysql', 'server_version': version}
                grants = {'checked': True, 'account': ACCOUNT, 'grants': [statement(['USAGE'])],
                          'partial_revokes': None}
                model = m._grant_model(report, grants)
                self.assertIsNone(model['grant_partial_revokes'])
                self.assertIsNone(m._grant_sql([(('INSERT',), 'data_%', False)], ACCOUNT,
                                               model['grant_partial_revokes']))
        for engine, version in [('mysql', '5.7.44'), ('mysql', '8.0.15'), ('mariadb', '11.4.13')]:
            self.assertIsNone(m._grant_model({'engine': engine, 'server_version': version},
                                             {'checked': False, 'error': 'read failed'}))

    def test_current_user_quoting_never_changes_the_account(self):
        cases = [("o'reilly@%", "`o'reilly`@`%`"),
                 ('tick`user@::1', '`tick``user`@`::1`'), ('user@2001:db8::42', '`user`@`2001:db8::42`'),
                 ('name@inside@%', '`name@inside`@`%`'), ('x`; GRANT SUPER@%', '`x``; GRANT SUPER`@`%`')]
        report = F.report('gr-8.4-two-members')
        for account, quoted in cases:
            with self.subTest(account=account):
                action = self.action(report, 'switch_to_multi_primary', self.checks([statement(['USAGE'])], account))
                self.assertEqual(action['grant_sql'], 'GRANT GROUP_REPLICATION_ADMIN ON *.* TO %s;' % quoted)

    def test_hostile_current_user_and_database_return_none_without_raising(self):
        hostile_accounts = [
            'dba\x01\\! id #@%', '\x15', '\x15@%', '\x1b[201~', '\x1b[201~@%',
            '\x7f', '\x7f@%', '\x85', '\x85@%', '\u2028', '\u2028@%', '\u2029@%',
            '\\', 'back\\slash@host%',
        ]
        for account in hostile_accounts:
            with self.subTest(direct_account=account):
                self.assertIsNone(m._grant_sql([(('GROUP_REPLICATION_ADMIN',), None, False)], account, False))
        for partial in (False, True, None):
            with self.subTest(direct_db_partial=partial):
                self.assertIsNone(m._grant_sql([(('INSERT',), 'db\x15x', False)], ACCOUNT, partial))
        for scenario, aid, expected_need in (
                ('gr-8.4-two-members', 'switch_to_multi_primary', 'Needs GROUP_REPLICATION_ADMIN or SUPER.'),
                ('galera-11.8-healthy', 'desync_node', 'Needs SUPER.')):
            report = F.report(scenario)
            for account in ('dba\x01\\! id #@%', 'dba\x15@%', 'dba\x1b[201~@%', 'dba\x7f@%',
                            'dba\x85@%', 'dba\u2028@%', 'back\\slash@host%'):
                with self.subTest(scenario=scenario, account=account):
                    action = self.action(report, aid, self.checks([statement(['USAGE'])], account))
                    self.assertFalse(action['enabled'])
                    self.assertFalse(action['privileges_unreadable'])
                    self.assertIsNone(action['grant_sql'])
                    self.assertEqual(action['why_not'], expected_need)
                    self.assertNotIn('partial_revokes', action['fix'])
        # S-H1: even if _grant_sql raises ValueError, Cluster action build catches it and keeps the reason.
        report = F.report('gr-8.4-two-members')
        with mock.patch.object(m, '_grant_sql', side_effect=ValueError('Names cannot contain NUL or line breaks.')):
            action = self.action(report, 'switch_to_multi_primary', self.checks([statement(['USAGE'])]))
        self.assertFalse(action['enabled'])
        self.assertIsNone(action['grant_sql'])
        self.assertEqual(action['why_not'], 'Needs GROUP_REPLICATION_ADMIN or SUPER.')
        self.assertNotIn('partial_revokes', action['fix'])

    def test_non_ascii_current_user_and_database_fall_back_to_grant_unavailable(self):
        payloads = [
            '\u00c8' * 6 + 'DROP DATABASE prod;#',
            '\u0e08DROP DATABASE prod;#',
            'sales' + '\u0308' * 4 + 'DROP DATABASE prod;#',
            'caf\u00e9',
            'a\u00a0b',
            'a\u00adb',
            'a\u200bb',
            'a\u202eb',
        ]
        for payload in payloads:
            for partial in (False, True, None):
                for account in (payload + '@%', 'monitor@' + payload):
                    with self.subTest(direct_account=account, partial=partial):
                        self.assertIsNone(m._grant_sql([(('GROUP_REPLICATION_ADMIN',), None, False)], account, partial))
                for db in (payload, payload + '_%'):
                    with self.subTest(direct_db=db, partial=partial):
                        self.assertIsNone(m._grant_sql([(('INSERT',), db, False)], ACCOUNT, partial))
        for scenario, aid, expected_need in (
                ('gr-8.4-two-members', 'switch_to_multi_primary', 'Needs GROUP_REPLICATION_ADMIN or SUPER.'),
                ('galera-11.8-healthy', 'desync_node', 'Needs SUPER.')):
            report = F.report(scenario)
            for payload in payloads:
                for account in (payload + '@%', 'monitor@' + payload):
                    with self.subTest(scenario=scenario, account=account):
                        action = self.action(report, aid, self.checks([statement(['USAGE'])], account))
                        self.assertFalse(action['enabled'])
                        self.assertFalse(action['privileges_unreadable'])
                        self.assertIsNone(action['grant_sql'])
                        self.assertEqual(action['why_not'], expected_need)
                        self.assertEqual(action['fix'], m._GRANT_UNAVAILABLE)
        # Even when partial_revokes is None and a schema requirement has '_', an unsafe account still gets _GRANT_UNAVAILABLE.
        ic_report = self.report((8, 4, 11), 'ic-8.4-healthy')
        for payload in payloads:
            for account in (payload + '@%', 'monitor@' + payload):
                with self.subTest(scenario='ic-8.4-healthy', account=account):
                    checks = {'shell': SHELL, 'grants': {'checked': True, 'account': account,
                                                         'grants': [statement(['SUPER'])], 'partial_revokes': None}}
                    action = self.action(ic_report, 'switch_to_multi_primary', checks)
                    self.assertFalse(action['enabled'])
                    self.assertFalse(action['privileges_unreadable'])
                    self.assertIsNone(action['grant_sql'])
                    self.assertEqual(action['fix'], m._GRANT_UNAVAILABLE)

    def test_unreadable_empty_unparsed_or_unknown_account_is_not_a_denial(self):
        report = F.report('gr-8.4-two-members')
        for raw in ({'checked': False}, {'checked': True, 'account': ACCOUNT, 'grants': []},
                    {'checked': True, 'account': ACCOUNT, 'grants': ['not a grant']},
                    {'checked': True, 'grants': [statement(['USAGE'])]},
                    {'checked': True, 'account': 'bad\nuser@%', 'grants': [statement(['USAGE'])]}):
            for blocked in (False, True):
                with self.subTest(grants=raw, blocked=blocked):
                    report['cluster']['quorum'] = not blocked
                    action = self.action(report, 'switch_to_multi_primary', {'grants': raw})
                    self.assertEqual(action['enabled'], not blocked)
                    self.assertTrue(action['privileges_unreadable'])
                    self.assertEqual(action['missing_privileges'], [])
                    self.assertIsNone(action['grant_sql'])
                    self.assertIn(UNREADABLE, action['why_not'])

    def test_workspace_catalog_reads_grants_once_and_never_serializes_them(self):
        conn = GrantsClient()
        report = m.read_status(conn)
        self.assertFalse(any('SHOW GRANTS' in sql for sql in conn.reads), 'status alone remains read-only monitoring')
        catalog = m.with_actions(report)
        action = next(a for a in catalog['actions'] if a['id'] == 'switch_to_multi_primary')
        self.assert_missing(action, [['GROUP_REPLICATION_ADMIN', 'SUPER']], statement(['GROUP_REPLICATION_ADMIN']))
        self.assertEqual(sum('SHOW GRANTS' in sql for sql in conn.reads), 1)
        public = json.loads(json.dumps(catalog))
        self.assertNotIn('_action_conn', public)
        self.assertNotIn('login@client', json.dumps(public))
        self.assertEqual(conn.writes, [])

    def test_failed_grants_read_on_connection_keeps_action_available(self):
        conn = GrantsClient(denied=True)
        report = m.with_actions(m.read_status(conn))
        action = next(a for a in report['actions'] if a['id'] == 'switch_to_multi_primary')
        self.assertTrue(action['enabled'])
        self.assertTrue(action['privileges_unreadable'])
        self.assertEqual(action['why_not'], UNREADABLE)
        plan = m.plan_action(report, 'switch_to_multi_primary')
        self.assertIn(UNREADABLE, plan['warnings'])
        self.assertFalse(any('GRANT ' in sql for sql in plan['statements']))

    def test_pxc_still_has_no_actions(self):
        report = F.report('pxc-8.4-healthy')
        for target in report['members']:
            self.assertEqual(m.available_actions(report, target['key'], self.checks([statement(['USAGE'])])), [])
        self.assertEqual(m.overview_actions(report, self.checks([statement(['USAGE'])])), [])


class TestGaleraReviewedIdentity(_ClusterPrivilegeCase):
    def client(self, scenario='galera-11.8-healthy'):
        return GrantsClient(scenario, grants=[statement(['SUPER'])], variables={
            'server_id': '231', 'hostname': 'reviewed-host', 'port': '4306', 'wsrep_node_name': 'reviewed-node'})

    def plan(self, conn, aid='desync_node'):
        report = m.read_status(conn)
        me = next(x for x in report['members'] if x['local'])
        return m.plan_action(report, aid, me['key'], m.action_checks(conn, report, aid))

    def test_desync_plan_and_send_carry_all_four_read_values(self):
        conn = self.client()
        plan = self.plan(conn)
        self.assertEqual({k: plan['expect']['server'][k] for k in
                          ('server_id', 'hostname', 'port', 'wsrep_node_name')},
                         {'server_id': 231, 'hostname': 'reviewed-host', 'port': 4306,
                          'wsrep_node_name': 'reviewed-node'})
        reviewed_writes.review_details(plan)
        guard = ("SET @myflames_target = (SELECT 1 FROM (SELECT 1 UNION ALL SELECT 1) AS g WHERE "
                 "@@global.server_id <> 231 OR @@global.hostname <> 'reviewed-host' OR @@global.port <> 4306 "
                 "OR @@global.wsrep_node_name <> 'reviewed-node' OR NOT ((SELECT VARIABLE_VALUE FROM "
                 "information_schema.GLOBAL_STATUS WHERE VARIABLE_NAME = 'wsrep_gcomm_uuid') <=> '%s'));"
                 % plan['expect']['server']['wsrep_gcomm_uuid'])
        self.assertEqual(plan['session_guard'], [guard])
        result = m.execute_action(conn, plan)
        self.assertEqual(result['status'], 'done', result)
        self.assertEqual([(g, s) for g, s, _ in guarded_packets(conn.batches[0])],
                         [(guard, dbops.split_statements('/*myflames:cluster_op*/ SET GLOBAL wsrep_desync = ON;')[0]['text'])])
        self.assertEqual(conn.writes, [dbops.split_statements(s)[0]['text'] for s in plan['statements']])

    def test_same_session_mismatch_refuses_before_any_action_is_executed(self):
        conn = self.client()
        plan = self.plan(conn)
        conn.guard_mismatch = True
        result = m.execute_action(conn, plan)
        self.assertEqual((result['status'], result['outcome'], result['states']),
                         ('refused', 'identity_mismatch', ['not_sent']))
        self.assertEqual(result['message'], dbops.TARGET_IDENTITY_MISMATCH)
        self.assertEqual(conn.writes, [])
        self.assertEqual(len(conn.batches), 1)

    def test_two_nodes_with_identical_server_fields_are_distinguished_by_gcomm_uuid(self):
        import re
        conn = self.client()
        plan = self.plan(conn)
        own_uuid = plan['expect']['server']['wsrep_gcomm_uuid']
        other_uuid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
        self.assertNotEqual(own_uuid, other_uuid)

        def swapped_write(sql, **kwargs):
            conn.batches.append(sql)
            packets = guarded_packets(sql)
            match = re.search(r"VARIABLE_NAME = 'wsrep_gcomm_uuid'\) <=> '([^']+)'", packets[0][0])
            self.assertIsNotNone(match)
            if match.group(1) != other_uuid:
                raise ConnectorError('ERROR 1242 (21000) at line 2: Subquery returns more than 1 row')
            conn.writes.extend(s for _, s, _ in packets)
            return ''

        conn.run_write = swapped_write
        result = m.execute_action(conn, plan)
        self.assertEqual((result['status'], result['outcome'], result['sent']), ('refused', 'identity_mismatch', []))
        self.assertEqual(conn.writes, [])
        self.assertIn(own_uuid, guarded_packets(conn.batches[0])[0][0])

    def test_changed_identity_in_fresh_read_sends_no_batch(self):
        for key, value in (('server_id', '232'), ('hostname', 'another-host'), ('port', '4307'),
                           ('wsrep_node_name', 'another-node')):
            with self.subTest(field=key):
                conn = self.client()
                plan = self.plan(conn)
                conn.variables[key] = value
                result = m.execute_action(conn, plan)
                self.assertEqual((result['status'], result['outcome']), ('refused', 'changed'))
                self.assertEqual(conn.batches, [])

    def test_desync_resync_bootstrap_have_complete_reviewed_identity(self):
        for scenario, aid in (('galera-11.8-healthy', 'desync_node'), ('galera-11.8-donor', 'resync_node'),
                              ('galera-11.8-non-primary', 'bootstrap_pc')):
            with self.subTest(action=aid):
                conn = self.client(scenario)
                plan = self.plan(conn, aid)
                self.assertEqual(len(reviewed_writes.session_guard(plan)), 1)
                self.assertEqual(m.execute_action(conn, plan, typed=plan['typed_guard'])['status'], 'done')
                self.assertEqual(conn.writes, [dbops.split_statements(s)[0]['text'] for s in plan['statements']])

    def test_missing_identity_still_fails_closed(self):
        conn = self.client()
        conn.variables['server_id'] = None
        plan = self.plan(conn)
        with self.assertRaisesRegex(ValueError, 'server_id is missing or invalid'):
            reviewed_writes.review_details(plan)
        self.assertEqual(conn.batches, [])


if __name__ == '__main__':
    unittest.main()
