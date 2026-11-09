// A missing MySQL or MariaDB command-line client is recoverable from the
// workspace: the ClientMissingNotice replaces the bare error text wherever a
// read was refused with the client-missing code (api.ts isClientMissing), and
// the DatabaseClientDialog (Settings > Database client…) detects the clients
// on this computer, stores a chosen one, or returns to automatic detection.
import { useEffect, useRef, useState, type RefObject } from "react";
import {
  clientSettings,
  detectClients,
  type ClientSettings,
} from "./api";
import { errorText, useFocusScope } from "./kit";
import { droppedFilePath } from "./filesApi";
import { Icon } from "./components";
import { MAC, WINDOWS } from "./platform";
import "./client-missing.css";

/** How to get a client on this computer, by platform. */
export const CLIENT_HINT = WINDOWS
  ? "Install MySQL Server, MySQL Workbench or MariaDB, or choose mysql.exe."
  : MAC
    ? "Install MySQL's client with Homebrew (brew install mysql-client) or the MySQL installer."
    : "Install the MySQL or MariaDB client with your package manager, or choose the client binary.";

const CLIENT_SENTENCE = "myflames needs the MySQL or MariaDB command-line client to read servers.";

/** Shown instead of the bare error text of a read refused for a missing
 * client: what is wrong, how to fix it, Choose client… and Try again. */
export function ClientMissingNotice({
  onRetry,
  className = "",
}: {
  onRetry?: () => void;
  className?: string;
}) {
  const [choosing, setChoosing] = useState(false);
  const choose = useRef<HTMLButtonElement>(null);
  return (
    <div className={`client-missing ${className}`.trim()} data-client-missing>
      <p className="client-missing-text" role="alert">{CLIENT_SENTENCE}</p>
      <p className="hint client-missing-hint">{CLIENT_HINT}</p>
      <div className="client-missing-actions">
        <button ref={choose} type="button" className="button primary" onClick={() => setChoosing(true)}>
          Choose client…
        </button>
        {onRetry && (
          <button type="button" className="button" onClick={onRetry}>
            Try again
          </button>
        )}
      </div>
      {choosing && <DatabaseClientDialog returnFocus={choose} onClose={() => setChoosing(false)} />}
    </div>
  );
}

type ClientAction = "read" | "detect" | "choose" | "clear" | "";

/** Settings > Database client…: the client in use, the detected ones, a
 * chosen path (a file picker in the desktop app, a text field in a browser)
 * and the way back to automatic detection. Every action answers in place. */
export function DatabaseClientDialog({
  onClose,
  returnFocus,
}: {
  onClose: () => void;
  returnFocus?: RefObject<HTMLElement | null>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [settings, setSettings] = useState<ClientSettings | null>(null);
  const [busy, setBusy] = useState<ClientAction>("read");
  const [error, setError] = useState("");
  const [pathText, setPathText] = useState("");
  // The desktop app resolves a chosen File to its on-disk path; a plain
  // browser cannot, so there the path is typed.
  const canPickFile =
    typeof window !== "undefined" &&
    typeof (window as unknown as { myflamesDesktop?: { pathForFile?: unknown } }).myflamesDesktop?.pathForFile ===
      "function";

  useFocusScope(dialog, {
    open: true,
    onClose,
    initialFocus: () => closeButton.current,
    returnFocus: () => returnFocus?.current ?? null,
  });

  useEffect(() => {
    const d = dialog.current;
    if (d && !d.open) d.showModal();
    let live = true;
    clientSettings().then(
      (answer) => { if (live) { setSettings(answer); setBusy(""); } },
      (e) => { if (live) { setError(errorText(e)); setBusy(""); } },
    );
    return () => { live = false; };
  }, []);

  async function apply(action: Exclude<ClientAction, "">, run: () => Promise<ClientSettings>) {
    if (busy) return;
    setBusy(action);
    setError("");
    try {
      setSettings(await run());
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy("");
    }
  }

  const detect = () => void apply("detect", () => detectClients());
  const choosePath = (path: string) => void apply("choose", () => clientSettings(path));
  const clearChoice = () => void apply("clear", () => clientSettings(null));

  function onFileChosen(files: FileList | null) {
    const file = files?.[0];
    if (fileInput.current) fileInput.current.value = "";
    if (!file) return;
    const path = droppedFilePath(file);
    if (!path) {
      setError("myflames could not read the file's path. Type the full path instead.");
      return;
    }
    choosePath(path);
  }

  const active = settings?.active ?? null;
  const detected = settings?.detected ?? [];
  const shownError = error || settings?.error || "";

  return (
    <dialog
      ref={dialog}
      className="import-dialog client-dialog"
      aria-labelledby="client-dialog-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="dialog-heading">
        <h2 id="client-dialog-title">Database client</h2>
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

      <div className="client-dialog-body">
        {active ? (
          <div className="client-in-use" data-testid="client-in-use">
            <strong>Client in use</strong>
            <code className="client-path">{active.path}</code>
            <span className="hint">
              {active.version} · {active.source === "chosen" ? "Chosen here" : "Found automatically"}
            </span>
          </div>
        ) : (
          <p className="client-none" role="status">
            {busy === "read" ? "Looking for a client…" : "No client found."}
          </p>
        )}
        <p className="hint">{CLIENT_HINT}</p>

        <div className="client-detected-head">
          <strong>Detected clients</strong>
          <button type="button" className="button small" disabled={!!busy} onClick={detect}>
            {busy === "detect" ? (
              <>
                <span className="spinner" aria-hidden="true" />
                <span>Detecting…</span>
              </>
            ) : (
              "Detect"
            )}
          </button>
        </div>
        {settings && !detected.length && (
          <p className="hint">No client found in the usual places.</p>
        )}
        {detected.length > 0 && (
          <ul className="client-detected">
            {detected.map((client) => {
              const inUse = !!settings?.path && settings.path === client.path;
              return (
                <li key={client.path} className="client-detected-row">
                  <span className="client-detected-name">{client.name}</span>
                  <span className="hint client-detected-version">{client.version}</span>
                  <code className="client-path client-detected-path" title={client.path}>{client.path}</code>
                  {inUse ? (
                    <span className="hint client-in-use-label">In use</span>
                  ) : (
                    <button
                      type="button"
                      className="button small"
                      disabled={!!busy}
                      onClick={() => choosePath(client.path)}
                    >
                      {busy === "choose" ? "Checking…" : "Use this one"}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}

        {canPickFile ? (
          <>
            <input
              ref={fileInput}
              type="file"
              hidden
              aria-label="Choose the mysql or mariadb client binary"
              onChange={(e) => onFileChosen(e.target.files)}
            />
            <button
              type="button"
              className="button"
              disabled={!!busy}
              onClick={() => fileInput.current?.click()}
            >
              {busy === "choose" ? "Checking…" : "Choose…"}
            </button>
          </>
        ) : (
          <form
            className="client-path-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (pathText.trim()) choosePath(pathText.trim());
            }}
          >
            <label className="field client-path-field">
              Full path of the client binary
              <input
                value={pathText}
                spellCheck={false}
                autoCapitalize="off"
                placeholder={WINDOWS ? "C:\\Program Files\\MySQL\\bin\\mysql.exe" : "/usr/local/bin/mysql"}
                onChange={(e) => setPathText(e.target.value)}
              />
            </label>
            <button type="submit" className="button" disabled={!!busy || !pathText.trim()}>
              {busy === "choose" ? "Checking…" : "Use this path"}
            </button>
          </form>
        )}

        {shownError && (
          <p className="form-error" role="alert">{shownError}</p>
        )}
      </div>

      <div className="dialog-actions">
        {settings?.path ? (
          <button type="button" className="text-button" disabled={!!busy} onClick={clearChoice}>
            {busy === "clear" ? "Clearing…" : "Use automatic detection"}
          </button>
        ) : (
          <span />
        )}
        <div className="action-spacer" />
        <button type="button" className="text-button" onClick={onClose}>
          Close
        </button>
      </div>
    </dialog>
  );
}
