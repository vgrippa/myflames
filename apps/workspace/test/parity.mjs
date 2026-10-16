// Chart parity: the TypeScript bundle against the Python SVG
// (_render_svg(..., node_identity=True)) on every fixture, plus focus and
// collapse projections.
//   Visual Explain (render_exploration pages): node identity and order,
//     kinds, join symbols, card geometry and text, edges and widths, edge
//     labels, edge attachment and box overlap.
//   Execution tree, bar chart, flame graph (one page, MyflamesCharts.mount
//     per case): row/bar/frame identity, order, geometry, colors, labels,
//     details text and header text; flame frames also nest inside parents.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-parity-'));
execFileSync(process.env.PYTHON || 'python3', ['-c', `
import glob, json, sys
from pathlib import Path
from myflames.exploration import render_exploration, project_tree
from myflames.output_html_report import _render_svg
from myflames.parser import parse_explain, flatten_nodes
out = Path(sys.argv[1])
cases = []
files = sorted(glob.glob('test/fixtures/*.json') + glob.glob('test/*.json'))
for i, name in enumerate(files):
    root = parse_explain(Path(name).read_text())
    nodes = list(flatten_nodes(root))
    variants = [('', [])]
    inner = [n for n in nodes if n.get('children') and n is not root]
    if inner:
        branch = inner[0]
        variants.append((branch['node_id'], []))
        variants.append(('', [branch['node_id']]))
    for j, (focus, collapsed) in enumerate(variants):
        key = '%03d-%d' % (i, j)
        projected = project_tree(root, focus, collapsed)
        (out / (key + '.svg')).write_text(_render_svg(projected, 'workbench', 1200, 'Query plan', 'ms', node_identity=True))
        (out / (key + '.html')).write_text(render_exploration(root, 'workbench', focus_id=focus, collapsed_ids=collapsed))
        cases.append({'key': key, 'name': name, 'focus': focus, 'collapsed': collapsed,
                      'visible': len(list(flatten_nodes(projected)))})
(out / 'cases.json').write_text(json.dumps(cases))
`, directory], { cwd: root, maxBuffer: 1 << 26 });

const cases = JSON.parse(await fs.readFile(path.join(directory, 'cases.json'), 'utf8'));

// Tree, bar chart and flame graph cases: Python reference SVG + chart data.
execFileSync(process.env.PYTHON || 'python3', ['-c', `
import glob, json, sys
from pathlib import Path
from myflames.chart_data import build_chart_data
from myflames.exploration import project_tree
from myflames.output_html_report import _render_svg
from myflames.parser import parse_explain, flatten_nodes
out = Path(sys.argv[1])
cases = []
files = sorted(glob.glob('test/fixtures/*.json') + glob.glob('test/*.json'))
# A 4.1 ms plan whose operators are mostly under 0.5 ms: both renderers must
# switch to whole microseconds instead of rounding operators away.
table = lambda op, name, ms, access='table': {'operation': op, 'access_type': access, 'table_name': name, 'actual_last_row_ms': ms}
small = json.dumps({'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 4.1, 'inputs': [
    {'operation': 'Nested loop inner join', 'access_type': 'join', 'actual_last_row_ms': 3.5, 'inputs': [
        {'operation': 'Filter: (o.total > 10)', 'access_type': 'filter', 'actual_last_row_ms': 1.2,
         'inputs': [table('Table scan on o', 'orders', 1.0)]},
        table('Index lookup on i using item_order (order_id = o.id)', 'items', 2.0, 'index')]},
    {'operation': 'Filter: (u.active = 1)', 'access_type': 'filter', 'actual_last_row_ms': 0.45,
     'inputs': [table('Single-row index lookup on u using PRIMARY (id = o.user_id)', 'users', 0.3, 'index')]}]})
sources = [(name, Path(name).read_text()) for name in files] + [('synthetic sub-0.5 ms operators', small)]
for i, (name, text) in enumerate(sources):
    root = parse_explain(text)
    nodes = list(flatten_nodes(root))
    variants = [('', [])]
    inner = [n for n in nodes if n.get('children') and n is not root]
    if inner:
        branch = inner[0]
        variants.append((branch['node_id'], []))
        variants.append(('', [branch['node_id']]))
    for j, (focus, collapsed) in enumerate(variants):
        projected = project_tree(root, focus, collapsed)
        data = build_chart_data(root, projection={'focus_id': focus, 'collapsed_ids': collapsed,
                                                  'selected_id': '', 'metric': 'self_time'})
        views = [('tree', {}), ('bargraph', {}), ('flamegraph', {})]
        if j == 0:
            views.append(('flamegraph', {'inverted': True, 'frame_height': 20}))
        for view, options in views:
            key = 'v%03d-%d-%s%s' % (i, j, view, '-inv' if options else '')
            unit = data['display_unit'] if view == 'bargraph' else 'ms'
            extra = {'inverted': True, 'frame_height': 20} if options else {}
            svg = _render_svg(projected, view, 1200, 'Query plan', unit, node_identity=True, **extra)
            (out / (key + '.svg')).write_text(svg)
            (out / (key + '.json')).write_text(json.dumps(data))
            cases.append({'key': key, 'name': name, 'view': view, 'options': options,
                          'focus': focus, 'collapsed': collapsed,
                          'visible': len(list(flatten_nodes(projected))),
                          'measured_total': projected['total_time']})
(out / 'view-cases.json').write_text(json.dumps(cases))
`, directory], { cwd: root, maxBuffer: 1 << 26 });
const viewCases = JSON.parse(await fs.readFile(path.join(directory, 'view-cases.json'), 'utf8'));
const bundle = await fs.readFile(path.join(root, 'myflames/assets/myflames-charts.js'), 'utf8');

// Runs in the page for both documents: tree rows, bars and flame frames.
function extractView(svgRoot, view) {
  const num = (v) => (v === null || v === undefined ? null : Number(v));
  const own = (el, selector) => Array.from(el.children).filter((child) => child.matches(selector));
  const rect = (r) => ({ x: num(r.getAttribute('x')), y: num(r.getAttribute('y')), w: num(r.getAttribute('width')),
    h: num(r.getAttribute('height')), fill: r.getAttribute('fill'), stroke: r.getAttribute('stroke') });
  const text = (t) => [t.textContent, num(t.getAttribute('x')), num(t.getAttribute('y')), t.getAttribute('text-anchor')];
  const result = {
    size: [svgRoot.getAttribute('width'), svgRoot.getAttribute('height'), svgRoot.getAttribute('viewBox')],
    // Python's "Search" control is replaced by the toolbar input above the chart.
    texts: own(svgRoot, 'text').filter((t) => !/^search(-btn)?$/.test(t.id) && t.id !== 'ignorecase').map(text),
  };
  if (view === 'tree') {
    result.rows = Array.from(svgRoot.querySelectorAll('.tree-row')).map((g) => ({
      id: g.getAttribute('data-node-id'), idx: g.getAttribute('data-idx'), depth: g.getAttribute('data-depth'),
      children: g.getAttribute('data-has-children'), teach: g.getAttribute('data-teach-index'),
      info: g.querySelector('.row-bg').getAttribute('data-info'),
      rects: own(g, 'rect').map(rect), texts: own(g, 'text').map(text), lines: own(g, 'line').length,
    }));
  } else if (view === 'bargraph') {
    result.bars = Array.from(svgRoot.querySelectorAll('rect.bar')).map((r) => ({
      ...rect(r), id: r.getAttribute('data-node-id'), info: r.getAttribute('data-info'),
      label: r.getAttribute('data-label'), teach: r.getAttribute('data-teach-index'), cls: r.getAttribute('class'),
    }));
  } else {
    const group = svgRoot.querySelector('.frames');
    result.frames = group ? Array.from(group.children).map((g) => {
      const r = g.querySelector('rect');
      const t = g.querySelector('text');
      return { id: g.getAttribute('data-node-id'), ...rect(r), label: t.textContent,
        tx: num(t.getAttribute('x')), ty: num(t.getAttribute('y')),
        title: (g.querySelector('title')?.textContent || '').split('\n')[0] };
    }) : [];
    result.texts = [];
  }
  return result;
}

// Runs in the page for both documents.
function extract(svgRoot) {
  const nums = (text) => (text.match(/-?\d+(?:\.\d+)?(?:e-?\d+)?/g) || []).map(Number);
  const own = (el, selector) => Array.from(el.children).filter((child) => child.matches(selector));
  const nodes = Array.from(svgRoot.querySelectorAll('.wb-node')).map((g) => {
    const [x, y] = nums(g.getAttribute('transform'));
    const box = own(g, '.wb-box')[0];
    const header = own(g, 'path')[0] || own(g, 'rect:not(.wb-box)')[0];
    const venn = own(g, '.wb-venn')[0];
    return {
      id: g.getAttribute('data-node-id'), kind: g.getAttribute('data-kind'), x, y,
      w: Number(box.getAttribute('width')), h: Number(box.getAttribute('height')),
      label: g.getAttribute('data-label'), aria: g.getAttribute('aria-label'),
      details: g.getAttribute('data-details'), teach: g.getAttribute('data-teach-index'),
      tooltip: own(g, 'title')[0]?.textContent, role: g.getAttribute('role'), tabindex: g.getAttribute('tabindex'),
      color: header ? header.getAttribute('fill') : null,
      texts: own(g, 'text').map((t) => [t.textContent, Number(t.getAttribute('x')), Number(t.getAttribute('y'))]),
      join: venn ? venn.getAttribute('data-join-type') : null,
      vennLabel: venn ? venn.getAttribute('aria-label') : null,
      regions: venn ? Array.from(venn.querySelectorAll('[data-region]')).map((r) => r.getAttribute('data-region') + ':' + r.getAttribute('fill')) : [],
    };
  });
  const edges = Array.from(svgRoot.querySelectorAll('.wb-edge')).map((p) => ({
    from: p.getAttribute('data-from'), to: p.getAttribute('data-to'),
    width: Number(p.getAttribute('stroke-width')), d: nums(p.getAttribute('d')),
  }));
  const labels = Array.from(svgRoot.querySelectorAll('.wb-edge-label')).map((t) =>
    [t.textContent, Number(t.getAttribute('x')), Number(t.getAttribute('y'))]);
  const viewport = svgRoot.querySelector('.wb-viewport');
  const graph = svgRoot.querySelector('.wb-graph');
  const terminal = Array.from(graph.children).filter((el) => el.tagName === 'rect').map((r) => nums(r.getAttribute('x') + ' ' + r.getAttribute('y')));
  const legend = Array.from(svgRoot.children).filter((el) => el.tagName === 'rect' && el.getAttribute('width') === '10').map((r) => r.getAttribute('fill'));
  return {
    size: [svgRoot.getAttribute('width'), svgRoot.getAttribute('height'), svgRoot.getAttribute('viewBox')],
    graph: [Number(viewport.getAttribute('data-graph-width')), Number(viewport.getAttribute('data-graph-height'))],
    viewport: ['x', 'y', 'width', 'height'].map((name) => viewport.getAttribute(name)),
    actions: Array.from(svgRoot.querySelectorAll('[data-action]')).map((b) => [b.getAttribute('data-action'), b.getAttribute('data-action') === 'reset' ? 'live' : b.getAttribute('aria-label')]),
    texts: Array.from(svgRoot.children).filter((el) => el.tagName === 'text').map((t) => t.textContent),
    nodes, edges, labels, terminal, legend,
  };
}

const near = (a, b, tolerance = 0.5) => Math.abs(a - b) <= tolerance;
const browser = await puppeteer.launch({ headless: 'shell' });
let compared = 0;
let nodesCompared = 0;
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1300, height: 1100 });
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error)));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  for (const item of cases) {
    const where = item.name + (item.focus ? ' focus' : '') + (item.collapsed.length ? ' collapse' : '');
    const [pythonSvg, html] = await Promise.all([
      fs.readFile(path.join(directory, item.key + '.svg'), 'utf8'),
      fs.readFile(path.join(directory, item.key + '.html'), 'utf8'),
    ]);
    await page.setContent(html);
    const result = await page.evaluate((source, extractSource) => {
      const extractor = new Function('return ' + extractSource)();
      const python = new DOMParser().parseFromString(source, 'image/svg+xml').documentElement;
      const ts = document.querySelector('svg.wb-plan');
      return {
        python: extractor(python), ts: extractor(ts), wired: ts.getAttribute('data-wired'),
        rows: document.querySelectorAll('.metric-row').length,
        boxes: Array.from(ts.querySelectorAll('.wb-node')).map((g) => {
          const b = g.getBBox();
          return [g.getAttribute('data-node-id'), b.width, b.height];
        }),
      };
    }, pythonSvg, extract.toString());
    const { python, ts } = result;
    assert.equal(result.wired, 'true', where);
    assert.equal(result.rows, item.visible, where + ': metric rows cover visible operators');
    assert.deepEqual(ts.size, python.size, where + ': outer size and viewBox');
    assert.deepEqual(ts.graph, python.graph, where + ': graph extent');
    assert.deepEqual(ts.viewport, python.viewport, where + ': viewport frame');
    assert.deepEqual(ts.actions, python.actions, where + ': controls');
    assert.deepEqual(ts.texts, python.texts, where + ': heading, caption, legend text');
    assert.deepEqual(ts.legend, python.legend, where + ': legend palette');
    assert.deepEqual(ts.nodes.map((n) => n.id), python.nodes.map((n) => n.id), where + ': node identity and order');
    assert.equal(ts.nodes.length, item.visible, where + ': every visible node drawn once');
    const byId = new Map(ts.nodes.map((n) => [n.id, n]));
    python.nodes.forEach((expected, i) => {
      const actual = ts.nodes[i];
      const at = where + ' ' + expected.id;
      // Python projects a copy whose collapsed nodes lose children, which
      // can turn an unlabeled parent "unknown"; the bundle keeps the
      // canonical kind (see report). Compare kind for everything else.
      const collapsedHere = item.collapsed.includes(expected.id);
      if (!collapsedHere) {
        assert.equal(actual.kind, expected.kind, at + ': data-kind');
        assert.equal(actual.color, expected.color, at + ': operator color');
      }
      assert.ok(near(actual.x, expected.x) && near(actual.y, expected.y), at + ': position ' + JSON.stringify([actual.x, actual.y, expected.x, expected.y]));
      assert.equal(actual.w, expected.w, at + ': width');
      assert.equal(actual.h, expected.h, at + ': card height');
      for (const field of ['label', 'aria', 'details', 'teach', 'tooltip', 'role', 'tabindex', 'join', 'vennLabel']) {
        assert.equal(actual[field], expected[field], at + ': ' + field);
      }
      assert.deepEqual(actual.regions, expected.regions, at + ': Venn regions');
      assert.deepEqual(actual.texts.map((t) => t[0]), expected.texts.map((t) => t[0]), at + ': card text');
      actual.texts.forEach((t, k) => assert.ok(near(t[1], expected.texts[k][1]) && near(t[2], expected.texts[k][2]), at + ': text position'));
      nodesCompared++;
    });
    const key = (e) => e.from + '>' + e.to;
    assert.deepEqual(ts.edges.map(key).sort(), python.edges.map(key).sort(), where + ': edge pairs');
    assert.equal(ts.edges.length, Math.max(0, item.visible - 1), where + ': one edge per input');
    const pythonEdges = new Map(python.edges.map((e) => [key(e), e]));
    for (const edge of ts.edges) {
      const expected = pythonEdges.get(key(edge));
      assert.equal(edge.width, expected.width, where + ' ' + key(edge) + ': stroke width');
      edge.d.forEach((v, k) => assert.ok(near(v, expected.d[k]), where + ': edge path'));
      // Attachment: leaves the bottom centre of the source card and ends
      // just above (arrowhead) the top centre of the target card.
      const source = byId.get(edge.from);
      const target = byId.get(edge.to);
      const [sx, sy] = edge.d;
      const [tx, ty] = edge.d.slice(-2);
      assert.ok(near(sx, source.x + source.w / 2) && near(sy, source.y + source.h), where + ': edge leaves source bounds');
      assert.ok(near(edge.d[3], target.x + target.w / 2) && near(ty + 5, target.y), where + ': edge reaches target bounds ' + tx);
    }
    assert.deepEqual(ts.labels.map((l) => l[0]).sort(), python.labels.map((l) => l[0]).sort(), where + ': edge labels');
    assert.deepEqual(ts.terminal.length, python.terminal.length, where + ': output terminal');
    ts.terminal.forEach((point, k) => point.forEach((v, j) => assert.ok(near(v, python.terminal[k][j]), where + ': terminal position')));
    for (let i = 0; i < ts.nodes.length; i++) {
      for (let j = i + 1; j < ts.nodes.length; j++) {
        const a = ts.nodes[i];
        const b = ts.nodes[j];
        const overlap = a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
        assert.ok(!overlap, where + ': boxes overlap ' + a.id + ' ' + b.id);
      }
    }
    // Rendered geometry: each card's box is at least its declared size.
    for (const [id, w, h] of result.boxes) {
      const node = byId.get(id);
      assert.ok(w >= node.w - 0.5 && h >= node.h - 0.5, where + ': rendered card bounds ' + id);
    }
    compared++;
  }
  assert.deepEqual(errors, []);
  const fixtures = new Set(cases.map((c) => c.name)).size;
  console.log(`Parity: ${compared} renders across ${fixtures} fixtures (${nodesCompared} nodes) match the Python SVG: identity, order, kinds, joins, geometry, text, edges, widths, attachment, no overlap`);

  // ---- Execution tree, bar chart, flame graph -------------------------
  await page.setContent('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body><div id="host"></div><script>' + bundle + '</script></body></html>');
  const counts = { tree: 0, bargraph: 0, flamegraph: 0 };
  let marks = 0;
  let flameFrames = 0;
  for (const item of viewCases) {
    const where = item.name + ' ' + item.view + (item.options.inverted ? ' inverted' : '') + (item.focus ? ' focus' : '') + (item.collapsed.length ? ' collapse' : '');
    const [pythonSvg, json] = await Promise.all([
      fs.readFile(path.join(directory, item.key + '.svg'), 'utf8'),
      fs.readFile(path.join(directory, item.key + '.json'), 'utf8'),
    ]);
    const result = await page.evaluate((source, data, view, options, extractSource) => {
      const extractor = new Function('return ' + extractSource)();
      const host = document.getElementById('host');
      if (window.__handle) window.__handle.destroy();
      host.textContent = '';
      window.__handle = MyflamesCharts.mount(host, JSON.parse(data), { view, width: 1200, options });
      const ts = host.querySelector('svg');
      const python = source ? new DOMParser().parseFromString(source, 'image/svg+xml').documentElement : null;
      const out = { empty: !!host.querySelector('.mfc-empty'), ts: ts && extractor(ts, view), python: python && extractor(python, view),
        wired: ts && ts.getAttribute('data-wired') };
      if (view === 'flamegraph' && ts) {
        const parsed = JSON.parse(data);
        const parent = new Map(parsed.nodes.map((n) => [n.id, n.parent_id]));
        out.nesting = Array.from(ts.querySelectorAll('.frames > g[data-node-id]')).map((g) => {
          const r = g.querySelector('rect').getBBox();
          const up = ts.querySelector('.frames > g[data-node-id="' + parent.get(g.dataset.nodeId) + '"] rect');
          const p = up ? up.getBBox() : null;
          return [g.dataset.nodeId, r.x, r.x + r.width, r.y, p && p.x, p && p.x + p.width, p && p.y];
        });
      }
      return out;
    }, pythonSvg, json, item.view, item.options, extractView.toString());
    if (!pythonSvg.trim()) {
      // Python draws nothing when no frame has a whole display unit of time.
      assert.equal(item.view, 'flamegraph', where);
      assert.ok(result.empty, where + ': empty-state message');
      counts[item.view]++;
      continue;
    }
    const { python, ts } = result;
    assert.equal(result.wired, 'true', where);
    assert.deepEqual(ts.size, python.size, where + ': size and viewBox (height + viewBox in lockstep)');
    const sameTexts = (a, b, at) => {
      assert.deepEqual(a.map((t) => t[0]), b.map((t) => t[0]), at + ': text');
      a.forEach((t, k) => assert.ok(near(t[1], b[k][1]) && near(t[2], b[k][2]) && t[3] === b[k][3], at + ': text position ' + t[0]));
    };
    const sameRect = (a, b, at, tolerance = 0.06) => {
      for (const k of ['x', 'y', 'h']) assert.ok(a[k] === b[k] || near(a[k], b[k], tolerance), at + ': rect ' + k + ' ' + a[k] + ' vs ' + b[k]);
      assert.ok(a.w === b.w || near(a.w, b.w, Math.max(tolerance, Math.abs(b.w) * 0.01)), at + ': rect width ' + a.w + ' vs ' + b.w);
      assert.equal(a.fill, b.fill, at + ': fill');
      assert.equal(a.stroke, b.stroke, at + ': stroke');
    };
    if (item.view === 'tree') {
      // An unmeasured plan reads "Total: —" instead of Python's 0.001 ms scaling floor.
      const header = (texts) => texts.map((t, k) => (k === 1 && !item.measured_total ? [t[0].replace(/Total: [^·]*·/, 'Total: ·'), t[1], t[2], t[3]] : t));
      // "Expand All" is end-anchored in TS so it no longer overlaps "Collapse All".
      const links = (texts) => texts.map((t) => (t[0] === 'Expand All' ? [t[0], 0, t[2], ''] : t));
      sameTexts(links(header(ts.texts)), links(header(python.texts)), where + ' header');
      assert.equal(ts.rows.length, item.visible, where + ': one row per visible operator');
      assert.deepEqual(ts.rows.map((r) => r.id), python.rows.map((r) => r.id), where + ': row identity and pre-order');
      python.rows.forEach((expected, k) => {
        const actual = ts.rows[k];
        const at = where + ' ' + expected.id;
        for (const field of ['idx', 'depth', 'children', 'teach', 'info', 'lines']) assert.equal(actual[field], expected[field], at + ': ' + field);
        assert.equal(actual.rects.length, expected.rects.length, at + ': rect count (zebra, accent, bars)');
        actual.rects.forEach((r, n) => sameRect(r, expected.rects[n], at));
        sameTexts(actual.texts, expected.texts, at);
        marks++;
      });
    } else if (item.view === 'bargraph') {
      sameTexts(ts.texts, python.texts, where + ' text');
      assert.deepEqual(ts.bars.map((b) => b.id), python.bars.map((b) => b.id), where + ': bar identity and self-time order');
      python.bars.forEach((expected, k) => {
        const actual = ts.bars[k];
        const at = where + ' ' + expected.id;
        sameRect(actual, expected, at, 0.001);
        for (const field of ['info', 'label', 'teach', 'cls']) assert.equal(actual[field], expected[field], at + ': ' + field);
        marks++;
      });
    } else {
      assert.ok(!result.empty, where + ': frames drawn');
      // Both renderers emit frames in pre-order with inputs in reported order.
      assert.deepEqual(ts.frames.map((f) => f.id), python.frames.map((f) => f.id), where + ': frame identity and order (one per operator with time)');
      python.frames.forEach((expected, k) => {
        const actual = ts.frames[k];
        const at = where + ' ' + expected.id;
        // Depth is the row (y); geometry within 1% of the Python frame.
        for (const field of ['y', 'h', 'ty']) assert.equal(actual[field], expected[field], at + ': ' + field);
        assert.ok(near(actual.x, expected.x, Math.max(0.11, expected.x * 0.01)), at + ': x ' + actual.x + ' vs ' + expected.x);
        assert.ok(near(actual.w, expected.w, Math.max(0.11, expected.w * 0.01)), at + ': width ' + actual.w + ' vs ' + expected.w);
        assert.ok(near(actual.tx, expected.tx, Math.max(0.11, expected.tx * 0.01)), at + ': label x');
        assert.equal(actual.fill, expected.fill, at + ': color');
        assert.equal(actual.label, expected.label, at + ': fitted label');
        assert.equal(actual.title, expected.title, at + ': tooltip header');
        marks++;
        flameFrames++;
      });
      for (const [id, x1, x2, y, px1, px2, py] of result.nesting) {
        if (px1 === null) continue;
        assert.ok(x1 >= px1 - 0.11 && x2 <= px2 + 0.11, where + ' ' + id + ': frame lies within its parent');
        assert.ok(item.options.inverted ? y > py : y < py, where + ' ' + id + ': frame is one level beyond its parent');
      }
    }
    counts[item.view]++;
  }
  assert.deepEqual(errors, []);
  const viewFixtures = new Set(viewCases.map((c) => c.name)).size;
  console.log(`Parity: tree ${counts.tree}, bar chart ${counts.bargraph}, flame graph ${counts.flamegraph} renders (${flameFrames} frames) across ${viewFixtures} fixtures (${marks} rows/bars/frames) match the Python SVG: identity, order, geometry, colors, labels, details, nesting`);
} finally {
  await browser.close();
  await fs.rm(directory, { recursive: true, force: true });
}
