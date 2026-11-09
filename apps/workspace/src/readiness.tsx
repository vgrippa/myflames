import { useSyncExternalStore } from "react";
import { readReadiness, type Connection, type Readiness } from "./api";
import { connectionKey } from "./binding";
import { InfoPopover } from "./kit";
import { relativeTime } from "./session";
import "./connections-readiness.css";

const STORAGE = "myflames.connectionReadiness.v1";
type Facts = Pick<Readiness, "product" | "version" | "read_only" | "super_read_only" | "performance_schema" | "replica" | "group_member" | "galera_member" | "checked_at">;
type Entry = { facts?: Facts; stale?: boolean; pending?: boolean; error?: string };
let cache: Record<string, Entry> = {};
const listeners = new Set<() => void>();
const pending = new Map<string, { cred: string; generation: number; promise: Promise<Readiness> }>();
const generations = new Map<string, number>();
const timers = new Map<string, ReturnType<typeof setTimeout>>();
const EMPTY: Entry = {};
const SECRET_A = ((Math.random() * 0xffffffff) >>> 0) || 0x9e3779b9;
const SECRET_B = ((Math.random() * 0xffffffff) >>> 0) || 0x85ebca6b;
// Keyed digest of the active credential so pending requests detect password
// edits without storing the password string in memory or storage.
function credentialDigest(password: string | undefined): string {
  const text = password || "";
  let h1 = (0x811c9dc5 ^ SECRET_A) >>> 0;
  let h2 = (0x811c9dc5 ^ SECRET_B) >>> 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ ch, 0x01000193) >>> 0;
    h2 = Math.imul(h2 ^ ((ch + i) & 0xffff), 0x01000193) >>> 0;
  }
  return `${text.length}:${h1.toString(16)}:${h2.toString(16)}`;
}
// Persist exactly these facts. No credential fields, grants, passwords or
// server errors are serialized, even if the endpoint grows new fields later.
function factsOf(r: Facts): Facts {
  const flag = (v: unknown) => typeof v === "boolean" ? v : null;
  return { product: String(r.product).slice(0, 128), version: String(r.version).slice(0, 128),
    read_only: flag(r.read_only), super_read_only: flag(r.super_read_only),
    performance_schema: flag(r.performance_schema), replica: flag(r.replica),
    group_member: flag(r.group_member), galera_member: flag(r.galera_member), checked_at: r.checked_at };
}
try {
  const saved: unknown = JSON.parse(localStorage.getItem(STORAGE) || "{}");
  if (saved && typeof saved === "object" && !Array.isArray(saved)) {
    for (const [key, value] of Object.entries(saved).slice(-1000)) {
      const e = value as Entry;
      if (e?.facts && typeof e.facts.product === "string" && typeof e.facts.version === "string" &&
          typeof e.facts.checked_at === "string" && Number.isFinite(Date.parse(e.facts.checked_at)))
        cache[key] = { facts: factsOf(e.facts), stale: true };
    }
  }
} catch { /* Storage may be unavailable or hold an older/corrupt value. */ }
function publish(key: string, entry: Entry) {
  cache = { ...cache, [key]: entry };
  for (const listener of listeners) listener();
}
function persist() {
  try {
    localStorage.setItem(STORAGE, JSON.stringify(Object.fromEntries(Object.entries(cache)
      .filter(([, e]) => e.facts).slice(-1000).map(([key, e]) => [key, { facts: e.facts, stale: e.stale }]))));
  } catch { /* Convenience cache: quota or privacy settings must not block reads. */ }
}
export function useReadiness(connection: Connection): Entry {
  const key = connectionKey(connection);
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; },
    () => cache[key] || EMPTY, () => EMPTY);
}
/** Explicit choices and tests call this. Mount and session restore never do. */
export function checkReadiness(connection: Connection): Promise<Readiness> {
  const key = connectionKey(connection);
  const cred = credentialDigest(connection.password);
  const inflight = pending.get(key);
  if (inflight && inflight.cred === cred) return inflight.promise;
  const generation = (generations.get(key) || 0) + 1;
  generations.set(key, generation);
  clearTimeout(timers.get(key));
  timers.delete(key);
  publish(key, { ...cache[key], pending: true, error: "" });
  const promise = readReadiness(connection).then(report => {
    if (generations.get(key) === generation) {
      publish(key, { facts: factsOf(report), stale: false });
      persist();
    }
    return report;
  }, (error: unknown) => {
    if (generations.get(key) === generation) {
      const message = error instanceof Error ? error.message : "Could not check this server.";
      publish(key, { ...cache[key], pending: false, stale: true, error: message });
      persist();
      timers.set(key, setTimeout(() => {
        if (generations.get(key) === generation) publish(key, { ...cache[key], error: "" });
        timers.delete(key);
      }, 6000));
    }
    throw error;
  }).finally(() => {
    if (pending.get(key)?.generation === generation) pending.delete(key);
  });
  pending.set(key, { cred, generation, promise });
  return promise;
}
export function copyReadiness(from: Connection, to: Connection) {
  const entry = cache[connectionKey(from)];
  if (!entry?.facts) return;
  publish(connectionKey(to), { facts: entry.facts, stale: entry.stale });
  persist();
}
export function readinessLine(f: Facts): string {
  return [`${f.product} ${f.version}`, f.replica === true ? "replica" : f.replica === null ? "replica unknown" : "",
    f.group_member === null ? "group unknown" : f.group_member ? "group member" : "",
    f.galera_member === null ? "Galera unknown" : f.galera_member ? "Galera member" : "",
    f.super_read_only ? "super read-only" : f.read_only ? "read-only" : "",
    `Performance Schema ${f.performance_schema === null ? "unknown" : f.performance_schema ? "on" : "off"}`].filter(Boolean).join(" · ");
}
export function readinessHint(r: Readiness): string {
  if (r.performance_schema === false) return "Performance Schema is off; performance views are limited. See Server > Instruments.";
  const kill = r.actions.kill;
  if (kill?.allowed === false) return `Kill needs ${kill.need || (r.engine === "mariadb" ? "CONNECTION ADMIN or SUPER" : "CONNECTION_ADMIN or SUPER")}.`;
  if (r.privileges.PROCESS === false) return "PROCESS is needed to see every user's sessions.";
  return r.notes[0] || "";
}
export function ReadinessFacts({ connection, details = false }: { connection: Connection; details?: boolean }) {
  const entry = useReadiness(connection);
  const line = entry.facts ? readinessLine(entry.facts) : details ? "Not checked" : "";
  const seen = entry.facts ? `seen ${relativeTime(entry.facts.checked_at)}${entry.stale ? " · stale" : ""}` : "";
  const fullTitle = [line || "Not checked", seen, entry.pending ? "Checking…" : "", entry.error || ""].filter(Boolean).join(" · ");
  return <span className="connection-readiness" aria-live="polite" title={details ? undefined : fullTitle}>
    <span className="connection-readiness-line" title={fullTitle}>{line && <bdi>{line}</bdi>}</span>
    {entry.facts && <span className="hint" title={fullTitle}> · {seen}</span>}
    <span className="hint" role="status">{entry.pending ? " · Checking…" : ""}</span>
    {/* An alert only with something to announce: an empty one reads as a failure on Home. */}
    {entry.error && <span className="form-error" role="alert"><bdi>{entry.error}</bdi></span>}
    {details && entry.facts?.performance_schema === false && <InfoPopover label="Readiness limits">
      <p>Performance Schema is off; performance views are limited. See Server &gt; Instruments.</p>
    </InfoPopover>}
  </span>;
}
