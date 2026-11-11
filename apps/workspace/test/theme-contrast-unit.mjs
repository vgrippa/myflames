// Parse the production tokens and CSS consumers. No browser or server needed.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { build } from 'esbuild';
import { fileURLToPath } from 'node:url';
const read = name => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8');
const css = read('style.css');
export function block(selector, source = css) {
  const css = source;
  const start = css.indexOf(selector);
  assert.ok(start >= 0, `missing ${selector}`);
  return Object.fromEntries([...css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start))
    .matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map(([, key, value]) => [key, value.trim()]));
}
export function color(value, tokens) {
  const variable = /^var\((--[\w-]+)\)$/.exec(value);
  if (variable) return color(tokens[variable[1]], tokens);
  if (/^#[\da-f]{6}$/i.test(value)) return [1, 3, 5].map(i => parseInt(value.slice(i, i + 2), 16)).concat(1);
  const parts = /^rgba?\((.*)\)$/.exec(value)?.[1].split(/[\s,/]+/).filter(Boolean).map(Number);
  assert.ok(parts?.length >= 3 && parts.every(Number.isFinite), `unsupported color: ${value}`);
  return parts.length === 3 ? [...parts, 1] : parts;
}
export function over(foreground, background) {
  return foreground.slice(0, 3).map((c, i) => c * foreground[3] + background[i] * (1 - foreground[3])).concat(1);
}
function luminance(rgb) {
  return rgb.slice(0, 3).reduce((sum, c, i) => {
    const n = c / 255;
    return sum + (n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722][i];
  }, 0);
}
export function contrast(a, b) {
  const [low, high] = [luminance(a), luminance(b)].sort((a, b) => a - b);
  return (high + .05) / (low + .05);
}
const registryBundle = await build({ stdin: { contents: 'export { THEMES } from "./src/theme-picker";', resolveDir: fileURLToPath(new URL('../', import.meta.url)) }, bundle: true, write: false, format: 'esm', platform: 'browser' });
export const themeRegistry = (await import('data:text/javascript;base64,' + Buffer.from(registryBundle.outputFiles[0].text).toString('base64'))).THEMES;
export const themeCss = read('themes.css');
export function measureTokens(tokens, dark) {
  const c = key => color(tokens[key], tokens);
  const rows = [];
  const pair = (name, foreground, background, target) => rows.push({ pair: name, ratio: contrast(over(foreground, background), background), target });
  for (const surface of ['--bg', '--surface', '--surface-muted']) {
    for (const ink of ['--ink', '--ink-muted']) pair(`${ink} / ${surface}`, c(ink), c(surface), 4.5);
  }
  pair('control-line / bg', c('--control-line'), c('--bg'), 3);
  pair('accent / bg', c('--accent'), c('--bg'), 3);
  const bg = c('--surface');
  const selection = over(c('--editor-selection'), bg);
  pair('editor selection / editor', selection, bg, dark ? 1.8 : 1.5);
  pair('selected ink / selection', c('--editor-selection-ink'), selection, 4.5);
  for (const key of Object.keys(tokens).filter(key => key.startsWith('--syntax-'))) pair(`${key} / editor`, c(key), bg, 4.5);
  for (const role of ['ok', 'warn', 'danger', 'info']) pair(`${role}-text / bg`, c(`--${role}-text`), c('--bg'), 4.5);
  return rows;
}
export function assertMeasurements(name, rows) {
  for (const row of rows) assert.ok(row.ratio >= row.target, `${name}: ${row.pair}: ${row.ratio.toFixed(3)} < ${row.target}`);
}
const base = block(':root {');
const themes = [
  ['light media/default', base, false],
  ['dark media', { ...base, ...block(':root:not([data-theme="light"]) {') }, true],
  ['data-theme light', { ...base, ...block(':root[data-theme="light"] {') }, false],
  ['data-theme dark', { ...base, ...block(':root[data-theme="dark"] {') }, true],
];
for (const theme of themeRegistry.filter(theme => !['system', 'light', 'dark'].includes(theme.id))) {
  const tokens = block(`:root[data-theme="${theme.id}"] {`, themeCss);
  const darkTokens = block(':root[data-theme="dark"] {');
  for (const key of Object.keys(darkTokens)) assert.ok(key in tokens, `${theme.id}: missing Dark token ${key}`);
  const measuredKeys = ['--bg', '--surface', '--surface-muted', '--ink', '--ink-muted', '--control-line', '--accent', '--editor-selection', '--editor-selection-ink', '--editor-selection-match', '--editor-find-current', ...Object.keys(tokens).filter(key => key.startsWith('--syntax-') || /--(ok|warn|danger|info)-text$/.test(key))];
  for (const key of measuredKeys) assert.match(tokens[key], /^#[\da-f]{6}$/i, `${theme.id}: ${key} must be an opaque hex literal`);
  for (const surface of ['--bg', '--surface-muted']) {
    for (const key of ['--line', '--line-subtle', '--line-strong', '--control-line']) {
      assert.ok(contrast(color(tokens[key], tokens), color(tokens[surface], tokens)) >= 3, `${theme.id}: ${key} on ${surface}`);
    }
  }
  const rows = measureTokens(tokens, theme.base === 'dark');
  assertMeasurements(theme.id, rows);
  console.log(`${theme.id}: min text ${Math.min(...rows.filter(r=>r.target===4.5).map(r=>r.ratio)).toFixed(2)}; border ${rows.find(r=>r.pair==='control-line / bg').ratio.toFixed(2)}; accent ${rows.find(r=>r.pair==='accent / bg').ratio.toFixed(2)}; selection ${rows.find(r=>r.pair==='editor selection / editor').ratio.toFixed(2)}`);
  themes.push([theme.id, tokens, theme.base === 'dark']);
}
for (const [name, tokens, dark] of themes) {
  assertMeasurements(name, measureTokens(tokens, dark));
  const background = color(tokens['--surface'], tokens), activeLine = color(tokens['--surface-subtle'], tokens);
  const ink = color(tokens['--editor-selection-ink'], tokens);
  const selection = over(color(tokens['--editor-selection'], tokens), background);
  const match = over(color(tokens['--editor-selection-match'], tokens), background);
  const current = over(color(tokens['--editor-find-current'], tokens), background);
  for (const surface of [background, activeLine]) {
    assert.ok(contrast(selection, surface) >= (dark ? 1.8 : 1.5), `${name}: selection against editor/active line`);
    assert.ok(contrast(current, surface) >= (dark ? 1.8 : 1.5), `${name}: current find match against editor/active line`);
  }
  for (const highlight of [selection, match, current]) assert.ok(contrast(ink, highlight) >= 4.5, `${name}: highlighted text`);
  assert.ok(contrast(match, background) < contrast(selection, background) - .3, `${name}: match is subtler than selection`);
  if (dark) assert.ok(contrast(match, background) <= contrast(selection, background) - .4, `${name}: dark match stays below selection`);
  assert.ok(contrast(selection, match) >= 1.1, `${name}: match is distinct from selection`);
  assert.ok(contrast(match, background) >= (dark ? 1.3 : 1.2), `${name}: match remains visible`);
  const oldSelection = over(color(dark ? 'rgba(55, 148, 255, 0.35)' : 'rgba(0, 102, 204, 0.22)', tokens), background);
  const oldMatch = over(color(tokens['--accent-soft'], tokens), background);
  console.log(`${name}: selection ${contrast(oldSelection, background).toFixed(2)} -> ${contrast(selection, background).toFixed(2)}:1; match ${contrast(oldMatch, background).toFixed(2)} -> ${contrast(match, background).toFixed(2)}:1; current find ${contrast(oldMatch, background).toFixed(2)} -> ${contrast(current, background).toFixed(2)}:1; selected text ${contrast(ink, selection).toFixed(2)}:1`);
}
const cmCss = read('editor-cm.css'), extras = read('editor-extras.css'), find = read('editor-find.css'), marks = read('editor/findMarks.ts');
assert.match(cmCss, /\.cm-selectionBackground,[\s\S]*?background: var\(--editor-selection\)/, 'unfocused selection uses the same token');
assert.match(cmCss, /\.cm-selectedText \*[\s\S]*?color: var\(--editor-selection-ink\)/, 'selected syntax uses readable ink');
assert.match(cmCss, /::selection[\s\S]*?color: var\(--editor-selection-ink\)/);
assert.match(extras, /\.cm-selectionMatch[\s\S]*?var\(--editor-selection-match\)/);
assert.match(find, /\.sql-find-current[\s\S]*?var\(--editor-find-current\)/);
assert.match(marks, /"\.sql-find-current": \{ backgroundColor: "var\(--editor-find-current\)"/);
console.log(`${themes.length} theme blocks and focused/unfocused selection consumers passed.`);
