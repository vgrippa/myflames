// Execute Server's actual focus effect with controlled events and a clock.
// This checks lifecycle and timing, not browser hit testing or native focus.
// The coordinator's action-feedback.mjs --only server-refresh-all covers those.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';

const source = ts.createSourceFile('Server.tsx',
  fs.readFileSync(new URL('../src/Server.tsx', import.meta.url), 'utf8'),
  ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function find(node, predicate) {
  if (predicate(node)) return node;
  return ts.forEachChild(node, child => find(child, predicate));
}
const effect = find(source, node => ts.isCallExpression(node) && node.expression.getText(source) === 'useEffect' &&
  node.arguments[0]?.getText(source).includes('const toStop ='));
assert.ok(effect, 'Server has its Refresh all focus effect');
const code = ts.transpileModule(`(${effect.arguments[0].getText(source)})`, {
  compilerOptions: { target: ts.ScriptTarget.ES2020 },
}).outputText;
let checks = 0;
function mount({ running = true, stopArmed = false, present = true } = {}) {
  const listeners = new Map();
  const body = {};
  const document = { body, activeElement: { name: 'Refresh options' } };
  let now = 0;
  const deadline = { current: 600 };
  const stop = { focus(options) { assert.deepEqual(options, { preventScroll: true }); document.activeElement = stop; } };
  const place = {
    querySelector(selector) { assert.equal(selector, '.refresh-all-stop'); return stop; },
    addEventListener(type, callback) { assert.ok(!listeners.has(type)); listeners.set(type, callback); },
    removeEventListener(type, callback) { assert.equal(listeners.get(type), callback); listeners.delete(type); },
  };
  const env = { running, stopArmed, placeRef: { current: present ? place : null },
    stopArmedAt: deadline, document, Date: { now: () => now } };
  const cleanup = new Function(...Object.keys(env), `return ${code}`)(...Object.values(env))();
  return { document, stop, listeners, cleanup, deadline, advance: time => { now = time; },
    // mousedown's native default has already run when the click bubbles.
    click(active) { document.activeElement = active; listeners.get('click')?.(); } };
}

// The next click can arrive long after two 60 Hz frames (about 33 ms), while
// Stop is still protected. Advancing the clock needs no timers or frame mocks.
for (const delay of [1, 50, 250, 599]) {
  const run = mount();
  assert.equal(run.document.activeElement.name, 'Refresh options', 'initial handoff retains the trigger');
  run.advance(delay);
  run.click(run.document.body);
  assert.equal(run.document.activeElement, run.stop, `a trailing click at ${delay} ms returns focus to Stop`);
  run.cleanup();
  assert.equal(run.listeners.size, 0, 'cleanup removes the click listener');
  checks++;
}

const run = mount();
for (const control of [{ name: 'Stop' }, { name: 'Refresh options' }, { name: 'Other control' }]) {
  run.click(control);
  assert.equal(run.document.activeElement, control, 'a focused control keeps focus');
  checks++;
}
for (const delay of [600, 1000]) {
  run.advance(delay);
  run.click(run.document.body);
  assert.equal(run.document.activeElement, run.document.body, 'the deadline applies even before effect cleanup runs');
  checks++;
}
run.cleanup();
run.advance(100);
run.click(run.document.body);
assert.equal(run.document.activeElement, run.document.body, 'after unmount or run end no listener restores focus');
checks++;

for (const options of [{ running: false }, { stopArmed: true }, { present: false }]) {
  const inactive = mount(options);
  assert.equal(inactive.listeners.size, 0, 'no listener after the run, after arming, or without the place');
  inactive.cleanup?.();
  checks++;
}
console.log(`server-refresh-focus-unit: ${checks} checks passed (no browser or sockets)`);
