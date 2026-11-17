// Real browser authentication against an isolated workspace, no mocked API.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const home = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-auth-"));
const proc = spawn(process.env.PYTHON || "python3", ["-c", "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()"], {
  cwd: root, env: { ...process.env, HOME: home, USERPROFILE: home, MYFLAMES_SECRET_BACKEND: "vault" },
});
let browser;
let otherService;
try {
  const launch = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(Error("Workspace startup timed out")), 10000);
    proc.stderr.on("data", chunk => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    proc.on("error", reject);
  });
  const origin = new URL(launch).origin;
  const secret = new URL(launch).hash.slice("#launch=".length);
  browser = await puppeteer.launch({ headless: "shell", args: process.env.MYFLAMES_NO_SANDBOX ? ["--no-sandbox"] : (process.env.MYFLAMES_BROWSER_ARGS || "").split(" ").filter(Boolean) });
  // Public guidance remains styled when JavaScript is disabled. It carries
  // no credentials; the same page is used before bootstrap and after refusal.
  const staticPage = await browser.newPage();
  await staticPage.setJavaScriptEnabled(false);
  for (const width of [1440, 390]) {
    await staticPage.setViewport({ width, height: 900 });
    for (const theme of ["light", "dark"]) {
      await staticPage.emulateMediaFeatures([{ name: "prefers-color-scheme", value: theme }]);
      await staticPage.goto(origin);
      await staticPage.waitForSelector(".launch-refusal");
      const measured = await staticPage.$eval(".launch-refusal", e => ({
        font: getComputedStyle(e).fontFamily, background: getComputedStyle(e).backgroundColor,
        left: e.getBoundingClientRect().left, right: e.getBoundingClientRect().right,
        overflow: document.documentElement.scrollWidth > innerWidth,
      }));
      assert.match(measured.font, /system|Segoe/);
      assert.equal(measured.background, theme === "dark" ? "rgb(21, 23, 25)" : "rgb(255, 255, 255)");
      assert.ok(measured.left >= 0 && measured.right <= width && !measured.overflow, JSON.stringify(measured));
      assert.equal(await staticPage.$('meta[name="myflames-token"]'), null);
      assert.equal((await staticPage.content()).includes(secret), false);
    }
  }
  await staticPage.close();
  const page = await browser.newPage();
  const seen = [];
  page.on("request", request => seen.push({ url: request.url(), body: request.postData() || "" }));
  await page.goto(origin);
  await page.waitForSelector('[role="alert"]');
  assert.match(await page.$eval('[role="alert"]', e => e.textContent), /launch URL/);
  await page.goto(launch);
  await page.waitForSelector('.sidebar');
  assert.equal(new URL(page.url()).hash, "");
  assert.equal(await page.evaluate(() => document.cookie), "", "session is HttpOnly and API scoped");
  assert.equal(await page.$('meta[name="myflames-token"]'), null);
  assert.ok(seen.every(request => !request.url.includes(secret)), "secret is never a network URL");
  assert.equal(seen.filter(request => request.body.includes(secret)).length, 1);
  assert.equal(await page.evaluate(secret => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, html: document.documentElement.outerHTML }).includes(secret), secret), false);
  await page.reload();
  await page.waitForSelector('.sidebar');
  const tab = await browser.newPage();
  await tab.goto(origin);
  await tab.waitForSelector('.sidebar');
  await tab.goto(launch);
  await tab.waitForSelector('.sidebar');
  assert.equal(new URL(tab.url()).hash, "");
  const context = await browser.createBrowserContext();
  const stranger = await context.newPage();
  await stranger.goto(launch);
  await stranger.waitForSelector('[role="alert"]');
  assert.match(await stranger.$eval('[role="alert"]', e => e.textContent), /already used or expired/);
  const status = await page.evaluate(async () => (await fetch('/api/session/load', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status);
  assert.equal(status, 403, "cookie without CSRF is refused");
  // A service on another port receives host cookies, but cannot read the
  // proof stored under the workspace origin. Even forged Origin is useless.
  let copiedCookie = "";
  otherService = http.createServer((request, response) => {
    copiedCookie = request.headers.cookie || "";
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end("<!doctype html><title>Other local service</title>");
  });
  await new Promise(resolve => otherService.listen(0, "127.0.0.1", resolve));
  const other = await browser.newPage();
  await other.goto(`http://127.0.0.1:${otherService.address().port}/api/probe`);
  assert.ok(copiedCookie.includes("myflames"), "the second service actually receives the host cookie");
  assert.equal(await other.evaluate(() => localStorage.getItem("myflames.session-proof")), null);
  const stolen = await fetch(`${origin}/api/auth/session`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", Origin: origin, Cookie: copiedCookie }, body: "{}",
  });
  assert.equal(stolen.status, 403, "a copied cookie cannot retrieve the origin proof");
  await page.reload();
  await page.waitForSelector('.sidebar');
  await context.close();
  // Launch-secret retry rules (api.ts authenticate): an attempt that got no
  // answer (connection reset) or the overload refusal (a 503 the server
  // writes before reading the request) keeps the secret and retries within
  // the same three attempts; any other HTTP answer spends it. Each scenario
  // gets a fresh server because a presented secret is consumed server-side.
  const startWorkspace = async () => {
    const scenarioHome = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-auth-retry-"));
    const scenarioProc = spawn(process.env.PYTHON || "python3", ["-c", "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()"], {
      cwd: root, env: { ...process.env, HOME: scenarioHome, USERPROFILE: scenarioHome, MYFLAMES_SECRET_BACKEND: "vault" },
    });
    const scenarioLaunch = await new Promise((resolve, reject) => {
      let output = "";
      const timer = setTimeout(() => reject(Error("Workspace startup timed out")), 10000);
      scenarioProc.stderr.on("data", chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
        if (match) { clearTimeout(timer); resolve(match[0]); }
      });
      scenarioProc.on("error", reject);
    });
    return { proc: scenarioProc, home: scenarioHome, launch: scenarioLaunch };
  };
  const retryScenario = async (handleAuthRequest, check) => {
    const scenario = await startWorkspace();
    const scenarioSecret = new URL(scenario.launch).hash.slice("#launch=".length);
    const retryPage = await browser.newPage();
    let authRequests = 0;
    const secretBodies = [];
    await retryPage.setRequestInterception(true);
    retryPage.on("request", request => {
      if (new URL(request.url()).pathname === "/api/auth/session") {
        authRequests++;
        const body = request.postData() || "";
        if (body.includes(scenarioSecret)) secretBodies.push(body);
        handleAuthRequest(request, authRequests);
      } else {
        request.continue();
      }
    });
    const waitForAuthRequests = async n => {
      const deadline = Date.now() + 10000;
      while (authRequests < n && Date.now() < deadline)
        await new Promise(resolve => setTimeout(resolve, 50));
    };
    try {
      await retryPage.goto(scenario.launch);
      await check(retryPage, () => authRequests, secretBodies, waitForAuthRequests);
    } finally {
      scenario.proc.kill("SIGTERM");
      await fs.rm(scenario.home, { recursive: true, force: true });
      await retryPage.close().catch(() => {});
    }
  };
  // (a) A reset connection is unanswered: the retry carries the secret again.
  await retryScenario(
    (request, n) => { if (n === 1) request.abort("connectionreset"); else request.continue(); },
    async (retryPage, authRequests, secretBodies) => {
      await retryPage.waitForSelector(".sidebar");
      assert.equal(authRequests(), 2);
      assert.equal(secretBodies.length, 2, "the aborted attempt keeps the secret for the retry");
    });
  // (b) The 503 overload refusal is written before the request is read: it
  // does not spend the secret and is retried like an unanswered request.
  await retryScenario(
    (request, n) => { if (n === 1) request.respond({ status: 503, body: "" }); else request.continue(); },
    async (retryPage, authRequests, secretBodies) => {
      await retryPage.waitForSelector(".sidebar");
      assert.equal(authRequests(), 2);
      assert.equal(secretBodies.length, 2, "the 503 overload refusal keeps the secret for the retry");
    });
  // (c) Any other answer spends the secret: a 403 refusal is final, stays
  // cached, and a later call sends nothing.
  await retryScenario(
    (request, n) => {
      if (n === 1) request.respond({ status: 403, contentType: "application/json",
        body: JSON.stringify({ error: "Open the launch URL printed by myflames ui. If it was already used or expired, restart myflames ui." }) });
      else request.continue();
    },
    async (retryPage, authRequests, secretBodies, waitForAuthRequests) => {
      await waitForAuthRequests(1);
      // Two backoff slots pass without a second request: no retry fired.
      await new Promise(resolve => setTimeout(resolve, 700));
      assert.match(await retryPage.$eval('.launch-refusal', e => e.textContent), /launch URL/);
      await retryPage.evaluate(() => { window.location.hash = "#again"; });
      await new Promise(resolve => setTimeout(resolve, 500));
      assert.match(await retryPage.$eval('[role="alert"]', e => e.textContent), /launch URL/);
      assert.equal(authRequests(), 1, "a refusal stays cached: no retry, not even on a later call");
      assert.equal(secretBodies.length, 1);
    });
  // (d) At most three attempts per call; the failed promise is not cached,
  // so a later call tries again and the never-read secret still opens.
  await retryScenario(
    (request, n) => { if (n <= 3) request.abort("connectionreset"); else request.continue(); },
    async (retryPage, authRequests, secretBodies, waitForAuthRequests) => {
      // The static page already carries the guidance text; the bootstrap
      // failure replaces it with the fetch error, proving the call settled.
      await retryPage.waitForFunction(() =>
        document.querySelector(".launch-refusal p")?.textContent === "Failed to fetch");
      assert.equal(authRequests(), 3, "at most three attempts per call");
      assert.equal(secretBodies.length, 3);
      await retryPage.evaluate(() => { window.location.hash = "#retry"; });
      await retryPage.waitForSelector(".sidebar");
      assert.equal(authRequests(), 4, "a later call after three unanswered attempts tries again");
      assert.equal(secretBodies.length, 4);
    });
  console.log("auth-retry: reset keeps the secret, 503 overload refusal retries, 403 refusal is final and cached, three attempts then a later call retries passed");
  console.log("auth: public shell, one-time launch, fragment removal, unstored launch secret, HttpOnly cookie, refresh, tabs, browser isolation, cross-port cookie theft and CSRF passed");
} finally {
  if (browser) await browser.close();
  if (otherService) await new Promise(resolve => otherService.close(resolve));
  proc.kill("SIGTERM");
  await fs.rm(home, { recursive: true, force: true });
}
