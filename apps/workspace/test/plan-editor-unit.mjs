// Pure rendering checks for environments that cannot start Chromium or bind
// loopback. Layout and keyboard assertions live in plan-editor-layout.mjs.
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { build } from 'esbuild';

const require = createRequire(new URL('../package.json', import.meta.url));
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const result = await build({
  stdin: { contents: 'export { ContextPanel } from "./src/ContextPanel"; export { SchemaBrowser } from "./src/SchemaBrowser"; export { NewsSetting } from "./src/HomeNews"; export { bytes } from "./src/PerfShared";',
    resolveDir: new URL('../', import.meta.url).pathname, loader: 'tsx' },
  bundle: true, write: false, platform: 'node', format: 'cjs',
  external: ['react', 'react-dom'], loader: { '.css': 'empty' },
});
const compiled = { exports: {} };
new Function('module', 'exports', 'require', result.outputFiles[0].text)(compiled, compiled.exports, require);
const { ContextPanel, SchemaBrowser, NewsSetting, bytes } = compiled.exports;
const selected = { node_id: '0', label: 'Table scan', details: { actual_last_row_ms: 1, actual_rows: 3 },
  self_time_ms: 1, total_time_ms: 1, loops: 1 };
const plan = { analysis: { warnings: [] }, operators: [selected] };
const render = op => renderToStaticMarkup(React.createElement(ContextPanel, {
  plan, selected: op, mismatch: null, onMetricsTarget() {}, onSelect() {},
}));
const empty = render(undefined);
assert.match(empty, /data-empty="true"/);
assert.match(empty, /class="inspector-selection-hint">Select an operator to see its details<\/p>/);
assert.match(empty, /class="inspector-metrics"/, 'the linked metrics mount survives the compact empty state');
const detail = render(selected);
assert.match(detail, /data-empty="false"/);
assert.match(detail, /operator-detail/);
assert.match(detail, /Self time/);
assert.match(detail, /Actual rows \/ loop/);
// The visible panel starts its first read by itself (an effect, so it is not
// in static markup; plan-editor-layout.mjs covers it in Chrome). The only
// gate left asks for a connection and offers no Load schemas button.
const schemaBrowser = connection => renderToStaticMarkup(React.createElement(SchemaBrowser, {
  connection, visible: true, width: 240, onWidth() {}, onClose() {}, onInsert() {}, onQuery() {},
}));
const unconnected = schemaBrowser({ host: '', port: 3306, user: '' });
assert.match(unconnected, /Choose a connection to see its schemas\./);
assert.doesNotMatch(unconnected, /Load schemas/);
assert.doesNotMatch(unconnected, /aria-label="Reload schemas"/);
const schemas = schemaBrowser({ host: '127.0.0.1', port: 3306, user: 'app' });
assert.doesNotMatch(schemas, /Choose a connection/);
// Reload is offered only once a read has started.
assert.doesNotMatch(schemas, /aria-label="Reload schemas"/);
assert.doesNotMatch(schemas, /aria-label="Reading schemas…"/);
const newsSetting = renderToStaticMarkup(React.createElement(NewsSetting));
assert.match(newsSetting, /aria-describedby="news-toggle-hint"/);
assert.match(newsSetting, /read over HTTPS while Home is open\./);
assert.doesNotMatch(newsSetting, /About Home news/);
for (const [value, expected] of [[0, '0 B'], [1023, '1023 B'], [1024, '1.0 KiB'],
  [123456, '120.6 KiB'], [1048576, '1.0 MiB'], [1073741824, '1.0 GiB'], [1099511627776, '1.0 TiB'],
  [null, '—'], [undefined, '—']]) assert.equal(bytes(value), expected);
console.log('plan/editor unit checks passed: inspector markup; schema panel connection gate; news hint without duplicate info; byte unit boundaries');
