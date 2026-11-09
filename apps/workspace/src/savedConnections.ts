// Saved connections (the API calls them profiles): one list for the whole
// window. Every list of saved connections reads it here: the connection
// picker, Home's Connections, the Source and Target of Compare in Parameters,
// Users and roles and Schema versions, Maintenance's clone donor, Cluster and
// the Audit log. A connection created, edited, renamed or deleted in this
// window, or a password saved or forgotten, is read again once (Connections
// calls refreshSavedConnections after each write), and every list shows it
// without a reload. Returning to the window (focus, a visible page) reads the
// list again at most once a second, for changes made in another window or with
// the CLI. profiles/list reads the workspace store on this computer: no server
// is read and nothing is sent.
import { useEffect, useSyncExternalStore } from "react";
import { api, type Profile, type SecretStore } from "./api";

export type SavedConnections = {
  /** The saved connections as the latest read returned them (no passwords). */
  items: Profile[];
  /** A read has succeeded at least once. */
  loaded: boolean;
  /** The latest read failed: its message; "" once a read succeeds. */
  error: string;
  secretStore: SecretStore | null;
};

const EMPTY: SavedConnections = { items: [], loaded: false, error: "", secretStore: null };
let state: SavedConnections = EMPTY;
const listeners = new Set<() => void>();
let sequence = 0;
let latest: Promise<Profile[]> | null = null;
/** When the window last came back and read the list (Date.now()). */
let lastReturn = 0;
/** The name of every saved connection this window has seen, by id, so a
 * picker whose choice was deleted can still name it. */
const names = new Map<string, string>();

function publish(next: SavedConnections) {
  state = next;
  for (const listener of [...listeners]) listener();
}
const sameJson = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
const errorText = (e: unknown) => (e instanceof Error && e.message ? e.message : "Could not read the saved connections.");

/**
 * Read the saved connections again. The latest read wins: an older answer,
 * success or failure, never replaces a newer one, and every caller gets the
 * answer of the latest read (a write-policy check never acts on an old list).
 * An unchanged list keeps its identity, so the lists that show it do not
 * change.
 */
export function refreshSavedConnections(): Promise<Profile[]> {
  const n = ++sequence;
  const pending: Promise<Profile[]> = (async () => {
    try {
      const r = await api<{ items: Profile[]; secret_store?: SecretStore }>("profiles/list", {});
      if (n !== sequence) return latest!;
      const items = Array.isArray(r.items) ? r.items : [];
      for (const p of items) names.set(p.id, p.name);
      const next: SavedConnections = {
        items: sameJson(state.items, items) ? state.items : items,
        loaded: true,
        error: "",
        secretStore: r.secret_store && !sameJson(r.secret_store, state.secretStore) ? r.secret_store : state.secretStore,
      };
      // Nothing changed: no list re-renders (a return to the window is cheap).
      if (next.items !== state.items || next.secretStore !== state.secretStore || !state.loaded || state.error) publish(next);
      return state.items;
    } catch (e) {
      if (n !== sequence) return latest!;
      publish({ ...state, error: errorText(e) });
      throw e;
    }
  })();
  latest = pending;
  return pending;
}

/** The list as it is now, outside React. */
export const savedConnectionsNow = () => state;

/** The name this window last saw for the saved connection *id*, or "". */
export const seenConnectionName = (id: string) => names.get(id) || "";
/** Saved connections that are not in the list any more, named as this window
 * last saw them: "Alpha (deleted)". */
export function deletedConnectionLabel(id: string): string {
  const name = names.get(id);
  return name ? `${name} (deleted)` : "A deleted saved connection";
}
/** The saved connection *id* was deleted: the list has loaded and lacks it. */
export const savedConnectionDeleted = (list: SavedConnections, id: string) =>
  !!id && list.loaded && !list.items.some((p) => p.id === id);

// Back in the window: read again, at most once a second, only after a first
// read (the workspace reads first once its session is restored).
function returned() {
  if (document.visibilityState === "hidden" || !(state.loaded || state.error)) return;
  const since = Date.now() - lastReturn;
  if (since >= 0 && since < 1000) return;
  lastReturn = Date.now();
  refreshSavedConnections().catch(() => {});
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    window.addEventListener("focus", returned);
    document.addEventListener("visibilitychange", returned);
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      window.removeEventListener("focus", returned);
      document.removeEventListener("visibilitychange", returned);
    }
  };
}
const snapshot = () => state;

/**
 * The saved connections of this window. *load* reads them once when nothing
 * has been read yet (a component shown on its own); the workspace shell
 * passes false and reads them once its session is restored.
 */
export function useSavedConnections(load = true): SavedConnections {
  const list = useSyncExternalStore(subscribe, snapshot, snapshot);
  useEffect(() => {
    if (load && !latest) refreshSavedConnections().catch(() => {});
  }, [load]);
  return list;
}

/** Tests only: forget every read (a fresh window). */
export function resetSavedConnectionsForTests() {
  state = EMPTY;
  sequence = 0;
  latest = null;
  lastReturn = 0;
  names.clear();
}
