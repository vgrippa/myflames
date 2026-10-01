import { useState } from "react";
import { time, type Plan } from "./api";
function JsonBranch({
  name,
  value,
  query,
  depth = 0,
}: {
  name: string;
  value: unknown;
  query: string;
  depth?: number;
}) {
  const object = value !== null && typeof value === "object";
  if (
    query &&
    !`${name} ${JSON.stringify(value)}`
      .toLowerCase()
      .includes(query.toLowerCase())
  )
    return null;
  if (!object)
    return (
      <div className="trace-value">
        <strong>{name}</strong>
        <code>{JSON.stringify(value)}</code>
      </div>
    );
  const entries = Object.entries(value as Record<string, unknown>);
  return (
    <details className="trace-branch" open={query ? true : undefined}>
      <summary>
        {name}{" "}
        <span>
          {entries.length} {Array.isArray(value) ? "items" : "fields"}
        </span>
      </summary>
      {depth < 40 ? (
        entries.map(([k, v]) => (
          <JsonBranch
            key={k}
            name={k}
            value={v}
            query={query}
            depth={depth + 1}
          />
        ))
      ) : (
        <pre>{JSON.stringify(value, null, 2)}</pre>
      )}
    </details>
  );
}
export function ContextPanel({ plan }: { plan: Plan }) {
  const [tab, setTab] = useState("schema");
  const [search, setSearch] = useState("");
  const c = plan.capture;
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
          This saved plan has no captured schema or runtime context. Use Query
          laboratory to collect it with the next execution.
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
                    query=""
                  />
                  <JsonBranch
                    name="Table statistics"
                    value={c.table_stats?.[table]}
                    query=""
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
                  {search.trim() && !`Optimizer trace ${JSON.stringify(c.trace.data)}`.toLowerCase().includes(search.trim().toLowerCase()) ? (
                    <div className="empty-small" role="status">
                      <p>No matching trace fields.</p>
                      <button className="button" onClick={() => setSearch("")}>Clear trace search</button>
                    </div>
                  ) : <JsonBranch
                    name="Optimizer trace"
                    value={c.trace.data}
                    query={search.trim()}
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
              query=""
            />
          )}
        </div>
      )}
    </section>
  );
}
