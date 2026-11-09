// Source-grounded synthetic reports. Real SQL and screenshots still need the lab.
import assert from "node:assert/strict";
import path from "node:path";
import { load, open } from "./perf-harness.mjs";

const [ok, watch, off, denied, fallback, hostile, mariadbOn, top, schemas] = await Promise.all([
  "memory-ok.json", "memory-watch.json", "memory-not-collected.json", "memory-denied.json",
  "memory-mariadb-fallback.json", "memory-hostile.json", "memory-mariadb-on.json", "top-mysql-8.4-lab.json",
  "schemas-mysql-8.4.json",
].map(load));
const endpoint = "/api/live/memory";
// Workload opens on Top statements, which reads its statements and the database list.
const h = await open(endpoint, "memory", { "/api/live/top": { status: 200, body: top }, "/api/live/schemas": { status: 200, body: schemas } },
  { list: "docked", fallback: { status: 200, body: ok } });
const { page, state } = h;
const view = '.server-view[data-view="memory"]';
const diagnostics = [], failures = [], httpErrors = [];
page.on("console", m => { if (["error", "warning"].includes(m.type())) diagnostics.push(m.text()); });
page.on("requestfailed", r => failures.push(`${r.url()}: ${r.failure()?.errorText}`));
page.on("response", r => { if (r.status() >= 400) httpErrors.push(`${r.status()} ${r.url()}`); });
async function list() {
  if (await page.$(`${view} .detail-back`)) await page.click(`${view} .detail-back`);
  await page.keyboard.press("Escape");
}
async function group(value) {
  await list();
  await page.click(`${view} .segmented button[data-value=${value}]`);
}
async function refresh(report) {
  await list();
  state.queue.push({ status: 200, body: report });
  const reply = page.waitForResponse(r => r.url().endsWith(endpoint));
  await h.refresh();
  if (state.delay) {
    await page.waitForSelector(`${view} .capture-status`);
    assert.match(await h.text(`${view} .capture-status`), /Sampling memory for 5 s/);
  }
  const response = await reply;
  assert.equal(response.status(), 200);
  assert.equal((await response.json()).source, report.source);
  await h.idle(view);
}
try {
  await h.workload();
  await h.view("memory");
  await page.waitForSelector(`${view} [aria-label="Memory by category"] tbody tr[data-row-key]`);
  await h.idle(view);
  assert.equal(state.seen.length, 1, "reads once on open");
  assert.equal(state.seen[0].interval, 5);
  assert.equal(await page.$$eval(`${view} [data-action=refresh]`, nodes => nodes.length), 1);
  assert.match(await h.text(`${view} .summary-line`), /OK.*1\.4 GiB tracked/);
  assert.match(await page.$eval('#server-nav-memory', e => e.textContent + e.getAttribute('aria-label')), /Memory/);
  assert.deepEqual(await page.$$eval(`${view} .perf-window option`, ns => ns.map(n => n.value)), ["2", "5", "10", "15", "30", "60"]);
  await page.click(`${view} th[data-col=current] button`);
  assert.equal(await page.$eval(`${view} th[data-col=current]`, e => e.getAttribute("aria-sort")), "descending");
  await h.select(`${view} tr[data-row-key="category:buffer_pool"]`);
  assert.match(await h.inspector(), /What it is.*Why it matters.*Numbers.*Server context.*Source/s);
  assert.match(await h.inspector(), /Current.*1\.0 GiB/s);
  // The exact bytes are visible text beside the binary value, not only a tooltip.
  assert.equal(await page.$eval('.view-inspector .inspector-fields dd .memory-value', e => e.textContent), "1.0 GiB");
  assert.equal(await page.$eval('.view-inspector .inspector-fields dd .cell-muted', e => e.textContent), "1,073,741,824 B");
  assert.match(await h.inspector(), /400 of 400/);
  assert.match(await h.inspector(), /memory_summary_global_by_event_name/);
  await group("sessions");
  await page.waitForSelector(`${view} [aria-label="Memory by session"]`);
  await h.select(`${view} tr[data-row-key="session:42"]`);
  assert.match(await h.inspector(), /Thread ID.*52/s);
  await group("accounts");
  assert.match(await h.text(`${view} [aria-label="Memory by account"]`), /app@localhost/);
  await group("categories");
  await page.click(`${view} details summary`);
  await page.waitForSelector(`${view} [aria-label="Top memory events"]`);
  assert.equal(await page.$$eval(`${view} [aria-label="Top memory events"] tbody tr[data-row-key]`, ns => ns.length), 4);

  state.delay = 300;
  const previousReads = state.seen.length;
  await refresh(watch);
  state.delay = 0;
  await page.waitForFunction(v => /Watch/.test(document.querySelector(`${v} .summary-line`)?.textContent || ""), {}, view);
  assert.equal(state.seen.length, previousReads + 1);
  const rules = await h.notes();
  assert.match(rules, /Rule of thumb:.*25%.*1 GiB/s);
  for (const rule of [/Growth triggers/g, /25% rule/g, /1 GiB session rule/g])
    assert.equal((rules.match(rule) || []).length, 1, 'each general rule appears once in Notes');
  await list();
  assert.match(await h.text(`${view} tr[data-row-key="category:sort"] .status-pill`), /Watch/);
  assert.equal(await page.$(`${view} tr[data-row-key="category:buffer_pool"] .status-pill`), null);
  await h.select(`${view} tr[data-row-key="category:sort"]`);
  assert.match(await h.inspector(), /Sort exceeds this threshold/);
  assert.doesNotMatch(await h.inspector(), /Inspect this session|short allocation burst|Growth triggers|25% rule|1 GiB session rule/);
  await h.select(`${view} tr[data-row-key="category:buffer_pool"]`);
  assert.doesNotMatch(await h.inspector(), /Rules of thumb|Sort exceeds|Inspect this session/);
  await group("sessions");
  assert.match(await h.text(`${view} tr[data-row-key="session:42"] .status-pill`), /Watch/);
  await h.select(`${view} tr[data-row-key="session:42"]`);
  assert.match(await h.inspector(), /Inspect this session/);
  assert.doesNotMatch(await h.inspector(), /Sort exceeds|short allocation burst/);
  await group("categories");
  await page.waitForFunction(() => /Watch/i.test(document.querySelector('#server-nav-memory')?.textContent || document.querySelector('#server-nav-memory')?.getAttribute('aria-label') || ""));

  await refresh(off);
  await page.waitForSelector(`${view} .gate [data-instruments-row="group:memory"]`);
  assert.match(await h.text(`${view} .summary-line`), /Not judged/);
  assert.match(await h.text(`${view} .gate`), /Most memory instruments are off/);
  for (const [segment, label, row] of [
    ["sessions", "Memory by session", "session:42"],
    ["accounts", "Memory by account", `account:${JSON.stringify(["app", "localhost"])}`],
  ]) {
    await group(segment);
    assert.match(await h.text(`${view} [aria-label="${label}"]`), /Not collected/);
    assert.doesNotMatch(await h.text(`${view} [aria-label="${label}"]`), /0 B/);
    // Pass the key as an argument so quotes in an account key remain data.
    await page.evaluate((v, key) => [...document.querySelectorAll(`${v} [data-row-key]`)].find(e => e.dataset.rowKey === key)?.click(), view, row);
    await page.waitForSelector('.view-inspector .inspector-title');
    assert.match(await h.inspector(), /instruments are disabled; open Server > Instruments/);
  }
  await group("categories");
  await refresh(denied);
  await page.waitForFunction(v => /Not collected tracked/.test(document.querySelector(`${v} .summary-line`)?.textContent || ""), {}, view);
  assert.equal(await page.$(`${view} [role=alert]`), null, "privilege errors are notes");
  assert.match(await h.notes(), /1142/);

  await refresh(fallback);
  await page.waitForFunction(v => /MariaDB/.test(document.querySelector(`${v} .gate`)?.textContent || ""), {}, view);
  await page.waitForSelector(`${view} [aria-label="Memory by session"]`);
  assert.match(await h.text(`${view} .summary-line`), /sum of highs not collected/);
  assert.doesNotMatch(await h.text(`${view} .summary-line`), /Not collected sum of highs/);
  assert.match(await h.text(`${view} [aria-label="Memory by session"]`), /80\.0 MiB.*90\.0 MiB/s);
  await h.select(`${view} tr[data-row-key="session:42"]`);
  assert.match(await h.inspector(), /information_schema\.PROCESSLIST/);
  assert.match(await h.inspector(), /Session high/);
  await group("accounts");
  assert.match(await h.text(view), /Account memory not collected/);

  await refresh(hostile);
  await group("accounts");
  assert.match(await h.text(`${view} [aria-label="Memory by account"]`), /<img src=x onerror=alert\(1\)>/);
  assert.equal(await page.$(`${view} img[src=x]`), null);
  assert.ok(await page.$(`${view} td bdi`), "server text has bidi isolation");
  await group("categories");

  // MariaDB with Performance Schema on registers neither NET::buff nor
  // Filesort_buffer::sort_keys: Network is listed as not collected, Sort says
  // what it leaves out.
  await refresh(mariadbOn);
  assert.match(await h.text(`${view} tr[data-row-key="category:network"]`), /Network.*Not collected/s);
  await h.select(`${view} tr[data-row-key="category:sort"]`);
  assert.match(await h.inspector(), /does not instrument the sort buffer \(Filesort_buffer::sort_keys\)/);
  await h.select(`${view} tr[data-row-key="category:network"]`);
  assert.match(await h.inspector(), /does not instrument the network buffer \(NET::buff\)/);
  // A session that freed another thread's memory reads below 0, never as a negative byte count.
  await refresh({ ...ok, sessions: [{ ...ok.sessions[0], current_bytes: -5 * 2 ** 20 }] });
  await group("sessions");
  const belowZero = await h.text(`${view} [aria-label="Memory by session"]`);
  assert.match(belowZero, /Below 0 \(freed by another thread\)/);
  assert.doesNotMatch(belowZero, /-5\.0 MiB/);
  await group("categories");
  await refresh(ok);
  for (const width of [1440, 720]) for (const theme of ["light", "dark"]) {
    await list();
    await page.setViewport({ width, height: 900 });
    await page.evaluate(t => { document.documentElement.dataset.theme = t; }, theme);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(w => window.innerWidth === w && document.documentElement.scrollWidth <= w, {}, width);
    await page.screenshot({ path: path.join(h.shots, `memory-${width}-${theme}.png`), fullPage: true });
    // The fixed Category width leaves both trailing metrics in view at 720 px.
    await page.waitForFunction(v => {
      const table = document.querySelector(`${v} [aria-label="Memory by category"].data-table-wrap`);
      const growth = table?.querySelector('th[data-col="growth"]');
      return !!growth && growth.getBoundingClientRect().right <= table.getBoundingClientRect().right + 1;
    }, {}, view);
    const frame = `${view} .data-table-frame`;
    if (await page.$(frame)) {
      assert.ok(await page.$eval(frame, e => e.scrollWidth >= e.clientWidth), "table owns any horizontal overflow");
    }
    await h.select(`${view} tr[data-row-key="category:buffer_pool"]`);
    await page.waitForSelector('.view-inspector .inspector-title');
    assert.match(await h.inspector(), /Buffer pool setting/);
    await page.screenshot({ path: path.join(h.shots, `memory-inspector-${width}-${theme}.png`), fullPage: true });
  }
  await list();
  await page.select(`${view} .perf-window select`, "30");
  const saved = await h.savedSlow(s => s.kind === "memory" && s.memory_interval === 30);
  assert.equal(saved.state.mode, "workload");
  assert.equal(JSON.stringify(saved.state).includes("memory-1.0"), false);
  state.fallback = { status: 200, body: ok };
  const readsBeforeRestore = state.seen.length;
  await page.reload({ waitUntil: "networkidle0" });
  // The 720 px window restores the saved place, view and window as they were:
  // Memory pushed over the list of views (its Back button returns to the
  // list), read once on open with the saved 30 s window. Nothing is clicked
  // first, so navigation cannot repair a lost saved place or view.
  await page.waitForSelector('.main-nav button[data-place="workload"].active');
  await h.views();
  await h.onView("memory");
  assert.equal(await page.$eval(`${view} .server-back`, e => e.offsetParent !== null), true, "narrow: the view is pushed with Back");
  await page.waitForFunction(v => /tracked/.test(document.querySelector(`${v} .summary-line`)?.textContent || ""), {}, view);
  assert.equal(await page.$eval(`${view} .perf-window select`, e => e.value), "30");
  assert.equal(state.seen.length, readsBeforeRestore + 1, "reads once on restore");
  assert.equal(state.seen.at(-1).interval, 30);
  // From the pushed view a person goes Back to the list and chooses the view
  // again (the harness does the same); the loaded report is not read again.
  await h.workload();
  await h.view("memory");
  await page.waitForSelector(`${view} [aria-label="Memory by category"]`);
  assert.equal(state.seen.length, readsBeforeRestore + 1, "choosing the loaded view does not read");
  // Also cover restoration with the view directly visible in a wide window.
  // No place click or h.view() here: a lost saved selection must fail instead of being repaired.
  await page.setViewport({ width: 1440, height: 900 });
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector('.main-nav button[data-place="workload"].active');
  await h.views();
  await h.onView("memory");
  await page.waitForSelector(`${view} [aria-label="Memory by category"]`);
  assert.equal(await page.$eval(`${view} .perf-window select`, e => e.value), "30");
  assert.deepEqual(h.errors, []);
  assert.deepEqual(diagnostics, []);
  assert.deepEqual(failures, []);
  assert.deepEqual(httpErrors, []);
  console.log("memory: categories, sessions, accounts, coverage, fallback, hostile text, persistence and layouts passed");
} finally {
  await h.close();
}
