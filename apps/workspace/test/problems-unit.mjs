// Real EditorState and installed lint scheduler; no DOM, network or database.
// Run from apps/workspace: node test/problems-unit.mjs
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const root = fileURLToPath(new URL('..', import.meta.url));
const built = await build({ stdin: { resolveDir: root, contents: `
  export * from './src/editor/problems'; export * from './src/editor/catalog';
  export {showProblemsCmd, nextProblemCmd, previousProblemCmd} from './src/editor/commands';
  export {EditorState} from '@codemirror/state';
  export {openLintPanel, nextDiagnostic, previousDiagnostic, forEachDiagnostic} from '@codemirror/lint';
` }, bundle: true, format: 'esm', platform: 'neutral', target: 'es2020', write: false, logLevel: 'silent',
  define: { 'process.env.NODE_ENV': '"production"' }, plugins: [{ name: 'no-api', setup(b) {
    b.onResolve({ filter: /^(\.\/|\.\.\/)api$/ }, () => ({ path: 'api', namespace: 'stub' }));
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: `export class ApiError extends Error {}
      export const api = () => globalThis.__problemApi();` }));
  } }],
});
const cm = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text + '\n//# sourceURL=problems-unit-bundle.mjs').toString('base64'));
// Exercise QueryLab's real runtime updater without mounting React or opening a
// session. TypeScript extracts the production declarations, not copies of them.
const labSource = ts.createSourceFile('QueryLab.tsx', readFileSync(new URL('../src/QueryLab.tsx', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function labDeclaration(name) {
  let found;
  function visit(node) {
    if ((ts.isVariableDeclaration(node) || ts.isFunctionDeclaration(node)) && node.name?.getText(labSource) === name) found = node;
    ts.forEachChild(node, visit);
  }
  visit(labSource);
  assert.ok(found, name);
  return ts.isVariableDeclaration(found) ? `const ${found.getText(labSource)};` : found.getText(labSource);
}
const runtimeHarness = new Function(ts.transpileModule(`
  const IDLE_ACTION = {}, runtimeRef = {current: {}};
  const setRuntime = () => {};
  ${labDeclaration('blankRuntime')}
  ${labDeclaration('CREATE_TEMPORARY_TABLE')}
  ${labDeclaration('patchRuntime')}
  return {patch: patchRuntime, get: id => runtimeRef.current[id]};
`, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText);
const liveSession = { connection_id: 7, started_at: '2026-11-10T12:00:00Z', ended: false };
const tempRun = (text = 'CREATE TEMPORARY TABLE tmp(id INT)', status = 'ok', state = 'complete') =>
  ({ state, statements: [{ text, status }] });
const original = { setTimeout, clearTimeout, now: Date.now };
let now = new Date(2026, 10, 10, 14, 7).getTime(), next = 0, apiCalls = 0, passed = 0;
const timers = new Map(), resources = [];
globalThis.__problemApi = () => { apiCalls++; assert.fail('Problem warnings must never call the API'); };
globalThis.setTimeout = (fn, ms = 0) => { const id = ++next; timers.set(id, { at: now + ms, fn }); return id; };
globalThis.clearTimeout = id => timers.delete(id);
Date.now = () => now;
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
async function advance(ms) {
  const end = now + ms;
  await flush();
  for (;;) {
    const first = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
    if (!first) break;
    now = first[1].at; timers.delete(first[0]); first[1].fn(); await flush();
  }
  now = end; await flush();
}
function pair(connection = { host: 'localhost', port: 3306, user: 'test', database: 'app' }) {
  const env = { current: { connection, docId: 'one', backslashEscapes: true, engine: 'mysql', serverVersion: '8.4.0' } };
  const catalog = cm.catalogFor(env); resources.push(catalog);
  return { env, catalog };
}
// Seed the actual in-memory service, without allowing even test setup to read a
// server. Cache expiry, identity invalidation and notifications remain real.
function cache(p, kind, params, value, flags = {}) {
  const key = JSON.stringify([kind, params]);
  const previous = p.catalog.cache.get(key);
  if (previous) clearTimeout(previous.timer);
  const result = { value, complete: true, failed: false, truncated: false, readAt: now, ...flags };
  const entry = { result, until: now + 30000, kind, params,
    timer: setTimeout(() => p.catalog.remove(key, entry, 'expired'), 30000) };
  p.catalog.cache.set(key, entry);
  p.catalog.notify({ kind, params, reason: 'arrived' });
  return result;
}
function tables(p, names = ['t', 'users'], schema = 'app', flags = {}, params = { schema }) {
  return cache(p, 'tables', params, { schema_version: 'catalog-1.0', level: 'tables', schema,
    tables: names.map(name => ({ name, type: 'table' })), truncated: false, prefix: null, after: null, next_after: null, limit: 500 }, flags);
}
function columns(p, table = 't', names = ['id', 'name'], schema = 'app', flags = {}) {
  return cache(p, 'columns', { schema, table }, { schema_version: 'catalog-1.0', level: 'table', schema, table,
    columns: names.map(name => ({ name, type: 'int' })), indexes: [] }, flags);
}
const diagnostics = (p, sql) => cm.problemDiagnostics(cm.EditorState.create({ doc: sql }), p.env.current, p.catalog);
const messages = (p, sql) => diagnostics(p, sql).map(d => d.message);
const tableWarnings = (p, sql) => diagnostics(p, sql).filter(d => d.message.startsWith('No table'));
const columnWarnings = (p, sql) => diagnostics(p, sql).filter(d => d.message.startsWith('No column'));
async function test(name, run) {
  try { await run(); assert.equal(apiCalls, 0); passed++; }
  catch (e) { console.error(name); throw e; }
  finally { for (const c of resources.splice(0)) c.dispose(); timers.clear(); }
}

// Use the installed CodeMirror linter plugin itself, not a mocked forceLinting.
// The DOM-free view delivers real EditorState transactions to its two plugins.
function editor(p, doc) {
  const extensions = cm.problemsExtension({ problem_warnings: true }, p.env);
  const nativeSpec = extensions[0][1], appSpec = extensions[2];
  let state = cm.EditorState.create({ doc, extensions }), native, app;
  const view = { get state() { return state; }, plugin(spec) {
    return spec === nativeSpec ? native : spec === appSpec ? app : null;
  }, dispatch(spec) {
    const startState = state, tr = state.update(spec); state = tr.state;
    const update = { view, startState, state, transactions: [tr], docChanged: tr.docChanged };
    native?.update(update); app?.update(update);
  } };
  native = nativeSpec.create(view); app = appSpec.create(view);
  return { view, diagnostics() {
    const found = []; cm.forEachDiagnostic(state, (d, from, to) => found.push({ ...d, from, to })); return found;
  }, destroy() { app.destroy(); native.destroy(); } };
}

try {
  await test('peek misses never call the API; successful cache hits preserve the result', () => {
    const p = pair(); assert.equal(p.catalog.peek('tables', { schema: 'app' }), null);
    assert.equal(p.catalog.peek('schemas', {}), null);
    const stored = tables(p); assert.equal(p.catalog.peek('tables', { schema: 'app' }), stored);
    const schema = cache(p, 'schemas', {}, { schemas: [{ name: 'app' }] });
    assert.equal(p.catalog.peek('schemas', {}), schema);
    assert.deepEqual(messages(p, 'SELECT * FROM t'), []);
  });
  await test('unknown tables: FROM, JOIN, UPDATE modifiers, INTO, DELETE and comma joins', () => {
    const p = pair(); tables(p);
    for (const sql of ['SELECT * FROM missing', 'SELECT * FROM t JOIN missing m ON t.id=m.id',
      'UPDATE missing SET id=1 WHERE id=2', 'UPDATE LOW_PRIORITY IGNORE missing SET id=1 WHERE id=2',
      'INSERT INTO missing(id) VALUES(1)', 'REPLACE INTO missing VALUES(1)', 'DELETE FROM missing WHERE id=1',
      'SELECT * FROM t, missing', 'UPDATE t, missing SET t.id=1 WHERE t.id=2']) {
      const found = tableWarnings(p, sql);
      assert.equal(found.length, 1, sql);
      assert.equal(sql.slice(found[0].from, found[0].to), 'missing', sql);
      assert.equal(found[0].message, 'No table `missing` in `app` (table list read at 14:07).');
    }
  });
  await test('session temporary tables suppress only unknown-table warnings and stay per tab', () => {
    const p = pair(), other = pair(); tables(p); tables(other); columns(p);
    const sql = 'SELECT t.absent FROM t JOIN tmp ON 1=1; UPDATE tmp SET id=1; DELETE FROM tmp';
    const before = diagnostics(p, sql);
    assert.equal(before.filter(d => d.message.startsWith('No table')).length, 3);
    p.env.current.sessionTempTables = true;
    assert.deepEqual(diagnostics(p, sql), before.filter(d => !d.message.startsWith('No table')));
    assert.equal(columnWarnings(p, sql).length, 1);
    assert.equal(messages(p, sql).filter(m => m.includes('without WHERE')).length, 2);
    assert.equal(tableWarnings(other, 'SELECT * FROM tmp').length, 1);
    p.env.current.sessionTempTables = false;
    assert.deepEqual(diagnostics(p, sql), before);
  });
  await test('QueryLab remembers only successful temporary creates, after leading comments and whitespace', () => {
    const h = runtimeHarness();
    for (const [i, text] of ['CREATE TEMPORARY TABLE tmp(id INT)',
      ' \n/* note */ -- note\r\n# note\n cReAtE TeMpOrArY TaBlE tmp(id INT)',
      '--\nCREATE TEMPORARY TABLE IF NOT EXISTS tmp(id INT)'].entries()) {
      const id = String(i);
      h.patch(id, { session: liveSession });
      h.patch(id, { run: tempRun(text) });
      assert.equal(h.get(id).sessionTempTables, true, text);
      h.patch(id, { run: tempRun('SELECT * FROM tmp') });
      assert.equal(h.get(id).sessionTempTables, true, 'later reads retain the session knowledge');
    }
    for (const status of ['pending', 'running', 'error', 'stopped', 'not_run', 'session_ended']) {
      h.patch(status, { session: liveSession, run: tempRun(undefined, status) });
      assert.equal(h.get(status).sessionTempTables, false, status);
    }
    for (const [i, text] of ['CREATE TABLE tmp(id INT)', 'SELECT "CREATE TEMPORARY TABLE tmp(id INT)"',
      '/* CREATE TEMPORARY TABLE tmp(id INT) */ SELECT 1', '--not_a_comment CREATE TEMPORARY TABLE tmp(id INT)'].entries()) {
      const id = `ordinary-${i}`;
      h.patch(id, { session: liveSession, run: tempRun(text) });
      assert.equal(h.get(id).sessionTempTables, false, text);
    }
    h.patch('other', { session: liveSession });
    assert.equal(h.get('other').sessionTempTables, false, 'each tab has its own runtime');
  });
  await test('QueryLab clears temporary-table knowledge on end, close, or replacement, without replaying old results', () => {
    for (const patch of [{ session: null }, { session: { ...liveSession, ended: true } },
      { session: { ...liveSession, connection_id: 8 } },
      { session: { ...liveSession, started_at: '2026-11-10T13:00:00Z' } },
      { run: tempRun(undefined, 'ok', 'session_ended') }]) {
      const h = runtimeHarness();
      h.patch('one', { session: liveSession, run: tempRun() });
      assert.equal(h.get('one').sessionTempTables, true);
      h.patch('one', patch);
      assert.equal(h.get('one').sessionTempTables, false, JSON.stringify(patch));
      h.patch('one', { note: 'Session ended' });
      assert.equal(h.get('one').sessionTempTables, false, 'retained old results do not set the flag again');
    }
    const h = runtimeHarness();
    h.patch('one', { session: liveSession, run: tempRun() });
    h.patch('one', { session: { ...liveSession, idle_seconds: 4 } });
    assert.equal(h.get('one').sessionTempTables, true, 'a status reply for the same live session retains it');
  });
  await test('qualified schema overrides default; absent default does not guess', () => {
    const p = pair(); tables(p); tables(p, [], 'other');
    assert.match(messages(p, 'SELECT * FROM other.missing')[0], /in `other`/);
    assert.deepEqual(messages(p, 'SELECT * FROM unloaded.missing'), []);
    p.env.current.connection = { ...p.env.current.connection, database: undefined };
    p.catalog.syncConnection(); tables(p, [], 'other');
    assert.deepEqual(messages(p, 'SELECT * FROM missing'), []);
    assert.equal(tableWarnings(p, 'SELECT * FROM other.missing').length, 1);
  });
  await test('identifier case is deliberately conservative; columns ignore case too', () => {
    const p = pair(); tables(p, ['MiXeD']); columns(p, 'MiXeD', ['ID']);
    assert.deepEqual(messages(p, 'SELECT * FROM mixed'), []);
    assert.deepEqual(messages(p, 'SELECT A.id FROM MiXeD a'), []);
    assert.equal(columnWarnings(p, 'SELECT a.absent FROM MiXeD a').length, 1);
  });
  await test('missing, failed, incomplete, prefix-filtered and truncated caches cannot prove absence', () => {
    const p = pair();
    assert.deepEqual(messages(p, 'SELECT x.bad FROM missing x'), []);
    for (const flags of [{ complete: false }, { failed: true }, { truncated: true }, { value: null }]) {
      tables(p, [], 'app', flags); columns(p, 'missing', [], 'app', flags);
      assert.deepEqual(messages(p, 'SELECT x.bad FROM missing x'), []);
    }
    p.catalog.dispose();
    tables(p, [], 'app', { complete: false }, { schema: 'app', prefix: 'mi' });
    assert.deepEqual(messages(p, 'SELECT * FROM missing'), []);
    for (const value of [{ prefix: 'mi' }, { after: 'a' }, { truncated: true }]) {
      const result = tables(p, []); Object.assign(result.value, value);
      assert.deepEqual(messages(p, 'SELECT * FROM missing'), []);
    }
  });
  await test('qualified columns require a resolved table and loaded successful columns', () => {
    const p = pair(); columns(p);
    assert.deepEqual(messages(p, 'SELECT t.missing FROM t'), ['No column `missing` in `app.t`.']);
    assert.deepEqual(messages(p, 'SELECT x.missing FROM app.t AS x'), ['No column `missing` in `app.t`.']);
    assert.deepEqual(messages(p, 'SELECT missing, t.*, t.id, unknown.missing FROM t'), []);
    assert.deepEqual(messages(p, 'SELECT t.missing FROM users t'), []);
    assert.deepEqual(messages(p, 'SELECT t.fn(id) FROM t'), []);
    assert.deepEqual(messages(p, 'SELECT app.t.missing FROM app.t'), []);
    assert.deepEqual(messages(p, 'SELECT t.id FROM app.t'), []);
    columns(p, 'users', ['other']);
    const sql = 'SELECT a.nope, b.missing FROM t a JOIN users b ON a.id=b.other';
    assert.equal(columnWarnings(p, sql).length, 2);
  });
  await test('aliases never leak between statements, nested queries or UNION branches', () => {
    const p = pair(); columns(p); columns(p, 'users', ['other']);
    assert.deepEqual(messages(p, 'SELECT x.id FROM t x; SELECT x.other FROM users x'), []);
    assert.deepEqual(messages(p, 'SELECT x.id FROM t x WHERE EXISTS (SELECT x.other FROM users x)'), []);
    assert.deepEqual(messages(p, 'SELECT x.id FROM t x UNION SELECT x.other FROM users x'), []);
    assert.deepEqual(messages(p, 'SELECT x.id FROM t x WHERE EXISTS (SELECT x.unknown)'), []);
    assert.deepEqual(messages(p, 'SELECT x.unknown FROM t x JOIN users x'), []);
    assert.equal(columnWarnings(p, 'SELECT x.bad FROM t x UNION SELECT x.bad FROM users x').length, 2);
  });
  await test('DELETE targets are aliases, not physical tables, in both multi-table forms', () => {
    const p = pair(); tables(p); columns(p); columns(p, 'users');
    for (const sql of ['DELETE a, b FROM t a JOIN users b ON a.id=b.id WHERE a.id=1',
      'DELETE FROM a, b USING t a JOIN users b ON a.id=b.id WHERE a.id=1',
      'DELETE a.* FROM t a WHERE a.id=1', 'DELETE FROM a.* USING t a WHERE a.id=1']) {
      assert.deepEqual(messages(p, sql), [], sql);
    }
    assert.equal(tableWarnings(p, 'DELETE FROM a USING missing a WHERE a.id=1').length, 1);
  });
  await test('UPDATE and DELETE need a top-level WHERE, including multi-table and CTE forms', () => {
    const p = pair();
    for (const sql of ['UPDATE t SET id=1', 'UPDATE t JOIN users u ON t.id=u.id SET t.id=1',
      'UPDATE t, users SET t.id=1', 'UPDATE t SET id=(SELECT id FROM users WHERE id=1)',
      "UPDATE t SET name='WHERE' /* WHERE id=1 */", 'UPDATE t SET `where`=1',
      'UPDATE t SET t.where=1', 'WITH x AS (SELECT id FROM users WHERE id=1) UPDATE t SET id=1',
      'UPDATE t SET id=1 LIMIT 1']) {
      assert.deepEqual(messages(p, sql), ['UPDATE without WHERE: it changes every row of the table unless LIMIT or a join narrows it.'], sql);
    }
    for (const sql of ['DELETE FROM t', 'DELETE t FROM t JOIN users ON t.id=users.id',
      'DELETE FROM t USING t JOIN users ON t.id=users.id', 'DELETE FROM t ORDER BY (SELECT id FROM users WHERE id=1)',
      'WITH x AS (SELECT id FROM users WHERE id=1) DELETE FROM t', 'DELETE FROM t # WHERE id=1']) {
      assert.deepEqual(messages(p, sql), ['DELETE without WHERE: it removes every row of the table unless LIMIT or a join narrows it.'], sql);
    }
    for (const sql of ['UPDATE t SET id=1 WHERE id=2', 'DELETE FROM t WHERE id=(SELECT id FROM users)',
      'SELECT * FROM t FOR UPDATE', 'INSERT INTO t VALUES(1) ON DUPLICATE KEY UPDATE id=1',
      'EXPLAIN UPDATE t SET id=1']) assert.deepEqual(messages(p, sql), [], sql);
  });
  await test('WHERE in strings, comments, backtick names and functions is not a filter', () => {
    const p = pair();
    for (const sql of ["UPDATE t SET name='; WHERE'", 'UPDATE t SET name="WHERE"',
      'UPDATE t SET name=1 -- WHERE\n', 'UPDATE t SET name=1 # WHERE\n',
      'UPDATE `where` SET id=1', 'UPDATE t SET id=EXTRACT(YEAR FROM dt)']) {
      assert.deepEqual(messages(p, sql), ['UPDATE without WHERE: it changes every row of the table unless LIMIT or a join narrows it.'], sql);
    }
    tables(p, []);
    assert.deepEqual(messages(p, 'SELECT EXTRACT(YEAR FROM dt), SUBSTRING(s FROM n) FROM DUAL'), []);
    assert.deepEqual(messages(p, "SELECT 'FROM missing', \"JOIN missing\", @missing.col /* FROM missing */"), []);
  });
  await test('CTEs, recursive CTEs, CTE column names and their aliases are local', () => {
    const p = pair(); tables(p); columns(p, 'x');
    for (const sql of ['WITH x AS (SELECT 1) SELECT x.bad FROM x',
      'WITH RECURSIVE x(id) AS (SELECT 1 UNION SELECT id+1 FROM x) SELECT z.bad FROM x z',
      'WITH x(a) AS (SELECT 1), y AS (SELECT * FROM x) SELECT * FROM y JOIN x ON 1=1',
      'SELECT * FROM (WITH x AS (SELECT 1) SELECT * FROM x) d']) assert.deepEqual(messages(p, sql), [], sql);
    assert.equal(tableWarnings(p, 'WITH x AS (SELECT * FROM missing) SELECT * FROM x').length, 1);
    assert.equal(tableWarnings(p, 'WITH x AS (SELECT 1) SELECT * FROM app.x').length, 1);
  });
  await test('derived aliases never borrow physical metadata; inner reads still get checked', () => {
    const p = pair(); tables(p); columns(p);
    assert.deepEqual(messages(p, 'SELECT t.extra FROM (SELECT id AS extra FROM users) t'), []);
    assert.deepEqual(messages(p, 'SELECT d.extra FROM (SELECT 1 AS extra) AS d'), []);
    assert.equal(tableWarnings(p, 'SELECT d.id FROM (SELECT * FROM missing) d').length, 1);
    assert.deepEqual(messages(p, 'SELECT * FROM JSON_TABLE(\'[]\', \'$[*]\' COLUMNS(id INT PATH \'$\')) j'), []);
  });
  await test('earlier CREATE TABLE and CREATE TEMPORARY TABLE exempt only their names', () => {
    const p = pair(); tables(p, []); columns(p);
    for (const create of ['CREATE TABLE t(id INT)', 'CREATE TEMPORARY TABLE t(id INT)',
      'CREATE TABLE IF NOT EXISTS app.t(id INT)', 'CREATE OR REPLACE TEMPORARY TABLE t(id INT)']) {
      assert.deepEqual(messages(p, `${create}; SELECT t.extra FROM app.t`), [], create);
    }
    assert.equal(tableWarnings(p, 'SELECT * FROM t; CREATE TABLE t(id INT); SELECT * FROM t').length, 1);
    assert.equal(tableWarnings(p, 'CREATE TABLE other.t(id INT); SELECT * FROM t').length, 1);
    assert.equal(tableWarnings(p, 'CREATE TABLE t(id INT); SELECT * FROM missing').length, 1);
    assert.deepEqual(messages(p, 'CREATE TABLE `a``b`(id INT); SELECT * FROM `a``b`'), []);
  });
  await test('USE skips all subsequent statements, but preserves preceding diagnostics', () => {
    const p = pair(); tables(p, []);
    assert.deepEqual(messages(p, 'USE other; SELECT * FROM missing; UPDATE t SET id=1; DELETE FROM t'), []);
    assert.equal(tableWarnings(p, 'SELECT * FROM missing; USE other; SELECT * FROM missing').length, 1);
    assert.equal(tableWarnings(p, "SELECT 'USE other'; SELECT * FROM missing").length, 1);
  });
  await test('routine bodies, nested BEGIN, IF, CASE and MariaDB anonymous blocks are skipped', () => {
    const p = pair(); tables(p, []);
    for (const sql of ['CREATE PROCEDURE p() BEGIN SELECT * FROM missing; UPDATE t SET id=1; DELETE FROM t; END',
      'CREATE FUNCTION f() RETURNS INT BEGIN RETURN (SELECT id FROM missing); END',
      'CREATE TRIGGER tr BEFORE UPDATE ON t FOR EACH ROW BEGIN DELETE FROM t; END',
      'CREATE EVENT e ON SCHEDULE EVERY 1 DAY DO BEGIN DELETE FROM t; END',
      'BEGIN NOT ATOMIC DELETE FROM t; UPDATE t SET id=1; END']) assert.deepEqual(messages(p, sql), [], sql);
    const sql = 'CREATE PROCEDURE p() BEGIN IF 1 THEN BEGIN UPDATE t SET id=1; END; END IF; '
      + 'SELECT CASE WHEN 1 THEN 2 ELSE 3 END; CASE 1 WHEN 1 THEN DELETE FROM t; END CASE; END; SELECT * FROM missing';
    assert.equal(tableWarnings(p, sql).length, 1);
    assert.equal(tableWarnings(p, 'BEGIN; SELECT * FROM missing; COMMIT').length, 1, 'transaction BEGIN is not a routine');
    assert.deepEqual(messages(p, 'CREATE PROCEDURE p() BEGIN SELECT * FROM missing; DELETE FROM t'), []);
  });
  await test('DELIMITER, oversized documents and ambiguous drafts produce no diagnostics', () => {
    const p = pair(); tables(p, []);
    for (const sql of ['DELIMITER $$\nDELETE FROM t$$', '  delimiter //\nUPDATE t SET id=1//',
      'SELECT * FROM missing;\nDELIMITER $$', 'DELETE FROM t' + ' '.repeat(60000),
      'SELECT * FROM `missing', "UPDATE t SET name='unfinished", 'UPDATE t SET id=(1',
      '/*!80000 USE other */; SELECT * FROM missing', '/*M! UPDATE t SET id=1 */']) {
      assert.deepEqual(messages(p, sql), [], sql.slice(0, 100));
    }
    assert.equal(diagnostics(p, 'DELETE FROM t' + ' '.repeat(60000 - 13)).length, 2, '60,000 is allowed');
  });
  await test('backslash mode is passed to both tokenizer masking and shared statement splitting', () => {
    const p = pair();
    const sql = "UPDATE t SET name='x\\' WHERE id=1; DELETE FROM t; -- '";
    // With escapes enabled, the final quote closes the one literal: the
    // apparent WHERE and DELETE are both string content, not SQL clauses.
    assert.deepEqual(messages(p, sql), ['UPDATE without WHERE: it changes every row of the table unless LIMIT or a join narrows it.']);
    p.env.current.backslashEscapes = false;
    assert.deepEqual(messages(p, sql), ['DELETE without WHERE: it removes every row of the table unless LIMIT or a join narrows it.']);
  });
  await test('shared splitter handles semicolons and mysql client terminators with exact UTF-16 ranges', () => {
    const p = pair(); tables(p, []);
    const sql = "SELECT '😀;'; SELECT * FROM missing\\G UPDATE t SET id=1 WHERE id=2";
    assert.deepEqual(tableWarnings(p, sql).map(d => sql.slice(d.from, d.to)), ['missing', 't']);
  });
  await test('hostile identifiers stay raw text only in diagnostic messages', () => {
    const p = pair();
    const quote = s => '`' + s.replace(/`/g, '``') + '`';
    const names = ['<img src=x onerror=1>', 'a`b', '"><svg onload=window.__xss=1>', '</script>', '${x}'];
    for (const name of names) {
      tables(p, [], name);
      const sql = `SELECT * FROM ${quote(name)}.${quote(name)}`;
      const found = diagnostics(p, sql);
      assert.equal(found.length, 1);
      assert.equal(found[0].message, `No table \`${name}\` in \`${name}\` (table list read at 14:07).`);
      assert.deepEqual(Object.keys(found[0]).sort(), ['from', 'message', 'severity', 'to']);
      assert.equal(sql.slice(found[0].from, found[0].to), quote(name));
      columns(p, name, [], name);
      const column = columnWarnings(p, `SELECT z.${quote(name)} FROM ${quote(name)}.${quote(name)} z`)[0];
      assert.equal(column.message, `No column \`${name}\` in \`${name}.${name}\`.`);
      assert.deepEqual(Object.keys(column).sort(), ['from', 'message', 'severity', 'to']);
    }
  });
  await test('peek respects expiry even before a throttled timer fires', () => {
    const p = pair(); tables(p, []); columns(p);
    assert.equal(messages(p, 'SELECT t.bad FROM t').length, 2);
    now += 30000;
    assert.deepEqual(messages(p, 'SELECT t.bad FROM t'), []);
    assert.equal(p.catalog.cache.size, 0);
  });
  await test('peek invalidates on changed database, credentials or connection', () => {
    const p = pair();
    for (const patch of [{ database: 'other' }, { password: 'changed' }, { host: 'new' }]) {
      p.catalog.syncConnection(); tables(p, []);
      assert.ok(p.catalog.peek('tables', { schema: 'app' }));
      p.env.current.connection = { ...p.env.current.connection, ...patch };
      assert.equal(p.catalog.peek('tables', { schema: 'app' }), null);
    }
    p.catalog.syncConnection(); tables(p, []); p.env.current.connection = undefined;
    assert.equal(p.catalog.peek('tables', { schema: 'app' }), null);
  });
  await test('native 750 ms debounce, idle arrival, expiry and invalidation relint without reads', async () => {
    const p = pair(), live = editor(p, 'SELECT t.bad FROM t');
    try {
      await advance(750); assert.deepEqual(live.diagnostics(), []);
      tables(p); columns(p); await flush();
      assert.equal(live.diagnostics().length, 1, 'arrival after an idle lint must schedule and force a new lint');
      await advance(30000); assert.deepEqual(live.diagnostics(), [], 'expiry removes stale diagnostics');
      columns(p); await flush(); assert.equal(live.diagnostics().length, 1);
      p.env.current = { ...p.env.current, connection: { ...p.env.current.connection, password: 'new' } };
      live.view.dispatch({}); await flush(); assert.deepEqual(live.diagnostics(), []);
      live.view.dispatch({ changes: { from: 0, to: live.view.state.doc.length, insert: 'DELETE FROM t' } });
      await advance(749); assert.deepEqual(live.diagnostics(), []);
      await advance(1); assert.equal(live.diagnostics()[0].message, 'DELETE without WHERE: it removes every row of the table unless LIMIT or a join narrows it.');
    } finally { live.destroy(); }
  });
  await test('destroy unsubscribes and cancels queued refresh; disabled warnings install nothing', async () => {
    const p = pair(), live = editor(p, 'SELECT * FROM missing');
    assert.equal(p.catalog.listeners.size, 1);
    tables(p); live.destroy();
    const before = live.view.state;
    await flush(); assert.equal(live.view.state, before);
    assert.equal(p.catalog.listeners.size, 0);
    assert.deepEqual(cm.problemsExtension({ problem_warnings: false }, p.env), []);
    assert.equal(timers.size, 1, 'only the cache expiry timer remains');
  });
  await test('session backslash-mode changes relint an unchanged document', async () => {
    const p = pair(), live = editor(p, "UPDATE t SET name='x\\' WHERE id=1; DELETE FROM t; -- '");
    try {
      await advance(750);
      assert.deepEqual(live.diagnostics().map(d => d.message), ['UPDATE without WHERE: it changes every row of the table unless LIMIT or a join narrows it.']);
      p.env.current.backslashEscapes = false;
      live.view.dispatch({}); await flush();
      assert.deepEqual(live.diagnostics().map(d => d.message), ['DELETE without WHERE: it removes every row of the table unless LIMIT or a join narrows it.']);
    } finally { live.destroy(); }
  });
  await test('temporary-table flag changes relint unchanged SQL without hiding other diagnostics', async () => {
    const p = pair(); tables(p); columns(p);
    const live = editor(p, 'SELECT t.bad FROM t JOIN tmp ON 1=1; DELETE FROM tmp');
    try {
      await advance(750);
      const before = live.diagnostics();
      assert.equal(before.filter(d => d.message.startsWith('No table')).length, 2);
      p.env.current.sessionTempTables = true;
      live.view.dispatch({}); await flush();
      assert.deepEqual(live.diagnostics(), before.filter(d => !d.message.startsWith('No table')));
      p.env.current.sessionTempTables = false;
      live.view.dispatch({}); await flush();
      assert.deepEqual(live.diagnostics(), before);
    } finally { live.destroy(); }
  });
  await test('commands are the actual installed lint panel and navigation commands', () => {
    assert.equal(cm.showProblemsCmd, cm.openLintPanel);
    assert.equal(cm.nextProblemCmd, cm.nextDiagnostic);
    assert.equal(cm.previousProblemCmd, cm.previousDiagnostic);
  });
  console.log(`${passed} problem-warning tests passed; API calls: ${apiCalls}.`);
} finally {
  for (const c of resources.splice(0)) c.dispose();
  globalThis.setTimeout = original.setTimeout; globalThis.clearTimeout = original.clearTimeout; Date.now = original.now;
  delete globalThis.__problemApi;
}
