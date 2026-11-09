"""The Query editor's schema browser (myflames/schema_browser.py, /api/live/catalog)."""
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from myflames import schema_browser  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.ui import api_response  # noqa: E402

NS = ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'


def xml(rows):
    """The mysql client's --xml output for *rows* (dicts; None is xsi:nil)."""
    out = ['<?xml version="1.0"?>', "", '<resultset statement="x"%s>' % NS]
    for row in rows:
        out.append("  <row>")
        for name, value in row.items():
            if value is None:
                out.append('\t<field name="%s" xsi:nil="true" />' % name)
            else:
                out.append('\t<field name="%s">%s</field>' % (name, value.replace("&", "&amp;").replace("<", "&lt;")))
        out.append("  </row>")
    out.append("</resultset>")
    return "\n".join(out) + "\n"


class FakeConn(object):
    """Answers information_schema reads by table name; records the SQL."""

    def __init__(self, answers, error=None):
        self.answers = answers
        self.error = error
        self.sql = []
        self.flags = []

    def run(self, sql, extra_flags=None):
        self.sql.append(sql)
        self.flags.append(extra_flags)
        if self.error:
            raise self.error
        for key, rows in self.answers.items():
            if "information_schema.%s " % key in sql:
                return xml(rows)
        return xml([])


TABLES = [{"TABLE_NAME": "orders", "TABLE_TYPE": "BASE TABLE"}, {"TABLE_NAME": "recent", "TABLE_TYPE": "VIEW"},
          {"TABLE_NAME": "s1", "TABLE_TYPE": "SEQUENCE"}]
COLUMNS = [
    {"COLUMN_NAME": "id", "COLUMN_TYPE": "bigint unsigned", "IS_NULLABLE": "NO", "COLUMN_KEY": "PRI",
     "COLUMN_DEFAULT": None, "EXTRA": "auto_increment", "GENERATION_EXPRESSION": ""},
    {"COLUMN_NAME": "note", "COLUMN_TYPE": "varchar(20)", "IS_NULLABLE": "YES", "COLUMN_KEY": "",
     "COLUMN_DEFAULT": "NULL", "EXTRA": "", "GENERATION_EXPRESSION": ""},
    {"COLUMN_NAME": "total", "COLUMN_TYPE": "decimal(10,2)", "IS_NULLABLE": "YES", "COLUMN_KEY": "MUL",
     "COLUMN_DEFAULT": None, "EXTRA": "VIRTUAL GENERATED", "GENERATION_EXPRESSION": "`a` + `b`"},
]
STATISTICS = [
    {"INDEX_NAME": "PRIMARY", "NON_UNIQUE": "0", "SEQ_IN_INDEX": "1", "COLUMN_NAME": "id", "SUB_PART": None,
     "INDEX_TYPE": "BTREE", "EXPRESSION": None},
    {"INDEX_NAME": "k_note", "NON_UNIQUE": "1", "SEQ_IN_INDEX": "1", "COLUMN_NAME": "note", "SUB_PART": "8",
     "INDEX_TYPE": "BTREE", "EXPRESSION": None},
    {"INDEX_NAME": "k_note", "NON_UNIQUE": "1", "SEQ_IN_INDEX": "2", "COLUMN_NAME": None, "SUB_PART": None,
     "INDEX_TYPE": "BTREE", "EXPRESSION": "(`total` * 2)"},
]


class TestCatalogReport(unittest.TestCase):
    def test_tables_of_one_schema_tagged_and_quoted_as_hex(self):
        conn = FakeConn({"TABLES": TABLES})
        report = schema_browser.catalog_report(conn, "shop'; DROP")
        self.assertEqual(report["schema_version"], "catalog-1.0")
        self.assertEqual(report["level"], "tables")
        self.assertEqual([(t["name"], t["type"]) for t in report["tables"]],
                         [("orders", "table"), ("recent", "view"), ("s1", "sequence")])
        self.assertFalse(report["truncated"])
        sql = conn.sql[0]
        self.assertTrue(sql.startswith("/*myflames:editor*/ SELECT"))
        self.assertIn("TABLE_SCHEMA = CONVERT(X'%s' USING utf8mb4)" % "shop'; DROP".encode().hex(), sql)
        self.assertNotIn("DROP", sql)
        self.assertEqual(conn.flags[0], ["--comments", "--xml"])

    def test_stats_add_engine_estimated_rows_and_size_only_when_asked(self):
        rows = [{"TABLE_NAME": "orders", "TABLE_TYPE": "BASE TABLE", "ENGINE": "InnoDB", "TABLE_ROWS": "1200",
                 "DATA_LENGTH": "16384", "INDEX_LENGTH": "4096"},
                {"TABLE_NAME": "recent", "TABLE_TYPE": "VIEW", "ENGINE": None, "TABLE_ROWS": None,
                 "DATA_LENGTH": None, "INDEX_LENGTH": None}]
        conn = FakeConn({"TABLES": rows})
        report = schema_browser.catalog_report(conn, "shop", stats=True)
        self.assertTrue(report["stats"])
        self.assertEqual(report["tables"], [
            {"name": "orders", "type": "table", "engine": "InnoDB", "rows": 1200, "bytes": 20480},
            {"name": "recent", "type": "view", "engine": None, "rows": None, "bytes": None}])
        self.assertIn("ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES", conn.sql[0])
        self.assertIn("LIMIT %d" % (schema_browser.MAX_TABLES + 1), conn.sql[0])
        plain = FakeConn({"TABLES": TABLES})
        self.assertFalse(schema_browser.catalog_report(plain, "shop")["stats"])
        self.assertNotIn("TABLE_ROWS", plain.sql[0])
        self.assertNotIn("engine", schema_browser.catalog_report(FakeConn({"TABLES": TABLES}), "shop")["tables"][0])
        # Only a literal true asks for them.
        self.assertFalse(schema_browser.catalog_report(FakeConn({"TABLES": TABLES}), "shop", stats="yes")["stats"])

    def test_more_tables_than_the_limit_are_cut_and_flagged(self):
        rows = [{"TABLE_NAME": "t%05d" % i, "TABLE_TYPE": "BASE TABLE"} for i in range(6)]
        with mock.patch.object(schema_browser, "MAX_TABLES", 5):
            report = schema_browser.catalog_report(FakeConn({"TABLES": rows}), "big")
        self.assertEqual(len(report["tables"]), 5)
        self.assertTrue(report["truncated"])

    def test_columns_and_indexes_of_one_table(self):
        conn = FakeConn({"COLUMNS": COLUMNS, "STATISTICS": STATISTICS})
        report = schema_browser.catalog_report(conn, "shop", "orders")
        self.assertEqual(report["level"], "table")
        self.assertEqual(report["columns"][0], {"name": "id", "type": "bigint unsigned", "nullable": False,
                                                "key": "PRI", "default": None, "extra": "auto_increment",
                                                "generated": False})
        # The text 'NULL' as a default stays text; a missing default is null.
        self.assertEqual(report["columns"][1]["default"], "NULL")
        self.assertTrue(report["columns"][1]["nullable"])
        self.assertTrue(report["columns"][2]["generated"])
        primary, prefix = report["indexes"]
        self.assertEqual((primary["name"], primary["primary"], primary["unique"]), ("PRIMARY", True, True))
        self.assertEqual(prefix["columns"], [{"name": "note", "expression": None, "sub_part": 8},
                                             {"name": None, "expression": "(`total` * 2)", "sub_part": None}])
        self.assertFalse(prefix["unique"])
        self.assertTrue(all("TABLE_NAME = CONVERT(X'6f7264657273' USING utf8mb4)" in s for s in conn.sql))

    def test_missing_table_and_bad_names(self):
        with self.assertRaisesRegex(ValueError, "No table shop.gone"):
            schema_browser.catalog_report(FakeConn({}), "shop", "gone")
        with self.assertRaisesRegex(ValueError, "Choose a schema"):
            schema_browser.catalog_report(FakeConn({}), "", "orders")
        with self.assertRaisesRegex(ValueError, "table name"):
            schema_browser.catalog_report(FakeConn({}), "shop", "x" * 65)
        with self.assertRaisesRegex(ValueError, "schema name"):
            schema_browser.catalog_report(FakeConn({}), "a\nb")

    def test_server_errors_become_value_errors(self):
        conn = FakeConn({}, ConnectorError("mysql client exited 1: ERROR 1142 (42000): SELECT command denied"))
        with self.assertRaisesRegex(ValueError, "Could not read shop: ERROR 1142"):
            schema_browser.catalog_report(conn, "shop")


class _Conn(object):
    """MySQLConnection stand-in for the endpoint: records the options."""

    def __init__(self):
        self.options = None

    def __call__(self, **options):
        self.options = options
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, sql, extra_flags=None):
        if "information_schema.TABLES" in sql:
            return xml(TABLES[:1])
        raise ConnectorError("mysql client exited 1: Access denied for password Pa55-saved")


class TestCatalogEndpoint(unittest.TestCase):
    PASSWORD = "Pa55-saved"

    def setUp(self):
        from myflames.workspace_store import WorkspaceStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.temp.name, "USERPROFILE": self.temp.name,
                                               "MYFLAMES_SECRET_BACKEND": "vault"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(Path(self.temp.name) / "workspace.sqlite3"))
        saved = api_response("/api/profiles/save", {
            "value": {"name": "shop", "host": "db.internal", "port": 3306, "user": "app"},
            "password": self.PASSWORD, "remember_password": True}, self.workspace)
        self.connection = {"name": "shop", "host": "db.internal", "port": 3306, "user": "app",
                           "profile_id": saved["id"], "password": ""}

    def test_fills_the_saved_password_and_redacts_it(self):
        fake = _Conn()
        with mock.patch("myflames.connector.MySQLConnection", fake):
            report = api_response("/api/live/catalog", {"connection": self.connection, "schema": "shop"},
                                  self.workspace)
            self.assertEqual(report["tables"], [{"name": "orders", "type": "table"}])
            self.assertEqual(fake.options["password"], self.PASSWORD)
            with self.assertRaises(ValueError) as caught:
                api_response("/api/live/catalog", {"connection": self.connection, "schema": "shop",
                                                   "table": "orders"}, self.workspace)
        self.assertNotIn(self.PASSWORD, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))
        json.dumps(report)

    def test_in_the_http_allowlist(self):
        import inspect
        from myflames import ui
        self.assertIn("/api/live/catalog", ui._CONNECTION_PATHS)
        self.assertNotIn("/api/live/catalog", ui._WRITE_PATHS)
        self.assertIn("_CATALOG_PATHS", inspect.getsource(ui.WorkspaceHandler.do_POST))



class TestCatalogPagination(unittest.TestCase):
    def test_paging_validation_precedes_sql_and_keeps_literal_names(self):
        for key in ('prefix', 'after'):
            for value in ('x' * 65, True, 1, [], {}, ' ', 'x\x00y', 'x\ny', 'x\ry'):
                conn = FakeConn({})
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, '1 to 64 characters'):
                    schema_browser.catalog_report(conn, 'shop', **{key: value})
                self.assertEqual(conn.sql, [])
        name = "é'_%" * 16
        conn = FakeConn({})
        result = schema_browser.catalog_report(conn, 'shop', prefix=name, after=name)
        self.assertEqual((result['prefix'], result['after']), (name, name))
        self.assertIn('LEFT(TABLE_NAME, 64) = ' + schema_browser.literal(name), conn.sql[0])
        self.assertIn('BINARY TABLE_NAME > BINARY ' + schema_browser.literal(name), conn.sql[0])
        self.assertNotIn(name, conn.sql[0])

    def test_prefix_keyset_continuation(self):
        # Paging only table names applies to both engines' catalog views.
        names = [{'TABLE_NAME': 't%05d' % i, 'TABLE_TYPE': 'BASE TABLE'} for i in range(201)]
        conn = FakeConn({'TABLES': names})
        first = schema_browser.catalog_report(conn, 'big', prefix='t%_', after='t00000')
        self.assertEqual((len(first['tables']), first['next_after']), (200, 't00199'))
        self.assertTrue(first['truncated'])
        self.assertIn('ORDER BY BINARY TABLE_NAME LIMIT 201', conn.sql[0])
        self.assertIn("LEFT(TABLE_NAME, 3) = CONVERT(X'74255f' USING utf8mb4)", conn.sql[0])
        self.assertIn("BINARY TABLE_NAME > BINARY CONVERT(X'743030303030' USING utf8mb4)", conn.sql[0])
        second = schema_browser.catalog_report(FakeConn({'TABLES': names[-1:]}), 'big', after=first['next_after'])
        self.assertIsNone(second['next_after'])
        self.assertFalse(second['truncated'])
        self.assertEqual(second['tables'][0]['name'], 't00200')


ROUTINES = [
    {"ROUTINE_NAME": "p_top", "ROUTINE_TYPE": "PROCEDURE"},
    {"ROUTINE_NAME": "f_total", "ROUTINE_TYPE": "FUNCTION"},
    # MariaDB packages are out of scope; the SQL filters them, but a stray row is skipped too.
]
TRIGGERS = [
    {"TRIGGER_NAME": "orders_bi", "EVENT_OBJECT_TABLE": "orders", "EVENT_MANIPULATION": "INSERT", "ACTION_TIMING": "BEFORE"},
]
EVENTS = [
    {"EVENT_NAME": "e_nightly", "STATUS": "ENABLED"},
    {"EVENT_NAME": "e_off", "STATUS": "DISABLED"},
]


class TestCatalogObjectGroups(unittest.TestCase):
    """The schema browser's Routines, Triggers and Events folders (catalog-1.0,
    level "objects"): the same columns on MySQL and MariaDB (see the module
    docstring), one schema per read, hex literals, bounded pages."""

    def test_routines_of_one_schema(self):
        conn = FakeConn({"ROUTINES": ROUTINES})
        report = schema_browser.catalog_report(conn, "shop'; DROP", group="routines")
        self.assertEqual(report["schema_version"], "catalog-1.0")
        self.assertEqual(report["level"], "objects")
        self.assertEqual(report["group"], "routines")
        self.assertEqual(report["objects"], [{"name": "p_top", "kind": "procedure"},
                                             {"name": "f_total", "kind": "function"}])
        self.assertFalse(report["truncated"])
        sql = conn.sql[0]
        self.assertTrue(sql.startswith("/*myflames:editor*/ SELECT"))
        self.assertIn("ROUTINE_SCHEMA = CONVERT(X'%s' USING utf8mb4)" % "shop'; DROP".encode().hex(), sql)
        self.assertIn("ROUTINE_TYPE IN ('PROCEDURE', 'FUNCTION')", sql)
        self.assertNotIn("DROP", sql)

    def test_triggers_and_events_of_one_schema(self):
        conn = FakeConn({"TRIGGERS": TRIGGERS, "EVENTS": EVENTS})
        triggers = schema_browser.catalog_report(conn, "shop", group="triggers")
        self.assertEqual(triggers["objects"], [{"name": "orders_bi", "table": "orders",
                                                "event": "insert", "timing": "before"}])
        self.assertIn("FROM information_schema.TRIGGERS", conn.sql[0])
        events = schema_browser.catalog_report(conn, "shop", group="events")
        self.assertEqual(events["objects"], [{"name": "e_nightly", "status": "enabled"},
                                             {"name": "e_off", "status": "disabled"}])
        self.assertIn("FROM information_schema.EVENTS", conn.sql[1])

    def test_group_pages_are_bounded_and_flagged(self):
        rows = [{"ROUTINE_NAME": "p%05d" % i, "ROUTINE_TYPE": "PROCEDURE"} for i in range(6)]
        with mock.patch.object(schema_browser, "MAX_OBJECTS", 5):
            report = schema_browser.catalog_report(FakeConn({"ROUTINES": rows}), "shop", group="routines")
        self.assertEqual(len(report["objects"]), 5)
        self.assertTrue(report["truncated"])
        self.assertEqual(report["limit"], 5)

    def test_an_unknown_group_is_refused_without_sql(self):
        conn = FakeConn({})
        with self.assertRaisesRegex(ValueError, "routines, triggers or events"):
            schema_browser.catalog_report(conn, "shop", group="tables")
        self.assertEqual(conn.sql, [])

    def test_group_errors_become_value_errors(self):
        conn = FakeConn({}, ConnectorError("mysql client exited 1: ERROR 1142 (42000): SELECT command denied"))
        with self.assertRaisesRegex(ValueError, "Could not read shop: ERROR 1142"):
            schema_browser.catalog_report(conn, "shop", group="events")


class _SearchConn(object):
    """Answers the object search's one UNION read; records the SQL."""

    def __init__(self, rows, error=None):
        self.rows = rows
        self.error = error
        self.sql = []

    def run(self, sql, extra_flags=None, timeout=None):
        self.sql.append(sql)
        if self.error:
            raise self.error
        assert "UNION ALL" in sql, sql
        return xml(self.rows)


class TestObjectSearch(unittest.TestCase):
    """The command palette's object search (object-search-1.0): one bounded
    UNION over TABLES, ROUTINES, TRIGGERS and EVENTS with LIKE wildcards
    escaped, the same on both engines."""

    ROWS = [
        {"obj_schema": "shop", "obj_name": "orders", "obj_kind": "BASE TABLE"},
        {"obj_schema": "shop", "obj_name": "orders_recent", "obj_kind": "VIEW"},
        {"obj_schema": "shop", "obj_name": "orders_seq", "obj_kind": "SEQUENCE"},
        {"obj_schema": "shop", "obj_name": "p_orders", "obj_kind": "PROCEDURE"},
        {"obj_schema": "shop", "obj_name": "f_orders", "obj_kind": "FUNCTION"},
        {"obj_schema": "shop", "obj_name": "orders_bi", "obj_kind": "TRIGGER"},
        {"obj_schema": "shop", "obj_name": "e_orders", "obj_kind": "EVENT"},
    ]

    def test_matches_of_every_kind(self):
        report = schema_browser.object_search(_SearchConn(self.ROWS), "ord")
        self.assertEqual(report["schema_version"], "object-search-1.0")
        self.assertEqual(report["text"], "ord")
        self.assertEqual([(o["schema"], o["name"], o["kind"]) for o in report["objects"]],
                         [("shop", "orders", "table"), ("shop", "orders_recent", "view"),
                          ("shop", "orders_seq", "sequence"), ("shop", "p_orders", "procedure"),
                          ("shop", "f_orders", "function"), ("shop", "orders_bi", "trigger"),
                          ("shop", "e_orders", "event")])
        self.assertFalse(report["truncated"])
        self.assertEqual(report["limit"], schema_browser.OBJECT_SEARCH_LIMIT)

    def test_like_wildcards_and_the_escape_char_stay_literal(self):
        conn = _SearchConn([])
        schema_browser.object_search(conn, "a!%_b\\c' OR 1=1")
        sql = conn.sql[0]
        pattern = "CONVERT(X'%s' USING utf8mb4)" % "%a!!!%!_b\\c' OR 1=1%".encode().hex()
        self.assertEqual(sql.count(" LIKE %s ESCAPE '!'" % pattern), 4)
        self.assertIn("LIMIT %d" % (schema_browser.OBJECT_SEARCH_LIMIT + 1), sql)
        self.assertNotIn("OR 1=1", sql)

    def test_search_escape_and_literal_matching_in_both_sql_modes(self):
        """Contract probe, not a live engine test. Decode the generated escape
        token under each engine's two lexer modes, then use SQLite's LIKE as
        an independent wildcard evaluator. The old two-backslash token must
        fail the NO_BACKSLASH_ESCAPES single-character rule.
        """
        for engine, kind in (("mysql", "BASE TABLE"), ("mariadb", "SEQUENCE")):
            for mode in ("", "NO_BACKSLASH_ESCAPES"):
                with self.subTest(engine=engine, sql_mode=mode):
                    conn = _SearchConn([{"obj_schema": "shop", "obj_name": "a!%_b\\c", "obj_kind": kind}])
                    report = schema_browser.object_search(conn, "a!%_b\\c")
                    self.assertEqual(report["objects"][0]["kind"], "table" if engine == "mysql" else "sequence")
                    terms = re.findall(r"LIKE CONVERT\(X'([0-9a-f]+)' USING utf8mb4\) ESCAPE '([^']*)'", conn.sql[0])
                    self.assertEqual(len(terms), 4)
                    for encoded, token in terms:
                        # Both lexers leave backslashes untouched in this mode.
                        escape = token if mode else token.replace("\\\\", "\\")
                        self.assertEqual(len(escape), 1, "ESCAPE must be one character in " + (mode or "ordinary mode"))
                        pattern = bytes.fromhex(encoded).decode("utf-8")
                        db = sqlite3.connect(":memory:")
                        try:
                            for name, matches in (("a!%_b\\c", 1), ("prefix_a!%_b\\c_suffix", 1),
                                                  ("a!XYb\\c", 0), ("a!%Zb\\c", 0), ("a!%_bc", 0)):
                                self.assertEqual(db.execute("SELECT ? LIKE ? ESCAPE ?", (name, pattern, escape)).fetchone()[0], matches, name)
                        finally:
                            db.close()

    def test_more_matches_than_the_limit_are_cut_and_flagged(self):
        rows = [{"obj_schema": "s", "obj_name": "t%05d" % i, "obj_kind": "BASE TABLE"}
                for i in range(schema_browser.OBJECT_SEARCH_LIMIT + 1)]
        report = schema_browser.object_search(_SearchConn(rows), "t0")
        self.assertEqual(len(report["objects"]), schema_browser.OBJECT_SEARCH_LIMIT)
        self.assertTrue(report["truncated"])

    def test_validation_precedes_any_read(self):
        for value in ("x", "x" * 129, " ", "x\ny", "x\x00y", 12, [], {}, None, True):
            conn = _SearchConn([])
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    schema_browser.object_search(conn, value)
            self.assertEqual(conn.sql, [])

    def test_server_errors_become_value_errors(self):
        conn = _SearchConn([], ConnectorError("mysql client exited 1: ERROR 1227 (42000): Access denied"))
        with self.assertRaisesRegex(ValueError, "Could not search objects: ERROR 1227"):
            schema_browser.object_search(conn, "orders")


class _SearchEndpointConn(object):
    """MySQLConnection stand-in for the search endpoint: records the options."""

    def __init__(self):
        self.options = None
        self.allow = True

    def __call__(self, **options):
        self.options = options
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, sql, extra_flags=None, timeout=None):
        if self.allow and "UNION ALL" in sql:
            return xml([{"obj_schema": "shop", "obj_name": "orders", "obj_kind": "BASE TABLE"}])
        raise ConnectorError("mysql client exited 1: Access denied for password Pa55-saved")


class TestObjectSearchEndpoint(unittest.TestCase):
    PASSWORD = "Pa55-saved"

    def setUp(self):
        from myflames.workspace_store import WorkspaceStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.temp.name, "USERPROFILE": self.temp.name,
                                               "MYFLAMES_SECRET_BACKEND": "vault"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(Path(self.temp.name) / "workspace.sqlite3"))
        saved = api_response("/api/profiles/save", {
            "value": {"name": "shop", "host": "db.internal", "port": 3306, "user": "app"},
            "password": self.PASSWORD, "remember_password": True}, self.workspace)
        self.connection = {"name": "shop", "host": "db.internal", "port": 3306, "user": "app",
                           "profile_id": saved["id"], "password": ""}

    def test_fills_the_saved_password_with_a_two_second_budget(self):
        fake = _SearchEndpointConn()
        with mock.patch("myflames.connector.MySQLConnection", fake):
            report = api_response("/api/live/object-search", {"connection": self.connection, "text": "ord"},
                                  self.workspace)
        self.assertEqual(report["objects"], [{"schema": "shop", "name": "orders", "kind": "table"}])
        self.assertEqual(fake.options["password"], self.PASSWORD)
        self.assertEqual(fake.options["query_timeout"], 2)
        json.dumps(report)

    def test_errors_never_carry_the_password(self):
        fake = _SearchEndpointConn()
        fake.allow = False
        with mock.patch("myflames.connector.MySQLConnection", fake):
            with self.assertRaises(ValueError) as caught:
                api_response("/api/live/object-search", {"connection": self.connection, "text": "denied"},
                             self.workspace)
        self.assertNotIn(self.PASSWORD, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))

    def test_validation_precedes_any_connection(self):
        fake = _SearchEndpointConn()
        with mock.patch("myflames.connector.MySQLConnection", fake):
            with self.assertRaisesRegex(ValueError, "2 to"):
                api_response("/api/live/object-search", {"connection": self.connection, "text": "x"},
                             self.workspace)
        self.assertIsNone(fake.options, "a refused search opens no connection")

    def test_in_the_http_allowlist(self):
        import inspect
        from myflames import ui
        self.assertIn("/api/live/object-search", ui._CONNECTION_PATHS)
        self.assertNotIn("/api/live/object-search", ui._WRITE_PATHS)
        self.assertIn("_OBJECT_SEARCH_PATHS", inspect.getsource(ui.WorkspaceHandler.do_POST))


if __name__ == '__main__':
    unittest.main()
