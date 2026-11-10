import { autocompletion, closeCompletion, completionStatus, startCompletion, type Completion, type CompletionContext as CMContext,
  type CompletionResult, type CompletionSource } from "@codemirror/autocomplete";
import { StateEffect, StateField, type Extension } from "@codemirror/state";
import { EditorView, ViewPlugin, showPanel, type Panel } from "@codemirror/view";
import type { Connection } from "../api";
import { columnTargets, completionContext, suggestions, type Suggestion } from "../sqlCompletion";
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
const types: Record<Suggestion["kind"], string> = { schema: "schema", table: "table", column: "column", alias: "alias", keyword: "keyword" };
function resultFor(context: NonNullable<ReturnType<typeof completionContext>>, items: Suggestion[], note = ""): SqlCompletionResult {
  return { from: context.start, to: context.end, filter: false, note,
    // No validFor: every edit must rerun the lexical guard and prefix request.
    // filter:false preserves suggestions()'s ordering and its exact 40-item cap.
    options: items.map(item => ({ label: item.label, detail: item.detail, type: types[item.kind], apply: item.insert })) };
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
  ready?: { key: string; result: SqlCompletionResult };
  view?: EditorView;
  hint = "";
  source: CompletionSource;
  constructor(readonly env: Environment) {
    this.catalog = catalogFor(env);
    this.source = context => this.complete(context);
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
    this.job?.cancel();
    this.job = undefined;
    this.ready = undefined;
    this.publish("");
  }
  async complete(cm: CMContext): Promise<SqlCompletionResult | null> {
    const epoch = this.catalog.syncConnection(), env = this.env.current;
    const sql = cm.state.doc.toString(), selection = cm.state.selection;
    const context = selection.ranges.some(range => !range.empty) ? null
      : completionContext(sql, cm.pos, cm.pos, env.backslashEscapes);
    const key = JSON.stringify([epoch, env.docId, env.backslashEscapes, sql, cm.pos]);
    // CM retains a result's explicit bit across typing (autocomplete:1023),
    // including our public refresh. Only the shortcut starts a new manual read.
    const explicit = cm.explicit && (!cm.view || !this.typed);
    if (this.composing || cm.view?.composing || cm.aborted || !context ||
      (!explicit && (!this.auto || (context.prefix.length < 2 && !context.qualifiers.length)))) {
      this.cancel();
      return null;
    }
    // A live source first returns local words, then refreshes once the catalog
    // arrives. Consume that refresh without interpreting it as an explicit retry.
    if (this.ready?.key === key && this.job?.valid()) {
      const ready = this.ready.result;
      this.ready = undefined;
      return ready;
    }
    this.cancel();
    const owner = { editor: this.editor, generation: ++this.generation };
    const connection = env.connection as Connection | undefined;
    const local = resultFor(context, suggestions(context, {}));
    if (!connection) return local;
    let cancelled = false;
    let delay: ReturnType<typeof setTimeout> | undefined, budget: ReturnType<typeof setTimeout> | undefined;
    let stop: () => void = () => {};
    const stopped = new Promise<null>(resolve => { stop = () => resolve(null); });
    const valid = () => !cancelled && !this.composing && !cm.aborted && this.catalog.syncConnection() === epoch &&
      this.env.current.docId === env.docId && this.env.current.backslashEscapes === env.backslashEscapes;
    const job: Job = { owner, key, valid, cancel: () => {
      cancelled = true;
      clearTimeout(delay); clearTimeout(budget);
      this.catalog.cancelOwner(owner);
      stop();
    } };
    this.job = job;
    cm.addEventListener("abort", () => { if (this.job === job) this.cancel(); }, { onDocChange: true });
    this.publish("Reading catalog…");
    const timeout = new Promise<SqlCompletionResult>(resolve => {
      budget = setTimeout(() => {
        this.catalog.cancelOwner(owner);
        resolve({ ...local, note: "Catalog suggestions timed out. Try Ctrl+Space again." });
      }, 8000);
    });
    const read = async (): Promise<SqlCompletionResult | null> => {
      if (!explicit) await Promise.race([new Promise<void>(resolve => { delay = setTimeout(resolve, 220); }), stopped]);
      if (!valid()) return null;
      let unavailable = false, limited = false;
      const targets = columnTargets(context, connection.database || "");
      const columns: { name: string; type: string; table: typeof targets[number] }[] = [];
      const tables: { name: string; schema: string }[] = [];
      let schemas: string[] = [];
      if (context.qualifiers.length < 2 && !targets.length) {
        const data = await this.catalog.read("schemas", {}, owner, explicit);
        unavailable = data.failed;
        schemas = data.value?.schemas.map(schema => schema.name) || [];
      }
      if (!valid()) return null;
      let tableSchema = "";
      if (context.place === "table") tableSchema = context.qualifiers[0] || connection.database || "";
      else if (context.place === "expression" && context.qualifiers.length === 1 && !targets.length && schemas.includes(context.qualifiers[0]))
        tableSchema = context.qualifiers[0];
      await Promise.all([
        ...(tableSchema ? [this.catalog.read("tables", { schema: tableSchema, prefix: context.prefix }, owner, explicit).then(data => {
          unavailable ||= data.failed;
          limited = data.truncated;
          if (data.value) tables.push(...data.value.tables.map(table => ({ name: table.name, schema: tableSchema })));
        })] : []),
        ...targets.map(table => this.catalog.read("columns", { schema: table.schema, table: table.table }, owner, explicit).then(data => {
          unavailable ||= data.failed;
          if (data.value) columns.push(...data.value.columns.map(column => ({ name: column.name, type: column.type, table })));
        })),
      ]);
      columns.sort((a, b) => targets.indexOf(a.table) - targets.indexOf(b.table));
      return resultFor(context, suggestions(context, { schemas, tables, columns }),
        unavailable ? "Some catalog suggestions are unavailable." : limited ? "More tables match. Keep typing to narrow the list." : "");
    };
    const finished = Promise.race([read().catch(() => null), timeout, stopped]).then(result => {
      clearTimeout(delay); clearTimeout(budget);
      if (!valid() || this.job !== job || !result) return null;
      this.publish(result.note);
      return result;
    });
    if (!cm.view) return finished;
    const view = cm.view;
    // The single source cannot stream results. Keep local suggestions usable
    // while reading, then use the public restart command to publish the final list.
    void finished.then(result => {
      if (!result || !valid() || this.view !== view || !view.hasFocus) return;
      this.ready = { key, result };
      startCompletion(view);
    });
    return local;
  }
}
const sessions = new WeakMap<Environment, CompletionSession>();
function sessionFor(env: Environment): CompletionSession {
  let session = sessions.get(env);
  if (!session) { session = new CompletionSession(env); sessions.set(env, session); }
  return session;
}
export function completionSources(env: Environment): CompletionSource[] { return [sessionFor(env).source]; }

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
    auto = session.auto;
    constructor(readonly view: EditorView) {
      session.view = view;
      session.typed = false;
      session.cancel();
      // A restored EditorState may carry a completion from its former tab or
      // connection. Do not revive that list or its in-flight request.
      queueMicrotask(() => { if (session.view === view) closeCompletion(view); });
    }
    update(update: import("@codemirror/view").ViewUpdate) {
      const epoch = session.catalog.syncConnection();
      const environmentChanged = epoch !== this.epoch || env.current.docId !== this.docId || env.current.backslashEscapes !== this.backslashEscapes;
      if (update.docChanged) session.typed = true;
      if (update.docChanged || update.selectionSet || environmentChanged || this.auto !== session.auto ||
        (completionStatus(update.startState) === "active" && completionStatus(update.state) === null)) session.cancel();
      if (environmentChanged || this.auto !== session.auto) {
        queueMicrotask(() => { if (session.view === this.view) closeCompletion(this.view); });
      }
      this.epoch = epoch; this.docId = env.current.docId; this.backslashEscapes = env.current.backslashEscapes;
      this.auto = session.auto;
    }
    destroy() { if (session.view === this.view) { session.cancel(); session.view = undefined; } }
  });
  const textOption = (className: string, text: string) => {
    const span = document.createElement("span");
    span.className = className;
    span.textContent = text;
    return span;
  };
  const extension = [completionHint, lifecycle, autocompletion({
    // The source owns the existing 220 ms catalog debounce. Avoid stacking the
    // default 100 ms delay on top. Explicit start still has CM's fixed 50 ms.
    activateOnTypingDelay: 0, icons: false,
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
        // A manual retry also supersedes a result awaiting CM's 50 ms refresh.
        session.cancel();
        session.typed = false;
      }
    },
  })];
  extensions.set(env, extension);
  return extension;
}
