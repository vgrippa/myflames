// ReplicationTopology: a static map of the replication topology.
//
// One row per channel (MySQL) or connection (MariaDB): the source it reads
// from on the left, a connector in the middle, the replica on the right.
// Nothing here moves. Every state is drawn by line style, color and a text
// label, from fields the report already measured:
//   - IO and SQL threads running: solid line, "Running"
//   - a thread stopped, no error: dashed line, which thread is stopped
//   - an error recorded: solid danger line, "Error <code>"
//   - lag: measured seconds as text, "Lag unknown" when the value is NULL
// Each source shows its engine and version when the backend knows it (the
// replica's relay log, else a saved connection to that host and port), with
// where the version came from in its tooltip, the connector's details and the
// inspector.
// Selecting a connector selects the channel in the inspector, and the filter
// narrows the channels shown. Hover or keyboard focus on a connector shows the
// thread states, positions and last error as a plain popover.
import { useEffect, useId, useMemo, useState } from "react";
import type { Connection } from "./api";
import { Icon } from "./components";
import { channelCell, engineName } from "./OpsShared";
import type { ReplicationChannel, ReplicationReport, SourceVersion } from "./opsApi";
import type { Selection } from "./kit";
import "./replication-topology.css";

export interface ReplicationTopologyProps {
  report: ReplicationReport;
  connection: Connection;
  selection?: Selection;
  onSelectChannel?: (channelName: string) => void;
  compact?: boolean;
}

type Tone = "ok" | "watch" | "problem" | "unknown";

/** What the connector says about one channel, from measured fields only. */
export function linkState(c: ReplicationChannel): { kind: "running" | "stopped" | "error"; label: string } {
  const code = c.sql.error.errno || c.io.error.errno;
  if (code) return { kind: "error", label: `Error ${code}` };
  const io = c.io.running === "Yes";
  const sql = c.sql.running === "Yes";
  if (io && sql) return { kind: "running", label: "Running" };
  if (!io && !sql) return { kind: "stopped", label: "Stopped" };
  const thread = io ? "SQL" : "IO";
  return { kind: "stopped", label: c.io.running === "Connecting" && !io ? "IO thread connecting" : `${thread} thread stopped` };
}

export function lagText(lag: number | null | undefined): string {
  return lag === null || lag === undefined ? "Lag unknown" : `${lag} s behind`;
}

/** A saved connection confirmed by its server ID alone (MariaDB reports no
 * source UUID): evidence, not proof, since both sides often keep the default 1. */
const SERVER_ID_ONLY = "only its server ID could be compared";

/** "MySQL 9.7.2", or "Version unknown". */
export function sourceVersionText(v: SourceVersion): string {
  return v.version ? engineName(v.engine, v.version, v.product) : "Version unknown";
}

/** "from the saved connection X (only its server ID could be compared)", or the label. */
export function sourceVersionHow(v: SourceVersion): string {
  return v.provenance === "saved_connection" && !v.verified ? `${v.label} (${SERVER_ID_ONLY})` : v.label;
}

/** Where the version came from (or why it is unknown), without the version itself. */
export function sourceVersionNote(v: SourceVersion): string {
  if (!v.version) return [v.reason, v.hint].filter(Boolean).join(" ") || "The source's version could not be read.";
  const how = sourceVersionHow(v);
  return [`${how.charAt(0).toUpperCase()}${how.slice(1)}.`, v.caveat].filter(Boolean).join(" ");
}

/** Where the version came from, or why it is unknown, as sentences. */
export function sourceVersionDetail(v: SourceVersion): string {
  if (!v.version) return [v.reason, v.hint].filter(Boolean).join(" ") || "The source's version could not be read.";
  const how = sourceVersionHow(v);
  const short = engineName(v.engine, v.version, v.product);
  const full = short.endsWith(` ${v.version}`) ? short : `${short} (${v.version})`;
  return [`${full}, ${how}.`, v.caveat].filter(Boolean).join(" ");
}

const TONE_WORD: Record<Tone, string> = { ok: "OK", watch: "Watch", problem: "Problem", unknown: "Not judged" };

function position(file: unknown, pos: unknown): string | null {
  return file ? `${file}${pos !== undefined && pos !== null && pos !== "" ? `:${pos}` : ""}` : null;
}

function worstTone(channels: ReplicationChannel[], failed: number): Tone {
  if (failed > 0 || channels.some((c) => c.status === "problem")) return "problem";
  if (channels.some((c) => c.status === "watch")) return "watch";
  if (channels.some((c) => c.status === "unknown")) return "unknown";
  return channels.length ? "ok" : "unknown";
}

export function ReplicationTopology({ report, connection, selection, onSelectChannel, compact = false }: ReplicationTopologyProps) {
  const [filterText, setFilterText] = useState("");
  const detailPrefix = useId();
  const [hovered, setHovered] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const detailKey = hovered || focused;
  useEffect(() => {
    if (!detailKey || detailKey === dismissed) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      setDismissed(detailKey);
    };
    // Also dismiss a pointer-opened tooltip when focus is elsewhere.
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [detailKey, dismissed]);

  const channels = useMemo(() => {
    const q = filterText.trim().toLowerCase();
    if (!q) return report.channels;
    return report.channels.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        (c.source.host && c.source.host.toLowerCase().includes(q)) ||
        c.status.toLowerCase().includes(q),
    );
  }, [report.channels, filterText]);

  const running = report.channels.filter((c) => linkState(c).kind === "running").length;
  const tone = worstTone(report.channels, report.summary.failed);
  const gtid = report.gtid?.mode ?? report.gtid?.gtid_mode;
  const select = (name: string) => {
    selection?.select(`channel:${name}`, true);
    onSelectChannel?.(name);
  };

  return (
    <div className={`replication-topology-container${compact ? " is-compact" : ""}`}>
      <div className="topology-toolbar">
        <span className="topology-count">
          {running} of {report.channels.length} {report.channels.length === 1 ? "channel" : "channels"} running
        </span>
        <label className="topology-search-box">
          <Icon name="search" size={12} />
          <input
            type="text"
            className="topology-search-input"
            placeholder="Filter channels or hosts"
            aria-label="Filter channels or hosts"
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
          />
          {filterText && (
            <button type="button" className="topology-search-clear" onClick={() => setFilterText("")} aria-label="Clear filter">
              ×
            </button>
          )}
        </label>
      </div>

      <div className="topology-scroll">
        {channels.length === 0 ? (
          <p className="topology-empty">No channel matches the filter.</p>
        ) : (
          <div className="topology-map">
            <div className="topology-rows">
            {channels.map((c) => {
              const key = `channel:${c.name}`;
              const detailId = `${detailPrefix}-${encodeURIComponent(c.name)}`;
              const state = linkState(c);
              const read = position(c.position?.source_log_file ?? c.position?.Master_Log_File, c.position?.read_source_log_pos ?? c.position?.Read_Master_Log_Pos);
              const exec = position(c.position?.exec_file ?? c.position?.Relay_Master_Log_File, c.position?.exec_pos ?? c.position?.Exec_Master_Log_Pos);
              return (
                <div className="topology-row" key={key}>
                  <div className="topology-node topology-source">
                    <span className="topology-eyebrow">Source</span>
                    <strong className="topology-host">{c.source.host || "Unknown source"}</strong>
                    <span className="topology-meta">
                      {c.source.port ? `Port ${c.source.port}` : "Port unknown"}
                      {c.source_version ? <>{" · "}<span className={`topology-version${c.source_version.version ? "" : " is-unknown"}`}
                        title={sourceVersionDetail(c.source_version)}>{sourceVersionText(c.source_version)}</span></> : null}
                    </span>
                    {c.source.server_id !== null && c.source.server_id !== undefined ?
                      <span className="topology-meta">Server ID {c.source.server_id}</span> : null}
                    {read && <code className="topology-coord" title="Binary log position read by the replica">{read}</code>}
                  </div>

                  <div className="topology-link-wrap"
                    onMouseEnter={() => { setHovered(key); setDismissed(null); }}
                    onMouseLeave={() => setHovered(null)}
                    onFocus={() => { setFocused(key); setDismissed(null); }}
                    onBlur={() => setFocused(null)}>
                  <button
                    type="button"
                    className={`topology-link is-${state.kind} tone-${c.status}${selection?.key === key ? " is-selected" : ""}`}
                    aria-pressed={selection?.key === key}
                    aria-describedby={detailId}
                    onClick={() => select(c.name)}
                  >
                    <span className="topology-link-name">{channelCell(c.name)}</span>
                    <span className="topology-line" aria-hidden="true" />
                    <span className="topology-link-info">
                      <span className="topology-link-state">
                        <span className="topology-dot" aria-hidden="true" />
                        {state.label}
                      </span>
                      <span className="topology-link-lag">{lagText(c.lag_s)}</span>
                    </span>
                  </button>
                    <span id={detailId} className={detailKey === key && dismissed !== key ? "topology-detail" : "visually-hidden"} role="tooltip">
                      {c.source_version ? <span className="topology-detail-row"><span>Source version</span><span>{c.source_version.version
                        ? `${sourceVersionText(c.source_version)}, ${sourceVersionHow(c.source_version)}` : sourceVersionDetail(c.source_version)}</span></span> : null}
                      <span className="topology-detail-row"><span>IO thread</span><span>{c.io.running || "Unknown"}{c.io.state ? `, ${c.io.state}` : ""}</span></span>
                      <span className="topology-detail-row"><span>SQL thread</span><span>{c.sql.running || "Unknown"}{c.sql.state ? `, ${c.sql.state}` : ""}</span></span>
                      {c.sql.delay_s ? <span className="topology-detail-row"><span>Configured delay</span><span>{c.sql.delay_s} s</span></span> : null}
                      {read && <span className="topology-detail-row"><span>Read position</span><span>{read}</span></span>}
                      {exec && <span className="topology-detail-row"><span>Executed position</span><span>{exec}</span></span>}
                      {c.io.error.errno ? <span className="topology-detail-row is-error"><span>IO error {c.io.error.errno}</span><span>{c.io.error.message}</span></span> : null}
                      {c.sql.error.errno ? <span className="topology-detail-row is-error"><span>SQL error {c.sql.error.errno}</span><span>{c.sql.error.message}</span></span> : null}
                    </span>
                  </div>
                </div>
              );
            })}
            </div>

            <div className="topology-node topology-replica">
              <span className="topology-eyebrow">Replica</span>
              <strong className="topology-host">{connection.host || "This server"}</strong>
              <span className="topology-meta">
                Port {connection.port || 3306} · {engineName(report.engine, report.server_version || "", report.server_product)}
              </span>
              {gtid ? <span className="topology-meta">GTID mode {String(gtid)}</span> : null}
              <span className={`topology-lag tone-${tone}`}>
                <span className="topology-dot" aria-hidden="true" />
                {TONE_WORD[tone]} · {lagText(report.summary.lag_max_s)}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
