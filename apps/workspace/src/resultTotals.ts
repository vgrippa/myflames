// Aggregates for the Query editor result grid's selected cells (the totals
// footer). Pure math, no DOM, so Node can unit-test it (test/result-totals.mjs).
// NULL is skipped and counted; plain decimals are exact (BigInt fixed-point,
// so BIGINT UNSIGNED and DECIMAL never round); one value with an exponent
// switches every aggregate to doubles and marks the line approximate.

export type Totals = {
  /** Every selected cell. */
  cells: number;
  /** Selected cells that are NULL (skipped by the aggregates). */
  nulls: number;
  /** Selected cells with a numeric value (exact decimals plus exponents). */
  numericCount: number;
  sum: string | null;
  avg: string | null;
  min: string | null;
  max: string | null;
  /** True when an exponent value forced the double path. */
  approximate: boolean;
};

/** A plain decimal: sign, integer digits, optional fraction (no exponent). */
const PLAIN = /^(-?)(\d*)(?:\.(\d*))?$/;
/** A number with an exponent (FLOAT/DOUBLE text like 1e10 or -2.5E-3). */
const EXPONENT = /^-?(?:\d+\.?\d*|\.\d+)[eE][-+]?\d+$/;

/** v as text with the point scale digits from the left of the right end;
 * trailing fraction zeros are trimmed ("150" at scale 2 is "1.5"). */
function formatScaled(v: bigint, scale: number): string {
  if (scale <= 0) return v.toString();
  const neg = v < 0n;
  const digits = (neg ? -v : v).toString().padStart(scale + 1, "0");
  const int = digits.slice(0, digits.length - scale);
  const frac = digits.slice(digits.length - scale).replace(/0+$/, "");
  const out = frac ? `${int}.${frac}` : int;
  return neg ? `-${out}` : out;
}

export function totals(values: (string | null)[]): Totals {
  const out: Totals = {
    cells: values.length,
    nulls: 0,
    numericCount: 0,
    sum: null,
    avg: null,
    min: null,
    max: null,
    approximate: false,
  };
  const exact: { neg: boolean; int: string; frac: string }[] = [];
  const numbers: number[] = [];
  let exponent = false;
  for (const value of values) {
    if (value === null) {
      out.nulls++;
      continue;
    }
    const m = PLAIN.exec(value);
    if (m && (m[2] !== "" || (m[3] || "") !== "")) {
      exact.push({ neg: m[1] === "-", int: m[2], frac: m[3] || "" });
      numbers.push(Number(value));
      out.numericCount++;
      continue;
    }
    if (EXPONENT.test(value)) {
      exponent = true;
      numbers.push(Number(value));
      out.numericCount++;
    }
    // Anything else (text, 0x hex, dates) counts into cells only.
  }
  if (out.numericCount === 0) return out;
  if (exponent) {
    // Doubles for every aggregate: the exact values would survive BigInt,
    // but one shared scale keeps the line honest, and it says approximate.
    out.approximate = true;
    let sum = 0;
    let min = numbers[0];
    let max = numbers[0];
    for (const n of numbers) {
      sum += n;
      if (n < min) min = n;
      if (n > max) max = n;
    }
    out.sum = String(sum);
    out.avg = String(sum / numbers.length);
    out.min = String(min);
    out.max = String(max);
    return out;
  }
  // Exact: scale every value by 10^F (F the longest fraction) and work on
  // BigInts, so 9223372036854775807 + 1 and 0.1 + 0.2 stay exact.
  const scale = exact.reduce((f, v) => Math.max(f, v.frac.length), 0);
  const scaled = exact.map((v) => {
    const digits = (v.int + v.frac.padEnd(scale, "0")).replace(/^0+(?=\d)/, "") || "0";
    return BigInt((v.neg ? "-" : "") + digits);
  });
  let sum = 0n;
  let min = scaled[0];
  let max = scaled[0];
  for (const s of scaled) {
    sum += s;
    if (s < min) min = s;
    if (s > max) max = s;
  }
  out.sum = formatScaled(sum, scale);
  out.min = formatScaled(min, scale);
  out.max = formatScaled(max, scale);
  // Avg: six digits past the shared scale, rounded half away from zero.
  const n = BigInt(scaled.length);
  const num = sum * 1000000n;
  let q = num / n;
  const r = num % n;
  if ((r < 0n ? -r : r) * 2n >= n) q += num < 0n ? -1n : 1n;
  out.avg = formatScaled(q, scale + 6);
  return out;
}

/** The footer line: "Count 3 · Sum 6 · Avg 2 · Min 1 · Max 3" for numbers,
 * "4 cells" otherwise; NULLs are skipped and counted apart. */
export function totalsText(t: Totals): string {
  const n = (v: number) => v.toLocaleString("en-US");
  const parts: string[] = [];
  if (t.numericCount) {
    if (t.cells !== t.numericCount) parts.push(`${n(t.cells)} cells`);
    parts.push(`Count ${n(t.numericCount)}`, `Sum ${t.sum}`, `Avg ${t.avg}`, `Min ${t.min}`, `Max ${t.max}`);
  } else parts.push(`${n(t.cells)} ${t.cells === 1 ? "cell" : "cells"}`);
  if (t.nulls) parts.push(`${n(t.nulls)} NULL`);
  if (t.approximate) parts.push("approximate (floating point)");
  return parts.join(" · ");
}

/**
 * *text* laid out as indented JSON when it is a JSON object or array, else
 * null. Numbers and strings are copied exactly as written, so a BIGINT or a
 * long DECIMAL inside the document never rounds (JSON.parse only checks it).
 */
export function prettyJson(text: string, indent = 2): string | null {
  const source = text.trim();
  if (!/^[[{]/.test(source)) return null;
  try {
    JSON.parse(source);
  } catch {
    return null;
  }
  const pad = (depth: number) => "\n" + " ".repeat(depth * indent);
  let out = "";
  let depth = 0;
  for (let i = 0; i < source.length; i++) {
    const ch = source[i];
    if (ch === '"') {
      let j = i + 1;
      while (j < source.length && source[j] !== '"') j += source[j] === "\\" ? 2 : 1;
      out += source.slice(i, j + 1);
      i = j;
    } else if (ch === "{" || ch === "[") {
      let j = i + 1;
      while (/\s/.test(source[j] || "")) j++;
      if (source[j] === (ch === "{" ? "}" : "]")) {
        out += ch + source[j];
        i = j;
      } else out += ch + pad(++depth);
    } else if (ch === "}" || ch === "]") out += pad(--depth) + ch;
    else if (ch === ",") out += "," + pad(depth);
    else if (ch === ":") out += ": ";
    else if (!/\s/.test(ch)) out += ch;
  }
  return out;
}
