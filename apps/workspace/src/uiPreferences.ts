// Shell choices live in workspace.sqlite3 because the desktop renderer uses
// a fresh in-memory partition on each launch. No editor or lazy view imports.
import { useEffect, useState } from "react";
import { api } from "./api";
import type { ThemeId } from "./themes";

export const SIDEBAR_MIN = 208;
export const SIDEBAR_MAX = 320;
export const SIDEBAR_DEFAULT = 208;
export const clampSidebarWidth = (value: number) => Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(value)));
export const INSPECTOR_MIN = 280;
export const INSPECTOR_MAX = 560;
export const INSPECTOR_DEFAULT = 340;
export type ExplainFormat = "table" | "tree" | "json";
export type UiPreferences = {
  theme: ThemeId;
  inspector_width: number;
  explain_format: ExplainFormat;
  files_show_hidden: boolean;
  files_transfers_open: boolean;
  sidebar_width: number;
  server_nav_collapsed: boolean;
  health_auto_production: number;
  health_auto_development: number;
  diagram_zoom_speed: number;
};
const defaults: UiPreferences = { theme: "system", inspector_width: INSPECTOR_DEFAULT, explain_format: "table", files_show_hidden: false, files_transfers_open: true, sidebar_width: SIDEBAR_DEFAULT, server_nav_collapsed: true, health_auto_production: -1, health_auto_development: -1, diagram_zoom_speed: 100 };
// Read legacy browser development values once. The server's complete answer
// always wins, including its defaults. Never copy browser values to the store.
function legacyPreferences(): UiPreferences {
  const value = { ...defaults };
  if (typeof document === "undefined" || !document.documentElement) return value;
  value.theme = (document.documentElement.dataset.theme || "system") as ThemeId;
  try {
    value.server_nav_collapsed = localStorage.getItem("myflames.serverNavCollapsed") !== "false";
    const width = Number(localStorage.getItem("myflames.sidebarWidth"));
    if (Number.isInteger(width) && width >= SIDEBAR_MIN && width <= SIDEBAR_MAX) value.sidebar_width = width;
    const inspector = Number(localStorage.getItem("myflames.inspector.width"));
    if (Number.isInteger(inspector) && inspector >= INSPECTOR_MIN && inspector <= INSPECTOR_MAX) value.inspector_width = inspector;
    const format = localStorage.getItem("myflames.editor.explainFormat");
    if (format === "tree" || format === "json") value.explain_format = format;
    for (const [key, legacy] of [["files_show_hidden", "showHidden"], ["files_transfers_open", "transfersOpen"]] as const) {
      const flag = localStorage.getItem(`myflames.files.${legacy}`);
      if (flag !== null) value[key] = flag === "1";
    }
    for (const type of ["production", "development"] as const) {
      const auto = localStorage.getItem(`myflames.health.auto.${type}`);
      if (auto !== null && auto.trim() && [-1, 0, 10, 30, 60].includes(Number(auto))) value[`health_auto_${type}`] = Number(auto);
    }
  } catch { /* The injected theme and defaults work without browser storage. */ }
  return value;
}
let preferences = legacyPreferences();
let loading: Promise<UiPreferences> | null = null;
let writes = 0;
let queue: Promise<unknown> = Promise.resolve();
const listeners = new Set<(value: UiPreferences) => void>();
export function applySidebarWidth(width: number) {
  document.documentElement.style.setProperty("--sidebar-w", `${clampSidebarWidth(width)}px`);
}
function publish(next: UiPreferences) {
  preferences = next;
  applySidebarWidth(next.sidebar_width);
  listeners.forEach(listener => listener(next));
}
export const currentUiPreferences = () => preferences;
export function loadUiPreferences(): Promise<UiPreferences> {
  if (!loading) {
    const before = writes;
    loading = api<{ preferences: UiPreferences }>("settings/ui", {}).then(reply => {
      if (writes === before) publish({ ...defaults, ...reply.preferences });
      return preferences;
    }, error => { loading = null; applySidebarWidth(preferences.sidebar_width); throw error; });
  }
  return loading;
}
/** Let startup render while a slow read continues to publish through the listeners. */
export async function loadUiPreferencesWithin(timeoutMs = 1500): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { await Promise.race([loadUiPreferences(), new Promise<void>(resolve => { timer = setTimeout(resolve, timeoutMs); })]); }
  finally { clearTimeout(timer); }
}
/** Optimistic display, serialized writes and latest-answer publication. A
 * failed latest write re-reads the server instead of guessing its outcome. */
export function setUiPreference<K extends keyof UiPreferences>(key: K, value: UiPreferences[K]): Promise<void> {
  const mine = ++writes;
  publish({ ...preferences, [key]: value });
  const sent = queue.then(() => api<{ preferences: UiPreferences }>("settings/ui", { set: { [key]: value } }));
  queue = sent.catch(() => undefined);
  return sent.then(reply => {
    if (mine === writes) {
      publish({ ...defaults, ...reply.preferences });
      loading = Promise.resolve(preferences);
    }
  }, error => {
    if (mine === writes) {
      loading = null;
      void loadUiPreferences().catch(() => { /* Keep the last known choice while offline. */ });
    }
    throw error;
  });
}
export function subscribeUiPreferences(listener: (value: UiPreferences) => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
export function useUiPreferences() {
  const [value, setValue] = useState(preferences);
  useEffect(() => {
    const stop = subscribeUiPreferences(setValue);
    setValue(preferences);
    void loadUiPreferences().catch(() => {});
    return stop;
  }, []);
  return value;
}

/** Drain writes queued before or during shutdown, without hanging on an offline backend. */
export async function flushUiPreferences(timeoutMs = 1500): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const drain = async () => {
    let pending;
    do { pending = queue; await pending; } while (pending !== queue);
  };
  try { await Promise.race([drain(), new Promise<void>(resolve => { timer = setTimeout(resolve, timeoutMs); })]); }
  finally { clearTimeout(timer); }
}
// The desktop invokes this in the renderer before stopping its owned backend.
if (typeof window !== "undefined") Object.assign(window, { __myflamesFlushUiPreferences: flushUiPreferences });
