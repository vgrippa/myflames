// Find helpers, decorations and the controller with real EditorState, no DOM.
// Run from apps/workspace: node test/editor-find-unit.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { Worker as NodeWorker } from "node:worker_threads";
import { build } from "esbuild";
import ts from "typescript";

const root = fileURLToPath(new URL("..", import.meta.url));
const bundled = await build({
  stdin: { contents: `
    export { EditorState, EditorSelection, Transaction } from "@codemirror/state";
    export { EditorView, ViewPlugin } from "@codemirror/view";
    export { history, undo, redo, undoDepth, isolateHistory } from "@codemirror/commands";
    export * from "./src/editor/find";
    export * from "./src/editor/findMarks";
    export * from "./src/editor/FindBar";
  `, resolveDir: root },
  bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent",
  define: { "process.env.NODE_ENV": '"production"' },
});
const cm = await import("data:text/javascript;base64," + Buffer.from(bundled.outputFiles[0].text).toString("base64"));
const options = { matchCase: false, wholeWord: false, regex: false };
const regex = { ...options, regex: true };

// Compare the unchanged old helpers (frozen in the fixture) using editor-find.mjs's compiler boundary.
const source = await fs.readFile(new URL("./fixtures/legacy-find-helpers.tsx", import.meta.url), "utf8");
const oldExports = {};
new Function("exports", ts.transpileModule(source.slice(source.indexOf("export type Range =")), {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
}).outputText)(oldExports);
assert.equal(cm.FIND_WORKER_SOURCE, oldExports.FIND_WORKER_SOURCE, "worker source moved verbatim");
assert.equal(cm.FIND_MATCH_LIMIT, 100_000);
assert.equal(cm.FIND_MARK_LIMIT, 1000);
assert.equal(cm.FIND_REGEX_TIMEOUT_MS, 250);
for (const [text, query] of [
  ["İfoo", "foo"], ["İfoo and İFOO", "foo"], ["İfoo and İ foo", "foo"],
  ["select in inside tint", "in"], ["in _in in$ in1 🐬in 𐐀in in𐐀", "in"],
  ["foo\nFOO foo", "foo"], ["a1 b22 c333", "\\d+"], ["aaaa", "a*"],
  ["aaa", "\\b"], ["aaa", "(?=a)"], ["🐬X🐬", "🐬"], ["[] .* $&", ".*"],
  ["a", "["], ["", "a"], ["text", ""], ["a\nb", "^|$"],
]) {
  for (const matchCase of [false, true]) for (const wholeWord of [false, true]) for (const isRegex of [false, true]) {
    const opts = { matchCase, wholeWord, regex: isRegex };
    assert.deepEqual(cm.computeFind(text, query, opts), oldExports.computeFind(text, query, opts), JSON.stringify({ text, query, opts }));
  }
}
assert.deepEqual(cm.computeFind("İfoo", "foo", options), { matches: [{ start: 1, end: 4 }], error: "", capped: false });
assert.deepEqual(cm.computeFind("İfoo and İFOO", "foo", options).matches, [{ start: 1, end: 4 }, { start: 10, end: 13 }]);
assert.deepEqual(cm.computeFind("İfoo and İ foo", "foo", { ...options, wholeWord: true }).matches, [{ start: 11, end: 14 }]);
assert.deepEqual(cm.computeFind("in _in in$ in1 🐬in 𐐀in in𐐀", "in", { ...options, wholeWord: true }).matches,
  [{ start: 0, end: 2 }, { start: 17, end: 19 }], "supplementary letters are word characters, emoji are not");
for (const opts of [options, regex]) {
  const capped = cm.computeFind("a".repeat(cm.FIND_MATCH_LIMIT + 1), "a", opts);
  assert.equal(capped.matches.length, cm.FIND_MATCH_LIMIT);
  assert.equal(capped.capped, true);
  assert.deepEqual(capped, oldExports.computeFind("a".repeat(cm.FIND_MATCH_LIMIT + 1), "a", opts));
}
assert.deepEqual(cm.computeFind("aaa", "(?=a)", regex), { matches: [], error: "", capped: false });
assert.ok(cm.computeFind("a", "[", regex).error);
assert.equal(cm.firstMatchAt([{ start: 2, end: 3 }, { start: 8, end: 9 }], 3), 1);
assert.equal(cm.firstMatchAt([{ start: 2, end: 3 }, { start: 8, end: 9 }], 9), 0);
assert.equal(cm.firstMatchAt([], 9), 0);

const terminations = [];
let terminatedCount = 0;
function spawnNodeWorker() {
  const worker = new NodeWorker(`
    const { parentPort } = require("node:worker_threads");
    const self = { postMessage: msg => parentPort.postMessage(msg) };
    ${cm.FIND_WORKER_SOURCE}
    parentPort.on("message", data => self.onmessage({ data }));
  `, { eval: true });
  let onmessage = null, onerror = null;
  worker.on("message", data => onmessage?.({ data }));
  worker.on("error", error => onerror?.({ message: error.message }));
  return {
    postMessage: data => worker.postMessage(data),
    terminate() { terminatedCount++; terminations.push(worker.terminate()); },
    set onmessage(fn) { onmessage = fn; }, get onmessage() { return onmessage; },
    set onerror(fn) { onerror = fn; }, get onerror() { return onerror; },
  };
}
const finder = cm.createRegexFinder(spawnNodeWorker);
try {
  const started = Date.now();
  const slow = await new Promise(resolve => finder.search(`${"a".repeat(35)}!`, "(a+)+$", regex, resolve));
  assert.deepEqual(slow, { matches: [], error: cm.FIND_SLOW_PATTERN_ERROR, capped: false });
  assert.equal(terminatedCount, 1, "the stuck regex worker is terminated");
  assert.ok(Date.now() - started < 1000, "regex timeout leaves the main thread responsive");
  const fast = await new Promise(resolve => finder.search("a1 b22 c333", "\\d+", regex, resolve));
  assert.deepEqual(fast, { matches: [{ start: 1, end: 2 }, { start: 4, end: 6 }, { start: 8, end: 11 }], error: "", capped: false });
  for (const [text, query] of [["aaa", "(?=a)"], ["foo foo!", "foo"], ["İfoo and İ foo", "foo"], ["a", "["]]) {
    const opts = { ...regex, wholeWord: true };
    const result = await new Promise(resolve => finder.search(text, query, opts, resolve));
    assert.deepEqual(result, cm.computeFind(text, query, opts), "worker regex semantics match the legacy helpers");
  }
  let delivered = false;
  const cancel = finder.search(`${"a".repeat(35)}!`, "(a+)+$", regex, () => { delivered = true; });
  cancel();
  const fresh = await new Promise(resolve => finder.search("b22", "\\d+", regex, resolve));
  assert.equal(delivered, false, "canceled searches never publish");
  assert.deepEqual(fresh.matches, [{ start: 1, end: 3 }]);
} finally {
  finder.dispose();
  await Promise.all(terminations);
}

const stateFor = (doc, selection = { anchor: 0 }, extensions = []) => cm.EditorState.create({
  doc, selection, extensions: [cm.history(), cm.findExtension(), ...extensions],
});
function target(state) {
  const view = { state, transactions: [], dispatch(...specs) {
    const tr = specs[0] instanceof cm.Transaction ? specs[0] : view.state.update(...specs);
    view.transactions.push(tr);
    view.state = tr.state;
  } };
  return view;
}
function marks(state) {
  const result = [];
  for (let i = state.field(cm.findMarkState).decorations.iter(); i.value; i.next()) result.push([i.from, i.to, i.value.spec.class]);
  return result;
}
function installMatches(state, matches, current = 0) {
  return state.update({ effects: cm.setFindMatches.of({ matches, current }) }).state;
}
{
  const view = target(stateFor("foo foo"));
  const first = cm.planReplace("foo foo", cm.computeFind("foo foo", "foo", options).matches, 0, "foobar", false);
  assert.deepEqual(first, { changes: [{ from: 0, to: 3, insert: "foobar" }], anchor: 6, undoAnchor: 0, selection: 6 });
  view.dispatch({ changes: first.changes, selection: { anchor: first.selection }, annotations: cm.isolateHistory.of("full"), userEvent: "input.replace" });
  assert.equal(view.state.doc.toString(), "foobar foo");
  const matches = cm.computeFind(view.state.doc.toString(), "foo", options).matches;
  assert.equal(cm.firstMatchAt(matches, first.anchor), 1, "advance past inserted text containing the query");
  const second = cm.planReplace(view.state.doc.toString(), matches, 1, "foobar", false);
  view.dispatch({ changes: second.changes, annotations: cm.isolateHistory.of("full"), userEvent: "input.replace" });
  assert.equal(view.state.doc.toString(), "foobar foobar");
  assert.equal(cm.undoDepth(view.state), 2, "two replacements cannot merge");
  assert.equal(cm.undo(view), true);
  assert.equal(view.state.doc.toString(), "foobar foo");
  assert.equal(cm.undo(view), true);
  assert.equal(view.state.doc.toString(), "foo foo");
  assert.ok(view.transactions.at(-1).isUserEvent("undo"));
}
for (const replacement of ["$&$1", "", "long\ntext", "🐬"]) {
  const view = target(stateFor("İfoo and İFOO"));
  const matches = cm.computeFind(view.state.doc.toString(), "foo", options).matches;
  const plan = cm.planReplace(view.state.doc.toString(), matches, 1, replacement, true);
  assert.equal(plan.changes.length, 2);
  assert.equal(plan.anchor, 1);
  assert.equal(plan.undoAnchor, 1);
  assert.equal(plan.selection, 1 + replacement.length);
  view.dispatch({ changes: plan.changes, selection: { anchor: plan.selection }, annotations: cm.isolateHistory.of("full"), userEvent: "input.replace" });
  assert.equal(view.transactions.length, 1, "Replace all is a single transaction");
  assert.equal(view.state.doc.toString(), `İ${replacement} and İ${replacement}`);
  assert.equal(cm.undoDepth(view.state), 1);
  assert.equal(cm.undo(view), true);
  assert.equal(view.state.doc.toString(), "İfoo and İFOO");
}
// Port the original sequential Unicode Replace check, not only Replace all.
{
  const view = target(stateFor("İfoo and İFOO"));
  let anchor = 0;
  for (const expected of ["İbar and İFOO", "İbar and İbar"]) {
    const text = view.state.doc.toString();
    const matches = cm.computeFind(text, "foo", options).matches;
    const plan = cm.planReplace(text, matches, cm.firstMatchAt(matches, anchor), "bar", false);
    view.dispatch({ changes: plan.changes, annotations: cm.isolateHistory.of("full") });
    anchor = plan.anchor;
    assert.equal(view.state.doc.toString(), expected);
  }
}
assert.equal(cm.planReplace("a", [], 0, "x", false), null);
assert.equal(cm.planReplace("a", [{ start: -1, end: 1 }], 0, "x", true), null);
assert.equal(cm.planReplace("abc", [{ start: 0, end: 2 }, { start: 1, end: 3 }], 0, "x", true), null);
assert.equal(cm.planReplace("a", [{ start: 0, end: 2 }], 0, "x", false), null);

// Map edits before, after, inside and across matches. Current marks stay capped.
{
  let state = installMatches(stateFor("foo foo foo"), [{ start: 0, end: 3 }, { start: 4, end: 7 }, { start: 8, end: 11 }], 1);
  assert.deepEqual(marks(state), [[0, 3, "sql-find-mark"], [4, 7, "sql-find-mark sql-find-current"], [8, 11, "sql-find-mark"]]);
  state = state.update({ changes: [{ from: 0, insert: "x" }, { from: 5, insert: "!" }] }).state;
  assert.deepEqual(state.field(cm.findMarkState).matches, [{ start: 1, end: 4 }, { start: 10, end: 13 }]);
  assert.equal(state.field(cm.findMarkState).current, 1, "an invalidated current range advances to a surviving successor");
  state = state.update({ changes: { from: 2, to: 3, insert: "z" } }).state;
  assert.deepEqual(state.field(cm.findMarkState).matches, [{ start: 10, end: 13 }]);
  state = state.update({ changes: { from: 0, to: state.doc.length } }).state;
  assert.deepEqual(marks(state), [], "full document replacement invalidates all ranges");
}
for (const changes of [
  { from: 0, insert: "!" }, { from: 3, insert: "!" }, { from: 7, insert: "!" },
  [{ from: 0, insert: "!!" }, { from: 3, to: 4 }, { from: 7, insert: "!" }],
]) {
  const start = installMatches(stateFor("foo foo"), [{ start: 0, end: 3 }, { start: 4, end: 7 }]);
  const tr = start.update({ changes });
  for (const match of tr.state.field(cm.findMarkState).matches) {
    assert.equal(tr.state.doc.sliceString(match.start, match.end), "foo", "mapped offsets still cover original text");
  }
}
{
  const doc = "a ".repeat(1500), matches = cm.computeFind(doc, "a", options).matches;
  let state = installMatches(stateFor(doc), matches);
  assert.equal(marks(state).length, 1000);
  state = state.update({ effects: cm.setFindCurrent.of(1499) }).state;
  assert.equal(marks(state).length, 1000, "a distant current match replaces one ordinary mark");
  assert.deepEqual(marks(state).at(-1), [2998, 2999, "sql-find-mark sql-find-current"]);
  state = state.update({ effects: cm.clearFindMarks.of(null) }).state;
  assert.deepEqual(marks(state), []);
  const big = installMatches(stateFor("x".repeat(1_000_000) + "needle"), [{ start: 1_000_000, end: 1_000_006 }]);
  assert.deepEqual(marks(big), [[1_000_000, 1_000_006, "sql-find-mark sql-find-current"]], "marks ignore the syntax size limit");
  const hostile = '<img src=x onerror="alert(1)">';
  const attack = installMatches(stateFor(hostile), [{ start: 0, end: hostile.length }]);
  assert.deepEqual(attack.field(cm.findMarkState).decorations.iter().value.spec,
    { class: "sql-find-mark sql-find-current" }, "untrusted text cannot supply decoration attributes");
}
{
  const doc = stateFor("a\n🐬\n").doc;
  for (const [raw, expected] of [["", null], ["  ", null], ["oops", null], [NaN, null], ["-4", 1], ["0", 1], ["1", 1], ["2.9", 2], ["3", 3], ["999", 3], [Infinity, 3], [-Infinity, 1]]) {
    assert.equal(cm.clampGoToLine(raw, doc.lines), expected, String(raw));
  }
  assert.deepEqual(cm.resolveGoToLine(doc, "2"), { line: 2, from: 2 });
  assert.deepEqual(cm.resolveGoToLine(doc, "999"), { line: 3, from: 5 });
  assert.equal(cm.resolveGoToLine(doc, "no"), null);
  assert.deepEqual(cm.resolveGoToLine(stateFor("").doc, "500"), { line: 1, from: 0 });
}

// Deterministic controller harness: production view plugin callbacks, no DOM.
// Reject dispatch during plugin updates, matching EditorView's reentrancy guard.
const originalSetTimeout = globalThis.setTimeout, originalClearTimeout = globalThis.clearTimeout;
const timers = new Map();
let timerId = 0;
globalThis.setTimeout = (fn, delay) => { const id = ++timerId; timers.set(id, { fn, delay }); return id; };
globalThis.clearTimeout = id => { timers.delete(id); };
const flush = async () => {
  const pending = [...timers.values()];
  timers.clear();
  for (const { fn } of pending) fn();
  await Promise.resolve();
};
const harnesses = [];
function harness(doc, selection = { anchor: 0 }, extensions = [], makeFinder) {
  const pluginSpec = cm.findExtension().find(item => item instanceof cm.ViewPlugin);
  let plugin, updating = false;
  const view = {
    state: stateFor(doc, selection, extensions), focused: 0, transactions: [],
    focus() { view.focused++; },
    dispatch(...specs) {
      assert.equal(updating, false, "no dispatch while a plugin is updating");
      const tr = specs[0] instanceof cm.Transaction ? specs[0] : view.state.update(...specs);
      view.state = tr.state;
      view.transactions.push(tr);
      updating = true;
      try { plugin.update({ view, docChanged: tr.docChanged, transactions: [tr] }); }
      finally { updating = false; }
    },
    setState(state) {
      updating = true;
      try { plugin.destroy(); view.state = state; plugin = pluginSpec.create(view); }
      finally { updating = false; }
    },
  };
  plugin = pluginSpec.create(view);
  const snapshots = [];
  const controller = new cm.FindController(view, snapshot => snapshots.push(snapshot), makeFinder);
  const result = { view, controller, snapshots, destroy() { controller.dispose(); plugin.destroy(); } };
  harnesses.push(result);
  return result;
}
try {
  const a = harness("foo foo\nFOO", { anchor: 4, head: 7 }), b = harness("bar");
  assert.equal(cm.findHooks.isOpen(a.view), false);
  assert.equal(cm.findHooks.openFind(a.view, "replace"), true);
  assert.equal(cm.findHooks.isOpen(a.view), true);
  assert.equal(cm.findHooks.isOpen(b.view), false, "hooks belong to each view");
  assert.equal(a.controller.snapshot.query, "foo");
  assert.equal(a.controller.snapshot.current, 1, "prefill starts at selected match");
  await Promise.resolve();
  assert.equal(marks(a.view.state).length, 3, "deferred invalidation cannot erase a synchronous search");
  cm.findHooks.next(a.view);
  assert.equal(a.controller.snapshot.current, 2);
  cm.findHooks.next(a.view);
  assert.equal(a.controller.snapshot.current, 0, "next wraps");
  cm.findHooks.previous(a.view);
  assert.equal(a.controller.snapshot.current, 2, "previous wraps");
  a.controller.close();
  assert.equal(a.view.focused, 1);
  assert.equal(a.view.state.selection.main.from, 8);
  assert.deepEqual(marks(a.view.state), []);
  a.view.dispatch({ selection: { anchor: 0, head: 8 } });
  cm.findHooks.openFind(a.view, "find");
  assert.equal(a.controller.snapshot.query, "foo", "multiline selection does not seed query");

  const replace = harness("foo foo");
  cm.findHooks.openFind(replace.view, "replace");
  replace.controller.setQuery("foo");
  await flush();
  replace.controller.setReplacement("foobar");
  replace.controller.replace(false);
  await flush();
  assert.equal(replace.view.state.doc.toString(), "foobar foo");
  assert.equal(replace.controller.snapshot.current, 1);
  assert.equal(replace.view.state.selection.main.from, 7);
  assert.equal(cm.undo(replace.view), true);
  await flush();
  assert.equal(replace.view.state.doc.toString(), "foo foo");
  assert.equal(replace.controller.snapshot.current, 0, "undo restores the replaced-match anchor");
  cm.findHooks.next(replace.view);
  replace.controller.setReplacement("bar");
  replace.controller.replace(false);
  cm.undo(replace.view); // Undo before the replacement's debounce has fired.
  await flush();
  assert.equal(replace.controller.snapshot.current, 1, "immediate undo restores a nonzero replacement anchor");
  assert.equal(replace.view.state.selection.main.from, 4);
  replace.controller.setReplacement("$&$1");
  replace.controller.replace(true);
  await flush();
  assert.equal(replace.view.state.doc.toString(), "$&$1 $&$1");
  assert.equal(cm.undoDepth(replace.view.state), 1);
  cm.undo(replace.view);
  await flush();
  assert.equal(replace.view.state.doc.toString(), "foo foo");
  assert.equal(replace.controller.snapshot.current, 0);
  replace.view.dispatch({ changes: { from: 0, insert: "prefix " }, userEvent: "input.type" });
  replace.controller.setReplacement("ok");
  replace.controller.replace(false);
  assert.equal(replace.view.state.doc.toString(), "prefix ok foo", "literal Replace recomputes during debounce");
  await flush();

  const readOnly = harness("foo", { anchor: 0, head: 3 }, [cm.EditorState.readOnly.of(true)]);
  cm.findHooks.openFind(readOnly.view, "replace");
  readOnly.controller.setReplacement("bad");
  readOnly.controller.replace(true);
  assert.equal(readOnly.view.state.doc.toString(), "foo");

  const jobs = [];
  const async = harness("a1 b22", { anchor: 0 }, [], () => ({
    search(text, query, opts, done) {
      const job = { text, query, opts, done, canceled: false };
      jobs.push(job);
      return () => { job.canceled = true; };
    }, dispose() {},
  }));
  cm.findHooks.openFind(async.view, "replace");
  async.controller.toggleOption("regex");
  async.controller.setQuery("\\d+");
  await flush();
  const stale = jobs.at(-1);
  async.controller.setReplacement("x");
  async.controller.replace(true);
  assert.equal(async.view.state.doc.toString(), "a1 b22", "pending regex cannot replace");
  async.view.dispatch({ changes: { from: 0, insert: "z" }, userEvent: "input.type" });
  assert.equal(stale.canceled, true);
  stale.done(cm.computeFind(stale.text, stale.query, stale.opts));
  assert.deepEqual(async.controller.snapshot.found.matches, [], "late results from old documents are ignored");
  await flush();
  const latest = jobs.at(-1);
  latest.done(cm.computeFind(latest.text, latest.query, latest.opts));
  async.controller.focusFind("replace");
  assert.equal(async.controller.snapshot.query, "\\d+", "opening Replace from the bar preserves a regex query");
  async.controller.replace(true);
  assert.equal(async.view.state.doc.toString(), "zax bx");
  await flush();
  const switchedJob = jobs.at(-1);
  async.view.setState(stateFor("new tab"));
  assert.equal(cm.findHooks.isOpen(async.view), false, "setState closes the bar synchronously");
  switchedJob.done({ matches: [{ start: 0, end: 1 }], error: "", capped: false });
  await flush();
  assert.deepEqual(marks(async.view.state), []);
  assert.equal(async.view.state.doc.toString(), "new tab");

  const line = harness("one\ntwo\nthree", { anchor: 5 });
  cm.findHooks.openGoToLine(line.view);
  assert.equal(line.controller.snapshot.mode, "line");
  assert.equal(line.controller.snapshot.line, "2");
  assert.equal(line.controller.snapshot.lineCount, 3);
  assert.equal(cm.findHooks.isOpen(line.view), false, "Go to line does not capture Ctrl+G as next match");
  line.controller.setLine("999");
  line.controller.goToLine();
  assert.equal(line.view.state.selection.main.from, 8);
  assert.equal(line.view.state.selection.main.empty, true);
  assert.equal(line.controller.snapshot.mode, false);
  assert.equal(line.view.focused, 1);
  assert.ok(line.view.transactions.some(tr => tr.effects.some(effect => effect.value?.y === "center")), "jump scrolls to center");
  cm.findHooks.openGoToLine(line.view);
  line.controller.setLine("");
  line.controller.goToLine();
  assert.equal(line.controller.snapshot.mode, "line", "blank input keeps the bar open");
  line.controller.setLine("-100");
  line.controller.goToLine();
  assert.equal(line.view.state.selection.main.from, 0);

  const saved = a.view.state;
  a.view.setState(stateFor("different"));
  a.view.setState(saved);
  await Promise.resolve();
  assert.equal(a.controller.snapshot.mode, false);
  assert.deepEqual(marks(a.view.state), [], "restoring a tab cannot resurrect saved marks");
} finally {
  for (const h of harnesses) h.destroy();
  assert.equal(timers.size, 0, "controllers leave no pending timers");
  globalThis.setTimeout = originalSetTimeout;
  globalThis.clearTimeout = originalClearTimeout;
}
assert.equal(cm.findHooks.openFind({}, "find"), false);
assert.equal(cm.findHooks.next({}), false);
console.log("Editor find unit: legacy parity, Unicode offsets, worker timeout/recovery/cancel, replacement undo, mapped/capped marks, hooks, stale searches, tab switches and Go to line passed.");
