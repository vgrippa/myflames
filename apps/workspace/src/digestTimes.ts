// Query digest's time range: the From and To values of the range picker
// (datetime-local strings, in the report's own clock) and the pure helpers
// around them. Times are wall-clock digits, never moved to this computer's
// zone: they are read and written through Date.UTC so no daylight saving rule
// can shift them. Both ends are inclusive to the precision written (a To
// without seconds keeps that whole minute), as slowlog.normalize_bound does
// in Python for the slow log sources.

/** "YYYY-MM-DDTHH:MM[:SS]" or "" for an open end; quick names a quick range. */
export type TimeRange = { from: string; to: string; quick?: QuickRange };
export type QuickRange = "hour" | "day" | "today";
export const ANY_TIME: TimeRange = { from: "", to: "" };
export const QUICK_RANGES: [QuickRange, string][] = [["hour", "Last hour"], ["day", "Last 24 hours"], ["today", "Today"]];

const TIME = /^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?$/;

export function rangeActive(range: TimeRange) {
  return !!(range.from || range.to);
}

function parts(value: string) {
  const m = TIME.exec((value || "").trim());
  if (!m) return null;
  const [y, mo, d] = [+m[1], +m[2], +m[3]];
  const h = m[4] === undefined ? null : +m[4];
  const mi = m[5] === undefined ? null : +m[5];
  const s = m[6] === undefined ? null : +m[6];
  const at = new Date(Date.UTC(y, mo - 1, d, h ?? 0, mi ?? 0, s ?? 0));
  // Date.UTC rolls 2026-02-30 over to March: refuse what is not a real time.
  if (at.getUTCFullYear() !== y || at.getUTCMonth() !== mo - 1 || at.getUTCDate() !== d ||
    at.getUTCHours() !== (h ?? 0) || at.getUTCMinutes() !== (mi ?? 0) || at.getUTCSeconds() !== (s ?? 0)) return null;
  return { y, mo, d, h, mi, s, frac: m[7] ?? null, at };
}

const pad = (n: number, w = 2) => String(n).padStart(w, "0");

/** A time as a key that compares in time order ("YYYY-MM-DD HH:MM:SS.ffffff"),
 * padded down for a From and up for a To; null when empty or unreadable. */
export function timeKey(value: string | null | undefined, end = false): string | null {
  if (!value) return null;
  const p = parts(value);
  if (!p) return null;
  const h = p.h ?? (end ? 23 : 0);
  const mi = p.mi ?? (end ? 59 : 0);
  const s = p.s ?? (end ? 59 : 0);
  const frac = p.frac === null ? (end ? "999999" : "000000") : p.frac.padEnd(6, end ? "9" : "0");
  return `${pad(p.y, 4)}-${pad(p.mo)}-${pad(p.d)} ${pad(h)}:${pad(mi)}:${pad(s)}.${frac}`;
}

/** Why a range cannot be used, or "". */
export function rangeProblem(range: TimeRange): string {
  if (range.from && !timeKey(range.from)) return "From is not a date and time.";
  if (range.to && !timeKey(range.to, true)) return "To is not a date and time.";
  const low = timeKey(range.from);
  const high = timeKey(range.to, true);
  if (low && high && low > high) return "From must be before To.";
  return "";
}

/** Performance Schema keeps one running total per statement: a range can
 * only keep statements whose first and last seen times overlap it. */
export function overlaps(first: string | null | undefined, last: string | null | undefined, range: TimeRange) {
  if (!rangeActive(range)) return true;
  const low = timeKey(range.from);
  const high = timeKey(range.to, true);
  const firstKey = timeKey(first || "");
  const lastKey = timeKey(last || "");
  if (!firstKey || !lastKey) return false;
  return (!high || firstKey <= high) && (!low || lastKey >= low);
}

/** A key or log time as a datetime-local value (seconds kept). */
export function inputValue(value: string | null | undefined): string {
  const p = value ? parts(value.replace(" ", "T").slice(0, 19)) : null;
  if (!p) return "";
  return `${pad(p.y, 4)}-${pad(p.mo)}-${pad(p.d)}T${pad(p.h ?? 0)}:${pad(p.mi ?? 0)}:${pad(p.s ?? 0)}`;
}

function shift(p: { at: Date }, seconds: number) {
  const at = new Date(p.at.getTime() + seconds * 1000);
  return `${pad(at.getUTCFullYear(), 4)}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())}T${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}:${pad(at.getUTCSeconds())}`;
}

/** A quick range ending at *anchor* (the newest time the report knows, in
 * its own clock), or null without one. */
export function quickRange(kind: QuickRange, anchor: string | null | undefined): TimeRange | null {
  const p = anchor ? parts(anchor.replace(" ", "T").slice(0, 19)) : null;
  if (!p) return null;
  const to = shift(p, 0);
  if (kind === "today") return { from: `${to.slice(0, 10)}T00:00:00`, to, quick: kind };
  return { from: shift(p, kind === "hour" ? -3600 : -86400), to, quick: kind };
}

/** A short wall-clock text ("Oct 31, 9:05 AM"), with seconds when they are not zero. */
export function wallText(value: string | null | undefined, withYear = false): string {
  const p = value ? parts(value.replace(" ", "T").slice(0, 19)) : null;
  if (!p) return value || "";
  return p.at.toLocaleString(undefined, {
    timeZone: "UTC", month: "short", day: "numeric", ...(withYear ? { year: "numeric" } : {}),
    hour: "numeric", minute: "2-digit", ...(p.s ? { second: "2-digit" } : {}),
  });
}

/** The time of day alone ("10:00 AM"), for a range that ends on its first day. */
function clockText(value: string): string {
  const p = parts(value.replace(" ", "T").slice(0, 19));
  if (!p) return value;
  return p.at.toLocaleTimeString(undefined, { timeZone: "UTC", hour: "numeric", minute: "2-digit", ...(p.s ? { second: "2-digit" } : {}) });
}

/** The range as the toolbar button and the summary say it. */
export function rangeText(range: TimeRange): string {
  if (!rangeActive(range)) return "Any time";
  const quick = QUICK_RANGES.find(([k]) => k === range.quick);
  if (quick) return quick[1];
  if (range.from && range.to) {
    const sameDay = range.from.slice(0, 10) === range.to.slice(0, 10);
    return `${wallText(range.from)} to ${sameDay ? clockText(range.to) : wallText(range.to)}`;
  }
  return range.from ? `From ${wallText(range.from)}` : `Until ${wallText(range.to)}`;
}
