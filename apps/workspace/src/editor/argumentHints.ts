import type { Extension } from "@codemirror/state";
import type { EditorEnv, EditorFeatureSettings } from "./types";

export function argumentHintsExtension(_settings: EditorFeatureSettings, _env: { current: EditorEnv }): Extension {
  return [];
}
