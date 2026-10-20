"""
Unit tests for :mod:`myflames.mcp_server`.

Only the *tool logic* is tested here (it's pure stdlib). The MCP transport
wrapper imports the optional ``mcp`` dependency lazily; we assert that
``build_server`` either succeeds (extra installed) or raises a clear ImportError
(extra absent) — never an obscure failure.
"""
import os
import ast
import contextlib
import inspect
import subprocess
import json
import tempfile
import types
from functools import partial
from unittest import mock
import sys
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

from myflames import mcp_server as mcp
from myflames.parser import OPTIMIZER_SWITCH_EXPLANATIONS

FIXTURE_DIR = os.path.join(TEST_DIR, "fixtures")
PLAN = os.path.join(FIXTURE_DIR, "explain-001-table-scan-users-no-filter.json")
PLAN2 = os.path.join(FIXTURE_DIR, "explain-008-index-scan-users-by-country.json")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestToolLogic(unittest.TestCase):

    def test_analyze_plan_tool_returns_valid_sidecar(self):
        from myflames.output_sidecar import validate_sidecar
        payload = mcp.analyze_plan_tool(_read(PLAN))
        self.assertTrue(validate_sidecar(payload))
        self.assertIn("plan_summary", payload)
        self.assertEqual(payload["source"]["engine"], "mysql")

    def test_explain_query_tool_accepts_only_one_select(self):
        with mock.patch("myflames.connector.MySQLConnection") as connector:
            for query in ("SELECT 1; DELETE FROM t", "UPDATE t SET a = 1", "SELECT 1; SELECT 2"):
                with self.assertRaises(ValueError):
                    mcp.explain_query_tool(query, "127.0.0.1")
            connector.assert_not_called()
            fake = connector.return_value
            fake.__enter__.return_value = fake
            fake.explain_analyze.return_value = _read(PLAN)
            mcp.explain_query_tool("SELECT * FROM users;", "127.0.0.1")
            fake.explain_analyze.assert_called_once_with("SELECT * FROM users")

    def test_explain_query_tool_runs_inside_a_read_only_transaction(self):
        """EXPLAIN ANALYZE runs the statement: like capture, the tool sends
        SET SESSION TRANSACTION READ ONLY, START TRANSACTION READ ONLY, the
        query and ROLLBACK in that order, in one client session."""
        from myflames import connector
        cases = (("mysql", b"8.4.8\n", "EXPLAIN ANALYZE FORMAT=JSON SELECT * FROM users"),
                 ("mariadb", b"11.4.10-MariaDB\n", "ANALYZE FORMAT=JSON SELECT * FROM users"))
        for engine, version, explain in cases:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as home:
                sent = []

                argvs = []

                def fake_run(argv, **kwargs):
                    sql = argv[-1]
                    sent.append(sql)
                    argvs.append(argv)
                    if "@@version_comment" in sql:
                        out = b"MySQL Community Server - GPL\n"
                    elif "@@version" in sql:
                        out = version
                    else:
                        out = _read(PLAN).encode("utf-8")
                    return subprocess.CompletedProcess(argv, 0, out, b"")
                env = {"HOME": home, "USERPROFILE": home, "MYFLAMES_SECRET_BACKEND": "vault"}
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(connector.MySQLConnection, "_resolve_binary", return_value="/bin/mysql"), \
                        mock.patch("myflames.connector._run_bounded", side_effect=fake_run):
                    os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
                    digest = mcp.explain_query_tool("SELECT * FROM users -- note", "127.0.0.1")
                self.assertIn("myflames digest", digest)
                scripts = [sql for sql in sent if "ANALYZE" in sql]
                self.assertEqual(len(scripts), 1, sent)
                script = scripts[0]
                order = ["SET SESSION TRANSACTION READ ONLY", "START TRANSACTION READ ONLY", explain, "ROLLBACK"]
                positions = [script.find(part) for part in order]
                self.assertNotIn(-1, positions, script)
                self.assertEqual(positions, sorted(positions), script)
                # The trailing comment ends at a newline before the terminator.
                self.assertIn("-- note\n;\nROLLBACK", script)
                self.assertTrue(script.rstrip().endswith("ROLLBACK"), script)
                # Without --force the client stops at the first failing
                # statement, so the query never runs outside the transaction.
                self.assertFalse(any("--force" in argv for argv in argvs))

    def test_analyze_plan_tool_default_is_full(self):
        # The internal helper defaults to the complete record — the digest and
        # explain_query paths depend on it. Only the MCP transport wrapper asks
        # for detail="core".
        full = mcp.analyze_plan_tool(_read(PLAN))
        explicit = mcp.analyze_plan_tool(_read(PLAN), detail="full")
        self.assertEqual(set(full), set(explicit))

    def test_analyze_plan_tool_core_is_valid_subset(self):
        from myflames.output_sidecar import validate_sidecar
        full = mcp.analyze_plan_tool(_read(PLAN), detail="full")
        core = mcp.analyze_plan_tool(_read(PLAN), detail="core")
        self.assertTrue(validate_sidecar(core))
        self.assertTrue(set(core).issubset(set(full)))
        for heavy in ("collected", "query", "teach_hooks"):
            self.assertNotIn(heavy, core)

    def test_digest_still_reads_the_full_record(self):
        # digest_plan_tool builds on analyze_plan_tool's full output; the core
        # default on the MCP wrapper must not have starved it.
        digest = mcp.digest_plan_tool(_read(PLAN))
        self.assertIn("myflames digest", digest)

    def test_capture_evidence_matches_cli_digest_warnings(self):
        from myflames.cli import _sidecar_from_input
        from myflames.collectors import parse_show_create_table
        from myflames.digest import build_digest
        ddl = ("CREATE TABLE `users` (\n  `a` int,\n  `b` int,\n"
               "  KEY `k_a` (`a`),\n  KEY `k_ab` (`a`,`b`)\n) ENGINE=InnoDB")
        for engine, version in (('mysql', '8.4.11'), ('mariadb', '11.4.13-MariaDB')):
            capture = {'schema_version': 'capture-1.0', 'raw': _read(PLAN),
                       'engine': engine, 'server_version': version,
                       'schema': {'app.users': parse_show_create_table(ddl)}}
            text = json.dumps(capture)
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, 'capture.json')
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write(text)
                _, expected = _sidecar_from_input(path, quiet=True)
                self.assertTrue(any(w['category'] == 'redundant_index' for w in expected['warnings']))
                for detail in ('core', 'full'):
                    actual = mcp.analyze_plan_tool(text, detail=detail)
                    self.assertEqual(actual['warnings'], expected['warnings'])
                    self.assertEqual(actual['source']['engine'], engine)
                    self.assertEqual(actual['source']['engine_version'], version)
                self.assertEqual(mcp.digest_plan_tool(text), build_digest(expected))

    def test_digest_plan_tool_is_compact_text(self):
        digest = mcp.digest_plan_tool(_read(PLAN))
        self.assertIsInstance(digest, str)
        self.assertIn("myflames digest", digest)

    def test_compare_plans_tool_returns_delta_summary(self):
        sidecar = mcp.compare_plans_tool(_read(PLAN), _read(PLAN2))
        self.assertEqual(sidecar["schema_version"], "compare-1.0")
        self.assertIn("summary", sidecar)

    def test_explain_optimizer_switch_known(self):
        result = mcp.explain_optimizer_switch_tool("hash_join")
        self.assertTrue(result["known"])
        self.assertTrue(result["explanation"])

    def test_explain_optimizer_switch_case_insensitive(self):
        self.assertTrue(mcp.explain_optimizer_switch_tool("HASH_JOIN")["known"])

    def test_explain_optimizer_switch_unknown_lists_available(self):
        result = mcp.explain_optimizer_switch_tool("definitely_not_a_switch")
        self.assertFalse(result["known"])
        self.assertEqual(set(result["available"]), set(OPTIMIZER_SWITCH_EXPLANATIONS))


class TestServerWrapper(unittest.TestCase):

    def test_v1_transport_imports_lazily_from_stub_modules(self):
        with registered_tools() as tools:
            self.assertTrue(set(NEW_TOOLS).issubset(tools))
            self.assertIn('analyze_plan', tools)

    def test_server_identity_uses_myflames_version(self):
        from myflames import __version__
        modules = {name: types.ModuleType(name) for name in
                   ('mcp', 'mcp.server', 'mcp.server.fastmcp')}
        modules['mcp'].__version__ = '1.30.0'
        modules['mcp.server.fastmcp'].FastMCP = FakeMCP
        with mock.patch.dict(sys.modules, modules):
            server = mcp.build_server()
        self.assertEqual(server._mcp_server.name, 'myflames')
        self.assertEqual(server._mcp_server.version, __version__)

    def test_incompatible_transport_entry_point_is_one_line_exit_2(self):
        # Run the real module entry point against a v2-shaped SDK stub so an
        # uncaught exception (including its chained cause) cannot hide in a mock.
        with tempfile.TemporaryDirectory() as directory:
            package = os.path.join(directory, 'mcp')
            os.makedirs(os.path.join(package, 'server'))
            files = {'__init__.py': "__version__ = '2.3.0'\n",
                     'server/__init__.py': '',
                     'server/fastmcp.py':
                         'raise ModuleNotFoundError("FastMCP moved in v2.\\nPin mcp<2 to keep running v1 code.")\n'}
            for name, content in files.items():
                with open(os.path.join(package, name), 'w', encoding='utf-8') as handle:
                    handle.write(content)
            env = dict(os.environ, PYTHONPATH=directory + os.pathsep + os.path.dirname(TEST_DIR))
            result = subprocess.run([sys.executable, '-m', 'myflames.mcp_server'],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    universal_newlines=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn('mcp version: 2.3.0', result.stderr)
        self.assertIn('requires mcp>=1.2,<2', result.stderr)
        self.assertIn("pip install --upgrade 'myflames[mcp]'", result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('code..', result.stderr)

    def test_main_runs_successful_transport(self):
        server = mock.Mock()
        with mock.patch.object(mcp, 'build_server', return_value=server):
            mcp.main()
        server.run.assert_called_once_with()

    def test_missing_or_incompatible_transport_reports_version_and_cause(self):
        for version, cause in ((None, "No module named 'mcp'"),
                               ('2.3.0', "cannot import name 'FastMCP'"),
                               ('1.25.0', "No module named 'transport_dependency'")):
            modules = {'mcp': None} if version is None else {
                name: types.ModuleType(name) for name in ('mcp', 'mcp.server', 'mcp.server.fastmcp')}
            if version is not None:
                modules['mcp'].__version__ = version
            real_import = __import__

            def importing(name, *args, **kwargs):
                if name == 'mcp.server.fastmcp':
                    raise ImportError(cause)
                return real_import(name, *args, **kwargs)

            with self.subTest(version=version), mock.patch.dict(sys.modules, modules), \
                    mock.patch('builtins.__import__', side_effect=importing):
                with self.assertRaises(ImportError) as caught:
                    mcp.build_server()
            message = str(caught.exception)
            self.assertIn(version or 'not installed', message)
            self.assertIn(cause, message)
            self.assertIn('mcp>=1.2,<2', message)
            self.assertIn('myflames[mcp]', message)

    def test_version_diagnostic_has_stdlib_fallback_on_python37(self):
        real_import = __import__

        def importing(name, *args, **kwargs):
            if name == 'importlib.metadata':
                raise ImportError('Python 3.7 has no importlib.metadata')
            return real_import(name, *args, **kwargs)

        with mock.patch.dict(sys.modules, {'mcp': types.ModuleType('mcp')}), \
                mock.patch('builtins.__import__', side_effect=importing):
            self.assertIn('unknown', mcp._mcp_version())

    def test_optional_extra_stays_on_verified_sdk_major(self):
        with open(os.path.join(os.path.dirname(TEST_DIR), 'pyproject.toml'), encoding='utf-8') as f:
            self.assertIn('mcp>=1.2,<2', f.read())

    def test_build_server_succeeds_or_raises_clean_importerror(self):
        try:
            server = mcp.build_server()
        except ImportError as e:
            # Extra not installed: the message must point at the fix.
            self.assertIn("myflames[mcp]", str(e))
        else:
            # Extra installed: we got a server object.
            self.assertIsNotNone(server)


NEW_TOOLS = ("server_report", "top_statements", "sessions", "locks_report", "health_sample",
             "innodb_status", "my_privileges", "replication_status", "cluster_status")


class FakeMCP(object):
    """Exercise registration without importing the optional transport extra."""
    def __init__(self, name):
        self.tools = {}
        self._mcp_server = types.SimpleNamespace(name=name, version=None)

    def tool(self, name=None):
        def register(fn):
            self.tools[name or fn.__name__] = fn
            return fn
        return register


@contextlib.contextmanager
def registered_tools():
    modules = {name: types.ModuleType(name) for name in ("mcp", "mcp.server", "mcp.server.fastmcp")}
    modules["mcp.server.fastmcp"].FastMCP = FakeMCP
    with mock.patch.dict(sys.modules, modules):
        yield mcp.build_server().tools


def fixture_connection(tool, maria=False):
    """Real module readers over existing replay fixtures and module-test fakes."""
    from _perf_replay import FakeTime, replay
    from myflames import report, health
    clock = FakeTime()
    patches = []
    version = "mariadb-11.4" if maria else "mysql-8.4"
    if tool in ("sessions", "innodb_status", "replication_status", "server_report"):
        view = {"innodb_status": "innodb", "replication_status": "replication",
                "server_report": "report"}.get(tool, tool)
        server = version
        if view == "replication":
            server += "-healthy"
        if view == "report" and not maria:
            server += "-replica"
        if view == "replication":
            # The replication tests' loader adds the skip-counter variables the
            # recordings predate; use it so both suites replay the same reads.
            from _perf_replay import ReplayConn
            from test_replication import fixture as replication_fixture
            data = replication_fixture(view, server)
            conn = ReplayConn(data["calls"], clock=clock)
        else:
            conn, data = replay(view, server, clock=clock)
        if view == "report":
            patches.append(mock.patch.object(report, "analysis_report", partial(
                report.analysis_report, workers=1, sleep=clock.sleep, clock=clock.monotonic)))
    elif tool == "cluster_status":
        # Live recordings: MariaDB Galera and MySQL InnoDB Cluster.
        from _cluster_fixtures import recorded
        conn = recorded("galera-11.8-healthy" if maria else "ic-8.4-healthy")[0]
    elif tool == "health_sample":
        from test_health import conn_for
        conn = conn_for(version, clock=clock)
        patches.append(mock.patch.object(health, "health_report", partial(
            health.health_report, sleep=clock.sleep, clock=clock.monotonic)))
    elif tool == "locks_report":
        from test_locks import mysql_conn, ma_conn
        conn = ma_conn() if maria else mysql_conn()
    elif tool == "top_statements":
        from test_discover import FakeConn, server, CONSUMERS_ON, MAX_TEXT, digest_row, batch
        ver = "11.4.13-MariaDB" if maria else "8.4.11"
        row = digest_row("SELECT * FROM `orders` WHERE `status` = ?", no_index=3,
                         with_sample=not maria, sample="SELECT * FROM orders WHERE status = 'new'")
        row.extend([2, 1, 3])
        conn = FakeConn(ver, [server(ver), CONSUMERS_ON, MAX_TEXT,
                             ("WHERE DIGEST IS NULL", ""),
                             ("SHOW GLOBAL STATUS", batch([["Uptime", 600], ["Performance_schema_digest_lost", 0]])),
                             ("events_statements_summary_by_digest", batch([row]))])
    else:
        from test_locks import FakeConn, batch
        ver = "11.4.13-MariaDB" if maria else "8.4.11"
        grant = "GRANT PROCESS ON *.* TO PUBLIC" if maria else "GRANT PROCESS ON *.* TO `app`@`%`"
        conn = FakeConn(ver, [("SELECT 'myflames_who'", batch([
            ["myflames_who", "app@localhost", "app@%", "NONE", ver], [grant],
            ["partial_revokes", "OFF"]])), ("APPLICABLE_ROLES", "")])
    return conn, patches


class TestServerReadTools(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        env = dict(os.environ, HOME=self.home.name, USERPROFILE=self.home.name, MYFLAMES_SECRET_BACKEND="vault")
        env.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def run_fixture(self, tool, maria=False, detail="core", **kwargs):
        from _perf_replay import FakeFactory
        conn, patches = fixture_connection(tool, maria)
        factory = FakeFactory(conn)
        # Modules imported after this patch must still see the real static
        # batch decoder, rather than treating the factory as that class.
        from myflames.connector import MySQLConnection
        factory._unescape_mysql = MySQLConnection._unescape_mysql
        if hasattr(conn, "answers"):
            conn.answers = {mcp._read_sql(sql): answers for sql, answers in conn.answers.items()}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("myflames.connector.MySQLConnection", factory))
            resolver = stack.enter_context(mock.patch("myflames.workspace_cli._resolve_connection", return_value={
                "host": "localhost", "port": 3306, "user": "app", "password": "SAVED-SECRET"}))
            for patch in patches:
                stack.enter_context(patch)
            with registered_tools() as registered:
                result = registered[tool]("fixture", detail=detail, **kwargs)
            args = resolver.call_args[0][0]
            self.assertEqual(args.profile, "fixture")
            self.assertIsNone(args.password)
        self.assertEqual(factory.options["password"], "SAVED-SECRET")
        self.assertTrue(conn.calls)
        # SQL returned in an observation (a current DML statement or suggested
        # KILL) is data. Only the statements actually sent must be read-only.
        from myflames.live import TOKEN
        for sql, flags in conn.calls:
            stripped = TOKEN.sub(" ", sql)
            for statement in stripped.split(";"):
                if statement.strip():
                    self.assertRegex(statement.strip(), r"^(SELECT|SHOW)\b", sql)
                    self.assertNotRegex(statement, r"\b(KILL|SET|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|GRANT|REVOKE|CLONE|CALL)\b")
            self.assertIn("--comments", flags)
        return result

    def test_every_tool_digest_and_full_on_both_engines(self):
        schemas = {"server_report": "report-1.0", "top_statements": "top-1.0", "sessions": "sessions-1.0",
                   "locks_report": "locks-1.1", "health_sample": "health-1.0", "innodb_status": "innodb-1.0",
                   "my_privileges": "privileges-1.0", "replication_status": "replication-1.0",
                   "cluster_status": "cluster-1.0"}
        for tool in NEW_TOOLS:
            for maria in (False, True):
                with self.subTest(tool=tool, maria=maria):
                    core = self.run_fixture(tool, maria)
                    full = self.run_fixture(tool, maria, detail="full")
                    self.assertIsInstance(core, str)
                    self.assertTrue(core.strip())
                    self.assertEqual(full["schema_version"], schemas[tool])
                    self.assertNotIn("SAVED-SECRET", core)
                    self.assertNotIn("SAVED-SECRET", json.dumps(full, allow_nan=False))
                    self.assertLess(len(core), len(json.dumps(full)))
                    if tool == "server_report":
                        from myflames.report import format_digest
                        self.assertEqual(core, format_digest(full))
                    elif tool == "top_statements":
                        self.assertIn(full["statements"][0]["digest_text"], core)
                        self.assertNotIn("sample_text", core)
                        self.assertIn("cumulative", core.lower().replace("accumulate", "cumulative"))
                    elif tool == "my_privileges":
                        self.assertIn(full["session_note"], core)
                        self.assertIn("unknown:", core)

    def test_attribution_orders_and_table_caveats(self):
        for order in ("full_join", "scan", "no_index"):
            for detail in ("core", "full"):
                with self.subTest(order=order, detail=detail):
                    result = self.run_fixture("top_statements", detail=detail, order=order, table="shop.orders")
                    text = result if detail == "core" else json.dumps(result)
                    self.assertIn("table_match", text)
                    self.assertIn("text match", text)
                    self.assertIn("not proof", text)

    def test_lock_scopes_keep_parsed_deadlock_but_omit_raw_text_from_core(self):
        for maria in (False, True):
            for scope in ("waits_only", "deadlock_only"):
                with self.subTest(maria=maria, scope=scope):
                    core = self.run_fixture("locks_report", maria, **{scope: True})
                    full = self.run_fixture("locks_report", maria, detail="full", **{scope: True})
                    self.assertEqual(full["scope"], "waits" if scope == "waits_only" else "deadlock")
                    if scope == "deadlock_only":
                        self.assertTrue(full["deadlock"]["transactions"])
                        self.assertNotIn(full["deadlock"]["raw"], core)
                        self.assertIn("LATEST DETECTED DEADLOCK", core)

    def test_registry_is_exact_and_new_tools_map_only_to_read_helpers(self):
        with registered_tools() as registered:
            self.assertEqual(set(registered), set(NEW_TOOLS) | {
                "analyze_plan", "digest_plan", "compare_plans", "explain_optimizer_switch", "explain_query"})
            expected = {"server_report": "analysis_report", "top_statements": "top_report",
                        "sessions": "sessions_report", "locks_report": "lock_report", "health_sample": "health_report",
                        "innodb_status": "innodb_report", "my_privileges": "read_privileges",
                        "replication_status": "read_status", "cluster_status": "read_status"}
            forbidden = {"kill_session", "kill_cli", "skip_one", "plan_skip", "SkipLoop", "run", "execute",
                         "apply", "dump", "restore", "clone", "set_variable"}
            for name in NEW_TOOLS:
                fn = registered[name]
                self.assertIs(fn, getattr(mcp, name + "_tool"))
                calls = {node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
                         for node in ast.walk(ast.parse(inspect.getsource(fn))) if isinstance(node, ast.Call)
                         and isinstance(node.func, (ast.Attribute, ast.Name))}
                self.assertTrue(expected[name] in calls or expected[name] in inspect.getsource(fn))
                self.assertFalse(calls & forbidden, (name, calls & forbidden))
                self.assertNotIn("password", inspect.signature(fn).parameters)
                self.assertIn("Read-only", fn.__doc__)

    def test_invalid_arguments_refused_before_connection_or_profile_lookup(self):
        bad = {"server_report": ({"interval": 1}, {"interval": True}, {"schemas": "db"}, {"schemas": ["x" * 65]}),
               "top_statements": ({"order": "kill"}, {"order": []}, {"limit": True}, {"limit": 201},
                                  {"schema": ""}, {"table": "orders"}, {"table": {}}),
               "sessions": ({"active_only": 1}, {"min_seconds": float("nan")}, {"min_seconds": -1},
                            {"min_seconds": float("inf")}, {"limit": 10001}),
               "locks_report": ({"waits_only": True, "deadlock_only": True}, {"waits_only": "yes"}, {"limit": 0}),
               "health_sample": ({"interval": 0}, {"interval": 61}, {"interval": 1.5})}
        with mock.patch("myflames.workspace_cli._resolve_connection") as resolve:
            for name in NEW_TOOLS:
                fn = getattr(mcp, name + "_tool")
                for kwargs in ({"detail": "digest"}, {"profile": ""}, {"profile": None}, {"profile": {}},
                               {"profile": "x" * 4097}) + bad.get(name, ()):
                    with self.subTest(tool=name, kwargs=kwargs):
                        values = dict(profile="fixture", **{k: v for k, v in kwargs.items() if k != "profile"})
                        if "profile" in kwargs:
                            values["profile"] = kwargs["profile"]
                        with self.assertRaises(ValueError):
                            fn(**values)
                with self.assertRaises(TypeError):
                    fn("fixture", password="inline-password")
            resolve.assert_not_called()

    def test_read_adapter_refuses_writes_and_converts_transaction_clock(self):
        from _perf_replay import ReplayConn
        conn = ReplayConn([])
        reader = mcp._ReadConnection(conn)
        for sql in ("KILL 1", "SET GLOBAL x=1", "SET SESSION x=1", "CREATE TABLE t(a int)",
                    "SELECT 1; DROP TABLE t", "SHOW GLOBAL STATUS; START REPLICA", "CALL p()", ""):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                reader.run(sql)
        self.assertEqual(conn.calls, [])
        from myflames import sessions, innodb_status
        for tag in (sessions.SESSIONS_TAG, innodb_status.INNODB_TAG):
            sql = tag + sessions.SYSTEM_TIME_ZONE + tag + "SELECT TIMESTAMPDIFF(SECOND, trx_started, NOW())"
            actual = mcp._read_sql(sql)
            self.assertEqual(actual, tag + "SELECT TIMESTAMPDIFF(SECOND, trx_started, "
                             "CONVERT_TZ(UTC_TIMESTAMP(), '+00:00', 'SYSTEM'))")
        self.assertEqual(mcp._read_sql("SELECT 'a; SET x=1'; SHOW GRANTS"), "SELECT 'a; SET x=1'; SHOW GRANTS")

    def test_saved_profile_resolution_and_password_redaction(self):
        from _perf_replay import FakeFactory
        from myflames.connector import ConnectorError
        conn, patches = fixture_connection("my_privileges")
        store = mock.Mock()
        store.find_profile.return_value = {"id": "p1", "name": "Fixture", "host": "localhost", "port": 3306,
                                          "user": "app", "remember_password": True}
        store.connection_with_saved_password.side_effect = lambda settings: dict(settings, password="saved-test-secret")
        with mock.patch("myflames.workspace_store.WorkspaceStore", return_value=store), \
                mock.patch("myflames.connector.MySQLConnection", FakeFactory(conn)):
            result = mcp.my_privileges_tool("Fixture", detail="full")
            store.find_profile.assert_called_once_with("Fixture")
            self.assertEqual(store.connection_with_saved_password.call_args[0][0]["profile_id"], "p1")
            self.assertEqual(result["account"], "app@%")
            with mock.patch.object(conn, "run", side_effect=ConnectorError("saved-test-secret failed")):
                with self.assertRaises(ValueError) as caught:
                    mcp.my_privileges_tool("Fixture")
            self.assertNotIn("saved-test-secret", str(caught.exception))
            self.assertIn("[redacted]", str(caught.exception))
        for detail in ("core", "full"):
            with mock.patch("myflames.workspace_cli._resolve_connection", return_value={"password": "saved-test-secret"}), \
                    mock.patch("myflames.connector.MySQLConnection", FakeFactory(conn)), \
                    mock.patch("myflames.sessions.sessions_report", return_value={"notes": ["saved-test-secret"]}), \
                    mock.patch("myflames.sessions.format_text", return_value="saved-test-secret"):
                self.assertNotIn("saved-test-secret", str(mcp.sessions_tool("Fixture", detail=detail)))


if __name__ == "__main__":
    unittest.main()
