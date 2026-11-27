// The Topology place: one connection group read whole. On a Group connection
// (a saved connection group opened as one connection) its tabs are Map,
// Members, Parameters, Accounts and Plugins; every read is one POST
// /api/live/topology section, sent only when a tab first comes on screen and
// on Refresh, never on automatic refresh, never polling. A saved connection
// in a group offers to open it as a Group connection; one without a group
// offers New group... and the topology suggestions of ConnectionGroupBar.
// Reports are kept per group connection key, membership and tab: an edit of
// the group's members makes the old membership's reports stale, and
// switching the group drops the reads on their way.
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { Connection, GroupConnection, Profile } from "./api";
import { connectionKey } from "./binding";
import { ConnectionGroupBar } from "./ConnectionGroupBar";
import { GroupDriftTable } from "./GroupDriftTable";
import { TopologyMap, mapAddress, mapEdgeLabel, mapEdgeText, mapEdgeVerification, mapNodeLabels } from "./TopologyMap";
import { RollingChangeSheet } from "./RollingChangeSheet";
import { runBinding, runMemberSpot, runTerminal, useRollingRun } from "./rollingRuns";
import {
  DataTable, EmptyState, Fields, InspectorContext, InspectorSection, Pill, Segmented, SummaryLine, ToolbarButton,
  ViewLayout, ViewStatus, type BadgeTone, type Column, type InspectorContent, type InspectorShared, type Selection,
} from "./kit";
import { plural } from "./OpsShared";
import { useViewOnScreen } from "./viewOnScreen";
import {
  readTopology, type AccountRow, type MapEdge, type ParameterRow, type PluginRow, type TopologyGroupReport,
  type TopologyMember, type TopologySection,
} from "./topologyApi";
import "./topology-place.css";

const TABS: { value: TopologySection; label: string; title: string }[] = [
  { value: "map", label: "Map", title: "The group's servers and links as verified reads report them" },
  { value: "members", label: "Members", title: "Every member of the group with its role, state and lag" },
  { value: "parameters", label: "Parameters", title: "Server parameters compared across the group" },
  { value: "accounts", label: "Accounts", title: "Accounts compared across the group" },
  { value: "plugins", label: "Plugins", title: "Plugins and components compared across the group" },
];

const ROLE_LABEL: Record<string, string> = {
  primary: "Primary", secondary: "Secondary", node: "Node", source: "Source",
  replica: "Replica", standalone: "Standalone", proxy: "Proxy", cache: "Cache",
};
const OUTCOME_LABEL: Record<string, string> = { read: "Read", failed: "Failed", not_reached: "Not reached" };
const CLUSTER_ROLES = new Set(["primary", "secondary", "node"]);
const REPLICATION_ROLES = new Set(["source", "replica"]);
const PARAMETER_STATUS: Record<string, [BadgeTone | "info", string]> = {
  same: ["ok", "Same"], different: ["warn", "Different"], missing: ["warn", "Missing"], per_server: ["info", "Per server"],
};

/** Why a Parameters row cannot be changed on every member, or "" when it can. */
function rollingChangeBlock(row: ParameterRow, report: TopologyGroupReport, connection: Connection): string {
  if (report.effective_safety.read_only || connection.read_only) return "This Group connection is Read-only.";
  if (row.status === "per_server") return row.reason || "Each member keeps its own value for this parameter.";
  const compared = report.parameters?.compared || [];
  if (row.reason) return row.reason;
  if (compared.some((id) => row.values[id] === undefined || row.values[id] === null || row.values[id] === "[redacted]"))
    return "A value is redacted or unavailable on a member.";
  return "";
}

function engineText(m: TopologyMember): string {
  return [m.product || (m.engine === "mariadb" ? "MariaDB" : m.engine === "mysql" ? "MySQL" : ""), m.version || ""]
    .filter(Boolean).join(" ");
}

function readOnlyText(m: TopologyMember): string {
  // Both flags decide: MySQL gates writes with super_read_only on top of
  // read_only, so read_only ON with super_read_only OFF is still read-only;
  // MariaDB has read_only alone (super_read_only stays null there), with raw
  // values NO_LOCK and NO_LOCK_NO_ADMIN that also refuse writes.
  if (m.read_only === null && m.super_read_only === null) return "—";
  const raw = m.read_only_value && !["ON", "OFF"].includes(m.read_only_value.toUpperCase())
    ? m.read_only_value : "";
  if (m.read_only && m.super_read_only) return "On (both)";
  if (m.super_read_only) return "On (super_read_only)";
  if (m.read_only) return `On (read_only${raw ? `=${raw}` : ""})`;
  return "Off";
}

/** The flags behind the read-only cell, for its tooltip. */
function readOnlyTitle(m: TopologyMember): string {
  const flag = (name: string, value: boolean | null) =>
    value === null ? `${name}: not read` : `${name}: ${value ? "ON" : "OFF"}`;
  return [flag("read_only", m.read_only), flag("super_read_only", m.super_read_only)].join(" · ");
}

/** A report's key: the connection's identity plus the group's membership
 * (member profile ids with their host:port, order-insensitive). Editing the
 * group's members or any member's endpoint under the same name, or
 * recreating a group under a name already read, changes the key, so the old
 * membership's reports never answer for the group as it is now. */
function reportKey(connection: Connection, group: GroupConnection | null | undefined): string {
  const membership = (group?.members.map((m) => `${m.profile_id}@${m.host}:${m.port}`) || []).sort().join(",");
  return `${connectionKey(connection)}|${membership}`;
}

export function TopologyPlace({ connection, group = null, profiles, disabled = false, onOpenGroup, onNewGroup, onUseInServer, onOpenCluster, onOpenReplication, onOpenAccounts }: {
  connection: Connection;
  /** The active Group connection, when the connection in use is one. */
  group?: GroupConnection | null;
  profiles?: Profile[];
  disabled?: boolean;
  /** Open a connection group as one Group connection, with a member probe. */
  onOpenGroup: (name: string) => void;
  /** Open the group editor in create mode, these saved connections ticked. */
  onNewGroup: (memberIds: string[]) => void;
  /** Set the Server place's member choice (memberByPlace "server"). */
  onUseInServer: (profileId: string) => void;
  onOpenCluster: () => void;
  onOpenReplication: () => void;
  /** Open Server > Users and roles with this member chosen. */
  onOpenAccounts: (profileId: string) => void;
}) {
  const [tab, setTab] = useState<TopologySection>("map");
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [changeRow, setChangeRow] = useState<ParameterRow | null>(null);
  const [changeDone, setChangeDone] = useState(false);
  // The rolling change run outlives its sheet (rollingRuns.ts): a connection
  // change, a group switch or a Refresh here never loses it; the banner
  // reopens the sheet on the run's own connection and report snapshot.
  const rollingRun = useRollingRun();
  // A run answers only for its own group incarnation: another group (or the
  // same name after a membership edit) gets the run's banner, never its
  // sheet, progress or Stop. A place without a group of its own keeps the
  // banner with Show, which reopens the sheet on the run's own connection
  // and report snapshot.
  const placeBinding = connection.group_name && group ? runBinding(connection.group_name, group.members) : null;
  const ownPlaceRun = rollingRun && (placeBinding === null || rollingRun.binding === placeBinding) ? rollingRun : null;
  const rollingSheetRun = changeRow && ownPlaceRun && ownPlaceRun.row.name === changeRow.name ? ownPlaceRun : null;
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

  const key = reportKey(connection, group);
  const isGroup = !!connection.group_name;
  const [reports, setReports] = useState<Record<string, TopologyGroupReport>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Busy is per request (connection key and tab): a slow Map read keeps its
  // slot while a Members read starts and finishes, and the visible busy
  // state is the current tab's own entry, never another tab's.
  const [busy, setBusy] = useState<Record<string, true>>({});
  const [doneAt, setDoneAt] = useState<Record<string, number>>({});
  const [startedAt, setStartedAt] = useState<Record<string, number>>({});
  const controllers = useRef(new Map<string, AbortController>());
  const tried = useRef(new Set<string>());
  const connectionRef = useRef(connection);
  connectionRef.current = connection;
  const groupRef = useRef(group);
  groupRef.current = group;

  const readTab = useCallback(async (section: TopologySection) => {
    const startKey = reportKey(connectionRef.current, groupRef.current);
    const id = `${startKey}:${section}`;
    controllers.current.get(id)?.abort();
    const controller = new AbortController();
    controllers.current.set(id, controller);
    setBusy((current) => ({ ...current, [id]: true }));
    setStartedAt((current) => ({ ...current, [id]: Date.now() }));
    setErrors((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
    let published = false;
    try {
      const report = await readTopology(connectionRef.current, section, controller.signal);
      // Only the request still registered for the tab publishes and clears
      // the spinner: a Refresh registered a newer controller, a group switch
      // cleared the map, and a superseded request leaves both to its
      // replacement.
      if (controller.signal.aborted || controllers.current.get(id) !== controller) return;
      if (reportKey(connectionRef.current, groupRef.current) !== startKey) return;
      setReports((current) => ({ ...current, [id]: report }));
      setDoneAt((current) => ({ ...current, [id]: Date.now() }));
      published = true;
    } catch (error) {
      if (controller.signal.aborted || (error as Error).name === "AbortError") return;
      if (controllers.current.get(id) !== controller) return;
      setErrors((current) => ({ ...current, [id]: (error as Error).message }));
      published = true;
    } finally {
      const registered = controllers.current.get(id);
      if (registered === controller) controllers.current.delete(id);
      if (registered === controller || registered === undefined) {
        setBusy((current) => {
          if (!(id in current)) return current;
          const next = { ...current };
          delete next[id];
          return next;
        });
        // An attempt that produced neither a report nor an error (a group
        // switch aborted it) frees the tab's first read: shown again, the
        // tab reads again.
        if (!published) tried.current.delete(id);
      }
    }
  }, []);

  // Switching the group (or leaving a Group connection) aborts every read on
  // its way; reports stay keyed to the connection that read them.
  useEffect(() => {
    const running = controllers.current;
    return () => {
      for (const controller of running.values()) controller.abort();
      running.clear();
    };
  }, [key]);

  const seenKey = useRef<string | null>(null);
  const lastGroupName = useRef<string | null>(null);
  // Switching the connection or group closes the change sheet; an unfinished
  // run keeps going in rollingRuns and the banner below reopens it.
  // A key change with the same group name is a membership edit: every report
  // of the old membership is stale, so drop it together with the selection
  // and the change sheet. An edit never reads on its own; marking the edited
  // group's tabs as tried keeps their first-show read for the next explicit
  // open or Refresh. A switch to another group keeps the first-show read.
  useEffect(() => {
    const previous = seenKey.current;
    const name = group?.group_name || null;
    const edited = previous !== null && previous !== key && lastGroupName.current !== null && lastGroupName.current === name;
    seenKey.current = key;
    lastGroupName.current = name;
    if (previous === null || previous === key) return;
    const prefix = `${key}:`;
    const dropOld = <T,>(current: Record<string, T>): Record<string, T> => {
      const ids = Object.keys(current);
      if (ids.every((id) => id.startsWith(prefix))) return current;
      const next: Record<string, T> = {};
      for (const id of ids) if (id.startsWith(prefix)) next[id] = current[id];
      return next;
    };
    setReports(dropOld);
    setErrors(dropOld);
    setBusy(dropOld);
    setDoneAt(dropOld);
    setStartedAt(dropOld);
    for (const id of Array.from(tried.current)) if (!id.startsWith(prefix)) tried.current.delete(id);
    setSelectedKey("");
    setInspectorOpen(false);
    setChangeRow(null);
    setChangeDone(false);
    if (edited) for (const t of TABS) tried.current.add(`${prefix}${t.value}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const onScreen = useViewOnScreen();
  const currentId = `${key}:${tab}`;
  // A tab reads when it first comes on screen on a Group connection, once per
  // group connection and tab. Refresh is the only other read.
  useEffect(() => {
    if (!onScreen || !isGroup || disabled) return;
    if (reports[currentId] || errors[currentId] || tried.current.has(currentId)) return;
    tried.current.add(currentId);
    void readTab(tab);
  }, [onScreen, isGroup, disabled, currentId, tab, reports, errors, readTab]);

  const profile = profiles?.find((p) => p.id === connection.profile_id);
  const savedGroup = (profile?.group || connection.group || "").trim();

  const memberName = useCallback((profileId: string) => {
    const report = reports[currentId];
    const found = report?.members.find((m) => m.profile_id === profileId)
      || group?.members.find((m) => m.profile_id === profileId);
    return found?.profile_name || profileId;
  }, [reports, currentId, group]);

  const serverCount = group?.members.length || 0;
  const busyHere = busy[currentId] === true;
  const busyText = busyHere
    ? tab === "accounts"
      ? `Reading accounts on ${plural(serverCount, "server")}. Accounts are read one at a time, so this can take a few minutes.`
      : `Reading ${plural(serverCount, "server")}…`
    : "";
  const error = errors[currentId] || "";
  const report = reports[currentId] || null;

  const membersContent: InspectorContent | null = useMemo(() => {
    if ((tab !== "members" && tab !== "map") || !report || !selection.key) return null;
    const member = report.members.find((m) => m.profile_id === selection.key);
    if (!member) return null;
    const aliasName = member.duplicate_of
      ? report.members.find((other) => other.profile_id === member.duplicate_of)?.profile_name || member.duplicate_of
      : "";
    const actions = (
      <div className="topology-member-actions">
        <ToolbarButton className="small" onClick={() => onUseInServer(member.profile_id)}>Use in Server views</ToolbarButton>
        {member.role && CLUSTER_ROLES.has(member.role) && (
          <ToolbarButton className="small" onClick={onOpenCluster}>Open in Cluster</ToolbarButton>
        )}
        {member.role && REPLICATION_ROLES.has(member.role) && (
          <ToolbarButton className="small" onClick={onOpenReplication}>Open in Replication</ToolbarButton>
        )}
      </div>
    );
    return {
      key: member.profile_id,
      title: member.profile_name,
      meta: <Pill tone={member.outcome === "read" ? "ok" : member.outcome === "failed" ? "danger" : "warn"}>{OUTCOME_LABEL[member.outcome]}</Pill>,
      actions,
      body: <>
        <InspectorSection title="Member">
          <Fields rows={[
            ["Server", `${member.profile_name} (${member.host}:${member.port})`],
            !!member.role && ["Role", ROLE_LABEL[member.role] || member.role],
            !!member.state && ["State", member.state],
            ["Engine and version", engineText(member) || "Not read"],
            ["Read only", readOnlyText(member)],
            member.super_read_only !== null && ["super_read_only", member.super_read_only ? "On" : "Off"],
            !!member.hostname && ["Hostname", member.hostname],
            !!member.server_uuid && ["UUID", <code className="ops-wrap">{member.server_uuid}</code>],
            member.lag && ["Lag", `${member.lag.value === null ? "Not reported" : member.lag.value.toLocaleString()} (${member.lag.kind})`],
            member.lag && ["Lag message", member.lag.message],
            member.behind && ["Behind", `${member.behind.value.toLocaleString()} (${member.behind.kind})`],
            member.behind && ["Behind message", member.behind.message],
            !member.behind && !!member.behind_reason && ["Behind", member.behind_reason],
            ["Read", `${OUTCOME_LABEL[member.outcome]} in ${member.elapsed_s.toLocaleString()} s`],
            ["Message", member.message],
          ]} />
        </InspectorSection>
        {member.duplicate_of && (
          <InspectorSection title="Alias">
            <p>
              This saved connection reaches the same server as {aliasName}{" "}
              (for example a router or proxy port). A rolling change refuses it when {aliasName} is also selected.{" "}
              The map shows both saved connections, linked as aliases; {aliasName} represents the server in its cluster. Drift tables list both.
            </p>
          </InspectorSection>
        )}
        {member.channels.length > 0 && (
          <InspectorSection title="Channels">
            {member.channels.map((channel, index) => (
              <Fragment key={`${channel.name}-${index}`}>
                <Fields rows={[
                  ["Channel", channel.name || "(default)"],
                  ["Source", `${channel.source_host || "?"}:${channel.source_port ?? "?"}`],
                  ["IO running", channel.io_running || "Not reported"],
                  ["SQL running", channel.sql_running || "Not reported"],
                  ["Seconds behind", channel.seconds_behind === null ? "Not reported" : channel.seconds_behind.toLocaleString()],
                ]} />
              </Fragment>
            ))}
          </InspectorSection>
        )}
        {tab === "map" && <MapNodeLinks report={report} nodeId={report.map?.nodes.find((node) => node.profile_id === member.profile_id)?.id || member.profile_id} />}
      </>,
    };
  }, [tab, report, selection.key, onUseInServer, onOpenCluster, onOpenReplication]);

  const peerContent: InspectorContent | null = useMemo(() => {
    if (tab !== "map" || !report?.map || !selection.key) return null;
    const peer = report.map.nodes.find((node) => node.id === selection.key);
    if (!peer || peer.kind !== "peer") return null;
    const peerLabel = !peer.host && /^\?(?::\d+)?$/.test(peer.label) ? "Replica without a reported address" : peer.label;
    return {
      key: peer.id,
      title: peerLabel,
      meta: <Pill tone="neutral">Not in this group</Pill>,
      body: <>
        <InspectorSection title="Reported peer">
          <Fields rows={[
            ["Address", mapAddress(peer)],
            ["Reported by", peer.seen_by.map(memberName).join(", ") || "Not reported"],
          ]} />
          <p>{peer.host
            ? `Not in this group: add a saved connection for ${mapAddress(peer)} to this group to read it.`
            : "Not in this group: add a saved connection for this server to this group to read it."}</p>
        </InspectorSection>
        <MapNodeLinks report={report} nodeId={peer.id} />
      </>,
    };
  }, [tab, report, selection.key, memberName]);

  const accountsContent: InspectorContent | null = useMemo(() => {
    if (tab !== "accounts" || !report?.accounts || !selection.key) return null;
    const row = report.accounts.rows.find((r) => r.account === selection.key);
    if (!row) return null;
    const compared = report.accounts!.compared;
    return {
      key: row.account,
      title: row.account,
      meta: <Pill tone={row.status === "same" ? "ok" : "warn"}>{row.status === "same" ? "Same" : row.status === "missing" ? "Missing" : "Different"}</Pill>,
      actions: compared.length ? (
        <div className="topology-member-actions">
          {compared.map((id) => (
            <ToolbarButton key={id} className="small" onClick={() => onOpenAccounts(id)}>
              Open in Users and roles on {memberName(id)}
            </ToolbarButton>
          ))}
        </div>
      ) : undefined,
      body: (
        <InspectorSection title="Differences">
          {row.differences.length === 0 ? (
            <p>No differences on the compared members.</p>
          ) : (
            <ul className="topology-differences">
              {row.differences.map((difference, index) => (
                <li key={`${difference.profile_id}-${difference.kind}-${index}`}>
                  <strong>{memberName(difference.profile_id)}</strong> ({difference.kind}): {difference.detail}
                </li>
              ))}
            </ul>
          )}
        </InspectorSection>
      ),
    };
  }, [tab, report, selection.key, comparedIds(report), memberName, onOpenAccounts]);

  const parametersContent: InspectorContent | null = useMemo(() => {
    if (tab !== "parameters" || !report?.parameters || !selection.key) return null;
    const row = report.parameters.rows.find((r) => r.name === selection.key);
    if (!row) return null;
    const section = report.parameters;
    const [tone, word] = PARAMETER_STATUS[row.status] || PARAMETER_STATUS.same;
    const blocked = rollingChangeBlock(row, report, connection);
    return {
      key: row.name,
      title: row.name,
      meta: <Pill tone={tone}>{word}</Pill>,
      actions: (
        <div className="topology-member-actions">
          <ToolbarButton className="small" primary disabled={!!blocked} title={blocked || undefined}
            onClick={() => { if (!blocked) setChangeRow(row); }}>Change on every member…</ToolbarButton>
          {blocked && <p className="hint topology-change-blocked">{blocked}</p>}
        </div>
      ),
      body: (
        <InspectorSection title="Parameter">
          <Fields rows={[
            ["Apply type", row.apply_type || (row.dynamic === null ? "Unknown" : row.dynamic ? "Dynamic" : "Restart required")],
            ...section.compared.map((id): [string, ReactNode] => [
              memberName(id),
              row.values[id] === undefined || row.values[id] === null
                ? <span className="drift-cell-missing">Not present</span>
                : String(row.values[id]),
            ]),
            !!row.reason && ["Note", row.reason],
          ]} />
        </InspectorSection>
      ),
    };
  }, [tab, report, selection.key, connection, memberName]);

  const content = membersContent || peerContent || accountsContent || parametersContent;

  const groupTabs = (
    <Segmented<TopologySection>
      label="Topology sections"
      value={tab}
      onChange={(next) => { setTab(next); setSelectedKey(""); setInspectorOpen(false); }}
      options={TABS.map((t) => ({ value: t.value, label: t.label, title: t.title }))}
    />
  );

  const summary = report ? summaryText(report) : "";
  const drift = report ? driftSection(report, tab) : null;

  return (
    <InspectorContext.Provider value={inspector}>
      <div className="topology-place">
        <ViewLayout
          id="topology"
          title="Topology"
          controls={isGroup ? groupTabs : undefined}
          onRefresh={isGroup ? () => { setChangeDone(false); void readTab(tab); } : undefined}
          refreshDisabled={disabled}
          loading={!!busyText}
          inspector={content}
          inspectorEmpty={<p>Select a row to see its details.</p>}
          info={<>
            <p>The whole connection group of the connection in use: its members, their roles and state, and how parameters, accounts and plugins differ across them.</p>
            <p>A tab reads once when it first comes on screen, and again on Refresh. Reading changes nothing: the read logs in to each saved member of the group, one bounded read each, and draws links only from verified identity matches.</p>
          </>}
          status={<ViewStatus
            loading={busyText || undefined}
            error={error || undefined}
            startedAt={startedAt[currentId]}
            doneAt={doneAt[currentId]}
            onRetry={error ? () => { void readTab(tab); } : undefined}
          />}
        >
          {rollingRun && !rollingSheetRun && (ownPlaceRun ? (
            <p className="rolling-run-banner" role="status">
              {runTerminal(ownPlaceRun)
                ? `Rolling change finished: ${ownPlaceRun.plan.name}.`
                : `Rolling change running: ${ownPlaceRun.plan.name}, server ${runMemberSpot(ownPlaceRun).at} of ${runMemberSpot(ownPlaceRun).of}.`}
              {" "}
              <button type="button" className="text-button" onClick={() => setChangeRow(ownPlaceRun.row)}>Show</button>
            </p>
          ) : !runTerminal(rollingRun) ? (
            <p className="rolling-run-banner" role="status">
              {`Rolling change running in group ${rollingRun.plan.group_name || rollingRun.connection.group_name}.`}
            </p>
          ) : null)}
          {!isGroup ? (
            <NonGroupState
              connection={connection}
              profiles={profiles}
              savedGroup={savedGroup}
              onOpenGroup={onOpenGroup}
              onNewGroup={onNewGroup}
            />
          ) : report ? (
            tab === "members" ? (
              <MembersTab report={report} summary={summary} selection={selection} />
            ) : tab === "map" ? (
              <MapTab report={report} memberName={memberName} selection={selection} />
            ) : drift ? (
              <>
                {tab === "parameters" && (changeDone || !!(ownPlaceRun && runTerminal(ownPlaceRun))) && (
                  <p className="hint topology-change-done">A rolling change finished. Refresh reads the group again; nothing was re-read on its own.</p>
                )}
                <DriftTab tab={tab} drift={drift} report={report} memberName={memberName} selection={selection} />
              </>
            ) : null
          ) : busyHere ? null : (
            <EmptyState
              icon="globe"
              title={error ? "Could not read the group" : "Read the group"}
              action={<ToolbarButton primary disabled={disabled} onClick={() => { void readTab(tab); }}>{error ? "Retry" : "Load"}</ToolbarButton>}
            >
              {error || "The whole connection group of the connection in use. Reading changes nothing."}
            </EmptyState>
          )}
        </ViewLayout>
        {changeRow && (rollingSheetRun || report?.parameters) && (
          <RollingChangeSheet
            connection={rollingSheetRun ? rollingSheetRun.connection : connection}
            report={rollingSheetRun ? rollingSheetRun.report : report!}
            row={changeRow}
            onClose={() => setChangeRow(null)}
            onChanged={() => setChangeDone(true)}
          />
        )}
      </div>
    </InspectorContext.Provider>
  );
}

function comparedIds(report: TopologyGroupReport | null): string {
  return report?.accounts ? report.accounts.compared.join(",") : "";
}

function summaryText(report: TopologyGroupReport): string {
  const versions = report.summary.versions;
  const drift = versions.length > 1 ? `${versions.length} versions: ${versions.join(", ")} · ` : "";
  return `${drift}${report.summary.text}`;
}

function driftSection(report: TopologyGroupReport, tab: TopologySection) {
  if (tab === "parameters") return report.parameters || null;
  if (tab === "accounts") return report.accounts || null;
  if (tab === "plugins") return report.plugins || null;
  return null;
}

/** The four states of a connection that is not a Group connection. */
function NonGroupState({ connection, profiles, savedGroup, onOpenGroup, onNewGroup }: {
  connection: Connection;
  profiles?: Profile[];
  savedGroup: string;
  onOpenGroup: (name: string) => void;
  onNewGroup: (memberIds: string[]) => void;
}) {
  if (!connection.host && !connection.profile_id) {
    return (
      <EmptyState icon="globe" title="No connection">
        Connect to a server to read the topology of its connection group.
      </EmptyState>
    );
  }
  if (!connection.profile_id) {
    return (
      <EmptyState icon="globe" title="Not a saved connection">
        Save this connection to add it to a group.
      </EmptyState>
    );
  }
  if (savedGroup) {
    return (
      <EmptyState
        icon="globe"
        title={`This connection is in group ${savedGroup}.`}
        action={<ToolbarButton primary onClick={() => onOpenGroup(savedGroup)}>Open group connection {savedGroup}</ToolbarButton>}
      >
        Topology reads the whole group on a Group connection.
      </EmptyState>
    );
  }
  return (
    <>
      <p className="topology-member-note">
        Topology reads a whole group of servers. This connection is not in a group.
      </p>
      <div className="topology-group-actions">
        <ToolbarButton primary onClick={() => onNewGroup(connection.profile_id ? [connection.profile_id] : [])}>New group…</ToolbarButton>
      </div>
      <ConnectionGroupBar group={null} connection={connection} profiles={profiles} />
    </>
  );
}

function MembersTab({ report, summary, selection }: { report: TopologyGroupReport; summary: string; selection: Selection }) {
  const columns: Column<TopologyMember>[] = [
    {
      key: "server", label: "Server", primary: true, flex: true, width: 160, minWidth: 140,
      sort: (m) => m.profile_name,
      render: (m) => (
        <span className="cell-name">
          <strong className="cell-truncate" title={m.profile_name}>{m.profile_name}</strong>
          <span className="topology-map-detail">{m.host}:{m.port}</span>
          {m.duplicate_of && (
            <span className="topology-alias-badge">
              Alias of {report.members.find((other) => other.profile_id === m.duplicate_of)?.profile_name || m.duplicate_of}
            </span>
          )}
        </span>
      ),
    },
    { key: "role", label: "Role", sort: (m) => m.role || "", render: (m) => (m.role ? ROLE_LABEL[m.role] || m.role : "—") },
    { key: "state", label: "State", sort: (m) => m.state || "", render: (m) => m.state || "—" },
    {
      key: "engine", label: "Engine and version", sort: (m) => engineText(m),
      render: (m) => engineText(m) ? <span title={m.version_comment || engineText(m)}>{engineText(m)}</span> : "—",
    },
    {
      key: "readonly", label: "Read only", sort: (m) => readOnlyText(m),
      render: (m) => <span title={readOnlyTitle(m)}>{readOnlyText(m)}</span>,
    },
    {
      key: "lag", label: "Lag", num: true, sort: (m) => m.lag?.value ?? null,
      render: (m) => m.lag
        ? <span title={`${m.lag.kind}: ${m.lag.message}`}>{m.lag.value === null ? "—" : m.lag.value.toLocaleString()}</span>
        : "—",
    },
    {
      key: "behind", label: "Behind", num: true, sort: (m) => m.behind?.value ?? null,
      render: (m) => m.behind
        ? <span title={`${m.behind.kind}: ${m.behind.message}`}>{m.behind.value.toLocaleString()}</span>
        : m.behind_reason ? <span title={m.behind_reason}>—</span> : "—",
    },
    {
      key: "read", label: "Read", sort: (m) => m.outcome,
      render: (m) => m.outcome === "read"
        ? OUTCOME_LABEL.read
        : <span title={m.message}>{OUTCOME_LABEL[m.outcome]}{m.message ? ` · ${m.message}` : ""}</span>,
    },
  ];
  return (
    <>
      <SummaryLine notesTitle="What the summary counts">{summary}</SummaryLine>
      <DataTable id="topology.members" label="Group members" columns={columns} rows={report.members} rowKey={(m) => m.profile_id} selection={selection} />
    </>
  );
}

function DriftTab({ tab, drift, report, memberName, selection }: {
  tab: TopologySection;
  drift: NonNullable<TopologyGroupReport["parameters"] | TopologyGroupReport["accounts"] | TopologyGroupReport["plugins"]>;
  report: TopologyGroupReport;
  memberName: (profileId: string) => string;
  selection: Selection;
}) {
  const members = drift.compared.map((id) => ({ id, name: memberName(id) }));
  const common = {
    members,
    notCompared: drift.not_compared,
    notes: "notes" in drift ? drift.notes : undefined,
    memberName,
    selection,
  };
  if (tab === "parameters") {
    const section = report.parameters!;
    return (
      <GroupDriftTable<ParameterRow>
        id="topology.parameters" label="Parameters" itemLabel="Parameter"
        rows={section.rows} rowKey={(row) => row.name} itemName={(row) => row.name}
        cell={(row, id) => {
          const value = row.values[id];
          if (value === undefined || value === null) return <span className="drift-cell-missing">Not present</span>;
          return <span title={String(value)}>{String(value)}</span>;
        }}
        {...common}
      />
    );
  }
  if (tab === "accounts") {
    const section = report.accounts!;
    return (
      <GroupDriftTable<AccountRow>
        id="topology.accounts" label="Accounts" itemLabel="Account"
        rows={section.rows} rowKey={(row) => row.account} itemName={(row) => row.account}
        cell={(row, id) => row.present_on.includes(id)
          ? "Present"
          : <span className="drift-cell-missing">Not present</span>}
        {...common}
      />
    );
  }
  const section = report.plugins!;
  return (
    <GroupDriftTable<PluginRow>
      id="topology.plugins" label="Plugins" itemLabel="Plugin"
      rows={section.rows} rowKey={(row) => `${row.kind}:${row.name}`} itemName={(row) => row.name}
      cell={(row, id) => {
        const value = row.values[id];
        if (!value) return <span className="drift-cell-missing">Not present</span>;
        return <span title={[value.status, value.version].filter(Boolean).join(" · ")}>
          {[value.status, value.version].filter(Boolean).join(" · ")}
        </span>;
      }}
      {...common}
    />
  );
}

/** The diagram and its accessible text alternative share the same report. */
function MapTab({ report, memberName, selection }: { report: TopologyGroupReport; memberName: (profileId: string) => string; selection: Selection }) {
  const map = report.map;
  const indexed = useMemo(() => map ? {
    labels: mapNodeLabels(map),
    members: map.nodes.filter((node) => node.kind === "member"),
    peers: map.nodes.filter((node) => node.kind === "peer"),
  } : null, [map]);
  // Small server lists stay complete; larger ones use the same 200-row
  // pages as links. Each new report, including Refresh, resets the pages.
  const firstPage = { map, members: (indexed?.members.length || 0) > 500 ? 200 : 500,
    peers: (indexed?.peers.length || 0) > 500 ? 200 : 500, links: 200 };
  const [pages, setPages] = useState(firstPage);
  const visible = pages.map === map ? pages : firstPage;
  if (!map) return null;
  const { labels, members, peers } = indexed!;
  const labelOf = (id: string) => labels.get(id) || id;
  const showMore = (list: "members" | "peers" | "links") =>
    setPages({ ...visible, [list]: visible[list] + 200 });
  const edgeText = (edge: MapEdge) =>
    edge.kind === "alias"
      ? mapEdgeLabel(edge)
      : `${mapEdgeLabel(edge)}${edge.state ? ` · ${edge.state}` : ""} · ${mapEdgeVerification(edge)}${edge.reason ? ` · ${edge.reason}` : ""}`;
  // Cap notes explain what the diagram left out, so they sit above it; every
  // note shows once.
  const capNotes = map.notes.filter((note) => /^Map \w+ capped at /.test(note.message));
  const otherNotes = map.notes.filter((note) => !capNotes.includes(note));
  const noteText = (note: { profile_id: string; message: string }) =>
    `${note.profile_id ? `${memberName(note.profile_id)}: ` : ""}${note.message}`;
  return (
    <>
      {capNotes.length > 0 && (
        <div className="topology-map-caps" role="note">
          {capNotes.map((note, index) => (
            <p key={`${note.profile_id}-${index}`} className="hint">{noteText(note)}</p>
          ))}
        </div>
      )}
      <TopologyMap map={map} selection={selection} />
      <section className="topology-map-section" aria-label="Servers">
        <h3>Servers</h3>
        <ul className="topology-map-list">
          {members.slice(0, visible.members).map((node) => (
            <li key={node.id} data-node={node.id}>
              <span className="topology-map-label">{node.label}</span>
              <span className="topology-map-detail">
                {mapAddress(node)}
                {node.role ? ` · ${ROLE_LABEL[node.role] || node.role}` : ""}
                {node.state ? ` · ${node.state}` : ""}
                {node.outcome !== "read" ? ` · ${OUTCOME_LABEL[node.outcome]}` : ""}
                {node.duplicate_of ? ` · Alias of ${labelOf(node.duplicate_of)}` : ""}
              </span>
            </li>
          ))}
        </ul>
        {members.length > visible.members && <button type="button" className="button" onClick={() => showMore("members")}>Show 200 more</button>}
        {peers.length > 0 && (
          <div className="topology-map-peers">
            <h4>Not in this group</h4>
            <ul className="topology-map-list">
              {peers.slice(0, visible.peers).map((node) => (
                <li key={node.id} data-node={node.id}>
                  <span className="topology-map-label">{node.label}</span>
                  <span className="topology-map-detail">
                    {node.reason}
                    {node.kind === "peer" && node.seen_by.length > 0 ? ` · reported by ${node.seen_by.map(memberName).join(", ")}` : ""}
                  </span>
                </li>
              ))}
            </ul>
            {peers.length > visible.peers && <button type="button" className="button" onClick={() => showMore("peers")}>Show 200 more</button>}
          </div>
        )}
      </section>
      <section className="topology-map-section" aria-label="Links">
        <h3>Links</h3>
        {map.edges.length === 0 ? (
          <p className="hint">No verified links between the members.</p>
        ) : (
          <ul className="topology-map-list">
            {map.edges.slice(0, visible.links).map((edge, index) => (
              <li key={`${edge.from}-${edge.to}-${edge.kind}-${index}`} data-edge-kind={edge.kind} data-verified={edge.verified ? "true" : "false"}>
                <span className="topology-map-label">{labelOf(edge.from)} → {labelOf(edge.to)}</span>
                <span className="topology-map-detail">{edgeText(edge)}</span>
              </li>
            ))}
          </ul>
        )}
        {map.edges.length > visible.links && <button type="button" className="button" onClick={() => showMore("links")}>Show 200 more</button>}
      </section>
      {map.clusters.length > 0 && (
        <section className="topology-map-section" aria-label="Clusters">
          <h3>Clusters</h3>
          <ul className="topology-map-list">
            {map.clusters.map((cluster) => (
              <li key={cluster.id} data-cluster={cluster.id}>
                <span className="topology-map-label">{cluster.name}</span>
                <span className="topology-map-detail">
                  {cluster.kind.replace(/_/g, " ")} · {plural(cluster.members.length, "member")}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {otherNotes.length > 0 && (
        <section className="topology-map-section topology-map-notes" aria-label="Notes">
          <h3>Notes</h3>
          {otherNotes.map((note, index) => (
            <p key={`${note.profile_id}-${index}`} className="hint">{noteText(note)}</p>
          ))}
        </section>
      )}
    </>
  );
}

/** Incident links stay available to keyboard users through the node inspector. */
function MapNodeLinks({ report, nodeId }: { report: TopologyGroupReport; nodeId: string }) {
  const map = report.map;
  if (!map) return null;
  const edges = map.edges.filter((edge) => edge.from === nodeId || edge.to === nodeId);
  if (!edges.length) return null;
  const label = (id: string) => map.nodes.find((node) => node.id === id)?.label || id;
  return <InspectorSection title="Links">
    {edges.map((edge, index) => <Fields key={`${edge.from}-${edge.to}-${index}`} rows={[
      ["Link", `${label(edge.from)} → ${label(edge.to)}`],
      ["State and verification", mapEdgeText(edge)],
      ["Evidence", edge.evidence || "Not reported"],
    ]} />)}
  </InspectorSection>;
}
