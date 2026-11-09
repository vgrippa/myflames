// The Query editor's Explain against a real MySQL server, with no mocks:
// the real WorkspaceServer (throwaway HOME, vault secrets), the built
// workspace and a MySQL container. Skipped unless MYFLAMES_MYSQL_LAB names the
// container (for example myflames-ga-mysql-9-7 from scripts/ga-matrix.sh) and
// MYFLAMES_MYSQL_LAB_PORT its published port; the root password is read from
// the container's environment and kept in memory. Covers: Explain stays in the
// editor and shows EXPLAIN FORMAT=TRADITIONAL's columns in an "Explain" result
// tab with Export; EXPLAIN of a DELETE never deletes; the Tree format; two
// statements are refused; Explore opens the plan in Plans only when clicked.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../../..");
const container = process.env.MYFLAMES_MYSQL_LAB;
const port = Number(process.env.MYFLAMES_MYSQL_LAB_PORT);
if (!container || !port) {
  console.log("editor-explain-live: skipped (set MYFLAMES_MYSQL_LAB to a MySQL container name and MYFLAMES_MYSQL_LAB_PORT to its port)");
  process.exit(0);
}
const env = execFileSync("docker", ["inspect", container, "--format", "{{range .Config.Env}}{{println .}}{{end}}"]).toString();
const password = env.split("\n").find((l) => l.startsWith("MYSQL_ROOT_PASSWORD="))?.slice("MYSQL_ROOT_PASSWORD=".length);
assert.ok(password, "the container has MYSQL_ROOT_PASSWORD");
const sql = (text) => execFileSync("docker", ["exec", "-i", "-e", "MYSQL_PWD", container, "mysql", "-uroot", "-N", "-e", text],
  { env: { ...process.env, MYSQL_PWD: password } }).toString().trim();
sql("CREATE DATABASE IF NOT EXISTS mf_explain; CREATE TABLE IF NOT EXISTS mf_explain.t (id INT PRIMARY KEY, v INT, KEY k_v (v)); "
  + "INSERT IGNORE INTO mf_explain.t VALUES (1,1),(2,2),(3,3),(4,3),(5,5);");
const before = sql("SELECT COUNT(*) FROM mf_explain.t");

const home = await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(), "myflames-explain-live-")));
const proc = spawn(process.env.PYTHON || "python3",
  ["-c", "import sys, myflames.ui as u; s=u.WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()",
    path.join(home, "workspace.sqlite3")],
  { cwd: root, env: { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: "vault" } });
const url = await new Promise((resolve, reject) => {
  let text = "";
  const timer = setTimeout(() => reject(Error(text)), 15000);
  proc.stderr.on("data", (chunk) => {
    text += chunk;
    const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
    if (match) { clearTimeout(timer); resolve(match[0]); }
  });
});

const browser = await puppeteer.launch({ headless: "shell" });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url);
  await page.waitForSelector('.main-nav button[data-place="query"]', { timeout: 30000 });
  const api = (pathname, body) => page.evaluate(async (p, b) => {
    const headers = { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" };
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers, body: "{}" })).json()).token;
    const reply = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify(b) });
    return reply.json();
  }, pathname, body);
  await api("/api/profiles/save", { value: { name: "Explain lab", host: "127.0.0.1", port, user: "root", database: "mf_explain",
    ssl_mode: "PREFERRED", ssl_ca: "", type: "development" }, password, remember_password: true });
  await page.reload({ waitUntil: "networkidle0" });
  await page.click('.main-nav button[data-place="query"]');
  await page.waitForSelector(".query-lab .editor-toolbar", { visible: true });
  await chooseConnection(page, await connectionOption(page, "Explain lab"));
  const editor = '[aria-label="SQL editor"]';
  const explain = async (text, format = "table") => {
    await page.$eval(editor, (e) => { e.focus(); e.select(); });
    await page.locator(editor).fill(text);
    await page.select('select[aria-label="Explain format"]', format);
    await page.click("[data-action=explain]");
  };

  // Table: EXPLAIN FORMAT=TRADITIONAL rows in an "Explain" tab; no place switch.
  await explain("SELECT * FROM mf_explain.t WHERE v = 3");
  await page.waitForFunction(() => [...document.querySelectorAll(".query-lab [role=tab]")].some((t) => /Explain/.test(t.textContent)), { timeout: 60000 });
  assert.equal(await page.$eval(".main-nav button.active", (e) => e.dataset.place), "query", "Explain stays in the Query editor");
  await page.waitForFunction(() => /select_type/.test(document.querySelector(".query-lab")?.innerText || ""), { timeout: 30000 });
  const grid = await page.$eval(".query-lab", (e) => e.innerText);
  for (const column of ["select_type", "table", "type", "possible_keys", "key", "rows", "filtered", "Extra"])
    assert.ok(grid.includes(column), `the Explain result has the ${column} column`);
  assert.match(grid, /k_v/, "the plan names the index it uses");
  assert.ok(await page.$(".results-tabs-actions button ::-p-text(Explore)"), "Explore is offered on the Explain result");
  assert.ok(await page.$(".results-tabs-actions .export-menu, .results-tabs-actions [aria-label^='Export'], .results-tabs-actions button ::-p-text(Export)"),
    "the Explain result has Export");
  const shots = process.env.EXPLAIN_SHOTS || path.join(here, "shots-editor-explain-live");
  await fs.mkdir(shots, { recursive: true });
  await page.screenshot({ path: path.join(shots, "table-light-1440.png") });

  // EXPLAIN of a DELETE explains it and deletes nothing.
  await explain("DELETE FROM mf_explain.t WHERE v = 3");
  await page.waitForFunction(() => /DELETE/.test(document.querySelector(".query-lab")?.innerText || ""), { timeout: 60000 });
  await new Promise((r) => setTimeout(r, 1000));
  assert.equal(sql("SELECT COUNT(*) FROM mf_explain.t"), before, "EXPLAIN DELETE removed no row");

  // Tree format (MySQL 8.0.16+).
  await explain("SELECT * FROM mf_explain.t WHERE v = 3", "tree");
  await page.waitForFunction(() => /->/.test(document.querySelector(".query-lab")?.innerText || ""), { timeout: 60000 });

  // Explain works on the statement under the cursor, like Workbench's Explain
  // Current Statement; a selection spanning two statements is refused.
  await page.$eval(editor, (e) => { e.focus(); e.select(); });
  await page.locator(editor).fill("SELECT 1; SELECT 2");
  await page.$eval(editor, (e) => { e.focus(); e.setSelectionRange(0, e.value.length); e.dispatchEvent(new Event("select", { bubbles: true })); });
  await page.click("[data-action=explain]");
  await page.waitForFunction(() => /one statement at a time/i.test(document.querySelector(".query-lab")?.innerText || ""), { timeout: 30000 });

  // Explore opens the visual plan in Plans, only when clicked.
  await explain("SELECT * FROM mf_explain.t WHERE v = 3");
  await page.waitForSelector(".results-tabs-actions button ::-p-text(Explore)", { timeout: 60000 });
  await page.click(".results-tabs-actions button ::-p-text(Explore)");
  await page.waitForFunction(() => document.querySelector(".main-nav button.active")?.dataset.place === "plans", { timeout: 60000 });

  assert.deepEqual(errors, []);
  console.log("editor-explain-live: ok");
} finally {
  await browser.close();
  proc.kill();
  await fs.rm(home, { recursive: true, force: true });
}
