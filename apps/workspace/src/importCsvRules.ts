// Import CSV's pure rules, shared by the wizard (ImportCsv.tsx) and unit
// tested without a browser (test/import-csv-unit.mjs): separator and
// encoding names, CSV-to-table column matching, names for a new table, and
// the column type grammar, which is the backend's (csv_import.parse_type).
// No imports, so the test can compile this file on its own.

/** The four named separators; anything else is Other (one character). */
export const SEPARATORS: { value: string; label: string; name: string }[] = [
  { value: ",", label: "Comma", name: "comma" },
  { value: ";", label: "Semicolon", name: "semicolon" },
  { value: "\t", label: "Tab", name: "tab" },
  { value: "|", label: "Pipe", name: "pipe" },
];
/** "comma", "tab", "space", or the character in quotes. */
export function separatorName(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "unknown";
  const named = SEPARATORS.find((s) => s.value === value);
  if (named) return named.name;
  if (value === " ") return "space";
  return `“${value}”`;
}
export const ENCODINGS: [string, string][] = [
  ["utf-8", "UTF-8"],
  ["utf-8-sig", "UTF-8 with BOM"],
  ["utf-16-le", "UTF-16 LE"],
  ["utf-16-be", "UTF-16 BE"],
  ["cp1252", "Latin-1 / Windows-1252"],
];
/** The option value for an encoding name as Python or the file may spell it. */
export function encodingValue(name: string | null | undefined): string {
  const n = (name || "").toLowerCase().replace(/_/g, "-");
  if (n === "utf8" || n === "utf-8") return "utf-8";
  if (n === "utf-8-sig" || n === "utf8-sig" || n === "utf-8-bom") return "utf-8-sig";
  if (n === "utf-16le" || n === "utf-16-le") return "utf-16-le";
  if (n === "utf-16be" || n === "utf-16-be") return "utf-16-be";
  if (["cp1252", "windows-1252", "latin-1", "latin1", "iso-8859-1", "iso8859-1"].includes(n)) return "cp1252";
  return name || "utf-8";
}
export function encodingLabel(name: string | null | undefined): string {
  const value = encodingValue(name);
  return ENCODINGS.find(([v]) => v === value)?.[1] || value;
}
export const QUOTES: [string, string][] = [
  ['"', "Double quote (\")"],
  ["'", "Single quote (')"],
  ["", "None"],
];

const loose = (text: string) => text.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");

/** The table column each CSV column fills, by header name (exact, then
 * ignoring case and punctuation), then by position for the rest. A table
 * column is used once. Without a header row only position counts. */
export function matchColumns(csv: { index: number; header: string }[], table: string[], header: boolean): (string | null)[] {
  const result: (string | null)[] = csv.map(() => null);
  const taken = new Set<string>();
  if (header) {
    for (const pass of [(s: string) => s.trim().toLowerCase(), loose]) {
      csv.forEach((c, i) => {
        if (result[i] !== null || !c.header.trim()) return;
        const hit = table.find((t) => !taken.has(t) && pass(t) === pass(c.header) && pass(t) !== "");
        if (hit) { result[i] = hit; taken.add(hit); }
      });
    }
  }
  csv.forEach((_c, i) => {
    if (result[i] !== null) return;
    const byPosition = table[i];
    if (byPosition !== undefined && !taken.has(byPosition)) { result[i] = byPosition; taken.add(byPosition); }
  });
  return result;
}

const MAX_NAME = 64;
function identifier(text: string, fallback: string): string {
  const cleaned = text.normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^A-Za-z0-9_$]+/g, "_")
    .replace(/_+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
  const name = cleaned || fallback;
  return (/^[0-9]+$/.test(name) ? `${fallback}_${name}` : name).slice(0, MAX_NAME);
}
/** A new table's name from the file name: "Orders 2026.csv" -> "orders_2026". */
export function tableNameFromFile(name: string): string {
  return identifier(name.replace(/\.(csv|tsv|txt)$/i, ""), "imported");
}
/** New table column names from the headers: cleaned, never empty, unique. */
export function columnNames(headers: string[]): string[] {
  const seen = new Map<string, number>();
  return headers.map((h, i) => {
    let name = identifier(h, `column_${i + 1}`);
    const key = name.toLowerCase();
    const n = seen.get(key) || 0;
    seen.set(key, n + 1);
    if (n) name = `${name.slice(0, MAX_NAME - 4)}_${n + 1}`;
    return name;
  });
}
/** Why a name cannot be a table or column name, or "". */
export function nameProblem(name: string): string {
  if (!name.trim()) return "Enter a name.";
  if (name !== name.trimEnd()) return "A name cannot end with a space.";
  if ([...name].length > MAX_NAME) return `At most ${MAX_NAME} characters.`;
  if (/[\u0000/\\.]/.test(name)) return "No dots, slashes or NUL characters.";
  return "";
}
/** Types offered for a new column, after the one inferred from the data. */
export const TYPE_CHOICES = [
  "INT", "INT UNSIGNED", "BIGINT", "BIGINT UNSIGNED", "TINYINT(1)", "DECIMAL(10,2)", "DOUBLE",
  "DATE", "DATETIME", "TIMESTAMP", "TIME", "YEAR", "VARCHAR(255)", "TEXT", "MEDIUMTEXT", "LONGTEXT", "JSON",
];
// The type grammar of a new table's column, the same as the backend's
// csv_import.parse_type (myflames/csv_import.py): a fixed list of base types,
// one optional parenthesized argument list and an optional UNSIGNED, with the
// same limits and the same words, so a type it refuses never reaches /plan.
const TYPE_RE = /^\s*(tinyint|smallint|mediumint|int|integer|bigint|decimal|numeric|float|double|date|datetime|timestamp|time|year|char|varchar|tinytext|text|mediumtext|longtext|binary|varbinary|tinyblob|blob|mediumblob|longblob|json|bit|bool|boolean|enum|set)\s*(?:\(([^()]*)\))?\s*(unsigned)?\s*$/is;
const INTEGER_TYPES = ["tinyint", "smallint", "mediumint", "int", "integer", "bigint"];
const NO_LENGTH = ["date", "year", "json", "tinytext", "text", "mediumtext", "longtext", "tinyblob", "blob", "mediumblob", "longblob"];
/** ENUM or SET members as the server prints them: quotes doubled, backslash escapes. */
function parseMembers(text: string): string[] {
  const members: string[] = [];
  let i = 0;
  while (i < text.length) {
    if (text[i] === " " || text[i] === ",") { i++; continue; }
    if (text[i] !== "'") throw new Error("The ENUM or SET members could not be read.");
    i++;
    let out = "";
    for (;;) {
      if (i >= text.length) throw new Error("The ENUM or SET members could not be read.");
      const c = text[i];
      if (c === "'") {
        if (text[i + 1] === "'") { out += "'"; i += 2; continue; }
        i++;
        break;
      }
      if (c === "\\" && i + 1 < text.length) {
        const next = text[i + 1];
        out += ({ "0": "\0", n: "\n", r: "\r", "\\": "\\", "'": "'", Z: "\x1a", t: "\t" } as Record<string, string>)[next] ?? next;
        i += 2;
        continue;
      }
      out += c;
      i++;
    }
    members.push(out);
  }
  return members;
}
/** Why a new column's type would be refused (the backend's words), or "". */
export function typeProblem(text: string): string {
  if (!text.trim() || text.length > 4000) return "Choose a column type.";
  const match = TYPE_RE.exec(text);
  if (!match) return `${text.trim().slice(0, 40)} is not a type Import CSV can create.`;
  let base = match[1].toLowerCase();
  const args = match[2];
  const unsigned = !!match[3];
  const BASE = base.toUpperCase();
  if (base === "bool" || base === "boolean") return args !== undefined || unsigned ? "BOOLEAN takes no length." : "";
  if (base === "integer") base = "int";
  if (base === "enum" || base === "set") {
    const bad = `${BASE} members must be quoted text without backslashes or control characters.`;
    if (!args || unsigned) return `${BASE} needs its members, like ${BASE}('a','b').`;
    if (args.includes("\\")) return bad;
    let members: string[];
    try { members = parseMembers(args); } catch (e) { return (e as Error).message; }
    // eslint-disable-next-line no-control-regex
    if (!members.length || members.some((m) => m.includes("\\") || /[\u0000-\u001f]/.test(m))) return bad;
    if (members.length > (base === "enum" ? 65535 : 64) || new Set(members.map((m) => m.toLowerCase())).size !== members.length)
      return `${BASE} members must be distinct (at most ${base === "enum" ? "65,535" : "64"}).`;
    return "";
  }
  let numbers: number[] = [];
  if (args !== undefined) {
    if (!/^\s*\d+\s*(,\s*\d+\s*)?$/.test(args)) return `${base.toUpperCase()} takes whole numbers in parentheses.`;
    numbers = args.split(",").map((x) => Number(x.trim()));
  }
  const NAME = base.toUpperCase();
  const decimalLike = ["decimal", "numeric", "float", "double"].includes(base);
  if (unsigned && !INTEGER_TYPES.includes(base) && !decimalLike) return "UNSIGNED applies to numbers only.";
  if (unsigned && decimalLike) return `UNSIGNED is deprecated for ${NAME}; use a CHECK in the Query editor.`;
  if (INTEGER_TYPES.includes(base))
    return numbers.length && (numbers.length !== 1 || numbers[0] < 1 || numbers[0] > 255) ? `${NAME} takes at most one display width.` : "";
  if (base === "decimal" || base === "numeric") {
    const precision = numbers.length ? numbers[0] : 10;
    const scale = numbers.length > 1 ? numbers[1] : 0;
    return precision < 1 || precision > 65 || scale < 0 || scale > 30 || scale > precision
      ? "DECIMAL precision is 1 to 65 and scale 0 to 30, no larger than the precision." : "";
  }
  if (base === "float" || base === "double" || NO_LENGTH.includes(base)) return numbers.length ? `Write ${NAME} without a length.` : "";
  if (base === "datetime" || base === "timestamp" || base === "time") {
    const fsp = numbers.length ? numbers[0] : 0;
    return numbers.length > 1 || fsp < 0 || fsp > 6 ? `${NAME} takes a fractional precision from 0 to 6.` : "";
  }
  if (base === "bit") {
    const width = numbers.length ? numbers[0] : 1;
    return numbers.length > 1 || width < 1 || width > 64 ? "BIT takes a width from 1 to 64." : "";
  }
  // char, varchar, binary, varbinary
  if (numbers.length !== 1) return `${NAME} needs a length, like ${NAME}(64).`;
  const limit = ({ char: 255, binary: 255, varchar: 16383, varbinary: 65532 } as Record<string, number>)[base];
  const low = base === "char" || base === "binary" ? 0 : 1;
  return numbers[0] < low || numbers[0] > limit ? `${NAME} length must be at most ${limit}.` : "";
}
/** A progress id for /apply and /progress (16 random bytes as hex). */
export function progressId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
