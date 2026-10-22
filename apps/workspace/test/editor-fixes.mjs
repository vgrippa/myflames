// Static and unit checks for the editor/data-editing fixes (no browser).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
const src = f => fs.readFileSync(new URL(`../src/${f}`, import.meta.url), 'utf8');

// QueryDigest exports execution statistics, not extrema/percentiles of digest
// averages or per-digest row totals. Execute the actual text generator with
// formatters stubbed to expose its numeric inputs (no browser needed).
const digest = src('QueryDigest.tsx');
const generator = /const generatePtReportText = \(\) => \{([\s\S]*?)\n  \};/.exec(digest);
assert.ok(generator, 'digest text generator exists');
const body = generator[1]
  .replace('const lines: string[]', 'const lines')
  .replace('(str: string, len: number)', '(str, len)')
  .replace('(value: number | null)', '(value)')
  .replace('(total: number)', '(total)');
// Keep the real product formatter: Percona uses engine=mysql, so the
// server_product argument must reach the exported report's header.
const opsTree = sourceTree('OpsShared.tsx');
const engineNameDeclaration = findNode(opsTree, node => ts.isFunctionDeclaration(node) && node.name?.text === 'engineName');
assert.ok(engineNameDeclaration, 'shared product formatter exists');
const engineName = new Function(ts.transpileModule(engineNameDeclaration.getText(opsTree).replace(/^export\s+/, ''), {
  compilerOptions: { target: ts.ScriptTarget.ES2020 },
}).outputText + '\nreturn engineName;')();
const generate = new Function('statements', 'aggregates', 'serverMeta', 'bound', 'filter',
  'time', 'number', 'shortDigest', 'formatRatio', 'digestQuery', 'engineName', body);
function exported(rows, serverMeta = { engine: 'mysql', version: '8.4', filter: 'time' }) {
  const sum = key => rows.reduce((n, row) => n + (row[key] || 0), 0);
  return generate(rows, { totalWorkloadMs: sum('total_ms'), totalCalls: sum('count'),
    totalExamined: sum('rows_examined'), totalSent: sum('rows_sent'), totalLockMs: sum('lock_time_ms') },
    serverMeta, { label: 'Alice@db:3306' }, 'time',
    x => `${x}ms`, String, String, () => ({ text: '1:1' }), row => ({ text: row.digest_text }), engineName);
}
const row = { digest_text: 'SELECT 1', count: 100, total_ms: 100, avg_ms: 1, max_ms: 1,
  rows_sent: 100, rows_examined: 200, lock_time_ms: 50, tmp_disk_tables: 0 };
const text = exported([row, { ...row, count: 1, avg_ms: 100, max_ms: 100 }]);
assert.match(text, /^# Server: MySQL 8\.4$/m);
assert.match(exported([row], { engine: 'mysql', version: '8.4.11-11', server_product: 'Percona Server', filter: 'time' }),
  /^# Server: Percona Server 8\.4\.11-11$/m, 'Percona report header keeps the product and release suffix');
const attributes = label => text.split('\n').find(line => line.startsWith(`# ${label}`)).trim().split(/\s+/).slice(3);
assert.deepEqual(attributes('Exec time').slice(0, 3), ['200ms', 'unknown', '100ms']);
assert.equal(attributes('Exec time')[4], 'unknown', 'execution p95 cannot be recovered from averages');
assert.deepEqual(attributes('Rows sent').slice(0, 3), ['200', 'unknown', 'unknown']);
assert.deepEqual(attributes('Rows examine').slice(0, 3), ['400', 'unknown', 'unknown']);
assert.deepEqual(attributes('Lock time').slice(0, 3), ['100ms', 'unknown', 'unknown']);
assert.match(exported([{ ...row, max_ms: 0 }]), /# Exec time\s+100ms\s+unknown\s+0ms/);
assert.match(exported([{ ...row, max_ms: undefined, lock_time_ms: undefined }]), /# Exec time\s+100ms\s+unknown\s+unknown/);
assert.match(exported([{ ...row, lock_time_ms: undefined }]), /# Lock time\s+unknown/);
assert.match(exported([]), /# Exec time\s+0ms\s+unknown\s+unknown\s+unknown\s+unknown/);
assert.ok(/const escape = csvCell/.test(digest) && digest.includes("from \"./reportTableExport\""), 'digest CSV uses the formula guard');

// TableData: CSV guard, JSON null, NULL is empty in TSV, Set to Default only where real.
const td = src('TableData.tsx');
assert.ok(td.includes('csvCell(v)'), 'row CSV uses csvCell');
assert.ok(td.includes('Record<string, string | null>') && td.includes('?? null'), 'row JSON emits null for NULL');
assert.ok(/v\?\.kind === 'null' \? null/.test(td) && !/\? 'NULL' :/.test(td), 'NULL is not copied as the text NULL');
assert.ok(td.includes('"Revert change"') && td.includes('contextMenu.inserted ? "Set to Default"'), 'existing rows say Revert change');

// ResultsPane: Copy value is the raw value.
const rp = src('ResultsPane.tsx');
assert.ok(rp.includes('copy(result.rows[menu.row][menu.col] ?? ""'), 'Copy value copies the raw value');

// TableAlterDialog: identifiers are backtick-escaped.
const alter = src('TableAlterDialog.tsx');
assert.ok(/bt\(schema\)\}\.\$\{bt\(table\)/.test(alter) && !alter.includes('`${schema}`'), 'ALTER TABLE quotes identifiers');

// MassKillDialog (Kill sessions…): statements are reviewed before anything is sent; cancel is recorded.
const mk = src('MassKillDialog.tsx');
const handle = mk.slice(mk.indexOf('async function handleConfirm'), mk.indexOf('function declineBulk'));
assert.ok(handle.includes('prepareBulkKill') && !handle.includes('killBulkSessions'), 'prepare does not send');
const send = mk.slice(mk.indexOf('async function sendBulk'), mk.indexOf('return (\n    <dialog'));
assert.ok(send.includes('killBulkSessions(') && send.includes('policy.check()'), 'send re-checks policy then kills');
assert.ok(mk.includes('<ConfirmSheet') && mk.includes('pending.review.statements') && mk.includes('onCancel={declineBulk}'), 'confirmation sheet lists the statements');
assert.ok(/recordDeclined\(/.test(mk), 'cancel is recorded as declined');
console.log('Editor fixes: digest execution statistics and CSV guard, table copy, Copy value, ALTER quoting and Kill sessions review passed.');

// Execute the real TSX handlers with deterministic event targets. These checks
// need neither a browser socket nor a renderer; browser suites still own focus
// and pixels. Compile through TypeScript so casts cannot change the test code.
function sourceTree(file) {
  return ts.createSourceFile(file, src(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
}
function findNode(root, predicate) {
  if (predicate(root)) return root;
  return ts.forEachChild(root, child => findNode(child, predicate));
}
function executable(expression, file, environment) {
  const js = ts.transpileModule(`(${expression.getText(file)})`, {
    compilerOptions: { target: ts.ScriptTarget.ES2020 },
  }).outputText.trim().replace(/;$/, '');
  return new Function(...Object.keys(environment), `return ${js}`)(...Object.values(environment));
}
function pointerHandlers(fileName, className, environment) {
  const file = sourceTree(fileName);
  const element = findNode(file, node => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
    node.attributes.properties.some(attr => ts.isJsxAttribute(attr) && attr.name.text === 'className' && attr.initializer?.text === className));
  assert.ok(element, `${fileName}: ${className} exists`);
  return Object.fromEntries(element.attributes.properties.filter(attr => ts.isJsxAttribute(attr) &&
    /^on(?:Pointer|LostPointer)/.test(attr.name.text)).map(attr => [attr.name.text, executable(attr.initializer.expression, file, environment)]));
}
function target() {
  const listeners = new Map();
  return { setPointerCapture() {}, addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) { if (listeners.get(type) === listener) listeners.delete(type); },
    dispatch(type, event) { listeners.get(type)?.(event); }, listeners };
}
const event = (node, x = 100, y = 100) => ({ clientX: x, clientY: y, pointerId: 1, currentTarget: node, preventDefault() {} });
for (const cancel of ['onPointerCancel', 'onLostPointerCapture']) {
  const node = target(), drag = { current: null }, widths = [], commits = [];
  const column = pointerHandlers('kit.tsx', 'col-resize', { drag, c: { key: 'name' }, widths: [200], i: 0,
    setDragging: value => widths.push(value), commitWidth: (...args) => commits.push(args) });
  column.onPointerDown(event(node));
  column.onPointerMove(event(node, 120));
  assert.equal(widths.at(-1).width, 220);
  column[cancel](event(node));
  assert.equal(drag.current, null);
  assert.equal(widths.at(-1), null, 'cancelling discards the live column preview');
  column.onPointerMove(event(node, 180));
  column.onPointerUp(event(node, 180));
  assert.deepEqual(commits, [], 'a cancelled column drag never persists a width');

  const seen = [], inspectorDrag = { current: null };
  const inspector = pointerHandlers('kit.tsx', 'inspector-resize', { drag: inspectorDrag, width: 340, resize: value => seen.push(value) });
  inspector.onPointerDown(event(node));
  inspector.onPointerMove(event(node, 80));
  assert.deepEqual(seen, [360]);
  inspector[cancel](event(node));
  inspector.onPointerMove(event(node, 60));
  inspector.onPointerUp(event(node, 60));
  assert.equal(inspectorDrag.current, null);
  assert.deepEqual(seen, [360], 'no inspector resize after cancellation');
}
for (const cancel of ['pointerup', 'pointercancel', 'lostpointercapture']) {
  for (const [fileName, className, environment, changed] of [
    ['SchemaBrowser.tsx', 'browser-resize', { width: 200 }, 'onWidth'],
    ['QueryLab.tsx', 'editor-splitter', { editorHeight: 200 }, 'setHeight'],
  ]) {
    const node = target(), seen = [];
    const handlers = pointerHandlers(fileName, className, { ...environment, [changed]: value => seen.push(value) });
    handlers.onPointerDown(event(node));
    node.dispatch('pointermove', event(node, 120, 120));
    assert.deepEqual(seen, [220]);
    node.dispatch(cancel, event(node));
    assert.equal(node.listeners.size, 0, `${className} removes all drag listeners on ${cancel}`);
    node.dispatch('pointermove', event(node, 180, 180));
    assert.deepEqual(seen, [220]);
    handlers.onPointerDown(event(node));
    node.dispatch('pointermove', event(node, 130, 130));
    assert.deepEqual(seen, [220, 230], 'a fresh drag works after cancellation');
    node.dispatch('pointerup', event(node));
    assert.equal(node.listeners.size, 0);
  }
}
// Exercise the actual layout callback against measured short/tall menus and a
// footer. A fixed guessed menu height fails the short-menu case immediately.
const kitTree = sourceTree('kit.tsx');
const menu = findNode(kitTree, node => ts.isFunctionDeclaration(node) && node.name?.text === 'ContextMenu');
const place = findNode(menu, node => ts.isVariableDeclaration(node) && node.name.getText(kitTree) === 'place').initializer;
function placed({ x, y, height, viewport = 300, footer }) {
  const element = { style: {}, offsetWidth: 220, get offsetHeight() { return Math.min(height, parseFloat(this.style.maxHeight) || height); } };
  let style;
  executable(place, kitTree, { panel: { current: element }, x, y, setStyle: next => { style = next; },
    window: { innerWidth: 400, innerHeight: viewport }, document: { querySelector: () => footer === undefined ? null : { getBoundingClientRect: () => ({ top: footer }) } } })();
  return style;
}
assert.deepEqual(placed({ x: 380, y: 280, height: 32 }), { left: 172, top: 260, maxHeight: 32 });
assert.deepEqual(placed({ x: 380, y: 280, height: 1000 }), { left: 172, top: 8, maxHeight: 284 });
assert.deepEqual(placed({ x: 10, y: 200, height: 80, footer: 260 }), { left: 10, top: 172, maxHeight: 80 });
assert.deepEqual(placed({ x: -20, y: -20, height: 32 }), { left: 8, top: 8, maxHeight: 284 });
const dataTable = findNode(kitTree, node => ts.isFunctionDeclaration(node) && node.name?.text === 'DataTable');
const menuElement = findNode(dataTable, node => ts.isJsxSelfClosingElement(node) && node.tagName.getText(kitTree) === 'ContextMenu');
const actionExpression = menuElement.attributes.properties.find(attr => ts.isJsxAttribute(attr) && attr.name.text === 'actions').initializer.expression;
const copied = [];
const customAction = { label: 'Custom action', onClick() {} };
const menuRow = { id: 'one' }, col = { key: 'text' };
for (const cellText of ['', 'value']) {
  const actions = executable(actionExpression, kitTree, { contextMenu: { row: menuRow, col, cellText }, setContextMenu() {},
    copyWithFeedback: (text, message) => { copied.push([text, message]); }, rowContextMenu: (given, helpers) => {
      assert.equal(given, menuRow); assert.equal(helpers.col, col); assert.equal(helpers.cellText, cellText);
      return [customAction];
    } });
  assert.equal(actions[0], customAction);
  assert.equal(actions.at(-1).label, 'Copy cell', 'copy follows custom actions even for an empty value');
  actions.at(-1).onClick();
  assert.deepEqual(copied.at(-1), [cellText, 'Cell value copied']);
}
// Table editing keeps its own mutation actions above the standard copies.
// Execute the production action builder for existing, inserted and read-only rows.
const cellBuilder = td.slice(td.indexOf('  const cellActions:'), td.indexOf('\n  return <section className="table-data-panel"'));
const cellJs = ts.transpileModule(cellBuilder + '\nreturn cellActions;', { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
function cellLabels(overrides = {}) {
  const environment = { contextMenu: { rowId: 'one', inserted: false }, activeCol: { name: 'name', nullable: true }, editable: true,
    activeDeleted: false, activeVal: { kind: 'text', value: 'one' }, drafts: { one: { values: { name: { kind: 'text', value: 'two' } } } },
    ...overrides };
  return new Function(...Object.keys(environment), cellJs)(...Object.values(environment)).map(action => action.label || '|');
}
const copyLabels = ['Copy cell value', 'Copy row (Excel TSV)', 'Copy row as CSV', 'Copy row as JSON', 'Copy column (name)', 'Copy table with headers'];
assert.deepEqual(cellLabels(), ['Set to NULL', 'Revert change', 'Delete row', '|', ...copyLabels]);
assert.deepEqual(cellLabels({ contextMenu: { rowId: 'one', inserted: true } }), ['Set to NULL', 'Set to Default', 'Remove row', '|', ...copyLabels]);
assert.deepEqual(cellLabels({ activeDeleted: true }), ['Undo delete', '|', ...copyLabels]);
assert.deepEqual(cellLabels({ editable: false }), copyLabels);
assert.deepEqual(cellLabels({ activeCol: null }), []);
assert.ok(td.includes('<ContextMenu') && !td.includes('menuH ='), 'table editing uses the same measured menu');
assert.ok(!src('kit.tsx').includes('rowCells'), 'unused eager row copy work is gone');
console.log('Menu placement and pointer cancellation contracts passed.');
