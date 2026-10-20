// Chart runtime suite: pure helpers and the selection bridge in Node (vm,
// no DOM), then real-browser behavior of the bundled charts:
//   Visual Explain: mounting, two charts per page, search, keyboard, drag
//     suppression, the parent bridge in a CSP-locked sandboxed iframe,
//     teach-bridge clicks, SVG export, collapse labels, untrusted text.
//   Execution tree, bar chart, flame graph: mixed charts on one page,
//     collapse/expand (row and all), regex search, hover/pin details, n/N/Esc,
//     Ctrl+F only inside the chart, flame zoom/unzoom with recomputed labels,
//     exact tooltips, µs display, empty state, icicle, no prompt() dialogs.
//   HTML reports (every view): teach CTA bridge, Export SVG, JSON-LD,
//     noscript note, analysis highlights.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const bundle = await fs.readFile(path.join(root, 'myflames/assets/myflames-charts.js'), 'utf8');
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-charts-'));
execFileSync(process.env.PYTHON || 'python3', ['-c', `
import json, sys
from pathlib import Path
from myflames._labels import fit_label
from myflames.chart_assets import chart_embed
from myflames.chart_data import build_chart_data
from myflames.exploration import render_exploration
from myflames.output_workbench import _fmt, _time
from myflames.parser import parse_explain, flatten_nodes
from myflames.teach_hooks import build_teach_hooks, build_teach_index_maps
out = Path(sys.argv[1])
page = lambda body: '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>' + body + '</body></html>'
plan = parse_explain(Path('test/fixtures/explain-044-join-3t-products-category-reviews.json').read_text())
other = parse_explain(Path('test/mysql-explain-hash-join.json').read_text())
teach = build_teach_index_maps(build_teach_hooks(plan))['by_folded_label']
first = build_chart_data(plan, title='First plan', teach_index_by_folded=teach)
second = build_chart_data(other, title='Second plan')
(out / 'two.html').write_text(page('<div id="chart-panel">' + chart_embed(first, 'workbench') + '</div>'
                                   + '<div id="second">' + chart_embed(second, 'workbench', runtime=False) + '</div>'))
tiny = build_chart_data(parse_explain('{"operation":"Table scan on t","access_type":"table","table_name":"t"}'))
broken = dict(tiny, schema_version='chart-data-0')
(out / 'mounts.html').write_text(page(chart_embed(tiny, 'workbench') + chart_embed(broken, 'workbench', runtime=False)
                                      + chart_embed(tiny, 'nonexistent', runtime=False)))
nodes = list(flatten_nodes(plan))
branch = next(n for n in nodes if len(n['children']) == 2)
(out / 'explore.html').write_text(render_exploration(plan, 'workbench', selected_id=nodes[1]['node_id'],
                                                     collapsed_ids=[branch['node_id']], metric='rows'))
hostile = parse_explain(json.dumps({'operation': 'Filter', 'condition': '</script><img src=x onerror=alert(1)>',
    'inputs': [{'operation': 'Table scan on <b>x</b>', 'access_type': 'table', 'table_name': '<svg onload=alert(1)>'}],
    'inputs_from_select_list': [{'operation': 'Select #2 (subquery in projection; run only once)'}]}))
(out / 'hostile.html').write_text(render_exploration(hostile, 'workbench'))
values = [0.0, -0.0, 0.5, 1, 3.625, -3.625, 0.125, 2.675, 1.005, 2.5, 0.0005, 0.00012345, 1e-05, 9.9996,
          999.5, 999.4, 1000, 1234.5, 99999.5, 123456, 1e6, 2.345e7, 1e21, 5e-324, 0.1 + 0.2, 1 / 3, 7.000001]
fixtures = {'values': [[v, '%.3g' % v, '%.2f' % v, '%.1f' % v, _fmt(v), _time(v)] for v in values],
            'labels': [[t, w, s, fit_label(t, w, s)] for t, w, s in [
                ('Index lookup on products using idx_category', 232, 13), ('users.very_long_column_name_here', 120, 12),
                ('\\u8868\\u626b\\u63cf\\u8868\\u626b\\u63cf\\u8868\\u626b\\u63cf\\u8868\\u626b\\u63cf', 60, 12),
                ('emoji \\U0001F600\\U0001F600\\U0001F600 tail', 50, 12), ('a.b', 5, 12), ('', 10, 12),
                ('Covering index skip scan for grouping on t1 using idx_long_name.with_dot', 232, 12)]]}
(out / 'expected.json').write_text(json.dumps(fixtures))
`, directory], { cwd: root });
execFileSync(process.env.PYTHON || 'python3', ['-c', `
import json, sys
from pathlib import Path
from myflames.chart_assets import chart_embed
from myflames.chart_data import build_chart_data
from myflames.flamegraph import FAMILY_COLORS, NO_TIME, PALETTES, fit_frame_label, flame_header, flame_scale, flame_units, flame_value_text, frame_color, label_hash
from myflames.output_html_report import _render_svg
import xml.etree.ElementTree as ET
from myflames.output_bargraph import format_number
from myflames.output_html_report import render_html_report
from myflames.output_tree import _format_time
from myflames.parser import parse_explain
out = Path(sys.argv[1])
page = lambda body: '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>' + body + '</body></html>'
text = Path('test/fixtures/explain-044-join-3t-products-category-reviews.json').read_text()
plan = parse_explain(text)
other = parse_explain(Path('test/mysql-explain-hash-join.json').read_text())
first = build_chart_data(plan, title='Plan A')
(out / 'mixed.html').write_text(page(
    '<div id="tree1">' + chart_embed(first, 'tree') + '</div>'
    + '<div id="bar1">' + chart_embed(first, 'bargraph', runtime=False) + '</div>'
    + '<div id="flame1">' + chart_embed(first, 'flamegraph', runtime=False) + '</div>'
    + '<div id="tree2">' + chart_embed(build_chart_data(other, title='Plan B'), 'tree', runtime=False) + '</div>'
    + '<div id="flame2">' + chart_embed(build_chart_data(other, title='Plan B'), 'flamegraph', runtime=False) + '</div>'
    + '<p id="outside" tabindex="0">outside</p>'))
twins = parse_explain(json.dumps({'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 20,
    'inputs': [{'operation': 'Table scan on t', 'access_type': 'table', 'table_name': 't', 'actual_last_row_ms': 4, 'actual_rows': 5},
               {'operation': 'Table scan on t', 'access_type': 'table', 'table_name': 't', 'actual_last_row_ms': 9, 'actual_rows': 7}]}))
micro = parse_explain('{"operation":"Table scan on t","access_type":"table","table_name":"t","actual_last_row_ms":0.2}')
# 4.1 ms plan whose users branch (and three more operators) is under 0.5 ms.
scan = lambda op, table, ms, **kw: dict(kw, operation=op, access_type=kw.pop('access', 'table'), table_name=table, actual_last_row_ms=ms)
small = parse_explain(json.dumps({'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 4.1, 'inputs': [
    {'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 3.5, 'inputs': [
        {'operation': 'Filter: (o.total > 10)', 'access_type': 'filter', 'condition': '(o.total > 10)', 'actual_last_row_ms': 1.2,
         'inputs': [scan('Table scan on o', 'orders', 1.0)]},
        scan('Index lookup on i using item_order (order_id = o.id)', 'items', 2.0, access='index', index_name='item_order')]},
    {'operation': 'Filter: (u.active = 1)', 'access_type': 'filter', 'condition': '(u.active = 1)', 'actual_last_row_ms': 0.45,
     'inputs': [scan('Single-row index lookup on u using PRIMARY (id = o.user_id)', 'users', 0.3, access='index', index_name='PRIMARY')]}]}))
reference = ET.fromstring(_render_svg(small, 'flamegraph', 1200, 'Query plan', 'ms', node_identity=True).encode('utf-8'))
ns = '{http://www.w3.org/2000/svg}'
small_frames = [[g.get('data-node-id'), g.find(ns + 'title').text.split(chr(10))[0]] for g in reference.find(ns + 'g').findall(ns + 'g')]
flames = {'twins': build_chart_data(twins), 'micro': build_chart_data(micro), 'plan': first,
          'small': build_chart_data(small), 'smallFrames': small_frames,
          'zero': build_chart_data(parse_explain('{"operation":"Table scan","actual_last_row_ms":0}')),
          'unmeasured': build_chart_data(parse_explain('{"operation":"Table scan"}'))}
(out / 'flames.json').write_text(json.dumps(flames))
for view in ('tree', 'bargraph', 'flamegraph', 'workbench', 'diagram'):
    (out / ('report-' + view + '.html')).write_text(render_html_report(text, view_type=view, title='Report ' + view))
review = '{"operation":"Filter: (t.id > 0)","actual_last_row_ms":0.5,"actual_rows":1,"actual_loops":1,"inputs":[{"operation":"Table scan on t","actual_last_row_ms":0.2,"actual_rows":1,"actual_loops":1}]}'
(out / 'report-micro.html').write_text(render_html_report(review, view_type='bargraph'))
for view in ('tree', 'bargraph'):
    (out / ('report-scan-' + view + '.html')).write_text(render_html_report(
        Path('test/fixtures/explain-001-table-scan-users-no-filter.json').read_text(), view_type=view))
names = ['', 'TABLE SCAN [users]', 'NESTED LOOP INNER starts=1 rows=10000', '\\u8868\\u626b\\u63cf', 'emoji \\U0001F600 x',
         'lone \\ud800 surrogate', '--', '-', 'a' * 200]
families = [None, 'other', 'not-a-family'] + sorted(FAMILY_COLORS)
palettes = sorted(PALETTES) + ['HOT', 'unknown', '']
(out / 'expected-views.json').write_text(json.dumps({
    'colors': [[n, f, p, frame_color(n, f, p)] for n in names for f in families for p in palettes],
    'hash': [[n, label_hash(n)] for n in names],
    'fit': [[n, w, fit_frame_label(n, w)] for n in names for w in (0, 5, 20, 27.24, 27.25, 48.48, 100, 1180)],
    'scale': [[selfs, total, list(flame_scale(selfs, total))] for selfs, total in [
        ([0.8], 0.8), ([3.0, 0.3], 4.1), ([0.49999999999999994], 10), ([0.4999], 10), ([9.5, 0.5], 10),
        ([10, 0, None], 10), ([], 0), ([], None), ([2.0], 1.0), ([1e-09], 5), ([1.4, 1.6], None), ([0, None, -1], 0)]],
    'units': [[ms, m, flame_units(ms, m)] for ms in [0, None, -1, 1e-09, 0.0004, 0.0005, 0.2, 0.4999, 0.5, 2.6, 1234.5675]
              for m in (1, 1000)],
    'time': [[v, _format_time(v)] for v in [0, -1, 0.0004, 0.5, 1, 99.994, 99.996, 100, 999.5, 1000, 1234.567, 98765.4]],
    'flameValues': [[v, u, flame_value_text(v, u)] for u in ('ms', '\u00b5s', 'samples') for v in [
        0, 0.3, 1, 2.5, 7, 13, 412, 999.4, 999.5, 7125, 7127, 9994, 9995, 71300, 99949, 99950, 6357231, 1234568]],
    'headers': [[label, v, t, u, flame_header(label, v, t, u)] for label in ('Except starts=1', 'SORT <a & b>')
                for v, t in ((1, 1), (7127, 7127), (300, 4100), (1, 3)) for u in ('\u00b5s', 'ms', 'samples', NO_TIME)],
    'commas': [[v, format_number(v)] for v in [0, 7, 999, 1000, 12345, 1234567, 12.9]],
}))
`, directory], { cwd: root });

// ---- Node: pure helpers and the bridge, without a DOM -------------------
const context = vm.createContext({});
vm.runInContext(bundle, context);
const charts = vm.runInContext('MyflamesCharts', context);
assert.match(bundle.slice(0, 200), /sources-sha256=[0-9a-f]{64}/);
assert.equal(charts.sourcesSha, bundle.match(/sources-sha256=([0-9a-f]{64})/)[1]);
assert.deepEqual([...charts.availableViews()].sort(), ['bargraph', 'diagram', 'flamegraph', 'tree', 'workbench']);
const expected = JSON.parse(await fs.readFile(path.join(directory, 'expected.json'), 'utf8'));
for (const [value, g3, f2, f1, compact, duration] of expected.values) {
  const v = Object.is(value, 0) && 1 / value < 0 ? -0 : value;
  assert.equal(charts.internals.pyG(v, 3), g3, '%.3g of ' + value);
  assert.equal(charts.internals.pyFixed(v, 2), f2, '%.2f of ' + value);
  assert.equal(charts.internals.pyFixed(v, 1), f1, '%.1f of ' + value);
  assert.equal(charts.internals.fmt(v), compact, '_fmt of ' + value);
  assert.equal(charts.internals.time(v), duration, '_time of ' + value);
}
for (const [text, width, size, fitted] of expected.labels) {
  assert.equal(charts.internals.fitLabel(text, width, size), fitted, 'fit_label of ' + text);
}
assert.equal(charts.internals.fmt(null), 'n/a');
assert.equal(charts.internals.time(null), 'n/a', 'missing time is n/a, never zero');
{
  const views = JSON.parse(await fs.readFile(path.join(directory, 'expected-views.json'), 'utf8'));
  // Flame colors, hash and label fitting agree with myflames/flamegraph.py.
  for (const [label, family, palette, color] of views.colors) {
    assert.equal(charts.internals.frameColor(label, family, palette), color, 'frame_color of ' + [label.slice(0, 20), family, palette]);
  }
  for (const [label, hash] of views.hash) assert.equal(charts.internals.labelHash(label), hash, 'label_hash of ' + label.slice(0, 20));
  for (const [label, width, fitted] of views.fit) {
    assert.equal(charts.internals.fitFrameLabel(label, width), fitted, 'fit_frame_label of ' + label.slice(0, 20) + ' at ' + width);
  }
  for (const [value, text] of views.time) assert.equal(charts.internals.treeTime(value), text, 'output_tree._format_time of ' + value);
  for (const [value, text] of views.commas) assert.equal(charts.internals.commas(value), text, 'format_number of ' + value);
  const room = (chars) => 6 + (chars + 0.5) * 12 * 0.59;
  assert.equal(charts.internals.fitFrameLabel('TABLE SCAN [users]', room(6)), 'TABL..', 'cut labels end with two dots');
  assert.equal(charts.internals.fitFrameLabel('TABLE SCAN [users]', room(18)), 'TABLE SCAN [users]');
  assert.equal(charts.internals.fitFrameLabel('ab', room(2)), '', 'fewer than 3 characters of room draws no label');
  assert.equal(charts.internals.frameColor('x', 'table_scan'), '#db9292', 'families reuse the Visual Explain table color');
  assert.equal(charts.internals.frameColor('--', 'table_scan'), '#9aa0a6', 'separators stay grey');
  // Frames: whole display units, inputs in reported order from the parent's
  // left edge, zero-unit frames dropped, nodes never modified.
  const tree = (nodes) => {
    const kids = (n) => n.children.map((id) => nodes.find((x) => x.id === id));
    const frozen = JSON.stringify(nodes);
    const result = (multiplier, span = 1180) => {
      const built = charts.internals.layoutFrames(nodes[0], kids, multiplier, span);
      assert.equal(JSON.stringify(nodes), frozen, 'layout is copy-only');
      return [built.total, Array.from(built.frames, (f) => [f.node.id, f.depth, f.start, f.value])];
    };
    return result;
  };
  for (const [selfs, total, [unit, multiplier]] of views.scale) {
    assert.deepEqual({ ...charts.internals.flameScale(selfs, total) }, { unit, multiplier }, 'flame_scale of ' + JSON.stringify([selfs, total]));
  }
  for (const [ms, multiplier, units] of views.units) assert.equal(charts.internals.flameUnits(ms, multiplier), units, 'flame_units of ' + [ms, multiplier]);
  // The count unit sets widths only; every visible time reads in its own unit.
  for (const [value, unit, text] of views.flameValues) {
    assert.equal(charts.internals.flameValueText(value, unit), text, 'flame_value_text of ' + [value, unit]);
  }
  const readable = charts.internals.flameValueText;
  assert.equal(readable(6357231, 'µs'), '6,357 ms', 'whole ms with separators from 100 ms');
  assert.equal(readable(7127, 'µs'), '7.13 ms', 'two decimals below 10 ms');
  assert.equal(readable(71300, 'µs'), '71.3 ms', 'one decimal below 100 ms');
  assert.equal(readable(300, 'µs'), '300 µs', 'whole µs below 1 ms');
  assert.equal(readable(0.3, 'µs'), '1 µs', 'at least 1 µs');
  assert.equal(readable(0, 'ms'), '0 ms');
  assert.equal(readable(1234567, 'samples'), '1,234,567 samples', 'other units keep integers');
  for (const [label, value, total, unit, text] of views.headers) {
    assert.equal(charts.internals.flameHeader(label, value, total, unit), text, 'flame_header of ' + [label, value, total, unit]);
  }
  assert.equal(charts.internals.flameHeader('Except starts=1', 1, 1, 'no measured time'), 'Except starts=1 (no measured time)',
    'a plan without measured time states no time or share');
  assert.equal(charts.internals.flameScale([0, null], 0).unit, 'no measured time');
  const kept = tree([{ id: 'a', self_ms: 1.4, children: ['b', 'c'] }, { id: 'b', self_ms: 0.4, children: [] },
    { id: 'c', self_ms: 2.6, children: [] }]);
  assert.deepEqual(kept(1), [5, [['a', 0, 0, 5], ['b', 1, 0, 1], ['c', 1, 1, 3]]], 'a measured input never rounds away');
  assert.deepEqual(kept(1000), [4400, [['a', 0, 0, 4400], ['b', 1, 0, 400], ['c', 1, 400, 2600]]], 'µs units');
  // Plans count in µs, so 1.4 ms and 1.6 ms keep a 14:16 width ratio.
  const pair = [{ id: 'p', self_ms: 0, children: ['x', 'y'] }, { id: 'x', self_ms: 1.4, children: [] },
    { id: 'y', self_ms: 1.6, children: [] }];
  const { multiplier: pairScale } = charts.internals.flameScale(pair.map((n) => n.self_ms), 3);
  assert.deepEqual(tree(pair)(pairScale), [3000, [['p', 0, 0, 3000], ['x', 1, 0, 1400], ['y', 1, 1400, 1600]]], '14:16, not 1:2');
  const ordered = tree([{ id: 'r', self_ms: 1, children: ['z', 'y'] }, { id: 'z', self_ms: 2.6, children: ['w'] },
    { id: 'y', self_ms: 1.6, children: [] }, { id: 'w', self_ms: null, children: [] }]);
  assert.deepEqual(ordered(1), [6, [['r', 0, 0, 6], ['z', 1, 0, 3], ['y', 1, 3, 2]]], 'reported order, not alphabetical; unmeasured is 0');
  assert.deepEqual(ordered(1, 0.25)[1].map((f) => f[0]), ['r', 'z'], 'frames under 0.1 px are dropped');
}

function fakeWindow(framed) {
  const listeners = [];
  const posted = [];
  const parent = { postMessage: (message, origin) => posted.push([message, origin]) };
  const win = {
    listeners, posted,
    addEventListener: (type, fn) => listeners.push(fn),
    removeEventListener: (type, fn) => listeners.splice(listeners.indexOf(fn), 1),
  };
  win.parent = framed ? parent : win;
  if (!framed) win.postMessage = (message, origin) => posted.push([message, origin]);
  win.dispatch = (data, source) => listeners.forEach((fn) => fn({ data, source }));
  return win;
}
{
  const win = fakeWindow(true);
  const selected = [];
  const bridge = charts.createBridge(win, (id) => selected.push(id));
  win.dispatch({ type: 'myflames-select', node_id: 'n:aaaaaaaaaaaa' }, win.parent);
  win.dispatch({ type: 'myflames-select', node_id: 'n:bbbbbbbbbbbb' }, {});
  win.dispatch({ type: 'myflames-select', node_id: 42 }, win.parent);
  win.dispatch({ type: 'other', node_id: 'n:cccccccccccc' }, win.parent);
  win.dispatch(null, win.parent);
  assert.deepEqual(selected, ['n:aaaaaaaaaaaa'], 'only well-formed parent messages select');
  bridge.notify('n:aaaaaaaaaaaa');
  assert.deepEqual(JSON.parse(JSON.stringify(win.posted)), [[{ type: 'myflames-node', node_id: 'n:aaaaaaaaaaaa' }, '*']]);
  bridge.dispose();
  assert.equal(win.listeners.length, 0, 'dispose removes the listener');
  const top = fakeWindow(false);
  charts.createBridge(top, () => {}).notify('n:aaaaaaaaaaaa');
  assert.equal(top.posted.length, 0, 'a top-level chart has no parent to notify');
}
{
  const data = { root_id: 'a', nodes: [
    { id: 'a', children: ['b', 'd'], depth: 0 }, { id: 'b', children: ['c'], depth: 1 },
    { id: 'c', children: [], depth: 2 }, { id: 'd', children: [], depth: 1 }] };
  const before = JSON.stringify(data);
  const whole = charts.project(data);
  assert.deepEqual(Array.from(whole.visible, (n) => n.id), ['a', 'b', 'c', 'd'], 'pre-order, input order kept');
  const folded = charts.project(data, '', ['b']);
  assert.deepEqual(Array.from(folded.visible, (n) => n.id), ['a', 'b', 'd']);
  assert.equal(folded.hidden(folded.byId.get('b')), 1);
  const focused = charts.project(data, 'b');
  assert.deepEqual(Array.from(focused.visible, (n) => n.id), ['b', 'c']);
  assert.throws(() => charts.project(data, 'missing'));
  assert.equal(JSON.stringify(data), before, 'projection is copy-only');
}
{
  const posted = [];
  const selection = charts.internals.createSelection((id) => posted.push(id));
  const element = () => { const classes = new Set(); const attrs = {};
    return { attrs, classes, setAttribute: (k, v) => { attrs[k] = v; },
      classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c) } }; };
  const a = element(); const b = element();
  selection.register('a', a); selection.register('b', b);
  assert.equal(selection.select('missing', true), false);
  assert.equal(selection.select('a', true), true);
  assert.equal(selection.select('b', false), true);
  assert.deepEqual(posted, ['a'], 'only notifying selections reach the parent');
  assert.equal(a.attrs['aria-pressed'], 'false');
  assert.equal(b.attrs['aria-pressed'], 'true');
}
console.log('Node: Python-identical number/label formatting, bridge origin filtering and dispose, copy-only projection, selection passed');

// ---- Browser ------------------------------------------------------------
const browser = await puppeteer.launch({ headless: 'shell' });
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1300, height: 1100 });
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  const load = async (name) => page.setContent(await fs.readFile(path.join(directory, name), 'utf8'));

  // Mounting: minimal plan, schema guard, unknown view; failures stay local.
  const consoleErrors = [];
  const onConsole = (message) => { if (message.type() === 'error') consoleErrors.push(message.text()); };
  page.on('console', onConsole);
  await load('mounts.html');
  const mounts = await page.$$eval('.mfc-mount', (els) => els.map((el) => ({
    state: el.getAttribute('data-mfc-mounted'), wired: !!el.querySelector('svg.wb-plan[data-wired="true"]'),
    nodes: el.querySelectorAll('.wb-node').length, text: el.querySelector('.mfc-error')?.textContent || '' })));
  assert.equal(mounts[0].state, 'true'); assert.ok(mounts[0].wired); assert.equal(mounts[0].nodes, 1);
  assert.equal(mounts[1].state, 'error'); assert.match(mounts[1].text, /chart-data-1\.0/);
  assert.equal(mounts[2].state, 'true'); assert.match(mounts[2].text, /not available/);
  assert.equal(consoleErrors.length, 1, 'the broken chart is reported once');
  page.off('console', onConsole);
  assert.equal(await page.evaluate(() => MyflamesCharts.mountAll(document).length), 0, 'mountAll is idempotent');

  // Two charts on one page are fully independent.
  await load('two.html');
  const scoped = (selector, fn, arg) => page.$eval(selector, fn, arg);
  const state = () => page.$$eval('svg.wb-plan', (svgs) => svgs.map((svg) => ({
    box: svg.querySelector('.wb-viewport').getAttribute('viewBox'),
    zoom: svg.querySelector('[data-action="reset"]').textContent,
    dim: svg.querySelectorAll('.wb-node.dim').length,
    selected: Array.from(svg.querySelectorAll('.wb-node.selected')).map((n) => n.dataset.nodeId),
    status: svg.querySelector('.wb-search-status').textContent,
    details: svg.querySelector('.wb-details').textContent,
    marker: svg.querySelector('marker').id, uses: svg.querySelector('.wb-edge')?.getAttribute('marker-end') })));
  const initial = await state();
  assert.equal(initial.length, 2);
  assert.notEqual(initial[0].marker, initial[1].marker, 'instance-unique SVG ids');
  initial.forEach((chart) => assert.equal(chart.uses, 'url(#' + chart.marker + ')'));
  await page.click('#chart-panel [data-action="in"]');
  let now = await state();
  assert.notEqual(now[0].box, initial[0].box);
  assert.equal(now[1].box, initial[1].box, 'zooming one chart leaves the other');
  const total = await page.$$eval('#chart-panel .wb-node', (els) => els.length);
  await page.type('#chart-panel input[type="search"]', 'covering');
  now = await state();
  assert.equal(now[1].dim, 0, 'search is scoped to its chart');
  assert.ok(now[0].dim > 0 && now[0].dim < total);
  assert.equal(now[0].status, (total - now[0].dim) + ' / ' + total, 'search reports matches / total');
  await scoped('#chart-panel input[type="search"]', (el) => { el.value = 'no-such-operator'; el.dispatchEvent(new Event('input')); });
  assert.equal((await state())[0].status, '0 / ' + total);
  await scoped('#chart-panel input[type="search"]', (el) => { el.value = ''; el.dispatchEvent(new Event('input')); });
  assert.equal((await state())[0].dim, 0);
  assert.equal((await state())[0].status, '');

  // Keyboard selection drives the details strip of that chart only.
  await page.$eval('#second .wb-node', (el) => el.focus());
  await page.keyboard.press('Enter');
  now = await state();
  assert.equal(now[1].selected.length, 1);
  assert.equal(now[0].selected.length, 0);
  assert.match(now[1].details, /^Operation: /);
  await page.$eval('#second .wb-node:last-of-type', (el) => el.focus());
  await page.keyboard.press(' ');
  now = await state();
  assert.equal(now[1].selected[0], await page.$eval('#second .wb-node:last-of-type', (el) => el.dataset.nodeId), 'Space selects too');

  // Teach bridge: the report's capture-phase walk finds data-teach-index.
  const teachIndexes = await page.$$eval('#chart-panel .wb-node[data-teach-index]', (els) => els.map((el) => el.dataset.teachIndex));
  assert.ok(teachIndexes.length > 0, 'teach indexes are drawn');
  const teach = await page.evaluate(() => new Promise((resolve) => {
    const panel = document.getElementById('chart-panel');
    panel.addEventListener('click', (event) => {
      let el = event.target;
      while (el && el !== document.body) {
        if (el.getAttribute && el.getAttribute('data-teach-index')) return resolve(el.getAttribute('data-teach-index'));
        el = el.parentNode;
      }
      resolve(null);
    }, { capture: true, once: true });
    const target = panel.querySelector('.wb-node[data-teach-index] .wb-title');
    target.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  }));
  assert.ok(teachIndexes.includes(teach), 'a click inside a card reaches the teach bridge');

  // Export (output_html_report.js exportSVG): self-styled, parseable SVG.
  const exported = await page.evaluate(() => {
    const svg = document.querySelector('#chart-panel svg');
    const text = new XMLSerializer().serializeToString(svg);
    const doc = new DOMParser().parseFromString('<?xml version="1.0" standalone="no"?>\n' + text, 'image/svg+xml');
    return { error: !!doc.querySelector('parsererror'), styled: /\.wb-plan/.test(doc.querySelector('style')?.textContent || ''),
      nodes: doc.querySelectorAll('.wb-node').length, root: doc.documentElement.getAttribute('class') };
  });
  assert.deepEqual(exported, { error: false, styled: true, nodes: total, root: 'wb-plan mfc-workbench' });

  // Dragging from a card pans and must not select it.
  await page.click('#chart-panel [data-action="fit"]');
  const before = (await state())[0];
  const card = await page.$eval('#chart-panel', (panel) => {
    const frame = panel.querySelector('.wb-viewport').getBoundingClientRect();
    const inside = Array.from(panel.querySelectorAll('.wb-node')).map((el) => el.getBoundingClientRect())
      .find((r) => r.left >= frame.left && r.right <= frame.right && r.top >= frame.top && r.bottom <= frame.bottom);
    return { x: inside.left, y: inside.top, width: inside.width, height: inside.height };
  });
  await page.mouse.move(card.x + card.width / 2, card.y + card.height / 2);
  await page.mouse.down();
  await page.mouse.move(card.x + card.width / 2 + 30, card.y + card.height / 2 + 20, { steps: 4 });
  await page.mouse.up();
  now = await state();
  assert.notEqual(now[0].box, before.box, 'drag pans');
  assert.deepEqual(now[0].selected, before.selected, 'drag does not select');

  // Initial selection, collapse labels, and metric list in the explore page.
  await load('explore.html');
  const explore = await page.evaluate(() => ({
    selected: document.querySelector('.wb-node.selected')?.dataset.nodeId,
    pressed: Array.from(document.querySelectorAll('[aria-pressed="true"]')).map((el) => el.dataset.nodeId),
    details: document.querySelector('.wb-details').textContent,
    hidden: Array.from(document.querySelectorAll('.metric-name')).map((el) => el.textContent).filter((t) => /hidden\)$/.test(t)),
    heading: document.querySelector('.metric-panel h2').textContent,
    note: document.querySelector('.metric-panel p').textContent,
    rows: document.querySelectorAll('.metric-row').length, cards: document.querySelectorAll('.wb-node').length,
  }));
  assert.ok(explore.selected, 'projection.selected_id is applied');
  assert.deepEqual(explore.pressed, [explore.selected, explore.selected], 'card and metric row share the selection');
  assert.match(explore.details, /^Operation: /);
  assert.deepEqual(explore.hidden.length, 1);
  assert.match(explore.heading, /^Actual rows · rows per loop$/);
  assert.match(explore.note, /Collapsed descendants are excluded/);
  assert.equal(explore.rows, explore.cards);

  // Untrusted plan text stays text, and SELECT-list inputs are labeled.
  await load('hostile.html');
  assert.equal(await page.$$eval('img, b, .wb-graph svg', (els) => els.length), 0, 'labels never become markup');
  assert.ok(await page.$$eval('text', (els) => els.some((el) => el.textContent.includes('<svg onload=alert(1)>'))));
  assert.ok(await page.$$eval('.wb-edge-label', (els) => els.some((el) => el.textContent === 'select-list subquery')));
  assert.ok(await page.$$eval('.metric-name', (els) => els.some((el) => el.textContent.endsWith('· SELECT-list subquery'))));

  // The workspace iframe: opaque sandbox plus safePreview's CSP.
  const html = (await fs.readFile(path.join(directory, 'explore.html'), 'utf8')).replace(/<head[^>]*>/i, (match) => match +
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'">`);
  await page.setContent('<script>window.messages=[];window.addEventListener("message",e=>window.messages.push(e.data))</script><iframe sandbox="allow-scripts" style="width:1250px;height:1400px"></iframe>');
  await page.$eval('iframe', (element, source) => { element.srcdoc = source; }, html);
  const frame = await (await page.$('iframe')).contentFrame();
  await frame.waitForSelector('svg.wb-plan[data-wired="true"]');
  const violations = await frame.evaluate(() => new Promise((resolve) => {
    const seen = [];
    document.addEventListener('securitypolicyviolation', (event) => seen.push(event.violatedDirective));
    document.querySelector('[data-action="fit"]').click();
    setTimeout(() => resolve(seen), 100);
  }));
  assert.deepEqual(violations, [], 'the bundle runs under the preview CSP');
  assert.ok(await frame.$eval('style#mfc-panel-styles', (el) => el.sheet.cssRules.length > 0), 'panel styles apply under the CSP');
  assert.equal(await page.evaluate(() => window.messages.length), 0, 'initial selection does not notify the parent');
  const ids = await frame.$$eval('.wb-node', (els) => els.map((el) => el.dataset.nodeId));
  await frame.$eval('.wb-node', (el) => el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  await page.waitForFunction((id) => window.messages.some((m) => m.type === 'myflames-node' && m.node_id === id), {}, ids[0]);
  assert.equal(await page.evaluate(() => window.messages.length), 1, 'one selection posts one message');
  await page.$eval('iframe', (el, id) => el.contentWindow.postMessage({ type: 'myflames-select', node_id: id }, '*'), ids[1]);
  await frame.waitForFunction((id) => document.querySelector('.wb-node.selected')?.dataset.nodeId === id, {}, ids[1]);
  await frame.evaluate((id) => window.postMessage({ type: 'myflames-select', node_id: id }, '*'), ids[0]);
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(await frame.$eval('.wb-node.selected', (el) => el.dataset.nodeId), ids[1], 'messages from non-parent windows are ignored');
  assert.equal(await page.evaluate(() => window.messages.length), 1, 'parent-driven selection is not echoed');
  await viewSuite(browser, page, errors);
  assert.deepEqual(errors, []);
  console.log('Browser: mounting, two independent charts, scoped search and counts, keyboard, teach bridge, SVG export, drag suppression, initial selection, collapse, untrusted text, sandbox + CSP bridge passed');
} finally {
  await browser.close();
  await fs.rm(directory, { recursive: true, force: true });
}


// ---- Tree, bar chart, flame graph, and the HTML reports -----------------
async function viewSuite(browser, page, errors) {
  const dialogs = [];
  page.on('dialog', (dialog) => { dialogs.push(dialog.type()); dialog.dismiss(); });
  const load = async (name) => page.setContent(await fs.readFile(path.join(directory, name), 'utf8'));
  // SVG elements are focusable (tabindex) but not HTMLElements for page.focus.
  const focusOn = (selector) => page.$eval(selector, (el) => el.focus());
  await load('mixed.html');
  await page.waitForSelector('#tree2 svg[data-wired="true"]');

  // Tree: rows, toggles, collapse/expand all, nested collapse survives.
  const shown = (scope) => page.$$eval(scope + ' .tree-row', (rows) => rows.filter((r) => r.style.display !== 'none').length);
  const total = await page.$$eval('#tree1 .tree-row', (rows) => rows.length);
  assert.equal(total, 8);
  const treeTwo = await shown('#tree2');
  await page.click('#tree1 .tree-row .toggle');
  assert.equal(await shown('#tree1'), 1, 'collapsing the root hides every descendant');
  assert.equal(await shown('#tree2'), treeTwo, 'the other tree is untouched');
  assert.equal(await page.$eval('#tree1 .tree-row .toggle', (t) => t.textContent), '▸');
  await page.click('#tree1 .tree-row .toggle');
  assert.equal(await shown('#tree1'), total);
  const nested = await page.$$eval('#tree1 .tree-row', (rows) => rows.findIndex((r) => r.dataset.depth === '3' && r.dataset.hasChildren === '1'));
  await page.$$eval('#tree1 .tree-row', (rows, i) => rows[i].querySelector('.toggle').dispatchEvent(new MouseEvent('click', { bubbles: true })), nested);
  const afterNested = await shown('#tree1');
  assert.ok(afterNested < total);
  await page.click('#tree1 .tree-row .toggle');
  await page.click('#tree1 .tree-row .toggle');
  assert.equal(await shown('#tree1'), afterNested, 'a collapsed descendant stays collapsed when its ancestor reopens');
  const positions = await page.$$eval('#tree1 .tree-row', (rows) => rows.filter((r) => r.style.display !== 'none').map((r) => r.getBoundingClientRect().top));
  positions.slice(1).forEach((top, i) => assert.ok(top > positions[i], 'visible rows reflow without gaps'));
  await page.$$eval('#tree1 .ctrl-link', (links) => links.find((l) => l.textContent === 'Collapse All').dispatchEvent(new MouseEvent('click', { bubbles: true })));
  assert.equal(await shown('#tree1'), 1);
  await page.$$eval('#tree1 .ctrl-link', (links) => links.find((l) => l.textContent === 'Expand All').dispatchEvent(new MouseEvent('click', { bubbles: true })));
  assert.equal(await shown('#tree1'), total);
  // Keyboard: arrows collapse/expand, Enter pins, Esc releases.
  await focusOn('#tree1 .tree-row');
  await page.keyboard.press('ArrowLeft');
  assert.equal(await page.$eval('#tree1 .tree-row', (r) => r.getAttribute('aria-expanded')), 'false');
  await page.keyboard.press('ArrowRight');
  assert.equal(await shown('#tree1'), total);
  await page.keyboard.press('Enter');
  const pinnedInfo = await page.$eval('#tree1 .tree-row', (r) => r.querySelector('.row-bg').dataset.info);
  const treeDetails = () => page.$$eval('#tree1 .details-line', (lines) => lines.map((l) => l.textContent).filter(Boolean));
  assert.deepEqual(await treeDetails(), pinnedInfo.split('  ·  '));
  assert.ok(await page.$eval('#tree1 .tree-row', (r) => r.classList.contains('is-selected')));
  await page.hover('#tree1 .tree-row:last-of-type');
  assert.deepEqual(await treeDetails(), pinnedInfo.split('  ·  '), 'pinned details survive hover');
  // Keep pointer hover separate from keyboard unpinning: scrolling a focused
  // row back into view can otherwise generate a new pointerenter after Esc.
  await page.mouse.move(0, 0);
  await page.keyboard.press('Escape');
  assert.match((await treeDetails())[0], /^Click a row to pin details/);
  await page.hover('#tree1 .tree-row:last-of-type');
  assert.equal((await treeDetails())[0], (await page.$eval('#tree1 .tree-row:last-of-type .row-bg', (r) => r.dataset.info)).split('  ·  ')[0], 'hover previews details');
  // Regex search, count, invalid regex; Ctrl+F only from inside the chart.
  await focusOn('#tree1 .tree-row');
  await page.keyboard.down('Control'); await page.keyboard.press('f'); await page.keyboard.up('Control');
  assert.ok(await page.$eval('#tree1 .mfc-toolbar input', (el) => el === document.activeElement), 'Ctrl+F inside the chart opens its search');
  await page.keyboard.type('scan|lookup');
  const dim = await page.$$eval('#tree1 .tree-row.mfc-dim', (rows) => rows.length);
  assert.ok(dim > 0 && dim < total);
  assert.equal(await page.$eval('#tree1 .mfc-search-status', (el) => el.textContent), (total - dim) + ' / ' + total);
  assert.equal(await page.$$eval('#tree2 .tree-row.mfc-dim', (rows) => rows.length), 0, 'search is scoped');
  await page.$eval('#tree1 .mfc-toolbar input', (el) => { el.value = '(unclosed'; el.dispatchEvent(new Event('input')); });
  assert.equal(await page.$eval('#tree1 .mfc-search-status', (el) => el.textContent), 'Invalid regex');
  assert.equal(await page.$eval('#tree1 .mfc-toolbar input', (el) => el.getAttribute('aria-invalid')), 'true');
  await page.keyboard.press('Escape');
  assert.equal(await page.$$eval('#tree1 .tree-row.mfc-dim', (rows) => rows.length), 0, 'Esc clears the search');
  const outside = await page.evaluate(() => new Promise((resolve) => {
    document.getElementById('outside').focus();
    document.addEventListener('keydown', (event) => resolve(event.defaultPrevented), { once: true });
    document.getElementById('outside').dispatchEvent(new KeyboardEvent('keydown', { key: 'f', ctrlKey: true, bubbles: true, cancelable: true }));
  }));
  assert.equal(outside, false, 'browser find is not hijacked outside the chart');
  const cmdF = await page.evaluate(() => {
    const press = (target, platform) => new Promise((resolve) => {
      Object.defineProperty(navigator, 'platform', { value: platform, configurable: true });
      document.addEventListener('keydown', (event) => resolve(event.defaultPrevented), { once: true });
      target.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', metaKey: true, bubbles: true, cancelable: true }));
    });
    return (async () => {
      const row = document.querySelector('#tree1 .tree-row');
      const out = { macInside: await press(row, 'MacIntel'), otherInside: await press(row, 'Win32'), macOutside: await press(document.getElementById('outside'), 'MacIntel') };
      return out;
    })();
  });
  assert.equal(cmdF.macInside, true, 'Cmd+F inside the chart opens its search on macOS');
  assert.equal(cmdF.otherInside, false, 'Cmd+F is left alone on other platforms');
  assert.equal(cmdF.macOutside, false, 'Cmd+F outside the chart keeps browser find on macOS');
  await page.keyboard.press('Escape');

  // Selecting a row hidden by Collapse All reveals it.
  const runtime = await page.evaluate(() => {
    const source = document.querySelector('#tree1 script.mfc-data').textContent;
    const host = document.createElement('div');
    document.body.appendChild(host);
    const handle = MyflamesCharts.mount(host, JSON.parse(source), { view: 'tree' });
    Array.from(host.querySelectorAll('.ctrl-link')).find((l) => l.textContent === 'Collapse All').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    const last = host.querySelector('.tree-row:last-of-type');
    const ok = handle.select(last.dataset.nodeId);
    const result = { ok, display: last.style.display, selected: last.classList.contains('is-selected') };
    handle.destroy();
    result.left = host.children.length;
    host.remove();
    return result;
  });
  assert.deepEqual(runtime, { ok: true, display: '', selected: true, left: 0 });

  // Bar chart: self-time order, hover/pin, search with n/N/Esc and "/".
  const barOrder = await page.evaluate(() => {
    const data = JSON.parse(document.querySelector('#bar1 script.mfc-data').textContent);
    const expected = data.nodes.map((n) => [n.id, n.self_ms || 0]).sort((a, b) => b[1] - a[1]).filter((n) => n[1] >= 0.001).map((n) => n[0]);
    return { expected, actual: Array.from(document.querySelectorAll('#bar1 rect.bar'), (r) => r.dataset.nodeId),
      widths: Array.from(document.querySelectorAll('#bar1 rect.bar'), (r) => Number(r.getAttribute('width'))) };
  });
  assert.deepEqual(barOrder.actual, barOrder.expected, 'bars sorted by self time, sub-µs filtered');
  assert.ok(barOrder.widths.every((w) => w >= 3), '3px minimum width');
  const barDetails = () => page.$$eval('#bar1 .details-line', (lines) => lines.map((l) => l.textContent).filter(Boolean));
  await page.hover('#bar1 rect.bar');
  assert.equal((await barDetails())[0], await page.$eval('#bar1 rect.bar', (r) => r.dataset.info.split('  ·  ')[0]));
  await page.click('#bar1 rect.bar:last-of-type');
  const pinnedBar = await page.$eval('#bar1 rect.bar.is-selected', (r) => r.dataset.info);
  await page.hover('#bar1 rect.bar');
  assert.deepEqual(await barDetails(), pinnedBar.split('  ·  '), 'click pins details');
  await page.keyboard.press('/');
  assert.ok(await page.$eval('#bar1 .mfc-toolbar input', (el) => el === document.activeElement), '"/" opens the search');
  await page.keyboard.type('scan|index');
  const matchCount = await page.$$eval('#bar1 rect.bar', (bars) => bars.filter((b) => b.style.stroke).length);
  assert.ok(matchCount > 1);
  const activeMatch = () => page.$$eval('#bar1 rect.bar', (bars) => bars.findIndex((b) => b.style.stroke === 'rgb(230, 0, 230)'));
  const firstActive = await activeMatch();
  assert.equal(await page.$eval('#bar1 .mfc-search-status', (el) => el.textContent), '1 / ' + matchCount);
  await focusOn('#bar1 rect.bar');
  await page.keyboard.press('n');
  assert.notEqual(await activeMatch(), firstActive, 'n steps to the next match');
  await page.keyboard.press('N');
  assert.equal(await activeMatch(), firstActive, 'N steps back');
  assert.ok(await page.$$eval('#bar1 rect.bar', (bars) => bars.some((b) => b.style.opacity === '0.3')), 'non-matches are dimmed');
  await page.keyboard.press('Escape');
  assert.equal(await page.$$eval('#bar1 rect.bar', (bars) => bars.filter((b) => b.style.stroke || b.style.opacity).length), 0, 'Esc clears');
  assert.equal(await page.$$eval('#tree1 .tree-row.mfc-dim', (rows) => rows.length), 0);

  // Flame graph: zoom/unzoom with recomputed labels, search, ignore case.
  const MATCH = '#ff38c8';
  const frameState = () => page.$$eval('#flame1 .frames > g', (gs) => gs.map((g) => ({
    id: g.dataset.nodeId, cls: g.getAttribute('class') || '', x: Number(g.querySelector('rect').getAttribute('x')),
    w: Number(g.querySelector('rect').getAttribute('width')), label: g.querySelector('text').textContent,
    fill: g.querySelector('rect').getAttribute('fill'), title: g.querySelector('title').textContent })));
  const before = await frameState();
  assert.ok(before.every((f) => f.id), 'every frame carries its node id');
  // The covering index lookup has a sibling join that must hide when zoomed.
  const target = before.find((f) => f.title.startsWith('COVERING INDEX'));
  await page.$eval('#flame1 .frames > g[data-node-id="' + target.id + '"]', (g) => g.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  let after = await frameState();
  const zoomedFrame = after.find((f) => f.id === target.id);
  assert.equal(zoomedFrame.x, 10);
  assert.equal(zoomedFrame.w, 1180, 'the clicked frame fills the width');
  assert.ok(zoomedFrame.label.length > target.label.length, 'labels are recomputed for the new width');
  assert.ok(!zoomedFrame.label.endsWith('..') || zoomedFrame.label.length * 7.08 > 1100, 'the zoomed label uses the new room');
  assert.ok(after.some((f) => /fg-ancestor/.test(f.cls)), 'ancestors stay as dimmed parents');
  assert.ok(after.filter((f) => /fg-ancestor/.test(f.cls)).every((f) => f.x === 10 && f.w === 1180), 'ancestors span the width');
  assert.ok(after.some((f) => /fg-hidden/.test(f.cls)), 'unrelated frames hide');
  assert.equal(await page.$$eval('#flame2 .frames > g.fg-hidden, #flame2 .frames > g.fg-ancestor', (gs) => gs.length), 0, 'the other flame graph keeps its view');
  assert.ok(await page.$eval('#flame2 .fg-reset', (el) => el.classList.contains('fg-off')));
  assert.ok(!(await page.$eval('#flame1 .fg-reset', (el) => el.classList.contains('fg-off'))), 'Reset zoom appears');
  const header = target.title.split('\n')[0];
  assert.ok((await page.$eval('#flame1 .fg-details', (el) => el.textContent)).startsWith(header), 'details line shows the selected frame');
  assert.ok(await page.$eval('#flame1 .frames > g[data-node-id="' + target.id + '"]', (g) => g.classList.contains('is-selected')), 'clicking selects');
  await page.$eval('#flame1 .frames > g.fg-ancestor', (g) => g.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  after = await frameState();
  const layoutOf = (frames) => frames.map((f) => [f.x, f.w, f.label, f.cls.replace('is-selected', '').trim()]);
  assert.deepEqual(layoutOf(after), layoutOf(before), 'clicking the root ancestor unzooms (and selects it)');
  assert.ok(await page.$eval('#flame1 .fg-reset', (el) => el.classList.contains('fg-off')), 'Reset zoom hides again');
  // Zoom in, then the Reset zoom control and a background double click restore the full view.
  await page.$eval('#flame1 .frames > g[data-node-id="' + target.id + '"]', (g) => g.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  await page.$eval('#flame1 .fg-reset', (el) => el.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  assert.deepEqual(layoutOf(await frameState()), layoutOf(before), 'Reset zoom restores the full view');
  await page.$eval('#flame1 .frames > g[data-node-id="' + target.id + '"]', (g) => g.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  await page.$eval('#flame1 svg.mfc-flame > rect', (el) => el.dispatchEvent(new MouseEvent('dblclick', { bubbles: true })));
  assert.deepEqual(layoutOf(await frameState()), layoutOf(before), 'double-clicking the background resets the zoom');
  await focusOn('#flame1 .frames > g[data-node-id="' + target.id + '"]');
  await page.keyboard.press('Enter');
  assert.equal((await frameState()).find((f) => f.id === target.id).w, 1180, 'Enter zooms');
  await page.keyboard.press('Escape');
  assert.deepEqual((await frameState()).map((f) => f.w), before.map((f) => f.w), 'Esc unzooms');
  await focusOn('#flame1 .frames > g');
  await page.keyboard.press('F3');
  assert.ok(await page.$eval('#flame1 .mfc-toolbar input', (el) => el === document.activeElement), 'F3 opens the inline search');
  await page.keyboard.type('TABLE SCAN');
  const highlighted = () => page.$$eval('#flame1 .frames rect', (rects, color) => rects.filter((r) => r.getAttribute('fill') === color).length, MATCH);
  const found = await highlighted();
  assert.ok(found > 0);
  assert.equal(found, before.filter((f) => /TABLE SCAN/i.test(f.title.split('\n')[0].replace(/ \([^()]*\)$/, ''))).length, 'every matching frame is highlighted');
  assert.match(await page.$eval('#flame1 .fg-matched', (el) => el.textContent), /^Matched: \d+\.\d%$/);
  assert.equal(await page.$$eval('#flame2 .frames rect', (rects, color) => rects.filter((r) => r.getAttribute('fill') === color).length, MATCH), 0, 'search is scoped to its chart');
  assert.equal(await page.$eval('#flame2 .fg-matched', (el) => el.textContent), '');
  assert.equal(await page.$eval('#flame1 .mfc-search-status', (el) => el.textContent), found + (found === 1 ? ' frame' : ' frames'));
  await page.$eval('#flame1 .mfc-toolbar input', (el) => { el.value = 'table scan'; el.dispatchEvent(new Event('input')); });
  assert.equal(await highlighted(), found, 'ignore case is on by default');
  await page.click('#flame1 .mfc-toggle input');
  assert.ok(!(await page.$eval('#flame1 .mfc-toggle input', (el) => el.checked)));
  assert.equal(await page.$eval('#flame1 .mfc-search-status', (el) => el.textContent), '0 frames', 'case-sensitive once unticked');
  assert.equal(await highlighted(), 0);
  assert.equal(await page.$eval('#flame1 .fg-matched', (el) => el.textContent), 'Matched: 0.0%');
  await page.$eval('#flame1 .frames > g[data-node-id="' + target.id + '"]', (g) => g.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  await focusOn('#flame1 .mfc-toolbar input');
  await page.keyboard.press('Escape');
  assert.equal(await page.$eval('#flame1 .mfc-toolbar input', (el) => el.value), '', 'Esc clears the search first');
  assert.deepEqual((await frameState()).map((f) => f.fill), before.map((f) => f.fill), 'clearing restores colors');
  assert.equal(await page.$eval('#flame1 .fg-matched', (el) => el.textContent), '');
  assert.equal((await frameState()).find((f) => f.id === target.id).w, 1180, 'the zoom survives the first Esc');
  await page.keyboard.press('Escape');
  assert.deepEqual((await frameState()).map((f) => f.w), before.map((f) => f.w), 'the second Esc resets the zoom');
  // Cmd+F inside the chart opens its search; outside it stays with the browser.
  await focusOn('#flame1 .frames > g');
  await page.keyboard.down('Meta'); await page.keyboard.press('f'); await page.keyboard.up('Meta');
  assert.ok(await page.$eval('#flame1 .mfc-toolbar input', (el) => el === document.activeElement), 'Cmd+F opens the inline search');
  // External selection highlights the frame and reveals it when zoomed away.
  const reveal = await page.evaluate((id) => {
    const source = document.querySelector('#flame1 script.mfc-data').textContent;
    const host = document.createElement('div');
    document.body.appendChild(host);
    const handle = MyflamesCharts.mount(host, JSON.parse(source), { view: 'flamegraph', bridge: false });
    const frames = Array.from(host.querySelectorAll('.frames > g'));
    const span = (g) => {
      const r = g.querySelector('rect');
      const x = Number(r.getAttribute('x'));
      return [x, x + Number(r.getAttribute('width'))];
    };
    const [from, to] = span(host.querySelector('.frames > g[data-node-id="' + id + '"]'));
    // A frame beside the target (neither its ancestor nor its descendant).
    const other = frames.find((g) => { const [a, b] = span(g); return b <= from + 0.01 || a >= to - 0.01; });
    other.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    const hidden = host.querySelector('.frames > g[data-node-id="' + id + '"]').classList.contains('fg-hidden');
    const ok = handle.select(id);
    const g = host.querySelector('.frames > g[data-node-id="' + id + '"]');
    const result = { hidden, ok, selected: g.classList.contains('is-selected'), shown: !g.classList.contains('fg-hidden') };
    handle.destroy();
    result.left = host.children.length;
    host.remove();
    return result;
  }, target.id);
  assert.deepEqual(reveal, { hidden: true, ok: true, selected: true, shown: true, left: 0 });
  assert.deepEqual(dialogs, [], 'no prompt()/alert() dialogs');

  // Flame graph edge cases, mounted directly.
  const flames = JSON.parse(await fs.readFile(path.join(directory, 'flames.json'), 'utf8'));
  const flame = (data, options = {}) => page.evaluate((data, options) => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    MyflamesCharts.mount(host, data, { view: 'flamegraph', options });
    const result = { empty: host.querySelector('.mfc-empty')?.textContent || '',
      frames: Array.from(host.querySelectorAll('.frames > g'), (g) => ({ id: g.dataset.nodeId, title: g.querySelector('title').textContent,
        y: Number(g.querySelector('rect').getAttribute('y')) })) };
    host.remove();
    return result;
  }, data, options);
  const twins = await flame(flames.twins);
  const scans = twins.frames.filter((f) => f.title.startsWith('TABLE SCAN'));
  assert.equal(scans.length, 2, 'equal labels stay separate frames');
  const rows = scans.map((f) => f.title.match(/Rows: (\d+) actual/)[1]).sort();
  assert.deepEqual(rows, ['5', '7'], 'each tooltip describes its own operator, not a fuzzy match');
  scans.forEach((f) => assert.equal(f.id, flames.twins.nodes.find((n) => n.flame_details === f.title.split('\n\n')[1]).id));
  const micro = await flame(flames.micro);
  assert.match(micro.frames[0].title, /\(200 µs, 100\.00%\)/, 'sub-millisecond plans count microseconds');
  for (const name of ['zero', 'unmeasured']) assert.match((await flame(flames[name])).empty, /^No positive time/);
  // A few-ms plan with sub-0.5 ms operators: every operator keeps its frame,
  // counted in µs exactly like the Python renderer, each header readable.
  const small = await flame(flames.small);
  assert.equal(small.frames.length, flames.small.nodes.length, 'no measured operator rounds away');
  assert.deepEqual(small.frames.map((f) => [f.id, f.title.split('\n')[0]]), flames.smallFrames, 'same frames, order and headers as Python');
  assert.match(small.frames[0].title, /\(4\.10 ms, 100\.00%\)/, 'µs counts read as ms from 1 ms');
  assert.ok(small.frames.some((f) => /\(300 µs, 7\.32%\)/.test(f.title)), 'a 0.3 ms operator reads 300 µs');
  assert.ok(small.frames.some((f) => /users/.test(f.title)), 'the users branch survives');
  const normal = await flame(flames.plan);
  const icicle = await flame(flames.plan, { inverted: true, frame_height: 20 });
  const depthOf = (frames, id) => frames.find((f) => f.id === id).y;
  const rootId = flames.plan.root_id;
  const childId = flames.plan.nodes[1].id;
  assert.ok(depthOf(normal.frames, childId) < depthOf(normal.frames, rootId), 'flame: inputs stack upward');
  assert.ok(depthOf(icicle.frames, childId) > depthOf(icicle.frames, rootId), 'icicle: inputs hang downward');

  // HTML reports for every view: teach CTA, Export SVG, JSON-LD, noscript.
  for (const view of ['tree', 'bargraph', 'flamegraph', 'workbench', 'diagram']) {
    const file = 'report-' + view + '.html';
    const html = await fs.readFile(path.join(directory, file), 'utf8');
    assert.match(html, /<noscript><p class="mfc-noscript">/, view + ': noscript note');
    await page.setContent(html);
    await page.waitForSelector('#chart-panel svg[data-wired="true"]');
    const report = await page.evaluate(async () => {
      const jsonld = JSON.parse(document.querySelector('script[type="application/ld+json"]').textContent);
      const cta = document.getElementById('open-teach-btn');
      const before = cta.hidden;
      const teach = document.querySelector('#chart-panel [data-teach-index]');
      (teach.querySelector('rect, text') || teach).dispatchEvent(new MouseEvent('click', { bubbles: true }));
      let blob = null;
      const original = URL.createObjectURL;
      URL.createObjectURL = (value) => { blob = value; return 'blob:test'; };
      HTMLAnchorElement.prototype.click = () => {};
      exportSVG();
      URL.createObjectURL = original;
      const exported = new DOMParser().parseFromString(await blob.text(), 'image/svg+xml');
      return { schema: jsonld.schema_version, tree: Array.isArray(jsonld.plan_tree) || typeof jsonld.plan_tree === 'object',
        ctaBefore: before, ctaAfter: cta.hidden, hint: document.getElementById('teach-cta-hint').textContent,
        error: !!exported.querySelector('parsererror'), cls: exported.documentElement.getAttribute('class'),
        styled: (exported.querySelector('style')?.textContent || '').length > 100,
        toolbar: exported.querySelectorAll('input').length };
    });
    assert.ok(report.schema && report.tree, view + ': JSON-LD sidecar');
    assert.equal(report.ctaBefore, true);
    assert.equal(report.ctaAfter, false, view + ': clicking an operator offers its lesson');
    assert.match(report.hint, /^Selected: /);
    assert.equal(report.error, false, view + ': exported SVG parses');
    assert.ok(report.styled, view + ': exported SVG carries its own styles');
    assert.equal(report.cls, { tree: 'mfc-tree', bargraph: 'mfc-bar', flamegraph: 'mfc-flame' }[view] || 'wb-plan mfc-workbench');
    if (view !== 'workbench' && view !== 'diagram') assert.equal(report.toolbar, 0, view + ': export excludes the search toolbar');
  }
  await page.setContent(await fs.readFile(path.join(directory, 'report-micro.html'), 'utf8'));
  const microInfo = await page.$$eval('#chart-panel rect.bar', (bars) => bars.map((b) => b.dataset.info));
  assert.ok(microInfo.some((info) => info.includes('Self: 300 µs') && info.includes('Total: 500 µs')), 'report bar chart scales ms for µs display only');
  for (const view of ['tree', 'bargraph']) {
    await page.setContent(await fs.readFile(path.join(directory, 'report-scan-' + view + '.html'), 'utf8'));
    const flagged = view === 'tree'
      ? await page.$$eval('#chart-panel .row-label.warn', (els) => els.length)
      : await page.$$eval('#chart-panel rect.bar.in-query-analysis', (els) => els.filter((e) => e.dataset.analysisMsg).length);
    assert.equal(flagged, 1, view + ': analysis highlights reach the report chart');
  }
  assert.deepEqual(errors, []);
  console.log('Browser: tree collapse/expand/all/keyboard/search/reveal, bar order/pin/search n/N/Esc, flame zoom/unzoom/search/ignore-case/exact tooltips/µs/empty/icicle, Ctrl+F scoped to the chart, no dialogs, reports (teach CTA, Export SVG, JSON-LD, noscript, highlights) passed');
}
