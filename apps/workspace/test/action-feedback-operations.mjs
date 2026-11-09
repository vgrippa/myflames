// Server writes have two separate waits: the command's reply and a later read.
// Hold those independently so a result cannot disappear behind its refresh.
import assert from "node:assert/strict";

export function operationScenarios(h, fixtures) {
  const { click, named, visible, confirmButton, goPlace, goView, waitText, waitNotBusy,
    respond, heldFor, switchConnection, escapeRe, viewRefresh } = h;
  const { variable, parameters, replication, skipped, running, stopped } = fixtures;
  const sessions = '.server-view[data-view="sessions"]';
  const config = '.server-view[data-view="config"]';
  const replica = '.server-view[data-view="replication"]';
  const variablePath = "/api/live/variable/apply";
  const replicationPath = "/api/live/replication";
  const skipPath = `${replicationPath}/skip`;
  const loopStart = `${replicationPath}/skip-loop/start`;
  const loopStop = `${replicationPath}/skip-loop/stop`;
  const loopStatus = `${replicationPath}/skip-loop/status`;
  const killPath = "/api/live/kill";
  const sheet = "dialog.ops-confirm[open]";
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const count = (ctx, path) => ctx.log.filter((entry) => entry.path === path).length;
  const focus = async (page, selector) => page.waitForFunction((sel) =>
    [...document.querySelectorAll(sel)].some((element) => element === document.activeElement),
  { timeout: 3000 }, selector).catch(() => { throw Error(`outcome does not have focus: ${selector}`); });
  const close = async (page, selector) => {
    await waitText(page, `${selector} .sheet-progress`, /^No reply yet/);
    await visible(page, `${selector} .sheet-leave-note`);
    await click(page, "Close", selector);
    await page.waitForFunction((sel) => !document.querySelector(sel), {}, selector);
  };
  // A transport rejection differs from the server's explicit HTTP 400 refusal.
  const transport = (scenario, selector) => ({ ...scenario, name: `${scenario.name}-transport`,
    label: `${scenario.label}: transport lost`, modes: ["error"],
    respondError: (request) => request.abort("connectionreset"),
    error: async ({ page, ctx }) => {
      await waitText(page, selector, /Outcome unknown/i);
      await focus(page, selector);
      assert.equal(count(ctx, scenario.paths[0]), 1, "an uncertain write is never sent again");
    },
  });

  // Only reset availability and persisted metadata are synthetic; the normal
  // single-variable shape and values come from the recorded MySQL fixture.
  function variableMocks(reset = false) {
    const info = clone(variable.variable);
    const catalog = clone(parameters);
    if (reset) {
      info.persisted = { ...info.persisted, present: true, value: "300" };
      info.modes.reset_persist = { available: true, reason: null, recommended: false };
      catalog.parameters.items.find((item) => item.name === info.name).persisted = true;
    }
    const plan = (body) => {
      const mode = body.mode || "global";
      const value = mode === "reset_persist" ? null : body.value;
      const statement = mode === "reset_persist" ? `RESET PERSIST ${body.name}` :
        `SET ${mode.toUpperCase()} ${body.name} = ${value}`;
      return { ...clone(variable.plan), name: body.name, mode, requested: value, value,
        statement, variable: { ...info, name: body.name },
        before: { ...variable.plan.before, persisted: reset ? "300" : null },
        expect: { ...variable.plan.expect, name: body.name, mode, statement, persisted_value: reset ? "300" : null } };
    };
    return {
      "/api/live/config": () => catalog,
      "/api/live/variable": () => info,
      "/api/live/variable/plan": plan,
      [variablePath]: (body) => ({ ...clone(variable.change), name: body.name, mode: body.mode,
        statement: plan(body).statement, message: reset ? "Removed the persisted value of max_connections." : variable.change.message,
        before: { ...variable.change.before, persisted: reset ? "300" : null },
        after: reset ? { ...variable.change.before, persisted: null } : variable.change.after }),
    };
  }
  async function openVariable(page, reset) {
    await goView(page, "server", "config");
    await (await visible(page, `${config} tr[data-row-key="max_connections"] td:first-child`)).click();
    await (await visible(page, `.view-inspector [data-${reset ? "reset" : "change"}="max_connections"]`)).click();
    await visible(page, "dialog.variable-sheet[open]");
    if (!reset) {
      const field = await visible(page, "#variable-value-max_connections");
      await field.click({ clickCount: 3 }); await field.type("300");
    }
    await confirmButton("dialog.variable-sheet[open]")(page);
  }
  function parameterWrite(kind) {
    const batch = kind === "batch", reset = kind === "reset";
    const dialog = `dialog.${batch ? "parameter-batch-sheet" : "variable-sheet"}[open]`;
    const result = `${config} ${batch ? ".parameter-results" : ".variable-result"}`;
    const pending = `${config} ${batch ? ".parameter-batch-pending" : ".variable-pending"}`;
    return {
      name: `config-${kind === "single" ? "change" : kind}-confirm`, label: `Parameters: ${batch ? "Apply batch" : reset ? "Reset persisted value" : "Apply variable"}`,
      paths: [variablePath], mocks: variableMocks(reset), sheet: true, replyDelayMs: 2000,
      setup: async ({ page }) => {
        if (!batch) return openVariable(page, reset);
        await goView(page, "server", "config");
        await click(page, "Edit", config);
        const field = await visible(page, "#parameter-value-max_connections");
        await field.click({ clickCount: 3 }); await field.type("300");
        await click(page, "Review and apply (1)", config);
        await confirmButton(dialog)(page);
      },
      target: confirmButton(dialog),
      afterClick: async ({ page, ctx }) => {
        await waitText(page, `${dialog} button[aria-busy=true]`, /Applying…/);
        // The result and its focus must not wait for this read to finish.
        ctx.hold.add("/api/live/config");
        ctx.operationReads = count(ctx, "/api/live/config");
      },
      ok: async ({ page, ctx }) => {
        await waitText(page, result, /Changed\./);
        await focus(page, result);
        await heldFor(ctx, ["/api/live/config"]);
        assert.ok(await page.$(`${result} [data-status=unknown]`) === null, "a confirmed result stays confirmed during the refresh");
      },
      error: async ({ page }) => {
        await waitText(page, result, /Simulated failure/);
        assert.doesNotMatch(await page.$eval(result, (el) => el.textContent), /Outcome unknown/i,
          "HTTP 400 is a refusal, not an unknown outcome");
        if (batch) assert.equal(await page.$eval(`${result} [data-result=max_connections]`, (el) => el.dataset.status), "failed");
        await focus(page, result);
      },
      hang: async ({ page, ctx }) => {
        await close(page, dialog);
        await waitText(page, pending, batch ? /Waiting for the replies to the parameter changes/ : /Waiting for the reply to the change to max_connections/);
        await waitText(page, `${pending} .action-elapsed`, /^\d{2,} s so far$/);
        assert.equal(count(ctx, "/api/live/config"), ctx.operationReads, "a pending write does not reread parameters");
        assert.equal(count(ctx, variablePath), 1, "Close never sends a decline or repeats the write");
        const write = await heldFor(ctx, [variablePath]);
        ctx.held.splice(ctx.held.indexOf(write), 1);
        await respond(write.request, 200, ctx.mocks[variablePath](write.body));
        await waitText(page, result, /Changed\./);
        await focus(page, result);
      },
    };
  }

  const report = clone(replication);
  for (const channel of report.channels) if (channel.skip_plan) Object.assign(channel.skip_plan, { review_token: "feedback-skip-review" });
  const replicaMocks = { [replicationPath]: () => report, [skipPath]: () => skipped };
  async function openReplication(page, select = true) {
    await goPlace(page, "replication");
    await visible(page, `${replica} [data-action=refresh]`);
    await waitNotBusy(page, `${replica} [data-action=refresh]`);
    if (select) {
      await click(page, "Channels");
      await (await visible(page, `${replica} tbody tr td:first-child`)).click();
      await named("Skip this transaction…", ".view-inspector")(page);
      await page.waitForFunction(() => [...document.querySelectorAll(".view-inspector button")].some((button) =>
        button.getClientRects().length > 0 && button.textContent.trim() === "Skip this transaction…" &&
        !button.disabled && button.getAttribute("aria-disabled") !== "true"));
    }
  }
  const skipOpen = (loop) => ({
    name: loop ? "replication-loop-open" : "replication-skip-open",
    label: `${loop ? "Skip until stopped" : "Skip this transaction"}: preflight`,
    paths: [replicationPath], mocks: replicaMocks, replyDelayMs: 2000,
    setup: async ({ page }) => {
      await openReplication(page);
      const opener = await named(loop ? "Skip until stopped…" : "Skip this transaction…", ".view-inspector")(page);
      await opener.evaluate((button) => { window.__replicationOpener = button; });
    },
    target: named(loop ? "Skip until stopped…" : "Skip this transaction…", ".view-inspector"),
    afterClick: async ({ page, mode }) => {
      await waitText(page, ".view-inspector button[aria-busy=true]", /Checking…/);
      assert.equal(await page.evaluate(() => document.activeElement === window.__replicationOpener &&
        !window.__replicationOpener.disabled && window.__replicationOpener.getAttribute("aria-disabled") === "true"), true,
      "the original preflight button keeps focus after a double click");
      // A user may move focus while the read waits. The review must still
      // return to the button that initiated it, not the last focused control.
      if (mode === "ok") await page.focus(`${replica} [data-action=refresh]`);
    },
    ok: async ({ page, ctx }) => {
      await visible(page, sheet);
      const cancel = await named("Cancel", sheet)(page);
      assert.equal(await cancel.evaluate(button => button === document.activeElement), true, "the review takes focus on Cancel");
      // Exercise both dismissal routes and require the actual initiating node,
      // not another button with the same text, to receive focus again.
      if (loop) await page.keyboard.press("Escape");
      else await cancel.click();
      await page.waitForFunction((sel) => !document.querySelector(sel), {}, sheet);
      await page.waitForFunction(() => document.activeElement === window.__replicationOpener);
      assert.equal(count(ctx, skipPath) + count(ctx, loopStart), 0, "closing the review sends no write");
    },
    error: async ({ page }) => {
      await waitText(page, `${replica} [role=alert]`, /Simulated failure/);
      assert.equal(await page.evaluate(() => document.activeElement === window.__replicationOpener), true,
        "a failed preflight leaves focus on its opener");
    },
    hang: async ({ page, ctx }) => {
      await waitText(page, `${replica} .view-status .capture-status`, /Still waiting for the server/);
      await click(page, "Stop", `${replica} .view-status`);
      await waitNotBusy(page, ".view-inspector button");
      await named(loop ? "Skip until stopped…" : "Skip this transaction…", ".view-inspector")(page);
      assert.equal(await page.$(sheet), null, "an abandoned preflight opens no confirmation");
      assert.equal(count(ctx, skipPath) + count(ctx, loopStart), 0, "abandoning preflight sends no write");
    },
  });
  function replicationWrite(loop) {
    const path = loop ? loopStart : skipPath;
    return {
      name: loop ? "replication-loop-start" : "replication-skip-confirm",
      label: loop ? "Start skipping (confirm)" : "Skip transaction (confirm)",
      paths: [path], mocks: replicaMocks, replyDelayMs: 2000, sheet: true,
      setup: async ({ page, mocks }) => {
        // A registry per scenario prevents a running fixture leaking into another run.
        let registered = [];
        mocks[loopStatus] = () => ({ schema_version: "replication-skip-loop-1.0", loops: registered });
        mocks[loopStart] = () => { registered = [running]; return running; };
        await openReplication(page);
        await click(page, loop ? "Skip until stopped…" : "Skip this transaction…", ".view-inspector");
        await visible(page, sheet);
        if (loop) await page.click(`${sheet} .ops-loop-options input[type=checkbox]`);
        await confirmButton(sheet)(page);
      },
      target: confirmButton(sheet),
      afterClick: async ({ page, ctx }) => {
        await waitText(page, `${sheet} button[aria-busy=true]`, loop ? /Starting…/ : /Skipping…/);
        ctx.hold.add(replicationPath);
        ctx.operationReads = count(ctx, replicationPath);
      },
      ok: async ({ page, ctx }) => {
        await waitText(page, loop ? `${replica} .ops-loop` : `${replica} .ops-result`, loop ? /Skipping 1062/ : /Skipped/);
        await focus(page, loop ? `${replica} .ops-loop` : `${replica} .ops-result`);
        if (!loop) await heldFor(ctx, [replicationPath]);
      },
      error: async ({ page }) => {
        await waitText(page, `${replica} [role=alert]`, /Simulated failure/);
        assert.doesNotMatch(await page.$eval(replica, (el) => el.textContent), /Outcome unknown/i,
          "a replication HTTP 400 is a failure");
        await focus(page, `${replica} .replication-write-result`);
      },
      hang: async ({ page, ctx }) => {
        await close(page, sheet);
        await waitText(page, `${replica} .replication-pending`, loop ? /Waiting for the reply to Start skipping/ : /Waiting for the reply to Skip transaction/);
        await waitText(page, `${replica} .replication-pending .action-elapsed`, /^\d{2,} s so far$/);
        assert.equal(count(ctx, replicationPath), ctx.operationReads, "Close does not refresh a write still awaiting its reply");
        assert.equal(count(ctx, path), 1, "Close does not repeat a replication write");
        const write = await heldFor(ctx, [path]);
        ctx.held.splice(ctx.held.indexOf(write), 1);
        await respond(write.request, 200, ctx.mocks[path](write.body));
        await waitText(page, loop ? `${replica} .ops-loop` : `${replica} .ops-result`, loop ? /Skipping 1062/ : /Skipped/);
        await focus(page, loop ? `${replica} .ops-loop` : `${replica} .ops-result`);
      },
    };
  }
  const stop = {
    name: "replication-loop-stop", label: "Skip loop Stop", paths: [loopStop], mocks: replicaMocks,
    replyDelayMs: 2000, slowShiftMs: 10_100,
    setup: async ({ page, mocks }) => {
      let registered = [running];
      mocks[loopStatus] = () => ({ schema_version: "replication-skip-loop-1.0", loops: registered });
      mocks[loopStop] = () => { registered = [stopped]; return stopped; };
      await openReplication(page, false);
      await named("Stop", `${replica} .ops-loop`)(page);
    },
    target: named("Stop", `${replica} .ops-loop`),
    afterClick: async ({ page }) => waitText(page, `${replica} .ops-loop button[aria-busy=true]`, /Stopping…/),
    ok: async ({ page }) => {
      await waitText(page, `${replica} .ops-loop`, /Reached the maximum number of skips/);
      await focus(page, `${replica} .ops-loop`);
    },
    error: async ({ page }) => {
      await waitText(page, `${replica} [role=alert]`, /Simulated failure/);
      await focus(page, `${replica} .replication-write-result`);
    },
    hang: async ({ page, ctx }) => {
      await waitText(page, `${replica} .ops-loop .action-status`, /No reply to Stop yet; the loop may still be running\./);
      await waitText(page, `${replica} .ops-loop .action-elapsed`, /^\d+ s so far$/);
      assert.equal(count(ctx, loopStop), 1, "Stop is single-flight while its reply is missing");
    },
  };
  const killReply = { schema_version: "kill-1.0", status: "killed", connection_id: 94, mode: "query", statement: "KILL QUERY 94",
    verified: ["user", "host", "not this session"], after: { present: true },
    message: "Stopped the running statement on connection 94. The connection stays open." };
  const kill = {
    name: "sessions-kill", label: "Processlist: Kill query (confirm)", paths: [killPath],
    mocks: { [killPath]: () => killReply }, replyDelayMs: 2000, sheet: true,
    setup: async ({ page }) => {
      await goView(page, "workload", "sessions");
      await (await visible(page, `${sessions} tr[data-session="94"] td:nth-child(3)`)).click();
      await (await visible(page, '.view-inspector [aria-label="Kill: connection 94"]')).click();
      await visible(page, "dialog.kill-dialog[open]");
      assert.equal(await page.$eval('.kill-choices input[value="query"]', (input) => input.checked), true,
        "running session 94 starts with Kill query selected");
    },
    target: confirmButton("dialog.kill-dialog[open]"),
    afterClick: async ({ ctx }) => {
      const request = await heldFor(ctx, [killPath]);
      assert.equal(request.body.connection_id, 94, "the write names the session reviewed");
      assert.equal(request.body.mode, "query", "the reply fixture matches the reviewed Kill query");
      ctx.hold.add("/api/live/sessions"); ctx.operationReads = count(ctx, "/api/live/sessions");
    },
    ok: async ({ page, ctx }) => {
      await waitText(page, `${sessions} .kill-result`, /^Done\. Stopped the running statement on connection 94/);
      await focus(page, `${sessions} .kill-result`);
      await heldFor(ctx, ["/api/live/sessions"]);
      assert.equal(await page.$(`${sessions} .kill-result-on`), null, "same-connection result does not repeat its origin");
    },
    error: async ({ page }) => { await waitText(page, `${sessions} .kill-result`, /Could not end it\. Simulated failure/); await focus(page, `${sessions} .kill-result`); },
    hang: async ({ page, ctx }) => {
      await close(page, "dialog.kill-dialog[open]");
      await waitText(page, `${sessions} .action-status`, /Waiting for the reply to the KILL of connection 94\./);
      assert.equal(count(ctx, "/api/live/sessions"), ctx.operationReads, "closing a pending Kill does not reread Processlist");
      const sentOn = await switchConnection(page);
      const write = await heldFor(ctx, [killPath]);
      ctx.held.splice(ctx.held.indexOf(write), 1);
      await respond(write.request, 200, killReply);
      await waitText(page, `${sessions} .kill-result`, /^Done\. Stopped the running statement on connection 94/);
      await waitText(page, `${sessions} .kill-result-on`, new RegExp(`^Sent on ${escapeRe(sentOn)}, not the connection in use\\.$`));
      assert.equal(await page.$(`${sessions} .kill-result ::-p-text(Refresh)`), null, "a result from another connection offers no Refresh");
      assert.equal(count(ctx, "/api/live/sessions"), ctx.operationReads, "a late Kill reply never refreshes the other connection");
    },
  };

  const single = parameterWrite("single"), reset = parameterWrite("reset"), batch = parameterWrite("batch");
  const skip = replicationWrite(false), start = replicationWrite(true);
  return [
    viewRefresh("sessions", "workload", "/api/live/sessions", { label: "Processlist", replyDelayMs: 2000 }),
    viewRefresh("config", "server", "/api/live/config", { label: "Parameters", replyDelayMs: 2000 }),
    viewRefresh("replication", "replication", replicationPath, { label: "Replication", replyDelayMs: 2000,
      mocks: replicaMocks, go: async (page) => openReplication(page, false) }),
    kill, single, reset, batch,
    transport(single, `${config} .variable-result`), transport(reset, `${config} .variable-result`), transport(batch, `${config} .parameter-results`),
    skipOpen(false), skipOpen(true), skip, start, stop,
    transport(skip, `${replica} .replication-write-result`), transport(start, `${replica} .replication-write-result`),
    transport(stop, `${replica} .replication-write-result`),
  ];
}
