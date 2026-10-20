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
