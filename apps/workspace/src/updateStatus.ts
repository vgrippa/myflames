// The application update check shared by Home's footer and About myflames.
// Nothing here reaches the network on its own: the stored last check is a
// local read, and GitHub is asked only from checkForUpdates (a button click).
import { useSyncExternalStore } from "react";
import { appUpdateStatus, appVersion, checkAppUpdate, type AppUpdateInfo, type AppUpdateLastCheck } from "./api";

export type UpdateState = {
  /** The stored last check has been read. */
  loaded: boolean;
  /** Reading the stored last check failed: its outcome is unknown (retried on the next mount). */
  loadFailed: boolean;
  checking: boolean;
  last: AppUpdateLastCheck | null;
  /** The full answer of a check made in this window (needed to download). */
  info: AppUpdateInfo | null;
};

/** A result older than this is shown as a past result, never as current. */
export const STALE_AFTER_MS = 24 * 60 * 60 * 1000;

let state: UpdateState = { loaded: false, loadFailed: false, checking: false, last: null, info: null };
const listeners = new Set<() => void>();
let loading = false;
let inFlight: Promise<AppUpdateInfo | null> | null = null;

function set(patch: Partial<UpdateState>) {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useUpdateStatus(): UpdateState {
  return useSyncExternalStore(subscribe, () => state, () => state);
}

/** Read the stored last check once per page (again after a failed read). Local only. */
export function loadUpdateStatus() {
  if (loading || state.loaded) return;
  loading = true;
  appUpdateStatus()
    .then((r) => set({ loaded: true, loadFailed: false, last: state.last ?? r.last_check ?? null }))
    .catch(() => set({ loadFailed: true }))
    .finally(() => { loading = false; });
}

/** Ask GitHub for the latest release. Only call this from a user's click. */
export function checkForUpdates(): Promise<AppUpdateInfo | null> {
  if (inFlight) return inFlight;
  set({ checking: true });
  inFlight = checkAppUpdate(true)
    .then((info) => {
      const checkedAt = info.checked_at || new Date().toISOString();
      const last: AppUpdateLastCheck = info.last_check ?? (info.error
        ? { checked_at: checkedAt, outcome: "failed", current_version: info.current_version, latest_version: null,
            reason: info.reason || info.error }
        : { checked_at: checkedAt, outcome: info.update_available ? "available" : "up_to_date",
            current_version: info.current_version, latest_version: info.latest_version, reason: null });
      const usable = info.error ? null : info;
      set({ loaded: true, loadFailed: false, last, info: usable });
      return usable;
    })
    .catch((e: unknown) => {
      // The workspace itself did not answer: shown for this window only.
      set({ loaded: true, loadFailed: false, info: null, last: { checked_at: new Date().toISOString(), outcome: "failed",
        current_version: appVersion(), latest_version: null,
        reason: e instanceof Error ? e.message.replace(/\.$/, "") : "the request failed" } });
      return null;
    })
    .finally(() => {
      inFlight = null;
      set({ checking: false });
    });
  return inFlight;
}

const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

/** "today at 10:42", "yesterday", "Oct 18" (with the year when it differs), in local time. */
export function checkedWhen(iso: string, now = new Date()): { text: string; title: string; stale: boolean } | null {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const text = sameDay(at, now)
    ? `today at ${at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`
    : sameDay(at, yesterday)
      ? "yesterday"
      : at.toLocaleDateString(undefined, at.getFullYear() === now.getFullYear()
        ? { month: "short", day: "numeric" }
        : { month: "short", day: "numeric", year: "numeric" });
  return {
    text,
    title: at.toLocaleString(undefined, { dateStyle: "full", timeStyle: "medium" }),
    stale: now.getTime() - at.getTime() > STALE_AFTER_MS,
  };
}

/** The status words for a last check. A stale result names itself as the last result. */
export function updateStatusText(last: AppUpdateLastCheck | null, stale: boolean, loadFailed = false): string {
  if (!last) return loadFailed ? "Last check unknown" : "Not checked yet";
  if (last.outcome === "failed") return `Could not check: ${last.reason || "unknown reason"}`;
  const found = last.outcome === "available" ? `${last.latest_version} available` : "Up to date";
  return stale ? `Last result: ${found.charAt(0).toLowerCase()}${found.slice(1)}` : found;
}
