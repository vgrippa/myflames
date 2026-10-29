// A statement's latency histogram (digest-histogram-1.0) as display bins and
// one sentence. MySQL keeps 450 buckets per digest
// (performance_schema.events_statements_histogram_by_digest): bucket 0 below
// 10 µs, then bounds growing by 10^(1/50), the last one open-ended
// (storage/perfschema/pfs_histogram.cc). Five buckets make one display bin, a
// tenth of a decade, so a log time axis gets evenly spaced bars; bins come
// from bucket numbers, since the bounds arrive rounded. Pure (no
// React, no imports) so node tests run it without a browser.

export type LatencyBucket = { bucket: number; low_ms: number; high_ms: number | null; count: number };
export type LatencyBin = {
  /** Display bin index: -1 below 10 µs, then tenths of a decade from 10 µs. */
  index: number;
  low_ms: number;
  /** null for a bin holding the open-ended last bucket. */
  high_ms: number | null;
  count: number;
};
export type LatencyBins = { bins: LatencyBin[]; total: number; peak: number };

/** Where bucket 1 starts: 10 µs (BUCKET_BASE_TIMER). */
const BASE_MS = 0.01;
/** Bins per decade on the time axis. */
export const BINS_PER_DECADE = 10;
/** MySQL buckets per decade (BUCKET_BASE_FACTOR ^ 50 = 10), so per bin. */
const BUCKETS_PER_BIN = 50 / BINS_PER_DECADE;

/** The bin of a bucket, from its number (exact, unlike its rounded bounds). */
function binIndex(bucket: number): number {
  return bucket < 1 ? -1 : Math.floor((bucket - 1) / BUCKETS_PER_BIN);
}
export function binLow(index: number): number {
  return index < 0 ? 0 : BASE_MS * 10 ** (index / BINS_PER_DECADE);
}
export function binHigh(index: number): number {
  return BASE_MS * 10 ** ((index + 1) / BINS_PER_DECADE);
}

/** The buckets as contiguous display bins over whole decades, from the one
 * that holds the fastest call to the one that holds the slowest (empty bins
 * included, so the axis stays linear and its edges fall on 10 µs, 100 µs,
 * 1 ms…). One busy bucket still reads as a narrow peak in its decade. */
export function latencyBins(buckets: LatencyBucket[]): LatencyBins {
  const counts = new Map<number, number>();
  const open = new Set<number>();
  for (const b of buckets) {
    if (!(b.count > 0) || !Number.isInteger(b.bucket)) continue;
    const i = binIndex(b.bucket);
    counts.set(i, (counts.get(i) || 0) + b.count);
    if (b.high_ms === null) open.add(i);
  }
  if (!counts.size) return { bins: [], total: 0, peak: 0 };
  const fastest = Math.min(...counts.keys());
  const slowest = Math.max(...counts.keys());
  // The bin below 10 µs stands alone before the first decade.
  const first = fastest < 0 ? -1 : Math.floor(fastest / BINS_PER_DECADE) * BINS_PER_DECADE;
  const last = (Math.floor(Math.max(slowest, 0) / BINS_PER_DECADE) + 1) * BINS_PER_DECADE - 1;
  const bins: LatencyBin[] = [];
  let total = 0;
  let peak = 0;
  for (let i = first; i <= last; i++) {
    const count = counts.get(i) || 0;
    total += count;
    peak = Math.max(peak, count);
    bins.push({ index: i, low_ms: binLow(i), high_ms: open.has(i) ? null : binHigh(i), count });
  }
  return { bins, total, peak };
}

/** A time bound, two significant digits: "10 µs", "1.3 ms", "250 ms", "1.2 s". */
export function boundText(ms: number): string {
  const [value, unit] = scaled(ms);
  return `${round2(value)} ${unit}`;
}
function scaled(ms: number): [number, string] {
  if (ms >= 1000) return [ms / 1000, "s"];
  if (ms < 1) return [ms * 1000, "µs"];
  return [ms, "ms"];
}
function round2(value: number): string {
  if (value === 0) return "0";
  const digits = value >= 100 ? 0 : value >= 10 ? 0 : 1;
  const rounded = value >= 100 ? Math.round(value) : Number(value.toPrecision(2));
  return rounded.toFixed(digits).replace(/\.0$/, "");
}
/** "1.3 to 2.5 ms", or "800 µs to 1.3 ms" across units. */
export function rangeText(low: number, high: number | null): string {
  if (low <= 0 && high !== null) return `under ${boundText(high)}`;
  if (high === null) return `over ${boundText(low)}`;
  const [a, unitA] = scaled(low);
  const [b, unitB] = scaled(high);
  return unitA === unitB ? `${round2(a)} to ${round2(b)} ${unitB}` : `${boundText(low)} to ${boundText(high)}`;
}
function timeText(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  if (ms > 0 && ms < 1) return `${(ms * 1000).toFixed(1)} µs`;
  return `${Number(ms.toFixed(2))} ms`;
}

/** The narrowest run of bins that holds more than half of the calls (the
 * fullest one when several are as narrow), or null. */
export function mostCalls(bins: LatencyBin[], total: number): { low_ms: number; high_ms: number | null; count: number } | null {
  if (!(total > 0)) return null;
  let best: { from: number; to: number; count: number } | null = null;
  for (let from = 0; from < bins.length; from++) {
    let sum = 0;
    for (let to = from; to < bins.length; to++) {
      sum += bins[to].count;
      if (sum * 2 > total) {
        const width = to - from;
        if (!best || width < best.to - best.from || (width === best.to - best.from && sum > best.count)) best = { from, to, count: sum };
        break;
      }
    }
  }
  if (!best) return null;
  return { low_ms: bins[best.from].low_ms, high_ms: bins[best.to].high_ms, count: best.count };
}

/** One sentence for the chart: "Most calls took 1 to 2 ms; the slowest took
 * 1.20 s." *maxMs* is the digest's exact MAX_TIMER_WAIT when known. */
export function latencySummary(data: LatencyBins, maxMs?: number | null): string {
  if (!data.total) return "No calls were timed.";
  const most = mostCalls(data.bins, data.total);
  const head = most ? `Most calls took ${rangeText(most.low_ms, most.high_ms)}` : "Calls were timed";
  const slowestBin = [...data.bins].reverse().find((b) => b.count > 0);
  const slowest = typeof maxMs === "number" && Number.isFinite(maxMs) && maxMs > 0
    ? `the slowest took ${timeText(maxMs)}`
    : slowestBin ? `the slowest took ${rangeText(slowestBin.low_ms, slowestBin.high_ms)}` : "";
  return slowest ? `${head}; ${slowest}.` : `${head}.`;
}
