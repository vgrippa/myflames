// Plans presentation loads on demand. The shell owns all state and actions,
// including imports, session restore and navigation, so loading this view
// cannot reset a selection or replace a handler queued before it mounted.
import { useMemo, useState, type Dispatch, type MutableRefObject, type ReactNode, type RefObject, type SetStateAction, type SyntheticEvent } from "react";
import { digestSource, number, time, type Comparison, type Operator, type Plan, type View } from "./api";
import { Icon } from "./components";
import { Checkbox, ActionButton, ExportMenu, ActionStatus, useAction, closePopovers, Disclosure, NotesButton, Popover, Segmented, Select } from "./kit";
import { changeExact, changeText, matchText, pairTitle, severityText, statusText } from "./compareText";
import { engineName, metrics, views } from "./planDisplay";
import { lazyComponent } from "./lazy";

const PlanChart = lazyComponent(() => import("./PlanChart").then((m) => m.PlanChart));
const ContextPanel = lazyComponent(() => import("./ContextPanel").then((m) => m.ContextPanel));
const ExecutionContext = lazyComponent(() => import("./ContextPanel").then((m) => m.ExecutionContext));

/** Idle loading fetches code only; nothing mounts or reads from a server. The
 * two chunks load together (the shell's PlanScreen loader fetches them the
 * same way). */
export async function preloadPlanViews() {
  await Promise.all([PlanChart.preload(), ContextPanel.preload()]);
}

type PlanScreenProps = {
  ready: boolean;
  mode: "inspect" | "compare";
  active: Plan | undefined;
  openPlan: (id: string) => void;
  plans: (Plan & { origin?: "sample" })[];
  openImport: () => void;
  setMode: (mode: "compare" | "teach") => void;
  exportJson: () => void;
  hideLiterals: boolean;
  setHideLiterals: Dispatch<SetStateAction<boolean>>;
  exporting: boolean;
  exportHtml: () => void;
  sampleAction: ReturnType<typeof useAction>;
  busy: boolean;
  openSample: () => void;
  exportAction: ReturnType<typeof useAction>;
  exportRetry: MutableRefObject<(() => void) | null>;
  planHeaderTools: RefObject<HTMLDivElement>;
  exclusivePanel: (e: SyntheticEvent<HTMLElement>) => void;
  setNotice: Dispatch<SetStateAction<string>>;
  setError: Dispatch<SetStateAction<string>>;
  misestimateNotShown: boolean;
  misestimateNote: ReactNode;
  view: View;
  metric: string;
  focusId: string;
  collapsed: string[];
  selectedId: string;
  chartSelect: (id: string) => void;
  statsIds: string[];
  setFocusId: Dispatch<SetStateAction<string>>;
  setCollapsed: Dispatch<SetStateAction<string[]>>;
  setStatsIds: Dispatch<SetStateAction<string[]>>;
  metricsTarget: HTMLDivElement | null;
  reveal: { id: string; n: number; };
  setMisestimateNotShown: Dispatch<SetStateAction<boolean>>;
  setMisestimateNote: Dispatch<SetStateAction<ReactNode>>;
  setView: Dispatch<SetStateAction<View>>;
  setMetric: Dispatch<SetStateAction<string>>;
  setLesson: Dispatch<SetStateAction<string>>;
  setReturnToPlan: Dispatch<SetStateAction<boolean>>;
  setMetricsTarget: Dispatch<SetStateAction<HTMLDivElement | null>>;
  search: string;
  setSearch: Dispatch<SetStateAction<string>>;
  selectOperator: (id: string) => void;
  setBeforeId: Dispatch<SetStateAction<string>>;
  beforeId: string;
  afterId: string;
  setAfterId: Dispatch<SetStateAction<string>>;
  comparison: Comparison | null;
  loading: boolean;
  before: Plan | undefined;
  after: Plan | undefined;
  pair: [string, string];
  compareSelect: (side: "before_node_id" | "after_node_id") => (id: string) => void;
  setPair: Dispatch<SetStateAction<[string, string]>>;
  setSelectedId: Dispatch<SetStateAction<string>>;
  setReveal: Dispatch<SetStateAction<{ id: string; n: number }>>;
};

export function PlanScreen({
  ready, mode, active, openPlan, plans, openImport, setMode, exportJson,
  hideLiterals, setHideLiterals, exporting, exportHtml, sampleAction,
  busy, openSample, exportAction, exportRetry,
  planHeaderTools, exclusivePanel, setNotice, setError, misestimateNotShown,
  misestimateNote, view, metric, focusId, collapsed, selectedId, chartSelect, statsIds,
  setFocusId, setCollapsed, setStatsIds, metricsTarget, reveal, setMisestimateNotShown, setMisestimateNote,
  setView, setMetric, setLesson, setReturnToPlan, setMetricsTarget,
  search, setSearch, selectOperator, setBeforeId,
  beforeId, afterId, setAfterId, comparison, loading,
  before, after, pair, compareSelect, setPair, setSelectedId, setReveal,
}: PlanScreenProps) {
  const activeSource = digestSource(active?.capture);
  const selected = active?.operators.find((n) => n.node_id === selectedId);
  const measured =
    typeof active?.operators[0]?.details.actual_last_row_ms === "number";
  const summary = active?.analysis.plan_summary;
  const primaryIndex = Number(
    active?.analysis.primary_action?.ref.match(/\[(\d+)\]/)?.[1] ?? -1,
  );
  const primary = active?.analysis.suggestions[primaryIndex];
  /** The Plan summary line in the plan header: the first thing to investigate, or
   * what the plan does. */
  const planSummaryText = primary
    ? primary.action
    : !measured
      ? "Estimated execution plan. Run analysis to collect actual rows and timings."
      : active?.analysis.executive_summary || "";
  const breadcrumbs: Operator[] = [];
  let ancestor = active?.operators.find((n) => n.node_id === focusId);
  while (ancestor) {
    breadcrumbs.unshift(ancestor);
    ancestor = active?.operators.find((n) => n.node_id === ancestor?.parent_id);
  }
  function jump(kind: string) {
    if (!active) return;
    const candidates = active.operators.filter((n) =>
      kind === "estimate_error"
        ? ratio(n) !== null
        : typeof n.details.actual_last_row_ms === "number",
    );
    const target = [...candidates].sort((a, b) =>
      kind === "estimate_error"
        ? (ratio(b) || 0) - (ratio(a) || 0)
        : b.self_time_ms - a.self_time_ms,
    )[0];
    if (target) {
      setSelectedId(target.node_id);
      setFocusId("");
      setCollapsed([]);
      setSearch("");
    }
  }
  const visibleOps = useMemo(() => {
    const ops = active?.operators || [];
    const q = search.trim().toLowerCase();
    if (!q) return ops;
    return ops.filter((n) => `${n.label} ${JSON.stringify(n.details)}`.toLowerCase().includes(q));
  }, [active?.operators, search]);
  /** Operators the parser flags for a row misestimate (chart data), worst
   * first, then in plan order. The plan header's chip steps through them. */
  const misestimated = useMemo(() => (active?.chart_data?.nodes || [])
    .flatMap((node, order) => node.misestimate ? [{ id: node.id, label: node.short_label, mark: node.misestimate, order }] : [])
    .sort((a, b) => b.mark.factor - a.mark.factor || a.order - b.order), [active?.chart_data]);
  /** Select the next (or previous) misestimated operator and bring it into
   * view; keyboard focus stays on the chip so it can be pressed again. */
  function cycleMisestimate(step: number) {
    if (!misestimated.length) return;
    const at = misestimated.findIndex((item) => item.id === selectedId);
    const index = at === -1 ? (step > 0 ? 0 : misestimated.length - 1)
      : (at + step + misestimated.length) % misestimated.length;
    const target = misestimated[index];
    setSearch("");
    selectOperator(target.id);
    setReveal((current) => ({ id: target.id, n: current.n + 1 }));
    setMisestimateNotShown(false);
    setMisestimateNote(<>Misestimate {index + 1} of {misestimated.length}: <bdi>{target.label}</bdi>, <bdi>{target.mark.label}</bdi></>);
  }

  return (
    <>
      {/* Phones only: the open plan and its actions in one row (the
          Explore | Compare switch and Import plan live in the top bar). */}
      {ready && mode === "inspect" && active && (
        <div className="plans-phone-bar">
          <div className="plans-phone-toolbar">
            <Select label="Open plan" value={active.id} onChange={openPlan}
              options={plans.map((plan): [string, string] => [plan.id, plan.name])} />
            <Popover label="Plan actions" trigger="More" buttonClassName="button">
              <div className="plan-menu">
                <span className="hint">{engineName(active.analysis.source.engine)} · {active.operators.length} operators</span>
                <button className="text-button" onClick={() => { openImport(); closePopovers(); }}>Import plan</button>
                <button className="text-button" onClick={() => { setMode("compare"); closePopovers(); }}>Compare plans</button>
                <button className="text-button" onClick={() => { exportJson(); closePopovers(); }}>Export JSON</button>
                <Checkbox checked={hideLiterals} onChange={setHideLiterals}>Hide literal values</Checkbox>
                <ActionButton busy={exporting} busyLabel="Exporting…" onClick={exportHtml}>Export HTML</ActionButton>
                <dl className="plan-menu-facts" aria-label="Plan measurements">
                  <Stat hint="Total plan iterator time" label="Execution time" value={!measured ? "Not executed" : time(summary!.total_time_ms)} />
                  <Stat hint="Estimate across leaf operators" label="Rows examined ≈" value={number(summary!.rows_examined_estimate)} />
                  <Stat hint="Rows at the root operator" label="Rows returned" value={!measured ? "—" : number(summary!.rows_sent)} />
                  <Stat hint="Warnings in this plan" label="Findings" value={String(active.analysis.warnings.length)} />
                </dl>
              </div>
            </Popover>
          </div>
        </div>
      )}
      {ready && mode === "inspect" && !active && (
        <section
          className="plans-empty"
          aria-labelledby="plans-empty-title"
        >
          <Icon name="plan" size={22} />
          <h1 id="plans-empty-title">No plan is open</h1>
          <p>
            Import a plan, drop an EXPLAIN JSON file here, or capture one
            in the Query editor.
          </p>
          <ActionButton busy={sampleAction.pending} busyLabel="Opening…" disabled={!sampleAction.pending && busy} onClick={openSample}>
            Explore sample plan
          </ActionButton>
          <ActionStatus keepLine state={sampleAction} onStop={sampleAction.cancel} stopLabel="Stop waiting" onRetry={openSample} />
        </section>
      )}
      {(mode === "inspect" || mode === "compare") && <ActionStatus className="plan-export-status" state={exportAction}
        onRetry={() => exportRetry.current?.()} onStop={exportAction.cancel} stopLabel="Stop waiting" />}
      {mode === "inspect" && active && (
        <>
          <div className="plan-screen">
          <header className="page-heading plan-heading">
            <div className="plan-title">
              <h1 title={active.name}>{active.name}</h1>
              <span className="plan-meta" title={active.capture?.captured_at
                ? `Captured ${new Date(active.capture.captured_at).toLocaleString()}` : "Imported plan"}>
                {engineName(active.analysis.source.engine)} · {active.operators.length} operators
              </span>

            </div>
            <dl className="plan-stats" aria-label="Plan measurements">
              <Stat
                label="Execution time"
                value={
                  !measured ? "Not executed" : time(summary!.total_time_ms)
                }
                hint="Total plan iterator time"
              />
              <Stat
                label="Rows examined ≈"
                value={number(summary!.rows_examined_estimate)}
                hint="Estimate across leaf operators"
              />
              <Stat
                label="Rows returned"
                value={!measured ? "—" : number(summary!.rows_sent)}
                hint="Rows at the root operator"
              />
              <Stat
                label="Findings"
                value={String(active.analysis.warnings.length)}
                hint={`${active.analysis.suggestions.length} suggestions to review`}
              />
              {/* Where the optimizer was wrong first: each press selects
                  the next operator whose row estimate the parser flags
                  (worst first) and brings it into view. */}
              {misestimated.length > 0 && (
                <div className="plan-stat-chip">
                  <dt className="visually-hidden">Misestimated operators</dt>
                  <dd>
                    <button type="button" className="misestimate-chip"
                      aria-label={`Misestimates ${misestimated.length}: select the next operator whose row estimate is off`}
                      title={`Misestimates ${misestimated.length}: operators whose actual rows are far from the optimizer's estimate. Click for the next one, Shift-click for the previous one.`}
                      onClick={(e) => cycleMisestimate(e.shiftKey ? -1 : 1)}>
                      <span className="misestimate-chip-mark" aria-hidden="true" />
                      <span className="misestimate-chip-label">Misestimates</span>{" "}
                      <strong>{misestimated.length}</strong>
                    </button>
                  </dd>
                </div>
              )}
            </dl>
            {/* The plan summary and its SQL open in place from the
                header: one click each, no band of their own. */}
            <div className="plan-header-tools" ref={planHeaderTools}>
              <details
                className={`plan-strip primary-finding${primary ? " attention" : ""}`}
              >
                <summary title={planSummaryText} onClick={exclusivePanel}>
                  <Icon name={primary ? "search" : "check"} size={14} />
                  <strong>
                    {primary ? "Investigate first" : "Plan summary"}
                  </strong>
                </summary>
                <div className="strip-body plan-header-panel">
                  <p className="plan-header-meta">{engineName(active.analysis.source.engine)} · {active.operators.length} operators{active.capture?.captured_at ? ` · captured ${new Date(active.capture.captured_at).toLocaleString()}` : ""}</p>
                  <p>{planSummaryText}</p>
                  {activeSource && <p>From Top statements: <code>{activeSource.digest}</code>{activeSource.schema ? ` in schema ${activeSource.schema}` : ""}</p>}
                  {primary?.why && (
                    <p>
                      <strong>Why this matters: </strong>
                      {primary.why}
                    </p>
                  )}
                </div>
              </details>
              {active.query && (
                <div className="plan-strip sql-row">
                  <details className="sql-card">
                    <summary title={active.capture?.mode === "explain" ? "SQL of this estimated plan" : "Captured statement"} aria-label={`SQL, ${active.capture?.mode === "explain" ? "estimated plan" : "captured statement"}`} onClick={exclusivePanel}>
                      <Icon name="disclosure" size={14} />
                      <strong>SQL</strong>
                    </summary>
                    <pre className="plan-header-panel" tabIndex={0} aria-label="SQL text">{active.query}</pre>
                  </details>
                  <button
                    className="text-button sql-copy"
                    aria-label="Copy SQL"
                    title="Copy SQL"
                    onClick={() => {
                      navigator.clipboard
                        .writeText(active.query)
                        .then(() => setNotice("Query copied."))
                        .catch(() =>
                          setError("Select the query text to copy it."),
                        );
                    }}
                  >
                    <Icon name="copy" size={13} />
                    <span className="sql-copy-label">Copy SQL</span>
                  </button>
                </div>
              )}
            </div>
            <PlanExports label="Export plan" onJson={exportJson} onHtml={exportHtml} exporting={exporting} hideLiterals={hideLiterals} onHideLiterals={setHideLiterals} />
          </header>
          <div className={misestimateNotShown ? "misestimate-status hint" : "misestimate-status visually-hidden"}
            role="status">{misestimateNote}</div>
          <div className="exploration-layout plan-fold">
            <section className="plan-canvas" aria-label="Execution plan">
              <PlanChart
                plan={active}
                view={view}
                label="Interactive execution plan"
                variant="inspect"
                metric={metric}
                focusId={focusId}
                collapsed={collapsed}
                selectedId={selectedId}
                onSelect={chartSelect}
                statsIds={statsIds}
                onSelectStats={(ids) => {
                  setFocusId("");
                  setCollapsed([]);
                  setStatsIds(ids);
                }}
                metricsTarget={metricsTarget}
                visibleOperatorIds={visibleOps.map((op) => op.node_id)}
                reveal={reveal}
                onReveal={(id, shown) => {
                  if (id !== selectedId || shown || view !== "bargraph") return;
                  setMisestimateNotShown(true);
                  setMisestimateNote("Not shown in the bar chart (no self time); open Visual Explain or the execution tree to see it");
                }}
                toolbar={<>
                  <Segmented label="Chart view" className="chart-views" value={view}
                    options={views.map(([value, label]) => ({ value, label }))} onChange={setView} />
                  <div className="chart-phone-options">
                    <Select label="Chart view" value={view} options={views} onChange={(value) => setView(value as View)} />
                    <Popover label="Chart options" trigger="Options" buttonClassName="button">
                      <div className="plan-menu">
                        <label>Metric <Select label="Phone operator metric" value={metric} options={metrics} onChange={setMetric} /></label>
                        <label>Branch <Select label="Phone focused branch" value={focusId} onChange={setFocusId}
                          options={[["", "Entire plan"], ...breadcrumbs.map((n): [string, string] => [n.node_id, n.label])]} /></label>
                        <button className="text-button" onClick={() => { jump("self_time"); closePopovers(); }}>Largest self time</button>
                        <button className="text-button" onClick={() => { jump("estimate_error"); closePopovers(); }}>Largest estimate mismatch</button>
                        {collapsed.length > 0 && <button className="text-button" onClick={() => { setCollapsed([]); closePopovers(); }}>Expand all</button>}
                      </div>
                    </Popover>
                  </div>
                  <label className="chart-desktop-option">Metric <Select label="Operator metric" value={metric} options={metrics} onChange={setMetric} /></label>
                  <span className="chart-desktop-option"><Select label="Focused branch" value={focusId} onChange={setFocusId}
                    options={[["", "Entire plan"], ...breadcrumbs.map((n): [string, string] => [n.node_id, n.label])]} /></span>
                  <Popover className="chart-desktop-option" label="Find an operator" trigger="Go to" buttonTitle="Find an operator" buttonClassName="button">
                    <div className="plan-menu">
                      <button className="text-button" onClick={() => { jump("self_time"); closePopovers(); }}>Largest self time</button>
                      <button className="text-button" onClick={() => { jump("estimate_error"); closePopovers(); }}>Largest estimate mismatch</button>
                      {collapsed.length > 0 && <button className="text-button" onClick={() => { setCollapsed([]); closePopovers(); }}>Expand all</button>}
                    </div>
                  </Popover>
                </>}
              />
            </section>
            <ContextPanel key={active.id} plan={active} selected={selected}
              mismatch={selected ? ratio(selected) : null}
              onFocus={selected ? () => setFocusId(selected.node_id) : undefined}
              onCollapse={selected ? () => setCollapsed((ids) => ids.includes(selected.node_id)
                ? ids.filter((id) => id !== selected.node_id) : [...ids, selected.node_id]) : undefined}
              collapsed={!!selected && collapsed.includes(selected.node_id)}
              onLesson={(lesson) => { setLesson(lesson); setReturnToPlan(true); setMode("teach"); }}
              onMetricsTarget={setMetricsTarget} search={search} onSearch={setSearch}
              visibleOperators={visibleOps} onSelect={selectOperator} />
          </div>
          </div>
          <div className="plan-below">
          {active.analysis.warnings.length > 0 && (
            <Disclosure title="Plan findings" count={active.analysis.warnings.length} className="findings">
              {active.analysis.warnings.map((w, i) => (
                <div className="finding" key={i}>
                  <span className={`severity ${w.severity}`}>
                    {severityText(w.severity)}
                  </span>
                  <p>{w.text}</p>
                  {w.node_ids?.[0] && (
                    <button
                      className="text-button"
                      onClick={() => {
                        selectOperator(w.node_ids![0]);
                        document
                          .querySelector(".plan-canvas")
                          ?.scrollIntoView({
                            block: "start",
                            behavior: "smooth",
                          });
                      }}
                    >
                      Inspect operator →
                    </button>
                  )}
                </div>
              ))}
            </Disclosure>
          )}
          <Disclosure title="Execution context"><ExecutionContext key={active.id} plan={active} /></Disclosure>
          </div>
        </>
      )}
      {mode === "compare" && (
        <section className="compare-screen">
          {/* One header row: the title, the two plans, and the exports;
              then flush panes divided by hairlines (no cards). */}
          <header className="page-heading compare-heading">
            <h1 title="Review structural changes and measured differences together.">
              Compare plans
            </h1>
            <div className="compare-selectors">
            <label className="toolbar-field">
              <span>Before</span>
              <select
                value={beforeId}
                onChange={(e) => setBeforeId(e.target.value)}
              >
                <option value="">Choose a plan</option>
                {plans.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <Icon name="compare" />
            <label className="toolbar-field">
              <span>After</span>
              <select
                value={afterId}
                onChange={(e) => setAfterId(e.target.value)}
              >
                <option value="">Choose a plan</option>
                {plans.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            </div>
            {comparison && !loading && (
              <PlanExports label="Export comparison" onJson={exportJson} onHtml={exportHtml} exporting={exporting} hideLiterals={hideLiterals} onHideLiterals={setHideLiterals} />
            )}
          </header>
          {!before || !after || before.id === after.id ? (
            <div className="compare-empty empty-compact">
              <Icon name="compare" size={22} />
              <h2>Choose two different plans</h2>
              <p>
                {plans.length < 2
                  ? "Import or capture a second plan to compare it with your baseline."
                  : "Pick a before and an after plan above."}
              </p>
              {plans.length < 2 && <button className="button" onClick={openImport}>
                <Icon name="import" /> Import plan…
              </button>}
            </div>
          ) : loading ? (
            <div className="preview-state">Matching operators…</div>
          ) : (
            comparison && (
              <>
                <div className="compare-bar">
                  <dl className="plan-stats compare-stats" aria-label="Comparison measurements">
                    <Stat
                      label="Before"
                      value={
                        comparison.analysis.before.timing_available === false
                          ? "Not executed"
                          : time(comparison.analysis.before.total_time_ms)
                      }
                      hint={before.name}
                    />
                    <Stat
                      label="After"
                      value={
                        comparison.analysis.after.timing_available === false
                          ? "Not executed"
                          : time(comparison.analysis.after.total_time_ms)
                      }
                      hint={after.name}
                    />
                    <Stat
                      label="Change"
                      value={changeText(comparison.analysis.summary.time_delta_pct)}
                      hint={`Total measured plan time${comparison.analysis.summary.time_delta_pct === null ? "" : `: ${changeExact(comparison.analysis.summary.time_delta_pct)}`}`}
                    />
                    <Stat
                      label="Regressions"
                      value={String(comparison.analysis.summary.regressions)}
                      hint="Confident matches only"
                    />
                  </dl>
                  <div className="compare-tools">
                    <Segmented label="Comparison chart view" value={view}
                      options={views.map(([value, label]) => ({ value, label }))} onChange={setView} />
                    {/* Phones: the four views as one select, like Explore. */}
                    <span className="compare-phone-view"><Select label="Comparison chart view on phones" value={view} options={views} onChange={(value) => setView(value as View)} /></span>
                    <label>Metric <Select label="Comparison operator metric" value={metric} options={metrics} onChange={setMetric} /></label>
                    <NotesButton label="About operator matches" notes={[
                      "Select a node or comparison row to highlight its counterpart. Uncertain matches are tentative and excluded from regression counts.",
                      "Each chart is scaled to its own plan's total time, so compare the printed times, not bar lengths across plans.",
                    ]} />
                  </div>
                </div>
                <div className="comparison-canvases">
                  <ComparisonPane plan={before} side="Before" view={view} metric={metric} selectedId={pair[0]} onSelect={compareSelect("before_node_id")} />
                  <ComparisonPane plan={after} side="After" view={view} metric={metric} selectedId={pair[1]} onSelect={compareSelect("after_node_id")} />
                </div>
                <div className="comparison-list">
                  <div className="pane-head">
                    <h2>Operator changes</h2>
                  </div>
                  {comparison.analysis.deltas.map((d, i) => (
                    <button
                      key={i}
                      className={`comparison-row ${pair[0] === d.before_node_id && pair[1] === d.after_node_id ? "selected" : ""}`}
                      aria-pressed={pair[0] === (d.before_node_id || "") && pair[1] === (d.after_node_id || "")}
                      onClick={() =>
                        setPair([
                          d.before_node_id || "",
                          d.after_node_id || "",
                        ])
                      }
                    >
                      <span>
                        <strong>{pairTitle(d.before_label, d.after_label)}</strong>
                        <small>{matchText(d.matching)}</small>
                      </span>
                      <span className="match-badge" data-status={d.matching.status}>
                        {statusText(d.matching.status)}
                      </span>
                      <span>
                        {d.self_time_ms.before === null
                          ? "—"
                          : time(d.self_time_ms.before)}{" "}
                        →{" "}
                        {d.self_time_ms.after === null
                          ? "—"
                          : time(d.self_time_ms.after)}
                      </span>
                    </button>
                  ))}
                </div>
              </>
            )
          )}
        </section>
      )}
    </>
  );
}

/** A single export home for both Plans modes (the shared Export pull-down). */
function PlanExports({ label, onJson, onHtml, exporting = false, hideLiterals, onHideLiterals }: {
  label: string; onJson: () => void; onHtml: () => void; exporting?: boolean;
  hideLiterals: boolean; onHideLiterals: (value: boolean) => void;
}) {
  return <ExportMenu label={label} className="plan-exports" busy={exporting} busyLabel="Exporting…"
    toggles={[{ key: "hide-literals", label: "Hide literal values", checked: hideLiterals, onChange: onHideLiterals }]}
    formats={[
      { key: "json", label: "JSON", onSelect: onJson },
      { key: "html", label: "HTML report", onSelect: onHtml, busy: exporting, busyLabel: "Exporting…" },
    ]} />;
}

/** A comparison keeps each chart's controls and metric rows instance-scoped. */
function ComparisonPane({ plan, side, view, metric, selectedId, onSelect }: {
  plan: Plan; side: string; view: string; metric: string; selectedId: string; onSelect: (id: string) => void;
}) {
  const [target, setTarget] = useState<HTMLDivElement | null>(null);
  // Without a selection the linked metric list waits in a hidden holder
  // (the inspector's Operators section docks it when it opens).
  const [holder, setHolder] = useState<HTMLDivElement | null>(null);
  const selected = plan.operators.find((op) => op.node_id === selectedId);
  const totalMs = plan.analysis.plan_summary.total_time_ms;
  const total = typeof totalMs === "number" ? time(totalMs) : "";
  return <div className="compare-pane">
    <div className="compare-pane-heading">
      <h3 title={plan.name}>{side}: {plan.name}</h3>
      {total && <span className="compare-pane-total" title="Total measured plan time; this chart is scaled to it">{total}</span>}
      {plan.capture && <NotesButton label={`${side} capture measurements`} notes={[
        plan.capture.measurements.median_ms === null ? "Estimated plan. No measured runs." :
          `${plan.capture.measurements.count} measured runs, median ${time(plan.capture.measurements.median_ms)}. Chart shows the last captured plan.`,
      ]} />}
    </div>
    <PlanChart plan={plan} view={view} label={`${side} execution plan`} variant="compare"
      metric={metric} selectedId={selectedId} onSelect={onSelect} metricsTarget={target || holder} />
    <div className="compare-metrics-holder" hidden ref={setHolder} />
    {/* The inspector opens for a selection only: two empty panes would push
        the operator changes below the fold. */}
    {selected && <ContextPanel key={plan.id} plan={plan} selected={selected} mismatch={ratio(selected)}
      onMetricsTarget={setTarget} onSelect={onSelect} />}
  </div>;
}

/** One measurement in the plan's stats strip: a label beside the value, the
 * longer explanation on hover. */
function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div title={hint}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function ratio(op: Operator) {
  const actual = op.details.actual_rows,
    estimate = op.details.estimated_rows;
  if (
    typeof actual !== "number" ||
    typeof estimate !== "number" ||
    actual < 0 ||
    estimate < 0
  )
    return null;
  if (actual === estimate) return 1;
  if (actual === 0 || estimate === 0) return Infinity;
  return Math.max(actual / estimate, estimate / actual);
}
