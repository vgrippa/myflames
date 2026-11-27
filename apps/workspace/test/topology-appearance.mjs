import { setUiPreferences } from "./perf-harness.mjs";
// Presentation regression coverage over real-server recordings. The harness
// owns a disposable workspace; no production connections are opened.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { load, open, pause } from './perf-harness.mjs';
const cluster = await load('cluster-ic-8.4-healthy.json');
const clusterOffline = await load('cluster-ic-8.4-member-offline.json');
const donor = await load('cluster-pxc-8.4-donor.json');
const clusterSet = await load('cluster-ic-8.4-clusterset-primary.json');
const galera = await load('cluster-galera-11.8-healthy.json');
const replication = await load('replication-mysql-8.4-healthy.json');
const proxysql = await load('proxysql-lab.json');
const readyset = await load('readyset-lab.json');
const h = await open('/api/live/cluster', 'topology-appearance', {
  '/api/live/replication': { status: 200, body: replication },
  '/api/live/replication/skip-loop/status': { status: 200, body: { loops: [] } },
  '/api/live/proxysql': { status: 200, body: proxysql },
  '/api/live/readyset': { status: 200, body: readyset },
}, { fallback: { status: 200, body: cluster } });
const { page } = h;
const output = process.env.TOPOLOGY_SHOTS || '/tmp/myflames-topology-review';
await fs.mkdir(output, { recursive: true });

// Resolve expected colors and rings in the browser, including color-mix()
// and the translucent default theme tokens, rather than comparing states to
// each other (two wrong colors can still be different).
const resolved = (property, value) => page.evaluate((property, value) => {
  const probe = document.createElement('span');
  probe.style[property] = value;
  document.body.appendChild(probe);
  const result = getComputedStyle(probe)[property];
  probe.remove();
  return result;
}, property, value);

async function assertCardInteraction(selector, { selected = false, watch = false }, label) {
  const accent = await resolved('color', 'var(--accent)');
  const warn = await resolved('color', 'var(--warn)');
  const ring = await resolved('boxShadow', 'inset 0 0 0 1px var(--accent)');
  const watchEdge = await resolved('boxShadow', 'inset 3px 0 0 var(--warn)');
  const snapshot = () => page.$eval(selector, e => {
    const style = getComputedStyle(e);
    return { background: style.backgroundImage, shadow: style.boxShadow, border: style.borderTopColor,
      left: style.borderLeftColor, width: style.borderLeftWidth };
  });
  await page.mouse.move(0, 0);
  await page.$eval(selector, e => e.blur());
  await pause(250);
  const before = await snapshot();
  if (selected) {
    assert.equal(before.border, accent, `${label}: selected accent border`);
    assert.ok(before.shadow.includes(ring), `${label}: selected inset accent ring`);
  }
  const check = async action => {
    const after = await snapshot();
    if (selected) {
      assert.equal(after.border, accent, `${label} ${action}: accent border remains`);
      assert.equal(after.background, before.background, `${label} ${action}: accent face remains`);
      assert.ok(after.shadow.includes(ring), `${label} ${action}: accent ring remains`);
    }
    if (watch) {
      assert.equal(after.left, warn, `${label} ${action}: Watch edge remains`);
      assert.equal(after.width, '3px', `${label} ${action}: Watch edge keeps its width`);
      assert.ok(after.shadow.includes(watchEdge), `${label} ${action}: inset Watch edge remains`);
    }
  };
  await check('at rest');
  await page.hover(selector);
  await pause(250);
  assert.equal(await page.$eval(selector, e => e.matches(':hover')), true, `${label}: card is hovered`);
  await check('hover');
  await page.mouse.move(0, 0);
  // Establish keyboard modality before programmatic focus so :focus-visible,
  // not just :focus, exercises the production selector.
  await page.keyboard.press('Tab');
  await page.focus(selector);
  await pause(250);
  assert.equal(await page.$eval(selector, e => e.matches(':focus-visible')), true, `${label}: keyboard focus is visible`);
  await check('focus');
  await page.$eval(selector, e => e.blur());
  await page.keyboard.press('Escape'); // Close the inspector opened by selection before the layout sweep.
}

// Selecting a Cluster member opens its page and hides the topology; Escape
// returns to the topology with the card still selected.
async function selectCard(selector) {
  await page.click(selector);
  await pause(250);
  await page.keyboard.press('Escape');
  await page.waitForFunction(s => { const e = document.querySelector(s); return !!e && e.getBoundingClientRect().width > 0; }, {}, '.cluster-topology');
  await pause(200);
}

async function assertStaleHasNoGlow(root, connectors, label) {
  const shadows = () => page.$$eval(`${root} :is(${connectors})`, elements => elements.map(e => getComputedStyle(e).boxShadow));
  const before = await shadows();
  assert.ok(before.length > 0 && before.every(shadow => shadow !== 'none'), `${label}: current connectors have a halo`);
  for (const state of ['is-stale', 'is-other-connection']) {
    await page.$eval(root, (e, state) => e.parentElement.classList.add(state), state);
    try {
      assert.deepEqual(await shadows(), before.map(() => 'none'), `${label} ${state}: every connector halo is removed`);
    } finally {
      await page.$eval(root, (e, state) => e.parentElement.classList.remove(state), state);
    }
    assert.deepEqual(await shadows(), before, `${label}: current report halos return`);
  }
}

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
    assert.equal(await page.$(`${root} .topology-presentation-controls`), null, `${place}: no presentation controls`);
    await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
    assert.equal(await animation(), 'none', `${place}: OS preference wins`);
    await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
    await page.$eval(root, e => e.parentElement.classList.add('is-stale'));
    assert.equal(await animation(), 'none', `${place}: stale reports do not animate`);
    await page.$eval(root, e => e.parentElement.classList.remove('is-stale'));
    if (place === 'cluster') {
      // The stage presentation is the only look: no look switch survives on
      // the root element, on any element, or in any stylesheet rule, and the
      // stage floor renders without one. The name is built in parts so this
      // suite does not itself match a search for the removed switch.
      const lookAttr = ['data-topology', 'look'].join('-');
      assert.equal(await page.evaluate(name => document.documentElement.hasAttribute(name), lookAttr), false, 'cluster: no look switch on the root element');
      assert.equal(await page.$(`[${lookAttr}]`), null, 'cluster: no element carries the look switch');
      assert.equal(await page.evaluate(name => {
        const scan = (rules) => [...rules].reduce((n, r) => n + (r.selectorText?.includes(name) ? 1 : 0) + (r.cssRules ? scan(r.cssRules) : 0), 0);
        return [...document.styleSheets].reduce((n, sheet) => { try { return n + scan(sheet.cssRules); } catch { return n; } }, 0);
      }, lookAttr), 0, 'cluster: no stylesheet rule references the look switch');
      assert.match(await page.$eval(root, e => getComputedStyle(e).backgroundImage), /radial-gradient/, 'cluster: the stage floor renders without a look switch');
      // The group rail takes the accent only when every member is healthy:
      // compare computed colors so the check holds in every theme.
      // Under 760 px the group frame stands in for the rail and follows the same rule.
      const assertFrame = async (healthy, label) => {
        await page.setViewport({ width: 720, height: 1000 });
        await pause(250);
        try {
          const expected = await resolved('color', healthy ? 'color-mix(in srgb, var(--accent) 52%, var(--line-strong))' : 'var(--line-strong)');
          const { color, width } = await page.$eval('.cluster-group', e => ({ color: getComputedStyle(e).borderTopColor, width: getComputedStyle(e).borderTopWidth }));
          assert.equal(width, '2px', `${label}: narrow frame is rendered`);
          assert.equal(color, expected, `${label}: group frame color`);
          if (healthy) await assertStaleHasNoGlow(root, '.cluster-group', `${label} narrow rail`);
        } finally {
          await page.setViewport({ width: 1440, height: 1000 });
          await pause(250);
        }
      };
      for (const scheme of ['light', 'dark']) {
        await setUiPreferences(page, { theme: scheme });
        const accent = await resolved('color', 'var(--accent)');
        const bus = await page.$eval('.cluster-bus-line', e => getComputedStyle(e).borderTopColor);
        assert.equal(bus, accent, `cluster ${scheme}: every member healthy lights the group rail with the accent`);
        const stemAccent = await resolved('color', 'color-mix(in srgb, var(--accent) 82%, var(--accent-ink) 18%)');
        const stems = await page.$$eval('.cluster-stem.is-active', elements => elements.map(e => getComputedStyle(e).borderLeftColor));
        assert.equal(stems.length, 3, 'healthy recording has three active stems');
        assert.deepEqual(stems, stems.map(() => stemAccent), `cluster ${scheme}: active stems take the accent mix`);
        await assertStaleHasNoGlow(root, '.cluster-stem, .cluster-bus-line', `cluster ${scheme}`);
        await selectCard('.cluster-group button.cluster-card:not(.is-detached)');
        await assertCardInteraction('.cluster-card.is-selected', { selected: true }, `cluster ${scheme} selected`);
        await assertFrame(true, `cluster ${scheme} healthy 720`);

        // Prove the frame assertion rejects the review's counterexample: a
        // danger-colored healthy frame would pass the old inequality check.
        const previousStyle = await page.$eval('.cluster-group', e => e.getAttribute('style'));
        await page.$eval('.cluster-group', e => e.style.setProperty('border-color', 'var(--danger)', 'important'));
        try {
          await assert.rejects(() => assertFrame(true, `cluster ${scheme} mutated 720`), /group frame color/);
        } finally {
          await page.$eval('.cluster-group', (e, value) => value === null ? e.removeAttribute('style') : e.setAttribute('style', value), previousStyle);
        }
      }
      h.state.fallback = { status: 200, body: clusterOffline };
      await h.refresh();
      await page.waitForFunction(() => document.querySelector('.cluster-topology')?.textContent.includes('MISSING'));
      for (const scheme of ['light', 'dark']) {
        await setUiPreferences(page, { theme: scheme });
        const neutral = await resolved('color', 'color-mix(in srgb, var(--line-strong) 80%, var(--ink) 20%)');
        const bus = await page.$eval('.cluster-bus-line', e => getComputedStyle(e).borderTopColor);
        assert.equal(bus, neutral, `cluster ${scheme}: a member out of the group leaves the group rail neutral`);
        await assertFrame(false, `cluster ${scheme} member missing 720`);
      }

      h.state.fallback = { status: 200, body: donor };
      await h.refresh();
      const watchCard = '.cluster-card.tone-watch:not(.is-detached)';
      await page.waitForSelector(watchCard, { visible: true });
      for (const scheme of ['light', 'dark']) {
        await setUiPreferences(page, { theme: scheme });
        // Cover neutral stems too, including the attached PXC donor's stem.
        await assertStaleHasNoGlow(root, '.cluster-stem, .cluster-bus-line', `PXC donor ${scheme}`);
        await selectCard('.cluster-card.tone-ok');
        await assertCardInteraction(watchCard, { watch: true }, `PXC donor ${scheme} Watch`);
        await selectCard(watchCard);
        await assertCardInteraction(watchCard, { selected: true, watch: true }, `PXC donor ${scheme} selected Watch`);
      }

      h.state.fallback = { status: 200, body: clusterSet };
      await h.refresh();
      // The set level is ClusterSetMap's own root, outside .cluster-topology.
      const setRoot = '.clusterset-map';
      await page.waitForSelector(`${setRoot} .clusterset-link:not(.is-broken) .clusterset-arrow`);
      for (const scheme of ['light', 'dark']) {
        await setUiPreferences(page, { theme: scheme });
        await assertStaleHasNoGlow(setRoot, '.clusterset-link:not(.is-broken) .clusterset-arrow', `ClusterSet ${scheme}`);
        // A failed refresh disables motion on the root without adding an
        // .is-stale ancestor. Its intact arrows must also lose their halos.
        assert.equal(await page.$eval(setRoot, e => e.dataset.motion), 'on', `ClusterSet ${scheme}: current report enables motion`);
        await page.$eval(setRoot, e => { e.dataset.motion = 'off'; });
        try {
          const shadows = await page.$$eval(`${setRoot} .clusterset-link:not(.is-broken) .clusterset-arrow`, elements => elements.map(e => getComputedStyle(e).boxShadow));
          assert.ok(shadows.length > 0, `ClusterSet ${scheme}: motion-off map keeps its arrows`);
          assert.deepEqual(shadows, shadows.map(() => 'none'), `ClusterSet ${scheme}: motion-off map has no arrow halo`);
        } finally {
          await page.$eval(setRoot, e => { e.dataset.motion = 'on'; });
        }
      }
      h.state.fallback = { status: 200, body: cluster };
      await h.refresh();
      await page.waitForFunction(() => document.querySelectorAll('.cluster-stem.is-active').length === 3);
    }
    if (place === 'replication') {
      for (const scheme of ['light', 'dark']) {
        await setUiPreferences(page, { theme: scheme });
        await assertStaleHasNoGlow(root, '.topology-link.is-running .topology-line', `replication ${scheme}`);
        // A failed refresh disables motion on the root without adding an
        // .is-stale ancestor. Its running links must also lose their halos.
        assert.equal(await page.$eval(root, e => e.dataset.motion), 'on', `replication ${scheme}: current report enables motion`);
        await page.$eval(root, e => { e.dataset.motion = 'off'; });
        try {
          const shadows = await page.$$eval(`${root} .topology-link.is-running .topology-line`, elements => elements.map(e => getComputedStyle(e).boxShadow));
          assert.ok(shadows.length > 0, `replication ${scheme}: motion-off report keeps its connectors`);
          assert.deepEqual(shadows, shadows.map(() => 'none'), `replication ${scheme}: motion-off report has no connector glow`);
        } finally {
          await page.$eval(root, e => { e.dataset.motion = 'on'; });
        }
      }
    }
    for (const scheme of ['light', 'dark']) {
      await setUiPreferences(page, { theme: scheme });
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
        await setUiPreferences(page, { theme: scheme });
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
  for (const place of ['proxysql', 'readyset']) {
    await page.click(`.main-nav button[data-place="${place}"]`);
    const view = `.server-view[data-view="${place}"]`;
    if (place === 'proxysql') {
      await page.waitForSelector(`${view} .segmented button`, { visible: true });
      await page.evaluate((v) => [...document.querySelectorAll(`${v} .segmented button`)].find((b) => b.textContent.trim() === 'Topology').click(), view);
    }
    const root = `${view} .readyset-topology`;
    await page.waitForSelector(root, { visible: true });
    assert.equal(await page.$(`${root} .topology-presentation-controls`), null, `${place}: no presentation controls`);
    for (const scheme of ['light', 'dark']) {
      await setUiPreferences(page, { theme: scheme });
      for (const width of [1440, 720]) {
        await page.setViewport({ width, height: 1000 });
        await pause(250);
        assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth), `${place} ${scheme} ${width}: no page overflow`);
        await page.screenshot({ path: path.join(output, `${place}-${scheme}-${width}.png`) });
      }
    }
    await page.setViewport({ width: 1440, height: 1000 });
  }
  assert.deepEqual(h.errors, []);
  console.log(`topology appearance: ok; screenshots: ${output}`);
} finally { await h.close(); }
