// Top-level Replication place: MySQL and MariaDB replication monitoring and
// recovery (skip one transaction, skip until stopped) as a place of its own.
//
// Sections: Topology, Channels (Connections on MariaDB), Lag, GTID and
// Recovery, under the shared summary line and metric tiles. Every change goes
// through a write-policy confirmation sheet.
import { useEffect, useMemo, useRef, useState } from "react";
import type { Connection } from "./api";
import { connectionKey, connectionLabel, endpointKey, type Bound } from "./binding";
import { ConnectionGroupBar } from "./ConnectionGroupBar";
import { ReplicationView, type ReplicationSubTab } from "./Replication";
import { readReplication, type ReplicationReport, type SkipLoop } from "./opsApi";
import type { ClusterKind } from "./clusterApi";
import { InspectorContext, useWindowStatus, type InspectorShared, type Selection } from "./kit";
import { InstrumentsLinkContext } from "./Instruments";
import { engineName } from "./OpsShared";
import "./replication.css";

const SHEET_WIDTH = 1100;

export function ReplicationPlace({
  connection,
  disabled = false,
  onBack,
  clusterKind = null,
  onOpenCluster,
  onOpenReadyset,
  onOpenProxySql,
  onSwitchConnection,
  onOpenInstruments,
  loops: propLoops,
  onLoops,
  otherLoops = [],
  otherLoopStopping = null,
  otherLoopErrors = {},
  onStopOtherLoop,
  onLoopActivity,
  report: propReport,
  onReport,
  restoredLoop = false,
  restoredStoppedKeys = [],
  restoredOutsideNote = "",
}: {
  connection: Connection;
  disabled?: boolean;
  onBack?: () => void;
  /** The cluster the member this place reads belongs to, once the Cluster
   * place or Refresh all read it on that same member (a node that is not a
   * replica points there). The sidebar's marker is drawn by the shell from
   * the shared report. */
  clusterKind?: ClusterKind | null;
  onOpenCluster?: () => void;
  onOpenReadyset?: () => void;
  onOpenProxySql?: () => void;
  onSwitchConnection?: (profileId: string) => void;
  onOpenInstruments?: (row: string) => void;
  /** Skip loops per connectionKey, owned by the shell. */
  loops?: Record<string, Bound<SkipLoop[]>>;
  onLoops?: (loops: Bound<SkipLoop[]>) => void;
  /** Active skip loops running on other members of the active group (or a
   * restored same-group member), with their server label and Stop handler. */
  otherLoops?: { bound: Bound<SkipLoop[]>; server: string; stale?: boolean }[];
  otherLoopStopping?: { key: string; id: string } | null;
  otherLoopErrors?: Record<string, string>;
  onStopOtherLoop?: (bound: Bound<SkipLoop[]>, loop: SkipLoop) => void;
  onLoopActivity?: (active: boolean) => void;
  /** Owned by the shell so Server's Refresh all feeds this place too. */
  report?: Bound<ReplicationReport> | null;
  onReport?: (report: Bound<ReplicationReport>) => void;
  /** The restored workspace had a skip loop running on this connection: read
   * loop status at once (it reads no database) and say if it stopped with
   * myflames. */
  restoredLoop?: boolean;
  /** Keys whose restored skip loop was already confirmed stopped by a restart. */
  restoredStoppedKeys?: string[];
  /** Note when a restored loop's member is outside the active connection's group. */
  restoredOutsideNote?: string;
}) {
  const [localReport, setLocalReport] = useState<Bound<ReplicationReport> | null>(null);
  const report = propReport !== undefined ? propReport : localReport;
  const setReport = (next: Bound<ReplicationReport>) => {
    setLocalReport(next);
    onReport?.(next);
  };
  const [localLoops, setLocalLoops] = useState<Record<string, Bound<SkipLoop[]>>>({});
  const allLoops = propLoops !== undefined ? propLoops : localLoops;
  const handleLoops = (nextLoops: Bound<SkipLoop[]>) => {
    if (propLoops === undefined) {
      setLocalLoops((current) => {
        const endpoint = endpointKey(nextLoops.key);
        const updated: Record<string, Bound<SkipLoop[]>> = {};
        for (const [k, v] of Object.entries(current)) {
          if (k !== nextLoops.key && endpointKey(k) === endpoint) continue;
          updated[k] = v;
        }
        updated[nextLoops.key] = nextLoops;
        return updated;
      });
    }
    onLoops?.(nextLoops);
  };
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(320);
  const [activeTab, setActiveTab] = useState<ReplicationSubTab>("topology");
  const [, setLoopActiveNow] = useState(false);
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
  const ownLoops =
    allLoops[key] ||
    Object.values(allLoops).find((b) => endpointKey(b.key) === endpointKey(key)) ||
    null;
  const sameEndpointLoops = !!ownLoops;
  const current = report && report.key === key ? report.report : null;
  useWindowStatus(`replication:${key}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
    engineVersion: current && current.server_version ? engineName(current.engine, current.server_version, current.server_product, current.server_base) : undefined,
  });

  const showBar =
    !!current &&
    current.available &&
    (activeTab === "topology" || (!current.replica && !!current.connection_group));

  return (
    <InspectorContext.Provider value={inspector}>
      <InstrumentsLinkContext.Provider value={onOpenInstruments || (() => {})}>
        <div ref={placeRef} className="replication-place">
          <ReplicationView
            topologyHeader={showBar ? (
              <ConnectionGroupBar
                group={current?.connection_group}
                connection={connection}
                disabled={disabled}
                onReload={async () => {
                  const fresh = await readReplication(connection);
                  setReport({ key, label: connectionLabel(connection), report: fresh });
                }}
              />
            ) : null}
            connection={connection}
            disabled={disabled}
            onBack={onBack}
            selection={selection}
            report={report}
            onReport={setReport}
            loops={ownLoops}
            sameEndpoint={sameEndpointLoops}
            onLoops={handleLoops}
            otherLoops={otherLoops}
            otherLoopStopping={otherLoopStopping}
            otherLoopErrors={otherLoopErrors}
            onStopOtherLoop={onStopOtherLoop}
            restoredLoop={restoredLoop}
            restoredStoppedKeys={restoredStoppedKeys}
            restoredOutsideNote={restoredOutsideNote}
            onLoopActivity={(active) => {
              setLoopActiveNow(active);
              onLoopActivity?.(active);
            }}
            activeTab={activeTab}
            onTabChange={setActiveTab}
            clusterKind={clusterKind}
            onOpenCluster={onOpenCluster}
            onOpenReadyset={onOpenReadyset}
            onOpenProxySql={onOpenProxySql}
            onSwitchConnection={onSwitchConnection}
          />
        </div>
      </InstrumentsLinkContext.Provider>
    </InspectorContext.Provider>
  );
}
