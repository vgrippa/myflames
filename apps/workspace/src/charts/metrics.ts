// Linked operator metric list (formerly rendered by exploration.py). Values
// and labels come from Python's metric_value: unavailable is never zero.
// Bars compare visible operators on one linear scale.
import { html } from "./core/dom";
import type { PanelState, ViewContext, ViewInstance } from "./core/view";

export function renderMetrics(context: ViewContext, parent: HTMLElement,
                              { metric, collapsedIds }: PanelState): ViewInstance {
  const { doc, data, projection, selection } = context;
  const meta = data.metrics_meta[metric];
  const notes = data.labels.metric_notes;
  const section = html(doc, "section", { class: "metric-panel", "aria-label": "Operator metrics" }, parent);
  const heading = html(doc, "h2", undefined, section, meta.label + " ");
  html(doc, "small", undefined, heading, "· " + meta.unit);
  let note = notes.base;
  if (collapsedIds.length) note += " " + notes.collapsed;
  if (projection.visible.some((node) => node.input_kind === "select_list")) note += " " + notes.select_list;
  html(doc, "p", undefined, section, note);

  const present = projection.visible
    .map((node) => node.metrics[metric][0])
    .filter((value): value is number => typeof value === "number");
  const maximum = (present.length ? Math.max(...present) : 1) || 1;
  const baseDepth = projection.root.depth;
  const cleanups: Array<() => void> = [];
  projection.visible.forEach((node) => {
    const [value, display] = node.metrics[metric];
    const proportion = value === null ? 0 : Math.min(100, (value / maximum) * 100);
    let name = node.short_label || node.full_label || "Operator";
    const hidden = projection.hidden(node);
    if (hidden) name += " (" + hidden + " hidden)";
    if (node.input_kind === "select_list") name += " · SELECT-list subquery";
    const row = html(doc, "button", { type: "button", class: "metric-row", "data-node-id": node.id }, section);
    row.style.setProperty("--depth", String(node.depth - baseDepth));
    row.style.setProperty("--amount", proportion.toFixed(4) + "%");
    html(doc, "span", { class: "metric-name" }, row, name);
    html(doc, "span", { class: "metric-value" }, row, display);
    html(doc, "span", undefined, html(doc, "span", { class: "metric-track" }, row));
    selection.register(node.id, row);
    const click = () => selection.select(node.id, true);
    row.addEventListener("click", click);
    cleanups.push(() => row.removeEventListener("click", click));
  });
  return {
    destroy() {
      cleanups.splice(0).forEach((cleanup) => cleanup());
      section.remove();
    },
  };
}
