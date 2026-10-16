// One plan chart, drawn in the page by the chart runtime (src/charts) from
// the chart-data-1.0 payload that /api/analyze returns. Views that are not
// registered in the runtime yet keep the sandboxed /api/render iframe.
//
// The chart is not sandboxed here, so the runtime must only ever set plan
// text with textContent/setAttribute (see src/charts/core/dom.ts). Keyboard
// shortcuts stay on the chart's own elements: browser find (Ctrl+F) is
// untouched unless focus is inside a chart that binds it.
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api, safePreview, type Plan } from "./api";
import { availableViews, mount, type ChartHandle } from "./charts";
import type { MetricName } from "./charts/core/types";

type Variant = "inspect" | "compare";

export type PlanChartProps = {
  plan: Plan;
  view: string;
  /** Accessible name of the chart region (and the iframe title). */
  label: string;
  variant: Variant;
  selectedId: string;
  /** User selected an operator inside the chart or its metric list. */
  onSelect: (id: string) => void;
  metric?: string;
  focusId?: string;
  collapsed?: string[];
  /** Workspace controls share the runtime's toolbar and keyboard scope. */
  toolbar?: ReactNode;
  /** Dock the runtime's linked metric list in the operator inspector. */
  metricsTarget?: HTMLElement | null;
  visibleOperatorIds?: string[];
};

/** True when the runtime draws *view* and the plan carries chart data. */
export function isNativeView(plan: Plan | undefined, view: string) {
  return !!plan?.chart_data && availableViews().indexOf(view) !== -1;
}

export function PlanChart(props: PlanChartProps) {
  return isNativeView(props.plan, props.view) ? (
    <NativeChart {...props} />
  ) : (
    <FrameChart {...props} />
  );
}

// Every chart keeps its linked operator metric list.
const PANELS = ["metrics"];
const MIN_CHART_WIDTH: Record<string, number> = {
  workbench: 320,
  tree: 820,
  bargraph: 760,
  flamegraph: 720,
};

function NativeChart({
  plan,
  view,
  label,
  variant,
  selectedId,
  onSelect,
  metric = "self_time",
  focusId = "",
  collapsed = [],
  toolbar,
  metricsTarget,
  visibleOperatorIds,
}: PlanChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const handle = useRef<ChartHandle | null>(null);
  const [error, setError] = useState("");
  const [toolbarSlot, setToolbarSlot] = useState<HTMLElement | null>(null);
  const toolbarSlotRef = useRef<HTMLDivElement | null>(null);
  const toolbarFocus = useRef<HTMLElement | null>(null);
  const toolbarScroll = useRef(0);
  const metricPanel = useRef<HTMLElement | null>(null);
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const data = plan.chart_data!;
  // A stable dependency for the collapsed set (a fresh array each render).
  const collapsedKey = collapsed.join("\n");
  useLayoutEffect(() => {
    const element = host.current;
    if (!element) return;
    setError("");
    let chart: ChartHandle | null = null;
    let toolbarResize: ResizeObserver | null = null;
    const fold = element.closest<HTMLElement>(".plan-fold");
    const inspector = fold?.querySelector<HTMLElement>(":scope > .plan-inspector");
    const inspectorW =
      inspector && window.getComputedStyle(inspector).position === "absolute"
        ? inspector.getBoundingClientRect().width
        : 0;
    const availW = Math.floor((element.getBoundingClientRect().width || 1200) - inspectorW);
    const chartWidth = Math.max(MIN_CHART_WIDTH[view] || 720, availW);
    try {
      // Focus, collapse, metric and selection are a copy-only projection
      // of the full tree, so switching them needs no server round trip.
      chart = mount(element, data, {
        view,
        width: chartWidth,
        panels: PANELS,
        bridge: false,
        options: { workspace: true },
        projection: {
          focus_id: focusId,
          collapsed_ids: collapsedKey ? collapsedKey.split("\n") : [],
          metric: metric as MetricName,
          selected_id: selectedRef.current,
        },
        onSelect: (id) => onSelectRef.current(id),
      });
      // Move only mount chrome. Keep the actual controls and their listeners,
      // and keep them inside .mfc-chart so Ctrl+F and Escape stay instance-scoped.
      const container = element.querySelector<HTMLElement>(".mfc-chart")!;
      const chrome = document.createElement("div");
      chrome.className = "chart-toolbar";
      chrome.setAttribute("role", "group");
      chrome.setAttribute("aria-label", "Chart controls");
      // The React controls survive chart projections. Otherwise a view change
      // would unmount the segmented control and drop its keyboard focus.
      const slot = toolbarSlotRef.current || document.createElement("div");
      toolbarSlotRef.current = slot;
      slot.className = "chart-options";
      chrome.appendChild(slot);
      const controls = container.querySelector<HTMLElement>(".wb-controls, .mfc-toolbar");
      if (controls) chrome.appendChild(controls);
      container.prepend(chrome);
      // Small screens keep the chart's readable text size in a scrollable
      // viewport. Visual Explain already owns its pan/zoom coordinate system.
      if (view !== "workbench") {
        const svg = container.querySelector<SVGSVGElement>(":scope > svg");
        if (svg) {
          const viewport = document.createElement("div");
          viewport.className = "chart-scroll";
          viewport.tabIndex = 0;
          viewport.setAttribute("role", "region");
          viewport.setAttribute("aria-label", "Plan chart. Scroll to see more operators and measurements.");
          svg.before(viewport);
          viewport.appendChild(svg);
        }
      }
      // Wrapped controls still span the inspector. Its top follows the real
      // toolbar height, including font changes and a restored narrow window.
      if (fold) {
        toolbarResize = new ResizeObserver(() => {
          fold.style.setProperty("--plan-toolbar-height", `${chrome.getBoundingClientRect().height}px`);
        });
        toolbarResize.observe(chrome);
      }
      chrome.scrollLeft = toolbarScroll.current;
      setToolbarSlot(slot);
      if (toolbarFocus.current && slot.contains(toolbarFocus.current)) {
        toolbarFocus.current.focus({ preventScroll: true });
      }
      toolbarFocus.current = null;
      metricPanel.current = element.querySelector<HTMLElement>(".metric-panel");
      const note = metricPanel.current?.querySelector("p");
      if (note) {
        const disclosure = document.createElement("details");
        disclosure.className = "metric-notes";
        const summary = document.createElement("summary");
        summary.textContent = "About these measurements";
        note.before(disclosure);
        disclosure.append(summary, note);
      }
      const empty = document.createElement("p");
      empty.className = "metric-empty";
      empty.setAttribute("role", "status");
      empty.hidden = true;
      empty.textContent = "No matching operators in this view.";
      metricPanel.current?.appendChild(empty);
      metricPanel.current?.querySelectorAll<HTMLElement>(".metric-name").forEach((name) => {
        name.title = name.textContent || "";
      });
    } catch (e) {
      // A partly drawn chart must not linger beside the message.
      element.replaceChildren();
      setError(
        "Could not draw this chart: " +
          (e instanceof Error ? e.message : String(e)),
      );
    }
    handle.current = chart;
    return () => {
      const focused = document.activeElement;
      toolbarFocus.current = focused instanceof HTMLElement && toolbarSlotRef.current?.contains(focused) ? focused : null;
      toolbarScroll.current = toolbarSlotRef.current?.parentElement?.scrollLeft || 0;
      handle.current = null;
      metricPanel.current = null;
      toolbarResize?.disconnect();
      fold?.style.removeProperty("--plan-toolbar-height");
      chart?.destroy();
    };
  }, [data, view, metric, focusId, collapsedKey]);
  useLayoutEffect(() => {
    const panel = metricPanel.current;
    if (!panel) return;
    const target = metricsTarget || host.current;
    // Re-appending an already docked panel can discard keyboard focus in its
    // rows. A selection or search update only changes the rows' hidden state.
    if (target && panel.parentElement !== target) target.appendChild(panel);
    const visible = visibleOperatorIds && new Set(visibleOperatorIds);
    panel.querySelectorAll<HTMLElement>(".metric-row").forEach((row) => {
      row.hidden = !!visible && !visible.has(row.dataset.nodeId || "");
    });
    const empty = panel.querySelector<HTMLElement>(".metric-empty");
    if (empty) empty.hidden = !!panel.querySelector(".metric-row:not([hidden])");
  }, [metricsTarget, visibleOperatorIds, data, view, metric, focusId, collapsedKey]);
  useEffect(() => {
    const chart = handle.current;
    if (!chart || chart.selected() === selectedId) return;
    // Unknown, hidden, or empty IDs clear the highlight, so the chart never
    // shows a stale operator while the page selects another one.
    if (!selectedId || !chart.select(selectedId, false)) chart.clear();
  }, [selectedId]);
  return (
    <>
      {toolbarSlot && createPortal(toolbar, toolbarSlot)}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      <div
        ref={host}
        className={`mfc-mount plan-chart plan-chart-${variant}`}
        role="region"
        aria-label={label}
        data-view={view}
        hidden={!!error}
      />
    </>
  );
}

/** Fallback for views the runtime does not draw yet: the /api/render page. */
function FrameChart({
  plan,
  view,
  label,
  variant,
  selectedId,
  onSelect,
  metric = "self_time",
  focusId = "",
  collapsed = [],
  toolbar,
}: PlanChartProps) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const collapsedKey = collapsed.join("\n");
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    api<{ html: string }>(
      "render",
      {
        plan: plan.raw,
        view,
        metric,
        focus_id: focusId,
        collapsed_ids: collapsedKey ? collapsedKey.split("\n") : [],
        selected_id: selectedRef.current,
      },
      controller.signal,
    )
      .then((r) => {
        if (!controller.signal.aborted) setHtml(safePreview(r.html));
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [plan.raw, plan.id, view, metric, focusId, collapsedKey]);
  const post = () =>
    frame.current?.contentWindow?.postMessage(
      { type: "myflames-select", node_id: selectedRef.current },
      "*",
    );
  useEffect(post, [selectedId]);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      // Only this chart's own frame may select operators.
      if (
        !frame.current ||
        event.source !== frame.current.contentWindow ||
        event.data?.type !== "myflames-node" ||
        typeof event.data.node_id !== "string"
      )
        return;
      onSelectRef.current(event.data.node_id);
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, []);
  if (loading)
    return (
      <div className="preview-state" role="status">
        <span className="spinner" />
        Rendering plan…
      </div>
    );
  if (error) return <div className="error">{error}</div>;
  return (
    <>
    {toolbar && <div className="chart-toolbar">{toolbar}</div>}
    <iframe
      ref={frame}
      className={variant === "inspect" ? "chart-frame" : "comparison-frame"}
      title={label}
      sandbox={
        variant === "inspect"
          ? "allow-scripts allow-downloads allow-modals"
          : "allow-scripts"
      }
      srcDoc={html}
      onLoad={post}
    />
    </>
  );
}
