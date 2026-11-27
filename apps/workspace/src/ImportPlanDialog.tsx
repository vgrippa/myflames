// The Import dialog opens on a user action (Import… in the shell, Import in
// Investigations), so it loads with its first opener instead of the entry
// chunk (docs/DEVELOPER_REFERENCE.md, "Bundle and lazy loading"). The shell
// preloads it beside the first screen and awaits that preload before opening.
import { useRef, useState } from "react";
import { ActionButton, ActionStatus, useFocusScope, useSince } from "./kit";
import { Icon } from "./components";

export function ImportDialog({
  busy,
  error,
  onClose,
  onFile,
  onPaste,
}: {
  busy: boolean;
  error: string;
  onClose: () => void;
  onFile: (file?: File) => void;
  onPaste: (raw: string, name: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [raw, setRaw] = useState("");
  const [name, setName] = useState("");
  const since = useSince(busy);
  useFocusScope(dialog, { open: true, onClose: () => { if (!busy) onClose(); } });
  return (
    <dialog
      ref={dialog}
      className="import-dialog"
      tabIndex={-1}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onClose();
      }}
      aria-labelledby="import-title"
      aria-describedby="import-format-hint"
    >
      <div className="dialog-heading">
        <div>
          <div className="eyebrow">ADD TO YOUR WORKSPACE</div>
          <h2 id="import-title">Import an execution plan</h2>
        </div>
        <button
          className="icon-button"
          aria-label="Close import"
          onClick={onClose}
          disabled={busy}
        >
          <Icon name="close" />
        </button>
      </div>
      <p className="hint" id="import-format-hint">Paste or choose EXPLAIN ANALYZE FORMAT=JSON output, or a myflames capture .json.</p>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      <label
        className={`file-picker ${busy ? "disabled" : ""}`}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          const file = e.dataTransfer.files[0];
          if (!busy && file) onFile(file);
        }}
      >
        <Icon name="import" size={22} />
        <strong>Choose a JSON file or drop it here</strong>
        <span>Up to 5 MiB</span>
        <input
          type="file"
          accept=".json,application/json,text/plain"
          aria-label="Choose JSON plan file"
          disabled={busy}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) onFile(file);
            e.target.value = "";
          }}
        />
      </label>
      <div className="divider">
        <span>or paste your plan</span>
      </div>
      <label className="field">
        Plan name{" "}
        <input
          placeholder="e.g. orders-before.json"
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
          maxLength={150}
        />
      </label>
      <label className="field">
        EXPLAIN JSON
        <textarea
          placeholder={'{\n  "operation": "…",\n  "inputs": […]\n}'}
          value={raw}
          disabled={busy}
          onChange={(e) => setRaw(e.target.value)}
          spellCheck={false}
        />
      </label>
      <div className="dialog-actions">
        {/* Always mounted: announces the analysis; counts its seconds after 1 s. */}
        <ActionStatus state={{ phase: busy ? "pending" : "idle", message: "Analyzing the plan on this computer…", startedAt: since }}
          slowText="Large plans take longer." />
        {!busy && <span>Processed locally on your computer.</span>}
        <ActionButton
          primary
          busy={busy}
          busyLabel="Analyzing…"
          disabled={!busy && !raw.trim()}
          onClick={() => onPaste(raw, name.trim() || "pasted-plan.json")}
        >
          Analyze plan
        </ActionButton>
      </div>
    </dialog>
  );
}
