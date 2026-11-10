import type { ChangeSpec, Line, StateCommand } from "@codemirror/state";
import { selectNextOccurrence, selectSelectionMatches } from "@codemirror/search";

function commentMarkerLength(text: string, indent: number): number {
  const rest = text.slice(indent);
  if (rest.startsWith("-- ")) return 3;
  if (rest === "--" || rest.startsWith("--\t")) return 2;
  if (rest.startsWith("#")) return 1;
  return 0;
}

export const toggleSqlComment: StateCommand = ({ state, dispatch }) => {
  if (state.readOnly) return false;
  const lines: { line: Line; indent: number; blank: boolean; marker: number }[] = [];
  let prevLine = -1;
  for (const { from, to } of state.selection.ranges) {
    for (let pos = from; pos <= to;) {
      const line = state.doc.lineAt(pos);
      if (line.number > prevLine && (from === to || to > line.from)) {
        prevLine = line.number;
        const indent = /^[ \t]*/.exec(line.text)![0].length;
        const blank = indent === line.text.length;
        const marker = blank ? 0 : commentMarkerLength(line.text, indent);
        lines.push({ line, indent, blank, marker });
      }
      pos = line.to + 1;
    }
  }
  if (!lines.length) return false;
  const nonBlank = lines.filter(item => !item.blank);
  const changes: ChangeSpec[] = [];
  if (nonBlank.length > 0 && nonBlank.every(item => item.marker > 0)) {
    for (const item of nonBlank) {
      const start = item.line.from + item.indent;
      changes.push({ from: start, to: start + item.marker, insert: "" });
    }
  } else {
    const targets = nonBlank.length > 0 ? nonBlank : lines;
    const minIndent = Math.min(...targets.map(item => item.indent));
    for (const item of targets) {
      changes.push({ from: item.line.from + minIndent, insert: "-- " });
    }
  }
  const changeSet = state.changes(changes);
  dispatch(state.update({
    changes: changeSet,
    selection: state.selection.map(changeSet, 1),
    userEvent: "input",
  }));
  return true;
};

export const selectNextOccurrenceCmd: StateCommand = selectNextOccurrence;
export const selectAllOccurrencesCmd: StateCommand = selectSelectionMatches;
export const showProblemsCmd: StateCommand = () => false;
export const nextProblemCmd: StateCommand = () => false;
export const previousProblemCmd: StateCommand = () => false;
