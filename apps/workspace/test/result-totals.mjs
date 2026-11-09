// Unit tests for src/resultTotals.ts: the totals of the result grid's
// selected cells, their footer line, and the row panel's JSON layout. No
// build or browser needed: the module is compiled with Vite's esbuild
// transform and imported from a data URL.
// Run: node test/result-totals.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(
  new URL("../src/resultTotals.ts", import.meta.url),
  "utf8",
);
const { code } = await transformWithEsbuild(source, "resultTotals.ts", {
  loader: "ts",
  format: "esm",
  target: "es2020",
});
const { totals, totalsText, prettyJson } = await import(
  "data:text/javascript;base64," + Buffer.from(code).toString("base64")
);

let passed = 0;
function test(name, body) {
  try {
    body();
    passed++;
  } catch (error) {
    console.error("FAIL " + name);
    throw error;
  }
}

test("empty selection: every aggregate is null", () => {
  assert.deepEqual(totals([]), {
    cells: 0,
    nulls: 0,
    numericCount: 0,
    sum: null,
    avg: null,
    min: null,
    max: null,
    approximate: false,
  });
});

test("big integers stay exact past 2^63 (BigInt, not approximate)", () => {
  const t = totals(["9223372036854775807", "1"]);
  assert.equal(t.sum, "9223372036854775808");
  assert.equal(t.approximate, false);
  assert.equal(t.numericCount, 2);
  assert.equal(t.min, "1");
  assert.equal(t.max, "9223372036854775807");
});

test("decimals are exact: 0.1 + 0.2 is 0.3", () => {
  const t = totals(["0.1", "0.2"]);
  assert.equal(t.sum, "0.3");
  assert.equal(t.approximate, false);
});

test("mixed scales: 1.5 + 2.25 is 3.75", () => {
  const t = totals(["1.5", "2.25"]);
  assert.equal(t.sum, "3.75");
  assert.equal(t.min, "1.5");
  assert.equal(t.max, "2.25");
  assert.equal(t.avg, "1.875");
});

test("negatives sum and order exactly", () => {
  const t = totals(["-5", "3", "-2.5"]);
  assert.equal(t.sum, "-4.5");
  assert.equal(t.min, "-5");
  assert.equal(t.max, "3");
  assert.equal(t.avg, "-1.5");
});

test("min/max order big integers beyond double precision", () => {
  const t = totals(["9223372036854775807", "-9223372036854775808", "5"]);
  assert.equal(t.min, "-9223372036854775808");
  assert.equal(t.max, "9223372036854775807");
  assert.equal(t.sum, "4");
});

test("avg of [1, 2] is 1.5", () => {
  assert.equal(totals(["1", "2"]).avg, "1.5");
});

test("avg rounds at six digits past the shared scale", () => {
  assert.equal(totals(["1", "2", "4"]).avg, "2.333333");
  assert.equal(totals(["1", "3"]).avg, "2");
});

test("a fraction-only value parses (.5)", () => {
  const t = totals([".5", ".25"]);
  assert.equal(t.sum, "0.75");
});

test("an exponent value falls back to doubles and marks approximate", () => {
  const t = totals(["1e10", "5"]);
  assert.equal(t.approximate, true);
  assert.equal(t.numericCount, 2);
  assert.equal(t.sum, "10000000005");
  assert.equal(t.min, "5");
  assert.equal(t.max, "10000000000");
  assert.equal(t.avg, "5000000002.5");
});

test("a negative exponent also forces the approximate path", () => {
  const t = totals(["-2.5E-3"]);
  assert.equal(t.approximate, true);
  assert.equal(t.sum, "-0.0025");
});

test("NULL is skipped by the aggregates and counted", () => {
  const t = totals([null, "1", null, "3"]);
  assert.equal(t.cells, 4);
  assert.equal(t.nulls, 2);
  assert.equal(t.numericCount, 2);
  assert.equal(t.sum, "4");
  assert.equal(t.approximate, false);
});

test("only NULLs: no numeric aggregates", () => {
  const t = totals([null, null]);
  assert.equal(t.cells, 2);
  assert.equal(t.nulls, 2);
  assert.equal(t.numericCount, 0);
  assert.equal(t.sum, null);
  assert.equal(t.avg, null);
  assert.equal(t.min, null);
  assert.equal(t.max, null);
});

test("non-numeric values count into cells only", () => {
  const t = totals(["abc", "0x1F", "2026-10-27", ""]);
  assert.equal(t.cells, 4);
  assert.equal(t.nulls, 0);
  assert.equal(t.numericCount, 0);
  assert.equal(t.sum, null);
  assert.equal(t.min, null);
  assert.equal(t.max, null);
  assert.equal(t.avg, null);
});

test("mixed text and numbers aggregate the numbers only", () => {
  const t = totals(["n/a", "10", null, "2"]);
  assert.equal(t.cells, 4);
  assert.equal(t.nulls, 1);
  assert.equal(t.numericCount, 2);
  assert.equal(t.sum, "12");
  assert.equal(t.avg, "6");
});

test("trailing zeros in the fraction trim away", () => {
  assert.equal(totals(["1.50", "2.50"]).sum, "4");
  assert.equal(totals(["1.10", "1"]).sum, "2.1");
});

test("the footer line for numbers, NULLs and text", () => {
  assert.equal(totalsText(totals(["1", "2", "3"])), "Count 3 · Sum 6 · Avg 2 · Min 1 · Max 3");
  assert.equal(totalsText(totals(["9223372036854775807", "1", null])),
    "3 cells · Count 2 · Sum 9223372036854775808 · Avg 4611686018427387904 · Min 1 · Max 9223372036854775807 · 1 NULL");
  assert.equal(totalsText(totals(["a"])), "1 cell");
  assert.equal(totalsText(totals(["a", "b", null])), "3 cells · 1 NULL");
  assert.equal(totalsText(totals(["1e3", "1"])), "Count 2 · Sum 1001 · Avg 500.5 · Min 1 · Max 1000 · approximate (floating point)");
  assert.equal(totalsText(totals(Array(1200).fill("1"))), "Count 1,200 · Sum 1200 · Avg 1 · Min 1 · Max 1");
});

test("JSON is laid out with numbers and strings as written", () => {
  assert.equal(prettyJson('{"id": 9007199254740993, "tags": ["a", "b"], "n": {}}'),
    '{\n  "id": 9007199254740993,\n  "tags": [\n    "a",\n    "b"\n  ],\n  "n": {}\n}');
  assert.equal(prettyJson('[1.10, "x: {y}, [z]", "q\\"uote", []]'),
    '[\n  1.10,\n  "x: {y}, [z]",\n  "q\\"uote",\n  []\n]');
  assert.equal(prettyJson(" [ ] "), "[]");
  assert.equal(prettyJson("not json"), null);
  assert.equal(prettyJson('"a string"'), null, "only objects and arrays");
  assert.equal(prettyJson("42"), null);
  assert.equal(prettyJson("{broken"), null);
  assert.equal(prettyJson("[1,]"), null);
});

console.log(`result-totals: ${passed} tests passed`);
