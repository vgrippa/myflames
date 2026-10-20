import { useEffect, useMemo, useRef, useState } from "react";
import { number, time, type Operator, type Plan } from "./api";
import { Disclosure, Fields, InspectorSection, Segmented, StatusPill } from "./kit";
import { severityText } from "./compareText";

/** The inspector owns operator detail and the linked list, leaving the canvas
 * height independent of the operator count. The runtime owns metric values. */
export function ContextPanel({
  plan, selected, mismatch, onFocus, onCollapse, collapsed = false, onLesson,
  onMetricsTarget, search = "", onSearch, visibleOperators = plan.operators, onSelect,
}: {
  plan: Plan;
  selected?: Operator;
  mismatch: number | null;
  onFocus?: () => void;
  onCollapse?: () => void;
  collapsed?: boolean;
  onLesson?: (lesson: string) => void;
  onMetricsTarget: (element: HTMLDivElement | null) => void;
  search?: string;
  onSearch?: (value: string) => void;
  visibleOperators?: Operator[];
  onSelect: (id: string) => void;
}) {
  // A plan with findings opens on them until an operator is chosen; a
  // section the reader picks stays picked.
  const [tab, setTabState] = useState(() => !selected && plan.analysis.warnings.length ? "findings" : "measurements");
  const picked = useRef(false);
  const setTab = (value: string) => { picked.current = true; setTabState(value); };
  const selectedId = selected?.node_id;
  useEffect(() => { if (selectedId && !picked.current) setTabState("measurements"); }, [selectedId]);
  const findings = plan.analysis.warnings.filter((w) => !selected ||
    w.node_ids?.includes(selected.node_id) || w.node_labels?.includes(selected.label));
  const measured = typeof selected?.details.actual_last_row_ms === "number";
  const hook = plan.teach_hooks?.find((h) => h.match.short_label === selected?.label);
  const value = (v: unknown) => typeof v === "number" ? number(v) : "—";
  return (
    <aside className="inspector plan-inspector" aria-label="Operator inspector">
      <h2 className="visually-hidden">Operator inspector</h2>
      <header className={`inspector-title${selected ? " operator-detail" : ""}`}>
        <h3>{selected?.label || "Select an operator"}</h3>
        {selected && <StatusPill tone={findings.length ? "watch" : "info"}
          label={findings.length ? `${findings.length} ${findings.length === 1 ? "finding" : "findings"}` : measured ? "Measured" : "Estimated"} />}
      </header>
      <Segmented label="Inspector section" value={tab} onChange={setTab} options={[
        { value: "measurements", label: "Measurements" },
        { value: "findings", label: "Findings", count: findings.length },
        { value: "lessons", label: "Lessons" },
        { value: "operators", label: "Operators" },
      ]} />
      <div className="plan-inspector-body" hidden={tab !== "measurements"}>
        {selected ? <>
          {(onFocus || onCollapse) && <div className="inspector-actions">
            {onFocus && <button className="button" onClick={onFocus}>Focus branch</button>}
            {onCollapse && !!selected.children?.length && <button className="button" onClick={onCollapse}>
              {collapsed ? "Expand branch" : "Collapse branch"}
            </button>}
          </div>}
          <InspectorSection title="Measurements" info="Times include all loops. Row counts are per loop.">
            <Fields rows={[
              ["Self time", measured ? time(selected.self_time_ms) : "—"],
              ["Total time", measured ? time(selected.total_time_ms) : "—"],
              ["Actual rows / loop", value(selected.details.actual_rows)],
              ["Estimated rows / loop", value(selected.details.estimated_rows)],
              ["Loops", number(selected.loops)],
              ["Estimate mismatch", mismatch === null ? "—" : mismatch === Infinity ? "Zero vs nonzero" : `${mismatch.toFixed(1)}×`],
            ]} />
          </InspectorSection>
          {!!selected.details.complexity && <Disclosure title="What this does">
            <p>{String((selected.details.complexity as { rationale?: string }).rationale || selected.details.operation)}</p>
          </Disclosure>}
          <Disclosure title="All operator fields" className="raw-fields">
            <pre>{JSON.stringify(selected.details, null, 2)}</pre>
          </Disclosure>
        </> : <p className="inspector-empty">Choose an operator in the chart or the Operators list.</p>}
      </div>
      <div className="plan-inspector-body" hidden={tab !== "findings"}>
        <InspectorSection title={selected ? "Related findings" : "Plan findings"}>
          {findings.length ? findings.map((w, i) => <div className="inspector-finding" key={i}>
            <StatusPill tone={w.severity === "critical" ? "problem" : "watch"} label={severityText(w.severity)} />
            <p>{w.text}</p>
            {w.node_ids?.[0] && <button className="text-button" onClick={() => onSelect(w.node_ids![0])}>Inspect operator</button>}
          </div>) : <p>{selected ? "No findings are linked to this operator." : "No findings in this plan."}</p>}
        </InspectorSection>
      </div>
      <div className="plan-inspector-body" hidden={tab !== "lessons"}>
        {hook && onLesson ? <InspectorSection title="Understand the algorithm">
          <button className="button" onClick={() => onLesson(hook.lesson)}>Learn this operator</button>
        </InspectorSection> : <p>{hook ? "Open this plan in Explore to see its lesson." : selected ? "No lesson is linked to this operator." : "Select an operator to see its lessons."}</p>}
      </div>
      {/* Keep the target mounted while another section is visible. Moving the
          runtime's own rows preserves projection, selection and keyboard use. */}
      <div className="plan-inspector-body operators" hidden={tab !== "operators"}>
        {onSearch && <label className="search">
          <input type="search" aria-label="Search operators or tables" placeholder="Search operators or tables"
            value={search} onChange={(e) => onSearch(e.target.value)} />
        </label>}
        <div className="inspector-metrics" ref={onMetricsTarget} />
        {!plan.chart_data && visibleOperators.map((op) => <button key={op.node_id}
          className="operator-row" aria-pressed={selected?.node_id === op.node_id} onClick={() => onSelect(op.node_id)}>
          <span>{op.label}</span><span>{typeof op.details.actual_last_row_ms === "number" ? time(op.self_time_ms) : "—"}</span>
        </button>)}
        {!plan.chart_data && !visibleOperators.length && <p role="status">No matching operators.</p>}
      </div>
    </aside>
  );
}

/** For one search, the objects of a JSON tree that hold a match in their own
 * name or anywhere below. Built in one pass per search (not one serialization
 * per branch and keystroke). Objects are tree nodes (parsed JSON is never
 * shared), so object identity is a stable key. */
type Matches = WeakSet<object>;
function primitiveMatches(name: string, value: unknown, needle: string) {
  return `${name} ${JSON.stringify(value)}`.toLowerCase().includes(needle);
}
function collectMatches(root: unknown, rootName: string, query: string): { root: boolean; set: Matches } {
  const needle = query.toLowerCase();
  const set: Matches = new WeakSet();
  // Iterative post-order walk: deep traces must not overflow the stack.
  type Frame = { name: string; value: object; entries: [string, unknown][]; next: number; hit: boolean; parent: Frame | null };
  const visit = (name: string, value: unknown, parent: Frame | null): boolean | Frame => {
    if (value === null || typeof value !== "object") return primitiveMatches(name, value, needle);
    return {
      name,
      value,
      entries: Object.entries(value as Record<string, unknown>),
      next: 0,
      hit: name.toLowerCase().includes(needle),
      parent,
    };
  };
  const first = visit(rootName, root, null);
  if (typeof first === "boolean") return { root: first, set };
  let frame: Frame | null = first;
  let rootHit = false;
  while (frame) {
    if (frame.next < frame.entries.length) {
      const [k, v] = frame.entries[frame.next++];
      const child = visit(k, v, frame);
      if (typeof child === "boolean") {
        if (child) frame.hit = true;
      } else frame = child;
      continue;
    }
    if (frame.hit) set.add(frame.value);
    const done: Frame = frame;
    frame = frame.parent;
    if (frame) {
      if (done.hit) frame.hit = true;
    } else rootHit = done.hit;
  }
  return { root: rootHit, set };
}

function JsonBranch({
  name,
  value,
  matches,
  depth = 0,
}: {
  name: string;
  value: unknown;
  /** The current search's matches, or null without a search. */
  matches: { needle: string; set: Matches } | null;
  depth?: number;
}) {
  const object = value !== null && typeof value === "object";
  // Children are rendered only once the branch is opened (or a search opens
  // it), so a large trace costs nothing while its branches are closed.
  const [open, setOpen] = useState(false);
  if (matches) {
    if (object ? !matches.set.has(value as object) : !primitiveMatches(name, value, matches.needle)) return null;
  }
  if (!object)
    return (
      <div className="trace-value">
        <strong>{name}</strong>
        <code>{JSON.stringify(value)}</code>
      </div>
    );
  const entries = Object.entries(value as Record<string, unknown>);
  const shown = !!matches || open;
  return (
    <details
      className="trace-branch"
      open={matches ? true : open}
      onToggle={(e) => {
        if (!matches) setOpen((e.currentTarget as HTMLDetailsElement).open);
      }}
    >
      <summary>
        {name}{" "}
        <span>
          {entries.length} {Array.isArray(value) ? "items" : "fields"}
        </span>
      </summary>
      {shown &&
        (depth < 40 ? (
          entries.map(([k, v]) => (
            <JsonBranch
              key={k}
              name={k}
              value={v}
              matches={matches}
              depth={depth + 1}
            />
          ))
        ) : (
          <pre>{JSON.stringify(value, null, 2)}</pre>
        ))}
    </details>
  );
}
export function ExecutionContext({ plan }: { plan: Plan }) {
  const [tab, setTab] = useState("schema");
  const [search, setSearch] = useState("");
  const c = plan.capture;
  const query = search.trim();
  const traceData = c?.trace?.data;
  // One pass over the trace per search text, reused by every branch.
  const traceMatches = useMemo(
    () => (query && traceData !== undefined ? collectMatches(traceData, "Optimizer trace", query) : null),
    [traceData, query],
  );
  return (
    <section className="card context-panel">
      <div className="card-heading">
        <h2>Execution context</h2>
        {c && (
          <span className="hint">
            {c.engine} {c.server_version} ·{" "}
            {c.captured_at
              ? new Date(c.captured_at).toLocaleString()
              : "Imported metadata"}
          </span>
        )}
      </div>
      <div className="view-tabs">
        {[
          ["schema", "Tables & indexes"],
          ["statistics", "Measurements"],
          ["trace", "Optimizer trace"],
          ["settings", "Settings"],
        ].map(([k, label]) => (
          <button
            key={k}
            className={k === tab ? "active" : ""}
            onClick={() => setTab(k)}
          >
            {label}
          </button>
        ))}
      </div>
      {!c ? (
        <div className="empty-small">
          This plan has no captured schema or runtime context. Run it from the
          Query editor to collect both with the next execution.
        </div>
      ) : (
        <div className="context-body">
          {c.warnings.length > 0 && (
            <details className="capture-warnings">
              <summary>{c.warnings.length} collection notices</summary>
              {c.warnings.map((w, i) => (
                <p key={i}>{w}</p>
              ))}
            </details>
          )}
          {tab === "schema" && (
            <>
              {Object.entries(c.schema || {}).map(([table, data]) => (
                <details key={table} className="schema-table">
                  <summary>{table}</summary>
                  <JsonBranch
                    name="Columns, indexes and definition"
                    value={data}
                    matches={null}
                  />
                  <JsonBranch
                    name="Table statistics"
                    value={c.table_stats?.[table]}
                    matches={null}
                  />
                </details>
              ))}
              {Object.keys(c.schema || {}).length === 0 && (
                <p>No table definitions were collected for this query.</p>
              )}
            </>
          )}
          {tab === "statistics" && (
            <>
              <div className="stats-summary">
                <div>
                  <small>Measured runs</small>
                  <strong>{c.measurements.count}</strong>
                </div>
                <div>
                  <small>Median plan time</small>
                  <strong>
                    {c.measurements.median_ms === null
                      ? "Estimated plan"
                      : time(c.measurements.median_ms)}
                  </strong>
                </div>
                <div>
                  <small>Range</small>
                  <strong>
                    {c.measurements.min_ms === null
                      ? "—"
                      : `${time(c.measurements.min_ms)} – ${time(c.measurements.max_ms!)}`}
                  </strong>
                </div>
                <div>
                  <small>Standard deviation</small>
                  <strong>
                    {c.measurements.stdev_ms === null
                      ? "—"
                      : time(c.measurements.stdev_ms)}
                  </strong>
                </div>
              </div>
              <p className="hint">
                Sequential runs; cache state is not reset. Status deltas include
                the capture statement and instrumentation. Plan iterator time
                and Performance Schema statement time have different scopes.
              </p>
              {c.conditions && <p>Conditions: {c.conditions}</p>}
              {c.runs.map((r, i) => (
                <details key={i}>
                  <summary>
                    Run {i + 1} ·{" "}
                    {r.total_time_ms === null
                      ? "Estimated plan"
                      : time(r.total_time_ms)}
                  </summary>
                  <h3>Statement statistics</h3>
                  {r.statistics ? (
                    <dl className="context-values">
                      {Object.entries(r.statistics).map(([k, v]) => (
                        <div key={k}>
                          <dt>{k.replace(/_/g, " ")}</dt>
                          <dd>
                            {k === "timer_wait_ps"
                              ? `${time(v / 1e9)} (${v.toLocaleString()} ps)`
                              : v.toLocaleString()}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  ) : (
                    <p>Statement history was unavailable.</p>
                  )}
                  <h3>Session status changes</h3>
                  <dl className="context-values">
                    {Object.entries(r.session_status_delta).map(([k, v]) => (
                      <div key={k}>
                        <dt>{k}</dt>
                        <dd>{v}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              ))}
            </>
          )}
          {tab === "trace" && (
            <>
              {c.trace ? (
                <>
                  <p className="hint">
                    Optimizer decisions recorded by the server. A considered
                    alternative is not necessarily the selected plan.
                  </p>
                  {(c.trace.missing_bytes > 0 ||
                    c.trace.insufficient_privileges > 0) && (
                    <p className="notice">
                      This trace is incomplete: {c.trace.missing_bytes} missing
                      bytes; privilege restriction:{" "}
                      {c.trace.insufficient_privileges}.
                    </p>
                  )}
                  <label className="field">
                    Search trace
                    <input
                      type="search"
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      placeholder="Try chosen, cost, range, index…"
                    />
                  </label>
                  {traceMatches && !traceMatches.root ? (
                    <div className="empty-small" role="status">
                      <p>No matching trace fields.</p>
                      <button className="button" onClick={() => setSearch("")}>Clear trace search</button>
                    </div>
                  ) : <JsonBranch
                    // A new search starts from closed branches again once it is cleared.
                    key={query ? "search" : "browse"}
                    name="Optimizer trace"
                    value={c.trace.data}
                    matches={traceMatches ? { needle: query.toLowerCase(), set: traceMatches.set } : null}
                  />}
                </>
              ) : (
                <p>
                  Enable “Capture optimizer trace” before running a query to
                  inspect the optimizer’s decisions here.
                </p>
              )}
            </>
          )}
          {tab === "settings" && (
            <JsonBranch
              name="Captured server settings"
              value={c.variables}
              matches={null}
            />
          )}
        </div>
      )}
    </section>
  );
}
