// Small pieces shared by Replication and Maintenance (Server > Workload and
// Setup): exact commands with a neutral Copy, engine facts as label and value
// rows, and the plain names of replication channels.
import { useEffect, useId, useRef, useState } from "react";
import { ActionButton, ActionStatus, Fields, useAction } from "./kit";
import { Duration } from "./PerfShared";
import { chooseBackupDirectory, factText, type Facts, type Json } from "./opsApi";
import { baseName, joinPath, localList, parentPath } from "./filesApi";

/** A portable child name, never a path supplied by a database identifier. */
export function dumpFolderStem(name: string, now = new Date()) {
  const letters = Array.from(name.replace(/[\\/<>:"|?*\u0000-\u001f\u007f-\u009f]/g, "-")
    .replace(/\.{2,}/g, "-").replace(/^[.\s-]+|[.\s]+$/g, "")).slice(0, 80);
  while (new TextEncoder().encode(letters.join("")).length > 180) letters.pop();
  const safe = letters.join("") || "dump";
  const date = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), String(now.getDate()).padStart(2, "0")].join("-");
  return `${safe}-${date}`;
}

/** Advisory only: preflight and Start retain their empty-directory checks. */
export async function suggestDumpFolder(parent: string, stem: string, signal?: AbortSignal, reserved: string[] = []) {
  const listing = await localList(parent, signal);
  const names = new Set([...listing.entries.map(e => e.name.toLowerCase()), ...reserved
    .filter(p => parentPath(p) === listing.path).map(p => baseName(p).toLowerCase())]);
  let name = stem, suffix = 2;
  while (names.has(name.toLowerCase())) name = `${stem}-${suffix++}`;
  return joinPath(listing.path, name);
}

/** A manually entered root or dot path is not a child of a Browse choice. */
export function dumpOutputName(value: string, fallback: string) {
  const name = baseName(value);
  return name && name !== "." && name !== ".." && !/[\\/<>:"|?*\u0000-\u001f\u007f-\u009f]/.test(name)
    && !/[.\s]$/.test(name) && !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(name) ? name : fallback;
}

/** Browse chooses an existing parent; it never creates the output directory. */
export function DumpFolderField({ value, stem, onChange, onPicking, disabled }: {
  value: string; stem: string; onChange: (path: string) => void; onPicking: (picking: boolean) => void; disabled: boolean;
}) {
  const id = useId();
  const action = useAction();
  const latest = useRef({ value, disabled, onChange, onPicking });
  latest.current = { value, disabled, onChange, onPicking };
  useEffect(() => () => latest.current.onPicking(false), []);
  async function browse() {
    const previous = value;
    if (disabled || action.pending) return;
    onPicking(true);
    const result = await action.run(async signal => {
      const picked = await chooseBackupDirectory();
      if (picked.cancelled || !picked.path) return "";
      return suggestDumpFolder(picked.path, dumpOutputName(previous, stem), signal);
    }, { pending: "Choose the parent folder in the system dialog.", done: path => path ? "Output folder selected." : "Folder selection cancelled.",
      failed: e => `${(e as Error).message} You can enter the output path above.` });
    if (result.ok && result.value && !latest.current.disabled && latest.current.value === previous)
      latest.current.onChange(result.value);
    if (result.ok || (result.reason !== "busy" && result.reason !== "cancelled")) latest.current.onPicking(false);
  }
  return <div className="form-row ops-folder-row">
    <label htmlFor={id}>Output folder</label>
    <div className="form-row-control ops-field">
      <div className="ops-folder-input">
        <input id={id} value={value} disabled={disabled || action.pending} spellCheck={false} autoComplete="off"
          aria-describedby={`${id}-hint`} placeholder="Enter a new folder path"
          onChange={e => { action.reset(); onChange(e.target.value); }} />
        <ActionButton busy={action.pending} busyLabel="Choosing…" disabled={disabled} aria-label="Browse for output folder" onClick={() => { void browse(); }}>Browse…</ActionButton>
      </div>
      <span id={`${id}-hint`} className="hint">{value ? `Creates ${baseName(value)} in ${parentPath(value)}. ` : "On this computer. "}Browse picks the folder it goes in.</span>
      <ActionStatus state={action} />
    </div>
  </div>;
}

/** The exact statements or commands a confirmation will send. They may be
 * SQL, a Shell expression or an external program, so Copy says only that the
 * text was copied (never "run it yourself", since myflames sends it). */
export function Commands({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState("");
  return (
    <div className="perf-sql ops-commands">
      <pre className="ops-pre" tabIndex={0} aria-label={label}>
        <code>{text}</code>
      </pre>
      <button
        type="button"
        className="text-button"
        aria-label={`Copy: ${label}`}
        onClick={() => {
          void navigator.clipboard.writeText(text).then(
            () => setCopied("Copied."),
            () => setCopied("Select the text to copy it."),
          );
        }}
      >
        Copy
      </button>
      {copied && (
        <span className="hint" role="status">
          {copied}
        </span>
      )}
    </div>
  );
}

/** "heartbeat_age_s" as "Heartbeat age"; values carry their duration units. */
export function factLabel(key: string) {
  const seconds = key.endsWith("_s");
  const words = (seconds ? key.slice(0, -2) : key).replace(/_/g, " ").replace(/\bgtid\b/gi, "GTID").replace(/\bio\b/g, "I/O").replace(/\bpos\b/g, "position");
  const label = words.charAt(0).toUpperCase() + words.slice(1);
  return label;
}

function factValue(value: Json | undefined) {
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") return value.toLocaleString();
  return factText(value);
}

/** Engine facts (lag signals, positions, GTID state) as label and value rows,
 * with names a person reads. Per-domain lists repeat the position text next to
 * them, so they are left out. */
export function FactsList({ facts }: { facts: Facts }) {
  const rows = factRows(facts);
  return rows.length ? <Fields rows={rows} /> : <p className="hint">Not reported by this server.</p>;
}
const GTID_SET_KEY = /(?:^|_)(?:gtid_set|gtid_executed|gtid_purged|slave_pos|current_pos|binlog_pos|failing_gtid)$/i;

/** A GTID set or position in a monospace block that wraps after commas. */
export function formatGtidSet(text: string): React.ReactNode {
  const parts = text.split(",");
  return (
    <code className="ops-gtid">
      {parts.map((part, i) => (
        <span key={i} className="ops-gtid-item">
          {part}{i < parts.length - 1 ? <>,<wbr /></> : ""}
        </span>
      ))}
    </code>
  );
}

/** The rows of FactsList, for callers that join several fact groups into one
 * list (one label column, one value column). *labels* names keys whose
 * generated label would be ambiguous out of context. */
export function factRows(facts: Facts, labels: Record<string, string> = {}): [string, React.ReactNode][] {
  return Object.entries(facts)
    .filter(([key]) => !key.endsWith("_domains"))
    .map(([key, value]): [string, React.ReactNode] => {
      const rendered = key.endsWith("_s") && typeof value === "number" ? <Duration value={value} /> : factValue(value);
      const isGtid = GTID_SET_KEY.test(key) && typeof value === "string" && value.trim() !== "";
      return [labels[key] || factLabel(key), isGtid ? formatGtidSet(value as string) : <span className="ops-wrap">{rendered}</span>];
    });
}

/** The replication channel (MySQL) or connection (MariaDB) as a table cell. */
export function channelCell(name: string) {
  return name || "(default)";
}
/** The channel or connection in a sentence: "channel 'ch80'", "the default connection". */
export function channelPhrase(engine: string | null | undefined, name: string) {
  const word = engine === "mariadb" ? "connection" : "channel";
  return name ? `${word} '${name}'` : `the default ${word}`;
}
/** "MySQL 8.4.11", "MariaDB 11.4.13" (the build suffix of "11.4.13-MariaDB-log" left out),
 * "Percona Server 8.4.11-11" when the server names its product (Percona's release
 * number is part of its version, so it stays). */
export function engineName(engine: string | null | undefined, version?: string | null, product?: string | null) {
  const name = product || (engine === "mariadb" ? "MariaDB" : engine === "mysql" ? "MySQL" : engine || "Server");
  const pattern = name.startsWith("Percona") ? /^\d+(?:\.\d+)*(?:-\d+(?:\.\d+)?)?/ : /^\d+(?:\.\d+)*/;
  const short = version ? version.match(pattern)?.[0] || version : "";
  return short ? `${name} ${short}` : name;
}
/** "1 channel", "2 channels". */
export function plural(n: number, word: string, many = `${word}s`) {
  return `${n.toLocaleString()} ${n === 1 ? word : many}`;
}
