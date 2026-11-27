// Complete investigation workflow. Optional live DB is an explicitly supplied test server.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(
  path.join(os.tmpdir(), "myflames-investigation-"),
);
// Exercise first-use chunk races in fresh pages with independent module
// registries in the authenticated browser context. Session reads/saves are
// mocked, but a closing page can still flush a final keepalive save past
// interception, so these scenarios run after the main workflow has finished
// with the store.
async function lazyPlanRegressions(browser, launchUrl) {
  const raw = await fs.readFile(path.join(root, "apps/workspace/src/example.json"), "utf8");
  const origin = new URL(launchUrl).origin;
  const saveButton = '.topbar button[aria-label="Save investigation"]';
  const sheet = 'dialog.notebook-sheet[open]';
  const chunkOf = (request) => new URL(request.url()).pathname
    .match(/^\/assets\/(PlanScreen|PlanChart|ContextPanel|ImportPlanDialog)-[^/]+\.js$/)?.[1];
  async function scenario({ plan = true, fail = [], hold = [] }, check) {
    const page = await browser.newPage();
    const failed = new Set(fail), delayed = new Set(hold), requested = new Set();
    const held = [], errors = [], interceptionErrors = [];
    const state = {
      schema_version: "session-1.0", mode: plan ? "inspect" : "home",
      view: "workbench", metric: "self_time", active_plan_id: plan ? "lazy-plan" : "",
      plans: plan ? [{ id: "lazy-plan", name: "Lazy plan", raw, origin: "sample" }] : [],
      investigation: { name: "Lazy investigation", notes: "Keep these notes", tags: "lazy", dirty: false },
    };
    let revision = 0;
    const json = (request, body) => request.respond({
      status: 200, contentType: "application/json", body: JSON.stringify(body),
    });
    const failChunk = (request) => request.respond({
      status: 404, contentType: "text/javascript", body: "// Simulated stale chunk.",
    });
    async function intercept(request) {
      const pathname = new URL(request.url()).pathname;
      if (pathname === "/api/session/load") return json(request, {
        revision, saved_at: "2026-11-17T12:00:00Z", restore: true, skip_restore: false, state,
      });
      if (pathname === "/api/session/save") return json(request, {
        revision: ++revision, saved_at: "2026-11-17T12:00:01Z",
      });
      if (pathname === "/api/investigations/list") return json(request, {
        schema_version: "investigation-list-1.0", items: [], next_cursor: null,
      });
      const chunk = chunkOf(request);
      if (chunk) {
        requested.add(chunk);
        if (delayed.has(chunk)) { held.push(request); return; }
        if (failed.has(chunk)) return failChunk(request);
      }
      return request.continue();
    }
    const requestListener = (request) => {
      void intercept(request).catch((error) => interceptionErrors.push(String(error)));
    };
    const chunkRequested = async (name) => {
      if (!requested.has(name)) await page.waitForRequest((request) => chunkOf(request) === name);
    };
    const release = async (name, reject = false) => {
      delayed.delete(name);
      const matching = held.filter((request) => chunkOf(request) === name);
      assert.ok(matching.length, `${name} was actually delayed`);
      for (const request of matching) {
        held.splice(held.indexOf(request), 1);
        if (reject) await failChunk(request);
        else await request.continue();
      }
    };
    const place = (label) => page.locator(`.main-nav button ::-p-text(${label})`).click();
    const expanded = () => page.$eval(saveButton, (button) => button.getAttribute("aria-expanded"));
    try {
      page.on("pageerror", (error) => errors.push(String(error)));
      await page.setViewport({ width: 1440, height: 1000 });
      await page.setCacheEnabled(false);
      await page.setRequestInterception(true);
      page.on("request", requestListener);
      // Held requests intentionally keep the network busy. Wait for the
      // shell, not networkidle, before interacting with its controls.
      await page.goto(origin, { waitUntil: "domcontentloaded" });
      await page.waitForSelector(plan ? saveButton : '.workspace[data-place="home"] .home');
      await check({ page, place, release, chunkRequested, expanded, failed, requested });
      assert.deepEqual(interceptionErrors, [], "all intercepted requests were handled");
      assert.deepEqual(errors, [], "chunk failures do not escape their loading boundary");
    } finally {
      // Resolve paused requests before closing, including after an assertion.
      await Promise.allSettled(held.splice(0).map((request) => request.abort("aborted")));
      await page.close();
    }
  }

  // 1. A failed Plans chunk keeps its failure and Reload in view. Saving
  // opens an eager notebook on every press, including after a cached failure.
  await scenario({ fail: ["PlanScreen"] }, async ({ page, expanded, failed }) => {
    await page.waitForSelector('#main [role="alert"] button');
    assert.match(await page.$eval('#main [role="alert"]', (el) => el.textContent), /did not load/);
    for (let press = 0; press < 2; press++) {
      await page.click(saveButton);
      await page.waitForSelector(sheet);
      assert.equal(await expanded(), "true");
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => !document.querySelector('dialog.notebook-sheet'));
      assert.equal(await expanded(), "false");
      assert.equal(await page.$eval('#main [role="alert"] button', (el) => el.textContent), "Reload");
    }
    // A page reload really retries the failed module, rather than merely
    // toggling aria-expanded on React.lazy's cached fallback.
    failed.delete("PlanScreen");
    await Promise.all([
      page.waitForNavigation({ waitUntil: "networkidle0" }),
      page.click('#main [role="alert"] button'),
    ]);
    await page.waitForSelector('.plan-heading');
    assert.equal(await page.$('#main > .view-status [role="alert"]'), null);
  });
  await scenario({ hold: ["PlanScreen"] }, async ({ page, place, chunkRequested, release }) => {
    await chunkRequested("PlanScreen");
    await place("Investigations");
    await page.locator('.library button ::-p-text(Save 1 open plan as an investigation)').click();
    await page.waitForSelector(sheet);
    await release("PlanScreen", true);
    // The sheet has no chunk dependency, so a failed idle/pending Plans
    // request cannot close it or remove its fields and save controls.
    await page.waitForNetworkIdle();
    assert.equal(await page.$eval('.investigation-notes textarea', (el) => el.value), "Keep these notes");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector('dialog.notebook-sheet'));
    await page.locator('.library button ::-p-text(Save 1 open plan as an investigation)').click();
    await page.waitForSelector(sheet);
  });

  // 2. Each chart dependency can fail without taking down the empty Plans
  // screen, its Import control or the notebook for an already-open plan.
  for (const chunk of ["PlanChart", "ContextPanel"]) {
    await scenario({ plan: false, fail: [chunk] }, async ({ page, place, chunkRequested }) => {
      await place("Plans");
      await chunkRequested(chunk);
      await page.waitForSelector('#plans-empty-title');
      assert.equal(await page.$eval('#plans-empty-title', (el) => el.textContent), "No plan is open");
      assert.equal(await page.$('#main [role="alert"]'), null);
      await page.click('.topbar .plans-import');
      await page.waitForSelector('dialog.import-dialog[open]');
    });
    await scenario({ fail: [chunk] }, async ({ page, chunkRequested, expanded }) => {
      await chunkRequested(chunk);
      await page.waitForSelector('.plan-heading');
      await page.waitForSelector('.plan-fold [role="alert"] button');
      assert.equal(await page.$('#main > .view-status [role="alert"]'), null,
        `${chunk} fails only inside its chart/inspector area`);
      await page.click(saveButton);
      await page.waitForSelector(sheet);
      assert.equal(await expanded(), "true");
    });
  }

  // 3. Release the screen after another dialog has opened. Dispatch both
  // actions in one turn to cover a request made just before React commits.
  await scenario({ hold: ["PlanScreen", "PlanChart", "ContextPanel"] },
    async ({ page, chunkRequested, release, expanded }) => {
      for (const name of ["PlanScreen", "PlanChart", "ContextPanel"]) await chunkRequested(name);
      // An ordinary press opens immediately even while all three chunks wait.
      await page.click(saveButton);
      await page.waitForSelector(sheet);
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => !document.querySelector('dialog.notebook-sheet'));
      await page.evaluate(() => {
        document.querySelector('.topbar .window-save').click();
        document.querySelector('.topbar .plans-import').click();
      });
      await page.waitForSelector('dialog.import-dialog[open]');
      await page.focus('.import-dialog textarea');
      const focus = await page.evaluate(() => document.activeElement.tagName);
      for (const name of ["PlanScreen", "PlanChart", "ContextPanel"]) await release(name);
      await page.waitForSelector('.plan-heading');
      await page.waitForNetworkIdle();
      assert.equal(await page.$$eval('dialog[open]', (els) => els.length), 1);
      assert.equal(await page.$(sheet), null);
      assert.equal(await expanded(), "false");
      assert.equal(await page.evaluate(() => document.activeElement.tagName), focus);
      assert.ok(await page.evaluate(() => document.querySelector('.import-dialog').contains(document.activeElement)));
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => !document.querySelector('dialog[open]'));
      assert.equal(await page.$('dialog.notebook-sheet'), null, "closing Import does not replay the notebook request");
    });
  await scenario({ hold: ["PlanScreen"] }, async ({ page, chunkRequested, release }) => {
    await chunkRequested("PlanScreen");
    await page.evaluate(() => {
      document.querySelector('.topbar .window-save').click();
      [...document.querySelectorAll('.main-nav button')]
        .find((button) => button.textContent.trim() === "Investigations").click();
    });
    await page.waitForSelector('.workspace[data-place="investigations"] .library');
    await release("PlanScreen");
    await page.waitForNetworkIdle();
    assert.equal(await page.$('dialog.notebook-sheet'), null, "leaving Plans cancels the request, including a trip to Investigations");
  });

  // 4. Drop before the empty-state component exists. Check hit testing so
  // this exercises the full loading surface, not just a synthetic main node.
  await scenario({ plan: false, hold: ["PlanScreen", "PlanChart", "ContextPanel"] },
    async ({ page, place, chunkRequested, release }) => {
      let analyses = 0;
      const analyzed = (request) => { if (new URL(request.url()).pathname === "/api/analyze") analyses++; };
      page.on("request", analyzed);
      await place("Plans");
      for (const name of ["PlanScreen", "PlanChart", "ContextPanel"]) await chunkRequested(name);
      assert.equal(await page.$('.plans-empty'), null, "the lazy drop target has not mounted");
      const accepted = await page.evaluate((raw) => {
        const main = document.querySelector('#main'), box = main.getBoundingClientRect();
        const target = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
        if (!main.contains(target)) throw Error("The loading area is not a drop target");
        const dataTransfer = new DataTransfer();
        dataTransfer.items.add(new File([raw], "dropped-before-plans.json", { type: "application/json" }));
        const drag = new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer });
        target.dispatchEvent(drag);
        target.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer }));
        return drag.defaultPrevented;
      }, raw);
      assert.equal(accepted, true, "the shell accepts the file drop");
      await page.waitForFunction(() => [...document.querySelectorAll('.plan-item')]
        .some((el) => el.textContent.includes("dropped-before-plans")));
      assert.equal(analyses, 1, "the drop is imported exactly once while the screen waits");
      await release("PlanScreen");
      // The plan header renders while both chart dependencies still wait.
      await page.waitForSelector('.plan-heading');
      assert.match(await page.$eval('.plan-heading h1', (el) => el.textContent), /dropped-before-plans/);
      for (const name of ["PlanChart", "ContextPanel"]) await release(name);
      await page.waitForNetworkIdle();
      assert.equal((await page.$$('.plan-item')).length, 1);
      assert.equal(analyses, 1, "mounting PlanScreen neither loses nor reimports the dropped file");
      page.off("request", analyzed);
    });
  // Import opens only after its form is ready. Escape cancels a pending open,
  // and resolving the chunk later cannot steal focus or reopen the dialog.
  await scenario({ hold: ["ImportPlanDialog"] }, async ({ page, chunkRequested, release }) => {
    await chunkRequested("ImportPlanDialog");
    await page.click('.topbar .plans-import');
    await page.waitForFunction(() => [...document.querySelectorAll('#main [role="status"]')]
      .some((el) => el.textContent.includes("Opening Import plan")));
    assert.equal(await page.$('dialog.import-dialog'), null);
    assert.equal(await page.$('.app > .view-status'), null, "loading never adds a column to the shell");
    await page.keyboard.press("Escape");
    await release("ImportPlanDialog");
    await page.waitForNetworkIdle();
    assert.equal(await page.$('dialog.import-dialog'), null, "Escape cancels the opening, including after the chunk arrives");
    assert.equal(await page.$eval('.topbar .plans-import', (el) => document.activeElement === el), true);
    await page.click('.topbar .plans-import');
    await page.waitForSelector('dialog.import-dialog[open]');
    assert.ok(await page.evaluate(() => document.querySelector('.import-dialog').contains(document.activeElement)));
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector('dialog.import-dialog'));
    assert.equal(await page.$eval('.topbar .plans-import', (el) => document.activeElement === el), true);
  });
  // A dialog that opens while the Import chunk loads wins the screen: the
  // pending open is cancelled instead of closing the newer notebook.
  await scenario({ hold: ["ImportPlanDialog"] }, async ({ page, chunkRequested, release, expanded }) => {
    await chunkRequested("ImportPlanDialog");
    await page.click('.topbar .plans-import');
    await page.waitForFunction(() => [...document.querySelectorAll('#main [role="status"]')]
      .some((el) => el.textContent.includes("Opening Import plan")));
    await page.click(saveButton);
    await page.waitForSelector(sheet);
    assert.equal(await expanded(), "true");
    await release("ImportPlanDialog");
    await page.waitForNetworkIdle();
    assert.equal(await page.$$eval('dialog[open]', (els) => els.length), 1, "the notebook stays the only dialog");
    assert.equal(await page.$('dialog.import-dialog'), null, "the pending Import open does not interrupt the notebook");
    assert.equal(await page.$eval('.investigation-notes textarea', (el) => el.value), "Keep these notes");
    await page.waitForFunction(() => ![...document.querySelectorAll('#main [role="status"]')]
      .some((el) => el.textContent.includes("Opening Import plan")));
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector('dialog.notebook-sheet'));
    assert.equal(await page.$('dialog.import-dialog'), null, "closing the notebook does not replay the pending open");
    // A later press opens Import normally: the cancellation left no state.
    await page.click('.topbar .plans-import');
    await page.waitForSelector('dialog.import-dialog[open]');
  });
  await scenario({ fail: ["ImportPlanDialog"] }, async ({ page, expanded, failed }) => {
    await page.click('.topbar .plans-import');
    await page.waitForSelector('.import-open-error[role="alert"]');
    assert.equal(await page.$('dialog.import-dialog'), null);
    assert.equal(await page.$('.app > .view-status'), null, "a failed import never replaces the modal with a shell column");
    await page.click(saveButton);
    await page.waitForSelector(sheet);
    assert.equal(await expanded(), "true", "a failed Import does not suppress Save investigation");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector('dialog.notebook-sheet'));
    await page.locator('.import-open-error button ::-p-text(Dismiss)').click();
    assert.equal(await page.$('.import-open-error'), null);
    assert.equal(await page.$eval('.topbar .plans-import', (el) => document.activeElement === el), true);
    await page.click('.topbar .plans-import');
    await page.waitForSelector('.import-open-error[role="alert"]');
    failed.delete("ImportPlanDialog");
    await page.locator('.import-open-error button ::-p-text(Retry)').click();
    await page.waitForSelector('dialog.import-dialog[open]');
    assert.equal(await page.$('.import-open-error'), null, "Retry loads a fresh chunk after the original module fetch failed");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector('dialog.import-dialog'));
    assert.equal(await page.$eval('.topbar .plans-import', (el) => document.activeElement === el), true);
  });
  console.log("Lazy Plans and Import failures, retries, modal ownership and loading-area drops passed.");
}

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
  browser = await puppeteer.launch({ headless: "shell", args: process.env.MYFLAMES_NO_SANDBOX ? ["--no-sandbox"] : [] });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.setViewport({ width: 1512, height: 1050 });
  await page.goto(url, { waitUntil: "networkidle0" });
  const client = await page.createCDPSession();
  await client.send("Browser.setDownloadBehavior", {
    behavior: "allow",
    downloadPath: directory,
  });
  const button = (text, scope = "") =>
    page.locator(`${scope}button ::-p-text(${text})`).click();
  // The inspect chart is drawn in the page (PlanChart), not in an iframe; its
  // operator rows are docked in the inspector's Operators section.
  const M = ".plan-fold .inspector-metrics .metric-row";
  const inspectorSection = (section) =>
    page.click(`.plan-fold .segmented[aria-label="Inspector section"] button[data-value="${section}"]`);
  async function frame() {
    await page.waitForNetworkIdle();
    await page.waitForSelector(M);
    assert.equal(await page.$(".plan-canvas iframe"), null);
  }
  // Focus/collapse/metric re-project the chart client-side; wait for it.
  async function metricRows(min, max = min) {
    await page.waitForFunction(
      (selector, low, high) => {
        const n = document.querySelectorAll(selector).length;
        return n >= low && n <= high;
      },
      {},
      M,
      min,
      max,
    );
    return page.$$eval(M, (els) => els.length);
  }
  async function select(label) {
    await page.$$eval(
      M,
      (els, text) => els.find((e) => e.textContent.includes(text)).click(),
      label,
    );
  }
  await button("Explore sample plan");
  await frame();
  assert.equal(await page.$(".save-state"), null, "the read-only sample is clean");
  await select("Table scan [categories]");
  await inspectorSection("lessons");
  await button("Learn this operator");
  await page.waitForSelector(".lesson-frame");
  const operatorLesson = await page.$eval('.lesson-frame', el => el.title);
  await button("Next lesson");
  await page.waitForFunction(title => document.querySelector('.lesson-frame')?.title !== title,
    {}, operatorLesson);
  // Return through the plan after navigating to another lesson: the
  // operator link must reopen the matching lesson.
  const lessonRequests = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/teach/lesson")) lessonRequests.push(JSON.parse(request.postData() || "{}"));
  });
  await button("Back to plan");
  await frame();
  await inspectorSection("lessons");
  await button("Learn this operator");
  await page.waitForFunction(title => document.querySelector('.lesson-frame')?.title === title,
    {}, operatorLesson);
  assert.ok(lessonRequests.length > 0, "the operator lesson was requested");
  assert.ok(lessonRequests.every((body) => !Object.hasOwn(body, "view")),
    "lesson requests need only the lesson key");
  await button("Back to plan");
  await frame();
  assert.match(
    await page.$eval(".operator-detail h3", (e) => e.textContent),
    /categories/,
  );
  await select("Nested loop inner join");
  await button("Focus branch");
  const focused = await metricRows(2, 7);
  assert.ok(focused < 8 && focused > 1);
  await button("Collapse branch");
  assert.equal(await metricRows(1), 1);
  // The Operators list holds the runtime's rows, so only operators the
  // projection shows can be picked there: expand the branch, pick a
  // descendant, then leave the focus for the entire plan.
  await button("Expand branch");
  assert.equal(await metricRows(2, 7), focused);
  await select("Table scan [categories]");
  await page.waitForFunction(
    (selector) =>
      document.querySelector(selector + ".is-selected")?.textContent
        .includes("Table scan [categories]"),
    {},
    M,
  );
  await page.select('[aria-label="Focused branch"]', "");
  assert.equal(await metricRows(8), 8);
  await select("Limit: 20 rows");
  await page.select('[aria-label="Operator metric"]', "estimate_error");
  await page.waitForFunction(() =>
    /Estimate error/.test(
      document.querySelector(".plan-fold .inspector-metrics .metric-panel h2")?.textContent,
    ),
  );
  // Projection changes keep the page selection on the chart.
  assert.match(
    await page.$eval(M + ".is-selected", (e) => e.textContent),
    /Limit: 20 rows/,
  );
  // The notebook is a sheet opened by Save investigation, not a form under the chart.
  assert.equal(await page.$(".investigation-notes"), null, "no notebook form under the chart");
  await button("Save investigation", ".topbar ");
  await page.waitForSelector("dialog.notebook-sheet[open] .investigation-notes");
  await page
    .locator(".notebook-grid .field:nth-child(1) input")
    .fill("Index investigation");
  await page
    .locator(".investigation-notes textarea")
    .fill("Baseline and candidate. Check warm-cache timings.");
  await page
    .locator(".notebook-grid .field:nth-child(4) input")
    .fill("index, review");
  assert.equal(await page.$eval(".save-state", (e) => e.textContent), "Unsaved changes");
  await button("Save", "dialog.notebook-sheet .sheet-actions ");
  await page.waitForFunction(
    () => document.querySelector(".save-state")?.textContent === "Investigation saved",
  );
  await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
  // Autosave restores the open plan and notebook after a reload.
  await page.waitForFunction(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const r = await fetch("/api/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: "{}",
    });
    const state = (await r.json()).state;
    return !!state?.investigation.id && !state.investigation.dirty;
  });
  await page.reload({ waitUntil: "networkidle0" });
  // The workspace restores quietly (3.3.2): wait for the restored plan.
  await page.waitForSelector(".plan-item");
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 1);
  await button("Save investigation", ".topbar ");
  await page.waitForSelector("dialog.notebook-sheet[open]");
  assert.equal(
    await page.$eval(".investigation-notes textarea", (e) => e.value),
    "Baseline and candidate. Check warm-cache timings.",
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
  await button("Investigations", ".main-nav ");
  await page.waitForSelector(".library-item");
  await page.locator('[aria-label="Search investigations"]').fill('no-matching-audit');
  await page.waitForSelector(".library-list .view-empty");
  assert.equal((await page.$$('.library-item')).length, 0);
  await button('Clear search');
  await page.waitForSelector('.library-item');
  assert.match(
    await page.$eval(".library-item", (e) => e.textContent),
    /Index investigation/,
  );
  await page.click(".library-item");
  assert.match(await page.$eval(".library-detail h2", (e) => e.textContent), /Index investigation/);
  assert.equal(await page.$(".library-evidence"), null, "plans-only investigations look as before without an Evidence section");
  // Save an investigation with evidence and verify the Evidence section, monospace text block, Copy and Download.
  await page.evaluate(async () => {
    const auth = await (await fetch("/api/auth/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Myflames-Bootstrap": "1",
        "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "",
      },
      body: "{}",
    })).json();
    await fetch("/api/investigations/save", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": auth.token },
      body: JSON.stringify({
        value: {
          schema_version: "investigation-1.0",
          name: "Capture: threads running 57 on prod-eu, 2026-10-27 14:02",
          notes: "Captured automatically when threads running 57 on prod-eu.",
          tags: ["watch", "capture"],
          baseline_id: "",
          plans: [],
          evidence: [
            {
              id: "watch-1",
              kind: "watch",
              captured_at: "2026-10-27T14:02:31Z",
              title: "Watch trigger snapshot",
              text: "",
              data: {
                schema_version: "watch-1.0",
                engine: "mysql",
                server_version: "8.4.11",
                read_at: "2026-10-27T14:02:31Z",
                threads_running: 57,
                longest_statement: { id: 42, seconds: 19, user: "app", db: "shop" },
                longest_lock_wait: { id: 88, seconds: 12 },
              },
            },
            {
              id: "sessions-1",
              kind: "sessions",
              captured_at: "2026-10-27T14:02:32Z",
              title: "Processlist",
              text: "Sessions · MySQL 8.4.11\n#42 app shop Query 19s SELECT * FROM orders",
              data: { schema_version: "sessions-1.0" },
            },
          ],
        },
      }),
    });
  });
  await page.locator('[aria-label="Search investigations"]').fill("prod-eu");
  await page.waitForFunction(() => document.querySelector(".library-detail h2")?.textContent?.includes("Capture: threads running 57 on prod-eu"));
  await page.waitForSelector(".library-evidence [data-evidence-id]");
  const evItems = await page.$$eval(".library-evidence [data-evidence-id]", (els) =>
    els.map((e) => [e.dataset.evidenceKind, e.querySelector(".row-title")?.textContent, e.querySelector(".row-detail")?.textContent]),
  );
  assert.equal(evItems.length, 2);
  assert.equal(evItems[0][0], "watch");
  assert.equal(evItems[0][1], "Watch trigger snapshot");
  // Evidence times are local, like the capture's name (the fixture instant is UTC).
  const at = new Date("2026-10-27T14:02:31Z"), pad = (n) => String(n).padStart(2, "0");
  const local = `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
  assert.ok(evItems[0][2]?.includes(local), `${evItems[0][2]} shows ${local}`);
  await page.click('.library-evidence [data-evidence-kind="watch"]');
  await page.waitForSelector(".library-evidence-view .library-evidence-text");
  assert.match(await page.$eval(".library-evidence-view .library-evidence-text", (e) => e.textContent), /Threads running: 57/);
  await button("Download", ".library-evidence-actions ");
  await page.waitForSelector(".library-evidence-view [role=status]");
  assert.equal(await page.$eval(".library-evidence-view [role=status]", (e) => e.textContent),
    "Downloaded watch-trigger-snapshot-watch-1.txt.");
  // The browser writes a download after the click returns: wait for the file.
  const evDownloaded = path.join(directory, "watch-trigger-snapshot-watch-1.txt");
  for (let i = 0; i < 100; i++) {
    try { await fs.access(evDownloaded); break; } catch { await new Promise((r) => setTimeout(r, 50)); }
  }
  assert.match(await fs.readFile(evDownloaded, "utf8"), /Threads running: 57/, "the downloaded text is the evidence text");
  // The evidence view in light and dark, at the 720 px minimum window and 1440 px:
  // long lines scroll inside the text block, never the page.
  await page.click('.library-evidence [data-evidence-kind="sessions"]');
  await page.waitForFunction(() => /#42 app shop/.test(document.querySelector(".library-evidence-text")?.textContent || ""));
  for (const width of [720, 1440]) {
    for (const theme of ["light", "dark"]) {
      await page.setViewport({ width, height: 900 });
      await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
      await new Promise((r) => setTimeout(r, 100));
      const fit = await page.evaluate(() => {
        const view = document.querySelector(".library-evidence-view").getBoundingClientRect();
        const detail = document.querySelector(".library-detail").getBoundingClientRect();
        return { page: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          inside: view.left >= detail.left - 1 && view.right <= detail.right + 1 };
      });
      assert.ok(fit.page <= 0 && fit.inside, `evidence fits at ${width}px (${theme}): ${JSON.stringify(fit)}`);
      await page.screenshot({ path: path.join(directory, `evidence-${width}-${theme}.png`) });
    }
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
  await page.setViewport({ width: 1512, height: 1050 });
  // An investigation of evidence only lists its items, not an empty Plans box.
  assert.equal(await page.$(".library-detail .library-plans"), null);
  assert.match(await page.$eval(".library-item.is-selected small", (e) => e.textContent), /^2 evidence items/);
  await page.evaluate(async () => {
    const auth = await (await fetch("/api/auth/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Myflames-Bootstrap": "1",
        "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "",
      },
      body: "{}",
    })).json();
    const list = await (await fetch("/api/investigations/list", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": auth.token },
      body: JSON.stringify({ search: "prod-eu" }),
    })).json();
    for (const item of (list.items || []).filter((i) => i.name.includes("prod-eu"))) {
      await fetch("/api/investigations/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Myflames-Token": auth.token },
        body: JSON.stringify({ id: item.id }),
      });
    }
  });
  // Clearing the search lists the saved investigations again, without the deleted capture.
  await page.click('[aria-label="Search investigations"]', { count: 3 });
  await page.keyboard.press("Backspace");
  await page.waitForFunction(() => document.querySelector(".library-detail h2")?.textContent?.includes("Index investigation"));
  assert.equal(await page.$$eval(".library-item", (els) => els.filter((e) => e.textContent.includes("prod-eu")).length), 0);
  await button("Open all", ".library-actions ");
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 1);
  await button("Save investigation", ".topbar ");
  await page.waitForSelector("dialog.notebook-sheet[open]");
  assert.equal(
    await page.$eval(".investigation-notes textarea", (e) => e.value),
    "Baseline and candidate. Check warm-cache timings.",
  );
  await button("Export investigation bundle");
  await page.waitForNetworkIdle();
  const filename = path.join(directory, "Index-investigation.json");
  await fs.access(filename);
  const bundle = JSON.parse(await fs.readFile(filename, "utf8"));
  assert.equal(bundle.schema_version, "investigation-1.0");
  assert.equal(bundle.plans.length, 1);
  assert.equal(bundle.baseline_id, bundle.plans[0].id);
  await button("Close", "dialog.notebook-sheet ");
  await page.waitForFunction(() => !document.querySelector("dialog.notebook-sheet"));
  await button("Import plan", ".plans-bar ");
  await page.waitForSelector("dialog");
  await (
    await page.$("dialog input[type=file]")
  ).uploadFile(path.join(root, "test/mariadb-explain-join.json"));
  await frame();
  assert.equal((await page.$$(".plan-item")).length, 2);
  await button("Compare", ".plans-bar ");
  await page.waitForSelector(".comparison-row");
  async function compareMeasurements(width) {
    await page.setViewport({ width, height: 900 });
    const measured = await page.evaluate(() => {
      const rect = selector => {
        const r = document.querySelector(selector).getBoundingClientRect();
        return { top: r.top + scrollY, bottom: r.bottom + scrollY, left: r.left, right: r.right };
      };
      return { pickers: [...document.querySelectorAll(".compare-selectors .toolbar-field")].map(e => {
        const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom };
      }), tools: rect(".compare-tools"), stats: rect(".compare-stats"),
        chart: rect(".comparison-canvases"), firstRow: rect(".comparison-row"),
        overflow: document.documentElement.scrollWidth > innerWidth };
    });
    assert.equal(measured.overflow, false, JSON.stringify(measured));
    assert.ok(Math.abs(measured.pickers[0].top - measured.pickers[1].top) < 1, "the two plan pickers share a row");
    if (width === 1440) assert.ok(measured.tools.top < measured.stats.bottom && measured.stats.top < measured.tools.bottom,
      "desktop controls and headline measurements share one bar: " + JSON.stringify(measured));
    console.log("Compare geometry " + width + "x900: " + JSON.stringify(measured));
    await page.screenshot({ path: path.join(directory, "compare-compact-" + width + ".png"), fullPage: false });
    return measured;
  }
  await compareMeasurements(390);
  await compareMeasurements(1440);
  // Both plans are drawn in the page; the pair follows either side.
  const charts = ".comparison-canvases .plan-chart";
  await page.waitForFunction(
    (selector) =>
      [...document.querySelectorAll(selector)].filter((el) =>
        el.querySelector(".wb-node"),
      ).length === 2,
    {},
    charts,
  );
  assert.equal(await page.$(".comparison-canvases iframe"), null);
  assert.equal(await page.$(".comparison-canvases .wb-details"), null, "Compare has no empty report detail boxes");
  assert.equal(await page.$$eval(".compare-pane", (panes) => {
    const [a, b] = panes.map((el) => el.getBoundingClientRect());
    return Math.abs(a.top - b.top) < 1 && Math.abs(a.width - b.width) < 1;
  }), true, "desktop comparison panes share the row equally");
  const selectedIds = () =>
    page.$$eval(charts, (els) =>
      els.map((el) =>
        [...el.querySelectorAll(".wb-node.selected")].map(
          (node) => node.dataset.nodeId,
        ),
      ),
    );
  const rowSelected = (index) =>
    page.waitForFunction(
      (i) =>
        document
          .querySelectorAll(".comparison-row")
          [i].classList.contains("selected"),
      {},
      index,
    );
  const rows = await page.$$(".comparison-row");
  // Row -> charts. A removed operator has no counterpart: After clears.
  await rows[0].click();
  await rowSelected(0);
  const [beforeSel, afterSel] = await selectedIds();
  assert.equal(beforeSel.length, 1);
  assert.deepEqual(afterSel, []);
  // A pair with the same label on both sides is titled once; its badge says
  // it was matched (not Added or Removed).
  const matched = await page.$$eval(".comparison-row", (els) =>
    els.findIndex((el) =>
      el.querySelector("strong").textContent === "Nested loop inner join" &&
      !["added", "removed"].includes(el.querySelector(".match-badge").dataset.status),
    ),
  );
  assert.ok(matched > 0);
  await rows[matched].click();
  await rowSelected(matched);
  const pairIds = await selectedIds();
  assert.equal(pairIds[0].length, 1);
  assert.equal(pairIds[1].length, 1);
  assert.notDeepEqual(pairIds[0], beforeSel);
  // Chart -> row and counterpart, from the After side.
  await rows[0].click();
  await rowSelected(0);
  await page.$$eval(
    charts,
    (els, id) =>
      els[1]
        .querySelector('.wb-node[data-node-id="' + id + '"]')
        .dispatchEvent(new MouseEvent("click", { bubbles: true })),
    pairIds[1][0],
  );
  await rowSelected(matched);
  assert.deepEqual(await selectedIds(), pairIds);
  // ...and from the Before side, back to the removed operator.
  await page.$$eval(
    charts,
    (els, id) =>
      els[0]
        .querySelector('.wb-node[data-node-id="' + id + '"]')
        .dispatchEvent(new MouseEvent("click", { bubbles: true })),
    beforeSel[0],
  );
  await rowSelected(0);
  assert.deepEqual(await selectedIds(), [beforeSel, []]);
  await page.setViewport({ width: 390, height: 844 });
  await page.waitForFunction(() => [...document.querySelectorAll('.compare-pane svg.wb-plan')].every((svg) =>
    Math.abs(svg.getBoundingClientRect().width - svg.viewBox.baseVal.width) < 1));
  const narrowPanes = await page.$$eval(".compare-pane", (panes) => panes.map((el) => {
    const rect = el.getBoundingClientRect();
    const svg = el.querySelector('svg.wb-plan');
    const canvas = svg.getBoundingClientRect();
    const viewport = svg.querySelector('.wb-viewport').viewBox.baseVal;
    return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right,
      width: rect.width, canvasWidth: canvas.width, zoom: canvas.width / viewport.width, y: viewport.y };
  }));
  assert.ok(narrowPanes[1].top >= narrowPanes[0].bottom - 1, "phone comparison stacks Before then After");
  for (const pane of narrowPanes) {
    assert.ok(pane.left >= 0 && pane.right <= 390 && Math.abs(pane.canvasWidth - pane.width) < 2);
    // Fit keeps Visual Explain's 11 px text at 11 px (zoom 1, core/zoom.ts);
    // the measured ratio of two rendered widths can land a rounding error off it.
    assert.ok(pane.zoom >= 1 - 1e-6 && pane.zoom <= 1 + 1e-6, JSON.stringify(pane));
    assert.equal(pane.y, -28, "each comparison starts at the inputs");
  }
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.screenshot({
    path: path.join(directory, "compare-mobile.png"),
    fullPage: true,
  });
  await page.setViewport({ width: 1512, height: 1050 });
  await button("Explore", ".plans-bar ");
  await frame();
  // The phone picker opens the same investigation plans; projection remains
  // controlled by the shared chart state even when its controls are in a menu.
  await page.setViewport({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  const phonePlanIds = await page.$$eval('select[aria-label="Open plan"] option', options => options.map(option => option.value));
  assert.equal(phonePlanIds.length, 2);
  await page.select('select[aria-label="Open plan"]', phonePlanIds[1]);
  await frame();
  assert.equal(await page.$eval('select[aria-label="Open plan"]', select => select.value), phonePlanIds[1]);
  await page.click('button[aria-label="Chart options"]');
  await page.select('select[aria-label="Phone operator metric"]', 'rows');
  await page.waitForFunction(() => document.querySelector('.plan-inspector .metric-panel h2')?.textContent.includes('rows'));
  await page.keyboard.press('Escape');
  await page.select('select[aria-label="Open plan"]', phonePlanIds[0]);
  await frame();
  assert.equal(await page.$eval('select[aria-label="Operator metric"]', select => select.value), 'rows', 'phone and desktop share the metric');
  await page.setViewport({ width: 1512, height: 1050 });
  await page.select('select[aria-label="Operator metric"]', 'self_time');
  await frame();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(directory, "plan.png"),
    fullPage: true,
  });
  if (process.env.MYFLAMES_TEST_PORT) {
    // Set up the connection on Home and put it in use, then capture.
    await button("Home", ".main-nav ");
    await button("Add connection");
    await page.locator(".connection-panel label ::-p-text(Host)").wait();
    await page.$eval(
      ".connection-panel",
      (panel, port) => {
        const set = (label, value) => {
          const e = [...panel.querySelectorAll("label")]
            .find((e) => e.firstChild.textContent.trim() === label)
            ?.querySelector("input");
          if (!e) throw Error(label);
          const setter = Object.getOwnPropertyDescriptor(
            HTMLInputElement.prototype,
            "value",
          ).set;
          setter.call(e, value);
          e.dispatchEvent(new Event("input", { bubbles: true }));
        };
        set("Port", port);
        set("User", "root");
        set("Password", "myflames-validation-only");
        set("Schema", "lab");
      },
      process.env.MYFLAMES_TEST_PORT,
    );
    await button("Test connection");
    await page.waitForFunction(() =>
      document
        .querySelector(".connections .notice")
        ?.textContent.includes("Connected"),
    );
    await button("Use without saving", ".connection-panel ");
    await button("Query editor", ".main-nav ");
    await page.waitForSelector(".query-lab");
    await page
      .locator('[aria-label="SQL editor"]')
      .fill("SELECT category, SUM(price) FROM items GROUP BY category");
    // Tabs rename inline: F2 on the selected tab, Enter keeps the name.
    await page.focus(".query-tabs [role=tab][aria-selected=true]");
    await page.keyboard.press("F2");
    await page
      .locator('[aria-label="Query name"]')
      .fill("Live experiment");
    await page.keyboard.press("Enter");
    await page.$eval(".execution-options input[type=checkbox]", (e) =>
      e.click(),
    );
    await button("Run analysis");
    await page.waitForFunction(
      () =>
        document.querySelector(".plan-heading h1")?.textContent ===
        "Live experiment",
      { timeout: 30000 },
    );
    await frame();
    await button("Measurements", ".context-panel ");
    assert.match(
      await page.$eval(".context-body", (e) => e.textContent),
      /Measured runs/,
    );
    await button("Optimizer trace", ".context-panel ");
    assert.ok(await page.$(".trace-branch"));
    await page.locator(".context-panel input[type=search]").fill("sum");
    assert.match(
      await page.$eval(".context-body", (e) => e.textContent),
      /items/,
    );
  }
  await button("Query editor", ".main-nav ");
  await page.waitForSelector(".query-lab");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: path.join(directory, "query-lab.png"),
    fullPage: true,
  });
  await page.setViewport({ width: 390, height: 844 });
  assert.equal(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth,
    ),
    false,
  );
  await page.screenshot({
    path: path.join(directory, "query-lab-mobile.png"),
    fullPage: true,
  });
  // Unknown saved view fields must not affect lesson restore.
  const snapshot = await page.evaluate(async () => {
    const auth = await fetch("/api/auth/session", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1",
        "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" },
      body: "{}",
    });
    const { token } = await auth.json();
    const response = await fetch("/api/session/load", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
      body: "{}",
    });
    return response.json();
  });
  assert.ok(snapshot.state, "the workflow saved a session to restore");
  snapshot.state.mode = "teach";
  snapshot.state.teach = { lesson: "btree", return_to_plan: false, view: "3d" };
  const restoreRequest = (request) => request.url().endsWith("/api/session/load")
    ? request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(snapshot) })
    : request.continue();
  await page.setRequestInterception(true);
  page.on("request", restoreRequest);
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector('.lesson-frame[title^="Lesson: B"]');
  const restoredLesson = await (await page.$(".lesson-frame")).contentFrame();
  await restoredLesson.waitForSelector("#btn-play");
  assert.ok(await restoredLesson.$("svg"), "restored lesson uses the SVG player");
  assert.equal(await page.$('.segmented[aria-label="Lesson view"]'), null);
  assert.ok(lessonRequests.every((body) => !Object.hasOwn(body, "view")),
    "restoring obsolete preferences requests the current lesson");
  page.off("request", restoreRequest);
  await page.setRequestInterception(false);
  assert.deepEqual(errors, []);
  await lazyPlanRegressions(browser, url);
  console.log("Investigation workflow passed. " + directory);
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) {
    server.kill("SIGINT");
    await once(server, "exit");
  }
}
