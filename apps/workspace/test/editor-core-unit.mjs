// Pure EditorState and command tests. No browser, server or generated test files.
// Run from apps/workspace: node test/editor-core-unit.mjs
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

globalThis.__editorTokenizeCalls = 0;
globalThis.__editorTestMac = false;
const root = fileURLToPath(new URL("..", import.meta.url));
const tokenizer = fileURLToPath(new URL("../src/sqlHighlight.ts", import.meta.url));
const bundled = await build({
  stdin: { contents: `
    export { EditorState, EditorSelection, Transaction, Compartment } from "@codemirror/state";
    export { history, undo, redo, undoDepth, isolateHistory } from "@codemirror/commands";
    export { syntaxTree } from "@codemirror/language";
    export { autocompletion } from "@codemirror/autocomplete";
    export * from "./src/editor/SqlEditor";
    export * from "./src/editor/sqlLanguage";
    export * from "./src/editor/marks";
    export * from "./src/editor/keymap";
    export * from "./src/editor/automation";
    export * from "./src/editor/commands";
    export * from "./src/editor/types";
  `, resolveDir: root },
  bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent",
  define: { "process.env.NODE_ENV": '"production"' },
  plugins: [{ name: "tokenize-spy", setup(b) {
    b.onResolve({ filter: /^\.\.\/platform$/ }, () => ({ path: "platform", namespace: "test-platform" }));
    b.onLoad({ filter: /.*/, namespace: "test-platform" }, () => ({ contents: "export const MAC = globalThis.__editorTestMac;" }));
    b.onResolve({ filter: /^\.\.\/sqlHighlight$/ }, () => ({ path: "tokenizer", namespace: "spy" }));
    b.onLoad({ filter: /.*/, namespace: "spy" }, () => ({ contents: `
      export * from ${JSON.stringify(tokenizer)};
      import { tokenize as original } from ${JSON.stringify(tokenizer)};
      export function tokenize(sql) { globalThis.__editorTokenizeCalls++; return original(sql); }
    `, resolveDir: root }));
  } }],
});
const cm = await import("data:text/javascript;base64," + Buffer.from(bundled.outputFiles[0].text).toString("base64"));
const allSettings = { multi_cursor: true, completion_auto: true, close_brackets: true, soft_wrap: false, problem_warnings: true, hover_info: true };
const stateFor = (doc, selection = { anchor: 0 }, extensions = []) => cm.EditorState.create({ doc, selection,
  extensions: [cm.EditorState.allowMultipleSelections.of(true), cm.history(), cm.sqlLanguage, cm.sqlMarks(), ...extensions] });
function target(initial) {
  const view = { state: initial, dispatch(...specs) {
    view.state = specs[0] instanceof cm.Transaction ? specs[0].state : view.state.update(...specs).state;
  } };
  return view;
}
function decorations(set) {
  const result = [];
  for (let i = set.iter(); i.value; i.next()) result.push([i.from, i.to, i.value.spec.class]);
  return result;
}
function gutters(state) {
  const result = [];
  for (let i = state.field(cm.markState).gutters.iter(); i.value; i.next()) result.push([i.from, i.value.elementClass]);
  return result;
}

// External replacement is minimal, maps both selections and cannot join user input.
{
  const original = "SELECT name FROM t";
  const view = target(stateFor(original, cm.EditorSelection.create([
    cm.EditorSelection.cursor(3), cm.EditorSelection.range(12, original.length),
  ], 1)));
  view.dispatch({ changes: { from: original.length, insert: ";" }, userEvent: "input.type" });
  const before = view.state, beforeText = before.doc.toString();
  const spec = cm.externalValueTransaction(before, beforeText.replace("name", "long_name"));
  assert.ok(spec);
  const tr = before.update(spec);
  assert.equal(tr.annotation(cm.External), true);
  assert.equal(tr.annotation(cm.isolateHistory), "full");
  const changes = [];
  tr.changes.iterChanges((from, to, newFrom, newTo, insert) => changes.push([from, to, newFrom, newTo, insert.toString()]));
  assert.deepEqual(changes, [[7, 7, 7, 12, "long_"]]);
  view.dispatch(tr);
  assert.equal(view.state.selection.ranges[0].head, 3);
  assert.equal(view.state.selection.main.from, before.selection.main.from + 5);
  assert.equal(view.state.selection.main.to, before.selection.main.to + 5);
  assert.equal(cm.undoDepth(view.state), 2);
  assert.equal(cm.undo(view), true);
  assert.equal(view.state.doc.toString(), beforeText);
  assert.ok(view.state.selection.eq(before.selection));
  assert.equal(cm.undo(view), true);
  assert.equal(view.state.doc.toString(), original);
  assert.equal(cm.redo(view), true);
  assert.equal(cm.redo(view), true);
  assert.equal(view.state.doc.toString(), "SELECT long_name FROM t;");
  assert.equal(cm.externalValueTransaction(view.state, view.state.doc.toString()), null);
}
for (const [before, after] of [["", "SELECT 1"], ["SELECT 1", ""], ["abc", "ab"], ["🐬x", "🐬newx"], ["same", "other"]]) {
  const view = target(stateFor(before));
  view.dispatch(cm.externalValueTransaction(view.state, after));
  assert.equal(view.state.doc.toString(), after);
  assert.equal(cm.undoDepth(view.state), 1);
  assert.equal(cm.undo(view), true);
  assert.equal(view.state.doc.toString(), before);
}

// The same store used by the component retains independent undo stacks and scroll.
{
  const store = cm.createDocumentStore(), a = target(stateFor("A")), b = target(stateFor("B"));
  a.dispatch({ changes: { from: 1, insert: " one" }, userEvent: "input.type" });
  b.dispatch({ changes: { from: 1, insert: " two" }, userEvent: "input.type" });
  const scroll = { top: 41, left: 9 };
  store.save("a", a.state, scroll);
  scroll.top = 900;
  store.save("b", b.state, { top: 12, left: 0 });
  assert.equal(store.restore("a").state, a.state);
  assert.deepEqual(store.restore("a").scroll, { top: 41, left: 9 });
  assert.equal(store.size, 2);
  const restoredA = target(store.restore("a").state), restoredB = target(store.restore("b").state);
  assert.equal(cm.undo(restoredA), true);
  assert.equal(restoredA.state.doc.toString(), "A");
  assert.equal(restoredB.state.doc.toString(), "B two");
  assert.equal(cm.undo(restoredB), true);
  assert.equal(restoredB.state.doc.toString(), "B");
  store.prune(["b"]);
  assert.equal(store.restore("a"), undefined);
  assert.equal(store.size, 1);
  store.prune([]);
  assert.equal(store.size, 0);
}

// Reserved shell shortcuts are absent on both platforms; settings gate all rows.
{
  const reserved = new Set(["Mod-Enter", "Mod-[", "Mod-]", "Mod-i", "Mod-Alt-\\"]);
  for (const row of cm.EDITOR_SHORTCUTS) {
    assert.ok(!reserved.has(row.mac));
    assert.ok(!reserved.has(row.other));
  }
  const enabled = cm.editorKeymap(allSettings);
  for (const binding of enabled) {
    for (const platform of ["key", "mac", "linux", "win"]) assert.ok(!reserved.has(binding[platform]));
    assert.ok(!["Alt-ArrowLeft", "Alt-ArrowRight"].includes(binding.key));
  }
  const disabled = cm.editorKeymap({ ...allSettings, multi_cursor: false, problem_warnings: false });
  for (const row of cm.EDITOR_SHORTCUTS.filter(row => row.setting)) {
    assert.ok(!disabled.some(binding => binding.key === row.other || (row.mac && binding.mac === row.mac)), row.id);
  }
  const deleteLine = enabled.find(binding => binding.key === "Shift-Mod-k");
  assert.equal(deleteLine.stopPropagation, true);
  assert.equal(deleteLine.preventDefault, true);
  assert.equal(enabled.filter(binding => binding.key === "Escape").length, 1);
  const one = target(stateFor("SELECT 1", { anchor: 0, head: 6 }));
  assert.equal(cm.escapeEditor(one), false, "one nonempty selection must still reach Stop");
  const many = target(stateFor("SELECT 1", cm.EditorSelection.create([cm.EditorSelection.cursor(1), cm.EditorSelection.range(4, 6)], 1)));
  assert.equal(cm.escapeEditor(many), true);
  assert.equal(many.state.selection.ranges.length, 1);
  assert.deepEqual(cm.mainSelection(many.state), { start: 4, end: 6 });

  // Manual completion works even when typing activation is disabled.
  const manual = target(stateFor("SEL", { anchor: 3 }, [cm.autocompletion({ activateOnTyping: false, defaultKeymap: false, override: [() => null] })]));
  for (const key of ["Ctrl-Space", "Shift-Ctrl-Space"]) {
    assert.equal(cm.editorKeymap({ ...allSettings, completion_auto: false }).find(binding => binding.key === key).run(manual), true);
    assert.equal(cm.escapeEditor(manual), true);
    assert.equal(cm.escapeEditor(manual), false);
  }
  const enter = target(stateFor("  SELECT (", { anchor: 10 }));
  assert.equal(enabled.filter(binding => binding.key === "Enter").at(-1).run(enter), true);
  assert.equal(enter.state.doc.toString(), "  SELECT (\n  ");

  // Off Mac Ctrl+G shares navigation and Go to line without duplicate bindings.
  let findOpen = false, goto = 0;
  const next = cm.editorKeymap(allSettings, { findIsOpen: () => findOpen, goToLine: () => { goto++; return true; } })
    .find(binding => binding.key === "Mod-g");
  assert.equal(next.run(one), true);
  assert.equal(goto, 1);
  findOpen = true;
  assert.equal(next.run(one), false, "FindBar stub owns next match when open");
  assert.equal(goto, 1);

  globalThis.__editorTestMac = true;
  const mac = await import("data:text/javascript;base64," + Buffer.from(bundled.outputFiles[0].text).toString("base64") + "#mac");
  const macBindings = mac.editorKeymap(allSettings);
  assert.equal(macBindings.filter(binding => (binding.mac || binding.key) === "Mod-Alt-f").length, 1);
  assert.ok(macBindings.some(binding => binding.mac === "Ctrl-g"));
  assert.ok(macBindings.some(binding => binding.mac === "Shift-Alt-m"));
  assert.equal(macBindings.find(binding => binding.mac === "Shift-Mod-k").stopPropagation, true);
  globalThis.__editorTestMac = false;
}

// Real multi-cursor search commands select matching occurrences.
{
  const view = target(stateFor("x x x", { anchor: 0, head: 1 }));
  assert.equal(cm.selectNextOccurrenceCmd(view), true);
  assert.equal(view.state.selection.ranges.length, 2);
  assert.equal(cm.selectAllOccurrencesCmd(view), false, "CodeMirror requires one nonempty selection for select-all matches");
  cm.escapeEditor(view);
  assert.equal(cm.selectAllOccurrencesCmd(view), true);
  assert.equal(view.state.selection.ranges.length, 3);
}

// Exact statement, flash, error and gutter ranges, including mapped edits.
{
  let state = stateFor("SELECT 1;\nSELECT 'x';\n");
  state = state.update({ effects: [cm.setStatement.of({ start: 0, end: 10 }), cm.setFlash.of({ start: 10, end: 20 }), cm.setError.of({ start: 17, end: 18 })] }).state;
  assert.deepEqual(decorations(state.field(cm.markState).decorations), [
    [0, 10, "sql-current"], [10, 20, "sql-flash"], [17, 18, "sql-error-mark"],
  ]);
  assert.deepEqual(gutters(state), [[0, "is-current"], [10, "is-current is-error"]]);
  const before = state.field(cm.markState);
  state = state.update({ selection: { anchor: 5 } }).state;
  assert.equal(state.field(cm.markState), before, "cursor motion does not rebuild statement/gutter marks");
  state = state.update({ changes: { from: 0, insert: "--\n" } }).state;
  assert.deepEqual(decorations(state.field(cm.markState).decorations), [[3, 13, "sql-current"], [13, 23, "sql-flash"], [20, 21, "sql-error-mark"]]);
  state = state.update({ effects: cm.setFlash.of(null) }).state;
  assert.ok(!decorations(state.field(cm.markState).decorations).some(range => range[2] === "sql-flash"));
  assert.equal(cm.FLASH_MS, 1600);
}

for (const [sql, carets, expected] of [
  ["'abc'", [0, 1, 4, 5], [[0, 1, "sql-match"], [4, 5, "sql-match"]]],
  ["`a``b`", [0, 1, 5, 6], [[0, 1, "sql-match"], [5, 6, "sql-match"]]],
  ['"a""b"', [0, 1, 5, 6], [[0, 1, "sql-match"], [5, 6, "sql-match"]]],
  ["'open", [0, 1, 5], []], ["`open", [0, 1, 5], []], ["/* 'abc' */", [3, 4, 7, 8], []],
]) {
  let state = stateFor(sql);
  const calls = globalThis.__editorTokenizeCalls;
  assert.ok(calls > 0, "the tokenizer spy is active");
  for (const caret of carets) {
    state = state.update({ selection: { anchor: caret } }).state;
    assert.deepEqual(decorations(state.field(cm.quoteState)), expected, `${sql} at ${caret}`);
    assert.deepEqual(decorations(cm.quotePairDecorations(state)), expected);
  }
  assert.equal(globalThis.__editorTokenizeCalls, calls, "selection/marks must not tokenize");
}
{
  const state = stateFor("'abc'", { anchor: 2, head: 4 });
  assert.deepEqual(decorations(cm.quotePairDecorations(state)), []);
  assert.deepEqual(decorations(cm.quotePairDecorations(stateFor("'abc'", { anchor: 3 }))), []);
  let large = stateFor("'x'" + " ".repeat(59998));
  large = large.update({ effects: [cm.setStatement.of({ start: 0, end: 3 }), cm.setError.of({ start: 1, end: 2 })] }).state;
  assert.deepEqual(decorations(large.field(cm.markState).decorations), []);
  assert.deepEqual(gutters(large), []);
  assert.deepEqual(decorations(cm.quotePairDecorations(large)), []);
}

// Automation getters remain live across state replacements and cannot write SQL.
{
  const view = target(stateFor("SELECT 1"));
  view.contentDOM = {};
  const detach = cm.attachAutomation(view), element = view.contentDOM;
  assert.equal(element.value, "SELECT 1");
  assert.throws(() => { element.value = "DROP TABLE t"; }, TypeError);
  element.setSelectionRange(-5, 999);
  assert.deepEqual([element.selectionStart, element.selectionEnd], [0, 8]);
  element.setSelectionRange(7, 2);
  assert.deepEqual([element.selectionStart, element.selectionEnd], [2, 2]);
  view.state = stateFor("new");
  element.select();
  assert.equal(element.value, "new");
  assert.deepEqual([element.selectionStart, element.selectionEnd], [0, 3]);
  detach();
  assert.equal(Object.hasOwn(element, "value"), false);
}
console.log("Editor core: external undo and selection mapping, document histories, shortcut gates and fallthrough, marks, tokenizer spy and automation passed.");
