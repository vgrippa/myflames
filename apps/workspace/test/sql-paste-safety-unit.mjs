// Unit contracts for sqlPasteSafety.ts: the name allowlist shared with
// privileges._UNSAFE_GRANT_SQL and the mysql client command detector, which
// conservatively covers the client's add_line (client/mysql.cc), including
// both client families, backslash-escape modes, optional MySQL dollar quotes
// and client-side parsing in server comments. Destination session modes are
// deliberately not assumed. Socket-free; run directly with Node.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const here = path.dirname(fileURLToPath(import.meta.url));
const directory = await mkdtemp(path.join(here, '.sql-paste-safety-'));
const require = createRequire(import.meta.url);
try {
  const result = await build({ stdin: { contents: 'export * from "./sqlPasteSafety"; export { sanitizeClipboardText } from "./clipboard";',
    resolveDir: path.join(here, '../src'), loader: 'ts' }, bundle: true,
    platform: 'node', format: 'cjs', write: false });
  const output = path.join(directory, 'sqlPasteSafety.cjs');
  await writeFile(output, result.outputFiles[0].text);
  const safety = require(output);

  // Name allowlist: printable ASCII (0x20-0x7E) without backslash.
  for (const ok of ['orders', 'my table', '', '`quoted`', 'a.b', '~tilde', ' space ', '!@#$%^&*()']) {
    assert.equal(safety.isPasteSafeName(ok), true, JSON.stringify(ok));
  }
  for (const bad of ['café', 'a\\b', 'tab\tname', 'x\x80y', 'del\x7f', 'line\nbreak', '\x1b[2J', 'é', '‮']) {
    assert.equal(safety.isPasteSafeName(bad), false, JSON.stringify(bad));
  }
  assert.equal(safety.unsafeNamesReason(['schema', 'table']), null);
  assert.equal(safety.unsafeNamesReason(['']), null);
  const reason = safety.unsafeNamesReason(['ok', 'bad\x85name']);
  assert.match(reason, /not printable ASCII or contains a backslash/);
  assert.match(reason, /\\!/);
  assert.match(safety.unsafeNamesReason(['back\\slash']), /backslash/);

  // Detector: no client command in ordinary SQL.
  for (const safe of [
    'SELECT * FROM `orders` LIMIT 100;',
    'SELECT 1',
    'SELECT \'\\!\'',                    // single-quoted string
    'SELECT "\\!"',                      // double-quoted string
    'SELECT `\\!`',                      // backtick-quoted identifier
    'SELECT \'it\'\'s \\!\'',            // '' escape stays in the string
    'SELECT 1 -- \\! touch /tmp/x',      // line comment
    'SELECT 1 # \\! touch /tmp/x',       // # line comment
    '/* \\! touch /tmp/x */ SELECT 1',   // plain block comment
    'SELECT \\N',                        // NULL literal, not a command
    'SELECT \\z',                        // not a command character
    'SELECT a$5',                        // $ inside an identifier
    'SELECT 1;\n-- \\!\nSELECT 2;',
    "SELECT 'source /tmp/evil.sql'",      // named command inside a string
    '/* system touch /tmp/x */ SELECT 1',
    'SELECT source_column FROM t',        // no partial-word matches
  ]) {
    assert.equal(safety.findMysqlClientCommand(safe), null, JSON.stringify(safe));
  }

  // Detector: commands the mysql client would run.
  const flagged = [
    ['/*!99999 \\! touch /tmp/x # */', '\\!'],   // ledger payload: executable comment
    ['/*! \\! */ SELECT 1', '\\!'],
    ['/*+ \\! */ SELECT 1', '\\!'],              // optimizer hint comment
    ['SELECT 1 \\G', '\\G'],
    ['SELECT 1 \\g', '\\g'],
    ['SELECT /**/ \\! touch /tmp/x', '\\!'],      // empty plain comment still closes
    ["SELECT \\' \\! touch /tmp/x", '\\!'],    // unknown shortcut consumes the quote
    ['\\. /tmp/evil.sql', '\\.'],
    ['SELECT 1; --\\! x', '\\!'],                // "--" without space is not a comment
    ['-- \\!\n\\! touch /tmp/x', '\\!'],         // the comment ends at the line break
    ['SELECT 1;\r-- \\!\r\\! touch /tmp/x', '\\!'],
    ['`a\\` \\! x', '\\!'],                      // the backtick ends the identifier
    ['$$ \\! $ x $$ \\! y', '\\!'],              // $ alone is not the closing tag... $ closes: $$ \\! $ x is not one body
    ['$$ \\! $$', '\\!'],                        // destination may not support dollar quoting
    ['$body$ \\! $body$', '\\!'],
    ['SELECT a$$$ \\! x', '\\!'],                 // dollars within an identifier are not a quote
    ["SELECT 'a\\' \\! '", '\\!'],              // NO_BACKSLASH_ESCAPES closes the quote
    ['SELECT "a\\" \\! "', '\\!'],              // also consider ANSI_QUOTES / no backslash escaping
    [String.raw`SELECT 'a\' "' "b\" \! touch MARK #`, '\\!'], // MariaDB ANSI_QUOTES changes double quotes independently
    ['SELECT /*+ # \\! touch /tmp/x # */ 1', '\\!'],
    ["SELECT /*+ ' \\! touch /tmp/x # */ 1", '\\!'],
    ['SELECT /*+ -- \\! touch /tmp/x # */ 1', '\\!'],
    ['SELECT /*+ /* \\! touch /tmp/x # */ 1', '\\!'],
    ['SELECT /*M!999999 \\! touch /tmp/x # */ 1', '\\!'],
    ['SELECT /*!99999 --\u00a0\\! touch /tmp/x # */ 1', '\\!'],
    ['SELECT /*!99999 --\ufeff\\! touch /tmp/x # */ 1', '\\!'],
    ["SELECT 'line one\nsystem touch /tmp/x\n';", 'system'], // over-warn about a physical line's first word
    ['/*\nsource /tmp/payload.sql\n*/ SELECT 1', 'source'],
  ];
  for (const [sql, cmd] of flagged) {
    assert.equal(safety.findMysqlClientCommand(sql), cmd, JSON.stringify(sql));
  }

  // Security round 2 scanner_cases.mjs: retain every executable probe variant,
  // including cases that can run only with a different client/session mode.
  // Check the sanitized bytes because those are what clipboard consumers see.
  const executableProbes = [
    ['c01_conditional_baseline', 'SELECT SLEEP(0) /*!99999 \\! touch MARK # */;\n'],
    ['c02_hint_hash', 'SELECT SLEEP(0) /*+ # */ /*!99999 \\! touch MARK # */;\n'],
    ['c03_hint_quote', "SELECT SLEEP(0) /*+ ' */ /*!99999 \\! touch MARK # */ -- '\n;\n"],
    ['c04_hint_dashdash', 'SELECT SLEEP(0) /*+ -- */ /*!99999 \\! touch MARK # */;\n'],
    ['c05_hint_block', 'SELECT SLEEP(0) /*+ /* \\! touch MARK # */;\n'],
    ['c06_mariadb_M', 'SELECT SLEEP(0) /*M!999999 \\! touch MARK # */;\n'],
    ['c07_dash_nbsp', 'SELECT SLEEP(0) /*!99999 --\u00a0\\! touch MARK # */;\n'],
    ['c08_dash_bom', 'SELECT SLEEP(0) /*!99999 --\ufeff\\! touch MARK # */;\n'],
    ['c09_dollar_unsupported / c09b_dollar_supported', 'SELECT SLEEP(0) /*!99999 $$ \\! touch MARK # $$ */;\n'],
    ['c10_nbe_session / c10b_nbe_default', "SELECT 'a\\' \\! touch MARK # ';\n"],
    ['c11_named_system', 'SELECT SLEEP(0) /*!99999 ;\nsystem touch MARK\n*/;\n'],
    ['c12_delimiter_quote', 'SELECT SLEEP(0) /*!99999 ;\nDELIMITER "\'"\nSELECT 1\' \\! touch MARK #\n*/\n'],
    ['c14_backtick_backslash', 'SELECT `a\\` \\! touch MARK # `;\n'],
    ['c15_dash_c1_space', 'SELECT SLEEP(0) /*!99999 --\u0085\\! touch MARK # */;\n'],
    ['c19_crlf_after_comment', 'SELECT 1 # x\r\n\\! touch MARK #\n;\n'],
  ];
  for (const [id, sql] of executableProbes) {
    const clean = safety.sanitizeClipboardText(sql).text;
    assert.ok(safety.findMysqlClientCommand(clean), `${id}: command flagged after sanitization`);
    assert.match(safety.clientCommandNote(clean), /mysql client command/, `${id}: warning produced`);
  }

  // Security R3-1: a union of independently parsed MySQL and MariaDB modes.
  // Combining their comment rules can hide executable text in a quote even
  // though one real client considers it outside that quote. Check both raw
  // input and the sanitized clipboard bytes, retaining the probe identifiers.
  const round3Probes = [
    ['r01_hint_eol_named', 'SELECT SLEEP(0) /*+\n; system touch MARK;\n*/;\n'],
    ['r02_hint_nested_cond', 'SELECT SLEEP(0) /*+ /*! ; system touch MARK; */;\n'],
    ['r03_dollar_quote_named', "SELECT $$'$$; system touch MARK;\n"],
    ['r04_dollar_quote_bs', "SELECT $$'$$ \\! touch MARK # '\n;\n"],
    ['r05_mysqlM_quote_named', "SELECT 1 /*M! ' */; system touch MARK;\n"],
    ['r06_mysqlM_quote_bs', "SELECT 1 /*M! ' */ \\! touch MARK # '\n;\n"],
    // Both r08 and r09 executed (RAN) with the mariadb:11.4 client, 11.4.13,
    // against round3/fake_mysqld.py. The comment must not open a SQL quote.
    ['r08_maria_dashdash_named', "--x '\nSELECT 1; system touch MARK;\n-- '\n"],
    ['r09_maria_dashdash_bs', "SELECT 1;--x '\n\\! touch MARK # '\n;\n"],
    ['r10_hint_eol_quote', "SELECT SLEEP(0) /*+\n' */ ' ; system touch MARK;\n"],
    ['r11_hint_nested_cond_quote', "SELECT SLEEP(0) /*+ /*! ' */ ' ; system touch MARK;\n"],
    ['r12_account_breakout', "DROP USER ''@'\\'' /*M! \" */; system touch MARK; #';\n"],
  ];
  for (const [id, sql] of round3Probes) {
    for (const [kind, text] of [['raw', sql], ['sanitized', safety.sanitizeClipboardText(sql).text]]) {
      assert.ok(safety.findMysqlClientCommand(text), `${id}: command flagged (${kind})`);
      assert.match(safety.clientCommandNote(text), /mysql client command/, `${id}: warning produced (${kind})`);
    }
  }
  assert.equal(safety.findMysqlClientCommand('SELECT SLEEP(0) /*+ ; system touch MARK; */;\n'), null,
    'r02b_hint_single_line: a semicolon alone does not end an optimizer hint');

  // Exercise the mode combinations and boundaries, rather than merely
  // treating every dollar or newline as an unconditional quote terminator.
  for (const [id, sql] of [
    ['dollar_named_tag', "SELECT $body_9$'$body_9$; system touch MARK;\n"],
    ['dollar_numeric_tag', "SELECT $9$'$9$; system touch MARK;\n"],
    ['dollar_exact_tag', "SELECT $body$'$other$$body$; system touch MARK;\n"],
    ['dollar_no_backslash_escapes', "SELECT $$'\\$$; system touch MARK;\n"],
    ['mariadb_hint_survives_newline', "SELECT /*+\n# ' */; system touch MARK;\n"],
    ['hint_overlapping_close', "SELECT /*+ /*/ ' */ ' ; system touch MARK;\n"],
  ]) {
    assert.equal(safety.findMysqlClientCommand(sql), 'system', id);
  }
  for (const [id, sql] of [
    ['dollar_inside_identifier', "SELECT a$body$'; system touch MARK;\n"],
    ['mariadb_dashdash_buffered', "SELECT 1\n--x '\n; system touch MARK;\n"],
  ]) {
    assert.equal(safety.findMysqlClientCommand(sql), null, id);
  }

  for (const [id, sql] of [
    ['c13_plain_comment', 'SELECT SLEEP(0) /* \\! touch MARK # */;\n'],
    ['c16_trailing_backslash', 'SELECT 1 \\\n! touch MARK\n;\n'],
    ['c17_ls_in_line_comment', 'SELECT 1 -- x\u2028\\! touch MARK #\n;\n'],
    ['c18_cr_in_hash_comment', 'SELECT 1 # x\r\\! touch MARK #\n;\n'],
  ]) {
    assert.equal(safety.findMysqlClientCommand(safety.sanitizeClipboardText(sql).text), null, id);
  }
  // Long-form commands are case insensitive and can begin a line or follow
  // a statement boundary. DELIMITER itself warns before it can alter parsing.
  for (const command of ['source /tmp/payload.sql', 'system touch /tmp/x', 'DELIMITER "\'"', 'use database_name',
    'connect database_name', 'pager less', 'tee /tmp/out', 'charset utf8mb4', 'resetconnection']) {
    for (const prefix of ['', '\t', 'SELECT 1;\n  ', 'SELECT 1; ']) {
      assert.ok(safety.findMysqlClientCommand(prefix + command), JSON.stringify(prefix + command));
    }
  }

  // Feedback helpers.
  assert.equal(safety.clientCommandNote('SELECT 1'), '');
  assert.equal(safety.copyNote('Copied SELECT query', 'SELECT 1'), 'Copied SELECT query');
  const note = safety.clientCommandNote('/*!99999 \\! touch /tmp/x # */');
  assert.match(note, /mysql client command \(\\!\)/);
  assert.match(note, /\/\*! \*\/ comments/);
  const msg = safety.copyNote('Copied SELECT query', 'SELECT 1 \\G');
  assert.match(msg, /^Copied SELECT query\. Note: /);
  assert.match(msg, /\(\\G\)/);

  console.log('sqlPasteSafety unit contracts passed.');
} finally {
  await rm(directory, { recursive: true, force: true });
}
