// Parameters > Versions (ParameterVersions.tsx) with the real components and
// kit. Every HTTP request is intercepted and Chromium uses a pipe: no loopback
// server, no database. The catalog replies are SYNTHETIC
// (fixtures/parameter-versions-mock.mjs) in the PLAN's contracts; when the
// JSON Schemas exist (docs/schemas/parameter-version-*.json) the replies are
// checked against them first.
//   node test/parameter-versions.mjs            browser suite
//   node test/parameter-versions.mjs --unit     helpers and typed calls only
//   PV_SHOTS=DIR node test/parameter-versions.mjs   also saves screenshots
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { build, transformWithEsbuild } from "vite";
import puppeteer from "puppeteer";
import { buildMock } from "./fixtures/parameter-versions-mock.mjs";

const workspace = fileURLToPath(new URL("../", import.meta.url));
const root = path.resolve(workspace, "../..");
const source = name => JSON.stringify(path.join(workspace, "src", name));
const mock = buildMock(await fs.readFile(path.join(root, "myflames/mysql_sysvars.py"), "utf8"));
const temporary = await fs.mkdtemp(path.join(workspace, ".parameter-versions-test-"));
const shots = process.env.PV_SHOTS ? path.resolve(process.env.PV_SHOTS) : null;
let browser, activePage;

/** The replies the browser gets, also checked against the contracts' schemas once CORE has written them. */
function validateContracts() {
  const replies = { "parameter-version-catalog-1.0": [mock.catalog()],
    "parameter-version-compare-1.0": [mock.compare("mysql", "8.0.36", "8.4.3"), mock.compare("mariadb", "10.6.12", "11.8.3")],
    "parameter-history-1.0": [mock.history("mysql", "innodb_log_file_size"), mock.history("mysql", "innodb_log_size")] };
  const file = path.join(temporary, "replies.json");
  return fs.writeFile(file, JSON.stringify(replies)).then(() => {
    const checked = spawnSync(process.env.PYTHON || "python3", ["-c", `
import json,sys
from pathlib import Path
sys.path.insert(0,'test')
from test_chart_data import validate
done=[]
for name,items in json.loads(Path(sys.argv[1]).read_text()).items():
    schema=Path('docs/schemas/'+name+'.json')
    if not schema.exists(): continue
    for item in items: validate(item,json.loads(schema.read_text()))
    done.append(name)
print(','.join(done) or 'none')
`, file], { cwd: root, env: { ...process.env, HOME: temporary, USERPROFILE: temporary, MYFLAMES_SECRET_BACKEND: "vault", PYTHONDONTWRITEBYTECODE: "1" }, encoding: "utf8" });
    assert.equal(checked.status, 0, checked.stderr);
    console.log(`parameter-versions: replies checked against schemas: ${checked.stdout.trim()}`);
  });
}

async function unit() {
  const compiled = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
    build: { ssr: true, write: false, rollupOptions: { input: path.join(workspace, "src/paramVersionsApi.ts"), output: { inlineDynamicImports: true } } } });
  const api = await import("data:text/javascript;base64," + Buffer.from(compiled.output.find(o => o.type === "chunk").code).toString("base64"));
  const mysql = mock.catalog().engines[0];
  assert.equal(api.releaseOf("8.4.6-6"), "8.4.6");
  assert.equal(api.releaseOf("11.4.5-MariaDB-log"), "11.4.5");
  assert.equal(api.releaseOf("garbage"), null);
  assert.ok(api.compareReleases("8.0.9", "8.0.10") < 0 && api.compareReleases("9.0.1", "8.4.6") > 0 && api.compareReleases("8.4.3", "8.4.3") === 0);
  const groups = api.seriesGroups(mysql.versions);
  assert.equal(groups[0].series, "9.4", "newest series first");
  assert.deepEqual(groups.find(g => g.series === "8.4").releases.map(r => r.version).slice(0, 2), ["8.4.6", "8.4.5"]);
  assert.deepEqual(api.serverRelease(mysql, "8.0.36"), { state: "cataloged", version: "8.0.36" });
  assert.deepEqual(api.serverRelease(mysql, "9.9.0-1"), { state: "newer", version: "9.9.0", newest: "9.4.0" });
  assert.equal(api.serverRelease(mysql, "8.0.29").state, "skipped");
  assert.equal(api.serverRelease(mysql, "8.0.29").nearest, "8.0.28");
  assert.deepEqual(api.serverRelease(mysql, null), { state: "none" });
  assert.deepEqual(api.defaultPair(mysql, "8.0.36"), { from: "8.0.36", to: "9.4.0" });
  assert.deepEqual(api.defaultPair(mysql, null), { from: "8.4.6", to: "9.4.0" }, "without a server: the newest LTS or GA release of another series");
  assert.deepEqual(api.defaultPair(mysql, "9.4.0"), { from: "9.3.0", to: "9.4.0" }, "a server on the newest release compares with the one before");
  assert.deepEqual(api.defaultPair(mysql, "8.0.29"), { from: "8.0.28", to: "9.4.0" });
  // Kind filter: a change counts under each of its filters; Other is every kind without one.
  const counts = api.kindCounts([{ kinds: ["added"] }, { kinds: ["default", "range"] }, { kinds: ["deprecated", "default"] }, { kinds: ["dynamic"] }]);
  assert.deepEqual(counts, { all: 4, added: 1, removed: 0, deprecated: 1, default: 2, other: 2 });
  assert.equal(api.shownKind(["deprecated", "default"]), "default");
  assert.equal(api.shownKind(["added"]), "added");
  // Values: exact grouping past 2^53, the server's text on hover; unknowns say why.
  assert.deepEqual(api.groupedValue("18446744073709551615"), { text: "18,446,744,073,709,551,615", title: "18446744073709551615", code: true });
  assert.equal(api.groupedValue("200").text, "200");
  assert.equal(api.groupedValue("ON").text, "ON");
  const f = { scope: "global", type: "integer", dynamic: true, default: null, default_note: "unknown", min: null, max: null, values: null, deprecated: null, replacement: null, alias_of: null, plugin: null };
  assert.equal(api.defaultValue(f).text, "Unknown"); assert.match(api.defaultValue(f).title, /could be read/);
  assert.equal(api.defaultValue({ ...f, default_note: "null" }).text, "NULL");
  assert.equal(api.defaultValue({ ...f, default_note: "host", default: "8" }).text, "Computed at startup");
  assert.equal(api.rangeValue(f).text, "Unknown", "an integer without a range is unknown");
  assert.equal(api.rangeValue({ ...f, type: "string" }).text, "None");
  assert.equal(api.kindValue("default", null).text, "Not present");
  const rows = api.factRows({ ...f, default: "200", default_note: null, min: "100", max: "18446744073709551615" }, { ...f, default: "10000", default_note: null, min: "100", max: "18446744073709551615", deprecated: ["server", "source"] });
  assert.deepEqual(rows.map(r => r.key), ["scope", "type", "dynamic", "default", "range", "deprecated"], "empty optional rows are left out");
  assert.deepEqual(rows.filter(r => r.differs).map(r => r.key), ["default", "deprecated"]);
  assert.equal(rows.find(r => r.key === "deprecated").b.text, "Yes (server warning, source code)");
  // Typed calls: exact paths and bodies, no connection and no password.
  const seen = [];
  globalThis.fetch = async (url, init) => {
    if (url === "/api/auth/session") return { ok: true, json: async () => ({ token: "fixture" }) };
    seen.push({ url, body: JSON.parse(init.body) });
    return { ok: true, json: async () => ({}) };
  };
  await api.versionCatalog(); await api.compareVersions("mysql", "8.0.36", "8.4.3"); await api.parameterHistory("mariadb", "innodb_log_file_size");
  assert.deepEqual(seen, [{ url: "/api/config/versions", body: {} }, { url: "/api/config/versions/compare", body: { engine: "mysql", from: "8.0.36", to: "8.4.3" } },
    { url: "/api/config/versions/history", body: { engine: "mariadb", name: "innodb_log_file_size" } }]);
  console.log("parameter-versions unit: releases, defaults, kind counts, values, facts and typed calls passed");
}

async function compileFixture() {
  const entry = "\0parameter-versions-fixture.tsx";
  const compiled = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
    plugins: [{ name: "parameter-versions-fixture", resolveId(id) { if (id === entry) return id; }, async load(id) {
      if (id !== entry) return null;
      const code = `
        import React, {useEffect, useState} from ${JSON.stringify(path.join(workspace, "node_modules/react/index.js"))};
        import {createRoot} from ${JSON.stringify(path.join(workspace, "node_modules/react-dom/client.js"))};
        import {ConfigView} from ${source("Config.tsx")};
        import {InspectorContext} from ${source("kit.tsx")}; import {ViewOnScreenContext} from ${source("viewOnScreen.ts")};
        import ${source("styles.ts")};
        function Fixture() {
          const [host,setHost]=useState('db1'), [report,setReport]=useState(null), [selected,setSelected]=useState(''), [open,setOpen]=useState(false);
          const [sheet,setSheet]=useState(innerWidth<1040);
          useEffect(()=>{const f=()=>setSheet(innerWidth<1040); addEventListener('resize',f); return ()=>removeEventListener('resize',f);},[]);
          const connection={name:host,host,port:3306,user:'operator',database:'',ssl_mode:'DISABLED'};
          return <><nav aria-label="Test controls"><button onClick={()=>setHost(h=>h==='db1'?'db2':'db1')}>Switch connection</button></nav>
            <main className="fixture-main"><ViewOnScreenContext.Provider value={true}>
              <InspectorContext.Provider value={{open,setOpen,width:380,setWidth:()=>{},sheet}}>
                <ConfigView connection={connection} disabled={false} report={report} onReport={setReport}
                  selection={{key:selected,select:(key,o)=>{setSelected(key);if(o)setOpen(true);}}}/>
              </InspectorContext.Provider></ViewOnScreenContext.Provider></main></>;
        }
        createRoot(document.getElementById('root')).render(<Fixture/>);
      `;
      return (await transformWithEsbuild(code, "fixture.tsx", { loader: "tsx", jsx: "automatic" })).code;
    } }], build: { write: false, target: "es2020", rollupOptions: { input: entry, output: { format: "iife", inlineDynamicImports: true } } } });
  return { css: compiled.output.filter(o => o.type === "asset" && o.fileName.endsWith(".css")).map(o => o.source).join("\n"),
    js: compiled.output.find(o => o.type === "chunk").code };
}

async function browserSuite() {
  const { css, js } = await compileFixture();
  browser = await puppeteer.launch({ headless: "shell", pipe: true, userDataDir: path.join(temporary, "chromium"), env: { ...process.env, HOME: temporary, USERPROFILE: temporary } });
  const page = activePage = await browser.newPage();
  const errors = []; page.on("pageerror", error => errors.push(String(error)));
  const seen = []; let compareDelay = 0;
  const base = JSON.parse(await fs.readFile(path.join(workspace, "test/fixtures/parameters-mysql.json"), "utf8"));
  // db1 runs a cataloged MySQL release; db2 is a Percona Server newer than the catalog.
  const configFor = host => host === "db2"
    ? { ...base, server_version: "9.9.0-1", flavor: "percona", server_product: "Percona Server", parameters: { ...base.parameters, flavor: "percona" } }
    : { ...base, server_version: "8.0.36" };
  const modified = new Set(base.parameters.items.filter(p => p.modified === true).map(p => p.name));
  await page.setRequestInterception(true);
  page.on("request", request => {
    const pathname = new URL(request.url()).pathname;
    const respond = (body, type = "application/json", status = 200) => request.respond({ status, contentType: `${type}; charset=utf-8`, body: type === "application/json" ? JSON.stringify(body) : body }).catch(() => {});
    if (pathname === "/") return void respond('<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script src="/fixture.js"></script></body></html>', "text/html");
    if (pathname === "/favicon.ico") return void respond("", "text/plain");
    if (pathname === "/fixture.js") return void respond(js, "text/javascript");
    if (pathname === "/fixture.css") return void respond(css, "text/css");
    const body = JSON.parse(request.postData() || "{}"); seen.push({ pathname, body });
    if (pathname === "/api/auth/session") return void respond({ token: "fixture" });
    if (pathname === "/api/live/config") return void respond(configFor(body.connection.host));
    if (pathname === "/api/config/versions") return void respond(mock.catalog());
    if (pathname === "/api/config/versions/compare") {
      if (body.from === "8.1.0") return void respond({ error: "Could not compare MySQL 8.1.0: the catalog has no facts for it." }, "application/json", 400);
      let report;
      try { report = mock.compare(body.engine, body.from, body.to); } catch (e) { return void respond({ error: e.message }, "application/json", 400); }
      if (compareDelay) return void setTimeout(() => void respond(report), compareDelay);
      return void respond(report);
    }
    if (pathname === "/api/config/versions/history") return void respond(mock.history(body.engine, body.name));
    errors.push(`Unexpected request: ${pathname}`); void request.abort();
  });
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto("https://versions.invalid/", { waitUntil: "networkidle0" });

  const view = '[data-view="parameters-versions"]';
  const list = '[data-view="config"]';
  const reads = p => seen.filter(r => r.pathname === p);
  const tab = name => page.click(`#parameters-views-tab-${name}`);
  const select = label => `${view} select[aria-label="${label}"]`;
  const value = sel => page.$eval(sel, e => e.value);
  const rowKeys = () => page.$$eval(`${view} tbody tr[data-row-key]`, rows => rows.map(r => r.dataset.rowKey));
  const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const waitCompare = (from, to) => page.waitForFunction((from, to) =>
    !!document.querySelector(`[data-view="parameters-versions"] table[aria-label="Parameter changes from ${from} to ${to}"]`) &&
    !document.querySelector('[data-view="parameters-versions"] .pv-result.is-stale'), {}, from, to);
  const modeButton = label => `${view} .pv-mode button[data-value="${label}"]`;

  // The Parameters list reads; Versions reads nothing until its first visit.
  await page.waitForSelector(`${list} [data-row-key]`);
  assert.equal(reads("/api/config/versions").length, 0, "Versions mounts on first visit");
  await page.type(`${list} input[aria-label="Filter parameters"]`, "innodb");
  await page.waitForFunction(() => [...document.querySelectorAll('[data-view="config"] tbody tr[data-row-key]')].every(r => r.dataset.rowKey.includes("innodb")));
  const listRows = await page.$$eval(`${list} tbody tr[data-row-key]`, rows => rows.length);

  await tab("versions");
  await waitCompare("8.0.36", "9.4.0");
  assert.equal(reads("/api/config/versions").length, 1);
  assert.deepEqual(reads("/api/config/versions/compare").at(-1).body, { engine: "mysql", from: "8.0.36", to: "9.4.0" }, "From is the connected server's release, To the newest");
  assert.equal(await value(select("From")), "8.0.36");
  assert.deepEqual((await page.$$eval(`${select("To")} optgroup`, g => g.map(o => o.label))).slice(0, 7),
    ["MySQL 9.4 (Innovation)", "MySQL 9.3 (Innovation)", "MySQL 9.2 (Innovation)", "MySQL 9.1 (Innovation)", "MySQL 9.0 (Innovation)", "MySQL 8.4 (LTS)", "MySQL 8.3 (Innovation)"]);
  assert.equal(await page.$(`${view} input[type="text"]:not([role])`), null, "releases are picked, never typed");
  // Back to Parameters: its filter, rows and focus target are as they were; then Versions again reads nothing.
  await tab("list");
  assert.equal(await page.$eval(`${list} input[aria-label="Filter parameters"]`, e => e.value), "innodb");
  assert.equal(await page.$$eval(`${list} tbody tr[data-row-key]`, rows => rows.length), listRows);
  await tab("versions"); await settle();
  assert.equal(reads("/api/config/versions").length, 1, "the catalog index loads once");
  assert.equal(reads("/api/config/versions/compare").length, 1, "the same choice is not read again");
  assert.equal(seen.filter(r => r.pathname.startsWith("/api/live/")).length, 1, "Versions reads no server (only the Parameters list read one)");

  // Choosing To reads at once; the busy state shows and the old table greys while it reads.
  compareDelay = 700;
  await page.select(select("To"), "8.4.3");
  await page.waitForSelector(`${view} .action-status[data-phase="pending"]`);
  assert.match(await page.$eval(`${view} .action-status[data-phase="pending"]`, e => e.textContent), /Comparing MySQL 8\.0\.36 with 8\.4\.3/);
  assert.ok(await page.$(`${view} .pv-result.is-stale`), "the previous result is greyed while the new one reads");
  compareDelay = 0;
  await waitCompare("8.0.36", "8.4.3");
  const report = mock.compare("mysql", "8.0.36", "8.4.3");
  assert.equal(await page.$eval(`${view} .summary-text`, e => e.textContent), report.summary);
  assert.equal((await rowKeys()).length, report.changes.length);

  // Stop on a slow read keeps the earlier result greyed and not busy, says so,
  // and Try again reads the current choice.
  const other = await page.$$eval(`${select("To")} option`, o => o.map(x => x.value).find(v => v && !["8.4.3", "8.0.36", "8.1.0"].includes(v)));
  compareDelay = 14_000; // Stop appears after 10 s; the late reply lands on a cancelled read
  await page.select(select("To"), other);
  const stop = await page.waitForFunction(sel => [...document.querySelectorAll(sel)].find(b => b.textContent === "Stop"),
    { timeout: 20_000 }, `${view} .action-status button`);
  await stop.asElement().click();
  await page.waitForSelector(`${view} .action-status[data-phase="stopped"]`);
  assert.match(await page.$eval(`${view} .action-status[data-phase="stopped"]`, e => e.textContent), /^Stopped\. The table shows the earlier choice\.Try again$/);
  assert.ok(await page.$(`${view} .pv-result.is-stale`), "the earlier result stays greyed");
  assert.equal(await page.$(`${view} .pv-result[aria-busy]`), null, "nothing is busy after Stop");
  compareDelay = 0;
  await page.click(`${view} button[data-action="retry-versions"]`);
  await waitCompare("8.0.36", other);
  assert.equal(await page.$(`${view} .action-status[data-phase="stopped"]`), null);
  await page.select(select("To"), "8.4.3");
  await waitCompare("8.0.36", "8.4.3");

  // Swap reads the other direction and swaps both pick lists.
  await page.click(`${view} button[aria-label="Swap From and To"]`);
  await waitCompare("8.4.3", "8.0.36");
  assert.deepEqual([await value(select("From")), await value(select("To"))], ["8.4.3", "8.0.36"]);
  assert.deepEqual(reads("/api/config/versions/compare").at(-1).body, { engine: "mysql", from: "8.4.3", to: "8.0.36" });
  await page.click(`${view} button[aria-label="Swap From and To"]`);
  await waitCompare("8.0.36", "8.4.3");

  // Kind filter: counts from the rows, each filter shows its count.
  const segments = await page.$$eval(`${view} .pv-kind-filter button`, b => b.map(x => [x.dataset.value, Number(x.querySelector(".segment-count").textContent.replace(/\D/g, ""))]));
  const expected = { all: report.changes.length, added: 0, removed: 0, deprecated: 0, default: 0, other: 0 };
  for (const c of report.changes) {
    for (const k of ["added", "removed", "deprecated", "default"]) if (c.kinds.includes(k)) expected[k] += 1;
    if (c.kinds.some(k => !["added", "removed", "deprecated", "default"].includes(k))) expected.other += 1;
  }
  assert.deepEqual(Object.fromEntries(segments), expected);
  for (const kind of ["added", "removed", "other"]) {
    await page.click(`${view} .pv-kind-filter button[data-value="${kind}"]`);
    await page.waitForFunction((n, sel) => document.querySelectorAll(sel).length === n, {}, expected[kind], `${view} tbody tr[data-row-key]`);
  }
  await page.click(`${view} .pv-kind-filter button[data-value="all"]`);
  // Badges: relationship words in comparison tones, never health colors; at most three per row.
  const badges = await page.$$eval(`${view} tbody [data-comparison-status]`, b => b.map(x => ({ text: x.textContent, health: /\bis-(ok|warn|danger)\b/.test(x.className) })));
  assert.ok(badges.length >= report.changes.length && badges.every(b => !b.health));
  assert.ok(badges.some(b => b.text === "Added") && badges.some(b => b.text === "Removed") && badges.some(b => b.text === "Default"));
  assert.match(await page.$eval(`${view} tr[data-row-key="innodb_numa_interleave"]`, e => e.textContent), /Deprecated.*Default.*\+2/, "four kinds: two badges and +2");
  // Name filter.
  await page.type(`${view} input[aria-label="Filter parameters"]`, "redo_log");
  await page.waitForFunction(sel => [...document.querySelectorAll(sel)].map(r => r.dataset.rowKey).join() === "innodb_redo_log_capacity", {}, `${view} tbody tr[data-row-key]`);
  await page.$eval(`${view} input[aria-label="Filter parameters"]`, e => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; set.call(e, ""); e.dispatchEvent(new Event("input", { bubbles: true })); });
  await page.waitForFunction((n, sel) => document.querySelectorAll(sel).length === n, {}, report.changes.length, `${view} tbody tr[data-row-key]`);

  // Only parameters set on the connection in use (modified in its Parameters report).
  const setLabel = await page.$eval(`${view} .pv-filters label.checkbox`, e => e.textContent.trim());
  const setExpected = report.changes.filter(c => modified.has(c.name)).length;
  assert.equal(setLabel, `Only parameters set on db1 (${setExpected})`);
  assert.ok(setExpected >= 1, "the fixture has a set parameter among the changes");
  await page.click(`${view} .pv-filters label.checkbox input`);
  await page.waitForFunction((n, sel) => document.querySelectorAll(sel).length === n, {}, setExpected, `${view} tbody tr[data-row-key]`);
  assert.ok((await rowKeys()).every(k => modified.has(k)));
  await page.click(`${view} .pv-filters label.checkbox input`);

  // Keyboard: Enter on a row opens the inspector; Setting | From | To with grouped integers and exact text on hover.
  await page.focus(`${view} tr[data-row-key="innodb_io_capacity"]`); await page.keyboard.press("Enter");
  await page.waitForSelector(`${view} .view-inspector .pv-facts`);
  assert.deepEqual(await page.$$eval(`${view} .view-inspector .pv-facts thead th`, e => e.map(h => h.textContent)), ["Setting", "8.0.36", "8.4.3"]);
  const facts = Object.fromEntries(await page.$$eval(`${view} .view-inspector .pv-facts tbody tr`, rows => rows.map(r => [r.querySelector("th").textContent, [...r.querySelectorAll("td")].map(c => [c.textContent, c.querySelector("[title]")?.title || ""])])));
  assert.deepEqual(facts.DefaultDifferent, [["200", ""], ["10,000", "10000"]], "grouped integers keep the exact text on hover");
  assert.deepEqual(facts.Range[1], ["100 to 2⁶⁴ − 1", "100 to 18,446,744,073,709,551,615"], "an integer limit reads as the limit, exact on hover");
  assert.equal(await page.$eval(`${view} .view-inspector .pv-sentences`, e => e.textContent), "The default changed from 200 to 10000.", "Python's sentences as sent");
  assert.equal(await page.$(`${view} .view-inspector .pv-since`), null, "Python's sentence names the release; no second Since line");
  assert.match(await page.$eval(`${view} .view-inspector .inspector-meta`, e => e.textContent), /Default/);
  // Escape closes the inspector and returns focus to the row.
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector('[data-view="parameters-versions"] .view-inspector .pv-facts'));
  await page.waitForFunction(() => document.activeElement?.dataset?.rowKey === "innodb_io_capacity");
  // Arrow keys move between rows; Enter opens the next one.
  await page.keyboard.press("ArrowDown");
  const next = await page.evaluate(() => document.activeElement?.dataset?.rowKey);
  assert.ok(next && next !== "innodb_io_capacity");
  await page.keyboard.press("Enter");
  await page.waitForFunction(n => document.querySelector('[data-view="parameters-versions"] .inspector-title h3')?.textContent === n, {}, next);
  await page.keyboard.press("Escape");

  // A deprecated parameter with a replacement, then Show full history.
  await page.focus(`${view} tr[data-row-key="innodb_numa_interleave"]`); await page.keyboard.press("Enter");
  await page.waitForSelector(`${view} [data-action="show-full-history"]`);
  await page.click(`${view} [data-action="show-full-history"]`);
  await page.waitForFunction(() => document.querySelector('[data-view="parameters-versions"] .pv-history-table'));
  assert.equal(await page.$eval(`${view} input[role="combobox"]`, e => e.value), "innodb_numa_interleave");
  assert.deepEqual(reads("/api/config/versions/history").at(-1).body, { engine: "mysql", name: "innodb_numa_interleave" });
  assert.equal(await page.$(`${view} .view-inspector .pv-facts`), null, "Parameter history has no row inspector open");
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("role")), "combobox", "focus lands on the parameter field");
  const seriesRows = await page.$$eval(`${view} .pv-history-table tbody`, b => b.map(t => [t.dataset.series, [...t.querySelectorAll("tr[data-version]")].map(r => r.dataset.version)]));
  assert.deepEqual(seriesRows, [["8.4", ["8.4.0"]]]);

  // Unknown name: typed text plus Enter asks for it as written; Python's close names become buttons.
  await page.click(`${view} input[role="combobox"]`, { clickCount: 3 });
  await page.keyboard.type("innodb_log_size"); await page.keyboard.press("Enter");
  await page.waitForSelector(`${view} .pv-suggestions button`);
  assert.match(await page.$eval(`${view} .summary-text`, e => e.textContent), /no parameter named innodb_log_size/);
  const suggestions = await page.$$eval(`${view} .pv-suggestions button`, b => b.map(x => x.textContent));
  assert.ok(suggestions.includes("innodb_log_file_size"), suggestions.join());
  await page.locator(`${view} .pv-suggestions button ::-p-text(innodb_log_file_size)`).click();
  await page.waitForFunction(() => document.querySelector('[data-view="parameters-versions"] .pv-latest'));
  const latest = Object.fromEntries(await page.$$eval(`${view} .pv-latest .inspector-fields > div`, d => d.map(x => [x.querySelector("dt").textContent, x.querySelector("dd").textContent])));
  assert.equal(latest["First release"], "8.0.11 (the first cataloged)");
  assert.equal(latest.Default, "50,331,648");
  assert.equal(latest.Replacement, "innodb_redo_log_capacity");
  assert.match(latest.Deprecated, /^Yes \(manual, server warning, source code\)$/);
  assert.deepEqual(await page.$$eval(`${view} .pv-history-table tr[data-version]`, r => r.map(x => x.dataset.version)), ["8.0.30"]);
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("role")), "combobox", "a close name returns focus to the field");

  // Combobox keyboard: type, ArrowDown, Enter picks the catalog name; Escape closes the list first.
  await page.click(`${view} input[role="combobox"]`, { clickCount: 3 });
  await page.keyboard.type("redo_log_cap");
  await page.waitForSelector(`${view} .pv-combobox-list:not([hidden]) [role="option"]`);
  assert.deepEqual(await page.$$eval(`${view} [role="option"]`, o => o.map(x => x.textContent)), ["innodb_redo_log_capacity"]);
  await page.keyboard.press("ArrowDown");
  assert.match(await page.$eval(`${view} input[role="combobox"]`, e => e.getAttribute("aria-activedescendant") || ""), /option-0/);
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => /from 8\.0\.30/.test(document.querySelector('[data-view="parameters-versions"] .summary-text')?.textContent || ""));
  assert.deepEqual(await page.$$eval(`${view} .pv-history-table tbody`, b => b.map(t => t.dataset.series)), ["8.0", "8.4"], "changes grouped by series, oldest first");
  await page.click(`${view} input[role="combobox"]`, { clickCount: 3 }); await page.keyboard.type("innodb_io");
  await page.waitForSelector(`${view} .pv-combobox-list:not([hidden])`);
  await page.keyboard.press("Escape");
  await page.waitForSelector(`${view} .pv-combobox-list[hidden]`, { hidden: true });
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("role")), "combobox");

  // Keyboard on the mode switch: arrows change the mode.
  await page.focus(modeButton("history")); await page.keyboard.press("ArrowLeft");
  await page.waitForSelector(`${view} .pv-kind-filter`);
  assert.equal(await page.$eval(modeButton("compare"), e => e.getAttribute("aria-pressed")), "true");
  await page.keyboard.press("ArrowRight");
  await page.waitForSelector(`${view} .pv-history-table`);

  // Version history from the Parameters inspector: Versions > Parameter history for that name.
  await tab("list");
  await page.$eval(`${list} input[aria-label="Filter parameters"]`, e => { const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; set.call(e, "innodb_io_capacity"); e.dispatchEvent(new Event("input", { bubbles: true })); });
  // The list filters with a deferred value: wait for the one row, then open it.
  await page.waitForFunction(sel => { const keys = [...document.querySelectorAll(sel)].map(r => r.dataset.rowKey); return keys.includes("innodb_io_capacity") && keys.every(k => k.includes("innodb_io_capacity")); }, {}, `${list} tbody tr[data-row-key]`);
  await settle();
  await page.focus(`${list} tr[data-row-key="innodb_io_capacity"]`); await page.keyboard.press("Enter");
  await page.waitForSelector(`${list} [data-action="version-history"]`);
  await page.click(`${list} [data-action="version-history"]`);
  await page.waitForFunction(() => document.querySelector('#parameters-views-tab-versions')?.getAttribute("aria-selected") === "true");
  await page.waitForFunction(() => document.querySelector('[data-view="parameters-versions"] input[role="combobox"]')?.value === "innodb_io_capacity");
  await page.waitForFunction(() => document.querySelector('[data-view="parameters-versions"] .pv-history-table tr[data-version="8.4.0"]'));
  assert.deepEqual(reads("/api/config/versions/history").at(-1).body, { engine: "mysql", name: "innodb_io_capacity" });
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("role")), "combobox");

  // An error appears once beside the choice, with Try again; the last result stays greyed.
  await page.click(modeButton("compare"));
  await page.select(select("From"), "8.1.0");
  await page.waitForSelector(`${view} .action-status[data-phase="failed"]`);
  assert.equal(await page.$$eval(`${view} .action-status[data-phase="failed"]`, e => e.length), 1);
  assert.match(await page.$eval(`${view} .action-status[data-phase="failed"]`, e => e.textContent), /Could not compare MySQL 8\.1\.0.*Try again/);
  assert.ok(await page.$(`${view} .pv-result.is-stale`));
  await page.select(select("From"), "8.0.36");
  await waitCompare("8.0.36", "8.4.3");
  assert.equal(await page.$(`${view} .action-status[data-phase="failed"]`), null);

  // Another engine: MariaDB releases; the set-on-server filter needs a matching engine.
  await page.select(select("Engine"), "mariadb");
  await waitCompare("11.4.5", "11.8.3");
  assert.deepEqual(reads("/api/config/versions/compare").at(-1).body, { engine: "mariadb", from: "11.4.5", to: "11.8.3" });
  assert.equal(await page.$(`${view} .pv-filters label.checkbox`), null, "a MySQL server's set parameters do not filter MariaDB releases");
  await page.select(select("Engine"), "mysql");
  await waitCompare("8.0.36", "9.4.0");

  // Percona Server newer than the catalog: one line each, after its Parameters report is read.
  await page.click('nav[aria-label="Test controls"] button');
  await tab("list");
  for (let i = 0; i < 100 && !reads("/api/live/config").some(r => r.body.connection.host === "db2"); i++) await new Promise(r => setTimeout(r, 30));
  assert.ok(reads("/api/live/config").some(r => r.body.connection.host === "db2"), "Parameters reads db2 when it comes on screen");
  await page.waitForFunction(() => !document.querySelector('[data-view="config"][data-other-connection="true"]'));
  await tab("versions");
  await page.waitForFunction(() => document.querySelectorAll('[data-view="parameters-versions"] .pv-notes p').length === 2);
  const notes = await page.$$eval(`${view} .pv-notes p`, p => p.map(x => x.textContent));
  assert.deepEqual(notes, ["Percona Server uses the MySQL catalog. Variables Percona Server adds are not cataloged.",
    "Percona Server 9.9.0 on db2 is newer than the catalog, whose newest release is 9.4.0."]);
  assert.match(await page.$eval(`${view} .pv-filters label.checkbox`, e => e.textContent), /set on db2/);

  // Layout: no page overflow, tables scroll inside their frame; badge contrast in both appearances.
  for (const [width, height] of [[1440, 900], [720, 600]]) for (const theme of ["light", "dark"]) {
    await page.setViewport({ width, height });
    await page.evaluate(t => { document.documentElement.dataset.theme = t; window.scrollTo(0, 0); }, theme);
    for (const mode of ["compare", "history"]) {
      await page.click(modeButton(mode)); await settle();
      if (mode === "compare") await page.waitForSelector(`${view} tbody tr[data-row-key]`);
      const m = await page.evaluate(sel => {
        const root = document.querySelector(sel);
        const rows = [...root.querySelectorAll("tbody tr[data-row-key]")].filter(r => { const b = r.getBoundingClientRect(); return b.height > 0 && b.top >= 0 && b.bottom <= innerHeight; }).length;
        const color = s => (s.match(/[\d.]+/g) || []).map(Number);
        const blend = (f, b) => f.slice(0, 3).map((v, i) => v * (f[3] ?? 1) + b[i] * (1 - (f[3] ?? 1)));
        const lum = rgb => rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }).reduce((s, v, i) => s + v * [.2126, .7152, .0722][i], 0);
        const contrast = [...root.querySelectorAll("[data-comparison-status]")].filter(b => b.getBoundingClientRect().height > 0).map(b => {
          const chain = []; for (let e = b; e; e = e.parentElement) chain.unshift(e);
          const bg = chain.reduce((acc, e) => blend(color(getComputedStyle(e).backgroundColor), acc), [255, 255, 255]);
          const [x, y] = [lum(blend(color(getComputedStyle(b).color), bg)), lum(bg)].sort((a, c) => c - a);
          return (x + .05) / (y + .05);
        });
        const wrap = root.querySelector(".data-table-wrap");
        return { rows, overflow: document.documentElement.scrollWidth - innerWidth, main: root.querySelector(".view-main").scrollWidth - root.querySelector(".view-main").clientWidth,
          tableScroll: wrap && wrap.getClientRects().length ? wrap.scrollWidth - wrap.clientWidth : null,
          minContrast: contrast.length ? Math.min(...contrast) : null };
      }, view);
      console.log(`PVSTAT ${mode} ${width}x${height} ${theme}: ${JSON.stringify(m)}`);
      assert.equal(m.overflow, 0, `${mode} ${width} ${theme}: no page overflow`);
      assert.ok(m.main <= 1, `${mode} ${width} ${theme}: the view does not scroll sideways`);
      if (m.minContrast !== null) assert.ok(m.minContrast >= 4.5, `${mode} ${width} ${theme}: badge contrast ${m.minContrast}`);
      if (mode === "compare" && width === 1440) assert.ok(m.rows >= 10, `at least ten rows above the fold at 1440x900 (${m.rows})`);
      if (mode === "compare") assert.ok(m.tableScroll <= 1, `${width} ${theme}: every column fits the table frame (${m.tableScroll})`);
      if (shots) await page.screenshot({ path: path.join(shots, `suite-${mode}-${width}-${theme}.png`) });
    }
  }
  // At 720 the inspector is a sheet: Escape closes it and returns focus to the row.
  await page.click(modeButton("compare"));
  await page.select(select("From"), "8.0.36"); await page.select(select("To"), "8.4.3"); await waitCompare("8.0.36", "8.4.3");
  await page.type(`${view} input[aria-label="Filter parameters"]`, "innodb_io_capacity");
  await page.waitForSelector(`${view} tr[data-row-key="innodb_io_capacity"]`);
  await page.focus(`${view} tr[data-row-key="innodb_io_capacity"]`); await page.keyboard.press("Enter");
  await page.waitForSelector(`${view} .view-inspector.is-sheet .pv-facts`);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth), 0, "the sheet does not overflow at 720");
  if (shots) await page.screenshot({ path: path.join(shots, "suite-inspector-720-dark.png") });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector(".view-inspector.is-sheet"));
  await page.waitForFunction(() => document.activeElement?.dataset?.rowKey === "innodb_io_capacity");

  assert.equal(seen.filter(r => r.pathname.startsWith("/api/live/") && r.pathname !== "/api/live/config").length, 0, "nothing in Versions reads a server");
  assert.deepEqual(errors, []);
  console.log("parameter-versions browser: first-visit mount, kept Parameters state, defaults, Swap, busy and error states, kind counts, name and set-on-server filters, inspector and Escape, Show full history, Version history, suggestions, combobox and mode keyboard, Percona and newer-server notes, 1440/720 light/dark layout passed");
}

try {
  await validateContracts();
  if (process.argv.includes("--unit")) await unit();
  else { await unit(); if (shots) await fs.mkdir(shots, { recursive: true }); await browserSuite(); }
} catch (error) {
  if (shots && activePage) await activePage.screenshot({ path: path.join(shots, "suite-failure.png") }).catch(() => {});
  throw error;
} finally {
  await browser?.close();
  await fs.rm(temporary, { recursive: true, force: true });
}
