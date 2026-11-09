// Schema versions place (SchemaVersions.tsx) on mocked /api/schema-versions/*
// endpoints: the saved list and its Database choice (the server's databases),
// Save version (sheet, database choice, same-as line), the inspector's objects
// and definitions, Rename and Delete, Compare (source and target filled in,
// request sides, Ignore definers, differences, a difference over the whole view
// with both definitions side by side, errors with Try again), the Sync script
// (review list, Open in Query editor only when the target is a database read
// now through the server in use) and a narrow window. The payloads are made by the Python package from
// the recorded snapshots in test/fixtures/schema_versions, so the suite reads
// what the backend really sends. No server is contacted.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { open, pause } from "./perf-harness.mjs";
import { chooseConnection } from "./chrome-helpers.mjs";

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

const root = fileURLToPath(new URL("../../../", import.meta.url));
const fixturePython = `
import json, sys
sys.path.insert(0, 'test')
import _schema_replay as r
from myflames import schema_versions as sv
from myflames import schema_lint
chosen = json.loads(sys.argv[1]) if len(sys.argv) > 1 else None
dependency_mode = len(sys.argv) > 2 and sys.argv[2] == 'dependencies'
before, after = r.snapshot('mysql-8.4', 'before'), r.snapshot('mysql-8.4', 'after')
for s in (before, after):
    s['server']['host'], s['server']['port'] = '127.0.0.1', 3306
    # A predictable snapshot-only rebuild, alongside the replay's unknown
    # hidden-fact cases. No fabricated live table facts are supplied.
    note = next(o for o in s['objects'] if o['type'] == 'table' and o['name'] == 'notes')
    note['ddl'] += ' ROW_FORMAT=' + ('COMPACT' if s is before else 'DYNAMIC')
before, after = sv.validate_snapshot(before), sv.validate_snapshot(after)
def item(snap, number, key, name='', same=None):
    out = {'id': key, 'saved_at': '2026-10-26T20:00:0%dZ' % number, 'source_key': sv.source_key(snap), 'number': number,
           'name': name, 'note': ''}
    out.update(sv.version_summary(snap))
    if same is not None:
        out['same_as'] = same
    return out
v1 = item(before, 1, 'a' * 32, 'before release 42')
v2 = item(before, 2, 'b' * 32, '', {'id': v1['id'], 'number': 1, 'name': v1['name']})
# Source: the saved version; target: the database now (the sync script would
# bring the database back to the version).
report = sv.compare(before, after, {'kind': 'version', 'id': v1['id'], 'number': 1, 'name': v1['name']}, {'kind': 'live'}, chosen_keys=None if dependency_mode else chosen)
import copy
# A separate, explicit lint fixture exercises a positive finding as well as
# the unmodified snapshot's not-judged details; both use Python's own words.
lint_watch = copy.deepcopy(before)
next(o for o in lint_watch['objects'] if o['type'] == 'table')['ddl'] = next(o for o in lint_watch['objects'] if o['type'] == 'table')['ddl'].replace('ENGINE=InnoDB', 'ENGINE=MyISAM')
elsewhere = copy.deepcopy(after)
elsewhere['server']['host'] = 'db9'
other = sv.compare(before, elsewhere, {'kind': 'version', 'id': v1['id'], 'number': 1}, {'kind': 'live'})
saved_target = sv.compare(after, before, {'kind': 'live'}, {'kind': 'version', 'id': v1['id'], 'number': 1})
# The same address read through SSH host bastion-a: another server than
# 127.0.0.1:3306 read directly or through bastion-b.
through_a = copy.deepcopy(after)
through_a['server']['ssh_host'] = 'bastion-a'
via_a = sv.compare(before, through_a, {'kind': 'version', 'id': v1['id'], 'number': 1}, {'kind': 'live'})
saved_a = copy.deepcopy(before)
saved_a['server']['ssh_host'] = 'bastion-a'
v3 = item(saved_a, 1, 'c' * 32, 'through bastion-a')
dep_source, dep_target = r.retained_child_pair('mysql-8.4')
blocked = sv.compare(dep_source, dep_target, {'kind': 'version'}, {'kind': 'live'}, chosen_keys=chosen if dependency_mode else None)
for i, result in enumerate((report, other, saved_target, blocked, via_a)):
    # The API, rather than the comparison engine, assigns this opaque cache id.
    result['comparison_id'] = 'browser-comparison-%d' % i
    result['markdown'] = sv.format_markdown(result)
print(json.dumps({'v1': v1, 'v2': v2, 'v3': v3, 'via_a': via_a, 'snapshot': before, 'report': report, 'other': other, 'saved_target': saved_target, 'blocked': blocked,
                 'lint': schema_lint.lint(before), 'lint_watch': schema_lint.lint(lint_watch)}))
`;
const fixtures = (keys, dependencies = false) => JSON.parse(execFileSync("python3", ["-c", fixturePython,
  ...(keys ? [JSON.stringify(keys), ...(dependencies ? ["dependencies"] : [])] : [])], { cwd: root }).toString());
const made = fixtures();

const catalog = { schema_version: "schemas-1.0", engine: "mysql", server_version: "8.4.11", user_tables: 14, all_limit: 2000, all_note: "", system: [],
  schemas: [{ name: "sv_b", tables: 7, system: false }, { name: "sv_m", tables: 7, system: false }, { name: "mysql", tables: 38, system: true }] };
let versions = [];
let saves = 0;
const replies = { sync: [], lint: [] };
// Saved connections to the same address, 127.0.0.1:3306, through two SSH
// hosts and directly. Without saved passwords: Compare never reads through
// them, and the connection in use stays the default until step 12.
const sshProfile = (id, name, ssh) => ({ id, name, host: "127.0.0.1", port: 3306, user: "root", database: "", ssl_mode: "PREFERRED",
  ssl_ca: "", ...(ssh ? { ssh_host: ssh } : {}), has_saved_password: false, secret_backend: "", secret_backend_label: "" });
const sshProfiles = [sshProfile("via-a", "Via A", "bastion-a"), sshProfile("via-b", "Via B", "bastion-b"), sshProfile("direct", "Direct", "")];
const h = await open("/api/schema-versions/compare", "schema-versions", {
  "/api/live/schemas": { status: 200, body: catalog },
  "/api/profiles/list": { status: 200, body: { items: sshProfiles, secret_store: { backend: "vault", label: "encrypted vault", available: true } } },
  "/api/schema-versions/list": () => ({ status: 200, body: { schema_version: "schema-version-list-1.0", items: versions } }),
  "/api/schema-versions/save": () => {
    const v = saves++ === 0 ? made.v1 : made.v2;
    versions = [v, ...versions];
    return { status: 200, body: v, delay: 300 };
  },
  "/api/schema-versions/get": (body) => ({ status: 200, body: { ...versions.find((v) => v.id === body.id), snapshot: made.snapshot } }),
  "/api/schema-versions/sync": (body) => replies.sync.shift() || ({ status: 200,
    body: fixtures(body.chosen_keys).report }),
  "/api/schema-versions/lint": () => replies.lint.shift() || ({ status: 200, body: made.lint }),
  "/api/schema-versions/update": (body) => {
    versions = versions.map((v) => (v.id === body.id ? { ...v, name: body.name, note: body.note } : v));
    return { status: 200, body: versions.find((v) => v.id === body.id) };
  },
  "/api/schema-versions/delete": (body) => {
    versions = versions.filter((v) => v.id !== body.id);
    return { status: 200, body: { deleted: true } };
  },
}, { fallback: { status: 200, body: made.report } });
const { page, state, errors } = h;
const sent = (pathname) => state.other.filter((r) => r.path === pathname);
const visible = (selector) => page.$eval(selector, (e) => !e.closest("[hidden]") && e.offsetParent !== null).catch(() => false);

try {
  // 1. The place: SQL workspace group, after Database diagram; reads the saved list on open, never a server.
  const places = await page.$$eval(".main-nav button[data-place]", (els) => els.map((e) => e.dataset.place));
  assert.equal(places[places.indexOf("diagram") + 1], "schema", "Schema versions follows Database diagram");
  await page.click('.main-nav button[data-place="schema"]');
  await page.waitForSelector(".schema-versions-view .view-empty");
  assert.equal(sent("/api/schema-versions/list").length, 1, "the saved list is read on open");
  assert.equal(state.seen.length, 0, "nothing is compared on open");
  assert.match(await h.text(".schema-versions-view .view-empty"), /No saved versions/);
  assert.equal(await page.$$eval('[data-action="schema-save"]', (els) => els.length), 1, "one Save version… while the list is empty");
  assert.equal(await page.$eval('.window-statusbar [data-status="safety"]', (e) => !!e).catch(() => false), true,
    "the footer names the write safety of the connection in use");

  // 2. Save version: a sheet with the databases of the connection in use (no system ones), name and note.
  await page.click('[data-action="schema-save"]');
  await page.waitForSelector("dialog[open] select#schema-save-database");
  const options = await page.$$eval("dialog[open] select#schema-save-database option", (os) => os.map((o) => o.value));
  assert.deepEqual(options.filter(Boolean), ["sv_b", "sv_m"], "system databases are not offered");
  await page.select("dialog[open] select#schema-save-database", "sv_m");
  await page.type("dialog[open] #schema-save-name", "before release 42");
  await page.click("dialog[open] button ::-p-text(Save version)");
  await page.waitForSelector("dialog[open] [aria-busy=true]");
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  const save = sent("/api/schema-versions/save")[0].body;
  assert.equal(save.database, "sv_m");
  assert.equal(save.name, "before release 42");
  assert.equal(save.connection.host, "127.0.0.1", "the connection in use is read");
  await page.waitForSelector(".schema-versions-view tbody tr");
  assert.match(await h.text(".schema-versions-line"), /Saved version 1 of sv_m \(“before release 42”\) · 7 tables, 1 view/);

  // 3. The inspector: the saved definitions are read once, objects open their definition.
  await page.waitForSelector(".view-inspector .schema-object-list");
  assert.equal(sent("/api/schema-versions/get").length, 1);
  assert.match(await h.text(".view-inspector"), /First version/);
  await page.click(".schema-object-list button ::-p-text(customers)");
  await page.waitForSelector(".schema-object-list .schema-code pre");
  assert.match(await h.text(".schema-object-list .schema-code pre"), /^CREATE TABLE `customers`/);
  assert.equal(sent("/api/schema-versions/lint").length, 0, "selecting a saved version never reads Checks");
  await page.click(".schema-checks > summary");
  await page.waitForFunction(() => /No findings in the checks completed/.test(document.querySelector(".schema-checks")?.textContent || ""));
  assert.equal(sent("/api/schema-versions/lint").length, 1);
  assert.equal(sent("/api/schema-versions/lint")[0].body.id, made.v1.id);
  assert.equal(sent("/api/schema-versions/lint")[0].body.connection.host, "127.0.0.1", "Checks uses the connection in use");
  assert.equal(await page.$eval(".schema-checks > summary .count", (e) => e.textContent), String(made.lint.findings.length));
  assert.equal(await page.$eval(".schema-checks details", (e) => e.open), false, "not-judged details start folded");
  await page.click(".schema-checks details > summary");
  assert.match(await h.text(".schema-checks details"), /Not judged/);
  const lintReads = sent("/api/schema-versions/lint").length;
  await page.click('.main-nav button[data-place="query"]');
  await pause(150);
  assert.equal(sent("/api/schema-versions/lint").length, lintReads, "no Checks read while the place is hidden");
  await page.click('.main-nav button[data-place="schema"]');
  await pause(150);
  assert.equal(sent("/api/schema-versions/lint").length, lintReads, "reopening the completed Checks uses its result");

  // 4. Saving the same schema again says nothing changed since version 1.
  await page.click('[data-action="schema-save"]');
  await page.waitForSelector("dialog[open] select#schema-save-database");
  await page.select("dialog[open] select#schema-save-database", "sv_m");
  await page.click("dialog[open] button ::-p-text(Save version)");
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  await page.waitForFunction(() => /No changes since version 1\./.test(document.querySelector(".schema-versions-line")?.textContent || ""));
  assert.equal(await page.$$eval(".schema-versions-view tbody tr", (rows) => rows.length), 2);
  assert.match(await h.text(".schema-versions-view .summary-line"), /2 saved versions of 1 database/);
  assert.equal(await page.$(".schema-versions-view tbody .schema-version-flag"), null, "complete versions carry no flag");
  // The new version has its own Checks. A reply for it cannot populate the
  // first version after a fast selection change.
  assert.equal(await page.$eval(".schema-checks", (e) => e.open), false);
  replies.lint.push({ status: 200, body: made.lint_watch, delay: 600 });
  await page.click(".schema-checks > summary");
  await page.waitForSelector('.schema-checks [data-phase="pending"]');
  await page.click(`.schema-versions-view tr[data-row-key="${made.v1.id}"]`);
  await pause(700);
  assert.equal(await page.$eval(".schema-checks", (e) => e.open), false);
  assert.equal(await page.$(".schema-check-list"), null, "a late Checks result stays with its original version");
  replies.lint.push({ status: 200, body: made.lint_watch });
  await page.click(".schema-checks > summary");
  await page.waitForSelector(".schema-check-list li");
  assert.equal(await page.$$eval(".schema-check-list li", (els) => els.length), made.lint_watch.findings.length);
  assert.equal(await page.$eval(".schema-check-list li", (el) => el.title), `${made.lint_watch.findings[0].text}\n${made.lint_watch.findings[0].detail}`);
  assert.match(await h.text(".schema-check-list"), /Watch/);
  await page.screenshot({ path: `${h.shots}/schema-checks-1440.png`, fullPage: true });
  // The Database choice lists the server's databases (no system ones) with their saved versions; nothing is typed.
  const choices = await page.$$eval(".schema-database-filter select option", (os) => os.map((o) => [o.value, o.textContent]));
  assert.deepEqual(choices, [["", "All databases"], ["sv_b", "sv_b · none saved"], ["sv_m", "sv_m · 2 versions"]]);
  await page.select(".schema-database-filter select", "sv_b");
  await page.waitForFunction(() => /No saved versions of sv_b/.test(document.querySelector(".schema-versions-view .view-empty")?.textContent || ""));
  await page.click('.schema-versions-view .view-empty [data-action="schema-save"]');
  await page.waitForSelector("dialog[open] select#schema-save-database");
  assert.equal(await page.$eval("dialog[open] select#schema-save-database", (s) => s.value), "sv_b", "Save starts with the chosen database");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  await page.select(".schema-database-filter select", "sv_m");
  await page.waitForFunction(() => document.querySelectorAll(".schema-versions-view tbody tr").length === 2);
  assert.match(await h.text(".schema-versions-view .summary-line"), /2 saved versions of sv_m/);
  await page.select(".schema-database-filter select", "");
  // Choosing sv_b hid the selected version, so it is no longer selected: select it again.
  await page.click(".schema-versions-view tbody tr ::-p-text(Same as 1)");
  await page.waitForSelector(".view-inspector .schema-inspector-actions");
  // An account that could not see every kind of object: said in words, never color alone; Refresh reads the list again.
  versions = versions.map((v) => (v.id === made.v2.id ? { ...v, complete: false } : v));
  await page.click('.schema-versions-view [data-action="refresh"]');
  await page.waitForFunction(() => /May be incomplete/.test(document.querySelector(".schema-versions-view tbody")?.textContent || ""));
  assert.equal(sent("/api/schema-versions/list").length, 2);

  // 5. Rename (Name and Note) and Delete (Cancel keeps it).
  await h.button("Rename…", ".view-inspector ");
  await page.waitForSelector("dialog[open] #schema-rename-name");
  await page.type("dialog[open] #schema-rename-name", "after deploy");
  await page.click("dialog[open] button ::-p-text(Save)");
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  assert.equal(sent("/api/schema-versions/update").at(-1).body.name, "after deploy");
  await page.waitForFunction(() => /after deploy/.test(document.querySelector(".schema-versions-view tbody")?.textContent || ""));
  await h.button("Delete…", ".view-inspector ");
  await page.waitForSelector("dialog[open]");
  assert.match(await h.text("dialog[open]"), /removed from this computer\. The database is not changed/);
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  assert.equal(sent("/api/schema-versions/delete").length, 0, "Escape deletes nothing");

  // 6. Compare: Source and Target are filled in (the newest saved version; the
  // database now through the connection in use, its first database), never typed.
  await page.click("#schema-versions-tab-compare");
  await page.waitForSelector(".schema-compare-bar");
  assert.equal(await page.$('.schema-versions-view [data-action="refresh"]'), null, "Compare is the one way to compare");
  assert.deepEqual(await h.texts(".schema-compare-bar legend"), ["Source", "Target"]);
  assert.deepEqual(await page.$$eval(".schema-compare-bar legend", labels => labels.map(e => e.title)), ["The schema to match", "The schema the sync script changes"]);
  await page.waitForFunction(() => /Ready to compare/.test(document.querySelector(".schema-versions-view .view-empty")?.textContent || ""));
  assert.equal(await page.$eval("#schema-side-source-version", (s) => s.value), made.v2.id, "the newest version is the source");
  assert.equal(await page.$eval("select#schema-side-target-database", (s) => s.value), "sv_b", "the target is a database now");
  await page.select("#schema-side-source-version", made.v1.id);
  // Keyboard: the first result replaces the setup and its Compare button;
  // focus lands on the bar's Compare, never on the page.
  await page.focus(".schema-compare-run button.primary");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".schema-compare-sides");
  await page.waitForFunction(() => document.activeElement?.matches(".comparison-actions .schema-compare-run button.primary"));
  assert.equal(await page.$(".schema-compare-bar fieldset"), null, "successful comparison collapses setup");
  const change = '.schema-compare-bar button[aria-expanded]';
  assert.equal(await page.$eval(change, e => e.getAttribute("aria-expanded")), "false");
  assert.match(await h.text(".comparison-names"), /Source.*sv_m.*Version 1.*Target.*sv_b/s);
  assert.match(await page.$eval(".comparison-header", e => e.title), /Last compared: Source.*read.*Target.*read/s);
  await page.click(change);
  await page.waitForSelector("#schema-side-source-version");
  assert.equal(await page.$eval(change, e => e.getAttribute("aria-expanded")), "true");
  await page.focus("#schema-side-source-version"); await page.keyboard.press("Escape");
  assert.equal(await page.$("#schema-side-source-version"), null);
  assert.equal(await page.$eval(change, e => e === document.activeElement), true, "Escape returns focus to Change");
  await page.click(change); await page.click(change);
  assert.equal(await page.$("#schema-side-source-version"), null, "second Change click collapses setup");
  const comparisons = state.seen.length;
  const compared = await h.text(".comparison-names");
  await page.click('.schema-compare-bar [aria-label="Swap source and target"]');
  assert.match(await h.text(".schema-sides-changed"), /chosen sides changed \(next: Source sv_b.*Target sv_m/s);
  assert.equal(await h.text(".comparison-names"), compared, "the bar keeps naming the sides the result came from");
  await page.click(change);
  assert.equal(await page.$eval("#schema-side-target-version", s => s.value), made.v1.id, "Swap changes the selected sides");
  assert.equal(await page.$eval("#schema-side-source-database", s => s.value), "sv_b");
  await page.click('.schema-compare-bar [aria-label="Swap source and target"]');
  await page.click(change);
  assert.equal(state.seen.length, comparisons, "Change and Swap read no comparison");
  assert.equal(await page.$(".schema-sides-changed"), null, "restoring the compared sides clears the note");
  await reviewCompare(page, "schema", ".schema-versions-view", ".schema-compare-tabs");
  const request = state.seen.at(-1);
  assert.deepEqual(request.source, { version: made.v1.id });
  assert.equal(request.target.live.database, "sv_b");
  assert.equal(request.target.live.connection.host, "127.0.0.1");
  assert.equal(request.ignore_definers, false);
  assert.equal(request.markdown, true);
  assert.deepEqual(await h.texts(".schema-compare-sides span"), ["Source", "Target"]);
  const summary = made.report.summary;
  assert.match(await h.text(".schema-versions-view .summary-line"),
    new RegExp(`${summary.different} different · ${summary.only_in_source} only in source · ${summary.only_in_target} only in target`));
  const rows = await page.$$eval(".schema-versions-view tbody tr", (r) => r.length);
  assert.equal(rows, made.report.objects.filter((o) => o.status !== "identical").length, "identical objects are hidden");
  const statuses = await h.texts(".schema-versions-view tbody .pill");
  assert.ok(statuses.includes("Only in source") && statuses.includes("Only in target") && statuses.includes("Different"));
  assert.match(await h.text(".schema-versions-view tbody"), /Created in target/);
  assert.match(await h.text(".schema-versions-view tbody"), /Excluded/);
  assert.equal(await h.text('.schema-versions-view th[data-col="risk"] .th-label'), "Risk");
  const notes = made.report.objects.find((o) => o.name === "notes");
  assert.ok(notes.risks.rebuild, "the fixture has a known table rebuild");
  const rebuildLabel = '.schema-versions-view tr[data-row-key="relation:notes"] td[data-col="risk"] .pill';
  // The pill reads "Rebuilds table", then Python's sentence for assistive technology.
  assert.ok((await h.texts(rebuildLabel)).some((text) => text.startsWith("Rebuilds table")));
  assert.ok((await page.$$eval(rebuildLabel, (labels) => labels.map((label) => label.title))).includes(notes.risks.rebuild),
    "the Risk label exposes Python's complete sentence");
  assert.ok(!made.report.sync.statements.some((statement) => /^DROP TABLE\b/i.test(statement.sql)), "target-only table drops are excluded by default");
  const boxes = await page.$$eval('.schema-versions-view tbody tr', (rows) => rows.map((row) => ({
    key: row.dataset.rowKey, checked: row.querySelector('td[data-col="include"] input').checked,
  })));
  for (const box of boxes) assert.equal(box.checked, made.report.sync.chosen_keys.includes(box.key));
  const allKeys = made.report.objects.map((entry) => entry.key);
  const allReport = fixtures(allKeys).report, noneReport = fixtures([]).report;
  const compareReads = state.seen.length;
  replies.sync.push({ status: 200, body: allReport, delay: 800 }, { status: 200, body: noneReport, delay: 100 });
  await Promise.all([
    page.waitForRequest((request) => new URL(request.url()).pathname === "/api/schema-versions/sync"),
    page.click('.schema-versions-view thead th[data-col="include"] input'),
  ]);
  await page.waitForFunction(() => /Updating the sync script/.test(document.querySelector(".schema-sync-status")?.textContent || ""));
  assert.deepEqual(sent("/api/schema-versions/sync").at(-1).body.chosen_keys, allKeys, "select all includes hidden identical objects");
  assert.equal(sent("/api/schema-versions/sync").at(-1).body.comparison_id, made.report.comparison_id);
  assert.equal(await page.$(".view-inspector.is-detail"), null, "checkboxes do not open object details");
  await page.click("#schema-compare-tab-sync");
  await page.waitForSelector(".schema-migration.is-stale");
  assert.ok((await page.$$eval(".schema-migration-actions button", (buttons) => buttons.map((b) => b.disabled))).every(Boolean), "stale script actions are unavailable");
  await page.click("#schema-compare-tab-differences");
  await page.click('.schema-versions-view thead th[data-col="include"] input');
  await page.waitForFunction(() => !/Updating the sync script/.test(document.querySelector(".schema-sync-status")?.textContent || ""));
  await pause(850);
  await page.click("#schema-compare-tab-sync");
  await page.waitForSelector(".schema-migration:not(.is-stale)");
  assert.equal((await h.text(".schema-script")).trimEnd(), noneReport.sync.script.trimEnd(), "the late superseded generation cannot replace the latest choices");
  assert.equal(state.seen.length, compareReads, "sync selection never rereads a database comparison");
  await page.click("#schema-compare-tab-differences");
  replies.sync.push({ status: 400, body: { error: "The comparison expired. Compare again." } });
  await page.click('.schema-versions-view thead th[data-col="include"] input');
  await page.waitForFunction(() => /Compare again/.test(document.querySelector(".schema-sync-status")?.textContent || ""));
  await page.click("#schema-compare-tab-sync");
  assert.equal(await page.$eval(".schema-migration-actions button.primary", (b) => b.disabled), true, "an expired comparison does not expose stale SQL");
  await page.click(".schema-compare-run button.primary");
  await page.waitForFunction(() => document.querySelector('#schema-compare-tab-differences')?.getAttribute('aria-selected') === 'true');
  await page.click(".schema-compare-tabs .checkbox, .schema-compare-tabs label ::-p-text(Show identical)");
  await page.waitForFunction((n) => document.querySelectorAll(".schema-versions-view tbody tr").length === n, {}, made.report.objects.length);

  // 7. A different table opens over the whole view: what differs (source and
  // target side by side) and both definitions; Back returns to the same list.
  await page.click(".schema-versions-view tbody tr td[data-col=\"name\"] ::-p-text(customers)");
  await page.waitForSelector(".view-inspector.is-detail .schema-changes-table");
  assert.equal(await visible(".schema-versions-view .view-main"), false, "the detail takes the whole view");
  const customers = made.report.objects.find((o) => o.name === "customers");
  assert.deepEqual(await h.texts(".schema-changes-table tbody th .schema-change-name"), customers.changes.map((c) => c.label));
  assert.ok((await page.$$eval(".schema-change-kind", badges => badges.map(b => b.className))).every(c => c.includes("comparison-status") && !/\bis-(ok|warn|danger)\b/.test(c)), "schema change kinds share comparison badges");
  assert.ok(await page.$(".schema-split td.is-source"), "the source's own lines are marked");
  assert.ok(await page.$(".schema-split td.is-target"), "the target's own lines are marked");
  assert.deepEqual(await h.texts(".schema-split thead th"), ["Source", "Target"]);
  assert.match(await h.text(".schema-detail-lead"), /sync script changes the target/);
  const customerRisks = [...customers.risks.destructive, ...customers.risks.data_dependent, ...(customers.risks.rebuild ? [customers.risks.rebuild] : [])];
  if (customerRisks.length) {
    const detail = await h.text(".schema-detail .schema-risk-details");
    for (const risk of customerRisks) assert.ok(detail.includes(risk), "detail uses Python's complete risk explanation");
  }
  await h.button("Back to differences", ".schema-versions-view ");
  await page.waitForFunction((n) => document.querySelectorAll(".schema-versions-view tbody tr").length === n, {}, made.report.objects.length);
  assert.equal(await page.$(".view-inspector.is-detail"), null);

  // The predictable rebuild has the same Why presentation as the editor.
  await page.click('.schema-versions-view tr[data-row-key="relation:notes"] td[data-col="name"]');
  await page.waitForSelector('.schema-detail .alter-prediction[data-algorithm="INPLACE"]');
  assert.match(await h.text(".schema-detail .alter-prediction"), /Predicted: INPLACE, rebuilds the table/);
  assert.ok((await h.text(".schema-detail .schema-risk-details")).includes(notes.risks.rebuild));
  const noteAlter = made.report.sync.statements.find((s) => s.object?.name === "notes" && s.algorithm);
  await page.click(".schema-alter-prediction details summary");
  const why = await h.text(".schema-alter-prediction .alter-prediction-details");
  for (const clause of noteAlter.algorithm.clauses) {
    assert.ok(why.includes(clause.reason), "Why uses the predictor's clause reasons");
    if (clause.source) assert.ok(why.includes(clause.source), "Why includes the source evidence");
  }
  await h.button("Back to differences", ".schema-versions-view ");

  // 8. The sync script: the review list, and Open in Query editor because the
  // target is a database read now through the server in use.
  await page.click("#schema-compare-tab-sync");
  await page.waitForSelector(".schema-migration");
  assert.match(await h.text(".schema-migration-head"), new RegExp(`${made.report.sync.summary.statements} statements that would change sv_b \\(the target\\) to match sv_m \\(the source\\)`));
  assert.match(await h.text(".schema-migration"), /Drops data/);
  assert.equal(await page.$eval(".schema-migration-actions button.primary", (b) => b.disabled), false);
  assert.match(await h.text(".schema-script"), /^-- myflames schema sync/);
  assert.match(await h.text(".schema-script"), /-- ALGORITHM: INPLACE, rebuilds the table/);
  assert.match(await h.text(".schema-script"), /-- ALGORITHM: not predicted/);
  await page.click(".schema-migration-actions button.primary");
  await page.waitForFunction(() => /-- myflames schema sync/.test(document.querySelector("textarea.sql-editor")?.value || ""));
  assert.equal(await visible('.main-nav button[data-place="query"].active'), true, "the Query editor is on screen");
  assert.equal(state.other.filter((r) => r.path.startsWith("/api/editor/run")).length, 0, "nothing runs");

  // 9. The target on another server, or a saved version: the script cannot
  // open in the editor, and says why.
  await page.click('.main-nav button[data-place="schema"]');
  await page.waitForSelector(".schema-compare-bar");
  state.queue.push({ status: 200, body: made.other });
  await page.click(".schema-compare-run button.primary");
  await page.waitForFunction(() => /db9/.test(document.querySelector(".schema-compare-sides")?.textContent || ""));
  await page.click("#schema-compare-tab-sync");
  await page.waitForSelector(".schema-migration-actions button.primary");
  assert.equal(await page.$eval(".schema-migration-actions button.primary", (b) => b.disabled), true);
  assert.match(await h.text(".schema-migration-why"), /use a connection to that server/);
  state.queue.push({ status: 200, body: made.saved_target });
  await page.click(".schema-compare-run button.primary");
  await page.waitForFunction(() => /Target.*version 1/s.test(document.querySelector(".schema-compare-sides")?.textContent || ""));
  await page.click("#schema-compare-tab-sync");
  await page.waitForFunction(() => /The target is a saved version/.test(document.querySelector(".schema-migration-why")?.textContent || ""));
  assert.equal(await page.$eval(".schema-migration-actions button.primary", (b) => b.disabled), true);

  // 10. Ignore definers goes with the request; a failed compare offers Try again and keeps the last result.
  await page.click('.schema-compare-bar button[aria-expanded="false"]');
  await page.click(".schema-compare-run label ::-p-text(Ignore definers)");
  state.queue.push({ status: 400, body: { error: "Could not complete the request: There is no database named sv_b, or the account cannot see it." } });
  await page.click(".schema-compare-run button.primary");
  await page.waitForFunction(() => /no database named sv_b/.test(document.querySelector(".schema-versions-view .action-status")?.textContent || ""));
  assert.equal(state.seen.at(-1).ignore_definers, true);
  assert.equal(await page.$eval('.schema-compare-bar button[aria-expanded]', e => e.getAttribute("aria-expanded")), "true", "a failed comparison preserves the expanded setup");
  assert.ok(await page.$(".schema-compare-sides"), "the last comparison stays on screen");
  await h.button("Try again", ".schema-versions-view ");
  await page.waitForFunction(() => !/no database named/.test(document.querySelector(".schema-versions-view .action-status")?.textContent || ""));

  // A kept target-only child blocks its parent change and a new child that
  // reuses its constraint name. The row names the object to choose, alongside
  // Python's risk labels. Choosing that object regenerates without a read.
  state.queue.push({ status: 200, body: made.blocked });
  await page.click(".schema-compare-run button.primary");
  await page.click("#schema-compare-tab-differences");
  await page.waitForSelector('tr[data-row-key="relation:parent2"] .schema-needs');
  for (const name of ["parent2", "child3"]) {
    const row = `tr[data-row-key="relation:${name}"]`;
    assert.equal(await h.text(`${row} td[data-col="risk"] .schema-needs`), "Needs child2");
    assert.equal(await h.text(`${row} td[data-col="sync"]`), "Not generated");
  }
  const keysWithChild = [...made.blocked.sync.chosen_keys, "relation:child2"];
  await page.screenshot({ path: `${h.shots}/schema-needs-1440.png`, fullPage: true });
  replies.sync.push({ status: 200, body: fixtures(keysWithChild, true).blocked });
  const dependencyReads = state.seen.length;
  await page.click('tr[data-row-key="relation:child2"] td[data-col="include"] input');
  await page.waitForFunction(() => !document.querySelector('tr[data-row-key="relation:parent2"] .schema-needs'));
  assert.equal(await page.$('tr[data-row-key="relation:child3"] .schema-needs'), null);
  assert.equal(await h.text('tr[data-row-key="relation:child3"] td[data-col="sync"]'), "Created in target");
  assert.equal(state.seen.length, dependencyReads, "choosing a blocker uses the cached comparison");
  state.queue.push({ status: 200, body: made.report });
  await page.click(".schema-compare-run button.primary");
  await page.waitForFunction(() => !document.querySelector('tr[data-row-key="relation:child2"]'));

  // 11. A narrow window: no horizontal page scroll; a difference still opens over the whole view.
  await page.setViewport({ width: 720, height: 900 });
  await pause(300);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(overflow <= 0, `horizontal scroll of ${overflow}px at 720 px`);
  await page.click("#schema-compare-tab-differences");
  // Scrolled into view first: a row half under the window footer takes no click.
  const orders = await page.$(".schema-versions-view tbody tr td[data-col=\"name\"] ::-p-text(orders)");
  await orders.scrollIntoView();
  await orders.click();
  await page.waitForSelector(".view-inspector.is-detail .schema-split");
  const detailOverflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert.ok(detailOverflow <= 0, `horizontal scroll of ${detailOverflow}px in the detail at 720 px`);
  await page.screenshot({ path: `${h.shots}/schema-versions-720.png`, fullPage: true });
  await page.setViewport({ width: 1440, height: 1000 });

  // 12. The SSH target is part of the server: a comparison whose target was
  // read through SSH host bastion-a opens in the Query editor only from a
  // connection through bastion-a, never through bastion-b or directly to the
  // same 127.0.0.1:3306, and says why; the reverse holds for a direct read.
  await h.button("Back to differences", ".schema-versions-view ");
  const openButton = ".schema-migration-actions button.primary";
  const syncState = async () => {
    await page.click("#schema-compare-tab-sync");
    await page.waitForSelector(openButton);
    return { disabled: await page.$eval(openButton, (b) => b.disabled),
      why: await page.$eval(".schema-migration-head", (e) => e.querySelector(".schema-migration-why")?.textContent || "") };
  };
  await chooseConnection(page, "via-a");
  state.queue.push({ status: 200, body: made.via_a });
  await page.click(".schema-compare-run button.primary");
  await page.waitForFunction(() => /via bastion-a/.test(document.querySelector(".schema-compare-sides")?.textContent || ""));
  assert.deepEqual(await syncState(), { disabled: false, why: "" }, "through bastion-a, the server the target was read from");
  await chooseConnection(page, "via-b");
  let sync = await syncState();
  assert.equal(sync.disabled, true, "the same address through bastion-b is another server");
  assert.equal(sync.why, "The target was read through SSH host bastion-a and the connection in use connects through SSH host bastion-b: "
    + "the same address can be another server. Use a connection to 127.0.0.1:3306 via bastion-a to open the script in the Query editor.");
  await chooseConnection(page, "direct");
  sync = await syncState();
  assert.equal(sync.disabled, true, "a direct connection to the same address may reach another server");
  assert.match(sync.why, /^The target was read through SSH host bastion-a and the connection in use connects directly:/);
  // A target read directly: never from a connection through SSH.
  state.queue.push({ status: 200, body: made.report });
  await page.click(".schema-compare-run button.primary");
  await page.waitForFunction(() => !/via bastion-a/.test(document.querySelector(".schema-compare-sides")?.textContent || ""));
  assert.deepEqual(await syncState(), { disabled: false, why: "" });
  await chooseConnection(page, "via-a");
  sync = await syncState();
  assert.equal(sync.disabled, true);
  assert.equal(sync.why, "The target was read directly and the connection in use connects through SSH host bastion-a: "
    + "the same address can be another server. Use a connection to 127.0.0.1:3306 without SSH to open the script in the Query editor.");
  await page.click(`${openButton}`).catch(() => {});
  assert.equal(await page.$eval('.main-nav button[data-place="schema"]', (e) => e.classList.contains("active")), true, "nothing opened");

  // Compare with the database now follows the same rule for a version saved
  // through bastion-a, and its Server column names the SSH host.
  versions = [made.v3, ...versions];
  await page.click("#schema-versions-tab-saved");
  await page.click('.schema-versions-view [data-action="refresh"]');
  const v3Row = `.schema-versions-view tr[data-row-key="${made.v3.id}"]`;
  await page.waitForSelector(v3Row);
  assert.match(await h.text(`${v3Row} td[data-col="server"]`), /^127\.0\.0\.1:3306 via bastion-a$/);
  await page.click(`${v3Row} td[data-col="database"]`);
  const nowButton = ".view-inspector .schema-inspector-actions button.primary";
  await page.waitForSelector(nowButton);
  assert.equal(await page.$eval(nowButton, (b) => b.disabled), false, "the connection in use goes through bastion-a");
  await chooseConnection(page, "via-b");
  await page.waitForFunction((b) => document.querySelector(b)?.disabled === true, {}, nowButton);
  assert.equal(await page.$eval(nowButton, (b) => b.title), "This version was read through SSH host bastion-a and the connection "
    + "in use connects through SSH host bastion-b: the same address can be another server.");
  await chooseConnection(page, "direct");
  await page.waitForFunction((b) => /connects directly/.test(document.querySelector(b)?.title || ""), {}, nowButton);
  assert.equal(await page.$eval(nowButton, (b) => b.disabled), true);

  assert.deepEqual(errors, [], "no page errors");
  console.log("schema-versions: ok");
} finally {
  await h.close();
}
