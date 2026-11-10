// Real EditorState/CompletionContext, fake API and clock. No DOM or server.
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
const root = fileURLToPath(new URL('..', import.meta.url));
const built = await build({ stdin: { resolveDir: root, contents: `
  export * from './src/editor/catalog'; export * from './src/editor/completion';
  export {EditorState, EditorSelection} from '@codemirror/state';
  export {CompletionContext, autocompletion, startCompletion, closeCompletion, completionStatus, currentCompletions} from '@codemirror/autocomplete';
` }, bundle: true, format: 'esm', platform: 'neutral', target: 'es2020', write: false, logLevel: 'silent',
  define: { 'process.env.NODE_ENV': '"production"' }, plugins: [{ name: 'api', setup(b) {
    b.onResolve({ filter: /^(\.\/|\.\.\/)api$/ }, () => ({ path: 'api', namespace: 'stub' }));
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({ contents: `export class ApiError extends Error {}
      export const api = (path, body, signal) => globalThis.__catalogApi(path, body, signal);` }));
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
  const source = cm.completionSources(env)[0]; resources.push(cm.catalogFor(env)); return { env, source };
}
function context(marked, explicit = false, selection) {
  const pos = marked.indexOf('|'), doc = marked.replace('|', '');
  const state = cm.EditorState.create({ doc, selection: selection || { anchor: pos }, extensions: [cm.EditorState.allowMultipleSelections.of(true)] });
  return new cm.CompletionContext(state, pos, explicit);
}
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
// Exercise the app's lifecycle plugin with real transactions but without a DOM.
// CodeMirror's own tooltip rendering remains a browser-suite gate.
async function editor(pair, marked, automatic = true) {
  const extension = cm.completionExtension({ completion_auto: automatic }, pair.env);
  const lifecycle = extension.find(value => value.baseExtensions && !value.domEventHandlers);
  const handlers = extension.find(value => value.domEventHandlers?.compositionstart).domEventHandlers;
  const observers = extension.find(value => value.domEventObservers?.keydown).domEventObservers;
  const initial = context(marked);
  let state = cm.EditorState.create({ doc: initial.state.doc, selection: initial.state.selection,
    extensions: [cm.autocompletion({ override: [pair.source], activateOnTyping: automatic, defaultKeymap: false }), extension] });
  let plugin;
  const view = { hasFocus: true, composing: false, get state() { return state; }, dispatch(spec) {
    const before = state, tr = state.update(spec); state = tr.state;
    plugin?.update({ view, startState: before, state, docChanged: tr.docChanged, selectionSet: !!tr.selection, transactions: [tr] });
  } };
  plugin = lifecycle.create(view); await flush();
  return { view, handlers, observers, destroy: () => plugin.destroy(),
    query: (explicit = false) => pair.source(new cm.CompletionContext(state, state.selection.main.head, explicit, view)) };
}
async function nativeEditor(pair, marked, restoredPending = false) {
  const extension = cm.completionExtension({ completion_auto: true }, pair.env);
  const nativeExtension = cm.autocompletion({ override: [pair.source], defaultKeymap: false });
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
  return { view, escape() { observer.keydown({ key: 'Escape' }, view); cm.closeCompletion(view); }, destroy() {
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
  await test('automatic threshold and explicit local completion', async () => {
    const { source, env } = sourceEnv(); env.current.connection = undefined;
    assert.equal(await source(context('S|')), null); assert.equal(await source(context('|')), null);
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
    await catalog.read('schemas', {}, owner()); assert.equal(sent.length, 2);
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
  await test('32 entry FIFO cap; cache hits do not reorder', async () => {
    const catalog = service(); handler = () => Promise.resolve(columns(['id']));
    for (let i = 0; i < 32; i++) await catalog.read('columns', { schema: 'app', table: `t${i}` }, owner());
    await catalog.read('columns', { schema: 'app', table: 't0' }, owner()); assert.equal(sent.length, 32);
    await catalog.read('columns', { schema: 'app', table: 't32' }, owner()); assert.equal(sent.length, 33);
    await catalog.read('columns', { schema: 'app', table: 't1' }, owner()); assert.equal(sent.length, 33);
    await catalog.read('columns', { schema: 'app', table: 't0' }, owner()); assert.equal(sent.length, 34);
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
    assert.equal(await source(context('S|')), null); assert.equal(await first, null); await advance(1000); assert.equal(sent.length, 0);
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
  await test('live source publishes local words and Reading catalog before metadata', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SE|'), held = deferred();
    handler = () => held.promise;
    const first = await live.query(); await flush();
    assert.equal(first.options[0].label, 'SELECT'); assert.equal(sent.length, 0);
    assert.equal(live.view.state.field(cm.completionHint), 'Reading catalog…');
    await advance(220); assert.equal(sent.length, 1);
    held.resolve(schemas(['SE_database'])); await flush();
    assert.equal(cm.completionStatus(live.view.state), 'pending', 'catalog arrival asks CM to refresh the one source');
    const refreshed = await live.query(true);
    assert.deepEqual(refreshed.options.map(o => o.label), ['SE_database', 'SELECT', 'SET']);
    assert.equal(sent.length, 1, 'internal refresh does not start another request');
    assert.equal(live.view.state.field(cm.completionHint), ''); live.destroy();
  });
  await test('no-option loading, failure and timeout notes survive without a list', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SELECT * FROM zz|');
    handler = () => Promise.reject(new Error('denied'));
    assert.deepEqual((await live.query()).options, []); await flush();
    assert.equal(live.view.state.field(cm.completionHint), 'Reading catalog…');
    await advance(220);
    assert.equal(live.view.state.field(cm.completionHint), 'Some catalog suggestions are unavailable.');
    assert.deepEqual((await live.query(true)).options, []);
    live.observers.keydown({ key: 'Escape' }, live.view); await flush();
    assert.equal(live.view.state.field(cm.completionHint), '');
    handler = () => new Promise(() => {}); await live.query(true); await advance(8000);
    assert.equal(live.view.state.field(cm.completionHint), 'Catalog suggestions timed out. Try Ctrl+Space again.'); live.destroy();
  });
  await test('composition closes completion, aborts reads, blocks source until end', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SE|'); handler = () => new Promise(() => {});
    cm.startCompletion(live.view); await live.query(true); await flush();
    live.handlers.compositionstart({}, live.view); await flush();
    assert.equal(sent[0].signal.aborted, true); assert.equal(cm.completionStatus(live.view.state), null);
    assert.equal(await live.query(true), null); assert.equal(live.view.state.field(cm.completionHint), '');
    live.handlers.compositionend({}, live.view); handler = () => Promise.resolve(schemas(['app']));
    assert.equal((await live.query(true)).options[0].label, 'SELECT'); await flush(); live.destroy();
  });
  await test('edits, selection, environment switch and destroy cancel live work', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SE|'); handler = () => new Promise(() => {});
    await live.query(true); await flush();
    live.view.dispatch({ changes: { from: 2, insert: 'L' }, selection: { anchor: 3 }, userEvent: 'input.type' });
    assert.equal(sent[0].signal.aborted, true);
    live.observers.keydown({ ctrlKey: true, key: ' ' }, live.view);
    await live.query(true); await flush(); live.view.dispatch({ selection: { anchor: 2 } });
    assert.equal(sent[1].signal.aborted, true);
    await live.query(true); await flush(); pair.env.current.connection = { ...conn(), password: 'new' }; live.view.dispatch({});
    assert.equal(sent[2].signal.aborted, true);
    await live.query(true); await flush(); live.destroy(); assert.equal(sent[3].signal.aborted, true);
  });
  await test('alias targets preserve table order despite reverse response order', async () => {
    const { source } = sourceEnv(), first = deferred(), second = deferred();
    handler = (_path, body) => body.table === 'users' ? first.promise : second.promise;
    const pending = source(context('SELECT na| FROM app.users u JOIN app.roles r ON u.id=r.id', true));
    await flush(); assert.deepEqual(sent.map(c => c.body.table), ['users', 'roles']);
    second.resolve(columns(['name_role'])); await flush(); first.resolve(columns(['name_user']));
    assert.deepEqual((await pending).options.map(o => o.apply), ['u.name_user', 'r.name_role']);
  });
  await test('typing after a public refresh cannot inherit explicit status or skip debounce', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SELECT * FROM us|');
    await live.query(true); await flush(); await live.query(true); assert.equal(sent.length, 2);
    const pos = live.view.state.selection.main.head;
    live.view.dispatch({ changes: { from: pos, insert: 'e' }, selection: { anchor: pos + 1 }, userEvent: 'input.type' });
    // CM preserves the old result's explicit bit on typing (autocomplete:1023).
    await live.query(true); await flush();
    assert.equal(sent.length, 2, 'typing must not become an explicit catalog request');
    await advance(219); assert.equal(sent.length, 2); await advance(1); assert.equal(sent.length, 3);
    await live.query(true); await flush();
    const end = live.view.state.selection.main.head;
    live.view.dispatch({ changes: { from: end, insert: 'r' }, selection: { anchor: end + 1 }, userEvent: 'input.type' });
    live.observers.keydown({ ctrlKey: true, shiftKey: true, key: ' ' }, live.view);
    await live.query(true); await flush(); assert.equal(sent.length, 4, 'Shift-Ctrl-Space is explicit after typing');
    live.destroy();
  });
  await test('inherited explicit status cannot bypass a failure, but either shortcut can', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SE|'); handler = () => Promise.reject(new Error('denied'));
    await live.query(true); await flush(); await live.query(true); assert.equal(sent.length, 1);
    live.view.dispatch({ changes: { from: 2, insert: 'L' }, selection: { anchor: 3 }, userEvent: 'input.type' });
    await live.query(true); await advance(220);
    assert.equal(sent.length, 1, 'typing respects the cached schema failure');
    await live.query(true); // Consume only the internal result refresh.
    for (const shiftKey of [false, true]) {
      live.observers.keydown({ ctrlKey: true, shiftKey, code: 'Space' }, live.view);
      await live.query(true); await flush(); await live.query(true);
    }
    assert.equal(sent.length, 3, 'Ctrl-Space and Shift-Ctrl-Space each retry'); live.destroy();
  });
  await test('switching credentials or disabling automatic suggestions closes the CM session', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SE|');
    cm.startCompletion(live.view); assert.equal(cm.completionStatus(live.view.state), 'pending');
    pair.env.current.connection = { ...conn(), password: 'changed' }; live.view.dispatch({}); await flush();
    assert.equal(cm.completionStatus(live.view.state), null);
    cm.startCompletion(live.view);
    cm.completionExtension({ completion_auto: false }, pair.env); live.view.dispatch({}); await flush();
    assert.equal(cm.completionStatus(live.view.state), null); assert.equal(await live.query(false), null);
    live.observers.keydown({ ctrlKey: true, key: ' ' }, live.view);
    assert.equal((await live.query(true)).options[0].label, 'SELECT'); await flush(); live.destroy();
  });
  await test('manual retry wins over a catalog refresh waiting in CM debounce', async () => {
    const pair = sourceEnv(), live = await editor(pair, 'SE|'); handler = () => Promise.reject(new Error('denied'));
    await live.query(true); await flush(); assert.equal(sent.length, 1);
    // The failed result is ready, but CM has not called the source again yet.
    live.observers.keydown({ ctrlKey: true, key: ' ' }, live.view);
    handler = () => Promise.resolve(schemas(['SE_database']));
    await live.query(true); await flush();
    assert.equal(sent.length, 2, 'manual retry must not be consumed as an internal refresh');
    const result = await live.query(true); assert.equal(result.options[0].label, 'SE_database'); live.destroy();
  });
  await test('native scheduler: 50 ms manual start/refresh, then 220 ms automatic debounce', async () => {
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
  console.log(`${passed} completion source and catalog tests passed.`);
} finally {
  globalThis.setTimeout = original.setTimeout; globalThis.clearTimeout = original.clearTimeout; Date.now = original.now;
  delete globalThis.__catalogApi;
}
