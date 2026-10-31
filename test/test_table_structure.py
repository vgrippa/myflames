"""Table structure (Server > Tables): schema_browser.table_structure and
/api/live/table-structure (table-structure-1.0).

``test/fixtures/schema_export/structure-<server>.json`` holds every client
call one live read sent and what the client printed (--xml), recorded on
2026-10-31 from throwaway MySQL 8.4.13, MariaDB 11.4.13 and MySQL 5.7.44
containers (database xp_src of test_schema_export.py). No server is contacted.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from myflames import schema_browser, ui  # noqa: E402
from myflames.connector import ConnectorError  # noqa: E402
from myflames.ui import api_response  # noqa: E402
from test_chart_data import validate  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "schema_export")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "schemas", "table-structure-1.0.json"),
          encoding="utf-8") as _handle:
    CONTRACT = json.load(_handle)
SERVERS = ("mysql-8.4", "mariadb-11.4", "mysql-5.7")


def recorded(server):
    with open(os.path.join(FIXTURES, "structure-%s.json" % server), encoding="utf-8") as handle:
        return json.load(handle)


class Replay(object):
    """Answers the recorded statements; anything else is a ConnectorError."""

    def __init__(self, calls, fail=None):
        self.calls = calls
        self.fail = fail or {}
        self.sent = []

    def run(self, sql, extra_flags=None, timeout=None):
        self.sent.append(sql)
        for marker, error in self.fail.items():
            if marker in sql:
                raise ConnectorError(error)
        for call in self.calls:
            if call["sql"] == sql:
                return call["stdout"]
        raise ConnectorError("unexpected SQL: %s" % sql[:200])


def read(server, table, **fail):
    conn = Replay(recorded(server)[table]["calls"], fail)
    return schema_browser.table_structure(conn, "xp_src", table), conn


class TestTableStructure(unittest.TestCase):
    def test_every_recorded_read_matches_the_contract(self):
        for server in SERVERS:
            for table, record in recorded(server).items():
                with self.subTest(server=server, table=table):
                    if "error" in record:
                        # Recorded for a table this server does not have.
                        self.assertIn("No table xp_src.%s" % table, record["error"])
                        with self.assertRaisesRegex(ValueError, "No table"):
                            read(server, table)
                        continue
                    validate(read(server, table)[0], CONTRACT)
        report = read("mysql-8.4", "customers")[0]
        report["extra"] = 1
        with self.assertRaises(AssertionError):
            validate(report, CONTRACT)

    def test_every_engine_reads_columns_indexes_keys_and_the_create_table(self):
        for server in SERVERS:
            with self.subTest(server=server):
                report, conn = read(server, "customers")
                self.assertEqual(report["schema_version"], "table-structure-1.0")
                self.assertEqual((report["schema"], report["table"], report["type"], report["engine"]),
                                 ("xp_src", "customers", "table", "InnoDB"))
                self.assertEqual(report["filename"], "xp_src.customers.sql")
                self.assertEqual(report["comment"], "Customer accounts; ünïcode")
                names = [c["name"] for c in report["columns"]]
                self.assertEqual(names, ["id", "email", "name", "referrer_id", "created"])
                email = report["columns"][1]
                self.assertEqual((email["comment"], email["character_set"], email["collation"], email["nullable"]),
                                 ("login ação", "utf8mb4", "utf8mb4_bin", False))
                name = report["columns"][2]
                self.assertEqual((name["character_set"], name["collation"]), ("latin1", "latin1_swedish_ci"))
                self.assertEqual(report["columns"][0]["extra"], "auto_increment")
                self.assertEqual([x["name"] for x in report["indexes"]], ["PRIMARY", "k_ref", "uq_email"])
                self.assertTrue(report["indexes"][0]["primary"] and report["indexes"][0]["unique"])
                # Recorded: MySQL 8.4 reports an unspecified rule as NO ACTION, 5.7.44 and MariaDB 11.4 as RESTRICT.
                self.assertEqual(report["foreign_keys"], [{
                    "name": "fk_referrer", "columns": ["referrer_id"], "referenced_schema": "xp_src",
                    "referenced_table": "customers", "referenced_columns": ["id"],
                    "on_update": "NO ACTION" if server == "mysql-8.4" else "RESTRICT", "on_delete": "SET NULL"}])
                self.assertTrue(report["create_table"].startswith("CREATE TABLE `customers` (\n"))
                self.assertIsNone(report["create_error"])
                # Read-only: information_schema and SHOW CREATE TABLE, tagged.
                for sql in conn.sent:
                    self.assertTrue(sql.startswith(schema_browser.TAG))
                    self.assertRegex(sql[len(schema_browser.TAG):], r"^(SELECT \* FROM information_schema\.|SHOW CREATE TABLE )")

    def test_columns_each_server_lacks_are_unknown(self):
        mysql, _ = read("mysql-8.4", "checks")
        functional = next(x for x in mysql["indexes"] if x["name"] == "k_fn")
        self.assertEqual(functional["columns"], [{"name": None, "expression": "(`qty` * 2)", "sub_part": None,
                                                  "order": "asc"}])
        self.assertFalse(next(x for x in mysql["indexes"] if x["name"] == "k_inv")["visible"])
        maria, _ = read("mariadb-11.4", "ticket")
        self.assertFalse(next(x for x in maria["indexes"] if x["name"] == "k_qty")["visible"], "IGNORED")
        self.assertEqual(maria["columns"][0]["default"], "nextval(`xp_src`.`seq_ids`)")
        old, _ = read("mysql-5.7", "js")
        # 5.7: no IS_VISIBLE and no EXPRESSION column.
        self.assertTrue(all(x["visible"] is None for x in old["indexes"]))
        self.assertEqual([x["type"] for x in old["indexes"]], ["BTREE", "SPATIAL"])
        self.assertTrue(all(c["expression"] is None for x in old["indexes"] for c in x["columns"]))

    def test_foreign_keys_with_rules(self):
        for server in SERVERS:
            with self.subTest(server=server):
                report, _ = read(server, "a_lines")
                self.assertEqual(report["foreign_keys"][0]["on_delete"], "CASCADE")
                self.assertEqual(report["foreign_keys"][0]["referenced_table"], "orders")

    def test_a_view(self):
        report, _ = read("mysql-5.7", "v_orders")
        self.assertEqual((report["type"], report["engine"], report["comment"]), ("view", None, ""))
        self.assertTrue(report["create_table"].startswith("CREATE ALGORITHM="))
        self.assertEqual(report["indexes"], [])

    def test_a_refused_show_create_keeps_the_rest(self):
        report, _ = read("mysql-8.4", "customers", **{"SHOW CREATE TABLE": "ERROR 1142 (42000) at line 1: SHOW command "
                                                                           "denied to user 'u'@'%' for table 'customers'"})
        self.assertIsNone(report["create_table"])
        self.assertTrue(report["create_error"].startswith("SHOW CREATE TABLE failed: "))
        self.assertIn("SHOW command denied to user 'u'@'%' for table 'customers'", report["create_error"])
        self.assertEqual(len(report["columns"]), 5)
        with self.assertRaisesRegex(ValueError, "Could not read xp_src.customers"):
            read("mysql-8.4", "customers", **{"SHOW CREATE TABLE": "ERROR 2013 (HY000): Lost connection"})

    def test_a_missing_table_and_bad_names(self):
        conn = Replay([{"sql": schema_browser.TAG + "x", "stdout": ""}])
        conn.run = lambda sql, extra_flags=None, timeout=None: ""
        with self.assertRaisesRegex(ValueError, "No table xp_src.nope, or no privilege to see it."):
            schema_browser.table_structure(conn, "xp_src", "nope")
        with self.assertRaisesRegex(ValueError, "Choose a table."):
            schema_browser.table_structure(conn, "xp_src", "")
        with self.assertRaisesRegex(ValueError, "1 to 64 characters"):
            schema_browser.table_structure(conn, "xp_src", "x" * 65)
        self.assertEqual(schema_browser.structure_filename("a/b", 'c:"d'), "a_b.c__d.sql")


class TestEndpoint(unittest.TestCase):
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
            "value": {"name": "lab", "host": "db.internal", "port": 3306, "user": "app"},
            "password": self.PASSWORD, "remember_password": True}, self.workspace)
        self.connection = {"name": "lab", "host": "db.internal", "port": 3306, "user": "app",
                           "profile_id": saved["id"], "password": ""}

    def test_the_endpoint_fills_the_saved_password_and_redacts_it(self):
        self.assertIn("/api/live/table-structure", ui._CATALOG_PATHS)
        self.assertIn("/api/live/table-structure", ui._CONNECTION_PATHS)
        self.assertNotIn("/api/live/table-structure", ui._WRITE_PATHS)
        calls = recorded("mysql-8.4")["customers"]["calls"]
        seen = {}

        class Conn(Replay):
            def __init__(self, **options):
                Replay.__init__(self, calls)
                seen.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False
        with mock.patch("myflames.connector.MySQLConnection", Conn):
            report = api_response("/api/live/table-structure", {"connection": self.connection, "schema": "xp_src",
                                                                "table": "customers"}, self.workspace)
        self.assertEqual(report["table"], "customers")
        self.assertEqual(seen["password"], self.PASSWORD)
        json.dumps(report)
        password = self.PASSWORD

        class Broken(Conn):
            def run(self, sql, extra_flags=None, timeout=None):
                raise ConnectorError("mysql client exited 1: Access denied for password %s" % password)
        with mock.patch("myflames.connector.MySQLConnection", Broken):
            with self.assertRaises(ValueError) as caught:
                api_response("/api/live/table-structure", {"connection": self.connection, "schema": "xp_src",
                                                           "table": "customers"}, self.workspace)
        self.assertNotIn(self.PASSWORD, str(caught.exception))
        self.assertIn("[redacted]", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
