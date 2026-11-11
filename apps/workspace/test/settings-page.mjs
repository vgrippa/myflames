// The Settings page (src/SettingsPage.tsx): the sidebar gear, ⌘, / Ctrl+, and
// the command palette open it; each section shows its controls; every setting
// still saves and survives a reload exactly as before; Back (⌘[) and Esc
// return to the previous place; the 1440 and 720 px layouts, light and dark,
// have no horizontal overflow and keep the sticky top bar.
// Real Python server, no database. Run after `npm run build`:
// node test/settings-page.mjs
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-settings-page-"));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url,file=sys.stderr,flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
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
  browser = await puppeteer.launch({ headless: "shell", args: (process.env.MYFLAMES_BROWSER_ARGS || "").split(" ").filter(Boolean) });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setViewport({ width: 1440, height: 1000 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");

  const MOD = process.platform === "darwin" ? "Meta" : "Control";
  const settings = 'button[aria-label="Settings"]';
  const text = (selector) => page.$eval(selector, (e) => e.textContent);
  const texts = (selector) => page.$$eval(selector, (els) => els.map((e) => e.textContent.trim()));
  const overflow = () => page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  const currentPlace = () => page.$$eval(".main-nav [aria-current=page]", (els) => els.map((e) => e.textContent));
  const headingFocused = () => page.evaluate(() => document.activeElement?.id === "settings-section-title");
  const openSection = async (label) => {
    await page.locator(`.settings-page .source-item ::-p-text(${label})`).click();
    await page.waitForFunction((t) => document.querySelector(".settings-panel-title")?.textContent === t, {}, label);
  };

  // 1. The gear opens the page (Appearance first), not a place: no sidebar
  // place is current, the gear reads as the current spot, and focus moves to
  // the section heading. The list marks the current section.
  assert.deepEqual(await currentPlace(), ["Home"]);
  await page.click(settings);
  await page.waitForSelector(".settings-page");
  assert.equal(await text(".settings-panel-title"), "Appearance");
  assert.equal(await page.$eval(settings, (e) => e.getAttribute("aria-current")), "page");
  assert.deepEqual(await currentPlace(), [], "Settings is a page, not a place");
  assert.ok(await headingFocused(), "focus moves to the section heading");
  assert.equal(await page.$eval("#settings-section-appearance", (e) => e.getAttribute("aria-current")), "page");

  // 2. Each section shows its controls, with its hints.
  const cardNames = await texts(".settings-page .theme-card .theme-card-name");
  assert.deepEqual(cardNames.slice(0, 3), ["System", "Light", "Dark"]);
  assert.equal(cardNames.length, 17);
  await openSection("Query editor");
  await page.waitForSelector(".settings-page .settings-editor-group");
  assert.deepEqual(await texts(".settings-page .settings-editor-group .session-toggle"), [
    "Multiple cursors", "Suggestions while typing", "Close brackets and quotes",
    "Wrap long lines", "Problem warnings", "Details on hover",
  ]);
  assert.ok(await headingFocused(), "a section change moves focus to its heading");
  await openSection("Workspace");
  assert.equal(await text("#session-toggle-hint"), "Off clears the saved workspace. Passwords are never saved in it.");
  assert.equal(await page.$eval(".settings-page .session-toggle input", (e) => e.checked), true);
  assert.ok(await page.$(".settings-panel button ::-p-text(Start fresh)"));
  await openSection("Updates and news");
  assert.equal(await text("#update-auto-hint"), "Asks GitHub for the latest release when myflames opens and once a day.");
  assert.equal(await text("#news-toggle-hint"), "Planet MySQL and the MariaDB Foundation, read over HTTPS while Home is open.");
  // Client tools opens the dialog it has always opened.
  await openSection("Client tools");
  await page.locator(".settings-panel button ::-p-text(Client tools…)").click();
  await page.waitForSelector("dialog.client-tools-dialog[open]");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.client-tools-dialog"));
  // About myflames and the Audit log sit at the bottom of the section list
  // and open what they open everywhere else.
  await page.locator(".settings-page .source-item ::-p-text(About myflames)").click();
  await page.waitForSelector("dialog.about-dialog[open]");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.about-dialog"));
  assert.equal(await page.evaluate(() => document.activeElement?.textContent.trim()), "About myflames", "focus returns to the item that opened About");
  await page.locator(".settings-page .source-item ::-p-text(Audit log)").click();
  await page.waitForSelector(".server-view[data-view=audit]");
  assert.equal(await page.$(".settings-page"), null, "the Audit log replaces the page");
  await page.locator(".main-nav button ::-p-text(Home)").click();
  await page.waitForSelector(".home");

  // 3. Esc and Back (⌘[) return to where the user was; ⌘, and the palette
  // open the page.
  await page.click(settings);
  await page.waitForSelector(".settings-page");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".settings-page"));
  assert.deepEqual(await currentPlace(), ["Home"], "Esc returns to the previous place");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Settings", "focus returns to the gear");
  await page.keyboard.down(MOD); await page.keyboard.press(","); await page.keyboard.up(MOD);
  await page.waitForSelector(".settings-page");
  await page.keyboard.down(MOD); await page.keyboard.press("["); await page.keyboard.up(MOD);
  await page.waitForFunction(() => !document.querySelector(".settings-page"));
  assert.deepEqual(await currentPlace(), ["Home"], "Back returns to the previous place");
  await page.keyboard.down(MOD); await page.keyboard.press("k"); await page.keyboard.up(MOD);
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "Settings");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".settings-page");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".settings-page"));

  // 4. Every setting still saves and survives a reload, exactly as before:
  // the theme (workspace store), a Query editor switch and the news and update
  // switches (the workspace store through their real endpoints).
  await page.click(settings);
  await page.waitForSelector(".settings-page");
  await page.locator('.settings-page .theme-card[data-theme-id="dark"]').click();
  await page.waitForFunction(async () => {
    const reply = await fetch('/api/settings/ui', {method:'POST', headers:{'Content-Type':'application/json', 'X-Myflames-Token':localStorage.getItem('myflames.session-proof')}, body:'{}'}).then(r => r.json());
    return document.documentElement.dataset.theme === "dark" && reply.preferences?.theme === "dark";
  });
  await openSection("Query editor");
  await page.waitForSelector(".settings-page .settings-editor-group");
  let saved = page.waitForResponse((r) => r.url().includes("/api/settings/editor") && (r.request().postData() || "").includes('"soft_wrap":true'));
  await page.locator('.settings-page .settings-editor-group .session-toggle ::-p-text(Wrap long lines)').click();
  await saved;
  await openSection("Updates and news");
  saved = page.waitForResponse((r) => r.url().includes("/api/news/settings") && (r.request().postData() || "").includes('"enabled":false'));
  await page.locator(".settings-page .news-toggle").click();
  await saved;
  await page.waitForFunction(() => document.querySelector(".settings-page .news-toggle input")?.checked === false);
  saved = page.waitForResponse((r) => r.url().includes("/api/app/update/settings") && (r.request().postData() || "").includes('"auto_check":false'));
  await page.locator(".settings-page .update-auto-toggle").click();
  await saved;
  await page.waitForFunction(() => document.querySelector(".settings-page .update-auto-toggle input")?.checked === false);
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");

  assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark", "the reload applies the theme");
  await page.waitForSelector(".settings-page");
  await openSection("Query editor");
  await page.waitForFunction(() => {
    const row = [...document.querySelectorAll(".settings-page .settings-editor-group .session-toggle")]
      .find((e) => e.textContent.trim() === "Wrap long lines");
    return !!row && row.querySelector("input").checked === true;
  });
  await openSection("Updates and news");
  assert.equal(await page.$eval(".settings-page .news-toggle input", (e) => e.checked), false, "the news switch survives a reload");
  assert.equal(await page.$eval(".settings-page .update-auto-toggle input", (e) => e.checked), false, "the update switch survives a reload");
  // And back, the same way.
  saved = page.waitForResponse((r) => r.url().includes("/api/news/settings") && (r.request().postData() || "").includes('"enabled":true'));
  await page.locator(".settings-page .news-toggle").click();
  await saved;
  saved = page.waitForResponse((r) => r.url().includes("/api/app/update/settings") && (r.request().postData() || "").includes('"auto_check":true'));
  await page.locator(".settings-page .update-auto-toggle").click();
  await saved;
  await openSection("Query editor");
  await page.waitForSelector(".settings-page .settings-editor-group");
  saved = page.waitForResponse((r) => r.url().includes("/api/settings/editor") && (r.request().postData() || "").includes('"soft_wrap":false'));
  await page.locator('.settings-page .settings-editor-group .session-toggle ::-p-text(Wrap long lines)').click();
  await saved;
  await openSection("Appearance");
  await page.locator('.settings-page .theme-card[data-theme-id="system"]').click();
  await page.waitForFunction(() => !document.documentElement.dataset.theme);
  // The startup option: off stops autosave and clears the saved workspace,
  // so the reload lands on Home with the switch off; on again saves at once.
  await openSection("Workspace");
  await page.locator(".settings-page .session-toggle").click();
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "off");
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");
  await page.click(settings);
  await page.waitForSelector(".settings-page");
  await openSection("Workspace");
  assert.equal(await page.$eval(".settings-page .session-toggle input", (e) => e.checked), false, "the startup option survives a reload");
  await page.locator(".settings-page .session-toggle").click();
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state === "saved");
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");
  await page.click(settings);
  await page.waitForSelector(".settings-page");
  await openSection("Workspace");
  assert.equal(await page.$eval(".settings-page .session-toggle input", (e) => e.checked), true, "the startup option is back on");

  // 5. Layouts: 1440 and 720 px, light and dark. No horizontal overflow, and
  // the top bar stays put while the page scrolls inside the main area. At
  // 720 px the section list sits above the controls.
  const layout = () => page.evaluate(() => {
    const nav = document.querySelector(".settings-nav")?.getBoundingClientRect();
    const panel = document.querySelector(".settings-panel")?.getBoundingClientRect();
    const topbar = document.querySelector(".window-toolbar")?.getBoundingClientRect();
    let scroller = null;
    for (let e = document.querySelector(".settings-page"); e; e = e.parentElement) {
      if (e.scrollHeight > e.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(e).overflowY)) { scroller = e; break; }
    }
    const before = topbar?.top;
    let scrolled = 0;
    if (scroller) { scroller.scrollTop = 150; scrolled = scroller.scrollTop; scroller.scrollTop = 0; }
    return {
      wide: document.documentElement.scrollWidth - innerWidth,
      above: nav && panel ? nav.bottom <= panel.top + 1 : null,
      sideBySide: nav && panel ? panel.left >= nav.right - 1 : null,
      topbarStays: scrolled > 0 ? Math.abs((document.querySelector(".window-toolbar")?.getBoundingClientRect().top ?? -1) - before) < 1 : "content fits",
      windowStays: document.scrollingElement.scrollTop === 0,
    };
  });
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    for (const [width, height] of [[1440, 900], [720, 520]]) {
      await page.setViewport({ width, height });
      await page.waitForSelector(".settings-page");
      await new Promise((r) => setTimeout(r, 150));
      const m = await layout();
      assert.ok(m.wide <= 0, `${theme} at ${width} px: no horizontal overflow (${JSON.stringify(m)})`);
      assert.ok(m.windowStays, `${theme} at ${width} px: the window does not scroll (${JSON.stringify(m)})`);
      if (width === 720) assert.equal(m.above, true, `${theme} at 720 px: the section list sits above the controls (${JSON.stringify(m)})`);
      else assert.equal(m.sideBySide, true, `${theme} at 1440 px: the section list sits beside the controls (${JSON.stringify(m)})`);
      await page.screenshot({ path: path.join(directory, `settings-${theme}-${width}.png`) });
    }
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await page.setViewport({ width: 1440, height: 1000 });

  assert.deepEqual(errors, []);
  console.log("Settings page checks passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
