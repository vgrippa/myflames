// Execution tree, the TypeScript twin of myflames/output_tree.py: one row per
// visible operator in pre-order (reported input order), indented by depth,
// with self/total time, share of the root, a log-scaled blue self bar over a
// total-time outline, zebra stripes and a warning accent for operators named
// by the analysis. Collapse/expand (per row and all), regex search and a
// pinned details strip are scoped to this chart instance.
//
// Geometry and text match the Python SVG (parity-tested); the in-SVG
// "Search" link is replaced by the toolbar input above the chart.
import { svg } from "./core/dom";
import { cpLength, cpSlice, estimateText, pyFixed, treeTime } from "./core/format";
import { TREE_CSS } from "./core/styles";
import { bindKeys, compile, createToolbar, isTyping } from "./core/toolbar";
import type { ChartNode } from "./core/types";
import type { ViewContext, ViewInstance } from "./core/view";

export const ROW_H = 26;
export const HEADER_H = 72;
const LEFT = 10;
const INDENT = 18;
const TOGGLE_W = 16;
const LABEL_W = 440;
const SELF_W = 90;
const TOTAL_W = 90;
const PCT_W = 55;
const DETAIL_LINES = 6;
const DETAIL_LINE_H = 16;
const DETAILS_H = 20 + DETAIL_LINES * DETAIL_LINE_H;
const SEP = "  ·  ";
const HINT = "Click a row to pin details  ·  Click ▾/▸ to expand/collapse  ·  Ctrl+F to search";
const LOW = [0xde, 0xeb, 0xf7];
const HIGH = [0x21, 0x71, 0xb5];

/** Parser self/total time: unmeasured operators count as 0 ms. */
export const selfMs = (node: ChartNode) => node.self_ms ?? 0;
export const totalMs = (node: ChartNode) => node.total_ms ?? 0;

/** Sequential blue, light = fast, dark = slow: output_tree._row_color. */
export function rowColor(self: number, rootTotal: number): string {
  if (!rootTotal || self <= 0) return "#e8f4fd";
  const ratio = Math.min(1, Math.log1p(self) / Math.log1p(Math.max(rootTotal, 0.001)));
  return "#" + LOW.map((low, i) => Math.trunc(low + (HIGH[i] - low) * ratio).toString(16).padStart(2, "0")).join("");
}

/** Details-strip text, segments joined by "  ·  ": output_tree._row_info. */
export function rowInfo(node: ChartNode, rootTotal: number): string {
  const parts: string[] = [];
  const full = node.full_label || node.short_label || "";
  if (full) parts.push(cpSlice(full, 0, 80));
  const self = selfMs(node);
  const total = totalMs(node);
  const pct = rootTotal > 0 ? (self / rootTotal) * 100 : 0;
  parts.push("Self: " + treeTime(self) + " (" + pyFixed(pct, 1) + "%)");
  if (Math.abs(total - self) > 0.001) parts.push("Total: " + treeTime(total));
  const rows = node.actual_rows ?? 0;
  // No row counter reads "not measured", never 0; null rows and loops (a
  // MySQL operator that never ran) read "not executed" (output_tree._row_info).
  parts.push(node.actual_rows !== null ? "Rows: " + pyFixed(rows, 0)
    : node.loops === null ? "Rows: not executed" : "Rows: not measured");
  const loops = node.loops || 1;
  if (loops > 1) parts.push("Loops: " + Math.trunc(loops));
  if (node.table) parts.push("Table: " + node.table);
  if (node.index) parts.push("Index: " + node.index);
  if (node.access_type) parts.push("Access: " + node.access_type);
  const estimate = estimateText(rows, node.estimated_rows, "");
  if (estimate) parts.push(estimate);
  const cond = node.condition;
  if (cond) parts.push("Cond: " + (cpLength(cond) > 52 ? cpSlice(cond, 0, 50) + "…" : cond));
  return parts.join(SEP);
}

interface Row {
  node: ChartNode;
  element: SVGGElement;
  toggle: SVGTextElement | null;
  depth: number;
  info: string;
  hasChildren: boolean;
  hidden: boolean;
  collapsed: boolean;
}

export function renderTree(context: ViewContext): ViewInstance {
  const { doc, data, projection, selection, container } = context;
  const workspace = context.options.workspace === true;
  const width = Math.trunc(context.width || 1200);
  const visible = projection.visible;
  const top = projection.root;
  const measuredTotal = totalMs(top);
  const rootTotal = measuredTotal || 0.001;
  const barX = LEFT + LABEL_W + SELF_W + TOTAL_W + PCT_W + 20;
  const barMax = Math.max(80, width - barX - LEFT - 10);
  const selfX = LEFT + LABEL_W;
  const totalX = selfX + SELF_W;
  const pctX = totalX + TOTAL_W;
  const headerH = workspace ? 30 : HEADER_H;
  const bottomY = headerH + visible.length * ROW_H + (workspace ? 8 : 12);
  const height = workspace ? bottomY : bottomY + DETAILS_H + 16;

  const toolbar = createToolbar(doc, container, "Search operators (regex)");
  const root = svg(doc, "svg", {
    width, height, viewBox: "0 0 " + width + " " + height, class: "mfc-tree",
    role: "group", "aria-label": "Execution tree",
  }, container);
  svg(doc, "title", undefined, root, (data.title ? `${data.title}: ` : "") + "Execution tree");
  svg(doc, "style", undefined, root, TREE_CSS);
  svg(doc, "rect", { width: "100%", height: "100%", class: "mfc-background", fill: "#fafafa" }, root);
  if (!workspace) {
    svg(doc, "text", { x: width / 2, y: 26, "text-anchor": "middle", class: "title" }, root, data.title || "");
    // An unmeasured plan reads "—", not the 0.001 ms floor used for scaling.
    svg(doc, "text", { x: LEFT, y: 44, "text-anchor": "start", class: "subtitle" }, root,
      "Execution tree" + SEP + "Total: " + treeTime(measuredTotal) + SEP + "Click row to pin" + SEP + "Ctrl+F to search");
  }
  const linkY = workspace ? 20 : 44;
  const link = (x: number, anchor: string, text: string) => svg(doc, "text", {
    class: "ctrl-link", x, y: linkY, "text-anchor": anchor, role: "button", tabindex: 0,
  }, root, text);
  // End-anchored with a gap in standalone SVG; left-aligned in the operator
  // header column in workspace mode.
  const expandLink = workspace ? link(LEFT, "start", "Expand All") : link(width - LEFT - 80, "end", "Expand All");
  const collapseLink = workspace ? link(LEFT + 72, "start", "Collapse All") : link(width - LEFT, "end", "Collapse All");
  const rule = (y: number, stroke: string) =>
    svg(doc, "line", { class: "mfc-rule", x1: LEFT, y1: y, x2: width - LEFT, y2: y, stroke, "stroke-width": 1 }, root);
  if (!workspace) rule(52, "#ddd");
  const hdrY = workspace ? 20 : 65;
  svg(doc, "text", { x: selfX + SELF_W - 4, y: hdrY, "text-anchor": "end", class: "col-hdr" }, root, "SELF");
  svg(doc, "text", { x: totalX + TOTAL_W - 4, y: hdrY, "text-anchor": "end", class: "col-hdr" }, root, "TOTAL");
  svg(doc, "text", { x: pctX + PCT_W - 4, y: hdrY, "text-anchor": "end", class: "col-hdr" }, root, "%");
  svg(doc, "text", { x: barX, y: hdrY, "text-anchor": "start", class: "col-hdr" }, root, "SELF TIME BAR");
  rule(workspace ? 25 : 68, "#ddd");

  const rows: Row[] = [];
  visible.forEach((node, idx) => {
    const y = headerH + idx * ROW_H;
    const depth = node.depth - top.depth;
    const hasChildren = projection.children(node).length > 0;
    const self = selfMs(node);
    const total = totalMs(node);
    const pct = (self / rootTotal) * 100;
    let label = node.short_label || "";
    if (cpLength(label) > 46) label = cpSlice(label, 0, 45) + "…";
    const warn = node.highlight !== null && node.highlight !== undefined;
    const info = rowInfo(node, rootTotal);
    const textY = y + ROW_H - 8;
    const indentX = LEFT + depth * INDENT;
    const group = svg(doc, "g", {
      class: "tree-row", "data-node-id": node.id, "data-idx": idx, "data-depth": depth,
      "data-has-children": hasChildren ? 1 : 0, "data-teach-index": node.teach_index,
      role: "button", tabindex: 0, "aria-label": node.short_label || node.full_label || "Operator",
      "aria-expanded": hasChildren ? "true" : null,
    }, root);
    svg(doc, "rect", { class: "row-bg", x: LEFT, y, width: width - 2 * LEFT, height: ROW_H, fill: "transparent", "data-info": info }, group);
    if (warn) svg(doc, "rect", { x: LEFT, y, width: 4, height: ROW_H, fill: "#e65100", "pointer-events": "none" }, group);
    if (idx % 2 === 1) {
      svg(doc, "rect", { x: LEFT, y, width: width - 2 * LEFT, height: ROW_H, class: "mfc-stripe", fill: "rgba(0,0,0,0.018)", "pointer-events": "none" }, group);
    }
    const toggle = hasChildren
      ? svg(doc, "text", { class: "toggle", x: indentX + 2, y: textY, "data-idx": idx }, group, "▾")
      : null;
    if (!hasChildren) {
      svg(doc, "text", { class: "leaf-indicator", x: indentX + 2, y: textY, "pointer-events": "none" }, group, "·");
    }
    svg(doc, "text", { class: warn ? "row-label warn" : "row-label", x: indentX + TOGGLE_W, y: textY, "pointer-events": "none" },
      group, (warn ? "⚠ " : "") + label);
    svg(doc, "text", { class: "row-time", x: selfX + SELF_W - 4, y: textY, "text-anchor": "end", "pointer-events": "none" }, group, treeTime(self));
    if (Math.abs(total - self) > 0.001) {
      svg(doc, "text", { class: "row-time", x: totalX + TOTAL_W - 4, y: textY, "text-anchor": "end", "pointer-events": "none" }, group, treeTime(total));
    }
    svg(doc, "text", { class: "row-pct", x: pctX + PCT_W - 4, y: textY, "text-anchor": "end", "pointer-events": "none" },
      group, (pct >= 1 ? pyFixed(pct, 0) : pyFixed(pct, 1)) + "%");
    const selfBar = Math.max(0, (self / rootTotal) * barMax);
    const totalBar = Math.max(0, (total / rootTotal) * barMax);
    if (totalBar > 0.5) {
      svg(doc, "rect", { x: barX, y: y + 5, width: pyFixed(totalBar, 1), height: ROW_H - 10, fill: "none", stroke: "#c0d8ee", "stroke-width": 1, rx: 2, "pointer-events": "none" }, group);
    }
    if (selfBar > 0.5) {
      svg(doc, "rect", { x: barX, y: y + 5, width: pyFixed(selfBar, 1), height: ROW_H - 10, fill: rowColor(self, rootTotal), rx: 2, "pointer-events": "none" }, group);
    }
    svg(doc, "line", { x1: LEFT, y1: y + ROW_H, x2: width - LEFT, y2: y + ROW_H, class: "mfc-rule", stroke: "#eeeeee", "stroke-width": 1 }, group);
    rows.push({ node, element: group, toggle, depth, info, hasChildren, hidden: false, collapsed: false });
  });

  const lines: SVGTextElement[] = [];
  if (!workspace) {
    const sepY = bottomY + 4;
    rule(sepY, "#e0e0e0");
    svg(doc, "rect", { x: LEFT, y: sepY + 4, width: width - 2 * LEFT, height: DETAILS_H, class: "mfc-pane", fill: "#eeeeee", rx: 4 }, root);
    for (let i = 0; i < DETAIL_LINES; i++) {
      lines.push(svg(doc, "text", { x: LEFT + 10, y: sepY + 20 + i * DETAIL_LINE_H, class: "details-line" }, root, i === 0 ? HINT : ""));
    }
  }

  // ---- interaction (all state is local to this instance) -----------------
  let pinned: Row | null = null;
  let searchText = "";
  const setLines = (parts: string[]) => lines.forEach((line, i) => { line.textContent = i < parts.length ? parts[i] : ""; });
  const idle = () => setLines(searchText ? [searchText] : [HINT]);
  const show = (info: string) => (info ? setLines(info.split(SEP)) : idle());

  const reflow = () => {
    let y = headerH;
    rows.forEach((row, i) => {
      if (row.hidden) {
        row.element.style.display = "none";
        return;
      }
      row.element.style.display = "";
      const dy = y - (headerH + i * ROW_H);
      if (dy) row.element.setAttribute("transform", "translate(0," + dy + ")");
      else row.element.removeAttribute("transform");
      y += ROW_H;
    });
    if (workspace) {
      const nextH = y + 8;
      root.setAttribute("height", String(nextH));
      root.setAttribute("viewBox", "0 0 " + width + " " + nextH);
    }
  };
  const setHidden = (index: number, hidden: boolean) => {
    const depth = rows[index].depth;
    for (let j = index + 1; j < rows.length && rows[j].depth > depth; j++) {
      rows[j].hidden = hidden;
      if (!hidden && rows[j].collapsed) {
        // A still-collapsed descendant keeps its own subtree hidden.
        setHidden(j, true);
        const inner = rows[j].depth;
        while (j + 1 < rows.length && rows[j + 1].depth > inner) j++;
      }
    }
  };
  const mark = (row: Row) => {
    if (!row.toggle) return;
    row.toggle.textContent = row.collapsed ? "▸" : "▾";
    row.element.setAttribute("aria-expanded", row.collapsed ? "false" : "true");
  };
  const toggle = (index: number, collapse = !rows[index].collapsed) => {
    const row = rows[index];
    if (!row.hasChildren || row.collapsed === collapse) return;
    row.collapsed = collapse;
    mark(row);
    setHidden(index, collapse);
    reflow();
  };
  const expandAll = () => {
    rows.forEach((row) => { row.hidden = false; row.collapsed = false; mark(row); });
    reflow();
  };
  const collapseAll = () => {
    rows.forEach((row) => {
      if (row.depth > 0) row.hidden = true;
      if (row.hasChildren) { row.collapsed = true; mark(row); }
    });
    reflow();
  };
  /** Expand collapsed ancestors so a selected operator is visible. */
  const reveal = (index: number) => {
    const ancestors: number[] = [];
    let depth = rows[index].depth;
    for (let j = index - 1; j >= 0 && depth > 0; j--) {
      if (rows[j].depth < depth) { ancestors.unshift(j); depth = rows[j].depth; }
    }
    ancestors.forEach((j) => toggle(j, false));
  };

  const byId = new Map<string, number>();
  const cleanups: Array<() => void> = [];
  const listen = <K extends keyof SVGElementEventMap>(target: Element, type: K, fn: (event: SVGElementEventMap[K]) => void) => {
    target.addEventListener(type, fn as EventListener);
    cleanups.push(() => target.removeEventListener(type, fn as EventListener));
  };
  rows.forEach((row, index) => {
    byId.set(row.node.id, index);
    selection.register(row.node.id, row.element);
    // A click always selects (and tells the parent); clicking the chart
    // background or pressing Esc releases the pinned details.
    const choose = () => selection.select(row.node.id, true);
    listen(row.element, "click", (event: MouseEvent) => {
      event.stopPropagation();
      if (row.toggle && event.target === row.toggle) { toggle(index); return; }
      choose();
    });
    listen(row.element, "keydown", (event: KeyboardEvent) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
      else if (event.key === "ArrowRight" && row.hasChildren) { event.preventDefault(); toggle(index, false); }
      else if (event.key === "ArrowLeft" && row.hasChildren) { event.preventDefault(); toggle(index, true); }
    });
    listen(row.element, "pointerenter", () => { if (!pinned) show(row.info); });
    listen(row.element, "pointerleave", () => { if (!pinned) idle(); });
  });
  for (const [element, action] of [[expandLink, expandAll], [collapseLink, collapseAll]] as Array<[SVGTextElement, () => void]>) {
    listen(element, "click", (event: MouseEvent) => { event.stopPropagation(); action(); });
    listen(element, "keydown", (event: KeyboardEvent) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); action(); }
    });
  }
  // Clicking outside every row releases the pinned details.
  listen(root, "click", () => selection.clear());

  selection.listen((id) => {
    const index = id ? byId.get(id) : undefined;
    pinned = index === undefined ? null : rows[index];
    if (index === undefined) { idle(); return; }
    reveal(index);
    show(rows[index].info);
  });

  const search = () => {
    const term = toolbar.input.value;
    const pattern = term ? compile(term, "i") : null;
    toolbar.input.setAttribute("aria-invalid", term && !pattern ? "true" : "false");
    let count = 0;
    rows.forEach((row) => {
      const match = !pattern || pattern.test(row.info);
      row.element.classList.toggle("mfc-dim", !!term && !match);
      if (match) count++;
    });
    searchText = pattern ? "Matches: " + term + SEP + "Esc to clear" : "";
    toolbar.status.textContent = !term ? "" : pattern ? count + " / " + rows.length : "Invalid regex";
    if (!pinned) idle();
  };
  const clearSearch = () => { toolbar.input.value = ""; search(); };
  listen(toolbar.input, "input", search);
  listen(toolbar.input, "keydown", (event: KeyboardEvent) => {
    if (event.key === "Escape") { event.preventDefault(); clearSearch(); }
  });
  cleanups.push(bindKeys(container, () => toolbar.focus(), (event) => {
    if (event.key !== "Escape" || isTyping(event.target)) return false;
    if (toolbar.input.value) clearSearch(); else if (pinned) selection.clear(); else return false;
    return true;
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
