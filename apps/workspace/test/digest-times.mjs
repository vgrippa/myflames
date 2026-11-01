// Unit tests for digestTimes.ts (Query digest's time range: keys, the
// Performance Schema overlap rule, quick ranges and the button's text).
// Run: node test/digest-times.mjs
//
// The keys follow slowlog.normalize_bound in Python: both ends inclusive to
// the precision written, wall-clock digits never moved to another zone.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/digestTimes.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "digestTimes.ts", { loader: "ts", format: "esm", target: "es2020" });
const { ANY_TIME, inputValue, overlaps, quickRange, rangeActive, rangeProblem, rangeText, timeKey, wallText } =
  await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));

// Keys: padded down for From, up for To, like Python's normalize_bound.
assert.equal(timeKey("2026-10-31T10:00"), "2026-10-31 10:00:00.000000");
assert.equal(timeKey("2026-10-31T10:00", true), "2026-10-31 10:00:59.999999");
assert.equal(timeKey("2026-10-31", true), "2026-10-31 23:59:59.999999");
assert.equal(timeKey("2026-10-31 10:00:05.5", true), "2026-10-31 10:00:05.599999");
assert.equal(timeKey("2026-10-31 10:00:05.123456"), "2026-10-31 10:00:05.123456");
for (const bad of ["", "soon", "2026-02-30T10:00", "2026-10-31T24:00", "31/10/2026"]) assert.equal(timeKey(bad), null, bad);

// Problems: unreadable values and From after To; an open end is fine.
assert.equal(rangeProblem(ANY_TIME), "");
assert.equal(rangeProblem({ from: "2026-10-31T10:00", to: "" }), "");
assert.equal(rangeProblem({ from: "2026-10-31T11:00", to: "2026-10-31T10:00" }), "From must be before To.");
assert.equal(rangeProblem({ from: "2026-10-31T10:00", to: "2026-10-31T10:00" }), "", "one minute, both ends included");
assert.equal(rangeProblem({ from: "x", to: "" }), "From is not a date and time.");
assert.equal(rangeActive(ANY_TIME), false);
assert.equal(rangeActive({ from: "", to: "2026-10-31T10:00" }), true);

// Performance Schema: a statement is kept when first seen <= To and last seen >= From.
const range = { from: "2026-10-31T10:00", to: "2026-10-31T11:00" };
assert.equal(overlaps("2026-10-31 09:00:00.000000", "2026-10-31 10:00:00.000000", range), true, "last seen at From");
assert.equal(overlaps("2026-10-31 11:00:30.000000", "2026-10-31 12:00:00.000000", range), true, "first seen inside the To minute");
assert.equal(overlaps("2026-10-31 11:01:00.000000", "2026-10-31 12:00:00.000000", range), false, "first seen after To");
assert.equal(overlaps("2026-10-31 08:00:00.000000", "2026-10-31 09:59:59.999999", range), false, "last seen before From");
assert.equal(overlaps("2026-10-31 08:00:00", "2026-10-31 12:00:00", range), true, "spans the range");
assert.equal(overlaps(null, null, range), false, "no times: not seen in the range");
assert.equal(overlaps(null, null, ANY_TIME), true, "no range keeps everything");

// Quick ranges end at the anchor (the newest time in the report), in its clock.
assert.deepEqual(quickRange("hour", "2026-10-31 10:07:23.840005"),
  { from: "2026-10-31T09:07:23", to: "2026-10-31T10:07:23", quick: "hour" });
assert.deepEqual(quickRange("day", "2026-03-01 00:30:00"), { from: "2026-02-28T00:30:00", to: "2026-03-01T00:30:00", quick: "day" });
assert.deepEqual(quickRange("today", "2026-10-31 18:24:14"), { from: "2026-10-31T00:00:00", to: "2026-10-31T18:24:14", quick: "today" });
// A wall-clock hour is 3,600 s of digits: no local daylight saving rule applies.
assert.deepEqual(quickRange("hour", "2026-03-08 03:30:00"), { from: "2026-03-08T02:30:00", to: "2026-03-08T03:30:00", quick: "hour" });
assert.equal(quickRange("hour", null), null);

// Values for the datetime-local fields, and the button's text.
assert.equal(inputValue("2026-10-31 10:07:23.840005"), "2026-10-31T10:07:23");
assert.equal(inputValue("2026-10-31T10:07"), "2026-10-31T10:07:00");
assert.equal(inputValue(""), "");
assert.equal(rangeText(ANY_TIME), "Any time");
assert.equal(rangeText({ from: "a", to: "b", quick: "day" }), "Last 24 hours");
const same = rangeText({ from: "2026-10-31T09:00", to: "2026-10-31T10:30" });
assert.ok(same.startsWith(wallText("2026-10-31T09:00")) && same.includes(" to "), same);
assert.ok(!same.split(" to ")[1].includes("31"), `a range within one day names the day once: ${same}`);
assert.ok(rangeText({ from: "2026-10-31T09:00", to: "" }).startsWith("From "));
assert.ok(rangeText({ from: "", to: "2026-10-31T09:00" }).startsWith("Until "));
// Wall text keeps the digits as written (never shifted by this computer's zone).
assert.match(wallText("2026-10-31 23:30:00"), /31/);
assert.match(wallText("2026-10-31 23:30:00"), /11:30|23:30/);
console.log("digest-times: ok");
