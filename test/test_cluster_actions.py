"""Reviewed Cluster changes (myflames/cluster.py "reviewed actions",
myflames/reviewed_writes.py, /api/live/cluster/plan and
/api/live/cluster/action), over the same live recordings and scenarios as
test_cluster.py (test/_cluster_fixtures.py). Writes go to a recorder; every
test runs with a temporary HOME and the vault backend.
"""
import copy
from decimal import Decimal
import json
import os
import tempfile
import unittest
from unittest import mock

import _cluster_fixtures as F
from myflames import cluster as m, clusterset_execution, dbops, ui
from myflames.connector import ConnectorError
from test_cluster import install_clusterset_batch_replay

install_clusterset_batch_replay()

UUID = '8f1a3b2c-0d4e-11ef-9a3b-0242ac120002'
MAX_SAFE_JS_INT = 2 ** 53 - 1


def js_json_roundtrip(value):
    """Round-trip *value* through JSON the way JavaScript does: integers above
    2^53-1 are rounded to the nearest IEEE 754 binary64 double."""
    def parse_js_int(text):
        number = int(text)
        if -MAX_SAFE_JS_INT <= number <= MAX_SAFE_JS_INT:
            return number
        return int(Decimal(repr(float(number))))
    return json.loads(json.dumps(value), parse_int=parse_js_int)


def member(report, name):
    return next(x for x in report['members'] if x['name'] == name)


def local(report):
    return next(x for x in report['members'] if x['local'])


def offered(report, name):
    target = member(report, name)
    checks = {'bootstrap_group': 'OFF'}  # Existing recordings predate this preflight variable.
    actions = m.available_actions(report, target['key'], checks)
    if target['local']:
        actions += m.overview_actions(report, checks)
    return [a['id'] for a in actions if a['enabled']]


class Writes(object):
    """A fixture connection that records writes (one run_write per send) and
    answers group_replication_bootstrap_group."""

    def __init__(self, conn, bootstrap='OFF', fail=None):
        self.conn, self.bootstrap, self.fail = conn, bootstrap, fail
        self.writes = []

    def run(self, sql, extra_flags=None, timeout=None):
        if 'SELECT @@global.group_replication_bootstrap_group' in sql:
            if self.bootstrap is None:
                raise ConnectorError('mysql client exited 1: ERROR 1193 (HY000) at line 1: Unknown system variable')
            return '%s\n' % self.bootstrap
        return self.conn.run(sql, extra_flags)

    def run_write(self, sql, extra_flags=None, timeout=None):
        self.writes.append(sql)
        if self.fail is not None:
            from _reviewed_replay import acknowledged_error
            raise self.fail if dbops.error_code(str(self.fail)) == 1242 else acknowledged_error(self.fail, sql)
        return ''

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {'HOME': self.tmp.name, 'USERPROFILE': self.tmp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


def clusterset_report():
    """Explicit current-pass route evidence; the archived fixtures stay unchanged.

    The route, primary and channel facts here model the SQL read contract.
    Reachability of an absent primary is separate evidence supplied by a
    preflight, never inferred from a failed read or a metadata-only card.
    """
    report = F.report('ic-8.4-clusterset-three')
    report['metadata']['schema_version'] = '2.4.0'
    cs = report['clusterset']
    replica = next(c for c in cs['clusters'] if c['name'] == 'drcluster')
    invalid = copy.deepcopy(replica)
    invalid.update(name='oldcluster', role=None, invalidated=True, local=False)
    for index, instance in enumerate(invalid['members']):
        instance['server_uuid'] = 'aaaaaaaa-bbbb-cccc-dddd-%012d' % (index + 1)
        instance['address'] = 'old-%d:3306' % (index + 1)
    invalid['instances'] = copy.deepcopy(invalid['members'])
    cs['clusters'].append(invalid)
    report._clusterset_groups['oldcluster'] = 'aaaaaaaa-bbbb-cccc-dddd-999999999999'
    for index, cluster in enumerate(cs['clusters']):
        for position, instance in enumerate(cluster['members']):
            instance.update(state='ONLINE', role='PRIMARY' if position == 0 else 'SECONDARY')
        cluster['primary'] = copy.deepcopy(cluster['members'][0])
        cluster.update(read='this_connection' if cluster['local'] else 'route', read_error=None,
                       quorum=True, fenced_writes=cluster['role'] != 'PRIMARY',
                       status='INVALIDATED' if cluster['invalidated'] else 'OK',
                       channel={'present': True, 'receiver_state': 'ON', 'applier_state': 'ON',
                                'last_error_number': 0, 'read_error': None, 'source': 'mf-main-1:3306'},
                       route={'profile_id': 'cs-%d' % index, 'profile_name': cluster['name'],
                              'self': cluster['local'],
                              'server_uuid': cluster['primary']['server_uuid']})
    cs.update(name='production-set', view_id='4294967301', status='AVAILABLE', from_local_copy=False,
              metadata_read_on=copy.deepcopy(cs['clusters'][0]['route']),
              routers=[{'id': 1, 'name': 'router-host::rw', 'address': 'router-host', 'version': '9.7.0',
                        'cluster_name': None, 'options': {}, 'stored_configuration': True}],
              routing_options={'stored_configuration': True, 'options': {}})
    return report


def clusterset_checks(report, version=(9, 7, 0)):
    """Store-owned active-group inputs and a standalone preflight from this pass."""
    profiles = [{'id': c['route']['profile_id'], 'name': c['name'], 'host': 'cs-%d' % index,
                 'port': 3306, 'user': 'admin', 'remember_password': True, 'group': 'Production',
                 'topology_identity': {'server_uuid': c['route']['server_uuid']}}
                for index, c in enumerate(report['clusterset']['clusters']) if c.get('route')]
    profiles.append({'id': 'spare', 'name': 'Spare instance', 'host': 'saved-endpoint', 'port': 3307,
                     'user': 'admin', 'remember_password': True, 'group': 'Production'})
    uuid = 'bbbbbbbb-cccc-dddd-eeee-000000000001'
    profiles[-1]['topology_identity'] = {'server_uuid': uuid}
    return {'shell': {'path': '/opt/mysqlsh/bin/mysqlsh', 'version': '.'.join(str(n) for n in version),
                      'version_tuple': list(version)}, 'grants': None, 'group_name': 'Production',
            'group_profiles': profiles,
            'current_profile': next(p for p in profiles if p['id'] == next(
                c['route']['profile_id'] for c in report['clusterset']['clusters'] if c['local'])),
            'replica_candidates': [{'profile_id': 'spare', 'read': 'route', 'server_uuid': uuid,
                                    'route': {'profile_id': 'spare', 'profile_name': 'Spare instance',
                                              'server_uuid': uuid, 'self': False},
                                    'address': 'saved-endpoint:3307', 'reported_address': 'saved-endpoint:3307',
                                    **clusterset_execution.candidate_binding(profiles[-1]),
                                    'server_version': '8.4.11',
                                    'engine': 'mysql', 'read_error': None,
                                    'kind': 'none', 'group_name': '', 'gtid_subset': True,
                                    'super_read_only': True}]}


def clusterset_unreachable(report, checks, name):
    """Every metadata member was probed; a failed SQL read alone never suffices."""
    target = next(c for c in report['clusterset']['clusters'] if c['name'] == name)
    checks.setdefault('clusterset_reachability', {})[name] = [
        {'server_uuid': member['server_uuid'], 'reachable': False} for member in target['members']
        if member.get('instance_type') != 'read-replica']
    target.update(route=None, read='failed', read_error='Every saved member connection was unreachable.',
                  primary=None, quorum=None, status='NOT_READ')
    if target['role'] == 'PRIMARY':
        cs = report['clusterset']
        cs.update(metadata_read_on=copy.deepcopy(next(c['route'] for c in cs['clusters'] if c['local'])),
                  from_local_copy=True, status='UNKNOWN')
    return target


class TestClusterSetCatalog(Home):
    def catalog(self, report, checks):
        return {a['id']: a for a in m.overview_actions(report, checks) if a['scope'] == 'clusterset'}

    def assert_disabled(self, entry):
        self.assertFalse(entry['enabled'], entry)
        self.assertIsInstance(entry['why_not'], str)
        self.assertTrue(entry['why_not'].strip(), entry)
        self.assertIsInstance(entry['fix'], str)
        self.assertTrue(entry['fix'].strip(), entry)

    def test_sro_without_fencing_disables_mutations_consistently_with_status(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        primary = next(c for c in report['clusterset']['clusters'] if c['role'] == 'PRIMARY')
        primary.update(super_read_only=True, fenced_writes=False, disable_super_read_only_if_primary=True)
        m._judge_clusterset(report['clusterset'])
        self.assertEqual(primary['status'], 'NOT_OK')
        self.assertEqual(report['clusterset']['status'], 'UNAVAILABLE')
        actions = self.catalog(report, checks)
        for aid in ('set_primary_cluster:drcluster', 'rejoin_cluster:oldcluster',
                    'repair_cluster:drcluster', 'remove_cluster:drcluster',
                    'remove_cluster_keep:drcluster', 'set_routing_option',
                    'set_routing_option:router-host::rw', 'create_replica_cluster:spare'):
            with self.subTest(action=aid):
                self.assert_disabled(actions[aid])
                self.assertIn('super_read_only ON', actions[aid]['why_not'])
                with self.assertRaisesRegex(m.ActionUnavailable, 'super_read_only ON'):
                    m.plan_action(report, aid, checks=checks)
        # Diagnostic reads and deliberate teardown keep their documented exceptions.
        self.assertTrue(actions['check_clusterset']['enabled'], actions['check_clusterset'])
        self.assertTrue(actions['dissolve_clusterset']['enabled'], actions['dissolve_clusterset'])

    def test_set_families_are_always_listed_even_without_a_clusterset(self):
        families = {'set_primary_cluster', 'force_primary_cluster', 'rejoin_cluster', 'repair_cluster',
                    'remove_cluster', 'remove_cluster_keep', 'set_routing_option',
                    'create_replica_cluster', 'dissolve_clusterset', 'check_clusterset'}
        report = F.report('ic-8.4-healthy')
        actions = self.catalog(report, {'shell': clusterset_checks(clusterset_report())['shell']})
        self.assertEqual({aid.split(':', 1)[0] for aid in actions}, families)
        for entry in actions.values():
            self.assert_disabled(entry)

    def test_every_target_has_one_entry_and_each_family_has_a_usable_state(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        actions = self.catalog(report, checks)
        for aid in ('set_primary_cluster:drcluster', 'rejoin_cluster:oldcluster',
                    'remove_cluster:drcluster', 'remove_cluster_keep:drcluster',
                    'set_routing_option', 'set_routing_option:router-host::rw',
                    'create_replica_cluster:spare', 'dissolve_clusterset', 'check_clusterset'):
            self.assertTrue(actions[aid]['enabled'], actions[aid])
            self.assertIsNone(actions[aid]['fix'])
        self.assert_disabled(actions['force_primary_cluster:drcluster'])
        # A healthy channel does not need repair; a stopped channel does.
        replica = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        replica.update(status='NOT_REPLICATING')
        replica['channel']['receiver_state'] = 'OFF'
        actions = self.catalog(report, checks)
        self.assertTrue(actions['repair_cluster:drcluster']['enabled'])
        self.assert_disabled(actions['set_primary_cluster:drcluster'])
        clusterset_unreachable(report, checks, 'maincluster')
        self.assertTrue(self.catalog(report, checks)['force_primary_cluster:drcluster']['enabled'])

    def test_unprobed_metadata_members_are_not_replica_creation_targets(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        local_id = report._clusterset_ids[report['clusterset']['local_cluster']]
        local_secondary = report._clusterset_instances[local_id][1]
        host, port = m._split_address(local_secondary['endpoint'])
        # Shared metadata may include instances from a cluster outside this set.
        report._clusterset_instances['outside-set'] = [{
            'server_uuid': 'cccccccc-dddd-eeee-ffff-000000000001',
            'address': 'outside-db:3309', 'endpoint': 'outside-db:3309'}]
        checks['group_profiles'].extend([
            {'id': 'unprobed-local', 'host': host.upper(), 'port': port, 'group': 'Production'},
            {'id': 'unprobed-outside', 'host': 'outside-db', 'port': 3309, 'group': 'Production'},
            {'id': 'different-port', 'host': host, 'port': port + 1, 'group': 'Production'},
        ])
        actions = self.catalog(report, checks)
        self.assertNotIn('create_replica_cluster:unprobed-local', actions)
        self.assertNotIn('create_replica_cluster:unprobed-outside', actions)
        self.assertIn('create_replica_cluster:different-port', actions)
        self.assertIn('create_replica_cluster:spare', actions)

    def test_switchover_rejoin_and_repair_require_their_target_read(self):
        for aid, target_name in (('set_primary_cluster:drcluster', 'drcluster'),
                                 ('rejoin_cluster:oldcluster', 'oldcluster'),
                                 ('repair_cluster:drcluster', 'drcluster')):
            for read in ('metadata_only', 'failed'):
                with self.subTest(action=aid, read=read):
                    report = clusterset_report()
                    checks = clusterset_checks(report)
                    target = next(c for c in report['clusterset']['clusters'] if c['name'] == target_name)
                    target.update(read=read, route=None, status='NOT_READ')
                    self.assert_disabled(self.catalog(report, checks)[aid])

    def test_failover_targets_are_not_limited_to_the_connected_replica(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        clusterset_unreachable(report, checks, 'maincluster')
        action = self.catalog(report, checks)['force_primary_cluster:drcluster']
        self.assertTrue(action['enabled'], action)
        self.assertEqual(action['cluster_name'], 'drcluster')
        self.assertFalse(next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')['local'])

    def test_failover_does_not_guess_reachability_from_read_errors_or_partial_probes(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        clusterset_unreachable(report, checks, 'maincluster')
        evidence = checks.pop('clusterset_reachability')
        self.assert_disabled(self.catalog(report, checks)['force_primary_cluster:drcluster'])
        for probes in (evidence['maincluster'][:-1],
                       [dict(p, reachable=True) if i == 0 else p for i, p in enumerate(evidence['maincluster'])],
                       [dict(p, reachable=None) if i == 0 else p for i, p in enumerate(evidence['maincluster'])]):
            checks['clusterset_reachability'] = {'maincluster': probes}
            self.assert_disabled(self.catalog(report, checks)['force_primary_cluster:drcluster'])

    def test_failover_refuses_when_the_active_server_contradicts_unreachable_evidence(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        primary = clusterset_unreachable(report, checks, 'maincluster')
        self.assertTrue(self.catalog(report, checks)['force_primary_cluster:drcluster']['enabled'])
        # This read answered from an old-primary member, even though all the
        # supplied connectivity probes claim those same UUIDs unreachable.
        report['server']['server_uuid'] = primary['members'][0]['server_uuid']
        self.assert_disabled(self.catalog(report, checks)['force_primary_cluster:drcluster'])

    def test_invalidated_removal_still_needs_a_healthy_reachable_channel(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        names = ('remove_cluster:oldcluster', 'remove_cluster_keep:oldcluster')
        for aid in names:
            self.assertTrue(self.catalog(report, checks)[aid]['enabled'])
        for problem in ('not_read', 'missing', 'receiver_off', 'applier_off', 'read_error', 'receiver_error'):
            with self.subTest(problem=problem):
                changed = copy.deepcopy(report)
                invalid = next(c for c in changed['clusterset']['clusters'] if c['name'] == 'oldcluster')
                channel = invalid['channel']
                if problem == 'not_read':
                    invalid['channel'] = None
                elif problem == 'missing':
                    channel['present'] = False
                elif problem == 'receiver_off':
                    channel['receiver_state'] = 'OFF'
                elif problem == 'applier_off':
                    channel['applier_state'] = 'OFF'
                elif problem == 'read_error':
                    channel['read_error'] = 'Channel workers could not be read.'
                else:
                    channel['last_error_number'] = 1236
                actions = self.catalog(changed, checks)
                for aid in names:
                    self.assert_disabled(actions[aid])

    def test_a_missing_shell_and_each_credential_floor_boundary_disable_with_a_fix(self):
        report = clusterset_report()
        for version in ((8, 0, 26), (8, 0, 27), (8, 0, 31), (8, 0, 32)):
            checks = clusterset_checks(report, version)
            entry = self.catalog(report, checks)['set_primary_cluster:drcluster']
            self.assertEqual(entry['enabled'], version >= (8, 0, 32), entry)
            if not entry['enabled']:
                self.assert_disabled(entry)
                self.assertIn('8.0.32', entry['why_not'] + entry['fix'])
        checks = clusterset_checks(report)
        checks['shell'] = None
        for entry in self.catalog(report, checks).values():
            self.assert_disabled(entry)

    def test_dissolve_and_keep_running_gate_at_900(self):
        report = clusterset_report()
        for version in ((8, 4, 11), (8, 9, 99), (9, 0, 0), (9, 7, 0)):
            actions = self.catalog(report, clusterset_checks(report, version))
            self.assertTrue(actions['remove_cluster:drcluster']['enabled'])
            for aid in ('remove_cluster_keep:drcluster', 'dissolve_clusterset'):
                self.assertEqual(actions[aid]['enabled'], version >= (9, 0, 0), actions[aid])
                if not actions[aid]['enabled']:
                    self.assert_disabled(actions[aid])
                    self.assertIn('9.0.0', actions[aid]['why_not'] + actions[aid]['fix'])

    def test_router_metadata_failures_disable_routing(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        report['clusterset']['routers'] = None
        report['clusterset']['routing_options']['options'] = None
        self.assert_disabled(self.catalog(report, checks)['set_routing_option'])

    def test_removal_requires_readable_global_and_per_router_targets(self):
        for failure in ('routers', 'defaults', 'override'):
            with self.subTest(failure=failure):
                report = clusterset_report()
                checks = clusterset_checks(report)
                cs = report['clusterset']
                if failure == 'routers':
                    cs['routers'] = None
                elif failure == 'defaults':
                    cs['routing_options']['options'] = None
                else:
                    cs['routers'][0]['options'] = None
                actions = self.catalog(report, checks)
                self.assert_disabled(actions['remove_cluster:drcluster'])
                self.assert_disabled(actions['remove_cluster_keep:drcluster'])

    def test_primary_changes_require_readable_global_and_per_router_options(self):
        for action in ('set_primary_cluster:drcluster', 'force_primary_cluster:drcluster'):
            for failure in ('routers', 'defaults', 'override'):
                with self.subTest(action=action, failure=failure):
                    report = clusterset_report()
                    checks = clusterset_checks(report)
                    if action.startswith('force_primary_cluster'):
                        clusterset_unreachable(report, checks, 'maincluster')
                    cs = report['clusterset']
                    self.assertTrue(self.catalog(report, checks)[action]['enabled'])
                    if failure == 'routers':
                        cs['routers'] = None
                    elif failure == 'defaults':
                        cs['routing_options']['options'] = None
                    else:
                        cs['routers'][0]['options'] = None
                    entry = self.catalog(report, checks)[action]
                    self.assert_disabled(entry)
                    self.assertIn('Router', entry['why_not'])
                    self.assertIn('per-Router', entry['fix'])

    def test_empty_router_configuration_is_readable_for_primary_changes(self):
        for action in ('set_primary_cluster:drcluster', 'force_primary_cluster:drcluster'):
            with self.subTest(action=action):
                report = clusterset_report()
                checks = clusterset_checks(report)
                if action.startswith('force_primary_cluster'):
                    clusterset_unreachable(report, checks, 'maincluster')
                report['clusterset']['routers'] = []
                report['clusterset']['routing_options']['options'] = {}
                self.assertTrue(self.catalog(report, checks)[action]['enabled'])

    def test_routing_option_choices_keep_their_individual_gates(self):
        report = clusterset_report()
        minimums = {'target_cluster': (8, 0, 27), 'invalidated_cluster_policy': (8, 0, 27),
                    'stats_updates_frequency': (8, 0, 31), 'use_replica_primary_as_rw': (8, 0, 32),
                    'tags': (8, 1, 0), 'read_only_targets': (8, 1, 0), 'guideline': (9, 2, 0)}
        for version in ((8, 0, 27), (8, 0, 31), (8, 0, 32), (8, 1, 0), (9, 1, 99), (9, 2, 0)):
            choices = self.catalog(report, clusterset_checks(report, version))['set_routing_option']['options']
            self.assertEqual(set(choices), set(minimums))
            for option, minimum in minimums.items():
                with self.subTest(version=version, option=option):
                    self.assertEqual(choices[option]['minimum_shell'], list(minimum))
                    self.assertEqual(choices[option]['enabled'], version >= max(minimum, (8, 0, 32)))
                    if not choices[option]['enabled']:
                        self.assert_disabled(choices[option])

    def test_shell_check_is_read_only_and_discloses_its_connections(self):
        report = clusterset_report()
        entry = self.catalog(report, clusterset_checks(report))['check_clusterset']
        self.assertEqual(entry['kind'], 'read')
        self.assertFalse(entry['danger'])
        self.assertIn('every member', (entry['label'] + entry['description']).lower())

    def test_an_invalidated_calling_cluster_cannot_offer_ordinary_set_writes(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        cs = report['clusterset']
        cs.update(invalidated=True, local_role=None)
        mine = next(c for c in cs['clusters'] if c['local'])
        mine.update(invalidated=True, role=None, status='INVALIDATED')
        actions = self.catalog(report, checks)
        for aid in ('set_primary_cluster:drcluster', 'force_primary_cluster:drcluster',
                    'remove_cluster:drcluster', 'remove_cluster_keep:drcluster',
                    'set_routing_option', 'create_replica_cluster:spare'):
            with self.subTest(action=aid):
                self.assert_disabled(actions[aid])
                self.assertIn('invalidated', actions[aid]['why_not'])
        for aid in ('rejoin_cluster:mfcluster', 'rejoin_cluster:oldcluster',
                    'dissolve_clusterset', 'check_clusterset'):
            self.assertTrue(actions[aid]['enabled'], actions[aid])

    def test_guideline_choice_requires_the_metadata_schema_that_stores_it(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        for schema in ('2.2.0', '2.3.0', '2.4.0'):
            with self.subTest(schema=schema):
                report['metadata']['schema_version'] = schema
                option = self.catalog(report, checks)['set_routing_option']['options']['guideline']
                self.assertEqual(option['enabled'], schema >= '2.3.0')
                if not option['enabled']:
                    self.assert_disabled(option)
                    self.assertIn('2.3.0', option['why_not'])

    def test_create_requires_fresh_standalone_evidence_within_the_active_group(self):
        for changed in ('outside_group', 'no_group', 'not_read', 'read_error', 'in_cluster', 'in_group',
                        'old_server', 'wrong_engine', 'gtid_unknown', 'gtid_divergent', 'fencing_unknown',
                        'uuid_mismatch'):
            with self.subTest(changed=changed):
                report = clusterset_report()
                checks = clusterset_checks(report)
                candidate = checks['replica_candidates'][0]
                if changed == 'outside_group':
                    checks['group_profiles'] = [p for p in checks['group_profiles'] if p['id'] != 'spare']
                elif changed == 'no_group':
                    checks['group_name'] = None
                elif changed == 'not_read':
                    candidate['read'] = 'metadata_only'
                elif changed == 'read_error':
                    candidate['read_error'] = 'The standalone preflight was not completed.'
                elif changed == 'in_cluster':
                    candidate['kind'] = 'innodb_cluster'
                elif changed == 'in_group':
                    candidate['group_name'] = 'existing-group'
                elif changed == 'old_server':
                    candidate['server_version'] = '8.0.26'
                elif changed == 'wrong_engine':
                    candidate.update(engine='mariadb', server_version='10.11.16-MariaDB')
                elif changed == 'gtid_unknown':
                    candidate['gtid_subset'] = None
                elif changed == 'gtid_divergent':
                    candidate['gtid_subset'] = False
                elif changed == 'fencing_unknown':
                    candidate['super_read_only'] = None
                else:
                    candidate['route']['server_uuid'] = 'cccccccc-dddd-eeee-ffff-000000000001'
                create = [a for a in self.catalog(report, checks).values()
                          if a['id'].startswith('create_replica_cluster')]
                self.assertTrue(create)
                for entry in create:
                    self.assert_disabled(entry)


class TestClusterSetPlans(Home):
    def plan(self, report, aid, checks=None, parameters=None):
        return m.plan_action(report, aid, checks=checks or clusterset_checks(report), parameters=parameters)

    def test_standard_reviews_and_typed_destructive_reviews(self):
        report = clusterset_report()
        for aid in ('set_primary_cluster:drcluster', 'rejoin_cluster:oldcluster'):
            self.assertIsNone(self.plan(report, aid)['typed_guard'])
        for aid in ('remove_cluster:drcluster', 'remove_cluster_keep:drcluster'):
            self.assertEqual(self.plan(report, aid)['typed_guard'], 'drcluster')
        self.assertEqual(self.plan(report, 'dissolve_clusterset')['typed_guard'], 'production-set')
        create = self.plan(report, 'create_replica_cluster:spare',
                           parameters={'name': 'new-cluster', 'recoveryMethod': 'incremental'})
        self.assertEqual(create['typed_guard'], 'new-cluster')
        checks = clusterset_checks(report)
        clusterset_unreachable(report, checks, 'maincluster')
        self.assertEqual(self.plan(report, 'force_primary_cluster:drcluster', checks)['typed_guard'], 'drcluster')

    def test_a_self_route_cannot_bind_another_members_uuid(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        mine = next(c for c in report['clusterset']['clusters'] if c['local'])
        self.assertEqual(mine['route']['server_uuid'], report['server']['server_uuid'])
        mine['route']['server_uuid'] = mine['members'][1]['server_uuid']
        self.assertTrue(mine['route']['self'])
        with self.assertRaisesRegex(ValueError, 'route UUID'):
            self.plan(report, 'dissolve_clusterset', checks)

    def test_shell_check_and_refresh_use_only_the_read_and_refresh_calls(self):
        report = clusterset_report()
        check = self.plan(report, 'check_clusterset')
        self.assertIsNone(check['typed_guard'])
        self.assertIn('.status(', check['statements'][0])
        self.assertIn('extended', check['statements'][0])
        self.assertNotIn(m.CLUSTER_ACTION_TAG, check['statements'][0])
        replica = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        replica['status'] = 'NOT_REPLICATING'
        replica['channel']['present'] = False
        repair = self.plan(report, 'repair_cluster:drcluster')
        self.assertIsNone(repair['typed_guard'])
        self.assertIn('.rejoinCluster("drcluster")', repair['statements'][0])
        self.assertNotIn('removeCluster', repair['undo'])

    def test_create_uses_saved_address_and_requires_an_explicit_recovery_method(self):
        report = clusterset_report()
        plan = self.plan(report, 'create_replica_cluster:spare',
                         parameters={'name': 'new-cluster', 'recoveryMethod': 'incremental'})
        call = plan['statements'][0]
        self.assertIn('"saved-endpoint:3307"', call)
        # Object arguments use JSON.parse to preserve __proto__ as data;
        # decode both JSON layers instead of matching its display escapes.
        encoded, _ = json.JSONDecoder().raw_decode(call.split('JSON.parse(', 1)[1])
        self.assertEqual(json.loads(encoded)['recoveryMethod'], 'incremental')
        self.assertIn('recoveryProgress', call)
        self.assertNotIn('dryRun', call)
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in plan['facts']), plan['facts'])
        facts = dict(plan['facts'])
        self.assertEqual(facts['Reported address'], 'saved-endpoint:3307')
        self.assertEqual(facts['Instance'], 'saved-endpoint:3307')
        self.assertEqual(facts['Target super_read_only'], 'ON')
        self.assertEqual(facts['Target GTIDs subset of primary'], 'Yes')
        checks = clusterset_checks(report)
        checks['replica_candidates'][0]['super_read_only'] = False
        plan = self.plan(report, 'create_replica_cluster:spare', checks=checks,
                         parameters={'name': 'new-cluster', 'recoveryMethod': 'incremental'})
        self.assertEqual(dict(plan['facts'])['Target super_read_only'], 'OFF')
        for parameters in ({'name': 'new-cluster'}, {'name': 'new-cluster', 'recoveryMethod': 'auto'},
                           {'name': 'MFCLUSTER', 'recoveryMethod': 'incremental'}):
            with self.assertRaises(ValueError):
                self.plan(report, 'create_replica_cluster:spare', parameters=parameters)

    def test_create_clone_binds_the_saved_recipient_in_the_shell_guard(self):
        from myflames import reviewed_writes
        report = clusterset_report()
        checks = clusterset_checks(report)
        candidate = checks['replica_candidates'][0]
        plan = self.plan(report, 'create_replica_cluster:spare', checks=checks,
                         parameters={'name': 'new-cluster', 'recoveryMethod': 'clone'})
        self.assertEqual(plan['expect']['server_identity']['target_instance'], {
            'address': candidate['address'], 'server_uuid': candidate['server_uuid']})
        guard = reviewed_writes.session_guard(plan)[0]
        self.assertIn('shell.openSession', guard)
        self.assertIn(candidate['server_uuid'], guard)
        self.assertIn('myflamesTarget.close()', guard)

    def test_routing_options_accept_only_supported_values_and_shell_versions(self):
        cases = [('target_cluster', 'drcluster', (8, 0, 27)),
                 ('invalidated_cluster_policy', 'accept_ro', (8, 0, 27)),
                 ('stats_updates_frequency', 60, (8, 0, 31)),
                 ('use_replica_primary_as_rw', True, (8, 0, 32)),
                 ('tags', {'region': 'west'}, (8, 1, 0)),
                 ('read_only_targets', 'read_replicas', (8, 1, 0)),
                 ('guideline', 'application-guideline', (9, 2, 0))]
        report = clusterset_report()
        versions = ((8, 0, 27), (8, 0, 31), (8, 0, 32), (8, 0, 99), (8, 1, 0),
                    (9, 1, 99), (9, 2, 0), (9, 7, 0))
        for option, value, minimum in cases:
            for version in versions:
                with self.subTest(option=option, version=version):
                    checks = clusterset_checks(report, version)
                    parameters = {'option': option, 'value': value}
                    if version < max(minimum, (8, 0, 32)):
                        with self.assertRaises(ValueError):
                            self.plan(report, 'set_routing_option:router-host::rw', checks, parameters)
                    else:
                        plan = self.plan(report, 'set_routing_option:router-host::rw', checks, parameters)
                        self.assertIn(json.dumps(option), plan['statements'][0])
                        self.assertIn('"router-host::rw"', plan['statements'][0])
                        self.assertIsNone(plan['typed_guard'])
        for option, value in (('target_cluster', 'missing-cluster'), ('invalidated_cluster_policy', 'accept_rw'),
                              ('stats_updates_frequency', -1), ('stats_updates_frequency', True),
                              ('use_replica_primary_as_rw', 1), ('read_only_targets', 'append'), ('tags', [])):
            with self.subTest(option=option, value=value), self.assertRaises(ValueError):
                self.plan(report, 'set_routing_option', parameters={'option': option, 'value': value})

    def test_guideline_plan_requires_metadata_230(self):
        report = clusterset_report()
        parameters = {'option': 'guideline', 'value': 'application-guideline'}
        report['metadata']['schema_version'] = '2.2.0'
        with self.assertRaisesRegex(ValueError, '2.3.0'):
            self.plan(report, 'set_routing_option', parameters=parameters)
        report['metadata']['schema_version'] = '2.3.0'
        self.assertIn('application-guideline', self.plan(report, 'set_routing_option',
                                                        parameters=parameters)['statements'][0])

    def test_effects_explain_routing_removal_failover_and_dissolve(self):
        report = clusterset_report()
        switchover = self.plan(report, 'set_primary_cluster:drcluster')
        self.assertIn('Router', switchover['description'] + ' '.join(switchover['warnings']))
        self.assertIn('guideline', (switchover['description'] + ' '.join(switchover['warnings'])).lower())
        rejoin = self.plan(report, 'rejoin_cluster:oldcluster')
        self.assertNotIn('removeCluster', rejoin['undo'])
        remove = self.plan(report, 'remove_cluster:drcluster')
        self.assertIn('dissolv', remove['description'].lower())
        routing = self.plan(report, 'set_routing_option', parameters={'option': 'target_cluster', 'value': 'primary'})
        routing_text = routing['description'] + ' '.join(routing['warnings'])
        self.assertIn('stored configuration', routing_text.lower())
        self.assertIn('refresh', routing_text.lower())
        dissolve = self.plan(report, 'dissolve_clusterset')
        dissolve_text = dissolve['description'] + ' '.join(dissolve['warnings']) + ' '.join(dissolve['checks'])
        self.assertIn('--no-wizard', dissolve_text)
        self.assertIn('every reachable instance', dissolve_text.lower())
        checks = clusterset_checks(report)
        clusterset_unreachable(report, checks, 'maincluster')
        report['clusterset']['from_local_copy'] = True
        failover = self.plan(report, 'force_primary_cluster:drcluster', checks)
        failover_text = failover['description'] + ' '.join(failover['warnings'])
        self.assertIn('lost', failover_text.lower())
        self.assertIn('isolated', failover_text.lower())
        self.assertIn('Router', failover_text)
        self.assertNotIn("reads the primary cluster's before", failover_text)

    def test_removal_names_global_and_explicit_router_pins_before_its_effects(self):
        report = clusterset_report()
        cs = report['clusterset']
        cs['routing_options']['options']['target_cluster'] = report._clusterset_groups['drcluster']
        explicit = dict(cs['routers'][0], id=2, name='explicit-host::rw', options={'target_cluster': 'drcluster'})
        other = dict(cs['routers'][0], id=3, name='other-host::rw', options={'target_cluster': 'primary'})
        cs['routers'] += [explicit, other]
        for aid in ('remove_cluster:drcluster', 'remove_cluster_keep:drcluster'):
            with self.subTest(action=aid):
                plan = self.plan(report, aid)
                self.assertEqual(plan['facts'][0][0], 'Routing targets to change first')
                self.assertIn('router-host::rw', plan['facts'][0][1])
                self.assertIn('explicit-host::rw', plan['facts'][0][1])
                self.assertIn('ClusterSet routing default', plan['facts'][0][1])
                self.assertNotIn('other-host::rw', plan['facts'][0][1])

    def test_a_router_pin_changed_since_remove_review_changes_the_bound_expectation(self):
        report = clusterset_report()
        reviewed = self.plan(report, 'remove_cluster:drcluster')
        view_id = report['clusterset']['view_id']
        report['clusterset']['routers'][0]['options']['target_cluster'] = 'drcluster'
        fresh = self.plan(report, 'remove_cluster:drcluster')
        self.assertEqual(report['clusterset']['view_id'], view_id)
        self.assertEqual(reviewed['statements'], fresh['statements'])
        self.assertNotEqual(reviewed['expect'], fresh['expect'])

    def test_primary_change_plans_detach_and_bind_the_complete_routing_snapshot(self):
        for action in ('set_primary_cluster:drcluster', 'force_primary_cluster:drcluster'):
            with self.subTest(action=action):
                report = clusterset_report()
                checks = clusterset_checks(report)
                if action.startswith('force_primary_cluster'):
                    clusterset_unreachable(report, checks, 'maincluster')
                cs = report['clusterset']
                cs['routing_options']['options'] = {'target_cluster': 'primary', 'tags': {'active': False}}
                cs['routers'][0]['options'] = {'target_cluster': 'drcluster', 'read_only_targets': 'all'}
                plan = self.plan(report, action, checks)
                snapshot = copy.deepcopy({'defaults': cs['routing_options'], 'routers': cs['routers']})
                self.assertEqual(plan['expect']['routing_configuration'], snapshot)
                cs['routing_options']['options']['tags']['active'] = True
                cs['routers'][0]['options']['target_cluster'] = 'primary'
                self.assertEqual(plan['expect']['routing_configuration'], snapshot)

    def test_force_is_not_accepted_for_a_reachable_target(self):
        report = clusterset_report()
        for aid in ('remove_cluster:drcluster', 'remove_cluster_keep:drcluster', 'dissolve_clusterset'):
            with self.subTest(action=aid), self.assertRaises(ValueError):
                self.plan(report, aid, parameters={'force': True})

    def test_invalidation_options_cannot_name_healthy_primary_or_target_clusters(self):
        report = clusterset_report()
        checks = clusterset_checks(report)
        for names in ([], ['maincluster'], ['mfcluster'], ['drcluster'], ['not-in-the-set']):
            with self.subTest(names=names), self.assertRaises(ValueError):
                self.plan(report, 'set_primary_cluster:drcluster', checks,
                          {'invalidateReplicaClusters': names})
        # A previously invalidated cluster is not an unavailable replica that
        # this operation may newly invalidate.
        with self.assertRaises(ValueError):
            self.plan(report, 'set_primary_cluster:drcluster', checks,
                      {'invalidateReplicaClusters': ['oldcluster']})

    def test_unsupported_create_options_cannot_bypass_the_reviewed_target(self):
        report = clusterset_report()
        required = {'name': 'new-cluster', 'recoveryMethod': 'incremental'}
        for key, value in (('instance', 'outside.example:3306'), ('address', 'outside.example:3306'),
                            ('recoveryProgress', 3), ('recoveryProgress', True), ('timeout', -1),
                            ('communicationStack', 'MySQL'), ('paxosSingleLeader', True),
                            ('clusterSetReplicationBind', 'outside-interface')):
            with self.subTest(option=key), self.assertRaises(ValueError):
                self.plan(report, 'create_replica_cluster:spare', parameters=dict(required, **{key: value}))

    def test_preview_options_cannot_enter_any_mutating_plan(self):
        report = clusterset_report()
        for aid, parameters in (
                ('set_primary_cluster:drcluster', {}), ('rejoin_cluster:oldcluster', {}),
                ('remove_cluster:drcluster', {}), ('remove_cluster_keep:drcluster', {}),
                ('set_routing_option', {'option': 'target_cluster', 'value': 'primary'}),
                ('create_replica_cluster:spare', {'name': 'new-cluster', 'recoveryMethod': 'incremental'}),
                ('dissolve_clusterset', {})):
            plan = self.plan(report, aid, parameters=parameters)
            self.assertNotIn('dryRun', '\n'.join(plan['statements']))
            for preview in (True, False):
                with self.subTest(action=aid, preview=preview), self.assertRaises(ValueError):
                    self.plan(report, aid, parameters=dict(parameters, dryRun=preview))

    def test_plan_binds_the_target_route_uuid_and_set_view(self):
        report = clusterset_report()
        target = next(c for c in report['clusterset']['clusters'] if c['name'] == 'drcluster')
        plan = self.plan(report, 'set_primary_cluster:drcluster')
        self.assertEqual(plan['expect']['clusterset_view_id'], report['clusterset']['view_id'])
        identity = plan['expect']['clusterset_target']
        self.assertEqual(identity['cluster_name'], target['name'])
        self.assertEqual(identity['server_uuid'], target['route']['server_uuid'])
        self.assertEqual(identity['view_id'], report['clusterset']['view_id'])
        self.assertEqual(plan['expect']['server_identity']['adminapi']['groups'], report._clusterset_groups)

    def test_the_review_token_covers_effects_statements_confirmation_and_identity(self):
        from myflames import reviewed_writes
        from myflames.review_tokens import ReviewTokens
        report = clusterset_report()
        plan = self.plan(report, 'remove_cluster:drcluster')
        tokens = ReviewTokens()
        identity = {'profile_id': 'cs-0', 'type': 'production'}
        token = tokens.issue('cluster_action', identity, reviewed_writes.review_details(plan))
        for field, value in (('typed_guard', None), ('description', 'Nothing changes.'),
                             ('statements', ['myflamesClusterSet.dissolve();']), ('warnings', [])):
            changed = copy.deepcopy(plan)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                tokens.check(token, 'cluster_action', identity, reviewed_writes.review_details(changed))
        changed = copy.deepcopy(plan)
        changed['expect']['clusterset_view_id'] = '4294967302'
        with self.assertRaises(ValueError):
            tokens.check(token, 'cluster_action', identity, reviewed_writes.review_details(changed))
        for key, value in (('server_uuid', 'cccccccc-dddd-eeee-ffff-000000000001'),
                            ('cluster_name', 'different-cluster')):
            changed = copy.deepcopy(plan)
            changed['expect']['clusterset_target'][key] = value
            with self.subTest(identity=key), self.assertRaises(ValueError):
                tokens.check(token, 'cluster_action', identity, reviewed_writes.review_details(changed))
        tokens.check(token, 'cluster_action', identity, reviewed_writes.review_details(plan))

    def test_a_changed_view_or_wrong_confirmation_is_refused_before_any_send(self):
        from myflames import reviewed_writes
        class NoSend(object):
            def run_write(self, *args, **kwargs):
                raise AssertionError('A refused plan must not reach a sender.')
        report = clusterset_report()
        plan = self.plan(report, 'remove_cluster:drcluster')
        wrong = reviewed_writes.execute(NoSend(), plan, lambda: self.plan(report, 'remove_cluster:drcluster'),
                                        m.ACTION_SCHEMA_VERSION, typed='wrong-cluster')
        self.assertEqual((wrong['status'], wrong['outcome']), ('refused', 'typed_guard'))
        report['clusterset']['view_id'] = '4294967302'
        changed = reviewed_writes.execute(NoSend(), plan, lambda: self.plan(report, 'remove_cluster:drcluster'),
                                          m.ACTION_SCHEMA_VERSION, typed='drcluster')
        self.assertEqual((changed['status'], changed['outcome']), ('refused', 'changed'))



class TestOffered(Home):
    def test_galera_desync_resync_and_bootstrap(self):
        healthy = F.report('galera-11.8-healthy')
        self.assertEqual(offered(healthy, local(healthy)['name']), ['desync_node'])
        for x in healthy['members']:
            if not x['local']:
                self.assertFalse(any(a['enabled'] for a in m.available_actions(healthy, x['key'])),
                                 'node SQL never runs on another node')
        donor = F.report('galera-11.8-donor')
        self.assertEqual((donor['galera']['desync'], offered(donor, local(donor)['name'])), ('ON', ['resync_node']))
        non_primary = F.report('galera-11.8-non-primary')
        self.assertEqual(offered(non_primary, local(non_primary)['name']), ['bootstrap_pc'])
        self.assertEqual(offered(F.report('galera-11.8-joiner'), local(F.report('galera-11.8-joiner'))['name']), [],
                         'a joining node is never bootstrapped')
        self.assertEqual(offered(F.report('galera-11.8-wsrep-off'),
                                 local(F.report('galera-11.8-wsrep-off'))['name']), [])

    def test_donor_without_wsrep_desync_offers_neither(self):
        report = F.report('galera-11.8-healthy')
        report['galera']['metrics']['local_state_comment'] = 'Donor/Desynced'
        self.assertEqual(offered(report, local(report)['name']), [], 'a state transfer or backup lock ends on its own')

    def test_component_not_read_or_disconnected_is_not_bootstrapped(self):
        report = F.report('galera-11.8-non-primary')
        for component, connected in ((None, True), ('Disconnected', True), ('non-Primary', False)):
            r = copy.deepcopy(report)
            r['cluster']['component'] = component
            r['galera']['metrics']['connected'] = connected
            self.assertNotIn('bootstrap_pc', offered(r, local(r)['name']), (component, connected))

    def test_no_wsrep_on_actions_anywhere(self):
        for name in F.ALL:
            report = F.report(name)
            for x in report['members']:
                for a in m.available_actions(report, x['key']):
                    self.assertNotIn(a['id'], ('stop_node', 'start_node'), name)
                    self.assertNotIn('GCache', a['label'] + a['description'], name)

    def test_pxc_and_galera_for_mysql_offer_nothing(self):
        for name in ('pxc-8.4-healthy', 'pxc-8.4-donor', 'galera-mysql-8.4-healthy'):
            report = F.report(name)
            self.assertEqual([a for x in report['members'] for a in m.available_actions(report, x['key'])], [], name)

    def test_group_replication(self):
        ic = F.report('ic-8.4-healthy')
        primary = next(x for x in ic['members'] if x['role'] == 'PRIMARY')
        self.assertEqual(offered(ic, primary['name']), [], 'the primary is not switched to itself')
        for x in ic['members']:
            ids = [a['id'] for a in m.available_actions(ic, x['key']) if a['enabled']]
            if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE':
                self.assertIn('switchover', ids)
            self.assertEqual('stop_member' in ids, x['local'])
            self.assertNotIn('switch_to_multi_primary', ids, 'InnoDB Cluster mode switches belong to MySQL Shell')
        offline = F.report('ic-8.4-local-offline')
        self.assertEqual(offered(offline, local(offline)['name']), ['start_member'])
        degraded = F.report('ic-8.4-degraded')
        self.assertNotIn('switchover', [a['id'] for x in degraded['members'] for a in m.available_actions(degraded, x['key']) if a['enabled']],
                         'no switchover while a member is recovering or unreachable')
        plain = F.report('gr-8.4-multi-primary')
        self.assertIn('switch_to_single_primary', offered(plain, local(plain)['name']))

    def test_a_member_in_error_offers_stop_not_start(self):
        report = F.report('ic-8.4-local-offline')
        local(report)['state'] = 'ERROR'
        self.assertEqual(offered(report, local(report)['name']), ['stop_member'])

    def test_switchover_needs_a_uuid(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        target['uuid'] = "x') OR ('1"
        self.assertNotIn('switchover', offered(report, target['name']))
        target['uuid'] = None
        self.assertNotIn('switchover', offered(report, target['name']))

    def test_old_mysql_offers_no_group_change(self):
        report = F.report('ic-8.4-healthy')
        report['server_version'] = '8.0.12'
        self.assertEqual([a['id'] for x in report['members'] for a in m.available_actions(report, x['key'])
                          if a['enabled']], [], 'All reviewed Group Replication changes need MySQL 8.0.13')

    def test_workspace_reports_match_the_schema(self):
        from pathlib import Path
        from test_chart_data import validate
        schema = json.loads((Path(__file__).resolve().parent.parent / 'docs' / 'schemas' / 'cluster-1.0.json')
                            .read_text(encoding='utf-8'))
        for name in F.ALL:
            validate(m.with_actions(F.report(name)), schema)

    def test_with_actions_is_only_for_the_workspace(self):
        report = F.report('galera-11.8-healthy')
        self.assertNotIn('actions', local(report), 'the CLI and MCP report has no actions')
        self.assertEqual([a['id'] for a in local(m.with_actions(report))['actions']],
                         ['desync_node', 'resync_node', 'bootstrap_pc'])


class TestPlans(Home):
    def test_switchover_sends_the_member_uuid(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        plan = m.plan_action(report, 'switchover', target['key'])
        self.assertEqual(plan['statements'], ["/*myflames:cluster_op*/ SELECT group_replication_set_as_primary('%s');"
                                              % target['uuid']])
        self.assertRegex(target['uuid'], r'^[0-9a-f-]{36}$')
        primary = next(x for x in report['members'] if x['role'] == 'PRIMARY')
        self.assertIn(primary['uuid'], plan['undo'])
        self.assertEqual(plan['expect']['member']['uuid'], target['uuid'])
        self.assertIsNone(plan['typed_guard'])
        self.assertEqual(dict(plan['facts'])['Server'], 'MySQL 8.4.11')

    def test_plan_refuses_what_is_not_offered(self):
        report = F.report('galera-11.8-healthy')
        other = next(x for x in report['members'] if not x['local'])
        for action, key in (('desync_node', other['key']), ('bootstrap_pc', local(report)['key']),
                            ('stop_node', local(report)['key']), ('resync_node', local(report)['key']),
                            ('desync_node', None), ('desync_node', 'member:nope')):
            with self.assertRaises(ValueError, msg=(action, key)):
                m.plan_action(report, action, key)

    def test_statements_and_guards(self):
        galera = F.report('galera-11.8-healthy')
        plan = m.plan_action(galera, 'desync_node', local(galera)['key'])
        self.assertEqual(plan['statements'], ['/*myflames:cluster_op*/ SET GLOBAL wsrep_desync = ON;'])
        self.assertNotIn('GCache', plan['title'] + plan['description'])
        resync = F.report('galera-11.8-donor')
        self.assertEqual(m.plan_action(resync, 'resync_node', local(resync)['key'])['statements'],
                         ['/*myflames:cluster_op*/ SET GLOBAL wsrep_desync = OFF;'])
        np = F.report('galera-11.8-non-primary')
        boot = m.plan_action(np, 'bootstrap_pc', local(np)['key'])
        self.assertEqual(boot['statements'], ["/*myflames:cluster_op*/ SET GLOBAL wsrep_provider_options = "
                                              "'pc.bootstrap=YES';"])
        self.assertEqual(boot['typed_guard'], local(np)['name'])
        self.assertIn('split brain', boot['description'])
        self.assertIn('wsrep_last_committed here', dict(boot['facts']))
        ic = F.report('ic-8.4-healthy')
        stop = m.plan_action(ic, 'stop_member', local(ic)['key'])
        self.assertEqual(stop['statements'], ['/*myflames:cluster_op*/ STOP GROUP_REPLICATION;'])
        self.assertIn('super_read_only', stop['description'])

    def test_stopping_the_primary_or_the_last_member_is_typed(self):
        report = F.report('gr-8.4-two-members')
        me = local(report)
        stop = m.plan_action(report, 'stop_member', me['key'])
        self.assertEqual(stop['typed_guard'] if me['role'] == 'PRIMARY' else None,
                         me['name'] if me['role'] == 'PRIMARY' else None)
        last = F.report('ic-8.4-healthy')
        for x in last['members']:
            if not x['local'] and x['state_source'] == 'replication_group_members':
                x['state'] = 'OFFLINE'
        stop = m.plan_action(last, 'stop_member', local(last)['key'])
        self.assertEqual(stop['typed_guard'], local(last)['name'])
        self.assertIn('bootstrapped again', stop['undo'])

    def test_start_refuses_with_bootstrap_group_on_or_unknown(self):
        report = F.report('ic-8.4-local-offline')
        key = local(report)['key']
        for value in ('ON', '1', None):
            with self.assertRaises(ValueError, msg=value):
                m.plan_action(report, 'start_member', key, {'bootstrap_group': value})
        plan = m.plan_action(report, 'start_member', key, {'bootstrap_group': 'OFF'})
        self.assertEqual(plan['statements'], ['/*myflames:cluster_op*/ START GROUP_REPLICATION;'])
        self.assertIn('clone', plan['description'])
        self.assertEqual(plan['expect']['bootstrap_group'], 'OFF')

    def test_mode_switch_is_typed_and_warns_about_auto_increment(self):
        report = F.report('gr-8.4-two-members')
        plan = m.plan_action(report, 'switch_to_multi_primary', local(report)['key'])
        self.assertEqual(plan['statements'], ['/*myflames:cluster_op*/ SELECT group_replication_switch_to_multi_primary_mode();'])
        self.assertEqual(plan['typed_guard'], local(report)['name'])
        self.assertTrue(any('auto_increment' in w for w in plan['warnings']))

    def test_every_cluster_statement_is_risky(self):
        from myflames import reviewed_writes
        for name, action in (('galera-11.8-healthy', 'desync_node'), ('galera-11.8-donor', 'resync_node'),
                             ('ic-8.4-healthy', 'stop_member')):
            report = F.report(name)
            plan = m.plan_action(report, action, local(report)['key'])
            self.assertEqual(dbops.decide(reviewed_writes.worst_kind(plan['statements']), 'development')['action'],
                             'confirm', name)


class Endpoints(Home):
    def setUp(self):
        super().setUp()
        from myflames.workspace_store import WorkspaceStore
        self.workspace = type('Workspace', (), {'store': WorkspaceStore(os.path.join(self.tmp.name, 'ws.sqlite3'))})()
        self.connection = {'host': '127.0.0.1', 'port': 34101, 'user': 'mf_admin', 'password': 'pw-secret'}

    def api(self, path, body, conn):
        factory = lambda **options: conn  # noqa: E731
        with mock.patch('myflames.connector.MySQLConnection', factory):
            return ui.api_response(path, body, self.workspace)

    def plan(self, conn, action, member_key):
        return self.api('/api/live/cluster/plan', {'connection': self.connection, 'action': action,
                                                    'member_key': member_key}, conn)

    def send(self, conn, plan, typed=None, token=None):
        plan = dict(plan)
        own = plan.pop('review_token', None)
        return self.api('/api/live/cluster/action', {'connection': self.connection, 'plan': plan,
                                                      'review_token': own if token is None else token,
                                                      'typed': typed, 'confirmed': True}, conn)

    def audit(self):
        return dbops.audit_report(self.workspace.store)['records']

    def test_read_endpoint_adds_actions(self):
        report = self.api('/api/live/cluster', {'connection': self.connection}, F.conn('galera-11.8-healthy'))
        self.assertEqual([a['id'] for a in local(report)['actions']],
                         ['desync_node', 'resync_node', 'bootstrap_pc'])
        self.assertEqual([a['id'] for a in local(report)['actions'] if a['enabled']], ['desync_node'])
        self.assertIn('/api/live/cluster/action', ui._WRITE_PATHS)
        self.assertNotIn('/api/live/cluster/action', ui._SERVER_VIEW_PATHS)

    def test_desync_is_sent_once_after_a_fresh_read_and_audited(self):
        conn = Writes(F.conn('galera-11.8-healthy'))
        report = F.report('galera-11.8-healthy')
        plan = self.plan(conn, 'desync_node', local(report)['key'])
        result = self.send(conn, plan)
        self.assertEqual(result['status'], 'done', result)
        from _reviewed_replay import guarded_packets
        self.assertEqual(len(conn.writes), 1)
        self.assertEqual([(g, s) for g, s, _ in guarded_packets(conn.writes[0])],
                         [(plan['session_guard'][0], dbops.split_statements(s)[0]['text']) for s in plan['statements']])
        (record,) = self.audit()
        self.assertEqual((record['operation'], record['status'], record['connection']['engine']),
                         ('cluster_action', 'ok', 'mariadb'))
        self.assertNotIn('pw-secret', json.dumps(record))

    def test_another_member_or_a_changed_state_sends_nothing(self):
        report = F.report('galera-11.8-healthy')
        other = next(x for x in report['members'] if not x['local'])
        with self.assertRaises(ValueError):
            self.plan(Writes(F.conn('galera-11.8-healthy')), 'desync_node', other['key'])
        # Reviewed on a Synced node, sent after it became a donor.
        plan = self.plan(Writes(F.conn('galera-11.8-healthy')), 'desync_node', local(report)['key'])
        donor = Writes(F.conn('galera-11.8-donor'))
        result = self.send(donor, plan)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(donor.writes, [])
        (record,) = self.audit()
        self.assertEqual((record['status'], record['outcome']), ('refused', 'changed'))

    def test_bootstrap_needs_the_typed_name(self):
        report = F.report('galera-11.8-non-primary')
        name, key = local(report)['name'], local(report)['key']
        conn = Writes(F.conn('galera-11.8-non-primary'))
        plan = self.plan(conn, 'bootstrap_pc', key)
        self.assertEqual(self.send(conn, plan, typed='galera')['status'], 'refused')
        self.assertEqual(conn.writes, [])
        plan = self.plan(conn, 'bootstrap_pc', key)
        self.assertEqual(self.send(conn, plan, typed=name)['status'], 'done')
        self.assertEqual(len(conn.writes), 1)

    def test_bootstrap_refuses_when_the_component_changed(self):
        # Codex review: a node joining the non-Primary component needs another review.
        report = F.report('galera-11.8-non-primary')
        name, key = local(report)['name'], local(report)['key']
        conn = Writes(F.conn('galera-11.8-non-primary'))
        plan = self.plan(conn, 'bootstrap_pc', key)
        self.assertIn('component_view', plan['expect'])
        changed = copy.deepcopy(report)
        changed['cluster']['size'] = (changed['cluster']['size'] or 1) + 1
        changed['galera']['metrics']['cluster_conf_id'] = (changed['galera']['metrics'].get('cluster_conf_id') or 0) + 1
        with mock.patch.object(m, 'read_status', return_value=changed):
            result = self.send(conn, plan, typed=name)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(conn.writes, [])

    def test_start_reads_bootstrap_group_at_send_time(self):
        report = F.report('ic-8.4-local-offline')
        key = local(report)['key']
        conn = Writes(F.conn('ic-8.4-local-offline'))
        plan = self.plan(conn, 'start_member', key)
        conn.bootstrap = 'ON'
        result = self.send(conn, plan)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(conn.writes, [])

    def test_result_carries_states_effect_checks_and_progress(self):
        conn = Writes(F.conn('galera-11.8-healthy'))
        report = F.report('galera-11.8-healthy')
        plan = self.plan(conn, 'desync_node', local(report)['key'])
        self.assertEqual(plan['timeout_s'], m.ACTION_TIMEOUT)
        self.assertIn('wsrep_desync', plan['checks'][0])
        plan = dict(plan)
        token = plan.pop('review_token')
        result = self.api('/api/live/cluster/action', {'connection': self.connection, 'plan': plan,
                                                        'review_token': token, 'confirmed': True,
                                                        'progress_id': 'c' * 24}, conn)
        self.assertEqual((result['status'], result['states']), ('done', ['done']))
        self.assertEqual([row[0] for row in result['effect']], ['wsrep_desync', 'State'])
        self.assertEqual(result['checks'], plan['checks'])
        progress = ui.api_response('/api/live/action/progress', {'progress_id': 'c' * 24}, self.workspace)
        self.assertEqual((progress['phase'], progress['states'], progress['status']), ('finished', ['done'], 'done'))

    def test_lost_reply_is_unknown(self):
        report = F.report('ic-8.4-healthy')
        conn = Writes(F.conn('ic-8.4-healthy'), fail=ConnectorError('Query timed out after 120s; the outcome is unknown.'))
        plan = self.plan(conn, 'stop_member', local(report)['key'])
        result = self.send(conn, plan, typed=plan['typed_guard'])
        self.assertEqual((result['status'], result['states'], result['effect']), ('unknown', ['unknown'], None))
        (record,) = self.audit()
        self.assertEqual(record['status'], 'unknown')

    def test_server_refusal_is_failed(self):
        report = F.report('ic-8.4-healthy')
        target = next(x for x in report['members'] if x['role'] == 'SECONDARY' and x['state'] == 'ONLINE')
        conn = Writes(F.conn('ic-8.4-healthy'), fail=ConnectorError(
            "mysql client exited 1: ERROR 3910 (HY000) at line 2: The function 'group_replication_set_as_primary' "
            "failed. There is a replica channel running in the group's current primary member."))
        result = self.send(conn, self.plan(conn, 'switchover', target['key']))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('replica channel', result['message'])

    def test_bootstrap_unsigned_conf_id_and_large_last_committed_survive_js_roundtrip(self):
        for fixture, expected_conf_id, expected_last in (
                ('galera-11.8-non-primary-unsigned', '18446744073709551615', '25'),
                ('galera-11.8-non-primary', '-1', '25')):
            with self.subTest(fixture=fixture):
                report = F.report(fixture)
                name, key = local(report)['name'], local(report)['key']
                plan = m.plan_action(report, 'bootstrap_pc', key)
                self.assertIs(type(plan['expect']['component_view']['conf_id']), str)
                self.assertIs(type(plan['expect']['last_committed']), str)
                self.assertEqual(plan['expect']['component_view']['conf_id'], expected_conf_id)
                self.assertEqual(plan['expect']['last_committed'], expected_last)
                conn = Writes(F.conn(fixture))
                result = m.execute_action(conn, js_json_roundtrip(plan), typed=name)
                self.assertEqual(result['status'], 'done', result)
                self.assertEqual(len(conn.writes), 1)
                # Full API round-trip (review token + JS JSON parse/stringify).
                api_conn = Writes(F.conn(fixture))
                api_plan = js_json_roundtrip(self.plan(api_conn, 'bootstrap_pc', key))
                api_result = self.send(api_conn, api_plan, typed=name)
                self.assertEqual(api_result['status'], 'done', api_result)
                self.assertEqual(len(api_conn.writes), 1)

        # Also verify wsrep_last_committed above 2^53-1 and missing (None) values.
        large_seqno = '9007199254740993'
        large_edit = F.editor((F.WSREP, F.pairs(
            wsrep_cluster_conf_id='18446744073709551615', wsrep_last_committed=large_seqno)))
        large_conn = Writes(F.recorded('galera-11.8-non-primary', edit=large_edit)[0])
        large_report = m.read_status(large_conn)
        name, key = local(large_report)['name'], local(large_report)['key']
        large_plan = m.plan_action(large_report, 'bootstrap_pc', key)
        self.assertEqual(large_plan['expect']['last_committed'], large_seqno)
        self.assertEqual(large_plan['expect']['component_view']['conf_id'], '18446744073709551615')
        large_res = m.execute_action(large_conn, js_json_roundtrip(large_plan), typed=name)
        self.assertEqual(large_res['status'], 'done', large_res)
        self.assertEqual(len(large_conn.writes), 1)

        missing_edit = F.editor((F.WSREP, F.pairs(wsrep_cluster_conf_id=None, wsrep_last_committed=None)))
        missing_conn = Writes(F.recorded('galera-11.8-non-primary', edit=missing_edit)[0])
        missing_report = m.read_status(missing_conn)
        missing_plan = m.plan_action(missing_report, 'bootstrap_pc', local(missing_report)['key'])
        self.assertIsNone(missing_plan['expect']['last_committed'])
        self.assertIsNone(missing_plan['expect']['component_view']['conf_id'])
        missing_res = m.execute_action(missing_conn, js_json_roundtrip(missing_plan),
                                       typed=local(missing_report)['name'])
        self.assertEqual(missing_res['status'], 'done', missing_res)
        self.assertEqual(len(missing_conn.writes), 1)

    def test_execute_action_rejects_coerced_server_identity_types(self):
        report = F.report('galera-11.8-healthy')
        key = local(report)['key']
        base_plan = m.plan_action(report, 'desync_node', key)
        self.assertEqual(base_plan['expect']['server']['server_id'], 1)
        self.assertEqual(base_plan['expect']['server']['port'], 3306)
        for field, bad_value in (('server_id', True), ('server_id', 1.0), ('port', 3306.0)):
            with self.subTest(field=field, bad_value=bad_value):
                tampered = copy.deepcopy(base_plan)
                tampered['expect']['server'][field] = bad_value
                conn = Writes(F.conn('galera-11.8-healthy'))
                with mock.patch.object(m, 'action_checks', wraps=m.action_checks) as checks_spy:
                    result = m.execute_action(conn, tampered)
                self.assertEqual((result['status'], result.get('outcome')), ('refused', 'changed'))
                self.assertIn('reaches another server or cluster', result['message'])
                self.assertEqual(conn.writes, [])
                checks_spy.assert_not_called()


if __name__ == '__main__':
    unittest.main()
