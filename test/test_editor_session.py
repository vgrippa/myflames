"""Query editor sessions (myflames/editor_session.py).

Three layers:

* Replay: output captured from the real MySQL 8.4 and MariaDB 11.4 clients
  (test/fixtures/editor/, recorded from throwaway containers with the
  module's own framing) is fed to the session's stdout and stderr parsers in
  uneven chunks: typed values, summary and info lines, errors attributed by
  line number, the MariaDB client's late echo, and a KILL QUERY mid-result.
* Process: a fake client (test/_fake_mysql_client.py, copied as ``mysql``
  onto PATH) runs the real session code end to end: session affinity, row
  limit kill, Stop, transactions, the confirmation policy and audit rows,
  identity, idle timeout, history.
* Pure functions: statement planning, offsets, error positions, exports.

Every test that reaches the workspace database runs with a temporary HOME
and MYFLAMES_SECRET_BACKEND=vault.
"""
import io
import json
import os
from pathlib import Path
import random
import shutil
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from myflames import dbops, editor_session as ed
from myflames.workspace_store import WorkspaceStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "editor"
FAKE = Path(__file__).resolve().parent / "_fake_mysql_client.py"


class _Stdin(object):
    def __init__(self):
        self.stdin = io.BytesIO()


def replay_session(name, chunk_seed=7, crlf=False, chunk_sizes=None):
    """A session whose steps are rebuilt with the module's framing, fed the
    recorded stdout and stderr of fixture *name* in random-size chunks."""
    data = json.loads((FIXTURES / (name + ".json")).read_text(encoding="utf-8"))
    session = ed.EditorSession("t", "k", {"host": "h", "port": 1, "user": "u"})
    session.proc = _Stdin()
    session.delimiter = "__mfcafe0123_"
    session._write("DELIMITER %s\n" % session.delimiter)
    steps = []
    for item in data["steps"]:
        # A user statement with a row limit in these captures is one plain SELECT.
        steps.append(session._send(item["kind"], item["send"], limit=item["limit"], results=item["results"],
                                   kill_ok=item["kind"] == "user" and item["limit"] is not None))
    rng = random.Random(chunk_seed)

    def chunks(text):
        at = 0
        while at < len(text):
            size = rng.choice(chunk_sizes or (1, 3, 17, 64, 500, 4096))
            yield text[at:at + size]
            at += size
    stdout, stderr = data["stdout"], data["stderr"]
    if crlf:
        stdout, stderr = stdout.replace("\n", "\r\n"), stderr.replace("\n", "\r\n")
    from collections import deque
    out = deque(chunks(stdout))
    err = deque(chunks(stderr))
    # Interleave the two pipes the way two reader threads might.
    while out or err:
        with session.lock:
            if out and (not err or rng.random() < 0.7):
                session._feed_stdout(out.popleft())
            else:
                session._feed_stderr(err.popleft())
    return session, steps, data


def rows_of(step, index=0):
    return [e for e in step.entries if e["type"] == "rows"][index]


class ReplayTests(unittest.TestCase):
    """Parsing real client output (both clients)."""

    def test_framing_matches_the_recorded_stdin(self):
        for name in ("mysql-8.4-client-session", "mariadb-11.4-client-session", "mysql-8.4-client-kill"):
            with self.subTest(name=name):
                session, steps, data = replay_session(name)
                self.assertEqual(session.proc.stdin.getvalue().decode("utf-8"), data["stdin"])

    def test_session_capture(self):
        for name in ("mysql-8.4-client-session", "mariadb-11.4-client-session"):
            for seed in (1, 7, 42):
                with self.subTest(name=name, seed=seed):
                    self._check_session(name, seed)

    def _check_session(self, name, seed):
        session, steps, data = replay_session(name, seed)
        mariadb = "mariadb" in name
        self.assertTrue(all(s.marker for s in steps), [s.send for s in steps if not s.marker])
        # Every block but the last is closed by the next echo; the last helper by its summary.
        self.assertTrue(all(s.closed for s in steps))
        self.assertEqual(session.notes, [])
        hello = session._helper_row(steps[0])
        self.assertEqual(hello["myflames_editor"], "f00d")
        self.assertTrue(int(hello["connection_id"]) > 0)
        self.assertEqual("MariaDB" in hello["version"], mariadb)
        self.assertEqual(steps[1].entries, [{"type": "ok", "affected_rows": 0, "warnings": 0, "info": None}])
        values = rows_of(steps[2])
        names = [c["name"] for c in values["columns"]]
        row = dict(zip(names, values["rows"][0]))
        self.assertEqual(names.count("dup"), 2)
        self.assertEqual(values["rows"][0][names.index("dup")], "")
        self.assertEqual(values["rows"][0][len(names) - 1 - names[::-1].index("dup")], "second")
        self.assertIsNone(row["n"])                     # SQL NULL
        self.assertEqual(row["e"], "")                  # empty string
        self.assertEqual(row["s"], "NULL")              # the string 'NULL'
        self.assertEqual(row["b"], "0x00FF41")          # binary as hex
        self.assertEqual(row["emptybin"], "0x")
        self.assertEqual(row["big"], "18446744073709551615")
        self.assertEqual(row["dt"], "2024-01-02 03:04:05")
        self.assertEqual(row["esc"], 'x<y&"z">')
        self.assertEqual(row["ml"], "multi\nline")
        self.assertEqual(row["tb"], "tab\there")
        self.assertEqual(row["uni"], "café \U0001F600")
        self.assertEqual(values["server_rows"], 1)
        self.assertEqual(steps[3].entries[0]["type"], "ok")
        self.assertEqual(rows_of(steps[4])["rows"], [["5"]])        # SET @x persisted
        insert = steps[7].entries[0]
        self.assertEqual((insert["affected_rows"], insert["info"]), (2, "Records: 2  Duplicates: 0  Warnings: 0"))
        update = steps[8].entries[0]
        self.assertEqual((update["affected_rows"], update["info"]), (2, "Rows matched: 2  Changed: 2  Warnings: 0"))
        self.assertEqual(rows_of(steps[9])["rows"], [["2"]])        # ROW_COUNT() of the UPDATE
        empty = rows_of(steps[10])
        self.assertEqual((empty["columns"], empty["rows"], empty["server_rows"]), ([], [], 0))
        division = rows_of(steps[11])
        self.assertEqual((division["rows"], division["warnings"]), ([[None]], 1))
        self.assertEqual(rows_of(steps[12])["rows"][0][1:], ["1365", "Division by 0"])  # SHOW WARNINGS still sees it
        self.assertEqual([(e["code"], e["sqlstate"]) for e in steps[13].errors], [(1146, "42S02")])
        self.assertEqual(steps[13].entries, [])
        syntax = steps[14].errors[0]
        self.assertEqual(syntax["code"], 1064)
        self.assertTrue(syntax["message"].endswith("near '' at line 2"))
        call = steps[17]
        self.assertEqual([e["type"] for e in call.entries], ["rows", "rows", "ok"])
        self.assertEqual([rows_of(call, 0)["rows"], rows_of(call, 1)["rows"]], [[["1"]], [["2"]]])
        self.assertEqual(rows_of(steps[18])["rows"], [["3"]])
        probe = steps[19]
        if mariadb:
            self.assertEqual(probe.errors[0]["code"], 1305)
        else:
            self.assertEqual(session._helper_row(probe)["consumer"], "YES")
        status = session._helper_row(steps[20])
        self.assertEqual(status["myflames_editor"], "a1b2c3")
        self.assertEqual(status["connection_id"], hello["connection_id"])
        self.assertEqual("in_transaction" in status, mariadb)
        # Errors carry the line of their statement, so no error lands on another step.
        for step in steps:
            for error in step.errors:
                self.assertTrue(step.first_line <= error["line"] <= step.marker_line)

    def test_kill_while_rows_stream(self):
        for name in ("mysql-8.4-client-kill", "mariadb-11.4-client-kill"):
            with self.subTest(name=name):
                session, steps, data = replay_session(name)
                big = rows_of(steps[1])
                self.assertEqual(len(big["rows"]), 100)
                self.assertTrue(big["truncated"])
                self.assertEqual(big["stopped"], "row_limit")
                self.assertTrue(steps[1].kill_requested)
                self.assertGreater(big["server_rows"], 100)
                self.assertEqual([e["code"] for e in steps[1].errors], [1317])
                self.assertEqual(rows_of(steps[2])["rows"], [["0"]])
                self.assertTrue(all(s.marker and s.closed for s in steps))

    def test_crlf_recordings_including_split_crlf_pairs(self):
        """Converted recordings, not native Windows captures; raw value LF
        also becomes CRLF in this conversion and must stay that way."""
        for name in ("mysql-8.4-client-session", "mariadb-11.4-client-session",
                     "mysql-8.4-client-kill", "mariadb-11.4-client-kill"):
            _, original, _ = replay_session(name)
            for sizes in (None, (1,)):
                with self.subTest(name=name, sizes=sizes):
                    session, steps, _ = replay_session(name, crlf=True, chunk_sizes=sizes)
                    self.assertFalse(session.ended, session.notes)
                    self.assertEqual(session.notes, [])
                    self.assertTrue(all(s.marker and s.closed for s in steps))
                    for expected, actual in zip(original, steps):
                        entries = json.loads(json.dumps(expected.entries))
                        for entry in entries:
                            if entry["type"] == "rows":
                                entry["rows"] = [[v.replace("\n", "\r\n") if v is not None else None
                                                  for v in row] for row in entry["rows"]]
                        self.assertEqual(actual.entries, entries)
                        self.assertEqual(actual.errors, expected.errors)
                        self.assertEqual(actual.kill_requested, expected.kill_requested)

    def test_mariadb_late_echo_after_an_error(self):
        """The MariaDB client writes an error before flushing the failed
        statement's echo; on separate pipes the echo just arrives later."""
        session = ed.EditorSession("t", "k", {"host": "h", "port": 1, "user": "u"})
        session.proc = _Stdin()
        session.delimiter = "__d_"
        session._write("DELIMITER __d_\n")
        bad = session._send("user", "SELECT * FROM nope")
        good = session._send("user", "SELECT 2")
        with session.lock:
            session._feed_stderr("ERROR 1146 (42S02) at line %d: Table 'test.nope' doesn't exist\n" % bad.first_line)
            session._feed_stderr("ERROR at line %d: %s\n" % (bad.marker_line, "DELIMITER must be followed by x"))
        self.assertTrue(bad.marker)
        self.assertEqual(bad.errors[0]["code"], 1146)
        self.assertFalse(bad.closed)
        with session.lock:
            session._feed_stdout("--------------\nSELECT * FROM nope\n--------------\n\n"
                                 "--------------\nSELECT 2\n--------------\n\n<?xml version=\"1.0\"?>\n\n"
                                 "<resultset statement=\"SELECT 2\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-"
                                 "instance\">\n  <row>\n\t<field name=\"2\">2</field>\n  </row>\n</resultset>\n"
                                 "1 row in set\n\n")
        self.assertTrue(bad.closed)
        self.assertEqual(bad.entries, [])
        self.assertEqual(rows_of(good)["rows"], [["2"]])

    def test_multi_line_error_message(self):
        session = ed.EditorSession("t", "k", {"host": "h", "port": 1, "user": "u"})
        session.proc = _Stdin()
        session.delimiter = "__d_"
        session._write("DELIMITER __d_\n")
        step = session._send("user", "SELECT 'a\nb' FRM t")
        with session.lock:
            session._feed_stderr("ERROR 1064 (42000) at line %d: You have an error near 'FRM t\nmore' at line 2\n"
                                 "ERROR at line %d: DELIMITER must be followed by" % (step.first_line,
                                                                                      step.marker_line))
            self.assertFalse(step.marker)
            session._feed_stderr(" a 'delimiter' character or string\n")
        self.assertTrue(step.marker)
        self.assertEqual(step.errors[0]["message"], "You have an error near 'FRM t\nmore' at line 2")


class ResultBudgetTests(unittest.TestCase):
    """Exercise real streaming parsing and exported snapshots, without a server."""

    def setUp(self):
        self.session = ed.EditorSession("t", "k", {"host": "h", "port": 1, "user": "u"})
        self.session.proc = _Stdin()
        self.session.delimiter = "__d_"
        sql = "SELECT 1; SELECT 2"
        self.units = ed.plan_statements(sql)
        self.run = ed.Run(self.units, ed.MAX_LIMIT, None, sql, ed.Offsets(sql), "all")
        self.session.run = self.run

    def send(self, index=0, kill_ok=False):
        step = self.session._send("user", self.units[index]["send"], unit=index,
                                  limit=ed.MAX_LIMIT, kill_ok=kill_ok)
        self.feed("--------------\n" + step.send + "\n--------------\n\n")
        return step

    def feed(self, text, chunk_size=4096):
        with self.session.lock:
            for start in range(0, len(text), chunk_size):
                self.session._feed_stdout(text[start:start + chunk_size])
        self.assertFalse(self.session.ended, self.session.notes)

    def result(self, rows, names=None, newline="\n", chunk_size=4096):
        def escape(value):
            return value.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
        fields = []
        for row in rows:
            names = names if names is not None else ["c%d" % i for i in range(len(row))]
            fields.append(newline + "  <row>" + newline)
            for name, value in zip(names, row):
                field = '\t<field name="%s"' % escape(name)
                field += ' xsi:nil="true" />' if value is None else ">%s</field>" % escape(value)
                fields.append(field + newline)
            fields.append("  </row>" + newline)
        self.feed(ed.XML_HEAD.replace("\n", newline) + "SELECT 1" + ed.XML_HEAD_END +
                  "".join(fields) + "</resultset>" + newline +
                  ("%d rows in set" % len(rows)) + newline * 2, chunk_size)

    def test_null_and_empty_cells_are_bounded_across_sets_and_statements(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.setUp()
                first = self.send()
                self.result([[value] * 1000] * 60)
                second = self.send(1)
                self.result([[value] * 1000] * 60)
                self.result([[value] * 1000] * 60)
                self.assertEqual(len(rows_of(first)["rows"]), 60)
                limited = rows_of(second)
                self.assertEqual(len(limited["rows"]), 40)
                self.assertEqual(len(second.entries), 1)
                self.assertEqual(self.run.cells, ed.RESULT_CELL_CAP)
                self.assertGreater(self.run.retained, self.run.cells * 64)
                self.assertLessEqual(self.run.retained, ed.RESULT_MEMORY_CAP)
                self.assertTrue(limited["truncated"])
                self.assertEqual(limited["stopped"], "size_limit_read")
                self.assertEqual(limited["budget_reason"], "cell_limit")
                snap = self.session._snapshot(self.run)
                self.assertIn("cell limit", snap["results"][1]["label"])
                self.assert_exportable(snap)

    def assert_exportable(self, snapshot):
        from myflames import ui
        for result in snapshot["results"]:
            if result["type"] != "rows":
                continue
            for ascii_only in (True, False):
                body = json.dumps({"format": "json", "result": result}, ensure_ascii=ascii_only).encode("utf-8")
                self.assertLess(len(body), ui.MAX_BODY)
            ed.export_result("json", result)

    def test_serialized_budget_counts_json_escaping_unicode_and_column_names(self):
        for value in ("\x01" * 120000, "\\\"" * 120000, "\U0001f600" * 60000):
            with self.subTest(kind=repr(value[:2])):
                self.setUp()
                step = self.send()
                self.result([[value]] * 4, names=["\x01\U0001f600" * 1000])
                self.result([[value]] * 12, names=["\x01\U0001f600" * 1000])
                self.assertFalse(rows_of(step)["truncated"])
                entry = rows_of(step, 1)
                self.assertTrue(entry["truncated"])
                self.assertEqual(entry["stopped"], "size_limit_read")
                self.assertEqual(entry["budget_reason"], "byte_limit")
                self.assertLessEqual(self.run.serialized, ed.RESULT_BYTE_CAP)
                self.assertLess(len(entry["rows"]), 12)
                self.assert_exportable(self.session._snapshot(self.run))

    def test_column_metadata_alone_can_exhaust_serialized_budget(self):
        step = self.send()
        self.result([[""]], names=["\x01\U0001f600" * 400000])
        entry = rows_of(step)
        self.assertEqual(entry["rows"], [])
        self.assertEqual(entry["columns"], [])
        self.assertEqual(entry["budget_reason"], "byte_limit")
        self.assert_exportable(self.session._snapshot(self.run))

    def test_container_overhead_can_exhaust_memory_before_text_bytes(self):
        step = self.send(kill_ok=True)
        with mock.patch.object(ed, "RESULT_MEMORY_CAP", 3000):
            self.result([[None, ""]] * 20)
        entry = rows_of(step)
        self.assertTrue(entry["truncated"])
        self.assertEqual(entry["stopped"], "size_limit")
        self.assertEqual(entry["budget_reason"], "memory_limit")
        self.assertTrue(step.kill_requested)
        self.assertGreater(len(entry["rows"]), 0)
        self.assertLess(self.run.retained, 3000)

    def test_result_set_budget_includes_empty_sets_and_ok_packets(self):
        for kind in ("empty", "ok", "rows"):
            with self.subTest(kind=kind):
                self.setUp()
                step = self.send()
                for _ in range(ed.RESULT_SET_CAP + 10):
                    if kind == "ok":
                        self.feed("Query OK, 0 rows affected\n\n")
                    else:
                        self.result([] if kind == "empty" else [[None]])
                self.assertEqual(len(step.entries), ed.RESULT_SET_CAP)
                notice = step.entries[-1]
                self.assertTrue(notice["truncated"])
                self.assertEqual(notice["stopped"], "size_limit_read")
                self.assertEqual(notice["budget_reason"], "result_limit")
                self.assertIn("result-set limit", self.session._snapshot(self.run)["results"][-1]["label"])
                # A budget does not poison the state helper, even in this run.
                helper = self.session._send("helper", "SELECT 2", results=1)
                self.feed("--------------\nSELECT 2\n--------------\n\n")
                self.result([["2"]])
                self.assertTrue(helper.closed)
                self.assertEqual(rows_of(helper)["rows"], [["2"]])

    def test_crlf_framing_preserves_raw_cr_lf_in_values_and_names(self):
        self.send()
        value = "a\rb\r\nc\n\r<>&\""
        name = "column\r\nname"
        self.result([[value, None, ""]], names=[name, "n", "e"], newline="\r\n", chunk_size=1)
        entry = rows_of(self.run.steps[0])
        self.assertEqual(entry["rows"], [[value, None, ""]])
        self.assertEqual(entry["columns"][0]["name"], name)

    def test_oversized_complete_row_is_dropped_even_in_one_chunk(self):
        step = self.send()
        with mock.patch.object(ed, "ROW_CHAR_CAP", 100):
            self.result([["a" * 150]], chunk_size=4096)
        entry = rows_of(step)
        self.assertEqual(entry["rows"], [])
        self.assertEqual(entry["columns"], [])
        self.assertTrue(entry["truncated"])
        self.assertEqual(entry["stopped"], "size_limit_read")


class PlanTests(unittest.TestCase):
    def test_splitting_and_what_is_sent(self):
        units = ed.plan_statements("-- note\nSELECT 1;  \n/* keep */ SELECT 2 -- tail\n;\n"
                                   "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END//\n"
                                   "DELIMITER ;\nCALL p()")
        self.assertEqual([u["send"] for u in units],
                         ["SELECT 1", "/* keep */ SELECT 2 -- tail",
                          "CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END", "CALL p()"])
        self.assertEqual(units[0]["lead"], len("-- note\n"))
        self.assertEqual(units[0]["text"], "-- note\nSELECT 1")

    def test_refusals(self):
        cases = {
            "SELECT 'unclosed": "invalid_statement",
            "SELECT 1 \\! ls": "invalid_statement",
            "/* c */ delimiter $$": "invalid_statement",
            "SELECT $tag$ x $tag$": "invalid_statement",
            "SELECT 'a\x00b'": "invalid_statement",
            "SELECT /*+ \\C latin1 */ 1": "invalid_statement",
            "SELECT /*+ \\d */ 1": "invalid_statement",
            "SET sql_mode = 'NO_BACKSLASH_ESCAPES';\nSELECT 'a\\'b'": "invalid_statement",
            "   ": "empty",
            "-- only a comment": "empty",
        }
        for sql, code in cases.items():
            with self.subTest(sql=sql):
                with self.assertRaises(ed.EditorError) as caught:
                    ed.plan_statements(sql)
                self.assertEqual(caught.exception.code, code)

    def test_delimiter_command_prefix_cannot_reach_the_client(self):
        # The shared splitter recognizes the client's nine-character command
        # prefix on a fresh line. The editor must also reject it after ';'.
        for command in ("delimiterfoo", "DELIMITER_", "delimiter//"):
            with self.subTest(command=command):
                with self.assertRaises(ed.EditorError) as caught:
                    ed.plan_statements("SELECT 0; " + command + " //\nSELECT 1//")
                self.assertEqual(caught.exception.code, "invalid_statement")

    def test_custom_delimiter_cannot_hide_a_mode_change_in_one_unit(self):
        for change in ("SET sql_mode = 'NO_BACKSLASH_ESCAPES'", "CALL change_mode()",
                       "SET `sql_mode` = 'NO_BACKSLASH_ESCAPES'"):
            with self.subTest(change=change):
                # Both token readings are closed, but disagree about whether
                # the DELETE is SQL or part of the quoted value.
                sql = "DELIMITER //\nSELECT 0; " + change + "; SELECT 'a\\'; DELETE FROM t; -- '//"
                with self.assertRaises(ed.EditorError) as caught:
                    ed.plan_statements(sql)
                self.assertEqual(caught.exception.code, "invalid_statement")
        # A single statement is parsed entirely with the known current mode;
        # reading sql_mode beside an escaped quote must remain valid.
        sql = "SELECT @@sql_mode, 'a\\'b'"
        self.assertEqual(ed.plan_statements(sql)[0]["send"], sql)

    def test_backslash_reading(self):
        # Under NO_BACKSLASH_ESCAPES 'a\' is a whole string.
        self.assertEqual([u["send"] for u in ed.plan_statements("SELECT 'a\\'; SELECT 2", False)],
                         ["SELECT 'a\\'", "SELECT 2"])
        with self.assertRaises(ed.EditorError):
            ed.plan_statements("SELECT 'a\\'; SELECT 2", True)

    def test_offsets_are_utf16_for_the_ui(self):
        text = "SELECT '\U0001F600'; SELECT 2"
        offsets = ed.Offsets(text)
        second = text.index("SELECT 2")
        self.assertEqual(offsets.to_js(second), second + 1)
        self.assertEqual(offsets.to_py(second + 1), second)
        self.assertEqual(offsets.to_py(9), 8)       # inside the surrogate pair: the character itself
        units = ed.plan_statements(text)
        self.assertEqual(offsets.to_js(units[1]["start"]), second + 1)

    def test_error_position(self):
        sql = "SELECT 1;\nSELECT x FROM\n  t WHERE"
        units = ed.plan_statements(sql)
        offsets = ed.Offsets(sql)
        pos = ed.error_position("... near '' at line 2", units[1], offsets)
        self.assertEqual((pos["offset"], pos["line"], pos["exact"]), (len(sql), 3, True))
        pos = ed.error_position("... near 'WHERE' at line 2", units[1], offsets)
        self.assertEqual((pos["offset"], pos["line"], pos["column"]), (sql.index("WHERE"), 3, 5))
        pos = ed.error_position("Table 't' doesn't exist", units[1], offsets)
        self.assertEqual((pos["offset"], pos["exact"]), (units[1]["start"], False))


class ModeChangeGuardTests(unittest.TestCase):
    def test_backtick_quoted_sql_mode_is_seen_as_a_mode_change(self):
        # Found live: a backtick-quoted `sql_mode` was masked in the guard's
        # code, so a later statement whose backslash quote reads differently
        # under NO_BACKSLASH_ESCAPES ran without the split/confirm guard.
        for first in ("SET SESSION `sql_mode` = 'NO_BACKSLASH_ESCAPES'",
                      "SET @@SESSION.`sql_mode` = 'NO_BACKSLASH_ESCAPES'",
                      "SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'"):
            with self.subTest(first=first):
                with self.assertRaises(ed.EditorError) as caught:
                    ed.plan_statements(first + ";\nSELECT 'x\\'; DROP TABLE d.t; -- ';\n")
                self.assertEqual(caught.exception.code, "invalid_statement")


class ExportTests(unittest.TestCase):
    COLUMNS = [{"name": "id"}, {"name": "v"}, {"name": "b", "hex": True, "binary": True}]
    ROWS = [["1", None, "0x00FF"], ["2", "", "0x"], ["3", "NULL", None], ["4", 'a,"b"\r\nc\td \U0001F600', "0x41"],
            ["9223372036854775807", "=SUM(A1)", None], ["6", "back\\slash 'q'", None], ["7", "x|y\nz <b>*", None]]

    def export(self, fmt, **kw):
        return ed.export_result(fmt, {"columns": self.COLUMNS, "rows": self.ROWS}, **kw)["content"]

    def test_csv(self):
        text = self.export("csv")
        lines = text.split("\r\n")
        self.assertEqual(lines[0], "id,v,b")
        self.assertEqual(lines[1], "1,,0x00FF")          # NULL: empty, unquoted
        self.assertEqual(lines[2], '2,"",0x')            # empty string: ""
        self.assertEqual(lines[3], "3,NULL,")            # the string 'NULL'
        self.assertIn('4,"a,""b""\r\nc\td \U0001F600",0x41', text)
        self.assertTrue(text.endswith("\r\n"))
        guarded = self.export("csv", options={"formula_guard": True, "bom": True})
        self.assertTrue(guarded.startswith("﻿"))
        self.assertIn("'=SUM(A1)", guarded)
        import csv as csvmod
        parsed = list(csvmod.reader(io.StringIO(text, newline="")))
        self.assertEqual(parsed[4][1], 'a,"b"\r\nc\td \U0001F600')

    def test_tsv(self):
        text = self.export("tsv")
        self.assertIn('4\t"a,""b""\r\nc\td \U0001F600"\t0x41', text)

    def test_json(self):
        body = json.loads(self.export("json"))
        self.assertEqual(body["rows"][0], ["1", None, "0x00FF"])
        self.assertEqual(body["rows"][4][0], "9223372036854775807")
        self.assertEqual(body["columns"][2]["hex"], True)
        dup = ed.export_result("json", {"columns": ["a", "a"], "rows": [["1", "2"]]}, options={"objects": True})
        self.assertEqual(json.loads(dup["content"])["rows"], [{"a": "1", "a (2)": "2"}])

    def test_sql(self):
        text = ed.export_result("sql", {"columns": [{"name": "i`d", "type": "bigint"}, {"name": "v"},
                                                    {"name": "b", "hex": True, "binary": True}], "rows": self.ROWS},
                                table="t`x", schema="s", options={"rows_per_insert": 4})["content"]
        self.assertTrue(text.startswith("INSERT INTO `s`.`t``x` (`i``d`, `v`, `b`) VALUES\n  (1, NULL, X'00FF'),\n"))
        self.assertIn("(2, '', X'')", text)
        self.assertIn("(3, 'NULL', NULL)", text)
        self.assertIn("(4, _utf8mb4 X'%s', X'41')" % 'a,"b"\r\nc\td \U0001F600'.encode("utf-8").hex().upper(), text)
        self.assertIn("(9223372036854775807, '=SUM(A1)', NULL)", text)
        self.assertIn("_utf8mb4 X'%s'" % "back\\slash 'q'".encode("utf-8").hex().upper(), text)
        self.assertEqual(text.count("INSERT INTO"), 2)
        # Without a numeric type, numbers are quoted (safe for any column).
        plain = ed.export_result("sql", {"columns": ["n"], "rows": [["5"]]}, table="t")["content"]
        self.assertIn("('5')", plain)
        with self.assertRaises(ValueError):
            ed.export_result("sql", {"columns": ["n"], "rows": [["5"]]})
        with self.assertRaises(ValueError):
            ed.export_result("sql", {"columns": ["n"], "rows": [["5"]]}, table="t", engine="oracle")

    def test_gate_11_hex_is_only_a_guess(self):
        """A text column holding '0xFF00FF' looks like binary; SQL export writes
        X'..' only for columns the caller marks binary."""
        result = {"columns": [{"name": "v", "hex": True}], "rows": [["0xFF00FF"], ["0x"]]}
        text = ed.export_result("sql", result, table="t")["content"]
        self.assertIn("('0xFF00FF')", text)
        self.assertIn("('0x')", text)
        result["columns"][0]["binary"] = True
        self.assertIn("(X'FF00FF')", ed.export_result("sql", result, table="t")["content"])

    def test_gate_12_results_fit_an_export_request(self):
        from myflames import ui
        self.assertLess(ed.RESULT_BYTE_CAP + 1024 * 1024, ui.MAX_BODY)
        self.assertLess(ed.ROW_CHAR_CAP, ed.RESULT_BYTE_CAP)

    def test_markdown(self):
        text = self.export("markdown")
        lines = text.splitlines()
        self.assertEqual(lines[0], "| id | v | b |")
        self.assertEqual(lines[2], "| 1 | *NULL* | 0x00FF |")
        self.assertEqual(lines[3], "| 2 |  | 0x |")
        self.assertIn("x\\|y<br>z &lt;b&gt;\\*", text)
        self.assertIn("back\\\\slash", text)

    def test_no_connection_details(self):
        for fmt in ed.EXPORT_FORMATS:
            out = ed.export_result(fmt, {"columns": ["a"], "rows": [["1"]]}, table="t")
            self.assertNotIn("password", json.dumps(out).lower())

    def test_bad_input(self):
        for bad in ({"columns": ["a"], "rows": [["1", "2"]]}, {"columns": [{"x": 1}], "rows": []},
                    {"columns": ["a"], "rows": [[{"x": 1}]]}, None):
            with self.assertRaises(ValueError):
                ed.export_result("csv", bad)
        with self.assertRaises(ValueError):
            ed.export_result("xml", {"columns": [], "rows": []})


@unittest.skipIf(os.name == "nt", "the fake client is a POSIX script")
class ProcessTests(unittest.TestCase):
    """The real session code against the fake client."""

    engine = "mysql"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        bindir = root / "bin"
        bindir.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        script = bindir / "mysql"
        # Add identity fields to this test's client without changing the shared fixture.
        client = FAKE.read_text(encoding="utf-8").replace(
            '"connection_id": str(self.conn_id),',
            '"connection_id": str(self.conn_id), "hostname": "fake-db", "port": "3306", "server_id": "1",')
        client = client.replace('arg.startswith("--defaults-extra-file=")',
                                'arg.startswith(("--defaults-file=", "--defaults-extra-file="))')
        script.write_text("#!%s\n%s" % (sys.executable, client), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        env = {"HOME": self.temp.name, "USERPROFILE": self.temp.name, "MYFLAMES_SECRET_BACKEND": "vault",
               "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""), "FAKE_STATE": str(self.state),
               "FAKE_ENGINE": self.engine}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("MYFLAMES_VAULT_PASSPHRASE", None)
        os.environ.pop("FAKE_PS", None)
        os.environ.pop("FAKE_DENY", None)
        # Test controls are injected at the process boundary, never allowlisted
        # in production. The mock retains the original env for assertions.
        popen = ed.subprocess.Popen

        def spawn(*args, **kwargs):
            env = dict(kwargs.get("env") or os.environ)
            env.update({k: v for k, v in os.environ.items() if k.startswith("FAKE_")})
            return popen(*args, **dict(kwargs, env=env))
        process_patch = mock.patch.object(ed.subprocess, "Popen", side_effect=spawn)
        self.processes = process_patch.start()
        self.addCleanup(process_patch.stop)
        ed._FEATURES.clear()
        self.store = WorkspaceStore()
        self.registry = ed.EditorSessions(reap_every=0)
        self.addCleanup(self.registry.close)
        self.workspace = types.SimpleNamespace(store=self.store, editor=self.registry)
        self.connection = {"host": "127.0.0.1", "port": 3306, "user": "app", "password": "s3cret-pw"}

    def call(self, path, tab="tab1", **payload):
        payload.setdefault("tab_id", tab)
        if path not in ("/api/editor/export", "/api/editor/history"):
            payload.setdefault("connection", dict(self.connection))
        payload.setdefault("wait", 15)
        return ed.api_response(path, payload, self.workspace)

    def run_sql(self, sql, tab="tab1", confirm=True, **payload):
        result = self.call("/api/editor/run", tab=tab, sql=sql, **payload)
        if confirm and result["status"] == "needs_confirmation":
            result = self.call("/api/editor/run", tab=tab, sql=sql, confirmed=True,
                               confirm_token=result["confirmation"]["token"], **payload)
        return result

    def rows(self, result, index=0):
        return [r for r in result["run"]["results"] if r["type"] == "rows"][index]["rows"]

    def session_log(self):
        session = self.registry.get("tab1")
        return (self.state / ("session-%d.log" % session.connection_id)).read_text(encoding="utf-8").splitlines()

    # -- helpers for barrier tests --------------------------------------------------------
    def until(self, predicate, what, timeout=10.0):
        """Wait for a condition (not a fixed time); fail with *what* if it never holds."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("timed out waiting for " + what)

    def logged(self, tab, text):
        session = self.registry.get(tab)
        path = self.state / ("session-%d.log" % session.connection_id)
        return path.exists() and any(text in line for line in path.read_text(encoding="utf-8").splitlines())

    def release(self, tab="tab1"):
        session = self.registry.get(tab)
        (self.state / ("release-%d" % session.connection_id)).write_text("1")

    def dev(self):
        return self.save_profile(type="development")

    # -- review gate regressions ------------------------------------------------------------
    def test_preview_stays_unknown_after_set_role_and_later_run(self):
        from myflames import privileges
        ticket = ed.begin_privileges_read(self.workspace, {'connection': self.connection})
        model = privileges.parse_grants([])
        model['schema_version'] = 'privileges-1.0'
        ed.finish_privileges_read(self.workspace, ticket, model)
        sql = 'SET GLOBAL max_connections=123'
        self.assertEqual(self.call('/api/editor/preview', sql=sql)['aggregate']['state'], 'denied')
        self.assertEqual(self.run_sql('SET ROLE NONE')['status'], 'complete')
        self.assertEqual(self.run_sql('SELECT 1')['status'], 'complete')
        self.assertTrue(self.registry.get('tab1').privileges_changed)
        reply = self.call('/api/editor/preview', sql=sql)
        self.assertEqual(reply['aggregate']['state'], 'unknown')
        self.assertIn('SET ROLE', reply['aggregate']['reason'])

    def test_gate_1_racing_requests_cannot_share_a_session(self):
        """A run for tab X waits in the password fill while another request opens
        tab X on a different connection: the run must not reach that session."""
        other = dict(self.connection, port=3307)

        def fill(payload):
            ed.api_response("/api/editor/open", {"tab_id": "tab1", "connection": other}, self.workspace)
            return payload
        with self.assertRaises(ed.EditorError) as caught:
            ed.api_response("/api/editor/run", {"tab_id": "tab1", "connection": dict(self.connection),
                                                "sql": "SELECT 5", "wait": 5}, self.workspace, fill)
        self.assertEqual(caught.exception.code, "identity_mismatch")
        session = self.registry.get("tab1")
        self.assertEqual(session.port, 3307)
        self.assertFalse(self.logged("tab1", "SELECT 5"))

    def test_gate_2_row_limit_never_kills_a_call(self):
        result = self.run_sql("CALL rows_then_write()", limit=100, wait=0)
        self.assertEqual(result["status"], "running")
        session = self.registry.get("tab1")
        step = session.run.current
        self.until(lambda: step.entries and step.entries[0]["truncated"], "the 101st row")
        self.assertFalse(step.kill_requested)
        self.assertEqual(session.run.kill_threads, [])
        self.release()
        final = self.call("/api/editor/status", wait=10)
        self.assertEqual(final["run"]["state"], "complete")
        rows = final["run"]["results"][0]
        self.assertEqual((rows["row_count"], rows["stopped"]), (100, "row_limit_read"))
        self.assertIn("ran to its end", rows["label"])
        self.assertFalse((self.state / "kills.log").exists())
        self.assertEqual(self.rows(self.run_sql("SELECT * FROM written")), [["1", "by the procedure"]])
        self.assertEqual(self.store.audit_records()[0]["status"], "ok")

    def test_gate_3_read_only_cannot_lift_the_guard_for_one_statement(self):
        self.save_profile(read_only=True)
        for sql in ("SET STATEMENT `transaction_read_only`=0 FOR SELECT 1",
                    "SET STATEMENT `tx_read_only`=0 FOR SELECT 1",
                    "SET STATEMENT transaction_read_only=0 FOR SELECT 1"):
            with self.subTest(sql=sql):
                self.assertEqual(self.call("/api/editor/run", sql=sql)["status"], "refused")
        self.assertIsNone(self.registry.get("tab1"))

    def test_gate_4_stop_after_the_statement_finished_is_not_a_stop(self):
        self.dev()
        self.run_sql("CREATE TEMPORARY TABLE tt (id INT, v TEXT)")
        started = self.run_sql("INSERT INTO tt VALUES (5, 'e') /*gate*/", wait=0)
        self.assertEqual(started["status"], "running")
        session = self.registry.get("tab1")
        self.until(lambda: self.logged("tab1", "INSERT INTO tt VALUES (5, 'e')"), "the INSERT to start")
        step = session.run.current
        original = session._kill_query

        def late(options, target):
            # The KILL arrives after the statement ended (slow network).
            self.release()
            self.until(lambda: target.marker, "the INSERT to finish")
            return original(options, target)
        session._kill_query = late
        stop = self.call("/api/editor/stop")
        self.assertFalse(stop["stopped"])
        self.assertIn("already finished", stop["message"])
        final = self.call("/api/editor/status", wait=10)
        self.assertEqual(final["run"]["statements"][0]["status"], "ok")
        self.assertEqual(final["run"]["state"], "complete")
        self.assertIsNone(final["run"]["stopped"])
        record = [r for r in self.store.audit_records() if r["id"] == final["run"]["audit_id"]][0]
        self.assertEqual((record["status"], record["outcome"]), ("ok", "completed"))
        self.assertEqual(step.stop_sent, False)

    def test_gate_5_client_dies_mid_write(self):
        import signal
        self.dev()
        self.run_sql("CREATE TEMPORARY TABLE tt (id INT, v TEXT)")
        self.run_sql("INSERT INTO tt VALUES (6, 'f') /*gate:nokill*/", wait=0)
        session = self.registry.get("tab1")
        self.until(lambda: self.logged("tab1", "INSERT INTO tt VALUES (6, 'f')"), "the INSERT to start")
        os.kill(session.proc.pid, signal.SIGKILL)
        final = self.call("/api/editor/status", wait=10)
        self.assertEqual(final["run"]["state"], "session_ended")
        self.assertEqual(final["run"]["statements"][0]["status"], "session_ended")
        record = [r for r in self.store.audit_records() if r["id"] == final["run"]["audit_id"]][0]
        self.assertEqual((record["status"], record["outcome"]), ("unknown", "session_ended"))

    def test_gate_6_table_data_in_an_error_cannot_end_the_session(self):
        self.run_sql("START TRANSACTION")
        result = self.run_sql("SELECT spoof_error();\nSELECT 2")
        self.assertEqual(result["status"], "error")
        error = result["run"]["error"]
        self.assertEqual(error["code"], 1292)
        self.assertIn("ERROR 2013", error["message"])
        self.assertFalse(result["session"]["ended"])
        self.assertIs(result["session"]["in_transaction"], True)
        self.assertEqual(self.rows(self.run_sql("SELECT 3")), [["3"]])

    def test_gate_7_audit_failure_sends_nothing_and_frees_the_tab(self):
        self.run_sql("SELECT 1")

        def broken(row):
            raise RuntimeError("database is locked")
        with mock.patch.object(self.store, "audit_insert", broken):
            with self.assertRaises(ed.EditorError) as caught:
                self.run_sql("CREATE TEMPORARY TABLE tt (id INT)")
        self.assertEqual(caught.exception.code, "audit_failed")
        self.assertFalse(self.logged("tab1", "CREATE TEMPORARY TABLE tt"))
        self.assertEqual(self.rows(self.run_sql("SELECT 2")), [["2"]])

    def test_gate_8_turning_safe_updates_off_follows_the_policy(self):
        asked = self.call("/api/editor/open", safe_updates=False)
        self.assertEqual(asked["status"], "needs_confirmation")
        self.assertEqual(asked["confirmation"]["statements"], [ed.SAFE_UPDATES_OFF])
        self.assertIsNone(self.registry.get("tab1"))
        done = self.call("/api/editor/open", safe_updates=False, confirmed=True,
                         confirm_token=asked["confirmation"]["token"])
        self.assertIs(done["session"]["safe_updates"], False)
        record = self.store.audit_records()[0]
        self.assertEqual((record["operation"], record["status"], record["statements"]),
                         ("editor_safe_updates", "ok", [ed.SAFE_UPDATES_OFF]))
        with self.assertRaises(ed.EditorError):
            self.call("/api/editor/run", sql="SELECT 1", safe_updates=False)
        # Turning it back on needs nothing.
        self.assertIs(self.call("/api/editor/open", safe_updates=True)["session"]["safe_updates"], True)

    def test_gate_9_implicit_commit_and_unknown_state(self):
        self.dev()
        self.run_sql("START TRANSACTION")
        asked = self.call("/api/editor/run", sql="SET autocommit = 1")
        self.assertEqual(asked["status"], "needs_confirmation")
        self.assertIs(asked["confirmation"]["in_transaction"], True)
        self.assertIn("commits the open transaction", asked["confirmation"]["warnings"][0])
        ddl = self.call("/api/editor/run", sql="DROP TABLE x")
        self.assertTrue(ddl["confirmation"]["warnings"])
        temporary = self.call("/api/editor/run", sql="DROP TEMPORARY TABLE x")
        self.assertEqual(temporary["status"], "needs_confirmation")  # DDL needs its own review, including temporary tables
        self.assertEqual(temporary["confirmation"]["warnings"], [])  # still no implicit commit
        self.call("/api/editor/rollback")
        self.assertEqual(self.call("/api/editor/run", sql="SET autocommit = 1")["status"], "complete")

    def test_gate_9_close_asks_when_the_state_is_unknown(self):
        if self.engine != "mysql":
            self.skipTest("MariaDB always reports @@in_transaction")
        os.environ["FAKE_PS"] = "off"
        try:
            self.run_sql("SELECT 1")
        finally:
            os.environ.pop("FAKE_PS", None)
        result = self.call("/api/editor/close")
        self.assertEqual((result["closed"], result["reason"]), (False, "transaction_unknown"))
        self.assertTrue(self.call("/api/editor/close", discard=True)["closed"])

    def test_review_new_session_regates_implicit_commit(self):
        self.dev()
        open_session = ed._open_session
        for state in (None, True):
            for sql in ("START TRANSACTION", "SET autocommit = 1"):
                with self.subTest(state=state, sql=sql):
                    tab = "%s-%s" % (state, sql.split()[0])

                    def opened(*args, **kwargs):
                        session = open_session(*args, **kwargs)
                        # Unknown tracking, or a concurrently opened session
                        # whose transaction is already active.
                        session.in_transaction = state
                        return session

                    with mock.patch.object(ed, "_open_session", side_effect=opened):
                        asked = self.call("/api/editor/run", tab=tab, sql=sql)
                    self.assertEqual(asked["status"], "needs_confirmation")
                    self.assertIs(asked["confirmation"]["in_transaction"], state)
                    self.assertTrue(asked["confirmation"]["warnings"])
                    self.assertFalse(self.logged(tab, sql))
                    done = self.call("/api/editor/run", tab=tab, sql=sql, confirmed=True,
                                     confirm_token=asked["confirmation"]["token"])
                    self.assertEqual(done["status"], "complete")
                    self.assertTrue(self.logged(tab, sql))

    def test_review_new_session_regates_changed_warnings(self):
        sql = "CREATE TABLE reviewed (id INT)"
        asked = self.call("/api/editor/run", sql=sql)
        self.assertEqual(asked["confirmation"]["warnings"], [])
        open_session = ed._open_session

        def opened(*args, **kwargs):
            session = open_session(*args, **kwargs)
            session.in_transaction = None
            return session

        with mock.patch.object(ed, "_open_session", side_effect=opened):
            again = self.call("/api/editor/run", sql=sql, confirmed=True,
                              confirm_token=asked["confirmation"]["token"])
        self.assertEqual(again["status"], "needs_confirmation")
        self.assertTrue(again["confirmation"]["warnings"])
        self.assertNotEqual(asked["confirmation"]["token"], again["confirmation"]["token"])
        self.assertFalse(self.logged("tab1", sql))

    def test_gate_12_running_status_sends_no_rows(self):
        self.run_sql("SELECT 1;\nSELECT 2 /*gate*/", wait=0)
        session = self.registry.get("tab1")
        self.until(lambda: self.logged("tab1", "SELECT 2"), "the second statement")
        status = self.call("/api/editor/status")
        self.assertEqual(status["run"]["state"], "running")
        first = status["run"]["results"][0]
        self.assertEqual((first["rows"], first["row_count"]), ([], 1))
        self.release()
        final = self.call("/api/editor/status", wait=10)
        self.assertEqual(final["run"]["results"][0]["rows"], [["1"]])

    def test_gate_13_a_statement_that_ignores_kill_ends_the_session(self):
        with mock.patch.object(ed, "STOP_GRACE", 0.3):
            self.run_sql("SELECT 3 /*gate:nokill*/", wait=0)
            self.until(lambda: self.logged("tab1", "SELECT 3"), "the statement to start")
            self.assertTrue(self.call("/api/editor/stop")["stopped"])
            final = self.call("/api/editor/status", wait=5)
        self.assertEqual(final["run"]["state"], "session_ended")
        self.assertEqual(final["session"]["ended_reason"], "unresponsive")

    def test_gate_14_confirmation_tokens_are_used_once_and_expire(self):
        sql = "CREATE TEMPORARY TABLE tt (id INT)"
        asked = self.call("/api/editor/run", sql=sql)
        token = asked["confirmation"]["token"]
        self.assertEqual(asked["confirmation"]["expires_in"], int(ed.TOKEN_TTL))
        self.assertEqual(self.call("/api/editor/run", sql=sql, confirmed=True, confirm_token=token)["status"],
                         "complete")
        again = self.call("/api/editor/run", sql=sql, confirmed=True, confirm_token=token)
        self.assertEqual(again["status"], "needs_confirmation")
        with mock.patch.object(ed, "TOKEN_TTL", -1):
            stale = self.call("/api/editor/run", sql=sql)["confirmation"]["token"]
        self.assertEqual(self.call("/api/editor/run", sql=sql, confirmed=True, confirm_token=stale)["status"],
                         "needs_confirmation")

    def test_session_affinity(self):
        first = self.run_sql("SET @x = 1")
        self.assertEqual(first["status"], "complete")
        self.assertEqual(self.rows(self.run_sql("SELECT @x AS x")), [["1"]])
        self.run_sql("CREATE TEMPORARY TABLE tt (id INT, v TEXT)")
        self.run_sql("INSERT INTO tt VALUES (1, 'a'), (2, NULL)")
        self.assertEqual(self.rows(self.run_sql("SELECT * FROM tt")), [["1", "a"], ["2", None]])
        started = self.run_sql("START TRANSACTION;\nINSERT INTO tt VALUES (3, 'c')")
        self.assertIs(started["session"]["in_transaction"], True)
        self.assertEqual(self.rows(self.run_sql("SELECT * FROM tt"))[-1], ["3", "c"])
        self.assertIs(self.call("/api/editor/status")["session"]["in_transaction"], True)
        committed = self.call("/api/editor/commit", confirmed=False)
        self.assertEqual(committed["status"], "needs_confirmation")       # Production asks before COMMIT
        committed = self.call("/api/editor/commit", confirmed=True,
                              confirm_token=committed["confirmation"]["token"])
        self.assertEqual(committed["status"], "complete")
        self.assertIs(committed["session"]["in_transaction"], False)
        self.run_sql("START TRANSACTION;\nINSERT INTO tt VALUES (4, 'd')")
        rolled = self.call("/api/editor/rollback")
        self.assertEqual(rolled["status"], "complete")
        self.assertEqual(len(self.rows(self.run_sql("SELECT * FROM tt"))), 3)
        # One client process, one connection id throughout.
        argv = (self.state / "argv.log").read_text(encoding="utf-8")
        self.assertEqual(argv.count("--xml"), 1)
        ids = {r["session"]["connection_id"] for r in (first, committed, rolled)}
        self.assertEqual(len(ids), 1)
        # Helpers are tagged; user statements are sent as written.
        log = self.session_log()
        self.assertIn("SET @x = 1", log)
        self.assertTrue(all(line.startswith(("/*myflames:editor*/", "SET @x", "SELECT", "CREATE", "INSERT",
                                             "START")) for line in log), log)

    def test_flags_and_password_handling(self):
        result = self.run_sql("SELECT 1")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.rows(result), [["1"]])
        self.assertEqual(len(result["run"]["results"]), 1)
        self.assertEqual(result["run"]["counts"]["statements"], 1)
        self.assertNotIn("SET NAMES", self.session_log())  # init stays outside the framed stdin protocol
        argv = (self.state / "argv.log").read_text(encoding="utf-8")
        for flag in ("--xml", "--binary-as-hex", "--binary-mode", "--comments", "--force", "--quick", "--unbuffered",
                     "-vv", "--skip-reconnect", "--default-character-set=utf8mb4",
                     "--init-command=SET NAMES utf8mb4"):
            self.assertIn(repr(flag), argv)
        self.assertNotIn("s3cret-pw", argv)
        self.assertIn("defaults-exists=True", argv)
        child_argv = next(call[0][0] for call in self.processes.call_args_list if "--xml" in call[0][0])
        self.assertTrue(child_argv[1].startswith("--defaults-file="), child_argv)
        self.assertNotIn("--defaults-extra-file=", argv)
        session = self.registry.get("tab1")
        self.assertIsNone(session._conn._defaults_file_path)      # removed once the client connected
        self.assertNotIn("--skip-commands", argv)

    def test_mariadb_version_comment_selects_transaction_helpers_for_masked_version(self):
        script = Path(os.environ['PATH'].split(os.pathsep)[0]) / 'mysql'
        client = script.read_text(encoding='utf-8')
        client = client.replace('ENGINE = os.environ.get("FAKE_ENGINE", "mysql")', 'ENGINE = "mariadb"')
        client = client.replace('"11.4.13-MariaDB-fake" if ENGINE == "mariadb" else "8.4.11-fake"',
                                '"11.4.13", "version_comment": "MariaDB Server"')
        script.write_text(client, encoding='utf-8')
        result = self.run_sql('SELECT 1')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['session']['engine'], 'mariadb')
        log = '\n'.join(self.session_log())
        self.assertIn('@@version_comment AS version_comment', log)
        self.assertIn('@@in_transaction AS in_transaction', log)
        self.assertNotIn('PS_CURRENT_THREAD_ID()', log)

    def test_skip_commands_when_the_client_has_it(self):
        os.environ["FAKE_COMMANDS"] = "1"
        try:
            self.run_sql("SELECT 1")
        finally:
            os.environ.pop("FAKE_COMMANDS", None)
        self.assertIn("'--skip-commands'", (self.state / "argv.log").read_text(encoding="utf-8"))

    def test_values_and_result_sets(self):
        result = self.run_sql("SELECT special;\nSELECT 1/0;\nSELECT empty;\nCALL two()")
        self.assertEqual(result["status"], "complete")
        results = result["run"]["results"]
        first = results[0]
        self.assertEqual(first["rows"][0], [None, "", "NULL", "0x00FF41", "18446744073709551615",
                                            "2024-01-02 03:04:05", 'a<b>&"c"', "line1\nline2", "1", "2"])
        self.assertEqual([c["hex"] for c in first["columns"]][:4], [False, False, False, True])
        self.assertEqual([c["name"] for c in first["columns"]][-2:], ["dup", "dup"])
        self.assertEqual(results[1]["warnings"], 1)
        self.assertEqual((results[2]["columns"], results[2]["columns_known"], results[2]["row_count"]), ([], False, 0))
        self.assertEqual([r["type"] for r in results[3:]], ["rows", "rows", "ok"])
        self.assertEqual([r["statement"] for r in results], [0, 1, 2, 3, 3, 3])
        self.assertEqual(result["run"]["counts"]["statements"], 4)

    def test_error_stops_the_script_and_the_session_survives(self):
        sql = "SELECT 1;\nSELECT * FROM nope;\nSELECT 3"
        result = self.run_sql(sql)
        self.assertEqual(result["status"], "error")
        statuses = [s["status"] for s in result["run"]["statements"]]
        self.assertEqual(statuses, ["ok", "error", "not_run"])
        error = result["run"]["error"]
        self.assertEqual((error["statement"], error["code"], error["sqlstate"]), (1, 1146, "42S02"))
        self.assertEqual(error["position"]["offset"], sql.index("SELECT * FROM nope"))
        self.assertEqual(error["position"]["line"], 2)
        again = self.run_sql("SELECT 4")
        self.assertEqual(self.rows(again), [["4"]])
        self.assertEqual(again["session"]["connection_id"], result["session"]["connection_id"])

    def test_syntax_error_position(self):
        sql = "SELECT 1;\n  SELEC 2 FROM t"
        result = self.run_sql(sql)
        self.assertEqual(result["run"]["error"]["code"], 1064)
        self.assertEqual(result["run"]["error"]["position"]["offset"], sql.index("SELEC 2"))

    def test_selection_runs_only_the_selection(self):
        sql = "SELECT 1;\nSELECT 2;\nSELECT 3"
        start = sql.index("SELECT 2")
        result = self.run_sql(sql, selection={"start": start, "end": start + len("SELECT 2")})
        self.assertEqual(result["run"]["scope"], "selection")
        self.assertEqual([s["start"] for s in result["run"]["statements"]], [start])
        self.assertEqual(self.rows(result), [["2"]])

    def test_row_limit_kills_the_statement(self):
        result = self.run_sql("SELECT n FROM big_table;\nSELECT 5", limit=120)
        self.assertEqual(result["status"], "complete")
        big = result["run"]["results"][0]
        self.assertEqual((big["row_count"], big["truncated"], big["stopped"]), (120, True, "row_limit"))
        self.assertEqual(big["label"], "stopped after 120 rows")
        self.assertLess(big["server_rows"], 200000)
        self.assertEqual(self.rows(result, 1), [["5"]])
        session = self.registry.get("tab1")
        kills = (self.state / "kills.log").read_text(encoding="utf-8")
        self.assertIn("/*myflames:editor*/ KILL QUERY %d" % session.connection_id, kills)
        self.assertNotIn("1317", json.dumps(result["run"]["error"]))
        # Load more: the same statement again with a larger limit.
        more = self.run_sql("SELECT n FROM big_table LIMIT 300", limit=1000)
        self.assertEqual((more["run"]["results"][0]["row_count"], more["run"]["results"][0]["truncated"]),
                         (300, False))

    def test_stop_keeps_the_session(self):
        started = self.run_sql("SELECT SLEEP(20);\nSELECT 2", wait=0)
        self.assertEqual(started["status"], "running")
        deadline = time.time() + 10
        while time.time() < deadline:
            session = self.registry.get("tab1")
            if session.run and session.run.current and session.run.current.kind == "user":
                break
            time.sleep(0.02)
        stopped = self.call("/api/editor/stop")
        self.assertTrue(stopped["stopped"], stopped)
        self.assertIn("KILL QUERY", stopped["message"])
        final = self.call("/api/editor/status", wait=10)
        self.assertEqual(final["run"]["state"], "stopped")
        # KILL QUERY makes SLEEP() return 1 without an error: the statement ended
        # normally, the next one was not run.
        self.assertEqual([s["status"] for s in final["run"]["statements"]], ["ok", "not_run"])
        self.assertEqual(self.rows(final), [["1"]])
        self.assertEqual(final["run"]["stopped"]["by"], "user")
        self.assertEqual(self.rows(self.run_sql("SELECT 7")), [["7"]])
        self.assertFalse(final["session"]["ended"])

    def test_stop_control_connection_scrubs_environment_and_errors(self):
        self.run_sql("SELECT 1 /*gate*/", wait=0)
        self.until(lambda: self.logged("tab1", "SELECT 1 /*gate*/"), "the statement to start")
        session = self.registry.get("tab1")
        # A connector failure may quote a credential; neither the Stop reply
        # nor the notes returned by status may retain it.
        with mock.patch.object(ed.MySQLConnection, "run", side_effect=ed.ConnectorError("denied s3cret-pw")):
            failed = self.call("/api/editor/stop")
        self.assertFalse(failed["stopped"])
        self.assertIn("[redacted]", failed["message"])
        self.assertNotIn("s3cret-pw", json.dumps(failed))
        self.assertNotIn("s3cret-pw", json.dumps(session.status()))
        secrets = {"MYSQL_PWD": "env-secret", "MYSQL_HOST": "wrong-server",
                   "MYSQL_TEST_LOGIN_FILE": "/private/login-file", "ARBITRARY_API_TOKEN": "api-secret"}
        self.processes.reset_mock()
        with mock.patch.dict(os.environ, secrets):
            self.assertTrue(self.call("/api/editor/stop")["stopped"])
        self.assertTrue(session.run.done.wait(5))
        controls = [call for call in self.processes.call_args_list
                    if any("KILL QUERY" in str(arg) for arg in call[0][0])]
        self.assertEqual(len(controls), 1)
        argv = controls[0][0][0]
        env = controls[0][1]["env"]
        self.assertNotIn("s3cret-pw", " ".join(argv))
        self.assertIn("--binary-mode", argv)
        self.assertIn("--comments", argv)
        self.assertEqual(env["USERPROFILE"], env["HOME"])
        self.assertNotEqual(env["HOME"], self.temp.name)
        self.assertFalse(os.path.exists(env["HOME"]))
        for name, secret in secrets.items():
            self.assertNotEqual(env.get(name), secret, name)

    def test_session_ending_mid_write_is_audited_as_unknown(self):
        sql = "CREATE TEMPORARY TABLE tt (id INT); SELECT SLEEP(20)"
        started = self.call("/api/editor/run", sql=sql)
        self.call("/api/editor/run", sql=sql, confirmed=True, confirm_token=started["confirmation"]["token"], wait=0)
        deadline = time.time() + 10
        while time.time() < deadline:
            run = self.registry.get("tab1").run
            if run and run.current and run.current.send.startswith("SELECT SLEEP"):
                break
            time.sleep(0.02)
        self.call("/api/editor/stop", force=True)
        final = self.call("/api/editor/status", wait=10)
        self.assertEqual(final["run"]["state"], "session_ended")
        record = self.store.audit_records()[0]
        self.assertEqual((record["status"], record["outcome"]), ("unknown", "session_ended"))

    def test_stop_interrupting_a_statement(self):
        self.dev()
        self.run_sql("CREATE TEMPORARY TABLE tt (id INT, v TEXT)")
        self.run_sql("INSERT INTO tt VALUES (7, 'g') /*gate*/;\nSELECT 2", wait=0)
        self.until(lambda: self.logged("tab1", "INSERT INTO tt VALUES (7, 'g')"), "the INSERT to start")
        self.assertTrue(self.call("/api/editor/stop")["stopped"])
        final = self.call("/api/editor/status", wait=10)
        self.assertEqual([s["status"] for s in final["run"]["statements"]], ["stopped", "not_run"])
        self.assertEqual(final["run"]["stopped"]["message"], ed.STOPPED_TEXT)
        self.assertTrue(final["run"]["results"][0]["interrupted"])
        self.assertEqual(self.rows(self.run_sql("SELECT * FROM tt")), [])     # the INSERT was not applied
        record = [r for r in self.store.audit_records() if r["id"] == final["run"]["audit_id"]][0]
        self.assertEqual((record["status"], record["outcome"], record["error_code"]), ("error", "stopped", 1317))

    def test_force_stop_ends_and_never_reconnects(self):
        self.run_sql("SELECT SLEEP(20)", wait=0)
        time.sleep(0.3)
        result = self.call("/api/editor/stop", force=True)
        self.assertTrue(result["session"]["ended"])
        ended = self.run_sql("SELECT 1")
        self.assertEqual(ended["status"], "session_ended")
        self.assertEqual(ended["message"], "Stopped by ending the session. " + ed.SESSION_ENDED_TEXT)
        self.assertEqual((self.state / "argv.log").read_text(encoding="utf-8").count("--xml"), 1)   # no new client
        self.call("/api/editor/close")
        fresh = self.run_sql("SELECT 1")
        self.assertEqual(fresh["status"], "complete")
        self.assertNotEqual(fresh["session"]["connection_id"], result["session"]["connection_id"])

    def test_lost_connection_ends_the_session(self):
        result = self.run_sql("SELECT lose_connection();\nSELECT 2")
        self.assertEqual(result["status"], "session_ended")
        self.assertEqual([s["status"] for s in result["run"]["statements"]], ["error", "not_run"])
        self.assertEqual(result["session"]["message"], ed.SESSION_ENDED_TEXT)
        self.assertEqual(self.run_sql("SELECT 1")["status"], "session_ended")

    def test_refused_connection(self):
        os.environ["FAKE_DENY"] = "1"
        with self.assertRaises(ed.ConnectorError) as caught:
            self.run_sql("SELECT 1")
        self.assertIn("Access denied", str(caught.exception))
        self.assertNotIn("s3cret-pw", str(caught.exception))
        self.assertIsNone(self.registry.get("tab1"))

    def test_busy_and_identity(self):
        self.run_sql("SELECT SLEEP(3)", wait=0)
        with self.assertRaises(ed.EditorError) as caught:
            self.run_sql("SELECT 1")
        self.assertEqual(caught.exception.code, "busy")
        other = dict(self.connection, port=3307)
        with self.assertRaises(ed.EditorError) as caught:
            self.call("/api/editor/run", sql="SELECT 1", connection=other)
        self.assertEqual(caught.exception.code, "identity_mismatch")
        with self.assertRaises(ed.EditorError):
            self.call("/api/editor/stop", connection=dict(self.connection, user="root"))
        self.call("/api/editor/stop")

    def test_too_many_sessions(self):
        self.registry.max_sessions = 2
        self.run_sql("SELECT 1", tab="a")
        self.run_sql("SELECT 1", tab="b")
        with self.assertRaises(ed.EditorError) as caught:
            self.run_sql("SELECT 1", tab="c")
        self.assertEqual(caught.exception.code, "too_many_sessions")
        self.call("/api/editor/close", tab="a")
        self.assertEqual(self.run_sql("SELECT 1", tab="c")["status"], "complete")

    def test_idle_timeout(self):
        opened = self.call("/api/editor/open", idle_minutes=1)
        self.assertEqual(opened["session"]["idle_timeout_seconds"], 60)
        session = self.registry.get("tab1")
        session.last_used -= 61
        self.registry.reap()
        status = self.call("/api/editor/status")["session"]
        self.assertTrue(status["ended"])
        self.assertEqual(status["ended_reason"], "idle")
        self.assertIn("rolled back", status["message"])
        self.assertIsNotNone(session.proc.poll())

    def test_server_exit_ends_every_client(self):
        self.run_sql("SELECT 1", tab="a")
        self.run_sql("SELECT 1", tab="b")
        procs = [self.registry.get(t).proc for t in ("a", "b")]
        self.registry.close()
        for proc in procs:
            self.assertIsNotNone(proc.poll())

    def test_close_with_open_transaction_asks(self):
        self.run_sql("START TRANSACTION")
        result = self.call("/api/editor/close")
        self.assertEqual((result["closed"], result["reason"]), (False, "transaction_open"))
        self.assertTrue(self.call("/api/editor/close", discard=True)["closed"])
        self.assertIsNone(self.registry.get("tab1"))

    def test_status_and_close_require_the_session_connection(self):
        self.run_sql("START TRANSACTION")
        session = self.registry.get("tab1")
        strangers = [None, dict(self.connection, host="other"), dict(self.connection, port=3307),
                     dict(self.connection, user="other"), dict(self.connection, profile_id="other"),
                     dict(self.connection, ssl_mode="REQUIRED"), dict(self.connection, ssl_ca="other.pem")]
        for action in ("status", "close"):
            path = "/api/editor/" + action
            unknown = self.call(path, tab="missing", discard=True)
            for connection in strangers:
                with self.subTest(action=action, connection=connection):
                    request = {"tab_id": "tab1", "discard": True}
                    if connection is not None:
                        request["connection"] = connection
                    with mock.patch.object(session, "end", wraps=session.end) as end:
                        self.assertEqual(ed.api_response(path, request, self.workspace), unknown)
                        end.assert_not_called()
                    self.assertIs(self.registry.get("tab1"), session)
                    self.assertFalse(session.ended)
                    self.assertTrue(session.in_transaction)
                    self.assertIsNone(session.proc.poll())
        # Password and schema changes do not transfer ownership to another endpoint.
        same = dict(self.connection, password="changed", database="another_schema")
        self.assertEqual(self.call("/api/editor/status", connection=same)["session"]["connection_id"],
                         session.connection_id)
        self.assertFalse(self.call("/api/editor/close", connection=same)["closed"])
        self.assertTrue(self.call("/api/editor/close", connection=same, discard=True)["closed"])
        self.assertIsNone(self.registry.get("tab1"))
        self.assertIsNotNone(session.proc.poll())

    def test_close_stop_and_rollback_survive_a_policy_change(self):
        profile = self.save_profile(type="production")
        self.run_sql("START TRANSACTION")
        session = self.registry.get("tab1")
        self.store.save("profile", dict(profile, type="development", read_only=True), profile["id"])
        self.assertEqual(self.call("/api/editor/status")["session"]["connection_id"], session.connection_id)
        self.assertTrue(self.call("/api/editor/stop")["session"] is not None)
        self.assertEqual(self.call("/api/editor/rollback")["status"], "complete")
        self.assertFalse(session.in_transaction)
        closed = self.call("/api/editor/close")
        self.assertTrue(closed["closed"])
        self.assertTrue(closed["session"]["ended"])
        self.assertIsNone(self.registry.get("tab1"))
        self.assertIsNotNone(session.proc.poll())
        # A different endpoint still gets the unknown-tab answer.
        self.store.save("profile", dict(profile, type="production", read_only=False), profile["id"])
        self.run_sql("SELECT 1")
        other = dict(self.connection, user="other")
        self.assertEqual(self.call("/api/editor/close", connection=other),
                         self.call("/api/editor/close", tab="missing"))
        self.assertIsNotNone(self.registry.get("tab1"))
        with self.assertRaises(ed.EditorError):
            self.call("/api/editor/stop", connection=other)

    def test_ended_session_results_keep_their_connection_owner(self):
        self.run_sql("SELECT 42")
        session = self.registry.get("tab1")
        session.end("closed", ed.SESSION_ENDED_TEXT)
        other = dict(self.connection, user="other")
        for action in ("status", "close"):
            path = "/api/editor/" + action
            self.assertEqual(self.call(path, connection=other), self.call(path, tab="missing"))
            self.assertIs(self.registry.get("tab1"), session)
        owned = self.call("/api/editor/status")
        self.assertTrue(owned["session"]["ended"])
        self.assertIsNotNone(owned["run"])
        self.assertTrue(self.call("/api/editor/close")["closed"])
        self.assertIsNone(self.registry.get("tab1"))

    def test_security_session_endpoints_reject_other_owners_without_disclosure(self):
        for ended in (False, True):
            tab = "ended" if ended else "live"
            self.run_sql("SELECT 42", tab=tab)
            session = self.registry.get(tab)
            session.name = "private-production-account"
            if ended:
                session.end("closed", "Private disconnect details")
            other = dict(self.connection, user="other")
            for action in ("run", "commit", "rollback", "stop", "preview", "open"):
                with self.subTest(ended=ended, action=action):
                    with mock.patch.object(session, "status") as status, \
                            mock.patch.object(session, "start_run") as start, \
                            mock.patch.object(session, "stop") as stop, \
                            mock.patch.object(ed, "EditorSession") as create:
                        with self.assertRaises(ed.EditorError) as caught:
                            self.call("/api/editor/" + action, tab=tab, connection=other, sql="SELECT 9")
                        self.assertEqual(caught.exception.code, "identity_mismatch")
                        self.assertNotIn(session.name, str(caught.exception))
                        self.assertNotIn(self.connection["host"], str(caught.exception))
                        self.assertIn("another connection", str(caught.exception))
                        self.assertIn("Close it", str(caught.exception))
                        self.assertNotIn("Private disconnect", str(caught.exception))
                        status.assert_not_called()
                        start.assert_not_called()
                        stop.assert_not_called()
                        create.assert_not_called()
                    self.assertIs(self.registry.get(tab), session)

    def test_security_racing_open_cannot_replace_another_owners_ended_session(self):
        other = dict(self.connection, user="other")
        captured = []

        def fill(payload):
            self.call("/api/editor/open", connection=other)
            session = self.registry.get("tab1")
            session.name = "private-production-account"
            session.end("closed", "Private disconnect details")
            captured.append(session)
            return payload

        with self.assertRaises(ed.EditorError) as caught:
            ed.api_response("/api/editor/run", {"tab_id": "tab1", "connection": self.connection,
                                               "sql": "SELECT 9", "wait": 5}, self.workspace, fill)
        self.assertEqual(caught.exception.code, "identity_mismatch")
        self.assertNotIn(captured[0].name, str(caught.exception))
        self.assertIs(self.registry.get("tab1"), captured[0])
        self.assertFalse(self.logged("tab1", "SELECT 9"))

    def test_security_racing_run_uses_the_authenticated_password_for_history(self):
        def fill(payload):
            self.call("/api/editor/open", connection=dict(self.connection, password="racing-session-secret"))
            return payload

        result = ed.api_response("/api/editor/run", {
            "tab_id": "tab1", "connection": self.connection,
            "sql": "SELECT 'racing-session-secret'", "wait": 5,
        }, self.workspace, fill)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.call("/api/editor/history")["items"][0]["sql"], "SELECT '[redacted]'")

    def test_security_ended_owner_can_explicitly_reconnect(self):
        self.run_sql("SELECT 42")
        previous = self.registry.get("tab1")
        previous.end("closed", ed.SESSION_ENDED_TEXT)
        self.assertEqual(self.run_sql("SELECT 1")["status"], "session_ended")
        same = dict(self.connection, password="replacement-password", database="another_schema")
        opened = self.call("/api/editor/open", connection=same)
        self.assertFalse(opened["session"]["ended"])
        self.assertIsNot(self.registry.get("tab1"), previous)
        self.assertEqual(self.registry.get("tab1").identity, previous.identity)

    def test_security_ended_session_requires_exact_identity_after_policy_change(self):
        profile = self.save_profile(type="production")
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                self.store.save("profile", dict(profile, type="production", read_only=False), profile["id"])
                self.call("/api/editor/open")
                previous = self.registry.get("tab1")
                previous.end("closed", ed.SESSION_ENDED_TEXT)
                self.store.save("profile", dict(profile, type="development", read_only=read_only), profile["id"])
                for action in ("run", "commit", "open"):
                    with self.subTest(action=action):
                        with self.assertRaises(ed.EditorError) as caught:
                            self.call("/api/editor/" + action, sql="SELECT 1")
                        self.assertEqual(caught.exception.code, "identity_mismatch")
                        self.assertIs(self.registry.get("tab1"), previous)
                # Status, Stop, Rollback and Close follow endpoint ownership, not the policy.
                self.assertEqual(self.call("/api/editor/status")["session"]["connection_id"],
                                 previous.connection_id)
                self.assertFalse(self.call("/api/editor/stop")["session"] is None)
                self.assertEqual(self.call("/api/editor/rollback")["status"], "session_ended")
                self.assertTrue(self.call("/api/editor/close")["closed"])
                self.assertIsNone(self.registry.get("tab1"))
                opened = self.call("/api/editor/open")
                self.assertFalse(opened["session"]["ended"])
                self.assertEqual(opened["session"]["read_only_transactions"], read_only)
                self.assertIsNot(self.registry.get("tab1"), previous)
                result = self.call("/api/editor/run", sql="SET @x = 1")
                self.assertEqual(result["status"], "refused" if read_only else "complete")
                self.call("/api/editor/close", discard=True)

    def test_security_ended_owner_still_requires_profile_host_and_tls(self):
        self.save_profile(type="production")
        self.run_sql("SELECT 42")
        session = self.registry.get("tab1")
        session.end("closed", ed.SESSION_ENDED_TEXT)
        other_profile = self.store.save("profile", {"name": "other", "host": "127.0.0.1", "port": 3306,
                                                     "user": "app"})
        strangers = [dict(self.connection, profile_id=other_profile["id"]),
                     dict(self.connection, host="other"), dict(self.connection, ssl_mode="REQUIRED"),
                     dict(self.connection, ssl_ca="other.pem")]
        for connection in strangers:
            with self.subTest(connection=connection):
                with self.assertRaises(ed.EditorError) as caught:
                    self.call("/api/editor/open", connection=connection)
                self.assertEqual(caught.exception.code, "identity_mismatch")
                self.assertIs(self.registry.get("tab1"), session)
                self.assertIsNone(self.call("/api/editor/status", connection=connection)["session"])
                self.assertIsNone(self.call("/api/editor/close", connection=connection)["session"])
                self.assertIs(self.registry.get("tab1"), session)

    def test_security_export_does_not_look_up_a_session(self):
        with mock.patch.object(self.registry, "get", side_effect=AssertionError("session lookup")):
            exported = self.call("/api/editor/export", format="json",
                                 result={"columns": ["value"], "rows": [["caller-owned"]]})
        self.assertEqual(json.loads(exported["content"])["rows"], [["caller-owned"]])

    def test_security_reused_session_redacts_password_from_history_and_audit(self):
        self.run_sql("SELECT 1")
        request = dict(self.connection)
        password = request.pop("password")
        session = self.registry.get("tab1")
        self.assertNotIn("profile_id", request)
        result = self.run_sql("SELECT 's3cret-pw'", connection=request)
        self.assertEqual(result["status"], "complete")
        self.assertIs(self.registry.get("tab1"), session)
        history = self.call("/api/editor/history")["items"][0]
        self.assertEqual(history["sql"], "SELECT '[redacted]'")
        # Refused and declined writes are audited before credential lookup.
        for decline in (False, True):
            result = self.call("/api/editor/run", connection=request,
                               sql="SET @x = 's3cret-pw'", decline=decline)
            self.assertEqual(result["status"], "declined" if decline else "needs_confirmation")
            self.assertNotIn(password, json.dumps(self.store.audit_records()[0]))
        # Exercise the allowed run too: its audit and history are persisted
        # after credential lookup, with no password/profile in this request.
        executed = self.run_sql("SET @x = 's3cret-pw'", connection=request)
        self.assertEqual(executed["status"], "complete")
        record = next(r for r in self.store.audit_records() if r["id"] == executed["run"]["audit_id"])
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["statements"], ["SET @x = '[redacted]'"])
        self.assertEqual(self.call("/api/editor/history")["items"][0]["sql"], "SET @x = '[redacted]'")
        self.assertNotIn(password.encode(), self.store.path.read_bytes())

    def test_unknown_transaction_state(self):
        os.environ["FAKE_PS"] = "off"
        try:
            status = self.call("/api/editor/open")["session"]
        finally:
            os.environ.pop("FAKE_PS", None)
        self.assertIsNone(status["in_transaction"])
        self.assertIn("unknown", status["transaction_note"])

    # -- safety -------------------------------------------------------------------------
    def save_profile(self, **safety):
        profile = self.store.save("profile", dict({"name": "dev", "host": "127.0.0.1", "port": 3306, "user": "app"},
                                                  **safety))
        self.connection = {"host": "127.0.0.1", "port": 3306, "user": "app", "password": "s3cret-pw",
                           "profile_id": profile["id"]}
        return profile

    def test_review_policy_race_during_password_lookup(self):
        for field, value in (("read_only", True), ("type", "production")):
            for existing in (False, True):
                with self.subTest(field=field, existing=existing):
                    profile = self.save_profile(type="development")
                    tab = "race-%s-%s" % (field, existing)
                    if existing:
                        self.call("/api/editor/open", tab=tab)
                    def fill(payload):
                        self.store.save("profile", dict(profile, **{field: value}), profile["id"])
                        return payload
                    reply = ed.api_response("/api/editor/run", {
                        "tab_id": tab, "connection": self.connection,
                        "sql": "INSERT INTO tt VALUES (1, 'x')", "wait": 15,
                    }, self.workspace, fill)
                    self.assertEqual(reply["status"], "refused")
                    self.assertIn("nothing was sent", reply["message"].lower())
                    session = self.registry.get(tab)
                    if session:
                        self.assertFalse(self.logged(tab, "INSERT INTO tt"))

    def test_open_lowering_safe_updates_rereads_policy_after_password_lookup(self):
        for field, value in (("read_only", True), ("type", "production")):
            for existing in (False, True):
                with self.subTest(field=field, existing=existing):
                    profile = self.save_profile(type="development")
                    tab = "open-%s-%s" % (field, existing)
                    if existing:
                        self.call("/api/editor/open", tab=tab)

                    def fill(payload):
                        self.store.save("profile", dict(profile, **{field: value}), profile["id"])
                        return payload
                    reply = ed.api_response("/api/editor/open", {
                        "tab_id": tab, "connection": self.connection, "safe_updates": False, "wait": 15,
                    }, self.workspace, fill)
                    self.assertEqual(reply["status"], "refused")
                    self.assertIn("nothing was sent", reply["message"].lower())
                    if existing:
                        self.assertFalse(self.logged(tab, ed.SAFE_UPDATES_OFF))
                    else:
                        self.assertIsNone(self.registry.get(tab))

    def test_open_lowering_safe_updates_rereads_policy_after_connecting(self):
        """Security review: the policy can change while the client connects
        (after the password-lookup re-read); SET sql_safe_updates = 0 must not
        be sent then. A session this request started is ended, not left open
        with safe updates deferred under the old policy."""
        original = ed._open_session
        for field, value in (("read_only", True), ("type", "production")):
            for existing in (False, True):
                with self.subTest(field=field, existing=existing):
                    profile = self.save_profile(type="development")
                    tab = "connect-%s-%s" % (field, existing)
                    if existing:
                        self.call("/api/editor/open", tab=tab)
                    opened = []

                    def connect(*args, **kwargs):
                        session = original(*args, **kwargs)
                        opened.append(session)
                        self.store.save("profile", dict(profile, **{field: value}), profile["id"])
                        return session
                    audits = len(self.store.audit_records())
                    with mock.patch.object(ed, "_open_session", side_effect=connect):
                        reply = self.call("/api/editor/open", tab=tab, safe_updates=False)
                    self.assertEqual(reply["status"], "refused")
                    self.assertIn("nothing was sent", reply["message"].lower())
                    session = opened[0]
                    log = self.state / ("session-%d.log" % session.connection_id)
                    self.assertFalse(log.exists() and ed.SAFE_UPDATES_OFF in log.read_text(encoding="utf-8"))
                    records = self.store.audit_records()
                    self.assertEqual(len(records), audits + 1)
                    self.assertEqual((records[0]["operation"], records[0]["status"], records[0]["statements"]),
                                     ("editor_safe_updates", "refused", [ed.SAFE_UPDATES_OFF]))
                    if existing:
                        self.assertIs(self.registry.get(tab), session)
                        self.assertFalse(session.ended)
                        self.assertEqual(reply["session"]["state"], "idle")
                    else:
                        self.assertIsNone(self.registry.get(tab))
                        self.assertTrue(session.ended)
                        self.assertIsNone(reply["session"])

    def test_review_drop_database_requires_exact_typed_target(self):
        for ctype in ("production", "development"):
            self.save_profile(type=ctype)
            for sql, target in (("DROP DATABASE Camel", "Camel"),
                                ("DROP SCHEMA IF EXISTS `a``b`", "a`b"),
                                ("/*!80000 DROP DATABASE `Mixed` */", "Mixed")):
                with self.subTest(type=ctype, sql=sql):
                    asked = self.call("/api/editor/run", sql=sql)
                    self.assertEqual(asked["confirmation"]["typed_target"], target)
                    for typed in (None, target.lower(), target + " "):
                        if typed == target:
                            continue
                        answer = self.call("/api/editor/run", sql=sql, confirmed=True,
                                           confirm_token=asked["confirmation"]["token"], typed_target=typed)
                        self.assertEqual(answer["status"], "needs_confirmation")
                        asked = answer
                    answer = self.call("/api/editor/run", sql=sql, confirmed=True,
                                       confirm_token=asked["confirmation"]["token"], typed_target=target)
                    self.assertNotEqual(answer["status"], "needs_confirmation")
                    self.registry.close()
                    self.registry = ed.EditorSessions(reap_every=0)
                    self.workspace.editor = self.registry
                    self.addCleanup(self.registry.close)

    def test_review_multiple_ddl_refused_before_password(self):
        for sql in ("DROP TABLE a; DROP TABLE b", "CREATE TABLE a (n int); ALTER TABLE a ADD b int",
                    "DELIMITER $$\nDROP TABLE a; DROP TABLE b$$"):
            fill = mock.Mock(side_effect=AssertionError("must refuse before password lookup"))
            reply = ed.api_response("/api/editor/run", {"tab_id": "ddl", "connection": self.connection,
                                    "sql": sql, "scope": "all", "confirmed": True}, self.workspace, fill)
            self.assertEqual(reply["status"], "refused")
            self.assertIn("one at a time", reply["message"])
            fill.assert_not_called()

    def test_review_alter_context_rechecked_under_run_lock(self):
        sql = "ALTER TABLE tt ADD COLUMN c INT"
        review = {"schema": "reviewed", "table": "tt", "warnings": []}
        original = ed.EditorSession._helper_row
        def row(session, step):
            if "AS database_name" in step.send:
                self.assertTrue(session.run_lock.locked())
                return {"database_name": "changed"}
            return original(session, step)
        with mock.patch.object(ed, "_alter_review", return_value=review), \
                mock.patch.object(ed.EditorSession, "_helper_row", row):
            asked = self.call("/api/editor/run", sql=sql)
            with self.assertRaisesRegex(ed.EditorError, "context changed"):
                self.call("/api/editor/run", sql=sql, confirmed=True,
                          confirm_token=asked["confirmation"]["token"])
        self.assertFalse(self.logged("tab1", sql))
        self.assertFalse(self.logged("tab1", ed.ALTER_LOCK_WAIT))
        self.assertEqual(self.store.audit_records()[0]["outcome"], "target_changed")

    def test_review_alter_guard_error_stops_before_ddl(self):
        # Change this test's fake binary only, leaving the shared fixture intact.
        client = Path(self.temp.name) / "bin" / "mysql"
        text = client.read_text(encoding="utf-8")
        text = text.replace("        upper = text.upper()\n", "        upper = text.upper()\n"
                            "        if upper == 'SET SESSION LOCK_WAIT_TIMEOUT = 5':\n"
                            "            self.error(line, 1227, '42000', 'Guard denied')\n"
                            "            return\n")
        client.write_text(text)
        sql = "ALTER TABLE test.tt ADD COLUMN c INT"
        with mock.patch.object(ed, "_alter_review", return_value={"schema": "test", "table": "tt", "warnings": []}):
            done = self.run_sql(sql)
        self.assertEqual(done["status"], "error")
        self.assertEqual(done["run"]["error"]["code"], 1227)
        self.assertFalse(self.logged("tab1", sql))
        self.assertEqual(done["run"]["statements"][1]["status"], "not_run")

    def test_review_policy_changes_after_session_start(self):
        profile = self.save_profile(type="development")
        original = ed._open_session
        opened = []
        def opening(*args, **kwargs):
            session = original(*args, **kwargs)
            opened.append(session)
            self.store.save("profile", dict(profile, read_only=True), profile["id"])
            return session
        with mock.patch.object(ed, "_open_session", side_effect=opening):
            result = self.call("/api/editor/run", sql="INSERT INTO tt VALUES (1, 'x')")
        self.assertEqual(result["status"], "refused")
        # The session this run started is ended, and nothing reached it.
        self.assertIsNone(self.registry.get("tab1"))
        log = self.state / ("session-%d.log" % opened[0].connection_id)
        self.assertFalse(log.exists() and "INSERT INTO tt" in log.read_text(encoding="utf-8"))

    def test_review_policy_refusal_ends_only_a_session_the_run_started(self):
        """The final policy re-read refuses the run. A session this request
        started is ended and removed (not left open until Close); a session
        the tab already had is kept."""
        original = ed._open_session
        for existing in (False, True):
            with self.subTest(existing=existing):
                profile = self.save_profile(type="development")
                tab = "final-%s" % existing
                if existing:
                    self.call("/api/editor/open", tab=tab)
                before = self.registry.get(tab)
                opened = []

                def opening(*args, **kwargs):
                    session = original(*args, **kwargs)
                    opened.append(session)
                    return session

                def drift(*args, **kwargs):
                    # The last re-read before start_run sees the new policy.
                    self.store.save("profile", dict(profile, read_only=True), profile["id"])
                    return real_run(*args, **kwargs)
                real_run = ed.Run
                with mock.patch.object(ed, "_open_session", side_effect=opening), \
                        mock.patch.object(ed, "Run", side_effect=drift):
                    result = self.call("/api/editor/run", tab=tab, sql="INSERT INTO tt VALUES (1, 'x')")
                self.assertEqual(result["status"], "refused")
                self.assertIn("nothing was sent", result["message"].lower())
                if existing:
                    self.assertEqual(opened, [])
                    self.assertIs(self.registry.get(tab), before)
                    self.assertFalse(before.ended)
                    self.assertEqual(result["session"]["state"], "idle")
                    self.assertFalse(self.logged(tab, "INSERT INTO tt"))
                else:
                    self.assertEqual(len(opened), 1)
                    self.assertTrue(opened[0].ended)
                    self.assertIsNone(self.registry.get(tab))
                    self.assertIsNone(result["session"])
                    log = self.state / ("session-%d.log" % opened[0].connection_id)
                    self.assertFalse(log.exists() and "INSERT INTO tt" in log.read_text(encoding="utf-8"))

    def test_review_policy_refusal_keeps_a_new_session_another_request_holds(self):
        """Review gate: between creating the session and the final re-read,
        another request can take the session for its own run (run_lock is held
        for the whole run). The refused request must not end it."""
        profile = self.save_profile(type="development")
        original = ed._open_session
        real_run = ed.Run
        opened = []

        def opening(*args, **kwargs):
            session = original(*args, **kwargs)
            opened.append(session)
            return session

        def drift(*args, **kwargs):
            self.store.save("profile", dict(profile, read_only=True), profile["id"])
            self.assertTrue(opened[0].run_lock.acquire(False))  # another request's run
            return real_run(*args, **kwargs)
        with mock.patch.object(ed, "_open_session", side_effect=opening), \
                mock.patch.object(ed, "Run", side_effect=drift):
            result = self.call("/api/editor/run", tab="held", sql="INSERT INTO tt VALUES (1, 'x')")
        try:
            self.assertEqual(result["status"], "refused")
            self.assertFalse(opened[0].ended)
            self.assertIs(self.registry.get("held"), opened[0])
            self.assertIsNotNone(result["session"])
        finally:
            opened[0].run_lock.release()

    def test_review_alter_guard_is_shown_and_sent_in_same_session(self):
        sql = "ALTER TABLE test.tt ADD COLUMN c INT"
        with mock.patch.object(ed, "_alter_review", return_value={
                "schema": "test", "table": "tt", "approx_bytes": 1234,
                "metadata_locks": [], "transactions": [], "warnings": [],
                "suggestion": sql + ", ALGORITHM=INPLACE, LOCK=NONE"}):
            asked = self.call("/api/editor/run", sql=sql)
            self.assertEqual(asked["confirmation"]["alter_preflight"]["approx_bytes"], 1234)
            self.assertEqual(asked["confirmation"]["statements"],
                             [ed.ALTER_LOCK_WAIT, sql])
            done = self.call("/api/editor/run", sql=sql, confirmed=True,
                             confirm_token=asked["confirmation"]["token"])
            self.assertIn(done["status"], ("complete", "error"))
            log = self.session_log()
            self.assertLess(log.index(ed.ALTER_LOCK_WAIT), log.index(sql))
            self.assertNotIn(sql + ", ALGORITHM=INPLACE, LOCK=NONE", log)
            records = self.store.audit_records()
            self.assertTrue(any(r["statements"] == [ed.ALTER_LOCK_WAIT, sql] for r in records))

    def test_production_confirms_writes_and_audits(self):
        asked = self.call("/api/editor/run", sql="INSERT INTO tt VALUES (1, 'x'); SELECT 1")
        self.assertEqual(asked["status"], "needs_confirmation")
        confirmation = asked["confirmation"]
        self.assertEqual(confirmation["statements"], ["INSERT INTO tt VALUES (1, 'x')", "SELECT 1"])
        self.assertEqual(confirmation["title"], "Production connection")
        self.assertIsNone(self.registry.get("tab1"))         # nothing opened before the confirmation
        again = self.call("/api/editor/run", sql="INSERT INTO tt VALUES (1, 'x'); SELECT 1", confirmed=True)
        self.assertEqual(again["status"], "needs_confirmation")   # confirmed needs the token of what was shown
        changed = self.call("/api/editor/run", sql="INSERT INTO tt VALUES (2, 'x'); SELECT 1", confirmed=True,
                            confirm_token=confirmation["token"])
        self.assertEqual(changed["status"], "needs_confirmation")
        blocked = self.store.audit_records()
        self.assertEqual(len(blocked), 3)
        self.assertTrue(all((r["status"], r["outcome"]) == ("refused", "needs_confirmation") for r in blocked))
        run = self.call("/api/editor/run", sql="CREATE TEMPORARY TABLE tt (id INT); INSERT INTO tt VALUES (1, 'x')",
                        confirmed=True, confirm_token=self.call(
                            "/api/editor/run", sql="CREATE TEMPORARY TABLE tt (id INT); INSERT INTO tt VALUES (1, 'x')"
                        )["confirmation"]["token"])
        self.assertEqual(run["status"], "complete")
        records = self.store.audit_records()
        self.assertEqual(len(records), 5)  # four unconfirmed requests and the completed run
        self.assertEqual((records[0]["status"], records[0]["operation"], records[0]["surface"]),
                         ("ok", "editor_run", "ui"))
        self.assertEqual(records[0]["statements"], ["CREATE TEMPORARY TABLE tt (id INT)",
                                                    "INSERT INTO tt VALUES (1, 'x')"])
        self.assertEqual(run["run"]["audit_id"], records[0]["id"])
        # Reads are not audited.
        self.run_sql("SELECT 1")
        self.assertEqual(len(self.store.audit_records()), 5)
        # Production starts with sql_safe_updates on.
        self.assertIs(run["session"]["safe_updates"], True)
        self.assertIn("/*myflames:editor*/ SET SESSION sql_safe_updates = 1", self.session_log())

    def test_secrets_are_redacted_in_audit_and_history(self):
        sql = "CREATE USER u IDENTIFIED BY 'hunter2'; SELECT 's3cret-pw'"
        self.run_sql(sql)
        record = self.store.audit_records()[0]
        self.assertEqual(record["statements"], ["CREATE USER u IDENTIFIED BY '[redacted]'", "SELECT '[redacted]'"])
        history = self.call("/api/editor/history")["items"][0]
        self.assertNotIn("hunter2", history["sql"])
        self.assertNotIn("s3cret-pw", json.dumps(history))
        self.assertNotIn("s3cret-pw", json.dumps(record))

    def test_decline_and_refuse_are_audited(self):
        declined = self.call("/api/editor/run", sql="INSERT INTO t VALUES (1)", decline=True)
        self.assertEqual(declined["status"], "declined")
        self.save_profile(read_only=True)
        refused = self.call("/api/editor/run", sql="INSERT INTO t VALUES (1)")
        self.assertEqual(refused["status"], "refused")
        self.assertIsNone(self.registry.get("tab1"))
        statuses = sorted(r["status"] for r in self.store.audit_records())
        self.assertEqual(statuses, ["declined", "refused"])
        # Reads run, inside a read-only session.
        read = self.run_sql("SELECT 1")
        self.assertEqual(read["status"], "complete")
        self.assertIn("/*myflames:editor*/ SET SESSION TRANSACTION READ ONLY", self.session_log())
        self.assertTrue(read["session"]["read_only_transactions"])
        lifted = self.call("/api/editor/run", sql="START TRANSACTION READ WRITE")
        self.assertEqual(lifted["status"], "refused")

    def test_explicit_review_tightens_development_policy(self):
        self.save_profile(type="development")
        sql = "UPDATE t SET v = 'enabled' WHERE id = 1"
        asked = self.call("/api/editor/run", sql=sql, require_confirmation=True)
        self.assertEqual(asked["status"], "needs_confirmation")
        self.assertIsNone(self.registry.get("tab1"))
        self.assertEqual(asked["confirmation"]["statements"], [sql])
        changed = self.call("/api/editor/run", sql=sql + " AND 1=1", require_confirmation=True,
                            confirmed=True, confirm_token=asked["confirmation"]["token"])
        self.assertEqual(changed["status"], "needs_confirmation")
        asked = self.call("/api/editor/run", sql=sql, require_confirmation=True)
        done = self.call("/api/editor/run", sql=sql, require_confirmation=True,
                         confirmed=True, confirm_token=asked["confirmation"]["token"])
        self.assertEqual(done["status"], "complete")
        self.assertIn(sql, self.session_log())
        self.assertEqual(self.store.audit_records()[0]["status"], "ok")

    def test_explicit_review_preserves_read_only_refusal(self):
        self.save_profile(type="development", read_only=True)
        answer = self.call("/api/editor/run", sql="UPDATE t SET a=1", require_confirmation=True)
        self.assertEqual(answer["status"], "refused")
        self.assertIsNone(self.registry.get("tab1"))

    def test_development_runs_plain_writes(self):
        self.save_profile(type="development")
        result = self.run_sql("CREATE TEMPORARY TABLE tt (id INT); INSERT INTO tt VALUES (1, 'a')")
        self.assertEqual(result["status"], "complete")
        self.assertIs(result["session"]["safe_updates"], False)
        self.assertEqual([r["status"] for r in self.store.audit_records()], ["ok", "refused"])
        risky = self.call("/api/editor/run", sql="DROP TABLE t")
        self.assertEqual(risky["status"], "needs_confirmation")
        self.assertEqual(risky["confirmation"]["title"], "Risky write")

    def test_safe_updates_override(self):
        self.save_profile(type="development")
        result = self.call("/api/editor/open", safe_updates=True)
        self.assertIs(result["session"]["safe_updates"], True)
        result = self.call("/api/editor/open", safe_updates=False)
        self.assertIs(result["session"]["safe_updates"], False)

    def test_safety_read_only_development_cannot_disable_safe_updates(self):
        self.save_profile(type="development", read_only=True)
        fill = mock.Mock(side_effect=AssertionError("password lookup before refusal"))
        result = ed.api_response("/api/editor/open", {
            "tab_id": "tab1", "connection": self.connection, "safe_updates": False,
        }, self.workspace, fill)
        self.assertEqual(result["status"], "refused")
        self.assertIsNone(self.registry.get("tab1"))
        fill.assert_not_called()
        self.assertEqual(self.store.audit_records()[0]["status"], "refused")
        # An already-open read-only Development session defaults to off, but
        # an explicit request to disable protection must still be refused.
        self.call("/api/editor/open")
        self.assertEqual(self.call("/api/editor/open", safe_updates=False)["status"], "refused")

    def test_safety_development_disable_is_audited_even_on_new_session(self):
        self.dev()
        result = self.call("/api/editor/open", safe_updates=False)
        self.assertIs(result["session"]["safe_updates"], False)
        record = self.store.audit_records()[0]
        self.assertEqual((record["operation"], record["status"], record["statements"]),
                         ("editor_safe_updates", "ok", [ed.SAFE_UPDATES_OFF]))
        self.assertIn(record["statements"][0], self.session_log())

    def test_safety_enabling_safe_updates_is_audited_on_new_and_existing_sessions(self):
        self.dev()
        for tab in ("new", "existing"):
            with self.subTest(tab=tab):
                if tab == "existing":
                    self.call("/api/editor/open", tab=tab)
                result = self.call("/api/editor/open", tab=tab, safe_updates=True)
                self.assertIs(result["session"]["safe_updates"], True)
                record = self.store.audit_records()[0]
                self.assertEqual((record["status"], record["statements"]), ("ok", [ed.SAFE_UPDATES_ON]))
        self.assertEqual(len(self.store.audit_records()), 2)

    def test_safety_safe_updates_missing_completion_is_unknown(self):
        self.call("/api/editor/open")
        session = self.registry.get("tab1")
        for marker, ended in ((False, False), (False, True), (True, True)):
            with self.subTest(marker=marker, ended=ended):
                step = mock.Mock(marker=marker, real_errors=lambda: [])
                audit = dbops.AuditTrail(self.store, "editor_safe_updates", "ui")
                with mock.patch.object(session, "_helper", return_value=step), \
                        mock.patch.object(session, "ended", ended), \
                        mock.patch.object(session, "refresh_state") as refresh:
                    # Stub only the identity read when the implementation has
                    # one, so this regression also executes on the old code.
                    with mock.patch.object(session, "_verify_identity", create=True):
                        with self.assertRaises(ed.EditorError):
                            ed._set_safe_updates(session, ed.SAFE_UPDATES_OFF, audit)
                record = self.store.audit_records()[0]
                self.assertEqual((record["status"], record["outcome"]), ("unknown", "unknown"))
                self.assertTrue(session.safe_updates_wanted)
                refresh.assert_not_called()
                self.assertFalse(session.run_lock.locked())

    def test_safety_helper_error_is_not_reported_as_success(self):
        self.call("/api/editor/open")
        session = self.registry.get("tab1")
        step = mock.Mock(marker=True, real_errors=lambda: [{"code": 1227, "message": "denied"}])
        audit = dbops.AuditTrail(self.store, "editor_safe_updates", "ui")
        with mock.patch.object(session, "_helper", return_value=step), \
                mock.patch.object(session, "_verify_identity", create=True), \
                mock.patch.object(session, "refresh_state"):
            with self.assertRaises(ed.EditorError):
                ed._set_safe_updates(session, ed.SAFE_UPDATES_OFF, audit)
        self.assertTrue(session.safe_updates_wanted)
        self.assertEqual(self.store.audit_records()[0]["status"], "error")

    def test_review_safe_updates_exceptions_finish_audit(self):
        self.call("/api/editor/open")
        session = self.registry.get("tab1")
        for phase in ("_helper", "refresh_state"):
            for error, status in ((RuntimeError("failed s3cret-pw"), "error"),
                                  (KeyboardInterrupt(), "unknown")):
                with self.subTest(phase=phase, error=type(error).__name__):
                    audit = dbops.AuditTrail(self.store, "editor_safe_updates", "ui", password="s3cret-pw")
                    step = mock.Mock(marker=True, real_errors=lambda: [])
                    with mock.patch.object(session, "_verify_identity"), \
                            mock.patch.object(session, "_helper", return_value=step) as helper, \
                            mock.patch.object(session, "refresh_state") as refresh:
                        (helper if phase == "_helper" else refresh).side_effect = error
                        with self.assertRaises(type(error)) as caught:
                            ed._set_safe_updates(session, ed.SAFE_UPDATES_OFF, audit)
                    self.assertIs(caught.exception, error)
                    record = self.store.audit_records()[0]
                    self.assertEqual(record["status"], status)
                    self.assertTrue(record["finished_at"])
                    self.assertNotIn("s3cret-pw", json.dumps(record))
                    self.assertFalse(audit.pending)
                    self.assertFalse(session.run_lock.locked())

    def test_review_thread_start_failure_finishes_audit_and_frees_tab(self):
        for error, status in ((RuntimeError("cannot start s3cret-pw"), "error"),
                              (KeyboardInterrupt(), "unknown")):
            with self.subTest(error=type(error).__name__):
                tab = type(error).__name__
                self.call("/api/editor/open", tab=tab)
                session = self.registry.get(tab)
                plan = ed._plan_run({"sql": "SET @unsent = 1"}, session)
                audit = dbops.AuditTrail(self.store, "editor_run", "ui", password="s3cret-pw")
                run = ed.Run(plan["units"], ed.DEFAULT_LIMIT, dict(self.connection), plan["text"],
                             plan["offsets"], plan["scope"], audit=audit)
                with mock.patch.object(ed.threading.Thread, "start", side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        session.start_run(run)
                self.assertIs(caught.exception, error)
                record = self.store.audit_records()[0]
                self.assertEqual(record["status"], status)
                self.assertTrue(record["finished_at"])
                self.assertNotIn("s3cret-pw", json.dumps(record))
                self.assertFalse(audit.pending)
                self.assertIsNone(audit.password)
                self.assertIsNone(run.options)
                self.assertFalse(session.run_lock.locked())
                self.assertFalse(session.busy())
                self.assertFalse(self.logged(tab, "SET @unsent"))
                self.assertEqual(self.rows(self.run_sql("SELECT 2", tab=tab)), [["2"]])

    def test_safety_confirmation_attempts_and_full_script_are_audited(self):
        sql = "SELECT 1; SET @x = 2; SELECT 3"
        asked = self.call("/api/editor/run", sql=sql)
        shown = asked["confirmation"]["statements"]
        record = self.store.audit_records()[0]
        self.assertEqual((record["status"], record["outcome"]), ("refused", "needs_confirmation"))
        self.assertEqual(record["statements"], shown)
        self.assertEqual(asked["audit_id"], record["id"])
        result = self.call("/api/editor/run", sql=sql, confirmed=True,
                           confirm_token=asked["confirmation"]["token"])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.store.audit_records()[0]["statements"], shown)
        self.assertEqual([line for line in self.session_log() if not line.startswith(ed.TAG)], shown)

    def test_safety_development_commit_policy_and_password_redaction(self):
        self.dev()
        # COMMIT remains an ordinary write: its button is already the user's
        # explicit action. Production confirms it; Development allows/audits it.
        self.connection["password"] = "COMMIT"
        self.run_sql("START TRANSACTION; SET @x = 1")
        request = dict(self.connection)
        request.pop("password")  # use the password of the existing session
        result = self.call("/api/editor/commit", connection=request)
        self.assertEqual(result["status"], "complete")
        record = self.store.audit_records()[0]
        self.assertEqual((record["operation"], record["status"]), ("editor_commit", "ok"))
        self.assertNotIn("COMMIT", json.dumps(record))
        self.assertNotIn("COMMIT", json.dumps(self.call("/api/editor/history")["items"][0]))

    def _review_blocked_action(self, action, tab):
        self.run_sql("START TRANSACTION", tab=tab)
        session = self.registry.get(tab)
        send = session._send

        def gated(kind, sql, **kwargs):
            return send(kind, sql + " /*gate*/" if kind == "action" else sql, **kwargs)

        request = dict(self.connection)
        request.pop("password")  # actions must use the authenticated session
        with mock.patch.object(session, "_send", side_effect=gated):
            result = self.call("/api/editor/" + action.lower(), tab=tab, connection=request, wait=0)
            self.until(lambda: self.logged(tab, action + " /*gate*/"), "the blocked action")
        self.assertNotIn("s3cret-pw", json.dumps(result))
        self.assertEqual(session.run.current.kind, "action")
        return session, session.run

    def test_review_stop_kills_transaction_actions(self):
        self.dev()
        for action in ("COMMIT", "ROLLBACK"):
            with self.subTest(action=action):
                tab = action.lower()
                session, run = self._review_blocked_action(action, tab)
                result = self.call("/api/editor/stop", tab=tab)
                self.assertTrue(result["stopped"], result)
                self.assertTrue(run.done.wait(5))
                self.assertEqual(run.state, "stopped")
                self.assertFalse(session.ended)
                self.assertIsNone(run.options)
                self.assertNotIn("s3cret-pw", json.dumps(self.call("/api/editor/status", tab=tab)))
                self.assertNotIn("s3cret-pw", json.dumps(self.store.audit_records()))

    def test_review_close_kills_transaction_actions_with_session_credentials(self):
        self.dev()
        for action in ("COMMIT", "ROLLBACK"):
            with self.subTest(action=action):
                tab = action.lower()
                session, run = self._review_blocked_action(action, tab)
                request = dict(self.connection)
                request.pop("password")
                with mock.patch.object(ed, "MySQLConnection", wraps=ed.MySQLConnection) as control:
                    result = self.call("/api/editor/close", tab=tab, connection=request, discard=True)
                self.assertTrue(result["closed"])
                control.assert_called_once()
                self.assertEqual(control.call_args[1]["password"], "s3cret-pw")
                self.assertEqual(control.call_args[1]["host"], self.connection["host"])
                self.assertIn("KILL QUERY %d" % session.connection_id, (self.state / "kills.log").read_text(encoding="utf-8"))
                self.assertTrue(run.done.wait(5))
                self.assertIsNone(run.options)
                self.assertNotIn("s3cret-pw", json.dumps(result))
                self.assertNotIn("s3cret-pw", json.dumps(self.store.audit_records()))

    def test_safety_client_environment_is_scrubbed(self):
        secrets = {"MYSQL_PWD": "env-secret", "MYSQL_HOST": "wrong-server", "MYSQL_TCP_PORT": "9999",
                   "MYSQL_TEST_LOGIN_FILE": "/private/login-file", "AWS_SECRET_ACCESS_KEY": "aws-secret",
                   "MYFLAMES_VAULT_PASSPHRASE": "vault-secret", "ARBITRARY_API_TOKEN": "api-secret"}
        with mock.patch.dict(os.environ, secrets):
            self.call("/api/editor/open")
        for call in self.processes.call_args_list:
            env = call[1].get("env")
            self.assertIsInstance(env, dict)
            for name, secret in secrets.items():
                self.assertNotEqual(env.get(name), secret, name)
            self.assertNotEqual(env["HOME"], self.temp.name)
            self.assertEqual(env["USERPROFILE"], env["HOME"])
        home = self.processes.call_args_list[-1][1]["env"]["HOME"]
        self.call("/api/editor/close", discard=True)
        self.assertFalse(os.path.exists(home))

    def test_safety_live_identity_is_checked_before_every_write_path(self):
        self.dev()
        for path, payload in (("run", {"sql": "SET @x = 99"}), ("commit", {}),
                              ("open", {"safe_updates": True})):
            with self.subTest(path=path):
                self.call("/api/editor/open", tab=path)
                session = self.registry.get(path)
                original = session._helper_row

                def changed(step):
                    row = original(step)
                    if row and "hostname" in row:
                        row["hostname"] = "another-server"
                    return row
                with mock.patch.object(session, "_helper_row", side_effect=changed):
                    with self.assertRaises(ed.EditorError) as caught:
                        self.call("/api/editor/" + path, tab=path, **payload)
                self.assertEqual(caught.exception.code, "identity_mismatch")
                log = (self.state / ("session-%d.log" % session.connection_id)).read_text(encoding="utf-8")
                self.assertNotIn("SET @x = 99", log)
                self.assertNotIn(ed.TAG + "COMMIT", log)
                self.assertNotIn("SET SESSION sql_safe_updates = 1", log)
                self.assertIn("@@hostname AS hostname", log)
                self.assertEqual(self.store.audit_records()[0]["status"], "refused")

    def test_history(self):
        self.run_sql("SELECT 1")
        self.run_sql("SELECT * FROM nope")
        items = self.call("/api/editor/history")["items"]
        self.assertEqual([i["status"] for i in items], ["error", "complete"])
        self.assertEqual(items[0]["error_code"], 1146)
        self.assertEqual(items[0]["connection"]["name"], "127.0.0.1:3306")
        self.assertEqual(self.call("/api/editor/history", action="clear")["cleared"], 2)
        self.assertEqual(self.call("/api/editor/history")["items"], [])

    def test_history_caps(self):
        for i in range(12):
            self.store.history_add({"sql": "SELECT %d" % i + " " * 200, "connection": {"profile_id": None}}, 10,
                                   100000)
        self.assertEqual(len(self.store.history_list(100)), 10)
        self.assertEqual(self.store.history_list(1)[0]["sql"].strip(), "SELECT 11")
        self.store.history_add({"sql": "x" * 1000}, 100, 1500)
        items = self.store.history_list(100)
        self.assertLessEqual(sum(len(json.dumps(i)) for i in items), 1500 + 200)
        self.assertEqual(items[0]["sql"], "x" * 1000)


    # -- Query editor UI review gate (2026-10-15) ------------------------------------------
    def test_ui_gate_decline_never_runs_after_the_policy_relaxed(self):
        """A sheet answered with Cancel after the connection became Development:
        the decline must send nothing, although Development would allow it."""
        profile = self.save_profile(type="production")
        asked = self.call("/api/editor/run", sql="INSERT INTO tt VALUES (1, 'x')")
        self.assertEqual(asked["status"], "needs_confirmation")
        self.store.save("profile", {"name": "dev", "host": "127.0.0.1", "port": 3306, "user": "app",
                                    "type": "development"}, profile["id"])
        declined = self.call("/api/editor/run", sql="INSERT INTO tt VALUES (1, 'x')", decline=True)
        self.assertEqual(declined["status"], "declined")
        self.assertIsNone(self.registry.get("tab1"), "a decline opens no session")
        records = self.store.audit_records()
        self.assertEqual(records[0]["status"], "declined")
        self.assertEqual(records[0]["statements"], ["INSERT INTO tt VALUES (1, 'x')"])
        # With a session open, a declined write that Development allows is not sent either.
        self.assertEqual(self.call("/api/editor/run", sql="SELECT 1")["status"], "complete")
        again = self.call("/api/editor/run", sql="INSERT INTO tt VALUES (2, 'y')", decline=True)
        self.assertEqual(again["status"], "declined")
        self.assertFalse(self.logged("tab1", "INSERT INTO tt VALUES (2"))
        # A decline of reads, a rollback or an open that lowers nothing sends nothing too.
        self.assertEqual(self.call("/api/editor/run", sql="SELECT 7", decline=True)["status"], "declined")
        self.assertFalse(self.logged("tab1", "SELECT 7"))
        self.assertEqual(self.call("/api/editor/rollback", decline=True)["status"], "declined")
        self.assertEqual(self.call("/api/editor/open", tab="tab2", safe_updates=False, decline=True)["status"],
                         "declined")
        self.assertIsNone(self.registry.get("tab2"))

    def test_ui_gate_the_server_chooses_the_statement_at_the_cursor(self):
        sql = "SELECT 1;\nSELECT 2;\nSELECT 3"
        result = self.call("/api/editor/run", sql=sql, cursor=sql.index("2"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual([s["text"] for s in result["run"]["statements"]], ["SELECT 2"])
        self.assertEqual(result["run"]["scope"], "statement")
        self.assertEqual(result["run"]["statements"][0]["start"], sql.index("SELECT 2"))
        self.assertTrue(self.logged("tab1", "SELECT 2"))
        self.assertFalse(self.logged("tab1", "SELECT 3"))


class RunScopeTests(unittest.TestCase):
    """What runs is decided on the server, with the session's reading of
    backslashes, the client's \\g terminator, executable comments and DELIMITER."""

    @staticmethod
    def session(backslash=True):
        return types.SimpleNamespace(sql_mode="" if backslash else "NO_BACKSLASH_ESCAPES",
                                     backslash_escapes=backslash, engine="mysql")

    def sends(self, payload, backslash=True):
        return [u["send"] for u in ed._plan_run(payload, self.session(backslash))["units"]]

    def test_cursor_uses_the_sessions_backslash_reading(self):
        sql = "SELECT 'a\\'; INSERT INTO t VALUES (1); SELECT 'c';"
        self.assertEqual(self.sends({"sql": sql, "cursor": 3}, backslash=False), ["SELECT 'a\\'"])
        self.assertEqual(self.sends({"sql": sql, "cursor": sql.index("INSERT") + 3}, backslash=False),
                         ["INSERT INTO t VALUES (1)"])

    def test_cursor_and_the_client_terminator(self):
        sql = "SELECT 1\\g SELECT 2"
        self.assertEqual(self.sends({"sql": sql, "cursor": 0}), ["SELECT 1"])
        self.assertEqual(self.sends({"sql": sql, "cursor": len(sql)}), ["SELECT 2"])

    def test_executable_comments_are_code(self):
        sql = "/*! SELECT 1 */; SELECT 2"
        self.assertEqual(self.sends({"sql": sql, "cursor": 2}), ["/*! SELECT 1 */"])

    def test_delimiter_blocks_for_the_cursor_and_a_selection(self):
        body = "CREATE PROCEDURE p() BEGIN SELECT 1; SELECT 2; END"
        sql = "SELECT 0;\nDELIMITER //\n" + body + "//\nDELIMITER ;\nCALL p();"
        self.assertEqual(self.sends({"sql": sql, "cursor": sql.index("SELECT 2")}), [body])
        start = sql.index(body)
        self.assertEqual(self.sends({"sql": sql, "selection": {"start": start, "end": start + len(body)}}), [body])
        self.assertEqual(self.sends({"sql": sql, "cursor": len(sql)}), ["CALL p()"])

    def test_a_selection_is_split_on_the_server(self):
        sql = "SELECT 1;\nUPDATE t SET a = 1 WHERE id = 2;\nSELECT 3"
        self.assertEqual(self.sends({"sql": sql, "selection": {"start": 0, "end": sql.index("SELECT 3")}}),
                         ["SELECT 1", "UPDATE t SET a = 1 WHERE id = 2"])

    def test_bad_offsets(self):
        with self.assertRaises(ValueError):
            ed._plan_run({"sql": "SELECT 1", "cursor": 99}, self.session())
        with self.assertRaises(ed.EditorError):
            ed._plan_run({"sql": "-- only a note", "cursor": 0}, self.session())


class MariaDBProcessTests(ProcessTests):
    """The same tests with the fake client in MariaDB mode: @@in_transaction,
    no Performance Schema probe, and errors written before the echo is flushed."""

    engine = "mariadb"

    def test_unknown_transaction_state(self):
        status = self.call("/api/editor/open")["session"]
        self.assertIs(status["in_transaction"], False)
        self.assertEqual(status["transaction_source"], "in_transaction")


class UiWiringTests(unittest.TestCase):
    def test_paths_and_policy_lists(self):
        from myflames import ui
        for path in ed.WRITE_PATHS:
            self.assertIn(path, ui._WRITE_PATHS)
        for path in ed.CONNECTION_PATHS:
            self.assertIn(path, ui._CONNECTION_PATHS)
            self.assertNotIn(path, ui._SERVER_VIEW_PATHS)
        result = ui.api_response("/api/editor/export", {"format": "csv", "result": {"columns": ["a"], "rows": [[None]]}},
                                 types.SimpleNamespace(store=None))
        self.assertEqual(result["content"], "a\r\n\r\n")

    def test_endpoints_are_allowlisted(self):
        import inspect
        from myflames import ui
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        for path in ed.PATHS:
            self.assertIn('"%s"' % path, source)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                              'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.workspace = types.SimpleNamespace(store=WorkspaceStore(), editor=ed.EditorSessions(reap_every=0))
        self.addCleanup(self.workspace.editor.close)
        self.connection = {'host': 'db', 'port': 3306, 'user': 'app', 'database': 'shop'}

    def cache(self, grants=(), connection=None, **extra):
        from myflames import privileges
        model = privileges.parse_grants(list(grants))
        model.update(schema_version='privileges-1.0', account='app@%', **extra)
        ticket = ed.begin_privileges_read(self.workspace, {'connection': connection or self.connection})
        ed.finish_privileges_read(self.workspace, ticket, model)
        return model

    def preview(self, sql, **extra):
        from myflames import ui
        payload = dict(connection=self.connection, tab_id='preview', sql=sql, **extra)
        # No password resolution, client, session start, or SQL is allowed,
        # even when a caller supplies confirmation flags to this read path.
        with mock.patch.object(ed, 'MySQLConnection') as conn, \
                mock.patch.object(ed.subprocess, 'Popen') as process, \
                mock.patch.object(ed.EditorSession, 'start') as start, \
                mock.patch.object(ed.EditorSession, '_send') as send, \
                mock.patch.object(ui, '_with_saved_password') as password:
            result = ui.api_response('/api/editor/preview', payload, self.workspace)
            for blocked in (conn, process, start, send, password):
                blocked.assert_not_called()
        return result

    def test_allowed_denied_unknown_and_mixed_without_io(self):
        sql = 'SET GLOBAL max_connections=100'
        self.assertEqual(self.preview(sql)['aggregate']['state'], 'unknown')
        self.cache()
        denied = self.preview(sql, confirmed=True, decline=True)
        self.assertEqual(denied['aggregate']['state'], 'denied')
        self.assertEqual(denied['statements'][0]['action_keys'], ['set_global'])
        self.assertIn('SYSTEM_VARIABLES_ADMIN', denied['aggregate']['reason'])
        self.cache(['GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO app@%'])
        self.assertEqual(self.preview(sql)['aggregate']['state'], 'allowed')
        self.assertEqual(self.preview('UPDATE shop.t SET n=1')['aggregate']['state'], 'unknown')
        self.cache(['GRANT INSERT ON shop.t TO app@%'])
        mixed = self.preview(sql + '; INSERT INTO shop.t VALUES(1)')
        self.assertEqual([s['state'] for s in mixed['statements']], ['denied', 'allowed'])
        self.assertEqual(mixed['aggregate']['state'], 'unknown')
        self.assertIn('Statement 1', mixed['aggregate']['warning'])
        self.assertEqual(self.preview(sql + '; SET PERSIST max_connections=100')['aggregate']['state'], 'denied')
        self.assertEqual(self.workspace.editor.sessions, {})
        self.assertEqual(self.workspace.store.history_list(), [])

    def test_planning_cursor_selection_delimiter_and_utf16(self):
        self.cache()
        sql = "SELECT '\U0001F600';\nSET GLOBAL max_connections=100;"
        start = ed.Offsets(sql).to_js(sql.index('SET'))
        end = ed.Offsets(sql).to_js(len(sql))
        for extra in ({'cursor': start}, {'selection': {'start': start, 'end': end}}):
            reply = self.preview(sql, **extra)
            self.assertEqual(len(reply['statements']), 1)
            self.assertEqual(reply['aggregate']['state'], 'denied')
        self.assertEqual(self.preview(sql, cursor=0)['aggregate']['state'], 'unknown')
        sql = 'DELIMITER $$\nSET GLOBAL max_connections=100$$\nSELECT 1$$'
        self.assertEqual(self.preview(sql, cursor=sql.index('SET'))['aggregate']['state'], 'denied')
        self.assertEqual(self.preview('SELECT \'unfinished')['status'], 'invalid')

    def test_identity_and_refresh_failure_cannot_reuse_denial(self):
        self.cache(connection=dict(self.connection, user='other'))
        self.assertEqual(self.preview('SET GLOBAL max_connections=100')['aggregate']['state'], 'unknown')
        self.cache()
        from myflames import accounts_api, ui
        with mock.patch.object(accounts_api, 'response', side_effect=ValueError('failed')):
            with self.assertRaises(ValueError):
                ui.api_response('/api/live/privileges', {'connection': self.connection}, self.workspace)
        self.assertEqual(self.preview('SET GLOBAL max_connections=100')['aggregate']['state'], 'unknown')

    def test_existing_privileges_read_populates_cache_and_latest_read_wins(self):
        from myflames import accounts_api, ui
        model = self.cache()
        ed.invalidate_privileges(self.workspace)
        with mock.patch.object(accounts_api, 'response', return_value=model):
            ui.api_response('/api/live/privileges', {'connection': self.connection}, self.workspace)
        self.assertEqual(self.preview('SET GLOBAL max_connections=100')['aggregate']['state'], 'denied')
        old = ed.begin_privileges_read(self.workspace, {'connection': self.connection})
        self.cache(['GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO app@%'])
        ed.finish_privileges_read(self.workspace, old, model)
        self.assertEqual(self.preview('SET GLOBAL max_connections=100')['aggregate']['state'], 'allowed')

    def test_role_changed_session_and_role_changes_inside_script(self):
        self.cache()
        payload = {'connection': self.connection}
        options, identity = ed._connection_facts(payload, self.workspace.store)[:2]
        session = ed.EditorSession('preview', identity, options)
        session.sql_mode, session.engine = '', 'mysql'
        session.privileges_changed = True
        self.workspace.editor.sessions['preview'] = session
        reply = self.preview('SET GLOBAL max_connections=100; CREATE ROLE r')
        self.assertEqual([s['state'] for s in reply['statements']], ['unknown', 'unknown'])
        self.assertIn('SET ROLE', reply['aggregate']['reason'])
        session.privileges_changed = False
        reply = self.preview('SET ROLE ALL; SET GLOBAL max_connections=100')
        self.assertEqual([s['state'] for s in reply['statements']], ['unknown', 'unknown'])
        self.assertFalse(session.privileges_changed, 'preview must not mutate the session')

    def test_read_only_policy_is_independent(self):
        self.connection['read_only'] = True
        self.cache(['GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO app@%'])
        reply = self.preview('SET GLOBAL max_connections=100')
        self.assertEqual(reply['aggregate']['state'], 'allowed')
        self.assertEqual(reply['policy']['action'], 'refuse')

    def test_no_statement_secrets_echoed(self):
        self.cache()
        reply = self.preview("CREATE USER a IDENTIFIED BY 'keep-this-secret'")
        self.assertNotIn('keep-this-secret', json.dumps(reply))
        self.assertEqual(set(reply['policy']), {'action', 'text'})

    def test_preview_is_not_a_connection_or_write_path(self):
        from myflames import ui
        self.assertNotIn('/api/editor/preview', ui._CONNECTION_PATHS)
        self.assertNotIn('/api/editor/preview', ui._WRITE_PATHS)


if __name__ == "__main__":
    unittest.main()


class AlterPreflightTests(unittest.TestCase):
    """Deterministic MySQL/MariaDB metadata replays, not a live-server gate."""

    def test_review_readers_scope_locks_and_attribute_transactions(self):
        from myflames import locks
        for engine, version in (("mysql", "8.4.11"), ("mariadb", "10.11.13")):
            for ps_on in (True, False):
                seen = []
                class Reader(object):
                    def query_lock_rows(self, sql):
                        seen.append(sql)
                        if "information_schema.TABLES" in sql:
                            return [["InnoDB", "4096", "2048"]]
                        if "information_schema.INNODB_TRX" in sql:
                            return [["1", "RUNNING", "2026-01-01", "60", None, None, None, "42",
                                     "SELECT * FROM d.t", None, "1", "1", "1", "1", "0", "REPEATABLE READ", "1"],
                                    ["2", "RUNNING", "2026-01-01", "30", None, None, None, "43"]]
                        if "setup_instruments" in sql:
                            return [["instrument", "YES"], ["global_instrumentation", "YES"]]
                        if "metadata_locks m" in sql:
                            return [["TABLE", "d", "t", "SHARED_READ", "TRANSACTION", "GRANTED", None, "100", "42"]]
                        if "performance_schema.threads WHERE" in sql or "information_schema.PROCESSLIST" in sql:
                            return [["100", "42", "u", "h", "d", "Query", "60", "executing", "SELECT * FROM d.t"]]
                        raise AssertionError(sql)
                with self.subTest(engine=engine, ps_on=ps_on):
                    result = locks.alter_table_preflight(Reader(), "d", "t", engine, version, ps_on)
                    self.assertEqual(result["approx_bytes"], 6144)
                    self.assertEqual(result["metadata_known"], ps_on)
                    self.assertEqual([t["connection_id"] for t in result["transactions"]], [42] if ps_on else [])
                    self.assertEqual(len(result["unattributed_transactions"]), 1 if ps_on else 2)
                    self.assertTrue(all(s.startswith("/*myflames:locks*/ SELECT") for s in seen))
                    if ps_on:
                        sql = next(s for s in seen if "metadata_locks m" in s)
                        self.assertIn("m.OBJECT_SCHEMA = CONVERT(X'64'", sql)
                        self.assertIn("m.OBJECT_NAME = CONVERT(X'74'", sql)
                    else:
                        self.assertTrue(any("does not prove" in s for s in result["warnings"]))

    def test_review_unknown_and_denied_preflight_never_claim_clear(self):
        from myflames import locks
        from myflames.connector import ConnectorError
        class Reader(object):
            def query_lock_rows(self, sql):
                raise ConnectorError("ERROR 1142 (42000): SELECT command denied")
        result = locks.alter_table_preflight(Reader(), "d", "t", "mysql", "8.4.0", True)
        self.assertIsNone(result["approx_bytes"])
        self.assertFalse(result["metadata_known"])
        self.assertFalse(result["transactions_known"])
        self.assertTrue(any("does not prove" in s for s in result["warnings"]))

    def test_review_routine_ddl_cannot_authorize_trailing_ddl(self):
        safety = {"type": "development", "read_only": False}
        routine = "CREATE PROCEDURE p() BEGIN SELECT CASE WHEN 1 THEN 2 ELSE 3 END; BEGIN SELECT 1; END; END"
        plan = ed._plan_run({"sql": "DELIMITER $$\n" + routine + "$$"}, None)
        self.assertEqual(ed._decision(plan, safety)["action"], "confirm")
        for tail in ("DROP DATABASE d", "CREATE PROCEDURE q() BEGIN SELECT 1; END",
                     "BEGIN NOT ATOMIC DROP DATABASE d; END"):
            plan = ed._plan_run({"sql": "DELIMITER $$\n" + routine + "; " + tail + "$$"}, None)
            self.assertEqual(ed._decision(plan, safety)["action"], "refuse")

    def test_review_timeout_override_and_wrapped_ddl_refuse(self):
        safety = {"type": "development", "read_only": False}
        for sql in ("ALTER TABLE t WAIT 100 ADD COLUMN c INT",
                    "ALTER /*+ SET_VAR(lock_wait_timeout=100) */ TABLE t ADD COLUMN c INT",
                    "DELIMITER $$\nSELECT 1; ALTER TABLE t ADD COLUMN c INT$$",
                    "CREATE TABLE a AS SELECT function(); DROP TABLE b",
                    "DELIMITER $$\nBEGIN NOT ATOMIC DROP DATABASE d; END$$",
                    "SET STATEMENT lock_wait_timeout=100 FOR ALTER TABLE t ADD COLUMN c INT"):
            with self.subTest(sql=sql):
                plan = ed._plan_run({"sql": sql}, None)
                self.assertEqual(ed._decision(plan, safety)["action"], "refuse")

    def test_review_suggestion_is_version_gated_and_never_instant_none(self):
        for engine, version in (("mysql", "8.4.0"), ("mysql", "9.7.0"), ("mariadb", "10.11.1"), ("mariadb", "13.0.0")):
            sql = "ALTER TABLE d.t ADD COLUMN c INT"
            unit = ed._alter_unit(ed._plan_run({"sql": sql}, None))
            self.assertEqual(ed._alter_suggestion(unit, {"table_engine": "InnoDB"}, engine, version),
                             sql + ", ALGORITHM=INPLACE, LOCK=NONE")
        for sql, engine, version, storage in (
                ("ALTER TABLE t ADD COLUMN c INT", "mysql", "5.7.44", "InnoDB"),
                ("ALTER TABLE t ADD COLUMN c INT", "mariadb", "10.5.0", "InnoDB"),
                ("ALTER TABLE t ADD COLUMN c INT", "unknown", "26.7.0", "InnoDB"),
                ("ALTER TABLE t ADD COLUMN c INT", "mysql", "8.4.0", "MyISAM"),
                ("ALTER TABLE t ADD COLUMN c INT", "mysql", "8.4.0", None),
                ("ALTER TABLE t ADD COLUMN c INT, ALGORITHM=INSTANT", "mysql", "8.4.0", "InnoDB"),
                ("ALTER TABLE t DROP PARTITION p", "mysql", "8.4.0", "InnoDB"),
                ("ALTER TABLE t ADD COLUMN c INT -- trailing comment", "mysql", "8.4.0", "InnoDB")):
            with self.subTest(sql=sql, engine=engine, version=version):
                unit = ed._alter_unit(ed._plan_run({"sql": sql}, None))
                self.assertIsNone(ed._alter_suggestion(unit, {"table_engine": storage}, engine, version))
