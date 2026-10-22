// Batch B actions use the same held-request harness as the shared feedback
// suite. Fixtures describe UI states only; no database endpoint is contacted.
import assert from "node:assert/strict";

export function batchBScenarios({ goPlace, goView, visible, named, click, confirmButton, waitText, waitNotBusy,
  pause, viewRefresh, editorSession, editorRun, switchConnection, respond }) {
  const sessionStatus = ".query-lab .editor-session-status";
  const stopStatus = ".query-lab .editor-stop-status";
  const diagram = '.server-view[data-view="diagram"]';
  const model = { schema_version: "schema-diagram-1.0", tables: [{ id: "orders", schema: "mftab", name: "orders",
    columns: [{ name: "id", data_type: "bigint", nullable: false, primary_key: true }] }], relationships: [], warnings: [] };
  const diagramMocks = { "/api/schema-diagram/read": () => model, "/api/schema-diagram/import": () => model };
  const prepareEditor = async ({ page }) => {
    await goPlace(page, "query");
    await page.locator('[aria-label="SQL editor"]').fill("SELECT 1");
    await pause(400);
  };
  const requireFocus = async (page, selector) => {
    assert.equal(await page.$eval(selector, e => document.activeElement === e && !e.disabled), true, "the starting control keeps focus while waiting");
  };
  const noRetry = async (page, selector) => {
    assert.equal(await page.$$eval(`${selector} button`, els => els.some(e => /Try again/.test(e.textContent))), false, "writes never offer retry");
  };
  const elapsed = async (page, selector) => waitText(page, `${selector} .action-elapsed`, /^\d{2,} s so far$/);
  const transaction = (action, unknown = false) => ({
    name: `editor-${action}${unknown ? "-unknown" : ""}`, label: `Editor ${action}`, paths: [`/api/editor/${action}`],
    ...(unknown ? { modes: ["error"], errorStatus: 503 } : {}),
    mocks: {
      "/api/editor/run": b => ({ status: "complete", session: editorSession({ in_transaction: true }), run: editorRun(b.sql) }),
      [`/api/editor/${action}`]: () => ({ status: "complete", session: editorSession(), run: { ...editorRun(action.toUpperCase()), kind: "action" } }),
    },
    setup: async ctx => { await prepareEditor(ctx); await ctx.page.click("[data-action=run]"); await visible(ctx.page, `[data-action=${action}]`); },
    target: `[data-action=${action}]`,
    afterClick: async ({ page }) => {
      await requireFocus(page, `[data-action=${action}]`);
      await waitText(page, sessionStatus, action === "commit" ? /Committing…/ : /Rolling back…/);
    },
    ok: async ({ page }) => waitText(page, ".query-lab .editor-feedback", action === "commit" ? /^Committed\.$/ : /^Rolled back\.$/),
    error: async ({ page, ctx }) => {
      await waitText(page, sessionStatus, unknown ? /Outcome unknown.*not sent again.*Audit log/ : /Simulated failure/);
      if (!unknown) assert.doesNotMatch(await page.$eval(sessionStatus, e => e.textContent), /Outcome unknown/);
      await noRetry(page, sessionStatus);
      if (unknown) {
        assert.equal(await page.$eval(`[data-action=${action}]`, e => e.disabled), true);
        await pause(250);
        assert.equal(ctx.log.filter(e => e.path === `/api/editor/${action}`).length, 1, "lost write replies never resend");
      }
    },
    hang: async ({ page }) => {
      await waitText(page, sessionStatus, /Still waiting for the server\./);
      await elapsed(page, sessionStatus);
      await requireFocus(page, `[data-action=${action}]`);
      await noRetry(page, sessionStatus);
    },
  });
  const stop = (unknown = false) => ({
    name: `editor-stop${unknown ? "-unknown" : ""}`, label: "Stop", paths: ["/api/editor/stop"],
    ...(unknown ? { modes: ["error"], errorStatus: 503 } : {}),
    setup: async ctx => {
      await prepareEditor(ctx);
      const run = { ...editorRun(), state: "running", id: "held-run" };
      ctx.mocks["/api/editor/run"] = () => ({ status: "running", session: editorSession({ state: "running" }), run });
      ctx.hold.add("/api/editor/status");
      await ctx.page.click("[data-action=run]");
      await visible(ctx.page, ".results-running");
      // The run finishes while Stop's reply is still outstanding. Its result
      // must not clear Stopping or allow a second KILL for this run.
      ctx.mocks["/api/editor/status"] = () => ({ status: "complete", session: editorSession(), run: { ...run, state: "complete" } });
    },
    target: "[data-action=stop]",
    afterClick: async ({ page }) => { await requireFocus(page, "[data-action=stop]"); await waitText(page, stopStatus, /Sending KILL QUERY…/); },
    ok: async ({ page }) => { await waitText(page, stopStatus, /KILL QUERY sent\./); await waitNotBusy(page, "[data-action=stop]"); },
    error: async ({ page }) => {
      await waitText(page, stopStatus, unknown ? /Outcome unknown.*not sent again.*Audit log/ : /Simulated failure/);
      if (!unknown) assert.doesNotMatch(await page.$eval(stopStatus, e => e.textContent), /Outcome unknown/);
      await noRetry(page, stopStatus);
    },
    hang: async ({ page, ctx }) => {
      await waitText(page, stopStatus, /No reply to Stop yet; the statement may still be running\./);
      await elapsed(page, stopStatus);
      const poll = ctx.held.find(e => e.path === "/api/editor/status");
      assert.ok(poll, "the running statement has a held status read");
      await respond(poll.request, 200, ctx.mocks[poll.path](poll.body));
      ctx.held.splice(ctx.held.indexOf(poll), 1);
      await waitNotBusy(page, ".results-pane");
      await requireFocus(page, "[data-action=stop]");
      assert.equal(await page.$eval("[data-action=stop]", e => e.getAttribute("aria-busy")), "true");
      await page.click("[data-action=stop]");
      assert.equal(ctx.log.filter(e => e.path === "/api/editor/stop").length, 1);
    },
  });
  const capture = action => ({
    name: `editor-${action}`, label: action === "explain" ? "Explain" : "Run analysis", paths: ["/api/live/start"],
    mocks: {
      "/api/live/start": () => ({ id: "feedback-capture" }),
      "/api/live/status": () => ({ id: "feedback-capture", state: "complete", progress: 1, repeat: 1, error: "", result: null }),
    },
    setup: prepareEditor,
    target: `[data-action=${action}]`,
    afterClick: async ({ page }) => requireFocus(page, `[data-action=${action}]`),
    ok: async ({ page }) => {
      await waitNotBusy(page, `[data-action=${action}]`);
      await waitText(page, ".plan-pane", /Each capture opens as a plan in Plans/);
    },
    error: async ({ page }) => { await waitText(page, ".query-lab [role=alert]", /Simulated failure/); await waitNotBusy(page, `[data-action=${action}]`); },
    hang: async ({ page }) => {
      await waitText(page, ".plan-pane", /Capturing 0 \/ 1 runs/);
      await named("Cancel capture")(page);
      await requireFocus(page, `[data-action=${action}]`);
    },
  });
  return [
    viewRefresh("top", "workload", "/api/live/top", { label: "Top statements" }),
    viewRefresh("errorlog", "server", "/api/live/error-log", { label: "Error log" }),
    {
      name: "top-load", label: "Load top statements", paths: ["/api/live/top"],
      setup: async ctx => {
        // Stop the automatic first read to reach the retained empty-state
        // action without introducing an error banner above it.
        ctx.hold.add("/api/live/top");
        await goView(ctx.page, "workload", "top");
        await ctx.page.evaluate(() => { window.__clockOffset += 65_000; });
        await click(ctx.page, "Stop", '.server-view[data-view="top"] .action-status');
        await waitNotBusy(ctx.page, '.server-view[data-view="top"] .action-button');
        ctx.hold.delete("/api/live/top");
        for (const held of ctx.held.filter(h => h.path === "/api/live/top")) {
          await held.request.abort().catch(() => {});
          ctx.held.splice(ctx.held.indexOf(held), 1);
        }
        ctx.topButton = await (await named("Load top statements")(ctx.page)).boundingBox();
      },
      target: named("Load top statements"),
      afterClick: async ({ page, ctx }) => {
        const button = await named("Loading…", '.server-view[data-view="top"]')(page);
        const box = await button.boundingBox();
        assert.ok(Math.abs(box.y - ctx.topButton.y) <= 1, "loading progress does not move the empty-state control");
        assert.equal(await button.evaluate(e => document.activeElement === e && !e.disabled), true);
      },
      ok: async ({ page }) => waitText(page, '.server-view[data-view="top"] .view-status [role=status]', /^Updated at /),
      error: async ({ page }) => { await waitText(page, '.server-view[data-view="top"] [role=alert]', /Simulated failure/); await visible(page, '.server-view[data-view="top"] .view-status-retry'); },
      hang: async ({ page }) => {
        await elapsed(page, '.server-view[data-view="top"] .action-status');
        await click(page, "Stop", '.server-view[data-view="top"] .action-status');
        await named("Load top statements")(page);
      },
    },
    {
      name: "editor-run-all", label: "Run all", paths: ["/api/editor/run"], setup: prepareEditor, target: "[data-action=run-all]",
      afterClick: async ({ page }) => requireFocus(page, "[data-action=run-all]"),
      ok: async ({ page }) => waitText(page, ".query-lab .results-tabs [role=tab]", /Result 1/),
      error: async ({ page }) => waitText(page, ".query-lab [role=alert]", /Simulated failure/),
      hang: async ({ page }) => { await elapsed(page, ".results-first-run"); await requireFocus(page, "[data-action=run-all]"); },
    },
    stop(), stop(true), transaction("commit"), transaction("rollback"), transaction("commit", true), transaction("rollback", true),
    {
      name: "editor-close-session", label: "Close session", paths: ["/api/editor/close"],
      setup: async ctx => {
        await prepareEditor(ctx); await ctx.page.click("[data-action=run]");
        await waitText(ctx.page, ".results-tabs [role=tab]", /Result 1/);
        await switchConnection(ctx.page); await visible(ctx.page, '[data-banner="mismatch"]');
      },
      target: named("Close session", '[data-banner="mismatch"]'),
      ok: async ({ page }) => waitText(page, ".query-lab .editor-feedback", /Session closed/),
      error: async ({ page }) => waitText(page, sessionStatus, /Simulated failure/),
      hang: async ({ page }) => { await elapsed(page, sessionStatus); await requireFocus(page, '[data-banner="mismatch"] button'); },
    },
    {
      name: "editor-reconnect", label: "Reconnect", paths: ["/api/editor/open"],
      mocks: { "/api/editor/run": b => ({ status: "complete", session: editorSession({ ended: true, ended_reason: "connection_lost" }), run: editorRun(b.sql) }) },
      setup: async ctx => { await prepareEditor(ctx); await ctx.page.click("[data-action=run]"); await visible(ctx.page, '[data-banner="ended"]'); },
      target: named("Reconnect", '[data-banner="ended"]'),
      ok: async ({ page }) => waitText(page, ".query-lab .editor-feedback", /Session open \(connection/),
      error: async ({ page }) => { await waitText(page, sessionStatus, /Simulated failure/); await named("Reconnect")(page); },
      hang: async ({ page }) => { await elapsed(page, sessionStatus); await requireFocus(page, '[data-banner="ended"] button'); },
    },
    capture("explain"), capture("analyze"),
    viewRefresh("diagram", "diagram", "/api/schema-diagram/read", {
      name: "diagram-rebuild", label: "Rebuild database diagram", mocks: diagramMocks,
      setup: async ({ page }) => { await goPlace(page, "diagram"); await page.select(`${diagram} [aria-label=Database]`, "mftab"); },
    }),
    {
      name: "diagram-reload-list", label: "Reload database list", paths: ["/api/live/schemas"],
      setup: async ({ page }) => { await goPlace(page, "diagram"); await waitNotBusy(page, ".diagram-reload-list"); },
      target: ".diagram-reload-list",
      afterClick: async ({ page }) => requireFocus(page, ".diagram-reload-list"),
      ok: async ({ page }) => waitText(page, `${diagram} .diagram-source-note`, /Database list updated/),
      error: async ({ page }) => { await waitText(page, `${diagram} .diagram-source-note`, /Simulated failure/); await named("Try again", `${diagram} .diagram-source-note`)(page); },
      hang: async ({ page }) => { await elapsed(page, `${diagram} .diagram-source-note`); await waitText(page, `${diagram} .diagram-source-note`, /Still waiting for the server/); },
    },
    {
      name: "diagram-import", label: "Import diagram", paths: ["/api/schema-diagram/import"], mocks: diagramMocks,
      setup: async ({ page }) => {
        await goPlace(page, "diagram");
        await page.select(`${diagram} [aria-label=Database]`, "mftab"); await page.click(`${diagram} [data-action=refresh]`);
        await visible(page, ".diagram-table");
        await click(page, "Import", diagram);
        await page.locator(".diagram-paste textarea").fill("CREATE TABLE orders (id BIGINT PRIMARY KEY)");
      },
      target: confirmButton("dialog.diagram-import[open]"),
      ok: async ({ page }) => { await page.waitForFunction(() => !document.querySelector("dialog.diagram-import")); await waitText(page, `${diagram} .summary-line`, /Imported SQL/); },
      error: async ({ page }) => { await waitText(page, "dialog.diagram-import [role=alert]", /Simulated failure/); await waitNotBusy(page, "dialog.diagram-import"); },
      hang: async ({ page, ctx }) => {
        await waitText(page, "dialog.diagram-import .sheet-progress", /No reply yet/);
        await click(page, "Close", "dialog.diagram-import");
        await page.waitForFunction(() => !document.querySelector("dialog.diagram-import"));
        await visible(page, ".diagram-table");
        const held = ctx.held.find(h => h.path === "/api/schema-diagram/import");
        await respond(held.request, 200, { ...model, tables: [{ ...model.tables[0], id: "late", name: "late" }] });
        await pause(250);
        assert.equal(await page.$('[data-table-id="late"]'), null, "a cancelled import cannot replace the current diagram");
        assert.ok(await page.$('[data-table-id="orders"]'));
        assert.equal(ctx.log.filter(e => /^\/api\/editor\/(run|commit|rollback)$/.test(e.path)).length, 0, "imports never execute SQL");
      },
    },
  ].map(s => ({ replyDelayMs: 2000, ...s }));
}
