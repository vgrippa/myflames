// Exercise the real gate through --from, plus runner actions with a fake
// Puppeteer surface. No Chrome/server: timings are explicit test fixtures.
import assert from 'node:assert/strict';
import { test, after } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';
const here = path.dirname(fileURLToPath(import.meta.url));
const root = process.env.PERF_SOURCE_ROOT || path.join(here, '..');
const runner = fs.readFileSync(path.join(root, 'test/perf-measure.mjs'), 'utf8');
const ast = ts.createSourceFile('perf-measure.mjs', runner, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
function pick(predicate) {
  let found;
  function visit(n) { if (!found && predicate(n)) found = n; if (!found) ts.forEachChild(n, visit); }
  visit(ast); assert.ok(found, 'runner test subject exists'); return found.getText(ast);
}
const fn = name => pick(n => ts.isFunctionDeclaration(n) && n.name?.text === name);
function evaluate(code, env) { return new Function(...Object.keys(env), code)(...Object.values(env)); }
const metrics = { content_ms: 10, rows_drawn: 40, key_p50_ms: 10, key_p95_ms: 15, p95_ms: 12, layout_ms: 5 };
const measured = name => ({ name, ...metrics });
const env = {
  PLACES: ['home', 'server', 'workload', 'query', 'plans'],
  goPlace: async (_s, place, name = `place ${place}`) => measured(name),
  goView: async (_s, view, name = `view ${view}`) => measured(name),
  viewSel: kind => `.server-view[data-view="${kind}"]`,
  interact: async (_s, name) => measured(name), scrollMeasure: async (_s, name) => measured(name),
  tableScenario: async (_s, { label }) => ['first render', 'sort', 'type in filter', 'scroll'].map(suffix => measured(`${label}: ${suffix}`)),
};
// The labels for place/table measurements come from the actual runner. A
// checker-only rename cannot make this fixture pass with stale runner names.
const makeRows = evaluate([fn('step'), fn('placesScenario'), fn('tablesScenario'),
  'return async s => [...await placesScenario(s), ...await tablesScenario(s)];'].join('\n'), env);
const scenarioRows = await makeRows({ page: { $: async () => true } });
const otherNames = ['command palette open (click)', 'command palette open (⌘K)', 'info popover open',
  'sheet open (Kill sessions…)', 'inspector open (select row)', 'editor type at end (5,000 lines)',
  'editor type in middle (5,000 lines)', 'place home (5,000-line script open)',
  'chart diagram (Visual Explain) (synthetic 1,000 operators): hover',
  'chart diagram (Visual Explain) (synthetic 1,000 operators): wheel'];
const rows = ['1440x900', '720x520'].flatMap(size => [...scenarioRows, ...otherNames.map(measured)].map(r => ({ ...r, size })));
rows.push(...['memory: every place ×5', 'memory: every Server and Workload view ×5'].map(name => ({ name, size: '1440x900', node_growth: 100, heap_growth_mb: 1 })));
const fixture = () => ({ results: structuredClone(rows), medians: structuredClone(rows) });
function guardFixture() {
  const data = fixture();
  // Include the old aliases here only, so reverting error/memory guards
  // fails these tests for that defect instead of an unrelated name mismatch.
  for (const key of ['results', 'medians']) for (const row of [...data[key]]) {
    if (row.name === 'place home (warm)') data[key].push({ ...row, name: 'place home (first)' });
    if (row.name.startsWith('Parameters all:')) data[key].push({ ...row, name: row.name.replace('Parameters all:', 'Parameters:') });
  }
  return data;
}
const directory = fs.mkdtempSync(path.join(here, '.perf-budget-test-'));
after(() => fs.rmSync(directory, { recursive: true, force: true }));
function gate(data) {
  const filename = path.join(directory, 'results.json');
  fs.writeFileSync(filename, JSON.stringify(data));
  const result = spawnSync(process.execPath, [path.join(root, 'test/perf-budgets.mjs'), '--from', filename], { encoding: 'utf8' });
  assert.ifError(result.error);
  return { status: result.status, output: result.stdout + result.stderr };
}
function fails(data, reason) {
  const result = gate(data); assert.notEqual(result.status, 0, 'gate must fail'); assert.match(result.output, reason);
}

test('gate accepts actual Home warm/Parameters all names and empty error arrays', () => {
  const data = fixture(); data.results.push({ size: '1440x900', name: 'no page errors', errors: [] });
  const result = gate(data); assert.equal(result.status, 0, result.output); assert.match(result.output, /100 checks passed/);
  // Interrupted runs may contain only raw results, which remain supported.
  delete data.medians; assert.equal(gate(data).status, 0);
});

for (const [label, selector] of [
  ['sheet open (Kill sessions…)', '.server-view[data-view="sessions"] button[data-action="kill-sessions"]'],
  ['import dialog', '.plans-import'],
]) test(`${label} timing action uses Puppeteer input while interact requires an input event`, async () => {
  const call = pick(n => ts.isCallExpression(n) && n.expression.getText(ast) === 'step' && n.arguments[1]?.getText(ast) === JSON.stringify(label));
  const clicks = [], outputs = [];
  const page = { evaluate: async () => true, click: async selector => clicks.push(selector), keyboard: { press: async key => clicks.push(key) } };
  const interact = async (_s, name, act) => {
    const before = clicks.length; await act(); assert.ok(clicks.length > before, `${name} must produce a real Puppeteer input`); return { name };
  };
  // Let assertion failures propagate; production step catches them as errors.
  await evaluate(`return (async () => { await ${call}; })();`, {
    page, s: { page }, out: outputs, viewSel: env.viewSel, interact,
    step: async (out, _name, body) => out.push(...await body()),
  });
  assert.deepEqual(clicks, [selector, 'Escape']);
  assert.equal(outputs.length, 2);
});

test('floating and narrow view list measurements use Puppeteer input and release the handle', async () => {
  for (const pushed of [false, true]) {
    let evaluations = 0, clicks = 0, disposed = 0;
    const page = {
      $: async () => null, waitForFunction: async () => {},
      evaluate: async () => ++evaluations === 1 ? false : pushed,
      evaluateHandle: async (_body, selector) => {
        assert.equal(selector, pushed ? '.server-view .server-back' : '.server-view .view-switcher');
        return { asElement: () => ({ click: async () => { clicks++; } }), dispose: async () => { disposed++; } };
      },
      screenshot: async () => {},
    };
    const open = evaluate(fn('openViewList') + '\nreturn openViewList;', {
      path, OUT: '.', interact: async (_s, _name, act) => { await act(); assert.equal(clicks, 1); },
    });
    await open({ page }, true); assert.equal(disposed, 1);
  }
});

test('raw page errors fail even when medians are clean', () => {
  const data = guardFixture(); data.results.push({ size: '1440x900', name: 'editor: page errors', errors: ['TypeError: render failed'] });
  fails(data, /Unexpected page errors.*render failed/);
});

test('medians alone cannot establish that the raw runs had no page errors', () => {
  const data = guardFixture(); delete data.results;
  fails(data, /expected raw results for error checks/);
});

test('a failed or timed-out later run cannot hide behind clean medians', () => {
  for (const extra of [{ error: 'late run failed' }, { timed_out: true }]) {
    const data = guardFixture(); data.results.push({ name: 'unbudgeted step', size: '720x520', ...extra });
    fails(data, /unbudgeted step.*(failed|timed out)/);
  }
});

test('both memory measurements are mandatory', () => {
  for (const prefix of ['memory: every place', 'memory: every Server and Workload view']) {
    const data = guardFixture(); for (const key of ['results', 'medians']) data[key] = data[key].filter(r => !r.name.startsWith(prefix));
    fails(data, /Missing measurement: 1440x900 :: memory:/);
  }
});

test('missing and nonnumeric memory values fail, and memory regressions exceed their budgets', () => {
  for (const prefix of ['memory: every place', 'memory: every Server and Workload view']) {
    for (const field of ['heap_growth_mb', 'node_growth']) for (const value of [undefined, null, '0', 100000]) {
      const data = guardFixture(); data.medians.find(r => r.name.startsWith(prefix))[field] = value;
      fails(data, /missing finite numeric|Budget exceeded/);
    }
  }
});

test('timing regressions and missing required measurements still fail', () => {
  const data = fixture(); data.medians.find(r => r.name === 'place home (warm)').content_ms = 251;
  fails(data, /Budget exceeded.*place home \(warm\)/);
  const missing = fixture(); missing.medians = missing.medians.filter(r => r.name !== 'Parameters all: sort');
  fails(missing, /Missing measurement.*Parameters all: sort/);
});
