/**
 * Headless-browser smoke test for every teach lesson.
 *
 * Verifies that animations *actually move pixels*, not just that the
 * <script> block parses. Catches the kind of regression that took
 * out nested_loop.js after the Tier-1 migration: Motion One's CSS
 * transforms don't compose with SVG transform attributes, so the
 * pills stayed glued to the start position even though `node --check`
 * passed and Python tests were green.
 *
 * For each lesson:
 *   1. Render to a temp file via `python3 -m myflames teach <name>`.
 *   2. Open the file in headless Chromium via Puppeteer.
 *   3. Capture every console error / page error — fail if any.
 *   4. Click the Play button.
 *   5. Snapshot the SVG after 200ms and again after 1500ms.
 *   6. Diff the two snapshots: if literally nothing changed inside
 *      the lesson's primary <svg>, the animation is broken.
 *
 * Usage:
 *   cd assets
 *   node verify-animations.mjs                # all lessons
 *   node verify-animations.mjs nested_loop    # one lesson
 *   node verify-animations.mjs --headed       # show the browser
 */
import puppeteer from "puppeteer";
import { execSync } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ALL_LESSONS = [
  "full_scan", "btree", "unique_lookup", "non_unique_lookup", "icp",
  "covering_index", "nested_loop", "hash", "bnl", "bka_join",
  "semijoin_weedout", "derived_table", "lru", "buffer_pool_warmup",
  "filesort", "tmp", "filter", "skip_scan", "index_merge",
  "rowid_filter",
];

const argv = process.argv.slice(2);
const headed = argv.includes("--headed");
const targets = argv.filter(a => !a.startsWith("--"));
const lessons = targets.length ? targets : ALL_LESSONS;

const REPO = join(import.meta.dirname, "..");
const TMP = mkdtempSync(join(tmpdir(), "myflames-verify-"));

function renderLesson(slug) {
  const out = join(TMP, `${slug}.html`);
  execSync(`python3 -m myflames teach ${slug} -o ${out}`, {
    cwd: REPO, stdio: ["ignore", "ignore", "pipe"],
  });
  return out;
}

// Snapshot every animatable attribute we care about across all SVG
// elements inside the primary lesson <svg>. Returns a sorted-keys
// object so we can diff cheaply.
async function snapshotSvg(page) {
  return await page.evaluate(() => {
    // Pick the lesson's main stage SVG. Lessons name it variably
    // (nlj-svg, svg-pool, svg-innodb, complexity-chart, …) so we
    // grab every <svg> inside <main> EXCEPT the static help-tip ones.
    const svgs = Array.from(document.querySelectorAll("main svg"));
    if (!svgs.length) return { _err: "no svg in main" };
    const out = {};
    let i = 0;
    for (const svg of svgs) {
      // Skip tiny svgs (help-tip icons etc).
      const r = svg.getBoundingClientRect();
      if (r.width < 50 || r.height < 50) continue;
      const els = svg.querySelectorAll("rect, circle, text, g, line, path, polygon");
      const sig = [];
      for (const el of els) {
        const cls = el.getAttribute("class") || "";
        const id = el.getAttribute("id") || "";
        const t = el.getAttribute("transform") || "";
        const f = el.getAttribute("fill") || "";
        const x = el.getAttribute("x") || el.getAttribute("cx") || "";
        const y = el.getAttribute("y") || el.getAttribute("cy") || "";
        const w = el.getAttribute("width") || "";
        const op = el.getAttribute("opacity") || "";
        const sw = el.getAttribute("stroke-width") || "";
        const txt = el.tagName === "text" ? (el.textContent || "").slice(0, 40) : "";
        const cs = el.style.transform || "";  // CSS transform too
        sig.push([el.tagName, cls, id, t, f, x, y, w, op, sw, txt, cs].join("|"));
      }
      out[`svg${i++}`] = sig.join("\n");
    }
    return out;
  });
}

function diffSnapshots(a, b) {
  const changed = [];
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) {
    if (a[k] !== b[k]) changed.push(k);
  }
  return changed;
}

async function verifyOne(browser, slug) {
  const file = renderLesson(slug);
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 900 });
  const errors = [];
  page.on("console", msg => {
    if (msg.type() === "error") errors.push(`console.error: ${msg.text()}`);
  });
  page.on("pageerror", err => {
    errors.push(`pageerror: ${err.message}`);
  });
  await page.goto("file://" + file, { waitUntil: "domcontentloaded" });
  // Give scripts a beat to install handlers.
  await new Promise(r => setTimeout(r, 150));

  // Verify the Tier-1 helpers are reachable (sanity check the bundle
  // installed correctly).
  const bundleOk = await page.evaluate(() => {
    return typeof window.anim === "object"
      && typeof window.anim.tween === "function"
      && typeof window.anim.flip === "function"
      && typeof window.anim.spring === "function";
  });
  if (!bundleOk) {
    await page.close();
    return { slug, ok: false, reason: "window.anim or Tier-1 helpers missing" };
  }

  // Snapshot before play.
  const before = await snapshotSvg(page);

  // Click Play.
  const clicked = await page.evaluate(() => {
    const btn = document.getElementById("btn-play");
    if (!btn) return false;
    btn.click();
    return true;
  });
  if (!clicked) {
    await page.close();
    return { slug, ok: false, reason: "no #btn-play element" };
  }

  // Let it animate.
  await new Promise(r => setTimeout(r, 1500));

  const after = await snapshotSvg(page);
  await page.close();

  if (errors.length) {
    return { slug, ok: false, reason: "JS errors: " + errors.join("; ") };
  }
  const changed = diffSnapshots(before, after);
  if (changed.length === 0) {
    return { slug, ok: false, reason: "stage SVG did not change after Play (animation dead)" };
  }
  return { slug, ok: true, changedSvgs: changed };
}

(async () => {
  const browser = await puppeteer.launch({ headless: !headed });
  const results = [];
  for (const slug of lessons) {
    process.stdout.write(`  ${slug.padEnd(22)} `);
    try {
      const r = await verifyOne(browser, slug);
      results.push(r);
      console.log(r.ok ? "OK" : `FAIL — ${r.reason}`);
    } catch (e) {
      results.push({ slug, ok: false, reason: String(e.message || e) });
      console.log("ERROR — " + (e.message || e));
    }
  }
  await browser.close();
  const failed = results.filter(r => !r.ok);
  console.log("");
  console.log(`Passed: ${results.length - failed.length} / ${results.length}`);
  if (failed.length) {
    console.log("Failed:");
    for (const f of failed) console.log(`  ${f.slug}: ${f.reason}`);
    process.exit(1);
  }
})();
