// Real handlers and Messages, without a socket. Browser tests own focus and
// event dispatch. Exemplars: editor-fixes.mjs and session-rules.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import ts from 'typescript';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
const require = createRequire(import.meta.url);
const src = fileURLToPath(new URL('../src/', import.meta.url));
const baseline = process.env.W2A_BASELINE;
function tree(file) {
  return ts.createSourceFile(file, fs.readFileSync(path.join(baseline || src, file), 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}
function find(root, predicate) {
  if (predicate(root)) return root;
  return ts.forEachChild(root, child => find(child, predicate));
}
function execute(text, name, env) {
  const js = ts.transpileModule(text, { compilerOptions: {
    target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  return new Function('require', 'exports', ...Object.keys(env), `${js}\nreturn ${name};`)(require, {}, ...Object.values(env));
}
function handler(name, env, source = 'QueryLab.tsx') {
  const file = tree(source);
  const node = find(file, n => ts.isFunctionDeclaration(n) && n.name?.text === name);
  assert.ok(node, name);
  return execute(node.getText(file), name, env);
}
const tests = [];
function test(name, body) { tests.push([name, body]); }
function harness() {
  const tab = { id: '1', name: 'Report', sql: 'SELECT 2;', database: 'app' };
  const rt = { fileBusy: false, file: { token: 'expired', name: 'report.sql', savedSql: 'SELECT 1;' } };
  const notes = [];
  return { tab, rt, notes, env: {
    tabsRef: { current: [tab] }, runtimeRef: { current: { '1': rt } },
    patchRuntime: (_, patch) => Object.assign(rt, patch),
    note: (_, text, error = false) => notes.push({ text, error }),
    changeTab: patch => Object.assign(tab, patch),
  } };
}
for (const outcome of ['saved', 'cancelled', 'failed']) test(`expired token recovery: ${outcome}`, async () => {
  const h = harness(), requests = [];
  // Named binding lets the original recursive implementation execute too.
  const env = { ...h.env, api: async (endpoint, body) => {
    requests.push({ endpoint, body });
    if (requests.length === 1) throw Object.assign(Error('Expired binding'), { code: 'unknown_token' });
    if (outcome === 'failed') throw Error('Disk is full');
    if (outcome === 'cancelled') return { cancelled: true };
    return { cancelled: false, token: 'new', name: 'copy.sql', path: '/tmp/copy.sql' };
  } };
  const save = handler('saveTab', env);
  assert.equal(await save('1'), outcome === 'saved');
  assert.deepEqual(requests, [
    { endpoint: 'editor/file/save', body: { sql: 'SELECT 2;', token: 'expired' } },
    { endpoint: 'editor/file/save', body: { sql: 'SELECT 2;', save_as: true, name: 'report.sql' } },
  ]);
  assert.equal(h.rt.fileBusy, false);
  assert.equal(h.rt.file.savedSql, outcome === 'saved' ? 'SELECT 2;' : 'SELECT 1;');
  assert.match(h.notes.at(-1).text, outcome === 'saved' ? /Saved to/ : outcome === 'failed' ? /Disk is full/ : /Save cancelled/);
});
test('Save keeps edits made while the dialog is open dirty and prevents duplicate saves', async () => {
  const h = harness();
  let finish;
  const save = handler('saveTab', { ...h.env, api: () => new Promise(resolve => { finish = resolve; }) });
  const pending = save('1');
  assert.equal(await save('1'), false);
  h.env.tabsRef.current = [{ ...h.tab, sql: 'SELECT 3;' }];
  finish({ cancelled: false, token: 'new', name: 'report.sql', path: '/tmp/report.sql' });
  assert.equal(await pending, true);
  assert.equal(h.rt.file.savedSql, 'SELECT 2;');
  assert.notEqual(h.rt.file.savedSql, h.env.tabsRef.current[0].sql);
});
test('database mismatch guards both the remembered choice and USE', async () => {
  const h = harness();
  h.rt.session = { ended: false, database_name: 'app' };
  let apiCalls = 0;
  const choose = handler('chooseDatabase', { ...h.env, tab: h.tab,
    sessionProblem: () => 'This tab is still connected to A. Close its session to run on B.',
    sessionConnectionOf: () => ({}), adoptSession: () => {},
    api: async () => { apiCalls++; return { session: null }; },
  });
  await choose('shop');
  assert.equal(h.tab.database, 'app');
  assert.equal(apiCalls, 0);
  assert.equal(h.notes.at(-1).error, true);
});
test('explicit None overrides connection and source databases; old tabs inherit', async () => {
  const h = harness();
  await handler('chooseDatabase', { ...h.env, tab: h.tab, sessionProblem: () => '' })('');
  const connectionFor = handler('connectionFor', { runtimeRef: h.env.runtimeRef, connection: { database: 'app' } });
  assert.equal(connectionFor(h.tab).database, '');
  assert.equal(connectionFor({ id: 'legacy', source: { schema: 'source' } }).database, 'app');
  assert.equal(connectionFor({ id: 'explicit', database: '', source: { schema: 'source' } }).database, '');
});
for (const fail of [false, true]) test(`Don't save keeps dirty state after ${fail ? 'failed close' : 'cancelled transaction prompt'}`, async () => {
  const h = harness();
  let closing = null;
  const closingFile = { tabId: '1', keepTab: false };
  const requestClose = handler('requestClose', { ...h.env,
    IDLE_ACTION: { phase: "idle" }, closingFile, setClosingFile: () => {}, setClosing: value => { closing = value; }, setError: () => {},
    sessionConnectionOf: () => null, connectionRef: { current: {} },
    api: async () => {
      if (fail) throw Error('Close failed');
      return { closed: false, reason: 'transaction_open', session: { in_transaction: true } };
    }, adoptSession: () => {}, writeFailure: error => ({ phase: 'failed', message: error.message }),
  });
  const file = tree('QueryLab.tsx');
  const secondary = find(file, n => ts.isObjectLiteralExpression(n) && n.properties.some(p =>
    ts.isPropertyAssignment(p) && p.name.getText(file) === 'label' && p.initializer.text === "Don't save"));
  const click = secondary.properties.find(p => p.name.getText(file) === 'onClick').initializer;
  execute(`const click = ${click.getText(file)};`, 'click', {
    closingFile, setClosingFile: () => {}, runtimeRef: h.env.runtimeRef,
    tabsRef: h.env.tabsRef, patchRuntime: h.env.patchRuntime, requestClose,
  })();
  await Promise.resolve();
  assert.equal(h.rt.file.savedSql, 'SELECT 1;');
  assert.notEqual(h.rt.file.savedSql, h.tab.sql);
  if (!fail) {
    assert.equal(closing.discardFile, true);
    const calls = [];
    handler('commitAndClose', { closing, setClosing: () => {}, commit: (_, after) => after(), requestClose: (...args) => calls.push(args) })();
    assert.deepEqual(calls, [['1', false, true]], 'Commit carries this close attempt\'s discard');
  }
});
test('None survives serialized restore and differs from the older inherited default', async () => {
  const result = await build({ stdin: { contents: fs.readFileSync(path.join(baseline || src, 'session.ts'), 'utf8'), resolveDir: src, loader: 'ts' },
    bundle: true, format: 'esm', platform: 'neutral', write: false, logLevel: 'silent' });
  const { normalizeLab, sameLab } = await import('data:text/javascript;base64,' + Buffer.from(result.outputFiles[0].text).toString('base64'));
  const tab = { id: '1', name: 'Existing query', sql: 'SELECT 1;', parameters: '{}' };
  const old = normalizeLab({ connection: { database: 'app' }, tabs: [tab], tab_id: '1' });
  const explicit = normalizeLab(JSON.parse(JSON.stringify({ ...old, tabs: [{ ...tab, database: '' }] })));
  assert.equal(explicit.tabs[0].database, '');
  assert.equal(Object.hasOwn(old.tabs[0], 'database'), false);
  assert.equal(sameLab(explicit, old), false);
  assert.equal(normalizeLab({ tabs: [{ ...tab, database: 'bad\nname' }] }).tabs[0].database, undefined);
});
const Messages = handler('Messages', { count: n => String(n), duration: () => '', StatementMessage: () => null }, 'ResultsPane.tsx');
function summary(statuses, code = null) {
  const run = { statements: statuses.map((status, index) => ({ index, status })),
    results: code === null ? [] : [{ type: 'error', code, statement: 0 }], on_error: 'continue',
    counts: { statements: statuses.length, run: 2, errors: code === null ? 0 : 1, affected_rows: 0, warnings: 0 }, duration_ms: 0 };
  return renderToStaticMarkup(React.createElement(Messages, { run, running: false }));
}
test('Continue counts actual statuses after Stop, lost connection and an incomplete terminal reply', () => {
  assert.match(summary(['ok', 'error', 'not_run', 'not_run', 'not_run'], 1146), /5 statements: 1 succeeded, 1 failed, 3 not run/);
  assert.match(summary(['stopped', 'not_run']), /2 statements: 0 succeeded, 0 failed, 1 not run, 1 stopped/);
  assert.match(summary(['session_ended', 'not_run']), /2 statements: 0 succeeded, 0 failed, 1 not run, 1 outcome unknown/);
  assert.match(summary(['running', 'pending']), /2 statements: 0 succeeded, 0 failed, 1 not run, 1 outcome unknown/);
});
for (const engine of ['MySQL', 'MariaDB']) test(`${engine}: deadlock rollback is certain; timeout setting is not supplied`, () => {
  // Engine behavior, never a claimed outcome (a script can SIGNAL either code).
  assert.match(summary(['error'], 1213), /Error 1213: an InnoDB deadlock rolls back the victim(?:'|&#x27;)s whole transaction/);
  assert.doesNotMatch(summary(['error'], 1213), /Transaction was rolled back/);
  assert.match(summary(['error'], 1205), /Error 1205: an InnoDB lock wait timeout rolls back the whole transaction only with innodb_rollback_on_timeout ON/);
  assert.doesNotMatch(summary(['error'], 1146), /rolled back/);
});
let failed = 0;
for (const [name, body] of tests) {
  try { await body(); console.log(`PASS ${name}`); }
  catch (error) { failed++; console.error(`FAIL ${name}: ${error.stack}`); }
}
console.log(`editor-files-unit: ${tests.length - failed}/${tests.length} passed`);
if (failed) process.exitCode = 1;
