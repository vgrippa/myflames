// Read-only VillageSQL extensions through the real workspace shell. Reports
// below are explicit synthetic edge cases; Python replay tests own live data.
// perf-harness refuses every unmocked server read and isolates workspace HOME.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import ts from "typescript";
import { load, open } from "./perf-harness.mjs";
import { chooseConnection } from "./chrome-helpers.mjs";

const root = fileURLToPath(new URL("../../../", import.meta.url));
// Exercise the real callbacks without an ExportMenu opener. This catches a
// missing binding guard even when the browser's disabled control hides it.
const componentSource = (await fs.readFile(new URL("../src/Extensions.tsx", import.meta.url), "utf8"))
  .replace(/^import[\s\S]*?;\n/gm, "");
const componentCode = ts.transpileModule(componentSource, { compilerOptions: {
  target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS,
  jsx: ts.JsxEmit.React, jsxFactory: "jsx", jsxFragmentFactory: "Fragment",
} }).outputText;
function componentExports(bound, inspect = false) {
  const downloads = [];
  const context = { exports: {}, Fragment: "Fragment",
    jsx: (type, props, ...children) => ({ type, props: { ...props, children } }),
    useState: value => [value, () => {}], useOpenRead: () => {},
    useConnectionReads: () => ({ key: "current", loading: null, running: () => false }),
    count: String, engineLabel: () => "VillageSQL", csvReport: () => "inventory csv",
    download: (...args) => downloads.push(args),
  };
  for (const name of ["ViewLayout", "ExportMenu", "ViewStatus", "SummaryLine", "FactStrip", "EmptyState", "DataTable", "Disclosure", "Fields"])
    context[name] = name;
  vm.runInNewContext(componentCode, context);
  const view = context.exports.ExtensionsView({ connection: { host: "current" }, disabled: false,
    selection: { key: "" }, onReport: () => {}, report: bound });
  if (inspect === "registration") return vm.runInNewContext("({ names: Names, count: registeredCount })", context);
  if (inspect) return view;
  const controls = view.props.controls.props;
  controls.formats.forEach(format => format.onSelect());
  return { disabled: controls.disabled, downloads };
}
const shots = path.join(root, "local", "vsql-screens");
await fs.mkdir(shots, { recursive: true });
const view = '.server-view[data-view="extensions"]';
const healthView = '.server-view[data-view="health"]';
const table = `${view} table[aria-label="Extensions"]`;
const ok = body => ({ status: 200, body });
const clone = value => structuredClone(value);
const deferred = () => {
  let release;
  const wait = new Promise(resolve => { release = resolve; });
  return { wait, release };
};
const hostile = '<img src=x data-extension-injected onerror="window.extensionInjected=true">';
const hostileName = `vsql_${hostile}`;
const hostileError = `Cannot register ${hostile}\nuser supplied value: <script>window.extensionInjected=true</script>\u0001`;
const protocolNote = "VillageSQL 0.0.8 refuses extensions using protocol 4.";
const emptyDependencies = () => ({ count: 0, sample: [], truncated: false });
const extension = (name, changes = {}) => ({
  name, version: "0.1.0", pending: null, protocol: 5, protocol_state: "current", protocol_note: null,
  sdk_version: "0.0.8", functions: [], functions_truncated: false, types: [], types_truncated: false,
  columns: emptyDependencies(), routines: emptyDependencies(), variables: [], variables_truncated: false,
  ...changes,
});
const populated = {
  schema_version: "extensions-1.0", engine: "mysql", flavor: "villagesql", server_product: "VillageSQL",
  server_base: "mysql", server_version: "8.4.11-villagesql-0.0.8", taken_at: "2026-11-27T12:00:00Z", uptime_s: 7200,
  available: true, reason: null,
  settings: { veb_dir: "/usr/lib/veb/", preview_extensions: false, server_protocol: 6, schema_version: "mysql-8.4_0.0.8",
    codebase: "mysql-8.4_0.0.8", build: { git_sha: "543f42e0601", is_dirty: false } },
  status: { veb_cache_repaired: 0, install_count: 4, uninstall_count: 0 },
  extensions: [
    extension("vsql_complex", {
      functions: ["complex", "complex_add", "complex_abs"], types: ["COMPLEX"],
      columns: { count: 3, sample: [
        { schema: "testdb", table: "signals", column: "amplitude", data_type: "vsql_complex.COMPLEX" },
        { schema: "testdb", table: "signals", column: "phase", data_type: "vsql_complex.COMPLEX" },
      ], truncated: true },
      routines: { count: 2, sample: [
        { schema: "testdb", routine: "normalize_signal", routine_type: "FUNCTION", parameter: "signal", data_type: "vsql_complex.COMPLEX" },
      ], truncated: true },
      variables: [
        { name: "vsql_complex.mode", value: "precise", kind: "system" },
        { name: "vsql_complex.ext_hits", value: "42", kind: "status" },
      ],
      pending: { version: "0.2.0", requested_at: "2026-11-27 11:40:00", last_error: hostileError,
        error_truncated: false, last_error_at: "2026-11-27 11:41:00" },
    }),
    extension("vsql_legacy", { protocol: 3, protocol_state: "deprecated", sdk_version: "0.0.6" }),
    extension("vsql_unsupported", { protocol: 4, protocol_state: "unsupported", protocol_note: protocolNote }),
    extension(hostileName, { protocol: 6, functions: [`fn_${hostile}\u0002`], types: [`type_${hostile}`],
      variables: [{ name: `${hostileName}.mode`, value: `${hostile}\u0003`, kind: "system" }] }),
  ],
  other_variables: [{ name: "unowned.mode", value: "active", kind: "system" }], other_variables_truncated: false,
  summary: { installed: 4, pending: 1, failing: 1, deprecated_protocol: 1, unsupported_protocol: 1 }, notes: [],
};
const empty = { ...clone(populated), extensions: [], other_variables: [],
  summary: { installed: 0, pending: 0, failing: 0, deprecated_protocol: 0, unsupported_protocol: 0 } };
for (const bound of [null, { key: "previous", label: "Previous", report: empty }])
  assert.deepEqual(componentExports(bound), { disabled: true, downloads: [] }, "missing or mismatched reports guard both export callbacks");
const currentExports = componentExports({ key: "current", label: "Current", report: empty });
assert.equal(currentExports.disabled, false);
assert.deepEqual(currentExports.downloads.map(row => row[1]), ["myflames-extensions.csv", "myflames-extensions.txt"]);
const counterText = currentExports.downloads[1][0];
assert.match(counterText, /install and update requests: 4/, "the export names both operations counted by Com_install_extension");
assert.match(counterText, /[Rr]equest counts do not establish successful installations/, "request counters never claim successful installations");
const counterView = JSON.stringify(componentExports({ key: "current", label: "Current", report: empty }, true));
assert.match(counterView, /Install and update requests/, "the Server details label names the shared counter");
assert.match(counterView, /[Rr]equest counts do not establish successful installations/, "the view explains request-counter semantics");
const unknownCounters = { ...clone(empty), status: { veb_cache_repaired: null, install_count: null, uninstall_count: null } };
assert.match(componentExports({ key: "current", label: "Current", report: unknownCounters }).downloads[1][0],
  /install and update requests: Not collected/, "missing counters remain unknown");
const unseen = { ...clone(empty), extensions: [extension("unseen", {
  columns: { count: null, sample: [], truncated: true }, routines: { count: null, sample: [], truncated: true },
})], summary: { ...empty.summary, installed: 1 } };
const unseenText = componentExports({ key: "current", label: "Current", report: unseen }).downloads[1][0];
assert.match(unseenText, /Showing 0 columns; more may exist, the total is unknown\./);
assert.match(unseenText, /Showing 0 routines; more may exist, the total is unknown\./);
assert.doesNotMatch(unseenText, /more exist/);
const registration = componentExports({ key: "current", label: "Current", report: empty }, "registration");
for (const names of [null, []]) {
  const budgetReport = { ...clone(empty), extensions: [extension("budget_exhausted", {
    functions: names, functions_truncated: true, types: names, types_truncated: true,
  })] };
  const budgetText = componentExports({ key: "current", label: "Current", report: budgetReport }).downloads[1][0];
  assert.match(budgetText, /functions: Not collected/);
  assert.match(budgetText, /types: Not collected/);
  assert.doesNotMatch(budgetText, /None registered/, "a zero-name truncated prefix cannot establish an empty registration");
  assert.equal(registration.count(names, true), "Not collected", "bounded counts remain unknown");
  const rendered = JSON.stringify(registration.names({ names, truncated: true }));
  assert.match(rendered, /Not collected/);
  assert.doesNotMatch(rendered, /None registered/);
}
assert.equal(registration.count([], false), "0", "a complete empty list is a known zero");
assert.match(JSON.stringify(registration.names({ names: [], truncated: false })), /None registered/);
if (process.argv.includes("--unit")) {
  console.log("PASS Extensions export guards, dependency bounds and install/update request-counter semantics");
  process.exit(0);
}
const healthBase = await load("health-mysql-8.4.json");
const recordedReports = await Promise.all(["8.4", "percona-8.4", "9.7"].map(async codebase => {
  const recording = JSON.parse(await fs.readFile(path.join(root, "test", "fixtures", "perf", `extensions-villagesql-${codebase}.json`), "utf8"));
  return { codebase, report: recording.report };
}));
let healthReport = { ...clone(healthBase), flavor: undefined, server_product: undefined };
const initialHealth = deferred();
let firstHealth = initialHealth;
const healthReads = [];
const diagnostics = { console: [], failed: [], http: [] };
const seed = `import json, sys
from myflames.workspace_store import WorkspaceStore
store = WorkspaceStore(sys.argv[1])
store.set_ui_preferences({"health_auto_production": 0, "health_auto_development": 0})
profiles = {key: store.save("profile", dict(name=name, host=host, port=3306, user="reader", database="", ssl_mode="PREFERRED", type="production"), remember=False) for key, name, host in [("checked", "Checked server", "checked.example"), ("other", "Other MySQL", "mysql.example")]}
print(json.dumps(profiles))`;
const readiness = { engine: "mysql", flavor: "mysql", product: "MySQL", version: "8.4.11",
  performance_schema: true, read_only: false, super_read_only: false, replica: false, group_member: false, galera_member: false,
  checked_at: "2026-11-27T12:00:00Z", privileges: {}, actions: {}, notes: [] };
let readinessReport = clone(readiness);
const readinessReads = [];
const h = await open("/api/live/extensions", "extensions", {
  "/api/live/health": (body, request) => {
    healthReads.push(body);
    const report = clone(healthReport);
    if (firstHealth) {
      const pending = firstHealth; firstHealth = null;
      void pending.wait.then(() => request.respond({ status: 200, contentType: "application/json", body: JSON.stringify(report) }));
      return null;
    }
    return ok(report);
  },
  "/api/live/readiness": body => {
    readinessReads.push(body);
    return ok(clone(readinessReport));
  },
}, {
  list: "docked", seed,
  beforeGoto: page => {
    page.on("console", message => {
      if (["error", "warning"].includes(message.type())) diagnostics.console.push({ type: message.type(), text: message.text() });
    });
    page.on("requestfailed", request => diagnostics.failed.push({ path: new URL(request.url()).pathname, error: request.failure()?.errorText }));
    page.on("response", response => {
      if (response.status() >= 400) diagnostics.http.push({ path: new URL(response.url()).pathname, status: response.status() });
    });
  },
});
const { page, state } = h;
page.setDefaultTimeout(10000);
state.fallback = ok(empty);
// Register the release before opening Health; the first mock consumes its gate.
const releaseFirstHealth = initialHealth.release;
let serial = 0;
const reportText = () => h.text(view);
const row = name => `${table} tbody tr[data-row-key=${JSON.stringify(name)}]`;
const waitText = (selector, expected) => page.waitForFunction((s, text) => document.querySelector(s)?.textContent.includes(text),
  { timeout: 10000 }, selector, expected);
const waitRead = async (before, expected, selector = view) => {
  await waitText(selector, expected);
  await page.waitForFunction(s => document.querySelector(`${s} [data-action="refresh"]`)?.getAttribute("aria-busy") !== "true", {}, selector);
  assert.equal(state.seen.length, before + 1, "one Extensions read per explicit refresh");
};
const back = async () => {
  for (const button of await page.$$(`${view} .view-inspector .detail-back`)) {
    if (await button.evaluate(element => element.getClientRects().length > 0)) {
      await button.click();
      await page.waitForFunction(s => ![...document.querySelectorAll(`${s} .view-inspector.is-detail`)].some(el => el.getClientRects().length > 0), {}, view);
      return;
    }
  }
  if (await page.$(`${view} .view-inspector`)) await page.keyboard.press("Escape");
};
const refresh = async (report, expected) => {
  await back();
  const before = state.seen.length;
  state.fallback = ok({ ...clone(report), taken_at: `2026-11-27T12:${String(++serial).padStart(2, "0")}:00Z` });
  await h.refresh();
  await waitRead(before, expected);
};
const layout = async (name, inspector = false) => {
  for (const theme of ["light", "dark"]) for (const width of [1440, 720]) {
    await page.setViewport({ width, height: 900 });
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() => [...document.querySelectorAll(".server-place:not(.is-tuning-place)")]
      .filter(place => place.offsetParent !== null)
      .every(place => place.querySelector(".server-layout")?.classList.contains("is-compact") === (place.clientWidth < 720)));
    if (inspector && !(await page.$eval(`${view} .view-inspector`, element => element.getClientRects().length > 0).catch(() => false)))
      await h.select(row("vsql_complex"));
    await page.waitForFunction(s => {
      const target = document.querySelector(s);
      if (!target || !target.getClientRects().length) return false;
      return document.documentElement.scrollWidth <= document.documentElement.clientWidth;
    }, { timeout: 10000 }, inspector ? `${view} .view-inspector` : table);
    // Two settled frames after ResizeObserver and fonts prevent a stale layout
    // from passing because its old boxes happened to fit the previous width.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const metrics = await page.evaluate(({ view, table, inspector }) => {
      const target = document.querySelector(inspector ? `${view} .view-inspector` : table);
      const frame = inspector ? target : target.closest(".data-table-frame");
      const rect = frame?.getBoundingClientRect();
      return { overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        left: rect?.left, right: rect?.right, width: innerWidth,
        tableHeader: !inspector && getComputedStyle(target.querySelector("th")).position,
        wrappers: !!document.querySelector(".workspace") && !!document.querySelector(".topbar-stack") };
    }, { view, table, inspector });
    assert.ok(metrics.overflow <= 0, `${name} ${width} ${theme}: page overflow ${metrics.overflow}`);
    assert.ok(metrics.wrappers, "workspace and sticky topbar wrappers remain mounted");
    if (!inspector) assert.equal(metrics.tableHeader, "sticky", "table headers stay sticky inside the scrolling frame");
    if (metrics.left !== undefined) assert.ok(metrics.left >= -1 && metrics.right <= metrics.width + 1,
      `${name} ${width} ${theme}: content frame remains within the window ${JSON.stringify(metrics)}`);
    await page.screenshot({ path: path.join(shots, `${name}-${width}-${theme}.png`) });
  }
  await page.setViewport({ width: 1440, height: 1000 });
  await h.views();
};

try {
  await h.server();
  // Health deliberately stays idle with automatic sampling off. The user's
  // explicit Refresh starts the held identity read for the unknown-state case.
  await h.refresh();
  await page.waitForSelector(`${healthView} [data-action="refresh"][aria-busy="true"]`);
  assert.equal(await page.$("#server-nav-extensions"), null, "Extensions is absent while identity is unknown and Health is pending");
  releaseFirstHealth();
  await waitText(healthView, healthBase.server_version);
  await h.idle(healthView);
  assert.equal(await page.$("#server-nav-extensions"), null, "a report without explicit VillageSQL flavor never adds Extensions");
  for (const [engine, flavor, product, version] of [
    ["mysql", "mysql", "MySQL", "8.4.11"],
    ["mysql", "percona", "Percona Server", "8.4.11-11"],
    ["mariadb", "mariadb", "MariaDB", "11.4.9-MariaDB"],
    ["mysql", "readyset", "Readyset", "8.0.0-readyset"],
  ]) {
    healthReport = { ...clone(healthBase), engine, flavor, server_product: product, server_version: version };
    const before = healthReads.length;
    await h.refresh();
    await page.waitForFunction((s, value) => document.querySelector(s)?.textContent.includes(value), {}, healthView, version);
    await h.idle(healthView);
    assert.equal(healthReads.length, before + 1);
    assert.equal(await page.$("#server-nav-extensions"), null, `${product} has no Extensions tab`);
    assert.equal(state.seen.length, 0, "non-VillageSQL navigation never requests extensions");
  }
  // Only an explicit, validated VillageSQL flavor from the connection check
  // enables this tab. Product/version labels alone never identify the server.
  const beforeChecks = healthReads.length;
  let checkSerial = 0;
  for (const flavor of ["mysql", "percona", "mariadb", "readyset", "unknown", "VillageSQL", null, ["villagesql"], "villagesql"]) {
    readinessReport = { ...clone(readiness), flavor, product: "VillageSQL", version: "8.4.11-villagesql-0.0.8",
      checked_at: `2026-11-27T12:00:${String(++checkSerial).padStart(2, "0")}Z` };
    const before = readinessReads.length;
    await chooseConnection(page, h.seeded.checked.id);
    await page.waitForFunction((expected, checkedAt) => {
      const saved = JSON.parse(localStorage.getItem("myflames.connectionReadiness.v1") || "{}");
      return Object.values(saved).some(entry => !entry.stale && entry.facts?.flavor === expected && entry.facts?.checked_at === checkedAt);
    }, {}, ["mysql", "percona", "mariadb", "readyset", "villagesql"].includes(flavor) ? flavor : "", readinessReport.checked_at);
    assert.equal(readinessReads.length, before + 1, "each explicit connection check reads readiness once");
    if (flavor === "villagesql") await page.waitForSelector("#server-nav-extensions", { visible: true });
    else assert.equal(await page.$("#server-nav-extensions"), null, `${JSON.stringify(flavor)} readiness has no Extensions tab`);
    assert.equal(healthReads.length, beforeChecks, "connection checks do not need a Health read");
    assert.equal(state.seen.length, 0, "connection checks do not read extension inventory");
  }
  // Persisted VillageSQL identity is stale after reload and cannot grant a tab.
  const beforeReload = readinessReads.length;
  await page.reload({ waitUntil: "networkidle0" });
  await page.waitForSelector(healthView, { visible: true });
  assert.equal(await page.$("#server-nav-extensions"), null, "stale VillageSQL readiness cannot enable Extensions");
  assert.equal(readinessReads.length, beforeReload, "restoring readiness never probes the server");
  await chooseConnection(page, h.seeded.checked.id);
  await page.waitForSelector("#server-nav-extensions", { visible: true });
  healthReport = { ...clone(healthBase), engine: "mysql", flavor: "villagesql", server_product: "VillageSQL",
    server_base: "percona", server_version: "8.4.11-villagesql-0.0.8" };
  await h.refresh();
  await page.waitForSelector("#server-nav-extensions", { visible: true });
  await h.idle(healthView);
  // Receipt order, rather than the server's clock or evidence source, owns
  // identity. Unknown readiness must not mask a known connection-bound report.
  readinessReport = { ...clone(readiness), flavor: "", checked_at: "2000-01-01T00:00:00Z" };
  await chooseConnection(page, h.seeded.checked.id);
  await page.waitForFunction(() => Object.values(JSON.parse(localStorage.getItem("myflames.connectionReadiness.v1") || "{}"))
    .some(entry => !entry.stale && entry.facts?.flavor === "" && entry.facts?.checked_at === "2000-01-01T00:00:00Z"));
  assert.ok(await page.$("#server-nav-extensions"), "unknown readiness keeps the report's VillageSQL identity");
  for (const [checkedFlavor, reportFlavor] of [["mysql", "villagesql"], ["villagesql", "mysql"]]) {
    readinessReport = { ...clone(readiness), flavor: checkedFlavor, checked_at: "2000-01-01T00:00:00Z" };
    await chooseConnection(page, h.seeded.checked.id);
    await page.waitForFunction(expected => !!document.querySelector("#server-nav-extensions") === expected, {}, checkedFlavor === "villagesql");
    healthReport = { ...clone(healthBase), flavor: reportFlavor, taken_at: "1999-01-01T00:00:00Z" };
    await h.refresh();
    await h.idle(healthView);
    await page.waitForFunction(expected => !!document.querySelector("#server-nav-extensions") === expected, {}, reportFlavor === "villagesql");
  }
  healthReport = { ...clone(healthBase), flavor: "villagesql", server_product: "VillageSQL" };
  await h.refresh();
  await h.idle(healthView);
  await page.waitForSelector("#server-nav-extensions", { visible: true });
  const initial = deferred();
  state.queue.push({ ...ok(empty), wait: initial.wait });
  await h.view("extensions");
  await page.waitForSelector(`${view} [data-action="refresh"][aria-busy="true"]`);
  assert.match(await reportText(), /Reading|Loading|Refreshing/);
  assert.equal(state.seen.length, 1, "opening Extensions reads once");
  initial.release();
  await waitText(view, "No extensions are installed.");
  await h.idle(view);
  assert.match(await reportText(), /INSTALL EXTENSION.*EXTENSION_ADMIN/s);
  assert.equal(await page.$(table), null, "an empty report has an empty state instead of a blank table");
  assert.equal((await page.$$eval(`${view} button`, elements => elements.map(element => element.textContent)))
    .some(label => /^(Install|Uninstall|Update extension)/.test(label.trim())), false, "Extensions exposes no server write controls");

  const loading = deferred();
  state.queue.push({ ...ok(populated), wait: loading.wait });
  const beforeRefresh = state.seen.length;
  await h.refresh();
  await page.waitForSelector(`${view} [data-action="refresh"][aria-busy="true"]`);
  const refreshing = `${view} [data-action="refresh"]`;
  assert.match(await h.text(refreshing), /Refreshing/);
  await page.$eval(refreshing, button => button.click());
  assert.equal(state.seen.length, beforeRefresh + 1, "repeated Refresh during a read is single flight");
  loading.release();
  await waitRead(beforeRefresh, "vsql_complex");
  assert.match(await h.text(`${view} .summary-line`), /4.*installed/i);
  assert.match(await h.text(`${view} .summary-line`), /1.*pending|pending.*1/i);
  assert.match(await h.text(`${view} .summary-line`), /1.*fail|fail.*1/i);
  assert.match(await reportText(), /\/usr\/lib\/veb\//);
  await page.waitForFunction(s => [...document.querySelectorAll(`${s} [role="status"]`)].some(element => /Updated at/.test(element.textContent)), {}, view);
  assert.deepEqual(await page.$$eval(`${table} thead th[data-col]`, elements => elements.map(element => element.dataset.col)),
    ["name", "version", "protocol", "types", "functions", "columns", "pending"]);

  await h.select(row("vsql_complex"));
  let detail = await h.inspector();
  assert.match(detail, /Protocol 5 · Current/);
  for (const value of ["complex_add", "COMPLEX", "signals", "amplitude", "normalize_signal", "vsql_complex.mode", "precise", "vsql_complex.ext_hits", "42", "0.2.0"])
    assert.ok(detail.includes(value), `inspector includes ${value}`);
  assert.match(detail, /Showing 2 of 3 columns\./);
  assert.match(detail, /Showing 1 of 2 routines\./);
  assert.doesNotMatch(detail, /the server listed more/, "complete registration lists do not claim truncation");
  assert.ok((await page.$$eval(`${view} .view-inspector pre`, elements => elements.map(element => element.textContent))).includes(hostileError),
    "pending error remains exact plain preformatted text");
  assert.equal(await page.$("[data-extension-injected]"), null);
  assert.equal(await page.evaluate(() => window.extensionInjected), undefined);
  await h.select(row("vsql_unsupported"));
  detail = await h.inspector();
  assert.match(detail, /unsupported/i);
  assert.match(detail, /0\.0\.8.*refuses|refuses.*0\.0\.8/i);
  await h.select(row("vsql_legacy"));
  assert.match(await h.inspector(), /deprecated/i);
  assert.match(await h.inspector(), /No variables were read for this extension\./);
  assert.doesNotMatch(await h.inspector(), /The read was truncated\./, "complete variable lists do not claim truncation");
  await h.select(row(hostileName));
  assert.ok((await h.inspector()).includes(hostileName), "hostile extension name is readable text");
  assert.ok((await h.inspector()).includes(`fn_${hostile}`));
  assert.equal(await page.$("[data-extension-injected]"), null, "extension-supplied names do not create DOM elements");
  assert.equal(await page.evaluate(() => window.extensionInjected), undefined);
  await back();

  // Downloads use the real ExportMenu and real Blob contents, with only the
  // browser's final save click captured to keep artifacts inside the suite.
  await page.evaluate(() => {
    window.extensionDownloads = [];
    const blobs = new Map();
    const create = URL.createObjectURL.bind(URL);
    URL.createObjectURL = blob => { const url = create(blob); blobs.set(url, blob); return url; };
    const click = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      if (this.download && blobs.has(this.href)) {
        const entry = { filename: this.download, text: null };
        window.extensionDownloads.push(entry);
        void blobs.get(this.href).text().then(text => { entry.text = text; });
      } else click.call(this);
    };
  });
  for (const format of ["csv", "text"]) {
    await page.click(`${view} .export-button`);
    await page.click(`.export-menu-items [data-format="${format}"]`);
    await page.waitForFunction(n => window.extensionDownloads.length === n && window.extensionDownloads[n - 1].text !== null,
      {}, format === "csv" ? 1 : 2);
  }
  const downloads = await page.evaluate(() => window.extensionDownloads);
  assert.match(downloads[0].filename, /\.csv$/);
  assert.match(downloads[0].text, /"Name","Version","Protocol"/);
  assert.match(downloads[0].text, /vsql_complex/);
  assert.match(downloads[1].filename, /\.txt$/);
  assert.match(downloads[1].text, /VillageSQL|Extensions/);
  assert.match(downloads[1].text, /vsql_complex/);

  // One read may fail independently; known values stay visible and unread
  // lists/counters stay unknown instead of becoming a misleading zero.
  const partial = clone(populated);
  partial.extensions = [extension("vsql_partial", { protocol: null, protocol_state: "unknown", sdk_version: null,
    functions: null, functions_truncated: null, types: null, types_truncated: null,
    columns: { count: null, sample: [], truncated: null }, routines: { count: null, sample: [], truncated: null },
    variables: null, variables_truncated: null })];
  partial.summary = { installed: 1, pending: 0, failing: 0, deprecated_protocol: null, unsupported_protocol: null };
  partial.settings.server_protocol = null;
  partial.notes = ["Could not read extension registrations: access denied.", "Could not read custom type dependencies."];
  await refresh(partial, "vsql_partial");
  assert.match(await h.notes(), /Could not read extension registrations.*access denied/s);
  await h.select(row("vsql_partial"));
  assert.match(await h.inspector(), /not collected|unknown|unavailable/i);
  assert.doesNotMatch(await h.inspector(), /First 0 of 0/i, "unread dependencies never masquerade as an empty result");
  const unavailable = { ...clone(partial), extensions: null, summary: {
    installed: null, pending: null, failing: null, deprecated_protocol: null, unsupported_protocol: null,
  }, notes: ["Could not read installed extensions: access denied."] };
  await refresh(unavailable, "Extension inventory was not collected");
  assert.doesNotMatch(await reportText(), /No extensions are installed\./, "failed installed-list read never claims the server is empty");

  const truncated = clone(populated);
  truncated.extensions[0].columns = { count: null, sample: [{ schema: "testdb", table: "signals", column: "amplitude", data_type: "vsql_complex.COMPLEX" }], truncated: true };
  truncated.extensions[0].routines = { count: null, sample: [{ schema: "testdb", routine: "normalize_signal", routine_type: "FUNCTION", parameter: null, data_type: "vsql_complex.COMPLEX" }], truncated: true };
  truncated.extensions[0].functions_truncated = true;
  truncated.extensions[0].types_truncated = true;
  truncated.extensions[0].variables = [];
  truncated.extensions[0].variables_truncated = true;
  truncated.extensions[0].pending.last_error = (hostileError + "x".repeat(2100)).slice(0, 2000);
  truncated.extensions[0].pending.error_truncated = true;
  truncated.extensions.push(extension("vsql_unseen", {
    functions: null, functions_truncated: true, types: null, types_truncated: true,
    columns: { count: null, sample: [], truncated: true },
    routines: { count: null, sample: [], truncated: true },
  }));
  truncated.summary.installed++;
  truncated.notes = ["Custom type column dependencies are limited to the first 500 rows; totals are not collected."];
  await refresh(truncated, "vsql_complex");
  await h.select(row("vsql_unseen"));
  detail = await h.inspector();
  assert.match(detail, /Showing 0 columns; more may exist, the total is unknown\./);
  assert.match(detail, /Showing 0 routines; more may exist, the total is unknown\./);
  assert.doesNotMatch(detail, /more exist/, "a global cap does not prove this extension has omitted dependencies");
  assert.match(detail, /Not collected\./, "registration lists omitted by the report budget stay unknown");
  assert.doesNotMatch(detail, /None registered/, "an exhausted registration budget cannot establish an empty list");
  await back();
  await h.select(row("vsql_complex"));
  detail = await h.inspector();
  assert.match(detail, /Showing 1 column; more may exist, the total is unknown\./);
  assert.match(detail, /Showing 1 routine; more may exist, the total is unknown\./);
  assert.match(detail, /Showing the first name; the server listed more\./);
  assert.match(detail, /Showing the first 3 names; the server listed more\./);
  assert.match(detail, /truncated|first 2,000|first 2000/i, "bounded pending errors identify their truncation");
  const variablesText = await page.$$eval(`${view} .view-inspector section`, sections =>
    sections.find(section => section.querySelector("h4,h3")?.textContent === "Variables")?.textContent);
  assert.match(variablesText || "", /truncated/i, "an empty variable sample at the global cap still identifies omitted variables");
  assert.match(variablesText || "", /No variables were read for this extension\. The read was truncated\./);
  assert.ok((await page.$$eval(`${view} .view-inspector pre`, elements => elements.map(element => element.textContent)))
    .includes(truncated.extensions[0].pending.last_error));
  await layout("extensions-inspector", true);
  await back();
  await layout("extensions-table");

  const plural = clone(truncated);
  plural.extensions[0].columns.sample.push({ schema: "testdb", table: "signals", column: "phase", data_type: "vsql_complex.COMPLEX" });
  plural.extensions[0].routines.sample.push({ schema: "testdb", routine: "combine_signals", routine_type: "FUNCTION", parameter: null, data_type: "vsql_complex.COMPLEX" });
  await refresh(plural, "vsql_complex");
  await h.select(row("vsql_complex"));
  assert.match(await h.inspector(), /Showing 2 columns; more may exist, the total is unknown\./);
  assert.match(await h.inspector(), /Showing 2 routines; more may exist, the total is unknown\./);

  // The three live recordings exercise actual installed names, registered
  // function/type arrays and dependency rows through the same component.
  for (const { codebase, report } of recordedReports) {
    assert.ok(report.extensions.length > 0, `${codebase} has a recorded installed extension inventory`);
    await refresh(report, report.extensions[0].name);
    assert.equal(await page.$$eval(`${table} tbody tr[data-row-key]`, elements => elements.length), report.extensions.length,
      `${codebase} displays every recorded extension`);
    assert.ok((await h.text(`${view} .summary-line`)).includes(`${report.summary.installed} installed`));
  }
  await refresh(recordedReports[0].report, recordedReports[0].report.extensions[0].name);
  await layout("extensions-live");

  // A failed HTTP refresh retains the last good reading and waits for a
  // user's retry. It must not convert the table into an empty report.
  const beforeFailure = state.seen.length;
  state.queue.push({ status: 400, body: { error: "Could not read extensions: synthetic connection failure." } });
  await h.refresh();
  await waitText(`${view} [role="alert"]`, "synthetic connection failure");
  await h.idle(view);
  assert.equal(state.seen.length, beforeFailure + 1);
  assert.ok(await page.$(row(recordedReports[0].report.extensions[0].name)), "last good data remains after refresh failure");
  await refresh(empty, "No extensions are installed.");

  // A retained reading belongs to its original connection while the next
  // VillageSQL connection is pending or failed, including its exports.
  const switched = deferred();
  state.queue.push({ status: 400, body: { error: "Could not read extensions: switched connection failure." }, wait: switched.wait });
  readinessReport = { ...clone(readiness), flavor: "villagesql", product: "VillageSQL" };
  try {
    await chooseConnection(page, h.seeded.other.id);
    await page.waitForSelector(`${view} [data-action="refresh"][aria-busy="true"]`);
    assert.equal(await page.$eval(`${view} .export-button`, button => button.disabled), true,
      "another connection's retained report cannot be exported during a read");
    await page.$eval(`${view} .export-button`, button => button.click());
    assert.equal(await page.evaluate(() => window.extensionDownloads.length), 2, "a disabled export creates no download");
  } finally {
    switched.release();
  }
  await waitText(`${view} [role="alert"]`, "switched connection failure");
  await h.idle(view);
  assert.equal(await page.$eval(`${view} .export-button`, button => button.disabled), true,
    "another connection's retained report cannot be exported after failure");
  await refresh(empty, "No extensions are installed.");
  assert.equal(await page.$eval(`${view} .export-button`, button => button.disabled), false,
    "a successful reading from the active connection enables export again");

  // Switching to a MySQL connection hides an already loaded VillageSQL view
  // immediately. A stale report must not grant a different connection a tab.
  const beforeSwitch = state.seen.length;
  healthReport = { ...clone(healthBase), flavor: "mysql", server_product: "MySQL" };
  readinessReport = clone(readiness);
  await chooseConnection(page, h.seeded.other.id);
  await page.waitForFunction(() => !document.querySelector("#server-nav-extensions"));
  await page.waitForFunction(s => !document.querySelector(s) || !document.querySelector(s).getClientRects().length, {}, view);
  assert.equal(state.seen.length, beforeSwitch, "switching to MySQL does not request extensions");

  const unexpected = state.other.filter(item => !["/api/live/health", "/api/live/readiness"].includes(item.path));
  assert.deepEqual(unexpected, [], "the view uses only its read endpoint and existing identity reads");
  for (const request of state.seen) {
    assert.ok(request.connection && typeof request.connection === "object");
    assert.equal(request.sql, undefined, "the read endpoint never receives client SQL");
    assert.equal(request.confirm_token, undefined, "a read-only view has no write confirmation flow");
  }
  assert.deepEqual(h.errors, []);
  assert.deepEqual(diagnostics.failed, []);
  assert.deepEqual(diagnostics.http, [{ path: "/api/live/extensions", status: 400 }, { path: "/api/live/extensions", status: 400 }]);
  assert.equal(diagnostics.console.filter(item => /Failed to load resource: the server responded with a status of 400/.test(item.text)).length, 2,
    "the intentional failed refreshes are the only HTTP console diagnostics");
  assert.deepEqual(diagnostics.console.filter(item => !/Failed to load resource: the server responded with a status of 400/.test(item.text)), []);
  console.log("extensions browser: identity gates, loading/empty/populated, protocols, dependencies, partial errors, bounds, hostile text, refresh, exports and 1440/720 light/dark passed");
} catch (error) {
  await page.screenshot({ path: path.join(shots, "extensions-failure.png"), fullPage: true }).catch(() => {});
  await fs.writeFile(path.join(shots, "extensions-diagnostics.json"), JSON.stringify({
    error: String(error), pageErrors: h.errors, diagnostics, seen: state.seen.map(({ connection, ...rest }) => ({
      ...rest, connection: connection ? { host: connection.host, port: connection.port, profile_id: connection.profile_id } : null,
    })), other: state.other.map(item => item.path),
    text: await page.$eval("body", element => element.textContent).catch(() => "unavailable"),
  }, null, 2));
  throw error;
} finally {
  releaseFirstHealth();
  await h.close();
}
