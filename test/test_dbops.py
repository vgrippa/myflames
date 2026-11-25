"""The shared write safety layer (myflames/dbops.py): classification, the
confirmation policy, privilege hints, redaction, and the audit trail.

Every test that reaches the workspace database uses a temporary HOME and
MYFLAMES_SECRET_BACKEND=vault, never ~/.myflames or the OS keychain.
"""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from myflames import dbops, ui, workspace_cli
from myflames.workspace_store import WorkspaceStore


class TempHome(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {'HOME': self.temp.name, 'USERPROFILE': self.temp.name,
                                               'MYFLAMES_SECRET_BACKEND': 'vault'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        self.store = WorkspaceStore()


def _legacy_split_statements(sql, backslash_escapes=True, delimiter=";", dollar_quotes=False,
                     engine=None, server_version=None, unknown_execute=True, wsrep_on=None,
                     ansi_quotes=False, mssql=False, sql_mode=None, comment_dialect=None, client_engine=None,
                     client_ansi_quotes=False, discarded_prefixes=None, client_backslash_escapes=None):
    """Client statement boundaries with code masked by the server's SQL lexer.

    ``text`` and offsets retain the original SQL. ``code``, ``idents`` and
    ``token_spans`` come exclusively from :func:`lex_sql`'s server reading.
    The client framing pass uses that same lexer with ``client=True``:
    conditional comments do not suppress client delimiters, regardless of
    whether the server will execute their contents. An unusable DELIMITER,
    client command or unclosed token is never a safe unit.

    DELIMITER/get_arg: mysql-server/client/mysql.cc:2661-2682,4652-4671,4897-4935;
    delimiter and line-comment recognition: :2838-2912; MariaDB :2746-2833.
    Dollar bodies use the optional newer client grammar (:2715-2746).
    If supplied, ``discarded_prefixes`` collects (start, end, delimiter)
    for consumed DELIMITER lines. The editor compares both original ranges
    and argument values before any prefix disappears from a planned unit.
    """
    # Frozen pre-fix implementation: compare every output field, not only
    # statement text, when changing the position indexes in split_statements.
    import bisect
    from myflames.dbops import (
        _usable_delimiter, lex_sql, _masked_fragment, _DELIMITER_COMMAND,
        _delimiter_argument, _CLIENT_SPACE, REASON_COMMENT_FRAMING,
        REASON_QUOTE_FRAMING, REASON_EXECUTED_FRAMING,
        REASON_NAMED_CLIENT_COMMAND, REASON_CRLF_TRANSPORT, REASON_DELIMITER)
    if not isinstance(sql, str):
        raise ValueError("Pass SQL text.")
    # add_line scans physical lines independently. A caller-supplied
    # delimiter must not cross a line or depend on CRLF translation either.
    if not _usable_delimiter(delimiter):
        raise ValueError("Pass a delimiter of 1 to 15 UTF-8 bytes starting with ASCII, without backslashes, NUL, line endings, "
                         "or leading or trailing whitespace.")
    framing = lex_sql(sql, backslash_escapes, engine, server_version, dollar_quotes, client=True,
                      client_delimiter=delimiter, sql_mode=sql_mode, ansi_quotes=ansi_quotes,
                      client_engine=client_engine, comment_dialect=comment_dialect,
                      client_ansi_quotes=client_ansi_quotes, client_backslash_escapes=client_backslash_escapes)
    scan = lex_sql(sql, backslash_escapes, engine, server_version, dollar_quotes,
                   unknown_execute=unknown_execute, wsrep_on=wsrep_on, framing=framing["tokens"],
                   ansi_quotes=ansi_quotes, mssql=mssql, sql_mode=sql_mode, comment_dialect=comment_dialect)
    out = []
    consumed_delimiters = set()
    state = {"start": 0, "pending": False, "issues": [], "unclosed": False}
    pos, token_index = 0, 0
    n = len(sql)

    def issue(reason):
        if reason not in state["issues"]:
            state["issues"].append(reason)

    def finish(end, next_start):
        start = state["start"]
        code, idents, tokens = _masked_fragment(sql, scan, start, end)
        # A skipped comment cannot mask SQL across separate COM_QUERYs.
        # In --force mode a later fragment could execute after the first
        # fragment's unclosed-comment error. Refuse this framing, including
        # for classifier callers outside the editor's packet builder.
        for boundary in (start, end):
            index = bisect.bisect_right(scan["ends"], boundary)
            if index < len(scan["tokens"]):
                kind, left, right = scan["tokens"][index]
                if kind == "comment" and left < boundary < right:
                    issue(REASON_COMMENT_FRAMING)
                if kind in ("string", "identifier") and left < boundary < right:
                    issue(REASON_QUOTE_FRAMING)
            for comment in scan["comments"]:
                if comment["executes"] is not False and comment["start"] < boundary < comment["end"]:
                    issue(REASON_EXECUTED_FRAMING)
        for at, reason in scan["issues"] + framing["issues"]:
            if start <= at < end and not (at in consumed_delimiters and
                                          reason == REASON_NAMED_CLIENT_COMMAND % "DELIMITER"):
                issue(reason)
        unclosed = state["unclosed"] or any(left < end and right > start
                                            for left, right in scan["unclosed"] + framing["unclosed"])
        if code or state["pending"] or state["issues"] or unclosed:
            raw = sql[start:end]
            lead = len(raw) - len(raw.lstrip())
            text = raw.strip()
            # MySQL's Windows batch reader changes even CRLF inside quoted
            # data. MariaDB's binary reader preserves it. Command lines and
            # whitespace between units are already outside this sent span;
            # refuse remaining changes instead of rewriting SQL literals.
            # mysql-8.4.0:client/readline.cc:75-95.
            if sys.platform == "win32" and client_engine != "mariadb" and "\r\n" in text:
                issue(REASON_CRLF_TRANSPORT)
            unused_code, unused_idents, client_tokens = _masked_fragment(sql, framing, start, end)
            out.append({"text": text, "start": start + lead, "end": start + lead + len(text),
                        "code": code, "idents": idents, "token_spans": tokens,
                        "client_token_spans": client_tokens,
                        "unclosed": bool(unclosed), "issues": list(state["issues"]), "delimiter": delimiter,
                        "next_start": next_start})
        state.update(start=next_start, pending=False, issues=[], unclosed=False)

    tokens = framing["tokens"]
    while pos < n:
        while token_index < len(tokens) and tokens[token_index][2] <= pos:
            token_index += 1
        if token_index == len(tokens):
            break
        kind, left, right = tokens[token_index]
        if kind == "delimiter_command":
            command = _DELIMITER_COMMAND.match(sql, left)
            value, line_end = _delimiter_argument(sql, command.end(), client_engine)
            # A MySQL client treats /*M! SQL */ as an ordinary comment, but
            # a MariaDB server executes it. Only discard a prefix the server
            # also ignores. Keep unsafe text in a refused unit, including
            # when this DELIMITER is the last line of the run.
            # mysql-8.4.0:client/mysql.cc:2455-2471,2895-2902;
            # mariadb-11.8.9:sql/sql_lex.cc:2416-2467.
            prefix_code, unused_idents, unused_tokens = _masked_fragment(sql, scan, state["start"], left)
            ignored = not prefix_code and not any(
                comment["executes"] is not False and comment["start"] < left and
                comment["end"] > state["start"] for comment in scan["comments"])
            if ignored:
                consumed_delimiters.add(command.start(1))
                if discarded_prefixes is not None:
                    discarded_prefixes.append((state["start"], line_end, value))
            else:
                issue("SQL before DELIMITER may run on the server. End it with the current delimiter "
                      "before changing DELIMITER.")
            # Suppress only the exact command proved safe above. Other
            # commands and problems in the prefix must still refuse.
            for at, reason in scan["issues"] + framing["issues"]:
                if state["start"] <= at < line_end and not (at in consumed_delimiters and
                                                            reason == REASON_NAMED_CLIENT_COMMAND % "DELIMITER"):
                    issue(reason)
            if not ignored:
                finish(line_end, line_end)
            if value and "\\" not in value:
                delimiter = value
            else:
                state["unclosed"] = True
                issue(REASON_DELIMITER)
            pos = line_end
            state["start"] = pos
            continue
        if kind == "delimiter":
            finish(left, right)
            pos = right
            continue
        if kind != "code":
            if kind in ("identifier", "string"):
                state["pending"] = True
            pos = right
            continue
        ch = sql[pos]
        if ch == "\\":
            following = sql[pos + 1:pos + 2]
            if following in ("g", "G"):
                finish(pos, pos + 2)
                pos += 2
                continue
            # lex_sql reports unsupported commands in both complete passes.
        if ch not in _CLIENT_SPACE:
            state["pending"] = True
        pos += 1
    finish(n, n)
    return out


def _splitter_comment_probe(size):
    head = "SELECT SLEEP(600) /* delimiter */ "
    return head + "/*!99999;" * ((size - len(head) - 3) // 9) + " */"


# The supplied T1 repro's mysqldump shape, including conditional comments,
# session settings, table data and per-table trigger DELIMITER changes.
_SPLITTER_DUMP_HEADER = """-- MySQL dump 10.13  Distrib 8.4.3, for Linux (x86_64)
--
-- Host: localhost    Database: shop
-- ------------------------------------------------------
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
"""
def _splitter_dump_table(i, triggers):
    t = """
--
-- Table structure for table `t%(i)d`
--

DROP TABLE IF EXISTS `t%(i)d`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `t%(i)d` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(64) DEFAULT NULL,
  `created` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

LOCK TABLES `t%(i)d` WRITE;
/*!40000 ALTER TABLE `t%(i)d` DISABLE KEYS */;
INSERT INTO `t%(i)d` VALUES (1,'alpha','2026-01-01 00:00:00'),(2,'beta','2026-01-02 00:00:00');
/*!40000 ALTER TABLE `t%(i)d` ENABLE KEYS */;
UNLOCK TABLES;
""" % {"i": i}
    if triggers:
        t += """/*!50003 SET @saved_sql_mode       = @@sql_mode */ ;
/*!50003 SET sql_mode              = 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES' */ ;
DELIMITER ;;
/*!50003 CREATE*/ /*!50017 DEFINER=`root`@`localhost`*/ /*!50003 TRIGGER `t%(i)d_bi` BEFORE INSERT ON `t%(i)d` FOR EACH ROW SET NEW.created = NOW() */;;
DELIMITER ;
/*!50003 SET sql_mode              = @saved_sql_mode */ ;
""" % {"i": i}
    return t

def _splitter_dump(tables):
    return _SPLITTER_DUMP_HEADER + ''.join(_splitter_dump_table(i, True) for i in range(tables))


class TestSplitterScaling(unittest.TestCase):
    def test_splitter_matches_frozen_implementation(self):
        import random

        root = Path(__file__).resolve().parent.parent
        fixtures = json.loads((root / 'apps/workspace/test/fixtures/sql-split.json').read_text(encoding='utf-8'))
        corpus = [(case['sql'], {'backslash_escapes': case.get('backslash', True)})
                  for case in fixtures['statements']]
        corpus.extend((case['sql'].replace('|', '', 1), {'backslash_escapes': case.get('backslash', True)})
                      for case in fixtures['cursors'])
        corpus.extend((sql, {}) for sql, unused_kind, unused_category, unused_verbs in CASES)
        corpus.extend((path.read_text(encoding='utf-8'), {})
                      for path in sorted((root / 'test/fixtures/dumps').glob('*.sql')))
        corpus.append((_splitter_dump(8), {}))
        # Include nested/overlapping comments, issue positions discovered at
        # comment close, consumed prefixes, invalid commands and EOF ranges.
        fragments = (
            "SELECT 'a; b', `a``;b`, \"quoted; text\";\n",
            "/*!50000 SELECT 1; SELECT 'x; y' */;\n",
            "/*!999999 SELECT 1; /*!50000 SELECT 2; */ */;\n",
            "/*M!100100 SELECT 1; */ SELECT 2;\n",
            "/*!50000 SELECT 1; /*M!999999 SELECT 2; */ SELECT 3; */;\n",
            "/*!50000 SELECT 1; /*!!50000 SELECT 2; */ SELECT 3; */;\n",
            "/*!50000 SELECT 1;\n\\! hidden\nDELIMITER //\n*/;\n",
            "/*+ SELECT ';'\nDELIMITER $$\n\\C latin1 */ SELECT 1;\n",
            "/* note */\nDELIMITER //\nSELECT 'inside;//'//\nDELIMITER ;\n",
            "/*!50000 SELECT 1 */\nDELIMITER //\nSELECT 2//\nDELIMITER ;\n",
            "DELIMITER ''\nSELECT 1;\nDELIMITER //\nSELECT 2//\n",
            "DELIMITER $$\nCREATE PROCEDURE p() BEGIN SELECT 'a;$$'; SELECT 2; END$$\nDELIMITER ;\n",
            "DELIMITER ';'';'\nSELECT 'a; b';';\nDELIMITER ;\n",
            "SELECT 1\\gSELECT 2\\G\n",
            "SELECT 'a\\'; SELECT 2; -- '\n",
            "SELECT $tag$ x; y $tag$;\n",
            "SELECT [a; b], \"a; b\";\n",
            "/*M!999999\\- enable the sandbox mode */\nSELECT 1;\n",
            "SELECT 1 --\x01 hidden;\nSELECT 2;\n",
            "/*!50000 SELECT 1; SELECT 'unterminated",
            "/* unterminated; /*!50000 SELECT 1;\n",
        )
        corpus.extend((sql, {}) for sql in fragments)
        rng = random.Random(352)
        for unused in range(40):
            corpus.append((''.join(rng.choice(fragments) for unused in range(rng.randrange(1, 6))), {}))
        modes = (
            {},
            {'engine': 'mysql', 'server_version': '5.7.44', 'client_engine': 'mysql'},
            {'engine': 'mysql', 'server_version': '8.4.3', 'client_engine': 'mysql', 'sql_mode': ''},
            {'engine': 'mysql', 'server_version': '8.4.3', 'unknown_execute': False},
            {'engine': 'mysql', 'server_version': '9.7.0', 'dollar_quotes': True},
            {'engine': 'mariadb', 'server_version': '11.4.0', 'client_engine': 'mariadb'},
            {'engine': 'mariadb', 'server_version': '11.8.0', 'sql_mode': 'ORACLE,MSSQL',
             'client_ansi_quotes': True, 'client_backslash_escapes': False},
            {'unknown_execute': False, 'ansi_quotes': True, 'mssql': True, 'dollar_quotes': True},
            {'backslash_escapes': False, 'unknown_execute': False, 'delimiter': '//'},
        )
        comparisons = 0
        for index, (sql, fixture_mode) in enumerate(corpus):
            for mode in modes:
                options = dict(fixture_mode, **mode)
                with self.subTest(case=index, mode=options):
                    expected_prefixes, actual_prefixes = [], []
                    expected = _legacy_split_statements(sql, discarded_prefixes=expected_prefixes, **options)
                    actual = dbops.split_statements(sql, discarded_prefixes=actual_prefixes, **options)
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual_prefixes, expected_prefixes)
                    comparisons += 1
        print('R352_SPLITTER_EQUIVALENCE cases=%d comparisons=%d' % (len(corpus), comparisons), flush=True)

    def test_custom_boundary_semicolon_budget_fails_closed(self):
        suffix = ("DELIMITER //\nSET GLOBAL wsrep_sst_auth = 'value-private'//\n"
                  "SELECT 'tail-private'//")
        # Count declared custom delimiters toward the same 4,096 token budget
        # as semicolons (suffix contributes 3 '//' tokens).
        raw = 'SELECT 1;\n' * (4096 - suffix.count('//')) + suffix
        self.assertTrue(dbops._secret_variable_boundaries(raw))
        self.assertIn("SELECT 'tail-private'//", dbops.redact(raw))
        raw = 'SELECT 1;\n' + raw
        with mock.patch.object(dbops, 'split_statements', side_effect=AssertionError('over-budget splitter')):
            self.assertEqual(dbops._secret_variable_boundaries(raw), [])
            clean = dbops.redact(raw)
        self.assertNotIn('value-private', clean)
        self.assertNotIn('tail-private', clean)
        self.assertIn('[redacted]', clean)
        # Custom-delimiter tokens alone (including quoted DELIMITER arguments)
        # also trip the 4,096 budget and fail closed without calling the splitter.
        for declaration in ('$$', '"$$"', "'$$'"):
            with self.subTest(declaration=declaration):
                body = ("DELIMITER " + declaration + "\n"
                        "SET GLOBAL wsrep_sst_auth = 'value-private'$$\n"
                        "SELECT 'tail-private'$$\n")
                at_limit = body + 'SELECT 1$$\n' * (4096 - body.count('$$'))
                self.assertEqual(at_limit.count('$$'), 4096)
                self.assertTrue(dbops._secret_variable_boundaries(at_limit))
                self.assertIn("SELECT 'tail-private'$$", dbops.redact(at_limit))
                over_limit = at_limit + 'SELECT 1$$\n'
                with mock.patch.object(dbops, 'split_statements',
                                       side_effect=AssertionError('over-budget splitter')):
                    self.assertEqual(dbops._secret_variable_boundaries(over_limit), [])
                    clean_custom = dbops.redact(over_limit)
                self.assertNotIn('value-private', clean_custom)
                self.assertNotIn('tail-private', clean_custom)
                self.assertIn('[redacted]', clean_custom)

    def test_custom_boundary_2mib_custom_delimiter_comment_abuse_under_one_second(self):
        # Repeating /*!99999$$ after DELIMITER $$ (unquoted or quoted) must hit
        # the 4,096 token bound before lexing so 2 MiB stays linear and under 1 s.
        for declaration in ('$$', '"$$"'):
            with self.subTest(declaration=declaration):
                code = (
                    "import time\nfrom myflames import dbops\n"
                    "head = 'DELIMITER ' + %r + '\\nSELECT SLEEP(600) /* wsrep */ '\n"
                    "unit = '/*!99999$$'\n"
                    "text = head + unit * ((2 * 1024 * 1024 - len(head) - 3) // len(unit)) + ' */'\n"
                    "assert dbops._secret_variable_boundaries(text) == []\n"
                    "began = time.perf_counter()\n"
                    "clean = dbops.redact(text)\n"
                    "elapsed = time.perf_counter() - began\n"
                    "assert clean == text\n"
                    "print(elapsed)\n" % declaration)
                try:
                    result = subprocess.run([sys.executable, '-c', code],
                        cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
                except subprocess.TimeoutExpired:
                    self.fail('2 MiB custom-delimiter comment probe exceeded the 5 s deadline')
                self.assertEqual(result.returncode, 0, result.stderr)
                elapsed = float(result.stdout.strip())
                print('R352_CUSTOM_DELIMITER_2MIB_TIMING declaration=%s elapsed=%.6fs' %
                      (declaration, elapsed), flush=True)
                self.assertLess(elapsed, 1.0)

    def test_custom_boundary_text_budget_fails_closed(self):
        # At the exact size limit the normal proof is still attempted.
        # Mock only this guard check to avoid making the test lex 2 Mi of
        # padding four times; the over-limit abuse case uses real redaction.
        limit = 2 * 1024 * 1024
        raw = '/* delimiter */' + ' ' * (limit - len('/* delimiter */'))
        with mock.patch.object(dbops, 'split_statements', return_value=[]) as splitter:
            self.assertEqual(dbops._secret_variable_boundaries(raw), [])
        self.assertEqual(splitter.call_args_list,
                         [mock.call(raw, backslash_escapes=True), mock.call(raw, backslash_escapes=False)])
        suffix = ("DELIMITER //\nSET GLOBAL wsrep_sst_auth = 'value-private'//\n"
                  "SELECT 'tail-private'//")
        raw = '/*' + 'x' * (limit + 1 - len(suffix) - 5) + '*/\n' + suffix
        self.assertEqual(len(raw), limit + 1)
        with mock.patch.object(dbops, 'split_statements', side_effect=AssertionError('over-budget splitter')):
            self.assertEqual(dbops._secret_variable_boundaries(raw), [])
            clean = dbops.redact(raw)
        self.assertNotIn('value-private', clean)
        self.assertNotIn('tail-private', clean)
        self.assertIn('[redacted]', clean)

    def _assert_timing_ratio(self, shape, operation='redact'):
        # Time inside a child process so a reintroduced quadratic path has
        # a hard deadline. Alternate sizes and take medians to reduce load
        # and warm-up noise; process startup and generation are not timed.
        code = (
            "import json, statistics, sys, time\n"
            "sys.path.insert(0, 'test')\n"
            "from test_dbops import _splitter_comment_probe, _splitter_dump\n"
            "from myflames import dbops\n"
            "shape, operation = %r, %r\n"
            "values = ([_splitter_comment_probe(size) for size in (16384, 65536)]\n"
            "          if shape == 'comments' else [_splitter_dump(size) for size in (100, 400)])\n"
            "fn = getattr(dbops, operation)\n"
            "samples = [[], []]\n"
            "for repeat in range(3):\n"
            "    for index in ((0, 1) if repeat %% 2 == 0 else (1, 0)):\n"
            "        began = time.perf_counter()\n"
            "        result = fn(values[index])\n"
            "        samples[index].append(time.perf_counter() - began)\n"
            "        if operation == 'redact':\n"
            "            assert result == values[index], 'ordinary SQL changed'\n"
            "medians = [statistics.median(group) for group in samples]\n"
            "print(json.dumps({'shape': shape, 'operation': operation,\n"
            "                  'characters': list(map(len, values)), 'samples': samples,\n"
            "                  'medians': medians, 'ratio': medians[1] / medians[0]}))\n"
        ) % (shape, operation)
        try:
            result = subprocess.run([sys.executable, '-c', code],
                cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=45, universal_newlines=True)
        except subprocess.TimeoutExpired:
            self.fail('%s %s exceeded the 45 s deadline' % (operation, shape))
        self.assertEqual(result.returncode, 0, result.stderr)
        measurement = json.loads(result.stdout)
        print('R352_SPLITTER_TIMING ' + json.dumps(measurement, sort_keys=True), flush=True)
        self.assertLess(measurement['ratio'], 6, measurement)

    def test_redact_conditional_comments_scale_below_sixfold(self):
        self._assert_timing_ratio('comments')

    def test_redact_mysqldump_scales_below_sixfold(self):
        self._assert_timing_ratio('mysqldump')

    def test_splitter_mysqldump_scales_below_sixfold(self):
        # Exercise the index optimization directly as well: redaction may
        # conservatively skip custom boundaries above its work budget.
        self._assert_timing_ratio('mysqldump', 'split_statements')


# ---- classification -----------------------------------------------------------------

# (sql, overall kind, category of the worst statement, verbs in order)
CASES = (
    # Reads.
    ('SELECT 1', 'read', 'read', ['SELECT']),
    ('select * from t where id = 1;', 'read', 'read', ['SELECT']),
    ('(SELECT 1) UNION (SELECT 2)', 'read', 'read', ['SELECT']),
    ('WITH d AS (SELECT id FROM t) SELECT * FROM d', 'read', 'read', ['WITH']),
    ('WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) SELECT * FROM c',
     'read', 'read', ['WITH']),
    ('TABLE t', 'read', 'read', ['TABLE']),
    ('VALUES ROW(1, 2)', 'read', 'read', ['VALUES']),
    ('SHOW FULL PROCESSLIST', 'read', 'read', ['SHOW']),
    ('DESC t', 'read', 'read', ['DESC']),
    ('DESCRIBE SELECT 1', 'read', 'read', ['DESCRIBE']),
    ('EXPLAIN FORMAT=JSON SELECT * FROM t', 'read', 'read', ['EXPLAIN']),
    ('EXPLAIN DELETE FROM t', 'read', 'read', ['EXPLAIN']),
    ('EXPLAIN FOR CONNECTION 42', 'read', 'read', ['EXPLAIN']),
    ("HELP 'select'", 'read', 'read', ['HELP']),
    ('USE shop', 'read', 'session', ['USE']),
    ('SELECT REPLACE(a, "x", "y"), INSERT(a, 1, 1, "z") FROM t', 'read', 'read', ['SELECT']),
    ('SELECT `where`, `delete` FROM `update`', 'read', 'read', ['SELECT']),
    ("SELECT 'DROP TABLE t; DELETE FROM u'", 'read', 'read', ['SELECT']),
    ('SELECT 1--1', 'read', 'read', ['SELECT']),
    ('SET NAMES utf8mb4', 'read', 'session', ['SET NAMES']),
    ("SET SESSION time_zone = '+00:00', max_execution_time = 1000", 'read', 'session', ['SET SESSION']),
    ('SET TRANSACTION ISOLATION LEVEL READ COMMITTED', 'read', 'session', ['SET TRANSACTION']),
    ('START TRANSACTION READ ONLY', 'read', 'transaction', ['START TRANSACTION']),
    ('BEGIN', 'read', 'transaction', ['BEGIN']),
    ('ROLLBACK', 'read', 'transaction', ['ROLLBACK']),
    ('UNLOCK TABLES', 'read', 'lock', ['UNLOCK TABLES']),
    ('XA RECOVER', 'read', 'transaction', ['XA RECOVER']),
    ("PREPARE s FROM 'SELECT 1'", 'read', 'session', ['PREPARE']),
    ('CHECKSUM TABLE t', 'read', 'read', ['CHECKSUM']),
    # Comments hide nothing and add nothing.
    ('/* DELETE FROM t */ SELECT 1', 'read', 'read', ['SELECT']),
    ('SELECT 1 -- ; DROP TABLE t', 'read', 'read', ['SELECT']),
    ('SELECT 1 # ; DROP TABLE t', 'read', 'read', ['SELECT']),
    ('SELECT /*+ MAX_EXECUTION_TIME(10) */ 1', 'read', 'read', ['SELECT']),
    # Writes.
    ('INSERT INTO t VALUES (1)', 'write', 'dml', ['INSERT']),
    ('REPLACE INTO t VALUES (1)', 'write', 'dml', ['REPLACE']),
    # UPDATE and DELETE with a WHERE are risky unless the caller passes key_columns (TestClassify).
    ('UPDATE t SET a = 1 WHERE id = 3', 'risky', 'dml', ['UPDATE']),
    ('DELETE FROM t WHERE id IN (SELECT id FROM u WHERE x = 1) LIMIT 10', 'risky', 'dml', ['DELETE']),
    ('WITH d AS (SELECT id FROM t) DELETE FROM t WHERE id IN (SELECT id FROM d)', 'risky', 'dml', ['WITH']),
    ('SELECT * FROM t WHERE id = 1 FOR UPDATE', 'write', 'locking_read', ['SELECT']),
    ('SELECT * FROM t FOR SHARE SKIP LOCKED', 'write', 'locking_read', ['SELECT']),
    ('SELECT * FROM t LOCK IN SHARE MODE', 'write', 'locking_read', ['SELECT']),
    ("SELECT * INTO OUTFILE '/tmp/t.csv' FROM t", 'write', 'file', ['SELECT']),
    ("SELECT a FROM t INTO DUMPFILE '/tmp/a'", 'write', 'file', ['SELECT']),
    ('SELECT a INTO @x FROM t LIMIT 1', 'write', 'session', ['SELECT']),
    ('SELECT @n := @n + 1 FROM t', 'write', 'session', ['SELECT']),
    ("SELECT GET_LOCK('job', 10)", 'write', 'lock', ['SELECT']),
    ('SELECT NEXTVAL(s)', 'write', 'sequence', ['SELECT']),
    ('SET @x = 1', 'write', 'session', ['SET']),
    ('SET autocommit = 0', 'write', 'session', ['SET']),
    ('SET SESSION sql_mode = ""', 'write', 'session', ['SET SESSION']),
    ('SET ROLE ALL', 'write', 'session', ['SET ROLE']),
    ('COMMIT', 'write', 'transaction', ['COMMIT']),
    ("XA START 'x'", 'write', 'transaction', ['XA START']),
    ('CREATE TEMPORARY TABLE x (a INT)', 'write', 'ddl_temporary', ['CREATE TABLE']),
    ('ANALYZE TABLE t', 'write', 'maintenance', ['ANALYZE TABLE']),
    ('CHECK TABLE t', 'write', 'maintenance', ['CHECK']),
    ('FLUSH STATUS', 'write', 'server', ['FLUSH STATUS']),
    ('ANALYZE FORMAT=JSON INSERT INTO t VALUES (1)', 'write', 'dml', ['ANALYZE']),
    ('SET SESSION transaction_read_only = OFF', 'write', 'session', ['SET SESSION']),
    ('SET TRANSACTION READ WRITE', 'write', 'session', ['SET TRANSACTION']),
    ('START TRANSACTION READ WRITE', 'write', 'transaction', ['START TRANSACTION']),
    # Risky writes.
    ('DELETE FROM t', 'risky', 'dml', ['DELETE']),
    ('DELETE FROM t LIMIT 10', 'risky', 'dml', ['DELETE']),
    ('UPDATE t SET a = (SELECT b FROM u WHERE u.id = 1)', 'risky', 'dml', ['UPDATE']),
    ('UPDATE t SET a = 1 WHERE 1 = 1', 'risky', 'dml', ['UPDATE']),
    ('DELETE FROM t WHERE TRUE', 'risky', 'dml', ['DELETE']),
    ('UPDATE `t` SET `where` = 1', 'risky', 'dml', ['UPDATE']),
    ('EXPLAIN ANALYZE DELETE FROM t', 'risky', 'dml', ['EXPLAIN ANALYZE']),
    ('TRUNCATE TABLE t', 'risky', 'ddl', ['TRUNCATE']),
    ('CREATE TABLE x (a INT)', 'risky', 'ddl', ['CREATE TABLE']),
    ('CREATE TABLE x AS SELECT * FROM t', 'risky', 'ddl', ['CREATE TABLE']),
    ('ALTER TABLE t ADD INDEX (a), ALGORITHM=INPLACE, LOCK=NONE', 'risky', 'ddl', ['ALTER TABLE']),
    ('DROP DATABASE shop', 'risky', 'ddl', ['DROP DATABASE']),
    ('RENAME TABLE a TO b', 'risky', 'ddl', ['RENAME TABLE']),
    ('OPTIMIZE TABLE t', 'risky', 'maintenance', ['OPTIMIZE']),
    ('KILL 41', 'risky', 'kill', ['KILL']),
    ('KILL CONNECTION 41', 'risky', 'kill', ['KILL']),
    ('KILL QUERY 41', 'risky', 'kill', ['KILL QUERY']),
    ('SET GLOBAL max_connections = 500', 'risky', 'global', ['SET GLOBAL']),
    ('SET @@GLOBAL.max_connections = 500', 'risky', 'global', ['SET GLOBAL']),
    ('SET PERSIST innodb_io_capacity = 2000', 'risky', 'persist', ['SET PERSIST']),
    ('SET @@persist_only.innodb_log_file_size = 1', 'risky', 'persist', ['SET PERSIST_ONLY']),
    ('RESET PERSIST innodb_io_capacity', 'risky', 'persist', ['RESET PERSIST']),
    ('SET GLOBAL TRANSACTION ISOLATION LEVEL READ COMMITTED', 'risky', 'global', ['SET GLOBAL']),
    ('SET sql_log_bin = 0', 'risky', 'replication', ['SET']),
    ('SET SESSION gtid_next = "AUTOMATIC"', 'risky', 'replication', ['SET SESSION']),
    ('SET @x = 1, GLOBAL read_only = ON', 'risky', 'global', ['SET']),
    ('START REPLICA', 'risky', 'replication', ['START REPLICA']),
    ('STOP SLAVE', 'risky', 'replication', ['STOP SLAVE']),
    ('START ALL SLAVES', 'risky', 'replication', ['START ALL']),
    ("CHANGE REPLICATION SOURCE TO SOURCE_HOST = 'h'", 'risky', 'replication', ['CHANGE REPLICATION']),
    ("CHANGE MASTER TO MASTER_HOST = 'h'", 'risky', 'replication', ['CHANGE MASTER']),
    ('RESET MASTER', 'risky', 'replication', ['RESET MASTER']),
    ("PURGE BINARY LOGS TO 'binlog.000010'", 'risky', 'replication', ['PURGE BINARY']),
    ("CLONE INSTANCE FROM 'u'@'h':3306 IDENTIFIED BY 'x'", 'risky', 'clone', ['CLONE']),
    ('GRANT SELECT ON shop.* TO app', 'risky', 'account', ['GRANT']),
    ('REVOKE ALL ON *.* FROM app', 'risky', 'account', ['REVOKE']),
    ("CREATE USER u IDENTIFIED BY 'x'", 'risky', 'account', ['CREATE USER']),
    ('DROP USER u', 'risky', 'account', ['DROP USER']),
    ("SET PASSWORD FOR u = 'x'", 'risky', 'account', ['SET PASSWORD']),
    ("LOAD DATA INFILE '/tmp/t.csv' INTO TABLE t", 'risky', 'bulk', ['LOAD DATA']),
    ('LOCK TABLES t WRITE', 'risky', 'lock', ['LOCK TABLES']),
    ('FLUSH TABLES WITH READ LOCK', 'risky', 'server', ['FLUSH TABLES']),
    ("XA COMMIT 'x'", 'risky', 'transaction', ['XA COMMIT']),
    ('CALL refresh_totals()', 'risky', 'unknown', ['CALL']),
    ('EXECUTE s', 'risky', 'unknown', ['EXECUTE']),
    ("EXECUTE IMMEDIATE 'DELETE FROM t'", 'risky', 'unknown', ['EXECUTE']),
    ('SHUTDOWN', 'risky', 'server', ['SHUTDOWN']),
    ('DO SLEEP(1)', 'risky', 'unknown', ['DO']),
    ('DO my_db.cleanup()', 'risky', 'unknown', ['DO']),
    ('ANALYZE FORMAT=JSON DELETE FROM t WHERE id = 1', 'risky', 'dml', ['ANALYZE']),
    ('SET skip_replication = 1', 'risky', 'replication', ['SET']),
    ('SET SESSION wsrep_on = OFF', 'risky', 'replication', ['SET SESSION']),
    ('FROBNICATE t', 'risky', 'unknown', ['FROBNICATE']),
    ("SELECT 'unclosed", 'risky', 'unknown', ['SELECT']),
    ('SET STATEMENT max_statement_time = 1 FOR DELETE FROM t', 'risky', 'dml', ['SET STATEMENT']),
    # Executable comments are code.
    ('/*!50000 DROP TABLE t */', 'risky', 'ddl', ['DROP TABLE']),
    ('/*!DROP TABLE t*/', 'risky', 'ddl', ['DROP TABLE']),
    ('/*!100100 DELETE FROM t */', 'risky', 'dml', ['DELETE']),
    ('/*M!100100 DELETE FROM t */', 'risky', 'dml', ['DELETE']),
    ('SELECT 1 /*!50000 , GET_LOCK("x", 1) */', 'write', 'lock', ['SELECT']),
    # Several statements: the worst counts.
    ('SELECT 1; DROP TABLE t', 'risky', 'ddl', ['SELECT', 'DROP TABLE']),
    ('SELECT 1 # x\n; DELETE FROM t', 'risky', 'dml', ['SELECT', 'DELETE']),
    ('SELECT 1 /*!50000 ; DROP TABLE t */', 'risky', 'unknown', ['SELECT', 'DROP TABLE']),
    ("PREPARE s FROM 'DROP TABLE t'; EXECUTE s", 'risky', 'unknown', ['PREPARE', 'EXECUTE']),
    ('INSERT INTO t VALUES (1); SELECT 1', 'write', 'dml', ['INSERT', 'SELECT']),
    ('DELIMITER //\nCREATE PROCEDURE p() BEGIN UPDATE t SET a = 1; END//\nDELIMITER ;\nCALL p();',
     'risky', 'ddl', ['CREATE PROCEDURE', 'CALL']),
    ('', 'read', 'read', []),
    ('-- only a comment\n/* and another */', 'read', 'read', []),
)


class TestSelectFunctionSafety(unittest.TestCase):
    def test_administrative_and_unknown_calls_require_review_in_both_types(self):
        # MySQL administrative UDFs and either engine's stored functions can
        # perform work from SELECT. No connection needs opening to gate them.
        queries = (
            'SELECT group_replication_switch_to_multi_primary_mode()',
            'SELECT asynchronous_connection_failover_reset()',
            "SELECT keyring_key_generate('key', 'AES', 32)",
            "SELECT service_get_write_locks('namespace', 0, 'lock')",
            'SELECT mutate_rows()', 'SELECT mutar_ação()', 'SELECT app.mutate_rows()',
            'SELECT app.COUNT(1)', 'SELECT `app`.`mutate_rows`()',
            'SELECT `COUNT`(1)', 'SELECT "app"."mutate_rows"()',
            'SELECT "mutate_rows"()', 'SELECT mutate_rows /* comment */ ()',
            'SELECT COALESCE(mutate_rows(), 0)',
            'WITH c AS (SELECT mutate_rows()) SELECT * FROM c',
            'SELECT 1 /*!50000 , mutate_rows() */',
            'SELECT 1 /*M! , mutate_rows() */',
            'SELECT JSON_TABLE(doc, "$[*]" COLUMNS (n INT PATH "$")) FROM t',
        )
        for sql in queries:
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual(verdict['kind'], 'risky')
                for connection_type in ('production', 'development'):
                    decision = dbops.decide(verdict, connection_type)
                    self.assertEqual(decision['action'], 'confirm')
                    self.assertEqual(decision['tone'], 'danger')
                    self.assertEqual(dbops.decide(verdict, connection_type, True)['action'], 'refuse')

    def test_waits_and_uuid_generators_are_reads(self):
        # These native functions exist on both supported engines. Duration and
        # nondeterminism do not make a SELECT a write; nested writes still gate.
        for sql in ('SELECT SLEEP(30)', 'SELECT SLEEP(0)', 'SELECT SLEEP(NULL)',
                    'SELECT UUID(), UUID_SHORT()', 'SELECT sleep /* delay */ (0)',
                    'WITH c AS (SELECT UUID()) SELECT * FROM c'):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual(verdict['kind'], 'read')
                for connection_type in ('production', 'development'):
                    for read_only in (False, True):
                        self.assertEqual(dbops.decide(verdict, connection_type, read_only)['action'], 'allow')
        for sql in ("SELECT BENCHMARK(100, ABS(1))", 'SELECT SLEEP(mutate_rows())',
                    'SELECT app.UUID()', 'SELECT `SLEEP`(1)',
                    "SELECT UUID(), GET_LOCK('n',0)", "SELECT UUID_SHORT(), RELEASE_LOCK('n')"):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertNotEqual(verdict['kind'], 'read')
                self.assertEqual(dbops.decide(verdict, 'production')['action'], 'confirm')

    def test_builtin_confirmation_reasons_describe_the_actual_effect(self):
        cases = (
            ('SELECT BENCHMARK(100, ABS(1))', 'risky', 'unknown',
             'BENCHMARK runs an expression many times and can load the server.'),
            ("SELECT GET_LOCK('n', 0)", 'write', 'lock',
             'This built-in takes or releases a named user lock.'),
            ("SELECT RELEASE_LOCK('n')", 'write', 'lock',
             'This built-in takes or releases a named user lock.'),
            ('SELECT RELEASE_ALL_LOCKS()', 'write', 'lock',
             'This built-in takes or releases a named user lock.'),
        )
        for sql, kind, category, reason in cases:
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual((verdict['kind'], verdict['category']), (kind, category))
                self.assertEqual(verdict['reasons'], [reason])
        for sql in ('SELECT app.BENCHMARK(1, 2)', 'SELECT `BENCHMARK`(1, 2)',
                    'SELECT mutate_rows()', 'SELECT BENCHMARK(1, mutate_rows())'):
            with self.subTest(sql=sql):
                self.assertTrue(any('not a verified native reader' in reason
                                    for reason in dbops.classify(sql)['reasons']))

    def test_common_native_readers_and_sql_parentheses_remain_reads(self):
        queries = (
            'SELECT COUNT(*), COALESCE(SUM(x),0), AVG(x), MIN(x), MAX(x) FROM t',
            'SELECT CONCAT(LOWER(name), UPPER(name)), LENGTH(name), ABS(n), ROUND(n,2) FROM t',
            'SELECT ABS /* a native builder accepts spaces */ (1)',
            "SELECT JSON_EXTRACT(doc, '$.name'), JSON_OBJECT('n', 1), JSON_ARRAY(1) FROM t",
            "SELECT DATE_FORMAT(NOW(), '%Y'), YEAR(CURDATE()), DATE_ADD(NOW(), INTERVAL 1 DAY)",
            'SELECT CAST(id AS DECIMAL(10,2)), CONVERT(id, DECIMAL(10,2)) FROM t',
            'SELECT ROW_NUMBER() OVER (PARTITION BY x ORDER BY id), LAG(x) OVER (ORDER BY id) FROM t',
            'SELECT * FROM t WHERE id IN (1,2) AND (x=1 OR (y=2)) AND EXISTS (SELECT 1 FROM u)',
            'WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM c WHERE n<5) SELECT * FROM c',
            'WITH c(n) AS (SELECT 1), d(m) AS (SELECT 2) SELECT * FROM c JOIN d USING (n)',
            'SELECT * FROM (SELECT 1) AS t(n)',
            'SELECT * FROM (SELECT 1) `t`(n)',
            'WITH `c`(`n`) AS (SELECT 1) SELECT * FROM `c`',
            'SELECT * FROM t USE INDEX (idx)',
            "SELECT 'mutate_rows()', `mutate_rows`, 'COUNT (1)' /* mutate_rows() */",
            'SELECT CONNECTION_ID(), USER(), CURRENT_USER(), VERSION(), DATABASE()',
            'SELECT LAST_INSERT_ID()',
            'SELECT ROW_COUNT()',
        )
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['kind'], 'read')

    def test_space_sensitive_native_names_are_not_assumed_native(self):
        # Without IGNORE_SPACE these names can resolve differently. Comments
        # also break adjacency. The generic native ABS builder is tested above.
        for sql in ('SELECT COUNT (1)', 'SELECT SUM /* comment */ (x) FROM t',
                    'SELECT NOW\n()', 'SELECT `ABS`(1)'):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['kind'], 'risky')

    def test_sequences_and_session_functions_are_writes(self):
        # MariaDB sequences have both function and expression syntax.
        for sql in ('SELECT NEXTVAL(s)', 'SELECT SETVAL(s,42)', 'SELECT NEXT VALUE FOR s',
                    "SELECT GET_LOCK('n',0)", "SELECT RELEASE_LOCK('n')", 'SELECT RELEASE_ALL_LOCKS()',
                    'SELECT LAST_INSERT_ID(42)'):
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'write')
                self.assertEqual(dbops.decide(result, 'production', True)['action'], 'refuse')
        for sql in ('SELECT s.NEXTVAL', 'SELECT db.s.nextval', 'SELECT `s`.`NEXTVAL`',
                    'SELECT "s"."NEXTVAL"'):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['kind'], 'risky')


class TestClassify(unittest.TestCase):
    maxDiff = None

    def test_native_explain_formats_are_reads_under_both_policies(self):
        statements = (
            "SELECT REPLACE(name, 'a', 'b'), INSERT(name, 1, 1, 'x') FROM t",
            "SELECT CAST(name AS CHAR CHARACTER SET utf8mb4) FROM t",
            "UPDATE t SET v = 2",
            "DELETE FROM t", "INSERT INTO t VALUES (1)", "REPLACE INTO t VALUES (1)",
            "TABLE t", "(SELECT 1)", "CREATE TABLE t (id INT)",
        )
        for prefix in ("EXPLAIN ", "EXPLAIN FORMAT=TRADITIONAL ", "EXPLAIN FORMAT=TREE ", "EXPLAIN FORMAT=JSON "):
            for sql in statements:
                with self.subTest(sql=prefix + sql):
                    verdict = dbops.classify(prefix + sql)
                    self.assertEqual(verdict["kind"], dbops.KINDS[0])
                    self.assertEqual(verdict["kind"], "read")
                    for policy in ("production", "development"):
                        self.assertEqual(dbops.decide(verdict, policy, read_only=True)["action"], "allow")
        # Measured execution retains its inner statement's write classification.
        self.assertEqual(dbops.classify("EXPLAIN ANALYZE DELETE FROM t")["kind"], "risky")

    def test_explain_keeps_the_expression_checks_planning_evaluates(self):
        """Planning materializes a derived table without tables (verified on
        MySQL 9.7: EXPLAIN of (SELECT GET_LOCK(...)) holds the lock after), so
        the SELECT expression checks apply under EXPLAIN and DESCRIBE too."""
        cases = (
            ("EXPLAIN SELECT * FROM (SELECT GET_LOCK('m', 0)) a", "write", "lock"),
            ("EXPLAIN FORMAT=TRADITIONAL SELECT * FROM (SELECT RELEASE_ALL_LOCKS()) a", "write", "lock"),
            ("DESCRIBE SELECT * FROM (SELECT attacker.f()) a", "risky", "unknown"),
            ("EXPLAIN FORMAT=JSON DELETE FROM t WHERE id = db.f()", "risky", "unknown"),
            ("EXPLAIN FORMAT=TREE SELECT @x := 1", "write", "session"),
            ("EXPLAIN SELECT NEXTVAL(s)", "write", "sequence"),
            ("EXPLAIN SELECT * FROM t FOR UPDATE", "write", "locking_read"),
            ("EXPLAIN INSERT INTO t (id) VALUES (f())", "risky", "unknown"),
            ("EXPLAIN INSERT INTO t (id) SELECT GET_LOCK('m', 0)", "write", "lock"),
        )
        for sql, kind, category in cases:
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual((verdict["kind"], verdict["category"]), (kind, category))
                self.assertIn("EXPLAIN evaluates constant expressions", verdict["reasons"][0])
                self.assertNotEqual(dbops.decide(verdict, "development", read_only=True)["action"], "allow")
        # An explained INSERT INTO or SELECT ... INTO writes nothing.
        # A target column list is names, not a call.
        for sql in ("EXPLAIN INSERT INTO t VALUES (1)", "EXPLAIN SELECT a INTO @x FROM t",
                    "EXPLAIN INSERT INTO t (id) VALUES (1)", "EXPLAIN REPLACE INTO db.t (a, `b c`) VALUES (1, 2)",
                    "EXPLAIN INSERT IGNORE INTO t PARTITION (p0) (id) SELECT id FROM u",
                    "EXPLAIN INSERT INTO t (id) VALUES (1) ON DUPLICATE KEY UPDATE v = VALUES(v)",
                    "EXPLAIN SELECT COUNT(*), MAX(id) FROM t WHERE created > NOW()", "DESCRIBE t"):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)["kind"], "read")

    def test_handler_open_requires_review_and_read_close_stay_available(self):
        """Both engines give HANDLER OPEN's metadata lock explicit duration."""
        reason = ('HANDLER OPEN holds a metadata lock on the table until '
                  'HANDLER CLOSE or the session ends')
        for sql in ('HANDLER t OPEN', 'handler db.t open AS h',
                    'HANDLER `db`.`table name` OPEN AS `h`',
                    'HANDLER `t`OPEN', 'HANDLER `db`.`t`OPEN', 'HANDLER "t"OPEN',
                    'HANDLER "table name" OPEN', 'HANDLER café OPEN',
                    'HANDLER 1table OPEN', 'HANDLER t /* note */ OPEN',
                    '/*!80000 HANDLER t OPEN */', '/*M! HANDLER t OPEN */'):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual((verdict['kind'], verdict['category']), ('risky', 'lock'))
                self.assertIn(reason, verdict['reasons'])
                self.assertEqual(dbops.decide(verdict, 'production')['action'], 'confirm')
                self.assertEqual(dbops.decide(verdict, 'development')['action'], 'confirm')
                self.assertEqual(dbops.decide(verdict, 'development', read_only=True)['action'], 'refuse')
        for sql in ('HANDLER t READ FIRST', 'HANDLER t READ NEXT LIMIT 10',
                    'HANDLER `open` READ FIRST', "HANDLER t READ FIRST WHERE v = 'OPEN'",
                    'HANDLER t CLOSE', 'HANDLER `open` CLOSE', 'HANDLER open_table CLOSE',
                    'HANDLER tOPEN READ FIRST'):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual(verdict['kind'], 'read')
                self.assertEqual(dbops.decide(verdict, read_only=True)['action'], 'allow')
        self.assertEqual(dbops.classify('HANDLER t OPEN; HANDLER t READ FIRST; HANDLER t CLOSE')['kind'],
                         'risky')

    def test_show_and_handler_where_expressions_are_evaluated(self):
        """SHOW and HANDLER READ evaluate their WHERE on the server, so the
        SELECT expression checks (functions, :=, named locks) apply: a
        side-effect call must never classify read (review 2026-11-05)."""
        for sql in ("SHOW TABLES WHERE GET_LOCK('mf', 0)",
                    "SHOW TABLE STATUS WHERE Name = 't' OR RELEASE_LOCK('mf')",
                    "SHOW COLUMNS FROM t WHERE @x := SLEEP(0)",
                    "SHOW TABLES WHERE densify(t) > 1",
                    "HANDLER t READ FIRST WHERE GET_LOCK('mf', 0)",
                    "HANDLER t READ idx = (1) WHERE myfunc(v)"):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertNotEqual(verdict['kind'], 'read', verdict)
                self.assertEqual(dbops.decide(verdict, read_only=True)['action'], 'refuse')
        for sql in ("SHOW TABLES", "SHOW FULL PROCESSLIST", "SHOW TABLES LIKE 'a%'",
                    "SHOW TABLES WHERE Tables_in_db LIKE 't%'",
                    "SHOW TABLE STATUS WHERE Name = 't'",
                    "SHOW GRANTS FOR CURRENT_USER()", "SHOW COUNT(*) WARNINGS",
                    "SHOW VARIABLES WHERE Variable_name = 'x' AND Value IN ('ON', 'OFF')",
                    "HANDLER t READ FIRST", "HANDLER t READ idx = (1, 2)",
                    "HANDLER t READ FIRST WHERE v > 3 LIMIT 5"):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual(verdict['kind'], 'read', verdict)
                self.assertEqual(dbops.decide(verdict, read_only=True)['action'], 'allow')

    def test_get_diagnostics_assigns_variables(self):
        """GET DIAGNOSTICS only assigns variables; it is a session write."""
        for sql in ('GET DIAGNOSTICS CONDITION 1 @p = MESSAGE_TEXT',
                    'GET DIAGNOSTICS @c = NUMBER',
                    'GET CURRENT DIAGNOSTICS CONDITION 1 @s = RETURNED_SQLSTATE'):
            with self.subTest(sql=sql):
                verdict = dbops.classify(sql)
                self.assertEqual((verdict['kind'], verdict['category']), ('write', 'session'))
                self.assertEqual(dbops.decide(verdict, read_only=True)['action'], 'refuse')

    def test_table(self):
        for sql, kind, category, verbs in CASES:
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual((result['kind'], result['category'], [s['verb'] for s in result['statements']]),
                                 (kind, category, verbs))
                self.assertEqual(result['count'], len(verbs))
                if kind != 'read':
                    self.assertTrue(result['reasons'], 'a write always says why')
                json.dumps(result, allow_nan=False)

    def test_statement_text_and_offsets(self):
        sql = '  SELECT 1 ;\n/* c */ DELETE FROM t WHERE id = 2;  '
        statements = dbops.classify(sql)['statements']
        self.assertEqual([s['text'] for s in statements], ['SELECT 1', '/* c */ DELETE FROM t WHERE id = 2'])
        for s in statements:
            self.assertEqual(sql[s['start']:s['end']], s['text'])
        self.assertEqual([s['kind'] for s in statements], ['read', 'risky'])

    def test_delimiter_lines_are_not_statements(self):
        sql = 'DELIMITER $$\nCREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END$$\n' \
              'DELIMITER ;\nSELECT 1;'
        parts = dbops.split_statements(sql)
        self.assertEqual([p['text'] for p in parts],
                         ['CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW BEGIN SET NEW.a = 1; END', 'SELECT 1'])
        self.assertEqual([p['delimiter'] for p in parts], ['$$', ';'])
        # DELIMITER inside a statement or a string is text, not a command.
        self.assertEqual(len(dbops.split_statements("SELECT 'x'\nDELIMITER //")), 1)
        self.assertEqual([p['text'] for p in dbops.split_statements("SELECT 'DELIMITER //'; SELECT 2")],
                         ["SELECT 'DELIMITER //'", 'SELECT 2'])

    def test_quoted_identifiers_are_names_not_keywords(self):
        part = dbops.split_statements('UPDATE `t` SET `where` = 1 WHERE `my id` = 2')[0]
        self.assertEqual(part['idents'], ['t', 'where', 'my id'])
        result = dbops.classify('UPDATE `t` SET `where` = 1 WHERE `my id` = 2', key_columns=['my id'])
        self.assertEqual(result['kind'], 'write')

    def test_both_backslash_readings(self):
        # With backslash escapes this is one SELECT; under NO_BACKSLASH_ESCAPES
        # the string ends at the backslash and DROP TABLE is a statement.
        sql = "SELECT 'a\\'; DROP TABLE t; SELECT \\''"
        result = dbops.classify(sql)
        self.assertTrue(result['ambiguous'])
        self.assertEqual(result['kind'], 'risky')
        self.assertIn(dbops.AMBIGUOUS_REASON, result['reasons'])
        # A server backslash reading alone does not bind the client's status
        # even when sql_mode is known. Only a mode-setting reply permits the
        # synchronized non-NBE reading used by the bound editor.
        self.assertEqual(dbops.classify(sql, backslash_escapes=True)['kind'], 'risky')
        self.assertEqual(dbops.classify(sql, sql_mode='')['kind'], 'risky')
        self.assertEqual(dbops.classify(sql, sql_mode='', client_status_synced=True)['kind'], 'read')
        self.assertEqual(dbops.classify(sql, backslash_escapes=False)['kind'], 'risky')
        self.assertFalse(dbops.classify("SELECT 'it''s'; SELECT 2")['ambiguous'])

    def test_key_columns_decide_whether_where_is_risky(self):
        self.assertEqual(dbops.classify('DELETE FROM t WHERE id = 5', key_columns=['id'])['kind'], 'write')
        self.assertEqual(dbops.classify('DELETE FROM t WHERE t.ID IN (1, 2)', key_columns=['id'])['kind'], 'write')
        self.assertEqual(dbops.classify('UPDATE t SET a = 1 WHERE 5 = id', key_columns=['id'])['kind'], 'write')
        risky = dbops.classify('UPDATE t SET a = 1 WHERE status = "x"', key_columns=['id'])
        self.assertEqual(risky['kind'], 'risky')
        self.assertIn('pins no key column (id) to a value', risky['reasons'][0])
        # Without key information Development must confirm too (it may change many rows).
        unknown = dbops.classify('UPDATE t SET a = 1 WHERE id = 5')
        self.assertEqual(unknown['kind'], 'risky')
        self.assertIn('could not check whether WHERE pins a key column', unknown['reasons'][0])
        self.assertEqual(dbops.decide(unknown, 'development')['action'], 'confirm')

    def test_bad_input(self):
        for value in (None, 5, b'SELECT 1'):
            with self.assertRaises(ValueError):
                dbops.classify(value)
        with self.assertRaises(ValueError):
            dbops.classify('x' * 1000001)


# ---- review gate regressions (2026-10-14) -------------------------------------------------

class TestReviewGate(unittest.TestCase):
    """Probes from the review of the safety foundation, one per finding."""

    def kind(self, sql, **kw):
        return dbops.classify(sql, **kw)['kind']

    def test_1_set_names_is_one_item_of_the_list(self):
        for sql in ('SET NAMES utf8mb4, GLOBAL read_only = 0', 'SET NAMES utf8mb4, sql_log_bin = 0',
                    'SET CHARACTER SET utf8mb4, PERSIST max_connections = 1',
                    'SET NAMES latin1, @@global.super_read_only = OFF', 'SET CHARSET utf8mb4, @@persist_only.x = 1'):
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'risky')
                self.assertEqual(dbops.decide(result, 'production', True)['action'], 'refuse')
        self.assertEqual(self.kind("SET NAMES 'utf8mb4' COLLATE 'utf8mb4_bin'"), 'read')
        self.assertEqual(self.kind('SET NAMES utf8mb4, @x = 1'), 'write')

    def test_2_quoted_delimiter_argument(self):
        for sql in ("DELIMITER '//'\nSELECT 1//\nDROP TABLE t//", 'DELIMITER "$$"\nSELECT 1$$\nDROP TABLE t$$',
                    'DELIMITER `;;`\nSELECT 1;;\nDROP TABLE t;;', 'DELIMITER //\tx\nSELECT 1//\tx DROP TABLE t//\tx',
                    'delimiterX //\nSELECT 1//DROP TABLE t//'):
            with self.subTest(sql=sql):
                parts = dbops.split_statements(sql)
                self.assertEqual([p['text'] for p in parts], ['SELECT 1', 'DROP TABLE t'])
                self.assertEqual(self.kind(sql), 'risky')
        # An unquoted argument ends at a space; the client keeps the old
        # delimiter when the parsed value still has a backslash or is
        # missing. get_arg unescapes pairs in unquoted values first.
        self.assertEqual(dbops.split_statements('DELIMITER // ignored\nSELECT 1//')[0]['delimiter'], '//')
        self.assertEqual(dbops.split_statements('DELIMITER /\\/\nSELECT 1//')[0]['delimiter'], '//')
        for sql in ('DELIMITER /\\\\/\nSELECT 1', 'DELIMITER\nSELECT 1', "DELIMITER ''\nSELECT 1"):
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'risky')
                self.assertIn(dbops.REASON_DELIMITER, result['reasons'])

    def test_3_semicolons_inside_a_custom_delimiter_statement(self):
        result = dbops.classify('DELIMITER //\nSELECT 1; DROP TABLE t//')
        self.assertEqual((result['kind'], result['count']), ('risky', 1))
        self.assertIn(dbops.REASON_MULTI, result['reasons'])
        self.assertEqual(dbops.decide(result, 'production', True)['action'], 'refuse')
        self.assertEqual(self.kind('DELIMITER //\nSELECT 1; SELECT 2//'), 'read')
        self.assertEqual(self.kind("DELIMITER //\nSELECT ';' ; SHOW TABLES//"), 'read')

    def test_4_dash_comments_need_client_white_space(self):
        for dash in ('\x1c', '\x1d', '\x1e', '\x1f', '\x01', '\x7f'):
            with self.subTest(dash=repr(dash)):
                result = dbops.classify('SELECT 1 --%s; DROP TABLE t' % dash)
                self.assertEqual(result['kind'], 'risky')
                self.assertIn(dbops.REASON_DASH_CONTROL, result['reasons'])
        # NBSP is not white space to the client: '--\xa0' is code and ';' splits.
        result = dbops.classify('SELECT 1 --\xa0; DROP TABLE t')
        self.assertEqual((result['kind'], result['count']), ('risky', 2))
        self.assertEqual(self.kind('SELECT 1 --\t; DROP TABLE t'), 'read')
        self.assertEqual(self.kind('SELECT 1 --\v; DROP TABLE t'), 'read')

    def test_5_client_backslash_commands(self):
        parts = dbops.split_statements('SELECT 1\\gDROP TABLE t\\G')
        self.assertEqual([p['text'] for p in parts], ['SELECT 1', 'DROP TABLE t'])
        self.assertEqual(self.kind('SELECT 1\\gDROP TABLE t'), 'risky')
        for command in ('!', '.', 'r', 'u', 'd', 'C', 'q', 'T', 'P'):
            sql = 'SELECT 1\\%s x' % command
            with self.subTest(sql=sql):
                result = dbops.classify(sql)
                self.assertEqual(result['kind'], 'risky')
                self.assertIn(dbops.REASON_CLIENT_COMMAND % command, result['reasons'])
        # Inside strings and comments a backslash is no command; \N is NULL.
        for sql in ("SELECT 'a\\\\gb'", 'SELECT 1 /* \\! x */', 'SELECT 1 -- \\! x', 'SELECT \\N'):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'read')
        self.assertEqual(dbops.WRITE_CLIENT_FLAGS, ('--comments', '--binary-mode'))

    def test_6_requests_cannot_lower_the_protection(self):
        self.assertEqual(dbops.connection_safety(None, {'type': 'development'})['type'], 'production')
        dev = {'id': 'p1', 'name': 'dev', 'type': 'development', 'read_only': False, 'host': 'dev.db', 'port': 3306,
               'user': 'app'}
        same = {'profile_id': 'p1', 'host': 'dev.db', 'port': '3306', 'user': 'app', 'password': 'x'}
        self.assertEqual(dbops.connection_safety(dev, same)['type'], 'development')
        for change in ({'host': 'prod.db'}, {'port': 3307}, {'user': 'root'}, {'ssl_mode': 'DISABLED'}, {'port': 'x'}):
            with self.subTest(change=change):
                safety = dbops.connection_safety(dev, dict(same, **change))
                self.assertEqual((safety['type'], safety['endpoint_matches']), ('production', False))
        locked = dbops.connection_safety(dict(dev, read_only=True), dict(same, host='prod.db'))
        self.assertTrue(locked['read_only'])

    def test_7_set_statement_variables_count(self):
        for variable in ('sql_log_bin=0', 'gtid_domain_id=9', 'server_id=7', 'pseudo_thread_id=3'):
            sql = 'SET STATEMENT %s FOR INSERT INTO t VALUES (1)' % variable
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['category'], 'replication')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = 1, sql_log_bin = 0 FOR SELECT 1'), 'risky')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = 1 FOR SELECT 1'), 'read')

    def test_8_dml_needs_a_key_pinned_to_a_value(self):
        for where in ('id > 0', '2 > 1', "'1'", 'TRUE OR a = 2', 'a = a', 'id = id', 'NOT id = 5', 'id <> 5',
                      'id = 5 OR id = 6', 'id IN (SELECT id FROM u)', 'id BETWEEN 1 AND 9'):
            for keys in (None, ['id']):
                sql = 'DELETE FROM t WHERE ' + where
                with self.subTest(sql=sql, keys=keys):
                    result = dbops.classify(sql, key_columns=keys)
                    self.assertEqual(result['kind'], 'risky')
                    self.assertEqual(dbops.decide(result, 'development')['action'], 'confirm')
        for where in ('id = 5', 'id = ?', 'id = :id', "id = 'a'", 'id IN (1, 2)', 'id <=> 5', 't.id = 5 AND a > 2',
                      '5 = id', '`id` = 1'):
            sql = 'UPDATE t SET a = 1 WHERE ' + where
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql, key_columns=['id']), 'write')

    def test_9_read_only_is_enforced_by_the_server_too(self):
        self.assertEqual(dbops.decide(dbops.classify('SELECT f()'), 'production', True)['session_guard'],
                         ['SET SESSION TRANSACTION READ ONLY'])
        self.assertEqual(dbops.decide(dbops.classify('SELECT 1'), 'production')['session_guard'], [])
        for sql in ('SET SESSION transaction_read_only = 0', 'SET tx_read_only = OFF', 'SET TRANSACTION READ WRITE',
                    'SET SESSION TRANSACTION READ WRITE', 'START TRANSACTION READ WRITE'):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.decide(dbops.classify(sql), 'development', True)['action'], 'refuse')
        self.assertEqual(self.kind('SET SESSION TRANSACTION READ ONLY'), 'read')
        self.assertEqual(self.kind('DO my_function()'), 'risky')

    def test_11_category_and_tone_do_not_depend_on_order(self):
        for sql in ('SET @a = 1; DELETE FROM t WHERE id = 1', 'DELETE FROM t WHERE id = 1; SET @a = 1'):
            with self.subTest(sql=sql):
                result = dbops.classify(sql, key_columns=['id'])
                self.assertEqual((result['kind'], result['category']), ('write', 'dml'))
                self.assertEqual(dbops.decide(result, 'production')['tone'], 'danger')
        self.assertEqual(dbops.decide(dbops.classify('SET @a = 1; COMMIT'), 'production')['tone'], 'primary')

    def test_12_key_check_is_linear(self):
        import time
        started = time.monotonic()
        for size in (20000, 60000):
            dbops.classify('DELETE FROM t WHERE ' + 'a .' * size, key_columns=['id'])
        self.assertLess(time.monotonic() - started, 5)
        result = dbops.classify('DELETE FROM t WHERE id = 1 AND ' + 'a = 1 AND ' * 3000 + 'b = 2', key_columns=['id'])
        self.assertEqual(result['kind'], 'risky')
        self.assertIn('too long', result['reasons'][0])

    def test_13_mariadb_replication_session_variables(self):
        for variable in ('skip_replication', 'wsrep_on'):
            self.assertIn(variable, dbops.SESSION_RISKY_VARIABLES)
        hint = dbops.privilege_hint('set_session_restricted', 'mariadb', (11, 4, 2), ['GRANT USAGE ON *.* TO u'],
                                    variable='skip_replication')
        self.assertEqual((hint['allowed'], hint['need']), (True, 'no privilege'))
        hint = dbops.privilege_hint('set_session_restricted', 'mariadb', (11, 4, 2), ['GRANT USAGE ON *.* TO u'],
                                    variable='wsrep_on')
        self.assertEqual((hint['allowed'], hint['need']), (False, 'SUPER'))

    def test_14_connection_identity_matches_binding_ts(self):
        base = {'profile_id': 'p1', 'host': 'db', 'port': '3306', 'user': 'app', 'ssl_mode': 'REQUIRED',
                'password': 'never'}
        self.assertEqual(dbops.connection_identity(base), '["p1","db",3306,"app","REQUIRED",""]')
        self.assertEqual(dbops.connection_identity(dict(base, type='production', read_only=False)),
                         dbops.connection_identity(base))
        self.assertEqual(dbops.connection_identity(dict(base, type='development')),
                         '["p1","db",3306,"app","REQUIRED","","development",false]')
        self.assertEqual(dbops.connection_identity(base, {'type': 'production', 'read_only': True}),
                         '["p1","db",3306,"app","REQUIRED","","production",true]')
        self.assertNotIn('never', dbops.connection_identity(base))
        source = (Path(__file__).resolve().parent.parent / 'apps' / 'workspace' / 'src' / 'binding.ts').read_text(encoding="utf-8")
        self.assertIn('key.push(type, c.read_only === true)', source)
        from myflames import report
        options = {'host': 'db', 'port': 3306}
        self.assertNotEqual(report.flight_key(options, 'p1', {'type': 'development'}),
                            report.flight_key(options, 'p1', {'type': 'production'}))
        self.assertEqual(report.flight_key(options, 'p1'), report.flight_key(options, 'p1'))

    def test_15_set_statement_cannot_lift_session_guards(self):
        """Editor review gate item 3: MariaDB SET STATEMENT accepts
        transaction_read_only and tx_read_only (no NO_SET_STMT), so a
        read-only session could write for one statement. Quoted names count."""
        for sql in ('SET STATEMENT transaction_read_only = 0 FOR SELECT f()',
                    'SET STATEMENT `transaction_read_only` = 0 FOR SELECT f()',
                    'SET STATEMENT `tx_read_only`=0 FOR SELECT f()',
                    'SET STATEMENT max_statement_time = 1, sql_safe_updates = 0 FOR SELECT 1'):
            with self.subTest(sql=sql):
                self.assertNotEqual(self.kind(sql), 'read')
                self.assertEqual(dbops.decide(dbops.classify(sql), 'development', True)['action'], 'refuse')
        for sql in ('SET SESSION sql_safe_updates = 0', 'SET `sql_safe_updates` = 0', 'SET tx_read_only = 0'):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'write')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = 1 FOR SELECT 1'), 'read')

    def test_15b_set_value_with_a_call_is_a_write(self):
        """A read variable assigned from a function call, user variable or
        subquery is a write: the value can run a stored function or take a lock."""
        for sql in ("SET SESSION group_concat_max_len = my_writer()",
                    "SET SESSION group_concat_max_len = GET_LOCK('a', 1)",
                    "SET max_execution_time = (SELECT 1)",
                    "SET SESSION group_concat_max_len = @a := 5",
                    "SET SESSION time_zone = '+00:00', max_execution_time = db.f(1)"):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'write')
        for sql in ('SET SESSION group_concat_max_len = 1000000', "SET SESSION time_zone = 'a(b'"):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'read')

    def test_15c_set_values_that_can_write_are_writes(self):
        """Security review: calls named in non-ASCII or with $, quoted names
        (ANSI_QUOTES "f" is masked as a string), MariaDB NEXT VALUE FOR and
        ORACLE-mode seq.NEXTVAL, and every value inside MariaDB SET STATEMENT
        (set_stmt_option takes set_expr_or_default) can change state."""
        for sql in ('SET time_zone = "f"()',
                    "SET time_zone = 'f' ()",
                    'SET sql_select_limit = é()',
                    'SET sql_select_limit = €()',
                    'SET sql_select_limit = ©()',
                    'SET sql_select_limit = 。()',
                    'SET sql_select_limit = 1$()',
                    'SET sql_select_limit = \u00a0()',
                    'SET sql_select_limit = (TABLE v)',
                    'SET sql_select_limit = NEXT VALUE FOR db.s',
                    'SET sql_select_limit = next  value  for s',
                    'SET sql_select_limit = s.NEXTVAL',
                    'SET sql_select_limit = db.s.`nextval`',
                    "SET STATEMENT max_statement_time = GET_LOCK('x',1) FOR SELECT 1",
                    'SET STATEMENT max_statement_time = (@x := 5) FOR SELECT 1',
                    'SET STATEMENT max_statement_time = 1, sql_select_limit = f() FOR SELECT 1',
                    'SET time_zone = CONVERT(f() USING utf8mb4)',
                    "SET time_zone = CONCAT(CONVERT(X'41' USING utf8mb4))",
                    "SET time_zone = CONVERT(X'41' USING utf8mb4), sql_select_limit = f()"):
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'write')
                self.assertEqual(dbops.decide(dbops.classify(sql), 'development', True)['action'], 'refuse')

    def test_15c2_set_statement_splits_at_the_for_that_ends_the_assignments(self):
        """MariaDB SET STATEMENT ... FOR <statement>: NEXT VALUE FOR and
        PREVIOUS VALUE FOR inside a value are not the FOR that starts the
        statement (sql_yacc.yy column_default_non_parenthesized_expr)."""
        split = dbops._set_statement_for
        for sql, statement in (
                ('SET STATEMENT max_statement_time = NEXT VALUE FOR s FOR SELECT 1', 'SELECT 1'),
                ('SET STATEMENT max_statement_time = next  value  for db.s FOR DELETE FROM t', 'DELETE FROM t'),
                ('SET STATEMENT max_statement_time = PREVIOUS VALUE FOR s, sql_select_limit = 5 FOR SELECT 2',
                 'SELECT 2'),
                ('SET STATEMENT max_statement_time = 1 FOR SELECT 3', 'SELECT 3'),
                ('SET STATEMENT max_statement_time = (SUBSTRING(x FROM 1 FOR 2)) FOR SELECT 4', 'SELECT 4')):
            with self.subTest(sql=sql):
                upper = sql.upper()
                found = split(dbops._flat(upper))
                self.assertIsNotNone(found)
                self.assertEqual(upper[found.end():].strip(), statement.upper())
        self.assertIsNone(split('SET STATEMENT MAX_STATEMENT_TIME = NEXT VALUE FOR S'))
        # The split now reads the value (a sequence advance is a write) and
        # the real statement, instead of judging "s FOR SELECT 1" as unknown.
        verdict = dbops.classify('SET STATEMENT max_statement_time = NEXT VALUE FOR s FOR SELECT 1')
        self.assertEqual((verdict['kind'], verdict['category']), ('write', 'session'))
        self.assertEqual(dbops.decide(verdict, 'development', True)['action'], 'refuse')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = NEXT VALUE FOR s FOR DELETE FROM t'), 'risky')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = PREVIOUS VALUE FOR s FOR SELECT 1'), 'read')
        self.assertEqual(self.kind('SET STATEMENT max_statement_time = NEXT VALUE FOR s'), 'risky')

    def test_15d_reads_myflames_sends_stay_reads(self):
        """Plain values, and myflames' own hex text literal (the exact
        CONVERT(X'..' USING utf8mb4) form), stay reads, so Parameters session
        changes keep working on read-only connections."""
        from myflames import perf_common, variables
        reads = ["SET time_zone = '+00:00'", 'SET NAMES utf8mb4', 'SET SESSION sql_select_limit = 10',
                 "SET @@session.optimizer_switch = 'index_merge=off'",
                 "SET SESSION time_zone = 'SYSTEM'", 'SET SESSION explain_json_format_version=2',
                 'SET explain_json_format_version=2', 'SET SESSION lock_wait_timeout = 5',
                 'SET SESSION innodb_lock_wait_timeout = 5', 'SET SESSION information_schema_stats_expiry = 0',
                 "SET SESSION optimizer_trace='enabled=on'", "SET SESSION optimizer_switch='mrr=on'",
                 'SET SESSION TRANSACTION READ ONLY', 'SET time_zone = @@global.time_zone',
                 'SET max_execution_time = 1.5', 'SET sql_select_limit = s.CURRVAL',
                 'SET STATEMENT max_statement_time = 5 FOR SELECT 1',
                 'SET SESSION time_zone = ' + perf_common.literal('Europe/Zürich'),
                 "SET time_zone = CONVERT( X'41'  USING  UTF8MB4 )",
                 variables.statement_for('time_zone', 'session', variables._text_literal('Europe/Zürich')),
                 variables.statement_for('lc_time_names', 'session', variables._text_literal("it's"))]
        for sql in reads:
            with self.subTest(sql=sql):
                self.assertEqual(self.kind(sql), 'read')
                self.assertEqual(dbops.decide(dbops.classify(sql), 'production', True)['action'], 'allow')

    def test_15e_calls_named_with_any_non_ascii_character_are_not_reads(self):
        """Both lexers take any byte that starts a multibyte character as an
        identifier character (MySQL my_ismb1st, MariaDB is_multi_byte_ident),
        so with utf8mb4 €() calls a stored function: capture and the MCP
        explain tool must refuse it. Non-ASCII text in strings, plain column
        names and ASCII white space stay reads."""
        from myflames import live
        # Non-ASCII spaces are identifier characters too: CONCAT\u00a0( calls
        # a function named "CONCAT\u00a0", not CONCAT.
        for sql in ('SELECT €()', 'SELECT ©(1) FROM t', 'SELECT 。()', 'SELECT a FROM t WHERE €x(a) > 0',
                    'SELECT db€.f()', 'SELECT \u3000() FROM t', "SELECT CONCAT\u00a0('a')",
                    "SELECT COUNT\u2007(*) FROM t"):
            with self.subTest(sql=sql):
                self.assertNotEqual(self.kind(sql), 'read')
                with self.assertRaises(ValueError):
                    live.bind_query(sql)
        for sql in ("SELECT '€()' FROM t", 'SELECT €col FROM t', 'SELECT `€`(1) FROM t',
                    "SELECT CONCAT('a', '\u00a0')", 'SELECT 1\tFROM\nt'):
            with self.subTest(sql=sql):
                expected = 'read' if not sql.startswith('SELECT `') else 'risky'
                self.assertEqual(self.kind(sql), expected)

    def test_16_backslash_commands_inside_optimizer_hints(self):
        """Editor review gate item 10: the client runs backslash commands inside
        /*+ */ hints (SSC_HINT is not a comment to add_line), e.g. \\C latin1."""
        for sql in ('SELECT /*+ \\C latin1 */ 1', 'SELECT /*+ \\d */ 1', "SELECT /*+ SET_VAR(a='\\x') */ 1"):
            with self.subTest(sql=sql):
                statement = dbops.split_statements(sql)[0]
                self.assertTrue(statement['issues'])
                self.assertEqual(self.kind(sql), 'risky')
        self.assertEqual(dbops.split_statements('SELECT /*+ MAX_EXECUTION_TIME(10) */ 1')[0]['issues'], [])
        self.assertEqual(dbops.split_statements('SELECT /* \\C latin1 */ 1')[0]['issues'], [])


# ---- the confirmation policy -----------------------------------------------------------

class TestPolicy(unittest.TestCase):
    MATRIX = {
        # kind: (production, development, read-only production, read-only development)
        'read': ('allow', 'allow', 'allow', 'allow'),
        'write': ('confirm', 'allow', 'refuse', 'refuse'),
        'risky': ('confirm', 'confirm', 'refuse', 'refuse'),
    }

    def test_matrix(self):
        sql = {'read': 'SELECT 1', 'write': 'INSERT INTO t VALUES (1)', 'risky': 'KILL 41'}
        for kind, expected in self.MATRIX.items():
            verdict = dbops.classify(sql[kind])
            got = tuple(dbops.decide(verdict, ctype, read_only)['action']
                        for read_only in (False, True) for ctype in ('production', 'development'))
            self.assertEqual(got, expected, kind)

    def test_texts_and_tone(self):
        refuse = dbops.decide(dbops.classify('KILL 41'), 'production', True, name='prod db')
        self.assertEqual(refuse['title'], 'Read-only connection')
        self.assertEqual(refuse['text'], "Connection prod db is read-only in myflames, so this statement was not "
                                         "sent. To allow writes, turn off Read-only in its settings, or run: "
                                         "myflames profiles set 'prod db' --read-write.")
        confirm = dbops.decide(dbops.classify('DELETE FROM t; DELETE FROM u'), 'production')
        self.assertEqual(confirm['text'], 'This is a Production connection, so myflames asks before every write.')
        self.assertEqual((confirm['tone'], confirm['statements']), ('danger', ['DELETE FROM t', 'DELETE FROM u']))
        risky = dbops.decide(dbops.classify('DROP TABLE t'), 'development', name='dev')
        self.assertEqual(risky['text'], 'Connection dev is a Development connection. myflames asks only before '
                                        'risky writes, and this statement is one.')
        light = dbops.decide(dbops.classify('SET @x = 1'), 'production')
        self.assertEqual((light['action'], light['tone']), ('confirm', 'primary'))
        for decision in (refuse, confirm, risky, light):
            self.assertNotIn('—', decision['text'])

    def test_gate(self):
        confirm = dbops.decide('write', 'production')
        self.assertEqual(dbops.gate(confirm)[0], 'needs_confirmation')
        self.assertIsNone(dbops.gate(confirm, confirmed=True))
        self.assertEqual(dbops.gate(dbops.decide('write', 'production', True), confirmed=True)[0], 'refused')
        self.assertIsNone(dbops.gate(dbops.decide('write', 'development')))

    def test_types_and_safety(self):
        self.assertEqual(dbops.normalize_type(None), 'production')
        self.assertEqual(dbops.normalize_type('Development'), 'development')
        for bad in ('staging', 5, True):
            with self.assertRaises(ValueError):
                dbops.normalize_type(bad)
        with self.assertRaises(ValueError):
            dbops.decide({'kind': 'maybe'})
        profile = {'id': 'p1', 'name': 'dev', 'type': 'development', 'read_only': False}
        self.assertEqual(dbops.connection_safety(profile),
                         {'profile_id': 'p1', 'profile_name': 'dev', 'type': 'development', 'read_only': False,
                          'endpoint_matches': True})
        # A request may ask for more protection, never less.
        self.assertTrue(dbops.connection_safety(profile, {'read_only': True})['read_only'])
        self.assertTrue(dbops.connection_safety(dict(profile, read_only=True), {'read_only': False})['read_only'])
        self.assertEqual(dbops.connection_safety(None, {}),
                         {'profile_id': None, 'profile_name': None, 'type': 'production', 'read_only': False,
                          'endpoint_matches': False})
        self.assertEqual(dbops.connection_safety(None, {'type': 'other', 'read_only': 'yes'})['type'], 'production')
        self.assertFalse(dbops.connection_safety(None, {'read_only': 'yes'})['read_only'])


# ---- privileges ----------------------------------------------------------------------

class TestPrivileges(unittest.TestCase):
    def test_parse_grants(self):
        parsed = dbops.parse_grants([
            'GRANT SELECT, PROCESS ON *.* TO `app`@`%`',
            'GRANT CONNECTION_ADMIN,SYSTEM_USER ON *.* TO `app`@`%`',
            'GRANT INSERT, UPDATE ON `shop`.* TO `app`@`%`',
            'GRANT DELETE ON `my\\_db`.* TO `app`@`%`',
            'GRANT ALTER ON `shop`.`t` TO `app`@`%`',
            'GRANT `role1`@`%` TO `app`@`%`',
        ])
        self.assertEqual(parsed['global'], {'SELECT', 'PROCESS', 'CONNECTION_ADMIN', 'SYSTEM_USER'})
        self.assertEqual(parsed['schemas'], {'shop': {'INSERT', 'UPDATE'}, 'my_db': {'DELETE'}})
        self.assertEqual(parsed['unparsed'], [])

    def test_parse_grants_keeps_escaped_and_wildcard_names_apart(self):
        # db\_x is the database db_x; db_x is a pattern that also matches dbAx.
        parsed = dbops.parse_grants(['GRANT SELECT ON `db\\_x`.* TO `u`@`%`', 'GRANT INSERT ON `db_x`.* TO `u`@`%`',
                                     'GRANT DELETE ON `shop%`.* TO `u`@`%`'])
        self.assertEqual(parsed['schemas'], {'db_x': {'SELECT'}})
        self.assertEqual(parsed['patterns'], {'db_x': {'INSERT'}, 'shop%': {'DELETE'}})
        lines = ['GRANT SELECT ON `db\\_x`.* TO u', 'GRANT INSERT ON `db_x`.* TO u']
        self.assertIsNone(self.hint('insert', 'mysql', (8, 4, 6), *lines, schema='db_x')['allowed'])
        self.assertTrue(self.hint('insert', 'mysql', (8, 4, 6), *lines, schema='dbAx')['allowed'])

    def test_parse_grants_keeps_package_library_and_unknown_lines(self):
        parsed = dbops.parse_grants(['GRANT EXECUTE ON PACKAGE BODY `shop`.`pkg` TO `app`@`%`',
                                     'GRANT EXECUTE ON LIBRARY `shop`.`lib` TO `app`@`%`',
                                     'GRANT SOMETHING NEW ON EVERYTHING'], 'mariadb')
        self.assertEqual({r['kind'] for r in parsed['model']['routines']}, {'PACKAGE BODY', 'LIBRARY'})
        self.assertEqual(parsed['unparsed'], ['GRANT SOMETHING NEW ON EVERYTHING'])
        # An unparsed line may grant it: unknown, never denied.
        hint = dbops.privilege_hint('kill', 'mysql', (8, 4, 6), ['GRANT USAGE ON *.* TO u', 'GRANT ??? ON x'])
        self.assertIsNone(hint['allowed'])

    def test_strip_credentials(self):
        cases = {
            "GRANT USAGE ON *.* TO `a`@`%` IDENTIFIED BY PASSWORD '*13C17A9ABF411E6300B51748CB04F59182323686'":
                'GRANT USAGE ON *.* TO `a`@`%`',
            "GRANT USAGE ON *.* TO `e`@`%` IDENTIFIED VIA ed25519 USING 'bikAd6IJ/jBlkOkm' OR unix_socket":
                'GRANT USAGE ON *.* TO `e`@`%`',
            "GRANT SELECT ON *.* TO `a`@`%` IDENTIFIED VIA x USING 'WITH GRANT OPTION' WITH GRANT OPTION":
                'GRANT SELECT ON *.* TO `a`@`%` WITH GRANT OPTION',
            "CREATE USER `a`@`%` IDENTIFIED WITH 'caching_sha2_password' AS 0x2441243030 DEFAULT ROLE `r`@`%` "
            "REQUIRE NONE": 'CREATE USER `a`@`%` DEFAULT ROLE `r`@`%` REQUIRE NONE',
            "CREATE USER `a`@`%` IDENTIFIED WITH 'p1' AS 'x' AND IDENTIFIED WITH 'p2' AS 'y' ACCOUNT LOCK":
                'CREATE USER `a`@`%` ACCOUNT LOCK',
            "CREATE USER `p`@`%` IDENTIFIED BY PASSWORD '*27E2' WITH MAX_USER_CONNECTIONS 3":
                'CREATE USER `p`@`%` WITH MAX_USER_CONNECTIONS 3',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dbops.strip_credentials(text), expected)

    def test_gate351_strip_credentials_fails_closed_after_unterminated_quote(self):
        for value in ("CREATE USER 'a'@'%  IDENTIFIED BY 'gate351-secret'",
                      'CREATE USER "a"@"%  IDENTIFIED BY \'gate351-secret\'',
                      "CREATE USER `a`@`%  IDENTIFIED VIA ed25519 USING 'gate351-secret'",
                      "CREATE USER 'a'@'%  PASSWORD 'gate351-secret'"):
            with self.subTest(value=value):
                clean = dbops.strip_credentials(value)
                self.assertNotIn('gate351-secret', clean)
                self.assertNotIn('IDENTIFIED', clean)

    def test_strip_credentials_visits_many_clauses_once(self):
        # Repeated clauses used to re-tokenize the whole remaining text.
        code = (
            "import time\nfrom myflames import dbops\n"
            "clause = \"IDENTIFIED BY 'secret' REQUIRE NONE \"\n"
            "count = 65536 // len(clause)\n"
            "value = clause * count\n"
            "began = time.monotonic()\n"
            "clean = dbops.strip_credentials(value)\n"
            "elapsed = time.monotonic() - began\n"
            "assert clean == ' '.join(['REQUIRE NONE'] * count)\n"
            "print(elapsed)\n")
        result = subprocess.run([sys.executable, '-c', code], cwd=str(Path(__file__).resolve().parent.parent),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(float(result.stdout.strip()), 1)

    def test_strip_credentials_does_not_retry_unterminated_escaped_quotes(self):
        code = (
            "import time\nfrom myflames import dbops\n"
            "prefix = \"CREATE USER u IDENTIFIED BY '\"\n"
            "value = prefix + ('a' + chr(92) + chr(39)) * (65536 // 3) + ' WITH GRANT OPTION'\n"
            "began = time.monotonic()\n"
            "clean = dbops.strip_credentials(value)\n"
            "elapsed = time.monotonic() - began\n"
            "assert clean == 'CREATE USER u', clean\n"
            "print(elapsed)\n")
        result = subprocess.run([sys.executable, '-c', code], cwd=str(Path(__file__).resolve().parent.parent),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(float(result.stdout.strip()), 1)

    def test_read_grants_returns_no_credentials(self):
        from _grants_fixtures import grants_batch
        class Conn(object):
            def run(self, sql, extra_flags=None):
                return grants_batch([
                    "GRANT USAGE ON *.* TO `app`@`%` IDENTIFIED BY PASSWORD '*13C17A9ABF411E6300B51748CB04F59182323686'",
                    "GRANT SELECT ON `shop`.* TO `app`@`%`"], account='app@%', login='app@localhost',
                    version='11.4.13-MariaDB')
        result = dbops.read_grants(Conn())
        self.assertEqual(result['grants'], ['GRANT USAGE ON *.* TO `app`@`%`', 'GRANT SELECT ON `shop`.* TO `app`@`%`'])
        self.assertNotIn('13C17A9', json.dumps(result))

    def test_mariadb_super_split_ends_at_11_0_1(self):
        from myflames import locks
        line = ['GRANT SUPER ON *.* TO u']
        self.assertTrue(self.hint('kill', 'mariadb', (11, 0, 0), *line)['allowed'])
        self.assertIsNone(self.hint('kill', 'mariadb', (11, 0, 1), *line)['allowed'])
        self.assertTrue(locks.parse_kill_privilege('mariadb', (11, 0, 0), line)['can_kill_others'])
        self.assertFalse(locks.parse_kill_privilege('mariadb', (11, 0, 1), line)['can_kill_others'])
        self.assertTrue(self.hint('write_when_read_only', 'mariadb', (10, 11, 0), *line)['allowed'])
        hint = self.hint('write_when_read_only', 'mariadb', (10, 11, 1), *line)
        self.assertIsNone(hint['allowed'])
        self.assertIn('10.11.1', hint['note'])

    def hint(self, operation, engine, version, *lines, **kw):
        return dbops.privilege_hint(operation, engine, version, list(lines), **kw)

    def test_mysql(self):
        self.assertEqual(self.hint('kill', 'mysql', (8, 4, 6), 'GRANT SUPER ON *.* TO u')['via'], 'SUPER')
        h = self.hint('kill', 'mysql', (8, 4, 6), 'GRANT PROCESS ON *.* TO u')
        self.assertEqual((h['allowed'], h['need']), (False, 'CONNECTION_ADMIN or SUPER'))
        self.assertIn('SYSTEM_USER', h['note'])
        h = self.hint('set_persist_only', 'mysql', (8, 4, 6), 'GRANT SUPER ON *.* TO u')
        self.assertEqual((h['allowed'], h['need']), (False, 'SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN'))
        h = self.hint('set_global', 'mysql', (8, 4, 6), 'GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO u',
                      variable='default_table_encryption')
        self.assertFalse(h['allowed'])
        h = self.hint('set_global', 'mysql', (8, 4, 6),
                      'GRANT SYSTEM_VARIABLES_ADMIN,TABLE_ENCRYPTION_ADMIN ON *.* TO u',
                      variable='default_table_encryption')
        self.assertEqual(h['via'], 'SYSTEM_VARIABLES_ADMIN and TABLE_ENCRYPTION_ADMIN')
        h = self.hint('set_global', 'mysql', (8, 4, 6), 'GRANT SUPER ON *.* TO u', variable='mandatory_roles')
        self.assertEqual((h['allowed'], h['need']), (False, 'SYSTEM_VARIABLES_ADMIN and ROLE_ADMIN or SUPER and ROLE_ADMIN'))
        self.assertTrue(self.hint('replication', 'mysql', (8, 4, 6), 'GRANT ALL PRIVILEGES ON *.* TO u')['allowed'])
        self.assertTrue(self.hint('clone_donor', 'mysql', (8, 4, 6), 'GRANT BACKUP_ADMIN ON *.* TO u')['allowed'])

    def test_mariadb(self):
        h = self.hint('kill', 'mariadb', (10, 11, 9), 'GRANT SUPER ON *.* TO u')
        self.assertEqual((h['allowed'], h['via']), (True, 'SUPER'))
        # From 11.0, SUPER counts only for grants stored by an earlier version.
        h = self.hint('kill', 'mariadb', (11, 4, 2), 'GRANT SUPER ON *.* TO u')
        self.assertIsNone(h['allowed'])
        self.assertIn('stored by an earlier version', h['note'])
        self.assertTrue(self.hint('kill', 'mariadb', (11, 4, 2), 'GRANT CONNECTION ADMIN ON *.* TO u')['allowed'])
        self.assertEqual(self.hint('set_session_restricted', 'mariadb', (11, 4, 2), 'GRANT BINLOG ADMIN ON *.* TO u',
                                   variable='sql_log_bin')['via'], 'BINLOG ADMIN')
        self.assertFalse(self.hint('set_persist', 'mariadb', (11, 4, 2), 'GRANT ALL ON *.* TO u')['available'])
        self.assertIsNone(self.hint('write_when_read_only', 'mariadb', (11, 4, 2), 'GRANT SUPER ON *.* TO u')['allowed'])
        self.assertTrue(self.hint('write_when_read_only', 'mariadb', (10, 6, 0), 'GRANT SUPER ON *.* TO u')['allowed'])

    def test_object_privileges_check_global_and_schema_grants(self):
        line = 'GRANT INSERT ON `shop`.* TO u'
        self.assertTrue(self.hint('insert', 'mysql', (8, 4, 6), line, schema='shop')['allowed'])
        self.assertIsNone(self.hint('insert', 'mysql', (8, 4, 6), line, schema='other')['allowed'])
        with self.assertRaises(ValueError):
            dbops.required_privileges('fly', 'mysql')

    def test_kill_hint_shares_the_parser(self):
        from myflames import locks
        lines = ['GRANT PROCESS ON *.* TO `u`@`%`', 'GRANT CONNECTION_ADMIN ON *.* TO `u`@`%`']
        self.assertEqual(locks.parse_kill_privilege('mysql', (8, 4, 6), lines)['via'], 'CONNECTION_ADMIN')
        self.assertEqual(dbops.privilege_hint('kill', 'mysql', (8, 4, 6), lines)['via'], 'CONNECTION_ADMIN')

    def test_read_grants(self):
        from _grants_fixtures import grants_batch
        class Conn(object):
            calls = []

            def run(self, sql, extra_flags=None):
                self.calls.append((sql, extra_flags))
                return grants_batch(['GRANT USAGE ON *.* TO `app`@`%`'], account='app@%', login='app@localhost')
        conn = Conn()
        result = dbops.read_grants(conn)
        self.assertEqual((result['user'], result['account'], result['grants']),
                         ('app', 'app@%', ['GRANT USAGE ON *.* TO `app`@`%`']))
        self.assertEqual(len(conn.calls), 1)
        sql, flags = conn.calls[0]
        self.assertTrue(sql.startswith('/*myflames:dbops*/ '))
        self.assertEqual(flags, ['--comments', '--skip-reconnect'])
        self.assertEqual(sql.count('SHOW GRANTS'), 1)
        self.assertEqual(sql.count('CURRENT_USER()'), 2)
        self.assertEqual(sql.count("'partial_revokes'"), 2)
        self.assertNotIn('wsrep_gcomm_uuid', sql)
        self.assertEqual(result['partial_revokes'], False)

    def test_grants_galera_status_is_an_explicit_opt_in(self):
        from _grants_fixtures import grants_batch, UUID
        conn = mock.Mock()
        conn.run.return_value = grants_batch(['GRANT SUPER ON *.* TO `app`@`%`'],
                                             version='11.4.13-MariaDB', variables={'wsrep_gcomm_uuid': UUID})
        result = dbops.read_grants(conn, galera_identity=True)
        self.assertTrue(result['checked'], result)
        self.assertEqual(conn.run.call_args[0][0].count(
            "SHOW GLOBAL STATUS WHERE Variable_name = 'wsrep_gcomm_uuid'"), 2)
        conn.run.return_value = grants_batch(['GRANT SUPER ON *.* TO `app`@`%`'],
                                             version='11.4.13-MariaDB', variables={'wsrep_gcomm_uuid': UUID},
                                             after_variables={'wsrep_gcomm_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'})
        self.assertFalse(dbops.read_grants(conn, galera_identity=True)['checked'])

    def test_grants_use_registered_uuid_instead_of_the_version_brand(self):
        from _grants_fixtures import grants_batch
        # MariaDB's --version override can remove its name or impersonate
        # an arbitrary MySQL version. An absent optional variable is valid.
        for version in ('11.8.9-custom', '8.4.11', 'custom-server'):
            with self.subTest(version=version):
                conn = mock.Mock()
                conn.run.return_value = grants_batch(['GRANT SUPER ON *.* TO `app`@`%`'], version=version,
                                                     variables={'server_uuid': None, 'partial_revokes': None})
                self.assertTrue(dbops.read_grants(conn)['checked'])
        # Conversely, a MariaDB-looking VERSION() must not mask a malformed
        # registered UUID on a MySQL server.
        conn.run.return_value = grants_batch(['GRANT SUPER ON *.* TO `app`@`%`'], version='11.8.9-MariaDB',
                                             variables={'server_uuid': 'invalid'})
        self.assertFalse(dbops.read_grants(conn)['checked'])

    def test_grants_refuse_a_server_account_or_session_change(self):
        from _grants_fixtures import grants_batch
        changes = [dict(after_variables={'server_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}),
                   dict(after_variables={'wsrep_gcomm_uuid': 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'}),
                   dict(after_variables={'partial_revokes': 'ON'})]
        changes += [dict(after_identity={i: value}) for i, value in enumerate(
            ['other@client', 'other@%', '9.7.0', '74', '2', 'other-host', '3307'])]
        for change in changes:
            with self.subTest(change=change):
                conn = mock.Mock()
                conn.run.return_value = grants_batch(['GRANT SUPER ON *.* TO `app`@`%`'], **change)
                result = dbops.read_grants(conn)
                self.assertFalse(result['checked'])
                self.assertIsNone(result['account'])
                self.assertEqual(result['grants'], [])
                self.assertIn('changed while reading grants', result['error'])
                conn.run.assert_called_once()

    def test_grants_optional_variables_across_lifecycle_engines(self):
        from _grants_fixtures import UUID
        from _perf_replay import batch
        grant = 'GRANT SELECT ON *.* TO `operator`@`%`'
        cases = [('5.7.44', None), ('8.0.15', None), ('8.0.16', False), ('8.4.11', True), ('9.7.2', False)]
        cases += [(version + '-MariaDB', None) for version in
                  ('10.11.16', '11.0.6', '11.1.6', '11.2.6', '11.3.2', '11.4.13', '11.8.9')]
        for version, partial in cases:
            with self.subTest(version=version):
                # Handwritten result sets, independent of grants_batch: SHOW
                # filters absent variables out rather than raising error 1193.
                variables = [] if 'MariaDB' in version else [['server_uuid', UUID]]
                if partial is not None:
                    variables.insert(0, ['partial_revokes', 'ON' if partial else 'OFF'])
                identity = ['operator@client', 'operator@%', version, '73', '1', 'fixture-db', '3306']
                result_sets = ([['myflames_grants_begin'] + identity], variables, [],
                               [['myflames_grants_rows']], [[grant]],
                               [['myflames_grants_end'] + identity], variables, [], [['myflames_grants_done']])
                conn = mock.Mock()
                conn.run.return_value = ''.join(batch(rows) for rows in result_sets)
                result = dbops.read_grants(conn)
                self.assertTrue(result['checked'], result)
                self.assertEqual((result['user'], result['account'], result['grants']),
                                 ('operator', 'operator@%', [grant]))
                self.assertIs(result['partial_revokes'], partial)
                conn.run.assert_called_once()
                sql = conn.run.call_args[0][0]
                self.assertNotRegex(sql, r'@@(?:global\.)?partial_revokes')
                self.assertEqual(sql.count("SHOW GLOBAL VARIABLES WHERE Variable_name IN "
                                           "('server_uuid', 'partial_revokes')"), 2)

    def test_grants_partial_revokes_and_invalid_identity_are_explicit(self):
        from _grants_fixtures import grants_batch
        for value, expected in [('ON', True), ('1', True), ('OFF', False), ('0', False),
                                (None, None), ('unknown', None)]:
            with self.subTest(value=value):
                conn = mock.Mock()
                conn.run.return_value = grants_batch(['GRANT USAGE ON *.* TO `app`@`%`'],
                                                     variables={'partial_revokes': value})
                result = dbops.read_grants(conn)
                self.assertTrue(result['checked'])
                self.assertIs(result['partial_revokes'], expected)
        for version in ('5.7.44', '8.4.11'):
            conn = mock.Mock()
            conn.run.return_value = grants_batch(['GRANT USAGE ON *.* TO `app`@`%`'], version=version,
                                                 variables={'server_uuid': 'invalid'})
            self.assertFalse(dbops.read_grants(conn)['checked'])
        conn.run.return_value = grants_batch(['GRANT SUPER ON *.* TO `app`@`%`'], version='11.4.13-MariaDB',
                                             variables={'server_uuid': None, 'partial_revokes': None})
        self.assertTrue(dbops.read_grants(conn)['checked'])

    def test_incomplete_grants_batch_never_exposes_an_account(self):
        from _grants_fixtures import grants_batch
        complete = grants_batch(['GRANT USAGE ON *.* TO `app`@`%`'])
        for text in ('', complete.rsplit('myflames_grants_done', 1)[0], complete + 'unexpected\n',
                     complete + 'myflames_grants_done\n'):
            conn = mock.Mock()
            conn.run.return_value = text
            result = dbops.read_grants(conn)
            self.assertEqual((result['checked'], result['account'], result['grants']), (False, None, []))


# ---- redaction -----------------------------------------------------------------------------

SET_PASSWORD_REDACTION_CASES = (
    ('long_executable_assignment',
     "SET PASSWORD /*!50700 FOR '" + 'u' * 600 + "'@'%' = 'private-value' */",
     ('private-value',)),
    ('long_after_apostrophe',
     "it's; SET PASSWORD FOR '" + 'u' * 600 + "'@'%' = 'private-value'",
     ('private-value',)),
    ('long_unbalanced_account',
     "SET PASSWORD FOR 'app@'%'" + ' ' * 600 + "= 'private-value' REPLACE 'old-value'",
     ('private-value', 'old-value')),
    ('quoted_diagnostic_with_account_separator',
     "ERROR 1064 (42000): near 'SET PASSWORD FOR 'a=b'@'%' = 'private-value'' at line 1",
     ('private-value',)),
    ('prose_apostrophe_with_account_separator',
     "Can't apply: SET PASSWORD FOR 'a=b'@'%' = 'private-value'",
     ('private-value',)),
    ('unbalanced_previous_statement',
     "SELECT 'x;\nSET PASSWORD FOR 'a=b'@'%' = 'private-value'",
     ('private-value',)),
    ('double_quoted_prepare',
     'PREPARE s FROM "SET PASSWORD FOR \'a=b\'@\'%\' = \'private-value\'"',
     ('private-value',)),
    ('executable_statement_with_account_separator',
     "/*!50700 SET PASSWORD FOR 'a=b'@'%' = 'private-value' */",
     ('private-value',)),
    ('unfinished_single_character_single_quote',
     "ERROR near 'SET PASSWORD FOR 'a=b'@'%' = 'Z", ('Z',)),
    ('unfinished_single_character_double_quote',
     'ERROR near "SET PASSWORD FOR \'a=b\'@\'%\' = "Z', ('Z',)),
    ('random_replace_current_user', "SET PASSWORD TO RANDOM REPLACE 'old-value'", ('old-value',)),
    ('random_replace_named_user', "SET PASSWORD FOR u TO RANDOM REPLACE 'old-value'", ('old-value',)),
)


# Each entry exercises a different credential clause, including the keyword
# guards that make ordinary editor text inexpensive to scan.
R352_CREDENTIAL_CLAUSES = (
    ('identified_by', 'CREATE USER u IDENTIFIED BY ', ''),
    ('identified_hash', 'CREATE USER u IDENTIFIED BY PASSWORD ', ''),
    ('identified_as', 'ALTER USER u IDENTIFIED WITH auth_plugin AS ', ''),
    ('via_using', 'ALTER USER u IDENTIFIED VIA auth_plugin USING ', ''),
    ('with_using', 'ALTER USER u IDENTIFIED WITH auth_plugin USING ', ''),
    ('via_password', 'ALTER USER u IDENTIFIED VIA auth_plugin USING PASSWORD(', ')'),
    ('password_function', 'SELECT PASSWORD(', ')'),
    ('old_password_function', 'SELECT OLD_PASSWORD(', ')'),
    ('set_password', 'SET PASSWORD = ', ''),
    ('named_set_password', 'SET PASSWORD FOR u = ', ''),
    ('source_password', 'CHANGE REPLICATION SOURCE TO SOURCE_PASSWORD = ', ''),
    ('master_password', 'CHANGE MASTER TO MASTER_PASSWORD = ', ''),
    ('report_password', 'SET GLOBAL report_password = ', ''),
    ('password_option', 'CREATE SERVER s OPTIONS (PASSWORD ', ')'),
    ('random_replace', 'ALTER USER u IDENTIFIED BY RANDOM PASSWORD REPLACE ', ''),
    ('set_random_replace', 'SET PASSWORD TO RANDOM REPLACE ', ''),
)


class TestRedaction(unittest.TestCase):
    def test_r352_url_database_dialect_components_hide_numeric_slash_passwords(self):
        for scheme in ('mysql+pymysql', 'mysql+mysqlconnector',
                       'mariadb+mariadbconnector', 'postgresql+psycopg2',
                       'postgres+asyncpg', 'mysqlx+srv',
                       'pool+mysql', 'pool+mysql+pymysql',
                       'jdbc:mysql+pymysql', 'MySQL+PyMySQL'):
            for quoted in (True, False):
                with self.subTest(scheme=scheme, quoted=quoted):
                    raw = scheme + '://app:12/ab@db1/shop'
                    expected = scheme + '://app:[redacted]@db1/shop'
                    self.assertEqual(dbops.redact(raw, quoted=quoted), expected)
        for raw in ('https://h:443/path?mail=a@b',
                    'https+transport://h:443/path?mail=a@b',
                    'git+https://h:443/path?mail=a@b',
                    'mysql+pymysql://app@db1/shop',
                    'mysql+pymysql://db1:3306/shop'):
            with self.subTest(unchanged=raw):
                self.assertEqual(dbops.redact(raw), raw)

    def test_r352_url_username_brackets_do_not_hide_slash_passwords(self):
        cases = (
            ('mysql://app[ro:pa/ss@db1/x', 'mysql://app[ro:[redacted]@db1/x'),
            ('mysql://a[b:12/xy@h/db', 'mysql://a[b:[redacted]@h/db'),
            ('mysql+pymysql://a[b:12/xy@h/db', 'mysql+pymysql://a[b:[redacted]@h/db'),
            ('mysql://app[ro]:pa/ss@db1/x', 'mysql://app[ro]:[redacted]@db1/x'),
            ('mysql://[app]:pa/ss@db1/x', 'mysql://[app]:[redacted]@db1/x'),
            ('mysql://[app:pa/ss@db1/x', 'mysql://[app:[redacted]@db1/x'),
            ('mysql://[a:12/xy@h/db', 'mysql://[a:[redacted]@h/db'),
            ('mysql://[a:pa]/ss@db1/x', 'mysql://[a:[redacted]@db1/x'),
            ('mysql://u:p@[::1]:3306/db', 'mysql://u:[redacted]@[::1]:3306/db'),
            ('mysql://u:12/ab@[::1]:3306/db', 'mysql://u:[redacted]@[::1]:3306/db'),
            ('mysql://[::1]:3306/db', 'mysql://[::1]:3306/db'),
            ('mysql://[::1]:3306/db?mail=a@b', 'mysql://[::1]:3306/db?mail=a@b'),
            ('mysql://[fe80::1%25eth0]:3306/db?mail=a@b', 'mysql://[fe80::1%25eth0]:3306/db?mail=a@b'),
            ('mysql://user@[::1]:3306/db', 'mysql://user@[::1]:3306/db'),
            ('mysql://user@[::1]:3306/db?mail=a@b', 'mysql://user@[::1]:3306/db?mail=a@b'),
            ('https://[2001:db8::1]/users/alice@example.com',
             'https://[2001:db8::1]/users/alice@example.com'),
        )
        for raw, expected in cases:
            for quoted in (True, False):
                with self.subTest(raw=raw, quoted=quoted):
                    self.assertEqual(dbops.redact(raw, quoted=quoted), expected)

    def test_r352_secret_literals_cover_both_backslash_readings(self):
        for names in (('wsrep_sst_auth', 's3_secret_key'),
                      ('s3_secret_key', 'wsrep_sst_auth'),
                      ('wsrep_sst_auth', 'wsrep_provider_options')):
            for quote in ("'", '"'):
                with self.subTest(names=names, quote=quote):
                    raw = ('SET GLOBAL ' + names[0] + ' = ' + quote + 'mode-first\\' + quote +
                           ', @@global.' + names[1] + ' = ' + quote + 'mode-second' + quote)
                    clean = dbops.redact(raw)
                    self.assertNotIn('mode-first', clean)
                    self.assertNotIn('mode-second', clean)
                    self.assertIn('[redacted]', clean)
        # With escaping the middle quote closes the string. With literal
        # backslashes the adjacent quotes form an escaped quote instead.
        for lead in ('SET GLOBAL ', 'echo '):
            for quote in ("'", '"'):
                with self.subTest(lead=lead, later_no_backslash_end=quote):
                    raw = (lead + 'wsrep_sst_auth = ' + quote + 'mode-head\\' +
                           quote * 2 + 'mode-tail' + quote + '; SELECT 37')
                    clean = dbops.redact(raw)
                    self.assertNotIn('mode-head', clean)
                    self.assertNotIn('mode-tail', clean)
                    self.assertIn('[redacted]', clean)
        ordinary = "SELECT 'it\\'s', 'ordinary'; SET SESSION max_execution_time = 1000"
        self.assertEqual(dbops.redact(ordinary), ordinary)

    def test_r352_localized_wrong_value_errors_use_error_number(self):
        # MySQL messages_to_clients.txt and MariaDB errmsg-utf8.txt use
        # ASCII apostrophes for German, Italian, Japanese and Chinese
        # ER_WRONG_VALUE_FOR_VAR; Chinese has no space before either quote.
        messages = (
            "Variable 'wsrep_sst_auth' kann nicht auf 'locale-private' gesetzt werden",
            "Alla variabile 'wsrep_sst_auth' non puo' essere assegato il valore 'locale-private'",
            "変数 'wsrep_sst_auth' に値 'locale-private' を設定できません。",
            "变量'wsrep_sst_auth'无法设置为'locale-private'的值",
        )
        prefixes = ('ERROR 1231 (42000): ', 'ERROR 1231 (42000) at line 7: ',
                    'ERROR 1231: ', 'mysql client exited 1: ERROR 1231 (42000): ',
                    'mysqlsh exited 1: ERROR 1231 (42000): ',
                    'Could not change parameter: ERROR 1231 (42000): ')
        for message in messages:
            for prefix in prefixes:
                with self.subTest(message=message, prefix=prefix):
                    raw = prefix + message
                    self.assertEqual(dbops.redact(raw), raw.replace('locale-private', '[redacted]'))
        # Chinese 1231 with a colon/secret value (MariaDB errmsg-utf8.txt) and
        # an embedded apostrophe (odd quote count fails closed).
        chinese = "ERROR 1231 (42000): 变量'wsrep_sst_auth'无法设置为'sst:S3cr3t'的值"
        self.assertEqual(dbops.redact(chinese),
                         "ERROR 1231 (42000): 变量'wsrep_sst_auth'无法设置为'[redacted]'的值")
        chinese_apos = "ERROR 1231 (42000): 变量'wsrep_sst_auth'无法设置为'it's S3cr3t'的值"
        clean_apos = dbops.redact(chinese_apos)
        self.assertNotIn('S3cr3t', clean_apos)
        self.assertIn('[redacted]', clean_apos)
        unrelated = 'ERROR 9999 (42000): ' + messages[0]
        self.assertEqual(dbops.redact(unrelated), unrelated)

    def test_r352_error_1231_keeps_ordinary_variables_readable(self):
        # In every translation in both engine trees the quoted variable name
        # comes first. Ordinary recognizable variable names stay readable;
        # secret or unrecognizable names still fail closed.
        ordinary = (
            "ERROR 1231 (42000) at line 1: Variable 'sql_mode' can't be set to the value of 'BOGUS'",
            "ERROR 1231 (42000): Variable 'max_connections' can't be set to the value of '-1'",
            "ERROR 1231 (42000): Variable 'time_zone' kann nicht auf 'Mars/Phobos' gesetzt werden",
            "ERROR 1231 (42000): Alla variabile 'sql_mode' non puo' essere assegato il valore 'BOGUS'",
            "ERROR 1231 (42000): 变量'sql_mode'无法设置为'BOGUS'的值",
            "ERROR 1231 (42000): Variable 'validate_password.policy' can't be set to the value of 'NOPE'",
        )
        for message in ordinary:
            with self.subTest(ordinary=message):
                self.assertEqual(dbops.redact(message), message)
        # Mixed ordinary and secret 1231 lines preserve only the ordinary one.
        mixed = (
            "ERROR 1231 (42000) at line 1: Variable 'sql_mode' can't be set to the value of 'BOGUS'\n"
            "ERROR 1231 (42000): Variable 'wsrep_sst_auth' kann nicht auf 'S3cr3t' gesetzt werden"
        )
        self.assertEqual(dbops.redact(mixed), mixed.replace('S3cr3t', '[redacted]'))
        # A secret variable or unrecognizable/preceded-by-quote name stays hidden.
        for hidden, secret in (
                ("ERROR 1231 (42000) at line 1: Variable 'wsrep_sst_auth' can't be set to the value of 'S3cr3t'", 'S3cr3t'),
                ("ERROR 1231 (42000): Variable 'telemetry.otel_exporter_otlp_logs_headers' kann nicht auf 'k=S3cr3t' gesetzt werden", 'S3cr3t'),
                ("ERROR 1231 (42000): can't'sql_mode' 'S3cr3t'", 'S3cr3t'),
                ("ERROR 1231 (42000): 'unclosed 'sql_mode' 'S3cr3t'", 'S3cr3t'),
                ("ERROR 1231 (42000): unknown 'bad-name' 'S3cr3t'", 'S3cr3t')):
            with self.subTest(hidden=hidden):
                clean = dbops.redact(hidden)
                self.assertNotIn(secret, clean)
                self.assertIn('[redacted]', clean)

    def test_r352_localized_errors_hide_unknown_names_and_every_value(self):
        for message in (
                "ERROR 1231 (42000): 未知 'opaque-name' と 'locale-first' と \"locale-second\"",
                "ERROR 1231 (42000): 'wsrep_sst_auth': 'locale-first' oder \"locale-second\"",
                "ERROR 1231 (42000): Variable 'wsrep_sst_auth' kann nicht auf 'locale-first'locale-second' gesetzt werden",
                "ERROR 1231 (42000): 変数 'wsrep_sst_auth' に値 'locale-first\nlocale-second' を設定できません。"):
            with self.subTest(message=message):
                clean = dbops.redact(message)
                for secret in ('opaque-name', 'locale-first', 'locale-second'):
                    self.assertNotIn(secret, clean)
                self.assertIn('[redacted]', clean)
        raw = ("ERROR 1231 (42000): Variable 'wsrep_sst_auth' kann nicht auf 'locale-private' gesetzt werden\n"
               "ERROR 9999 (42000): 'ordinary-value'")
        clean = dbops.redact(raw)
        self.assertNotIn('locale-private', clean)
        self.assertTrue(clean.endswith("ERROR 9999 (42000): 'ordinary-value'"))

    def test_r352_all_dynamic_credential_clauses_hide_escaped_values(self):
        suffix = '; SELECT 37'
        for label, prefix, tail in R352_CREDENTIAL_CLAUSES:
            for quote in ("'", '"'):
                for escaped in (quote * 2, '\\' + quote):
                    with self.subTest(clause=label, quote=quote, escape=escaped):
                        raw = ('PREPARE s FROM ' + quote + prefix + escaped +
                               'dynamic-private' + escaped + tail +
                               '; SELECT ' + escaped + 'dynamic-tail' + escaped + quote + suffix)
                        clean = dbops.redact(raw)
                        self.assertNotIn('dynamic-private', clean)
                        self.assertNotIn('dynamic-tail', clean)
                        self.assertIn('[redacted]', clean)
                        self.assertTrue(clean.endswith(suffix), clean)

    def test_r352_dynamic_replace_hides_both_credentials(self):
        for quote in ("'", '"'):
            for escaped in (quote * 2, '\\' + quote):
                with self.subTest(quote=quote, escape=escaped):
                    raw = ('PREPARE s FROM ' + quote + 'ALTER USER u IDENTIFIED BY ' +
                           escaped + 'new-private' + escaped + ' REPLACE ' +
                           escaped + 'old-private' + escaped + quote + '; SELECT 37')
                    clean = dbops.redact(raw)
                    self.assertNotIn('new-private', clean)
                    self.assertNotIn('old-private', clean)
                    self.assertTrue(clean.endswith('; SELECT 37'), clean)

    def test_r352_set_password_replace_stays_redacted_across_quote_readings(self):
        # F1: merging backslash and NO_BACKSLASH_ESCAPES spans must not hide
        # the REPLACE keyword from the current-password redaction pass.
        cases = (
            ("SET PASSWORD FOR 'o\\'brien'@'%' = 'bmV3cGFzc3c=' REPLACE 'S3cr3t'",
             ('bmV3cGFzc3c=', 'S3cr3t')),
            ("SET PASSWORD /* don't */ FOR app = 'x=' REPLACE 'S3cr3t'",
             ('x=', 'S3cr3t')),
            ("SET PASSWORD FOR 'it\\'s'@'h' = 'NEW' REPLACE 'CUR=''S3cr3t'",
             ('NEW', 'CUR', 'S3cr3t')),
            ("don't forget: SET PASSWORD = '' REPLACE 'S3cr3t'",
             ('S3cr3t',)),
        )
        for raw, secrets in cases:
            with self.subTest(raw=raw):
                clean = dbops.redact(raw)
                self.assertIn('[redacted]', clean)
                for secret in secrets:
                    self.assertNotIn(secret, clean)
        self.assertEqual(
            dbops.redact("SET PASSWORD FOR 'it\\'s'@'h' = 'NEW' REPLACE 'CUR=''S3cr3t'"),
            "SET PASSWORD FOR 'it\\'s'@'h' = '[redacted]' REPLACE '[redacted]'")
        # Dynamic SQL REPLACE values must pass through the same enclosing-string
        # rule so doubled or backslash-escaped inner quotes cannot leak tails.
        dynamic_cases = (
            "PREPARE s FROM 'SET PASSWORD = \"new-pw\" REPLACE ''S3cr3t'''; SELECT 37",
            "PREPARE s FROM 'SET PASSWORD = \"new-pw\" REPLACE \\'S3cr3t\\''; SELECT 37",
            'PREPARE s FROM "SET PASSWORD = \'new-pw\' REPLACE ""S3cr3t"""; SELECT 37',
            'PREPARE s FROM "SET PASSWORD = \'new-pw\' REPLACE \\"S3cr3t\\""; SELECT 37',
            "PREPARE s FROM 'ALTER USER u IDENTIFIED BY \"new-pw\" REPLACE ''S3cr3t'''; SELECT 37",
        )
        for raw in dynamic_cases:
            with self.subTest(dynamic=raw):
                clean = dbops.redact(raw)
                self.assertNotIn('new-pw', clean)
                self.assertNotIn('S3cr3t', clean)
                self.assertIn('[redacted]', clean)
                self.assertTrue(clean.endswith('; SELECT 37'), clean)
        # Ordinary SQL REPLACE(...) function calls stay intact.
        ordinary = "SET PASSWORD FOR 'o\\'brien'@'%' = 'new-pw'; SELECT REPLACE(col, 'old', 'new')"
        clean = dbops.redact(ordinary)
        self.assertNotIn('new-pw', clean)
        self.assertTrue(clean.endswith("; SELECT REPLACE(col, 'old', 'new')"), clean)

    def test_r352_comment_and_backtick_apostrophes_preserve_later_secret_assignments(self):
        # F2: an apostrophe in a comment or backtick identifier must not open a
        # false enclosing string that skips a following secret assignment.
        body = ('SET STATEMENT wsrep_sst_auth = "sst:first" FOR SELECT 1;\n'
                "SET STATEMENT s3_secret_key = 'S3cr3t' FOR SELECT 1")
        expected_body = ('SET STATEMENT wsrep_sst_auth = "[redacted]" FOR SELECT 1;\n'
                         "SET STATEMENT s3_secret_key = '[redacted]' FOR SELECT 1")
        for prefix in ("-- it's the SST setup\n",
                       "# it's the SST setup\n",
                       "/* it's the SST setup */\n",
                       "SELECT `it's` FROM t;\n"):
            with self.subTest(prefix=prefix):
                self.assertEqual(dbops.redact(prefix + body), prefix + expected_body)
        # Even when an apostrophe appears in plain prose outside any comment or
        # backtick, resuming at match.end() after an extension still hides the
        # second secret assignment.
        prose = ("don't run: SET STATEMENT wsrep_sst_auth = \"sst:first\" FOR SELECT 1; "
                 "SET STATEMENT s3_secret_key = 'S3cr3t' FOR SELECT 1")
        clean_prose = dbops.redact(prose)
        self.assertNotIn('sst:first', clean_prose)
        self.assertNotIn('S3cr3t', clean_prose)
        # Comment and backtick apostrophes also must not break quote parity for
        # a subsequent PREPARE dynamic SQL literal.
        for prefix in ("-- don't forget\n",
                       "# it's\n",
                       "/* isn't */ ",
                       "SELECT `it's` FROM t; "):
            with self.subTest(prepare_prefix=prefix):
                raw = prefix + "PREPARE s FROM 'SET GLOBAL wsrep_sst_auth = ''S3cr3t'''; SELECT 37"
                clean = dbops.redact(raw)
                self.assertNotIn('S3cr3t', clean)
                self.assertTrue(clean.endswith('; SELECT 37'), clean)

    def test_r352_context_scanner_keeps_dynamic_sql_in_comments_fail_closed(self):
        cases = (
            ("-- PREPARE s FROM 'SET GLOBAL wsrep_sst_auth = ''S3cr3t'''\nSELECT 37", '\nSELECT 37'),
            ("/* PREPARE s FROM 'SET GLOBAL wsrep_sst_auth = ''S3cr3t''' */ SELECT 37", ' */ SELECT 37'),
            ("/*!50000 PREPARE s FROM 'SET GLOBAL wsrep_sst_auth = ''S3cr3t''' */ SELECT 37", ' */ SELECT 37'),
            ("/*M!100500 PREPARE s FROM 'SET GLOBAL wsrep_sst_auth = ''S3cr3t''' */ SELECT 37", ' */ SELECT 37'),
            ("Run `PREPARE s FROM 'SET GLOBAL wsrep_sst_auth = ''S3cr3t'''`; SELECT 37", '`; SELECT 37'),
            ("SELECT 1 -- note;SET @q = 'CREATE USER u IDENTIFIED\n  BY ''S3cr3t'''; PREPARE s FROM @q",
             '; PREPARE s FROM @q'),
            ("SELECT 2 # note;PREPARE s FROM 'SET GLOBAL wsrep_sst_auth =\n  ''S3cr3t'''; EXECUTE s",
             '; EXECUTE s'),
        )
        for raw, suffix in cases:
            with self.subTest(raw=raw):
                clean = dbops.redact(raw)
                self.assertNotIn('S3cr3t', clean)
                self.assertIn('[redacted]', clean)
                self.assertTrue(clean.endswith(suffix), clean)

    def test_r352_credential_clauses_hide_encoded_literal_forms(self):
        literals = (("X'616263'", '616263'), ("x'616263'", '616263'),
                    ('0x707731', '707731'), ('0X707731', '707731'),
                    ("B'01110111'", '01110111'), ("b'01110111'", '01110111'),
                    ('0b01110111', '01110111'), ('0B01110111', '01110111'),
                    ("N'encoded-private'", 'encoded-private'),
                    ("n'encoded-private'", 'encoded-private'),
                    ("_utf8mb4'encoded-private'", 'encoded-private'),
                    ("_utf8mb4'encoded-private' COLLATE utf8mb4_bin", 'encoded-private'))
        for label, prefix, tail in R352_CREDENTIAL_CLAUSES:
            for literal, secret in literals:
                with self.subTest(clause=label, literal=literal):
                    raw = prefix + literal + tail + '; SELECT 37'
                    clean = dbops.redact(raw)
                    self.assertNotIn(secret, clean)
                    self.assertIn('[redacted]', clean)
                    self.assertTrue(clean.endswith(tail + '; SELECT 37'), clean)
        ordinary = "SELECT X'616263', 0x707731, B'01110111', 0b01110111, N'public', _utf8mb4'public'"
        self.assertEqual(dbops.redact(ordinary), ordinary)

    def test_r352_encoded_dynamic_credentials_keep_enclosing_boundary(self):
        for label, prefix, tail in R352_CREDENTIAL_CLAUSES:
            for literal, secret in (("X'616263'", '616263'), ('0x707731', '707731'),
                                    ("b'01110111'", '01110111'), ('0b01110111', '01110111'),
                                    ("N'encoded-private'", 'encoded-private'),
                                    ("_utf8mb4'encoded-private' COLLATE utf8mb4_bin", 'encoded-private')):
                for quote in ("'", '"'):
                    for escaped in (quote * 2, '\\' + quote):
                        with self.subTest(clause=label, literal=literal, quote=quote, escape=escaped):
                            inner = (prefix + literal + tail).replace(quote, escaped)
                            raw = 'PREPARE s FROM ' + quote + inner + quote + '; SELECT 37'
                            clean = dbops.redact(raw)
                            self.assertNotIn(secret, clean)
                            self.assertIn('[redacted]', clean)
                            self.assertTrue(clean.endswith('; SELECT 37'), clean)

    def test_r352_every_credential_clause_unions_quote_mode_readings(self):
        # Replacing a short reading before collecting the longer one loses
        # the original quote context and can expose the remainder of a value.
        for label, prefix, tail in R352_CREDENTIAL_CLAUSES:
            for quote in ("'", '"'):
                with self.subTest(clause=label, quote=quote):
                    value = quote + 'mode-head\\' + quote * 2 + 'mode-tail' + quote
                    clean = dbops.redact(prefix + value + tail + '; SELECT 37')
                    self.assertNotIn('mode-head', clean)
                    self.assertNotIn('mode-tail', clean)
                    self.assertIn('[redacted]', clean)

    def test_r352_introduced_set_password_values_preserve_accounts(self):
        long_account = "'" + 'account' * 80 + "=b'@'%'"
        prefixes = (
            "SET PASSWORD FOR 'a=b'@'%' = ",
            'SET PASSWORD FOR ' + long_account + ' = ',
            "SET PASSWORD FOR 'a=b'@'%' /* account policy */ = ",
            'SET PASSWORD /* account policy */ FOR ' + long_account + ' /* assignment */ = ',
        )
        for prefix in prefixes:
            for literal, secret in (("X'616263'", '616263'), ('0x707731', '707731'),
                                    ("B'01110111'", '01110111'), ('0b01110111', '01110111'),
                                    ("N'introduced-private'", 'introduced-private'),
                                    ("_utf8mb4'introduced-private' COLLATE utf8mb4_bin",
                                     'introduced-private')):
                with self.subTest(prefix=prefix, literal=literal):
                    clean = dbops.redact(prefix + literal + '; SELECT 37')
                    self.assertNotIn(secret, clean)
                    self.assertIn('[redacted]', clean)
                    self.assertTrue(clean.startswith(prefix), clean)
                    self.assertTrue(clean.endswith('; SELECT 37'), clean)

    def test_r352_secret_assignments_after_ambiguous_ordinary_strings(self):
        for quote in ("'", '"'):
            for close in (quote, quote * 2):
                for name in ('wsrep_sst_auth', 's3_secret_key'):
                    with self.subTest(quote=quote, close=close, name=name):
                        # One close exposes SET only with literal backslashes;
                        # two closes expose it only with backslash escapes.
                        prefix = 'SELECT ' + quote + 'ordinary\\' + close + '; '
                        raw = prefix + 'SET GLOBAL ' + name + ' = reading_private; SELECT 37'
                        clean = dbops.redact(raw)
                        self.assertNotIn('reading_private', clean)
                        self.assertIn('[redacted]', clean)

    def test_r352_localized_errors_hide_unfinished_values_across_error_lines(self):
        for translated in (
                "Variable 'wsrep_sst_auth' kann nicht auf 'locale-unfinished",
                "変数 'wsrep_sst_auth' に値 'locale-unfinished",
                "不明 'unknown-private' と 'locale-unfinished"):
            with self.subTest(translated=translated):
                raw = 'ERROR 1231 (42000): ' + translated
                clean = dbops.redact(raw)
                self.assertNotIn('locale-unfinished', clean)
                self.assertNotIn('unknown-private', clean)
                self.assertIn('[redacted]', clean)
        raw = ("ERROR 1231 (42000): Variable 'wsrep_sst_auth' kann nicht auf 'locale-first\n"
               "ERROR 9999 (42000): 'ordinary-value'\n"
               "mysql client exited 1: ERROR 1231 (42000): 変数 's3_secret_key' に値 'locale-last'")
        clean = dbops.redact(raw)
        self.assertNotIn('locale-first', clean)
        self.assertNotIn('locale-last', clean)
        self.assertIn("ERROR 9999 (42000): 'ordinary-value'", clean)
        self.assertIn('s3_secret_key', clean)

    def test_set_password_diagnostic_and_random_replace_regressions(self):
        for label, raw, secrets in SET_PASSWORD_REDACTION_CASES:
            with self.subTest(case=label):
                clean = dbops.redact(raw)
                self.assertIn('[redacted]', clean)
                for secret in secrets:
                    self.assertNotIn(secret, clean)

    def test_added_set_password_scans_leave_empty_literals_alone(self):
        for scan in (dbops._set_password_separator_spans, dbops._set_password_keyword_spans):
            for raw in ("SET PASSWORD = ''", 'SET PASSWORD = ""',
                        "SET PASSWORD = '", 'SET PASSWORD = "',
                        "PREPARE s FROM 'SET PASSWORD FOR ''app''@''%'' = ''private-value'''"):
                with self.subTest(scan=scan.__name__, raw=raw):
                    self.assertEqual(list(scan(raw)), [])

    def test_added_set_password_scans_keep_unfinished_one_character_secrets(self):
        for scan in (dbops._set_password_separator_spans, dbops._set_password_keyword_spans):
            for quote in ("'", '"'):
                with self.subTest(scan=scan.__name__, quote=quote):
                    prefix = 'SET PASSWORD FOR u = '
                    raw = prefix + quote + 'Z'
                    self.assertEqual(list(scan(raw)), [(len(prefix), len(raw))])

    def test_set_password_union_drops_the_whole_overlapping_literal(self):
        # The lexical pass starts at the single quote; the raw-separator pass
        # starts inside it and extends past its end to the final double quote.
        raw = "SET PASSWORD FOR 'a=b'@'%' = 'first SET PASSWORD = \"overlap' tail-secret\""
        self.assertEqual(dbops._redact_set_password_all(raw),
                         "SET PASSWORD FOR 'a=b'@'%' = '[redacted]'")
        self.assertEqual(dbops.redact(raw), "SET PASSWORD FOR 'a=b'@'%' = '[redacted]'")

    def test_megabyte_added_set_password_scans_finish_within_two_seconds(self):
        shapes = {
            'repeated_keywords': ('', 'SET PASSWORD ', ''),
            'long_account': ("SET PASSWORD FOR '", 'u', "'@'%' = 'private-value'"),
            'unfinished_quote': ("SET PASSWORD FOR '", 'SET PASSWORD ', ''),
            'unfinished_comment': ('SET PASSWORD /*', 'SET PASSWORD ', ''),
            'dense_assignments': ('', "SET PASSWORD FOR 'u'@'%' = 'private-value';", ''),
            'dense_overlaps': ('',
                "SET PASSWORD FOR 'a=b'@'%' = 'first SET PASSWORD = \"overlap' tail-secret\";", ''),
        }
        for helper in ('_set_password_separator_spans', '_set_password_keyword_spans',
                       '_redact_set_password_all'):
            for shape, (prefix, repeated, tail) in shapes.items():
                with self.subTest(helper=helper, shape=shape):
                    code = (
                        "import time\nfrom myflames import dbops\n"
                        "n = 1024 * 1024\n"
                        "prefix, repeated, tail = " + repr((prefix, repeated, tail)) + "\n"
                        "value = prefix + (repeated * (n // len(repeated) + 1))[:n - len(prefix) - len(tail)] + tail\n"
                        "assert len(value) == n, len(value)\n"
                        "began = time.monotonic()\n"
                        "result = dbops." + helper + "(value)\n"
                        "if not isinstance(result, str):\n"
                        "    result = list(result)\n"
                        "elapsed = time.monotonic() - began\n"
                        "print('%.6f' % elapsed)\n")
                    try:
                        result = subprocess.run([sys.executable, '-c', code],
                            cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
                    except subprocess.TimeoutExpired:
                        self.fail('%s exceeded the 5 s subprocess deadline for %s' % (helper, shape))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    elapsed = float(result.stdout.strip())
                    print('REDACTION_TIMING %s %s %.6fs' % (shape, helper, elapsed), flush=True)
                    self.assertLess(elapsed, 2)

    def test_megabyte_hostile_shapes_finish_within_deadline(self):
        # Each helper gets its own process: a super-linear regex must fail
        # within a fixed deadline rather than stall the entire test worker.
        shapes = {
            'password_spaces': "'PASSWORD' + ' ' * (n - 9) + 'x'",
            'set_password': "('SET PASSWORD ' * (n // 13 + 1))[:n]",
            'identified_require': "('IDENTIFIED REQUIRE ' * (n // 19 + 1))[:n]",
        }
        for label, quote in (('single', "'"), ('double', '"'), ('backtick', '`')):
            prefix = repr(quote)
            pair = repr('\\' + quote)
            shapes[label + '_quote_pairs'] = prefix + ' + ' + pair + " * ((n - 2) // 2) + 'x'"
            shapes[label + '_lone_backslash'] = prefix + ' + ' + pair + ' * ((n - 2) // 2) + chr(92)'
            shapes[label + '_backslash_newline'] = prefix + ' + ' + pair + " * ((n - 4) // 2) + chr(92) + '\\nx'"
            shapes[label + '_combined'] = ("('IDENTIFIED REQUIRE ' * (n // 57)) + (" + prefix +
                                           ' + ' + pair + ' * n)[:n - 19 * (n // 57)]')
        for shape, expression in shapes.items():
            for helper in ('redact', 'strip_credentials'):
                with self.subTest(shape=shape, helper=helper):
                    code = (
                        "import time\nfrom myflames import dbops\n"
                        "n = 1024 * 1024\n"
                        "value = " + expression + "\n"
                        "assert len(value) == n, len(value)\n"
                        "began = time.monotonic()\n"
                        "clean = dbops." + helper + "(value)\n"
                        "elapsed = time.monotonic() - began\n"
                        "expected = value\n"
                        "if " + repr(helper) + " == 'strip_credentials':\n"
                        "    if " + repr(shape) + " == 'identified_require':\n"
                        "        expected = ' '.join(value.replace('IDENTIFIED ', '').split())\n"
                        "    elif " + repr(shape) + ".endswith('_combined'):\n"
                        "        count = n // 57\n"
                        "        expected = ' '.join(['REQUIRE'] * count + [value[19 * count:]])\n"
                        "assert clean == expected, (len(clean), len(expected))\n"
                        "print('%.6f' % elapsed)\n")
                    try:
                        result = subprocess.run([sys.executable, '-c', code],
                            cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
                    except subprocess.TimeoutExpired:
                        print('REDACTION_TIMING %s %s >5s' % (shape, helper), flush=True)
                        self.fail('%s exceeded the 5 s subprocess deadline for %s' % (helper, shape))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    elapsed = float(result.stdout.strip())
                    print('REDACTION_TIMING %s %s %.6fs' % (shape, helper, elapsed), flush=True)
                    self.assertLess(elapsed, 4)

    def test_r352_set_password_quoted_accounts_finish_within_one_second(self):
        # The leading assignment makes the full SET PASSWORD matcher run.
        # A failing suffix used to retry every way of splitting doubled
        # quotes into adjacent literals, despite the 512-token limit.
        shapes = (
            ('single_doubled', "'a'", ';'),
            ('double_doubled', '"a"', ';'),
            ('backtick_doubled', '`a`', ';'),
            ('backslashes', "'a\\\\b'", ';'),
            ('unterminated', "'a'", "'unfinished\\"),
        )
        for shape, unit, suffix in shapes:
            with self.subTest(shape=shape):
                code = (
                    "import time\nfrom myflames import dbops\n"
                    "size = 400 * 1024\n"
                    "prefix = 'x = 1; SET PASSWORD FOR '\n"
                    "unit, suffix = " + repr((unit, suffix)) + "\n"
                    "copies, padding = divmod(size - len(prefix) - len(suffix), len(unit))\n"
                    "value = prefix + unit * copies + ' ' * padding + suffix\n"
                    "assert len(value) == size\n"
                    "began = time.monotonic()\n"
                    "clean = dbops.redact(value)\n"
                    "elapsed = time.monotonic() - began\n"
                    "assert clean == value, (len(clean), len(value))\n"
                    "print(elapsed)\n")
                try:
                    result = subprocess.run([sys.executable, '-c', code],
                        cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, timeout=3, universal_newlines=True)
                except subprocess.TimeoutExpired:
                    self.fail('SET PASSWORD %s exceeded the 3 s deadline for 400 KB' % shape)
                self.assertEqual(result.returncode, 0, result.stderr)
                elapsed = float(result.stdout.strip())
                print('R352_SET_PASSWORD_TIMING %s bytes=409600 elapsed=%.6fs' %
                      (shape, elapsed), flush=True)
                self.assertLess(elapsed, 1)

    def test_r352_atomic_account_tokens_preserve_credential_values(self):
        # Each account is longer than the diagnostic fallback's 512-byte
        # bound. The token walk must still reach introduced/encoded values
        # after doubled quotes, without mistaking an account's '=' for it.
        for quote in ("'", '"', '`'):
            account = quote + ('a=b' + quote * 2) * 200 + 'z' + quote
            prefix = 'SET PASSWORD FOR ' + account + "@'%' = "
            for value in ("_utf8mb4'atomic-private'", "X'61746f6d6963'", '0x61746f6d6963'):
                with self.subTest(quote=quote, value=value):
                    self.assertEqual(dbops.redact(prefix + value + '; SELECT 1'),
                                     prefix + "'[redacted]'; SELECT 1")

    def test_redaction_clause_outputs_remain_stable(self):
        cases = (
            ("SOURCE_PASSWORD = 'x'", "SOURCE_PASSWORD = '[redacted]'"),
            ("MASTER_PASSWORD='x'", "MASTER_PASSWORD='[redacted]'"),
            ("PASSWORD 'x'", "PASSWORD '[redacted]'"),
            ("OPTIONS (PASSWORD   'x')", "OPTIONS (PASSWORD   '[redacted]')"),
            ("password\t=\n 'x'", "password\t=\n '[redacted]'"),
            ('report_password = "x"', 'report_password = "[redacted]"'),
            ("'caching_sha2_password' AS 'h'", "'caching_sha2_password' AS 'h'"),
            ("PASSWORD == 'x'", "PASSWORD == 'x'"),
            ("SET PASSWORD FOR `a`@`%` = 'x'", "SET PASSWORD FOR `a`@`%` = '[redacted]'"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(dbops.redact(raw), expected)

    def test_set_password_for_long_quoted_account_stays_redacted(self):
        from myflames import privileges
        # MySQL accepts up to 255 host bytes; identifier quoting doubles
        # backticks, so valid account SQL can exceed 512 characters.
        account = privileges.quote_account('u' * 32, '`' * 255)
        prefix = 'SET PASSWORD FOR ' + account + ' = '
        self.assertEqual(dbops.redact(prefix + "'private-value'"), prefix + "'[redacted]'")

    def test_set_password_after_a_quoted_command_stays_redacted(self):
        raw = "SELECT 'SET PASSWORD'; SET PASSWORD FOR 'u'@'%' = 'SECRET';"
        self.assertEqual(dbops._redact_set_password(raw), raw.replace("'SECRET'", "'[redacted]'"))
        self.assertNotIn('SECRET', dbops.redact(raw))
        # Delimiters inside the account prevent the bounded fallback from
        # hiding a scanner regression. Every prefix must keep its quote context.
        for before in ("SELECT 'SET PASSWORD'; ", 'SELECT "SET PASSWORD"; ',
                       "SELECT 'SET PASSWORD \\'text'; ",
                       'SELECT "SET PASSWORD \\"text"; ',
                       "SELECT 'SET PASSWORD ''text'; ",
                       'SELECT "SET PASSWORD ""text"; ',
                       'SELECT `SET PASSWORD ``text`; ',
                       '/* SET PASSWORD */ ', '-- SET PASSWORD\n', '# SET PASSWORD\n'):
            with self.subTest(before=before):
                raw = before + "SET PASSWORD FOR 'u=;name'@'%' = 'SECRET';"
                self.assertEqual(dbops._redact_set_password(raw), raw.replace("'SECRET'", "'[redacted]'"))
                self.assertNotIn('SECRET', dbops.redact(raw))

    def test_set_password_redacts_multiple_commands(self):
        raw = ("SET PASSWORD FOR 'a=b'@'%' = 'first-private'; "
               'SET PASSWORD /* x=1; */ FOR `a;b`@`%` = "second-private";')
        self.assertEqual(dbops.redact(raw),
                         raw.replace('first-private', '[redacted]').replace('second-private', '[redacted]'))

    def test_set_password_assignment_in_literal_cannot_hide_real_command(self):
        before = 'SELECT "SET PASSWORD = \'x\'"; '
        # The lexical pass leaves the example literal alone; the public
        # redactor may also redact it as a conservative diagnostic fallback.
        self.assertEqual(dbops._redact_set_password(before), before)
        raw = before + "SET PASSWORD FOR 'u=;name'@'%' = 'SECRET';"
        clean = dbops.redact(raw)
        self.assertNotIn('SECRET', clean)
        self.assertIn("SET PASSWORD FOR 'u=;name'@'%' = '[redacted]';", clean)

    def test_set_password_in_quoted_diagnostic_stays_redacted(self):
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                raw = ("ERROR 1064 (42000): syntax error near " + quote +
                       "SET PASSWORD FOR `u`@`%` = 'private-value';" + quote + " at line 1")
                self.assertEqual(dbops.redact(raw), raw.replace('private-value', '[redacted]'))

    def test_megabyte_quoted_set_password_runs_finish(self):
        for shape in ('quoted_commands', 'unfinished_single', 'unfinished_double'):
            with self.subTest(shape=shape):
                code = (
                    "import time\nfrom myflames import dbops\n"
                    "n = 1024 * 1024\n"
                    "shape = " + repr(shape) + "\n"
                    "if shape == 'quoted_commands':\n"
                    "    tail = \" SET PASSWORD FOR 'u=;name'@'%' = 'private-value';\"\n"
                    "    repeated = \"'SET PASSWORD';\"\n"
                    "    value = (repeated * ((n - len(tail)) // len(repeated))).ljust(n - len(tail)) + tail\n"
                    "else:\n"
                    "    quote = chr(39) if shape == 'unfinished_single' else chr(34)\n"
                    "    value = quote + ('SET PASSWORD ' * (n // 13 + 1))[:n - 2] + chr(92)\n"
                    "assert len(value) == n, len(value)\n"
                    "began = time.monotonic()\n"
                    "clean = dbops.redact(value)\n"
                    "elapsed = time.monotonic() - began\n"
                    "if shape == 'quoted_commands':\n"
                    "    assert 'private-value' not in clean\n"
                    "    assert '[redacted]' in clean\n"
                    "else:\n"
                    "    assert clean == value\n"
                    "print(elapsed)\n")
                result = subprocess.run([sys.executable, '-c', code],
                    cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=20, universal_newlines=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                elapsed = float(result.stdout.strip())
                print('REDACTION_TIMING %s redact %.6fs' % (shape, elapsed), flush=True)
                self.assertLess(elapsed, 15)

    def test_set_password_skips_quoted_names_and_comments_before_assignment(self):
        for prefix in ("SET PASSWORD FOR 'a=b'@'%' = ",
                       'SET PASSWORD FOR `a;b`@`%` = ',
                       'SET PASSWORD FOR "a=;b"@"%" = ',
                       "SET PASSWORD FOR 'a''=b'@'%' = ",
                       'SET PASSWORD /* x=1; */ = ',
                       'SET PASSWORD -- x=1;\n = ',
                       'SET PASSWORD # x=1;\n = ',
                       "ERROR near 'SET PASSWORD FOR `u`@`%` = "):
            with self.subTest(prefix=prefix):
                self.assertEqual(dbops.redact(prefix + "'private-value'"), prefix + "'[redacted]'")
                self.assertEqual(dbops.redact(prefix + "'private-value' REPLACE 'old-value'"),
                                 prefix + "'[redacted]' REPLACE '[redacted]'")

    def test_long_set_password_scanner_respects_assignment_boundaries(self):
        prefix = 'SET PASSWORD' + ' ' * 513 + 'FOR `u`@`%` = '
        self.assertEqual(dbops.redact(prefix + "'private-value' REPLACE 'old-value'"),
                         prefix + "'[redacted]' REPLACE '[redacted]'")
        repeated = 'SET PASSWORD ' * 100
        self.assertEqual(dbops.redact(repeated + "= 'private-value'"), repeated + "= '[redacted]'")
        # A semicolon ends the candidate, so a later ordinary assignment
        # does not inherit its credential context.
        unrelated = repeated + "; SELECT ordinary = 'public-value'"
        self.assertEqual(dbops.redact(unrelated), unrelated)
        self.assertEqual(dbops.redact(unrelated + '; ' + prefix + "'private-value'"),
                         unrelated + '; ' + prefix + "'[redacted]'")

    def test_megabyte_set_password_prefixes_with_assignment_finish(self):
        code = (
            "import time\nfrom myflames import dbops\n"
            "tail = \"= 'private-value'\"\n"
            "prefix = ('SET PASSWORD ' * (1024 * 1024 // 13 + 1))[:1024 * 1024 - len(tail)]\n"
            "began = time.monotonic()\n"
            "clean = dbops.redact(prefix + tail)\n"
            "elapsed = time.monotonic() - began\n"
            "assert clean == prefix + \"= '[redacted]'\"\n"
            "print(elapsed)\n")
        result = subprocess.run([sys.executable, '-c', code], cwd=str(Path(__file__).resolve().parent.parent),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(float(result.stdout.strip()), 12)

    def test_strip_grant_outputs_remain_stable(self):
        cases = (
            ("CREATE USER `a`@`%` IDENTIFIED WITH 'caching_sha2_password' AS '$A$005$x''y\\'z' "
             "REQUIRE NONE PASSWORD EXPIRE DEFAULT", "CREATE USER `a`@`%` REQUIRE NONE PASSWORD EXPIRE DEFAULT"),
            ("GRANT USAGE ON *.* TO `a`@`%` IDENTIFIED BY PASSWORD '*HASH' WITH GRANT OPTION",
             "GRANT USAGE ON *.* TO `a`@`%` WITH GRANT OPTION"),
            ("CREATE USER `m`@`%` IDENTIFIED VIA ed25519 USING 'k' OR unix_socket REQUIRE SSL",
             "CREATE USER `m`@`%` REQUIRE SSL"),
            ("CREATE USER `f`@`%` IDENTIFIED WITH 'a' AS 'h1' AND IDENTIFIED WITH 'b' AS 'h2' REQUIRE NONE",
             "CREATE USER `f`@`%` REQUIRE NONE"),
            ("GRANT SELECT ON `db`.* TO `a`@`%`", "GRANT SELECT ON `db`.* TO `a`@`%`"),
            ("GRANT USAGE ON *.* TO `a`@`%` IDENTIFIED BY PASSWORD '*H'", "GRANT USAGE ON *.* TO `a`@`%`"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(dbops.strip_credentials(raw), expected)

    def test_unterminated_literal_tails_remain_secret(self):
        for quote in ("'", '"'):
            for tail in ('\\', '\\\n'):
                with self.subTest(quote=quote, tail=tail):
                    raw = 'IDENTIFIED BY ' + quote + 'private-value' + tail
                    self.assertEqual(dbops.redact(raw), 'IDENTIFIED BY ' + quote + '[redacted]' + quote)
        for quote in ("'", '"', '`'):
            for tail in ('\\', '\\\nREQUIRE NONE'):
                with self.subTest(quote=quote, tail=tail):
                    raw = 'CREATE USER u IDENTIFIED BY ' + quote + 'private-value' + tail
                    self.assertEqual(dbops.strip_credentials(raw), 'CREATE USER u')

    def test_unterminated_quoted_runs_are_single_grant_tokens(self):
        for quote in ("'", '"', '`'):
            for tail in ('', '\\', '\\\nREQUIRE NONE'):
                with self.subTest(quote=quote, tail=tail):
                    # Identifier quotes are doubled; backslashes in them
                    # are literal, including immediately before a close.
                    escaped = 'private\\' + quote if quote == "'" else 'private' + quote * 2
                    raw = quote + escaped * 3 + tail
                    self.assertEqual(dbops.GRANT_TOKEN.findall(raw), [raw])

    def test_identifier_final_backslash_does_not_hide_credential_clause(self):
        for quote in ('`', '"'):
            with self.subTest(quote=quote):
                account = quote + 'user\\' + quote + '@' + quote + '%' + quote
                prefix = 'GRANT USAGE ON *.* TO ' + account
                raw = prefix + " IDENTIFIED BY 'private-value' REQUIRE NONE"
                self.assertEqual(dbops.strip_credentials(raw), prefix + ' REQUIRE NONE')

    def test_url_secret_scan_bounds_unfinished_megabyte_credentials(self):
        # Bound the test process as well as the measured regex so a regression
        # cannot monopolize a worker for minutes.
        code = (
            "import time\nfrom myflames import dbops\n"
            "value = ('mysql://a:b,' * (1024 * 1024 // 12 + 1))[:1024 * 1024]\n"
            "began = time.monotonic()\n"
            "assert not list(dbops._URL_SECRET(value))\n"
            "print(time.monotonic() - began)\n")
        result = subprocess.run([sys.executable, '-c', code], cwd=str(Path(__file__).resolve().parent.parent),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(float(result.stdout.strip()), 5)

    def test_url_secret_scan_bounds_megabyte_digit_dot_scheme_run(self):
        code = (
            "import time\nfrom myflames import dbops\n"
            "n = 1024 * 1024\nvalue = ('1.' * n)[:n - 3] + '://'\n"
            "began = time.monotonic()\n"
            "assert not list(dbops._URL_SECRET(value))\n"
            "assert not list(dbops._URL_SECRET(value, quoted=False))\n"
            "print(time.monotonic() - began)\n")
        result = subprocess.run([sys.executable, '-c', code], cwd=str(Path(__file__).resolve().parent.parent),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, universal_newlines=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        elapsed = float(result.stdout.strip())
        print('URL_DIGIT_RUN_TIMING %.6fs' % elapsed, flush=True)
        self.assertLess(elapsed, 5)

    def test_url_redaction_starts_at_the_whole_scheme_character_run(self):
        for prefix in ('-', '+', '1.', '10.0.0.1-'):
            with self.subTest(prefix=prefix):
                self.assertEqual(dbops.redact(prefix + 'mysql://app:SECRET@db1'),
                                 prefix + 'mysql://app:[redacted]@db1')
        for value in ('gcomm://10.0.0.1,10.0.0.2', 'https://user@host/path', '1.2://app:public@host'):
            self.assertEqual(dbops.redact(value), value)

    def test_bounded_url_fields_still_redact_supported_schemes_and_pool_prefixes(self):
        for scheme in ('mysql', 'mariadb', 'postgres', 'postgresql'):
            for prefix in ('', '3*'):
                with self.subTest(scheme=scheme, prefix=prefix):
                    raw = prefix + scheme + '://' + 'u' * 255 + ':' + 'p' * 1024 + '@db'
                    clean = dbops.redact(raw)
                    self.assertEqual(clean, prefix + scheme + '://' + 'u' * 255 + ':[redacted]@db')

    def test_url_secret_scan_has_no_credential_length_limit(self):
        for scheme in ('mysql', 'mariadb', 'postgres', 'postgresql', 'https'):
            for username in ('', 'user', 'u' * 5000):
                with self.subTest(scheme=scheme, username_length=len(username)):
                    raw = '3*' + scheme + '://' + username + ':' + 'p' * 5000 + '@db/path'
                    self.assertEqual(dbops.redact(raw), '3*' + scheme + '://' + username + ':[redacted]@db/path')

    def test_url_secret_scan_does_not_cross_path_or_quote_delimiters(self):
        for boundary in (' ', '\n', "'", '"', '`'):
            with self.subTest(boundary=boundary):
                raw = 'mysql://user:part' + boundary + 'path@host'
                self.assertEqual(dbops.redact(raw), raw)
        self.assertEqual(dbops.redact('mysql://user:part%2Fsecret@host'), 'mysql://user:[redacted]@host')
        self.assertEqual(dbops.redact('mysql://unfinished:mysql://user:secret@host'),
                         'mysql://unfinished:[redacted]@host')

    def test_gate351_url_slash_password_and_noncredential_paths(self):
        for password in ('pa/SECRET', 'Zq9/S3cr3t', 'pa/a@b/c@SECRET', '/SECRET'):
            with self.subTest(password=password):
                value = "mysql://fed:" + password + "@remote:3306/db/t"
                expected = "mysql://fed:[redacted]@remote:3306/db/t"
                self.assertEqual(dbops.redact(value), expected)
                sql = "CREATE TABLE t (a int) ENGINE=FEDERATED CONNECTION='%s'" % value
                self.assertEqual(dbops.redact(sql), sql.replace(value, expected))
        # Database schemes deliberately fail closed: digits before a slash
        # might be a password, even when the URL could also mean a host port.
        self.assertEqual(dbops.redact('mysql://h:3306/db?mail=a@b'),
                         'mysql://h:[redacted]@b')
        for value in ('mysql://u@h:3306/db', 'http://h/a@b', 'https://h:443/db?mail=a@b'):
            with self.subTest(value=value):
                self.assertEqual(dbops.redact(value), value)

    def test_gate351_secret_variable_assignments(self):
        from myflames.param_compare import _SECRET_VARIABLES, sensitive
        self.assertIs(_SECRET_VARIABLES, dbops._SECRET_VARIABLES)
        self.assertIs(sensitive, dbops.sensitive)
        for name in sorted(_SECRET_VARIABLES):
            with self.subTest(name=name):
                self.assertTrue(sensitive(name.upper()))
                for prefix in ('SET ', 'SET GLOBAL ', 'SET SESSION ', 'SET PERSIST ', 'SET PERSIST_ONLY ',
                               'SET @@', 'SET @@global.', 'SET @@session.', 'SET @@persist.',
                               'SET @@persist_only.'):
                    for identifier in (name.upper(), '`' + name + '`'):
                        for literal in ("'gate351-secret'", '"gate351-secret"', 'gate351-secret', '987654321'):
                            statement = prefix + identifier + ' = ' + literal
                            clean = dbops.redact(statement)
                            self.assertNotIn(literal.strip('\'"'), clean)
                            self.assertIn('[redacted]', clean)
                            self.assertTrue(clean.startswith(prefix + identifier + ' = '))
        self.assertEqual(dbops.redact('SET GLOBAL max_connections = 500'), 'SET GLOBAL max_connections = 500')

    def test_gate351_secret_assignments_accept_comments_and_quoted_components(self):
        for assignment in (
                'SET GLOBAL wsrep_sst_auth /* auth */ = ',
                'SET SESSION `wsrep_sst_auth` # auth\n = ',
                'SET PERSIST wsrep_sst_auth -- auth\n = ',
                'SET @@global /* scope */ . /* name */ `wsrep_sst_auth` /* auth */ = /* value */ ',
                'SET GLOBAL `telemetry`.`otel_exporter_otlp_logs_headers` = ',
                'SET GLOBAL telemetry.`otel_exporter_otlp_logs_headers` = ',
                'SET GLOBAL `telemetry`.otel_exporter_otlp_logs_headers = ',
                'SET @@global.`telemetry` /* namespace */ . /* component */ `otel_exporter_otlp_logs_headers` = '):
            with self.subTest(assignment=assignment):
                self.assertEqual(dbops.redact(assignment + "'gate351-comment-secret'"),
                                 assignment + "'[redacted]'")
        nonsecret = 'SET GLOBAL max_connections /* limit */ = 500'
        self.assertEqual(dbops.redact(nonsecret), nonsecret)

    def test_gate351_multiple_secret_assignments_keep_nonsecret_values(self):
        statement = ("SET GLOBAL max_connections = 500, wsrep_sst_auth = 'sst:gate351-secret', "
                     "@@session.`s3_secret_key` = \"key,gate351-tail\", report_password = raw-gate351")
        clean = dbops.redact(statement)
        for secret in ('sst:', 'gate351-secret', 'key,gate351-tail', 'raw-gate351'):
            self.assertNotIn(secret, clean)
        self.assertEqual(clean.count('[redacted]'), 3)
        self.assertIn('max_connections = 500', clean)

    def test_gate351_executable_comments_redact_secret_assignments(self):
        for opener in ('/*!80000 ', '/*! ', '/*!', '/*M!100100 ', '/*M! '):
            for name in sorted(dbops._SECRET_VARIABLES):
                for value in ("'sst:private'", '"sst:private"', "CONCAT('sst:', 'private')"):
                    with self.subTest(opener=opener, name=name, value=value):
                        statement = opener + 'SET GLOBAL `' + name + '` = ' + value + ' */'
                        quote = value[0] if value[0] in ('\'', '"') else ''
                        expected = (opener + 'SET GLOBAL `' + name + '` = ' + quote +
                                    '[redacted]' + quote + ' */')
                        self.assertEqual(dbops.redact(statement), expected)
        statements = (
            "SET /*!80000 GLOBAL */ wsrep_sst_auth = 'sst:private'",
            "SET GLOBAL wsrep_sst_auth /*!80000 = 'sst:private' */; SELECT 1",
            "/*!80000 SET GLOBAL max_connections = 500, wsrep_sst_auth = 'sst:private' */; "
            "/*M!100100 SET GLOBAL s3_secret_key = 'sst:private' */",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                self.assertEqual(dbops.redact(statement), statement.replace('sst:private', '[redacted]'))
        for prefix, name in (('', 'SET GLOBAL wsrep_sst_auth'),
                             ('SET ', 'wsrep_sst_auth'), ('SET GLOBAL ', 'wsrep_sst_auth'),
                             ('SET @@', 'global.wsrep_sst_auth'),
                             ('SET @@global', '.wsrep_sst_auth'),
                             ('SET @@global.', 'wsrep_sst_auth'),
                             ('SET GLOBAL telemetry.', 'otel_exporter_otlp_logs_headers')):
            for marker in ('/*M!100100 ', '/*!800000 ', '/*M!100100'):
                with self.subTest(prefix=prefix, name=name, marker=marker):
                    statement = prefix + marker + name + " = CONCAT('sst:', 'private') */"
                    expected = prefix + marker + name + " = [redacted] */"
                    self.assertEqual(dbops._redact_secret_variables(statement), expected)
                    self.assertEqual(dbops.redact(statement), expected)

        # Ordinary comments and quoted examples stay inert to the lexical
        # scanner. The context-free fallback separately redacts quoted
        # inventory assignments even inside comments and diagnostic text.
        for statement in (
                "/* SET GLOBAL wsrep_sst_auth = 'sst:private' */ SELECT 1",
                "-- SET GLOBAL wsrep_sst_auth = 'sst:private'\nSELECT 1",
                "# SET GLOBAL wsrep_sst_auth = 'sst:private'\nSELECT 1",
                "SELECT '/*!80000 SET GLOBAL wsrep_sst_auth = ''sst:private'' */'",
                "/*!80000 SET GLOBAL max_connections = 500 */"):
            with self.subTest(statement=statement):
                self.assertEqual(dbops._redact_secret_variables(statement), statement)

    def test_gate351_rejected_secret_variable_values_are_redacted(self):
        for name in sorted(dbops._SECRET_VARIABLES):
            for value in ('sst:private', "sst:pa'ss", "sst:pa''ss", 'sst:pa\\ss', ''):
                with self.subTest(name=name, value=value):
                    prefix = "ERROR 1231 (42000): Variable '%s' can't be set to the value of '" % name.upper()
                    self.assertEqual(dbops.redact(prefix + value + "'"), prefix + "[redacted]'")
        for name in ('max_connections', 'password_history', 'require_secure_transport'):
            message = "Variable '%s' can't be set to the value of 'not-a-valid-value'" % name
            with self.subTest(name=name):
                self.assertEqual(dbops.redact(message), message)
        message = ("Variable 'wsrep_sst_auth' can't be set to the value of 'sst:private'\n"
                   "Variable 'max_connections' can't be set to the value of 'invalid'")
        self.assertEqual(dbops.redact(message), message.replace('sst:private', '[redacted]'))
        for separator in ('; ', '\n'):
            for names in (('max_connections', 'wsrep_sst_auth'), ('wsrep_sst_auth', 'max_connections')):
                message = separator.join("Variable '%s' can't be set to the value of '%s'" %
                                         (name, 'sst:private' if dbops.sensitive(name) else 'invalid')
                                         for name in names)
                with self.subTest(separator=separator, names=names):
                    self.assertEqual(dbops.redact(message), message.replace('sst:private', '[redacted]'))
        message = "Variable 'wsrep_sst_auth' can't be set to the value of 'sst:multi\nline'"
        self.assertEqual(dbops.redact(message),
                         "Variable 'wsrep_sst_auth' can't be set to the value of '[redacted]'")
        incomplete = "Variable 'wsrep_sst_auth' can't be set to the value of 'sst:private"
        self.assertEqual(dbops.redact(incomplete),
                         "Variable 'wsrep_sst_auth' can't be set to the value of '[redacted]")

    def test_gate351_slash_url_scan_stays_linear_on_400kb_inputs(self):
        for shape in ('repeated_slash_credentials', 'nested_no_at'):
            with self.subTest(shape=shape):
                code = (
                    "import time\nfrom myflames import dbops\n"
                    "shape = " + repr(shape) + "\n"
                    "unit = 'mysql://u:pa/SECRET@h/db ' if shape == 'repeated_slash_credentials' else 'mysql://u:'\n"
                    "value = unit * (400 * 1024 // len(unit))\n"
                    "began = time.monotonic()\n"
                    "clean = dbops.redact(value)\n"
                    "elapsed = time.monotonic() - began\n"
                    "assert 'SECRET' not in clean\n"
                    "assert shape != 'nested_no_at' or clean == value\n"
                    "print(elapsed)\n")
                result = subprocess.run([sys.executable, '-c', code],
                    cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertLess(float(result.stdout.strip()), 1)

    def test_gate65_adjacent_credential_urls_redact_every_authority(self):
        for first, cleaned_first in (
                ('mysql://app:pw1@db1', 'mysql://app:[redacted]@db1'),
                ('mysql://reader@db1', 'mysql://reader@db1'),
                ('mysql://app:pw1@db1:3306', 'mysql://app:[redacted]@db1:3306')):
            second = 'mysql://admin:pw2@db2:3306'
            cleaned_second = 'mysql://admin:[redacted]@db2:3306'
            for separator in (',', ';', '|', '&b=', '","'):
                for quoted in (True, False):
                    with self.subTest(first=first, separator=separator, quoted=quoted):
                        raw = first + separator + second
                        expected = cleaned_first + separator + cleaned_second
                        self.assertEqual(dbops.redact(raw, quoted=quoted), expected)
                        if quoted:
                            self.assertEqual(dbops.redact("SELECT '" + raw + "'"),
                                             "SELECT '" + expected + "'")
        raw = '["mysql://rs:pw1@db1:3306","mysql://rs:pw2@db2:3306"]'
        self.assertEqual(dbops.redact(raw, quoted=False),
                         '["mysql://rs:[redacted]@db1:3306","mysql://rs:[redacted]@db2:3306"]')
        self.assertEqual(dbops.redact('https://user@host?next=mysql://dbuser:secret@db'),
                         'https://user@host?next=mysql://dbuser:[redacted]@db')

    def test_gate65_database_urls_hide_numeric_slash_passwords(self):
        for scheme in ('mysql', 'mariadb', 'mysqlx', 'postgres', 'postgresql', 'MYSQL'):
            for password in ('2024/secret', '2024/x', '2024/one/two'):
                for quoted in (True, False):
                    with self.subTest(scheme=scheme, password=password, quoted=quoted):
                        raw = scheme + '://app:' + password + '@db1:3306/shop'
                        self.assertEqual(dbops.redact(raw, quoted=quoted),
                                         scheme + '://app:[redacted]@db1:3306/shop')
        self.assertEqual(dbops.redact('https://h:2024/secret@db'), 'https://h:2024/secret@db')

    def test_gate65_ipv6_hosts_are_not_passwords(self):
        for raw in ('https://[2001:db8::1]/users/alice@example.com',
                    'https://[::1]:443/users/alice@example.com',
                    'mysql://[2001:db8::1]:3306/db?mail=a@b'):
            for quoted in (True, False):
                with self.subTest(raw=raw, quoted=quoted):
                    self.assertEqual(dbops.redact(raw, quoted=quoted), raw)
        for raw, expected in (
                ('mysql://u:p/w@[::1]:3306/db', 'mysql://u:[redacted]@[::1]:3306/db'),
                ('mysql://u:2024/x@[2001:db8::1]:3306/db',
                 'mysql://u:[redacted]@[2001:db8::1]:3306/db')):
            with self.subTest(raw=raw):
                self.assertEqual(dbops.redact(raw), expected)

    def test_gate65_secret_assignments_preserve_following_clauses(self):
        for clause in ('WHERE id = 1', 'ORDER BY id', 'LIMIT 1',
                       'ON DUPLICATE KEY UPDATE is_admin = 1', 'RETURNING id', 'FOR UPDATE',
                       'where id = 1'):
            for expression in ("'gate65-secret'", "CONCAT('gate65-', 'secret')"):
                with self.subTest(clause=clause, expression=expression):
                    statement = 'UPDATE cfg SET s3_secret_key = ' + expression + ' ' + clause
                    quote = "'" if expression.startswith("'") else ''
                    expected = ('UPDATE cfg SET s3_secret_key = ' + quote + '[redacted]' + quote +
                                ' ' + clause)
                    self.assertEqual(dbops._redact_secret_variables(statement), expected)
                    self.assertEqual(dbops.redact(statement), expected)
        statement = "INSERT INTO cfg SET s3_secret_key = 'abc' ON DUPLICATE KEY UPDATE is_admin = 1"
        self.assertEqual(dbops.redact(statement), statement.replace("'abc'", "'[redacted]'"))
        for expression in ("CONCAT('a', 'b')", "CONCAT('WHERE', 'ON', 'FOR')",
                           "CONCAT((SELECT value FROM secrets WHERE id = 1), 'tail')"):
            with self.subTest(expression=expression):
                self.assertEqual(dbops.redact('SET GLOBAL wsrep_sst_auth = ' + expression),
                                 'SET GLOBAL wsrep_sst_auth = [redacted]')

    def test_gate65_new_set_restarts_unterminated_assignment_scripts(self):
        cases = (
            ("SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = 'gate65-secret'",
             "SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = '[redacted]'"),
            ("SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = CONCAT('gate65', 'secret')",
             "SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = [redacted]"),
            ("SET GLOBAL wsrep_sst_auth = firstsecret\nSET GLOBAL s3_secret_key = secondsecret",
             "SET GLOBAL wsrep_sst_auth = [redacted]\nSET GLOBAL s3_secret_key = [redacted]"),
            ("DELIMITER //\nSET GLOBAL max_connections = 500//\n"
             "SET GLOBAL wsrep_sst_auth = CONCAT('gate65', 'secret')",
             "DELIMITER //\nSET GLOBAL max_connections = 500//\n"
             "SET GLOBAL wsrep_sst_auth = [redacted]"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(dbops._redact_secret_variables(raw), expected)
                self.assertEqual(dbops.redact(raw), expected)

    def test_gate65_double_quoted_secret_variable_names(self):
        for name in sorted(dbops._SECRET_VARIABLES):
            for identifier in ('"' + name.upper() + '"',
                               '.'.join('"' + part + '"' for part in name.split('.'))):
                for prefix in ('SET GLOBAL ', 'SET @@global.', 'SET @@"GLOBAL".'):
                    for expression in ("'gate65-secret'", "CONCAT('gate65', 'secret')"):
                        with self.subTest(identifier=identifier, prefix=prefix, expression=expression):
                            raw = prefix + identifier + ' := ' + expression
                            quote = "'" if expression.startswith("'") else ''
                            expected = prefix + identifier + ' := ' + quote + '[redacted]' + quote
                            self.assertEqual(dbops._redact_secret_variables(raw), expected)
                            self.assertEqual(dbops.redact(raw), expected)

    def test_gate65_secret_inventory_literals_redact_in_any_context(self):
        for name in sorted(dbops._SECRET_VARIABLES):
            for identifier in (name.upper(), '`' + name + '`', '"' + name + '"'):
                for prefix in ('', '@@', '@@global.', '@@SESSION.', '@@persist_only.'):
                    for operator in ('=', ':='):
                        with self.subTest(name=name, identifier=identifier, prefix=prefix, operator=operator):
                            assignment = prefix + identifier + ' ' + operator + " 'gate65-secret'"
                            raw = 'arbitrary diagnostic: ' + assignment + '; keep this text'
                            self.assertEqual(dbops.redact(raw), raw.replace('gate65-secret', '[redacted]'))
        for raw in (
                'EXECUTE IMMEDIATE "SET GLOBAL wsrep_sst_auth = \'gate65-secret\'"',
                "/* s3_secret_key = 'gate65-secret' */ SELECT 1",
                'diagnostic: s3_secret_key = "gate65-secret"',
                "-- s3_secret_key = 'gate65-secret'\nSELECT 1"):
            with self.subTest(raw=raw):
                self.assertEqual(dbops.redact(raw), raw.replace('gate65-secret', '[redacted]'))
        # An echo's closing quotes can also be an escaped quote in the
        # secret. This ambiguous diagnostic must fail closed.
        echoed = "ERROR 1064: near 'SET GLOBAL wsrep_sst_auth = 'gate65-secret'' at line 1"
        cleaned = dbops.redact(echoed)
        self.assertNotIn('gate65-secret', cleaned)
        self.assertIn('[redacted]', cleaned)
        for raw in ("diagnostic: max_connections = 'public'",
                    "diagnostic: my_wsrep_sst_auth = 'public'",
                    "diagnostic: wsrep_sst_auth_suffix = 'public'"):
            with self.subTest(raw=raw):
                self.assertEqual(dbops.redact(raw), raw)

    def test_gate65_executable_comment_version_digits_do_not_hide_values(self):
        for marker in ('/*!', '/*!-', '/*M!', '/*M!-'):
            with self.subTest(marker=marker):
                raw = 'SET GLOBAL wsrep_sst_auth = ' + marker + '80000987654321*/'
                expected = 'SET GLOBAL wsrep_sst_auth = ' + marker + '80000[redacted]*/'
                self.assertEqual(dbops._redact_secret_variables(raw), expected)
                self.assertEqual(dbops.redact(raw), expected)
        for marker in ('/*!-80000 ', '/*M!-100100 '):
            with self.subTest(marker=marker):
                raw = marker + "SET GLOBAL wsrep_sst_auth = 'gate65-secret' */"
                self.assertEqual(dbops._redact_secret_variables(raw), raw.replace('gate65-secret', '[redacted]'))

    def test_gate65_stray_comment_close_does_not_consume_next_comment_opener(self):
        for prefix in ("SELECT 2 */* it's */ 3; ",
                       "/*!80000 SET GLOBAL max_connections = 500 **/; SELECT 2 */* it's */ 3; "):
            for expression in ("'gate65-secret'", "CONCAT('gate65', 'secret')", 'unquotedsecret'):
                with self.subTest(prefix=prefix, expression=expression):
                    raw = prefix + 'SET GLOBAL wsrep_sst_auth = ' + expression
                    quote = "'" if expression.startswith("'") else ''
                    expected = prefix + 'SET GLOBAL wsrep_sst_auth = ' + quote + '[redacted]' + quote
                    self.assertEqual(dbops._redact_secret_variables(raw), expected)
                    self.assertEqual(dbops.redact(raw), expected)

    def test_gate65_new_paths_stay_linear_on_400kb_inputs(self):
        # Each branch has a hard process timeout as well as a timing bound.
        # Exact output also guards against making a path fast by skipping it.
        shapes = (
            ('adjacent_username_urls', 'a://b@c,', 'a://b@c,'),
            ('adjacent_credentials', 'mysql://u:SECRET@h,mysql://v:SECRET@i ',
             'mysql://u:[redacted]@h,mysql://v:[redacted]@i '),
            ('numeric_slash_password', 'mysql://u:2024/SECRET@h/db ', 'mysql://u:[redacted]@h/db '),
            ('ipv6_host', 'https://[2001:db8::1]/users/alice@example.com ',
             'https://[2001:db8::1]/users/alice@example.com '),
            ('clause_boundaries', "UPDATE cfg SET s3_secret_key = 'SECRET' WHERE id = 1; ",
             "UPDATE cfg SET s3_secret_key = '[redacted]' WHERE id = 1; "),
            ('restart_set', "SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = SECRET\n",
             "SET GLOBAL max_connections = 500\nSET GLOBAL wsrep_sst_auth = [redacted]\n"),
            ('double_quoted_name', 'SET GLOBAL "wsrep_sst_auth" = SECRET; ',
             'SET GLOBAL "wsrep_sst_auth" = [redacted]; '),
            ('context_free_inventory', "echo s3_secret_key := 'SECRET'; ",
             "echo s3_secret_key := '[redacted]'; "),
            ('executable_digits', 'SET GLOBAL wsrep_sst_auth = /*!80000987654321*/; ',
             'SET GLOBAL wsrep_sst_auth = /*!80000[redacted]*/; '),
            ('glued_version_digit', "SET /*M!100100wsrep_sst_auth = CONCAT('SECRET', 'tail') */; "
             "/*M!100100SET GLOBAL s3_secret_key = CONCAT('SECRET', 'tail') */; ",
             'SET /*M!100100wsrep_sst_auth = [redacted] */; '
             '/*M!100100SET GLOBAL s3_secret_key = [redacted] */; '),
            ('stray_comment_close', "SELECT 2 */* it's */ 3; SET GLOBAL wsrep_sst_auth = SECRET; ",
             "SELECT 2 */* it's */ 3; SET GLOBAL wsrep_sst_auth = [redacted]; "),
        )
        for shape, unit, cleaned_unit in shapes:
            with self.subTest(shape=shape):
                code = (
                    "import time\nfrom myflames import dbops\n"
                    "n = 400 * 1024\n"
                    "unit, cleaned_unit = " + repr((unit, cleaned_unit)) + "\n"
                    "copies, padding = divmod(n, len(unit))\n"
                    "value = unit * copies + ' ' * padding\n"
                    "expected = cleaned_unit * copies + ' ' * padding\n"
                    "began = time.monotonic()\n"
                    "clean = dbops.redact(value)\n"
                    "elapsed = time.monotonic() - began\n"
                    "assert clean == expected, (len(clean), len(expected), clean[:200])\n"
                    "print(elapsed)\n")
                result = subprocess.run([sys.executable, '-c', code],
                    cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                elapsed = float(result.stdout.strip())
                print('GATE65_TIMING %s %.6fs' % (shape, elapsed), flush=True)
                self.assertLess(elapsed, 1)

    def test_r352_brackets_in_url_usernames_do_not_disable_redaction(self):
        cases = (
            ('mysql://app[ro]:PW@db1/x', 'mysql://app[ro]:[redacted]@db1/x'),
            ('mysql://app[ro]:PW/tail@db1/x', 'mysql://app[ro]:[redacted]@db1/x'),
            ('mysql://app[ro:PW@db1/x', 'mysql://app[ro:[redacted]@db1/x'),
            ('mysql://[::1]:3306/db', 'mysql://[::1]:3306/db'),
            ('mysql://u:p@[::1]:3306/db', 'mysql://u:[redacted]@[::1]:3306/db'),
        )
        for raw, expected in cases:
            for quoted in (True, False):
                with self.subTest(raw=raw, quoted=quoted):
                    self.assertEqual(dbops.redact(raw, quoted=quoted), expected)

    def test_r352_assignment_free_set_restarts_secret_assignments(self):
        for prefix in ('SET NAMES utf8mb4 ', 'SET CHARACTER SET utf8mb4 ',
                       'ALTER TABLE t CHARACTER SET utf8mb4 '):
            for assignment in ('SET GLOBAL wsrep_sst_auth = ',
                               'SET @@global.wsrep_sst_auth = '):
                for value in ("CONCAT('private', 'tail')", '@private_value'):
                    with self.subTest(prefix=prefix, assignment=assignment, value=value):
                        raw = prefix + assignment + value
                        expected = prefix + assignment + '[redacted]'
                        self.assertEqual(dbops._redact_secret_variables(raw), expected)
                        self.assertEqual(dbops.redact(raw), expected)
        # SET in CHARACTER SET and inside an expression must not expose a
        # suffix of the current secret or change the harmless statement.
        for raw in ('SET CHARACTER SET utf8mb4',
                    'ALTER TABLE t CHARACTER SET utf8mb4'):
            with self.subTest(raw=raw):
                self.assertEqual(dbops.redact(raw), raw)
        raw = "SET GLOBAL wsrep_sst_auth = CONCAT('SET', 'CHARACTER SET', 'private')"
        self.assertEqual(dbops.redact(raw), 'SET GLOBAL wsrep_sst_auth = [redacted]')

    def test_r352_secret_inventory_pass_skips_editor_text_without_names(self):
        raw = ("SELECT id, total FROM orders WHERE status = 'open';\n"
               "SET SESSION max_execution_time = 1000;\n"
               "ALTER USER app IDENTIFIED BY 'private-login';\n"
               "SELECT 'mysql://reader:private-url@db1/shop';")
        expected = raw.replace('private-login', '[redacted]').replace('private-url', '[redacted]')
        inventory_pattern = next(pattern for pattern in dbops._SECRET_PATTERNS
                                 if dbops._SECRET_VARIABLE_NAME in pattern.pattern)
        tracked_pattern = mock.Mock(wraps=inventory_pattern)
        patterns = tuple(tracked_pattern if pattern is inventory_pattern else pattern
                         for pattern in dbops._SECRET_PATTERNS)
        with mock.patch.object(dbops, '_SECRET_PATTERNS', patterns):
            with mock.patch.object(dbops, '_SECRET_VARIABLE_LITERAL', tracked_pattern, create=True):
                with mock.patch.object(dbops, '_redact_secret_variables',
                                       wraps=dbops._redact_secret_variables) as inventory_pass:
                    self.assertEqual(dbops.redact(raw), expected)
                    inventory_pass.assert_not_called()
                    tracked_pattern.sub.assert_not_called()

    def test_r352_secret_inventory_guard_keeps_all_name_spellings(self):
        for name in sorted(dbops._SECRET_VARIABLES):
            for identifier in (name.upper(), '`' + name + '`', '"' + name.upper() + '"',
                               ' . '.join('`' + part.upper() + '`' for part in name.split('.')),
                               ' /* gap */ . '.join('"' + part + '"' for part in name.split('.'))):
                with self.subTest(identifier=identifier):
                    raw = 'SET GLOBAL ' + identifier + " = CONCAT('private', 'tail')"
                    self.assertEqual(dbops.redact(raw), 'SET GLOBAL ' + identifier + ' = [redacted]')
        # The fallback uses Unicode re.I matching. A fast ASCII-name guard
        # must not skip spellings that the existing fallback already hides.
        for name in ('w\u017frep_sst_auth', 'f\u0131le_key_management_filekey',
                     'f\u0130le_key_management_filekey', '\u212aeyring_hashicorp_secret_id'):
            with self.subTest(name=name):
                raw = "diagnostic: " + name + " = 'private-value'"
                self.assertEqual(dbops.redact(raw), raw.replace('private-value', '[redacted]'))

    def test_r352_inventory_guard_keeps_fragmented_names_fail_closed(self):
        # Even malformed names are joined by the existing SET scanner. A
        # fast path must not expose values it previously hid.
        for identifier in ('w srep_sst_auth', 'w/**/srep_sst_auth',
                           'w-- gap\nsrep_sst_auth', 'w# gap\nsrep_sst_auth',
                           '`w`"srep_sst_auth"',
                           '`ws` /* gap */ `rep_sst_auth`',
                           'w/*!80000 */srep_sst_auth',
                           'w/*M!100100 */srep_sst_auth',
                           '/*M!100100w srep_sst_auth */'):
            with self.subTest(identifier=identifier):
                raw = 'SET GLOBAL ' + identifier + " = CONCAT('private', 'tail')"
                self.assertEqual(dbops.redact(raw), 'SET GLOBAL ' + identifier + ' = [redacted]')
        for name in sorted(dbops._SECRET_VARIABLES):
            for split in (1, 2, 3):
                with self.subTest(name=name, split=split):
                    identifier = name[:split] + ' /**/ ' + name[split:]
                    raw = 'SET GLOBAL ' + identifier + ' = @private'
                    self.assertEqual(dbops.redact(raw), 'SET GLOBAL ' + identifier + ' = [redacted]')

    def test_r352_insert_set_row_alias_ends_secret_value(self):
        for expression in ("'private-value'", '"private-value"', '@private_value',
                           "CONCAT('private', 'tail')", "CAST('private-value' AS CHAR)",
                           "(SELECT 'private-value' AS hidden)"):
            for alias in ('AS new', 'as `new`', 'AS new (new_secret)',
                          'AS new (new_secret, c)'):
                with self.subTest(expression=expression, alias=alias):
                    prefix = 'INSERT INTO t SET wsrep_sst_auth = '
                    suffix = ' ' + alias + ' ON DUPLICATE KEY UPDATE c = new.c'
                    quote = expression[0] if expression[0] in ('\"', "'") else ''
                    expected = prefix + quote + '[redacted]' + quote + suffix
                    self.assertEqual(dbops._redact_secret_variables(prefix + expression + suffix), expected)
                    self.assertEqual(dbops.redact(prefix + expression + suffix), expected)
        # VALUES has no SET assignment to parse. Its existing credential
        # redaction still leaves the row alias and duplicate-key clause intact.
        raw = ("INSERT INTO t (c) VALUES ('mysql://u:private-value@h/db') AS new "
               "ON DUPLICATE KEY UPDATE c = new.c")
        self.assertEqual(dbops.redact(raw), raw.replace('private-value', '[redacted]'))

    def test_r352_as_inside_secret_values_stays_hidden(self):
        for prefix in ('SET GLOBAL wsrep_sst_auth = ',
                       'UPDATE t SET wsrep_sst_auth = ',
                       'INSERT INTO t SET wsrep_sst_auth = '):
            for expression in ("'private AS tail'", "CONCAT('private', ' AS tail')",
                               "CAST('private-value' AS CHAR)",
                               "(SELECT 'private-value' AS hidden)",
                               'secrets.AS + private_tail', 'secrets . AS + private_tail',
                               '@AS + private_tail', 'private-AS-tail',
                               'private AS + tail', 'private AS tail + remainder',
                               '@private OR AS tail', '@private AND AS tail',
                               '@private NOT AS tail', '@private COLLATE AS tail',
                               "'private AS unfinished"):
                with self.subTest(prefix=prefix, expression=expression):
                    quote = expression[0] if expression[0] in ('\"', "'") else ''
                    self.assertEqual(dbops.redact(prefix + expression),
                                     prefix + quote + '[redacted]' + quote)
        for prefix in ('SET GLOBAL wsrep_sst_auth = ', 'UPDATE t SET wsrep_sst_auth = '):
            with self.subTest(prefix=prefix):
                self.assertEqual(dbops.redact(prefix + 'private AS tail'), prefix + '[redacted]')
                self.assertEqual(dbops.redact(prefix + "'private' WHERE c = 1"),
                                 prefix + "'[redacted]' WHERE c = 1")
        raw = 'INSERT INTO t VALUES (1) SET GLOBAL wsrep_sst_auth = private AS tail'
        self.assertEqual(dbops.redact(raw),
                         'INSERT INTO t VALUES (1) SET GLOBAL wsrep_sst_auth = [redacted]')

    def test_r352_prefixed_database_schemes_hide_numeric_slash_passwords(self):
        for scheme in ('pool+mysql', 'jdbc:mysql', 'jdbc:pool+mysql',
                       'pool+mariadb', 'pool+mysqlx', 'pool+postgres', 'pool+postgresql',
                       'POOL+MYSQL'):
            for value in ('p', 'p/w', '123/pw', '2024/one/two'):
                for quoted in (True, False):
                    with self.subTest(scheme=scheme, value=value, quoted=quoted):
                        raw = scheme + '://u:' + value + '@h/db'
                        self.assertEqual(dbops.redact(raw, quoted=quoted),
                                         scheme + '://u:[redacted]@h/db')
        for quoted in (True, False):
            with self.subTest(quoted=quoted):
                raw = 'git+https://h:443/path?mail=a@b'
                self.assertEqual(dbops.redact(raw, quoted=quoted), raw)
                self.assertEqual(dbops.redact('git+https://u:p@h/repo', quoted=quoted),
                                 'git+https://u:[redacted]@h/repo')

    def test_r352_reversed_executable_comments_hide_secret_expressions(self):
        # sql_lex.cc accepts a second ! after either /*! or /*M!, then
        # consumes five version digits and a sixth when present.
        for opener in ('/*!!100000 ', '/*M!!100000 ', '/*!!80000 ', '/*M!!80000 ',
                       '/*!! ', '/*M!! ', '/*!!100000', '/*M!!100000'):
            for value in ("'private-value'", "CONCAT('private', 'tail')", '@private_value'):
                with self.subTest(opener=opener, value=value):
                    raw = opener + 'SET GLOBAL wsrep_sst_auth = ' + value + ' */; SELECT 1'
                    quote = "'" if value.startswith("'") else ''
                    expected = (opener + 'SET GLOBAL wsrep_sst_auth = ' + quote +
                                '[redacted]' + quote + ' */; SELECT 1')
                    self.assertEqual(dbops._redact_secret_variables(raw), expected)
                    self.assertEqual(dbops.redact(raw), expected)
        for marker in ('/*!!100000', '/*M!!100000'):
            with self.subTest(marker=marker):
                raw = 'SET ' + marker + "wsrep_sst_auth = CONCAT('private', 'tail') */"
                self.assertEqual(dbops.redact(raw), 'SET ' + marker + 'wsrep_sst_auth = [redacted] */')
        # MariaDB consumes no version digits unless there are at least five.
        # Shorter numeric bodies after '=' must remain secret, as on the base.
        for marker in ('/*!!', '/*M!!'):
            for number in ('7', '73', '731', '7319'):
                with self.subTest(marker=marker, number=number):
                    raw = 'SET GLOBAL wsrep_sst_auth = ' + marker + number + '*/'
                    for redact in (dbops._redact_secret_variables, dbops.redact):
                        clean = redact(raw)
                        self.assertNotIn(number, clean)
                        self.assertIn('[redacted]', clean)

    def test_r352_realistic_editor_megabyte_redaction(self):
        import statistics
        import time

        unit = ("-- Inspect recent orders and the customers who placed them.\n"
                "SELECT o.id, o.created_at, o.total, c.name\n"
                "FROM orders AS o JOIN customers AS c ON c.id = o.customer_id\n"
                "WHERE o.status = 'open' AND o.total > 25\n"
                "ORDER BY o.created_at DESC LIMIT 100;\n"
                "SET SESSION max_execution_time = 1000;\n")
        for shape, suffix in (('without_secret_name', ''),
                              ('with_secret_name', "SET GLOBAL wsrep_sst_auth = 'benchmark-private';\n")):
            with self.subTest(shape=shape):
                size = 1024 * 1024
                copies, padding = divmod(size - len(suffix), len(unit))
                raw = unit * copies + ' ' * padding + suffix
                expected = raw.replace('benchmark-private', '[redacted]')
                self.assertEqual(len(raw.encode('utf-8')), size)
                timings = []
                for _ in range(3):
                    began = time.monotonic()
                    clean = dbops.redact(raw)
                    timings.append(time.monotonic() - began)
                    self.assertEqual(clean, expected)
                print('R352_EDITOR_TIMING %s bytes=%d median=%.6fs samples=%s' %
                      (shape, size, statistics.median(timings),
                       ','.join('%.6f' % elapsed for elapsed in timings)), flush=True)

    def test_r352_dynamic_secret_literals_hide_escaped_quotes_and_tail(self):
        # An escaped quote belongs to the enclosing dynamic SQL literal;
        # treating it as a new empty literal can leave the password visible.
        for quote in ("'", '"'):
            for escaped in (quote * 2, '\\' + quote):
                for command in ('PREPARE s FROM ', 'EXECUTE IMMEDIATE '):
                    with self.subTest(quote=quote, escaped=escaped, command=command):
                        raw = (command + quote + 'SET GLOBAL wsrep_sst_auth = ' +
                               escaped + 'dynamic-private' + escaped +
                               '; SELECT ' + escaped + 'dynamic-tail' + escaped + quote)
                        clean = dbops.redact(raw)
                        self.assertNotIn('dynamic-private', clean)
                        self.assertNotIn('dynamic-tail', clean)
                        self.assertIn('[redacted]', clean)
        # An unfinished enclosing string must hide the same tail.
        raw = "PREPARE s FROM 'SET GLOBAL wsrep_sst_auth = ''dynamic-private'' dynamic-tail"
        clean = dbops.redact(raw)
        self.assertNotIn('dynamic-private', clean)
        self.assertNotIn('dynamic-tail', clean)
        self.assertIn('[redacted]', clean)

    def test_r352_dynamic_assignment_without_literal_stays_unchanged(self):
        for raw in (
                "SET @q = CONCAT('SET GLOBAL wsrep_sst_auth = ', QUOTE(@pw))",
                'SET @q = CONCAT("SET GLOBAL wsrep_sst_auth = ", QUOTE(@pw))',
                "PREPARE s FROM 'SELECT wsrep_sst_auth'",
                "PREPARE s FROM 'SET GLOBAL max_connections = ''public'''"):
            with self.subTest(raw=raw):
                self.assertEqual(dbops.redact(raw), raw)

    def test_r352_dynamic_literals_preserve_later_statements(self):
        suffix = "; SELECT 37; SET GLOBAL s3_secret_key = 'separate-private'; SELECT 41"
        for quote in ("'", '"'):
            for escaped in (quote * 2, '\\' + quote):
                with self.subTest(quote=quote, escaped=escaped):
                    raw = ('EXECUTE IMMEDIATE ' + quote + 'SET GLOBAL wsrep_sst_auth = ' +
                           escaped + 'dynamic-private' + escaped + quote + suffix)
                    clean = dbops.redact(raw)
                    self.assertNotIn('dynamic-private', clean)
                    self.assertNotIn('separate-private', clean)
                    self.assertTrue(clean.endswith(suffix.replace('separate-private', '[redacted]')), clean)
        prefix = "SET @q = CONCAT('SET GLOBAL wsrep_sst_auth = ', QUOTE(@pw)); "
        for statement in ("SET GLOBAL wsrep_sst_auth = 'separate-private'; SELECT 1",
                          'EXECUTE IMMEDIATE "SET GLOBAL wsrep_sst_auth = \'separate-private\'"; SELECT 1'):
            with self.subTest(statement=statement):
                self.assertEqual(dbops.redact(prefix + statement),
                                 prefix + statement.replace('separate-private', '[redacted]'))

    def test_r352_dynamic_diagnostic_ambiguity_stays_hidden(self):
        for raw in (
                "error near 'wsrep_sst_auth = 'diagnostic-private' at line 1",
                "error near 'wsrep_sst_auth = ',diagnostic-private' at line 1",
                "error CONCAT('wsrep_sst_auth = ',diagnostic-private' at line 1)",
                "error CONCAT('wsrep_sst_auth = ', QUOTE(@pw)) diagnostic-private'",
                "error CONCAT('wsrep_sst_auth = ', QUOTE(@pw)) diagnostic-private",
                "error CONCAT('wsrep_sst_auth = ', QUOTE(@pw)); diagnostic-private'"):
            with self.subTest(raw=raw):
                clean = dbops.redact(raw)
                self.assertNotIn('diagnostic-private', clean)
                self.assertIn('[redacted]', clean)

    def test_r352_clause_named_variables_never_end_secret_values(self):
        for name in ('where', 'order', 'limit', 'on', 'returning', 'for', 'as', 'set'):
            for variable in ('@' + name, "@'" + name + "'", '@"' + name + '"',
                             '@`' + name + '`', '@@' + name, '@@GLOBAL.' + name):
                for expression in (variable, variable + " + 'clause-private'",
                                   'CONCAT(' + variable + ", 'clause-private')"):
                    with self.subTest(variable=variable, expression=expression):
                        prefix = 'SET GLOBAL wsrep_sst_auth = '
                        raw = prefix + expression + '; SELECT 1'
                        expected = prefix + '[redacted]; SELECT 1'
                        self.assertEqual(dbops._redact_secret_variables(raw), expected)
                        self.assertEqual(dbops.redact(raw), expected)

    def test_r352_variable_prefix_cannot_hide_later_secret_assignment(self):
        # Outside an active secret value, preserve the old conservative SET
        # recognition even when a malformed prefix includes an @ character.
        for prefix in ('SELECT @\n', 'SELECT @'):
            with self.subTest(prefix=prefix):
                raw = prefix + "SET GLOBAL wsrep_sst_auth = CONCAT('variable-private', 'tail')"
                expected = prefix + 'SET GLOBAL wsrep_sst_auth = [redacted]'
                self.assertEqual(dbops._redact_secret_variables(raw), expected)
                self.assertEqual(dbops.redact(raw), expected)

    def test_r352_custom_delimiters_preserve_following_statements(self):
        for declaration, delimiter in (('$$', '$$'), ('//', '//'), (';;', ';;'),
                                       ("'//'", '//'), ('"$$"', '$$')):
            for expression in ("'delimiter-private'", 'delimiter_private',
                               "CONCAT('delimiter-private', 'tail')"):
                with self.subTest(declaration=declaration, expression=expression):
                    prefix = 'DELIMITER ' + declaration + '\nSET GLOBAL wsrep_sst_auth = '
                    suffix = delimiter + '\nSELECT 1' + delimiter + '\nDELIMITER ;\nSELECT 2;'
                    quote = "'" if expression.startswith("'") else ''
                    expected = prefix + quote + '[redacted]' + quote + suffix
                    self.assertEqual(dbops._redact_secret_variables(prefix + expression + suffix), expected)
                    self.assertEqual(dbops.redact(prefix + expression + suffix), expected)
        raw = ("  delimiter $$ ignored\nSET GLOBAL max_connections = 500$$\n"
               "DELIMITER //\nSET GLOBAL s3_secret_key = 'second-private'//\nSELECT 1//")
        self.assertEqual(dbops.redact(raw), raw.replace('second-private', '[redacted]'))

    def test_r352_custom_delimiter_fails_closed_when_tail_has_credentials(self):
        # When a custom-delimiter boundary would end a hidden value and the
        # remaining text contains a credential keyword or inventory variable
        # name, keep hiding through the end of text (parity with 3.5.1).
        tails = (
            "CREATE USER app IDENTIFIED BY'S3cr3t'$$",
            "CREATE USER app identified/**/by'S3cr3t'$$",
            "START REPLICA USER='r' PASSWORD = /* c */ 'S3cr3t'$$",
            "CHANGE REPLICATION SOURCE TO SOURCE_PASSWORD /* c */ = 'S3cr3t'$$",
            "CHANGE MASTER TO MASTER_PASSWORD = _binary 0x533363723374$$",
            "CREATE USER app IDENTIFIED VIA ed25519 USING/**/'S3cr3t'$$",
            "ALTER USER app IDENTIFIED BY 'new'/**/REPLACE/**/'S3cr3t'$$",
            "DELIMITER //\nSET GLOBAL s3_secret_key = 'S3cr3t'//\nSELECT 1//",
            "PREPARE s FROM 'SET GLOBAL wsrep_sst_auth /**/ = ''S3cr3t'''$$",
            "EXECUTE IMMEDIATE \"INSERT INTO cfg SET telemetry.otel_exporter_otlp_logs_headers = _utf8mb4 0x533363723374\"$$",
        )
        for tail in tails:
            with self.subTest(tail=tail):
                raw = "DELIMITER $$\nSET GLOBAL wsrep_sst_auth = 'first-private'$$\n" + tail
                clean = dbops.redact(raw)
                self.assertEqual(clean, "DELIMITER $$\nSET GLOBAL wsrep_sst_auth = '[redacted]'")
                self.assertNotIn('first-private', clean)
                self.assertNotIn('S3cr3t', clean)
                self.assertNotIn('533363723374', clean)
        # An earlier unclosed quote can flip parity so a custom delimiter
        # lands inside a doubled-quote secret value; fail closed there too.
        flipped = ("DELIMITER go\nSELECT 'unclosed\n"
                   "SET @q = 'SET GLOBAL file_key_management_filekey = ''headgoS3cr3t'', GLOBAL read_only = ON';")
        clean_flipped = dbops.redact(flipped)
        self.assertNotIn('S3cr3t', clean_flipped)
        self.assertIn('[redacted]', clean_flipped)
        # Without credential keywords in the tail, ordinary statements remain visible.
        ordinary = "DELIMITER $$\nSET GLOBAL wsrep_sst_auth = 'first-private'$$\nSELECT 1$$\nSELECT 'plain'$$"
        self.assertEqual(dbops.redact(ordinary), ordinary.replace('first-private', '[redacted]'))
        # Repeated custom delimiters with fragmented names search the tail at most once.
        repeated = ("DELIMITER //\n" +
                    "SET GLOBAL w/**/srep_sst_auth = 'first-private'//\n" * 200 +
                    "SELECT 1//\n")
        with mock.patch.object(dbops, '_DELIMITER_TAIL_CREDENTIAL',
                               wraps=dbops._DELIMITER_TAIL_CREDENTIAL) as tail_search:
            clean_repeated = dbops.redact(repeated)
        self.assertLessEqual(tail_search.search.call_count, 1)
        self.assertNotIn('first-private', clean_repeated)
        self.assertTrue(clean_repeated.endswith('SELECT 1//\n'))

    def test_r352_delimiters_inside_secret_literals_are_not_value_ends(self):
        for expression in ("'before$$delimiter-private'",
                           "CONCAT('before$$delimiter-private', 'tail')",
                           "CONCAT('before', /* $$ */ 'delimiter-private')"):
            with self.subTest(expression=expression):
                prefix = 'DELIMITER $$\nSET GLOBAL wsrep_sst_auth = '
                suffix = '$$\nSELECT 1$$'
                quote = "'" if expression.startswith("'") else ''
                self.assertEqual(dbops.redact(prefix + expression + suffix),
                                 prefix + quote + '[redacted]' + quote + suffix)
        # A DELIMITER-looking line inside a quoted value is secret text.
        raw = "SET GLOBAL wsrep_sst_auth = 'before\nDELIMITER $$\ndelimiter-private'; SELECT 1"
        self.assertEqual(dbops.redact(raw), "SET GLOBAL wsrep_sst_auth = '[redacted]'; SELECT 1")

    def test_r352_ambiguous_delimiter_framing_keeps_secret_tail_hidden(self):
        # With backslash escapes the first $$ follows a closed string. With
        # literal backslashes it is inside a string that includes the tail.
        # Redaction must not newly expose that tail by choosing one reading.
        raw = ("DELIMITER $$\nSET GLOBAL wsrep_sst_auth = 'head\\'middle'$$\n"
               "delimiter-private'$$\nSELECT 1$$")
        clean = dbops.redact(raw)
        self.assertNotIn('delimiter-private', clean)
        self.assertIn('[redacted]', clean)
        for declaration in ('DELIMITER', "DELIMITER ''", "DELIMITER '//'", 'DELIMITER //\\\\'):
            with self.subTest(declaration=declaration):
                # A malformed/ambiguous declaration or unfinished value
                # cannot turn the apparent marker into a trusted boundary.
                raw = declaration + "\nSET GLOBAL wsrep_sst_auth = 'head//delimiter-private"
                clean = dbops.redact(raw)
                self.assertNotIn('delimiter-private', clean)
                self.assertIn('[redacted]', clean)

    def test_r352_swallowed_literal_never_exposes_later_credential(self):
        # An unterminated or mis-framed literal must not advance the scan past
        # a later credential keyword of the same pattern.
        cases = (
            ("N4a cut REPLACE single quote",
             "ALTER USER a IDENTIFIED BY 'n' REPLACE 'cut off here\n"
             "CREATE USER b IDENTIFIED WITH caching_sha2_password BY 'S3cr3t';"),
            ("N4b cut REPLACE double quote",
             'ALTER USER a IDENTIFIED BY \'n\' REPLACE "trunc\n'
             '---TRANSACTION 421\n'
             'ALTER USER b IDENTIFIED BY "S3cr3t"'),
            ("N4g cut REPLACE doubled quote",
             "ALTER USER a IDENTIFIED BY 'n' REPLACE 'it''s\n"
             "ALTER USER b IDENTIFIED BY 'S3cr3t'"),
            ("N3a earlier SET PASSWORD account and REPLACE quote",
             "ALTER USER 'SET PASSWORD'@'h' IDENTIFIED BY 'a' REPLACE \"b' OR '1'='1\";\n"
             "/*!80000 SET PASSWORD FOR app = 'S3cr3t' */"),
            ("introduced N literal cut",
             "ALTER USER a IDENTIFIED WITH p AS N'cut ;\n"
             "GRANT ALL ON *.* TO b IDENTIFIED BY 'S3cr3t'"),
            ("introduced _latin1 literal cut",
             "SELECT PASSWORD(_latin1'cut\nSELECT password('S3cr3t')"),
            ("plain cut IDENTIFIED BY",
             "ALTER USER a IDENTIFIED BY 'cut ;\n"
             "GRANT ALL ON *.* TO b IDENTIFIED BY 'S3cr3t'"),
            ("plain cut SET PASSWORD",
             "SET PASSWORD = 'cut;\nSET PASSWORD = 'S3cr3t'"),
            ("harmless literal ending in IDENTIFIED BY before real credential",
             "SELECT * FROM t WHERE d = 'IDENTIFIED BY ' AND IDENTIFIED BY 'S3cr3t'"),
            ("nested double-quoted REPLACE after cut single-quoted REPLACE",
             "ALTER USER a IDENTIFIED BY 'n' REPLACE 'a\n"
             "ALTER USER b IDENTIFIED BY \"x\" REPLACE \"p' REPLACE 'q' S3cr3t\""),
        )
        for label, raw in cases:
            with self.subTest(case=label):
                clean = dbops.redact(raw)
                self.assertNotIn('S3cr3t', clean, (label, clean))
                self.assertIn('[redacted]', clean)

    def test_r352_replace_chain_100k_clauses_under_one_second(self):
        # Walking a REPLACE chain once keeps both scans linear even on a
        # 100,000-clause chain.
        code = (
            "import time\nfrom myflames import dbops\n"
            "text = \"ALTER USER a IDENTIFIED BY 'n'\" + \" REPLACE 'x'\" * 100000\n"
            "began = time.perf_counter()\n"
            "clean = dbops.redact(text)\n"
            "elapsed = time.perf_counter() - began\n"
            "assert \"'x'\" not in clean and \"'n'\" not in clean\n"
            "assert clean.count('[redacted]') == 100001\n"
            "print(elapsed)\n")
        try:
            result = subprocess.run([sys.executable, '-c', code],
                cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=10, universal_newlines=True)
        except subprocess.TimeoutExpired:
            self.fail('100,000-clause REPLACE chain exceeded the 10 s deadline')
        self.assertEqual(result.returncode, 0, result.stderr)
        elapsed = float(result.stdout.strip())
        print('R352_REPLACE_CHAIN_100K_TIMING elapsed=%.6fs' % elapsed, flush=True)
        self.assertLess(elapsed, 1.0)

    def test_r352_nested_insert_alias_scan_stays_linear_on_400kb(self):
        # Each nested AS can match the row-alias suffix, but none can end the
        # outer value. Scanning its ever-growing prefix at each AS is quadratic.
        code = (
            "import time\nfrom unittest import mock\nfrom myflames import dbops\n"
            "size = 400 * 1024\n"
            "prefix = 'INSERT INTO t SET wsrep_sst_auth = ('\n"
            "suffix = '); SELECT 1'\n"
            "unit = 'alias_private AS x ON DUPLICATE KEY UPDATE '\n"
            "copies, padding = divmod(size - len(prefix) - len(suffix), len(unit))\n"
            "value = prefix + unit * copies + ' ' * padding + suffix\n"
            "assert len(value) == size\n"
            "with mock.patch.object(dbops, '_SET_ROW_ALIAS', wraps=dbops._SET_ROW_ALIAS) as row_alias:\n"
            "    with mock.patch.object(dbops, '_SET_ALIAS_VALUE', wraps=dbops._SET_ALIAS_VALUE) as alias_value:\n"
            "        began = time.monotonic()\n"
            "        clean = dbops.redact(value)\n"
            "        elapsed = time.monotonic() - began\n"
            "        assert row_alias.match.call_count == 0, row_alias.match.call_count\n"
            "        assert alias_value.fullmatch.call_count == 0, alias_value.fullmatch.call_count\n"
            "assert clean == 'INSERT INTO t SET wsrep_sst_auth = [redacted]; SELECT 1', clean[:200]\n"
            "print(elapsed)\n")
        try:
            result = subprocess.run([sys.executable, '-c', code],
                cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
        except subprocess.TimeoutExpired:
            self.fail('Nested INSERT aliases exceeded the 5 s deadline for 400 KB')
        self.assertEqual(result.returncode, 0, result.stderr)
        elapsed = float(result.stdout.strip())
        print('R352_NESTED_ALIAS_TIMING bytes=409600 elapsed=%.6fs' % elapsed, flush=True)
        self.assertLess(elapsed, 1)

    def test_r352_keyword_guards_preserve_mixed_case_and_unicode_redaction(self):
        cases = (
            ("alter user u iDeNtIfIeD by 'guard-private'", "guard-private"),
            ("alter user u IDENT\u0130FIED by 'guard-private'", "guard-private"),
            ("WITH auth_plugin uSiNg 'guard-private'", "guard-private"),
            ("VIA auth_plugin U\u017fING 'guard-private'", "guard-private"),
            ("OLD_pAsSwOrD('guard-private')", "guard-private"),
            ("PA\u017fSWORD('guard-private')", "guard-private"),
            ("set pAsSwOrD for 'u=;name'@'%' = 'guard-private'", "guard-private"),
            ("source_PaSsWoRd = 'guard-private'", "guard-private"),
            ("RANDOM PASSWORD rEpLaCe 'guard-private'", "guard-private"),
            ("IDENTIFIED BY 'new-private' REPLACE 'guard-private'", "guard-private"),
            ("AS 0X41424344", "0X41424344"),
            ("Variable 'wsrep_sst_auth' can't be set to the value of 'guard-private'", "guard-private"),
            ('MYSQL://u:guard-private@db', 'guard-private'),
        )
        for raw, private in cases:
            with self.subTest(raw=raw):
                clean = dbops.redact(raw)
                self.assertNotIn(private, clean)
                self.assertIn('[redacted]', clean)
        # The connection password must still be removed even if no syntax
        # keyword occurs, including its SQL-escaped spelling.
        self.assertEqual(dbops.redact("SELECT 'own''private'", password="own'private"),
                         "SELECT '[redacted]'")

    def test_r352_editor_megabyte_redaction_speed_budget(self):
        import statistics
        import time

        unit = ("-- Inspect recent orders and the customers who placed them.\n"
                "SELECT o.id, o.created_at, o.total, c.name\n"
                "FROM orders AS o JOIN customers AS c ON c.id = o.customer_id\n"
                "WHERE o.status = 'open' AND o.total > 25\n"
                "ORDER BY o.created_at DESC LIMIT 100;\n"
                "SET SESSION max_execution_time = 1000;\n")
        medians = {}
        for shape, suffix, budget in (
                ('without_secret_name', '', 1.5),
                ('with_secret_name', "SET GLOBAL wsrep_sst_auth = 'benchmark-private';\n", 4.0)):
            with self.subTest(shape=shape):
                size = 1024 * 1024
                copies, padding = divmod(size - len(suffix), len(unit))
                raw = unit * copies + ' ' * padding + suffix
                expected = raw.replace('benchmark-private', '[redacted]')
                timings = []
                for _ in range(3):
                    began = time.monotonic()
                    clean = dbops.redact(raw)
                    timings.append(time.monotonic() - began)
                    self.assertEqual(clean, expected)
                elapsed = statistics.median(timings)
                medians[shape] = elapsed
                print('R352_EDITOR_BUDGET %s bytes=%d median=%.6fs budget=%.3fs samples=%s' %
                      (shape, size, elapsed, budget,
                       ','.join('%.6f' % value for value in timings)), flush=True)
                self.assertLess(elapsed, budget)
        with_secret = medians['with_secret_name']
        without_secret = medians['without_secret_name']
        ratio = without_secret / with_secret if with_secret else float('inf')
        print('R352_EDITOR_BUDGET ratio=%.4f limit=0.4000 without=%.6fs with=%.6fs' %
              (ratio, without_secret, with_secret), flush=True)
        self.assertLessEqual(without_secret, 0.40 * with_secret)

    def test_url_password_containing_at_redacts_to_last_authority_at(self):
        scheme = 'mysql:' + '//'

        # Case 1: single "@" inside password.
        url1 = scheme + 'u' + ':' + 'Pr0d' + '@' + 'Tail99' + '@h/db'
        expected1 = scheme + 'u:' + dbops.REDACTED + '@h/db'
        clean1 = dbops.redact(url1)
        self.assertEqual(clean1, expected1)
        for piece in ('Pr0d', 'Tail99'):
            self.assertNotIn(piece, clean1)

        # Case 2: multiple "@" characters inside password, with host port and path.
        url2 = scheme + 'admin' + ':' + 'a' + '@' + 'b' + '@' + 'c' + '@db.internal:3306/app'
        expected2 = scheme + 'admin:' + dbops.REDACTED + '@db.internal:3306/app'
        clean2 = dbops.redact(url2)
        self.assertEqual(clean2, expected2)
        for piece in ('a' + '@' + 'b', 'b' + '@' + 'c', '@' + 'c'):
            self.assertNotIn(piece, clean2)

        # Case 3: two URLs in one string; last-"@" stays inside each URL across whitespace and quotes.
        first = scheme + 'u' + ':' + 'pw' + '@' + 'h1'
        second = scheme + 'v' + ':' + 'p' + '@' + 'q' + '@h2/d'
        expected_first = scheme + 'u:' + dbops.REDACTED + '@h1'
        expected_second = scheme + 'v:' + dbops.REDACTED + '@h2/d'
        clean3 = dbops.redact('x ' + first + ' y ' + second)
        self.assertEqual(clean3, 'x ' + expected_first + ' y ' + expected_second)
        for piece in ('pw', 'p' + '@' + 'q', '@' + 'q', 'p' + '@'):
            self.assertNotIn(piece, clean3)
        self.assertIn('@h1', clean3)
        self.assertIn('@h2/d', clean3)
        for quote in ("'", '"', '`'):
            with self.subTest(quote=quote):
                wrapped = quote + first + quote + ',' + quote + second + quote
                clean_wrapped = dbops.redact(wrapped)
                self.assertEqual(clean_wrapped,
                                 quote + expected_first + quote + ',' + quote + expected_second + quote)
                for piece in ('pw', 'p' + '@' + 'q', '@' + 'q', 'p' + '@'):
                    self.assertNotIn(piece, clean_wrapped)

        # Case 4: "@" in query string after "/" stays outside the authority.
        url4 = scheme + 'u' + ':' + 'pw' + '@h/db?x=a@b'
        clean4 = dbops.redact(url4)
        self.assertEqual(clean4, scheme + 'u:' + dbops.REDACTED + '@h/db?x=a@b')
        self.assertNotIn('pw', clean4)
        self.assertIn('@h/db?x=a@b', clean4)

        # Case 5: case-1 URL in single quotes, with "jdbc:" prefix, and with "2*" prefix.
        for before, after in (("'", "'"), ('jdbc:', ''), ('2*', '')):
            with self.subTest(before=before, after=after):
                clean5 = dbops.redact(before + url1 + after)
                self.assertEqual(clean5, before + expected1 + after)
                for piece in ('Pr0d', 'Tail99'):
                    self.assertNotIn(piece, clean5)
        quoted_no_path = "'" + scheme + 'u' + ':' + 'Pr0d' + '@' + 'Tail99' + "@h'"
        clean_quoted_no_path = dbops.redact(quoted_no_path)
        self.assertEqual(clean_quoted_no_path, "'" + scheme + 'u:' + dbops.REDACTED + "@h'")
        for piece in ('Pr0d', 'Tail99'):
            self.assertNotIn(piece, clean_quoted_no_path)

    def test_url_at_redaction_stays_linear_on_400kb_inputs(self):
        for shape in ('repeated_urls', 'at_run'):
            with self.subTest(shape=shape):
                code = (
                    "import time\nfrom myflames import dbops\n"
                    "n = 400 * 1024\n"
                    "scheme = 'mysql:' + '//'\n"
                    "prefix = scheme + 'a' + ':'\n"
                    "shape = " + repr(shape) + "\n"
                    "if shape == 'repeated_urls':\n"
                    "    unit = prefix + 'b' + '@' + 'h/'\n"
                    "    value = (unit * (n // len(unit) + 1))[:n]\n"
                    "else:\n"
                    "    value = prefix + '@' * (n - len(prefix))\n"
                    "assert len(value) == n, len(value)\n"
                    "began = time.monotonic()\n"
                    "clean = dbops.redact(value)\n"
                    "elapsed = time.monotonic() - began\n"
                    "if shape == 'repeated_urls':\n"
                    "    assert (':' + 'b' + '@') not in clean\n"
                    "    assert clean.startswith(prefix + dbops.REDACTED + '@h/')\n"
                    "else:\n"
                    "    assert clean == prefix + dbops.REDACTED + '@'\n"
                    "print(elapsed)\n")
                result = subprocess.run([sys.executable, '-c', code],
                    cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=5, universal_newlines=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                elapsed = float(result.stdout.strip())
                print('URL_AT_TIMING %s %.6fs' % (shape, elapsed), flush=True)
                self.assertLess(elapsed, 1)

    def test_hex_hashes(self):
        text = ("CREATE USER `a`@`%` IDENTIFIED WITH 'caching_sha2_password' AS "
                "0x244124303041242A493F2F2725193549337A REQUIRE NONE")
        self.assertEqual(dbops.redact(text), "CREATE USER `a`@`%` IDENTIFIED WITH 'caching_sha2_password' AS "
                                             "'[redacted]' REQUIRE NONE")
        self.assertNotIn('0x2441', dbops.redact('ERROR near AS 0x2441ABCD'))
        # A quoted plugin name ending in "password" is not a PASSWORD option (it used to swallow the rest).
        text = "CREATE USER `r`@`%` IDENTIFIED WITH 'caching_sha2_password' REQUIRE NONE ACCOUNT LOCK"
        self.assertEqual(dbops.redact(text), text)

    def test_secrets(self):
        cases = {
            "CREATE USER 'u'@'%' IDENTIFIED BY 's3cret'": "CREATE USER 'u'@'%' IDENTIFIED BY '[redacted]'",
            "ALTER USER u IDENTIFIED WITH caching_sha2_password BY 'new' REPLACE 'old'":
                "ALTER USER u IDENTIFIED WITH caching_sha2_password BY '[redacted]' REPLACE '[redacted]'",
            "CREATE USER u IDENTIFIED WITH mysql_native_password AS '*ABCD'":
                "CREATE USER u IDENTIFIED WITH mysql_native_password AS '[redacted]'",
            "CREATE USER u IDENTIFIED BY PASSWORD '*94BD'": "CREATE USER u IDENTIFIED BY PASSWORD '[redacted]'",
            "CREATE USER u IDENTIFIED VIA ed25519 USING PASSWORD('pw')":
                "CREATE USER u IDENTIFIED VIA ed25519 USING PASSWORD('[redacted]')",
            "SET PASSWORD FOR 'u'@'h' = PASSWORD('x1')": "SET PASSWORD FOR 'u'@'h' = PASSWORD('[redacted]')",
            "SET PASSWORD = 'plain'": "SET PASSWORD = '[redacted]'",
            "CHANGE REPLICATION SOURCE TO SOURCE_USER='r', SOURCE_PASSWORD='p4', SOURCE_HOST='h'":
                "CHANGE REPLICATION SOURCE TO SOURCE_USER='r', SOURCE_PASSWORD='[redacted]', SOURCE_HOST='h'",
            'CHANGE MASTER TO MASTER_PASSWORD = "p4"': 'CHANGE MASTER TO MASTER_PASSWORD = "[redacted]"',
            "START REPLICA USER='r' PASSWORD='rp'": "START REPLICA USER='r' PASSWORD='[redacted]'",
            "CLONE INSTANCE FROM 'u'@'h':3306 IDENTIFIED BY 'cl'":
                "CLONE INSTANCE FROM 'u'@'h':3306 IDENTIFIED BY '[redacted]'",
            "CREATE SERVER s FOREIGN DATA WRAPPER mysql OPTIONS (USER 'r', PASSWORD 'f', HOST 'h')":
                "CREATE SERVER s FOREIGN DATA WRAPPER mysql OPTIONS (USER 'r', PASSWORD '[redacted]', HOST 'h')",
            "ALTER USER u IDENTIFIED BY 'a\\'b' ": "ALTER USER u IDENTIFIED BY '[redacted]' ",
            "IDENTIFIED BY 'never closed": "IDENTIFIED BY '[redacted]'",
            'SELECT 1': 'SELECT 1',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dbops.redact(text), expected)

    def test_random_password_replace_redacted_without_explicit_secret(self):
        prefix = 'ALTER USER u IDENTIFIED BY RANDOM PASSWORD REPLACE '
        for literal in ("'old_secret'", "'old''secret'", '"old_secret"', "'unfinished"):
            with self.subTest(literal=literal):
                self.assertEqual(dbops.redact(prefix + literal), prefix + literal[0] + '[redacted]' + literal[0])
        statement = "SELECT REPLACE(value, 'old', 'new') FROM t"
        self.assertEqual(dbops.redact(statement), statement)

    def test_connection_password_anywhere(self):
        self.assertEqual(dbops.redact("Access denied for hunter2 ('hunter2')", 'hunter2'),
                         "Access denied for [redacted] ('[redacted]')")
        self.assertEqual(dbops.redact("x 'it''s' y it\\'s", "it's"), "x '[redacted]' y [redacted]")
        self.assertIsNone(dbops.redact(None))
        self.assertEqual(dbops.redact_value({'a': ['pw is pw1'], 'password': 'pw1', 'n': 3}, 'pw1'),
                         {'a': ['pw is [redacted]'], 'n': 3})

    def test_error_code(self):
        self.assertEqual(dbops.error_code('mysql client exited 1: ERROR 1095 (HY000) at line 1: x'), 1095)
        self.assertIsNone(dbops.error_code('Lost connection'))


class TestTargetNotConnected(unittest.TestCase):
    def test_login_refusals_require_no_statement_or_guard_evidence(self):
        from myflames.connector import ConnectorError
        for code in (1045, 1698):
            with self.subTest(code=code):
                text = "ERROR %d (28000): Access denied for user 'app'@'localhost'" % code
                self.assertTrue(dbops.target_not_connected(ConnectorError(text)))
                self.assertFalse(dbops.target_not_connected(ConnectorError(
                    text.replace('(28000):', '(28000) at line 2:'))))
                acknowledged = ConnectorError(text)
                acknowledged.guard_markers = ('myflames_guard_acknowledged',)
                self.assertFalse(dbops.target_not_connected(acknowledged))


class TestConnectionErrorText(unittest.TestCase):
    """Common connection errors lead with plain words and what to check, then
    the client's own error line (libmysql/errmsg.cc and MariaDB
    sql-common/errmsg.c texts)."""

    def test_cannot_connect_mysql_and_mariadb(self):
        mysql = "ERROR 2003 (HY000): Can't connect to MySQL server on '127.0.0.1:3306' (61)"
        text = dbops.connection_error_text("Could not read the server status: " + mysql)
        self.assertEqual(text, "Could not connect to the server. Check that it is running and that the host "
                               "and port are right. " + mysql)
        # The client wrapper and the reader's context never stack before it.
        self.assertEqual(dbops.connection_error_text("mysql client exited 1: " + mysql), text)
        self.assertNotIn("Could not read", text)
        # MariaDB's client reports a refused TCP connection as 2002.
        maria = "ERROR 2002 (HY000): Can't connect to server on '10.0.0.5' (115)"
        self.assertEqual(dbops.connection_error_text("mysql client exited 1: " + maria),
                         dbops._CANNOT_CONNECT + " " + maria)
        socket = "ERROR 2002 (HY000): Can't connect to local MySQL server through socket '/tmp/mysql.sock' (2)"
        self.assertTrue(dbops.connection_error_text(socket).startswith(
            "Could not connect to the server. Check that it is running and that the socket path is right. ERROR 2002"))

    def test_unknown_host_keeps_the_host_and_a_tunnel_note(self):
        error = "mysql client exited 1: ERROR 2005 (HY000): Unknown MySQL server host 'db.exmaple' (8)\n SSH said: x"
        self.assertEqual(dbops.connection_error_text(error),
                         "Could not find the server's host name. Check the host for a typing error, or use its IP "
                         "address. ERROR 2005 (HY000): Unknown MySQL server host 'db.exmaple' (8) SSH said: x")

    def test_lost_connection_keeps_what_was_being_done(self):
        for code, message in ((2013, "Lost connection to MySQL server during query"),
                              (2006, "MySQL server has gone away"), (2006, "Server has gone away")):
            with self.subTest(code=code, message=message):
                error = "mysql client exited 1: ERROR %d (HY000): %s" % (code, message)
                text = dbops.connection_error_text("Could not end connection 41: " + error[len("mysql client exited 1: "):])
                self.assertTrue(text.startswith("Lost the connection to the server. Check that it is still running"))
                self.assertTrue(text.endswith("Could not end connection 41: ERROR %d (HY000): %s" % (code, message)))
                self.assertEqual(dbops.connection_error_text(error), dbops._LOST + " ERROR %d (HY000): %s" % (code, message))

    def test_password_hint_only_for_access_denied(self):
        denied = "ERROR 1045 (28000): Access denied for user 'app'@'172.17.0.1' (using password: YES)"
        self.assertEqual(dbops.connection_error_text("mysql client exited 1: " + denied),
                         "The server refused the user name or password. Check both, and that the account may "
                         "connect from your address. " + denied)
        no_password = "ERROR 1045 (28000): Access denied for user 'app'@'localhost' (using password: NO)"
        self.assertEqual(dbops.connection_error_text(no_password),
                         "The server refused the login, and no password was sent. Enter the password, or check "
                         "the user name. " + no_password)
        # A 1045 without the server's Access denied words is not called a password error.
        self.assertIsNone(dbops.connection_error_text('ERROR 1045 (28000) at line 1: near "SHOW": syntax error'))

    def test_other_errors_are_left_alone(self):
        for text in ("ERROR 1227 (42000): Access denied; you need (at least one of) the PROCESS privilege(s)",
                     "ERROR 1142 (42000): SELECT command denied to user 'app'@'%' for table 'x'",
                     "ERROR 2026 (HY000): SSL connection error: unknown error number",
                     "Query timed out after 30s: SHOW GLOBAL STATUS", "Lost connection", "", None):
            with self.subTest(text=text):
                self.assertIsNone(dbops.connection_error_text(text))


# ---- the audit trail ----------------------------------------------------------------------

class TestAudit(TempHome):
    CONNECTION = {'host': 'db.internal', 'port': 3307, 'user': 'app', 'password': 'pw-not-stored'}

    def trail(self, **kw):
        safety = {'profile_id': 'p1', 'profile_name': 'prod', 'type': 'production', 'read_only': False}
        return dbops.AuditTrail(self.store, kw.pop('operation', 'kill'), kw.pop('surface', 'cli'),
                                connection=self.CONNECTION, safety=safety, password='pw-not-stored', **kw)

    def test_set_password_diagnostic_and_random_replace_audit_regressions(self):
        for index, (label, statement, secrets) in enumerate(SET_PASSWORD_REDACTION_CASES, 1):
            with self.subTest(case=label):
                self.trail(operation='accounts.password').outcome(
                    [statement], 'refused', message=statement)
                records = self.store.audit_records()
                self.assertEqual(len(records), index)
                stored = json.dumps(records)
                for secret in secrets:
                    self.assertNotIn(secret, stored)
                self.assertTrue(all('[redacted]' in record['statements'][0] for record in records))
                self.assertTrue(all('[redacted]' in record['message'] for record in records))

    def test_set_password_names_and_comments_cannot_expose_audit_secrets(self):
        for prefix in ("SET PASSWORD FOR 'a=b'@'%' = ", 'SET PASSWORD FOR `a;b`@`%` = ',
                       'SET PASSWORD /* x=1 */ = ',
                       "SELECT 'SET PASSWORD'; SET PASSWORD FOR 'u=;name'@'%' = "):
            with self.subTest(prefix=prefix):
                statement = prefix + "'audit-private-value'"
                self.trail(operation='accounts.password').outcome([statement], 'refused', message=statement)
        records = self.store.audit_records()
        self.assertEqual(len(records), 4)
        self.assertNotIn('audit-private-value', json.dumps(records))
        self.assertTrue(all('[redacted]' in record['statements'][0] for record in records))

    def test_lifecycle_pending_then_ok(self):
        trail = self.trail(policy={'action': 'confirm', 'kind': 'risky'})
        trail.server('mysql', '8.4.6')
        audit_id = trail.attempt(["CREATE USER u IDENTIFIED BY 'secret' -- pw-not-stored"],
                                 target={'user': 'app', 'host': '10.0.0.5'}, verified=['user', 'host'])
        record = self.store.audit_records()[0]
        self.assertEqual((record['id'], record['status'], record['outcome_unknown'], record['finished_at']),
                         (audit_id, 'pending', True, None))
        self.assertEqual(record['statements'], ["CREATE USER u IDENTIFIED BY '[redacted]' -- [redacted]"])
        self.assertEqual(record['connection'], {'profile_id': 'p1', 'profile_name': 'prod', 'type': 'production',
                                                'read_only': False, 'host': 'db.internal', 'port': 3307,
                                                'user': 'app', 'engine': 'mysql', 'server_version': '8.4.6'})
        self.assertEqual((record['target'], record['verified'], record['policy']),
                         ({'user': 'app', 'host': '10.0.0.5'}, ['user', 'host'], {'action': 'confirm', 'kind': 'risky'}))
        trail.complete('ok', outcome='killed', message='done', after={'present': False})
        record = self.store.audit_records()[0]
        self.assertEqual((record['status'], record['outcome'], record['outcome_unknown'], record['after']),
                         ('ok', 'killed', False, {'present': False}))
        self.assertIsNotNone(record['finished_at'])
        self.assertGreaterEqual(record['duration_ms'], 0)
        self.assertEqual((record['surface'], record['operation']), ('cli', 'kill'))
        # A second complete never rewrites a finished record.
        self.assertFalse(dbops.complete(self.store, audit_id, 'error', message='late'))
        self.assertEqual(self.store.audit_records()[0]['status'], 'ok')
        self.assertNotIn(b'pw-not-stored', self.store.path.read_bytes())
        self.assertNotIn(b'secret', self.store.path.read_bytes())
        if os.name == 'posix':
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.store.path.parent.stat().st_mode & 0o777, 0o700)

    def test_a_crash_leaves_outcome_unknown(self):
        trail = self.trail()
        trail.attempt(['/*myflames:kill*/ KILL CONNECTION 41'])
        # The process dies here: no complete(). A new store sees the pending record.
        record = WorkspaceStore(self.store.path).audit_records()[0]
        self.assertEqual((record['status'], record['outcome_unknown']), ('pending', True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            workspace_cli.cmd_audit([])
        self.assertIn('unknown', out.getvalue())

    def test_error_codes_and_unsent_outcomes(self):
        trail = self.trail()
        trail.attempt(['/*myflames:kill*/ KILL CONNECTION 41'])
        trail.complete('error', outcome='denied', error='ERROR 1095 (HY000): You are not owner of thread 41',
                       message='refused')
        self.assertEqual(self.store.audit_records()[0]['error_code'], 1095)
        for status in ('declined', 'refused'):
            self.trail().outcome(['KILL 7'], status, outcome=status, message='Nothing was ended.')
        failed = self.trail()
        failed.fail(ValueError('connection refused for pw-not-stored'), ['KILL 8'])
        records = self.store.audit_records()
        self.assertEqual([r['status'] for r in records], ['error', 'refused', 'declined', 'error'])
        self.assertEqual(records[0]['message'], 'connection refused for [redacted]')
        self.assertTrue(all(r['finished_at'] for r in records))

    def test_10_interrupt_or_timeout_after_sending_is_unknown(self):
        for exc, outcome in ((KeyboardInterrupt(), 'interrupted'),
                             (ValueError('Query timed out after 12s: KILL 4'), 'timeout'),
                             (ValueError('ERROR 2013 (HY000): Lost connection to server during query'), 'timeout')):
            with self.subTest(exc=repr(exc)):
                trail = self.trail()
                trail.attempt(['KILL 4'])
                self.assertTrue(trail.pending)
                trail.fail(exc)
                record = self.store.audit_records()[0]
                self.assertEqual((record['status'], record['outcome'], record['outcome_unknown']),
                                 ('unknown', outcome, True))
                self.assertIn('its outcome is unknown', record['message'])
        trail = self.trail()
        trail.attempt(['KILL 5'])
        trail.fail(ValueError('ERROR 1045: Access denied'))
        self.assertEqual(self.store.audit_records()[0]['status'], 'error')

    def test_15_unsent_outcomes_are_written_in_one_transaction(self):
        with mock.patch.object(WorkspaceStore, 'audit_complete', side_effect=AssertionError('second write')):
            dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'cli', 'declined', message='no')
            self.trail().outcome(['KILL 2'], 'refused', message='read-only')
        records = self.store.audit_records()
        self.assertEqual([(r['status'], r['outcome_unknown']) for r in records],
                         [('refused', False), ('declined', False)])
        self.assertTrue(all(r['finished_at'] for r in records))

    def test_validation(self):
        with self.assertRaises(ValueError):
            dbops.record_attempt(self.store, 'kill', ['KILL 1'], 'web')
        with self.assertRaises(ValueError):
            dbops.complete(self.store, 'x', 'pending')

    def test_filters_limit_and_purge(self):
        for name, host in (('prod', 'db.internal'), (None, 'other.host')):
            safety = {'profile_id': None, 'profile_name': name, 'type': 'production', 'read_only': False}
            dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'ui', 'refused', connection={'host': host, 'port': 3306},
                                 safety=safety)
        self.assertEqual(len(self.store.audit_records(server='PROD')), 1)
        self.assertEqual(len(self.store.audit_records(server='other.host:3306')), 1)
        self.assertEqual(len(self.store.audit_records(limit=1)), 1)
        self.assertEqual(len(self.store.audit_records(since_days=1)), 2)
        # Age one record by 200 days: --since and purge leave it out.
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = '2020-01-01T00:00:00.000000+00:00' WHERE host = 'other.host'")
        self.assertEqual(len(self.store.audit_records(since_days=30)), 1)
        self.assertEqual(self.store.audit_purge(30), 1)
        self.assertEqual(self.store.audit_purge(0), 1)
        self.assertEqual(self.store.audit_records(), [])
        for bad in ({'since_days': 0}, {'since_days': True}, {'limit': 0}, {'server': 5}):
            with self.assertRaises(ValueError):
                self.store.audit_records(**bad)
        with self.assertRaises(ValueError):
            self.store.audit_purge(-1)

    def test_status_filter_applies_before_the_limit_and_pages_continue(self):
        """An older record beyond the newest page is found by its status, and
        `before` pages through the rest in the list's order (review gate)."""
        from myflames.workspace_store import _cutoff
        dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'ui', 'refused', message='old')
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = ?", (_cutoff(10),))
        for n in range(5):
            dbops.record_outcome(self.store, 'kill', ['KILL %d' % (n + 10)], 'ui', 'declined')
        report = dbops.audit_report(self.store, limit=3)
        self.assertEqual((len(report['records']), report['has_more']), (3, True))
        self.assertNotIn('refused', [r['status'] for r in report['records']])
        found = dbops.audit_report(self.store, limit=3, status='refused')
        self.assertEqual(([r['statements'] for r in found['records']], found['has_more']), ([['KILL 1']], False))
        # Paging: every record exactly once, newest first.
        seen, before = [], None
        while True:
            page = dbops.audit_report(self.store, limit=2, before=before)
            seen.extend(r['id'] for r in page['records'])
            if not page['has_more']:
                break
            before = page['records'][-1]['id']
        self.assertEqual(seen, [r['id'] for r in self.store.audit_records()])
        self.assertEqual(len(seen), 6)
        self.assertEqual(dbops.audit_report(self.store, limit=100000)['has_more'], False)
        for bad in ({'status': 'done'}, {'before': ''}, {'before': 5}, {'before': 'missing'}):
            with self.assertRaises(ValueError):
                self.store.audit_records(**bad)
        for limit in (0, 100001, True):
            with self.assertRaises(ValueError):
                dbops.audit_report(self.store, limit=limit)
        exported = json.loads(dbops.audit_export(self.store, 'json', status='refused')['content'])
        self.assertEqual([r['statements'] for r in exported['records']], [['KILL 1']])
        self.assertNotIn('has_more', exported)
        rows = list(csv.reader(io.StringIO(dbops.audit_export(self.store, 'csv', status='declined')['content'])))
        self.assertEqual(len(rows), 6)

    def test_retention_purges_opportunistically(self):
        from myflames.workspace_store import _cutoff
        self.assertEqual(self.store.audit_retention_days(), 90)
        dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'cli', 'refused')
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = '2020-01-01T00:00:00.000000+00:00'")
        dbops.record_outcome(self.store, 'kill', ['KILL 2'], 'cli', 'refused')
        self.assertEqual([r['statements'] for r in self.store.audit_records()], [['KILL 2']])
        dbops.record_outcome(self.store, 'kill', ['KILL 3'], 'cli', 'refused')
        with contextlib.closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("UPDATE audit SET attempted_at = ? WHERE statements LIKE '%KILL 3%'", (_cutoff(10),))
        self.assertEqual(self.store.set_audit_retention_days(5), 5)
        self.assertEqual(self.store.audit_retention_days(), 5)
        self.assertEqual([r['statements'] for r in self.store.audit_records()], [['KILL 2']])
        for bad in (0, 3651, '5', True):
            with self.assertRaises(ValueError):
                self.store.set_audit_retention_days(bad)

    def test_reading_never_creates_the_database(self):
        self.assertEqual(self.store.audit_records(), [])
        self.assertEqual(self.store.audit_purge(0), 0)
        self.assertEqual(self.store.audit_retention_days(), 90)
        self.assertFalse(self.store.path.exists())

    def test_export_json_and_csv(self):
        dbops.record_outcome(self.store, 'sql', ['=HYPERLINK("http://x")', "SET PASSWORD = 'p'"], 'ui', 'declined',
                             message='-cmd')
        exported = dbops.audit_export(self.store, 'json')
        self.assertEqual((exported['format'], exported['mime']), ('json', 'application/json'))
        self.assertRegex(exported['filename'], r'^myflames-audit-\d{8}-\d{6}\.json$')
        data = json.loads(exported['content'])
        self.assertEqual((data['schema_version'], data['retention_days']), ('audit-1.0', 90))
        self.assertEqual(data['records'][0]['statements'][1], "SET PASSWORD = '[redacted]'")
        exported = dbops.audit_export(self.store, 'csv')
        rows = list(csv.reader(io.StringIO(exported['content'])))
        self.assertEqual(rows[0], list(dbops.AUDIT_CSV_COLUMNS))
        record = dict(zip(rows[0], rows[1]))
        # Cells a spreadsheet would run as a formula start with a quote.
        self.assertEqual(record['statements'], '\'=HYPERLINK("http://x")\nSET PASSWORD = \'[redacted]\'')
        self.assertEqual((record['message'], record['status']), ("'-cmd", 'declined'))
        with self.assertRaises(ValueError):
            dbops.audit_export(self.store, 'xml')


# ---- profiles: type, read-only, migration ---------------------------------------------------

class TestProfileSafety(TempHome):
    def test_new_profiles_are_production_and_keep_settings_on_save(self):
        saved = self.store.save('profile', {'name': 'prod', 'host': 'db', 'user': 'app'})
        self.assertEqual((saved['type'], saved['read_only']), ('production', False))
        dev = self.store.save('profile', {'name': 'dev', 'host': 'dev', 'type': 'development', 'read_only': True})
        self.assertEqual((dev['type'], dev['read_only']), ('development', True))
        # A save that does not mention them (an older UI form) keeps them on the same endpoint.
        again = self.store.save('profile', {'name': 'dev again', 'host': 'dev'}, dev['id'])
        self.assertEqual((again['name'], again['type'], again['read_only']), ('dev again', 'development', True))
        changed = self.store.save('profile', {'name': 'dev', 'host': 'dev', 'read_only': False}, dev['id'])
        self.assertEqual((changed['type'], changed['read_only']), ('development', False))

    def test_6_a_new_endpoint_without_a_type_is_production_again(self):
        dev = self.store.save('profile', {'name': 'dev', 'host': 'dev', 'user': 'app', 'type': 'development'})
        same = self.store.save('profile', {'name': 'dev renamed', 'host': 'dev', 'user': 'app', 'database': 'x'},
                               dev['id'])
        self.assertEqual(same['type'], 'development')
        moved = self.store.save('profile', {'name': 'dev', 'host': 'prod', 'user': 'app'}, dev['id'])
        self.assertEqual(moved['type'], 'production')
        chosen = self.store.save('profile', {'name': 'dev', 'host': 'other', 'user': 'app', 'type': 'development'},
                                 dev['id'])
        self.assertEqual(chosen['type'], 'development')

    def test_bad_values_do_not_create_the_database(self):
        for value in ({'type': 'staging'}, {'type': 1}, {'read_only': 'yes'}, {'read_only': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.save('profile', dict(value, host='db'))
        self.assertFalse(self.store.path.exists())

    def test_set_profile_safety_keeps_the_saved_password(self):
        saved = self.store.save('profile', {'name': 'prod', 'host': 'db', 'user': 'app'}, password='pw',
                                remember=True)
        changed = self.store.set_profile_safety(saved['id'], 'development', True)
        self.assertEqual((changed['type'], changed['read_only'], changed['has_saved_password']),
                         ('development', True, True))
        filled = self.store.connection_with_saved_password({'profile_id': saved['id'], 'host': 'db', 'user': 'app'})
        self.assertEqual(filled['password'], 'pw')
        self.assertEqual(self.store.set_profile_safety(saved['id'], read_only=False)['type'], 'development')
        forgotten = self.store.forget_password(saved['id'])
        self.assertEqual((forgotten['type'], forgotten['read_only']), ('development', False))
        for args in ((saved['id'],), (saved['id'], 'other'), (saved['id'], None, 'no'), ('missing', 'production')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.set_profile_safety(*args)

    def test_existing_profiles_migrate_to_production(self):
        path = self.store.path
        path.parent.mkdir(mode=0o700, parents=True)
        with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
            # The schema and a profile written by myflames before connection types.
            conn.execute('CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT NOT NULL, updated TEXT NOT NULL, '
                         'payload TEXT NOT NULL)')
            conn.execute("INSERT INTO records VALUES ('p1', 'profile', '2026-01-01', ?)",
                         (json.dumps({'name': 'old', 'host': 'db', 'port': 3306, 'remember_password': False}),))
            conn.execute("INSERT INTO records VALUES ('p2', 'profile', '2026-01-01', 'not json')")
        os.chmod(str(path), 0o600)
        profile = WorkspaceStore(path).get('profile', 'p1')
        self.assertEqual((profile['type'], profile['read_only']), ('production', False))
        with contextlib.closing(sqlite3.connect(str(path))) as conn, conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 1)
            stored = json.loads(conn.execute("SELECT payload FROM records WHERE id = 'p1'").fetchone()[0])
            self.assertEqual((stored['type'], stored['read_only']), ('production', False))
            self.assertEqual(conn.execute("SELECT payload FROM records WHERE id = 'p2'").fetchone()[0], 'not json')
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({'audit', 'settings'} <= tables)


# ---- CLI ----------------------------------------------------------------------------------------

def run_cli(command, argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, 'stdin', stdin or io.StringIO('')):
        try:
            command(list(argv))
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class TtyInput(io.StringIO):
    def isatty(self):
        return True


class TestAuditCLI(TempHome):
    def add(self, status='refused', host='db.internal', name='prod'):
        safety = {'profile_id': None, 'profile_name': name, 'type': 'production', 'read_only': True}
        return dbops.record_outcome(self.store, 'kill', ['/*myflames:kill*/ KILL CONNECTION 41'], 'cli', status,
                                    outcome='read_only', connection={'host': host, 'port': 3306, 'user': 'app'},
                                    safety=safety, message='Connection prod is read-only')

    def test_list_text_json_csv(self):
        code, out, err = run_cli(workspace_cli.cmd_audit, [])
        self.assertEqual((code, out), (0, 'WHEN (UTC)  STATUS  FROM  OPERATION  SERVER  STATEMENT  ID\n'))
        self.assertIn('No audit records yet', err)
        audit_id = self.add()
        code, out, err = run_cli(workspace_cli.cmd_audit, ['list'])
        self.assertEqual(code, 0, err)
        self.assertIn('WHEN (UTC)', out)
        self.assertIn('refused (read_only)', out)
        self.assertIn(audit_id[:8], out)
        self.assertIn('Kept for 90 days', err)
        code, out, _ = run_cli(workspace_cli.cmd_audit, ['--since', '7', '--server', 'prod', '--json'])
        data = json.loads(out)
        self.assertEqual((data['schema_version'], len(data['records'])), ('audit-1.0', 1))
        code, out, _ = run_cli(workspace_cli.cmd_audit, ['list', '--csv', '-q'])
        self.assertTrue(out.startswith('id,attempted_at,'))
        code, out, err = run_cli(workspace_cli.cmd_audit, ['list', '--server', 'nope'])
        self.assertIn('No audit records match', err)

    def test_export_to_a_file(self):
        self.add()
        path = Path(self.temp.name) / 'audit.csv'
        code, out, err = run_cli(workspace_cli.cmd_audit, ['export', '--csv', '-o', str(path)])
        self.assertEqual((code, out), (0, ''))
        self.assertIn('KILL CONNECTION 41', path.read_text(encoding="utf-8"))
        code, out, _ = run_cli(workspace_cli.cmd_audit, ['export'])
        self.assertEqual(json.loads(out)['records'][0]['status'], 'refused')

    def test_purge_needs_confirmation(self):
        self.add()
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '0'])
        self.assertEqual(code, 1)
        self.assertIn('Re-run with --yes', err)
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '0'], stdin=TtyInput('n\n'))
        self.assertEqual(code, 1)
        self.assertIn('Nothing was deleted', err)
        self.assertEqual(len(self.store.audit_records()), 1)
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '0'], stdin=TtyInput('y\n'))
        self.assertEqual(code, 0)
        self.assertIn('Deleted 1 audit record.', err)
        self.add()
        code, _, err = run_cli(workspace_cli.cmd_audit, ['purge', '--older-than', '30', '--yes'])
        self.assertEqual((code, len(self.store.audit_records())), (0, 1))

    def test_usage_errors_exit_2(self):
        for argv in (['purge'], ['purge', '--older-than', '-1'], ['list', '--since', '0'], ['list', '--json', '--csv'],
                     ['bogus'], ['list', '--limit', '0']):
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(workspace_cli.cmd_audit, argv)[0], 2)

    def test_root_dispatch_and_help(self):
        from myflames import cli
        with mock.patch.object(sys, 'argv', ['myflames', 'audit', '--json']):
            code, out, _ = run_cli(lambda argv: cli.main(), [])
        self.assertEqual((code, json.loads(out)['schema_version']), (0, 'audit-1.0'))
        with mock.patch.object(sys, 'argv', ['myflames', '--help']):
            code, out, _ = run_cli(lambda argv: cli.main(), [])
        self.assertIn('audit     Local audit trail of server writes', out)


class TestProfilesSetCLI(TempHome):
    def test_set_and_list(self):
        saved = self.store.save('profile', {'name': 'prod', 'host': 'db', 'user': 'app'})
        code, out, err = run_cli(workspace_cli.cmd_profiles, ['list'])
        self.assertIn('TYPE', out)
        self.assertIn('production  read-write', out)
        code, out, err = run_cli(workspace_cli.cmd_profiles, ['set', 'prod', '--type', 'development', '--read-only'])
        self.assertEqual((code, out), (0, ''))
        self.assertIn("Profile 'prod' is now Development and read-only.", err)
        code, out, _ = run_cli(workspace_cli.cmd_profiles, ['list', '--json'])
        profile = json.loads(out)['profiles'][0]
        self.assertEqual((profile['id'], profile['type'], profile['read_only']), (saved['id'], 'development', True))
        code, _, err = run_cli(workspace_cli.cmd_profiles, ['set', saved['id'], '--read-write', '-q'])
        self.assertEqual((code, err), (0, ''))
        self.assertFalse(self.store.get('profile', saved['id'])['read_only'])
        for argv in (['set', 'prod'], ['set', 'prod', '--type', 'staging'], ['set', 'nope', '--read-only'],
                     ['set', 'prod', '--read-only', '--read-write']):
            with self.subTest(argv=argv):
                self.assertEqual(run_cli(workspace_cli.cmd_profiles, argv)[0], 2)


# ---- endpoints ---------------------------------------------------------------------------------

class TestEndpoints(TempHome):
    def workspace(self):
        return type('Workspace', (), {'store': self.store})()

    def test_audit_list_export_settings(self):
        dbops.record_outcome(self.store, 'kill', ['KILL 1'], 'ui', 'refused', message='read-only')
        listed = ui.api_response('/api/audit/list', {'since_days': 7}, self.workspace())
        self.assertEqual((listed['schema_version'], len(listed['records'])), ('audit-1.0', 1))
        exported = ui.api_response('/api/audit/export', {'format': 'csv'}, self.workspace())
        self.assertEqual(exported['mime'], 'text/csv')
        self.assertIn('KILL 1', exported['content'])
        self.assertEqual(ui.api_response('/api/audit/settings', {}, self.workspace()), {'retention_days': 90})
        self.assertEqual(ui.api_response('/api/audit/settings', {'retention_days': 30}, self.workspace()),
                         {'retention_days': 30})
        with self.assertRaises(ValueError):
            ui.api_response('/api/audit/settings', {'retention_days': 0}, self.workspace())
        # Status and paging go through the endpoints too.
        dbops.record_outcome(self.store, 'kill', ['KILL 2'], 'ui', 'declined')
        listed = ui.api_response('/api/audit/list', {'status': 'refused', 'limit': 1}, self.workspace())
        self.assertEqual(([r['status'] for r in listed['records']], listed['has_more']), (['refused'], False))
        first = ui.api_response('/api/audit/list', {'limit': 1}, self.workspace())
        self.assertTrue(first['has_more'])
        older = ui.api_response('/api/audit/list', {'limit': 1, 'before': first['records'][0]['id']}, self.workspace())
        self.assertEqual(([r['statements'] for r in older['records']], older['has_more']), ([['KILL 1']], False))
        exported = ui.api_response('/api/audit/export', {'format': 'json', 'status': 'declined'}, self.workspace())
        self.assertEqual([r['status'] for r in json.loads(exported['content'])['records']], ['declined'])
        with self.assertRaisesRegex(ValueError, 'requires a running workspace'):
            ui.api_response('/api/audit/list', {})
        with self.assertRaises(ValueError):
            ui.api_response('/api/audit/other', {}, self.workspace())

    def test_14_ui_declines_are_audited(self):
        saved = self.store.save('profile', {'name': 'dev', 'host': 'db', 'user': 'app', 'type': 'development'})
        result = ui.api_response('/api/audit/declined', {
            'operation': 'kill', 'statements': ["/*myflames:kill*/ KILL CONNECTION 41 -- IDENTIFIED BY 'pw'"],
            'target': {'connection_id': 41}, 'connection': {'profile_id': saved['id'], 'host': 'db', 'user': 'app',
                                                            'password': 'typed-secret'}}, self.workspace())
        record = self.store.audit_records()[0]
        self.assertEqual((record['id'], record['status'], record['surface'], result['status']),
                         (result['audit_id'], 'declined', 'ui', 'declined'))
        self.assertEqual((record['connection']['type'], record['connection']['profile_name']), ('development', 'dev'))
        self.assertNotIn(b'typed-secret', self.store.path.read_bytes())
        self.assertIn("IDENTIFIED BY '[redacted]'", record['statements'][0])
        for bad in ({}, {'operation': 'kill'}, {'operation': 'kill', 'statements': 'KILL 1'},
                    {'operation': 'kill', 'statements': ['KILL 1'], 'target': 'x'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ui.api_response('/api/audit/declined', bad, self.workspace())

    def test_routes_are_allowlisted_and_not_connection_paths(self):
        import inspect
        source = inspect.getsource(ui.WorkspaceHandler.do_POST)
        for route in ('/api/audit/list', '/api/audit/export', '/api/audit/settings', '/api/audit/declined'):
            self.assertIn('"%s"' % route, source)
            self.assertNotIn(route, ui._CONNECTION_PATHS)
        self.assertIn('/api/live/kill', ui._WRITE_PATHS)

    def test_profiles_accept_and_return_type_and_read_only(self):
        saved = ui.api_response('/api/profiles/save', {'value': {'name': 'dev', 'host': 'db', 'type': 'development',
                                                                 'read_only': True}}, self.workspace())
        self.assertEqual((saved['type'], saved['read_only']), ('development', True))
        items = ui.api_response('/api/profiles/list', {}, self.workspace())['items']
        self.assertEqual([(p['type'], p['read_only']) for p in items], [('development', True)])
        with self.assertRaises(ValueError):
            ui.api_response('/api/profiles/save', {'value': {'host': 'db', 'type': 'qa'}}, self.workspace())


class TestAuditSafetyRegressions(TempHome):
    def test_declined_refused_and_unknown_are_once_only_and_redacted(self):
        secret = 'audit-regression-secret'
        statement = "ALTER USER 'app'@'%' IDENTIFIED BY 'new-account-secret' /* " + secret + ' */'
        ids = []
        for status in ('declined', 'refused', 'unknown'):
            with self.subTest(status=status):
                trail = dbops.AuditTrail(self.store, 'accounts.password', 'ui',
                                         connection={'host': 'db', 'password': secret}, password=secret,
                                         policy={'note': secret})
                target = {'password': secret, 'nested': {'detail': secret}}
                if status == 'unknown':
                    audit_id = trail.attempt([statement], target=target, verified=[secret])
                    pending = next(r for r in self.store.audit_records() if r['id'] == audit_id)
                    self.assertEqual((pending['status'], pending['outcome_unknown']), ('pending', True))
                    trail.fail(ValueError('ERROR 2013: Lost connection to server: ' + secret))
                else:
                    audit_id = trail.outcome([statement], status, outcome=status,
                                             message='Nothing sent: ' + secret, target=target, verified=[secret])
                ids.append(audit_id)
                original = next(r for r in self.store.audit_records() if r['id'] == audit_id)
                self.assertEqual((original['status'], original['outcome_unknown']), (status, status == 'unknown'))
                self.assertIsNotNone(original['finished_at'])
                # Handlers can report the same exception at multiple layers.
                # None may duplicate an outcome or overwrite the first result.
                trail.fail(ValueError('late failure: ' + secret), [statement])
                trail.complete('ok', message='late success')
                self.assertEqual(trail.outcome([statement], 'refused'), audit_id)
                self.assertFalse(dbops.complete(self.store, audit_id, 'ok'))
                self.assertEqual(next(r for r in self.store.audit_records() if r['id'] == audit_id), original)
        records = self.store.audit_records()
        self.assertEqual(len(records), 3)
        self.assertEqual({r['id'] for r in records}, set(ids))
        reopened = WorkspaceStore(self.store.path).audit_records()
        self.assertEqual(reopened, records)
        for value in (secret, 'new-account-secret'):
            self.assertNotIn(value, json.dumps(records))
            self.assertNotIn(value.encode('utf-8'), self.store.path.read_bytes())

    def test_failed_audit_insert_prevents_a_real_write_path_sending(self):
        from myflames import locks
        from test_locks import KillScenario, bound_kill_expect
        scenario = KillScenario()
        trail = dbops.AuditTrail(self.store, 'kill', 'ui')
        with mock.patch.object(self.store, 'audit_insert', side_effect=sqlite3.OperationalError('disk full')) as insert:
            with self.assertRaisesRegex(ValueError, 'Could not write the audit record.*nothing was sent'):
                locks.kill_session(scenario.conn, 41, expect=bound_kill_expect(scenario.conn),
                                   settle_s=0, audit=trail, confirmed=True)
        insert.assert_called_once()
        self.assertFalse(trail.pending)
        self.assertIsNone(trail.id)
        # Reads really ran: an empty statement list alone could hide an early
        # validation refusal, which would not test the audit-before-send rule.
        self.assertTrue(scenario.conn.calls)
        self.assertEqual(scenario.kills(), [])
        self.assertEqual(self.store.audit_records(), [])

    def test_retention_preserves_pending_rows_at_and_inside_cutoff(self):
        from myflames import workspace_store
        cutoff = '2026-01-01T00:00:00.000000+00:00'
        # Each deletion entry point must preserve both recent and boundary
        # pending rows. Older pending rows follow the documented retention.
        for purge in ('insert', 'explicit', 'settings'):
            with self.subTest(purge=purge):
                store = WorkspaceStore(Path(self.temp.name) / (purge + '.sqlite3'))
                trails = [dbops.AuditTrail(store, 'kill', 'ui') for _ in range(3)]
                ids = [trail.attempt(['KILL %d' % i]) for i, trail in enumerate(trails, 1)]
                dates = ('2025-12-31T23:59:59.999999+00:00', cutoff,
                         '2026-01-01T00:00:00.000001+00:00')
                with contextlib.closing(sqlite3.connect(str(store.path))) as conn, conn:
                    conn.executemany('UPDATE audit SET attempted_at = ? WHERE id = ?', zip(dates, ids))
                with mock.patch.object(workspace_store, '_cutoff', return_value=cutoff):
                    if purge == 'insert':
                        dbops.record_outcome(store, 'kill', ['KILL 4'], 'ui', 'declined')
                    elif purge == 'explicit':
                        self.assertEqual(store.audit_purge(30), 1)
                    else:
                        self.assertEqual(store.set_audit_retention_days(30), 30)
                pending = store.audit_records(status='pending')
                self.assertEqual({r['id'] for r in pending}, set(ids[1:]))
                self.assertTrue(all(r['outcome_unknown'] and r['finished_at'] is None for r in pending))
                # Retention must not prevent a surviving write from completing.
                trails[2].complete('ok', outcome='killed')
                self.assertEqual([r['id'] for r in store.audit_records(status='pending')], [ids[1]])
                self.assertEqual([r['id'] for r in store.audit_records(status='ok')], [ids[2]])


if __name__ == '__main__':
    unittest.main()


class TestCLIConfirmation(unittest.TestCase):
    def confirm(self, answer='', tty=True, **kwargs):
        stream = mock.Mock()
        stream.isatty.return_value = tty
        if isinstance(answer, BaseException):
            stream.readline.side_effect = answer
        else:
            stream.readline.return_value = answer
        err = io.StringIO()
        result = dbops.cli_confirm('Target: db:3306\nSET GLOBAL x = 1;', 'Apply? [y/N] ',
                                    stdin=stream, stderr=err, **kwargs)
        return result, err.getvalue(), stream

    def test_default_no_interrupt_eof_and_affirmative_answers(self):
        for answer in ('', '\n', 'n\n', 'yes please\n', EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer):
                result, err, stream = self.confirm(answer)
                self.assertEqual(result, ('declined', 'declined', 'Nothing was sent.'))
                self.assertLess(err.index('SET GLOBAL'), err.index('Apply?'))
                stream.readline.assert_called_once_with()
        for answer in ('y\n', 'YES\r\n', ' yes \n'):
            self.assertIsNone(self.confirm(answer)[0])

    def test_non_terminal_yes_quiet_and_policy_allowed(self):
        for ask in (False, True):
            result, err, stream = self.confirm(tty=False, ask=ask)
            self.assertEqual(result[:2], ('refused', 'no_terminal'))
            self.assertIn('--yes', result[2])
            stream.readline.assert_not_called()
        for quiet in (False, True):
            result, err, stream = self.confirm(tty=False, assume_yes=True, quiet=quiet)
            self.assertIsNone(result)
            self.assertEqual(bool(err), not quiet)
            stream.readline.assert_not_called()
        result, err, stream = self.confirm(ask=False, quiet=True)
        self.assertIsNone(result)
        self.assertIn('SET GLOBAL', err)
        stream.readline.assert_not_called()

    def test_typed_guard_is_exact_and_yes_does_not_bypass_it(self):
        for answer, accepted in [('db:3306\n', True), ('db:3306\r\n', True),
                                 (' db:3306\n', False), ('db:3306 \n', False), ('DB:3306\n', False)]:
            with self.subTest(answer=answer):
                result, _, _ = self.confirm(answer, expected='db:3306')
                self.assertEqual(result is None, accepted)
        for supplied, outcome in [(None, 'no_target'), (' db:3306', 'target_mismatch'), ('db:3306', None)]:
            result, _, stream = self.confirm(tty=False, assume_yes=True, expected='db:3306', supplied=supplied)
            self.assertEqual(result[1] if result else None, outcome)
            stream.readline.assert_not_called()
        for answer in ('', EOFError(), KeyboardInterrupt()):
            self.assertEqual(self.confirm(answer, expected='db:3306', mismatch_outcome='refused')[0][:2],
                             ('declined', 'declined'))

    def test_terminal_controls_and_credentials_are_scrubbed(self):
        err = io.StringIO()
        stream = mock.Mock()
        stream.isatty.return_value = False
        problem = dbops.cli_confirm("ALTER USER x IDENTIFIED BY 'secret';\nTarget: db\x1b[2J",
            'secret\x07', stdin=stream, stderr=err, passwords=('secret',),
            no_terminal_message='secret\x1b[2J')
        text = err.getvalue() + problem[2]
        self.assertNotIn('secret', text)
        self.assertNotIn('\x1b', text)
        self.assertIn('[redacted]', text)


class TestReadysetClassificationAndRedaction(unittest.TestCase):
    """Readyset SQL verbs, inline cache hints, and upstream URL credential redaction."""

    def test_readyset_ddl_and_commands_are_risky_server_operations(self):
        for sql in (
            "ALTER READYSET SET LOG LEVEL 'info'",
            "ALTER READYSET SET EVICTION MEMORY LIMIT 1073741824",
            "ALTER READYSET ENTER MAINTENANCE MODE",
            "ALTER READYSET RESNAPSHOT TABLE `shop`.`orders`",
            "ALTER READYSET SHUTDOWN RESET",
            "CREATE SHALLOW CACHE `c_orders` WITH (ttl = 30s) FROM q_1234abcd",
            "CREATE DEEP CACHE FROM SELECT * FROM orders WHERE customer_id = 1",
            "DROP CACHE `c_orders`",
            "DROP ALL SHALLOW CACHES",
            "DROP ALL PROXIED QUERIES",
            "FLUSH CACHE `c_orders`",
            "FLUSH ALL SHALLOW CACHES",
        ):
            with self.subTest(sql=sql):
                c = dbops.classify(sql)
                self.assertEqual(c['kind'], 'risky')
                self.assertEqual(c['statements'][0]['category'], 'server')
                self.assertEqual(dbops.decide(c, 'production', read_only=True)['action'], 'refuse')

    def test_readyset_explain_and_show_stay_read_only(self):
        for sql in (
            "SHOW READYSET STATUS",
            "SHOW READYSET VERSION",
            "SHOW CACHES",
            "SHOW PROXIED QUERIES",
            "SHOW SHALLOW CACHE ALLOWED FUNCTIONS",
            "EXPLAIN ALL CACHES",
            "EXPLAIN CREATE SHALLOW CACHE FROM SELECT * FROM orders WHERE id = 1",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(dbops.classify(sql)['kind'], 'read')

    def test_readyset_inline_create_hint_is_classified_as_risky(self):
        hinted = "SELECT /*rs+ create shallow(ttl=30s) */ * FROM orders WHERE id = 1"
        c = dbops.classify(hinted)
        self.assertEqual(c['kind'], 'risky')
        self.assertEqual(c['statements'][0]['category'], 'server')
        plain_hint = "SELECT /*rs+ nocache */ * FROM orders WHERE id = 1"
        self.assertEqual(dbops.classify(plain_hint)['kind'], 'read')

    def test_url_passwords_are_redacted(self):
        raw = "Upstream failed: mysql://readyset_user:s3cr3t!pw@db-primary.internal:3306/shop?ssl-mode=DISABLED"
        clean = dbops.redact(raw)
        self.assertNotIn("s3cr3t!pw", clean)
        self.assertIn("mysql://readyset_user:[redacted]@db-primary.internal:3306/shop", clean)

    def test_client_status_probe_formats_and_validation(self):
        self.assertEqual(dbops.client_status_probe("$$", True),
                         ("SELECT '\\'$$' /* ' */ AS mf_probe", "'$$"))
        self.assertEqual(dbops.client_status_probe("//", False),
                         ("SELECT '\\''//' /* ' */ AS mf_probe", "\\'//"))
        self.assertEqual(dbops.client_status_probe("$$", True, ansi_quotes=True, client_engine="mariadb"),
                         ('SELECT \'\\\'$$\' /* \' */ AS mf_probe, 1 AS "\\""$$" /* " */', "'$$"))
        self.assertEqual(dbops.client_status_probe("$$", True, ansi_quotes=False, client_engine="mariadb"),
                         ('SELECT \'\\\'$$\' /* \' */ AS mf_probe, 1 AS "\\"$$" /* " */', "'$$"))
        # MySQL clients ignore ANSI_QUOTES; NBE makes all quotes literal.
        self.assertEqual(dbops.client_status_probe("$$", True, ansi_quotes=True, client_engine="mysql"),
                         ("SELECT '\\'$$' /* ' */ AS mf_probe", "'$$"))
        self.assertEqual(dbops.client_status_probe("$$", False, ansi_quotes=True, client_engine="mariadb"),
                         ("SELECT '\\''$$' /* ' */ AS mf_probe", "\\'$$"))
        self.assertEqual(
            dbops.client_status_probe("$$", True, ansi_quotes=False, client_engine="mariadb",
                                      server_ansi_quotes=True),
            ('SELECT \'\\\'$$\' /* \' */ AS mf_probe /*!99999 "\\"$$" */ /* " */', "'$$"))
        self.assertEqual(
            dbops.client_status_probe("$$", True, ansi_quotes=True, client_engine=None,
                                      server_ansi_quotes=True),
            ('SELECT \'\\\'$$\' /* \' */ AS mf_probe /*M!999999 "\\""$$" */ /* " */', "'$$"))
        with self.assertRaises(ValueError):
            dbops.client_status_probe("$$", True, ansi_quotes=True, client_engine="mariadb",
                                      server_ansi_quotes=False)
        for bad in ("", None, "'", '"', "\\", "a b", "/*"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dbops.client_status_probe(bad, True)
