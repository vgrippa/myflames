// Real SQL syntax tree, EditorState and extension. No server or browser.
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
const bundled = await build({
  stdin: { contents: `
    export { EditorState, EditorSelection } from "@codemirror/state";
    export { showTooltip } from "@codemirror/view";
    export { sqlLanguage } from "./src/editor/sqlLanguage";
    export { argumentHintsExtension } from "./src/editor/argumentHints";
    export { functionsFor } from "./src/editor/engineWords";
  `, resolveDir: fileURLToPath(new URL("..", import.meta.url)) },
  bundle: true, format: "esm", platform: "neutral", target: "es2020", write: false, logLevel: "silent",
});
const cm = await import("data:text/javascript;base64," + Buffer.from(bundled.outputFiles[0].text).toString("base64"));
const classes = new Set(["sql-arg-hint"]);
// As in hover-unit, markup, attributes and non-allowlisted classes fail loudly.
class Element {
  nodeType = 1;
  childNodes = [];
  constructor(tag) { assert.equal(tag, "div"); this.tagName = tag.toUpperCase(); }
  set className(value) { assert.ok(classes.has(value), `fixed app class: ${value}`); this.class = value; }
  get className() { return this.class; }
  set textContent(value) { this.childNodes = value === "" ? [] : [{ nodeType: 3, data: String(value) }]; }
  get textContent() { return this.childNodes.map(n => n.nodeType === 3 ? n.data : n.textContent).join(""); }
  appendChild(child) { this.childNodes.push(child); return child; }
  set innerHTML(_) { assert.fail("innerHTML forbidden"); }
  set outerHTML(_) { assert.fail("outerHTML forbidden"); }
  setAttribute() { assert.fail("no attributes needed"); }
  insertAdjacentHTML() { assert.fail("HTML insertion forbidden"); }
  get style() { assert.fail("no SQL text in CSS"); }
}
const previousDocument = globalThis.document;
globalThis.document = { createElement: tag => new Element(tag) };
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const live = new Set();
function harness(marked, options = {}) {
  const pos = marked.indexOf("|"), doc = marked.replace("|", "");
  assert.ok(pos >= 0);
  const env = { current: { docId: "tab-1", engine: "mysql", serverVersion: "8.0.36", backslashEscapes: true, ...options.env } };
  const extension = cm.argumentHintsExtension({ hover_info: true }, env);
  let state = cm.EditorState.create({ doc, selection: options.selection || { anchor: pos }, extensions: [
    cm.sqlLanguage, cm.EditorState.allowMultipleSelections.of(true), extension,
  ] });
  const field = extension[0];
  let dispatches = 0, updating = false;
  const view = { get state() { return state; }, dispatch(spec) {
    assert.equal(updating, false, "no reentrant dispatch"); dispatches++; updating = true;
    try { state = state.update(spec).state; } finally { updating = false; }
  } };
  assert.equal(state.field(field), null, "nothing shown before lazy vocabulary loads");
  const plugin = extension[1].create(view); live.add(plugin);
  return { env, view, tip() {
    const value = state.field(field);
    assert.deepEqual(state.facet(cm.showTooltip).filter(Boolean), value ? [value] : []);
    return value;
  }, get dispatches() { return dispatches; }, destroy() { plugin.destroy(); live.delete(plugin); } };
}
let passed = 0;
async function check(marked, expected, options) {
  const h = harness(marked, options); await flush(); const tip = h.tip();
  if (expected === null) assert.equal(tip, null, marked.slice(0, 120));
  else {
    assert.ok(tip, marked.slice(0, 120)); assert.ok(tip.pos <= h.view.state.selection.main.head); assert.equal(tip.above, true);
    const name = expected.slice(0, expected.indexOf("("));
    assert.equal(h.view.state.doc.sliceString(tip.pos - name.length - 1, tip.pos).toUpperCase(), `${name}(`,
      "the hint anchors immediately after its function's opening parenthesis");
    const { dom } = tip.create(h.view);
    assert.equal(dom.className, "sql-arg-hint"); assert.equal(dom.textContent, expected, marked.slice(0, 120));
    assert.deepEqual(dom.childNodes, [{ nodeType: 3, data: expected }], "only text nodes, never SQL as markup");
    assert.equal("keydown" in dom, false); assert.equal("focus" in dom, false);
  }
  h.destroy(); passed++;
}
try {
  const list = cm.functionsFor("mysql", "8.0.36");
  for (const [name, min, max] of [["POW", 2, 2], ["ABS", 1, 1], ["CONCAT", 1, "unbounded"], ["ATAN", 1, 2], ["COUNT", null, null], ["PI", 0, 0]])
    assert.deepEqual(list.functions.find(fn => fn.name === name), { name, min, max });
  assert.ok(list.functions.every(fn => /^[A-Z_][A-Z0-9_]*$/.test(fn.name)), "generated names cannot contain hostile markup");
  for (const [marked, expected] of [
    ["SELECT DATE_FORMAT(|)", "DATE_FORMAT(…) · 2 arguments · argument 1"],
    ["SELECT abs(|)", "ABS(…) · 1 argument · argument 1"],
    ["SELECT ATAN(|)", "ATAN(…) · 1 to 2 arguments · argument 1"],
    ["SELECT CONCAT(|)", "CONCAT(…) · at least 1 arguments · argument 1"],
    ["SELECT COUNT(|)", "COUNT(…) · argument 1"], ["SELECT PI(|)", "PI(…) · 0 arguments · argument 1"],
    ["SELECT POW(1, CONCAT('a', |))", "CONCAT(…) · at least 1 arguments · argument 2"],
    ["SELECT POW(CONCAT('a', 'b'), |)", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW(1, (2, |))", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW(1, missing(2, |))", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW('a,(;', `x,y`, /* , ( ; */ |)", "POW(…) · 2 arguments · argument 3"],
    ['SELECT POW("a,(", |)', "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW('a\\\',b', |)", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW('a''b,c', |)", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW(1, -- , (;\n |)", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW(1, # , (;\n |)", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW(1, /*! , (; */ |)", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW('still, typing |", "POW(…) · 2 arguments · argument 1"],
    ["SELECT POW(1, /* , |", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW(1, `still, |", "POW(…) · 2 arguments · argument 2"],
    ["SELECT POW(1, 2); SELECT ABS(|)", "ABS(…) · 1 argument · argument 1"],
  ]) await check(marked, expected);
  await check("SELECT CONCAT('a\\', 1, |)", "CONCAT(…) · at least 1 arguments · argument 3", { env: { backslashEscapes: false } });
  await check("SELECT CONCAT('a\\', 1, |)", "CONCAT(…) · at least 1 arguments · argument 1", { env: { backslashEscapes: true } });
  await check("SELECT CONCAT('a\\', 1; SELECT |", null, { env: { backslashEscapes: false } });
  await check("SELECT CONCAT('a\\', 1); SELECT ABS(|)", "ABS(…) · 1 argument · argument 1", { env: { backslashEscapes: false } });
  await check("SELECT CONCAT('a\\', " + " ".repeat(4000) + "|)", null, { env: { backslashEscapes: false } });
  for (const marked of [
    "SELECT POW (|)", "SELECT POW\n(|)", "SELECT POW/*comment*/(|)", "SELECT db.POW(|)",
    "SELECT db. POW(|)", "SELECT db./* c */POW(|)", "SELECT `POW`(|)", "SELECT @POW(|)",
    "SELECT :POW(|)", "SELECT missing(|)", "SELECT POW(1, 2)|", "SELECT POW(1; SELECT |", "SELECT (|)",
    "SELECT 'POW(|)'", "SELECT `POW(|)`", "SELECT /* POW(|) */", "SELECT # POW(|)",
    'SELECT `<img src=x onerror=1>`(|)', "SELECT 1POW(|)",
    "SELECT POW(" + " ".repeat(4000) + "|)", "SELECT db." + " ".repeat(4100) + "POW(|)",
    "SELECT " + " ".repeat(4100) + "POW(|)",
    "SELECT db./*" + "x".repeat(4100) + "*/POW(|)", "x".repeat(60001) + " SELECT POW(|)",
  ]) await check(marked, null);
  await check("SELECT POW(" + " ".repeat(4100) + "1, ABS(|))", "ABS(…) · 1 argument · argument 1");
  await check("SELECT POW(1, |)", null, { selection: cm.EditorSelection.create([cm.EditorSelection.cursor(7), cm.EditorSelection.cursor(14)]) });
  await check("SELECT POW(1, |)", null, { selection: { anchor: 12, head: 14 } });
  await check("SELECT POW(|)", null, { env: { engine: undefined } });
  await check("SELECT POW(|)", null, { env: { serverVersion: "unknown" } });
  await check("SELECT POW(|)", "POW(…) · 2 arguments · argument 1, MySQL 8.0.46 list", { env: { serverVersion: "8.0.99" } });
  await check("SELECT POW(|)", "POW(…) · 2 arguments · argument 1", { env: { engine: "mariadb", serverVersion: "11.4.5" } });
  {
    const h = harness("SELECT POW(1, |)"); await flush(); assert.ok(h.tip());
    h.view.dispatch({ selection: { anchor: h.view.state.doc.length } }); assert.equal(h.tip(), null);
    h.view.dispatch({ selection: { anchor: h.view.state.doc.length - 1 } }); assert.ok(h.tip());
    h.env.current.serverVersion = "unknown"; h.view.dispatch({}); assert.equal(h.tip(), null);
    h.env.current.serverVersion = "8.0.36"; h.view.dispatch({}); assert.ok(h.tip());
    h.view.dispatch({ changes: { from: 7, to: 10, insert: "custom" } }); assert.equal(h.tip(), null);
    h.destroy(); passed++;
  }
  {
    const h = harness("SELECT POW(|)"); h.destroy(); await flush();
    assert.equal(h.dispatches, 0, "destroyed editor cannot receive a lazy-load refresh"); passed++;
  }
  {
    // Warm the index, then count catalog name reads rather than timing a CPU.
    // A linear find for each unknown call reads every function name again.
    const h = harness("SELECT ABS(|)"); await flush();
    const before = h.tip();
    h.view.dispatch({ changes: { from: h.view.state.selection.main.head, insert: "123" },
      selection: { anchor: h.view.state.selection.main.head + 3 } });
    assert.equal(h.tip(), before, "typing within one argument keeps the tooltip and create function");
    const pos = h.view.state.selection.main.head;
    h.view.dispatch({ changes: { from: pos, insert: "," }, selection: { anchor: pos + 1 } });
    assert.notEqual(h.tip(), before, "a changed argument label replaces the tooltip");
    assert.ok(h.tip().create(h.view).dom.textContent.endsWith("argument 2"));
    const second = h.tip();
    h.view.dispatch({ changes: { from: 0, insert: " " }, selection: { anchor: pos + 2 } });
    assert.notEqual(h.tip(), second, "moving the call's anchor replaces the tooltip");
    assert.equal(h.tip().pos, second.pos + 1);
    h.destroy(); passed++;
  }
  {
    const list = cm.functionsFor("mysql", "8.0.36");
    let reads = 0;
    const descriptors = list.functions.map(fn => Object.getOwnPropertyDescriptor(fn, "name"));
    try {
      list.functions.forEach((fn, i) => Object.defineProperty(fn, "name", {
        configurable: true, get() { reads++; return descriptors[i].value; },
      }));
      // Existing hints have already built this catalog's map. A fresh editor
      // and functionsFor wrapper must share it, including unknown names.
      for (const nesting of [1, 2000]) {
        const h = harness("a(".repeat(nesting) + "|"); await flush();
        assert.equal(h.tip(), null);
        assert.equal(reads, 0, "unknown call lookups do not scan the function catalog");
        h.view.dispatch({ changes: { from: h.view.state.doc.length, insert: "1" },
          selection: { anchor: h.view.state.doc.length + 1 } });
        assert.equal(reads, 0, "keystrokes reuse the same catalog index");
        h.destroy();
      }
    } finally {
      list.functions.forEach((fn, i) => Object.defineProperty(fn, "name", descriptors[i]));
    }
    passed++;
  }
  console.log(`${passed} argument hint cases passed.`);
} finally {
  for (const plugin of live) plugin.destroy(); globalThis.document = previousDocument;
}
