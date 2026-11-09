// Words for Compare: the plan time change and the operator matching
// metadata from plan_matching.match_plan_nodes (never raw enums on screen).
const formats = new Map<string, Intl.NumberFormat>();
/** The reader's locale with these options (thousands separators included). */
function formatNumber(value: number, options: Intl.NumberFormatOptions): string {
  const key = JSON.stringify(options);
  let format = formats.get(key);
  if (!format) formats.set(key, (format = new Intl.NumberFormat(undefined, options)));
  return format.format(value);
}

/** Total time change as words. A change of 100% or more reads as a ratio
 * ("750× slower", "2.4× faster"); smaller changes read as a percentage. */
export function changeText(pct: number | null | undefined): string {
  if (typeof pct !== "number" || !Number.isFinite(pct)) return "—";
  if (pct === 0) return "No change";
  const ratio = 1 + pct / 100;
  if (pct >= 100) return `${times(ratio)} slower`;
  if (ratio > 0 && ratio <= 0.5) return `${times(1 / ratio)} faster`;
  const amount = formatNumber(Math.abs(pct), { maximumFractionDigits: Math.abs(pct) < 10 ? 1 : 0 });
  return `${amount}% ${pct > 0 ? "slower" : "faster"}`;
}

/** "750×", "12.3×", "2.4×": whole numbers with separators from 100×. */
function times(ratio: number): string {
  return formatNumber(ratio, { maximumFractionDigits: ratio >= 100 ? 0 : 1 }) + "×";
}

/** The exact change for a tooltip: "+74,867.34%". */
export function changeExact(pct: number | null | undefined): string {
  if (typeof pct !== "number" || !Number.isFinite(pct)) return "";
  return (pct > 0 ? "+" : "") + formatNumber(pct, { maximumFractionDigits: 2 }) + "%";
}

const METHODS: Record<string, string> = {
  unique_subtree: "Same subtree",
  unique_operator: "Same operator",
  parent_context: "Same parent",
  unique_table: "Same table",
  child_context: "Same inputs",
  ambiguous_operator: "Matched by position",
  unmatched: "No counterpart",
};
const CONFIDENCE: Record<string, string> = {
  high: "high confidence",
  medium: "medium confidence",
  uncertain: "tentative",
};
const STATUS: Record<string, string> = {
  added: "Added",
  removed: "Removed",
  changed: "Changed",
  unchanged: "Unchanged",
  uncertain: "Uncertain",
};

/** Sentence-case words for an unknown enum: "some_value" -> "Some value". */
function words(value: string): string {
  const text = value.replace(/_/g, " ").trim();
  return text ? text[0].toUpperCase() + text.slice(1) : "";
}

/** How an operator pair was matched: "Same operator · high confidence". */
export function matchText(matching: { method: string; confidence: string }): string {
  const method = METHODS[matching.method] || words(matching.method);
  if (matching.method === "unmatched") return method;
  return [method, CONFIDENCE[matching.confidence] || matching.confidence].filter(Boolean).join(" · ");
}

/** The structural status badge: "Removed", "Changed". */
export function statusText(status: string): string {
  return STATUS[status] || words(status);
}

/** A finding's severity in words: "Critical", "Warning", "Note". */
export function severityText(severity: string): string {
  return ({ critical: "Critical", warn: "Warning", warning: "Warning", info: "Note" } as Record<string, string>)[severity] || words(severity);
}

/** The pair's title: one name when both sides carry the same label. */
export function pairTitle(before: string | null | undefined, after: string | null | undefined): string {
  if (!before) return after || "Added operator";
  if (!after) return before;
  return before === after ? before : `${before} → ${after}`;
}
