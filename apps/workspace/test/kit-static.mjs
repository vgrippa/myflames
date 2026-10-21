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
  assert.match(savedRows, /Development · Read-only/);
  assert.match(savedRows, /class="type-dot is-read-only" role="img" aria-label="Development, read-only"/);
  assert.match(savedRows, /title="Development · Read-only"/);
  assert.match(savedRows, /title="Password saved in macOS Keychain">Saved<\/span>/);
  assert.match(savedRows, /class="cell-truncate" title="127\.0\.0\.1:3306"/);
  assert.doesNotMatch(savedRows, /<script>/);
  assert.match(savedRows, /Not recorded/, 'profile modification dates are never used as usage history');
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
  assert.match(status, /data-status="engine">MySQL 8\.4\.0</);
  assert.match(status, /data-status="transaction">Autocommit</);
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
  assert.match(full, /data-status="rows">1 row</);
  assert.match(full, /data-status="duration">1.?234 ms</);
  assert.match(full, /data-status="refreshed">Refreshed <time dateTime="2026-10-15T12:05:00.000Z"/);
  // sortRows reads each row's value once, keeps equal values in their order,
  // and puts missing values last in both directions.
  const sortable = [{ id: 'a', v: 2 }, { id: 'b', v: null }, { id: 'c', v: 1 }, { id: 'd', v: 2 }, { id: 'e', v: null }, { id: 'f', v: 0 }];
  let reads = 0;
  const byV = (r) => { reads++; return r.v; };
  assert.deepEqual(kit.sortRows(sortable, { key: 'v', dir: 'asc' }, byV).map(r => r.id), ['f', 'c', 'a', 'd', 'b', 'e']);
  assert.equal(reads, sortable.length, 'one read per row');
  assert.deepEqual(kit.sortRows(sortable, { key: 'v', dir: 'desc' }, byV).map(r => r.id), ['a', 'd', 'c', 'f', 'b', 'e']);
  assert.equal(kit.sortRows(sortable, null, byV), sortable, 'unsorted rows are returned as they are');
  console.log('Kit static render contracts passed.');
} finally {
  console.error = originalError;
  if (storageDescriptor) Object.defineProperty(globalThis, 'localStorage', storageDescriptor);
  else delete globalThis.localStorage;
  await rm(directory, { recursive: true, force: true });
}
