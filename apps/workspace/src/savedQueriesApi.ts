// Saved query requests load with their editor surface. Keeping these helpers
// out of the entry module leaves room for product-specific navigation there.
import { api, type SavedQuery, type SavedQueryList } from "./api";

/** The saved queries, most recently saved, renamed or moved first. */
export const listSavedQueries = (signal?: AbortSignal) => api<SavedQueryList>("editor/saved/list", {}, signal);
/** Save a query; *redacted* says a credential in it was replaced by [redacted]. */
export const saveQuery = (query: { name: string; folder: string; sql: string; note?: string; database?: string | null }) =>
  api<{ saved: SavedQuery; redacted: boolean }>("editor/saved/save", query);
export const renameSavedQuery = (id: string, name: string) => api<{ saved: SavedQuery }>("editor/saved/rename", { id, name });
export const moveSavedQuery = (id: string, folder: string) => api<{ saved: SavedQuery }>("editor/saved/move", { id, folder });
export const deleteSavedQuery = (id: string) => api<{ deleted: number }>("editor/saved/delete", { id });
