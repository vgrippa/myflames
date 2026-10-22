// The Query editor's run history (/api/editor/history): what ran, where and
// how it ended, newest first, kept on this computer with secrets redacted and
// never a password. Click an entry to insert its SQL at the cursor.
import { useEffect, useState } from "react";
import { api, type EditorHistoryItem } from "./api";
import { ActionStatus, ConfirmSheet, IconButton, SearchField, StatusPill, useSince, type Tone } from "./kit";
import { duration } from "./ResultsPane";

const TONE: Record<string, [Tone, string]> = {
  complete: ["ok", "Done"],
  error: ["problem", "Error"],
  stopped: ["watch", "Stopped"],
  session_ended: ["problem", "Session ended"],
};

function when(iso: string) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const d = new Date(t);
  const today = new Date();
  return d.toDateString() === today.toDateString() ? d.toLocaleTimeString() : d.toLocaleString();
}

export function HistoryPanel({
  refresh,
  onInsert,
  onClose,
}: {
  /** Changes after every run, so an open panel reads the history again. */
  refresh: number;
  onInsert: (sql: string) => void;
  onClose: () => void;
}) {
  const [items, setItems] = useState<EditorHistoryItem[] | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [clearing, setClearing] = useState(false);
  const [busy, setBusy] = useState(false);
  // Try again reads the history once more after a failed read.
  const [attempt, setAttempt] = useState(0);
  const reading = items === null && !error;
  const readingSince = useSince(reading);
  useEffect(() => {
    const controller = new AbortController();
    api<{ items: EditorHistoryItem[] }>("editor/history", { limit: 200 }, controller.signal).then(
      (r) => {
        setItems(r.items);
        setError("");
      },
      (e) => {
        if (!controller.signal.aborted) setError((e as Error).message);
      },
    );
    return () => controller.abort();
  }, [refresh, attempt]);
  async function clear() {
    setBusy(true);
    try {
      await api("editor/history", { action: "clear" });
      setItems([]);
      setClearing(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const needle = filter.trim().toLowerCase();
  const shown = (items || []).filter(
    (i) => !needle || i.sql.toLowerCase().includes(needle) || (i.connection.name || "").toLowerCase().includes(needle),
  );
  return (
    <aside className="history-panel" aria-label="Run history" aria-busy={reading || undefined}>
      <div className="browser-head">
        <h2>History</h2>
        <button type="button" className="text-button" disabled={!items?.length} onClick={() => setClearing(true)}>
          Clear
        </button>
        <IconButton icon="close" label="Hide the history" onClick={onClose} />
      </div>
      <div className="browser-filter">
        <SearchField label="Search history" value={filter} onChange={setFilter} />
      </div>
      {/* Always mounted: reading and a failed read are announced; Try again reads once more. */}
      <ActionStatus className="browser-status" state={{
        phase: reading ? "pending" : error ? "failed" : "idle",
        message: reading ? "Reading the run history…" : error, startedAt: readingSince,
      }} onRetry={error && items === null ? () => { setError(""); setAttempt((n) => n + 1); } : undefined} />
      {items && !shown.length && <p className="browser-status">{items.length ? "Nothing matches." : "Runs appear here."}</p>}
      <ol className="history-list">
        {shown.map((item, k) => {
          const [tone, word] = TONE[item.status] || ["unknown", item.status];
          const meta = [
            when(item.at),
            item.connection.name || `${item.connection.host}:${item.connection.port}`,
            duration(item.duration_ms),
            item.rows ? `${item.rows.toLocaleString()} rows` : item.affected_rows ? `${item.affected_rows.toLocaleString()} affected` : "",
            item.error_code ? `error ${item.error_code}` : "",
          ]
            .filter(Boolean)
            .join(" · ");
          return (
            <li key={`${item.at}-${k}`}>
              <button
                type="button"
                className="history-item"
                title={`${item.sql}\n\nClick to insert at the cursor`}
                onClick={() => onInsert(item.sql)}
              >
                <span className="history-sql">{item.sql.trim().split("\n").slice(0, 2).join(" ")}</span>
                <span className="history-meta">
                  <StatusPill tone={tone} label={word} />
                  <span>{meta}</span>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
      {clearing && (
        <ConfirmSheet
          eyebrow="History"
          title="Clear the run history?"
          description="This removes every entry on this computer. The Audit log keeps its records."
          confirmLabel="Clear history"
          confirmTone="primary"
          busy={busy}
          onCancel={() => setClearing(false)}
          onConfirm={() => void clear()}
        />
      )}
    </aside>
  );
}
