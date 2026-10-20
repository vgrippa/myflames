// Session restore: the workspace snapshot that is autosaved to the local
// server (/api/session/*) and reopened on the next launch.
//
// The snapshot never holds a password. Connection settings are copied field
// by field, and stripSecrets removes any `password` key as a second guard
// (the server strips them again before writing).
import { api, ApiError, type Capture, type CaptureSource } from "./api";

export const SESSION_VERSION = "session-1.0";
export const MAX_SESSION_BYTES = 64 * 1024 * 1024;
// Browsers cap the bodies of keepalive requests in flight at 64 KiB in total.
const KEEPALIVE_LIMIT = 60 * 1024;

export type QueryTab = {
  id: string;
  name: string;
  sql: string;
  parameters: string;
  /** Set when the tab was opened from Top statements; captures record it. */
  source?: CaptureSource;
  /** The connection the Top statements row came from (binding.ts
   * connectionKey and label, never a password): the tab runs only there
   * until the user explicitly moves it to the connection in use. */
  target?: TabTarget;
  /** Parameters (`p1`, …) that replaced the digest's ? and need values. */
  fill?: string[];
  /** The notice shown until those parameters have values. */
  fill_note?: string;
  /** Rows kept per result set when the tab runs SQL (default 1,000). */
  limit?: number;
  /** The caret or selection, restored with the tab. */
  selection?: { start: number; end: number };
  /** The results tab on screen: "messages", "plan", or "result:N". */
  result_tab?: string;
};
export type TabTarget = { key: string; label: string };
/** The Query editor's panes as the user left them. */
export type EditorLayout = {
  /** Height of the SQL text area in px (the results take the rest). */
  height: number;
  /** The schema browser is open, and its width. */
  browser: boolean;
  browser_width: number;
  /** The run history panel is open. */
  history: boolean;
  /** Seconds before a run is stopped with KILL QUERY; 0 for no limit. */
  run_timeout: number;
};
export const DEFAULT_ROW_LIMIT = 1000;
export const MAX_ROW_LIMIT = 100_000;
export const defaultEditorLayout = (): EditorLayout => ({
  height: 260,
  browser: true,
  browser_width: 248,
  history: false,
  run_timeout: 0,
});
/** A stored layout, clamped, with defaults for anything missing. */
export function normalizeEditorLayout(raw: unknown): EditorLayout {
  const base = defaultEditorLayout();
  if (!isObject(raw)) return base;
  return {
    height: int(raw.height, base.height, 120, 2000),
    browser: typeof raw.browser === "boolean" ? raw.browser : base.browser,
    browser_width: int(raw.browser_width, base.browser_width, 180, 480),
    history: raw.history === true,
    run_timeout: int(raw.run_timeout, base.run_timeout, 0, 86400),
  };
}
const RESULT_TAB = /^(messages|plan|result:\d{1,4})$/;
/** A stored tab target, or undefined when absent or malformed. */
export function normalizeTarget(raw: unknown): TabTarget | undefined {
  if (!isObject(raw) || typeof raw.key !== "string" || !raw.key || raw.key.length > 2000) return undefined;
  return { key: raw.key, label: typeof raw.label === "string" ? raw.label.slice(0, 300) : "" };
}
/** The Server place's views, in sub-navigation order (Overview, Workload,
 * Contention, Schema, Setup, Security). */
export const SERVER_KINDS = [
  "health",
  "innodb",
  "errorlog",
  "tuning",
  "report",
  "top",
  "sessions",
  "replication",
  "cluster",
  "waits",
  "io",
  "locks",
  "indexes",
  "tables",
  "diagram",
  "config",
  "instruments",
  "maintenance",
  "accounts",
] as const;
export type ServerKind = (typeof SERVER_KINDS)[number];
/** View ids saved by earlier versions, and the view that replaced them. A
 * Map, so a stored "constructor" or "__proto__" finds nothing. */
export const LEGACY_SERVER_KINDS: ReadonlyMap<string, ServerKind> = new Map([
  ["running", "sessions"],
  ["processlist", "sessions"],
  ["configuration", "config"],
]);
/** A saved view id (current or from an earlier version), or Health. */
export function serverKind(value: unknown): ServerKind {
  if (typeof value !== "string") return "health";
  if (SERVER_KINDS.includes(value as ServerKind)) return value as ServerKind;
  const legacy = LEGACY_SERVER_KINDS.get(value);
  return legacy && SERVER_KINDS.includes(legacy) ? legacy : "health";
}
/** Top statements orders a restore accepts: the rankings and the workload
 * attribution filters (joins without an index, first-table scans, reads
 * without an index). */
export const TOP_ORDERS: readonly string[] = ["total", "avg", "examined", "count", "full_join", "scan", "no_index"];
export type SlowState = {
  kind: ServerKind;
  order: string;
  min_seconds: number;
  /** Health sampling window in seconds (one of HEALTH_INTERVALS). */
  health_interval: number;
  /** Schema in focus for I/O and Top statements ("" = none). */
  schema: string;
  /** Waits and I/O sampling windows in seconds (one of WINDOW_INTERVALS). */
  waits_interval: number;
  io_interval: number;
  /** Databases chosen in the Indexes and Tables pickers; absent or empty
   * means all user databases. */
  indexes_schemas?: string[];
  tables_schemas?: string[];
  /** The inspector pane is open (its width is a per-browser preference). */
  inspector: boolean;
  /** Sessions: active sessions only, or every connection. */
  sessions_scope: "active" | "all" | "locked";
  /** A Full analysis ran in this workspace (the report itself is never saved). */
  report_run?: boolean;
  /** Maintenance: the open task section. */
  maintenance_section?: "tools" | "upgrade" | "dump" | "restore" | "clone";
  /** Replication: a skip loop was active when the workspace was saved. The
   * only loop state a restore keeps; it warns that nothing resumes. */
  replication_loop_active?: boolean;
  /** Column layout of the workspace tables (Server's views and the Cluster
   * place) by table id (TABLE_IDS): widths and hidden columns. Versioned
   * key: a new layout format gets a new key. */
  columns_v1?: Record<string, ColumnLayout>;
};
/** One table's column layout: widths in px by column key, hidden keys. */
export type ColumnLayout = { widths?: Record<string, number>; hidden?: string[] };
/** The tables that keep a column layout (DataTable id): Server's views and
 * the Cluster place (Nodes on Galera and PXC, Members on Group Replication,
 * Routers), each with its own. */
export const TABLE_IDS = [
  "errorlog.entries",
  "errorlog.codes",
  "top.statements",
  "sessions.sessions",
  "replication.channels",
  "cluster.members",
  "cluster.nodes",
  "cluster.routers",
  "waits.events",
  "waits.classes",
  "io.tables",
  "io.files",
  "io.kinds",
  "locks.transactions",
  "locks.rows",
  "locks.metadata",
  "indexes.inventory",
  "indexes.unused",
  "indexes.redundant",
  "indexes.scans",
  "indexes.nodata",
  "tables.inventory",
  "tables.missing",
  "tables.free",
  "maintenance.tools",
  "maintenance.upgrade",
  "maintenance.checks",
  "instruments.groups",
  "instruments.consumers",
  "instruments.list",
  "parameters.all",
] as const;
/** Column widths a restore accepts (DataTable clamps again to each column's minimum). */
export const COLUMN_WIDTH_MIN = 48;
export const COLUMN_WIDTH_MAX = 1200;
const COLUMN_KEY = /^[A-Za-z0-9_.:-]{1,64}$/;
/** Restored column layouts: known tables only; column keys checked, widths
 * clamped, non-numbers dropped, duplicates removed. A table the DataTable
 * then finds with unknown keys drops them, and never hides every column. */
export function columnLayouts(v: unknown): Record<string, ColumnLayout> {
  const out: Record<string, ColumnLayout> = {};
  if (!isObject(v)) return out;
  for (const id of TABLE_IDS) {
    const raw = Object.prototype.hasOwnProperty.call(v, id) ? v[id] : undefined;
    if (!isObject(raw)) continue;
    const layout: ColumnLayout = {};
    const widths: Record<string, number> = {};
    if (isObject(raw.widths))
      for (const [key, width] of Object.entries(raw.widths).slice(0, 64))
        if (COLUMN_KEY.test(key) && typeof width === "number" && Number.isFinite(width))
          widths[key] = Math.round(Math.min(COLUMN_WIDTH_MAX, Math.max(COLUMN_WIDTH_MIN, width)));
    if (Object.keys(widths).length) layout.widths = widths;
    const hidden: string[] = [];
    if (Array.isArray(raw.hidden))
      for (const key of raw.hidden.slice(0, 64))
        if (typeof key === "string" && COLUMN_KEY.test(key) && !hidden.includes(key)) hidden.push(key);
    if (hidden.length) layout.hidden = hidden;
    if (layout.widths || layout.hidden) out[id] = layout;
  }
  return out;
}
/** A picker's restored selection: valid names only, deduplicated. */
export function schemaList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  const out: string[] = [];
  for (const name of v.slice(0, 200))
    if (typeof name === "string" && name && name.length <= 64 && !/[\n\r\0]/.test(name) && !out.includes(name))
      out.push(name);
  return out;
}
/** Sampling windows the Health tab offers; the server accepts 1 to 60. */
export const HEALTH_INTERVALS = [1, 2, 5, 10, 15, 30, 60];
/** Sampling windows Waits and I/O offer; the server accepts 2 to 60. */
export const WINDOW_INTERVALS = [2, 5, 10, 15, 30, 60];
/** Non-secret connection fields. There is deliberately no password here. */
export type SessionConnection = {
  name: string;
  host: string;
  port: number;
  user: string;
  database: string;
  ssl_mode: string;
  ssl_ca: string;
  /** Refuse every write. The one safety choice an unsaved connection has (it
   * is always Production); for a saved connection the profile decides. */
  read_only: boolean;
};
export type LabState = {
  profile_id: string;
  connection: SessionConnection;
  tabs: QueryTab[];
  tab_id: string;
  timeout: number;
  repeat: number;
  trace: boolean;
  conditions: string;
  slow: SlowState;
  /** Query editor panes (absent: the defaults). */
  editor_layout?: EditorLayout;
};
export type SessionPlan = {
  origin?: "sample";
  id: string;
  name: string;
  raw: string;
  capture?: Capture;
  notes?: string;
};
export type SessionState = {
  schema_version: string;
  mode: string;
  view: string;
  metric: string;
  active_plan_id: string;
  selected_id: string;
  focus_id: string;
  collapsed_ids: string[];
  before_id: string;
  after_id: string;
  investigation: {
    id: string;
    name: string;
    notes: string;
    tags: string;
    baseline_id: string;
    dirty: boolean;
  };
  teach: { lesson: string; return_to_plan: boolean };
  query_lab: LabState;
  plans: SessionPlan[];
};
export type SessionLoad = {
  revision: number;
  saved_at: string;
  state: unknown;
  restore: boolean;
  skip_restore: boolean;
};

export const SSL_MODES = [
  "PREFERRED",
  "REQUIRED",
  "VERIFY_CA",
  "VERIFY_IDENTITY",
  "DISABLED",
];
export const defaultConnection: SessionConnection = {
  name: "Local MySQL",
  host: "127.0.0.1",
  port: 3306,
  user: "",
  database: "",
  ssl_mode: "PREFERRED",
  ssl_ca: "",
  read_only: false,
};
export function defaultLab(): LabState {
  return {
    profile_id: "",
    connection: { ...defaultConnection },
    tabs: [{ id: "1", name: "Query 1", sql: "SELECT 1", parameters: "{}" }],
    tab_id: "1",
    timeout: 30,
    repeat: 1,
    trace: false,
    conditions: "",
    slow: {
      kind: "health",
      order: "total",
      min_seconds: 0,
      health_interval: 5,
      schema: "",
      waits_interval: 10,
      io_interval: 10,
      inspector: false,
      sessions_scope: "active",
    },
  };
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  !!v && typeof v === "object" && !Array.isArray(v);
export const text = (v: unknown, fallback = "", max = 1_000_000) =>
  typeof v === "string" ? v.slice(0, max) : fallback;
const int = (v: unknown, fallback: number, min: number, max: number) =>
  typeof v === "number" && Number.isFinite(v)
    ? Math.min(max, Math.max(min, Math.round(v)))
    : fallback;

/** Remove every `password` key at any depth (a copy; the input is untouched). */
export function stripSecrets<T>(value: T): T {
  if (Array.isArray(value)) return value.map(stripSecrets) as T;
  if (!isObject(value)) return value;
  const out: Record<string, unknown> = {};
  for (const [key, item] of Object.entries(value))
    if (key.toLowerCase() !== "password") out[key] = stripSecrets(item);
  return out as T;
}

/** A digest source as stored, or undefined when absent or malformed. */
export function normalizeSource(raw: unknown): CaptureSource | undefined {
  if (!isObject(raw) || raw.kind !== "digest") return undefined;
  if (
    typeof raw.digest !== "string" ||
    !/^(?:[0-9a-f]{32}|[0-9a-f]{64})$/i.test(raw.digest)
  )
    return undefined;
  const schema =
    typeof raw.schema === "string" && raw.schema && raw.schema.length <= 64
      ? raw.schema
      : null;
  return { kind: "digest", digest: raw.digest.toLowerCase(), schema };
}

/** A complete, well-typed lab state from whatever was stored. Fixed key order. */
export function normalizeLab(raw: unknown): LabState {
  const base = defaultLab();
  if (!isObject(raw)) return base;
  const c = isObject(raw.connection) ? raw.connection : {};
  const connection: SessionConnection = {
    name: text(c.name, base.connection.name, 200),
    host: text(c.host, base.connection.host, 255),
    port: int(c.port, base.connection.port, 1, 65535),
    user: text(c.user, "", 255),
    database: text(c.database, "", 255),
    ssl_mode: SSL_MODES.includes(c.ssl_mode as string)
      ? (c.ssl_mode as string)
      : base.connection.ssl_mode,
    ssl_ca: text(c.ssl_ca, "", 4096),
    read_only: c.read_only === true,
  };
  const seen = new Set<string>();
  const tabs: QueryTab[] = [];
  for (const t of Array.isArray(raw.tabs) ? raw.tabs.slice(0, 100) : []) {
    // Ids are cut to 100 characters first, so two ids that only differ
    // after that are one id (the second tab is dropped), never two tabs
    // with the same id.
    const id = isObject(t) && typeof t.id === "string" ? t.id.slice(0, 100) : "";
    if (!isObject(t) || !id || seen.has(id)) continue;
    seen.add(id);
    const tab: QueryTab = {
      id,
      name: text(t.name, `Query ${tabs.length + 1}`, 200),
      sql: text(t.sql),
      parameters: text(t.parameters, "{}"),
    };
    const source = normalizeSource(t.source);
    if (source) {
      tab.source = source;
      const target = normalizeTarget(t.target);
      if (target) tab.target = target;
    }
    const fill = Array.isArray(t.fill)
      ? t.fill
          .filter((n): n is string => typeof n === "string" && /^p\d{1,4}$/.test(n))
          .slice(0, 1000)
      : [];
    if (fill.length) {
      tab.fill = fill;
      const note = text(t.fill_note, "", 2000);
      if (note) tab.fill_note = note;
    }
    if (typeof t.limit === "number" && Number.isFinite(t.limit) && Math.round(t.limit) !== DEFAULT_ROW_LIMIT)
      tab.limit = int(t.limit, DEFAULT_ROW_LIMIT, 1, MAX_ROW_LIMIT);
    if (isObject(t.selection)) {
      const start = int(t.selection.start, 0, 0, tab.sql.length);
      const end = int(t.selection.end, start, 0, tab.sql.length);
      if (start || end) tab.selection = { start: Math.min(start, end), end: Math.max(start, end) };
    }
    if (typeof t.result_tab === "string" && RESULT_TAB.test(t.result_tab)) tab.result_tab = t.result_tab;
    tabs.push(tab);
  }
  if (!tabs.length) tabs.push(...base.tabs);
  const wanted = typeof raw.tab_id === "string" ? raw.tab_id.slice(0, 100) : "";
  const tabId = tabs.some((t) => t.id === wanted) ? wanted : tabs[0].id;
  const s = isObject(raw.slow) ? raw.slow : {};
  return {
    profile_id: text(raw.profile_id, "", 200),
    connection,
    tabs,
    tab_id: tabId,
    timeout: int(raw.timeout, base.timeout, 1, 300),
    repeat: int(raw.repeat, base.repeat, 1, 10),
    trace: raw.trace === true,
    conditions: text(raw.conditions, "", 10000),
    slow: {
      kind: serverKind(s.kind),
      order: TOP_ORDERS.includes(s.order as string) ? (s.order as string) : "total",
      min_seconds: int(s.min_seconds, 0, 0, 1_000_000),
      health_interval: HEALTH_INTERVALS.includes(s.health_interval as number)
        ? (s.health_interval as number)
        : 5,
      schema: text(s.schema, "", 64).replace(/[\n\r\0]/g, ""),
      waits_interval: WINDOW_INTERVALS.includes(s.waits_interval as number)
        ? (s.waits_interval as number)
        : 10,
      io_interval: WINDOW_INTERVALS.includes(s.io_interval as number)
        ? (s.io_interval as number)
        : 10,
      ...(schemaList(s.indexes_schemas).length ? { indexes_schemas: schemaList(s.indexes_schemas) } : {}),
      ...(schemaList(s.tables_schemas).length ? { tables_schemas: schemaList(s.tables_schemas) } : {}),
      inspector: s.inspector === true,
      sessions_scope: s.sessions_scope === "all" ? "all" : s.sessions_scope === "locked" ? "locked" : "active",
      maintenance_section: (["tools", "upgrade", "dump", "restore", "clone"] as const).includes(
        s.maintenance_section as "tools" | "upgrade" | "dump" | "restore" | "clone",
      )
        ? (s.maintenance_section as "tools" | "upgrade" | "dump" | "restore" | "clone")
        : "tools",
      ...(s.replication_loop_active === true ? { replication_loop_active: true } : {}),
      ...(s.report_run === true ? { report_run: true } : {}),
      ...(Object.keys(columnLayouts(s.columns_v1)).length ? { columns_v1: columnLayouts(s.columns_v1) } : {}),
    },
    ...(isObject(raw.editor_layout) ? { editor_layout: normalizeEditorLayout(raw.editor_layout) } : {}),
  };
}

/** Same lab state, same string: used to tell real edits from a re-render. */
/** The small fields of a tab (everything but its text), for comparison. */
const tabExtras = (t: QueryTab) =>
  JSON.stringify([
    t.source ?? null,
    t.target ?? null,
    t.fill ?? null,
    t.fill_note ?? null,
    t.limit ?? null,
    t.selection ?? null,
    t.result_tab ?? null,
  ]);
/** Equal restorable lab state. Called on every editor change, so tab text is
 * compared as strings (the same string object compares at once), never
 * serialized: with tens of MB of SQL, serializing both copies cost tens of
 * milliseconds per keystroke. */
export function sameLab(a: LabState, b: LabState) {
  const x = normalizeLab(a);
  const y = normalizeLab(b);
  if (x.tabs.length !== y.tabs.length) return false;
  for (let i = 0; i < x.tabs.length; i++) {
    const s = x.tabs[i];
    const t = y.tabs[i];
    if (s.id !== t.id || s.name !== t.name || s.sql !== t.sql || s.parameters !== t.parameters) return false;
    if (tabExtras(s) !== tabExtras(t)) return false;
  }
  return JSON.stringify({ ...x, tabs: [] }) === JSON.stringify({ ...y, tabs: [] });
}

/** What a tab holds (not where its caret or results tab were). */
const tabText = (t: QueryTab) => [t.id, t.name, t.sql, t.parameters, t.source ?? null, t.target ?? null];
/** True when the snapshot holds anything worth announcing as restored. */
export function hasContent(state: SessionState) {
  const lab = state.query_lab;
  const blank = defaultLab();
  return (
    state.plans.length > 0 ||
    !!state.investigation.notes ||
    !!state.investigation.tags ||
    !!lab.profile_id ||
    JSON.stringify(lab.tabs.map(tabText)) !== JSON.stringify(blank.tabs.map(tabText)) ||
    JSON.stringify(lab.connection) !== JSON.stringify(blank.connection)
  );
}

/** "5 minutes ago", "yesterday", or "a moment ago". */
export function relativeTime(iso: string, now = Date.now()) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return "your last session";
  const seconds = Math.round((then - now) / 1000);
  if (Math.abs(seconds) < 45) return "a moment ago";
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["minute", 60],
    ["hour", 3600],
    ["day", 86400],
    ["week", 604800],
    ["month", 2629800],
    ["year", 31557600],
  ];
  let unit = units[0];
  for (const candidate of units)
    if (Math.abs(seconds) >= candidate[1]) unit = candidate;
  return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(
    Math.round(seconds / unit[1]),
    unit[0],
  );
}

export type SaveStatus =
  | { kind: "loading" }
  | { kind: "off"; reason: string }
  | { kind: "pending" }
  | { kind: "saving" }
  | { kind: "saved"; at: string }
  | { kind: "error"; reason: string };

/** Content the user made, excluding navigation, chart and read-only view state.
 * Reuse plan and SQL strings: never serialize their text again for status. */
export type SessionEdits = { plans: string; investigation: string; tabs: QueryTab[] };
export function sessionEdits(s: Pick<SessionState, "investigation" | "query_lab">, plansJson: string): SessionEdits {
  return { plans: plansJson, investigation: JSON.stringify(s.investigation), tabs: s.query_lab.tabs };
}
type SaveEdits = SessionEdits | string;
function sameEdits(a: SaveEdits, b: SaveEdits): boolean {
  if (typeof a === "string" || typeof b === "string") return a === b;
  if (a.plans !== b.plans || a.investigation !== b.investigation || a.tabs.length !== b.tabs.length) return false;
  return a.tabs.every((t, i) => {
    const other = b.tabs[i];
    return t.id === other.id && t.name === other.name && t.sql === other.sql && t.parameters === other.parameters
      && JSON.stringify([t.source, t.target]) === JSON.stringify([other.source, other.target]);
  });
}

const utf8 = new TextEncoder();

/**
 * Debounced autosave with revision checks. One page is one `clientId`; its
 * saves carry an increasing `seq`, so the server can accept this page's own
 * overlapping saves and drop a late older one, while a save from another
 * tab over a newer revision is a conflict.
 */
export class SessionSaver {
  revision = 0;
  readonly clientId =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : String(Math.random()).slice(2);
  private seq = 0;
  /** Reserve the next sequence number for a clear or settings change, so a
   * save from this page that is still in flight lands as stale after it. */
  nextSeq(): number {
    return ++this.seq;
  }
  private enabled = false;
  private saved = "";
  private pending = "";
  private savedEdits: SaveEdits = "";
  private pendingEdits: SaveEdits = "";
  private statusTimer = 0;
  private sent = "";
  private savedSeq = 0;
  private inflight = 0;
  private timer = 0;
  failed = false;
  constructor(
    private onStatus: (status: SaveStatus) => void,
    private onConflict: () => void,
  ) {}
  get active() {
    return this.enabled;
  }
  /** A status of "saved" also covers silent navigation saves; only this
   * exact snapshot comparison proves that workspace content is persisted. */
  isSaved(serialized: string): boolean {
    return serialized === this.saved;
  }
  /**
   * Changes the server has not confirmed that a closing page may lose: the
   * final save on pagehide goes out as a keepalive request, which browsers
   * cap at 64 KiB, so a larger snapshot may never arrive.
   */
  atRisk(): boolean {
    if (!this.enabled) return false;
    const body = this.pending;
    if (!body || body === this.saved) return false;
    // Each UTF-16 unit is at least one byte and at most three.
    if (body.length + 200 >= KEEPALIVE_LIMIT) return true;
    if (body.length * 3 + 200 < KEEPALIVE_LIMIT) return false;
    return utf8.encode(body).length + 200 >= KEEPALIVE_LIMIT;
  }
  /** Start saving changes; *baseline* is what the server already holds. */
  enable(baseline: string) {
    this.enabled = true;
    this.failed = false;
    this.saved = this.sent = baseline;
    // A restore establishes authored content once, outside the navigation path.
    const state = baseline ? JSON.parse(baseline) : null;
    this.savedEdits = state?.investigation && state?.query_lab
      ? sessionEdits(state, JSON.stringify(state.plans || [])) : baseline;
    if (this.pending && this.pending !== baseline) this.schedule();
    else this.onStatus({ kind: "saved", at: "" });
  }
  disable(reason: string) {
    this.enabled = false;
    this.failed = false;
    window.clearTimeout(this.timer);
    window.clearTimeout(this.statusTimer);
    this.onStatus({ kind: "off", reason });
  }
  /** Pause without changing the status (while a restore is applied). */
  pause() {
    this.enabled = false;
    window.clearTimeout(this.timer);
    window.clearTimeout(this.statusTimer);
  }
  update(serialized: string, edits: SaveEdits = serialized) {
    this.pending = serialized;
    this.pendingEdits = edits;
    if (serialized === this.saved) this.savedEdits = edits;
    if (sameEdits(edits, this.savedEdits)) window.clearTimeout(this.statusTimer);
    if (!this.enabled || serialized === this.saved) {
      window.clearTimeout(this.timer);
      if (this.enabled && !this.inflight && !this.failed)
        this.onStatus({ kind: "saved", at: "" });
      return;
    }
    this.schedule();
  }
  private schedule() {
    window.clearTimeout(this.timer);
    if (!this.failed) this.onStatus(sameEdits(this.pendingEdits, this.savedEdits)
      ? { kind: "saved", at: "" } : { kind: "pending" });
    this.timer = window.setTimeout(() => void this.flush(), 1000);
  }
  /** Save now. *final*: the page is hiding or unloading. */
  async flush(final = false) {
    window.clearTimeout(this.timer);
    if (!this.enabled) return;
    const body = this.pending;
    // On unload, a change already on its way is sent again as keepalive: the
    // request in flight may be cancelled with the page.
    if (!body || body === this.saved || (body === this.sent && !final)) return;
    if (this.inflight && !final) {
      // The in-flight save finishes first, then this change is saved.
      this.schedule();
      return;
    }
    if (utf8.encode(body).length > MAX_SESSION_BYTES) {
      this.fail(
        "The workspace is larger than 64 MiB. Remove some plans so it can be saved.",
      );
      return;
    }
    const edits = this.pendingEdits;
    const seq = ++this.seq;
    const payload =
      `{"revision":${this.revision},"client_id":${JSON.stringify(this.clientId)},` +
      `"seq":${seq},"state":${body}}`;
    this.sent = body;
    this.inflight++;
    // Navigation is saved silently. Even a content save gets a 400 ms grace
    // period: a local round trip must not flash an orange progress indicator.
    if (!final && !sameEdits(edits, this.savedEdits)) {
      window.clearTimeout(this.statusTimer);
      this.statusTimer = window.setTimeout(() => {
        if (this.enabled && !sameEdits(this.pendingEdits, this.savedEdits))
          this.onStatus({ kind: "saving" });
      }, 400);
    }
    try {
      const result = await api<{
        revision: number;
        saved_at: string;
        stale?: boolean;
      }>(
        "session/save",
        payload,
        undefined,
        // The keepalive limit is in bytes, not UTF-16 characters.
        final && utf8.encode(payload).length < KEEPALIVE_LIMIT,
      );
      this.revision = Math.max(this.revision, result.revision);
      if (!result.stale && seq > this.savedSeq) {
        this.savedSeq = seq;
        this.saved = body;
        this.savedEdits = edits;
      }
      this.failed = false;
      if (this.pending === this.saved)
        this.onStatus({ kind: "saved", at: result.saved_at });
      else this.schedule();
    } catch (e) {
      this.sent = "";
      if (e instanceof ApiError && e.code === "session_conflict") {
        this.pause();
        this.onConflict();
      } else
        this.fail(
          e instanceof Error ? e.message : "The workspace could not be saved.",
        );
    } finally {
      window.clearTimeout(this.statusTimer);
      this.inflight--;
    }
  }
  private fail(reason: string) {
    this.failed = true;
    this.onStatus({ kind: "error", reason });
  }
}

export async function loadSession() {
  return api<SessionLoad>("session/load", {});
}

type PlanLike = {
  origin?: "sample";
  id: string;
  name: string;
  raw: string;
  capture?: Capture;
  notes?: string;
};
/** The plans part of the snapshot, serialized separately (it is the bulk). */
export function serializePlans(plans: PlanLike[]) {
  return JSON.stringify(
    stripSecrets(
      plans.map((p) => ({
        id: p.id,
        name: p.name,
        raw: p.raw,
        capture: p.capture || null,
        notes: p.notes || "",
        ...(p.origin === "sample" ? { origin: p.origin } : {}),
      })),
    ),
  );
}
/** The whole snapshot in a fixed key order, so equal state gives equal text. */
export function serializeSession(
  s: Omit<SessionState, "schema_version" | "plans">,
  plansJson: string,
) {
  const head = JSON.stringify(
    stripSecrets({
      schema_version: SESSION_VERSION,
      mode: s.mode,
      view: s.view,
      metric: s.metric,
      active_plan_id: s.active_plan_id,
      selected_id: s.selected_id,
      focus_id: s.focus_id,
      collapsed_ids: s.collapsed_ids,
      before_id: s.before_id,
      after_id: s.after_id,
      investigation: {
        id: s.investigation.id,
        name: s.investigation.name,
        notes: s.investigation.notes,
        tags: s.investigation.tags,
        baseline_id: s.investigation.baseline_id,
        dirty: s.investigation.dirty,
      },
      teach: {
        lesson: s.teach.lesson,
        return_to_plan: s.teach.return_to_plan,
      },
      query_lab: normalizeLab(s.query_lab),
      plans: 0,
    }),
  );
  // Splice the separately serialized plans in place of the 0 placeholder.
  return head.slice(0, -2) + plansJson + "}";
}
