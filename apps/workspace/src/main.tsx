import { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  api,
  download,
  number,
  safePreview,
  time,
  type Bundle,
  type Capture,
  type Comparison,
  type Operator,
  type Plan,
  type View,
} from "./api";
import { Icon, ImportDialog } from "./components";
import { Teach } from "./Teach";
import { QueryLab } from "./QueryLab";
import { Library } from "./Library";
import { ContextPanel } from "./ContextPanel";
import example from "./example.json";
import logo from "../../../docs/brand/myflames-icon-v2.png";
import "./style.css";
const views: [View, string][] = [
  ["workbench", "Visual Explain"],
  ["flamegraph", "Flame graph"],
  ["tree", "Execution tree"],
  ["bargraph", "Bar chart"],
  ["treemap", "Treemap"],
];
const metrics = [
  ["self_time", "Self time"],
  ["total_time", "Total time"],
  ["rows", "Actual rows / loop"],
  ["estimate_error", "Estimation mismatch"],
];
type Mode = "inspect" | "compare" | "teach" | "library" | "query";
const message = (e: unknown) =>
  e instanceof Error ? e.message : "Could not complete the operation.";
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
function App() {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [activeId, setActiveId] = useState("");
  const active = plans.find((p) => p.id === activeId);
  const [mode, setMode] = useState<Mode>("inspect");
  const [view, setView] = useState<View>("workbench");
  const [metric, setMetric] = useState("self_time");
  const [selectedId, setSelectedId] = useState("");
  const selected = active?.operators.find((n) => n.node_id === selectedId);
  const [focusId, setFocusId] = useState("");
  const [collapsed, setCollapsed] = useState<string[]>([]);
  const [html, setHtml] = useState("");
  const [loading, setLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [search, setSearch] = useState("");
  const [beforeId, setBeforeId] = useState("");
  const [afterId, setAfterId] = useState("");
  const before = plans.find((p) => p.id === beforeId),
    after = plans.find((p) => p.id === afterId);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [pair, setPair] = useState<[string, string]>(["", ""]);
  const [compareCharts, setCompareCharts] = useState<[string, string]>([
    "",
    "",
  ]);
  const [name, setName] = useState("Untitled investigation");
  const [notes, setNotes] = useState("");
  const [tags, setTags] = useState("");
  const [savedId, setSavedId] = useState<string | undefined>();
  const [baseline, setBaseline] = useState("");
  const [dirty, setDirty] = useState(false);
  const [lesson, setLesson] = useState("");
  const [returnToPlan, setReturnToPlan] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const beforeFrame = useRef<HTMLIFrameElement>(null),
    afterFrame = useRef<HTMLIFrameElement>(null);
  const file = useRef<HTMLInputElement>(null);
  const importLock = useRef(false);
  function selectOperator(id: string) {
    const ancestors = new Set<string>();
    let node = active?.operators.find((op) => op.node_id === id);
    while (node) {
      ancestors.add(node.node_id);
      node = active?.operators.find((op) => op.node_id === node?.parent_id);
    }
    if (focusId && !ancestors.has(focusId)) setFocusId("");
    setCollapsed((ids) => {
      const next = ids.filter((key) => key === id || !ancestors.has(key));
      return next.length === ids.length ? ids : next;
    });
    setSelectedId(id);
  }
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  useEffect(() => {
    setSelectedId("");
    setFocusId("");
    setCollapsed([]);
    setSearch("");
  }, [activeId]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setLoading(true);
    setPreviewError("");
    api<{ html: string }>(
      "render",
      {
        plan: active.raw,
        view,
        metric,
        focus_id: focusId,
        collapsed_ids: collapsed,
        selected_id: selectedRef.current,
      },
      controller.signal,
    )
      .then((r) => {
        if (!controller.signal.aborted) setHtml(safePreview(r.html));
      })
      .catch((e) => {
        if (!controller.signal.aborted) setPreviewError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [active?.raw, activeId, view, metric, focusId, collapsed]);
  useEffect(() => {
    frame.current?.contentWindow?.postMessage(
      { type: "myflames-select", node_id: selectedId },
      "*",
    );
  }, [selectedId]);
  useEffect(() => {
    if (mode !== "compare" || !before || !after || before.id === after.id) {
      setComparison(null);
      setCompareCharts(["", ""]);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setPair(["", ""]);
    Promise.all([
      api<Comparison>(
        "compare",
        { before: before.raw, after: after.raw },
        controller.signal,
      ),
      api<{ html: string }>(
        "render",
        { plan: before.raw, view: "workbench" },
        controller.signal,
      ),
      api<{ html: string }>(
        "render",
        { plan: after.raw, view: "workbench" },
        controller.signal,
      ),
    ])
      .then(([diff, b, a]) => {
        setComparison(diff);
        setCompareCharts([safePreview(b.html), safePreview(a.html)]);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [mode, before, after]);
  useEffect(() => {
    beforeFrame.current?.contentWindow?.postMessage(
      { type: "myflames-select", node_id: pair[0] },
      "*",
    );
    afterFrame.current?.contentWindow?.postMessage(
      { type: "myflames-select", node_id: pair[1] },
      "*",
    );
  }, [pair]);
  useEffect(() => {
    const receive = (event: MessageEvent) => {
      if (
        event.data?.type !== "myflames-node" ||
        typeof event.data.node_id !== "string"
      )
        return;
      const id = event.data.node_id;
      if (
        mode === "inspect" &&
        event.source === frame.current?.contentWindow &&
        active?.operators.some((n) => n.node_id === id)
      ) {
        setSelectedId(id);
        setSearch("");
      }
      if (mode === "compare" && comparison) {
        const side =
          event.source === beforeFrame.current?.contentWindow
            ? "before_node_id"
            : event.source === afterFrame.current?.contentWindow
              ? "after_node_id"
              : null;
        if (side) {
          const match = comparison.analysis.deltas.find((d) => d[side] === id);
          if (match)
            setPair([match.before_node_id || "", match.after_node_id || ""]);
        }
      }
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [mode, active, comparison]);
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  const analyze = async (raw: string, capture?: Capture) =>
    api<
      Pick<Plan, "analysis" | "operators" | "query" | "teach_hooks" | "capture">
    >("analyze", { plan: raw, capture });
  async function importPlan(raw: string, planName: string, capture?: Capture) {
    if (importLock.current) return;
    importLock.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await analyze(raw, capture);
      const plan: Plan = {
        ...result,
        raw,
        name: planName,
        id: crypto.randomUUID(),
        capture: result.capture || undefined,
      };
      setPlans((ps) => [...ps, plan]);
      setActiveId(plan.id);
      setBeforeId((id) => id || plan.id);
      setAfterId(plan.id);
      setBaseline((id) => id || plan.id);
      setMode("inspect");
      setImportOpen(false);
      setDirty(true);
    } catch (e) {
      setError(message(e));
      throw e;
    } finally {
      setBusy(false);
      importLock.current = false;
    }
  }
  const capture = useCallback(async (result: Capture, planName: string) => {
    await importPlan(result.raw, planName, result);
  }, []);
  async function openBundle(bundle: Bundle) {
    if (
      bundle.schema_version !== "investigation-1.0" ||
      !Array.isArray(bundle.plans) ||
      bundle.plans.length > 30
    )
      throw new Error("Choose a myflames investigation bundle.");
    const parsed = await Promise.all(
      bundle.plans.map(async (p) => ({
        ...p,
        ...(await analyze(p.raw, p.capture)),
      })),
    );
    // Opening another saved investigation keeps unsaved work available as a bundle.
    if (dirty && plans.length)
      download(
        JSON.stringify(makeBundle(), null, 2),
        "unsaved-investigation.json",
        "application/json",
      );
    setPlans(parsed);
    setActiveId(parsed[0]?.id || "");
    setBaseline(bundle.baseline_id);
    setBeforeId(bundle.baseline_id || parsed[0]?.id || "");
    setAfterId(parsed.find((p) => p.id !== bundle.baseline_id)?.id || "");
    setName(bundle.name);
    setNotes(bundle.notes);
    setTags(bundle.tags.join(", "));
    setSavedId(bundle.id);
    setDirty(false);
    setMode("inspect");
    setImportOpen(false);
  }
  async function importFile(f?: File) {
    if (!f) return;
    setError("");
    try {
      if (f.size > 10 * 1024 * 1024)
        throw new Error("Choose a file smaller than 10 MiB.");
      const raw = await f.text();
      let obj;
      try {
        obj = JSON.parse(raw);
      } catch {
        /* parser diagnoses JSON */
      }
      if (obj?.schema_version === "investigation-1.0") await openBundle(obj);
      else if (obj?.schema_version === "capture-1.0")
        await importPlan(obj.raw, f.name, obj);
      else await importPlan(raw, f.name);
    } catch (e) {
      setError(message(e));
    }
  }
  function makeBundle(): Bundle {
    return {
      schema_version: "investigation-1.0",
      name,
      notes,
      tags: tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean),
      baseline_id: baseline,
      plans: plans.map((p) => ({
        id: p.id,
        name: p.name,
        raw: p.raw,
        capture: p.capture,
        notes: p.notes,
      })),
    };
  }
  async function save() {
    setBusy(true);
    setError("");
    try {
      const r = await api<Bundle>("investigations/save", {
        id: savedId,
        value: makeBundle(),
      });
      setSavedId(r.id);
      setDirty(false);
      setNotice("Investigation saved on this computer.");
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }
  function removePlan(id: string) {
    const rest = plans.filter((p) => p.id !== id);
    setPlans(rest);
    if (id === activeId) setActiveId(rest[0]?.id || "");
    if (id === beforeId) setBeforeId("");
    if (id === afterId) setAfterId("");
    if (id === baseline) setBaseline(rest[0]?.id || "");
    setDirty(true);
  }
  async function exportHtml() {
    if (mode === "compare" && comparison) {
      download(comparison.html, "myflames-comparison.html", "text/html");
      return;
    }
    if (!active) return;
    setExporting(true);
    try {
      const r = await api<{ html: string }>("report", {
        plan: active.raw,
        view,
      });
      download(
        r.html,
        `${active.name.replace(/\.json$/i, "")}-report.html`,
        "text/html",
      );
    } catch (e) {
      setError(message(e));
    } finally {
      setExporting(false);
    }
  }
  function exportJson() {
    const data = mode === "compare" ? comparison?.analysis : active?.analysis;
    if (data)
      download(
        JSON.stringify(data, null, 2),
        mode === "compare"
          ? "comparison.json"
          : `${active!.name.replace(/\.json$/i, "")}-analysis.json`,
        "application/json",
      );
  }
  const measured =
    typeof active?.operators[0]?.details.actual_last_row_ms === "number";
  const summary = active?.analysis.plan_summary;
  const primaryIndex = Number(
    active?.analysis.primary_action?.ref.match(/\[(\d+)\]/)?.[1] ?? -1,
  );
  const primary = active?.analysis.suggestions[primaryIndex];
  const selectedHook = active?.teach_hooks?.find(
    (h) => h.match.short_label === selected?.label,
  );
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
  const visibleOps =
    active?.operators.filter((n) =>
      `${n.label} ${JSON.stringify(n.details)}`
        .toLowerCase()
        .includes(search.toLowerCase()),
    ) || [];
  const modeName = {
    inspect: "Plan explorer",
    compare: "Compare plans",
    teach: "Teach",
    library: "Investigations",
    query: "Query laboratory",
  }[mode];
  return (
    <div className="app">
      <a href="#main" className="skip">
        Skip to workspace
      </a>
      <aside className="sidebar" aria-label="Workspace navigation">
        <button className="brand" onClick={() => setMode("inspect")}>
          <img src={logo} alt="myflames project logo" />
          <span>
            myflames<small>Query workspace</small>
          </span>
        </button>
        <nav className="main-nav" aria-label="Views">
          {(
            [
              ["inspect", "chart", "Plan explorer"],
              ["query", "file", "Query laboratory"],
              ["compare", "compare", "Compare plans"],
              ["library", "file", "Investigations"],
              ["teach", "book", "Teach"],
            ] as [Mode, string, string][]
          ).map(([key, icon, label]) => (
            <button
              key={key}
              className={mode === key ? "active" : ""}
              aria-current={mode === key ? "page" : undefined}
              onClick={() => {
                if (key === "teach") {
                  setLesson("");
                  setReturnToPlan(false);
                }
                setMode(key);
                setError("");
                setNotice("");
              }}
            >
              <Icon name={icon} />
              {label}
            </button>
          ))}
        </nav>
        <div className="section-label">
          EXPERIMENTS <span>{plans.length}</span>
        </div>
        <button
          className="import-sidebar"
          onClick={() => {
            setError("");
            setImportOpen(true);
          }}
        >
          <Icon name="plus" /> Import a plan
        </button>
        <div className="plan-list">
          {!plans.length && (
            <p className="sidebar-empty">
              Import or capture a plan to begin an investigation.
            </p>
          )}
          {plans.map((p) => (
            <div
              className={`plan-item ${p.id === activeId ? "selected" : ""}`}
              key={p.id}
            >
              <button
                className="plan-select"
                onClick={() => {
                  setActiveId(p.id);
                  setMode("inspect");
                }}
              >
                <Icon name="file" />
                <span>
                  <strong>{p.name}</strong>
                  <small>
                    {p.capture?.mode === "explain"
                      ? "Estimated plan"
                      : time(p.analysis.plan_summary.total_time_ms)}
                    {p.id === baseline ? " · Baseline" : ""}
                  </small>
                </span>
              </button>
              <button
                className="icon-button remove"
                aria-label={`Remove ${p.name}`}
                onClick={() => removePlan(p.id)}
              >
                <Icon name="close" size={13} />
              </button>
            </div>
          ))}
        </div>
        <div className="sidebar-footer">
          <span className="local-dot" /> Local workspace
          <p>
            {dirty ? "Unsaved changes" : "Your data stays on this computer."}
          </p>
          <span className="hint">Python engine · Browser interface</span>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <Icon name="chevron" size={12} />
            <strong>{modeName}</strong>
          </div>
          <div className="topbar-actions">
            {plans.length > 0 && (
              <>
                <span className="save-state">{dirty ? "Edited" : "Saved"}</span>
                <button className="button" disabled={busy} onClick={save}>
                  Save investigation
                </button>
              </>
            )}
            <span className="local-badge">LOCAL</span>
          </div>
        </header>
        <main id="main">
          {error && !importOpen && (
            <div role="alert" className="error">
              {error}
              <button
                className="icon-button"
                aria-label="Dismiss error"
                onClick={() => setError("")}
              >
                <Icon name="close" />
              </button>
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              {notice}
              <button className="text-button" onClick={() => setNotice("")}>
                Dismiss
              </button>
            </div>
          )}
          <div hidden={mode !== "query"}>
            <QueryLab onCapture={capture} />
          </div>
          {mode === "library" && <Library onOpen={openBundle} />}
          <Teach
            active={mode === "teach"}
            initialLesson={lesson}
            onReturn={
              returnToPlan
                ? () => {
                    setMode("inspect");
                    setReturnToPlan(false);
                  }
                : undefined
            }
          />
          {mode === "inspect" && !active && (
            <section className="welcome">
              <div className="welcome-brand">
                <img src={logo} alt="myflames" />
              </div>
              <div className="eyebrow">A CLEARER VIEW OF YOUR QUERIES</div>
              <h1>
                Understand the work
                <br />
                behind every query.
              </h1>
              <p>
                Explore execution plans, investigate bottlenecks, and compare
                your next experiment. All in one local workspace.
              </p>
              <div className="button-row">
                <button
                  className="button primary"
                  onClick={() => setMode("query")}
                >
                  Open query laboratory <Icon name="chevron" size={15} />
                </button>
                <button className="button" onClick={() => setImportOpen(true)}>
                  Import plan
                </button>
              </div>
              <div className="welcome-cards">
                <button
                  className="import-card"
                  onClick={() => setImportOpen(true)}
                  onDragOver={(e) => e.preventDefault()}
                  onDrop={(e) => {
                    e.preventDefault();
                    importFile(e.dataTransfer.files[0]);
                  }}
                >
                  <Icon name="upload" size={24} />
                  <h2>Bring your own plan</h2>
                  <p>
                    Drop an EXPLAIN JSON file or paste a plan from MySQL or
                    MariaDB.
                  </p>
                  <span>Choose a file or paste JSON →</span>
                </button>
                <button
                  className="sample-card"
                  onClick={() =>
                    importPlan(
                      JSON.stringify(example),
                      "sample-plan.json",
                    ).catch(() => {})
                  }
                >
                  <Icon name="chart" size={24} />
                  <h2>Explore a sample</h2>
                  <p>
                    Follow the joins, inspect the measurements, and try all five
                    views.
                  </p>
                  <span>Explore sample plan →</span>
                </button>
                <button
                  className="sample-card"
                  onClick={() => setMode("teach")}
                >
                  <Icon name="book" size={24} />
                  <h2>Learn as you explore</h2>
                  <p>
                    Interactive lessons explain the algorithms behind the plan.
                  </p>
                  <span>Open Teach →</span>
                </button>
              </div>
            </section>
          )}
          {mode === "inspect" && active && (
            <>
              <div className="page-heading plan-heading">
                <div>
                  <div className="eyebrow">
                    {active.analysis.source.engine || "SQL"} INVESTIGATION
                  </div>
                  <h1>{active.name}</h1>
                  <p>
                    {active.operators.length} operators
                    {active.capture?.captured_at
                      ? ` · Captured ${new Date(active.capture.captured_at).toLocaleString()}`
                      : " · Imported plan"}
                  </p>
                </div>
                <div className="heading-actions">
                  <button className="button" onClick={exportJson}>
                    Export JSON
                  </button>
                  <button
                    className="button"
                    disabled={exporting}
                    onClick={exportHtml}
                  >
                    Export HTML <Icon name="upload" size={14} />
                  </button>
                  <button
                    className="button primary"
                    onClick={() => setImportOpen(true)}
                  >
                    <Icon name="plus" />
                    Import plan
                  </button>
                </div>
              </div>
              <div className="metrics">
                <Metric
                  label="Execution time"
                  value={
                    !measured ? "Not executed" : time(summary!.total_time_ms)
                  }
                  hint="Total plan iterator time"
                />
                <Metric
                  label="Rows examined ≈"
                  value={number(summary!.rows_examined_estimate)}
                  hint="Estimate across leaf operators"
                />
                <Metric
                  label="Rows returned"
                  value={!measured ? "—" : number(summary!.rows_sent)}
                  hint="Rows at the root operator"
                />
                <Metric
                  label="Findings"
                  value={String(active.analysis.warnings.length)}
                  hint={`${active.analysis.suggestions.length} suggestions to review`}
                />
              </div>
              <div className={`primary-finding ${primary ? "attention" : ""}`}>
                <Icon name={primary ? "search" : "check"} />
                <div>
                  <strong>
                    {primary ? "Investigate first" : "Plan summary"}
                  </strong>
                  <p>
                    {primary
                      ? primary.action
                      : !measured
                        ? "Estimated execution plan. Run analysis to collect actual rows and timings."
                        : active.analysis.executive_summary}
                  </p>
                  {primary?.why && (
                    <details>
                      <summary>Why this matters</summary>
                      <p>{primary.why}</p>
                    </details>
                  )}
                </div>
              </div>
              {active.query && (
                <details className="sql-card card" open>
                  <summary>
                    SQL query{" "}
                    <span>
                      {active.capture?.mode === "explain"
                        ? "Estimated plan"
                        : "Captured statement"}
                    </span>
                  </summary>
                  <pre>{active.query}</pre>
                  <button
                    className="text-button"
                    onClick={() => {
                      navigator.clipboard
                        .writeText(active.query)
                        .then(() => setNotice("Query copied."))
                        .catch(() =>
                          setError("Select the query text to copy it."),
                        );
                    }}
                  >
                    Copy SQL
                  </button>
                </details>
              )}
              <div className="exploration-layout">
                <div className="canvas-column">
                  <section className="card plan-canvas">
                    <div className="card-heading">
                      <h2>Execution plan</h2>
                      <div className="button-row">
                        <button
                          className="text-button"
                          onClick={() => jump("self_time")}
                        >
                          Largest self time
                        </button>
                        <button
                          className="text-button"
                          onClick={() => jump("estimate_error")}
                        >
                          Largest estimate mismatch
                        </button>
                      </div>
                    </div>
                    <div className="view-tabs">
                      {views.map(([id, label]) => (
                        <button
                          key={id}
                          aria-pressed={view === id}
                          className={view === id ? "active" : ""}
                          onClick={() => setView(id)}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <div className="chart-toolbar">
                      <label>
                        Metric{" "}
                        <select
                          aria-label="Operator metric"
                          value={metric}
                          onChange={(e) => setMetric(e.target.value)}
                        >
                          {metrics.map(([k, label]) => (
                            <option key={k} value={k}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </label>
                      <div className="focus-path">
                        <button onClick={() => setFocusId("")}>
                          Entire plan
                        </button>
                        {breadcrumbs.map((n) => (
                          <span key={n.node_id}>
                            {" "}
                            ›{" "}
                            <button onClick={() => setFocusId(n.node_id)}>
                              {n.label}
                            </button>
                          </span>
                        ))}
                      </div>
                      {collapsed.length > 0 && (
                        <button
                          className="text-button"
                          onClick={() => setCollapsed([])}
                        >
                          Expand all
                        </button>
                      )}
                    </div>
                    {loading ? (
                      <div className="preview-state" role="status">
                        <span className="spinner" />
                        Rendering plan…
                      </div>
                    ) : previewError ? (
                      <div className="error">{previewError}</div>
                    ) : (
                      <iframe
                        ref={frame}
                        className="chart-frame"
                        title="Interactive execution plan"
                        sandbox="allow-scripts allow-downloads allow-modals"
                        srcDoc={html}
                        onLoad={() =>
                          frame.current?.contentWindow?.postMessage(
                            { type: "myflames-select", node_id: selectedId },
                            "*",
                          )
                        }
                      />
                    )}
                  </section>
                  <section className="operators card">
                    <div className="card-heading">
                      <h2>
                        Operators{" "}
                        <span className="count">{active.operators.length}</span>
                      </h2>
                      <label className="search">
                        <Icon name="search" size={15} />
                        <input
                          type="search"
                          placeholder="Search operators or tables"
                          aria-label="Search operators or tables"
                          value={search}
                          onChange={(e) => setSearch(e.target.value)}
                        />
                      </label>
                    </div>
                    <div className="operator-table-head">
                      <span>Operation</span>
                      <span>Self time</span>
                      <span>Rows / loop</span>
                    </div>
                    <div className="operator-list">
                      {visibleOps.map((op) => (
                        <button
                          key={op.node_id}
                          className={`operator-row ${selectedId === op.node_id ? "selected" : ""}`}
                          aria-pressed={selectedId === op.node_id}
                          onClick={() => selectOperator(op.node_id)}
                        >
                          <span
                            style={{ paddingLeft: Math.min(op.depth, 8) * 13 }}
                          >
                            <i />
                            {op.label}
                          </span>
                          <span>
                            {typeof op.details.actual_last_row_ms === "number"
                              ? time(op.self_time_ms)
                              : "—"}
                          </span>
                          <span>
                            {typeof op.details.actual_rows === "number"
                              ? number(op.details.actual_rows)
                              : "—"}
                          </span>
                        </button>
                      ))}
                      {!visibleOps.length && (
                        <p className="empty-small">No matching operators.</p>
                      )}
                    </div>
                  </section>
                </div>
                <aside
                  className="inspector card"
                  aria-label="Operator inspector"
                >
                  <div className="card-heading">
                    <h2>Inspector</h2>
                    {selected && <span className="selection-dot" />}
                  </div>
                  {selected ? (
                    <div className="operator-detail">
                      <div className="eyebrow">SELECTED OPERATOR</div>
                      <h3>{selected.label}</h3>
                      <div className="inspector-actions">
                        <button
                          className="button"
                          onClick={() => setFocusId(selected.node_id)}
                        >
                          Focus branch
                        </button>
                        {!!selected.children?.length && (
                          <button
                            className="button"
                            onClick={() =>
                              setCollapsed((ids) =>
                                ids.includes(selected.node_id)
                                  ? ids.filter((id) => id !== selected.node_id)
                                  : [...ids, selected.node_id],
                              )
                            }
                          >
                            {collapsed.includes(selected.node_id)
                              ? "Expand branch"
                              : "Collapse branch"}
                          </button>
                        )}
                      </div>
                      <h4>In this plan</h4>
                      <dl className="details-grid">
                        <dt>Self time</dt>
                        <dd>
                          {typeof selected.details.actual_last_row_ms ===
                          "number"
                            ? time(selected.self_time_ms)
                            : "—"}
                        </dd>
                        <dt>Total time</dt>
                        <dd>
                          {typeof selected.details.actual_last_row_ms ===
                          "number"
                            ? time(selected.total_time_ms)
                            : "—"}
                        </dd>
                        <dt>Actual rows / loop</dt>
                        <dd>
                          {typeof selected.details.actual_rows === "number"
                            ? number(selected.details.actual_rows)
                            : "—"}
                        </dd>
                        <dt>Estimated rows / loop</dt>
                        <dd>
                          {typeof selected.details.estimated_rows === "number"
                            ? number(selected.details.estimated_rows)
                            : "—"}
                        </dd>
                        <dt>Loops</dt>
                        <dd>{number(selected.loops)}</dd>
                        <dt>Estimate mismatch</dt>
                        <dd>
                          {ratio(selected) === null
                            ? "—"
                            : ratio(selected) === Infinity
                              ? "Zero vs nonzero"
                              : `${ratio(selected)!.toFixed(1)}×`}
                        </dd>
                      </dl>
                      <p className="hint">
                        Times include all loops. Row counts are per loop.
                      </p>
                      {!!selected.details.complexity && (
                        <section className="inspector-section">
                          <h4>What this does</h4>
                          <p>
                            {String(
                              (
                                selected.details.complexity as {
                                  rationale?: string;
                                }
                              ).rationale || selected.details.operation,
                            )}
                          </p>
                        </section>
                      )}
                      <section className="inspector-section">
                        <h4>Related findings</h4>
                        {active.analysis.warnings
                          .filter(
                            (w) =>
                              w.node_ids?.includes(selectedId) ||
                              w.node_labels?.includes(selected.label),
                          )
                          .map((w, i) => (
                            <p key={i}>{w.text}</p>
                          ))}
                        {!active.analysis.warnings.some(
                          (w) =>
                            w.node_ids?.includes(selectedId) ||
                            w.node_labels?.includes(selected.label),
                        ) && (
                          <p className="hint">
                            No findings are linked to this operator. Review its
                            measurements and inputs in context.
                          </p>
                        )}
                      </section>
                      {selectedHook && (
                        <section className="inspector-section learn-card">
                          <Icon name="book" />
                          <h4>Understand the algorithm</h4>
                          <p>
                            Explore this operation with an interactive lesson.
                          </p>
                          <button
                            className="button"
                            onClick={() => {
                              setLesson(selectedHook.lesson);
                              setReturnToPlan(true);
                              setMode("teach");
                            }}
                          >
                            Learn this operator
                          </button>
                        </section>
                      )}
                      <details className="raw-fields">
                        <summary>All operator fields</summary>
                        <pre>{JSON.stringify(selected.details, null, 2)}</pre>
                      </details>
                    </div>
                  ) : (
                    <div className="inspector-empty">
                      <Icon name="search" size={26} />
                      <h3>Explore an operator</h3>
                      <p>
                        Select a node in the chart or operator list. Its
                        measurements, findings, and lessons appear here.
                      </p>
                    </div>
                  )}
                </aside>
              </div>
              {active.analysis.warnings.length > 0 && (
                <section className="card findings">
                  <div className="card-heading">
                    <h2>Findings</h2>
                  </div>
                  {active.analysis.warnings.map((w, i) => (
                    <div className="finding" key={i}>
                      <span className={`severity ${w.severity}`}>
                        {w.severity}
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
                </section>
              )}
              <ContextPanel key={active.id} plan={active} />
              <section className="card investigation-notes">
                <div className="card-heading">
                  <h2>Investigation notebook</h2>
                  <button
                    className="text-button"
                    onClick={() =>
                      download(
                        JSON.stringify(makeBundle(), null, 2),
                        `${name.replace(/[^a-z0-9_-]/gi, "-") || "investigation"}.json`,
                        "application/json",
                      )
                    }
                  >
                    Export investigation bundle
                  </button>
                </div>
                <div className="notebook-grid">
                  <label className="field">
                    Investigation name
                    <input
                      value={name}
                      onChange={(e) => {
                        setName(e.target.value);
                        setDirty(true);
                      }}
                    />
                  </label>
                  <label className="field">
                    Experiment name
                    <input
                      value={active.name}
                      onChange={(e) => {
                        setPlans((ps) =>
                          ps.map((p) =>
                            p.id === activeId
                              ? { ...p, name: e.target.value }
                              : p,
                          ),
                        );
                        setDirty(true);
                      }}
                    />
                  </label>
                  <label className="field">
                    Baseline
                    <select
                      value={baseline}
                      onChange={(e) => {
                        setBaseline(e.target.value);
                        setBeforeId(e.target.value);
                        setDirty(true);
                      }}
                    >
                      {plans.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    Tags
                    <input
                      placeholder="payments, index-review"
                      value={tags}
                      onChange={(e) => {
                        setTags(e.target.value);
                        setDirty(true);
                      }}
                    />
                  </label>
                </div>
                <label className="field">
                  Notes and conclusions
                  <textarea
                    placeholder="What changed? What did the evidence show?"
                    value={notes}
                    onChange={(e) => {
                      setNotes(e.target.value);
                      setDirty(true);
                    }}
                  />
                </label>
                <div className="button-row">
                  <button
                    className="button primary"
                    disabled={busy}
                    onClick={save}
                  >
                    Save investigation
                  </button>
                  <span className="hint">
                    Saves queries and plans on this computer. Credentials are
                    excluded.
                  </span>
                </div>
              </section>
            </>
          )}
          {mode === "compare" && (
            <section>
              <div className="page-heading">
                <div>
                  <div className="eyebrow">MEASURE THE CHANGE</div>
                  <h1>Compare experiments</h1>
                  <p>
                    Review structural changes and measured differences together.
                  </p>
                </div>
                {comparison && (
                  <div className="heading-actions">
                    <button className="button" onClick={exportJson}>
                      Export JSON
                    </button>
                    <button className="button" onClick={exportHtml}>
                      Export HTML
                    </button>
                  </div>
                )}
                <button className="button" onClick={() => setImportOpen(true)}>
                  Import plan
                </button>
              </div>
              <div className="compare-selectors card">
                <label className="field">
                  Before
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
                <label className="field">
                  After
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
              {!before || !after || before.id === after.id ? (
                <div className="compare-empty card empty-small">
                  <h2>Choose two different experiments</h2>
                  <p>
                    Import or capture a second plan to compare it with your
                    baseline.
                  </p>
                </div>
              ) : loading ? (
                <div className="preview-state">Matching operators…</div>
              ) : (
                comparison && (
                  <>
                    <div className="metrics">
                      <Metric
                        label="Before"
                        value={
                          comparison.analysis.before.timing_available === false
                            ? "Not executed"
                            : time(comparison.analysis.before.total_time_ms)
                        }
                        hint={before.name}
                      />
                      <Metric
                        label="After"
                        value={
                          comparison.analysis.after.timing_available === false
                            ? "Not executed"
                            : time(comparison.analysis.after.total_time_ms)
                        }
                        hint={after.name}
                      />
                      <Metric
                        label="Change"
                        value={
                          comparison.analysis.summary.time_delta_pct === null
                            ? "—"
                            : `${comparison.analysis.summary.time_delta_pct}%`
                        }
                        hint="Total measured plan time"
                      />
                      <Metric
                        label="Regressions"
                        value={String(comparison.analysis.summary.regressions)}
                        hint="Confident matches only"
                      />
                    </div>
                    <p className="hint">
                      Select a node or comparison row to highlight its
                      counterpart. Uncertain matches are tentative and excluded
                      from regression counts.
                    </p>
                    <div className="comparison-canvases">
                      <div className="card">
                        <h3>{before.name}</h3>
                        {before.capture && (
                          <p className="comparison-samples">
                            {before.capture.measurements.median_ms === null
                              ? "Estimated plan · no measured runs."
                              : `${before.capture.measurements.count} measured runs · median ${time(before.capture.measurements.median_ms)}. Chart shows the last captured plan.`}
                          </p>
                        )}
                        <iframe
                          ref={beforeFrame}
                          className="comparison-frame"
                          title="Before execution plan"
                          sandbox="allow-scripts"
                          srcDoc={compareCharts[0]}
                        />
                      </div>
                      <div className="card">
                        <h3>{after.name}</h3>
                        {after.capture && (
                          <p className="comparison-samples">
                            {after.capture.measurements.median_ms === null
                              ? "Estimated plan · no measured runs."
                              : `${after.capture.measurements.count} measured runs · median ${time(after.capture.measurements.median_ms)}. Chart shows the last captured plan.`}
                          </p>
                        )}
                        <iframe
                          ref={afterFrame}
                          className="comparison-frame"
                          title="After execution plan"
                          sandbox="allow-scripts"
                          srcDoc={compareCharts[1]}
                        />
                      </div>
                    </div>
                    <div className="card comparison-list">
                      <div className="card-heading">
                        <h2>Operator changes</h2>
                      </div>
                      {comparison.analysis.deltas.map((d, i) => (
                        <button
                          key={i}
                          className={`comparison-row ${pair[0] === d.before_node_id && pair[1] === d.after_node_id ? "selected" : ""}`}
                          onClick={() =>
                            setPair([
                              d.before_node_id || "",
                              d.after_node_id || "",
                            ])
                          }
                        >
                          <span>
                            <strong>
                              {d.before_label || "Added"} →{" "}
                              {d.after_label || "Removed"}
                            </strong>
                            <small>
                              {d.matching.method} · {d.matching.confidence}{" "}
                              confidence
                            </small>
                          </span>
                          <span className="match-badge">
                            {d.matching.status}
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
          <footer className="app-footer">
            myflames · Inspired by{" "}
            <a
              href="https://www.brendangregg.com/flamegraphs.html"
              target="_blank"
              rel="noreferrer"
            >
              Brendan Gregg’s FlameGraph
            </a>{" "}
            and{" "}
            <a
              href="https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/"
              target="_blank"
              rel="noreferrer"
            >
              Tanel Poder’s SQL Plan FlameGraphs
            </a>
            .
          </footer>
        </main>
      </div>
      <input
        ref={file}
        type="file"
        accept=".json,application/json"
        hidden
        onChange={(e) => {
          importFile(e.target.files?.[0]);
          e.target.value = "";
        }}
      />
      {importOpen && (
        <ImportDialog
          busy={busy}
          error={error}
          onClose={() => {
            setImportOpen(false);
            setError("");
          }}
          onFile={(f) => {
            if (f) importFile(f);
            else file.current?.click();
          }}
          onPaste={(raw, n) => {
            importPlan(raw, n).catch(() => {});
          }}
        />
      )}
    </div>
  );
}
function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint: string;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{hint}</small>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
