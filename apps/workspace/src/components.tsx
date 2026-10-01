import React, { useRef, useEffect, useState } from "react";
export function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    book: (
      <path d="M12 5C8 2 4 3 2 4v16c3-2 7-2 10 0 3-2 7-2 10 0V4c-2-1-6-2-10 1v15" />
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    file: (
      <>
        <path d="M14 3H5v18h14V8zM14 3v5h5M8 13h8M8 17h5" />
      </>
    ),
    upload: (
      <>
        <path d="M12 16V3m-5 5 5-5 5 5M4 15v6h16v-6" />
      </>
    ),
    chart: (
      <>
        <path d="M4 20h16M6 16v-4m6 4V4m6 12V8" />
      </>
    ),
    compare: (
      <>
        <path d="M4 7h16l-4-4M20 17H4l4 4M4 7v4m16 6v-4" />
      </>
    ),
    close: <path d="m6 6 12 12M6 18 18 6" />,
    chevron: <path d="m9 5 7 7-7 7" />,
    search: (
      <>
        <circle cx="10" cy="10" r="6" />
        <path d="m15 15 5 5" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    flame: (
      <path d="M12 2c2 5-3 6-1 10 2-1 3-3 3-5 6 5 6 10 2 13-5 4-13 0-12-6 0-3 3-6 4-7-1 5 0 5 1 6-1-5 4-6 3-11Z" />
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.file}
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
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="import-dialog"
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onClose();
      }}
      aria-labelledby="import-title"
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
      <p>
        Use EXPLAIN ANALYZE FORMAT=JSON output from MySQL, or ANALYZE
        FORMAT=JSON from MariaDB.
      </p>
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
        <Icon name="upload" size={23} />
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
