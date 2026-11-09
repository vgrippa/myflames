// Execute the actual TypeScript hook/callback bodies without sockets. Browser
// integration is in startup-reads.mjs and sessions.mjs.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
const source = name => {
  const text = fs.readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
  return ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
};
function find(root, predicate) {
  if (predicate(root)) return root;
  return ts.forEachChild(root, node => find(node, predicate));
}
const named = (root, name) => find(root, n => ts.isFunctionDeclaration(n) && n.name?.text === name);
const variable = (root, name) => find(root, n => ts.isVariableDeclaration(n) && n.name.getText() === name);
function run(text, context) {
  const code = ts.transpileModule(text.replace(/\bexport /g, ""), { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.None, jsx: ts.JsxEmit.React } }).outputText;
  return vm.runInNewContext(code, context);
}
// Hook storage preserves React's dependency/ref semantics, with effects flushed
// after render. State changes are observed on the next explicit render.
function hooks() {
  const values = [], deps = [], cleanups = [];
  let cursor = 0, effects = [];
  const changed = (i, next) => !next || !deps[i] || next.some((x, j) => !Object.is(x, deps[i][j]));
  return {
    render(fn) { cursor = 0; effects = []; const value = fn(); for (const effect of effects) effect(); return value; },
    useRef(value) { const i = cursor++; return values[i] ||= { current: value }; },
    useState(value) { const i = cursor++; if (!(i in values)) values[i] = value; return [values[i], next => { values[i] = typeof next === 'function' ? next(values[i]) : next; }]; },
    useCallback(fn, next) { const i = cursor++; if (changed(i, next)) { values[i] = fn; deps[i] = next; } return values[i]; },
    useEffect(fn, next) { const i = cursor++; if (changed(i, next)) { deps[i] = next; effects.push(() => { cleanups[i]?.(); cleanups[i] = fn(); }); } },
    close() { for (const cleanup of cleanups) cleanup?.(); },
  };
}
let cases = 0;
const hook = hooks(), requests = [];
const Provider = run(`(${named(source('privileges.ts'), 'PrivilegesProvider').getText()})`, {
  ...hook, connectionKey: c => c.host, AbortController, Date,
  PrivilegesContext: { Provider: {} }, createElement: (_, props) => props.value,
  sharedRead: (_, __, read) => read(new AbortController().signal),
  readPrivileges: async c => { requests.push(c.password); return { actions: {} }; },
  window: { addEventListener() {}, removeEventListener() {} },
});
let props = { connection: { host: 'mock', password: '' }, connectRevision: 0, passwordRevision: 0 };
let value = hook.render(() => Provider(props));
await Promise.resolve();
assert.deepEqual(requests, ['']); cases++;
requests.length = 0;
for (const password of ['s', 'se', 'sec', 'secr', 'secre', 'secret']) {
  props = { ...props, connection: { ...props.connection, password } };
  value = hook.render(() => Provider(props));
  await value.refresh(); // Focus/manual incidental reads also cannot send a draft.
}
assert.deepEqual(requests, []); cases++;
props = { ...props, passwordRevision: 1 };
value = hook.render(() => Provider(props));
await Promise.resolve();
assert.deepEqual(requests, ['secret']); cases++;
hook.render(() => Provider(props));
assert.deepEqual(requests, ['secret']); cases++;
props = { ...props, connectRevision: 1 };
hook.render(() => Provider(props));
await Promise.resolve();
assert.deepEqual(requests, ['secret', 'secret']); cases++;
hook.close();

// The same commit value on Enter and blur increments its revision only once.
const committedPassword = { current: '' }, commits = [];
const commit = run(`(${named(source('Connections.tsx'), 'commitPassword').getText()})`, {
  committedPassword, active: { password: 'secret' }, onPasswordCommit: () => commits.push(true),
});
commit(); commit(); assert.equal(commits.length, 1); cases++;

const screen = { current: true }, openHook = hooks();
const openRead = run(`(${named(source('viewOnScreen.ts'), 'useOpenRead').getText()})`, { ...openHook, useViewOnScreen: () => screen.current });
const reads = [];
let input = { key: 'a', reportKey: null, ready: true, busy: false, read: () => reads.push(input.key) };
const render = () => openHook.render(() => openRead(input));
render(); render(); assert.deepEqual(reads, ['a']); cases++;
input = { ...input, reportKey: 'a', key: 'b' }; render();
assert.deepEqual(reads, ['a'], 'on-screen switch keeps old report'); cases++;
screen.current = false; render(); screen.current = true; render();
assert.deepEqual(reads, ['a', 'b'], 'reopening reads the selected connection'); cases++;
render(); assert.deepEqual(reads, ['a', 'b'], 'failure does not retry itself'); cases++;
openHook.close();

// Each view hands the identical cancellation signal to the actual api call.
for (const [file, component] of [['Waits.tsx', 'WaitsView'], ['InnoDB.tsx', 'InnoDBView'], ['Locks.tsx', 'LocksView'], ['Sessions.tsx', 'SessionsView']]) {
  const tree = source(file);
  const owner = named(tree, component) || tree;
  const loader = named(owner, 'load');
  assert.ok(loader, file);
  const runCall = find(loader, n => ts.isCallExpression(n) && n.expression.getText() === 'reads.run');
  const signal = new AbortController().signal, calls = [];
  const callback = run(`(${runCall.arguments[2].getText()})`, {
    interval: 10, raw: false, waitsOnly: false, activeOnly: true, minSeconds: 0, source: 'auto', lockedOnly: false,
    api: (...args) => { calls.push(args); return Promise.resolve({}); },
  });
  await callback({ host: 'mock' }, signal);
  assert.equal(calls[0][2], signal, `${file} forwards cancellation`); cases++;
}

const kill = named(source('MassKillDialog.tsx'), 'MassKillDialog');
const compile = variable(kill, 'queryPattern').initializer.arguments[0].getText();
const filter = variable(kill, 'matchedSessions').initializer.arguments[0].getText();
const clientSessions = ['SELECTx', 'SELECT x', '12', '[literal', ' SELECT'].map((info, id) => ({ id, info, command: 'Query' }));
for (const [pattern, expected] of [[String.raw`^SELECT\S`, [0]], [String.raw`^\D+$`, [0, 1, 3, 4]], [String.raw`^\W`, [3, 4]], [String.raw`\BSELECT`, []], [' SELECT', [4]], ['[', []], ['', [0, 1, 2, 3, 4]]]) {
  const queryPattern = run(`(${compile})()`, { matchInfo: pattern });
  const matched = run(`(${filter})()`, { queryPattern, clientSessions, selectedUser: 'all', ignoreUser: '', matchHost: '', selectedDb: 'all', selectedCommand: 'all', timeType: 'any', minSeconds: 0, lockWaitsOnly: false });
  assert.deepEqual(Array.from(matched, row => row.id), expected, pattern);
  assert.equal(queryPattern.invalid, pattern === '['); cases++;
}
// These styles must resolve existing theme tokens instead of fixed fallback colors.
const tokens = fs.readFileSync(new URL('../src/style.css', import.meta.url), 'utf8');
for (const [file, names] of [['sessions.css', ['danger-soft', 'warn-soft', 'scrim', 'accent-soft']], ['tables.css', ['warn']], ['editor.css', ['ink', 'shadow-sm']]]) {
  const css = fs.readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');
  assert.doesNotMatch(css, /var\(--(?:fg|color-warn-ink)\b/);
  for (const token of names) {
    assert.ok(css.includes(`var(--${token})`), `${file} uses ${token}`);
    assert.ok(tokens.split(`--${token}:`).length >= 3, `${token} has light and dark values`);
  }
  cases++;
}
console.log(`review-read-hooks: ${cases} cases passed`);
