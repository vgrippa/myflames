// chart-data-1.0 (docs/schemas/chart-data-1.0.json), emitted by
// myflames/chart_data.py. Python owns every MySQL/MariaDB decision; the
// bundle only lays out, formats, and wires interaction.

export type MetricName = "self_time" | "total_time" | "rows" | "estimate_error";
export type MetricValue = [number | null, string];

export type WorkbenchKind =
  | "table" | "lookup" | "range" | "index" | "nested_loop" | "hash" | "bka"
  | "operation" | "materialize" | "unknown";

export interface ChartNode {
  id: string;
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
  highlight: string | null;
  metrics: Record<MetricName, MetricValue>;
  details_text: string;
  /** Flame-graph tooltip lines for exactly this operator (may be empty). */
  flame_details: string;
}

export interface ChartProjection {
  focus_id: string;
  collapsed_ids: string[];
  selected_id: string;
  metric: MetricName;
}

export interface ChartData {
  schema_version: "chart-data-1.0";
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
  destroy(): void;
}
