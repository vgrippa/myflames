// The ProxySQL place: the ProxySQL Admin interface the connection in use
// reaches (proxysql-1.0, myflames/proxysql.py). Summary line, tiles, then
// Servers (each backend as ProxySQL runs it, with its connection pool and the
// configuration row it comes from), Hostgroups, Query rules and Query
// digests, with an inspector per row. It reads when it opens on screen, once
// per connection; Refresh reads again. A report from another connection, or
// kept after a failed read, is greyed and offers no change.
//
// Query digests reads on its own (proxysql-digests-1.0,
// myflames/proxysql_digests.py) while it is on screen: one order (Slowest,
// Most time, Most frequent, Highest rate, Most rows sent), the top 10, 25, 50
// or 100, and filters by hostgroup, schema, user, digest text, one digest or
// the rule that applies, all bounded in Admin. Rules and digests are cross
// checked here: each digest shows where the active rules send it, evaluated
// here, next to the hostgroup it ran on, with a mismatch flagged; each rule
// shows its hits and the digests it applies to. Selecting one jumps to the
// other.
//
// Changes are reviewed (ActionReview.tsx): a server's status, weight or
// hostgroup, or a rule's active flag, is changed in memory and loaded to
// runtime in one unit; saving to disk is a separate change, offered after it
// and in the Configuration line. Nothing is persisted without its own review.
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useWritePolicy } from "./Connections";
import {
  DataTable, EmptyState, Fields, InfoPopover, InspectorContext, InspectorSection, MetricTile, Pill, PrerequisiteGate,
  SearchField, Segmented, StatusPill, SummaryLine, TileGrid, ToolbarButton, ToolbarSelect, ViewLayout, ViewStatus, useWindowStatus,
  type Column, type InspectorContent, type InspectorShared, type Selection, type Tone,
} from "./kit";
import { useOpenRead, useViewOnScreen } from "./viewOnScreen";
import { bytes, CompactNumber, CopySql, count, ms } from "./PerfShared";
import { plural } from "./OpsShared";
import { ClusterMark } from "./ClusterTopology";
import { ActionResult, useReviewedActions } from "./ActionReview";
import {
  planProxySqlAction, readProxySql, readProxySqlDigests, sendProxySqlAction,
  type DigestFilters, type DigestOrder, type ProxySqlAction, type ProxySqlDigest, type ProxySqlDigestsReport,
  type ProxySqlFailure, type ProxySqlHostgroup, type ProxySqlQueryRule, type ProxySqlReport, type ProxySqlRouting,
  type ProxySqlServer, type ProxySqlTarget,
} from "./proxysqlApi";
import "./cluster.css";
import "./proxysql.css";

export type ProxySqlTab = "servers" | "hostgroups" | "rules" | "digests";
const SHEET_WIDTH = 1100;
export const DEFAULT_DIGEST_FILTERS: DigestFilters = {
  order: "time", limit: 10, hostgroup: null, schema: null, user: null, text: null, digest: null, rule_id: null,
};
/** The orders of Query digests: the column each one sorts, and its words. */
const ORDERS: { value: DigestOrder; label: string; title: string; column: string; word: string }[] = [
  { value: "slowest", label: "Slowest", title: "Highest average time per execution (sum_time / count_star)", column: "avg", word: "average time" },
  { value: "time", label: "Most time", title: "Most total time (sum_time)", column: "total", word: "total time" },
  { value: "frequent", label: "Most frequent", title: "Most executions (count_star)", column: "count", word: "executions" },
  { value: "rate", label: "Highest rate", title: "Most executions per second between a digest's first and last execution (at least one second)", column: "rate", word: "executions per second" },
  { value: "rows", label: "Most rows sent", title: "Most rows sent to clients (sum_rows_sent, ProxySQL 2.0.7 and later)", column: "rows", word: "rows sent" },
];
/** Moves between tabs: a rule, a digest (filtered to it), a rule's digests. */
type Go = { rule: (id: number) => void; digest: (d: ProxySqlDigest) => void; ruleDigests: (id: number) => void };
const MONITOR_WORD: Record<string, string> = {
  galera: "Galera", group_replication: "Group Replication", replication: "Replication", aws_aurora: "Aurora",
};
const STATUS_TITLE: Record<string, string> = {
  ONLINE: "ONLINE: ProxySQL sends traffic to it",
  SHUNNED: "SHUNNED: ProxySQL stopped sending to it for a while (connection errors or replication lag)",
  OFFLINE_SOFT: "OFFLINE_SOFT: no new traffic; queries and transactions already on it finish",
  OFFLINE_HARD: "OFFLINE_HARD: out of traffic",
};

function upText(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return d ? `up ${d} d ${h} h` : h ? `up ${h} h ${m} min` : `up ${Math.max(m, 1)} min`;
}
const addr = (s: { hostname: string; port: number }) => `${s.hostname}:${s.port}`;
const latency = (us: number | null | undefined) => (typeof us === "number" ? ms(us / 1000) : "—");
const num = (v: number | null | undefined) => (typeof v === "number" ? v : null);
const numCell = (v: number | null | undefined) => (typeof v === "number" ? <CompactNumber value={v} /> : "—");
function statusWord(s: ProxySqlServer): string {
  if (s.runtime_status === null) return s.config?.status ? `${s.config.status} (configured)` : "Not in runtime";
  return s.status_detail ? `${s.runtime_status} (${s.status_detail})` : s.runtime_status;
}
function roleText(role: string | null, monitor: string | null) {
  return role && monitor ? `${MONITOR_WORD[monitor] || monitor} ${role}` : "";
}
function hostgroupTone(h: ProxySqlHostgroup): Tone {
  return !h.servers && h.tone === "ok" ? "unknown" : (h.tone as Tone);
}
function hostgroupStateWord(h: ProxySqlHostgroup): string {
  return h.tone === "problem" ? "No ONLINE server" : h.tone === "watch" ? "Watch" : h.servers ? "OK" : "Empty";
}

export function summaryText(report: ProxySqlReport): string {
  if (!report.is_proxysql) return "Not a ProxySQL Admin interface";
  const s = report.summary;
  const parts = [`ProxySQL ${report.version || ""}`.trim()];
  const up = upText(report.uptime_s);
  if (up) parts.push(up);
  parts.push(`${plural(s.backends, "backend")} in ${plural(report.hostgroups.filter((h) => h.servers).length, "hostgroup")}`);
  const out = [s.SHUNNED ? `${s.SHUNNED} SHUNNED` : "", s.OFFLINE_SOFT ? `${s.OFFLINE_SOFT} OFFLINE_SOFT` : "",
    s.OFFLINE_HARD ? `${s.OFFLINE_HARD} OFFLINE_HARD` : ""].filter(Boolean);
  parts.push(`${s.ONLINE} ONLINE${out.length ? `, ${out.join(", ")}` : ""}`);
  return parts.join(" · ");
}

export function ProxySqlPlace({ connection, disabled = false, report, onReport, failure = null, onFailure }: {
  connection: Connection;
  disabled?: boolean;
  /** Owned by the shell so the sidebar knows when the connection is not ProxySQL Admin. */
  report: Bound<ProxySqlReport> | null;
  onReport: (report: Bound<ProxySqlReport>) => void;
  failure?: ProxySqlFailure | null;
  onFailure?: (key: string, message: string) => void;
}) {
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(320);
  const [tab, setTab] = useState<ProxySqlTab>("servers");
  const [filters, setFilters] = useState<DigestFilters>(DEFAULT_DIGEST_FILTERS);
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
  const go: Go = {
    rule: (id) => { setTab("rules"); setSelectedKey(`rule:${id}`); setInspectorOpen(true); },
    digest: (d) => {
      setFilters((f) => ({ ...DEFAULT_DIGEST_FILTERS, order: f.order, limit: f.limit, digest: d.digest }));
      setTab("digests"); setSelectedKey(d.key); setInspectorOpen(true);
    },
    ruleDigests: (id) => {
      setFilters((f) => ({ ...f, rule_id: id, digest: null }));
      setTab("digests"); setSelectedKey("");
    },
  };
  const selection: Selection = {
    key: selectedKey,
    select: (key: string, open?: boolean) => {
      setSelectedKey(key);
      if (open !== false) setInspectorOpen(true);
    },
  };
  const inspector = useMemo<InspectorShared>(() => ({
    open: inspectorOpen && !!selectedKey, setOpen: setInspectorOpen,
    width: inspectorWidth, setWidth: setInspectorWidth, sheet,
  }), [inspectorOpen, selectedKey, inspectorWidth, sheet]);
  const key = connectionKey(connection);
  const current = report && report.key === key ? report.report : null;
  useWindowStatus(`proxysql:${key}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
    engineVersion: current?.is_proxysql && current.version ? `ProxySQL ${current.version}` : undefined,
  });
  return (
    <InspectorContext.Provider value={inspector}>
      <div ref={placeRef} className="cluster-place proxysql-place">
        <ProxySqlView connection={connection} disabled={disabled} report={report} onReport={onReport}
          failure={failure} onFailure={onFailure} selection={selection} tab={tab}
          onTab={(t) => { setTab(t); setSelectedKey(""); }} filters={filters} onFilters={setFilters} go={go} />
      </div>
    </InspectorContext.Provider>
  );
}

function ProxySqlView({ connection, disabled, report: bound, onReport, failure, onFailure, selection, tab, onTab, filters, onFilters, go }: {
  connection: Connection; disabled: boolean; report: Bound<ProxySqlReport> | null;
  onReport: (report: Bound<ProxySqlReport>) => void; failure: ProxySqlFailure | null; onFailure?: (key: string, message: string) => void;
  selection: Selection; tab: ProxySqlTab; onTab: (tab: ProxySqlTab) => void;
  filters: DigestFilters; onFilters: (filters: DigestFilters) => void; go: Go;
}) {
  const reads = useConnectionReads(connection);
  const digestReads = useConnectionReads(connection);
  const onScreen = useViewOnScreen();
  const policy = useWritePolicy();
  const report = bound?.report || null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const current = failure && failure.key === reads.key && failure.over === bound ? failure : null;
  const failedOver = !other && !!report && !!current;
  const stale = !!other || failedOver;
  async function load(force = false) {
    if (disabled) return;
    await reads.run("proxysql", "Reading ProxySQL…", readProxySql, onReport,
      { force, onError: (message) => onFailure?.(reads.key, message) });
  }
  // Reads when the place opens on screen, once per connection (viewOnScreen.ts).
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: !!reads.loading || reads.running(), read: () => { void load(); } });
  const [startModuleRef] = useState<{ current: (id: string) => void }>(() => ({ current: () => undefined }));
  const actions = useReviewedActions({
    connection, operation: "proxysql_action", send: sendProxySqlAction, onSent: () => {
      // Routing was evaluated with the old runtime rules and hostgroups.
      // Drop even a pending read before it can repopulate the digest cache.
      digestReads.stop();
      setDigestsBound(null);
      setDigestRevision((n) => n + 1);
      void load(true);
    },
    onNext: (id) => window.setTimeout(() => startModuleRef.current(id), 0),
  });

  const isProxy = !!report && report.is_proxysql;
  // Query digests: its own bounded read, only while that tab is on screen,
  // for the report of the connection in use; a filter change reads again.
  const [digestsBound, setDigestsBound] = useState<{ bound: Bound<ProxySqlDigestsReport>; filters: string } | null>(null);
  // A pending read has no current cache to clear. Still retry on invalidation
  // while visible, and otherwise defer the read until Query digests opens.
  const [digestRevision, setDigestRevision] = useState(0);
  const filtersKey = JSON.stringify(filters);
  async function loadDigests(force = false) {
    if (disabled) return;
    const wanted = filtersKey;
    const asked = filters;
    await digestReads.run(`proxysql-digests:${wanted}`, "Reading query digests…",
      (c, signal) => readProxySqlDigests(c, asked, signal), (b) => setDigestsBound({ bound: b, filters: wanted }), { force });
  }
  const digestsCurrent = !!digestsBound && digestsBound.bound.key === reads.key && digestsBound.filters === filtersKey;
  useEffect(() => {
    if (tab !== "digests" || !onScreen || disabled || !isProxy || stale || digestsCurrent) return;
    void loadDigests();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, onScreen, disabled, isProxy, stale, reads.key, filtersKey, digestsCurrent, digestRevision]);
  const digestsReport = digestsBound && digestsBound.bound.key === reads.key ? digestsBound.bound.report : null;
  // Changes only on the current report of the connection in use, with its
  // write policy known and not read-only.
  const changeBlock = !isProxy ? "Not a ProxySQL Admin interface."
    : stale ? (other ? `This report was read on ${other}. Refresh to read the connection in use.` : "The last read failed. Refresh first.")
      : disabled ? "Reads are paused." : policy?.blocked ? policy.blocked
        : connection.read_only ? "This connection is read-only in myflames, so changes are refused." : "";
  function start(action: ProxySqlAction, target: ProxySqlTarget, subject: string) {
    if (changeBlock || actions.busy) return;
    if (action.value) {
      const v = action.value;
      actions.askValue({ title: `${action.label.replace(/…$/, "")}: ${subject}`, label: v.label, min: v.min, max: v.max, current: v.current,
        run: (value) => { void actions.review(action.id, (c) => planProxySqlAction(c, action.id, target, value)); } });
      return;
    }
    void actions.review(action.id, (c) => planProxySqlAction(c, action.id, target));
  }
  function startModule(id: string) {
    const action = report?.actions.find((a) => a.id === id);
    if (action) start(action, null, "ProxySQL");
  }
  startModuleRef.current = startModule;
  const buttons = (list: ProxySqlAction[], target: ProxySqlTarget, subject: string) => list.length ? (
    <div className="review-actions" role="group" aria-label={`Changes for ${subject}`}>
      {list.map((a) => (
        <button key={a.id} type="button" className={`button${a.danger ? " danger" : ""}`} title={changeBlock || a.description}
          aria-disabled={!!changeBlock || actions.busy} onClick={() => start(a, target, subject)}>
          {actions.planning === a.id ? "Reading…" : a.label}
        </button>
      ))}
      {changeBlock && <p className="hint">{changeBlock}</p>}
    </div>
  ) : undefined;

  const servers = isProxy ? report!.servers : [];
  const hostgroups = isProxy ? report!.hostgroups : [];
  const rules = isProxy ? report!.query_rules : [];
  const digests = digestsReport?.digests || [];
  const chosenServer = tab === "servers" ? servers.find((s) => s.key === selection.key) : undefined;
  const chosenGroup = tab === "hostgroups" ? hostgroups.find((h) => `hg:${h.id}` === selection.key) : undefined;
  const chosenRule = tab === "rules" ? rules.find((r) => `rule:${r.rule_id}` === selection.key) : undefined;
  // A digest opened from a rule shows at once from the report while its own read runs.
  const chosenDigest = tab === "digests" ? digests.find((d) => d.key === selection.key)
    || (isProxy ? report!.query_digests.find((d) => d.key === selection.key) : undefined) : undefined;
  const content: InspectorContent | null = !report ? null
    : chosenServer ? serverInspector(chosenServer, report, buttons(chosenServer.actions || [], chosenServer.config_key ? { config: chosenServer.config_key } : null, addr(chosenServer)))
      : chosenGroup ? hostgroupInspector(chosenGroup, report)
        : chosenRule ? ruleInspector(chosenRule, report, go, buttons(chosenRule.actions || [], { rule_id: chosenRule.rule_id }, `rule ${chosenRule.rule_id}`))
          : chosenDigest ? digestInspector(chosenDigest, report, go) : null;

  const serverColumns: Column<ProxySqlServer>[] = [
    { key: "server", label: "Server", primary: true, flex: true, width: 170, minWidth: 140, sort: (s) => addr(s), render: (s) => addr(s) },
    { key: "hostgroup", label: "Hostgroup", num: true, sort: (s) => s.hostgroup_id, render: (s) => s.hostgroup_id },
    { key: "role", label: "Role", sort: (s) => s.role || "", render: (s) => (s.role
      ? <span title={`${roleText(s.role, s.monitor)} hostgroup`}>{s.role.charAt(0).toUpperCase() + s.role.slice(1)}</span> : "—") },
    { key: "status", label: "Status", minWidth: 200, sort: (s) => statusWord(s), render: (s) => (
      <ClusterMark tone={s.tone as Tone} label={statusWord(s)} title={[STATUS_TITLE[s.runtime_status || ""] || "", ...s.why].filter(Boolean).join(" ")} />) },
    { key: "weight", label: "Weight", num: true, sort: (s) => num(s.settings.weight as number), render: (s) => numCell(s.settings.weight as number) },
    { key: "used", label: "In use", num: true, title: "ConnUsed: backend connections in use now", sort: (s) => num(s.pool?.conn_used), render: (s) => numCell(s.pool?.conn_used) },
    { key: "free", label: "Free", num: true, title: "ConnFree: idle backend connections in the pool now", sort: (s) => num(s.pool?.conn_free), render: (s) => numCell(s.pool?.conn_free) },
    { key: "errors", label: "Errors", num: true, title: "ConnERR: failed backend connections since ProxySQL started (cumulative)", sort: (s) => num(s.pool?.conn_err), render: (s) => numCell(s.pool?.conn_err) },
    { key: "queries", label: "Queries", num: true, title: "Queries sent to this backend since ProxySQL started (cumulative)", sort: (s) => num(s.pool?.queries), render: (s) => numCell(s.pool?.queries) },
    { key: "latency", label: "Ping", num: true, title: "Latency_us: the monitor's last ping", sort: (s) => num(s.pool?.latency_us), render: (s) => latency(s.pool?.latency_us) },
  ];
  const groupColumns: Column<ProxySqlHostgroup>[] = [
    { key: "id", label: "Hostgroup", num: true, primary: true, sort: (h) => h.id, render: (h) => h.id },
    { key: "role", label: "Role", flex: true, sort: (h) => roleText(h.role, h.monitor), render: (h) => roleText(h.role, h.monitor) || "—" },
    { key: "state", label: "State", minWidth: 120, sort: (h) => hostgroupTone(h), render: (h) => (
      <ClusterMark tone={hostgroupTone(h)} label={hostgroupStateWord(h)} title={h.why.join(" ")} />) },
    { key: "servers", label: "Servers", num: true, sort: (h) => h.servers, render: (h) => h.servers },
    { key: "online", label: "ONLINE", num: true, sort: (h) => h.status.ONLINE, render: (h) => h.status.ONLINE },
    { key: "shunned", label: "SHUNNED", num: true, title: "SHUNNED, standby writers included", sort: (h) => h.status.SHUNNED, render: (h) => h.status.SHUNNED },
    { key: "soft", label: "OFFLINE_SOFT", num: true, sort: (h) => h.status.OFFLINE_SOFT, render: (h) => h.status.OFFLINE_SOFT },
    { key: "queries", label: "Queries", num: true, title: "Queries sent to this hostgroup's backends (cumulative)", sort: (h) => h.queries, render: (h) => <CompactNumber value={h.queries} /> },
    { key: "used", label: "Connections used", num: true, sort: (h) => h.conn_used, render: (h) => <CompactNumber value={h.conn_used} /> },
  ];
  const ruleColumns: Column<ProxySqlQueryRule>[] = [
    { key: "id", label: "Rule", num: true, sort: (r) => r.rule_id, render: (r) => r.rule_id },
    { key: "active", label: "Active", sort: (r) => (r.active ? 1 : 0), render: (r) => (r.active ? "Yes" : "No") },
    { key: "match", label: "Matches", primary: true, flex: true, sort: (r) => matchText(r), render: (r) => <code className="proxysql-code">{matchText(r)}</code> },
    { key: "dest", label: "Destination hostgroup", num: true, sort: (r) => r.destination_hostgroup, render: (r) => r.destination_hostgroup ?? "—" },
    { key: "apply", label: "Apply", sort: (r) => (r.apply ? 1 : 0), render: (r) => (r.apply ? "Yes" : "No") },
    { key: "hits", label: "Hits", num: true, title: "stats_mysql_query_rules: queries this rule matched since the rules were last loaded to runtime", sort: (r) => r.hits, render: (r) => (
      r.hits === 0 ? <span className="proxysql-nohits"><Pill title="No query matched it since the rules were last loaded to runtime">No hits</Pill>0</span> : numCell(r.hits)) },
    { key: "digests", label: "Digests", num: true, title: "Of the 100 digests with the most total time, the ones this rule applies to (evaluated here)", sort: (r) => r.digest_count ?? null, render: (r) => (
      r.digest_count === null || r.digest_count === undefined ? "—"
        : `${r.digest_count.toLocaleString()}${r.possible_digests?.length ? ` +${r.possible_digests.length} maybe` : ""}`) },
    { key: "comment", label: "Comment", sort: (r) => r.comment, render: (r) => r.comment || "—" },
  ];
  const digestColumns: Column<ProxySqlDigest>[] = [
    { key: "text", label: "Statement", primary: true, flex: true, sort: (d) => d.digest_text, render: (d) => <code className="proxysql-code">{d.digest_text}</code> },
    { key: "hg", label: "Ran on", num: true, title: "The hostgroup it ran on (-1: the query cache)", sort: (d) => d.hostgroup, render: (d) => d.hostgroup ?? "—" },
    { key: "route", label: "Rules send to", minWidth: 150, title: "Where the active query rules send it, evaluated here; a different hostgroup is flagged", sort: (d) => routeSort(d.routing), render: (d) => routeMark(d.routing) },
    { key: "count", label: "Executions", num: true, sort: (d) => d.count_star, render: (d) => numCell(d.count_star) },
    { key: "rate", label: "Per second", num: true, title: "Executions per second between its first and last execution (at least one second)", sort: (d) => d.rate_qps ?? null, render: (d) => rateText(d.rate_qps) },
    { key: "total", label: "Total time", num: true, sort: (d) => d.sum_time_us, render: (d) => latency(d.sum_time_us) },
    { key: "avg", label: "Average", num: true, sort: (d) => d.avg_time_us, render: (d) => latency(d.avg_time_us) },
    { key: "max", label: "Slowest", num: true, title: "The slowest execution (max_time)", sort: (d) => d.max_time_us, render: (d) => latency(d.max_time_us) },
    { key: "rows", label: "Rows sent", num: true, title: "sum_rows_sent (ProxySQL 2.0.7 and later)", sort: (d) => d.rows_sent, render: (d) => numCell(d.rows_sent) },
    { key: "schema", label: "Schema", sort: (d) => d.schemaname || "", render: (d) => d.schemaname || "—" },
    { key: "user", label: "User", sort: (d) => d.username || "", render: (d) => d.username || "—" },
  ];

  const failed = current ? current.message.split("\n")[0] : reads.error && !onFailure ? reads.error : "";
  const order = ORDERS.find((o) => o.value === filters.order) || ORDERS[1];
  const tone: Tone | undefined = !report ? undefined : !report.is_proxysql ? "unknown" : (report.status as Tone);
  const configLine = isProxy && (tab === "servers" || tab === "rules")
    ? <ConfigLine report={report!} module={tab === "servers" ? "servers" : "query_rules"} block={changeBlock}
      busy={actions.busy} planning={actions.planning} onAction={startModule} /> : null;
  const shownResult = actions.result;
  return (
    <ViewLayout id="proxysql" title="ProxySQL" detailBackLabel="" className={`cluster-view proxysql-view${failedOver ? " is-stale" : ""}`}
      onRefresh={() => { void load(true); if (tab === "digests" && isProxy && !stale) void loadDigests(true); }} refreshDisabled={disabled}
      loading={!!reads.loading || !!digestReads.loading} otherConnection={other} inspector={content}
      inspectorEmpty={<p>Select a row to see its details{tab === "servers" || tab === "rules" ? " and the changes it offers" : ""}.</p>}
      controls={isProxy ? (
        <Segmented<ProxySqlTab>
          label="ProxySQL sections"
          value={tab}
          onChange={onTab}
          options={[
            { value: "servers", label: "Servers", title: "Each backend as ProxySQL runs it, with its connection pool", count: servers.length },
            { value: "hostgroups", label: "Hostgroups", title: "Hostgroups in runtime and the monitors that manage them", count: hostgroups.length },
            { value: "rules", label: "Query rules", title: "mysql_query_rules: where ProxySQL routes each query", count: rules.length },
            { value: "digests", label: "Query digests", title: "stats_mysql_query_digest: the top digests in the order you choose, with where the rules send each one" },
          ]}
        />
      ) : undefined}
      info={<>
        <p>The ProxySQL Admin interface the connection in use reaches (port 6032 unless admin-mysql_ifaces says otherwise): its backends as ProxySQL runs them, hostgroups, query rules and query digests. Opening it reads; nothing changes.</p>
        <p>ProxySQL keeps three copies of its configuration: memory (what Admin edits), runtime (what it uses) and disk (what it loads at its next start). A change here edits memory and loads it to runtime; Save to disk is a separate change. Each one shows the exact statements, asks first, and is recorded in the Audit log.</p>
        <p>Galera and Group Replication monitors move servers between their hostgroups by health, so a backend can run in another hostgroup than its configuration row.</p>
        <p>Query digests lists the top digests in one order (slowest, most time, most frequent, highest rate, most rows sent) with filters, read with ORDER BY and LIMIT in Admin. Each one shows where the active query rules send it, evaluated here, next to the hostgroup it ran on.</p>
      </>}
      status={<>
        <ViewStatus loading={reads.loading || digestReads.loading || (actions.planning ? "Reading the target for review…" : undefined)} error={failed || actions.error}
          onStop={reads.loading ? reads.stop : digestReads.loading ? digestReads.stop : undefined}
          onRetry={failed && !actions.error ? () => { void load(true); } : undefined} doneAt={Math.max(reads.doneAt, digestReads.doneAt)} />
        {shownResult && <ActionResult shown={shownResult} onClose={actions.clearResult}
          onNext={(id) => { actions.clearResult(); startModule(id); }}
          extra={<AfterLines after={shownResult.value.after} />} />}
        {/* Close leaves progress here even while the inspector hides the list. */}
        {actions.sheet}
      </>}>
      {report ? <>
        {failedOver && (
          <div className="gate other-connection cluster-stale" role="note">
            <p>The read since failed; this is the last report. Refresh to try again.</p>
          </div>
        )}
        <SummaryLine tone={tone} toneLabel={!report.is_proxysql ? "Not ProxySQL" : report.status === "unknown" ? "Not judged" : undefined}
          notes={report.notes.length ? report.notes : undefined} notesTitle="Notes: what could not be read or compared">
          {summaryText(report)}
        </SummaryLine>
        {!report.is_proxysql ? (
          <EmptyState icon="proxy" title="Not a ProxySQL Admin interface">
            {(report.message || "").replace(/^This connection is not a ProxySQL Admin interface:\s*/, "").replace(/^./, (c) => c.toUpperCase())} Connect to ProxySQL's Admin port (6032 by default) with an admin account.
          </EmptyState>
        ) : <>
          {report.status !== "ok" && report.status_why.length > 0 && (
            <ul className="cluster-findings proxysql-findings" aria-label="Findings">
              {report.status_why.map((why, i) => (
                <li key={i}><span className="cluster-finding-head"><ClusterMark tone={report.status as Tone} label={report.status === "problem" ? "Problem" : report.status === "watch" ? "Watch" : "Not judged"} /><span className="cluster-finding-title">{why}</span></span></li>
              ))}
            </ul>
          )}
          {tab !== "digests" && proxyTiles(report)}
          {!report.config_readable && <PrerequisiteGate>This account cannot read ProxySQL's configuration tables (a stats-only account reads the stats schema only), so servers show what runs and no change is offered. Connect with an admin account to change anything.</PrerequisiteGate>}
          {configLine}
          {tab === "servers" ? (
            <DataTable key="proxysql.servers" id="proxysql.servers" label="ProxySQL servers" columns={serverColumns} rows={servers} rowKey={(s) => s.key} selection={selection}
              empty={<EmptyState icon="proxy" title="No backends">mysql_servers is empty and nothing runs in runtime.</EmptyState>} />
          ) : tab === "hostgroups" ? (
            <DataTable key="proxysql.hostgroups" id="proxysql.hostgroups" label="ProxySQL hostgroups" columns={groupColumns} rows={hostgroups} rowKey={(h) => `hg:${h.id}`} selection={selection} />
          ) : tab === "rules" ? (
            <DataTable key="proxysql.rules" id="proxysql.rules" label="ProxySQL query rules" columns={ruleColumns} rows={rules} rowKey={(r) => `rule:${r.rule_id}`} selection={selection}
              empty={<EmptyState icon="proxy" title="No query rules">Every query goes to its user's default hostgroup.</EmptyState>} />
          ) : <>
            <DigestTools filters={filters} onFilters={onFilters} report={digestsReport || null} proxy={report!} disabled={stale || disabled} />
            <DigestSummary report={digestsReport} order={order.word} filters={filters} error={digestReads.error} proxy={report!} />
            {digestReads.error && !digestsCurrent ? (
              <EmptyState icon="proxy" title="Could not read the query digests" action={<ToolbarButton primary disabled={disabled || stale} onClick={() => { void loadDigests(true); }}>Retry</ToolbarButton>}>
                {digestReads.error}
              </EmptyState>
            ) : (
              <DataTable key={`proxysql.digests.${filters.order}`} id="proxysql.digests" label="ProxySQL query digests" columns={digestColumns} rows={digests}
                rowKey={(d) => d.key} selection={selection} defaultSort={{ key: order.column, dir: "desc" }}
                rowClass={(d) => (d.routing.verdict === "mismatch" ? "proxysql-mismatch" : "")}
                empty={digestReads.loading ? <p className="hint">Reading query digests…</p> : filtered(filters)
                  ? <EmptyState icon="proxy" title="No digest matches">Clear a filter to see more digests.</EmptyState>
                  : <EmptyState icon="proxy" title="No digests">stats_mysql_query_digest is empty: no traffic since it was last reset, or query digests are off (mysql-query_digests).</EmptyState>} />
            )}
          </>}
        </>}
      </> : reads.loading ? null : failed ? (
        <EmptyState icon="proxy" title="Could not read ProxySQL" action={<ToolbarButton primary disabled={disabled} onClick={() => { void load(true); }}>Retry</ToolbarButton>}>
          {failed}
        </EmptyState>
      ) : (
        <EmptyState icon="proxy" title="Read ProxySQL" action={<ToolbarButton primary disabled={disabled} onClick={() => { void load(); }}>Load</ToolbarButton>}>
          The ProxySQL Admin interface the connection in use reaches: backends, hostgroups, query rules and digests.
        </EmptyState>
      )}
    </ViewLayout>
  );
}

/** Memory versus runtime and disk for one module, with its Load and Save changes. */
function ConfigLine({ report, module, block, busy, planning, onAction }: {
  report: ProxySqlReport; module: "servers" | "query_rules"; block: string; busy: boolean; planning: string; onAction: (id: string) => void;
}) {
  const pending = report.pending[module];
  const word = module === "servers" ? "servers" : "query rules";
  const runtimeMatches = pending.runtime !== null && pending.runtime.length === 0;
  const diskMatches = pending.disk !== null && pending.disk.length === 0;
  const runtime = pending.runtime === null ? "runtime not compared" : pending.runtime.length ? `${plural(pending.runtime.length, "change")} not loaded to runtime` : "memory matches runtime";
  const disk = pending.disk === null ? "disk not compared" : pending.disk.length ? `${plural(pending.disk.length, "difference")} from disk` : "saved to disk";
  const tone: Tone | undefined = pending.runtime?.length || pending.disk?.length ? "watch" : pending.runtime === null || pending.disk === null ? "unknown" : undefined;
  const ids = [`${module}_load`, `${module}_save`];
  const acts = report.actions.filter((a) => ids.includes(a.id));
  return (
    <div className="proxysql-config" role="group" aria-label={`Configuration of ${word}`}>
      <span className="proxysql-config-text">
        {tone && <StatusPill tone={tone} quiet label={tone === "watch" ? "Not applied" : "Unknown"} />}
        <span>{`Configuration of ${word}: ${runtime} · ${disk}`}</span>
      </span>
      {acts.map((a) => {
        const isLoad = a.id === `${module}_load`;
        const matches = isLoad ? runtimeMatches : diskMatches;
        const noOpTitle = isLoad ? "Nothing to load: memory matches runtime" : "Nothing to save: disk matches memory";
        return (
          <button key={a.id} type="button" className={`button${matches ? " ghost" : ""}`} title={block || (matches ? noOpTitle : a.description)} aria-disabled={!!block || busy}
            onClick={() => { if (!block && !busy) onAction(a.id); }}>{planning === a.id ? "Reading…" : a.label}</button>
        );
      })}
    </div>
  );
}

function AfterLines({ after }: { after: Record<string, unknown> | null }) {
  if (!after) return null;
  const lines: string[] = [];
  const runtime = after.runtime as { hostgroup_id: number; status: string; weight: number | null }[] | undefined;
  if (Array.isArray(runtime)) lines.push(runtime.length ? `Runtime now: ${runtime.map((r) => `hostgroup ${r.hostgroup_id} ${r.status}${r.weight !== null ? `, weight ${r.weight}` : ""}`).join("; ")}.` : "Runtime now: no row for this server.");
  const rule = after.rule as { rule_id: number; active: boolean; runtime_active: boolean | null } | null | undefined;
  if (rule) lines.push(`Rule ${rule.rule_id}: active ${rule.active ? "1" : "0"} in memory, ${rule.runtime_active === null ? "not read" : rule.runtime_active ? "1" : "0"} in runtime.`);
  const disk = after.pending_disk as string[] | null | undefined;
  if (Array.isArray(disk)) lines.push(disk.length ? `Not saved to disk: ${plural(disk.length, "difference")}.` : "Memory and disk match.");
  return lines.length ? <>{lines.map((l, i) => <p key={i} className="hint">{l}</p>)}</> : null;
}

function matchText(r: ProxySqlQueryRule): string {
  const parts = [r.match_digest ? `digest ${r.match_digest}` : "", r.match_pattern ? `${r.negate_match_pattern ? "not " : ""}${r.match_pattern}` : "",
    r.username ? `user ${r.username}` : "", r.schemaname ? `schema ${r.schemaname}` : ""].filter(Boolean);
  return parts.join(" · ") || "Every query";
}

function proxyTiles(report: ProxySqlReport) {
  const s = report.summary;
  const used = report.hostgroups.filter((h) => h.servers);
  const monitored = report.hostgroups.filter((h) => h.monitor).length;
  const monitorNames = report.monitors.filter((m) => m.active).map((m) => `${MONITOR_WORD[m.kind]} monitor`).join(" · ");
  const problems = used.filter((h) => h.tone === "problem").length;
  const activeRules = report.query_rules.filter((r) => r.active).length;
  const clients = report.metrics.Client_Connections_connected;
  const backendsTone: Tone | undefined = s.backends && !s.ONLINE ? "problem" : s.SHUNNED + s.OFFLINE_SOFT + s.OFFLINE_HARD > 0 ? "watch" : undefined;
  const standby = used.reduce((n, h) => n + h.standby, 0);
  const hostgroupsHint = `${used.length} with servers · ${monitored ? `${monitored} in monitors` : `${report.hostgroups.length - used.length} empty`}`;
  return (
    <section className="cluster-tiles" aria-label="ProxySQL summary">
      <TileGrid label="ProxySQL summary">
        <MetricTile rowKey="backends" label="Backends ONLINE" value={s.ONLINE.toLocaleString()} unit={`of ${s.backends}`}
          tone={backendsTone && standby && standby >= s.SHUNNED && !s.OFFLINE_SOFT && !s.OFFLINE_HARD && s.ONLINE ? undefined : backendsTone}
          trend={<span className="tile-hint">{[standby ? `${standby} standby ${standby === 1 ? "writer" : "writers"}` : "", s.not_in_runtime ? `${s.not_in_runtime} only configured` : ""].filter(Boolean).join(" · ") || "Rows in runtime"}</span>} />
        <MetricTile rowKey="hostgroups" label="Hostgroups" value={report.hostgroups.length.toLocaleString()}
          tone={problems ? "problem" : undefined} toneLabel={problems ? `${problems} without ONLINE` : undefined}
          attrs={monitorNames ? { title: monitorNames } : undefined}
          trend={<span className="tile-hint">{hostgroupsHint}</span>} />
        <MetricTile rowKey="rules" label="Query rules active" value={activeRules.toLocaleString()} unit={`of ${report.query_rules.length}`} />
        <MetricTile rowKey="clients" label="Client connections" value={typeof clients === "number" ? clients.toLocaleString() : "Not read"}
          attrs={{ title: "Client_Connections_connected: client connections open to ProxySQL now" }}
          trend={<span className="tile-hint">connected now</span>} />
      </TileGrid>
    </section>
  );
}

function serverInspector(s: ProxySqlServer, report: ProxySqlReport, actions: ReactNode): InspectorContent {
  const cfg = s.config;
  const p = s.pool;
  const monitor = report.monitors.find((m) => m.active && [m.writer_hostgroup, m.backup_writer_hostgroup, m.reader_hostgroup, m.offline_hostgroup].includes(s.hostgroup_id));
  return {
    key: s.key,
    title: addr(s),
    meta: <ClusterMark tone={s.tone as Tone} label={statusWord(s)} />,
    actions,
    body: <>
      <InspectorSection title="Runtime">
        <Fields rows={[
          ["Hostgroup", `${s.hostgroup_id}${s.role ? ` (${roleText(s.role, s.monitor)})` : ""}`],
          ["Status", s.runtime_status ?? "Not in runtime"],
          ...s.why.map((w): [string, string] => ["Why", w]),
          ["Weight", s.settings.weight === null || s.settings.weight === undefined ? "—" : String(s.settings.weight)],
          ["Max connections", s.settings.max_connections === null || s.settings.max_connections === undefined ? "—" : String(s.settings.max_connections)],
          ["TLS to backend", s.settings.use_ssl === 1 ? "Yes (use_ssl)" : s.settings.use_ssl === 0 ? "No" : "—"],
        ]} />
      </InspectorSection>
      {p && (
        <InspectorSection title="Connection pool">
          <Fields rows={[
            ["Connections used", numText(p.conn_used)], ["Connections free", numText(p.conn_free)],
            ["Most used at once", numText(p.max_conn_used)], ["Connections opened", numText(p.conn_ok)],
            ["Connection errors", `${numText(p.conn_err)} since ProxySQL started`], ["Queries", numText(p.queries)],
            ["Sent", bytes(p.bytes_sent)], ["Received", bytes(p.bytes_recv)], ["Ping latency", latency(p.latency_us)],
          ]} />
        </InspectorSection>
      )}
      <InspectorSection title="Configuration row">
        {cfg ? <Fields rows={[
          ["Row", `hostgroup ${cfg.hostgroup_id}, ${cfg.hostname}:${cfg.port}`],
          ["Status", cfg.status || "—"], ["Weight", numText(cfg.weight)], ["Max connections", numText(cfg.max_connections)],
          ["Max replication lag", cfg.max_replication_lag ? `${cfg.max_replication_lag} s` : "Not checked (0)"],
          ["Comment", cfg.comment || "—"],
        ]} /> : <p className="hint">{s.config_note ? `No change offered: ${s.config_note}.` : "No configuration row."}</p>}
      </InspectorSection>
      {monitor && (
        <InspectorSection title={`${MONITOR_WORD[monitor.kind]} monitor`}>
          <Fields rows={[
            ["Hostgroups", [["writer", monitor.writer_hostgroup], ["backup writer", monitor.backup_writer_hostgroup], ["reader", monitor.reader_hostgroup], ["offline", monitor.offline_hostgroup]]
              .filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => `${k} ${v}`).join(", ")],
            ["Max writers", numText(monitor.max_writers)], ["Writer is also reader", numText(monitor.writer_is_also_reader)],
            ["Max transactions behind", numText(monitor.max_transactions_behind)],
          ]} />
        </InspectorSection>
      )}
      {(s.action_notes || []).map((n, i) => <p key={i} className="hint proxysql-note">{n}</p>)}
    </>,
  };
}
function numText(v: number | null | undefined) {
  return typeof v === "number" ? v.toLocaleString() : "—";
}

function hostgroupInspector(h: ProxySqlHostgroup, report: ProxySqlReport): InspectorContent {
  const members = report.servers.filter((s) => s.hostgroup_id === h.id && s.runtime_status !== null);
  return {
    key: `hg:${h.id}`,
    title: `Hostgroup ${h.id}`,
    meta: <>
      <ClusterMark tone={hostgroupTone(h)} label={hostgroupStateWord(h)} />
      {h.role && <span className="hint">{roleText(h.role, h.monitor)}</span>}
    </>,
    body: <>
      <InspectorSection title="Servers in runtime">
        {members.length ? <Fields rows={members.map((s): [string, string] => [addr(s), statusWord(s)])} /> : <p className="hint">None.</p>}
      </InspectorSection>
      {h.why.length > 0 && <InspectorSection title="State"><Fields rows={h.why.map((w): [string, string] => ["Why", w])} /></InspectorSection>}
      <InspectorSection title="Traffic">
        <Fields rows={[["Queries", h.queries.toLocaleString()], ["Connections used", h.conn_used.toLocaleString()],
          ["Connections free", h.conn_free.toLocaleString()], ["Connection errors", `${h.conn_err.toLocaleString()} since ProxySQL started`]]} />
      </InspectorSection>
    </>,
  };
}

function ruleInspector(r: ProxySqlQueryRule, report: ProxySqlReport, go: Go, actions: ReactNode): InspectorContent {
  const shown = Object.entries(r.fields).filter(([, v]) => v !== null && v !== "");
  const byKey = new Map(report.query_digests.map((d) => [d.key, d]));
  const applies = (r.digests || []).map((k) => byKey.get(k)).filter((d): d is ProxySqlDigest => !!d);
  const maybe = (r.possible_digests || []).map((k) => byKey.get(k)).filter((d): d is ProxySqlDigest => !!d);
  // Routing is what runs: a rule disabled in memory but not loaded still routes.
  const evaluated = r.digests !== null && r.digests !== undefined;
  return {
    key: `rule:${r.rule_id}`,
    title: `Query rule ${r.rule_id}`,
    meta: <span className="hint">{r.active ? "Active" : "Not active"}{r.runtime_active !== null && r.runtime_active !== r.active ? ` (runtime: ${r.runtime_active ? "active" : "not active"})` : ""}</span>,
    actions,
    body: <>
      <InspectorSection title="Rule">
        <Fields rows={[["Matches", matchText(r)], ["Destination hostgroup", r.destination_hostgroup === null ? "None" : String(r.destination_hostgroup)],
          ["Apply", r.apply ? "Yes: no later rule is checked" : "No: later rules are checked too"],
          ["Hits", r.hits === null ? "Not read" : r.hits === 0 ? "0: no query matched it since the rules were last loaded to runtime" : r.hits.toLocaleString()]]} />
      </InspectorSection>
      <InspectorSection title="Digests it applies to" info={<RoutingInfo report={report} />}>
        {r.runtime_active === false ? <p className="hint">Not active in runtime, so it applies to no query.</p>
          : !evaluated ? <p className="hint">Not evaluated: {report.routing?.note || "the rules ProxySQL runs could not be read."}</p>
            : <>
              {applies.length ? <DigestLinks digests={applies} go={go} />
                : <p className="hint">None of the {report.query_digests.length.toLocaleString()} digests with the most total time, evaluated here.</p>}
              {maybe.length > 0 && <>
                <p className="hint">May apply to these, which ran on hostgroup {r.destination_hostgroup}: it tests what a digest does not keep{r.match_pattern ? " (match_pattern checks the query text)" : ""}.</p>
                <DigestLinks digests={maybe} go={go} />
              </>}
              <div className="proxysql-jump"><button type="button" className="button" onClick={() => go.ruleDigests(r.rule_id)}>Show its digests in Query digests</button></div>
            </>}
      </InspectorSection>
      <InspectorSection title="Every column set">
        <Fields rows={shown.map(([k, v]): [string, string] => [k, String(v)])} />
      </InspectorSection>
    </>,
  };
}

function DigestLinks({ digests, go }: { digests: ProxySqlDigest[]; go: Go }) {
  const shown = digests.slice(0, 8);
  return (
    <ul className="proxysql-links">
      {shown.map((d) => (
        <li key={d.key}>
          <button type="button" className="text-button proxysql-link" onClick={() => go.digest(d)} title="Open this digest in Query digests">
            <code>{d.digest_text}</code>
          </button>
          <span className="hint">hostgroup {d.hostgroup ?? "—"} · {plural(d.count_star || 0, "execution")} · {routeWord(d.routing)}</span>
        </li>
      ))}
      {digests.length > shown.length && <li className="hint">{`and ${(digests.length - shown.length).toLocaleString()} more`}</li>}
    </ul>
  );
}

/** How routing is evaluated here (the ⓘ beside Routing and Digests it applies to). */
function RoutingInfo({ report }: { report: Pick<ProxySqlReport, "routing"> }) {
  const ctx = report.routing;
  const engine = ctx?.regex_engine ? `${ctx.regex_engine} (mysql-query_processor_regex = ${ctx.regex_variable})` : "PCRE or RE2 (mysql-query_processor_regex)";
  return <>
    <p>Evaluated here, not by ProxySQL: the active rules of runtime_mysql_query_rules in rule_id order, following flagIN, flagOUT and apply{ctx?.iterations ? ` (mysql-query_processor_iterations = ${ctx.iterations})` : ""}, then mysql_query_rules_fast_routing, then the user&apos;s default hostgroup.</p>
    <p>Patterns are tested with Python&apos;s re; ProxySQL uses {engine}, so a pattern can match differently. match_pattern tests the query text, which a digest does not keep (comments are removed and values are ?), so it is tested on the digest text and marked approximate.</p>
    <p>A different hostgroup can come from a transaction kept on one hostgroup (transaction_persistent), a /* hostgroup=N */ comment, a connection locked to a hostgroup, or rules loaded after these executions.</p>
  </>;
}

function digestInspector(d: ProxySqlDigest, report: ProxySqlReport, go: Go): InspectorContent {
  const r = d.routing;
  const role = (id: number | null) => {
    if (id === null) return "—";
    if (id === -1) return "-1 (the query cache)";
    const hg = report.hostgroups.find((h) => h.id === id);
    const words = hg ? roleText(hg.role, hg.monitor) : "";
    return `${id}${words ? ` (${words})` : ""}`;
  };
  const rules = new Map(report.query_rules.map((x) => [x.rule_id, x]));
  const ruleButton = (id: number, note?: string) => {
    const rule = rules.get(id);
    return (
      <li key={`${id}${note || ""}`}>
        <button type="button" className="text-button proxysql-link" onClick={() => go.rule(id)} title="Open this rule in Query rules">{`Rule ${id}`}</button>
        <span className="hint">{rule ? `${matchText(rule)} → ${rule.destination_hostgroup ?? "no hostgroup"}${rule.apply ? ", apply" : ""}` : ""}{note ? ` · ${note}` : ""}</span>
      </li>
    );
  };
  return {
    key: d.key,
    title: "Query digest",
    meta: <span className="hint">{d.digest || ""}</span>,
    body: <>
      <CopySql sql={d.digest_text} label="Digest text" />
      <InspectorSection title="Routing" info={<RoutingInfo report={report} />}>
        <div className="proxysql-route" data-verdict={r.verdict}>{routeMark(r)}<span>{r.approximate ? "Approximate, evaluated here" : "Evaluated here"}</span></div>
        <Fields rows={[["Ran on", role(r.observed)], ["Rules send it to", r.expected === null ? "Unknown" : `${role(r.expected)}${sourceWord(r) ? `, ${sourceWord(r)}` : ""}`]]} />
        <p className="proxysql-route-text">{r.text}</p>
        {r.why.length > 0 && <ul className="proxysql-why">{r.why.map((w, i) => <li key={i}>{w}</li>)}</ul>}
        {(r.rules.length > 0 || r.possible.length > 0) && (
          <ul className="proxysql-links" aria-label="Rules">
            {r.rules.map((id) => ruleButton(id, id === r.decisive ? "sets the hostgroup" : "applies"))}
            {r.possible.map((id) => ruleButton(id, "may apply"))}
          </ul>
        )}
      </InspectorSection>
      <InspectorSection title="Executions">
        <Fields rows={[["Executions", numText(d.count_star)], ["Per second", rateText(d.rate_qps)], ["Total time", latency(d.sum_time_us)], ["Average", latency(d.avg_time_us)],
          ["Fastest", latency(d.min_time_us)], ["Slowest", latency(d.max_time_us)], ["Rows sent", numText(d.rows_sent)], ["Rows affected", numText(d.rows_affected)],
          ["First seen", seenText(d.first_seen)], ["Last seen", seenText(d.last_seen)]]} />
      </InspectorSection>
      <InspectorSection title="Where">
        <Fields rows={[["Hostgroup", d.hostgroup === null ? "—" : String(d.hostgroup)], ["Schema", d.schemaname || "—"], ["User", d.username || "—"],
          ["Client", d.client_address || "Not recorded (mysql-query_digests_track_hostname)"]]} />
      </InspectorSection>
    </>,
  };
}

function sourceWord(r: ProxySqlRouting): string {
  return r.source === "rule" ? `by rule ${r.decisive}` : r.source === "fast_routing" ? "by fast routing" : r.source === "default" ? "the user's default hostgroup" : "";
}
/** The Rules send to cell: the evaluated hostgroup, and a flag when it differs. */
function routeMark(r: ProxySqlRouting) {
  const by = r.source === "rule" ? `rule ${r.decisive}` : r.source === "fast_routing" ? "fast routing" : "default";
  switch (r.verdict) {
    case "match": return <ClusterMark tone="ok" label={`${r.expected} · ${by}`} title={r.text} />;
    case "mismatch": return <ClusterMark tone="watch" label={`Mismatch: ${r.expected} (${by})`} title={r.text} />;
    case "possible": return <ClusterMark tone="unknown" label={`Maybe rule ${r.possible[0]}`} title={r.text} />;
    case "local": return <ClusterMark tone="info" label="Answered by ProxySQL" title={r.text} />;
    case "cache": return <ClusterMark tone="info" label="Query cache" title={r.text} />;
    default: return <ClusterMark tone="unknown" label="Not evaluated" title={r.text} />;
  }
}
function routeWord(r: ProxySqlRouting): string {
  return r.verdict === "match" ? `ran where the rules send it (${r.expected})` : r.verdict === "mismatch" ? `mismatch: rules send it to ${r.expected}`
    : r.verdict === "possible" ? "may be another rule's" : r.verdict === "cache" ? "from the query cache"
      : r.verdict === "local" ? "answered by ProxySQL" : "routing not evaluated";
}
function routeSort(r: ProxySqlRouting): number {
  return { mismatch: 0, possible: 1, unknown: 2, local: 3, cache: 4, match: 5 }[r.verdict] ?? 6;
}
/** Executions per second: two significant digits below 10, so a digest
 * seen 5 times in 1.6 hours reads 0.00087/s, never 0.00/s. */
function rateText(v: number | null | undefined) {
  if (typeof v !== "number" || !Number.isFinite(v)) return "—";
  if (v === 0) return "0/s";
  return v >= 10 ? `${count(Math.round(v))}/s` : `${Number(v.toPrecision(2)).toLocaleString(undefined, { maximumSignificantDigits: 2 })}/s`;
}
function seenText(epoch: number | null) {
  return epoch === null ? "—" : new Date(epoch * 1000).toLocaleString();
}
function filtered(f: DigestFilters) {
  return f.hostgroup !== null || !!f.schema || !!f.user || !!f.text || !!f.digest || f.rule_id !== null;
}

/** The orders, Top N and filters of Query digests (each change reads again). */
function DigestTools({ filters, onFilters, report, proxy, disabled }: {
  filters: DigestFilters; onFilters: (f: DigestFilters) => void; report: ProxySqlDigestsReport | null; proxy: ProxySqlReport; disabled: boolean;
}) {
  const [text, setText] = useState(filters.text || "");
  // The filters as they are when the pause ends: another choice made while
  // typing (an order, a hostgroup) is kept, never replaced by an older copy.
  const latest = useRef(filters);
  latest.current = filters;
  useEffect(() => { setText(filters.text || ""); }, [filters.text]);
  // Typing waits for a pause before it reads (one bounded read per pause).
  useEffect(() => {
    const next = text.trim() || null;
    if (next === latest.current.text) return;
    const timer = window.setTimeout(() => {
      if (next !== latest.current.text) onFilters({ ...latest.current, text: next });
    }, 450);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);
  const facets = report?.facets;
  const unavailable = new Map((report?.orders || []).filter((o) => !o.available).map((o) => [o.id, o.why || ""]));
  const groupWord = (id: number) => {
    const hg = proxy.hostgroups.find((h) => h.id === id);
    const words = hg ? roleText(hg.role, hg.monitor) : "";
    return id === -1 ? "-1 (query cache)" : `${id}${words ? ` (${words})` : ""}`;
  };
  const choices = <V,>(list: V[], current: V | null, label: (v: V) => string): [string, string][] => {
    const values = list.map(String);
    const out: [string, string][] = list.map((v) => [String(v), label(v)]);
    if (current !== null && current !== undefined && !values.includes(String(current))) out.push([String(current), label(current)]);
    return out;
  };
  const hostgroups = (facets?.hostgroups || []).map((f) => f.value as number);
  const schemas = (facets?.schemas || []).map((f) => String(f.value ?? ""));
  const users = (facets?.users || []).map((f) => String(f.value ?? ""));
  const set = (patch: Partial<DigestFilters>) => { if (!disabled) onFilters({ ...filters, ...patch }); };
  return <>
    <div className="report-table-tools proxysql-digest-tools" role="group" aria-label="Query digest order">
      <Segmented<DigestOrder> label="Order digests by" className="proxysql-order-segments" value={filters.order} onChange={(order) => set({ order })}
        options={ORDERS.map((o) => ({ value: o.value, label: o.label, title: unavailable.get(o.value) || o.title, disabled: unavailable.has(o.value) }))} />
      {/* The same choice as a menu where the window is too narrow for five segments. */}
      <ToolbarSelect label="Order" className="proxysql-order-select" value={filters.order} disabled={disabled}
        onChange={(v) => set({ order: v as DigestOrder })}
        options={ORDERS.filter((o) => !unavailable.has(o.value) || o.value === filters.order).map((o) => [o.value, o.label])} />
      <ToolbarSelect label="Top" value={filters.limit} disabled={disabled} onChange={(v) => set({ limit: Number(v) as DigestFilters["limit"] })}
        options={[10, 25, 50, 100].map((n) => [n, String(n)])} />
    </div>
    <div className="report-table-tools proxysql-digest-filters" role="group" aria-label="Query digest filters">
      <SearchField label="Search digest text" placeholder="Search digest text" value={text} onChange={setText} />
      <ToolbarSelect label="Hostgroup" value={filters.hostgroup === null ? "" : filters.hostgroup} disabled={disabled}
        onChange={(v) => set({ hostgroup: v === "" ? null : Number(v) })}
        options={[["", "All"], ...choices(hostgroups, filters.hostgroup, groupWord)]} />
      <ToolbarSelect label="Schema" value={exactValue(filters.schema)} disabled={disabled} onChange={(v) => set({ schema: exactChoice(v) })}
        options={[["", "All"], ...exactOptions(schemas, filters.schema)]} />
      <ToolbarSelect label="User" value={exactValue(filters.user)} disabled={disabled} onChange={(v) => set({ user: exactChoice(v) })}
        options={[["", "All"], ...exactOptions(users, filters.user)]} />
    </div>
    {(filters.digest || filters.rule_id !== null) && (
      <div className="proxysql-chips" role="group" aria-label="Other filters">
        {filters.digest && <span className="proxysql-chip">Digest {filters.digest}<button type="button" className="text-button" aria-label="Remove the digest filter" onClick={() => onFilters({ ...filters, digest: null })}>Remove</button></span>}
        {filters.rule_id !== null && <span className="proxysql-chip">Rule {filters.rule_id} applies, evaluated here<button type="button" className="text-button" aria-label="Remove the rule filter" onClick={() => onFilters({ ...filters, rule_id: null })}>Remove</button></span>}
      </div>
    )}
  </>;
}

/** Schema and user choices: "" is All; any other value is the JSON text of
 * the exact name, so the empty name (no default schema) is a choice of its own. */
const exactValue = (v: string | null) => (v === null ? "" : JSON.stringify(v));
const exactChoice = (v: string): string | null => (v === "" ? null : String(JSON.parse(v)));
function exactOptions(values: string[], current: string | null): [string, string][] {
  const all = current !== null && !values.includes(current) ? [...values, current] : values;
  return all.map((v) => [JSON.stringify(v), v === "" ? "(none)" : v]);
}

/** One line above the digests: what is listed, out of what, since when. */
function DigestSummary({ report, order, filters, error, proxy }: {
  report: ProxySqlDigestsReport | null; order: string; filters: DigestFilters; error: string; proxy: ProxySqlReport;
}) {
  if (!report) return null;
  const shown = report.digests.length;
  const parts = [`Top ${filters.limit} by ${order}`, `${plural(shown, "digest")} listed${report.more ? ", more match" : ""}`];
  if (report.scanned !== null && report.scanned !== undefined) parts.push(`rule ${filters.rule_id} applies to these of the ${report.scanned.toLocaleString()} read in this order`);
  if (report.totals) parts.push(`${plural(report.totals.digests, "digest")} and ${compactCount(report.totals.executions)} executions in all`);
  if (report.window?.first_seen) parts.push(`since ${new Date(report.window.first_seen * 1000).toLocaleString()}`);
  const mismatches = report.digests.filter((d) => d.routing.verdict === "mismatch").length;
  return (
    <div className="proxysql-digest-summary">
      <span>{parts.join(" · ")}</span>
      {mismatches > 0 && <StatusPill tone="watch" label={`${mismatches} on another hostgroup than the rules send to`} />}
      {error && <span className="error">{error}</span>}
      <InfoPopover label="How routing is evaluated"><RoutingInfo report={report.routing ? report : proxy} /></InfoPopover>
    </div>
  );
}
function compactCount(v: number) {
  return v.toLocaleString();
}
