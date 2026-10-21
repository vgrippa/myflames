// The ProxySQL place: the ProxySQL Admin interface the connection in use
// reaches (proxysql-1.0, myflames/proxysql.py). Summary line, tiles, then
// Servers (each backend as ProxySQL runs it, with its connection pool and the
// configuration row it comes from), Hostgroups, Query rules and Query
// digests, with an inspector per row. It reads when it opens on screen, once
// per connection; Refresh reads again. A report from another connection, or
// kept after a failed read, is greyed and offers no change.
//
// Changes are reviewed (ActionReview.tsx): a server's status, weight or
// hostgroup, or a rule's active flag, is changed in memory and loaded to
// runtime in one unit; saving to disk is a separate change, offered after it
// and in the Configuration line. Nothing is persisted without its own review.
import { useMemo, useState, type ReactNode } from "react";
import type { Connection } from "./api";
import { useConnectionReads, type Bound } from "./binding";
import { useWritePolicy } from "./Connections";
import {
  DataTable, EmptyState, Fields, InspectorContext, InspectorSection, MetricTile, PrerequisiteGate, Segmented,
  StatusPill, SummaryLine, TileGrid, ToolbarButton, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type InspectorShared, type Selection, type Tone,
} from "./kit";
import { useOpenRead } from "./viewOnScreen";
import { bytes, CompactNumber, CopySql, ms } from "./PerfShared";
import { plural } from "./OpsShared";
import { ClusterMark } from "./ClusterTopology";
import { ActionResult, useReviewedActions } from "./ActionReview";
import {
  planProxySqlAction, readProxySql, sendProxySqlAction,
  type ProxySqlAction, type ProxySqlDigest, type ProxySqlFailure, type ProxySqlHostgroup, type ProxySqlQueryRule,
  type ProxySqlReport, type ProxySqlServer, type ProxySqlTarget,
} from "./proxysqlApi";
import "./cluster.css";
import "./proxysql.css";

export type ProxySqlTab = "servers" | "hostgroups" | "rules" | "digests";
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
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [tab, setTab] = useState<ProxySqlTab>("servers");
  const selection: Selection = {
    key: selectedKey,
    select: (key: string, open?: boolean) => {
      setSelectedKey(key);
      if (open !== false) setInspectorOpen(true);
    },
  };
  const inspector = useMemo<InspectorShared>(() => ({
    open: inspectorOpen && !!selectedKey, setOpen: setInspectorOpen,
    width: inspectorWidth, setWidth: setInspectorWidth, sheet: false,
  }), [inspectorOpen, selectedKey, inspectorWidth]);
  return (
    <InspectorContext.Provider value={inspector}>
      <div className="cluster-place proxysql-place">
        <ProxySqlView connection={connection} disabled={disabled} report={report} onReport={onReport}
          failure={failure} onFailure={onFailure} selection={selection} tab={tab}
          onTab={(t) => { setTab(t); setSelectedKey(""); }} />
      </div>
    </InspectorContext.Provider>
  );
}

function ProxySqlView({ connection, disabled, report: bound, onReport, failure, onFailure, selection, tab, onTab }: {
  connection: Connection; disabled: boolean; report: Bound<ProxySqlReport> | null;
  onReport: (report: Bound<ProxySqlReport>) => void; failure: ProxySqlFailure | null; onFailure?: (key: string, message: string) => void;
  selection: Selection; tab: ProxySqlTab; onTab: (tab: ProxySqlTab) => void;
}) {
  const reads = useConnectionReads(connection);
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
  const actions = useReviewedActions({
    connection, operation: "proxysql_action", send: sendProxySqlAction, onSent: () => { void load(true); },
  });

  const isProxy = !!report && report.is_proxysql;
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
  const digests = isProxy ? report!.query_digests : [];
  const chosenServer = tab === "servers" ? servers.find((s) => s.key === selection.key) : undefined;
  const chosenGroup = tab === "hostgroups" ? hostgroups.find((h) => `hg:${h.id}` === selection.key) : undefined;
  const chosenRule = tab === "rules" ? rules.find((r) => `rule:${r.rule_id}` === selection.key) : undefined;
  const digestKeys = digestKeyList(digests);
  const chosenDigest = tab === "digests" ? digests.find((_, i) => digestKeys[i] === selection.key) : undefined;
  const content: InspectorContent | null = !report ? null
    : chosenServer ? serverInspector(chosenServer, report, buttons(chosenServer.actions || [], chosenServer.config_key ? { config: chosenServer.config_key } : null, addr(chosenServer)))
      : chosenGroup ? hostgroupInspector(chosenGroup, report)
        : chosenRule ? ruleInspector(chosenRule, buttons(chosenRule.actions || [], { rule_id: chosenRule.rule_id }, `rule ${chosenRule.rule_id}`))
          : chosenDigest ? digestInspector(chosenDigest, digestKeys[digests.indexOf(chosenDigest)]) : null;

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
    { key: "state", label: "State", minWidth: 120, sort: (h) => h.tone, render: (h) => (
      <ClusterMark tone={h.tone as Tone} label={h.tone === "problem" ? "No ONLINE server" : h.tone === "watch" ? "Watch" : h.servers ? "OK" : "Empty"} title={h.why.join(" ")} />) },
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
    { key: "hits", label: "Hits", num: true, title: "stats_mysql_query_rules: queries this rule matched since the rules were last loaded", sort: (r) => r.hits, render: (r) => numCell(r.hits) },
    { key: "comment", label: "Comment", sort: (r) => r.comment, render: (r) => r.comment || "—" },
  ];
  const digestColumns: Column<ProxySqlDigest>[] = [
    { key: "text", label: "Statement", primary: true, flex: true, sort: (d) => d.digest_text, render: (d) => <code className="proxysql-code">{d.digest_text}</code> },
    { key: "hg", label: "Hostgroup", num: true, sort: (d) => d.hostgroup, render: (d) => d.hostgroup ?? "—" },
    { key: "schema", label: "Schema", sort: (d) => d.schemaname || "", render: (d) => d.schemaname || "—" },
    { key: "user", label: "User", sort: (d) => d.username || "", render: (d) => d.username || "—" },
    { key: "count", label: "Executions", num: true, sort: (d) => d.count_star, render: (d) => numCell(d.count_star) },
    { key: "total", label: "Total time", num: true, sort: (d) => d.sum_time_us, render: (d) => latency(d.sum_time_us) },
    { key: "avg", label: "Average", num: true, sort: (d) => d.avg_time_us, render: (d) => latency(d.avg_time_us) },
    { key: "max", label: "Slowest", num: true, sort: (d) => d.max_time_us, render: (d) => latency(d.max_time_us) },
  ];

  const failed = current ? current.message.split("\n")[0] : reads.error && !onFailure ? reads.error : "";
  const tone: Tone | undefined = !report ? undefined : !report.is_proxysql ? "unknown" : (report.status as Tone);
  const configLine = isProxy && (tab === "servers" || tab === "rules")
    ? <ConfigLine report={report!} module={tab === "servers" ? "servers" : "query_rules"} block={changeBlock}
      busy={actions.busy} planning={actions.planning} onAction={startModule} /> : null;
  const shownResult = actions.result;
  return (
    <ViewLayout id="proxysql" title="ProxySQL" className={`cluster-view proxysql-view${failedOver ? " is-stale" : ""}`}
      onRefresh={() => { void load(true); }} refreshDisabled={disabled}
      loading={!!reads.loading} otherConnection={other} inspector={content}
      detailBackLabel={`Back to ${tab === "servers" ? "servers" : tab === "hostgroups" ? "hostgroups" : tab === "rules" ? "query rules" : "query digests"}`}
      inspectorEmpty={<p>Select a row to see its details{tab === "servers" || tab === "rules" ? " and the changes it offers" : ""}.</p>}
      controls={isProxy ? (
        <Segmented<ProxySqlTab>
          label="ProxySQL sections"
          value={tab}
          onChange={onTab}
          options={[
            { value: "servers", label: "Servers", title: "Each backend as ProxySQL runs it, with its connection pool", count: servers.length },
            { value: "hostgroups", label: "Hostgroups", title: "Hostgroups in runtime and the monitors that manage them", count: hostgroups.filter((h) => h.servers).length },
            { value: "rules", label: "Query rules", title: "mysql_query_rules: where ProxySQL routes each query", count: rules.length },
            { value: "digests", label: "Query digests", title: "The 100 statement digests with the most total time", count: digests.length },
          ]}
        />
      ) : undefined}
      info={<>
        <p>The ProxySQL Admin interface the connection in use reaches (port 6032 unless admin-mysql_ifaces says otherwise): its backends as ProxySQL runs them, hostgroups, query rules and query digests. Opening it reads; nothing changes.</p>
        <p>ProxySQL keeps three copies of its configuration: memory (what Admin edits), runtime (what it uses) and disk (what it loads at its next start). A change here edits memory and loads it to runtime; Save to disk is a separate change. Each one shows the exact statements, asks first, and is recorded in the Audit log.</p>
        <p>Galera and Group Replication monitors move servers between their hostgroups by health, so a backend can run in another hostgroup than its configuration row.</p>
      </>}
      status={<>
        <ViewStatus loading={reads.loading || (actions.planning ? "Reading the target for review…" : undefined)} error={failed || actions.error} />
        {shownResult && <ActionResult shown={shownResult} onClose={actions.clearResult}
          onNext={(id) => { actions.clearResult(); startModule(id); }}
          extra={<AfterLines after={shownResult.value.after} />} />}
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
          {proxyTiles(report)}
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
          ) : (
            <DataTable key="proxysql.digests" id="proxysql.digests" label="ProxySQL query digests" columns={digestColumns} rows={digests} rowKey={(d) => digestKeys[digests.indexOf(d)]} selection={selection}
              defaultSort={{ key: "total", dir: "desc" }}
              empty={<EmptyState icon="proxy" title="No digests">stats_mysql_query_digest is empty: no traffic since it was last reset, or query digests are off (mysql-query_digests).</EmptyState>} />
          )}
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
      {actions.sheet}
    </ViewLayout>
  );
}

/** Memory versus runtime and disk for one module, with its Load and Save changes. */
function ConfigLine({ report, module, block, busy, planning, onAction }: {
  report: ProxySqlReport; module: "servers" | "query_rules"; block: string; busy: boolean; planning: string; onAction: (id: string) => void;
}) {
  const pending = report.pending[module];
  const word = module === "servers" ? "servers" : "query rules";
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
      {acts.map((a) => (
        <button key={a.id} type="button" className="button" title={block || a.description} aria-disabled={!!block || busy}
          onClick={() => { if (!block && !busy) onAction(a.id); }}>{planning === a.id ? "Reading…" : a.label}</button>
      ))}
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

function digestKeyList(digests: ProxySqlDigest[]): string[] {
  const seen = new Map<string, number>();
  return digests.map((d) => {
    const base = `digest:${d.hostgroup}:${d.schemaname}:${d.username}:${d.digest}`;
    const n = seen.get(base) || 0;
    seen.set(base, n + 1);
    return n ? `${base}#${n}` : base;
  });
}

function proxyTiles(report: ProxySqlReport) {
  const s = report.summary;
  const used = report.hostgroups.filter((h) => h.servers);
  const problems = used.filter((h) => h.tone === "problem").length;
  const activeRules = report.query_rules.filter((r) => r.active).length;
  const clients = report.metrics.Client_Connections_connected;
  const backendsTone: Tone | undefined = s.backends && !s.ONLINE ? "problem" : s.SHUNNED + s.OFFLINE_SOFT + s.OFFLINE_HARD > 0 ? "watch" : undefined;
  const standby = used.reduce((n, h) => n + h.standby, 0);
  return (
    <section className="cluster-tiles" aria-label="ProxySQL summary">
      <TileGrid label="ProxySQL summary">
        <MetricTile rowKey="backends" label="Backends ONLINE" value={s.ONLINE.toLocaleString()} unit={`of ${s.backends}`}
          tone={backendsTone && standby && standby >= s.SHUNNED && !s.OFFLINE_SOFT && !s.OFFLINE_HARD && s.ONLINE ? undefined : backendsTone}
          trend={<span className="tile-hint">{[standby ? `${standby} standby ${standby === 1 ? "writer" : "writers"}` : "", s.not_in_runtime ? `${s.not_in_runtime} only configured` : ""].filter(Boolean).join(" · ") || "Rows in runtime"}</span>} />
        <MetricTile rowKey="hostgroups" label="Hostgroups" value={used.length.toLocaleString()}
          tone={problems ? "problem" : undefined} toneLabel={problems ? `${problems} without ONLINE` : undefined}
          trend={<span className="tile-hint">{report.monitors.filter((m) => m.active).map((m) => `${MONITOR_WORD[m.kind]} monitor`).join(" · ") || "No monitor"}</span>} />
        <MetricTile rowKey="rules" label="Query rules active" value={activeRules.toLocaleString()} unit={`of ${report.query_rules.length}`} />
        <MetricTile rowKey="clients" label="Client connections" value={typeof clients === "number" ? clients.toLocaleString() : "Not read"}
          trend={<span className="tile-hint">Client_Connections_connected</span>} />
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
    meta: h.role ? <span className="hint">{roleText(h.role, h.monitor)}</span> : undefined,
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

function ruleInspector(r: ProxySqlQueryRule, actions: ReactNode): InspectorContent {
  const shown = Object.entries(r.fields).filter(([, v]) => v !== null && v !== "");
  return {
    key: `rule:${r.rule_id}`,
    title: `Query rule ${r.rule_id}`,
    meta: <span className="hint">{r.active ? "Active" : "Not active"}{r.runtime_active !== null && r.runtime_active !== r.active ? ` (runtime: ${r.runtime_active ? "active" : "not active"})` : ""}</span>,
    actions,
    body: <>
      <InspectorSection title="Rule">
        <Fields rows={[["Matches", matchText(r)], ["Destination hostgroup", r.destination_hostgroup === null ? "None" : String(r.destination_hostgroup)],
          ["Apply", r.apply ? "Yes: no later rule is checked" : "No: later rules are checked too"], ["Hits", r.hits === null ? "Not read" : r.hits.toLocaleString()]]} />
      </InspectorSection>
      <InspectorSection title="Every column set">
        <Fields rows={shown.map(([k, v]): [string, string] => [k, String(v)])} />
      </InspectorSection>
    </>,
  };
}

function digestInspector(d: ProxySqlDigest, key: string): InspectorContent {
  return {
    key,
    title: "Query digest",
    meta: <span className="hint">{d.digest || ""}</span>,
    body: <>
      <CopySql sql={d.digest_text} label="Digest text" />
      <InspectorSection title="Executions">
        <Fields rows={[["Executions", numText(d.count_star)], ["Total time", latency(d.sum_time_us)], ["Average", latency(d.avg_time_us)],
          ["Fastest", latency(d.min_time_us)], ["Slowest", latency(d.max_time_us)], ["Rows sent", numText(d.rows_sent)], ["Rows affected", numText(d.rows_affected)]]} />
      </InspectorSection>
      <InspectorSection title="Where">
        <Fields rows={[["Hostgroup", d.hostgroup === null ? "—" : String(d.hostgroup)], ["Schema", d.schemaname || "—"], ["User", d.username || "—"],
          ["Client", d.client_address || "—"]]} />
      </InspectorSection>
    </>,
  };
}
