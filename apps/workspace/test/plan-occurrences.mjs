// Inspector-width occurrence rows, keyboard selection and full-path names.
// Real analyze endpoint; isolated workspace, no database connections.
// WORKSPACE_ASSETS may point at a Vite --outDir build for lane validation.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-occurrences-'));
const shots = process.env.PLANSED_SHOTS || directory;
await fs.mkdir(shots, { recursive: true });
const baseline = process.env.PLANSED_BASELINE === '1';
const proc = spawn(process.env.PYTHON || 'python3', ['-c',
  "import os,sys; from pathlib import Path; import myflames.ui as u; u.ASSET_DIR=Path(os.environ.get('WORKSPACE_ASSETS', str(u.ASSET_DIR))); s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
  path.join(directory, 'workspace.sqlite3')], { cwd: root,
  env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: 'vault' } });
let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => reject(new Error('server did not start: ' + output)), 10000);
    proc.stderr.on('data', chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    proc.on('error', reject);
  });
  browser = await puppeteer.launch({ headless: 'shell' });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: 'networkidle0' });
  async function button(label, scope = '') {
    const buttons = await page.$$(scope + 'button');
    for (const b of buttons) if (await b.evaluate((e, label) => e.textContent.trim() === label && !!e.getClientRects().length, label)) { await b.click(); return; }
    throw new Error('missing button ' + label);
  }
  await button('Explore sample plan');
  await page.waitForSelector('.wb-node');
  // A repeated operator with different self times and ancestor paths.
  await page.$eval('.plan-fold .wb-node[data-label*="Nested loop"]', e => e.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  await button('All occurrences', '.plan-inspector ');
  await page.waitForSelector('section[aria-label="All occurrences"]');
  if (!baseline) {
    const rows = await page.$$eval('.occurrence-row', es => es.map(e => ({ id: e.dataset.nodeId, text: e.textContent, path: e.title, name: e.getAttribute('aria-label') })));
    assert.equal(rows.length, 2);
    assert.ok(rows.every(r => r.name.includes(r.path)), 'full ancestor path is in the accessible name');
    assert.match(rows[0].text, /self · .*% · .*rows × .*loops?/);
    assert.equal(await page.$('section[aria-label="All occurrences"] .data-table'), null);
    await page.focus('.occurrence-row:first-child');
    await page.keyboard.press('Enter');
    const selected = () => page.$eval('.plan-fold .wb-node.selected', e => e.dataset.nodeId);
    assert.equal(await selected(), rows[0].id);
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.$eval('.occurrence-row:last-child', e => document.activeElement === e), true);
    assert.equal(await selected(), rows[0].id, 'arrows move focus without selecting');
    await page.keyboard.press('Enter');
    assert.equal(await selected(), rows[1].id, 'Enter selects the actual chart operator');
    await page.click('.occurrence-row:first-child');
    assert.equal(await selected(), rows[0].id, 'click selects the chart operator');
  }
  for (const width of [1440, 720]) for (const theme of ['light', 'dark']) {
    await page.setViewport({ width, height: width === 1440 ? 900 : 600 });
    await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
    // Narrow Plans keeps its inspector below the chart, in its own scroller.
    await page.$eval('section[aria-label="All occurrences"]', e => e.scrollIntoView({ block: 'center' }));
    if (!baseline) await page.$eval('.occurrence-list', e => e.scrollIntoView({ block: 'nearest' }));
    await new Promise(r => setTimeout(r, 120));
    const measurement = await page.$eval('.plan-inspector', e => {
      const r = e.getBoundingClientRect();
      return { width: r.width, horizontalOverflow: e.scrollWidth - e.clientWidth,
        visibleRows: [...e.querySelectorAll('.occurrence-row, .data-table tbody tr')].filter(row => {
          const b = row.getBoundingClientRect(); return b.top >= Math.max(r.top, 0) && b.bottom <= Math.min(r.bottom, innerHeight);
        }).length };
    });
    console.log(JSON.stringify({ width, theme, ...measurement }));
    if (!baseline) {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      assert.equal(measurement.horizontalOverflow, 0, 'inspector never scrolls horizontally');
      assert.equal(measurement.visibleRows, 2);
      assert.equal(await page.$eval('.occurrence-list', e => e.scrollWidth - e.clientWidth), 0);
    }
    await page.screenshot({ path: path.join(shots, `plans-occurrences-${width}-${theme}.png`) });
  }
  if (!baseline) {
    await button('Back to measurements', '.plan-inspector ');
    assert.equal(await page.$('section[aria-label="All occurrences"]'), null);
  }
  assert.deepEqual(errors, []);
  console.log('plan occurrences passed');
} finally {
  await browser?.close();
  proc.kill();
  await new Promise(resolve => proc.exitCode !== null ? resolve() : proc.once('exit', resolve));
  await fs.rm(directory, { recursive: true, force: true });
}
