import { useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  number,
  time,
  type Connection,
  type SlowQueriesReport,
  type TopOrder,
  type TopReport,
  type TopStatement,
} from "./api";
import { useConnectionReads, type Bound } from "./binding";
import { useOpenRead, useViewOnScreen } from "./viewOnScreen";
import { Icon } from "./components";
import { EmptyState } from "./kit";
import { csvCell } from "./reportTableExport";
import { digestName, digestQuery, shortDigest } from "./placeholders";
import "./query-digest.css";

export type DigestFilterMode =
  | "time"
  | "scans"
  | "full_join"
  | "tmp_tables"
  | "locks"
  | "slow_queries";

export type QueryDigestProps = {
  connection: Connection;
  onOpenInEditor?: (row: TopStatement, rank: number, from: string) => void;
  onExplain?: (row: TopStatement, rank: number, from: string) => void;
};

function formatRatio(examined: number, sent: number): { text: string; tone: "danger" | "warning" | "success" } {
  if (sent <= 0) {
    if (examined <= 0) return { text: "1:1", tone: "success" };
    return { text: `${number(examined)}:0`, tone: "danger" };
  }
  const ratio = Math.round(examined / sent);
  if (ratio >= 1000) return { text: `${number(ratio)}:1`, tone: "danger" };
  if (ratio >= 50) return { text: `${number(ratio)}:1`, tone: "warning" };
  return { text: `${number(ratio)}:1`, tone: "success" };
}

function downloadFile(filename: string, content: string, mimeType: string) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

type DigestReport = {
  statements: TopStatement[];
  engine: string;
  version: string;
  source?: string;
  filter: DigestFilterMode;
};

export function QueryDigestView({
  connection,
  onOpenInEditor,
  onExplain,
}: QueryDigestProps) {
  const [filter, setFilter] = useState<DigestFilterMode>("time");
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(50);
  const [minSeconds] = useState(1.0);
  const reads = useConnectionReads(connection);
  const [bound, setBound] = useState<Bound<DigestReport> | null>(null);
  const statements = bound?.report.statements || [];
  const serverMeta = bound?.report;
  const loading = !!reads.loading;
  const error = reads.error;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const actionsOff = !bound || !!other;
  const [selectedDigest, setSelectedDigest] = useState<string | null>(null);
  const [exportModalOpen, setExportModalOpen] = useState(false);
  const [copiedNote, setCopiedNote] = useState<string | null>(null);
  const loadData = async () => {
    await reads.run(
      `query-digest:${filter}:${limit}:${minSeconds}:${connection.database || ""}`,
      "Reading statement digests…",
      async (c, signal): Promise<DigestReport> => {
        const res = filter === "slow_queries"
          ? await api<SlowQueriesReport>("live/slow-queries", {
              connection: c, min_seconds: minSeconds, limit, source: "auto",
            }, signal)
          : await api<TopReport>("live/top", {
              connection: c, order: filter as TopOrder, limit, extra_metrics: true,
            }, signal);
        return {
          statements: res.statements || [],
          engine: res.engine || "mysql",
          version: res.server_version || "",
          source: "source" in res ? res.source : undefined,
          filter,
        };
      },
      (result) => {
        setBound(result);
        const first = result.report.statements[0];
        setSelectedDigest(first ? first.digest || first.digest_text || "1" : null);
      },
    );
  };

  // Query digest is a place of its own: it reads when it opens on screen
  // (once per connection; Refresh reads again), and a new filter, limit or
  // default database reads again while it is on screen.
  const onScreen = useViewOnScreen();
  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: true, busy: loading || reads.running(), read: () => { void loadData(); } });
  const choices = useRef(true);
  useEffect(() => {
    if (choices.current) { choices.current = false; return; }
    if (onScreen) void loadData();
  }, [filter, limit, connection.database]);

  function openRow(row: TopStatement, rank: number, callback: QueryDigestProps["onExplain"]) {
    // Recheck at dispatch as well as disabling the visible action. The editor
    // receives the report's identity and performs its own final check.
    if (!bound || bound.key !== reads.currentKey()) return;
    callback?.(row, rank, bound.key);
  }

  // Compute workload aggregates
  const aggregates = useMemo(() => {
    let totalWorkloadMs = 0;
    let totalCalls = 0;
    let totalExamined = 0;
    let totalSent = 0;
    let totalLockMs = 0;

    for (const s of statements) {
      totalWorkloadMs += s.total_ms || 0;
      totalCalls += s.count || 0;
      totalExamined += s.rows_examined || 0;
      totalSent += s.rows_sent || 0;
      totalLockMs += s.lock_time_ms || 0;
    }

    return {
      totalWorkloadMs,
      totalCalls,
      totalExamined,
      totalSent,
      totalLockMs,
    };
  }, [statements]);

  // Filtered statements by search query
  const filteredStatements = useMemo(() => {
    if (!search.trim()) return statements;
    const q = search.toLowerCase();
    return statements.filter((s) => {
      const text = (s.sample_text || s.digest_text || "").toLowerCase();
      const schema = (s.schema || "").toLowerCase();
      const digest = (s.digest || "").toLowerCase();
      return text.includes(q) || schema.includes(q) || digest.includes(q);
    });
  }, [statements, search]);

  const selectedRow = useMemo(() => {
    if (!selectedDigest) return filteredStatements[0] || null;
    return (
      filteredStatements.find(
        (s) => (s.digest || s.digest_text) === selectedDigest
      ) || filteredStatements[0] || null
    );
  }, [filteredStatements, selectedDigest]);

  // Generate pt-query-digest formatted plain text report
  const generatePtReportText = () => {
    const lines: string[] = [];
    lines.push("# ------------------------------------------------------------------------");
    lines.push("# pt-query-digest Report (myflames live query digest)");
    lines.push("# ------------------------------------------------------------------------");
    lines.push(`# Server: ${serverMeta?.engine.toUpperCase() || "Unknown"} ${serverMeta?.version || ""}`);
    lines.push(`# Connection: ${bound?.label || "Not loaded"}`);
    lines.push(`# Generated at: ${new Date().toISOString().replace("T", " ").substring(0, 19)} UTC`);
    lines.push(`# Filter mode: ${serverMeta?.filter || filter}`);
    lines.push(`# Loaded scope: ${statements.length} query digests, ${aggregates.totalCalls.toLocaleString()} executions`);
    lines.push("# Statistics cover these loaded digests only. Unknown means not collected.");
    lines.push(`# Total response time: ${time(aggregates.totalWorkloadMs)}`);
    lines.push("#");
    lines.push("# Attribute          total        min        max        avg        95%");
    lines.push("# ============= ========== ========== ========== ========== ==========");

    // Digest averages are not execution samples. Only a reported MAX_TIMER_WAIT
    // supports an execution maximum; averages cannot supply minima or percentiles.
    const maxTime = statements.length && statements.every(s => Number.isFinite(s.max_ms))
      ? Math.max(...statements.map(s => s.max_ms)) : null;
    const avgTime = aggregates.totalCalls > 0 ? aggregates.totalWorkloadMs / aggregates.totalCalls : null;
    const lockTime = statements.length && statements.every(s => s.lock_time_ms != null && Number.isFinite(s.lock_time_ms))
      ? aggregates.totalLockMs : null;
    const pad = (str: string, len: number) => str.padStart(len, " ");
    const unknown = pad("unknown", 10);
    const duration = (value: number | null) => pad(value === null ? "unknown" : time(value), 10);
    const average = (total: number) => aggregates.totalCalls > 0
      ? pad(number(total / aggregates.totalCalls), 10) : unknown;

    lines.push(`# Exec time     ${pad(time(aggregates.totalWorkloadMs), 10)} ${unknown} ${duration(maxTime)} ${duration(avgTime)} ${unknown}`);
    lines.push(`# Lock time     ${duration(lockTime)} ${unknown} ${unknown} ${duration(lockTime !== null && aggregates.totalCalls > 0 ? lockTime / aggregates.totalCalls : null)} ${unknown}`);
    lines.push(`# Rows sent     ${pad(number(aggregates.totalSent), 10)} ${unknown} ${unknown} ${average(aggregates.totalSent)} ${unknown}`);
    lines.push(`# Rows examine  ${pad(number(aggregates.totalExamined), 10)} ${unknown} ${unknown} ${average(aggregates.totalExamined)} ${unknown}`);
    lines.push("# Execution minima, row/lock maxima and percentiles are not collected.");
    lines.push("#");
    lines.push("# Profile");
    lines.push("# Rank Query ID           Response time  Calls R/Call   Ratio  Item");
    lines.push("# ==== ================== ============== ===== ======== ====== ===============");

    statements.slice(0, 30).forEach((s, idx) => {
      const rank = (idx + 1).toString().padStart(4, " ");
      const qid = (s.digest ? `0x${shortDigest(s.digest)}` : `0x${(idx + 1).toString(16).padStart(8, "0")}`).padEnd(18, " ");
      const pct = aggregates.totalWorkloadMs > 0 ? ((s.total_ms / aggregates.totalWorkloadMs) * 100).toFixed(1) : "0.0";
      const respTime = `${time(s.total_ms)} ${pct}%`.padStart(14, " ");
      const calls = s.count.toString().padStart(5, " ");
      const rCall = time(s.avg_ms).padStart(8, " ");
      const ratio = formatRatio(s.rows_examined, s.rows_sent).text.padStart(6, " ");
      const snippet = (s.sample_text || s.digest_text || "").replace(/\s+/g, " ").trim().substring(0, 35);
      lines.push(`# ${rank} ${qid} ${respTime} ${calls} ${rCall} ${ratio} ${snippet}`);
    });

    lines.push("");
    lines.push("# ------------------------------------------------------------------------");
    lines.push("# Query Details");
    lines.push("# ------------------------------------------------------------------------");

    statements.slice(0, 10).forEach((s, idx) => {
      const q = digestQuery(s);
      lines.push("");
      lines.push(`# Query ${idx + 1} (Digest: 0x${s.digest || `query_${idx + 1}`})`);
      lines.push(`# Schema: ${s.schema || "(none)"}`);
      lines.push(`# Executions: ${s.count.toLocaleString()}`);
      lines.push(`# Total Time: ${time(s.total_ms)} (${aggregates.totalWorkloadMs > 0 ? ((s.total_ms / aggregates.totalWorkloadMs) * 100).toFixed(1) : "0"}%)`);
      lines.push(`# Avg Latency: ${time(s.avg_ms)}, Max Latency: ${time(s.max_ms)}`);
      lines.push(`# Lock Time: ${time(s.lock_time_ms || 0)}`);
      lines.push(`# Rows Examined: ${number(s.rows_examined)}, Rows Sent: ${number(s.rows_sent)} (Ratio: ${formatRatio(s.rows_examined, s.rows_sent).text})`);
      lines.push(`# Temp Tables: Total ${s.tmp_tables || s.tmp_disk_tables}, On Disk: ${s.tmp_disk_tables}`);
      lines.push(`# Full Table Scans: ${s.select_scan || s.no_index_used || 0}, Full Joins: ${s.select_full_join || 0}`);
      lines.push("# Fingerprint:");
      lines.push(q.text.trim());
      lines.push(";");
    });

    return lines.join("\n");
  };

  // Generate CSV Export
  const generateCsv = () => {
    const headers = [
      "Rank",
      "Query Digest",
      "Schema",
      "Calls",
      "Total Time (ms)",
      "Avg Time (ms)",
      "Max Time (ms)",
      "Lock Time (ms)",
      "Rows Examined",
      "Rows Sent",
      "Examined/Sent Ratio",
      "Disk Temp Tables",
      "Total Temp Tables",
      "Full Table Scans",
      "Full Joins",
      "Fingerprint",
      "Sample SQL",
    ];

    const escape = csvCell;

    const rows = statements.map((s, idx) => {
      const ratio = s.rows_sent > 0 ? Math.round(s.rows_examined / s.rows_sent) : s.rows_examined;
      return [
        idx + 1,
        s.digest || "",
        s.schema || "",
        s.count,
        s.total_ms.toFixed(2),
        s.avg_ms.toFixed(2),
        s.max_ms.toFixed(2),
        (s.lock_time_ms || 0).toFixed(2),
        s.rows_examined,
        s.rows_sent,
        ratio,
        s.tmp_disk_tables,
        s.tmp_tables || 0,
        s.select_scan || s.no_index_used || 0,
        s.select_full_join || 0,
        s.digest_text || "",
        s.sample_text || "",
      ]
        .map(escape)
        .join(",");
    });

    return [headers.map(escape).join(","), ...rows].join("\n");
  };

  const handleCopyText = (content: string, noteText: string) => {
    void navigator.clipboard.writeText(content);
    setCopiedNote(noteText);
    setTimeout(() => setCopiedNote(null), 2000);
  };

  return (
    <div className="query-digest-container">
      {/* Top Header & Toolbar */}
      <div className="query-digest-header">
        <div className="query-digest-title-row">
          <div className="query-digest-heading">
            <div className="query-digest-icon">
              <Icon name="statements" size={18} />
            </div>
            <div>
              <h2 className="query-digest-title">Query digest</h2>
              <p className="query-digest-subtitle">
                Statement digests ranked by total time, in the style of pt-query-digest
              </p>
            </div>
          </div>
          <div className="query-digest-actions">
            <button
              type="button"
              className="query-digest-btn-sm"
              onClick={() => void loadData()}
              disabled={loading}
              title="Refresh digest profile"
            >
              <Icon name="refresh" size={13} />
              <span>{loading ? "Reading…" : "Refresh"}</span>
            </button>
            <button
              type="button"
              className="query-digest-btn-sm"
              onClick={() => setExportModalOpen(true)}
              disabled={!bound}
              title="Export a pt-query-digest style text report or CSV"
            >
              <Icon name="export" size={13} />
              <span>Export</span>
            </button>
          </div>
        </div>

        {/* Filter Pills / Tabs */}
        <div className="query-digest-filters-bar">
          <div className="query-digest-pill-tabs" role="tablist" aria-label="Query digest filters">
            <button
              type="button"
              role="tab"
              aria-selected={filter === "time"}
              className={`query-digest-pill-tab ${filter === "time" ? "is-active" : ""}`}
              onClick={() => setFilter("time")}
            >
              <Icon name="health" size={13} />
              <span>By total time</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={filter === "scans"}
              className={`query-digest-pill-tab ${filter === "scans" ? "is-active" : ""}`}
              onClick={() => setFilter("scans")}
            >
              <Icon name="table" size={13} />
              <span>Full scans</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={filter === "full_join"}
              className={`query-digest-pill-tab ${filter === "full_join" ? "is-active" : ""}`}
              onClick={() => setFilter("full_join")}
            >
              <Icon name="diagram" size={13} />
              <span>Joins without an index</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={filter === "tmp_tables"}
              className={`query-digest-pill-tab ${filter === "tmp_tables" ? "is-active" : ""}`}
              onClick={() => setFilter("tmp_tables")}
            >
              <Icon name="database" size={13} />
              <span>Temp tables on disk</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={filter === "locks"}
              className={`query-digest-pill-tab ${filter === "locks" ? "is-active" : ""}`}
              onClick={() => setFilter("locks")}
            >
              <Icon name="lock" size={13} />
              <span>Lock time</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={filter === "slow_queries"}
              className={`query-digest-pill-tab ${filter === "slow_queries" ? "is-active" : ""}`}
              onClick={() => setFilter("slow_queries")}
            >
              <Icon name="warning" size={13} />
              <span>Slow queries (over 1 s)</span>
            </button>
          </div>

          <div className="query-digest-tools-row">
            <div className="query-digest-search-box">
              <Icon name="search" size={13} />
              <input
                type="text"
                placeholder="Filter queries, tables, schemas…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
            <select
              aria-label="Row limit"
              className="query-digest-btn-sm"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option value={20}>Top 20</option>
              <option value={50}>Top 50</option>
              <option value={100}>Top 100</option>
            </select>
          </div>
        </div>
      </div>

      {other && <p className="notice" role="status">From {other}. Refresh to load this connection; statement actions are unavailable.</p>}

      {/* Aggregate Stats Strip */}
      <div className="query-digest-summary-strip">
        <div className="query-digest-stat-card">
          <span className="query-digest-stat-label">Total time</span>
          <span className="query-digest-stat-value">{time(aggregates.totalWorkloadMs)}</span>
          <span className="query-digest-stat-sub">Across {statements.length} digests</span>
        </div>
        <div className="query-digest-stat-card">
          <span className="query-digest-stat-label">Executions</span>
          <span className="query-digest-stat-value">{aggregates.totalCalls.toLocaleString()}</span>
          <span className="query-digest-stat-sub">Cumulative calls</span>
        </div>
        <div className="query-digest-stat-card">
          <span className="query-digest-stat-label">Rows examined</span>
          <span className="query-digest-stat-value">{number(aggregates.totalExamined)}</span>
          <span className="query-digest-stat-sub">
            {formatRatio(aggregates.totalExamined, aggregates.totalSent).text} overall scan ratio
          </span>
        </div>
        <div className="query-digest-stat-card">
          <span className="query-digest-stat-label">Rows sent</span>
          <span className="query-digest-stat-value">{number(aggregates.totalSent)}</span>
          <span className="query-digest-stat-sub">Result payload rows</span>
        </div>
        <div className="query-digest-stat-card">
          <span className="query-digest-stat-label">Lock time</span>
          <span className="query-digest-stat-value">{time(aggregates.totalLockMs)}</span>
          <span className="query-digest-stat-sub">Cumulative lock waits</span>
        </div>
      </div>

      {/* Main Split View: pt-query-digest Table + Inspector */}
      <div className="query-digest-main-split">
        <div className="query-digest-table-pane">
          {error && (
            <div style={{ padding: "16px 20px" }}>
              <p className="notice is-error" role="alert">
                {error}
              </p>
            </div>
          )}

          {!loading && filteredStatements.length === 0 && (
            <EmptyState icon="statements" title="No matching query digests">
              No statement digests recorded matching the current filter ({filter}).
            </EmptyState>
          )}

          <div className="query-digest-table-wrapper">
            <table className="query-digest-table">
              <thead>
                <tr>
                  <th style={{ width: 44, textAlign: "center" }}>Rank</th>
                  <th>Digest</th>
                  <th className="num-col" style={{ width: 140 }}>
                    Total time
                  </th>
                  <th className="num-col" style={{ width: 80 }}>
                    Calls
                  </th>
                  <th className="num-col" style={{ width: 120 }}>
                    Latency (avg / max)
                  </th>
                  <th className="num-col" style={{ width: 140 }}>
                    Rows (examined : sent)
                  </th>
                  <th className="num-col" style={{ width: 85 }}>
                    Temp tables
                  </th>
                  <th style={{ width: 190, textAlign: "right" }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredStatements.map((s, idx) => {
                  const rank = idx + 1;
                  const key = s.digest || s.digest_text || String(idx);
                  const isSelected = selectedRow && (selectedRow.digest || selectedRow.digest_text) === key;
                  const timePct = aggregates.totalWorkloadMs > 0 ? (s.total_ms / aggregates.totalWorkloadMs) * 100 : 0;
                  const ratio = formatRatio(s.rows_examined, s.rows_sent);
                  const q = digestQuery(s);

                  return (
                    <tr
                      key={key}
                      className={isSelected ? "is-selected" : ""}
                      onClick={() => setSelectedDigest(key)}
                    >
                      <td style={{ textAlign: "center" }}>
                        <span className="query-digest-rank">#{rank}</span>
                      </td>
                      <td className="query-digest-sql-cell">
                        <span className="query-digest-sql-text" title={q.text}>
                          {q.text}
                        </span>
                        <div className="query-digest-sql-meta">
                          {s.schema && (
                            <span className="query-digest-schema-chip">
                              {s.schema}
                            </span>
                          )}
                          {s.digest && (
                            <span className="query-digest-hash-chip">
                              0x{shortDigest(s.digest)}
                            </span>
                          )}
                          {(s.select_scan || s.no_index_used) ? (
                            <span className="query-digest-badge danger" title="Full table or index scan">
                              Full scan
                            </span>
                          ) : null}
                          {s.select_full_join ? (
                            <span className="query-digest-badge warning" title="Join without usable index">
                              Full join
                            </span>
                          ) : null}
                          {s.tmp_disk_tables ? (
                            <span className="query-digest-badge warning" title="Created on-disk temporary table">
                              Disk temp ({s.tmp_disk_tables})
                            </span>
                          ) : null}
                          {s.avg_ms >= 1000 ? (
                            <span className="query-digest-badge danger" title="Average latency over 1 second">
                              Slow (&gt;1s)
                            </span>
                          ) : null}
                        </div>
                      </td>
                      <td className="num-col query-digest-time-cell">
                        <div className="query-digest-time-val">{time(s.total_ms)}</div>
                        <div className="query-digest-time-pct-bar">
                          <div className="query-digest-progress-track">
                            <div
                              className={`query-digest-progress-fill ${timePct > 30 ? "high" : ""}`}
                              style={{ width: `${Math.min(100, Math.max(2, timePct))}%` }}
                            />
                          </div>
                          <span className="query-digest-time-pct-text">
                            {timePct.toFixed(1)}%
                          </span>
                        </div>
                      </td>
                      <td className="num-col">
                        <span style={{ fontWeight: 600, fontFeatureSettings: "tnum" }}>
                          {s.count.toLocaleString()}
                        </span>
                      </td>
                      <td className="num-col">
                        <div style={{ fontWeight: 600, fontFeatureSettings: "tnum" }}>
                          {time(s.avg_ms)}
                        </div>
                        <div className="query-digest-subvalue">
                          Max: {time(s.max_ms)}
                        </div>
                      </td>
                      <td className="num-col query-digest-rows-cell">
                        <div className="query-digest-rows-line">
                          <strong>{number(s.rows_examined)}</strong> ex / <strong>{number(s.rows_sent)}</strong> sent
                        </div>
                        <span className={`query-digest-ratio-badge query-digest-badge ${ratio.tone}`}>
                          {ratio.text}
                        </span>
                      </td>
                      <td className="num-col">
                        <div style={{ fontFeatureSettings: "tnum" }}>
                          {s.tmp_disk_tables > 0 ? (
                            <span className="query-digest-tmp-disk">
                              {s.tmp_disk_tables} disk
                            </span>
                          ) : s.tmp_tables ? (
                            <span>{s.tmp_tables} mem</span>
                          ) : (
                            <span className="query-digest-zero">0</span>
                          )}
                        </div>
                      </td>
                      <td>
                        <div className="query-digest-action-btns">
                          <button
                            type="button"
                            className="query-digest-btn-sm"
                            disabled={actionsOff}
                            title="Explain this statement and open its plan"
                            onClick={(e) => {
                              e.stopPropagation();
                              openRow(s, rank, onExplain);
                            }}
                          >
                            <Icon name="plan" size={12} />
                            <span>Explain</span>
                          </button>
                          <button
                            type="button"
                            className="query-digest-btn-sm primary"
                            disabled={actionsOff}
                            title="Open in the Query editor"
                            onClick={(e) => {
                              e.stopPropagation();
                              openRow(s, rank, onOpenInEditor);
                            }}
                          >
                            <Icon name="query" size={12} />
                            <span>Open in editor</span>
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Side Inspector Pane */}
        {selectedRow && (
          <aside className="query-digest-inspector" aria-label="Statement inspector">
            <div className="query-digest-inspector-header">
              <h3 className="query-digest-inspector-title">
                {digestName(
                  filteredStatements.indexOf(selectedRow) + 1,
                  selectedRow
                )}
              </h3>
              <div style={{ display: "flex", gap: 6 }}>
                <button
                  type="button"
                  className="query-digest-btn-sm"
                  onClick={() => handleCopyText(selectedRow.sample_text || selectedRow.digest_text || "", "SQL Copied!")}
                  title="Copy SQL"
                >
                  <Icon name="copy" size={12} />
                  <span>{copiedNote || "Copy SQL"}</span>
                </button>
              </div>
            </div>

            <div className="query-digest-inspector-body">
              {/* Actions Box */}
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  style={{ flex: 1 }}
                  className="query-digest-btn-sm primary"
                  disabled={actionsOff}
                  onClick={() => openRow(selectedRow, filteredStatements.indexOf(selectedRow) + 1, onOpenInEditor)}
                >
                  <Icon name="query" size={13} />
                  <span>Open in Query editor</span>
                </button>
                <button
                  type="button"
                  style={{ flex: 1 }}
                  className="query-digest-btn-sm"
                  disabled={actionsOff}
                  onClick={() => openRow(selectedRow, filteredStatements.indexOf(selectedRow) + 1, onExplain)}
                >
                  <Icon name="plan" size={13} />
                  <span>Explain</span>
                </button>
              </div>

              {/* pt-query-digest Attributes Card */}
              <div className="query-digest-card">
                <h4 className="query-digest-card-title">Attributes</h4>
                <table className="pt-attributes-table">
                  <thead>
                    <tr>
                      <th>Metric</th>
                      <th>Total</th>
                      <th>Avg</th>
                      <th>Max</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>Exec time</td>
                      <td>{time(selectedRow.total_ms)}</td>
                      <td>{time(selectedRow.avg_ms)}</td>
                      <td>{time(selectedRow.max_ms)}</td>
                    </tr>
                    <tr>
                      <td>Lock time</td>
                      <td>{time(selectedRow.lock_time_ms || 0)}</td>
                      <td>—</td>
                      <td>—</td>
                    </tr>
                    <tr>
                      <td>Rows examined</td>
                      <td>{number(selectedRow.rows_examined)}</td>
                      <td>{number(Math.round(selectedRow.rows_examined / Math.max(1, selectedRow.count)))}</td>
                      <td>—</td>
                    </tr>
                    <tr>
                      <td>Rows sent</td>
                      <td>{number(selectedRow.rows_sent)}</td>
                      <td>{number(Math.round(selectedRow.rows_sent / Math.max(1, selectedRow.count)))}</td>
                      <td>—</td>
                    </tr>
                    <tr>
                      <td>Examine/Sent</td>
                      <td colSpan={3}>
                        <span className={`query-digest-badge ${formatRatio(selectedRow.rows_examined, selectedRow.rows_sent).tone}`}>
                          {formatRatio(selectedRow.rows_examined, selectedRow.rows_sent).text} ratio
                        </span>
                      </td>
                    </tr>
                    <tr>
                      <td>Temp tables</td>
                      <td colSpan={3}>
                        {selectedRow.tmp_disk_tables > 0
                          ? `${selectedRow.tmp_disk_tables} on disk (${selectedRow.tmp_tables || selectedRow.tmp_disk_tables} total)`
                          : `${selectedRow.tmp_tables || 0} in memory`}
                      </td>
                    </tr>
                    {selectedRow.schema && (
                      <tr>
                        <td>Schema</td>
                        <td colSpan={3} style={{ fontFamily: "monospace" }}>
                          {selectedRow.schema}
                        </td>
                      </tr>
                    )}
                    {selectedRow.digest && (
                      <tr>
                        <td>Digest hash</td>
                        <td colSpan={3} style={{ fontFamily: "monospace", fontSize: 11 }}>
                          0x{selectedRow.digest}
                        </td>
                      </tr>
                    )}
                    {selectedRow.first_seen && (
                      <tr>
                        <td>First seen</td>
                        <td colSpan={3} style={{ fontSize: 11 }}>
                          {selectedRow.first_seen}
                        </td>
                      </tr>
                    )}
                    {selectedRow.last_seen && (
                      <tr>
                        <td>Last seen</td>
                        <td colSpan={3} style={{ fontSize: 11 }}>
                          {selectedRow.last_seen}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              {/* Full Statement SQL */}
              <div className="query-digest-card">
                <h4 className="query-digest-card-title">Normalized SQL</h4>
                <div className="query-digest-code-box">
                  <pre>{digestQuery(selectedRow).text}</pre>
                </div>
              </div>

              {selectedRow.sample_text && selectedRow.sample_text !== selectedRow.digest_text && (
                <div className="query-digest-card">
                  <h4 className="query-digest-card-title">Query sample</h4>
                  <div className="query-digest-code-box">
                    <pre>{selectedRow.sample_text}</pre>
                  </div>
                </div>
              )}
            </div>
          </aside>
        )}
      </div>

      {/* Export Report Modal */}
      {exportModalOpen && (
        <div
          className="query-digest-modal-backdrop"
          onClick={() => setExportModalOpen(false)}
        >
          <div
            className="query-digest-modal"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-labelledby="export-modal-title"
          >
            <div className="query-digest-modal-header">
              <h3 id="export-modal-title" className="query-digest-modal-title">
                Export the digest report
              </h3>
              <button
                type="button"
                className="query-digest-btn-sm"
                aria-label="Close"
                onClick={() => setExportModalOpen(false)}
              >
                <Icon name="close" />
              </button>
            </div>
            <div className="query-digest-modal-content">
              <p className="query-digest-preview-lead">
                Preview of the formatted pt-query-digest report generated from live Performance Schema statement digests:
              </p>
              <pre className="query-digest-preview-pre">{generatePtReportText()}</pre>
            </div>
            <div className="query-digest-modal-footer">
              <button
                type="button"
                className="query-digest-btn-sm"
                onClick={() =>
                  handleCopyText(
                    generatePtReportText(),
                    "pt-query-digest report copied!"
                  )
                }
              >
                <Icon name="copy" size={13} />
                <span>{copiedNote || "Copy to clipboard"}</span>
              </button>
              <button
                type="button"
                className="query-digest-btn-sm"
                onClick={() =>
                  downloadFile(
                    `pt-query-digest-${connection.name || "mysql"}-${Date.now()}.csv`,
                    generateCsv(),
                    "text/csv"
                  )
                }
              >
                <Icon name="export" size={13} />
                <span>Download CSV</span>
              </button>
              <button
                type="button"
                className="query-digest-btn-sm primary"
                onClick={() =>
                  downloadFile(
                    `pt-query-digest-${connection.name || "mysql"}-${Date.now()}.txt`,
                    generatePtReportText(),
                    "text/plain"
                  )
                }
              >
                <Icon name="export" size={13} />
                <span>Download text report (.txt)</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
