import { useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { EditorSelection, type Text } from "@codemirror/state";
import { isolateHistory } from "@codemirror/commands";
import { EditorView } from "@codemirror/view";
import { MAC } from "../platform";
import {
  computeFind, createRegexFinder, firstMatchAt, planReplace, resolveGoToLine,
  type FindOptions, type FindResult, type RegexWorkerController,
} from "./find";
import { attachFindViewListener, clearFindMarks, setFindCurrent, setFindMatches } from "./findMarks";

type FindMode = false | "find" | "replace" | "line";
type FindSnapshot = {
  mode: FindMode; query: string; replacement: string; options: FindOptions;
  found: FindResult; current: number; line: string; lineCount: number; focusVersion: number;
};
const emptyResult = (): FindResult => ({ matches: [], error: "", capped: false });
const initialSnapshot = (): FindSnapshot => ({
  mode: false, query: "", replacement: "", options: { matchCase: false, wholeWord: false, regex: false },
  found: emptyResult(), current: 0, line: "", lineCount: 1, focusVersion: 0,
});
const controllers = new WeakMap<EditorView, FindController>();

/** One controller per view. Mutable anchors and search generations never lag a React render. */
export class FindController {
  snapshot = initialSnapshot();
  private anchor = 0;
  private undoAnchor: number | null = null;
  private replacing = false;
  private revealAfterSearch = false;
  private searchedDoc: Text | null = null;
  private generation = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private cancelRegex: (() => void) | null = null;
  private finder: RegexWorkerController | null = null;
  private alive = true;
  private detach: () => void;

  constructor(
    private readonly view: EditorView,
    private readonly changed: (snapshot: FindSnapshot) => void,
    private readonly makeFinder: () => RegexWorkerController = createRegexFinder,
  ) {
    controllers.set(view, this);
    this.detach = attachFindViewListener(view, event => {
      if (event.type === "stateReplaced") {
        // Called during setState/destroy: do not dispatch into an updating view.
        this.reset();
        return;
      }
      if (event.isUndo && this.undoAnchor !== null) {
        this.anchor = this.undoAnchor;
        this.revealAfterSearch = true;
      }
      if (!this.replacing) this.undoAnchor = null;
      this.publish({ lineCount: view.state.doc.lines });
      if (this.isOpen()) this.scheduleSearch(100, this.revealAfterSearch);
    }, () => this.isOpen());
  }

  private publish(patch: Partial<FindSnapshot>) {
    this.snapshot = { ...this.snapshot, ...patch };
    if (this.alive) this.changed(this.snapshot);
  }

  isOpen() { return this.snapshot.mode === "find" || this.snapshot.mode === "replace"; }

  private cancelSearch() {
    this.generation++;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.cancelRegex?.();
    this.cancelRegex = null;
    this.searchedDoc = null;
  }

  private reset() {
    this.cancelSearch();
    this.finder?.dispose();
    this.finder = null;
    this.undoAnchor = null;
    this.revealAfterSearch = false;
    this.publish({ mode: false, found: emptyResult(), current: 0 });
  }

  dispose() {
    this.alive = false;
    this.reset();
    this.detach();
    if (controllers.get(this.view) === this) controllers.delete(this.view);
  }

  openFind(mode: "find" | "replace") {
    const { doc, selection } = this.view.state;
    const main = selection.main;
    const selected = doc.sliceString(main.from, main.to);
    const query = selected && !selected.includes("\n") ? selected : this.snapshot.query;
    this.anchor = main.from;
    this.undoAnchor = null;
    this.publish({ mode, query, lineCount: doc.lines, focusVersion: this.snapshot.focusVersion + 1 });
    this.scheduleSearch(0, true);
    return true;
  }

  focusFind(mode: "find" | "replace") {
    if (!this.isOpen()) return this.openFind(mode);
    this.publish({ mode, focusVersion: this.snapshot.focusVersion + 1 });
    return true;
  }

  openGoToLine() {
    this.cancelSearch();
    this.finder?.dispose();
    this.finder = null;
    this.undoAnchor = null;
    this.view.dispatch({ effects: clearFindMarks.of(null) });
    const { doc, selection } = this.view.state;
    this.publish({ mode: "line", line: String(doc.lineAt(selection.main.head).number), lineCount: doc.lines,
      found: emptyResult(), current: 0, focusVersion: this.snapshot.focusVersion + 1 });
    return true;
  }

  setLine(line: string) { this.publish({ line }); }
  setReplacement(replacement: string) { this.publish({ replacement }); }
  setQuery(query: string) {
    this.undoAnchor = null;
    this.publish({ query });
    this.scheduleSearch(100, true);
  }
  toggleOption(key: keyof FindOptions) {
    this.undoAnchor = null;
    this.publish({ options: { ...this.snapshot.options, [key]: !this.snapshot.options[key] } });
    this.scheduleSearch(100, true);
  }

  /** Debounce text edits; regex work always stays inside the terminable worker. */
  private scheduleSearch(delay: number, reveal: boolean) {
    this.cancelSearch();
    this.revealAfterSearch = reveal;
    const generation = this.generation;
    const doc = this.view.state.doc;
    const { query, options } = this.snapshot;
    // Invalidate replacement ranges immediately. Clearing marks is deferred because
    // docChanged is delivered while CodeMirror is still updating its plugins.
    this.publish({ found: emptyResult(), current: 0 });
    queueMicrotask(() => {
      if (this.alive && generation === this.generation && this.isOpen() && this.searchedDoc !== doc) {
        this.view.dispatch({ effects: clearFindMarks.of(null) });
      }
    });
    const apply = (found: FindResult) => {
      if (!this.alive || generation !== this.generation || doc !== this.view.state.doc || !this.isOpen()) return;
      this.searchedDoc = doc;
      const current = firstMatchAt(found.matches, this.anchor);
      this.publish({ found, current });
      this.view.dispatch({ effects: setFindMatches.of({ matches: found.matches, current }) });
      if (this.revealAfterSearch) this.reveal(current);
      this.revealAfterSearch = false;
    };
    const run = () => {
      this.timer = null;
      if (!options.regex || !query) apply(computeFind(doc.toString(), query, options));
      else {
        if (!this.finder) this.finder = this.makeFinder();
        this.cancelRegex = this.finder.search(doc.toString(), query, options, apply);
      }
    };
    // Opening a literal search is synchronous, just as in the old editor.
    if (!delay) run();
    else this.timer = setTimeout(run, delay);
  }

  private reveal(index: number) {
    const match = this.snapshot.found.matches[index];
    if (!match || this.searchedDoc !== this.view.state.doc) return;
    const selection = EditorSelection.single(match.start, match.end);
    this.view.dispatch({ selection, userEvent: "select.search",
      effects: EditorView.scrollIntoView(selection.main, { y: "center" }) });
  }

  navigate(delta: number) {
    if (!this.isOpen()) return false;
    const { found, current } = this.snapshot;
    if (!found.matches.length || this.searchedDoc !== this.view.state.doc) return true;
    const next = (current + delta + found.matches.length) % found.matches.length;
    this.undoAnchor = null;
    this.anchor = found.matches[next].start;
    this.publish({ current: next });
    this.view.dispatch({ effects: setFindCurrent.of(next) });
    this.reveal(next);
    return true;
  }

  replace(all: boolean) {
    if (!this.isOpen() || this.view.state.readOnly) return;
    const { doc } = this.view.state;
    const { options, query, replacement, found, current } = this.snapshot;
    // Never run regex on the main thread, and never apply a stale worker result.
    if (options.regex && this.searchedDoc !== doc) return;
    const matches = options.regex ? found.matches : computeFind(doc.toString(), query, options).matches;
    const index = this.searchedDoc === doc ? current : firstMatchAt(matches, this.anchor);
    const plan = planReplace(doc.toString(), matches, index, replacement, all);
    if (!plan) return;
    this.cancelSearch();
    this.anchor = plan.anchor;
    this.undoAnchor = plan.undoAnchor;
    this.revealAfterSearch = true;
    this.replacing = true;
    try {
      this.view.dispatch({ changes: plan.changes, selection: EditorSelection.single(plan.selection),
        annotations: isolateHistory.of("full"), userEvent: "input.replace" });
    } finally {
      this.replacing = false;
    }
    // Also handles a replacement that leaves the text unchanged.
    this.scheduleSearch(100, true);
  }

  goToLine() {
    const target = resolveGoToLine(this.view.state.doc, this.snapshot.line);
    if (!target) return;
    const selection = EditorSelection.single(target.from);
    this.view.dispatch({ selection, userEvent: "select",
      effects: EditorView.scrollIntoView(selection.main, { y: "center" }) });
    this.close();
  }

  close() {
    this.reset();
    this.view.dispatch({ effects: clearFindMarks.of(null) });
    this.view.focus();
  }
}

export type FindBarProps = { view: EditorView | null };
export function FindBar({ view }: FindBarProps) {
  const [state, setState] = useState(initialSnapshot);
  const controller = useRef<FindController | null>(null);
  const findField = useRef<HTMLInputElement>(null);
  const lineField = useRef<HTMLInputElement>(null);
  const errorId = useId(), lineHintId = useId(), lineId = useId();
  useLayoutEffect(() => {
    if (!view) return;
    const next = new FindController(view, setState);
    controller.current = next;
    setState(next.snapshot);
    return () => { next.dispose(); controller.current = null; };
  }, [view]);
  useLayoutEffect(() => {
    const field = state.mode === "line" ? lineField.current : state.mode ? findField.current : null;
    field?.focus();
    field?.select();
  }, [state.mode, state.focusVersion]);

  function applyReplace(all: boolean) {
    controller.current?.replace(all);
    findField.current?.focus();
  }

  function keyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    const c = controller.current;
    if (!c) return;
    const mod = MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
    if (e.key === "Escape") {
      e.preventDefault(); e.stopPropagation(); c.close();
    } else if (e.key === "Enter" && e.target instanceof HTMLInputElement) {
      e.preventDefault(); e.stopPropagation();
      if (state.mode === "line") c.goToLine();
      else if (e.target.getAttribute("aria-label") === "Replace with") applyReplace(false);
      else c.navigate(e.shiftKey ? -1 : 1);
    } else if (mod && !e.altKey && e.code === "KeyG" && c.isOpen()) {
      e.preventDefault(); e.stopPropagation(); c.navigate(e.shiftKey ? -1 : 1);
    } else if (mod && (e.code === "KeyF" || (!MAC && e.code === "KeyH"))) {
      e.preventDefault(); e.stopPropagation();
      if (state.mode === "line" || e.altKey || e.code === "KeyH") c.focusFind(e.altKey || e.code === "KeyH" ? "replace" : "find");
      else { findField.current?.focus(); findField.current?.select(); }
    } else if (MAC && e.ctrlKey && !e.metaKey && e.code === "KeyG") {
      e.preventDefault(); e.stopPropagation(); c.openGoToLine();
    }
  }

  if (!state.mode) return null;
  const c = controller.current;
  const { found, options } = state;
  const closeButton = <button type="button" className="sql-find-close"
    aria-label={state.mode === "line" ? "Close go to line (Escape)" : "Close find (Escape)"}
    title={state.mode === "line" ? "Close go to line (Escape)" : "Close find (Escape)"} onClick={() => c?.close()}>
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg>
  </button>;
  return <div className="sql-find" role="search" aria-label={state.mode === "line" ? "Go to line" : "Find and replace"} onKeyDown={keyDown}>
    {state.mode === "line" ? <>
      <div className="sql-find-row sql-find-line-row">
        <label htmlFor={lineId}>Line</label>
        <input ref={lineField} id={lineId} type="number" className="sql-find-field" aria-describedby={lineHintId}
          min={1} max={state.lineCount} step={1} value={state.line} onChange={e => c?.setLine(e.target.value)} />
        <button type="button" className="sql-find-action" onClick={() => c?.goToLine()}>Go</button>
        {closeButton}
      </div>
      <span className="sql-find-line-hint" id={lineHintId}>1 to {state.lineCount}</span>
    </> : <>
      <div className="sql-find-row">
        <input ref={findField} type="text" className="sql-find-field" aria-label="Find"
          aria-invalid={found.error ? true : undefined} aria-describedby={found.error ? errorId : undefined}
          placeholder="Find" spellCheck={false} autoCapitalize="off" autoCorrect="off" autoComplete="off"
          value={state.query} onChange={e => c?.setQuery(e.target.value)} />
        <span className="sql-find-count" role="status" aria-live="polite">
          {!state.query || found.error ? "" : !found.matches.length ? "No matches"
            : `${state.current + 1} of ${found.matches.length}${found.capped ? "+" : ""}`}
        </span>
        <button type="button" className="sql-find-toggle" aria-pressed={options.matchCase} aria-label="Match case" title="Match case" onClick={() => c?.toggleOption("matchCase")}>Aa</button>
        <button type="button" className="sql-find-toggle sql-find-word" aria-pressed={options.wholeWord} aria-label="Whole word" title="Match the whole word" onClick={() => c?.toggleOption("wholeWord")}>|ab|</button>
        <button type="button" className="sql-find-toggle" aria-pressed={options.regex} aria-label="Regular expression" title="Use a regular expression" onClick={() => c?.toggleOption("regex")}>.*</button>
        <button type="button" className="sql-find-nav" aria-label={`Previous match (${MAC ? "⇧⌘G" : "Ctrl+Shift+G"})`} title="Previous match (Shift+Enter)" disabled={!found.matches.length} onClick={() => c?.navigate(-1)}>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m4 10 4-4 4 4" /></svg>
        </button>
        <button type="button" className="sql-find-nav" aria-label={`Next match (${MAC ? "⌘G" : "Ctrl+G"})`} title="Next match (Enter)" disabled={!found.matches.length} onClick={() => c?.navigate(1)}>
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
        </button>
        {closeButton}
      </div>
      {state.mode === "replace" && <div className="sql-find-row">
        <input type="text" className="sql-find-field" aria-label="Replace with" placeholder="Replace"
          spellCheck={false} autoCapitalize="off" autoCorrect="off" autoComplete="off"
          value={state.replacement} onChange={e => c?.setReplacement(e.target.value)} />
        <button type="button" className="sql-find-action" disabled={!found.matches.length || view?.state.readOnly} onClick={() => applyReplace(false)}>Replace</button>
        <button type="button" className="sql-find-action" disabled={!found.matches.length || view?.state.readOnly} onClick={() => applyReplace(true)}>Replace all</button>
      </div>}
      <div className="sql-find-row"><button type="button" className="sql-find-action" onClick={() => c?.openGoToLine()}>Go to line</button></div>
      <div className="sql-find-error" role="alert" id={errorId} hidden={!found.error}>{found.error}</div>
    </>}
  </div>;
}

// Keymap calls always resolve the live controller for this view.
export const findHooks = {
  close(view: EditorView): boolean {
    const controller = controllers.get(view);
    if (!controller || !controller.snapshot.mode) return false;
    controller.close();
    return true;
  },
  openFind(view: EditorView, mode: "find" | "replace"): boolean { return controllers.get(view)?.openFind(mode) ?? false; },
  openGoToLine(view: EditorView): boolean { return controllers.get(view)?.openGoToLine() ?? false; },
  next(view: EditorView): boolean { return controllers.get(view)?.navigate(1) ?? false; },
  previous(view: EditorView): boolean { return controllers.get(view)?.navigate(-1) ?? false; },
  isOpen(view: EditorView): boolean { return controllers.get(view)?.isOpen() ?? false; },
};
