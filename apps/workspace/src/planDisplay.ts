// Small plan values shared with session restore and the window status bar.
import type { Capture, View } from "./api";
import { villageSQLLabel } from "./serverIdentity";

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

/** Captured-plan identity for the status bar; other modes have no capture label. */
export function captureServerLabel(mode: string, capture?: Pick<Capture, "engine" | "server_version"> | null) {
  const version = mode === "inspect" ? capture?.server_version : undefined;
  return version ? villageSQLLabel(version) || `${engineName(capture?.engine)} ${version}` : undefined;
}
