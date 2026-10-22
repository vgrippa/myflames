import React, { useRef, useState } from "react";
import { ActionButton, ActionStatus, useFocusScope, useSince } from "./kit";
import { icons, type IconShape } from "./icons.generated";
/**
 * Icons are named by meaning, and each meaning has its own glyph: a place in
 * the sidebar never shares a drawing with another place or with an action.
 * 16 px in rows and navigation; 12 px disclosures and inline status glyphs.
 */
export type IconName =
  | "home"
  | "plan"
  | "query"
  | "server"
  | "investigation"
  | "learn"
  | "connection"
  | "key"
  | "settings"
  | "menu"
  | "more"
  | "import"
  | "export"
  | "compare"
  | "plus"
  | "close"
  | "chevron"
  | "search"
  | "check"
  // Server views (one glyph each) and view controls.
  | "health"
  | "engine"
  | "analysis"
  | "tuning"
  | "errorlog"
  | "statements"
  | "sessions"
  | "waits"
  | "io"
  | "lock"
  | "index"
  | "table"
  | "diagram"
  | "configuration"
  | "instruments"
  | "replication"
  | "cluster"
  | "proxy"
  | "maintenance"
  | "info"
  | "refresh"
  | "back"
  | "disclosure"
  | "warning"
  | "columns"
  | "inspector"
  | "audit"
  // Query editor actions and panes.
  | "run"
  | "runAll"
  | "stop"
  | "history"
  | "copy"
  | "browser"
  | "view"
  | "baseline"
  | "database"
  | "teach"
  | "users"
  | "wrench"
  | "split-pane"
  | "chevron-down"
  | "branches"
  | "edit"
  | "trash"
  | "globe"
  | "optimize"
  | "shield"
  | "minus"
  | "block"
  | "clean"
  | "undo"
  // Query editor: Format SQL.
  | "format";
// Glyphs are Hugeicons (free Stroke Rounded set, MIT) vendored into
// icons.generated.ts by scripts/vendor-hugeicons.mjs, drawn on a 24 px grid and
// scaled to 16 px (rows, navigation) or 12 px (disclosures, inline status).
const data: Record<IconName, IconShape> = icons;
export function isIconName(name: string): name is IconName {
  return Object.prototype.hasOwnProperty.call(data, name);
}
export function Icon({ name, size }: { name: IconName; size?: number }) {
  const inline = ["chevron", "disclosure", "back", "check", "warning"].includes(name);
  const pixels = size && size > 16 ? size : inline || size === 12 ? 12 : 16;
  return (
    <svg width={pixels} height={pixels} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth={pixels <= 12 ? 2 : 1.75}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {data[name].map(([tag, attrs], i) => React.createElement(tag, { key: i, ...attrs }))}
    </svg>
  );
}
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
