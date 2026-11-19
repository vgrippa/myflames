"""G1 source-grounded SQL lexing and editor consent regressions.

Expected tokens follow mysql-server/sql/sql_lex.cc:1037-1066,1782-1812,
1931-2075 and mariadb-server/sql/sql_lex.cc:1824-1860,2401-2562. No real
connection is opened; the planner, classifier and target guards are real.
"""
import types
import unittest

from myflames import dbops, editor_session as ed, mcp_server


SERVERS = (("mysql", "8.4.11"), ("mariadb", "11.8.9-MariaDB"))
G1A = "/*! # */ '\n*/ /*!999999 SELECT 1 FROM ( */ DELETE FROM t /*!999999 ) x */ # '"
G1B = ("DELIMITER $$\nCREATE PROCEDURE p() /*!50000 COMMENT '*/' */ "
       "/*!999999 BEGIN */ SELECT '1'; DROP DATABASE prod; /*!999999 END */ -- '$$")
G2 = "/*!999999 /* */ SELECT 1 FROM ( */ DELETE FROM t /*!999999 ) x */"
G8 = "/*!50000 SELECT '*/', 'tail\\' */ ; DROP DATABASE prod; /* ' */"
OUTFILE = "SELECT 'a\\' INTO OUTFILE '/var/lib/mysql-files/out' -- '"
ANSI_ATTACK = 'SELECT 1 AS "a\\"; DELETE FROM t; -- "'
MSSQL_ATTACK = "SELECT 1 AS [a'] ; DELETE FROM t; -- '"
CHARSET_COMMENT_ATTACK = "SELECT 1 /*!999999 ' */ \\C utf8mb4 /*\n; DELETE FROM t ; /* */ /*!999999 ' */"
CHARSET_MARIA_COMMENT_ATTACK = "SELECT 1 /*M! ' */ \\C utf8mb4 /*\n; DELETE FROM t ; /* */ /*M! ' */"
CHARSET_ANSI_ATTACK = 'SELECT 1 AS "a\\" \\C utf8mb4 /*\n; DELETE FROM t ; /* */ -- "'
CHARSET_MSSQL_ATTACK = "SELECT 1 AS [a'] \\C utf8mb4 /*\n; DELETE FROM t ; /* */ -- '"
COMMENT_DELIMITER_ATTACK = "/*!40101 SELECT 1 */ DELIMITER xx /*\nxx DELETE FROM t xx */"
J1_HINT_NESTED = "SELECT /*+ /*! ' */ ' \\C utf8mb4 ' /*\n' ; DROP DATABASE prod ; /* ' */"
J1_HINT_HASH = "SELECT /*+ BKA(t) # */ 'x' AS a, '\n\\C gbk\n' AS b -- '\n;\nSELECT 1"
J1_COMMENT_DELIMITER = "/*!40101 SELECT 1 */\nDELIMITER //\nDELETE FROM t//\nSELECT 1;"
J1_ANSI_PROXY = 'SELECT 1 AS "a\\"; SELECT "second" AS x; -- "'
K1_ORDINARY_COMMENT_ATTACKS = (
    ("KA-nested!", "SELECT 1 /* x /*! y */ , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '"),
    ("KB-nested+", "SELECT 1 /* x /*+ y */ , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '"),
    ("KC-nested5", "SELECT 1 /* a /*!50000 b */ , ' /* z */ \\C gbk '\n, '; DROP DATABASE prod; -- '"),
    ("KM-nestedM!", "SELECT 1 /* x /*M! y */ , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '"),
)
K1_CONTROL = "SELECT 1 /* x y */ , ' /* z */ \\C utf8mb4 '\n, '; DELETE FROM t; -- '"
K1_SERVERS = (("mysql", "5.7.44"), ("mysql", "8.0.46"), ("mysql", "8.4.0"), ("mysql", "9.7.0"),
              ("mariadb", "10.11.16-MariaDB"), ("mariadb", "11.4.10-MariaDB"),
              ("mariadb", "11.8.9-MariaDB"))
J3_PROCEDURE = "CREATE PROCEDURE p() BEGIN SELECT 1; END"
J3_COMMENT_SCRIPTS = (
    ("cmtDELIM", "-- make the procedure\nDELIMITER //\n" + J3_PROCEDURE + "//\nDELIMITER ;\nCALL p();",
     (J3_PROCEDURE, "CALL p()")),
    ("blkDELIM", "/* setup */\nDELIMITER //\n" + J3_PROCEDURE + "//\nDELIMITER ;", (J3_PROCEDURE,)),
    ("stmtCmt", "SELECT 1;\n-- next\nDELIMITER //\n" + J3_PROCEDURE + "//", ("SELECT 1", J3_PROCEDURE)),
)
# The restriction is deliberately independent of client version. Include the
# client bug/fix release boundaries as server facts, with both client families.
J1_SERVERS = tuple(("mysql", version) for version in (
    "5.7.44", "8.0.42", "8.0.43", "8.0.46", "8.1.0", "8.4.0", "8.4.5", "8.4.6", "8.4.13",
    "9.0.0", "9.1.0", "9.2.0", "9.7.2")) + tuple(("mariadb", version + "-MariaDB") for version in (
        "10.11.16", "11.4.10", "11.8.9", "12.0.1"))
SAFETY = {"type": "production", "read_only": False, "profile_name": "prod"}


def session(engine="mysql", version="8.4.11", mode="", bound=True, wsrep=None, client_engine="mysql"):
    identity = {"connection_id": 1, "server_id": "1", "hostname": "lexer-test", "port": "3306",
                "version": version}
    if engine == "mysql":
        identity["server_uuid"] = "11111111-2222-3333-4444-555555555555"
    complete = dict(identity)
    if wsrep is not None:
        complete["wsrep_on"] = "ON" if wsrep else "OFF"
        if wsrep:
            complete["wsrep_gcomm_uuid"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    return types.SimpleNamespace(engine=engine, server_version=version, sql_mode=mode, client_engine=client_engine,
                                 backslash_escapes=mode is None or "NO_BACKSLASH_ESCAPES" not in mode,
                                 in_transaction=False, connection_id=1,
                                 server_identity=identity if bound else None,
                                 _kill_identity=complete if bound else None, _routed_startup=False)


class SQLLexerTests(unittest.TestCase):
    def code(self, sql, engine, version, escaping=True):
        # A MariaDB client frames even a /*M! body skipped by a MySQL server.
        units = dbops.split_statements(sql, escaping, delimiter="//", engine=engine, server_version=version,
                                       client_engine="mariadb")
        self.assertEqual(len(units), 1)
        return units[0]["code"]

    def test_g1_line_comment_does_not_close_executed_comment(self):
        for engine, version in SERVERS:
            with self.subTest(engine=engine):
                found = dbops.skipped_executable_comments(G1A, engine, version)
                self.assertEqual([c["opener"] for c in found], ["/*!999999", "/*!999999"])
                self.assertEqual(dbops.classify(G1A, engine=engine, server_version=version)["kind"], "risky")
                self.assertEqual(self.code(G1A, engine, version), "DELETE FROM t")

    def test_executed_quote_and_line_comments_are_sql_tokens(self):
        for engine, version in SERVERS:
            for inner in ("SELECT '*/'", 'SELECT "*/"', "SELECT `*/`", "# */ '\nSELECT 1",
                          "-- */ '\nSELECT 1", "/* plain */ SELECT 1", "/* nested /* plain */ SELECT 1"):
                sql = "/*!50000 " + inner + " */"
                with self.subTest(engine=engine, inner=inner):
                    scan = dbops.lex_sql(sql, True, engine, version)
                    self.assertEqual(scan["unclosed"], [])
                    self.assertEqual(scan["comments"][0]["end"], len(sql))
                    expected = "risky" if inner.startswith("/*") else "read"
                    self.assertEqual(dbops.classify(sql, engine=engine, server_version=version)["kind"], expected)
                    if inner.startswith("/*"):
                        with self.assertRaisesRegex(ed.EditorError, "nested"):
                            ed._plan_run({"sql": sql}, session(engine, version))
                    self.assertFalse(dbops.skipped_executable_comments(sql, engine, version))

    def test_skipped_comment_consumes_exactly_one_nested_level(self):
        for engine, version in SERVERS:
            scan = dbops.lex_sql(G2, True, engine, version)
            first = scan["comments"][0]
            self.assertEqual(G2[first["start"]:first["end"]], "/*!999999 /* */ SELECT 1 FROM ( */")
            self.assertEqual(self.code(G2, engine, version), "DELETE FROM t")
            # Not arbitrary nesting: consume_comment(0) stops at the first */.
            sql = "/*!999999 /* /* */ */ SELECT 1 */ DELETE FROM t"
            found = dbops.skipped_executable_comments(sql, engine, version)
            self.assertEqual(sql[:found[0]["end"]], "/*!999999 /* /* */ */")

    def test_version_digits_belong_to_the_engine(self):
        for opener in ("/*!", "/*M!"):
            for space in ("", " "):
                sql = opener + "100000" + space + "END*/"
                self.assertEqual(self.code(sql, "mariadb", "11.8.9-MariaDB"), "END")
        self.assertEqual(self.code("/*!100000END*/", "mysql", "9.7.0"), "0END")
        self.assertEqual(self.code("/*!100000 END*/", "mysql", "9.7.0"), "")
        self.assertEqual(self.code("/*M!100000END*/", "mysql", "9.7.0"), "")

    def test_mysql_only_and_maria_only_comments(self):
        for number in (50700, 80016, 99999):
            self.assertEqual(self.code("/*!%d SELECT 1 */" % number, "mariadb", "11.8.9-MariaDB"), "")
        self.assertEqual(self.code("/*M!50700 SELECT 1 */", "mariadb", "11.8.9-MariaDB"), "SELECT 1")
        self.assertEqual(self.code("/*!80016 SELECT 1 */", "mysql", "8.0.16"), "SELECT 1")
        self.assertEqual(self.code("/*!80016 SELECT 1 */", "mysql", "5.7.44"), "")

    def test_backticks_double_but_do_not_backslash_escape(self):
        sql = "SELECT `a\\`, `b``c`, 'it''s'"
        for engine, version in SERVERS:
            for escaping in (True, False):
                units = dbops.split_statements(sql, escaping, engine=engine, server_version=version)
                self.assertEqual(units[0]["idents"], ["a\\", "b`c"])
                self.assertEqual(units[0]["code"], "SELECT `0`, `1`, ''")
                self.assertFalse(units[0]["unclosed"])

    def test_skipped_quotes_do_not_change_server_reading(self):
        sql = "SELECT 1 /*!999999 '*/ DELETE FROM t"
        for engine, version in SERVERS:
            scan = dbops.lex_sql(sql, True, engine, version)
            self.assertIn(("code", sql.index(" DELETE"), len(sql)), scan["tokens"])
            # The client still opens a quote, so this cannot reach transport.
            units = dbops.split_statements(sql, True, engine=engine, server_version=version)
            self.assertTrue(units[0]["unclosed"])

    def test_client_delimiter_is_not_suppressed_by_server_version_gate(self):
        sql = "SELECT 1 /*!999999 ; DELETE FROM t */"
        for engine, version in SERVERS:
            units = dbops.split_statements(sql, engine=engine, server_version=version)
            self.assertEqual(len(units), 2)
            self.assertEqual([u["code"] for u in units], ["SELECT 1", ""])
            self.assertEqual([u["text"] for u in units], ["SELECT 1 /*!999999", "DELETE FROM t */"])

    def test_client_split_skipped_comment_cannot_hide_later_packet(self):
        # The client's --force path could run the DELETE fragment after
        # rejecting the earlier unterminated conditional-comment fragment.
        sql = "SELECT 1 /*!999999 ; DELETE FROM t # */\n;"
        for engine, version in SERVERS:
            with self.subTest(engine=engine):
                verdict = dbops.classify(sql, engine=engine, server_version=version)
                self.assertEqual(verdict["kind"], "risky")
                self.assertIn(dbops.REASON_COMMENT_FRAMING, verdict["reasons"])
                with self.assertRaisesRegex(ed.EditorError, "client delimiter"):
                    ed._plan_run({"sql": sql}, session(engine, version))

    def test_target_guard_unknown_mode_rejects_executed_comment_token_drift(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            dbops.target_statement(G8, delimiter="//")

    def test_unknown_mode_takes_file_write_even_when_unit_offsets_match(self):
        for engine, version in SERVERS:
            result = dbops.classify(OUTFILE, engine=engine, server_version=version)
            self.assertEqual((result["kind"], result["category"]), ("write", "file"))
            true = dbops.split_statements(OUTFILE, True, engine=engine, server_version=version)
            false = dbops.split_statements(OUTFILE, False, engine=engine, server_version=version)
            self.assertEqual([(u["start"], u["end"]) for u in true], [(u["start"], u["end"]) for u in false])
            self.assertTrue(result["ambiguous"])
            self.assertEqual(result["statements"][0]["kind"], "write")
            self.assertEqual(dbops.classify(OUTFILE, True, engine=engine, server_version=version)["kind"], "read")
            self.assertEqual(dbops.classify(OUTFILE, False, engine=engine, server_version=version)["kind"], "write")

    def test_target_guard_compares_whole_reading_inside_executable_comments(self):
        for engine, version in SERVERS:
            with self.assertRaisesRegex(ValueError, "ambiguous|closed"):
                dbops.target_statement(G8, delimiter="//", engine=engine, server_version=version)
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                dbops.target_statement(OUTFILE, engine=engine, server_version=version)
            self.assertEqual(dbops.target_statement(G8, False, "//", engine, version), G8)

    def test_unknown_version_is_not_evidence_that_a_comment_executes(self):
        for engine, version in ((None, None), ("mysql", None), ("mariadb", "custom")):
            found = dbops.skipped_executable_comments("/*!40101 SELECT 1 */", engine, version)
            self.assertEqual(len(found), 1)
            self.assertFalse(found[0]["certain"])
        attack = "/*!999999 SELECT 1 FROM ( */ /*!50000 DELETE FROM t */ /*!999999 ) x */"
        self.assertEqual(dbops.classify(attack)["kind"], "risky")
        with self.assertRaises(ValueError):
            mcp_server._read_sql(attack)

    def test_mcp_cannot_mistake_g1_for_read(self):
        for sql in (G1A, G2, "/*! # */ '\n*/ DELETE FROM t"):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                mcp_server._read_sql(sql)

    def test_mode_combinations_and_delimited_identifier_escapes(self):
        # MySQL sys_vars.cc:4708-4716; MariaDB sys_vars.cc:3999-4039.
        for engine, version in SERVERS:
            combinations = ("ANSI_QUOTES", "ANSI")
            if engine == "mariadb":
                combinations += ("ORACLE", "MSSQL", "POSTGRESQL", "DB2", "MAXDB")
            for mode in combinations:
                for suffix in ("", ",NO_BACKSLASH_ESCAPES"):
                    with self.subTest(engine=engine, mode=mode + suffix):
                        scan = dbops.lex_sql('SELECT "a""b", "c\\"', engine=engine,
                                             server_version=version, sql_mode=mode + suffix)
                        self.assertEqual([kind for kind, start, end in scan["tokens"]],
                                         ["code", "identifier", "code", "identifier"])
                        self.assertFalse(scan["unclosed"])
                        self.assertEqual(dbops.classify(ANSI_ATTACK, engine=engine, server_version=version,
                                                       sql_mode=mode + suffix)["kind"], "risky")
            self.assertEqual(dbops.classify(ANSI_ATTACK, engine=engine, server_version=version,
                                           sql_mode="")["kind"], "risky")
            self.assertEqual(dbops.classify(ANSI_ATTACK, engine=engine, server_version=version,
                                           sql_mode="", client_status_synced=True)["kind"], "read")
        unit = dbops.split_statements('SELECT [a]]b], [c\\]', engine="mariadb", sql_mode="MSSQL")[0]
        self.assertEqual(unit["idents"], ["a]b", "c\\"])
        self.assertEqual(unit["code"], "SELECT `0`, `1`")
        self.assertEqual(dbops.classify(MSSQL_ATTACK, engine="mariadb", sql_mode="MSSQL")["kind"], "risky")
        self.assertFalse(dbops.sql_mode_flags("MSSQL", "mysql")["mssql"])

    def test_unknown_engine_reads_both_six_digit_grammars(self):
        # MariaDB consumes all six digits even without a following space.
        # MySQL consumes five, leaving 0INTO or 0FOR, not the write clause.
        for clause, category in (("INTO OUTFILE '/tmp/h1'", "file"), ("FOR UPDATE", "locking_read")):
            sql = "SELECT 1 /*!100000" + clause + "*/"
            with self.subTest(clause=clause):
                verdict = dbops.classify(sql, sql_mode="")
                self.assertEqual((verdict["kind"], verdict["category"]), ("write", category))
                with self.assertRaises(ValueError):
                    mcp_server._read_sql(sql)
                with self.assertRaisesRegex(ValueError, "ambiguous"):
                    dbops.target_statement(sql, sql_mode="")

    def test_unknown_mode_agreement_and_effect_disagreement(self):
        for engine, version in SERVERS:
            for sql in ("SELECT 1", "SELECT 'it''s'", "/*!40101 SELECT 1 */"):
                with self.subTest(engine=engine, sql=sql):
                    result = dbops.classify(sql, engine=engine, server_version=version)
                    self.assertEqual(result["kind"], "read")
                    self.assertFalse(result["ambiguous"])
                    self.assertEqual(dbops.target_statement(sql, engine=engine, server_version=version), sql)
            self.assertTrue(dbops.classify(ANSI_ATTACK, engine=engine, server_version=version)["ambiguous"])
            with self.assertRaises(ValueError):
                dbops.target_statement(ANSI_ATTACK, engine=engine, server_version=version)
        self.assertTrue(dbops.classify(MSSQL_ATTACK, engine="mariadb")["ambiguous"])

    def test_client_delimiter_cannot_split_executed_comment_or_server_quote(self):
        for engine, version in SERVERS:
            for sql in ("/*!50000 SELECT 1; DELETE FROM t */", "/*! SELECT 'a;b'; SELECT 2 */"):
                with self.subTest(engine=engine, sql=sql):
                    verdict = dbops.classify(sql, engine=engine, server_version=version, sql_mode="")
                    self.assertEqual(verdict["kind"], "risky")
                    self.assertIn(dbops.REASON_EXECUTED_FRAMING, verdict["reasons"])
                    with self.assertRaisesRegex(ed.EditorError, "client delimiter"):
                        ed._plan_run({"sql": sql}, session(engine, version))
        for sql in ("SELECT 1 AS [a; DELETE FROM t; -- ]",):
            with self.assertRaisesRegex(ed.EditorError, "client delimiter"):
                ed._plan_run({"sql": sql}, session("mariadb", "11.8.9-MariaDB", "MSSQL"))

    def test_client_delimiter_can_split_comment_marker_or_version_digits(self):
        # add_line scans each character, not a parsed version header.
        for engine, version in SERVERS:
            for delimiter in ("!", "*", "40101", "01"):
                sql = "DELIMITER " + delimiter + "\n/*!40101 SELECT 1 */" + delimiter
                with self.subTest(engine=engine, delimiter=delimiter):
                    with self.assertRaisesRegex(ed.EditorError, "client delimiter"):
                        ed._plan_run({"sql": sql}, session(engine, version))
            for marker in ("/*!40101", "/*+"):
                sql = "DELIMITER /end\n" + marker + " SELECT 1 */end"
                with self.subTest(engine=engine, marker=marker):
                    with self.assertRaisesRegex(ed.EditorError, "client delimiter"):
                        ed._plan_run({"sql": sql}, session(engine, version))
            with self.assertRaisesRegex(ed.EditorError, "client delimiter"):
                ed._plan_run({"sql": "DELIMITER //\n/*!40101 SELECT 1 *///"}, session(engine, version))
            accepted = ed._plan_run({"sql": "DELIMITER //\n/*!40101 SELECT 1 */ //"}, session(engine, version))
            self.assertEqual(accepted["units"][0]["send"], "/*!40101 SELECT 1 */")


class ClientCommandLexerTests(unittest.TestCase):
    """I3: command execution precedes SQL and can discard comment openers.

    mysql-8.4.0 client/mysql.cc:2453-2464,2552-2553,2796-2815;
    mariadb-11.8.9 client/mysql.cc:2506-2518,2659-2715. Binary mode
    retains C (and MariaDB '-'), but not c/d or long-form charset.
    The editor deliberately refuses those disabled spellings as well.
    """

    def test_round_k1_ordinary_comment_attacks_refuse_and_control_remains_read(self):
        # mysql-8.4.0:client/mysql.cc:2907-2930 leaves ml_comment open
        # after the first */ when an inner /*! or /*+ set ss_comment.
        for engine, version in K1_SERVERS:
            for client in ("mysql", "mariadb"):
                bound = session(engine, version, client_engine=client)
                for label, sql in K1_ORDINARY_COMMENT_ATTACKS:
                    with self.subTest(engine=engine, version=version, client=client, label=label):
                        verdict = dbops.classify(sql, engine=engine, server_version=version,
                                                 sql_mode="", client_engine=client)
                        self.assertEqual(verdict["kind"], "risky")
                        self.assertIn("ordinary comment contains a nested", " ".join(verdict["reasons"]))
                        with self.assertRaisesRegex(ed.EditorError, "ordinary comment contains a nested"):
                            ed._plan_run({"sql": sql}, bound)
                        # The packet builder intentionally returns its generic
                        # invalid-statement message instead of lexer details.
                        with self.assertRaises(ValueError):
                            dbops.target_statement(sql, engine=engine, server_version=version,
                                                   sql_mode="", client_engine=client)
                with self.subTest(engine=engine, version=version, client=client, label="KD-control"):
                    plan = ed._plan_run({"sql": K1_CONTROL}, bound)
                    self.assertEqual([u["send"] for u in plan["units"]], [K1_CONTROL])
                    self.assertEqual(plan["verdict"]["kind"], "read")
                    self.assertEqual(ed._decision(plan, SAFETY, bound)["action"], "allow")

    def test_nested_special_openers_refuse_inside_ordinary_comments_across_lines(self):
        for client in ("mysql", "mariadb"):
            for opener in ("/*!", "/*!50000", "/*+", "/*M!", "/*M!100100"):
                # Quotes and line-comment markers do not protect text inside
                # an ordinary block comment. Newlines cannot make it safe on
                # every supported client's state machine either.
                for before, after in ((" ", " "), ("\n", " "), (" ", "\n"), ("\r\n", "\r\n"),
                                      (" '", "' "), (' "', '" '), (" # ", " "), (" -- ", " ")):
                    sql = "SELECT 1 /* outer" + before + opener + " inner" + after + "*/"
                    with self.subTest(client=client, sql=sql):
                        server = dbops.lex_sql(sql, engine="mysql", server_version="8.4.0", sql_mode="")
                        self.assertFalse(server["issues"], "the server still ends at the first */")
                        scan = dbops.lex_sql(sql, client=True, client_engine=client, client_delimiter=";")
                        self.assertTrue(any("ordinary comment contains a nested" in reason
                                            for at, reason in scan["issues"]))
                        self.assertFalse(scan["unclosed"])
                        with self.assertRaisesRegex(ed.EditorError, "ordinary comment contains a nested"):
                            ed._plan_run({"sql": sql}, session(client_engine=client))
                # Existing executable-comment and hint guards still own
                # nested openers in those contexts.
                for sql, reason in (("/*!50000 SELECT 1 " + opener + " x */ */",
                                     dbops.REASON_NESTED_CLIENT_COMMENT),
                                    ("SELECT /*+ BKA(t) " + opener + " x */ 1", dbops.REASON_HINT)):
                    scan = dbops.lex_sql(sql, client=True, client_engine=client, client_delimiter=";")
                    self.assertIn(reason, [message for at, message in scan["issues"]])

    def test_nested_opener_text_stays_data_in_line_comments_and_quoted_tokens(self):
        for engine, version in K1_SERVERS:
            for client in ("mysql", "mariadb"):
                for opener in ("/*!", "/*+", "/*M!"):
                    body = "/* outer " + opener + " inner */"
                    cases = ["SELECT 1 /* ordinary */", "SELECT 1 /* outer /* ordinary */",
                             "# " + body + "\nSELECT 1", "-- " + body + "\nSELECT 1",
                             "SELECT 1 # " + body, "SELECT 1 -- " + body]
                    cases.extend("SELECT " + quote + body + quote for quote in ("'", '"', "`"))
                    for sql in cases:
                        with self.subTest(engine=engine, version=version, client=client, sql=sql):
                            plan = ed._plan_run({"sql": sql}, session(engine, version, client_engine=client))
                            self.assertEqual(plan["verdict"]["kind"], "read")
                            self.assertEqual(ed._decision(plan, SAFETY)["action"], "allow")

    def test_named_command_message_places_ordinary_comments_on_earlier_lines(self):
        sql = "/* setup */ DELIMITER //\nSELECT 1//"
        with self.assertRaises(ed.EditorError) as raised:
            ed._plan_run({"sql": sql}, session())
        self.assertIn("ordinary comments on earlier lines", str(raised.exception))

    def test_round_j_hints_and_comment_commands_cannot_classify_as_reads(self):
        for engine, version in J1_SERVERS:
            for client in ("mysql", "mariadb"):
                for sql in (J1_HINT_NESTED, J1_HINT_HASH, J1_HINT_HASH.replace("# */", "-- */"),
                            J1_COMMENT_DELIMITER):
                    with self.subTest(engine=engine, version=version, client=client, sql=sql):
                        verdict = dbops.classify(sql, engine=engine, server_version=version,
                                                 sql_mode="", client_engine=client)
                        self.assertEqual(verdict["kind"], "risky")
                        with self.assertRaises(ValueError):
                            dbops.target_statement(sql, engine=engine, server_version=version,
                                                   sql_mode="", client_engine=client)

    def test_only_consumed_delimiter_commands_lose_the_named_command_issue(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                for label, sql, expected in J3_COMMENT_SCRIPTS:
                    with self.subTest(engine=engine, client=client, label=label):
                        units = dbops.split_statements(sql, engine=engine, server_version=version,
                                                       sql_mode="", client_engine=client)
                        self.assertEqual(tuple(unit["text"] for unit in units), expected)
                        self.assertFalse(any(unit["issues"] or unit["unclosed"] for unit in units))
                for prefix in ("-- heading\n", "# heading\n", "/* heading */\n"):
                    for command in ("charset utf8mb4", "sandbox"):
                        sql = prefix + command
                        with self.subTest(engine=engine, client=client, sql=sql):
                            with self.assertRaisesRegex(ed.EditorError, "client command"):
                                ed._plan_run({"sql": sql}, session(engine, version, client_engine=client))
                # Executed SQL stays pending; its DELIMITER is sent as text,
                # so the MySQL 8.2+ comment-skipping command lookup is unsafe.
                for opener in ("/*!40101", "/*M!100100"):
                    sql = J1_COMMENT_DELIMITER.replace("/*!40101", opener)
                    with self.subTest(engine=engine, client=client, opener=opener):
                        if opener == "/*M!100100" and client == "mysql" and engine == "mysql":
                            continue  # Only this server also ignores the consumed prefix.
                        with self.assertRaisesRegex(ed.EditorError, "client command DELIMITER"):
                            ed._plan_run({"sql": sql}, session(engine, version, client_engine=client))

    def test_round_j_ansi_status_is_independent_of_server_mode(self):
        for engine, version in J1_SERVERS:
            for mode in ("ANSI_QUOTES", "ANSI", None):
                with self.subTest(engine=engine, version=version, mode=mode):
                    readings = dbops.lexer_readings(None, engine, mode, version, "mariadb", J1_ANSI_PROXY)
                    statuses = {reading.get("client_ansi_quotes") for reading in readings
                                if reading["ansi_quotes"]}
                    self.assertEqual(statuses, {False, True})
                    verdict = dbops.classify(J1_ANSI_PROXY, engine=engine, server_version=version,
                                             sql_mode=mode, client_engine="mariadb")
                    self.assertTrue(verdict["ambiguous"])
                    with self.assertRaises(ValueError):
                        dbops.target_statement(J1_ANSI_PROXY, engine=engine, server_version=version,
                                               sql_mode=mode, client_engine="mariadb")

    def test_inline_backslash_commands_are_detected_by_the_client_pass(self):
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                for command in ("C", "c", "d", "-", "!", ".", "T"):
                    sql = "SELECT 1 \\" + command + " utf8mb4 /*\n; DELETE FROM t ; /* */"
                    with self.subTest(engine=engine, client=client, command=command):
                        scan = dbops.lex_sql(sql, engine=engine, server_version=version, sql_mode="",
                                             client=True, client_engine=client, client_delimiter=";")
                        self.assertIn((sql.index("\\"), dbops.REASON_CLIENT_COMMAND % command), scan["issues"])

    def test_client_or_server_code_cannot_hide_charset_parameter_stripping(self):
        for engine, version in SERVERS:
            cases = [(CHARSET_COMMENT_ATTACK, ""), (CHARSET_MARIA_COMMENT_ATTACK, ""),
                     (CHARSET_ANSI_ATTACK, "ANSI_QUOTES")]
            if engine == "mariadb":
                cases.append((CHARSET_MSSQL_ATTACK, "MSSQL"))
            else:
                cases.append((CHARSET_MSSQL_ATTACK, ""))
            for client in ("mysql", "mariadb"):
                for sql, bound_mode in cases:
                    for mode in (bound_mode, None):
                        with self.subTest(engine=engine, client=client, mode=mode, sql=sql):
                            if sql == CHARSET_MARIA_COMMENT_ATTACK and engine == client == "mariadb":
                                # Both parsers execute /*M!: this C stays inside
                                # the same quoted token, so no command runs.
                                self.assertFalse(any(u["issues"] for u in dbops.split_statements(
                                    sql, engine=engine, server_version=version, sql_mode=mode, client_engine=client)))
                                continue
                            if sql == CHARSET_MSSQL_ATTACK and engine == "mysql":
                                # MySQL has no bracket identifiers: the quote
                                # after a starts a string containing C. This
                                # is not an executable client-command repro.
                                self.assertFalse(any(u["issues"] for u in dbops.split_statements(
                                    sql, engine=engine, server_version=version, sql_mode=mode, client_engine=client)))
                                continue
                            verdict = dbops.classify(sql, engine=engine, server_version=version,
                                                     sql_mode=mode, client_engine=client)
                            self.assertEqual(verdict["kind"], "risky")
                            self.assertIn(dbops.REASON_CLIENT_COMMAND % "C", verdict["reasons"])
                            with self.assertRaises(ValueError):
                                dbops.target_statement(sql, engine=engine, server_version=version,
                                                       sql_mode=mode, client_engine=client)

    def test_named_commands_after_leading_comments_are_not_sql(self):
        prefixes = ("/* plain */ ", "/*!40101 x */ ", "/*!999999 ' */ ", "/*M! ' */ ",
                    "/*+ hint */ ", "/* first */ /* second */ ", "/* multi\nline */ ")
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                for prefix in prefixes:
                    for command in ("DELIMITER //", "delimiterSuffix //", "charset utf8mb4", "sandbox"):
                        sql = prefix + command
                        with self.subTest(engine=engine, client=client, sql=sql):
                            scan = dbops.lex_sql(sql, engine=engine, server_version=version, client=True,
                                                 client_engine=client, client_delimiter=";")
                            self.assertTrue(scan["issues"], "leading comments must not hide a client command")
                            for mode in ("", None):
                                self.assertEqual(dbops.classify(sql, engine=engine, server_version=version,
                                                                sql_mode=mode, client_engine=client)["kind"], "risky")
                                with self.assertRaises(ValueError):
                                    dbops.target_statement(sql, engine=engine, server_version=version,
                                                           sql_mode=mode, client_engine=client)
                for sql in (COMMENT_DELIMITER_ATTACK, "SELECT 1; /* x */ DELIMITER //",
                            "charset utf8mb4", "charset;", "/* x */ charset;", "sandbox;",
                            "DELIMITER //\ncharset//", "DELIMITER //\n/* x */ sandbox//"):
                    with self.subTest(engine=engine, client=client, sql=sql):
                        self.assertTrue(any(u["issues"] for u in dbops.split_statements(
                            sql, engine=engine, server_version=version, sql_mode="", client_engine=client)))
                        with self.assertRaises(ed.EditorError):
                            ed.plan_statements(sql, engine=engine, server_version=version, identity_bound=True,
                                               sql_mode="", client_engine=client)

    def test_command_text_in_strings_and_comments_is_ordinary_text(self):
        cases = ("SELECT '\\C utf8mb4 /*'", "SELECT '\\c \\d \\- charset delimiter'",
                 "SELECT 1 /* \\C utf8mb4 */", "SELECT 1 # \\C utf8mb4\n",
                 "SELECT 1 -- \\C utf8mb4\n", "SELECT '/* x */ DELIMITER //\ncharset utf8mb4'",
                 "SELECT CHARSET('x'), delimiter FROM t", "SET NAMES utf8mb4", "SET CHARSET utf8mb4",
                 # A column-zero delimiter prefix loses its newline in
                 # add_line; indentation keeps this identifier as SQL.
                 "CREATE TABLE t (\ncharset INT,\n delimiter INT\n)")
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                for mode in ("", "ANSI_QUOTES", "NO_BACKSLASH_ESCAPES", None):
                    for sql in cases:
                        with self.subTest(engine=engine, client=client, mode=mode, sql=sql):
                            units = ed.plan_statements(sql, engine=engine, server_version=version,
                                                       identity_bound=True, sql_mode=mode, client_engine=client)
                            self.assertEqual(len(units), 1)
                            self.assertEqual(units[0]["send"], sql.rstrip())
                            self.assertEqual(dbops.target_statement(sql, engine=engine, server_version=version,
                                                                    sql_mode=mode, client_engine=client), sql)

    def test_standalone_delimiter_lines_keep_their_existing_behavior(self):
        sql = "\nDELIMITER //\nSELECT 1; SELECT 2//\nDELIMITER ;\nSELECT '\\C';"
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                for mode in ("", None):
                    with self.subTest(engine=engine, client=client, mode=mode):
                        units = ed.plan_statements(sql, engine=engine, server_version=version,
                                                   identity_bound=True, sql_mode=mode, client_engine=client)
                        self.assertEqual([u["send"] for u in units], ["SELECT 1; SELECT 2", "SELECT '\\C'"])
                        self.assertEqual([u["delimiter"] for u in units], ["//", ";"])

    def test_comment_prefix_only_allows_delimiter_consumed_before_send(self):
        for prefix in ("/* header */\n", "/*!40101 SELECT 1 */\n", "/*!999999 ' */\n",
                       "/*M! SELECT 1 */\n", "/*+ BKA(t) */\n", "# header\n", "-- header\n",
                       "/* first */\n \t/* second */\n\n", "/*myflames:editor:tag*/ /*!40101 SELECT 1 */\n"):
            for client in ("mysql", "mariadb"):
                for command in ("DELIMITER //", "delimiterSuffix //", "charset utf8mb4", "sandbox"):
                    for initial in ("", "SELECT 0;\n", "DELIMITER //\nSELECT 0//\n"):
                        sql = initial + prefix + command + "\nDELETE FROM t//\nSELECT 1;"
                        with self.subTest(client=client, prefix=prefix, command=command, initial=initial):
                            units = dbops.split_statements(sql, sql_mode="", client_engine=client)
                            # With an unknown server, /*M! can execute even
                            # when the MySQL client treats it as a comment.
                            pending_sql = "/*!" in prefix or "/*M!" in prefix
                            if command.lower().startswith("delimiter") and not pending_sql:
                                # J3 narrows J1: this whole command line is
                                # consumed, so it cannot change client framing.
                                self.assertFalse(any(unit["issues"] for unit in units))
                                plan = ed._plan_run({"sql": sql}, session(client_engine=client, bound=False))
                                expected = (["SELECT 0"] if initial else []) + ["DELETE FROM t", "SELECT 1;"]
                                self.assertEqual([unit["send"] for unit in plan["units"]], expected)
                                continue
                            self.assertTrue(any("client command" in reason for unit in units for reason in unit["issues"]))
                            with self.assertRaises(ed.EditorError):
                                ed._plan_run({"sql": sql}, session(client_engine=client, bound=False))

    def test_unknown_mode_distinguishes_identifiers_from_strings_and_names(self):
        for engine, version in SERVERS:
            for sql in ('SELECT "x"', 'ALTER TABLE "t" ADD COLUMN c INT', 'DROP DATABASE "prod"'):
                with self.subTest(engine=engine, sql=sql):
                    self.assertTrue(dbops.classify(sql, engine=engine, server_version=version)["ambiguous"])
                    with self.assertRaisesRegex(ValueError, "ambiguous"):
                        dbops.target_statement(sql, engine=engine, server_version=version)
                    normal = dbops.split_statements(sql, engine=engine, server_version=version, sql_mode="")
                    ansi = dbops.split_statements(sql, engine=engine, server_version=version, sql_mode="ANSI_QUOTES")
                    self.assertNotEqual(dbops._mode_reading(normal), dbops._mode_reading(ansi))
            left = dbops.split_statements("ALTER TABLE `a` ADD c INT", engine=engine, server_version=version)
            right = dbops.split_statements("ALTER TABLE `b` ADD c INT", engine=engine, server_version=version)
            self.assertNotEqual(dbops._mode_reading(left), dbops._mode_reading(right),
                                "identical placeholder numbers must not erase the target name")


class VersionAndClientLexerTests(unittest.TestCase):
    def test_json_value_native_reader_starts_at_mysql_8021(self):
        # JSON_VALUE_SYM is absent in mysql-8.0.20; mysql-8.0.21 adds it
        # at sql/sql_yacc.yy:1261,10005-10010.
        sql = "SELECT JSON_VALUE('{}', '$.a')"
        for version, kind, action in (("8.0.20", "risky", "confirm"), ("8.0.21", "read", "allow")):
            for client in ("mysql", "mariadb"):
                with self.subTest(version=version, client=client):
                    bound = session("mysql", version, client_engine=client)
                    plan = ed._plan_run({"sql": sql}, bound)
                    self.assertEqual(plan["verdict"]["kind"], kind)
                    self.assertEqual(ed._decision(plan, SAFETY, bound)["action"], action)
                    if kind == "read":
                        self.assertEqual(mcp_server._read_sql(sql, engine="mysql", server_version=version), sql)
                    else:
                        with self.assertRaises(ValueError):
                            mcp_server._read_sql(sql, engine="mysql", server_version=version)
                    for call in ("db.JSON_VALUE", "`JSON_VALUE`"):
                        self.assertNotEqual(dbops.classify(sql.replace("JSON_VALUE", call), engine="mysql",
                                                           server_version=version, sql_mode="")["kind"], "read")

    def test_multiline_hint_message_names_dollar_quotes_without_comment_markers(self):
        with self.assertRaises(ed.EditorError) as raised:
            ed._plan_run({"sql": "SELECT /*+\n $tag$ */ 1"}, session(client_engine="mysql"))
        self.assertIn("dollar quotes", str(raised.exception))
        self.assertNotIn("comment markers", str(raised.exception))

    """I2: independent expectations from tagged server and client sources."""

    def test_mysql_comment_digits_change_at_810_not_800(self):
        # mysql-5.7.44 sql/sql_lex.cc:1923-1938; mysql-8.0.46:1892-1910;
        # mysql-8.1.0:1911-1929. The sixth zero is SQL on the older servers.
        sql = "SELECT /*!100000 INTO OUTFILE '/tmp/i2' */"
        for version, required, kind in (("5.7.44", 10000, "write"), ("8.0.46", 10000, "write"),
                                         ("8.1.0", 100000, "read"), ("8.4.0", 100000, "read"),
                                         ("9.7.2", 100000, "read")):
            for client in ("mysql", "mariadb"):
                with self.subTest(version=version, client=client):
                    scan = dbops.lex_sql(sql, engine="mysql", server_version=version, sql_mode="")
                    self.assertEqual(scan["comments"][0]["required"], required)
                    verdict = dbops.classify(sql, engine="mysql", server_version=version,
                                             sql_mode="", client_engine=client)
                    self.assertEqual(verdict["kind"], kind)
                    if kind == "write":
                        self.assertEqual(verdict["category"], "file")
                    unit = dbops.split_statements(sql, engine="mysql", server_version=version,
                                                  sql_mode="", client_engine=client)[0]
                    self.assertEqual(unit["code"], "SELECT 0 INTO OUTFILE ''" if kind == "write" else "SELECT")

    def test_sixth_digit_requires_whitespace_only_from_mysql_810(self):
        for version in ("5.7.44", "8.0.46", "8.1.0", "8.4.0", "9.7.2"):
            for suffix in ("X", "", "\u00a0"):
                with self.subTest(version=version, suffix=suffix):
                    scan = dbops.lex_sql("/*!100000" + suffix + "*/", engine="mysql", server_version=version)
                    self.assertEqual(scan["comments"][0]["required"], 10000)
            for space in " \t\n\r\f\v":
                scan = dbops.lex_sql("/*!100000" + space + "*/", engine="mysql", server_version=version)
                self.assertEqual(scan["comments"][0]["required"],
                                 10000 if version in ("5.7.44", "8.0.46") else 100000)

    def test_unknown_mysql_version_and_engine_take_all_comment_grammars(self):
        for engine, version in (("mysql", None), ("mysql", "custom"), (None, None), (None, "8.4.0")):
            for space in ("", " "):
                sql = "SELECT /*!100000" + space + "INTO OUTFILE '/tmp/i2' */"
                with self.subTest(engine=engine, version=version, space=space):
                    verdict = dbops.classify(sql, engine=engine, server_version=version, sql_mode="")
                    self.assertEqual((verdict["kind"], verdict["category"]), ("write", "file"))
                    self.assertEqual(verdict["statements"][0]["kind"], "write")
                    self.assertTrue(verdict["ambiguous"])
                    with self.assertRaisesRegex(ed.EditorError, "unknown|not bound"):
                        ed._plan_run({"sql": sql}, session(engine, version))

    def test_unknown_legacy_grammar_cannot_execute_an_810_or_higher_gate(self):
        # The five-digit grammar ended before 8.1. A version-99999 gate
        # cannot expose the final 9 as SQL on any server using that grammar.
        sql = "/*!999999 SET @x=1 */"
        for engine in ("mysql", None):
            with self.subTest(engine=engine):
                verdict = dbops.classify(sql, engine=engine, sql_mode="")
                self.assertEqual((verdict["kind"], verdict["category"]), ("write", "session"))
                self.assertFalse(verdict["ambiguous"])
                self.assertNotIn(dbops.AMBIGUOUS_REASON, verdict["reasons"])
        provisional = ed._plan_run({"sql": sql}, None)
        self.assertEqual(provisional["verdict"]["kind"], "write")
        self.assertEqual(provisional["units"][0]["send"], sql)

    def test_legacy_mode_aliases_are_mysql_version_specific(self):
        # mysql-5.7.44 sql/sys_vars.cc:3806-3830; mysql-8.0.46:4896-4909.
        for mode in ("ORACLE", "MSSQL", "POSTGRESQL", "DB2", "MAXDB"):
            for version in ("5.7.44", "8.0.46", "8.1.0", "8.4.0", "9.7.2"):
                with self.subTest(mode=mode, version=version):
                    flags = dbops.sql_mode_flags(mode, "mysql", version)
                    self.assertEqual(flags["ansi_quotes"], version == "5.7.44")
                    self.assertFalse(flags["mssql"], "MySQL's MSSQL alias does not enable brackets")
                    result = dbops.classify(ANSI_ATTACK, engine="mysql", server_version=version,
                                            sql_mode=mode, client_engine="mysql", client_status_synced=True)
                    self.assertEqual(result["kind"], "risky" if version == "5.7.44" else "read")
            for version in (None, "custom"):
                self.assertEqual(dbops.classify(ANSI_ATTACK, engine="mysql", server_version=version,
                                                sql_mode=mode, client_engine="mysql")["kind"], "risky")
            self.assertTrue(dbops.sql_mode_flags(mode, "mariadb", "11.4.10")["ansi_quotes"])
        for version in ("5.7.44", "8.0.46", "8.1.0", "8.4.0", "9.7.2"):
            self.assertTrue(dbops.sql_mode_flags("ANSI", "mysql", version)["ansi_quotes"])
            self.assertFalse(dbops.sql_mode_flags("TRADITIONAL", "mysql", version)["ansi_quotes"])

    def test_client_ansi_quotes_requires_an_explicit_status_reading(self):
        # mariadb-10.11.16 client/mysql.cc:2630-2634;
        # mysql-8.4.0 client/mysql.cc:2771-2785.
        sql = 'SELECT "a\\"; SELECT 2 -- "'
        for engine, version in SERVERS:
            for client in ("mysql", "mariadb"):
                with self.subTest(engine=engine, client=client):
                    for status in (False, True):
                        units = dbops.split_statements(sql, engine=engine, server_version=version,
                                                       sql_mode="ANSI_QUOTES", client_engine=client,
                                                       client_ansi_quotes=status)
                        self.assertEqual(len(units), 2 if client == "mariadb" and status else 1)
                        self.assertTrue(all(not u["unclosed"] and not u["issues"] for u in units))
                    self.assertEqual(len(dbops.split_statements(sql, engine=engine, server_version=version,
                                                                sql_mode="", client_engine=client)), 1)
        with self.assertRaisesRegex(ed.EditorError, "client family is unknown"):
            ed._plan_run({"sql": sql}, session("mariadb", "11.4.10", "ANSI_QUOTES", client_engine=None))

    def test_maria_comment_is_ordinary_only_to_mysql_client(self):
        # mysql-8.4.0 client/mysql.cc:2895-2902;
        # mariadb-10.11.16 client/mysql.cc:2788-2798.
        for sql in ("SELECT 1 /*M! ' */", "SELECT 1 /*M! ; DELETE FROM t */"):
            with self.subTest(sql=sql):
                mysql = dbops.split_statements(sql, engine="mysql", server_version="8.4.0",
                                                sql_mode="", client_engine="mysql")
                self.assertEqual(len(mysql), 1)
                self.assertEqual(mysql[0]["code"], "SELECT 1")
                self.assertFalse(mysql[0]["unclosed"] or mysql[0]["issues"])
                self.assertEqual(dbops.classify(sql, engine="mysql", server_version="8.4.0",
                                                sql_mode="", client_engine="mysql")["kind"], "read")
                for client in ("mariadb", None):
                    self.assertEqual(dbops.classify(sql, engine="mysql", server_version="8.4.0",
                                                    sql_mode="", client_engine=client)["kind"], "risky")
                    with self.assertRaises(ed.EditorError):
                        ed._plan_run({"sql": sql}, session(client_engine=client))

    def test_hint_unclosed_quotes_and_comment_markers_refuse_every_client(self):
        for body in ("'x", '"x', "`t", "/* inner", "/*! SELECT", "#", "--", "-- ", "--\t", "--\n",
                     "'x#y'", '"x/*y"', "`x-- y`", "'x\\y'"):
            for newline in ("", "\n"):
                sql = "SELECT /*+ BKA(t) " + newline + body + "*/ 1"
                for client in ("mysql", "mariadb"):
                    with self.subTest(body=body, newline=newline, client=client):
                        verdict = dbops.classify(sql, sql_mode="", client_engine=client)
                        self.assertEqual(verdict["kind"], "risky")
                        self.assertIn(dbops.REASON_HINT, verdict["reasons"])
                        with self.assertRaisesRegex(ed.EditorError, "optimizer hint"):
                            ed._plan_run({"sql": sql}, session(client_engine=client))
                        with self.assertRaises(ValueError):
                            dbops.target_statement(sql, sql_mode="", client_engine=client)

    def test_multiline_hint_dollar_quotes_refuse_mysql_framing(self):
        # mysql-8.4.0 client/mysql.cc:2741,2895-2947: ss_comment is line-local.
        for body in ("\n$tag$", "\n$$"):
            sql = "SELECT /*+ " + body + " */ 1"
            with self.subTest(body=body):
                verdict = dbops.classify(sql, engine="mysql", server_version="8.4.0",
                                         sql_mode="", client_engine="mysql")
                self.assertEqual(verdict["kind"], "risky")
                self.assertIn(dbops.REASON_MULTILINE_HINT, verdict["reasons"])
                with self.assertRaisesRegex(ed.EditorError, "multi-line optimizer hint"):
                    ed._plan_run({"sql": sql}, session(client_engine="mysql"))
                with self.assertRaises(ValueError):
                    dbops.target_statement(sql, sql_mode="", client_engine="mysql")
                # MariaDB 10.11-11.8 ml_comment and main's persistent hint
                # state both keep these characters out of the client lexer.
                self.assertEqual(ed._plan_run({"sql": sql}, session(client_engine="mariadb"))[
                    "units"][0]["send"], sql)
        for sql in ("SELECT /*+ BKA(t) */ 1", "SELECT /*+\n BKA(t)\n */ 1", "SELECT /*+ x--y */ 1"):
            for engine, version in J1_SERVERS:
                for client in ("mysql", "mariadb"):
                    self.assertEqual(ed._plan_run({"sql": sql}, session(engine, version, client_engine=client))[
                        "units"][0]["send"], sql)
        sql = "DELIMITER //\nSELECT /*+\n //\n */ 1//"
        with self.assertRaisesRegex(ed.EditorError, "multi-line optimizer hint"):
            ed._plan_run({"sql": sql}, session())

    def test_balanced_one_line_hint_quotes_keep_the_exact_statement(self):
        hints = ("SET_VAR(optimizer_switch = 'mrr_cost_based=off')", "BKA(`t1`)",
                 'BKA("t1")', "QB_NAME(`a``b`)", "SET_VAR(x='a''b')",
                 'SET_VAR(x="a""b")', "SET_VAR(x='a\"`b')", "BKA(t1) NO_BKA(t2)", "QB_NAME(qb1)")
        for engine, version in J1_SERVERS:
            for client in ("mysql", "mariadb"):
                for hint in hints:
                    sql = "SELECT /*+ " + hint + " */ 1"
                    with self.subTest(engine=engine, version=version, client=client, hint=hint):
                        plan = ed._plan_run({"sql": sql}, session(engine, version, client_engine=client))
                        self.assertEqual(plan["units"][0]["send"], sql)
                        self.assertEqual(plan["verdict"]["kind"], "read")
                        self.assertFalse(dbops.lex_sql(sql, client=True, client_engine=client)["issues"])

    def test_multiline_hint_quotes_and_delimiters_refuse_every_client(self):
        for engine, version in J1_SERVERS:
            for client in ("mysql", "mariadb"):
                for body in ("\n'x'", '\n"x"', "\n`t`", "'x'\n", "\n;", "\r\n'x'"):
                    sql = "SELECT /*+ BKA(t) " + body + " */ 1"
                    with self.subTest(engine=engine, version=version, client=client, body=body):
                        with self.assertRaisesRegex(ed.EditorError, "multi-line optimizer hint"):
                            ed._plan_run({"sql": sql}, session(engine, version, client_engine=client))
                        with self.assertRaises(ValueError):
                            dbops.target_statement(sql, engine=engine, server_version=version,
                                                   sql_mode="", client_engine=client)
                sql = "DELIMITER //\nSELECT /*+\n //\n */ 1//"
                with self.assertRaisesRegex(ed.EditorError, "multi-line optimizer hint"):
                    ed._plan_run({"sql": sql}, session(engine, version, client_engine=client))

    def test_trailing_backslash_cannot_change_reviewed_text(self):
        # mysql-8.4.0 client/mysql.cc:2776-2785 drops an unpaired end-of-line
        # backslash; MariaDB's binary-mode branch can append a NUL instead.
        for quote in ("'", '"', "`"):
            for newline in ("\n", "\r\n"):
                sql = "SELECT " + quote + "a\\" + newline + "b" + quote
                with self.subTest(quote=quote, newline=newline):
                    units = dbops.split_statements(sql, sql_mode="", client_engine="mysql")
                    self.assertIn(dbops.REASON_TRAILING_BACKSLASH, units[0]["issues"])
                    self.assertEqual(dbops.classify(sql, sql_mode="", client_engine="mysql")["kind"], "risky")
                    with self.assertRaisesRegex(ed.EditorError, "line ends with a backslash"):
                        ed._plan_run({"sql": sql}, session())
                    with self.assertRaises(ValueError):
                        dbops.target_statement(sql, sql_mode="", client_engine="mysql")
                    # A bound NBE mode permits refreshing the stale escape
                    # bit before the client reads this otherwise intact text.
                    plan = ed._plan_run({"sql": sql}, session(mode="NO_BACKSLASH_ESCAPES"))
                    self.assertEqual(ed._review_statements(plan["units"]),
                                     [ed.TAG + dbops.CLIENT_STATUS_SYNC, sql])
                    if quote == "`":
                        self.assertEqual(ed._plan_run({"sql": sql}, session(client_engine="mariadb"))[
                            "units"][0]["send"], sql)
                    if quote == '"':
                        plan = ed._plan_run({"sql": sql}, session(
                            "mariadb", "11.4.10", "ANSI_QUOTES", client_engine="mariadb"))
                        self.assertEqual(ed._review_statements(plan["units"]),
                                         [ed.TAG + dbops.CLIENT_STATUS_SYNC, sql])
        sql = "SELECT 'a\\\\\nb'"
        self.assertEqual(ed._plan_run({"sql": sql}, session())["units"][0]["send"], sql)


class EditorLexerTests(unittest.TestCase):
    def plan(self, sql, engine="mysql", version="8.4.11", mode="", **kwargs):
        return ed._plan_run({"sql": sql}, session(engine, version, mode, **kwargs))

    def test_g1_and_g2_delete_reaches_write_gate_with_bound_facts(self):
        for engine, version in SERVERS:
            for sql in (G1A, G2):
                with self.subTest(engine=engine, sql=sql):
                    if sql == G2:
                        with self.assertRaisesRegex(ed.EditorError, "nested"):
                            self.plan(sql, engine, version)
                        continue
                    plan = self.plan(sql, engine, version)
                    self.assertEqual(plan["verdict"]["kind"], "risky")
                    self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
                    self.assertEqual(plan["units"][0]["code"], "DELETE FROM t")
                    self.assertEqual(plan["units"][0]["send"], sql)
                    self.assertEqual(ed._decision(plan, dict(SAFETY, read_only=True))["action"], "refuse")

    def test_g1b_ddl_body_cannot_hide_drop(self):
        for engine, version in SERVERS:
            plan = self.plan(G1B, engine, version)
            self.assertEqual(len(ed._ddl_codes(plan["units"][0])), 2)
            self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")

    def test_g3_mariadb_end_terminates_the_routine(self):
        for opener in ("/*!", "/*M!"):
            for space in ("", " "):
                sql = ("DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; " + opener + "100000" +
                       space + "END*/ ; DROP DATABASE prod; END//")
                # Keep whitespace after the closer so this reaches the DDL
                # check on a MySQL client too. Without it, /*M! is ordinary
                # to that client and the exact-wire spacing guard refuses.
                plan = self.plan(sql, "mariadb", "11.8.9-MariaDB")
                self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")
                self.assertTrue(any("DROP DATABASE prod" in part for part in ed._ddl_codes(plan["units"][0])))

    def test_g3_skipped_mariadb_bodies_cannot_supply_blocks_or_file_writes(self):
        sql = ("DELIMITER $$\nCREATE PROCEDURE p() /*M!999999BEGIN*/ SELECT 1; "
               "DROP DATABASE prod; /*M!999999END*/$$")
        plan = self.plan(sql, "mariadb", "11.8.9-MariaDB")
        self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")
        sql = "SELECT 1 /*M!999999INTO OUTFILE '/tmp/x'*/"
        plan = self.plan(sql, "mariadb", "11.8.9-MariaDB")
        self.assertEqual(plan["units"][0]["code"], "SELECT 1")
        self.assertEqual(plan["verdict"]["kind"], "read")
        self.assertTrue(ed._needs_session_guard(plan["units"][0]))

    def test_g8b_literal_mode_resynchronizes_but_nonliteral_mode_refuses(self):
        for engine, version in SERVERS:
            sql = "DELIMITER //\nSET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'//\n" + G8 + "//"
            plan = self.plan(sql, engine, version)
            self.assertTrue(plan["units"][-1]["client_status_sync"])
            self.assertEqual(plan["units"][-1]["send"], G8)
            self.assertEqual(plan["verdict"]["kind"], "risky")
            self.assertEqual(ed._decision(plan, SAFETY)["action"], "refuse")
            with self.assertRaisesRegex(ed.EditorError, "NO_BACKSLASH_ESCAPES"):
                self.plan(sql.replace("'NO_BACKSLASH_ESCAPES'", "@mode"), engine, version)

    def test_unknown_initial_mode_marks_every_unit_unknown(self):
        for engine, version in SERVERS:
            with self.assertRaisesRegex(ed.EditorError, "quote mode is unknown"):
                self.plan(OUTFILE, engine, version, mode=None)
            plan = self.plan("SELECT 1; SELECT 'it''s'", engine, version, mode=None)
            self.assertTrue(all(u["mode_unknown"] for u in plan["units"]))
            self.assertEqual(plan["verdict"]["kind"], "read")

    def test_dump_headers_and_cross_version_options_keep_exact_sql(self):
        for engine, version in SERVERS + (("mysql", "5.7.44"), ("mysql", "8.0.16")):
            for header in (40101, 40103, 50503):
                sql = "/*!%d SET @dump = 1 */" % header
                plan = self.plan(sql, engine, version)
                self.assertEqual(plan["units"][0]["send"], sql)
                self.assertEqual(plan["verdict"]["kind"], "write")
            sql = "CREATE DATABASE d /*!80016 DEFAULT ENCRYPTION='N' */"
            plan = self.plan(sql, engine, version)
            self.assertEqual(plan["units"][0]["send"], sql)
            self.assertEqual(ed._decision(plan, SAFETY)["action"], "confirm")
            expected = "CREATE DATABASE d" if engine == "mariadb" or version.startswith("5.") else "CREATE DATABASE d DEFAULT ENCRYPTION=''"
            self.assertEqual(plan["units"][0]["code"], expected)

    def test_skip_is_a_noop_not_a_write_on_the_bound_server(self):
        plan = self.plan("/*!80016 SET @x = 1 */", "mariadb", "11.8.9-MariaDB")
        self.assertEqual(plan["verdict"]["kind"], "read")
        self.assertEqual(plan["units"][0]["send"], "/*!80016 SET @x = 1 */")

    def test_refusals_name_construct_without_sql_body(self):
        secret = "private_sql_body"
        cases = (("/*!40101 SELECT '%s' */", {"bound": False}),
                 ("/*!!999999 SELECT '%s' */", {}),
                 ("/*!40101 SELECT '%s'", {}),
                 ("/*!99997 SELECT '%s' */", {"wsrep": True}),
                 ("/*!99997 SELECT '%s' */", {}))
        for template, kwargs in cases:
            with self.subTest(template=template, kwargs=kwargs):
                with self.assertRaises(ed.EditorError) as raised:
                    self.plan(template % secret, "mariadb", "11.8.9-MariaDB", **kwargs)
                self.assertIn("executable comment", str(raised.exception))
                self.assertNotIn(secret, str(raised.exception))
        for engine, version in (("unknown", "8.4.11"), ("mysql", "custom")):
            with self.assertRaises(ed.EditorError):
                self.plan("/*!40101 SELECT 1 */", engine, version)

    def test_wsrep_marker_is_accepted_only_with_known_off(self):
        plan = self.plan("SELECT 1 /*!99997 + 2 */", "mariadb", "11.8.9-MariaDB", wsrep=False)
        self.assertEqual(plan["units"][0]["code"], "SELECT 1")
        self.assertEqual(plan["verdict"]["kind"], "read")
        # MariaDB-specific comments do not take the skipped-MySQL branch
        # where the 99997 wsrep exception lives (sql_lex.cc:2477-2500).
        for wsrep in (None, False, True):
            plan = self.plan("SELECT 1 /*M!99997 + 2 */", "mariadb", "11.8.9-MariaDB", wsrep=wsrep)
            self.assertEqual(plan["units"][0]["code"], "SELECT 1 + 2")
        bound = session("mariadb", "11.8.9-MariaDB", wsrep=False)
        editor = ed.EditorSession("wsrep-lexer", "test", {})
        for key in ("engine", "server_version", "sql_mode", "connection_id", "server_identity", "_kill_identity", "client_engine"):
            setattr(editor, key, getattr(bound, key))
        sql = "SELECT 1 /*!99997 + 2 */"
        packet, unused_lines = editor._guarded_statement(editor._write_session_guard(), sql)
        self.assertIn("OR NOT (@@SESSION.wsrep_on <=> 0)", packet)
        self.assertIn(sql, packet)
        editor._kill_identity["wsrep_on"] = "unknown"
        with self.assertRaisesRegex(ed.EditorError, "wsrep executable comment"):
            ed._plan_run({"sql": sql}, editor)

    def test_routed_or_incomplete_identity_never_authorizes_skipped_code(self):
        for engine, version in SERVERS:
            for change in ("missing_complete", "missing_field", "version_drift", "session_drift", "routed"):
                s = session(engine, version)
                if change == "missing_complete":
                    s._kill_identity = None
                elif change == "missing_field":
                    del s.server_identity["hostname"]
                    del s._kill_identity["hostname"]
                elif change == "version_drift":
                    s.server_version = "5.7.44"
                elif change == "session_drift":
                    s.connection_id = 2
                else:
                    s._routed_startup = True
                with self.subTest(engine=engine, change=change), self.assertRaises(ed.EditorError):
                    ed._plan_run({"sql": "SELECT 1 /*!80016 + 2 */"}, s)

    def test_quoted_comment_text_does_not_require_identity(self):
        plan = self.plan("SELECT '/*!99997 private */'", bound=False, mode=None)
        self.assertEqual(plan["verdict"]["kind"], "read")


if __name__ == "__main__":
    unittest.main()
