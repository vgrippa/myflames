// Top-level Replication place: MySQL and MariaDB replication monitoring and
// recovery (skip one transaction, skip until stopped) as a place of its own.
//
// Sections: Topology, Channels (Connections on MariaDB), Lag, GTID and
// Recovery, under the shared summary line and metric tiles. Every change goes
// through a write-policy confirmation sheet.
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { Connection } from "./api";
import { connectionKey, type Bound } from "./binding";
import { ReplicationView, type ReplicationSubTab } from "./Replication";
import type { ReplicationReport, SkipLoop } from "./opsApi";
import { NavMarker, InspectorContext, type InspectorShared, type Selection, worstTone, TONE_TEXT } from "./kit";
import { InstrumentsLinkContext } from "./Instruments";
import "./replication.css";

export function ReplicationPlace({
  connection,
  disabled = false,
  onBack,
  onBadgeChange,
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
  onBadgeChange?: (badge: ReactNode) => void;
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

  useEffect(() => {
    const currentKey = connectionKey(connection);
    if (!report || report.key !== currentKey) {
      onBadgeChange?.(null);
      return;
    }
    const rep = report.report;
    if (!rep) {
      onBadgeChange?.(null);
      return;
    }
    if (!rep.replica) {
      onBadgeChange?.(<NavMarker tone="unknown" text="no replica channels" />);
    } else if (rep.summary.failed > 0) {
      const n = rep.summary.failed;
      onBadgeChange?.(<NavMarker compact tone="problem" count={n} countTone="danger" text={`Problem, ${n} failed`} />);
    } else {
      const judged = rep.channels.filter((c) => c.judged);
      const worst = judged.length ? worstTone(judged.map((c) => c.status)) : "unknown";
      onBadgeChange?.(<NavMarker tone={worst} text={worst === "ok" ? "replicating" : TONE_TEXT[worst] || worst} />);
    }
  }, [report, connection, onBadgeChange]);

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
          />
        </div>
      </InstrumentsLinkContext.Provider>
    </InspectorContext.Provider>
  );
}
