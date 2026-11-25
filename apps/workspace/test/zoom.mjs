// Real SVG geometry and animation frames catch trackpad regressions that a
// mocked DOM cannot: wheel distance, momentum, cursor anchors, and controls.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import puppeteer from 'puppeteer';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-zoom-'));
execFileSync(process.env.PYTHON || 'python3', ['-c', `
import sys
from pathlib import Path
from myflames.exploration import render_exploration
from myflames.output_workbench import render_workbench
from myflames.parser import parse_explain
root = parse_explain(Path('test/fixtures/explain-044-join-3t-products-category-reviews.json').read_text())
Path(sys.argv[1], 'plan.html').write_text(render_exploration(root, 'workbench'))
Path(sys.argv[1], 'plan.svg').write_text(render_workbench(root))
`, directory], { cwd: root });
const browser = await puppeteer.launch({ headless: 'shell' });
try {
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 1200 });
  const errors = [];
  page.on('pageerror', error => errors.push(String(error)));
  await page.setContent(await fs.readFile(path.join(directory, 'plan.html'), 'utf8'));
  const box = () => page.$eval('.wb-viewport', el => el.getAttribute('viewBox').split(' ').map(Number));
  const fit = () => page.$eval('[data-action="fit"]', el => el.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  const settle = () => page.evaluate(() => new Promise(resolve => setTimeout(resolve, 700)));
  const wheel = (deltaY, deltaMode = 0, count = 1, deltaX = 0) => page.$eval('.wb-viewport', (el, input) => {
    let point = el.createSVGPoint();
    point.x = el.x.baseVal.value + el.width.baseVal.value * .35;
    point.y = el.y.baseVal.value + el.height.baseVal.value * .4;
    point = point.matrixTransform(el.ownerSVGElement.getScreenCTM());
    // WheelEvent stores integer CSS coordinates.
    point.x = Math.round(point.x); point.y = Math.round(point.y);
    const anchor = point.matrixTransform(el.getScreenCTM().inverse());
    const before = el.getAttribute('viewBox');
    let prevented;
    for (let i = 0; i < input.count; i++) {
      const event = new WheelEvent('wheel', { deltaY: input.deltaY, deltaX: input.deltaX,
        deltaMode: input.deltaMode, clientX: point.x, clientY: point.y, cancelable: true });
      el.dispatchEvent(event);
      prevented = event.defaultPrevented;
    }
    return { anchor: [anchor.x, anchor.y], client: [point.x, point.y], prevented,
      synchronous: before === el.getAttribute('viewBox') };
  }, { deltaY, deltaMode, count, deltaX });

  const initial = await box();
  const readout = () => page.$eval('[data-action="reset"]', el => el.textContent);
  const expectedZoom = async () => Math.round(1200 / (await box())[2] * 100) + '%';
  assert.equal(await readout(), await expectedZoom(), 'Fit reports the actual scale');
  // The plan is taller than the viewport: fit stops at readable text (11 px,
  // the floor) and starts at the first input instead of shrinking to 45%.
  const smallestText = () => page.$eval('.wb-viewport', el => Math.min(...Array.from(el.querySelectorAll('text'),
    t => parseFloat(getComputedStyle(t).fontSize) * Math.hypot(t.getScreenCTM().a, t.getScreenCTM().b))));
  assert.ok(await smallestText() >= 10.99, 'fit keeps operator text at 11 px or more: ' + await smallestText());
  assert.equal(initial[1], -28, 'a plan too tall to fit readably starts at its first input');
  await page.click('[data-action="reset"]');
  assert.equal(await readout(), '100%');
  await page.click('[data-action="in"]');
  assert.equal(await readout(), '125%');
  assert.ok(await page.$('[data-action="in"].wb-activated'), 'click has visible feedback');
  await page.click('[data-action="out"]');
  assert.equal(await readout(), '100%');
  for (let i = 0; i < 20; i++) {
    await page.$eval('[data-action="in"]', el => el.click());
  }
  assert.equal(await readout(), '500%');
  assert.ok(await page.$('[data-action="in"]:disabled'), 'upper limit is visible');
  await page.click('[data-action="reset"]');
  assert.ok(await page.$('[data-action="in"]:enabled'));
  await fit();
  assert.equal(await readout(), await expectedZoom());
  const tiny = await wheel(1);
  assert.equal(tiny.prevented, true);
  assert.ok(tiny.synchronous, 'wheel updates are batched into animation frames');
  await settle();
  assert.ok((await box())[2] / initial[2] < 1.002, 'a one-pixel event must not jump by a full notch');

  await fit();
  const anchor = await wheel(48);
  await settle();
  const pixels = await box();
  assert.equal(await readout(), await expectedZoom(), 'wheel updates the percentage too');
  assert.ok(Math.abs(pixels[2] / initial[2] - Math.exp(.048)) < .0001);
  const anchored = await page.$eval('.wb-viewport', (el, coords) => {
    const p = el.createSVGPoint(); p.x = coords[0]; p.y = coords[1];
    const q = p.matrixTransform(el.getScreenCTM().inverse());
    return [q.x, q.y];
  }, anchor.client);
  anchored.forEach((coordinate, i) => assert.ok(Math.abs(coordinate - anchor.anchor[i]) < .001, 'zoom preserves the point beneath the cursor: ' + JSON.stringify({ anchored, anchor, initial, pixels })));

  await fit(); await wheel(3, 1); await settle();
  assert.ok(Math.abs((await box())[2] - pixels[2]) < .01, 'line and pixel deltas agree');
  await fit();
  const viewportHeight = await page.$eval('.wb-viewport', el => el.height.baseVal.value * Math.abs(el.ownerSVGElement.getScreenCTM().d));
  await wheel(48 / viewportHeight, 2); await settle();
  assert.ok(Math.abs((await box())[2] - pixels[2]) < .01, 'page and pixel deltas agree');

  await fit();
  assert.equal((await wheel(0)).prevented, false);
  assert.equal((await wheel(1, 0, 1, 80)).prevented, false);
  await settle();
  assert.deepEqual(await box(), initial, 'horizontal and zero deltas do not zoom');

  await wheel(10000, 0, 80);
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.ok((await box())[2] / initial[2] < 1.05, 'a momentum burst advances gently per frame');
  await settle();
  assert.ok((await box())[2] / initial[2] < 1.175, 'queued momentum has a bounded target');
  await fit(); await wheel(-48); await settle();
  assert.ok(Math.abs((await box())[2] / initial[2] - Math.exp(-.048)) < .0001);

  await wheel(10000, 0, 80); await fit(); await settle();
  assert.deepEqual(await box(), initial, 'Fit cancels pending wheel motion');
  await wheel(10000, 0, 80);
  const beforeButton = await page.$eval('[data-action="in"]', el => {
    const viewport = document.querySelector('.wb-viewport');
    const width = Number(viewport.getAttribute('viewBox').split(' ')[2]);
    // Mid-gesture in the workspace the viewBox still shows the gesture's
    // first frame and a CSS transform on the root carries the zoom
    // (core/zoom.ts); the effective width is the viewBox width divided by
    // that scale. Report pages paint the viewBox per frame: scale 1.
    const transform = (viewport.ownerSVGElement && viewport.ownerSVGElement.style.transform) || '';
    const scale = Number((transform.match(/scale\(([^)]+)\)/) || [])[1] || 1);
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    return width / scale;
  });
  const buttonBox = await box();
  await settle();
  assert.deepEqual(await box(), buttonBox, 'toolbar actions cancel pending wheel motion');
  assert.ok(Math.abs(buttonBox[2] / beforeButton - .8) < .0001);

  // A programmatic zoom during an active wheel gesture settles the gesture
  // first (core/zoom.ts zoom calls stopWheel, like fit/reset/resize/show):
  // no CSS transform stays on the root over the painted viewBox. The
  // controller comes from the bundle's test surface (charts/index.ts
  // internals); the toolbar path above cannot reach it without its own
  // stopWheel.
  const midGesture = await page.evaluate(() => new Promise((resolve) => {
    const NS = 'http://www.w3.org/2000/svg';
    const root = document.createElementNS(NS, 'svg');
    root.setAttribute('width', '800');
    root.setAttribute('height', '600');
    root.setAttribute('viewBox', '0 0 800 600');
    root.style.cssText = 'position:fixed;left:0;top:0';
    const viewport = document.createElementNS(NS, 'svg');
    viewport.setAttribute('width', '800');
    viewport.setAttribute('height', '600');
    viewport.setAttribute('viewBox', '0 0 800 600');
    root.appendChild(viewport);
    document.body.appendChild(root);
    const buttons = {};
    for (const name of ['fit', 'zoomIn', 'zoomOut', 'reset']) buttons[name] = document.createElement('button');
    const controller = window.MyflamesCharts.internals.createZoom({
      root, viewport, width: 800, height: 600, graphWidth: 2000, graphHeight: 1000,
      buttons, wheelTransform: true,
    });
    const initialBox = viewport.getAttribute('viewBox');
    viewport.dispatchEvent(new WheelEvent('wheel', { deltaY: 96, clientX: 400, clientY: 300, cancelable: true }));
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const transformDuring = root.style.transform;
      controller.zoom(0.8, 0.5, 0.5);
      const result = {
        initialBox, transformDuring,
        transformAfter: root.style.transform,
        box: viewport.getAttribute('viewBox'),
        readout: buttons.reset.textContent,
      };
      controller.destroy();
      root.remove();
      resolve(result);
    }));
  }));
  assert.ok(/scale\(/.test(midGesture.transformDuring), 'the wheel gesture put a transform on the root');
  assert.equal(midGesture.transformAfter, '', 'zoom during a wheel gesture leaves no transform on the root');
  const midBox = midGesture.box.split(' ').map(Number);
  assert.ok(midBox[2] < 800, 'zoom during a wheel gesture zoomed in: ' + midGesture.box);
  assert.notEqual(midGesture.box, midGesture.initialBox, 'the viewBox caught up with the zoom');
  assert.equal(midGesture.readout, Math.round(800 / midBox[2] * 100) + '%', 'the readout matches the painted viewBox');

  // Zooming out by hand still reaches the whole plan, below the fit's floor.
  await fit();
  for (let i = 0; i < 4; i++) await page.$eval('[data-action="out"]', el => el.click());
  assert.ok(await smallestText() < 11, 'zoom out goes past the readable fit on request');
  await fit();
  assert.deepEqual(await box(), initial, 'Fit returns to the readable opening view');

  await fit();
  const rect = await page.$eval('.wb-viewport', el => {
    const p = el.createSVGPoint();
    p.x = el.x.baseVal.value + el.width.baseVal.value * .8;
    p.y = el.y.baseVal.value + el.height.baseVal.value * .7;
    const q = p.matrixTransform(el.ownerSVGElement.getScreenCTM());
    return { x: q.x, y: q.y };
  });
  await page.mouse.move(rect.x, rect.y);
  await page.mouse.down();
  await page.mouse.move(rect.x + 40, rect.y + 20, { steps: 3 });
  await page.mouse.up();
  const panned = await box();
  assert.equal(panned[2], initial[2]);
  assert.ok(panned[0] < initial[0] && panned[1] < initial[1], 'dragging still pans');
  await page.$eval('.wb-node', el => el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })));
  assert.ok(await page.$('.wb-node.selected'), 'keyboard selection remains available');

  // The standalone SVG (output_workbench.py + workbench.js) opens the same way.
  await page.goto('file://' + path.join(directory, 'plan.svg'));
  await page.waitForSelector('svg.wb-plan[data-wired="true"]');
  const standalone = await box();
  assert.ok(await smallestText() >= 10.99, 'standalone fit keeps text at 11 px or more: ' + await smallestText());
  assert.equal(standalone[1], -28, 'standalone: a tall plan starts at its first input');
  assert.equal(await readout(), '100%');
  await page.$eval('[data-action="out"]', el => el.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  assert.equal(await readout(), '80%', 'standalone zoom out still works');
  await page.$eval('[data-action="fit"]', el => el.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  assert.deepEqual(await box(), standalone, 'standalone Fit returns to the readable view');

  // Report and standalone pages scale the SVG with their column. While a
  // chart is still fitted, a change of screen scale refits it so the text
  // stays at 11 px; a deliberate zoom is kept. In a tiny embedding the
  // readable bound wins over the 500% zoom cap, and zoom in stays disabled.
  const settleLayout = () => page.evaluate(() => new Promise(resolve =>
    requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 50)))));
  const svgScale = () => page.$eval('svg.wb-plan', el => el.getScreenCTM().a);
  const click = action => page.$eval(`[data-action="${action}"]`, el => el.dispatchEvent(new MouseEvent('click', { bubbles: true })));
  const report = await fs.readFile(path.join(directory, 'plan.html'), 'utf8');
  const inline = await fs.readFile(path.join(directory, 'plan.svg'), 'utf8');
  const hosted = width => '<!doctype html><style>body{margin:0}#host>svg{display:block;max-width:100%;height:auto}</style>' +
    `<div id="host" style="width:${width}px">${inline}</div>`;
  const pages = [
    ['report', () => page.setViewport({ width: 1400, height: 1200 }).then(() => page.setContent(report)),
      () => page.setViewport({ width: 700, height: 1200 }), () => page.setViewport({ width: 1400, height: 1200 }),
      () => page.setViewport({ width: 200, height: 900 }).then(() => page.setContent(report))],
    ['standalone', () => page.setViewport({ width: 1400, height: 1200 }).then(() => page.setContent(hosted(1200))),
      () => page.$eval('#host', el => { el.style.width = '600px'; }), () => page.$eval('#host', el => { el.style.width = '1200px'; }),
      () => page.setContent(hosted(150))],
  ];
  await page.goto('about:blank');  // leave the SVG document: setContent needs HTML
  for (const [name, open, shrink, grow, tiny] of pages) {
    await open();
    await page.waitForSelector('svg.wb-plan[data-wired="true"]');
    await settleLayout();
    assert.equal(await readout(), '100%', name + ': opens fitted at 100%');
    await shrink();
    await settleLayout();
    assert.ok(await svgScale() < 0.7, name + ': the page shrank the chart: ' + await svgScale());
    assert.ok(await smallestText() >= 10.99, name + ': a fitted chart refits when its screen scale changes: ' + await smallestText());
    await click('in');
    const deliberate = await box();
    await grow();
    await settleLayout();
    assert.deepEqual(await box(), deliberate, name + ': a deliberate zoom survives a resize');
    await tiny();
    await page.waitForSelector('svg.wb-plan[data-wired="true"]');
    await settleLayout();
    assert.ok(await svgScale() < 0.2, name + ': a tiny embedding: ' + await svgScale());
    assert.ok(await smallestText() >= 10.99, name + ': the readable fit wins over the 500% cap: ' + await smallestText());
    assert.ok(await page.$('[data-action="in"]:disabled'), name + ': manual zoom in keeps its limit');
    const tinyFit = await box();
    await wheel(-48);
    await settle();
    assert.equal((await box())[2], tinyFit[2], name + ': wheel zoom in past the cap does not jump back out');
    await click('out');
    assert.ok((await box())[2] > tinyFit[2], name + ': zoom out still works');
    await click('fit');
    assert.deepEqual(await box(), tinyFit, name + ': Fit returns to the readable view');
  }
  assert.deepEqual(errors, []);
  console.log('Zoom: readable fit (11 px floor) in the report and the standalone SVG, refit on a page resize while fitted, readable bound over the zoom cap, proportional pixel/line/page input, smooth bounded momentum, cursor anchoring, controls, pan, and selection passed');
} finally {
  await browser.close();
  await fs.rm(directory, { recursive: true, force: true });
}
