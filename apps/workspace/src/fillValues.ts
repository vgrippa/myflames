// The form that asks for a Top statements tab's parameter values: the SQL
// around each :pN marker, a type guessed from it, and the values it writes
// into the tab's Parameters JSON (which stays the source of truth). Pure
// functions, no DOM: test/fill-values.mjs runs them in Node.

export type FillType = "number" | "text" | "null";
/** One parameter of the form. *list*: it replaced a collapsed list (IN (...)). */
export type FillField = { name: string; list: boolean; context: string; type: FillType };
/** What the user chose for one parameter. *edited*: false while the form
 * shows the value the Parameters JSON already has; only edited entries are
 * written back. *locked*: the current value as JSON when the field cannot
 * show it without changing it (a list with NULL in it, text with a comma),
 * with the *reason*; the form shows it read-only and never rewrites it. */
export type FillEntry = { type: FillType; value: string; edited?: boolean; locked?: string; reason?: string };

/** Stands for the marker in the SQL shown beside each field. */
export const MARK = "▢";
/** Doubles keep 15 significant digits exactly; longer numbers would change. */
export const EXACT_DIGITS = 15;

/** Quoted text and comments: a marker inside them is not a parameter. */
const SKIP =
  /(\/\*[\s\S]*?(?:\*\/|$)|--(?=\s|$)[^\r\n]*|#[^\r\n]*|'(?:''|\\[\s\S]|[^'\\])*'?|"(?:""|\\[\s\S]|[^"\\])*"?|`(?:``|[^`])*`?)/
    .source;
const NUMBER = /^-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$/;
/** Column names that hold numbers: ids, counts, amounts, sizes, times. */
const NUMERIC_WORD =
  "id|count|cnt|num|number|qty|quantity|amount|price|cost|total|sum|age|year|size|length|len|score|rank|level|limit|offset|days?|hours?|minutes?|seconds?|ms|bytes|rows|version|port|pid";
const NUMERIC_NAME = new RegExp(`^(?:${NUMERIC_WORD})$|_(?:${NUMERIC_WORD})$|[a-z]Id$`, "i");
/** The column compared with the marker: `col = `, `col IN (`, `col BETWEEN x AND `. */
const COMPARED =
  /([`"]?)([A-Za-z_][\w$]*)\1\s*(?:<=>|<>|!=|<=|>=|=|<|>|(?:NOT\s+)?IN\s*\(|(?:NOT\s+)?BETWEEN(?:\s+\S+\s+AND)?)\s*$/i;

/** Where `:name` first appears outside quotes and comments, or null. */
export function findMarker(sql: string, name: string): { start: number; end: number } | null {
  const pattern = new RegExp(`${SKIP}|(?<!:)(:${name})(?![A-Za-z0-9_])`, "g");
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(sql || ""))) {
    if (m[2] !== undefined) return { start: m.index, end: m.index + m[2].length };
    if (!m[0]) pattern.lastIndex++;
  }
  return null;
}

/** A few words of SQL around the marker, the marker drawn as ▢:
 * "… `customer_id` IN (▢) AND …". Empty when the marker is not found. */
export function markerContext(sql: string, name: string): string {
  const at = findMarker(sql, name);
  if (!at) return "";
  const flat = (s: string) => s.replace(/\s+/g, " ");
  const before = flat(sql.slice(0, at.start));
  const after = flat(sql.slice(at.end));
  let pre = /(?:\S+ ?){0,3}$/.exec(before)?.[0] ?? "";
  let post = /^(?: ?\S+){0,2}/.exec(after)?.[0] ?? "";
  if (pre.length > 40) pre = pre.slice(pre.length - 40);
  if (post.length > 24) post = post.slice(0, 24);
  const head = before.trimEnd().length > pre.trimEnd().length ? "… " : "";
  const tail = after.trim().length > post.trim().length ? " …" : "";
  return `${head}${pre}${MARK}${post}${tail}`;
}

/** Number when the SQL around the marker is numeric (LIMIT, arithmetic, a
 * column named like an id or a count); Text otherwise. A guess the user changes. */
export function guessType(sql: string, name: string): FillType {
  const at = findMarker(sql, name);
  if (!at) return "text";
  const before = sql.slice(Math.max(0, at.start - 160), at.start).replace(/\s+/g, " ");
  if (/\b(?:LIMIT|OFFSET)\s*$/i.test(before) || /\bLIMIT\s+\S+\s*,\s*$/i.test(before)) return "number";
  if (/[-+*/%]\s*$/.test(before) && !/--\s*$/.test(before)) return "number";
  const compared = COMPARED.exec(before);
  return compared && NUMERIC_NAME.test(compared[2]) ? "number" : "text";
}

/** The form's fields for *names* (in order) of a tab's SQL. */
export function fillFields(sql: string, names: string[], lists: string[] = []): FillField[] {
  return names.map((name) => ({
    name,
    list: lists.includes(name),
    context: markerContext(sql, name),
    type: guessType(sql, name),
  }));
}

/** Why the form cannot show *v* in its field without changing it (written
 * back, a comma-separated field would drop or split something), or "" when
 * it can. A list field takes "1, 2, 3" or "a, b, c"; a single value is a
 * number, a text or NULL. */
export function unshownReason(v: unknown, list: boolean): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isFinite(v) ? "" : "A value of another kind";
  if (typeof v === "string") {
    if (v === "") return "An empty text";
    if (list && v.includes(",")) return "Text with a comma in it";
    if (list && v.trim() !== v) return "Text with spaces at either end";
    return "";
  }
  if (!Array.isArray(v) || !list) return "A value of another kind";
  if (!v.length) return "An empty list";
  if (v.some((x) => x === null)) return v.length === 1 ? "" : "A list with NULL in it";
  if (v.every((x) => typeof x === "number")) return v.every((x) => Number.isFinite(x)) ? "" : "A value of another kind";
  if (!v.every((x) => typeof x === "string")) {
    return v.every((x) => typeof x === "number" || typeof x === "string") ? "A list of numbers and text" : "A value of another kind";
  }
  if (v.some((x) => x === "")) return "A list with an empty text in it";
  if (v.some((x) => (x as string).includes(","))) return "Text with a comma in it";
  if (v.some((x) => (x as string).trim() !== x)) return "Text with spaces at either end";
  return "";
}

/** The form's starting entries: values already in the Parameters JSON, else
 * the guessed type and an empty value. Nothing is edited yet; a value the
 * field cannot show as is comes back *locked* (its JSON) with its reason. */
export function startEntries(fields: FillField[], parametersText: string): Record<string, FillEntry> {
  const current = parseObject(parametersText) || {};
  const out: Record<string, FillEntry> = {};
  for (const f of fields) {
    const v = current[f.name];
    const reason = unshownReason(v, f.list);
    if (reason) {
      out[f.name] = { type: f.type, value: "", edited: false, locked: JSON.stringify(v), reason };
      continue;
    }
    const first = Array.isArray(v) ? v.find((x) => x !== null) : v;
    const allNull = Array.isArray(v) && v.length > 0 && v.every((x) => x === null);
    const type: FillType =
      allNull ? "null" : typeof first === "number" ? "number" : typeof first === "string" ? "text" : f.type;
    const value = allNull ? "" : Array.isArray(v) ? v.map((x) => String(x)).join(", ")
      : typeof v === "number" || typeof v === "string" ? String(v) : "";
    out[f.name] = { type, value, edited: false };
  }
  return out;
}

function parseObject(text: string): Record<string, unknown> | null {
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** Significant digits of a decimal number written as text. */
function digits(text: string) {
  const mantissa = text.replace(/^-/, "").replace(/[eE].*$/, "").replace(".", "");
  return mantissa.replace(/^0+/, "").replace(/0+$/, "").length || 1;
}

/** One number as typed: a finite double that keeps every digit, or why not. */
function number(text: string): { value: number } | { error: string } {
  if (!NUMBER.test(text)) return { error: `“${text}” is not a number. Choose Text for other values.` };
  const value = Number(text);
  if (!Number.isFinite(value)) return { error: `“${text}” is out of range.` };
  // Integer digits that are only zeros (1e20) still count: trailing zeros of
  // an integer part are significant.
  const plain = !/[.eE]/.test(text);
  const count = plain ? text.replace(/^-?0*/, "").length : digits(text);
  if (count > EXACT_DIGITS)
    return { error: `${text} has more than ${EXACT_DIGITS} digits and would lose precision. Write it into the SQL instead.` };
  return { value };
}

/** The JSON value for one entry: `{empty: true}` when nothing was typed. */
export function entryValue(entry: FillEntry, list: boolean): { value: unknown } | { empty: true } | { error: string } {
  if (entry.type === "null") return { value: list ? [null] : null };
  if (list) {
    const items = entry.value.split(",").map((x) => x.trim()).filter((x) => x !== "");
    if (!items.length) return { empty: true };
    if (items.length > 1000) return { error: "At most 1,000 values." };
    if (entry.type === "text") return { value: items };
    const out: number[] = [];
    for (const item of items) {
      const n = number(item);
      if ("error" in n) return n;
      out.push(n.value);
    }
    return { value: out };
  }
  if (entry.type === "text") return entry.value === "" ? { empty: true } : { value: entry.value };
  const text = entry.value.trim();
  if (!text) return { empty: true };
  return number(text);
}

export type FillOutcome =
  | { ok: true; parameters: string; filled: string[]; remaining: string[] }
  | { ok: false; errors: Record<string, string> };

/** The Parameters JSON with the edited entries written in. Other keys, and
 * parameters the user did not edit or that are locked, stay exactly as they
 * were (a null one is still to fill in); an edited entry left empty is null
 * again, as the form showed the current values. */
export function applyEntries(parametersText: string, fields: FillField[], entries: Record<string, FillEntry>): FillOutcome {
  const base = parseObject(parametersText) || {};
  const errors: Record<string, string> = {};
  const filled: string[] = [];
  const remaining: string[] = [];
  for (const f of fields) {
    const entry = entries[f.name];
    if (!entry || entry.edited === false || entry.locked !== undefined) {
      if (base[f.name] === null || base[f.name] === undefined) remaining.push(f.name);
      continue;
    }
    const result = entryValue(entry, f.list);
    if ("error" in result) errors[f.name] = result.error;
    else if ("empty" in result) {
      remaining.push(f.name);
      base[f.name] = null;
    } else {
      base[f.name] = result.value;
      filled.push(f.name);
    }
  }
  if (Object.keys(errors).length) return { ok: false, errors };
  return { ok: true, parameters: JSON.stringify(base, null, 2), filled, remaining };
}
