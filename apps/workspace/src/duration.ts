// Durations as words, matching the CLI's perf_common._duration_text for every
// value of zero or more so the workspace and the command line read the same:
// "18 d 12 h", "4 h 5 min", "4 min 12 s", "36 s", "0 s". On purpose it differs
// where the CLI has no good answer: unknown shows "—" (the workspace's word for
// a missing value, the CLI prints "?") and a negative reads "0 s". Pure (no
// React) so unit tests can run it without a browser.

/** Seconds as a readable duration. Null or not finite: "—".
 * Fractions truncate (Python's int()); a negative reads as "0 s". */
export function durationText(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const s = Math.max(0, Math.trunc(value));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  if (minutes) return `${minutes} min ${secs} s`;
  return `${secs} s`;
}
