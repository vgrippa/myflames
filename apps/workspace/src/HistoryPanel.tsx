// The Query editor's side panel, with two tabs. History (/api/editor/history):
// what ran, where and how it ended, newest first, kept on this computer with
// secrets redacted and never a password; click an entry to insert its SQL at
// the cursor. Saved (SavedQueries.tsx): the queries the user saved, in
// folders. Each tab reads only while it is shown.
import { useEffect, useState } from "react";
import { api, type EditorHistoryItem, type SavedQuery } from "./api";
import { ActionStatus, ConfirmSheet, IconButton, SearchField, Segmented, StatusPill, useSince, type Tone } from "./kit";
import { duration } from "./ResultsPane";
import { SavedQueriesList } from "./SavedQueries";

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

export type SidePanelTab = "history" | "saved";

export function HistoryPanel({
  refresh,
  savedRefresh,
  tab,
  onTab,
  onInsert,
  onOpenSaved,
  onClose,
}: {
  /** Changes after every run, so an open panel reads the history again. */
  refresh: number;
  /** Changes after Save query…, so an open Saved tab reads again. */
  savedRefresh: number;
  tab: SidePanelTab;
  onTab: (tab: SidePanelTab) => void;
  onInsert: (sql: string) => void;
  /** Open a saved query in a new tab (it never runs). */
  onOpenSaved: (query: SavedQuery) => void;
  onClose: () => void;
}) {
  const [clearing, setClearing] = useState(false);
  const [hasHistory, setHasHistory] = useState(false);
  // The tab on screen reports its first read, for the panel's aria-busy.
  const [reading, setReading] = useState(false);
  return (
    <aside className="history-panel" aria-label="History and saved queries" aria-busy={reading || undefined}>
      <div className="browser-head history-head">
        <Segmented<SidePanelTab>
          label="History and saved queries"
          className="history-tabs"
          tabs="editor-side"
          value={tab}
          onChange={onTab}
          options={[
            { value: "history", label: "History", title: "Past runs on this computer" },
            { value: "saved", label: "Saved", title: "Queries you saved, in folders" },
          ]}
        />
        <span className="history-head-gap" />
        {tab === "history" && (
          <button type="button" className="text-button" disabled={!hasHistory} onClick={() => setClearing(true)}>
            Clear
          </button>
        )}
        <IconButton icon="close" label="Hide the history" onClick={onClose} />
      </div>
      <div id="editor-side-panel-history" role="tabpanel" aria-labelledby="editor-side-tab-history" className="history-tab"
        hidden={tab !== "history"}>
        {tab === "history" && (
          <RunHistory refresh={refresh} onInsert={onInsert} clearing={clearing} onClearing={setClearing} onHasItems={setHasHistory}
            onReading={setReading} />
        )}
      </div>
      <div id="editor-side-panel-saved" role="tabpanel" aria-labelledby="editor-side-tab-saved" className="history-tab"
        hidden={tab !== "saved"}>
        {tab === "saved" && <SavedQueriesList refresh={savedRefresh} onOpen={onOpenSaved} onInsert={onInsert} onReading={setReading} />}
      </div>
    </aside>
  );
}

function RunHistory({
  refresh,
  onInsert,
  clearing,
  onClearing,
  onHasItems,
  onReading,
}: {
  refresh: number;
  onInsert: (sql: string) => void;
  clearing: boolean;
  onClearing: (open: boolean) => void;
  onHasItems: (has: boolean) => void;
  onReading: (reading: boolean) => void;
}) {
  const [items, setItems] = useState<EditorHistoryItem[] | null>(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
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
  useEffect(() => onHasItems(!!items?.length), [items, onHasItems]);
  useEffect(() => {
    onReading(reading);
    return () => onReading(false);
  }, [reading, onReading]);
  async function clear() {
    setBusy(true);
    try {
      await api("editor/history", { action: "clear" });
      setItems([]);
      onClearing(false);
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
    <div className="run-history">
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
          description="This removes every entry on this computer. The Audit log keeps its records, and saved queries stay."
          confirmLabel="Clear history"
          confirmTone="primary"
          busy={busy}
          onCancel={() => onClearing(false)}
          onConfirm={() => void clear()}
        />
      )}
    </div>
  );
}
