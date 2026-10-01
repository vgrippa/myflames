export type View =
  | "flamegraph"
  | "tree"
  | "bargraph"
  | "treemap"
  | "workbench";
export type Finding = {
  text: string;
  severity: string;
  node_ids?: string[];
  node_labels?: string[];
};
export type Suggestion = { action: string; why?: string; severity: string };
export type Analysis = {
  schema_version: string;
  source: { engine?: string };
  plan_summary: {
    total_time_ms: number;
    rows_sent: number;
    rows_examined_estimate: number;
    operator_count: number;
  };
  executive_summary: string;
  primary_action?: { ref: string };
  warnings: Finding[];
  suggestions: Suggestion[];
};
export type Operator = {
  node_id: string;
  label: string;
  depth: number;
  self_time_ms: number;
  total_time_ms: number;
  rows: number;
  loops: number;
  details: Record<string, unknown>;
  parent_id?: string | null;
  children?: string[];
};
export type Plan = {
  id: string;
  name: string;
  raw: string;
  analysis: Analysis;
  operators: Operator[];
  query: string;
  teach_hooks?: TeachHook[];
  capture?: Capture;
  notes?: string;
};
export type Comparison = {
  analysis: {
    before: { total_time_ms: number; timing_available?: boolean };
    after: { total_time_ms: number; timing_available?: boolean };
    deltas: MatchDelta[];
    summary: {
      time_delta_ms: number;
      time_delta_pct: number | null;
      regressions: number;
      improvements: number;
    };
  };
  html: string;
};
export async function api<T>(
  path: string,
  body: object,
  signal?: AbortSignal,
): Promise<T> {
  const token =
    document.querySelector<HTMLMetaElement>('meta[name="myflames-token"]')
      ?.content || "";
  const response = await fetch(`/api/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Myflames-Token": token },
    body: JSON.stringify(body),
    signal,
  });
  const result = await response.json();
  if (!response.ok)
    throw new Error(result.error || "The request failed. Try again.");
  return result;
}
export function download(text: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function time(ms: number) {
  if (!Number.isFinite(ms)) return "—";
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  if (ms > 0 && ms < 1) return `${(ms * 1000).toFixed(1)} µs`;
  return `${ms.toLocaleString(undefined, { maximumFractionDigits: 2 })} ms`;
}
export function number(value: number) {
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 });
}
export function safePreview(html: string) {
  // The iframe has an opaque origin. This policy also keeps report/plan content
  // from making external requests or submitting forms inside the preview.
  const policy = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'">`;
  return html.replace(/<head[^>]*>/i, (match) => match + policy);
}

export type TeachHook = {
  lesson: string;
  match: { short_label?: string };
  note: string;
};
export type Capture = {
  raw: string;
  query: string;
  mode: string;
  engine: string;
  server_version: string;
  captured_at: string;
  runs: {
    total_time_ms: number | null;
    statistics: Record<string, number> | null;
    session_status_delta: Record<string, number>;
    plan: string;
  }[];
  measurements: {
    count: number;
    median_ms: number | null;
    min_ms: number | null;
    max_ms: number | null;
    stdev_ms: number | null;
  };
  schema: Record<string, unknown>;
  table_stats: Record<string, unknown>;
  variables: Record<string, unknown>;
  trace: {
    data: unknown;
    missing_bytes: number;
    insufficient_privileges: number;
  } | null;
  warnings: string[];
  conditions: string;
};
export type Connection = {
  name: string;
  host: string;
  port: number;
  user: string;
  password?: string;
  database: string;
  ssl_mode: string;
  ssl_ca?: string;
};
export type Bundle = {
  schema_version: "investigation-1.0";
  id?: string;
  name: string;
  notes: string;
  tags: string[];
  baseline_id: string;
  plans: {
    id: string;
    name: string;
    raw: string;
    notes?: string;
    capture?: Capture;
  }[];
  updated_at?: string;
};
export type MatchDelta = {
  before_node_id: string | null;
  after_node_id: string | null;
  before_label: string;
  after_label: string;
  short_label: string;
  classification: string;
  matching: { method: string; confidence: string; status: string };
  self_time_ms: {
    before: number | null;
    after: number | null;
    change_pct: number | null;
  };
};
