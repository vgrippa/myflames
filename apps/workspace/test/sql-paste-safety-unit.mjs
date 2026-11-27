// Unit contracts for sqlPasteSafety.ts: the name allowlist shared with
// privileges._UNSAFE_GRANT_SQL and the mysql client command detector, which
// mirrors the client's add_line (client/mysql.cc): commands are live outside
// strings, quoted identifiers, # / "-- " line comments and plain /* */
// comments, and stay live inside /*! */ and /*+ */ comments. Socket-free;
// run directly with Node.
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
  const result = await build({ entryPoints: [path.join(here, '../src/sqlPasteSafety.ts')], bundle: true,
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
    'SELECT \'a\\\' \\! \'',             // \' escape stays in the string
    'SELECT "a\\" \\! "',                // \" escape stays in the string
    'SELECT 1 -- \\! touch /tmp/x',      // line comment
    'SELECT 1 # \\! touch /tmp/x',       // # line comment
    '/* \\! touch /tmp/x */ SELECT 1',   // plain block comment
    'SELECT \\N',                        // NULL literal, not a command
    'SELECT \\z',                        // not a command character
    'SELECT a$5',                        // $ inside an identifier
    '$$ \\! $$',                         // dollar-quoted body
    '$body$ \\! $body$',                 // tagged dollar quote
    'SELECT 1;\n-- \\!\nSELECT 2;',
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
    ['\\. /tmp/evil.sql', '\\.'],
    ['SELECT 1; --\\! x', '\\!'],                // "--" without space is not a comment
    ['-- \\!\n\\! touch /tmp/x', '\\!'],         // the comment ends at the line break
    ['SELECT 1;\r-- \\!\r\\! touch /tmp/x', '\\!'],
    ['`a\\` \\! x', '\\!'],                      // the backtick ends the identifier
    ['$$ \\! $ x $$ \\! y', '\\!'],              // $ alone is not the closing tag... $ closes: $$ \\! $ x is not one body
  ];
  for (const [sql, cmd] of flagged) {
    assert.equal(safety.findMysqlClientCommand(sql), cmd, JSON.stringify(sql));
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
