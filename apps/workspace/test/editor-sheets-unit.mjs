// Execute the actual editor transition handlers without a browser/socket.
// editor.mjs owns focus trapping/restoration and docked/sheet geometry.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import ts from 'typescript';
async function parse(name) {
  const text = await fs.readFile(new URL(`../src/${name}`, import.meta.url), 'utf8');
  return ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}
function find(root, check) {
  if (check(root)) return root;
  return ts.forEachChild(root, n => find(n, check));
}
function executable(node, source, env, statement = false) {
  assert.ok(node);
  const text = statement ? `(() => { ${node.getText(source)} })` : `(${node.getText(source)})`;
  const code = ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  return new Function(...Object.keys(env), `return ${code}`)(...Object.values(env));
}
const lab = await parse('QueryLab.tsx');
const results = await parse('ResultsPane.tsx');
const named = (source, name) => find(source, n => ts.isFunctionDeclaration(n) && n.name?.text === name);
class Element {}
const browserOpener = new Element();
const historyOpener = new Element();
const rowOpener = new Element();
const state = { narrow: true, sheet: '', layout: { browser: false, history: false }, panel: false, sel: null };
const sheetOpener = { current: null };
const panelOpener = { current: null };
const setSheet = update => { state.sheet = typeof update === 'function' ? update(state.sheet) : update; };
function toggle(pane, opener) {
  executable(named(lab, 'togglePane'), lab, { narrow: state.narrow, sheet: state.sheet, sheetOpener,
    setSheet, setLayout: update => { state.layout = update(state.layout); }, document: { activeElement: rowOpener }, HTMLElement: Element })(pane, opener);
}
const setPanel = value => { state.panel = value; };
function openRow() {
  executable(named(results, 'showRow'), results, {
    setSel: update => { state.sel = update(state.sel); }, one: c => ({ anchor: c, focus: c, allRows: false }),
    menu: null, root: { current: { querySelector: () => rowOpener } }, panelOpener,
    rowPanel: { onOpen: () => setSheet('') }, setPanel,
  })(2);
}
// The render-time adjustment that closes the row sheet when another sheet opens.
const blockCheck = find(results, n => ts.isIfStatement(n) && n.expression.getText(results) === 'blocked !== seenBlocked');
let seenBlocked = false;
function settle() {
  const blocked = state.narrow && state.sheet !== '';
  executable(blockCheck, results, { blocked, seenBlocked, setSeenBlocked: v => { seenBlocked = v; }, panel: state.panel, setPanel }, true)();
}
for (const pane of ['browser', 'history']) {
  toggle(pane, pane === 'browser' ? browserOpener : historyOpener);
  assert.equal(state.sheet, pane);
  assert.equal(sheetOpener.current, pane === 'browser' ? browserOpener : historyOpener);
  openRow(); settle();
  assert.equal(state.sheet, '', 'Show row dismisses the side sheet');
  assert.equal(state.panel, true);
  assert.equal(panelOpener.current, rowOpener);
  toggle(pane); settle();
  assert.equal(state.sheet, pane);
  assert.equal(state.panel, false, 'opening a side sheet forgets the row open state');
  toggle(pane); settle();
  assert.equal(state.sheet, '');
  assert.equal(state.panel, false, 'closing the side sheet cannot resurrect a row sheet');
}
state.narrow = false;
openRow();
toggle('browser'); toggle('history'); settle();
assert.deepEqual(state.layout, { browser: true, history: true });
assert.equal(state.panel, true, 'wide panels remain independent');
assert.equal(state.sheet, '');
console.log('editor sheet handlers: both directions, no stale reopening, opener capture and independent wide panels passed');
