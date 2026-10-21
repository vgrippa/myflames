// Query editor: the ⓘ next to Safe updates in Run options. It opens the
// standard info popover at once without closing Run options, explains what
// sql_safe_updates refuses, its defaults and how to turn it off for the tab,
// and links to the MySQL and MariaDB documentation through the desktop link
// allowlist. Escape closes the explanation first, then Run options. The
// packaged page and the real Python server; the editor API is mocked, so no
// database is needed. Run after `npm run build`: node test/safe-updates-info.mjs
// (SAFE_UPDATES_SHOTS=<dir> also writes light and dark screenshots at
// 1440x900 and 720x520).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const require = createRequire(import.meta.url);
const { externalLink } = require("../../desktop/backend.cjs");
const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-safe-updates-"));
const shots = process.env.SAFE_UPDATES_SHOTS || "";
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  // A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);

const seen = [];
const calls = (p) => seen.filter((s) => s.path === p);
function reply(p) {
  switch (p) {
    case "/api/editor/preview":
      return { status: "ready", statements: [], aggregate: { state: "unknown", missing: [], reason: "This session's privileges are not verified.", warning: "" }, policy: { action: "allow", text: "" } };
    case "/api/live/privileges":
      return { schema_version: "privileges-1.0", account: "app@%", grants: [], current_roles: [], actions: {} };
    case "/api/editor/status":
      return { status: "none", session: null, run: null };
    case "/api/editor/history":
      return { items: [] };
  }
  return undefined;
}

const RUN_OPTIONS = 'button[aria-label="Run options"]';
const RUN_PANEL = '[role=dialog][aria-label="Run options"]';
const INFO_BUTTON = `${RUN_PANEL} .safe-updates-row button[aria-label="About safe updates"]`;
const INFO = '[role=dialog][aria-label="About safe updates"]';
const MYSQL_DOCS = "https://dev.mysql.com/doc/refman/8.4/en/server-system-variables.html#sysvar_sql_safe_updates";
const MARIADB_DOCS = "https://mariadb.com/docs/server/server-management/variables-and-modes/server-system-variables#sql_safe_updates";

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
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const p = new URL(request.url()).pathname;
    if (!p.startsWith("/api/editor/") && p !== "/api/live/privileges") {
      if (p.startsWith("/api/live/") || p.startsWith("/api/connection/")) {
        seen.push({ path: p });
        return request.respond({ status: 400, contentType: "application/json", body: '{"error":"Not mocked"}' });
      }
      return request.continue();
    }
    seen.push({ path: p, body: JSON.parse(request.postData() || "{}") });
    const answer = reply(p);
    if (answer === undefined) return request.respond({ status: 500, contentType: "application/json", body: '{"error":"no mock"}' });
    request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(answer) });
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.locator(".main-nav button ::-p-text(Query editor)").click();
  await page.waitForSelector(".query-lab .editor-toolbar");

  const open = (selector) => page.$eval(selector, (e) => !e.hidden && e.getClientRects().length > 0).catch(() => false);
  const active = (selector) => page.$eval(selector, (e) => e === document.activeElement);
  async function openRunOptions() {
    if (!(await open(RUN_PANEL))) await page.click(RUN_OPTIONS);
    await page.waitForSelector(`${RUN_PANEL}:not([hidden])`);
    await page.waitForFunction((s) => getComputedStyle(document.querySelector(s)).visibility !== "hidden", {}, RUN_PANEL);
  }
  /** Click the ⓘ and return the time, measured in the page, from the click
   * to the explanation placed on screen. Run options' entrance is restarted
   * and held at its first frame (its transform applied) first, so a panel
   * transform that became the explanation's containing block would shift it
   * every time, whatever the runner's speed. */
  async function openInfo() {
    const held = await page.evaluate((panel, button, info) => {
      const el = document.querySelector(panel);
      el.style.animation = "none";
      void el.offsetWidth;
      el.style.animation = "";
      for (const a of el.getAnimations()) {
        a.pause();
        a.currentTime = 0;
      }
      window.__infoTiming = {};
      const shown = () => {
        const e = document.querySelector(info);
        if (!e || getComputedStyle(e).visibility === "hidden" || !e.style.left) return false;
        window.__infoTiming.shown = performance.now();
        return true;
      };
      document.querySelector(button).addEventListener("click", () => {
        window.__infoTiming.click = performance.now();
        const watch = new MutationObserver(() => { if (shown()) watch.disconnect(); });
        watch.observe(document.body, { subtree: true, childList: true, attributes: true });
      }, { once: true, capture: true });
      return el.getAnimations().length;
    }, RUN_PANEL, INFO_BUTTON, INFO);
    assert.ok(held >= 1, "Run options' entrance is held for the check");
    await page.click(INFO_BUTTON);
    await page.waitForFunction((s) => {
      const e = document.querySelector(s);
      return !!e && getComputedStyle(e).visibility !== "hidden";
    }, { timeout: 2000 }, INFO);
    assert.equal(await page.$eval(RUN_PANEL, (e) => e.getAnimations().length), 0, "opening a nested popover ends Run options' entrance");
    const timing = await page.evaluate(() => window.__infoTiming);
    assert.ok(timing.click !== undefined && timing.shown !== undefined, `the explanation was placed after the click: ${JSON.stringify(timing)}`);
    return timing.shown - timing.click;
  }
  /** The explanation lies inside the window and above the window footer. */
  async function fits() {
    const box = await page.$eval(INFO, (e) => {
      const r = e.getBoundingClientRect();
      const footer = document.querySelector(".window-statusbar")?.getBoundingClientRect().top ?? innerHeight;
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: innerWidth, height: innerHeight, footer,
        styleLeft: parseFloat(e.style.left), styleTop: parseFloat(e.style.top) };
    });
    // Fixed to the window where the popover placed it: Run options' entrance
    // transform must not become its containing block (it shifted it by the
    // panel's own offset, off the right edge). Its own entrance slides 4 px.
    assert.ok(Math.abs(box.left - box.styleLeft) < 1 && box.top - box.styleTop >= -0.5 && box.top - box.styleTop <= 4.5, `the explanation sits where it was placed: ${JSON.stringify(box)}`);
    assert.ok(box.left >= 0 && box.right <= box.width, `the explanation fits the window width: ${JSON.stringify(box)}`);
    assert.ok(box.top >= 0 && box.bottom <= Math.min(box.height, box.footer > box.top ? box.footer : box.height), `the explanation fits above the footer: ${JSON.stringify(box)}`);
    const button = await page.$eval(INFO_BUTTON, (e) => { const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right }; });
    const covers = box.left < button.right && box.right > button.left && box.top < button.bottom && box.bottom > button.top;
    assert.equal(covers, false, `the explanation leaves its ⓘ visible: ${JSON.stringify({ box, button })}`);
    return box;
  }

  // 1. The ⓘ sits beside the Safe updates box, inside Run options.
  await openRunOptions();
  const placement = await page.$eval(INFO_BUTTON, (button) => {
    const row = button.closest(".safe-updates-row");
    const box = row.querySelector(".safe-updates").getBoundingClientRect();
    const b = button.getBoundingClientRect();
    return { gap: b.left - box.right, middle: Math.abs((b.top + b.bottom) / 2 - (box.top + box.bottom) / 2), expanded: button.getAttribute("aria-expanded"), popup: button.getAttribute("aria-haspopup"), title: button.title };
  });
  assert.ok(placement.gap >= 0 && placement.gap <= 12, `the ⓘ follows the checkbox: ${JSON.stringify(placement)}`);
  assert.ok(placement.middle <= 3, `the ⓘ is centered on the checkbox line: ${JSON.stringify(placement)}`);
  assert.equal(placement.expanded, "false");
  assert.equal(placement.popup, "dialog");
  assert.equal(placement.title, "About safe updates", "the icon-only button has a tooltip");
  const checkedBefore = await page.$eval(".safe-updates input", (e) => e.checked);
  assert.equal(checkedBefore, true, "a Production connection starts with safe updates on");
  // Keyboard order: the box, then its ⓘ.
  await page.focus(".safe-updates input");
  await page.keyboard.press("Tab");
  assert.equal(await active(INFO_BUTTON), true, "Tab moves from Safe updates to its ⓘ");

  // 2. Opening it reacts at once and keeps Run options open.
  const took = await openInfo();
  assert.ok(took < 100, `the explanation appeared ${took.toFixed(1)} ms after the click`);
  assert.equal(await open(RUN_PANEL), true, "Run options stays open behind the explanation");
  assert.equal(await page.$eval(RUN_OPTIONS, (e) => e.getAttribute("aria-expanded")), "true");
  assert.equal(await page.$eval(INFO_BUTTON, (e) => e.getAttribute("aria-expanded")), "true");
  assert.equal(await page.$eval(INFO, (e) => e.contains(document.activeElement)), true, "focus moves into the explanation");

  // 3. What it says, in plain words.
  const said = (await page.$eval(INFO, (e) => e.textContent)).replace(/\s+/g, " ").trim();
  for (const phrase of [
    /^Stops an UPDATE or DELETE from changing every row by mistake/,
    /the server refuses one whose WHERE cannot use an index, unless it has a LIMIT \(error 1175, nothing changes\)/,
    /MariaDB also refuses a DELETE without a WHERE, even with a LIMIT/,
    /SELECT is not limited/,
    /It sets sql_safe_updates for this tab's session; on by default for Production, off for Development/,
    /Clear the box to turn it off for this tab \(Production asks first\)/,
  ]) assert.match(said, phrase);
  assert.doesNotMatch(said, /—/, "no em dashes");

  // 4. Official documentation, opened in the default browser (target=_blank
  // reaches the desktop window-open handler, which allows only these hosts).
  const links = await page.$$eval(`${INFO} a`, (as) => as.map((a) => ({ href: a.href, text: a.textContent, target: a.target, rel: a.rel, label: a.getAttribute("aria-label") })));
  assert.deepEqual(links.map((l) => [l.text, l.href]), [["MySQL", MYSQL_DOCS], ["MariaDB", MARIADB_DOCS]]);
  for (const link of links) {
    assert.equal(link.target, "_blank");
    assert.deepEqual(link.rel.split(/\s+/).sort(), ["noopener", "noreferrer"]);
    assert.match(link.label, new RegExp(`sql_safe_updates in the ${link.text} documentation \\(opens in your browser\\)`));
    assert.equal(externalLink(link.href), link.href, `${link.href} passes the desktop link allowlist`);
  }
  await fits();

  // 5. The ⓘ only explains: no session request, the box is unchanged.
  assert.equal(calls("/api/editor/open").length, 0, "reading the explanation sends nothing");
  assert.equal(await page.$eval(".safe-updates input", (e) => e.checked), checkedBefore);

  // 6. Escape closes the explanation first (focus back on the ⓘ), then Run options.
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => !document.querySelector(s), {}, INFO);
  assert.equal(await open(RUN_PANEL), true, "the first Escape keeps Run options open");
  assert.equal(await active(INFO_BUTTON), true, "focus returns to the ⓘ");
  await page.keyboard.press("Escape");
  await page.waitForFunction((s) => document.querySelector(s)?.hidden, {}, RUN_PANEL);
  assert.equal(await active(RUN_OPTIONS), true, "the second Escape closes Run options and focuses its button");

  // 7. Keyboard opening; a click elsewhere in Run options closes only the
  // explanation; a click outside closes both.
  await openRunOptions();
  await page.focus(INFO_BUTTON);
  await page.keyboard.press("Enter");
  await page.waitForSelector(INFO);
  await page.click(`${RUN_PANEL} [aria-label="Row limit"]`);
  await page.waitForFunction((s) => !document.querySelector(s), {}, INFO);
  assert.equal(await open(RUN_PANEL), true, "a click inside Run options keeps it open");
  await openInfo();
  await page.click('[aria-label="SQL editor"]');
  await page.waitForFunction((r, i) => !document.querySelector(i) && document.querySelector(r)?.hidden, {}, RUN_PANEL, INFO);

  // 8. Only a popover inside Run options keeps it open: another popover's
  // opening event closes it, and closePopovers() (an empty detail) closes
  // both; clicking another ⓘ closes it too.
  await openRunOptions();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("myflames-popover", { detail: "another-popover" })));
  await page.waitForFunction((s) => document.querySelector(s)?.hidden, {}, RUN_PANEL);
  await openRunOptions();
  await openInfo();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent("myflames-popover", { detail: "" })));
  await page.waitForFunction((r, i) => !document.querySelector(i) && document.querySelector(r)?.hidden, {}, RUN_PANEL, INFO);
  await openRunOptions();
  await page.click('button[aria-label="Query editor shortcuts"]');
  await page.waitForFunction((s) => document.querySelector(s)?.hidden, {}, RUN_PANEL);
  await page.keyboard.press("Escape");

  // 9. Small window (the app's minimum, 720 x 520) in light and dark.
  for (const [width, height] of [[1440, 900], [720, 520]]) {
    await page.setViewport({ width, height });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
      await pause(50);
      await openRunOptions();
      await openInfo();
      const box = await fits();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      assert.ok(overflow <= 0, `no horizontal page scroll at ${width} px (${overflow})`);
      if (shots) {
        await fs.mkdir(shots, { recursive: true });
        await pause(250); // the panel's entrance animation
        await page.screenshot({ path: path.join(shots, `safe-updates-info-${width}x${height}-${scheme}.png`) });
      }
      assert.ok(box.bottom - box.top > 0);
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      await page.waitForFunction((s) => document.querySelector(s)?.hidden, {}, RUN_PANEL);
    }
  }
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);

  assert.deepEqual(errors, [], "no page errors");
  console.log("Safe updates explanation: placement, instant opening inside Run options, wording, documentation links on the desktop allowlist, Escape order, outside clicks and 1440/720 px layouts in light and dark passed.");
} finally {
  await browser?.close();
  server.kill();
  await once(server, "exit").catch(() => {});
  await fs.rm(directory, { recursive: true, force: true });
}
