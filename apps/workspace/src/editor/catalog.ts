import { api, type CatalogTable, type CatalogTables, type Connection, type SchemaCatalog } from "../api";
import { connectionKey } from "../binding";
import type { EditorEnv } from "./types";

export type CatalogOwner = { editor: object; generation: number };
export type CatalogKinds = { schemas: SchemaCatalog; tables: CatalogTables; columns: CatalogTable };
export type CatalogParams = { schemas: Record<string, never>; tables: { schema: string; prefix?: string }; columns: { schema: string; table: string } };
export type CatalogResult<T> = { value: T | null; failed: boolean; complete: boolean; truncated: boolean; stale: boolean; readAt: number };
export type CatalogEvent = { kind: keyof CatalogKinds; params: object; reason: "arrived" | "expired" | "invalidated" };
type Entry = { result: CatalogResult<unknown>; until: number; retryAt?: number; timer: ReturnType<typeof setTimeout>; kind: keyof CatalogKinds; params: object };
type Subscriber = { owner: CatalogOwner; cancel: () => void };
type Pending = { controller: AbortController; subscribers: Set<Subscriber>; promise: Promise<CatalogResult<unknown>> };
const aborted = () => new DOMException("Catalog read cancelled", "AbortError");

/** Page-memory metadata only. Credentials never appear in cache keys or events.
 * Expiry removes diagnostic authority, not useful names from this identity. */
export class CatalogService {
  private identity = "";
  private password: string | undefined;
  private epoch = 0;
  private cache = new Map<string, Entry>();
  private pending = new Map<string, Pending>();
  private listeners = new Set<(event: CatalogEvent) => void>();
  constructor(private connection: () => Connection | undefined) { this.syncConnection(); }

  syncConnection(): number {
    const connection = this.connection();
    const identity = connection ? JSON.stringify([connectionKey(connection), connection.database]) : "";
    if (identity !== this.identity || connection?.password !== this.password) {
      this.identity = identity;
      this.password = connection?.password;
      this.epoch++;
      for (const read of this.pending.values()) read.controller.abort();
      this.pending.clear();
      for (const [key, entry] of this.cache) this.remove(key, entry, "invalidated");
    }
    return this.epoch;
  }
  subscribe(fn: (event: CatalogEvent) => void): () => void {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }
  /** Inspect identity-bound metadata, including stale names. Never starts IO. */
  peek<K extends keyof CatalogKinds>(kind: K, params: CatalogParams[K]): CatalogResult<CatalogKinds[K]> | null {
    this.syncConnection();
    const entry = this.cache.get(JSON.stringify([kind, params]));
    if (!entry) return null;
    if (entry.until <= Date.now()) this.expire(entry);
    return entry.result as CatalogResult<CatalogKinds[K]>;
  }
  /** Prefix inventories are useful local names even when a new prefix still
   * needs a server read. Their coverage never becomes a complete inventory. */
  peekTableNames(schema: string): CatalogResult<CatalogTables>[] {
    this.syncConnection();
    const out: CatalogResult<CatalogTables>[] = [];
    for (const entry of this.cache.values()) {
      if (entry.kind !== "tables" || (entry.params as CatalogParams["tables"]).schema !== schema) continue;
      const result = this.peek("tables", entry.params as CatalogParams["tables"]);
      if (result?.value && !result.failed) out.push(result);
    }
    return out;
  }
  cancelOwner(owner: CatalogOwner): void {
    for (const [key, read] of this.pending) {
      for (const subscriber of read.subscribers) {
        if (subscriber.owner.editor === owner.editor && subscriber.owner.generation === owner.generation) {
          read.subscribers.delete(subscriber);
          subscriber.cancel();
        }
      }
      if (!read.subscribers.size) {
        read.controller.abort();
        if (this.pending.get(key) === read) this.pending.delete(key);
      }
    }
  }
  dispose(): void {
    for (const read of this.pending.values()) read.controller.abort();
    this.pending.clear();
    for (const entry of this.cache.values()) clearTimeout(entry.timer);
    this.cache.clear();
    this.listeners.clear();
  }
  private notify(event: CatalogEvent): void { for (const listener of this.listeners) listener(event); }
  private expire(entry: Entry): void {
    if (entry.result.stale) return;
    entry.result = { ...entry.result, stale: true };
    this.notify({ kind: entry.kind, params: entry.params, reason: "expired" });
  }
  private remove(key: string, entry: Entry, reason: CatalogEvent["reason"]): void {
    if (this.cache.get(key) !== entry) return;
    clearTimeout(entry.timer);
    this.cache.delete(key);
    this.notify({ kind: entry.kind, params: entry.params, reason });
  }

  /** Stale names are served immediately while a single refresh runs. Completion's
   * remote lane may await that refresh; its local lane already returned peek().
   * Each consumer can cancel its own wait without aborting other owners. */
  async read<K extends keyof CatalogKinds>(kind: K, params: CatalogParams[K], owner: CatalogOwner,
    explicit = false, awaitFresh = false): Promise<CatalogResult<CatalogKinds[K]>> {
    const epoch = this.syncConnection(), connection = this.connection();
    const key = JSON.stringify([kind, params]), old = this.cache.get(key);
    if (old && old.until <= Date.now()) this.expire(old);
    if (old && !old.result.stale && !(explicit && old.result.failed)) return old.result as CatalogResult<CatalogKinds[K]>;
    if (old?.result.value && old.retryAt && old.retryAt > Date.now() && !explicit) return old.result as CatalogResult<CatalogKinds[K]>;
    if (!connection) throw aborted();
    let pending = this.pending.get(key);
    if (!pending) {
      const controller = new AbortController();
      pending = { controller, subscribers: new Set(), promise: Promise.resolve(null!) };
      this.pending.set(key, pending);
      pending.promise = this.fetch(kind, params, key, epoch, { ...connection }, controller).finally(() => {
        if (this.pending.get(key) === pending) this.pending.delete(key);
      });
    }
    const read = pending;
    let subscriber: Subscriber;
    const cancel = new Promise<never>((_, reject) => {
      subscriber = { owner, cancel: () => reject(aborted()) };
      read.subscribers.add(subscriber);
    });
    const result = Promise.race([read.promise, cancel]).finally(() => read.subscribers.delete(subscriber));
    if (old?.result.value && !awaitFresh) {
      void result.catch(() => {});
      return old.result as CatalogResult<CatalogKinds[K]>;
    }
    return result as Promise<CatalogResult<CatalogKinds[K]>>;
  }
  private async fetch<K extends keyof CatalogKinds>(kind: K, params: CatalogParams[K], key: string, epoch: number,
    connection: Connection, controller: AbortController): Promise<CatalogResult<CatalogKinds[K]>> {
    let onAbort: () => void = () => {};
    const cancel = new Promise<never>((_, reject) => {
      onAbort = () => reject(aborted());
      controller.signal.addEventListener("abort", onAbort, { once: true });
    });
    let value: CatalogKinds[K] | null = null, failed = false;
    try {
      value = await Promise.race([api<CatalogKinds[K]>(kind === "schemas" ? "live/schemas" : "live/catalog",
        { connection, ...params }, controller.signal), cancel]);
    } catch (error) {
      if (controller.signal.aborted) throw error;
      failed = true;
    } finally {
      controller.signal.removeEventListener("abort", onAbort);
    }
    if (controller.signal.aborted || this.syncConnection() !== epoch) throw aborted();
    const previous = this.cache.get(key);
    // A failed refresh never erases usable metadata or makes it authoritative.
    if (failed && previous?.result.value) {
      previous.retryAt = Date.now() + 10000;
      return previous.result as CatalogResult<CatalogKinds[K]>;
    }
    const truncated = !!(value && "truncated" in value && value.truncated);
    const complete = !failed && !truncated && (kind !== "tables" || !(params as CatalogParams["tables"]).prefix);
    const readAt = Date.now(), result = { value, failed, complete, truncated, stale: false, readAt };
    if (previous) { clearTimeout(previous.timer); this.cache.delete(key); }
    if (this.cache.size >= 64) {
      const first = [...this.cache].find(([, entry]) => entry.result.stale || entry.until <= readAt) || this.cache.entries().next().value!;
      this.remove(first[0], first[1], "expired");
    }
    const ttl = failed ? 10000 : 30000;
    const entry: Entry = { result, until: readAt + ttl, kind, params: { ...params },
      timer: setTimeout(() => this.expire(entry), ttl) };
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
