import { api, type CatalogTable, type CatalogTables, type Connection, type SchemaCatalog } from "../api";
import { connectionKey } from "../binding";
import type { EditorEnv } from "./types";

export type CatalogOwner = { editor: object; generation: number };
export type CatalogKinds = { schemas: SchemaCatalog; tables: CatalogTables; columns: CatalogTable };
export type CatalogParams = { schemas: Record<string, never>; tables: { schema: string; prefix?: string }; columns: { schema: string; table: string } };
export type CatalogResult<T> = { value: T | null; failed: boolean; complete: boolean; truncated: boolean; readAt: number };
export type CatalogEvent = { kind: keyof CatalogKinds; params: object; reason: "arrived" | "expired" | "invalidated" };
type Entry = { result: CatalogResult<unknown>; until: number; timer: ReturnType<typeof setTimeout>; kind: keyof CatalogKinds; params: object };
type Pending = { owner: CatalogOwner; controller: AbortController };
const aborted = () => new DOMException("Catalog read cancelled", "AbortError");

/** Page-memory metadata only. Each editor shares this service with its consumers.
 * Credentials are compared separately and never appear in cache keys or events. */
export class CatalogService {
  private identity = "";
  private password: string | undefined;
  private epoch = 0;
  private cache = new Map<string, Entry>();
  private pending = new Set<Pending>();
  private listeners = new Set<(event: CatalogEvent) => void>();
  constructor(private connection: () => Connection | undefined) { this.syncConnection(); }

  /** Call on editor reconfiguration as well as reads, including switching away
   * and back before an outstanding response reaches the renderer. */
  syncConnection(): number {
    const connection = this.connection();
    const identity = connection ? JSON.stringify([connectionKey(connection), connection.database]) : "";
    if (identity !== this.identity || connection?.password !== this.password) {
      this.identity = identity;
      this.password = connection?.password;
      this.epoch++;
      for (const read of this.pending) read.controller.abort();
      for (const [key, entry] of this.cache) this.remove(key, entry, "invalidated");
    }
    return this.epoch;
  }

  subscribe(fn: (event: CatalogEvent) => void): () => void {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }
  cancelOwner(owner: CatalogOwner): void {
    for (const read of this.pending)
      if (read.owner.editor === owner.editor && read.owner.generation === owner.generation) read.controller.abort();
  }
  dispose(): void {
    for (const read of this.pending) read.controller.abort();
    for (const entry of this.cache.values()) clearTimeout(entry.timer);
    this.cache.clear();
    this.listeners.clear();
  }
  private notify(event: CatalogEvent): void { for (const listener of this.listeners) listener(event); }
  private remove(key: string, entry: Entry, reason: CatalogEvent["reason"]): void {
    if (this.cache.get(key) !== entry) return;
    clearTimeout(entry.timer);
    this.cache.delete(key);
    this.notify({ kind: entry.kind, params: entry.params, reason });
  }

  async read<K extends keyof CatalogKinds>(kind: K, params: CatalogParams[K], owner: CatalogOwner,
    explicit = false): Promise<CatalogResult<CatalogKinds[K]>> {
    const epoch = this.syncConnection(), connection = this.connection();
    const key = JSON.stringify([kind, params]);
    const old = this.cache.get(key);
    if (old && old.until > Date.now() && !(explicit && old.result.failed)) return old.result as CatalogResult<CatalogKinds[K]>;
    if (old) this.remove(key, old, "expired");
    if (!connection) throw aborted();
    const controller = new AbortController(), pending = { owner, controller };
    this.pending.add(pending);
    let onAbort: () => void = () => {};
    const cancel = new Promise<never>((_, reject) => {
      onAbort = () => reject(aborted());
      controller.signal.addEventListener("abort", onAbort, { once: true });
    });
    let value: CatalogKinds[K] | null = null, failed = false;
    try {
      value = await Promise.race([api<CatalogKinds[K]>(kind === "schemas" ? "live/schemas" : "live/catalog",
        { connection: { ...connection }, ...params }, controller.signal), cancel]);
    } catch (error) {
      if (controller.signal.aborted) throw error;
      failed = true;
    } finally {
      controller.signal.removeEventListener("abort", onAbort);
      this.pending.delete(pending);
    }
    // A transport may finish after an abort, or the environment may have changed
    // without a view update yet. Neither result may populate the new cache.
    if (controller.signal.aborted || this.syncConnection() !== epoch) throw aborted();
    const truncated = !!(value && "truncated" in value && value.truncated);
    const complete = !failed && !truncated && (kind !== "tables" || !(params as CatalogParams["tables"]).prefix);
    const readAt = Date.now(), result = { value, failed, complete, truncated, readAt };
    const previous = this.cache.get(key);
    if (previous) this.remove(key, previous, "expired");
    if (this.cache.size >= 32) {
      const first = this.cache.entries().next().value!;
      this.remove(first[0], first[1], "expired");
    }
    const ttl = failed ? 10000 : 30000;
    const entry: Entry = { result, until: readAt + ttl, kind, params: { ...params },
      timer: setTimeout(() => this.remove(key, entry, "expired"), ttl) };
    this.cache.set(key, entry);
    this.notify({ kind, params: entry.params, reason: "arrived" });
    return result;
  }
}

const services = new WeakMap<{ current: EditorEnv }, CatalogService>();
export function catalogFor(env: { current: EditorEnv }): CatalogService {
  let service = services.get(env);
  if (!service) {
    service = new CatalogService(() => env.current.connection as Connection | undefined);
    services.set(env, service);
  }
  service.syncConnection();
  return service;
}
