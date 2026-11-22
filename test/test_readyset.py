"""Unit, schema, action-safety, CLI and HTTP tests for :mod:`myflames.readyset`.

Runs entirely with a temporary HOME and ``MYFLAMES_SECRET_BACKEND=vault`` so
developer profiles and the OS keychain are never touched.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape as xml_escape

from _perf_replay import run_cli
from myflames import dbops, readyset, reviewed_writes, topology_links, ui
from myflames.connector import ConnectorError, MySQLConnection

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((ROOT / "docs" / "schemas" / "readyset-1.0.json").read_text(encoding="utf-8"))


def xml_rows(rows):
    body = []
    for row in rows:
        fields = []
        for k, v in row.items():
            if v is None:
                fields.append('<field name="%s" xsi:nil="true" />' % xml_escape(str(k)))
            else:
                fields.append('<field name="%s">%s</field>' % (xml_escape(str(k)), xml_escape(str(v))))
        body.append("<row>%s</row>" % "".join(fields))
    return (
        '<?xml version="1.0"?>'
        '<resultset xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">%s</resultset>'
        % "".join(body)
    )


def assert_matches_schema(case, value, schema, path="$"):
    if "enum" in schema:
        case.assertIn(value, schema["enum"], path)
    stype = schema.get("type")
    if stype is not None:
        allowed = stype if isinstance(stype, list) else [stype]
        ok = False
        for t in allowed:
            if t == "null" and value is None:
                ok = True
            elif t == "boolean" and isinstance(value, bool):
                ok = True
            elif t == "integer" and isinstance(value, int) and not isinstance(value, bool):
                ok = True
            elif t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                ok = True
            elif t == "string" and isinstance(value, str):
                ok = True
            elif t == "array" and isinstance(value, list):
                ok = True
            elif t == "object" and isinstance(value, dict):
                ok = True
        case.assertTrue(ok, "%s expected %r, got %r (%s)" % (path, allowed, value, type(value).__name__))
    if isinstance(value, dict) and "properties" in schema:
        for req in schema.get("required", ()):
            case.assertIn(req, value, "%s missing required %r" % (path, req))
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(schema["properties"])
            case.assertEqual(extra, set(), "%s unexpected keys %r" % (path, extra))
        for k, v in value.items():
            if k in schema["properties"]:
                assert_matches_schema(case, v, schema["properties"][k], "%s.%s" % (path, k))
    elif isinstance(value, list) and "items" in schema:
        for idx, item in enumerate(value):
            assert_matches_schema(case, item, schema["items"], "%s[%d]" % (path, idx))


def _fake_unquote_ident(text):
    """Unquote one SQL identifier the way the server would: remove exactly one
    pair of outer backticks, then collapse doubled backticks inside."""
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        return text[1:-1].replace("``", "`")
    return text


class FakeReadyset(object):
    """Stateful Readyset SQL (3307) or Admin SQL (6036) simulator."""

    def __init__(self, port=3307, database="shop", admin=False, repl_mode="mysql_gtid", cloud_blocked=False):
        self.port = port
        self.database = database
        self.admin = admin
        self.repl_mode = repl_mode
        self.cloud_blocked = cloud_blocked
        self.password = "rs-secret-pw"
        self.version = "stable-250605"
        self.status_text = "Online"
        self.repl_status = "Running"
        self.last_error = None
        self.db_connection = "Connected"
        self.controller_status = "Online"
        self.fail_reads = frozenset()  # any of "status", "caches", "tables"
        self.fail_virtual = frozenset()  # any of "cached_stats", "upstream_stats", "shallow", "refresh", "coalesce"
        self.omit_status_fields = False  # SHOW READYSET STATUS answers without Status / Replication Status
        self.explain_support = None  # planted readyset_supported for EXPLAIN CREATE CACHE
        self.heartbeat_staleness = None
        self.refresh_over_cap = "false"
        self.dropped_refreshes = "0"
        self.allowlists = {
            "FUNCTIONS": ["NOW", "UNIX_TIMESTAMP"],
            "VARIABLES": ["time_zone"],
            "SCHEMAS": ["shop"],
        }
        self.caches = [
            {
                "query_id": "q_1111aaaa",
                "name": "c_orders_deep",
                "query": "SELECT id, total FROM orders WHERE customer_id = $1",
                "properties": "deep",
                "count": "400",
                "database": "shop",
                "kind": "deep",
            },
            {
                "query_id": "q_2222bbbb",
                "name": "c_recent_shallow",
                "query": "SELECT id, status FROM orders WHERE created_at > $1",
                "properties": "shallow (ttl = 30s)",
                "count": "600",
                "database": "shop",
                "kind": "shallow",
            },
        ]
        self.proxied = [
            {
                "query_id": "q_3333cccc",
                "query": "SELECT COUNT(*) FROM orders WHERE status = $1",
                "readyset_supported": "yes",
                "count": "150",
            },
            {
                "query_id": "q_4444dddd",
                "query": "SELECT RAND(), id FROM orders",
                "readyset_supported": "unsupported: non-deterministic function RAND",
                "count": "12",
            },
        ]
        self.tables = [
            {"table": "`shop`.`orders`", "status": "Online", "description": ""},
            {"table": "`shop`.`audit_blob`", "status": "Not replicated",
             "description": "Column type POINT is not supported."},
        ]
        self.upstream_rows = [
            {
                "host": "mysql-primary", "port": "3306", "database": "shop", "user": "readyset",
                "weight": "100", "purpose": "cdc, query", "active_cdc": "true",
                "health": "up", "down_since": None, "last_ok": "2026-10-20T12:00:00Z", "connections": "3",
            },
            {
                "host": "mysql-replica", "port": "3306", "database": "shop", "user": "readyset",
                "weight": "100", "purpose": "query", "active_cdc": "false",
                "health": "up", "down_since": None, "last_ok": "2026-10-20T12:00:00Z", "connections": "2",
            },
        ]
        self.writes = []
        self.calls = []
        self.fail_on_write = None
        self.fail_reads_after_write = False
        self._after_failed_write = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run_write(self, sql, timeout=None):
        s = sql.strip()
        self.writes.append(s)
        return self._apply_write(s)

    def _apply_write(self, sql):
        s = sql.strip().rstrip(";").strip()
        upper = s.upper()
        if upper.startswith("CREATE ") and "CACHE" in upper:
            self.caches.append({
                "query_id": "q_9999eeee",
                "name": "c_new",
                "query": "SELECT COUNT(*) FROM orders WHERE status = $1",
                "properties": "shallow" if "SHALLOW" in upper else "deep",
                "count": "0",
                "database": "shop",
                "kind": "shallow" if "SHALLOW" in upper else "deep",
            })
        elif upper.startswith("DROP CACHE "):
            target = _fake_unquote_ident(s.split("DROP CACHE ", 1)[1].strip())
            self.caches = [c for c in self.caches if c["name"] != target and c["query_id"] != target]
        elif upper == "DROP ALL CACHES":
            self.caches = []
        elif upper == "DROP ALL DEEP CACHES":
            self.caches = [c for c in self.caches if c["kind"] != "deep"]
        elif upper == "DROP ALL SHALLOW CACHES":
            self.caches = [c for c in self.caches if c["kind"] != "shallow"]
        elif upper == "DROP ALL PROXIED QUERIES":
            self.proxied = []
        elif upper == "ALTER READYSET ENTER MAINTENANCE MODE":
            self.status_text = "Maintenance Mode"
        elif upper == "ALTER READYSET EXIT MAINTENANCE MODE":
            self.status_text = "Online"
        elif upper == "ALTER READYSET STOP REPLICATION":
            self.repl_status = "Stopped"
        elif upper == "ALTER READYSET START REPLICATION":
            self.repl_status = "Running"
        elif upper.startswith("ALTER READYSET ADD SHALLOW CACHE ALLOWED "):
            rest = s[len("ALTER READYSET ADD SHALLOW CACHE ALLOWED "):]
            kind_word, ident = rest.split(" ", 1)
            plural = kind_word.upper() + "S"
            val = _fake_unquote_ident(ident.strip())
            if val not in self.allowlists[plural]:
                self.allowlists[plural].append(val)
        elif upper.startswith("ALTER READYSET DROP SHALLOW CACHE ALLOWED "):
            rest = s[len("ALTER READYSET DROP SHALLOW CACHE ALLOWED "):]
            kind_word, ident = rest.split(" ", 1)
            plural = kind_word.upper() + "S"
            val = _fake_unquote_ident(ident.strip())
            self.allowlists[plural] = [x for x in self.allowlists[plural] if x.lower() != val.lower()]
        if self.fail_on_write:
            self._after_failed_write = True
            raise ConnectorError(self.fail_on_write)
        return ""

    def run(self, sql, extra_flags=None, timeout=None):
        self.calls.append((sql, list(extra_flags or ())))
        if self._after_failed_write and self.fail_reads_after_write:
            raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
        s = sql.strip().rstrip(";").strip()
        upper = s.upper()
        if s == "SELECT @@version_comment":
            return xml_rows([{"@@version_comment": "Readyset"}])
        if upper == "SHOW READYSET VERSION":
            return xml_rows([
                {"name": "release version", "value": self.version},
                {"name": "commit_id", "value": "6d2adde9b0"},
            ])
        if "SELECT 1 FROM REPLICATION_STATUS LIMIT 0" in upper:
            if self.admin or (self.database or "").lower() == "readyset":
                return xml_rows([])
            raise ConnectorError("ERROR 1146 (42S02): Table 'shop.replication_status' doesn't exist")
        if upper == "SHOW DATABASES":
            # Both listeners route this to DataFusion with readyset selected.
            if self.admin or (self.database or "").lower() == "readyset":
                return xml_rows([{"Database": "information_schema"}, {"Database": "public"}, {"Database": "readyset"}])
            return xml_rows([{"Database": "information_schema"}, {"Database": "shop"}, {"Database": "readyset"}])
        if upper == "SHOW READYSET STATUS":
            if "status" in self.fail_reads:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            rows = [
                {"name": "Database Connection", "value": self.db_connection},
                {"name": "Connection Count", "value": "5"},
                {"name": "Status", "value": self.status_text},
                {"name": "Replication Status", "value": self.repl_status},
                {"name": "CDC Server", "value": "mysql-primary:3306"},
                {"name": "Maximum Replication Offset", "value": "aaaa:1-100"},
                {"name": "Minimum Replication Offset", "value": "aaaa:1-100"},
                {"name": "Readyset Controller Status", "value": self.controller_status},
                {"name": "Last completed snapshot", "value": "2026-10-20 12:00:00 UTC"},
                {"name": "Last replicator error", "value": self.last_error},
                {"name": "Host CPUs", "value": "8"},
            ]
            if self.omit_status_fields:
                omitted = (("Status", "Replication Status") if self.omit_status_fields is True
                           else self.omit_status_fields)
                rows = [r for r in rows if r["name"] not in omitted]
            return xml_rows(rows)
        if upper == "SHOW CACHES":
            if "caches" in self.fail_reads:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            if self.cloud_blocked:
                raise ConnectorError("ERROR 1105 (HY000): Please manage caches via Readyset Cloud UI")
            return xml_rows([
                {k: v for k, v in c.items() if k != "kind"}
                for c in self.caches
            ])
        if upper.startswith("SHOW PROXIED ") and "QUERIES" in upper:
            return xml_rows(self.proxied)
        if upper == "SHOW READYSET ALL TABLES":
            if "tables" in self.fail_reads:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            return xml_rows(self.tables)
        if upper == "SHOW CONNECTIONS":
            return xml_rows([
                {"client_addr": "172.18.0.5:51234", "username": "app", "upstream": "mysql://readyset:secret@mysql-primary:3306/shop"},
            ])
        for kind in ("FUNCTIONS", "VARIABLES", "SCHEMAS"):
            if upper == "SHOW SHALLOW CACHE ALLOWED %s" % kind:
                return xml_rows([{"name": item} for item in self.allowlists[kind]])
        if "UPSTREAM_SERVERS" in upper:
            return xml_rows(self.upstream_rows)
        if "REPLICATION_STATUS" in upper:
            if self.repl_mode == "mysql_file":
                return xml_rows([{
                    "replication_mode": "mysql_file",
                    "upstream_offset": "mysql-bin.000003:4096",
                    "replicator_offset": "mysql-bin.000003:3072",
                    "persisted_offset": "mysql-bin.000003:2048",
                    "consume_lag": "1024",
                    "persist_lag": "2048",
                    "heartbeat_staleness_seconds": self.heartbeat_staleness or "2",
                }])
            return xml_rows([{
                "replication_mode": "mysql_gtid",
                "upstream_offset": "aaaa:1-100",
                "replicator_offset": "aaaa:1-100",
                "persisted_offset": "aaaa:1-100",
                "consume_lag": "0",
                "persist_lag": "0",
                "heartbeat_staleness_seconds": self.heartbeat_staleness or "1",
            }])
        if "DEEP_CACHES" in upper:
            return xml_rows([
                {
                    "query_id": c["query_id"], "name": c["name"], "query": c["query"],
                    "always": "false", "until_write": "true", "total_count": c["count"],
                }
                for c in self.caches if c["kind"] == "deep"
            ])
        if "SHALLOW_CACHES" in upper and "STATS" not in upper and "ENTRIES" not in upper:
            if "shallow" in self.fail_virtual:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            return xml_rows([
                {
                    "query_id": c["query_id"], "name": c["name"], "query": c["query"],
                    "ttl_ms": "30000", "refresh_ms": "15000", "coalesce_ms": "50",
                    "always": "false", "until_write": "false", "schedule": None, "adaptive": "true",
                    "hits": "540", "misses": "60", "refreshes": "10", "coalesces": "8",
                }
                for c in self.caches if c["kind"] == "shallow"
            ])
        if "SHALLOW_CACHE_REFRESH_STATS" in upper:
            if "refresh" in self.fail_virtual:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            return xml_rows([{
                "query_id": "q_2222bbbb", "wasted_refreshes": "2", "dropped_refreshes": self.dropped_refreshes,
                "load_actual_ppm": "1200", "load_baseline_ppm": "1000", "over_cap": self.refresh_over_cap,
                "scheduler_queue_len": "0",
            }])
        if "SHALLOW_CACHE_COALESCE_STATS" in upper:
            if "coalesce" in self.fail_virtual:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            return xml_rows([{
                "query_id": "q_2222bbbb", "coalesce_timeouts": "0", "coalesce_aborts": "0",
                "total_us": "4000", "avg_us": "500", "upstream_saved_total_us": "24000", "upstream_saved_avg_us": "3000",
            }])
        if "CACHED_QUERY_STATS" in upper:
            if "cached_stats" in self.fail_virtual:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            return xml_rows([
                {
                    "query_id": "q_1111aaaa", "query": "SELECT id, total FROM orders WHERE customer_id = $1",
                    "total_count": "400", "total_us": "80000", "avg_us": "200", "min_us": "90", "max_us": "1500",
                    "p50_us": "180", "p90_us": "320", "p95_us": "450", "p99_us": "900", "p999_us": "1400",
                    "last_executed_s": "3",
                },
                {
                    "query_id": "q_2222bbbb", "query": "SELECT id, status FROM orders WHERE created_at > $1",
                    "total_count": "600", "total_us": "180000", "avg_us": "300", "min_us": "110", "max_us": "2200",
                    "p50_us": "250", "p90_us": "480", "p95_us": "650", "p99_us": "1200", "p999_us": "2000",
                    "last_executed_s": "1",
                },
            ])
        if "UPSTREAM_QUERY_STATS" in upper:
            if "upstream_stats" in self.fail_virtual:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
            return xml_rows([
                {
                    "query_id": "q_3333cccc", "query": "SELECT COUNT(*) FROM orders WHERE status = $1",
                    "total_count": "250", "total_us": "2500000", "avg_us": "10000", "min_us": "4000", "max_us": "45000",
                    "p50_us": "9000", "p90_us": "18000", "p95_us": "24000", "p99_us": "38000", "p999_us": "44000",
                    "last_executed_s": "2",
                },
            ])
        if upper == "EXPLAIN ALL CACHES":
            if getattr(self, "legacy_explain_caches", False):
                raise ConnectorError("ERROR 1064 (42000) at line 1: syntax error near 'ALL CACHES'")
            return xml_rows([
                {"query": "CREATE DEEP CACHE `c_orders_deep` FROM SELECT id, total FROM orders WHERE customer_id = 1;", "database": "shop"},
                {"query": "USE `shop`; CREATE SHALLOW CACHE `c_recent_shallow` WITH (ttl = 30s) FROM SELECT id, status FROM orders WHERE created_at > '2026-01-01';", "database": "shop"},
            ])
        if upper == "EXPLAIN CACHES":
            return xml_rows([
                {"query": "CREATE DEEP CACHE `c_orders_deep` FROM SELECT id, total FROM orders WHERE customer_id = 1;"},
                {"query": "USE `shop`; CREATE SHALLOW CACHE `c_recent_shallow` WITH (ttl = 30s) FROM SELECT id, status FROM orders WHERE created_at > '2026-01-01';"},
            ])
        if upper.startswith("EXPLAIN CREATE"):
            if self.admin or (self.database or "").lower() == "readyset":
                raise ConnectorError("ERROR 1105 (HY000): EXPLAIN CREATE CACHE must run on the SQL adapter port")
            if self.explain_support is not None:
                return xml_rows([{
                    "query_id": "q_6666ffff",
                    "query": "SELECT id FROM t WHERE u = $1",
                    "readyset_supported": self.explain_support,
                }])
            if "RAND()" in upper:
                return xml_rows([{
                    "query_id": "q_4444dddd",
                    "query": "SELECT RAND(), id FROM orders",
                    "readyset_supported": "unsupported: non-deterministic function RAND",
                }])
            qid = "q_1111aaaa" if "q_1111aaaa" in s else "q_3333cccc"
            return xml_rows([{
                "query_id": qid,
                "query": "SELECT COUNT(*) FROM orders WHERE status = $1",
                "readyset_supported": "yes",
            }])
        return xml_rows([])


class ReadysetBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = dict(os.environ, HOME=self.temp.name, USERPROFILE=self.temp.name, MYFLAMES_SECRET_BACKEND="vault")
        env.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        topology_links._reset_cache_for_tests()
        from myflames.workspace_store import WorkspaceStore
        self.workspace = type("Workspace", (), {"store": WorkspaceStore(os.path.join(self.temp.name, "ws.sqlite3"))})()


class TestReadStatusAndSchema(ReadysetBase):

    def test_adapter_read_matches_schema_and_computes_telemetry(self):
        conn = FakeReadyset(port=3307, database="shop")
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertTrue(report["available"])
        self.assertTrue(report["is_readyset"])
        self.assertEqual(report["port_role"], "adapter")
        self.assertFalse(report["in_readyset_schema"])
        self.assertEqual(report["status"], "ok")
        tel = report["telemetry"]
        self.assertEqual(tel["cached_executions"], 1000)
        self.assertEqual(tel["upstream_executions"], 250)
        self.assertEqual(tel["total_observed_executions"], 1250)
        self.assertAlmostEqual(tel["cached_share_pct"], 80.0)
        self.assertEqual(tel["shallow_hits"], 540)
        self.assertEqual(tel["shallow_misses"], 60)
        self.assertAlmostEqual(tel["shallow_hit_rate_pct"], 90.0)
        self.assertEqual(tel["wasted_refreshes"], 2)
        self.assertAlmostEqual(tel["upstream_saved_ms"], 24.0)
        # Microseconds from virtual tables become milliseconds in report.
        c0 = report["caches"][0]
        self.assertAlmostEqual(c0["cached_stats"]["p50_ms"], 0.18)
        self.assertAlmostEqual(c0["cached_stats"]["p99_ms"], 0.9)
        # URL password in SHOW CONNECTIONS upstream is redacted.
        self.assertNotIn("secret@", json.dumps(report))
        self.assertIn("[redacted]@", json.dumps(report))
        # GTID replication mode reports transactions unit.
        self.assertEqual(report["replication"]["lag_unit"], "transactions")
        # Both deep and shallow proxied queries are merged by query_id.
        self.assertEqual(len(report["proxied_queries"]), 2)
        pq_by_id = {p["query_id"]: p for p in report["proxied_queries"]}
        self.assertEqual(pq_by_id["q_3333cccc"]["shallow_support"], "yes")
        self.assertEqual(pq_by_id["q_3333cccc"]["deep_support"], "yes")
        self.assertEqual(pq_by_id["q_4444dddd"]["shallow_support"], "unsupported")

    def test_session_database_reports_the_default_database_exactly_as_given(self):
        report = readyset.read_status(FakeReadyset(port=3307, database="shop"))
        assert_matches_schema(self, report, SCHEMA)
        self.assertEqual(report["session_database"], "shop")
        # Exactly as given: no trimming, so a padded name stays padded.
        padded = readyset.read_status(FakeReadyset(port=3307, database=" shop"))
        self.assertEqual(padded["session_database"], " shop")
        # Empty means None (admin port connections carry no default database).
        self.assertIsNone(readyset.read_status(FakeReadyset(port=3307, database=""))["session_database"])
        admin = readyset.read_status(FakeReadyset(port=6036, database=None, admin=True))
        assert_matches_schema(self, admin, SCHEMA)
        self.assertIsNone(admin["session_database"])
        # A non-Readyset report carries null too.
        class MySQLConn(object):
            def run(self, sql, extra_flags=None, timeout=None):
                return xml_rows([{"@@version_comment": "MySQL Community Server - GPL"}])
        not_rs = readyset.read_status(MySQLConn())
        assert_matches_schema(self, not_rs, SCHEMA)
        self.assertIsNone(not_rs["session_database"])

    def test_binlog_file_mode_reports_byte_lag_unit(self):
        conn = FakeReadyset(repl_mode="mysql_file")
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertEqual(report["replication"]["lag_unit"], "bytes")
        self.assertEqual(report["replication"]["consume_lag"], 1024)
        self.assertEqual(report["replication"]["persist_lag"], 2048)

    def test_admin_port_enables_shutdown_and_disables_create_cache(self):
        conn = FakeReadyset(port=6036, database=None, admin=True)
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertEqual(report["port_role"], "admin")
        self.assertTrue(report["in_readyset_schema"])
        self.assertFalse(report["capabilities"]["can_create_cache"])
        self.assertFalse(report["capabilities"]["can_explain_cache"])
        self.assertTrue(report["capabilities"]["can_shutdown"])
        acts = {a["id"]: a for a in report["actions"]}
        # Every action is always listed; the port role only flips enabled.
        self.assertIn("create_cache", acts)
        self.assertFalse(acts["create_cache"]["enabled"])
        self.assertIn("SQL port", acts["create_cache"]["why_not"])
        self.assertIn("shutdown", acts)
        self.assertTrue(acts["shutdown"]["enabled"])
        self.assertIn("shutdown_reset", acts)
        self.assertTrue(acts["shutdown_reset"]["enabled"])

    def test_non_readyset_server_returns_clean_unavailable_report(self):
        class MySQLConn(object):
            def run(self, sql, extra_flags=None, timeout=None):
                return xml_rows([{"@@version_comment": "MySQL Community Server - GPL"}])
        report = readyset.read_status(MySQLConn())
        assert_matches_schema(self, report, SCHEMA)
        self.assertFalse(report["available"])
        self.assertFalse(report["is_readyset"])
        self.assertIn("not a Readyset", report["message"])

    def test_degraded_replication_sets_problem_or_watch_verdict(self):
        conn = FakeReadyset()
        conn.last_error = "Binlog purged on upstream"
        report = readyset.read_status(conn)
        self.assertEqual(report["status"], "problem")
        self.assertTrue(any("Replication" in w or "error" in w.lower() for w in report["status_why"]))

    def test_shallow_only_disabled_replication_stays_ok(self):
        conn = FakeReadyset()
        conn.repl_status = "Disabled"
        conn.caches = [c for c in conn.caches if c["kind"] == "shallow"]
        report = readyset.read_status(conn)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["status_why"], [])

        # If deep caches exist while Replication Status is Disabled, surface Watch.
        conn_with_deep = FakeReadyset()
        conn_with_deep.repl_status = "Disabled"
        report_deep = readyset.read_status(conn_with_deep)
        self.assertEqual(report_deep["status"], "watch")
        self.assertTrue(any("Disabled" in w for w in report_deep["status_why"]))

    def test_query_normalizer_preserves_quoted_whitespace_and_rejects_equal_refresh_ttl(self):
        kind, norm = readyset._normalize_query_or_id(
            "SELECT  /* block comment */  'a  b',  `x  y`, 'it''s  ok'\nFROM   orders  WHERE status = 'paid  now';"
        )
        self.assertEqual(kind, "sql")
        self.assertEqual(
            norm,
            "SELECT 'a  b', `x  y`, 'it''s  ok' FROM orders WHERE status = 'paid  now'",
        )
        with self.assertRaises(ValueError):
            readyset._build_create_cache_sql({
                "cache_type": "shallow",
                "query_or_id": "SELECT id FROM orders",
                "ttl_seconds": 30,
                "refresh_seconds": 30,
            })


class TestPortRoleEvidence(ReadysetBase):
    """Lane K3a: the listener role comes from probe evidence, never the port
    number; an inconclusive probe leaves the role not read (None)."""

    class ProbeFailConn(FakeReadyset):
        """No default database; the unqualified replication_status probe fails."""

        def __init__(self, probe_error, **kwargs):
            FakeReadyset.__init__(self, database=None, **kwargs)
            self.probe_error = probe_error

        def run(self, sql, extra_flags=None, timeout=None):
            if "SELECT 1 FROM REPLICATION_STATUS LIMIT 0" in sql.strip().upper():
                raise ConnectorError(self.probe_error)
            return FakeReadyset.run(self, sql, extra_flags=extra_flags, timeout=timeout)

    def test_no_database_probe_error_1046_or_1146_means_adapter(self):
        for message in (
            "ERROR 1046 (3D000) at line 1: No database selected",
            "ERROR 1146 (42S02): Table 'readyset.replication_status' doesn't exist",
        ):
            with self.subTest(message=message):
                conn = self.ProbeFailConn(message)
                report = readyset.read_status(conn)
                assert_matches_schema(self, report, SCHEMA)
                self.assertEqual(report["port_role"], "adapter")
                self.assertFalse(report["in_readyset_schema"])
                acts = {a["id"]: a for a in report["actions"]}
                self.assertTrue(acts["create_cache"]["enabled"])
                self.assertFalse(acts["shutdown"]["enabled"])
                self.assertIn("admin SQL port", acts["shutdown"]["why_not"])

    def test_no_database_probe_other_failure_leaves_role_not_read(self):
        conn = self.ProbeFailConn("ERROR 2013 (HY000): Lost connection to Readyset server during query")
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertIsNone(report["port_role"])
        self.assertFalse(report["in_readyset_schema"])
        self.assertFalse(report["capabilities"]["can_explain_cache"])
        self.assertFalse(report["capabilities"]["can_create_cache"])
        acts = {a["id"]: a for a in report["actions"]}
        why_not = "Readyset did not show which listener this is."
        fix = "Refresh; if it persists, check the connection's port."
        for aid in ("create_cache", "shutdown", "shutdown_reset"):
            self.assertFalse(acts[aid]["enabled"], aid)
            self.assertEqual(acts[aid]["why_not"], why_not, aid)
            self.assertEqual(acts[aid]["fix"], fix, aid)
        for row in report["proxied_queries"]:
            entry = row["actions"][0]
            self.assertEqual(entry["id"], "create_cache")
            self.assertFalse(entry["enabled"])
            self.assertEqual(entry["why_not"], why_not)
            self.assertEqual(entry["fix"], fix)
        # plan_action refuses with exactly the catalog text, at review and at apply.
        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
            readyset.plan_action(report, "create_cache", None,
                                 {"cache_type": "shallow", "query_or_id": "SELECT 1"})
        self.assertEqual(str(caught.exception), "%s %s" % (why_not, fix))
        with self.assertRaises(reviewed_writes.ActionUnavailable):
            readyset.plan_action(report, "shutdown")
        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
            readyset.explain_cache(conn, "SELECT 1")
        self.assertEqual(str(caught.exception), "%s %s" % (why_not, fix))
        self.assertFalse(any(sql.startswith("EXPLAIN CREATE") for sql, _ in conn.calls))
        # Summary and CLI text say the port was not read, never a guessed role.
        text = readyset.format_text(report)
        self.assertIn("port not read", text)
        self.assertNotIn("(sql port)", text)

    def test_readyset_database_probe_on_copy_identifies_adapter_on_any_port(self):
        # The original connection routes unqualified reads to DataFusion on
        # either listener. Only the copy with no database distinguishes them.
        conn = FakeReadyset(port=6036, database="readyset", admin=False)
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertEqual(report["port_role"], "adapter")
        self.assertTrue(report["in_readyset_schema"])
        self.assertFalse(report["capabilities"]["can_shutdown"])
        acts = {a["id"]: a for a in report["actions"]}
        self.assertFalse(acts["shutdown"]["enabled"])
        self.assertIn("admin SQL port", acts["shutdown"]["why_not"])
        self.assertFalse(acts["shutdown_reset"]["enabled"])
        self.assertEqual(conn.database, "readyset")
        self.assertIn("SELECT 1 FROM replication_status LIMIT 0", [sql for sql, _ in conn.calls])
        self.assertNotIn("SHOW DATABASES", [sql for sql, _ in conn.calls])
        self.assertFalse(report["capabilities"]["can_explain_cache"])

    def test_readyset_database_probe_on_copy_identifies_admin_on_any_port(self):
        conn = FakeReadyset(port=3307, database="readyset", admin=True)
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertEqual(report["port_role"], "admin")
        self.assertTrue(report["in_readyset_schema"])
        self.assertTrue(report["capabilities"]["can_shutdown"])
        acts = {a["id"]: a for a in report["actions"]}
        self.assertTrue(acts["shutdown"]["enabled"])
        self.assertTrue(acts["shutdown_reset"]["enabled"])
        self.assertEqual(conn.database, "readyset")
        self.assertNotIn("SHOW DATABASES", [sql for sql, _ in conn.calls])

    def test_readyset_database_failed_probe_leaves_role_not_read(self):
        conn = self.ProbeFailConn("ERROR 2013 (HY000): Lost connection to Readyset server during query")
        conn.database = "readyset"
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertIsNone(report["port_role"])
        self.assertTrue(report["in_readyset_schema"])
        acts = {a["id"]: a for a in report["actions"]}
        why_not = "Readyset did not show which listener this is."
        self.assertEqual(acts["create_cache"]["why_not"], why_not)
        self.assertEqual(acts["shutdown"]["why_not"], why_not)
        self.assertEqual(acts["shutdown_reset"]["why_not"], why_not)
        self.assertEqual(conn.database, "readyset")
        self.assertFalse(report["capabilities"]["can_explain_cache"])
        with self.assertRaisesRegex(reviewed_writes.ActionUnavailable, "did not show which listener"):
            readyset.explain_cache(conn, "SELECT 1")
        self.assertFalse(any(sql.startswith("EXPLAIN CREATE") for sql, _ in conn.calls))

    def test_readyset_database_copy_probe_error_codes(self):
        for code, role in ((1046, "adapter"), (1146, "adapter"), (1045, None), (1105, None)):
            with self.subTest(code=code):
                conn = self.ProbeFailConn("ERROR %d (HY000): probe failed" % code)
                conn.database = "readyset"
                self.assertEqual(readyset._detect_port_mode(conn, "readyset"), (role, True))
                self.assertEqual(conn.database, "readyset")

    def test_uncopyable_connection_leaves_role_unknown_without_changing_database(self):
        class Uncopyable(FakeReadyset):
            def __copy__(self):
                raise TypeError("cannot copy")

        class SameCopy(FakeReadyset):
            def __copy__(self):
                return self

        class ReadOnlyDatabase(FakeReadyset):
            def __setattr__(self, name, value):
                if name == "database" and value is None:
                    raise AttributeError("read-only database")
                object.__setattr__(self, name, value)

        for cls in (Uncopyable, SameCopy, ReadOnlyDatabase):
            with self.subTest(cls=cls.__name__):
                conn = cls(database="readyset")
                self.assertEqual(readyset._detect_port_mode(conn, "readyset"), (None, True))
                self.assertEqual(conn.database, "readyset")
                self.assertEqual(conn.calls, [])

    def test_connection_copy_preserves_transport_and_login_consent(self):
        conn = MySQLConnection("rs", port=13307, user="user", password="secret", database="readyset",
                               ssl_mode="REQUIRED", ssh_host="jump", ssh_user="ssh-user", ssh_port=2222)
        conn._login_guard = lambda: None
        seen = []

        def probe(probe_conn, sql, timeout=None):
            self.assertIsNot(probe_conn, conn)
            self.assertIsNone(probe_conn.database)
            for key in ("host", "port", "user", "password", "ssl_mode", "ssh_host", "ssh_user", "ssh_port"):
                self.assertEqual(getattr(probe_conn, key), getattr(conn, key))
            self.assertIs(probe_conn._login_guard, conn._login_guard)
            self.assertEqual(sql, "SELECT 1 FROM replication_status LIMIT 0")
            seen.append(sql)
            return []

        with mock.patch.object(readyset, "select", side_effect=probe):
            self.assertEqual(readyset._detect_port_mode(conn, "readyset"), ("admin", True))
        self.assertEqual(conn.database, "readyset")
        self.assertEqual(len(seen), 1)

    def test_probe_copy_owns_no_original_resources_and_retains_login_attempt(self):
        for error, expected_role in ((None, "admin"), ("ERROR 1046: No database", "adapter"),
                                     ("ERROR 2013: Lost connection", None)):
            with self.subTest(error=error):
                conn = MySQLConnection("rs", database="readyset")
                self.addCleanup(conn.close)
                conn._login_guard = mock.Mock()
                conn._group_login_started = False
                held, legacy = mock.Mock(), mock.Mock()
                conn._held_lease, conn._legacy_lease = held, legacy
                path = os.path.join(self.temp.name, "original-%s.cnf" % expected_role)
                Path(path).write_text("[client]\n", encoding="utf-8")
                conn._defaults_file_path = path

                def probe(probe_conn, sql, timeout=None):
                    self.assertIsNot(probe_conn, conn)
                    self.assertIsNone(probe_conn._held_lease)
                    self.assertIsNone(probe_conn._legacy_lease)
                    self.assertIsNone(probe_conn._defaults_file_path)
                    # Exercise the real connector's consent bookkeeping without
                    # spawning a client, acquiring a tunnel or contacting a server.
                    with mock.patch("myflames.connector._run_bounded", return_value=""):
                        probe_conn._run_client(["unused-client"])
                    if error:
                        raise ConnectorError(error)
                    return []

                with mock.patch.object(readyset, "select", new=probe):
                    self.assertEqual(readyset._detect_port_mode(conn, "readyset"), (expected_role, True))
                self.assertIs(conn._held_lease, held)
                self.assertIs(conn._legacy_lease, legacy)
                self.assertEqual(conn._defaults_file_path, path)
                self.assertTrue(Path(path).exists())
                held.release.assert_not_called()
                legacy.release.assert_not_called()
                self.assertTrue(conn._group_login_started)
                conn._login_guard.assert_called_once_with()
                self.assertEqual(conn.database, "readyset")


class TestFindings(ReadysetBase):
    def by_id(self, report):
        return {f["id"]: f for f in report["findings"]}

    def one(self, conn, fid):
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        findings = self.by_id(report)
        self.assertIn(fid, findings, [f["id"] for f in report["findings"]])
        return report, findings[fid]

    def test_upstream_unreachable_is_problem_on_topology(self):
        conn = FakeReadyset()
        conn.db_connection = "Unreachable"
        report, f = self.one(conn, "upstream_unreachable")
        self.assertEqual(f["status"], "problem")
        self.assertEqual(f["tab"], "topology")
        self.assertEqual(report["status"], "problem")
        # Shallow misses, uncached queries and writes hit the upstream; deep
        # misses do not (status_reporter.rs 41-46, 214-237).
        self.assertIn("Shallow cache misses, uncached queries and every write go to the upstream", f["text"])
        self.assertIn("deep-cache misses are filled from Readyset's local base tables", f["text"])
        self.assertIn("a reconnect attempt failed, or no upstream URL is configured", f["text"])

    def test_controller_unavailable_is_problem_on_topology(self):
        conn = FakeReadyset()
        conn.controller_status = "Unavailable"
        report, f = self.one(conn, "controller_unavailable")
        self.assertEqual(f["status"], "problem")
        self.assertEqual(f["tab"], "topology")
        self.assertEqual(report["status"], "problem")

    def test_replication_error_merges_last_error_and_error_status(self):
        conn = FakeReadyset()
        conn.last_error = "Binlog purged on upstream"
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        matches = [f for f in report["findings"] if f["id"] == "replication_error"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["status"], "problem")
        self.assertEqual(matches[0]["tab"], "topology")
        self.assertIn("Binlog purged on upstream", matches[0]["text"])
        self.assertEqual([f["id"] for f in report["findings"]], ["replication_error"])
        self.assertEqual(report["status"], "problem")
        self.assertEqual(report["status_why"], [f["title"] for f in report["findings"]])

        # A Replication Status of "Error" does not exist in the pinned engine
        # (readyset-client/src/status.rs 107-123 emits only Running, Stopped,
        # Disabled), but the contract merges it with the last replicator error
        # into the same problem finding, never a generic watch.
        only_status = FakeReadyset()
        only_status.repl_status = "Error"
        report3 = readyset.read_status(only_status)
        findings3 = self.by_id(report3)
        self.assertIn("replication_error", findings3)
        self.assertEqual(findings3["replication_error"]["status"], "problem")
        self.assertEqual(findings3["replication_error"]["title"], "Replication reported an error.")
        self.assertNotIn("replication_status", findings3)
        self.assertEqual(report3["status"], "problem")

        # Both signals still merge into one finding.
        both = FakeReadyset()
        both.repl_status = "Error"
        both.last_error = "Binlog purged on upstream"
        report4 = readyset.read_status(both)
        self.assertEqual([f["id"] for f in report4["findings"]], ["replication_error"])

    def test_upstream_down_id_uses_index_and_severity_follows_active_cdc(self):
        conn = FakeReadyset()
        conn.upstream_rows[0]["health"] = "down"  # active_cdc true
        conn.upstream_rows[1]["health"] = "down"  # active_cdc false
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        findings = self.by_id(report)
        self.assertEqual(findings["upstream_down:0"]["status"], "problem")
        self.assertEqual(findings["upstream_down:1"]["status"], "watch")
        self.assertEqual(findings["upstream_down:0"]["tab"], "topology")
        self.assertIn("mysql-primary:3306", findings["upstream_down:0"]["title"])
        for f in report["findings"]:
            self.assertNotIn("mysql", f["id"], "finding ids never carry a server address")
        self.assertEqual(report["status"], "problem")
        # Health is the adapter's proxy-pool state; a down CDC server hints at
        # replicator failover (upstream_servers_vrel.rs 60-78, 121-124).
        text = findings["upstream_down:0"]["text"]
        self.assertIn("proxy-pool health down", text)
        self.assertIn("the replicator's connection to it is likely failing too", text)
        self.assertIn("fails over to another CDC server if one is listed", text)

    def test_upstream_down_title_keeps_fixed_words_with_long_host(self):
        # The fixed words of a title are never capped; interpolated server
        # values are (80 chars each through the display helper). A long host
        # cannot cut "health is down." off the title.
        conn = FakeReadyset()
        conn.upstream_rows[0]["host"] = "h" * 66
        conn.upstream_rows[0]["health"] = "down"
        conn.upstream_rows[0]["active_cdc"] = "false"
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        f = self.by_id(report)["upstream_down:0"]
        self.assertEqual(f["status"], "watch")
        self.assertEqual(
            f["title"],
            "Upstream %s:3306 (cdc, query) health is down." % ("h" * 66),
        )
        self.assertTrue(f["title"].endswith("health is down."))
        self.assertGreater(len(f["title"]), 80)
        self.assertEqual(report["status_why"], [x["title"] for x in report["findings"]])
        self.assertIn(f["title"], report["status_why"])

    def test_table_errors_is_problem_on_tables(self):
        conn = FakeReadyset()
        conn.tables = [
            {"table": "`shop`.`a`", "status": "Not replicated",
             "description": "An unexpected replication error occurred: binlog event failed to apply"},
            {"table": "`shop`.`b`", "status": "Not replicated",
             "description": "An unexpected replication error occurred: binlog event failed to apply"},
        ]
        report, f = self.one(conn, "table_errors")
        self.assertEqual(f["status"], "problem")
        self.assertEqual(f["tab"], "tables")
        self.assertEqual(f["title"], "2 replicated base tables reported an error.")

    def test_table_status_classifier_matches_the_readyset_statuses(self):
        # The status column shows exactly these values
        # (readyset-client/src/table.rs 242-264); the error bucket is reachable
        # only through the description (readyset-sql/src/ast/table.rs 411-413).
        cases = [
            ("Online", None, "snapshotted"),
            ("Initializing", None, "snapshotting"),
            ("Snapshotting", None, "snapshotting"),
            ("Compacting", None, "snapshotting"),
            ("Creating index", None, "snapshotting"),
            ("Not replicated", "Column type POINT is not supported.", "ignored"),
            ("Not replicated", "No specific reason provided.", "ignored"),
            ("Not replicated", "An unexpected replication error occurred: disk full", "error"),
            ("Dropped", None, "other"),
            ("Something new", None, "other"),
            (None, None, "other"),
        ]
        for status, description, bucket in cases:
            with self.subTest(status=status, description=description):
                self.assertEqual(readyset._classify_table_status(status, description), bucket)

    def test_maintenance_mode_is_watch_on_topology(self):
        conn = FakeReadyset()
        conn.status_text = "Maintenance Mode"
        report, f = self.one(conn, "maintenance_mode")
        self.assertEqual(f["status"], "watch")
        self.assertEqual(f["tab"], "topology")
        self.assertEqual(report["status"], "watch")

    def test_snapshot_in_progress_is_watch_on_topology(self):
        conn = FakeReadyset()
        conn.status_text = "Snapshot In Progress"
        report, f = self.one(conn, "snapshot_in_progress")
        self.assertEqual(f["status"], "watch")
        self.assertEqual(f["tab"], "topology")

    def test_replication_stopped_is_watch_on_topology(self):
        conn = FakeReadyset()
        conn.repl_status = "Stopped"
        report, f = self.one(conn, "replication_stopped")
        self.assertEqual(f["status"], "watch")
        self.assertEqual(f["tab"], "topology")

    def test_replication_disabled_is_watch_on_topology(self):
        conn = FakeReadyset()
        conn.repl_status = "Disabled"
        report, f = self.one(conn, "replication_disabled")
        self.assertEqual(f["status"], "watch")
        self.assertEqual(f["tab"], "topology")

    def test_replication_status_other_is_watch_with_display_safe_title(self):
        conn = FakeReadyset()
        conn.repl_status = "Paused"
        report, f = self.one(conn, "replication_status")
        self.assertEqual(f["status"], "watch")
        self.assertEqual(f["tab"], "topology")
        self.assertEqual(f["title"], "Replication status is Paused.")

    def test_replication_heartbeat_stale_is_watch(self):
        conn = FakeReadyset()
        conn.heartbeat_staleness = "120"
        report, f = self.one(conn, "replication_heartbeat_stale")
        self.assertEqual(f["status"], "watch")
        self.assertEqual(f["tab"], "topology")
        self.assertEqual(f["title"], "Replication heartbeat is 120 s old.")
        # Heartbeat timestamps come from different hosts; the measurement is
        # not proof of data delay (replication_lag_reporter.rs:507-523).
        self.assertEqual(
            f["text"],
            "Readyset measures heartbeat staleness at 120 s; this alone does not establish how far "
            "replicated data or deep caches lag. Check consume lag, the replicator, heartbeat writes "
            "to the upstream, and clock skew between the upstream and Readyset hosts.",
        )

    def test_shallow_refresh_capped_is_watch_on_caches(self):
        conn = FakeReadyset()
        conn.refresh_over_cap = "true"
        report, f = self.one(conn, "shallow_refresh_capped")
        self.assertEqual(f["status"], "watch")
        self.assertEqual(f["tab"], "caches")
        # Dropped refreshes also count a failed upstream connection or query
        # (metric/src/shallow.rs 32-33).
        self.assertIn("the upstream connection or query failed", f["text"])

        conn2 = FakeReadyset()
        conn2.dropped_refreshes = "3"
        report2, f2 = self.one(conn2, "shallow_refresh_capped")
        self.assertEqual(f2["status"], "watch")

    def test_failed_status_read_is_problem_and_never_online(self):
        conn = FakeReadyset()
        conn.fail_reads = frozenset(("status",))
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        findings = self.by_id(report)
        self.assertEqual(findings["read_failed:status"]["status"], "problem")
        self.assertEqual(report["status"], "problem")
        self.assertIsNone(report["status_fields"]["status"])
        self.assertIsNone(report["status_fields"]["replication_status"])
        self.assertIsNone(report["topology"]["this_server"]["status"])
        self.assertNotIn("Online", json.dumps(report["status_fields"]))
        self.assertNotIn("Online", json.dumps(report["topology"]["this_server"]))
        text = readyset.format_text(report)
        self.assertIn("Not read", text)
        self.assertNotIn("Online", text)

    def test_failed_caches_and_tables_reads_are_watch(self):
        conn = FakeReadyset()
        conn.fail_reads = frozenset(("caches", "tables"))
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        findings = self.by_id(report)
        self.assertEqual(findings["read_failed:caches"]["status"], "watch")
        self.assertEqual(findings["read_failed:caches"]["tab"], "caches")
        self.assertEqual(findings["read_failed:tables"]["status"], "watch")
        self.assertEqual(findings["read_failed:tables"]["tab"], "tables")
        # No problem finding: the state reads succeeded and are healthy.
        self.assertEqual(report["status"], "watch")

    def test_failed_statistics_reads_leave_counts_and_share_unknown(self):
        # Codex: a failed cached statistics read is not a zero; the share needs
        # both sides, so it stays null when either input was not read.
        conn = FakeReadyset()
        conn.fail_virtual = frozenset(("cached_stats",))
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        tel = report["telemetry"]
        self.assertIsNone(tel["cached_executions"])
        self.assertEqual(tel["upstream_executions"], 250)
        self.assertIsNone(tel["total_observed_executions"])
        self.assertIsNone(tel["cached_share_pct"])
        text = readyset.format_text(report)
        self.assertIn("not read", text)
        self.assertNotIn("0.0% cached share", text)

        conn2 = FakeReadyset()
        conn2.fail_virtual = frozenset(("upstream_stats",))
        tel2 = readyset.read_status(conn2)["telemetry"]
        self.assertEqual(tel2["cached_executions"], 1000)
        self.assertIsNone(tel2["upstream_executions"])
        self.assertIsNone(tel2["total_observed_executions"])
        self.assertIsNone(tel2["cached_share_pct"])

        conn3 = FakeReadyset()
        conn3.fail_virtual = frozenset(("shallow",))
        tel3 = readyset.read_status(conn3)["telemetry"]
        self.assertIsNone(tel3["shallow_hits"])
        self.assertIsNone(tel3["shallow_misses"])
        self.assertIsNone(tel3["shallow_hit_rate_pct"])
        self.assertIsNone(tel3["shallow_refreshes"])

    def test_unreported_status_fields_are_watch_never_ok(self):
        # Codex: a successful but incomplete SHOW READYSET STATUS (no Status,
        # no Replication Status) must not leave the report OK on missing data.
        conn = FakeReadyset()
        conn.omit_status_fields = True
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertEqual([f["id"] for f in report["findings"]],
                         ["status_unreported", "replication_status_unreported"])
        finding = report["findings"][0]
        self.assertEqual(finding["status"], "watch")
        self.assertEqual(finding["title"], "Readyset did not report its status.")
        self.assertEqual(finding["tab"], "topology")
        self.assertEqual(report["status"], "watch")
        self.assertEqual(report["status_why"], ["Readyset did not report its status.",
                                               "Readyset did not report its replication status."])

        # An unrelated problem does not account for either missing field.
        conn2 = FakeReadyset()
        conn2.omit_status_fields = True
        conn2.db_connection = "Unreachable"
        report2 = readyset.read_status(conn2)
        ids2 = [f["id"] for f in report2["findings"]]
        self.assertEqual(ids2, ["upstream_unreachable", "status_unreported", "replication_status_unreported"])

    def test_each_missing_status_field_gets_its_own_finding(self):
        for field, fid, other in (("Status", "status_unreported", "replication_status"),
                                  ("Replication Status", "replication_status_unreported", "status")):
            with self.subTest(field=field):
                conn = FakeReadyset()
                conn.omit_status_fields = {field}
                report = readyset.read_status(conn)
                assert_matches_schema(self, report, SCHEMA)
                self.assertEqual(report["status"], "watch")
                self.assertEqual([f["id"] for f in report["findings"]], [fid])
                self.assertEqual(report["findings"][0]["status"], "watch")
                self.assertEqual(report["findings"][0]["tab"], "topology")
                self.assertIsNotNone(report["status_fields"][other])
                self.assertEqual(report["status_why"], [report["findings"][0]["title"]])
                self.assertIn(report["findings"][0]["title"], readyset.format_text(report))

    def test_missing_status_findings_do_not_duplicate_evidence(self):
        conn = FakeReadyset()
        conn.fail_reads = {"status"}
        report = readyset.read_status(conn)
        self.assertEqual([f["id"] for f in report["findings"]], ["read_failed:status"])
        conn = FakeReadyset()
        conn.omit_status_fields = {"Replication Status"}
        conn.last_error = "Binlog read failed"
        report = readyset.read_status(conn)
        self.assertEqual([f["id"] for f in report["findings"]], ["replication_error"])

    def test_upstream_active_cdc_preserves_unknown_true_and_false(self):
        for raw, expected in ((None, None), ("true", True), ("false", False)):
            with self.subTest(raw=raw):
                conn = FakeReadyset()
                conn.upstream_rows[0]["active_cdc"] = raw
                conn.upstream_rows[0]["health"] = "down"
                report = readyset.read_status(conn)
                assert_matches_schema(self, report, SCHEMA)
                self.assertIs(report["upstreams"][0]["active_cdc"], expected)
                self.assertIs(report["topology"]["upstreams"][0]["active_cdc"], expected)
                finding = self.by_id(report)["upstream_down:0"]
                self.assertEqual(finding["status"], "problem" if expected else "watch")

    def test_status_matches_old_verdict_when_every_read_succeeds(self):

        cases = [
            (FakeReadyset(port=3307, database="shop"), "ok"),
            (FakeReadyset(port=3307, database="shop", repl_mode="mysql_file"), "ok"),
            (FakeReadyset(port=6036, database=None, admin=True), "ok"),
        ]
        maint = FakeReadyset()
        maint.status_text = "Maintenance Mode"
        maint.repl_status = "Stopped"
        cases.append((maint, "watch"))
        prob = FakeReadyset()
        prob.last_error = "Binlog purged on upstream"
        cases.append((prob, "problem"))
        shallow_disabled = FakeReadyset()
        shallow_disabled.repl_status = "Disabled"
        shallow_disabled.caches = [c for c in shallow_disabled.caches if c["kind"] == "shallow"]
        cases.append((shallow_disabled, "ok"))
        deep_disabled = FakeReadyset()
        deep_disabled.repl_status = "Disabled"
        cases.append((deep_disabled, "watch"))
        for conn, expected in cases:
            with self.subTest(expected=expected):
                report = readyset.read_status(conn)
                assert_matches_schema(self, report, SCHEMA)
                self.assertEqual(report["status"], expected)
                self.assertEqual(report["status_why"], [f["title"] for f in report["findings"]])

    def test_findings_are_worst_first_with_stable_order(self):
        conn = FakeReadyset()
        conn.status_text = "Maintenance Mode"   # watch
        conn.db_connection = "Unreachable"      # problem
        conn2 = FakeReadyset()
        conn2.status_text = "Maintenance Mode"
        conn2.repl_status = "Stopped"
        report = readyset.read_status(conn)
        self.assertEqual([f["status"] for f in report["findings"]],
                         ["problem"] + ["watch"] * (len(report["findings"]) - 1))
        self.assertEqual(report["findings"][0]["id"], "upstream_unreachable")
        report2 = readyset.read_status(conn2)
        self.assertEqual([f["id"] for f in report2["findings"]],
                         ["maintenance_mode", "replication_stopped"])

    def test_abuse_huge_bidi_server_text_is_capped_and_display_safe(self):
        blob = ("<img onerror=x> ‮ \x1b " * 2) + "x" * (100 * 1024)
        conn = FakeReadyset()
        conn.last_error = blob
        conn.repl_status = "Paused‮\x1b" + "z" * (100 * 1024)
        conn.upstream_rows[0]["health"] = "down"
        conn.upstream_rows[0]["host"] = "mysql-‮\x1b" + "h" * (100 * 1024)
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        for f in report["findings"]:
            for ch in f["title"]:
                self.assertNotIn(unicodedata.category(ch), ("Cc", "Cf"), f["id"])
            # Fixed words are never capped; each interpolated server value is
            # capped at 80, so a huge value cannot push a title past two
            # interpolated values plus the fixed wording.
            self.assertLessEqual(len(f["title"]), 80 * 2 + 60, f["id"])
        up = self.by_id(report)["upstream_down:0"]
        self.assertTrue(up["title"].endswith("health is down."), up["title"])
        rep_err = self.by_id(report)["replication_error"]
        fixed = ("Replication from the upstream database hit an error; deep caches are not receiving upstream "
                 "writes while it persists. Readyset's last replicator error: ")
        self.assertTrue(rep_err["text"].startswith(fixed))
        self.assertLessEqual(len(rep_err["text"]), len(fixed) + 2000)
        # format_text prints the finding titles only: each is display-safe.
        text = readyset.format_text(report)
        for f in report["findings"]:
            self.assertIn(f["title"], text)

    def test_abuse_secrets_never_reach_report_or_format_text(self):
        conn = FakeReadyset()
        conn.last_error = "upstream mysql://u:secret@h:3306/db refused: IDENTIFIED BY 'x' for rs-secret-pw"
        # A cached query carrying credentials is redacted for display and plan
        # facts; the SQL myflames sends never comes from this copy.
        conn.caches[1]["query"] = (
            "SELECT * FROM t WHERE d = 'mysql://u:secret@h:3306/db' IDENTIFIED BY 'x' AND u = 'rs-secret-pw'"
        )
        conn.tables = [{
            "table": "`shop`.`t1`", "status": "Error",
            "description": "see mysql://u:secret@h:3306/db IDENTIFIED BY 'x' for rs-secret-pw",
        }]
        conn.proxied = [{
            "query_id": "q_aaaa0001",
            "query": "SELECT * FROM t WHERE d = 'mysql://u:secret@h:3306/db' IDENTIFIED BY 'x' AND u = 'rs-secret-pw'",
            "readyset_supported": "unsupported: column rs-secret-pw IDENTIFIED BY 'x'",
            "count": "1",
        }]
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        plan = readyset.plan_action(
            report, "flush_cache",
            {"kind": "cache", "name": "c_recent_shallow", "query_id": "q_2222bbbb"})
        dumped = json.dumps(report) + readyset.format_text(report) + json.dumps(plan)
        self.assertNotIn("rs-secret-pw", dumped)
        self.assertNotIn("secret@h", dumped)
        self.assertNotIn("IDENTIFIED BY 'x'", dumped)
        self.assertIn("[redacted]", dumped)

    def test_identity_and_version_errors_are_redacted(self):
        class VersionFailConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql.strip().upper() == "SHOW READYSET VERSION":
                    raise ConnectorError(
                        "ERROR 1105 (HY000): bootstrap failed for rs-secret-pw via mysql://u:secret@h:3306/db")
                return FakeReadyset.run(self, sql, extra_flags=extra_flags, timeout=timeout)

        report = readyset.read_status(VersionFailConn())
        self.assertFalse(report["is_readyset"])
        dumped = json.dumps(report) + readyset.format_text(report)
        self.assertNotIn("rs-secret-pw", dumped)
        self.assertNotIn("secret@h", dumped)
        self.assertIn("[redacted]", dumped)

        class CommentFailConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql.strip() == "SELECT @@version_comment":
                    raise ConnectorError("ERROR 1105 (HY000): handshake mentions rs-secret-pw")
                return FakeReadyset.run(self, sql, extra_flags=extra_flags, timeout=timeout)

        ok, detail = readyset.identify(CommentFailConn())
        self.assertFalse(ok)
        self.assertNotIn("rs-secret-pw", detail)
        self.assertIn("[redacted]", detail)

    def test_error_redaction_precedes_truncation(self):
        class ErrorConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql.startswith(self.failing_sql):
                    raise ConnectorError(self.error)
                return super().run(sql, extra_flags, timeout)

        for failing_sql in ("SELECT @@version_comment", "SHOW READYSET VERSION", "EXPLAIN CREATE"):
            for password in ("connection-secret-" * 32, "connection-secret-\n" + "tail" * 100):
                for url_first in (False, True):
                    with self.subTest(sql=failing_sql, multiline="\n" in password, url_first=url_first):
                        conn = ErrorConn()
                        conn.password = password
                        conn.failing_sql = failing_sql
                        url = "mysql://u:" + "url-secret-" * 40 + "@h/db"
                        details = (url, password) if url_first else (password, url)
                        conn.error = "ERROR 1105 (HY000): failed for %s via %s" % details
                        if failing_sql == "EXPLAIN CREATE":
                            result = readyset.explain_cache(conn, "SELECT 1")
                            reasons = [result[kind]["reason"] for kind in ("shallow", "deep")]
                            self.assertTrue(all(result[kind]["supported"] == "error"
                                                for kind in ("shallow", "deep")))
                        else:
                            ok, reason = readyset.identify(conn)
                            self.assertFalse(ok)
                            reasons = [reason, json.dumps(readyset.read_status(conn))]
                        for reason in reasons:
                            self.assertNotIn("connection-secret-", reason)
                            self.assertNotIn("url-secret-", reason)
                            self.assertNotIn("tailtail", reason)
                            self.assertIn("[redacted]", reason)
                            self.assertNotIn("\n", reason)
                        self.assertEqual(conn.writes, [])

    def test_non_readyset_comment_is_redacted_single_line_and_bounded(self):
        class CommentConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql == "SELECT @@version_comment":
                    return xml_rows([{"@@version_comment": self.comment}])
                return super().run(sql, extra_flags, timeout)

        conn = CommentConn()
        conn.password = "connection-secret-" * 32
        conn.comment = "mysql://u:url-secret@h " + conn.password + "\n\r\t\u202e\u2028\u2029" + "x" * 100
        ok, reason = readyset.identify(conn)
        self.assertFalse(ok)
        prefix = "mysql://u:[redacted]@h [redacted]"
        self.assertEqual(reason, "@@version_comment is %s, not Readyset" %
                         (prefix + "x" * (60 - len(prefix))))
        self.assertFalse(any(unicodedata.category(c) in ("Cc", "Cf") or c in "\u2028\u2029"
                             for c in reason))
        dumped = json.dumps(readyset.read_status(conn))
        for secret in ("connection-secret-", "url-secret"):
            self.assertNotIn(secret, reason)
            self.assertNotIn(secret, dumped)
        self.assertEqual(conn.writes, [])


class FakeCacheExport(FakeReadyset):
    definition = "CREATE CACHE c FROM SELECT 1;"
    export_rows = None

    def run(self, sql, extra_flags=None, timeout=None):
        if sql in ("EXPLAIN ALL CACHES", "EXPLAIN CACHES"):
            return xml_rows(self.export_rows if self.export_rows is not None else
                            [{"query": self.definition}])
        return super().run(sql, extra_flags, timeout)


class TestExplainAndExportCaches(ReadysetBase):

    def test_export_comments_client_commands_and_additional_statements(self):
        # probe10_passthrough: a mysql client must never read server text as
        # a client command or as a second statement when importing this file.
        definitions = (
            "CREATE CACHE a FROM SELECT 1;\n\\! id",
            "CREATE CACHE a FROM SELECT 1;\nDELIMITER //\nDROP ALL CACHES//",
            "CREATE CACHE a FROM SELECT 1 \\T f",
            "CREATE CACHE a FROM SELECT 1 \\. f",
            "CREATE CACHE a FROM SELECT 1; ALTER READYSET SHUTDOWN",
            "CREATE CACHE a FROM SELECT 1 \\G",
            "CREATE CACHE a FROM SELECT 1 \\g",
            "CREATE CACHE a FROM SELECT 1 \\N",
            "CREATE CACHE a FROM SELECT 1 \\q",
            "DELIMITER //",
            "SELECT 1;",
            "USE `shop`; USE `x`; CREATE CACHE a FROM SELECT 1;",
            "USE `shop`; DROP ALL CACHES;",
            "USE `shop`; CREATE CACHE a FROM SELECT 1; DROP ALL CACHES;",
            "USE shop; CREATE CACHE a FROM SELECT 1;",
            "USE 'shop'; CREATE CACHE a FROM SELECT 1;",
            'USE "shop"; CREATE CACHE a FROM SELECT 1;',
            "USE ``; CREATE CACHE a FROM SELECT 1;",
            "USE `unfinished; CREATE CACHE a FROM SELECT 1;",
            "USE `shop`.`other`; CREATE CACHE a FROM SELECT 1;",
            "USE /* comment */ `shop`; CREATE CACHE a FROM SELECT 1;",
            "USE `shop` /* comment */; CREATE CACHE a FROM SELECT 1;",
            "USE `shop` CREATE CACHE a FROM SELECT 1;",
            "USE `shop\\name`; CREATE CACHE a FROM SELECT 1;",
            "USE `shop\nname`; CREATE CACHE a FROM SELECT 1;",
            "USE `shop`;\nCREATE CACHE a FROM SELECT 1;",
            "USEFUL `shop`; CREATE CACHE a FROM SELECT 1;",
            "CREATE CACHE a FROM SELECT 1 /*! ; ALTER READYSET SHUTDOWN */;",
            "CREATE CACHE a FROM SELECT 1 /*rs+ create shallow */;",
            "CREATE CACHE a FROM SELECT 'unfinished",
            "CREATE CACHE a FROM SELECT 1 /* unfinished",
            "CREATE CACHE a FROM SELECT 1 -- terminator swallowed",
            "CREATE CACHE a FROM SELECT 1\n",
            "CREATE CACHE `a\nb` FROM SELECT 1;",
            "CREATE CACHE a FROM SELECT 'x\\'; ALTER READYSET SHUTDOWN; -- ';",
        )
        # A valid USE prefix must not make any earlier abuse case runnable.
        for definition in definitions + tuple("USE `shop`; " + sql for sql in definitions):
            with self.subTest(definition=definition):
                conn = FakeCacheExport()
                conn.definition = definition
                result = readyset.export_caches(conn)
                terminated = definition if definition.endswith(";") else definition + ";"
                expected = "\n".join("-- " + line for line in terminated.splitlines())
                self.assertEqual(result["caches"][0]["statement"], expected)
                self.assertIn(readyset._EXPORT_UNSAFE_COMMENT + "\n" + expected + "\n", result["sql"])
                self.assertTrue(all(line.startswith("-- ") for line in result["sql"].splitlines()))
                self.assertEqual(conn.writes, [])

    def test_export_extracts_readyset_use_prefix_and_groups_databases(self):
        shallow = ("CREATE SHALLOW CACHE `c` WITH (ttl = 30s) FROM "
                   "SELECT id FROM orders WHERE id = ?;")
        other = "CREATE CACHE a FROM SELECT 1;"
        for field in ("query", "create statement"):
            for column in (None, "different_column_database"):
                with self.subTest(field=field, column=column):
                    conn = FakeCacheExport()
                    conn.export_rows = [
                        {field: "USE `shop`; " + shallow, "database": column},
                        {field: "  USE `shop`; " + other, "database": column},
                        {field: "USE `next`; " + other, "database": column},
                    ]
                    result = readyset.export_caches(conn)
                    self.assertEqual(result["count"], 3)
                    self.assertEqual(result["caches"], [
                        {"database": "shop", "statement": shallow},
                        {"database": "shop", "statement": other},
                        {"database": "next", "statement": other},
                    ])
                    self.assertEqual(result["sql"].splitlines()[2:], [
                        "USE `shop`;", shallow, other, "USE `next`;", other,
                    ])
                    self.assertEqual(conn.writes, [])

    def test_export_use_prefix_round_trips_quoted_database(self):
        # R4 formerly rejected this valid Readyset prefix as a second
        # statement. Only the exporter-generated USE is now runnable.
        statement = "CREATE CACHE a FROM SELECT 1;"
        for quoted, database in (("`shop`", "shop"),
                                 ("`a``b`", "a`b"),
                                 ("```shop```", "`shop`"),
                                 ("` shop `", " shop "),
                                 ("`semi;colon`", "semi;colon")):
            with self.subTest(database=database):
                conn = FakeCacheExport()
                conn.definition = "USE %s; %s" % (quoted, statement)
                self.assertFalse(readyset._runnable_export_sql(conn.definition))
                result = readyset.export_caches(conn)
                self.assertEqual(result["caches"], [
                    {"database": database, "statement": statement},
                ])
                self.assertEqual(result["sql"].splitlines()[2:], [
                    "USE %s;" % quoted, statement,
                ])
                self.assertEqual(conn.writes, [])

    def test_export_use_prefix_preserves_redaction(self):
        conn = FakeCacheExport()
        conn.definition = "USE `shop`; CREATE CACHE c FROM SELECT 'rs-secret-pw';"
        result = readyset.export_caches(conn)
        statement = "-- CREATE CACHE c FROM SELECT '[redacted]';"
        self.assertEqual(result["caches"], [{"database": "shop", "statement": statement}])
        self.assertEqual(result["sql"].splitlines()[2:], [
            "USE `shop`;", readyset._EXPORT_REDACTED_COMMENT, statement,
        ])
        self.assertNotIn(conn.password, json.dumps(result))
        self.assertEqual(conn.writes, [])

    def test_export_keeps_only_single_create_cache_definitions_runnable(self):
        for definition in (
                "CREATE CACHE c FROM SELECT 1;",
                "CREATE SHALLOW CACHE `c` WITH (ttl = 30s) FROM SELECT 1;",
                "CREATE DEEP CACHE c FROM SELECT 'literal; \\! DELIMITER //';",
                "CREATE CACHE c FROM SELECT 'first\n\\! id\nlast';",
                "CREATE CACHE c FROM SELECT `password = 'identifier-only'` /* safe comment */;",
                "CREATE CACHE c FROM q_1234;"):
            with self.subTest(definition=definition):
                conn = FakeCacheExport()
                conn.definition = definition
                result = readyset.export_caches(conn)
                self.assertEqual(result["caches"][0]["statement"], definition)
                self.assertTrue(result["sql"].endswith("\n" + definition + "\n"))
                self.assertNotIn(readyset._EXPORT_UNSAFE_COMMENT, result["sql"])
                self.assertNotIn(readyset._EXPORT_REDACTED_COMMENT, result["sql"])
                self.assertEqual(conn.writes, [])

    def test_export_redacts_whole_credential_clauses_but_preserves_identifiers(self):
        # probe9_f1: the display path already redacts these clauses. The
        # export must see the keyword and literal together, across tokens.
        for clause, query, secret in (
                ("IDENTIFIED BY 'id-secret-4'", "SELECT 1; CREATE USER app IDENTIFIED BY 'id-secret-4'", "id-secret-4"),
                ("PASSWORD('fn-secret-1')", "SELECT id FROM users WHERE pass = PASSWORD('fn-secret-1')", "fn-secret-1"),
                ("password = 'pred-secret-2'", "SELECT id FROM users WHERE password = 'pred-secret-2'", "pred-secret-2"),
                ("api_password = 'pred-secret-3'", "SELECT id FROM accounts WHERE api_password = 'pred-secret-3' AND tenant = 7", "pred-secret-3")):
            with self.subTest(clause=clause):
                conn = FakeCacheExport()
                conn.definition = "CREATE SHALLOW CACHE c FROM %s;" % query
                conn.caches[0]["query"] = query
                payload = {"connection": {"host": "127.0.0.1", "port": 3307, "user": "root",
                                          "password": conn.password, "database": "shop"}}
                with mock.patch("myflames.connector.MySQLConnection", return_value=conn):
                    report = ui.api_response("/api/live/readyset", payload, self.workspace)
                    result = ui.api_response("/api/live/readyset/export-caches", payload, self.workspace)
                self.assertNotIn(secret, json.dumps(report))
                self.assertNotIn(secret, json.dumps(result))
                self.assertIn(dbops.REDACTED, result["caches"][0]["statement"])
                self.assertTrue(all(line.startswith("-- ") for line in result["sql"].splitlines()))
                self.assertIn(readyset._EXPORT_REDACTED_COMMENT, result["sql"])

                # Quotes and credential-looking words inside identifiers are
                # data. Preserve multiple identifiers even after a redaction
                # changes the offsets of the identifiers that follow it.
                conn.definition = "CREATE CACHE `%s` FROM SELECT `%s`, `a``b`, `\ue000`;" % (clause, clause)
                clean = readyset.export_caches(conn)
                self.assertEqual(clean["caches"][0]["statement"], conn.definition)
                self.assertNotIn(readyset._EXPORT_REDACTED_COMMENT, clean["sql"])
                conn.definition = "CREATE CACHE `%s` FROM %s, `%s`, `a``b`;" % (clause, query, clause)
                mixed = readyset.export_caches(conn)["caches"][0]["statement"]
                self.assertEqual(mixed.count("`%s`" % clause), 2)
                self.assertTrue(mixed.endswith(", `%s`, `a``b`;" % clause))
                self.assertIn(dbops.REDACTED, mixed)
                self.assertEqual(conn.writes, [])

    def test_export_password_with_at_has_no_tail_in_any_token_context(self):
        # probe4_order: match the original literal before any URL substitution.
        for password in ("Pr0d@Tail99", "another-connection-password"):
            for token in ("'mysql://readyset:Pr0d@Tail99@mysql-primary:3306/shop'",
                          '"mysql://readyset:Pr0d@Tail99@mysql-primary:3306/shop"',
                          "/* mysql://readyset:Pr0d@Tail99@mysql-primary:3306/shop */",
                          "mysql://readyset:Pr0d@Tail99@mysql-primary:3306/shop",
                          "/*!50000 , 'mysql://readyset:Pr0d@Tail99@mysql-primary:3306/shop' */"):
                with self.subTest(password=password, token=token):
                    conn = FakeCacheExport()
                    conn.password = password
                    conn.definition = "CREATE CACHE c FROM SELECT %s;" % token
                    result = readyset.export_caches(conn)
                    self.assertNotIn("Pr0d", json.dumps(result))
                    self.assertNotIn("Tail99", json.dumps(result))
                    statement = result["caches"][0]["statement"]
                    self.assertTrue(all(line.startswith("-- ") for line in statement.splitlines()))
                    self.assertIn(statement + "\n", result["sql"])
                    self.assertEqual(conn.writes, [])
        self.assertEqual(readyset._redact_export_sql("SELECT 'Pr0d@Tail99';", "Pr0d@Tail99"),
                         ("SELECT '[redacted]';", True))

    def test_export_url_pattern_handles_400kb_in_under_two_seconds(self):
        # A failed bounded match must not retry a long scheme at each byte,
        # or rescan the same authority suffix for every candidate '@'.
        for body in ("a" * 400000 + "://u:secret@h",
                     "mysql://" + "u" * 400000 + ":secret@h",
                     "mysql://u:" + "x" * 400000,
                     "mysql://u:" + "x@" * 200000,
                     "mysql://u:" * 40000):
            with self.subTest(prefix=body[:30]):
                started = time.monotonic()
                redacted = readyset._EXPORT_URL_CREDENTIALS.sub(
                    lambda match: match.group(1) + dbops.REDACTED + "@", body)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 2.0, "400 KB URL pattern took %.3f s" % elapsed)
                if body.endswith(":secret@h"):
                    self.assertNotIn(":secret@h", redacted)

    def test_export_version_header_is_redacted_single_line_and_bounded(self):
        conn = FakeCacheExport()
        conn.password = "connection-secret-" * 32
        conn.version = ("stable\nDROP CACHE planted;\r\n" + conn.password +
                        "\t\u202e\u2028\u2029mysql://u:url-secret@h " + "x" * 100)
        result = readyset.export_caches(conn)
        self.assertEqual(len(result["version"]), 80)
        self.assertTrue(result["version"].startswith(
            "stableDROP CACHE planted;[redacted]mysql://u:[redacted]@h "))
        self.assertFalse(any(unicodedata.category(c) in ("Cc", "Cf") or c in "\u2028\u2029"
                             for c in result["version"]))
        self.assertEqual(result["sql"].splitlines(), [
            "-- Readyset cache definitions exported by myflames (Readyset %s)" % result["version"],
            "-- Credentials in query text are shown as [redacted].",
            "CREATE CACHE c FROM SELECT 1;",
        ])
        self.assertNotIn("connection-secret-", json.dumps(result))
        self.assertNotIn("url-secret", json.dumps(result))
        self.assertEqual(conn.writes, [])

    def test_export_redacts_url_password_with_slash(self):
        for password in ("secret/part", "prefix/" + "x" * 1017):
            for token in ("'mysql://u:%s@h/db'", '"mysql://u:%s@h/db"',
                          "/* mysql://u:%s@h/db */", "mysql://u:%s@h/db",
                          "'postgresql://:%s@h/db'"):
                with self.subTest(length=len(password), token=token):
                    conn = FakeCacheExport()
                    identifier = "`mysql://u:identifier/part@h`"
                    conn.definition = "SELECT %s, %s;" % (identifier, token % password)
                    result = readyset.export_caches(conn)
                    expected = "-- SELECT %s, %s;" % (identifier, token % dbops.REDACTED)
                    self.assertEqual(result["caches"][0]["statement"], expected)
                    self.assertIn(readyset._EXPORT_REDACTED_COMMENT + "\n" + expected, result["sql"])
                    self.assertNotIn(password, json.dumps(result))
                    self.assertEqual(conn.writes, [])

    def test_export_redacts_connection_password_in_comments(self):
        for comment, replacement in (("/* before rs-secret-pw after */", "/* [redacted] */"),
                                     ("-- before rs-secret-pw after", "-- [redacted]"),
                                     ("# before rs-secret-pw after", "# [redacted]"),
                                     ("/* before\nrs-secret-pw\nafter */", "/* [redacted] */")):
            with self.subTest(comment=comment):
                conn = FakeCacheExport()
                conn.definition = "CREATE CACHE c FROM SELECT 1 %s\n;" % comment
                result = readyset.export_caches(conn)
                statement = result["caches"][0]["statement"]
                self.assertEqual(statement, "-- CREATE CACHE c FROM SELECT 1 %s\n-- ;" % replacement)
                self.assertTrue(all(line.startswith("-- ") for line in statement.splitlines()))
                self.assertIn(readyset._EXPORT_REDACTED_COMMENT + "\n" + statement, result["sql"])
                self.assertNotIn(conn.password, json.dumps(result))
                self.assertEqual(conn.writes, [])

    def test_explain_collision_keeps_exact_server_name(self):
        for name in (" c_orders_deep ", "   ", "`literal`", "a``b", None, ""):
            with self.subTest(name=name):
                conn = FakeReadyset()
                conn.caches[0]["name"] = name
                result = readyset.explain_cache(conn, "q_1111aaaa")
                expected = "q_1111aaaa" if name in (None, "") else name
                self.assertEqual(result["collisions"][0]["name"], expected)
                self.assertEqual(result["collisions"][0]["name"],
                                 readyset.read_status(conn)["caches"][0]["name"])

    def test_explain_query_redacts_password_and_url_credentials(self):
        class SecretQueryConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql.upper().startswith("EXPLAIN CREATE"):
                    return xml_rows([{
                        "query_id": "q_1234", "readyset_supported": "yes",
                        self.query_field: "SELECT 'rs-secret-pw', 'mysql://u:urlpw@h:3306/db'",
                    }])
                return super().run(sql, extra_flags, timeout)

        for field in ("query", "proxied query"):
            with self.subTest(field=field):
                conn = SecretQueryConn()
                conn.query_field = field
                result = readyset.explain_cache(conn, "q_1234")
                for kind in ("shallow", "deep"):
                    self.assertEqual(result[kind]["query"],
                                     "SELECT '[redacted]', 'mysql://u:[redacted]@h:3306/db'")
                dumped = json.dumps(result)
                self.assertNotIn(conn.password, dumped)
                self.assertNotIn("urlpw", dumped)
                self.assertEqual(conn.writes, [])

    def test_export_redacts_queries_in_both_formats_and_marks_header(self):
        class SecretExportConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql == "EXPLAIN ALL CACHES" and self.legacy_explain_caches:
                    raise ConnectorError("ERROR 1064 (42000): syntax error")
                if sql in ("EXPLAIN ALL CACHES", "EXPLAIN CACHES"):
                    return xml_rows([{
                        "database": "shop",
                        self.query_field: ("CREATE SHALLOW CACHE `secret` FROM SELECT "
                                           "'rs-secret-pw', 'mysql://u:urlpw@h:3306/db'"),
                    }])
                return super().run(sql, extra_flags, timeout)

        for legacy in (False, True):
            for field in ("query", "create statement"):
                with self.subTest(legacy=legacy, field=field):
                    conn = SecretExportConn()
                    conn.legacy_explain_caches = legacy
                    conn.query_field = field
                    result = readyset.export_caches(conn)
                    expected = ("-- CREATE SHALLOW CACHE `secret` FROM SELECT "
                                "'[redacted]', 'mysql://u:[redacted]@h:3306/db';")
                    self.assertEqual(result["count"], 1)
                    self.assertEqual(result["caches"], [{"database": "shop", "statement": expected}])
                    self.assertEqual(result["sql"].splitlines(), [
                        "-- Readyset cache definitions exported by myflames (Readyset stable-250605)",
                        "-- Credentials in query text are shown as [redacted].",
                        "USE `shop`;",
                        "-- Credentials in this definition are shown as [redacted]; it will not run as is.",
                        expected,
                    ])
                    dumped = json.dumps(result)
                    self.assertNotIn(conn.password, dumped)
                    self.assertNotIn("urlpw", dumped)
                    self.assertEqual(conn.writes, [])

    def test_export_redaction_keeps_identifiers_and_flags_changed_definitions(self):
        class CustomExportConn(FakeReadyset):
            def __init__(self, password, rows):
                super().__init__()
                self.password = password
                self._export_rows = rows

            def run(self, sql, extra_flags=None, timeout=None):
                if sql in ("EXPLAIN ALL CACHES", "EXPLAIN CACHES"):
                    return xml_rows(self._export_rows)
                return super().run(sql, extra_flags, timeout)

        # Password matching an identifier or keyword leaves DDL byte for byte
        # intact and adds no per-definition warning comment.
        ident_stmt = "CREATE SHALLOW CACHE sales_summary FROM SELECT * FROM sales;"
        clean = readyset.export_caches(CustomExportConn("sales", [
            {"database": "sales", "query": ident_stmt},
            {"database": "sales", "query": "CREATE DEEP CACHE `sales` FROM SELECT `sales` FROM `sales` WHERE status = 'open';"},
        ]))
        self.assertEqual(clean["caches"][0]["statement"], ident_stmt)
        self.assertEqual(
            clean["caches"][1]["statement"],
            "CREATE DEEP CACHE `sales` FROM SELECT `sales` FROM `sales` WHERE status = 'open';",
        )
        self.assertEqual(clean["sql"].splitlines(), [
            "-- Readyset cache definitions exported by myflames (Readyset stable-250605)",
            "-- Credentials in query text are shown as [redacted].",
            "USE `sales`;",
            ident_stmt,
            "CREATE DEEP CACHE `sales` FROM SELECT `sales` FROM `sales` WHERE status = 'open';",
        ])

        # Literal password in WHERE (single/double-quoted or escaped) and URL
        # credentials inside literals are redacted and flagged; untouched
        # definitions in the same export get no comment.
        mixed = readyset.export_caches(CustomExportConn("sales", [
            {"database": "shop", "query": ident_stmt},
            {"database": "shop", "query": "CREATE SHALLOW CACHE `by_region` FROM SELECT * FROM sales WHERE region = 'sales';"},
            {"database": "shop", "query": 'CREATE SHALLOW CACHE `dq` FROM SELECT * FROM sales WHERE region = "my-sales-pw";'},
            {"database": "shop", "query": "CREATE SHALLOW CACHE `url` FROM SELECT 'mysql://u:p@h:3306/db' FROM orders;"},
        ]))
        self.assertEqual(mixed["caches"], [
            {"database": "shop", "statement": ident_stmt},
            {"database": "shop", "statement": "-- CREATE SHALLOW CACHE `by_region` FROM SELECT * FROM sales WHERE region = '[redacted]';"},
            {"database": "shop", "statement": "-- CREATE SHALLOW CACHE `dq` FROM SELECT * FROM sales WHERE region = '[redacted]';"},
            {"database": "shop", "statement": "-- CREATE SHALLOW CACHE `url` FROM SELECT 'mysql://u:[redacted]@h:3306/db' FROM orders;"},
        ])
        flag = "-- Credentials in this definition are shown as [redacted]; it will not run as is."
        self.assertEqual(mixed["sql"].splitlines(), [
            "-- Readyset cache definitions exported by myflames (Readyset stable-250605)",
            "-- Credentials in query text are shown as [redacted].",
            "USE `shop`;",
            ident_stmt,
            flag,
            "-- CREATE SHALLOW CACHE `by_region` FROM SELECT * FROM sales WHERE region = '[redacted]';",
            flag,
            "-- CREATE SHALLOW CACHE `dq` FROM SELECT * FROM sales WHERE region = '[redacted]';",
            flag,
            "-- CREATE SHALLOW CACHE `url` FROM SELECT 'mysql://u:[redacted]@h:3306/db' FROM orders;",
        ])

        # Escaped quotes inside literals matching the password and URLs in
        # backticked identifiers vs literals are handled safely.
        escaped = readyset.export_caches(CustomExportConn("o'reilly", [
            {"database": "shop", "query": "CREATE SHALLOW CACHE `esc` FROM SELECT * FROM t WHERE a = 'o''reilly' AND b = 'o\\'reilly';"},
            {"database": "shop", "query": "CREATE SHALLOW CACHE `ok` FROM SELECT `o'reilly` FROM `mysql://u:p@h`;"},
        ]))
        self.assertEqual(escaped["caches"], [
            {"database": "shop", "statement": "-- CREATE SHALLOW CACHE `esc` FROM SELECT * FROM t WHERE a = '[redacted]' AND b = '[redacted]';"},
            {"database": "shop", "statement": "CREATE SHALLOW CACHE `ok` FROM SELECT `o'reilly` FROM `mysql://u:p@h`;"},
        ])
        self.assertEqual(escaped["sql"].splitlines(), [
            "-- Readyset cache definitions exported by myflames (Readyset stable-250605)",
            "-- Credentials in query text are shown as [redacted].",
            "USE `shop`;",
            flag,
            "-- CREATE SHALLOW CACHE `esc` FROM SELECT * FROM t WHERE a = '[redacted]' AND b = '[redacted]';",
            "CREATE SHALLOW CACHE `ok` FROM SELECT `o'reilly` FROM `mysql://u:p@h`;",
        ])

        # Display redaction (read_status) stays unchanged.
        status_conn = FakeReadyset()
        status_conn.password = "orders"
        report = readyset.read_status(status_conn)
        self.assertIn("[redacted]", report["caches"][0]["query"])

    def test_export_redaction_handles_long_url_password(self):
        url_password = "url-secret-" * 32
        for token in ("'mysql://u:%s@h/db'", "/* mysql://u:%s@h/db */",
                      "mysql://u:%s@h/db"):
            with self.subTest(token=token):
                sql = "CREATE SHALLOW CACHE c FROM SELECT " + token % url_password
                expected = "CREATE SHALLOW CACHE c FROM SELECT " + token % dbops.REDACTED
                self.assertEqual(readyset._redact_export_sql(sql), (expected, True))

    def test_export_redaction_handles_double_quoted_url(self):
        sql = 'CREATE SHALLOW CACHE c FROM SELECT "mysql://u:p@h/db";'
        expected = 'CREATE SHALLOW CACHE c FROM SELECT "mysql://u:[redacted]@h/db";'
        self.assertEqual(readyset._redact_export_sql(sql), (expected, True))

    def test_export_redaction_preserves_url_in_identifier(self):
        sql = "CREATE SHALLOW CACHE `mysql://u:p@h` FROM SELECT `mysql://u:p@h`;"
        self.assertEqual(readyset._redact_export_sql(sql, "p"), (sql, False))
        mixed = "SELECT `mysql://u:p@h`, 'mysql://u:secret@h';"
        expected = "SELECT `mysql://u:p@h`, 'mysql://u:[redacted]@h';"
        self.assertEqual(readyset._redact_export_sql(mixed), (expected, True))

    def test_export_redaction_password_matches_redaction_marker(self):
        # A marker introduced by URL redaction is not an original password;
        # an existing marker inside a literal still is. Detecting the password
        # must disable the definition even if replacement leaves identical text.
        cases = (
            ("SELECT '[redacted]';", "SELECT '[redacted]';", True),
            ('SELECT "[redacted]";', "SELECT '[redacted]';", True),
            ("SELECT 'prefix[redacted]suffix';", "SELECT '[redacted]';", True),
            ("SELECT 'mysql://u:p@h';", "SELECT 'mysql://u:[redacted]@h';", True),
            ("SELECT 'mysql://u:[redacted]@h';", "SELECT '[redacted]';", True),
            ("SELECT 'mysql://u:p@h/[redacted]';", "SELECT '[redacted]';", True),
            ("SELECT 1 /* [redacted] */;", "SELECT 1 /* [redacted] */;", True),
            ("SELECT `[redacted]`;", "SELECT `[redacted]`;", False),
            ("SELECT 'unrelated';", "SELECT 'unrelated';", False),
        )
        for sql, expected, changed in cases:
            with self.subTest(sql=sql):
                self.assertEqual(readyset._redact_export_sql(sql, dbops.REDACTED),
                                 (expected, changed))
                conn = FakeCacheExport()
                conn.password = dbops.REDACTED
                conn.definition = "CREATE CACHE c FROM " + sql
                result = readyset.export_caches(conn)
                statement = ("-- " if changed else "") + "CREATE CACHE c FROM " + expected
                self.assertEqual(result["caches"][0]["statement"], statement)
                self.assertEqual(readyset._EXPORT_REDACTED_COMMENT in result["sql"], changed)
                self.assertEqual(conn.writes, [])

    def test_redacted_export_comments_every_line_in_sql_and_structured_items(self):
        class MultilineExportConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql in ("EXPLAIN ALL CACHES", "EXPLAIN CACHES"):
                    return xml_rows([{"database": "shop", "query": self.definition}])
                return super().run(sql, extra_flags, timeout)

        for line_break in ("\n", "\r\n", "\r", "\u2028", "\u2029"):
            with self.subTest(line_break=repr(line_break)):
                conn = MultilineExportConn()
                conn.definition = line_break.join((
                    "CREATE SHALLOW CACHE c FROM", "SELECT 'rs-secret-pw'", "FROM orders;"))
                result = readyset.export_caches(conn)
                expected = "-- CREATE SHALLOW CACHE c FROM\n-- SELECT '[redacted]'\n-- FROM orders;"
                self.assertEqual(result["caches"][0]["statement"], expected)
                self.assertIn(readyset._EXPORT_REDACTED_COMMENT + "\n" + expected + "\n", result["sql"])
                self.assertTrue(all(line.startswith("-- ") for line in expected.splitlines()))
                self.assertNotIn(conn.password, json.dumps(result))
                self.assertEqual(conn.writes, [])

    def test_explain_cache_checks_both_modes_and_flags_collisions(self):
        conn = FakeReadyset()
        res = readyset.explain_cache(conn, "SELECT COUNT(*) FROM orders WHERE status = 'open'", cache_type="both")
        self.assertEqual(res["schema_version"], "readyset-explain-1.0")
        self.assertEqual(res["shallow"]["supported"], "yes")
        self.assertEqual(res["deep"]["supported"], "yes")
        self.assertEqual(res["collisions"], [])

        colliding = readyset.explain_cache(conn, "q_1111aaaa", cache_type="both")
        self.assertEqual(len(colliding["collisions"]), 1)
        self.assertEqual(colliding["collisions"][0]["name"], "c_orders_deep")

        unsupported = readyset.explain_cache(conn, "SELECT RAND(), id FROM orders", cache_type="deep")
        self.assertIsNone(unsupported["shallow"])
        self.assertEqual(unsupported["deep"]["supported"], "unsupported")
        self.assertIn("RAND", unsupported["deep"]["reason"])

        # Refused on Admin port
        admin_conn = FakeReadyset(port=6036, database=None, admin=True)
        with self.assertRaisesRegex(ValueError, "SQL port"):
            readyset.explain_cache(admin_conn, "SELECT 1")

    def test_abuse_secrets_never_reach_explain_result_or_refusal_text(self):
        # A planted EXPLAIN CREATE CACHE reason is redacted in the explain result.
        conn = FakeReadyset()
        conn.explain_support = (
            "unsupported: routed via mysql://u:secret@h:3306/db "
            "IDENTIFIED BY 'x' for rs-secret-pw"
        )
        res = readyset.explain_cache(conn, "SELECT id FROM t WHERE u = 'x'", cache_type="both")
        dumped = json.dumps(res)
        self.assertNotIn("rs-secret-pw", dumped)
        self.assertNotIn("secret@h", dumped)
        self.assertNotIn("IDENTIFIED BY 'x'", dumped)
        self.assertIn("[redacted]", dumped)

        # A state value carrying secrets reaches an ActionUnavailable refusal
        # (why_not + " " + fix) only redacted and display-safe.
        paused = FakeReadyset()
        paused.repl_status = "Paused mysql://u:secret@h IDENTIFIED BY 'x' rs-secret-pw"
        report = readyset.read_status(paused)
        entry = {a["id"]: a for a in report["actions"]}["start_replication"]
        self.assertFalse(entry["enabled"])
        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
            readyset.plan_action(report, "start_replication")
        refusal = str(caught.exception)
        self.assertEqual(refusal, "%s %s" % (entry["why_not"], entry["fix"]))
        for secret in ("rs-secret-pw", "secret@h", "IDENTIFIED BY 'x'"):
            self.assertNotIn(secret, refusal)
            self.assertNotIn(secret, json.dumps(report["actions"]))
        self.assertIn("[redacted]", refusal)

    def test_export_caches_returns_ddl_statements(self):
        conn = FakeReadyset()
        exp = readyset.export_caches(conn)
        self.assertEqual(exp["schema_version"], "readyset-export-1.0")
        self.assertEqual(exp["count"], 2)
        self.assertIn("CREATE DEEP CACHE", exp["sql"])
        self.assertIn("CREATE SHALLOW CACHE", exp["sql"])
        fixture = ROOT / "apps" / "workspace" / "test" / "fixtures" / "readyset-export-caches.json"
        self.assertEqual(exp, json.loads(fixture.read_text(encoding="utf-8")))

        legacy = FakeReadyset()
        legacy.legacy_explain_caches = True
        exp_legacy = readyset.export_caches(legacy)
        self.assertEqual(exp_legacy["count"], 2)
        self.assertIn("CREATE DEEP CACHE", exp_legacy["sql"])
        self.assertNotIn(readyset._EXPORT_UNSAFE_COMMENT, exp_legacy["sql"])
        self.assertEqual(exp_legacy["caches"][1]["database"], "shop")
        self.assertTrue(exp_legacy["caches"][1]["statement"].startswith("CREATE SHALLOW CACHE"))
        self.assertTrue(any("EXPLAIN CACHES" in n for n in exp_legacy["notes"]))

    def test_ui_live_endpoints_accept_workspace_payload_shapes(self):
        from contextlib import contextmanager
        from unittest import mock
        from myflames import ui

        fake = FakeReadyset()

        @contextmanager
        def fake_conn(**_kwargs):
            yield fake

        conn_payload = {"host": "127.0.0.1", "port": 3307, "user": "root", "database": "shop"}
        with mock.patch("myflames.connector.MySQLConnection", side_effect=fake_conn):
            exp = ui._discover_response("/api/live/readyset/explain-cache", {
                "connection": conn_payload,
                "query_or_id": "q_1111aaaa",
                "cache_type": "both",
            })
            self.assertEqual(exp["schema_version"], "readyset-explain-1.0")
            self.assertEqual(len(exp["collisions"]), 1)
            self.assertEqual(exp["collisions"][0]["name"], "c_orders_deep")

            ddl = ui._discover_response("/api/live/readyset/export-caches", {
                "connection": conn_payload,
            })
            self.assertEqual(ddl["schema_version"], "readyset-export-1.0")
            self.assertEqual(ddl["count"], 2)


class TestActionsAndAbuseCases(ReadysetBase):

    def connection(self, environment="development", read_only=False):
        return {
            "host": "127.0.0.1", "port": 3307, "user": "root", "password": "rs-secret-pw",
            "database": "shop", "environment": environment, "read_only": read_only,
        }

    def api_plan(self, conn, action, target=None, connection=None):
        payload = {"connection": connection or self.connection(), "action": action, "target": target or {}}
        with mock.patch("myflames.connector.MySQLConnection", return_value=conn):
            return ui.api_response("/api/live/readyset/plan", payload, self.workspace)

    def api_send(self, conn, plan, connection=None, token=None, typed=None, confirmed=True):
        c = connection or self.connection()
        plan_copy = dict(plan)
        own = plan_copy.pop("review_token", None)
        sent_typed = plan_copy.get("typed_guard") if typed is None else typed
        payload = {
            "connection": c,
            "plan": plan_copy,
            "review_token": own if token is None else token,
            "typed": sent_typed,
            "confirmed": confirmed,
        }
        with mock.patch("myflames.connector.MySQLConnection", return_value=conn):
            return ui.api_response("/api/live/readyset/action", payload, self.workspace)

    def test_version_is_redacted_in_all_six_sinks(self):
        # probe6_sinks2 and probe7_audit: sanitizing only the export header
        # leaves the report, review expectations and persisted audit exposed.
        for password in ("ConnPlain_77", "Pr0d@Tail99"):
            with self.subTest(password=password):
                conn = FakeReadyset()
                conn.password = password
                conn.version = "stable %s\u202e mysql://u:version-secret@h" % password
                connection = dict(self.connection(), password=password)
                expected = "stable [redacted] mysql://u:[redacted]@h"
                with mock.patch("myflames.connector.MySQLConnection", return_value=conn):
                    report = ui.api_response("/api/live/readyset", {"connection": connection}, self.workspace)
                    explain = ui.api_response("/api/live/readyset/explain-cache", {
                        "connection": connection, "query_or_id": "SELECT 1"}, self.workspace)
                plan = self.api_plan(conn, "flush_all_shallow_caches", connection=connection)
                result = self.api_send(conn, plan, connection=connection)
                self.assertEqual(result["status"], "done", result)
                self.assertEqual(conn.writes, ["FLUSH ALL SHALLOW CACHES;"])
                audit = self.workspace.store.audit_records()[0]
                self.assertEqual(report["version"], expected)
                self.assertEqual(report["topology"]["this_server"]["version"], expected)
                self.assertEqual(explain["version"], expected)
                self.assertEqual(plan["expect"]["readyset_version"], expected)
                self.assertIn(expected, json.dumps(plan["facts"]))
                self.assertEqual(audit["connection"]["server_version"], expected)
                text = readyset.format_text(report)
                self.assertIn(expected, text)
                sinks = {"report": report, "topology": report["topology"], "explain": explain,
                         "plan": plan, "audit": audit, "format_text": text}
                for name, sink in sinks.items():
                    with self.subTest(sink=name):
                        dumped = json.dumps(sink, ensure_ascii=False)
                        for secret in (password, "Tail99", "version-secret", "\u202e", "\\u202e"):
                            self.assertNotIn(secret, dumped)

    def test_every_operational_action_sends_exact_reviewed_sql(self):
        cases = [
            ("create_cache", {
                "cache_type": "shallow", "name": "c_open_orders", "query_id": "q_3333cccc",
                "ttl_seconds": 45, "refresh_seconds": 15, "coalesce_ms": 100, "adaptive": True,
            }, "CREATE SHALLOW CACHE `c_open_orders` WITH (POLICY TTL 45 SECONDS REFRESH EVERY 15 SECONDS, COALESCE 100 MS, ADAPTIVE) FROM q_3333cccc;"),
            ("create_cache", {
                "cache_type": "deep", "name": "c_deep_q",
                "query": "SELECT COUNT(*) FROM orders WHERE status = 'paid'", "until_write": True,
            }, "CREATE DEEP CACHE `c_deep_q` WITH (UNTIL WRITE) FROM SELECT COUNT(*) FROM orders WHERE status = 'paid';"),
            ("flush_cache", {"name": "c_recent_shallow"}, "FLUSH CACHE `c_recent_shallow`;"),
            ("flush_all_shallow_caches", {}, "FLUSH ALL SHALLOW CACHES;"),
            ("drop_cache", {"name": "c_orders_deep"}, "DROP CACHE `c_orders_deep`;"),
            ("enter_maintenance_mode", {}, "ALTER READYSET ENTER MAINTENANCE MODE;"),
            ("exit_maintenance_mode", {}, "ALTER READYSET EXIT MAINTENANCE MODE;"),
            ("set_log_level", {"directives": "info,readyset_server=debug"},
             "ALTER READYSET SET LOG LEVEL 'info,readyset_server=debug';"),
            ("set_eviction_memory_limit", {"bytes": 536870912},
             "ALTER READYSET SET EVICTION MEMORY LIMIT 536870912;"),
            ("set_eviction_period", {"milliseconds": 5000},
             "ALTER READYSET SET EVICTION PERIOD 5000;"),
            ("add_shallow_allowed", {"kind": "function", "identifier": "CURDATE"},
             "ALTER READYSET ADD SHALLOW CACHE ALLOWED FUNCTION `CURDATE`;"),
            ("drop_shallow_allowed", {"kind": "function", "identifier": "NOW"},
             "ALTER READYSET DROP SHALLOW CACHE ALLOWED FUNCTION `NOW`;"),
            ("flush_privileges", {}, "ALTER READYSET FLUSH PRIVILEGES;"),
            ("resnapshot_table", {"table": "shop.orders"},
             "ALTER READYSET RESNAPSHOT TABLE `shop`.`orders`;"),
            ("add_tables", {"tables": ["shop.orders", "shop.items"]},
             "ALTER READYSET ADD TABLES `shop`.`orders`, `shop`.`items`;"),
            ("stop_replication", {}, "ALTER READYSET STOP REPLICATION;"),
            ("start_replication", {}, "ALTER READYSET START REPLICATION;"),
            ("set_replication_position", {"position": "aaaa:1-120"},
             "ALTER READYSET SET REPLICATION POSITION 'aaaa:1-120';"),
            ("drop_all_proxied_queries", {}, "DROP ALL PROXIED QUERIES;"),
            ("drop_all_shallow_caches", {}, "DROP ALL SHALLOW CACHES;"),
            ("drop_all_deep_caches", {}, "DROP ALL DEEP CACHES;"),
            ("drop_all_caches", {}, "DROP ALL CACHES;"),
        ]
        for action, target, expected_sql in cases:
            with self.subTest(action=action, expected_sql=expected_sql):
                conn = FakeReadyset()
                if action == "exit_maintenance_mode":
                    conn.status_text = "Maintenance Mode"
                elif action == "start_replication":
                    conn.repl_status = "Stopped"
                plan = self.api_plan(conn, action, target)
                self.assertEqual(plan["statements"], [expected_sql])
                res = self.api_send(conn, plan)
                self.assertEqual(res["status"], "done", res)
                self.assertEqual(conn.writes, [expected_sql])

    def test_default_database_change_refuses_create_and_add_tables_with_one_audit_row(self):
        # Readyset resolves unqualified ADD TABLES against its search path
        # (backend/noria_connector.rs:1034-1042), and CREATE CACHE carries that
        # path and database (backend/extensions.rs:1684-1693).
        for action, target in (
                ("create_cache", {"cache_type": "shallow", "query": "SELECT id FROM orders"}),
                ("add_tables", {"tables": ["orders"]})):
            for before, after in (("shop", "archive"), (" shop", "shop")):
                with self.subTest(action=action, before=before, after=after):
                    conn = FakeReadyset(database=before)
                    plan = self.api_plan(conn, action, target)
                    self.assertEqual(plan["expect"]["session_database"], before)
                    self.assertIn(["Default database", before], plan["facts"])
                    audit_before = len(self.workspace.store.audit_records())
                    conn.database = after
                    result = self.api_send(conn, plan)
                    self.assertEqual(result["status"], "refused", result)
                    self.assertEqual(result["outcome"], "changed", result)
                    self.assertEqual(result["states"], ["not_sent"])
                    self.assertEqual(conn.writes, [])
                    records = self.workspace.store.audit_records()
                    self.assertEqual(len(records), audit_before + 1)
                    self.assertEqual(records[0]["status"], "refused")
                    self.assertEqual(records[0]["outcome"], "changed")

    def test_resnapshot_guard_is_reported_name_and_rejects_caller_alias(self):
        conn = FakeReadyset()
        plan = self.api_plan(conn, "resnapshot_table", {"table": "shop.orders"})
        self.assertEqual(plan["typed_guard"], "`shop`.`orders`")
        self.assertEqual(plan["expect"]["table"], "`shop`.`orders`")
        rejected = self.api_send(conn, plan, typed="shop.orders")
        self.assertEqual(rejected["status"], "refused")
        self.assertEqual(rejected["outcome"], "typed_guard")
        self.assertEqual(conn.writes, [])
        accepted = self.api_send(conn, plan, typed="`shop`.`orders`")
        self.assertEqual(accepted["status"], "done")
        self.assertEqual(conn.writes, ["ALTER READYSET RESNAPSHOT TABLE `shop`.`orders`;"])

    def test_admin_shutdown_actions_refused_on_adapter_and_allowed_on_admin(self):
        adapter = FakeReadyset(port=3307, database="shop", admin=False)
        with self.assertRaisesRegex(ValueError, "admin SQL port"):
            self.api_plan(adapter, "shutdown")
        admin = FakeReadyset(port=6036, database=None, admin=True)
        conn_cfg = dict(self.connection(environment="production"), port=6036, database=None)
        plan = self.api_plan(admin, "shutdown_reset", {}, connection=conn_cfg)
        self.assertEqual(plan["statements"], ["ALTER READYSET SHUTDOWN RESET;"])
        self.assertEqual(plan["policy"]["action"], "confirm")
        self.assertEqual(plan["typed_guard"], "SHUTDOWN RESET")
        # Wrong typed target refuses without writing.
        bad = self.api_send(admin, plan, connection=conn_cfg, typed="wrong")
        self.assertEqual(bad["status"], "refused")
        self.assertEqual(admin.writes, [])
        # Exact target executes.
        plan2 = self.api_plan(admin, "shutdown_reset", {}, connection=conn_cfg)
        ok = self.api_send(admin, plan2, connection=conn_cfg, typed=plan2["typed_guard"])
        self.assertEqual(ok["status"], "done", ok)
        self.assertEqual(admin.writes, ["ALTER READYSET SHUTDOWN RESET;"])

    def test_duplicate_cache_collision_across_shallow_and_deep_sets_typed_guard_and_warning(self):
        conn = FakeReadyset()
        plan = self.api_plan(conn, "create_cache", {"cache_type": "shallow", "query_id": "q_1111aaaa"})
        self.assertTrue(plan["warnings"])
        self.assertEqual(plan["typed_guard"], "c_orders_deep")
        plan2 = self.api_plan(conn, "create_cache", {
            "cache_type": "deep", "name": "c_recent_shallow", "query_id": "q_3333cccc",
        })
        self.assertEqual(plan2["typed_guard"], "c_recent_shallow")

    def test_abuse_cases_comment_crlf_semicolon_and_token_replay(self):
        conn = FakeReadyset()
        # Line comment in CREATE CACHE query text must be rejected so trailing tokens cannot be commented out.
        for bad_query in (
            "SELECT * FROM orders -- trailing comment",
            "SELECT * FROM orders # hash comment",
            "SELECT 1; DROP ALL CACHES",
            "UPDATE orders SET total = 0",
        ):
            with self.subTest(bad_query=bad_query):
                with self.assertRaises(ValueError):
                    self.api_plan(conn, "create_cache", {"cache_type": "shallow", "query": bad_query})
        # Control / newline injection in identifiers and string literals.
        for action, target in (
            ("create_cache", {"cache_type": "shallow", "name": "bad\rname", "query_id": "q_3333cccc"}),
            ("set_log_level", {"directives": "info'\nALTER READYSET SHUTDOWN"}),
            ("set_replication_position", {"position": "aaaa:1-5\r\nDROP ALL CACHES"}),
            ("resnapshot_table", {"table": "shop.orders\nDROP CACHE x"}),
            ("add_shallow_allowed", {"kind": "function", "identifier": "NOW\x00"}),
        ):
            with self.subTest(action=action, target=target):
                with self.assertRaises(ValueError):
                    self.api_plan(conn, action, target)

        # Backtick inside cache name is escaped by doubling, never breaking identifier quoting.
        plan = self.api_plan(conn, "create_cache", {
            "cache_type": "deep", "name": "weird`name", "query_id": "q_3333cccc",
        })
        self.assertEqual(plan["statements"], ["CREATE DEEP CACHE `weird``name` FROM q_3333cccc;"])
        self.assertEqual(conn.writes, [])

        # Read-only profile refuses before issuing writes.
        ro = self.connection(read_only=True)
        ro_plan = self.api_plan(conn, "flush_all_shallow_caches", connection=ro)
        self.assertEqual(ro_plan["policy"]["action"], "refuse")
        res_ro = self.api_send(conn, ro_plan, connection=ro)
        self.assertEqual(res_ro["status"], "refused")
        self.assertEqual(conn.writes, [])

        # Review token is single-use and bound to exact statements.
        tampered = dict(plan, statements=["DROP ALL CACHES;"])
        res_tampered = self.api_send(conn, tampered, token=plan["review_token"])
        self.assertEqual(res_tampered["status"], "refused")
        self.assertEqual(conn.writes, [])

    def test_post_error_verification_reports_partial_and_unknown_honestly(self):
        # 1. Server reported an error at line 1 after applying state change -> "partial"
        conn = FakeReadyset()
        plan = self.api_plan(conn, "enter_maintenance_mode")
        conn.fail_on_write = "ERROR 1105 (HY000) at line 1: Controller timeout after switching mode using rs-secret-pw"
        res = self.api_send(conn, plan)
        self.assertEqual(res["status"], "partial")
        self.assertNotIn("rs-secret-pw", json.dumps(res))

        # 2. Server error at line 1 and follow-up read fails -> "unknown"
        conn2 = FakeReadyset()
        plan2 = self.api_plan(conn2, "enter_maintenance_mode")
        conn2.fail_on_write = "ERROR 1105 (HY000) at line 1: Controller error"
        conn2.fail_reads_after_write = True
        res2 = self.api_send(conn2, plan2)
        self.assertEqual(res2["status"], "unknown")

        # 3. Lost connection (2013) -> "unknown" without resending
        conn3 = FakeReadyset()
        plan3 = self.api_plan(conn3, "enter_maintenance_mode")
        conn3.fail_on_write = "ERROR 2013 (HY000): Lost connection to MySQL server during query"
        calls_at_send = []
        original_write = conn3.run_write
        def send(sql, **kwargs):
            calls_at_send.extend(conn3.calls)
            return original_write(sql, **kwargs)
        with mock.patch.object(conn3, 'run_write', side_effect=send):
            res3 = self.api_send(conn3, plan3)
        self.assertEqual(res3["status"], "unknown")
        self.assertEqual(res3["states"], ["unknown"])
        self.assertIsNone(res3["after"])
        self.assertIsNone(res3["effect"])
        self.assertEqual(conn3.writes, plan3["statements"])
        self.assertEqual(conn3.calls, calls_at_send, "a reply-less mysql-client error triggered an after-read")

    def test_cache_named_with_literal_backticks_plans_its_own_name(self):
        # SHOW CACHES prints names unquoted (extensions.rs:1113), so backticks
        # at both ends are part of the name and must be doubled, never stripped.
        conn = FakeReadyset()
        conn.caches.append({
            "query_id": "q_5555eeee",
            "name": "`c_orders_deep`",
            "query": "SELECT id FROM orders",
            "properties": "deep",
            "count": "7",
            "database": "shop",
            "kind": "deep",
        })
        plan = self.api_plan(conn, "drop_cache", {"name": "`c_orders_deep`"})
        self.assertEqual(plan["statements"], ["DROP CACHE ```c_orders_deep```;"])
        res = self.api_send(conn, plan)
        self.assertEqual(res["status"], "done", res)
        names = sorted(c["name"] for c in conn.caches)
        self.assertNotIn("`c_orders_deep`", names)
        self.assertIn("c_orders_deep", names, "the plain cache of the same letters must survive")

        # The reverse: the plain cache still plans its own plain name.
        plan2 = self.api_plan(conn, "drop_cache", {"name": "c_orders_deep"})
        self.assertEqual(plan2["statements"], ["DROP CACHE `c_orders_deep`;"])
        res2 = self.api_send(conn, plan2)
        self.assertEqual(res2["status"], "done", res2)
        self.assertEqual([c["name"] for c in conn.caches], ["c_recent_shallow"])

    def test_flush_cache_on_backticked_name_plans_doubled_backticks(self):
        conn = FakeReadyset()
        conn.caches.append({
            "query_id": "q_6666ffff",
            "name": "`c_recent_shallow`",
            "query": "SELECT id FROM orders",
            "properties": "shallow (ttl = 30s)",
            "count": "3",
            "database": "shop",
            "kind": "shallow",
        })
        plan = self.api_plan(conn, "flush_cache", {"name": "`c_recent_shallow`"})
        self.assertEqual(plan["statements"], ["FLUSH CACHE ```c_recent_shallow```;"])
        # Flushing the plain twin still names the plain twin.
        plan2 = self.api_plan(conn, "flush_cache", {"name": "c_recent_shallow"})
        self.assertEqual(plan2["statements"], ["FLUSH CACHE `c_recent_shallow`;"])

    def test_table_named_with_literal_backticks_resnapshots_exact_name(self):
        # SHOW READYSET ALL TABLES quotes every part and doubles inner
        # backticks (readyset-sql/src/ast/table.rs:122-131, dialect.rs:65-72),
        # so a table literally named `orders` prints as ```orders```.
        conn = FakeReadyset()
        conn.tables.append({"table": "`shop`.```orders```", "status": "Online", "description": ""})
        plan = self.api_plan(conn, "resnapshot_table", {"table": "`shop`.```orders```"})
        self.assertEqual(plan["statements"], ["ALTER READYSET RESNAPSHOT TABLE `shop`.```orders```;"])

    def test_user_typed_table_names_parse_once(self):
        conn = FakeReadyset()
        for typed in ("shop.orders", "`shop`.`orders`"):
            with self.subTest(typed=typed):
                plan = self.api_plan(conn, "add_tables", {"tables": [typed]})
                self.assertEqual(plan["statements"], ["ALTER READYSET ADD TABLES `shop`.`orders`;"])

    def test_resnapshot_tolerates_unparseable_other_rows(self):
        # P2: a line-break name listed before the selected table must not fail
        # the plan of a later, valid row; the bad row itself is still refused.
        conn = FakeReadyset()
        conn.tables.insert(0, {"table": "shop.bad\nname", "status": "Online", "description": ""})
        plan = self.api_plan(conn, "resnapshot_table", {"table": "`shop`.`orders`"})
        self.assertEqual(plan["statements"], ["ALTER READYSET RESNAPSHOT TABLE `shop`.`orders`;"])
        with self.assertRaises(ValueError):
            self.api_plan(conn, "resnapshot_table", {"table": "shop.bad\nname"})


class TestActionCatalog(ReadysetBase):
    """Contract 2 action catalog: always listed, enabled or why_not + fix."""

    DANGER_IDS = {
        "drop_cache",
        "drop_all_caches",
        "drop_all_shallow_caches",
        "drop_all_deep_caches",
        "drop_all_proxied_queries",
        "drop_shallow_allowed",
        "resnapshot_table",
        "enter_maintenance_mode",
        "stop_replication",
        "set_replication_position",
        "shutdown",
        "shutdown_reset",
    }
    SERVER_IDS = [
        "create_cache",
        "flush_all_shallow_caches",
        "drop_all_caches",
        "drop_all_shallow_caches",
        "drop_all_deep_caches",
        "drop_all_proxied_queries",
        "enter_maintenance_mode",
        "exit_maintenance_mode",
        "start_replication",
        "stop_replication",
        "set_replication_position",
        "add_tables",
        "set_log_level",
        "set_eviction_memory_limit",
        "set_eviction_period",
        "add_shallow_allowed",
        "flush_privileges",
        "shutdown",
        "shutdown_reset",
    ]
    CATALOG_KEYS = {"id", "label", "scope", "tone", "danger", "description", "enabled", "why_not", "fix"}

    def by_id(self, report):
        return {a["id"]: a for a in report["actions"]}

    def all_entries(self, report):
        entries = list(report["actions"])
        for row in report["caches"]:
            entries.extend(row["actions"])
        for row in report["tables"]:
            entries.extend(row["actions"])
        for row in report["proxied_queries"]:
            entries.extend(row["actions"])
        for kind in ("functions", "variables", "schemas"):
            entries.extend(report["allowlist_actions"][kind])
        return entries

    def test_every_catalog_entry_has_the_contract_fields(self):
        for conn in (FakeReadyset(), FakeReadyset(port=6036, database=None, admin=True)):
            report = readyset.read_status(conn)
            assert_matches_schema(self, report, SCHEMA)
            self.assertEqual([a["id"] for a in report["actions"]], self.SERVER_IDS)
            entries = self.all_entries(report)
            self.assertTrue(entries)
            for entry in entries:
                self.assertEqual(set(entry), self.CATALOG_KEYS, entry["id"])
                self.assertIsInstance(entry["danger"], bool)
                self.assertEqual(entry["tone"], "danger" if entry["danger"] else "default")
                self.assertTrue(entry["label"])
                self.assertTrue(entry["description"])
                if entry["enabled"]:
                    self.assertIsNone(entry["why_not"], entry["id"])
                    self.assertIsNone(entry["fix"], entry["id"])
                else:
                    self.assertTrue(entry["why_not"], entry["id"])
                    self.assertTrue(entry["fix"], entry["id"])
            for entry in report["actions"]:
                self.assertEqual(entry["scope"], "server")
            for entry in entries[len(report["actions"]):]:
                self.assertEqual(entry["scope"], "row")
            # Row catalogs attach per row and per allowlist kind.
            self.assertEqual([a["id"] for a in report["caches"][0]["actions"]], ["flush_cache", "drop_cache"])
            self.assertEqual([a["id"] for a in report["tables"][0]["actions"]], ["resnapshot_table"])
            self.assertEqual([a["id"] for a in report["proxied_queries"][0]["actions"]], ["create_cache"])
            self.assertEqual(
                [a["id"] for a in report["allowlist_actions"]["functions"]],
                ["add_shallow_allowed", "drop_shallow_allowed"],
            )

    def test_danger_set_equals_v350_tone_danger_set(self):
        report = readyset.read_status(FakeReadyset())
        for entry in self.all_entries(report):
            self.assertEqual(entry["danger"], entry["id"] in self.DANGER_IDS, entry["id"])

    def test_port_role_gates_create_cache_and_shutdown(self):
        adapter = self.by_id(readyset.read_status(FakeReadyset()))
        self.assertTrue(adapter["create_cache"]["enabled"])
        for aid in ("shutdown", "shutdown_reset"):
            self.assertFalse(adapter[aid]["enabled"])
            self.assertIn("admin SQL port", adapter[aid]["why_not"])
            self.assertIn("6036", adapter[aid]["fix"])

        admin = self.by_id(readyset.read_status(FakeReadyset(port=6036, database=None, admin=True)))
        self.assertFalse(admin["create_cache"]["enabled"])
        self.assertIn("SQL port", admin["create_cache"]["why_not"])
        self.assertIn("3307", admin["create_cache"]["fix"])
        self.assertTrue(admin["shutdown"]["enabled"])
        self.assertTrue(admin["shutdown_reset"]["enabled"])

        rs_schema = self.by_id(readyset.read_status(FakeReadyset(database="readyset")))
        self.assertFalse(rs_schema["create_cache"]["enabled"])
        self.assertIn("SQL port", rs_schema["create_cache"]["why_not"])

    def test_unreported_facts_disable_the_actions_that_depend_on_them(self):
        conn = FakeReadyset()
        conn.fail_reads = frozenset(("status",))
        acts = self.by_id(readyset.read_status(conn))
        for aid in ("enter_maintenance_mode", "exit_maintenance_mode", "start_replication", "stop_replication"):
            self.assertFalse(acts[aid]["enabled"], aid)
            self.assertTrue(acts[aid]["why_not"].startswith("Readyset did not report"), aid)
            self.assertTrue(acts[aid]["fix"], aid)

        conn2 = FakeReadyset()
        conn2.fail_reads = frozenset(("caches",))
        acts2 = self.by_id(readyset.read_status(conn2))
        for aid in ("flush_all_shallow_caches", "drop_all_caches",
                    "drop_all_shallow_caches", "drop_all_deep_caches",
                    "create_cache"):
            self.assertFalse(acts2[aid]["enabled"], aid)
            self.assertEqual(acts2[aid]["why_not"], "Readyset did not report its caches.")

    def test_cache_ddl_blocked_disables_cache_ddl_and_allowlist_changes(self):
        reason = "This Readyset listener refuses cache DDL."
        conn = FakeReadyset(cloud_blocked=True)  # SHOW CACHES itself is refused
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertFalse(report["capabilities"]["can_manage_caches"])
        acts = self.by_id(report)
        for aid in ("create_cache", "flush_all_shallow_caches", "drop_all_caches",
                    "drop_all_shallow_caches", "drop_all_deep_caches",
                    "drop_all_proxied_queries", "add_shallow_allowed"):
            self.assertFalse(acts[aid]["enabled"], aid)
            self.assertEqual(acts[aid]["why_not"], reason, aid)
        for kind in ("functions", "variables", "schemas"):
            for entry in report["allowlist_actions"][kind]:
                self.assertFalse(entry["enabled"])
                self.assertEqual(entry["why_not"], reason)
        # Non-DDL actions stay enabled.
        for aid in ("set_log_level", "flush_privileges", "add_tables"):
            self.assertTrue(acts[aid]["enabled"], aid)

        # Row catalogs follow the same one-way gate: rebuild them on a report
        # whose cache rows were read before the gate fired.
        rows_report = readyset.read_status(FakeReadyset())
        rows_report["capabilities"]["can_manage_caches"] = False
        readyset.attach_actions(rows_report)
        for row in rows_report["caches"]:
            for entry in row["actions"]:
                self.assertFalse(entry["enabled"])
                self.assertEqual(entry["why_not"], reason)
        for row in rows_report["proxied_queries"]:
            self.assertFalse(row["actions"][0]["enabled"])
            self.assertEqual(row["actions"][0]["why_not"], reason)
        # No text anywhere claims cache DDL is allowed.
        for entry in self.all_entries(report) + self.all_entries(rows_report):
            text = " ".join([entry["description"], entry["why_not"] or "", entry["fix"] or ""])
            if "DDL" in text:
                self.assertEqual(entry["why_not"], reason, entry["id"])
        self.assertNotIn("cache ddl is allowed", json.dumps(report).lower())

    def test_explain_cache_is_not_gated_by_the_cache_ddl_signal(self):
        # EXPLAIN CREATE CACHE checks only the readyset_schema route
        # (readyset-adapter/src/backend/extensions.rs 1732-1736), never
        # settings.allow_cache_ddl.
        conn = FakeReadyset(cloud_blocked=True)  # SHOW CACHES refused
        report = readyset.read_status(conn)
        self.assertFalse(report["capabilities"]["can_manage_caches"])
        self.assertFalse(report["capabilities"]["can_create_cache"])
        self.assertTrue(report["capabilities"]["can_explain_cache"])

    def test_allowlist_descriptions_cover_automatic_creation_only(self):
        report = readyset.read_status(FakeReadyset())
        server = self.by_id(report)["add_shallow_allowed"]
        self.assertEqual(
            server["description"],
            "Lets automatic shallow cache creation accept queries that reference one function, variable "
            "or schema; explicit CREATE SHALLOW CACHE and query hints bypass the allowlists.",
        )
        for kind in ("functions", "variables", "schemas"):
            add, drop = report["allowlist_actions"][kind]
            self.assertEqual(
                add["description"],
                "Lets automatic shallow cache creation accept queries that reference this %s." % kind[:-1],
            )
            self.assertEqual(
                drop["description"],
                "Stops automatic shallow cache creation from accepting queries that reference this %s; "
                "existing caches and explicit CREATE SHALLOW CACHE are not affected." % kind[:-1],
            )

    def test_state_gates(self):
        # No shallow caches: flush/drop shallow are disabled, the rest stay enabled.
        conn = FakeReadyset()
        conn.caches = [c for c in conn.caches if c["kind"] == "deep"]
        acts = self.by_id(readyset.read_status(conn))
        self.assertFalse(acts["flush_all_shallow_caches"]["enabled"])
        self.assertEqual(acts["flush_all_shallow_caches"]["why_not"], "There are no shallow caches to flush.")
        self.assertFalse(acts["drop_all_shallow_caches"]["enabled"])
        self.assertTrue(acts["drop_all_deep_caches"]["enabled"])
        self.assertTrue(acts["drop_all_caches"]["enabled"])

        empty = FakeReadyset()
        empty.caches = []
        empty_acts = self.by_id(readyset.read_status(empty))
        self.assertFalse(empty_acts["drop_all_caches"]["enabled"])
        self.assertEqual(empty_acts["drop_all_caches"]["why_not"], "There are no caches to drop.")

        # Maintenance on enables exit and disables enter; off is the reverse.
        maint = FakeReadyset()
        maint.status_text = "Maintenance Mode"
        maint_acts = self.by_id(readyset.read_status(maint))
        self.assertTrue(maint_acts["exit_maintenance_mode"]["enabled"])
        self.assertFalse(maint_acts["enter_maintenance_mode"]["enabled"])
        self.assertEqual(maint_acts["enter_maintenance_mode"]["why_not"],
                         "Readyset is already in maintenance mode.")
        online_acts = self.by_id(readyset.read_status(FakeReadyset()))
        self.assertTrue(online_acts["enter_maintenance_mode"]["enabled"])
        self.assertFalse(online_acts["exit_maintenance_mode"]["enabled"])
        self.assertEqual(online_acts["exit_maintenance_mode"]["why_not"],
                         "Readyset is not in maintenance mode.")

        # During a snapshot Readyset cannot report Maintenance Mode
        # (controller/inner.rs 749-757): enter is disabled until Online.
        snap = FakeReadyset()
        snap.status_text = "Snapshot In Progress"
        snap_acts = self.by_id(readyset.read_status(snap))
        self.assertFalse(snap_acts["enter_maintenance_mode"]["enabled"])
        self.assertEqual(
            snap_acts["enter_maintenance_mode"]["why_not"],
            "Readyset reports Maintenance Mode only when its Status is Online (now: Snapshot In Progress).",
        )
        self.assertEqual(snap_acts["enter_maintenance_mode"]["fix"],
                         "Wait until the snapshot finishes and Status is Online.")
        self.assertFalse(snap_acts["exit_maintenance_mode"]["enabled"])
        self.assertEqual(
            snap_acts["exit_maintenance_mode"]["why_not"],
            "Readyset does not show maintenance mode until its Status is Online (now: Snapshot In Progress).",
        )
        self.assertEqual(snap_acts["exit_maintenance_mode"]["fix"],
                         "Wait until the snapshot finishes and Status is Online.")

        # Replication Running enables stop and disables start; Stopped is the reverse.
        running_acts = self.by_id(readyset.read_status(FakeReadyset()))
        self.assertTrue(running_acts["stop_replication"]["enabled"])
        self.assertFalse(running_acts["start_replication"]["enabled"])
        self.assertEqual(running_acts["start_replication"]["why_not"], "Replication is already Running.")
        stopped = FakeReadyset()
        stopped.repl_status = "Stopped"
        stopped_acts = self.by_id(readyset.read_status(stopped))
        self.assertTrue(stopped_acts["start_replication"]["enabled"])
        self.assertFalse(stopped_acts["stop_replication"]["enabled"])
        self.assertEqual(stopped_acts["stop_replication"]["why_not"], "Replication is already Stopped.")
        paused = FakeReadyset()
        paused.repl_status = "Paused"
        paused_acts = self.by_id(readyset.read_status(paused))
        self.assertFalse(paused_acts["start_replication"]["enabled"])
        self.assertFalse(paused_acts["stop_replication"]["enabled"])
        self.assertEqual(paused_acts["start_replication"]["why_not"], "Replication status is Paused.")

    def test_bad_table_name_disables_only_that_rows_actions(self):
        conn = FakeReadyset()
        conn.tables = [
            {"table": "shop.broken\nname", "status": "Snapshotted", "description": ""},
            {"table": "`shop`.`orders`", "status": "Snapshotted", "description": ""},
        ]
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        self.assertEqual(len(report["tables"]), 2)
        broken, good = report["tables"]
        self.assertEqual(broken["actions"][0]["id"], "resnapshot_table")
        self.assertFalse(broken["actions"][0]["enabled"])
        self.assertEqual(broken["actions"][0]["why_not"],
                         "Readyset reported a name that cannot be used in a statement.")
        self.assertTrue(good["actions"][0]["enabled"])
        # available_actions answers the same way instead of raising.
        entry = readyset.available_actions(report, {"kind": "table", "table": "shop.broken\nname"})
        self.assertFalse(entry[0]["enabled"])
        self.assertEqual(entry[0]["why_not"], broken["actions"][0]["why_not"])
        # Catalog fields never enter fingerprints or the post-write snapshot.
        self.assertNotIn("actions", json.dumps(report["fingerprints"]))
        self.assertNotIn("why_not", json.dumps(report["fingerprints"]))
        self.assertNotIn("actions", json.dumps(readyset._snapshot_state(report)))

    def test_maintenance_mode_wording(self):
        conn = FakeReadyset()
        conn.status_text = "Maintenance Mode"
        report = readyset.read_status(conn)
        finding = next(f for f in report["findings"] if f["id"] == "maintenance_mode")
        self.assertEqual(finding["title"], "Readyset is in maintenance mode.")

        enter = readyset.plan_action(readyset.read_status(FakeReadyset()), "enter_maintenance_mode")
        self.assertEqual(
            enter["description"],
            "Sets Readyset's status to Maintenance Mode: a status signal that tools such as ProxySQL are "
            "meant to act on by stopping traffic to Readyset, which keeps serving the queries it receives. "
            "The setting is kept in memory; restarting Readyset clears it.",
        )
        exit_plan = readyset.plan_action(readyset.read_status(conn), "exit_maintenance_mode")
        self.assertEqual(
            exit_plan["description"],
            "Clears the Maintenance Mode status signal so tools such as ProxySQL send queries to "
            "Readyset again; Readyset kept serving the queries it received. The setting is kept in "
            "memory; restarting Readyset clears it.",
        )


class TestExactServerIdentifiers(ReadysetBase):

    def test_oversized_table_names_are_time_bounded_and_disable_only_their_rows(self):
        # A separate process supplies a hard deadline even if parsing regresses
        # to quadratic front-deletion. subprocess.run reaps it on timeout.
        code = """
import sys
import time
sys.path.insert(0, 'test')
from test_readyset import FakeReadyset
from myflames import readyset
bad_names = [' ' * 320000, 'shop.' + ' ' * 320000 + 'orders']
started = time.monotonic()
for name in bad_names:
    try:
        readyset._rs_table_ident(name)
    except ValueError:
        pass
    else:
        raise AssertionError('oversized table name accepted')
conn = FakeReadyset()
conn.tables.extend({'table': name, 'status': 'Online'} for name in bad_names)
report = readyset.read_status(conn)
assert len(report['tables']) == 4
assert all(row['actions'][0]['enabled'] for row in report['tables'][:2])
for row in report['tables'][2:]:
    action = row['actions'][0]
    assert not action['enabled']
    assert (action['why_not'], action['fix']) == readyset._BAD_ROW_NAME
assert time.monotonic() - started < 2.0
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
                                universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_table_length_bound_preserves_quoted_identifiers_and_linear_trimming(self):
        self.assertEqual(readyset._rs_table_ident(" " * 589 + "shop.orders"), "`shop`.`orders`")
        with self.assertRaisesRegex(ValueError, "at most 600 characters"):
            readyset._rs_table_ident(" " * 590 + "shop.orders")
        longest_quoted_part = "`" + "``" * 128 + "`"
        longest_quoted_name = longest_quoted_part + "." + longest_quoted_part
        self.assertEqual(readyset._rs_table_ident(longest_quoted_name), longest_quoted_name)
        self.assertEqual(readyset._rs_table_ident(" shop . ` orders ` "), "`shop`.` orders `")

    def test_resnapshot_aliases_keep_the_matched_reported_guard(self):
        conn = FakeReadyset()
        report = readyset.read_status(conn)
        for alias in ("shop.orders", "`shop`.orders", " shop . orders ", "\u2028shop.orders\u2029"):
            with self.subTest(alias=repr(alias)):
                plan = readyset.plan_action(report, "resnapshot_table", {"kind": "table", "table": alias})
                self.assertEqual(plan["typed_guard"], conn.tables[0]["table"])
                self.assertEqual(plan["expect"]["table"], conn.tables[0]["table"])
                self.assertEqual(plan["statements"], ["ALTER READYSET RESNAPSHOT TABLE `shop`.`orders`;"])

    """Lane K1: a name Readyset reported reaches the statement byte for byte,
    and the catalog and the planner resolve a target to the same row."""

    def test_padded_cache_names_drop_and_flush_their_own_cache(self):
        conn = FakeReadyset()
        conn.caches.append({
            "query_id": "q_7777aaaa", "name": " cache",
            "query": "SELECT id FROM orders", "properties": "shallow (ttl = 30s)",
            "count": "5", "database": "shop", "kind": "shallow",
        })
        conn.caches.append({
            "query_id": "q_7777bbbb", "name": "cache",
            "query": "SELECT id FROM orders", "properties": "shallow (ttl = 30s)",
            "count": "6", "database": "shop", "kind": "shallow",
        })
        report = readyset.read_status(conn)
        names = [c["name"] for c in report["caches"]]
        self.assertIn(" cache", names)
        self.assertIn("cache", names)
        for action, verb in (("drop_cache", "DROP CACHE"), ("flush_cache", "FLUSH CACHE")):
            with self.subTest(action=action):
                padded = readyset.plan_action(
                    report, action, {"kind": "cache", "name": " cache", "query_id": "q_7777aaaa"})
                self.assertEqual(padded["statements"], ["%s ` cache`;" % verb])
                plain = readyset.plan_action(
                    report, action, {"kind": "cache", "name": "cache", "query_id": "q_7777bbbb"})
                self.assertEqual(plain["statements"], ["%s `cache`;" % verb])

    def test_quoted_table_with_inner_space_resnapshots_exactly(self):
        # Whitespace inside a quoted component is part of the name
        # (readyset-sql/src/ast/table.rs:124): `shop`.` orders` and
        # `shop`.`orders` are different tables.
        conn = FakeReadyset()
        conn.tables = [
            {"table": "`shop`.` orders`", "status": "Snapshotted", "description": ""},
            {"table": "`shop`.`orders`", "status": "Online", "description": ""},
        ]
        report = readyset.read_status(conn)
        spaced = readyset.plan_action(report, "resnapshot_table", {"kind": "table", "table": "`shop`.` orders`"})
        self.assertEqual(spaced["statements"], ["ALTER READYSET RESNAPSHOT TABLE `shop`.` orders`;"])
        self.assertIn(["Current status", "Snapshotted"], spaced["facts"])
        plain = readyset.plan_action(report, "resnapshot_table", {"kind": "table", "table": "`shop`.`orders`"})
        self.assertEqual(plain["statements"], ["ALTER READYSET RESNAPSHOT TABLE `shop`.`orders`;"])
        self.assertIn(["Current status", "Online"], plain["facts"])
        entry = readyset.available_actions(report, {"kind": "table", "table": "`shop`.` orders`"})
        self.assertTrue(entry[0]["enabled"])

    def test_export_keeps_database_with_leading_whitespace(self):
        class ExportConn(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql.strip().upper() == "EXPLAIN ALL CACHES":
                    return xml_rows([
                        {"query": "CREATE DEEP CACHE `c` FROM SELECT 1;", "database": " shop"},
                    ])
                return FakeReadyset.run(self, sql, extra_flags=extra_flags, timeout=timeout)

        exp = readyset.export_caches(ExportConn())
        self.assertIn("USE ` shop`;", exp["sql"])
        self.assertEqual(exp["caches"][0]["database"], " shop")

    def test_changed_row_identity_is_refused(self):
        # Lane K2: when a target carries both a name and a query id, both must
        # match the same current row. Here the padded name matches nothing and
        # the query id matches the shallow cache: the identity changed since it
        # was read, so the catalog offers nothing and the planner refuses
        # instead of acting on the row either half matches.
        report = readyset.read_status(FakeReadyset())
        target = {"kind": "cache", "name": " c_orders_deep ", "query_id": "q_2222bbbb"}
        self.assertEqual(readyset.available_actions(report, target), [])
        for action in ("drop_cache", "flush_cache"):
            with self.subTest(action=action):
                with self.assertRaises(ValueError) as caught:
                    readyset.plan_action(report, action, target)
                self.assertEqual(
                    str(caught.exception),
                    "That cache changed since it was read. Refresh and try again.")
        # The reverse direction: a current name with another cache's query id.
        swapped = {"kind": "cache", "name": "c_orders_deep", "query_id": "q_2222bbbb"}
        with self.assertRaises(ValueError) as caught2:
            readyset.plan_action(report, "drop_cache", swapped)
        self.assertEqual(
            str(caught2.exception),
            "That cache changed since it was read. Refresh and try again.")
        # Both halves of the same current row still plan normally.
        plan = readyset.plan_action(
            report, "drop_cache",
            {"kind": "cache", "name": "c_recent_shallow", "query_id": "q_2222bbbb"})
        self.assertEqual(plan["statements"], ["DROP CACHE `c_recent_shallow`;"])

    def test_newline_cache_name_disabled_and_refused(self):
        conn = FakeReadyset()
        conn.caches.append({
            "query_id": "q_7777cccc", "name": "bad\nname",
            "query": "SELECT id FROM orders", "properties": "shallow (ttl = 30s)",
            "count": "1", "database": "shop", "kind": "shallow",
        })
        report = readyset.read_status(conn)
        row = next(c for c in report["caches"] if c["query_id"] == "q_7777cccc")
        self.assertTrue(row["actions"])
        for entry in row["actions"]:
            self.assertFalse(entry["enabled"])
            self.assertEqual(entry["why_not"], "Readyset reported a name that cannot be used in a statement.")
            self.assertEqual(entry["fix"], "Run this change on Readyset by hand.")
        for action in ("drop_cache", "flush_cache"):
            with self.subTest(action=action):
                with self.assertRaises(reviewed_writes.ActionUnavailable):
                    readyset.plan_action(
                        report, action, {"kind": "cache", "name": "bad\nname", "query_id": "q_7777cccc"})

    def test_unreported_unparseable_table_is_not_offered(self):
        report = readyset.read_status(FakeReadyset())
        self.assertEqual(
            readyset.available_actions(report, {"kind": "table", "table": "no_such\ntable"}), [])
        with self.assertRaises(ValueError) as caught:
            readyset.plan_action(report, "resnapshot_table", {"kind": "table", "table": "no_such\ntable"})
        self.assertNotIsInstance(caught.exception, reviewed_writes.ActionUnavailable)
        self.assertEqual(
            str(caught.exception), "That change is not offered on this Readyset now. Refresh and try again.")
        # A kind-only table target still finds its row through the value.
        plan = readyset.plan_action(report, "resnapshot_table", {"kind": "table"}, "shop.orders")
        self.assertEqual(plan["statements"], ["ALTER READYSET RESNAPSHOT TABLE `shop`.`orders`;"])

    def test_bidi_control_cache_name_is_disabled(self):
        conn = FakeReadyset()
        conn.caches.append({
            "query_id": "q_7777dddd", "name": "c_\u202ee",
            "query": "SELECT id FROM orders", "properties": "deep",
            "count": "1", "database": "shop", "kind": "deep",
        })
        report = readyset.read_status(conn)
        row = next(c for c in report["caches"] if c["query_id"] == "q_7777dddd")
        self.assertTrue(row["actions"])
        for entry in row["actions"]:
            self.assertFalse(entry["enabled"])
            self.assertEqual(entry["why_not"], "Readyset reported a name with control or format characters.")
            self.assertEqual(entry["fix"], "Run this change on Readyset by hand.")
        with self.assertRaises(reviewed_writes.ActionUnavailable):
            readyset.plan_action(
                report, "drop_cache", {"kind": "cache", "name": "c_\u202ee", "query_id": "q_7777dddd"})

    def test_dotted_cache_name_is_disabled_and_refused(self):
        # Lane K2: SHOW CACHES renders `sales`.`report` and the one identifier
        # `sales.report` both as sales.report (extensions.rs:1113 with
        # readyset-sql/src/ast/table.rs:135), and DROP CACHE / FLUSH CACHE
        # resolve a Relation name only, never a query id, for a named cache
        # (readyset-sql-parsing/src/lib.rs:1687,1716). The row's drop and flush
        # are disabled with a fixed reason instead of quoting either reading.
        conn = FakeReadyset()
        conn.caches.append({
            "query_id": "q_7777eeee", "name": "sales.report",
            "query": "SELECT id FROM orders", "properties": "shallow (ttl = 30s)",
            "count": "1", "database": "shop", "kind": "shallow",
        })
        report = readyset.read_status(conn)
        row = next(c for c in report["caches"] if c["query_id"] == "q_7777eeee")
        self.assertTrue(row["actions"])
        for entry in row["actions"]:
            self.assertFalse(entry["enabled"])
            self.assertEqual(
                entry["why_not"],
                "Readyset reported a cache name with a dot, which a statement cannot address unambiguously.")
            self.assertEqual(
                entry["fix"],
                "Run this change on Readyset by hand: SHOW CACHES does not show whether the dot is a qualifier.")
        for action in ("drop_cache", "flush_cache"):
            with self.subTest(action=action):
                with self.assertRaises(reviewed_writes.ActionUnavailable):
                    readyset.plan_action(
                        report, action, {"kind": "cache", "name": "sales.report", "query_id": "q_7777eeee"})


class TestReviewTextSafety(ReadysetBase):
    PADDED_WARNING = "The name starts or ends with a space: type it exactly, spaces included."
    COLLISION_REFUSAL = ("A cache with control or format characters in its name collides; "
                         "drop it on Readyset by hand first.")

    def test_bulk_drop_reviews_escape_controls_without_changing_expected_state(self):
        conn = FakeReadyset()
        # XML supports these Cc and Cf values, including a non-BMP format
        # character. A bulk drop must still identify the exact listed names.
        name = "cache\t\n\u0085\u202e\u2066\U000e0001"
        escaped = "cache\\u0009\\u000a\\u0085\\u202e\\u2066\\U000e0001"
        conn.caches[0]["name"] = name
        conn.caches[1]["name"] = name + " shallow"
        report = readyset.read_status(conn)
        for action, stmt in (("drop_all_caches", "DROP ALL CACHES;"),
                             ("drop_all_deep_caches", "DROP ALL DEEP CACHES;"),
                             ("drop_all_shallow_caches", "DROP ALL SHALLOW CACHES;")):
            with self.subTest(action=action):
                plan = readyset.plan_action(report, action)
                self.assertTrue(all(escaped in change for change in plan["changes"]))
                self.assertFalse(any(unicodedata.category(ch) in ("Cc", "Cf")
                                     for change in plan["changes"] for ch in change))
                self.assertEqual(plan["statements"], [stmt])
                self.assertEqual(plan["typed_guard"], stmt[:-1])
                self.assertEqual(plan["expect"]["caches_fp"], report["fingerprints"]["caches"])
        self.assertEqual(report["caches"][0]["name"], name)
        self.assertIn(name, readyset._snapshot_state(report)["caches"])

    def test_create_control_name_collisions_refuse_by_query_id_and_exact_name(self):
        for control in ("\t", "\n", "\u0085", "\u202e", "\u2066", "\U000e0001"):
            for mode in ("shallow", "deep"):
                with self.subTest(control=repr(control), mode=mode):
                    conn = FakeReadyset()
                    name = "existing" + control + "cache"
                    conn.caches[0]["name"] = name
                    report = readyset.read_status(conn)
                    with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
                        readyset.plan_action(report, "create_cache", conn=conn, value={
                            "cache_type": mode, "query_or_id": "q_1111aaaa", "name": "replacement"})
                    self.assertEqual(str(caught.exception), self.COLLISION_REFUSAL)
                    # User-entered controls are refused before collision or
                    # support checks, including names already on the server.
                    with self.assertRaises(ValueError) as caught:
                        readyset.plan_action(report, "create_cache", conn=conn, value={
                            "cache_type": mode, "query_or_id": "q_3333cccc", "name": name})
                    self.assertEqual(str(caught.exception),
                                     "Choose a cache name without dots or control characters.")
                    self.assertEqual(conn.writes, [])

    def test_control_name_collision_discovered_during_apply_refuses_once(self):
        class Audit(object):
            def __init__(self):
                self.outcomes = []

            def server(self, *args):
                pass

            def outcome(self, *args, **kwargs):
                self.outcomes.append((args, kwargs))
                return 1

        conn = FakeReadyset()
        plan = readyset.plan_action(readyset.read_status(conn), "create_cache",
                                   value={"cache_type": "shallow", "query_or_id": "q_3333cccc"}, conn=conn)
        conn.caches.append(dict(conn.caches[0], query_id="q_3333cccc", name="cache\u202eevil"))
        audit = Audit()
        result = readyset.execute_action(conn, plan, audit=audit)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["outcome"], "changed")
        self.assertEqual(result["message"], self.COLLISION_REFUSAL + " Nothing was sent.")
        self.assertEqual(len(audit.outcomes), 1)
        self.assertEqual(conn.writes, [])

    def test_review_facts_escape_controls_in_queries_and_refuse_control_allowlist_names(self):
        conn = FakeReadyset()
        conn.caches[0]["query"] = "SELECT id FROM `or\u202eders`"
        item = "fu\u202enc\tname"
        conn.allowlists["FUNCTIONS"].append(item)
        report = readyset.read_status(conn)
        plan = readyset.plan_action(report, "drop_cache", {"kind": "cache", "name": "c_orders_deep"})
        self.assertIn(["Query", "SELECT id FROM `or\\u202eders`"], plan["facts"])
        self.assertEqual(plan["typed_guard"], "c_orders_deep")
        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
            readyset.plan_action(report, "drop_shallow_allowed", {"kind": "function", "name": item})
        self.assertEqual(str(caught.exception), "%s %s" % readyset._CONTROL_ROW_NAME)

    def test_allowlist_controls_preserved_then_refused_in_row_catalog_and_plan(self):
        controls = ("\t", "\n", "\u0085", "\u202e", "\u2066", "\U000e0001", "\u2028", "\u2029")
        for kind in ("function", "variable", "schema"):
            for control in controls:
                for name in (control + "bad", "b" + control + "ad", "bad" + control):
                    with self.subTest(kind=kind, name=repr(name)):
                        conn = FakeReadyset()
                        conn.allowlists[kind.upper() + "S"].append(name)
                        report = readyset.read_status(conn)
                        self.assertIn(name, report["shallow_allowlists"][kind + "s"])
                        target = {"kind": kind, "name": name}
                        entries = readyset.available_actions(report, target)
                        drop = next(entry for entry in entries if entry["id"] == "drop_shallow_allowed")
                        self.assertFalse(drop["enabled"])
                        self.assertEqual((drop["why_not"], drop["fix"]), readyset._CONTROL_ROW_NAME)
                        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
                            readyset.plan_action(report, "drop_shallow_allowed", target)
                        self.assertEqual(str(caught.exception), "%s %s" % readyset._CONTROL_ROW_NAME)
                        # Another item of the same kind remains addressable.
                        good_name = conn.allowlists[kind.upper() + "S"][0]
                        good = readyset.available_actions(report, {"kind": kind, "name": good_name})
                        self.assertTrue(all(entry["enabled"] for entry in good))
                        self.assertEqual(conn.writes, [])

    def test_allowlist_item_actions_disable_only_the_unusable_item(self):
        conn = FakeReadyset()
        bad = "bad\u202ename"
        conn.allowlists["FUNCTIONS"].append(bad)
        report = readyset.read_status(conn)
        assert_matches_schema(self, report, SCHEMA)
        names = report["shallow_allowlists"]["functions"]
        entries = report["allowlist_item_actions"]["functions"]
        self.assertEqual(len(entries), len(names))
        for name, entry in zip(names, entries):
            self.assertEqual(entry["id"], "drop_shallow_allowed")
            if name == bad:
                self.assertFalse(entry["enabled"])
                self.assertEqual((entry["why_not"], entry["fix"]), readyset._CONTROL_ROW_NAME)
            else:
                self.assertTrue(entry["enabled"], name)
                self.assertIsNone(entry["why_not"])
                self.assertIsNone(entry["fix"])
        bad_entry = entries[names.index(bad)]
        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
            readyset.plan_action(report, "drop_shallow_allowed", {"kind": "function", "name": bad})
        self.assertEqual(str(caught.exception), bad_entry["why_not"] + " " + bad_entry["fix"])
        self.assertEqual(conn.writes, [])

    def test_legacy_control_allowlist_plan_refuses_at_apply_and_audits_once(self):
        conn = FakeReadyset()
        plan = readyset.plan_action(readyset.read_status(conn), "drop_shallow_allowed",
                                   {"kind": "function", "name": "NOW"})
        # Represent a review issued before this fix, when bidi names could be
        # planned. Execute must apply the same row gate to its own fresh read.
        name = "bad\u202ename"
        conn.allowlists["FUNCTIONS"].append(name)
        plan["target"]["name"] = name
        plan["expect"]["item"] = name
        plan["statements"] = ["ALTER READYSET DROP SHALLOW CACHE ALLOWED FUNCTION `%s`;" % name]
        audit = mock.Mock()
        audit.outcome.return_value = "refused-row"
        result = readyset.execute_action(conn, plan, audit=audit)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(result["outcome"], "changed")
        self.assertEqual(result["message"], "%s %s Nothing was sent." % readyset._CONTROL_ROW_NAME)
        self.assertEqual(result["states"], ["not_sent"])
        audit.outcome.assert_called_once()
        self.assertEqual(audit.outcome.call_args[1]["outcome"], "changed")
        audit.attempt.assert_not_called()
        self.assertEqual(conn.writes, [])

    def test_typed_allowlist_controls_cannot_bypass_row_checks(self):
        conn = FakeReadyset()
        report = readyset.read_status(conn)
        for action in ("add_shallow_allowed", "drop_shallow_allowed"):
            for name in ("\tNOW", "NOW\n", "bad\u202ename", "bad\u2028name", "bad\u2029name"):
                with self.subTest(action=action, name=repr(name)):
                    with self.assertRaises(ValueError):
                        readyset.plan_action(report, action, {"kind": "function"}, {"name": name})
                    self.assertEqual(conn.writes, [])

    def test_reported_allowlist_spaces_are_not_trimmed_into_another_item(self):
        conn = FakeReadyset()
        conn.allowlists["SCHEMAS"].append(" shop ")
        report = readyset.read_status(conn)
        self.assertIn(" shop ", report["shallow_allowlists"]["schemas"])
        plan = readyset.plan_action(report, "drop_shallow_allowed", {"kind": "schema", "name": " shop "})
        self.assertEqual(plan["expect"]["item"], " shop ")
        self.assertEqual(plan["statements"], ["ALTER READYSET DROP SHALLOW CACHE ALLOWED SCHEMA ` shop `;"])

    def test_unicode_line_and_paragraph_separators_are_controls(self):
        for separator in ("\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                self.assertTrue(readyset._has_ctl_or_fmt(separator))
                self.assertEqual(readyset._display("a" + separator + "b"), "ab")
                self.assertEqual(readyset._review_text(separator), "\\u%04x" % ord(separator))
                conn = FakeReadyset()
                conn.caches[0]["name"] = "a" + separator + "b"
                conn.tables[0]["table"] = "`shop`.`a" + separator + "b`"
                report = readyset.read_status(conn)
                for entry in report["caches"][0]["actions"] + report["tables"][0]["actions"]:
                    self.assertFalse(entry["enabled"])
                    self.assertEqual((entry["why_not"], entry["fix"]), readyset._CONTROL_ROW_NAME)

    def test_padded_typed_guards_warn_and_keep_exact_characters(self):
        for name in (" cache", "cache ", " cache ", "cache"):
            with self.subTest(name=name):
                conn = FakeReadyset()
                conn.caches[0]["name"] = name
                report = readyset.read_status(conn)
                drop = readyset.plan_action(report, "drop_cache", {"kind": "cache", "name": name})
                create = readyset.plan_action(report, "create_cache",
                                             value={"cache_type": "shallow", "query_or_id": "q_1111aaaa"})
                for plan in (drop, create):
                    self.assertEqual(plan["typed_guard"], name)
                    self.assertEqual(self.PADDED_WARNING in plan["warnings"], name != name.strip())
                self.assertEqual(drop["statements"], ["DROP CACHE `%s`;" % name])
                self.assertEqual(create["expect"]["collisions"][0]["name"], name)


class TestPlanActionCatalogRefusals(ReadysetBase):
    """Lane A2b: plan_action selects from the catalog and refuses a disabled
    entry with exactly the catalog's why_not + fix, at review and at apply."""

    # The v3.5.0 guards, except resnapshot now requires the reported row name
    # instead of its caller alias (admin fixture for the shutdown actions).
    TYPED_GUARDS = {
        "create_cache": None,
        "flush_cache": None,
        "drop_cache": None,
        "flush_all_shallow_caches": None,
        "drop_all_caches": "DROP ALL CACHES",
        "drop_all_shallow_caches": "DROP ALL SHALLOW CACHES",
        "drop_all_deep_caches": "DROP ALL DEEP CACHES",
        "drop_all_proxied_queries": "DROP ALL PROXIED QUERIES",
        "enter_maintenance_mode": "MAINTENANCE MODE",
        "exit_maintenance_mode": None,
        "set_log_level": None,
        "set_eviction_memory_limit": None,
        "set_eviction_period": None,
        "add_shallow_allowed": None,
        "drop_shallow_allowed": None,
        "flush_privileges": None,
        "resnapshot_table": "`shop`.`orders`",
        "add_tables": None,
        "stop_replication": "STOP REPLICATION",
        "start_replication": None,
        "set_replication_position": "SET REPLICATION POSITION",
        "shutdown": "SHUTDOWN",
        "shutdown_reset": "SHUTDOWN RESET",
    }

    def fixture_states(self):
        deep_only = FakeReadyset()
        deep_only.caches = [c for c in deep_only.caches if c["kind"] == "deep"]
        no_caches = FakeReadyset()
        no_caches.caches = []
        maintenance = FakeReadyset()
        maintenance.status_text = "Maintenance Mode"
        stopped = FakeReadyset()
        stopped.repl_status = "Stopped"
        paused = FakeReadyset()
        paused.repl_status = "Paused"
        no_status = FakeReadyset()
        no_status.fail_reads = frozenset(("status",))
        no_caches_read = FakeReadyset()
        no_caches_read.fail_reads = frozenset(("caches",))
        return [
            ("adapter", FakeReadyset()),
            ("admin", FakeReadyset(port=6036, database=None, admin=True)),
            ("ddl_blocked", FakeReadyset(cloud_blocked=True)),
            ("deep_only", deep_only),
            ("no_caches", no_caches),
            ("maintenance", maintenance),
            ("repl_stopped", stopped),
            ("repl_paused", paused),
            ("status_not_read", no_status),
            ("caches_not_read", no_caches_read),
        ]

    def entries_and_targets(self, report):
        for entry in report["actions"]:
            yield entry, None, None
        for row in report["caches"]:
            target = {"kind": "cache", "name": row["name"], "query_id": row["query_id"]}
            for entry in row["actions"]:
                yield entry, target, None
        for row in report["tables"]:
            target = {"kind": "table", "table": row["table"]}
            for entry in row["actions"]:
                yield entry, target, None
        for row in report["proxied_queries"]:
            target = {"kind": "proxied_query", "query_id": row["query_id"], "query": row["query"]}
            for entry in row["actions"]:
                yield entry, target, None
        for kind in ("function", "variable", "schema"):
            target = {"kind": kind}
            for entry in report["allowlist_actions"][kind + "s"]:
                yield entry, target, None

    def test_disabled_entries_refuse_with_exactly_the_catalog_text(self):
        for state_name, conn in self.fixture_states():
            report = readyset.read_status(conn)
            for entry, target, value in self.entries_and_targets(report):
                with self.subTest(state=state_name, action=entry["id"], scope=entry["scope"]):
                    if entry["enabled"]:
                        try:
                            readyset.plan_action(report, entry["id"], target, value)
                        except reviewed_writes.ActionUnavailable as exc:
                            self.fail("enabled entry %r refused: %s" % (entry["id"], exc))
                        except ValueError:
                            pass  # its own input validation (a value is needed)
                    else:
                        expected = "%s %s" % (entry["why_not"], entry["fix"])
                        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
                            readyset.plan_action(report, entry["id"], target, value)
                        self.assertEqual(str(caught.exception), expected)

    def test_disabled_create_cache_sends_no_explain(self):
        conn = FakeReadyset(cloud_blocked=True)
        report = readyset.read_status(conn)
        calls_before = len(conn.calls)
        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
            readyset.plan_action(report, "create_cache", None,
                                 {"cache_type": "shallow", "query_or_id": "SELECT 1"}, conn=conn)
        self.assertEqual(
            str(caught.exception),
            "This Readyset listener refuses cache DDL. "
            "Run cache changes on the CACHE_DDL_ADDRESS listener or the admin SQL port (default 6036); "
            "CREATE CACHE still needs an upstream database selected.",
        )
        sent = [sql for sql, _flags in conn.calls[calls_before:]]
        self.assertFalse(any("EXPLAIN" in sql.upper() for sql in sent), sent)

    def test_create_cache_disabled_when_caches_not_read(self):
        # Lane K2: Readyset drops a colliding cache before creating the
        # replacement (extensions.rs:96,243), so CREATE CACHE needs the cache
        # list; with SHOW CACHES unread the action is disabled, at plan and at
        # apply (execute_action re-plans on a fresh report).
        conn = FakeReadyset()
        conn.fail_reads = frozenset(("caches",))
        report = readyset.read_status(conn)
        entry = next(a for a in report["actions"] if a["id"] == "create_cache")
        self.assertFalse(entry["enabled"])
        self.assertEqual(entry["why_not"], "Readyset did not report its caches.")
        self.assertEqual(entry["fix"], "Check the connection and refresh: SHOW CACHES lists them.")
        expected = ("Readyset did not report its caches. "
                    "Check the connection and refresh: SHOW CACHES lists them.")
        with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
            readyset.plan_action(report, "create_cache", None,
                                 {"cache_type": "shallow", "query_or_id": "SELECT 1"})
        self.assertEqual(str(caught.exception), expected)

    def test_prequoted_create_name_collides_and_gets_the_guard(self):
        # Lane K2: the identifier the statement names (after user-input
        # parsing) is what collision warnings and the typed guard compare
        # against, so a backticked input cannot slip past the checks.
        conn = FakeReadyset()
        conn.caches.append({
            "query_id": "q_7777ffff", "name": "orders_cache",
            "query": "SELECT id FROM orders", "properties": "shallow (ttl = 30s)",
            "count": "1", "database": "shop", "kind": "shallow",
        })
        report = readyset.read_status(conn)
        plan = readyset.plan_action(
            report, "create_cache", None,
            {"cache_type": "deep", "name": "`orders_cache`", "query_or_id": "SELECT id FROM orders"})
        self.assertEqual(plan["statements"], ["CREATE DEEP CACHE `orders_cache` FROM SELECT id FROM orders;"])
        self.assertEqual(plan["expect"]["name"], "orders_cache")
        self.assertEqual(plan["typed_guard"], "orders_cache")
        self.assertTrue(plan["warnings"])
        self.assertTrue(any("will be replaced" in w for w in plan["warnings"]), plan["warnings"])
        self.assertEqual(
            plan["changes"],
            ["Replace existing shallow cache orders_cache (q_7777ffff)"])

    def test_create_name_dots_and_unicode_controls_refused_before_explain(self):
        conn = FakeReadyset()
        report = readyset.read_status(conn)
        for name in ("sales.report", "`sales.report`", "`sales`.`report`", "bad\x00name", "bad\rname",
                     "bad\nname", "bad\tname", "bad\u0085name", "bad\u202ename", "bad\u2066name",
                     "bad\u2028name", "bad\u2029name",
                     "bad\U000e0001name", "\ttrimmed", "trimmed\n", "\t"):
            for mode in ("shallow", "deep"):
                with self.subTest(name=repr(name), mode=mode):
                    conn.calls[:] = []
                    with self.assertRaises(ValueError) as caught:
                        readyset.plan_action(report, "create_cache", conn=conn, value={
                            "cache_type": mode, "name": name, "query_or_id": "SELECT 1"})
                    self.assertEqual(str(caught.exception),
                                     "Choose a cache name without dots or control characters.")
                    self.assertFalse(any(sql.upper().startswith("EXPLAIN CREATE") for sql, _ in conn.calls))
                    self.assertEqual(conn.writes, [])
        # Ordinary Unicode and an escaped literal backtick stay usable.
        for name, parsed in (("café", "café"), ("`a``b`", "a`b")):
            plan = readyset.plan_action(report, "create_cache", value={"name": name, "query_or_id": "SELECT 1"})
            self.assertEqual(plan["expect"]["name"], parsed)

    def test_prequoted_allowlist_name_is_parsed_for_checks_and_expect(self):
        conn = FakeReadyset()
        report = readyset.read_status(conn)
        for kind, item in (("function", "now"), ("variable", "time_zone"), ("schema", "shop")):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "already in the shallow cache allowlist"):
                    readyset.plan_action(report, "add_shallow_allowed", {"kind": kind}, {"name": "`%s`" % item})
                plan = readyset.plan_action(report, "drop_shallow_allowed", {"kind": kind},
                                           {"name": "`%s`" % item})
                self.assertEqual(plan["expect"]["item"], item)
                self.assertEqual(plan["statements"], [
                    "ALTER READYSET DROP SHALLOW CACHE ALLOWED %s `%s`;" % (kind.upper(), item)])
        # Parse once: quoted literal backticks belong to the resulting name.
        added = readyset.plan_action(report, "add_shallow_allowed", {"kind": "function"},
                                    {"name": "```new``name```"})
        self.assertEqual(added["expect"]["item"], "`new`name`")
        self.assertEqual(added["statements"], [
            "ALTER READYSET ADD SHALLOW CACHE ALLOWED FUNCTION ```new``name```;"])
        conn.allowlists["FUNCTIONS"].append("`now`")
        report = readyset.read_status(conn)
        dropped = readyset.plan_action(report, "drop_shallow_allowed", {"kind": "function", "name": "`now`"})
        self.assertEqual(dropped["expect"]["item"], "`now`")
        self.assertEqual(dropped["statements"], ["ALTER READYSET DROP SHALLOW CACHE ALLOWED FUNCTION ```now```;"])

    def test_inconsistent_allowlist_kinds_refused(self):
        # Lane K2: the catalog gate and the planner resolve one allowlist kind
        # from the request; disagreeing target and value kinds are refused
        # instead of checking one list and changing another.
        report = readyset.read_status(FakeReadyset())
        for target, value in (
            ({"kind": "function", "allow_kind": "variable", "name": "NOW"}, None),
            ({"kind": "function", "name": "NOW"}, {"allow_kind": "schema"}),
            ({"allow_kind": "function"}, {"allow_kind": "variable", "name": "x"}),
        ):
            with self.subTest(target=target, value=value):
                with self.assertRaises(ValueError) as caught:
                    readyset.plan_action(report, "drop_shallow_allowed", target, value)
                self.assertEqual(
                    str(caught.exception),
                    "The target and the value name different allowlist kinds. Pass one kind and try again.")
        # One kind in either place, or the same kind twice, plans normally.
        plan = readyset.plan_action(
            report, "drop_shallow_allowed", {"allow_kind": "function", "name": "NOW"})
        self.assertEqual(
            plan["statements"], ["ALTER READYSET DROP SHALLOW CACHE ALLOWED FUNCTION `NOW`;"])
        plan2 = readyset.plan_action(
            report, "drop_shallow_allowed", {"kind": "function", "name": "NOW"},
            {"allow_kind": "function"})
        self.assertEqual(
            plan2["statements"], ["ALTER READYSET DROP SHALLOW CACHE ALLOWED FUNCTION `NOW`;"])

    def test_maintenance_plan_refused_when_status_not_read(self):
        conn = FakeReadyset()
        conn.fail_reads = frozenset(("status",))
        report = readyset.read_status(conn)
        self.assertIsNone(report["status_fields"]["status"])
        expected = ("Readyset did not report its Status. "
                    "Check the connection and refresh: SHOW READYSET STATUS reports it.")
        for aid in ("enter_maintenance_mode", "exit_maintenance_mode"):
            with self.subTest(action=aid):
                with self.assertRaises(reviewed_writes.ActionUnavailable) as caught:
                    readyset.plan_action(report, aid)
                self.assertEqual(str(caught.exception), expected)

    def test_unknown_id_is_refused_without_echoing_it(self):
        report = readyset.read_status(FakeReadyset())
        for bad_id in ("evil_action", "drop_all_caches; SHUTDOWN", "shutdown ' OR 1=1 --"):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError) as caught:
                    readyset.plan_action(report, bad_id)
                self.assertEqual(
                    str(caught.exception),
                    "That change is not offered on this Readyset now. Refresh and try again.",
                )
                self.assertNotIn(bad_id, str(caught.exception))

    def test_row_target_absent_from_the_fresh_read_is_refused(self):
        report = readyset.read_status(FakeReadyset())
        with self.assertRaises(ValueError) as caught:
            readyset.plan_action(report, "drop_cache",
                                 {"kind": "cache", "name": "c_gone", "query_id": "q_0000ffff"})
        self.assertEqual(
            str(caught.exception),
            "That change is not offered on this Readyset now. Refresh and try again.",
        )
        with self.assertRaises(ValueError) as caught2:
            readyset.plan_action(report, "resnapshot_table", {"kind": "table", "table": "shop.missing"})
        self.assertEqual(str(caught2.exception),
                         "That change is not offered on this Readyset now. Refresh and try again.")

    def test_drop_all_caches_has_no_value_scope(self):
        report = readyset.read_status(FakeReadyset())
        # Lane K2: one id builds one statement shape; any non-null legacy scope
        # value is refused, never silently broadened to DROP ALL CACHES.
        for value in ("shallow", "deep", "all", {"scope": "shallow"}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    readyset.plan_action(report, "drop_all_caches", None, value)
                self.assertEqual(
                    str(caught.exception),
                    "Use Drop shallow caches or Drop deep caches for a scoped drop.")
        plan = readyset.plan_action(report, "drop_all_caches", None, None)
        self.assertEqual(plan["statements"], ["DROP ALL CACHES;"])
        self.assertEqual(plan["expect"]["scope"], "all")

    def test_apply_after_state_flip_is_refused_audited_once_and_sends_nothing(self):
        class StubAudit(object):
            def __init__(self):
                self.outcomes = []

            def server(self, *args, **kwargs):
                pass

            def outcome(self, *args, **kwargs):
                self.outcomes.append((args, kwargs))
                return 1

        conn = FakeReadyset()  # replication Running at review
        reviewed = readyset.plan_action(readyset.read_status(conn), "stop_replication")
        conn.repl_status = "Stopped"  # the state flips before the send
        audit = StubAudit()
        res = readyset.execute_action(conn, reviewed, audit=audit, typed="STOP REPLICATION")
        self.assertEqual(res["status"], "refused", res)
        self.assertEqual(res["outcome"], "changed")
        self.assertEqual(
            res["message"],
            "Replication is already Stopped. Only running replication can be stopped. Nothing was sent.",
        )
        self.assertEqual(len(audit.outcomes), 1, audit.outcomes)
        self.assertEqual(audit.outcomes[0][1].get("outcome"), "changed")
        self.assertEqual(conn.writes, [])

    def test_typed_guards_keep_v350_contract_except_reported_resnapshot_name(self):
        conn = FakeReadyset()
        report = readyset.read_status(conn)
        admin_report = readyset.read_status(FakeReadyset(port=6036, database=None, admin=True))
        maint_report = readyset.read_status(FakeReadyset())
        maint_conn = FakeReadyset()
        maint_conn.status_text = "Maintenance Mode"
        maint_report = readyset.read_status(maint_conn)
        stopped_conn = FakeReadyset()
        stopped_conn.repl_status = "Stopped"
        stopped_report = readyset.read_status(stopped_conn)
        shallow_row = {"kind": "cache", "name": "c_recent_shallow", "query_id": "q_2222bbbb"}
        cases = {
            "create_cache": (report, None, {"cache_type": "shallow", "query_or_id": "q_3333cccc"}),
            "flush_cache": (report, shallow_row, None),
            "drop_cache": (report, shallow_row, None),
            "flush_all_shallow_caches": (report, None, None),
            "drop_all_caches": (report, None, None),
            "drop_all_shallow_caches": (report, None, None),
            "drop_all_deep_caches": (report, None, None),
            "drop_all_proxied_queries": (report, None, None),
            "enter_maintenance_mode": (report, None, None),
            "exit_maintenance_mode": (maint_report, None, None),
            "set_log_level": (report, None, "info"),
            "set_eviction_memory_limit": (report, None, 536870912),
            "set_eviction_period": (report, None, 5000),
            "add_shallow_allowed": (report, {"kind": "function"}, {"allow_kind": "function", "name": "CURDATE"}),
            "drop_shallow_allowed": (report, {"kind": "function", "name": "NOW"}, None),
            "flush_privileges": (report, None, None),
            "resnapshot_table": (report, {"kind": "table", "table": "shop.orders"}, None),
            "add_tables": (report, None, {"tables": ["shop.orders"]}),
            "stop_replication": (report, None, None),
            "start_replication": (stopped_report, None, None),
            "set_replication_position": (report, None, {"position": "aaaa:1-120"}),
            "shutdown": (admin_report, None, None),
            "shutdown_reset": (admin_report, None, None),
        }
        self.assertEqual(sorted(cases), sorted(self.TYPED_GUARDS))
        for action, expected in self.TYPED_GUARDS.items():
            rep, target, value = cases[action]
            with self.subTest(action=action):
                plan = readyset.plan_action(rep, action, target, value)
                self.assertEqual(plan["typed_guard"], expected)
        # The dynamic guards of v3.5.0 stay dynamic, with the same values.
        deep_drop = readyset.plan_action(
            report, "drop_cache", {"kind": "cache", "name": "c_orders_deep", "query_id": "q_1111aaaa"})
        self.assertEqual(deep_drop["typed_guard"], "c_orders_deep")
        two_shallow = FakeReadyset()
        two_shallow.caches.append({
            "query_id": "q_5555eeee", "name": "c_second_shallow",
            "query": "SELECT id FROM orders", "properties": "shallow (ttl = 30s)",
            "count": "5", "database": "shop", "kind": "shallow",
        })
        flush_two = readyset.plan_action(readyset.read_status(two_shallow), "flush_all_shallow_caches")
        self.assertEqual(flush_two["typed_guard"], "FLUSH ALL SHALLOW CACHES")
        zero_limit = readyset.plan_action(report, "set_eviction_memory_limit", None, 0)
        self.assertEqual(zero_limit["typed_guard"], "0")

    def test_database_binding_reports_none_when_no_default_database_is_selected(self):
        class NoDatabaseAdapter(FakeReadyset):
            def run(self, sql, extra_flags=None, timeout=None):
                if sql == "SELECT 1 FROM replication_status LIMIT 0":
                    raise ConnectorError("ERROR 1046: No database selected")
                return super().run(sql, extra_flags, timeout)

        conn = NoDatabaseAdapter(database=None)
        report = readyset.read_status(conn)
        for action, value in (("create_cache", {"query_or_id": "SELECT 1"}),
                              ("add_tables", {"tables": ["shop.orders"]})):
            with self.subTest(action=action):
                plan = readyset.plan_action(report, action, value=value, conn=conn)
                self.assertIn("session_database", plan["expect"])
                self.assertIsNone(plan["expect"]["session_database"])
                self.assertIn(["Default database", "none"], plan["facts"])

    def test_ddl_blocked_wording(self):
        self.assertEqual(readyset._DDL_BLOCKED, (
            "This Readyset listener refuses cache DDL.",
            "Run cache changes on the CACHE_DDL_ADDRESS listener or the admin SQL port (default 6036); "
            "CREATE CACHE still needs an upstream database selected.",
        ))

    def test_action_unavailable_is_one_shared_class(self):
        from myflames import cluster
        self.assertIs(cluster.ActionUnavailable, reviewed_writes.ActionUnavailable)
        self.assertTrue(issubclass(reviewed_writes.ActionUnavailable, ValueError))

    def test_request_error_text_passes_action_unavailable_through_unchanged(self):
        # ui.request_error_text (ui.py 3131-3151) is the one path every
        # workspace 400 answers through; a catalog refusal keeps its exact
        # why_not + fix text across that boundary.
        text = ("This Readyset listener refuses cache DDL. "
                "Run cache changes on the admin SQL port (default 6036).")
        exc = reviewed_writes.ActionUnavailable(text)
        self.assertIs(ui.request_error_text(exc), text)
        self.assertEqual(ui.request_error_text(exc), str(exc))
        wrapped = ConnectorError("ERROR 2013 (HY000): Lost connection to Readyset server during query")
        self.assertNotEqual(ui.request_error_text(wrapped), text)


class TestCliSurface(ReadysetBase):

    def test_cli_json_export_explain_and_fail_on(self):
        conn = FakeReadyset()
        code, out, err, _ = run_cli("readyset", ["--json"], conn)
        self.assertEqual(code, 0, err)
        parsed = json.loads(out)
        assert_matches_schema(self, parsed, SCHEMA)

        code, out, err, _ = run_cli("readyset", ["--export-caches"], conn)
        self.assertEqual(code, 0, err)
        self.assertIn("CREATE DEEP CACHE", out)

        code, out, err, _ = run_cli("readyset", ["--explain-cache", "SELECT COUNT(*) FROM orders WHERE status = 'open'"], conn)
        self.assertEqual(code, 0, err)
        self.assertIn("Shallow cache: yes", out)

        conn.repl_status = "Stopped"
        code, out, err, _ = run_cli("readyset", ["--fail-on", "watch"], conn)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
