import { useMemo, useState, type ReactNode } from "react";
import { number, time, type Plan } from "./api";
import { DataTable, Disclosure, Fields, type Column } from "./kit";
import type { ChartData, PlanStatsRow } from "./charts/core/types";

const count = (value: number | null) => value === null ? "Unknown" : number(value);
const rowKey = (row: PlanStatsRow) => row.table === undefined ? row.label : JSON.stringify([row.schema || "", row.table]);
const duration = (value: number | null) => value === null ? "Not measured" : time(value);
const share = (value: number | null) => value === null ? "Unknown" : `${(value * 100).toFixed(1)}%`;

/** Aggregation belongs to Python. This component only formats and selects. */
export function PlanStats({ plan, selectedId, onSelect, onSelectStats, toolbar }: {
  plan: Plan; selectedId: string; onSelect: (id: string) => void;
  onSelectStats?: (ids: string[]) => void; toolbar?: ReactNode;
}) {
  const stats = plan.chart_data?.stats;
  const columns: Column<PlanStatsRow>[] = [
    { key: "name", label: "Name", primary: true, flex: true, minWidth: 100, sort: r => r.label, render: r => r.label },
    { key: "count", label: "Count", num: true, sort: r => r.count, render: r => number(r.count) },
    { key: "self", label: "Self time", num: true, sort: r => r.self_ms, render: r => duration(r.self_ms) },
    { key: "share", label: "Plan share", num: true, sort: r => r.share, render: r => share(r.share) },
    { key: "rows", label: "Rows produced", num: true, sort: r => r.actual_rows, render: r => count(r.actual_rows) },
    { key: "estimates", label: "Est. rows / loop", num: true, sort: r => r.estimated_rows, render: r => count(r.estimated_rows) },
    { key: "loops", label: "Loops", num: true, sort: r => r.loops, render: r => count(r.loops) },
  ];
  return <div className="plan-chart plan-statistics" data-view="stats">
    <div className="chart-toolbar"><div className="chart-options">{toolbar}</div></div>
    <div className="plan-statistics-body">
    {!stats ? <p>Stats are unavailable for this saved plan. Reimport the plan to compute them.</p> : <>
      <Disclosure title="About these measurements"><p>{stats.note}</p></Disclosure>
      {([ ["tables", "Time per table"], ["operators", "Time per operator type"] ] as const).map(([key, label]) => {
        const rows = stats[key];
        const selected = rows.find(row => row.node_ids.includes(selectedId));
        return <section key={key} aria-label={label}>
          <h3>{label}</h3>
          <DataTable label={label} density="compact" columns={key === "tables" ? [...columns,
            { key: "operators", label: "Operators", sort: r => (r.operators || []).join(", "), render: r => (r.operators || []).join(", ") }] : columns}
            rows={rows} rowKey={rowKey} defaultSort={{ key: "self", dir: "desc" }}
            selection={{ key: selected ? rowKey(selected) : "", select: key => {
              const row = rows.find(item => rowKey(item) === key);
              if (row?.node_ids.length) {
                onSelect(row.node_ids[0]);
                onSelectStats?.(row.node_ids);
              }
            } }} />
        </section>;
      })}
    </>}
    </div>
  </div>;
}

/** Ancestor paths are presentation of the existing ordered parent links. */
export function AllOccurrences({ data, group, selectedId, onSelect, onClose }: {
  data: ChartData; group: string; selectedId: string; onSelect: (id: string) => void; onClose: () => void;
}) {
  const summary = data.groups?.[group];
  const [focusedId, setFocusedId] = useState(selectedId);
  const rows = useMemo(() => {
    const nodes = new Map(data.nodes.map(node => [node.id, node]));
    return data.nodes.filter(node => node.group === group).map(node => {
      const labels: string[] = [];
      let parent = node.parent_id ? nodes.get(node.parent_id) : undefined;
      while (parent) {
        labels.unshift(parent.short_label);
        parent = parent.parent_id ? nodes.get(parent.parent_id) : undefined;
      }
      return { ...node, path: labels.join(" → ") || "Plan root", tail: labels.slice(-2).join(" → ") || "Plan root" };
    }).sort((a, b) => (b.self_ms ?? -Infinity) - (a.self_ms ?? -Infinity));
  }, [data, group]);
  if (!summary) return <p>Occurrences are unavailable for this plan.</p>;
  const total = data.nodes.find(node => node.id === data.root_id)?.total_ms;
  const tabId = rows.some(row => row.id === focusedId) ? focusedId : rows.find(row => row.id === selectedId)?.id || rows[0]?.id;
  return <section aria-label="All occurrences">
    <button className="text-button" onClick={onClose}>Back to measurements</button>
    <h3>All occurrences</h3>
    <p>{summary.label} appears {summary.count} {summary.count === 1 ? "time" : "times"}.</p>
    <Fields rows={[["Self time in total", duration(summary.self_ms)], ["Share of plan time", share(summary.share)]]} />
    <div className="occurrence-list" role="listbox" aria-label="Operator occurrences">
      {rows.map((row, index) => {
        const loops = row.measured || row.actual_rows !== null ? row.loops : null;
        const metrics = `${duration(row.self_ms)} self · ${share(row.self_ms !== null && total != null && total > 0 ? row.self_ms / total : null)} · ${count(row.actual_rows)} ${row.actual_rows === 1 ? "row" : "rows"} × ${count(loops)} ${loops === 1 ? "loop" : "loops"}`;
        return <button type="button" role="option" key={row.id} className="occurrence-row"
          data-node-id={row.id} title={row.path} aria-label={`${row.path}. ${metrics}`}
          aria-selected={selectedId === row.id} tabIndex={tabId === row.id ? 0 : -1}
          onFocus={() => setFocusedId(row.id)} onClick={() => onSelect(row.id)}
          onKeyDown={event => {
            if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "Home" ? 0 : event.key === "End" ? rows.length - 1
              : Math.max(0, Math.min(rows.length - 1, index + (event.key === "ArrowDown" ? 1 : -1)));
            const target = event.currentTarget.parentElement?.children[next] as HTMLElement | undefined;
            target?.focus();
            target?.scrollIntoView({ block: "nearest" });
          }}>
          <span className="occurrence-path">{row.tail}</span>
          <span className="occurrence-metrics">{metrics}</span>
        </button>;
      })}
    </div>
  </section>;
}
