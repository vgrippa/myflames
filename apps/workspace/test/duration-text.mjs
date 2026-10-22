// Unit tests for duration.ts (the workspace's duration formatter, matching
// perf_common._duration_text). Run: node test/duration-text.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
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

console.log("duration text: ok");
