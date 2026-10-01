// Exercise the shared renderer bridge inside the same opaque sandbox as the UI.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-exploration-'));
execFileSync(process.env.PYTHON || 'python3', ['-c', `
import sys
from pathlib import Path
from myflames.exploration import VIEWS, render_exploration
from myflames.parser import parse_explain
root = parse_explain(Path('test/fixtures/explain-044-join-3t-products-category-reviews.json').read_text())
for view in VIEWS:
    Path(sys.argv[1], view + '.html').write_text(render_exploration(root, view))
`, directory], { cwd: root });
const browser = await puppeteer.launch({ headless: 'shell' });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  for (const view of ['diagram', 'workbench', 'tree', 'bargraph', 'treemap', 'flamegraph']) {
    const html = await fs.readFile(path.join(directory, view + '.html'), 'utf8');
    await page.setContent('<script>window.selection=[];window.addEventListener("message",e=>window.selection.push(e.data))</script><iframe sandbox="allow-scripts" style="width:1200px;height:1500px"></iframe>');
    await page.$eval('iframe', (element, html) => element.srcdoc = html, html);
    const frame = await (await page.$('iframe')).contentFrame();
    await frame.waitForSelector('.metric-row[aria-pressed]');
    const ids = await frame.$$eval('.metric-row', nodes => nodes.map(node => node.dataset.nodeId));
    assert.equal(ids.length, 8);
    await frame.click('.metric-row:last-child');
    await page.waitForFunction(id => window.selection.some(event => event.type === 'myflames-node' && event.node_id === id), {}, ids.at(-1));
    await page.$eval('iframe', (element, id) => element.contentWindow.postMessage({ type: 'myflames-select', node_id: id }, '*'), ids[0]);
    await frame.waitForFunction(id => document.querySelector('.metric-row[data-node-id="' + id + '"]').classList.contains('is-selected'), {}, ids[0]);
    const native = (await frame.$$('.chart [data-node-id]')).at(-1);
    assert.ok(native);
    const nativeId = await native.evaluate(node => node.dataset.nodeId);
    await page.evaluate(() => window.selection = []);
    await native.click();
    await page.waitForFunction(id => window.selection.some(event => event.node_id === id), {}, nativeId);
    // Keyboard selection must be available even for zero-width chart marks.
    await frame.focus('.metric-row:last-child');
    await page.keyboard.press('Enter');
    await frame.waitForFunction(id => document.querySelector('.metric-row[data-node-id="' + id + '"]').classList.contains('is-selected'), {}, ids.at(-1));
    console.log(view + ': native and metric selection, parent synchronization, keyboard passed');
  }
  assert.deepEqual(errors, []);
} finally {
  await browser.close();
  await fs.rm(directory, { recursive: true, force: true });
}
