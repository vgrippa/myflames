// Visual Explain (Workbench 26.7 conventions), the TypeScript twin of
// myflames/output_workbench.py + workbench.js. Python decides operator kind,
// reported join type, input phase, arrow width and details text; this file
// lays out and draws them.
//
// Invariants: every visible node and parent-child edge is drawn exactly once
// in reported input order; edges attach to the actual card bounds (join
// cards are taller); estimated cost is never printed as milliseconds; time
// appears only when actual_last_row_ms was measured; MariaDB synthetic joins
// keep the "unknown" (unshaded) symbol.
import { createDetails } from "./core/details";
import { f1, html, svg } from "./core/dom";
import { fitLabel, fmt, time } from "./core/format";
import { bindSearch } from "./core/search";
import { WORKBENCH_CSS } from "./core/styles";
import type { ChartNode, WorkbenchKind } from "./core/types";
import type { ViewContext, ViewInstance } from "./core/view";
import { createZoom } from "./core/zoom";

export const BOX_W = 256;
export const BOX_H = 126;
export const JOIN_H = 186;
export const H_GAP = 76;
export const V_GAP = 90;
export const VIEWPORT_Y = 136;
export const VIEWPORT_H = 660;

/** Operator palette; must match output_workbench.COLORS (parity-tested). */
export const COLORS: Record<WorkbenchKind | "result", string> = {
  table: "#db9292", lookup: "#9ac59d", range: "#e3ca83",
  index: "#e4b181", nested_loop: "#c998b3", hash: "#9ecbcb",
  bka: "#a9d8dd", operation: "#d1cfad", materialize: "#b4a4d8",
  unknown: "#ccd2d8", result: "#bec7ce",
};

const LEGEND: Array<[string, WorkbenchKind]> = [
  ["Table scan", "table"], ["Index lookup", "lookup"], ["Index range", "range"],
  ["Index scan", "index"], ["Join", "nested_loop"], ["Hash", "hash"],
  ["Operation", "operation"], ["Materialize", "materialize"],
];

const ALGORITHMS: Partial<Record<WorkbenchKind, string>> = { hash: "Hash", nested_loop: "Nested loop", bka: "BKA" };
const PLACEHOLDER = "Select a node to inspect its details. Drag to pan; scroll gently to zoom. Timings are across loops; optimizer cost is a separate estimate.";

export const isJoin = (node: ChartNode) => node.wb_kind === "nested_loop" || node.wb_kind === "hash" || node.wb_kind === "bka";
export const nodeHeight = (node: ChartNode) => (isJoin(node) ? JOIN_H : BOX_H);

export interface PlacedNode { node: ChartNode; x: number; y: number; height: number }
export interface PlacedEdge { from: PlacedNode; to: PlacedNode; phase: string }

interface Measured { node: ChartNode; children: Measured[]; width: number; height: number; cx: number; cy: number }

/**
 * Lay out whole child subtrees in order and align each parent with its last
 * input (output_workbench._layout). Coordinates are canvas-independent.
 */
export function layout(root: ChartNode, inputs: (node: ChartNode) => ChartNode[]) {
  const measure = (node: ChartNode): Measured => {
    const children = inputs(node).map(measure);
    const width = children.reduce((sum, child) => sum + child.width, 0) + H_GAP * Math.max(0, children.length - 1);
    const depth = children.reduce((most, child) => Math.max(most, child.height), 0);
    const lastX = children.slice(0, -1).reduce((sum, child) => sum + child.width + H_GAP, 0);
    const last = children[children.length - 1];
    return {
      node, children, width: Math.max(BOX_W, width),
      height: nodeHeight(node) + (children.length ? depth + V_GAP : 0),
      cx: last ? lastX + last.cx : BOX_W / 2,
      cy: children.length ? depth + V_GAP : 0,
    };
  };
  const measured = measure(root);
  const nodes: PlacedNode[] = [];
  const edges: PlacedEdge[] = [];
  const place = (item: Measured, x: number, y: number): PlacedNode => {
    const here = { node: item.node, x: x + item.cx - BOX_W / 2, y: y + item.cy, height: nodeHeight(item.node) };
    nodes.push(here);
    let offset = 0;
    for (const child of item.children) {
      const childY = y + item.cy - V_GAP - child.height;
      const there = place(child, x + offset, childY);
      edges.push({ from: there, to: here, phase: child.node.phase });
      offset += child.width + H_GAP;
    }
    return here;
  };
  place(measured, 0, 0);
  return { nodes, edges, width: measured.width, height: measured.height };
}

function joinSymbol(doc: Document, parent: Element, joinType: string, label: string, meaning: string) {
  // Intersections of radius-44 circles at (96,60) and (160,60).
  const top = 60 - Math.sqrt(44 * 44 - 32 * 32);
  const bottom = 120 - top;
  const lens = "M128 " + top.toFixed(3) + " A44 44 0 0 1 128 " + bottom.toFixed(3) + " A44 44 0 0 1 128 " + top.toFixed(3) + " Z";
  const group = svg(doc, "g", { class: "wb-venn", role: "img", "aria-label": label + ": " + meaning, "data-join-type": joinType }, parent);
  for (const cx of [96, 160]) svg(doc, "circle", { cx, cy: 60, r: 44, fill: "#f5f5f7" }, group);
  const fill = "#b7d9ff";
  if (joinType === "left" || joinType === "right" || joinType === "full" || joinType === "anti") {
    const centers = joinType === "full" ? [96, 160] : joinType === "right" ? [160] : [96];
    for (const cx of centers) {
      svg(doc, "circle", { class: "wb-join-highlight", "data-region": cx === 160 ? "right" : "left", cx, cy: 60, r: 44, fill }, group);
    }
    if (joinType === "anti") svg(doc, "path", { "data-region": "excluded-overlap", d: lens, fill: "#f5f5f7" }, group);
  } else if (joinType === "inner" || joinType === "semi") {
    svg(doc, "path", { class: "wb-join-highlight", "data-region": "overlap", d: lens, fill }, group);
  }
  for (const cx of [96, 160]) {
    svg(doc, "circle", { cx, cy: 60, r: 44, fill: "none", stroke: "#6e7d8c", "stroke-width": 1.5 }, group);
  }
  svg(doc, "text", { x: 78, y: 64, "text-anchor": "middle", class: "wb-side" }, group, "L");
  svg(doc, "text", { x: 178, y: 64, "text-anchor": "middle", class: "wb-side" }, group, "R");
  if (joinType === "cross") svg(doc, "text", { x: 128, y: 64, "text-anchor": "middle" }, group, "×");
}

export function renderWorkbench(context: ViewContext): ViewInstance {
  const { doc, data, projection, selection, container, instance } = context;
  const workspace = context.options.workspace === true;
  const width = Math.max(workspace ? 320 : 720, Math.trunc(context.width || (workspace && container.clientWidth) || 1200));
  const placed = layout(projection.root, projection.children);
  const graphWidth = placed.width;
  // A synthetic output terminal follows the root; it is not an operator.
  const graphHeight = placed.height + 90;
  const detailsY = VIEWPORT_Y + VIEWPORT_H + 8;
  const height = workspace ? VIEWPORT_H : detailsY + 180;
  const title = data.title || "Visual Explain";
  const joinLabels = data.labels.join_types;
  const joinLabel = (type: string) => joinLabels[type] || joinLabels.unknown || { label: "Join", meaning: "" };
  const arrowId = "mfc-arrow-" + instance;

  const root = svg(doc, "svg", {
    width, height, viewBox: "0 0 " + width + " " + height, class: "wb-plan mfc-workbench",
    role: "group", "aria-label": "Visual Explain",
    "aria-description": "Data flows downward · Colors identify operators · Arrow width = estimated rows (log scale)",
  }, container);
  svg(doc, "title", undefined, root, (data.title ? `${data.title}: ` : "") + "Visual Explain");
  svg(doc, "style", undefined, root, WORKBENCH_CSS);
  svg(doc, "rect", { width: "100%", height: "100%", class: "mfc-background", fill: "#f8fafb" }, root);
  if (!workspace) {
    svg(doc, "text", { x: 20, y: 29, class: "wb-heading" }, root, title);
    svg(doc, "text", { x: 20, y: 50, class: "wb-caption" }, root,
      "Data flows downward · Colors identify operators · Arrow width = estimated rows (log scale)");
  }
  const controlsObject = workspace ? container : svg(doc, "foreignObject", { x: 20, y: 61, width: width - 40, height: 42 }, root);
  const controls = html(doc, "div", { class: "wb-controls" }, controlsObject);
  const button = (action: string, text: string, label?: string) =>
    html(doc, "button", { type: "button", "data-action": action, "aria-label": label, title: action === "reset" ? label : undefined }, controls, text);
  const fitButton = button("fit", "Fit plan");
  const inButton = button("in", "+", "Zoom in");
  const outButton = button("out", "−", "Zoom out");
  const resetButton = button("reset", "100%", "Reset zoom to 100%");
  const search = html(doc, "input", { type: "search", "aria-label": "Search plan nodes", placeholder: "Search plan nodes" }, controls);
  const status = html(doc, "span", { class: "wb-search-status", "aria-live": "polite" }, controls);
  // Workspace chrome is HTML, so legend text remains readable when the
  // canvas narrows. Reports retain their original SVG geometry for export.
  const fillLegend = (target: HTMLElement) => {
    for (const [label, kind] of LEGEND) {
      const item = html(doc, "span", undefined, target);
      const swatch = html(doc, "i", { "aria-hidden": "true" }, item);
      swatch.style.backgroundColor = COLORS[kind];
      html(doc, "span", undefined, item, label);
    }
    html(doc, "span", { class: "wb-pan-hint" }, target, "Drag to pan");
  };
  let legend: HTMLElement | null = null;
  if (workspace) {
    const legendPopover = html(doc, "details", { class: "wb-legend-popover" }, controls);
    html(doc, "summary", { class: "wb-legend-trigger" }, legendPopover, "Legend");
    fillLegend(html(doc, "div", { class: "wb-legend-menu", role: "group", "aria-label": "Operator colors" }, legendPopover));
    legend = html(doc, "div", { class: "wb-legend", role: "group", "aria-label": "Operator colors" }, container);
    fillLegend(legend);
    root.before(legend);
  } else {
    LEGEND.forEach(([label, kind], i) => {
      const x = 20 + i * ((width - 40) / 8);
      svg(doc, "rect", { x: f1(x), y: 112, width: 10, height: 10, fill: COLORS[kind] }, root);
      svg(doc, "text", { x: f1(x + 15), y: 121, class: "wb-caption" }, root, label);
    });
  }

  const viewport = svg(doc, "svg", {
    x: 0, y: workspace ? 0 : VIEWPORT_Y, width, height: VIEWPORT_H, viewBox: "0 0 " + width + " " + VIEWPORT_H,
    class: "wb-viewport", style: "touch-action:none;cursor:grab;overflow:hidden",
    "data-graph-width": graphWidth, "data-graph-height": graphHeight,
  }, root);
  const marker = svg(doc, "marker", {
    id: arrowId, markerWidth: 8, markerHeight: 8, refX: 7, refY: 4, orient: "auto", markerUnits: "userSpaceOnUse",
  }, svg(doc, "defs", undefined, viewport));
  svg(doc, "path", { d: "M0 0 L8 4 L0 8 Z", class: "mfc-arrow-head", fill: "#8795a1" }, marker);
  svg(doc, "rect", { x: -100000, y: -100000, width: 200000, height: 200000, class: "mfc-background", fill: "#f8fafb" }, viewport);
  const graph = svg(doc, "g", { class: "wb-graph" }, viewport);

  for (const edge of placed.edges) {
    const source = edge.from;
    const target = edge.to;
    const sx = source.x + BOX_W / 2;
    const sy = source.y + source.height;
    const tx = target.x + BOX_W / 2;
    const ty = target.y;
    const bend = sy + V_GAP * 0.63;
    const child = source.node;
    svg(doc, "path", {
      class: "wb-edge mfc-edge", "data-from": child.id, "data-to": target.node.id,
      d: "M" + f1(sx) + " " + f1(sy) + " V" + f1(bend) + " H" + f1(tx) + " V" + f1(ty - 5),
      fill: "none", stroke: "#8795a1", "stroke-width": child.arrow_width, "marker-end": "url(#" + arrowId + ")",
    }, graph);
  }
  for (const edge of placed.edges) {
    const source = edge.from;
    const sy = source.y + source.height;
    const child = source.node;
    svg(doc, "text", { x: f1(source.x + 8), y: f1(sy + 23), class: "wb-edge-label" }, graph,
      "Rows/loop: " + fmt(child.actual_rows) + " actual · " + fmt(child.estimated_rows) + " est.");
    if (edge.phase) svg(doc, "text", { x: f1(source.x + 8), y: f1(sy + 41), class: "wb-edge-label" }, graph, edge.phase);
  }

  const items: Array<{ element: SVGGElement; node: ChartNode }> = [];
  for (const item of placed.nodes) {
    const node = item.node;
    const label = node.short_label || "Unknown operator";
    const group = svg(doc, "g", {
      class: "wb-node", role: "button", tabindex: 0, "aria-label": label, "data-node-id": node.id,
      "data-details": node.details_text, "data-label": label, "data-kind": node.wb_kind,
      transform: "translate(" + f1(item.x) + " " + f1(item.y) + ")",
      "data-teach-index": node.teach_index === null ? null : node.teach_index,
    }, graph);
    svg(doc, "title", undefined, group, node.details_text);
    svg(doc, "rect", { class: "wb-box", width: BOX_W, height: item.height, rx: 7, fill: "white", stroke: "#c4cfd6" }, group);
    if (isJoin(node)) {
      const joinType = node.join_type || "unknown";
      const names = joinLabel(joinType);
      svg(doc, "rect", { x: 8, width: 240, height: 3, rx: 1.5, fill: COLORS[node.wb_kind] }, group);
      joinSymbol(doc, group, joinType, names.label, names.meaning);
      const heading = names.label + " · " + (ALGORITHMS[node.wb_kind] || "Join");
      svg(doc, "text", { x: 128, y: 124, "text-anchor": "middle", class: "wb-title" }, group, fitLabel(heading, BOX_W - 24, 13));
    } else {
      let secondary = node.table || node.condition || node.index || node.operation || "";
      if (node.table && node.index) secondary += " · " + node.index;
      const heading = node.table && label.includes(" [") && label.endsWith("]")
        ? label.slice(0, label.indexOf(" ["))
        : (!node.table && node.condition && label.startsWith("Filter: ") ? "Filter" : label);
      svg(doc, "path", { d: "M7 0 H249 Q256 0 256 7 V34 H0 V7 Q0 0 7 0", fill: COLORS[node.wb_kind] || COLORS.unknown }, group);
      svg(doc, "text", { x: 12, y: 23, class: "wb-title wb-band-title" }, group, fitLabel(heading, BOX_W - 24, 13));
      svg(doc, "text", { x: 12, y: 57 }, group, fitLabel(secondary, BOX_W - 24, 12));
    }
    const timing = "Self " + time(node.measured ? node.self_ms : null) + " · total " + time(node.measured ? node.total_ms : null);
    svg(doc, "text", { x: 12, y: item.height - 35, class: "wb-caption" }, group, timing);
    svg(doc, "text", { x: 12, y: item.height - 14, class: "wb-caption" }, group,
      "Est. cost " + fmt(node.estimated_cost) + " · loops " + fmt(node.loops));
    items.push({ element: group, node });
  }

  const first = placed.nodes[0];
  const terminalX = first.x + BOX_W / 2;
  const terminalY = first.y + first.height;
  svg(doc, "path", { d: "M" + f1(terminalX) + " " + f1(terminalY) + " v40", class: "mfc-edge", stroke: "#8795a1", "stroke-width": 2, "marker-end": "url(#" + arrowId + ")" }, graph);
  svg(doc, "rect", { x: f1(terminalX - 80), y: f1(terminalY + 46), width: 160, height: 32, rx: 5, class: "mfc-pane", fill: "#dbe3e8" }, graph);
  svg(doc, "text", { x: f1(terminalX), y: f1(terminalY + 67), "text-anchor": "middle" }, graph, "Plan output");

  let details: ReturnType<typeof createDetails> | null = null;
  if (!workspace) {
    const detailsObject = svg(doc, "foreignObject", { x: 20, y: detailsY, width: width - 40, height: 150 }, root);
    const detailsElement = html(doc, "div", { class: "wb-details", tabindex: 0, role: "status" }, detailsObject);
    details = createDetails(detailsElement, PLACEHOLDER);
    svg(doc, "text", { x: 20, y: detailsY + 168, class: "wb-caption" }, root,
      "Visual conventions: MySQL Workbench 26.7 · Data and analysis: myflames");
  }

  const zoom = createZoom({
    root, viewport, width, height: VIEWPORT_H, graphWidth, graphHeight,
    buttons: { fit: fitButton, zoomIn: inButton, zoomOut: outButton, reset: resetButton },
    fitWidth: workspace,
  });
  // Match both SVG coordinate systems to the actual flex canvas. Otherwise
  // preserveAspectRatio letterboxes a 1200-wide drawing inside phone panes
  // and silently scales it a second time after the zoom controller's fit.
  const resize = workspace ? new ResizeObserver(([entry]) => {
    const { width: w, height: h } = entry.contentRect;
    if (w <= 0 || h <= 0) return;
    root.setAttribute("width", String(w));
    root.setAttribute("height", String(h));
    root.setAttribute("viewBox", `0 0 ${w} ${h}`);
    viewport.setAttribute("width", String(w));
    viewport.setAttribute("height", String(h));
    zoom.resize(w, h);
  }) : null;
  resize?.observe(root);
  const unbindSearch = bindSearch(search, status, items.map(({ element, node }) => ({ element, text: node.details_text })));

  const byId = new Map<string, SVGGElement>();
  const cleanups: Array<() => void> = [];
  for (const { element, node } of items) {
    byId.set(node.id, element);
    selection.register(node.id, element);
    const click = (event: Event) => {
      // A drag that started on a card pans; it must not select.
      if (zoom.dragged()) { event.preventDefault(); return; }
      selection.select(node.id, true);
    };
    const key = (event: KeyboardEvent) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selection.select(node.id, true);
    };
    element.addEventListener("click", click);
    element.addEventListener("keydown", key);
    cleanups.push(() => { element.removeEventListener("click", click); element.removeEventListener("keydown", key); });
  }
  let selected: SVGGElement | null = null;
  selection.listen((id) => {
    const element = byId.get(id);
    if (selected) selected.classList.remove("selected");
    selected = element || null;
    if (!element) { details?.reset(); return; }
    element.classList.add("selected");
    details?.show(element.getAttribute("data-details") || "");
  });

  zoom.fit();
  root.setAttribute("data-wired", "true");
  return {
    destroy() {
      zoom.destroy();
      resize?.disconnect();
      legend?.remove();
      unbindSearch();
      cleanups.splice(0).forEach((cleanup) => cleanup());
      root.remove();
    },
  };
}
