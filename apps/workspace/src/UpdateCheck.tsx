// Check for updates: the status line and button in the window footer and
// in About myflames. GitHub is asked when the button is clicked and by the
// automatic check (updateStatus.ts); the last check (kept in the workspace
// store) is shown with its local time.
import { useEffect, useRef, useState } from "react";
import {
  checkForUpdates, checkedWhen, loadUpdateStatus, openUpdateDialog, updateStatusText, useUpdateStatus,
} from "./updateStatus";

/** Re-render once a minute so "today" and the 24-hour limit stay true. */
export function useMinuteClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** The last check's outcome and time, plus the next step when a newer release was found. */
export function UpdateCheckStatus() {
  const s = useUpdateStatus();
  const now = useMinuteClock();
  const nextButton = useRef<HTMLButtonElement>(null);
  useEffect(() => loadUpdateStatus(), []);
  const when = s.last ? checkedWhen(s.last.checked_at, now) : null;
  // A time that cannot be read is never shown as a current result.
  const stale = !!s.last && (!when || when.stale);
  const outcome = s.checking ? "checking" : s.ready ? "ready" : s.last?.outcome || (s.loaded ? "never" : s.loadFailed ? "unknown" : "loading");
  const available = !!s.ready || s.installing || (s.last?.outcome === "available" && !stale);
  const next = s.ready ? "Restart to update…" : s.installing ? "Updating…" : s.support?.supported ? "Update…" : "Download…";
  return (
    <>
      <span className="update-check" data-outcome={outcome} data-stale={stale || undefined} role="status" aria-live="polite">
        {s.checking ? (
          <span className="update-check-status"><span className="spinner" aria-hidden="true" />Checking for updates…</span>
        ) : s.ready ? (
          <span className="update-check-status">{s.ready} ready to install</span>
        ) : s.loaded || s.loadFailed || s.last ? (
          <span className="update-check-status">{updateStatusText(s.last, stale, s.loadFailed)}</span>
        ) : null}
        {!s.checking && !s.ready && s.last && when && (
          <time className="update-check-time" dateTime={s.last.checked_at}
            title={stale ? `${when.title}. More than a day ago: check again for the current release.` : when.title}>
            {s.last.outcome === "failed" ? "Tried" : "Checked"} {when.text}
          </time>
        )}
      </span>
      {available && (
        <button ref={nextButton} type="button" className="text-button update-check-download" aria-haspopup="dialog"
          aria-disabled={s.checking || undefined}
          onClick={() => { if (!s.checking) void openUpdateDialog(nextButton.current); }}>
          {next}
        </button>
      )}
    </>
  );
}

/** Asks GitHub now. Stays focusable while checking (aria-disabled), so focus is not lost. */
export function CheckForUpdatesButton({ className = "text-button" }: { className?: string }) {
  const s = useUpdateStatus();
  if (s.ready) return null;
  return (
    <button
      type="button"
      className={`${className} update-check-button`}
      aria-disabled={s.checking || s.installing || undefined}
      onClick={() => { if (!s.checking && !s.installing) void checkForUpdates(); }}
    >
      Check for updates
    </button>
  );
}
