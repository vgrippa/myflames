"""Exhaustive small-alphabet checks against independent client state machines."""
import itertools
import sys
import time
import unittest
from unittest import mock

from client_addline_oracle import client_packets, server_statement_count, skip_over_comments_and_space
from myflames import editor_session as ed
from test_editor_session_j2 import SAFETY, bound_session


SUFFIX = " , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '"
ALPHABET = "/!+M*'\n x"
LINE_TOKENS = ("/*", "/*+ ", "/*!999999 ", "/*M!999999 ", "*/", "'", "\n", "\r\n",
               "DELIMITER ?", "delimiter //", "charset utf8mb4", "\\C utf8mb4",
               "SELECT 1", "DELETE FROM t", "?", ";")
CLIENT_VERSIONS = {"mysql": ("5.7.44", "8.0.46", "8.1.0", "8.4.0", "9.7.0"),
                   "mariadb": ("10.11.16", "11.8.9", "12.0.1")}


def generated_sql():
    # Every string through length three, not a random sample. Wrappers
    # put even one-byte fragments at comment openers and closing markers.
    for size in range(4):
        for chars in itertools.product(ALPHABET, repeat=size):
            fragment = "".join(chars)
            for prefix, suffix in (
                    ("SELECT 1 ", ""),
                    ("SELECT 1 ", " , ' \\C utf8mb4 '\n, 'z'"),
                    ("SELECT 1 /* ", "*/" + SUFFIX),
                    ("SELECT /*+ ", "*/ 1" + SUFFIX),
                    ("SELECT /*+ a\n", "*/ 1" + SUFFIX),
                    ("SELECT 1 /*! ", "*/" + SUFFIX),
                    ("SELECT 1 /*M! ", "*/" + SUFFIX)):
                yield prefix + fragment + suffix


def generated_lines():
    # Exhaustive token sequences through length three include a comment
    # opener, a physical newline, and a named command. The second envelope
    # closes it on a later line, so accepted comment-leading inputs cannot
    # escape the check just because most arbitrary fragments are unclosed.
    for size in range(4):
        for parts in itertools.product(LINE_TOKENS, repeat=size):
            fragment = "".join(parts)
            yield fragment + "\nSELECT 1"
            yield fragment + "\n*/ SELECT 1"
    # Separating the overlapping opener from the later closer requires
    # four bytes, beyond the old character generator's length-three limit.
    for opener in ("/*!40101", "/*M!100000"):
        yield "SELECT " + opener + " 1 */* " + SUFFIX


class ClientCommentParityTests(unittest.TestCase):
    def test_oracle_named_dispatch_in_unclosed_special_comments(self):
        for family, versions in CLIENT_VERSIONS.items():
            for version in versions:
                for opener in ("/*+ ", "/*!999999 "):
                    sql = opener + "\nDELIMITER ?\nDELETE FROM t?\n*/ SELECT 1"
                    with self.subTest(family=family, version=version, opener=opener):
                        packets, remaining, commands = client_packets(sql, family, client_version=version)
                        modern = family == "mysql" and version in ("8.4.0", "9.7.0")
                        self.assertEqual(commands, ["DELIMITER"] if modern else [])
                        if modern:
                            self.assertEqual(packets, ["DELETE FROM t"])
                            self.assertTrue(remaining.endswith("__oracle_end__\n"))
                        else:
                            self.assertEqual((packets, remaining), ([sql], ""))
                # Ordinary comments retain ml_comment and suppress dispatch.
                sql = "/* \nDELIMITER ?\n*/ SELECT 1"
                self.assertEqual(client_packets(sql, family, client_version=version), ([sql], "", []))
                # Named charset is disabled in --binary-mode on all clients.
                self.assertEqual(client_packets("charset utf8mb4", family, client_version=version),
                                 (["charset utf8mb4"], "", []))
        self.assertEqual(skip_over_comments_and_space("/*!999999 \n"), len("/*!999999 \n"))
        self.assertLess(skip_over_comments_and_space("/*!999999 x\n"), len("/*!999999 x\n"))
        self.assertLess(skip_over_comments_and_space("/* a * x */\n"), len("/* a * x */\n"))

    def test_oracle_crlf_binary_reader_and_delimiter_arguments(self):
        sql = "DELIMITER //\r\nSELECT 1//\r\nSELECT 2//"
        for family, versions in CLIENT_VERSIONS.items():
            for version in versions:
                for windows in (False, True):
                    with self.subTest(family=family, version=version, windows=windows):
                        packets, remaining, commands = client_packets(
                            sql, family, client_version=version, windows=windows)
                        self.assertEqual(commands, ["DELIMITER"])
                        if windows and family == "mysql":
                            self.assertEqual(packets, ["SELECT 1", "SELECT 2"])
                        else:
                            self.assertEqual(packets, ["SELECT 1"])
                            self.assertTrue(remaining.startswith("SELECT 2//\n"))

    def test_oracle_mariadb_12_closer_consumes_both_bytes(self):
        sql = "SELECT /*!40101 1 */*2"
        self.assertEqual(client_packets(sql, "mariadb", client_version="12.0.1"), ([sql], "", []))
        for family, version in (("mysql", "8.4.0"), ("mariadb", "11.8.9")):
            packets, remaining, commands = client_packets(sql, family, client_version=version)
            self.assertEqual((packets, commands), ([], []))
            self.assertTrue(remaining)

    def test_oracle_known_comment_boundary_and_command_examples(self):
        for family in ("mysql", "mariadb"):
            good = "SELECT 1 /* a */" + SUFFIX
            packets, remaining, commands = client_packets(good, family)
            self.assertEqual((packets, remaining, commands), ([good], "", []))
            bad = "SELECT 1 /* a /*/" + SUFFIX
            packets, remaining, commands = client_packets(bad, family)
            self.assertEqual(commands, ["C"])
            self.assertEqual(packets, ["SELECT 1 /* a /*/ , ' /* z */ \n, '; DELETE FROM t; -- '"])
            self.assertEqual(remaining, "")
            self.assertEqual(server_statement_count(good, family), 1)
            self.assertEqual(server_statement_count(packets[0], family), 2)
            self.assertEqual(client_packets("SELECT 1 /* x */+2", family)[0], ["SELECT 1 /* x */ +2"])
            self.assertEqual(client_packets("SELECT 1 /*! \\C utf8mb4 */ +2", family)[0],
                             ["SELECT 1 /*! */ +2"])
            self.assertEqual(client_packets("SELECT 1 /*! x\n\\C utf8mb4 */ +2", family)[0],
                             ["SELECT 1 /*! x"])
        self.assertEqual(client_packets("SELECT /*+ ' */ 1", "mysql")[0], ["SELECT /*+ ' */ 1"])
        self.assertEqual(client_packets("SELECT /*+ ' */ 1", "mariadb")[0], ["SELECT /*+ ' */ 1"])
        # MariaDB sees /*M! as executable client text but sets no ss_comment.
        self.assertEqual(client_packets("SELECT 1 /*M! \\C utf8mb4 */ +2", "mariadb")[0], ["SELECT 1 /*M!"])
        self.assertEqual(server_statement_count("SELECT 1 /*! ; SELECT 2 */", "mysql"), 2)
        self.assertEqual(server_statement_count("SELECT 1 /*M! ; SELECT 2 */", "mysql"), 1)
        self.assertEqual(server_statement_count("SELECT 1 /*M! ; SELECT 2 */", "mariadb"), 2)

    def test_exhaustive_comment_boundary_parity(self):
        started, count, accepted, failures = time.monotonic(), 0, 0, []
        sessions = [(engine, family, bound_session(engine, version, ""))
                    for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.8.9-MariaDB"))
                    for family in ("mysql", "mariadb")]
        for engine, family, session in sessions:
            session.client_engine = family
            for sql in generated_sql():
                count += 1
                try:
                    plan = ed._plan_run({"sql": sql}, session)
                except ed.EditorError:
                    continue
                if ed._decision(plan, SAFETY, session)["action"] == "refuse":
                    continue
                accepted += 1
                for unit in plan["units"]:
                    packets, remaining, commands = client_packets(unit["send"], family)
                    reviewed_count = sum(bool(part.strip()) for part in unit["code"].split(";"))
                    actual_count = sum(server_statement_count(packet, engine) for packet in packets)
                    if packets != [unit["send"]] or remaining or commands or actual_count != reviewed_count:
                        failures.append((engine, family, sql, packets, remaining, commands, reviewed_count, actual_count))
        print("Comment differential: %d inputs (%d accepted), %.3fs, %d mismatches" %
              (count, accepted, time.monotonic() - started, len(failures)), flush=True)
        for failure in failures:
            print("Comment mismatch (server, client, SQL, packets, residual, commands, reviewed/server counts): %r" %
                  (failure,), flush=True)
        self.assertEqual(count, 4 * 7 * sum(len(ALPHABET) ** size for size in range(4)))
        self.assertGreater(accepted, 1000)  # Refusing the entire corpus cannot pass.
        self.assertFalse(failures, "First mismatch: %r" % (failures[:1],))

    def test_exhaustive_line_structured_command_parity(self):
        started, count, accepted, comparisons, failures = time.monotonic(), 0, 0, 0, []
        for windows, (engine, version) in itertools.product(
                (False, True), (("mysql", "8.4.0"), ("mariadb", "11.8.9-MariaDB"))):
            for family, versions in CLIENT_VERSIONS.items():
                session = bound_session(engine, version, "")
                session.client_engine = family
                for sql in generated_lines():
                    count += 1
                    try:
                        with mock.patch.object(sys, "platform", "win32" if windows else "linux"):
                            plan = ed._plan_run({"sql": sql}, session)
                    except ed.EditorError:
                        continue
                    if ed._decision(plan, SAFETY, session)["action"] == "refuse":
                        continue
                    accepted += 1
                    expected = [unit["send"] for unit in plan["units"]]
                    for client_version in versions:
                        comparisons += 1
                        actual, residual, commands = [], [], []
                        for unit in plan["units"]:
                            packets, remaining, executed = client_packets(
                                unit["send"], family, client_version=client_version, windows=windows)
                            actual.extend(packets)
                            if remaining:
                                residual.append(remaining)
                            commands.extend(executed)
                        if actual != expected or residual or commands:
                            failures.append((windows, engine, family, client_version, sql, expected, actual,
                                             residual, commands))
        elapsed = time.monotonic() - started
        print("Line differential: %d inputs (%d accepted), %d client comparisons, %.3fs, %d mismatches" %
              (count, accepted, comparisons, elapsed, len(failures)), flush=True)
        for failure in failures[:20]:
            print("Line mismatch (Windows, server, client, version, SQL, planned, packets, residual, commands): %r" %
                  (failure,), flush=True)
        self.assertEqual(count, 8 * (2 * sum(len(LINE_TOKENS) ** size for size in range(4)) + 2))
        self.assertGreater(accepted, 1000)
        self.assertFalse(failures, "First mismatch: %r" % (failures[:1],))


if __name__ == "__main__":
    unittest.main()
