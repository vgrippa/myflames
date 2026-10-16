import type { Projection } from "./project";
import type { Selection } from "./selection";
import type { ChartData, MetricName } from "./types";

export interface ViewContext {
  doc: Document;
  data: ChartData;
  projection: Projection;
  width: number;
  /** The chart's own container inside the mount. */
  container: HTMLElement;
  selection: Selection;
  /** Unique per mount; use it for any SVG id so charts can share a page. */
  instance: number;
  /** Per-view presentation settings (MountOptions.options). */
  options: Record<string, unknown>;
}

export interface ViewInstance {
  destroy(): void;
}

export type ViewRenderer = (context: ViewContext) => ViewInstance;

export interface PanelState {
  metric: MetricName;
  collapsedIds: string[];
}

/** Panels render beside the chart (into *parent*) and share its selection. */
export type PanelRenderer = (context: ViewContext, parent: HTMLElement, state: PanelState) => ViewInstance;
