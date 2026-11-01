// Read-only server comparisons. Each profile selector is resolved independently
// by Python, including its saved password. No generated SQL is executed here.
import { api, type Connection } from "./api";
import { bt } from "./sqlIdent";

export type CompareStatus = "different" | "only_in_source" | "only_in_target" | "identical" | "not_compared";
export const COMPARE_LABELS: Record<CompareStatus, string> = {
  different: "Different", only_in_source: "Only in source", only_in_target: "Only in target", identical: "Identical", not_compared: "Not compared",
};
export const COMPARE_ORDER: CompareStatus[] = ["different", "only_in_source", "only_in_target", "not_compared", "identical"];
export type CompareSide = { live: { connection: Connection } } | { profile: string } | { snapshot: string };
export type CompareServer = { host: string; port: number; engine: string; server_product?: string | null; server_version?: string | null;
  profile_id?: string; profile_name?: string };
export type SideMetadata = { kind: "live" | "snapshot" | "file" | "unavailable"; label?: string; server?: CompareServer; captured_at?: string; complete: boolean;
  error?: string; number?: number; id?: string; saved_at?: string };
export type Snapshot = { id: string; number: number; saved_at: string; source_key: string; label: string; server: CompareServer; captured_at: string; complete: boolean; count: number };
export type ParameterValue = { name: string; value: string | number | boolean | null; default: string | number | boolean | null; source_label?: string | null; source?: string | null;
  type?: string | null };
export type ParameterDifference = { name: string; status: CompareStatus; status_text: string; identity: boolean; group: string | null;
  source: ParameterValue | null; target: ParameterValue | null; source_at_default: boolean | null; target_at_default: boolean | null; reason?: string };
export type CompareRole = { name: string; host: string; admin_option?: boolean };
export type AccountValue = { name: string; host: string; role: boolean; exists: boolean; grants: string[]; roles: CompareRole[]; default_roles: CompareRole[];
  attributes: { plugins?: string[]; locked?: boolean; expired?: boolean; tls?: Record<string, string | null>; limits?: Record<string, string>; password_options?: Record<string, string | number> };
  attributes_checked?: boolean; defaults_checked?: boolean; tls_checked?: boolean; error?: string; system?: boolean };
type SideChanges<T> = { only_in_source: T[]; only_in_target: T[] };
export type AccountDifference = { name: string; host: string; account: string; status: CompareStatus; status_text: string; source: AccountValue | null; target: AccountValue | null; reason?: string;
  /** A built-in system account (mysql.sys, mariadb.sys…): never chosen for export by a quick choice. */
  system?: boolean;
  differences: { grants: SideChanges<string>; roles: SideChanges<CompareRole>; default_roles: SideChanges<CompareRole>; attributes: { name: string; source: unknown; target: unknown }[] } | null };
export type Comparison<T> = { schema_version: string; compared_at: string; source: SideMetadata; target: SideMetadata; complete: boolean; identical: boolean;
  counts: Record<CompareStatus, number>; notes: string[]; items: T[] };
export type GrantsExport = { filename: string; text: string; side: SideMetadata };
export const listSnapshots = (signal?: AbortSignal, source_key?: string) => api<{ schema_version: "parameter-snapshot-list-1.0"; items: Snapshot[] }>("config/snapshots/list", { limit: 5000, ...(source_key ? { source_key } : {}) }, signal);
export const saveSnapshot = (connection: Connection, label: string, signal?: AbortSignal) => api<Snapshot>("config/snapshots/save", { connection, label }, signal);
export const deleteSnapshot = (id: string, signal?: AbortSignal) => api<{ deleted: boolean; id: string }>("config/snapshots/delete", { id }, signal);
export function compareParameters(source: CompareSide, target: CompareSide, signal: AbortSignal) {
  return api<Comparison<ParameterDifference>>("config/compare", { source, target }, signal);
}
export function compareAccounts(source: CompareSide, target: CompareSide, signal: AbortSignal) {
  return api<Comparison<AccountDifference>>("accounts/compare", { source, target }, signal);
}
export const exportGrants = (side: CompareSide, accounts: string[], signal: AbortSignal) => api<GrantsExport>("accounts/export", { side, accounts }, signal);
export function comparisonSummary(counts: Record<CompareStatus, number>) {
  return COMPARE_ORDER.filter(k => counts[k] || k === "identical").map(k => `${counts[k].toLocaleString()} ${COMPARE_LABELS[k].toLowerCase()}`).join(", ");
}
export const address = (server?: CompareServer) => server ? `${server.host}:${server.port}` : "Unknown server";
export function displayValue(value: unknown): string {
  return value == null ? "Unknown" : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value);
}
/** An account attribute record (TLS, limits, password policy) in words, one
 * entry per line; nothing set reads None, never "{}". */
export function recordText(value: Record<string, unknown> | null | undefined): string {
  const entries = Object.entries(value || {});
  if (!entries.length) return "None";
  // SQL clause names (MAX_QUERIES_PER_HOUR) stay as written; snake_case keys read as words.
  const name = (k: string) => k === k.toUpperCase() ? k : `${k.charAt(0).toUpperCase()}${k.slice(1).replace(/_/g, " ")}`;
  return entries.map(([k, v]) => `${name(k)}: ${v == null ? "None" : typeof v === "object" ? JSON.stringify(v) : String(v)}`).join("\n");
}
/** REQUIRE as read from mysql.user: the ssl_type word, then any cipher,
 * issuer or subject (hex, so the exact bytes stay visible). */
export function tlsText(tls: Record<string, string | null> | null | undefined): string {
  if (!tls) return "None";
  const lines = [(tls.type || "").trim() || "None"];
  for (const [key, label] of [["cipher_hex", "Cipher (hex)"], ["issuer_hex", "Issuer (hex)"], ["subject_hex", "Subject (hex)"]] as const) {
    if (tls[key]) lines.push(`${label}: ${tls[key]}`);
  }
  return lines.join("\n");
}
/** The accounts "All accounts only in source" chooses: built-in system
 * accounts stay out (exporting mysql.sys to MariaDB, or mariadb.sys to MySQL,
 * would create a stray account). */
export function onlyInSourceForExport<T extends { status: CompareStatus; system?: boolean; source: unknown }>(rows: T[]): T[] {
  return rows.filter(r => r.status === "only_in_source" && !r.system && !!r.source);
}
export function accountDifferenceText(row: AccountDifference) {
  if (!row.differences) return row.reason || row.status_text;
  const d = row.differences;
  const parts: string[] = [];
  for (const side of ["source", "target"] as const) {
    const n = d.grants[`only_in_${side}`].length;
    if (n) parts.push(`${n} ${n === 1 ? "grant" : "grants"} only in ${side}`);
  }
  if (d.roles.only_in_source.length || d.roles.only_in_target.length) parts.push("Roles differ");
  if (d.default_roles.only_in_source.length || d.default_roles.only_in_target.length) parts.push("Default roles differ");
  if (d.attributes.length) parts.push(`${d.attributes.length} ${d.attributes.length === 1 ? "attribute differs" : "attributes differ"}`);
  return parts.join(" · ") || row.reason || row.status_text;
}

/** Quote the structured identity so names containing @ or backticks stay exact. */
export const exportAccountName = (row: { name: string; host: string }) => `${bt(row.name)}@${bt(row.host)}`;
