// Learn: a lesson opens with the stage first (parameters, toolbar and
// drawing on screen, About this example under the stage), its animation fits
// the window, and Full screen shows only the animation (its controls,
// caption and numbers). Covers the browser's full
// screen, the overlay used when the request is refused (a browser that
// denies it), a request that settles after the lesson was left, Escape and
// focus return, Tab staying inside, the message checks on both sides, and
// both appearances. Runs against the packaged UI and server.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), "myflames-learn-fullscreen-"));
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
  await page.waitForSelector('.lesson-card[data-lesson="full_scan"]');
  await page.locator("button ::-p-text(Start learning)").click();
  await page.waitForSelector(".lesson-frame");
  const frame = await (await page.$(".lesson-frame")).contentFrame();
  await frame.waitForSelector("#btn-play");
  await frame.waitForFunction(() => typeof teachFocus !== "undefined");

  // The button: icon and label, its accessible name, and a frame allowed to
  // go full screen.
  const button = await page.$eval(FULL, (el) => ({ text: el.textContent.trim(), icon: !!el.querySelector("svg"), disabled: el.disabled }));
  assert.deepEqual(button, { text: "Full screen", icon: true, disabled: false });
  assert.equal(await page.$eval(".lesson-frame", (el) => el.getAttribute("allow")), "fullscreen");
  // Embedded, the lesson's own Full screen button gives way to the workspace's.
  assert.equal(await frame.$eval("#btn-fullscreen", (el) => getComputedStyle(el).display), "none");
  assert.equal(await frame.$eval("#btn-details", (el) => getComputedStyle(el).display), "none");

  // Fit: the lesson fills the window under the toolbar exactly, and the stage
  // drawing is never taller than the frame; toolbar and drawing fit together.
  const stage = () => frame.evaluate(() => {
    const svg = document.querySelector(".stage-with-phases svg");
    const r = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    const scale = Math.min(r.width / vb.width, r.height / vb.height);
    const toolbar = document.querySelector(".stage-toolbar").getBoundingClientRect();
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height, drawnH: vb.height * scale,
      drawnW: vb.width * scale, width: r.width, span: r.bottom - toolbar.top, vw: innerWidth, vh: innerHeight };
  });
  // The stage comes first: the page opens (no scrolling) with the parameters,
  // the toolbar and the top of the drawing on screen; at 1440x900 the whole
  // drawing and the step bar too. About this example follows the stage.
  const opening = () => frame.evaluate(() => {
    const box = (el) => el.getBoundingClientRect();
    const svg = box(document.querySelector(".stage-with-phases svg"));
    const toolbar = box(document.querySelector(".stage-toolbar"));
    const controls = document.querySelector("section.controls");
    const about = document.getElementById("about-example");
    const follows = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    return { scrolled: scrollY, toolbar: toolbar.top >= 0 && toolbar.bottom <= innerHeight,
      controls: box(controls).top >= 0 && box(controls).bottom <= toolbar.top,
      drawingTop: svg.top < innerHeight - 40, whole: svg.bottom <= innerHeight + 0.5,
      bar: box(document.querySelector(".guide-bar")).bottom <= innerHeight,
      order: follows(controls, document.querySelector("section.stage")) && follows(document.querySelector("section.stage"), about),
      about: box(about).top >= svg.bottom };
  });
  for (const [width, height] of [[1440, 900], [720, 600], [720, 520]]) {
    await page.setViewport({ width, height });
    await wait(150);
    const frameBox = await page.$eval(".lesson-frame", (el) => ({ bottom: el.getBoundingClientRect().bottom, window: innerHeight }));
    assert.ok(frameBox.bottom <= frameBox.window + 0.5, `the lesson ends on screen at ${width}x${height}: ${JSON.stringify(frameBox)}`);
    const fit = await stage();
    assert.ok(fit.height <= fit.vh, `drawing ${fit.height} px fits the ${fit.vh} px frame at ${width}x${height}`);
    assert.ok(fit.span <= fit.vh, `toolbar and drawing (${fit.span} px) fit the ${fit.vh} px frame at ${width}x${height}`);
    // The lesson bar is one control high (thinner than a view toolbar).
    assert.equal(await page.evaluate(() => document.querySelector(".teach-toolbar").getBoundingClientRect().height < 44), true,
      `the Learn toolbar keeps one thin row at ${width}x${height}`);
    const open = await opening();
    const wide = width === 1440;
    assert.deepEqual(open, { scrolled: 0, toolbar: true, controls: true, drawingTop: true, order: true, about: true,
      whole: wide || open.whole, bar: wide || open.bar }, `the stage comes first at ${width}x${height}`);
  }
  // "Lesson", never "Step", for the learning path (the step bar says "Step").
  assert.equal(await page.$eval(".teach-step-position", (el) => el.textContent), "Lesson 1 of 16");
  await page.setViewport({ width: 1440, height: 900 });
  await wait(150);
  // About this example: the link in the parameters scrolls the lesson to it
  // and moves focus to its heading; the frame never navigates away (a plain
  // "#" link in an srcdoc frame would load the workspace's own address).
  await frame.click(".about-link");
  // It scrolls as far as the page allows: the section's top reaches the top
  // of the frame unless the page ends first.
  await frame.waitForFunction(() => {
    const top = document.getElementById("about-example").getBoundingClientRect().top;
    const end = Math.ceil(scrollY + innerHeight) >= document.scrollingElement.scrollHeight - 1;
    return scrollY > 0 && top >= -1 && (top < 2 || (end && top < innerHeight / 2));
  }, { timeout: 5000 });
  assert.deepEqual(await frame.evaluate(() => ({ lesson: document.body.dataset.lesson, focus: document.activeElement.id })),
    { lesson: "full_scan", focus: "about-example-h" });
  await frame.evaluate(() => window.scrollTo(0, 0));

  async function enter(expectMode) {
    await page.click(FULL);
    await frame.waitForFunction(() => document.documentElement.dataset.focus === "true");
    await page.waitForFunction((mode) => document.querySelector(".lesson-host")?.classList.contains("is-focus") &&
      (mode === "overlay") === document.querySelector(".lesson-host").classList.contains("is-overlay"), {}, expectMode);
    await wait(100);
  }
  async function left() {
    await page.waitForFunction(() => !document.querySelector(".lesson-host.is-focus") && !document.fullscreenElement);
    await frame.waitForFunction(() => document.documentElement.dataset.focus !== "true" && !document.querySelector(".focus-shell"));
    await page.waitForFunction((selector) => document.activeElement === document.querySelector(selector), {}, FULL);
  }
  async function focusLayout(label) {
    const fit = await stage();
    assert.ok(fit.top >= 0 && fit.left >= 0 && fit.bottom <= fit.vh + 0.5 && fit.right <= fit.vw + 0.5, `${label}: the drawing is on screen ${JSON.stringify(fit)}`);
    assert.ok(fit.drawnH >= 0.6 * fit.vh || fit.drawnW >= fit.width - 1, `${label}: the drawing fills the screen ${JSON.stringify(fit)}`);
    const layout = await frame.evaluate(() => ({
      toolbar: !!document.querySelector(".focus-shell .focus-top .stage-toolbar #btn-play"),
      scrubber: !!document.querySelector(".focus-shell .focus-top #scrubber"),
      caption: !!document.querySelector(".focus-shell .focus-caption #phase-label"),
      readout: !!document.querySelector(".focus-shell .focus-strip .readout-grid #out-read"),
      details: getComputedStyle(document.getElementById("btn-details")).display !== "none",
      main: getComputedStyle(document.getElementById("lesson-main")).display,
      scroll: document.scrollingElement.scrollHeight <= innerHeight,
    }));
    assert.deepEqual(layout, { toolbar: true, scrubber: true, caption: true, readout: true, details: true, main: "none", scroll: true }, label);
  }

  // The browser's full screen (headless Chrome allows it): the lesson host is
  // the full-screen element and focus moves into the lesson.
  await enter("native");
  assert.equal(await page.evaluate(() => document.fullscreenElement === document.querySelector(".lesson-host")), true);
  assert.equal(await page.evaluate(() => document.activeElement === document.querySelector(".lesson-frame")), true);
  assert.equal(await page.$eval(".lesson-host", (el) => [el.getAttribute("role"), el.getAttribute("aria-modal")].join()), "dialog,true");
  await focusLayout("native full screen");
  // Details opens the parameters, query and explainer beside the stage, which
  // stays on screen; the first Escape closes Details, the next leaves.
  await frame.click("#btn-details");
  const details = await frame.evaluate(() => ({
    expanded: document.getElementById("btn-details").getAttribute("aria-expanded"),
    main: getComputedStyle(document.getElementById("lesson-main")).position,
    controls: !!document.querySelector("#lesson-main section.controls #rows"),
    query: !!document.querySelector("#lesson-main #about-example .query-card"),
    explainer: !!document.querySelector("#lesson-main #about-example .explainer-card"),
  }));
  assert.deepEqual(details, { expanded: "true", main: "fixed", controls: true, query: true, explainer: true });
  const beside = await stage();
  assert.ok(beside.right <= await frame.evaluate(() => document.getElementById("lesson-main").getBoundingClientRect().left) + 0.5, "the stage stays beside Details");
  await page.keyboard.press("Escape");
  await frame.waitForFunction(() => !document.documentElement.dataset.focusDetails && document.activeElement.id === "btn-details");
  assert.equal(await frame.evaluate(() => document.documentElement.dataset.focus), "true");
  await page.keyboard.press("Escape");
  await left();
  // The lesson is whole again: toolbar, caption and numbers back in place.
  assert.equal(await frame.evaluate(() => !!document.querySelector("main .stage-toolbar #phase-label") &&
    !!document.querySelector("main section.readout .readout-grid #out-read")), true);

  // Refused (a browser that denies full screen): the lesson
  // covers the window above the sidebar and toolbars, with Exit full screen.
  await page.evaluate(() => {
    Element.prototype.requestFullscreen = function () { return Promise.reject(new TypeError("Permissions check failed")); };
  });
  await enter("overlay");
  assert.equal(await page.evaluate(() => document.fullscreenElement), null);
  const cover = await page.evaluate(() => {
    const host = document.querySelector(".lesson-host");
    const r = host.getBoundingClientRect();
    const sidebar = document.elementFromPoint(40, 300);
    const toolbar = document.elementFromPoint(innerWidth - 40, 60);
    return { box: [r.left, r.top, r.width, r.height].map(Math.round), sidebar: host.contains(sidebar), toolbar: host.contains(toolbar),
      exit: document.querySelector(".lesson-focus-bar button")?.textContent.trim(), active: document.activeElement === document.querySelector(".lesson-frame") };
  });
  assert.deepEqual(cover, { box: [0, 0, 1440, 900], sidebar: true, toolbar: true, exit: "Exit full screen", active: true });
  await focusLayout("overlay");
  // Messages: the workspace ignores one that is not from the lesson's frame,
  // and the lesson ignores one that is not from its parent.
  await page.evaluate(() => window.postMessage({ type: "myflames-lesson-focus", focus: false }, "*"));
  await frame.evaluate(() => window.postMessage({ type: "myflames-lesson-focus", focus: false }, "*"));
  await wait(150);
  assert.equal(await page.$eval(".lesson-host", (el) => el.classList.contains("is-overlay")), true);
  assert.equal(await frame.evaluate(() => document.documentElement.dataset.focus), "true");
  // Tab stays inside: past the lesson's last control to Exit, and Shift+Tab
  // from Exit back into the lesson. (A guided lesson ends with its step bar
  // and simplified code, so the last control is found, not assumed.)
  await frame.evaluate(() => {
    const controls = [...document.querySelectorAll("button, input, select, [tabindex]")]
      .filter((el) => el.tabIndex >= 0 && !el.disabled && el.getClientRects().length);
    controls[controls.length - 1].focus();
  });
  await page.keyboard.press("Tab");
  await page.waitForFunction(() => document.activeElement === document.querySelector(".lesson-focus-bar button"));
  await page.keyboard.down("Shift");
  await page.keyboard.press("Tab");
  await page.keyboard.up("Shift");
  await page.waitForFunction(() => document.activeElement === document.querySelector(".lesson-frame"));
  // Escape on Exit leaves and returns focus to Full screen.
  await page.focus(".lesson-focus-bar button");
  await page.keyboard.press("Escape");
  await left();
  // Escape inside the lesson leaves too.
  await enter("overlay");
  await frame.focus("#btn-play");
  await page.keyboard.press("Escape");
  await left();
  // So does the Exit button; the animation keeps working afterwards.
  await enter("overlay");
  await frame.click("#btn-play");
  await page.click(".lesson-focus-bar button");
  await left();
  await frame.waitForFunction(() => document.getElementById("scrubber-time-current").textContent !== "0.0s");

  // Dark appearance: the focus layout uses the lesson's dark tokens.
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "dark" }]);
  await frame.waitForFunction(() => document.documentElement.dataset.theme === "dark");
  await enter("overlay");
  const dark = await frame.evaluate(() => ({
    shell: getComputedStyle(document.querySelector(".focus-shell")).backgroundColor,
    ink: getComputedStyle(document.querySelector(".focus-caption .phase-label")).color,
  }));
  assert.deepEqual(dark, { shell: "rgb(21, 23, 25)", ink: "rgb(237, 237, 238)" });
  await page.click(".lesson-focus-bar button");
  await left();

  // Leaving Learn (here through the command palette) ends full screen, and
  // the lesson opens in its normal layout on return.
  await page.emulateMediaFeatures([{ name: "prefers-color-scheme", value: "light" }]);
  await enter("overlay");
  await page.focus(".lesson-focus-bar button");
  await page.keyboard.down("Meta"); await page.keyboard.press("k"); await page.keyboard.up("Meta");
  await page.waitForSelector("dialog.command-palette[open]");
  await page.type('input[aria-label="Search commands"]', "Home");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => !document.querySelector(".lesson-host"));
  await page.locator(".main-nav button ::-p-text(Learn)").click();
  await page.waitForSelector(".lesson-frame");
  const back = await (await page.$(".lesson-frame")).contentFrame();
  await back.waitForFunction(() => typeof teachFocus !== "undefined");
  await wait(150);
  assert.equal(await page.$(".lesson-host.is-focus"), null);
  assert.equal(await back.evaluate(() => document.documentElement.dataset.focus || ""), "");

  // A request still waiting when the lesson is left changes nothing when it
  // settles: Learn reopens in its normal layout, not in full screen.
  await page.evaluate(() => {
    Element.prototype.requestFullscreen = function () {
      return new Promise((_, reject) => { window.__settleFullscreen = () => reject(new TypeError("late refusal")); });
    };
  });
  await page.click("button[aria-label='Show the lesson animation full screen']");
  await page.locator(".main-nav button ::-p-text(Home)").click();
  await page.waitForFunction(() => !document.querySelector(".lesson-host"));
  await page.evaluate(() => window.__settleFullscreen());
  await wait(150);
  await page.locator(".main-nav button ::-p-text(Learn)").click();
  await page.waitForSelector(".lesson-frame");
  await wait(300);
  assert.equal(await page.$(".lesson-host.is-focus"), null, "a late refusal does not reopen the overlay");
  assert.deepEqual(errors, []);
  console.log("Learn full screen checks passed (stage first and fit at 1440x900, 720x600 and 720x520, About link, Lesson n of 16, native, overlay, Escape, Tab, messages, dark).");
} finally {
  await browser?.close();
  server.kill();
}
