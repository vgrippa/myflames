// Shared primitives in a real browser, without a workspace/database server.
// Run after reviewing kit-static.mjs: node test/kit-primitives.mjs.
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';
const here = path.dirname(fileURLToPath(import.meta.url));
const bundle = await build({ stdin: { resolveDir: path.join(here, '../src'), loader: 'tsx', contents: `
  import React, { useState } from 'react';
  import { createRoot } from 'react-dom/client';
  import { SplitButton, SplitPane, FormGrid, FormRow, PendingBar, Fields, DataTable,
    WindowStatusProvider, WindowStatusBar, useWindowStatus, Checkbox, IconButton } from './kit';
  function Status() {
    useWindowStatus('server:A', { engineVersion: 'MySQL 8.4.0', connectionState: 'Connected', transaction: 'Autocommit', refreshedAt: 1000 });
    useWindowStatus('server:B', { engineVersion: 'MariaDB 11.4', connectionState: 'Disconnected' });
    return <WindowStatusBar scope="server:A" />;
  }
  function Demo() {
    const [runs, setRuns] = useState(0), [pending, setPending] = useState(2), [disabled, setDisabled] = useState(false);
    const [density, setDensity] = useState<'default' | 'compact'>('default');
    const [selection, setSelection] = useState('0');
    return <WindowStatusProvider>
      <SplitButton label="Run" onClick={() => setRuns(runs + 1)} popoverLabel="Run options" primary disabled={disabled}>
        <button onClick={() => setDisabled(true)}>Disable run</button>
      </SplitButton><output id="runs">{runs}</output>
      <FormGrid><FormRow label="Name" htmlFor="name" error="Required"><input id="name" /></FormRow></FormGrid>
      <Fields rows={[["connection_id", 55], ["innodb_buffer_pool_size", "128M"]]} />
      <div style={{ height: 360, width: '100%' }}><SplitPane direction="vertical" initialSize={160} minSize={80} maxSize={240}
        storageKey="test.vertical" first={<input aria-label="First pane" />} second="Results" collapsible /></div>
      <PendingBar count={pending} onReview={() => setRuns(runs + 10)} onDiscard={() => setPending(0)} />
      <button id="density" onClick={() => setDensity(density === 'default' ? 'compact' : 'default')}>Toggle density</button>
      <DataTable label="Numbers" density={density} rows={Array.from({ length: 500 }, (_, i) => ({ id: String(i), value: i }))}
        rowKey={r => r.id} selection={{ key: selection, select: setSelection }} columns={[
          { key: 'id', label: 'Identifier', render: r => r.id },
          { key: 'value', label: 'Count', num: true, render: r => r.value, sort: r => r.value }
        ]} /><Status />
      <div id="busy-look">
        <Checkbox className="busy-check" checked={false} busy onChange={() => {}}>Busy switch</Checkbox>
        <Checkbox className="idle-check" checked={false} onChange={() => {}}>Idle switch</Checkbox>
        <IconButton icon="refresh" label="Busy refresh" className="is-busy" aria-disabled />
        <IconButton icon="refresh" label="Gated icon" aria-disabled />
        <button type="button" className="button is-busy" aria-disabled>Busy button</button>
        <button type="button" className="button" aria-disabled title="Denied">Gated button</button>
      </div>
    </WindowStatusProvider>;
  }
  createRoot(document.getElementById('root')).render(<Demo />);
` }, bundle: true, platform: 'browser', format: 'iife', write: false, jsx: 'automatic', loader: { '.css': 'empty' } });
const css = (await Promise.all(['style.css', 'kit.css'].map(file => readFile(path.join(here, '../src', file), 'utf8')))).join('\n');
const browser = await puppeteer.launch({ headless: 'shell', pipe: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  await page.setViewport({ width: 1000, height: 900 });
  // Supply a real origin through interception, so localStorage is available.
  await page.setRequestInterception(true);
  page.on('request', request => request.respond({ status: 200, contentType: 'text/html', body: `<style>${css}</style><div id="root"></div>` }));
  await page.goto('http://kit.test/');
  await page.addScriptTag({ content: bundle.outputFiles[0].text });
  await page.waitForSelector('[aria-label="Run options"]');
  await page.click('.split-button > button');
  assert.equal(await page.$eval('#runs', e => e.textContent), '1');
  await page.focus('[aria-label="Run options"]');
  await page.keyboard.press('Enter');
  await page.waitForSelector('.popover');
  await page.keyboard.press('Escape');
  assert.equal(await page.$eval('[aria-label="Run options"]', e => e.getAttribute('aria-expanded')), 'false');
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Run options');
  await page.click('[aria-label="Run options"]');
  await page.locator('.popover button').click();
  await page.waitForFunction(() => !document.querySelector('.popover'));
  assert.equal(await page.$$eval('.split-button button', els => els.every(e => e.disabled)), true);
  const handle = '.split-pane-handle';
  await page.focus(handle);
  await page.keyboard.press('ArrowDown');
  assert.equal(await page.$eval(handle, e => e.getAttribute('aria-valuenow')), '168');
  await page.keyboard.press('End');
  assert.equal(await page.$eval(handle, e => e.getAttribute('aria-valuenow')), '240');
  await page.keyboard.press('Enter');
  assert.equal(await page.$eval('.split-pane-first', e => e.hidden), true);
  await page.click('[aria-label="Expand first pane"]');
  assert.equal(await page.$eval('.split-pane-first', e => e.hidden), false);
  assert.equal(await page.evaluate(() => JSON.parse(localStorage.getItem('myflames.split.test.vertical')).size), 240);
  const handleBox = await page.$eval(handle, e => { const r = e.getBoundingClientRect(); return { x: r.x + 50, y: r.y + 4 }; });
  await page.mouse.move(handleBox.x, handleBox.y);
  await page.mouse.down();
  await page.mouse.move(handleBox.x, handleBox.y - 32);
  await page.mouse.up();
  assert.equal(await page.$eval(handle, e => e.getAttribute('aria-valuenow')), '208');
  assert.equal(await page.$eval('#name', e => e.getAttribute('aria-describedby')), 'name-error');
  assert.equal(await page.$$eval('.inspector-fields .is-long-label', els => els.length), 1);
  await page.locator('.pending-bar button ::-p-text(Review changes)').click();
  assert.equal(await page.$eval('#runs', e => e.textContent), '11');
  await page.locator('.pending-bar button ::-p-text(Discard)').click();
  assert.equal(await page.$('.pending-bar'), null);
  await page.click('#density');
  await page.focus('.data-table tbody tr[data-row-key="0"]');
  await page.keyboard.press('End');
  await page.waitForSelector('.data-table tbody tr[data-row-key="499"]');
  assert.equal(await page.$eval('.data-table-frame', e => e.dataset.density), 'compact');
  assert.equal(await page.evaluate(() => document.activeElement?.dataset.rowKey), '499');
  // The trailing sortable header remains operable next to the Columns menu.
  await page.click('.data-table th[data-col="value"] .sort-button');
  assert.equal(await page.$eval('.data-table th[data-col="value"]', e => e.getAttribute('aria-sort')), 'descending');
  await page.click('.table-columns-button');
  await page.waitForSelector('[role="dialog"][aria-label="Columns of Numbers"]');
  await page.keyboard.press('Escape');
  assert.equal(await page.$('[role="dialog"][aria-label="Columns of Numbers"]'), null);
  // The busy look (dimmed, still focusable) belongs to controls that say they
  // are busy (is-busy); a privilege-gated aria-disabled button or icon button
  // keeps its own look.
  const look = await page.evaluate(() => {
    const of = (selector) => { const s = getComputedStyle(document.querySelector(selector)); return [s.opacity, s.cursor]; };
    return {
      busyCheck: of('#busy-look .busy-check input'), busyCheckLabel: getComputedStyle(document.querySelector('#busy-look .busy-check')).cursor,
      busyCheckDisabled: document.querySelector('#busy-look .busy-check input').disabled,
      idleCheck: of('#busy-look .idle-check input'),
      busyIcon: of('#busy-look [aria-label="Busy refresh"]'), gatedIcon: of('#busy-look [aria-label="Gated icon"]'),
      busyButton: of('#busy-look .button.is-busy'), gatedButton: of('#busy-look .button[title="Denied"]'),
    };
  });
  assert.deepEqual(look.busyCheck, ['0.5', 'default'], 'a busy Checkbox is dimmed');
  assert.equal(look.busyCheckLabel, 'default');
  assert.equal(look.busyCheckDisabled, false, 'a busy Checkbox stays focusable');
  assert.equal(look.idleCheck[0], '1');
  assert.deepEqual(look.busyIcon, ['0.5', 'default'], 'a busy icon button is dimmed');
  assert.deepEqual(look.busyButton, ['0.5', 'default'], 'a busy button is dimmed');
  assert.deepEqual(look.gatedIcon, ['1', 'pointer'], 'an aria-disabled icon button without is-busy keeps its look');
  assert.deepEqual(look.gatedButton, ['1', 'pointer'], 'a privilege-gated button keeps its look');
  const status = await page.$eval('.window-statusbar', e => e.textContent);
  assert.match(status, /MySQL 8.4.0ConnectedAutocommit/);
  assert.doesNotMatch(status, /MariaDB|Disconnected/);
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.deepEqual(errors, []);
  console.log('Kit primitive browser checks passed.');
} finally { await browser.close(); }
