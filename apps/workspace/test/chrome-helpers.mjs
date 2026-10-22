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
    (options, text) => options.find((e) => e.textContent === text).value, name);
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
