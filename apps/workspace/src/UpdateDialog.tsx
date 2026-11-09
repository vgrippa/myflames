// Software update: the newer release, its notes and the next step. In the
// desktop app, Update downloads and verifies the update file and hands it to
// the app; Restart to update installs it (or it installs when you quit).
// Elsewhere, or when this copy cannot replace itself, Download update saves
// the installer to the Downloads folder as before.
import { useEffect, useRef, useState } from "react";
import { appUpdateStatus, appVersion, downloadAppUpdate, type AppUpdateInfo } from "./api";
import { Icon } from "./components";
import { useFocusScope } from "./kit";
import {
  desktopUpdate, installUpdate, readUpdateSupport, restartToUpdate, stagingPercent, useUpdateStatus,
} from "./updateStatus";

const megabytes = (bytes: number) => `${Math.max(1, Math.round(bytes / 1_000_000))} MB`;
const article = (operation: string) => (/^[aeiou]/i.test(operation) ? "an " : "a ") + operation;
/** "A dump is running", "A dump and a restore are running". */
function runningText(operations: string[]) {
  const named = operations.map(article);
  const list = named.length > 1 ? `${named.slice(0, -1).join(", ")} and ${named[named.length - 1]}` : named[0];
  return `${list.charAt(0).toUpperCase()}${list.slice(1)} ${operations.length > 1 ? "are" : "is"} running`;
}

export function UpdateDialog({
  updateInfo,
  onClose,
  returnFocus,
}: {
  updateInfo: AppUpdateInfo | null;
  onClose: () => void;
  returnFocus?: () => HTMLElement | null;
}) {
  const s = useUpdateStatus();
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadResult, setDownloadResult] = useState<{ status: string; message: string; download_path?: string; html_url?: string } | null>(null);
  const [error, setError] = useState("");
  const [restarting, setRestarting] = useState(false);
  /** Operations a restart would stop, shown before Restart anyway. */
  const [stops, setStops] = useState<string[] | null>(null);

  useFocusScope(dialog, {
    open: true,
    onClose,
    initialFocus: () => closeButton.current,
    returnFocus: () => returnFocus?.() ?? null,
  });

  useEffect(() => {
    const d = dialog.current;
    if (d && !d.open) d.showModal();
    void readUpdateSupport();
  }, []);

  async function handleDownload() {
    if (!updateInfo) return;
    setDownloading(true);
    setError("");
    setDownloadResult(null);
    try {
      const res = await downloadAppUpdate(updateInfo.asset_url, updateInfo.html_url);
      setDownloadResult(res);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Could not download the update.");
    } finally {
      setDownloading(false);
    }
  }

  async function handleRestart(confirmed: boolean) {
    if (restarting) return;
    setRestarting(true);
    if (!confirmed) {
      let running: string[] = [];
      try { running = (await appUpdateStatus()).active_operations ?? []; } catch { /* unknown: restart as asked */ }
      if (running.length) { setStops(running); setRestarting(false); return; }
    }
    await restartToUpdate();
    setRestarting(false);
  }

  const current = updateInfo?.current_version || appVersion();
  const latest = s.ready || updateInfo?.latest_version || current;
  const isNewer = !!s.ready || (updateInfo?.update_available ?? false);
  // In place: the desktop app can replace this copy. Unknown (null) until it answers.
  const inPlace = desktopUpdate() ? (s.support ? s.support.supported : null) : false;
  const percent = stagingPercent(s.staging);
  const downloadingNow = s.installing && s.staging?.state === "running";

  return (
    <dialog
      ref={dialog}
      className="import-dialog update-dialog"
      aria-labelledby="update-dialog-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="dialog-heading">
        <h2 id="update-dialog-title">Software update</h2>
        <button
          ref={closeButton}
          type="button"
          className="icon-button"
          aria-label="Close dialog"
          onClick={onClose}
        >
          <Icon name="close" size={14} />
        </button>
      </div>

      <div className="update-body">
        <div className="update-versions">
          <div className="version-box current">
            <span className="version-label">Current version</span>
            <strong className="version-value">{current ? `v${current}` : "Unknown"}</strong>
          </div>
          <Icon name="chevron" size={16} />
          <div className={`version-box latest${isNewer ? " is-newer" : ""}`}>
            <span className="version-label">Latest release</span>
            <strong className="version-value">{latest ? `v${latest}` : "Unknown"}</strong>
          </div>
        </div>

        {s.ready ? (
          <div className="update-status-badge ready" role="status">
            <Icon name="check" size={14} />
            <span>myflames {s.ready} is ready. Restart now, or it installs when you quit myflames.</span>
          </div>
        ) : s.installing ? (
          <div className="update-progress" role="status" aria-live="polite">
            <progress max={100} value={downloadingNow && percent !== null ? percent : undefined} aria-label="Update progress" />
            <span>
              {downloadingNow
                ? (percent !== null && s.staging?.total
                  ? `Downloading: ${percent}% (${megabytes(s.staging.received)} of ${megabytes(s.staging.total)})`
                  : "Downloading…")
                : s.checking ? "Checking for the latest release…" : "Verifying and preparing the update…"}
            </span>
          </div>
        ) : isNewer ? (
          <div className="update-status-badge available">
            <Icon name="import" size={14} />
            <span>myflames {latest} is available.</span>
          </div>
        ) : (
          <div className="update-status-badge up-to-date">
            <Icon name="check" size={14} />
            <span>myflames is up to date.</span>
          </div>
        )}

        {isNewer && !s.ready && inPlace === false && s.support?.reason && (
          <p className="hint update-in-place-hint">
            {s.support.reason} Download update saves the installer in your Downloads folder instead.
          </p>
        )}

        {updateInfo?.release_title && (
          <div className="update-release-title">
            <strong>{updateInfo.release_title}</strong>
          </div>
        )}

        {updateInfo?.release_notes && (
          <div className="update-notes-container">
            <div className="update-notes-heading">Release notes</div>
            <pre className="update-notes-content">{updateInfo.release_notes}</pre>
          </div>
        )}

        {(error || s.installError) && <p className="form-error" role="alert">{error || s.installError}</p>}

        {stops && (
          <p className="form-error" role="alert">
            {runningText(stops)}. Restarting stops {stops.length > 1 ? "them" : "it"} before {stops.length > 1 ? "they finish" : "it finishes"}.
          </p>
        )}

        {downloadResult && (
          <div className="update-download-result" role="status">
            <Icon name="check" size={16} />
            <span>{downloadResult.message}</span>
            {downloadResult.html_url && (
              <a
                href={downloadResult.html_url}
                target="_blank"
                rel="noopener noreferrer"
                className="button small"
              >
                Open release page
              </a>
            )}
          </div>
        )}
      </div>

      <div className="dialog-actions">
        {updateInfo?.html_url && (
          <a
            href={updateInfo.html_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-button"
          >
            View on GitHub
          </a>
        )}
        <div className="action-spacer" />
        <button type="button" className="text-button" onClick={onClose}>
          {isNewer ? "Later" : "Close"}
        </button>
        {isNewer && !s.ready && (inPlace === false || s.installError) && (
          <button
            type="button"
            className={inPlace === false ? "button primary" : "text-button"}
            disabled={downloading || s.installing}
            onClick={handleDownload}
          >
            {downloading ? (
              <>
                <span className="spinner" aria-hidden="true" />
                <span>Downloading…</span>
              </>
            ) : (
              <span>{inPlace === false ? "Download update" : "Download instead"}</span>
            )}
          </button>
        )}
        {isNewer && !s.ready && inPlace !== false && (
          <button
            type="button"
            className="button primary"
            disabled={s.installing || inPlace === null}
            onClick={() => void installUpdate()}
          >
            {s.installing ? (
              <>
                <span className="spinner" aria-hidden="true" />
                <span>Updating…</span>
              </>
            ) : (
              <span>{s.installError ? "Try again" : "Update"}</span>
            )}
          </button>
        )}
        {s.ready && (
          <button
            type="button"
            className="button primary"
            aria-disabled={restarting || undefined}
            onClick={() => void handleRestart(!!stops)}
          >
            {restarting ? (
              <>
                <span className="spinner" aria-hidden="true" />
                <span>Restarting…</span>
              </>
            ) : (
              <span>{stops ? "Restart anyway" : "Restart to update"}</span>
            )}
          </button>
        )}
      </div>
    </dialog>
  );
}
