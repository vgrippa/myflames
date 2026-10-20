// The Audit log (Settings menu): the real server on a temporary workspace
// database, first empty, then with one record per status written by the real
// audit code (audit-seed.py). Opens from Settings with no place active; the
// table follows the DataTable contract; every filter (connection, status,
// period) goes to the server before its page of 500, and Load older pages
// through the rest (older rows open in the inspector);
// the inspector pairs each identifier with its value;
// statements stay redacted; CSV and JSON export through /api/audit/export;
// retention through /api/audit/settings, in a menu even when the log is
// empty; the Status column never clips; 390 px without horizontal scroll.
// Run after `npm run build`: node test/audit.mjs (or npm run test:audit)
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-audit-"));
const downloads = path.join(directory, "downloads");
await fs.mkdir(downloads);
const store = path.join(directory, "workspace.sqlite3");
// A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
const env = { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" };
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    store,
  ],
  { cwd: root, env },
);

let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    server.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    server.on("error", reject);
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  // Requests are watched, not changed: every audit call reaches the real server.
  const calls = [];
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname.startsWith("/api/audit/")) calls.push({ path: pathname, body: JSON.parse(request.postData() || "{}") });
  });
  const client = await page.createCDPSession();
  await client.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: downloads });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");

  const view = ".server-view[data-view=audit]";
  const table = `${view} [aria-label="Audit records"]`;
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const texts = (selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent.trim()));
  const rows = () => page.$$eval(`${table} tbody tr[data-row-key]`, (els) => els.map((e) => e.dataset.status));
  const idle = () => page.waitForFunction((v) => !document.querySelector(`${v} .toolbar-refresh .spinner`), {}, view);
  const last = (pathname) => calls.filter((c) => c.path === pathname).at(-1);
  const filter = async (name, value) => {
    await page.select(`${view} .audit-filter-${name} select`, value);
    await idle();
  };
  async function openAudit() {
    await page.click('button[aria-label="Settings"]');
    await page.waitForSelector("#settings-popover");
    await page.locator("#settings-popover button ::-p-text(Audit log)").click();
    await page.waitForSelector(view);
    await idle();
  }
  async function download(format) {
    const before = new Set(await fs.readdir(downloads));
    await page.click(`${view} .audit-export button`);
    await page.waitForSelector(`[role=dialog][aria-label="Export the audit log"]`);
    await page.click(`[role=dialog][aria-label="Export the audit log"] button[data-format=${format}]`);
    for (let i = 0; i < 100; i++) {
      const added = (await fs.readdir(downloads)).filter((f) => !before.has(f) && !f.endsWith(".crdownload"));
      if (added.length) {
        await page.keyboard.press("Escape");
        return fs.readFile(path.join(downloads, added[0]), "utf8");
      }
      await new Promise((r) => setTimeout(r, 50));
    }
    throw new Error("no download for " + format);
  }

  // 1. Settings opens the Audit log: a screen of its own, no place active,
  // focus on its title; an empty log says what will be recorded.
  await openAudit();
  assert.deepEqual(await page.$$eval(".main-nav [aria-current=page]", (els) => els.length), 0, "no place is active");
  assert.match(await text(".topbar-title"), /^Audit log/);
  assert.equal(await page.evaluate(() => document.activeElement?.id), "audit-title");
  assert.equal(await page.$eval("#audit-title", (e) => e.classList.contains("visually-hidden")), true, "the top bar supplies the visible screen title");
  await page.waitForSelector(`${view} .view-empty`);
  assert.match(await text(`${view} .view-empty`), /No writes recorded yet/);
  assert.match(await text(`${view} .view-empty`), /Writes made through myflames are recorded here/);
  assert.match(await text(`${view} .view-empty`), /90 days/);
  assert.equal(last("/api/audit/list").body.limit, 500);
  // Retention stays reachable with nothing listed (review gate): in the toolbar.
  assert.equal(await text(`${view} .view-toolbar .audit-retention button`), "Retention");
  await page.click(`${view} .audit-retention button`);
  assert.match(await text('[role=dialog][aria-label="Keep audit records"]'), /Kept 90 days/);
  await page.keyboard.press("Escape");
  // The ⓘ names every status, unknown with its meaning.
  await page.click(`button[aria-label="About Audit log"]`);
  const about = await text('[role=dialog][aria-label="About Audit log"]');
  assert.match(about, /Outcome unknown\s*Interrupted, timed out or disconnected after the statement may have reached the server/);
  assert.match(about, /Declined\s*Cancelled in the confirmation\. Nothing was sent\./);
  await page.keyboard.press("Escape");

  // 2. Records written by the audit code: newest first, one row per record.
  const seeded = execFileSync(process.env.PYTHON || "python3", [path.join(root, "apps/workspace/test/audit-seed.py"), store], {
    cwd: root,
    env,
  })
    .toString()
    .trim();
  const ids = Object.fromEntries(seeded.split(" ").map((pair) => pair.split("=")));
  // Future fields and check tokens stay readable without changing their values.
  execFileSync(process.env.PYTHON || "python3", ["-c", `
import json, sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    target = json.loads(db.execute("SELECT target FROM audit WHERE id = ?", (sys.argv[2],)).fetchone()[0])
    target["future_field_name"] = "stored_value"
    db.execute("UPDATE audit SET target = ?, verified = ? WHERE id = ?",
               (json.dumps(target), json.dumps(["future_check_name"]), sys.argv[2]))
`, store, ids.pending], { cwd: root, env });
  await page.click(`${view} [data-action=refresh]`);
  await page.waitForSelector(`${table} tbody tr[data-row-key]`);
  await idle();
  assert.deepEqual(await rows(), ["ok", "error", "unknown", "pending", "refused", "declined", "declined"]);
  assert.deepEqual(await texts(`${table} thead th[data-col] .th-label`), [
    "Time", "Status", "Connection", "Type", "Operation", "Statements", "Error", "Duration",
  ]);
  assert.match(await text(`${view} .summary-line`), /^7 records · newest first · Kept 90 days$/);
  const row = (id) => `${table} tr[data-row-key="${id}"]`;
  assert.match(await text(row(ids.ok)), /Done\s*Shop primary\s*Production\s*Kill connection\s*KILL CONNECTION 93/);
  // The cell drops the myflames tag; its tooltip and the inspector keep the statement as stored.
  assert.equal(await page.$eval(`${row(ids.ok)} td[data-col="statements"] .cell-name`, (e) => e.title), "/*myflames:kill*/ KILL CONNECTION 93");
  assert.match(await text(row(ids.error)), /Failed\s*Shop primary.*Kill query.*1095: ERROR 1095 \(HY000\): You are not owner of thread 94/);
  assert.match(await text(row(ids.unknown)), /Outcome unknown\s*Reports\s*Development\s*Kill connection.*2013/);
  assert.match(await text(row(ids.pending)), /Not finished\s*root@127\.0\.0\.1:3310\s*Production/);
  assert.match(await text(row(ids.refused)), /Refused\s*Reports\s*Development · Read-only/);
  // Statements as stored: the password never reaches the page.
  assert.match(await text(row(ids.declined)), /IDENTIFIED BY '\[redacted\]'/);
  assert.equal(await page.evaluate(() => document.body.textContent.includes("hunter2")), false);

  // A remembered keyboard cursor does not look inspected while focus is elsewhere.
  assert.equal(await page.$$eval(`${table} tr.is-selected`, (els) => els.length), 0);
  await page.focus(`${table} tbody tr[tabindex="0"]`);
  await page.keyboard.press("ArrowDown");
  assert.equal(await page.$(".view-inspector"), null);
  assert.equal(await page.$$eval(`${table} tr.is-selected`, (els) => els.length), 1);
  const cursor = await page.evaluate(() => document.activeElement.dataset.rowKey);
  await page.focus("#audit-title");
  assert.equal(await page.$$eval(`${table} tr.is-selected`, (els) => els.length), 0);
  assert.equal(await page.$eval(row(cursor), (e) => e.tabIndex), 0, "the keyboard cursor is retained");
  await page.focus(row(cursor));
  assert.equal(await page.$eval(row(cursor), (e) => e.classList.contains("is-selected")), true);

  // 3. Status filter (read by the server): Outcome unknown only.
  await filter("status", "unknown");
  assert.equal(last("/api/audit/list").body.status, "unknown");
  assert.deepEqual(await rows(), ["unknown"]);
  assert.match(await text(`${view} .summary-line`), /^1 record · newest first · Kept 90 days$/);
  await filter("status", "");
  // Connection filter (the server's): Reports only.
  assert.deepEqual(
    await page.$$eval(`${view} .audit-filter-connection option`, (els) => els.map((e) => e.value)),
    ["", "127.0.0.1:3310", "Reports", "Shop primary"],
  );
  await filter("connection", "Reports");
  assert.equal(last("/api/audit/list").body.server, "Reports");
  assert.deepEqual(await rows(), ["unknown", "refused"]);
  await filter("connection", "");
  // Period: the 40-day-old record drops out of the last 30 days.
  await filter("since", "30");
  assert.equal(last("/api/audit/list").body.since_days, 30);
  assert.equal((await rows()).length, 6);
  assert.equal(await page.$(row(ids.old)), null);
  await filter("since", "");

  // 4. Inspector: the full record. Short identifiers pair with their value;
  // the kit owns whether a long key needs its own row.
  await page.click(row(ids.unknown));
  await page.waitForSelector(".view-inspector .inspector-title");
  const inspector = await text(".view-inspector");
  assert.match(inspector, /Kill connection/);
  assert.match(inspector, /Outcome unknown/);
  assert.match(inspector, /may have reached the server\. Check the server to see whether it ran\./);
  assert.match(inspector, /\/\*myflames:kill\*\/ KILL CONNECTION 55/);
  assert.match(inspector, /As myflames stored it, with secrets redacted\./);
  assert.match(inspector, /NameReports/);
  assert.match(inspector, /Serveranalyst@reports\.internal:3307/);
  assert.match(inspector, /TypeDevelopment/);
  assert.match(inspector, /Error code2013/);
  assert.match(inspector, /Checked before sending/);
  assert.match(inspector, /Lost connection to MySQL server during query\. The statement may have reached the server; its outcome is unknown\./);
  assert.deepEqual(await texts(".view-inspector .audit-list li"), ["Account matched", "Host matched", "Not this session"]);
  const field = (name) => page.evaluate((name) => {
    const dt = [...document.querySelectorAll(".view-inspector .inspector-fields dt")].find((e) => e.textContent === name);
    return dt?.nextElementSibling?.textContent;
  }, name);
  assert.equal(await field("Kill mode"), "connection");
  assert.equal(await field("Action"), "confirm");
  assert.equal(await field("Object kind"), "risky");
  assert.equal(await field("Category"), "kill");
  const label = await page.evaluateHandle(() =>
    [...document.querySelectorAll(".view-inspector .inspector-fields dt")].find((e) => e.textContent === "Connection ID"),
  );
  assert.equal(await label.evaluate((e) => e.querySelector("code")), null, "human labels are plain text");
  assert.equal(await label.evaluate((e) => e.nextElementSibling?.tagName), "DD");
  assert.equal(await label.evaluate((e) => e.nextElementSibling?.textContent), "55");
  const plain = await page.evaluateHandle(() =>
    [...document.querySelectorAll(".view-inspector .inspector-fields dt")].find((e) => e.textContent === "Name"),
  );
  assert.equal(await plain.evaluate((e) => e.nextElementSibling?.textContent), "Reports");
  await page.focus("#audit-title");
  assert.equal(await page.$eval(row(ids.unknown), (e) => e.classList.contains("is-selected")), true, "open inspector keeps its row filled without table focus");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));
  await page.waitForFunction((id) => document.activeElement?.dataset.rowKey === id, {}, ids.unknown);
  await page.focus("#audit-title");
  assert.equal(await page.$$eval(`${table} tr.is-selected`, (els) => els.length), 0);
  await page.click(row(ids.pending));
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.equal(await field("Future field name"), "stored_value");
  assert.deepEqual(await texts(".view-inspector .audit-list li"), ["Future check name"]);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));

  // 5. DataTable contract: the Columns menu hides and shows a column.
  await page.click(`${table} .table-columns-button`);
  await page.waitForSelector('[role=dialog][aria-label="Columns of Audit records"]');
  await page.click('[role=dialog][aria-label="Columns of Audit records"] input[data-col="error"]');
  assert.equal(await page.$(`${table} th[data-col="error"]`), null);
  await page.click('[role=dialog][aria-label="Columns of Audit records"] input[data-col="error"]');
  await page.waitForSelector(`${table} th[data-col="error"]`);
  await page.keyboard.press("Escape");
  assert.ok(await page.$(`${table} th[data-col="time"] [role=separator][aria-label="Width of Time"]`));

  // 6. Export: CSV and JSON from /api/audit/export, with the filters on screen
  // (the status filter applied to the file as well).
  await filter("status", "declined");
  const csv = await download("csv");
  assert.equal(last("/api/audit/export").body.format, "csv");
  assert.equal(last("/api/audit/export").body.status, "declined");
  assert.equal(last("/api/audit/export").body.server, undefined);
  const lines = csv.trim().split("\n");
  assert.match(lines[0], /^id,attempted_at,finished_at,status,/);
  assert.equal(lines.length, 3, "header and the two declined records");
  assert.ok(lines.slice(1).every((l) => l.split(",")[3] === "declined"));
  assert.equal(csv.includes("hunter2"), false);
  await filter("status", "");
  await filter("connection", "Shop primary");
  const json = JSON.parse(await download("json"));
  assert.equal(last("/api/audit/export").body.server, "Shop primary");
  assert.equal(json.schema_version, "audit-1.0");
  assert.deepEqual(json.records.map((r) => r.status).sort(), ["declined", "declined", "error", "ok"]);
  await filter("connection", "");

  // 7. Retention: shorter deletes older records now.
  await page.click(`${view} .audit-retention button`);
  await page.waitForSelector('[role=dialog][aria-label="Keep audit records"]');
  const days = '[role=dialog][aria-label="Keep audit records"] input';
  await page.click(days, { clickCount: 3 });
  await page.keyboard.type("30");
  await page.click('[role=dialog][aria-label="Keep audit records"] button[type=submit]');
  await page.waitForFunction(() => !document.querySelector('[role=dialog][aria-label="Keep audit records"]'));
  assert.equal(last("/api/audit/settings").body.retention_days, 30);
  await idle();
  assert.equal((await rows()).length, 6, "the 40-day-old record is gone");
  assert.match(await text(`${view} .summary-line`), /^6 records · newest first · Kept 30 days$/);
  await page.click(`${view} .audit-retention button`);
  assert.match(await text('[role=dialog][aria-label="Keep audit records"]'), /Kept 30 days/);
  await page.keyboard.press("Escape");

  // 7b. More than a page: 520 newer records push the others beyond the first
  // 500. A status filter still finds an older record (the server filters
  // before the page), Load older brings the rest, and an older row opens.
  execFileSync(process.env.PYTHON || "python3", [path.join(root, "apps/workspace/test/audit-seed.py"), store, "--bulk", "520"], { cwd: root, env });
  await page.click(`${view} [data-action=refresh]`);
  await page.waitForFunction((t) => document.querySelector(`${t}`)?.getAttribute("aria-rowcount") === "501", {}, table + " table");
  await idle();
  assert.match(await text(`${view} .summary-line`), /^500 records, more are older · newest first · Kept 30 days$/);
  assert.equal(await page.$(row(ids.unknown)), null, "beyond the first page");
  await filter("status", "unknown");
  assert.deepEqual(await rows(), ["unknown"]);
  await page.click(row(ids.unknown));
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.match(await text(".view-inspector"), /KILL CONNECTION 55/);
  // At 1440 px with the inspector open, "Outcome unknown" stays whole and in view.
  const pill = await page.$eval(`${row(ids.unknown)} td[data-col="status"]`, (td) => {
    const wrap = td.closest(".data-table-wrap").getBoundingClientRect();
    const p = td.querySelector(".status-pill").getBoundingClientRect();
    return { inside: p.right <= wrap.right && p.left >= wrap.left, clipped: td.scrollWidth > td.clientWidth + 1, text: td.textContent };
  });
  assert.deepEqual(pill, { inside: true, clipped: false, text: "Outcome unknown" });
  await page.keyboard.press("Escape");
  await filter("status", "");
  await page.locator(`${view} .audit-more button`).click();
  await page.waitForFunction((t) => document.querySelector(`${t}`)?.getAttribute("aria-rowcount") === "527", {}, table + " table");
  assert.equal(last("/api/audit/list").body.before.length, 32, "the next page starts after the last record listed");
  assert.equal(await page.$(`${view} .audit-more`), null, "nothing older");
  assert.match(await text(`${view} .summary-line`), /^526 records · newest first · Kept 30 days$/);
  // Long tables draw only the rows in view: scroll the oldest one in.
  await page.$eval(`${table}`, (e) => (e.scrollTop = e.scrollHeight));
  await page.waitForSelector(row(ids.declined));
  await page.click(row(ids.declined));
  await page.waitForSelector(".view-inspector .inspector-title");
  assert.match(await text(".view-inspector"), /IDENTIFIED BY '\[redacted\]'/);
  await page.keyboard.press("Escape");
  await page.$eval(`${table}`, (e) => (e.scrollTop = 0));

  // Compact controls are available at tablet width too, before the filter
  // labels compete with the trailing actions. They keep the same three filters.
  await page.setViewport({ width: 900, height: 900 });
  await page.waitForSelector(`${view}.is-compact-controls`);
  assert.ok(await page.$(`${view} [role=group][aria-label="Audit actions"] .audit-retention`));
  assert.ok(await page.$(`${view} [role=group][aria-label="Audit actions"] .audit-export`));
  await page.click(`${view} .audit-filters > button`);
  assert.equal(await page.$$eval('[role=dialog][aria-label="Filter audit records"] select', (els) => els.length), 3);
  await page.keyboard.press("Escape");

  // 8. 390 px: no horizontal page scroll; details use the full work area.
  await page.setViewport({ width: 390, height: 844 });
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.waitForSelector(`${view}.is-compact-controls`);
  assert.equal(await page.$(`${view} .view-toolbar .audit-filter-connection`), null, "mobile filters stay behind one control");
  assert.ok(await page.$eval(`${view} .view-toolbar`, (e) => e.getBoundingClientRect().height <= 56), "the mobile toolbar is one row");
  await page.click(`${view} .audit-filters > button`);
  const filters = '[role=dialog][aria-label="Filter audit records"]';
  assert.equal(await page.$$eval(`${filters} select`, (els) => els.length), 3);
  await page.select(`${filters} .audit-filter-status select`, "ok");
  await idle();
  assert.equal(last("/api/audit/list").body.status, "ok");
  await page.keyboard.press("Escape");
  assert.match(await text(`${view} .audit-filters button`), /^Filter \(1\)$/);
  await page.click(`${view} .audit-filters > button`);
  await page.select(`${filters} .audit-filter-status select`, "");
  await idle();
  await page.keyboard.press("Escape");
  await page.click(`${table} tbody tr[data-row-key]`);
  await page.waitForSelector(".view-inspector.is-detail");
  await page.screenshot({ path: path.join(directory, "audit-mobile.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector"));

  assert.deepEqual(errors, []);
  console.log("Audit log checks passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
