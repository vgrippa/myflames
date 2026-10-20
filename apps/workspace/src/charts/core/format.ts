// Display formatting. Values arrive as canonical milliseconds/rows; these
// helpers only change presentation. Mirrors output_workbench._fmt/_time and
// _labels.fit_label so the Python SVG and this bundle print the same text.

export function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stripZeros(text: string): string {
  return text.indexOf(".") === -1 ? text : text.replace(/0+$/, "").replace(/\.$/, "");
}

// Python formats the exact binary value and breaks exact ties to even;
// Number#toFixed/toExponential break ties away from zero (3.625 -> "3.63"
// vs Python "3.62"). Round the exact decimal expansion instead.

/** Exact |x| as digits * 10^exponent (x finite, non-zero). */
function exact(x: number): { digits: string; exponent: number } {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, Math.abs(x));
  const high = view.getUint32(0);
  const low = view.getUint32(4);
  const biased = (high >>> 20) & 0x7ff;
  let mantissa = (BigInt(high & 0xfffff) << 32n) | BigInt(low);
  let power = biased - 1075;
  if (biased === 0) power = -1074; else mantissa |= 1n << 52n;
  let digits: string;
  let exponent = 0;
  if (power >= 0) {
    digits = (mantissa << BigInt(power)).toString();
  } else {
    digits = (mantissa * 5n ** BigInt(-power)).toString();
    exponent = power;
  }
  const trimmed = digits.replace(/0+$/, "");
  return { digits: trimmed || "0", exponent: exponent + (digits.length - trimmed.length) };
}

/** Round digits*10^exponent to a multiple of 10^place, ties to even. */
function roundAt(digits: string, exponent: number, place: number): bigint {
  if (exponent >= place) return BigInt(digits) * 10n ** BigInt(exponent - place);
  const cut = place - exponent;
  const padded = digits.length > cut ? digits : "0".repeat(cut - digits.length + 1) + digits;
  const kept = BigInt(padded.slice(0, padded.length - cut));
  const dropped = padded.slice(padded.length - cut);
  const half = dropped[0] > "5" || (dropped[0] === "5" && /[1-9]/.test(dropped.slice(1)));
  const tie = dropped[0] === "5" && !/[1-9]/.test(dropped.slice(1));
  return half || (tie && kept % 2n === 1n) ? kept + 1n : kept;
}

function sign(value: number): string {
  return value < 0 || Object.is(value, -0) ? "-" : "";
}

/** Python's "%.<decimals>f". */
export function pyFixed(value: number, decimals: number): string {
  if (!Number.isFinite(value)) return value > 0 ? "inf" : value < 0 ? "-inf" : "nan";
  let text = "0";
  if (value !== 0) {
    const { digits, exponent } = exact(value);
    text = roundAt(digits, exponent, -decimals).toString();
  }
  if (decimals > 0) {
    text = text.padStart(decimals + 1, "0");
    text = text.slice(0, text.length - decimals) + "." + text.slice(text.length - decimals);
  }
  return sign(value) + text;
}

/** Python's "%.<precision>g". */
export function pyG(value: number, precision = 3): string {
  if (!Number.isFinite(value)) return value > 0 ? "inf" : value < 0 ? "-inf" : "nan";
  const p = Math.max(1, precision);
  if (value === 0) return sign(value) + "0";
  const { digits, exponent } = exact(value);
  let top = digits.length - 1 + exponent;
  let rounded = roundAt(digits, exponent, top - (p - 1)).toString();
  if (rounded.length > p) {
    top += 1;
    rounded = rounded.slice(0, p);
  }
  if (top < -4 || top >= p) {
    const mantissa = stripZeros(rounded[0] + "." + rounded.slice(1));
    return sign(value) + mantissa + "e" + (top < 0 ? "-" : "+") + String(Math.abs(top)).padStart(2, "0");
  }
  return stripZeros(pyFixed(value, Math.max(0, p - 1 - top)));
}

/** Compact count/cost: output_workbench._fmt. */
export function fmt(value: number | null | undefined): string {
  const v = finite(value);
  if (v === null) return "n/a";
  if (Math.abs(v) >= 1000000) return pyFixed(v / 1000000, 2) + "M";
  if (Math.abs(v) >= 1000) return pyFixed(v / 1000, 2) + "K";
  return pyG(v, 3);
}

/** Adaptive duration from milliseconds: output_workbench._time. */
export function time(ms: number | null | undefined): string {
  const v = finite(ms);
  if (v === null) return "n/a";
  if (v >= 1000) return pyFixed(v / 1000, 2) + " s";
  if (v > 0 && v < 1) return pyFixed(v * 1000, 1) + " \u00b5s";
  return pyFixed(v, 2) + " ms";
}

const ELLIPSIS = "…";

function charWidth(ch: string, ratio: number): number {
  const cp = ch.codePointAt(0) || 0;
  if (cp > 0xffff || (cp >= 0x1100 && cp <= 0x115f) || (cp >= 0x2e80 && cp <= 0x9fff) ||
      (cp >= 0xac00 && cp <= 0xd7a3) || (cp >= 0xff00 && cp <= 0xff60) ||
      (cp >= 0xffe0 && cp <= 0xffe6)) return 2 * ratio;
  return ratio;
}

function widthPx(chars: string[], fontSize: number, ratio: number): number {
  let total = 0;
  for (const ch of chars) total += charWidth(ch, ratio);
  return total * fontSize;
}

function middleEllipsis(chars: string[], keep: number): string[] {
  if (keep <= 1) return [ELLIPSIS];
  if (chars.length <= keep) return chars;
  const dot = chars.lastIndexOf(".");
  if (dot <= 0 || dot >= chars.length - 1) return chars.slice(0, keep - 1).concat(ELLIPSIS);
  const budget = keep - 1;
  const table = chars.slice(0, dot + 1);
  const column = chars.slice(dot + 1);
  if (table.length < budget) {
    const tail = budget - table.length;
    return table.concat(ELLIPSIS, column.slice(Math.max(0, column.length - tail)));
  }
  const half = Math.floor(budget / 2);
  return chars.slice(0, half).concat(ELLIPSIS, chars.slice(chars.length - (budget - half)));
}

/** Heuristic label fitting by code point: _labels.fit_label. */
export function fitLabel(text: string, pxWidth: number, fontSize: number,
                         fontWidth = 0.59, preferMiddle = true): string {
  if (!text) return "";
  const chars = Array.from(text);
  if (widthPx(chars, fontSize, fontWidth) <= pxWidth) return text;
  const average = fontSize * fontWidth;
  if (average <= 0) return ELLIPSIS;
  const budget = Math.max(1, Math.trunc(pxWidth / average));
  let fitted = preferMiddle && chars.indexOf(".") !== -1
    ? middleEllipsis(chars, budget)
    : chars.slice(0, Math.max(0, budget - 1)).concat(ELLIPSIS);
  while (fitted.length && widthPx(fitted, fontSize, fontWidth) > pxWidth) {
    if (fitted.length <= 1) return ELLIPSIS;
    const at = fitted.indexOf(ELLIPSIS);
    fitted = at > 0 && at < fitted.length - 1
      ? fitted.slice(0, at).concat(ELLIPSIS, fitted.slice(at + 2))
      : fitted.slice(0, Math.max(0, fitted.length - 2)).concat(ELLIPSIS);
  }
  return fitted.join("");
}

/** Python's "{:,}" / output_bargraph.format_number for an integer. */
export function commas(value: number): string {
  const text = String(Math.trunc(Math.abs(value)));
  return (value < 0 ? "-" : "") + text.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Python ``len(text)`` (code points, not UTF-16 units). */
export const cpLength = (text: string) => Array.from(text).length;

/** Python ``text[start:end]`` by code point. */
export function cpSlice(text: string, start: number, end?: number): string {
  return Array.from(text).slice(start, end).join("");
}

/** Adaptive duration used by the tree and its details: output_tree._format_time. */
export function treeTime(ms: number): string {
  if (!(ms > 0)) return "—";
  if (ms >= 1000) return pyFixed(ms / 1000, 2) + " s";
  if (ms >= 1) return ms < 100 ? pyFixed(ms, 2) + " ms" : pyFixed(ms, 0) + " ms";
  return pyFixed(ms * 1000, 1) + " µs";
}

/** Estimate-vs-actual suffix shared by the tree and bar details (Python thresholds). */
export function estimateText(rows: number, estimated: number | null, prefix: string): string | null {
  if (estimated === null || !(estimated > 0)) return null;
  const ratio = rows / estimated;
  let text = "Est.rows: " + pyFixed(estimated, 0);
  if (ratio > 2) text += "  ⚠ " + prefix + "underestimate (" + pyFixed(ratio, 1) + "×)";
  else if (ratio < 0.5 && rows > 0) text += "  ⚠ " + prefix + "overestimate";
  return text;
}
