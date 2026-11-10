import { StateEffect, StateField, type ChangeDesc, type Extension } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, type DecorationSet, type ViewUpdate } from "@codemirror/view";
import { FIND_MARK_LIMIT, type Range } from "./find";

// Security invariant: fixed class names only, never attributes derived from text or query.
const MATCH_MARK = Decoration.mark({ class: "sql-find-mark" });
const CURRENT_MARK = Decoration.mark({ class: "sql-find-mark sql-find-current" });

export type FindMatchesPayload = { matches: readonly Range[]; current: number };

export type FindMarkValue = {
  matches: readonly Range[];
  current: number;
  decorations: DecorationSet;
};

export const setFindMatches = StateEffect.define<FindMatchesPayload>();
export const setFindCurrent = StateEffect.define<number>();
export const clearFindMarks = StateEffect.define<null>();

const EMPTY_FIND_MARKS: FindMarkValue = {
  matches: [],
  current: 0,
  decorations: Decoration.none,
};

function buildFindMarks(docLength: number, matches: readonly Range[], current: number): FindMarkValue {
  if (!matches.length) return EMPTY_FIND_MARKS;
  const idx = Math.max(0, Math.min(current, matches.length - 1));
  const ranges: ReturnType<Decoration["range"]>[] = [];
  if (idx < FIND_MARK_LIMIT) {
    const limit = Math.min(matches.length, FIND_MARK_LIMIT);
    for (let i = 0; i < limit; i++) {
      const m = matches[i];
      if (m.start >= 0 && m.end > m.start && m.end <= docLength) {
        ranges.push((i === idx ? CURRENT_MARK : MATCH_MARK).range(m.start, m.end));
      }
    }
  } else {
    const headLimit = Math.max(0, FIND_MARK_LIMIT - 1);
    for (let i = 0; i < headLimit; i++) {
      const m = matches[i];
      if (m.start >= 0 && m.end > m.start && m.end <= docLength) {
        ranges.push(MATCH_MARK.range(m.start, m.end));
      }
    }
    const active = matches[idx];
    if (active && active.start >= 0 && active.end > active.start && active.end <= docLength) {
      ranges.push(CURRENT_MARK.range(active.start, active.end));
    }
  }
  return {
    matches,
    current: idx,
    decorations: Decoration.set(ranges, true),
  };
}

function mapFindMatches(value: FindMarkValue, changes: ChangeDesc, newDocLength: number): FindMarkValue {
  if (!value.matches.length) return EMPTY_FIND_MARKS;
  const edits: { fromA: number; toA: number; toB: number }[] = [];
  changes.iterChangedRanges((fromA, toA, _fromB, toB) => {
    edits.push({ fromA, toA, toB });
  });
  if (!edits.length) return value;

  const surviving: Range[] = [];
  let e = 0;
  let shift = 0;
  let nextCurrent = 0;
  for (let i = 0; i < value.matches.length; i++) {
    const m = value.matches[i];
    while (e < edits.length && edits[e].toA <= m.start) {
      shift = edits[e].toB - edits[e].toA;
      e++;
    }
    // Drop matches whose characters were deleted, replaced, or split by an interior edit.
    if (e < edits.length && edits[e].toA > m.start && edits[e].fromA < m.end) {
      continue;
    }
    const start = m.start + shift;
    const end = m.end + shift;
    if (start >= 0 && end > start && end <= newDocLength) {
      if (i < value.current) nextCurrent++;
      surviving.push({ start, end });
    }
  }
  if (!surviving.length) return EMPTY_FIND_MARKS;
  return buildFindMarks(newDocLength, surviving, Math.min(nextCurrent, surviving.length - 1));
}

export const findMarkState = StateField.define<FindMarkValue>({
  create: () => EMPTY_FIND_MARKS,
  update(value, tr) {
    const hasEffect = tr.effects.some(
      (effect) => effect.is(setFindMatches) || effect.is(setFindCurrent) || effect.is(clearFindMarks),
    );
    if (!tr.docChanged && !hasEffect) return value;

    let next = tr.docChanged ? mapFindMatches(value, tr.changes, tr.newDoc.length) : value;
    for (const effect of tr.effects) {
      if (effect.is(clearFindMarks)) {
        next = EMPTY_FIND_MARKS;
      } else if (effect.is(setFindMatches)) {
        next = buildFindMarks(tr.newDoc.length, effect.value.matches, effect.value.current);
      } else if (effect.is(setFindCurrent)) {
        next = buildFindMarks(tr.newDoc.length, next.matches, effect.value);
      }
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field, (value) => value.decorations),
});

export type FindViewEvent =
  | { type: "docChanged"; isUndo: boolean }
  | { type: "stateReplaced" };

const viewListeners = new WeakMap<EditorView, (event: FindViewEvent) => void>();
const viewOpenPredicates = new WeakMap<EditorView, () => boolean>();

export function attachFindViewListener(
  view: EditorView,
  listener: (event: FindViewEvent) => void,
  isOpen: () => boolean,
): () => void {
  viewListeners.set(view, listener);
  viewOpenPredicates.set(view, isOpen);
  return () => {
    if (viewListeners.get(view) === listener) viewListeners.delete(view);
    if (viewOpenPredicates.get(view) === isOpen) viewOpenPredicates.delete(view);
  };
}

const findViewPlugin = ViewPlugin.fromClass(
  class {
    private alive = true;
    private readonly view: EditorView;

    constructor(view: EditorView) {
      this.view = view;
      const hadInitialMarks = (view.state.field(findMarkState, false)?.matches.length ?? 0) > 0;
      if (hadInitialMarks && viewOpenPredicates.has(view)) {
        queueMicrotask(() => {
          if (!this.alive) return;
          const open = viewOpenPredicates.get(this.view)?.() ?? false;
          if (!open && (this.view.state.field(findMarkState, false)?.matches.length ?? 0) > 0) {
            this.view.dispatch({ effects: clearFindMarks.of(null) });
          }
        });
      }
    }

    update(update: ViewUpdate) {
      if (update.docChanged) {
        const isUndo = update.transactions.some((tr) => tr.isUserEvent("undo"));
        viewListeners.get(update.view)?.({ type: "docChanged", isUndo });
      }
    }

    destroy() {
      this.alive = false;
      viewListeners.get(this.view)?.({ type: "stateReplaced" });
    }
  },
);

export function findExtension(): Extension {
  // The old CSS targets .sql-layer, which the CodeMirror editor does not use.
  // Keep mark styling local to this extension, including forced-colors support.
  return [findMarkState, findViewPlugin, EditorView.baseTheme({
    ".sql-find-mark": { backgroundColor: "var(--accent-soft)", borderRadius: "2px" },
    ".sql-find-current": { boxShadow: "0 0 0 1px var(--accent)" },
    "@media (forced-colors: active)": {
      ".sql-find-mark": { backgroundColor: "transparent", boxShadow: "0 0 0 1px Highlight" },
      ".sql-find-current": { backgroundColor: "Highlight", boxShadow: "0 0 0 1px Highlight" },
    },
  })];
}
