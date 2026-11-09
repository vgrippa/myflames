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
  browser = await puppeteer.launch({ headless: "shell" });
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
  console.log("auth: public shell, one-time launch, fragment removal, unstored launch secret, HttpOnly cookie, refresh, tabs, browser isolation, cross-port cookie theft and CSRF passed");
} finally {
  if (browser) await browser.close();
  if (otherService) await new Promise(resolve => otherService.close(resolve));
  proc.kill("SIGTERM");
  await fs.rm(home, { recursive: true, force: true });
}
