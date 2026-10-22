// Regression guard for workspace responsiveness and DOM windowing.
// Runs a focused pass of perf-measure.mjs (or reads --from <results.json>)
// in a throwaway HOME and checks generous budgets that catch unwindowed
// tables, synchronous layout thrash, filter/editor typing stalls, and DOM leaks
// without flaking on a loaded machine.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
const opt = (name, fallback = "") => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 && args[i + 1] && !args[i + 1].startsWith("--") ? args[i + 1] : fallback;
};

const fromFile = opt("from", "");
let tmpDir = "";
let resultsPath = fromFile ? path.resolve(fromFile) : "";

try {
  if (!resultsPath) {
    tmpDir = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-perf-budgets-"));
    const outDir = path.join(tmpDir, "out");
    execFileSync(
      process.execPath,
      [
        path.join(here, "perf-measure.mjs"),
        "--runs",
        "1",
        "--cycles",
        "5",
        "--sizes",
        "1440x900,720x520",
        "--only",
        "places,tables,overlays,editor,charts,memory",
        "--out",
        outDir,
        "--quiet",
      ],
      {
        cwd: here,
        env: {
          ...process.env,
          HOME: tmpDir,
          USERPROFILE: tmpDir,
          MYFLAMES_SECRET_BACKEND: "vault",
        },
        stdio: "inherit",
      },
    );
    resultsPath = path.join(outDir, "results.json");
  }

  const raw = JSON.parse(await fs.readFile(resultsPath, "utf8"));
  assert.ok(Array.isArray(raw.results) && raw.results.length > 0, "perf-budgets: expected raw results for error checks");
  const rows = raw.medians || raw.results || [];
  assert.ok(rows.length > 0, "perf-budgets: expected measurement rows");

  const byKey = new Map(rows.map((r) => [`${r.size} :: ${r.name}`, r]));
  const find = (size, name) => {
    const row = byKey.get(`${size} :: ${name}`);
    assert.ok(row, `Missing measurement: ${size} :: ${name}`);
    assert.ok(!row.error, `${size} :: ${name} failed with error: ${row.error}`);
    assert.ok(!row.timed_out, `${size} :: ${name} timed out`);
    return row;
  };

  let checks = 0;
  const checkMax = (size, name, field, limit) => {
    const row = find(size, name);
    const actual = row[field];
    assert.ok(Number.isFinite(actual), `${size} :: ${name} missing finite numeric ${field}`);
    assert.ok(
      actual <= limit,
      `Budget exceeded for ${size} :: ${name} [${field}]: ${actual} > ${limit}`,
    );
    checks++;
  };

  // Aggregates omit page errors and can hide a failed later run. Check all
  // raw rows, including steps without an individual timing budget.
  for (const row of [...raw.results, ...rows]) {
    assert.ok(!row.error, `${row.size} :: ${row.name} failed with error: ${row.error}`);
    assert.ok(!row.timed_out, `${row.size} :: ${row.name} timed out`);
    assert.ok(!row.errors || (Array.isArray(row.errors) && row.errors.length === 0), `Unexpected page errors in ${row.size} :: ${row.name}: ${JSON.stringify(row.errors)}`);
  }

  // Timing caps are regression thresholds, not frame-time targets. The runner
  // measures input-to-paint latency (including scheduling) and frame intervals,
  // so reserve multiple frames for contention on a loaded laptop. Keep these
  // fixed across runs: deriving a limit from the current run's median would
  // let a sustained regression raise its own budget. No healthy-run timing
  // baseline is bundled here; validate headroom against saved --from results
  // when recalibrating. Row and memory caps guard growth, not timing noise.
  for (const size of ["1440x900", "720x520"]) {
    // 1. Place switches must never time out and must paint quickly, including with large reports loaded.
    for (const place of ["server", "workload", "query", "plans"]) {
      checkMax(size, `place ${place} (first)`, "content_ms", 250);
    }
    checkMax(size, "place home (warm)", "content_ms", 250);
    for (const place of ["home", "server", "query", "workload"]) {
      checkMax(size, `place ${place} (large reports loaded)`, "content_ms", 250);
    }

    // 2. Large tables: DOM row windowing cap, sort latency, filter keystroke latency, and scroll smoothness.
    for (const table of ["Processlist 2,000", "Top statements 1,000", "Parameters all"]) {
      checkMax(size, `${table}: first render`, "rows_drawn", 350);
    }
    for (const table of ["Processlist 2,000", "Top statements 1,000", "Parameters all", "Tables 10,000", "Indexes 10,000"]) {
      checkMax(size, `${table}: sort`, "content_ms", 250);
      checkMax(size, `${table}: type in filter`, "key_p50_ms", 100);
      checkMax(size, `${table}: type in filter`, "key_p95_ms", 200);
      checkMax(size, `${table}: scroll`, "p95_ms", 100);
    }
    checkMax(size, "Tables 10,000: 200 rows per page", "content_ms", 250);
    checkMax(size, "Tables 10,000: scroll 200 rows", "p95_ms", 100);
    checkMax(size, "Tables 10,000: scroll 200 rows", "layout_ms", 180);

    // 3. Overlays: palette, floating/pushed view list, info popover, and 2,000-session MassKill sheet.
    checkMax(size, "command palette open (click)", "content_ms", 250);
    checkMax(size, "command palette open (⌘K)", "content_ms", 250);
    checkMax(size, "info popover open", "content_ms", 250);
    checkMax(size, "sheet open (Kill sessions…)", "content_ms", 250);
    checkMax(size, "inspector open (select row)", "content_ms", 250);

    // 4. Query editor with a 5,000-line script and switching back to Home.
    checkMax(size, "editor type at end (5,000 lines)", "key_p50_ms", 100);
    checkMax(size, "editor type at end (5,000 lines)", "key_p95_ms", 200);
    checkMax(size, "editor type in middle (5,000 lines)", "key_p50_ms", 100);
    checkMax(size, "editor type in middle (5,000 lines)", "key_p95_ms", 200);
    checkMax(size, "place home (5,000-line script open)", "content_ms", 250);

    // 5. Plan charts on the 1,000-operator synthetic plan.
    checkMax(size, "chart diagram (Visual Explain) (synthetic 1,000 operators): hover", "layout_ms", 160);
    checkMax(size, "chart diagram (Visual Explain) (synthetic 1,000 operators): hover", "p95_ms", 100);
    // The reported loaded-machine run reached 66.7 ms; 150 ms leaves >2×
    // that observed p95 while still rejecting sustained zoom stalls.
    checkMax(size, "chart diagram (Visual Explain) (synthetic 1,000 operators): wheel", "p95_ms", 150);
  }

  // 6. Memory and DOM node growth across repeated place and view cycles.
  for (const [prefix, nodeLimit, heapLimit] of [
    ["memory: every place ×", 5000, 12],
    ["memory: every Server and Workload view ×", 15000, 15],
  ]) {
    const memory = rows.filter((r) => r.size === "1440x900" && r.name.startsWith(prefix));
    assert.ok(memory.length > 0, `Missing measurement: 1440x900 :: ${prefix}`);
    for (const row of memory) {
      checkMax(row.size, row.name, "node_growth", nodeLimit);
      checkMax(row.size, row.name, "heap_growth_mb", heapLimit);
    }
  }

  console.log(`Perf budgets: ${checks} checks passed across 1440x900 and 720x520.`);
} finally {
  if (tmpDir) await fs.rm(tmpDir, { recursive: true, force: true });
}
