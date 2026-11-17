import { EditorSelection } from "@codemirror/state";
import type { Command, KeyBinding } from "@codemirror/view";
import {
  addCursorAbove, addCursorBelow, copyLineDown, copyLineUp, deleteLine, historyKeymap,
  indentLess, indentMore, insertNewlineKeepIndent, moveLineDown, moveLineUp, standardKeymap, toggleTabFocusMode,
} from "@codemirror/commands";
import { acceptCompletion, closeCompletion, completionKeymap, startCompletion } from "@codemirror/autocomplete";
import { closeLintPanel } from "@codemirror/lint";
import { MAC } from "../platform";
import { findHooks } from "./FindBar";
import { nextProblemCmd, previousProblemCmd, selectAllOccurrencesCmd, selectNextOccurrenceCmd, showProblemsCmd, toggleSqlComment } from "./commands";
import type { EditorFeatureSettings } from "./types";

export const EDITOR_SHORTCUTS: { id: string; mac: string; other: string; label: string; setting?: keyof EditorFeatureSettings; aliasFor?: string }[] = [
  { id: "escape", mac: "Escape", other: "Escape", label: "Close suggestions, extra cursors, Find or Problems" },
  { id: "indent", mac: "Tab", other: "Tab", label: "Accept suggestion or indent" },
  { id: "outdent", mac: "Shift-Tab", other: "Shift-Tab", label: "Accept suggestion or unindent" },
  { id: "complete", mac: "Ctrl-Space", other: "Ctrl-Space", label: "Show suggestions" },
  { id: "completeAgain", mac: "Shift-Ctrl-Space", other: "Shift-Ctrl-Space", label: "Show suggestions", aliasFor: "complete" },
  { id: "comment", mac: "Mod-/", other: "Mod-/", label: "Toggle SQL comment" },
  { id: "moveUp", mac: "Alt-ArrowUp", other: "Alt-ArrowUp", label: "Move line up" },
  { id: "moveDown", mac: "Alt-ArrowDown", other: "Alt-ArrowDown", label: "Move line down" },
  { id: "copyUp", mac: "Shift-Alt-ArrowUp", other: "Shift-Alt-ArrowUp", label: "Copy line up" },
  { id: "copyDown", mac: "Shift-Alt-ArrowDown", other: "Shift-Alt-ArrowDown", label: "Copy line down" },
  { id: "deleteLine", mac: "Shift-Mod-k", other: "Shift-Mod-k", label: "Delete line" },
  { id: "nextOccurrence", mac: "Mod-d", other: "Mod-d", label: "Select next occurrence", setting: "multi_cursor" },
  { id: "allOccurrences", mac: "Shift-Mod-l", other: "Shift-Mod-l", label: "Select all occurrences", setting: "multi_cursor" },
  { id: "cursorAbove", mac: "Mod-Alt-ArrowUp", other: "Mod-Alt-ArrowUp", label: "Add cursor above", setting: "multi_cursor" },
  { id: "cursorBelow", mac: "Mod-Alt-ArrowDown", other: "Mod-Alt-ArrowDown", label: "Add cursor below", setting: "multi_cursor" },
  { id: "goToLine", mac: "Ctrl-g", other: "", label: "Go to line" },
  { id: "find", mac: "Mod-f", other: "Mod-f", label: "Find" },
  { id: "replace", mac: "Mod-Alt-f", other: "Ctrl-h", label: "Find and replace" },
  { id: "replaceAlt", mac: "", other: "Mod-Alt-f", label: "Find and replace", aliasFor: "replace" },
  { id: "nextMatch", mac: "Mod-g", other: "Mod-g", label: MAC ? "Next match" : "Next match (Ctrl+G opens Go to line when Find is closed)" },
  { id: "previousMatch", mac: "Shift-Mod-g", other: "Shift-Mod-g", label: "Previous match" },
  { id: "problems", mac: "Shift-Mod-m", other: "Shift-Mod-m", label: "Show problems", setting: "problem_warnings" },
  { id: "nextProblem", mac: "F8", other: "F8", label: "Next problem", setting: "problem_warnings" },
  { id: "previousProblem", mac: "Shift-F8", other: "Shift-F8", label: "Previous problem", setting: "problem_warnings" },
  { id: "tabFocus", mac: "Shift-Alt-m", other: "Ctrl-m", label: "Toggle Tab moving focus" },
];

/** One visible action per command, in shortcut-table order. Aliases share a
 * menu item; Escape and indentation bindings remain keyboard plumbing. */
export const EDITOR_ACTIONS = EDITOR_SHORTCUTS.filter(row =>
  !row.aliasFor && !["escape", "indent", "outdent"].includes(row.id));

/** All bindings for an action on this platform, including alternate keys. */
export function editorActionShortcuts(id: string, mac = MAC): string[] {
  const keys = EDITOR_SHORTCUTS.filter(row => row.id === id || row.aliasFor === id)
    .map(row => mac ? row.mac : row.other).filter(Boolean);
  // Off Mac, Ctrl+G opens Go to line while Find is closed (nextMatch below).
  if (id === "goToLine" && !mac) keys.push(EDITOR_SHORTCUTS.find(row => row.id === "nextMatch")!.other);
  return [...new Set(keys)];
}

export const escapeEditor: Command = view => {
  if (closeCompletion(view)) return true;
  if (view.state.selection.ranges.length > 1) {
    view.dispatch({ selection: EditorSelection.create([view.state.selection.main]), userEvent: "select" });
    return true;
  }
  return findHooks.close(view) || closeLintPanel(view);
};

export type EditorKeymapCommands = Partial<Record<string, Command>> & { findIsOpen?: (view: Parameters<Command>[0]) => boolean };

/** The toolbar and keys dispatch the same commands and respect the same settings.
 * A Map also makes unknown handle IDs (including object-property names) inert. */
export function editorCommands(settings: EditorFeatureSettings, commands: EditorKeymapCommands = {}): ReadonlyMap<string, Command> {
  const run: Record<string, Command> = {
    escape: escapeEditor,
    indent: view => acceptCompletion(view) || indentMore(view),
    outdent: view => acceptCompletion(view) || indentLess(view),
    tabFocus: toggleTabFocusMode,
    complete: startCompletion,
    completeAgain: startCompletion,
    comment: toggleSqlComment,
    moveUp: moveLineUp, moveDown: moveLineDown, copyUp: copyLineUp, copyDown: copyLineDown, deleteLine,
    nextOccurrence: selectNextOccurrenceCmd, allOccurrences: selectAllOccurrencesCmd,
    cursorAbove: addCursorAbove, cursorBelow: addCursorBelow,
    goToLine: view => findHooks.openGoToLine(view),
    find: view => findHooks.openFind(view, "find"),
    replace: view => findHooks.openFind(view, "replace"),
    replaceAlt: view => (commands.replace || run.replace)(view),
    nextMatch: view => !MAC && !(commands.findIsOpen || findHooks.isOpen)(view)
      ? (commands.goToLine || run.goToLine)(view) : findHooks.next(view),
    previousMatch: view => findHooks.previous(view),
    problems: showProblemsCmd, nextProblem: nextProblemCmd, previousProblem: previousProblemCmd,
  };
  return new Map(EDITOR_SHORTCUTS.filter(row => !row.setting || settings[row.setting])
    .map(row => [row.id, commands[row.id] || run[row.id]]));
}

/** Custom bindings precede completion navigation, editing and undo defaults. */
export function editorKeymap(settings: EditorFeatureSettings, commands: EditorKeymapCommands = {}): KeyBinding[] {
  const run = editorCommands(settings, commands);
  const bindings = EDITOR_SHORTCUTS.filter(row => run.has(row.id)).map(row => ({
    key: row.other || undefined,
    mac: row.mac || undefined,
    // A platform-only shortcut must not fall back to the other platform's key.
    ...(row.mac === "" ? { mac: undefined, key: MAC ? undefined : row.other } : {}),
    run: run.get(row.id)!,
    ...(row.id === "deleteLine" ? { stopPropagation: true, preventDefault: true } : {}),
  }));
  // standardKeymap contains none of the defaultKeymap's Run, history-navigation,
  // syntax-selection, comment or Escape bindings that conflict with the shell.
  const editing = standardKeymap.map(binding => binding.key === "Enter"
    ? { ...binding, run: insertNewlineKeepIndent, shift: insertNewlineKeepIndent }
    : binding.mac === "Ctrl-k" ? { ...binding, stopPropagation: true, preventDefault: true } : binding);
  const completionNavigation = completionKeymap.filter(binding =>
    ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Enter"].includes(binding.key || ""));
  return [...bindings, ...completionNavigation, ...editing, ...historyKeymap];
}
