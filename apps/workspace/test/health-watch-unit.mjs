// Capture when… without a browser: judging a watch-1.0 read, a connection
// switch, and times on this computer's clock (Health.tsx).
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { build } from "esbuild";

// UTC-3 all year: 14:02:31Z reads 11:02:31 here.
process.env.TZ = "America/Sao_Paulo";
const result = await build({
  stdin: {
    contents: `export { judgeWatch, switchHealthConnection, rememberHealth, clockTime, localMinute, watchConditions, emptyHealth, groupStatusText, pendingTrendText, createHealthRefresh, healthAutoPreference } from './src/Health'; export { trendRange } from './src/kit';`,
    loader: "tsx", resolveDir: new URL("..", import.meta.url).pathname,
  },
  bundle: true, platform: "node", format: "cjs", write: false,
  jsx: "automatic", loader: { ".css": "empty", ".svg": "text" },
  external: ["react", "react-dom", "react-dom/server"], logLevel: "silent",
});
const out = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(createRequire(import.meta.url), out, out.exports);
const { judgeWatch, switchHealthConnection, rememberHealth, clockTime, localMinute, watchConditions, emptyHealth, groupStatusText,
  pendingTrendText, trendRange, createHealthRefresh, healthAutoPreference } = out.exports;

const rules = { threads: 32, lockWait: 10, statement: 30, interval: 5, stopAfterFirst: true };
const read = (extra = {}) => ({
  schema_version: "watch-1.0", read_at: "2026-10-27T14:02:31Z", threads_running: 4,
  longest_statement: null, longest_lock_wait: null, reason: null,
  errors: { threads_running: null, longest_statement: null, longest_lock_wait: null }, notes: [], ...extra,
});

// Nothing met, nothing unread.
assert.deepEqual(judgeWatch(rules, read()), { tripped: "", note: "" });
// The first condition met names the condition and its value (seconds rounded).
assert.equal(judgeWatch(rules, read({ threads_running: 57 })).tripped, "threads running 57");
assert.equal(judgeWatch(rules, read({ longest_lock_wait: { id: 7, seconds: 12.6 } })).tripped, "row lock wait 13 s");
assert.equal(judgeWatch(rules, read({ longest_statement: { id: 8, seconds: 31, user: "a", db: null } })).tripped, "statement running 31 s");
// Thresholds are inclusive.
assert.equal(judgeWatch(rules, read({ threads_running: 32 })).tripped, "threads running 32");
assert.equal(judgeWatch(rules, read({ threads_running: 31 })).tripped, "");
// A condition that is off is never judged.
assert.equal(judgeWatch({ ...rules, threads: null }, read({ threads_running: 500 })).tripped, "");
// A value that was not read is never zero: it is named with the server's reason.
const unread = judgeWatch(rules, read({ threads_running: null, errors: { threads_running: "Access denied", longest_statement: null, longest_lock_wait: null } }));
assert.deepEqual(unread, { tripped: "", note: "Not judged: threads running (Access denied)." });
// A partial read: the value from the sources read still trips; the rest is named.
const partial = judgeWatch(rules, read({
  longest_lock_wait: { id: 77, seconds: 22.5 },
  errors: { threads_running: null, longest_statement: null, longest_lock_wait: "Row lock wait times need the PROCESS privilege." },
}));
assert.equal(partial.tripped, "row lock wait 23 s");
assert.equal(partial.note, "Partly judged: row lock waits (Row lock wait times need the PROCESS privilege.).");
// Errors of conditions that are off are not shown.
assert.equal(judgeWatch({ ...rules, lockWait: null }, read({ errors: { longest_lock_wait: "x" } })).note, "");
// An older report without errors still judges.
assert.equal(judgeWatch(rules, { ...read({ threads_running: 40 }), errors: undefined }).tripped, "threads running 40");
assert.equal(watchConditions(rules), "threads running at or above 32; a row lock wait older than 10 s; a statement running longer than 30 s");

// This computer's clock, 24-hour.
assert.equal(clockTime("2026-10-27T14:02:31Z"), "11:02:31");
assert.equal(localMinute("2026-10-27T14:02:31Z"), "2026-10-27 11:02");

// A connection switch clears the readings and stops a watch of another connection;
// a capture on its way keeps its outcome for the connection it started on.
const watch = (extra = {}) => ({
  run: 1, key: "a", label: "prod-eu", rules, active: true, checked: 3, lastAt: "2026-10-27T14:02:31Z",
  armed: true, note: "", capture: null, ...extra,
});
const memory = { ...emptyHealth, key: "a", label: "prod-eu", history: [{ at: "x", values: {} }], watch: watch() };
const switched = switchHealthConnection(memory, "b");
assert.deepEqual(switched.history, []);
assert.equal(switched.watch, null, "a watch without a capture ends");
const capturing = switchHealthConnection({ ...memory, watch: watch({ capture: { phase: "pending", message: "Capturing…", startedAt: 1, saved: null, missing: "" } }) }, "b");
assert.equal(capturing.watch.active, false, "checking stops");
assert.equal(capturing.watch.capture.phase, "pending", "the capture goes on, bound to its connection");
assert.equal(capturing.watch.key, "a");
// The same connection changes nothing.
assert.equal(switchHealthConnection(memory, "a"), memory);
// A new sample keeps the watch.
const sample = { taken_at: "2026-10-27T14:03:00Z", metrics: [{ key: "queries", value: 5 }], diagnostics: [], restarted: false };
assert.equal(rememberHealth(memory, { key: "a", label: "prod-eu", report: sample }).watch, memory.watch);

// The same sample twice (the same taken_at, even as another object) is one
// reading, so one reading never draws as a chart; a new sample is appended.
const first = rememberHealth(emptyHealth, { key: "a", label: "prod-eu", report: sample });
assert.equal(first.history.length, 1);
const again = rememberHealth(first, { key: "a", label: "prod-eu", report: structuredClone(sample) });
assert.equal(again.history, first.history, "no second reading");
assert.notEqual(again.report, first.report, "the answer still replaces the report");
const second = rememberHealth(first, { key: "a", label: "prod-eu", report: { ...sample, taken_at: "2026-10-27T14:03:10Z" } });
assert.equal(second.history.length, 2);
// Another connection with the same taken_at starts its own history.
assert.equal(rememberHealth(second, { key: "b", label: "other", report: structuredClone(sample) }).history.length, 1);
// Compare with baseline is bound to the connection it was chosen on: a new
// reading of that connection keeps it, a reading of another or a switch ends it.
const comparing = { ...second, baseline: { key: "a", baseline: { id: "0".repeat(32), name: "Before deploy" } } };
assert.equal(rememberHealth(comparing, { key: "a", label: "prod-eu", report: { ...sample, taken_at: "2026-10-27T14:04:00Z" } }).baseline,
  comparing.baseline);
assert.equal(rememberHealth(comparing, { key: "b", label: "other", report: structuredClone(sample) }).baseline, null);
assert.equal(switchHealthConnection(comparing, "b").baseline, null);
assert.equal(switchHealthConnection(comparing, "a"), comparing, "the same connection changes nothing");
assert.equal(emptyHealth.baseline, null);

// Live charts and sparklines: the range as the format writes it; "steady"
// when two or more readings show the same text (no Min equal to Max).
const at = (i) => `2026-10-27T14:0${i}:00Z`;
const perSecond = (v) => `${Math.round(v)}/s`;
assert.equal(trendRange([], perSecond), null);
assert.deepEqual(trendRange([{ at: at(1), value: 283 }], perSecond), { readings: 1, current: "283/s", low: "283/s", high: "283/s", steady: false });
assert.deepEqual(trendRange([{ at: at(1), value: 283 }, { at: at(2), value: 145.2 }], perSecond),
  { readings: 2, current: "145/s", low: "145/s", high: "283/s", steady: false });
// Equal at the precision shown is steady, even when the raw values differ.
assert.equal(trendRange([{ at: at(1), value: 283.1 }, { at: at(2), value: 282.9 }, { at: at(3), value: 283 }], perSecond).steady, true);
assert.equal(pendingTrendText(0), "Collecting…");
assert.equal(pendingTrendText(30), "Collecting…");
// A tile group's share of the summary's count, empty when all is well.
assert.equal(groupStatusText(["ok", "watch", "problem", "info"]), "1 problem, 1 to watch");
assert.equal(groupStatusText(["problem", "problem", "unknown"]), "2 problems");
assert.equal(groupStatusText(["watch", "watch"]), "2 to watch");
assert.equal(groupStatusText(["ok", "info", "unknown"]), "");

console.log("health watch unit: ok");

// The scheduling clock is independent of wall time and the browser. A pending
// read owns the automatic flight across hide/show and failures. Manual reads
// can replace it, as exercised against the component below.
function refreshHarness(every = 5000) {
  let time = 0, next = 0, reads = 0;
  const timers = new Map(), delays = [];
  const loop = createHealthRefresh({ every, read: () => { reads++; }, now: () => time,
    later: (fn, ms) => { timers.set(++next, { fn, at: time + ms }); return next; },
    cancel: (id) => timers.delete(id), backoff: (s) => delays.push(s) });
  const advance = (ms) => {
    const until = time + ms;
    while (true) {
      const due = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
      if (!due || due[1].at > until) break;
      time = due[1].at; timers.delete(due[0]); due[1].fn();
    }
    time = until;
  };
  return { loop, advance, delays, timers, get reads() { return reads; } };
}
for (const seconds of [1, 5, 10, 30, 60]) {
  const h = refreshHarness(seconds * 1000);
  h.advance(120000);
  assert.equal(h.reads, 0, "off screen never starts");
  h.loop.setVisible(true);
  assert.equal(h.reads, 1, "one read on open");
  h.advance(120000);
  assert.equal(h.reads, 1, "a slow read cannot overlap itself");
  h.loop.setVisible(false); h.loop.setVisible(true);
  assert.equal(h.reads, 1, "return while pending never duplicates the read");
  h.loop.finished(true);
  h.advance(seconds * 1000 - 1);
  assert.equal(h.reads, 1);
  h.advance(1);
  assert.equal(h.reads, 2, "a completed read waits for its next deadline");
  h.loop.finished(true); h.loop.setVisible(false); h.advance(120000);
  assert.equal(h.reads, 2, "hidden has no scheduled reads");
  h.loop.setVisible(true);
  assert.equal(h.reads, 3, "resume reads once without catch-up");
  h.loop.finished(true); h.advance(seconds * 1000 - 1);
  assert.equal(h.reads, 3);
  h.loop.dispose(); h.advance(120000);
  assert.equal(h.reads, 3, "unmount cancels the timer");
  assert.equal(h.timers.size, 0);
}
const retry = refreshHarness();
retry.loop.setVisible(true);
for (const seconds of [10, 20, 40, 60, 60]) {
  const before = retry.reads;
  retry.loop.finished(false);
  assert.equal(retry.delays.at(-1), seconds, "failure doubles, capped at 60 s");
  retry.advance(seconds * 1000 - 1);
  assert.equal(retry.reads, before, "no early retry");
  retry.advance(1);
  assert.equal(retry.reads, before + 1);
}
retry.loop.finished(false); retry.advance(1000);
retry.loop.setVisible(false); retry.advance(1000); retry.loop.setVisible(true);
const beforeRetry = retry.reads;
retry.advance(57999);
assert.equal(retry.reads, beforeRetry, "return preserves an outstanding failure backoff");
retry.advance(1);
assert.equal(retry.reads, beforeRetry + 1);
retry.loop.finished(true);
assert.equal(retry.delays.at(-1), 0, "success clears the backoff line");
retry.advance(5000);
assert.equal(retry.reads, beforeRetry + 2, "success restores the window interval");
retry.loop.finished(true); retry.advance(1000); retry.loop.started();
retry.advance(120000);
assert.equal(retry.reads, beforeRetry + 2, "manual Refresh cancels the scheduled read");
retry.loop.finished(false);
assert.equal(retry.delays.at(-1), 10, "the first failure after success starts over");
retry.loop.dispose(); retry.loop.setVisible(true); retry.loop.finished(true);
assert.equal(retry.timers.size, 0, "late completions cannot restart a disposed loop");
for (const [saved, expected] of [[null, -1], ["-1", -1], ["0", 0], ["10", 10], ["30", 30], ["60", 60], ["bad", -1], ["5", -1], ["", -1], [" ", -1]])
  assert.equal(healthAutoPreference(saved), expected, "only supported preferences survive restore");
// Every successful completion waits the full interval, even after a slow read.
const cadence = refreshHarness(10000);
cadence.loop.setVisible(true); cadence.advance(5000); cadence.loop.finished(true);
cadence.advance(9999); assert.equal(cadence.reads, 1);
cadence.advance(1); assert.equal(cadence.reads, 2, "the interval starts at completion");
cadence.advance(15000); cadence.loop.finished(true); cadence.advance(300);
assert.equal(cadence.reads, 2, "a slow completion does not trigger an immediate read");
cadence.advance(9699); assert.equal(cadence.reads, 2);
cadence.advance(1); assert.equal(cadence.reads, 3, "slow reads also wait the full interval");
cadence.advance(100000); assert.equal(cadence.reads, 3, "catch-up never overlaps that next read");
cadence.loop.dispose();
// A tick that starts no read (another connection's greyed report on screen)
// is deferred a full interval, and keeps an outstanding failure backoff.
const defer = refreshHarness(5000);
defer.loop.setVisible(true);
assert.equal(defer.reads, 1);
defer.loop.deferred();
defer.advance(4999);
assert.equal(defer.reads, 1, "a deferred tick waits the full interval");
defer.advance(1);
assert.equal(defer.reads, 2, "the next tick reads");
defer.loop.finished(false);
defer.advance(5000);
defer.loop.deferred();
defer.advance(4999);
assert.equal(defer.reads, 2, "a deferred tick keeps the failure backoff");
defer.advance(1);
assert.equal(defer.reads, 3, "the backoff deadline still reads");
defer.loop.dispose();
assert.equal(defer.timers.size, 0);
console.log("health refresh scheduling unit: ok");

// Render the actual Health component and connection-read hook without a DOM
// or socket. Transport, persisted UI preferences and React's hook storage
// are replaced (the preference queue has its own unit suite). This keeps
// sharedRead and the generation counters used by Refresh all under test.
const componentBuild = await build({
  stdin: {
    contents: `export { HealthView } from './src/Health'; export { connectionKey, sharedRead, nextGeneration, latestGeneration } from './src/binding';`,
    loader: "tsx", resolveDir: new URL("..", import.meta.url).pathname,
  },
  bundle: true, platform: "node", format: "cjs", write: false,
  jsx: "automatic", loader: { ".css": "empty", ".svg": "text" },
  external: ["react", "react-dom", "react-dom/server"], logLevel: "silent",
  plugins: [{ name: "health-transport", setup(b) {
    b.onResolve({ filter: /^\.\/uiPreferences$/ }, args => args.importer.endsWith("/Health.tsx")
      ? { path: "health-preferences", namespace: "preferences" } : undefined);
    b.onLoad({ filter: /.*/, namespace: "preferences" }, () => ({
      contents: "export const useUiPreferences = () => environment.preferences; export const setUiPreference = (key,value) => { environment.preferences[key]=value; return Promise.resolve(); };", loader: "js",
    }));
    b.onResolve({ filter: /^\.\/api$/ }, args => args.importer.endsWith("/Health.tsx")
      ? { path: "health-transport", namespace: "test" } : undefined);
    b.onLoad({ filter: /.*/, namespace: "test" }, () => ({
      contents: "export const api = (...args) => environment.read(...args);", loader: "js",
    }));
  } }],
});
const require = createRequire(import.meta.url), react = require("react");
let currentHooks;
const fakeReact = { ...react, ...Object.fromEntries(
  ["useState", "useRef", "useCallback", "useMemo", "useEffect"].map(name => [name, (...args) => currentHooks[name](...args)])),
  useContext: context => context._currentValue,
};
const timers = new Map();
let timerId = 0, clock = 0;
const observers = new Set();
function advance(ms) {
  const until = clock + ms;
  while (true) {
    const due = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
    if (!due || due[1].at > until) break;
    clock = due[1].at; timers.delete(due[0]); due[1].fn();
  }
  clock = until;
}
const environment = {
  window: { location: { hash: "" }, setTimeout: (fn, ms) => { timers.set(++timerId, { fn, at: clock + ms }); return timerId; },
    clearTimeout: id => timers.delete(id),
    // The refresh loop ticks through setInterval; the fake clock fires each
    // registration once, since the loop cancels its interval on entry.
    setInterval: (fn, ms) => { timers.set(++timerId, { fn, at: clock + ms }); return timerId; },
    clearInterval: id => timers.delete(id), addEventListener() {}, removeEventListener() {} },
  document: { visibilityState: "visible", addEventListener() {}, removeEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} },
  performance: { now: () => clock },
  MutationObserver: class {
    constructor(callback) { this.callback = callback; observers.add(this); }
    observe() {}
    disconnect() { observers.delete(this); }
  },
  getComputedStyle: () => ({ visibility: "visible" }),
};
const componentModule = { exports: {} };
new Function("require", "module", "exports", "environment", `
  const { window, document, localStorage, MutationObserver, getComputedStyle, performance } = environment;
  ${componentBuild.outputFiles[0].text}
`)(name => name === "react" ? fakeReact : require(name), componentModule, componentModule.exports, environment);
const { HealthView, connectionKey, sharedRead, nextGeneration, latestGeneration } = componentModule.exports;
const flush = () => new Promise(setImmediate);
function nodes(tree, predicate) {
  if (Array.isArray(tree)) return tree.flatMap(n => nodes(n, predicate));
  if (!tree || typeof tree !== "object" || !tree.props) return [];
  return [...(predicate(tree) ? [tree] : []), ...Object.values(tree.props).flatMap(n => nodes(n, predicate))];
}
function healthHarness(initialAuto = null) {
  clock = 0;
  const slots = [], effects = [], requests = [];
  let cursor = 0, tree, interval = 5, memory = emptyHealth;
  const changed = (i, deps) => !slots[i] || deps.some((v, j) => !Object.is(v, slots[i].deps[j]));
  const hooks = {
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
      return [slots[i], v => { slots[i] = typeof v === "function" ? v(slots[i]) : v; }];
    },
    useRef(initial) { const i = cursor++; return slots[i] || (slots[i] = { current: initial }); },
    useMemo(fn, deps) { const i = cursor++; if (changed(i, deps)) slots[i] = { deps, value: fn() }; return slots[i].value; },
    useCallback(fn, deps) { return hooks.useMemo(() => fn, deps); },
    useEffect(fn, deps) {
      const i = cursor++;
      if (!changed(i, deps)) return;
      const previous = slots[i]; slots[i] = { deps };
      effects.push(() => { previous?.cleanup?.(); slots[i].cleanup = fn(); });
    },
  };
  let connection = { host: "health-unit", port: 3306, user: "reader", ssl_mode: "disabled" };
  const view = { offsetParent: {}, parentElement: null, querySelector: () => null };
  environment.localStorage.getItem = () => initialAuto;
  environment.preferences = { health_auto_production: healthAutoPreference(initialAuto), health_auto_development: -1 };
  environment.read = (path, body, signal) => {
    assert.equal(path, "live/health");
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    requests.push({ body, signal, resolve, reject });
    return promise;
  };
  const render = () => {
    currentHooks = hooks; cursor = 0;
    tree = HealthView({ connection, interval, onIntervalChange: v => { interval = v; }, memory,
      onMemory: update => { memory = update(memory); }, selection: { key: "", select() {} }, disabled: false, settings: {}, onOpenView() {} });
    for (const node of nodes(tree, n => !!n.ref)) node.ref.current = { closest: () => view };
    while (effects.length) effects.shift()();
    return tree;
  };
  return { get connection() { return connection; }, render, requests,
    switchConnection(next) { connection = next; return render(); },
    visible(value) {
      view.offsetParent = value ? {} : null;
      for (const observer of observers) observer.callback();
    },
    refreshAll() {
      const gen = nextGeneration("health");
      return sharedRead(connectionKey(connection), `health:${interval}`,
        signal => environment.read("live/health", { interval }, signal)).then(report => {
          if (latestGeneration("health") === gen)
            memory = rememberHealth(memory, { key: connectionKey(connection), label: "reader", report });
        });
    },
    refresh: () => tree.props.onRefresh(),
    select: (label, value) => {
      nodes(tree, n => n.props.label === label)[0].props.onChange(String(value)); render();
    },
    status: () => nodes(tree, n => n.type?.name === "ViewStatus")[0].props,
    get memory() { return memory; },
    close() { for (const slot of slots) slot?.cleanup?.(); },
  };
}

// A manual read supersedes an in-flight automatic or Refresh all read, even
// for the same window. A stale completion cannot clear the newest error or
// release its scheduler slot. Test both completion orders and both engines.
const healthFixtures = await Promise.all(["health-mysql-8.4.json", "health-mariadb-13.0.json"]
  .map(async name => JSON.parse(await readFile(new URL(`fixtures/${name}`, import.meta.url), "utf8"))));
for (const origin of ["automatic", "Refresh all"]) for (const fixture of healthFixtures) {
  for (const action of ["Refresh", "Window then Refresh", "Auto-refresh then Refresh", "Off then Refresh"]) for (const oldFirst of [true, false]) {
    const h = healthHarness();
    let all;
    try {
      if (origin === "Refresh all") {
        const gen = nextGeneration("health");
        all = sharedRead(connectionKey(h.connection), "health:5", signal => environment.read("live/health", { interval: 5 }, signal))
          .then(() => latestGeneration("health") === gen);
      }
      h.render();
      assert.equal(h.requests.length, 1);
      const older = h.requests[0];
      const beforeManual = latestGeneration("health");
      if (action !== "Refresh") {
        h.select(action === "Window then Refresh" ? "Window" : "Auto-refresh",
          action === "Window then Refresh" ? 10 : action === "Off then Refresh" ? 0 : 30);
        assert.equal(h.requests.length, 1, `${action}: changing the control does not read`);
        assert.equal(latestGeneration("health"), beforeManual, "a control change leaves the current generation intact");
      }
      h.refresh();
      assert.equal(h.requests.length, 2, `${origin}/${action}: a manual read starts immediately`);
      assert.ok(latestGeneration("health") > beforeManual, "a manual read invalidates the older Refresh all generation");
      assert.equal(older.signal.aborted, origin === "automatic",
        "abort an unshared read; Refresh all may keep waiting for its superseded answer");
      const newest = h.requests[1];
      assert.equal(newest.body.interval, action === "Window then Refresh" ? 10 : 5);
      const stale = { ...fixture, taken_at: sample.taken_at };
      if (oldFirst) {
        older.resolve(stale); await flush(); h.render();
        assert.ok(h.status().loading, "the newer read still owns loading");
        assert.equal(timers.size, 0, "an older completion never schedules another automatic read");
      }
      newest.reject(new Error("Newest Health read failed")); await flush(); h.render();
      assert.equal(h.status().error, "Newest Health read failed", "ViewStatus receives the newest error for its alert");
      if (!oldFirst) { older.resolve(stale); await flush(); h.render(); }
      assert.equal(h.memory.report, null, "an older answer cannot overwrite a newer failed read");
      assert.equal(h.status().error, "Newest Health read failed", "late success never clears the newest error");
      if (all) assert.equal(await all, false, "Refresh all cannot count a superseded Health read as refreshed");
      // Recover, then reject another superseded read: neither stale success
      // nor stale failure can replace the latest successful report.
      h.refresh();
      const replaced = h.requests.at(-1);
      h.refresh();
      const accepted = { ...stale, taken_at: "2026-10-27T14:05:00Z" };
      h.requests.at(-1).resolve(accepted); await flush(); h.render();
      replaced.reject(new Error("Stale failure")); await flush(); h.render();
      assert.equal(h.memory.report, accepted);
      assert.equal(h.status().error, "");
    } finally { h.close(); }
    assert.equal(timers.size, 0, "unmount clears all fake timers");
  }
}
console.log("health manual supersession unit: 32 cases passed");

// Controls change the next read's settings without making a request, including
// enabling automatic refresh from Off. Exercise idle and in-flight reads.
for (const fixture of healthFixtures) for (const initialAuto of ["0", "-1"]) {
  const h = healthHarness(initialAuto);
  try {
    h.render();
    if (initialAuto === "0") h.refresh();
    h.select("Window", 10);
    assert.equal(h.requests.length, 1, "Window change does not read while pending");
    h.select("Auto-refresh", 30);
    assert.equal(h.requests.length, 1, "Auto-refresh change does not read while pending");
    h.requests[0].resolve(fixture); await flush(); h.render();
    h.select("Window", 15);
    assert.equal(h.requests.length, 1, "Window change does not read while idle");
    h.select("Auto-refresh", 0);
    advance(120000);
    assert.equal(h.requests.length, 1, "Off cancels the automatic schedule");
    h.select("Auto-refresh", 10);
    assert.equal(h.requests.length, 1, "enabling Auto-refresh does not read");
    advance(9999); assert.equal(h.requests.length, 1);
    advance(1); assert.equal(h.requests.length, 2, "the new schedule reads after its interval");
    assert.equal(h.requests[1].body.interval, 15, "the next read uses the chosen Window");
    h.requests[1].resolve(fixture); await flush(); h.render();
  } finally { h.close(); }
  assert.equal(timers.size, 0);
}
console.log("health controls without reads: 4 cases passed");

// Refresh all can join Health's pending read or start while its timer waits.
// In either case its completion resets the automatic interval. A DOM mutation
// on completion or Stop is not a visibility return; only hiding then showing is.
for (const fixture of healthFixtures) for (const pending of [true, false]) {
  const h = healthHarness();
  try {
    h.render();
    if (!pending) {
      h.requests[0].resolve(fixture); await flush(); h.render();
      advance(4900);
    }
    const all = h.refreshAll();
    const count = h.requests.length;
    advance(pending ? 12000 : 50);
    assert.equal(h.requests.length, count, "Health joins a Refresh all read instead of duplicating it");
    h.requests.at(-1).resolve({ ...fixture, taken_at: "2026-10-27T14:06:00Z" });
    await all; await flush(); h.render();
    h.visible(true);
    advance(300);
    assert.equal(h.requests.length, count, "completion of a Refresh all read does not trigger an automatic read");
    advance(4699); assert.equal(h.requests.length, count);
    advance(1); assert.equal(h.requests.length, count + 1, "Refresh all completion waits the full interval");
    h.requests.at(-1).resolve({ ...fixture, taken_at: "2026-10-27T14:07:00Z" });
    await flush(); h.render();
    h.visible(false); advance(120000);
    assert.equal(h.requests.length, count + 1, "hidden Health sends no automatic reads");
    h.visible(true);
    assert.equal(h.requests.length, count + 2, "an actual visibility return reads once");
    h.visible(true);
    assert.equal(h.requests.length, count + 2, "another mutation while visible does not read");
    h.requests.at(-1).resolve(fixture); await flush(); h.render();
  } finally { h.close(); }
  assert.equal(timers.size, 0);
}
console.log("health Refresh all completion scheduling: 4 cases passed");

// A connection switch greys the report on screen: automatic ticks never
// replace it. The view's own Refresh reads the connection in use.
for (const fixture of healthFixtures) {
  const h = healthHarness();
  try {
    h.render();
    h.requests[0].resolve(fixture); await flush(); h.render();
    assert.equal(h.requests.length, 1);
    h.switchConnection({ host: "staging", port: 3307, user: "reader", ssl_mode: "disabled" });
    advance(120000);
    assert.equal(h.requests.length, 1, "a greyed report never reads by itself");
    h.refresh();
    assert.equal(h.requests.length, 2, "the view's own Refresh reads the connection in use");
    assert.equal(h.requests[1].body.connection.host, "staging");
  } finally { h.close(); }
  assert.equal(timers.size, 0);
}
console.log("health connection switch: 2 cases passed");

// The type, rather than the server identity, selects the stored refresh choice.
{
  const h = healthHarness("0");
  try {
    let tree = h.render();
    const auto = tree => nodes(tree, n => n.props.label === "Auto-refresh")[0].props.value;
    assert.equal(auto(tree), 0, "an omitted type uses Production");
    h.select("Auto-refresh", 30);
    tree = h.switchConnection({ ...h.connection, type: "development" });
    assert.equal(auto(tree), -1, "Development starts with its own default");
    h.select("Auto-refresh", 60);
    assert.equal(environment.preferences.health_auto_production, 30);
    assert.equal(environment.preferences.health_auto_development, 60);
    tree = h.switchConnection({ ...h.connection, type: "production" });
    assert.equal(auto(tree), 30, "switching back restores Production");
  } finally { h.close(); }
  assert.equal(timers.size, 0);
}
console.log("health per-type preference selection: passed");
