import { forwardRef, useImperativeHandle, useLayoutEffect, useRef, useState } from "react";
import { Annotation, Compartment, EditorSelection, EditorState, Prec, type Extension, type StateEffect, type TransactionSpec } from "@codemirror/state";
import { EditorView, crosshairCursor, drawSelection, keymap, lineNumbers, placeholder, rectangularSelection, tooltips } from "@codemirror/view";
import { history, isolateHistory, redo, undo } from "@codemirror/commands";
import { autocompletion, closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { indentUnit } from "@codemirror/language";
import { HIGHLIGHT_LIMIT } from "../sqlHighlight";
import { attachAutomation } from "./automation";
import { argumentHintsExtension } from "./argumentHints";
import { completionExtension, completionSources } from "./completion";
import { FindBar, findHooks } from "./FindBar";
import { findExtension } from "./findMarks";
import { hoverExtension } from "./hover";
import { editorKeymap } from "./keymap";
import { FLASH_MS, matchingBrackets, setError, setFlash, setStatement, sqlMarks } from "./marks";
import { problemsExtension } from "./problems";
import { sqlHighlighting, sqlLanguage } from "./sqlLanguage";
import { clampOffset, clampRange, mainSelection, normalizeEol, normalizedOffset, normalizedRange, type EditorEnv, type Range, type SqlEditorHandle, type SqlEditorProps } from "./types";
import { visualExtension } from "./visual";

export type { SqlEditorHandle, SqlEditorProps } from "./types";
export const External = Annotation.define<boolean>();

declare global {
  interface Window {
    __myflamesEditCommand?: (command: "undo" | "redo") => boolean;
  }
}

// Installed once when this module loads. Native menu commands cannot reliably
// reach CodeMirror's history, especially after editor-only transactions.
if (typeof window !== "undefined") window.__myflamesEditCommand = command => {
  if (command !== "undo" && command !== "redo") return false;
  const editor = document.activeElement?.closest<HTMLElement>(".cm-editor");
  const view = editor ? EditorView.findFromDOM(editor) : null;
  if (!view) return false;
  if (command === "undo") undo(view);
  else redo(view);
  // An empty CodeMirror history is still handled: never fall back to Chromium.
  return true;
};

/** Preserve positions outside the changed span and make the edit one undo step. */
export function externalValueTransaction(state: EditorState, value: string): TransactionSpec | null {
  value = normalizeEol(value);
  const before = state.doc.toString();
  if (before === value) return null;
  let from = 0, oldEnd = before.length, newEnd = value.length;
  while (from < oldEnd && from < newEnd && before.charCodeAt(from) === value.charCodeAt(from)) from++;
  while (oldEnd > from && newEnd > from && before.charCodeAt(oldEnd - 1) === value.charCodeAt(newEnd - 1)) {
    oldEnd--;
    newEnd--;
  }
  return {
    changes: { from, to: oldEnd, insert: value.slice(from, newEnd) },
    annotations: [External.of(true), isolateHistory.of("full")],
  };
}

export type SavedDocument = { state: EditorState; scroll: { top: number; left: number } };
/** EditorState owns selection and history. No serialized text/history recreation. */
export function createDocumentStore() {
  const documents = new Map<string, SavedDocument>();
  return {
    save(id: string, state: EditorState, scroll: SavedDocument["scroll"]) {
      documents.set(id, { state, scroll: { ...scroll } });
    },
    restore(id: string): SavedDocument | undefined { return documents.get(id); },
    prune(ids: readonly string[]) {
      const keep = new Set(ids);
      for (const id of documents.keys()) if (!keep.has(id)) documents.delete(id);
    },
    get size() { return documents.size; },
  };
}

const compartmentNames = ["multi_cursor", "soft_wrap", "close_brackets", "completion_auto", "hover_info", "problem_warnings",
  "language", "matching", "keys", "placeholder", "attributes", "visual"] as const;
type CompartmentName = typeof compartmentNames[number];
type Compartments = Record<CompartmentName, Compartment>;
type Environment = { current: EditorEnv };

function configurations(props: SqlEditorProps, env: Environment, length: number): Record<CompartmentName, Extension> {
  const settings = props.settings, small = length <= HIGHLIGHT_LIMIT;
  return {
    multi_cursor: [EditorState.allowMultipleSelections.of(settings.multi_cursor),
      EditorView.clickAddsSelectionRange.of(event => settings.multi_cursor && event.altKey),
      ...(settings.multi_cursor ? [rectangularSelection({ eventFilter: event => event.button === 0 && event.altKey && event.shiftKey }), crosshairCursor()] : [])],
    soft_wrap: settings.soft_wrap ? EditorView.lineWrapping : [],
    close_brackets: settings.close_brackets ? [closeBrackets(), Prec.high(keymap.of(closeBracketsKeymap))] : [],
    completion_auto: small ? [autocompletion({
      override: completionSources(env), activateOnTyping: settings.completion_auto, defaultKeymap: false,
    }), completionExtension(settings, env)] : [],
    hover_info: small && settings.hover_info ? [hoverExtension(settings, env), argumentHintsExtension(settings, env)] : [],
    problem_warnings: small && settings.problem_warnings ? problemsExtension(settings, env) : [],
    language: [sqlLanguage, sqlHighlighting],
    matching: small ? matchingBrackets : [],
    keys: keymap.of(editorKeymap(settings)),
    placeholder: props.placeholder ? placeholder(props.placeholder) : [],
    attributes: EditorView.contentAttributes.of({
      "aria-label": "SQL editor", class: "sql-editor", spellcheck: "false", autocapitalize: "off", autocorrect: "off",
      ...(props.describedBy ? { "aria-describedby": props.describedBy } : {}),
    }),
    visual: visualExtension(settings, env, length),
  };
}

function sameConfiguration(a: SqlEditorProps | null, b: SqlEditorProps): boolean {
  return !!a && (Object.keys(b.settings) as (keyof typeof b.settings)[]).every(key => a.settings[key] === b.settings[key]) &&
    a.completionConnection === b.completionConnection && a.engine === b.engine && a.serverVersion === b.serverVersion &&
    a.backslashEscapes === b.backslashEscapes && a.placeholder === b.placeholder && a.describedBy === b.describedBy;
}
function sameRange(a?: Range | null, b?: Range | null) {
  return a?.start === b?.start && a?.end === b?.end;
}
function normalizedMark(text: string, range?: Range | null): Range | null {
  return range ? normalizedRange(text, range.start, range.end) : null;
}

export const SqlEditor = forwardRef<SqlEditorHandle, SqlEditorProps>(function SqlEditor(props, ref) {
  const host = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const [findView, setFindView] = useState<EditorView | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const env = useRef<EditorEnv>({ docId: props.docId, backslashEscapes: props.backslashEscapes ?? true });
  env.current = { connection: props.completionConnection, engine: props.engine, serverVersion: props.serverVersion,
    backslashEscapes: props.backslashEscapes ?? true, docId: props.docId, sessionTempTables: props.sessionTempTables };
  const compartments = useRef<Compartments | null>(null);
  if (!compartments.current) compartments.current = Object.fromEntries(compartmentNames.map(name => [name, new Compartment()])) as Compartments;
  const documents = useRef(createDocumentStore());
  const currentDoc = useRef(props.docId);
  const applied = useRef<SqlEditorProps | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  function clearFlashTimer() {
    if (flashTimer.current !== null) clearTimeout(flashTimer.current);
    flashTimer.current = null;
  }
  function createState(p: SqlEditorProps) {
    const value = normalizeEol(p.value);
    const config = configurations(p, env, value.length), slots = compartments.current!;
    const initial = normalizedRange(p.value, p.initialSelection?.start ?? 0, p.initialSelection?.end ?? p.initialSelection?.start ?? 0);
    return EditorState.create({ doc: value, selection: EditorSelection.single(initial.start, initial.end), extensions: [
      ...compartmentNames.map(name => slots[name].of(config[name])),
      history(), lineNumbers(), drawSelection(), indentUnit.of("  "), EditorState.tabSize.of(2), sqlMarks(), findExtension(),
      tooltips({ parent: document.body }),
      Prec.highest(EditorView.domEventHandlers({
        keydown(event) { latest.current.onKeyDown?.(event); return event.defaultPrevented; },
        drop(event) {
          if (!event.dataTransfer?.files.length) return false;
          event.preventDefault();
          return true;
        },
        focus() { latest.current.onFocus?.(); return false; },
      })),
      // Reconfigure in the same transaction, not recursively from updateListener.
      EditorState.transactionExtender.of(tr => {
        if ((tr.startState.doc.length <= HIGHLIGHT_LIMIT) === (tr.newDoc.length <= HIGHLIGHT_LIMIT)) return null;
        const next = configurations(latest.current, env, tr.newDoc.length);
        return { effects: (["completion_auto", "hover_info", "problem_warnings", "matching", "visual"] as const)
          .map(name => slots[name].reconfigure(next[name])) };
      }),
      EditorView.updateListener.of(update => {
        if (update.docChanged && update.transactions.some(tr => tr.docChanged && !tr.annotation(External))) {
          latest.current.onChange(update.state.doc.toString(), mainSelection(update.state));
        }
        if (!update.startState.selection.eq(update.state.selection)) {
          latest.current.onSelectionChange?.(mainSelection(update.state), update.state.selection.ranges.length);
        }
      }),
    ] });
  }

  useLayoutEffect(() => {
    const view = new EditorView({ state: createState(latest.current), parent: host.current! });
    viewRef.current = view;
    currentDoc.current = latest.current.docId;
    const detach = attachAutomation(view);
    setFindView(view);
    return () => {
      clearFlashTimer();
      detach();
      view.destroy();
      viewRef.current = null;
      applied.current = null;
      documents.current.prune([]);
    };
  }, []);

  // Props are authoritative; callback and environment refs stay current without
  // rebuilding extensions on every keystroke or replacing the EditorView.
  useLayoutEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const previous = applied.current, switched = currentDoc.current !== props.docId;
    let scroll: SavedDocument["scroll"] | undefined;
    if (switched) {
      clearFlashTimer();
      documents.current.save(currentDoc.current, view.state.update({ effects: setFlash.of(null) }).state,
        { top: view.scrollDOM.scrollTop, left: view.scrollDOM.scrollLeft });
      const saved = documents.current.restore(props.docId);
      view.setState(saved?.state ?? createState(props));
      scroll = saved?.scroll ?? { top: 0, left: 0 };
      currentDoc.current = props.docId;
    }
    documents.current.prune(props.docIds);
    const external = externalValueTransaction(view.state, props.value);
    if (external) view.dispatch(external);
    const effects: StateEffect<unknown>[] = [];
    if (switched || !sameConfiguration(previous, props)) {
      const config = configurations(props, env, view.state.doc.length);
      for (const name of compartmentNames) effects.push(compartments.current![name].reconfigure(config[name]));
    }
    const statement = normalizedMark(props.value, props.statement), error = normalizedMark(props.value, props.error);
    const flash = normalizedMark(props.value, props.flash);
    if (switched || !previous || !sameRange(normalizedMark(previous.value, previous.statement), statement)) effects.push(setStatement.of(statement));
    if (switched || !previous || !sameRange(normalizedMark(previous.value, previous.error), error)) effects.push(setError.of(error));
    if (switched || !previous || previous.flash !== props.flash || !sameRange(normalizedMark(previous.value, previous.flash), flash)) {
      clearFlashTimer();
      effects.push(setFlash.of(flash));
      if (props.flash) flashTimer.current = setTimeout(() => {
        flashTimer.current = null;
        if (viewRef.current === view) view.dispatch({ effects: setFlash.of(null) });
      }, FLASH_MS);
    }
    const selection = !props.settings.multi_cursor && view.state.selection.ranges.length > 1
      ? EditorSelection.create([view.state.selection.main]) : undefined;
    if (effects.length || selection) view.dispatch({ effects, selection });
    if (scroll) {
      const position = scroll, docId = props.docId;
      view.requestMeasure({ read: () => position, write: saved => {
        if (viewRef.current === view && currentDoc.current === docId) {
          view.scrollDOM.scrollTop = saved.top;
          view.scrollDOM.scrollLeft = saved.left;
        }
      } });
    }
    if (switched) latest.current.onSelectionChange?.(mainSelection(view.state), view.state.selection.ranges.length);
    applied.current = props;
  });

  // Before the parent's onChange echo, imperative callers already use the live
  // normalized document. Only use raw prop offsets while those props still match.
  function offsetSource(view: EditorView): string {
    const live = view.state.doc.toString(), raw = latest.current.value;
    return raw.includes("\r") && normalizeEol(raw) === live ? raw : live;
  }

  useImperativeHandle(ref, () => ({
    get element() { return viewRef.current?.contentDOM ?? null; },
    getValue: () => viewRef.current?.state.doc.toString() ?? "",
    getSelection: () => viewRef.current ? mainSelection(viewRef.current.state) : { start: 0, end: 0 },
    getSelections: () => viewRef.current?.state.selection.ranges.map(range => ({ start: range.from, end: range.to })) ?? [],
    setSelection(start, end = start, options = {}) {
      const view = viewRef.current;
      if (!view) return;
      const range = normalizedRange(offsetSource(view), start, end);
      const selection = EditorSelection.single(range.start, range.end);
      view.dispatch({ selection, userEvent: "select", effects: options.scroll === false ? []
        : EditorView.scrollIntoView(selection.main, { y: options.scroll ?? "nearest" }) });
      if (options.focus) view.focus();
    },
    replaceRange(from, to, text, options = {}) {
      const view = viewRef.current;
      if (!view || view.state.readOnly) return false;
      const raw = offsetSource(view), rawRange = clampRange(from, to, raw.length);
      const range = normalizedRange(raw, rawRange.start, rawRange.end);
      const insert = normalizeEol(text), delta = insert.length - (range.end - range.start);
      // Selection offsets describe the raw post-edit text. Map each segment
      // separately so a bound inside CRLF cannot create a spurious lone CR.
      const selectionOffset = (offset: number) => {
        const at = clampOffset(offset, raw.length - (rawRange.end - rawRange.start) + text.length);
        if (at <= rawRange.start) return normalizedOffset(raw, at);
        if (at <= rawRange.start + text.length) return range.start + normalizedOffset(text, at - rawRange.start);
        return normalizedOffset(raw, at - text.length + rawRange.end - rawRange.start) + delta;
      };
      const selected = options.select ? clampRange(selectionOffset(options.select.start), selectionOffset(options.select.end),
        view.state.doc.length + delta) : null;
      view.dispatch({ changes: { from: range.start, to: range.end, insert },
        selection: selected ? EditorSelection.single(selected.start, selected.end) : undefined,
        annotations: isolateHistory.of("full"), userEvent: "input", scrollIntoView: !!options.focus });
      if (options.focus) view.focus();
      return true;
    },
    insertAtCursor(text) {
      const view = viewRef.current;
      if (!view || view.state.readOnly) return;
      view.dispatch(view.state.replaceSelection(normalizeEol(text)), { annotations: isolateHistory.of("full"), userEvent: "input", scrollIntoView: true });
    },
    focus() { viewRef.current?.focus(); },
    isShown: () => !!viewRef.current?.contentDOM.getClientRects().length,
    revealInPage() { host.current?.scrollIntoView({ block: "nearest", inline: "nearest" }); },
    openFind(mode) { if (viewRef.current) findHooks.openFind(viewRef.current, mode); },
    openGoToLine() { if (viewRef.current) findHooks.openGoToLine(viewRef.current); },
  }), []);

  return <div className="sql-editor-wrap sql-cm" style={props.height === undefined ? undefined : { height: props.height }}>
    <div className="sql-cm-host" ref={host} />
    <FindBar view={findView} />
  </div>;
});
