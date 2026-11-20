// Small plan values shared with session restore and the window status bar.
import type { View } from "./api";

export const views: [View, string][] = [
  ["workbench", "Visual Explain"],
  ["flamegraph", "Flame graph"],
  ["tree", "Execution tree"],
  ["bargraph", "Bar chart"],
  ["stats", "Stats"],
];
export const metrics: [string, string][] = [
  ["self_time", "Self time"],
  ["total_time", "Total time"],
  ["rows", "Actual rows / loop"],
  ["estimate_error", "Estimation mismatch"],
];
/** "MySQL", "MariaDB", or the engine as reported. */
export function engineName(engine?: string) {
  if (engine === "mysql") return "MySQL";
  if (engine === "mariadb") return "MariaDB";
  return engine || "SQL";
}
