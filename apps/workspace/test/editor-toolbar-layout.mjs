// Query editor toolbar: no label is ever drawn over another. The scope, the
// transaction state and the privilege note end the toolbar; when its one row
// was too narrow for them (a 1440 px window with the schema browser closed
// and a MySQL 8.0 server, whose Run analysis note is long), the state shrank
// to nothing while its "Auto-commit" kept painting under the note. The note
// shows its whole sentence wherever one toolbar row can hold it. Checked at
// 1440, 1040 and 720 px, light and dark, with the schema browser open and
// closed, one statement or two (the scope), a plain state and a long
// transaction meter. The real isolated workspace (perf-harness.mjs); every
// editor and live request is mocked, so no database is needed.
// Build first: npm run build && node test/editor-toolbar-layout.mjs
// EDITOR_TOOLBAR_SHOTS=<dir> keeps a screenshot of each width and theme.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { open } from "./perf-harness.mjs";

// The server's note when Run analysis is unavailable (myflames/privileges.py
// NO_ANALYZE_NOTE): MySQL before 8.3 has no EXPLAIN ANALYZE FORMAT=JSON.
const NO_ANALYZE = "Run analysis needs EXPLAIN ANALYZE FORMAT=JSON, which MySQL has from 8.3 (8.4 LTS and later); use Explain for the estimated plan.";
const METER = { age_seconds: 240, age_source: "innodb", rows_modified: "1240", rows_locked: "1243", lock_structs: "5",
  blocking: 3, blocking_truncated: false, note: null };
let inTransaction = false;
const session = (tabId) => ({
  schema_version: "editor-session-1.0", tab_id: tabId, state: "idle",
  connection: { profile_id: null, name: "Local MySQL", host: "127.0.0.1", port: 3306, user: "", type: "production", read_only: false },
  engine: "mysql", server_version: "8.0.40", connection_id: 42, autocommit: true, in_transaction: inTransaction,
  transaction_source: "performance_schema", transaction_note: null, transaction_meter: inTransaction ? METER : null,
  sql_mode: "", backslash_escapes: true, safe_updates: true, safe_updates_default: true, read_only_transactions: false,
  started_at: "2026-11-09T12:00:00Z", idle_seconds: 0, idle_timeout_seconds: 1800, ended: false, ended_reason: null,
  message: null, notes: [], run: null,
});
const ok = (body) => ({ status: 200, body });
const h = await open("/api/unused", "editor-toolbar-layout", {
  // The tab's session probe (and the meter's refresh) answer with a session.
  "/api/editor/status": (body) => ok({ status: "idle", session: session(body.tab_id), run: null }),
  // Unverified statements add no note of their own: the one note is Run analysis's.
  "/api/editor/preview": ok({ status: "ready", statements: [], aggregate: { state: "unknown", missing: [], reason: "", warning: "" },
    policy: { action: "allow", text: "" } }),
  "/api/editor/history": ok({ items: [] }),
  "/api/editor/saved/list": ok({ items: [] }),
  "/api/live/schemas": ok({ schema_version: "schemas-1.0", engine: "mysql", server_version: "8.0.40",
    schemas: [{ name: "shop", tables: 1, system: false }], system: [], all_note: "" }),
});
const { page } = h;
const shots = process.env.EDITOR_TOOLBAR_SHOTS || h.shots;
await fs.mkdir(shots, { recursive: true });

/** Every label of the toolbar with the rectangle it paints: its box joined
 * with its text (text that overflows a box still paints), cut by any box
 * between it and the toolbar that clips. */
const measure = () => page.evaluate(() => {
  const bar = document.querySelector(".query-lab .editor-toolbar");
  const rect = (r) => ({ x: r.left, r: r.right, y: r.top, b: r.bottom });
  const items = [...bar.querySelectorAll(
    "button, select, .inline-field, .run-scope, .tx-state .status-pill, .editor-privilege-note")]
    .filter((e) => e.getClientRects().length > 0 && !e.closest("[hidden], [role=dialog]"))
    // A pill inside the meter's button is measured as itself, not twice.
    .filter((e) => !e.matches(".editor-transaction-trigger"));
  return {
    bar: rect(bar.getBoundingClientRect()),
    scroll: bar.scrollWidth - bar.clientWidth,
    page: document.documentElement.scrollWidth - innerWidth,
    labels: items.map((e) => {
      const range = document.createRange();
      range.selectNodeContents(e);
      const box = rect(e.getBoundingClientRect());
      const text = rect(range.getBoundingClientRect());
      let paint = text.r > text.x
        ? { x: Math.min(box.x, text.x), r: Math.max(box.r, text.r), y: Math.min(box.y, text.y), b: Math.max(box.b, text.b) }
        : box;
      for (let n = e; n && n !== bar; n = n.parentElement) {
        if (getComputedStyle(n).overflowX === "visible") continue;
        const c = rect(n.getBoundingClientRect());
        paint = { x: Math.max(paint.x, c.x), r: Math.min(paint.r, c.r), y: Math.max(paint.y, c.y), b: Math.min(paint.b, c.b) };
      }
      return { name: e.getAttribute("aria-label") || e.textContent.trim().slice(0, 40), paint, text, box, node: e.className };
    }),
    nested: items.map((e) => items.map((o) => e !== o && (e.contains(o) || o.contains(e)))),
    // The note's whole sentence (scroll), what it shows (client), and the
    // width of one full toolbar row (the toolbar's content box).
    note: (() => {
      const n = bar.querySelector(".editor-privilege-note");
      const s = getComputedStyle(bar);
      return { scroll: n.scrollWidth, client: n.clientWidth, margin: parseFloat(getComputedStyle(n).marginLeft),
        row: bar.clientWidth - parseFloat(s.paddingLeft) - parseFloat(s.paddingRight) };
    })(),
  };
});

/** Assert no two labels paint over each other, none is cut off by the
 * toolbar's edge, and the toolbar never scrolls sideways. */
async function noOverlap(where) {
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
  const m = await measure();
  assert.equal(m.scroll, 0, `the toolbar does not scroll sideways ${where}`);
  assert.ok(m.page <= 0, `no horizontal page scroll ${where}`);
  // One pixel of tolerance: a split button's halves share a border.
  const hit = (a, b) => Math.min(a.r, b.r) - Math.max(a.x, b.x) > 1 && Math.min(a.b, b.b) - Math.max(a.y, b.y) > 1;
  for (let i = 0; i < m.labels.length; i++) {
    const a = m.labels[i];
    assert.ok(a.paint.x >= m.bar.x - 1 && a.paint.r <= m.bar.r + 1, `${a.name} stays inside the toolbar ${where}: ${JSON.stringify(a.paint)}`);
    for (let j = i + 1; j < m.labels.length; j++) {
      if (m.nested[i][j]) continue;
      const b = m.labels[j];
      assert.ok(!hit(a.paint, b.paint), `"${a.name}" and "${b.name}" overlap ${where}: ${JSON.stringify([a.paint, b.paint])}`);
    }
  }
  return m;
}

async function theme(scheme) {
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: scheme }]);
  await page.evaluate((t) => {
    try { localStorage.setItem("myflames.theme", t); } catch { /* storage may be unavailable */ }
    document.documentElement.dataset.theme = t;
  }, scheme);
}

/** Open or close the schema browser pane beside the editor. */
async function browserPane(show) {
  const [toggle, done] = show ? ["Show the schema browser", "Hide the schema browser"] : ["Hide the schema browser", "Show the schema browser"];
  const button = await page.$(`.editor-toolbar button[aria-label="${toggle}"]`);
  if (button) await button.click();
  await page.waitForSelector(`.editor-toolbar button[aria-label="${done}"]`);
  await page.waitForFunction((s) => !!document.querySelector(".schema-browser")?.offsetParent === s, {}, show);
}

/** Every width, theme, schema browser and scope for the tab on screen. */
async function sweep(state) {
  for (const width of [1440, 1040, 720]) for (const scheme of ["light", "dark"]) {
    await page.setViewport({ width, height: width === 720 ? 600 : 900 });
    await theme(scheme);
    // At 720 px (NARROW in QueryLab.tsx) the schema browser is a sheet over
    // the editor, not a pane beside it, so it never narrows the toolbar.
    // Closed last, so the narrow window starts without the sheet.
    for (const pane of width > 720 ? [true, false] : [false]) {
      await browserPane(pane);
      for (const sql of ["SELECT 1", "SELECT 1;\nSELECT 2;"]) {
        await page.locator('[aria-label="SQL editor"]').fill(sql);
        const scope = sql.includes(";\n");
        await page.waitForFunction((s) => !!document.querySelector(".editor-toolbar .run-scope") === s, {}, scope);
        const where = `(${state}, ${width} px, ${scheme}, schema browser ${pane ? "open" : "closed"}, ${scope ? "two statements" : "one statement"})`;
        const m = await noOverlap(where);
        const names = m.labels.map((l) => l.name);
        assert.ok(names.some((n) => n.startsWith("Run analysis needs")), `the note is on screen ${where}`);
        // The note is never cut short to share a row: where its sentence fits
        // one full toolbar row it shows whole (on the next row if need be),
        // and only a sentence longer than a full row (the 720 px window, or
        // a 1040 px window with the schema browser open) ends with an ellipsis.
        const note = m.note;
        const fitsRow = note.scroll + note.margin <= note.row;
        if (width === 1440 || (width === 1040 && !pane))
          assert.ok(fitsRow, `one toolbar row holds the whole note ${where}: ${JSON.stringify(note)}`);
        if (fitsRow) assert.ok(note.scroll <= note.client, `the note shows its whole sentence ${where}: ${JSON.stringify(note)}`);
        else assert.ok(note.client + note.margin >= note.row - 1, `a note longer than a row takes a full row ${where}: ${JSON.stringify(note)}`);
        if (state === "Auto-commit") {
          // A plain state is short: its whole word shows, never cut to nothing.
          const pill = m.labels.find((l) => l.node.includes("status-pill"));
          assert.ok(pill.paint.r - pill.paint.x >= pill.text.r - pill.text.x - 1, `the whole state shows ${where}`);
        }
        if (!pane && !scope) {
          // A theme switch animates colors: shoot the settled frame.
          await page.waitForFunction(() => document.getAnimations().every((a) =>
            !(a instanceof CSSTransition) || a.playState !== "running"));
          await page.screenshot({ path: path.join(shots, `editor-toolbar-${state === "Auto-commit" ? "state" : "meter"}-${width}-${scheme}.png`) });
        }
      }
    }
  }
}

try {
  // A MySQL 8.0 server: Run analysis is unavailable, so its note is in the toolbar.
  await h.setPrivileges({ run_analysis: { state: "unknown", available: false, note: NO_ANALYZE, missing: [], need: "", reason: "", grey_out: true } });
  await h.button("Query editor", ".main-nav ");
  await page.waitForSelector(".query-lab .editor-toolbar");
  await page.locator('[aria-label="SQL editor"]').fill("SELECT 1");
  await page.waitForFunction(() => document.querySelector(".tx-state")?.dataset.state === "Auto-commit");
  await page.waitForFunction((note) => document.querySelector(".editor-privilege-note")?.textContent === note, {}, NO_ANALYZE);
  await sweep("Auto-commit");

  // A second tab whose session holds a transaction: the long meter in the same place.
  inTransaction = true;
  await page.setViewport({ width: 1440, height: 900 });
  await page.click(".query-tab-add");
  await page.waitForSelector(".editor-transaction-trigger");
  assert.match(await page.$eval(".editor-transaction-trigger", (e) => e.textContent), /Transaction open.*1,240 rows changed.*blocking 3/);
  await sweep("Transaction open");

  assert.deepEqual(h.errors, []);
  console.log("editor-toolbar-layout: no overlapping labels at 1440, 1040 and 720 px, light and dark, schema browser open and closed, scope, Auto-commit and the transaction meter");
} finally {
  await h.close();
}
