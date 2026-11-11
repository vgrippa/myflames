// The Query editor's persisted settings (/api/settings/editor,
// workspace_store.py EDITOR_SETTING_DEFAULTS), shared by the Settings popover,
// the command palette and the editor through one small store: the defaults
// apply until the first read lands, writes go out one at a time in the order
// they were made so the stored value is always the last choice, and a failed
// latest write reads the stored value again. No CodeMirror import: the editor
// subscribes through subscribeEditorSettings.
import { useEffect, useState } from "react";
import { api } from "./api";

export type EditorSettingKey =
  | "multi_cursor"
  | "completion_auto"
  | "close_brackets"
  | "soft_wrap"
  | "problem_warnings"
  | "hover_info";
export type EditorSettings = Record<EditorSettingKey, boolean>;
export const EDITOR_SETTING_DEFAULTS: EditorSettings = {
  multi_cursor: true,
  completion_auto: true,
  close_brackets: true,
  soft_wrap: false,
  problem_warnings: true,
  hover_info: true,
};
export const EDITOR_SETTING_KEYS: readonly EditorSettingKey[] = Object.keys(EDITOR_SETTING_DEFAULTS) as EditorSettingKey[];
export const EDITOR_SETTING_LABELS: Record<EditorSettingKey, string> = {
  multi_cursor: "Multiple cursors",
  completion_auto: "Suggestions while typing",
  close_brackets: "Close brackets and quotes",
  soft_wrap: "Wrap long lines",
  problem_warnings: "Problem warnings",
  hover_info: "Details on hover",
};

type EditorSettingsReply = { settings: EditorSettings };
const PATH = "settings/editor";
/** The server's answer over the defaults (a missing key keeps its default). */
const merge = (reply: EditorSettingsReply): EditorSettings => ({ ...EDITOR_SETTING_DEFAULTS, ...reply.settings });

let settings: EditorSettings = { ...EDITOR_SETTING_DEFAULTS };
let loaded = false;
let loading: Promise<EditorSettings> | null = null;
const listeners = new Set<(s: EditorSettings) => void>();
function publish(next: EditorSettings) {
  settings = next;
  loaded = true;
  listeners.forEach((listener) => listener(next));
}
/** The settings as known now: the defaults until the first read lands. */
export function currentEditorSettings(): EditorSettings {
  return settings;
}
/** True once a read or write answer landed; false while only the defaults show. */
export function editorSettingsLoaded(): boolean {
  return loaded;
}
let writes = 0;
/** One read, shared by every caller; a change made while it is on its way wins over it. */
export function loadEditorSettings(): Promise<EditorSettings> {
  if (!loading) {
    const before = writes;
    loading = api<EditorSettingsReply>(PATH, {}).then(
      (reply) => {
        if (writes === before) publish(merge(reply));
        return settings;
      },
      (error) => {
        loading = null;
        throw error;
      },
    );
  }
  return loading;
}
// Writes go out one after another, in the order they were made, so the stored
// value is always the last choice, even across popover openings and palette runs.
let queue: Promise<unknown> = Promise.resolve();
/** Turn one setting on or off (stored in the workspace store). Only the latest
 * write's answer is published; when it fails, the stored value is read again. */
export function setEditorSetting(key: EditorSettingKey, on: boolean): Promise<void> {
  const mine = ++writes;
  const sent = queue.then(() => api<EditorSettingsReply>(PATH, { set: { [key]: on } }));
  queue = sent.catch(() => undefined);
  return sent.then(
    (reply) => {
      if (mine === writes) {
        publish(merge(reply));
        loading = Promise.resolve(settings);
      }
    },
    (error) => {
      if (mine === writes) {
        loading = null;
        loadEditorSettings().catch(() => { /* the rows keep what they show */ });
      }
      throw error;
    },
  );
}
/** For non-React code (the editor): called with the settings on every change. */
export function subscribeEditorSettings(fn: (s: EditorSettings) => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}
/** The React side (Settings, the command palette): loads on first use. */
export function useEditorSettings(): EditorSettings {
  const [value, setValue] = useState(settings);
  useEffect(() => {
    listeners.add(setValue);
    setValue(settings);
    loadEditorSettings().catch(() => { /* the defaults keep showing */ });
    return () => { listeners.delete(setValue); };
  }, []);
  return value;
}
