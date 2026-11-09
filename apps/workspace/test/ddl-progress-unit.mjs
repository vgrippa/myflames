// Render the shared progress UI without a browser or a loopback server.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs from "node:fs/promises";
import { build } from "esbuild";
const fixture = JSON.parse(await fs.readFile(new URL("./fixtures/ddl-progress.json", import.meta.url), "utf8"));
const result = await build({
  stdin: {
    contents: `import React from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { DdlMeter, DdlDetails, DdlUnavailable } from './src/DdlProgress';
      export const meter = (item) => renderToStaticMarkup(<DdlMeter item={item}/>);
      export const details = (item) => renderToStaticMarkup(<DdlDetails item={item}/>);
      export const missing = (progress) => renderToStaticMarkup(<DdlUnavailable progress={progress} onOpenInstruments={() => {}}/>);`,
    loader: "tsx", resolveDir: new URL("..", import.meta.url).pathname,
  },
  bundle: true, platform: "node", format: "cjs", write: false,
  jsx: "automatic", loader: { ".css": "empty", ".svg": "text" },
  external: ["react", "react-dom/server"], logLevel: "silent",
});
const out = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(createRequire(import.meta.url), out, out.exports);
const { meter, details, missing } = out.exports;
const html = meter(fixture.items[0]);
assert.match(html, /<progress[^>]+value="42"/);
assert.match(html, /DDL progress on connection 42/);
assert.match(html, /42%/);
assert.match(html, /Copying rows.*2 min 54 s left \(estimate\)/);
// Grouped like every other count (the browser suites accept either separator).
assert.match(details(fixture.items[0]), /Rows \(estimate\).*42[,.]?000 of 100[,.]?000/);
assert.match(details(fixture.items[0]), /title="126 seconds"/);
assert.match(details(fixture.items[0]), /title="174 seconds Estimate"/);
assert.match(meter({ ...fixture.items[0], percent: null, left_s: null }), /Unknown/);
assert.doesNotMatch(meter({ ...fixture.items[0], percent: null, left_s: null }), /value="/);
const diagnostic = missing({ ...fixture, available: false, items: [], missing: ["events_stages_current"],
  reason: "Turn on events_stages_current for DDL stage tracking." });
assert.match(diagnostic, /data-instruments-row="consumer:events_stages_current"/);
assert.match(diagnostic, /Turn on stage tracking…/);
assert.match(missing({ ...fixture, missing: ["performance_schema"] }), /data-instruments-row="ps"/);
assert.match(missing({ ...fixture, missing: ["stage/sql/copy to tmp table"] }), /data-instruments-row="instrument:stage\/sql\/copy to tmp table"/);
assert.equal(missing(fixture), "");
const malicious = meter({ ...fixture.items[0], stage_label: '<img src=x onerror="alert(1)">' });
assert.ok(!malicious.includes("<img"), "stage names are escaped");
assert.match(malicious, /&lt;img/);
console.log("DDL progress UI: meter, estimates, unknowns, exact settings links and escaping passed.");
