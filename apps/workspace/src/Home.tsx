// Home is a launcher: the shell owns its title and toolbar, Connections owns
// server settings, and recent work uses the same compact rows as other places.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { appVersion, getInvestigation, listInvestigations, time, type Bundle, type InvestigationSummary, type Plan } from "./api";
import { Icon } from "./components";

import { defaultLab, relativeTime, type QueryTab } from "./session";
import "./home.css";

export function Home({
  busy, plans, connections, onDropFile, onSample,
  onOpenPlan, onOpenBundle, onInvestigations, queryTabs = [], onOpenQuery,
  onQuery, onServer, onDiagram,
}: {
  logo?: string;
  busy: boolean;
  plans: Plan[];
  /** App supplies the existing Connections component, including its editor. */
  connections: ReactNode;
  onDropFile: (file?: File) => void;
  onSample: () => void;
  onOpenPlan: (id: string) => void;
  onOpenBundle: (bundle: Bundle) => Promise<void>;
  onInvestigations: () => void;
  /** Supply App's current tabs, rather than reading a possibly stale saved session. */
  queryTabs?: QueryTab[];
  onOpenQuery?: (id: string) => void;
  onQuery: () => void;
  onServer: () => void;
  onDiagram: () => void;
}) {
  const [saved, setSaved] = useState<InvestigationSummary[] | null>(null);
  const [error, setError] = useState("");
  const [opening, setOpening] = useState("");
  const openingRef = useRef(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    listInvestigations(6, null, "", controller.signal)
      .then((r) => {
        if (!controller.signal.aborted)
          setSaved([...r.items].sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")));
      })
      .catch((e) => { if (!controller.signal.aborted) setError((e as Error).message); });
    return () => controller.abort();
  }, [retry]);
  const recent = (saved || []).slice(0, 6);
  const initialTab = defaultLab().tabs[0];
  const onlyInitialQuery = queryTabs.length === 1 && queryTabs[0].id === initialTab.id &&
    queryTabs[0].name === initialTab.name && queryTabs[0].sql === initialTab.sql &&
    queryTabs[0].parameters === initialTab.parameters;
  const queries = onlyInitialQuery ? [] : queryTabs;
  async function open(bundle: InvestigationSummary) {
    if (openingRef.current) return;
    openingRef.current = true;
    setOpening(bundle.id || bundle.name);
    setError("");
    try { await onOpenBundle(await getInvestigation(bundle)); }
    catch (e) { setError((e as Error).message); }
    finally { openingRef.current = false; setOpening(""); }
  }
  return (
    <section className="home welcome" aria-labelledby="home-title"
      onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) e.preventDefault(); }}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        onDropFile(e.dataTransfer.files[0]);
      }}>
      <h1 id="home-title" className="visually-hidden">Home</h1>
      <nav className="home-workflows" aria-label="Start working">
        <button onClick={onQuery}><Icon name="query" /><span><strong>Write SQL</strong><small>Queries, completion, and table data</small></span><Icon name="disclosure" size={14} /></button>
        <button onClick={onServer}><Icon name="server" /><span><strong>Inspect server</strong><small>Activity, performance, and configuration</small></span><Icon name="disclosure" size={14} /></button>
        <button onClick={onDiagram}><Icon name="plan" /><span><strong>Database diagram</strong><small>Live schemas and Mermaid diagrams</small></span><Icon name="disclosure" size={14} /></button>
      </nav>
      <div className="home-launcher">
        {connections}
        <section className="home-pane recent-work" aria-labelledby="recent-title">
          <div className="pane-head">
            <h2 id="recent-title">Recent work</h2>
          </div>
          {error && <div className="home-load-error"><p role="alert" className="error">{error}</p>
            <button className="text-button" onClick={() => setRetry((n) => n + 1)}>Retry investigations</button></div>}
          {saved === null && !error && <p className="empty-small" role="status">Loading investigations…</p>}
          {!plans.length && !recent.length && !queries.length && saved !== null && (
            <div className="home-start" aria-label="Get started">
              <p>No recent work yet. Import a plan from the top bar, or start with the sample.</p>
              <div className="home-start-actions">
                <button className="button sample-card" disabled={busy} onClick={onSample}>Explore sample plan</button>
              </div>
            </div>
          )}
          <ul className="recent-list" aria-label="Recent work">
            {plans.map((p) => <li key={`plan:${p.id}`} data-kind="plan">
              <button className="recent-item" onClick={() => onOpenPlan(p.id)}>
                <Icon name="plan" />
                <span><strong title={p.name}>{p.name}</strong><small>
                  Plan · {p.capture?.mode === "explain" ? "Estimated" : time(p.analysis.plan_summary.total_time_ms)}
                  {p.capture ? " · Captured" : " · Imported"}
                </small></span>
                <Icon name="disclosure" size={14} />
              </button>
            </li>)}
            {recent.map((b) => <li key={`investigation:${b.id}`} data-kind="investigation">
              <button className="recent-item" disabled={!!opening} onClick={() => void open(b)}>
                <Icon name="investigation" />
                <span><strong title={b.name}>{b.name}</strong><small>
                  Investigation · {b.plans.length} {b.plans.length === 1 ? "plan" : "plans"}
                  {b.updated_at && <> · <time dateTime={b.updated_at}>{relativeTime(b.updated_at)}</time></>}
                  {opening === (b.id || b.name) ? " · Opening…" : ""}
                </small></span>
                <Icon name="disclosure" size={14} />
              </button>
            </li>)}
            {onOpenQuery && queries.map((tab) => <li key={`query:${tab.id}`} data-kind="query">
              <button className="recent-item" onClick={() => onOpenQuery(tab.id)}>
                <Icon name="query" />
                <span><strong title={tab.name}>{tab.name}</strong><small title={tab.sql}>
                  Query · {tab.sql.trim().replace(/\s+/g, " ") || "Empty query"}
                </small></span>
                <Icon name="disclosure" size={14} />
              </button>
            </li>)}
          </ul>
          {!!saved?.length && <button className="text-button recent-more" onClick={onInvestigations}>All investigations</button>}
        </section>
      </div>
      <footer className="home-version" aria-label="Application version">
        <strong>myflames {appVersion() || "development"}</strong>
        <span>Updates not checked</span>
        <a href="https://github.com/vgrippa/myflames/releases" target="_blank" rel="noopener noreferrer">View releases</a>
      </footer>
    </section>
  );
}
