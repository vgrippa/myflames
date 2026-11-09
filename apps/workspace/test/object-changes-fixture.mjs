// Synthetic local audit data. These statements are display-only fixtures.
import assert from "node:assert/strict";

export function objectAuditFixture(body) {
  if (body.kind === "index" && body.key.index !== "k_status") return { status: 200, body: { schema_version: "audit-1.0", records: [] } };
  const parameter = body.kind === "parameter";
  const index = body.kind === "index";
  const target = index ? { alter_table: body.key } : parameter ? { name: body.key, mode: "global", global_value: body.key === "slow_query_log" ? "OFF" : "151" }
    : { account: "`app`@`%`", change_set_id: "fixture" };
  return { status: 200, body: { schema_version: "audit-1.0", records: [{
    id: "a".repeat(32), attempted_at: "2026-11-08T12:00:00+00:00", finished_at: "2026-11-08T12:00:01+00:00",
    status: "ok", operation: index ? "editor_run" : parameter ? "set_global" : "accounts.grant", outcome: "changed", outcome_unknown: false,
    surface: "ui", os_user: "fixture", connection: { host: "127.0.0.1", port: 3306, engine: "mysql" },
    target, statements: [index ? "ALTER TABLE `mfshop`.`orders` ALTER INDEX `k_status` INVISIBLE" : parameter ? `/*myflames:variable*/ SET GLOBAL ${body.key} = 200`
      : "GRANT SELECT ON `</script><img data-history-injection>`.* TO `app`@`%`"],
    verified: null, policy: null, error_code: null, message: null, rows_affected: null, after: null, duration_ms: 1,
  }] } };
}

export async function assertObjectHistory(page, kind, { empty = false } = {}) {
  const selector = `.view-inspector [data-object-changes="${kind}"]`;
  if (empty) {
    // Wait for the lookup before asserting absence, so loading cannot pass.
    await page.waitForNetworkIdle({ idleTime: 100 });
    assert.equal(await page.$(selector), null, "unmatched indexes have no history section");
    return;
  }
  await page.waitForSelector(`${selector}[aria-busy="false"]`);
  const content = await page.$eval(selector, e => e.textContent);
  assert.match(content, /Kept on this computer\./);
  assert.doesNotMatch(content, /Changes through myflames on this computer/);
  // Parameters also exercise Undo for a persisted entry in config.mjs.
  const labels = kind === "parameter" ? {
    set_global: "Set global parameter", set_persist: "Set and persist parameter",
    set_persist_only: "Persist parameter for restart", set_session: "Set session parameter", reset_persist: "Reset persisted parameter",
  } : kind === "index" ? { editor_run: "Run SQL" } : { "accounts.grant": "Grant privileges" };
  const operation = await page.$eval(`${selector} dd bdi[title]`, e => ({ key: e.title, label: e.textContent }));
  assert.ok(Object.hasOwn(labels, operation.key), "the raw operation key is available on hover");
  assert.equal(operation.label, labels[operation.key]);
  assert.ok(await page.$(`${selector} pre.inspector-sql[aria-label="Statement as stored"]`));
  assert.ok(await page.$(`${selector} pre code bdi`), "stored SQL has bidi isolation");
  const beside = await page.$eval(`${selector} .object-change-statement`, e => {
    const pre = e.querySelector('pre').getBoundingClientRect();
    const copy = e.querySelector('button').getBoundingClientRect();
    return copy.left >= pre.right && Math.abs(copy.top - pre.top) < 2;
  });
  assert.equal(beside, true, "Copy sits beside the boxed SQL");
  {
    assert.match(content, /changed/);
    assert.equal(await page.$eval(`${selector} time`, e => e.title), "2026-11-08T12:00:00+00:00");
    assert.ok(await page.$(`${selector} pre[aria-label="Statement as stored"]`));
    assert.ok(await page.$(`${selector} button[aria-label="Copy statement as stored"]`));
    await page.click(`${selector} button[aria-label="Copy statement as stored"]`);
    await page.waitForFunction(s => /Copied\.|Select the text to copy it\./.test(document.querySelector(s)?.textContent || ""), {}, selector);
    assert.equal(await page.$("img[data-history-injection]"), null, "stored SQL stays text");
    assert.match(content, /Open in Audit log/);
    if (kind === "account") assert.doesNotMatch(content, /Undo/);
  }
}
