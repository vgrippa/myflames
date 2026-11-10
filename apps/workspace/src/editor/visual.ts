import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { highlightSelectionMatches } from "@codemirror/search";
import { highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars } from "@codemirror/view";
import { HIGHLIGHT_LIMIT } from "../sqlHighlight";
import type { EditorEnv, EditorFeatureSettings } from "./types";

const BIDI_AND_CONTROL_CHARS = /[\u200c-\u200f\u202a-\u202e\u2066-\u2069]/;

export function visualExtension(_settings: EditorFeatureSettings, _env: { current: EditorEnv }, length = 0): Extension {
  const specialChars = highlightSpecialChars({ addSpecialChars: BIDI_AND_CONTROL_CHARS });
  if (length > HIGHLIGHT_LIMIT) return specialChars;
  const small: Extension = [
    highlightActiveLine(),
    highlightActiveLineGutter(),
    highlightSelectionMatches({ highlightWordAroundCursor: true, wholeWords: true, minSelectionLength: 2 }),
  ];
  const slot = new Compartment();
  return [
    slot.of(small),
    specialChars,
    EditorState.transactionExtender.of(tr => {
      if ((tr.startState.doc.length <= HIGHLIGHT_LIMIT) === (tr.newDoc.length <= HIGHLIGHT_LIMIT)) return null;
      return { effects: slot.reconfigure(tr.newDoc.length <= HIGHLIGHT_LIMIT ? small : []) };
    }),
  ];
}
