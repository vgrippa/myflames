import { useEffect, useRef, useState, type RefObject } from "react";
import { appVersion, downloadAppUpdate, type AppUpdateInfo } from "./api";
import { Icon } from "./components";
import { useFocusScope } from "./kit";

export function UpdateDialog({
  updateInfo,
  onClose,
  returnFocus,
}: {
  updateInfo: AppUpdateInfo | null;
  onClose: () => void;
  returnFocus?: RefObject<HTMLElement | null>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadResult, setDownloadResult] = useState<{ status: string; message: string; download_path?: string; html_url?: string } | null>(null);
  const [error, setError] = useState("");

  useFocusScope(dialog, {
    open: true,
    onClose,
    initialFocus: () => closeButton.current,
    returnFocus: () => returnFocus?.current ?? null,
  });

  useEffect(() => {
    const d = dialog.current;
    if (d && !d.open) d.showModal();
  }, []);

  async function handleUpdate() {
    if (!updateInfo) return;
    setDownloading(true);
    setError("");
    setDownloadResult(null);
    try {
      const res = await downloadAppUpdate(updateInfo.asset_url, updateInfo.html_url);
      setDownloadResult(res);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to download update.");
    } finally {
      setDownloading(false);
    }
  }

  const current = updateInfo?.current_version || appVersion();
  const latest = updateInfo?.latest_version || current;
  const isNewer = updateInfo?.update_available ?? false;

  return (
    <dialog
      ref={dialog}
      className="dialog update-dialog"
      aria-labelledby="update-dialog-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="dialog-heading">
        <h2 id="update-dialog-title">Software Update</h2>
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

        {isNewer ? (
          <div className="update-status-badge available">
            <Icon name="import" size={14} />
            <span>A new version of myflames is available!</span>
          </div>
        ) : (
          <div className="update-status-badge up-to-date">
            <Icon name="check" size={14} />
            <span>You are running the newest version.</span>
          </div>
        )}

        {updateInfo?.release_title && (
          <div className="update-release-title">
            <strong>{updateInfo.release_title}</strong>
          </div>
        )}

        {updateInfo?.release_notes && (
          <div className="update-notes-container">
            <div className="update-notes-heading">Release Notes</div>
            <pre className="update-notes-content">{updateInfo.release_notes}</pre>
          </div>
        )}

        {error && <p className="form-error" role="alert">{error}</p>}

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
        {isNewer && (
          <button
            type="button"
            className="button primary"
            disabled={downloading}
            onClick={handleUpdate}
          >
            {downloading ? (
              <>
                <span className="spinner" aria-hidden="true" />
                <span>Downloading update…</span>
              </>
            ) : (
              <span>Update Binary</span>
            )}
          </button>
        )}
      </div>
    </dialog>
  );
}
