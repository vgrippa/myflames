// Top-level Replication place: MySQL and MariaDB replication monitoring and
// recovery (skip one transaction, skip until stopped) as a place of its own.
//
// Sections: Topology, Channels (Connections on MariaDB), Lag, GTID and
// Recovery, under the shared summary line and metric tiles. Every change goes
// through a write-policy confirmation sheet.
import { useMemo, useState } from "react";
import type { Connection } from "./api";
import { connectionKey, connectionLabel, type Bound } from "./binding";
import { ReplicationView, type ReplicationSubTab } from "./Replication";
import type { ReplicationReport, SkipLoop } from "./opsApi";
import type { ClusterKind } from "./clusterApi";
import { InspectorContext, useWindowStatus, type InspectorShared, type Selection } from "./kit";
import { InstrumentsLinkContext } from "./Instruments";
import { engineName } from "./OpsShared";
import "./replication.css";

export function ReplicationPlace({
  connection,
  disabled = false,
  onBack,
  clusterKind = null,
  onOpenCluster,
  onOpenInstruments,
  loops: propLoops,
  onLoops,
  onLoopActivity,
  report: propReport,
  onReport,
  restoredLoop = false,
}: {
  connection: Connection;
  disabled?: boolean;
  onBack?: () => void;
  /** The cluster the connection in use belongs to, once the Cluster place or
   * Refresh all read it (a node that is not a replica points there). The
   * sidebar's marker is drawn by the shell from the shared report. */
  clusterKind?: ClusterKind | null;
  onOpenCluster?: () => void;
  onOpenInstruments?: (row: string) => void;
  loops?: Bound<SkipLoop[]> | null;
  onLoops?: (loops: Bound<SkipLoop[]>) => void;
  onLoopActivity?: (active: boolean) => void;
  /** Owned by the shell so Server's Refresh all feeds this place too. */
  report?: Bound<ReplicationReport> | null;
  onReport?: (report: Bound<ReplicationReport>) => void;
  /** The restored workspace had a skip loop running: read loop status at
   * once (it reads no database) and say if it stopped with myflames. */
  restoredLoop?: boolean;
}) {
  const [localReport, setLocalReport] = useState<Bound<ReplicationReport> | null>(null);
  const report = propReport !== undefined ? propReport : localReport;
  const setReport = (next: Bound<ReplicationReport>) => {
    setLocalReport(next);
    onReport?.(next);
  };
  const [localLoops, setLocalLoops] = useState<Bound<SkipLoop[]> | null>(null);
  const effectiveLoops = propLoops !== undefined ? propLoops : localLoops;
  const handleLoops = (nextLoops: Bound<SkipLoop[]>) => {
    setLocalLoops(nextLoops);
    onLoops?.(nextLoops);
  };
  const [selectedKey, setSelectedKey] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [inspectorWidth, setInspectorWidth] = useState(360);
  const [activeTab, setActiveTab] = useState<ReplicationSubTab>("topology");
  const [, setLoopActiveNow] = useState(false);

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
      sheet: false,
    }),
    [inspectorOpen, selectedKey, inspectorWidth],
  );
  const key = connectionKey(connection);
  const current = report && report.key === key ? report.report : null;
  useWindowStatus(`replication:${key}`, {
    connectionState: connection.host ? connectionLabel(connection) : "No connection",
    engineVersion: current && current.server_version ? engineName(current.engine, current.server_version) : undefined,
  });

  return (
    <InspectorContext.Provider value={inspector}>
      <InstrumentsLinkContext.Provider value={onOpenInstruments || (() => {})}>
        <div className="replication-place">
          <ReplicationView
            connection={connection}
            disabled={disabled}
            onBack={onBack}
            selection={selection}
            report={report}
            onReport={setReport}
            loops={effectiveLoops}
            onLoops={handleLoops}
            restoredLoop={restoredLoop}
            onLoopActivity={(active) => {
              setLoopActiveNow(active);
              onLoopActivity?.(active);
            }}
            activeTab={activeTab}
            onTabChange={setActiveTab}
            clusterKind={clusterKind}
            onOpenCluster={onOpenCluster}
          />
        </div>
      </InstrumentsLinkContext.Provider>
    </InspectorContext.Provider>
  );
}
