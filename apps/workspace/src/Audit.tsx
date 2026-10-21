// The Audit log: every write myflames sent, refused, or the user declined,
// read from this computer's workspace database (dbops.py, audit-1.0,
// /api/audit/*). It is local, not a server view, so it opens from the
// Settings menu as a screen of its own. Statements appear as stored (secrets
// redacted); a password is never recorded.
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, download, time, type AuditExport, type AuditRecord, type AuditReport, type AuditStatus, type Profile } from "./api";
import { Icon } from "./components";
import {
  DataTable,
  EmptyState,
  Fields,
  InspectorContext,
  InspectorSection,
  NumberField,
  Popover,
  StatusPill,
  SummaryLine,
  ToolbarSelect,
  ViewLayout,
  ViewStatus,
  closePopovers,
  storeInspectorWidth,
  storedInspectorWidth,
  type Column,
  type InspectorContent,
  type InspectorShared,
  type Selection,
  type Tone,
} from "./kit";
import "./audit.css";

/** Below this width the inspector is a bottom sheet (as in Server). */
const SHEET_WIDTH = 1040;
/** Records per page: the newest first, then Load older pages through the
 * rest (every filter applies on the server before the page is cut). */
const PAGE = 500;
const MAX_RETENTION = 3650;

/** Each status: its word, tone, and what it means for the statement. */
export const AUDIT_STATUS: Record<AuditStatus, { word: string; tone: Tone; meaning: string }> = {
  ok: { word: "Done", tone: "ok", meaning: "Sent, and the server answered without an error." },
  error: { word: "Failed", tone: "problem", meaning: "The server or the connection returned an error; the message says which." },
  declined: { word: "Declined", tone: "off", meaning: "Cancelled in the confirmation. Nothing was sent." },
  refused: {
    word: "Refused",
    tone: "info",
    meaning: "myflames did not send it: a read-only connection, or the target changed or ended after you confirmed.",
  },
  unknown: {
    word: "Outcome unknown",
    tone: "watch",
    meaning: "Interrupted, timed out or disconnected after the statement may have reached the server. Check the server to see whether it ran.",
  },
  pending: {
    word: "Not finished",
    tone: "watch",
    meaning: "Recorded before sending, but myflames stopped before it recorded the result. The statement may have run.",
  },
};
const STATUS_ORDER: AuditStatus[] = ["ok", "error", "unknown", "pending", "refused", "declined"];
const OPERATIONS: Record<string, string> = {
  kill: "Kill connection", kill_query: "Kill query",
  set_global: "SET GLOBAL", set_persist: "SET PERSIST", set_persist_only: "SET PERSIST_ONLY",
  set_session: "SET SESSION", reset_persist: "RESET PERSIST",
  editor_run: "Query editor run", editor_commit: "Commit", editor_rollback: "Roll back", editor_safe_updates: "Safe updates",
  replication_skip: "Skip this transaction", replication_skip_loop: "Skip until stopped",
  dump: "Dump", restore: "Restore", clone: "Clone", sql: "SQL from the command line",
};
/** Users and roles writes are recorded as accounts.<operation>. */
const ACCOUNT_OPERATIONS: Record<string, string> = {
  create: "Create account", drop: "Drop account", rename: "Rename account", lock: "Lock account", unlock: "Unlock account",
  expire: "Expire password", password: "Set password", grant: "Grant privileges", revoke: "Revoke privileges", roles: "Change roles",
};
const SINCE: [string, string][] = [
  ["", "Everything kept"],
  ["1", "Last 24 hours"],
  ["7", "Last 7 days"],
  ["30", "Last 30 days"],
  ["90", "Last 90 days"],
];

/** Labels describe stored fields without changing their recorded values. */
const FIELD_LABELS: Record<string, string> = {
  connection_id: "Connection ID", mode: "Kill mode", action: "Action", kind: "Object kind", category: "Category",
  user: "Account", host: "Host", connection_type: "Connection type", read_only: "Read-only",
  profile_id: "Connection ID", profile_name: "Connection name", server_id: "Server ID", server_uuid: "Server UUID",
  ps_thread_id: "Performance Schema thread ID", trx_started: "Transaction started", trx_checked: "Transaction checked",
  change_set_id: "Change set ID", last_sql_errno: "Applier error number", last_sql_error: "Applier error message",
  last_sql_error_timestamp: "Applier error time", gtid: "GTID", uuid: "UUID", os_user: "OS user",
};
const CHECK_LABELS: Record<string, string> = {
  user: "Account matched", host: "Host matched", "not this session": "Not this session",
  connection: "Connection matched", "server identity": "Server identity matched", "server thread": "Server thread matched",
  transaction: "Transaction matched", connection_key: "Connection matched", server_identity: "Server identity matched",
  request: "Request matched", tool: "Tool checked", preflight: "Preflight passed",
  policy: "Write policy checked", server: "Server matched", actor: "Acting account matched",
  account: "Account matched", guards: "Confirmation guards checked", statement: "Statement matched",
  engine: "Engine matched", "server version": "Server version matched", "global value": "Global value matched",
  "session value": "Session value matched", "source metadata": "Source metadata matched",
  "persisted value": "Persisted value matched", "persisted state": "Persisted state matched",
  "the skip method": "Skip method matched", "the error number": "Error number matched",
  "the error message": "Error message matched", "the error time": "Error time matched",
  "the failing GTID": "Failing GTID matched", "the relay log file": "Relay log file matched",
  "the relay log position": "Relay log position matched", "the applied source log file": "Applied source log file matched",
  "the applied source position": "Applied source position matched", "the source host": "Source host matched",
  "the source port": "Source port matched", "the source UUID": "Source UUID matched", "the source server ID": "Source server ID matched",
};
function readableLabel(key: string) {
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}
function mappedLabel(labels: Record<string, string>, key: string) {
  return Object.prototype.hasOwnProperty.call(labels, key) ? labels[key] : readableLabel(key);
}
const OUTCOME_NOTE = "The statement may have reached the server; its outcome is unknown.";
/** Older records append this note directly to an unpunctuated server error. */
function messageText(r: AuditRecord) {
  const message = r.message || "";
  if (r.status !== "unknown" || !message.endsWith(OUTCOME_NOTE)) return message;
  const error = message.slice(0, -OUTCOME_NOTE.length).trimEnd();
  return error ? `${error}${/[.!?]$/.test(error) ? "" : "."} ${OUTCOME_NOTE}` : message;
}

export function operationLabel(op: string) {
  const account = op.startsWith("accounts.") ? op.slice("accounts.".length) : "";
  if (account) return ACCOUNT_OPERATIONS[account] || `Users and roles: ${account.replace(/_/g, " ")}`;
  return OPERATIONS[op] || op.replace(/[._]/g, " ").replace(/^./, (c) => c.toUpperCase());
}
/** The connection a record names: its saved name, else user@host:port. */
function connectionOf(r: AuditRecord) {
  const c = r.connection;
  const target = c.host ? `${c.user ? `${c.user}@` : ""}${c.host}${c.port ? `:${c.port}` : ""}` : "";
  return c.profile_name || target || "Unknown connection";
}
/** The value the server's `server` filter matches (saved name, host, or host:port). */
function filterValue(r: AuditRecord) {
  const c = r.connection;
  return c.profile_name || (c.host ? (c.port ? `${c.host}:${c.port}` : c.host) : "");
}
function typeOf(r: AuditRecord) {
  const type = r.connection.type === "development" ? "Development" : r.connection.type === "production" ? "Production" : "";
  return [type, r.connection.read_only ? "Read-only" : ""].filter(Boolean).join(" · ") || "—";
}
function when(iso: string | null) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
// A statement without its leading myflames tag comment, for the one-line
// cell (the inspector and the tooltip keep it as stored).
function untagged(statement: string) {
  return statement.replace(/^\/\*myflames:[\w-]+\*\/\s*/, "");
}
function errorText(r: AuditRecord) {
  if (r.status !== "error" && r.status !== "unknown") return "";
  return [r.error_code ? String(r.error_code) : "", messageText(r)].filter(Boolean).join(": ");
}

function Value({ value }: { value: unknown }): ReactNode {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return <code className="audit-json">{JSON.stringify(value)}</code>;
  return String(value);
}
/** An object (target, policy, after state) as label / value rows. */
function ObjectFields({ label, value, checks = false }: { label: string; value: unknown; checks?: boolean }) {
  if (value === null || value === undefined) return <p className="hint">None recorded.</p>;
  if (typeof value !== "object" || Array.isArray(value))
    return Array.isArray(value) ? (
      <ul className="audit-list">
        {value.map((v, i) => (
          <li key={i}>
            <Value value={checks && typeof v === "string" ? mappedLabel(CHECK_LABELS, v) : v} />
          </li>
        ))}
      </ul>
    ) : (
      <p>
        <Value value={value} />
      </p>
    );
  return <Fields label={label} rows={Object.entries(value as Record<string, unknown>).map(([k, v]) => [mappedLabel(FIELD_LABELS, k), <Value key={k} value={v} />])} />;
}

/** *focusId*: a record to select and open (a reviewed change's result links here). */
export function AuditLog({ profiles, focusId = "" }: { profiles: Profile[]; focusId?: string }) {
  const [report, setReport] = useState<AuditReport | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [server, setServer] = useState("");
  const [since, setSince] = useState("");
  const [status, setStatus] = useState("");
  const [selected, setSelected] = useState(focusId);
  const [exporting, setExporting] = useState("");
  const [exported, setExported] = useState("");
  const [exportError, setExportError] = useState("");
  // Connections the filter offers: saved ones and every one seen in a list.
  const [seen, setSeen] = useState<string[]>([]);
  const request = useRef<AbortController | null>(null);

  // The filters every read sends: the server applies them before the page.
  const filters = useMemo(
    () => ({ ...(server ? { server } : {}), ...(since ? { since_days: Number(since) } : {}), ...(status ? { status } : {}) }),
    [server, since, status],
  );
  const remember = (records: AuditRecord[]) =>
    setSeen((names) => {
      const more = records.map(filterValue).filter((n) => n && !names.includes(n));
      return more.length ? [...names, ...new Set(more)] : names;
    });
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setLoadingMore(false);
    setError("");
    try {
      const next = await api<AuditReport>("audit/list", { limit: PAGE, ...filters }, controller.signal);
      if (controller.signal.aborted) return;
      setReport(next);
      remember(next.records);
    } catch (e) {
      if (!controller.signal.aborted) setError((e as Error).message);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [filters]);
  /** The next page after the oldest record listed, with the same filters. */
  async function loadOlder() {
    const current = report;
    const last = current?.records[current.records.length - 1];
    if (!current || !last) return;
    const controller = new AbortController();
    request.current?.abort();
    request.current = controller;
    setLoadingMore(true);
    setError("");
    try {
      const next = await api<AuditReport>("audit/list", { limit: PAGE, before: last.id, ...filters }, controller.signal);
      if (controller.signal.aborted) return;
      setReport({ ...next, records: [...current.records, ...next.records.filter((r) => !current.records.some((x) => x.id === r.id))] });
      remember(next.records);
    } catch (e) {
      if (!controller.signal.aborted) setError((e as Error).message);
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  }
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => () => request.current?.abort(), []);

  // The inspector: a pane when wide, a bottom sheet when narrow.
  const place = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const element = place.current;
    if (!element) return;
    const measure = () => setWidth(element.clientWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const [inspectorOpen, setInspectorOpen] = useState(!!focusId);
  const [inspectorWidth, setInspectorWidth] = useState(storedInspectorWidth);
  const sheet = width > 0 && width < SHEET_WIDTH;
  const inspector: InspectorShared = useMemo(
    () => ({
      open: inspectorOpen,
      setOpen: setInspectorOpen,
      width: inspectorWidth,
      setWidth: (w: number) => {
        setInspectorWidth(w);
        storeInspectorWidth(w);
      },
      sheet,
    }),
    [inspectorOpen, inspectorWidth, sheet],
  );
  // Opened from the Settings menu: focus lands on the view's title.
  useEffect(() => {
    document.getElementById("audit-title")?.focus();
  }, []);

  const records = report?.records || [];
  const record = records.find((r) => r.id === selected) || null;
  const selection: Selection = {
    key: record ? record.id : "",
    select: (key, open) => {
      setSelected(key);
      if (open) setInspectorOpen(true);
    },
  };
  const names = [...new Set([...profiles.map((p) => p.name).filter(Boolean), ...seen])].sort((a, b) => a.localeCompare(b));
  const filtered = !!(server || since || status);

  async function exportAs(format: "csv" | "json") {
    setExporting(format);
    setExported("");
    setExportError("");
    try {
      const file = await api<AuditExport>("audit/export", { format, ...filters });
      download(file.content, file.filename, file.mime);
      setExported(file.filename);
    } catch (e) {
      setExportError((e as Error).message);
    } finally {
      setExporting("");
    }
  }

  const columns: Column<AuditRecord>[] = [
    {
      key: "time",
      label: "Time",
      sort: (r) => r.attempted_at,
      render: (r) => <span title={`${r.attempted_at} (UTC)`}>{when(r.attempted_at)}</span>,
    },
    {
      key: "status",
      label: "Status",
      // "Outcome unknown" never clips, whatever the frame.
      minWidth: 152,
      sort: (r) => STATUS_ORDER.indexOf(r.status),
      render: (r) => <StatusPill tone={AUDIT_STATUS[r.status]?.tone || "unknown"} label={AUDIT_STATUS[r.status]?.word || r.status} />,
    },
    { key: "connection", label: "Connection", sort: connectionOf, render: (r) => <span className="cell-truncate">{connectionOf(r)}</span> },
    { key: "type", label: "Type", sort: typeOf, render: typeOf },
    { key: "operation", label: "Operation", sort: (r) => operationLabel(r.operation), render: (r) => operationLabel(r.operation) },
    {
      key: "statements",
      label: "Statements",
      flex: true,
      render: (r) => (
        <span className="cell-name" title={r.statements.join("\n")}>
          <code className="cell-truncate">{r.statements[0] ? untagged(r.statements[0]) : "—"}</code>
          {r.statements.length > 1 && <span className="cell-muted">+{r.statements.length - 1}</span>}
        </span>
      ),
    },
    { key: "error", label: "Error", sort: errorText, render: (r) => <span className="cell-truncate">{errorText(r) || "—"}</span> },
    { key: "duration", label: "Duration", num: true, sort: (r) => r.duration_ms, render: (r) => (r.duration_ms === null ? "—" : time(r.duration_ms)) },
  ];

  let content: InspectorContent | null = null;
  if (record) {
    const meta = AUDIT_STATUS[record.status];
    const c = record.connection;
    content = {
      key: record.id,
      title: operationLabel(record.operation),
      meta: (
        <>
          <StatusPill tone={meta?.tone || "unknown"} label={meta?.word || record.status} />
          <span>{when(record.attempted_at)}</span>
        </>
      ),
      body: (
        <>
          <InspectorSection title="What happened">
            <p>{meta?.meaning || record.status}</p>
            {record.message && <p>{messageText(record)}</p>}
          </InspectorSection>
          <InspectorSection title={record.statements.length === 1 ? "Statement" : "Statements"}>
            {record.statements.length ? (
              record.statements.map((s, i) => (
                <pre key={i} className="inspector-sql">
                  <code>{s}</code>
                </pre>
              ))
            ) : (
              <p className="hint">None recorded.</p>
            )}
            <p className="hint">As myflames stored it, with secrets redacted.</p>
          </InspectorSection>
          <InspectorSection title="Connection">
            <Fields
              label="Connection"
              rows={[
                ["Name", c.profile_name || "Not saved"],
                ["Server", c.host ? `${c.user ? `${c.user}@` : ""}${c.host}${c.port ? `:${c.port}` : ""}` : "—"],
                ["Type", c.type === "development" ? "Development" : c.type === "production" ? "Production" : "—"],
                ["Read-only", c.read_only === null ? "—" : c.read_only ? "Yes" : "No"],
                ["Engine", c.engine ? `${c.engine === "mariadb" ? "MariaDB" : "MySQL"} ${c.server_version || ""}`.trim() : "—"],
              ]}
            />
          </InspectorSection>
          <InspectorSection title="Result">
            <Fields
              label="Result"
              rows={[
                ["Outcome", record.outcome || "—"],
                record.error_code !== null && ["Error code", String(record.error_code)],
                record.rows_affected !== null && ["Rows affected", String(record.rows_affected)],
                ["Duration", record.duration_ms === null ? "—" : time(record.duration_ms)],
              ]}
            />
            {record.after !== null && record.after !== undefined && (
              <>
                <h5 className="audit-subhead">State after</h5>
                <ObjectFields label="State after" value={record.after} />
              </>
            )}
          </InspectorSection>
          <InspectorSection title="Confirmed target">
            <ObjectFields label="Confirmed target" value={record.target} />
          </InspectorSection>
          {record.verified !== null && record.verified !== undefined && (
            <InspectorSection title="Checked before sending">
              <ObjectFields label="Checked before sending" value={record.verified} checks />
            </InspectorSection>
          )}
          {record.policy !== null && record.policy !== undefined && (
            <InspectorSection title="Write policy">
              <ObjectFields label="Write policy" value={record.policy} />
            </InspectorSection>
          )}
          <InspectorSection title="Record">
            <Fields
              label="Record"
              rows={[
                ["Attempted", `${record.attempted_at} (UTC)`],
                ["Finished", record.finished_at ? `${record.finished_at} (UTC)` : "Not recorded"],
                ["From", record.surface === "cli" ? "Command line" : "Workspace"],
                ["OS user", record.os_user || "—"],
                ["Record ID", <code key="id">{record.id}</code>],
              ]}
            />
          </InspectorSection>
        </>
      ),
    };
  }

  const retention = report?.retention_days ?? 90;
  const compactControls = width > 0 && width < 960;
  const filterCount = [server, since, status].filter(Boolean).length;
  const filterControls = (
    <>
      <ToolbarSelect
        label="Connection"
        className="audit-filter-connection"
        value={server}
        onChange={setServer}
        options={[["", "All connections"], ...names.map((n): [string, string] => [n, n])]}
      />
      <ToolbarSelect
        label="Status"
        className="audit-filter-status"
        value={status}
        onChange={setStatus}
        options={[["", "All statuses"], ...STATUS_ORDER.map((s): [string, string] => [s, AUDIT_STATUS[s].word])]}
      />
      <ToolbarSelect label="Since" className="audit-filter-since" value={since} onChange={setSince} options={SINCE} />
    </>
  );
  return (
    <div className="audit-place" ref={place}>
      <InspectorContext.Provider value={inspector}>
        <ViewLayout
          id="audit"
          title="Audit log"
          hideTitle
          className={`audit-view${compactControls ? " is-compact-controls" : ""}`}
          info={
            <>
              <p>
                Every change myflames sends to a server is recorded here before it is sent, with how it ended. Cancelled
                confirmations and refused writes are recorded too. Kept on this computer only, never a password.
              </p>
              <ul className="audit-status-list">
                {STATUS_ORDER.map((s) => (
                  <li key={s}>
                    <StatusPill tone={AUDIT_STATUS[s].tone} label={AUDIT_STATUS[s].word} /> {AUDIT_STATUS[s].meaning}
                  </li>
                ))}
              </ul>
            </>
          }
          controls={
            <>
              {compactControls ? (
                <Popover
                  label="Filter audit records"
                  className="audit-filters"
                  buttonClassName="button"
                  trigger={<>Filter{filterCount ? ` (${filterCount})` : ""}</>}
                >
                  <h3 className="popover-title">Filter audit records</h3>
                  <div className="audit-filter-fields">{filterControls}</div>
                </Popover>
              ) : <div className="audit-inline-filters" role="group" aria-label="Filter audit records">{filterControls}</div>}
              <div className="audit-actions" role="group" aria-label="Audit actions">
                {report && <RetentionControl days={retention} onSaved={() => void load()} />}
                <Popover
                  label="Export the audit log"
                  className="audit-export"
                  buttonClassName="button"
                  buttonTitle={records.length ? "Export the records that match these filters" : "Nothing to export yet"}
                  disabled={!records.length}
                  trigger={
                    <>
                      <Icon name="export" size={15} />
                      Export
                    </>
                  }
                >
                  <h3 className="popover-title">Export as</h3>
                  <div className="audit-export-formats" role="group" aria-label="Export formats">
                    {(["csv", "json"] as const).map((format) => (
                      <button
                        key={format}
                        type="button"
                        className="button"
                        data-format={format}
                        disabled={!!exporting}
                        onClick={() => void exportAs(format)}
                      >
                        {exporting === format ? <span className="spinner" aria-hidden="true" /> : null}
                        {format.toUpperCase()}
                      </button>
                    ))}
                  </div>
                  <p className="hint">Every record that matches the filters, not only the ones listed.</p>
                  {exported && (
                    <p className="hint" role="status">
                      Saved {exported}
                    </p>
                  )}
                  {exportError && (
                    <p className="error" role="alert">
                      {exportError}
                    </p>
                  )}
                </Popover>
              </div>
            </>
          }
          onRefresh={() => void load()}
          loading={loading}
          inspector={content}
          inspectorEmpty={<p>Select a record to see the statements, the connection, and how it ended.</p>}
          status={<ViewStatus loading={loading && !report ? "Reading the audit log…" : undefined} error={error} />}
        >
          {report && !records.length && !filtered && !loading ? (
            <EmptyState icon="audit" title="No changes recorded yet">
              Every change myflames sends, refuses or you decline is listed here. Kept on this computer for{" "}
              {retention} {retention === 1 ? "day" : "days"}.
            </EmptyState>
          ) : report ? (
            <>
              <SummaryLine>
                {`${records.length.toLocaleString()} ${records.length === 1 ? "record" : "records"}`}
                {report.has_more ? ", more are older" : ""} · newest first · Kept {retention} {retention === 1 ? "day" : "days"}
              </SummaryLine>
              <DataTable
                label="Audit records"
                columns={columns}
                rows={records}
                rowKey={(r) => r.id}
                selection={selection}
                selectionHighlight="active"
                defaultSort={{ key: "time", dir: "desc" }}
                rowAttrs={(r) => ({ "data-status": r.status })}
                empty={<p>No records match these filters.</p>}
              />
              {report.has_more && (
                <div className="audit-more">
                  <button type="button" className="button" disabled={loadingMore || loading} onClick={() => void loadOlder()}>
                    {loadingMore ? <span className="spinner" aria-hidden="true" /> : null}
                    Load {PAGE} older
                  </button>
                </div>
              )}
            </>
          ) : null}
        </ViewLayout>
      </InspectorContext.Provider>
    </div>
  );
}

/** Retention stays reachable even when no records are listed. */
function RetentionControl({ days, onSaved }: { days: number; onSaved: () => void }) {
  const [value, setValue] = useState(String(days));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => setValue(String(days)), [days]);
  const next = Number(value);
  const valid = Number.isInteger(next) && next >= 1 && next <= MAX_RETENTION;
  async function save() {
    if (!valid) return;
    setSaving(true);
    setError("");
    try {
      await api<{ retention_days: number }>("audit/settings", { retention_days: next });
      closePopovers();
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }
  return (
    <Popover
      label="Keep audit records"
      className="audit-retention"
      buttonClassName="button"
      buttonTitle="How long records stay on this computer"
      trigger="Retention"
    >
      <h3 className="popover-title">Keep audit records</h3>
      <p className="hint audit-retention-summary">Kept {days} {days === 1 ? "day" : "days"}.</p>
      <form
        className="audit-retention-form"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <label className="toolbar-field">
          <span>For</span>
          <NumberField
            label="Days to keep audit records"
            value={value}
            unit="days"
            min={1}
            max={MAX_RETENTION}
            describedBy="audit-retention-hint"
            onChange={setValue}
          />
        </label>
        <button type="submit" className="button primary" disabled={!valid || saving || next === days}>
          {saving ? "Saving…" : "Save"}
        </button>
      </form>
      <p className="hint" id="audit-retention-hint">
        1 to {MAX_RETENTION.toLocaleString()} days. Shorter deletes older records now.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </Popover>
  );
}
