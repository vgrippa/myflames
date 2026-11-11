import { Compartment, EditorState, StateField, type Extension } from "@codemirror/state";
import { highlightSelectionMatches } from "@codemirror/search";
import { Decoration, EditorView, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars } from "@codemirror/view";
import { HIGHLIGHT_LIMIT } from "../sqlHighlight";
import type { EditorEnv, EditorFeatureSettings } from "./types";

const BIDI_AND_CONTROL_CHARS = /[\u200c-\u200f\u202a-\u202e\u2066-\u2069]/;

/** This follows selection ranges only, so even large documents need no scan. */
export const selectedText = StateField.define({
  create: state => Decoration.set(state.selection.ranges.filter(range => !range.empty)
    .map(range => Decoration.mark({ class: "cm-selectedText" }).range(range.from, range.to))),
  update(value, tr) {
    return tr.docChanged || tr.selection ? Decoration.set(tr.state.selection.ranges.filter(range => !range.empty)
      .map(range => Decoration.mark({ class: "cm-selectedText" }).range(range.from, range.to))) : value;
  },
  provide: field => EditorView.outerDecorations.from(field),
});

export function visualExtension(_settings: EditorFeatureSettings, _env: { current: EditorEnv }, length = 0): Extension {
  const specialChars = highlightSpecialChars({ addSpecialChars: BIDI_AND_CONTROL_CHARS });
  if (length > HIGHLIGHT_LIMIT) return [specialChars, selectedText];
  const small: Extension = [
    highlightActiveLine(),
    highlightActiveLineGutter(),
    highlightSelectionMatches({ highlightWordAroundCursor: true, wholeWords: true, minSelectionLength: 2 }),
  ];
  const slot = new Compartment();
  return [
    slot.of(small),
    specialChars, selectedText,
    EditorState.transactionExtender.of(tr => {
      if ((tr.startState.doc.length <= HIGHLIGHT_LIMIT) === (tr.newDoc.length <= HIGHLIGHT_LIMIT)) return null;
      return { effects: slot.reconfigure(tr.newDoc.length <= HIGHLIGHT_LIMIT ? small : []) };
    }),
  ];
}
