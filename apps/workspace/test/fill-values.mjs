// Unit tests for src/fillValues.ts: the parameter form of a Top statements
// tab (the SQL around each marker, the guessed type, and the values written
// into the Parameters JSON). No build or browser needed: the module is
// compiled with Vite's esbuild transform. Run: node test/fill-values.mjs
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/fillValues.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "fillValues.ts", { loader: "ts", format: "esm", target: "es2020" });
const { findMarker, markerContext, guessType, fillFields, startEntries, entryValue, applyEntries, MARK } = await import(
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

const DIGEST = "SELECT * FROM `orders` WHERE `customer_id` IN (:p1) AND `created_at` > :p2";

test("markers are found outside quotes and comments only", () => {
  const sql = "SELECT ':p1', `:p1` /* :p1 */ FROM t WHERE a = :p1 -- :p1";
  assert.deepEqual(findMarker(sql, "p1"), { start: sql.indexOf("= :p1") + 2, end: sql.indexOf("= :p1") + 5 });
  assert.equal(findMarker("SELECT :p10", "p1"), null, ":p10 is not :p1");
  assert.equal(findMarker("SELECT a::p1", "p1"), null, ":: is not a parameter");
  assert.equal(findMarker("SELECT 1", "p1"), null);
});

test("the context shows a few words around the marker", () => {
  assert.equal(markerContext(DIGEST, "p1"), `… \`customer_id\` IN (${MARK}) AND …`);
  assert.equal(markerContext(DIGEST, "p2"), `… AND \`created_at\` > ${MARK}`);
  assert.equal(markerContext("SELECT *\n  FROM t\n  LIMIT :p1", "p1"), `… FROM t LIMIT ${MARK}`);
  assert.equal(markerContext("WHERE id = :p1", "p1"), `WHERE id = ${MARK}`);
  assert.equal(markerContext("SELECT 1", "p1"), "");
  // Long tokens are cut, so the line stays short.
  const long = markerContext(`SELECT ${"x".repeat(80)} = :p1`, "p1");
  assert.ok(long.length < 60, long);
});

test("the type is Number in numeric places, Text elsewhere", () => {
  assert.equal(guessType(DIGEST, "p1"), "number", "customer_id is an id");
  assert.equal(guessType(DIGEST, "p2"), "text", "created_at compares as text");
  assert.equal(guessType("SELECT * FROM t LIMIT :p1", "p1"), "number");
  assert.equal(guessType("SELECT * FROM t LIMIT :p1, :p2", "p2"), "number");
  assert.equal(guessType("SELECT * FROM t LIMIT 5 OFFSET :p1", "p1"), "number");
  assert.equal(guessType("SELECT price * :p1 FROM t", "p1"), "number");
  assert.equal(guessType("WHERE t.`order_count` >= :p1", "p1"), "number");
  assert.equal(guessType("WHERE userId = :p1", "p1"), "number");
  assert.equal(guessType("WHERE qty BETWEEN :p1 AND :p2", "p2"), "number");
  assert.equal(guessType("WHERE status = :p1", "p1"), "text");
  assert.equal(guessType("WHERE name LIKE :p1", "p1"), "text");
  assert.equal(guessType("WHERE idea = :p1", "p1"), "text", "a word that starts with id is not an id");
});

test("fields keep the order and mark collapsed lists", () => {
  const fields = fillFields(DIGEST, ["p1", "p2"], ["p1"]);
  assert.deepEqual(fields.map((f) => [f.name, f.list, f.type]), [["p1", true, "number"], ["p2", false, "text"]]);
});

test("start entries come from the Parameters JSON", () => {
  const fields = fillFields(DIGEST, ["p1", "p2"], ["p1"]);
  // Nothing is edited yet: Use values writes only what the user changes.
  assert.deepEqual(startEntries(fields, '{"p1": null, "p2": null}'), {
    p1: { type: "number", value: "", edited: false },
    p2: { type: "text", value: "", edited: false },
  });
  assert.deepEqual(startEntries(fields, '{"p1": [3, 4], "p2": "2026-01-01"}'), {
    p1: { type: "number", value: "3, 4", edited: false },
    p2: { type: "text", value: "2026-01-01", edited: false },
  });
  assert.deepEqual(startEntries(fields, '{"p1": [null]}').p1, { type: "null", value: "", edited: false });
  assert.deepEqual(startEntries(fields, "not json").p2, { type: "text", value: "", edited: false });
});

test("entry values: numbers, text, NULL and lists", () => {
  assert.deepEqual(entryValue({ type: "number", value: " 42 " }, false), { value: 42 });
  assert.deepEqual(entryValue({ type: "number", value: "-1.5e3" }, false), { value: -1500 });
  assert.deepEqual(entryValue({ type: "text", value: " a b " }, false), { value: " a b " }, "text is kept as typed");
  assert.deepEqual(entryValue({ type: "text", value: "" }, false), { empty: true });
  assert.deepEqual(entryValue({ type: "number", value: "  " }, false), { empty: true });
  assert.deepEqual(entryValue({ type: "null", value: "x" }, false), { value: null });
  assert.deepEqual(entryValue({ type: "null", value: "" }, true), { value: [null] });
  assert.deepEqual(entryValue({ type: "number", value: "1, 2,3" }, true), { value: [1, 2, 3] });
  assert.deepEqual(entryValue({ type: "text", value: "a, b ,, c" }, true), { value: ["a", "b", "c"] });
  assert.deepEqual(entryValue({ type: "text", value: " , " }, true), { empty: true });
  assert.match(entryValue({ type: "number", value: "abc" }, false).error, /not a number/);
  assert.match(entryValue({ type: "number", value: "1, x" }, true).error, /“x” is not a number/);
  assert.match(entryValue({ type: "number", value: "1e999" }, false).error, /out of range/);
});

test("numbers that would lose digits are refused, not rounded", () => {
  assert.match(entryValue({ type: "number", value: "9007199254740993" }, false).error, /more than 15 digits/);
  assert.match(entryValue({ type: "number", value: "0.30000000000000001" }, false).error, /more than 15 digits/);
  assert.deepEqual(entryValue({ type: "number", value: "123456789012345" }, false), { value: 123456789012345 });
  assert.deepEqual(entryValue({ type: "number", value: "0.000000000000001" }, false), { value: 1e-15 });
  assert.deepEqual(entryValue({ type: "number", value: "1000000000000000" }, false).error !== undefined, true, "16 integer digits");
});

test("applying writes the JSON and reports what is left", () => {
  const fields = fillFields(DIGEST, ["p1", "p2"], ["p1"]);
  const out = applyEntries('{\n  "p1": null,\n  "p2": null,\n  "other": 1\n}', fields, {
    p1: { type: "number", value: "7, 8" },
    p2: { type: "text", value: "" },
  });
  assert.equal(out.ok, true);
  assert.deepEqual(JSON.parse(out.parameters), { p1: [7, 8], p2: null, other: 1 });
  assert.deepEqual(out.filled, ["p1"]);
  assert.deepEqual(out.remaining, ["p2"]);
  const all = applyEntries("{}", fields, { p1: { type: "null", value: "" }, p2: { type: "text", value: "2026-01-01" } });
  assert.deepEqual(JSON.parse(all.parameters), { p1: [null], p2: "2026-01-01" });
  assert.deepEqual(all.remaining, []);
  // A cleared field is null again: the form showed the current value.
  const cleared = applyEntries('{"p2": "x"}', fields, { p1: { type: "number", value: "1" }, p2: { type: "text", value: "" } });
  assert.equal(JSON.parse(cleared.parameters).p2, null);
  // An invalid value writes nothing.
  const bad = applyEntries("{}", fields, { p1: { type: "number", value: "x" }, p2: { type: "text", value: "y" } });
  assert.equal(bad.ok, false);
  assert.deepEqual(Object.keys(bad.errors), ["p1"]);
  // Parameters JSON that does not parse starts from an empty object.
  assert.deepEqual(JSON.parse(applyEntries("{oops", fields, { p1: { type: "number", value: "1" }, p2: { type: "text", value: "a" } }).parameters), { p1: [1], p2: "a" });
});

test("untouched parameters keep their exact Parameters value", () => {
  // p1 was given by hand as a list with a NULL member; only p2 is missing.
  const fields = fillFields(DIGEST, ["p1", "p2"], ["p1"]);
  const text = '{"p1": [1, null, 2], "p2": null, "other": true}';
  const start = startEntries(fields, text);
  const out = applyEntries(text, fields, { ...start, p2: { ...start.p2, value: "2026-01-01", edited: true } });
  assert.equal(out.ok, true);
  assert.deepEqual(JSON.parse(out.parameters), { p1: [1, null, 2], p2: "2026-01-01", other: true },
    "reopening the form never rebuilds a list the user did not edit");
  assert.deepEqual(out.filled, ["p2"]);
  assert.deepEqual(out.remaining, []);
  // Text with commas is not split, and an untouched value is not reported as filled by the form.
  const commas = '{"p1": ["a,b", "c"], "p2": "x"}';
  const kept = applyEntries(commas, fields, startEntries(fields, commas));
  assert.deepEqual(JSON.parse(kept.parameters), { p1: ["a,b", "c"], p2: "x" });
  assert.deepEqual([kept.filled, kept.remaining], [[], []]);
  // An untouched missing value stays missing and is still to fill in.
  const missing = applyEntries('{"p2": null}', fields, startEntries(fields, '{"p2": null}'));
  assert.deepEqual(JSON.parse(missing.parameters), { p2: null });
  assert.deepEqual(missing.remaining, ["p1", "p2"]);
});

test("lists the comma field cannot hold are shown read-only as JSON", () => {
  const fields = fillFields(DIGEST, ["p1", "p2"], ["p1"]);
  const locked = (value, list = true) => startEntries(fields, JSON.stringify({ [list ? "p1" : "p2"]: value }))[list ? "p1" : "p2"];
  for (const [value, reason] of [
    [[1, null, 2], /NULL/],
    [["a,b", "c"], /comma/],
    [[" a", "b"], /spaces/],
    [["", "b"], /empty text/],
    [[], /empty list/],
    [[1, "a"], /numbers and text/],
    [[null, null], /NULL/],
    [[true], /another kind/],
  ]) {
    const entry = locked(value);
    assert.equal(entry.locked, JSON.stringify(value), `${JSON.stringify(value)} is shown as JSON`);
    assert.match(entry.reason, reason);
    assert.equal(entry.edited, false);
  }
  assert.equal(locked([1, 2]).locked, undefined, "a plain number list stays editable");
  assert.deepEqual(locked([1, 2]), { type: "number", value: "1, 2", edited: false });
  assert.deepEqual(locked(["a", "b"]), { type: "text", value: "a, b", edited: false });
  assert.deepEqual(locked([null]), { type: "null", value: "", edited: false });
  // A single value: an array, an empty text or a boolean cannot be shown as typed.
  assert.equal(locked([1], false).locked, "[1]");
  assert.equal(locked("", false).locked, '""');
  assert.equal(locked(false, false).locked, "false");
  assert.deepEqual(locked("a, b", false), { type: "text", value: "a, b", edited: false }, "a single text keeps its commas");
  // A locked entry is never rewritten, even when marked as edited.
  const text = '{"p1": [1, null, 2], "p2": null}';
  const start = startEntries(fields, text);
  const out = applyEntries(text, fields, { p1: { ...start.p1, edited: true }, p2: { type: "text", value: "x", edited: true } });
  assert.deepEqual(JSON.parse(out.parameters).p1, [1, null, 2]);
});

console.log(`fill-values: ${passed} tests passed`);
