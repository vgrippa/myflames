// Render the actual component without a browser: sorting, path tails,
// accessible full paths, measurement formatting and unknown values.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { runInNewContext } from 'node:vm';
import { build } from 'esbuild';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
const built = await build({
  entryPoints: [fileURLToPath(new URL('../src/PlanStats.tsx', import.meta.url))],
  bundle: true, write: false, platform: 'node', format: 'cjs', jsx: 'automatic',
  packages: 'external', loader: { '.css': 'empty' },
});
const module = { exports: {} };
runInNewContext(built.outputFiles[0].text, { module, exports: module.exports, require: createRequire(import.meta.url) });
const { AllOccurrences } = module.exports;
const node = (id, parent_id, short_label, self_ms, group = '') => ({
  id, parent_id, short_label, self_ms, group, total_ms: 10, measured: true, actual_rows: 1500, loops: 1,
});
const data = {
  root_id: 'root',
  groups: { repeated: { label: 'Index lookup', count: 3, self_ms: 7, share: 0.7 } },
  nodes: [node('root', null, 'Limit <20>', 0), node('parent', 'root', 'Sort', 0), node('nested', 'parent', 'Join', 0),
    node('slow', 'nested', 'Lookup', 2, 'repeated'), node('fast', 'nested', 'Lookup', 5, 'repeated'),
    { ...node('unknown', 'root', 'Lookup', null, 'repeated'), measured: false, actual_rows: null }],
};
const render = (over = {}, group = 'repeated') => renderToStaticMarkup(React.createElement(AllOccurrences, {
  data: { ...data, ...over }, group, selectedId: 'slow', onSelect() {}, onClose() {},
}));
let html = render();
assert.deepEqual([...html.matchAll(/data-node-id="([^"]+)"/g)].map(m => m[1]), ['fast', 'slow', 'unknown']);
assert.match(html, /class="occurrence-path">Sort → Join</);
assert.match(html, /title="Limit &lt;20&gt; → Sort → Join"/);
assert.match(html, /aria-label="Limit &lt;20&gt; → Sort → Join\. 5 ms self · 50\.0% · 1,500 rows × 1 loop"/);
assert.match(html, /data-node-id="slow"[^>]*aria-selected="true" tabindex="0"/);
assert.match(html, /Not measured self · Unknown · Unknown rows × Unknown loops/);
assert.match(html, /Index lookup appears 3 times/);
assert.match(html, /Self time in total/);
assert.match(html, /Share of plan time/);
assert.match(html, /Back to measurements/);
assert.doesNotMatch(html, /<table|Columns/);
// Zero plan time cannot produce NaN or Infinity percentages.
html = render({ nodes: data.nodes.map(n => n.id === 'root' ? { ...n, total_ms: 0 } : n) });
assert.match(html, /5 ms self · Unknown/);
assert.doesNotMatch(html, /NaN|Infinity/);
assert.match(render({}, 'missing'), /Occurrences are unavailable/);
html = render({ groups: { repeated: { ...data.groups.repeated, count: 1 } }, nodes: [node('root', null, 'Lookup', 10, 'repeated')] });
assert.match(html, /class="occurrence-path">Plan root</);
assert.match(html, /appears 1 time\./);
console.log('occurrence render: sorted rows, path tails, accessible names, selection and unknown/zero timings passed');

// Execute the component's real keyboard/click handlers without duplicating
// navigation logic. Browser tests additionally check native Enter and focus.
const { default: ts } = await import('typescript');
const { readFile } = await import('node:fs/promises');
const source = await readFile(new URL('../src/PlanStats.tsx', import.meta.url), 'utf8');
const ast = ts.createSourceFile('PlanStats.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let attrs;
function find(node) {
  if (ts.isJsxOpeningElement(node) && node.attributes.properties.some(a => ts.isJsxAttribute(a) && a.name.text === 'className' && a.initializer?.text === 'occurrence-row')) attrs = node.attributes.properties;
  ts.forEachChild(node, find);
}
find(ast);
assert.ok(attrs);
const handler = (name, env) => {
  const expression = attrs.find(a => ts.isJsxAttribute(a) && a.name.text === name).initializer.expression.getText(ast);
  const code = ts.transpileModule(`(${expression})`, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  return new Function(...Object.keys(env), `return ${code}`)(...Object.values(env));
};
let selected = '';
let focused = -1;
let prevented = 0;
const children = [0, 1, 2].map(i => ({ focus() { focused = i; }, scrollIntoView() {} }));
const key = (index, k) => handler('onKeyDown', { index, rows: children })({ key: k, currentTarget: { parentElement: { children } }, preventDefault() { prevented++; } });
key(0, 'ArrowDown'); assert.equal(focused, 1);
key(0, 'ArrowUp'); assert.equal(focused, 0);
key(1, 'Home'); assert.equal(focused, 0);
key(1, 'End'); assert.equal(focused, 2);
key(2, 'ArrowDown'); assert.equal(focused, 2);
assert.equal(prevented, 5);
key(1, 'Enter'); assert.equal(prevented, 5, 'Enter remains the native button activation');
handler('onClick', { row: { id: 'chosen' }, onSelect: id => { selected = id; } })();
assert.equal(selected, 'chosen');
console.log('occurrence handlers: arrows, boundaries, Home/End and operator selection passed');
