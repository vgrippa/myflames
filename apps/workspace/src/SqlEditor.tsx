// The Query editor's text area: a native <textarea> (typing, undo, IME,
// spell check off, screen readers and selection all stay the browser's) over
// a layer that draws the same text with syntax colors, the statement at the
// cursor, a bracket or quote pair, and an error position, plus a line number
// gutter. The layers repeat the textarea's text metrics exactly and follow its
// scroll position with a transform, so nothing re-renders on scroll.
import {
  forwardRef,
  useImperativeHandle,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import type { Connection } from "./api";
import { MAC } from "./platform";
import { useSqlCompletion } from "./useSqlCompletion";
import "./completion.css";
import "./editor-find.css";
import { HIGHLIGHT_LIMIT, matchAt, tokenize, type Token } from "./sqlHighlight";

export type Range = { start: number; end: number };
/** Lines numbered one by one up to here; a longer text gets a plain column. */
const GUTTER_LINE_LIMIT = 5000;

type Mark = Range & { cls: string };

/** Find stops counting here (the count reads "…+"); a single-character search
 * of a big script must not allocate one object per character. */
const FIND_MATCH_LIMIT = 100_000;
/** At most this many matches are drawn as marks on the highlight layer. */
const FIND_MARK_LIMIT = 1000;

export type FindOptions = { matchCase: boolean; wholeWord: boolean; regex: boolean };
export type FindResult = { matches: Range[]; error: string; capped: boolean };

/** Time budget for a regular expression search in its background worker. */
export const FIND_REGEX_TIMEOUT_MS = 250;
export const FIND_SLOW_PATTERN_ERROR = "Pattern too slow to search";

/** Escape RegExp syntax characters so a literal query matches as plain text. */
function escapeRegExp(value: string): string {
  return value.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
}

/** A word character for the Whole word toggle: letters, digits, _ and $. */
function wordChar(ch: string | undefined): boolean {
  return ch !== undefined && /[\p{L}\p{N}_$]/u.test(ch);
}

/** Whether the code point immediately before *index* is a word character. */
function wordCharBefore(text: string, index: number): boolean {
  if (index <= 0) return false;
  const low = text.charCodeAt(index - 1);
  if (low >= 0xdc00 && low <= 0xdfff && index >= 2) {
    const high = text.charCodeAt(index - 2);
    if (high >= 0xd800 && high <= 0xdbff) return wordChar(text.slice(index - 2, index));
  }
  return wordChar(text[index - 1]);
}

/** Whether the code point starting at *index* is a word character. */
function wordCharAfter(text: string, index: number): boolean {
  if (index >= text.length) return false;
  const cp = text.codePointAt(index);
  return cp !== undefined && wordChar(String.fromCodePoint(cp));
}

function compileFindRegex(query: string, options: FindOptions): { re: RegExp | null; error: string } {
  try {
    return {
      re: new RegExp(options.wholeWord ? `\\b(?:${query})\\b` : query, options.matchCase ? "g" : "gi"),
      error: "",
    };
  } catch (cause) {
    return { re: null, error: cause instanceof Error ? cause.message : String(cause) };
  }
}

export const FIND_WORKER_SOURCE = `self.onmessage = (e) => {
  const { id, text, query, matchCase, wholeWord, limit } = e.data;
  let re;
  try {
    re = new RegExp(wholeWord ? "\\\\b(?:" + query + ")\\\\b" : query, matchCase ? "g" : "gi");
  } catch (cause) {
    self.postMessage({ id, matches: [], error: cause instanceof Error ? cause.message : String(cause), capped: false });
    return;
  }
  const matches = [];
  for (const hit of text.matchAll(re)) {
    const start = hit.index ?? 0;
    if (hit[0].length > 0) matches.push({ start, end: start + hit[0].length });
    if (matches.length >= limit) {
      self.postMessage({ id, matches, error: "", capped: true });
      return;
    }
  }
  self.postMessage({ id, matches, error: "", capped: false });
};`;

export type RegexWorkerLike = {
  postMessage: (data: { id: number; text: string; query: string; matchCase: boolean; wholeWord: boolean; limit: number }) => void;
  terminate: () => void;
  onmessage: ((e: { data: { id: number; matches: Range[]; error: string; capped: boolean } }) => void) | null;
  onerror: ((e: { message?: string }) => void) | null;
};

export type RegexWorkerController = {
  search: (text: string, query: string, options: FindOptions, onDone: (result: FindResult) => void, timeoutMs?: number) => () => void;
  dispose: () => void;
};

function defaultSpawnRegexWorker(): RegexWorkerLike {
  const url = URL.createObjectURL(new Blob([FIND_WORKER_SOURCE], { type: "text/javascript" }));
  const worker = new Worker(url);
  return {
    postMessage: (data) => worker.postMessage(data),
    terminate: () => {
      worker.terminate();
      URL.revokeObjectURL(url);
    },
    set onmessage(handler) {
      worker.onmessage = handler as ((this: AbstractWorker, ev: MessageEvent) => unknown) | null;
    },
    get onmessage() {
      return null;
    },
    set onerror(handler) {
      worker.onerror = handler as ((this: AbstractWorker, ev: ErrorEvent) => unknown) | null;
    },
    get onerror() {
      return null;
    },
  };
}

/** Run regular expression searches in a terminable worker with a time budget. */
export function createRegexFinder(spawnWorker: () => RegexWorkerLike = defaultSpawnRegexWorker): RegexWorkerController {
  let worker: RegexWorkerLike | null = null;
  let busy = false;
  let seq = 0;
  function kill() {
    if (worker) {
      worker.terminate();
      worker = null;
    }
    busy = false;
  }
  return {
    search(text, query, options, onDone, timeoutMs = FIND_REGEX_TIMEOUT_MS) {
      const id = ++seq;
      if (busy) kill();
      if (!query) {
        onDone({ matches: [], error: "", capped: false });
        return () => {};
      }
      const { re, error } = compileFindRegex(query, options);
      if (!re) {
        onDone({ matches: [], error, capped: false });
        return () => {};
      }
      if (!worker) worker = spawnWorker();
      const active = worker;
      busy = true;
      const timer = setTimeout(() => {
        if (seq !== id) return;
        kill();
        onDone({ matches: [], error: FIND_SLOW_PATTERN_ERROR, capped: false });
      }, timeoutMs);
      active.onmessage = (e) => {
        if (seq !== id || e.data.id !== id) return;
        clearTimeout(timer);
        busy = false;
        onDone({ matches: e.data.matches, error: e.data.error, capped: e.data.capped });
      };
      active.onerror = (e) => {
        if (seq !== id) return;
        clearTimeout(timer);
        kill();
        onDone({ matches: [], error: e.message || FIND_SLOW_PATTERN_ERROR, capped: false });
      };
      active.postMessage({
        id,
        text,
        query,
        matchCase: options.matchCase,
        wholeWord: options.wholeWord,
        limit: FIND_MATCH_LIMIT,
      });
      return () => {
        if (seq === id) seq++;
        clearTimeout(timer);
        if (busy) kill();
      };
    },
    dispose() {
      seq++;
      kill();
    },
  };
}

/** One search pass over the text; a bad pattern is reported, never thrown.
 * Case-insensitive literal search uses a Unicode RegExp on the escaped query
 * so characters whose lowercase expands (such as "İ") keep original offsets. */
export function computeFind(text: string, query: string, options: FindOptions): FindResult {
  if (!query) return { matches: [], error: "", capped: false };
  if (options.regex) {
    const { re, error } = compileFindRegex(query, options);
    if (!re) return { matches: [], error, capped: false };
    const matches: Range[] = [];
    for (const hit of text.matchAll(re)) {
      const start = hit.index ?? 0;
      // Empty matches (a*, \b) would be invisible marks; skip them.
      if (hit[0].length > 0) matches.push({ start, end: start + hit[0].length });
      if (matches.length >= FIND_MATCH_LIMIT) return { matches, error: "", capped: true };
    }
    return { matches, error: "", capped: false };
  }
  const re = new RegExp(escapeRegExp(query), options.matchCase ? "gu" : "giu");
  const matches: Range[] = [];
  for (const hit of text.matchAll(re)) {
    const start = hit.index ?? 0;
    const end = start + hit[0].length;
    if (end > start && (!options.wholeWord || (!wordCharBefore(text, start) && !wordCharAfter(text, end)))) {
      matches.push({ start, end });
      if (matches.length >= FIND_MATCH_LIMIT) return { matches, error: "", capped: true };
    }
  }
  return { matches, error: "", capped: false };
}

/** The first match at or after the anchor, wrapping to the first match. */
export function firstMatchAt(matches: Range[], anchor: number): number {
  for (let i = 0; i < matches.length; i++) if (matches[i].start >= anchor) return i;
  return 0;
}

/** Text pieces with their classes: token kinds plus the marks over them. */
function pieces(sql: string, tokens: Token[], marks: Mark[]): ReactNode[] {
  const cuts = new Set<number>([0, sql.length]);
  for (const t of tokens) {
    cuts.add(t.start);
    cuts.add(t.end);
  }
  for (const m of marks) {
    cuts.add(Math.max(0, Math.min(sql.length, m.start)));
    cuts.add(Math.max(0, Math.min(sql.length, m.end)));
  }
  const points = [...cuts].sort((a, b) => a - b);
  const out: ReactNode[] = [];
  let k = 0;
  let plain = "";
  // Marks ride along in a small active set instead of one scan of every mark
  // per piece: find can add hundreds of them. Mark edges are cut points, so a
  // mark either covers a piece or ends before it. The sort is stable, keeping
  // the caller's class order for marks that share a start.
  const ordered = [...marks].sort((a, b) => a.start - b.start);
  const active: Mark[] = [];
  let at = 0;
  for (let p = 0; p < points.length - 1; p++) {
    const start = points[p];
    const end = points[p + 1];
    if (end <= start) continue;
    while (k < tokens.length && tokens[k].end <= start) k++;
    const token = k < tokens.length && tokens[k].start <= start && start < tokens[k].end ? tokens[k] : null;
    while (at < ordered.length && ordered[at].start <= start) {
      const m = ordered[at++];
      if (m.end > start && m.end > m.start) active.push(m);
    }
    for (let i = active.length - 1; i >= 0; i--) if (active[i].end <= start) active.splice(i, 1);
    const classes: string[] = [];
    if (token) classes.push(`tok-${token.kind}`);
    for (const m of active) classes.push(m.cls);
    const text = sql.slice(start, end);
    if (!classes.length) {
      plain += text;
      continue;
    }
    if (plain) {
      out.push(plain);
      plain = "";
    }
    out.push(
      <span key={start} className={classes.join(" ")}>
        {text}
      </span>,
    );
  }
  if (plain) out.push(plain);
  return out;
}

/** 0-based line of an offset using precomputed newline offsets. */
function lineAt(newlines: number[], length: number, offset: number) {
  const limit = Math.min(offset, length);
  if (limit <= 0) return 0;
  let lo = 0;
  let hi = newlines.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (newlines[mid] < limit) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

export type SqlEditorProps = {
  value: string;
  onChange: (value: string, selectionStart?: number, selectionEnd?: number) => void;
  onSelect?: () => void;
  onKeyDown?: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  /** The caret or selection, to find the bracket pair next to it. */
  caret: Range;
  /** The statement Run would send (tinted while the caret is in it). */
  statement?: Range | null;
  /** The statement that just ran, highlighted briefly (`.sql-backdrop mark`). */
  flash?: Range | null;
  /** Where the last error points (a wavy underline). */
  error?: Range | null;
  describedBy?: string;
  /** Editor height in px (the splitter below sets it). */
  height?: number;
  placeholder?: string;
  completionConnection?: Connection;
  completionScope?: string;
  backslashEscapes?: boolean;
};

/** The textarea and its layers; the ref is the textarea itself. */
export const SqlEditor = forwardRef<HTMLTextAreaElement, SqlEditorProps>(function SqlEditor(
  { value, onChange, onSelect, onKeyDown, caret, statement, flash, error, describedBy, height, placeholder, completionConnection, completionScope = "", backslashEscapes = true },
  ref,
) {
  const area = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => area.current as HTMLTextAreaElement);
  const layer = useRef<HTMLDivElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const backdrop = useRef<HTMLDivElement>(null);
  const [composing, setComposing] = useState(false);
  const focusState = useRef(false);
  const focused = focusState.current;
  const [completionMode, setCompletionMode] = useState<false | "auto" | "manual">(false);
  const [choice, setChoice] = useState(0);
  const [completionRequest, setCompletionRequest] = useState(0);
  const listId = useId();
  const completion = useSqlCompletion(completionConnection, value, caret.start, caret.end,
    focused && !composing && !!completionMode, completionMode === "manual", completionScope, backslashEscapes, completionRequest);
  const showing = focused && !composing && !!completionMode &&
    (completion.items.length > 0 || completion.loading || !!completion.note);
  const selected = Math.min(choice, Math.max(0, completion.items.length - 1));
  const popup = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  useEffect(() => { setCompletionMode((m) => (m === false ? m : false)); }, [completionScope]);
  useEffect(() => { setChoice((c) => (c === 0 ? c : 0)); }, [completion.key]);
  useLayoutEffect(() => {
    if (showing) document.getElementById(`${listId}-${selected}`)?.scrollIntoView({ block: "nearest" });
  }, [selected, showing, completion.items]);
  function positionCompletion() {
    const el = area.current;
    if (!showing || !el) return;
    // Measure a plain-text mirror, including tabs and non-ASCII names. The
    // popup is a portal so editor splitters and overflow never clip it.
    const style = getComputedStyle(el), rect = el.getBoundingClientRect();
    const mirror = document.createElement("div");
    Object.assign(mirror.style, { position: "fixed", left: "0", top: "0", visibility: "hidden",
      whiteSpace: "pre", font: style.font, lineHeight: style.lineHeight, tabSize: style.tabSize,
      letterSpacing: style.letterSpacing, fontVariantLigatures: "none", padding: style.padding });
    mirror.textContent = value.slice(0, caret.start);
    const marker = document.createElement("span"); marker.textContent = "\u200b";
    mirror.append(marker); document.body.append(mirror);
    const point = marker.getBoundingClientRect(); mirror.remove();
    const width = popup.current?.offsetWidth || 320;
    const height = popup.current?.offsetHeight || 180;
    const line = parseFloat(style.lineHeight) || 20;
    const x = rect.left + point.left - el.scrollLeft;
    const y = rect.top + point.top - el.scrollTop;
    const left = Math.max(8, Math.min(x, window.innerWidth - width - 8));
    const top = Math.max(8, Math.min(y + line + height > window.innerHeight - 8 ? y - height : y + line, window.innerHeight - height - 8));
    setPosition(previous => previous.left === left && previous.top === top ? previous : { left, top });
  }
  useLayoutEffect(positionCompletion, [showing, value, caret.start, selected, completion.items, completion.loading, completion.note]);
  useEffect(() => {
    if (!showing) return;
    window.addEventListener("resize", positionCompletion);
    return () => window.removeEventListener("resize", positionCompletion);
  }, [showing, value, caret.start]);
  function acceptCompletion(index: number) {
    const el = area.current, item = completion.items[index], context = completion.context;
    if (!el || !item || !context || el.value !== value || el.selectionStart !== caret.start || el.selectionEnd !== caret.end) return;
    el.focus();
    el.setSelectionRange(context.start, context.end);
    // Native insertText joins the browser's undo stack. Never replace .value
    // or synthesize a React edit, which discards native undo history.
    if (!document.execCommand("insertText", false, item.insert)) el.setSelectionRange(caret.start, caret.end);
    setCompletionMode(false);
    onSelect?.();
  }
  function completionKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (composing || e.nativeEvent.isComposing || e.keyCode === 229) return;
    const findMod = MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
    if (findMod && (e.code === "KeyF" || (!MAC && e.code === "KeyH"))) {
      e.preventDefault();
      e.stopPropagation();
      setCompletionMode(false);
      openFind((MAC && e.altKey) || e.code === "KeyH" ? "replace" : "find");
      return;
    }
    if (findOpen && findMod && !e.altKey && e.code === "KeyG") {
      e.preventDefault();
      e.stopPropagation();
      navigateFind(e.shiftKey ? -1 : 1);
      return;
    }
    if (e.ctrlKey && !e.metaKey && !e.altKey && (e.code === "Space" || e.key === " ")) {
      e.preventDefault(); e.stopPropagation(); setCompletionMode("manual"); setCompletionRequest(n => n + 1); setChoice(0); return;
    }
    if (showing && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setCompletionMode(false); return; }
      if (completion.items.length && !e.shiftKey && ["ArrowDown", "ArrowUp", "Enter", "Tab"].includes(e.key)) {
        e.preventDefault(); e.stopPropagation();
        if (e.key === "ArrowDown") setChoice((selected + 1) % completion.items.length);
        else if (e.key === "ArrowUp") setChoice((selected + completion.items.length - 1) % completion.items.length);
        else acceptCompletion(selected);
        return;
      }
    }
    if (["ArrowLeft", "ArrowRight", "Home", "End", "Tab"].includes(e.key) || e.ctrlKey || e.metaKey) setCompletionMode(false);
    onKeyDown?.(e);
  }

  // Find and replace (⌘F / ⌥⌘F, Ctrl+F / Ctrl+H): a bar docked at the
  // editor's top right; matches are marks on the highlight layer, the current
  // one stronger, and edits keep the browser's undo stack.
  const [findMode, setFindMode] = useState<false | "find" | "replace">(false);
  const [findQuery, setFindQuery] = useState("");
  const [replaceWith, setReplaceWith] = useState("");
  const [findOptions, setFindOptions] = useState<FindOptions>({ matchCase: false, wholeWord: false, regex: false });
  const [found, setFound] = useState<FindResult>({ matches: [], error: "", capped: false });
  const [findCurrent, setFindCurrent] = useState(0);
  const findField = useRef<HTMLInputElement>(null);
  const findAnchor = useRef(0);
  const lastReplaceStart = useRef<number | null>(null);
  const findReveal = useRef(false);
  const regexFinder = useRef<RegexWorkerController | null>(null);
  const findRunId = useRef(0);
  const foundFor = useRef({ value: "", query: "", options: "" });
  const findErrorId = useId();
  const findOpen = findMode !== false;
  const findIndex = found.matches.length ? Math.min(findCurrent, found.matches.length - 1) : 0;
  const findOptionsKey = `${findOptions.matchCase ? "c" : ""}${findOptions.wholeWord ? "w" : ""}${findOptions.regex ? "r" : ""}`;

  useEffect(() => () => {
    regexFinder.current?.dispose();
    regexFinder.current = null;
  }, []);

  /** Select the match and scroll it into view. The textarea has no
   * scrollIntoView for a selection, so the match's spot is measured with a
   * mirror, the same trick as the completion popup. */
  function revealMatch(index: number, matches: Range[]) {
    const el = area.current;
    const m = matches[index];
    if (!el || !m) return;
    el.setSelectionRange(m.start, m.end);
    const style = getComputedStyle(el);
    const mirror = document.createElement("div");
    Object.assign(mirror.style, { position: "fixed", left: "0", top: "0", visibility: "hidden",
      whiteSpace: "pre", font: style.font, lineHeight: style.lineHeight, tabSize: style.tabSize,
      letterSpacing: style.letterSpacing, fontVariantLigatures: "none", padding: style.padding });
    mirror.textContent = value.slice(0, m.start);
    const marker = document.createElement("span");
    marker.textContent = "\u200b";
    mirror.append(marker);
    document.body.append(mirror);
    const point = marker.getBoundingClientRect();
    mirror.remove();
    const line = parseFloat(style.lineHeight) || 20;
    if (point.top < el.scrollTop + line) el.scrollTop = Math.max(0, point.top - line);
    else if (point.top + line > el.scrollTop + el.clientHeight) el.scrollTop = point.top + 2 * line - el.clientHeight;
    if (point.left < el.scrollLeft + 40) el.scrollLeft = Math.max(0, point.left - 40);
    else if (point.left > el.scrollLeft + el.clientWidth - 40) el.scrollLeft = point.left - el.clientWidth + 80;
    onSelect?.();
  }

  function openFind(mode: "find" | "replace") {
    const el = area.current;
    let query = findQuery;
    if (el && el.selectionStart < el.selectionEnd) {
      const selected = value.slice(el.selectionStart, el.selectionEnd);
      // A one-line selection prefills the field; a longer one stays put.
      if (!selected.includes("\n")) query = selected;
    }
    findAnchor.current = el ? el.selectionStart : 0;
    lastReplaceStart.current = null;
    setFindQuery(query);
    if (!query || !findOptions.regex) {
      const result = computeFind(value, query, findOptions);
      foundFor.current = { value, query, options: findOptionsKey };
      setFound(result);
      const index = firstMatchAt(result.matches, findAnchor.current);
      setFindCurrent(index);
      setFindMode(mode);
      revealMatch(index, result.matches);
    } else if (
      foundFor.current.value === value &&
      foundFor.current.query === query &&
      foundFor.current.options === findOptionsKey
    ) {
      const index = firstMatchAt(found.matches, findAnchor.current);
      setFindCurrent(index);
      setFindMode(mode);
      revealMatch(index, found.matches);
    } else {
      findReveal.current = true;
      setFindMode(mode);
    }
  }

  useEffect(() => {
    if (findMode === false) return;
    const field = findField.current;
    if (field) {
      field.focus();
      field.select();
    }
  }, [findMode]);

  const findPrev = useRef({ query: "", options: "", open: false });
  useEffect(() => {
    const runId = ++findRunId.current;
    if (findMode === false) {
      regexFinder.current?.dispose();
      regexFinder.current = null;
      findPrev.current = { query: "", options: "", open: false };
      foundFor.current = { value: "", query: "", options: "" };
      setFound((f) => (f.matches.length || f.error ? { matches: [], error: "", capped: false } : f));
      return;
    }
    // Typing recomputes after a pause so a big script stays responsive;
    // opening computes at once in openFind (or in the worker for regex).
    const fresh = findPrev.current.query !== findQuery || findPrev.current.options !== findOptionsKey || !findPrev.current.open;
    const delay = findOptions.regex && (!findPrev.current.open || findReveal.current) ? 0 : 100;
    let cancelRegex: (() => void) | null = null;
    function applyResult(result: FindResult) {
      if (findRunId.current !== runId) return;
      findPrev.current = { query: findQuery, options: findOptionsKey, open: true };
      foundFor.current = { value, query: findQuery, options: findOptionsKey };
      setFound(result);
      const index = firstMatchAt(result.matches, findAnchor.current);
      setFindCurrent(index);
      if (fresh || findReveal.current) {
        findReveal.current = false;
        revealMatch(index, result.matches);
      }
    }
    const timer = setTimeout(() => {
      if (!findQuery || !findOptions.regex) {
        applyResult(computeFind(value, findQuery, findOptions));
        return;
      }
      if (!regexFinder.current) regexFinder.current = createRegexFinder();
      cancelRegex = regexFinder.current.search(value, findQuery, findOptions, applyResult);
    }, delay);
    return () => {
      clearTimeout(timer);
      cancelRegex?.();
    };
  }, [findMode, findQuery, findOptionsKey, value]); // eslint-disable-line react-hooks/exhaustive-deps

  function navigateFind(delta: number) {
    const matches = found.matches;
    if (!matches.length) return;
    const next = (findIndex + delta + matches.length) % matches.length;
    setFindCurrent(next);
    lastReplaceStart.current = null;
    findAnchor.current = matches[next].start;
    revealMatch(next, matches);
  }

  /** Escape or the close button: back to the editor, at the current match. */
  function closeFind() {
    setFindMode(false);
    const el = area.current;
    const m = found.matches[findIndex];
    if (el) {
      if (m) el.setSelectionRange(m.start, m.end);
      el.focus();
    }
    onSelect?.();
  }

  function applyReplace(all: boolean) {
    const el = area.current;
    if (!el) return;
    // Recompute against the live text for literal and whole-word searches: an
    // edit since the last debounce must not move a replacement. Regex searches
    // use the worker's verified result for the current text and never run on
    // the main thread.
    let matches: Range[];
    if (!findOptions.regex) {
      matches = computeFind(value, findQuery, findOptions).matches;
    } else if (
      foundFor.current.value === value &&
      foundFor.current.query === findQuery &&
      foundFor.current.options === findOptionsKey
    ) {
      matches = found.matches;
    } else {
      return;
    }
    if (!matches.length) return;
    el.focus();
    if (all) {
      let nextText = "";
      let last = 0;
      for (const m of matches) {
        nextText += value.slice(last, m.start) + replaceWith;
        last = m.end;
      }
      nextText += value.slice(last);
      // Selecting the whole text makes Replace all one step on the undo stack.
      el.setSelectionRange(0, value.length);
      let inserted = false;
      try {
        inserted = document.execCommand("insertText", false, nextText);
      } catch {
        inserted = false;
      }
      if (!inserted) {
        el.setRangeText(nextText, 0, value.length, "end");
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
      el.setSelectionRange(matches[0].start + replaceWith.length, matches[0].start + replaceWith.length);
      lastReplaceStart.current = matches[0].start;
      findAnchor.current = matches[0].start;
    } else {
      const currentIdx = findReveal.current
        ? firstMatchAt(matches, findAnchor.current)
        : Math.min(findIndex, matches.length - 1);
      const m = matches[currentIdx];
      el.setSelectionRange(m.start, m.end);
      let inserted = false;
      try {
        inserted = document.execCommand("insertText", false, replaceWith);
      } catch {
        inserted = false;
      }
      if (!inserted) {
        el.setRangeText(replaceWith, m.start, m.end, "end");
        el.dispatchEvent(new Event("input", { bubbles: true }));
      }
      lastReplaceStart.current = m.start;
      findAnchor.current = m.start + replaceWith.length;
    }
    findReveal.current = true;
    onSelect?.();
    findField.current?.focus();
  }

  function findFieldKeyDown(e: KeyboardEvent<HTMLInputElement>, inReplace: boolean) {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    const mod = MAC ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
    if (mod && (e.code === "KeyF" || (!MAC && e.code === "KeyH"))) {
      e.preventDefault();
      if ((MAC && e.altKey) || e.code === "KeyH") setFindMode("replace");
      else e.currentTarget.select();
      return;
    }
    if (mod && !e.altKey && e.code === "KeyG") {
      e.preventDefault();
      navigateFind(e.shiftKey ? -1 : 1);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      closeFind();
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      if (inReplace) applyReplace(false);
      else navigateFind(e.shiftKey ? -1 : 1);
    }
  }

  const colored = value.length <= HIGHLIGHT_LIMIT;
  const tokens = useMemo(() => (colored ? tokenize(value) : []), [value, colored]);
  const match = colored && caret.start === caret.end ? matchAt(value, tokens, caret.start) : null;
  const statementStart = statement?.start ?? -1;
  const statementEnd = statement?.end ?? -1;
  const errorStart = error?.start ?? -1;
  const errorEnd = error?.end ?? -1;
  const matchKey = match ? `${match.kind}:${match.at}:${match.partner}` : "";
  const drawn = useMemo(() => {
    if (!colored) return null;
    const marks: Mark[] = [];
    if (statementEnd > statementStart) marks.push({ start: statementStart, end: statementEnd, cls: "sql-current" });
    if (errorEnd > errorStart) marks.push({ start: errorStart, end: errorEnd, cls: "sql-error-mark" });
    if (match) {
      const cls = match.partner < 0 ? "sql-unmatched" : "sql-match";
      marks.push({ start: match.at, end: match.at + 1, cls });
      if (match.partner >= 0) marks.push({ start: match.partner, end: match.partner + 1, cls });
    }
    if (findOpen) {
      for (let i = 0; i < found.matches.length && i < FIND_MARK_LIMIT; i++) {
        const m = found.matches[i];
        marks.push({ start: m.start, end: m.end, cls: i === findIndex ? "sql-find-mark sql-find-current" : "sql-find-mark" });
      }
    }
    return pieces(value, tokens, marks);
    // matchKey stands for match (a new object on every render).
  }, [value, tokens, colored, statementStart, statementEnd, errorStart, errorEnd, matchKey, findOpen, found, findIndex]);

  // Line numbers, with the statement's lines and the error line marked.
  const newlines = useMemo(() => {
    const out: number[] = [];
    for (let i = 0; i < value.length; i++) if (value.charCodeAt(i) === 10) out.push(i);
    return out;
  }, [value]);
  const lineCount = newlines.length + 1;
  const firstLine = lineCount <= GUTTER_LINE_LIMIT && statement && statementEnd > statementStart ? lineAt(newlines, value.length, statementStart) : -1;
  const lastLine = lineCount <= GUTTER_LINE_LIMIT && statement && statementEnd > statementStart ? lineAt(newlines, value.length, statementEnd) : -1;
  const errorLine = lineCount <= GUTTER_LINE_LIMIT && errorEnd > errorStart ? lineAt(newlines, value.length, errorStart) : -1;
  const numbers = useMemo(() => {
    if (lineCount > GUTTER_LINE_LIMIT)
      return Array.from({ length: lineCount }, (_, i) => i + 1).join("\n");
    return Array.from({ length: lineCount }, (_, i) => (
      <span
        key={i}
        className={[i >= firstLine && i <= lastLine ? "is-current" : "", i === errorLine ? "is-error" : ""].join(" ").trim() || undefined}
      >
        {i + 1}
      </span>
    ));
  }, [lineCount, firstLine, lastLine, errorLine]);

  // The layers follow the textarea's scroll position.
  function follow() {
    const el = area.current;
    if (!el) return;
    const shift = `translate(${-el.scrollLeft}px, ${-el.scrollTop}px)`;
    if (layer.current) layer.current.style.transform = shift;
    if (backdrop.current) backdrop.current.style.transform = shift;
    if (gutter.current) gutter.current.style.transform = `translateY(${-el.scrollTop}px)`;
  }
  useLayoutEffect(follow, [value, height, flash?.start, flash?.end]);

  const flashLit = flash && flash.end > flash.start ? flash : null;
  return (
    <div
      className={`sql-editor-wrap${colored ? " is-colored" : ""}${composing ? " is-composing" : ""}`}
      style={height ? { height } : undefined}
    >
      <div className="sql-gutter" aria-hidden="true">
        <div ref={gutter} className="sql-gutter-lines">
          {numbers}
        </div>
      </div>
      <div className="sql-surface">
        {colored && (
          <div className="sql-highlight" aria-hidden="true">
            <div ref={layer} className="sql-layer">
              {drawn}
              {"\n"}
            </div>
          </div>
        )}
        {flashLit && (
          // The statement that runs, highlighted without touching the caret or selection.
          <div className="sql-backdrop" aria-hidden="true">
            <div ref={backdrop} className="sql-layer">
              {value.slice(0, flashLit.start)}
              <mark>{value.slice(flashLit.start, flashLit.end)}</mark>
              {value.slice(flashLit.end)}
              {"\n"}
            </div>
          </div>
        )}
        <textarea
          ref={area}
          aria-label="SQL editor"
          aria-describedby={describedBy}
          role="combobox"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={showing}
          aria-controls={showing ? listId : undefined}
          aria-activedescendant={showing && completion.items.length ? `${listId}-${selected}` : undefined}
          className="sql-editor"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          autoComplete="off"
          wrap="off"
          placeholder={placeholder}
          value={value}
          onChange={(e) => {
            const input = e.nativeEvent as InputEvent;
            if (input.inputType === "historyUndo" && lastReplaceStart.current !== null) {
              findAnchor.current = lastReplaceStart.current;
              lastReplaceStart.current = null;
            }
            const nextMode = composing || input.isComposing || input.inputType?.startsWith("history") ? false : "auto";
            setCompletionMode((m) => (m === nextMode ? m : nextMode));
            onChange(e.target.value, e.target.selectionStart, e.target.selectionEnd);
          }}
          onFocus={() => { focusState.current = true; }}
          onBlur={() => { focusState.current = false; setCompletionMode(false); }}
          onPointerDown={() => setCompletionMode(false)}
          onSelect={onSelect}
          onScroll={() => { follow(); positionCompletion(); }}
          onKeyDown={completionKeyDown}
          onCompositionStart={() => { setComposing(true); setCompletionMode(false); }}
          onCompositionEnd={() => { setComposing(false); setCompletionMode(false); }}
        />
        {findMode !== false && (
          <div className="sql-find" role="search" aria-label="Find and replace">
            <div className="sql-find-row">
              <input
                ref={findField}
                type="text"
                className="sql-find-field"
                aria-label="Find"
                aria-invalid={found.error ? true : undefined}
                aria-describedby={found.error ? findErrorId : undefined}
                placeholder="Find"
                spellCheck={false}
                autoCapitalize="off"
                autoCorrect="off"
                autoComplete="off"
                value={findQuery}
                onChange={(e) => setFindQuery(e.target.value)}
                onKeyDown={(e) => findFieldKeyDown(e, false)}
              />
              <span className="sql-find-count" role="status" aria-live="polite">
                {!findQuery || found.error
                  ? ""
                  : found.matches.length === 0
                    ? "No matches"
                    : `${findIndex + 1} of ${found.matches.length}${found.capped ? "+" : ""}`}
              </span>
              <button type="button" className="sql-find-toggle" aria-pressed={findOptions.matchCase}
                aria-label="Match case" title="Match case"
                onClick={() => setFindOptions((o) => ({ ...o, matchCase: !o.matchCase }))}>
                Aa
              </button>
              <button type="button" className="sql-find-toggle sql-find-word" aria-pressed={findOptions.wholeWord}
                aria-label="Whole word" title="Match the whole word"
                onClick={() => setFindOptions((o) => ({ ...o, wholeWord: !o.wholeWord }))}>
                |ab|
              </button>
              <button type="button" className="sql-find-toggle" aria-pressed={findOptions.regex}
                aria-label="Regular expression" title="Use a regular expression"
                onClick={() => setFindOptions((o) => ({ ...o, regex: !o.regex }))}>
                .*
              </button>
              <button type="button" className="sql-find-nav" aria-label={`Previous match (${MAC ? "⇧⌘G" : "Ctrl+Shift+G"})`}
                title="Previous match (Shift+Enter)" disabled={!found.matches.length} onClick={() => navigateFind(-1)}>
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m4 10 4-4 4 4" /></svg>
              </button>
              <button type="button" className="sql-find-nav" aria-label={`Next match (${MAC ? "⌘G" : "Ctrl+G"})`}
                title="Next match (Enter)" disabled={!found.matches.length} onClick={() => navigateFind(1)}>
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
              </button>
              <button type="button" className="sql-find-close" aria-label="Close find (Escape)" title="Close find (Escape)" onClick={closeFind}>
                <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true"><path d="m4 4 8 8M12 4l-8 8" /></svg>
              </button>
            </div>
            {findMode === "replace" && (
              <div className="sql-find-row">
                <input
                  type="text"
                  className="sql-find-field"
                  aria-label="Replace with"
                  placeholder="Replace"
                  spellCheck={false}
                  autoCapitalize="off"
                  autoCorrect="off"
                  autoComplete="off"
                  value={replaceWith}
                  onChange={(e) => setReplaceWith(e.target.value)}
                  onKeyDown={(e) => findFieldKeyDown(e, true)}
                />
                <button type="button" className="sql-find-action" disabled={!found.matches.length} onClick={() => applyReplace(false)}>
                  Replace
                </button>
                <button type="button" className="sql-find-action" disabled={!found.matches.length} onClick={() => applyReplace(true)}>
                  Replace all
                </button>
              </div>
            )}
            <div className="sql-find-error" role="alert" id={findErrorId} hidden={!found.error}>
              {found.error}
            </div>
          </div>
        )}
        {showing && createPortal(
          <div ref={popup} className="sql-completion" style={position}>
            <div id={listId} role="listbox" aria-label="SQL suggestions" className="sql-completion-list">
              {completion.items.map((item, index) => (
                <div key={`${item.kind}:${item.insert}`} id={`${listId}-${index}`} role="option"
                  aria-selected={index === selected} className="sql-completion-option"
                  onMouseDown={(e) => { e.preventDefault(); acceptCompletion(index); }}
                  onMouseMove={() => setChoice(index)}>
                  <span className="sql-completion-label">{item.label}</span>
                  <span className="sql-completion-kind">{item.kind}</span>
                  <span className="sql-completion-detail">{item.detail}</span>
                </div>
              ))}
            </div>
            <div className="sql-completion-hint" role="status">
              {completion.loading ? "Reading catalog…" : completion.note || <><kbd>Enter</kbd> or <kbd>Tab</kbd> to insert · <kbd>Esc</kbd> to dismiss</>}
            </div>
          </div>, document.body,
        )}
      </div>
    </div>
  );
});
