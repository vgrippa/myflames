"""The Query editor's schema browser (myflames/schema_browser.py, /api/live/catalog)."""
import json
import os
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


if __name__ == '__main__':
    unittest.main()
