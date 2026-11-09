import type { ChartData, ChartNode } from "./types";

/** Resolve a warning without expanding or mutating the shared payload. */
export function highlightText(data: ChartData, node: ChartNode): string | null {
  if (data.schema_version === "chart-data-1.0") {
    return typeof node.highlight === "string" ? node.highlight : null;
  }
  const id = node.highlight_id;
  if (typeof id !== "number" || !Number.isInteger(id) || id < 0) return null;
  const message = data.highlight_messages?.[id];
  return typeof message === "string" ? message : null;
}
