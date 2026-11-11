// Unit tests for the Query editor's SQL comment toggle and visual extensions.
// Run: node test/editor-commands-unit.mjs
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const rootDir = fileURLToPath(new URL("../src/editor", import.meta.url));
const result = await build({
  stdin: {
    contents: `
      export { EditorSelection, EditorState } from "@codemirror/state";
      export { history, undo } from "@codemirror/commands";
      export { toggleSqlComment, selectNextOccurrenceCmd, selectAllOccurrencesCmd } from "./commands";
      export { visualExtension } from "./visual";
      export { HIGHLIGHT_LIMIT } from "../sqlHighlight";
    `,
    resolveDir: rootDir,
    loader: "ts",
  },
  bundle: true,
  format: "esm",
  platform: "neutral",
  target: "es2020",
  write: false,
  logLevel: "silent",
});

const {
  EditorSelection,
  EditorState,
  history,
  undo,
  toggleSqlComment,
  selectNextOccurrenceCmd,
  selectAllOccurrencesCmd,
  visualExtension,
  HIGHLIGHT_LIMIT,
} = await import(
  "data:text/javascript;base64," + Buffer.from(result.outputFiles[0].text).toString("base64")
);

function makeState(doc, selection, extensions = []) {
  return EditorState.create({
    doc,
    selection,
    extensions: [EditorState.allowMultipleSelections.of(true), history(), ...extensions],
  });
}

function runCommand(state, cmd) {
  let next = state;
  const ok = cmd({
    state,
    dispatch(tr) {
      assert.equal(tr.startState, state, "command transactions must apply to the live editor state");
      next = tr.state;
    },
  });
  assert.equal(ok, true);
  return next;
}

// 1. One line: comment and uncomment, including "--" at end of line and mapped cursor.
{
  let state = makeState("SELECT 1;", EditorSelection.cursor(0));
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "-- SELECT 1;");
  assert.equal(state.selection.main.head, 3);

  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "SELECT 1;");
  assert.equal(state.selection.main.head, 0);

  let bareDash = makeState("--\nSELECT 2;", EditorSelection.cursor(1));
  bareDash = runCommand(bareDash, toggleSqlComment);
  assert.equal(bareDash.doc.toString(), "\nSELECT 2;");
}

// 2. Several lines: comment and uncomment all touched lines.
{
  const initial = "SELECT 1;\nSELECT 2;\nSELECT 3;";
  let state = makeState(initial, EditorSelection.range(2, 14));
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "-- SELECT 1;\n-- SELECT 2;\nSELECT 3;");
  assert.equal(state.selection.main.from, 5);
  assert.equal(state.selection.main.to, 20);

  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), initial);
  assert.equal(state.selection.main.from, 2);
  assert.equal(state.selection.main.to, 14);
}

// 3. Mixed commented and uncommented lines: adds "-- " to all non-blank lines.
{
  let state = makeState("-- SELECT 1;\nSELECT 2;", EditorSelection.range(0, 18));
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "-- -- SELECT 1;\n-- SELECT 2;");

  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "-- SELECT 1;\nSELECT 2;");
}

// 4. Indented lines: inserts "-- " at the smallest indentation of the lines.
{
  const initial = "    SELECT 1;\n  SELECT 2;";
  let state = makeState(initial, EditorSelection.range(0, initial.length));
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "  --   SELECT 1;\n  -- SELECT 2;");

  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), initial);
}

// 5. "#" lines: strips "#" marker (including when mixed with "-- " lines).
{
  let state = makeState("#SELECT 1;\n  #SELECT 2;\n-- SELECT 3;", EditorSelection.range(0, 30));
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "SELECT 1;\n  SELECT 2;\nSELECT 3;");
}

// 6. A "--x" line: never treated as a comment marker; gets "-- " added, never stripped.
{
  let state = makeState("--x", EditorSelection.cursor(0));
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "-- --x");

  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "--x");

  let mixed = makeState("-- SELECT 1;\n--x", EditorSelection.range(0, 15));
  mixed = runCommand(mixed, toggleSqlComment);
  assert.equal(mixed.doc.toString(), "-- -- SELECT 1;\n-- --x");
}

// 7. Blank lines inside the range: left untouched and ignored when checking markers or indentation.
{
  const initial = "  SELECT 1;\n\n   \n  SELECT 2;";
  let state = makeState(initial, EditorSelection.range(0, initial.length));
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "  -- SELECT 1;\n\n   \n  -- SELECT 2;");

  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), initial);
}

// 8. Multiple selection ranges: comments all touched lines in one transaction and maps all selections.
{
  const initial = "SELECT 1;\nSELECT 2;\nSELECT 3;";
  let state = makeState(
    initial,
    EditorSelection.create([EditorSelection.cursor(2), EditorSelection.cursor(22)])
  );
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "-- SELECT 1;\nSELECT 2;\n-- SELECT 3;");
  assert.deepEqual(
    state.selection.ranges.map(r => [r.from, r.to]),
    [[5, 5], [28, 28]]
  );

  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), initial);
  assert.deepEqual(
    state.selection.ranges.map(r => [r.from, r.to]),
    [[2, 2], [22, 22]]
  );
}

// 9. One undo step: undo restores the original text and selection in one call.
{
  const initial = "SELECT 1;\nSELECT 2;\nSELECT 3;";
  const initialSel = EditorSelection.create([EditorSelection.range(0, 6), EditorSelection.range(20, 26)]);
  let state = makeState(initial, initialSel);
  state = runCommand(state, toggleSqlComment);
  assert.equal(state.doc.toString(), "-- SELECT 1;\nSELECT 2;\n-- SELECT 3;");

  state = runCommand(state, undo);
  assert.equal(state.doc.toString(), initial);
  assert.deepEqual(
    state.selection.ranges.map(r => [r.from, r.to]),
    [[0, 6], [20, 26]]
  );
}

// 10. visualExtension: returns full visual set under HIGHLIGHT_LIMIT and only specialChars above it.
{
  const settings = {
    multi_cursor: true,
    completion_auto: true,
    close_brackets: true,
    soft_wrap: false,
    problem_warnings: true,
    hover_info: true,
  };
  const env = { current: { backslashEscapes: true, docId: "q1" } };
  const normalExt = visualExtension(settings, env, 100);
  const largeExt = visualExtension(settings, env, HIGHLIGHT_LIMIT + 1);
  assert.ok(Array.isArray(normalExt));
  assert.ok(Array.isArray(largeExt));
  assert.ok(normalExt.length > largeExt.length);
}

// Select all accepts a caret or matching ranges left by repeated Select next.
{
  const ranges = state => state.selection.ranges.map(r => [r.from, r.to]);
  let state = makeState("foo foo foo foo", EditorSelection.cursor(1));
  state = runCommand(state, selectAllOccurrencesCmd);
  assert.deepEqual(ranges(state), [[0, 3], [4, 7], [8, 11], [12, 15]], "caret expands to its word before selecting all");

  state = makeState("foo foo foo foo", EditorSelection.range(0, 3));
  state = runCommand(state, selectNextOccurrenceCmd);
  state = runCommand(state, selectNextOccurrenceCmd);
  assert.equal(state.selection.ranges.length, 3);
  state = runCommand(state, selectAllOccurrencesCmd);
  assert.deepEqual(ranges(state), [[0, 3], [4, 7], [8, 11], [12, 15]], "select all works after two Select next commands");

  state = makeState("foo foo foo", EditorSelection.create([
    EditorSelection.range(0, 3), EditorSelection.range(8, 11),
  ], 1));
  state = runCommand(state, selectAllOccurrencesCmd);
  assert.equal(state.selection.main.from, 8, "keep the main occurrence even when it is not the first range");

  const capped = makeState("foo ".repeat(1002), EditorSelection.cursor(1));
  let capDispatch = false;
  assert.equal(selectAllOccurrencesCmd({ state: capped, dispatch() { capDispatch = true; } }), false);
  assert.equal(capDispatch, false, "CodeMirror's occurrence cap must not leave an intermediate word selection");

  for (const selection of [EditorSelection.cursor(0), EditorSelection.create([
    EditorSelection.range(1, 4), EditorSelection.range(5, 8),
  ])]) {
    const initial = makeState(" foo bar", selection);
    let dispatched = false;
    assert.equal(selectAllOccurrencesCmd({ state: initial, dispatch() { dispatched = true; } }), false,
      "whitespace and unequal selected text are not expanded");
    assert.equal(dispatched, false, "a refused command keeps every selection");
  }
}

console.log("Editor commands and visual aids unit tests passed.");
