"""Unit, schema, action-safety, CLI and HTTP tests for :mod:`myflames.readyset`.

Runs entirely with a temporary HOME and ``MYFLAMES_SECRET_BACKEND=vault`` so
developer profiles and the OS keychain are never touched.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape as xml_escape

from _perf_replay import run_cli
from myflames import dbops, readyset, reviewed_writes, topology_links, ui
from myflames.connector import ConnectorError

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
            {"table": "`shop`.`orders`", "status": "Snapshotted", "description": ""},
            {"table": "`shop`.`audit_blob`", "status": "Ignored", "description": "Unsupported column type"},
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
            target = s.split("DROP CACHE ", 1)[1].strip().strip("`").replace("``", "`")
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
            val = ident.strip().strip("`").replace("``", "`")
            if val not in self.allowlists[plural]:
                self.allowlists[plural].append(val)
        elif upper.startswith("ALTER READYSET DROP SHALLOW CACHE ALLOWED "):
            rest = s[len("ALTER READYSET DROP SHALLOW CACHE ALLOWED "):]
            kind_word, ident = rest.split(" ", 1)
            plural = kind_word.upper() + "S"
            val = ident.strip().strip("`").replace("``", "`")
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
            if self.admin:
                return xml_rows([])
            raise ConnectorError("ERROR 1146 (42S02): Table 'shop.replication_status' doesn't exist")
        if upper == "SHOW DATABASES":
            if self.admin:
                return xml_rows([{"Database": "information_schema"}, {"Database": "public"}, {"Database": "readyset"}])
            return xml_rows([{"Database": "information_schema"}, {"Database": "shop"}, {"Database": "readyset"}])
        if upper == "SHOW READYSET STATUS":
            return xml_rows([
                {"name": "Database Connection", "value": "Connected"},
                {"name": "Connection Count", "value": "5"},
                {"name": "Status", "value": self.status_text},
                {"name": "Replication Status", "value": self.repl_status},
                {"name": "CDC Server", "value": "mysql-primary:3306"},
                {"name": "Maximum Replication Offset", "value": "aaaa:1-100"},
                {"name": "Minimum Replication Offset", "value": "aaaa:1-100"},
                {"name": "Readyset Controller Status", "value": "Online"},
                {"name": "Last completed snapshot", "value": "2026-10-20 12:00:00 UTC"},
                {"name": "Last replicator error", "value": self.last_error},
                {"name": "Host CPUs", "value": "8"},
            ])
        if upper == "SHOW CACHES":
            if self.cloud_blocked:
                raise ConnectorError("ERROR 1105 (HY000): Please manage caches via Readyset Cloud UI")
            return xml_rows([
                {k: v for k, v in c.items() if k != "kind"}
                for c in self.caches
            ])
        if upper.startswith("SHOW PROXIED ") and "QUERIES" in upper:
            return xml_rows(self.proxied)
        if upper == "SHOW READYSET ALL TABLES":
            return xml_rows(self.tables)
        if upper == "SHOW CONNECTIONS":
            return xml_rows([
                {"client_addr": "172.18.0.5:51234", "username": "app", "upstream": "mysql://readyset:secret@mysql-primary:3306/shop"},
            ])
        for kind in ("FUNCTIONS", "VARIABLES", "SCHEMAS"):
            if upper == "SHOW SHALLOW CACHE ALLOWED %s" % kind:
                return xml_rows([{"name": item} for item in self.allowlists[kind]])
        if "UPSTREAM_SERVERS" in upper:
            return xml_rows([
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
            ])
        if "REPLICATION_STATUS" in upper:
            if self.repl_mode == "mysql_file":
                return xml_rows([{
                    "replication_mode": "mysql_file",
                    "upstream_offset": "mysql-bin.000003:4096",
                    "replicator_offset": "mysql-bin.000003:3072",
                    "persisted_offset": "mysql-bin.000003:2048",
                    "consume_lag": "1024",
                    "persist_lag": "2048",
                    "heartbeat_staleness_seconds": "2",
                }])
            return xml_rows([{
                "replication_mode": "mysql_gtid",
                "upstream_offset": "aaaa:1-100",
                "replicator_offset": "aaaa:1-100",
                "persisted_offset": "aaaa:1-100",
                "consume_lag": "0",
                "persist_lag": "0",
                "heartbeat_staleness_seconds": "1",
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
            return xml_rows([{
                "query_id": "q_2222bbbb", "wasted_refreshes": "2", "dropped_refreshes": "0",
                "load_actual_ppm": "1200", "load_baseline_ppm": "1000", "over_cap": "false", "scheduler_queue_len": "0",
            }])
        if "SHALLOW_CACHE_COALESCE_STATS" in upper:
            return xml_rows([{
                "query_id": "q_2222bbbb", "coalesce_timeouts": "0", "coalesce_aborts": "0",
                "total_us": "4000", "avg_us": "500", "upstream_saved_total_us": "24000", "upstream_saved_avg_us": "3000",
            }])
        if "CACHED_QUERY_STATS" in upper:
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
                {"query": "CREATE SHALLOW CACHE `c_recent_shallow` WITH (ttl = 30s) FROM SELECT id, status FROM orders WHERE created_at > '2026-01-01';", "database": "shop"},
            ])
        if upper == "EXPLAIN CACHES":
            return xml_rows([
                {"query": "CREATE DEEP CACHE `c_orders_deep` FROM SELECT id, total FROM orders WHERE customer_id = 1;"},
                {"query": "USE `shop`; CREATE SHALLOW CACHE `c_recent_shallow` WITH (ttl = 30s) FROM SELECT id, status FROM orders WHERE created_at > '2026-01-01';"},
            ])
        if upper.startswith("EXPLAIN CREATE"):
            if self.admin or (self.database or "").lower() == "readyset":
                raise ConnectorError("ERROR 1105 (HY000): EXPLAIN CREATE CACHE must run on the SQL adapter port")
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
        self.assertNotIn("create_cache", acts)
        self.assertIn("shutdown", acts)
        self.assertIn("shutdown_reset", acts)

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
        conn.repl_status = "Error"
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


class TestExplainAndExportCaches(ReadysetBase):

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

    def test_export_caches_returns_ddl_statements(self):
        conn = FakeReadyset()
        exp = readyset.export_caches(conn)
        self.assertEqual(exp["schema_version"], "readyset-export-1.0")
        self.assertEqual(exp["count"], 2)
        self.assertIn("CREATE DEEP CACHE", exp["sql"])
        self.assertIn("CREATE SHALLOW CACHE", exp["sql"])

        legacy = FakeReadyset()
        legacy.legacy_explain_caches = True
        exp_legacy = readyset.export_caches(legacy)
        self.assertEqual(exp_legacy["count"], 2)
        self.assertIn("CREATE DEEP CACHE", exp_legacy["sql"])
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
        res3 = self.api_send(conn3, plan3)
        self.assertEqual(res3["status"], "unknown")


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

        conn.repl_status = "Error"
        code, out, err, _ = run_cli("readyset", ["--fail-on", "watch"], conn)
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
