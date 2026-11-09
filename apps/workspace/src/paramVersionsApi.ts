// Parameter versions: a catalog of MySQL and MariaDB releases, each read from
// a running server in a lab and shipped with myflames (param_versions.py).
// These calls answer from the package: no connection, no server read, no audit.
// Python writes every sentence; this file only types the replies and turns
// raw facts into display text.
import { api } from "./api";

export type VersionEngine = "mysql" | "mariadb";
export const VERSION_ENGINES: VersionEngine[] = ["mysql", "mariadb"];
export const ENGINE_NAMES: Record<VersionEngine, string> = { mysql: "MySQL", mariadb: "MariaDB" };

/** parameter-version-catalog-1.0 */
export type CatalogRelease = { version: string; series: string; maturity: string; date: string };
export type CatalogEngine = {
  engine: VersionEngine; product: string;
  /** False when this build has no catalog for the engine; notes[0] says why. */
  available: boolean; versions: CatalogRelease[];
  skipped: { version: string; reason: string }[]; names: string[];
  generated: Record<string, unknown>; notes: string[];
};
export type VersionCatalog = { schema: "parameter-version-catalog-1.0"; engines: CatalogEngine[] };

/** One parameter in one release, in the catalog's FIELDS order. Values are the
 * server's own text (ON/OFF, enum names, decimal integers). */
export type VersionFacts = {
  scope: "global" | "session" | "both" | null;
  type: string | null;
  dynamic: boolean | null;
  default: string | null;
  /** null: the default as read; "null": SQL NULL; "host": computed at startup
   * from the host; "release", "build" and "state" are never compared; "unknown": no default could be read. */
  default_note: null | "null" | "host" | "release" | "build" | "state" | "unknown";
  min: string | null;
  max: string | null;
  values: string[] | null;
  /** Evidence: "server" (the running server warned), "source", "manual". */
  deprecated: string[] | null;
  replacement: string | null;
  alias_of: string | null;
  plugin: string | null;
};
export type ChangeKind = "added" | "removed" | "deprecated" | "undeprecated" | "default" | "dynamic" | "scope" | "type" | "values" | "range";
export const CHANGE_KINDS: ChangeKind[] = ["added", "removed", "deprecated", "undeprecated", "default", "dynamic", "scope", "type", "values", "range"];
/** Badge words. Facts keep the names the Parameters list uses (Apply type,
 * Data type, Allowed values). */
export const KIND_LABELS: Record<ChangeKind, string> = {
  added: "Added", removed: "Removed", deprecated: "Deprecated", undeprecated: "No longer deprecated", default: "Default",
  dynamic: "Apply type", scope: "Scope", type: "Data type", values: "Allowed values", range: "Range",
};

/** parameter-version-compare-1.0 */
export type VersionChange = {
  name: string; kinds: ChangeKind[]; text: string[];
  from: VersionFacts | null; to: VersionFacts | null;
  /** The earliest release, walking back from `to`, since which each kind's value holds. */
  /** Per kind, the release on To's own line from which To's value holds; null when From's line made the difference. */
  since: Partial<Record<ChangeKind, string | null>>;
  replacement: string | null;
};
export type VersionCounts = Record<ChangeKind | "changed" | "unchanged", number>;
export type VersionComparison = {
  schema: "parameter-version-compare-1.0"; engine: VersionEngine; product: string;
  from: string; to: string; direction: "upgrade" | "downgrade" | "same";
  summary: string; counts: VersionCounts; changes: VersionChange[]; notes: string[];
};

/** parameter-history-1.0 */
export type HistoryEvent = {
  version: string; previous: string | null; series: string; kinds: ChangeKind[]; text: string[];
  before: VersionFacts | null; after: VersionFacts | null;
};
export type ParameterHistory = {
  schema: "parameter-history-1.0"; engine: VersionEngine; product: string; name: string; found: boolean; summary: string;
  first: string | null; latest: string | null; current: VersionFacts | null;
  series: { series: string; ranges: { from: string; to: string; facts: VersionFacts | null }[] }[];
  events: HistoryEvent[]; suggestions: string[];
  /** One sentence when the parameter exists only in builds with an option (innodb_numa_interleave). */
  build_only: string | null;
  /** How the server sets this default at the latest release, when it computes it at startup. */
  default_rule: string | null;
  /** Names that are aliases of this parameter at the latest release. */
  aliases: string[]; notes: string[];
};

export const versionCatalog = (signal?: AbortSignal) => api<VersionCatalog>("config/versions", {}, signal);
export const compareVersions = (engine: VersionEngine, from: string, to: string, signal?: AbortSignal) =>
  api<VersionComparison>("config/versions/compare", { engine, from, to }, signal);
export const parameterHistory = (engine: VersionEngine, name: string, signal?: AbortSignal) =>
  api<ParameterHistory>("config/versions/history", { engine, name }, signal);

// ---- Releases ------------------------------------------------------------------

const parts = (version: string) => version.split(".").map(n => Number.parseInt(n, 10) || 0);
/** Numeric release order (8.0.9 before 8.0.10). */
export function compareReleases(a: string, b: string) {
  const x = parts(a), y = parts(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) - (y[i] || 0);
  return 0;
}
/** The release number in a server's version text: "8.4.6-6" (Percona),
 * "11.4.5-MariaDB-log" -> "8.4.6", "11.4.5"; null without one. */
export function releaseOf(serverVersion: string | null | undefined): string | null {
  const m = /^\s*(\d+)\.(\d+)\.(\d+)/.exec(serverVersion || "");
  return m ? `${Number(m[1])}.${Number(m[2])}.${Number(m[3])}` : null;
}
/** Releases grouped by series, newest series and newest release first. */
export function seriesGroups(versions: CatalogRelease[]): { series: string; releases: CatalogRelease[] }[] {
  const groups = new Map<string, CatalogRelease[]>();
  for (const r of [...versions].sort((a, b) => compareReleases(b.version, a.version))) {
    const list = groups.get(r.series);
    if (list) list.push(r); else groups.set(r.series, [r]);
  }
  return [...groups].map(([series, releases]) => ({ series, releases }));
}
export type ServerRelease =
  | { state: "none" }
  | { state: "cataloged"; version: string }
  | { state: "newer"; version: string; newest: string }
  | { state: "skipped"; version: string; reason: string; nearest: string | null }
  | { state: "missing"; version: string; nearest: string | null };
/** Where the connected server's release stands in the catalog. */
export function serverRelease(catalog: CatalogEngine | null | undefined, serverVersion: string | null | undefined): ServerRelease {
  const version = releaseOf(serverVersion);
  if (!catalog || !version || !catalog.versions.length) return { state: "none" };
  if (catalog.versions.some(r => r.version === version)) return { state: "cataloged", version };
  const sorted = [...catalog.versions].sort((a, b) => compareReleases(a.version, b.version));
  const newest = sorted[sorted.length - 1].version;
  if (compareReleases(version, newest) > 0) return { state: "newer", version, newest };
  // The nearest earlier release of the same series, else of any series.
  const series = version.split(".").slice(0, 2).join(".");
  const earlier = sorted.filter(r => compareReleases(r.version, version) < 0);
  const nearest = [...earlier].reverse().find(r => r.series === series || r.version.startsWith(`${series}.`))?.version
    || earlier[earlier.length - 1]?.version || null;
  const skipped = catalog.skipped.find(s => s.version === version);
  return skipped ? { state: "skipped", version, reason: skipped.reason, nearest } : { state: "missing", version, nearest };
}
/** From and To before the user picks: From is the connected server's release
 * (or the nearest earlier cataloged one), To the newest release. Without a
 * server, From is the newest GA or LTS release of another series, else the
 * newest release of the series before. */
export function defaultPair(catalog: CatalogEngine, serverVersion: string | null | undefined): { from: string; to: string } {
  const sorted = [...catalog.versions].sort((a, b) => compareReleases(b.version, a.version));
  if (!sorted.length) return { from: "", to: "" };
  const newest = sorted[0];
  const at = serverRelease(catalog, serverVersion);
  let from = at.state === "cataloged" ? at.version : at.state === "skipped" || at.state === "missing" ? at.nearest || "" : "";
  if (!from || from === newest.version) {
    const stable = sorted.find(r => r.series !== newest.series && /^(GA|LTS)$/i.test(r.maturity));
    from = (stable || sorted.find(r => r.series !== newest.series) || sorted[1] || newest).version;
    // A connected server on the newest release compares with the release before it.
    if (at.state === "cataloged" && at.version === newest.version) from = (sorted[1] || newest).version;
  }
  return { from, to: newest.version };
}

// ---- Kind filter ---------------------------------------------------------------

export type KindFilter = "all" | "added" | "removed" | "deprecated" | "default" | "other";
export const KIND_FILTERS: [KindFilter, string][] = [
  ["all", "All"], ["added", "Added"], ["removed", "Removed"], ["deprecated", "Deprecated"], ["default", "Default changed"], ["other", "Other"],
];
const NAMED = new Set<ChangeKind>(["added", "removed", "deprecated", "default"]);
/** A change with several kinds counts under each of its filters; Other holds
 * every kind without its own filter (no longer deprecated, apply type, scope,
 * data type, allowed values, range). */
export function matchesKind(change: { kinds: ChangeKind[] }, filter: KindFilter) {
  if (filter === "all") return true;
  if (filter === "other") return change.kinds.some(k => !NAMED.has(k));
  return change.kinds.includes(filter);
}
export function kindCounts(changes: { kinds: ChangeKind[] }[]): Record<KindFilter, number> {
  const counts = { all: 0, added: 0, removed: 0, deprecated: 0, default: 0, other: 0 } as Record<KindFilter, number>;
  for (const c of changes) for (const [f] of KIND_FILTERS) if (matchesKind(c, f)) counts[f] += 1;
  return counts;
}
/** The kind whose values the From and To columns show: the default first,
 * then the other value facts; Added and Removed show the default. */
const SHOWN: ChangeKind[] = ["default", "range", "values", "type", "scope", "dynamic", "deprecated", "undeprecated", "added", "removed"];
export function shownKind(kinds: ChangeKind[]): ChangeKind {
  return SHOWN.find(k => kinds.includes(k)) || kinds[0] || "default";
}

// ---- Values --------------------------------------------------------------------

/** A display value and what hover says (the exact text, or why it is unknown). */
export type Shown = { text: string; title?: string; muted?: boolean; code?: boolean;
  /** Pieces that wrap as wholes before wrapping inside ("100 to", then the maximum). */
  parts?: string[] };
let grouping: Intl.NumberFormat | null = null;
/** Integers get thousands grouping (exact, even past 2^53); anything else stays as read. */
export function groupedValue(text: string): Shown {
  if (/^-?\d{4,}$/.test(text)) {
    grouping = grouping || new Intl.NumberFormat(undefined, { useGrouping: true });
    try { return { text: grouping.format(BigInt(text)), title: text, code: true }; } catch { /* keep the text */ }
  }
  return { text, title: text.length > 40 ? text : undefined, code: true };
}
const UNKNOWN: Shown = { text: "Unknown", title: "Not recorded for this release.", muted: true };
export const NOT_PRESENT: Shown = { text: "Not present", title: "The parameter does not exist in this release.", muted: true };
export function defaultValue(f: VersionFacts): Shown {
  if (f.default_note === "null") return { text: "NULL", title: "The default is SQL NULL.", code: true };
  if (f.default_note === "host") return { text: "Computed at startup", muted: true,
    title: `The server computes it at startup (from the host's CPUs, memory, file limits or host name, or from other settings).${f.default !== null ? ` One lab server got ${f.default}.` : ""}` };
  if (f.default_note === "release") return { text: f.default ?? "The release itself", muted: true,
    title: "The server reports its own release or build here. It is not a setting with a default, so it is never compared." };
  if (f.default_note === "state") return { text: "Set by the server", muted: true,
    title: "Session or server state (a connection id, a GTID set, the clock), not a setting with a default. Never compared." };
  if (f.default_note === "build") return { text: "Depends on the build", muted: true,
    title: `It follows the build's installation layout (paths), so it is never compared.${f.default !== null ? ` The lab build had ${f.default}.` : ""}` };
  if (f.default_note === "unknown" || f.default === null) return { text: "Unknown", title: "No default could be read from the running server.", muted: true };
  if (f.default === "") return { text: "Empty", title: "An empty string.", muted: true };
  return groupedValue(f.default);
}
const NUMERIC = /^(integer|double|bytes|int|uint|ulong|ulonglong|long)$/i;
/** Range ends at an integer type's limit read as the limit (the exact number is on hover):
 * "0 to 2⁶⁴ − 1" fits a narrow column where 18,446,744,073,709,551,615 would break. */
const INTEGER_LIMITS: Record<string, string> = {
  "18446744073709551615": "2⁶⁴ − 1", "9223372036854775807": "2⁶³ − 1", "-9223372036854775808": "−2⁶³",
  "4294967295": "2³² − 1", "2147483647": "2³¹ − 1", "-2147483648": "−2³¹",
};
export function rangeValue(f: VersionFacts): Shown {
  if (f.min === null && f.max === null) return f.type && NUMERIC.test(f.type) ? UNKNOWN : { text: "None", title: "Not a number.", muted: true };
  const end = (v: string | null) => v === null ? "?" : INTEGER_LIMITS[v] || groupedValue(v).text;
  const exact = (v: string | null) => v === null ? "unknown" : groupedValue(v).text;
  return { text: `${end(f.min)} to ${end(f.max)}`, parts: [`${end(f.min)} to`, end(f.max)], title: `${exact(f.min)} to ${exact(f.max)}`, code: true };
}
export function valuesValue(f: VersionFacts): Shown {
  if (!f.values || !f.values.length) return { text: "None listed", muted: true };
  const text = f.values.join(", ");
  return { text, title: text, code: true };
}
export const scopeText = (s: VersionFacts["scope"]) => s === "global" ? "Global" : s === "session" ? "Session" : s === "both" ? "Global and session" : null;
export const dynamicText = (d: boolean | null) => d === true ? "Dynamic" : d === false ? "Static" : null;
const EVIDENCE: Record<string, string> = { server: "server warning", source: "source code", manual: "manual" };
export function deprecatedValue(f: VersionFacts): Shown {
  if (!f.deprecated || !f.deprecated.length) return { text: "No" };
  return { text: `Yes (${f.deprecated.map(e => EVIDENCE[e] || e).join(", ")})`, title: "What says it is deprecated in this release." };
}
const plain = (text: string | null): Shown => text === null ? UNKNOWN : { text };
/** The value of one kind's fact, for the From and To columns and history rows. */
export function kindValue(kind: ChangeKind, f: VersionFacts | null): Shown {
  if (!f) return NOT_PRESENT;
  switch (kind) {
    case "range": return rangeValue(f);
    case "values": return valuesValue(f);
    case "type": return plain(f.type);
    case "scope": return plain(scopeText(f.scope));
    case "dynamic": return plain(dynamicText(f.dynamic));
    case "deprecated": case "undeprecated": return deprecatedValue(f);
    default: return defaultValue(f);
  }
}
/** Setting | From | To rows (and the latest facts of a history). Rows that
 * say nothing on either side (no plugin, no alias) are left out. */
export type FactRow = { key: string; label: string; a: Shown; b: Shown; differs: boolean };
export function factRows(a: VersionFacts | null, b: VersionFacts | null): FactRow[] {
  const sides = [a, b];
  const row = (key: string, label: string, value: (f: VersionFacts) => Shown, optional = false): FactRow | null => {
    const [x, y] = sides.map(f => f ? value(f) : NOT_PRESENT);
    if (optional && sides.every(f => !f || value(f).muted)) return null;
    return { key, label, a: x, b: y, differs: !!a && !!b && x.text !== y.text };
  };
  const name = (v: string | null): Shown => v ? { text: v, code: true } : { text: "None", muted: true };
  return [
    row("scope", "Scope", f => plain(scopeText(f.scope))),
    row("type", "Data type", f => plain(f.type)),
    row("dynamic", "Apply type", f => plain(dynamicText(f.dynamic))),
    row("default", "Default", defaultValue),
    row("range", "Range", rangeValue, true),
    row("values", "Allowed values", valuesValue, true),
    row("deprecated", "Deprecated", deprecatedValue),
    row("replacement", "Replacement", f => name(f.replacement), true),
    row("alias_of", "Alias of", f => name(f.alias_of), true),
    row("plugin", "Plugin", f => name(f.plugin), true),
  ].filter((r): r is FactRow => !!r);
}
