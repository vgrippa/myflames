// Baselines (myflames/baselines.py): a Health reading or the Query digest
// totals of one server, kept on this computer to compare a later reading
// with. List and save read the server's identity (server_uuid, else
// @@hostname and port) through the connection; get and delete open none.
import { api, type Connection, type HealthStatus, type TopStatement } from "./api";

export type BaselineKind = "health" | "digest";
/** The server a baseline belongs to, as it named itself. */
export type BaselineServer = {
  engine: string;
  flavor: string | null;
  product: string;
  version: string;
  server_uuid: string | null;
  hostname: string | null;
  port: number | null;
};
/** The server a reading came from: read in the same client invocation as the data
 * (`with_identity: true` on live/health and live/top adds `reading_server`),
 * or why it could not be read. */
export type ReadingServer =
  | { verified: true; server_key: string; server: BaselineServer; uptime_s: number | null; server_time: string | null; error?: undefined }
  | { verified?: false; error: string; server_key?: undefined; server?: undefined };
/** The `reading_server` a reading carries, or null for a reading without one. */
export function readingServerOf(report: unknown): ReadingServer | null {
  const value = report && typeof report === "object" ? (report as { reading_server?: unknown }).reading_server : null;
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (typeof v.error === "string") return { error: v.error };
  if (v.verified !== true) return { error: "The server identity was not verified with this reading. Refresh, then try again." };
  return typeof v.server_key === "string" && v.server && typeof v.server === "object" ? (value as ReadingServer) : null;
}
/** One saved baseline as a list shows it (baseline-list-1.0 item). */
export type BaselineItem = {
  id: string;
  kind: BaselineKind;
  name: string;
  /** The server it belongs to (server_uuid, else host name and port, with the engine). */
  server_key: string;
  /** When it was saved (ISO, this computer's clock in UTC). */
  created_at: string;
  server: BaselineServer;
  uptime_s?: number | null;
  /** Health: the reading's time, its window and its counts. */
  taken_at?: string;
  interval?: number | null;
  status?: "ok" | "watch" | "problem";
  problem?: number;
  watch?: number;
  ok?: number;
  /** Query digest: when it was read (this computer), on the server's clock, and its totals. */
  read_at?: string;
  server_time?: string | null;
  order?: string;
  limit?: number | null;
  statements?: number;
  executions?: number;
  total_ms?: number;
};
export type BaselineList = {
  schema_version: "baseline-list-1.0";
  kind: BaselineKind;
  server: BaselineServer;
  server_key: string;
  /** The most baselines of one kind a server keeps. */
  max: number;
  items: BaselineItem[];
};
/** What a Health baseline keeps of health-1.0. */
export type HealthBaselinePayload = {
  schema_version: "health-1.0";
  engine: string;
  server_product: string | null;
  server_version: string | null;
  interval: number | null;
  taken_at: string;
  uptime_s: number | null;
  summary: { status: "ok" | "watch" | "problem" } & Record<HealthStatus, number>;
  metrics: { key: string; group: string; label: string; unit: string; value: number | null; status: HealthStatus }[];
  diagnostics: { key: string; title: string; status: HealthStatus; state: string | null; headline: string; value: number | null; unit: string }[];
  server?: BaselineServer;
};
/** One statement a Query digest baseline keeps (literal values hidden). */
export type DigestBaselineRow = {
  digest: string | null;
  schema: string | null;
  digest_text: string | null;
  count: number;
  total_ms: number;
  avg_ms: number | null;
  max_ms: number | null;
  first_seen: string | null;
  last_seen: string | null;
};
export type DigestBaselinePayload = {
  schema_version: "top-1.0";
  engine: string;
  server_version: string | null;
  server_product: string | null;
  order: string;
  limit: number | null;
  read_at: string;
  /** The server's clock when it was read (NOW(6) less the Uptime since). */
  server_time?: string | null;
  coverage: { uptime_s: number | null; counting_since: string | null; oldest_first_seen?: string | null; digest_lost: number | null };
  totals: { digests: number | null; executions: number | null; total_ms: number | null } | null;
  statements: DigestBaselineRow[];
  server?: BaselineServer;
};
export type Baseline<P> = BaselineItem & { payload: P };
/** A Query digest reading as save sends it: top-1.0 with its read time. */
export type DigestReading = {
  schema_version: "top-1.0";
  engine: string;
  server_version: string;
  server_product?: string | null;
  order: string;
  limit: number;
  read_at: string;
  coverage?: { uptime_s: number | null; counting_since?: string | null; oldest_first_seen?: string | null; digest_lost: number | null } | null;
  totals?: { digests: number; total_ms: number; executions: number } | null;
  statements: TopStatement[];
  /** The server the reading came from (see ReadingServer). */
  reading_server?: ReadingServer | null;
};

export const listBaselines = (connection: Connection, kind: BaselineKind, signal?: AbortSignal) =>
  api<BaselineList>("baselines/list", { connection, kind }, signal);
export const saveBaseline = (connection: Connection, kind: BaselineKind, name: string, report: object, signal?: AbortSignal) =>
  api<BaselineItem & { max: number }>("baselines/save", { connection, kind, name, report }, signal);
export const getBaseline = <P>(id: string, signal?: AbortSignal) => api<Baseline<P>>("baselines/get", { id }, signal);
export const deleteBaseline = (id: string, signal?: AbortSignal) =>
  api<{ deleted: boolean; id: string }>("baselines/delete", { id }, signal);
