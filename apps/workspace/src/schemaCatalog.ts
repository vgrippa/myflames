// The shared database list (one cheap read per connection: schema names and
// table counts, perf_common.schema_catalog). Its own module so the shell,
// which keeps a catalog for the editor's completion and cross-links, does
// not pull every Server-view component of PerfShared into the entry chunk.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Connection, type SchemaCatalog } from "./api";
import { connectionKey } from "./binding";

/** One list per connection while the workspace is open. */
const catalogs = new Map<string, Promise<SchemaCatalog>>();
/** When the cached read of a connection resolved, for reloadStale. */
const catalogReadAt = new Map<string, number>();
/** The hook instances of a connection: a refresh replaces the shared promise,
 * so the others are told to attach to it (one subscriber set per key); an
 * invalidation (true) makes only the instances on screen read again. */
const catalogListeners = new Map<string, Set<(invalidated?: boolean) => void>>();
/** The connection each key was last rendered with: a read for a key always
 * uses that key's connection, never the one on screen when a late callback
 * runs (a run that ends after the person switched connections). */
const catalogConnections = new Map<string, Connection>();

/** Forget *connection*'s database list (a run created or dropped a database):
 * its views on screen read it again now; hidden ones read when shown. */
export function invalidateSchemaCatalog(connection: Connection) {
  const key = connectionKey(connection);
  catalogs.delete(key);
  catalogReadAt.delete(key);
  for (const listener of [...(catalogListeners.get(key) || [])]) listener(true);
}

/** The database list for *connection*: loaded when *enabled*, cached. */
export function useSchemaCatalog(connection: Connection, enabled = true) {
  // The same identity as every Server report (binding.ts): saved connection
  // id, host, port, user and TLS settings including the CA file.
  const key = connectionKey(connection);
  const [state, setState] = useState<{ key: string; catalog: SchemaCatalog | null; error: string; loading: boolean }>(
    { key: "", catalog: null, error: "", loading: false },
  );
  catalogConnections.set(key, connection);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  /** The shared read this instance shows: when it is shown again after the
   * list was invalidated or replaced meanwhile, it attaches to the current one. */
  const attached = useRef<Promise<SchemaCatalog> | null>(null);
  const load = useCallback(
    (refresh = false) => {
      if (refresh || !catalogs.has(key)) {
        const request = api<SchemaCatalog>("live/schemas", { connection: catalogConnections.get(key)! });
        catalogs.set(key, request);
        // The failure is handled below; this branch only records the time.
        request.then(() => {
          if (catalogs.get(key) === request) catalogReadAt.set(key, Date.now());
        }, () => {});
        // A failed read is forgotten, unless a newer one already replaced it.
        request.catch(() => {
          if (catalogs.get(key) === request) catalogs.delete(key);
        });
        if (refresh) for (const listener of catalogListeners.get(key) || []) listener();
      }
      const request = catalogs.get(key)!;
      attached.current = request;
      // Only the latest read of the connection on screen may answer: an older
      // read of the same connection (before Reload databases) is dropped.
      // (A failed read removes itself from the cache first.)
      const latest = () => !catalogs.has(key) || catalogs.get(key) === request;
      setState((s) => ({ key, catalog: s.key === key ? s.catalog : null, error: "", loading: true }));
      request.then(
        (catalog) => setState((s) => (s.key === key && latest() ? { key, catalog, error: "", loading: false } : s)),
        (e) => setState((s) => (s.key === key && latest() ? { key, catalog: null, error: (e as Error).message, loading: false } : s)),
      );
    },
    [key],
  );
  // Another instance's refresh (Reload databases, the editor's catalog
  // triggers) shows here without waiting for this view to reload itself.
  useEffect(() => {
    let listeners = catalogListeners.get(key);
    if (!listeners) catalogListeners.set(key, (listeners = new Set()));
    const attach = (invalidated?: boolean) => { if (!invalidated || enabledRef.current) load(); };
    listeners.add(attach);
    return () => {
      listeners.delete(attach);
      if (!listeners.size) catalogListeners.delete(key);
    };
  }, [key, load]);
  useEffect(() => {
    if (enabled && (state.key !== key || catalogs.get(key) !== attached.current)) load();
  }, [enabled, key, load, state.key]);
  const current = state.key === key ? state : { catalog: null, error: "", loading: enabled };
  return {
    catalog: current.catalog,
    error: current.error,
    loading: current.loading,
    reload: () => load(true),

    /** Re-read only when the cached list is older than *ms* (or never read):
     * opening a picker every few seconds does not refetch. */
    reloadStale: (ms: number) => {
      const at = catalogReadAt.get(key);
      if (at !== undefined && Date.now() - at <= ms) return;
      load(at !== undefined);
    },
  };
}
