import React, { useRef, useState } from "react";
import { useFocusScope } from "./kit";
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
  | "split-pane";
// Geometry is drawn on a 16 px grid; no scaled 24 px paths. Disclosure and
// inline status glyphs render at 12 px, with a consistent 1.5 stroke width.
const paths: Record<IconName, React.ReactNode> = {
  home: <path d="m2 7 6-5 6 5M3.5 6v8h3v-4h3v4h3V6" />,
  baseline: <path d="M3 14V2h9l-2 3 2 3H3" />,
  plan: <path d="M6 1.5h4v3H6zM1.5 11.5h4v3h-4zM10.5 11.5h4v3h-4zM8 4.5v4M3.5 11.5v-3h9v3" />,
  query: <path d="m5 5-3 3 3 3m6-6 3 3-3 3M9 2 7 14" />,
  server: <><rect x="2" y="2" width="12" height="5" rx="1" /><rect x="2" y="9" width="12" height="5" rx="1" /><path d="M5 4.5h.01M5 11.5h.01M9 4.5h2M9 11.5h2" /></>,
  database: <><ellipse cx="8" cy="3.5" rx="5.5" ry="2" /><path d="M2.5 3.5v9c0 2.7 11 2.7 11 0v-9M2.5 8c0 2.7 11 2.7 11 0" /></>,
  investigation: <path d="M1.5 4h5l2 2h6v7.5h-13z" />,
  learn: <path d="M8 4c-2-1.5-4-1.5-6-.5v10c2-1 4-1 6 .5 2-1.5 4-1.5 6-.5v-10c-2-1-4-1-6 .5zm0 0v10" />,
  teach: <path d="m1.5 5 6.5-3 6.5 3L8 8zM4 6.5v4c2 2 6 2 8 0v-4M14.5 5v7" />,
  connection: <path d="M6 1.5v3m4-3v3M4 4.5h8V7a4 4 0 0 1-8 0zM8 11v3.5" />,
  key: <><circle cx="5" cy="11" r="3" /><path d="m7 9 6.5-6.5m-2 2 2 2m-4 0 2 2" /></>,
  settings: <><path d="M2 4.5h6m4 0h2M2 11.5h2m4 0h6" /><circle cx="10" cy="4.5" r="2" /><circle cx="6" cy="11.5" r="2" /></>,
  more: <><circle cx="3" cy="8" r=".75" /><circle cx="8" cy="8" r=".75" /><circle cx="13" cy="8" r=".75" /></>,
  menu: <path d="M2 4h12M2 8h12M2 12h12" />,
  import: <path d="M8 1.5v9m-3-3 3 3 3-3M2 10v4h12v-4" />,
  export: <path d="M8 10V1.5m-3 3 3-3 3 3M2 10v4h12v-4" />,
  compare: <path d="M2 5h12l-3-3M14 11H2l3 3" />,
  plus: <path d="M8 3v10M3 8h10" />,
  close: <path d="m4 4 8 8m-8 0 8-8" />,
  chevron: <path d="m6 3 5 5-5 5" />,
  search: <><circle cx="7" cy="7" r="4.5" /><path d="m10.5 10.5 3 3" /></>,
  check: <path d="m3 8 3 3 7-7" />,
  health: <path d="M1.5 8H4l2-5 4 10 2-5h2.5" />,
  engine: <><rect x="4" y="4" width="8" height="8" rx="1" /><path d="M6 1.5V4m4-2.5V4M6 12v2.5m4-2.5v2.5M1.5 6H4m-2.5 4H4m8-4h2.5M12 10h2.5" /></>,
  analysis: <><path d="M6 3H3v11h10V3h-3M5 7l1 1 2-2m-3 5 1 1 2-2m2-3h1m-1 4h1" /><rect x="6" y="1.5" width="4" height="3" rx=".5" /></>,
  statements: <path d="M3 13V7m4 6V3m4 10V9M1.5 14h13" />,
  sessions: <><rect x="1.5" y="2.5" width="13" height="9" rx="1" /><path d="m4 5 2 2-2 2m4 0h3M6 14h4m-2-2.5V14" /></>,
  users: <><circle cx="6" cy="5" r="2.5" /><path d="M1.5 14v-1a4.5 4.5 0 0 1 9 0v1m1-10.5a2.5 2.5 0 0 1 0 4.5m1 2a4.5 4.5 0 0 1 2 4" /></>,
  waits: <path d="M4 1.5h8m-8 13h8M5 1.5c0 4 6 4 6 6.5s-6 2.5-6 6.5m6-13c0 4-6 4-6 6.5s6 2.5 6 6.5" />,
  io: <path d="M5 14V2L2 5m3-3 3 3m3-3v12l-3-3m3 3 3-3" />,
  lock: <><rect x="3" y="7" width="10" height="7" rx="1.5" /><path d="M5 7V4.5a3 3 0 0 1 6 0V7m-3 3v1" /></>,
  index: <path d="M2 4h2m-2 4h2m-2 4h2M7 4h7M7 8h7m-7 4h7" />,
  diagram: <path d="M1.5 2h5v5h-5zM9.5 9h5v5h-5zM6.5 4.5H8v7h1.5M1.5 4h5m3 7h5" />,
  table: <><rect x="1.5" y="2.5" width="13" height="11" rx="1" /><path d="M1.5 6.5h13m-13 3.5h13M6 6.5v7" /></>,
  configuration: <><rect x="1.5" y="5" width="13" height="6" rx="3" /><circle cx="11" cy="8" r="1" /></>,
  instruments: <path d="M2 12a6 6 0 1 1 12 0M8 12l3-4M8 12h.01" />,
  replication: <><rect x="1.5" y="2" width="4" height="4" rx="1" /><rect x="10.5" y="10" width="4" height="4" rx="1" /><path d="M5.5 4h7v4m-2-2 2 2 2-2M10.5 12h-7V8m-2 2 2-2 2 2" /></>,
  maintenance: <path d="m9 4 3-3a4 4 0 0 1-5 5l-5 5a2 2 0 0 0 3 3l5-5a4 4 0 0 0 5-5l-3 3z" />,
  wrench: <path d="m9 4 3-3a4 4 0 0 1-5 5l-5 5a2 2 0 0 0 3 3l5-5a4 4 0 0 0 5-5l-3 3z" />,
  info: <><circle cx="8" cy="8" r="6" /><path d="M8 7v4m0-6v.01" /></>,
  refresh: <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2v3h-3" />,
  back: <path d="m10 3-5 5 5 5" />,
  disclosure: <path d="m6 4 4 4-4 4" />,
  warning: <path d="m8 2 6.5 12h-13zM8 6v3m0 2.5v.01" />,
  columns: <><rect x="1.5" y="2.5" width="13" height="11" rx="1" /><path d="M6 2.5v11m4-11v11" /></>,
  inspector: <><rect x="1.5" y="2.5" width="13" height="11" rx="1" /><path d="M10 2.5v11" /></>,
  "split-pane": <><rect x="1.5" y="2.5" width="13" height="11" rx="1" /><path d="M8 2.5v11" /></>,
  audit: <path d="M4 1.5h9v13H4zm0 0H2v13h2M7 5h3M7 8h3m-3 3h2" />,
  run: <path d="m5 2.5 8 5.5-8 5.5z" />,
  runAll: <path d="m2 2.5 6 5.5-6 5.5zm6 0 6 5.5-6 5.5z" />,
  stop: <rect x="4" y="4" width="8" height="8" rx="1" />,
  history: <><path d="M2 4v3h3M2 7a6 6 0 1 1 1 5M8 4.5V8l2.5 1.5" /></>,
  copy: <><rect x="5.5" y="5.5" width="8" height="8" rx="1" /><path d="M10.5 5.5V2.5h-8v8h3" /></>,
  browser: <><rect x="1.5" y="2.5" width="13" height="11" rx="1" /><path d="M6 2.5v11M3.5 6h.01m-.01 3h.01" /></>,
  view: <><rect x="1.5" y="2.5" width="13" height="11" rx="1" /><path d="M1.5 6.5h13" /><circle cx="8" cy="10" r="1.5" /></>,
};
export function Icon({ name, size }: { name: IconName; size?: number }) {
  const inline = ["chevron", "disclosure", "back", "check", "warning"].includes(name);
  const pixels = size && size > 16 ? size : inline || size === 12 ? 12 : 16;
  return (
    <svg width={pixels} height={pixels} viewBox="0 0 16 16" fill="none"
      stroke="currentColor" strokeWidth="1.5"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {paths[name]}
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
        <span>Processed locally on your computer.</span>
        <button
          className="button primary"
          disabled={busy || !raw.trim()}
          onClick={() => onPaste(raw, name.trim() || "pasted-plan.json")}
        >
          {busy ? "Analyzing…" : "Analyze plan"}
          <Icon name="chevron" size={15} />
        </button>
      </div>
    </dialog>
  );
}
