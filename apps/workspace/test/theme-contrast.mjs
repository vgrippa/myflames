// Computed-style counterpart of theme-contrast-unit.mjs, using the same color
// math and targets. Run after npm run build. No live database is contacted.
import { themeRegistry, themeCss, block, color, over, contrast, measureTokens, assertMeasurements } from './theme-contrast-unit.mjs';
import { withThemeWorkspace } from './theme-browser-harness.mjs';
import { readFileSync } from 'node:fs';
const css = readFileSync(new URL('../src/style.css', import.meta.url), 'utf8');
const keys = [...new Set([...Object.keys(block(':root {', css)), ...Object.keys(block(':root[data-theme="dark"] {', css)), ...Object.keys(block(':root[data-theme="fire"] {', themeCss))])];
await withThemeWorkspace(async page => {
  for (const theme of themeRegistry) {
    // Explicit light media makes System reproducible; dark media is exercised
    // by themes.mjs, including custom light themes under a dark OS setting.
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    const tokens = await page.evaluate(({id,keys}) => {
      if (id === 'system') delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = id;
      const style = getComputedStyle(document.documentElement);
      return Object.fromEntries(keys.map(key => [key, style.getPropertyValue(key).trim()]).filter(([,value]) => value));
    }, { id: theme.id, keys });
    const rows = measureTokens(tokens, theme.base === 'dark');
    // Browser coverage also checks the distinct, subtler match and its ink.
    const bg = color(tokens['--surface'], tokens);
    const selection = over(color(tokens['--editor-selection'], tokens), bg);
    const match = over(color(tokens['--editor-selection-match'], tokens), bg);
    rows.push({pair:'match / editor',ratio:contrast(match,bg),target:theme.base==='dark'?1.3:1.2});
    rows.push({pair:'selected ink / match',ratio:contrast(color(tokens['--editor-selection-ink'],tokens),match),target:4.5});
    console.log(`\n${theme.name}`);
    console.table(rows.map(row => ({ pair:row.pair, ratio:row.ratio.toFixed(3), target:row.target })));
    assertMeasurements(theme.id,rows);
    if (contrast(selection,bg)-contrast(match,bg)<.3) throw Error(`${theme.id}: match is not subtler than selection`);
  }
});
console.log('Computed contrast passed for every theme.');
