// Presentation regression coverage over real-server recordings. The harness
// owns a disposable workspace; no production connections are opened.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { load, open, pause } from './perf-harness.mjs';
const cluster = await load('cluster-ic-8.4-healthy.json');
const galera = await load('cluster-galera-11.8-healthy.json');
const replication = await load('replication-mysql-8.4-healthy.json');
const h = await open('/api/live/cluster', 'topology-appearance', {
  '/api/live/replication': { status: 200, body: replication },
  '/api/live/replication/skip-loop/status': { status: 200, body: { loops: [] } },
}, { fallback: { status: 200, body: cluster } });
const { page } = h;
const output = process.env.TOPOLOGY_SHOTS || '/tmp/myflames-topology-review';
await fs.mkdir(output, { recursive: true });
try {
  await page.evaluate(async () => {
    const token = (await (await fetch('/api/auth/session', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Myflames-Bootstrap': '1', 'X-Myflames-Token': localStorage.getItem('myflames.session-proof') || '' }, body: '{}' })).json()).token;
    await fetch('/api/profiles/save', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Myflames-Token': token }, body: JSON.stringify({ value: { name: 'Topology test', host: 'node-a.test', port: 3306, user: 'dba', database: '', ssl_mode: 'PREFERRED', ssl_ca: '' } }) });
  });
  await page.reload({ waitUntil: 'networkidle0' });
  for (const place of ['cluster', 'replication']) {
    await page.click(`.main-nav button[data-place="${place}"]`);
    const root = place === 'cluster' ? '.cluster-topology' : '.replication-topology-container';
    await page.waitForSelector(root, { visible: true });
    const mark = place === 'cluster' ? '.cluster-stem.is-active' : '.topology-link.is-running .topology-line';
    const pseudo = place === 'cluster' ? '::after' : '::before';
    const animation = () => page.$eval(`${root} ${mark}`, (e, p) => getComputedStyle(e, p).animationName, pseudo);
    assert.notEqual(await animation(), 'none', `${place}: healthy state animates`);
    await page.click(`${root} .topology-motion-toggle`);
    assert.equal(await animation(), 'none', `${place}: pause stops motion`);
    await page.click(`${root} .topology-motion-toggle`);
    await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
    assert.equal(await animation(), 'none', `${place}: OS preference wins`);
    await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
    await page.$eval(root, e => e.parentElement.classList.add('is-stale'));
    assert.equal(await animation(), 'none', `${place}: stale reports do not animate`);
    await page.$eval(root, e => e.parentElement.classList.remove('is-stale'));
    await page.click(`${root} .topology-presentation-controls button:first-child`);
    assert.equal(await page.$eval(root, e => e.dataset.depth), 'off');
    await page.click(`${root} .topology-presentation-controls button:first-child`);
    for (const scheme of ['light', 'dark']) {
      await page.evaluate(s => { localStorage.setItem('myflames.theme', s); document.documentElement.dataset.theme = s; }, scheme);
      for (const width of [1440, 720]) {
        await page.setViewport({ width, height: 1000 });
        await pause(250);
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), `${place} ${scheme} ${width}: no page overflow`);
        await page.screenshot({ path: path.join(output, `${place}-${scheme}-${width}.png`) });
      }
    }
    if (place === 'cluster') {
      h.state.fallback = { status: 200, body: galera };
      await h.refresh();
      await page.waitForFunction(() => document.querySelector('.cluster-topology')?.textContent.includes('In the component · not read here'));
      for (const scheme of ['light', 'dark']) {
        await page.evaluate(s => { localStorage.setItem('myflames.theme', s); document.documentElement.dataset.theme = s; }, scheme);
        for (const width of [1440, 720]) {
          await page.setViewport({ width, height: 1000 });
          await pause(250);
          const labels = await page.$$eval('.cluster-card > .cluster-mark', marks => marks.map(mark => {
            const card = mark.closest('.cluster-card');
            const bounds = card.getBoundingClientRect();
            const range = document.createRange();
            range.selectNodeContents(mark);
            return { text: mark.textContent, fits: [...range.getClientRects()].every(rect => rect.left >= bounds.left && rect.right <= bounds.right),
              noOverflow: card.scrollWidth <= card.clientWidth };
          }));
          assert.equal(labels.filter(label => label.text.includes('In the component · not read here')).length, 2);
          assert.ok(labels.every(label => label.fits && label.noOverflow), `Galera status stays inside its own card at ${width}: ${JSON.stringify(labels)}`);
          await page.screenshot({ path: path.join(output, `cluster-galera-${scheme}-${width}.png`) });
        }
      }
    }
    await page.setViewport({ width: 1440, height: 1000 });
  }
  assert.deepEqual(h.errors, []);
  console.log(`topology appearance: ok; screenshots: ${output}`);
} finally { await h.close(); }
