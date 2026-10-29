// The Schema versions place's wire types (schema-version-list-1.0,
// schema-snapshot-1.0 and schema-compare-1.0 from myflames/schema_versions.py,
// docs/schemas/) and its calls (/api/schema-versions/*, schema_versions_api.py).
// Python decides every difference and writes every sentence (change text,
// summaries, the sync script); this file only carries them.
import { api, type Connection } from "./api";
import { sshKey, trimPasted } from "./binding";
import type { AlterPrediction } from "./AlterPrediction";

export type ObjectType =
  | "database" | "table" | "view" | "sequence" | "procedure" | "function" | "package" | "package_body" | "trigger" | "event";
export const TYPE_LABELS: Record<ObjectType, string> = {
  database: "Database", table: "Table", view: "View", sequence: "Sequence", procedure: "Procedure",
  function: "Function", package: "Package", package_body: "Package body", trigger: "Trigger", event: "Event",
};
/** Plural nouns for counts ("7 tables"). */
export const TYPE_PLURALS: Record<ObjectType, [string, string]> = {
  database: ["database", "databases"], table: ["table", "tables"], view: ["view", "views"],
  sequence: ["sequence", "sequences"], procedure: ["procedure", "procedures"], function: ["function", "functions"],
  package: ["package", "packages"], package_body: ["package body", "package bodies"], trigger: ["trigger", "triggers"],
  event: ["event", "events"],
};
export const TYPE_ORDER: ObjectType[] = ["database", "table", "view", "sequence", "procedure", "function", "package",
  "package_body", "trigger", "event"];

export type SnapshotServer = {
  engine: "mysql" | "mariadb";
  flavor?: string | null;
  server_product?: string | null;
  server_version?: string | null;
  hostname?: string | null;
  host?: string | null;
  port?: number | null;
  lower_case_table_names?: string | null;
  /** Read through an SSH tunnel: host and port are then the database as this
   * SSH host reaches it. Absent for a direct read. Part of the server's
   * identity (schema_versions.SSH_FIELDS). */
  ssh_host?: string;
  ssh_user?: string;
  ssh_port?: number;
};
export type Coverage = Record<string, { state: "complete" | "unknown"; reason: string }>;
export type Counts = Partial<Record<ObjectType | "unreadable", number>>;

export type SnapshotObject = {
  type: ObjectType;
  name: string;
  ddl: string | null;
  reason?: string;
  table?: string;
  timing?: string;
  event?: string;
  order?: number;
  sql_mode?: string | null;
};
export type Snapshot = {
  schema_version: "schema-snapshot-1.0";
  taken_at: string;
  database: string;
  server: SnapshotServer;
  connection?: { profile_id?: string; profile_name?: string };
  coverage: Coverage;
  objects: SnapshotObject[];
  notes: string[];
  counts: Counts;
  checksum: string;
};

/** One saved version (schema-version-list-1.0 item). */
export type VersionItem = {
  id: string;
  saved_at: string;
  source_key: string;
  number: number;
  name: string;
  note: string;
  database: string;
  taken_at: string;
  server: SnapshotServer;
  connection: { profile_id?: string; profile_name?: string };
  counts: Counts;
  checksum: string;
  myflames_version: string;
  /** Every object kind was provably listed (the account's privileges). */
  complete: boolean;
  /** Save and import only: the previous version of this database with the same checksum. */
  same_as?: { id: string; number: number; name: string } | null;
};
export type VersionList = { schema_version: "schema-version-list-1.0"; items: VersionItem[] };
export type VersionWithSnapshot = VersionItem & { snapshot: Snapshot };

/** Each object's status: the source is the schema to match, the target the
 * one the sync script changes. */
export type CompareStatus = "different" | "only_in_source" | "only_in_target" | "not_compared" | "identical";
/** Python's risk judgments; null means no known rebuild or copy. */
export type Risks = { destructive: string[]; data_dependent: string[]; rebuild: string | null };
export type Change = {
  part: string;
  name: string | null;
  change: "only_in_source" | "only_in_target" | "different" | "moved";
  /** "column email", "key idx_name", "table options" (Python's words). */
  label?: string;
  source?: string | null;
  target?: string | null;
  position?: number;
  moved?: boolean;
  /** The effective character set and collation of a text column on each side
   * (information_schema.COLUMNS), when they differ. */
  collation?: { source: string[]; target: string[] };
  text: string;
  risks: Risks;
};
/** lines_diff(target, source): "-" only in the target, "+" only in the source. */
export type DiffLine = [" " | "-" | "+" | "@", string];
export type CompareEntry = {
  /** Stable namespace:name identity, including shared table/view/sequence names. */
  key: string;
  /** Comparison keys to choose before this object's changes can be generated. */
  blocked_by?: string[];
  type: ObjectType;
  name: string;
  status: CompareStatus;
  reason?: string;
  changes?: Change[];
  summary?: string;
  definer_only?: boolean;
  /** The target's spelling when the names pair without regard to case. */
  target_name?: string;
  source_ddl?: string | null;
  target_ddl?: string | null;
  diff?: DiffLine[];
  risks: Risks;
};
export type CompareSide = {
  kind: "live" | "version" | "file";
  label: string;
  database: string;
  server: SnapshotServer;
  taken_at: string;
  checksum: string;
  connection: { profile_id?: string; profile_name?: string };
  coverage: Coverage;
  id?: string;
  number?: number;
  name?: string;
};
/** Snapshot-only prediction, with the same clause evidence as the editor review. */
export type SyncAlgorithm = Omit<AlterPrediction, "text"> & { source: string; needs: string[] };
export type SyncStatement = {
  phase: string;
  object: { type: ObjectType; name: string } | null;
  sql: string;
  risks: ("drops_data" | "may_change_data" | "checks_rows" | "drops_privileges")[];
  risk_details: Risks;
  algorithm?: SyncAlgorithm;
  warnings: string[];
  compound: boolean;
};
/** The sync script: statements that would change the target to match the source. */
export type Sync = {
  database: string;
  statements: SyncStatement[];
  skipped: { type: ObjectType; name: string; reason: string }[];
  warnings: string[];
  chosen_keys: string[];
  omitted: { key: string; reason: string }[];
  summary: { statements: number; drops_data: number; skipped: number; warnings: number };
  script: string;
};
export type CompareReport = {
  schema_version: "schema-compare-1.0";
  comparison_id: string;
  compared_at: string;
  source: CompareSide;
  target: CompareSide;
  options: { ignore_definers: boolean };
  identical: boolean;
  summary: Record<CompareStatus | "definer_only", number>;
  objects: CompareEntry[];
  notes: string[];
  sync: Sync;
  markdown?: string;
};

export type LintReport = {
  schema_version: "schema-lint-1.0";
  findings: { rule: string; status: "watch"; object: string; column: string | null; text: string; detail: string }[];
  not_judged: { rule: string; object: string | null; column: string | null; reason: string }[];
};

/** What one side of a comparison is: a saved version, or a database read now
 * through a connection (the connection in use, or a saved one whose password
 * is saved; the server fills it). */
export type SideRequest = { version: string } | { live: { connection: Connection; database: string } };

export const listVersions = (signal?: AbortSignal) => api<VersionList>("schema-versions/list", {}, signal);
export const getVersion = (id: string, signal?: AbortSignal) => api<VersionWithSnapshot>("schema-versions/get", { id }, signal);
export const saveVersion = (connection: Connection, database: string, name: string, note: string, signal?: AbortSignal) =>
  api<VersionItem>("schema-versions/save", { connection, database, name, note }, signal);
export const updateVersion = (id: string, name: string, note: string) =>
  api<VersionItem>("schema-versions/update", { id, name, note });
export const deleteVersion = (id: string) => api<{ deleted: boolean }>("schema-versions/delete", { id });
export const exportVersion = (id: string, signal?: AbortSignal) =>
  api<{ filename: string; text: string }>("schema-versions/export", { id }, signal);
export const importVersion = (text: string, signal?: AbortSignal) => api<VersionItem>("schema-versions/import", { text }, signal);
export const compareSchemas = (source: SideRequest, target: SideRequest, ignoreDefiners: boolean, signal?: AbortSignal) =>
  api<CompareReport>("schema-versions/compare", { source, target, ignore_definers: ignoreDefiners, markdown: true }, signal);
export const selectSync = (comparisonId: string, chosenKeys: string[], signal?: AbortSignal) =>
  api<CompareReport>("schema-versions/sync", { comparison_id: comparisonId, chosen_keys: chosenKeys }, signal);
export const lintVersion = (id: string, connection?: Connection, signal?: AbortSignal) =>
  api<LintReport>("schema-versions/lint", { id, ...(connection ? { connection } : {}) }, signal);

/** "7 tables, 2 views, 1 procedure": the objects of a version, without the database. */
export function countsText(counts: Counts): string {
  const parts = TYPE_ORDER.filter((t) => t !== "database" && (counts[t] || 0) > 0)
    .map((t) => `${counts[t]} ${TYPE_PLURALS[t][counts[t] === 1 ? 0 : 1]}`);
  return parts.length ? parts.join(", ") : "No objects";
}
/** Objects of a version, not counting the database itself. */
export function objectCount(counts: Counts): number {
  return TYPE_ORDER.filter((t) => t !== "database").reduce((sum, t) => sum + (counts[t] || 0), 0);
}
/** "deploy@bastion:2222": the SSH target of a server read through SSH, else "". */
export function sshTarget(server: Pick<SnapshotServer, "ssh_host" | "ssh_user" | "ssh_port">): string {
  const ssh = sshKey(server);
  if (!ssh) return "";
  const [, host, user, port] = ssh;
  return `${user ? `${user}@` : ""}${host}${port ? `:${port}` : ""}`;
}
/** " via deploy@bastion:2222" for a server read through SSH, else "" (the
 * words of schema_versions.ssh_text). */
export function sshText(server: Pick<SnapshotServer, "ssh_host" | "ssh_user" | "ssh_port">): string {
  const target = sshTarget(server);
  return target ? ` via ${target}` : "";
}
/** "db1:3306", "127.0.0.1:3306 via bastion" (or the server's own host name
 * for an imported file without an address). */
export function serverAddress(server: SnapshotServer): string {
  return server.host ? `${server.host}:${server.port ?? ""}${sshText(server)}` : server.hostname || "Unknown server";
}
type Endpoint = Pick<Connection, "host" | "port" | "ssh_host" | "ssh_user" | "ssh_port">;
/**
 * Whether *connection* reaches the server a snapshot was read from: the same
 * host (any letter case) and port, and the same SSH target, or none on both
 * (127.0.0.1:3306 behind SSH host A is another server than behind B, or than
 * a direct connection). *ssh* explains a mismatch of the SSH target alone
 * (host and port match), naming the side as *what*; it is "" otherwise.
 */
export function serverMatch(server: SnapshotServer, connection: Endpoint, what = "It"): { same: boolean; ssh: string } {
  const host = trimPasted(server.host || "").toLowerCase();
  if (!host || !connection.host || trimPasted(connection.host).toLowerCase() !== host || Number(connection.port) !== Number(server.port)) {
    return { same: false, ssh: "" };
  }
  const read = sshKey(server), now = sshKey(connection);
  if (JSON.stringify(read) === JSON.stringify(now)) return { same: true, ssh: "" };
  const how = (target: string) => target ? `through SSH host ${target}` : "directly";
  return { same: false, ssh: `${what} was read ${how(sshTarget(server))} and the connection in use connects `
    + `${how(sshTarget(connection))}: the same address can be another server.` };
}
export function productText(server: SnapshotServer): string {
  return [server.server_product, server.server_version].filter(Boolean).join(" ");
}
/** "Version 3" or "Version 3 · before release". */
export function versionTitle(item: Pick<VersionItem, "number" | "name">): string {
  return item.name ? `Version ${item.number} · ${item.name}` : `Version ${item.number}`;
}
/** "version 3 of shop" or "version 3 of shop (“before release”)", for sentences. */
export function versionPhrase(item: Pick<VersionItem, "number" | "name" | "database">): string {
  return `version ${item.number} of ${item.database}${item.name ? ` (“${item.name}”)` : ""}`;
}
/** Local date and time for a stored UTC ISO stamp. */
export function localTime(iso: string): string {
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
export const STATUS_LABELS: Record<CompareStatus, string> = {
  different: "Different", only_in_source: "Only in source", only_in_target: "Only in target",
  not_compared: "Not compared", identical: "Identical",
};
/** What the sync script does to the target for each status. */
export const SYNC_ACTIONS: Record<CompareStatus, string> = {
  different: "Changed in target", only_in_source: "Created in target", only_in_target: "Dropped from target",
  not_compared: "Not generated", identical: "",
};
export const RISK_LABELS: Record<SyncStatement["risks"][number], string> = {
  drops_data: "Drops data", may_change_data: "May change data", checks_rows: "Checks rows",
  drops_privileges: "Drops privileges",
};
