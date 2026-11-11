import type { Range } from "./types";

export type { Range } from "./types";

/** Find stops counting here (the count reads "…+"); a single-character search
 * of a big script must not allocate one object per character. */
export const FIND_MATCH_LIMIT = 100_000;
/** At most this many matches are drawn as marks on the highlight layer. */
export const FIND_MARK_LIMIT = 1000;

export type FindOptions = { matchCase: boolean; wholeWord: boolean; regex: boolean };
export type FindResult = { matches: Range[]; error: string; capped: boolean };

/** Time budget for a regular expression search in its background worker. */
export const FIND_REGEX_TIMEOUT_MS = 250;
export const FIND_SLOW_PATTERN_ERROR = "Pattern too slow to search";

/** Escape RegExp syntax characters so a literal query matches as plain text. */
export function escapeRegExp(value: string): string {
  return value.replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
}

/** A word character for the Whole word toggle: letters, digits, _ and $. */
export function wordChar(ch: string | undefined): boolean {
  return ch !== undefined && /[\p{L}\p{N}_$]/u.test(ch);
}

/** Whether the code point immediately before *index* is a word character. */
export function wordCharBefore(text: string, index: number): boolean {
  if (index <= 0) return false;
  const low = text.charCodeAt(index - 1);
  if (low >= 0xdc00 && low <= 0xdfff && index >= 2) {
    const high = text.charCodeAt(index - 2);
    if (high >= 0xd800 && high <= 0xdbff) return wordChar(text.slice(index - 2, index));
  }
  return wordChar(text[index - 1]);
}

/** Whether the code point starting at *index* is a word character. */
export function wordCharAfter(text: string, index: number): boolean {
  if (index >= text.length) return false;
  const cp = text.codePointAt(index);
  return cp !== undefined && wordChar(String.fromCodePoint(cp));
}

export function compileFindRegex(query: string, options: FindOptions): { re: RegExp | null; error: string } {
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
  // User regex searches belong only in the worker, where they can time out.
  if (!query || options.regex) return { matches: [], error: "", capped: false };
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

export type ReplaceChange = { from: number; to: number; insert: string };
export type ReplacePlan = {
  changes: ReplaceChange[];
  anchor: number;
  undoAnchor: number;
  selection: number;
};

/** Plan CodeMirror changes and post-replace anchors for one match or all matches. */
export function planReplace(
  text: string,
  matches: readonly Range[],
  index: number,
  replaceWith: string,
  all: boolean,
): ReplacePlan | null {
  if (!matches.length) return null;
  if (all) {
    const changes: ReplaceChange[] = [];
    let prevEnd = 0;
    for (const m of matches) {
      if (m.start < prevEnd || m.end < m.start || m.end > text.length) return null;
      changes.push({ from: m.start, to: m.end, insert: replaceWith });
      prevEnd = m.end;
    }
    const first = matches[0];
    return {
      changes,
      anchor: first.start,
      undoAnchor: first.start,
      selection: first.start + replaceWith.length,
    };
  }
  const clamped = Math.max(0, Math.min(index, matches.length - 1));
  const target = matches[clamped];
  if (target.start < 0 || target.end < target.start || target.end > text.length) return null;
  const after = target.start + replaceWith.length;
  return {
    changes: [{ from: target.start, to: target.end, insert: replaceWith }],
    anchor: after,
    undoAnchor: target.start,
    selection: after,
  };
}

export type GoToLineTarget = { line: number; from: number };

/** Clamp a 1-based line input into [1, lineCount], or null when blank or non-numeric. */
export function clampGoToLine(raw: string | number, lineCount: number): number | null {
  const maxLine = Math.max(1, Math.trunc(lineCount));
  const text = typeof raw === "number" ? String(raw) : raw.trim();
  if (!text) return null;
  const parsed = typeof raw === "number" ? raw : Number(text);
  if (Number.isNaN(parsed)) return null;
  if (parsed <= 1) return 1;
  if (parsed >= maxLine) return maxLine;
  return Math.trunc(parsed);
}

/** Resolve a 1-based line input to the start offset of the clamped line in a CodeMirror Text. */
export function resolveGoToLine(
  doc: { readonly lines: number; line(n: number): { readonly from: number } },
  raw: string | number,
): GoToLineTarget | null {
  const line = clampGoToLine(raw, doc.lines);
  if (line === null) return null;
  return { line, from: doc.line(line).from };
}
