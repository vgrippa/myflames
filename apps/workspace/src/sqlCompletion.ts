/** Completion is a bounded reading aid, never an SQL parser or execution rule. */
import { KEYWORDS, quoteName, tokenize } from "./sqlHighlight";

export type TableRef = { schema: string; table: string; alias: string };
export type CompletionContext = {
  start: number; end: number; prefix: string; qualifiers: string[];
  place: "table" | "schema" | "expression";
  tables: TableRef[];
};
export type Suggestion = { label: string; insert: string; kind: "schema" | "table" | "column" | "alias" | "keyword"; detail: string };
type Lexeme = { text: string; name: string; start: number; end: number; depth: number; identifier: boolean };
const word = /[\p{L}\p{N}_$]/u;
const MAX_SQL = 60000;
const TABLE_WORDS = new Set(["FROM", "JOIN", "UPDATE", "INTO", "TABLE", "DESCRIBE", "DESC"]);
const COMMON_WORDS = ["SELECT", "FROM", "WHERE", "JOIN", "LEFT JOIN", "INNER JOIN", "ON", "GROUP BY", "ORDER BY", "HAVING", "LIMIT", "AS", "AND", "OR", "IS NULL", "IS NOT NULL", "DISTINCT", "INSERT INTO", "UPDATE", "DELETE FROM", "VALUES", "SET", "WITH", "EXPLAIN"];

function lex(sql: string, scanSql: string): Lexeme[] {
  const special = new Map(tokenize(scanSql).filter(t => ["string", "comment", "ident", "variable", "param"].includes(t.kind)).map(t => [t.start, t]));
  const result: Lexeme[] = [];
  let depth = 0;
  for (let i = 0; i < sql.length;) {
    const token = special.get(i);
    if (token) {
      if (token.kind === "ident") {
        const text = sql.slice(i, token.end);
        result.push({ text, name: text.slice(1, token.closed ? -1 : undefined).replace(/``/g, "`"), start: i, end: token.end, depth, identifier: true });
      }
      i = token.end;
      continue;
    }
    if (/\s/.test(sql[i])) { i++; continue; }
    const start = i;
    if (word.test(sql[i])) while (i < sql.length && word.test(sql[i])) i++;
    else i++;
    const text = sql.slice(start, i);
    if (text === ")") depth = Math.max(0, depth - 1);
    result.push({ text, name: text, start, end: i, depth, identifier: word.test(text[0]) && !KEYWORDS.has(text.toUpperCase()) });
    if (text === "(") depth++;
  }
  return result;
}

export function completionContext(sql: string, caret: number, end = caret, backslashEscapes = true): CompletionContext | null {
  if (sql.length > MAX_SQL || caret !== end || caret < 0 || caret > sql.length || /^\s*DELIMITER\b/im.test(sql)) return null;
  const scanSql = backslashEscapes ? sql : sql.replace(/\\/g, " ");
  const special = tokenize(scanSql);
  for (const token of special) {
    if (!["string", "comment", "variable", "param"].includes(token.kind)) continue;
    const atEnd = caret === token.end && (token.closed === false || token.kind === "variable" || token.kind === "param" ||
      (token.kind === "comment" && !sql.slice(token.start).startsWith("/*")));
    if (caret > token.start && (caret < token.end || atEnd)) return null;
  }
  const tokens = lex(sql, scanSql);
  const current = tokens.find(t => t.start < caret && caret <= t.end && (t.identifier || word.test(t.text[0])));
  let start = caret, replaceEnd = caret, prefix = "";
  if (current) {
    start = current.start; replaceEnd = current.end;
    // An unfinished backtick must not consume the rest of a draft statement.
    if (special.some(t => t.start === start && t.kind === "ident" && t.closed === false)) replaceEnd = caret;
    prefix = sql.slice(start, caret);
    if (prefix.startsWith("`")) prefix = prefix.slice(1).replace(/`$/, "").replace(/``/g, "`");
  }
  // A number, literal, variable or operator is never an identifier prefix.
  if (/^\d/.test(prefix) || (!current && caret > 0 && /['"@]/.test(sql[caret - 1]))) return null;
  const before = tokens.filter(t => t.end <= start);
  const qualifiers: string[] = [];
  let idx = before.length - 1;
  while (before[idx]?.text === "." && before[idx - 1]?.identifier) {
    qualifiers.unshift(before[idx - 1].name); idx -= 2;
  }
  if (qualifiers.length > 2) return null;
  const previous = before[idx]?.text.toUpperCase() || "";
  const place = previous === "USE" ? "schema" : TABLE_WORDS.has(previous) ? "table" : "expression";
  // Never resolve aliases from another statement or nested SELECT scope.
  const first = tokens.reduce((at, t) => t.text === ";" && t.end <= caret ? t.end : at, 0);
  const last = tokens.find(t => t.text === ";" && t.start >= caret)?.start ?? sql.length;
  const statement = tokens.filter(t => t.start >= first && t.end <= last);
  const selects = statement.filter(t => t.text.toUpperCase() === "SELECT" && t.start <= caret);
  const preceding = tokens.filter(t => t.start < caret);
  const caretDepth = preceding[preceding.length - 1]?.depth ?? 0;
  const queryDepth = [...selects].reverse().find(t => t.depth <= caretDepth)?.depth ?? 0;
  const scoped = statement.filter(t => t.depth === queryDepth);
  const tables: TableRef[] = [];
  const hasCTE = statement.some(t => t.depth === queryDepth && t.text.toUpperCase() === "WITH");
  if (!hasCTE) for (let i = 0; i < scoped.length - 1; i++) {
    if (!TABLE_WORDS.has(scoped[i].text.toUpperCase()) || !scoped[i + 1]?.identifier) continue;
    let j = i + 1;
    let schema = "", table = scoped[j++].name;
    if (scoped[j]?.text === "." && scoped[j + 1]?.identifier) { schema = table; table = scoped[j + 1].name; j += 2; }
    if (scoped[j]?.text === "(" || scoped[j]?.text === ".") continue;
    if (scoped[j]?.text.toUpperCase() === "AS") j++;
    const alias = scoped[j]?.identifier ? scoped[j].name : table;
    if (!tables.some(t => t.alias.toLowerCase() === alias.toLowerCase())) tables.push({ schema, table, alias });
  }
  return { start, end: replaceEnd, prefix, qualifiers, place, tables: tables.slice(0, 4) };
}

export function columnTargets(context: CompletionContext, database: string): TableRef[] {
  if (context.place !== "expression") return [];
  const q = context.qualifiers;
  if (q.length === 2) return [{ schema: q[0], table: q[1], alias: q[1] }];
  const refs = q.length === 1 ? context.tables.filter(t => t.alias.toLowerCase() === q[0].toLowerCase()) : context.tables;
  return refs.map(t => ({ ...t, schema: t.schema || database })).filter(t => t.schema);
}

export function suggestions(context: CompletionContext, metadata: {
  schemas?: string[]; tables?: { name: string; schema: string }[];
  columns?: { name: string; type: string; table: TableRef }[];
}): Suggestion[] {
  const out: Suggestion[] = [];
  const add = (label: string, kind: Suggestion["kind"], detail: string, insert = quoteName(label)) => {
    if (label.toLocaleLowerCase().startsWith(context.prefix.toLocaleLowerCase())) out.push({ label, kind, detail, insert });
  };
  for (const c of metadata.columns || []) add(c.name, "column", `${c.table.schema}.${c.table.table} · ${c.type}`,
    context.qualifiers.length || context.tables.length < 2 ? quoteName(c.name) : `${quoteName(c.table.alias)}.${quoteName(c.name)}`);
  if (!context.qualifiers.length && context.place === "expression") for (const t of context.tables) add(t.alias, "alias", `${t.schema ? t.schema + "." : ""}${t.table}`);
  for (const t of metadata.tables || []) add(t.name, "table", t.schema);
  if (!context.qualifiers.length) for (const s of metadata.schemas || []) add(s, "schema", "Database");
  if (!context.qualifiers.length && context.place === "expression") for (const k of COMMON_WORDS) add(k, "keyword", "SQL", k);
  const seen = new Set<string>();
  return out.filter(s => { const key = `${s.kind}:${s.insert}`; if (seen.has(key)) return false; seen.add(key); return true; }).slice(0, 40);
}
