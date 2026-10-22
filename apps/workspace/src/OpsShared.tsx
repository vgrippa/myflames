// Small pieces shared by Replication and Maintenance (Server > Workload and
// Setup): exact commands with a neutral Copy, engine facts as label and value
// rows, and the plain names of replication channels.
import { useState } from "react";
import { Fields } from "./kit";
import { factText, type Facts, type Json } from "./opsApi";

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

/** "heartbeat_age_s" as "Heartbeat age (s)". */
export function factLabel(key: string) {
  const seconds = key.endsWith("_s");
  const words = (seconds ? key.slice(0, -2) : key).replace(/_/g, " ").replace(/\bgtid\b/gi, "GTID").replace(/\bio\b/g, "I/O").replace(/\bpos\b/g, "position");
  const label = words.charAt(0).toUpperCase() + words.slice(1);
  return seconds ? `${label} (s)` : label;
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
      const rendered = factValue(value);
      const isGtid = GTID_SET_KEY.test(key) && typeof value === "string" && value.trim() !== "";
      return [labels[key] || factLabel(key), isGtid ? formatGtidSet(rendered) : <span className="ops-wrap">{rendered}</span>];
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
