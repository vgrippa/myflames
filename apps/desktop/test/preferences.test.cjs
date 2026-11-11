'use strict';
// Run the actual shutdown function with renderer/backend doubles. No Electron
// process, service or database is launched by these tests.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const main = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8');
const shutdown = main.slice(main.indexOf('async function flushRendererPreferences()'), main.indexOf('function bundleWritable()'));
function harness(executeJavaScript, destroyed = false) {
  const events = [], timers = new Map();
  const context = vm.createContext({
    window: { isDestroyed: () => destroyed, webContents: { isDestroyed: () => false,
      executeJavaScript: script => { events.push('flush'); assert.equal(script, 'window.__myflamesFlushUiPreferences?.()'); return executeJavaScript(); } } },
    backend: { stop: async () => { events.push('stop'); } }, launching: null, fatal: false, smoke: true,
    setTimeout: (fn, ms) => { assert.equal(ms, 1600); timers.set(1, fn); return 1; },
    clearTimeout: id => timers.delete(id),
  });
  const done = vm.runInContext(shutdown + '\nstopBackend()', context);
  return { events, timers, done };
}
test('quit waits for the renderer preference drain before stopping the backend', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const run = harness(() => pending);
  assert.deepEqual(run.events, ['flush']);
  release(); await run.done;
  assert.deepEqual(run.events, ['flush', 'stop']);
  assert.equal(run.timers.size, 0);
});
test('unresponsive renderer cannot hold backend shutdown beyond the bound', async () => {
  const run = harness(() => new Promise(() => {}));
  assert.deepEqual(run.events, ['flush']);
  run.timers.get(1)(); await run.done;
  assert.deepEqual(run.events, ['flush', 'stop']);
  assert.equal(run.timers.size, 0);
});
test('crashed or destroyed renderers still allow owned backend shutdown', async () => {
  const crashed = harness(() => { throw new Error('renderer gone'); }); await crashed.done;
  assert.deepEqual(crashed.events, ['flush', 'stop']);
  const destroyed = harness(() => assert.fail('destroyed window cannot execute JavaScript'), true); await destroyed.done;
  assert.deepEqual(destroyed.events, ['stop']);
});
// Exercise the actual close listeners with controllable Electron events and
// clocks. A delayed replay is delivered only after window.close() has returned.
function closeHarness({ executeJavaScript = () => Promise.resolve(), quitting = false,
  exiting = false, asynchronous = false, refusesUnload = false, restartingForUpdate = false } = {}) {
  const events = [], timers = new Map(), replays = [];
  let destroyed = false, nextTimer = 0;
  const window = new EventEmitter(), contents = new EventEmitter();
  window.webContents = contents;
  window.isDestroyed = () => destroyed;
  contents.isDestroyed = () => false;
  contents.executeJavaScript = script => {
    assert.equal(script, 'window.__myflamesFlushUiPreferences?.()');
    events.push('flush');
    return executeJavaScript();
  };
  const emitClose = () => {
    let prevented = false;
    window.emit('close', { preventDefault: () => { prevented = true; events.push('prevent'); } });
    if (!prevented) {
      events.push('allowed');
      if (refusesUnload) {
        let overridden = false;
        contents.emit('will-prevent-unload', { preventDefault: () => { overridden = true; events.push('override'); } });
        if (!overridden) { events.push('refused'); return; }
      }
      destroyed = true;
      window.emit('closed');
    }
  };
  window.close = () => {
    events.push('ordinary-close');
    if (asynchronous) replays.push(emitClose);
    else emitClose();
  };
  const context = vm.createContext({ window, contents, quitting, exiting, restartingForUpdate,
    setTimeout: (fn, ms) => { timers.set(++nextTimer, { fn, ms }); return nextTimer; },
    clearTimeout: id => timers.delete(id),
  });
  const start = main.indexOf('  let closing = false, flushingClose = false;');
  const unload = main.indexOf("  contents.on('will-prevent-unload',");
  assert.ok(start >= 0 && unload > start);
  vm.runInContext(shutdown + main.slice(start, main.indexOf('  const contents = window.webContents;', start)) +
    main.slice(unload, main.indexOf("  contents.on('will-attach-webview',", unload)), context);
  return { events, timers, replays, close: emitClose,
    settle: () => new Promise(resolve => setImmediate(resolve)),
    replay: async () => { await Promise.resolve(); assert.ok(replays.length); replays.shift()(); },
    fireTimer: ms => {
      const matches = [...timers].filter(([, timer]) => timer.ms === ms);
      assert.equal(matches.length, 1, `one ${ms} ms timer is pending`);
      const [id, timer] = matches[0];
      timers.delete(id); timer.fn();
    },
  };
}
test('direct window close drains once, then replays ordinary close without bypassing beforeunload', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const run = closeHarness({ executeJavaScript: () => pending });
  run.close(); run.close();
  assert.deepEqual(run.events, ['prevent', 'flush', 'prevent'], 'duplicate close waits on the same drain');
  release(); await run.settle();
  assert.deepEqual(run.events, ['prevent', 'flush', 'prevent', 'ordinary-close', 'allowed']);
  assert.equal(run.timers.size, 0, 'closed clears the replay fallback');
});
test('window close while quitting is prevented without another preference flush', async () => {
  const run = closeHarness({ quitting: true });
  run.close(); await run.settle();
  assert.deepEqual(run.events, ['prevent']);
  assert.equal(run.timers.size, 0);
});
test('window close while exiting is allowed without a preference flush', async () => {
  const run = closeHarness({ exiting: true, quitting: true });
  run.close(); await run.settle();
  assert.deepEqual(run.events, ['allowed']);
  assert.equal(run.timers.size, 0);
});
test('asynchronously replayed close goes through after exactly one preference flush', async () => {
  const run = closeHarness({ asynchronous: true });
  run.close(); await run.settle();
  assert.deepEqual(run.events, ['prevent', 'flush', 'ordinary-close']);
  assert.equal(run.replays.length, 1);
  await run.replay(); await run.settle();
  assert.deepEqual(run.events, ['prevent', 'flush', 'ordinary-close', 'allowed']);
  assert.equal(run.replays.length, 0, 'no close loop is queued');
  assert.equal(run.timers.size, 0);
});
test('beforeunload refusal lets the next close flush preferences again', async () => {
  const run = closeHarness({ refusesUnload: true });
  run.close(); await run.settle();
  assert.deepEqual(run.events, ['prevent', 'flush', 'ordinary-close', 'allowed', 'refused']);
  assert.equal(run.timers.size, 0, 'refusal clears the replay fallback immediately');
  run.close(); await run.settle();
  assert.deepEqual(run.events, [
    'prevent', 'flush', 'ordinary-close', 'allowed', 'refused',
    'prevent', 'flush', 'ordinary-close', 'allowed', 'refused',
  ]);
  assert.equal(run.timers.size, 0);
});
test('Restart to update still overrides beforeunload refusal', async () => {
  const run = closeHarness({ exiting: true, quitting: true, restartingForUpdate: true, refusesUnload: true });
  run.close(); await run.settle();
  assert.deepEqual(run.events, ['allowed', 'override']);
  assert.equal(run.timers.size, 0);
});
test('hung renderer cannot hold direct close beyond the preference flush bound', async () => {
  const run = closeHarness({ executeJavaScript: () => new Promise(() => {}) });
  run.close(); run.close();
  assert.deepEqual(run.events, ['prevent', 'flush', 'prevent']);
  run.fireTimer(1600); await run.settle();
  assert.deepEqual(run.events, ['prevent', 'flush', 'prevent', 'ordinary-close', 'allowed']);
  assert.equal(run.timers.size, 0);
});
test('missing close completion expires the replay guard so a later close flushes again', async () => {
  const run = closeHarness({ asynchronous: true });
  run.close(); await run.settle();
  assert.deepEqual(run.events, ['prevent', 'flush', 'ordinary-close']);
  run.fireTimer(2000);
  assert.equal(run.timers.size, 0);
  run.close(); await run.settle();
  assert.deepEqual(run.events, ['prevent', 'flush', 'ordinary-close', 'prevent', 'flush', 'ordinary-close']);
  run.fireTimer(2000);
  assert.equal(run.timers.size, 0);
});
