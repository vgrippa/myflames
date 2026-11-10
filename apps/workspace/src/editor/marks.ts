import { RangeSet, StateEffect, StateField, type ChangeDesc, type EditorState, type Extension } from "@codemirror/state";
import { Decoration, EditorView, GutterMarker, gutterLineClass, type DecorationSet } from "@codemirror/view";
import { bracketMatching, syntaxTree } from "@codemirror/language";
import { HIGHLIGHT_LIMIT } from "../sqlHighlight";
import { tokenInfo } from "./sqlLanguage";
import { clampRange, type Range } from "./types";

export const FLASH_MS = 1600;
function mapRange(value: Range | null, changes: ChangeDesc): Range | null {
  if (!value) return null;
  const start = changes.mapPos(value.start, 1), end = changes.mapPos(value.end, -1);
  return end > start ? { start, end } : null;
}
export const setStatement = StateEffect.define<Range | null>({ map: mapRange });
export const setFlash = StateEffect.define<Range | null>({ map: mapRange });
export const setError = StateEffect.define<Range | null>({ map: mapRange });

class LineClass extends GutterMarker {
  constructor(readonly elementClass: string) { super(); }
  eq(other: LineClass) { return other.elementClass === this.elementClass; }
}
const currentLine = new LineClass("is-current");
const errorLine = new LineClass("is-error");
const bothLine = new LineClass("is-current is-error");
type Marks = {
  statement: Range | null;
  flash: Range | null;
  error: Range | null;
  decorations: DecorationSet;
  gutters: RangeSet<GutterMarker>;
};

function buildMarks(state: EditorState, ranges: Pick<Marks, "statement" | "flash" | "error">): Marks {
  const decorations: ReturnType<Decoration["range"]>[] = [];
  const gutters: ReturnType<GutterMarker["range"]>[] = [];
  const bounded = (r: Range | null) => r ? clampRange(r.start, r.end, state.doc.length) : null;
  const statement = bounded(ranges.statement), flash = bounded(ranges.flash), error = bounded(ranges.error);
  if (state.doc.length <= HIGHLIGHT_LIMIT) {
    for (const [range, cls] of [[statement, "sql-current"], [flash, "sql-flash"], [error, "sql-error-mark"]] as const) {
      if (range && range.end > range.start) decorations.push(Decoration.mark({ class: cls }).range(range.start, range.end));
    }
    const errorAt = error && error.end > error.start ? state.doc.lineAt(error.start).from : -1;
    if (statement && statement.end > statement.start) {
      const first = state.doc.lineAt(statement.start).number;
      // Preserve the textarea gutter's inclusive end-line convention.
      const last = state.doc.lineAt(statement.end).number;
      for (let n = first; n <= last; n++) {
        const from = state.doc.line(n).from;
        gutters.push((from === errorAt ? bothLine : currentLine).range(from));
      }
    }
    if (errorAt >= 0 && !gutters.some(mark => mark.from === errorAt)) gutters.push(errorLine.range(errorAt));
  }
  return { statement, flash, error, decorations: Decoration.set(decorations, true), gutters: RangeSet.of(gutters, true) };
}

export const markState = StateField.define<Marks>({
  create: state => buildMarks(state, { statement: null, flash: null, error: null }),
  update(value, tr) {
    if (!tr.docChanged && !tr.effects.some(effect => effect.is(setStatement) || effect.is(setFlash) || effect.is(setError))) return value;
    const ranges = {
      statement: mapRange(value.statement, tr.changes),
      flash: mapRange(value.flash, tr.changes),
      error: mapRange(value.error, tr.changes),
    };
    for (const effect of tr.effects) {
      if (effect.is(setStatement)) ranges.statement = effect.value;
      if (effect.is(setFlash)) ranges.flash = effect.value;
      if (effect.is(setError)) ranges.error = effect.value;
    }
    return buildMarks(tr.state, ranges);
  },
  provide: field => [EditorView.decorations.from(field, value => value.decorations), gutterLineClass.from(field, value => value.gutters)],
});

/** Inspect only the nodes touching each cursor, never tokenize on selection. */
export function quotePairDecorations(state: EditorState): DecorationSet {
  if (state.doc.length > HIGHLIGHT_LIMIT) return Decoration.none;
  const ends = new Set<number>();
  const tree = syntaxTree(state);
  for (const range of state.selection.ranges) {
    if (!range.empty) continue;
    for (const at of [range.head - 1, range.head]) {
      if (at < 0 || at >= state.doc.length) continue;
      const node = tree.resolveInner(at, 1), info = node.type.prop(tokenInfo);
      if (info?.closed && (info.kind === "string" || info.kind === "ident") && node.to - node.from >= 2 &&
          (at === node.from || at === node.to - 1)) {
        ends.add(node.from);
        ends.add(node.to - 1);
        break;
      }
    }
  }
  return Decoration.set([...ends].map(at => Decoration.mark({ class: "sql-match" }).range(at, at + 1)), true);
}
export const quoteState = StateField.define<DecorationSet>({
  create: quotePairDecorations,
  update: (value, tr) => tr.docChanged || tr.selection || syntaxTree(tr.startState) !== syntaxTree(tr.state)
    ? quotePairDecorations(tr.state) : value,
  provide: field => EditorView.decorations.from(field),
});

export const matchingBrackets = bracketMatching({
  maxScanDistance: HIGHLIGHT_LIMIT,
  renderMatch(match, state) {
    if (state.doc.length > HIGHLIGHT_LIMIT || syntaxTree(state).resolveInner(match.start.from, 1).type.prop(tokenInfo)?.kind !== "bracket") return [];
    // CodeMirror's plain scan compares node types, skipping strings and comments.
    const mark = Decoration.mark({ class: match.matched ? "sql-match" : "sql-unmatched" });
    return [mark.range(match.start.from, match.start.to), ...(match.end ? [mark.range(match.end.from, match.end.to)] : [])];
  },
});

export function sqlMarks(): Extension {
  return [markState, quoteState];
}
