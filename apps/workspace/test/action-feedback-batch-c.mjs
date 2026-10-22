// Plans, Maintenance, and the shared Cluster/ProxySQL review. Kept separate
// from the harness: every server action holds its reply for at least 2 s.
import assert from "node:assert/strict";
import fs from "node:fs/promises";

const load = async (name) => JSON.parse(await fs.readFile(new URL(`./fixtures/${name}.json`, import.meta.url), "utf8"));
const [upgrade, dump, restore, clone, running, cancelled, clusterPlan, clusterResult, proxyPlan, proxyResult] = await Promise.all([
  "upgrade-native-8.0.46-to-8.4.0", "lifecycle-plan-dump", "lifecycle-plan-restore", "lifecycle-plan-clone",
  "lifecycle-job-dump-running", "lifecycle-job-dump-cancelled", "cluster-plan-switchover", "cluster-result-switchover",
  "proxysql-plan-offline-soft", "proxysql-result-done",
].map(load));
const plans = { dump, restore, clone };
const MV = '.server-view[data-view="maintenance"]';
const SHEET = 'dialog.ops-confirm[open]';
const REVIEW = 'dialog.action-review[open]';
const PREPARE = '/api/lifecycle/prepare';
const START = '/api/lifecycle/start';
const CANCEL = '/api/lifecycle/cancel';
const TOOLS = '/api/lifecycle/tools';

export function batchC({ goPlace, goView, visible, named, click, confirmButton, waitText, waitNotBusy, pause, respond, answer, viewRefresh }) {
  const noSheet = (page, selector) => page.waitForFunction((sel) => !document.querySelector(sel), {}, selector);
  async function sample(page) {
    await goPlace(page, "plans");
    await click(page, "Explore sample plan");
    await waitText(page, '.plan-heading h1', /sample-plan\.json/);
  }
  async function tools(page) {
    await goView(page, "server", "maintenance");
    await click(page, "Detect tools", MV);
    await visible(page, `${MV} [data-row-key="tool:mysqlsh"]`);
  }
  async function field(page, text, value) {
    const handle = await page.evaluateHandle((label) => {
      for (const e of document.querySelectorAll('label')) {
        if (!e.closest('[hidden]') && e.getClientRects().length && e.textContent.trim().startsWith(label)) {
          const input = e.control || e.querySelector('input');
          if (input) return input;
        }
      }
      return null;
    }, text);
    const input = handle.asElement();
    assert.ok(input, `input for ${text}`);
    await input.click({ clickCount: 3 });
    await page.keyboard.press('Backspace');
    await input.type(value);
  }
  async function operationForm(page, op) {
    await tools(page);
    await click(page, op[0].toUpperCase() + op.slice(1), MV);
    if (op === 'clone') {
      await field(page, 'Donor host', 'donor.test');
      await field(page, 'Donor user', 'root');
    } else {
      const input = await visible(page, `${MV} .ops-folder-input input`);
      await input.type(`/tmp/action-feedback-${op}`);
    }
  }
  async function openStart(page, op) {
    await operationForm(page, op);
    await click(page, 'Run preflight…', MV);
    await visible(page, SHEET);
    if (op !== 'dump') await field(page, `Type ${plans[op].target} to confirm`, plans[op].target);
  }
  const mocks = (op) => ({
    [PREPARE]: () => ({ ...plans[op], review_token: `feedback-${op}` }),
    [START]: () => ({ ...running, id: `feedback-${op}`, operation: op }),
    '/api/lifecycle/status': () => ({ ...running, id: `feedback-${op}`, operation: op }),
  });
  const readError = async ({ page }) => {
    await waitText(page, `${MV} .view-status [role=alert]`, /Simulated failure/);
    await visible(page, `${MV} .view-status-retry`);
  };
  const stopRead = async ({ page }) => {
    await waitText(page, `${MV} .action-status .action-elapsed`, /^\d{2,} s so far$/);
    await click(page, 'Stop waiting', `${MV} .action-status`);
    await page.waitForFunction((sel) => !document.querySelector(`${sel} .action-status[data-phase=pending]`), {}, MV);
  };
  const checkNoResend = (ctx, path) => assert.equal(ctx.log.filter((r) => r.path === path).length, 1, 'closing and a late reply never resend the write');
  async function leaveStart({ page, ctx }, unknown = false) {
    await waitText(page, `${SHEET} .action-elapsed`, /^\d{2,} s so far$/);
    const declines = ctx.log.filter((r) => r.path === '/api/audit/declined').length;
    await click(page, 'Close', SHEET);
    await noSheet(page, SHEET);
    await waitText(page, `${MV} .ops-job .action-status`, /Whether it started is unknown/);
    assert.equal(ctx.log.filter((r) => r.path === '/api/audit/declined').length, declines, 'Close never declines a sent request');
    // Trying the preflight while Start still waits sends nothing.
    await (await named('Run preflight…', MV)(page)).evaluate((e) => e.click());
    assert.equal(ctx.log.filter((r) => r.path === PREPARE).length, 1);
    ctx.hold.clear();
    for (const h of ctx.held.splice(0)) {
      if (unknown) await respond(h.request, 503, { error: 'Start reply lost' });
      else await answer(ctx, h.path, h.body, h.request);
    }
    if (unknown) await waitText(page, `${MV} .view-status [role=alert]`, /Whether it started is unknown/);
    else await visible(page, `${MV} .ops-job[data-state=running]`);
    await noSheet(page, SHEET);
    checkNoResend(ctx, START);
  }
  async function reviewedSetup(page, place, plan) {
    await goPlace(page, place);
    if (place === 'cluster') {
      await click(page, 'Members');
      await (await visible(page, `tr[data-row-key="${plan.member_key}"] td`)).click();
      await click(page, 'Make primary…', '.view-inspector');
    } else {
      await (await visible(page, 'tr[data-row-key="rt|32|mf-ic-2|3306"] td')).click();
      await click(page, 'Set OFFLINE_SOFT…', '.view-inspector');
    }
    await visible(page, REVIEW);
    // A real plan token and its exact statements are retained by the hook.
    await waitText(page, `${REVIEW} .ops-pre`, new RegExp(place === 'cluster' ? 'group_replication_set_as_primary' : 'LOAD MYSQL SERVERS TO RUNTIME'));
  }
  function reviewed(place, plan, result, unknown = false) {
    const path = `/api/live/${place}/action`;
    return {
      name: `${place}-reviewed-confirm${unknown ? '-late-unknown' : ''}`, label: `${place} reviewed confirm`, paths: [path], delayMs: 2000,
      ...(unknown ? { modes: ['hang'] } : {}),
      mocks: {
        [`/api/live/${place}/plan`]: () => ({ ...plan, review_token: 'feedback-review' }), [path]: () => result,
        // Checking, rather than sending: a 400 here is a pre-send refusal.
        '/api/live/action/progress': () => ({ schema_version: 'action-progress-1.0', phase: 'checking', states: plan.statements.map(() => 'waiting'), status: null, elapsed_s: 1, waiting_s: 0, timeout_s: 60 }),
      },
      setup: async ({ page }) => reviewedSetup(page, place, plan), target: confirmButton(REVIEW),
      afterClick: async ({ page, ctx }) => {
        await waitText(page, `${REVIEW} .sheet-actions button[aria-busy=true]`, /^Sending…$/);
        const sent = ctx.log.find((r) => r.path === path).body;
        assert.equal(sent.review_token, 'feedback-review');
      },
      ok: async ({ page }) => { await waitText(page, `${REVIEW} .action-outcome[data-status=done]`, /Done/); },
      error: async ({ page }) => { await waitText(page, `${REVIEW} .action-outcome[data-status=refused]`, /Simulated failure/); },
      hang: async ({ page, ctx }) => {
        await waitText(page, `${REVIEW} .action-elapsed`, /^\d{2,} s so far$/);
        const declines = ctx.log.filter((r) => r.path === '/api/audit/declined').length;
        await click(page, 'Close', REVIEW);
        await noSheet(page, REVIEW);
        await waitText(page, '.reviewed-action-status', /nothing will be sent again/);
        assert.equal(ctx.log.filter((r) => r.path === '/api/audit/declined').length, declines);
        ctx.hold.clear();
        for (const h of ctx.held.splice(0)) {
          if (unknown) await respond(h.request, 503, { error: 'Send reply lost' });
          else await answer(ctx, h.path, h.body, h.request);
        }
        await visible(page, `.action-result[data-status=${unknown ? 'unknown' : 'done'}]`);
        await noSheet(page, REVIEW);
        checkNoResend(ctx, path);
      },
    };
  }
  return [
    {
      name: 'plans-sample', label: 'Explore sample plan', paths: ['/api/analyze'], delayMs: 2000,
      setup: async ({ page }) => goPlace(page, 'plans'), target: named('Explore sample plan'),
      ok: async ({ page }) => waitText(page, '.plan-heading h1', /sample-plan\.json/),
      error: async ({ page }) => { await waitText(page, '.plans-empty .action-status', /Simulated failure/); await named('Try again', '.plans-empty')(page); },
      hang: async ({ page }) => { await waitText(page, '.plans-empty .action-elapsed', /^\d{2,} s so far$/); await click(page, 'Stop waiting', '.plans-empty'); await waitNotBusy(page, '.plans-empty .action-button'); },
    },
    {
      name: 'plans-export-html', label: 'Export HTML report', paths: ['/api/report'], delayMs: 2000,
      setup: async ({ page }) => { await sample(page); await click(page, 'Export plan'); }, target: named('HTML report', '.export-menu-items'),
      afterClick: async ({ page }) => { await waitText(page, '.plan-exports > button', /Exporting…/); },
      ok: async ({ page }) => waitText(page, '.plan-export-status', /Saved sample-plan-report\.html\./),
      error: async ({ page }) => { await waitText(page, '.plan-export-status', /Simulated failure/); await named('Try again', '.plan-export-status')(page); },
      hang: async ({ page }) => { await waitText(page, '.plan-export-status .action-elapsed', /^\d{2,} s so far$/); await click(page, 'Stop waiting', '.plan-export-status'); await waitNotBusy(page, '.plan-exports > button'); },
    },
    {
      // JSON is a local, synchronous download: there is no server request to
      // hold or lose. Test immediate success and a thrown download failure.
      name: 'plans-export-json', label: 'Export JSON', paths: [], local: true, modes: ['ok', 'error'], double: false,
      setup: async ({ page, mode }) => {
        await sample(page); await click(page, 'Export plan');
        if (mode === 'error') await page.evaluate(() => { URL.createObjectURL = () => { throw Error('Local download failed'); }; });
      }, target: named('JSON', '.export-menu-items'),
      ok: async ({ page }) => waitText(page, '.plan-export-status', /Saved sample-plan-analysis\.json\./),
      error: async ({ page }) => { await waitText(page, '.plan-export-status', /Local download failed/); await named('Try again', '.plan-export-status')(page); },
    },
    {
      name: 'plans-save-investigation', label: 'Save investigation', paths: ['/api/investigations/save'], delayMs: 2000,
      setup: async ({ page }) => { await sample(page); await click(page, 'Save investigation'); }, target: named('Save', 'dialog.notebook-sheet[open]'),
      ok: async ({ page }) => { await noSheet(page, 'dialog.notebook-sheet[open]'); await waitText(page, '#main', /Investigation saved on this computer\./); },
      error: async ({ page }) => {
        await waitText(page, 'dialog.notebook-sheet .action-status', /Simulated failure/);
        const errors = await page.$$eval('.action-status-message, [role=alert]', (els) => els.filter((e) => e.getClientRects().length && /Simulated failure/.test(e.textContent)).length);
        assert.equal(errors, 1, 'save error appears once, in the sheet');
      },
      hang: async ({ page, ctx }) => {
        await waitText(page, 'dialog.notebook-sheet .action-elapsed', /^\d{2,} s so far$/);
        await click(page, 'Close', 'dialog.notebook-sheet');
        await noSheet(page, 'dialog.notebook-sheet[open]');
        await waitText(page, '#main .action-status[data-phase=pending]', /Saving the investigation/);
        ctx.hold.clear();
        for (const h of ctx.held.splice(0)) await answer(ctx, h.path, h.body, h.request);
        await waitText(page, '#main', /Investigation saved on this computer\./);
        checkNoResend(ctx, '/api/investigations/save');
      },
    },
    ...[false, true].map((prerequisite) => ({
      name: `maintenance-detect-${prerequisite ? 'prerequisite' : 'empty'}`, label: 'Detect tools', paths: [TOOLS], delayMs: 2000,
      setup: async ({ page }) => { await goView(page, 'server', 'maintenance'); if (prerequisite) await click(page, 'Dump', MV); }, target: named('Detect tools', MV),
      ok: async ({ page }) => { if (prerequisite) await named('Run preflight…', MV)(page); else await visible(page, `${MV} [data-row-key="tool:mysqlsh"]`); },
      error: readError, hang: stopRead,
    })),
    viewRefresh('maintenance', 'server', TOOLS, { setup: async ({ page }) => tools(page), delayMs: 2000 }),
    {
      name: 'maintenance-check-upgrade', label: 'Check upgrade', paths: ['/api/lifecycle/upgrade-check'], delayMs: 2000,
      mocks: { '/api/lifecycle/upgrade-check': () => upgrade },
      setup: async ({ page }) => { await tools(page); await click(page, 'Upgrade check', MV); await page.select(`${MV} select[aria-label="Target version"]`, 'custom'); await field(page, 'Exact version', '8.4.0'); },
      target: named('Check upgrade', MV), ok: async ({ page }) => waitText(page, `${MV} .summary-line`, /Partial check/), error: readError, hang: stopRead,
    },
    ...['dump', 'restore', 'clone'].flatMap((op) => [
      {
        name: `maintenance-${op}-preflight`, label: `${op} preflight`, paths: [PREPARE], mocks: mocks(op), delayMs: 2000,
        setup: async ({ page }) => operationForm(page, op), target: named('Run preflight…', MV),
        ok: async ({ page }) => { await waitText(page, `${SHEET} .ops-pre`, new RegExp(op === 'clone' ? 'CLONE' : op === 'dump' ? 'dump' : 'load|mysql')); },
        error: readError, hang: stopRead,
      },
      ...[false, true].map((unknown) => ({
        name: `maintenance-${op}-start${unknown ? '-late-unknown' : ''}`, label: `Start ${op}`, paths: [START], mocks: mocks(op), delayMs: 2000,
        ...(unknown ? { modes: ['hang'] } : {}),
        setup: async ({ page }) => openStart(page, op), target: confirmButton(SHEET),
        ok: async ({ page }) => { await noSheet(page, SHEET); await visible(page, `${MV} .ops-job[data-state=running]`); },
        error: async ({ page }) => { await noSheet(page, SHEET); await waitText(page, `${MV} .view-status [role=alert]`, /Simulated failure/); },
        hang: async (ctx) => leaveStart(ctx, unknown),
      })),
    ]),
    {
      name: 'maintenance-cancel', label: 'Cancel job', paths: [CANCEL], delayMs: 2000,
      mocks: { '/api/lifecycle/jobs': () => ({ jobs: [running] }), '/api/lifecycle/status': () => running, [CANCEL]: () => cancelled },
      setup: async ({ page }) => { await goView(page, 'server', 'maintenance'); await click(page, 'Dump', MV); }, target: named('Cancel', `${MV} .ops-job`),
      ok: async ({ page }) => visible(page, `${MV} .ops-job[data-state=cancelled]`),
      error: async ({ page }) => waitText(page, `${MV} .view-status [role=alert]`, /Simulated failure/),
      hang: async ({ page }) => {
        await waitText(page, `${MV} .ops-job .action-elapsed`, /^\d{2,} s so far$/);
        // No modal traps the user; a cancel reply is never treated as proof
        // the job stopped. Navigation remains available while it is pending.
        await goPlace(page, 'home'); await visible(page, '.home');
      },
    },
    viewRefresh('proxysql', 'proxysql', '/api/live/proxysql', { go: async (page) => { await goPlace(page, 'proxysql'); await visible(page, '.proxysql-view tbody tr'); }, delayMs: 2000 }),
    reviewed('cluster', clusterPlan, clusterResult), reviewed('cluster', clusterPlan, clusterResult, true),
    reviewed('proxysql', proxyPlan, proxyResult), reviewed('proxysql', proxyPlan, proxyResult, true),
  ];
}
