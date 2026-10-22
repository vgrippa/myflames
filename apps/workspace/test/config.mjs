import { chooseConnection, connectionOption } from "./chrome-helpers.mjs";
// Parameters: synthetic catalog metadata on recorded config-1.0 reports,
// mocked planning and apply endpoints, bounded batch review, declined audits,
// partial success, legacy fallback, source inspection and responsive tables.
// Single-variable sheets retain engine-specific modes, exact reviewed facts,
// NULL and Session SQL handoff, declined audits and connection binding.
import assert from "node:assert/strict";
import path from "node:path";
import { assertPrivilege, privilegeAnswer, load, open, pause } from "./perf-harness.mjs";

// Execute the production SQL handoff without a browser or server. The
// shell owns callback wiring; no execution API participates in this path.
if (process.argv.includes("--unit")) {
  const { build } = await import("vite");
  const fs = await import("node:fs/promises");
  const { fileURLToPath, pathToFileURL } = await import("node:url");
  const workspace = fileURLToPath(new URL("../", import.meta.url));
  const entry = "\0config-unit";
  const compiled = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
    plugins: [{ name: "config-unit", enforce: "pre", transform(code, id) {
      if (id.endsWith("/src/Config.tsx")) return code + "\nexport { parametersOf, allowedValues, unmodifiable, draftValue, sourceDetails, applyType, planParameterBatch, applyParameterBatch };";
    }, resolveId(id) { if (id === entry) return entry; }, load(id) {
      if (id === entry) return `export { openSessionSql, documentationLink, oneLine, parametersOf, allowedValues, unmodifiable, draftValue, sourceDetails, applyType, planParameterBatch, applyParameterBatch } from ${JSON.stringify(path.join(workspace, "src/Config.tsx"))}; export { connectionKey } from ${JSON.stringify(path.join(workspace, "src/binding.ts"))};`;
    } }], build: { ssr: true, write: false, rollupOptions: { input: entry, output: { inlineDynamicImports: true } } } });
  const temporary = await fs.mkdtemp(path.join(workspace, ".config-unit-"));
  try {
    const module = path.join(temporary, "unit.mjs");
    await fs.writeFile(module, compiled.output.find(f => f.type === "chunk").code);
    const { openSessionSql, documentationLink, oneLine, parametersOf, allowedValues, unmodifiable, draftValue, sourceDetails, applyType, planParameterBatch, applyParameterBatch, connectionKey } = await import(pathToFileURL(module));
    globalThis.fetch = () => { throw new Error("A handoff must never call an API"); };
    const opened = [], callback = sql => opened.push(sql);
    for (const statement of ["SET SESSION character_set_results = NULL", "SET SESSION character_set_results = 'NULL'", "SET SESSION sql_mode = 'STRICT_TRANS_TABLES';"]) {
      assert.equal(openSessionSql({ mode: "session", status: "ready", statement }, callback), true);
      assert.equal(opened.at(-1), statement.endsWith(";") ? statement : statement + ";");
    }
    const base = { mode: "session", status: "ready", statement: "SET SESSION sort_buffer_size = 1048576" };
    assert.equal(openSessionSql(base), false);
    for (const patch of [{ status: "refused" }, { mode: "global" }, { statement: null }])
      assert.equal(openSessionSql({ ...base, ...patch }, callback), false);
    assert.equal(opened.length, 3);
    const mysql = await load("parameters-mysql.json"), maria = await load("parameters-mariadb.json"), old = await load("config-mysql-8.4.json");
    for (const report of [mysql, maria]) {
      assert.ok(report.parameters.items.length >= 40);
      assert.deepEqual(parametersOf(report), report.parameters.items);
      assert.equal(new Set(report.parameters.items.map(p => p.name)).size, report.parameters.items.length);
      for (const p of report.parameters.items) {
        assert.ok(["dynamic", "static", null].includes(p.apply_type));
        assert.ok(["now", "restart", "option_file", null].includes(p.modifiable));
        assert.equal(typeof allowedValues(p), "string");
      }
    }
    const rows = parametersOf(mysql), find = name => rows.find(p => p.name === name);
    assert.equal(allowedValues(find("slow_query_log")), "OFF, ON");
    assert.equal(allowedValues(find("log_output")), "FILE, TABLE, NONE");
    assert.equal(allowedValues(find("innodb_buffer_pool_size")), "5.0 MiB to 1.0 TiB");
    assert.equal(allowedValues({ ...find("max_connections"), min: 0, max: null }), "0 to ?");
    assert.equal(allowedValues({ ...find("max_connections"), type: "string" }), "Any text");
    assert.equal(allowedValues(find("unknown_parameter")), "");
    assert.equal(draftValue({ ...find("slow_query_log"), value: "1" }), "ON");
    assert.equal(draftValue({ ...find("log_output"), value: "file" }), "FILE");
    assert.equal(unmodifiable(find("max_connections"), "mysql"), "");
    assert.equal(unmodifiable(find("innodb_log_file_size"), "mysql"), "");
    assert.match(unmodifiable(find("innodb_log_file_size"), "mariadb"), /Option file only/);
    assert.match(unmodifiable(find("unknown_parameter"), "mysql"), /cannot tell/);
    assert.match(unmodifiable(find("truncated_parameter"), "mysql"), /truncated/);
    assert.equal(applyType(find("unknown_parameter")), "Unknown");
    assert.equal(sourceDetails(find("modified_parameter")), "/etc/my.cnf · admin@localhost at 2026-10-18T12:00:00Z");
    // Documentation links: only HTTPS pages of the official manuals the desktop app opens.
    for (const url of ["https://dev.mysql.com/doc/refman/8.4/en/innodb-parameters.html#sysvar_innodb_buffer_pool_size",
      "https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables#innodb_buffer_pool_size",
      "https://docs.percona.com/percona-server/8.4/innodb-corrupt-table-action.html#innodb_corrupt_table_action"])
      assert.deepEqual(documentationLink({ url, source: "Manual" }), { url, source: "Manual" });
    for (const url of ["http://dev.mysql.com/doc/", "https://dev.mysql.com.evil.test/", "https://user@dev.mysql.com/", "https://dev.mysql.com:8443/",
      "javascript:alert(1)", "https://example.com/", "not a url"])
      assert.equal(documentationLink({ url, source: "Manual" }), null, url);
    assert.equal(documentationLink(null), null); assert.equal(documentationLink(undefined), null);
    assert.ok(mysql.parameters.items.filter(p => p.documentation).every(p => documentationLink(p.documentation)), "every fixture link is accepted");
    // One sentence: the server's description, else the opening sentence of a check about this parameter first.
    const checksOf = p => p.checks.map(k => mysql.checks.find(c => c.key === k)).filter(Boolean);
    assert.equal(oneLine(find("slow_query_log"), checksOf(find("slow_query_log"))), "The slow query log records statements that ran longer than long_query_time.");
    assert.equal(oneLine({ ...find("innodb_buffer_pool_size"), description: null }, checksOf(find("innodb_buffer_pool_size"))),
      "The memory InnoDB uses to cache data and index pages.");
    assert.equal(oneLine({ ...find("long_query_time"), description: null }, checksOf(find("long_query_time"))), "", "a check about another parameter first is not this parameter's description");
    assert.equal(oneLine({ ...find("max_connections"), description: "Uses 2.5 MiB per thread. Second sentence." }, []), "Uses 2.5 MiB per thread.");
    assert.ok(oneLine({ ...find("max_connections"), description: "x".repeat(600) }, []).length <= 240);
    const fallback = parametersOf(old), unique = new Set(old.checks.flatMap(c => c.settings.map(s => s.name)));
    assert.equal(fallback.length, unique.size);
    const pool = fallback.find(p => p.name === "innodb_buffer_pool_size");
    // A check describes its first setting only: innodb_dedicated_server takes the Dedicated server check's text.
    assert.equal(pool.description, old.checks.find(c => c.key === "buffer_pool").explain);
    assert.equal(fallback.find(p => p.name === "innodb_dedicated_server").description, old.checks.find(c => c.key === "dedicated_server").explain);
    assert.equal(fallback.find(p => p.name === "temptable_max_ram").description, null);
    assert.equal(pool.modified, false, "legacy defaults compare the formatted display with the formatted default");
    assert.equal(pool.type, null, "fallback does not invent catalog metadata");
    const duplicate = structuredClone(old);
    duplicate.checks.push({ ...duplicate.checks[0], key: "another_check" });
    assert.ok(parametersOf(duplicate).find(p => p.name === pool.name).checks.includes("another_check"));
    const staged = ["max_connections", "slow_query_log", "long_query_time"].map(name => ({ parameter: find(name), value: name === "slow_query_log" ? "ON" : "300" }));
    const intents = staged.map(r => ({ name: r.parameter.name, value: r.value, mode: "persist" }));
    const connection = { host: "db", port: 3306, user: "root", ssl_mode: "PREFERRED" };
    const via = { connection, key: connectionKey(connection), label: "root@db:3306" };
    const plans = Object.fromEntries(intents.map(i => [i.name, { name: i.name, requested: i.value, mode: i.mode, expect: { statement: `SET PERSIST ${i.name} = ${i.value}` } }]));
    for (const stopStatus of ["denied", "unchanged", "applied_unverified", "refused_by_server", "error"]) {
      const sent = []; let checks = 0, active = 0, peak = 0;
      const results = await applyParameterBatch(staged, intents, via, plans, async () => { checks++; return connection; }, () => via.key,
        async (intent, plan) => {
          active++; peak = Math.max(peak, active); sent.push(intent.name);
          assert.equal(plan, plans[intent.name], "the original reviewed expect is preserved");
          await Promise.resolve(); active--;
          if (sent.length === 2 && stopStatus === "error") throw new Error("The connection was lost.");
          return { name: intent.name, status: sent.length === 2 ? stopStatus : "changed", message: "Read-back result" };
        });
      assert.equal(checks, 1, "policy re-read exactly once per batch");
      assert.equal(peak, 1, "writes run sequentially");
      assert.deepEqual(sent, intents.slice(0, 2).map(i => i.name));
      // A thrown request may have run on the server: it is reported as unknown, never unchanged.
      assert.deepEqual(results.map(r => r.status), ["changed", stopStatus === "error" ? "unknown" : stopStatus, "not_sent"]);
      if (stopStatus === "error") assert.match(results[1].message, /outcome is unknown/);
    }
    let writes = 0;
    for (const check of [async () => ({ ...connection, read_only: true }), async () => { throw new Error("Policy unavailable"); }]) {
      const results = await applyParameterBatch(staged, intents, via, plans, check, () => via.key, async () => { writes++; });
      assert.equal(writes, 0); assert.ok(results.every(r => r.status === "not_sent" && /nothing was sent/.test(r.message)));
    }
    let activeKey = via.key;
    const moved = await applyParameterBatch(staged, intents, via, plans, async () => connection, () => activeKey,
      async () => { writes++; activeKey = "another connection"; return { status: "changed", message: "Changed" }; });
    assert.equal(writes, 1); assert.deepEqual(moved.map(r => r.status), ["changed", "not_sent", "not_sent"]);
    const allChanged = await applyParameterBatch(staged, intents, via, plans, undefined, () => via.key, async () => ({ status: "changed", message: "Changed" }));
    assert.ok(allChanged.every(r => r.status === "changed"));
    const many = Array.from({ length: 12 }, (_, n) => ({ name: `p${n}`, value: "1", mode: "global" }));
    const controller = new AbortController(), accepted = [], rejected = [];
    let inFlight = 0, peak = 0;
    await planParameterBatch(many, controller.signal, async intent => {
      inFlight++; peak = Math.max(peak, inFlight); await Promise.resolve(); inFlight--;
      if (intent.name === "p5") throw new Error("No metadata");
      return { name: intent.name };
    }, (name, plan) => { assert.equal(name, plan.name); accepted.push(name); }, (name, error) => rejected.push([name, error]));
    assert.equal(peak, 3, "planning uses at most three readers");
    assert.equal(accepted.length, 11); assert.deepEqual(rejected, [["p5", "No metadata"]]);
    const aborted = new AbortController(); let queued = 0, staleAnswers = 0;
    await planParameterBatch(many, aborted.signal, async () => { queued++; aborted.abort(); return {}; }, () => staleAnswers++, () => staleAnswers++);
    assert.equal(queued, 1, "abort leaves queued reads unstarted");
    assert.equal(staleAnswers, 0, "answers from an obsolete planning generation are ignored");
    console.log("config unit: catalog and Session SQL; three-reader planning, obsolete responses, sequential apply, early stop, policy drift and connection changes passed");
  } finally { await fs.rm(temporary, { recursive: true, force: true }); }
  process.exit(0);
}

const mysql = await load("parameters-mysql.json");
const mariadb = await load("parameters-mariadb.json");
const legacy = await load("config-mysql-8.4.json");
const report = await load("report-mysql-8.4.json");
const variableFixture = await load("variable-mysql-8.4-global.json");
const mariaFixture = await load("variable-mariadb-11.4-global.json");
let catalog = mysql;
const applied = [], planned = [], declined = [], variableReads = [];
let planReply = null, applyReply = null, variableReply = null;
let useSingleFixtures = false;
const variables = {
  "mysql:max_connections": await load("variable-mysql-8.4-global.json"),
  "mysql:innodb_log_file_size": await load("variable-mysql-8.4-persist-only.json"),
  "mariadb:max_connections": await load("variable-mariadb-11.4-global.json"),
  "mariadb:performance_schema": await load("variable-mariadb-11.4-readonly.json"),
};
const VERB = { global: "SET GLOBAL", persist: "SET PERSIST", persist_only: "SET PERSIST_ONLY", session: "SET SESSION" };
const fx = (name) => variables[`${catalog.engine}:${name}`];
// The recordings predate these additive fields. Supply deterministic metadata
// so the UI regressions can distinguish the initial read from a later plan.
const readAt = "2026-10-15T14:30:00+00:00";
const persistedState = (v) => ({
  state: !v.persisted.supported ? "unsupported" : v.persisted.present === null ? "unreadable" : v.persisted.present ? "present" : "absent",
  present: v.persisted.present, value: v.persisted.value,
});
for (const f of Object.values(variables)) {
  for (const v of [f.variable, f.plan.variable]) Object.assign(v, { nullable: false, read_at: readAt });
  f.plan.before.persisted_state = persistedState(f.plan.variable);
}
// A synthetic nullable session-variable envelope exercises JSON null and the
// literal string "NULL" separately; it makes no assertion about server writes.
const nullable = structuredClone(variables["mysql:max_connections"]);
Object.assign(nullable.variable, {
  name: "character_set_results", type: "string", scope: "both", nullable: true,
  min: null, max: null, global_value: "utf8mb4", session_value: "latin1", default: "utf8mb4",
});
for (const [m, data] of Object.entries(nullable.variable.modes)) {
  Object.assign(data, { available: m === "session", recommended: m === "session", reason: null });
}
Object.assign(nullable.plan, {
  name: "character_set_results", mode: "session", requested: "latin1", value: "latin1", display: "latin1",
  statement: "SET SESSION character_set_results = 'latin1'", variable: structuredClone(nullable.variable),
  before: { global: "utf8mb4", session: "latin1", persisted: null, persisted_state: persistedState(nullable.variable) },
  undo: ["SET SESSION character_set_results = 'latin1';"], caveats: [],
  effect: { now: true, after_restart: false, needs_restart: false, text: "Changes this myflames session." },
  privileges: { need: "no privilege", note: "", checked: true, allowed: true, via: null },
});
nullable.plan.expect = { name: "character_set_results", mode: "session", statement: nullable.plan.statement, global_value: "utf8mb4", session_value: "latin1", persisted_value: null };
variables["mysql:character_set_results"] = nullable;
function singlePlanFor(body) {
  const plan = structuredClone(fx(body.name).plan);
  if (plan.requested === body.value && plan.mode === body.mode) return plan;
  plan.requested = body.value;
  plan.value = body.value;
  plan.display = body.value;
  plan.mode = body.mode;
  if (plan.status === "ready") {
    const literal = body.value === null ? "NULL" : plan.variable.type === "string" ? `'${body.value}'` : body.value;
    plan.statement = body.mode === "reset_persist" ? `RESET PERSIST ${body.name}` : `${VERB[body.mode]} ${body.name} = ${literal}`;
    plan.expect = { ...plan.expect, mode: body.mode, statement: plan.statement };
  }
  return plan;
}

let planningActive = 0, planningPeak = 0;
const verb = { global: "SET GLOBAL", persist: "SET PERSIST", persist_only: "SET PERSIST_ONLY", session: "SET SESSION", reset_persist: "RESET PERSIST" };
const parameter = name => catalog.parameters.items.find(p => p.name === name);
function variableFor(name) {
  if (useSingleFixtures) return structuredClone(fx(name).variable);
  const p = parameter(name);
  const v = structuredClone((catalog.engine === "mysql" ? variableFixture : mariaFixture).variable);
  Object.assign(v, { name, type: p?.type || "integer", values: p?.values || null, min: p?.min ?? 1, max: p?.max ?? 100000,
    global_value: p?.value ?? "151", default: p?.default ?? "151", scope: "both", session_value: "151", nullable: false,
    dynamic: p?.apply_type === "dynamic", description: p?.description, read_at: "2026-10-18T12:00:00Z" });
  for (const [mode, m] of Object.entries(v.modes)) {
    m.available = mode === "global" || mode === "session" || catalog.engine === "mysql";
    m.recommended = mode === "global";
  }
  if (p?.modifiable === "restart") for (const [mode, m] of Object.entries(v.modes)) m.available = mode === "persist_only" || mode === "reset_persist";
  if (p?.modifiable === "option_file") for (const m of Object.values(v.modes)) m.available = false;
  return v;
}
function planFor(body) {
  if (useSingleFixtures) return singlePlanFor(body);
  const v = variableFor(body.name);
  const p = structuredClone((catalog.engine === "mysql" ? variableFixture : mariaFixture).plan);
  Object.assign(p, { engine: catalog.engine, name: body.name, variable: v, mode: body.mode, requested: body.value, value: body.value, display: body.value,
    statement: body.mode === "reset_persist" ? `${verb[body.mode]} ${body.name}` : `${verb[body.mode]} ${body.name} = ${body.value}`,
    status: v.modes[body.mode].available ? "ready" : "refused", problems: [], before: { global: v.global_value, session: v.session_value, persisted: null },
    policy: { action: "confirm", tone: "danger", title: "Production", text: "Every change on this Production connection asks first." },
    privileges: { need: "SYSTEM_VARIABLES_ADMIN", note: "", checked: true, allowed: true, via: null },
    config_line: `${body.name} = ${body.value}` });
  p.expect = { name: body.name, mode: body.mode, statement: p.statement, global_value: v.global_value, session_value: v.session_value, persisted_value: null };
  p.effect = { now: body.mode !== "persist_only", after_restart: body.mode !== "global", needs_restart: body.mode === "persist_only",
    text: body.mode === "persist_only" ? "The value applies after the next restart." : "Changes the running value." };
  return p;
}
// The list of views docked, as a person who docked it: this suite asserts
// the list's markers and keys (server-nav covers the floating default).
const h = await open("/api/live/config", "parameters", {
  "/api/live/variable": (body, request) => {
    variableReads.push(body);
    const answer = variableReply ? variableReply(body) : { status: 200, body: variableFor(body.name) };
    if (!answer.delay) return answer;
    setTimeout(() => { void request.respond({ status: answer.status, contentType: "application/json", body: JSON.stringify(answer.body) }).catch(() => undefined); }, answer.delay);
    return null;
  },
  "/api/live/variable/plan": (body, request) => {
    planned.push(body); planningActive++; planningPeak = Math.max(planningPeak, planningActive);
    const answer = planReply ? planReply(body) : { status: 200, body: planFor(body) };
    setTimeout(() => { planningActive--; void request.respond({ status: answer.status, contentType: "application/json", body: JSON.stringify(answer.body) }).catch(() => undefined); }, answer.delay ?? 40);
    return null;
  },
  "/api/live/variable/apply": (body, request) => {
    if (body.declined) { declined.push(body); void request.continue(); return null; }
    applied.push(body);
    if (useSingleFixtures && !applyReply) return { status: 200, body: fx(body.name).change };
    return applyReply ? applyReply(body) : { status: 200, body: { ...variableFixture.change, name: body.name, mode: body.mode, status: "changed", message: "The new value was read back." } };
  },
  "/api/live/report": { status: 200, body: report },
}, { list: "docked" });
const { page, state, text, texts } = h;
state.fallback = { status: 200, body: mysql };
const view = '.server-view[data-view="config"]';
const row = name => `${view} tr[data-row-key="${name}"]`;
const filter = `${view} .toolbar-field select`;
const batch = "dialog.parameter-batch-sheet[open]";
const confirm = `${batch} .sheet-actions button:last-of-type`;
const single = "dialog.variable-sheet[open]";
const singleConfirm = `${single} .sheet-actions button:last-of-type`;
const input = name => `${view} #parameter-value-${name}`;
const pendingCount = n => page.waitForFunction((v, count) => [...document.querySelectorAll(`${v} .toolbar-controls button`)].some(e => e.textContent === `Review and apply (${count})`), {}, view, n);
const waitFor = async (check, message) => {
  for (let i = 0; i < 100; i++) { if (check()) return; await pause(30); }
  assert.fail(message);
};
async function back() {
  const button = await page.$(`${view} .detail-back`);
  if (button) { await button.click(); await page.waitForFunction(v => !document.querySelector(`${v} .view-inspector`), {}, view); }
}
async function search(value, expectedCount) {
  await page.$eval(`${view} input[aria-label="Filter parameters"]`, e => { e.focus(); e.select(); });
  await page.keyboard.press("Backspace");
  if (value) await page.type(`${view} input[aria-label="Filter parameters"]`, value);
  if (expectedCount !== undefined) {
    const selector = `${view} table[aria-label="Parameters"] tbody tr[data-row-key]`;
    await page.waitForFunction((s, count) => document.querySelectorAll(s).length === count,
      { timeout: 5000 }, selector, expectedCount).catch(error => {
      if (error.name !== "TimeoutError") throw error;
      throw new Error(`Filter parameters ${JSON.stringify(value)}: expected ${expectedCount} rendered rows within 5000 ms (${selector})`);
    });
  }
}
async function edit() { await page.click(`${view} [data-action="edit-parameters"]`); }
async function stage(name, value) {
  await search(name);
  await page.waitForSelector(input(name));
  if (await page.$eval(input(name), e => e.getAttribute("role") === "switch")) { await page.click(input(name)); return; }
  if (await page.$eval(input(name), e => e.tagName === "SELECT")) { await page.select(input(name), value); return; }
  await page.click(input(name), { clickCount: 3 }); await page.keyboard.press("Backspace"); await page.type(input(name), value);
}
async function review(count) {
  await pendingCount(count);
  // Server views scroll as a page; the sticky safety strip must not cover the toolbar.
  await page.evaluate(() => window.scrollTo(0, 0));
  await h.button(`Review and apply (${count})`, `${view} `);
  await page.waitForSelector(batch);
  await page.waitForFunction(s => !document.querySelector(s).disabled, {}, confirm);
}
async function closeReview(key = "Escape") {
  const before = declined.length;
  if (key === "Escape") await page.keyboard.press(key); else await page.click(`${batch} .sheet-actions button:first-child`);
  await page.waitForFunction(() => !document.querySelector("dialog.parameter-batch-sheet"));
  await waitFor(() => declined.length > before, "Cancel records the declined rows");
}
async function refresh(next) {
  catalog = next;
  state.fallback = { status: 200, body: next };
  await back();
  const n = state.seen.length; await h.refresh();
  await waitFor(() => state.seen.length > n, "Refresh reads the parameters"); await h.idle(view);
}
async function picture(name, selector = ".server-place") {
  await (await page.$(selector)).screenshot({ path: path.join(h.shots, name) });
}
const audit = () =>
  page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    const response = await fetch("/api/audit/list", {
      method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: "{}",
    });
    return (await response.json()).records;
  });
const statement = () => page.$eval(`${single} .variable-statement`, (e) => e.textContent).catch(() => "");
async function setValue(name, value) {
  const input = `${single} #variable-value-${name}`;
  await page.click(input, { clickCount: 3 });
  await page.keyboard.press("Backspace");
  await page.type(input, value);
}
async function openChange(name) {
  await page.click(`.view-inspector button[data-change="${name}"]`);
  await page.waitForSelector(single);
  await page.waitForFunction((s) => document.querySelector(`${s} .variable-facts`) || document.querySelector(`${s} [role=alert]`), {}, single);
}
async function cancelAndAudit({ operation = "set_global", name = "max_connections", requested, statements = [], key = "Escape", focus = true }) {
  const before = (await audit()).length, writes = applied.length, attempts = declined.length;
  if (key === "Escape") await page.keyboard.press("Escape");
  else await page.click(`${single} .sheet-actions button:first-child`);
  await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
  let records = [];
  for (let i = 0; i < 50; i++) {
    records = await audit();
    if (records.length > before) break;
    await pause(50);
  }
  assert.equal(applied.length, writes, "cancelling never sends a write");
  assert.equal(declined.length, attempts + 1, "one declined request per sheet");
  assert.equal(records.length, before + 1, "every cancelled sheet adds exactly one audit record");
  assert.equal(records[0].status, "declined");
  assert.equal(records[0].operation, operation);
  assert.equal(records[0].target.name, name);
  assert.equal(records[0].target.requested, requested);
  assert.deepEqual(records[0].statements, statements);
  if (!statements.length) assert.equal(records[0].target.statement, undefined, "no stale expect statement in an unreviewed intent");
  assert.equal(declined.at(-1).reviewed_statement, statements.length ? statements[0].replace("/*myflames:variable*/ ", "") : null);
  if (!statements.length) assert.equal(declined.at(-1).expect, null, "an obsolete plan is not audited");
  if (focus) await page.waitForFunction(n => document.activeElement?.dataset?.change === n || document.activeElement?.dataset?.reset === n, {}, name);
  return records[0];
}
try {
  await h.server(); await h.view("config");
  await page.waitForSelector(`${view} .summary-line`); await h.idle(view);
  assert.equal(await text(`${view} h2`), "Parameters");
  assert.equal(state.seen.length, 1, "one automatic read on open");
  assert.equal(await page.$(`${view} .view-empty`), null);
  assert.match(await text(`${view} .summary-line`), /\d+ parameters, \d+ modified.*0 problems, 1 to watch/);
  assert.match(await text(`${view} .fact-strip`), /MySQL 8\.4/);
  assert.match(await text("#server-nav-config"), /Parameters/);
  assert.equal(await page.$eval(`${view} table[aria-label="Parameters"]`, e => e.closest(".data-table-frame") !== null), true);
  assert.ok(mysql.parameters.items.length >= 40);
  assert.equal(await page.$$eval(`${view} table[aria-label="Parameters"] tbody tr[data-row-key]`, es => es.length), mysql.parameters.items.length);
  assert.equal(variableReads.length, 0, "catalog rows do not read metadata individually");
  assert.match(await h.popover("About Parameters"), /stages local drafts.*every exact statement/s);
  assert.match(await text(row("innodb_buffer_pool_size")), /MiB|GiB/);
  assert.match(await text(row("unknown_parameter")), /SQL NULL.*Unknown/);
  assert.match(await text(row("log_output")), /FILE, TABLE, NONE/);
  assert.equal(await page.$eval(`${row("modified_parameter")} td[data-col=source] span`, e => e.title), "/etc/my.cnf · admin@localhost at 2026-10-18T12:00:00Z");

  for (const [choice, predicate] of [
    ["modified", p => p.modified === true], ["dynamic", p => p.apply_type === "dynamic"], ["static", p => p.apply_type === "static"],
    ["recommendations", p => p.checks.some(k => mysql.checks.some(c => c.key === k && ["watch", "problem"].includes(c.status)))],
  ]) {
    await page.select(filter, choice);
    assert.deepEqual((await page.$$eval(`${view} table[aria-label="Parameters"] tbody tr[data-row-key]`, es => es.map(e => e.dataset.rowKey))).sort(), mysql.parameters.items.filter(predicate).map(p => p.name).sort(), `filter ${choice}`);
  }
  await page.select(filter, "all");
  for (const term of ["catalog_parameter_05", "2048", "for searching"]) {
    const wanted = mysql.parameters.items.filter(p => [p.name, p.value || "", p.description || ""].some(v => v.toLowerCase().includes(term)));
    await search(term, wanted.length);
    assert.equal(await page.$$eval(`${view} table[aria-label="Parameters"] tbody tr[data-row-key]`, es => es.length), wanted.length);
  }
  await search("");
  // One compact section per parameter: a sentence, the documentation link and
  // the facts; a flagged parameter adds one recommendation with its reason,
  // the suggested change and the caveat. Nothing is repeated.
  await h.select(row("slow_query_log"));
  const slow = mysql.checks.find(c => c.key === "slow_log");
  const inspected = await h.inspector();
  assert.deepEqual(await texts(".view-inspector .inspector-block h4"), ["Current value", "Recommendation"]);
  assert.equal((inspected.match(/The slow query log records statements/g) || []).length, 1, "the description appears once");
  assert.ok(inspected.includes(slow.why) && inspected.includes(slow.caveat), "the reason and the caveat are shown");
  assert.doesNotMatch(inspected, /Explanation and recommendations|What it is|Why it is flagged|Apply type|Scope/);
  assert.equal(await page.$$eval(".view-inspector .parameter-finding", es => es.length), 1);
  assert.ok(await page.$(".view-inspector .parameter-finding .perf-sql pre"), "the suggested change is copyable");
  const docLink = await page.$eval('.view-inspector a[data-doc-link="slow_query_log"]', a => ({ href: a.href, target: a.target, rel: a.rel, text: a.textContent }));
  assert.equal(docLink.href, mysql.parameters.items.find(p => p.name === "slow_query_log").documentation.url);
  assert.equal(docLink.target, "_blank"); assert.match(docLink.rel, /noreferrer/);
  assert.match(docLink.text, /^Documentation for slow_query_log, opens in your browser$/);
  assert.match(inspected, /MySQL 8\.4 Reference Manual/);
  assert.match(inspected, /Changes\s*Now/);
  // Current value and Recommendation sit side by side only when both have room:
  // in the 720 px window the recommendation follows the facts, full width.
  const blocks = () => page.$$eval(".view-inspector .inspector-block", es => es.map(e => { const r = e.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, width: r.width }; }));
  let [facts, advice] = await blocks();
  assert.ok(Math.abs(facts.top - advice.top) < 2 && advice.left > facts.left + facts.width - 1, JSON.stringify([facts, advice]));
  await page.setViewport({ width: 720, height: 520 });
  await page.waitForFunction(() => { const [a, b] = document.querySelectorAll(".view-inspector .inspector-block"); return b && b.getBoundingClientRect().top >= a.getBoundingClientRect().bottom; });
  [facts, advice] = await blocks();
  assert.ok(advice.top >= facts.bottom && Math.abs(advice.left - facts.left) < 2 && advice.width >= facts.width - 1, JSON.stringify([facts, advice]));
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth), 0, "no horizontal overflow at 720 px");
  await page.setViewport({ width: 1440, height: 1000 });
  await back();
  // Two checks that do not flag it: one Checked row, no recommendation.
  await h.select(row("innodb_dedicated_server"));
  assert.deepEqual(await texts(".view-inspector .inspector-block h4"), ["Current value"]);
  assert.match(await h.inspector(), /Checked.*Buffer pool size.*Dedicated server/s);
  assert.match(await h.inspector(), /Changes\s*After a restart/);
  await back();
  // A parameter without a link or a description says so in one line.
  await h.select(row("unknown_parameter"));
  assert.equal(await page.$(".view-inspector a[data-doc-link]"), null);
  await back();

  // Snapshot the catalog in both appearances and at phone width. Overflow
  // belongs to the table's own frame, never to the document.
  for (const appearance of ["light", "dark"]) for (const width of [1440, 390]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: appearance }]);
    await page.setViewport({ width, height: 1000 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
    await picture(`parameters-${appearance}-${width}.png`);
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);

  // Two local drafts: controls keep native key handling; no server read or
  // plan is made until Review and apply. Preview lists the exact targets.
  await edit(); const plansBefore = planned.length, readsBefore = variableReads.length;
  await stage("max_connections", "300");
  await page.keyboard.press("Home");
  assert.equal(await page.$eval(input("max_connections"), e => document.activeElement === e), true, "table Home does not steal input keys");
  await stage("slow_query_log", "ON"); await pendingCount(2);
  assert.equal(planned.length, plansBefore); assert.equal(variableReads.length, readsBefore);
  await search(""); await page.select(filter, "pending");
  assert.deepEqual((await page.$$eval(`${view} table[aria-label="Parameters"] tbody tr[data-row-key]`, es => es.map(e => e.dataset.rowKey))).sort(), ["max_connections", "slow_query_log"]);
  await h.button("Preview changes (2)", `${view} `);
  assert.match(await text(`${view} .parameter-preview`), /Current valueNew valueApply typeWhen it applies/);
  assert.match(await text(`${view} .parameter-preview`), /max_connections151300/);
  await review(2);
  const expectedOrder = mysql.parameters.items.filter(p => ["max_connections", "slow_query_log"].includes(p.name)).map(p => p.name);
  assert.deepEqual(planned.slice(plansBefore).map(p => p.name), expectedOrder);
  assert.ok(planned.slice(plansBefore).every(p => p.mode === "persist"), "Keep after restart defaults on");
  assert.match(await text(batch), /SET PERSIST max_connections = 300;/);
  assert.match(await text(batch), /SYSTEM_VARIABLES_ADMIN.*permission confirmed/s);
  assert.equal(await page.$$eval(`${batch} .variable-policy`, es => es.length), 1);
  for (const appearance of ["light", "dark"]) for (const width of [1440, 390]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: appearance }]);
    await page.setViewport({ width, height: 1000 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
    assert.ok(await page.$eval(batch, e => e.scrollWidth <= e.clientWidth), "review stays within the phone width");
    await picture(`parameters-review-${appearance}-${width}.png`, batch);
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await page.click(confirm);
  await page.waitForSelector(`${view} .parameter-results`);
  await page.waitForFunction(() => !document.querySelector("dialog.parameter-batch-sheet"));
  assert.deepEqual(applied.map(p => p.name), expectedOrder);
  assert.ok(applied.every(p => p.confirmed && p.expect.statement.startsWith("SET PERSIST")));
  assert.equal(await page.$(`${view} [data-action="edit-parameters"]`) !== null, true, "all changed drafts leave Edit mode");
  await h.idle(view);

  // Three writes, partial success: send one at a time and stop at refusal.
  await page.select(filter, "all"); await edit();
  await stage("max_connections", "301"); await stage("slow_query_log", "ON"); await stage("long_query_time", "2");
  await search(""); await review(3);
  assert.ok(planningPeak <= 3);
  const batchOrder = mysql.parameters.items.filter(p => ["max_connections", "slow_query_log", "long_query_time"].includes(p.name)).map(p => p.name);
  const sentBefore = applied.length;
  applyReply = body => ({ status: 200, body: { ...variableFixture.change, name: body.name, mode: body.mode,
    status: applied.length === sentBefore + 2 ? "denied" : "changed", message: applied.length === sentBefore + 2 ? "Permission changed before sending." : "The new value was read back." } });
  await page.click(confirm); await page.waitForSelector(`${view} [data-result="${batchOrder[2]}"][data-status=not_sent]`);
  assert.deepEqual(applied.slice(sentBefore).map(p => p.name), batchOrder.slice(0, 2));
  assert.match(await text(`${view} .parameter-results`), /Not changed\. Permission changed before sending/);
  await pendingCount(2); await h.idle(view);
  assert.equal(await page.$(`${view} [data-action="edit-parameters"]`), null, "unsuccessful rows remain editable");
  applyReply = null;

  // Cancel and Escape record each row against the current reviewed plan.
  for (const key of ["Escape", "Cancel"]) {
    const before = declined.length, writes = applied.length;
    await review(2);
    await page.click(`${batch} .parameter-persist input`);
    await page.waitForFunction(s => !document.querySelector(s).disabled, {}, confirm);
    assert.match(await text(batch), /SET GLOBAL/);
    await closeReview(key);
    await waitFor(() => declined.length === before + 2, "both declined attempts arrive");
    assert.equal(applied.length, writes);
    assert.ok(declined.slice(before).every(p => p.declined && p.mode === "global" && p.expect && p.reviewed_statement.startsWith("SET GLOBAL")));
  }
  await h.button("Cancel", `${view} .toolbar-controls `);

  // Server refusals stay visible with Remove, and cannot be confirmed.
  await edit(); await stage("max_connections", "302");
  planReply = body => ({ status: 200, body: { ...planFor(body), status: "refused", problems: [{ code: "value_changed", message: "The parameter changed on the server." }] } });
  await h.button("Review and apply (1)", `${view} `); await page.waitForSelector(`${batch} .variable-problems`);
  assert.equal(await page.$eval(confirm, e => e.disabled), true);
  await page.click(`${batch} button[aria-label="Remove max_connections"]`);
  assert.equal(await page.$eval(confirm, e => e.disabled), true);
  await page.keyboard.press("Escape"); planReply = null;
  await h.button("Cancel", `${view} .toolbar-controls `);

  // A scope toggle immediately invalidates the previous plan, even if the
  // newer planning response has not arrived when Escape is pressed.
  await edit(); await stage("max_connections", "303"); await review(1);
  planReply = body => ({ status: 200, body: planFor(body), delay: 300 });
  const obsoleteDeclines = declined.length;
  await page.click(`${batch} .parameter-persist input`);
  assert.equal(await page.$eval(confirm, e => e.disabled), true);
  await closeReview();
  assert.equal(declined[obsoleteDeclines].mode, "global");
  assert.equal(declined[obsoleteDeclines].expect, null);
  assert.equal(declined[obsoleteDeclines].reviewed_statement, null);
  await waitFor(() => planningActive === 0, "obsolete plan request settles");
  planReply = null; await h.button("Cancel", `${view} .toolbar-controls `);

  // The cap is enforced before planning. Preview can remove a draft to
  // reach the cap, and a 25-row review still has only three reads in flight.
  await edit();
  for (let i = 0; i < 26; i++) await stage(`catalog_parameter_${String(i).padStart(2, "0")}`, String(1000 + i));
  await pendingCount(26);
  const cappedPlans = planned.length;
  assert.equal(await page.$eval(`${view} .toolbar-controls button.primary`, e => e.disabled), true);
  assert.match(await text(`${view} .variable-problems`), /at most 25 changes/);
  await h.button("Preview changes (26)", `${view} `);
  await page.click(`${view} button[aria-label="Remove catalog_parameter_25 from preview"]`);
  await pendingCount(25);
  assert.equal(planned.length, cappedPlans);
  await search("");
  for (const appearance of ["light", "dark"]) for (const width of [1440, 390]) {
    await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: appearance }]);
    await page.setViewport({ width, height: 1000 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth));
    await picture(`parameters-edit-${appearance}-${width}.png`);
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await review(25); assert.ok(planningPeak <= 3);
  const cappedDeclines = declined.length;
  await closeReview(); await waitFor(() => declined.length === cappedDeclines + 25, "all 25 reviewed attempts are declined");
  await h.button("Cancel", `${view} .toolbar-controls `);

  // Static MySQL rows always plan PERSIST_ONLY, regardless of the checkbox.
  await edit(); await stage("innodb_log_file_size", "67108864"); await review(1);
  assert.equal(planned.at(-1).mode, "persist_only");
  assert.match(await text(batch), /SET PERSIST_ONLY innodb_log_file_size = 67108864;.*Applies after|Applies after.*SET PERSIST_ONLY/s);
  await closeReview(); await h.button("Cancel", `${view} .toolbar-controls `);

  // The inspector's edit action returns to the table and focuses its control.
  await search(""); await h.select(row("max_connections"));
  await h.button("Edit value", ".view-inspector ");
  await page.waitForFunction(id => document.activeElement?.id === id, {}, "parameter-value-max_connections");
  await h.button("Cancel", `${view} .toolbar-controls `); await search("");
  await h.select(row("max_connections")); await h.button("Reset to default", ".view-inspector ");
  await review(1); assert.equal(planned.at(-1).value, "DEFAULT"); assert.match(await text(batch), /max_connections = DEFAULT/);
  await closeReview(); await h.button("Cancel", `${view} .toolbar-controls `);

  // Single-variable modes and Session handoff remain available.
  await search(""); await h.select(row("max_connections"));
  await page.click('.view-inspector [data-change="max_connections"]'); await page.waitForSelector(`${single} .variable-statement`);
  assert.deepEqual(await texts(`${single} .variable-modes label strong`), ["Now only", "Now and after restart", "After restart only", "Session", "Remove the persisted value"]);
  await page.click(`${single} input[value=session]`);
  await page.waitForFunction(s => document.querySelector(`${s} .variable-statement`)?.textContent.startsWith("SET SESSION"), {}, single);
  await page.waitForFunction(s => !document.querySelector(s).disabled, {}, singleConfirm);
  const writesBeforeSession = applied.length, declinesBeforeSession = declined.length;
  await page.click(singleConfirm);
  await page.waitForSelector('.query-lab [aria-label="SQL editor"]', { visible: true });
  assert.equal(applied.length, writesBeforeSession); assert.equal(declined.length, declinesBeforeSession);
  assert.equal(state.other.some(r => r.path === "/api/editor/run"), false);
  await h.server(); await h.view("config"); await back();

  // At least 600 rows use DataTable's existing row window, with no metadata fanout.
  const large = structuredClone(mysql);
  for (let i = 0; i < 650; i++) large.parameters.items.push({ ...mysql.parameters.items[0], name: `large_parameter_${i}`, checks: [] });
  await refresh(large); await search("");
  assert.equal(await page.$eval(`${view} table[aria-label="Parameters"]`, e => Number(e.getAttribute("aria-rowcount"))), large.parameters.items.length + 1);
  assert.ok(await page.$$eval(`${view} tr[data-row-key]`, es => es.length) < 100);
  await refresh(mysql);

  // Full analysis links name check keys, but select the check's first parameter.
  await h.fullAnalysis(); await h.button("Run full analysis", '.server-view[data-view="tuning"] ');
  await page.waitForSelector('.server-view[data-view="tuning"] .summary-line');
  await h.select('.server-view[data-view="tuning"] li[data-finding="config.buffer_pool"]');
  const linkButton = await page.$('.view-inspector .inspector-links button');
  assert.ok(linkButton, "the finding offers its destination view"); await linkButton.click();
  await page.waitForFunction(() => document.querySelector('.server-view[data-view="config"] .view-inspector h3')?.textContent === "innodb_buffer_pool_size");
  await back();

  // An older backend remains readable; lack of metadata does not invent ranges.
  await refresh(legacy);
  assert.equal(await page.$$eval(`${view} tr[data-row-key]`, es => es.length), new Set(legacy.checks.flatMap(c => c.settings.map(s => s.name))).size);
  assert.match(await text(row("max_connections")), /151/);
  // A value the server sets has no control and says why; a parameter with a
  // session value says connected sessions keep theirs.
  const computed = structuredClone(mysql);
  computed.parameters.items.push({ ...mysql.parameters.items[0], name: "version", value: "8.4.11", scope: "global",
    apply_type: "static", modifiable: "none", type: "string", values: null, min: null, max: null, checks: [] });
  computed.parameters.items = computed.parameters.items.map(p => p.name === "sort_buffer_size" ? { ...p, scope: "both" } : p);
  await refresh(computed);
  await edit(); await search("version");
  await page.waitForSelector(row("version"));
  assert.equal(await page.$(input("version")), null, "server-set values are not editable");
  assert.match(await page.$eval(`${row("version")} td[data-col=value] span`, e => e.title), /Set by the server/);
  await search("sort_buffer_size");
  await stage("sort_buffer_size", "524288");
  await page.evaluate(() => window.scrollTo(0, 0));
  await h.button("Preview changes (1)", `${view} `);
  assert.match(await text(`${view} .parameter-preview`), /Applies now to new sessions; existing sessions keep their value/);
  await h.button("Cancel", `${view} .toolbar-controls `); await search("");
  await refresh(mariadb);
  await edit(); await search("performance_schema");
  await page.waitForSelector(row("performance_schema"));
  assert.equal(await page.$(input("performance_schema")), null, "MariaDB static parameters are option-file only");
  assert.match(await page.$eval(`${row("performance_schema")} td[data-col=value] span`, e => e.title), /Option file only/);
  await search("unknown_parameter");
  await page.waitForSelector(row("unknown_parameter"));
  assert.equal(await page.$(input("unknown_parameter")), null);
  assert.match(await page.$eval(`${row("unknown_parameter")} td[data-col=value] span`, e => e.title), /cannot tell/);
  await stage("max_connections", "300"); await review(1);
  assert.equal(planned.at(-1).mode, "global");
  assert.equal(await page.$(`${batch} .parameter-persist`), null);
  assert.match(await text(batch), /MariaDB has no SET PERSIST/);
  assert.equal(await text(`${batch} pre[aria-label="Option file line for max_connections"]`), "max_connections = 300");
  await closeReview(); await h.button("Cancel", `${view} .toolbar-controls `); await search("");

  // Parameter permission hints belong to each exact plan, with the shared label.
  for (const allowed of [false, null, true]) {
    await edit(); await stage("max_connections", "300");
    planReply = body => ({ status: 200, body: { ...planFor(body), privileges: { need: "CONNECTION ADMIN", checked: allowed !== null, allowed, note: "", via: null } } });
    await h.button("Review and apply (1)", `${view} `); await page.waitForSelector(`${batch} .variable-permissions`);
    await assertPrivilege(page, confirm, allowed === false ? "denied" : allowed === null ? "unknown" : "allowed", allowed === false ? "CONNECTION ADMIN" : undefined, { unverifiedBadge: false });
    await closeReview(); await h.button("Cancel", `${view} .toolbar-controls `); planReply = null;
  }

  // Errors keep the last report; no automatic retry loop after a failed read.
  const reads = state.seen.length;
  state.queue.push({ status: 400, body: { error: "Could not read parameters." } });
  await h.refresh(); await page.waitForSelector(`${view} [role=alert]`); await pause(150);
  assert.equal(state.seen.length, reads + 1); assert.ok(await page.$(`${view} .summary-line`));

  // Recorded single-variable coverage starts with an unmodified catalog.
  // The synthetic metadata used by the batch tests above stays unchanged.
  {
    useSingleFixtures = true;
    if (await page.$(`${view} .parameter-results`)) await page.click(`${view} .parameter-results .text-button`);
    await refresh(mysql); await search("");
    const writesAtStart = applied.length, auditsAtStart = (await audit()).length;
    await h.select(row("max_connections"));
    assert.equal(await text('.view-inspector button[data-change="max_connections"]'), "Change…");
    assert.equal(await page.$('.view-inspector .config-actions .privilege-unverified'), null);
    assert.equal(await page.$('.view-inspector button[data-reset="max_connections"]'), null, "nothing persisted, no reset");
    await openChange("max_connections");
    assert.equal(await page.evaluate(() => document.activeElement?.textContent), "Cancel", "Cancel has focus");
    assert.equal(await text(`${single} .eyebrow`), "SET GLOBAL");
    assert.equal(await text(`${single} h2`), "Change max_connections?");
    // The modes this server and variable allow: global only, so no session; nothing persisted, so no reset.
    assert.deepEqual(await texts(`${single} .variable-modes label strong`), ["Now only", "Now and after restart", "After restart only"]);
    assert.equal(await page.$eval(`${single} #variable-value-max_connections`, (e) => e.value), "151");
    await page.waitForFunction(() => /SET GLOBAL max_connections = 151;/.test(document.querySelector("dialog.variable-sheet .variable-statement")?.textContent || ""));
    assert.match(await text(`${single} .variable-facts`), /Server.*MySQL 8\.4\.11.*Global before151.*Global sourcedefault.*mysqld-auto\.cnfno value.*Read at2026-10-15T14:30:00/s);
    await setValue("max_connections", "300");
    assert.equal(await page.$eval(singleConfirm, (e) => e.disabled), true, "off until the server checked the new value");
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
    const facts = await text(`${single} .variable-plan`);
    assert.match(facts, /EffectChanges the running value now, until the server restarts\./);
    assert.match(facts, /PermissionsSYSTEM_VARIABLES_ADMIN or SUPER \(your grants include SYSTEM_VARIABLES_ADMIN\)/);
    assert.match(facts, /UndoSET GLOBAL max_connections = 151;/);
    assert.match(facts, /raises the worst-case memory/);
    assert.match(await text(`${single} .variable-policy`), /Production connection, so myflames asks before every write/);
    assert.equal(await h.label(singleConfirm), "Set globally");
    assert.equal(await page.$eval(singleConfirm, (e) => e.disabled), false);
    assert.match(await page.$eval(singleConfirm, (e) => e.className), /danger-solid/, "SET GLOBAL is confirmed in the danger tone");
    await page.screenshot({ path: path.join(h.shots, "config-change.png") });
    // At 390 px the sheet fits the screen (no sideways scroll inside or outside it).
    await page.setViewport({ width: 390, height: 844 });
    await pause(200);
    const fit = await page.$eval(single, (d) => ({ width: d.getBoundingClientRect().width, scroll: d.scrollWidth - d.clientWidth }));
    assert.ok(fit.width <= 390 && fit.scroll <= 0, `the sheet fits at 390 px: ${JSON.stringify(fit)}`);
    await page.screenshot({ path: path.join(h.shots, "config-change-390.png") });
    await page.setViewport({ width: 1440, height: 1000 });
    await pause(150);
    // Switching mode plans again: the statement follows.
    await page.click(`${single} input[value=persist]`);
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET PERSIST max_connections = 300;");
    assert.equal(await h.label(singleConfirm), "Set and persist");
    assert.equal(await text(`${single} .eyebrow`), "SET PERSIST");
    await page.click(`${single} input[value=global]`);
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
    // Escape sends nothing and is recorded as declined, with the statement shown.
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    assert.equal(applied.length, writesAtStart);
    await page.waitForFunction(() => document.activeElement?.dataset?.change === "max_connections");
    let records = [];
    for (let i = 0; i < 50 && records.length < auditsAtStart + 1; i++) {
      records = await audit();
      if (records.length < auditsAtStart + 1) await pause(100);
    }
    assert.equal(records.length, auditsAtStart + 1, "Escape adds one declined audit");
    assert.equal(records[0].status, "declined");
    assert.equal(records[0].operation, "set_global");
    assert.deepEqual(records[0].statements, ["/*myflames:variable*/ SET GLOBAL max_connections = 300"]);

    // Every cancellation is audited, including an initial read that is still
    // pending or failed, a pending plan, and a refused or failed plan.
    variableReply = (body) => ({ status: 200, body: fx(body.name).variable, delay: 600 });
    await page.click('.view-inspector button[data-change="max_connections"]');
    await page.waitForSelector(single);
    await cancelAndAudit({ requested: "" });
    variableReply = () => ({ status: 400, body: { error: "Variable read failed" } });
    await openChange("max_connections");
    assert.match(await text(`${single} [role=alert]`), /Variable read failed/);
    await cancelAndAudit({ requested: "", key: "Cancel" });
    variableReply = null;

    planReply = (body) => ({ status: 200, body: planFor(body), delay: 600 });
    await openChange("max_connections");
    assert.equal(await statement(), "", "no statement has been reviewed before the plan arrives");
    await cancelAndAudit({ requested: "151" });
    planReply = null;

    await openChange("max_connections");
    await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, singleConfirm);
    planReply = (body) => ({ status: 200, body: planFor(body), delay: 600 });
    await page.click(`${single} input[value=persist]`);
    await setValue("max_connections", "401");
    assert.equal(await statement(), "", "a previous statement disappears as the intent changes");
    await cancelAndAudit({ operation: "set_persist", requested: "401", key: "Cancel" });
    planReply = (body) => body.value === "451"
      ? { status: 400, body: { error: "Plan read failed" } }
      : { status: 200, body: planFor(body) };
    await openChange("max_connections");
    await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, singleConfirm);
    await setValue("max_connections", "451");
    await page.waitForFunction(() => /Plan read failed/.test(document.querySelector("dialog.variable-sheet")?.textContent || ""));
    await cancelAndAudit({ requested: "451" });

    planReply = (body) => ({ status: 200, body: {
      ...planFor(body), status: "refused", statement: null, expect: null,
      problems: [{ code: "invalid_value", message: "Value rejected during planning" }],
    } });
    await openChange("max_connections");
    await page.waitForFunction(() => /Value rejected during planning/.test(document.querySelector("dialog.variable-sheet")?.textContent || ""));
    await cancelAndAudit({ requested: "151", key: "Cancel" });
    planReply = null;
    assert.equal(applied.length, writesAtStart, "all cancellation paths send no write");

    // The initial read is 151, but the reviewed plan sees 205 with another
    // source and read time. The facts must be the same snapshot as expect.
    planReply = (body) => {
      const p = planFor(body);
      p.before.global = "205";
      p.expect.global_value = "205";
      Object.assign(p.variable, { global_value: "205", set_by: "SET GLOBAL", source: "DYNAMIC", read_at: "2026-10-15T14:31:00+00:00" });
      return { status: 200, body: p };
    };
    await openChange("max_connections");
    await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, singleConfirm);
    const reviewedFacts = await text(`${single} .variable-facts`);
    assert.match(reviewedFacts, /Global before205.*Global sourceSET GLOBAL \(DYNAMIC\).*Read at2026-10-15T14:31:00/s);
    assert.doesNotMatch(reviewedFacts, /151|default|COMPILED/);
    await page.screenshot({ path: path.join(h.shots, "config-change-reviewed.png") });
    // A fresh plan may report a present SQL NULL. That is different from an
    // absent entry and from a persisted entry that could not be read.
    planReply = (body) => {
      const p = planFor(body);
      p.before.persisted_state = { state: "present", present: true, value: null };
      p.variable.persisted = { ...p.variable.persisted, present: true, value: null };
      p.variable.read_at = "2026-10-15T14:32:00+00:00";
      return { status: 200, body: p };
    };
    await setValue("max_connections", "152");
    await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, singleConfirm);
    assert.match(await text(`${single} .variable-facts`), /mysqld-auto\.cnfSQL NULL.*Read at2026-10-15T14:32:00/s);
    planReply = (body) => {
      const p = planFor(body);
      p.before.global = "205";
      p.expect.global_value = "205";
      p.variable.global_value = "205";
      return { status: 200, body: p };
    };
    await setValue("max_connections", "151");
    await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, singleConfirm);
    const reviewedCancel = await cancelAndAudit({ requested: "151", statements: ["/*myflames:variable*/ SET GLOBAL max_connections = 151"] });
    assert.equal(reviewedCancel.target.global_value, "205", "audit uses the newly reviewed snapshot");
    planReply = null;

    // Confirm: one apply with the expect identity; the answer shows before and after; the row is read again.
    const reads = state.seen.length;
    await openChange("max_connections");
    await setValue("max_connections", "300");
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
    await page.waitForFunction((b) => !document.querySelector(b)?.disabled, {}, singleConfirm);
    await page.click(singleConfirm);
    await page.waitForSelector(`${view} .variable-result[data-status=changed]`);
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    assert.equal(applied.length, writesAtStart + 1);
    assert.equal(applied[writesAtStart].confirmed, true);
    assert.equal(applied[writesAtStart].mode, "global");
    assert.equal(applied[writesAtStart].value, "300");
    assert.equal(applied[writesAtStart].expect.statement, "SET GLOBAL max_connections = 300");
    assert.equal(applied[writesAtStart].expect.global_value, "151");
    const result = await text(`${view} .variable-result`);
    assert.match(result, /Changed\. max_connections, now only\./);
    assert.match(result, /Global151 → 300/);
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains("variable-result")), true, "the answer is focused");
    await (await page.$(".server-place")).screenshot({ path: path.join(h.shots, "config-changed.png") });
    for (let i = 0; i < 50 && state.seen.length === reads; i++) await pause(50);
    assert.equal(state.seen.length, reads + 1, "the configuration is read again after the change");
    await page.click(`${view} .variable-result .text-button`);
    await page.waitForFunction(() => !document.querySelector(".variable-result"));

    // A read-only MySQL variable: Change at next restart, SET PERSIST_ONLY only.
    await h.select(row("innodb_log_file_size"));
    assert.equal(await text('.view-inspector button[data-change="innodb_log_file_size"]'), "Change…");
    await openChange("innodb_log_file_size");
    assert.deepEqual(await texts(`${single} .variable-modes label strong`), ["After restart only"]);
    assert.equal(await text(`${single} .eyebrow`), "SET PERSIST_ONLY");
    await setValue("innodb_log_file_size", "64M");
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET PERSIST_ONLY innodb_log_file_size = 67108864;");
    assert.match(await text(`${single} .variable-field .hint`), /Sends 67,108,864 \(64\.0 MiB\)\./);
    const restart = await text(`${single} .variable-plan`);
    assert.match(restart, /The running value does not change; the new value applies at the next restart\./);
    assert.match(restart, /SYSTEM_VARIABLES_ADMIN and PERSIST_RO_VARIABLES_ADMIN/);
    assert.match(restart, /does not check the value of SET PERSIST_ONLY/);
    assert.equal(await h.label(singleConfirm), "Persist for next restart");
    await cancelAndAudit({ name: "innodb_log_file_size", operation: "set_persist_only", requested: "64M", statements: ["/*myflames:variable*/ SET PERSIST_ONLY innodb_log_file_size = 67108864"], key: "Cancel" });
    assert.equal(applied.length, writesAtStart + 1, "Cancel sends nothing");

    // ---- Change: MariaDB 11.4 has no PERSIST; the option file line is offered instead ----
    await refresh(mariadb);
    await h.select(row("max_connections"));
    await openChange("max_connections");
    assert.deepEqual(await texts(`${single} .variable-modes label strong`), ["Now only"], "no PERSIST modes on MariaDB");
    assert.match(await text(single), /MariaDB has no SET PERSIST/);
    await setValue("max_connections", "300");
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET GLOBAL max_connections = 300;");
    assert.equal(await text(`${single} .variable-plan .perf-sql pre`), "max_connections = 300", "the option file line to copy");
    assert.match(await text(`${single} .variable-plan`), /CONNECTION ADMIN/);
    await page.screenshot({ path: path.join(h.shots, "config-change-mariadb.png") });
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    // A read-only MariaDB variable: no runtime change, the option file line and a restart.
    await h.select(row("performance_schema"));
    assert.equal(await text('.view-inspector button[data-change="performance_schema"]'), "Change…");
    await openChange("performance_schema");
    assert.equal(await page.$(`${single} .variable-modes`), null, "no mode can change it now");
    assert.match(await text(`${single} .variable-note`), /read-only while the server runs\. It changes only at a restart\./);
    assert.equal(await page.$eval(`${single} #variable-value-performance_schema`, e => e.getAttribute("role")), "switch");
    await page.click(`${single} #variable-value-performance_schema`);
    assert.equal(await page.$eval(`${single} #variable-value-performance_schema`, e => e.getAttribute("aria-checked")), "true");
    assert.equal(applied.length, writesAtStart + 1, "a boolean toggle only changes a draft");
    await page.waitForFunction(() => /restart the server/.test(document.querySelector("dialog.variable-sheet .variable-alternative")?.textContent || ""));
    assert.equal(await text(`${single} h2`), "Change performance_schema at the next restart?");
    assert.equal(await text(`${single} .eyebrow`), "OPTION FILE");
    assert.equal(await page.$(`${single} .variable-problems`), null, "the reason is said once, in the note");
    assert.equal(await page.$(`${single} .variable-policy`), null, "nothing to send, so no policy line");
    assert.equal(await text(`${single} .variable-alternative .perf-sql pre`), "performance_schema = ON");
    assert.equal(await page.$eval(singleConfirm, (e) => e.disabled), true);
    assert.equal(await h.label(singleConfirm), "No change at runtime");
    await page.screenshot({ path: path.join(h.shots, "config-change-readonly.png") });
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    assert.equal(applied.length, writesAtStart + 1);

    // Session offers an editor handoff, using the planning connection value
    // only as a snapshot. It must never execute through variable/apply.
    const nullableReport = structuredClone(mysql);
    nullableReport.parameters.items.push({ ...mysql.parameters.items.find(p => p.name === "max_connections"),
      name: "character_set_results", type: "string", value: "latin1", default: "utf8mb4", scope: "both", checks: [] });
    await refresh(nullableReport);
    await h.select(row("character_set_results"));
    await openChange("character_set_results");
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement"));
    assert.deepEqual(await texts(`${single} .variable-modes label strong`), ["Session"], "a session-only change offers no server-wide modes");
    assert.match(await text(`${single} .variable-facts`), /Planning session valuelatin1.*Read at2026-10-15T14:30:00/s);
    assert.doesNotMatch(await text(`${single} .variable-facts`), /Global before|Global source|utf8mb4/);
    const nullChoice = `${single} .variable-field input[type=checkbox]`;
    assert.equal(await page.$eval(nullChoice, (e) => e.checked), false);
    await setValue("character_set_results", "NULL");
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET SESSION character_set_results = 'NULL';");
    assert.equal(planned.at(-1).value, "NULL", "typing NULL sends text");
    await page.click(nullChoice);
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET SESSION character_set_results = NULL;");
    assert.equal(planned.at(-1).value, null, "the explicit choice sends JSON null");
    assert.equal(await page.$eval(`${single} #variable-value-character_set_results`, (e) => e.disabled), true);
    await page.setViewport({ width: 390, height: 844 });
    const nullableFit = await page.$eval(single, (d) => ({ width: d.getBoundingClientRect().width, scroll: d.scrollWidth - d.clientWidth }));
    assert.ok(nullableFit.width <= 390 && nullableFit.scroll <= 0, "SQL NULL controls fit at 390 px");
    await page.screenshot({ path: path.join(h.shots, "config-change-null-390.png") });
    await page.setViewport({ width: 1440, height: 1000 });
    assert.equal(await h.label(singleConfirm), "Open in Query editor");
    assert.match(await text(single), /current Query editor tab without running/);
    assert.doesNotMatch(await text(`${single} .variable-plan`), /Changes this myflames session/);
    const writesBeforeHandoff = applied.length, auditBeforeHandoff = (await audit()).length;
    // The shell wires the handoff: the statement lands in the current Query
    // editor tab, selected, and nothing runs.
    assert.equal(await page.$eval(singleConfirm, e => e.disabled), false);
    await page.click(singleConfirm);
    await page.waitForSelector(".query-lab:not([hidden]) .editor-toolbar, .query-lab .editor-toolbar", { visible: true });
    await page.waitForFunction(() => {
      const e = document.querySelector('[aria-label="SQL editor"]');
      return !!e && e.value.endsWith("SET SESSION character_set_results = NULL;") &&
        e.value.slice(e.selectionStart, e.selectionEnd) === "SET SESSION character_set_results = NULL";
    });
    assert.equal(state.other.some(r => r.path === "/api/editor/run"), false);
    await h.server(); await h.view("config");
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    assert.equal(applied.length, writesBeforeHandoff, "Session never sends variable/apply");
    assert.equal((await audit()).length, auditBeforeHandoff, "opening or cancelling an SQL draft is not a write attempt");

    // A sent write whose persisted value cannot be read is not "nothing sent".
    await refresh(mysql);
    await h.select(row("max_connections"));
    await openChange("max_connections");
    await setValue("max_connections", "300");
    await page.click(`${single} input[value=persist]`);
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement")?.textContent === "SET PERSIST max_connections = 300;");
    applyReply = () => ({ status: 200, body: {
      ...variables["mysql:max_connections"].change, mode: "persist", status: "applied_unverified",
      message: "The statement succeeded, but the persisted value could not be read.",
      before: { global: "151", session: null, persisted: null, persisted_state: { state: "absent", present: false, value: null } },
      after: { global: "300", session: null, persisted: null, persisted_state: { state: "unreadable", present: null, value: null } },
    } });
    await page.click(singleConfirm);
    await page.waitForSelector(`${view} .variable-result[data-status=applied_unverified]`);
    const unverified = await text(`${view} .variable-result`);
    assert.match(unverified, /Applied; verification incomplete\./);
    assert.match(unverified, /mysqld-auto\.cnfno value → not readable/);
    assert.doesNotMatch(unverified, /Nothing was sent|SQL NULL/);
    await page.screenshot({ path: path.join(h.shots, "config-change-unverified.png") });
    await h.idle(view);
    await page.click(`${view} .variable-result .text-button`);
    applyReply = null;

    // RESET PERSIST opens from its own inspector trigger, with no value field.
    // Model a present entry on the same recorded variable, without changing
    // the base fixtures or the batch test metadata.
    const persistedReport = structuredClone(mysql);
    persistedReport.parameters.items.find(p => p.name === "max_connections").persisted = true;
    variableReply = body => {
      const v = variableFor(body.name);
      v.persisted = { ...v.persisted, present: true, value: "275" };
      v.modes.reset_persist.available = true;
      return { status: 200, body: v };
    };
    planReply = body => {
      const p = planFor(body);
      p.variable = variableReply(body).body;
      p.before.persisted = "275";
      p.before.persisted_state = { state: "present", present: true, value: "275" };
      p.expect.persisted_value = "275";
      p.undo = ["SET PERSIST_ONLY max_connections = 275;"];
      p.effect = { now: false, after_restart: true, needs_restart: false,
        text: "Removes max_connections from mysqld-auto.cnf. The running value stays; after a restart the server uses the option files or its default." };
      return { status: 200, body: p };
    };
    await refresh(persistedReport); await h.select(row("max_connections"));
    async function openReset() {
      await page.click('.view-inspector [data-reset="max_connections"]');
      await page.waitForSelector(`${single} .variable-statement`);
      await page.waitForFunction(s => !document.querySelector(s).disabled, {}, singleConfirm);
    }
    assert.equal(await text('.view-inspector [data-reset="max_connections"]'), "Reset persisted value…");
    await openReset();
    assert.equal(await text(`${single} h2`), "Remove max_connections from mysqld-auto.cnf?");
    assert.equal(await text(`${single} .eyebrow`), "RESET PERSIST");
    assert.equal(await page.$(`${single} .variable-field`), null);
    assert.equal(await statement(), "RESET PERSIST max_connections;");
    assert.equal(planned.at(-1).value, null);
    assert.match(await text(`${single} .variable-plan`), /The running value stays.*option files or its default/s);
    assert.equal(await h.label(singleConfirm), "Remove persisted value");
    await cancelAndAudit({ operation: "reset_persist", requested: null,
      statements: ["/*myflames:variable*/ RESET PERSIST max_connections"], key: "Cancel" });
    await openReset();
    const resetPlan = structuredClone(planReply(planned.at(-1)).body);
    const writesBeforeReset = applied.length;
    applyReply = () => ({ status: 200, body: { ...variableFixture.change, mode: "reset_persist",
      statement: "RESET PERSIST max_connections", message: "Removed max_connections from mysqld-auto.cnf. The running value stays.",
      before: resetPlan.before, after: { global: "151", session: null, persisted: null,
        persisted_state: { state: "absent", present: false, value: null } } } });
    await page.click(singleConfirm);
    await page.waitForSelector(`${view} .variable-result[data-status=changed]`);
    assert.equal(applied.length, writesBeforeReset + 1);
    assert.deepEqual(applied.at(-1).expect, resetPlan.expect);
    assert.equal(applied.at(-1).confirmed, true);
    assert.equal(applied.at(-1).mode, "reset_persist");
    assert.equal(applied.at(-1).value, null);
    assert.match(await text(`${view} .variable-result`), /mysqld-auto\.cnf275 → no value/);
    assert.doesNotMatch(await text(`${view} .variable-result`), /Global151 →/);
    await h.idle(view); await page.click(`${view} .variable-result .text-button`);
    variableReply = null; planReply = null; applyReply = null;
    await refresh(mysql);

    // A denied mode cannot lock the Change entry: another mode may work.
    await h.setPrivileges({ set_global: privilegeAnswer("denied", "SYSTEM_VARIABLES_ADMIN"),
      set_persist: privilegeAnswer("denied", "SYSTEM_VARIABLES_ADMIN"),
      set_session_restricted: privilegeAnswer("denied", "SESSION_VARIABLES_ADMIN"),
      config: privilegeAnswer("denied", "PROCESS", { grey_out: false }) });
    assert.equal(await page.$eval(`${view} [data-action=refresh]`, e => e.disabled), false);
    await h.select(row("max_connections"));
    assert.equal(await page.$eval('.view-inspector button[data-change="max_connections"]', e => e.disabled), false, "review stays available when a generic mode is denied");
    for (const [mode, wanted] of [["global", "denied"], ["persist", "allowed"], ["persist_only", "unknown"], ["reset_persist", "denied"]]) {
      planReply = body => {
        const p = planFor(body);
        p.privileges = { need: "SYSTEM_VARIABLES_ADMIN", note: "", checked: wanted !== "unknown",
          allowed: wanted === "allowed" ? true : wanted === "denied" ? false : null, via: null };
        p.variable.modes.reset_persist.available = true;
        return { status: 200, body: p };
      };
      await openChange("max_connections");
      await page.waitForSelector(`${single} .variable-permissions`);
      if (mode !== "global") {
        await page.click(`${single} input[value=${mode}]`);
        await page.waitForFunction((s, m) => document.querySelector(`${s} .variable-modes input:checked`)?.value === m, {}, single, mode);
        await page.waitForFunction((s, prefix) => document.querySelector(`${s} .variable-statement`)?.textContent.startsWith(prefix), {}, single, mode === "reset_persist" ? "RESET PERSIST" : VERB[mode]);
      }
      await assertPrivilege(page, singleConfirm, wanted, wanted === "denied" ? "SYSTEM_VARIABLES_ADMIN" : undefined, { unverifiedBadge: false });
      assert.equal(await page.$(`${single} button .privilege-unverified`), null, "permission status stays in the dialog body");
      assert.match(await text(`${single} .variable-permissions`), /SYSTEM_VARIABLES_ADMIN/);
      if (wanted === "unknown") assert.match(await text(`${single} .variable-permissions`), /permission could not be verified/);
      if (wanted === "unknown") {
        for (const appearance of ["light", "dark"]) for (const width of [1440, 390]) {
          await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: appearance }]);
          await page.setViewport({ width, height: 1000 });
          await page.screenshot({ path: path.join(h.shots, `config-review-permissions-${appearance}-${width}.png`) });
        }
        await page.setViewport({ width: 1440, height: 1000 });
        await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
      }
      if (wanted === "denied") {
        const before = applied.length;
        await page.click(singleConfirm); await page.keyboard.press("Enter");
        assert.equal(applied.length, before, "a denied mode sends no write");
      }
      await page.keyboard.press("Escape");
      await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    }
    // A refused plan must still explain permissions, without an apply action.
    planReply = body => {
      const p = planFor(body);
      p.status = "refused";
      p.privileges = { need: "SYSTEM_VARIABLES_ADMIN", note: "", checked: true, allowed: false, via: null };
      p.problems = [{ code: "privilege_denied", message: "The connected account lacks the required privilege." }];
      return { status: 200, body: p };
    };
    await openChange("max_connections");
    await page.waitForSelector(`${single} .variable-permissions`);
    assert.match(await text(`${single} .variable-permissions`), /SYSTEM_VARIABLES_ADMIN.*your grants do not seem to include it/);
    assert.equal(await page.$eval(singleConfirm, e => e.disabled), true);
    assert.equal(await page.$(`${single} button .privilege-unverified`), null);
    const writesBeforeRefused = applied.length;
    await page.click(singleConfirm);
    assert.equal(applied.length, writesBeforeRefused);
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    planReply = null;
    // A plan refused by the connection policy cannot send its ready SQL.
    planReply = body => ({ status: 200, body: { ...planFor(body),
      policy: { action: "refuse", tone: "danger", title: "Read-only connection", text: "This connection is read-only." },
      problems: [{ code: "read_only", message: "This connection is read-only." }] } });
    await openChange("max_connections");
    await page.waitForSelector(`${single} .variable-problems`);
    assert.match(await text(`${single} .variable-problems`), /This connection is read-only/);
    assert.equal(await page.$eval(singleConfirm, e => e.disabled), true);
    const writesBeforePolicy = applied.length;
    await page.click(singleConfirm);
    assert.equal(applied.length, writesBeforePolicy);
    await cancelAndAudit({ requested: "151", statements: ["/*myflames:variable*/ SET GLOBAL max_connections = 151"] });
    planReply = null;
    // Drafting SESSION SQL does not use the generic restricted-session gate.
    await refresh(nullableReport); await h.select(row("character_set_results"));
    await openChange("character_set_results");
    await page.waitForFunction(() => document.querySelector("dialog.variable-sheet .variable-statement"));
    assert.equal(await h.label(singleConfirm), "Open in Query editor");
    assert.equal(await page.$eval(singleConfirm, e => e.getAttribute("aria-disabled")), null);
    const writesBeforeCancel = applied.length, auditBeforeCancel = (await audit()).length;
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("dialog.variable-sheet"));
    assert.equal(applied.length, writesBeforeCancel);
    assert.equal((await audit()).length, auditBeforeCancel);
    await refresh(mysql);
    await h.setPrivileges({});

    useSingleFixtures = false;
    await back();
  }

  // Saved read-only connections still auto-load their catalog; Edit explains
  // the connection policy instead of pretending a missing privilege is known.
  await page.evaluate(async () => {
    const token = (await (await fetch("/api/auth/session", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Bootstrap": "1", "X-Myflames-Token": localStorage.getItem("myflames.session-proof") || "" }, body: "{}" })).json()).token;
    for (const value of [
      { name: "Read only", host: "readonly.local", port: 3306, user: "reader", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "production", read_only: true },
      { name: "Staging", host: "staging.local", port: 3306, user: "dev", database: "", ssl_mode: "PREFERRED", ssl_ca: "", type: "production", read_only: false },
    ]) await fetch("/api/profiles/save", { method: "POST", headers: { "Content-Type": "application/json", "X-Myflames-Token": token }, body: JSON.stringify({ value }) });
  });
  await page.reload({ waitUntil: "networkidle0" }); await h.server(); await h.view("config"); await page.waitForSelector(`${view} .summary-line`); await h.idle(view);
  const initialConnection = await connectionOption(page, "Staging");
  await chooseConnection(page, initialConnection);
  await h.refresh();
  await page.waitForFunction(v => !document.querySelector(`${v}[data-other-connection=true]`) && !document.querySelector(`${v} .spinner`), {}, view);
  await h.select(row("max_connections"));
  const readonly = await connectionOption(page, "Read only");
  await chooseConnection(page, readonly);
  await page.waitForSelector(`${view}[data-other-connection=true]`);
  assert.equal(await page.$eval(`${view} [data-action="edit-parameters"]`, e => e.disabled), true);
  assert.equal(await page.$eval('.view-inspector [data-change="max_connections"]', e => e.disabled), true);
  assert.match(await page.$eval('.view-inspector [data-change="max_connections"]', e => e.title), /refresh to load this server/);
  await h.view("health"); await h.view("config");
  await page.waitForFunction(v => !document.querySelector(`${v}[data-other-connection=true]`) && !document.querySelector(`${v} .spinner`), {}, view);
  assert.equal(await page.$eval(`${view} [data-action="edit-parameters"]`, e => e.disabled), true);
  assert.match(await page.$eval(`${view} [data-action="edit-parameters"]`, e => e.title), /read-only/);
  await h.select(row("max_connections"));
  assert.equal(await page.$eval('.view-inspector [data-change="max_connections"]', e => e.disabled), true);
  assert.match(await page.$eval('.view-inspector [data-change="max_connections"]', e => e.title), /read-only/);

  // A connection change while a sheet is open locks the original review.
  // Dispatch the real picker action programmatically to model an external
  // switch while the modal makes the surrounding controls inert.
  await chooseConnection(page, initialConnection); await h.refresh(); await h.idle(view);
  await page.waitForFunction(v => !document.querySelector(`${v}[data-other-connection=true]`), {}, view);
  useSingleFixtures = true;
  await h.select(row("max_connections")); await openChange("max_connections");
  await page.waitForSelector(`${single} .variable-statement`);
  const originalConnection = structuredClone(planned.at(-1).connection), writesBeforeSwitch = applied.length;
  await page.$eval('.window-toolbar .connection-pill', e => e.click());
  await page.waitForSelector('.window-toolbar select[aria-label="Connection"]');
  await page.select('.window-toolbar select[aria-label="Connection"]', readonly);
  await page.waitForFunction(s => /connection in use changed after this sheet opened/.test(document.querySelector(s)?.textContent || ""), {}, single);
  assert.equal(await page.$eval(singleConfirm, e => e.disabled), true);
  await page.click(singleConfirm);
  assert.equal(applied.length, writesBeforeSwitch);
  await cancelAndAudit({ requested: "151", statements: ["/*myflames:variable*/ SET GLOBAL max_connections = 151"], focus: false });
  assert.deepEqual(declined.at(-1).connection, originalConnection, "the audit stays bound to the original connection");
  await page.keyboard.press("Escape");
  await h.idle(view); await waitFor(() => planningActive === 0, "all delayed plans settled");
  useSingleFixtures = false;
  const saved = await h.savedSlow(s => s.kind === "config");
  assert.equal(JSON.stringify(saved.state).includes("config-1.0"), false, "reports are not saved");
  assert.equal(JSON.stringify(saved.state).includes("variable-plan"), false, "plans are not saved");
  assert.deepEqual(h.errors, []);
  console.log(`parameters view: ok; screenshots in ${h.shots}`);
} finally { await h.close(); }
