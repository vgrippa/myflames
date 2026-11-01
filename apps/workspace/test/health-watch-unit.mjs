// Capture when… without a browser: judging a watch-1.0 read, a connection
// switch, and times on this computer's clock (Health.tsx).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { build } from "esbuild";

// UTC-3 all year: 14:02:31Z reads 11:02:31 here.
process.env.TZ = "America/Sao_Paulo";
const result = await build({
  stdin: {
    contents: `export { judgeWatch, switchHealthConnection, rememberHealth, clockTime, localMinute, watchConditions, emptyHealth, groupStatusText, pendingTrendText } from './src/Health'; export { trendRange } from './src/kit';`,
    loader: "tsx", resolveDir: new URL("..", import.meta.url).pathname,
  },
  bundle: true, platform: "node", format: "cjs", write: false,
  jsx: "automatic", loader: { ".css": "empty", ".svg": "text" },
  external: ["react", "react-dom", "react-dom/server"], logLevel: "silent",
});
const out = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(createRequire(import.meta.url), out, out.exports);
const { judgeWatch, switchHealthConnection, rememberHealth, clockTime, localMinute, watchConditions, emptyHealth, groupStatusText,
  pendingTrendText, trendRange } = out.exports;

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
assert.equal(pendingTrendText(0), "The chart starts with the next reading.");
assert.equal(pendingTrendText(30), "The chart starts with the next reading (every 30 s).");
// A tile group's share of the summary's count, empty when all is well.
assert.equal(groupStatusText(["ok", "watch", "problem", "info"]), "1 problem, 1 to watch");
assert.equal(groupStatusText(["problem", "problem", "unknown"]), "2 problems");
assert.equal(groupStatusText(["watch", "watch"]), "2 to watch");
assert.equal(groupStatusText(["ok", "info", "unknown"]), "");

console.log("health watch unit: ok");
