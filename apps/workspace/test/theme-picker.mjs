// The Settings > Appearance theme picker (src/SettingsPage.tsx): every
// registry theme has a card in the documented order (myflames, popular dark,
// popular light), a click applies the theme, the radiogroup has one tab stop
// and arrow/Home/End keys move and select, the checked card follows a palette
// Theme action, and the section stays within the window at 720 px. Real
// Python server, no database. Run after `npm run build`:
// node test/theme-picker.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { themeRegistry } from './theme-contrast-unit.mjs';
import { withThemeWorkspace } from './theme-browser-harness.mjs';

const expectedIds = [
  'system', 'light', 'dark', 'fire', 'digital-rain', 'neon-grid',
  'dracula', 'nord', 'solarized-dark', 'tokyo-night', 'gruvbox-dark',
  'catppuccin-mocha', 'github-dark', 'one-dark',
  'solarized-light', 'catppuccin-latte', 'github-light',
];
const byId = id => themeRegistry.find(theme => theme.id === id);
assert.deepEqual(themeRegistry.map(theme => theme.id).sort(), [...expectedIds].sort(), 'the order list covers the registry');
const expectedNames = expectedIds.map(id => byId(id).name);

const screenshots = await fs.mkdtemp(path.join(os.tmpdir(), 'myflames-theme-picker-'));
await withThemeWorkspace(async page => {
  const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';
  const card = id => `.settings-page .theme-card[data-theme-id="${id}"]`;
  const state = () => page.evaluate(async () => ({
    theme: document.documentElement.dataset.theme || 'system',
    saved: (await fetch('/api/settings/ui', {method:'POST', headers:{'Content-Type':'application/json','X-Myflames-Token':localStorage.getItem('myflames.session-proof')}, body:'{}'}).then(reply => reply.json())).preferences.theme,
    checked: document.querySelector('.theme-card[aria-checked="true"]')?.dataset.themeId,
    tabStops: [...document.querySelectorAll('.theme-card[tabindex="0"]')].map(e => e.dataset.themeId),
    active: document.activeElement?.dataset?.themeId,
  }));
  const waitTheme = id => page.waitForFunction(async expected => {
    if ((document.documentElement.dataset.theme || 'system') !== expected
      || document.querySelector(`.theme-card[data-theme-id="${expected}"]`)?.getAttribute('aria-checked') !== 'true') return false;
    const reply = await fetch('/api/settings/ui', {method:'POST', headers:{'Content-Type':'application/json','X-Myflames-Token':localStorage.getItem('myflames.session-proof')}, body:'{}'}).then(reply => reply.json());
    return reply.preferences?.theme === expected;
  }, {}, id);

  await page.click('button[aria-label="Settings"]');
  await page.waitForSelector('.settings-page');

  // The radiogroup, its accessible name and the group labels.
  const group = await page.$eval('.settings-page .theme-picker', e => ({ role: e.getAttribute('role'), label: e.getAttribute('aria-label') }));
  assert.deepEqual(group, { role: 'radiogroup', label: 'Theme' });
  assert.deepEqual(await page.$$eval('.settings-page .theme-group-label', els => els.map(e => e.textContent)), ['myflames', 'Dark', 'Light']);
  // Every registry theme has a card in the documented order.
  assert.deepEqual(await page.$$eval('.settings-page .theme-card', els => els.map(e => e.dataset.themeId)), expectedIds);
  assert.deepEqual(await page.$$eval('.settings-page .theme-card .theme-card-name', els => els.map(e => e.textContent)), expectedNames);
  assert.deepEqual(await page.$$eval('.settings-page .theme-card', els => els.map(e => e.getAttribute('role'))), expectedIds.map(() => 'radio'));
  const credits = await page.$$eval('.settings-page .theme-card .theme-card-credit', els => els.map(e => e.textContent));
  assert.deepEqual(credits, expectedIds.map(id => byId(id).credit), 'each card shows the registry credit');

  // A card shows the theme's own colors whatever the current theme is. The
  // current theme is system (light under the harness media); Dracula's
  // preview still draws its registry swatches.
  const swatches = await page.$eval(card('dracula'), e => ({
    preview: e.querySelector('.theme-card-preview').style.background,
    surface: e.querySelector('.theme-card-surface').style.background,
    dots: [...e.querySelectorAll('.theme-card-dot')].map(dot => dot.style.background),
  }));
  assert.equal(swatches.preview, 'rgb(40, 42, 54)', 'preview background is Dracula, not the current theme');
  assert.equal(swatches.surface, 'rgb(68, 71, 90)');
  assert.deepEqual(swatches.dots, ['rgb(255, 121, 198)', 'rgb(241, 250, 140)'], 'two syntax colors from the registry');

  // Clicking a card applies it at once, with one tab stop on the checked card.
  await page.click(card('dracula'));
  await waitTheme('dracula');
  let s = await state();
  assert.equal(s.saved, 'dracula');
  assert.deepEqual(s.tabStops, ['dracula'], 'one tab stop, on the checked card');
  assert.ok(await page.$('.theme-card.selected .theme-card-check'), 'the selected state is not color alone');
  // Buttons transition border-color (style.css), so wait for the final value.
  await page.mouse.move(0, 0);
  await page.waitForFunction(() => {
    const probe = document.createElement('div');
    probe.style.color = 'var(--accent)';
    document.body.append(probe);
    const accent = getComputedStyle(probe).color;
    probe.remove();
    return getComputedStyle(document.querySelector('.theme-card.selected')).borderColor === accent;
  });

  // Arrow keys move and select; the focus follows the checked card.
  await page.focus(card('dracula'));
  await page.keyboard.press('ArrowRight');
  await waitTheme('nord');
  assert.equal((await state()).active, 'nord', 'focus follows the checked card');
  await page.keyboard.press('ArrowDown');
  await waitTheme('solarized-dark');
  await page.keyboard.press('ArrowLeft');
  await waitTheme('nord');
  await page.keyboard.press('ArrowUp');
  await waitTheme('dracula');
  // Left from the first card wraps to the last; right from the last wraps back.
  await page.keyboard.press('Home');
  await waitTheme('system');
  await page.keyboard.press('ArrowLeft');
  await waitTheme('github-light');
  await page.keyboard.press('ArrowRight');
  await waitTheme('system');
  await page.keyboard.press('End');
  await waitTheme('github-light');
  assert.deepEqual((await state()).tabStops, ['github-light'], 'the tab stop follows the checked card');
  // Space and Enter select the focused card.
  await page.focus(card('tokyo-night'));
  await page.keyboard.press('Enter');
  await waitTheme('tokyo-night');
  await page.focus(card('one-dark'));
  await page.keyboard.press('Space');
  await waitTheme('one-dark');

  // A palette Theme action moves the checked card while the page is open.
  await page.keyboard.down(MOD); await page.keyboard.press('k'); await page.keyboard.up(MOD);
  await page.waitForSelector('[aria-label="Search commands"]');
  await page.type('[aria-label="Search commands"]', 'Theme: Nord');
  const option = await page.waitForFunction(label => [...document.querySelectorAll('.command-option')].find(node => node.querySelector('bdi')?.textContent === label), {}, 'Theme: Nord');
  await option.asElement().click(); await option.dispose();
  await waitTheme('nord');
  assert.deepEqual((await state()).tabStops, ['nord'], 'the tab stop follows the palette choice');

  // No horizontal overflow at 1440 or 720 px; screenshots in Light and Dark.
  for (const theme of ['light', 'dark']) {
    await page.click(card(theme));
    await waitTheme(theme);
    for (const [width, height] of [[1440, 900], [720, 520]]) {
      await page.setViewport({ width, height });
      await page.waitForSelector('.settings-page');
      await new Promise(resolve => setTimeout(resolve, 150));
      const wide = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      assert.ok(wide <= 0, `${theme} at ${width} px: no horizontal overflow (${wide})`);
      const columns = await page.$$eval('.settings-page .theme-group:first-child .theme-card', els => {
        const tops = new Set(els.map(e => e.getBoundingClientRect().top));
        return els.length / tops.size;
      });
      assert.ok(columns >= 2, `${theme} at ${width} px: the grid wraps to at least two columns (${columns})`);
      const panel = await page.$('.settings-panel');
      await panel.screenshot({ path: path.join(screenshots, `appearance-${theme}-${width}.png`) });
      await panel.dispose();
    }
  }
  await page.setViewport({ width: 1440, height: 900 });
});
console.log('Theme picker checks passed. Screenshots: ' + screenshots);
