"""Tests for watch.py (watch-1.0, capture_evidence) and investigation-1.0 evidence."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _perf_replay import FakeFactory, PASSWORD, ReplayConn, batch
from myflames import locks, sessions, ui, watch
from myflames.connector import ConnectorError
from myflames.perf_common import SERVER_STATE_SQL
from myflames.workspace_store import (
    BUNDLE_MAX_BYTES,
    EVIDENCE_DATA_TRIMMED_NOTE,
    EVIDENCE_TEXT_TRIMMED_NOTE,
    WorkspaceStore,
    validate_bundle,
)

SAMPLE_PLAN = (
    '{"query_block": {"select_id": 1, "cost_info": {"query_cost": "1.00"}, '
    '"table": {"table_name": "t1", "access_type": "ALL", "rows_examined_per_scan": 10, '
    '"rows_produced_per_join": 10, "filtered": "100.00", "cost_info": {"read_cost": "0.90", '
    '"eval_cost": "0.10", "prefix_cost": "1.00", "data_read_per_join": "80"}}}}'
)


def _watch_conn(engine="mysql", version="8.4.11", ps_on="1", threads_running="7",
                pl_rows=(), trx_rows=()):
    """Replays one watch read. Any other statement (performance_schema.data_lock_waits
    included) fails the test: a watch never reads it."""
    source = ("performance_schema.processlist"
              if engine == "mysql" and ps_on in ("1", "ON")
              else "information_schema.PROCESSLIST")
    pl_sql = watch.WATCH_TAG + sessions._processlist_sql(engine, source)
    trx_sql = (locks.LOCKS_TAG + locks.SYSTEM_TIME_ZONE + locks.LOCKS_TAG +
               "SELECT trx_id, trx_state, trx_started, TIMESTAMPDIFF(SECOND, trx_started, NOW()), "
               "trx_requested_lock_id, trx_wait_started, TIMESTAMPDIFF(SECOND, trx_wait_started, NOW()), "
               "trx_mysql_thread_id, LEFT(trx_query, %d), trx_operation_state, trx_tables_in_use, "
               "trx_tables_locked, trx_lock_structs, trx_rows_locked, trx_rows_modified, "
               "trx_isolation_level, trx_is_read_only FROM information_schema.INNODB_TRX "
               "WHERE trx_mysql_thread_id <> CONNECTION_ID() ORDER BY trx_started, trx_id LIMIT 501"
               % locks.TEXT_CAP)
    calls = [
        [watch.WATCH_TAG + SERVER_STATE_SQL, batch([[version, ps_on]])],
        [watch.WATCH_TAG + "SHOW GLOBAL STATUS LIKE 'Threads_running'",
         batch([["Threads_running", str(threads_running)]] if threads_running is not None else [])],
        [pl_sql, batch(list(pl_rows))],
        [trx_sql, batch(list(trx_rows))],
    ]
    return ReplayConn(calls)


def _denied_trx(sql, text, _n):
    if "INNODB_TRX" in sql:
        raise ConnectorError("ERROR 1227 (42000): Access denied; you need (at least one of) "
                             "the PROCESS privilege(s) for this operation")
    return text


class TestWatchReport(unittest.TestCase):
    def test_mysql_watch_report_picks_longest_client_statement_and_lock_wait(self):
        pl_rows = [
            # System thread and own connection must be ignored for longest_statement.
            [10, "system user", "", None, "Connect", 9999, "Waiting for source", None, None],
            [11, "app", "10.0.0.1:4000", "shop", "Query", 500, "executing",
             "/*myflames:top*/ SELECT 1", 25],
            [12, "app", "10.0.0.2:4001", "shop", "Sleep", 300, "", None, None],
            [41, "alice", "10.0.0.3:4002", "orders", "Query", 14, "Sending data",
             "SELECT * FROM orders", 20],
            [42, "bob", "10.0.0.4:4003", "billing", "Execute", 37, "updating",
             "UPDATE invoices SET paid=1", 26],
            [43, "carol", "10.0.0.5:4004", "orders", "Query", 9,
             "Waiting for table metadata lock", "ALTER TABLE orders ADD COLUMN c INT", 35],
        ]
        trx_rows = [
            ["9001", "LOCK WAIT", "2026-10-27 14:00:00", "25", "lock-1",
             "2026-10-27 14:00:05", "19", "42", "UPDATE invoices SET paid=1",
             "starting index read", "1", "1", "2", "1", "0", "REPEATABLE READ", "0"],
        ]
        conn = _watch_conn(engine="mysql", version="8.4.11", ps_on="1",
                           threads_running="12", pl_rows=pl_rows, trx_rows=trx_rows)
        report = watch.watch_report(conn)
        self.assertEqual(report["schema_version"], "watch-1.0")
        self.assertEqual(report["engine"], "mysql")
        self.assertEqual(report["threads_running"], 12)
        self.assertEqual(report["longest_statement"], {
            "id": 42,
            "seconds": 37,
            "user": "bob",
            "db": "billing",
        })
        self.assertEqual(report["longest_lock_wait"], {
            "id": 42,
            "seconds": 19,
        })
        # Connection 43 waits for a metadata lock: reported with its statement's
        # running time, apart from the row lock wait.
        self.assertEqual(report["longest_waiting_statement"], {
            "id": 43, "seconds": 9, "state": "Waiting for table metadata lock"})
        self.assertEqual(report["errors"], {
            "threads_running": None,
            "longest_statement": None,
            "longest_lock_wait": None,
            "longest_waiting_statement": None,
        })
        self.assertEqual(report["notes"], [])
        self.assertIn("Threads running: 12", watch.format_text(report))

    def test_mariadb_watch_report_keeps_metadata_lock_waits_apart_from_row_lock_waits(self):
        pl_rows = [
            [77, "report_user", "localhost", "analytics", "Query", 22.5,
             "Waiting for table metadata lock", "OPTIMIZE TABLE events", 21,
             0, 0, None, 8192, 100],
            [78, "web", "localhost", "analytics", "Query", 8.2,
             "Sending data", "SELECT COUNT(*) FROM events", 27,
             0, 0, None, 4096, 50],
        ]
        trx_rows = [
            ["500", "LOCK WAIT", "2026-10-27 14:00:00", "10", "lock-9",
             "2026-10-27 14:00:04", "6", "78", "SELECT COUNT(*) FROM events",
             "fetching rows", "1", "1", "1", "1", "0", "REPEATABLE READ", "0"],
        ]
        conn = _watch_conn(engine="mariadb", version="11.4.13-MariaDB", ps_on="0",
                           threads_running="4", pl_rows=pl_rows, trx_rows=trx_rows)
        report = watch.watch_report(conn)
        self.assertEqual(report["engine"], "mariadb")
        self.assertEqual(report["threads_running"], 4)
        self.assertEqual(report["longest_statement"], {
            "id": 77,
            "seconds": 22.5,
            "user": "report_user",
            "db": "analytics",
        })
        # The row lock wait is InnoDB's wait time (6 s on 78). Connection 77's
        # metadata lock wait has no recorded start: its statement has run 22.5 s
        # (TIME_MS), reported apart and never as the lock wait.
        self.assertEqual(report["longest_lock_wait"], {"id": 78, "seconds": 6})
        self.assertEqual(report["longest_waiting_statement"], {
            "id": 77, "seconds": 22.5, "state": "Waiting for table metadata lock"})
        self.assertIn("Statement waiting for a metadata or table lock: connection 77", watch.format_text(report))

    def test_row_lock_wait_is_innodb_wait_time_never_statement_time(self):
        # Connection 42's statement has run 60 s, but it began waiting for its
        # row lock 2 s ago: a "lock wait older than 30 s" must not trip.
        pl_rows = [
            [42, "bob", "10.0.0.4:4003", "billing", "Query", 60, "updating",
             "UPDATE invoices SET paid=1", 26],
        ]
        trx_rows = [
            ["9001", "LOCK WAIT", "2026-10-27 14:00:00", "90", "lock-1",
             "2026-10-27 14:01:28", "2", "42", "UPDATE invoices SET paid=1",
             "starting index read", "1", "1", "2", "1", "0", "REPEATABLE READ", "0"],
            # A wait without a recorded start has no measured age: never judged.
            ["9002", "LOCK WAIT", "2026-10-27 13:00:00", "3600", "lock-2",
             None, None, "43", "UPDATE invoices SET paid=0",
             "starting index read", "1", "1", "2", "1", "0", "REPEATABLE READ", "0"],
        ]
        report = watch.watch_report(_watch_conn(pl_rows=pl_rows + [
            [43, "eve", "10.0.0.6:4005", "billing", "Query", 3600, "updating", "UPDATE invoices SET paid=0", 26],
        ], trx_rows=trx_rows))
        self.assertEqual(report["longest_lock_wait"], {"id": 42, "seconds": 2})
        self.assertIsNone(report["errors"]["longest_lock_wait"])

    def test_mysql_without_process_never_reads_data_lock_waits(self):
        # No INNODB_TRX: row lock waits have no measured age; data_lock_waits
        # records none and holds the lock-system latch, so it is not read
        # (ReplayConn fails on any statement it was not given).
        pl_rows = [[42, "app", "10.0.0.4:4003", "shop", "Query", 60, "updating", "UPDATE t SET a=1", 16]]
        conn = _watch_conn(pl_rows=pl_rows)
        conn.edit = _denied_trx
        report = watch.watch_report(conn)
        self.assertIsNone(report["longest_lock_wait"])
        self.assertIn("PROCESS", report["errors"]["longest_lock_wait"])
        self.assertFalse(any("data_lock_waits" in sql for sql, _ in conn.calls))
        # The statement is judged from the account's own sessions only, and says so.
        self.assertEqual(report["longest_statement"]["id"], 42)
        self.assertIn("only this account's sessions", report["errors"]["longest_statement"])

    def test_without_process_row_waits_are_unread_and_lock_waiters_reported_apart(self):
        pl_rows = [
            [77, "report_user", "localhost", "analytics", "Query", 22.5,
             "Waiting for table metadata lock", "OPTIMIZE TABLE events", 21, 0, 0, None, 8192, 100],
            [79, "batch", "localhost", "analytics", "Query", 5.0,
             "Waiting for table level lock", "UPDATE t SET a = 1", 18, 0, 0, None, 4096, 10],
        ]
        conn = _watch_conn(engine="mariadb", version="11.4.13-MariaDB", ps_on="0", pl_rows=pl_rows)
        conn.edit = _denied_trx
        report = watch.watch_report(conn)
        # No row lock wait time can be read; the metadata lock waiter is still
        # listed (its statement's running time), never as the lock wait.
        self.assertIsNone(report["longest_lock_wait"])
        self.assertIn("Row lock wait times need the PROCESS privilege", report["errors"]["longest_lock_wait"])
        self.assertIn(report["errors"]["longest_lock_wait"], report["notes"])
        self.assertEqual(report["longest_waiting_statement"], {
            "id": 77, "seconds": 22.5, "state": "Waiting for table metadata lock"})
        self.assertIn("only this account's sessions", report["errors"]["longest_waiting_statement"])
        self.assertIn("Longest row lock wait: not read", watch.format_text(report))

    def test_long_statement_waiting_for_a_lock_is_never_a_long_lock_wait(self):
        # An ALTER TABLE that copied rows for 600 s and now waits for its metadata
        # lock upgrade, and a 300 s statement waiting for a table-level lock:
        # process list TIME counts from the statement's start, so neither is a
        # 600 s or 300 s lock wait.
        pl_rows = [
            [60, "dba", "localhost", "shop", "Query", 600, "Waiting for table metadata lock",
             "ALTER TABLE orders ADD COLUMN c INT", 35],
            [61, "app", "localhost", "shop", "Query", 300, "Waiting for table level lock",
             "UPDATE legacy_myisam SET a = 1", 30],
        ]
        report = watch.watch_report(_watch_conn(pl_rows=pl_rows))
        self.assertIsNone(report["longest_lock_wait"])
        self.assertIsNone(report["errors"]["longest_lock_wait"])
        self.assertEqual(report["longest_waiting_statement"], {
            "id": 60, "seconds": 600, "state": "Waiting for table metadata lock"})
        # The table-level lock waiter alone is reported the same way.
        report = watch.watch_report(_watch_conn(pl_rows=pl_rows[1:]))
        self.assertIsNone(report["longest_lock_wait"])
        self.assertEqual(report["longest_waiting_statement"], {
            "id": 61, "seconds": 300, "state": "Waiting for table level lock"})

    def test_subread_errors_become_null_fields_with_reasons(self):
        def edit(sql, text, _n):
            if "Threads_running" in sql:
                raise ConnectorError("ERROR 1227 (42000): Access denied for status")
            if "processlist" in sql.lower():
                raise ConnectorError("ERROR 1142 (42000): SELECT command denied on processlist")
            if "INNODB_TRX" in sql:
                raise ConnectorError("ERROR 1227 (42000): Access denied; you need PROCESS privilege")
            return text

        conn = _watch_conn(engine="mariadb", version="11.4.13-MariaDB", ps_on="0")
        conn.edit = edit
        report = watch.watch_report(conn)
        self.assertIsNone(report["threads_running"])
        self.assertIsNone(report["longest_statement"])
        self.assertIsNone(report["longest_lock_wait"])
        self.assertIn("Access denied", report["errors"]["threads_running"])
        self.assertIn("SELECT command denied", report["errors"]["longest_statement"])
        self.assertIn("PROCESS", report["errors"]["longest_lock_wait"])
        self.assertTrue(report["reason"])
        self.assertEqual(len(report["notes"]), 3)

    def test_server_preflight_failure_returns_null_fields_with_reason(self):
        def edit(sql, _text, _n):
            if SERVER_STATE_SQL in sql:
                raise ConnectorError("ERROR 2013 (HY000): Lost connection to MySQL server")
            return _text

        conn = _watch_conn()
        conn.edit = edit
        report = watch.watch_report(conn)
        self.assertIsNone(report["engine"])
        self.assertIsNone(report["threads_running"])
        self.assertIsNone(report["longest_statement"])
        self.assertIsNone(report["longest_lock_wait"])
        self.assertIn("Lost connection", report["reason"])

    def test_ui_watch_and_capture_endpoints(self):
        conn = _watch_conn(
            engine="mysql",
            version="8.4.11",
            ps_on="1",
            threads_running="9",
            pl_rows=[[55, "app", "10.0.0.1:5000", "shop", "Query", 18, "executing", "SELECT 1", 8]],
            trx_rows=[],
        )
        server = mock.MagicMock()
        server.store = mock.MagicMock()
        with mock.patch("myflames.connector.MySQLConnection", FakeFactory(conn)):
            res = ui.api_response("/api/live/watch", {
                "connection": {"host": "127.0.0.1", "port": 3306, "user": "root", "password": PASSWORD},
            }, server)
        self.assertEqual(res["schema_version"], "watch-1.0")
        self.assertEqual(res["threads_running"], 9)
        self.assertEqual(res["longest_statement"]["id"], 55)

        fake_sessions = {
            "schema_version": "sessions-1.0",
            "engine": "mysql",
            "server_version": "8.4.11",
            "filters": {"active_only": False},
            "summary": {
                "status": "ok",
                "text": "1 session listed · 1 active",
                "total": 1, "active": 1, "idle": 0, "system": 0, "idle_in_trx": 0,
                "lock_wait": {"total": 0, "innodb": 0, "metadata": 0, "table": 0},
                "long_running": {"watch": 0, "problem": 0},
                "connections": {"current": 2, "max": 151, "max_used": 4, "running": 2,
                                "listed": 1, "used_pct": 1.3, "peak_pct": 2.6, "status": "ok", "rule": ""},
                "by_command": [], "by_state": [], "by_user": [], "by_host": [], "by_db": [],
                "oldest_transaction": None, "history_list_length": None,
            },
            "sessions": [],
            "matched": 0,
            "truncated": False,
            "notes": [],
        }
        fake_innodb = {
            "schema_version": "innodb-1.0",
            "engine": "mysql",
            "server_version": "8.4.11",
            "header": {"printed_at": "2026-10-27 14:02:00", "window_s": 10},
            "summary": {"status": "ok", "text": "No InnoDB issues detected."},
            "sections": [],
            "notes": [],
        }
        fake_locks = {
            "schema_version": "locks-1.1",
            "engine": "mysql",
            "server_version": "8.4.11",
            "summary": {
                "open_transactions": 0, "waiting_transactions": 0, "row_locks": 0,
                "row_lock_waits": 0, "metadata_lock_waits": 0, "ddl_waiting": 0,
                "root_blockers": 0, "deadlock_detected": False,
            },
            "capabilities": {},
            "blocking_tree": [],
            "ddl_waits": [],
            "transactions": [],
            "locks": [],
            "waits": [],
            "metadata_locks": [],
            "deadlock": None,
            "notes": [],
        }
        with mock.patch("myflames.connector.MySQLConnection", FakeFactory(conn)), \
                mock.patch("myflames.sessions.sessions_report", return_value=fake_sessions), \
                mock.patch("myflames.innodb_status.innodb_report", return_value=fake_innodb), \
                mock.patch("myflames.locks.lock_report", return_value=fake_locks):
            cap = ui.api_response("/api/live/watch/capture", {
                "connection": {"host": "127.0.0.1", "port": 3306, "user": "root", "password": PASSWORD},
                "watch": res,
            }, server)
        self.assertEqual(cap["schema_version"], "watch-capture-1.0")
        self.assertEqual([e["kind"] for e in cap["evidence"]], ["watch", "sessions", "innodb", "locks"])
        for item in cap["evidence"]:
            self.assertTrue(item["text"].strip())
            self.assertIsInstance(item["data"], dict)


class TestInvestigationEvidence(unittest.TestCase):
    def test_plans_only_bundle_stays_unchanged(self):
        bundle = validate_bundle({
            "schema_version": "investigation-1.0",
            "name": "Plan check",
            "notes": "Baseline only",
            "tags": ["oltp"],
            "baseline_id": "p1",
            "plans": [{"id": "p1", "name": "Query 1", "raw": SAMPLE_PLAN, "notes": ""}],
        })
        self.assertNotIn("evidence", bundle)

    def test_validate_bundle_accepts_evidence_and_populates_format_text(self):
        watch_data = {
            "schema_version": "watch-1.0",
            "engine": "mysql",
            "server_version": "8.4.11",
            "threads_running": 57,
            "longest_statement": {"id": 99, "seconds": 42, "user": "app", "db": "prod"},
            "longest_lock_wait": {"id": 102, "seconds": 15},
            "read_at": "2026-10-27T14:02:31+00:00",
            "errors": {"threads_running": None, "longest_statement": None, "longest_lock_wait": None},
            "notes": [],
        }
        bundle = validate_bundle({
            "schema_version": "investigation-1.0",
            "name": "Capture: threads running 57 on prod-eu, 2026-10-27 14:02",
            "notes": "",
            "tags": ["watch"],
            "baseline_id": "",
            "plans": [],
            "evidence": [
                {
                    "id": "ev-watch",
                    "kind": "watch",
                    "captured_at": "2026-10-27T14:02:31Z",
                    "title": "Watch trigger",
                    "text": "",
                    "data": watch_data,
                },
                {
                    "id": "ev-sessions",
                    "kind": "sessions",
                    "captured_at": "2026-10-27T14:02:32Z",
                    "title": "Processlist",
                    "text": "MySQL 8.4.11\nSUMMARY\n  Sessions 57 active\n",
                    "data": {"schema_version": "sessions-1.0", "matched": 57},
                },
            ],
        })
        self.assertEqual(len(bundle["evidence"]), 2)
        self.assertIn("Threads running: 57", bundle["evidence"][0]["text"])
        self.assertEqual(bundle["evidence"][1]["title"], "Processlist")

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp) / "workspace.sqlite3")
            saved = store.save("investigation", bundle)
            self.assertEqual(len(saved["evidence"]), 2)
            listed = store.list_investigations()
            self.assertEqual(len(listed["items"][0]["evidence"]), 2)
            self.assertEqual(listed["items"][0]["evidence"][0]["kind"], "watch")

    def test_evidence_is_stored_without_credentials(self):
        statements = [
            "ALTER USER app IDENTIFIED BY 'alter-secret'",
            "SET PASSWORD FOR app = PASSWORD('fn-secret')",
            "CREATE USER u IDENTIFIED WITH caching_sha2_password AS '$A$005$hash-secret'",
        ]
        sessions_data = {
            "schema_version": "sessions-1.0", "engine": "mysql", "server_version": "8.4.11",
            "summary": {"text": "3 sessions"}, "notes": [],
            "sessions": [{"id": 50 + i, "user": "root", "host": "localhost", "db": None,
                          "command": "Query", "time_s": 3, "state": "starting",
                          "info": sql, "info_short": sql[:40], "thread_kind": "client"}
                         for i, sql in enumerate(statements)],
        }
        locks_data = {
            "schema_version": "locks-1.1", "engine": "mysql", "server_version": "8.4.11",
            "transactions": [{"connection_id": 50, "current_statement": statements[0],
                              "query": statements[2]}],
            "waits": [{"statement": statements[1]}], "notes": [],
        }
        innodb_data = {
            "schema_version": "innodb-1.0", "engine": "mysql", "server_version": "8.4.11",
            "sections": [{"key": "transactions", "lines": [statements[0], statements[2]]}],
            "raw": {"text": "\n".join(statements), "truncated": False}, "notes": [],
        }
        bundle = validate_bundle({
            "schema_version": "investigation-1.0", "name": "Capture", "plans": [],
            "evidence": [
                {"id": "s", "kind": "sessions", "captured_at": "2026-10-27T14:02:31Z",
                 "title": "Processlist", "text": "", "data": sessions_data},
                {"id": "l", "kind": "locks", "captured_at": "2026-10-27T14:02:31Z",
                 "title": "Locks", "text": "", "data": locks_data},
                # Text sent by a client is redacted too.
                {"id": "i", "kind": "innodb", "captured_at": "2026-10-27T14:02:31Z",
                 "title": "InnoDB status", "text": "\n".join(statements), "data": innodb_data},
            ],
        })
        stored = json.dumps(bundle)
        for secret in ("alter-secret", "fn-secret", "hash-secret"):
            self.assertNotIn(secret, stored)
        self.assertIn("[redacted]", bundle["evidence"][0]["data"]["sessions"][0]["info"])
        self.assertIn("[redacted]", bundle["evidence"][2]["text"])
        # The formatted text comes from the redacted data and still lists the statements.
        self.assertIn("ALTER USER app IDENTIFIED BY", bundle["evidence"][0]["text"] + bundle["evidence"][1]["text"])
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp) / "workspace.sqlite3")
            saved = store.save("investigation", bundle)
            again = json.dumps(store.get("investigation", saved["id"]))
            for secret in ("alter-secret", "fn-secret", "hash-secret"):
                self.assertNotIn(secret, again)

    def test_validate_bundle_refuses_unknown_evidence_kind_and_bad_bounds(self):
        base = {
            "schema_version": "investigation-1.0",
            "name": "Bad evidence",
            "plans": [],
        }
        with self.assertRaises(ValueError):
            validate_bundle(dict(base, evidence=[{
                "id": "e1", "kind": "unknown_kind", "captured_at": "2026-10-27T14:00:00Z",
                "title": "Bad", "text": "x", "data": None,
            }]))
        with self.assertRaises(ValueError):
            validate_bundle(dict(base, evidence=[
                {"id": "dup", "kind": "locks", "captured_at": "2026-10-27T14:00:00Z", "title": "A", "text": "a", "data": None},
                {"id": "dup", "kind": "innodb", "captured_at": "2026-10-27T14:00:00Z", "title": "B", "text": "b", "data": None},
            ]))
        with self.assertRaises(ValueError):
            validate_bundle(dict(base, evidence=[
                {"id": "e-%d" % i, "kind": "locks", "captured_at": "2026-10-27T14:00:00Z", "title": "T", "text": "x", "data": None}
                for i in range(31)
            ]))

    def test_validate_bundle_trims_data_before_text_when_too_large(self):
        # 6 MiB of JSON data + 5 MiB of text > 10 MiB total bundle; trimming data
        # first brings the bundle under 10 MiB while keeping the original text.
        big_data = {"blob": "d" * (6 * 1024 * 1024)}
        original_text = "header line\n" + ("t" * (5 * 1024 * 1024))
        bundle = validate_bundle({
            "schema_version": "investigation-1.0",
            "name": "Large evidence",
            "plans": [],
            "evidence": [{
                "id": "ev-1",
                "kind": "innodb",
                "captured_at": "2026-10-27T14:02:00Z",
                "title": "InnoDB status",
                "text": original_text,
                "data": big_data,
            }],
        })
        item = bundle["evidence"][0]
        self.assertIsNone(item["data"])
        self.assertTrue(item.get("data_trimmed"))
        self.assertFalse(item.get("text_trimmed", False))
        self.assertIn(EVIDENCE_DATA_TRIMMED_NOTE, item["note"])
        self.assertIn(EVIDENCE_DATA_TRIMMED_NOTE, item["text"])
        self.assertTrue(item["text"].startswith("header line\n" + ("t" * 1024)))

    def test_validate_bundle_trims_text_when_still_over_10_mib_after_data_trimmed(self):
        huge_text = "x" * (BUNDLE_MAX_BYTES + 500000)
        bundle = validate_bundle({
            "schema_version": "investigation-1.0",
            "name": "Huge text evidence",
            "plans": [],
            "evidence": [{
                "id": "ev-1",
                "kind": "sessions",
                "captured_at": "2026-10-27T14:02:00Z",
                "title": "Sessions",
                "text": huge_text,
                "data": {"blob": "y" * 10000},
            }],
        })
        item = bundle["evidence"][0]
        self.assertIsNone(item["data"])
        self.assertTrue(item.get("data_trimmed"))
        self.assertTrue(item.get("text_trimmed"))
        self.assertIn(EVIDENCE_TEXT_TRIMMED_NOTE, item["note"])
        self.assertIn(EVIDENCE_TEXT_TRIMMED_NOTE, item["text"])


if __name__ == "__main__":
    unittest.main()
