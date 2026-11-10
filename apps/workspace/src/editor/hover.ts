import type { Extension } from "@codemirror/state";
import { closeHoverTooltip, closeHoverTooltips, hoverTooltip, repositionTooltips,
  type EditorView, type HoverTooltipSource, type Tooltip } from "@codemirror/view";
import type { CatalogColumn, CatalogTable, CatalogTables, Connection } from "../api";
import { completionContext, cteNames, type TableRef } from "../sqlCompletion";
import { HIGHLIGHT_LIMIT, tokenize } from "../sqlHighlight";
import { splitStatements } from "../sqlSelection";
import { catalogFor, type CatalogOwner, type CatalogResult, type CatalogService } from "./catalog";
import type { EditorEnv, EditorFeatureSettings } from "./types";

type Environment = { current: EditorEnv };
type Target = { from: number; to: number; tables: TableRef[]; column?: string };
const word = /[A-Za-z0-9_$\u0080-\uffff]/;
const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

/** Completion owns the conservative statement/SELECT-scope and alias rules.
 * Hover adds exact token boundaries: never resolve a prefix, literal or comment. */
function targetAt(sql: string, pos: number, side: number, env: EditorEnv): Target | null {
  if (sql.length > HIGHLIGHT_LIMIT || pos < 0 || pos > sql.length) return null;
  const at = side < 0 ? pos - 1 : pos;
  const statement = splitStatements(sql, env.backslashEscapes).find(s => s.start <= at && at < s.end);
  if (!statement) return null;
  const text = statement.text, offset = at - statement.start;
  const scan = env.backslashEscapes ? text : text.replace(/\\/g, " ");
  const special = tokenize(scan).find(t => t.start <= offset && offset < t.end);
  let from = offset, to = offset + 1;
  if (special) {
    if (special.kind !== "ident" || !special.closed) return null;
    from = special.start; to = special.end;
  } else {
    if (!word.test(text[offset] || "")) return null;
    while (from > 0 && word.test(text[from - 1])) from--;
    while (to < text.length && word.test(text[to])) to++;
  }
  const context = completionContext(text, to, to, env.backslashEscapes);
  if (!context || context.start !== from || context.end !== to || !context.prefix) return null;
  const database = (env.connection as Connection | undefined)?.database || "";
  let references = context.tables;
  // Completion skips table-valued functions, but INSERT/REPLACE's destination
  // column list also follows a table with "(". Resolve its head separately,
  // keeping aliases from a later INSERT ... SELECT out of the destination list.
  if (/^\s*(?:INSERT|REPLACE)\b/i.test(text)) {
    const brackets = tokenize(scan).filter(t => t.kind === "bracket");
    const open = brackets.find(t => text[t.start] === "(");
    if (open) {
      const head = text.slice(0, open.start).trimEnd();
      const destination = completionContext(head, head.length, head.length, env.backslashEscapes);
      const close = brackets.find(t => t.start > open.start && text[t.start] === ")");
      if (destination?.place === "table" && destination.tables.length === 1 &&
        (to <= open.start || (from > open.start && close && to <= close.start))) references = destination.tables;
    }
  }
  const ctes = cteNames(text, offset, env.backslashEscapes);
  const refs = references.filter(t => t.schema || !ctes.some(name => same(name, t.table)))
    .map(t => ({ ...t, schema: t.schema || database })).filter(t => t.schema);
  const name = special ? text.slice(from + 1, to - 1).replace(/``/g, "`") : text.slice(from, to);
  const range = { from: from + statement.start, to: to + statement.start };
  const q = context.qualifiers;
  if (context.place === "table") {
    const tables = refs.filter(t => same(t.table, name) && (!q.length || same(t.schema, q[0])));
    return tables.length === 1 ? { ...range, tables } : null;
  }
  // Function argument hints are a separate feature.
  if (/^\s*\(/.test(text.slice(to))) return null;
  if (!q.length) {
    const tables = refs.filter(t => same(t.alias, name) || same(t.table, name));
    if (tables.length === 1) return { ...range, tables };
    // Completion caps the list at four tables, so another candidate may exist.
    if (context.tables.length === 4) return null;
    return refs.length ? { ...range, tables: refs, column: name } : null;
  }
  const tables = refs.filter(t => q.length === 1 ? same(t.alias, q[0])
    : q.length === 2 && same(t.schema, q[0]) && same(t.table, q[1]));
  return tables.length === 1 ? { ...range, tables, column: name } : null;
}

// The cache-only accessor is supplied by the catalog lane. Keep this optional
// while older catalog services are in use: a hover must never fetch table lists.
// CatalogTables currently omits the optional statistics fields from its type.
type TablesWithEstimates = Omit<CatalogTables, "tables"> & { tables: (CatalogTables["tables"][number] & { rows?: number | null })[] };
type PeekCatalog = CatalogService & {
  peek?: (kind: "tables", params: { schema: string }) => CatalogResult<TablesWithEstimates> | undefined;
};
function cachedRows(catalog: CatalogService, schema: string, table: string): number | undefined {
  const cached = (catalog as PeekCatalog).peek?.("tables", { schema });
  const rows = cached && !cached.failed ? cached.value?.tables.find(t => same(t.name, table))?.rows : undefined;
  return typeof rows === "number" && Number.isFinite(rows) && rows >= 0 ? rows : undefined;
}

/** All server-controlled strings use textContent. Tags and classes are fixed
 * app strings; even ENUM types and hostile identifiers never become markup. */
function node(tag: "div" | "bdi", className: string, text: string): HTMLElement {
  const dom = document.createElement(tag);
  dom.className = className;
  dom.textContent = text;
  return dom;
}
function flags(column: CatalogColumn): string {
  const key = ["PRI", "UNI", "MUL"].includes(column.key) ? column.key : "";
  return [key, column.nullable ? "NULL" : "NOT NULL"].filter(Boolean).join("  ");
}
function render(dom: HTMLElement, table: CatalogTable, column: CatalogColumn | undefined, rows: number | undefined): void {
  dom.textContent = "";
  dom.appendChild(node("bdi", "sql-hover-heading", `${table.schema}.${table.table}${column ? "." + column.name : ""}`));
  if (column) {
    dom.appendChild(node("bdi", "sql-hover-type", column.type));
    dom.appendChild(node("div", "sql-hover-note", flags(column)));
    return;
  }
  if (rows !== undefined) dom.appendChild(node("div", "sql-hover-note", `Estimated rows: ${rows}`));
  for (const col of table.columns.slice(0, 12)) {
    const row = node("div", "sql-hover-row", "");
    row.appendChild(node("bdi", "sql-hover-name", col.name));
    row.appendChild(node("bdi", "sql-hover-type", col.type));
    row.appendChild(node("div", "sql-hover-note", flags(col)));
    dom.appendChild(row);
  }
  if (table.columns.length > 12) dom.appendChild(node("div", "sql-hover-note", `and ${table.columns.length - 12} more`));
}

type Job = { owner: CatalogOwner; cancel(): void };
/** Export the source for renderless tests with real EditorState and catalog IO.
 * Each view has an independent owner, even if it shares an environment/cache. */
export function hoverSource(env: Environment,
  close: (view: EditorView) => void = view => { view.dispatch({ effects: closeHoverTooltips }); }): HoverTooltipSource {
  const jobs = new WeakMap<EditorView, Job>();
  const editors = new WeakMap<EditorView, object>();
  let generation = 0;
  return (view, pos, side): Tooltip | null => {
    jobs.get(view)?.cancel();
    const snapshot = env.current, doc = view.state.doc;
    const target = snapshot.connection ? targetAt(doc.toString(), pos, side, snapshot) : null;
    if (!target) return null;
    const catalog = catalogFor(env), epoch = catalog.syncConnection();
    const results = target.tables.map(t => catalog.peek("columns", { schema: t.schema, table: t.table }));
    if (target.tables.length > 4 && results.some(r => !r)) return null;
    let editor = editors.get(view);
    if (!editor) { editor = {}; editors.set(view, editor); }
    const owner: CatalogOwner = { editor, generation: ++generation };
    let cancelled = false, dom: HTMLElement | undefined;
    let answer: { table: CatalogTable; column?: CatalogColumn } | null | undefined;
    const valid = () => !cancelled && jobs.get(view) === job && view.state.doc === doc &&
      env.current.docId === snapshot.docId && env.current.backslashEscapes === snapshot.backslashEscapes &&
      catalog.syncConnection() === epoch;
    const job: Job = { owner, cancel() {
      cancelled = true;
      catalog.cancelOwner(owner);
      if (dom) dom.textContent = "";
      if (jobs.get(view) === job) jobs.delete(view);
    } };
    jobs.set(view, job);
    const dismiss = () => {
      cancelled = true;
      catalog.cancelOwner(owner);
      if (dom) dom.textContent = "";
      // Tooltip.update runs during a view update: defer the close transaction.
      queueMicrotask(() => {
        if (jobs.get(view) === job) { job.cancel(); close(view); }
      });
    };
    const show = () => {
      if (!valid()) { dismiss(); return; }
      if (!answer) { if (answer === null) dismiss(); return; }
      if (dom) {
        render(dom, answer.table, answer.column, cachedRows(catalog, answer.table.schema, answer.table.table));
        repositionTooltips(view);
      }
    };
    void (async () => {
      for (let i = 0; i < results.length; i++) {
        if (results[i]) continue;
        const t = target.tables[i];
        results[i] = await catalog.read("columns", { schema: t.schema, table: t.table }, owner);
        if (!valid()) { dismiss(); return; }
      }
    })().then(() => {
      if (!valid()) { dismiss(); return; }
      // Ambiguous unqualified columns and partial failures are not evidence.
      if (results.some(r => !r || r.failed || !r.value)) { answer = null; show(); return; }
      const matches = results.flatMap(r => {
        const table = r!.value!;
        if (target.column === undefined) return [{ table }];
        const column = table.columns.find(c => same(c.name, target.column!));
        return column ? [{ table, column }] : [];
      });
      answer = matches.length === 1 ? matches[0] : null;
      show();
    }).catch(() => { answer = null; dismiss(); });
    return { pos: target.from, end: target.to, above: true, create() {
      dom = node("div", "sql-hover", "Reading…");
      show();
      return { dom, update() { if (!valid()) dismiss(); }, destroy() { job.cancel(); } };
    } };
  };
}

export function hoverExtension(_settings: EditorFeatureSettings, env: Environment): Extension {
  const source = hoverSource(env, view => { view.dispatch({ effects: closeHoverTooltip(extension) }); });
  const extension = hoverTooltip(source, { hoverTime: 350, hideOnChange: true });
  return extension;
}
