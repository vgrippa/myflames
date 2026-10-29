// Unit tests for latencyHistogram.ts (Query digest's latency histogram:
// display bins on a log time axis and its one-sentence summary).
// Run: node test/latency-histogram.mjs
//
// Buckets follow MySQL's events_statements_histogram_by_digest
// (storage/perfschema/pfs_histogram.cc): bucket 0 below 10 µs, bucket k >= 1
// from 10 µs * 10^((k - 1) / 50), the last one (449) open-ended.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { transformWithEsbuild } from "vite";

const source = await fs.readFile(new URL("../src/latencyHistogram.ts", import.meta.url), "utf8");
const { code } = await transformWithEsbuild(source, "latencyHistogram.ts", { loader: "ts", format: "esm", target: "es2020" });
const { binHigh, binLow, boundText, latencyBins, latencySummary, mostCalls, rangeText } =
  await import("data:text/javascript;base64," + Buffer.from(code).toString("base64"));

// The bounds as the server sends them: picoseconds over 1e9, rounded.
const low = (k) => (k < 1 ? 0 : Math.round(0.01 * 10 ** ((k - 1) / 50) * 1e6) / 1e6);
const high = (k) => (k >= 449 ? null : Math.round(0.01 * 10 ** (k / 50) * 1e6) / 1e6);
const bucket = (k, count) => ({ bucket: k, low_ms: low(k), high_ms: high(k), count });

// Bin edges: tenths of a decade from 10 µs.
assert.equal(binLow(-1), 0);
assert.equal(binLow(0), 0.01);
assert.ok(Math.abs(binLow(10) - 0.1) < 1e-12);
assert.ok(Math.abs(binHigh(9) - 0.1) < 1e-12);
assert.ok(Math.abs(binLow(30) - 10) < 1e-9);

// Five buckets per bin, by bucket number: bucket 6 starts the second bin even
// though its rounded lower bound (0.012589 ms) sits a hair under the edge.
{
  const { bins, total, peak } = latencyBins([bucket(1, 3), bucket(5, 2), bucket(6, 7)]);
  assert.equal(total, 12);
  assert.equal(peak, 7);
  const at = (index) => bins.find((b) => b.index === index)?.count;
  assert.deepEqual([at(0), at(1)], [5, 7]);
  // The axis covers whole decades (10 µs to 100 µs here), so a single peak
  // stays narrow and the edges fall on round times.
  assert.equal(bins.length, 10);
  assert.deepEqual([bins[0].index, bins.at(-1).index], [0, 9]);
}
{
  // 600 ms and 1.2 s: 100 ms to 10 s, two decades.
  const { bins } = latencyBins([bucket(240, 1), bucket(255, 1)]);
  assert.deepEqual([bins[0].index, bins.at(-1).index], [40, 59]);
  assert.ok(Math.abs(bins[0].low_ms - 100) < 1e-9);
  assert.ok(Math.abs(bins.at(-1).high_ms - 10000) < 1e-6);
  assert.equal(bins.reduce((n, b) => n + b.count, 0), 2);
}

// Contiguous bins, empty ones included, from the fastest to the slowest call;
// bucket 0 is the bin below 10 µs; the open last bucket has no upper bound.
{
  const data = latencyBins([bucket(0, 1), bucket(151, 40), bucket(449, 2), bucket(300, 0)]);
  assert.equal(data.bins[0].index, -1);
  assert.equal(data.bins[0].low_ms, 0);
  // The open bucket's bin (89) has no upper bound; its decade ends at bin 89.
  assert.equal(data.bins.at(-1).index, 89);
  assert.equal(data.bins.at(-1).high_ms, null);
  assert.equal(data.bins.length, 91);
  for (let i = 1; i < data.bins.length; i++) assert.equal(data.bins[i].index, data.bins[i - 1].index + 1);
  assert.equal(data.total, 43, "a zero-count bucket adds nothing");
}

// Nothing timed.
assert.deepEqual(latencyBins([]), { bins: [], total: 0, peak: 0 });
assert.equal(latencySummary(latencyBins([])), "No calls were timed.");

// Bounds and ranges read like the rest of the view: µs, ms, s.
assert.equal(boundText(0.01), "10 µs");
assert.equal(boundText(0.1), "100 µs");
assert.equal(boundText(1), "1 ms");
assert.equal(boundText(1.2589254), "1.3 ms");
assert.equal(boundText(12.589254), "13 ms");
assert.equal(boundText(1000), "1 s");
assert.equal(boundText(1258.9254), "1.3 s");
assert.equal(rangeText(1, 1.9952623), "1 to 2 ms");
assert.equal(rangeText(0.7943282, 1.2589254), "794 µs to 1.3 ms");
assert.equal(rangeText(0, 0.01), "under 10 µs");
assert.equal(rangeText(9120.1, null), "over 9.1 s");

// "Most calls" is the narrowest run of bins holding more than half of them.
{
  // 1 to 2 ms holds 60 of 100 calls (bins 20, 21 and 22: 1 ms to 1.995 ms).
  const buckets = [bucket(101, 30), bucket(106, 20), bucket(111, 10), bucket(201, 25), bucket(301, 15)];
  const data = latencyBins(buckets);
  const most = mostCalls(data.bins, data.total);
  assert.equal(most.count, 60);
  assert.equal(rangeText(most.low_ms, most.high_ms), "1 to 2 ms");
  assert.equal(latencySummary(data, 1234.5), "Most calls took 1 to 2 ms; the slowest took 1.23 s.");
  // Without the digest's MAX_TIMER_WAIT, the slowest bin's range.
  assert.equal(latencySummary(data), "Most calls took 1 to 2 ms; the slowest took 10 to 13 s.");
}
{
  // An even split: no single bin holds more than half, so the run widens.
  const data = latencyBins([bucket(101, 50), bucket(151, 50), bucket(152, 1)]);
  const most = mostCalls(data.bins, data.total);
  assert.equal(most.count, 51);
  assert.equal(rangeText(most.low_ms, most.high_ms), "10 to 13 ms");
}
{
  // Every call below 10 µs.
  const data = latencyBins([bucket(0, 9)]);
  assert.equal(latencySummary(data, 0.004), "Most calls took under 10 µs; the slowest took 4.0 µs.");
}

console.log("latency-histogram: ok");
