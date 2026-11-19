"""Exhaustive small-alphabet checks against independent client state machines."""
import itertools
import time
import unittest

from client_addline_oracle import client_packets, server_statement_count
from myflames import editor_session as ed
from test_editor_session_j2 import SAFETY, bound_session


SUFFIX = " , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '"
ALPHABET = "/!+M*'\n x"


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


class ClientCommentParityTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
