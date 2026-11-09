// chart-data-1.0 and chart-data-2.0 (docs/schemas/chart-data-*.json), emitted by
// myflames/chart_data.py. Python owns every MySQL/MariaDB decision; the
// bundle only lays out, formats, and wires interaction.

export type MetricName = "self_time" | "total_time" | "rows" | "estimate_error";
export type MetricValue = [number | null, string];

export type WorkbenchKind =
  | "table" | "lookup" | "range" | "index" | "nested_loop" | "hash" | "bka"
  | "operation" | "materialize" | "unknown";

export interface ChartNode {
  id: string;
  /** Normalized Python-owned operation/table/index identity. */
  group?: string;
  parent_id: string | null;
  children: string[];
  depth: number;
  input_kind: "input" | "select_list" | "subquery" | null;
  short_label: string;
  full_label: string;
  folded_label: string;
  operation: string;
  family: string;
  wb_kind: WorkbenchKind;
  join_type: string | null;
  phase: string;
  arrow_width: number;
  measured: boolean;
  self_ms: number | null;
  total_ms: number | null;
  actual_rows: number | null;
  estimated_rows: number | null;
  loops: number | null;
  first_row_ms: number | null;
  last_row_ms: number | null;
  estimated_cost: number | null;
  table: string;
  schema: string;
  index: string;
  access_type: string;
  index_access_type: string;
  condition: string;
  ranges: string[];
  covering: boolean | null;
  join_algorithm: string;
  complexity: Record<string, unknown> | null;
  teach_index: number | null;
  /** Version 1 inline message; retained for saved/external charts. */
  highlight?: string | null;
  /** Version 2 index into ChartData.highlight_messages; null means no warning. */
  highlight_id?: number | null;
  metrics: Record<MetricName, MetricValue>;
  details_text: string;
  /** Flame-graph tooltip lines for exactly this operator (may be empty). */
  flame_details: string;
  /** Present only when the parser flags this operator's row estimate. */
  misestimate?: Misestimate;
}

/** STALE_STATS evidence for a misestimated table (advisor.statistics_evidence). */
export interface MisestimateEvidence {
  /** schema.table as the server names it. */
  table: string;
  last_update: string | null;
  n_rows: number | null;
  histograms: string[];
  /** The advisor's facts, as text. */
  text: string;
  /** ANALYZE TABLE for copy and paste; never run by myflames. */
  statement: string;
}

/** The parser's row-estimate record (parser.misestimate_record). The bundle
 * draws ``label`` as the marker and shows the other text as it is. */
export interface Misestimate {
  direction: "under" | "over";
  factor: number;
  estimated: number;
  actual: number;
  loops: number;
  table: string;
  kind: "access" | "filter";
  label: string;
  text: string;
  reason: string;
  evidence: MisestimateEvidence | null;
}

export interface ChartProjection {
  focus_id: string;
  collapsed_ids: string[];
  selected_id: string;
  metric: MetricName;
}

export interface PlanStatsRow {
  label: string;
  count: number;
  node_ids: string[];
  self_ms: number | null;
  share: number | null;
  actual_rows: number | null;
  estimated_rows: number | null;
  loops: number | null;
  table?: string;
  schema?: string;
  operators?: string[];
}

export interface ChartData {
  schema_version: "chart-data-1.0" | "chart-data-2.0";
  /** Version 2 interns full messages here rather than in every node. */
  highlight_messages?: string[];
  stats?: { tables: PlanStatsRow[]; operators: PlanStatsRow[]; note: string };
  groups?: Record<string, PlanStatsRow>;
  title: string;
  engine: string;
  display_unit: string;
  timing_available: boolean;
  root_id: string;
  nodes: ChartNode[];
  projection: ChartProjection;
  metrics_meta: Record<MetricName, { label: string; unit: string }>;
  labels: {
    join_types: Record<string, { label: string; meaning: string }>;
    metric_notes: { base: string; collapsed: string; select_list: string };
  };
}

export interface MountOptions {
  view: string;
  width?: number;
  /** Extra panels; "metrics" adds the linked operator metric list. */
  panels?: string[];
  /** Overrides data.projection (copy-only). */
  projection?: Partial<ChartProjection>;
  /** Per-view presentation settings, e.g. flame graph inverted/frame_height. */
  options?: Record<string, unknown>;
  /**
   * Called when the user selects an operator in this chart or its panels
   * (never for select(id, false) or clear()). For in-page hosts such as the
   * React workspace.
   */
  onSelect?: (id: string) => void;
  /**
   * Exchange myflames-select / myflames-node window messages with the
   * parent window (default true). In-page hosts pass false and use
   * onSelect plus ChartHandle.select instead.
   */
  bridge?: boolean;
}

export interface ChartHandle {
  readonly element: HTMLElement;
  /** Select a visible operator; returns false for unknown or hidden IDs. */
  select(id: string, notify?: boolean): boolean;
  selected(): string;
  /** Drop the current selection locally; never notifies. */
  clear(): void;
  /**
   * Bring a drawn operator into view without changing the selection:
   * Visual Explain pans to its card (zoom kept), the other views scroll it
   * into view. False when the view does not draw it.
   */
  reveal(id: string): boolean;
  destroy(): void;
}
