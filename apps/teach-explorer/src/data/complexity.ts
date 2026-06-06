import type { CurveKind } from "./types";

// Approximate cost curves for log–log overlay. Inputs are normalized
// against a reference n=1000 (cost=1 for linear) so different families
// share comparable y-axis units. The shape matters more than the
// absolute magnitude — readers should see ratios, not "how many pages".
//
// Constants chosen so curves spread visually at n=1M:
//   linear   ≈ 1e3
//   log      ≈ 20
//   nlogn    ≈ 2e4
//   quadratic ≈ 1e6
const N_REF = 1000;
const M_INNER = 1_000_000; // assumed inner-table size for join curves
const BUF_PAGES = 256;     // join_buffer pages for BNL curve

const safeLog = (n: number) => Math.log2(Math.max(n, 2));

export const curves: Record<CurveKind, (n: number) => number> = {
  constant:   () => 1,
  log:        (n) => safeLog(n),
  linear:     (n) => n / N_REF,
  nlogn:      (n) => (n * safeLog(n)) / N_REF,
  quadratic:  (n) => (n * n) / (N_REF * N_REF),
  // Hash / index merge: cost dominated by sum of both sides.
  linear_sum: (n) => (n + M_INNER * 0.1) / N_REF,
  // Block Nested Loop: outer scanned (m/buf) times.
  block_nl:   (n) => (n * (M_INNER / (BUF_PAGES * 1024))) / N_REF,
  // Non-covering secondary lookup: two B-tree descents per row.
  two_log:    (n) => 2 * safeLog(n),
  // Skip-scan: roughly sqrt(n) per low-cardinality leading column.
  sqrt_n:     (n) => Math.sqrt(n),
  // Indexed nested loop: outer n probes, each O(log m).
  nested_indexed: (n) => (n / N_REF) * safeLog(M_INNER),
  // greedy_search worst case: O(N!) over the small N axis (table count,
  // not row count). The Compare view's x-axis is "rows (n)" by default
  // — for join_order we reinterpret n as N (number of tables joined).
  // Cap at 1e15 so the chart's log axis stays sane past N≈18.
  factorial: (n) => {
    const N = Math.max(2, Math.min(20, Math.round(Math.log10(Math.max(n, 2)) * 2)));
    let r = 1;
    for (let i = 2; i <= N; i++) {
      r *= i;
      if (r > 1e15) return 1e15;
    }
    return r;
  },
};

export const curveLabel: Record<CurveKind, string> = {
  constant:       "O(1)",
  log:            "O(log n)",
  linear:         "O(n)",
  nlogn:          "O(n log n)",
  quadratic:      "O(n²)",
  linear_sum:     "O(n + m)",
  block_nl:       "O(n·m / buf)",
  two_log:        "O(2 log n)",
  sqrt_n:         "O(√n)",
  nested_indexed: "O(n·log m)",
  factorial:      "O(N!)",
};

/** Generate logarithmically-spaced sample points across the x domain. */
export function sampleCurve(kind: CurveKind, minN = 10, maxN = 1e9, samples = 120): Array<{ n: number; y: number }> {
  const fn = curves[kind];
  const logMin = Math.log10(minN);
  const logMax = Math.log10(maxN);
  const points: Array<{ n: number; y: number }> = [];
  for (let i = 0; i < samples; i++) {
    const t = i / (samples - 1);
    const n = Math.pow(10, logMin + t * (logMax - logMin));
    const y = Math.max(fn(n), 1e-3);
    points.push({ n, y });
  }
  return points;
}

export function evaluateCurve(kind: CurveKind, n: number): number {
  return Math.max(curves[kind](n), 1e-3);
}

export function formatN(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return `${Math.round(n)}`;
}
