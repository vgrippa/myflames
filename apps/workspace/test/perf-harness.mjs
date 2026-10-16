// Shared harness for the Server place's view suites (Waits, Indexes, Tables,
// I/O, Configuration, Instruments, Sessions, InnoDB, the sub-navigation):
// the real Python server on a temporary workspace database (session autosave,
// the rest of the API), with one live endpoint mocked from fixtures captured
// on throwaway MySQL and MariaDB servers.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixtures = fileURLToPath(new URL("./fixtures/", import.meta.url));

export const load = async (name) => JSON.parse(await fs.readFile(path.join(fixtures, name), "utf8"));
export const pause = (ms) => new Promise((r) => setTimeout(r, ms));

/** The Server sub-navigation labels by group, in order (SERVER_VIEWS in
 * Server.tsx). The one place suites learn the list: update it with a new view. */
export const SERVER_GROUPS = {
  Overview: ["Health", "InnoDB", "Full analysis"],
  Workload: ["Top statements", "Sessions", "Replication", "Waits", "I/O"],
  Contention: ["Locks"],
  Schema: ["Indexes", "Tables", "Database diagram"],
  Setup: ["Configuration", "Instruments", "Maintenance"],
  Security: ["Users and roles"],
};
export const SERVER_VIEW_LABELS = Object.values(SERVER_GROUPS).flat();
/** The Server state a session saves when nothing was changed (normalizeLab in
 * session.ts). Suites spread it under the values they set, so an exact
 * comparison stays exact when a view adds a saved key: update it here. */
export const SLOW_DEFAULTS = {
  kind: "health", order: "total", min_seconds: 0, health_interval: 5, schema: "", waits_interval: 10, io_interval: 10,
  inspector: false, sessions_scope: "active", maintenance_section: "tools",
};

/** Explicit synthetic SHOW GRANTS decisions for UI gating, not server evidence. */
export const privilegeAnswer = (state, need = "CONNECTION_ADMIN or SUPER", extra = {}) => ({
  state, allowed: state === "allowed" ? true : state === "denied" ? false : null,
  missing: state === "denied" ? [need] : [], need, reason: state === "denied" ? `Needs ${need}.` : "",
  grey_out: state === "denied", available: true, ...extra,
});
export async function assertPrivilege(page, selector, state, need) {
  await page.waitForFunction((s, expected) => document.querySelector(s)?.dataset.privilegeState === expected, {}, selector, state);
  const actual = await page.$eval(selector, e => ({ disabled: e.disabled, aria: e.getAttribute("aria-disabled"), title: e.title,
    described: (e.getAttribute("aria-describedby") || "").split(" ").map(id => document.getElementById(id)?.textContent || "").join(" "),
    unverified: !!e.querySelector(".privilege-unverified") }));
  assert.equal(actual.disabled, false, "privilege denial never removes the button from keyboard focus");
  assert.equal(actual.aria, String(state === "denied"));
  assert.equal(actual.unverified, state === "unknown");
  if (need) { assert.ok(actual.title.includes(need), actual.title); assert.ok(actual.described.includes(need), actual.described); }
  await page.focus(selector);
  assert.equal(await page.$eval(selector, e => document.activeElement === e), true);
}

/** Start the workspace, open the Server place, and mock *endpoint*.
 * *extra* maps other paths to fixed responses ({status, body, delay?}), for
 * example the database picker's /api/live/schemas, or to a function
 * (body, request) returning one, or null when it answers the request itself
 * later (request.respond). Returns helpers; call close() in a finally block. */
export async function open(endpoint, name, extra = {}) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), `myflames-${name}-`));
  const store = path.join(directory, "workspace.sqlite3");
  const proc = spawn(
    process.env.PYTHON || "python3",
    ["-c", "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()", store],
    // A throwaway HOME and the vault backend: never ~/.myflames or the OS keychain.
    { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
  );
  const url = await new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(Error(text)), 10000);
    proc.stderr.on("data", (c) => {
      text += c;
      const match = text.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
    proc.on("error", reject);
  });
  const browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const state = { queue: [], fallback: null, delay: 0, seen: [], other: [], privilegeReads: [], privileges: { schema_version: "privileges-1.0", account: "root@localhost", actions: {} } };
  await page.setRequestInterception(true);
  page.on("request", (request) => {
    const pathname = new URL(request.url()).pathname;
    if (pathname !== endpoint && extra[pathname]) {
      const body = JSON.parse(request.postData() || "{}");
      state.other.push({ path: pathname, body });
      const mock = typeof extra[pathname] === "function" ? extra[pathname](body, request) : extra[pathname];
      if (!mock) return;
      const reply = () => request.respond({ status: mock.status, contentType: "application/json", body: JSON.stringify(mock.body) });
      return mock.delay ? void setTimeout(reply, mock.delay) : reply();
    }
    if (pathname !== endpoint && pathname === "/api/live/privileges") {
      // The privileges read of the connection in use (PrivilegesProvider) is
      // not a view read: kept in state.privilegeReads, apart from state.other,
      // and answered from state.privileges (no actions: nothing greyed).
      state.privilegeReads.push(JSON.parse(request.postData() || "{}"));
      return request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(state.privileges) });
    }
    if (pathname !== endpoint) {
      // Other live views are recorded and refused, so a test never reaches a real server.
      if ((pathname.startsWith("/api/live/") && pathname !== "/api/live/kill/prepare") || pathname.startsWith("/api/connection/")) {
        state.other.push({ path: pathname, body: JSON.parse(request.postData() || "{}") });
        return request.respond({ status: 400, contentType: "application/json",
          body: JSON.stringify({ error: "Not mocked in this suite." }) });
      }
      return request.continue();
    }
    state.seen.push(JSON.parse(request.postData() || "{}"));
    const mock = state.queue.shift() || state.fallback;
    setTimeout(
      () => request.respond({ status: mock.status, contentType: "application/json", body: JSON.stringify(mock.body) }),
      state.delay,
    );
  });
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const shots = process.env.PERF_SHOTS || directory;
  const helpers = {
    page,
    state,
    errors,
    shots,
    async setPrivileges(actions) {
      state.privileges = { ...state.privileges, actions };
      const before = state.privilegeReads.length;
      // The public focus refresh path; advance only during event dispatch so
      // confirmation TTLs and polling clocks are unaffected.
      await page.evaluate(() => {
        const now = Date.now;
        Date.now = () => now() + 61_000;
        try { window.dispatchEvent(new Event("focus")); } finally { Date.now = now; }
      });
      for (let i = 0; i < 100 && state.privilegeReads.length === before; i++) await pause(20);
      assert.equal(state.privilegeReads.length, before + 1, "provider refreshed on focus");
    },
    button: (text, scope = "") => page.locator(`${scope}button ::-p-text(${text})`).click(),
    text: (selector) => page.$eval(selector, (e) => e.textContent),
    /** A button's visible label: its text without the privilege reason
     * (.account-sr) that describes it through aria-describedby. */
    label: (selector) => page.$eval(selector, (e) => {
      const label = e.cloneNode(true);
      label.querySelectorAll(".account-sr").forEach((x) => x.remove());
      return label.textContent;
    }),
    texts: (selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent)),
    idle: (panel) => page.waitForFunction((p) => !document.querySelector(`${p} .spinner`), {}, panel),
    async server() {
      await helpers.button("Server", ".main-nav ");
      await page.waitForSelector(".server-place .source-list");
    },
    /** Open a Server view from the sub-navigation and wait for it. */
    async view(kind) {
      await page.click(`#server-nav-${kind}`);
      await page.waitForSelector(`#server-nav-${kind}[aria-current=page]`);
      await page.waitForSelector(`.server-view[data-view="${kind}"]`);
    },
    /** The toolbar Refresh of the open view (the one place it lives). */
    async refresh() {
      await page.click(".server-view [data-action=refresh]");
    },
    /** Click a row (a table row, a grouped row or a tile) and wait for the inspector to show it. */
    async select(selector) {
      const row = await page.waitForSelector(selector);
      const target = (await row.$("[data-row-key]")) || row;
      await target.click();
      await page.waitForSelector(".view-inspector .inspector-title");
    },
    /** The inspector's text (title, meta, actions and body). */
    inspector: () => page.$eval(".view-inspector", (e) => e.textContent),
    /** Open an ⓘ popover by its label and return its text; Escape closes it again. */
    async popover(label) {
      await page.click(`button[aria-label="${label}"]`);
      const text = await page.$eval(`[role=dialog][aria-label="${label}"]`, (e) => e.textContent);
      await page.keyboard.press("Escape");
      await page.waitForFunction((l) => !document.querySelector(`[role=dialog][aria-label="${l}"]`), {}, label);
      return text;
    },
    /** Open the "N notes" popover of the open view and return its text. */
    async notes() {
      await page.click(".server-view .notes-button");
      const text = await page.$eval(".popover", (e) => e.textContent);
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => !document.querySelector(".popover"));
      return text;
    },
    /** Poll the stored session until *check(slow)* is true; returns slow. */
    async savedSlow(check) {
      const token = await page.evaluate(async () => (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token);
      let saved;
      for (let i = 0; i < 100; i++) {
        saved = await page.evaluate(async (t) => (await fetch("/api/session/load", {
          method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": t }, body: "{}",
        })).json(), token);
        if (saved.state?.query_lab?.slow && check(saved.state.query_lab.slow)) return saved;
        await pause(100);
      }
      assert.fail("the session never saved the expected Server state");
    },
    /** No horizontal page scroll at 390 px: the open view is pushed on screen
     * with a back button, the inspector is a sheet; restores the desktop size. */
    async narrow(file) {
      await page.setViewport({ width: 390, height: 844 });
      await pause(250);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(overflow <= 0, `horizontal scroll of ${overflow}px at 390 px`);
      const layout = await page.evaluate(() => ({
        list: !!document.querySelector(".server-layout.show-list"),
        back: !!document.querySelector(".server-view .server-back"),
        view: !!document.querySelector(".server-view")?.offsetParent,
      }));
      assert.ok(layout.list || (layout.back && layout.view), `narrow layout: ${JSON.stringify(layout)}`);
      if (file) await page.screenshot({ path: path.join(shots, file), fullPage: true });
      await page.setViewport({ width: 1440, height: 1000 });
      await pause(150);
    },
    async close() {
      await browser.close();
      proc.kill();
      await fs.rm(directory, { recursive: true, force: true });
    },
  };
  return helpers;
}
