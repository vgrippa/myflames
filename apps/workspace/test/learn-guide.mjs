// Learn: guided lessons. Next step plays one step and holds, the simplified
// code highlights the lines that step runs (forward and back), Check
// yourself answers at once with a reason, and Explore steps freely over
// every step and replays it with changed inputs, returning to the guided
// steps at the same step. Covers the three family flagships and the index
// family's other lessons, both appearances, the 720 px window and full
// screen, and that every lesson opens with the stage first (parameters,
// toolbar, step bar and whole drawing on screen at 1440x900). Runs against
// the packaged UI and server.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-learn-guide-"));
const server = spawn(
  process.env.PYTHON || "python3",
  [
    "-c",
    "from myflames.ui import WorkspaceServer; import sys; s=WorkspaceServer(store_path=sys.argv[1]); print(s.launch_url, file=sys.stderr, flush=True); s.serve_forever()",
    path.join(directory, "workspace.sqlite3"),
  ],
  { cwd: root, env: { ...process.env, HOME: directory, USERPROFILE: directory, MYFLAMES_SECRET_BACKEND: "vault" } },
);
const FULL = '.teach-toolbar button[aria-label="Show the lesson animation full screen"]';
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser;
try {
  const url = await new Promise((resolve, reject) => {
    let output = "";
    const timer = setTimeout(() => reject(new Error("UI did not start: " + output)), 10000);
    server.once("error", (error) => { clearTimeout(timer); reject(error); });
    server.once("exit", (code) => { clearTimeout(timer); reject(new Error(`UI exited: ${code}: ${output}`)); });
    server.stderr.on("data", (chunk) => {
      output += chunk;
      const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/#launch=[A-Za-z0-9_-]+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
  });
  browser = await puppeteer.launch({ headless: "shell" });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(url, { waitUntil: "networkidle0" });
  await page.locator(".main-nav button ::-p-text(Learn)").click();

  async function openLesson(key) {
    await page.waitForSelector(`.lesson-card[data-lesson="${key}"]`);
    await page.click(`.lesson-card[data-lesson="${key}"]`);
    const handle = await page.waitForSelector(".lesson-frame");
    const frame = await handle.contentFrame();
    await frame.waitForFunction((k) => document.body && document.body.dataset.lesson === k &&
      typeof teachGuide !== "undefined" && teachGuide && document.querySelector(".guide-bar"), {}, key);
    // Faster steps keep the suite short; the step logic is the same.
    await frame.select("#sel-speed", "4");
    return frame;
  }
  const guideOf = (frame) => frame.evaluate(() => JSON.parse(document.getElementById("lesson-guide").textContent));
  const view = (frame) => frame.evaluate(() => ({
    count: document.querySelector(".guide-count").textContent,
    lines: [...document.querySelectorAll(".code-line.is-active")].map((li) => li.dataset.line),
    current: [...document.querySelectorAll('.code-line[aria-current="step"]')].length,
    caption: document.getElementById("phase-label").textContent,
    state: teachGuide.state(),
  }));
  async function step(frame, selector) {
    await frame.click(selector);
    await frame.waitForFunction(() => {
      const s = teachGuide.state();
      return s.phase === "held" || s.finished || s.beat < 0;
    }, { timeout: 15000 });
    return view(frame);
  }
  const stage = (frame) => frame.evaluate(() => {
    const svg = document.querySelector(".stage-with-phases svg");
    const r = svg.getBoundingClientRect();
    const side = document.querySelector(".stage-side").getBoundingClientRect();
    const toolbar = document.querySelector(".stage-toolbar").getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, right: r.right, height: r.height, span: r.bottom - toolbar.top,
      sideTop: side.top, sideLeft: side.left, sideBottom: side.bottom, sideHeight: side.height,
      vh: innerHeight, vw: innerWidth, overflow: document.scrollingElement.scrollWidth > innerWidth + 1 };
  });

  // The stage comes first. At 1440x900 a lesson opens (no scrolling) with
  // its parameters, toolbar, scrubber, step bar and whole drawing on screen;
  // About this example (query and explainer) follows the stage.
  const opening = (frame) => frame.evaluate(() => {
    const box = (el) => el.getBoundingClientRect();
    const svgs = [...document.querySelectorAll(".stage-with-phases svg")]
      .filter((s) => s.getClientRects().length && !s.closest("button"));
    const top = Math.min(...svgs.map((s) => box(s).top));
    const bottom = Math.max(...svgs.map((s) => box(s).bottom));
    const controls = document.querySelector("section.controls");
    const stageSection = document.querySelector("section.stage");
    const about = document.getElementById("about-example");
    const follows = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const toolbar = box(document.querySelector(".stage-toolbar"));
    return {
      scrolled: scrollY,
      order: follows(controls, stageSection) && follows(stageSection, about) &&
        !stageSection.querySelector(".query-card, .explainer-card") &&
        !!about.querySelector(".query-card") && !!about.querySelector(".explainer-card"),
      controls: box(controls).top >= 0 && box(controls).bottom <= toolbar.top,
      toolbar: toolbar.top >= 0,
      scrubber: box(document.querySelector(".stage-scrubber")).bottom <= innerHeight,
      bar: box(document.querySelector(".guide-bar")).bottom <= innerHeight,
      drawing: top >= 0 && bottom <= innerHeight + 0.5,
    };
  });
  const STAGE_FIRST = { scrolled: 0, order: true, controls: true, toolbar: true, scrubber: true, bar: true, drawing: true };

  // --- btree, the index family's flagship: step forward and back.
  let frame = await openLesson("btree");
  assert.deepEqual(await opening(frame), STAGE_FIRST, "btree opens with the stage first at 1440x900");
  assert.equal(await page.$eval(".teach-step-position", (el) => el.textContent), "Lesson 2 of 16",
    "the learning path says Lesson, the step bar says Step");
  const btree = await guideOf(frame);
  let now = await view(frame);
  assert.equal(now.count, "8 steps");
  assert.deepEqual(now.lines, [], "nothing is highlighted before the first step");
  assert.equal(await frame.$eval(".guide-next", (el) => el.classList.contains("primary")), true, "Next step leads");
  assert.equal(await frame.$eval("#btn-play", (el) => el.classList.contains("primary")), false);
  assert.equal(await frame.$eval(".code-tag", (el) => el.textContent), "Simplified");
  now = await step(frame, ".guide-next");
  assert.equal(now.count, "Step 1 of 8");
  assert.deepEqual(now.lines, ["root"]);
  assert.match(now.caption, /page read 1 of 6: root page/);
  now = await step(frame, ".guide-next");
  assert.equal(now.count, "Step 2 of 8");
  assert.deepEqual(now.lines, ["loop", "child"], "the highlight moves with the step");
  assert.equal(now.current, 2);
  assert.match(now.caption, /page read 2 of 6: internal page/);
  now = await step(frame, ".guide-back");
  assert.equal(now.count, "Step 1 of 8");
  assert.deepEqual(now.lines, ["root"], "Back moves the highlight back");
  assert.match(now.caption, /root page/);
  now = await step(frame, ".guide-back");
  assert.equal(now.count, "8 steps");
  assert.deepEqual(now.lines, []);
  assert.equal(await frame.$eval(".guide-back", (el) => el.disabled), true);

  // Wide window: the code sits beside the drawing; drawing and code fit.
  let fit = await stage(frame);
  assert.ok(fit.sideLeft >= fit.right - 0.5, `the code is beside the drawing at 1440: ${JSON.stringify(fit)}`);
  assert.ok(fit.height <= fit.vh && fit.span <= fit.vh, `the drawing fits with the code open: ${JSON.stringify(fit)}`);
  assert.ok(fit.sideHeight <= fit.vh, `the code column fits too: ${JSON.stringify(fit)}`);

  // Every step the timeline marks names lines in the guide, in both lookups.
  for (const type of ["secondary_noncovering", "pk"]) {
    await frame.select("#key_type", type);
    const kinds = await frame.evaluate(() => {
      const out = [];
      const total = teachRuntime.stepState().count;
      for (let k = 0; k < total; k++) {
        teachRuntime.stepTo(k);
        out.push(teachRuntime.stepState().kind);
      }
      teachRuntime.stepTo(-1);
      return out;
    });
    for (const kind of kinds) assert.ok(btree.beats[kind], `btree step ${kind} (${type}) has lines`);
  }
  // The covering secondary lookup draws the covering index it walks (it once
  // drew the clustered tree "looking for id=42").
  await frame.select("#key_type", "secondary_covering");
  const covering = await frame.evaluate(() => {
    teachRuntime.stepTo(0);
    const first = { caption: document.getElementById("phase-label").textContent,
      heading: document.querySelector("#btree-svg text").textContent };
    const last = teachRuntime.stepState().count - 1;
    teachRuntime.stepTo(last);
    const out = { first, kind: teachRuntime.stepState().kind,
      lines: [...document.querySelectorAll(".code-line.is-active")].map((li) => li.dataset.line) };
    teachRuntime.stepTo(-1);
    return out;
  });
  assert.match(covering.first.heading, /^Covering secondary B\+tree/);
  assert.match(covering.first.caption, /covering index \(secondary\), page read 1 of 3: root page/);
  assert.equal(covering.kind, "covered");
  assert.deepEqual(covering.lines, ["covers", "ret"]);
  await frame.select("#key_type", "secondary_noncovering");
  // A parameter change shows on the drawing without scrolling.
  assert.deepEqual(await opening(frame), STAGE_FIRST, "btree after changing the lookup type");

  // To the end with Next step: Check yourself appears and takes the focus
  // from Next step, which is done.
  await frame.focus(".guide-next");
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press("Enter");
    await frame.waitForFunction(() => { const s = teachGuide.state(); return s.phase === "held" || s.finished; }, { timeout: 15000 });
  }
  now = await view(frame);
  assert.equal(now.count, "Step 8 of 8, done");
  assert.deepEqual(now.lines, ["ret"]);
  assert.equal(await frame.$eval(".guide-next", (el) => el.disabled), true);
  assert.equal(await frame.$eval(".guide-check", (el) => !el.hidden), true);
  assert.equal(await frame.evaluate(() => document.activeElement.classList.contains("guide-check")), true);

  // Check yourself: a wrong answer, then the right one, each with its reason.
  await page.keyboard.press("Enter");
  await frame.waitForFunction(() => document.querySelector(".quiz-dialog").open);
  assert.equal(await frame.evaluate(() => document.activeElement.name), "quiz-0", "focus starts on the first question");
  const q1 = btree.quiz[0];
  const wrong = q1.choices.findIndex((c) => !c.correct);
  const right = q1.choices.findIndex((c) => c.correct);
  await frame.click(`.quiz-q input[name="quiz-0"][value="${wrong}"]`);
  let feedback = await frame.$eval("#quiz-feedback-0", (el) => el.textContent);
  assert.equal(feedback, "Not quite. " + q1.choices[wrong].why);
  assert.equal(await frame.$eval(`.quiz-q input[name="quiz-0"][value="${wrong}"]`, (el) => el.closest("label").className), "quiz-choice is-wrong");
  // Keyboard: the arrow keys move between choices and answer at once.
  await frame.focus(`.quiz-q input[name="quiz-0"][value="${wrong}"]`);
  const keys = right > wrong ? "ArrowDown" : "ArrowUp";
  for (let i = 0; i < Math.abs(right - wrong); i++) await page.keyboard.press(keys);
  feedback = await frame.$eval("#quiz-feedback-0", (el) => el.textContent);
  assert.equal(feedback, "Right. " + q1.choices[right].why);
  assert.equal(await frame.$eval(".quiz-tally", (el) => el.textContent), "1 of 3 right");
  // Escape closes the quiz and gives the focus back.
  await page.keyboard.press("Escape");
  await frame.waitForFunction(() => !document.querySelector(".quiz-dialog").open);
  assert.equal(await frame.evaluate(() => document.activeElement.classList.contains("guide-check")), true);

  // Explore: a slider over every step replaces the time scrubber; inputs
  // change and the same step is shown again; leaving keeps that step.
  await frame.click(".guide-explore");
  assert.equal(await frame.evaluate(() => document.documentElement.dataset.explore), "true");
  assert.equal(await frame.$eval(".guide-explore", (el) => el.getAttribute("aria-pressed")), "true");
  assert.equal(await frame.$eval(".stage-scrubber", (el) => getComputedStyle(el).display), "none");
  assert.equal(await frame.$eval(".guide-beat", (el) => getComputedStyle(el).display !== "none" && el.max), "8");
  await frame.$eval(".guide-beat", (el) => { el.value = "3"; el.dispatchEvent(new Event("input", { bubbles: true })); });
  now = await view(frame);
  assert.equal(now.count, "Step 3 of 8");
  assert.deepEqual(now.lines, ["child", "rec"]);
  await frame.$eval(".guide-beat", (el) => { el.value = "6"; el.dispatchEvent(new Event("input", { bubbles: true })); });
  assert.deepEqual((await view(frame)).lines, ["loop", "child", "clus"], "any step, in any order");
  await frame.$eval(".guide-beat", (el) => { el.value = "3"; el.dispatchEvent(new Event("input", { bubbles: true })); });
  await frame.click(".guide-inputs-toggle");
  assert.equal(await frame.evaluate(() => !!document.querySelector(".guide-bar .guide-inputs section.controls #rows")), true);
  await frame.select("#key_type", "pk");
  now = await view(frame);
  assert.equal(now.count, "Step 3 of 4", "the same step with the new inputs");
  assert.deepEqual(now.lines, ["child", "rec"]);
  assert.match(now.caption, /users PK \(clustered\), page read 3 of 3: leaf page/);
  now = await step(frame, ".guide-replay");
  assert.equal(now.count, "Step 3 of 4", "Replay step plays the step again");
  fit = await stage(frame);
  assert.ok(fit.height <= fit.vh, `the drawing fits while exploring: ${JSON.stringify(fit)}`);
  await frame.click(".guide-explore");
  assert.equal(await frame.evaluate(() => document.documentElement.dataset.explore || ""), "");
  assert.equal(await frame.evaluate(() => document.querySelector("section.controls").parentElement.id), "lesson-main",
    "the inputs return to the page");
  assert.notEqual(await frame.$eval(".stage-scrubber", (el) => getComputedStyle(el).display), "none");
  now = await view(frame);
  assert.equal(now.count, "Step 3 of 4", "the guided steps continue at the same step");
  now = await step(frame, ".guide-next");
  assert.equal(now.count, "Step 4 of 4, done");
  assert.deepEqual(now.lines, ["ret"]);

  // Light and dark: the highlighted line uses the accent wash and the
  // appearance's ink.
  async function colors() {
    return frame.evaluate(() => {
      const probe = document.createElement("div");
      probe.style.cssText = "position:absolute;background:var(--accent-soft);color:var(--ink)";
      document.body.appendChild(probe);
      const want = getComputedStyle(probe);
      const line = document.querySelector(".code-line.is-active");
      const out = { bg: getComputedStyle(line).backgroundColor, wantBg: want.backgroundColor,
        ink: getComputedStyle(line.querySelector(".code-text")).color, wantInk: want.color,
        theme: document.documentElement.dataset.theme || "" };
      probe.remove();
      return out;
    });
  }
  const luminance = (rgb) => {
    const [r, g, b] = rgb.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  };
  let light = await colors();
  assert.equal(light.bg, light.wantBg);
  assert.equal(light.ink, light.wantInk);
  assert.ok(luminance(light.ink) < 0.3, `dark text in the light appearance: ${light.ink}`);
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  await frame.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  await wait(100);
  const dark = await colors();
  assert.equal(dark.bg, dark.wantBg);
  assert.equal(dark.ink, dark.wantInk);
  assert.ok(luminance(dark.ink) > 0.7, `light text in the dark appearance: ${dark.ink}`);
  assert.notEqual(dark.bg, light.bg);
  assert.deepEqual((await view(frame)).lines, ["ret"], "the step survives the appearance change");
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);

  // What the index family's default steps must contain (regressions):
  // non_unique_lookup once ran covering for "No" ("false" is a truthy
  // string), and unique_lookup once fetched the row with one page instead
  // of a second walk of the clustered index.
  const familyKinds = {
    non_unique_lookup: (kinds) => kinds.includes("row") && !kinds.includes("row-covered") &&
      kinds[kinds.length - 1] === "stop",
    unique_lookup: (kinds) => ["clus-root", "clus-internal", "clus-leaf", "done"].every((k) => kinds.includes(k)),
    skip_scan: (kinds) => kinds.join() === "first-prefix,seek,range,next-prefix,seek,range,end",
    index_merge: (kinds) => kinds.join() === "scan-a,scan-b,merge-union,fetch",
  };

  // --- The other flagships and the index family: every step names lines,
  // and the highlight moves. nested_loop stays last: the 720 px checks below
  // continue from it.
  for (const [key, firstLines, secondLines] of [
    ["unique_lookup", ["root"], ["loop", "child"]],
    ["non_unique_lookup", ["root"], ["loop", "child"]],
    ["icp", ["open"], ["each", "off", "icp", "skip"]],
    ["index_merge", ["for", "scan"], ["for", "scan"]],
    ["skip_scan", ["first"], ["loop", "seek"]],
    ["rowid_filter", ["build", "add"], ["sort"]],
    ["full_scan", ["open"], ["read", "test", "else", "discard"]],
    ["filter", ["child"], ["loop", "test", "drop"]],
    ["covering_index", ["plan", "seek"], ["loop", "else", "hop", "send-row"]],
    ["tmp", ["create"], ["loop", "group", "insert"]],
    ["derived_table", ["create"], ["run", "write"]],
    ["filesort", ["read", "append"], ["full", "run"]],
    ["lru", ["access", "old", "old-time", "promote"], ["access", "miss", "insert"]],
    ["buffer_pool_warmup", ["run", "check", "miss", "read"], ["run", "check", "miss", "read"]],
    ["join_order", ["start"], ["loop", "each", "cost"]],
    // The rest of the join family, which follows nested_loop.
    ["hash", ["build", "hash"], ["build", "hash"]],
    ["bnl", ["block", "fill"], ["scan", "each", "test"]],
    ["join", ["bnl-block", "bnl-scan", "hash-build"], ["bnl-block", "bnl-scan", "hash-probe"]],
    ["bka_join", ["fill"], ["send", "lookup"]],
    ["semijoin_firstmatch", ["outer", "scan", "join"], ["outer", "scan", "join"]],
    ["semijoin_weedout", ["loop"], ["key", "insert", "new"]],
    ["nested_loop", ["outer"], ["probe", "inner", "emit"]],
  ]) {
    await page.locator(".teach-toolbar button ::-p-text(All lessons)").click();
    frame = await openLesson(key);
    assert.deepEqual(await opening(frame), STAGE_FIRST, `${key} opens with the stage first at 1440x900`);
    const guide = await guideOf(frame);
    now = await step(frame, ".guide-next");
    assert.deepEqual(now.lines, firstLines, key);
    now = await step(frame, ".guide-next");
    assert.deepEqual(now.lines, secondLines, key);
    const kinds = await frame.evaluate(() => {
      const out = [];
      for (let k = 0; k < teachRuntime.stepState().count; k++) {
        teachRuntime.stepTo(k);
        out.push(teachRuntime.stepState().kind);
      }
      return out;
    });
    for (const kind of kinds) assert.ok(guide.beats[kind], `${key} step ${kind} has lines`);
    if (familyKinds[key]) assert.ok(familyKinds[key](kinds), `${key} steps: ${kinds.join()}`);
    assert.equal(await frame.$eval(".guide-check", (el) => !el.hidden), true, `${key}: the last step offers Check yourself`);
    const lastCaption = () => frame.evaluate(() => {
      teachRuntime.stepTo(teachRuntime.stepState().count - 1);
      return document.getElementById("phase-label").textContent;
    });
    if (key === "lru") {
      assert.match(await lastCaption(), /InnoDB 8\/8 hits, classic 0\/8/, "lru: the default story");
      // A young sublist of 2 pages cannot hold the 8 hot pages.
      await frame.$eval("#old_pct", (el) => { el.value = "90"; el.dispatchEvent(new Event("input", { bubbles: true })); });
      assert.match(await lastCaption(), /InnoDB 2\/8 hits/, "lru: innodb_old_blocks_pct=90 loses most of the hot set");
    }
    if (key === "filesort") {
      // MariaDB radix sorts by the sort key's length (order_date: 3 bytes),
      // never the row's; MySQL has no radix sort.
      const allKinds = () => frame.evaluate(() => {
        const out = [];
        for (let k = 0; k < teachRuntime.stepState().count; k++) {
          teachRuntime.stepTo(k);
          out.push(teachRuntime.stepState().kind);
        }
        return out;
      });
      assert.equal(kinds.includes("radix-pass"), false, "filesort: MySQL never radix sorts");
      await frame.select("#server", "mariadb");
      assert.equal(await frame.$eval("#sbs", (el) => el.value), "2097152", "MariaDB's 2 MiB default");
      for (const size of ["200", "20", "4"]) {
        await frame.$eval("#row_size", (el, v) => { el.value = v; el.dispatchEvent(new Event("input", { bubbles: true })); }, size);
        const maria = await allKinds();
        assert.ok(maria.includes("radix-pass") && maria.includes("radix-gather"), `filesort: MariaDB radix sorts ${size} B rows: ${maria.join()}`);
        for (const kind of maria) assert.ok(guide.beats[kind], `filesort step ${kind} has lines`);
      }
      assert.match(await lastCaption(), /10,000 rows sorted in memory by MariaDB's radix sort/);
    }
    if (key === "buffer_pool_warmup") {
      const dump = () => frame.evaluate(() => {
        teachRuntime.stepTo(4);
        return { kind: teachRuntime.stepState().kind, caption: document.getElementById("phase-label").textContent };
      });
      let shown = await dump();
      assert.equal(shown.kind, "dump");
      assert.match(shown.caption, /IDs of the 2 most recently used pages/, "MySQL: 25% of the 10 cached pages");
      await frame.select("#server", "mariadb");
      shown = await dump();
      assert.match(shown.caption, /IDs of the 6 most recently used pages/, "MariaDB: at most 25% of the 24-page pool");
      assert.match(await lastCaption(), /finds 6 of its 10 pages in memory/);
    }
    if (key === "join_order") {
      // optimizer_prune_level is a select; "0" must reach the timeline as 0,
      // and stepping must keep the search depth pill at "62 (max)".
      const withPrune = await frame.evaluate(() => teachRuntime.stepState().count);
      await frame.select("#prune_level", "0");
      const run = await frame.evaluate(() => {
        const kinds = [];
        for (let k = 0; k < teachRuntime.stepState().count; k++) {
          teachRuntime.stepTo(k);
          kinds.push(teachRuntime.stepState().kind);
        }
        return { kinds, counter: stageState.plansCounter.textContent,
          readout: document.getElementById("out-plans").textContent,
          pill: document.querySelector('[data-pill-for="search_depth"]').textContent };
      });
      assert.equal(run.kinds.includes("prune"), false, "prune_level=0 has no prune step");
      assert.equal(run.kinds.length, withPrune - 1);
      assert.equal(run.counter, run.readout, "the stage counter matches the readout");
      assert.equal(run.pill, "62 (max)");
      await frame.select("#algo", "hypergraph");
      const kinds = await frame.evaluate(() => {
        const out = [];
        for (let k = 0; k < teachRuntime.stepState().count; k++) {
          teachRuntime.stepTo(k);
          out.push(teachRuntime.stepState().kind);
        }
        return out;
      });
      assert.deepEqual(kinds, ["hg-tables", "hg-pairs", "hg-grow", "hg-grow", "hg-grow", "hg-grow", "hg-done"]);
      for (const kind of kinds) assert.ok(guide.beats[kind], `join_order hypergraph step ${kind} has lines`);
    }
  }

  // --- 720 px window: the code goes under the drawing; everything fits.
  await page.setViewport({ width: 720, height: 520 });
  await wait(200);
  fit = await stage(frame);
  assert.ok(fit.sideTop >= fit.bottom - 0.5, `the code is under the drawing at 720: ${JSON.stringify(fit)}`);
  // The parameters, the toolbar and the top of the drawing stay on screen.
  const narrow = await opening(frame);
  assert.ok(narrow.scrolled === 0 && narrow.order && narrow.controls && narrow.toolbar && fit.top < fit.vh - 40,
    `the stage comes first at 720: ${JSON.stringify(narrow)} ${JSON.stringify(fit)}`);
  assert.ok(fit.height <= fit.vh && fit.span <= fit.vh, `the drawing fits at 720: ${JSON.stringify(fit)}`);
  assert.equal(fit.overflow, false, "no sideways scrolling at 720");
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  assert.equal(await frame.$eval(".guide-bar", (el) => {
    const r = el.getBoundingClientRect();
    return r.left >= 0 && r.right <= innerWidth + 0.5;
  }), true, "the step bar fits the window");
  now = await step(frame, ".guide-back");
  assert.deepEqual(now.lines, ["probe", "inner", "emit"], "stepping works at 720");

  // --- Full screen (the overlay used when the browser refuses): the step
  // bar and the code come along; Escape in the quiz closes only the quiz.
  await page.setViewport({ width: 1440, height: 900 });
  await page.evaluate(() => {
    Element.prototype.requestFullscreen = function () { return Promise.reject(new TypeError("refused")); };
  });
  await page.click(FULL);
  await frame.waitForFunction(() => document.documentElement.dataset.focus === "true");
  await wait(150);
  const focusLayout = await frame.evaluate(() => {
    const svg = document.querySelector(".focus-stage .stage-with-phases svg").getBoundingClientRect();
    const side = document.querySelector(".focus-stage .stage-side").getBoundingClientRect();
    return { bar: !!document.querySelector(".focus-shell .focus-top .guide-bar .guide-next"),
      side: side.left >= svg.right - 0.5 && side.bottom <= innerHeight + 0.5,
      lines: [...document.querySelectorAll(".focus-stage .code-line.is-active")].map((li) => li.dataset.line).join(),
      svgOnScreen: svg.bottom <= innerHeight + 0.5 && svg.right <= innerWidth + 0.5,
      scroll: document.scrollingElement.scrollHeight <= innerHeight };
  });
  assert.deepEqual(focusLayout, { bar: true, side: true, lines: "probe,inner,emit", svgOnScreen: true, scroll: true });
  now = await step(frame, ".guide-next");
  assert.deepEqual(now.lines, ["end"], "stepping works in full screen");
  await frame.click(".guide-explore");
  await frame.waitForFunction(() => document.documentElement.dataset.explore === "true");
  await frame.click(".guide-explore");
  await frame.evaluate(() => teachGuide.openQuiz());
  await frame.waitForFunction(() => document.querySelector(".quiz-dialog").open);
  await page.keyboard.press("Escape");
  await frame.waitForFunction(() => !document.querySelector(".quiz-dialog").open);
  assert.equal(await frame.evaluate(() => document.documentElement.dataset.focus), "true", "the lesson stays full screen");
  await page.click(".lesson-focus-bar button");
  await frame.waitForFunction(() => document.documentElement.dataset.focus !== "true");
  assert.equal(await frame.evaluate(() => !!document.querySelector("main .guide-bar") &&
    !!document.querySelector("main .stage-with-phases .stage-side")), true, "the step bar and code return to the page");

  assert.deepEqual(errors, []);
  console.log("Learn guide checks passed (stage first in every lesson, steps forward and back, code highlight, quiz, Explore with inputs, light and dark, 720 px, full screen).");
} finally {
  await browser?.close();
  server.kill();
}
