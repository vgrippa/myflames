// The Cluster place: the cluster the connection in use belongs to (MariaDB
// Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster, MySQL
// InnoDB Cluster, Group Replication). Opening it reads; the changes a member
// offers (cluster.py "reviewed actions") sit in its inspector, one home, and
// go through the reviewed flow (ActionReview.tsx). Summary
// line, then (when the cluster is not OK) the findings as one compact line
// each, the metric tiles, then Topology (ClusterTopology), Members (Nodes on
// Galera) and, for InnoDB Cluster, Routers, with an inspector per member or
// router. The report is owned by the shell, so Server's Refresh all feeds
// this place and the sidebar knows when the connection is not part of a
// cluster. A report that is not the connection in use's current one (from
// another connection, or kept after a failed read) is greyed and never marks
// a member "This connection".
import { useId, useMemo, useState, type ReactNode } from "react";
import type { Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { useWritePolicy } from "./Connections";
import { ActionResult, useReviewedActions } from "./ActionReview";
import {
  DataTable, EmptyState, Fields, InspectorContext, InspectorSection, MetricTile, PrerequisiteGate, Segmented,
  SummaryLine, TileGrid, ToolbarButton, ViewLayout, ViewStatus, useWindowStatus,
  type Column, type InspectorContent, type InspectorShared, type Selection, type Tone,
} from "./kit";
import { useOpenRead } from "./viewOnScreen";
import { engineName, plural } from "./OpsShared";
import {
  ageText, blindMember, checkInText, failureText, flowPercent, isGalera, isReadReplica, kindLabel, memberNoun, readCluster,
  stateWord, utcText, planClusterAction, sendClusterAction,
  type ClusterAction, type ClusterFailure, type ClusterFinding, type ClusterMember, type ClusterReport, type ClusterRouter,
} from "./clusterApi";
import {
  ClusterMark, ClusterTopology, GLOSSARY, memberAddress, memberEngine, memberEngineFull, memberFlow, memberKey, routerKey,
  type LocalMarker,
} from "./ClusterTopology";
import "./cluster.css";

export type ClusterTab = "topology" | "members" | "routers";
const ROLE_WORD: Record<string, string> = { PRIMARY: "Primary", SECONDARY: "Secondary", READ_REPLICA: "Read replica" };
const SOURCE_TEXT: Record<string, string> = {
  wsrep_local_state_comment: "wsrep_local_state_comment on this node",
  pxc_cluster_view: "performance_schema.pxc_cluster_view on this node",
  replication_group_members: "performance_schema.replication_group_members",
  metadata: "InnoDB Cluster metadata only",
  replication_channel: "the read_replica_replication channel (replication_connection_status, replication_applier_status)",
};
// One label per metric: the same words on cards, in the table and here.
const METRIC_LABELS: Record<string, string> = {
  transactions_in_queue: "Certification queue", applier_queue: "Applier queue",
  conflicts_detected: "Conflicts", transactions_checked: "Transactions checked",
  rows_validating: "Rows validating", remote_applied: "Remote applied",
  local_proposed: "Local proposed", local_rollback: "Local rollbacks",
  local_recv_queue: "Receive queue", local_recv_queue_avg: "Receive queue average",
  local_send_queue: "Send queue", local_send_queue_avg: "Send queue average",
  flow_control_paused: "Flow control paused", flow_control_active: "Flow control active",
  flow_control_sent: "Flow control messages sent", flow_control_recv: "Flow control messages received",
  cert_deps_distance: "Certification dependency distance", local_cert_failures: "Cert failures",
  local_bf_aborts: "Brute-force aborts", desync_count: "Desync count", last_committed: "Last committed seqno",
  local_index: "Local index", cluster_conf_id: "Cluster configuration id", cluster_weight: "Cluster weight",
  local_state: "Local state number", ready: "Ready", connected: "Connected", evs_state: "EVS state",
  receiver_state: "Receiver", applier_state: "Applier", last_error_number: "Last error number",
};

function metricText(key: string, value: number | string | boolean | null | undefined): string {
  if (value === null || value === undefined) return "Not reported";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (key === "flow_control_paused" && typeof value === "number") return `${flowPercent(value)} (fraction ${value})`;
  return typeof value === "number" ? value.toLocaleString() : value;
}

/** When the report was read, as "14:40:03 (5 min ago)". */
function readAt(report: ClusterReport): string {
  const at = new Date(report.captured_at);
  if (Number.isNaN(at.getTime())) return "";
  const age = ageText((Date.now() - at.getTime()) / 1000);
  return `${at.toLocaleTimeString()}${age ? ` (${age})` : ""}`;
}

/** The member's state as one phrase ("this node Synced"; for a report that
 * is not the current one, "read on pxc-3 at 14:40:03 (5 min ago): Synced"). */
function localPhrase(report: ClusterReport, me: ClusterMember, stale: boolean) {
  return stale ? `read on ${me.name}${readAt(report) ? ` at ${readAt(report)}` : ""}: ${stateWord(me)}` : `this ${memberNoun(report).toLowerCase()} ${stateWord(me)}`;
}

/** One line (at most 120 characters) naming the cluster and its state. */
export function summaryText(report: ClusterReport, stale = false): string {
  if (!report.available) return engineName(report.engine, report.server_version, report.server_product);
  if (report.kind === "none") return engineName(report.engine, report.server_version, report.server_product);
  const c = report.cluster;
  const parts = [`${kindLabel(report)}${c.name ? ` ${c.name}` : ""}`];
  const me = report.members.find((m) => m.local);
  if (isGalera(report)) {
    if (c.component) parts.push(`${c.component} component`);
    // One word order everywhere: "2 of 3 nodes", "3 nodes".
    if (c.size !== null) parts.push(c.expected_size && c.expected_size !== c.size ? `${c.size} of ${plural(c.expected_size, "node")}` : plural(c.size, "node"));
    if (me?.state) parts.push(localPhrase(report, me, stale));
    return parts.join(" · ");
  }
  const blind = blindMember(report);
  if (me && isReadReplica(me)) {
    parts.push(stale ? `${localPhrase(report, me, true)} (a read replica)` : "this server is a read replica", ...(stale ? [] : [stateWord(me)]));
  } else if (blind) {
    parts.push(stale ? localPhrase(report, blind, true) : `this member ${blind.state}`, "group not visible from here");
  } else {
    const group = report.members.filter((m) => m.state_source === "replication_group_members");
    const online = group.filter((m) => m.state === "ONLINE").length;
    const total = Math.max(c.expected_size || 0, group.length);
    parts.push(`${online} of ${plural(total, "member")} ONLINE`);
    if (stale && me) parts.push(localPhrase(report, me, true));
  }
  if (c.topology) parts.push(c.topology === "single_primary" ? "single-primary" : "multi-primary");
  if (c.quorum === false) parts.push("no quorum");
  return parts.join(" · ");
}

export function ClusterPlace({ connection, disabled = false, report, onReport, failure = null, onFailure }: {
  connection: Connection;
  disabled?: boolean;
  /** Owned by the shell so Server's Refresh all feeds this place and the sidebar. */
  report: Bound<ClusterReport> | null;
  onReport: (report: Bound<ClusterReport>) => void;
  /** The last failed read (this place's or Refresh all's), shared by the shell. */
  failure?: ClusterFailure | null;
  onFailure?: (key: string, message: string) => void;
}) {
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [tab, setTab] = useState<ClusterTab>("topology");
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
  const key = connectionKey(connection);
  const current = report && report.key === key ? report.report : null;
  useWindowStatus(`cluster:${key}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
    engineVersion: current && current.server_version ? engineName(current.engine, current.server_version, current.server_product) : undefined,
  });
  return (
    <InspectorContext.Provider value={inspector}>
      <div className="cluster-place">
        <ClusterView connection={connection} disabled={disabled} report={report} onReport={onReport}
          failure={failure} onFailure={onFailure} selection={selection} tab={tab} onTab={setTab} />
      </div>
    </InspectorContext.Provider>
  );
}

/** A finding's identity across reports: its rule and the member it names
 * (a second finding of the same rule and member gets its occurrence). */
export function findingKeys(findings: ClusterFinding[]): string[] {
  const seen = new Map<string, number>();
  return findings.map((f) => {
    const key = `${f.id}:${f.member ?? ""}`;
    const n = seen.get(key) || 0;
    seen.set(key, n + 1);
    return n ? `${key}#${n}` : key;
  });
}

/** Findings, one compact line each: the state's shape, the title in
 * foreground text with only the member name as a link, and the reason on
 * demand, stacked under the title. Details stay open by the finding's
 * identity, so a refresh never opens another finding; the caller remounts
 * this per connection, so nothing carries over to another cluster. */
function Findings({ report, selection }: { report: ClusterReport; selection: Selection }) {
  const base = useId();
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const names = new Map(report.members.map((m) => [m.key, m.name]));
  const keys = findingKeys(report.findings);
  return (
    <ul className="cluster-findings" aria-label="Findings">
      {report.findings.map((f: ClusterFinding, i) => {
        const id = `${base}-finding-${i}`;
        const key = keys[i];
        const name = f.member ? names.get(f.member) : undefined;
        const linked = !!name && f.title.startsWith(name);
        const shown = !!open[key];
        return (
          <li key={key} className={shown ? "is-open" : undefined}>
            <span className="cluster-finding-head">
              <ClusterMark tone={f.status} label={f.status === "problem" ? "Problem" : "Watch"} />
              <span className="cluster-finding-title">
                {linked ? <>
                  <button type="button" className="text-button" onClick={() => selection.select(f.member!, true)}>{name}</button>
                  {f.title.slice(name!.length)}
                </> : f.title}
              </span>
              <button type="button" className="text-button cluster-finding-toggle" aria-expanded={shown} aria-controls={id}
                onClick={() => setOpen((o) => ({ ...o, [key]: !o[key] }))}>
                {shown ? "Hide details" : "Details"}
              </button>
            </span>
            <p id={id} className="cluster-finding-text" hidden={!shown}>{f.text}</p>
          </li>
        );
      })}
    </ul>
  );
}

function ClusterView({ connection, disabled, report: bound, onReport, failure, onFailure, selection, tab, onTab }: {
  connection: Connection; disabled: boolean; report: Bound<ClusterReport> | null;
  onReport: (report: Bound<ClusterReport>) => void; failure: ClusterFailure | null; onFailure?: (key: string, message: string) => void;
  selection: Selection; tab: ClusterTab; onTab: (tab: ClusterTab) => void;
}) {
  const reads = useConnectionReads(connection);
  const report = bound?.report || null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  // A failed read of the connection in use (this place's Refresh or Refresh
  // all) that left the report on screen as it was: shown as the last report
  // until a read succeeds through either path (a new report).
  const current = failure && failure.key === reads.key && failure.over === bound ? failure : null;
  const failedOver = !other && !!report && !!current;
  const stale = !!other || failedOver;
  const marker: LocalMarker = stale
    ? { word: "Read here", title: `This report was read on ${bound?.label || "another connection"}${report ? `, ${readAt(report)}` : ""}` }
    : { word: "This connection" };
  async function load(force = false) {
    if (disabled) return;
    await reads.run("cluster", "Reading the cluster…", readCluster, onReport, { force, onError: (message) => onFailure?.(reads.key, message) });
  }
  const policy = useWritePolicy();
  const actions = useReviewedActions({
    connection, operation: "cluster_action", send: sendClusterAction, onSent: () => { void load(true); },
  });
  // Reads when the place opens on screen, once per connection (viewOnScreen.ts).
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled, busy: !!reads.loading || reads.running(), read: () => { void load(); } });

  const isCluster = !!report && report.available && report.kind !== "none";
  const routersTab = !!report && report.kind === "innodb_cluster";
  const shownTab: ClusterTab = !routersTab && tab === "routers" ? "topology" : tab;
  const members = report?.members || [];
  const routers = report?.routers || [];
  const chosenMember = isCluster ? members.find((m) => m.key === selection.key) : undefined;
  const routerIndex = isCluster ? routers.findIndex((r, i) => routerKey(r, i) === selection.key) : -1;
  const chosenRouter = routerIndex >= 0 ? routers[routerIndex] : undefined;
  const galera = !!report && isGalera(report);
  const noun = report ? memberNoun(report) : "Member";

  // Changes only on the current report of the connection in use, with its
  // write policy known and not read-only.
  const changeBlock = stale ? (other ? `This report was read on ${other}. Refresh to read the connection in use.` : "The last read failed. Refresh first.")
    : disabled ? "Reads are paused." : policy?.blocked ? policy.blocked
      : connection.read_only ? "This connection is read-only in myflames, so changes are refused." : "";
  function start(a: ClusterAction, m: ClusterMember) {
    if (changeBlock || actions.busy) return;
    void actions.review(a.id, (c) => planClusterAction(c, a.id, m.key));
  }
  const memberActions = (m: ClusterMember): ReactNode => (m.actions || []).length ? (
    <div className="review-actions" role="group" aria-label={`Changes for ${m.name}`}>
      {(m.actions || []).map((a) => (
        <button key={a.id} type="button" className={`button${a.danger ? " danger" : ""}`} title={changeBlock || a.description}
          aria-disabled={!!changeBlock || actions.busy} onClick={() => start(a, m)}>
          {actions.planning === a.id ? "Reading…" : a.label}
        </button>
      ))}
      {changeBlock && <p className="hint">{changeBlock}</p>}
    </div>
  ) : undefined;
  const content: InspectorContent | null = report && chosenMember ? memberInspector(report, chosenMember, stale ? bound?.label || "" : null, memberActions(chosenMember))
    : report && chosenRouter ? routerInspector(chosenRouter, routerKey(chosenRouter, routerIndex)) : null;

  const memberColumns: Column<ClusterMember>[] = [
    // A starting width close to the names, so the flexible column leaves the
    // numbers and the address whole at 1440 px.
    { key: "name", label: noun, primary: true, flex: true, width: 150, minWidth: 140, sort: (m) => m.name, render: (m) => (
      <span className="cluster-cell-name"><span className="cluster-cell-text">{m.name}</span>{m.local && <span className="cluster-local" title={marker.title}>{marker.word}</span>}</span>) },
    { key: "state", label: "State", minWidth: 120, sort: (m) => stateWord(m), render: (m) => <ClusterMark tone={m.status as Tone} label={stateWord(m)} title={m.status_why.join(" ")} /> },
    ...(galera ? [] : [{ key: "role", label: "Role", sort: (m: ClusterMember) => m.role || "", render: (m: ClusterMember) => (m.role ? ROLE_WORD[m.role] || m.role : "—") }]),
    // InnoDB Cluster names members by their address (the metadata label).
    ...(members.some((m) => memberAddress(m) !== m.name) ? [{ key: "address", label: "Address", sort: (m: ClusterMember) => memberAddress(m), render: (m: ClusterMember) => memberAddress(m) }] : []),
    { key: "version", label: "Version", sort: (m) => memberEngine(report!, m), render: (m) => memberEngine(report!, m)
      ? <span title={memberEngineFull(report!, m)}>{memberEngine(report!, m)}</span> : <span title="Read on that node">—</span> },
    ...(galera ? [
      { key: "recv", label: "Receive queue", num: true, title: "wsrep_local_recv_queue (this node only)", sort: (m: ClusterMember) => num(m.metrics.local_recv_queue), render: (m: ClusterMember) => numCell(m.metrics.local_recv_queue) },
      { key: "send", label: "Send queue", num: true, title: "wsrep_local_send_queue (this node only)", sort: (m: ClusterMember) => num(m.metrics.local_send_queue), render: (m: ClusterMember) => numCell(m.metrics.local_send_queue) },
      { key: "paused", label: "Flow control paused", num: true, title: `${GLOSSARY.flow} (wsrep_flow_control_paused, this node only)`, sort: (m: ClusterMember) => num(m.metrics.flow_control_paused), render: (m: ClusterMember) => typeof m.metrics.flow_control_paused === "number" ? flowPercent(m.metrics.flow_control_paused) : "—" },
      { key: "cert", label: "Cert failures", num: true, title: "wsrep_local_cert_failures: this node's transactions rolled back by certification, cumulative (this node only)", sort: (m: ClusterMember) => num(m.metrics.local_cert_failures), render: (m: ClusterMember) => numCell(m.metrics.local_cert_failures) },
      ...(report?.kind === "pxc" ? [{ key: "segment", label: "Segment", num: true, title: "gmcast segment (pxc_cluster_view)", sort: (m: ClusterMember) => m.segment ?? null, render: (m: ClusterMember) => m.segment ?? "—" }] : []),
    ] : [
      { key: "queue", label: "Certification queue", num: true, title: "COUNT_TRANSACTIONS_IN_QUEUE (now): transactions waiting for conflict checks", sort: (m: ClusterMember) => num(m.metrics.transactions_in_queue), render: (m: ClusterMember) => numCell(m.metrics.transactions_in_queue) },
      { key: "applier", label: "Applier queue", num: true, title: "COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE (now): certified transactions waiting to be applied", sort: (m: ClusterMember) => num(m.metrics.applier_queue), render: (m: ClusterMember) => numCell(m.metrics.applier_queue) },
      { key: "conflicts", label: "Conflicts", num: true, title: "COUNT_CONFLICTS_DETECTED (cumulative)", sort: (m: ClusterMember) => num(m.metrics.conflicts_detected), render: (m: ClusterMember) => numCell(m.metrics.conflicts_detected) },
      { key: "applied", label: "Remote applied", num: true, title: "COUNT_TRANSACTIONS_REMOTE_APPLIED (cumulative)", sort: (m: ClusterMember) => num(m.metrics.remote_applied), render: (m: ClusterMember) => numCell(m.metrics.remote_applied) },
      { key: "rollback", label: "Local rollbacks", num: true, title: "COUNT_TRANSACTIONS_LOCAL_ROLLBACK (cumulative)", sort: (m: ClusterMember) => num(m.metrics.local_rollback), render: (m: ClusterMember) => numCell(m.metrics.local_rollback) },
    ]),
  ];
  const copyNote = "Read from this member's metadata copy, which may be out of date: it left the group.";
  const routerColumns: Column<ClusterRouter>[] = [
    { key: "name", label: "Router", primary: true, flex: true, sort: (r) => r.name || r.address || "", render: (r) => r.name || r.address || "Router" },
    // A router bootstrapped without --name is named by its host alone.
    ...(routers.some((r) => r.name) ? [{ key: "address", label: "Host", sort: (r: ClusterRouter) => r.address || "", render: (r: ClusterRouter) => r.address || "—" }] : []),
    { key: "version", label: "Version", sort: (r) => r.version || "", render: (r) => [r.product, r.version].filter(Boolean).join(" ") || "—" },
    { key: "rw", label: "RW port", num: true, title: "Read-write port: MySQL Router sends it to the primary", sort: (r) => r.ports.RWEndpoint || "", render: (r) => r.ports.RWEndpoint || "—" },
    { key: "ro", label: "RO port", num: true, title: "Read-only port: MySQL Router sends it to the secondaries", sort: (r) => r.ports.ROEndpoint || "", render: (r) => r.ports.ROEndpoint || "—" },
    { key: "split", label: "RW split port", num: true, title: "Read/write splitting port: reads to secondaries, writes to the primary", sort: (r) => r.ports.RWSplitEndpoint || "", render: (r) => r.ports.RWSplitEndpoint || "—" },
    { key: "checkin", label: "Last check-in", sort: (r) => r.last_check_in_age_s, render: (r) => r.last_check_in
      ? <span title={`${r.last_check_in_utc ? utcText(r.last_check_in_utc) : `${r.last_check_in} (server time)`}. MySQL Router writes it periodically, on the writable member; it is shown, not judged.${r.from_local_copy ? ` ${copyNote}` : ""}`}>
        {ageText(r.last_check_in_age_s) || r.last_check_in}{r.from_local_copy ? " · this member's copy" : ""}</span> : "Not recorded" },
  ];

  const tiles = report && isCluster ? clusterTiles(report, stale) : null;
  const tone: Tone | undefined = !report || !isCluster ? (report && !report.available ? "unknown" : undefined) : report.status;
  const failed = current ? failureText(current.message) : reads.error && !onFailure ? failureText(reads.error) : "";
  const findings = isCluster && report!.findings.length > 0 && shownTab !== "routers" ? <Findings key={bound?.key} report={report!} selection={selection} /> : null;
  // Each table keeps its own column layout (ColumnStoreContext, saved with
  // the workspace): Galera and PXC Nodes, Group Replication Members and
  // Routers have different columns, so each has its own id and instance.
  const membersTable = galera ? "cluster.nodes" : "cluster.members";
  return (
    <ViewLayout id="cluster" title="Cluster" className={`cluster-view${failedOver ? " is-stale" : ""}`} onRefresh={() => { void load(); }} refreshDisabled={disabled}
      loading={!!reads.loading} otherConnection={other} inspector={content}
      inspectorEmpty={<p>Select a {noun.toLowerCase()} to see its state, role, queues and where each value comes from.</p>}
      controls={isCluster ? (
        <Segmented<ClusterTab>
          label="Cluster sections"
          value={shownTab}
          onChange={onTab}
          options={[
            { value: "topology", label: "Topology", title: galera ? "The nodes in the component, as this node sees them" : `The members joined to the group${routersTab ? ", read replicas and routers" : ""}` },
            { value: "members", label: `${noun}s`, title: `Every ${noun.toLowerCase()} with its state${galera ? "" : ", role"} and queues` },
            ...(routersTab ? [{ value: "routers" as ClusterTab, label: "Routers", title: "MySQL Router instances registered in the metadata", count: routers.length }] : []),
          ]}
        />
      ) : undefined}
      info={<>
        <p>The cluster the connection in use belongs to: MariaDB Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster, MySQL InnoDB Cluster or Group Replication. Members are shown as this server sees them.</p>
        <p>Galera exposes only the connected node's own state, so the other nodes are listed in the component without a verdict: connect to a node to inspect it. Percona XtraDB Cluster names every node's state.</p>
        <p>OK: quorum, and every expected member present and healthy. Watch: quorum holds but a member is missing or in transition, or one more failure would stop the cluster. Problem: no quorum, a member in error, or the connected member cannot see the group.</p>
        <p>Opening it reads; nothing changes. A member's inspector offers the changes that do what they say on that server: make a secondary the primary, stop or start Group Replication on the connected member, and on MariaDB Galera desync, resync or bootstrap a Primary component on the connected node. Each one shows the exact statement, asks first, and is recorded in the Audit log.</p>
      </>}
      status={<>
        <ViewStatus loading={reads.loading || (actions.planning ? "Reading the member for review…" : undefined)} error={failed || actions.error}
          onStop={reads.loading ? reads.stop : undefined} onRetry={failed && !actions.error ? () => { void load(true); } : undefined} doneAt={reads.doneAt} />
        {actions.result && <ActionResult shown={actions.result} onClose={actions.clearResult} />}
      </>}>
      {report ? <>
        {failedOver && (
          <div className="gate other-connection cluster-stale" role="note">
            <p>Last read {readAt(report)}; the read since failed. Refresh to try again.</p>
          </div>
        )}
        <SummaryLine tone={tone} toneLabel={!report.available ? "Not available" : report.status === "unknown" ? "Not judged" : undefined}
          notes={report.notes.length ? report.notes : undefined}
          notesTitle="Notes: where the values come from, what this server cannot see, and engine differences">
          {summaryText(report, stale)}
        </SummaryLine>
        {!report.available ? (
          <PrerequisiteGate>{deniedText(report.message)}</PrerequisiteGate>
        ) : report.kind === "none" ? (
          <EmptyState icon="cluster" title="Not part of a cluster">
            {notClusterReason(report.message)} myflames reads MariaDB Galera Cluster, Galera Cluster for MySQL, Percona XtraDB Cluster and MySQL InnoDB Cluster (and Group Replication without InnoDB Cluster).
          </EmptyState>
        ) : <>
          {/* Not OK: what is wrong comes first, above the tiles. */}
          {report.status !== "ok" && findings}
          {tiles}
          {report.status === "ok" && findings}
          {shownTab === "topology" ? (
            <ClusterTopology report={report} selection={selection} marker={marker} />
          ) : shownTab === "members" ? (
            <DataTable key={membersTable} id={membersTable} label={`Cluster ${noun.toLowerCase()}s`} columns={memberColumns} rows={members} rowKey={memberKey} selection={selection} />
          ) : (
            routers.length ? (
              <DataTable key="cluster.routers" id="cluster.routers" label="Routers" columns={routerColumns} rows={routers} rowKey={(r) => routerKey(r, routers.indexOf(r))} selection={selection} />
            ) : <EmptyState icon="cluster" title="No routers registered">No MySQL Router is registered for this cluster in the InnoDB Cluster metadata.</EmptyState>
          )}
        </>}
      </> : reads.loading ? null : failed ? (
        <EmptyState icon="cluster" title="Could not read the cluster" action={<ToolbarButton primary disabled={disabled} onClick={() => { void load(); }}>Retry</ToolbarButton>}>
          {failed}
        </EmptyState>
      ) : (
        <EmptyState icon="cluster" title="Read the cluster" action={<ToolbarButton primary disabled={disabled} onClick={() => { void load(); }}>Load</ToolbarButton>}>
          The cluster the connection in use belongs to, its members and their state. Reading changes nothing.
        </EmptyState>
      )}
      {actions.sheet}
    </ViewLayout>
  );
}

/** An account that cannot read: the reason in one sentence and the grant to
 * run, without the client's error text (kept in the CLI and JSON). */
function deniedText(message: string | null) {
  const text = message || "Cluster status is not available for this connection.";
  const reason = text.split(/:\s*(?:mysql client exited \d+:\s*)?ERROR \d+/)[0].replace(/\s*$/, "");
  const account = text.match(/to user '([^']*)'@'([^']*)'/);
  const table = text.match(/(performance_schema|mysql)\.\w+/)?.[1];
  return <>
    {reason.endsWith(".") ? reason : `${reason}.`}
    {account && table && <> To grant it: <code className="ops-wrap">{`GRANT SELECT ON ${table}.* TO '${account[1]}'@'${account[2]}';`}</code></>}
  </>;
}

/** The backend's sentence without its "Not part of a cluster:" lead, which the title already says. */
function notClusterReason(message: string | null): string {
  const text = (message || "").replace(/^Not part of a cluster:\s*/, "");
  // An identifier (wsrep_on) keeps its case.
  return !text || /^\S*_/.test(text) ? text : text.charAt(0).toUpperCase() + text.slice(1);
}

function num(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}
function numCell(value: unknown) {
  return typeof value === "number" ? value.toLocaleString() : "—";
}
function worst(tones: Tone[]): Tone | undefined {
  return tones.includes("problem") ? "problem" : tones.includes("watch") ? "watch" : undefined;
}

function clusterTiles(report: ClusterReport, stale: boolean) {
  const c = report.cluster;
  const me = report.members.find((m) => m.local);
  const galera = isGalera(report);
  const s = report.summary;
  const counts = [
    s.watch ? `${s.watch} to watch` : "",
    s.problem ? plural(s.problem, "problem") : "",
  ].filter(Boolean);
  const blind = blindMember(report) || (me && isReadReplica(me) ? me : null);
  // Quorum and failures tolerated carry their own tone: no majority is a
  // problem, one more failure away from losing it a watch.
  const quorumTone: Tone | undefined = c.quorum === false ? "problem" : c.quorum && c.failures_tolerated === 0 ? "watch" : undefined;
  // A member that cannot see the group (a read replica, or this member out of
  // it) never claims quorum: both stay unknown, with the reason.
  const unseen = !galera && c.quorum === null && blind
    ? isReadReplica(blind) ? "A read replica is not a group member, so it cannot see the group's quorum." : "This member is not in the group, so it cannot see the group's quorum."
    : "";
  const tolerance = c.failures_tolerated !== null ? <span className="tile-hint">{`Tolerates ${plural(c.failures_tolerated, "failure")}`}</span>
    : unseen ? <span className="tile-hint">Failures tolerated unknown</span> : undefined;
  let membersTile;
  if (galera) {
    const judged = s.members - s.unknown;
    const short = c.size !== null && !!c.expected_size && c.size < c.expected_size;
    membersTile = (
      <MetricTile rowKey="members" label="Nodes" value={c.size === null ? "Unknown" : c.size.toLocaleString()}
        unit={c.expected_size && c.expected_size !== c.size ? `of ${c.expected_size} configured` : "in component"}
        tone={worst([...report.members.map((m) => m.status as Tone), ...(short ? ["watch" as Tone] : [])])}
        trend={<span className="tile-hint">{[s.unknown ? `${judged} inspected here` : "", ...counts].filter(Boolean).join(" · ") || "Every node inspected here"}</span>} />
    );
  } else if (blind) {
    membersTile = (
      <MetricTile rowKey="members" label="Members" value="Not visible" unit={c.expected_size ? `${c.expected_size} in metadata` : undefined}
        trend={<span className="tile-hint">{isReadReplica(blind) ? "A read replica is not in the group" : "This member is not in the group"}</span>} />
    );
  } else {
    const group = report.members.filter((m) => m.state_source === "replication_group_members");
    const online = group.filter((m) => m.state === "ONLINE").length;
    const total = Math.max(c.expected_size || 0, group.length);
    membersTile = (
      <MetricTile rowKey="members" label="Members" value={online.toLocaleString()} unit={`of ${total} ONLINE`}
        tone={worst(report.members.map((m) => m.status as Tone))}
        trend={counts.length ? <span className="tile-hint">{counts.join(" · ")}</span> : undefined} />
    );
  }
  return (
    <section className="cluster-tiles" aria-label="Cluster summary">
      <TileGrid label="Cluster summary">
        {galera ? (
          <MetricTile rowKey="component" label="Component" value={c.component ? c.component.replace(/^non-/, "Non-") : "Unknown"}
            tone={quorumTone} toneLabel={c.quorum === false ? "No quorum" : undefined} trend={tolerance}
            attrs={{ title: `${GLOSSARY.component} ${GLOSSARY.tolerates}${c.failures_tolerated !== null ? " Assumes equal pc.weight." : ""}` }} />
        ) : (
          <MetricTile rowKey="quorum" label="Quorum" value={c.quorum === null ? "Not visible" : c.quorum ? "Yes" : "No"}
            tone={quorumTone} trend={tolerance} attrs={{ title: unseen ? `${unseen} ${GLOSSARY.tolerates}` : GLOSSARY.tolerates }} />
        )}
        {membersTile}
        <MetricTile rowKey="this" label={stale ? "Read from" : galera ? "This node" : me && isReadReplica(me) ? "This server" : "This member"} value={me ? stateWord(me) : "Not found"}
          tone={me && me.status !== "ok" && me.status !== "unknown" ? (me.status as Tone) : undefined}
          trend={me && (stale || (!galera && me.role)) ? <span className="tile-hint">{[stale ? me.name : "", !galera && me.role ? ROLE_WORD[me.role] || me.role : ""].filter(Boolean).join(" · ")}</span> : undefined} />
        {galera ? (
          <MetricTile rowKey="flow" label="Flow control paused" value={flowPercent(report.galera?.metrics?.flow_control_paused)}
            attrs={{ title: `${GLOSSARY.flow} wsrep_flow_control_paused on this node.` }}
            trend={<span className="tile-hint">{`Receive queue ${report.galera?.metrics?.local_recv_queue ?? "unknown"}`}</span>} />
        ) : (
          <MetricTile rowKey="topology" label="Mode" value={c.topology === "multi_primary" ? "Multi-primary" : c.topology === "single_primary" ? "Single-primary" : "Unknown"}
            trend={report.kind === "innodb_cluster" ? <span className="tile-hint">{plural(report.routers.length, "router")}</span> : undefined} />
        )}
      </TileGrid>
    </section>
  );
}

function memberInspector(report: ClusterReport, m: ClusterMember, readOn: string | null, actions?: ReactNode): InspectorContent {
  const metrics = Object.entries(m.metrics || {}).filter(([k]) => k in METRIC_LABELS && k !== "local_state_comment");
  const g = report.galera;
  const galera = isGalera(report);
  const flow = memberFlow(report, m);
  return {
    key: m.key,
    title: m.name,
    meta: <ClusterMark tone={m.status as Tone} label={stateWord(m)} />,
    actions,
    body: <>
      <InspectorSection title={memberNoun(report)}>
        <Fields rows={[
          ["Read from", m.state_source ? SOURCE_TEXT[m.state_source] || m.state_source : "Not readable from this server"],
          !!m.pxc_status && m.state_source === "pxc_cluster_view" && ["pxc_cluster_view STATUS", m.pxc_status],
          !!m.role && ["Role", ROLE_WORD[m.role] || m.role],
          ["Address", memberAddress(m)],
          ["Version", memberEngineFull(report, m) || "Not visible here"],
          !!m.uuid && ["UUID", <code className="ops-wrap">{m.uuid}</code>],
          m.local && (readOn ? ["Report read on", readOn] : ["This connection", "Yes"]),
          !!m.label && m.label !== m.name && ["Metadata label", m.label],
          !!m.communication_stack && ["Communication stack", m.communication_stack],
          m.segment !== undefined && m.segment !== null && ["Segment", String(m.segment)],
        ]} />
        {m.status_why.map((s, i) => <p key={i}>{s}</p>)}
        {!m.status_why.some((s) => s.includes(flow)) && <p className="hint">{flow}</p>}
      </InspectorSection>
      {metrics.length > 0 && (
        <InspectorSection title={galera ? "This node's wsrep status" : m.state_source === "replication_channel" ? "Replication channel" : "Group Replication counters"}>
          <Fields rows={metrics.map(([k, v]) => [METRIC_LABELS[k], metricText(k, v)] as [string, string])} />
          <p className="hint">{galera
            ? "Averages and flow control cover the time since the provider's statistics were last reset or since start."
            : m.state_source === "replication_channel" ? "Read on this read replica; the Replication place shows the channel in detail."
              : "Queue lengths and rows validating are current; the other counts are cumulative. Another member's numbers are the ones it last sent to this member."}</p>
        </InspectorSection>
      )}
      {m.local && g && (
        <InspectorSection title="Node settings">
          <Fields rows={[
            !!g.wsrep_on && ["wsrep_on", g.wsrep_on],
            ["wsrep_node_name", g.node_name || "Not set"],
            ["wsrep_node_address", g.node_address || "Not set"],
            ["wsrep_cluster_address", g.cluster_address || "Not set"],
            ["wsrep_desync", g.desync || "Not set"],
            !!g.pxc_strict_mode && ["pxc_strict_mode", g.pxc_strict_mode],
            !!g.pxc_maint_mode && ["pxc_maint_mode", g.pxc_maint_mode],
            !!report.cluster.provider?.version && ["Galera provider", `${report.cluster.provider?.name || "Galera"} ${report.cluster.provider?.version}`],
          ]} />
        </InspectorSection>
      )}
    </>,
  };
}

function routerInspector(r: ClusterRouter, key: string): InspectorContent {
  return {
    key,
    title: r.name || r.address || "Router",
    meta: <ClusterMark tone="unknown" label="Not judged" />,
    body: <>
      <InspectorSection title="Router">
        <Fields rows={[
          ["Host", r.address || "Not recorded"],
          ["Product", [r.product, r.version].filter(Boolean).join(" ") || "Not recorded"],
          !!r.ports.RWEndpoint && ["Read-write port", r.ports.RWEndpoint],
          !!r.ports.ROEndpoint && ["Read-only port", r.ports.ROEndpoint],
          !!r.ports.RWSplitEndpoint && ["Read-write split port", r.ports.RWSplitEndpoint],
          !!r.ports.RWXEndpoint && ["X Protocol read-write port", r.ports.RWXEndpoint],
          !!r.ports.ROXEndpoint && ["X Protocol read-only port", r.ports.ROXEndpoint],
          ["Last check-in", checkInText(r)],
        ]} />
        {r.from_local_copy && <p>Read from this member's copy of the metadata, which stopped updating when it left the group: the check-in may be out of date. Connect to an ONLINE member for the current time.</p>}
        <p>Registered in the InnoDB Cluster metadata. MySQL Router writes its check-in time periodically, on the writable member: by default every tenth metadata refresh (about every 5 s), never by default in a ClusterSet, or as its stats_updates_frequency option sets. It is shown, not judged: the server cannot tell whether the router is routing now.</p>
      </InspectorSection>
    </>,
  };
}
