// Measure the production card faces, including hover and selection, without
// a browser or server. Share the theme registry and WCAG math with the editor
// contrast suite so newly registered palettes are covered automatically.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import postcss from 'postcss';
import { block, color, contrast, over, themeCss, themeRegistry } from './theme-contrast-unit.mjs';

const read = name => readFileSync(new URL(name, import.meta.url), 'utf8');
const css = postcss.parse(read('../src/topology-presentation.css'));

// Split CSS function arguments without splitting the commas inside nested
// color-mix(), light-dark() or rgba() calls. Unsupported syntax fails closed.
function args(value) {
  let depth = 0, start = 0;
  const parts = [];
  for (let i = 0; i < value.length; i++) {
    if (value[i] === '(') depth++;
    if (value[i] === ')') depth--;
    if (value[i] === ',' && depth === 0) {
      parts.push(value.slice(start, i).trim());
      start = i + 1;
    }
  }
  assert.equal(depth, 0, `unbalanced CSS: ${value}`);
  return [...parts, value.slice(start).trim()];
}

function resolve(value, tokens, dark) {
  if (value === 'transparent') return [0, 0, 0, 0];
  const fn = /^(light-dark|color-mix)\(([\s\S]*)\)$/.exec(value);
  if (!fn) return color(value, tokens);
  const parts = args(fn[2]);
  if (fn[1] === 'light-dark') {
    assert.equal(parts.length, 2);
    return resolve(parts[dark ? 1 : 0], tokens, dark);
  }
  assert.equal(parts.shift(), 'in srgb');
  assert.equal(parts.length, 2);
  const stops = parts.map(part => {
    const weighted = /^([\s\S]+)\s+([\d.]+)%$/.exec(part);
    return { rgba: resolve(weighted ? weighted[1] : part, tokens, dark), weight: weighted ? Number(weighted[2]) / 100 : null };
  });
  const a = stops[0], b = stops[1];
  a.weight ??= b.weight === null ? .5 : 1 - b.weight;
  b.weight ??= 1 - a.weight;
  const total = a.weight + b.weight;
  const alpha = (a.rgba[3] * a.weight + b.rgba[3] * b.weight) / total;
  // CSS interpolates premultiplied sRGB. A weight sum below 100% also scales
  // the result's alpha, which matters for the default translucent tokens.
  return [0, 1, 2].map(i => alpha === 0 ? 0 :
    (a.rgba[i] * a.rgba[3] * a.weight + b.rgba[i] * b.rgba[3] * b.weight) / (total * alpha))
    .concat(alpha * Math.min(1, total));
}

const faces = [];
css.walkDecls('background', decl => {
  if (!/\.(?:cluster-card|topology-node|clusterset-cluster)(?![\w-])/.test(decl.parent.selector || '')) return;
  const gradient = /^linear-gradient\(([\s\S]*)\)$/.exec(decl.value);
  const stops = gradient ? args(gradient[1]).slice(1).map(stop => stop.replace(/\s+[\d.]+%$/, '')) : [decl.value];
  faces.push({ name: `${decl.parent.selector} (line ${decl.source.start.line})`, stops });
});
assert.ok(faces.length > 0, 'production card faces were found');

const base = block(':root {');
const themes = [
  ['light media/default', base, false],
  ['dark media', { ...base, ...block(':root:not([data-theme="light"]) {') }, true],
  ['light', { ...base, ...block(':root[data-theme="light"] {') }, false],
  ['dark', { ...base, ...block(':root[data-theme="dark"] {') }, true],
  ...themeRegistry.filter(theme => !['system', 'light', 'dark'].includes(theme.id))
    .map(theme => [theme.id, { ...base, ...block(`:root[data-theme="${theme.id}"] {`, themeCss) }, theme.base === 'dark']),
];

for (const [name, tokens, dark] of themes) test(`${name}: topology card text stays at 4.5:1`, () => {
  const backdrops = ['--surface', '--surface-raised', '--surface-muted'].map(key => color(tokens[key], tokens));
  let minimum = Infinity;
  for (const face of faces) {
    const stops = face.stops.map(stop => resolve(stop, tokens, dark));
    for (const backdrop of backdrops) {
      const points = stops.map(stop => over(stop, backdrop));
      // Check the lightest point explicitly, plus all stops and intermediate
      // colors: in light themes the darkest point is the harder background.
      const lightest = points.reduce((a, b) => contrast(a, [0, 0, 0, 1]) > contrast(b, [0, 0, 0, 1]) ? a : b);
      const samples = [lightest, ...points];
      for (let i = 1; i < points.length; i++) {
        for (const weight of [.25, .5, .75]) samples.push(points[i].map((v, c) => v * weight + points[i - 1][c] * (1 - weight)));
      }
      for (const ink of ['--ink', '--ink-secondary']) {
        for (const background of samples) {
          const ratio = contrast(over(color(tokens[ink], tokens), background), background);
          minimum = Math.min(minimum, ratio);
          assert.ok(ratio >= 4.5, `${name}: ${ink} / ${face.name}: ${ratio.toFixed(3)} < 4.5`);
        }
      }
    }
  }
  console.log(`${name}: minimum topology card contrast ${minimum.toFixed(2)}:1`);
});

test('both stage contracts distinguish theme colors from alpha-black shadows', () => {
  const reference = read('../../../docs/DEVELOPER_REFERENCE.md');
  const descriptions = reference.match(/^The topologies always use the stage presentation[^\n]+/gm) || [];
  assert.equal(descriptions.length, 2, 'Replication and Cluster both describe the stage');
  for (const description of descriptions) {
    assert.match(description, /colors come from theme tokens through color-mix\(\) and light-dark\(\)/);
    assert.match(description, /shadows use black with alpha/);
    assert.doesNotMatch(description, /everything is drawn from theme tokens/);
  }
});
