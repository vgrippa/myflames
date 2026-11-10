import { EditorSelection } from "@codemirror/state";
import type { EditorView } from "@codemirror/view";
import { clampRange } from "./types";

/** Textarea-shaped properties exist only for existing browser test automation. */
export function attachAutomation(view: EditorView): () => void {
  const element = view.contentDOM;
  const setSelectionRange = (a: number, b: number) => {
    const { start, end } = clampRange(a, b, view.state.doc.length);
    view.dispatch({ selection: EditorSelection.single(start, end), userEvent: "select" });
  };
  Object.defineProperties(element, {
    value: { configurable: true, get: () => view.state.doc.toString() },
    selectionStart: { configurable: true, get: () => view.state.selection.main.from },
    selectionEnd: { configurable: true, get: () => view.state.selection.main.to },
    setSelectionRange: { configurable: true, value: setSelectionRange },
    select: { configurable: true, value: () => setSelectionRange(0, view.state.doc.length) },
  });
  return () => {
    for (const key of ["value", "selectionStart", "selectionEnd", "setSelectionRange", "select"]) Reflect.deleteProperty(element, key);
  };
}
