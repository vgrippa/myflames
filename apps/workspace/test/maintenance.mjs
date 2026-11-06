import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
// Maintenance view of the Server place (Setup): mocked lifecycle
// endpoints answered with reports the real backend computed (ops-fixtures.py:
// tool detection and plans from test_lifecycle's fake tools and replayed
// connections, the dump job running and cancelled, the upgrade checks from
// the live recordings in test/fixtures/lifecycle/), and the real Python server
// for the session, saved connections and the audit trail. Covers: automatic local
// tool detection, tools, the upgrade check with its partial check label, the
// dump preflight refused for a folder with files and the confirmation with the
// exact commands (Cancel and Escape recorded as declined), start, progress
// and Cancel, a lost Start reply recovered from the job list without a second
// Start, restore with the typed target, clone preflight failures explained,
// jobs greyed after a connection switch, session restore of the section and
// rediscovery of a running job after a reload, and 390 px.
import assert from "node:assert/strict";
import path from "node:path";
import { assertPrivilege as assertPrivilegeState, privilegeAnswer, load, open, pause } from "./perf-harness.mjs";


// Retain the shared semantic/focus checks, then check the touch-visible feedback.
async function assertPrivilege(page, selector, state, need) {
  await assertPrivilegeState(page, selector, state, need);
  const feedback = await page.$eval(selector, (button, state) => {
    const wrapper = button.closest('.visible-privilege');
    const note = wrapper
      ? wrapper.querySelector(state === 'unknown' ? '.privilege-status-note' : '.account-sr')
      : button.closest('.sheet-actions')?.querySelector('.privilege-confirm-note');
    const visible = (element) => {
      if (!element) return false;
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return rect.width > 1 && rect.height > 1 && style.clipPath === 'none' && style.visibility !== 'hidden';
    };
    return { visible: visible(note), outside: !!note && !button.contains(note), text: note?.textContent || '',
      inlineUnknown: [...button.querySelectorAll('.privilege-unverified')].some(visible) };
  }, state);
  assert.equal(feedback.inlineUnknown, false, 'status never appears inside the action button');
  if (state === 'unknown' || state === 'denied') {
    assert.equal(feedback.visible, state !== 'unknown' || !selector.includes('.ops-actions'), 'read-only preflight hides the unknown note; confirmation retains it');
    assert.equal(feedback.outside, true, 'feedback sits beside or below the button');
    if (state === 'unknown') assert.equal(feedback.text, 'Unverified');
    else if (need) assert.ok(feedback.text.includes(need), feedback.text);
  }
}

const tools = await load("lifecycle-tools-mysql.json");
const native = await load("upgrade-native-8.0.46-to-8.4.0.json");
const shell = await load("upgrade-shell-8.0.46-to-8.4.0.json");
const dumpPlan = await load("lifecycle-plan-dump.json");
dumpPlan.review_token = "review-dump";
const refused = await load("lifecycle-refused-dump.json");
const restorePlan = await load("lifecycle-plan-restore.json");
const clonePlan = await load("lifecycle-plan-clone.json");
const cloneFailed = await load("lifecycle-plan-clone-failed.json");
const running = await load("lifecycle-job-dump-running.json");
const cancelled = await load("lifecycle-job-dump-cancelled.json");
const done = await load("lifecycle-job-dump-done.json");
const schemas = await load("schemas-mysql-8.4.json");

const UPGRADE = "/api/lifecycle/upgrade-check";
const PREPARE = "/api/lifecycle/prepare";
const START = "/api/lifecycle/start";
const STATUS = "/api/lifecycle/status";
const CANCEL = "/api/lifecycle/cancel";
const JOBS = "/api/lifecycle/jobs";
const DECLINED = "/api/audit/declined";
const REVEAL = "/api/files/local/reveal";
/** What the mocked server answers next, per path. */
const next = { upgrade: native, prepare: [], start: [], reveal: [] };
/** The jobs myflames holds, by id, as the status endpoint would return them,
 * and the endpoint each belongs to (the jobs list answers by endpoint). */
const held = new Map();
const owners = new Map();
const endpoint = (c) => JSON.stringify([c?.profile_id || "", c?.host, c?.port, c?.user, c?.ssl_mode || "", c?.ssl_ca || ""]);
const listed = (c) => [...held.values()].filter((j) => owners.get(j.id) === endpoint(c));
const job = (base, id, extra = {}) => ({ ...structuredClone(base), id, ...extra });
// The list of views docked, as a person who docked it: this suite asserts
// the list's markers and keys (server-nav covers the floating default).
const h = await open("/api/lifecycle/tools", "maintenance", {
  "/api/files/local/list": body => ({ status: 200, body: { path: body.path || "/Users/dba", home: "/Users/dba", entries: [], transfers: false } }),
  [REVEAL]: body => next.reveal.shift() || { status: 200, body: { revealed: body.path }, delay: 100 },
  [UPGRADE]: () => ({ status: 200, body: next.upgrade, delay: 50 }),
  [PREPARE]: () => next.prepare.shift() || { status: 200, body: dumpPlan },
  [START]: (body) => {
    const reply = next.start.shift();
    if (reply?.job) {
      held.set(reply.job.id, reply.job);
      owners.set(reply.job.id, endpoint(body.connection));
    }
    return reply?.status ? reply : { status: 200, body: reply.job };
  },
  [STATUS]: (body) => (held.has(body.id) && owners.get(body.id) === endpoint(body.connection) ? { status: 200, body: held.get(body.id) } : { status: 400, body: { error: "No such job." } }),
  [CANCEL]: (body) => {
    if (!held.has(body.id) || owners.get(body.id) !== endpoint(body.connection)) return { status: 400, body: { error: "No such job." } };
    const after = job(cancelled, body.id, { operation: held.get(body.id)?.operation || "dump" });
    held.set(body.id, after);
    return { status: 200, body: after };
  },
  [JOBS]: (body) => ({ status: 200, body: { schema_version: "lifecycle-jobs-1.0", jobs: listed(body.connection) } }),
  "/api/live/schemas": { status: 200, body: schemas },
  [DECLINED]: (body, request) => {
    void request.continue();
    return null;
  },
}, { list: "docked" });
const { page, state, text, texts } = h;
const view = '.server-view[data-view="maintenance"]';
const sheet = "dialog.ops-confirm[open]";
const confirmButton = `${sheet} .sheet-actions button:last-of-type`;
const cancelButton = `${sheet} .sheet-actions button:first-child`;
const calls = (p) => state.other.filter((o) => o.path === p);
async function settle(p, n, message) {
  for (let i = 0; i < 100 && calls(p).length < n; i++) await pause(50);
  assert.equal(calls(p).length, n, message || `${p} called ${n} times`);
}
const api = (route, body) =>
  page.evaluate(async (r, b) => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch(r, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    return response.json();
  }, route, body);
const audit = () => api("/api/audit/list", {}).then((r) => r.records);
/** The audit records once at least *n* exist (a declined sheet is recorded in the background). */
async function recorded(n) {
  let records = [];
  for (let i = 0; i < 100 && records.length < n; i++) {
    records = await audit();
    if (records.length < n) await pause(50);
  }
  assert.ok(records.length >= n, `${n} audit records`);
  return records;
}
const section = async (label) => {
  if (await page.$(".detail-back")) await page.click(".detail-back");
  await page.locator(`${view} .segmented button ::-p-text(${label})`).click();
  await page.waitForFunction((v, l) => document.querySelector(`${v} .segmented [aria-pressed=true]`)?.textContent === l, {}, view, label);
};
const connect = async (name) => {
  const option = await connectionOption(page, name);
  await chooseConnection(page, option);
  await page.waitForFunction((n) => document.querySelector('.window-toolbar .connection-pill-name')?.textContent === n, {}, name);
};
async function field(label, value) {
  const handle = await page.waitForSelector(`${view} .ops-field label ::-p-text(${label}), ${view} label.ops-field ::-p-text(${label}), ${view} .form-row > label ::-p-text(${label})`);
  const input = await handle.evaluateHandle((span) => span.closest("label").control || span.closest("label").querySelector("input"));
  await input.click({ clickCount: 3 });
  await page.keyboard.press("Backspace");
  if (value) await input.type(value);
}
const preflight = async () => {
  const back = await page.$(`${view} .detail-back`);
  if (back) await back.click();
  // The window footer is fixed over the bottom of the page: a button in the last pixels of the
  // viewport is in view for Puppeteer but covered, so bring it to the middle first.
  await page.$eval(`${view} .ops-actions button`, (e) => e.scrollIntoView({ block: "center" }));
  await page.locator(`${view} .ops-actions button ::-p-text(Start)`).click();
};
const closedSheet = () => page.waitForFunction(() => !document.querySelector("dialog.ops-confirm"));
async function captureSheet(prefix) {
  for (const theme of ['light', 'dark']) {
    await page.evaluate(t => document.documentElement.dataset.theme = t, theme);
    for (const width of [1440, 720]) {
      await page.setViewport({ width, height: width === 720 ? 520 : 900 });
      await page.evaluate(() => document.fonts.ready);
      await page.waitForFunction(() => document.documentElement.scrollWidth <= innerWidth + 1);
      await page.screenshot({ path: path.join(h.shots, `${prefix}-${theme}-${width}.png`) });
    }
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.evaluate(() => document.documentElement.dataset.theme = 'light');
}

try {
  // Two saved connections: Primary (the one in use) and Archive (to switch to).
  for (const [name, host, port] of [["Primary", "primary.test", 3306], ["Archive", "archive.test", 3307]])
    await api("/api/profiles/save", { value: { name, host, port, user: "dba", database: "", ssl_mode: "PREFERRED", ssl_ca: "" } });
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await connect("Primary");
  // Maintenance is the last view in Setup.
  assert.deepEqual(await page.$$eval(".source-list .source-group", (groups) =>
    [...groups.find((g) => g.querySelector("h3").textContent === "Setup").querySelectorAll(".source-item")].at(-1).dataset.view), "maintenance");
  state.fallback = { status: 200, body: tools };
  await h.view("maintenance");
  await page.waitForSelector(`${view} [data-row-key="tool:mysqlsh"]`);
  assert.match(await h.popover("About Maintenance"), /only read.*Audit log/s);
  assert.equal(state.seen.length, 1, "one local tool detection on open");
  assert.equal(state.seen[0].check_updates, false);
  assert.equal(calls(PREPARE).length + calls(START).length, 0, "opening never checks or starts a task");
  await settle(JOBS, 1, "the job list (no database read) is asked once for the connection in use");
  // Health is the Server place's first view: it read once on open for each connection
  // it was shown with (the default one, then Primary). Maintenance itself read nothing else.
  const healthReads = calls("/api/live/health");
  assert.deepEqual(healthReads.map((o) => o.body.connection.host), ["127.0.0.1", "primary.test"], "one on-open Health read per connection");
  assert.deepEqual(state.other.filter((o) => o.path !== JOBS && o.path !== "/api/live/health"), []);

  // Tools on this computer and what they offer for the server.
  state.fallback = { status: 200, body: tools };
  await page.waitForSelector(`${view} [data-row-key="tool:mysqlsh"]`);
  assert.equal(await text(`${view} .summary-text`), "MySQL 8.4.3 · Tools");
  assert.equal(await page.$(`${view} .summary-line .status-pill`), null, "no verdict on a tool list");
  assert.match(await text(`${view} [data-row-key="tool:mysqlpump"]`), /Deprecated/);
  assert.match(await text(`${view} .ops-offered`), /Dumpmysqlsh, mysqldump.*Upgrade checkMySQL Shell \(full check\)/s);
  await h.select(`${view} [data-row-key="tool:mysqlsh"]`);
  assert.match(await h.inspector(), /Path\/usr\/local\/bin\/mysqlsh/);

  // Upgrade check: the partial check without MySQL Shell, labelled as such.
  await section("Upgrade check");
  assert.deepEqual(await page.$$eval(`${view} select[aria-label="Target version"] option`, (els) => els.map((e) => e.value)), ["", "9.6.0", "custom"]);
  await page.select(`${view} select[aria-label="Target version"]`, "custom");
  await field("Exact version", "8.4.0");
  await assertPrivilege(page, `${view} .ops-actions button`, "unknown");
  await page.locator(`${view} label.checkbox ::-p-text(Partial check without MySQL Shell)`).click();
  await page.locator(`${view} .ops-actions button ::-p-text(Check upgrade)`).click();
  await settle(UPGRADE, 1);
  assert.equal("config_path" in calls(UPGRADE)[0].body, false, "HTTP upgrade checks never send a local config file path");
  assert.equal(await page.$eval(view, e => [...e.querySelectorAll("label")].some(l => l.textContent.includes("Server configuration file"))), false);
  assert.deepEqual([calls(UPGRADE)[0].body.target_version, calls(UPGRADE)[0].body.prefer_shell], ["8.4.0", false]);
  await page.waitForSelector(`${view} [data-row-key^="finding:"]`);
  assert.equal(await text(`${view} .summary-text`), "MySQL 8.0.46 to 8.4.0 · Partial check (native subset) · 1 error, 4 warnings, 2 notices");
  assert.match(await h.notes(), /Subset; install MySQL Shell for the full check\. This does not establish that an upgrade is safe\./);
  const results = await page.$$eval(`${view} [data-row-key^="finding:"]`, (rows) => rows.map((r) => r.querySelector("td").textContent));
  assert.deepEqual(results, ["Error", "Warning", "Warning", "Notice", "Notice", "Passed", "Not checked"]);
  await h.select(`${view} [data-row-key="finding:0"]`);
  assert.match(await h.inspector(), /Removed variables set in an option file or on the command lineError/);
  assert.match(await h.inspector(), /default_authentication_plugin \(COMMAND_LINE\)/);
  // MySQL Shell: the full check.
  next.upgrade = shell;
  await page.click(`${view} .detail-back`);
  await page.locator(`${view} label.checkbox ::-p-text(Partial check without MySQL Shell)`).click();
  await page.locator(`${view} .ops-actions button ::-p-text(Check upgrade)`).click();
  await settle(UPGRADE, 2);
  assert.equal(calls(UPGRADE)[1].body.prefer_shell, true);
  await page.waitForFunction((v) => /MySQL Shell ·/.test(document.querySelector(`${v} .summary-text`)?.textContent || ""), {}, view);
  assert.equal(await text(`${view} .summary-text`), "MySQL 8.0.46 to 8.4.0 · MySQL Shell · 2 errors, 27 warnings, 2 notices");

  // Dump: a folder with files in it is refused at the preflight.
  await section("Dump");
  await page.select(`${view} select[aria-label="Scope"]`, "schemas");
  await page.click(`${view} #ops-dump-schemas`);
  await page.waitForSelector(`${view} .perf-picker-panel`);
  await page.locator(`${view} .perf-picker-option ::-p-text(mftab)`).click();
  await page.keyboard.press("Escape");
  await field("Output folder", "/Users/dba/maintenance/busy");
  next.prepare.push({ status: 400, body: { error: `Could not complete the request: ${refused.error}` } });
  await preflight();
  await settle(PREPARE, 1);
  const asked = calls(PREPARE)[0].body;
  assert.deepEqual([asked.operation, asked.directory, asked.schemas, asked.tables, asked.threads, asked.prefer_shell],
    ["dump", "/Users/dba/maintenance/busy", ["mftab"], [], 4, true]);
  await page.waitForSelector(`${view} .ops-preflight-result.has-result`);
  assert.match(await text(`${view} .ops-preflight-result.has-result`), /must not exist or must be empty; refusing to overwrite/);
  assert.equal(await page.$(sheet), null, "no confirmation after a refused preflight");
  // An absent folder: the confirmation with the exact commands, no typed target for a dump.
  await field("Output folder", dumpPlan.target);
  const warningPlan = structuredClone(dumpPlan);
  warningPlan.tool.name = 'mysqlsh';
  warningPlan.warnings = [
    'A consistent snapshot protects InnoDB data only. Concurrent DDL must be avoided.',
    'The reviewed tool options determine whether routines, events and triggers are included.',
    'Shell briefly takes a global read lock; writers wait behind long statements.',
    'MySQL Shell 8.4 is older than the server (8.5) and may fail on it; update MySQL Shell.',
  ];
  next.prepare.push({ status: 200, body: warningPlan });
  await preflight();
  await page.waitForSelector(sheet);
  assert.equal(await text(`${sheet} .eyebrow`), "DUMP");
  assert.equal(await text(`${sheet} h2`), "Dump into the folder backup?");
  assert.equal(await text(`${sheet} .ops-target strong`), dumpPlan.target);
  assert.equal(await text(`${sheet} .ops-commands code`), dumpPlan.commands.join("\n"));
  assert.equal(await page.$(`${sheet} input[type=text]`), null);
  assert.equal(await page.$eval(confirmButton, e => e.classList.contains('primary') && !e.classList.contains('danger-solid')), true);
  assert.match(await text(`${sheet} .inspector-fields`), /Contentsapp \(all tables\)/);
  assert.match(await text(`${sheet} .ops-warning`), /^MySQL Shell .*older than the server/);
  assert.equal(await page.$eval(`${sheet} .ops-warning`, e => !!(e.compareDocumentPosition(e.parentElement.querySelector('.inspector-fields')) & Node.DOCUMENT_POSITION_FOLLOWING)), true);
  assert.equal(await page.$$eval(`${sheet} .ops-warning`, es => es.length), 2, 'routine caveats remain plain notes');
  await captureSheet('dump-confirm');
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel");
  await page.keyboard.press("Escape");
  await closedSheet();
  await settle(DECLINED, 1);
  assert.deepEqual([calls(DECLINED)[0].body.operation, calls(DECLINED)[0].body.statements], ["dump", dumpPlan.commands]);
  assert.equal((await recorded(1))[0].status, "declined");
  await preflight();
  await page.waitForSelector(sheet);
  await page.click(cancelButton);
  await closedSheet();
  await settle(DECLINED, 2);
  assert.equal(calls(START).length, 0, "nothing was started");
  await field('Output folder', '/tmp/settings-changed');
  assert.equal(await page.$(`${view} .ops-details`), null, 'form edits hide commands from the previous plan');
  next.prepare.push({ status: 400, body: { error: 'Shell dry run failed for the new settings.' } });
  await preflight();
  await page.waitForFunction(v => document.querySelector(`${v} .ops-preflight-result`)?.textContent.includes('Shell dry run failed'), {}, view);
  assert.equal(await page.$(`${view} .ops-details`), null, 'failed preflight clears old commands');
  await field('Output folder', dumpPlan.target);

  // Start, progress, Cancel.
  next.start.push({ job: job(running, "job-1") });
  await preflight();
  await page.waitForSelector(sheet);
  await page.click(confirmButton);
  await settle(START, 1);
  const started = calls(START)[0].body;
  assert.equal('typed_acknowledgement' in started, false, 'ordinary dumps send no acknowledgement');
  assert.equal(started.plan.review_token, "review-dump");
  assert.deepEqual(started.plan, dumpPlan, "Start round-trips the whole prepared plan");
  assert.deepEqual([started.operation, started.typed_target, started.confirmed, started.plan.binding], ["dump", dumpPlan.target, true, dumpPlan.binding]);
  await closedSheet();
  await page.waitForSelector(`${view} .ops-job[data-job=job-1][data-state=running]`);
  assert.match(await text(`${view} .ops-job`), /Dump · .*Running.*Cancel/);
  assert.match(await text(`${view} .ops-job .ops-progress`), /25%/);
  await page.waitForFunction(() => document.activeElement?.dataset.job === 'job-1');
  assert.equal(await page.$eval(`${view} .ops-job[data-job=job-1]`, e => e.tabIndex), -1, 'Start focuses its job card after closing the sheet');
  // mysqldump is silent: table sizes supply an explicitly approximate percent.
  const mib = 2 ** 20;
  const phase = 'Dumping with mysqldump into dump.sql.';
  held.set('job-1', job(running, 'job-1', { progress: null, phase, elapsed_s: 83,
    output: { written_bytes: 120 * mib, estimated_bytes: 262 * mib },
    events: [{ at: 'now', stream: 'stderr', text: phase }] }));
  const firstCard = `${view} .ops-job[data-job=job-1]`;
  await page.waitForFunction(s => document.querySelector(`${s} .ops-progress`)?.textContent.includes('≈46%'), {}, firstCard);
  assert.match(await text(`${firstCard} .ops-job-facts`), /Dumping with mysqldump.*120.0 MiB written of about 262.0 MiB.*Elapsed 1:23/);
  assert.equal(await page.$(`${firstCard} .ops-log-line`), null, 'phase is not repeated as tool output');
  assert.equal(await page.$eval(`${firstCard} progress`, e => e.hasAttribute('value')), true);
  assert.match(await page.$eval(`${firstCard} .ops-progress`, e => e.title), /Estimated from the server's table sizes/);
  assert.match(await text(`${view} .ops-actions .ops-job-summary`), /Dump running · ≈46% · 120.0 MiB.*Show progress/);
  // Show progress has the same focus behavior, including reduced motion.
  await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
  await page.locator(`${view} .ops-job-summary button ::-p-text(Show progress)`).click();
  await page.waitForFunction(() => document.activeElement?.dataset.job === 'job-1');
  await page.emulateMediaFeatures([]);
  held.set('job-1', job(running, 'job-1', { progress: null, phase, elapsed_s: 85,
    output: { written_bytes: 314 * mib, estimated_bytes: 262 * mib } }));
  await page.waitForFunction(s => { const text = document.querySelector(`${s} .ops-job-facts`)?.textContent || ''; return text.includes('more than') && /MiB\/s/.test(text); }, {}, firstCard);
  assert.match(await text(`${firstCard} .ops-job-facts`), /314.0 MiB written, more than the 262.0 MiB estimate/);
  assert.match(await text(`${firstCard} .ops-job-facts`), /MiB\/s/, 'rate appears after two output snapshots');
  assert.equal(await page.$eval(`${firstCard} progress`, e => e.hasAttribute('value')), false, 'exceeding the estimate is indeterminate');
  assert.doesNotMatch(await text(`${firstCard} .ops-progress`), /Running|%/, 'no duplicate Running label or invented completion');
  await captureSheet('dump-progress');
  assert.ok(calls(STATUS).length >= 2, "a running job is polled through successive snapshots");
  assert.equal(await page.$eval(`${view} .ops-actions button`, (e) => e.disabled), true, "no second operation while one runs");
  await page.locator(`${view} .ops-job button ::-p-text(Cancel)`).click();
  await settle(CANCEL, 1);
  assert.equal(calls(CANCEL)[0].body.id, "job-1");
  assert.deepEqual(calls(CANCEL)[0].body.connection, calls(START)[0].body.connection, "cancel keeps the starting connection");
  for (const call of calls(STATUS)) assert.deepEqual(call.body.connection, calls(START)[0].body.connection, "status keeps the starting connection");
  await page.waitForSelector(`${view} .ops-job[data-state=cancelled]`);
  assert.match(await text(`${view} .ops-job`), /Cancelled/);
  assert.match(await text(`${view} .ops-job .ops-warning`), /An incomplete dump may remain/);
  assert.equal(calls(REVEAL).length, 0, "cancelled dumps never open a folder");
  const polls = calls(STATUS).length;
  await pause(2000);
  assert.equal(calls(STATUS).length, polls, "a finished job is not polled");

  // A lost Start reply: the job is found in myflames' job list, never started twice.
  held.set("job-2", job(running, "job-2"));
  owners.set("job-2", endpoint(calls(START)[0].body.connection));
  next.start.push({ status: 502, body: { error: "The reply was lost." } });
  await preflight();
  await page.waitForSelector(sheet);
  await page.click(confirmButton);
  await settle(START, 2);
  await page.waitForSelector(`${view} .ops-job[data-job=job-2]`);
  assert.match(await text(`${view} .ops-note`), /The Start reply was lost, but the dump is in myflames' job list.*It was not started again\./);
  await pause(500);
  assert.equal(calls(START).length, 2, "Start is sent once");
  // Completion is followed even after leaving Dump for another section.
  await section("Restore");
  held.set("job-2", job(done, "job-2", { elapsed_s: 91 }));
  await settle(REVEAL, 1, "completion opens the folder while another section is visible");
  await section("Dump");
  await page.waitForSelector(`${view} .ops-job[data-job=job-2][data-state=done]`);
  assert.match(await text(`${view} .ops-job[data-job=job-2]`), /Completed/);
  await settle(REVEAL, 1, 'a dump recovered after a lost Start reply auto-opens once');
  assert.deepEqual(calls(REVEAL)[0].body, { path: `${done.result.directory}/myflames-manifest.json` });
  const completedCard = `${view} .ops-job[data-job=job-2]`;
  assert.match(await text(`${completedCard} .ops-job-facts`), /Completed in 1:31/);
  await page.waitForFunction(s => document.querySelector(s)?.textContent.includes('Opened the dump folder'), {}, completedCard);
  const openLabel = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform || navigator.userAgent)
    ? 'Open in Finder' : /Win/.test(navigator.platform || navigator.userAgent) ? 'Open in File Explorer' : 'Open folder');
  assert.equal(await text(`${completedCard} .ops-job-folder button .action-button-label > span`), openLabel);
  await section('Tools'); await section('Dump');
  await pause(1700);
  assert.equal(calls(REVEAL).length, 1, 'rerenders and section changes never auto-open twice');
  await page.click(`${completedCard} .ops-job-folder button`);
  await settle(REVEAL, 2, 'the Open folder button can reveal again');
  assert.deepEqual(calls(REVEAL)[1].body, calls(REVEAL)[0].body);
  await page.waitForFunction(s => document.querySelector(s)?.textContent.includes('Opened the dump folder') && !document.querySelector(`${s} .ops-job-folder button[aria-busy=true]`), {}, completedCard);
  assert.deepEqual(await texts(`${view} .ops-job[data-job=job-2] .ops-verification strong`), ["files", "source_sample_stability", "dump_rows"]);
  assert.ok(await page.$(`${view} .ops-job[data-job=job-2] details summary ::-p-text(Manifest)`));
  await page.waitForFunction((v, old) => {
    const path = document.querySelector(`${v} .ops-folder-input input`)?.value;
    return path?.startsWith('/Users/dba/maintenance/') && path !== old;
  }, {}, view, dumpPlan.target);

  // Restore: the typed target gates Start.
  await section("Restore");
  await field("Dump folder", dumpPlan.target);
  // Default database: the server's user databases, or a new name typed.
  await page.$eval(`${view} .ops-form details > summary`, (e) => { e.closest("details").open = true; });
  const databaseChoice = `${view} select[aria-label="Default database"]`;
  await page.waitForFunction((s) => document.querySelectorAll(`${s} option`).length > 2, {}, databaseChoice);
  assert.deepEqual(await page.$$eval(`${databaseChoice} option`, (os) => os.map((o) => o.textContent)),
    ["As in the dump", ...schemas.schemas.filter((s) => !s.system).map((s) => s.name), "New database…"]);
  await page.select(databaseChoice, await page.$eval(`${databaseChoice} option:last-child`, (o) => o.value));
  await field("New database name", "restored_copy");
  next.prepare.push({ status: 200, body: restorePlan });
  await preflight();
  await page.waitForSelector(sheet);
  assert.equal(await text(`${sheet} h2`), "Restore into 127.0.0.1:3306?");
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), true);
  await page.type(`${sheet} input[type=text]`, "127.0.0.1:330");
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), true, "the target must be typed exactly");
  await page.type(`${sheet} input[type=text]`, "6");
  assert.equal(await page.$eval(confirmButton, (e) => e.disabled), false);
  assert.equal(calls(PREPARE).at(-1).body.schema, "restored_copy", "a database the server does not have yet can be typed");
  next.start.push({ status: 200, body: { schema_version: "lifecycle-job-1.0", id: null, operation: "restore", state: "refused", progress: null, detail: null, events: [], error: "Read-only: myflames refuses every change on Archive.", result: { status: "refused", verification: [], audit_id: "a1" } } });
  await page.click(confirmButton);
  await settle(START, 3);
  assert.equal(calls(START)[2].body.typed_target, "127.0.0.1:3306");
  assert.equal('typed_acknowledgement' in calls(START)[2].body, false, 'ordinary restores send no acknowledgement');
  await closedSheet();
  await page.waitForSelector(`${view} .view-status [role=alert]`);
  assert.match(await text(`${view} .view-status [role=alert]`), /Read-only: myflames refuses every change/);

  // System-table restores need a separate explicit acknowledgement. Checking
  // it never replaces typing the target, and each new review starts unchecked.
  const systemPlan = { ...structuredClone(restorePlan), review_token: 'review-system-tables',
    required_acknowledgement: `REPLACE mysql SYSTEM TABLES ON ${restorePlan.target}` };
  const ack = `${sheet} .ops-system-ack input[type=checkbox]`;
  next.prepare.push({ status: 200, body: systemPlan });
  await preflight(); await page.waitForSelector(ack);
  assert.equal(await page.$eval(ack, e => e.checked), false);
  assert.equal(await page.$eval(confirmButton, e => e.disabled), true);
  await page.click(ack);
  assert.equal(await page.$eval(confirmButton, e => e.disabled), true, 'acknowledgement alone does not confirm the target');
  await page.type(`${sheet} input[type=text]`, 'wrong target');
  assert.equal(await page.$eval(confirmButton, e => e.disabled), true);
  await page.$eval(`${sheet} input[type=text]`, e => e.select());
  await page.type(`${sheet} input[type=text]`, systemPlan.target);
  assert.equal(await page.$eval(confirmButton, e => e.disabled), false);
  await page.keyboard.press('Escape'); await closedSheet();
  next.prepare.push({ status: 200, body: systemPlan });
  await preflight(); await page.waitForSelector(ack);
  assert.equal(await page.$eval(ack, e => e.checked), false, 'cancelled acknowledgement never carries into a new review');
  await page.type(`${sheet} input[type=text]`, systemPlan.target);
  assert.equal(await page.$eval(confirmButton, e => e.disabled), true, 'typed target alone cannot acknowledge mysql replacement');
  await page.click(ack);
  await captureSheet('restore-system-ack');
  const beforeAckStart = calls(START).length;
  next.start.push({ status: 200, body: { schema_version: 'lifecycle-job-1.0', id: null, operation: 'restore', state: 'refused', progress: null, detail: null, events: [], result: null, error: 'Synthetic acknowledgement fixture refusal.' } });
  await page.click(confirmButton); await closedSheet();
  await settle(START, beforeAckStart + 1);
  assert.equal(calls(START).at(-1).body.typed_acknowledgement, systemPlan.required_acknowledgement);
  assert.deepEqual(calls(START).at(-1).body.plan, systemPlan, 'the reviewed plan and token are unchanged');

  // Clone: the donor fields fill from a saved connection (never the one in
  // use, never a password), and stay editable.
  await section("Clone");
  const fillChoice = `${view} select[aria-label="Fill from saved connection"]`;
  assert.deepEqual(await page.$$eval(`${fillChoice} option`, (os) => os.map((o) => o.textContent)),
    ["Choose a connection", "Archive · archive.test:3307"]);
  await field("Donor password", "typed-before");
  await page.select(fillChoice, await page.$eval(`${fillChoice} option:last-child`, (o) => o.value));
  const donorValue = (label) => page.$eval(`${view} label.ops-field ::-p-text(${label})`, (span) => span.closest("label").querySelector("input, select").value);
  assert.deepEqual([await donorValue("Donor host"), await donorValue("Donor port"), await donorValue("Donor user"), await donorValue("Donor password"),
    await page.$eval(`${view} select[aria-label="TLS mode"]`, (e) => e.value)], ["archive.test", "3307", "dba", "", "REQUIRED"]);
  assert.match(await page.$eval(fillChoice, (e) => e.closest("label").textContent), /Filled from Archive; TLS set to Required, which clone needs\. Enter the password\./);
  await field("Donor port", "3306");
  assert.equal(await page.$eval(fillChoice, (e) => e.value), "", "edited fields no longer claim the saved connection");
  // Clone: a preflight with failures explains each one and offers no Start.
  await field("Donor host", "donor.test");
  await field("Donor user", "clone");
  await field("Donor password", "donor-secret-1");
  next.prepare.push({ status: 200, body: cloneFailed });
  await preflight();
  await page.waitForSelector(`${view} [data-row-key^="check:"]`);
  assert.equal(await page.$(sheet), null, "no confirmation after failed checks");
  assert.match(await text(`${view} .ops-preflight-result`), /Start clone is blocked by 2 failed checks/);
  const asked2 = calls(PREPARE).at(-1).body;
  assert.deepEqual([asked2.operation, asked2.donor.host, asked2.donor.user, asked2.require_ssl, asked2.data_directory], ["clone", "donor.test", "clone", true, ""]);
  const first = await page.$$eval(`${view} [data-row-key^="check:"]`, (rows) => rows.slice(0, 2).map((r) => [...r.querySelectorAll("td")].slice(0, 2).map((td) => td.textContent)));
  assert.deepEqual(first, [["Failed", "Donor plugin"], ["Failed", "Valid donor"]]);
  const failedRow = await page.$$eval(`${view} [data-row-key^="check:"]`, (rows) => rows.find((r) => /Valid donor/.test(r.textContent)).dataset.rowKey);
  await h.select(`${view} [data-row-key="${failedRow}"]`);
  assert.match(await h.inspector(), /valid_donorFailed/);
  assert.match(await h.inspector(), /clone_valid_donor_list/);
  const savedClone = await h.savedSlow((s) => s.maintenance_section === "clone");
  assert.equal(JSON.stringify(savedClone).includes("donor-secret-1"), false, "the donor password is never saved");
  assert.equal(JSON.stringify(savedClone).includes("donor.test"), false, "no donor field is saved");

  // Clone confirmation displays the bound pair, not the form's current
  // labels, and Start must preserve every opaque plan field.
  const reviewedClone = { ...clonePlan, expect: {
    donor: { endpoint: "donor.test:3306", connection_key: "donor-key", type: "development", read_only: true },
    recipient: { endpoint: "primary.test:3306", connection_key: "recipient-key", type: "production", read_only: false },
    donor_address: "donor.internal:3306", require_ssl: true,
  } };
  next.prepare.push({ status: 200, body: reviewedClone });
  await preflight();
  await page.waitForSelector(sheet);
  assert.match(await text(sheet), /Donordonor.test:3306.*Donor typeDevelopment, read-only.*Recipientprimary.test:3306.*Recipient typeProduction/s);
  assert.match(await text(sheet), /Donor address used by recipientdonor.internal:3306/);
  assert.equal(await page.$eval(confirmButton, e => e.disabled), true);
  await page.type(`${sheet} input[type=text]`, reviewedClone.target);
  next.start.push({ job: job(done, "clone-done", { operation: "clone" }) });
  const cloneStarts = calls(START).length;
  await page.click(confirmButton);
  await settle(START, cloneStarts + 1);
  assert.deepEqual(calls(START).at(-1).body.plan, reviewedClone);
  assert.equal(calls(START).at(-1).body.donor.password, "donor-secret-1");
  await closedSheet();
  assert.equal(calls(REVEAL).length, 2, "a completed clone never opens the dump folder");

  // A running job and the tools report greyed after a connection switch; polling waits.
  await section("Dump");
  next.start.push({ job: job(running, "job-3") });
  await preflight();
  await page.waitForSelector(sheet);
  await page.click(confirmButton);
  await page.waitForSelector(`${view} .ops-job[data-job=job-3][data-state=running]`);
  await connect("Archive");
  await page.waitForSelector(`${view} .ops-job[data-job=job-3][data-other-connection]`);
  assert.match(await text(`${view} .ops-job[data-job=job-3]`), /Switch back to that connection to manage this job\./);
  assert.equal(await page.$eval(`${view} .ops-job[data-job=job-3] .ops-job-head button`, (e) => e.disabled), true);
  await page.waitForFunction(v => !document.querySelector(`${v}[data-other-connection]`), {}, view);
  assert.equal(state.seen.filter(r => r.connection.host === "archive.test").length, 1, "tool discovery follows the new connection automatically");
  const quiet = calls(STATUS).length;
  await pause(2000);
  assert.ok(calls(STATUS).length <= quiet + 1, "no polling for another connection's job");

  // Session restore: the section comes back, the running job is found again, nothing starts.
  const starts = calls(START).length;
  await h.savedSlow((s) => s.maintenance_section === "dump");
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await h.server();
  await page.waitForSelector(view);
  assert.equal(await page.$eval(`${view} .segmented [aria-pressed=true]`, (e) => e.textContent), "Dump");
  // The page is on Archive now: its job list is empty.
  assert.equal(await page.$(`${view} .ops-job`), null);
  const revealsBeforeAdoption = calls(REVEAL).length;
  // Back on Primary the running job is found again and followed.
  const before = calls(STATUS).length;
  await connect("Primary");
  await page.waitForSelector(`${view} .ops-job[data-job=job-3][data-state=running]`);
  await settle(STATUS, before + 1, "the found job is polled");
  await page.waitForSelector(`${view} .ops-job[data-job=job-2][data-state=done] .ops-job-folder button`);
  await pause(1700);
  assert.equal(calls(REVEAL).length, revealsBeforeAdoption, 'a completed dump adopted on mount never auto-reveals');
  await page.locator(`${view} .ops-job[data-job=job-3] button ::-p-text(Cancel)`).click();
  await page.waitForSelector(`${view} .ops-job[data-job=job-3][data-state=cancelled]`);
  assert.equal(calls(START).length, starts, "a restored workspace never starts a job");

  for (const call of [...calls(STATUS), ...calls(CANCEL)])
    assert.equal(endpoint(call.body.connection), owners.get(call.body.id), "status/cancel always use the owner endpoint");
  for (const call of [...calls(UPGRADE), ...calls(PREPARE)]) assert.equal("config_path" in call.body, false);

  // A completed Start reply opens its folder even when reveal fails. The
  // error stays in the card; only an explicit click retries the reveal.
  next.start.push({ job: job(done, 'immediate-dump', { elapsed_s: 2 }) });
  next.reveal.push({ status: 400, body: { error: 'Could not reveal the dump folder.' }, delay: 300 });
  await preflight(); await page.waitForSelector(sheet);
  await page.click(confirmButton); await closedSheet();
  const immediate = `${view} .ops-job[data-job=immediate-dump]`;
  await page.waitForFunction(() => document.activeElement?.dataset.job === 'immediate-dump');
  await settle(REVEAL, revealsBeforeAdoption + 1);
  await page.waitForFunction(s => document.querySelector(s)?.textContent.includes('Could not reveal the dump folder.'), {}, immediate);
  await section('Restore'); await section('Dump');
  assert.match(await text(immediate), /Could not reveal the dump folder\./, 'reveal feedback survives a section switch');
  assert.equal(calls(REVEAL).length, revealsBeforeAdoption + 1, 'failed automatic reveal is not retried by rendering');
  await page.click(`${immediate} .ops-job-folder button`);
  await settle(REVEAL, revealsBeforeAdoption + 2);
  await page.waitForFunction(s => document.querySelector(s)?.textContent.includes('Opened the dump folder'), {}, immediate);
  assert.deepEqual(calls(REVEAL).at(-1).body, { path: `${done.result.directory}/myflames-manifest.json` });

  next.start.push({ job: job(done, 'failed-dump', { state: 'failed', result: { ...done.result, status: 'verification_failed' } }) });
  await preflight(); await page.waitForSelector(sheet);
  await page.click(confirmButton); await closedSheet();
  await page.waitForFunction(() => document.activeElement?.dataset.job === 'failed-dump');
  assert.equal(await page.$(`${view} .ops-job[data-job=failed-dump] .ops-job-folder button`), null);
  assert.equal(calls(REVEAL).length, revealsBeforeAdoption + 2, 'failed dumps do not reveal, even when a directory is present');

  // The tool is chosen by preflight, not guessed from the user's preference.
  await section("Tools");
  await h.refresh(); await h.idle(view);
  await section("Dump");
  const prepareButton = `${view} .ops-actions button`;
  for (const [tool, key] of [["mysqlsh", "dump_shell"], ["mysqldump", "dump_mysqldump"], ["mariadb-dump", "dump_mariadb"]]) {
    const detected = structuredClone(tools);
    detected.offered.dump = [tool];
    state.queue.push({ status: 200, body: detected });
    await section("Tools");
    const detection = page.waitForResponse(r => new URL(r.url()).pathname === '/api/lifecycle/tools');
    await h.refresh(); await detection;
    await page.waitForFunction(() => !document.querySelector('.ops-view .spinner'));
    await section("Dump");
    await field("Output folder", `/tmp/gating-${tool}`);
    await h.setPrivileges({ [key]: privilegeAnswer("denied", "RELOAD") });
    await assertPrivilege(page, prepareButton, "denied", "RELOAD");
    const beforePrepare = calls(PREPARE).length;
    await preflight();
    assert.equal(calls(PREPARE).length, beforePrepare, 'detected tool grants are used before a plan exists');
    await h.setPrivileges({ [key]: privilegeAnswer("allowed") });
    await assertPrivilege(page, prepareButton, "allowed");
    const chosenTool = structuredClone(dumpPlan);
    chosenTool.tool.name = tool;
    next.prepare.push({ status: 200, body: chosenTool });
    await preflight(); await page.waitForSelector(sheet);
    await h.setPrivileges({ [key]: privilegeAnswer("denied", "RELOAD") });
    await assertPrivilege(page, confirmButton, "denied", "RELOAD");
    const before = calls(START).length;
    await page.click(confirmButton); await page.keyboard.press("Enter");
    assert.equal(calls(START).length, before);
    await page.keyboard.press("Escape"); await closedSheet();
    await assertPrivilege(page, prepareButton, "denied", "RELOAD");
    await h.setPrivileges({ [key]: privilegeAnswer("allowed") });
    await assertPrivilege(page, prepareButton, "allowed");
    next.prepare.push({ status: 200, body: chosenTool });
    await preflight(); await page.waitForSelector(sheet);
    await assertPrivilege(page, confirmButton, "allowed");
    await h.setPrivileges({});
    await assertPrivilege(page, confirmButton, "unknown");
    await page.keyboard.press("Escape"); await closedSheet();
  }
  await section("Restore"); await field("Dump folder", "/tmp/gating-restore");
  // A database chosen from the list is sent by name, as a typed one was.
  await page.$eval(`${view} .ops-form details > summary`, (e) => { e.closest("details").open = true; });
  await page.select(`${view} select[aria-label="Default database"]`, "mftab");
  await h.setPrivileges({ restore: privilegeAnswer("denied", "READ_ONLY ADMIN") });
  await assertPrivilege(page, prepareButton, "denied", "READ_ONLY ADMIN");
  await h.setPrivileges({ restore: privilegeAnswer("allowed") });
  await assertPrivilege(page, prepareButton, "allowed");
  next.prepare.push({ status: 200, body: restorePlan });
  await preflight(); await page.waitForSelector(sheet);
  assert.equal(calls(PREPARE).at(-1).body.schema, "mftab");
  await page.type(`${sheet} input[type=text]`, restorePlan.target);
  await assertPrivilege(page, confirmButton, "allowed");
  await h.setPrivileges({ restore: privilegeAnswer("denied", "READ_ONLY ADMIN") });
  await assertPrivilege(page, confirmButton, "denied", "READ_ONLY ADMIN");
  const beforeRestore = calls(START).length;
  await page.click(confirmButton);
  assert.equal(calls(START).length, beforeRestore);
  await h.setPrivileges({});
  await assertPrivilege(page, confirmButton, "unknown");
  await page.keyboard.press("Escape"); await closedSheet();

  await section("Clone");
  await field("Donor host", "donor.test"); await field("Donor user", "backup");
  await h.setPrivileges({ clone_recipient: privilegeAnswer("denied", "CLONE_ADMIN"), clone_donor: privilegeAnswer("allowed") });
  await assertPrivilege(page, prepareButton, "denied", "CLONE_ADMIN");
  await page.select(`${view} select[aria-label=Mode]`, "directory");
  await field("Data directory", "/tmp/clone-target");
  await assertPrivilege(page, prepareButton, "unknown");
  const donorDenied = structuredClone(cloneFailed);
  donorDenied.request.data_directory = "/tmp/clone-target";
  donorDenied.checks = [
    { check: "recipient_privilege", status: "passed", reason: "BACKUP_ADMIN required on the recipient account." },
    { check: "donor_privilege", status: "failed", reason: "BACKUP_ADMIN required on the donor account." },
  ];
  next.prepare.push({ status: 200, body: donorDenied });
  await preflight(); await page.waitForSelector(`${view} [data-row-key^="check:"]`);
  await assertPrivilege(page, prepareButton, "denied", "donor account");
  // Editing donor credentials invalidates its earlier answer. The active
  // provider's clone_donor entry must never decide for the remote account.
  await field("Donor password", "changed-secret");
  await h.setPrivileges({ clone_recipient: privilegeAnswer("denied", "CLONE_ADMIN"), clone_donor: privilegeAnswer("denied", "BACKUP_ADMIN") });
  await assertPrivilege(page, prepareButton, "unknown");
  const permittedClone = structuredClone(donorDenied);
  permittedClone.ready = true;
  permittedClone.checks.forEach(c => { c.status = "passed"; });
  next.prepare.push({ status: 200, body: permittedClone });
  await preflight(); await page.waitForSelector(sheet);
  await page.type(`${sheet} input[type=text]`, permittedClone.target);
  await assertPrivilege(page, confirmButton, "allowed");
  await page.keyboard.press("Escape"); await closedSheet();
  assert.equal(await page.$(`${view} [data-action=refresh]`), null, "Clone has no unrelated tool refresh");
  const unsupported = structuredClone(tools);
  unsupported.server.engine = "mariadb"; unsupported.offered.clone = false;
  state.queue.push({ status: 200, body: unsupported });
  await section("Tools");
  await h.refresh(); await h.idle(view);
  assert.equal(await page.$(`${view} .segmented [data-value=clone]`), null);
  assert.equal(await page.$(`${view} .ops-actions button.primary`), null, "unsupported clone has no write action");
  await section("Tools");
  await h.setPrivileges({});

  await h.narrow("maintenance-390.png");
  await page.screenshot({ path: path.join(h.shots, "maintenance-1440.png") });
  assert.deepEqual(h.errors, []);
  console.log("maintenance: ok");
} finally {
  await h.close();
}
