import type { CompletionSource } from "@codemirror/autocomplete";
import type { Extension } from "@codemirror/state";
import type { EditorEnv, EditorFeatureSettings } from "./types";

export function completionExtension(_settings: EditorFeatureSettings, _env: { current: EditorEnv }): Extension {
  return [];
}

export function completionSources(_env: { current: EditorEnv }): CompletionSource[] {
  return [];
}
