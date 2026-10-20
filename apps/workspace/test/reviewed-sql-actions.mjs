// Server-generated SQL actions use an isolated editor session and mocked writes.
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
import { load, open, pause, privilegeAnswer } from './perf-harness.mjs';

const tables = await load('tables-mysql-8.4.json');
const schemas = await load('schemas-mysql-8.4.json');
const instruments = await load('instruments-mysql-8.4.json');
const calls = [];
const phases = new Map();
let mode = 'success', phase = 'none', lastRequest, denied = false, closeDelay = 0;
const body = value => ({ status: 200, body: value });
const run = (state = 'complete', errorRow = false) => ({
  id: 'reviewed-run', state, error: null,
  counts: { statements: 1, run: 1, rows: 0, affected_rows: 2, warnings: 0, errors: 0 },
  results: errorRow ? [{ type: 'rows', columns: [{ name: 'Msg_type' }, { name: 'Msg_text' }],
    rows: [['error', 'Maintenance refused by server']] }] : [],
});
const extra = {
  '/api/live/schemas': body(schemas), '/api/live/instruments': body(instruments),
  '/api/editor/preview': request => {
    calls.push(['preview', request]);
    return body({ status: 'ready', statements: [], aggregate: {
      state: denied ? 'denied' : 'unknown', reason: denied ? 'Missing INSERT privilege.' : 'Authority is unverified.', missing: [], warning: '' } });
  },
  '/api/editor/run': (request, interception) => {
    calls.push(['run', request]); lastRequest = request;
    assert.equal(request.require_confirmation, true);
    if (request.decline) return body({ status: 'declined' });
    if (!request.confirmed) return body({ status: 'needs_confirmation', confirmation: {
      token: 'one-use-review', expires_in: 300, title: 'Review SQL', text: 'Confirm the exact SQL.',
      tone: 'danger', statements: [request.sql], count: 1, warnings: ['May wait for metadata locks.'],
      connection_type: 'production', connection: { user: 'operator', host: 'test', port: 3306 } } });
    assert.equal(request.confirm_token, 'one-use-review');
    phase = mode === 'running' ? 'running' : 'complete';
    phases.set(request.tab_id, phase);
    if (mode === 'lost') { void interception.abort('failed'); return null; }
    return body({ status: phase, run: run(phase, mode === 'row_error') });
  },
  '/api/editor/status': request => { calls.push(['status', request]); const state = phases.get(request.tab_id) || phase; return body({ status: state, run: run(state) }); },
  '/api/editor/close': request => { calls.push(['close', request]); return { ...body({ closed: true, session: null }), delay: closeDelay }; },
  '/api/editor/stop': request => { calls.push(['stop', request]); phase = 'complete'; phases.set(request.tab_id, phase); return body({ stopped: true }); },
};
const h = await open('/api/live/tables', 'reviewed-sql-actions', extra);
const { page, state } = h;
await fs.mkdir(h.shots, { recursive: true });
state.fallback = body(tables);
const view = '.server-view[data-view=tables]';
const optimize = `${view} [data-reviewed-sql="Optimize frag_fpt"]`;
const status = `${view} [data-reviewed-status]`;
const confirm = async () => page.click('dialog.confirm-sheet button.danger-solid');
const review = async () => {
  await page.click(optimize);
  await page.waitForSelector('dialog.confirm-sheet[open]');
};
const done = () => page.waitForFunction(selector => /completed|Cancelled|refused/.test(document.querySelector(selector)?.textContent || ''), {}, status);
try {
  // The view reads once when it opens on screen (no consent gate here), so there is no Load button to press.
  await h.server(); await h.view('tables');
  await page.waitForSelector(`${view} .fact-strip`);
  await page.click(`${view} .segmented button[data-value=free]`);
  await h.select(`${view} tr[data-table="mftab.frag_fpt"]`);
  await review();
  assert.equal(calls.filter(([kind, request]) => kind === 'run' && request.confirmed).length, 0);
  assert.match(await h.text('dialog.confirm-sheet'), /OPTIMIZE TABLE `mftab`\.`frag_fpt`/);
  // The reviewed text is exactly the statement the run request carries (no trailing semicolon is added).
  assert.equal(lastRequest.sql, 'OPTIMIZE TABLE `mftab`.`frag_fpt`');
  assert.match(await h.text('dialog.confirm-sheet'), /test:3306/);
  await page.keyboard.press('Escape'); await done();
  assert.equal(calls.filter(([kind, request]) => kind === 'run' && request.decline).length, 1);
  const readsBefore = state.seen.length;
  await review(); await confirm(); await done();
  await page.waitForFunction((selector) => document.querySelector(selector)?.getAttribute('aria-disabled') === 'false', {}, optimize);
  assert.ok(state.seen.length > readsBefore, 'successful action refreshes report');
  assert.ok(calls.some(([kind]) => kind === 'close'), 'dedicated completed session closes');

  mode = 'row_error'; await review(); await confirm(); await done();
  assert.match(await h.text(status), /Maintenance refused by server/);
  assert.doesNotMatch(await h.text(status), /completed/);

  mode = 'lost'; const beforeLost = calls.filter(([kind, request]) => kind === 'run' && request.confirmed).length;
  await review(); await confirm(); await done();
  assert.equal(calls.filter(([kind, request]) => kind === 'run' && request.confirmed).length, beforeLost + 1, 'lost reply never resends execution');
  assert.ok(calls.some(([kind]) => kind === 'status'));

  mode = 'running'; await review(); await confirm();
  await page.waitForFunction(selector => /is running/.test(document.querySelector(selector)?.textContent || ''), {}, status);
  const owner = lastRequest.connection;
  await h.select(`${view} tr[data-table="mftab.ae_t"]`);
  assert.match(await h.text(status), /is running/, 'changing inspector preserves runner');
  // Instruments reads on open; no Load button.
  await h.view('instruments');
  const instrumentView = '.server-view[data-view=instruments]';
  await page.waitForSelector('#instr-ps');
  await page.select(`${instrumentView} select[aria-label=Preset]`, 'waits');
  await page.click(`${instrumentView} .instr-show-statements`);
  mode = 'success';
  const instrumentReads = state.other.filter(item => item.path === '/api/live/instruments').length;
  await page.click(`${instrumentView} [data-reviewed-sql="Apply Wait analysis"]`);
  await page.waitForSelector('dialog.confirm-sheet[open]');
  assert.ok((await h.text('dialog.confirm-sheet')).includes(instruments.presets.find(item => item.key === 'waits').runtime));
  await confirm();
  await page.waitForFunction(selector => /completed/.test(document.querySelector(selector)?.textContent || ''), {}, `${instrumentView} [data-reviewed-status]`);
  await page.waitForFunction(selector => document.querySelector(selector)?.getAttribute('aria-disabled') === 'false', {}, `${instrumentView} [data-reviewed-sql="Apply Wait analysis"]`);
  assert.ok(state.other.filter(item => item.path === '/api/live/instruments').length > instrumentReads);
  phase = 'running';
  await h.view('tables');
  closeDelay = 1000;
  await h.button('Stop', `${status} `); await done();
  assert.deepEqual(calls.find(([kind]) => kind === 'stop')[1].connection, owner);

  await h.select(`${view} tr[data-table="mftab.frag_fpt"]`);
  mode = 'running'; await review(); await confirm();
  const newTab = lastRequest.tab_id;
  await pause(300);
  assert.ok(calls.some(([kind, request]) => kind === 'status' && request.tab_id === newTab), 'new run polls while old session close is pending');
  closeDelay = 0;
  await h.button('Stop', `${status} `); await done();
  denied = true;
  const beforeDenied = calls.filter(([kind]) => kind === 'run').length;
  await page.click(optimize);
  await page.waitForFunction(selector => /Missing INSERT/.test(document.querySelector(selector)?.textContent || ''), {}, status);
  assert.equal(calls.filter(([kind]) => kind === 'run').length, beforeDenied);
  denied = false;
  await h.setPrivileges({ optimize_table: privilegeAnswer('denied', 'INSERT') });
  assert.equal(await page.$eval(optimize, node => node.getAttribute('aria-disabled')), 'true');
  await h.setPrivileges({ optimize_table: privilegeAnswer('unknown'), instruments_presets: privilegeAnswer('unknown') });

  mode = 'success'; await review();
  await page.evaluate(() => { const active = document.querySelector('.server-view[data-view=tables]'); active.parentElement.hidden = true; });
  await page.waitForFunction(() => !document.querySelector('dialog.confirm-sheet[open]'));
  await page.evaluate(() => { document.querySelector('.server-view[data-view=tables]').parentElement.hidden = false; });
  assert.ok(calls.filter(([kind, request]) => kind === 'run' && request.decline).length >= 2, 'hidden review is declined');

  for (const theme of ['light', 'dark']) {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
    await page.setViewport({ width: 1440, height: 950 });
    await review();
    for (const width of [1440, 390]) {
      await page.setViewport({ width, height: 950 });
      await pause(100);
      await page.screenshot({ path: path.join(h.shots, `reviewed-actions-${theme}-${width}.png`) });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    }
    await page.keyboard.press('Escape'); await done();
  }
  console.log('Reviewed SQL actions: confirmation, decline, privileges, messages, recovery, persistence and responsive screenshots passed.');
} finally { await h.close(); }
