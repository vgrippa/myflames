import type { Extension } from "@codemirror/state";
import type { EditorEnv, EditorFeatureSettings } from "./types";

export function visualExtension(_settings: EditorFeatureSettings, _env: { current: EditorEnv }): Extension {
  return [];
}
