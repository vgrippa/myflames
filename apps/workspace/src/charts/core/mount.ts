// Instance-scoped mounting. Nothing here uses document-global IDs or
// "the first <svg>", so several charts can share one page.
import { createBridge, type BridgeWindow } from "./bridge";
import { html } from "./dom";
import { project } from "./project";
import { createSelection } from "./selection";
import { ensurePanelStyles } from "./styles";
import type { ChartData, ChartHandle, MetricName, MountOptions } from "./types";
import type { PanelRenderer, ViewInstance, ViewRenderer } from "./view";

const SCHEMAS = ["chart-data-1.0", "chart-data-2.0"];
const views = new Map<string, ViewRenderer>();
const panels = new Map<string, PanelRenderer>();
let instances = 0;

export function registerView(name: string, renderer: ViewRenderer) {
  views.set(name, renderer);
}

export function registerPanel(name: string, renderer: PanelRenderer) {
  panels.set(name, renderer);
}

export function availableViews(): string[] {
  return Array.from(views.keys());
}

/** Render one chart into *element*; returns a handle scoped to it. */
export function mount(element: HTMLElement, data: ChartData, options: MountOptions): ChartHandle {
  if (!data || !SCHEMAS.includes(data.schema_version)) {
    throw new Error("Unsupported chart data; expected " + SCHEMAS.join(" or ") + ".");
  }
  const doc = element.ownerDocument;
  const win = (doc.defaultView || window) as unknown as BridgeWindow;
  ensurePanelStyles(doc);
  const state = Object.assign({}, data.projection, options.projection || {});
  const metric: MetricName = state.metric in data.metrics_meta ? state.metric : "self_time";
  const projection = project(data, state.focus_id || "", state.collapsed_ids || []);
  const instance = ++instances;
  const container = html(doc, "div", { class: "mfc-chart chart" }, element);
  const parts: ViewInstance[] = [];
  let bridge: ReturnType<typeof createBridge> | null = null;
  const onSelect = options.onSelect;
  const selection = createSelection((id) => {
    if (onSelect) onSelect(id);
    if (bridge) bridge.notify(id);
  });
  const context = {
    doc, data, projection, width: options.width || 1200, container, selection, instance,
    options: options.options || {},
  };
  const renderer = views.get(options.view);
  if (renderer) {
    parts.push(renderer(context));
  } else {
    html(doc, "p", { class: "mfc-error", role: "status" }, container,
      "This chart type is not available in this runtime.");
  }
  for (const name of options.panels || []) {
    const panel = panels.get(name);
    if (panel) parts.push(panel(context, element, { metric, collapsedIds: state.collapsed_ids || [] }));
  }
  if (options.bridge !== false) bridge = createBridge(win, (id) => { selection.select(id, false); });
  if (state.selected_id) selection.select(state.selected_id, false);
  element.setAttribute("data-mfc-mounted", "true");
  return {
    element,
    select: (id, notify = false) => selection.select(id, notify),
    selected: () => selection.current(),
    clear: () => selection.clear(),
    destroy() {
      if (bridge) bridge.dispose();
      parts.splice(0).forEach((part) => part.destroy());
      container.remove();
      element.removeAttribute("data-mfc-mounted");
    },
  };
}

function words(value: string | null): string[] {
  return (value || "").split(/\s+/).filter(Boolean);
}

function settings(value: string | null): Record<string, unknown> {
  if (!value) return {};
  const parsed = JSON.parse(value) as unknown;
  return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
}

/**
 * Mount every pending ``.mfc-mount`` (see myflames/chart_assets.py). Each
 * holds its chart-data JSON in a child ``script.mfc-data``. Already mounted
 * elements are skipped; a broken chart reports in place without stopping
 * the others.
 */
export function mountAll(root: ParentNode = document): ChartHandle[] {
  const handles: ChartHandle[] = [];
  const pending = Array.from(root.querySelectorAll<HTMLElement>(".mfc-mount:not([data-mfc-mounted])"));
  for (const element of pending) {
    try {
      const source = element.querySelector("script.mfc-data");
      const data = JSON.parse((source && source.textContent) || "null") as ChartData;
      handles.push(mount(element, data, {
        view: element.getAttribute("data-mfc-view") || "",
        width: Number(element.getAttribute("data-mfc-width")) || 1200,
        panels: words(element.getAttribute("data-mfc-panels")),
        options: settings(element.getAttribute("data-mfc-options")),
      }));
    } catch (error) {
      element.setAttribute("data-mfc-mounted", "error");
      html(element.ownerDocument, "p", { class: "mfc-error", role: "alert" }, element,
        "Could not draw this chart: " + (error instanceof Error ? error.message : String(error)));
      // Surface the cause to developers without aborting other charts.
      console.error(error);
    }
  }
  return handles;
}
