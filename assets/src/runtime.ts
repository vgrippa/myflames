/**
 * myflames Tier-1 lesson runtime.
 *
 * Additive layer over the existing window.anim (hand-rolled, still
 * shipped inline via _html.py). This bundle installs:
 *
 *   window.anim.flip(el, {x, y, scale, rotate, opacity}, opts)
 *     Motion One-backed FLIP animation. Replaces the ~40 lines of
 *     hand-rolled tween + setAttribute("transform", …) that lessons
 *     like nested_loop use for probe-pill flight. Supports springs,
 *     so the arrival motion has real physical weight.
 *
 *   window.anim.spring(el, props, {stiffness, damping, mass})
 *     Physics-damped transition. Replaces the mechanical-feeling
 *     cubic eases with a real damper for "arrival" moments.
 *
 *   window.anim.squarify(tree, width, height)
 *     Bruls/Huijsen/van Wijk squarified treemap via d3-hierarchy.
 *     Replaces the ~150-line hand-rolled _layout_squarified we wrote
 *     in output_treemap.py when the same could be 4 lines of config.
 *
 *   window.anim.smoothPath([{x,y}, {x,y}, …])
 *     Catmull-Rom curve generator via d3-shape. For paths that pass
 *     through multiple anchor points (probe pills dodging obstacles,
 *     tree-edge routing, etc.) — more flexible than our manual
 *     quadratic-Bezier anim.path(x1,y1, cx,cy, x2,y2).
 *
 * The existing anim.tween / timeline / pulse / arrival / svgEl /
 * setSpeed / complexityChart API is untouched. Lessons that want the
 * new capabilities opt in by calling window.anim.flip() etc.
 *
 * Honours prefers-reduced-motion via the same gate the inline engine
 * uses (navigator media query). Under reduced-motion every helper
 * collapses to a synchronous end-state apply and returns a
 * pre-resolved promise so callers don't hang.
 */
// Motion One's animate() is the Web Animations API wrapper. v11 types
// are in flux — we accept `any` for options to stay compatible across
// the 11.x line and let Motion's own input validation handle bad input.
import { animate, spring as motionSpring } from "motion";
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnimateOpts = any;
import { hierarchy, treemap, treemapSquarify, type HierarchyRectangularNode } from "d3-hierarchy";
import { line as d3line, curveCatmullRom } from "d3-shape";

// Module-local reduced-motion probe. The media-query API is sync and
// covers every major browser. We sample once at load and listen for
// changes so lessons that run for minutes pick up user toggles.
let _reducedMotion = false;
const _mm =
  typeof window !== "undefined" && window.matchMedia
    ? window.matchMedia("(prefers-reduced-motion: reduce)")
    : null;
if (_mm) {
  _reducedMotion = _mm.matches;
  _mm.addEventListener?.("change", (e) => {
    _reducedMotion = e.matches;
  });
}

// ---- flip ---------------------------------------------------------------
// Uses Motion One's animate() with transform interpolation. The el
// argument can be any SVGElement or HTMLElement; Motion handles both.
// Returns the Motion animation instance so callers can await .finished.

type FlipProps = {
  x?: number;
  y?: number;
  scale?: number;
  rotate?: number;
  opacity?: number;
};

type FlipOpts = {
  duration?: number;       // in seconds (Motion One convention)
  easing?: unknown;        // bezier tuple, named curve, or MotionValue
  spring?: {               // if present, overrides easing
    stiffness?: number;
    damping?: number;
    mass?: number;
  };
};

function flip(el: Element, props: FlipProps, opts: FlipOpts = {}) {
  if (_reducedMotion) {
    // Apply final state instantly. SVG uses transform via CSS;
    // fallback for older SVG semantics is the transform attribute.
    const t: string[] = [];
    if (props.x !== undefined || props.y !== undefined) {
      t.push(`translate(${props.x ?? 0}px, ${props.y ?? 0}px)`);
    }
    if (props.scale !== undefined) t.push(`scale(${props.scale})`);
    if (props.rotate !== undefined) t.push(`rotate(${props.rotate}deg)`);
    if (t.length) (el as HTMLElement).style.transform = t.join(" ");
    if (props.opacity !== undefined) {
      (el as HTMLElement).style.opacity = String(props.opacity);
    }
    return { finished: Promise.resolve() };
  }
  const target: Record<string, number | string> = {};
  if (props.x !== undefined) target.x = props.x;
  if (props.y !== undefined) target.y = props.y;
  if (props.scale !== undefined) target.scale = props.scale;
  if (props.rotate !== undefined) target.rotate = props.rotate;
  if (props.opacity !== undefined) target.opacity = props.opacity;
  const animOpts: AnimateOpts = opts.spring
    ? { easing: motionSpring(opts.spring as any) }
    : {
        duration: opts.duration ?? 0.35,
        easing: opts.easing ?? [0.2, 0.7, 0.2, 1],
      };
  return animate(el as any, target, animOpts);
}

// ---- spring -------------------------------------------------------------
// Convenience wrapper around flip() that always uses a spring.
function spring(
  el: Element,
  props: FlipProps,
  s: { stiffness?: number; damping?: number; mass?: number } = {}
) {
  return flip(el, props, {
    spring: {
      stiffness: s.stiffness ?? 180,
      damping: s.damping ?? 16,
      mass: s.mass ?? 1,
    },
  });
}

// ---- squarify -----------------------------------------------------------
// d3-hierarchy's squarified treemap layout. Takes a plain tree node
// ({value, children}) and returns a flat list of positioned rectangles
// matching the same shape the existing Python code produced. Callers
// render the rectangles themselves — we do layout only, not drawing.

type TreeNodeIn = { value?: number; children?: TreeNodeIn[]; [k: string]: unknown };
type LaidRect = {
  x: number;
  y: number;
  w: number;
  h: number;
  depth: number;
  node: TreeNodeIn;
};

function squarify(
  tree: TreeNodeIn,
  width: number,
  height: number,
  opts: { paddingInner?: number; paddingOuter?: number; ratio?: number } = {}
): LaidRect[] {
  const root = hierarchy<TreeNodeIn>(tree, (d) => d.children as TreeNodeIn[] | undefined)
    .sum((d) => (typeof d.value === "number" ? d.value : 0))
    .sort((a, b) => (b.value ?? 0) - (a.value ?? 0));
  const layout = treemap<TreeNodeIn>()
    .tile(treemapSquarify.ratio(opts.ratio ?? 1.618))
    .size([width, height])
    .paddingInner(opts.paddingInner ?? 1)
    .paddingOuter(opts.paddingOuter ?? 0)
    .round(true);
  const laid = layout(root) as HierarchyRectangularNode<TreeNodeIn>;
  const out: LaidRect[] = [];
  laid.each((n) => {
    out.push({
      x: n.x0,
      y: n.y0,
      w: n.x1 - n.x0,
      h: n.y1 - n.y0,
      depth: n.depth,
      node: n.data,
    });
  });
  return out;
}

// ---- smoothPath ---------------------------------------------------------
// d3-shape's Catmull-Rom line generator. Returns an SVG path string
// the caller can assign to <path d="...">.

function smoothPath(points: Array<{ x: number; y: number }>, tension = 0.5): string {
  const gen = d3line<{ x: number; y: number }>()
    .x((p) => p.x)
    .y((p) => p.y)
    .curve(curveCatmullRom.alpha(tension));
  return gen(points) ?? "";
}

// ---- expose -------------------------------------------------------------

export const anim = {
  // Tier 1 additions. Coexist with window.anim's existing surface;
  // see _html.py::ANIM_JS for the hand-rolled primitives (tween,
  // timeline, pulse, arrival, svgEl, reducedMotion, setSpeed, …).
  flip,
  spring,
  squarify,
  smoothPath,
  // Re-export reducedMotion() read as a function so lessons that
  // need it at call time don't have to close over the module var.
  reducedMotion(): boolean {
    return _reducedMotion;
  },
};

// The footer injected by build.mjs installs `window.anim` from the
// IIFE export, merging with whatever ANIM_JS already put there.
// A merged-in reassignment preserves the existing primitives while
// adding the Tier 1 helpers.
declare global {
  interface Window {
    anim?: Record<string, unknown> & typeof anim;
    MyflamesTier1?: { anim: typeof anim };
  }
}
