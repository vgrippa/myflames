// Real EditorState/CompletionContext, fake API and clock. No DOM or server.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
const root = fileURLToPath(new URL('..', import.meta.url));
const built = await build({ stdin: { resolveDir: root, contents: `
  export {selectedText} from './src/editor/visual';
  export * from './src/editor/catalog'; export * from './src/editor/completion';
  export {variableContext, cteNames} from './src/sqlCompletion';
  export {EditorView} from '@codemirror/view';
  export {history, undo} from '@codemirror/commands';
  export {Transaction} from '@codemirror/state';
  export {pickedCompletion, selectedCompletion, setSelectedCompletion} from '@codemirror/autocomplete';
  export {wordsFor, functionsFor} from './src/editor/engineWords';
  export {EditorState, EditorSelection} from '@codemirror/state';
  export {CompletionContext, autocompletion, startCompletion, closeCompletion, completionStatus, currentCompletions} from '@codemirror/autocomplete';
` }, bundle: true, format: 'esm', platform: 'neutral', target: 'es2020', write: false, logLevel: 'silent',
  define: { 'process.env.NODE_ENV': '"production"' }, plugins: [{ name: 'api', setup(b) {
    b.onResolve({ filter: /^(\.\/|\.\.\/)api$/ }, () => ({ path: 'api', namespace: 'stub' }));
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: `export class ApiError extends Error {}
      export const api = (path, body, signal) => globalThis.__catalogApi(path, body, signal);
      export const paramNames = (engine, version, signal) => api("config/versions/names", {engine, version}, signal);` }));
  } }],
});
const cm = await import('data:text/javascript;base64,' + Buffer.from(built.outputFiles[0].text + '\n//# sourceURL=completion-source-bundle.mjs').toString('base64'));
const original = { setTimeout, clearTimeout, now: Date.now };
let now = 100000, next = 0, timers = new Map(), sent = [], handler, passed = 0;
globalThis.setTimeout = (fn, ms = 0) => { const id = ++next; timers.set(id, { at: now + ms, fn }); return id; };
globalThis.clearTimeout = id => timers.delete(id); Date.now = () => now;
const flush = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
async function advance(ms) {
  const end = now + ms; await flush();
  for (;;) {
    const first = [...timers].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
    if (!first) break;
    now = first[1].at; timers.delete(first[0]); first[1].fn(); await flush();
  }
  now = end; await flush();
}
const conn = () => ({ name: 'test', host: 'localhost', port: 3306, user: 'reader', database: 'app', ssl_mode: 'REQUIRED', password: 'secret' });
const schemas = names => ({ schemas: names.map(name => ({ name })) });
const tables = (names, truncated = false) => ({ tables: names.map(name => ({ name, type: 'table' })), truncated });
const columns = names => ({ columns: names.map(name => ({ name, type: 'varchar(20)' })) });
const resources = [], owner = (generation = 1, editor = {}) => ({ editor, generation });
function service(get = () => conn()) { const value = new cm.CatalogService(get); resources.push(value); return value; }
function sourceEnv() {
  const env = { current: { connection: conn(), docId: 'tab1', backslashEscapes: true } };
  const sources = cm.completionSources(env), [source, localSource] = sources;
  resources.push(cm.catalogFor(env)); return { env, source, localSource, sources };
}
function context(marked, explicit = false, selection) {
  const pos = marked.indexOf('|'), doc = marked.replace('|', '');
  const state = cm.EditorState.create({ doc, selection: selection || { anchor: pos }, extensions: [cm.EditorState.allowMultipleSelections.of(true)] });
  return new cm.CompletionContext(state, pos, explicit);
}
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
// Exercise the app's lifecycle plugin with real transactions but without a DOM.
// CodeMirror's own tooltip rendering remains a browser-suite gate.
async function nativeEditor(pair, marked, restoredPending = false) {
  const extension = cm.completionExtension({ completion_auto: true }, pair.env);
  const nativeExtension = cm.autocompletion({ override: pair.sources, defaultKeymap: false });
  const initial = context(marked);
  let state = cm.EditorState.create({ doc: initial.state.doc, selection: initial.state.selection, extensions: [nativeExtension, extension] });
  let native, app;
  const view = { hasFocus: true, composing: false, get state() { return state; }, dispatch(spec) {
    const before = state, tr = state.update(spec); state = tr.state;
    const update = { view, startState: before, state, docChanged: tr.docChanged, selectionSet: !!tr.selection, transactions: [tr] };
    native?.update(update); app?.update(update);
  } };
  if (restoredPending) cm.startCompletion(view);
  // Installed autocomplete 6.20.4's actual scheduler, ahead of our lifecycle
  // plugin just as in SqlEditor.tsx. No rendering plugin or DOM is instantiated.
  native = nativeExtension.find(value => value.domEventHandlers?.compositionstart).create(view);
  app = extension.find(value => value.baseExtensions && !value.domEventHandlers && !value.domEventObservers).create(view);
  const observer = extension.find(value => value.domEventObservers?.keydown).domEventObservers;
  await flush();
  const handlers = extension.find(value => value.domEventHandlers?.compositionstart).domEventHandlers;
  return { view, observer, handlers, escape() { observer.keydown({ key: 'Escape' }, view); cm.closeCompletion(view); }, destroy() {
    app.destroy(); clearTimeout(native.debounceUpdate); clearTimeout(native.debounceAccept);
    for (const query of native.running) {
      for (const listener of query.context.abortListeners || []) listener();
      query.context.abortListeners = null;
    }
  } };
}
globalThis.__catalogApi = (path, body, signal) => { sent.push({ path, body, signal, at: now }); return handler(path, body, signal); };
async function test(name, run) {
  sent = []; handler = path => Promise.resolve(path === 'live/schemas' ? schemas(['app']) : tables(['users']));
  try { await run(); passed++; } catch (e) { console.error(name); throw e; }
  finally { for (const resource of resources.splice(0)) resource.dispose(); timers.clear(); }
}
try {
  handler = () => Promise.resolve({ names: [] });
  await cm.preloadCompletion({ current: { engine: 'mysql', serverVersion: '8.4.6' } });
  sent = [];
  await test('local keywords and cached names return before a pending catalog read', async () => {
    const pair = sourceEnv(), held = deferred(); handler = () => held.promise;
    const pending = pair.source(context('S|'));
    assert.equal((await pair.localSource(context('S|'))).options[0].label, 'SELECT');
    assert.equal(sent.length, 0); await advance(220); assert.equal(sent.length, 1);
    held.resolve({ schemas: [{ name: 'app', tables: 2 }] }); await pending;
    handler = () => Promise.resolve(tables(['users', 'usage']));
    await pair.source(context('SELECT * FROM u|', true));
    const reads = sent.length;
    assert.deepEqual((await pair.localSource(context('SELECT 1; SELECT * FROM us|'))).options.map(o => o.label), ['users', 'usage']);
    assert.equal(sent.length, reads);
  });
  await test('preload makes release keywords, functions and variables available on the first local call', async () => {
    const pair = sourceEnv(); Object.assign(pair.env.current, { engine: 'mysql', serverVersion: '8.4.3' });
    handler = () => Promise.resolve({ names: [{ name: 'max_connections', scope: 'global', deprecated: false }] });
    await cm.preloadCompletion(pair.env); const reads = sent.length;
    assert.ok((await pair.localSource(context('OPTIM|'))).options.some(o => o.label === 'OPTIMIZER_COSTS'));
    assert.ok((await pair.localSource(context('SELECT POW|'))).options.some(o => o.type === 'function' && o.label === 'POW'));
    assert.equal((await pair.localSource(context('SELECT @@max_c|'))).options[0].label, 'max_connections');
    assert.equal(sent.length, reads);
  });
  await test('native sources merge catalog names into the same open dialog and retain the highlighted object', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, '|'), held = deferred(); handler = () => held.promise;
    live.view.dispatch({ changes: { from: 0, insert: 'SE' }, selection: { anchor: 2 }, userEvent: 'input.type' });
    await advance(0);
    assert.ok(cm.currentCompletions(live.view.state).some(o => o.label === 'SELECT'), 'no debounce before local words');
    live.view.dispatch({ effects: cm.setSelectedCompletion(1) });
    const highlighted = cm.selectedCompletion(live.view.state);
    // aria-controls identifies CM's existing dialog, independently of DOM rendering.
    const attributes = () => live.view.state.facet(cm.EditorView.contentAttributes)
      .map(value => typeof value === 'function' ? value(live.view) : value).find(value => value['aria-controls']);
    const dialog = attributes()?.['aria-controls'];
    await advance(220); assert.equal(sent.length, 1);
    held.resolve(schemas(['SE_database'])); await advance(0);
    assert.ok(cm.currentCompletions(live.view.state).some(o => o.label === 'SE_database'));
    assert.equal(cm.selectedCompletion(live.view.state), highlighted, 'the exact local option object stays highlighted');
    assert.equal(attributes()?.['aria-controls'], dialog);
    live.destroy();
  });
  await test('native cached stale table names appear immediately and merge refreshed names', async () => {
    const pair = sourceEnv(), catalog = cm.catalogFor(pair.env);
    handler = path => Promise.resolve(path === 'live/schemas' ? { schemas: [{ name: 'app', tables: 1 }] } : tables(['users']));
    await pair.source(context('SELECT * FROM u|', true)); await advance(30000);
    const schemasRead = deferred(), tablesRead = deferred();
    handler = path => path === 'live/schemas' ? schemasRead.promise : tablesRead.promise;
    const live = await nativeEditor(pair, 'SELECT 1; SELECT * FROM u|');
    const pos = live.view.state.selection.main.head;
    live.view.dispatch({ changes: { from: pos, insert: 's' }, selection: { anchor: pos + 1 }, userEvent: 'input.type' });
    await advance(0); const users = cm.currentCompletions(live.view.state).find(o => o.label === 'users');
    assert.ok(users, 'stale complete table names do not wait for debounce or refresh');
    const before = sent.length; await advance(219); assert.equal(sent.length, before);
    await advance(1); assert.equal(sent.length, before + 1);
    schemasRead.resolve({ schemas: [{ name: 'app', tables: 2 }] }); await flush();
    tablesRead.resolve(tables(['users', 'usage'])); await advance(0);
    assert.deepEqual(cm.currentCompletions(live.view.state).map(o => o.label), ['usage', 'users']);
    assert.equal(cm.currentCompletions(live.view.state).find(o => o.label === 'users'), users);
    assert.equal(catalog.peek('tables', { schema: 'app' }).stale, false); live.destroy();
  });
  await test('native saturated local vocabulary leaves room for arriving names within the shared cap', async () => {
    const pair = sourceEnv(), held = deferred();
    Object.assign(pair.env.current, { engine: 'mysql', serverVersion: '8.4.3' });
    handler = path => path === 'config/versions/names' ? Promise.resolve({ names: [] }) : held.promise;
    const live = await nativeEditor(pair, '|'); cm.startCompletion(live.view); await advance(50);
    assert.equal(cm.currentCompletions(live.view.state).length, 20);
    held.resolve(schemas(Array.from({ length: 60 }, (_, i) => `db${i}`))); await advance(0);
    assert.equal(cm.currentCompletions(live.view.state).length, 40);
    assert.ok(cm.currentCompletions(live.view.state).some(o => o.type === 'schema'));
    live.destroy();
  });
  await test('native no-option failure and timeout hints survive; Ctrl+Space retries immediately', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, 'SELECT * FROM zz|');
    handler = () => Promise.reject(new Error('denied'));
    cm.startCompletion(live.view); await advance(50);
    assert.equal(live.view.state.field(cm.completionHint), 'Some catalog suggestions are unavailable.');
    const before = sent.length;
    live.observer.keydown({ ctrlKey: true, key: ' ' });
    handler = () => new Promise(() => {}); cm.startCompletion(live.view); await advance(50);
    assert.ok(sent.length > before, 'manual retry bypasses the cached failure');
    await advance(8000);
    assert.equal(live.view.state.field(cm.completionHint), 'Catalog suggestions timed out. Try Ctrl+Space again.');
    live.destroy();
  });
  await test('native inherited explicit status respects failure cache; both shortcuts retry', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, 'SE|'); handler = () => Promise.reject(new Error('denied'));
    cm.startCompletion(live.view); await advance(50); assert.equal(sent.length, 1);
    live.view.dispatch({ changes: { from: 2, insert: 'L' }, selection: { anchor: 3 }, userEvent: 'input.type' });
    await advance(220); assert.equal(sent.length, 1, 'typing must not inherit explicit retry privileges');
    for (const shiftKey of [false, true]) {
      live.observer.keydown({ ctrlKey: true, shiftKey, code: 'Space' });
      cm.startCompletion(live.view); await advance(50);
    }
    assert.equal(sent.length, 3); live.destroy();
  });
  await test('native disabling automatic suggestions closes the list and keeps manual completion', async () => {
    const pair = sourceEnv(); pair.env.current.connection = undefined;
    const live = await nativeEditor(pair, 'SE|'); cm.startCompletion(live.view); await advance(50);
    assert.ok(cm.currentCompletions(live.view.state).length);
    cm.completionExtension({ completion_auto: false }, pair.env); live.view.dispatch({}); await advance(0);
    assert.equal(cm.completionStatus(live.view.state), null);
    live.view.dispatch({ changes: { from: 2, insert: 'L' }, selection: { anchor: 3 }, userEvent: 'input.type' });
    await advance(0); assert.equal(cm.completionStatus(live.view.state), null);
    live.observer.keydown({ ctrlKey: true, key: ' ' }); cm.startCompletion(live.view); await advance(50);
    assert.ok(cm.currentCompletions(live.view.state).some(o => o.label === 'SELECT')); live.destroy();
  });
  await test('native composition, edits, selection, credentials and destruction cancel reads', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, 'SE|'); handler = () => new Promise(() => {});
    cm.startCompletion(live.view); await advance(50);
    live.handlers.compositionstart({}, live.view); await advance(0);
    assert.equal(sent.at(-1).signal.aborted, true); assert.equal(cm.completionStatus(live.view.state), null);
    cm.startCompletion(live.view); await advance(50); assert.deepEqual(cm.currentCompletions(live.view.state), []);
    live.handlers.compositionend();
    cm.startCompletion(live.view); await advance(50);
    live.view.dispatch({ changes: { from: 2, insert: 'L' }, selection: { anchor: 3 }, userEvent: 'input.type' });
    assert.equal(sent.at(-1).signal.aborted, true); await advance(220);
    live.view.dispatch({ selection: { anchor: 2 } }); assert.equal(sent.at(-1).signal.aborted, true);
    cm.startCompletion(live.view); await advance(50);
    pair.env.current.connection = { ...conn(), password: 'new' }; live.view.dispatch({}); await advance(0);
    assert.equal(sent.at(-1).signal.aborted, true); assert.equal(cm.completionStatus(live.view.state), null);
    cm.startCompletion(live.view); await advance(50); live.destroy(); assert.equal(sent.at(-1).signal.aborted, true);
  });
  await test('stale names survive one shared refresh, failure and identity invalidation', async () => {
    let connection = conn(); const catalog = service(() => connection);
    const first = await catalog.read('tables', { schema: 'app' }, owner());
    await advance(30000); const stale = catalog.peek('tables', { schema: 'app' });
    assert.equal(stale.stale, true); assert.equal(stale.value, first.value);
    const held = deferred(); handler = () => held.promise;
    assert.equal(await catalog.read('tables', { schema: 'app' }, owner()), stale);
    assert.equal(await catalog.read('tables', { schema: 'app' }, owner()), stale);
    assert.equal(sent.length, 2, 'only one refresh per key');
    held.reject(new Error('offline')); await flush();
    assert.equal(catalog.peek('tables', { schema: 'app' }), stale);
    assert.equal(await catalog.read('tables', { schema: 'app' }, owner()), stale);
    assert.equal(sent.length, 2, 'failed refresh has a bounded retry delay');
    connection = { ...connection, password: 'changed' }; catalog.syncConnection();
    assert.equal(catalog.peek('tables', { schema: 'app' }), null);
  });
  await test('single flight cancellation rejects only the cancelled owner', async () => {
    const catalog = service(), held = deferred(), one = owner(), two = owner(); handler = () => held.promise;
    const a = catalog.read('schemas', {}, one), b = catalog.read('schemas', {}, two);
    const rejected = assert.rejects(a, { name: 'AbortError' }); catalog.cancelOwner(one); await rejected;
    assert.equal(sent.length, 1); assert.equal(sent[0].signal.aborted, false);
    held.resolve(schemas(['app'])); assert.equal((await b).value.schemas[0].name, 'app');
  });
  await test('prefix changes immediately reuse cached names without enabling fuzzy matching', async () => {
    const pair = sourceEnv();
    handler = path => Promise.resolve(path === 'live/schemas' ? schemas(['app']) : tables(['users']));
    await pair.source(context('SELECT * FROM us|', true));
    const before = sent.length;
    const wider = await pair.localSource(context('SELECT 1; SELECT * FROM u|'));
    assert.equal(wider.options[0].label, 'users'); assert.equal(sent.length, before);
    assert.equal((await pair.localSource(context('SELECT * FROM usr|'))).options.length, 0, 'partial lists stay prefix-only');
    const pending = pair.source(context('SELECT * FROM u|'));
    await advance(219); assert.equal(sent.length, before);
    await advance(1); await pending; assert.equal(sent.length, before + 1, 'a new prefix still gets its own bounded read');
  });
  await test('idle preload runs on mount and when engine/version become known', async () => {
    const pair = sourceEnv();
    handler = () => Promise.resolve({ names: [{ name: 'max_connections', scope: 'global' }] });
    const live = await nativeEditor(pair, '|'); await advance(0);
    assert.equal(sent.length, 0, 'mount without engine preloads the chunk without asking for variables');
    Object.assign(pair.env.current, { engine: 'mysql', serverVersion: '8.4.3' });
    live.view.dispatch({}); assert.equal(sent.length, 0, 'preloading waits for idle');
    await advance(0); assert.equal(sent.length, 1); assert.equal(sent[0].path, 'config/versions/names');
    assert.equal((await pair.localSource(context('SELECT @@max_c|'))).options[0].label, 'max_connections');
    live.destroy();
  });
  await test('selected text marks follow multiple ranges, edits and focus-independent state', () => {
    let state = cm.EditorState.create({ doc: 'SELECT 1, 2',
      selection: cm.EditorSelection.create([cm.EditorSelection.range(0, 6), cm.EditorSelection.range(10, 11)]),
      extensions: [cm.EditorState.allowMultipleSelections.of(true), cm.selectedText] });
    const ranges = () => { const out = []; state.field(cm.selectedText).between(0, state.doc.length,
      (from, to, mark) => out.push([from, to, mark.spec.class])); return out; };
    assert.deepEqual(ranges(), [[0, 6, 'cm-selectedText'], [10, 11, 'cm-selectedText']]);
    state = state.update({ changes: { from: 0, insert: ' ' } }).state;
    assert.deepEqual(ranges(), [[1, 7, 'cm-selectedText'], [11, 12, 'cm-selectedText']]);
    state = state.update({ selection: { anchor: 1 } }).state; assert.deepEqual(ranges(), []);
  });
  await test('entry cap evicts stale failures before fresh good entries', async () => {
    const catalog = service(); handler = (_path, body) => body.table === 'denied'
      ? Promise.reject(new Error('denied')) : Promise.resolve(columns(['id']));
    await catalog.read('columns', { schema: 'app', table: 'first' }, owner());
    await catalog.read('columns', { schema: 'app', table: 'denied' }, owner());
    for (let i = 0; i < 62; i++) await catalog.read('columns', { schema: 'app', table: `t${i}` }, owner());
    await advance(10000);
    await catalog.read('columns', { schema: 'app', table: 'new' }, owner());
    assert.ok(catalog.peek('columns', { schema: 'app', table: 'first' }));
    assert.equal(catalog.peek('columns', { schema: 'app', table: 'denied' }), null);
    assert.equal(catalog.cache.size, 64);
  });
  await test('automatic threshold and explicit local completion', async () => {
    const { source, env } = sourceEnv(); env.current.connection = undefined;
    assert.equal((await source(context('S|'))).options[0].label, 'SELECT'); assert.equal(await source(context('|')), null);
    assert.equal((await source(context('SE|'))).options[0].label, 'SELECT');
    assert.equal((await source(context('S|', true))).options[0].apply, 'SELECT');
    assert.ok((await source(context('|', true))).options.length > 0); assert.equal(sent.length, 0);
  });
  await test('220 ms debounce, schemas before tables, exact prefix', async () => {
    const { source } = sourceEnv(), held = deferred();
    handler = path => path === 'live/schemas' ? held.promise : Promise.resolve(tables(['users', 'unrelated']));
    const answer = source(context('SELECT * FROM us|'));
    await advance(219); assert.equal(sent.length, 0);
    await advance(1); assert.deepEqual(sent.map(c => c.path), ['live/schemas']);
    held.resolve(schemas(['app'])); await flush();
    assert.equal(sent[1].path, 'live/catalog'); assert.deepEqual(sent[1].body, { connection: conn(), schema: 'app', prefix: 'us' });
    assert.deepEqual((await answer).options.map(o => o.label), ['users']);
    assert.ok(sent.every(c => c.signal instanceof AbortSignal));
  });
  await test('explicit is immediate; qualifier dot opens automatically and reads qualified columns', async () => {
    const { source } = sourceEnv(); const manual = source(context('SELECT * FROM u|', true)); await flush();
    assert.equal(sent.length, 2); assert.equal((await manual).options[0].label, 'users');
    sent = []; handler = () => Promise.resolve(columns(['name']));
    const dot = source(context('SELECT app.users.|'));
    await advance(219); assert.equal(sent.length, 0); await advance(1); assert.equal(sent.length, 1);
    assert.deepEqual(sent[0].body, { connection: conn(), schema: 'app', table: 'users' });
    assert.equal((await dot).options[0].detail, 'app.users · varchar(20)');
  });
  await test('lexical suppressions and selections never request metadata', async () => {
    const { source } = sourceEnv();
    for (const sql of ["SELECT 'na|me'", 'SELECT "na|me"', 'SELECT /* na| */', 'SELECT -- na|', 'SELECT # na|',
      'SELECT @na|', 'SELECT :na|', 'DELIMITER $$\nSELECT na|', 'a'.repeat(60001) + '|'])
      assert.equal(await source(context(sql, true)), null, sql.slice(0, 35));
    assert.equal(await source(context('SELECT name|', true, { anchor: 7, head: 11 })), null); assert.equal(sent.length, 0);
  });
  await test('backslash mode comes from the environment', async () => {
    const { env, source } = sourceEnv(); env.current.connection = undefined;
    assert.equal(await source(context("SELECT 'x\\' na|", true)), null);
    env.current.backslashEscapes = false; assert.notEqual(await source(context("SELECT 'x\\' na|", true)), null);
  });
  await test('30 second success TTL, arrival/expiry events and unsubscribe', async () => {
    const catalog = service(), events = [], off = catalog.subscribe(event => events.push(event));
    const first = await catalog.read('schemas', {}, owner());
    assert.equal(first.complete, true); assert.equal(first.truncated, false); assert.equal(first.readAt, now);
    await advance(29999); assert.equal(await catalog.read('schemas', {}, owner()), first); assert.equal(sent.length, 1);
    await advance(1); assert.deepEqual(events.map(e => e.reason), ['arrived', 'expired']);
    await catalog.read('schemas', {}, owner(), false, true); assert.equal(sent.length, 2);
    off(); await advance(30000); assert.equal(events.length, 3);
  });
  await test('10 second failure TTL and explicit bypass', async () => {
    const catalog = service(); handler = () => Promise.reject(new Error('denied'));
    const first = await catalog.read('schemas', {}, owner());
    assert.equal(first.failed, true); assert.equal(first.complete, false); assert.equal(first.value, null);
    await advance(9999); assert.equal(await catalog.read('schemas', {}, owner()), first); assert.equal(sent.length, 1);
    await advance(1); await catalog.read('schemas', {}, owner()); assert.equal(sent.length, 2);
    handler = () => Promise.resolve(schemas(['fixed']));
    assert.equal((await catalog.read('schemas', {}, owner(), true)).failed, false); assert.equal(sent.length, 3);
    assert.equal((await catalog.read('schemas', {}, owner())).value.schemas[0].name, 'fixed'); assert.equal(sent.length, 3);
  });
  await test('connection identity, database and password invalidate', async () => {
    let connection = conn(); const catalog = service(() => connection); await catalog.read('schemas', {}, owner());
    for (const patch of [{ host: 'new' }, { database: 'other' }, { password: 'changed' }, { ssh_host: 'jump' }, { profile_id: 'profile' }]) {
      const count = sent.length; connection = { ...connection, ...patch };
      await catalog.read('schemas', {}, owner()); assert.equal(sent.length, count + 1);
    }
    connection = conn(); await catalog.read('schemas', {}, owner()); assert.equal(sent.length, 7);
  });
  await test('64 entry FIFO cap; cache hits do not reorder', async () => {
    const catalog = service(); handler = () => Promise.resolve(columns(['id']));
    for (let i = 0; i < 64; i++) await catalog.read('columns', { schema: 'app', table: `t${i}` }, owner());
    await catalog.read('columns', { schema: 'app', table: 't0' }, owner()); assert.equal(sent.length, 64);
    await catalog.read('columns', { schema: 'app', table: 't64' }, owner()); assert.equal(sent.length, 65);
    await catalog.read('columns', { schema: 'app', table: 't1' }, owner()); assert.equal(sent.length, 65);
    await catalog.read('columns', { schema: 'app', table: 't0' }, owner()); assert.equal(sent.length, 66);
  });
  await test('filtered or truncated reads never claim a complete inventory', async () => {
    const catalog = service(); handler = () => Promise.resolve(tables(['users']));
    assert.equal((await catalog.read('tables', { schema: 'app', prefix: 'us' }, owner())).complete, false);
    assert.equal((await catalog.read('tables', { schema: 'app', prefix: '' }, owner())).complete, true);
    handler = () => Promise.resolve(tables(['users'], true)); const partial = await catalog.read('tables', { schema: 'other' }, owner());
    assert.equal(partial.truncated, true); assert.equal(partial.complete, false);
  });
  await test('cancel only the owner generation; late results never fill cache', async () => {
    const catalog = service(), a = deferred(), b = deferred(), editor = {}, one = owner(1, editor), two = owner(2, editor);
    handler = (_path, body) => body.table === 'a' ? a.promise : b.promise;
    const pending = catalog.read('columns', { schema: 'app', table: 'a' }, one), rejected = assert.rejects(pending, { name: 'AbortError' });
    const other = catalog.read('columns', { schema: 'app', table: 'b' }, two);
    catalog.cancelOwner({ ...one }); await rejected;
    assert.equal(sent[0].signal.aborted, true); assert.equal(sent[1].signal.aborted, false);
    a.resolve(columns(['stale'])); b.resolve(columns(['fresh'])); await other; await flush();
    handler = () => Promise.resolve(columns(['new']));
    assert.equal((await catalog.read('columns', { schema: 'app', table: 'a' }, one)).value.columns[0].name, 'new'); assert.equal(sent.length, 3);
  });
  await test('connection change aborts even when switched back before response', async () => {
    let connection = conn(); const catalog = service(() => connection), held = deferred(); handler = () => held.promise;
    const pending = catalog.read('schemas', {}, owner()), rejected = assert.rejects(pending, { name: 'AbortError' });
    connection = { ...connection, password: 'new' }; catalog.syncConnection(); connection = conn(); catalog.syncConnection();
    await rejected; assert.equal(sent[0].signal.aborted, true);
    held.resolve(schemas(['stale'])); await flush(); handler = () => Promise.resolve(schemas(['fresh']));
    assert.equal((await catalog.read('schemas', {}, owner())).value.schemas[0].name, 'fresh');
  });
  await test('8 second total budget, timeout note and local fallback', async () => {
    const { source } = sourceEnv(); handler = () => new Promise(() => {});
    const pending = source(context('SE|')); let settled = false; pending.then(() => { settled = true; });
    await advance(7999); assert.equal(settled, false); await advance(1); const result = await pending;
    assert.equal(result.note, 'Catalog suggestions timed out. Try Ctrl+Space again.');
    assert.equal(result.options[0].label, 'SELECT'); assert.equal(sent[0].signal.aborted, true);
  });
  await test('exact unavailable/truncated notes with precedence', async () => {
    let pair = sourceEnv(); handler = () => Promise.reject(new Error('denied'));
    assert.equal((await pair.source(context('SELECT * FROM us|', true))).note, 'Some catalog suggestions are unavailable.');
    pair = sourceEnv(); handler = path => Promise.resolve(path === 'live/schemas' ? schemas(['app']) : tables(['users'], true));
    assert.equal((await pair.source(context('SELECT * FROM us|', true))).note, 'More tables match. Keep typing to narrow the list.');
    pair = sourceEnv(); handler = path => path === 'live/schemas' ? Promise.reject(new Error('denied')) : Promise.resolve(tables(['users'], true));
    assert.equal((await pair.source(context('SELECT * FROM us|', true))).note, 'Some catalog suggestions are unavailable.');
  });
  await test('new request and CM abort cancel transport/debounce', async () => {
    const { source } = sourceEnv(); handler = () => new Promise(() => {});
    const first = source(context('SE|')); await advance(100);
    assert.equal(await source(context('|')), null); assert.equal(await first, null); await advance(1000); assert.equal(sent.length, 0);
    const ctx = context('SE|', true), pending = source(ctx); await flush(); assert.equal(ctx.abortOnDocChange, true);
    for (const listener of ctx.abortListeners) listener(); ctx.abortListeners = null;
    assert.equal(await pending, null); assert.equal(sent[0].signal.aborted, true);
  });
  await test('hostile labels/details are raw text and apply is a quoted string', async () => {
    const names = ['<img src=x onerror=1>', 'a`b', '${x}', '"><svg onload=window.__xss=1>', '</script>'];
    const inserts = ['`<img src=x onerror=1>`', '`a``b`', '`${x}`', '`"><svg onload=window.__xss=1>`', '`</script>`'];
    const { source } = sourceEnv();
    handler = () => Promise.resolve({ columns: names.map(name => ({ name, type: "enum('<img src=x onerror=1>','${x}')" })) });
    const result = await source(context('SELECT app.users.|', true));
    assert.equal(result.filter, false); assert.equal(result.validFor, undefined);
    assert.deepEqual(result.options.map(o => o.label), names); assert.deepEqual(result.options.map(o => o.apply), inserts);
    assert.ok(result.options.every(o => o.type === 'column' && typeof o.apply === 'string' && !o.info));
    assert.equal(result.options[0].detail, "app.users · enum('<img src=x onerror=1>','${x}')");
  });
  await test('stable order and 40 cap', async () => {
    const { source } = sourceEnv();
    handler = path => Promise.resolve(path === 'live/schemas' ? schemas(['app']) : tables(Array.from({ length: 60 }, (_, i) => `t${59 - i}`)));
    const result = await source(context('SELECT * FROM t|', true));
    assert.equal(result.options.length, 40); assert.equal(result.options[0].label, 't59'); assert.equal(result.options[39].label, 't20');
  });
  await test('hostile schema/table names stay in text and quoted insertion fields', async () => {
    const pair = sourceEnv(), name = '<img src=x onerror=1>', tick = 'a`b';
    handler = () => Promise.resolve(schemas([name, tick]));
    const database = await pair.source(context('USE |', true));
    assert.deepEqual(database.options, [
      { label: name, detail: 'Database', type: 'schema', apply: '`<img src=x onerror=1>`' },
      { label: tick, detail: 'Database', type: 'schema', apply: '`a``b`' },
    ]);
    pair.env.current.connection = { ...conn(), database: name };
    handler = path => Promise.resolve(path === 'live/schemas' ? schemas([]) : tables([tick]));
    const table = await pair.source(context('SELECT * FROM |', true));
    assert.deepEqual(table.options, [{ label: tick, detail: name, type: 'table', apply: '`a``b`' }]);
  });
  await test('alias targets preserve table order despite reverse response order', async () => {
    const { source } = sourceEnv(), first = deferred(), second = deferred();
    handler = (_path, body) => body.table === 'users' ? first.promise : second.promise;
    const pending = source(context('SELECT na| FROM app.users u JOIN app.roles r ON u.id=r.id', true));
    await flush(); assert.deepEqual(sent.map(c => c.body.table), ['users', 'roles']);
    second.resolve(columns(['name_role'])); await flush(); first.resolve(columns(['name_user']));
    assert.deepEqual((await pending).options.map(o => o.apply), ['u.name_user', 'r.name_role']);
  });
  await test('native scheduler: manual reads immediately, cached names survive typing without another read', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, 'SELECT * FROM us|');
    cm.startCompletion(live.view); await advance(49); assert.equal(sent.length, 0);
    await advance(1); assert.equal(sent.length, 2);
    await advance(50); assert.deepEqual(cm.currentCompletions(live.view.state).map(o => o.label), ['users']);
    const pos = live.view.state.selection.main.head;
    live.view.dispatch({ changes: { from: pos, insert: 'e' }, selection: { anchor: pos + 1 }, userEvent: 'input.type' });
    await advance(219); assert.equal(sent.length, 2);
    await advance(1); assert.equal(sent.length, 3);
    await advance(50); assert.deepEqual(cm.currentCompletions(live.view.state).map(o => o.label), ['users']); live.destroy();
  });
  await test('native scheduler cannot reopen after Escape at any read stage', async () => {
    for (const stage of ['before', 'during', 'ready']) {
      const pair = sourceEnv(), live = await nativeEditor(pair, 'SE|'), held = deferred(); handler = () => held.promise;
      cm.startCompletion(live.view);
      if (stage !== 'before') await advance(50);
      if (stage === 'ready') { held.resolve(schemas(['SE_database'])); await flush(); }
      const read = sent.at(-1); live.escape(); await advance(1000);
      assert.equal(cm.completionStatus(live.view.state), null, stage);
      assert.deepEqual(cm.currentCompletions(live.view.state), [], stage);
      assert.equal(live.view.state.field(cm.completionHint), '', stage);
      if (stage === 'during') assert.equal(read.signal.aborted, true);
      live.destroy();
    }
  });
  await test('native pending completion stored with a tab stays closed on restoration', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, 'SE|', true);
    await advance(1000);
    assert.equal(cm.completionStatus(live.view.state), null);
    assert.deepEqual(cm.currentCompletions(live.view.state), []);
    assert.equal(live.view.state.field(cm.completionHint), '');
    assert.ok(sent.every(call => call.signal.aborted)); live.destroy();
  });
  await test('manual completion remains available with automatic suggestions off', async () => {
    const { env, source } = sourceEnv(); env.current.connection = undefined;
    let state = cm.EditorState.create({ doc: 'S', selection: { anchor: 1 }, extensions: [
      cm.autocompletion({ override: [source], activateOnTyping: false, defaultKeymap: false }), cm.completionExtension({ completion_auto: false }, env),
    ] });
    const view = { get state() { return state; }, dispatch(spec) { state = state.update(spec).state; } };
    view.dispatch({ changes: { from: 1, insert: 'E' }, userEvent: 'input.type' }); assert.equal(cm.completionStatus(state), null);
    assert.equal(cm.startCompletion(view), true); assert.equal(cm.completionStatus(state), 'pending');
    assert.equal((await source(new cm.CompletionContext(state, 2, true))).options[0].label, 'SELECT'); assert.equal(cm.closeCompletion(view), true);
    assert.equal(await source(new cm.CompletionContext(state, 2, false)), null);
  });

  await test('connected vocabulary follows generated engine and release facts', async () => {
    for (const [engine, version, prefix, word, present, detail] of [
      ['mysql', '8.0.13', 'LATER', 'LATERAL', false],
      ['mysql', '8.0.14', 'LATER', 'LATERAL', true, 'reserved'],
      ['mysql', '8.0.36', 'QUAL', 'QUALIFY', false],
      ['mysql', '8.4.6', 'QUAL', 'QUALIFY', true, ''],
      ['mariadb', '11.8.2', 'VECT', 'VECTOR', true, 'reserved'],
    ]) {
      // Verify expectations against the decoded generated data as well as the source.
      assert.equal(cm.wordsFor(engine, version).keywords.some(k => k.name === word), present);
      const pair = sourceEnv(); Object.assign(pair.env.current, { connection: undefined, engine, serverVersion: version });
      const result = await pair.source(context(prefix + '|', true));
      assert.equal(result.options.some(o => o.label === word), present, `${engine} ${version} ${word}`);
      if (present) assert.equal(result.options.find(o => o.label === word && o.type === 'keyword').detail, detail);
    }
    const pair = sourceEnv(); Object.assign(pair.env.current, { connection: undefined, engine: 'mysql', serverVersion: '8.4.12' });
    assert.equal((await pair.source(context('QUAL|', true))).options.find(o => o.label === 'QUALIFY').detail,
      'reserved, MySQL 8.4.11 list');
    assert.equal((await pair.source(context('BUL|', true))).options.find(o => o.label === 'BULK').detail,
      'MySQL 8.4.11 list', 'a non-reserved fallback keyword shows only its release');
    pair.env.current.serverVersion = 'unknown';
    const fallback = (await pair.source(context('SEL|', true))).options[0];
    assert.equal(fallback.label, 'SELECT'); assert.equal(fallback.detail, '');
    assert.equal(sent.length, 0);
  });
  await test('function suggestions use generated counts and release details only in unquoted expressions', async () => {
    const pair = sourceEnv(); Object.assign(pair.env.current, { connection: undefined, engine: 'mysql', serverVersion: '8.0.36' });
    for (const [name, count] of [['PI', '0 arguments'], ['POW', '2 arguments'], ['ABS', '1 argument'], ['ATAN', '1 to 2 arguments'], ['CONCAT', 'at least 1 argument'], ['COUNT', '']]) {
      assert.ok(cm.functionsFor('mysql', '8.0.36').functions.some(fn => fn.name === name));
      const result = await pair.source(context(`SELECT ${name}|`, true));
      const fn = result.options.find(o => o.label === name && o.type === 'function');
      assert.ok(fn, name); assert.equal(fn.detail, count);
      assert.equal(typeof fn.apply, 'function');
    }
    const overlap = await pair.source(context('SELECT REVERSE|', true));
    assert.deepEqual(overlap.options.filter(o => o.label === 'REVERSE').map(o => o.type), ['keyword', 'function'], 'deduplication keeps keyword and function entries');
    for (const marked of ['SELECT * FROM POW|', 'SELECT app.POW|', 'SELECT `POW|']) {
      const result = await pair.source(context(marked, true));
      assert.ok(!result.options.some(o => o.type === 'function'), marked);
    }
    pair.env.current.serverVersion = '8.0.99';
    const result = await pair.source(context('SELECT POW|', true));
    assert.equal(result.options.find(o => o.type === 'function' && o.label === 'POW').detail, '2 arguments, MySQL 8.0.46 list');
    assert.equal((await pair.source(context('SELECT COUNT|', true))).options.find(o => o.type === 'function' && o.label === 'COUNT').detail,
      'MySQL 8.0.46 list', 'unknown counts keep the release without a leading comma');
    assert.equal(sent.length, 0);
  });
  await test('function apply keeps every matching cursor inside parentheses and undoes in one step', async () => {
    const pair = sourceEnv(); Object.assign(pair.env.current, { connection: undefined, engine: 'mysql', serverVersion: '8.0.36' });
    for (const [marked, multiple, expected, heads] of [
      ['SELECT PO|', false, 'SELECT POW()', [11]],
      ['SELECT PO|(1, 2)', false, 'SELECT POW(1, 2)', [10]],
      ['SELECT PO|, PO', true, 'SELECT POW(), POW()', [11, 18]],
    ]) {
      const ctx = context(marked, true);
      let state = cm.EditorState.create({ doc: ctx.state.doc,
        selection: multiple ? cm.EditorSelection.create([cm.EditorSelection.cursor(9), cm.EditorSelection.cursor(13)]) : ctx.state.selection,
        extensions: [cm.EditorState.allowMultipleSelections.of(true), cm.history()] });
      const initial = state.doc.toString(), initialSelection = state.selection, transactions = [];
      const view = { get state() { return state; }, dispatch(spec) { const tr = state.update(spec); state = tr.state; transactions.push(tr); } };
      const result = await pair.source(new cm.CompletionContext(state, state.selection.main.head, true));
      const fn = result.options.find(o => o.label === 'POW' && o.type === 'function'); assert.ok(fn);
      fn.apply(view, fn, result.from, result.to);
      assert.equal(state.doc.toString(), expected);
      assert.deepEqual(state.selection.ranges.map(r => r.head), heads);
      assert.equal(transactions.length, 1, 'insertion and caret movement are one transaction');
      assert.equal(transactions[0].annotation(cm.pickedCompletion), fn);
      assert.equal(transactions[0].annotation(cm.Transaction.userEvent), 'input.complete');
      assert.equal(cm.undo(view), true); assert.equal(state.doc.toString(), initial);
      assert.ok(state.selection.eq(initialSelection)); assert.equal(cm.undo(view), false);
    }
    // One mismatched prefix means only the main cursor completes. Other cursors
    // stay present and map through the insertion without moving into parentheses.
    const doc = 'SELECT PO, XX, PO';
    let state = cm.EditorState.create({ doc,
      selection: cm.EditorSelection.create([9, 13, 17].map(pos => cm.EditorSelection.cursor(pos)), 2),
      extensions: [cm.EditorState.allowMultipleSelections.of(true), cm.history()] });
    const result = await pair.source(new cm.CompletionContext(state, state.selection.main.head, true));
    const fn = result.options.find(o => o.label === 'POW' && o.type === 'function'); assert.ok(fn);
    const view = { get state() { return state; }, dispatch(spec) { state = state.update(spec).state; } };
    fn.apply(view, fn, result.from, result.to);
    assert.equal(state.doc.toString(), 'SELECT PO, XX, POW()');
    assert.deepEqual(state.selection.ranges.map(r => r.head), [9, 13, 19]);
    assert.equal(state.selection.mainIndex, 2);
    assert.equal(cm.undo(view), true); assert.equal(state.doc.toString(), doc);
  });
  await test('function parentheses and carets follow each matching cursor, in either main order', async () => {
    const pair = sourceEnv(); Object.assign(pair.env.current, { connection: undefined, engine: 'mysql', serverVersion: '8.0.36' });
    for (const mainIndex of [0, 1]) {
      const doc = 'SELECT PO, PO(1, 2)';
      let state = cm.EditorState.create({ doc,
        selection: cm.EditorSelection.create([9, 13].map(pos => cm.EditorSelection.cursor(pos)), mainIndex),
        extensions: [cm.EditorState.allowMultipleSelections.of(true), cm.history()] });
      const initialSelection = state.selection, transactions = [];
      const view = { get state() { return state; }, dispatch(spec) {
        const tr = state.update(spec); state = tr.state; transactions.push(tr);
      } };
      const result = await pair.source(new cm.CompletionContext(state, state.selection.main.head, true));
      const fn = result.options.find(o => o.label === 'POW' && o.type === 'function'); assert.ok(fn);
      fn.apply(view, fn, result.from, result.to);
      assert.equal(state.doc.toString(), 'SELECT POW(), POW(1, 2)', `main cursor ${mainIndex}`);
      assert.deepEqual(state.selection.ranges.map(r => r.head), [11, 17]);
      assert.equal(state.selection.mainIndex, mainIndex);
      assert.equal(transactions.length, 1);
      assert.equal(transactions[0].annotation(cm.pickedCompletion), fn);
      assert.equal(transactions[0].annotation(cm.Transaction.userEvent), 'input.complete');
      assert.equal(cm.undo(view), true); assert.equal(state.doc.toString(), doc);
      assert.ok(state.selection.eq(initialSelection)); assert.equal(cm.undo(view), false);
    }
  });
  await test('variable lexical guards, scopes, replacement and one cached offline call', async () => {
    const pair = sourceEnv(); Object.assign(pair.env.current, { connection: undefined, engine: 'mysql', serverVersion: '8.4.6' });
    const names = [
      { name: 'autocommit', scope: 'both', type: 'boolean', dynamic: true, deprecated: false },
      { name: 'version', scope: 'global', type: 'string', dynamic: false, deprecated: false },
      { name: 'sql_log_bin', scope: 'session', type: null, dynamic: null, deprecated: true },
    ];
    handler = () => Promise.resolve({ schema_version: 'param-names-1.0', engine: 'mysql', requested: '8.4.6', release: '8.4.6', exact: true, names });
    for (const [marked, expected] of [
      ['SELECT @@|', ['autocommit', 'version', 'sql_log_bin']],
      ['SELECT @@global.|', ['autocommit', 'version']],
      ['SELECT @@session.|', ['autocommit', 'sql_log_bin']],
      ['SELECT @@LOCAL.|', ['autocommit', 'sql_log_bin']],
    ]) {
      const result = await pair.source(context(marked, true));
      assert.deepEqual(result.options.map(o => o.label), expected);
      assert.equal(result.options[0].detail, 'global, session');
      if (expected.includes('sql_log_bin')) assert.equal(result.options.at(-1).detail, 'session, deprecated');
    }
    assert.equal(sent.length, 1); assert.equal(sent[0].path, 'config/versions/names');
    assert.deepEqual(sent[0].body, { engine: 'mysql', version: '8.4.6' });
    const middle = await pair.source(context('SELECT @@session.auto|commit', true));
    assert.deepEqual([middle.from, middle.to, middle.options[0].apply], [17, 27, 'autocommit']);
    const fuzzy = await pair.source(context('SELECT @@sllb|', true));
    assert.equal(fuzzy.filter, true); assert.equal(fuzzy.options[0].label, 'sql_log_bin');
    for (const marked of ["SELECT '@@ve|'", 'SELECT /* @@ve| */', 'SELECT -- @@ve|', 'SELECT `@@ve|`', 'SELECT @ve|', 'SELECT @@@ve|', 'DELIMITER $$\nSELECT @@ve|']) {
      const pos = marked.indexOf('|');
      assert.equal(cm.variableContext(marked.replace('|', ''), pos), null, marked);
    }
    assert.equal(sent.length, 1);
    pair.env.current.serverVersion = '8.0.36'; await pair.source(context('SELECT @@|', true));
    assert.equal(sent.length, 2, 'release change reads the matching names catalog');
    pair.env.current.serverVersion = '8.4.6'; await pair.source(context('SELECT @@|', true));
    assert.equal(sent.length, 2, 'returning to a release reuses its immutable catalog');
  });
  await test('variable abort, timeout and automatic triggers use no live catalog', async () => {
    const pair = sourceEnv(); Object.assign(pair.env.current, { connection: undefined, engine: 'mysql', serverVersion: '8.4.6' });
    handler = () => new Promise(() => {});
    const ctx = context('SELECT @@|', true), pending = pair.source(ctx); await flush();
    assert.equal(sent.length, 1);
    for (const listener of ctx.abortListeners) listener();
    assert.equal(await pending, null); assert.equal(sent[0].signal.aborted, true);
    const timed = pair.source(context('SELECT @@session.|')); await advance(220);
    assert.equal(sent.length, 2); await advance(7780);
    assert.equal((await timed).note, 'System variable suggestions timed out. Try Ctrl+Space again.');
    assert.equal(sent[1].signal.aborted, true);
  });
  await test('CTEs are local table suggestions with scope, quoting and no stored table guesses', async () => {
    const pair = sourceEnv(); pair.env.current.connection = undefined;
    const result = await pair.source(context('WITH a AS (SELECT 1), b AS (SELECT 2) SELECT * FROM |', true));
    assert.deepEqual(result.options.map(o => [o.label, o.type, o.detail]), [['a', 'table', 'CTE'], ['b', 'table', 'CTE']]);
    for (const [marked, expected] of [
      ['WITH `a``b`(id) AS (SELECT 1) SELECT * FROM |', ['a`b']],
      ['WITH a AS (SELECT 1) SELECT 1; SELECT * FROM |', []],
      ['WITH a AS (SELECT 1), b AS (SELECT * FROM |) SELECT 1', ['a']],
      ['WITH RECURSIVE a AS (SELECT * FROM |) SELECT 1', ['a']],
      ['SELECT * FROM (WITH inner_cte AS (SELECT 1) SELECT * FROM inner_cte) t JOIN |', []],
      ['WITH outer_cte AS (SELECT 1) SELECT * FROM (WITH inner_cte AS (SELECT 2) SELECT * FROM |) t', ['outer_cte', 'inner_cte']],
    ]) {
      const pos = marked.indexOf('|'); assert.deepEqual(cm.cteNames(marked.replace('|', ''), pos), expected, marked);
    }
    assert.equal(sent.length, 0);
  });
  await test('complete inventories use fuzzy candidates, prefix first, with a 40 item bound', async () => {
    const pair = sourceEnv();
    handler = () => Promise.resolve(columns(['users', 'usr_exact', 'unrelated', ...Array.from({ length: 50 }, (_, i) => `usr_${i}`)]));
    const result = await pair.source(context('SELECT app.people.usr|', true));
    assert.equal(result.options.length, 40); assert.equal(result.options[0].label, 'usr_exact');
    assert.ok(result.options.every(o => o.label.startsWith('usr')), 'prefix results fill the cap before fuzzy results');
    handler = () => Promise.resolve(columns(['users', 'usr_exact']));
    const other = sourceEnv();
    const fuzzy = await other.source(context('SELECT app.people.usr|', true));
    assert.equal(fuzzy.filter, true); assert.deepEqual(fuzzy.options.map(o => o.label), ['usr_exact', 'users']);
    assert.ok(fuzzy.options[0].boost > fuzzy.options[1].boost);
    const alias = sourceEnv(); alias.env.current.connection = undefined;
    assert.ok((await alias.source(context('SELECT usr| FROM people users', true))).options.some(o => o.type === 'alias' && o.label === 'users'));
    assert.ok((await alias.source(context('WITH users AS (SELECT 1) SELECT * FROM usr|', true))).options.some(o => o.label === 'users'));
  });
  await test('200-table schemas read the full list once; larger or truncated inventories stay prefix-only', async () => {
    for (const count of [200, 201]) {
      const pair = sourceEnv();
      handler = path => Promise.resolve(path === 'live/schemas' ? { schemas: [{ name: 'app', tables: count }] } : tables(['users', 'usr_exact'], count > 200));
      const before = sent.length, result = await pair.source(context('SELECT * FROM usr|', true));
      assert.deepEqual(sent[before + 1].body, { connection: conn(), schema: 'app', ...(count > 200 ? { prefix: 'usr' } : {}) });
      assert.deepEqual(result.options.map(o => o.label), count > 200 ? ['usr_exact'] : ['usr_exact', 'users']);
      assert.equal(result.note, count > 200 ? 'More tables match. Keep typing to narrow the list.' : '');
      if (count === 200) {
        await pair.source(context('SELECT * FROM user|', true));
        assert.equal(sent.length, before + 2, 'full schema inventory is reused across prefixes');
      }
    }
    const pair = sourceEnv(); handler = path => Promise.resolve(path === 'live/schemas' ? { schemas: [{ name: 'app', tables: 200 }] } : tables(['users'], true));
    assert.deepEqual((await pair.source(context('SELECT * FROM usr|', true))).options, [], 'truncated full read cannot enable fuzzy tables');
  });
  await test('a schema growing beyond 200 tables refreshes by prefix despite a stale complete list', async () => {
    const pair = sourceEnv();
    handler = path => Promise.resolve(path === 'live/schemas' ? { schemas: [{ name: 'app', tables: 1 }] } : tables(['users']));
    await pair.source(context('SELECT * FROM u|', true)); await advance(30000);
    handler = path => Promise.resolve(path === 'live/schemas' ? { schemas: [{ name: 'app', tables: 201 }] } : tables(['user_new']));
    const result = await pair.source(context('SELECT * FROM user|', true));
    assert.equal(sent.at(-1).body.prefix, 'user', 'stale completeness cannot override the current schema size');
    assert.equal(result.filter, false);
    assert.equal((await pair.localSource(context('SELECT * FROM user|'))).filter, false, 'the fresh prefix result stays prefix-only');
  });
  await test('engine-aware catalog quoting covers reserved and non-reserved keywords and hostile names', async () => {
    const pair = sourceEnv(); Object.assign(pair.env.current, { engine: 'mysql', serverVersion: '8.4.6' });
    assert.deepEqual(cm.wordsFor('mysql', '8.4.6').keywords.find(k => k.name === 'BULK'), { name: 'BULK', reserved: false });
    handler = () => Promise.resolve(columns(['select', 'bulk', 'a b', 'a`b', 'ordinary']));
    const result = await pair.source(context('SELECT app.people.|', true));
    assert.deepEqual(result.options.map(o => o.label), ['select', 'bulk', 'a b', 'a`b', 'ordinary']);
    assert.deepEqual(result.options.map(o => o.apply), ['`select`', '`bulk`', '`a b`', '`a``b`', 'ordinary']);
    assert.ok(result.options.every(o => typeof o.apply === 'string'));
  });
  await test('native aria-expanded follows visible lists while remote reads are pending', async () => {
    const pair = sourceEnv(), held = deferred(), live = await nativeEditor(pair, '|');
    handler = () => held.promise;
    // Keep the same facet callback across completion-only transactions, as the
    // view does. Opening or closing must not depend on editing the document.
    const attributes = live.view.state.facet(cm.EditorView.contentAttributes)
      .find(value => typeof value === 'function' && 'aria-expanded' in value(live.view));
    assert.equal(typeof attributes, 'function');
    const expanded = () => attributes(live.view)['aria-expanded'];
    assert.equal(expanded(), 'false');
    live.view.dispatch({ changes: { from: 0, insert: 'SEL' }, selection: { anchor: 3 }, userEvent: 'input.type' });
    const doc = live.view.state.doc;
    assert.equal(cm.completionStatus(live.view.state), 'pending');
    assert.deepEqual(cm.currentCompletions(live.view.state), []);
    assert.equal(expanded(), 'false', 'pending without a displayed list stays collapsed');
    await advance(50);
    assert.equal(live.view.state.doc, doc);
    assert.equal(cm.completionStatus(live.view.state), 'pending');
    assert.ok(cm.currentCompletions(live.view.state).some(o => o.label === 'SELECT'));
    assert.equal(expanded(), 'true', 'local list is expanded while the remote lane is pending');
    live.escape();
    assert.equal(live.view.state.doc, doc); assert.equal(expanded(), 'false');
    cm.startCompletion(live.view); await advance(50);
    assert.equal(live.view.state.doc, doc); assert.equal(expanded(), 'true');
    live.view.dispatch({ changes: { from: 3, insert: 'Z' }, selection: { anchor: 4 }, userEvent: 'input.type' });
    await advance(220);
    assert.equal(cm.completionStatus(live.view.state), 'pending');
    assert.deepEqual(cm.currentCompletions(live.view.state), [], 'the retained list is disabled during the read');
    assert.equal(expanded(), 'true', 'a disabled but displayed list is still expanded');
    const pendingDoc = live.view.state.doc;
    held.resolve(schemas([])); await advance(0);
    assert.equal(live.view.state.doc, pendingDoc);
    assert.equal(cm.completionStatus(live.view.state), null);
    assert.equal(expanded(), 'false', 'an empty remote result closes the list without an edit');
    live.destroy();
  });
  await test('native matcher ranks prefixes before fuzzy columns and toggles editor aria-expanded', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, 'SELECT app.people.usr|');
    handler = () => Promise.resolve(columns(['users', 'usr_exact']));
    const expanded = () => live.view.state.facet(cm.EditorView.contentAttributes)
      .map(value => typeof value === 'function' ? value(live.view) : value).find(value => 'aria-expanded' in value)['aria-expanded'];
    assert.equal(expanded(), 'false'); cm.startCompletion(live.view); assert.equal(expanded(), 'false');
    await advance(100);
    assert.deepEqual(cm.currentCompletions(live.view.state).map(o => o.label), ['usr_exact', 'users']);
    assert.equal(expanded(), 'true'); live.escape(); assert.equal(expanded(), 'false'); live.destroy();
  });

  await test('native fuzzy matching inside backticks keeps raw labels and string insertion', async () => {
    const pair = sourceEnv(), live = await nativeEditor(pair, 'SELECT app.people.`usr|');
    handler = () => Promise.resolve(columns(['users', 'usr_exact']));
    cm.startCompletion(live.view); await advance(100);
    assert.deepEqual(cm.currentCompletions(live.view.state).map(o => o.label), ['usr_exact', 'users']);
    live.destroy();
    const closed = sourceEnv(); handler = () => Promise.resolve(columns(['users', 'usr_exact']));
    const closedResult = await closed.source(context('SELECT app.people.`usr|`', true));
    assert.deepEqual(closedResult.options.map(o => o.label), ['usr_exact']);
    assert.equal(closedResult.filter, false, 'a closing quote in the replacement range is not a fuzzy search character');
    const other = sourceEnv(); handler = () => Promise.resolve(columns(['name']));
    const marked = 'SELECT `na|` FROM app.users u JOIN app.roles r ON u.id=r.id';
    const result = await other.source(context(marked, true));
    assert.deepEqual(result.options.map(o => o.apply), ['u`.`name`', 'r`.`name`']);
    assert.equal(marked.replace('|', '').slice(0, result.from) + result.options[0].apply + marked.replace('|', '').slice(result.to),
      'SELECT `u`.`name` FROM app.users u JOIN app.roles r ON u.id=r.id');
    const escaped = sourceEnv(); handler = () => Promise.resolve(columns(['a`b']));
    const quoted = await escaped.source(context('SELECT app.people.`a``|`', true));
    assert.equal(quoted.filter, false); assert.equal(quoted.options[0].apply, 'a``b`');
  });
  console.log(`${passed} completion source and catalog tests passed.`);
} finally {
  globalThis.setTimeout = original.setTimeout; globalThis.clearTimeout = original.clearTimeout; Date.now = original.now;
  delete globalThis.__catalogApi;
}
