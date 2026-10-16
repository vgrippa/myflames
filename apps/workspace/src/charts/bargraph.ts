// Bar chart, the TypeScript twin of myflames/output_bargraph.py: one bar per
// visible operator with measurable self time, sorted by self time (stable,
// so ties keep pre-order), an 8-color cycle, a 3px minimum width, and an
// 8-line details strip with the optimizer estimate ratio. Times arrive in
// milliseconds; the µs display_unit only scales what is printed.
//
// Hover shows details, click pins (and selects), regex search highlights
// matches with n/N to step and Esc to clear. Everything is instance-scoped.
import { svg } from "./core/dom";
import { commas, cpSlice, estimateText, fitLabel, pyFixed } from "./core/format";
import { BAR_CSS } from "./core/styles";
import { bindKeys, compile, createToolbar, isTyping } from "./core/toolbar";
import type { ChartNode } from "./core/types";
import type { ViewContext, ViewInstance } from "./core/view";

export const COLORS = [
  "rgb(255,90,90)", "rgb(255,130,70)", "rgb(255,165,50)", "rgb(255,200,50)",
  "rgb(255,220,80)", "rgb(200,200,100)", "rgb(150,200,150)", "rgb(100,180,180)",
];
export const BAR_H = 28;
export const BAR_GAP = 6;
export const TOP = 80;
const MARGIN = 10;
const DETAIL_LINES = 8;
const DETAIL_LINE_H = 14;
const BOTTOM = 16 + DETAIL_LINES * DETAIL_LINE_H + 12;
const LABEL_W = 320;
const LOOPS_W = 80;
const TIME_W = 120;
const SEP = "  ·  ";
const HINT = "Hover a bar for details; click to pin  ·  Ctrl+F to search";

export interface Bar { node: ChartNode; self: number; total: number }

const fixed = (value: number) => (value >= 1 ? pyFixed(value, 0) : pyFixed(value, 3));

/** Visible operators in display units, sorted and filtered like Python. */
export function bars(visible: ChartNode[], multiplier: number): Bar[] {
  return visible
    .map((node) => ({ node, self: (node.self_ms ?? 0) * multiplier, total: (node.total_ms ?? 0) * multiplier }))
    .sort((a, b) => b.self - a.self)
    .filter((bar) => bar.self >= 0.001);
}

/** Details text: output_bargraph._build_bar_info. */
export function barInfo(bar: Bar, pct: number, unit: string): string {
  const { node } = bar;
  const parts = [node.full_label || node.short_label || ""];
  parts.push("Self: " + fixed(bar.self) + " " + unit + " (" + pyFixed(pct, 1) + "%)");
  if (Math.abs(bar.total - bar.self) > 0.001) parts.push("Total: " + fixed(bar.total) + " " + unit);
  const rows = node.actual_rows ?? 0;
  parts.push("Rows: " + pyFixed(rows, 0));
  parts.push("Loops: " + commas(node.loops || 1));
  if (node.table) parts.push("Table: " + node.table);
  if (node.index) parts.push("Index: " + node.index);
  if (node.access_type) parts.push("Access: " + node.access_type);
  const estimate = estimateText(rows, node.estimated_rows, "Optimizer ");
  if (estimate) parts.push(estimate);
  const cond = node.condition;
  if (cond) parts.push("Cond: " + (Array.from(cond).length > 52 ? cpSlice(cond, 0, 50) + "…" : cond));
  return parts.join(SEP);
}

interface Item { node: ChartNode; rect: SVGRectElement; info: string; label: string }

export function renderBargraph(context: ViewContext): ViewInstance {
  const { doc, data, projection, selection, container } = context;
  const workspace = context.options.workspace === true;
  const width = Math.trunc(context.width || 1200);
  const unit = data.display_unit === "µs" ? "µs" : "ms";
  const multiplier = unit === "µs" ? 1000 : 1;
  const list = bars(projection.visible, multiplier);
  const rootTotal = (projection.root.total_ms ?? 0) * multiplier;
  const total = rootTotal || Math.max(0.001, list.reduce((sum, bar) => sum + bar.self, 0));
  const area = Math.max(40, width - MARGIN * 2 - LABEL_W - LOOPS_W - TIME_W - 20);
  const topY = workspace ? 32 : TOP;
  const bottomPad = workspace ? 8 : BOTTOM;
  const height = topY + list.length * (BAR_H + BAR_GAP) + bottomPad;
  const loopsX = MARGIN + LABEL_W;
  const barX = loopsX + LOOPS_W;
  const timeX = barX + area + 10;
  const sepY = height - BOTTOM + 6;

  const toolbar = createToolbar(doc, container, "Search operators (regex)");
  const root = svg(doc, "svg", {
    width, height, viewBox: "0 0 " + width + " " + height, class: "mfc-bar",
    role: "group", "aria-label": "Bar chart of self time per operator",
  }, container);
  svg(doc, "title", undefined, root, (data.title ? `${data.title}: ` : "") + "Bar chart");
  svg(doc, "style", undefined, root, BAR_CSS);
  svg(doc, "rect", { width: "100%", height: "100%", fill: "#fafafa" }, root);
  if (!workspace) {
    svg(doc, "text", { x: width / 2, y: 28, "text-anchor": "middle", class: "title" }, root, data.title || "");
    svg(doc, "text", { x: width / 2, y: 46, "text-anchor": "middle", class: "subtitle" }, root,
      "Self-time per operation" + SEP + "Total: " + fixed(total) + " " + unit);
  }
  const hdrY = workspace ? 20 : 68;
  const ruleY = workspace ? 24 : 72;
  svg(doc, "text", { x: MARGIN + LABEL_W - 10, y: hdrY, "text-anchor": "end", class: "col-header" }, root, "OPERATION");
  svg(doc, "text", { x: loopsX + LOOPS_W / 2, y: hdrY, "text-anchor": "middle", class: "col-header" }, root, "LOOPS");
  svg(doc, "text", { x: barX + area / 2, y: hdrY, "text-anchor": "middle", class: "col-header" }, root, "SELF-TIME");
  svg(doc, "line", { x1: MARGIN, y1: ruleY, x2: width - MARGIN, y2: ruleY, stroke: "#e0e0e0", "stroke-width": 1 }, root);
  const lines: SVGTextElement[] = [];
  if (!workspace) {
    svg(doc, "line", { x1: MARGIN, y1: sepY, x2: width - MARGIN, y2: sepY, stroke: "#e0e0e0", "stroke-width": 1 }, root);
    svg(doc, "rect", { x: MARGIN, y: sepY + 4, width: width - MARGIN * 2, height: BOTTOM - 10, fill: "#f5f5f5", rx: 4 }, root);
    for (let i = 0; i < DETAIL_LINES; i++) {
      lines.push(svg(doc, "text", { x: MARGIN + 10, y: sepY + 12 + i * DETAIL_LINE_H, "text-anchor": "start", class: "details-line" }, root, i === 0 ? HINT : ""));
    }
  }

  const items: Item[] = [];
  list.forEach((bar, i) => {
    const { node } = bar;
    const y = topY + i * (BAR_H + BAR_GAP);
    const pct = (bar.self / total) * 100;
    const barWidth = bar.self > 0 ? Math.max(3, (bar.self / total) * area) : 0;
    const textY = y + BAR_H / 2 + 4;
    const info = barInfo(bar, pct, unit);
    const highlight = node.highlight ? cpSlice(node.highlight, 0, 400) : "";
    svg(doc, "text", { x: MARGIN + LABEL_W - 10, y: textY, "text-anchor": "end", class: "label" }, root,
      fitLabel(node.short_label || "", LABEL_W - 14, 11, 0.55));
    svg(doc, "text", { x: loopsX + LOOPS_W / 2, y: textY, "text-anchor": "middle", class: "loops" }, root, commas(node.loops || 1));
    const label = node.short_label || node.full_label || "";
    const rect = svg(doc, "rect", {
      class: highlight ? "bar in-query-analysis" : "bar", x: barX, y, width: barWidth, height: BAR_H,
      fill: COLORS[i % COLORS.length], rx: 3, ry: 3, "data-node-id": node.id, "data-info": info,
      "data-label": label, "data-analysis-msg": highlight || null, "data-teach-index": node.teach_index,
      role: "button", tabindex: 0, "aria-label": label,
    }, root);
    svg(doc, "text", { x: timeX, y: textY, class: "value" }, root, fixed(bar.self) + " " + unit + " (" + pyFixed(pct, 1) + "%)");
    items.push({ node, rect, info: highlight ? info + SEP + "⚠ In Query Analysis: " + highlight : info, label });
  });

  // ---- interaction ----------------------------------------------------
  let pinned: Item | null = null;
  let matches: Item[] = [];
  let active = 0;
  let searchText = "";
  const setLines = (parts: string[]) => lines.forEach((line, i) => {
    line.textContent = i < parts.length ? parts[i] : "";
    line.setAttribute("class", "details-line" + (i > 0 && i >= parts.length - 1 && parts.length > 1 ? " dim" : ""));
  });
  const idle = () => setLines((searchText || HINT).split(SEP));
  const show = (item: Item) => setLines(item.info.split(SEP));

  const byId = new Map<string, Item>();
  const cleanups: Array<() => void> = [];
  const listen = <K extends keyof SVGElementEventMap>(target: Element, type: K, fn: (event: SVGElementEventMap[K]) => void) => {
    target.addEventListener(type, fn as EventListener);
    cleanups.push(() => target.removeEventListener(type, fn as EventListener));
  };
  for (const item of items) {
    byId.set(item.node.id, item);
    selection.register(item.node.id, item.rect);
    // A click always selects (and tells the parent); clicking the chart
    // background or pressing Esc releases the pinned details.
    const choose = () => selection.select(item.node.id, true);
    listen(item.rect, "click", (event: MouseEvent) => { event.stopPropagation(); choose(); });
    listen(item.rect, "keydown", (event: KeyboardEvent) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
    });
    listen(item.rect, "pointerenter", () => { if (!pinned) show(item); });
    listen(item.rect, "pointerleave", () => { if (!pinned) idle(); });
  }
  listen(root, "click", () => selection.clear());
  selection.listen((id) => {
    pinned = (id && byId.get(id)) || null;
    if (pinned) show(pinned); else idle();
  });

  const paintMatches = () => {
    items.forEach((item) => {
      const index = matches.indexOf(item);
      item.rect.style.opacity = !searchText ? "" : index === -1 ? "0.3" : "1";
      item.rect.style.stroke = index === -1 ? "" : index === active ? "rgb(230,0,230)" : "rgb(210,140,210)";
      item.rect.style.strokeWidth = index === -1 ? "" : index === active ? "3" : "1.5";
    });
  };
  const focusMatch = (index: number) => {
    if (!matches.length) return;
    const n = matches.length;
    active = ((index % n) + n) % n;
    paintMatches();
    const target = matches[active].rect as unknown as { scrollIntoView?: (options: ScrollIntoViewOptions) => void };
    if (target.scrollIntoView) target.scrollIntoView({ block: "nearest" });
    searchText = "Match " + (active + 1) + "/" + n + SEP + "n=next, N=prev, Esc=clear";
    toolbar.status.textContent = (active + 1) + " / " + n;
    if (!pinned) idle();
  };
  const search = () => {
    const term = toolbar.input.value;
    const pattern = term ? compile(term, "i") : null;
    toolbar.input.setAttribute("aria-invalid", term && !pattern ? "true" : "false");
    matches = pattern ? items.filter((item) => pattern.test(item.label)) : [];
    active = 0;
    searchText = !pattern ? "" : matches.length ? "" : "0 matches for " + term + SEP + "Esc to clear";
    toolbar.status.textContent = !term ? "" : pattern ? matches.length ? "" : "0 matches" : "Invalid regex";
    paintMatches();
    if (matches.length) focusMatch(0); else if (!pinned) idle();
  };
  const clearSearch = () => { toolbar.input.value = ""; search(); };
  listen(toolbar.input, "input", search);
  listen(toolbar.input, "keydown", (event: KeyboardEvent) => {
    if (event.key === "Enter") { event.preventDefault(); focusMatch(active + (event.shiftKey ? -1 : 1)); }
    else if (event.key === "Escape") { event.preventDefault(); clearSearch(); }
  });
  cleanups.push(bindKeys(container, () => toolbar.focus(), (event) => {
    if (isTyping(event.target) || event.ctrlKey || event.metaKey || event.altKey) return false;
    if (event.key === "/") { toolbar.focus(); return true; }
    if (event.key === "n" && matches.length) { focusMatch(active + 1); return true; }
    if (event.key === "N" && matches.length) { focusMatch(active - 1); return true; }
    if (event.key === "Escape" && toolbar.input.value) { clearSearch(); return true; }
    if (event.key === "Escape" && pinned) { selection.clear(); return true; }
    return false;
  }));

  root.setAttribute("data-wired", "true");
  return {
    destroy() {
      cleanups.splice(0).forEach((cleanup) => cleanup());
      root.remove();
      toolbar.element.remove();
    },
  };
}
