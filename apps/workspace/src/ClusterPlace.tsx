// The Cluster place: the cluster the connection in use belongs to (MariaDB
// Galera Cluster, Percona XtraDB Cluster, MySQL InnoDB Cluster, Group
// Replication), read-only in this release. Summary line, metric tiles, then
// Topology (ClusterTopology), Members and, for InnoDB Cluster, Routers, with an
// inspector per member or router. The report is owned by the shell, so
// Server's Refresh all feeds this place and the sidebar knows when the
// connection is not part of a cluster.
import { useMemo, useState } from "react";
import type { Connection } from "./api";
import { useConnectionReads, type Bound } from "./binding";
import {
  DataTable, EmptyState, Fields, InspectorContext, InspectorSection, MetricTile, PrerequisiteGate, Segmented,
  StatusPill, SummaryLine, TileGrid, ToolbarButton, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type InspectorShared, type Selection, type Tone,
} from "./kit";
import { useOpenRead } from "./viewOnScreen";
import { engineName, plural } from "./OpsShared";
import {
  KIND_LABEL, readCluster, stateWord,
  type ClusterMember, type ClusterReport, type ClusterRouter,
} from "./clusterApi";
import { ClusterTopology, memberAddress, memberEngine, memberFlow, memberKey, routerKey } from "./ClusterTopology";
import "./cluster.css";

export type ClusterTab = "topology" | "members" | "routers";
const ROLE_WORD: Record<string, string> = { PRIMARY: "Primary", SECONDARY: "Secondary", READ_REPLICA: "Read replica" };
const SOURCE_TEXT: Record<string, string> = {
  wsrep_local_state_comment: "wsrep_local_state_comment on this node",
  pxc_cluster_view: "performance_schema.pxc_cluster_view on this node",
  replication_group_members: "performance_schema.replication_group_members",
  metadata: "InnoDB Cluster metadata only",
};
const METRIC_LABELS: Record<string, string> = {
  transactions_in_queue: "Waiting for certification", applier_queue: "Waiting in the applier",
  conflicts_detected: "Conflicts detected", transactions_checked: "Transactions checked",
  rows_validating: "Rows validating", remote_applied: "Remote transactions applied",
  local_proposed: "Local transactions proposed", local_rollback: "Local transactions rolled back",
  local_recv_queue: "Receive queue", local_recv_queue_avg: "Receive queue average",
  local_send_queue: "Send queue", local_send_queue_avg: "Send queue average",
  flow_control_paused: "Flow control paused (fraction)", flow_control_active: "Flow control active",
  flow_control_sent: "Flow control messages sent", flow_control_recv: "Flow control messages received",
  cert_deps_distance: "Certification dependency distance", local_cert_failures: "Certification failures",
  local_bf_aborts: "Brute-force aborts", desync_count: "Desync count", last_committed: "Last committed seqno",
  local_index: "Local index", cluster_conf_id: "Cluster configuration id", cluster_weight: "Cluster weight",
  local_state: "Local state number", local_state_comment: "Local state", ready: "Ready", connected: "Connected",
  evs_state: "EVS state",
};

function metricText(value: number | string | boolean | null | undefined): string {
  if (value === null || value === undefined) return "Not reported";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return typeof value === "number" ? value.toLocaleString() : value;
}

function ageText(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return "";
  if (seconds < 120) return `${seconds} s ago`;
  if (seconds < 7200) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

function percent(value: unknown): string {
  return typeof value === "number" ? `${Math.round(value * 1000) / 10}%` : "Unknown";
}

/** One line (at most 120 characters) naming the cluster and its state. */
export function summaryText(report: ClusterReport): string {
  if (!report.available) return "Cluster status not available";
  if (report.kind === "none") return engineName(report.engine, report.server_version);
  const c = report.cluster;
  const parts = [`${KIND_LABEL[report.kind]}${c.name ? ` ${c.name}` : ""}`];
  if (report.kind === "galera" || report.kind === "pxc") {
    if (c.component) parts.push(`${c.component} component`);
    if (c.size !== null) parts.push(`${plural(c.size, "node")}${c.expected_size && c.expected_size !== c.size ? ` of ${c.expected_size}` : ""}`);
    const me = report.members.find((m) => m.local);
    if (me?.state) parts.push(`this node ${me.state}`);
  } else {
    const online = report.members.filter((m) => m.state === "ONLINE").length;
    const group = report.members.filter((m) => m.state_source === "replication_group_members").length;
    parts.push(`${online} of ${c.expected_size || group} ${(c.expected_size || group) === 1 ? "member" : "members"} ONLINE`);
    if (c.topology) parts.push(c.topology === "single_primary" ? "single-primary" : "multi-primary");
    if (c.quorum === false) parts.push("no quorum");
  }
  return parts.join(" · ");
}

export function ClusterPlace({ connection, disabled = false, report, onReport }: {
  connection: Connection;
  disabled?: boolean;
  /** Owned by the shell so Server's Refresh all feeds this place and the sidebar. */
  report: Bound<ClusterReport> | null;
  onReport: (report: Bound<ClusterReport>) => void;
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
  return (
    <InspectorContext.Provider value={inspector}>
      <div className="cluster-place">
        <ClusterView connection={connection} disabled={disabled} report={report} onReport={onReport}
          selection={selection} tab={tab} onTab={setTab} />
      </div>
    </InspectorContext.Provider>
  );
}

function ClusterView({ connection, disabled, report: bound, onReport, selection, tab, onTab }: {
  connection: Connection; disabled: boolean; report: Bound<ClusterReport> | null;
  onReport: (report: Bound<ClusterReport>) => void; selection: Selection; tab: ClusterTab; onTab: (tab: ClusterTab) => void;
}) {
  const reads = useConnectionReads(connection);
  const report = bound?.report || null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  async function load() {
    if (disabled) return;
    await reads.run("cluster", "Reading the cluster…", readCluster, onReport);
  }
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
  const galera = report?.kind === "galera" || report?.kind === "pxc";

  const content: InspectorContent | null = report && chosenMember ? memberInspector(report, chosenMember)
    : report && chosenRouter ? routerInspector(chosenRouter, routerKey(chosenRouter, routerIndex)) : null;

  const memberColumns: Column<ClusterMember>[] = [
    { key: "name", label: "Member", primary: true, flex: true, minWidth: 160, sort: (m) => m.name, render: (m) => (
      <span className="cluster-cell-name">{m.name}{m.local && <span className="cluster-local">This connection</span>}</span>) },
    { key: "state", label: "State", minWidth: 120, sort: (m) => stateWord(m), render: (m) => <StatusPill tone={m.status as Tone} label={stateWord(m)} title={m.status_why.join(" ")} /> },
    ...(galera ? [] : [{ key: "role", label: "Role", sort: (m: ClusterMember) => m.role || "", render: (m: ClusterMember) => (m.role ? ROLE_WORD[m.role] || m.role : "—") }]),
    // InnoDB Cluster names members by their address (the metadata label).
    ...(members.some((m) => memberAddress(m) !== m.name) ? [{ key: "address", label: "Address", sort: (m: ClusterMember) => memberAddress(m), render: (m: ClusterMember) => memberAddress(m) }] : []),
    { key: "version", label: "Version", sort: (m) => memberEngine(report!, m), render: (m) => memberEngine(report!, m) },
    ...(galera ? [
      { key: "recv", label: "Recv queue", num: true, title: "wsrep_local_recv_queue (this node only)", sort: (m: ClusterMember) => num(m.metrics.local_recv_queue), render: (m: ClusterMember) => numCell(m.metrics.local_recv_queue) },
      { key: "send", label: "Send queue", num: true, title: "wsrep_local_send_queue (this node only)", sort: (m: ClusterMember) => num(m.metrics.local_send_queue), render: (m: ClusterMember) => numCell(m.metrics.local_send_queue) },
      { key: "paused", label: "Flow control paused", num: true, title: "wsrep_flow_control_paused: fraction of time since the statistics were last reset (this node only)", sort: (m: ClusterMember) => num(m.metrics.flow_control_paused), render: (m: ClusterMember) => typeof m.metrics.flow_control_paused === "number" ? percent(m.metrics.flow_control_paused) : "—" },
      { key: "cert", label: "Cert failures", num: true, title: "wsrep_local_cert_failures since start (this node only)", sort: (m: ClusterMember) => num(m.metrics.local_cert_failures), render: (m: ClusterMember) => numCell(m.metrics.local_cert_failures) },
      ...(report?.kind === "pxc" ? [{ key: "segment", label: "Segment", num: true, sort: (m: ClusterMember) => m.segment ?? null, render: (m: ClusterMember) => m.segment ?? "—" }] : []),
    ] : [
      { key: "queue", label: "Certification queue", num: true, title: "COUNT_TRANSACTIONS_IN_QUEUE (now)", sort: (m: ClusterMember) => num(m.metrics.transactions_in_queue), render: (m: ClusterMember) => numCell(m.metrics.transactions_in_queue) },
      { key: "applier", label: "Applier queue", num: true, title: "COUNT_TRANSACTIONS_REMOTE_IN_APPLIER_QUEUE (now)", sort: (m: ClusterMember) => num(m.metrics.applier_queue), render: (m: ClusterMember) => numCell(m.metrics.applier_queue) },
      { key: "conflicts", label: "Conflicts", num: true, title: "COUNT_CONFLICTS_DETECTED (cumulative)", sort: (m: ClusterMember) => num(m.metrics.conflicts_detected), render: (m: ClusterMember) => numCell(m.metrics.conflicts_detected) },
      { key: "applied", label: "Remote applied", num: true, title: "COUNT_TRANSACTIONS_REMOTE_APPLIED (cumulative)", sort: (m: ClusterMember) => num(m.metrics.remote_applied), render: (m: ClusterMember) => numCell(m.metrics.remote_applied) },
      { key: "rollback", label: "Local rollbacks", num: true, title: "COUNT_TRANSACTIONS_LOCAL_ROLLBACK (cumulative)", sort: (m: ClusterMember) => num(m.metrics.local_rollback), render: (m: ClusterMember) => numCell(m.metrics.local_rollback) },
    ]),
  ];
  const routerColumns: Column<ClusterRouter>[] = [
    { key: "name", label: "Router", primary: true, flex: true, sort: (r) => r.name || r.address || "", render: (r) => r.name || r.address || "Router" },
    // A router bootstrapped without --name is named by its host alone.
    ...(routers.some((r) => r.name) ? [{ key: "address", label: "Host", sort: (r: ClusterRouter) => r.address || "", render: (r: ClusterRouter) => r.address || "—" }] : []),
    { key: "version", label: "Version", sort: (r) => r.version || "", render: (r) => [r.product, r.version].filter(Boolean).join(" ") || "—" },
    { key: "rw", label: "RW port", num: true, sort: (r) => r.ports.RWEndpoint || "", render: (r) => r.ports.RWEndpoint || "—" },
    { key: "ro", label: "RO port", num: true, sort: (r) => r.ports.ROEndpoint || "", render: (r) => r.ports.ROEndpoint || "—" },
    { key: "split", label: "RW split port", num: true, sort: (r) => r.ports.RWSplitEndpoint || "", render: (r) => r.ports.RWSplitEndpoint || "—" },
    { key: "checkin", label: "Last check-in", sort: (r) => r.last_check_in_age_s, render: (r) => r.last_check_in
      ? <span title={`${r.last_check_in} (server time). MySQL Router writes it periodically; it is shown, not judged.`}>{ageText(r.last_check_in_age_s) || r.last_check_in}</span> : "Not recorded" },
  ];

  const tiles = report && isCluster ? clusterTiles(report) : null;
  const tone: Tone = !report || !isCluster ? "unknown" : report.status;
  return (
    <ViewLayout id="cluster" title="Cluster" className="cluster-view" onRefresh={() => { void load(); }} refreshDisabled={disabled}
      loading={!!reads.loading} otherConnection={other} inspector={content}
      inspectorEmpty={<p>Select a member to see its state, role, queues and where each value comes from.</p>}
      controls={isCluster ? (
        <Segmented<ClusterTab>
          label="Cluster sections"
          value={shownTab}
          onChange={onTab}
          options={[
            { value: "topology", label: "Topology", title: "Members joined to the group, and the routers" },
            { value: "members", label: "Members", title: "Every member with its state, role and queues" },
            ...(routersTab ? [{ value: "routers" as ClusterTab, label: "Routers", title: "MySQL Router instances registered in the metadata", count: routers.length }] : []),
          ]}
        />
      ) : undefined}
      info={<>
        <p>The cluster the connection in use belongs to: MariaDB Galera Cluster, Percona XtraDB Cluster, MySQL InnoDB Cluster or Group Replication. Members are shown as this server sees them.</p>
        <p>MariaDB Galera exposes only the connected node's own state, so the other nodes are listed without a verdict: connect to a node to judge it. Percona XtraDB Cluster names every node's state.</p>
        <p>Read-only in this release: myflames reads status and never changes membership, the primary or any setting here.</p>
      </>}
      status={<ViewStatus loading={reads.loading} error={reads.error} />}>
      {report ? <>
        <SummaryLine tone={tone} toneLabel={!report.available ? "Not available" : report.kind === "none" ? "Not a cluster" : report.status === "unknown" ? "Not judged" : undefined}
          notes={report.notes.length ? report.notes : undefined}>
          {summaryText(report)}
        </SummaryLine>
        {!report.available ? (
          <PrerequisiteGate>{report.message || "Cluster status is not available for this connection."}</PrerequisiteGate>
        ) : report.kind === "none" ? (
          <EmptyState icon="cluster" title="Not part of a cluster">
            {notClusterReason(report.message)} myflames reads MariaDB Galera Cluster, Percona XtraDB Cluster and MySQL InnoDB Cluster (and Group Replication without it).
          </EmptyState>
        ) : <>
          {tiles}
          {report.findings.length > 0 && shownTab !== "routers" && (
            <ul className="cluster-findings" aria-label="Findings">
              {report.findings.map((f, i) => (
                <li key={`${f.id}-${i}`}>
                  <StatusPill tone={f.status} />
                  {f.member ? (
                    <button type="button" className="text-button" onClick={() => selection.select(f.member!, true)}>{f.title}</button>
                  ) : <strong>{f.title}</strong>}
                  <span className="cluster-finding-text">{f.text}</span>
                </li>
              ))}
            </ul>
          )}
          {shownTab === "topology" ? (
            <ClusterTopology report={report} selection={selection} />
          ) : shownTab === "members" ? (
            <DataTable id="cluster.members" label="Cluster members" columns={memberColumns} rows={members} rowKey={memberKey} selection={selection} />
          ) : (
            routers.length ? (
              <DataTable id="cluster.routers" label="Routers" columns={routerColumns} rows={routers} rowKey={(r) => routerKey(r, routers.indexOf(r))} selection={selection} />
            ) : <EmptyState icon="cluster" title="No routers registered">No MySQL Router is registered for this cluster in the InnoDB Cluster metadata.</EmptyState>
          )}
        </>}
      </> : (
        <EmptyState icon="cluster" title="Read the cluster" action={<ToolbarButton primary disabled={disabled || !!reads.loading} onClick={() => { void load(); }}>Load</ToolbarButton>}>
          The cluster the connection in use belongs to, its members and their state. It only reads.
        </EmptyState>
      )}
    </ViewLayout>
  );
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

function clusterTiles(report: ClusterReport) {
  const c = report.cluster;
  const me = report.members.find((m) => m.local);
  const galera = report.kind === "galera" || report.kind === "pxc";
  const judged = report.summary.members - report.summary.unknown;
  return (
    <section className="cluster-tiles" aria-label="Cluster summary">
      <TileGrid label="Cluster summary">
        {galera ? (
          <MetricTile rowKey="component" label="Component" value={c.component || "Unknown"}
            tone={c.quorum === null ? undefined : c.quorum ? "ok" : "problem"} toneLabel={c.quorum === null ? undefined : c.quorum ? "Quorum" : "No quorum"} />
        ) : (
          <MetricTile rowKey="quorum" label="Quorum" value={c.quorum === null ? "Not visible" : c.quorum ? "Yes" : "No"}
            tone={c.quorum === false ? "problem" : undefined}
            trend={c.failures_tolerated !== null ? <span className="tile-hint">{`Tolerates ${plural(c.failures_tolerated, "failure")}`}</span> : undefined} />
        )}
        <MetricTile rowKey="members" label={galera ? "Nodes" : "Members"}
          value={c.size === null ? "Unknown" : c.size.toLocaleString()} unit={c.expected_size && c.expected_size !== c.size ? `of ${c.expected_size}` : undefined}
          trend={<span className="tile-hint">{[
            report.summary.ok ? `${report.summary.ok} OK` : "",
            report.summary.watch ? `${report.summary.watch} to watch` : "",
            report.summary.problem ? `${report.summary.problem} problem` : "",
            report.summary.members - judged ? `${report.summary.members - judged} not judged` : "",
          ].filter(Boolean).join(" · ")}</span>} />
        <MetricTile rowKey="this" label={galera ? "This node" : "This member"} value={me ? stateWord(me) : "Not found"}
          tone={me ? (me.status as Tone) : undefined} toneLabel={me && !galera && me.role ? ROLE_WORD[me.role] || me.role : undefined} />
        {galera ? (
          <MetricTile rowKey="flow" label="Flow control paused" value={percent(report.galera?.metrics?.flow_control_paused)}
            attrs={{ title: "wsrep_flow_control_paused on this node: the fraction of time replication was paused since the statistics were last reset (FLUSH STATUS) or since start." }}
            trend={<span className="tile-hint">{`Recv queue ${metricText(report.galera?.metrics?.local_recv_queue)}`}</span>} />
        ) : (
          <MetricTile rowKey="topology" label="Mode" value={c.topology === "multi_primary" ? "Multi-primary" : c.topology === "single_primary" ? "Single-primary" : "Unknown"}
            trend={report.kind === "innodb_cluster" ? <span className="tile-hint">{plural(report.routers.length, "router")}</span> : undefined} />
        )}
      </TileGrid>
    </section>
  );
}

function memberInspector(report: ClusterReport, m: ClusterMember): InspectorContent {
  const metrics = Object.entries(m.metrics || {}).filter(([k]) => k in METRIC_LABELS && k !== "local_state_comment");
  const g = report.galera;
  return {
    key: m.key,
    title: m.name,
    meta: <StatusPill tone={m.status as Tone} label={stateWord(m)} />,
    body: <>
      <InspectorSection title="Member">
        <Fields rows={[
          ["State", stateWord(m)],
          ["Read from", m.state_source ? SOURCE_TEXT[m.state_source] || m.state_source : "Not readable from this server"],
          !!m.role && ["Role", ROLE_WORD[m.role] || m.role],
          ["Address", memberAddress(m)],
          ["Version", memberEngine(report, m)],
          !!m.uuid && ["UUID", <code className="ops-wrap">{m.uuid}</code>],
          m.local && ["This connection", "Yes"],
          !!m.label && ["Metadata label", m.label],
          !!m.communication_stack && ["Communication stack", m.communication_stack],
          m.segment !== undefined && m.segment !== null && ["Segment", String(m.segment)],
        ]} />
        {m.status_why.map((s, i) => <p key={i}>{s}</p>)}
        <p className="hint">{memberFlow(report, m)}</p>
      </InspectorSection>
      {metrics.length > 0 && (
        <InspectorSection title={report.kind === "galera" || report.kind === "pxc" ? "This node's wsrep status" : "Group Replication counters"}>
          <Fields rows={metrics.map(([k, v]) => [METRIC_LABELS[k], metricText(v)] as [string, string])} />
          <p className="hint">{report.kind === "galera" || report.kind === "pxc"
            ? "Averages and flow control cover the time since the statistics were last reset (FLUSH STATUS) or since start."
            : "Queue lengths and rows validating are current; the other counts are cumulative. Another member's numbers are the ones it last sent to this member."}</p>
        </InspectorSection>
      )}
      {m.local && g && (
        <InspectorSection title="Node settings">
          <Fields rows={[
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
    meta: <StatusPill tone="unknown" label="Not judged" />,
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
          ["Last check-in", r.last_check_in ? `${r.last_check_in}${r.last_check_in_age_s !== null ? ` (${ageText(r.last_check_in_age_s)})` : ""}` : "Not recorded"],
        ]} />
        <p>Registered in the InnoDB Cluster metadata. MySQL Router writes its check-in time periodically: by default every tenth metadata refresh (about every 5 s), never by default in a ClusterSet, or as its stats_updates_frequency option sets. It is shown, not judged: the server cannot tell whether the router is routing now.</p>
      </InspectorSection>
    </>,
  };
}
