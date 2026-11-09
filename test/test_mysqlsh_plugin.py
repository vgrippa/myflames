"""Tests for the MySQL Shell plugin in contrib/mysqlsh-plugin/myflames/init.py.

The plugin runs inside mysqlsh, so these tests load init.py with stub
``mysqlsh`` / ``mysqlsh.plugin_manager`` modules and a fake session whose
``run_sql`` returns canned results.
"""

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PLUGIN_PATH = os.path.join(REPO, "contrib", "mysqlsh-plugin", "myflames", "init.py")
PLAN_PATH = os.path.join(HERE, "mysql-explain-hash-join.json")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

with open(PLAN_PATH, encoding="utf-8") as _fh:
    PLAN_JSON = _fh.read()

GUARD_SQL = ("SELECT COUNT(*) FROM performance_schema.events_transactions_current "
             "WHERE THREAD_ID = PS_CURRENT_THREAD_ID() AND STATE = 'ACTIVE' "
             "AND AUTOCOMMIT = 'NO'")
FORMAT_SQL = "SELECT @@SESSION.explain_json_format_version"
QUERY = "SELECT u.name, o.total FROM users u JOIN orders o ON o.user_id = u.id"


class StubShellError(Exception):
    """Mirrors mysqlsh.Error(msg)."""


class FakeResult(object):
    def __init__(self, rows):
        self._rows = list(rows)

    def fetch_one(self):
        return self._rows.pop(0) if self._rows else None


class FakeSession(object):
    def __init__(self, version="8.4.3", format_version=1, active_trx=0,
                 plan=PLAN_JSON, explain_error=None, guard_error=False):
        self.version = version
        self.format_version = format_version
        self.active_trx = active_trx
        self.plan = plan
        self.explain_error = explain_error
        self.guard_error = guard_error
        self.statements = []

    def is_open(self):
        return True

    def run_sql(self, sql):
        self.statements.append(sql)
        if sql == "SELECT VERSION()":
            return FakeResult([(self.version,)])
        if sql == GUARD_SQL:
            if self.guard_error:
                raise RuntimeError("performance_schema disabled")
            return FakeResult([(self.active_trx,)])
        if sql == FORMAT_SQL:
            return FakeResult([(self.format_version,)])
        if sql.startswith("EXPLAIN"):
            if self.explain_error:
                raise RuntimeError(self.explain_error)
            return FakeResult([(self.plan,)])
        return FakeResult([])


class FakeShell(object):
    version = "Ver 9.4.0 for macos"

    def __init__(self, session=None, sql_handlers=True):
        self.session = session
        self.registered_handlers = []
        self.created_results = []
        if sql_handlers:
            self.register_sql_handler = self._register_sql_handler

    def get_session(self):
        return self.session

    def _register_sql_handler(self, name, description, prefixes, callback):
        self.registered_handlers.append((name, description, prefixes, callback))

    def create_result(self, data):
        self.created_results.append(data)
        return {"result": data}


def load_plugin(shell):
    """Execute init.py against stub mysqlsh modules; return (module, registry)."""
    registry = {"objects": [], "functions": []}

    def plugin(cls=None, **_kwargs):
        if cls is None:
            return lambda c: plugin(c)
        registry["objects"].append(cls.__name__)
        return cls

    def plugin_function(name, plugin_docs=None, shell=True, cli=False, web=False):
        def decorator(function):
            registry["functions"].append({"name": name, "cli": cli, "function": function})
            return function
        return decorator

    mysqlsh_mod = types.ModuleType("mysqlsh")
    mysqlsh_mod.Error = StubShellError
    mysqlsh_mod.globals = types.SimpleNamespace(shell=shell)
    pm_mod = types.ModuleType("mysqlsh.plugin_manager")
    pm_mod.plugin = plugin
    pm_mod.plugin_function = plugin_function
    mysqlsh_mod.plugin_manager = pm_mod

    with mock.patch.dict(sys.modules, {"mysqlsh": mysqlsh_mod,
                                       "mysqlsh.plugin_manager": pm_mod}):
        spec = importlib.util.spec_from_file_location("myflames_mysqlsh_plugin_under_test",
                                                      PLUGIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, registry


class PluginTestCase(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.shell = FakeShell(self.session)
        self.plugin, self.registry = load_plugin(self.shell)
        self.tmp = tempfile.mkdtemp(prefix="myflames-mysqlsh-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(self.plugin.webbrowser, "open")
        self.browser = patcher.start()
        self.addCleanup(patcher.stop)

    def run_quiet(self, fn, *args, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = fn(*args, **kwargs)
        return result, out.getvalue()


class RegistrationTest(PluginTestCase):
    def test_registers_global_object_and_functions(self):
        self.assertEqual(self.registry["objects"], ["myflames"])
        names = {f["name"]: f["cli"] for f in self.registry["functions"]}
        self.assertEqual(names, {
            "myflames.explain": True,
            "myflames.digest": True,
            "myflames.advise": True,
            "myflames.enableSqlHandler": False,
            "myflames.disableSqlHandler": False,
        })

    def test_docstrings_follow_shell_plugin_format(self):
        # The Shell registrar treats any docstring line ending in ':' as a
        # section header and rejects empty sections; only these are valid.
        documented = [self.plugin.myflames] + [f["function"] for f in self.registry["functions"]]
        for obj in documented:
            for line in (obj.__doc__ or "").splitlines():
                if line.rstrip().endswith(":"):
                    self.assertIn(line.strip(), ("Args:", "Keyword Args:"), obj)
        for entry in self.registry["functions"]:
            doc = entry["function"].__doc__
            if "**options" in doc:
                self.assertIn("Keyword Args:", doc)

    def test_plugin_folder_has_no_init_package_file(self):
        # A regular package named myflames in ~/.mysqlsh/plugins would shadow
        # the real myflames package while Shell loads the plugin.
        folder = os.path.dirname(PLUGIN_PATH)
        self.assertFalse(os.path.exists(os.path.join(folder, "__init__.py")))

    def test_inspiration_credit_present(self):
        with open(PLUGIN_PATH, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Inspired by Brendan Gregg's FlameGraph and Tanel Poder's "
                      "SQL Plan FlameGraphs", text)
        readme = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_PATH)), "README.md")
        with open(readme, encoding="utf-8") as fh:
            self.assertIn("Inspired by Brendan Gregg's FlameGraph and Tanel Poder's "
                          "SQL Plan FlameGraphs", fh.read())


class SqlSequenceTest(PluginTestCase):
    def test_analyze_sequence_sets_and_restores_format_version(self):
        out = os.path.join(self.tmp, "plan.html")
        self.run_quiet(self.plugin.run_explain, self.session, QUERY + ";",
                       {"output": out, "open": False})
        self.assertEqual(self.session.statements, [
            "SELECT VERSION()",
            GUARD_SQL,
            FORMAT_SQL,
            "SET SESSION explain_json_format_version = 2",
            "START TRANSACTION READ ONLY",
            "EXPLAIN ANALYZE FORMAT=JSON " + QUERY,
            "ROLLBACK",
            "SET SESSION explain_json_format_version = 1",
        ])

    def test_no_format_change_when_already_version_2(self):
        self.session.format_version = 2
        self.run_quiet(self.plugin.run_explain, self.session, QUERY,
                       {"output": os.path.join(self.tmp, "p.html"), "open": False})
        self.assertNotIn("SET SESSION explain_json_format_version = 2",
                         self.session.statements)
        self.assertEqual(self.session.statements[-1], "ROLLBACK")

    def test_estimated_plan_skips_transaction(self):
        self.run_quiet(self.plugin.run_explain, self.session, QUERY,
                       {"analyze": False, "output": os.path.join(self.tmp, "p.html"),
                        "open": False})
        self.assertEqual(self.session.statements, [
            "SELECT VERSION()",
            FORMAT_SQL,
            "SET SESSION explain_json_format_version = 2",
            "EXPLAIN FORMAT=JSON " + QUERY,
            "SET SESSION explain_json_format_version = 1",
        ])

    def test_failure_still_rolls_back_and_restores(self):
        self.session.explain_error = "ERROR 1146: Table doesn't exist"
        with self.assertRaises(RuntimeError):
            self.plugin.capture_plan(self.session, QUERY, True)
        self.assertEqual(self.session.statements[-2:], [
            "ROLLBACK", "SET SESSION explain_json_format_version = 1"])

    def test_open_transaction_is_refused(self):
        self.session.active_trx = 1
        with self.assertRaises(StubShellError) as ctx:
            self.plugin.capture_plan(self.session, QUERY, True)
        self.assertIn("open transaction", str(ctx.exception))
        self.assertNotIn("START TRANSACTION READ ONLY", self.session.statements)

    def test_guard_failure_is_best_effort(self):
        self.session.guard_error = True
        captured = self.plugin.capture_plan(self.session, QUERY, True)
        self.assertEqual(captured["raw"], PLAN_JSON)
        self.assertIn("START TRANSACTION READ ONLY", self.session.statements)

    def test_uses_global_shell_session_by_default(self):
        _, printed = self.run_quiet(self.registry_function("myflames.explain"), QUERY,
                                    output=os.path.join(self.tmp, "g.html"), open=False)
        self.assertIn("myflames: wrote", printed)
        self.assertIn("EXPLAIN ANALYZE FORMAT=JSON " + QUERY, self.session.statements)

    def test_no_session_is_a_clear_error(self):
        self.shell.session = None
        with self.assertRaises(StubShellError) as ctx:
            self.plugin.run_explain(None, QUERY, {"open": False})
        self.assertIn("Connect MySQL Shell", str(ctx.exception))

    def registry_function(self, name):
        for entry in self.registry["functions"]:
            if entry["name"] == name:
                return entry["function"]
        raise KeyError(name)


class StatementGuardTest(PluginTestCase):
    def assert_refused(self, sql, analyze=True, fragment=None):
        with self.assertRaises(StubShellError) as ctx:
            self.plugin.capture_plan(self.session, sql, analyze)
        self.assertEqual(self.session.statements, [])
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_dml_refused_for_analyze(self):
        for sql in ("UPDATE t SET a = 1",
                    "DELETE FROM t",
                    "insert into t values (1)",
                    "REPLACE INTO t VALUES (1)",
                    "WITH c AS (SELECT 1) DELETE FROM t",
                    "WITH RECURSIVE c (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) "
                    "UPDATE t SET a = 1",
                    "/* hint */ UPDATE t SET a = 1"):
            self.assert_refused(sql, fragment="EXPLAIN ANALYZE executes the statement")

    def test_other_refusals(self):
        self.assert_refused("SELECT 1 INTO OUTFILE '/tmp/x'", fragment="OUTFILE")
        self.assert_refused("EXPLAIN SELECT 1", fragment="without EXPLAIN")
        self.assert_refused("SELECT 1; DROP TABLE t", fragment="single statement")
        self.assert_refused("DROP TABLE t", analyze=False)
        self.assert_refused("   ", fragment="Pass the SQL statement")

    def test_read_statements_accepted(self):
        for sql in ("SELECT 'DELETE FROM t' AS s FROM t",
                    "select `update` from t -- UPDATE\n",
                    "WITH c AS (SELECT 1 AS x) SELECT * FROM c",
                    "WITH RECURSIVE c (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c "
                    "WHERE n < 3) SELECT * FROM c",
                    "(SELECT 1) UNION (SELECT 2)",
                    "TABLE t",
                    "VALUES ROW(1, 2)",
                    "SELECT * FROM t FOR UPDATE",
                    "SELECT 1\\G"):
            self.assertTrue(self.plugin.check_statement(sql, True))

    def test_estimated_plan_allows_dml(self):
        self.assertEqual(self.plugin.check_statement("UPDATE t SET a = 1", False),
                         "UPDATE t SET a = 1")

    def test_unknown_option_and_bad_type(self):
        with self.assertRaises(StubShellError):
            self.plugin.run_explain(self.session, QUERY, {"colour": "hot"})
        with self.assertRaises(StubShellError):
            self.plugin.run_explain(self.session, QUERY, {"type": "treemap"})
        self.assertEqual(self.session.statements, [])


class EngineGuardTest(PluginTestCase):
    def test_mariadb_refused(self):
        self.session.version = "10.11.6-MariaDB-1:10.11.6+maria~ubu2204"
        with self.assertRaises(StubShellError) as ctx:
            self.plugin.capture_plan(self.session, QUERY, True)
        self.assertIn("MariaDB", str(ctx.exception))
        self.assertIn("MySQL only", str(ctx.exception))
        self.assertEqual(self.session.statements, ["SELECT VERSION()"])

    def test_old_mysql_refused(self):
        self.session.version = "8.0.36"
        with self.assertRaises(StubShellError) as ctx:
            self.plugin.capture_plan(self.session, QUERY, True)
        self.assertIn("8.3", str(ctx.exception))
        self.assertEqual(self.session.statements, ["SELECT VERSION()"])


class OutputTest(PluginTestCase):
    def test_html_report_written_and_opened(self):
        out = os.path.join(self.tmp, "sub", "report.html")
        path, printed = self.run_quiet(self.plugin.run_explain, self.session, QUERY,
                                       {"output": out})
        self.assertEqual(path, out)
        with open(out, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn('class="mfc-mount"', html)
        self.assertIn("MyflamesCharts.mountAll", html)
        self.assertIn("myflames: wrote " + out, printed)
        self.browser.assert_called_once()
        self.assertTrue(self.browser.call_args[0][0].startswith("file://"))

    def test_svg_output_and_view_type(self):
        out = os.path.join(self.tmp, "plan.svg")
        self.run_quiet(self.plugin.run_explain, self.session, QUERY,
                       {"output": out, "type": "bargraph", "open": False})
        with open(out, encoding="utf-8") as fh:
            svg = fh.read()
        self.assertTrue(svg.lstrip().startswith("<?xml") or svg.lstrip().startswith("<svg"))
        self.browser.assert_not_called()

    def test_default_output_is_temp_html(self):
        path, _ = self.run_quiet(self.plugin.run_explain, self.session, QUERY,
                                 {"open": "false"})
        self.addCleanup(os.remove, path)
        self.assertTrue(path.endswith(".html"))
        self.assertTrue(os.path.basename(path).startswith("myflames-"))
        self.assertGreater(os.path.getsize(path), 1000)

    def test_digest_printed(self):
        text, printed = self.run_quiet(self.plugin.run_digest, self.session, QUERY)
        self.assertTrue(text.strip())
        self.assertIn(text.strip(), printed)
        self.assertIn("users", printed)
        self.assertIn("EXPLAIN ANALYZE FORMAT=JSON " + QUERY, self.session.statements)

    def test_advise_printed(self):
        findings, printed = self.run_quiet(self.plugin.run_advise, self.session, QUERY)
        self.assertIsInstance(findings, list)
        self.assertTrue(findings)
        first = findings[0]
        self.assertTrue(printed.startswith("[{}/{}/{}]".format(
            first["severity"], first["category"], first["confidence"])))

    def test_format_findings_empty(self):
        self.assertEqual(self.plugin.format_findings([]),
                         "No findings: the plan looks clean.")


class SqlHandlerTest(PluginTestCase):
    def test_prefix_matching(self):
        match = self.plugin.match_handler_sql
        self.assertEqual(match("EXPLAIN ANALYZE FORMAT=MYFLAMES SELECT 1"), (True, "SELECT 1"))
        self.assertEqual(match("  explain   analyze format = myflames\nSELECT 1;"),
                         (True, "SELECT 1;"))
        self.assertEqual(match("/* c */ EXPLAIN FORMAT=MyFlames SELECT 1"), (False, "SELECT 1"))
        self.assertIsNone(match("EXPLAIN ANALYZE FORMAT=JSON SELECT 1"))
        self.assertIsNone(match("EXPLAIN FORMAT=TREE SELECT 1"))
        self.assertIsNone(match("EXPLAIN ANALYZE FORMAT=MYFLAMESX SELECT 1"))
        self.assertIsNone(match("SELECT 1"))

    def test_registered_prefixes_never_cover_normal_explain(self):
        prefixes = self.plugin.SQL_HANDLER_PREFIXES
        self.assertIn("EXPLAIN ANALYZE FORMAT=MYFLAMES", prefixes)
        self.assertIn("EXPLAIN FORMAT = MYFLAMES", prefixes)
        self.assertEqual(len(prefixes), len(set(prefixes)))
        for prefix in prefixes:
            self.assertTrue(prefix.endswith("MYFLAMES"))
            self.assertIsNotNone(self.plugin.match_handler_sql(prefix + " SELECT 1"))

    def test_enable_registers_and_handles(self):
        _, printed = self.run_quiet(self.plugin.enable_handler, {"open": False})
        self.assertIn("enabled", printed)
        self.assertEqual(len(self.shell.registered_handlers), 1)
        name, description, prefixes, callback = self.shell.registered_handlers[0]
        self.assertEqual(name, "myflames")
        self.assertTrue(description)
        self.assertEqual(prefixes, self.plugin.SQL_HANDLER_PREFIXES)

        result, _ = self.run_quiet(callback, self.session,
                                   "EXPLAIN ANALYZE FORMAT=MYFLAMES " + QUERY)
        data = self.shell.created_results[-1]
        self.assertEqual(result, {"result": data})
        path = data["data"][0]["myflames_output"]
        self.addCleanup(os.remove, path)
        self.assertTrue(os.path.exists(path))
        self.assertIn("EXPLAIN ANALYZE FORMAT=JSON " + QUERY, self.session.statements)
        self.browser.assert_not_called()

        # Enabling twice must not register a second (duplicate-name) handler.
        self.run_quiet(self.plugin.enable_handler)
        self.assertEqual(len(self.shell.registered_handlers), 1)

    def test_handler_dml_refused(self):
        self.run_quiet(self.plugin.enable_handler)
        callback = self.shell.registered_handlers[0][3]
        with self.assertRaises(StubShellError):
            callback(self.session, "EXPLAIN ANALYZE FORMAT=MYFLAMES DELETE FROM t")
        self.assertEqual(self.session.statements, [])

    def test_disable_makes_handler_refuse(self):
        self.run_quiet(self.plugin.enable_handler)
        callback = self.shell.registered_handlers[0][3]
        self.run_quiet(self.plugin.disable_handler)
        with self.assertRaises(StubShellError) as ctx:
            callback(self.session, "EXPLAIN ANALYZE FORMAT=MYFLAMES SELECT 1")
        self.assertIn("disabled", str(ctx.exception))
        self.assertEqual(self.session.statements, [])

    def test_old_shell_without_sql_handlers(self):
        shell = FakeShell(self.session, sql_handlers=False)
        plugin, _ = load_plugin(shell)
        ok, printed = self.run_quiet(plugin.enable_handler)
        self.assertFalse(ok)
        self.assertIn("9.0.0", printed)


class MissingPackageTest(PluginTestCase):
    def test_missing_myflames_names_install_command(self):
        with mock.patch.dict(sys.modules, {"myflames.parser": None}):
            with self.assertRaises(StubShellError) as ctx:
                self.plugin.run_explain(self.session, QUERY, {"open": False})
        self.assertIn("mysqlsh --pym pip install --user myflames", str(ctx.exception))
        self.assertEqual(self.session.statements, [])

    def test_namespace_shadow_is_purged(self):
        shadow = types.ModuleType("myflames")
        shadow.__path__ = []
        with mock.patch.dict(sys.modules, {"myflames": shadow}):
            api = self.plugin._load_api()
            self.assertTrue(getattr(sys.modules["myflames"], "__file__", None))
        self.assertTrue(hasattr(api["report"], "render_html_report"))


if __name__ == "__main__":
    unittest.main()
