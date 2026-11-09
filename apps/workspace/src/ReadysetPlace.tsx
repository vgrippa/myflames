// The Readyset place: multi-tier topology (ProxySQL -> Readyset -> MySQL/MariaDB),
// active shallow & deep caches with EXPLAIN CREATE CACHE dry-run and DDL export,
// proxied query analysis, base-table CDC replication & re-snapshotting, and
// dynamic runtime parameters & shallow cache allowlists. Every write goes
// through ActionReview.tsx (dbops + reviewed_writes + Audit log).
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { download, type Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { ConnectionTypeBand, useWritePolicy } from "./Connections";
import { ActionResult, useReviewedActions } from "./ActionReview";
import { ClusterMark } from "./ClusterTopology";
import {
  ConfirmSheet,
  DataTable,
  EmptyState,
  Fields,
  GroupedList,
  GroupedRow,
  InspectorContext,
  InspectorSection,
  MetricTile,
  Pill,
  SearchField,
  Segmented,
  StatusPill,
  SummaryLine,
  TileGrid,
  ToolbarButton,
  ViewLayout,
  ViewStatus,
  useWindowStatus,
  type Column,
  type InspectorContent,
  type InspectorShared,
  type Selection,
  type Tone,
} from "./kit";
import { CompactNumber, CopySql, ms } from "./PerfShared";
import { plural } from "./OpsShared";
import {
  explainReadysetCache,
  exportReadysetCaches,
  planReadysetAction,
  readReadyset,
  sendReadysetAction,
  type ReadysetCache,
  type ReadysetClientConnection,
  type ReadysetExplainResponse,
  type ReadysetExportResponse,
  type ReadysetFailure,
  type ReadysetProxiedQuery,
  type ReadysetQueryStats,
  type ReadysetReport,
  type ReadysetSupportVerdict,
  type ReadysetTable,
  type ReadysetUpstream,
} from "./readysetApi";
import { ReadysetTopologyDiagram } from "./ReadysetTopology";
import { useOpenRead } from "./viewOnScreen";
import "./cluster.css";
import "./readyset.css";

export type ReadysetTab = "topology" | "caches" | "proxied" | "tables" | "parameters";
const SHEET_WIDTH = 1100;

type CreateDraft = {
  query_or_id: string;
  cache_type: "shallow" | "deep";
  name: string;
  ttl_seconds: string;
  refresh_seconds: string;
  coalesce_ms: string;
  adaptive: boolean;
  routing_override: "default" | "always" | "until_write";
  concurrently: boolean;
};

type InputPromptState =
  | { kind: "log_level"; value: string }
  | { kind: "eviction_memory"; mb: string }
  | { kind: "eviction_period"; ms: string }
  | { kind: "allowlist"; allow_kind: "function" | "variable" | "schema"; name: string }
  | { kind: "add_tables"; tables: string }
  | { kind: "replication_position"; position: string }
  | { kind: "drop_all_caches"; scope: "all" | "shallow" | "deep" };

const num = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const numCell = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v) ? <CompactNumber value={v} /> : "—");
const fmtCount = (v: number | null | undefined) => (typeof v === "number" && Number.isFinite(v) ? v.toLocaleString() : "—");

function supportTone(v: ReadysetSupportVerdict): Tone {
  if (v === "yes" || v === "cached") return "ok";
  if (v === "pending" || v === "skipped") return "watch";
  if (v === "unsupported" || v === "error") return "problem";
  return "unknown";
}

function supportWord(v: ReadysetSupportVerdict): string {
  if (v === "yes") return "Supported";
  if (v === "cached") return "Cached";
  if (v === "pending") return "Pending";
  if (v === "skipped") return "Skipped";
  if (v === "unsupported") return "Unsupported";
  if (v === "error") return "Error";
  return "Unknown";
}

function tableTone(bucket: ReadysetTable["bucket"]): Tone {
  if (bucket === "snapshotted") return "ok";
  if (bucket === "snapshotting") return "watch";
  if (bucket === "error") return "problem";
  return "unknown";
}

export function summaryText(report: ReadysetReport): string {
  if (!report.is_readyset) return "Not a Readyset instance";
  const parts = [
    `Readyset ${report.version || ""}`.trim(),
    report.port_role === "admin" ? "admin SQL port" : "SQL port",
    plural(report.caches.length, "cache"),
  ];
  if (report.telemetry.cached_share_pct !== null) {
    parts.push(`${report.telemetry.cached_share_pct}% cached`);
  }
  if (report.replication?.status) {
    parts.push(`Repl ${report.replication.status}`);
  }
  return parts.join(" · ");
}

function statsSection(title: string, stats: ReadysetQueryStats | null): ReactNode {
  if (!stats) return null;
  return (
    <InspectorSection title={title}>
      <Fields
        rows={[
          ["Executions", fmtCount(stats.total_count)],
          ["Average", ms(stats.avg_ms)],
          ["p50", ms(stats.p50_ms)],
          ["p90", ms(stats.p90_ms)],
          ["p95", ms(stats.p95_ms)],
          ["p99", ms(stats.p99_ms)],
          ["p99.9", ms(stats.p999_ms)],
          ["Max", ms(stats.max_ms)],
        ]}
      />
    </InspectorSection>
  );
}

function CreateCacheSheet({
  initial,
  connection,
  onClose,
  onReview,
}: {
  initial: CreateDraft;
  connection: Connection;
  onClose: () => void;
  onReview: (spec: Record<string, unknown>) => void;
}) {
  const [draft, setDraft] = useState<CreateDraft>(initial);
  const [checking, setChecking] = useState(false);
  const [explain, setExplain] = useState<ReadysetExplainResponse | null>(null);
  const [checkError, setCheckError] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const runCheck = useCallback(
    async (queryText: string) => {
      const q = queryText.trim();
      if (!q) return;
      abortRef.current?.abort();
      const ctl = new AbortController();
      abortRef.current = ctl;
      setChecking(true);
      setCheckError("");
      try {
        const res = await explainReadysetCache(connection, q, "both", ctl.signal);
        if (ctl.signal.aborted) return;
        setExplain(res);
        if (res.shallow?.supported === "unsupported" && res.deep?.supported === "yes") {
          setDraft((d) => ({ ...d, cache_type: "deep" }));
        } else if (res.deep?.supported === "unsupported" && res.shallow?.supported === "yes") {
          setDraft((d) => ({ ...d, cache_type: "shallow" }));
        }
      } catch (e) {
        if (!ctl.signal.aborted) setCheckError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!ctl.signal.aborted) setChecking(false);
      }
    },
    [connection],
  );

  useEffect(() => {
    if (initial.query_or_id.trim()) {
      void runCheck(initial.query_or_id);
    }
    return () => abortRef.current?.abort();
  }, [initial.query_or_id, runCheck]);

  const ttlNum = draft.ttl_seconds.trim() ? Number(draft.ttl_seconds.trim()) : null;
  const refNum = draft.refresh_seconds.trim() ? Number(draft.refresh_seconds.trim()) : null;
  const coalNum = draft.coalesce_ms.trim() ? Number(draft.coalesce_ms.trim()) : null;

  let validationError = "";
  if (!draft.query_or_id.trim()) {
    validationError = "Enter a SQL SELECT query or a Readyset query ID (q_…).";
  } else if (draft.cache_type === "shallow") {
    if (ttlNum !== null && (!Number.isInteger(ttlNum) || ttlNum < 1 || ttlNum > 31536000)) {
      validationError = "TTL must be between 1 and 31,536,000 seconds.";
    } else if (refNum !== null && ttlNum === null) {
      validationError = "Set a TTL before setting a background refresh interval.";
    } else if (refNum !== null && ttlNum !== null && ttlNum <= 1) {
      validationError = "TTL must be at least 2 seconds when setting a background refresh interval.";
    } else if (refNum !== null && ttlNum !== null && (!Number.isInteger(refNum) || refNum < 1 || refNum >= ttlNum)) {
      validationError = "Refresh interval must be at least 1 second and less than the TTL.";
    } else if (coalNum !== null && (!Number.isInteger(coalNum) || coalNum < 1 || coalNum > 60000)) {
      validationError = "Coalesce window must be between 1 and 60,000 ms.";
    }
  }

  const chosenExplain = draft.cache_type === "shallow" ? explain?.shallow : explain?.deep;
  const unsupportedChosen = chosenExplain?.supported === "unsupported" || chosenExplain?.supported === "error";

  const submit = () => {
    if (validationError || unsupportedChosen) return;
    const spec: Record<string, unknown> = {
      cache_type: draft.cache_type,
      query_or_id: draft.query_or_id.trim(),
      name: draft.name.trim() || undefined,
      always: draft.routing_override === "always",
      until_write: draft.routing_override === "until_write",
    };
    if (draft.cache_type === "shallow") {
      if (ttlNum !== null) spec.ttl_seconds = ttlNum;
      if (refNum !== null) spec.refresh_seconds = refNum;
      if (coalNum !== null) spec.coalesce_ms = coalNum;
      if (draft.adaptive) spec.adaptive = true;
    } else if (draft.concurrently) {
      spec.concurrently = true;
    }
    onReview(spec);
  };

  return (
    <ConfirmSheet
      className="ops-confirm action-value readyset-create-dialog"
      band={<ConnectionTypeBand connection={connection} />}
      eyebrow="CREATE CACHE"
      title="Configure Readyset cache"
      confirmTone="primary"
      confirmLabel="Review CREATE CACHE…"
      confirmDisabled={!!validationError || unsupportedChosen}
      onCancel={onClose}
      onConfirm={submit}
    >
      <div className="readyset-form">
        <label className="ops-field action-typed">
          <span>SQL query or query ID (for example q_8f21ab04)</span>
          <textarea
            rows={3}
            value={draft.query_or_id}
            onChange={(e) => {
              abortRef.current?.abort();
              setChecking(false);
              setCheckError("");
              setDraft((d) => ({ ...d, query_or_id: e.target.value }));
              setExplain(null);
            }}
            placeholder="SELECT id, total FROM orders WHERE customer_id = ?"
            spellCheck={false}
          />
        </label>

        <div className="readyset-explain-bar">
          <button
            type="button"
            className="button"
            disabled={!draft.query_or_id.trim() || checking}
            onClick={() => void runCheck(draft.query_or_id)}
          >
            {checking ? "Checking EXPLAIN…" : "Check support (EXPLAIN)"}
          </button>
          {explain && (
            <div className="readyset-explain-pills">
              {explain.shallow && (
                <StatusPill
                  tone={supportTone(explain.shallow.supported)}
                  label={`Shallow: ${supportWord(explain.shallow.supported)}`}
                  title={explain.shallow.reason || undefined}
                />
              )}
              {explain.deep && (
                <StatusPill
                  tone={supportTone(explain.deep.supported)}
                  label={`Deep: ${supportWord(explain.deep.supported)}`}
                  title={explain.deep.reason || undefined}
                />
              )}
            </div>
          )}
        </div>

        {checkError && <p className="ops-warning" role="alert">{checkError}</p>}
        {chosenExplain?.reason && (
          <p className="readyset-form-note">
            {draft.cache_type === "shallow" ? "Shallow" : "Deep"} cache note: {chosenExplain.reason}
          </p>
        )}
        {explain && explain.collisions.length > 0 && (
          <p className="ops-warning">
            Collides with existing cache {explain.collisions.map((c) => c.name).join(", ")}; Readyset replaces it when created.
          </p>
        )}

        <div className="readyset-form-row">
          <label className="ops-field action-typed">
            <span>Cache type</span>
            <select
              value={draft.cache_type}
              onChange={(e) => setDraft((d) => ({ ...d, cache_type: e.target.value as "shallow" | "deep" }))}
            >
              <option value="shallow">Shallow cache (TTL &amp; refresh)</option>
              <option value="deep">Deep cache (streaming dataflow)</option>
            </select>
          </label>
          <label className="ops-field action-typed">
            <span>Cache name (optional)</span>
            <input
              type="text"
              value={draft.name}
              onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
              placeholder="orders_by_customer"
              spellCheck={false}
            />
          </label>
        </div>

        {draft.cache_type === "shallow" ? (
          <div className="readyset-form-row is-three">
            <label className="ops-field action-typed">
              <span>TTL (seconds)</span>
              <input
                type="number"
                min={1}
                value={draft.ttl_seconds}
                onChange={(e) => setDraft((d) => ({ ...d, ttl_seconds: e.target.value }))}
                placeholder="60"
              />
            </label>
            <label className="ops-field action-typed">
              <span>Refresh every (s)</span>
              <input
                type="number"
                min={1}
                value={draft.refresh_seconds}
                onChange={(e) => setDraft((d) => ({ ...d, refresh_seconds: e.target.value }))}
                placeholder="Optional"
              />
            </label>
            <label className="ops-field action-typed">
              <span>Coalesce (ms)</span>
              <input
                type="number"
                min={1}
                value={draft.coalesce_ms}
                onChange={(e) => setDraft((d) => ({ ...d, coalesce_ms: e.target.value }))}
                placeholder="Optional"
              />
            </label>
          </div>
        ) : (
          <label className="readyset-check">
            <input
              type="checkbox"
              checked={draft.concurrently}
              onChange={(e) => setDraft((d) => ({ ...d, concurrently: e.target.checked }))}
            />
            <span>Build deep cache CONCURRENTLY without blocking reads</span>
          </label>
        )}

        <div className="readyset-form-row">
          <label className="ops-field action-typed">
            <span>Explicit transaction routing</span>
            <select
              value={draft.routing_override}
              onChange={(e) =>
                setDraft((d) => ({ ...d, routing_override: e.target.value as CreateDraft["routing_override"] }))
              }
            >
              <option value="default">Server default</option>
              <option value="always">ALWAYS (serve inside transactions too)</option>
              <option value="until_write">UNTIL WRITE (serve until first write in tx)</option>
            </select>
          </label>
          {draft.cache_type === "shallow" && (
            <label className="readyset-check is-inline">
              <input
                type="checkbox"
                checked={draft.adaptive}
                onChange={(e) => setDraft((d) => ({ ...d, adaptive: e.target.checked }))}
              />
              <span>ADAPTIVE refresh scheduling</span>
            </label>
          )}
        </div>

        {validationError && <p className="readyset-form-hint">{validationError}</p>}
      </div>
    </ConfirmSheet>
  );
}

function ExportCachesSheet({
  exported,
  onClose,
}: {
  exported: ReadysetExportResponse;
  onClose: () => void;
}) {
  return (
    <ConfirmSheet
      className="ops-confirm readyset-export-dialog"
      eyebrow="EXPLAIN ALL CACHES"
      title={`Exported ${plural(exported.count, "cache")}`}
      confirmTone="primary"
      confirmLabel="Close"
      doneLabel="Close"
      secondary={{
        label: "Download .sql",
        onClick: () => download(exported.sql, "readyset-caches.sql", "application/sql;charset=utf-8"),
      }}
      onCancel={onClose}
      onConfirm={onClose}
    >
      <CopySql sql={exported.sql} label="Cache DDL" />
    </ConfirmSheet>
  );
}

export function ReadysetPlace({
  connection,
  disabled = false,
  report,
  onReport,
  failure = null,
  onFailure,
  onOpenProxySql,
  onOpenCluster,
  onOpenReplication,
  onOpenServer,
  onSwitchConnection,
}: {
  connection: Connection;
  disabled?: boolean;
  report: Bound<ReadysetReport> | null;
  onReport: (report: Bound<ReadysetReport>) => void;
  failure?: ReadysetFailure | null;
  onFailure?: (key: string, message: string) => void;
  onOpenProxySql?: () => void;
  onOpenCluster?: () => void;
  onOpenReplication?: () => void;
  onOpenServer?: () => void;
  onSwitchConnection?: (profileId: string) => void;
}) {
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(340);
  const [tab, setTab] = useState<ReadysetTab>("topology");
  const placeRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(() => (typeof window !== "undefined" ? window.innerWidth : 1440));

  useEffect(() => {
    const el = placeRef.current;
    const measure = () => {
      const w = el && el.clientWidth > 0 ? el.clientWidth : window.innerWidth;
      if (w > 0) setWidth(w);
    };
    measure();
    const observer = typeof ResizeObserver !== "undefined" && el ? new ResizeObserver(measure) : null;
    if (el && observer) observer.observe(el);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);

  const sheet = width > 0 && width < SHEET_WIDTH;
  const selection: Selection = {
    key: selectedKey,
    select: (key: string, open?: boolean) => {
      setSelectedKey(key);
      if (open !== false) setInspectorOpen(true);
    },
  };
  const inspector = useMemo<InspectorShared>(
    () => ({
      open: inspectorOpen && !!selectedKey,
      setOpen: setInspectorOpen,
      width: inspectorWidth,
      setWidth: setInspectorWidth,
      sheet,
    }),
    [inspectorOpen, selectedKey, inspectorWidth, sheet],
  );

  const key = connectionKey(connection);
  const current = report && report.key === key ? report.report : null;
  useWindowStatus(`readyset:${key}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
    engineVersion: current?.is_readyset ? `Readyset ${current.version || ""}`.trim() : undefined,
  });

  return (
    <InspectorContext.Provider value={inspector}>
      <div ref={placeRef} className="cluster-place readyset-place">
        <ReadysetView
          connection={connection}
          disabled={disabled}
          report={report}
          onReport={onReport}
          failure={failure}
          onFailure={onFailure}
          selection={selection}
          tab={tab}
          onTab={(t) => {
            setTab(t);
            setSelectedKey("");
          }}
          onOpenProxySql={onOpenProxySql}
          onOpenCluster={onOpenCluster}
          onOpenReplication={onOpenReplication}
          onOpenServer={onOpenServer}
          onSwitchConnection={onSwitchConnection}
        />
      </div>
    </InspectorContext.Provider>
  );
}

function ReadysetView({
  connection,
  disabled,
  report: bound,
  onReport,
  failure,
  onFailure,
  selection,
  tab,
  onTab,
  onOpenProxySql,
  onOpenCluster,
  onOpenReplication,
  onOpenServer,
  onSwitchConnection,
}: {
  connection: Connection;
  disabled: boolean;
  report: Bound<ReadysetReport> | null;
  onReport: (report: Bound<ReadysetReport>) => void;
  failure: ReadysetFailure | null;
  onFailure?: (key: string, message: string) => void;
  selection: Selection;
  tab: ReadysetTab;
  onTab: (tab: ReadysetTab) => void;
  onOpenProxySql?: () => void;
  onOpenCluster?: () => void;
  onOpenReplication?: () => void;
  onOpenServer?: () => void;
  onSwitchConnection?: (profileId: string) => void;
}) {
  const reads = useConnectionReads(connection);
  const policy = useWritePolicy();
  const report = bound?.report || null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const current = failure && failure.key === reads.key && failure.over === bound ? failure : null;
  const failedOver = !other && !!report && !!current;
  const stale = !!other || failedOver;

  const [proxiedQuerySearch, setProxiedQuerySearch] = useState("");
  const [uncachedOnly, setUncachedOnly] = useState(false);
  const [expandedWhy, setExpandedWhy] = useState<string[]>([]);
  const [createDraft, setCreateDraft] = useState<CreateDraft | null>(null);
  const [prompt, setPrompt] = useState<InputPromptState | null>(null);
  const [exportedSql, setExportedSql] = useState<ReadysetExportResponse | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState("");
  const readsKeyRef = useRef(reads.key);
  readsKeyRef.current = reads.key;
  const findingIdBase = useId();

  useEffect(() => {
    setExportedSql(null);
    setExportError("");
    setExportBusy(false);
  }, [reads.key]);

  async function load(force = false) {
    if (disabled) return;
    setExportError("");
    await reads.run("readyset", "Reading Readyset…", readReadyset, onReport, {
      force,
      onError: (message) => onFailure?.(reads.key, message),
    });
  }

  useOpenRead({
    key: reads.key,
    reportKey: bound?.key,
    ready: !disabled,
    busy: !!reads.loading || reads.running(),
    read: () => {
      void load();
    },
  });

  const actions = useReviewedActions({
    connection,
    operation: "readyset_action",
    send: sendReadysetAction,
    onSent: () => {
      void load(true);
    },
  });

  const isRs = !!report && report.is_readyset;
  const changeBlock = !isRs
    ? "Not a Readyset instance."
    : stale
      ? other
        ? `This report was read on ${other}. Refresh to read the connection in use.`
        : "The last read failed. Refresh first."
      : disabled
        ? "Reads are paused."
        : policy?.blocked
          ? policy.blocked
          : connection.read_only
            ? "This connection is read-only in myflames, so changes are refused."
            : "";

  const canCreate = !!report?.capabilities.can_create_cache && !changeBlock && !actions.busy;
  const createDisabledWhy = !report?.capabilities.can_create_cache
    ? report?.in_readyset_schema
      ? "Connect to the Readyset SQL port (default 3307) with an upstream database schema to run CREATE CACHE."
      : "Cache DDL is disabled on this Readyset listener."
    : changeBlock;

  const openCreateCache = useCallback((seed?: Partial<CreateDraft>) => {
    setCreateDraft({
      query_or_id: seed?.query_or_id || "",
      cache_type: seed?.cache_type || "shallow",
      name: seed?.name || "",
      ttl_seconds: seed?.ttl_seconds ?? "60",
      refresh_seconds: seed?.refresh_seconds || "",
      coalesce_ms: seed?.coalesce_ms || "",
      adaptive: seed?.adaptive || false,
      routing_override: seed?.routing_override || "default",
      concurrently: seed?.concurrently || false,
    });
  }, []);

  const runExport = async () => {
    if (exportBusy || !!other || disabled) return;
    const exportKey = reads.key;
    setExportBusy(true);
    setExportError("");
    try {
      const res = await exportReadysetCaches(connection);
      if (readsKeyRef.current !== exportKey) return;
      setExportedSql(res);
    } catch (e) {
      if (readsKeyRef.current !== exportKey) return;
      setExportError(e instanceof Error ? e.message : String(e));
    } finally {
      if (readsKeyRef.current === exportKey) setExportBusy(false);
    }
  };

  const shallowCount = isRs ? report!.caches.filter((c) => c.cache_type === "shallow").length : 0;
  const deepCount = isRs ? report!.caches.filter((c) => c.cache_type === "deep").length : 0;

  const filteredProxied = useMemo(() => {
    if (!isRs) return [];
    const q = proxiedQuerySearch.trim().toLowerCase();
    return report!.proxied_queries.filter((p) => {
      if (uncachedOnly && p.already_cached) return false;
      if (!q) return true;
      return p.query_id.toLowerCase().includes(q) || p.query.toLowerCase().includes(q);
    });
  }, [isRs, report, proxiedQuerySearch, uncachedOnly]);

  const cacheColumns: Column<ReadysetCache>[] = [
    { key: "name", label: "Cache", primary: true, width: 190, minWidth: 140, sort: (r) => r.name, render: (r) => <code className="proxysql-code">{r.name}</code> },
    { key: "type", label: "Type", width: 96, sort: (r) => r.cache_type, render: (r) => <Pill tone={r.cache_type === "shallow" ? "info" : "neutral"}>{r.cache_type}</Pill> },
    { key: "executions", label: "Executions", num: true, sort: (r) => num(r.count), render: (r) => numCell(r.count) },
    { key: "p50", label: "Cached p50", num: true, sort: (r) => num(r.cached_stats?.p50_ms), render: (r) => ms(r.cached_stats?.p50_ms) },
    { key: "p99", label: "Cached p99", num: true, sort: (r) => num(r.cached_stats?.p99_ms), render: (r) => ms(r.cached_stats?.p99_ms) },
    {
      key: "hit_rate",
      label: "Hit rate",
      num: true,
      sort: (r) => num(r.hit_rate_pct),
      render: (r) => (r.cache_type === "shallow" ? (r.hit_rate_pct !== null ? `${r.hit_rate_pct}%` : "—") : "Materialized"),
    },
    { key: "query", label: "Query", flex: true, sort: (r) => r.query || r.query_id, render: (r) => <code className="proxysql-code">{r.query || r.query_id}</code> },
  ];

  const proxiedColumns: Column<ReadysetProxiedQuery>[] = [
    { key: "query_id", label: "Query ID", primary: true, width: 156, minWidth: 130, sort: (r) => r.query_id, render: (r) => <code className="proxysql-code">{r.query_id}</code> },
    { key: "count", label: "Proxied", num: true, sort: (r) => num(r.count), render: (r) => numCell(r.count) },
    {
      key: "shallow",
      label: "Shallow",
      minWidth: 118,
      sort: (r) => r.shallow_support,
      render: (r) => <StatusPill tone={supportTone(r.shallow_support)} label={supportWord(r.shallow_support)} title={r.shallow_reason || undefined} quiet />,
    },
    {
      key: "deep",
      label: "Deep",
      minWidth: 118,
      sort: (r) => r.deep_support,
      render: (r) => <StatusPill tone={supportTone(r.deep_support)} label={supportWord(r.deep_support)} title={r.deep_reason || undefined} quiet />,
    },
    { key: "up_p50", label: "Upstream p50", num: true, sort: (r) => num(r.upstream_stats?.p50_ms), render: (r) => ms(r.upstream_stats?.p50_ms) },
    { key: "up_p99", label: "Upstream p99", num: true, sort: (r) => num(r.upstream_stats?.p99_ms), render: (r) => ms(r.upstream_stats?.p99_ms) },
    { key: "query", label: "Query", flex: true, sort: (r) => r.query, render: (r) => <code className="proxysql-code">{r.query}</code> },
  ];

  const tableColumns: Column<ReadysetTable>[] = [
    { key: "table", label: "Table", primary: true, width: 240, minWidth: 160, sort: (r) => r.table, render: (r) => <code className="proxysql-code">{r.table}</code> },
    {
      key: "status",
      label: "Status",
      minWidth: 150,
      sort: (r) => r.status,
      render: (r) => <ClusterMark tone={tableTone(r.bucket)} label={r.status} />,
    },
    { key: "description", label: "Details", flex: true, sort: (r) => r.description || "", render: (r) => r.description || "—" },
  ];

  const upstreamColumns: Column<ReadysetUpstream>[] = [
    { key: "address", label: "Upstream server", primary: true, width: 200, minWidth: 150, sort: (r) => r.address, render: (r) => <code className="proxysql-code">{r.address}</code> },
    {
      key: "purpose",
      label: "Purpose",
      minWidth: 140,
      sort: (r) => r.purpose,
      render: (r) => `${r.purpose || "proxy"}${r.active_cdc ? " (active CDC)" : ""}`,
    },
    {
      key: "health",
      label: "Health",
      minWidth: 110,
      sort: (r) => r.health || "",
      render: (r) => {
        const h = (r.health || "").toLowerCase();
        const t: Tone = h === "up" ? "ok" : h === "down" ? (r.active_cdc ? "problem" : "watch") : "unknown";
        return <StatusPill tone={t} label={(r.health || "unknown").toUpperCase()} quiet />;
      },
    },
    { key: "connections", label: "Pool conn", num: true, sort: (r) => num(r.connections), render: (r) => numCell(r.connections) },
    { key: "database", label: "Database", flex: true, sort: (r) => r.database || "", render: (r) => r.database || "—" },
  ];

  const connColumns: Column<ReadysetClientConnection>[] = [
    { key: "client_addr", label: "Client address", primary: true, width: 200, sort: (r) => r.client_addr || "", render: (r) => <code className="proxysql-code">{r.client_addr || "—"}</code> },
    { key: "username", label: "User", width: 160, sort: (r) => r.username || "", render: (r) => r.username || "—" },
    { key: "upstream", label: "Upstream target", flex: true, sort: (r) => r.upstream || "", render: (r) => <code className="proxysql-code">{r.upstream || "—"}</code> },
  ];

  const selectedCache = tab === "caches" && isRs ? report!.caches.find((c) => `cache:${c.name}` === selection.key) : undefined;
  const selectedProxied = tab === "proxied" && isRs ? report!.proxied_queries.find((p) => `pq:${p.query_id}` === selection.key) : undefined;
  const selectedTable = tab === "tables" && isRs ? report!.tables.find((t) => `tbl:${t.table}` === selection.key) : undefined;
  const selectedUpstream = useMemo(() => {
    if (tab !== "topology" || !selection.key || !isRs) return null;
    const topoUp = report!.topology?.upstreams.find((u) => u.key === selection.key) || null;
    const rawUp = report!.upstreams.find((u) => `upstream:${u.host}:${u.port}` === selection.key) || null;
    return topoUp || rawUp ? { topo: topoUp, raw: rawUp } : null;
  }, [tab, selection.key, isRs, report]);

  const inspectorContent: InspectorContent | null = !isRs
    ? null
    : selectedCache
      ? {
          key: `cache:${selectedCache.name}`,
          title: selectedCache.name,
          meta: (
            <Pill tone={selectedCache.cache_type === "shallow" ? "info" : "neutral"}>
              {selectedCache.cache_type === "shallow" ? "Shallow cache" : "Deep cache"} · {selectedCache.query_id}
            </Pill>
          ),
          actions: (
            <div className="review-actions" role="group" aria-label={`Changes for ${selectedCache.name}`}>
              {selectedCache.cache_type === "shallow" && (
                <button
                  type="button"
                  className="button"
                  title={changeBlock || "Discard cached entries so the next read refreshes from upstream."}
                  aria-disabled={!!changeBlock || actions.busy || undefined}
                  onClick={() => {
                    if (changeBlock || actions.busy) return;
                    void actions.review(`flush_cache:${selectedCache.name}`, (c) =>
                      planReadysetAction(c, "flush_cache", { kind: "cache", name: selectedCache.name, query_id: selectedCache.query_id }),
                    );
                  }}
                >
                  Flush entries…
                </button>
              )}
              <button
                type="button"
                className="button danger"
                title={changeBlock || "Drop this Readyset cache."}
                aria-disabled={!!changeBlock || actions.busy || undefined}
                onClick={() => {
                  if (changeBlock || actions.busy) return;
                  void actions.review(`drop_cache:${selectedCache.name}`, (c) =>
                    planReadysetAction(c, "drop_cache", { kind: "cache", name: selectedCache.name, query_id: selectedCache.query_id }),
                  );
                }}
              >
                Drop cache…
              </button>
              {changeBlock && <p className="hint">{changeBlock}</p>}
            </div>
          ),
          body: (
            <>
              {selectedCache.query && (
                <InspectorSection title="Cached SQL">
                  <CopySql sql={selectedCache.query} label="Cached query" />
                </InspectorSection>
              )}
              <InspectorSection title="Configuration">
                <Fields
                  rows={[
                    ["Query ID", <code>{selectedCache.query_id}</code>],
                    ["Type", selectedCache.cache_type],
                    ["Database", selectedCache.database || "Default"],
                    ["Properties", selectedCache.properties || "Default"],
                    selectedCache.ttl_ms !== null && ["TTL", `${(selectedCache.ttl_ms / 1000).toLocaleString()} s`],
                    selectedCache.refresh_ms !== null && ["Refresh period", `${(selectedCache.refresh_ms / 1000).toLocaleString()} s`],
                    selectedCache.coalesce_ms !== null && ["Coalesce window", `${selectedCache.coalesce_ms} ms`],
                  ]}
                />
              </InspectorSection>
              {selectedCache.cache_type === "shallow" && (
                <InspectorSection title="Shallow cache efficiency">
                  <Fields
                    rows={[
                      ["Hit rate", selectedCache.hit_rate_pct !== null ? `${selectedCache.hit_rate_pct}%` : "—"],
                      ["Hits / Misses", `${fmtCount(selectedCache.hits)} / ${fmtCount(selectedCache.misses)}`],
                      ["Background refreshes", fmtCount(selectedCache.refreshes)],
                      ["Wasted / Dropped", `${fmtCount(selectedCache.wasted_refreshes)} / ${fmtCount(selectedCache.dropped_refreshes)}`],
                      ["Coalesced waits", fmtCount(selectedCache.coalesces)],
                      ["Upstream time saved", ms(selectedCache.upstream_saved_ms)],
                    ]}
                  />
                </InspectorSection>
              )}
              {statsSection("Cached execution latency", selectedCache.cached_stats)}
              {statsSection("Upstream fallback latency", selectedCache.upstream_stats)}
            </>
          ),
        }
      : selectedProxied
        ? {
            key: `pq:${selectedProxied.query_id}`,
            title: selectedProxied.query_id,
            meta: (
              <StatusPill
                tone={selectedProxied.already_cached ? "ok" : "info"}
                label={selectedProxied.already_cached ? "Already cached" : "Uncached proxied query"}
              />
            ),
            actions: (
              <div className="review-actions" role="group" aria-label={`Actions for ${selectedProxied.query_id}`}>
                <button
                  type="button"
                  className="button primary"
                  title={createDisabledWhy || "Configure and review CREATE CACHE for this query."}
                  aria-disabled={!canCreate || undefined}
                  onClick={() => {
                    if (!canCreate) return;
                    const preferred: "shallow" | "deep" =
                      selectedProxied.shallow_support === "yes"
                        ? "shallow"
                        : selectedProxied.deep_support === "yes"
                          ? "deep"
                          : "shallow";
                    openCreateCache({ query_or_id: selectedProxied.query_id, cache_type: preferred });
                  }}
                >
                  Create cache…
                </button>
                {createDisabledWhy && <p className="hint">{createDisabledWhy}</p>}
              </div>
            ),
            body: (
              <>
                <InspectorSection title="Observed SQL">
                  <CopySql sql={selectedProxied.query} label="Observed SQL" />
                </InspectorSection>
                <InspectorSection title="Cacheability">
                  <Fields
                    rows={[
                      ["Proxied executions", fmtCount(selectedProxied.count)],
                      [
                        "Shallow cache",
                        `${supportWord(selectedProxied.shallow_support)}${selectedProxied.shallow_reason ? ` — ${selectedProxied.shallow_reason}` : ""}`,
                      ],
                      [
                        "Deep cache",
                        `${supportWord(selectedProxied.deep_support)}${selectedProxied.deep_reason ? ` — ${selectedProxied.deep_reason}` : ""}`,
                      ],
                    ]}
                  />
                </InspectorSection>
                {statsSection("Upstream execution latency", selectedProxied.upstream_stats)}
              </>
            ),
          }
        : selectedTable
          ? {
              key: `tbl:${selectedTable.table}`,
              title: selectedTable.table,
              meta: <ClusterMark tone={tableTone(selectedTable.bucket)} label={selectedTable.status} />,
              actions: (
                <div className="review-actions" role="group" aria-label={`Actions for ${selectedTable.table}`}>
                  <button
                    type="button"
                    className="button danger"
                    title={changeBlock || "Re-snapshot this base table from the upstream primary."}
                    aria-disabled={!!changeBlock || actions.busy || undefined}
                    onClick={() => {
                      if (changeBlock || actions.busy) return;
                      void actions.review(`resnapshot:${selectedTable.table}`, (c) =>
                        planReadysetAction(c, "resnapshot_table", { kind: "table", table: selectedTable.table }),
                      );
                    }}
                  >
                    Re-snapshot table…
                  </button>
                  {changeBlock && <p className="hint">{changeBlock}</p>}
                </div>
              ),
              body: (
                <InspectorSection title="Replication state">
                  <Fields
                    rows={[
                      ["Table", <code>{selectedTable.table}</code>],
                      ["Status", selectedTable.status],
                      ["Bucket", selectedTable.bucket],
                      selectedTable.description && ["Details", selectedTable.description],
                    ]}
                  />
                </InspectorSection>
              ),
            }
          : selectedUpstream
            ? (() => {
                const t = selectedUpstream.topo;
                const r = selectedUpstream.raw;
                const addr = t?.address || r?.address || selection.key;
                return {
                  key: selection.key,
                  title: t?.profile_name || addr,
                  meta: <span>Upstream database · {addr}</span>,
                  actions:
                    t?.profile_id && onSwitchConnection ? (
                      <div className="review-actions">
                        <button type="button" className="button" onClick={() => onSwitchConnection(t.profile_id!)}>
                          Use {t.profile_name || addr}
                        </button>
                      </div>
                    ) : undefined,
                  body: (
                    <InspectorSection title="Upstream connection">
                      <Fields
                        rows={[
                          ["Endpoint", <code>{addr}</code>],
                          ["Purpose", t?.purpose || r?.purpose || "proxy"],
                          ["Active CDC", (t?.active_cdc ?? r?.active_cdc) ? "Yes" : "No"],
                          ["Health", (t?.health || r?.health || "unknown").toUpperCase()],
                          ["Pool connections", fmtCount(t?.connections ?? r?.connections)],
                          ["Database", t?.database || r?.database || "—"],
                          t?.product && ["Verified engine", `${t.product} ${t.version || ""}`.trim()],
                          t?.evidence && ["Verification", t.evidence],
                        ]}
                      />
                    </InspectorSection>
                  ),
                };
              })()
            : null;

  const submitPrompt = () => {
    if (!prompt || changeBlock || actions.busy) return;
    const p = prompt;
    setPrompt(null);
    if (p.kind === "log_level") {
      void actions.review("set_log_level", (c) =>
        planReadysetAction(c, "set_log_level", null, { directives: p.value.trim() }),
      );
    } else if (p.kind === "eviction_memory") {
      const limitBytes = Math.round(Number(p.mb) * 1024 * 1024);
      void actions.review("set_eviction_memory_limit", (c) =>
        planReadysetAction(c, "set_eviction_memory_limit", null, { bytes: limitBytes }),
      );
    } else if (p.kind === "eviction_period") {
      void actions.review("set_eviction_period", (c) =>
        planReadysetAction(c, "set_eviction_period", null, { period_ms: Number(p.ms) }),
      );
    } else if (p.kind === "allowlist") {
      void actions.review(`allow:${p.allow_kind}:${p.name}`, (c) =>
        planReadysetAction(
          c,
          "add_shallow_allowed",
          { allow_kind: p.allow_kind },
          { allow_kind: p.allow_kind, name: p.name.trim() },
        ),
      );
    } else if (p.kind === "add_tables") {
      void actions.review("add_tables", (c) =>
        planReadysetAction(c, "add_tables", null, { tables: p.tables }),
      );
    } else if (p.kind === "replication_position") {
      void actions.review("set_replication_position", (c) =>
        planReadysetAction(c, "set_replication_position", null, { position: p.position.trim() }),
      );
    } else if (p.kind === "drop_all_caches") {
      const act =
        p.scope === "shallow"
          ? "drop_all_shallow_caches"
          : p.scope === "deep"
            ? "drop_all_deep_caches"
            : "drop_all_caches";
      void actions.review(act, (c) => planReadysetAction(c, act, null, p.scope));
    }
  };

  const promptValid = useMemo(() => {
    if (!prompt) return false;
    if (prompt.kind === "log_level") return prompt.value.trim().length > 0 && prompt.value.trim().length <= 256;
    if (prompt.kind === "eviction_memory") {
      const n = Number(prompt.mb);
      return prompt.mb.trim() !== "" && Number.isFinite(n) && n >= 0 && n <= 1048576;
    }
    if (prompt.kind === "eviction_period") {
      const n = Number(prompt.ms);
      return Number.isInteger(n) && n >= 1 && n <= 86400000;
    }
    if (prompt.kind === "allowlist") return /^[A-Za-z0-9_$@.-]{1,128}$/.test(prompt.name.trim());
    if (prompt.kind === "add_tables") return prompt.tables.trim().length > 0;
    if (prompt.kind === "replication_position") return prompt.position.trim().length > 0;
    if (prompt.kind === "drop_all_caches") return true;
    return false;
  }, [prompt]);

  const failed = current ? current.message.split("\n")[0] : reads.error && !onFailure ? reads.error : "";
  const shownError = actions.error || failed || exportError;
  const tone: Tone | undefined = !report ? undefined : !report.is_readyset ? "unknown" : (report.status as Tone);
  const shownResult = actions.result && actions.result.key === reads.key ? actions.result : null;

  return (
    <ViewLayout
      id="readyset"
      title="Readyset"
      detailBackLabel=""
      className={`cluster-view readyset-view${failedOver ? " is-stale" : ""}`}
      onRefresh={() => {
        void load(true);
      }}
      refreshDisabled={disabled}
      loading={!!reads.loading}
      otherConnection={other}
      inspector={inspectorContent}
      inspectorEmpty={<p>Select a cache, proxied query, table or upstream server to inspect its details and actions.</p>}
      controls={
        isRs ? (
          <Segmented<ReadysetTab>
            label="Readyset sections"
            value={tab}
            onChange={onTab}
            options={[
              { value: "topology", label: "Topology", title: "Multi-tier routing and CDC topology across ProxySQL, Readyset and MySQL/MariaDB" },
              { value: "caches", label: "Caches", title: "Active shallow and deep caches", count: report!.caches.length },
              { value: "proxied", label: "Proxied", title: "Queries observed passing through Readyset", count: report!.proxied_queries.length },
              { value: "tables", label: "Tables", title: "Replicated base tables and re-snapshot controls", count: report!.tables.length },
              { value: "parameters", label: "Parameters", title: "Dynamic runtime parameters, allowlists and lifecycle" },
            ]}
          />
        ) : undefined
      }
      info={
        <>
          <p>
            <strong>Readyset</strong> sits between your application or <strong>ProxySQL</strong> and your upstream{" "}
            <strong>MySQL or MariaDB</strong> database, serving shallow TTL caches and incrementally maintained deep
            streaming caches over the MySQL wire protocol (SQL port 3307, admin SQL port 6036).
          </p>
          <p>
            Opening Readyset only reads status and virtual telemetry tables. Every cache DDL, replication recovery or{" "}
            <code>ALTER READYSET</code> parameter change shows the exact statement first, checks your write policy, and
            records its outcome in the Audit log.
          </p>
        </>
      }
      status={
        <>
          <ViewStatus
            loading={reads.loading || (actions.planning ? "Reading the target for review…" : undefined)}
            error={shownError}
            onStop={reads.loading ? reads.stop : undefined}
            onRetry={failed && !actions.error ? () => { void load(true); } : undefined}
            doneAt={reads.doneAt}
          />
          {shownResult && (
            <ActionResult
              shown={shownResult}
              onClose={actions.clearResult}
              onNext={(nextAction) => {
                actions.clearResult();
                void actions.review(nextAction, (c) => planReadysetAction(c, nextAction, null));
              }}
            />
          )}
          {actions.sheet}
        </>
      }
    >
      {report ? (
        <>
          {failedOver && (
            <div className="gate other-connection cluster-stale" role="note">
              <p>The read since failed; this is the last report. Refresh to try again.</p>
            </div>
          )}

          <SummaryLine
            tone={tone}
            toneLabel={!report.is_readyset ? "Not Readyset" : report.status === "unknown" ? "Not judged" : undefined}
            notes={report.notes.length ? report.notes : undefined}
            notesTitle="Notes: what could not be read or matched"
          >
            {summaryText(report)}
          </SummaryLine>

          {!report.is_readyset ? (
            <EmptyState
              icon="readyset"
              title="Not a Readyset instance"
              action={
                /proxysql/i.test(report.version || "") && onOpenProxySql ? (
                  <ToolbarButton primary onClick={onOpenProxySql}>
                    Open ProxySQL
                  </ToolbarButton>
                ) : onOpenServer ? (
                  <ToolbarButton primary onClick={onOpenServer}>
                    Open Server
                  </ToolbarButton>
                ) : undefined
              }
            >
              {report.message || "Connect to Readyset's SQL port (3307 by default) or admin SQL port (6036)."}
            </EmptyState>
          ) : (
            <>
              {report.status !== "ok" && report.status_why.length > 0 && (
                <ul className="cluster-findings readyset-findings" aria-label="Findings">
                  {report.status_why.map((line, i) => {
                    const id = `${findingIdBase}-${i}`;
                    const open = expandedWhy.includes(line);
                    const ftone: Tone =
                      report.status === "problem" && /unreachable|unavailable|error|down/i.test(line) ? "problem" : "watch";
                    return (
                      <li key={line} className={open ? "is-open" : undefined}>
                        <span className="cluster-finding-head">
                          <ClusterMark tone={ftone} label={ftone === "problem" ? "Problem" : "Watch"} />
                          <span className="cluster-finding-title">{line}</span>
                          <button
                            type="button"
                            className="text-button cluster-finding-toggle"
                            aria-expanded={open}
                            aria-controls={id}
                            onClick={() =>
                              setExpandedWhy((cur) => (cur.includes(line) ? cur.filter((x) => x !== line) : [...cur, line]))
                            }
                          >
                            {open ? "Hide details" : "Details"}
                          </button>
                        </span>
                        <p id={id} className="cluster-finding-text" hidden={!open}>
                          Reported by SHOW READYSET STATUS and Readyset virtual telemetry tables on this listener.
                        </p>
                      </li>
                    );
                  })}
                </ul>
              )}

              <section className="cluster-tiles readyset-tiles" aria-label="Readyset summary">
                <TileGrid label="Readyset summary">
                  <MetricTile
                    rowKey="cached-share"
                    label="Cached share"
                    value={report.telemetry.cached_share_pct !== null ? String(report.telemetry.cached_share_pct) : "—"}
                    unit={report.telemetry.cached_share_pct !== null ? "%" : undefined}
                    trend={
                      <span className="tile-hint">
                        {fmtCount(report.telemetry.cached_executions)} cached · {fmtCount(report.telemetry.upstream_executions)} upstream
                      </span>
                    }
                  />
                  <MetricTile
                    rowKey="active-caches"
                    label="Active caches"
                    value={report.caches.length.toLocaleString()}
                    trend={<span className="tile-hint">{shallowCount} shallow · {deepCount} deep</span>}
                  />
                  <MetricTile
                    rowKey="shallow-hit-rate"
                    label="Shallow hit rate"
                    value={report.telemetry.shallow_hit_rate_pct !== null ? String(report.telemetry.shallow_hit_rate_pct) : "—"}
                    unit={report.telemetry.shallow_hit_rate_pct !== null ? "%" : undefined}
                    trend={
                      <span className="tile-hint">
                        {fmtCount(report.telemetry.shallow_hits)} hits · {fmtCount(report.telemetry.shallow_refreshes)} refreshes
                      </span>
                    }
                  />
                  <MetricTile
                    rowKey="replication"
                    label="Base replication"
                    value={report.replication?.status || "Unknown"}
                    tone={
                      report.replication?.status === "Error"
                        ? "problem"
                        : report.replication?.status === "Stopped" ||
                            (report.replication?.status === "Disabled" && deepCount > 0)
                          ? "watch"
                          : undefined
                    }
                    trend={
                      <span className="tile-hint">
                        {report.replication?.consume_lag !== null && report.replication?.consume_lag !== undefined
                          ? `lag ${report.replication.consume_lag.toLocaleString()} ${report.replication.lag_unit || "events"}`
                          : `${report.table_counts.snapshotted}/${report.table_counts.total} tables ready`}
                      </span>
                    }
                  />
                </TileGrid>
              </section>

              {tab === "topology" && (
                <div className="readyset-tab-body">
                  <ReadysetTopologyDiagram
                    report={report}
                    current={!stale}
                    selectedKey={selection.key || null}
                    onSelectUpstream={(k) => selection.select(selection.key === k ? "" : k, selection.key !== k)}
                    onSwitchConnection={!stale ? onSwitchConnection : undefined}
                    onOpenProxySql={onOpenProxySql}
                    onOpenCluster={onOpenCluster}
                    onOpenReplication={onOpenReplication}
                  />

                  {report.upstreams.length > 0 && (
                    <div className="readyset-section-block">
                      <h3 className="readyset-section-heading">Upstream database servers</h3>
                      <DataTable<ReadysetUpstream>
                        key="readyset.upstreams"
                        id="readyset.upstreams"
                        label="Upstream database servers"
                        columns={upstreamColumns}
                        rows={report.upstreams}
                        rowKey={(r) => `upstream:${r.host}:${r.port}`}
                        selection={selection}
                      />
                    </div>
                  )}

                  {report.connections.length > 0 && (
                    <div className="readyset-section-block">
                      <h3 className="readyset-section-heading">Active client connections</h3>
                      <DataTable<ReadysetClientConnection>
                        key="readyset.connections"
                        id="readyset.connections"
                        label="Readyset client connections"
                        columns={connColumns}
                        rows={report.connections}
                        rowKey={(r) => `conn:${r.client_addr || ""}:${r.username || ""}:${r.upstream || ""}`}
                      />
                    </div>
                  )}
                </div>
              )}

              {tab === "caches" && (
                <div className="readyset-tab-body">
                  <div className="readyset-action-strip">
                    <div className="readyset-strip-buttons">
                      <button
                        type="button"
                        className="button primary"
                        aria-disabled={!canCreate || undefined}
                        title={createDisabledWhy || undefined}
                        onClick={() => {
                          if (!canCreate) return;
                          openCreateCache();
                        }}
                      >
                        Create cache…
                      </button>
                      <button
                        type="button"
                        className="button"
                        disabled={exportBusy || !!other || disabled}
                        onClick={() => void runExport()}
                      >
                        {exportBusy ? "Exporting…" : "Export SQL"}
                      </button>
                      {shallowCount > 0 && (
                        <button
                          type="button"
                          className="button"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            void actions.review("flush_all_shallow_caches", (c) =>
                              planReadysetAction(c, "flush_all_shallow_caches"),
                            );
                          }}
                        >
                          Flush shallow caches…
                        </button>
                      )}
                      {report.caches.length > 0 && (
                        <button
                          type="button"
                          className="button danger"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            setPrompt({ kind: "drop_all_caches", scope: "all" });
                          }}
                        >
                          Drop caches…
                        </button>
                      )}
                    </div>
                    {createDisabledWhy && !report.capabilities.can_create_cache && (
                      <span className="readyset-gate-note">{createDisabledWhy}</span>
                    )}
                  </div>

                  <DataTable<ReadysetCache>
                    key="readyset.caches"
                    id="readyset.caches"
                    label="Readyset caches"
                    columns={cacheColumns}
                    rows={report.caches}
                    rowKey={(r) => `cache:${r.name}`}
                    selection={selection}
                    empty={
                      <EmptyState icon="readyset" title="No active caches">
                        Create a shallow TTL cache or a deep streaming cache, or pick an observed query from Proxied.
                      </EmptyState>
                    }
                  />
                </div>
              )}

              {tab === "proxied" && (
                <div className="readyset-tab-body">
                  <div className="readyset-action-strip">
                    <div className="readyset-strip-filters">
                      <SearchField
                        value={proxiedQuerySearch}
                        onChange={setProxiedQuerySearch}
                        placeholder="Filter SQL or query ID…"
                        label="Filter proxied queries"
                      />
                      <label className="readyset-check is-inline">
                        <input
                          type="checkbox"
                          checked={uncachedOnly}
                          onChange={(e) => setUncachedOnly(e.target.checked)}
                        />
                        <span>Uncached only</span>
                      </label>
                    </div>
                    <div className="readyset-strip-buttons">
                      <button
                        type="button"
                        className="button primary"
                        aria-disabled={!canCreate || undefined}
                        title={createDisabledWhy || undefined}
                        onClick={() => {
                          if (!canCreate) return;
                          openCreateCache();
                        }}
                      >
                        Create cache…
                      </button>
                      {report.proxied_queries.length > 0 && (
                        <button
                          type="button"
                          className="button"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            void actions.review("drop_all_proxied_queries", (c) =>
                              planReadysetAction(c, "drop_all_proxied_queries"),
                            );
                          }}
                        >
                          Clear proxied queries…
                        </button>
                      )}
                    </div>
                  </div>

                  <DataTable<ReadysetProxiedQuery>
                    key="readyset.proxied"
                    id="readyset.proxied"
                    label="Proxied queries"
                    columns={proxiedColumns}
                    rows={filteredProxied}
                    rowKey={(r) => `pq:${r.query_id}`}
                    selection={selection}
                    empty={
                      <EmptyState icon="readyset" title="No matching proxied queries">
                        {report.proxied_queries.length === 0
                          ? "Readyset has not recorded uncached proxied queries yet."
                          : "Adjust the search text or uncheck Uncached only."}
                      </EmptyState>
                    }
                  />
                </div>
              )}

              {tab === "tables" && (
                <div className="readyset-tab-body">
                  <div className="readyset-repl-bar" aria-label="Readyset replication controls">
                    <div className="readyset-repl-meta">
                      <span>
                        <strong>Replication:</strong> {report.replication?.status || "Unknown"}
                      </span>
                      {report.replication?.mode && (
                        <span>
                          <strong>Mode:</strong> <code className="proxysql-code">{report.replication.mode}</code>
                        </span>
                      )}
                      {report.replication?.consume_lag !== null && report.replication?.consume_lag !== undefined && (
                        <span>
                          <strong>Consume lag:</strong> {report.replication.consume_lag.toLocaleString()}{" "}
                          {report.replication.lag_unit || "events"}
                        </span>
                      )}
                      {report.replication?.heartbeat_staleness_s !== null &&
                        report.replication?.heartbeat_staleness_s !== undefined && (
                          <span>
                            <strong>Heartbeat:</strong> {Math.round(report.replication.heartbeat_staleness_s)} s old
                          </span>
                        )}
                    </div>
                    <div className="readyset-strip-buttons">
                      {report.replication?.status === "Stopped" ? (
                        <button
                          type="button"
                          className="button primary"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            void actions.review("start_replication", (c) => planReadysetAction(c, "start_replication"));
                          }}
                        >
                          Start replication…
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="button"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            void actions.review("stop_replication", (c) => planReadysetAction(c, "stop_replication"));
                          }}
                        >
                          Stop replication…
                        </button>
                      )}
                      <button
                        type="button"
                        className="button"
                        aria-disabled={!!changeBlock || actions.busy || undefined}
                        title={changeBlock || undefined}
                        onClick={() => {
                          if (changeBlock || actions.busy) return;
                          setPrompt({
                            kind: "replication_position",
                            position: report.replication?.replicator_offset || "",
                          });
                        }}
                      >
                        Set position…
                      </button>
                      <button
                        type="button"
                        className="button"
                        aria-disabled={!!changeBlock || actions.busy || undefined}
                        title={changeBlock || undefined}
                        onClick={() => {
                          if (changeBlock || actions.busy) return;
                          setPrompt({ kind: "add_tables", tables: "" });
                        }}
                      >
                        Add tables…
                      </button>
                    </div>
                  </div>

                  <DataTable<ReadysetTable>
                    key="readyset.tables"
                    id="readyset.tables"
                    label="Replicated base tables"
                    columns={tableColumns}
                    rows={report.tables}
                    rowKey={(r) => `tbl:${r.table}`}
                    selection={selection}
                    empty={
                      <EmptyState icon="readyset" title="No replicated base tables">
                        Add upstream tables or check SHOW READYSET ALL TABLES.
                      </EmptyState>
                    }
                  />
                </div>
              )}

              {tab === "parameters" && (
                <div className="readyset-tab-body readyset-parameters">
                  <GroupedList id="rs-ops-group" title="Operational state & credentials">
                    <GroupedRow
                      rowKey="maintenance"
                      wrap
                      tone={report.status_fields.status === "Maintenance Mode" ? "watch" : "ok"}
                      toneLabel={report.status_fields.status === "Maintenance Mode" ? "Active" : "Normal"}
                      label="Maintenance mode"
                      detail={
                        report.status_fields.status === "Maintenance Mode"
                          ? "Bypasses caches and proxies queries directly to upstream"
                          : "Serves eligible queries from shallow and deep caches"
                      }
                      value={
                        report.status_fields.status === "Maintenance Mode" ? (
                          <button
                            type="button"
                            className="button primary"
                            aria-disabled={!!changeBlock || actions.busy || undefined}
                            title={changeBlock || undefined}
                            onClick={() => {
                              if (changeBlock || actions.busy) return;
                              void actions.review("exit_maintenance_mode", (c) =>
                                planReadysetAction(c, "exit_maintenance_mode"),
                              );
                            }}
                          >
                            Exit maintenance mode…
                          </button>
                        ) : (
                          <button
                            type="button"
                            className="button"
                            aria-disabled={!!changeBlock || actions.busy || undefined}
                            title={changeBlock || undefined}
                            onClick={() => {
                              if (changeBlock || actions.busy) return;
                              void actions.review("enter_maintenance_mode", (c) =>
                                planReadysetAction(c, "enter_maintenance_mode"),
                              );
                            }}
                          >
                            Enter maintenance mode…
                          </button>
                        )
                      }
                    />
                    <GroupedRow
                      rowKey="privileges"
                      wrap
                      label="Cached upstream privileges"
                      detail="Reload upstream MySQL/MariaDB user credentials and table ACLs immediately"
                      value={
                        <button
                          type="button"
                          className="button"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            void actions.review("flush_privileges", (c) => planReadysetAction(c, "flush_privileges"));
                          }}
                        >
                          Flush privileges…
                        </button>
                      }
                    />
                  </GroupedList>

                  <GroupedList id="rs-params-group" title="Dynamic runtime parameters (ALTER READYSET)">
                    <GroupedRow
                      rowKey="log_level"
                      wrap
                      label="Tracing log level"
                      detail="Runtime filter directives (for example info or info,readyset_adapter=debug)"
                      value={
                        <button
                          type="button"
                          className="button"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            setPrompt({ kind: "log_level", value: "info" });
                          }}
                        >
                          Set log level…
                        </button>
                      }
                    />
                    <GroupedRow
                      rowKey="eviction_memory"
                      wrap
                      label="Eviction memory limit"
                      detail={`Cache eviction threshold${report.status_fields.host_memory ? ` · Host memory ${report.status_fields.host_memory}` : ""}`}
                      value={
                        <button
                          type="button"
                          className="button"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            setPrompt({ kind: "eviction_memory", mb: "1024" });
                          }}
                        >
                          Set memory limit…
                        </button>
                      }
                    />
                    <GroupedRow
                      rowKey="eviction_period"
                      wrap
                      label="Eviction check period"
                      detail="Interval in milliseconds between Readyset cache eviction sweeps"
                      value={
                        <button
                          type="button"
                          className="button"
                          aria-disabled={!!changeBlock || actions.busy || undefined}
                          title={changeBlock || undefined}
                          onClick={() => {
                            if (changeBlock || actions.busy) return;
                            setPrompt({ kind: "eviction_period", ms: "1000" });
                          }}
                        >
                          Set eviction period…
                        </button>
                      }
                    />
                  </GroupedList>

                  <div className="readyset-allowlists" aria-label="Shallow cache allowlists">
                    <h3 className="readyset-section-heading">Shallow cache allowlists</h3>
                    {(
                      [
                        ["function", "Allowed SQL functions", report.shallow_allowlists.functions],
                        ["variable", "Allowed session/global variables", report.shallow_allowlists.variables],
                        ["schema", "Allowed schemas", report.shallow_allowlists.schemas],
                      ] as const
                    ).map(([kindKey, label, items]) => (
                      <div key={kindKey} className="readyset-allowlist-row">
                        <div className="readyset-allowlist-head">
                          <span className="readyset-allowlist-title">{label}</span>
                          <button
                            type="button"
                            className="button"
                            aria-disabled={!!changeBlock || actions.busy || undefined}
                            title={changeBlock || undefined}
                            onClick={() => {
                              if (changeBlock || actions.busy) return;
                              setPrompt({ kind: "allowlist", allow_kind: kindKey, name: "" });
                            }}
                          >
                            Allow {kindKey}…
                          </button>
                        </div>
                        {items.length === 0 ? (
                          <span className="readyset-allowlist-empty">None configured</span>
                        ) : (
                          <div className="readyset-allowlist-chips">
                            {items.map((item) => (
                              <span key={item} className="readyset-allow-chip">
                                <code className="proxysql-code">{item}</code>
                                <button
                                  type="button"
                                  className="text-button"
                                  aria-disabled={!!changeBlock || actions.busy || undefined}
                                  title={changeBlock || `Remove ${item}`}
                                  onClick={() => {
                                    if (changeBlock || actions.busy) return;
                                    void actions.review(`drop:${kindKey}:${item}`, (c) =>
                                      planReadysetAction(
                                        c,
                                        "drop_shallow_allowed",
                                        { kind: kindKey, allow_kind: kindKey, name: item },
                                        { allow_kind: kindKey, name: item },
                                      ),
                                    );
                                  }}
                                >
                                  Remove
                                </button>
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>

                  <div className="readyset-admin-lifecycle" aria-label="Admin SQL port lifecycle">
                    <h3 className="readyset-section-heading">Admin port lifecycle (port 6036)</h3>
                    <div className="readyset-strip-buttons">
                      <button
                        type="button"
                        className="button danger"
                        aria-disabled={!report.capabilities.can_shutdown || !!changeBlock || actions.busy || undefined}
                        title={
                          !report.capabilities.can_shutdown
                            ? "Connect to the Readyset admin SQL port (default 6036) to run SHUTDOWN."
                            : changeBlock || undefined
                        }
                        onClick={() => {
                          if (!report.capabilities.can_shutdown || changeBlock || actions.busy) return;
                          void actions.review("shutdown", (c) => planReadysetAction(c, "shutdown"));
                        }}
                      >
                        Shut down Readyset…
                      </button>
                      <button
                        type="button"
                        className="button danger"
                        aria-disabled={!report.capabilities.can_shutdown || !!changeBlock || actions.busy || undefined}
                        title={
                          !report.capabilities.can_shutdown
                            ? "Connect to the Readyset admin SQL port (default 6036) to run SHUTDOWN RESET."
                            : changeBlock || undefined
                        }
                        onClick={() => {
                          if (!report.capabilities.can_shutdown || changeBlock || actions.busy) return;
                          void actions.review("shutdown_reset", (c) => planReadysetAction(c, "shutdown_reset"));
                        }}
                      >
                        Shut down &amp; reset snapshot…
                      </button>
                    </div>
                    {!report.capabilities.can_shutdown && (
                      <p className="readyset-gate-note">
                        Connect to the Readyset admin SQL port (default 6036) to run SHUTDOWN or SHUTDOWN RESET.
                      </p>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </>
      ) : reads.loading ? null : failed ? (
        <EmptyState
          icon="readyset"
          title="Could not read Readyset"
          action={
            <ToolbarButton primary disabled={disabled} onClick={() => { void load(true); }}>
              Retry
            </ToolbarButton>
          }
        >
          {failed}
        </EmptyState>
      ) : (
        <EmptyState
          icon="readyset"
          title="Read Readyset"
          action={
            <ToolbarButton primary disabled={disabled} onClick={() => { void load(); }}>
              Load
            </ToolbarButton>
          }
        >
          The Readyset SQL or admin listener the connection in use reaches: topology, caches, telemetry and parameters.
        </EmptyState>
      )}

      {createDraft && (
        <CreateCacheSheet
          initial={createDraft}
          connection={connection}
          onClose={() => setCreateDraft(null)}
          onReview={(spec) => {
            setCreateDraft(null);
            void actions.review("create_cache", (c) => planReadysetAction(c, "create_cache", null, spec));
          }}
        />
      )}

      {prompt && (
        <ConfirmSheet
          className="ops-confirm action-value"
          band={<ConnectionTypeBand connection={connection} />}
          eyebrow="READYSET"
          title={
            prompt.kind === "log_level"
              ? "Set Readyset log level"
              : prompt.kind === "eviction_memory"
                ? "Set eviction memory limit"
                : prompt.kind === "eviction_period"
                  ? "Set eviction check period"
                  : prompt.kind === "allowlist"
                    ? `Allow shallow cache ${prompt.allow_kind}`
                    : prompt.kind === "add_tables"
                      ? "Add tables to Readyset replication"
                      : prompt.kind === "replication_position"
                        ? "Set replication position"
                        : "Drop Readyset caches"
          }
          confirmTone={prompt.kind === "drop_all_caches" ? "danger" : "primary"}
          confirmLabel="Review SQL…"
          confirmDisabled={!promptValid}
          onCancel={() => setPrompt(null)}
          onConfirm={submitPrompt}
        >
          {prompt.kind === "log_level" && (
            <label className="ops-field action-typed">
              <span>Tracing filter directives</span>
              <input
                type="text"
                value={prompt.value}
                onChange={(e) => setPrompt({ kind: "log_level", value: e.target.value })}
                placeholder="info,readyset_adapter=debug"
                spellCheck={false}
              />
            </label>
          )}
          {prompt.kind === "eviction_memory" && (
            <label className="ops-field action-typed">
              <span>Memory limit (MiB, converted to bytes)</span>
              <input
                type="number"
                min={0}
                step="any"
                value={prompt.mb}
                onChange={(e) => setPrompt({ kind: "eviction_memory", mb: e.target.value })}
              />
            </label>
          )}
          {prompt.kind === "eviction_period" && (
            <label className="ops-field action-typed">
              <span>Eviction check period (milliseconds)</span>
              <input
                type="number"
                min={1}
                value={prompt.ms}
                onChange={(e) => setPrompt({ kind: "eviction_period", ms: e.target.value })}
              />
            </label>
          )}
          {prompt.kind === "allowlist" && (
            <label className="ops-field action-typed">
              <span>{prompt.allow_kind.charAt(0).toUpperCase() + prompt.allow_kind.slice(1)} identifier</span>
              <input
                type="text"
                value={prompt.name}
                onChange={(e) =>
                  setPrompt({ kind: "allowlist", allow_kind: prompt.allow_kind, name: e.target.value })
                }
                placeholder={
                  prompt.allow_kind === "function"
                    ? "NOW"
                    : prompt.allow_kind === "variable"
                      ? "@@time_zone"
                      : "analytics"
                }
                spellCheck={false}
              />
            </label>
          )}
          {prompt.kind === "add_tables" && (
            <label className="ops-field action-typed">
              <span>Comma-separated table names (schema.table)</span>
              <input
                type="text"
                value={prompt.tables}
                onChange={(e) => setPrompt({ kind: "add_tables", tables: e.target.value })}
                placeholder="app.orders, app.order_items"
                spellCheck={false}
              />
            </label>
          )}
          {prompt.kind === "replication_position" && (
            <label className="ops-field action-typed">
              <span>GTID set or binlog file:offset</span>
              <input
                type="text"
                value={prompt.position}
                onChange={(e) => setPrompt({ kind: "replication_position", position: e.target.value })}
                placeholder="3E11FA47-71CA-11E1-9E33-C80AA9429562:1-42"
                spellCheck={false}
              />
            </label>
          )}
          {prompt.kind === "drop_all_caches" && (
            <label className="ops-field action-typed">
              <span>Which caches to drop</span>
              <select
                value={prompt.scope}
                onChange={(e) =>
                  setPrompt({
                    kind: "drop_all_caches",
                    scope: e.target.value as "all" | "shallow" | "deep",
                  })
                }
              >
                <option value="all">All caches ({report?.caches.length || 0})</option>
                {shallowCount > 0 && <option value="shallow">Shallow caches only ({shallowCount})</option>}
                {deepCount > 0 && <option value="deep">Deep caches only ({deepCount})</option>}
              </select>
            </label>
          )}
          <p className="hint">Nothing is sent yet: the next step shows the exact SQL statement for review.</p>
        </ConfirmSheet>
      )}

      {exportedSql && <ExportCachesSheet exported={exportedSql} onClose={() => setExportedSql(null)} />}
    </ViewLayout>
  );
}

export default ReadysetPlace;
