"""Exhaustive small-alphabet checks against independent client state machines."""
import itertools
import sys
import time
import unittest
from unittest import mock

from client_addline_oracle import (SPACE, client_packets, delimiter_command, server_statement_count,
                                  skip_over_comments_and_space)
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


def generated_delimiters(family):
    """Cross argument syntax, buffer boundaries and quote-adjacent framing.

    The independent get_arg transcription supplies the client's delimiter,
    including truncation. The app must refuse or recover those exact units.
    This tests original user input as well as the private send frame: merely
    replaying a wrongly merged planned unit would miss the hidden DELETE.
    """
    values = (";", "//", "??", "'a", '"a', "`a", "a'b", 'a"b', "a`b",
              "/ +", "a b", "a\tb", " +", "+ ", "\\+", "a\\b",
              "x" * 15, "x" * 16, "x" * 31, "123456789012345;")
    arguments = list(values)
    for value in values:
        arguments.extend(quote + value + quote for quote in ("'", '"', "`"))
    for argument in arguments:
        command = "DELIMITER " + argument
        unused_found, parsed = delimiter_command(command, family)
        delimiter = parsed or ";"  # A rejected command retains the old value.
        for atom in ("1 AS a", "'1'", '"1"', "`a`", "'1''2'", "1 /*a*/+2"):
            yield command + "\nSELECT " + atom + delimiter + "DELETE FROM t" + delimiter + "SELECT 2" + delimiter
    # 15/16-byte values, a quote straddling byte 255, trailing text and
    # whitespace. Literal line lengths, not arbitrarily 'long' examples.
    for length in (254, 255, 256, 257, 300):
        for argument in ("//", "'/;'", '"\'a"', "'" + "x" * 16 + "'"):
            command = "DELIMITER " + " " * (length - 10 - len(argument)) + argument
            unused_found, parsed = delimiter_command(command, family)
            delimiter = parsed or ";"
            yield command + "\nSELECT '1'" + delimiter + "DELETE FROM t" + delimiter
        command = "DELIMITER '//'" + " " * (length - len("DELIMITER '//'") - 1) + "x"
        yield command + "\nSELECT 1//SELECT 2//"


def generated_dollars():
    """Bounded tags, token bodies, identifier prefixes and physical lines."""
    for tag in ("$$", "$q$", "$Q_1$", "$1$"):
        atoms = ("'", '"', "`", "/*", "*/", "\\", ";", "\n", "x", tag)
        for size in range(3):
            for parts in itertools.product(atoms, repeat=size):
                body = "".join(parts)
                for before, after in (("SELECT ", ""), ("SELECT 1 /*!999999 ", " */"),
                                      ("SELECT [", "]"), ("SELECT id", "")):
                    yield before + tag + body + tag + after
        for prefix in ("delimiter", "DeLiMiTeR_x", " delimiter", "x"):
            for ending in ("/\n*", "-\n-", "+\n+"):
                for effect in ("INTO OUTFILE '/tmp/out'", "; DELETE FROM t;"):
                    yield ("SELECT 1 /*!999999 '/*' " + tag + " */ ' /* " + tag + " */ " + tag +
                           "\n" + prefix + tag + " */ + 1 " + ending + " /*!999999 " + tag +
                           " ' /* " + tag + " */ " + effect + " -- */")
        yield "SELECT [" + tag + " ' " + tag + "] + [/*a*/x] + [" + tag + " ' " + tag + "]"
        yield "SELECT 1 /*!999999 '/*' " + tag + " */ ' /* " + tag + " */ /*a*/x " + tag + " ' " + tag + " */"


class ClientCommentParityTests(unittest.TestCase):
    def test_oracle_dollar_quotes_follow_client_version_and_capability(self):
        sql = "SELECT $q$first\ndelimiter$q$ -\n- 1"
        changed = sql.replace(" -\n-", " --")
        for family, versions in CLIENT_VERSIONS.items():
            for version in versions:
                modern = family == "mysql" and tuple(map(int, version.split("."))) >= (8, 1, 0)
                with self.subTest(family=family, version=version):
                    self.assertEqual(client_packets(sql, family, client_version=version),
                                     ([changed], "", []))
                    # A delimiter and quote inside a dollar body stay data
                    # only in the capable MySQL clients.
                    text = "SELECT $q$' ; DELETE FROM t$q$"
                    packets, remaining, unused = client_packets(text, family, client_version=version)
                    self.assertEqual(packets, [text] if modern else [])
                    self.assertEqual(bool(remaining), not modern)
                    self.assertEqual(client_packets(text, family, client_version=version,
                                                    dollar_quote_supported=False)[0], [])
        self.assertEqual(client_packets("SELECT '$q$'", "mysql"), (["SELECT '$q$'"], "", []))
        self.assertEqual(client_packets("SELECT id$q$;SELECT 2", "mysql", delimiter=";"),
                         (["SELECT id$q$", "SELECT 2"], "", []))

    def test_oracle_quote_delimiters_and_byte_truncation(self):
        for family in CLIENT_VERSIONS:
            self.assertEqual(client_packets('DELIMITER "\'a"\nSELECT \'1\'\'aDELETE FROM t\'aSELECT \'2\' \'a',
                                            family, delimiter=";", append_delimiter=False),
                             (["SELECT '1'", "DELETE FROM t", "SELECT '2'"], "", ["DELIMITER"]))
            for size in (15, 16, 40):
                self.assertEqual(delimiter_command("DELIMITER " + "x" * size, family), (True, "x" * 15))
            # The reviewer used 244 spaces: byte 255 is already the opening
            # quote, so both clients reject the empty truncated argument.
            # With 243 spaces the retained '/ differs by client family.
            for padding in (243, 244):
                command = "DELIMITER " + " " * padding + "'/;'"
                self.assertEqual(delimiter_command(command, family),
                                 (True, "/" if family == "mysql" and padding == 243 else None))

    def test_generated_delimiter_input_and_send_parity(self):
        started, count, accepted, comparisons, failures = time.monotonic(), 0, 0, 0, []
        for engine, version in (("mysql", "8.4.0"), ("mariadb", "11.8.9")):
            for family, versions in CLIENT_VERSIONS.items():
                session = bound_session(engine, version, "")
                session.client_engine = family
                for sql in generated_delimiters(family):
                    count += 1
                    try:
                        plan = ed._plan_run({"sql": sql}, session)
                    except ed.EditorError:
                        continue
                    if ed._decision(plan, SAFETY, session)["action"] == "refuse":
                        continue
                    accepted += 1
                    expected = [unit["send"] for unit in plan["units"]]
                    for client_version in versions:
                        comparisons += 1
                        actual, residual, commands = client_packets(sql, family, delimiter=";",
                                                                   client_version=client_version,
                                                                   append_delimiter=False)
                        # Input splitting deliberately trims each unit's
                        # edge whitespace. add_line's need_space can carry
                        # a leading space into the next raw packet after a
                        # delimiter. Compare that documented input mapping,
                        # then require byte-exact equality for each private
                        # send below, without any trimming of sent text.
                        if [packet.strip(SPACE) for packet in actual] != expected or residual or commands != ["DELIMITER"]:
                            failures.append((engine, family, client_version, sql, expected, actual, residual, commands))
                        for unit in plan["units"]:
                            actual, residual, commands = client_packets(unit["send"], family,
                                                                       client_version=client_version)
                            if actual != [unit["send"]] or residual or commands:
                                failures.append((engine, family, client_version, sql, unit["send"],
                                                 actual, residual, commands))
        print("Delimiter differential: %d inputs (%d accepted), %d client comparisons, %.3fs, %d mismatches" %
              (count, accepted, comparisons, time.monotonic() - started, len(failures)), flush=True)
        for failure in failures[:20]:
            print("Delimiter mismatch (server, client, version, SQL, planned, packets, residual, commands): %r" %
                  (failure,), flush=True)
        self.assertGreater(count, 2000)
        self.assertGreater(accepted, 100)
        self.assertFalse(bool(failures), "First mismatch: %r" % (failures[:1],))

    def test_generated_dollar_quote_send_parity(self):
        started, count, accepted, comparisons, failures = time.monotonic(), 0, 0, 0, []
        for engine, version, mode in (("mysql", "8.4.0", ""), ("mariadb", "11.8.9", ""),
                                     ("mariadb", "11.8.9", "MSSQL")):
            for family, versions in CLIENT_VERSIONS.items():
                session = bound_session(engine, version, mode)
                session.client_engine = family
                for sql in generated_dollars():
                    count += 1
                    try:
                        plan = ed._plan_run({"sql": sql}, session)
                    except ed.EditorError:
                        continue
                    if ed._decision(plan, SAFETY, session)["action"] == "refuse":
                        continue
                    accepted += 1
                    for client_version in versions:
                        comparisons += 1
                        for unit in plan["units"]:
                            actual, residual, commands = client_packets(unit["send"], family,
                                                                       client_version=client_version)
                            if actual != [unit["send"]] or residual or commands:
                                failures.append((engine, mode, family, client_version, sql, unit["send"],
                                                 actual, residual, commands))
        print("Dollar differential: %d inputs (%d accepted), %d client comparisons, %.3fs, %d mismatches" %
              (count, accepted, comparisons, time.monotonic() - started, len(failures)), flush=True)
        for failure in failures[:20]:
            print("Dollar mismatch (server, mode, client, version, SQL, planned, packets, residual, commands): %r" %
                  (failure,), flush=True)
        self.assertGreater(count, 10000)
        self.assertGreater(accepted, 100)
        self.assertFalse(bool(failures), "First mismatch: %r" % (failures[:1],))

    def test_oracle_delimiter_newlines_can_form_comments_and_remove_predicates(self):
        cases = (
            ("DELETE FROM t WHERE flag =\ndelimiter -\n- 1 AND id = 5",
             "DELETE FROM t WHERE flag =\ndelimiter -- 1 AND id = 5"),
            ("SELECT 1 AS\ndelimiter/\n* ' */ ; DELETE FROM t; -- '",
             "SELECT 1 AS\ndelimiter/* ' */ ; DELETE FROM t; -- '"),
            ("SELECT a,\nDeLiMiTeR_x\nFROM t", "SELECT a,\nDeLiMiTeR_xFROM t"),
            ("SELECT 'first\ndelimiter_x' -\n- 1", "SELECT 'first\ndelimiter_x' -- 1"),
        )
        for family, versions in CLIENT_VERSIONS.items():
            for version in versions:
                for sql, expected in cases:
                    with self.subTest(family=family, version=version, sql=sql):
                        self.assertEqual(client_packets(sql, family, client_version=version),
                                         ([expected], "", []))
                for sql in ("SELECT 'a\ndelimiter_x\nb'", "SELECT a,\n delimiter_x\nFROM t",
                            "SELECT 1\ndelimiter /* c */\n+2",
                            "SELECT 1\ndelimiter -- c\n+2"):
                    # in_string protects the newline; comment branches
                    # flush the line output before the newline prefix test.
                    self.assertEqual(client_packets(sql, family, client_version=version), ([sql], "", []))

    def test_oracle_delimiter_get_arg_uses_each_family_quote_rules(self):
        for argument, mysql, maria in (("'//", "//", None), ("'a''b'tail", "a", "a'b"),
                                       ('"a""b" tail', "a", 'a"b'), ("`a``b`tail", "a", "a`b"),
                                       ("'//'ignored", "//", "//"), (r"'\+'", None, "+")):
            self.assertEqual(delimiter_command("DELIMITER " + argument, "mysql"), (True, mysql))
            self.assertEqual(delimiter_command("DELIMITER " + argument, "mariadb"), (True, maria))

    def test_oracle_hint_line_guard_is_mysql_only(self):
        sql = "SELECT /*+ # */ 'x\n\\C utf8mb4\n'"
        for family, versions in CLIENT_VERSIONS.items():
            for version in versions:
                with self.subTest(family=family, version=version):
                    commands = client_packets(sql, family, client_version=version)[2]
                    guarded = family == "mysql" and version in ("8.0.46", "9.7.0")
                    ordinary = family == "mariadb" and version != "12.0.1"
                    self.assertEqual(commands, [] if guarded or ordinary else ["C"])

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
                            # Special comments do not protect the physical
                            # newline after the column-zero DELIMITER line.
                            expected = (sql if family == "mariadb" and version != "12.0.1" and
                                        opener == "/*+ " else sql.replace("DELIMITER ?\n", "DELIMITER ?"))
                            self.assertEqual((packets, remaining), ([expected], ""))
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
        # /*M! does not set ss_comment, even in MariaDB 12.0.1. Its closer
        # still exposes the overlapping /*; only /*! and /*+ are atomic.
        sql = "SELECT /*M!100000 1 */*2"
        packets, remaining, commands = client_packets(sql, "mariadb", client_version="12.0.1")
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

    def test_client_status_probe_oracle_matrix(self):
        from myflames import dbops
        delimiters = (";",) + dbops._TARGET_DELIMITERS
        for family, versions in CLIENT_VERSIONS.items():
            actual_states = ([(False, False), (True, False)] if family == "mysql"
                             else [(False, False), (False, True), (True, False), (True, True)])
            expected_configs = ([(False, None), (True, None)] +
                                ([(True, False), (True, True)] if family == "mariadb" else []))
            for client_version in versions:
                for delimiter in delimiters:
                    for exp_backslash, exp_ansi in expected_configs:
                        sql, expected_value = dbops.client_status_probe(
                            delimiter, exp_backslash, ansi_quotes=exp_ansi, client_engine=family)
                        server_units = dbops.split_statements(
                            sql, delimiter=delimiter, backslash_escapes=exp_backslash,
                            ansi_quotes=bool(exp_ansi), engine=family, client_engine=family,
                            client_backslash_escapes=exp_backslash, client_ansi_quotes=bool(exp_ansi))
                        self.assertEqual(len(server_units), 1)
                        self.assertFalse(server_units[0]["unclosed"])
                        self.assertFalse(server_units[0]["issues"])
                        for actual_backslash, actual_ansi in actual_states:
                            with self.subTest(family=family, version=client_version, delimiter=delimiter,
                                              exp=(exp_backslash, exp_ansi), actual=(actual_backslash, actual_ansi)):
                                packets, residual, commands = client_packets(
                                    sql, family, delimiter=delimiter, client_version=client_version,
                                    backslash_escapes=actual_backslash, ansi_quotes=actual_ansi)
                                self.assertEqual(residual, "")
                                self.assertEqual(commands, [])
                                matched = (actual_backslash == exp_backslash and
                                           (exp_ansi is None or actual_ansi == exp_ansi))
                                if matched:
                                    self.assertEqual(packets, [sql])
                                    self.assertEqual(expected_value,
                                                     ("'" if exp_backslash else "\\'") + delimiter)
                                else:
                                    self.assertGreaterEqual(len(packets), 2)
                                    self.assertNotEqual(packets[0], sql)
                                    self.assertTrue(sql.startswith(packets[0]))
                                    broken = dbops.split_statements(
                                        packets[0], backslash_escapes=exp_backslash,
                                        ansi_quotes=bool(exp_ansi), engine=family)
                                    self.assertTrue(broken[0]["unclosed"], packets)
                # Server ANSI_QUOTES variants: MySQL + MariaDB/unknown client
                # (expected ansi=False) and MariaDB + unknown client (expected
                # ansi=True for MariaDB client, ignored by MySQL client).
                for delimiter in delimiters:
                    mysql_ansi_probe, _ = dbops.client_status_probe(
                        delimiter, True, ansi_quotes=False, client_engine="mariadb",
                        server_ansi_quotes=True)
                    for actual_backslash, actual_ansi in actual_states:
                        with self.subTest(case="mysql_srv_ansi", family=family, version=client_version,
                                          delimiter=delimiter, actual=(actual_backslash, actual_ansi)):
                            pkts, res, cmds = client_packets(
                                mysql_ansi_probe, family, delimiter=delimiter, client_version=client_version,
                                backslash_escapes=actual_backslash, ansi_quotes=actual_ansi)
                            self.assertEqual((res, cmds), ("", []))
                            if actual_backslash and not actual_ansi:
                                self.assertEqual(pkts, [mysql_ansi_probe])
                            else:
                                self.assertGreaterEqual(len(pkts), 2)
                                self.assertTrue(dbops.split_statements(
                                    pkts[0], backslash_escapes=True, ansi_quotes=True,
                                    engine="mysql", server_version="8.4.0")[0]["unclosed"], pkts)
                    maria_unknown_probe, _ = dbops.client_status_probe(
                        delimiter, True, ansi_quotes=True, client_engine=None,
                        server_ansi_quotes=True)
                    for actual_backslash, actual_ansi in actual_states:
                        with self.subTest(case="maria_srv_ansi_unknown_client", family=family,
                                          version=client_version, delimiter=delimiter,
                                          actual=(actual_backslash, actual_ansi)):
                            pkts, res, cmds = client_packets(
                                maria_unknown_probe, family, delimiter=delimiter, client_version=client_version,
                                backslash_escapes=actual_backslash, ansi_quotes=actual_ansi)
                            self.assertEqual((res, cmds), ("", []))
                            if actual_backslash and (family == "mysql" or actual_ansi):
                                self.assertEqual(pkts, [maria_unknown_probe])
                            else:
                                self.assertGreaterEqual(len(pkts), 2)
                                self.assertTrue(dbops.split_statements(
                                    pkts[0], backslash_escapes=True, ansi_quotes=True,
                                    engine="mariadb", server_version="11.4.10-MariaDB")[0]["unclosed"], pkts)


if __name__ == "__main__":
    unittest.main()
