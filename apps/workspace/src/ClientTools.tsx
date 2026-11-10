// Client tools: one dialog (Settings > Client tools…) and one block
// (UsedForServer) that show, for the server in use, which MySQL/MariaDB
// program runs each job (queries, dumps, restores, MySQL Shell), its version,
// where it came from and why (client-tools-1.0). The same helper line
// (clientToolsText.ts) appears everywhere; nothing else builds that text.
import { useEffect, useRef, useState, type Ref, type RefObject } from "react";
import {
  chooseClientTools,
  clientTools,
  type ClientToolUse,
  type ClientTools,
  type ClientToolsEngine,
  type ClientToolsInstallation,
  type ClientToolsUsed,
} from "./api";
import { connectionLabel } from "./binding";
import { toolUseLine, type ToolLineServer } from "./clientToolsText";
import { useConnectionInUse } from "./connectionInUse";
import { errorText, Select, useFocusScope } from "./kit";
import { droppedFilePath } from "./filesApi";
import { Icon } from "./components";
import { MAC, WINDOWS } from "./platform";
import "./client-missing.css";

/** How to get client tools on this computer, by platform. Shown only when no
 * installation of either engine was found. */
export const CLIENT_HINT = WINDOWS
  ? "Install MySQL Server, MySQL Workbench or MariaDB, or choose mysql.exe."
  : MAC
    ? "Install MySQL's client with Homebrew (brew install mysql-client) or the MySQL installer."
    : "Install the MySQL or MariaDB client with your package manager, or choose the client binary.";

const ROLE_LABELS: { role: "client" | "dump" | "load" | "shell" | "backup"; label: string }[] = [
  { role: "client", label: "Queries" },
  { role: "dump", label: "Dumps" },
  { role: "load", label: "Restores" },
  { role: "shell", label: "MySQL Shell" },
  { role: "backup", label: "Physical backup" },
];

const engineWord = (engine: ClientToolsEngine) => (engine === "mariadb" ? "MariaDB" : "MySQL");

/** Which programs run each job for the server in use: one line per role, a
 * warning line for a role with no program, notes below. Change… (when given)
 * opens the Client tools dialog. */
export function UsedForServer({
  used,
  server,
  onChange,
  changeRef,
  className = "",
}: {
  used: ClientToolsUsed;
  server?: ToolLineServer;
  onChange?: () => void;
  /** Focus returns here when the dialog Change… opens closes. */
  changeRef?: Ref<HTMLButtonElement>;
  className?: string;
}) {
  return (
    <div className={`client-tools-used ${className}`.trim()} data-client-tools-used>
      <dl className="client-tools-roles">
        {ROLE_LABELS.map(({ role, label }) => {
          const use: ClientToolUse | null = used[role];
          const missing = used.missing.find((m) => m.role === role);
          // Physical backup appears only when it has something to say.
          if (!use && !missing) return null;
          return (
            <div className="client-tools-role" data-role={role} key={role}>
              <dt>{label}</dt>
              <dd>
                {use ? (
                  <span
                    className={use.warning ? "client-tools-warning" : undefined}
                    title={use.path}
                  >
                    {toolUseLine(use, server).line}
                  </span>
                ) : (
                  <span className="client-tools-warning">{missing?.text}</span>
                )}
              </dd>
            </div>
          );
        })}
      </dl>
      {used.notes.map((note, i) => (
        <p className="hint client-tools-note" key={i}>{note}</p>
      ))}
      {onChange && (
        <button ref={changeRef} type="button" className="button small client-tools-change" onClick={onChange}>
          Change…
        </button>
      )}
    </div>
  );
}

type ClientAction = "read" | "detect" | "choose" | "";

/** The path a select choice sends for an installation: its client program's
 * path, or "bundled" for the bundled installation. */
function choicePath(installation: ClientToolsInstallation): string {
  if (installation.bundled) return "bundled";
  const client = installation.programs[installation.engine === "mariadb" ? "mariadb" : "mysql"];
  return client?.path || installation.folder;
}

/** Settings > Client tools…: what the server in use runs through, the choice
 * per engine (Automatic, an installation, or a chosen file), everything found
 * on this computer, and Detect again. Every action answers in place. */
export function ClientToolsDialog({
  onClose,
  returnFocus,
  onChanged,
}: {
  onClose: () => void;
  returnFocus?: RefObject<HTMLElement | null>;
  /** A stored choice changed: the caller re-reads what depended on it. */
  onChanged?: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const fileEngine = useRef<ClientToolsEngine>("mysql");
  const connection = useConnectionInUse();
  const [answer, setAnswer] = useState<ClientTools | null>(null);
  const [busy, setBusy] = useState<ClientAction>("read");
  const [error, setError] = useState("");
  const [pathText, setPathText] = useState<Record<ClientToolsEngine, string>>({ mysql: "", mariadb: "" });
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
    clientTools(connection ?? undefined).then(
      (reply) => { if (live) { setAnswer(reply); setBusy(""); } },
      (e) => { if (live) { setError(errorText(e)); setBusy(""); } },
    );
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function apply(action: Exclude<ClientAction, "">, run: () => Promise<ClientTools>) {
    if (busy) return;
    setBusy(action);
    setError("");
    try {
      setAnswer(await run());
      if (action === "choose") onChanged?.();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy("");
    }
  }

  const detect = () => void apply("detect", () => clientTools(connection ?? undefined, true));
  const choose = (engine: ClientToolsEngine, path: string | null) =>
    void apply("choose", () => chooseClientTools(engine, path, connection ?? undefined));

  function onFileChosen(files: FileList | null) {
    const file = files?.[0];
    if (fileInput.current) fileInput.current.value = "";
    if (!file) return;
    const path = droppedFilePath(file);
    if (!path) {
      setError("myflames could not read the file's path. Type the full path instead.");
      return;
    }
    choose(fileEngine.current, path);
  }

  function engineSection(engine: ClientToolsEngine) {
    const installations = (answer?.installations || []).filter((i) => i.engine === engine);
    const choice = answer?.choice[engine];
    const resolved = choice?.installation && installations.some((i) => i.key === choice.installation)
      ? choice.installation
      : "";
    const value = !choice || choice.path === null ? "auto" : resolved || "broken";
    const options: [string, string][] = [
      ["auto", "Automatic (best match for each server)"],
      ...installations.map((i): [string, string] => [i.key, `${i.version} · ${i.origin_label}`]),
      ...(value === "broken" ? [["broken", `${choice?.path} (no longer works)`] as [string, string]] : []),
    ];
    const selected = installations.find((i) => i.key === value);
    return (
      <section className="client-tools-section" key={engine} data-engine={engine}>
        <label className="field client-tools-engine-field">
          {engineWord(engine)} servers
          <Select
            label={`${engineWord(engine)} servers`}
            value={value}
            options={options}
            disabled={!!busy}
            onChange={(v) => {
              if (v === "auto") choose(engine, null);
              else if (v !== "broken") {
                const installation = installations.find((i) => i.key === v);
                if (installation) choose(engine, choicePath(installation));
              }
            }}
          />
        </label>
        {selected && <p className="hint client-tools-folder">{selected.folder}</p>}
        {choice?.error && <p className="form-error" role="alert">{choice.error}</p>}
        {canPickFile ? (
          <button
            type="button"
            className="button small"
            disabled={!!busy}
            onClick={() => { fileEngine.current = engine; fileInput.current?.click(); }}
          >
            {busy === "choose" ? "Checking…" : "Choose file…"}
          </button>
        ) : (
          <form
            className="client-path-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (pathText[engine].trim()) choose(engine, pathText[engine].trim());
            }}
          >
            <label className="field client-path-field">
              Full path of the {engineWord(engine)} client
              <input
                value={pathText[engine]}
                spellCheck={false}
                autoCapitalize="off"
                placeholder={WINDOWS ? "C:\\Program Files\\MySQL\\bin\\mysql.exe" : engine === "mariadb" ? "/usr/local/bin/mariadb" : "/usr/local/bin/mysql"}
                onChange={(e) => setPathText((t) => ({ ...t, [engine]: e.target.value }))}
              />
            </label>
            <button type="submit" className="button" disabled={!!busy || !pathText[engine].trim()}>
              {busy === "choose" ? "Checking…" : "Use this path"}
            </button>
          </form>
        )}
      </section>
    );
  }

  const usedConnection = connection && answer?.used && answer.server ? connection : null;
  const noneFound = !!answer && !answer.installations.length;

  return (
    <dialog
      ref={dialog}
      className="import-dialog client-dialog client-tools-dialog"
      aria-labelledby="client-dialog-title"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="dialog-heading">
        <h2 id="client-dialog-title">Client tools</h2>
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
        {busy === "read" && !answer && (
          <p className="hint" role="status">Reading the client tools on this computer…</p>
        )}
        {usedConnection && answer?.used && answer.server && (
          <section className="client-tools-section" aria-label={`Used for ${connectionLabel(usedConnection)}`}>
            <strong>Used for {connectionLabel(usedConnection)}</strong>
            <UsedForServer used={answer.used} server={answer.server} />
          </section>
        )}
        {!usedConnection && connection && answer?.server_error && (
          <p className="hint">{answer.server_error.error}</p>
        )}

        {engineSection("mysql")}
        {engineSection("mariadb")}
        <input
          ref={fileInput}
          type="file"
          hidden
          aria-label="Choose the mysql or mariadb client binary"
          onChange={(e) => onFileChosen(e.target.files)}
        />

        <section className="client-tools-section">
          <div className="client-detected-head">
            <strong>Found on this computer</strong>
            <button type="button" className="button small" disabled={!!busy} onClick={detect}>
              {busy === "detect" ? (
                <>
                  <span className="spinner" aria-hidden="true" />
                  <span>Detecting…</span>
                </>
              ) : (
                "Detect again"
              )}
            </button>
          </div>
          {noneFound && <p className="hint">No client tools found in the usual places.</p>}
          {noneFound && <p className="hint client-missing-hint">{CLIENT_HINT}</p>}
          {!!answer?.installations.length && (
            <table className="client-tools-table">
              <thead>
                <tr><th>Engine and version</th><th>From</th><th>Folder</th><th>Programs</th></tr>
              </thead>
              <tbody>
                {answer.installations.map((i) => (
                  <tr key={i.key} data-installation={i.key}>
                    <td>{engineWord(i.engine)} {i.version}</td>
                    <td>{i.origin_label}</td>
                    <td><code className="client-path" title={i.found_in}>{i.folder}</code></td>
                    <td>{Object.keys(i.programs).join(", ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!!answer?.shells.length && (
            <ul className="client-tools-shells">
              {answer.shells.map((s) => (
                <li key={s.path} title={s.realpath || s.path}>
                  MySQL Shell {s.version} · {s.origin_label} <code className="client-path hint">{s.path}</code>
                </li>
              ))}
            </ul>
          )}
        </section>

        {error && (
          <p className="form-error" role="alert">{error}</p>
        )}
      </div>

      <div className="dialog-actions">
        <div className="action-spacer" />
        <button type="button" className="text-button" onClick={onClose}>
          Close
        </button>
      </div>
    </dialog>
  );
}
