"""Startup cost guards: the workspace backend must start without importing
the feature handler modules or probing MySQL Shell, must close without
waiting for the tools warmup, and the editor's rewritten regexes must match
exactly what the IGNORECASE versions matched."""
import importlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent

#: Modules that must not load when only myflames.cli and myflames.ui are
#: imported: the feature handlers load on the first request that needs them.
HEAVY_FEATURE_MODULES = ('csv_import_api', 'compare_api', 'schema_diagram_api', 'schema_versions_api',
                         'accounts_api', 'slowlog_api', 'baselines', 'editor_session', 'news',
                         'upgrade_targets')

#: The renderers and plan helpers of cli.py load on first use, so importing
#: myflames.cli alone must not load them.
CLI_DEFERRED_MODULES = ('parser', 'flamegraph', 'output_bargraph', 'output_diagram', 'output_tree',
                        'teach_hooks', 'server_info')


def _imported_myflames(code):
    env = dict(os.environ)
    temp = tempfile.mkdtemp()
    env['HOME'] = temp
    env['USERPROFILE'] = temp
    env['MYFLAMES_SECRET_BACKEND'] = 'vault'
    env.pop('MYFLAMES_VAULT_PASSPHRASE', None)
    probe = ("import sys; " + code +
             "; print('\\n'.join(sorted(m for m in sys.modules if m.startswith('myflames.'))))")
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True,
                         cwd=str(ROOT), env=env)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return [line for line in out.stdout.splitlines() if line]


class TestDeferredImports(unittest.TestCase):
    def test_ui_import_leaves_feature_modules_unloaded(self):
        loaded = _imported_myflames("import myflames.cli, myflames.ui")
        heavy = [m for m in loaded if m.rsplit('.', 1)[-1] in HEAVY_FEATURE_MODULES]
        self.assertEqual(heavy, [])

    def test_cli_import_leaves_renderers_unloaded(self):
        loaded = _imported_myflames("import myflames.cli")
        deferred = [m for m in loaded if m.rsplit('.', 1)[-1] in CLI_DEFERRED_MODULES]
        self.assertEqual(deferred, [])

    def test_cli_lazy_names_still_resolve(self):
        import myflames.cli as cli
        self.assertTrue(callable(cli.parse_explain))
        self.assertTrue(callable(cli.render_tree))
        self.assertTrue(callable(cli.folded_to_svg))
        # The wrappers are used by cli.py's own functions through module
        # globals; a deferred name must actually call through.
        self.assertEqual(cli.detect_flavor('10.11.0-MariaDB'), 'mariadb')
        with self.assertRaises(AttributeError):
            cli.not_a_real_name


class TestRouteTables(unittest.TestCase):
    def test_feature_modules_share_the_route_table(self):
        from myflames import ui_routes
        for module_name, attr, expected in ui_routes.FEATURE_ROUTES:
            with self.subTest(module=module_name, attr=attr):
                module = importlib.import_module('myflames.' + module_name)
                # Every route in the table is answered by the module's
                # response() entry point (ui.py dispatches to it).
                self.assertTrue(callable(getattr(module, 'response', None)), module_name)
                self.assertEqual(getattr(module, attr), expected)

    def test_write_paths_unchanged(self):
        # The write-path list guards server writes; it must stay exactly as in
        # the base release (content and order).
        from myflames import ui
        self.assertEqual(ui._WRITE_PATHS, (
            '/api/live/kill', '/api/live/kill/bulk', '/api/editor/run', '/api/editor/commit',
            '/api/lifecycle/start', '/api/live/replication/skip', '/api/live/replication/skip-loop/start',
            '/api/live/variable/apply', '/api/live/topology/variable/apply', '/api/accounts/apply',
            '/api/live/cluster/action', '/api/live/proxysql/action', '/api/live/readyset/action',
            '/api/import/csv/apply'))


class TestWarmupClose(unittest.TestCase):
    def test_server_close_stops_the_tool_warmup_quickly(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {'HOME': temp.name, 'USERPROFILE': temp.name,
                                      'MYFLAMES_SECRET_BACKEND': 'vault'})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MYFLAMES_VAULT_PASSPHRASE', None)
        from myflames import client_tools
        from myflames.ui import WorkspaceServer
        started = threading.Event()
        seen = {}

        def slow_discover(*args, **kwargs):
            seen.update(kwargs)
            started.set()
            # A probe honors the stop signal between candidates.
            kwargs['stop'].wait(5)
            return {}

        with patch.object(client_tools, 'discover', slow_discover):
            server = WorkspaceServer(0, store_path=os.path.join(temp.name, 'workspace.sqlite3'))
            try:
                self.assertTrue(server._tools_warmup.daemon)
                self.assertTrue(started.wait(5))
                # The warmup probes only the query tools, never mysqlsh.
                self.assertEqual(seen.get('names'), client_tools.QUERY_DISCOVERY_NAMES)
                self.assertNotIn('mysqlsh', seen.get('names'))
                t0 = time.monotonic()
                server.server_close()
                elapsed = time.monotonic() - t0
                alive_after_close = server._tools_warmup.is_alive()
            finally:
                server.server_close()
        # close() signals the probe to stop and joins it (bounded, 2 s), so the
        # probe never outlives the server (commit 6abb5f36: a leftover probe's
        # temporary folder raced test cleanup on Python 3.7), and a probe that
        # honors the signal lets close return at once.
        self.assertFalse(alive_after_close)
        self.assertLess(elapsed, 0.5)


# ── editor_session regex rewrite equivalence ──────────────────────────────
# The old pattern strings, kept verbatim from the base release. The rewritten
# patterns in myflames.editor_session must match them exactly.

_ID = r"A-Za-z0-9_$\u0080-\U0010ffff"
_BOUNDARY = (r"(?:(?<![" + _ID + r"@.])(?=[" + _ID + r"])|(?<=[" + _ID + r"])(?![" + _ID + r"]))")
_NAME = r"(?:`[0-9]+`|[" + _ID + r"]+)"

_OLD_PATTERNS = {
    '_DOLLAR_QUOTE': (r"(?<![A-Za-z0-9_$\u0080-\U0010ffff])\$[A-Za-z0-9_\u0080-\U0010ffff]*\$", 0),
    '_READ_WRITE': (_BOUNDARY + r"READ\s+WRITE" + _BOUNDARY +
                    r"|(?<![" + _ID + r"@.])(?:@@\s*)?(?:(?:SESSION|LOCAL|GLOBAL)\s*\.\s*)?"
                    r"(?:TRANSACTION_READ_ONLY|TX_READ_ONLY)" + _BOUNDARY + r"\s*(?::=|=)",
                    re.I | re.ASCII),
    '_DROP_DATABASE': (r"^\s*DROP\s+(?:DATABASE|SCHEMA)\s+(?:IF\s+EXISTS\s+)?(" + _NAME + r")\s*$", re.I),
    '_ALTER_TABLE': (r"^\s*ALTER\s+(?:(?:ONLINE|OFFLINE|IGNORE)\s+)*TABLE\s+(?:IF\s+EXISTS\s+)?(" +
                     _NAME + r")(?:\s*\.\s*(" + _NAME + r"))?(?=\s|$)", re.I),
    '_DDL_START': (r"^\s*(?:CREATE|ALTER|DROP|RENAME|TRUNCATE)" + _BOUNDARY, re.I | re.ASCII),
    '_ROUTINE_CREATE': (r"\s*" + _BOUNDARY +
                        r"(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:DEFINER\s*=\s*[^;]+?\s+)?|"
                        r"ALTER\s+(?:DEFINER\s*=\s*[^;]+?\s+)?(?=EVENT" + _BOUNDARY + r"))"
                        r"(?:(AGGREGATE)\s+)?(PROCEDURE|FUNCTION|TRIGGER|EVENT|PACKAGE)" + _BOUNDARY,
                        re.I | re.S | re.ASCII),
    '_COMPOUND_START': (r"(?:^|;|" + _BOUNDARY + r"FOR\s+)\s*(?:" + _NAME + r"\s*:\s*)?(?:"
                        r"BEGIN" + _BOUNDARY + r"(?!\s*(?:WORK" + _BOUNDARY + r")?\s*(?:;|$))|"
                        r"(?:DECLARE|IF|WHILE|LOOP|REPEAT|CASE|FOR)" + _BOUNDARY + r")",
                        re.I | re.ASCII),
    '_IMPLICIT_COMMIT': (
        r"^\s*(?:(?:CREATE|DROP)" + _BOUNDARY + r"(?!\s+(?:OR\s+REPLACE\s+)?TEMPORARY" + _BOUNDARY +
        r")|(?:ALTER|RENAME|TRUNCATE)" + _BOUNDARY + r""
        r"|(?:GRANT|REVOKE)" + _BOUNDARY + r"|SET\s+PASSWORD" + _BOUNDARY + r"|SET\s+DEFAULT\s+ROLE" +
        _BOUNDARY + r"|(?:LOCK|UNLOCK)\s+(?:TABLES?|INSTANCE)" + _BOUNDARY + r""
        r"|START\s+TRANSACTION" + _BOUNDARY + r"|BEGIN" + _BOUNDARY + r"(?!\s+NOT\s+ATOMIC)"
        r"|(?:ANALYZE|OPTIMIZE|REPAIR|CHECK)\s+(?:NO_WRITE_TO_BINLOG\s+|LOCAL\s+)?TABLES?" + _BOUNDARY + r""
        r"|(?:FLUSH|RESET)" + _BOUNDARY + r"|CACHE\s+INDEX" + _BOUNDARY + r"|LOAD\s+INDEX" + _BOUNDARY +
        r"|(?:INSTALL|UNINSTALL)\s+(?:PLUGIN|COMPONENT|SONAME)" + _BOUNDARY + r""
        r"|(?:START|STOP)\s+(?:REPLICA|SLAVE|GROUP_REPLICATION|ALL\s+SLAVES)" + _BOUNDARY +
        r"|CHANGE\s+(?:REPLICATION|MASTER)" + _BOUNDARY + r")",
        re.I | re.ASCII),
    '_ENDING_CONTROL': (r"\s*(?:COMMIT|ROLLBACK(?!\s+(?:WORK\s+)?TO" + _BOUNDARY + r"))" + _BOUNDARY,
                        re.I | re.ASCII),
}

_SAMPLES = [
    "SELECT 1", "read write", "READ WRITE", "Read Write", "xREAD WRITE", "READ WRITES", "READ  WRITE",
    "set transaction_read_only = 1", "SET TRANSACTION_READ_ONLY=0", "set @@global.tx_read_only=1",
    "set @@tx_read_only := 0", "set session.transaction_read_only = 1", "set local.transaction_read_only = 1",
    "set mytransaction_read_only = 1", "set transaction_read_onlyx = 1",
    "drop database mydb", "DROP SCHEMA `123`", "  DROP   DATABASE   IF   EXISTS   café  ",
    "drop database 中文表", "DROP DATABASE 🎉party", "drop database ßeta", "DROP SCHEMA münchen_ß",
    "DROP İF EXISTS d", "drop database İstanbul", "dropx database y",
    "alter table t add column c int", "ALTER ONLINE TABLE `1` ADD c INT", "alter ignore table db.tbl add c int",
    "alter offline table café add c int", "ALTER TABLE 中文 ADD c INT", "alterx table t", "alter tablet x",
    "CREATE TABLE t (i INT)", "create or replace view v as select 1", "createtable x", "mycreate",
    "RENAME TABLE a TO b", "truncate table t", "TRUNCATE t", "create procedure p() begin select 1; end",
    "CREATE OR REPLACE DEFINER=u@h PROCEDURE p() SELECT 1",
    "alter definer=u@localhost event e on schedule every 1 day do select 1",
    "create aggregate function f(x) returns int", "create function f() returns int",
    "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SELECT 1", "create package p as begin end",
    "CREATE OR REPLACE AGGREGATE FUNCTION f(x) RETURNS INT",
    "alter event e on schedule every 1 day do select 1",
    "begin", "BEGIN NOT ATOMIC SELECT 1; END", "BEGIN WORK", "my_label: BEGIN", "`42`: begin",
    "declare v int", "while 1 do select 1; end while", "loop select 1; end loop",
    "repeat select 1; until 1 end repeat", "case when 1 then select 1; end case",
    "if 1 then select 1; end if", "for x in 1..2 do select 1; end for", "beforebegin", "beginning",
    "commit", "COMMIT WORK", "rollback", "ROLLBACK WORK", "ROLLBACK TO SAVEPOINT sp",
    "rollback work to sp", "rollback to café", "ROLLBACK TO 🎉", "start transaction",
    "START TRANSACTION READ WRITE", "begin not atomic", "grant select on *.* to u",
    "REVOKE ALL ON *.* FROM u", "set password for u = 'x'", "SET DEFAULT ROLE r TO u",
    "lock tables t read", "UNLOCK TABLES", "lock instance for backup", "unlock instance",
    "analyze table t", "OPTIMIZE NO_WRITE_TO_BINLOG TABLE t", "repair local table t", "check table t",
    "flush tables", "RESET MASTER", "cache index t in k", "LOAD INDEX INTO CACHE t",
    "install plugin x soname 'y'", "UNINSTALL COMPONENT 'c'", "install component 'c'", "start replica",
    "STOP SLAVE", "start group_replication", "stop all slaves", "change master to master_host='h'",
    "CHANGE REPLICATION SOURCE TO source_host='h'", "ınstall plugin x soname 'y'", "ſET PASSWORD = 'x'",
    "create temporary table t (i int)", "CREATE OR REPLACE TEMPORARY TABLE t (i int)",
    "drop temporary table t", "é", "表", "🎉", "$x$", "a$b$c", "@x", "@@global.x", "x$tag$", "$tag$",
    "é$tag$", " $ $ ", "; begin", ";begin", "x; FOR ", "; for ",
    "label: FOR x in (select 1) do select 1; end for",
    "CREATE TEMPORARY TABLE t", "DROP TEMPORARY TABLE t", "create or replace temporary table t",
    "createtable", "dropping", "altered", "truncated", "renamed",
]


class TestEditorRegexEquivalence(unittest.TestCase):
    """The (?i:...) scoped-keyword rewrites and the negated identifier classes
    must match exactly what the module-level IGNORECASE patterns matched."""

    def test_patterns_match_the_old_ones(self):
        from myflames import editor_session as ed
        for name, (old_text, old_flags) in _OLD_PATTERNS.items():
            old = re.compile(old_text, old_flags)
            new = getattr(ed, name)
            for sample in _SAMPLES:
                for method in ('search', 'match'):
                    with self.subTest(pattern=name, sample=sample, method=method):
                        before = getattr(old, method)(sample)
                        after = getattr(new, method)(sample)
                        self.assertEqual(
                            None if before is None else (before.span(), before.groups()),
                            None if after is None else (after.span(), after.groups()))

    def test_identifier_classes_match_the_old_ones(self):
        # Every code point: the negated classes are exactly the old ranges.
        from myflames import editor_session as ed
        old_id = re.compile(r"[A-Za-z0-9_$\u0080-\U0010ffff]")
        old_dotat = re.compile(r"[A-Za-z0-9_$\u0080-\U0010ffff@.]")
        old_no_dollar = re.compile(r"[A-Za-z0-9_\u0080-\U0010ffff]")
        new_id = re.compile(ed._SQL_ID_CLASS)
        new_dotat = re.compile(ed._SQL_ID_DOTAT_CLASS)
        new_no_dollar = re.compile(ed._SQL_ID_NO_DOLLAR_CLASS)
        for code_point in range(0x110000):
            char = chr(code_point)
            self.assertEqual(bool(old_id.match(char)), bool(new_id.match(char)), hex(code_point))
            self.assertEqual(bool(old_dotat.match(char)), bool(new_dotat.match(char)), hex(code_point))
            self.assertEqual(bool(old_no_dollar.match(char)), bool(new_no_dollar.match(char)),
                             hex(code_point))


if __name__ == '__main__':
    unittest.main()
