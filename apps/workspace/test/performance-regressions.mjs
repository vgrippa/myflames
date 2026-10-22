// Deterministic regressions for rendering/persistence optimizations. No DOM,
// server, sleeps or React dependency: execute the real TS functions/effects
// with controlled hooks and geometry. Browser layout still needs visual QA.
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourceRoot = process.env.PERF_SOURCE_ROOT || path.join(here, '..');
const read = name => fs.readFileSync(path.join(sourceRoot, 'src', name), 'utf8');
const parse = name => ts.createSourceFile(name, read(name), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function pick(file, predicate) {
  const source = parse(file);
  let found;
  function visit(node) {
    if (!found && predicate(node, node.getText(source))) found = node;
    if (!found) ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(found, `Missing test subject in ${file}`);
  return found.getText(source);
}
const declaration = (file, name) => pick(file, n => ts.isVariableStatement(n) && n.declarationList.declarations.some(d => d.name.getText() === name));
const fn = (file, name) => pick(file, n => ts.isFunctionDeclaration(n) && n.name?.text === name);
const effect = (file, needle) => pick(file, (n, text) => ts.isCallExpression(n) && ['useEffect', 'useLayoutEffect'].includes(n.expression.getText()) && text.includes(needle));
function run(code, env) {
  const js = ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS } }).outputText;
  return new Function(...Object.keys(env), js)(...Object.values(env));
}
// Keep hook dependencies and cleanups across renders, and deliberately leave
// passive effects pending until requested. This exposes a pre-unload gap.
function hooks() {
  let index = 0;
  const slots = [], pending = [];
  const register = phase => (body, deps) => {
    const i = index++, old = slots[i];
    if (!old || !deps || deps.some((d, j) => !Object.is(d, old.deps[j]))) {
      slots[i] = { deps, cleanup: old?.cleanup };
      pending.push({ i, body, phase });
    }
  };
  const api = {
    useRef(value) { const i = index++; return slots[i] ||= { current: value }; },
    useLayoutEffect: register('layout'), useEffect: register('passive'),
  };
  function commit(phase) {
    for (const item of [...pending]) if (item.phase === phase) {
      pending.splice(pending.indexOf(item), 1);
      slots[item.i].cleanup?.();
      slots[item.i].cleanup = item.body();
    }
  }
  return {
    render(code, env) { index = 0; run(code, { ...env, ...api }); commit('layout'); },
    passive() { commit('passive'); },
    close() { for (const s of slots) s.cleanup?.(); },
  };
}
function clock() {
  let id = 0;
  const timers = new Map();
  return {
    setTimeout(body, ms) { timers.set(++id, { body, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    runReports() { for (const [id, t] of [...timers]) if (t.ms === 90) { timers.delete(id); t.body(); } },
  };
}

const query = read('QueryLab.tsx');
const reportCode = query.slice(query.indexOf('  const received = useRef'), query.indexOf('  useEffect(() => {\n    if (!job'));
const saveCode = effect('main.tsx', 'saver.update(snapshot, edits)');
const leaveCode = effect('main.tsx', 'addEventListener("pagehide"');
for (const length of [5000, 70000]) test(`SQL ${length} chars: latest edit reaches save/leave before passive effects or timers`, async () => {
  const timer = clock(), requests = [], listeners = {};
  const win = { ...timer, addEventListener: (name, body) => { listeners[name] = body; }, removeEventListener() {} };
  const document = { visibilityState: 'hidden', addEventListener: win.addEventListener, removeEventListener() {} };
  // Use the actual SessionSaver and its keepalive/risk logic. Stub only transport.
  const exports = {};
  run(fs.readFileSync(path.join(here, '../src/session.ts'), 'utf8'), {
    exports, window: win, require: () => ({ ApiError: class extends Error {}, api: async (...args) => {
      requests.push(args); return { revision: requests.length, saved_at: 'now' };
    } }),
  });
  const saver = new exports.SessionSaver(() => {}, () => assert.fail('unexpected conflict'));
  const tab = { id: 'one', name: 'Saved script', sql: 'x'.repeat(length), parameters: '{}' };
  let payload = { tabs: [tab], tab_id: tab.id, timeout: 30, repeat: 1, trace: false, conditions: [] };
  const snapshot = () => JSON.stringify({ query_lab: payload });
  saver.enable(snapshot());
  const editorHooks = hooks(), shellHooks = hooks(), leaveHooks = hooks();
  const defaults = () => ({}), layout = {};
  const env = { window: win, onStateChange: p => { payload = p; }, onTabsNow() {}, start: {},
    defaultEditorLayout: defaults, layout, tabId: tab.id, timeout: 30, repeat: 1, trace: false, conditions: payload.conditions };
  const render = tabs => {
    editorHooks.render(reportCode, { ...env, tabs, tab: tabs[0] });
    shellHooks.render(saveCode, { ready: true, saver, snapshot: snapshot(), edits: snapshot() });
  };
  render([tab]); editorHooks.passive(); timer.runReports(); shellHooks.passive();
  saver.update(snapshot()); saver.enable(snapshot()); // established saved baseline
  leaveHooks.render(leaveCode, { saver, window: win, document }); leaveHooks.passive();
  const latest = { ...tab, sql: tab.sql + '\nSELECT 42;' };
  render([latest]); // neither a passive effect nor the old 90 ms delay gets to run
  assert.ok(payload.tabs[0].sql === latest.sql, 'editor publishes the edit during layout');
  assert.equal(saver.atRisk(), length > 60000, 'large snapshots trigger the existing unload guard immediately');
  listeners.visibilitychange();
  assert.equal(requests.length, 1, 'hide sends the pending edit');
  assert.equal(JSON.parse(requests[0][1]).state.query_lab.tabs[0].sql, latest.sql);
  assert.equal(requests[0][3], length < 60000, 'keepalive follows the existing byte limit');
  const final = { ...latest, sql: latest.sql + '\nSELECT 43;' };
  render([final]);
  listeners.pagehide();
  assert.equal(JSON.parse(requests.at(-1)[1]).state.query_lab.tabs[0].sql, final.sql, 'pagehide resends the newest edit even with a save in flight');
  await Promise.resolve();
  saver.disable('test complete'); editorHooks.close(); shellHooks.close(); leaveHooks.close();
});

test('KILL preview clamps an obsolete offset and resets scroll/cancels a queued frame on filtering', () => {
  const file = 'MassKillDialog.tsx';
  const calculate = ['windowed', 'rowH', 'first', 'last', 'shown'].map(n => declaration(file, n)).join('\n');
  for (const total of [0, 1, 80, 81, 100, 2000]) {
    const sessions = Array.from({ length: total }, (_, id) => ({ id }));
    for (const top of [0, 32000, 1e8, -32]) {
      const r = run(calculate + '\nreturn {first, last, shown};', { matchedSessions: sessions, previewScrollTop: top });
      assert.ok(r.first >= 0 && r.first <= r.last && r.last <= total);
      assert.equal(r.shown.length, total > 80 ? Math.min(36, total) : total);
    }
  }
  const reset = effect(file, 'previewWrap.current');
  const h = hooks(), cancelled = [], wrap = { scrollTop: 0 }, raf = { current: null };
  let state = 0;
  const env = { previewWrap: { current: wrap }, previewRaf: raf, setPreviewScrollTop: n => { state = n; }, window: { cancelAnimationFrame: id => cancelled.push(id) } };
  h.render(reset, { ...env, matchedSessions: [{ id: 1 }] });
  wrap.scrollTop = state = 32000; raf.current = 17;
  h.render(reset, { ...env, matchedSessions: [{ id: 2 }] });
  assert.equal(wrap.scrollTop, 0); assert.equal(state, 0); assert.deepEqual(cancelled, [17]);
  raf.current = 18; h.close(); assert.deepEqual(cancelled, [17, 18], 'unmount cancels pending frames too');
});

test('Run backdrop mounted after scrolling immediately follows both scroll axes', () => {
  const code = fn('SqlEditor.tsx', 'follow') + '\n' + effect('SqlEditor.tsx', 'follow,');
  const h = hooks(), backdrop = { current: null }, area = { current: { scrollTop: 320, scrollLeft: 90 } };
  const env = { value: 'SELECT 1;', height: 400, area, backdrop, layer: { current: { style: {} } }, gutter: { current: { style: {} } } };
  h.render(code, { ...env, flash: null });
  backdrop.current = { style: {} };
  h.render(code, { ...env, flash: { start: 0, end: 9 } });
  assert.equal(backdrop.current.style.transform, 'translate(-90px, -320px)');
  h.close();
});

test('completion popup repositions when loading finishes or its note changes', () => {
  const code = fn('SqlEditor.tsx', 'positionCompletion') + '\n' + effect('SqlEditor.tsx', 'positionCompletion,');
  const h = hooks(), popup = { current: { offsetWidth: 320, offsetHeight: 40 } };
  let position = { left: 0, top: 0 };
  const env = {
    showing: true, value: 'SELECT ', caret: { start: 7 }, selected: 0, popup,
    area: { current: { scrollTop: 20, scrollLeft: 0, getBoundingClientRect: () => ({ left: 20, top: 500 }) } },
    getComputedStyle: () => ({ lineHeight: '20px' }), window: { innerWidth: 800, innerHeight: 600 },
    document: { createElement: () => ({ style: {}, append() {}, remove() {}, getBoundingClientRect: () => ({ left: 30, top: 10 }) }), body: { append() {} } },
    setPosition: update => { position = update(position); },
  };
  h.render(code, { ...env, completion: { items: [], loading: true, note: '' } });
  assert.equal(position.top, 510);
  popup.current.offsetHeight = 220;
  const items = Array.from({ length: 8 }, () => ({}));
  h.render(code, { ...env, completion: { items, loading: false, note: '' } });
  assert.equal(position.top, 270); assert.ok(position.top + popup.current.offsetHeight <= 592);
  popup.current.offsetHeight = 260;
  h.render(code, { ...env, completion: { items, loading: false, note: 'Metadata unavailable' } });
  assert.equal(position.top, 230);
  h.close();
});

test('table measurement covers new pages/windows, every new row, and revealed columns', () => {
  const file = 'kit.tsx';
  const code = declaration(file, 'measuredFor') + '\n' + declaration(file, 'seenRows') + '\n' + effect(file, 'measuredFor.current');
  const h = hooks();
  const rows = Array.from({ length: 100 }, (_, id) => ({ id, width: id < 50 ? 60 : 180, pill: id === 99 ? 240 : 0 }));
  let shown = [], measures = 0, measured = { sig: '', natural: {}, min: {}, flags: {} };
  const th = { dataset: { col: 'n' }, querySelector: () => ({ getBoundingClientRect: () => ({ width: 20 }) }), getBoundingClientRect: () => ({ width: Math.max(0, ...shown.map(r => r.width)) }) };
  const table = { current: { tHead: { rows: [{ cells: [th] }] }, classList: { add() { measures++; }, remove() {} },
    tBodies: [{ querySelectorAll: () => shown.map(r => ({ querySelectorAll: () => r.pill ? [{ getBoundingClientRect: () => ({ width: r.pill }) }] : [], querySelector: () => null })) }] } };
  const wrap = { current: { clientWidth: 600, scrollLeft: 25 } };
  const columns = [{ key: 'n', num: true }];
  const render = (page, hiddenSig = '', sorted = page, signature = 'n') => {
    shown = page;
    h.render(code, { sorted, shown, sourceRows: rows, stableRows: sorted, signature, hiddenSig, frame: wrap.current.clientWidth,
      table, wrap, columns, measured, setMeasured: m => { measured = m; }, CSS: { escape: s => s },
      COLUMN_MIN: 48, COLUMN_MAX: 1200, CELL_PAD: 16, SORT_MARK: 12, FLEX_MIN: 160, AUTO_MAX: 360,
      clamp: (v, lo, hi) => Math.max(lo, Math.min(hi, v)) });
  };
  const first = rows.slice(0, 50), second = rows.slice(50);
  render(first); assert.equal(measured.min.n, 60);
  render(second); assert.ok(measured.min.n >= 264, 'page two includes the wide number and the pill after its 40th row');
  const count = measures;
  render(first); render([...second]); assert.equal(measures, count, 'previously measured objects stay cached');
  const newcomer = { id: 100, width: 400, pill: 0 };
  render([first[0], newcomer]); assert.equal(measured.min.n, 400, 'a familiar first row cannot hide a new later row');
  render(first, 'hidden'); assert.equal(measures, count + 2);
  render(second); assert.equal(measures, count + 3, 'revealed columns invalidate cached rows');
  render(first); assert.equal(measures, count + 4, 'cache does not retain measurements from a different column set');
  const windowRows = rows.concat(newcomer);
  render(first, '', windowRows);
  render([newcomer], '', windowRows); assert.equal(measures, count + 5, 'a new virtual window measures with the same sorted array');
  wrap.current.clientWidth = 0;
  render([{ id: 101, width: 500, pill: 0 }]); assert.equal(measures, count + 5, 'hidden rows are not marked measured');
  wrap.current.clientWidth = 600;
  render(shown); assert.equal(measured.min.n, 500, 'showing the table retries its deferred measurement');
  assert.equal(wrap.current.scrollLeft, 25); h.close();
});

test('parameter bounds keep fractions and reuse their cached formatting', () => {
  // Use the real cached formatter rather than a stub that could conceal rounding.
  const apiSource = fs.readFileSync(path.join(here, '../src/api.ts'), 'utf8');
  const formatCode = apiSource.slice(apiSource.indexOf('const numberFormats'), apiSource.indexOf('export function time'));
  const functions = run(formatCode.replace('export function', 'function') + '\nreturn {formatNumber};', {});
  const code = declaration('Config.tsx', 'allowedValuesCache') + '\n' + fn('Config.tsx', 'allowedValues') + '\nreturn allowedValues;';
  let calls = 0;
  const allowed = run(code, { formatNumber: (...args) => { calls++; return functions.formatNumber(...args); },
    number: n => functions.formatNumber(n, { maximumFractionDigits: 0 }), bytes: n => `${n} bytes` });
  for (const [min, max] of [[0.25, 0.75], [-0.125, 1.25], [0.000001, 0.000009], [1000, 2000]]) {
    const row = { type: 'double', min, max };
    const expected = [min, max].map(n => n.toLocaleString(undefined, { maximumSignificantDigits: 21 })).join(' to ');
    assert.equal(allowed(row), expected);
    const before = calls; assert.equal(allowed(row), expected); assert.equal(calls, before);
  }
  assert.equal(allowed({ type: 'double', min: null, max: 0.75 }), `? to ${0.75.toLocaleString()}`);
  assert.equal(allowed({ type: 'bytes', min: 1024, max: 2048 }), '1024 bytes to 2048 bytes');
  assert.equal(allowed({ type: 'boolean' }), 'OFF, ON');
  assert.equal(allowed({ type: 'enum', values: ['a', 'b'] }), 'a, b');
});
