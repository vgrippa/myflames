// The Readyset place: multi-tier topology (ProxySQL -> Readyset -> MySQL/MariaDB),
// active shallow & deep caches with EXPLAIN CREATE CACHE dry-run and DDL export,
// proxied query analysis, base-table CDC replication & re-snapshotting, and
// dynamic runtime parameters & shallow cache allowlists. Every write goes
// through ActionReview.tsx (dbops + reviewed_writes + Audit log).
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { download, type Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { ConnectionTypeBand, useWritePolicy } from "./Connections";
import { ConnectionGroupBar } from "./ConnectionGroupBar";
import { ActionResult, useReviewedActions } from "./ActionReview";
import { ActionGroup } from "./CatalogActions";
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
  cachesReadFailed,
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
  type ReadysetAction,
  type ReadysetFinding,
  type ReadysetProxiedQuery,
  type ReadysetQueryStats,
  type ReadysetReport,
  type ReadysetSupportVerdict,
  type ReadysetTable,
} from "./readysetApi";
import { ReadysetTopologyDiagram } from "./ReadysetTopology";
import { matchEvidenceLabel } from "./TopologyTiers";
import { useOpenRead } from "./viewOnScreen";
import "./cluster.css";
import "./readyset.css";

export type ReadysetTab = "topology" | "caches" | "proxied" | "tables" | "parameters";
const SHEET_WIDTH = 1100;

/** The tab label a finding's "Show in <Tab>" button names and switches to. */
const FINDING_TAB_LABELS: Record<ReadysetFinding["tab"], string> = {
  topology: "Topology",
  caches: "Caches",
  proxied: "Proxied",
  tables: "Tables",
  parameters: "Parameters",
};

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
  | { kind: "replication_position"; position: string };

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
    report.port_role === "admin" ? "admin SQL port" : report.port_role === null ? "port not read" : "SQL port",
    // A failed SHOW CACHES read leaves an empty list: not read, never zero.
    cachesReadFailed(report) ? "caches not read" : plural(report.caches.length, "cache"),
  ];
  if (report.telemetry.cached_share_pct !== null) {
    parts.push(`${report.telemetry.cached_share_pct}% cached`);
  }
  if (report.replication?.status && report.replication.status !== "Unknown") {
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
  submitBlock = "",
}: {
  initial: CreateDraft;
  connection: Connection;
  onClose: () => void;
  onReview: (spec: Record<string, unknown>) => void;
  /** Why the catalog entry for create_cache is not usable right now (the
   * client block or its why_not/fix); re-checked live against the report. */
  submitBlock?: string;
}) {
  const [draft, setDraft] = useState<CreateDraft>(initial);
  const [checking, setChecking] = useState(false);
  const [explain, setExplain] = useState<ReadysetExplainResponse | null>(null);
  const [checkError, setCheckError] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  // Each check is bound to the draft text and the connection it started with:
  // a stale response (an older request, an edited draft, another connection)
  // is discarded and never clears a newer request's state.
  const checkSeqRef = useRef(0);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const connectionRef = useRef(connection);
  connectionRef.current = connection;

  const runCheck = useCallback(
    async (queryText: string) => {
      const q = queryText.trim();
      if (!q || submitBlock) return;
      const seq = ++checkSeqRef.current;
      abortRef.current?.abort();
      const ctl = new AbortController();
      abortRef.current = ctl;
      const conn = connection;
      setChecking(true);
      setCheckError("");
      try {
        const res = await explainReadysetCache(conn, q, "both", ctl.signal);
        if (ctl.signal.aborted || seq !== checkSeqRef.current) return;
        if (conn !== connectionRef.current || draftRef.current.query_or_id.trim() !== q) return;
        setExplain(res);
        if (res.shallow?.supported === "unsupported" && res.deep?.supported === "yes") {
          setDraft((d) => ({ ...d, cache_type: "deep" }));
        } else if (res.deep?.supported === "unsupported" && res.shallow?.supported === "yes") {
          setDraft((d) => ({ ...d, cache_type: "shallow" }));
        }
      } catch (e) {
        if (!ctl.signal.aborted && seq === checkSeqRef.current) setCheckError(e instanceof Error ? e.message : String(e));
      } finally {
        // An aborted or superseded request never clears a newer request's
        // state; an abort with no newer request still clears checking.
        if (seq === checkSeqRef.current) setChecking(false);
      }
    },
    [connection, submitBlock],
  );

  // Check the current draft when the sheet opens and when a block or the
  // connection changes: the visible draft is checked, never the query the
  // sheet opened with, and stale results are dropped first.
  useEffect(() => {
    setExplain(null);
    const q = draftRef.current.query_or_id.trim();
    if (q) void runCheck(q);
    return () => abortRef.current?.abort();
  }, [runCheck]);

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
    if (validationError || unsupportedChosen || submitBlock) return;
    const spec: Record<string, unknown> = {
      cache_type: draft.cache_type,
      query_or_id: draft.query_or_id.trim(),
      // Ordinary spaces only: a boundary control or format character (a tab,
      // U+FEFF) stays in the name so the backend's own refusal names it.
      name: draft.name.replace(/^ +| +$/g, "") || undefined,
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
      confirmDisabled={!!validationError || unsupportedChosen || !!submitBlock}
      onCancel={onClose}
      onConfirm={submit}
    >
      <div className="readyset-form">
        {submitBlock && <p className="ops-warning" role="alert">{submitBlock}</p>}
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
            disabled={!draft.query_or_id.trim() || checking || !!submitBlock}
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
            {draft.cache_type === "shallow" ? "Shallow" : "Deep"} cache note: <bdi>{chosenExplain.reason}</bdi>
          </p>
        )}
        {explain && explain.collisions.length > 0 && (
          <p className="ops-warning">
            Collides with existing cache <bdi>{explain.collisions.map((c) => c.name).join(", ")}</bdi>; Readyset replaces
            it when created.
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
  // The connection key does not carry the default database: a report read with
  // another one is stale like a report from another connection. The compare is
  // exact (empty or missing is null): " shop" is another database than "shop".
  // A report that is not a Readyset read no session database, so it is never
  // database-stale.
  const normDb = (v: string | null | undefined) => v || null;
  const otherDatabase = !other && !!report && report.is_readyset && normDb(report.session_database) !== normDb(connection.database);
  const stale = !!other || failedOver || otherDatabase;

  const [proxiedQuerySearch, setProxiedQuerySearch] = useState("");
  const [uncachedOnly, setUncachedOnly] = useState(false);
  const [openFindings, setOpenFindings] = useState<Record<string, boolean>>({});
  const [createDraft, setCreateDraft] = useState<CreateDraft | null>(null);
  const [prompt, setPrompt] = useState<InputPromptState | null>(null);
  // An enabled allowlist Remove… points at the chips (they carry the item):
  // the kind that shows the hint, and each kind's first chip to focus.
  const [removeHint, setRemoveHint] = useState("");
  const firstChipRef = useRef<Record<string, HTMLButtonElement | null>>({});
  const [exportedSql, setExportedSql] = useState<ReadysetExportResponse | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState("");
  const readsKeyRef = useRef(reads.key);
  readsKeyRef.current = reads.key;
  const findingIdBase = useId();
  const allowIdBase = useId();

  useEffect(() => {
    // A connection switch rebinds everything the old report drove: open
    // sheets, prompts and finding toggles belong to the previous report.
    setExportedSql(null);
    setExportError("");
    setExportBusy(false);
    setCreateDraft(null);
    setPrompt(null);
    setRemoveHint("");
    setOpenFindings({});
  }, [reads.key]);

  useEffect(() => {
    // The report turned database-stale: an open sheet or prompt belongs to
    // the previous default database and closes with it.
    if (!otherDatabase) return;
    setCreateDraft(null);
    setPrompt(null);
    setRemoveHint("");
  }, [otherDatabase]);

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
    // A report read with another default database is stale: an open review
    // from before the change closes (recorded as declined) instead of acting.
    resetKey: otherDatabase ? "other-database" : "",
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
        : failedOver
          ? "The last read failed. Refresh first."
          : "This report was read with another default database. Refresh to read the connection in use."
      : disabled
        ? "Reads are paused."
        : policy?.blocked
          ? policy.blocked
          : connection.read_only
            ? "This connection is read-only in myflames, so changes are refused."
            : "";

  // The backend action catalog (readyset-1.0) gates every change: each area
  // renders its slice of report.actions (or a row's actions) through the
  // shared ActionGroup, always listed, disabled with its why_not and fix.
  const serverActions = useMemo(() => report?.actions || [], [report]);
  const catalogSlice = useCallback(
    (ids: string[]) => serverActions.filter((a) => ids.includes(a.id)),
    [serverActions],
  );
  const reason = (a: ReadysetAction) => [...new Set([a.why_not, a.fix].filter(Boolean))].join(" ");
  const createEntry = serverActions.find((a) => a.id === "create_cache") || null;
  const createBlock =
    changeBlock ||
    (isRs && !createEntry
      ? "Readyset did not offer CREATE CACHE in this report. Refresh first."
      : createEntry && !createEntry.enabled
        ? reason(createEntry)
        : "");

  /** One server-scope catalog action: prompt/sheet for the ones that need
   * input, a reviewed plan for the rest. The ActionGroup already refuses a
   * blocked or disabled entry; this re-checks before anything is planned. */
  function startServer(a: ReadysetAction) {
    if (changeBlock || actions.busy || !a.enabled) return;
    switch (a.id) {
      case "create_cache":
        openCreateCache();
        return;
      case "set_log_level":
        setPrompt({ kind: "log_level", value: "info" });
        return;
      case "set_eviction_memory_limit":
        setPrompt({ kind: "eviction_memory", mb: "1024" });
        return;
      case "set_eviction_period":
        setPrompt({ kind: "eviction_period", ms: "1000" });
        return;
      case "add_tables":
        setPrompt({ kind: "add_tables", tables: "" });
        return;
      case "set_replication_position":
        setPrompt({ kind: "replication_position", position: report?.replication?.replicator_offset || "" });
        return;
      default:
        // Scoped drops send their own ids with no value (the backend plans
        // drop_all_shallow_caches / drop_all_deep_caches from the id alone).
        void actions.review(a.id, (c) => planReadysetAction(c, a.id));
    }
  }

  /** The catalog entry behind an open input prompt, looked up in the current
   * report at every render: an entry that turned disabled (or vanished) since
   * the prompt opened blocks the submit and shows its reason instead. */
  function findPromptAction(p: InputPromptState): ReadysetAction | null {
    if (p.kind === "allowlist") {
      const key = p.allow_kind === "function" ? "functions" : p.allow_kind === "variable" ? "variables" : "schemas";
      return (report?.allowlist_actions?.[key] || []).find((a) => a.id === "add_shallow_allowed") || null;
    }
    const id =
      p.kind === "log_level"
        ? "set_log_level"
        : p.kind === "eviction_memory"
          ? "set_eviction_memory_limit"
          : p.kind === "eviction_period"
            ? "set_eviction_period"
            : p.kind === "add_tables"
              ? "add_tables"
              : "set_replication_position";
    return serverActions.find((a) => a.id === id) || null;
  }
  const promptAction = prompt ? findPromptAction(prompt) : null;
  const promptBlock = !prompt
    ? ""
    : changeBlock ||
      (!promptAction
        ? "Readyset did not offer this action in this report. Close and refresh."
        : !promptAction.enabled
          ? reason(promptAction)
          : "");

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
    if (exportBusy || stale || disabled) return;
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
  // A failed core read (S4) is never a zero and never an empty state.
  const cachesFailed = isRs && cachesReadFailed(report!);
  const tablesReadFailed = isRs && report!.findings.some((f) => f.id === "read_failed:tables");
  // A replicator error stands whatever the Replication Status word is: the
  // engine reports only Running, Stopped or Disabled there.
  const replErrorFinding = isRs ? report!.findings.find((f) => f.id === "replication_error") : undefined;
  const replErrored =
    !!replErrorFinding || report?.replication?.status === "Error" || !!report?.replication?.last_error;

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
            <ActionGroup
              list={selectedCache.actions || []}
              label={`Changes for ${selectedCache.name}`}
              blocked={changeBlock}
              planning={actions.planning}
              busy={actions.busy}
              onStart={(a) => {
                if (changeBlock || actions.busy || !a.enabled) return;
                void actions.review(a.id, (c) =>
                  planReadysetAction(c, a.id, { kind: "cache", name: selectedCache.name, query_id: selectedCache.query_id }),
                );
              }}
            />
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
              <ActionGroup
                list={selectedProxied.actions || []}
                label={`Actions for ${selectedProxied.query_id}`}
                blocked={changeBlock}
                planning={actions.planning}
                busy={actions.busy}
                onStart={(a) => {
                  if (changeBlock || actions.busy || !a.enabled) return;
                  if (a.id === "create_cache") {
                    const preferred: "shallow" | "deep" =
                      selectedProxied.shallow_support === "yes"
                        ? "shallow"
                        : selectedProxied.deep_support === "yes"
                          ? "deep"
                          : "shallow";
                    openCreateCache({ query_or_id: selectedProxied.query_id, cache_type: preferred });
                  }
                }}
              />
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
                        <>
                          {supportWord(selectedProxied.shallow_support)}
                          {selectedProxied.shallow_reason ? <> — <bdi>{selectedProxied.shallow_reason}</bdi></> : null}
                        </>,
                      ],
                      [
                        "Deep cache",
                        <>
                          {supportWord(selectedProxied.deep_support)}
                          {selectedProxied.deep_reason ? <> — <bdi>{selectedProxied.deep_reason}</bdi></> : null}
                        </>,
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
                <ActionGroup
                  list={selectedTable.actions || []}
                  label={`Actions for ${selectedTable.table}`}
                  blocked={changeBlock}
                  planning={actions.planning}
                  busy={actions.busy}
                  onStart={(a) => {
                    if (changeBlock || actions.busy || !a.enabled) return;
                    void actions.review(a.id, (c) =>
                      planReadysetAction(c, a.id, { kind: "table", table: selectedTable.table }),
                    );
                  }}
                />
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
                // Null health means Readyset did not report it: "Not read",
                // never an UNKNOWN placeholder.
                const health = t?.health || r?.health || null;
                return {
                  key: selection.key,
                  title: t?.profile_name || addr,
                  meta: <span>Upstream database · <bdi>{addr}</bdi></span>,
                  actions:
                    t?.profile_id && onSwitchConnection ? (
                      <div className="review-actions">
                        <button
                          type="button"
                          className="button"
                          disabled={stale}
                          onClick={() => {
                            if (stale) return;
                            onSwitchConnection(t.profile_id!);
                          }}
                        >
                          Use <bdi>{t.profile_name || addr}</bdi>
                        </button>
                      </div>
                    ) : undefined,
                  body: (
                    <InspectorSection title="Upstream connection">
                      <Fields
                        rows={[
                          ["Endpoint", <code>{addr}</code>],
                          ["Purpose", t?.purpose || r?.purpose || "proxy"],
                          // Null means Readyset did not report the CDC role:
                          // unknown reads "Not read", never No.
                          ["Active CDC", (t?.active_cdc ?? r?.active_cdc) === null || (t?.active_cdc ?? r?.active_cdc) === undefined ? "Not read" : (t?.active_cdc ?? r?.active_cdc) ? "Yes" : "No"],
                          ["Health", health ? health.toUpperCase() : "Not read"],
                          ["Pool connections", fmtCount(t?.connections ?? r?.connections)],
                          ["Database", t?.database || r?.database || "—"],
                          t?.product && [
                            matchEvidenceLabel(t.evidence) === "Verified" ? "Verified engine" : "Engine (matched by address)",
                            `${t.product} ${t.version || ""}`.trim(),
                          ],
                          t?.evidence && ["Verification", matchEvidenceLabel(t.evidence)],
                        ]}
                      />
                    </InspectorSection>
                  ),
                };
              })()
            : null;

  const submitPrompt = () => {
    if (!prompt || changeBlock || actions.busy) return;
    // Re-check the catalog entry in the current report: disabled or gone
    // means the prompt keeps showing its reason and nothing is planned.
    const entry = findPromptAction(prompt);
    if (!entry || !entry.enabled) return;
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
      className={`cluster-view readyset-view${failedOver || otherDatabase ? " is-stale" : ""}`}
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
              { value: "caches", label: "Caches", title: "Active shallow and deep caches", count: cachesFailed ? undefined : report!.caches.length },
              { value: "proxied", label: "Proxied", title: "Queries observed passing through Readyset", count: report!.proxied_queries.length },
              { value: "tables", label: "Tables", title: "Replicated base tables and re-snapshot controls", count: tablesReadFailed ? undefined : report!.tables.length },
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
          <p>
            Motion on the topology shows links that were running when last read; it does not measure traffic or
            throughput. Stopped, failed and unknown links stay still, and your system’s reduced motion setting turns
            motion off.
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
          {!failedOver && otherDatabase && (
            <div className="gate other-connection cluster-stale" role="note">
              <p>This report was read with another default database. Refresh to read the connection in use.</p>
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
              {report.findings.length > 0 && (
                <ul className="cluster-findings readyset-findings" aria-label="Findings">
                  {report.findings.map((f, i) => {
                    const id = `${findingIdBase}-finding-${i}`;
                    const open = !!openFindings[f.id];
                    return (
                      <li key={f.id} className={open ? "is-open" : undefined}>
                        <span className="cluster-finding-head">
                          <ClusterMark tone={f.status} label={f.status === "problem" ? "Problem" : "Watch"} />
                          <span className="cluster-finding-title">
                            <bdi>{f.title}</bdi>
                          </span>
                          <button
                            type="button"
                            className="text-button cluster-finding-toggle"
                            aria-expanded={open}
                            aria-controls={id}
                            onClick={() => setOpenFindings((cur) => ({ ...cur, [f.id]: !cur[f.id] }))}
                          >
                            {open ? "Hide details" : "Details"}
                          </button>
                          <button type="button" className="text-button readyset-finding-tab" onClick={() => onTab(f.tab)}>
                            Show in {FINDING_TAB_LABELS[f.tab]}
                          </button>
                        </span>
                        <p id={id} className="cluster-finding-text" hidden={!open}>
                          <bdi>{f.text}</bdi>
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
                    value={
                      report.telemetry.cached_share_pct !== null
                        ? String(report.telemetry.cached_share_pct)
                        : // A read zero denominator is no traffic, not an unread source.
                          report.telemetry.cached_executions !== null && report.telemetry.upstream_executions !== null
                          ? "No traffic yet"
                          : "Not read"
                    }
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
                    value={cachesFailed ? "Not read" : report.caches.length.toLocaleString()}
                    trend={
                      cachesFailed ? undefined : (
                        <span className="tile-hint">{shallowCount} shallow · {deepCount} deep</span>
                      )
                    }
                  />
                  <MetricTile
                    rowKey="shallow-hit-rate"
                    label="Shallow hit rate"
                    value={
                      report.telemetry.shallow_hit_rate_pct !== null
                        ? String(report.telemetry.shallow_hit_rate_pct)
                        : // A read zero denominator is no traffic, not an unread source.
                          report.telemetry.shallow_hits !== null && report.telemetry.shallow_misses !== null
                          ? "No traffic yet"
                          : "Not read"
                    }
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
                    value={report.replication?.status && report.replication.status !== "Unknown" ? report.replication.status : "Not read"}
                    tone={
                      replErrored
                        ? "problem"
                        : report.replication?.status === "Stopped" ||
                            (report.replication?.status === "Disabled" && deepCount > 0)
                          ? "watch"
                          : undefined
                    }
                    trend={
                      <span className="tile-hint">
                        {replErrored
                          ? replErrorFinding?.title || "Replication reported an error."
                          : report.replication?.status === "Stopped"
                            ? "Not replicating"
                            : report.replication?.consume_lag !== null && report.replication?.consume_lag !== undefined
                              ? `lag ${report.replication.consume_lag.toLocaleString()} ${report.replication.lag_unit || "events"}`
                              : tablesReadFailed
                                ? "Tables not read"
                                : `${report.table_counts.snapshotted}/${report.table_counts.total} tables ready`}
                      </span>
                    }
                  />
                </TileGrid>
              </section>

              {tab === "topology" && (
                <div className="readyset-tab-body">
                  <ConnectionGroupBar
                    group={report.connection_group}
                    connection={connection}
                    disabled={disabled || stale}
                    onReload={() => load(true)}
                  />
                  <ReadysetTopologyDiagram
                    report={report}
                    current={!stale}
                    selectedKey={selection.key || null}
                    onSelectUpstream={(k) => selection.select(selection.key === k ? "" : k, selection.key !== k)}
                    onSwitchConnection={!stale ? onSwitchConnection : undefined}
                    onOpenProxySql={onOpenProxySql}
                    onOpenCluster={onOpenCluster}
                    onOpenReplication={onOpenReplication}
                    connectionAddress={connection.host ? `${connection.host}:${connection.port}` : undefined}
                  />

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
                      <ActionGroup
                        list={catalogSlice([
                          "create_cache",
                          "flush_all_shallow_caches",
                          "drop_all_caches",
                          "drop_all_shallow_caches",
                          "drop_all_deep_caches",
                        ])}
                        label="Cache changes"
                        blocked={changeBlock}
                        planning={actions.planning}
                        busy={actions.busy}
                        onStart={startServer}
                      />
                      <button
                        type="button"
                        className="button"
                        disabled={exportBusy || stale || disabled}
                        onClick={() => void runExport()}
                      >
                        {exportBusy ? "Exporting…" : "Export SQL"}
                      </button>
                    </div>
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
                      cachesFailed ? (
                        <EmptyState icon="readyset" title="Caches not read">
                          SHOW CACHES failed on this read; the findings above have the details.
                        </EmptyState>
                      ) : (
                        <EmptyState icon="readyset" title="No active caches">
                          Create a shallow TTL cache or a deep streaming cache, or pick an observed query from Proxied.
                        </EmptyState>
                      )
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
                      <ActionGroup
                        list={catalogSlice(["create_cache", "drop_all_proxied_queries"])}
                        label="Proxied query changes"
                        blocked={changeBlock}
                        planning={actions.planning}
                        busy={actions.busy}
                        onStart={startServer}
                      />
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
                        <strong>Replication:</strong>{" "}
                        <bdi>
                          {report.replication?.status && report.replication.status !== "Unknown" ? report.replication.status : "Not read"}
                        </bdi>
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
                      <ActionGroup
                        list={catalogSlice(["start_replication", "stop_replication", "set_replication_position", "add_tables"])}
                        label="Replication changes"
                        blocked={changeBlock}
                        planning={actions.planning}
                        busy={actions.busy}
                        onStart={startServer}
                      />
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
                      tablesReadFailed ? (
                        <EmptyState icon="readyset" title="Tables not read">
                          SHOW READYSET ALL TABLES failed on this read; the findings above have the details.
                        </EmptyState>
                      ) : (
                        <EmptyState icon="readyset" title="No replicated base tables">
                          Add upstream tables or check SHOW READYSET ALL TABLES.
                        </EmptyState>
                      )
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
                      tone={
                        report.status_fields.status === "Maintenance Mode"
                          ? "watch"
                          : report.status_fields.status === "Online"
                            ? "ok"
                            : "unknown"
                      }
                      toneLabel={
                        report.status_fields.status === "Maintenance Mode"
                          ? "Active"
                          : report.status_fields.status === "Online"
                            ? "Normal"
                            : report.status_fields.status == null
                              ? "Not read"
                              : "Not known until Status is Online"
                      }
                      label="Maintenance mode"
                      detail={
                        report.status_fields.status === "Maintenance Mode"
                          ? "Signals tools such as ProxySQL to stop sending queries; Readyset still serves what it receives"
                          : report.status_fields.status == null
                            ? "Not read"
                            : report.status_fields.status === "Online"
                              ? "Not in maintenance mode"
                              : "Not known until Status is Online"
                      }
                      value={
                        <ActionGroup
                          list={catalogSlice(["enter_maintenance_mode", "exit_maintenance_mode"])}
                          label="Maintenance mode"
                          blocked={changeBlock}
                          planning={actions.planning}
                          busy={actions.busy}
                          onStart={startServer}
                        />
                      }
                    />
                    <GroupedRow
                      rowKey="privileges"
                      wrap
                      label="Cached upstream privileges"
                      detail="Reload upstream MySQL/MariaDB user credentials and table ACLs immediately"
                      value={
                        <ActionGroup
                          list={catalogSlice(["flush_privileges"])}
                          label="Cached upstream privileges"
                          blocked={changeBlock}
                          planning={actions.planning}
                          busy={actions.busy}
                          onStart={startServer}
                        />
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
                        <ActionGroup
                          list={catalogSlice(["set_log_level"])}
                          label="Tracing log level"
                          blocked={changeBlock}
                          planning={actions.planning}
                          busy={actions.busy}
                          onStart={startServer}
                        />
                      }
                    />
                    <GroupedRow
                      rowKey="eviction_memory"
                      wrap
                      label="Eviction memory limit"
                      detail={
                        <>
                          Cache eviction threshold
                          {report.status_fields.host_memory ? (
                            <>
                              {" "}
                              · Host memory <bdi>{report.status_fields.host_memory}</bdi>
                            </>
                          ) : null}
                        </>
                      }
                      value={
                        <ActionGroup
                          list={catalogSlice(["set_eviction_memory_limit"])}
                          label="Eviction memory limit"
                          blocked={changeBlock}
                          planning={actions.planning}
                          busy={actions.busy}
                          onStart={startServer}
                        />
                      }
                    />
                    <GroupedRow
                      rowKey="eviction_period"
                      wrap
                      label="Eviction check period"
                      detail="Interval in milliseconds between Readyset cache eviction sweeps"
                      value={
                        <ActionGroup
                          list={catalogSlice(["set_eviction_period"])}
                          label="Eviction check period"
                          blocked={changeBlock}
                          planning={actions.planning}
                          busy={actions.busy}
                          onStart={startServer}
                        />
                      }
                    />
                  </GroupedList>

                  <div className="readyset-allowlists" aria-label="Shallow cache allowlists">
                    <h3 className="readyset-section-heading">Shallow cache allowlists</h3>
                    {(
                      [
                        ["function", "Allowed SQL functions", report.shallow_allowlists.functions, report.allowlist_actions?.functions],
                        ["variable", "Allowed session/global variables", report.shallow_allowlists.variables, report.allowlist_actions?.variables],
                        ["schema", "Allowed schemas", report.shallow_allowlists.schemas, report.allowlist_actions?.schemas],
                      ] as const
                    ).map(([kindKey, label, items, kindActions]) => {
                      const kindEntries = kindActions || [];
                      const dropEntry = kindEntries.find((a) => a.id === "drop_shallow_allowed") || null;
                      const dropIndex = kindEntries.findIndex((a) => a.id === "drop_shallow_allowed");
                      // Fail closed: a missing catalog entry is not offered,
                      // so the Remove chip stays off with the reason.
                      const dropOff = !!changeBlock || actions.busy || !dropEntry || !dropEntry.enabled;
                      const kindBase = `${allowIdBase}-${kindKey}`;
                      // A disabled chip's accessible description: the visible
                      // block that holds the reason (the client block wins over
                      // catalog state), never only a title.
                      const dropWhy = changeBlock
                        ? `${kindBase}-blocked`
                        : !dropEntry
                          ? `${kindBase}-not-offered`
                          : dropEntry.why_not || dropEntry.fix
                            ? `${kindBase}-why-${dropIndex}`
                            : null;
                      return (
                        <div key={kindKey} className="readyset-allowlist-row">
                          <div className="readyset-allowlist-head">
                            <span className="readyset-allowlist-title">{label}</span>
                            <ActionGroup
                              list={kindEntries}
                              label={`${label} changes`}
                              blocked={changeBlock}
                              planning={actions.planning}
                              busy={actions.busy}
                              idBase={kindBase}
                              onStart={(a) => {
                                if (changeBlock || actions.busy || !a.enabled) return;
                                if (a.id === "add_shallow_allowed") {
                                  setRemoveHint("");
                                  setPrompt({ kind: "allowlist", allow_kind: kindKey, name: "" });
                                  return;
                                }
                                // drop_shallow_allowed needs the item to
                                // remove: the per-item Remove chips below carry
                                // it. An enabled Remove… points at them (focus
                                // plus a hint) instead of planning a value-less
                                // drop; a disabled Remove stays visible with its
                                // why_not and fix, also when the list is empty.
                                if (a.id === "drop_shallow_allowed") {
                                  setRemoveHint(kindKey);
                                  firstChipRef.current[kindKey]?.focus();
                                }
                              }}
                            />
                          </div>
                          {!dropEntry && !changeBlock && (
                            <p className="hint readyset-allowlist-why" id={`${kindBase}-not-offered`}>
                              Readyset did not offer this change in this report. Refresh first.
                            </p>
                          )}
                          {items.length === 0 ? (
                            <span className="readyset-allowlist-empty">None configured</span>
                          ) : (
                            <div className="readyset-allowlist-chips">
                              {items.map((item, itemIndex) => (
                                <span key={item} className="readyset-allow-chip">
                                  <code className="proxysql-code"><bdi>{item}</bdi></code>
                                  <button
                                    type="button"
                                    className="text-button"
                                    ref={
                                      itemIndex === 0
                                        ? (el) => {
                                            firstChipRef.current[kindKey] = el;
                                          }
                                        : undefined
                                    }
                                    aria-disabled={dropOff || undefined}
                                    aria-describedby={dropOff && dropWhy ? dropWhy : undefined}
                                    title={
                                      changeBlock ||
                                      (!dropEntry
                                        ? "Readyset did not offer this change in this report. Refresh first."
                                        : !dropEntry.enabled
                                          ? reason(dropEntry)
                                          : `Remove ${item}`)
                                    }
                                    onClick={() => {
                                      if (dropOff) return;
                                      setRemoveHint("");
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
                          {removeHint === kindKey && (
                            <p className="hint readyset-allowlist-hint" role="note">
                              Choose the item to remove.
                            </p>
                          )}
                        </div>
                      );
                    })}
                  </div>

                  <div className="readyset-admin-lifecycle" aria-label="Admin SQL port lifecycle">
                    <h3 className="readyset-section-heading">Admin port lifecycle (port 6036)</h3>
                    <ActionGroup
                      list={catalogSlice(["shutdown", "shutdown_reset"])}
                      label="Admin SQL port lifecycle"
                      blocked={changeBlock}
                      planning={actions.planning}
                      busy={actions.busy}
                      onStart={startServer}
                    />
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
          submitBlock={createBlock}
          onReview={(spec) => {
            if (createBlock || actions.busy) return;
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
                      : "Set replication position"
          }
          confirmTone="primary"
          confirmLabel="Review SQL…"
          confirmDisabled={!promptValid || !!promptBlock}
          onCancel={() => setPrompt(null)}
          onConfirm={submitPrompt}
        >
          {promptBlock && <p className="ops-warning" role="alert">{promptBlock}</p>}
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
          <p className="hint">Nothing is sent yet: the next step shows the exact SQL statement for review.</p>
        </ConfirmSheet>
      )}

      {exportedSql && <ExportCachesSheet exported={exportedSql} onClose={() => setExportedSql(null)} />}
    </ViewLayout>
  );
}

export default ReadysetPlace;
