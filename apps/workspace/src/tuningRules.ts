/**
 * Tuning: a scorecard over the statuses myflames' Python views already
 * decide (health.py, config_review.py, schema_health.py through report.py,
 * server_checks.py). The thresholds live there, source-verified; this module
 * reads their statuses and numbers by their real keys, maps them to four
 * states, and scores only what was measured.
 *
 * States: problem, watch, ok, unknown. Python's "info" means "a fact with no
 * rule", so it is unknown unless a check says otherwise (for example the
 * slow query log on with long_query_time at 10 s or more is still on: ok).
 * Unknown never counts toward the score or the grade, and a check that does
 * not apply to this server (thread cache under a thread pool) is left out of
 * the count altogether.
 *
 * Score: ok = 100 points, watch = 50, problem = 0. A category scores the mean
 * of its measured checks; the overall score is the weighted mean of the
 * categories with at least one measured check (their weights renormalized).
 * The score is provisional, with no letter grade, until at least half of the
 * applicable checks are measured and every category has one. Any problem
 * caps the grade at C. GRADE_SCALE and PROBLEM_GRADE_CAP are the only copy of
 * these thresholds: the Tuning page renders its grade legend and reason line
 * (gradeReason) from them.
 *
 * Remediation statements are text to copy or open in the Query editor;
 * nothing on the Tuning page runs them. Global changes are SET PERSIST on
 * MySQL; on MariaDB (no SET PERSIST: its sql_yacc.yy has no PERSIST) they are
 * SET GLOBAL plus an option-file line to copy, because SET GLOBAL is lost on
 * restart. Per-session limits (tmp_table_size, sort_buffer_size) are SET
 * SESSION on both, as health.py advises.
 *
 * Source citations: M/ = mysql-server 9.7 trunk (tags named where older),
 * Ma/ = mariadb-server 13.0 trunk (tags named where older).
 */

import type {
  AnalysisFinding,
  AnalysisReport,
  ConfigReport,
  HealthDiagnostic,
  HealthMetric,
  HealthReport,
  TablesReport,
} from "./api";

// ---- Types ---------------------------------------------------------------------

export type TuningStatus = "problem" | "watch" | "ok" | "unknown";
type PyStatus = "ok" | "watch" | "problem" | "info" | "unknown";

export type TuningCategoryKey = "innodb" | "queries" | "connections" | "tables" | "security";

export interface TuningCategoryConfig {
  key: TuningCategoryKey;
  name: string;
  weight: number;
  description: string;
}

export const TUNING_CATEGORIES: Record<TuningCategoryKey, TuningCategoryConfig> = {
  innodb: {
    key: "innodb",
    name: "Buffer pool and redo",
    weight: 0.3,
    description: "Buffer pool pressure, dirty pages, waits for a free page and the redo log size.",
  },
  queries: {
    key: "queries",
    name: "Queries",
    weight: 0.25,
    description: "Joins without an index, temporary tables on disk, sort merge passes and the slow query log.",
  },
  connections: {
    key: "connections",
    name: "Connections and threads",
    weight: 0.2,
    description: "Connection limit, thread cache and connections that ended without closing.",
  },
  tables: {
    key: "tables",
    name: "Tables and caches",
    weight: 0.15,
    description: "Table open cache, table definition cache, tables without a primary key and MyISAM tables.",
  },
  security: {
    key: "security",
    name: "Security",
    weight: 0.1,
    description: "Accounts without a password, anonymous accounts, root from any host and encrypted transport.",
  },
};
export const CATEGORY_ORDER: TuningCategoryKey[] = ["innodb", "queries", "connections", "tables", "security"];

/** Points per measured status. */
export const POINTS: Record<Exclude<TuningStatus, "unknown">, number> = { ok: 100, watch: 50, problem: 0 };

/** Where a check's numbers came from. */
export type TuningSourceView = "health" | "config" | "tables" | "report";
export interface TuningSource {
  view: TuningSourceView;
  label: string;
  takenAt: string | null;
}
/** The view that would measure a check that is not measured yet. */
export type TuningMeasureWith = "health" | "config" | "tables" | "report";

export interface TuningEvidence {
  label: string;
  text: string;
}
export interface TuningSetting {
  name: string;
  value: string;
  default?: string | null;
}
export interface TuningFix {
  /** What to do, in plain words. */
  text: string;
  /** Statements to copy or open in the Query editor; never run from this page. */
  sql?: string;
  /** An option-file line to copy (MariaDB, or a setting that needs a restart). */
  optionFile?: string;
  caveat?: string;
}

export interface TuningCheck {
  key: string;
  title: string;
  category: TuningCategoryKey;
  status: TuningStatus;
  /** False when the check does not apply to this server; it is not counted. */
  applies: boolean;
  /** The measured value, short ("78.6% hit ratio"), or "Not measured". */
  value: string;
  /** What this server shows. */
  verdict: string;
  /** Why it is unknown, and how to measure it. */
  unknownReason?: string;
  /** The threshold in words (from the Python rule). */
  rule: string | null;
  /** Why it matters. */
  why: string;
  evidence: TuningEvidence[];
  settings: TuningSetting[];
  /** What to do: only on problem and watch. */
  fix?: TuningFix;
  source: TuningSource | null;
  measureWith: TuningMeasureWith[];
  docs?: string;
}

export interface TuningCategoryResult {
  key: TuningCategoryKey;
  name: string;
  weight: number;
  /** Mean points of the measured checks, or null when none was measured. */
  score: number | null;
  status: TuningStatus;
  measured: number;
  total: number;
  checks: TuningCheck[];
}

export type TuningLabel = "Good" | "Needs attention" | "Problems found" | "Not enough data";
export type TuningGrade = "A" | "B" | "C" | "D" | "F";

export interface TuningReport {
  schema_version: "tuning-1.0";
  /** 0 to 100, or null when nothing was measured. */
  score: number | null;
  /** Fewer than half the checks measured, or a category with none: no grade. */
  provisional: boolean;
  grade: TuningGrade | null;
  label: TuningLabel;
  /** The worst measured status, or unknown when nothing was measured. */
  status: TuningStatus;
  measured: number;
  total: number;
  counts: Record<TuningStatus, number>;
  categories: TuningCategoryResult[];
  checks: TuningCheck[];
  /** Problem and watch checks, problems first, then by category weight. */
  recommendations: TuningCheck[];
  /** product: MySQL, Percona Server, Percona XtraDB Cluster or MariaDB (Percona keeps engine "mysql"). */
  server: { engine: "mysql" | "mariadb"; version: string | null; product?: string | null };
  /** The rule behind the score, in words (shown and exported). */
  scoring: string;
  generatedAt: string;
}

/** report-1.0 with the health_metrics list report.py adds (absent in older reports). */
export type AnalysisReportInput = AnalysisReport & { health_metrics?: HealthMetric[] };

export interface TuningInput {
  report?: AnalysisReportInput | null;
  health?: HealthReport | null;
  config?: ConfigReport | null;
  tables?: TablesReport | null;
  /** ISO time for generatedAt (tests). */
  now?: string;
}

/** The letter grades, best first: a score at or above `min` earns the grade (letterGrade). Scores are whole numbers. */
export const GRADE_SCALE: ReadonlyArray<{ grade: TuningGrade; min: number }> = [
  { grade: "A", min: 90 },
  { grade: "B", min: 80 },
  { grade: "C", min: 70 },
  { grade: "D", min: 50 },
  { grade: "F", min: 0 },
];
/** Any problem caps the grade here, whatever the score. */
export const PROBLEM_GRADE_CAP: TuningGrade = "C";

/** The scores a grade covers: "90 and above", "80 to 89", "below 50". */
export function gradeRangeText(grade: TuningGrade): string {
  const i = GRADE_SCALE.findIndex((g) => g.grade === grade);
  const { min } = GRADE_SCALE[i];
  if (i === 0) return `${min} and above`;
  const max = GRADE_SCALE[i - 1].min - 1;
  if (i === GRADE_SCALE.length - 1) return `below ${max + 1}`;
  return `${min} to ${max}`;
}

/** "A 90 and above, B 80 to 89, C 70 to 79, D 50 to 69, F below 50". */
export function gradeScaleText(): string {
  return GRADE_SCALE.map((g) => `${g.grade} ${gradeRangeText(g.grade)}`).join(", ");
}

/** How the score is computed, in words. */
export const SCORE_RULE =
  "Each measured check scores 100 when OK, 50 to watch and 0 for a problem. A category scores the mean of its " +
  "measured checks, and the overall score weights the categories that have one (Buffer pool and redo 30%, " +
  "Queries 25%, Connections and threads 20%, Tables and caches 15%, Security 10%). Checks that were not measured " +
  "are left out.";
/** How the score becomes a grade, in words. */
export const GRADE_RULE =
  `Grades: ${gradeScaleText()}. A problem caps the grade at ${PROBLEM_GRADE_CAP}. The score is provisional, with ` +
  "no grade, until at least half the checks are measured and every category has one.";
export const SCORING_RULE = `${SCORE_RULE} ${GRADE_RULE}`;

// ---- Formatting -------------------------------------------------------------------

export function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "?";
  const rounded = Math.abs(n) >= 100 || Number.isInteger(n) ? Math.round(n) : Math.round(n * 100) / 100;
  return rounded.toLocaleString("en-US");
}

/** Mirrors perf_common._bytes_text: "0 B", "512 B", "64.0 MiB". */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "?";
  if (bytes < 1024) return `${Math.max(0, Math.round(bytes))} B`;
  const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(1)} ${units[i]}`;
}

/** Mirrors health.format_value: a metric's headline value with its unit. */
export function formatMetric(m: Pick<HealthMetric, "value" | "unit">): string {
  const { value, unit } = m;
  if (value === null || value === undefined) return "—";
  if (unit === "%") return `${value.toFixed(1)}%`;
  if (unit === "B/s") return `${formatBytes(value)}/s`;
  if (unit === "/s") return `${formatCount(value)}/s`;
  if (unit === "pages/s" || unit === "ms/s") return `${formatCount(value)} ${unit}`;
  if (unit === "ms") return `${formatCount(value)} ms`;
  return formatCount(value);
}

function plural(n: number, one: string, many: string) {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** Drops the " Rule of thumb: <rule>." tail health.py and server_checks.py add to an observation. */
function observation(text: string | null | undefined, rule: string | null | undefined): string {
  const t = (text || "").trim();
  const at = t.indexOf(" Rule of thumb: ");
  if (at > 0 && (!rule || t.slice(at).includes(rule.slice(0, 20)))) return t.slice(0, at).trim();
  return t;
}

// ---- Versions ----------------------------------------------------------------------

/** "8.4.11" -> [8, 4, 11]; "11.4.13-MariaDB-ubu2404" -> [11, 4, 13]; "5.5.5-10.6.25-MariaDB" -> [10, 6, 25]. */
export function versionTuple(version: string | null | undefined): number[] | null {
  if (!version) return null;
  const text = version.replace(/^5\.5\.5-/, "");
  const match = text.match(/^(\d+)\.(\d+)(?:\.(\d+))?/);
  if (!match) return null;
  return [Number(match[1]), Number(match[2]), Number(match[3] || 0)];
}
function atLeast(tuple: number[] | null, want: number[]): boolean | null {
  if (!tuple) return null;
  for (let i = 0; i < want.length; i++) {
    const a = tuple[i] || 0;
    if (a !== want[i]) return a > want[i];
  }
  return true;
}

// ---- Normalized Python evidence -------------------------------------------------------

interface Item {
  status: PyStatus;
  /** Short value (headline). */
  value: string;
  verdict: string;
  rule: string | null;
  why: string;
  caveat: string | null;
  advice: string | null;
  actionSql: string | null;
  actionConfig: string | null;
  numbers: TuningEvidence[];
  settings: TuningSetting[];
  state: string | null;
  inputs: Record<string, number | null>;
  value_number: number | null;
  source: TuningSource;
}

const PY_STATUSES: PyStatus[] = ["ok", "watch", "problem", "info", "unknown"];
function pyStatus(s: string | null | undefined): PyStatus {
  return (PY_STATUSES as string[]).includes(s || "") ? (s as PyStatus) : "unknown";
}
function timeOf(iso: string | null | undefined): number {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? -Infinity : t;
}
function sourceOf(view: TuningSourceView, takenAt: string | null | undefined): TuningSource {
  const names: Record<TuningSourceView, string> = {
    health: "Health",
    config: "Parameters",
    tables: "Tables",
    report: "Full analysis",
  };
  return { view, label: names[view], takenAt: takenAt || null };
}
function settingOf(s: { name: string; value?: string | null; display?: string | null; default?: string | null }): TuningSetting {
  return { name: s.name, value: String(s.display ?? s.value ?? ""), default: s.default ?? null };
}

function metricItem(m: HealthMetric, source: TuningSource): Item {
  const value = formatMetric(m);
  return {
    status: pyStatus(m.status),
    value,
    verdict: observation(m.why, m.rule) || value,
    rule: m.rule || null,
    why: m.explain || "",
    caveat: null,
    advice: null,
    actionSql: null,
    actionConfig: null,
    numbers: [{ label: m.label || m.key, text: value }, ...(m.detail ? [{ label: "Detail", text: m.detail }] : [])],
    settings: [],
    state: null,
    inputs: m.inputs || {},
    value_number: m.value ?? null,
    source,
  };
}
function diagItem(d: HealthDiagnostic, source: TuningSource): Item {
  return {
    status: pyStatus(d.status),
    value: d.headline || "",
    verdict: d.verdict || d.headline || "",
    rule: d.rule || null,
    why: d.why || "",
    caveat: d.recommendation?.caveat || null,
    advice: d.recommendation?.text || null,
    actionSql: null,
    actionConfig: null,
    numbers: (d.numbers || []).filter(Boolean).map((n) => ({ label: n.label, text: n.text })),
    settings: (d.settings || []).filter(Boolean).map(settingOf),
    state: d.state ?? null,
    inputs: Object.fromEntries((d.numbers || []).filter(Boolean).map((n) => [n.key, n.value ?? null])),
    value_number: d.value ?? null,
    source,
  };
}
function findingItem(f: AnalysisFinding, source: TuningSource): Item {
  return {
    status: pyStatus(f.status),
    value: f.headline || "",
    verdict: f.verdict || f.headline || "",
    rule: f.rule || null,
    why: f.why || "",
    caveat: f.caveat || null,
    advice: f.action?.text || null,
    actionSql: f.action?.sql || null,
    actionConfig: f.action?.config || null,
    numbers: (f.numbers || []).map((n) => ({ label: n.label, text: n.text })),
    settings: (f.settings || []).filter((s) => s && s.name).map((s) => settingOf({ ...s, display: s.display ?? s.value })),
    state: f.state ?? null,
    inputs: Object.fromEntries((f.numbers || []).filter((n) => n.value !== undefined).map((n) => [n.label, n.value ?? null])),
    value_number: f.impact ?? null,
    source,
  };
}

class Ctx {
  readonly engine: "mysql" | "mariadb";
  readonly version: string | null;
  /** The product the reports name (Percona Server keeps engine "mysql": MySQL rules apply). */
  readonly product: string | null;
  readonly tuple: number[] | null;
  readonly report: AnalysisReportInput | null;
  readonly health: HealthReport | null;
  readonly config: ConfigReport | null;
  readonly tables: TablesReport | null;
  private readonly findings = new Map<string, AnalysisFinding>();

  constructor(input: TuningInput) {
    this.report = input.report ?? null;
    this.health = input.health ?? null;
    this.config = input.config ?? null;
    this.tables = input.tables ?? null;
    const engine =
      this.report?.header?.server?.engine || this.report?.engine || this.health?.server?.engine || this.health?.engine ||
      this.config?.engine || this.tables?.engine || "mysql";
    this.engine = engine.toLowerCase().includes("mariadb") ? "mariadb" : "mysql";
    this.version =
      this.report?.header?.server?.version || this.report?.server_version || this.health?.server?.version ||
      this.health?.server_version || this.config?.server_version || this.tables?.server_version || null;
    this.product =
      this.report?.header?.server?.product || this.report?.server_product || this.health?.server_product ||
      this.config?.server_product || this.tables?.server_product || null;
    this.tuple = versionTuple(this.version);
    for (const f of this.report?.findings || []) this.findings.set(f.id, f);
  }

  get mariadb() {
    return this.engine === "mariadb";
  }
  atLeast(want: number[]) {
    return atLeast(this.tuple, want);
  }
  /** When the Full analysis read a view (its own taken_at), else the report's. */
  reportTaken(view: string): string | null {
    const v = this.report?.header?.views?.find((x) => x.key === view);
    if (v && (v.status === "failed" || v.status === "skipped")) return null;
    return v?.taken_at || this.report?.taken_at || null;
  }
  reportRan(view: string): boolean {
    const v = this.report?.header?.views?.find((x) => x.key === view);
    return !!this.report && (!v || (v.status !== "failed" && v.status !== "skipped"));
  }
  finding(id: string): AnalysisFinding | null {
    return this.findings.get(id) ?? null;
  }
  /** The newer of the view's own item and the Full analysis's (the view wins a tie). */
  private newer(own: Item | null, fromReport: Item | null): Item | null {
    if (!own) return fromReport;
    if (!fromReport) return own;
    return timeOf(fromReport.source.takenAt) > timeOf(own.source.takenAt) ? fromReport : own;
  }

  metric(key: string): Item | null {
    const m = this.health?.metrics?.find((x) => x.key === key);
    const own = m ? metricItem(m, sourceOf("health", this.health?.taken_at)) : null;
    let fromReport: Item | null = null;
    if (this.reportRan("health")) {
      const src = sourceOf("report", this.reportTaken("health"));
      const listed = this.report?.health_metrics?.find((x) => x.key === key);
      if (listed) fromReport = metricItem(listed, src);
      else {
        // Older reports keep only watch and problem metrics (report._health_findings):
        // an absent finding is not evidence of ok.
        const f = this.finding(`health.${key}`);
        if (f) fromReport = findingItem(f, src);
      }
    }
    return this.newer(own, fromReport);
  }
  diag(key: string): Item | null {
    const d = this.health?.diagnostics?.find((x) => x.key === key);
    const own = d ? diagItem(d, sourceOf("health", this.health?.taken_at)) : null;
    const f = this.reportRan("health") ? this.finding(`health.${key}`) : null;
    return this.newer(own, f ? findingItem(f, sourceOf("report", this.reportTaken("health"))) : null);
  }
  configCheck(key: string): Item | null {
    const c = this.config?.checks?.find((x) => x.key === key);
    const own = c
      ? {
          status: pyStatus(c.status),
          value: c.value || "",
          verdict: observation(c.why, c.rule) || c.value || "",
          rule: c.rule || null,
          why: c.explain || "",
          caveat: c.caveat || null,
          advice: c.action?.text || null,
          actionSql: c.action?.sql || null,
          actionConfig: c.action?.config || null,
          numbers: [],
          settings: (c.settings || []).filter(Boolean).map(settingOf),
          state: null,
          inputs: {},
          value_number: null,
          source: sourceOf("config", this.config?.taken_at),
        }
      : null;
    const f = this.reportRan("config") ? this.finding(`config.${key}`) : null;
    return this.newer(own, f ? findingItem(f, sourceOf("report", this.reportTaken("config"))) : null);
  }
  serverCheck(key: string): Item | null {
    const f = this.reportRan("checks") ? this.finding(`checks.${key}`) : null;
    return f ? findingItem(f, sourceOf("report", this.reportTaken("checks"))) : null;
  }
  /** A variable's value from Parameters (every global variable), the checks' settings, or Health. */
  variable(name: string): string | null {
    const lower = name.toLowerCase();
    const p = this.config?.parameters?.items?.find((x) => x.name.toLowerCase() === lower);
    if (p && p.value !== null) return p.value;
    for (const c of this.config?.checks || [])
      for (const s of c.settings || []) if (s && s.name.toLowerCase() === lower && s.value != null) return String(s.value);
    for (const d of this.health?.diagnostics || [])
      for (const s of d.settings || []) if (s && s.name.toLowerCase() === lower && s.value != null) return String(s.value);
    for (const f of this.report?.findings || [])
      for (const s of f.settings || []) if (s && s.name.toLowerCase() === lower && s.value != null) return String(s.value);
    return null;
  }
  /** Parameters was read (config-1.0 with the parameters-1.0 catalog). */
  get parameters() {
    return this.config?.parameters?.items ? this.config.parameters : null;
  }
}

// ---- Statements ------------------------------------------------------------------------

/**
 * A global change for this engine. MySQL: SET PERSIST (runs now and survives a
 * restart). MariaDB: SET GLOBAL plus the option-file line to copy, since MariaDB
 * has no SET PERSIST. *dynamic* false: no statement, the option-file line and a
 * restart (SET PERSIST_ONLY is not offered: the restart is the point).
 */
export function globalChange(
  engine: "mysql" | "mariadb",
  name: string,
  value: string,
  dynamic = true,
): { sql?: string; optionFile?: string } {
  const line = `${name} = ${value}  # under [mysqld]`;
  if (!dynamic) return { optionFile: line };
  if (engine === "mysql") return { sql: `SET PERSIST ${name} = ${value};` };
  return { sql: `SET GLOBAL ${name} = ${value};`, optionFile: line };
}
function joinChanges(changes: { sql?: string; optionFile?: string }[]): { sql?: string; optionFile?: string } {
  const sql = changes.map((c) => c.sql).filter(Boolean).join("\n");
  const optionFile = changes.map((c) => c.optionFile).filter(Boolean).join("\n");
  return { ...(sql ? { sql } : {}), ...(optionFile ? { optionFile } : {}) };
}
/** The first integer assigned in a Python action ("SET GLOBAL x = 123;"), or null. */
function assigned(sql: string | null, name: string): number | null {
  if (!sql) return null;
  const m = sql.match(new RegExp(`${name}\\s*=\\s*(\\d+)`));
  return m ? Number(m[1]) : null;
}

// ---- Check builder -------------------------------------------------------------------------

const RANK: Record<TuningStatus, number> = { problem: 0, watch: 1, ok: 2, unknown: 3 };

interface Spec {
  key: string;
  title: string;
  category: TuningCategoryKey;
  items: (Item | null)[];
  /** Which views measure it, and the sentence shown when none of them was read. */
  measureWith: TuningMeasureWith[];
  missing: string;
  /** Python's info for this check: "ok" or "unknown" (the default), or a function of the item. */
  info?: "ok" | "unknown" | ((item: Item) => TuningStatus);
  /** Not applicable on this server (not counted). */
  notApplicable?: (item: Item) => boolean;
  value?: (item: Item, all: Item[]) => string;
  verdict?: (item: Item, all: Item[]) => string;
  why?: string;
  fix?: (item: Item, status: TuningStatus, all: Item[]) => TuningFix | undefined;
  evidence?: (item: Item, all: Item[]) => TuningEvidence[];
  docs?: string;
}

function mapStatus(item: Item, info: Spec["info"]): TuningStatus {
  if (item.status === "info") {
    if (typeof info === "function") return info(item);
    return info === "ok" ? "ok" : "unknown";
  }
  return item.status as TuningStatus;
}

function build(spec: Spec): TuningCheck {
  const items = spec.items.filter((x): x is Item => !!x);
  const base = {
    key: spec.key,
    title: spec.title,
    category: spec.category,
    measureWith: spec.measureWith,
    ...(spec.docs ? { docs: spec.docs } : {}),
  };
  if (!items.length) {
    return {
      ...base,
      status: "unknown",
      applies: true,
      value: "Not measured",
      verdict: "",
      unknownReason: spec.missing,
      rule: null,
      why: spec.why || "",
      evidence: [],
      settings: [],
      source: null,
    };
  }
  if (spec.notApplicable && items.every((i) => spec.notApplicable!(i))) {
    const item = items[0];
    return {
      ...base,
      status: "unknown",
      applies: false,
      value: "Does not apply",
      verdict: item.verdict,
      unknownReason: item.verdict,
      rule: item.rule,
      why: spec.why || item.why,
      evidence: item.numbers,
      settings: item.settings,
      source: item.source,
    };
  }
  const ranked = items
    .map((item) => ({ item, status: mapStatus(item, spec.info) }))
    .sort((a, b) => RANK[a.status] - RANK[b.status]);
  const { item, status } = ranked[0];
  const ordered = ranked.map((r) => r.item);
  const evidence = spec.evidence ? spec.evidence(item, ordered) : item.numbers;
  const settings: TuningSetting[] = [];
  for (const i of ordered) for (const s of i.settings) if (!settings.some((x) => x.name === s.name)) settings.push(s);
  return {
    ...base,
    status,
    applies: true,
    value: status === "unknown" ? "Not measured" : spec.value ? spec.value(item, ordered) : item.value,
    verdict: spec.verdict ? spec.verdict(item, ordered) : item.verdict,
    ...(status === "unknown"
      ? { unknownReason: item.verdict || `${item.source.label} did not judge this.` }
      : {}),
    rule: item.rule,
    why: spec.why || item.why,
    evidence,
    settings,
    ...(status === "problem" || status === "watch" ? { fix: spec.fix?.(item, status, ordered) } : {}),
    source: item.source,
  };
}

const LOAD_HEALTH = "Load Health, or run the full analysis, to measure this.";
const LOAD_CONFIG = "Open Parameters, or run the full analysis, to read this setting.";
const LOAD_TABLES = "Load Tables, or run the full analysis, to check the tables.";
const RUN_ANALYSIS = "Run the full analysis to read the accounts (it needs SELECT on mysql.user).";

// ---- Buffer pool and redo (30%) -------------------------------------------------------------

/** health.py _diag_buffer_pool (full / warming / room; disk reads >= 10/s and the hit ratio). */
function bufferPool(ctx: Ctx): TuningCheck {
  const diag = ctx.diag("buffer_pool");
  const hit = ctx.metric("bp_hit_ratio");
  return build({
    key: "buffer_pool",
    title: "Buffer pool",
    category: "innodb",
    items: [diag],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // Python's info here is "not full": with free pages left nothing is evicted
    // to make room (state "room"), so that is ok; "warming" cannot be judged yet
    // (health.py: "Reads from disk are expected while it warms").
    info: (i) => (i.state === "room" ? "ok" : "unknown"),
    evidence: (i) =>
      hit && hit.status !== "unknown" && hit.value_number !== null
        ? [{ label: "Hit ratio in the window", text: hit.value }, ...i.numbers]
        : i.numbers,
    fix: (i) => {
      const change = globalChange(ctx.engine, "innodb_buffer_pool_size", "<bytes>");
      const cap =
        ctx.mariadb && ctx.variable("innodb_buffer_pool_size_max") !== null
          ? " MariaDB grows it online only up to innodb_buffer_pool_size_max (0 means the startup size); beyond that it needs a restart."
          : "";
      return {
        text: (i.advice || "Look for queries that scan large tables first (Top statements); then consider a larger innodb_buffer_pool_size.") + cap,
        ...change,
        caveat: i.caveat || undefined,
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/innodb-buffer-pool-resize.html",
  });
}

/** health.py bp_dirty: watch at innodb_max_dirty_pages_pct, never a problem (DIRTY_OF_MAX). */
function dirtyPages(ctx: Ctx): TuningCheck {
  return build({
    key: "dirty_pages",
    title: "Dirty pages",
    category: "innodb",
    items: [ctx.metric("bp_dirty")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // No io_capacity advice: MySQL 8.4's innodb_io_capacity default is 10000
    // (M/storage/innobase/handler/ha_innodb.cc, mysql-8.4.10 tag), so a fixed
    // "2000" would cut flushing; MariaDB's lwm is 0, so sitting near the maximum
    // is normal there (Ma/storage/innobase/handler/ha_innodb.cc:19284-19290).
    fix: () => ({
      text:
        "A high share alone is not a stall. Check Waits for a free page and Redo log size: when both are OK, " +
        "InnoDB is keeping up.",
    }),
  });
}

/** health.py bp_wait_free: watch on any wait in the window, problem at 1 per second (SHOULD_BE_ZERO). */
function freePageWaits(ctx: Ctx): TuningCheck {
  // innodb_lru_scan_depth defaults: MySQL 1024 per buffer pool instance
  // (mysql-8.4.10 ha_innodb.cc:22676-22679), MariaDB 1536 (Ma/storage/innobase/
  // handler/ha_innodb.cc:19468-19471); dynamic on both (no READONLY flag).
  const depthDefault = ctx.mariadb ? "1536" : "1024 per buffer pool instance";
  return build({
    key: "free_page_waits",
    title: "Waits for a free page",
    category: "innodb",
    items: [ctx.metric("bp_wait_free")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    fix: () => {
      const current = ctx.variable("innodb_lru_scan_depth");
      return {
        text:
          `Sessions waited for a free page: the page cleaner did not keep enough free pages ahead of reads. A larger buffer pool helps ` +
          `when Buffer pool shows it is full; a deeper innodb_lru_scan_depth (now ${current ?? "unknown"}, default ` +
          `${depthDefault}) makes the page cleaner keep more free pages.`,
        ...globalChange(ctx.engine, "innodb_lru_scan_depth", "<pages>"),
        caveat:
          "Raise it only when the storage has spare I/O capacity: a deeper scan makes the page cleaner read and write more.",
      };
    },
  });
}

/** health.py _diag_redo (window) and config_review._redo (average since startup, REDO_HOURS = 1). */
function redoLog(ctx: Ctx): TuningCheck {
  const diag = ctx.diag("redo");
  const cfg = ctx.configCheck("redo");
  return build({
    key: "redo_log",
    title: "Redo log size",
    category: "innodb",
    items: [diag, cfg],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    fix: (_item, _status, all) => {
      // Only Python's target: max(an hour at the measured rate, the capacity) rounded up to 256 MiB,
      // offered only when it is larger than the capacity (health.py:1480-1482; config_review._redo
      // recommends only below an hour). Health also rates watch or problem from the checkpoint age
      // and from Innodb_log_waits, which point at flushing and innodb_log_buffer_size, not the size
      // (health.py:1471-1475): then no size is offered.
      const name = ctx.mariadb ? "innodb_log_file_size" : "innodb_redo_log_capacity";
      let target: number | null = null;
      for (const i of all) {
        const fromPython = assigned(i.actionSql, name) ?? assigned(i.advice, name);
        if (fromPython) target = Math.max(target || 0, fromPython);
      }
      const capacityNumber = all.map((i) => i.inputs.capacity).find((c) => typeof c === "number" && c > 0);
      const capacity =
        (typeof capacityNumber === "number" ? capacityNumber : null) ??
        ctx.health?.server?.redo_capacity_bytes ??
        (Number(ctx.variable(name)) || null);
      if (target !== null && capacity !== null && target <= capacity) target = null;
      const caveat =
        "A short window may be a burst or a lull; compare several refreshes. A larger redo log uses more disk and makes crash recovery longer.";
      const legacy = !ctx.mariadb && ctx.atLeast([8, 0, 30]) === false;
      const legacySized = legacy && all.some((i) => /innodb_log_file_size x innodb_log_files_in_group/.test(i.advice || ""));
      if (target === null && !legacySized) {
        const logWaits = all.some(
          (i) => (typeof i.inputs.log_waits === "number" && i.inputs.log_waits > 0) || /waits for space in the log buffer/.test(i.verdict),
        );
        return {
          text: logWaits
            ? "Waits for space in the log buffer raised the status. They point at innodb_log_buffer_size, not the redo log size."
            : "The checkpoint age raised the status, while the redo capacity already covers the measured rate. InnoDB flushed harder to free redo space; compare several refreshes before changing the size.",
          caveat,
        };
      }
      const value = target ? String(target) : "<bytes>";
      const size = target ? ` (${formatBytes(target)})` : "";
      if (!ctx.mariadb) {
        // innodb_redo_log_capacity exists from 8.0.30 (mysql-8.0.30 ha_innodb.cc:22503) and is dynamic
        // (M/storage/innobase/handler/ha_innodb.cc:22838-22843); before it, innodb_log_file_size is read-only.
        if (legacy)
          return {
            text: `About an hour of redo at the measured rate${size}. This MySQL has no innodb_redo_log_capacity: set the log files in the option file and restart.`,
            optionFile: `innodb_log_file_size = <bytes per file>  # under [mysqld]\ninnodb_log_files_in_group = 2`,
            caveat,
          };
        return {
          text: `About an hour of redo at the measured rate${size}, rounded up to 256 MiB. It resizes online.`,
          ...globalChange("mysql", "innodb_redo_log_capacity", value),
          caveat,
        };
      }
      // MariaDB: innodb_log_file_size is read-only up to 10.8 (mariadb-10.6.25 and 10.8.2
      // ha_innodb.cc PLUGIN_VAR_READONLY) and dynamic from 10.9.1 (ha_innodb.cc:19344-19348).
      if (ctx.atLeast([10, 9, 0]) === false)
        return {
          text: `About an hour of redo at the measured rate${size}. On MariaDB before 10.9 innodb_log_file_size is read-only: set it in the option file and restart.`,
          optionFile: `innodb_log_file_size = ${value}  # under [mysqld]`,
          caveat,
        };
      return {
        text: `About an hour of redo at the measured rate${size}, rounded up to 256 MiB. SET GLOBAL resizes it online and waits until the resize finishes.`,
        ...globalChange("mariadb", "innodb_log_file_size", value),
        caveat,
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/innodb-redo-log.html",
  });
}

// ---- Queries (25%) --------------------------------------------------------------------------

/** health.py select_full_join: watch on any, problem at 10 per second (JOINS_WITHOUT_INDEX). */
function joinsWithoutIndex(ctx: Ctx): TuningCheck {
  return build({
    key: "joins_without_index",
    title: "Joins without an index",
    category: "queries",
    items: [ctx.metric("select_full_join")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // Select_full_join counts a full scan of a later table in a join; on MySQL a
    // hash join's inner table counts too and is read once, not once per row
    // (M/sql/join_optimizer/access_path.cc:1906-1925; Ma/sql/sql_select.cc:16495-16565).
    why:
      "Select_full_join counts joins where a later table was read by a full scan because no index matched the " +
      "join. On MySQL a hash join's inner table counts too; it is read once rather than once per row. Without an " +
      "index the server reads every row of that table for the join.",
    fix: () => ({
      text:
        "Find the statements in Top statements ordered by joins without an index, then add an index on the join " +
        "columns of the table read by full scan. Explain the statement before and after.",
      caveat: "Every index adds work to writes on that table. Small lookup tables are often fine to scan.",
    }),
  });
}

/** health.py _diag_tmp_tables (window) and config_review._tmp_tables (since startup, 25% with at least 100). */
function tempTables(ctx: Ctx): TuningCheck {
  return build({
    key: "temp_tables",
    title: "Temporary tables on disk",
    category: "queries",
    items: [ctx.diag("tmp_tables"), ctx.configCheck("tmp_tables")],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    fix: (item, _status, all) => {
      // Which limit applies depends on internal_tmp_mem_storage_engine (MySQL):
      // TempTable reads tmp_table_size as a per-table limit from 8.0.28 (mysql-8.0.28
      // storage/temptable/src/handler.cc:132; absent at 8.0.27) and temptable_max_ram for all of
      // its tables (global, dynamic: M/sql/sys_vars.cc:5188-5195); MEMORY and MariaDB use the
      // smaller of tmp_table_size and max_heap_table_size (M/sql/sql_tmp_table.cc:2235-2241,
      // Ma/sql/sql_select.cc:22662-22668).
      const findFirst =
        "Find the statements with the temporary-table-on-disk flag in Top statements; an index that avoids the temporary table helps more than a bigger limit.";
      const storage = ctx.mariadb ? null : ctx.variable("internal_tmp_mem_storage_engine");
      const temptable = !!storage && storage.toLowerCase() === "temptable";
      const shared =
        "Every TempTable table shares temptable_max_ram: it is one memory budget for the whole server, so raise it only with memory to spare.";
      if (ctx.mariadb)
        return {
          text: item.advice || findFirst,
          sql: "SET SESSION tmp_memory_table_size = <bytes>;\nSET SESSION max_heap_table_size = <bytes>;",
          caveat: item.caveat || undefined,
        };
      if (!storage)
        return {
          text: `${item.advice || findFirst} Read internal_tmp_mem_storage_engine first (Parameters): it decides which limit applies.`,
          caveat: item.caveat || undefined,
        };
      if (!temptable)
        return {
          text: item.advice || findFirst,
          sql: "SET SESSION tmp_table_size = <bytes>;\nSET SESSION max_heap_table_size = <bytes>;",
          caveat: item.caveat || undefined,
        };
      // MySQL 9.1+: health.py reports which limit was hit (hit_tmp_table_size, hit_max_ram); when only
      // TempTable's total memory ran out, temptable_max_ram is the limit (health.py:1907-1909).
      const hits = (i: Item, key: string, label: string) => {
        const v = i.inputs[key] ?? i.inputs[label];
        return typeof v === "number" ? v : null;
      };
      const ramOnly =
        all.some((i) => {
          const ram = hits(i, "hit_max_ram", "Hit temptable_max_ram (all TempTable memory)");
          const perTable = hits(i, "hit_tmp_table_size", "Hit tmp_table_size (per table)");
          return ram !== null && ram > 0 && !(perTable !== null && perTable > 0);
        }) || all.some((i) => /temptable_max_ram \(global\) is the limit/.test(i.advice || ""));
      if (ramOnly)
        return {
          text: `${findFirst} Here TempTable's total memory ran out, so temptable_max_ram (global) is the limit to look at, not tmp_table_size.`,
          ...globalChange("mysql", "temptable_max_ram", "<bytes>"),
          caveat: shared,
        };
      if (ctx.atLeast([8, 0, 28]) === false)
        return {
          text: `${findFirst} Before MySQL 8.0.28 TempTable has no per-table limit; temptable_max_ram (global) decides when its tables move to disk.`,
          ...globalChange("mysql", "temptable_max_ram", "<bytes>"),
          caveat: shared,
        };
      return {
        text: item.advice || findFirst,
        sql: "SET SESSION tmp_table_size = <bytes>;",
        caveat: item.caveat || undefined,
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/internal-temporary-tables.html",
  });
}

/** health.py _diag_sorts: Sort_merge_passes in moderation (watch 1/s, problem 10/s, at least 5). */
function sortMergePasses(ctx: Ctx): TuningCheck {
  return build({
    key: "sort_merge_passes",
    title: "Sort merge passes",
    category: "queries",
    items: [ctx.diag("sorts")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    fix: (item) => ({
      text:
        item.advice ||
        "Find the sorting statements in Top statements first: an index in ORDER BY or GROUP BY order removes the sort.",
      sql: "SET SESSION sort_buffer_size = <bytes>;",
      caveat: item.caveat || "A large global sort_buffer_size lets every concurrent sort use up to that much memory.",
    }),
  });
}

/** config_review._slow_log: watch when off; info when on with long_query_time >= 10 s. */
function slowLog(ctx: Ctx): TuningCheck {
  return build({
    key: "slow_log",
    title: "Slow query log",
    category: "queries",
    items: [ctx.configCheck("slow_log")],
    measureWith: ["config", "report"],
    missing: LOAD_CONFIG,
    // On with a 10 s threshold is still on: Python only declines to add a rule.
    info: "ok",
    why:
      "The slow query log records statements that ran longer than long_query_time. Slow_queries counts them " +
      "even when the log is off, but only the log records each slow statement with its own text and timing.",
    fix: () => {
      // MariaDB's log_slow_query (slow_query_log is its alias) is copied by each connection when it
      // connects (Ma/sql/sys_vars.cc:5693-5710), so SET GLOBAL reaches new connections only.
      // long_query_time is per session on both engines.
      const changes = joinChanges([
        globalChange(ctx.engine, "slow_query_log", "ON"),
        globalChange(ctx.engine, "long_query_time", "<seconds>"),
      ]);
      return {
        text:
          "The slow query log is off, so slow statements are not written to a log. Turn it on with a threshold " +
          "that suits this server (long_query_time defaults to 10 s).",
        ...changes,
        caveat:
          "A low threshold on a busy server writes a lot of log. A new long_query_time applies to new connections only" +
          (ctx.mariadb
            ? ", and so does turning the log on: each MariaDB connection copies the setting when it connects, so pooled connections keep the old one."
            : "."),
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/slow-query-log.html",
  });
}

// ---- Connections and threads (20%) -------------------------------------------------------------

/**
 * health.py connections_used (open now, 75% / 90%, refusals in the window) and
 * config_review._connections (peak since startup at 80%, refusals since startup).
 */
function connections(ctx: Ctx): TuningCheck {
  const now = ctx.metric("connections_used");
  const peak = ctx.configCheck("max_connections");
  return build({
    key: "connections",
    title: "Connection limit",
    category: "connections",
    items: [now, peak],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    value: () => {
      if (now && now.status !== "unknown") {
        const { Threads_connected: open, max_connections: limit, Max_used_connections: top } = now.inputs;
        if (typeof open === "number" && typeof limit === "number")
          return `${formatCount(open)} of ${formatCount(limit)} open${typeof top === "number" ? `, peak ${formatCount(top)}` : ""}`;
        // A report-1.0 finding keeps the metric's detail: "1 of max_connections 151; peak 3".
        const detail = now.numbers.find((n) => n.label === "Detail")?.text || "";
        const m = detail.match(/^([\d,]+) of max_connections ([\d,]+)(?:; peak ([\d,]+))?/);
        if (m) return `${m[1]} of ${m[2]} open${m[3] ? `, peak ${m[3]}` : ""}`;
        return `${now.value} in use`;
      }
      return peak?.verdict || "Measured";
    },
    evidence: (_item, all) => {
      const out: TuningEvidence[] = [];
      for (const i of all) {
        if (i.status === "unknown") continue;
        out.push({ label: i.source.view === "config" || i === peak ? "Since startup" : "Now", text: i.verdict });
      }
      return out;
    },
    verdict: (_i, all) => all.filter((i) => i.status !== "unknown").map((i) => i.verdict).join(" ") || _i.verdict,
    fix: (_item, status, all) => {
      const change = globalChange(ctx.engine, "max_connections", "<n>");
      // Health rates connections_used a problem at 90% use with or without refusals (health.py:312,
      // 729-731); only Connection_errors_max_connections proves a refusal.
      const refusals = all.some(
        (i) =>
          (typeof i.inputs.Connection_errors_max_connections === "number" && i.inputs.Connection_errors_max_connections > 0) ||
          /refused/i.test(i.verdict),
      );
      return {
        text:
          (refusals
            ? "Clients were refused at max_connections. "
            : status === "problem"
              ? "Open connections reached 90% of max_connections. "
              : "") +
          "Find out why connections pile up first (Sessions: idle transactions, lock waits, a pool that grows). " +
          "A connection pool in the application caps them; raise the limit only with memory for the extra sessions.",
        ...change,
        caveat:
          "Each connection can allocate its own sort, join and temporary-table memory when a statement needs it, " +
          "so a higher limit raises the worst case." +
          (ctx.mariadb ? "" : " MySQL may lower max_connections at startup to fit open_files_limit."),
      };
    },
    docs: "https://dev.mysql.com/doc/refman/8.4/en/too-many-connections.html",
  });
}

/** health.py _diag_thread_cache: watch above 10% misses at 1 thread per second; never a problem. */
function threadCache(ctx: Ctx): TuningCheck {
  return build({
    key: "thread_cache",
    title: "Thread cache",
    category: "connections",
    items: [ctx.diag("thread_cache")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    notApplicable: (i) => i.state === "not_applicable",
    fix: (item) => {
      // Defaults: MySQL min(8 + max_connections / 100, 100) (M/sql/mysqld.cc:6935-6951);
      // MariaDB 256, at most max_connections (Ma/sql/sys_vars.cc:4441-4444). Dynamic on both.
      return {
        text:
          item.advice ||
          `Raise thread_cache_size toward the number of connections that arrive in a burst (default ${
            ctx.mariadb ? "256, at most max_connections" : "8 + max_connections / 100, at most 100"
          }).`,
        ...globalChange(ctx.engine, "thread_cache_size", "<n>"),
        caveat: item.caveat || "Creating a thread costs little, so the gain is modest.",
      };
    },
  });
}

/** health.py aborted_clients: in moderation (watch 1/s, problem 10/s, at least 5 in the window). */
function droppedConnections(ctx: Ctx): TuningCheck {
  return build({
    key: "dropped_connections",
    title: "Connections dropped",
    category: "connections",
    items: [ctx.metric("aborted_clients")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // No wait_timeout or max_allowed_packet values: 28800 is already the default on both
    // engines, and max_allowed_packet defaults differ (64 MiB MySQL, 16 MiB MariaDB).
    fix: () => ({
      text:
        "Look at the client side first: applications that exit or are killed without closing, pools that keep " +
        "idle connections longer than wait_timeout, network drops, or packets larger than max_allowed_packet.",
    }),
  });
}

// ---- Tables and caches (15%) -------------------------------------------------------------

/** health.py _diag_table_cache: overflows in moderation (watch 1/s, problem 10/s, at least 5). */
function tableOpenCache(ctx: Ctx): TuningCheck {
  return build({
    key: "table_open_cache",
    title: "Table open cache",
    category: "tables",
    items: [ctx.diag("table_cache"), ctx.configCheck("table_open_cache")],
    measureWith: ["health", "config", "report"],
    missing: LOAD_HEALTH,
    // Table_open_cache_overflows is about table_open_cache per instance
    // (M/sql/table_cache.h:381-391), not the table definition cache.
    fix: (item) => ({
      text: "Raise table_open_cache so the tables in use fit, and check that open_files_limit covers it.",
      ...globalChange(ctx.engine, "table_open_cache", "<n>"),
      caveat: item.caveat || "Each open table uses memory and may hold a file descriptor.",
    }),
  });
}

/** health.py _diag_table_definitions: watch at 1 per second with the cache at least 95% full. */
function tableDefinitionCache(ctx: Ctx): TuningCheck {
  return build({
    key: "table_definition_cache",
    title: "Table definition cache",
    category: "tables",
    items: [ctx.diag("table_definitions")],
    measureWith: ["health", "report"],
    missing: LOAD_HEALTH,
    // Opened_table_definitions counts new shares (M/sql/dd_table_share.cc:2392, Ma/sql/table.cc:3580);
    // defaults: MySQL min(400 + table_open_cache / 2, 2000) (M/sql/mysqld.cc:10815-10825), MariaDB 400.
    fix: (item) => ({
      text: "Raise table_definition_cache to cover the tables in use.",
      ...globalChange(ctx.engine, "table_definition_cache", "<n>"),
      caveat: item.caveat || "Each cached definition uses memory.",
    }),
  });
}

function scopeText(t: TablesReport | null): string {
  if (!t?.scope) return "the databases read";
  if (t.scope.schemas && t.scope.schemas.length) return t.scope.schemas.join(", ");
  return "every user database";
}

/** report._tables_findings: problem with no usable key, watch with only a NOT NULL unique key. */
/** No table in scope: nothing was judged, so unknown with the reason, never OK. */
function noTables(reason: string, source: TuningSource): Item {
  return {
    status: "unknown",
    value: "",
    verdict: reason,
    rule: null,
    why: "",
    caveat: null,
    advice: null,
    actionSql: null,
    actionConfig: null,
    numbers: [],
    settings: [],
    state: null,
    inputs: {},
    value_number: null,
    source,
  };
}
/** report._tables_findings writes "no tables" as the engines headline when the inventory was empty. */
function reportHasNoTables(ctx: Ctx): boolean {
  return (ctx.finding("tables.engines")?.headline || "").trim().toLowerCase() === "no tables";
}

function primaryKeys(ctx: Ctx): TuningCheck {
  const t = ctx.tables;
  const own: Item | null = t
    ? (() => {
        const s = t.summary;
        if (!s.tables) return noTables(`No tables were read in ${scopeText(t)}, so there is nothing to judge.`, sourceOf("tables", t.taken_at));
        const noKey = s.no_usable_key || 0;
        const standIn = s.not_null_unique || 0;
        const status: PyStatus = noKey ? "problem" : standIn ? "watch" : "ok";
        const why = (t.why || []).filter((w) => w.key === "clustering" || w.key === "replication").map((w) => w.text);
        const listed = (t.missing_primary_keys || []).slice(0, 5);
        return {
          status,
          value: noKey || standIn
            ? [noKey ? plural(noKey, "table without a key", "tables without a key") : "", standIn ? plural(standIn, "on a unique key", "on a unique key") : ""].filter(Boolean).join(", ")
            : "All have a key",
          verdict: noKey || standIn
            ? `In ${scopeText(t)}: ${listed.map((m) => `${m.schema}.${m.table}`).join(", ")}${noKey + standIn > listed.length ? ", ..." : ""}.`
            : `Every table in ${scopeText(t)} has a primary key or a unique key on NOT NULL columns.`,
          rule: t.rules?.no_usable_key ? `${t.rules.no_usable_key}; ${t.rules.not_null_unique || ""}`.replace(/; $/, "") : null,
          why: why.join(" "),
          caveat: listed[0]?.caveat || null,
          advice: null,
          actionSql: listed[0]?.fixes?.[0]?.sql || null,
          actionConfig: null,
          numbers: listed.map((m) => ({ label: `${m.schema}.${m.table}`, text: m.clustered_on || m.kind })),
          settings: [],
          state: null,
          inputs: { no_usable_key: noKey, not_null_unique: standIn },
          value_number: noKey,
          source: sourceOf("tables", t.taken_at),
        };
      })()
    : null;
  let fromReport: Item | null = null;
  if (ctx.reportRan("tables")) {
    const noKey = ctx.finding("tables.no_primary_key");
    const standIn = ctx.finding("tables.unique_stand_in");
    const src = sourceOf("report", ctx.reportTaken("tables"));
    const items = [noKey, standIn].filter((f): f is AnalysisFinding => !!f).map((f) => findingItem(f, src));
    items.sort((a, b) => RANK[a.status as TuningStatus] - RANK[b.status as TuningStatus]);
    if (items.length && reportHasNoTables(ctx)) {
      fromReport = noTables("The Full analysis read no tables in the databases it covered, so there is nothing to judge.", src);
    } else if (items.length) {
      const worst = items[0];
      const counts = [noKey, standIn].map((f) => (f && f.status !== "ok" ? f.impact || 0 : 0));
      fromReport = {
        ...worst,
        value: counts[0] || counts[1]
          ? [counts[0] ? plural(counts[0], "table without a key", "tables without a key") : "", counts[1] ? plural(counts[1], "on a unique key", "on a unique key") : ""].filter(Boolean).join(", ")
          : "All have a key",
        advice: null,
        actionSql: null,
      };
    }
  }
  const item = own && fromReport ? (timeOf(fromReport.source.takenAt) > timeOf(own.source.takenAt) ? fromReport : own) : own || fromReport;
  return build({
    key: "primary_keys",
    title: "Tables without a primary key",
    category: "tables",
    items: [item],
    measureWith: ["tables", "report"],
    missing: LOAD_TABLES,
    fix: (i) => ({
      text: "Add a primary key. Tables lists each table with the checks to run first and the statement for it.",
      ...(i.actionSql ? { sql: `-- ${i.numbers[0]?.label || "first table"} (template: check the columns first)\n${i.actionSql}` } : {}),
      caveat: i.caveat || "Adding a primary key rebuilds the table; adding an AUTO_INCREMENT column blocks writes for the whole rebuild.",
    }),
  });
}

/** report._tables_findings "tables.engines": watch when user tables use MyISAM, info otherwise. */
function myisamTables(ctx: Ctx): TuningCheck {
  const t = ctx.tables as (TablesReport & { summary: TablesReport["summary"] & { engines?: { engine: string; tables: number; bytes?: number }[] } }) | null;
  let own: Item | null = null;
  const engines = t?.summary?.engines;
  const total = t ? (engines || []).reduce((n, e) => n + (e.tables || 0), 0) : 0;
  if (t && (!t.summary?.tables || (engines && total === 0))) {
    own = noTables(`No tables were read in ${scopeText(t)}, so there is nothing to judge.`, sourceOf("tables", t.taken_at));
  } else if (t && engines) {
    const myisam = engines.filter((e) => (e.engine || "").toLowerCase() === "myisam").reduce((n, e) => n + (e.tables || 0), 0);
    const names = (t.tables || []).filter((r) => (r.engine || "").toLowerCase() === "myisam").slice(0, 5);
    own = {
      status: myisam ? "watch" : "info",
      value: myisam ? plural(myisam, "MyISAM table", "MyISAM tables") : "None",
      verdict: `${engines.map((e) => `${e.engine || "unknown"} ${formatCount(e.tables)}`).join(", ") || "No tables"} in ${scopeText(t)}.`,
      rule: "watch when any user table uses MyISAM",
      why:
        "MyISAM has no transactions, locks the whole table for each write, and after a crash a table may need a repair.",
      caveat: null,
      advice: null,
      actionSql: names[0] ? `ALTER TABLE \`${names[0].schema.replace(/`/g, "``")}\`.\`${names[0].table.replace(/`/g, "``")}\` ENGINE=InnoDB;` : null,
      actionConfig: null,
      numbers: names.map((r) => ({ label: `${r.schema}.${r.table}`, text: r.size_bytes != null ? formatBytes(r.size_bytes) : "MyISAM" })),
      settings: [],
      state: null,
      inputs: { myisam },
      value_number: myisam,
      source: sourceOf("tables", t.taken_at),
    };
  }
  const f = ctx.reportRan("tables") ? ctx.finding("tables.engines") : null;
  const fromReport = !f
    ? null
    : reportHasNoTables(ctx)
      ? noTables("The Full analysis read no tables in the databases it covered, so there is nothing to judge.", sourceOf("report", ctx.reportTaken("tables")))
      : { ...findingItem(f, sourceOf("report", ctx.reportTaken("tables"))), value: f.status === "watch" ? "MyISAM tables" : "None", rule: "watch when any user table uses MyISAM" };
  const item = own && fromReport ? (timeOf(fromReport.source.takenAt) > timeOf(own.source.takenAt) ? fromReport : own) : own || fromReport;
  return build({
    key: "myisam_tables",
    title: "MyISAM tables",
    category: "tables",
    items: [item],
    measureWith: ["tables", "report"],
    missing: LOAD_TABLES,
    // Python's info here means "no MyISAM": the engines were read and nothing matched.
    info: "ok",
    fix: (i) => ({
      text: "Convert them to InnoDB after checking for code that relies on MyISAM behavior, such as an instant COUNT(*) without WHERE. FULLTEXT and SPATIAL indexes work in InnoDB too.",
      sql: i.actionSql || "ALTER TABLE <schema>.<table> ENGINE=InnoDB;",
      caveat: "Changing the engine copies the table: it blocks writes while it runs and needs free disk space for the copy.",
    }),
  });
}

// ---- Security (10%) -------------------------------------------------------------------------

/** The account count of a checks-1.0 finding: its headline is str(len(names)) (server_checks._accounts). */
function accountCount(i: Item): number | null {
  const n = Number(i.value);
  return Number.isFinite(n) ? n : null;
}
function accounts(ctx: Ctx, key: "no_password" | "anonymous" | "root_any_host", title: string): TuningCheck {
  return build({
    key,
    title,
    category: "security",
    items: [ctx.serverCheck(key)],
    measureWith: ["report"],
    missing: RUN_ANALYSIS,
    value: (i) => {
      const n = accountCount(i);
      if (n === null) return i.value;
      return n === 0 ? "None" : plural(n, "account", "accounts");
    },
    evidence: (i) => i.numbers.filter((n) => n.label === "Account" || n.label === "More"),
    fix: (i) => ({
      text: i.advice || "Review the accounts listed.",
      ...(i.actionSql ? { sql: i.actionSql } : {}),
      caveat: i.caveat || undefined,
    }),
    docs: "https://dev.mysql.com/doc/refman/8.4/en/alter-user.html",
  });
}

/**
 * require_secure_transport (MySQL 5.7.8+, MariaDB 10.5.2+; GLOBAL and dynamic on both:
 * M/sql/sys_vars.cc:3747, Ma/sql/sys_vars.cc:3600-3609). Read only from Parameters,
 * which lists every global variable; the other views do not collect it. When ON,
 * TCP connections without TLS are refused; Unix socket and shared memory (named
 * pipes on MariaDB) count as secure (M/sql/auth/sql_authentication.cc:4363-4366,
 * 4501-4505; Ma/sql/sql_acl.cc:14425-14437). have_ssl exists on MySQL 8.0 (removed
 * in 8.4, WL#16095) and on MariaDB (Ma/sql/sys_vars.cc:5643).
 */
function secureTransport(ctx: Ctx): TuningCheck {
  const params = ctx.parameters;
  const require = params ? ctx.variable("require_secure_transport") : null;
  const haveSsl = params ? ctx.variable("have_ssl") : null;
  let item: Item | null = null;
  if (params) {
    const on = require !== null && ["ON", "1", "YES", "TRUE"].includes(require.toUpperCase());
    const base: Omit<Item, "status" | "value" | "verdict"> = {
      rule: "watch when require_secure_transport is OFF",
      why:
        "With require_secure_transport OFF, a client may connect over TCP without TLS, so the login handshake " +
        "and every query and result cross the network unencrypted.",
      caveat: null,
      advice: null,
      actionSql: null,
      actionConfig: null,
      numbers: [],
      settings: [
        ...(require !== null ? [{ name: "require_secure_transport", value: require, default: "OFF" }] : []),
        ...(haveSsl !== null ? [{ name: "have_ssl", value: haveSsl }] : []),
      ],
      state: null,
      inputs: {},
      value_number: null,
      source: sourceOf("config", ctx.config?.taken_at),
    };
    item =
      require === null
        ? { ...base, status: "unknown", value: "", verdict: "This server does not report require_secure_transport." }
        : on
          ? { ...base, status: "ok", value: "Required", verdict: "require_secure_transport=ON: clients must use TLS or a local socket." }
          : {
              ...base,
              status: "watch",
              value: "Not required",
              verdict:
                "require_secure_transport=OFF: clients may connect over TCP without TLS." +
                (haveSsl && haveSsl.toUpperCase() !== "YES" ? ` TLS is not set up on this server (have_ssl=${haveSsl}).` : ""),
            };
  }
  return build({
    key: "secure_transport",
    title: "Encrypted connections",
    category: "security",
    items: [item],
    measureWith: ["config"],
    missing: "Open Parameters to read require_secure_transport (the full analysis does not collect it).",
    fix: () => ({
      text:
        (haveSsl && haveSsl.toUpperCase() !== "YES"
          ? "Set up TLS first (ssl_cert, ssl_key), then check that every application connects with TLS. "
          : "Check that every application connects with TLS first. ") +
        "Then require it.",
      ...globalChange(ctx.engine, "require_secure_transport", "ON"),
      caveat:
        "From then on TCP connections without TLS are refused at login; local socket connections still work. Except on Windows, the server accepts the change even when TLS is not set up.",
    }),
    docs: "https://dev.mysql.com/doc/refman/8.4/en/using-encrypted-connections.html",
  });
}

// ---- Scoring ------------------------------------------------------------------------------

export function categoryScore(checks: TuningCheck[]): number | null {
  const measured = checks.filter((c) => c.applies && c.status !== "unknown");
  if (!measured.length) return null;
  const total = measured.reduce((sum, c) => sum + POINTS[c.status as Exclude<TuningStatus, "unknown">], 0);
  return Math.round(total / measured.length);
}

export function worstStatus(checks: TuningCheck[]): TuningStatus {
  const measured = checks.filter((c) => c.applies && c.status !== "unknown");
  if (!measured.length) return "unknown";
  return measured.reduce<TuningStatus>((w, c) => (RANK[c.status] < RANK[w] ? c.status : w), "ok");
}

export function overallScore(categories: TuningCategoryResult[]): number | null {
  let sum = 0;
  let weight = 0;
  for (const c of categories) {
    if (c.score === null) continue;
    sum += c.score * c.weight;
    weight += c.weight;
  }
  return weight > 0 ? Math.round(sum / weight) : null;
}

export function letterGrade(score: number): TuningGrade {
  return (GRADE_SCALE.find((g) => score >= g.min) || GRADE_SCALE[GRADE_SCALE.length - 1]).grade;
}

const gradeRank = (g: TuningGrade) => GRADE_SCALE.findIndex((x) => x.grade === g);

/** Why the grade is what it is, in one line; "" without a grade (the coverage hint says why). */
export function gradeReason(r: Pick<TuningReport, "score" | "grade" | "counts">): string {
  if (r.score === null || !r.grade) return "";
  const earned = letterGrade(r.score);
  if (earned !== r.grade) {
    const n = r.counts.problem;
    return `Capped at ${r.grade}: ${n.toLocaleString("en-US")} ${n === 1 ? "check has" : "checks have"} a problem (${r.score} is in the ${earned} range).`;
  }
  return `Score ${r.score} is in the ${r.grade} range, ${gradeRangeText(r.grade)}.`;
}

/** Pure: the scorecard for the reports given. Reports from another connection must not be passed. */
export function evaluateTuning(input: TuningInput): TuningReport {
  const ctx = new Ctx(input);
  const checks: TuningCheck[] = [
    bufferPool(ctx),
    dirtyPages(ctx),
    freePageWaits(ctx),
    redoLog(ctx),
    joinsWithoutIndex(ctx),
    tempTables(ctx),
    sortMergePasses(ctx),
    slowLog(ctx),
    connections(ctx),
    threadCache(ctx),
    droppedConnections(ctx),
    tableOpenCache(ctx),
    tableDefinitionCache(ctx),
    primaryKeys(ctx),
    myisamTables(ctx),
    accounts(ctx, "no_password", "Accounts without a password"),
    accounts(ctx, "anonymous", "Anonymous accounts"),
    accounts(ctx, "root_any_host", "root from any host"),
    secureTransport(ctx),
  ];
  const categories: TuningCategoryResult[] = CATEGORY_ORDER.map((key) => {
    const own = checks.filter((c) => c.category === key);
    const applicable = own.filter((c) => c.applies);
    return {
      key,
      name: TUNING_CATEGORIES[key].name,
      weight: TUNING_CATEGORIES[key].weight,
      score: categoryScore(own),
      status: worstStatus(own),
      measured: applicable.filter((c) => c.status !== "unknown").length,
      total: applicable.length,
      checks: own,
    };
  });
  const applicable = checks.filter((c) => c.applies);
  const measured = applicable.filter((c) => c.status !== "unknown").length;
  const total = applicable.length;
  const counts: Record<TuningStatus, number> = { problem: 0, watch: 0, ok: 0, unknown: 0 };
  for (const c of applicable) counts[c.status]++;
  const score = overallScore(categories);
  const provisional = score === null || measured * 2 < total || categories.some((c) => c.total > 0 && c.measured === 0);
  let grade: TuningGrade | null = null;
  if (score !== null && !provisional) {
    grade = letterGrade(score);
    if (counts.problem > 0 && gradeRank(grade) < gradeRank(PROBLEM_GRADE_CAP)) grade = PROBLEM_GRADE_CAP;
  }
  const label: TuningLabel =
    counts.problem > 0 ? "Problems found" : counts.watch > 0 ? "Needs attention" : provisional ? "Not enough data" : "Good";
  const weight = (c: TuningCheck) => TUNING_CATEGORIES[c.category].weight;
  const recommendations = checks
    .map((c, i) => ({ c, i }))
    .filter(({ c }) => c.applies && (c.status === "problem" || c.status === "watch"))
    .sort((a, b) => RANK[a.c.status] - RANK[b.c.status] || weight(b.c) - weight(a.c) || a.i - b.i)
    .map(({ c }) => c);
  return {
    schema_version: "tuning-1.0",
    score,
    provisional,
    grade,
    label,
    status: worstStatus(checks),
    measured,
    total,
    counts,
    categories,
    checks,
    recommendations,
    server: { engine: ctx.engine, version: ctx.version, product: ctx.product },
    scoring: SCORING_RULE,
    generatedAt: input.now || new Date().toISOString(),
  };
}

// ---- Binding ---------------------------------------------------------------------------------

/** A report and the connection it was read from (binding.ts Bound; Health's memory has the same shape). */
export type BoundLike<T> = { key: string; label: string; report: T | null } | null | undefined;

/** Which reports to score: the ones of the connection in use, else (greyed) the newest other connection's. */
export function tuningInputs({
  activeKey,
  report,
  health,
  config,
  tables,
  legacy,
}: {
  activeKey: string;
  report?: BoundLike<AnalysisReport>;
  health?: BoundLike<HealthReport>;
  config?: BoundLike<ConfigReport>;
  tables?: BoundLike<TablesReport>;
  legacy?: { health?: HealthReport | null; config?: ConfigReport | null; tables?: TablesReport | null };
}): { input: TuningInput; other: string | null; key: string } {
  const bound = [
    ["report", report],
    ["health", health],
    ["config", config],
    ["tables", tables],
  ] as const;
  const present = bound.filter(([, b]) => b && b.report);
  const current = present.filter(([, b]) => b!.key === activeKey);
  const pick = (key: string) => {
    const input: TuningInput = {};
    for (const [name, b] of present)
      if (b!.key === key) (input as Record<string, unknown>)[name] = b!.report;
    return input;
  };
  const withLegacy = (input: TuningInput): TuningInput => ({
    ...input,
    ...(health === undefined && legacy?.health ? { health: legacy.health } : {}),
    ...(config === undefined && legacy?.config ? { config: legacy.config } : {}),
    ...(tables === undefined && legacy?.tables ? { tables: legacy.tables } : {}),
  });
  if (current.length) return { input: withLegacy(pick(activeKey)), other: null, key: activeKey };
  const legacyOnly = withLegacy({});
  if (Object.keys(legacyOnly).length) return { input: legacyOnly, other: null, key: activeKey };
  if (present.length) {
    const [, first] = present[0];
    return { input: pick(first!.key), other: first!.label, key: first!.key };
  }
  return { input: {}, other: null, key: activeKey };
}

// ---- Words ------------------------------------------------------------------------------------

export const STATUS_TEXT: Record<TuningStatus, string> = {
  problem: "Problem",
  watch: "Watch",
  ok: "OK",
  unknown: "Not measured",
};

/** "12 of 19 checks measured". */
export function coverageText(r: Pick<TuningReport, "measured" | "total">): string {
  return `${r.measured} of ${r.total} checks measured`;
}

/** One line for the summary: "Needs attention: 74%, 15 of 19 checks measured; 1 problem, 2 to watch." */
export function summaryText(r: TuningReport): string {
  const head =
    r.score === null
      ? `${r.label}: no check measured yet`
      : `${r.label}: ${r.score}%${r.provisional ? " (provisional)" : r.grade ? `, grade ${r.grade}` : ""}, ${coverageText(r)}`;
  const parts: string[] = [];
  if (r.counts.problem) parts.push(plural(r.counts.problem, "problem", "problems"));
  if (r.counts.watch) parts.push(`${r.counts.watch} to watch`);
  return parts.length ? `${head}; ${parts.join(", ")}.` : `${head}.`;
}

// ---- Exports ------------------------------------------------------------------------------------

/** Escapes text for HTML element content and quoted attribute values. */
export function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** Escapes text for Markdown prose and table cells (server names can hold any character). */
export function escapeMarkdown(value: unknown): string {
  return String(value ?? "")
    .replace(/\r?\n/g, " ")
    .replace(/([\\`*_{}[\]()#+!|<>~])/g, "\\$1");
}

/** A fenced block that the text cannot close. */
function fence(text: string, lang = "sql"): string[] {
  const longest = Math.max(2, ...(text.match(/`+/g) || []).map((m) => m.length));
  const ticks = "`".repeat(longest + 1);
  return [`${ticks}${lang}`, text, ticks];
}

const CREDIT = "Made with myflames. Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs.";

function serverText(r: TuningReport): string {
  return `${r.server.product || (r.server.engine === "mariadb" ? "MariaDB" : "MySQL")} ${r.server.version || ""}`.trim();
}
function scoreText(r: TuningReport): string {
  if (r.score === null) return "No score: no check was measured.";
  return `${r.score}%${r.provisional ? " (provisional, no grade)" : r.grade ? ` (grade ${r.grade})` : ""}`;
}

export function tuningMarkdown(r: TuningReport, connectionLabel?: string): string {
  const md = escapeMarkdown;
  const lines: string[] = ["# Tuning scorecard", ""];
  lines.push(`${md(serverText(r))}${connectionLabel ? ` on ${md(connectionLabel)}` : ""}, ${md(r.generatedAt)}`, "");
  lines.push(`**${md(r.label)}.** Score ${md(scoreText(r))}. ${md(coverageText(r))}.`, "");
  lines.push(md(r.scoring), "");
  lines.push("| Category | Weight | Score | Measured |", "| :--- | ---: | ---: | ---: |");
  for (const c of r.categories)
    lines.push(`| ${md(c.name)} | ${Math.round(c.weight * 100)}% | ${c.score === null ? "not measured" : `${c.score}%`} | ${c.measured} of ${c.total} |`);
  lines.push("", "## Needs attention", "");
  if (!r.recommendations.length) lines.push("No problem or watch among the measured checks.", "");
  for (const c of r.recommendations) {
    lines.push(`### ${md(STATUS_TEXT[c.status])}: ${md(c.title)}`, "");
    lines.push(`- Measured: ${md(c.value)}`);
    if (c.verdict) lines.push(`- What this server shows: ${md(c.verdict)}`);
    if (c.rule) lines.push(`- Rule: ${md(c.rule)}`);
    if (c.source) lines.push(`- From: ${md(c.source.label)}${c.source.takenAt ? `, ${md(c.source.takenAt)}` : ""}`);
    if (c.fix) {
      lines.push(`- What to do: ${md(c.fix.text)}`);
      if (c.fix.sql) lines.push("", ...fence(c.fix.sql));
      if (c.fix.optionFile) lines.push("", "Option file (copy it; myflames does not edit files):", "", ...fence(c.fix.optionFile, "ini"));
      if (c.fix.caveat) lines.push("", `Caveat: ${md(c.fix.caveat)}`);
    }
    lines.push("");
  }
  const unknown = r.checks.filter((c) => c.status === "unknown");
  if (unknown.length) {
    lines.push("## Not measured", "");
    for (const c of unknown) lines.push(`- ${md(c.title)}: ${md(c.unknownReason || "")}`);
    lines.push("");
  }
  const ok = r.checks.filter((c) => c.applies && c.status === "ok");
  if (ok.length) {
    lines.push("## OK", "");
    for (const c of ok) lines.push(`- ${md(c.title)}: ${md(c.value)}`);
    lines.push("");
  }
  lines.push("Statements are for you to review and run yourself; myflames never runs them from this page.", "", CREDIT, "");
  return lines.join("\n");
}

export function tuningHtml(r: TuningReport, connectionLabel?: string): string {
  const e = escapeHtml;
  const tone = (s: TuningStatus) => `<span class="badge ${e(s)}">${e(STATUS_TEXT[s])}</span>`;
  const rows = r.categories
    .map(
      (c) =>
        `<tr><td>${e(c.name)}</td><td>${e(Math.round(c.weight * 100))}%</td><td>${
          c.score === null ? "not measured" : `${e(c.score)}%`
        }</td><td>${e(c.measured)} of ${e(c.total)}</td></tr>`,
    )
    .join("");
  const card = (c: TuningCheck) => `
    <section class="check ${e(c.status)}">
      <h3>${tone(c.status)} ${e(c.title)}</h3>
      <p><strong>Measured:</strong> ${e(c.value)}</p>
      ${c.verdict ? `<p>${e(c.verdict)}</p>` : ""}
      ${c.rule ? `<p class="muted"><strong>Rule:</strong> ${e(c.rule)}</p>` : ""}
      ${c.source ? `<p class="muted">From ${e(c.source.label)}${c.source.takenAt ? `, ${e(c.source.takenAt)}` : ""}</p>` : ""}
      ${c.fix ? `<p><strong>What to do:</strong> ${e(c.fix.text)}</p>` : ""}
      ${c.fix?.sql ? `<pre><code>${e(c.fix.sql)}</code></pre>` : ""}
      ${c.fix?.optionFile ? `<p class="muted">Option file (copy it; myflames does not edit files):</p><pre><code>${e(c.fix.optionFile)}</code></pre>` : ""}
      ${c.fix?.caveat ? `<p class="muted">Caveat: ${e(c.fix.caveat)}</p>` : ""}
    </section>`;
  const unknown = r.checks.filter((c) => c.status === "unknown");
  const ok = r.checks.filter((c) => c.applies && c.status === "ok");
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tuning scorecard</title>
<style>
:root { color-scheme: light dark; --bg: #ffffff; --ink: #1d1d1f; --muted: #6e6e73; --line: rgba(0,0,0,0.1);
  --ok: #1b7f3b; --warn: #a15c00; --danger: #c4271c; --info: #0b5cad; }
@media (prefers-color-scheme: dark) { :root { --bg: #1c1c1e; --ink: #f5f5f7; --muted: #a1a1a6; --line: rgba(255,255,255,0.14);
  --ok: #4cd964; --warn: #ffb340; --danger: #ff6961; --info: #64a8ff; } }
body { margin: 0; padding: 32px 16px; background: var(--bg); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif; }
main { max-width: 860px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; } h3 { font-size: 14px; margin: 0 0 6px; }
.muted { color: var(--muted); }
.score { font-size: 28px; font-weight: 600; margin: 16px 0 4px; font-variant-numeric: tabular-nums; }
table { width: 100%; border-collapse: collapse; } th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
.check { border-top: 1px solid var(--line); padding: 12px 0; } .check p { margin: 4px 0; }
.badge { display: inline-block; font-size: 12px; font-weight: 600; margin-right: 4px; }
.badge.problem { color: var(--danger); } .badge.watch { color: var(--warn); } .badge.ok { color: var(--ok); } .badge.unknown { color: var(--muted); }
pre { background: rgba(127,127,127,0.12); padding: 8px 10px; border-radius: 6px; overflow-x: auto; font-size: 12px; }
footer { margin-top: 32px; color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<main>
<h1>Tuning scorecard</h1>
<p class="muted">${e(serverText(r))}${connectionLabel ? ` on ${e(connectionLabel)}` : ""}, ${e(r.generatedAt)}</p>
<p class="score">${e(r.label)}: ${e(scoreText(r))}</p>
<p>${e(coverageText(r))}.</p>
<p class="muted">${e(r.scoring)}</p>
<h2>Categories</h2>
<table><thead><tr><th>Category</th><th>Weight</th><th>Score</th><th>Measured</th></tr></thead><tbody>${rows}</tbody></table>
<h2>Needs attention (${e(r.recommendations.length)})</h2>
${r.recommendations.length ? r.recommendations.map(card).join("") : "<p>No problem or watch among the measured checks.</p>"}
${unknown.length ? `<h2>Not measured (${e(unknown.length)})</h2><ul>${unknown.map((c) => `<li>${e(c.title)}: ${e(c.unknownReason || "")}</li>`).join("")}</ul>` : ""}
${ok.length ? `<h2>OK (${e(ok.length)})</h2><ul>${ok.map((c) => `<li>${e(c.title)}: ${e(c.value)}</li>`).join("")}</ul>` : ""}
<footer><p>Statements are for you to review and run yourself; myflames never runs them from this page.</p><p>${e(CREDIT)}</p></footer>
</main>
</body>
</html>
`;
}
