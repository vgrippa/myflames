// Import CSV, live and end to end: the real wizard (ImportCsv.tsx), the real
// backend (/api/import/csv/*) and running MySQL 8.4 and MariaDB 11.4 servers,
// with no mocked reply.
//
// LAB ONLY. This file is not part of `npm test`, run-tests.sh, CI or any
// package.json script; run it by hand on a computer with Docker. It starts
// two throwaway containers, myflames-e2e-<token>-mysql84 and
// myflames-e2e-<token>-mariadb114 (`--rm`, the port published on 127.0.0.1
// only, `--local-infile=1`), and removes them with their volumes at the end,
// also on Ctrl+C. An image is pulled only when it is missing and is then
// removed again; no other container, volume or image is touched. Root gets a
// random password that reaches Docker through a 0600 env file and the
// workspace through /api/profiles/save, and is never printed. What this file
// sends to a server itself (the e2e database and its tables, SET GLOBAL
// local_infile for the OFF check, SELECTs to read rows back through the
// client inside the container) goes only to those containers.
//
// The workspace server runs on a temporary HOME with
// MYFLAMES_SECRET_BACKEND=vault (never ~/.myflames or the keychain). A
// Development connection to each server is saved through the app's API, and
// headless Chrome drives the wizard: CSV files written to a temporary folder
// are opened with "Type a path instead", every step runs to Import, and the
// table is read back. Scenarios per server: an existing table with INSERT
// batches (comma), a new table (semicolon), a .tsv file and a Latin-1 file
// from Server > Tables (row menu and inspector), Skip and report with the
// problem lines shown, all or nothing with a row the server refuses (table
// unchanged), Commit every N rows, LOAD DATA LOCAL with local_infile ON and
// off with its reason when OFF, and Stop on a large file (keep nothing, and
// commit every N). Then the Audit log (API and screen, no row data) and a
// Read-only connection (every entry point off with its reason).
//
// Run after `npm run build`, with Docker running and the mysql client on PATH:
//   node test/import-csv-live.mjs
// Environment: IMPORT_CSV_LIVE_SHOTS (screenshot folder, outside the
// repository; failures are FAIL-*.png), IMPORT_CSV_LIVE_REPORT (results as
// JSON), IMPORT_CSV_LIVE_ENGINES (default "mysql,mariadb"),
// IMPORT_CSV_LIVE_MYSQL_IMAGE (default mysql:8.4) and
// IMPORT_CSV_LIVE_MARIADB_IMAGE (default mariadb:11.4), IMPORT_CSV_LIVE_BIG_ROWS
// (rows of the Stop file, default 600000).
import { execFileSync, spawn } from "node:child_process";
import crypto from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { chooseConnection, chooseView } from "./chrome-helpers.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const TOKEN = crypto.randomBytes(3).toString("hex");
const PREFIX = `myflames-e2e-${TOKEN}-`;
const ENGINES = (process.env.IMPORT_CSV_LIVE_ENGINES || "mysql,mariadb").split(",").map((s) => s.trim()).filter(Boolean);
const IMAGES = { mysql: process.env.IMPORT_CSV_LIVE_MYSQL_IMAGE || "mysql:8.4", mariadb: process.env.IMPORT_CSV_LIVE_MARIADB_IMAGE || "mariadb:11.4" };
const BIG_ROWS = Number(process.env.IMPORT_CSV_LIVE_BIG_ROWS || 600000);
const work = mkdtempSync(path.join(os.tmpdir(), "myflames-csv-live-"));
const home = path.join(work, "home");
const files = path.join(work, "files");
// Screenshots outlive the run (the temporary folder with the files and HOME does not).
const shots = process.env.IMPORT_CSV_LIVE_SHOTS || mkdtempSync(path.join(os.tmpdir(), "myflames-csv-live-shots-"));
for (const d of [home, files, shots]) mkdirSync(d, { recursive: true });
/** In every imported value: the Audit log must never contain it. */
const MARK = "ZQXE2E";
const SHEET = "dialog.import-csv-sheet";
const log = (text) => process.stderr.write(`[${new Date().toTimeString().slice(0, 8)}] ${text}\n`);

// ---- Docker: throwaway servers only ------------------------------------------------------

function run(cmd, args, { input, timeout = 120_000 } = {}) {
  return execFileSync(cmd, args, { input, timeout, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], maxBuffer: 256 << 20 });
}
function tryRun(cmd, args, options) {
  try { return run(cmd, args, options); } catch { return null; }
}
const servers = [];
const pulled = [];
let cleaned = false;
function cleanupDocker() {
  if (cleaned) return;
  cleaned = true;
  for (const s of servers) tryRun("docker", ["rm", "-f", "-v", s.name], { timeout: 120_000 });
  for (const image of pulled) tryRun("docker", ["rmi", image], { timeout: 300_000 });
}
/** The client inside the container; the password stays in its environment. */
const CLIENT = 'MYSQL_PWD="${MYSQL_ROOT_PASSWORD:-$MARIADB_ROOT_PASSWORD}"; export MYSQL_PWD; '
  + 'c=$(command -v mariadb || command -v mysql); exec "$c" -uroot -h127.0.0.1 -N -B --default-character-set=utf8mb4';
function sql(server, query) {
  const out = run("docker", ["exec", "-i", server.name, "sh", "-c", CLIENT], { input: query, timeout: 300_000 });
  return out.split("\n").filter((l) => l.length).map((l) => l.split("\t"));
}
const one = (server, query) => sql(server, query)[0]?.[0];
function startServer(engine) {
  const image = IMAGES[engine];
  if (tryRun("docker", ["image", "inspect", image]) === null) {
    log(`pulling ${image}`);
    run("docker", ["pull", "-q", image], { timeout: 1_800_000 });
    pulled.push(image);
  }
  const name = PREFIX + (engine === "mysql" ? "mysql84" : "mariadb114");
  const server = { engine, image, name, password: crypto.randomBytes(18).toString("base64url"), port: 0 };
  const envFile = path.join(work, `${name}.env`);
  writeFileSync(envFile, `${engine === "mysql" ? "MYSQL_ROOT_PASSWORD" : "MARIADB_ROOT_PASSWORD"}=${server.password}\n`, { mode: 0o600 });
  servers.push(server);
  try {
    run("docker", ["run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::3306", "--env-file", envFile, image, "--local-infile=1"], { timeout: 300_000 });
  } finally {
    rmSync(envFile, { force: true });
  }
  server.port = Number(run("docker", ["port", name, "3306/tcp"]).trim().split("\n")[0].split(":").pop());
  return server;
}
async function waitReady(server) {
  for (let i = 0; i < 150; i++) {
    try {
      const [version, infile] = sql(server, "SELECT @@version, @@GLOBAL.local_infile")[0];
      Object.assign(server, { version, localInfile: infile });
      return;
    } catch { await pause(2000); }
  }
  throw new Error(`${server.name} did not accept connections`);
}

// ---- Tables and files ------------------------------------------------------------------

const EXISTING = ["orders_comma", "orders_tsv", "orders_latin1", "orders_skip", "orders_dup", "orders_every", "orders_load", "orders_load_off", "orders_big"];
const SETUP = ["CREATE DATABASE e2e CHARACTER SET utf8mb4;",
  ...EXISTING.map((t) => `CREATE TABLE e2e.${t} (order_id INT UNSIGNED NOT NULL PRIMARY KEY, customer VARCHAR(40) NOT NULL, `
    + "amount DECIMAL(10,2) NULL, note VARCHAR(200) NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"),
  "INSERT INTO e2e.orders_skip (order_id, customer, amount, note) VALUES (5, 'seed', 5.00, 'seed');",
  "INSERT INTO e2e.orders_dup (order_id, customer, amount, note) VALUES (250, 'seed', 2.50, 'seed');",
  "INSERT INTO e2e.orders_every (order_id, customer, amount, note) VALUES (250, 'seed', 2.50, 'seed');"].join("\n");

const money = (cents) => `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
function write(name, text, encode = (t) => Buffer.from(t, "utf8")) {
  const file = path.join(files, name);
  writeFileSync(file, encode(text));
  return file;
}
/** Windows-1252 bytes: Latin-1 for U+0000..U+00FF, 0x80 for the euro sign. */
const cp1252 = (text) => Buffer.from([...text].map((ch) => (ch === "€" ? 0x80 : ch.codePointAt(0))));
function orderLines(count, sep = ",") {
  const lines = [["order_id", "customer", "amount", "note"].join(sep)];
  let cents = 0;
  for (let i = 1; i <= count; i++) {
    const c = (i * 37) % 100000;
    cents += c;
    lines.push([i, `Buyer ${i} ${MARK}`, money(c), `row ${i}`].join(sep));
  }
  return { text: lines.join("\n") + "\n", cents };
}
const FILES = {};
const EXPECT = {};
function writeFiles() {
  FILES.comma = write("orders_comma.csv", [
    "order_id,customer,amount,note",
    `1,Ana ${MARK},10.50,first order`,
    `2,"Lima, Bruno ${MARK}",7.00,`,
    `3,Carla ${MARK},12.25,"said ""hi"""`,
    `4,Davi ${MARK},0.99,multi`,
    `5,Eva ${MARK},99.90,gift`].join("\n") + "\n");
  EXPECT.comma = [["1", `Ana ${MARK}`, "10.50", "first order"], ["2", `Lima, Bruno ${MARK}`, "7.00", "NULL"],
    ["3", `Carla ${MARK}`, "12.25", 'said "hi"'], ["4", `Davi ${MARK}`, "0.99", "multi"], ["5", `Eva ${MARK}`, "99.90", "gift"]];
  const people = ["name;age;joined;score"];
  let ages = 0, scoreCents = 0;
  for (let i = 1; i <= 1500; i++) {
    const age = 20 + (i % 50), score = (i % 100) * 100 + ((i * 7) % 100);
    ages += age;
    scoreCents += score;
    people.push(`Person ${i} ${MARK};${age};2024-${String(1 + (i % 12)).padStart(2, "0")}-${String(1 + (i % 28)).padStart(2, "0")};${money(score)}`);
  }
  FILES.semicolon = write("people_semicolon.csv", people.join("\n") + "\n");
  EXPECT.semicolon = { rows: 1500, ages, score: money(scoreCents) };
  FILES.tsv = write("orders_tab.tsv", ["order_id\tcustomer\tamount\tnote", `1\tTab One ${MARK}\t1.25\thas, comma`,
    `2\tTab Two ${MARK}\t2.50\t`, `3\tTab Three ${MARK}\t3.75\tsemi; colon`].join("\n") + "\n");
  EXPECT.tsv = [["1", `Tab One ${MARK}`, "1.25", "has, comma"], ["2", `Tab Two ${MARK}`, "2.50", "NULL"], ["3", `Tab Three ${MARK}`, "3.75", "semi; colon"]];
  const latin = [["1", `José Ñandú ${MARK}`, "1.00", "café crème"], ["2", `Ação Müller ${MARK}`, "2.00", "price € 5"],
    ["3", `Øyvind Æsir ${MARK}`, "3.00", "naïve façade"]];
  FILES.latin1 = write("clientes_latin1.csv", ["order_id;customer;amount;note", ...latin.map((r) => r.join(";"))].join("\r\n") + "\r\n", cp1252);
  EXPECT.latin1 = latin;
  FILES.bad = write("orders_bad.csv", ["order_id,customer,amount,note", `1,A ${MARK},1.00,ok`, `2,B ${MARK},abc,bad amount`,
    `3,C ${MARK},3.00,ok`, `5,E ${MARK},5.00,duplicate key`, "6,,6.00,no customer", `7,G ${MARK},7.00,ok`].join("\n") + "\n");
  const dup = orderLines(1500);
  FILES.dup = write("orders_dup.csv", dup.text);
  FILES.every = write("orders_every.csv", dup.text);
  const load = orderLines(2000);
  FILES.load = write("orders_load.csv", load.text);
  EXPECT.load = { rows: 2000, amount: money(load.cents) };
  FILES.loadOff = write("orders_load_off.csv", orderLines(3).text);
  FILES.big = write("orders_big.csv", orderLines(BIG_ROWS).text);
}

// ---- The workspace server and the browser --------------------------------------------------

let proc, browser, page;
const errors = [];
const net = [];
const requests = new Map();
async function startWorkspace() {
  const env = { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: "vault" };
  delete env.MYFLAMES_VAULT_PASSPHRASE;
  proc = spawn(process.env.PYTHON || "python3",
    ["-c", "import sys; import myflames.ui as u; s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
      path.join(home, "workspace.sqlite3")], { cwd: root, env });
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error("the workspace did not start")), 20000);
    proc.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    proc.on("error", reject);
  });
  browser = await puppeteer.launch({ headless: "shell" });
  page = await browser.newPage();
  page.on("pageerror", (e) => errors.push(String(e)));
  // Observed, never answered here: every /api/import/csv/* request and its reply.
  page.on("request", (r) => {
    const p = new URL(r.url()).pathname;
    if (!p.startsWith("/api/import/csv/")) return;
    const rec = { path: p.slice("/api/import/csv/".length), req: JSON.parse(r.postData() || "{}"), done: false };
    requests.set(r, rec);
    net.push(rec);
  });
  page.on("response", async (r) => {
    const rec = requests.get(r.request());
    if (!rec) return;
    rec.status = r.status();
    try { rec.body = await r.json(); } catch { rec.body = null; }
    rec.done = true;
  });
  page.on("requestfailed", (r) => {
    const rec = requests.get(r);
    if (rec) Object.assign(rec, { done: true, failed: true });
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
}
/** The app's own API, as the page calls it (session token from the launch proof). */
function api(pathname, body) {
  return page.evaluate(async (p, b) => {
    const headers = { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" };
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers, body: "{}" })).json()).token;
    const reply = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    const json = await reply.json();
    if (!reply.ok) throw new Error(json.error || `HTTP ${reply.status}`);
    return json;
  }, pathname, body);
}

// ---- Wizard helpers ----------------------------------------------------------------------

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
async function waitText(selector, re, timeout = 30000) {
  await page.waitForFunction((s, src) => new RegExp(src).test(document.querySelector(s)?.textContent || ""), { timeout }, selector, re.source);
}
const sheetText = () => page.$eval(SHEET, (e) => e.textContent);
const stepIs = (name) => waitText(`${SHEET} .import-csv-steps [aria-current=step] .import-csv-step-label`, new RegExp(`^${escapeRe(name)}$`), 60000);
const idle = (timeout = 300000) => page.waitForFunction((s) => !document.querySelector(`${s} .import-csv-line[data-phase=pending]`), { timeout }, SHEET);
async function clickWhenEnabled(text, timeout = 60000) {
  await page.waitForFunction((s, t) => {
    const b = [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === t);
    return b && !b.disabled && b.getAttribute("aria-disabled") !== "true";
  }, { timeout }, SHEET, text);
  await page.evaluate((s, t) => [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === t).click(), SHEET, text);
}
const plans = (from) => net.slice(from).filter((r) => r.path === "plan");
/** Wait for the check (plan) started at or after net index *from* that
 * matches *pred* to finish, with no newer one started, then the sheet idle. */
async function settlePlan(from, pred = () => true, timeout = 300000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const list = plans(from);
    const last = list[list.length - 1];
    if (last && last.done && !last.failed && pred(last.req)) {
      await pause(450);
      const again = plans(from);
      if (again[again.length - 1] === last) {
        await idle();
        if (last.status !== 200) throw new Error(`plan answered ${last.status}: ${JSON.stringify(last.body).slice(0, 400)}`);
        return last.body;
      }
    }
    if (Date.now() > deadline) throw new Error(`no finished check (${list.length} started)`);
    await pause(60);
  }
}
async function openFile(file) {
  await page.locator(`${SHEET} button ::-p-text(Type a path instead)`).click();
  await page.waitForSelector(`${SHEET} .import-csv-path input`);
  // Typed, as a person would (a value set from script would skip React's onChange).
  await page.type(`${SHEET} .import-csv-path input`, file);
  await page.keyboard.press("Enter");
  await waitText(`${SHEET} .import-csv-file-name strong`, new RegExp(`^${escapeRe(path.basename(file))}$`), 60000);
  await page.waitForSelector(`${SHEET} [aria-label="Preview of the first rows"]`, { timeout: 180000 });
  await idle();
  return page.evaluate((s) => ({
    separator: document.querySelector(`${s} .segmented[aria-label="Separator"] [aria-pressed=true]`)?.textContent,
    detected: [...document.querySelectorAll(`${s} .import-csv-detected`)].map((e) => e.textContent),
    encoding: document.querySelector(`${s} .import-csv-grid-fields label.import-csv-field select`)?.value,
    counts: document.querySelector(`${s} .import-csv-counts`)?.textContent,
    headers: [...document.querySelectorAll(`${s} [aria-label="Preview of the first rows"] thead th`)].map((e) => e.querySelector(".import-csv-cell")?.textContent ?? e.textContent),
    firstRow: [...document.querySelectorAll(`${s} [aria-label="Preview of the first rows"] tbody tr:first-child td`)].map((e) => e.textContent),
  }), SHEET);
}
async function toTarget(kind) {
  await clickWhenEnabled("Next");
  await stepIs("Target and columns");
  if (kind === "existing") {
    await page.waitForSelector(`${SHEET} [aria-label="CSV columns and the table columns they fill"] select`, { timeout: 60000 });
    await page.waitForFunction((s) => {
      const b = [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === "Next");
      return b && b.getAttribute("aria-disabled") !== "true";
    }, { timeout: 60000 }, SHEET);
    return page.$$eval(`${SHEET} [aria-label="CSV columns and the table columns they fill"] tbody select`, (els) => els.map((e) => e.value));
  }
  await page.waitForSelector(`${SHEET} [aria-label="Columns of the new table"]`, { timeout: 60000 });
  return page.evaluate((s) => ({
    name: [...document.querySelectorAll(`${s} label.import-csv-field`)].find((l) => /New table name/.test(l.textContent))?.querySelector("input")?.value,
    columns: [...document.querySelectorAll(`${s} [aria-label="Columns of the new table"] tbody input[aria-label^="Name of"]`)].map((e) => e.value),
    types: [...document.querySelectorAll(`${s} [aria-label="Columns of the new table"] tbody select`)].map((e) => e.value),
    idKey: document.querySelector(`${s} .import-csv-columns .checkbox input`)?.checked,
  }), SHEET);
}
async function toCheck() {
  const from = net.length;
  await clickWhenEnabled("Next");
  await stepIs("Check");
  const plan = await settlePlan(from);
  await page.waitForSelector(`${SHEET} .import-csv-summary`, { timeout: 30000 });
  return plan;
}
async function choose(group, value, pred) {
  const from = net.length;
  await page.click(`${SHEET} .segmented[aria-label="${group}"] [data-value="${value}"]`);
  return settlePlan(from, pred);
}
async function commitEvery(n) {
  await choose("If something fails", "every", (r) => r.transaction?.mode === "every");
  const from = net.length;
  await page.$eval(`${SHEET} input[aria-label="Rows per commit"]`, (e) => e.select());
  await page.keyboard.type(String(n));
  return settlePlan(from, (r) => r.transaction?.mode === "every" && r.transaction.every === n);
}
const checkView = () => page.evaluate((s) => ({
  summary: document.querySelector(`${s} .import-csv-summary`)?.textContent || "",
  blockers: document.querySelector(`${s} .import-csv-blockers`)?.textContent || "",
  problems: [...document.querySelectorAll(`${s} [aria-label="Problems by line"] tbody tr`)].map((tr) => [...tr.children].map((td) => td.textContent)),
  review: (() => { const b = [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === "Review…"); return b ? b.getAttribute("aria-disabled") !== "true" : null; })(),
  loadData: (() => { const b = document.querySelector(`${s} .segmented[aria-label="Method"] [data-value=load_data]`); return b ? { disabled: b.disabled, title: b.title } : null; })(),
  methodHint: [...document.querySelectorAll(`${s} .import-csv-field`)].find((f) => /^Method/.test(f.textContent))?.querySelector(".hint")?.textContent || "",
}), SHEET);
async function meter() {
  return page.$eval(`${SHEET} .import-csv-meter span`, (e) => e.textContent).catch(() => "");
}
/** Review, then Import; *during* runs while the import is in flight. Every
 * import sent is recorded for the Audit log check, whatever its outcome. */
async function reviewAndImport(engine, file, table, during) {
  await clickWhenEnabled("Review…");
  await stepIs("Review");
  await page.waitForFunction(() => document.activeElement?.textContent === "Cancel", { timeout: 10000 });
  const review = {
    text: await sheetText(),
    eyebrow: await page.$eval(`${SHEET} .eyebrow`, (e) => e.textContent),
    band: await page.$eval(`${SHEET} .connection-type-band`, (e) => e.textContent).catch(() => ""),
    role: await page.$eval(SHEET, (e) => e.getAttribute("role")),
  };
  const from = net.length;
  await clickWhenEnabled("Import");
  await stepIs("Import");
  const extra = during ? await during() : null;
  await page.waitForSelector(`${SHEET} .import-csv-outcome`, { timeout: 900000 });
  const outcome = await page.$eval(`${SHEET} .import-csv-outcome`, (e) => ({ kind: e.dataset.outcome, text: e.textContent }));
  const apply = net.slice(from).find((r) => r.path === "apply");
  for (let i = 0; i < 100 && apply && !apply.done; i++) await pause(50);
  imports.push({ engine, file: path.basename(file), table, outcome: apply?.body?.outcome ?? outcome.kind, committed: apply?.body?.rows?.committed, audit: apply?.body?.audit?.id });
  return { review, outcome, result: apply?.body, status: apply?.status, applies: net.slice(from).filter((r) => r.path === "apply").length, extra };
}
async function stopAt(minSent, minCommitted = 0) {
  await page.waitForFunction((s, a, b) => {
    const t = document.querySelector(`${s} .import-csv-meter span`)?.textContent || "";
    const sent = Number((t.match(/^([\d,]+) of/) || [0, "0"])[1].replace(/,/g, ""));
    const com = Number((t.match(/· ([\d,]+) committed/) || [0, "0"])[1].replace(/,/g, ""));
    return sent >= a && com >= b;
  }, { timeout: 600000, polling: 50 }, SHEET, minSent, minCommitted);
  const at = await meter();
  const from = net.length;
  await page.evaluate((s) => [...document.querySelectorAll(`${s} .sheet-actions button`)].find((e) => e.textContent.trim() === "Stop").click(), SHEET);
  for (let i = 0; i < 100 && !net.slice(from).some((r) => r.path === "stop" && r.done); i++) await pause(50);
  return { at, stops: net.slice(from).filter((r) => r.path === "stop").length, stopping: await page.$eval(`${SHEET} .sheet-actions`, (e) => e.textContent) };
}
async function closeResult() {
  if (await page.$(`${SHEET}[open]`)) {
    const primary = await page.$(`${SHEET} .sheet-actions button.primary`);
    if (primary) await primary.click(); else await page.keyboard.press("Escape");
    await page.waitForFunction((s) => !document.querySelector(`${s}[open]`), { timeout: 10000 }, SHEET).catch(() => {});
  }
  await dismissStatus();
}
async function dismissStatus() {
  await page.evaluate(() => {
    for (const b of document.querySelectorAll(".import-csv-status button")) if (b.textContent === "Dismiss" && b.offsetParent) b.click();
  });
  await pause(150);
}
async function recover() {
  for (let i = 0; i < 4 && (await page.$(`${SHEET}[open]`)); i++) { await page.keyboard.press("Escape"); await pause(400); }
  if (await page.$(".context-menu[role=menu]")) await page.keyboard.press("Escape");
  await dismissStatus();
}

// ---- Entry points ------------------------------------------------------------------------

async function editorPlace() {
  if (!(await page.$(".main-nav button[data-place=query].active"))) await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab", { visible: true });
}
async function loadSchemas() {
  await editorPlace();
  if (await page.$('button[aria-label="Show the schema browser"]')) await page.click('button[aria-label="Show the schema browser"]');
  await page.waitForSelector(".schema-browser", { visible: true });
  // The visible panel reads the chosen connection's list by itself (no Load
  // schemas gate); every caller has just chosen a connection.
  const e2e = '.schema-browser .tree-row.kind-schema[data-node="s:e2e"]';
  await page.waitForSelector(e2e, { timeout: 60000 });
  await pause(300);
  if (await page.$eval(e2e, (e) => e.getAttribute("aria-expanded")) !== "true") await page.click(e2e);
  await page.waitForFunction(() => [...document.querySelectorAll(".schema-browser .tree-row.kind-table")].some((e) => e.dataset.node === "t:e2e\u001forders_comma"), { timeout: 60000 });
}
async function treeMenu(kind, name) {
  const handle = await page.evaluateHandle((k, n) => (k === "schema"
    ? document.querySelector(`.schema-browser .tree-row.kind-schema[data-node="s:${n}"]`)
    : [...document.querySelectorAll(".schema-browser .tree-row.kind-table")].find((e) => e.dataset.node === `t:e2e\u001f${n}`)), kind, name);
  const element = handle.asElement();
  if (!element) throw new Error(`no ${kind} ${name} in the Schemas panel`);
  await element.evaluate((e) => e.scrollIntoView({ block: "center" }));
  await element.click({ button: "right" });
  await page.waitForSelector(".schema-browser .context-menu[role=menu]");
}
const menuItem = (scope, text) => page.evaluate((sc, t) => {
  const e = [...document.querySelectorAll(`${sc} [role=menuitem]`)].find((x) => x.textContent.startsWith(t));
  return e ? { disabled: e.getAttribute("aria-disabled") === "true" || e.disabled, title: e.title, text: e.textContent } : null;
}, scope, text);
async function clickMenuItem(scope, text) {
  await page.evaluate((sc, t) => [...document.querySelectorAll(`${sc} [role=menuitem]`)].find((x) => x.textContent.startsWith(t)).click(), scope, text);
}
async function fromSchemasTable(table) {
  await treeMenu("table", table);
  const item = await menuItem(".schema-browser .context-menu", "Import CSV…");
  if (!item || item.disabled) throw new Error(`Import CSV… is not offered on ${table}: ${JSON.stringify(item)}`);
  await clickMenuItem(".schema-browser .context-menu", "Import CSV…");
  await page.waitForSelector(`${SHEET}[open]`);
}
async function fromSchemasDatabase(schema) {
  await treeMenu("schema", schema);
  const item = await menuItem(".schema-browser .context-menu", "Import CSV into a new table…");
  if (!item || item.disabled) throw new Error(`Import CSV into a new table… is not offered on ${schema}: ${JSON.stringify(item)}`);
  await clickMenuItem(".schema-browser .context-menu", "Import CSV into a new table…");
  await page.waitForSelector(`${SHEET}[open]`);
}
async function tablesView(refresh = false) {
  if (!(await page.$(".main-nav button[data-place=server].active"))) await page.locator(".main-nav button ::-p-text(Server)").click();
  await chooseView(page, "tables");
  if (refresh) {
    await page.evaluate(() => {
      for (const b of document.querySelectorAll("[data-action=refresh]")) if (!b.closest("[hidden]") && b.offsetParent) { b.click(); return; }
    });
  }
}
const tablesRow = (table) => `.server-view[data-view="tables"] tr[data-table="e2e.${table}"]`;
async function tablesRowMenu(table) {
  await page.waitForSelector(tablesRow(table), { visible: true, timeout: 120000 });
  await page.$eval(tablesRow(table), (e) => e.scrollIntoView({ block: "center" }));
  await page.click(`${tablesRow(table)} td:nth-child(2)`, { button: "right" });
  await page.waitForSelector(".context-menu[role=menu][aria-label='Row actions']");
}
async function tablesInspector(table) {
  const back = await page.$(".view-inspector.is-detail .detail-back");
  if (back) await back.click();
  await page.waitForSelector(tablesRow(table), { visible: true, timeout: 120000 });
  const target = (await page.$(`${tablesRow(table)} [data-row-key]`)) ? `${tablesRow(table)} [data-row-key]` : tablesRow(table);
  await page.$eval(target, (e) => e.scrollIntoView({ block: "center" }));
  await page.locator(target).click();
  await page.waitForSelector(".view-inspector [data-action=import-csv]", { timeout: 30000 });
}

// ---- Scenarios ---------------------------------------------------------------------------

const results = [];
const imports = [];
const skipShown = {};
function check(condition, message) {
  if (!condition) throw new Error(message);
}
async function scenario(engine, name, fn) {
  const started = Date.now();
  log(`${engine}: ${name}`);
  try {
    const detail = await fn();
    results.push({ engine, name, pass: true, seconds: Math.round((Date.now() - started) / 100) / 10, detail });
    log(`  PASS ${JSON.stringify(detail).slice(0, 600)}`);
  } catch (error) {
    const shot = path.join(shots, `FAIL-${engine}-${name.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}.png`);
    await page.screenshot({ path: shot }).catch(() => {});
    results.push({ engine, name, pass: false, seconds: Math.round((Date.now() - started) / 100) / 10, error: String(error?.message || error).slice(0, 3000), shot });
    log(`  FAIL ${String(error?.message || error).slice(0, 1500)}`);
    await recover().catch(() => {});
  }
}
const shot = (name) => page.screenshot({ path: path.join(shots, `${name}.png`) }).catch(() => {});
const rowsOf = (server, table) => sql(server, `SELECT order_id, customer, amount, IFNULL(note, 'NULL') FROM e2e.${table} ORDER BY order_id`);
const count = (server, table) => Number(one(server, `SELECT COUNT(*) FROM e2e.${table}`));

async function engineScenarios(server, profile) {
  const E = server.engine;
  await editorPlace();
  await chooseConnection(page, profile);
  await loadSchemas();

  await scenario(E, "existing table, INSERT batches, comma file, from the Schemas table menu", async () => {
    await fromSchemasTable("orders_comma");
    const h2 = await page.$eval(`${SHEET} h2`, (e) => e.textContent);
    check(h2 === "Import CSV into e2e.orders_comma", `title ${h2}`);
    const band = await page.$eval(`${SHEET} .connection-type-band`, (e) => e.textContent);
    check(/Development/.test(band), `band ${band}`);
    const file = await openFile(FILES.comma);
    check(file.separator === "Comma", `separator ${file.separator}`);
    check(file.detected.some((d) => /Detected: comma/.test(d)), `detected ${file.detected}`);
    check(/5 rows · 4 columns/.test(file.counts), `counts ${file.counts}`);
    const mapping = await toTarget("existing");
    check(JSON.stringify(mapping) === JSON.stringify(["order_id", "customer", "amount", "note"]), `mapping ${mapping}`);
    const plan = await toCheck();
    const view = await checkView();
    check(plan.ready && plan.token, `plan not ready: ${plan.blockers}`);
    check(/5 rows · 5 ready · 1 statement/.test(view.summary), `summary ${view.summary}`);
    check(view.review === true, "Review… is off");
    const run = await reviewAndImport(E, FILES.comma, "orders_comma");
    check(run.review.role === "alertdialog", `review role ${run.review.role}`);
    check(run.review.eyebrow === "INSERT", `eyebrow ${run.review.eyebrow}`);
    check(/Development/.test(run.review.band), `review band ${run.review.band}`);
    check(/INSERT INTO `e2e`\.`orders_comma` \(`order_id`, `customer`, `amount`, `note`\) VALUES \.\.\. : 5 rows in 1 statement/.test(run.review.text), "data summary in the review");
    check(run.outcome.kind === "done", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    check(run.applies === 1, `apply sent ${run.applies} times`);
    check(/Rows committed5/.test(run.outcome.text) && /The table now has5 rows/.test(run.outcome.text), `result ${run.outcome.text}`);
    await shot(`${E}-existing-comma-result`);
    await closeResult();
    const back = rowsOf(server, "orders_comma");
    check(JSON.stringify(back) === JSON.stringify(EXPECT.comma), `read back ${JSON.stringify(back)}`);
    return { committed: run.result.rows.committed, readBack: back.length, statements: plan.batches, outcome: run.outcome.text.slice(0, 120) };
  });

  await scenario(E, "new table from a semicolon file, from the Schemas database menu", async () => {
    await fromSchemasDatabase("e2e");
    check(await page.$eval(`${SHEET} h2`, (e) => e.textContent) === "Import CSV into a new table in e2e", "title before the file");
    const file = await openFile(FILES.semicolon);
    check(file.separator === "Semicolon" && file.detected.some((d) => /Detected: semicolon/.test(d)), `separator ${file.separator} ${file.detected}`);
    check(/1,500 rows · 4 columns/.test(file.counts), `counts ${file.counts}`);
    const target = await toTarget("new");
    check(target.name === "people_semicolon", `name ${target.name}`);
    check(JSON.stringify(target.columns) === JSON.stringify(["name", "age", "joined", "score"]), `columns ${target.columns}`);
    check(target.idKey === true, "Add an id primary key is on");
    const plan = await toCheck();
    check(plan.ready, `blockers ${plan.blockers}`);
    check(/^(\/\*myflames:import_csv\*\/ )?CREATE TABLE `e2e`\.`people_semicolon`/.test(plan.statements.create || ""), `CREATE TABLE planned: ${plan.statements.create}`);
    const run = await reviewAndImport(E, FILES.semicolon, "people_semicolon");
    check(run.review.eyebrow === "CREATE TABLE · INSERT", `eyebrow ${run.review.eyebrow}`);
    check(/CREATE TABLE, sent first on its own/.test(run.review.text) && /DROP TABLE `e2e`\.`people_semicolon`;/.test(run.review.text), "CREATE and DROP in the review");
    check(run.outcome.kind === "done", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    await shot(`${E}-new-table-result`);
    await closeResult();
    const [rows, ages, score] = sql(server, "SELECT COUNT(*), SUM(age), SUM(score) FROM e2e.people_semicolon")[0];
    const types = sql(server, "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = 'e2e' AND TABLE_NAME = 'people_semicolon' ORDER BY ORDINAL_POSITION");
    check(Number(rows) === EXPECT.semicolon.rows && Number(ages) === EXPECT.semicolon.ages && score === EXPECT.semicolon.score,
      `read back ${rows} rows, ages ${ages} (want ${EXPECT.semicolon.ages}), score ${score} (want ${EXPECT.semicolon.score})`);
    return { committed: run.result.rows.committed, readBack: Number(rows), statements: plan.batches, types: types.map((t) => t.join(" ")).join(", "), inferred: target.types };
  });

  await scenario(E, "Skip and report: problem lines shown, the rest imported", async () => {
    await fromSchemasTable("orders_skip");
    await openFile(FILES.bad);
    await toTarget("existing");
    const plan = await toCheck();
    let view = await checkView();
    check(!plan.ready && plan.token === null, "a bad row with Stop gives no token");
    check(view.review === false, "Review… stays off while bad rows stop the import");
    check(/2 rows have problems|2 with problems/.test(view.blockers + view.summary), `blockers ${view.blockers} summary ${view.summary}`);
    const lines = view.problems.map((r) => r[0]);
    check(JSON.stringify(lines) === JSON.stringify(["3", "6"]), `problem lines ${lines}`);
    await shot(`${E}-skip-check-blocked`);
    const skip = await choose("Bad rows", "skip", (r) => r.on_error === "skip");
    view = await checkView();
    check(skip.ready && view.review === true, "Skip and report makes it ready");
    const run = await reviewAndImport(E, FILES.bad, "orders_skip");
    check(run.outcome.kind === "done", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    const r = run.result;
    // What the result on screen says about the skipped rows (checked in the next scenario).
    skipShown[E] = { api: r.problems, text: run.outcome.text,
      lines: await page.$$eval(`${SHEET} .import-csv-outcome tbody tr`, (els) => els.map((e) => e.textContent)) };
    await shot(`${E}-skip-result`);
    const apiLines = (r.problems || []).map((p) => p.line).sort((a, b) => a - b);
    check(r.rows.committed === 3 && r.rows.skipped === 3 && JSON.stringify(apiLines) === JSON.stringify([3, 5, 6]), `rows ${JSON.stringify(r.rows)} problem lines ${apiLines}`);
    await closeResult();
    const n = count(server, "orders_skip");
    check(n === 4, `table rows ${n} (want 4: seed + 3)`);
    const ids = sql(server, "SELECT GROUP_CONCAT(order_id ORDER BY order_id) FROM e2e.orders_skip")[0][0];
    check(ids === "1,3,5,7", `ids ${ids}`);
    return { committed: r.rows.committed, skipped: r.rows.skipped, readBack: n, ids, problemLines: apiLines };
  });

  await scenario(E, "Skip and report: the result lists the skipped lines", async () => {
    const seen = skipShown[E];
    check(seen, "the Skip and report import did not finish");
    const lines = (seen.api || []).map((p) => p.line);
    const listed = lines.filter((line) => seen.lines.some((t) => t.startsWith(String(line))) || new RegExp(`\\bLines? ${line}\\b`).test(seen.text));
    check(listed.length === lines.length, `the result lists lines [${listed}] of the skipped lines [${lines}] (the reply's problems: ${JSON.stringify(seen.api)}); `
      + `the result reads: ${seen.text} (screenshot ${E}-skip-result.png)`);
    return { lines };
  });

  await scenario(E, "all or nothing, the server refuses a row: table unchanged", async () => {
    const before = count(server, "orders_dup");
    await fromSchemasTable("orders_dup");
    await openFile(FILES.dup);
    await toTarget("existing");
    const plan = await toCheck();
    check(plan.ready && plan.batches === 2, `plan ready ${plan.ready}, batches ${plan.batches}`);
    const run = await reviewAndImport(E, FILES.dup, "orders_dup");
    check(run.outcome.kind === "failed", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    check(/Duplicate entry '250'/.test(run.outcome.text) && /1062/.test(run.outcome.text), `server error not shown: ${run.outcome.text}`);
    await shot(`${E}-all-or-nothing-failed`);
    await closeResult();
    const after = count(server, "orders_dup");
    const seed = sql(server, "SELECT customer FROM e2e.orders_dup WHERE order_id = 250")[0][0];
    check(after === before && after === 1 && seed === "seed", `table rows ${before} -> ${after}, seed ${seed}`);
    return { committed: run.result.rows.committed, failedAt: run.result.failed_at, readBack: after, message: run.result.message };
  });

  await scenario(E, "Commit every 100 rows, the server refuses row 250: 200 kept", async () => {
    await fromSchemasTable("orders_every");
    await openFile(FILES.every);
    await toTarget("existing");
    await toCheck();
    const plan = await commitEvery(100);
    check(plan.ready && plan.transaction.mode === "every" && plan.transaction.every === 100, `plan ${JSON.stringify(plan.transaction)}`);
    const run = await reviewAndImport(E, FILES.every, "orders_every");
    check(/Commit every 100 rows/.test(run.review.text), "the review names Commit every 100 rows");
    check(run.outcome.kind === "failed", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    const r = run.result;
    check(r.rows.committed === 200 && r.next_line === 202, `committed ${r.rows.committed}, next line ${r.next_line}`);
    check(/Rows committed200/.test(run.outcome.text) && /First line not committedLine 202/.test(run.outcome.text), `result ${run.outcome.text}`);
    await shot(`${E}-commit-every-failed`);
    await closeResult();
    const n = count(server, "orders_every");
    check(n === 201, `table rows ${n} (want 201: seed + 200)`);
    return { committed: r.rows.committed, nextLine: r.next_line, readBack: n, commits: plan.transaction.commits };
  });

  await scenario(E, "LOAD DATA LOCAL offered and working with local_infile ON", async () => {
    check(one(server, "SELECT @@GLOBAL.local_infile") === "1", "local_infile is ON");
    await fromSchemasTable("orders_load");
    await openFile(FILES.load);
    await toTarget("existing");
    await toCheck();
    let view = await checkView();
    check(view.loadData && view.loadData.disabled === false, `LOAD DATA LOCAL ${JSON.stringify(view.loadData)}`);
    const plan = await choose("Method", "load_data", (r) => r.method === "load_data");
    view = await checkView();
    check(plan.ready && plan.method.chosen === "load_data", `plan ${JSON.stringify(plan.method)}`);
    check(/Faster: the server reads a checked copy of the rows, in \d[\d,]* statements?\./.test(view.methodHint), `hint ${view.methodHint}`);
    const run = await reviewAndImport(E, FILES.load, "orders_load");
    check(run.review.eyebrow === "LOAD DATA LOCAL INFILE", `eyebrow ${run.review.eyebrow}`);
    check(run.outcome.kind === "done", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    await closeResult();
    const [rows, amount] = sql(server, "SELECT COUNT(*), SUM(amount) FROM e2e.orders_load")[0];
    check(Number(rows) === EXPECT.load.rows && amount === EXPECT.load.amount, `read back ${rows} rows, sum ${amount} (want ${EXPECT.load.amount})`);
    return { committed: run.result.rows.committed, readBack: Number(rows), sum: amount, warnings: run.result.warnings?.count };
  });

  await scenario(E, "LOAD DATA LOCAL off with its reason when local_infile is OFF", async () => {
    sql(server, "SET GLOBAL local_infile = 0");
    try {
      await fromSchemasTable("orders_load_off");
      await openFile(FILES.loadOff);
      await toTarget("existing");
      const plan = await toCheck();
      const view = await checkView();
      check(plan.method.load_data_available === false, `plan method ${JSON.stringify(plan.method)}`);
      check(view.loadData?.disabled === true, `LOAD DATA LOCAL control ${JSON.stringify(view.loadData)}`);
      check(/LOAD DATA LOCAL is off: .*local_infile OFF.*myflames never changes it/.test(view.methodHint) && !/does not change this setting/.test(view.methodHint), `reason ${view.methodHint}`);
      check(/local_infile/.test(view.loadData.title), `title ${view.loadData.title}`);
      await shot(`${E}-load-data-off`);
      await recover();
      return { reason: view.methodHint };
    } finally {
      sql(server, "SET GLOBAL local_infile = 1");
    }
  });

  await scenario(E, `Stop on a large file (${BIG_ROWS.toLocaleString("en-US")} rows), keep nothing`, async () => {
    await fromSchemasTable("orders_big");
    const file = await openFile(FILES.big);
    await toTarget("existing");
    const plan = await toCheck();
    check(plan.ready, `blockers ${plan.blockers}`);
    const run = await reviewAndImport(E, FILES.big, "orders_big", () => stopAt(1000));
    check(run.extra.stops === 1, `stop requests ${run.extra.stops}`);
    check(run.outcome.kind === "stopped", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    await shot(`${E}-stop-keep-nothing`);
    await closeResult();
    const n = count(server, "orders_big");
    check(n === 0 && run.result.rows.committed === 0, `table rows ${n}, committed ${run.result.rows.committed}`);
    return { file: file.counts, stoppedAt: run.extra.at, sent: run.result.rows.sent, committed: 0, readBack: n, message: run.result.message };
  });

  await scenario(E, "Stop on a large file, commit every 10,000 rows: committed rows stay", async () => {
    await fromSchemasTable("orders_big");
    await openFile(FILES.big);
    await toTarget("existing");
    await toCheck();
    await commitEvery(10000);
    const run = await reviewAndImport(E, FILES.big, "orders_big", () => stopAt(10000, 10000));
    check(run.outcome.kind === "stopped", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    const r = run.result;
    await shot(`${E}-stop-commit-every`);
    await closeResult();
    const n = count(server, "orders_big");
    check(r.rows.committed >= 10000 && r.rows.committed % 10000 === 0 && n === r.rows.committed,
      `committed ${r.rows.committed}, table rows ${n}`);
    const max = Number(one(server, "SELECT MAX(order_id) FROM e2e.orders_big"));
    check(max === r.rows.committed && r.next_line === r.rows.committed + 2, `max id ${max}, next line ${r.next_line}`);
    sql(server, "TRUNCATE TABLE e2e.orders_big");
    return { stoppedAt: run.extra.at, committed: r.rows.committed, readBack: n, nextLine: r.next_line };
  });

  // Server > Tables: the row menu and the inspector, pre-filled.
  await scenario(E, "tab-separated .tsv file from the Server > Tables row menu", async () => {
    await tablesView(true);
    await tablesRowMenu("orders_tsv");
    const item = await menuItem(".context-menu[aria-label='Row actions']", "Import CSV…");
    check(item && !item.disabled, `row menu item ${JSON.stringify(item)}`);
    await clickMenuItem(".context-menu[aria-label='Row actions']", "Import CSV…");
    await page.waitForSelector(`${SHEET}[open]`);
    check(await page.$eval(`${SHEET} h2`, (e) => e.textContent) === "Import CSV into e2e.orders_tsv", "title");
    const file = await openFile(FILES.tsv);
    check(file.separator === "Tab" && file.detected.some((d) => /Detected: tab/.test(d)), `separator ${file.separator} ${file.detected}`);
    await toTarget("existing");
    const plan = await toCheck();
    check(plan.ready, `blockers ${plan.blockers}`);
    const run = await reviewAndImport(E, FILES.tsv, "orders_tsv");
    check(run.outcome.kind === "done", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    await closeResult();
    const back = rowsOf(server, "orders_tsv");
    check(JSON.stringify(back) === JSON.stringify(EXPECT.tsv), `read back ${JSON.stringify(back)}`);
    return { committed: run.result.rows.committed, readBack: back.length };
  });

  await scenario(E, "Latin-1 (Windows-1252) file from the Server > Tables inspector", async () => {
    await tablesView();
    await tablesInspector("orders_latin1");
    const gate = await page.$eval(".view-inspector [data-action=import-csv]", (e) => ({ aria: e.getAttribute("aria-disabled"), state: e.dataset.privilegeState }));
    check(gate.aria !== "true", `inspector action ${JSON.stringify(gate)}`);
    await page.click(".view-inspector [data-action=import-csv]");
    await page.waitForSelector(`${SHEET}[open]`);
    check(await page.$eval(`${SHEET} h2`, (e) => e.textContent) === "Import CSV into e2e.orders_latin1", "title");
    const file = await openFile(FILES.latin1);
    check(file.encoding === "cp1252", `encoding ${file.encoding} (${file.detected})`);
    check(file.separator === "Semicolon", `separator ${file.separator}`);
    check(file.firstRow.includes(`José Ñandú ${MARK}`), `preview ${file.firstRow}`);
    await toTarget("existing");
    const plan = await toCheck();
    check(plan.ready, `blockers ${plan.blockers}`);
    const run = await reviewAndImport(E, FILES.latin1, "orders_latin1");
    check(run.outcome.kind === "done", `outcome ${run.outcome.kind}: ${run.outcome.text}`);
    await closeResult();
    const back = sql(server, "SELECT order_id, HEX(customer), amount, HEX(note) FROM e2e.orders_latin1 ORDER BY order_id");
    const want = EXPECT.latin1.map(([id, c, a, n]) => [id, Buffer.from(c, "utf8").toString("hex").toUpperCase(), a, Buffer.from(n, "utf8").toString("hex").toUpperCase()]);
    check(JSON.stringify(back) === JSON.stringify(want), `read back ${JSON.stringify(back)}`);
    return { committed: run.result.rows.committed, readBack: back.length, detected: file.detected };
  });
}

// ---- Run ---------------------------------------------------------------------------------

let exitCode = 1;
const onSignal = () => { cleanupDocker(); process.exit(130); };
process.on("SIGINT", onSignal);
process.on("SIGTERM", onSignal);
try {
  writeFiles();
  for (const engine of ENGINES) startServer(engine);
  for (const s of servers) {
    await waitReady(s);
    sql(s, SETUP);
    log(`${s.name}: ${s.version} on 127.0.0.1:${s.port}, local_infile ${s.localInfile}`);
  }
  await startWorkspace();
  // The fields the connection form sends (Connections.tsx tidy): the picker
  // sends TLS PREFERRED, and a saved password is used only for the exact
  // endpoint and TLS settings it was saved with.
  const FORM = { host: "127.0.0.1", user: "root", database: "", ssl_mode: "PREFERRED", ssl_ca: "" };
  const profiles = {};
  for (const s of servers) {
    profiles[s.engine] = (await api("/api/profiles/save", { value: { ...FORM, name: `E2E ${s.engine}`, port: s.port, type: "development" },
      password: s.password, remember_password: true })).id;
  }
  const readOnlyServer = servers[0];
  const readOnly = (await api("/api/profiles/save", { value: { ...FORM, name: `E2E ${readOnlyServer.engine} read-only`, port: readOnlyServer.port,
    type: "development", read_only: true }, password: readOnlyServer.password, remember_password: true })).id;
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await editorPlace();

  for (const s of servers) await engineScenarios(s, profiles[s.engine]);

  await scenario("all", "Audit log: one record per import, no row data", async () => {
    const report = await api("/api/audit/list", { limit: 500 });
    const all = report.records.filter((r) => r.operation === "import_csv");
    const records = all.filter((r) => r.status !== "declined");
    const json = JSON.stringify(all);
    check(!json.includes(MARK), "an import record contains row data");
    check(records.length === imports.length, `${records.length} import records for ${imports.length} imports`);
    const ids = new Set(records.map((r) => r.id));
    const missing = imports.filter((i) => !ids.has(i.audit));
    check(!missing.length, `imports without their record: ${JSON.stringify(missing)}`);
    const summaries = records.map((r) => (r.statements || []).find((s) => / rows? in [\d,]+ statements? from /.test(s) || /LOAD DATA LOCAL INFILE/.test(s)) || "");
    check(summaries.every(Boolean), "every record names its data statements");
    const create = records.find((r) => (r.statements || []).some((s) => /^(\/\*myflames:import_csv\*\/ )?CREATE TABLE `e2e`\.`people_semicolon`/.test(s)));
    check(create, "the new table's CREATE TABLE is recorded in full");
    await page.click('button[aria-label="Settings"]');
    await page.waitForSelector("#settings-popover");
    await page.locator("#settings-popover button ::-p-text(Audit log)").click();
    const view = ".server-view[data-view=audit]";
    await page.waitForSelector(`${view} [aria-label="Audit records"] tbody tr[data-row-key]`, { timeout: 30000 });
    await pause(500);
    const screen = await page.$$eval(`${view} [aria-label="Audit records"] tbody tr[data-row-key]`, (els) => els.map((e) => e.textContent));
    const shownImports = screen.filter((t) => /Import csv|Import CSV/.test(t));
    check(shownImports.length >= Math.min(imports.length, 20), `${shownImports.length} import rows on screen`);
    check(!screen.join("\n").includes(MARK), "the Audit log screen shows row data");
    await shot("audit-log");
    // How a stopped import reads in the list and in its inspector.
    const stopped = records.find((r) => r.outcome === "stopped");
    let stoppedShown = null;
    if (stopped) {
      const rowSel = `${view} [aria-label="Audit records"] tbody tr[data-row-key="${stopped.id}"]`;
      if (await page.$(rowSel)) {
        const status = await page.$eval(rowSel, (e) => e.textContent);
        await page.$eval(rowSel, (e) => e.scrollIntoView({ block: "center" }));
        await page.click(rowSel);
        await page.waitForSelector(".view-inspector .inspector-title", { timeout: 10000 }).catch(() => {});
        await pause(300);
        const inspector = await page.evaluate(() => [...document.querySelectorAll(".view-inspector")].find((e) => e.offsetParent && !e.closest("[hidden]"))?.textContent || "");
        stoppedShown = { row: status.slice(0, 120), inspector: inspector.slice(0, 400) };
        await shot("audit-log-stopped-import");
      }
    }
    const statuses = records.reduce((m, r) => ({ ...m, [`${r.status}/${r.outcome}`]: (m[`${r.status}/${r.outcome}`] || 0) + 1 }), {});
    return { records: records.length, declined: all.length - records.length, imports: imports.length, statuses, stoppedShown, label: /Import CSV/.test(shownImports[0] || "") ? "Import CSV" : "Import csv", sample: summaries[0] };
  });

  await scenario(readOnlyServer.engine, "Read-only connection: every entry point off with its reason", async () => {
    await editorPlace();
    await chooseConnection(page, readOnly);
    await loadSchemas();
    await treeMenu("table", "orders_comma");
    const table = await menuItem(".schema-browser .context-menu", "Import CSV…");
    await page.keyboard.press("Escape");
    await treeMenu("schema", "e2e");
    const database = await menuItem(".schema-browser .context-menu", "Import CSV into a new table…");
    await shot("read-only-schemas-database-menu");
    await page.keyboard.press("Escape");
    check(table?.disabled && /This connection is read-only\./.test(table.text), `table item ${JSON.stringify(table)}`);
    check(database?.disabled && /This connection is read-only\./.test(database.text), `database item ${JSON.stringify(database)}`);
    // An item that is off opens nothing.
    await treeMenu("table", "orders_comma");
    await clickMenuItem(".schema-browser .context-menu", "Import CSV…");
    await pause(400);
    check(!(await page.$(`${SHEET}[open]`)), "the read-only item opened the wizard");
    await page.keyboard.press("Escape");
    await tablesView(true);
    await page.waitForFunction(() => !document.querySelector('.server-view[data-view="tables"] .spinner'), { timeout: 60000 }).catch(() => {});
    await pause(1000);
    // The list read on the read-only connection (a list from the other connection says so instead).
    let row = null;
    for (let i = 0; i < 60; i++) {
      await tablesRowMenu("orders_tsv");
      row = await menuItem(".context-menu[aria-label='Row actions']", "Import CSV…");
      if (!/another connection/.test(`${row?.text} ${row?.title}`)) break;
      await page.keyboard.press("Escape");
      await pause(1000);
    }
    await shot("read-only-tables-row-menu");
    await page.keyboard.press("Escape");
    check(row?.disabled && /read-only/.test(row.text + row.title), `row menu item ${JSON.stringify(row)}`);
    await tablesInspector("orders_tsv");
    const inspector = await page.$eval(".view-inspector [data-action=import-csv]", (e) => ({ aria: e.getAttribute("aria-disabled"), title: e.title }));
    check(inspector.aria === "true" && /This connection is read-only\./.test(inspector.title), `inspector ${JSON.stringify(inspector)}`);
    return { table: table.text, database: database.text, row: row.text || row.title, inspector: inspector.title };
  });

  if (errors.length) results.push({ engine: "all", name: "no page errors", pass: false, error: errors.join("\n").slice(0, 3000) });
  exitCode = results.every((r) => r.pass) ? 0 : 1;
} catch (error) {
  log(`aborted: ${error?.stack || error}`);
  if (page) await page.screenshot({ path: path.join(shots, "FAIL-aborted.png") }).catch(() => {});
  results.push({ engine: "all", name: "run", pass: false, error: String(error?.message || error).slice(0, 3000) });
} finally {
  await browser?.close().catch(() => {});
  proc?.kill();
  cleanupDocker();
  const summary = { token: TOKEN, servers: servers.map((s) => ({ engine: s.engine, image: s.image, version: s.version, name: s.name })),
    pulled, imports, results, errors };
  if (process.env.IMPORT_CSV_LIVE_REPORT) writeFileSync(process.env.IMPORT_CSV_LIVE_REPORT, JSON.stringify(summary, null, 2));
  rmSync(work, { recursive: true, force: true });
  const passed = results.filter((r) => r.pass).length;
  console.log(`import-csv-live: screenshots in ${shots}`);
  console.log(`import-csv-live: ${passed} of ${results.length} scenarios passed on ${servers.map((s) => `${s.engine} ${s.version}`).join(", ")}`);
  for (const r of results) console.log(`${r.pass ? "PASS" : "FAIL"} ${r.engine}: ${r.name}${r.pass ? "" : ` (${r.error.split("\n")[0].slice(0, 300)})`}`);
}
process.exit(exitCode);
