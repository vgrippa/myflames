// Release rf-3 regressions: execute the actual handlers, projections and JSX.
// No browser, listening sockets, database or copied production logic.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = name => ts.createSourceFile(name, fs.readFileSync(new URL(name, import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, name.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.JS);
function find(root, predicate) {
  if (predicate(root)) return root;
  return ts.forEachChild(root, node => find(node, predicate));
}
function named(root, name) {
  const node = find(root, n => ts.isFunctionDeclaration(n) && n.name?.text === name);
  assert.ok(node, name);
  return node.getText().replace(/^export /, '');
}
function run(text, env) {
  return vm.runInNewContext(ts.transpileModule(text, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.React },
  }).outputText, env);
}
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const kit = source('../src/kit.tsx'), bulk = source('../src/MassKillDialog.tsx');
const locks = source('../src/Locks.tsx');
const errorText = run(`(${named(kit, 'errorText')})`, {});
const writeFailure = run(`(${named(kit, 'writeFailure')})`, { errorText });
const connectionKey = run(`(${named(source('../src/binding.ts'), 'connectionKey')})`, {});
const React = { Fragment: 'fragment', createElement: (type, props, ...children) => ({ type, props: { ...props, children } }) };
function text(tree) {
  if (tree == null || typeof tree === 'boolean') return '';
  if (typeof tree !== 'object') return String(tree);
  return (tree.props?.children || []).flat(Infinity).map(text).join('');
}
function harness() {
  const check = deferred(), reply = deferred(), notes = [], newerNotes = [], errors = [], sent = [];
  const connection = { host: 'original', port: 3306, user: 'app', type: 'production', read_only: false };
  const review = { review_token: 'reviewed-token', statements: ['KILL QUERY 42'], mode: 'query' };
  const targets = [{ connection_id: 42, mode: 'query', expect: { user: 'app' } }];
  let closed = 0, classified = 0;
  const env = {
    connection, connectionKey, policy: { check: () => check.promise },
    pending: { review, targets, key: connectionKey(connection), label: 'Original' },
    busy: false, bulkInFlight: null, left: { current: false }, sending: { current: null },
    receive: { current: () => {} }, openDialogs: [], errorText,
    writeFailure: (...args) => { classified++; return writeFailure(...args); },
    begin: () => { env.busy = true; }, end: () => { env.busy = false; },
    setError: value => errors.push(value), setPending: value => { env.pending = value; },
    setBulkInFlight: value => { env.bulkInFlight = value; }, takeEarlier() {},
    onSuccess: note => notes.push(note), onClose: () => { closed++; },
    killBulkSessions: (...args) => { sent.push(args); return reply.promise; },
  };
  env.waitingNote = run(`(${named(bulk, 'waitingNote')})`, {});
  const send = run(`(${named(bulk, 'sendBulk')})`, env);
  const leave = run(`(${named(bulk, 'leave')})`, env);
  env.earlierOutcome = { current: null };
  env.onSuccessRef = { current: env.onSuccess };
  const mount = find(bulk, n => ts.isCallExpression(n) && n.expression.getText() === 'useEffect' &&
    n.arguments[0]?.getText().includes('openDialogs.push(own)'));
  const cleanup = run(`(${mount.arguments[0].getText()})()`, env);
  return { check, reply, notes, newerNotes, errors, sent, env, send, leave,
    unmount() { closed++; cleanup(); },
    closed: () => closed, classified: () => classified,
    newer() { env.openDialogs.push(note => newerNotes.push(note)); },
  };
}
const renderNote = note => run(`(${named(locks, 'KillResultNote')})`, {
  React, killHeadline: run(`(${named(locks, 'killHeadline')})`, {
    killStatusWord: run(`(${named(locks, 'killStatusWord')})`, {}),
  }),
})({ note, currentKey: 'another connection', noteRef: {}, onDismiss() {}, onRefresh() {} });

for (const departure of ['open', 'close', 'unmount']) for (const newer of departure === 'open' ? [false] : [false, true]) {
  const left = departure !== 'open';
  for (const change of [{ host: 'replacement' }, { type: 'development' }, { read_only: true }]) {
    test(`bulk policy change ${JSON.stringify(change)}, ${departure}, newer=${newer}`, async () => {
      const h = harness(), pending = h.send();
      if (left) { departure === 'unmount' ? h.unmount() : h.leave(); if (newer) h.newer(); }
      h.check.resolve({ ...h.env.connection, ...change });
      await pending;
      assert.equal(h.sent.length, 0, 'policy refusal cannot send KILL');
      assert.equal(h.env.pending, null, 'a new review is required');
      assert.equal(h.env.bulkInFlight, null); assert.equal(h.env.sending.current, null); assert.equal(h.env.busy, false);
      assert.equal(h.classified(), 0, 'a refusal is not a write failure');
      if (left) {
        const note = (newer ? h.newerNotes : h.notes).at(-1);
        assert.ok(note, 'the refusal reaches its mounted owner');
        assert.equal(note.status, 'refused', 'the pending note resolves even after Close');
        assert.equal(note.late, true); assert.equal(note.on.key, connectionKey(h.env.connection));
        assert.match(note.message, /nothing was sent/i);
        assert.equal(h.closed(), 1, 'late refusal does not close a newer dialog');
        assert.doesNotMatch(text(renderNote(note)), /Sent on /, 'an unsent refusal must not claim it was sent');
        if (newer) assert.equal(h.notes.length, 1, 'the newer dialog owns delivery to Processlist when it closes');
      } else {
        assert.match(h.errors.at(-1), /nothing was sent/i);
        assert.equal(h.notes.length, 0); assert.equal(h.closed(), 0);
      }
    });
  }
  for (const status of [undefined, 403, 503]) {
    test(`bulk policy read failure HTTP ${status}, ${departure}, newer=${newer}`, async () => {
      const h = harness(), pending = h.send();
      if (left) { departure === 'unmount' ? h.unmount() : h.leave(); if (newer) h.newer(); }
      h.check.reject(Object.assign(new Error('Policy unavailable'), status === undefined ? {} : { status }));
      await pending;
      assert.equal(h.sent.length, 0);
      assert.equal(h.classified(), 0, 'writeFailure is reserved for attempted writes');
      const note = (newer ? h.newerNotes : h.notes).at(-1);
      const message = left ? note.message : h.errors.at(-1);
      assert.match(message, /Policy unavailable/); assert.match(message, /Nothing was sent/);
      assert.doesNotMatch(message, /Outcome unknown|may have|Audit log/i);
      if (left) {
        assert.equal(note.status, 'error'); assert.equal(note.late, true);
        assert.equal(note.on.key, connectionKey(h.env.connection));
        assert.ok(!note.refresh, 'no write happened to refresh');
        assert.doesNotMatch(text(renderNote(note)), /Sent on /);
      }
      assert.equal(h.env.pending, null); assert.equal(h.env.bulkInFlight, null); assert.equal(h.env.busy, false);
      await h.send(); assert.equal(h.sent.length, 0, 'old confirmation is cleared');
    });
  }
}
for (const status of [undefined, 400, 503]) test(`bulk attempted write HTTP ${status} retains its existing outcome`, async () => {
  const h = harness(), pending = h.send();
  h.check.resolve(h.env.connection); await new Promise(setImmediate);
  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0][0], h.env.connection);
  assert.equal(h.sent[0][1], h.env.pending.targets);
  assert.equal(h.sent[0][2], 'reviewed-token');
  await h.send(); assert.equal(h.sent.length, 1, 'single flight before the reply');
  h.leave();
  h.reply.reject(Object.assign(new Error('Reply failed'), status === undefined ? {} : { status }));
  await pending;
  assert.equal(h.classified(), 1);
  const note = h.notes.at(-1);
  assert.equal(note.status, status === 400 ? 'error' : 'unknown');
  if (status !== 400) {
    assert.match(note.message, /may have run.*not sent again.*Audit log/);
    assert.match(text(renderNote(note)), /Outcome unknown/);
  }
  await h.send(); assert.equal(h.sent.length, 1, 'an unknown write is never repeated');
});

// Use the actual report projection and footer call for both response shapes.
const digest = source('../src/QueryDigest.tsx');
const load = find(digest, n => ts.isCallExpression(n) && n.expression.getText() === 'reads.run');
const footer = find(digest, n => ts.isCallExpression(n) && n.expression.getText() === 'useWindowStatus');
const current = find(digest, n => ts.isVariableDeclaration(n) && n.name.getText() === 'current');
const engineName = run(`(${named(source('../src/OpsShared.tsx'), 'engineName')})`, {});
for (const filter of ['time', 'slow_queries']) for (const [engine, server_version, server_product, label] of [
  ['mysql', '8.4.11-11', 'Percona Server', 'Percona Server 8.4.11-11'],
  ['mysql', '8.4.11-11.1', 'Percona XtraDB Cluster', 'Percona XtraDB Cluster 8.4.11-11.1'],
  ['mysql', '8.4.11', undefined, 'MySQL 8.4.11'],
  ['mariadb', '11.4.13-MariaDB-log', undefined, 'MariaDB 11.4.13'],
]) test(`digest ${filter}: ${label} through projection, footer and export`, async () => {
  const calls = [], signal = new AbortController().signal;
  const project = run(`(${load.arguments[2].getText()})`, {
    filter, limit: 50, minSeconds: 1,
    api: async (...args) => { calls.push(args); return { engine, server_version, server_product, statements: [] }; },
  });
  const report = await project({ host: 'original' }, signal);
  assert.equal(calls[0][0], filter === 'time' ? 'live/top' : 'live/slow-queries');
  assert.equal(calls[0][2], signal);
  let status;
  const env = { engineName, bound: { key: 'original', report }, reads: { key: 'original' },
    connection: { host: 'original' }, connectionLabel: c => c.host, loading: false,
    useWindowStatus: (_key, value) => { status = value; } };
  run(`const current = ${current.initializer.getText()}; ${footer.getText()}`, env);
  assert.equal(status.engineVersion, label);
  env.reads.key = 'other';
  run(`const current = ${current.initializer.getText()}; ${footer.getText()}`, { ...env });
  assert.equal(status.engineVersion, undefined, 'an old report never labels the active connection');
  const exportText = find(digest, n => ts.isVariableDeclaration(n) && n.name.getText() === 'generatePtReportText');
  const exported = run(`(${exportText.initializer.getText()})()`, {
    engineName, serverMeta: report, bound: { label: 'Original' }, filter, statements: [],
    aggregates: { totalCalls: 0, totalWorkloadMs: 0, totalExamined: 0, totalSent: 0, totalLockMs: 0 },
    time: String, number: String,
  });
  assert.match(exported, new RegExp(`^# Server: ${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, 'm'));
});

// Render the real Settings root's JSX attributes, then run the runner's actual
// readiness expression against a minimal selector surface. Geometry is not
// asserted here; perf-measure's __perf.vis still checks visibility in Chrome.
const sidebar = source('../src/Sidebar.tsx'), runner = source('./perf-measure.mjs');
const settings = find(sidebar, n => ts.isJsxOpeningElement(n) && n.attributes.properties.some(p =>
  ts.isJsxAttribute(p) && p.name.text === 'id' && p.initializer?.text === 'settings-popover'));
const root = run(`(${settings.getText().replace(/>$/, '/>')})`, { React, settings: { current: null } });
const step = find(runner, n => ts.isCallExpression(n) && n.expression.getText() === 'interact' && n.arguments[1]?.text === 'settings menu open');
const ready = step.arguments[3].text;
function matches(node, selector) {
  return selector.split(',').some(part => {
    let rest = part.trim(), ok = true;
    rest = rest.replace(/^[a-z][a-z0-9-]*/i, tag => { ok &&= node.type === tag; return ''; });
    rest = rest.replace(/([.#])([\w-]+)|\[([\w-]+)=["']?([^\]"']+)["']?\]/g, (_all, kind, name, attr, value) => {
      ok &&= kind === '.' ? (node.props.className || '').split(' ').includes(name)
        : kind === '#' ? node.props.id === name : node.props[attr] === value;
      return '';
    });
    assert.equal(rest, '', 'test supports the readiness selector syntax');
    return ok;
  });
}
test('Settings readiness targets the rendered dialog root', () => {
  assert.equal(root.props.role, 'dialog');
  assert.equal(run(ready, { __perf: { vis: selector => matches(root, selector) ? root : null } }), true);
});
test('unrelated menus and popovers cannot satisfy Settings readiness', () => {
  const unrelated = [{ type: 'div', props: { role: 'menu' } }, { type: 'div', props: { className: 'popover' } }];
  assert.equal(run(ready, { __perf: { vis: selector => unrelated.find(n => matches(n, selector)) } }), false);
  assert.equal(run(ready, { __perf: { vis: () => null } }), false, 'closed Settings is not ready');
});
