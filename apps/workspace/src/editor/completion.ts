import { autocompletion, closeCompletion, completionStatus, pickedCompletion, type Completion, type CompletionContext as CMContext,
  type CompletionResult, type CompletionSource } from "@codemirror/autocomplete";
import { EditorSelection, StateEffect, StateField, type Extension } from "@codemirror/state";
import { EditorView, ViewPlugin, showPanel, type Panel } from "@codemirror/view";
import { paramNames, type Connection, type ParamNames } from "../api";
import { columnTargets, commonCompletionWords, completionContext, cteNames, variableContext,
  type CompletionContext, type Suggestion } from "../sqlCompletion";
import { argumentCount } from "./argumentHints";
import { catalogFor, type CatalogOwner } from "./catalog";
import type { EditorEnv, EditorFeatureSettings } from "./types";

type Environment = { current: EditorEnv };
export type SqlCompletionResult = CompletionResult & { note: string };
const setHint = StateEffect.define<string>();
/** Independent of the completion list: failures and loading remain visible even
 * when the source has no matching options. The panel never accepts HTML. */
export const completionHint = StateField.define<string>({
  create: () => "",
  update(value, tr) {
    if (tr.docChanged || tr.selection) value = "";
    for (const effect of tr.effects) if (effect.is(setHint)) value = effect.value;
    return value;
  },
  provide: () => showPanel.of(hintPanel),
});
function hintPanel(view: EditorView): Panel {
  const dom = document.createElement("div");
  dom.className = "sql-completion-hint";
  dom.setAttribute("role", "status");
  dom.textContent = view.state.field(completionHint);
  return { dom, update(update) { dom.textContent = update.state.field(completionHint); } };
}

// Only app-owned kind values reach CodeMirror's CSS type field (C3).
const types: Record<Candidate["kind"], string> = { function: "function", schema: "schema", table: "table", column: "column", alias: "alias", keyword: "keyword" };
type WordCatalog = typeof import("./engineWords");
// Keep the generated engine vocabulary out of QueryLab and the entry bundle.
let wordCatalog: Promise<WordCatalog> | undefined;
let loadedWords: WordCatalog | undefined;
const loadWords = () => wordCatalog ||= import("./engineWords").then(words => loadedWords = words);
type Candidate = Omit<Suggestion, "kind"> & { kind: Suggestion["kind"] | "function"; fuzzy: boolean };
type IdentifierContext = CompletionContext & { quoted?: boolean; nativePattern?: string };
type Metadata = {
  schemas?: string[]; tables?: { name: string; schema: string }[]; completeTables?: boolean;
  columns?: { name: string; type: string; table: ReturnType<typeof columnTargets>[number] }[];
};
function candidates(context: IdentifierContext, metadata: Metadata, ctes: string[], env: EditorEnv, words?: WordCatalog): Candidate[] {
  const out: Candidate[] = [];
  const quote = (name: string) => !context.quoted && /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(name) &&
    (!env.engine || !env.serverVersion || words && !words.isKeyword(env.engine, env.serverVersion, name))
    ? name : "`" + name.replace(/`/g, "``") + "`";
  const add = (label: string, kind: Candidate["kind"], detail: string, fuzzy: boolean, insert = quote(label)) =>
    out.push({ label, kind, detail, insert, fuzzy });
  for (const column of metadata.columns || []) add(column.name, "column", `${column.table.schema}.${column.table.table} · ${column.type}`, true,
    context.qualifiers.length || context.tables.length < 2 ? quote(column.name) : `${quote(column.table.alias)}.${quote(column.name)}`);
  if (!context.qualifiers.length && context.place === "expression") for (const table of context.tables)
    add(table.alias, "alias", `${table.schema ? table.schema + "." : ""}${table.table}`, true);
  if (!context.qualifiers.length && context.place === "table") for (const name of ctes) add(name, "table", "CTE", true);
  for (const table of metadata.tables || []) add(table.name, "table", table.schema, !!metadata.completeTables);
  if (!context.qualifiers.length) for (const schema of metadata.schemas || []) add(schema, "schema", "Database", false);
  if (!context.qualifiers.length && context.place === "expression") {
    const list = env.engine && env.serverVersion ? words?.wordsFor(env.engine, env.serverVersion) : undefined;
    if (list?.release) for (const keyword of list.keywords) add(keyword.name, "keyword",
      [keyword.reserved ? "reserved" : "", list.exact ? "" : `${env.engine === "mysql" ? "MySQL" : "MariaDB"} ${list.release} list`]
        .filter(Boolean).join(", "), true, context.quoted ? quote(keyword.name) : keyword.name);
    else for (const name of commonCompletionWords) add(name, "keyword", "", true, context.quoted ? quote(name) : name);
    if (!context.quoted && env.engine && env.serverVersion) {
      const functions = words?.functionsFor(env.engine, env.serverVersion);
      if (functions?.release) for (const fn of functions.functions) add(fn.name, "function",
        [argumentCount(fn), functions.exact ? "" : `${env.engine === "mysql" ? "MySQL" : "MariaDB"} ${functions.release} list`]
          .filter(Boolean).join(", "), true, fn.name);
    }
  }
  return out;
}
/** A necessary subsequence check only bounds the candidate pool. CodeMirror's
 * matcher (filter:true) makes the final fuzzy decision, including its short-word
 * rules. Incomplete inventories never reach that matcher without prefix checks. */
function couldMatch(label: string, prefix: string): boolean {
  let at = 0;
  const chars = [...prefix];
  for (const char of label.toLowerCase()) if (char === chars[at] && ++at === chars.length) return true;
  return !prefix.length;
}
function resultFor(context: { start: number; end: number; prefix: string; quoted?: boolean; nativePattern?: string }, items: Candidate[], note = ""): SqlCompletionResult {
  const prefix = context.prefix.toLowerCase(), seen = new Set<string>();
  const exact: Candidate[] = [], fuzzy: Candidate[] = [];
  for (const item of items) {
    const starts = item.label.toLowerCase().startsWith(prefix);
    // Escaped/closed backticks differ from the raw label. Keep their legacy
    // prefix result rather than asking CM to match SQL quoting against a name.
    if (!starts && (!item.fuzzy || (context.nativePattern !== undefined && context.nativePattern !== context.prefix) ||
      !couldMatch(item.label, prefix))) continue;
    const key = `${item.kind}:${item.insert}`;
    if (seen.has(key)) continue;
    seen.add(key);
    (starts ? exact : fuzzy).push(item);
  }
  const selected = [...exact, ...fuzzy].slice(0, 40);
  const nativeFilter = selected.some(item => !item.label.toLowerCase().startsWith(prefix));
  return { from: context.start, to: context.end, filter: nativeFilter, note,
    // No validFor: every edit reruns the lexical guards and inventory checks.
    // Prefix-only results retain the legacy order and cap with filter:false.
    options: selected.map((item, index) => ({ label: item.label, detail: item.detail, type: types[item.kind], apply: item.kind === "function" ? (view: EditorView, completion: Completion, from: number, to: number) => {
      const state = view.state, main = state.selection.main;
      const fromOff = from - main.from, toOff = to - main.from;
      const allMatch = state.selection.ranges.every(range => range.empty &&
        state.sliceDoc(range.from + fromOff, range.from + toOff) === state.sliceDoc(from, to));
      // Each cursor may already precede an opening parenthesis. changeByRange
      // maps the independently chosen insertion/caret through every other edit.
      const changes = state.changeByRange(range => {
        if (!allMatch && range !== main) return { range };
        const start = range.from + fromOff, end = to === main.from ? range.to : range.from + toOff;
        const parentheses = state.sliceDoc(end, end + 1) !== "(";
        const insert = item.insert + (parentheses ? "()" : "");
        return { changes: { from: start, to: end, insert },
          range: EditorSelection.cursor(start + insert.length - (parentheses ? 1 : 0)) };
      });
      view.dispatch({ ...changes, scrollIntoView: true,
        annotations: pickedCompletion.of(completion), userEvent: "input.complete" });
    } : context.quoted ? item.insert.slice(1) : item.insert,
      ...(nativeFilter ? { boost: (item.label.toLowerCase().startsWith(prefix) ? 1e6 : 0) - index * 10000 } : {}) })) };
}
type Job = { owner: CatalogOwner; key: string; cancel: () => void; valid: () => boolean };
class CompletionSession {
  readonly catalog;
  readonly editor = {};
  generation = 0;
  auto = true;
  typed = false;
  composing = false;
  job?: Job;
  view?: EditorView;
  hint = "";
  readonly names = new Map<string, ParamNames>();
  source: CompletionSource;
  localSource: CompletionSource;
  local?: { key: string; result: SqlCompletionResult };
  private namesReads = new Map<string, Promise<ParamNames>>();
  constructor(readonly env: Environment) {
    this.catalog = catalogFor(env);
    this.source = context => this.complete(context);
    this.localSource = context => this.complete(context, true);
  }
  publish(text: string): void {
    this.hint = text;
    const view = this.view;
    // Cancellation also runs from ViewPlugin.update. Never dispatch reentrantly.
    queueMicrotask(() => {
      if (view && this.view === view && view.state.field(completionHint, false) !== undefined && this.hint === text)
        view.dispatch({ effects: setHint.of(text) });
    });
  }
  cancel(): void {
    this.generation++;
    this.job?.cancel();
    this.job = undefined;
    this.publish("");
  }
  async preload(signal?: AbortSignal): Promise<void> {
    const { engine, serverVersion } = this.env.current;
    const words = loadWords();
    if (!engine || !serverVersion) { await words; return; }
    await Promise.all([words, this.variableNames(engine, serverVersion, signal).catch(() => {})]);
  }
  private variableNames(engine: NonNullable<EditorEnv["engine"]>, version: string, signal?: AbortSignal): Promise<ParamNames> {
    const key = JSON.stringify([engine, version]), cached = this.names.get(key);
    if (cached) return Promise.resolve(cached);
    let pending = this.namesReads.get(key);
    if (!pending) {
      let onAbort: () => void = () => {};
      const stopped = new Promise<never>((_, reject) => {
        onAbort = () => reject(new DOMException("Variable names read cancelled", "AbortError"));
        if (signal?.aborted) onAbort();
        else signal?.addEventListener("abort", onAbort, { once: true });
      });
      pending = Promise.race([paramNames(engine, version, signal), stopped]).then(data => {
        if (signal?.aborted) throw new DOMException("Variable names read cancelled", "AbortError");
        this.names.set(key, data);
        if (this.names.size > 8) this.names.delete(this.names.keys().next().value!);
        return data;
      }).finally(() => {
        signal?.removeEventListener("abort", onAbort);
        if (this.namesReads.get(key) === pending) this.namesReads.delete(key);
      });
      this.namesReads.set(key, pending);
    }
    return pending;
  }
  /** Read the exact same keys as the remote lane, without starting any IO. */
  private cached(context: IdentifierContext, connection: Connection, explicit: boolean): { metadata: Metadata; note: string; missing: boolean } {
    const targets = columnTargets(context, connection.database || "");
    const metadata: Metadata = { columns: [], tables: [], schemas: [] };
    let missing = false, failed = false, limited = false;
    const inspect = <T,>(data: import("./catalog").CatalogResult<T> | null) => {
      missing ||= !data || data.stale || (explicit && data.failed);
      failed ||= !!data?.failed;
      limited ||= !!data?.truncated;
      return data?.value;
    };
    const schemas = context.qualifiers.length < 2 && !targets.length
      ? inspect(this.catalog.peek("schemas", {}))?.schemas || [] : [];
    metadata.schemas = schemas.map(schema => schema.name);
    const tableSchema = context.place === "table" ? context.qualifiers[0] || connection.database || ""
      : context.qualifiers.length === 1 && !targets.length && metadata.schemas.includes(context.qualifiers[0]) ? context.qualifiers[0] : "";
    if (tableSchema) {
      const full = this.catalog.peek("tables", { schema: tableSchema });
      const count = schemas.find(schema => schema.name === tableSchema)?.tables;
      const exact = this.catalog.peek("tables", { schema: tableSchema,
        ...(Number.isInteger(count) && count! >= 0 && count! <= 200 ? {} : { prefix: context.prefix }) });
      const data = full?.complete && !full.stale ? full : exact?.value ? exact : full?.complete ? full : exact;
      const value = inspect(data);
      metadata.tables = value?.tables.map(table => ({ name: table.name, schema: tableSchema })) || [];
      metadata.completeTables = !!data?.complete;
      if (!data?.complete) for (const cached of this.catalog.peekTableNames(tableSchema)) {
        metadata.tables.push(...cached.value!.tables.map(table => ({ name: table.name, schema: tableSchema })));
      }
    }
    for (const table of targets) {
      const value = inspect(this.catalog.peek("columns", { schema: table.schema, table: table.table }));
      metadata.columns!.push(...(value?.columns.map(column => ({ name: column.name, type: column.type, table })) || []));
    }
    return { metadata, missing, note: failed ? "Some catalog suggestions are unavailable."
      : limited ? "More tables match. Keep typing to narrow the list." : "" };
  }
  async complete(cm: CMContext, localOnly = false): Promise<SqlCompletionResult | null> {
    const epoch = this.catalog.syncConnection(), env = { ...this.env.current };
    const sql = cm.state.doc.toString(), selection = cm.state.selection;
    const parsedVariable = selection.ranges.some(range => !range.empty) ? null : variableContext(sql, cm.pos, env.backslashEscapes);
    const variable = parsedVariable ? { ...parsedVariable, nativePattern: sql.slice(parsedVariable.start, parsedVariable.end) } : null;
    const parsedContext = selection.ranges.some(range => !range.empty) ? null
      : completionContext(sql, cm.pos, cm.pos, env.backslashEscapes);
    // Match inside the opening quote, then keep that quote while replacing the
    // rest with a fully quoted string (including the closing quote). This also
    // handles alias.column insertions without using a completion apply callback.
    const context: IdentifierContext | null = parsedContext && sql[parsedContext.start] === "`"
      ? { ...parsedContext, start: parsedContext.start + 1, quoted: true, nativePattern: sql.slice(parsedContext.start + 1, parsedContext.end) }
      : parsedContext ? { ...parsedContext, nativePattern: sql.slice(parsedContext.start, parsedContext.end) } : null;
    const place = variable || context;
    const engine = env.engine, version = env.serverVersion;
    const key = JSON.stringify([epoch, env.docId, env.backslashEscapes, engine, version, sql, cm.pos]);
    // CM retains a result's explicit bit across typing (autocomplete:1023).
    // Only the shortcut starts a new manual read.
    const explicit = cm.explicit && (!cm.view || !this.typed);
    if (this.composing || cm.view?.composing || cm.aborted || !place ||
      (!explicit && (!this.auto || (!variable && context && context.prefix.length < 1 && !context.qualifiers.length)))) {
      if (!localOnly) this.cancel();
      return null;
    }
    const words = loadedWords;
    const connection = env.connection as Connection | undefined;
    const ctes = context ? cteNames(sql, cm.pos, env.backslashEscapes) : [];
    const cached = context && connection ? this.cached(context, connection, explicit) : undefined;
    const names = engine && version ? this.names.get(JSON.stringify([engine, version])) : undefined;
    const variableResult = (data?: ParamNames) => resultFor(variable!, (data?.names || []).filter(item => !variable!.scope ||
      item.scope === "both" || item.scope === variable!.scope).map(item => ({
        label: item.name, insert: item.name, kind: "column" as const, fuzzy: true,
        detail: (item.scope === "both" ? "global, session" : item.scope) + (item.deprecated ? ", deprecated" : ""),
      })));
    // The same local option objects survive the remote arrival. CodeMirror's
    // multi-source merge preserves the highlighted option by object identity.
    const localKey = JSON.stringify([key, !!words, cached?.metadata, cached?.note, names]);
    if (this.local?.key !== localKey) this.local = { key: localKey, result: variable ? variableResult(names)
      : resultFor(context!, candidates(context!, cached?.metadata || {}, ctes, env, words), cached?.note) };
    // Reserve space for names still being read. Both native sources remain
    // within the shared 40-option bound, even for an empty explicit prefix.
    if (cached?.missing && this.local.result.options.length > 20)
      this.local.result = { ...this.local.result, options: this.local.result.options.slice(0, 20) };
    const local = this.local.result;
    if (localOnly) return local;
    this.cancel();
    const generation = this.generation;
    const owner = { editor: this.editor, generation };
    if ((variable && (!engine || !version || names)) || (!variable && (!connection || !cached?.missing))) {
      this.publish(local.note);
      return cm.view ? null : local;
    }
    let cancelled = false;
    let delay: ReturnType<typeof setTimeout> | undefined, budget: ReturnType<typeof setTimeout> | undefined;
    const namesController = new AbortController();
    let stop: () => void = () => {};
    const stopped = new Promise<null>(resolve => { stop = () => resolve(null); });
    const valid = () => !cancelled && !this.composing && !cm.aborted && this.catalog.syncConnection() === epoch &&
      this.env.current.docId === env.docId && this.env.current.backslashEscapes === env.backslashEscapes &&
      this.env.current.engine === engine && this.env.current.serverVersion === version;
    const job: Job = { owner, key, valid, cancel: () => {
      cancelled = true;
      clearTimeout(delay); clearTimeout(budget);
      this.catalog.cancelOwner(owner);
      namesController.abort();
      stop();
    } };
    this.job = job;
    cm.addEventListener("abort", () => { if (this.job === job) this.cancel(); }, { onDocChange: true });
    this.publish(variable ? "Reading system variables…" : "Reading catalog…");
    const timeout = new Promise<SqlCompletionResult>(resolve => {
      budget = setTimeout(() => {
        this.catalog.cancelOwner(owner);
        namesController.abort();
        resolve({ ...local, note: variable ? "System variable suggestions timed out. Try Ctrl+Space again." : "Catalog suggestions timed out. Try Ctrl+Space again." });
      }, 8000);
    });
    const read = async (): Promise<SqlCompletionResult | null> => {
      if (!explicit) await Promise.race([new Promise<void>(resolve => { delay = setTimeout(resolve, 220); }), stopped]);
      if (!valid()) return null;
      if (variable) {
        try {
          const data = await this.variableNames(engine!, version!, namesController.signal);
          if (!valid()) return null;
          return variableResult(data);
        } catch {
          return valid() ? { ...local, note: "System variable suggestions are unavailable." } : null;
        }
      }
      if (!context || !connection) return null;
      let unavailable = false, limited = false, completeTables = false;
      const targets = columnTargets(context, connection.database || "");
      const columns: { name: string; type: string; table: typeof targets[number] }[] = [];
      const tables: { name: string; schema: string }[] = [];
      let schemas: string[] = [];
      const tableCounts = new Map<string, number>();
      if (context.qualifiers.length < 2 && !targets.length) {
        const data = await this.catalog.read("schemas", {}, owner, explicit, true);
        unavailable = data.failed || data.stale;
        schemas = data.value?.schemas.map(schema => schema.name) || [];
        for (const schema of data.value?.schemas || []) if (Number.isInteger(schema.tables) && schema.tables >= 0)
          tableCounts.set(schema.name, schema.tables);
      }
      if (!valid()) return null;
      let tableSchema = "";
      if (context.place === "table") tableSchema = context.qualifiers[0] || connection.database || "";
      else if (context.place === "expression" && context.qualifiers.length === 1 && !targets.length && schemas.includes(context.qualifiers[0]))
        tableSchema = context.qualifiers[0];
      const full = tableSchema ? this.catalog.peek("tables", { schema: tableSchema }) : null;
      const wholeTables = full?.complete && !full.stale || tableCounts.has(tableSchema) && tableCounts.get(tableSchema)! <= 200;
      await Promise.all([
        ...(tableSchema ? [this.catalog.read("tables", { schema: tableSchema, ...(wholeTables ? {} : { prefix: context.prefix }) }, owner, explicit, true).then(data => {
          unavailable ||= data.failed || data.stale;
          limited = data.truncated;
          completeTables = data.complete;
          if (data.value) tables.push(...data.value.tables.map(table => ({ name: table.name, schema: tableSchema })));
        })] : []),
        ...targets.map(table => this.catalog.read("columns", { schema: table.schema, table: table.table }, owner, explicit, true).then(data => {
          unavailable ||= data.failed || data.stale;
          if (data.value) columns.push(...data.value.columns.map(column => ({ name: column.name, type: column.type, table })));
        })),
      ]);
      columns.sort((a, b) => targets.indexOf(a.table) - targets.indexOf(b.table));
      return resultFor(context, candidates(context, { schemas, tables, columns, completeTables }, ctes, env, words),
        unavailable ? "Some catalog suggestions are unavailable." : limited ? "More tables match. Keep typing to narrow the list." : "");
    };
    const finished = Promise.race([read().catch(() => null), timeout, stopped]).then(result => {
      clearTimeout(delay); clearTimeout(budget);
      if (!valid() || this.job !== job || !result) return null;
      this.publish(result.note);
      return result;
    });
    if (!cm.view) return finished;
    return finished.then(result => {
      if (!result) return null;
      const keyFor = (option: Completion) => JSON.stringify([option.type, option.label, option.detail,
        typeof option.apply === "string" ? option.apply : null]);
      const present = new Set(local.options.map(keyFor));
      // Keep the combined list bounded. The local lane retains its option
      // objects and the remote lane contributes only newly discovered names.
      return { ...result, options: result.options.filter(option => !present.has(keyFor(option))).slice(0, 40 - local.options.length) };
    });
  }
}
const sessions = new WeakMap<Environment, CompletionSession>();
function sessionFor(env: Environment): CompletionSession {
  let session = sessions.get(env);
  if (!session) { session = new CompletionSession(env); sessions.set(env, session); }
  return session;
}
export function completionSources(env: Environment): CompletionSource[] {
  const session = sessionFor(env);
  // Metadata keeps its priority when it arrives. CM accepts the local source
  // independently while this first source is pending (updateSyncTime: 0).
  return [session.source, session.localSource];
}

/** Also exported for deterministic preload tests without a DOM. */
export function preloadCompletion(env: Environment, signal?: AbortSignal): Promise<void> { return sessionFor(env).preload(signal); }

const extensions = new WeakMap<Environment, Extension>();
export function completionExtension(settings: EditorFeatureSettings, env: Environment): Extension {
  const session = sessionFor(env);
  session.auto = settings.completion_auto;
  const existing = extensions.get(env);
  if (existing) return existing;
  const lifecycle = ViewPlugin.fromClass(class {
    epoch = session.catalog.syncConnection();
    docId = env.current.docId;
    backslashEscapes = env.current.backslashEscapes;
    engine = env.current.engine;
    version = env.current.serverVersion;
    auto = session.auto;
    private warmController?: AbortController;
    private idle?: number;
    private idleFallback?: ReturnType<typeof setTimeout>;
    private warmBudget?: ReturnType<typeof setTimeout>;
    private warm() {
      this.warmController?.abort();
      clearTimeout(this.idleFallback); clearTimeout(this.warmBudget);
      if (this.idle !== undefined && typeof cancelIdleCallback !== "undefined") cancelIdleCallback(this.idle);
      const controller = this.warmController = new AbortController();
      const run = () => {
        if (controller.signal.aborted) return;
        const budget = this.warmBudget = setTimeout(() => controller.abort(), 8000);
        void session.preload(controller.signal).catch(() => {}).finally(() => {
          clearTimeout(budget);
          if (this.warmBudget === budget) this.warmBudget = undefined;
        });
      };
      if (typeof requestIdleCallback !== "undefined") this.idle = requestIdleCallback(run, { timeout: 500 });
      else this.idleFallback = setTimeout(run, 0);
    }
    constructor(readonly view: EditorView) {
      session.view = view;
      session.typed = false;
      session.cancel();
      this.warm();
      // A restored EditorState may carry a completion from its former tab or
      // connection. Do not revive that list or its in-flight request.
      queueMicrotask(() => { if (session.view === view) closeCompletion(view); });
    }
    update(update: import("@codemirror/view").ViewUpdate) {
      const epoch = session.catalog.syncConnection();
      const environmentChanged = epoch !== this.epoch || env.current.docId !== this.docId || env.current.backslashEscapes !== this.backslashEscapes ||
        env.current.engine !== this.engine || env.current.serverVersion !== this.version;
      if (update.docChanged) session.typed = true;
      if (update.docChanged || update.selectionSet || environmentChanged || this.auto !== session.auto ||
        (completionStatus(update.startState) === "active" && completionStatus(update.state) === null)) session.cancel();
      if (env.current.engine !== this.engine || env.current.serverVersion !== this.version) this.warm();
      if (environmentChanged || this.auto !== session.auto) {
        queueMicrotask(() => { if (session.view === this.view) closeCompletion(this.view); });
      }
      this.epoch = epoch; this.docId = env.current.docId; this.backslashEscapes = env.current.backslashEscapes;
      this.engine = env.current.engine; this.version = env.current.serverVersion;
      this.auto = session.auto;
    }
    destroy() {
      this.warmController?.abort();
      clearTimeout(this.idleFallback); clearTimeout(this.warmBudget);
      if (this.idle !== undefined && typeof cancelIdleCallback !== "undefined") cancelIdleCallback(this.idle);
      if (session.view === this.view) { session.cancel(); session.view = undefined; }
    }
  });
  const textOption = (className: string, text: string) => {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    return span;
  };
  const extension = [completionHint, lifecycle,
    EditorView.contentAttributes.of(view => ({
      // CM supplies aria-controls exactly while its list is open, including
      // disabled lists retained during a read (currentCompletions omits those).
      // The view calls this function on every update, even without doc changes.
      "aria-expanded": view.state.facet(EditorView.contentAttributes).some(value =>
        typeof value !== "function" && !!value["aria-controls"]) ? "true" : "false",
    })), autocompletion({
    // The source owns the existing 220 ms catalog debounce. Avoid stacking the
    // default 100 ms delay on top. Explicit start still has CM's fixed 50 ms.
    activateOnTypingDelay: 0, updateSyncTime: 0, icons: false,
    tooltipClass: () => "sql-completion", optionClass: () => "sql-completion-option",
    addToOptions: [
      { position: 40, render: (completion: Completion) => textOption("sql-completion-label", completion.label) },
      { position: 60, render: (completion: Completion) => textOption("sql-completion-kind", completion.type || "") },
    ],
  }), EditorView.domEventHandlers({
    compositionstart(_event, view) { session.composing = true; session.cancel(); closeCompletion(view); return false; },
    compositionend() { session.composing = false; return false; },
    blur() { session.cancel(); return false; },
  }), EditorView.domEventObservers({
    // Observers run even when the shell or completion keymap handles the key.
    keydown(event) {
      if (event.key === "Escape") session.cancel();
      if (event.ctrlKey && !event.metaKey && !event.altKey && (event.code === "Space" || event.key === " ")) {
        // Manual retries supersede the outstanding automatic read.
        session.cancel();
        session.typed = false;
      }
    },
  })];
  extensions.set(env, extension);
  return extension;
}
