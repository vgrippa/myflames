// Check for updates: the status line and button in Home's version footer and
// in About myflames. GitHub is asked only when the button is clicked; the last
// check (kept in the workspace store) is shown with its local time.
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { AppUpdateInfo } from "./api";
import { UpdateDialog } from "./UpdateDialog";
import { checkForUpdates, checkedWhen, loadUpdateStatus, updateStatusText, useUpdateStatus } from "./updateStatus";

/** Re-render once a minute so "today" and the 24-hour limit stay true. */
function useMinuteClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** The last check's outcome and time, plus Download when a newer release was found. */
export function UpdateCheckStatus() {
  const s = useUpdateStatus();
  const now = useMinuteClock();
  // The dialog shows the result captured when Download was clicked, never a later one.
  const [dialogInfo, setDialogInfo] = useState<AppUpdateInfo | null>(null);
  const downloadButton = useRef<HTMLButtonElement>(null);
  useEffect(() => loadUpdateStatus(), []);
  const when = s.last ? checkedWhen(s.last.checked_at, now) : null;
  // A time that cannot be read is never shown as a current result.
  const stale = !!s.last && (!when || when.stale);
  const outcome = s.checking ? "checking" : s.last?.outcome || (s.loaded ? "never" : s.loadFailed ? "unknown" : "loading");
  const available = s.last?.outcome === "available" && !stale;
  async function download() {
    if (s.checking) return;
    // A stored result has no download details: ask again (a click), then open.
    const info = s.info?.update_available ? s.info : await checkForUpdates();
    if (info?.update_available) setDialogInfo(info);
  }
  return (
    <>
      <span className="update-check" data-outcome={outcome} data-stale={stale || undefined} role="status" aria-live="polite">
        {s.checking ? (
          <span className="update-check-status"><span className="spinner" aria-hidden="true" />Checking for updates…</span>
        ) : s.loaded || s.loadFailed || s.last ? (
          <span className="update-check-status">{updateStatusText(s.last, stale, s.loadFailed)}</span>
        ) : null}
        {!s.checking && s.last && when && (
          <time className="update-check-time" dateTime={s.last.checked_at}
            title={stale ? `${when.title}. More than a day ago: check again for the current release.` : when.title}>
            {s.last.outcome === "failed" ? "Tried" : "Checked"} {when.text}
          </time>
        )}
      </span>
      {available && (
        <button ref={downloadButton} type="button" className="text-button update-check-download"
          aria-disabled={s.checking || undefined} onClick={() => void download()}>
          Download…
        </button>
      )}
      {/* Rendered at the page root so About's styles and layout never reach it. */}
      {dialogInfo && createPortal(
        <UpdateDialog updateInfo={dialogInfo} returnFocus={downloadButton} onClose={() => setDialogInfo(null)} />,
        document.body,
      )}
    </>
  );
}

/** Asks GitHub now. Stays focusable while checking (aria-disabled), so focus is not lost. */
export function CheckForUpdatesButton({ className = "text-button" }: { className?: string }) {
  const s = useUpdateStatus();
  return (
    <button
      type="button"
      className={`${className} update-check-button`}
      aria-disabled={s.checking || undefined}
      onClick={() => { if (!s.checking) void checkForUpdates(); }}
    >
      Check for updates
    </button>
  );
}
