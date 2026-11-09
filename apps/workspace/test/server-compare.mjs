// Real components, real kit, production-generated replies. All HTTP requests
// are intercepted and Chromium uses a pipe: no loopback server or database.
// --unit validates fixtures and executes the typed API; --compile-only bundles
// the browser fixture without launching Chromium.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { build, transformWithEsbuild } from "vite";
import puppeteer from "puppeteer";

// Capture the real components at the desktop app's supported window sizes.
async function reviewCompare(page, screen, scope, strip) {
  const viewport = page.viewport();
  const theme = await page.evaluate(() => document.documentElement.dataset.theme);
  const directory = process.env.CMPSTAT_REVIEW ? path.join(process.env.CMPSTAT_REVIEW, screen) : null;
  if (directory) await fs.mkdir(directory, { recursive: true });
  for (const [width, height] of [[720, 600], [1440, 900]]) for (const appearance of ["light", "dark"]) {
    await page.setViewport({width, height});
    await page.evaluate(t => { document.documentElement.dataset.theme = t; document.querySelectorAll('*').forEach(e => { if (e.scrollTop) e.scrollTop = 0; }); }, appearance);
    await new Promise(resolve => setTimeout(resolve, 180));
    const measurement = await page.evaluate((scope, strip) => {
      const root = document.querySelector(scope);
      const visibleRows = [...root.querySelectorAll('tbody tr[data-row-key]')].filter(row => {
        const r = row.getBoundingClientRect(); let top = 0, bottom = innerHeight;
        for (let p = row.parentElement; p; p = p.parentElement) if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowY)) {
          const b = p.getBoundingClientRect(); top = Math.max(top, b.top); bottom = Math.min(bottom, b.bottom);
        }
        return r.height > 0 && r.top >= top && r.bottom <= bottom;
      }).length;
      // Resolve alpha backgrounds through the actual ancestor surfaces, then
      // test the rendered foreground against the result in both appearances.
      const color = s => (s.match(/[\d.]+/g) || []).map(Number);
      const blend = (front, back) => front.slice(0, 3).map((v, i) => v * (front[3] ?? 1) + back[i] * (1 - (front[3] ?? 1)));
      const lum = rgb => rgb.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }).reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
      const badges = [...root.querySelectorAll('[data-comparison-status]')].filter(b => b.getBoundingClientRect().height > 0).map(badge => {
        const ancestors = []; for (let e = badge; e; e = e.parentElement) ancestors.unshift(e);
        const background = ancestors.reduce((bg, e) => blend(color(getComputedStyle(e).backgroundColor), bg), [255, 255, 255]);
        const foreground = blend(color(getComputedStyle(badge).color), background);
        const [a, b] = [lum(foreground), lum(background)].sort((a, b) => b - a);
        return {status: badge.dataset.comparisonStatus, contrast: (a + .05) / (b + .05), health: /\bis-(ok|warn|danger)\b/.test(badge.className)};
      });
      return {badges, totalRows: root.querySelectorAll('tbody tr[data-row-key]').length, tabsY: Math.round(root.querySelector(strip).getBoundingClientRect().y), visibleRows,
        overflow: document.documentElement.scrollWidth - innerWidth};
    }, scope, strip);
    console.log(`CMPSTAT ${screen} ${width}x${height} ${appearance}: ${JSON.stringify({tabsY: measurement.tabsY, visibleRows: measurement.visibleRows, totalRows: measurement.totalRows, overflow: measurement.overflow, minContrast: Math.min(...measurement.badges.map(b => b.contrast))})}`);
    assert.equal(measurement.overflow, 0, `${screen} ${width} ${appearance}: no page overflow`);
    assert.ok(measurement.tabsY < 200, `${screen} ${width}: differences start at ${measurement.tabsY}px`);
    assert.ok(measurement.visibleRows >= Math.min(6, measurement.totalRows), `${screen} ${width}: at least six rows, or the entire fixture`);
    assert.ok(measurement.badges.length > 0);
    for (const badge of measurement.badges) {
      assert.equal(badge.health, false, `${badge.status} never uses health classes`);
      assert.ok(badge.contrast >= 4.5, `${badge.status} ${appearance} contrast ${badge.contrast}`);
    }
    if (directory) await page.screenshot({path: path.join(directory, `${width}-${appearance}.png`)});
  }
  await page.setViewport(viewport);
  await page.evaluate(t => { if(t) document.documentElement.dataset.theme=t; else delete document.documentElement.dataset.theme; }, theme);
}

const workspace = fileURLToPath(new URL("../", import.meta.url));
const root = path.resolve(workspace, "../..");
const source = name => JSON.stringify(path.join(workspace, "src", name));
const fixturePath = path.join(workspace, "test/fixtures/server-compare.json");
const fixture = JSON.parse(await fs.readFile(fixturePath, "utf8"));
const temporary = await fs.mkdtemp(path.join(workspace, ".server-compare-test-"));
let browser;
try {
  const checked = spawnSync(process.env.PYTHON || "python3", ["-c", `
import json,sys
from pathlib import Path
sys.path.insert(0,'test')
from test_chart_data import validate
f=json.loads(Path(sys.argv[1]).read_text())
for key,schema in [('parameters','parameter-compare-1.0'),('side_error','parameter-compare-1.0'),('accounts','account-compare-1.0'),('accounts_side_error','account-compare-1.0'),('snapshot_list','parameter-snapshot-list-1.0'),('snapshot','parameter-snapshot-1.0')]:
    validate(f[key],json.loads(Path('docs/schemas/'+schema+'.json').read_text()))
assert 'IDENTIFIED' not in f['export']['text']
`, fixturePath], { cwd: root, env: { ...process.env, HOME: temporary, USERPROFILE: temporary, MYFLAMES_SECRET_BACKEND: "vault", PYTHONDONTWRITEBYTECODE: "1" }, encoding: "utf8" });
  assert.equal(checked.status, 0, checked.stderr);
  if (process.argv.includes("--unit")) {
    const compiled = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
      build: { ssr: true, write: false, rollupOptions: { input: path.join(workspace, "src/compareApi.ts"), output: { inlineDynamicImports: true } } } });
    const api = await import("data:text/javascript;base64," + Buffer.from(compiled.output.find(o => o.type === "chunk").code).toString("base64"));
    assert.equal(api.exportAccountName({ name: "a@b`c", host: "d@e" }), "`a@b``c`@`d@e`");
    const shared = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
      build: { ssr: true, write: false, rollupOptions: { input: path.join(workspace, "src/compareShared.tsx"), output: { inlineDynamicImports: true } } } });
    const sharedPath = path.join(temporary, "compare-shared.mjs");
    await fs.writeFile(sharedPath, shared.output.find(o => o.type === "chunk").code);
    const { ComparisonBadge, ComparisonSetup } = await import(pathToFileURL(sharedPath).href);
    for (const [status, text, tone] of [["different", "Different", "accent"], ["only_in_source", "Only in source", "outline"],
      ["only_in_target", "Only in target", "outline"], ["not_compared", "Not compared", "muted"], ["identical", "Identical", "faint"], ["moved", "Moved", "outline"]]) {
      const html = renderToStaticMarkup(createElement(ComparisonBadge, {status}));
      assert.ok(html.includes(text) && html.includes(`comparison-status--${tone}`));
      assert.doesNotMatch(html, /is-(ok|warn|danger)/);
    }
    const props = {className: "test-compare", runClassName: "test-run", source: "v1", target: "db2", contextText: "Last compared: Source v1; Target db2",
      onExpanded: () => {}, onSwap: () => {}, busy: false, problem: "", onCompare: () => {}, children: createElement("fieldset", null, "Pick sides")};
    const initial = renderToStaticMarkup(createElement(ComparisonSetup, {...props, hasResult: false, expanded: false}));
    assert.ok(initial.includes("Pick sides")); assert.ok(!initial.includes("comparison-header"));
    const collapsed = renderToStaticMarkup(createElement(ComparisonSetup, {...props, hasResult: true, expanded: false}));
    assert.ok(!collapsed.includes("Pick sides")); assert.match(collapsed, /aria-expanded="false"/);
    assert.ok(collapsed.includes("Swap") && collapsed.includes("Change…") && collapsed.includes("Compare") && collapsed.includes("Last compared"));
    const expanded = renderToStaticMarkup(createElement(ComparisonSetup, {...props, hasResult: true, expanded: true}));
    assert.ok(expanded.includes("Pick sides")); assert.match(expanded, /aria-expanded="true"/);
    const seen = [];
    globalThis.fetch = async (url, init) => {
      if (url === "/api/auth/session") return { ok: true, json: async () => ({ token: "fixture" }) };
      seen.push({ url, body: JSON.parse(init.body), signal: init.signal });
      return { ok: true, json: async () => url.endsWith("export") ? fixture.export : fixture.parameters };
    };
    assert.match(api.comparisonSummary(fixture.parameters.counts), /different.*only in source.*only in target.*not compared.*identical/);
    assert.match(api.accountDifferenceText(fixture.accounts.items.find(r => r.name === "app")), /1 grant only in source.*1 grant only in target.*Roles differ/);
    assert.equal(api.displayValue(""), ""); assert.equal(api.displayValue(null), "Unknown"); assert.equal(api.displayValue(false), "false");
    // Account settings read as words, never "{}" or a JSON dump.
    assert.equal(api.recordText({}), "None"); assert.equal(api.recordText(null), "None");
    assert.equal(api.recordText({ MAX_QUERIES_PER_HOUR: 10, failed_login_attempts: 3 }), "MAX_QUERIES_PER_HOUR: 10\nFailed login attempts: 3");
    assert.equal(api.tlsText({ type: "", cipher_hex: "", issuer_hex: "", subject_hex: "" }), "None");
    assert.equal(api.tlsText({ type: "SPECIFIED", cipher_hex: "414553", issuer_hex: "", subject_hex: "" }), "SPECIFIED\nCipher (hex): 414553");
    // The quick export choice leaves built-in system accounts out.
    assert.deepEqual(api.onlyInSourceForExport(fixture.accounts.items).map(r => r.account), ["source_only@%"]);
    assert.ok(fixture.accounts.items.some(r => r.system && r.status === "only_in_source"), "the fixture keeps a system account only in the source");
    const signal = new AbortController().signal;
    const src = { profile: "saved-db" }, dst = { snapshot: fixture.snapshot_list.items[0].id };
    await api.compareParameters(src, dst, signal);
    assert.deepEqual(seen.at(-1), { url: "/api/config/compare", body: { source: src, target: dst }, signal });
    await api.compareAccounts(src, { live: { connection: { host: "db2" } } }, signal);
    assert.equal(seen.at(-1).url, "/api/accounts/compare");
    await api.exportGrants(src, ["source_only@%"], signal);
    assert.deepEqual(seen.at(-1).body, { side: src, accounts: ["source_only@%"] });
    await api.listSnapshots(signal, '["db1",3306]'); assert.equal(seen.at(-1).body.source_key, '["db1",3306]');
    assert.ok(seen.every(r => !JSON.stringify(r.body).includes("password")), "saved connections are profile selectors, never copied passwords");
    console.log("server-compare unit: shared badges/setup, schema fixtures, vocabulary, unknown/raw values and exact API selectors passed");
  } else {
    const entry = "\0server-compare-fixture.tsx";
    const compiled = await build({ root: workspace, configFile: false, publicDir: false, logLevel: "error",
      plugins: [{ name: "server-compare-fixture", resolveId(id) { if (id === entry) return id; }, async load(id) {
        if (id !== entry) return null;
        const code = `
          import React, {useState} from ${JSON.stringify(path.join(workspace, "node_modules/react/index.js"))};
          import {createRoot} from ${JSON.stringify(path.join(workspace, "node_modules/react-dom/client.js"))};
          import {ConfigView} from ${source("Config.tsx")}; import {AccountsView} from ${source("Accounts.tsx")};
          import {InspectorContext} from ${source("kit.tsx")}; import {ViewOnScreenContext} from ${source("viewOnScreen.ts")};
          import ${source("styles.ts")};
          function Panel({kind,connection,visible}) {
            const [report,setReport]=useState(null), [selected,setSelected]=useState(''), [open,setOpen]=useState(false);
            const View=kind==='parameters'?ConfigView:AccountsView;
            return <ViewOnScreenContext.Provider value={visible}><div hidden={!visible}>
              <InspectorContext.Provider value={{open,setOpen,width:400,setWidth:()=>{},sheet:false}}>
                <View connection={connection} disabled={false} report={report} onReport={setReport}
                  selection={{key:selected,select:(key,open)=>{setSelected(key);if(open)setOpen(true);}}}/>
              </InspectorContext.Provider></div></ViewOnScreenContext.Provider>;
          }
          function Fixture() {
            const [kind,setKind]=useState('parameters'),[visible,setVisible]=useState(true),[host,setHost]=useState('db1');
            const connection={name:host,host,port:3306,user:'operator',database:'',ssl_mode:'DISABLED'};
            return <><nav aria-label="Test controls"><button onClick={()=>setKind('parameters')}>Parameters screen</button><button onClick={()=>setKind('accounts')}>Accounts screen</button>
              <button onClick={()=>setHost(h=>h==='db1'?'db2':'db1')}>Switch connection</button><button onClick={()=>setVisible(v=>!v)}>Toggle screen</button></nav>
              <Panel kind="parameters" connection={connection} visible={visible&&kind==='parameters'}/><Panel kind="accounts" connection={connection} visible={visible&&kind==='accounts'}/></>;
          }
          createRoot(document.getElementById('root')).render(<Fixture/>);
        `;
        return (await transformWithEsbuild(code, "fixture.tsx", { loader: "tsx", jsx: "automatic" })).code;
      } }], build: { write: false, target: "es2020", rollupOptions: { input: entry, output: { format: "iife", inlineDynamicImports: true } } } });
    if (process.argv.includes("--compile-only")) console.log("server-compare browser fixture compiled; schema fixtures validated");
    else {
      browser = await puppeteer.launch({ headless: "shell", pipe: true, userDataDir: path.join(temporary, "chromium"), env: { ...process.env, HOME: temporary, USERPROFILE: temporary } });
      const page = await browser.newPage();
      const errors = []; page.on("pageerror", error => errors.push(String(error)));
      const seen = []; let sideError = false, compareDelay = 0;
      let snapshots = structuredClone(fixture.snapshot_list.items);
      const css = compiled.output.filter(o => o.type === "asset" && o.fileName.endsWith(".css")).map(o => o.source).join("\n");
      const js = compiled.output.find(o => o.type === "chunk").code;
      const config = JSON.parse(await fs.readFile(path.join(workspace, "test/fixtures/parameters-mysql.json"), "utf8"));
      const profiles = [{ id: "saved-db", name: "Saved target", host: "db2", port: 3306, user: "operator", database: "", ssl_mode: "DISABLED", has_saved_password: true }];
      await page.setRequestInterception(true);
      page.on("request", request => {
        const pathname = new URL(request.url()).pathname;
        const respond = (body, type = "application/json") => request.respond({ status: 200, contentType: `${type}; charset=utf-8`, body: type === "application/json" ? JSON.stringify(body) : body }).catch(() => {});
        if (pathname === "/") return void respond('<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script src="/fixture.js"></script></body></html>', "text/html");
        if (pathname === "/favicon.ico") return void respond("", "text/plain");
        if (pathname === "/fixture.js") return void respond(js, "text/javascript");
        if (pathname === "/fixture.css") return void respond(css, "text/css");
        const body = JSON.parse(request.postData() || "{}"); seen.push({ pathname, body });
        if (pathname === "/api/auth/session") return void respond({ token: "fixture" });
        if (pathname === "/api/live/config") return void respond(config);
        if (pathname === "/api/accounts/list") return void respond({ schema_version: "accounts-1.0", engine: "mysql", version: "8.4.0", complete: true, accounts: [], role_grants: [], default_roles: [], warnings: [] });
        if (pathname === "/api/profiles/list") return void respond({ items: profiles });
        if (pathname === "/api/config/snapshots/list") return void respond({ schema_version: "parameter-snapshot-list-1.0", items: snapshots });
        if (pathname === "/api/config/snapshots/save") { const saved = { ...snapshots[0], id: "22222222-2222-4222-8222-222222222222", number: 2, label: body.label }; snapshots.unshift(saved); return void respond(saved); }
        if (pathname === "/api/config/snapshots/delete") { snapshots = snapshots.filter(s => s.id !== body.id); return void respond({ deleted: true, id: body.id }); }
        if (pathname === "/api/config/compare" || pathname === "/api/accounts/compare") {
          const data = pathname.includes("config") ? (sideError ? fixture.side_error : fixture.parameters) : (sideError ? fixture.accounts_side_error : fixture.accounts);
          if (compareDelay) return void setTimeout(() => void respond(data), compareDelay);
          return void respond(data);
        }
        if (pathname === "/api/accounts/export") return void respond(fixture.export);
        errors.push(`Unexpected request: ${pathname}`); void request.abort();
      });
      await page.setViewport({ width: 1440, height: 1000 });
      await page.goto("https://compare.invalid/", { waitUntil: "networkidle0" });
      const click = (label, scope = "") => page.locator(`${scope}button ::-p-text(${label})`).click();
      const panel = kind => `.server-view[data-view="${kind}-compare"]`;
      const compare = kind => page.click(`${panel(kind)} .server-compare-run button`);
      const waitReport = kind => page.waitForSelector(`${panel(kind)} .comparison-header`, { visible: true });
      const change = kind => `${panel(kind)} .comparison-actions button[aria-expanded]`;
      const checkSetup = async kind => {
        assert.equal(await page.$(`${panel(kind)} fieldset`), null, "successful Compare collapses setup");
        const reads = seen.filter(r => r.pathname.endsWith("/compare")).length;
        const named = await page.$eval(`${panel(kind)} .comparison-names`, e => e.textContent);
        await page.click(change(kind));
        await page.waitForSelector(`${panel(kind)} select[aria-label="Source"]`);
        const before = await page.$$eval(`${panel(kind)} .server-compare-bar select`, e => e.map(s => s.value));
        await page.click(`${panel(kind)} [aria-label="Swap source and target"]`);
        assert.deepEqual(await page.$$eval(`${panel(kind)} .server-compare-bar select`, e => e.map(s => s.value)), [...before].reverse());
        // The bar keeps naming the sides the result came from; the note names the next ones.
        assert.equal(await page.$eval(`${panel(kind)} .comparison-names`, e => e.textContent), named);
        assert.match(await page.$eval(`${panel(kind)} .server-compare-note`, e => e.textContent), /chosen sides changed \(next: Source .+ → Target .+\)/);
        await page.focus(`${panel(kind)} select[aria-label="Source"]`); await page.keyboard.press("Escape");
        assert.equal(await page.$(`${panel(kind)} fieldset`), null);
        assert.equal(await page.$eval(change(kind), e => document.activeElement === e), true);
        await page.click(`${panel(kind)} [aria-label="Swap source and target"]`);
        await page.click(change(kind)); await page.click(change(kind));
        assert.equal(await page.$(`${panel(kind)} fieldset`), null, "second click collapses");
        assert.equal(seen.filter(r => r.pathname.endsWith("/compare")).length, reads, "changing the setup reads no server");
      };
      const check = async (label, kind = "parameters") => {
        await page.$$eval(`${panel(kind)} label.checkbox`, (labels, text) => labels.find(e => e.textContent.trim() === text)?.querySelector("input").click(), label);
      };
      await page.waitForSelector('[data-view="config"] [data-row-key]');
      assert.equal(seen.filter(r => r.pathname.includes("/compare")).length, 0);
      await click("Save snapshot…", '[data-view="config"] ');
      await page.type('input[aria-label="Snapshot label"]', "Before maintenance");
      await page.click('dialog .sheet-buttons .primary');
      await page.waitForFunction(() => document.querySelector('[data-view="config"]')?.textContent.includes("Saved snapshot #2"));
      await click("Compare", '[data-view="config"] .segmented ');
      await page.waitForFunction(() => document.querySelector('[data-view="parameters-compare"] select[aria-label="Target"]')?.options.length >= 5);
      const choices = await page.$$eval(`${panel("parameters")} select[aria-label="Target"] option`, e => e.map(o => o.textContent));
      assert.ok(choices.some(t => t.includes("Saved target"))); assert.ok(choices.some(t => t.includes("#2") && t.includes("Before maintenance")));
      assert.ok(choices.some(t => t.includes("Latest snapshot of db1:3306")));
      // Target is pre-filled with the latest snapshot of the server in use; nothing has to be typed or read.
      await page.waitForFunction(key => document.querySelector('[data-view="parameters-compare"] select[aria-label="Target"]').value === `latest:${key}`, {}, fixture.snapshot_list.items[0].source_key);
      await page.select(`${panel("parameters")} select[aria-label="Target"]`, `snapshot:${fixture.snapshot_list.items[0].id}`);
      const sides = () => page.$$eval(`${panel("parameters")} .server-compare-bar select`, e => e.map(s => s.value));
      await page.click(`${panel("parameters")} button[aria-label="Swap source and target"]`);
      await page.waitForFunction(id => document.querySelector('[data-view="parameters-compare"] select[aria-label="Source"]').value === id, {}, `snapshot:${fixture.snapshot_list.items[0].id}`);
      assert.deepEqual(await sides(), [`snapshot:${fixture.snapshot_list.items[0].id}`, "current"]);
      await page.click(`${panel("parameters")} button[aria-label="Swap source and target"]`);
      await page.waitForFunction(() => document.querySelector('[data-view="parameters-compare"] select[aria-label="Source"]').value === "current");
      assert.equal(seen.filter(r => r.pathname.endsWith("/compare")).length, 0, "choosing and swapping sides reads nothing");
      await compare("parameters"); await waitReport("parameters");
      assert.match(await page.$eval(`${panel("parameters")} [aria-label="Compared servers"]`, e => e.textContent), /Source.*db1:3306.*read.*Target.*Snapshot #1 \(“Before upgrade”\) of db1:3306.*captured/s);
      await checkSetup("parameters");
      await reviewCompare(page, "parameters", panel("parameters"), ".server-compare-filters");
      assert.deepEqual(seen.find(r => r.pathname === "/api/config/compare").body.target, { snapshot: fixture.snapshot_list.items[0].id });
      assert.equal(await page.$(`${panel("parameters")} [data-row-key="server_id"]`), null);
      assert.equal(await page.$(`${panel("parameters")} [data-row-key="same_setting"]`), null);
      await check("Show identical"); await page.waitForSelector(`${panel("parameters")} [data-row-key="same_setting"]`);
      await check("Show server-specific settings"); await page.waitForSelector(`${panel("parameters")} [data-row-key="server_id"]`);
      await page.type(`${panel("parameters")} input[aria-label="Filter differences"]`, "max_connections");
      await page.waitForFunction(() => document.querySelectorAll('[data-view="parameters-compare"] tbody [data-row-key]').length === 1);
      await page.focus(`${panel("parameters")} [data-row-key="max_connections"]`); await page.keyboard.press("Enter");
      await page.waitForSelector(`${panel("parameters")} .detail-back`);
      const factRows = () => page.$$eval(`${panel("parameters")} .view-inspector .server-compare-table tbody tr`, rows => rows.map(r => [...r.children].map(c => c.textContent.trim())));
      const facts = Object.fromEntries((await factRows()).map(([name, ...sides]) => [name, sides]));
      assert.deepEqual(facts["Raw valueDifferent"], ["200", "300"], "both raw values side by side, the row marked Different in words");
      assert.deepEqual(facts.Default, ["151", "151"]);
      assert.deepEqual(facts["At default"], ["No", "No"]);
      assert.deepEqual(await page.$$eval(`${panel("parameters")} .view-inspector .server-compare-table thead th`, e => e.map(h => h.textContent)), ["Setting", "Source", "Target"]);
      // Source and target sit side by side over the whole view, not stacked.
      assert.ok(await page.$eval(`${panel("parameters")} .view-inspector .server-compare-table`, t => t.getBoundingClientRect().width > 900));
      await page.click(`${panel("parameters")} .detail-back`);
      await page.waitForFunction(() => document.querySelector('[data-view="parameters-compare"] [data-row-key="max_connections"]') === document.activeElement);
      assert.equal(await page.$eval(`${panel("parameters")} input[aria-label="Filter differences"]`, e => e.value), "max_connections");
      await page.$eval(`${panel("parameters")} input[aria-label="Filter differences"]`, e => { const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set; setter.call(e, ""); e.dispatchEvent(new Event("input", {bubbles:true})); });
      await page.waitForSelector(`${panel("parameters")} [data-row-key="redacted_setting"]`);
      await page.focus(`${panel("parameters")} [data-row-key="redacted_setting"]`); await page.keyboard.press("Enter");
      await page.waitForSelector(`${panel("parameters")} .detail-back`);
      assert.match(await page.$eval(`${panel("parameters")} .view-inspector`, e => e.textContent), /redact/i);
      await page.click(`${panel("parameters")} .detail-back`);
      sideError = true; await compare("parameters");
      await page.waitForSelector(`${panel("parameters")} .server-compare-error`);
      assert.equal(await page.$$eval(`${panel("parameters")} .server-compare-error`, e => e.length), 1);
      assert.match(await page.$eval(`${panel("parameters")} .server-compare-error`, e => e.textContent), /Access denied on target/);
      sideError = false; await compare("parameters");
      await page.waitForFunction(() => !document.querySelector('[data-view="parameters-compare"] .server-compare-error'));
      await page.click(change("parameters"));
      await page.select(`${panel("parameters")} select[aria-label="Target"]`, `latest:${fixture.snapshot_list.items[0].source_key}`);
      await compare("parameters");
      await page.waitForFunction(() => !document.querySelector('[data-view="parameters-compare"] .server-compare-note[role="status"]'));
      assert.deepEqual(seen.filter(r => r.pathname === "/api/config/compare").at(-1).body.target, { snapshot: "22222222-2222-4222-8222-222222222222" });
      await page.click(change("parameters"));
      await click("Manage snapshots", `${panel("parameters")} `);
      await page.click(`${panel("parameters")} button[aria-label="Delete snapshot #2 from db1:3306"]`);
      assert.equal(seen.filter(r => r.pathname === "/api/config/snapshots/delete").length, 0, "deletion waits for confirmation");
      await page.click('dialog .sheet-buttons .danger-solid');
      await page.waitForFunction(() => !document.querySelector('dialog'));
      assert.equal(seen.filter(r => r.pathname === "/api/config/snapshots/delete").length, 1);
      await click("Manage snapshots", `${panel("parameters")} `);
      await page.click(change("parameters"));
      await click("Switch connection", 'nav[aria-label="Test controls"] ');
      await page.waitForSelector(`${panel("parameters")}[data-other-connection="true"]`);
      assert.match(await page.$eval(panel("parameters"), e => e.textContent), /From .*db1/);
      // The old result is greyed; the bar that compares again is not.
      const look = sel => page.$eval(sel, e => ({ opacity: Number(getComputedStyle(e).opacity), filter: getComputedStyle(e).filter }));
      assert.ok((await look(`${panel("parameters")} .server-compare-result`)).opacity < 1, "the result from db1 is greyed");
      assert.deepEqual(await look(`${panel("parameters")} .server-compare-content`), { opacity: 1, filter: "none" }, "Source, Target and Compare stay live");
      assert.ok(await page.$(`${panel("parameters")} .gate.other-connection button`), "the note offers Compare");
      const before = seen.length; await click("Toggle screen", 'nav[aria-label="Test controls"] ');
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(seen.length, before, "hiding causes no reads");
      await click("Toggle screen", 'nav[aria-label="Test controls"] ');
      compareDelay = 1000; await compare("parameters"); await page.waitForSelector(`${panel("parameters")} .action-status[data-phase="pending"] button`);
      await click("Stop", `${panel("parameters")} .action-status `); compareDelay = 0;
      await page.waitForFunction(() => document.querySelector('[data-view="parameters-compare"]').textContent.includes("Comparison stopped"));
      assert.ok(await page.$(`${panel("parameters")} [aria-label="Compared servers"]`), "Stop keeps the result on screen");
      await click("Accounts screen", 'nav[aria-label="Test controls"] ');
      await page.waitForSelector('[data-view="accounts"]', { visible: true });
      await click("Compare", '[data-view="accounts"] .segmented ');
      await page.waitForSelector(`${panel("accounts")} select[aria-label="Target"] option[value="profile:saved-db"]`);
      await page.waitForFunction(() => document.querySelector('[data-view="accounts-compare"] select[aria-label="Target"]').value === "profile:saved-db");
      await page.select(`${panel("accounts")} select[aria-label="Target"]`, "profile:saved-db");
      sideError = true; await compare("accounts"); await waitReport("accounts");
      assert.equal(await page.$$eval(`${panel("accounts")} .server-compare-error`, e => e.length), 1);
      sideError = false; await compare("accounts");
      await page.waitForFunction(() => !document.querySelector('[data-view="accounts-compare"] .server-compare-error'));
      await checkSetup("accounts");
      await reviewCompare(page, "accounts", panel("accounts"), ".server-compare-filters");
      assert.deepEqual(seen.find(r => r.pathname === "/api/accounts/compare").body.target, { profile: "saved-db" });
      assert.match(await page.$eval(panel("accounts"), e => e.textContent), /1 grant only in source/);
      await check("Show identical", "accounts");
      assert.match(await page.$eval(`${panel("accounts")} [data-row-key='["mysql.sys","localhost"]']`, e => e.textContent), /mysql\.sys@localhost.*System.*Only in source/s);
      await page.focus(`${panel("accounts")} [data-row-key='["source_only","%"]'] input[type="checkbox"]`); await page.keyboard.press("Space");
      await page.waitForFunction(() => document.querySelector(`[data-view="accounts-compare"] [data-row-key='["source_only","%"]'] input`).checked);
      assert.equal(await page.$(`${panel("accounts")} .detail-back`), null, "Space on a checkbox chooses the account, it does not open the detail");
      await page.keyboard.press("Space");
      await page.waitForFunction(() => !document.querySelector(`[data-view="accounts-compare"] [data-row-key='["source_only","%"]'] input`).checked);
      await page.click(`${panel("accounts")} tbody input[type="checkbox"]`);
      assert.equal(await page.$(`${panel("accounts")} .detail-back`), null, "choosing an export account keeps the table open");
      await page.$$eval(`${panel("accounts")} [data-row-key]`, rows => rows.find(r => r.textContent.includes("app@%"))?.focus()); await page.keyboard.press("Enter");
      await page.waitForSelector(`${panel("accounts")} .detail-back`);
      assert.match(await page.$eval(`${panel("accounts")} .view-inspector`, e => e.textContent), /Only in source.*INSERT.*Only in target.*UPDATE.*In both.*SELECT.*Roles.*reader.*Plugin.*caching_sha2_password.*TLS/s);
      const accountFacts = Object.fromEntries((await page.$$eval(`${panel("accounts")} .view-inspector .server-compare-table tbody tr`, rows => rows.map(r => [...r.children].map(c => c.textContent.trim())))).map(([name, ...v]) => [name, v]));
      assert.deepEqual(accountFacts.LockedDifferent, ["No", "Yes"]);
      assert.deepEqual(accountFacts.TLSDifferent, ["ANY", "None"]);
      assert.deepEqual(accountFacts.Limits, ["None", "None"], "an empty limit record reads None, never {}");
      assert.doesNotMatch(await page.$eval(`${panel("accounts")} .view-inspector`, e => e.textContent), /password_hash|authentication_string|IDENTIFIED/i);
      await page.click(`${panel("accounts")} .detail-back`);
      await click("All accounts only in source", `${panel("accounts")} `);
      await click("Export grants as SQL", `${panel("accounts")} `);
      await page.waitForSelector(`${panel("accounts")} [aria-label="SQL export"]`);
      assert.deepEqual(seen.find(r => r.pathname === "/api/accounts/export").body.accounts, ["`source_only`@`%`"], "the quick choice leaves mysql.sys and app@% out");
      assert.equal(await page.$eval(`${panel("accounts")} [data-row-key='["mysql.sys","localhost"]'] input`, e => e.checked), false);
      assert.equal(await page.$eval(`${panel("accounts")} .server-compare-choice`, e => e.textContent), "Chose 1 account only in the source; 1 system account left out.");
      assert.match(await page.$eval(`${panel("accounts")} .server-compare-export`, e => e.textContent), /Passwords are not included/);
      await page.evaluate(() => { window.__copied = ""; Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async text => { window.__copied = text; } } }); });
      await click("Copy", `${panel("accounts")} .server-compare-export-tools `);
      await page.waitForFunction(() => window.__copied?.includes("CREATE USER"));
      await page.evaluate(() => { window.__download = ""; const click = HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click = function() { if (this.download) window.__download = this.download; else click.call(this); }; });
      await click("Download .sql", `${panel("accounts")} `);
      assert.equal(await page.evaluate(() => window.__download), "account-grants.sql");
      await click("Switch connection", 'nav[aria-label="Test controls"] '); await page.waitForSelector(`${panel("accounts")}[data-other-connection="true"]`);
      for (const width of [1440, 720]) for (const theme of ["light", "dark"]) {
        await page.setViewport({ width, height: 1000 });
        await page.evaluate(t => { document.documentElement.dataset.theme = t; }, theme);
        for (const kind of ["accounts", "parameters"]) {
          await click(kind === "accounts" ? "Accounts screen" : "Parameters screen", 'nav[aria-label="Test controls"] ');
          await page.screenshot({ path: path.join(temporary, `${kind}-${width}-${theme}.png`), fullPage: true });
          assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, `${kind} ${width} ${theme} page overflow`);
          assert.equal(await page.$eval(`${panel(kind)} .view-main`, e => e.scrollWidth > e.clientWidth + 1), false, `${kind} ${width} ${theme} compare pane overflow`);
        }
      }
      assert.deepEqual(errors, []);
      console.log("server-compare browser: pickers, snapshot save, filters, keyboard details, side error, Stop, connection binding, export Copy/Download, light/dark and 720px overflow passed");
      if (process.env.COMPARE_SHOTS) {
        await fs.mkdir(process.env.COMPARE_SHOTS, { recursive: true });
        for (const name of await fs.readdir(temporary)) if (name.endsWith(".png")) await fs.copyFile(path.join(temporary, name), path.join(process.env.COMPARE_SHOTS, name));
      }
    }
  }
} finally {
  await browser?.close();
  await fs.rm(temporary, { recursive: true, force: true });
}
