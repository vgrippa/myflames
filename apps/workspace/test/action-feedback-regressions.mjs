// Execute the real handlers and harness branches without a server or browser.
// action-feedback.mjs still owns rendered visibility, focus and elapsed time.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import { SCENARIOS } from './action-feedback-lib.mjs';

const source = name => ts.createSourceFile(name, fs.readFileSync(new URL(name, import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, name.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.JS);
function find(root, predicate) {
  if (predicate(root)) return root;
  return ts.forEachChild(root, node => find(node, predicate));
}
const named = (root, name) => {
  const node = find(root, n => ts.isFunctionDeclaration(n) && n.name?.text === name);
  assert.ok(node, `${name} exists`);
  return node.getText(root).replace(/^export /, '');
};
function executable(text, env) {
  const code = ts.transpileModule(`(${text})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.React },
  }).outputText;
  return new Function(...Object.keys(env), `return ${code}`)(...Object.values(env));
}
const kit = source('../src/kit.tsx');
const editor = source('../src/QueryLab.tsx');
const errorText = executable(named(kit, 'errorText'), {});
const writeFailure = executable(named(kit, 'writeFailure'), { errorText });
let checks = 0;

// Stop/Commit/Roll back already share writeFailure. Exercise their actual
// catch blocks with both HTTP classes and a rejected network request.
for (const action of ['stop', 'commit', 'rollback']) {
  for (const status of [400, 403, 409, 499, 500, 503, 504, null]) {
    const runtimeRef = { current: { tab: { gen: 1, running: action === 'stop' } } };
    const error = Object.assign(new Error('Simulated failure'), status === null ? {} : { status });
    let requests = 0;
    const env = {
      runtimeRef, tab: { id: 'tab' }, IDLE_ACTION: { phase: 'idle' }, writeFailure,
      api: async () => { requests++; throw error; },
      patchRuntime: (id, patch) => Object.assign(runtimeRef.current[id], patch),
      denied() {}, sessionConnectionOf: () => ({ host: 'original-server' }),
    };
    if (action === 'stop') {
      const stop = executable(named(editor, 'stop'), env);
      await stop('tab');
      await stop('tab'); // An uncertain Stop must not send a second KILL.
    } else {
      const send = executable(named(editor, 'send'), env);
      await send('tab', `editor/${action}`, { connection: { host: 'original-server' } }, { action });
    }
    const state = runtimeRef.current.tab[action === 'stop' ? 'stopFeedback' : 'sessionFeedback'];
    const unknown = status === null || status >= 500;
    assert.equal(state.phase, unknown ? 'unknown' : 'failed', `${action}: HTTP ${status}`);
    if (unknown) assert.match(state.message, /^Outcome unknown\..*not sent again.*Audit log/);
    else assert.equal(state.message, error.message, '4xx keeps the server words');
    assert.equal(requests, 1, `${action} never resends`);
    checks++;
  }
}

// Run the real error branch with controlled browser/workspace boundaries.
// A scenario's 503 must reach the response, rather than silently becoming 400.
const harness = source('./action-feedback-lib.mjs');
for (const errorStatus of [undefined, 503]) {
  const replies = [];
  const endpoint = '/api/editor/commit';
  const handle = { boundingBox: async () => ({ x: 0, y: 0, width: 10, height: 10 }), evaluate: async () => 'button' };
  const ctx = { mocks: {}, hold: new Set(), held: [{ path: endpoint, request: {} }], log: [], errors: [] };
  ctx.page = {
    evaluate: async () => ({ tClick: 1, tFirst: 2 }), waitForFunction: async () => true, close: async () => {},
    mouse: { click: async () => { if (!ctx.log.length) ctx.log.push({ path: endpoint }); } },
  };
  const runOne = executable(named(harness, 'runOne'), {
    assert, startWorkspace: async () => ({ url: 'unused', close: async () => {} }), openPage: async () => ctx,
    pause: async () => {}, visible: async () => handle, heldFor: async () => {}, FEEDBACK_MS: 150,
    failure: () => 'Simulated failure', respond: (_request, status) => replies.push(status),
  });
  await runOne(null, { name: 'status', paths: [endpoint], target: 'button', setup: async () => {},
    errorStatus, error: async () => {} }, 'error');
  assert.deepEqual(replies, [errorStatus ?? 400], 'the declared HTTP status reaches the app');
  checks++;
}

// Refresh all supplies its own answers: both held and subsequent reads fail.
const refresh = SCENARIOS.find(s => s.name === 'server-refresh-all');
const replies = [];
const ctx = { hold: new Set(refresh.paths), mocks: {}, held: refresh.paths.map(path => ({ path,
  request: { respond: async reply => replies.push(reply.status) } })) };
await refresh.error({ ctx, page: { waitForFunction: async () => true } });
assert.equal(ctx.hold.size, 0);
assert.equal(ctx.held.length, 0);
assert.deepEqual(replies, refresh.paths.map(() => 400));
for (const path of refresh.paths) assert.equal(ctx.mocks[path]().__status, 400);
checks++;

// Evaluate each owner's actual status JSX and ViewLayout's actual JSX tree.
// With a detail open, the list is hidden; the pending review must stay outside
// that subtree. This tests placement without pretending to verify pixels.
const React = { Fragment: 'fragment', createElement: (type, props, ...children) => ({ type, props: { ...props, children } }) };
function contains(node, target, hidden = false) {
  if (!node || typeof node !== 'object') return [];
  hidden ||= !!node.props?.hidden;
  if (node === target) return [hidden];
  return (node.props?.children || []).flat(Infinity).flatMap(child => contains(child, target, hidden));
}
for (const file of ['ClusterPlace.tsx', 'ProxySqlPlace.tsx']) {
  const tree = source(`../src/${file}`);
  const layout = find(tree, n => ts.isJsxOpeningElement(n) && n.tagName.getText(tree) === 'ViewLayout');
  const status = layout.attributes.properties.find(n => ts.isJsxAttribute(n) && n.name.text === 'status');
  const marker = React.createElement('pending-review');
  const rendered = executable(`() => (${status.initializer.expression.getText(tree)})`, {
    React, reads: {}, digestReads: {}, actions: { sheet: marker }, failed: '', shownResult: null,
    ViewStatus: 'ViewStatus', ActionResult: 'ActionResult',
  })();
  const ViewLayout = executable(named(kit, 'ViewLayout'), {
    React, useContext: () => null, useEffect() {}, useRef: () => ({ current: null }),
    useInspector: () => ({ open: true, sheet: false }),
    ViewActivityContext: {}, ViewBackLabelContext: {}, ViewSwitcherContext: { Provider: 'provider' },
    Inspector: 'inspector', InfoPopover: 'info', Icon: 'icon',
  });
  const list = React.createElement('list', null, ...layout.parent.children.filter(n =>
    ts.isJsxExpression(n) && n.expression?.getText(tree) === 'actions.sheet').map(() => marker));
  const renderedLayout = ViewLayout({ id: 'review', title: 'Review', inspector: {}, status: rendered, children: list });
  assert.deepEqual(contains(renderedLayout, list), [true], 'the detail hides the retained list');
  assert.deepEqual(contains(renderedLayout, marker), [false], `${file}: pending review remains outside the hidden list`);
  checks++;
}
console.log(`action-feedback-regressions: ${checks} checks passed (no sockets)`);
