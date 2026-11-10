import type { EditorState } from "@codemirror/state";

/** All editor offsets are UTF-16 code units, like a textarea. */
export type Range = { start: number; end: number };
export type EditorFeatureSettings = {
  multi_cursor: boolean;
  completion_auto: boolean;
  close_brackets: boolean;
  soft_wrap: boolean;
  problem_warnings: boolean;
  hover_info: boolean;
};
export type EditorEnv = {
  connection?: unknown;
  engine?: "mysql" | "mariadb";
  serverVersion?: string;
  backslashEscapes: boolean;
  docId: string;
};
export type SqlEditorHandle = {
  readonly element: HTMLElement | null;
  getValue(): string;
  getSelection(): Range;
  getSelections(): Range[];
  setSelection(start: number, end?: number, o?: { focus?: boolean; scroll?: "nearest" | "center" | false }): void;
  replaceRange(from: number, to: number, text: string, o?: { select?: Range; focus?: boolean }): boolean;
  insertAtCursor(text: string): void;
  focus(): void;
  isShown(): boolean;
  revealInPage(): void;
  openFind(mode: "find" | "replace"): void;
  openGoToLine(): void;
};
export type SqlEditorProps = {
  docId: string;
  docIds: string[];
  value: string;
  onChange(value: string, selection: Range): void;
  onSelectionChange?(selection: Range, count: number): void;
  onFocus?(): void;
  onKeyDown?(e: KeyboardEvent): void;
  initialSelection?: Range;
  statement?: Range | null;
  flash?: Range | null;
  error?: Range | null;
  describedBy?: string;
  height?: number;
  placeholder?: string;
  settings: EditorFeatureSettings;
  backslashEscapes?: boolean;
  completionConnection?: unknown;
  engine?: "mysql" | "mariadb";
  serverVersion?: string;
};

export function mainSelection(state: EditorState): Range {
  const { from, to } = state.selection.main;
  return { start: from, end: to };
}

export function clampOffset(value: number, length: number): number {
  return Math.min(length, Math.max(0, Number.isNaN(value) ? 0 : Math.trunc(value)));
}

/** Reversed bounds collapse at the end, as setSelectionRange does. */
export function clampRange(start: number, end: number, length: number): Range {
  const to = clampOffset(end, length);
  return { start: Math.min(clampOffset(start, length), to), end: to };
}
