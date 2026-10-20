// SPDX-License-Identifier: Apache-2.0
//
// Flame graph view. An original implementation written for myflames from a
// functional specification, licensed under the Apache License 2.0.
//
// The TypeScript twin of myflames/flamegraph.py: given the same plan it draws
// the same frames (identity, pre-order, x/width, depth, colors, fitted labels
// and tooltip header), checked by test/parity.mjs. Python builds its frames
// from folded stacks of whole display units; this view rounds each
// operator's self time the same way (flameScale/flameUnits mirror
// flamegraph.flame_scale/flame_units: whole µs for any plan with measured
// time, and a positive time is never less than 1 µs), so a
// frame's width is its own units plus all units beneath it. Inputs keep
// their reported order, packed from the parent's left edge. The unit sets
// widths only; headers print each time in a readable unit (flameValueText).
//
// Click (or Enter/Space) selects a frame and zooms to it: its subtree fills
// the width, ancestors stay dimmed at full width (click one to zoom out),
// everything else hides, and labels are refitted. The inline toolbar
// searches by regex (Ctrl+F / Cmd+F / F3 only while focus is inside this
// chart); Escape clears the search first, then the zoom. When
// options.workspace is true, the standalone title and bottom details bar are
// omitted and reset/matched controls live in the toolbar. Instance-scoped:
// no ids, every listener removed on destroy.
import { html, svg } from "./core/dom";
import { commas, pyFixed } from "./core/format";
import { FLAME_CSS } from "./core/styles";
import { bindKeys, compile, createToolbar, isTyping } from "./core/toolbar";
import type { ChartNode } from "./core/types";
import type { ViewContext, ViewInstance } from "./core/view";

// Geometry and typography: keep in step with myflames/flamegraph.py.
export const SIDE_MARGIN = 10;
export const HEADER_H = 36;
export const FOOTER_H = 30;
export const TITLE_Y = 24;
export const ROW_GAP = 1;
export const FONT_SIZE = 12;
export const CHAR_RATIO = 0.59;
export const CHAR_W = FONT_SIZE * CHAR_RATIO;
export const LABEL_INSET = 3;
export const MIN_FRAME_PX = 0.1;
export const DEFAULT_ROW_H = 32;
const DETAILS_RESERVE = 150;
const BACKGROUND = "#fafafa";
const SEP = "  ·  ";
const HINT = "Hover a frame for details. Click to zoom, double-click the background to reset, Ctrl+F to search.";
const EMPTY = "No positive time is available at this chart's resolution. Inspect the operator metrics or use Visual Explain.";

// Colors: identical tables live in myflames/flamegraph.py
// (test/test_flamegraph.py compares the source text, test/charts.mjs the
// computed colors). Families with a Visual Explain counterpart reuse
// output_workbench.COLORS; "other" uses the palette.
export const FAMILY_COLORS: Record<string, string> = {
  table_scan: "#db9292",
  index_scan: "#e4b181",
  index_lookup: "#9ac59d",
  range: "#e3ca83",
  nested_loop_join: "#c998b3",
  hash_join: "#9ecbcb",
  temp_table: "#b4a4d8",
  filter: "#d1cfad",
  aggregate: "#a8bfdc",
  sort: "#c7b49b",
  limit: "#c5cfa0",
};

export const SEPARATOR_COLORS: Record<string, string> = { "--": "#9aa0a6", "-": "#c4c8cc" };

export const MATCH_COLOR = "#ff38c8";

/** Palette name -> inclusive [red, green, blue] channel ranges. */
export const PALETTES: Record<string, Array<[number, number]>> = {
  hot: [[210, 250], [80, 200], [40, 80]],
  mem: [[90, 150], [175, 225], [90, 140]],
  io: [[90, 150], [140, 195], [205, 250]],
  red: [[215, 250], [90, 150], [90, 150]],
  green: [[100, 160], [195, 240], [100, 160]],
  blue: [[110, 165], [150, 200], [220, 255]],
};

/**
 * Stable 32-bit hash (flamegraph.label_hash): FNV-1a over each code point
 * as three little-endian bytes, then a final avalanche.
 */
export function labelHash(text: string): number {
  let h = 0x811c9dc5;
  for (const ch of text) {
    const cp = ch.codePointAt(0) || 0;
    for (let shift = 0; shift < 24; shift += 8) h = Math.imul(h ^ ((cp >>> shift) & 0xff), 0x01000193) >>> 0;
  }
  h = (h ^ (h >>> 16)) >>> 0;
  h = Math.imul(h, 0x85ebca6b) >>> 0;
  h = (h ^ (h >>> 13)) >>> 0;
  h = Math.imul(h, 0xc2b2ae35) >>> 0;
  return (h ^ (h >>> 16)) >>> 0;
}

const hex = (value: number) => value.toString(16).padStart(2, "0");

/** Fill for a frame: separator grey, family color, or palette hash. */
export function frameColor(label: string, family?: string | null, palette = "hot"): string {
  if (Object.prototype.hasOwnProperty.call(SEPARATOR_COLORS, label)) return SEPARATOR_COLORS[label];
  if (family && Object.prototype.hasOwnProperty.call(FAMILY_COLORS, family)) return FAMILY_COLORS[family];
  const name = typeof palette === "string" ? palette.toLowerCase() : "";
  const ranges = Object.prototype.hasOwnProperty.call(PALETTES, name) ? PALETTES[name] : PALETTES.hot;
  const h = labelHash(label);
  return "#" + ranges.map(([low, high], i) => hex(low + Math.floor((((h >>> (8 * i)) & 0xff) * (high - low)) / 255))).join("");
}

/** Label that fits *px* at 12 px (flamegraph.fit_frame_label). */
export function fitFrameLabel(label: string, px: number): string {
  const room = px > 0 ? Math.floor((px - 2 * LABEL_INSET) / CHAR_W) : 0;
  if (room < 3) return "";
  const chars = Array.from(label);
  return chars.length <= room ? label : chars.slice(0, room - 2).join("") + "..";
}

function fitLine(text: string, px: number, charWidth = CHAR_W): string {
  const room = px > 0 ? Math.floor(px / charWidth) : 0;
  const chars = Array.from(text);
  if (chars.length <= room) return text;
  return room >= 3 ? chars.slice(0, room - 2).join("") + ".." : "";
}

const finiteMs = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : 0);

/** Count name for a plan without measured time (flamegraph.NO_TIME). */
export const NO_TIME = "no measured time";

/**
 * Count unit for a plan (flamegraph.flame_scale): µs whenever the plan has
 * measured time (a positive total or any positive self time), so widths keep
 * their proportions; NO_TIME for a plan with no measured time at all (this
 * view then shows its empty state).
 */
export function flameScale(selfTimes: Array<number | null | undefined>,
                           totalMs: number | null | undefined): { unit: string; multiplier: number } {
  const measured = finiteMs(totalMs) > 0 || selfTimes.some((value) => finiteMs(value) > 0);
  return measured ? { unit: "µs", multiplier: 1000 } : { unit: NO_TIME, multiplier: 1 };
}

/** Whole units for one self time (flamegraph.flame_units): 0 when none, else at least 1. */
export function flameUnits(selfMs: number | null | undefined, multiplier: number): number {
  const ms = finiteMs(selfMs);
  return ms > 0 ? Math.max(1, Math.floor(ms * multiplier + 0.5)) : 0;
}

/** Time count units as microseconds per count (flamegraph.TIME_UNITS). */
const TIME_UNITS: Record<string, number> = { ms: 1000, "µs": 1 };

const halfUp = (value: number) => Math.floor(value + 0.5);

/**
 * Visible text for a frame value (flamegraph.flame_value_text). The count
 * unit sets widths only: a time reads "0 ms", whole µs below 1 ms (at least
 * "1 µs"), then 2, 1 or 0 decimals of ms below 10, below 100 and from 100 ms
 * (thousands separators), rounding half up on the µs count; a value that
 * rounds up to a band's limit moves to the next band. Other units keep the
 * integer with thousands separators.
 */
export function flameValueText(value: number, unit: string): string {
  const per = Object.prototype.hasOwnProperty.call(TIME_UNITS, unit) ? TIME_UNITS[unit] : 0;
  if (!per) return commas(halfUp(value)) + " " + unit;
  const micros = value * per;
  if (!(micros > 0)) return "0 ms";
  const whole = halfUp(micros);
  if (whole < 1000) return Math.max(1, whole) + " µs";
  const hundredths = halfUp(micros / 10);
  if (hundredths < 1000) return Math.floor(hundredths / 100) + "." + String(hundredths % 100).padStart(2, "0") + " ms";
  const tenths = halfUp(micros / 100);
  if (tenths < 1000) return Math.floor(tenths / 10) + "." + (tenths % 10) + " ms";
  return commas(halfUp(micros / 1000)) + " ms";
}

/**
 * Frame header (flamegraph.flame_header): "<label> (<value>, <percent>%)",
 * or "<label> (no measured time)" with no time or share for NO_TIME.
 */
export function flameHeader(label: string, value: number, total: number, unit: string): string {
  if (unit === NO_TIME) return label + " (" + NO_TIME + ")";
  return label + " (" + flameValueText(value, unit) + ", " + pyFixed(100 * value / total, 2) + "%)";
}

export interface Frame {
  node: ChartNode;
  depth: number;
  /** Units before this frame on the full-width axis. */
  start: number;
  /** Own units plus every unit beneath it. */
  value: number;
}

/**
 * Frames in pre-order for *root* and its visible inputs: each operator's
 * self time in whole display units (flameUnits), inputs in reported order
 * from the parent's left edge, frames with no time at all or narrower than
 * MIN_FRAME_PX of *span* dropped. Nodes are read, never modified.
 */
export function layoutFrames(root: ChartNode, inputs: (node: ChartNode) => ChartNode[],
                             multiplier: number, span: number): { total: number; frames: Frame[] } {
  const values = new Map<ChartNode, number>();
  const measure = (node: ChartNode): number => {
    const own = flameUnits(node.self_ms, multiplier);
    const value = inputs(node).reduce((sum, child) => sum + measure(child), own);
    values.set(node, value);
    return value;
  };
  const total = measure(root);
  const frames: Frame[] = [];
  if (!(total > 0)) return { total, frames };
  const walk = (node: ChartNode, depth: number, start: number) => {
    const value = values.get(node) || 0;
    if (!(value > 0) || value / total * span < MIN_FRAME_PX) return;
    frames.push({ node, depth, start, value });
    let cursor = start;
    for (const child of inputs(node)) {
      walk(child, depth + 1, cursor);
      cursor += values.get(child) || 0;
    }
  };
  walk(root, 0, 0);
  return { total, frames };
}

interface Item extends Frame {
  g: SVGGElement;
  rect: SVGRectElement;
  text: SVGTextElement;
  label: string;
  fill: string;
  info: string;
  shown: string;
}

const px = (value: number) => pyFixed(value, 2);

export function renderFlamegraph(context: ViewContext): ViewInstance {
  const { doc, data, projection, selection, container, options } = context;
  const workspace = options.workspace === true;
  const width = Math.max(1, Math.trunc(context.width || 1200));
  const span = Math.max(1, width - 2 * SIDE_MARGIN);
  const requested = Math.trunc(Number(options.frame_height));
  const rowH = Number.isFinite(requested) && requested >= 1 ? requested : DEFAULT_ROW_H;
  const inverted = options.inverted === true;
  const palette = typeof options.colors === "string" ? options.colors : "hot";
  // Same choice as Python over the drawn (projected) operators.
  const { unit, multiplier } = flameScale(projection.visible.map((node) => node.self_ms), projection.root.total_ms);
  const { total, frames } = layoutFrames(projection.root, projection.children, multiplier, span);

  if (!frames.length) {
    const empty = html(doc, "p", { class: "mfc-empty", role: "status" }, container, EMPTY);
    return { destroy() { empty.remove(); } };
  }

  const rows = frames.reduce((most, frame) => Math.max(most, frame.depth), 0) + 1;
  const pitch = rowH + ROW_GAP;
  const headerH = workspace ? SIDE_MARGIN : HEADER_H;
  const footerH = workspace ? SIDE_MARGIN : FOOTER_H;
  const height = headerH + rows * pitch + footerH;

  const toolbar = createToolbar(doc, container, "Search frames (regex)", true);
  if (toolbar.ignoreCase) toolbar.ignoreCase.checked = true;
  const root = svg(doc, "svg", {
    width, height, viewBox: "0 0 " + width + " " + height, class: "mfc-flame",
    role: "group", "aria-label": "Flame graph of time per operator",
  }, container);
  svg(doc, "title", undefined, root, (data.title ? `${data.title}: ` : "") + "Flame graph");
  svg(doc, "style", undefined, root, FLAME_CSS);
  svg(doc, "rect", { x: 0, y: 0, width: "100%", height: "100%", class: "mfc-background", fill: BACKGROUND }, root);
  if (!workspace) {
    svg(doc, "text", { class: "fg-title", x: px(width / 2), y: TITLE_Y, "text-anchor": "middle" }, root,
      fitLine(data.title || "", Math.max(0, width - 240), 17 * CHAR_RATIO));
  }
  const resetControl: Element = workspace
    ? html(doc, "button", { type: "button", class: "fg-reset fg-off" }, toolbar.element, "Reset zoom")
    : svg(doc, "text", { class: "fg-reset fg-off", x: SIDE_MARGIN, y: TITLE_Y, role: "button", tabindex: 0 }, root, "Reset zoom");
  const holder = svg(doc, "g", { class: "frames", "data-icicle": inverted ? 1 : 0 }, root);

  const items: Item[] = frames.map((frame) => {
    const { node, depth, start, value } = frame;
    const label = node.folded_label || node.short_label || "";
    const x = SIDE_MARGIN + start / total * span;
    const w = value / total * span;
    const y = headerH + (inverted ? depth : rows - 1 - depth) * pitch;
    const header = flameHeader(label, value, total, unit);
    const details = node.flame_details || "";
    const fill = frameColor(label, node.family, palette);
    const shown = fitFrameLabel(label, w);
    const g = svg(doc, "g", {
      role: "button", tabindex: 0, "aria-label": header,
      "data-node-id": node.id, "data-teach-index": node.teach_index,
    }, holder);
    svg(doc, "title", undefined, g, details ? header + "\n\n" + details : header);
    const rect = svg(doc, "rect", { x: px(x), y, width: px(w), height: rowH, rx: 2, ry: 2, fill }, g);
    const text = svg(doc, "text", { x: px(x + LABEL_INSET), y: px(y + rowH / 2 + 4) }, g, shown);
    const info = [header].concat(details.split("\n").filter((line) => /\S/.test(line))).join(SEP);
    return { ...frame, g, rect, text, label, fill, info, shown };
  });

  const detailsLine = workspace
    ? null
    : svg(doc, "text", { class: "fg-details", x: SIDE_MARGIN, y: height - 10 }, root, fitLine(HINT, span - DETAILS_RESERVE));
  const matched: Element = workspace
    ? html(doc, "span", { class: "fg-matched" }, toolbar.element, "")
    : svg(doc, "text", { class: "fg-matched", x: width - SIDE_MARGIN, y: height - 10, "text-anchor": "end" }, root, "");

  // ---- interaction (all state is local to this instance) -----------------
  const cleanups: Array<() => void> = [];
  const listen = <K extends keyof SVGElementEventMap>(target: Element, type: K, fn: (event: SVGElementEventMap[K]) => void) => {
    target.addEventListener(type, fn as EventListener);
    cleanups.push(() => target.removeEventListener(type, fn as EventListener));
  };
  let zoomed: Item | null = null;
  let pinned: Item | null = null;
  const describe = (text: string) => { if (detailsLine) detailsLine.textContent = fitLine(text, span - DETAILS_RESERVE); };
  const idle = () => describe(pinned ? pinned.info : HINT);

  const contains = (outer: Frame, inner: Frame) =>
    outer.start <= inner.start && inner.start + inner.value <= outer.start + outer.value;
  const put = (item: Item, x: number, w: number, label: string) => {
    item.rect.setAttribute("x", px(x));
    item.rect.setAttribute("width", px(w));
    item.text.setAttribute("x", px(x + LABEL_INSET));
    item.text.textContent = label;
  };
  const reset = () => {
    for (const item of items) {
      put(item, SIDE_MARGIN + item.start / total * span, item.value / total * span, item.shown);
      item.g.classList.remove("fg-hidden", "fg-ancestor");
    }
    zoomed = null;
    resetControl.classList.add("fg-off");
  };
  const zoom = (target: Item) => {
    if (target.depth === 0) { reset(); return; }
    for (const item of items) {
      if (item === target || (item.depth > target.depth && contains(target, item))) {
        const w = item.value / target.value * span;
        put(item, SIDE_MARGIN + (item.start - target.start) / target.value * span, w, fitFrameLabel(item.label, w));
        item.g.classList.remove("fg-hidden", "fg-ancestor");
      } else if (item.depth < target.depth && contains(item, target)) {
        put(item, SIDE_MARGIN, span, fitFrameLabel(item.label, span));
        item.g.classList.remove("fg-hidden");
        item.g.classList.add("fg-ancestor");
      } else {
        item.g.classList.add("fg-hidden");
      }
    }
    zoomed = target;
    resetControl.classList.remove("fg-off");
  };

  const byId = new Map<string, Item>();
  for (const item of items) {
    byId.set(item.node.id, item);
    selection.register(item.node.id, item.g);
    const choose = () => {
      selection.select(item.node.id, true);
      zoom(item);
    };
    listen(item.g, "click", (event: MouseEvent) => { event.stopPropagation(); choose(); });
    listen(item.g, "keydown", (event: KeyboardEvent) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
    });
    listen(item.g, "pointerenter", () => describe(item.info));
    listen(item.g, "pointerleave", idle);
    listen(item.g, "focus", () => describe(item.info));
    listen(item.g, "blur", idle);
  }
  const resetFromControl = (event: Event) => { event.stopPropagation(); reset(); };
  listen(resetControl, "click", resetFromControl);
  listen(resetControl, "keydown", (event: KeyboardEvent) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); resetFromControl(event); }
  });
  // Clicking the background releases the selection; double-clicking it
  // (outside every frame) resets the zoom.
  listen(root, "click", () => selection.clear());
  listen(root, "dblclick", (event: MouseEvent) => {
    if (!holder.contains(event.target as Node)) reset();
  });
  selection.listen((id) => {
    pinned = (id && byId.get(id)) || null;
    // An operator selected elsewhere must be visible here.
    if (pinned && pinned.g.classList.contains("fg-hidden")) reset();
    idle();
  });

  const coverage = (spans: Array<[number, number]>) => {
    spans.sort((a, b) => a[0] - b[0]);
    let covered = 0;
    let end = -Infinity;
    for (const [from, to] of spans) {
      if (to <= end) continue;
      covered += to - Math.max(from, end);
      end = to;
    }
    return covered;
  };
  const search = () => {
    const term = toolbar.input.value;
    const ignore = !!toolbar.ignoreCase && toolbar.ignoreCase.checked;
    const pattern = term ? compile(term, ignore ? "i" : "") : null;
    toolbar.input.setAttribute("aria-invalid", term && !pattern ? "true" : "false");
    const spans: Array<[number, number]> = [];
    for (const item of items) {
      const hit = !!pattern && pattern.test(item.label);
      item.rect.setAttribute("fill", hit ? MATCH_COLOR : item.fill);
      if (hit) spans.push([item.start, item.start + item.value]);
    }
    toolbar.status.textContent = !term ? "" : !pattern ? "Invalid regex"
      : spans.length + (spans.length === 1 ? " frame" : " frames");
    matched.textContent = pattern ? "Matched: " + pyFixed(Math.min(100, coverage(spans) / total * 100), 1) + "%" : "";
  };
  listen(toolbar.input, "input", search);
  if (toolbar.ignoreCase) listen(toolbar.ignoreCase, "change", search);

  const escape = () => {
    if (toolbar.input.value) { toolbar.input.value = ""; search(); return true; }
    if (zoomed) { reset(); return true; }
    if (pinned) { selection.clear(); return true; }
    return false;
  };
  cleanups.push(bindKeys(container, () => toolbar.focus(), (event) => {
    const key = event.key;
    if (event.metaKey && !event.ctrlKey && !event.altKey && (key === "f" || key === "F")) { toolbar.focus(); return true; }
    if (key === "Escape") return escape();
    if (key === "/" && !isTyping(event.target) && !event.ctrlKey && !event.metaKey && !event.altKey) {
      toolbar.focus();
      return true;
    }
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
