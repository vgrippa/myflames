// Socket-free render contracts for the shared kit. Behavioral pointer/focus
// checks are in kit-primitives.mjs; run this one directly with Node.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
const here = path.dirname(fileURLToPath(import.meta.url));
const directory = await mkdtemp(path.join(here, '.kit-static-'));
const require = createRequire(import.meta.url);
const originalError = console.error;
const storageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
const preferences = new Map();
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
  getItem: key => preferences.get(key) ?? null,
  setItem: (key, value) => preferences.set(key, value),
} });
try {
  const result = await build({ entryPoints: [path.join(here, '../src/kit.tsx')], bundle: true, platform: 'node',
    format: 'cjs', write: false, loader: { '.css': 'empty' }, external: ['react', 'react-dom'], jsx: 'automatic' });
  const output = path.join(directory, 'kit.cjs');
  await writeFile(output, result.outputFiles[0].text);
  const kit = require(output);
  // SSR intentionally does not lay out tables. Suppress only React's known
  // useLayoutEffect notice, retaining every unexpected error.
  console.error = (...args) => {
    if (String(args[0]).includes('useLayoutEffect does nothing on the server')) return;
    originalError(...args);
  };
  const render = (name, props) => renderToStaticMarkup(React.createElement(kit[name], props));
  const fields = render('Fields', { rows: [['connection_id', 55], ['innodb_buffer_pool_size', 1024],
    [React.createElement('code', null, 'short_key'), 1], [React.createElement('code', null, 'x'.repeat(20)), 2],
    [React.createElement('code', null, 'x'.repeat(21)), 3]] });
  assert.match(fields, /<dt><code>connection_id<\/code><\/dt><dd>55/);
  assert.equal((fields.match(/class="is-long-label"/g) || []).length, 2);
  assert.match(render('SplitButton', { label: 'Run', onClick() {}, popoverLabel: 'Run options', disabled: true, children: 'Options' }), /aria-label="Run options" aria-haspopup="dialog" disabled="" aria-expanded="false"/);
  assert.equal(render('PendingBar', { count: 0, onReview() {}, onDiscard() {} }), '');
  assert.equal(render('PendingBar', { count: NaN, onReview() {}, onDiscard() {} }), '');
  assert.match(render('PendingBar', { count: 1, onReview() {}, onDiscard() {} }), /1 change pending/);
  const form = render('FormRow', { label: 'Name', htmlFor: 'name', error: 'Required', children: React.createElement('input', { id: 'name' }) });
  assert.match(form, /for="name"/);
  assert.match(form, /aria-describedby="name-error" aria-invalid="true"/);
  assert.match(form, /id="name-error"[^>]+role="alert"/);
  for (const density of ['default', 'compact']) {
    const table = render('DataTable', { label: 'Numbers', density, rows: [{ id: 'a', n: 123 }], rowKey: r => r.id,
      columns: [{ key: 'n', label: 'Count', num: true, sort: r => r.n, render: r => r.n }] });
    assert.match(table, new RegExp(`data-density="${density}"`));
    assert.match(table, /aria-label="Columns of Numbers"/);
    assert.match(table, /<\/table><\/div><div class="table-columns-cell">/, 'Columns lives outside the data scroller');
    assert.doesNotMatch(table, /class="col-menu"/, 'no phantom row-action column without actions');
    assert.match(table, /<td[^>]+data-col="n"[^>]*>123<\/td>/);
  }
  const multi = render('DataTable', { label: 'Connections', rows: [{ id: 'a' }, { id: 'b' }, { id: 'c' }], rowKey: r => r.id,
    columns: [{ key: 'name', label: 'Name', render: r => r.id }], selection: { key: 'b', select() {} },
    multiSelection: { keys: ['a', 'b'], select() {} }, onActivate() {} });
  assert.match(multi, /role="grid" aria-multiselectable="true"/);
  assert.equal((multi.match(/aria-selected="true"/g) || []).length, 2);
  assert.equal((multi.match(/aria-selected="false"/g) || []).length, 1);
  assert.equal((multi.match(/tabindex="0"/g) || []).length >= 1, true);
  assert.doesNotMatch(multi, /aria-current="true"/, 'collection selection is not navigation state');
  assert.match(render('Popover', { label: 'Actions for connection', menu: true, trigger: 'More', children: 'Use' }), /aria-haspopup="menu"/);
  const menuProps = { x: 800, y: 600, label: 'Row actions', opener: null, onClose() {} };
  assert.equal(render('ContextMenu', { ...menuProps, actions: [] }), '', 'no empty menu surface');
  assert.equal(render('ContextMenu', { ...menuProps, actions: [{ divider: true }] }), '', 'dividers alone are not a menu');
  const rowMenu = render('ContextMenu', { ...menuProps, actions: [
    { divider: true }, { label: 'Kill query…', disabled: true, hint: 'This connection is read-only', onClick() {} },
    { divider: true }, { divider: true }, { label: 'Copy cell', icon: 'copy', onClick() {} }, { divider: true },
  ] });
  assert.equal((rowMenu.match(/role="menuitem"/g) || []).length, 2);
  assert.equal((rowMenu.match(/role="separator"/g) || []).length, 1, 'no leading, trailing or repeated separators');
  assert.match(rowMenu, /aria-disabled="true" title="This connection is read-only"/);
  assert.ok(rowMenu.indexOf('Kill query…') < rowMenu.indexOf('Copy cell'), 'copy follows the custom action');
  assert.doesNotMatch(rowMenu, / disabled=""/, 'explained disabled actions remain keyboard focusable');
  const appBundle = await build({ stdin: { contents: 'export * from "./Connections"; export * from "./Home"; export { normalizeLab } from "./session";',
      resolveDir: path.join(here, '../src'), loader: 'tsx' }, bundle: true, platform: 'node', format: 'cjs', write: false,
    loader: { '.css': 'empty' }, external: ['react', 'react-dom'], jsx: 'automatic' });
  const appOutput = path.join(directory, 'home.cjs');
  await writeFile(appOutput, appBundle.outputFiles[0].text);
  const home = require(appOutput);
  const connectionFields = { name: 'Local MySQL', host: '127.0.0.1', port: 3306, user: '', database: '', ssl_mode: 'PREFERRED', ssl_ca: '', read_only: false };
  const connectionProps = { profiles: [], active: { id: '', fields: connectionFields, password: '' }, secretStore: null, busy: false,
    refresh: async () => [], onUse() {}, onActiveDeleted() {} };
  const renderConnections = props => renderToStaticMarkup(React.createElement(home.Connections, { ...connectionProps, ...props }));
  const local = renderConnections({});
  assert.match(local, /id="connections-table" tabindex="-1"/);
  assert.match(local, /aria-label="Select Local MySQL"|Select Local MySQL/);
  assert.match(local, /aria-label="Local MySQL, in use"/);
  assert.match(local, /Actions for Local MySQL/);
  assert.doesNotMatch(local, /row-confirm/);
  assert.match(local, /title="No saved password">Not saved</, 'the Password column names a connection with no saved password');
  const cleared = { id: '', fields: { ...connectionFields, host: '', name: '' }, password: '' };
  assert.doesNotMatch(renderConnections({ active: cleared }), /data-row-key="active"/);
  assert.equal(home.requestConnection(cleared).port, 0, 'an empty host must not silently open localhost');
  assert.equal(home.normalizeLab({ connection: cleared.fields }).connection.host, '', 'clearing survives session normalization');
  const profile = { ...connectionFields, id: 'saved', name: '<script>name</script>', has_saved_password: true,
    secret_backend: 'keychain', secret_backend_label: 'macOS Keychain', type: 'development', read_only: true };
  const savedRows = renderConnections({ profiles: [profile] });
  assert.match(savedRows, /&lt;script&gt;name&lt;\/script&gt;/);
  assert.match(savedRows, /class="safety-tags"><span class="type-tag is-development">Development<\/span><span class="type-tag is-read-only">Read-only<\/span><\/span>/,
    'type and Read-only are two compact pills, never one truncating string');
  assert.match(savedRows, /class="type-dot is-read-only" role="img" aria-label="Development, read-only"/);
  assert.match(savedRows, /Default database/, 'the column uses the form and vocabulary word');
  assert.match(savedRows, /title="Password saved in macOS Keychain">Saved<\/span>/);
  assert.match(savedRows, /class="cell-truncate" title="127\.0\.0\.1:3306"/);
  assert.doesNotMatch(savedRows, /<script>/);
  assert.match(savedRows, />—<span class="visually-hidden">Not used in this window<\/span>/,
    'a connection never used here shows —; profile modification dates are never used as usage history');
  assert.match(savedRows, /data-col="password"/);
  const windowDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: { innerWidth: 390 } });
  try {
    const phoneRows = renderConnections({ profiles: [profile] });
    assert.doesNotMatch(phoneRows, /data-col="(?:type|endpoint|schema|password|last-used)"/);
    assert.match(phoneRows, /class="type-dot is-read-only" role="img" aria-label="Development, read-only"/,
      'the compact name retains the type and read-only description');
    assert.match(phoneRows, /class="type-dot is-production" role="img" aria-label="Production"/);
  } finally {
    if (windowDescriptor) Object.defineProperty(globalThis, 'window', windowDescriptor);
    else delete globalThis.window;
  }
  assert.match(renderConnections({ profiles: Array.from({ length: 6 }, (_, i) => ({ ...profile, id: String(i), name: `Connection ${i}` })) }), /Search connections/);
  const recent = renderToStaticMarkup(React.createElement(home.Home, {
    busy: false, connections: 'Connections', plans: [{ id: 'plan', name: 'before.json', analysis: { plan_summary: { total_time_ms: 1 } } }],
    queryTabs: [{ id: 'custom', name: 'Orders', sql: 'SELECT 1', parameters: '{}' }], onImport() {}, onDropFile() {}, onSample() {},
    onOpenPlan() {}, onOpenQuery() {}, onOpenBundle: async () => {}, onInvestigations() {},
  }));
  assert.equal((recent.match(/<ul[^>]+aria-label="Recent work"/g) || []).length, 1);
  assert.match(recent, /data-kind="plan"/);
  assert.match(recent, /data-kind="query"/);
  assert.doesNotMatch(recent, /What you can do|Choose a connection or|home-work-grid/);
  const split = render('SplitPane', { direction: 'vertical', initialSize: Infinity, minSize: 80, maxSize: 300, storageKey: 'test', first: 'One', second: 'Two', collapsible: true });
  assert.match(split, /aria-orientation="horizontal"/);
  assert.match(split, /aria-valuenow="80"/);
  assert.match(split, /aria-label="Collapse first pane"/);
  preferences.set('myflames.split.test', '{broken');
  const paneProps = { direction: 'horizontal', initialSize: 140, minSize: 80, maxSize: 300, storageKey: 'test', first: 'One', second: 'Two', collapsible: true };
  assert.match(render('SplitPane', paneProps), /aria-valuenow="140"/);
  preferences.set('myflames.split.test', JSON.stringify({ size: 500, collapsed: false }));
  assert.match(render('SplitPane', paneProps), /aria-valuenow="300"/);
  preferences.set('myflames.split.test', JSON.stringify({ size: 140, collapsed: true }));
  assert.match(render('SplitPane', paneProps), /aria-valuetext="Collapsed"/);
  // The status bar shows known fields only: an unknown field is hidden,
  // never described ("not checked", "unknown", "Not refreshed").
  const status = render('WindowStatusBar' , { scope: 'test', fallback: { engineVersion: 'MySQL 8.4.0', transaction: 'Autocommit', refreshedAt: NaN } });
  assert.match(status, /data-status="engine"[^>]*>MySQL 8\.4\.0</);
  assert.match(status, /data-status="transaction"[^>]*>Autocommit</);
  assert.doesNotMatch(status, /data-status="(connection|session|last-run|duration|rows|refreshing|refreshed)"/, 'unknown fields are hidden');
  assert.doesNotMatch(status, /not checked|unknown|not refreshed/i);
  assert.doesNotMatch(render('WindowStatusBar', { scope: 'test' }), /data-status=/, 'nothing known, no fields');
  assert.doesNotMatch(render('WindowStatusBar', { scope: 'test', fallback: { durationMs: -1, rows: NaN, lastRunAt: NaN } }), /data-status=/,
    'invalid numbers and times are hidden');
  const full = render('WindowStatusBar', { scope: 'test', fallback: { engineVersion: 'MariaDB 11.4.3', connectionState: 'Prod (db:3306)',
    sessionState: 'Session open · connection 42', transaction: 'Transaction open', lastRunAt: Date.UTC(2026, 9, 15, 12),
    durationMs: 1234, rows: 1, refreshing: true, refreshedAt: Date.UTC(2026, 9, 15, 12, 5) } });
  for (const field of ['engine', 'connection', 'session', 'transaction', 'last-run', 'duration', 'rows', 'refreshing', 'refreshed'])
    assert.match(full, new RegExp(`data-status="${field}"`), field);
  assert.match(full, /data-status="rows"[^>]*>1 row</);
  assert.match(full, /data-status="duration"[^>]*>1.?234 ms</);
  assert.match(full, /data-status="refreshed"[^>]*>Refreshed <time dateTime="2026-10-15T12:05:00.000Z"/);
  // sortRows reads each row's value once, keeps equal values in their order,
  // and puts missing values last in both directions.
  const sortable = [{ id: 'a', v: 2 }, { id: 'b', v: null }, { id: 'c', v: 1 }, { id: 'd', v: 2 }, { id: 'e', v: null }, { id: 'f', v: 0 }];
  let reads = 0;
  const byV = (r) => { reads++; return r.v; };
  assert.deepEqual(kit.sortRows(sortable, { key: 'v', dir: 'asc' }, byV).map(r => r.id), ['f', 'c', 'a', 'd', 'b', 'e']);
  assert.equal(reads, sortable.length, 'one read per row');
  assert.deepEqual(kit.sortRows(sortable, { key: 'v', dir: 'desc' }, byV).map(r => r.id), ['a', 'd', 'c', 'f', 'b', 'e']);
  assert.equal(kit.sortRows(sortable, null, byV), sortable, 'unsorted rows are returned as they are');
  const clipBundle = await build({
    stdin: {
      contents: 'export * from "./clipboard"; export { tsv } from "./ResultsPane"; export { prettyJson } from "./resultTotals";',
      resolveDir: path.join(here, '../src'),
      loader: 'tsx',
    },
    bundle: true, platform: 'node',
    format: 'cjs', write: false, loader: { '.css': 'empty' }, external: ['react', 'react-dom'], jsx: 'automatic',
  });
  const clipOutput = path.join(directory, 'clipboard.cjs');
  await writeFile(clipOutput, clipBundle.outputFiles[0].text);
  const clip = require(clipOutput);
  // Clipboard sanitizer: keeps TAB/LF, normalizes only CRLF to LF, replaces
  // lone CR, other C0 controls, DEL, C1 (U+0080-U+009F), Unicode line/paragraph
  // separators (U+2028-U+2029) and bidi controls/marks (U+061C, U+200E, U+200F,
  // U+202A-U+202E, U+2066-U+2069) with one ASCII space each and counts every
  // replaced character.
  for (let cp = 0x00; cp <= 0xff; cp += 1) {
    const ch = String.fromCodePoint(cp);
    const res = clip.sanitizeClipboardText(`a${ch}b`);
    if (cp === 0x09 || cp === 0x0a || (cp >= 0x20 && cp <= 0x7e) || cp >= 0xa0) {
      assert.deepEqual(res, { text: `a${ch}b`, removed: 0 }, `code point 0x${cp.toString(16)} kept`);
    } else {
      assert.deepEqual(res, { text: 'a b', removed: 1 }, `code point 0x${cp.toString(16)} replaced with space`);
    }
  }
  assert.deepEqual(clip.sanitizeClipboardText('a\r\nb\rc\u2028d\u2029e\tf\ng'), { text: 'a\nb c d e\tf\ng', removed: 3 });
  assert.deepEqual(clip.sanitizeClipboardText('a\r\r\nb\r\n\rc'), { text: 'a \nb\n c', removed: 2 });
  assert.deepEqual(clip.sanitizeClipboardText('a\r\n\r\nb'), { text: 'a\n\nb', removed: 0 });
  const bidiReplaced = ['\u061c', '\u200e', '\u200f', '\u202a', '\u202b', '\u202c', '\u202d', '\u202e', '\u2066', '\u2067', '\u2068', '\u2069'];
  assert.deepEqual(
    clip.sanitizeClipboardText(`ok${bidiReplaced.join('')}end`),
    { text: `ok${' '.repeat(bidiReplaced.length)}end`, removed: bidiReplaced.length },
  );
  for (const mark of ['\u061c', '\u200e', '\u200f']) {
    assert.deepEqual(clip.sanitizeClipboardText(`x${mark}y`), { text: 'x y', removed: 1 }, `CP13 mark U+${mark.codePointAt(0).toString(16)}`);
  }
  assert.deepEqual(clip.sanitizeClipboardText('café \u200b \u202f 🔥'), { text: 'café \u200b \u202f 🔥', removed: 0 });
  assert.deepEqual(
    clip.sanitizeClipboardText('\x1b[201~DROP TABLE t;\x9b2J\u202e\x00\x07'),
    { text: ' [201~DROP TABLE t; 2J   ', removed: 5 },
  );
  // L2: a non-ASCII control after -- was not ASCII whitespace in the UTF-8
  // client. Replacing it with a space would newly hide the following text.
  const highControls = [
    ...Array.from({ length: 0x20 }, (_, i) => String.fromCodePoint(0x80 + i)),
    '\u2028', '\u2029', ...bidiReplaced,
  ];
  for (const ch of highControls) {
    const label = `L2 U+${ch.codePointAt(0).toString(16)}`;
    assert.deepEqual(clip.sanitizeClipboardText(`SELECT 1 --${ch}\\! x`),
      { text: 'SELECT 1 --_\\! x', removed: 1 }, `${label}: no new line comment`);
    assert.deepEqual(clip.sanitizeClipboardText(`SELECT 1 -- ${ch}\\! x`),
      { text: 'SELECT 1 --  \\! x', removed: 1 }, `${label}: existing line comment retained`);
    assert.deepEqual(clip.sanitizeClipboardText(`a${ch}b`), { text: 'a b', removed: 1 }, `${label}: ordinary replacement is a space`);
  }
  assert.deepEqual(clip.sanitizeClipboardText('--\u0085\u2028\\! x'), { text: '--_ \\! x', removed: 2 },
    'L2: only the replacement immediately after -- becomes an underscore');
  for (const ch of ['\x00', '\x01', '\x1b', '\r', '\x7f']) {
    assert.deepEqual(clip.sanitizeClipboardText(`--${ch}x`), { text: '-- x', removed: 1 },
      `L2: ASCII control U+${ch.codePointAt(0).toString(16)} stays a space`);
  }
  assert.deepEqual(clip.sanitizeClipboardText('--\u00a0\\! x'), { text: '--\u00a0\\! x', removed: 0 },
    'L2: non-control non-ASCII text remains intact');
  // CP1 abuse cases: comment boundaries stay intact (no new */, no new line
  // break, no "--" losing its separating space, no new "--" or "/*"), every
  // replacement counted, and sanitization is idempotent.
  const commentAbuse = [
    ['slash_star_glue', 'SELECT SLEEP(30) /* *\x01/ ; DROP DATABASE prod; -- */', 'SELECT SLEEP(30) /* * / ; DROP DATABASE prod; -- */', 1],
    ['c1_glue', 'SELECT SLEEP(30) /* *\u0085/ ; DROP DATABASE prod; -- */', 'SELECT SLEEP(30) /* * / ; DROP DATABASE prod; -- */', 1],
    ['bidi_glue', 'SELECT SLEEP(30) /* *\u202e/ ; DROP DATABASE prod; -- */', 'SELECT SLEEP(30) /* * / ; DROP DATABASE prod; -- */', 1],
    ['alm_glue', 'SELECT SLEEP(30) /* *\u061c/ ; DROP DATABASE prod; -- */', 'SELECT SLEEP(30) /* * / ; DROP DATABASE prod; -- */', 1],
    ['lrm_glue', 'SELECT SLEEP(30) /* *\u200e/ ; DROP DATABASE prod; -- */', 'SELECT SLEEP(30) /* * / ; DROP DATABASE prod; -- */', 1],
    ['rlm_glue', 'SELECT SLEEP(30) /* *\u200f/ ; DROP DATABASE prod; -- */', 'SELECT SLEEP(30) /* * / ; DROP DATABASE prod; -- */', 1],
    ['dashdash_ctrl', 'SELECT 1 --\x01; DROP DATABASE prod;', 'SELECT 1 -- ; DROP DATABASE prod;', 1],
    ['dashdash_cr', 'SELECT 1 --\r; DROP DATABASE prod;', 'SELECT 1 -- ; DROP DATABASE prod;', 1],
    ['dashdash_lrm', 'SELECT 1 --\u200e; DROP DATABASE prod;', 'SELECT 1 --_; DROP DATABASE prod;', 1],
    ['dash_glue', 'SELECT 1 -\x01- x', 'SELECT 1 - - x', 1],
    ['slash_star_open_glue', 'SELECT 1 /\x01* x', 'SELECT 1 / * x', 1],
    ['ls_in_line_comment', 'SELECT 1 -- \u2028; DROP DATABASE prod;\u2028', 'SELECT 1 --  ; DROP DATABASE prod; ', 2],
    ['ps_in_line_comment', 'SELECT 1 -- \u2029; DROP DATABASE prod;\u2029', 'SELECT 1 --  ; DROP DATABASE prod; ', 2],
    ['cr_in_line_comment', 'SELECT 1 # \r; DROP DATABASE prod;\r', 'SELECT 1 #  ; DROP DATABASE prod; ', 2],
  ];
  for (const [label, input, expected, count] of commentAbuse) {
    const res = clip.sanitizeClipboardText(input);
    assert.deepEqual(res, { text: expected, removed: count }, label);
    assert.equal(res.text.includes('\n'), false, `${label}: no line break introduced`);
    if (input.includes('/*')) {
      assert.equal(res.text.indexOf('*/'), res.text.length - 2, `${label}: no premature */ created`);
    }
    if (input.includes('--')) {
      assert.doesNotMatch(res.text, /--(?![ _\n])/, `${label}: -- preserves its whitespace or non-whitespace boundary`);
    }
    assert.deepEqual(clip.sanitizeClipboardText(res.text), { text: expected, removed: 0 }, `${label}: idempotent`);
  }
  // CP6: TSV rows with U+2028, U+2029 and lone CR keep exact row and column boundaries.
  const tsvInput = clip.tsv([
    ['1', 'alpha\u2028beta'],
    ['2', 'gamma\u2029delta'],
    ['3', 'lone\rcr'],
    ['4', 'quoted\r\nmultiline\u2028same'],
  ]);
  const tsvClean = clip.sanitizeClipboardText(tsvInput);
  assert.deepEqual(tsvClean, {
    text: '1\talpha beta\n2\tgamma delta\n3\t"lone cr"\n4\t"quoted\nmultiline same"',
    removed: 4,
  });
  const serverTsv = clip.sanitizeClipboardText('id\tnote\r\n1\ta\u2028b\r\n2\tc\u2029d\r\n');
  assert.deepEqual(serverTsv, { text: 'id\tnote\n1\ta b\n2\tc d\n', removed: 2 });
  assert.deepEqual(
    serverTsv.text.trimEnd().split('\n').map((line) => line.split('\t')),
    [['id', 'note'], ['1', 'a b'], ['2', 'c d']],
    'CP6: TSV rows stay intact without splitting on U+2028/U+2029',
  );
  // CP7: JSON strings with U+2028, U+2029, C1, DEL and bidi controls/marks stay valid JSON.
  const prettyInput = clip.prettyJson('{"id": 9007199254740993, "note": "a\u2028b\u2029c\u202ed\u0085e\u200ef\u200fg\u061ch"}');
  assert.ok(prettyInput, 'prettyJson accepts valid JSON containing U+2028/U+2029/C1/bidi');
  const prettyClean = clip.sanitizeClipboardText(prettyInput);
  assert.equal(prettyClean.removed, 7);
  assert.match(prettyClean.text, /"id": 9007199254740993/, 'CP7: BIGINT digits preserved');
  assert.equal(JSON.parse(prettyClean.text).note, 'a b c d e f g h', 'CP7: prettyJson stays valid JSON');
  const stringified = JSON.stringify({ msg: 'a\u2028b\u2029c\u202ed\u007fe\u009bf', nl: 'x\r\ny', esc: '\x1b' }, null, 2);
  const stringifiedClean = clip.sanitizeClipboardText(stringified);
  assert.equal(stringifiedClean.removed, 5);
  assert.deepEqual(JSON.parse(stringifiedClean.text), { msg: 'a b c d e f', nl: 'x\r\ny', esc: '\x1b' }, 'CP7: JSON.stringify stays valid JSON');

  assert.equal(clip.copyFeedback('Copied.', 0), 'Copied.');
  assert.equal(clip.copyFeedback('Copied.', 1), 'Copied. Removed 1 control character.');
  assert.equal(clip.copyFeedback('Copied.', 2), 'Copied. Removed 2 control characters.');
  assert.equal(clip.copyFeedback('Copied to clipboard', 2), 'Copied to clipboard. Removed 2 control characters.');
  assert.equal(clip.copyFeedback('Copied', 1), 'Copied. Removed 1 control character.');
  assert.equal(clip.copyFeedback('', 3), 'Copied. Removed 3 control characters.');
  const navigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'document');
  try {
    const written = [];
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: { clipboard: { writeText: async (value) => { written.push(value); } } },
    });
    const res1 = await clip.copyToClipboardResult('SELECT\t1\r\n-- \x1b[31mhi\u202e');
    assert.deepEqual(res1, { ok: true, text: 'SELECT\t1\n--  [31mhi ', removed: 2, note: '' });
    assert.equal(written.at(-1), 'SELECT\t1\n--  [31mhi ');
    assert.equal(await clip.copyToClipboard('ok\x00\x9b'), true);
    assert.equal(written.at(-1), 'ok  ');
    assert.equal(await kit.copyToClipboard('via\x1bkit\u202e'), true);
    assert.equal(written.at(-1), 'via kit ');
    // Every caller receives the command note with the result, even callers
    // that do not precompute copyNote. Feedback adds an existing note once.
    const commandResult = await clip.copyToClipboardResult('SELECT 1 --\u0085\\! touch /tmp/x');
    assert.equal(commandResult.text, 'SELECT 1 --_\\! touch /tmp/x');
    assert.equal(commandResult.removed, 1);
    assert.match(commandResult.note, /mysql client command \(\\!\)/);
    const universalFeedback = clip.copyFeedback('Copied query', commandResult.removed, commandResult.note);
    assert.match(universalFeedback, /^Copied query\. Removed 1 control character\./);
    assert.equal(universalFeedback.split('Note:').length - 1, 1, 'M1: unannotated caller gets one command warning');
    const annotatedFeedback = clip.copyFeedback(`Copied query.${commandResult.note}`, commandResult.removed, commandResult.note);
    assert.equal(annotatedFeedback.split('Note:').length - 1, 1, 'M1: preannotated caller never gets a duplicate note');
    assert.match(annotatedFeedback, /Removed 1 control character/);
    const namedResult = await clip.copyToClipboardResult('CREATE TABLE t (i INT);\nsource /tmp/payload.sql');
    assert.match(namedResult.note, /mysql client command/);
    assert.match(clip.copyFeedback('Copied', 0, namedResult.note), /Note:/,
      'M1: warning appears even with zero character replacements');
    // Fallback textarea path also receives sanitized text only and restores focus.
    const fallbackValues = [];
    let focusRestored = null;
    class FakeElement {
      isConnected = true;
      focus(opts) { focusRestored = opts; }
    }
    const htmlElementDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'HTMLElement');
    Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: FakeElement });
    Object.defineProperty(globalThis, 'navigator', {
      configurable: true,
      value: { clipboard: { writeText: async () => { throw new Error('denied'); } } },
    });
    Object.defineProperty(globalThis, 'document', {
      configurable: true,
      value: {
        activeElement: new FakeElement(),
        body: { appendChild(el) { fallbackValues.push(el.value); } },
        createElement(tag) {
          if (tag === 'div') throw new Error('status DOM unavailable');
          return { value: '', style: {}, setAttribute() {}, select() {}, remove() {} };
        },
        execCommand(cmd) { return cmd === 'copy'; },
      },
    });
    try {
      const res2 = await clip.copyToClipboardResult('a\r\nb\x1b[201~\x01');
      assert.deepEqual(res2, { ok: true, text: 'a\nb [201~ ', removed: 2, note: '' });
      assert.equal(fallbackValues.at(-1), 'a\nb [201~ ');
      assert.deepEqual(focusRestored, { preventScroll: true });
      const fallbackCommand = await clip.copyToClipboardResult('SELECT 1 \\G');
      assert.equal(fallbackCommand.ok, true);
      assert.match(fallbackCommand.note, /mysql client command \(\\G\)/,
        'M1: fallback path retains the shared command note');
    } finally {
      if (htmlElementDescriptor) Object.defineProperty(globalThis, 'HTMLElement', htmlElementDescriptor);
      else delete globalThis.HTMLElement;
    }
    // No clipboard and no document fails cleanly without throwing.
    delete globalThis.document;
    const res3 = await clip.copyToClipboardResult('a\x00b');
    assert.deepEqual(res3, { ok: false, text: 'a b', removed: 1, note: '' });
    assert.equal(await clip.copyToClipboard('a\x00b'), false);

    // CP10: document-level selection copy listener sanitizes selections outside
    // CodeMirror, leaves clean selections, CodeMirror and password inputs untouched.
    const elementDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'Element');
    const textareaDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'HTMLTextAreaElement');
    const inputDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'HTMLInputElement');
    const winDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'window');
    class FakeDomElement {
      constructor(cls = '') { this.cls = cls; }
      closest(sel) { return sel.split(',').some((s) => this.cls.includes(s.trim().replace(/^\./, ''))) ? this : null; }
    }
    class FakeTextArea extends FakeDomElement {
      constructor(value, start, end) { super(); this.value = value; this.selectionStart = start; this.selectionEnd = end; }
    }
    class FakeInput extends FakeDomElement {
      constructor(type, value, start, end) { super(); this.type = type; this.value = value; this.selectionStart = start; this.selectionEnd = end; }
    }
    Object.defineProperty(globalThis, 'Element', { configurable: true, value: FakeDomElement });
    Object.defineProperty(globalThis, 'HTMLTextAreaElement', { configurable: true, value: FakeTextArea });
    Object.defineProperty(globalThis, 'HTMLInputElement', { configurable: true, value: FakeInput });
    let currentSel = null;
    const listeners = new Map();
    Object.defineProperty(globalThis, 'window', { configurable: true, value: { getSelection: () => currentSel } });
    Object.defineProperty(globalThis, 'document', {
      configurable: true,
      value: {
        activeElement: null,
        addEventListener(type, fn) { listeners.set(type, [...(listeners.get(type) || []), fn]); },
        removeEventListener(type, fn) { listeners.set(type, (listeners.get(type) || []).filter((f) => f !== fn)); },
      },
    });
    const makeCopyEvent = (target, defaultPrevented = false) => {
      const data = new Map();
      const ev = {
        target,
        defaultPrevented,
        clipboardData: { setData: (type, value) => data.set(type, value) },
        preventDefault() { ev.defaultPrevented = true; },
        data,
      };
      return ev;
    };
    try {
      const cleanup = clip.installCopySanitizer();
      clip.installCopySanitizer();
      assert.equal((listeners.get('copy') || []).length, 1, 'CP10: registers exactly one copy listener');
      const onCopy = listeners.get('copy')[0];
      const probePayload = 'SELECT 1 \u001b]52;c;ZWNobw==\u0007 \u009b2J \u202eabc \u0000 \u0015 end';
      const preEl = new FakeDomElement('ops-pre');
      currentSel = { isCollapsed: false, anchorNode: { parentElement: preEl }, focusNode: { parentElement: preEl }, toString: () => probePayload };
      const ev1 = makeCopyEvent(preEl);
      assert.equal(onCopy(ev1), true);
      assert.equal(ev1.defaultPrevented, true);
      assert.equal(ev1.data.get('text/plain'), 'SELECT 1  ]52;c;ZWNobw==   2J  abc     end');
      // Clean selection passes through without calling preventDefault.
      currentSel = { isCollapsed: false, anchorNode: { parentElement: preEl }, focusNode: { parentElement: preEl }, toString: () => 'SELECT 1;\nSELECT 2;' };
      const evClean = makeCopyEvent(preEl);
      assert.equal(onCopy(evClean), false);
      assert.equal(evClean.defaultPrevented, false);
      assert.equal(evClean.data.size, 0);
      // CodeMirror-owned copy events remain untouched, so its editor handler
      // can preserve its own selection/formatting rules.
      const cmEl = new FakeDomElement('cm-editor cm-content');
      currentSel = { isCollapsed: false, anchorNode: { parentElement: cmEl }, focusNode: { parentElement: cmEl }, toString: () => probePayload };
      const evCm = makeCopyEvent(cmEl);
      assert.equal(onCopy(evCm), false);
      assert.equal(evCm.defaultPrevented, false);
      assert.equal(evCm.data.size, 0);
      // A document selection spanning into or out of the editor is still a
      // manual page selection. Neither endpoint may disable sanitization.
      for (const [anchor, focus] of [[preEl, cmEl], [cmEl, preEl]]) {
        currentSel = { isCollapsed: false, anchorNode: { parentElement: anchor }, focusNode: { parentElement: focus }, toString: () => probePayload };
        for (const target of [preEl, cmEl]) {
          const evMixed = makeCopyEvent(target);
          assert.equal(onCopy(evMixed), true, 'L1: cross-editor page selection is sanitized even when its target is CodeMirror');
          assert.equal(evMixed.defaultPrevented, true);
          assert.equal(evMixed.data.get('text/plain'), ev1.data.get('text/plain'));
        }
        const evMixedPrevented = makeCopyEvent(cmEl, true);
        assert.equal(onCopy(evMixedPrevented), false, 'L1: an editor-owned prevented event remains untouched');
        assert.equal(evMixedPrevented.data.size, 0);
      }
      // Already prevented events are left untouched.
      currentSel = { isCollapsed: false, anchorNode: { parentElement: preEl }, focusNode: { parentElement: preEl }, toString: () => probePayload };
      const evPrevented = makeCopyEvent(preEl, true);
      assert.equal(onCopy(evPrevented), false);
      assert.equal(evPrevented.data.size, 0);
      // Non-password input/textarea selections are sanitized; password inputs are never read.
      currentSel = { isCollapsed: true, toString: () => '' };
      const ta = new FakeTextArea('abc\x1bdef', 1, 6);
      const evTa = makeCopyEvent(ta);
      assert.equal(onCopy(evTa), true);
      assert.equal(evTa.defaultPrevented, true);
      assert.equal(evTa.data.get('text/plain'), 'bc de');
      const pw = new FakeInput('password', 'sec\x1bret', 0, 7);
      const evPw = makeCopyEvent(pw);
      assert.equal(onCopy(evPw), false);
      assert.equal(evPw.defaultPrevented, false);
      assert.equal(evPw.data.size, 0);
      cleanup();
      assert.equal((listeners.get('copy') || []).length, 0, 'CP10: cleanup removes the listener');
    } finally {
      if (elementDescriptor) Object.defineProperty(globalThis, 'Element', elementDescriptor);
      else delete globalThis.Element;
      if (textareaDescriptor) Object.defineProperty(globalThis, 'HTMLTextAreaElement', textareaDescriptor);
      else delete globalThis.HTMLTextAreaElement;
      if (inputDescriptor) Object.defineProperty(globalThis, 'HTMLInputElement', inputDescriptor);
      else delete globalThis.HTMLInputElement;
      if (winDescriptor) Object.defineProperty(globalThis, 'window', winDescriptor);
      else delete globalThis.window;
    }

    // CP5: clipboard chunk import failure returns null/false with zero unhandled rejections.
    const missingClip = path.join(directory, 'missing-clipboard-chunk.cjs');
    const brokenKitBundle = await build({
      entryPoints: [path.join(here, '../src/kit.tsx')],
      bundle: true, platform: 'node', format: 'cjs', write: false,
      loader: { '.css': 'empty' }, external: ['react', 'react-dom'], jsx: 'automatic',
      plugins: [{
        name: 'fail-clipboard-import',
        setup(b) {
          b.onResolve({ filter: /^\.\/clipboard$/ }, () => ({ path: missingClip, external: true }));
        },
      }],
    });
    const brokenKitOutput = path.join(directory, 'kit-broken-clip.cjs');
    await writeFile(brokenKitOutput, brokenKitBundle.outputFiles[0].text);
    const brokenKit = require(brokenKitOutput);
    const unhandled = [];
    const onUnhandled = (reason) => unhandled.push(reason);
    process.on('unhandledRejection', onUnhandled);
    try {
      // Preload pattern (fire-and-forget) produces no unhandled rejection.
      void brokenKit.loadClip();
      await new Promise((r) => setImmediate(r));
      assert.equal(await brokenKit.loadClip(), null, 'CP5: failed clipboard chunk import resolves loadClip to null');
      assert.equal(await brokenKit.copyToClipboard('SELECT 1'), false, 'CP5: copyToClipboard returns false on chunk failure');
      await new Promise((r) => setImmediate(r));
      assert.deepEqual(unhandled, [], 'CP5: no unhandled rejections when clipboard chunk fails to load');
      // L1: a failed startup preload must not permanently skip listener
      // installation. Once the import can succeed, loadClip owns installing it.
      assert.equal((listeners.get('copy') || []).length, 0, 'L1: failed imports installed no listener');
      await writeFile(missingClip, clipBundle.outputFiles[0].text);
      const recoveredClip = await brokenKit.loadClip();
      assert.ok(recoveredClip, 'L1: later successful import recovers after preload failure');
      try {
        assert.equal((listeners.get('copy') || []).length, 1, 'L1: first successful load installs the listener');
        await Promise.all([brokenKit.loadClip(), brokenKit.loadClip(), brokenKit.loadClip()]);
        assert.equal((listeners.get('copy') || []).length, 1, 'L1: repeated successful loads keep one listener');
      } finally {
        recoveredClip.installCopySanitizer()();
      }
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }

    // M1: shared command warnings reach callers of both clipboard helpers.
    // This small DOM records visible status content, and fake timers keep the
    // status lifetime deterministic without leaving any real timers running.
    const statusDescriptors = new Map(['document', 'navigator', 'window', 'HTMLElement', 'HTMLTextAreaElement', 'setTimeout', 'clearTimeout']
      .map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
    const statusNodes = new Set();
    const statusTimers = new Map();
    let nextTimer = 0;
    let selectionText = '';
    let appendedNotices = 0;
    class StatusElement {
      constructor(tag) { this.tagName = tag; this.style = {}; this.attributes = new Map(); this.textContent = ''; }
      setAttribute(key, value) { this.attributes.set(key, value); }
      select() { this.selectionStart = 0; this.selectionEnd = this.value?.length ?? 0; }
      remove() { statusNodes.delete(this); }
    }
    const statusDocument = {
      activeElement: null,
      body: { appendChild(node) { statusNodes.add(node); if (node.id === 'myflames-copy-notice') appendedNotices++; } },
      createElement(tag) { return new StatusElement(tag); },
      addEventListener() {},
      removeEventListener() {},
      execCommand() { return false; },
    };
    const successfulNavigator = { clipboard: { writeText: async () => {} } };
    const defineStatusGlobal = (key, value) => Object.defineProperty(globalThis, key, { configurable: true, value });
    const visibleNotices = () => [...statusNodes].filter((node) => node.id === 'myflames-copy-notice');
    const expireNotices = () => {
      for (const [id, { callback }] of statusTimers) {
        statusTimers.delete(id);
        callback();
      }
    };
    defineStatusGlobal('document', statusDocument);
    defineStatusGlobal('navigator', successfulNavigator);
    defineStatusGlobal('HTMLElement', StatusElement);
    defineStatusGlobal('HTMLTextAreaElement', StatusElement);
    defineStatusGlobal('window', { getSelection: () => ({
      isCollapsed: false, anchorNode: null, focusNode: null, toString: () => selectionText,
    }) });
    defineStatusGlobal('setTimeout', (callback, delay) => {
      const id = ++nextTimer;
      statusTimers.set(id, { callback, delay });
      return id;
    });
    defineStatusGlobal('clearTimeout', (id) => statusTimers.delete(id));
    try {
      assert.equal((await clip.copyToClipboardResult('SELECT 1')).ok, true);
      assert.equal(visibleNotices().length, 0, 'M1: ordinary successful copies create no command warning');
      const direct = await clip.copyToClipboardResult('SELECT 1 --\u0085\\! touch /tmp/x');
      assert.equal(direct.ok, true);
      assert.equal(visibleNotices().length, 1, 'M1: direct result helper displays the command warning');
      const directNotice = visibleNotices()[0];
      assert.equal(directNotice.attributes.get('role'), 'status');
      assert.equal(directNotice.attributes.get('aria-live'), 'polite');
      assert.equal(directNotice.attributes.get('aria-atomic'), 'true');
      assert.match(directNotice.textContent, /^Copied\. Removed 1 control character\./);
      assert.match(directNotice.textContent, /mysql client command \(\\!\)/);
      assert.equal(directNotice.textContent.split('Note:').length - 1, 1);
      const legacyLocal = clip.copyFeedback(`Copied SQL.${direct.note}`, direct.removed);
      assert.doesNotMatch(legacyLocal, /Note:/, 'M1: legacy caller feedback leaves the warning in the shared status');
      assert.equal((legacyLocal + directNotice.textContent).split('Note:').length - 1, 1,
        'M1: caller feedback plus shared status displays exactly one warning');

      assert.equal(await clip.copyToClipboard('source /tmp/payload.sql'), true);
      assert.equal(visibleNotices().length, 1, 'M1: boolean helper replaces the previous status');
      assert.match(visibleNotices()[0].textContent, /mysql client command \(source\)/);
      assert.equal(statusTimers.size, 1, 'M1: replacing a warning cancels its previous timer');
      assert.equal([...statusTimers.values()][0].delay, 12000, 'M1: warning allows twelve seconds of reading time');
      assert.equal(await clip.copyToClipboard('SELECT 2'), true);
      assert.equal(visibleNotices().length, 0, 'M1: a clean copy dismisses the previous command warning');
      assert.equal(statusTimers.size, 0, 'M1: dismissing the warning cancels its timer');
      assert.equal(await clip.copyToClipboard('SELECT 1 \\G'), true);
      assert.equal(visibleNotices().length, 1);
      expireNotices();
      assert.equal(visibleNotices().length, 0, 'M1: warning expires');

      // The kit wrapper shares the same warning contract without requiring
      // its caller to consume CopyResult or construct a note.
      assert.equal(await kit.copyToClipboard('SELECT 1 \\G'), true);
      assert.equal(visibleNotices().length, 1, 'M1: kit boolean helper displays the command warning');
      assert.match(visibleNotices()[0].textContent, /mysql client command \(\\G\)/);
      expireNotices();
      assert.equal(await clip.copyToClipboard('SELECT 1 \\G'), true);
      assert.equal(visibleNotices().length, 1);
      defineStatusGlobal('navigator', { clipboard: { writeText: async () => { throw new Error('denied'); } } });
      const failed = await clip.copyToClipboardResult('SELECT 1 \\G');
      assert.equal(failed.ok, false);
      assert.equal(visibleNotices().length, 0, 'M1: a failed copy never claims a command was copied');
      assert.equal(statusTimers.size, 0, 'M1: a failed copy clears the previous warning and its timer');
      defineStatusGlobal('navigator', successfulNavigator);

      // Selection copies also report the count when sanitization changes
      // their text, using the same single status surface.
      selectionText = 'customer\u2028name';
      const selectionData = new Map();
      const selectionEvent = {
        target: null, defaultPrevented: false,
        clipboardData: { setData(type, value) { selectionData.set(type, value); } },
        preventDefault() { this.defaultPrevented = true; },
      };
      assert.equal(clip.handleSelectionCopy(selectionEvent), true);
      assert.equal(selectionData.get('text/plain'), 'customer name');
      assert.equal(visibleNotices().length, 1);
      assert.equal(visibleNotices()[0].textContent, 'Copied. Removed 1 control character.');
      selectionText = 'customer name';
      const cleanSelectionEvent = { ...selectionEvent, defaultPrevented: false };
      assert.equal(clip.handleSelectionCopy(cleanSelectionEvent), false);
      assert.equal(visibleNotices().length, 0, 'M1: a clean selection copy clears the previous replacement notice');
      assert.equal(statusTimers.size, 0);

      // execCommand emits a copy event synchronously. The already-sanitized
      // fallback textarea must bypass the selection handler until transport
      // success is known, or it could double-report or report a failed copy.
      defineStatusGlobal('navigator', { clipboard: { writeText: async () => { throw new Error('denied'); } } });
      for (const fallbackSucceeds of [true, false]) {
        const noticesBeforeFallback = appendedNotices;
        statusDocument.execCommand = () => {
          const area = [...statusNodes].find((node) => node.tagName === 'textarea');
          assert.ok(area, 'M1: fallback textarea is present while execCommand runs');
          const fallbackData = new Map();
          const fallbackEvent = {
            target: area, defaultPrevented: false,
            clipboardData: { setData(type, value) { fallbackData.set(type, value); } },
            preventDefault() { this.defaultPrevented = true; },
          };
          assert.equal(clip.handleSelectionCopy(fallbackEvent), false, 'M1: fallback copy event is not intercepted');
          assert.equal(fallbackEvent.defaultPrevented, false);
          assert.equal(fallbackData.size, 0);
          assert.equal(visibleNotices().length, 0, 'M1: fallback event never reports before execCommand completes');
          assert.equal(statusTimers.size, 0);
          return fallbackSucceeds;
        };
        const fallback = await clip.copyToClipboardResult('SELECT 1\u0085 \\G');
        assert.equal(fallback.ok, fallbackSucceeds);
        assert.equal(fallback.removed, 1);
        assert.equal(visibleNotices().length, fallbackSucceeds ? 1 : 0);
        assert.equal(appendedNotices - noticesBeforeFallback, fallbackSucceeds ? 1 : 0,
          'M1: fallback reports exactly once after success and never after failure');
        if (fallbackSucceeds) assert.match(visibleNotices()[0].textContent, /Removed 1 control character/);
        expireNotices();
      }
      statusDocument.execCommand = () => false;
      defineStatusGlobal('navigator', successfulNavigator);
      expireNotices();
      assert.equal(visibleNotices().length, 0);
    } finally {
      expireNotices();
      for (const [key, descriptor] of statusDescriptors) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
    }
  } finally {
    if (navigatorDescriptor) Object.defineProperty(globalThis, 'navigator', navigatorDescriptor);
    else delete globalThis.navigator;
    if (documentDescriptor) Object.defineProperty(globalThis, 'document', documentDescriptor);
    else delete globalThis.document;
  }
  console.log('Kit static render contracts passed.');
} finally {
  console.error = originalError;
  if (storageDescriptor) Object.defineProperty(globalThis, 'localStorage', storageDescriptor);
  else delete globalThis.localStorage;
  await rm(directory, { recursive: true, force: true });
}
