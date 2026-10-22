// Unit tests for duration.ts (the workspace's duration formatter, matching
// perf_common._duration_text). Run: node test/duration-text.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/duration.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "duration.ts", { loader: "ts", format: "esm", target: "es2020" });
const { durationText } = await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));

// Every branch, with the values the CLI's perf_common._duration_text gives
// for values of zero or more.
assert.equal(durationText(1598400), "18 d 12 h");
assert.equal(durationText(90061), "1 d 1 h");
assert.equal(durationText(86400), "1 d 0 h");
assert.equal(durationText(14700), "4 h 5 min");
assert.equal(durationText(3600), "1 h 0 min");
assert.equal(durationText(252), "4 min 12 s");
assert.equal(durationText(60), "1 min 0 s");
assert.equal(durationText(36), "36 s");
assert.equal(durationText(59), "59 s");
assert.equal(durationText(1), "1 s");
assert.equal(durationText(0), "0 s");
// Fractions truncate like Python's int().
assert.equal(durationText(36.9), "36 s");
assert.equal(durationText(119.9), "1 min 59 s");
// Deliberate differences from the CLI: a negative reads as no time at all
// (the CLI would print "-1 d 23 h"), and unknown is "—" (the CLI prints "?").
assert.equal(durationText(-5), "0 s");
// Nothing usable shows the em dash.
for (const bad of [null, undefined, NaN, Infinity, -Infinity]) assert.equal(durationText(bad), "—", String(bad));

// Render the real shared cells and facts without opening a socket. Keep the
// formatter and markup together: readable words must not lose the measured
// seconds or the lag caveats. CSS is irrelevant to this markup-only check.
const bundled = await build({
  stdin: {
    contents: 'export { Duration } from "./src/PerfShared"; export { factRows, factLabel } from "./src/OpsShared"; export { lagText } from "./src/ReplicationTopology";',
    resolveDir: fileURLToPath(new URL("../", import.meta.url)),
    loader: "ts",
  },
  bundle: true, write: false, platform: "node", format: "cjs",
  loader: { ".css": "empty" }, external: ["react", "react-dom"], logLevel: "silent",
});
const compiled = { exports: {} };
new Function("module", "exports", "require", bundled.outputFiles[0].text)(compiled, compiled.exports, createRequire(import.meta.url));
const { Duration, factRows, factLabel, lagText } = compiled.exports;
const render = (value) => renderToStaticMarkup(value);
const fractional = render(createElement(Duration, { value: 252.1256, note: "Clock changes can skew lag." }));
assert.match(fractional, />4 min 12 s<\/abbr>/);
assert.match(fractional, /title="252\.1256 seconds Clock changes can skew lag\."/);
assert.match(fractional, /aria-label="252\.1256 seconds Clock changes can skew lag\."/);
assert.match(render(createElement(Duration, { value: 0 })), /title="0 seconds"[^>]*>0 s<\/abbr>/);
assert.match(render(createElement(Duration, { value: 60.75, approx: true })), /title="~60\.75 seconds"[^>]*>~1 min 0 s<\/abbr>/);
assert.equal(render(createElement(Duration, { value: null })), "—");
assert.equal(factLabel("heartbeat_interval_s"), "Heartbeat interval");
const rows = factRows({ heartbeat_age_s: 252.1256, heartbeats_received: 12, receiver_connected: true, receiver_idle_s: null, executed_gtid_set: "uuid:1-2" });
assert.deepEqual(rows.map(([name]) => name), ["Heartbeat age", "Heartbeats received", "Receiver connected", "Receiver idle", "Executed GTID set"]);
assert.match(render(rows[0][1]), /title="252\.1256 seconds"[^>]*>4 min 12 s<\/abbr>/);
assert.equal(render(rows[1][1]), '<span class="ops-wrap">12</span>');
assert.equal(render(rows[2][1]), '<span class="ops-wrap">Yes</span>');
assert.equal(render(rows[3][1]), '<span class="ops-wrap">Not available</span>');
assert.match(render(rows[4][1]), /class="ops-gtid"/);
assert.equal(lagText(252.1256), "4 min 12 s behind");
assert.equal(lagText(0), "0 s behind");
assert.equal(lagText(null), "Lag unknown");

console.log("duration text: ok");
