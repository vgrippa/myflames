// The Settings page (src/SettingsPage.tsx): the sidebar gear, ⌘, / Ctrl+, and
// the command palette open it; each section shows its controls; every setting
// still saves and survives a reload exactly as before; the Appearance Text
// group applies the interface and code font and size choices to body text,
// the Query editor's content rule and code text at once, offers only shipped
// or detected fonts, and keeps the page free of sideways scroll at the
// largest sizes; Back (⌘[) and Esc return to the previous place; the 1440 and
// 720 px layouts, light and dark, have no horizontal overflow and keep the
// sticky top bar.
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
  // Hold the first preference read past bounded startup: the new select
  // must not overwrite a stored value before that read arrives.
  let releasePreferences;
  await page.setRequestInterception(true);
  page.on("request", request => {
    if (!releasePreferences && request.url().endsWith("/api/settings/ui") && request.postData() === "{}") {
      releasePreferences = () => { void request.continue(); };
    } else void request.continue();
  });
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => {
    const status = document.querySelector(".session-status");
    return status && status.dataset.state !== "loading";
  });

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
  assert.equal(await text('label[for="diagram-zoom-speed"]'), "Diagram wheel zoom speed");
  assert.equal(await text("#diagram-zoom-speed-hint"), "How far one mouse-wheel step zooms the Database diagram and the Mermaid preview.");
  assert.equal(await page.$eval("#diagram-zoom-speed", e => e.disabled), true, "speed is disabled while preferences load");
  assert.ok(releasePreferences); releasePreferences();
  await page.waitForFunction(() => document.querySelector("#diagram-zoom-speed")?.disabled === false);
  assert.equal(await page.$eval("#diagram-zoom-speed", e => e.value), "100");
  assert.deepEqual(await page.$$eval("#diagram-zoom-speed option", els => els.map(e => [e.value,e.textContent])),
    [["25","Slowest"],["50","Slow"],["100","Normal"],["150","Fast"],["200","Fastest"]]);
  await openSection("Updates and news");
  assert.equal(await text("#update-auto-hint"), "Asks GitHub for the latest release when myflames opens and once a day.");
  // The installed version, update status and actions moved here from the
  // status bar.
  const updatesRow = await text(".settings-update-status");
  assert.match(updatesRow, /myflames \d+\.\d+\.\d+/, "the section names the installed version");
  assert.match(updatesRow, /Check for updates/, "Check for updates is here");
  assert.equal(await page.$eval(".settings-update-status a", (e) => e.textContent), "View releases");
  assert.equal(await page.$eval(".settings-update-status a", (e) => e.href), "https://github.com/vgrippa/myflames/releases");
  const installedVersion = await text(".brand-version");
  const footerText = await text(".window-statusbar");
  assert.equal(footerText.includes(installedVersion), false, "the status bar holds no installed version text");
  assert.doesNotMatch(footerText, /myflames|Check for updates|View releases/, "update actions live in Settings");
  assert.equal(await page.$eval(".window-statusbar", (e) => e.tabIndex), -1, "the empty Settings footer is not a tab stop");
  // Re-enter the same section through the version with the keyboard. The
  // section value does not change, so this needs a fresh focus request.
  for (const key of ["Enter", "Space"]) {
    await page.focus(".brand-version");
    await page.keyboard.press(key);
    await page.waitForFunction(() => document.activeElement?.id === "settings-section-title");
    assert.equal(await text(".settings-panel-title"), "Updates and news");
  }
  assert.equal(await page.$eval(".settings-update-status .update-check", (e) => e.getAttribute("role")), "status");
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
  await page.type('input[aria-label="Search commands"]', "zoom");
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
  await openSection("Workspace");
  for (const speed of ["25","50","100","150","200"]) {
    const saving = page.waitForResponse(r => r.url().endsWith("/api/settings/ui") &&
      (r.request().postData() || "").includes(`"diagram_zoom_speed":${speed}`));
    await page.select("#diagram-zoom-speed", speed);
    assert.equal((await saving).status(),200);
  }
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
  await openSection("Workspace");
  await page.waitForFunction(() => document.querySelector("#diagram-zoom-speed")?.value === "200");
  assert.equal(await page.$eval("#diagram-zoom-speed", e => e.disabled), false, "saved speed is usable after reload");
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

  // 5. Text settings (Settings > Appearance): the interface and code font
  // and size choices apply at once to body text, to the Query editor's
  // content rule and to code text; they survive a reload; both size range
  // ends are reachable; a missing installed font is not offered.
  await openSection("Appearance");
  await page.waitForFunction(() => document.querySelector("#ui-font")?.disabled === false);
  const optionTexts = (id) => page.$$eval(`#${id} option`, (els) => els.map((e) => e.textContent));
  assert.ok((await optionTexts("ui-font")).includes("Inter (included)"), "Inter is offered as included");
  assert.ok((await optionTexts("code-font")).includes("JetBrains Mono (included)"), "JetBrains Mono is offered as included");
  assert.deepEqual(await page.$$eval("#ui-text-size option", (els) => els.map((e) => [e.value, e.textContent])),
    [["12", "Smaller"], ["13", "Default"], ["14", "Large"], ["15", "Larger"], ["16", "Largest"]]);
  assert.deepEqual(await page.$$eval("#code-text-size option", (els) => els.map((e) => e.value)),
    ["11", "12", "13", "14", "15", "16", "17", "18", "19", "20"]);
  // The editor probe carries the Query editor content's real CSS classes:
  // CodeMirror sets no inline font on the content, so its computed style is
  // exactly the editor-cm.css rule.
  const textMetrics = () => page.evaluate(() => {
    const probe = document.createElement("div");
    probe.className = "sql-editor-wrap sql-cm";
    probe.style.cssText = "position:absolute;visibility:hidden;pointer-events:none";
    probe.innerHTML = '<div class="cm-editor"><div class="cm-scroller"><div class="cm-content"><div class="cm-line">x</div></div></div></div>';
    document.body.appendChild(probe);
    const read = (element) => {
      const style = getComputedStyle(element);
      return { family: style.fontFamily, size: style.fontSize };
    };
    // Real selector ancestry catches code overrides that a generic <code>
    // probe misses. Keep the original base sizes when the setting is 13.
    const surfaces = [
      ["generic pre", 12, '<pre>SELECT 1</pre>', "pre"],
      ["generic code", 12, '<code>SELECT 1</code>', "code"],
      ["reviewed write", 12, '<div class="editor-confirm"><pre class="sheet-statements">DELETE FROM orders</pre></div>', "pre"],
      ["expanded plan SQL", 12, '<div class="plan-header-tools"><details class="sql-card" open><pre class="plan-header-panel">SELECT 1</pre></details></div>', "pre"],
      ["plan JSON", 12, '<div class="plan-inspector"><div class="raw-fields"><pre>{}</pre></div></div>', "pre"],
      ["digest SQL", 12, '<div class="qd-code"><pre>SELECT 1</pre></div>', "pre"],
      ["digest table", 12, '<table class="data-table"><tbody><tr><td><code class="qd-statement">SELECT 1</code></td></tr></tbody></table>', "code"],
      ["Locks/Sessions statement", 12, '<table class="data-table"><tbody><tr><td><code class="cell-sql">SELECT 1</code></td></tr></tbody></table>', "code"],
      ["Locks/Sessions expanded statement", 12, '<pre class="inspector-sql"><code>SELECT 1</code></pre>', "code"],
      ["SQL analyzed", 12, '<details class="sent-sql" open><summary>SQL analyzed</summary><pre>SELECT 1</pre></details>', "pre"],
      ["Explain JSON", 12, '<pre class="row-panel-json">{}</pre>', "pre"],
      ["history SQL", 12, '<div class="history-sql">SELECT 1</div>', ".history-sql"],
      ["message SQL", 12, '<code class="message-sql">SELECT 1</code>', "code"],
      ["trace value", 11, '<div class="trace-value"><code>orders</code></div>', "code"],
      ["Session SQL filter", 11, '<div class="mass-kill-field"><input type="text" class="cell-sql" value="SELECT"></div>', "input"],
    ];
    const codeSurfaces = surfaces.map(([name, base, html, selector]) => {
      const container = document.createElement("div");
      container.innerHTML = html;
      probe.appendChild(container);
      const size = getComputedStyle(container.querySelector(selector)).fontSize;
      container.remove();
      return { name, base, size };
    });
    const value = {
      codeSurfaces,
      bodyText: read(document.querySelector('[data-preview="interface"]')),
      codeText: read(document.querySelector('[data-preview="code"] code')),
      editor: read(probe.querySelector(".cm-editor")),
      editorLine: getComputedStyle(probe.querySelector(".cm-line")).lineHeight,
    };
    probe.remove();
    return value;
  });
  const assertCodeMetrics = (metrics, size) => {
    assert.equal(metrics.editorLine, `${Math.round(size * 20 / 13)}px`,
      `editor line height follows code size ${size}`);
    for (const surface of metrics.codeSurfaces) {
      assert.ok(Math.abs(parseFloat(surface.size) - surface.base * size / 13) < 0.01,
        `${surface.name}: ${surface.size}, expected ${surface.base} * ${size} / 13 px`);
    }
  };
  // A computed font-family can name a font that failed to download. Require
  // a loaded @font-face, so a missing bundled URL fails this assertion.
  const assertBundledFontLoaded = async (family, size) => {
    const loaded = await page.evaluate(async (family, size) => {
      const specification = `400 ${size}px "${family}"`;
      try {
        const faces = await document.fonts.load(specification, "SELECT id FROM orders");
        return { checked: document.fonts.check(specification),
          faces: faces.map(face => ({ family: face.family.replace(/["']/g, ""), status: face.status })) };
      } catch (error) { return { error: String(error), faces: [] }; }
    }, family, size);
    assert.ok(loaded.checked && loaded.faces.some(face => face.family === family && face.status === "loaded"),
      `${family} must load from its bundled font file: ${JSON.stringify(loaded)}`);
  };
  let metrics = await textMetrics();
  assertCodeMetrics(metrics, 13);
  assert.deepEqual([metrics.bodyText.size, metrics.codeText.size, metrics.editor.size], ["13px", "13px", "13px"], "the defaults keep today's sizes");
  const choose = async (id, value, fragment) => {
    const saving = page.waitForResponse((r) => r.url().endsWith("/api/settings/ui") &&
      (r.request().postData() || "").includes(fragment));
    await page.select(`#${id}`, value);
    assert.equal((await saving).status(), 200);
  };
  await choose("ui-font", "inter", '"ui_font":"inter"');
  await choose("ui-text-size", "16", '"ui_text_size":16');
  assertCodeMetrics(await textMetrics(), 13);
  await assertBundledFontLoaded("Inter", 16);
  await choose("code-font", "jetbrains-mono", '"code_font":"jetbrains-mono"');
  await choose("code-text-size", "20", '"code_text_size":20');
  metrics = await textMetrics();
  assertCodeMetrics(metrics, 20);
  // The review's independent-size case: interface stays at 13, code at 20.
  await choose("ui-text-size", "13", '"ui_text_size":13');
  assertCodeMetrics(await textMetrics(), 20);
  await choose("ui-text-size", "16", '"ui_text_size":16');
  await assertBundledFontLoaded("JetBrains Mono", 20);
  assert.ok(metrics.bodyText.family.includes("Inter") && metrics.bodyText.size === "16px",
    `the interface font and size reach body text (${JSON.stringify(metrics.bodyText)})`);
  assert.ok(metrics.codeText.family.includes("JetBrains Mono") && metrics.codeText.size === "20px",
    `the code font and size reach code text (${JSON.stringify(metrics.codeText)})`);
  assert.ok(metrics.editor.family.includes("JetBrains Mono") && metrics.editor.size === "20px",
    `the code font and size reach the Query editor content (${JSON.stringify(metrics.editor)})`);
  // Largest interface text and the largest code size: no sideways scroll.
  for (const width of [1440, 720]) {
    await page.setViewport({ width, height: 900 });
    await page.waitForSelector(".settings-page");
    await new Promise((r) => setTimeout(r, 150));
    assert.equal(await overflow(), 0, `Largest text at ${width} px: no horizontal overflow`);
  }
  await page.setViewport({ width: 1440, height: 1000 });
  // The range's other ends are reachable too.
  await choose("ui-text-size", "12", '"ui_text_size":12');
  await choose("code-text-size", "11", '"code_text_size":11');
  metrics = await textMetrics();
  assertCodeMetrics(metrics, 11);
  assert.deepEqual([metrics.bodyText.size, metrics.codeText.size, metrics.editor.size], ["12px", "11px", "11px"],
    "the smallest sizes apply");
  // The choices survive a reload.
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");
  await page.waitForSelector(".settings-page");
  await openSection("Appearance");
  await page.waitForFunction(() => document.querySelector("#ui-font")?.disabled === false);
  assert.deepEqual([
    await page.$eval("#ui-font", (e) => e.value),
    await page.$eval("#ui-text-size", (e) => e.value),
    await page.$eval("#code-font", (e) => e.value),
    await page.$eval("#code-text-size", (e) => e.value),
  ], ["inter", "12", "jetbrains-mono", "11"], "the text choices survive a reload");
  metrics = await textMetrics();
  assertCodeMetrics(metrics, 11);
  await assertBundledFontLoaded("Inter", 12);
  await assertBundledFontLoaded("JetBrains Mono", 11);
  assert.ok(metrics.bodyText.family.includes("Inter") && metrics.bodyText.size === "12px" && metrics.codeText.size === "11px",
    "the reloaded choices are applied");
  // Restore default text settings resets the four preferences.
  const restoring = page.waitForResponse((r) => r.url().endsWith("/api/settings/ui") &&
    (r.request().postData() || "").includes('"code_text_size":13'));
  await page.locator(".settings-panel button ::-p-text(Restore default text settings)").click();
  assert.equal((await restoring).status(), 200);
  await page.waitForFunction(() => document.querySelector("#ui-font")?.value === "system"
    && document.querySelector("#code-font")?.value === "system");
  metrics = await textMetrics();
  assertCodeMetrics(metrics, 13);
  assert.deepEqual([metrics.bodyText.size, metrics.codeText.size, metrics.editor.size], ["13px", "13px", "13px"], "Restore default text settings applies");
  // With font detection reporting nothing installed, only System default and
  // the shipped fonts are offered; the persisted defaults survive.
  await page.evaluateOnNewDocument(() => { window.__myflamesDetectFont = () => false; });
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForFunction(() => document.querySelector(".session-status")?.dataset.state !== "loading");
  await page.waitForSelector(".settings-page");
  await openSection("Appearance");
  await page.waitForFunction(() => document.querySelector("#ui-font")?.disabled === false);
  assert.deepEqual(await optionTexts("ui-font"), ["System default", "Inter (included)"],
    "no missing installed interface font is offered");
  assert.deepEqual(await optionTexts("code-font"), ["System default", "JetBrains Mono (included)"],
    "no missing installed code font is offered");
  assert.deepEqual([await page.$eval("#ui-font", (e) => e.value), await page.$eval("#code-font", (e) => e.value)],
    ["system", "system"], "the restored defaults survive a reload");

  // 6. Layouts: 1440 and 720 px, light and dark. No horizontal overflow, and
  // the top bar and sidebar stay put while the page scrolls. At
  // 720 px the section list sits above the controls.
  await openSection("Appearance");
  const layout = () => page.evaluate(async () => {
    const nav = document.querySelector(".settings-nav")?.getBoundingClientRect();
    const panel = document.querySelector(".settings-panel")?.getBoundingClientRect();
    const topbar = document.querySelector(".window-toolbar")?.getBoundingClientRect();
    const sidebar = document.querySelector(".sidebar")?.getBoundingClientRect();
    let scroller = null;
    for (let e = document.querySelector(".settings-page"); e; e = e.parentElement) {
      if (e.scrollHeight > e.clientHeight + 1 && /auto|scroll/.test(getComputedStyle(e).overflowY)) { scroller = e; break; }
    }
    scroller ||= document.scrollingElement;
    const before = topbar?.top;
    let scrolled = 0;
    if (scroller) { scroller.scrollTop = 150; await new Promise(requestAnimationFrame); scrolled = scroller.scrollTop; }
    const sidebarAfter = document.querySelector(".sidebar")?.getBoundingClientRect();
    return {
      wide: document.documentElement.scrollWidth - innerWidth,
      above: nav && panel ? nav.bottom <= panel.top + 1 : null,
      sideBySide: nav && panel ? panel.left >= nav.right - 1 : null,
      scrolled,
      topbarStays: scrolled > 0 ? Math.abs((document.querySelector(".window-toolbar")?.getBoundingClientRect().top ?? -1) - before) < 1 : false,
      sidebarStays: sidebar && sidebarAfter ? Math.abs(sidebarAfter.left - sidebar.left) < 1 && Math.abs(sidebarAfter.top - sidebar.top) < 1 : false,
    };
  });
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
    // The wide panel needs a shorter viewport for its multi-row theme grid to
    // overflow; keep 720 px at the minimum supported window height.
    for (const [width, height] of [[1440, 360], [720, 520]]) {
      await page.setViewport({ width, height });
      await page.waitForSelector(".settings-page");
      await new Promise((r) => setTimeout(r, 150));
      const m = await layout();
      assert.ok(m.wide <= 0, `${theme} at ${width} px: no horizontal overflow (${JSON.stringify(m)})`);
      assert.ok(m.scrolled > 0, `${theme} at ${width} px: Appearance genuinely overflows (${JSON.stringify(m)})`);
      assert.equal(m.topbarStays, true, `${theme} at ${width} px: toolbar stays at the top while scrolled (${JSON.stringify(m)})`);
      assert.ok(m.sidebarStays, `${theme} at ${width} px: sidebar stays put while scrolled (${JSON.stringify(m)})`);
      if (width === 720) assert.equal(m.above, true, `${theme} at 720 px: the section list sits above the controls (${JSON.stringify(m)})`);
      else assert.equal(m.sideBySide, true, `${theme} at 1440 px: the section list sits beside the controls (${JSON.stringify(m)})`);
      await page.evaluate(() => { for (let e = document.querySelector(".settings-page"); e; e = e.parentElement) e.scrollTop = 0; });
      await page.screenshot({ path: path.join(directory, `settings-${theme}-${width}.png`) });
      await openSection("Workspace");
      assert.equal(await overflow(),0,`Workspace at ${width} px has no horizontal overflow`);
      assert.ok(await page.$eval("#diagram-zoom-speed", e => { const r=e.getBoundingClientRect();return r.width>0&&r.left>=0&&r.right<=innerWidth; }));
      await page.screenshot({ path: path.join(directory, `settings-workspace-${theme}-${width}.png`) });
      await openSection("Appearance");
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
