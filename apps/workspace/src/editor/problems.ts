import { StateEffect, type EditorState, type Extension } from "@codemirror/state";
import { ViewPlugin, type EditorView, type ViewUpdate } from "@codemirror/view";
import { forceLinting, linter, lintGutter, type Diagnostic } from "@codemirror/lint";
import type { Connection } from "../api";
import type { TableRef } from "../sqlCompletion";
import { HIGHLIGHT_LIMIT, KEYWORDS, tokenize } from "../sqlHighlight";
import { splitStatements } from "../sqlSelection";
import { catalogFor, type CatalogService } from "./catalog";
import type { EditorEnv, EditorFeatureSettings } from "./types";

type Word = { text: string; name: string | null; kw: string; from: number; to: number; depth: number };
type Scope = { depth: number; words: Word[]; refs: Map<string, TableRef | null> };
const fold = (name: string) => name.toLowerCase();
const word = /[A-Za-z0-9_$\u0080-\uffff]/;
const clauseEnd = new Set(["WHERE", "SET", "GROUP", "HAVING", "ORDER", "LIMIT", "WINDOW", "RETURNING", "UNION", "EXCEPT", "INTERSECT", "FOR", "LOCK", "VALUES"]);
const notName = new Set([...clauseEnd, "SELECT", "FROM", "JOIN", "INNER", "LEFT", "RIGHT", "CROSS", "NATURAL", "OUTER", "ON", "USING", "AS", "INTO"]);
const nameAt = (w: Word | undefined) => w?.name && !notName.has(w.kw) ? w.name : null;
const CREATE_TEMPORARY_TABLE = /^(?:\s|\/\*(?:[^*]|\*(?!\/))*\*\/|--(?=\s|$)[^\r\n]*(?=[\r\n]|$)|#[^\r\n]*(?=[\r\n]|$))*CREATE\s+(?:OR\s+REPLACE\s+)?TEMPORARY\s+TABLE\b/i;

export function createsTemporaryTable(text: string): boolean {
  return CREATE_TEMPORARY_TABLE.test(text);
}

/** Same tokenizer and backslash mask as completion. Keep offsets and opaque
 * literal tokens: dropping literals could join unrelated names across them.
 * This is deliberately a bounded reading aid, not an SQL validity checker. */
function lex(sql: string, offset: number, backslash: boolean) {
  const special = new Map(tokenize(backslash ? sql : sql.replace(/\\/g, " "))
    .filter(t => ["comment", "string", "ident", "variable", "param"].includes(t.kind)).map(t => [t.start, t]));
  const words: Word[] = [];
  let depth = 0, valid = true, executable = false;
  for (let i = 0; i < sql.length;) {
    const token = special.get(i);
    if (token) {
      const text = sql.slice(i, token.end);
      if (token.closed === false) valid = false;
      if (token.kind === "comment") executable ||= /^\/\*(?:!|M!)/.test(text);
      else words.push({ text, name: token.kind === "ident" && token.closed ? text.slice(1, -1).replace(/``/g, "`") : null,
        kw: "", from: offset + i, to: offset + token.end, depth });
      i = token.end;
      continue;
    }
    if (/\s/.test(sql[i])) { i++; continue; }
    const start = i;
    if (word.test(sql[i])) while (i < sql.length && word.test(sql[i])) i++;
    else i++;
    const text = sql.slice(start, i), identifier = word.test(text[0]) && !/^\d+$/.test(text);
    if (text === ")" && --depth < 0) valid = false;
    words.push({ text, name: identifier ? text : null,
      kw: identifier && words[words.length - 1]?.text !== "." ? text.toUpperCase() : "",
      from: offset + start, to: offset + i, depth });
    if (text === "(") depth++;
  }
  return { words, valid: valid && depth === 0, executable };
}

function tableName(words: Word[], at: number) {
  const first = nameAt(words[at]);
  if (!first) return null;
  if (words[at + 1]?.text === ".") {
    const second = words[at + 2]?.name;
    return second ? { schema: first, table: second, token: words[at + 2], end: at + 3 } : null;
  }
  return { schema: "", table: first, token: words[at], end: at + 1 };
}

/** Collect local names before inspecting references, including recursive CTEs.
 * A name seen in any CTE scope is conservatively exempt throughout its statement. */
function cteNames(words: Word[]): Set<string> {
  const names = new Set<string>();
  for (let i = 0; i < words.length; i++) {
    if (words[i].kw !== "WITH") continue;
    const depth = words[i].depth;
    let j = i + 1;
    if (words[j]?.kw === "RECURSIVE") j++;
    while (nameAt(words[j])) {
      const name = words[j++].name!;
      // Optional CTE column names.
      if (words[j]?.text === "(") {
        j++;
        while (j < words.length && !(words[j].text === ")" && words[j].depth === depth)) j++;
        j++;
      }
      if (words[j]?.kw !== "AS" || words[j + 1]?.text !== "(") break;
      names.add(fold(name));
      j += 2;
      while (j < words.length && !(words[j].text === ")" && words[j].depth === depth)) j++;
      if (words[++j]?.text !== ",") break;
      j++;
    }
  }
  return names;
}

/** Nested SELECTs and set-operation branches must not borrow another alias's
 * columns. Ordinary expression parentheses keep the enclosing query scope. */
function scopesOf(words: Word[]): Scope[] {
  const make = (depth: number): Scope => ({ depth, words: [], refs: new Map() });
  let current = make(0);
  const scopes = [current], stack: Scope[] = [];
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (w.text === ")") current = stack.pop() || current;
    if (["UNION", "EXCEPT", "INTERSECT"].includes(w.kw) && w.depth === current.depth) {
      current = make(w.depth); scopes.push(current);
    }
    current.words.push(w);
    if (w.text === "(") {
      stack.push(current);
      if (["SELECT", "WITH", "TABLE", "VALUES"].includes(words[i + 1]?.kw)) {
        current = make(w.depth + 1); scopes.push(current);
      }
    }
  }
  return scopes;
}

const tableKey = (schema: string, table: string) => JSON.stringify([fold(schema), fold(table)]);
const warning = (token: Word, message: string): Diagnostic => ({ from: token.from, to: token.to, severity: "warning", message });
function localTime(readAt: number) {
  const date = new Date(readAt);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

function metadataProblems(words: Word[], database: string, created: Set<string>, catalog: Pick<CatalogService, "peek">, sessionTempTables = false): Diagnostic[] {
  const out: Diagnostic[] = [], locals = cteNames(words), tableWords = new Set<Word>();
  for (const scope of scopesOf(words)) {
    const top = scope.words.filter(w => w.depth === scope.depth);
    const command = top[0]?.kw === "WITH"
      ? top.find(w => ["SELECT", "UPDATE", "DELETE", "INSERT", "REPLACE"].includes(w.kw))?.kw
      : top.find(w => !["UNION", "EXCEPT", "INTERSECT", "ALL", "DISTINCT"].includes(w.kw))?.kw;
    if (!["SELECT", "UPDATE", "DELETE", "INSERT", "REPLACE"].includes(command || "")) continue;
    const using = command === "DELETE" ? top.findIndex((w, i) => w.kw === "USING" && top[i + 1]?.text !== "(") : -1;
    let tables = false;
    const bind = (alias: string, ref: TableRef | null) => {
      const key = fold(alias);
      scope.refs.set(key, scope.refs.has(key) ? null : ref);
    };
    for (let i = 0; i < top.length; i++) {
      const w = top[i];
      if (clauseEnd.has(w.kw)) tables = false;
      const into = w.kw === "INTO" && (command === "INSERT" || command === "REPLACE");
      const start = w.kw === "FROM" && (command === "SELECT" || command === "DELETE" && using < 0 || command === "INSERT" || command === "REPLACE") ||
        w.kw === "UPDATE" && command === "UPDATE" || i === using || into;
      const next = tables && (w.text === "," || ["JOIN", "STRAIGHT_JOIN"].includes(w.kw));
      if (!start && !next) continue;
      tables = !into;
      let j = i + 1;
      if (w.kw === "UPDATE") while (["LOW_PRIORITY", "IGNORE"].includes(top[j]?.kw)) j++;
      if (top[j]?.kw === "LATERAL") j++;
      if (top[j]?.text === "(") {
        // A derived table (or an unsupported parenthesized join) has no
        // authoritative physical column list. Its alias shadows physical names.
        if (top[j + 1]?.text === ")") {
          j += 2;
          if (top[j]?.kw === "AS") j++;
          if (nameAt(top[j])) bind(top[j].name!, null);
        }
        continue;
      }
      const ref = tableName(top, j);
      if (!ref) continue;
      for (let k = j; k < ref.end; k++) tableWords.add(top[k]);
      j = ref.end;
      // Table-valued functions are not physical tables. INSERT's column list is.
      if (top[j]?.text === "(" && !into) continue;
      if (top[j]?.kw === "PARTITION" && top[j + 1]?.text === "(" && top[j + 2]?.text === ")") j += 3;
      const explicitAlias = top[j]?.kw === "AS";
      if (explicitAlias) j++;
      const alias = nameAt(top[j]) && (explicitAlias || !KEYWORDS.has(top[j].kw)) ? top[j].name! : ref.table;
      const schema = ref.schema || database;
      const local = !ref.schema && (locals.has(fold(ref.table)) || fold(ref.table) === "dual") || created.has(tableKey(schema, ref.table));
      bind(alias, local || !schema ? null : { schema, table: ref.table, alias });
      if (local || !schema) continue;
      const cached = catalog.peek("tables", { schema });
      if (!sessionTempTables && cached?.complete && !cached.stale && !cached.failed && !cached.truncated && cached.value &&
          !cached.value.prefix && !cached.value.after && !cached.value.truncated &&
          !cached.value.tables.some(t => fold(t.name) === fold(ref.table))) {
        out.push(warning(ref.token, `No table \`${ref.table}\` in \`${schema}\` (table list read at ${localTime(cached.readAt)}).`));
      }
    }
    for (let i = 0; i < scope.words.length - 2; i++) {
      const [qualifier, dot, column] = scope.words.slice(i, i + 3);
      if (!qualifier.name || dot.text !== "." || !column.name || tableWords.has(qualifier) ||
          scope.words[i - 1]?.text === "." || [".", "("].includes(scope.words[i + 3]?.text)) continue;
      const ref = scope.refs.get(fold(qualifier.name));
      if (!ref) continue;
      const cached = catalog.peek("columns", { schema: ref.schema, table: ref.table });
      if (cached?.complete && !cached.stale && !cached.failed && !cached.truncated && cached.value &&
          !cached.value.columns.some(c => fold(c.name) === fold(column.name!))) {
        out.push(warning(column, `No column \`${column.name}\` in \`${ref.schema}.${ref.table}\`.`));
      }
    }
  }
  return out;
}

/** No network-capable interface is available to the diagnostic source. */
export function problemDiagnostics(state: EditorState, env: EditorEnv, catalog: Pick<CatalogService, "peek">): Diagnostic[] {
  if (state.doc.length > HIGHLIGHT_LIMIT) return [];
  const sql = state.doc.toString();
  if (/^\s*DELIMITER\b/im.test(sql)) return [];
  const database = (env.connection as Connection | undefined)?.database || "";
  const out: Diagnostic[] = [], created = new Set<string>(), blocks: string[] = [];
  for (const statement of splitStatements(sql, env.backslashEscapes)) {
    const { words, valid, executable } = lex(statement.text, statement.start, env.backslashEscapes);
    // Executable comments can even change the schema. Without executing their
    // version gates, no later statement has a trustworthy default database.
    if (executable) break;
    const first = words[0]?.kw;
    const routine = first === "CREATE" && words.some(w => ["PROCEDURE", "FUNCTION", "TRIGGER", "EVENT"].includes(w.kw));
    const compound = first === "BEGIN" && words.length > 1 && words[1].kw !== "WORK";
    if (blocks.length || routine || compound) {
      for (let i = 0; i < words.length; i++) {
        const w = words[i];
        if (w.kw === "BEGIN" || w.kw === "CASE") blocks.push(w.kw);
        else if (w.kw === "END") {
          const suffix = words[i + 1]?.kw;
          if (!["IF", "LOOP", "WHILE", "REPEAT"].includes(suffix)) blocks.pop();
          if (["IF", "LOOP", "WHILE", "REPEAT", "CASE"].includes(suffix)) i++;
        }
      }
      continue;
    }
    if (first === "USE") break;
    if (!valid) continue;
    if (first === "CREATE") {
      const top = words.filter(w => w.depth === 0);
      let i = 1;
      if (top[i]?.kw === "OR" && top[i + 1]?.kw === "REPLACE") i += 2;
      if (top[i]?.kw === "TEMPORARY") i++;
      if (top[i++]?.kw === "TABLE") {
        if (top[i]?.kw === "IF" && top[i + 1]?.kw === "NOT" && top[i + 2]?.kw === "EXISTS") i += 3;
        const ref = tableName(top, i);
        if (ref) created.add(tableKey(ref.schema || database, ref.table));
      }
      continue;
    }
    const top = words.filter(w => w.depth === 0);
    const command = first === "WITH" ? top.find(w => ["SELECT", "UPDATE", "DELETE", "INSERT", "REPLACE"].includes(w.kw)) : top[0];
    if (command && ["UPDATE", "DELETE"].includes(command.kw) && !top.some(w => w.kw === "WHERE")) {
      out.push(warning(command, command.kw === "UPDATE"
        ? "UPDATE without WHERE: it changes every row of the table unless LIMIT or a join narrows it."
        : "DELETE without WHERE: it removes every row of the table unless LIMIT or a join narrows it."));
    }
    out.push(...metadataProblems(words, database, created, catalog, env.sessionTempTables));
  }
  return out.sort((a, b) => a.from - b.from || a.to - b.to);
}

const refreshProblems = StateEffect.define<void>();
export function problemsExtension(settings: EditorFeatureSettings, env: { current: EditorEnv }): Extension {
  if (!settings.problem_warnings) return [];
  const catalog = catalogFor(env);
  return [
    linter(view => problemDiagnostics(view.state, env.current, catalog), { delay: 750,
      needsRefresh: update => update.transactions.some(tr => tr.effects.some(e => e.is(refreshProblems))),
    }),
    lintGutter(),
    ViewPlugin.fromClass(class {
      private destroyed = false;
      private queued = false;
      private epoch = catalog.syncConnection();
      private environment = { ...env.current };
      private unsubscribe: () => void;
      constructor(private view: EditorView) {
        this.unsubscribe = catalog.subscribe(() => this.refresh());
      }
      private refresh() {
        if (this.queued || this.destroyed) return;
        this.queued = true;
        // Cache expiry can happen in peek or during a view update. Defer the
        // transaction so linting never dispatches inside CodeMirror's update.
        queueMicrotask(() => {
          this.queued = false;
          if (this.destroyed) return;
          // forceLinting only runs a *scheduled* lint in the installed CM.
          this.view.dispatch({ effects: refreshProblems.of() });
          forceLinting(this.view);
        });
      }
      update(_update: ViewUpdate) {
        const epoch = catalog.syncConnection(), current = env.current;
        if (epoch !== this.epoch || current.docId !== this.environment.docId ||
            current.backslashEscapes !== this.environment.backslashEscapes ||
            current.sessionTempTables !== this.environment.sessionTempTables ||
            current.engine !== this.environment.engine || current.serverVersion !== this.environment.serverVersion) this.refresh();
        this.epoch = epoch;
        this.environment = { ...current };
      }
      destroy() { this.destroyed = true; this.unsubscribe(); }
    }),
  ];
}
