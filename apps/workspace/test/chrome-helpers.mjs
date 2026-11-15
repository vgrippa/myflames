import assert from 'node:assert/strict';
import path from 'node:path';

// Interact with the visible connection popover, never a hidden place's picker.
export async function openConnectionPicker(page) {
  const trigger = '.window-toolbar .connection-pill';
  if (await page.$eval(trigger, (e) => e.getAttribute('aria-expanded') !== 'true'))
    await page.click(trigger);
  await page.waitForSelector('.window-toolbar .picker-panel', { visible: true });
}
export async function chooseConnection(page, id) {
  await openConnectionPicker(page);
  await page.select('.window-toolbar select[aria-label="Connection"]', id);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('.window-toolbar .picker-panel'));
}
export async function connectionOption(page, name) {
  await openConnectionPicker(page);
  return page.$$eval('.window-toolbar select[aria-label="Connection"] option',
    (options, text) => options.find((e) => e.textContent === text || e.textContent.startsWith(`${text} (`)).value, name);
}
// Account and privileges: an item in the connection picker that opens a sheet.
export async function openAccountSheet(page) {
  await openConnectionPicker(page);
  await page.locator('.window-toolbar .picker-panel button ::-p-text(Account and privileges…)').click();
  await page.waitForSelector('dialog.account-sheet[open]', { visible: true });
}
export async function closeAccountSheet(page) {
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => !document.querySelector('dialog.account-sheet'));
}

// The Server and Workload list of views: docked beside the view, or floating
// (the default), opened from the view's title button.
const shown = (page, selector) =>
  page.$eval(selector, (e) => !e.closest('[hidden]') && e.offsetParent !== null).catch(() => false);
/** Open the floating list of views from the title of the view on screen. */
export async function openViewList(page) {
  if (await page.$('.view-switcher-popover')) return;
  await page.waitForFunction(() => [...document.querySelectorAll('.server-place:not(.is-tuning-place) .server-view .view-switcher')]
    .some((e) => !e.closest('[hidden]') && e.offsetParent !== null));
  for (const button of await page.$$('.server-place .server-view .view-switcher')) {
    if (await button.evaluate((e) => !e.closest('[hidden]') && e.offsetParent !== null)) { await button.click(); break; }
  }
  await page.waitForSelector('.view-switcher-popover .source-list [aria-current=page]');
}
/** Choose a view of the place on screen from its list of views (docked or floating). */
export async function chooseView(page, kind) {
  await page.waitForFunction(() => [...document.querySelectorAll('.server-place:not(.is-tuning-place) :is(.source-list, .view-switcher)')]
    .some((e) => !e.closest('[hidden]') && e.offsetParent !== null));
  if (!(await shown(page, `#server-nav-${kind}`))) await openViewList(page);
  await page.locator(`#server-nav-${kind}`).click();
  await page.waitForSelector(`.server-view[data-view="${kind}"]`, { visible: true });
}
/** The Server or Workload view on screen ("" when none is). */
export const currentView = (page) => page.evaluate(() =>
  [...document.querySelectorAll('.server-place .server-view')].find((v) => v.offsetParent !== null)?.dataset.view || '');

/** Open the wider refresh scope from the visible view's split button. */
export async function openRefreshMenu(page) {
  if (await page.$('.view-refresh-menu [data-action="refresh-all"]')) return;
  await page.waitForFunction(() => [...document.querySelectorAll('.server-place .view-refresh button[aria-label="Refresh options"]')]
    .some((e) => !e.closest('[hidden]') && e.offsetParent !== null));
  for (const button of await page.$$('.server-place .view-refresh button[aria-label="Refresh options"]')) {
    if (await button.evaluate((e) => !e.closest('[hidden]') && e.offsetParent !== null)) {
      await button.click();
      break;
    }
  }
  await page.waitForSelector('.view-refresh-menu [data-action="refresh-all"]', { visible: true });
}

/**
 * For widths 1440 and 720 and themes light and dark, sets the viewport and
 * theme, asserts no horizontal page scroll and (when `frame` is given) that the
 * element does not overflow sideways, and writes `<name>-<width>-<theme>.png`.
 */
export async function layoutMatrix(page, shotsDir, name, { frame } = {}) {
  const height = page.viewport()?.height || 900;
  const written = [];
  const settleFrame = () => page.evaluate(async () => {
    // Finish finite motion without blocking on continuous topology animations.
    let timer;
    try {
      await Promise.race([
        Promise.all(document.getAnimations()
          .filter((animation) => {
            const timing = animation.effect?.getComputedTiming?.();
            return timing && timing.iterations !== Infinity;
          })
          .map((animation) => animation.finished.catch(() => {}))),
        new Promise((resolve) => { timer = setTimeout(resolve, 2000); }),
      ]);
    } finally {
      clearTimeout(timer);
    }
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  for (const width of [1440, 720]) {
    await page.setViewport({ width, height });
    for (const theme of ['light', 'dark']) {
      await page.evaluate((t) => {
        document.documentElement.dataset.theme = t;
        try {
          localStorage.setItem('myflames.theme', t);
        } catch {}
      }, theme);
      await settleFrame();
      const metrics = await page.evaluate((selector) => {
        const doc = document.documentElement;
        const pageScrollWidth = doc.scrollWidth;
        const pageClientWidth = doc.clientWidth;
        if (!selector) {
          return { pageScrollWidth, pageClientWidth, frame: null };
        }
        const el = document.querySelector(selector);
        if (!el) {
          return { pageScrollWidth, pageClientWidth, frame: { missing: true } };
        }
        const rect = el.getBoundingClientRect();
        return {
          pageScrollWidth,
          pageClientWidth,
          frame: {
            missing: false,
            scrollWidth: el.scrollWidth,
            clientWidth: el.clientWidth,
            left: Math.round(rect.left),
            right: Math.round(rect.right),
          },
        };
      }, frame || null);
      assert.ok(
        metrics.pageScrollWidth <= metrics.pageClientWidth,
        `${name} at ${width}px (${theme}) has horizontal page scroll: ${metrics.pageScrollWidth} > ${metrics.pageClientWidth}`,
      );
      if (frame) {
        assert.ok(
          metrics.frame && !metrics.frame.missing,
          `${name} at ${width}px (${theme}): frame ${frame} not found`,
        );
        assert.ok(
          metrics.frame.scrollWidth <= metrics.frame.clientWidth + 1 &&
            metrics.frame.left >= -1 &&
            metrics.frame.right <= metrics.pageClientWidth + 1,
          `${name} at ${width}px (${theme}): frame ${frame} overflows sideways (${JSON.stringify(metrics.frame)}, clientWidth=${metrics.pageClientWidth})`,
        );
      }
      const file = path.join(shotsDir, `${name}-${width}-${theme}.png`);
      await settleFrame();
      await page.screenshot({ path: file });
      written.push(file);
    }
  }
  return written;
}
