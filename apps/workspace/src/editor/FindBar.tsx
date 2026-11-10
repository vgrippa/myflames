import type { EditorView } from "@codemirror/view";

export type FindBarProps = { view: EditorView | null };
export function FindBar(_props: FindBarProps) {
  return null;
}

// The find worker can track per-view state; callers never retain a document's hooks.
export const findHooks = {
  openFind(_view: EditorView, _mode: "find" | "replace"): boolean { return false; },
  openGoToLine(_view: EditorView): boolean { return false; },
  next(_view: EditorView): boolean { return false; },
  previous(_view: EditorView): boolean { return false; },
  isOpen(_view: EditorView): boolean { return false; },
};
