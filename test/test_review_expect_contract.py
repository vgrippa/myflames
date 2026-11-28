"""Review-token JSON contract: every planner's ``expect`` (and any
client-echoed identity) must survive the workspace's JavaScript round trip.

apps/workspace/src/api.ts parses and re-serializes every plan, so a float
(``1.0`` becomes ``1``), ``-0.0`` or an int outside -(2^53-1)..2^53-1 comes
back changed, and reviewed_writes.same_reviewed_value (type-exact) could
never confirm it. The plans below are built from the same fixtures and
replays the module tests use; the check walks every scalar of the reviewed
identity subtrees. Built with a temporary HOME and the vault backend, like
the module tests.
"""
import os
import tempfile
import unittest
from unittest import mock

MAX_SAFE_INTEGER = 2 ** 53 - 1


def unsafe_values(value, path="$"):
    """Yield (path, value) for every scalar JavaScript cannot keep exact."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, float):
        yield path, value
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            yield path, value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            for bad in unsafe_values(item, "%s.%s" % (path, key)):
                yield bad
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            for bad in unsafe_values(item, "%s[%d]" % (path, index)):
                yield bad


class ContractCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"HOME": self.tmp.name, "USERPROFILE": self.tmp.name,
                                               "MYFLAMES_SECRET_BACKEND": "vault"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.planned = 0
        self.unreached = []

    def check(self, label, subtree):
        """Assert no float and no JS-unsafe int anywhere under *subtree*."""
        bad = list(unsafe_values(subtree))
        self.assertEqual(bad, [], "%s carries values the desktop app cannot echo exactly: %s" % (label, bad))

    def check_plan(self, label, plan):
        self.planned += 1
        for key in ("expect", "identity"):
            if isinstance(plan.get(key), (dict, list)):
                self.check("%s[%s]" % (label, key), plan[key])


class ClusterPlansTest(ContractCase):
    def test_every_offered_action_plan(self):
        import _cluster_fixtures as F
        from myflames import cluster as m
        checks = {"bootstrap_group": "OFF"}  # The recordings predate this preflight variable.
        for name in F.ALL:
            report = F.report(name)
            if report.get("kind") in (None, "none"):
                continue
            for x in report.get("members") or ():
                catalog = list(m.available_actions(report, x.get("key"), checks))
                if x.get("local"):
                    catalog += m.overview_actions(report, checks)
                for entry in catalog:
                    if not entry.get("enabled"):
                        continue
                    with self.subTest(fixture=name, member=x.get("key"), action=entry["id"]):
                        try:
                            plan = m.plan_action(report, entry["id"], x.get("key"), checks)
                        except ValueError as exc:
                            self.unreached.append("%s/%s: %s" % (name, entry["id"], exc))
                            continue
                        self.check_plan("cluster %s %s" % (name, entry["id"]), plan)
        self.assertGreater(self.planned, 0, "no cluster plan was reachable")
        self.assertEqual(self.unreached, [], "enabled cluster actions raised ValueError: %s" % self.unreached)


class VariablesPlanTest(ContractCase):
    def test_plan_change_expect(self):
        from test_variables import replay
        from myflames import variables
        for server in ("mysql-8.4", "mariadb-11.4"):
            with self.subTest(server=server):
                conn, _ = replay(server, "global")
                plan = variables.plan_change(conn, "max_connections", "300", "global", grants=False)
                self.check_plan("variables %s" % server, plan)


class AccountsPlanTest(ContractCase):
    def test_plan_change_expect(self):
        from test_accounts import FakeConn, actor
        from myflames import accounts, privileges
        for engine in ("mysql", "mariadb"):
            with self.subTest(engine=engine):
                conn = FakeConn(engine)
                with mock.patch.object(privileges, "read_privileges", return_value=actor(engine)):
                    plan = accounts.plan_lock(conn, "app")
                self.check_plan("accounts %s" % engine, plan)
                if isinstance(plan.get("actor_server"), dict):
                    self.check("accounts %s[actor_server]" % engine, plan["actor_server"])


class ReplicationPlanTest(ContractCase):
    def test_plan_skip_identity(self):
        from test_replication import status
        from myflames import replication
        for name in ("mysql-8.4-gtid-error", "mariadb-11.4-error"):
            with self.subTest(fixture=name):
                report, _ = status(name)
                channel = (report.get("channels") or [{}])[0].get("name") or ""
                plan = replication.plan_skip(report, channel)
                self.check_plan("replication %s" % name, plan)


class ProxysqlPlansTest(ContractCase):
    def test_every_catalog_entry(self):
        from test_proxysql import Home
        from myflames import proxysql

        case = Home(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        conn = case.admin()
        report = proxysql.read_status(conn)
        targets = [None] + [{"config": s["config_key"]} for s in report["servers"] if s.get("config_key")
                            ] + [{"rule_id": r["rule_id"]} for r in report["query_rules"]]
        for target in targets:
            for entry in proxysql.available_actions(report, target):
                # available_actions lists only what is executable now (no flag).
                with self.subTest(action=entry["id"], target=target):
                    plan = proxysql.plan_action(report, entry["id"], target, 50)
                    self.check_plan("proxysql %s" % entry["id"], plan)
        self.assertGreater(self.planned, 0, "no ProxySQL plan was reachable")


class ReadysetPlansTest(ContractCase):
    def test_every_catalog_entry(self):
        from test_readyset import FakeReadyset
        from myflames import readyset
        conn = FakeReadyset()
        report = readyset.read_status(conn)
        sample_values = {
            "create_cache": {"cache_type": "shallow", "query_or_id": "q_3333cccc"},
            "set_replication_position": {"position": "aaaa:1-120"},
            "add_tables": {"tables": ["shop.orders"]},
            "set_log_level": {"directives": "info"},
            "set_eviction_memory_limit": {"bytes": 536870912},
            "set_eviction_period": {"period_ms": 5000},
            "add_shallow_allowed": {"allow_kind": "function", "name": "CURDATE"},
        }
        offered = [(None, sample_values.get(entry["id"]), entry) for entry in report.get("actions") or ()]
        for c in report.get("caches") or ():
            target = {"kind": "cache", "name": c.get("name"), "query_id": c.get("query_id")}
            offered += [(target, None, entry) for entry in c.get("actions") or ()]
        for t in report.get("tables") or ():
            target = {"kind": "table", "table": t.get("table")}
            offered += [(target, None, entry) for entry in t.get("actions") or ()]
        for target, value, entry in offered:
            if not entry.get("enabled"):
                continue
            with self.subTest(action=entry["id"], target=target):
                try:
                    plan = readyset.plan_action(report, entry["id"], target, value)
                except ValueError as exc:
                    self.unreached.append("%s: %s" % (entry["id"], exc))
                    continue
                self.check_plan("readyset %s" % entry["id"], plan)
        self.assertGreater(self.planned, 0, "no Readyset plan was reachable")
        self.assertEqual(self.unreached, [], "enabled Readyset actions raised ValueError: %s" % self.unreached)


class TopologyChangePlanTest(ContractCase):
    def test_rolling_variable_plan(self):
        from test_topology_change import TopologyChangeTests

        case = TopologyChangeTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        plan = case.plan()
        self.check_plan("topology_change", plan)
        self.check("topology_change[members]", plan.get("members") or [])


class LocksKillExpectTest(ContractCase):
    def test_client_echoed_kill_expect(self):
        from test_locks import KillScenario, bound_kill_expect
        scenario = KillScenario()
        expect = bound_kill_expect(scenario.conn)
        self.check("locks kill expect", expect)


class EditorReviewIdentityTest(ContractCase):
    def test_replay_session_binding_values(self):
        from test_editor_session import replay_session
        for name in ("mysql-8.4-client-session", "mariadb-11.4-client-session"):
            with self.subTest(fixture=name):
                session, _, _ = replay_session(name)
                # The editor review digest binds session.connection_id and
                # session.server_identity; both are echoed back by the client.
                self.check("editor %s connection_id" % name, session.connection_id)
                self.check("editor %s server_identity" % name, session.server_identity)


if __name__ == "__main__":
    unittest.main()
