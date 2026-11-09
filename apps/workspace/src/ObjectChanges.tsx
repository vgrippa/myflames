import { useEffect, useState, type ReactNode } from "react";
import { readObjectAudit, type AuditObject, type AuditRecord, type Connection } from "./api";
import { openAuditLog } from "./ActionReview";
import { sshKey } from "./binding";
import { copyToClipboard, Fields, InspectorSection, StatusPill, ViewStatus } from "./kit";
import "./objectChanges.css";

/** A single-step inverse whose old value the variable audit target retains.
 * Compound SET PERSIST undo and session changes need more context; omit them.
 * SQL is always built afresh by the normal variable planner, never from a log.
 */
export function parameterUndo(record: AuditRecord): { mode: "global" | "persist_only" | "reset_persist"; value: string | null } | null {
  if (record.status !== "ok" || !record.target || typeof record.target !== "object") return null;
  const t = record.target as Record<string, unknown>;
  const value = (v: unknown) => v === null || (typeof v === "string" && v.length <= 4096 && !v.includes("[redacted]"));
  if (record.operation === "set_global" && t.mode === "global" && value(t.global_value))
    return { mode: "global", value: t.global_value as string | null };
  if ((record.operation === "set_persist_only" && t.mode === "persist_only") || (record.operation === "reset_persist" && t.mode === "reset_persist")) {
    const state = t.persisted_state as { state?: unknown } | null;
    if (state?.state === "present" && value(t.persisted_value)) return { mode: "persist_only", value: t.persisted_value as string | null };
    if (record.operation === "set_persist_only" && state?.state === "absent") return { mode: "reset_persist", value: "" };
  }
  return null;
}

/** Keys admitted by audit_object_matches, including every account writer. */
const OPERATION_LABELS: Record<string, string> = {
  editor_run: "Run SQL",
  set_global: "Set global parameter",
  set_persist: "Set and persist parameter",
  set_persist_only: "Persist parameter for restart",
  set_session: "Set session parameter",
  reset_persist: "Reset persisted parameter",
  "accounts.create": "Create account",
  "accounts.drop": "Drop account",
  "accounts.rename": "Rename account",
  "accounts.lock": "Lock account",
  "accounts.unlock": "Unlock account",
  "accounts.expire": "Expire password",
  "accounts.password": "Change password",
  "accounts.grant": "Grant privileges",
  "accounts.revoke": "Revoke privileges",
  "accounts.roles": "Change roles",
  "accounts.options": "Change account options",
};
export function operationLabel(operation: string): string {
  const label = OPERATION_LABELS[operation];
  return typeof label === "string" ? label : operation;
}

export function relativeChangeTime(iso: string, now = Date.now()): string {
  const time = Date.parse(iso);
  if (!Number.isFinite(time)) return iso;
  const seconds = Math.max(0, Math.floor((now - time) / 1000));
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

function StoredStatement({ statement }: { statement: string }) {
  const [copied, setCopied] = useState("");
  return <div className="object-change-statement">
    <pre className="inspector-sql" tabIndex={0} aria-label="Statement as stored"><code><bdi>{statement}</bdi></code></pre>
    <button type="button" className="text-button" aria-label="Copy statement as stored" onClick={() => {
      void copyToClipboard(statement).then(ok => setCopied(ok ? "Copied." : "Select the text to copy it."));
    }}>Copy</button>
    <span className="hint" role="status">{copied}</span>
  </div>;
}

/** Mounted by the inspector only. Identity-tagged state and abort prevent a
 * late reply (including the same server revisited) leaking to another object.
 */
export function ObjectChanges({ connection, object, undo }: {
  connection: Connection; object: AuditObject; undo?: (record: AuditRecord) => ReactNode;
}) {
  const server = `${connection.host}:${connection.port}`;
  const tunnel = sshKey(connection);
  const ssh = tunnel ? { host: tunnel[1], user: tunnel[2], port: tunnel[3] } : undefined;
  const identity = JSON.stringify({ server, ssh, ...object });
  const [result, setResult] = useState<{ identity: string; record?: AuditRecord; error?: string } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setResult(null);
    const { server: endpoint, ssh: targetSsh, ...target } = JSON.parse(identity) as { server: string; ssh?: { host: string; user: string; port: number } } & AuditObject;
    void readObjectAudit(endpoint, target, controller.signal, targetSsh).then(reply => {
      if (!controller.signal.aborted) setResult({ identity, record: reply.records[0] });
    }).catch((e: Error) => {
      if (!controller.signal.aborted) setResult({ identity, error: e.message });
    });
    return () => controller.abort();
  }, [identity]);
  const current = result?.identity === identity ? result : null;
  const record = current?.record;
  // Index history is meaningful only for a record naming this exact index:
  // hide the section while reading and after an empty lookup, but keep a
  // failed lookup visible so its error is not lost.
  if (object.kind === "index" && !record && !current?.error) return null;
  return <InspectorSection title="Changed through myflames">
    <div className="object-changes" data-object-changes={object.kind} aria-busy={!current}>
      <p className="hint">Kept on this computer.</p>
      <ViewStatus loading={!current ? "Reading local changes…" : undefined} error={current?.error} />
      {current && !current.error && !record && <p className="hint">No matching changes kept.</p>}
      {record && <>
        <Fields rows={[
          ["When", <time dateTime={record.attempted_at} title={record.attempted_at}>{relativeChangeTime(record.attempted_at)}</time>],
          ["Operation", <bdi title={record.operation}>{operationLabel(record.operation)}</bdi>],
          ["Outcome", <><StatusPill tone={record.status === "unknown" ? "unknown" : "ok"} label={record.status === "unknown" ? "Outcome unknown" : "OK"} /> <bdi>{record.outcome || record.message || "No outcome stored"}</bdi></>],
        ]} />
        {record.statements.map((statement, i) => <StoredStatement key={`${record.id}:${i}`} statement={statement} />)}
        <div className="object-change-actions">
          {undo?.(record)}
          <button type="button" className="text-button" onClick={() => openAuditLog(record.id)}>Open in Audit log</button>
        </div>
      </>}
    </div>
  </InspectorSection>;
}
