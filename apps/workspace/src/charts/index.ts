// myflames-charts: the browser runtime for chart-data-1.0 payloads.
// Built as an IIFE (global MyflamesCharts) into myflames/assets by
// vite.charts.config.ts and inlined by myflames/chart_assets.py. The React
// workspace may import these modules directly. No React or d3 here.
import { barInfo, bars, renderBargraph } from "./bargraph";
import { createBridge } from "./core/bridge";
import { commas, fitLabel, fmt, pyFixed, pyG, time, treeTime } from "./core/format";
import { availableViews, mount, mountAll, registerPanel, registerView } from "./core/mount";
import { project } from "./core/project";
import { createSelection } from "./core/selection";
import { fitFrameLabel, flameHeader, flameScale, flameUnits, flameValueText, frameColor, labelHash, layoutFrames, renderFlamegraph } from "./flamegraph";
import { renderMetrics } from "./metrics";
import { renderTree, rowColor, rowInfo } from "./tree";
import { layout, renderWorkbench } from "./workbench";

declare const __MFC_SOURCES_SHA__: string;

registerView("workbench", renderWorkbench);
// The former diagram view is an alias of Visual Explain.
registerView("diagram", renderWorkbench);
registerView("tree", renderTree);
registerView("bargraph", renderBargraph);
registerView("flamegraph", renderFlamegraph);
registerPanel("metrics", renderMetrics);

export const sourcesSha: string = typeof __MFC_SOURCES_SHA__ === "string" ? __MFC_SOURCES_SHA__ : "dev";
export { availableViews, mount, mountAll, project, createBridge };
export type { ChartData, ChartHandle, ChartNode, MountOptions } from "./core/types";

/** Pure helpers exposed for the Node/browser unit tests. */
export const internals = {
  barInfo, bars, commas, createSelection, fitFrameLabel, fitLabel, flameHeader, flameScale, flameUnits, flameValueText, fmt, frameColor,
  labelHash, layout, layoutFrames, pyFixed, pyG, rowColor, rowInfo, time, treeTime,
};
