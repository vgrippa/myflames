// Row-estimate marks: the parser flags an operator whose actual rows per loop
// are far from the optimizer's estimate (parser.misestimate_record), and the
// views draw its ``label`` as a chip (Visual Explain header band, tree and
// bar chart label columns) or an underline (flame graph). Python decides
// which operators and every word; this file only draws.
//
// Keep in step with myflames/output_workbench.py (MARK_*, mark_width).
// MARK_COLOR and MARK_FILL are the presentation attributes both renderers
// write (parity-tested); the runtime themes them with --mfc-estimate and
// --mfc-card (core/styles.ts).
import { svg } from "./dom";
import { cpLength, pyFixed } from "./format";
import type { ChartNode, Misestimate } from "./types";

export const MARK_COLOR = "#5b3cc4";
export const MARK_FILL = "#ffffff";
/** 11 px semibold text, per code point (measured: at most 6.19 px). */
export const MARK_CHAR_W = 6.2;
export const MARK_PAD = 10;
/** Chip height and its gap to the next column in the tree and bar chart
 * rows (output_tree.MARK_ROW_H, MARK_GAP). */
export const MARK_ROW_H = 16;
export const MARK_GAP = 6;

/** Width of a row-estimate chip: output_workbench.mark_width. */
export const markWidth = (text: string) => cpLength(text) * MARK_CHAR_W + MARK_PAD;

/** The parser's record for an operator, or null (old payloads have none). */
export function misestimateOf(node: ChartNode): Misestimate | null {
  const mark = node.misestimate;
  return mark && typeof mark.label === "string" && mark.label ? mark : null;
}

/** Draw a chip (rect, then centered text) and return its width. */
export function drawChip(doc: Document, parent: Element, label: string, x: number, y: number,
                         height: number, rx: number, textY: number, extra: Record<string, string> = {}): number {
  const width = markWidth(label);
  svg(doc, "rect", {
    class: "mfc-mis-chip", x: pyFixed(x, 1), y, width: pyFixed(width, 1), height, rx,
    fill: MARK_FILL, stroke: MARK_COLOR, ...extra,
  }, parent);
  svg(doc, "text", { class: "mfc-mis", x: pyFixed(x + width / 2, 1), y: textY, "text-anchor": "middle", ...extra }, parent, label);
  return width;
}
