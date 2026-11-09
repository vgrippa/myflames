// W4PLAN: measured layout and keyboard checks on an isolated workspace.
// MYFLAMES_TEST_ASSETS points at a Vite --outDir build. No database is used.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { open, pause } from './perf-harness.mjs';

const baseline = process.env.W4PLAN_BASELINE === '1';
const shots = process.env.W4PLAN_SHOTS || '/tmp/myflames-w4plan';
await fs.mkdir(shots, { recursive: true });
const ok = body => ({ status: 200, body });
const h = await open('/api/unused', 'plan-editor-layout', {
  '/api/editor/status': ok({ status: 'none', session: null, run: null }),
  '/api/editor/preview': ok({ status: 'ready', statements: [], aggregate: { state: 'unknown', missing: [], reason: '', warning: '' }, policy: { action: 'allow', text: '' } }),
  '/api/editor/history': ok({ items: [] }),
  '/api/live/schemas': ok({ schema_version: 'schemas-1.0', engine: 'mysql', server_version: '8.4.3', schemas: [{ name: 'shop', tables: 1, system: false }], system: [], all_note: '' }),
});
const { page } = h;
const schemaReads = () => h.state.other.filter(r => r.path === '/api/live/schemas').length;
const measures = [];
async function capture(state, width, theme) {
  await pause(180);
  const m = await page.evaluate(() => {
    const box = selector => {
      const e = document.querySelector(selector);
      if (!e || !e.checkVisibility()) return null;
      const b = e.getBoundingClientRect();
      return { x: b.x, y: b.y, width: b.width, height: b.height, overflow: e.scrollWidth - e.clientWidth };
    };
    return { overflow: document.documentElement.scrollWidth - innerWidth,
      inspector: box('.plan-inspector'), chart: box('.plan-chart'), toolbar: box('.editor-toolbar'),
      toolbarButtons: [...document.querySelectorAll('.editor-toolbar button')].filter(e => e.checkVisibility()).map(e => ({ label: e.getAttribute('aria-label') || e.textContent.trim(), x: e.getBoundingClientRect().x, y: e.getBoundingClientRect().y, width: e.getBoundingClientRect().width })),
      settings: box('.settings-popover'), schemas: box('.schema-browser') };
  });
  measures.push({ state, width, theme, ...m });
  assert.equal(m.overflow, 0, `${state}: no horizontal page overflow`);
  await page.screenshot({ path: path.join(shots, `${state}-${width}-${theme}.png`) });
  return m;
}
try {
  await h.button('Home', '.main-nav ');
  await h.button('Explore sample plan');
  await page.waitForSelector('.wb-node');
  // The Query editor is mounted but hidden: its Schemas panel reads nothing yet.
  assert.equal(schemaReads(), 0, 'a hidden Schemas panel reads no schemas');
  for (const width of [1440, 720]) for (const theme of ['light', 'dark']) {
    await page.setViewport({ width, height: width === 1440 ? 900 : 600 });
    await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    for (const [view, label] of [['workbench', 'Visual Explain'], ['flamegraph', 'Flame graph'], ['tree', 'Execution tree'], ['bargraph', 'Bar chart']]) {
      if (width === 720) await page.select('.chart-phone-options select', view);
      else await h.button(label, '.chart-views ');
      await page.waitForSelector(`.plan-chart[data-view="${view}"]`);
      const m = await capture(`plan-${view}`, width, theme);
      if (!baseline && width === 720) {
        // The section tabs stay (Findings, Lessons and Operators need no
        // selection); the empty Measurements body gives way to one line.
        assert.ok(m.inspector.height <= 88, `empty inspector is the tabs and one line: ${m.inspector.height}`);
        const shown = await page.$eval('.plan-inspector', e => e.innerText);
        assert.match(shown, /Select an operator to see its details/);
        assert.match(shown, /Operators/);
        assert.equal(await page.$('.plan-inspector .plan-inspector-body:not([hidden])') && await page.$eval('.plan-inspector .plan-inspector-body', e => getComputedStyle(e).display), 'none');
        assert.ok(m.inspector.y + m.inspector.height <= 600 - 24, 'hint fits above footer');
      }
    }
    if (!baseline && width === 720) {
      // Stats is a page of tables: with nothing selected it still scrolls, so
      // its last table can be brought into view (never clipped by the chart layout).
      await page.select('.chart-phone-options select', 'stats');
      await page.waitForSelector('.plan-statistics-body section');
      const reach = await page.evaluate(() => {
        const last = [...document.querySelectorAll('.plan-statistics-body section')].pop();
        last.scrollIntoView({ block: 'end' });
        const r = last.getBoundingClientRect();
        return { bottom: r.bottom, height: innerHeight };
      });
      assert.ok(reach.bottom <= reach.height, `the last Stats table scrolls into view: ${JSON.stringify(reach)}`);
      await page.select('.chart-phone-options select', 'workbench');
    }
    await h.button('Compare', '.plans-bar ');
    await page.waitForSelector('.compare-empty');
    const sides = await page.$$eval('.compare-selectors select', es => es.map(e => e.value));
    if (!baseline) {
      assert.ok(sides[0]);
      assert.equal(sides[1], '', 'one plan leaves After unselected');
    }
    await capture('plan-compare', width, theme);
    if (!baseline) {
      await page.focus('.compare-empty button');
      await page.keyboard.press('Enter');
      await page.waitForSelector('.import-dialog');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('.import-dialog'));
    }
    await h.button('Explore', '.plans-bar ');
    await page.click('button[aria-label="Settings"]');
    await page.waitForSelector('.settings-popover');
    if (!baseline) assert.equal(await page.$('.settings-option-row .info-button'), null);
    await capture('settings', width, theme);
    await page.keyboard.press('Escape');
    await h.button('Query editor', '.main-nav ');
    await page.waitForSelector('.editor-toolbar');
    const m = await capture('query', width, theme);
    if (!baseline && width === 720) {
      // At 720 px the toolbar may wrap onto a second row (never a sideways
      // scroll), and Format SQL, Schemas, History and the shortcuts stay
      // visible, labelled buttons rather than moving into a menu.
      assert.equal(m.toolbar.overflow, 0, 'all tools fit without sideways scrolling');
      for (const name of ['Format SQL', 'Show the schema browser', 'Show the run history', 'Query editor shortcuts'])
        assert.ok(await page.$eval(`.editor-toolbar button[aria-label="${name}"]`, e => e.getClientRects().length > 0), `${name} is on screen`);
    }
    if (!await page.$eval('.schema-browser-slot', e => !e.hidden)) await page.click('button[aria-label="Show the schema browser"]');
    // The visible panel lists the schemas by itself (no Load schemas gate),
    // read once per connection: showing it again reuses the list. A baseline
    // build may still show the old gate.
    await page.waitForSelector(baseline ? '.schema-browser :is(.browser-gate, .tree-row.kind-schema)' : '.schema-browser .tree-row.kind-schema');
    if (!baseline) {
      assert.equal(await page.$('.schema-browser .browser-gate'), null, 'no Load schemas gate with a connection');
      assert.ok(await page.$('.browser-head button[aria-label="Reload schemas"]'), 'Reload is offered once the list is read');
      assert.equal(schemaReads(), 1, 'the schema list is read once and reused');
    }
    await capture('schemas', width, theme);
    await page.click('.browser-head button[aria-label="Hide the schema browser"]');
    await h.button('Plans', '.main-nav ');
  }
  // A selected operator still opens its full inspector at the minimum size.
  await page.select('.chart-phone-options select', 'workbench');
  await page.waitForSelector('.wb-node');
  await page.$eval('.wb-node', e => e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  await page.waitForSelector('.plan-inspector .operator-detail');
  if (!baseline) assert.ok(await page.$eval('.plan-inspector', e => e.getBoundingClientRect().height > 40));
  // Showing the panel again reuses the list it read by itself; only Reload
  // schemas reads it again.
  await h.button('Query editor', '.main-nav ');
  await page.click('button[aria-label="Show the schema browser"]');
  if (!baseline) {
    await page.waitForSelector('.schema-browser .tree-row.kind-schema');
    assert.equal(schemaReads(), 1, 'showing the panel again reads nothing');
    await page.click('.browser-head button[aria-label="Reload schemas"]');
    for (let i = 0; i < 100 && schemaReads() < 2; i++) await pause(20);
    assert.equal(schemaReads(), 2, 'Reload schemas reads the list again');
    await page.waitForSelector('.browser-head button[aria-label="Reload schemas"]');
  }
  if (!baseline) {
    await page.keyboard.press('Escape');
    await h.button('Plans', '.main-nav ');
    await h.button('Compare', '.plans-bar ');
    await page.click('.compare-empty button');
    const file = await page.$('input[aria-label="Choose JSON plan file"]');
    await file.uploadFile(new URL('../../../test/mysql-explain-complex-join.json', import.meta.url).pathname);
    await page.waitForFunction(() => !document.querySelector('.import-dialog'));
    await h.button('Compare', '.plans-bar ');
    await page.waitForSelector('.comparison-canvases');
    const sides = await page.$$eval('.compare-selectors select', es => es.map(e => e.value));
    assert.ok(sides.every(Boolean));
    assert.notEqual(sides[0], sides[1], 'importing a second plan selects the distinct After plan');
  }
  assert.deepEqual(h.errors, []);
  console.log(JSON.stringify(measures, null, 2));
  await fs.writeFile(path.join(shots, 'measurements.json'), JSON.stringify(measures, null, 2));
  console.log('plan/editor layout passed');
} finally { await h.close(); }
