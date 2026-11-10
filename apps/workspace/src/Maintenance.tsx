// Server > Setup > Maintenance: detect tools, check an upgrade, and dump,
// restore or clone. The upgrade check and every preflight only read; each
// start needs an explicit confirmation that shows the exact commands (and the
// typed target for restore and clone). Job results are memory only and bound
// to the connection they ran on; the jobs themselves live in the myflames
// process, which lists them again after a reload or a lost Start reply.
import { useEffect, useRef, useState, type ComponentProps, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { deletedConnectionLabel } from "./savedConnections";
import { ClientMissingNotice } from "./ClientMissing";
import { ClientToolsDialog, UsedForServer } from "./ClientTools";
import { reasonSentence, toolUseLine } from "./clientToolsText";
import { privilegeResult, targetPrivilege, usePrivilege, usePrivileges } from "./privileges";
import { download, recordDeclined, type Connection, type Profile } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { ConnectionTypeBand, TYPE_LABEL, useWritePolicy } from "./Connections";
import { bytes, SchemaPicker, useSchemaCatalog } from "./PerfShared";
import {
  ActionButton, ActionStatus, useAction, useSince, writeFailure, Checkbox, ConfirmSheet, DataTable, Fields, FormGrid, FormRow, InspectorSection,
  NumberField, PrerequisiteGate, Segmented, Select, StatusPill, SummaryLine,
  ToolbarButton, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type Selection, type Tone,
} from "./kit";
import { Commands, DumpFolderField, dumpFolderStem, engineName, plural, suggestDumpFolder } from "./OpsShared";
import { joinPath, localReveal, parentPath } from "./filesApi";
import { MAC, WINDOWS } from "./platform";
import { durationText } from "./duration";
import {
  cancelLifecycleJob, checkUpgrade, documentationUrl, jobActive, listLifecycleJobs,
  prepareLifecycle, readDumpInfo, readLifecycleJob, readLifecycleTools, readUpgradeTargets, startLifecycle, upgradeLinkUrl, useOpsScope,
  type DumpInfo, type LifecycleJob, type LifecyclePlan, type LifecycleTools, type UpgradeTarget, type UpgradeTargets,
  type MissingPrivileges, type PrepareRequest, type Tool, type UpgradeFinding,
  type UpgradeReport, type Verification,
} from "./opsApi";
import { isPrivilegeDenial } from "./privileges";
import { VisiblePrivilegeGate, VisiblePrivilegeConfirmLabel } from "./Locks";
import "./ops.css";
import { DumpOptions, optionShown, optionsSummary } from "./DumpOptions";
import { TablePicker, useSchemaTables } from "./DumpTables";
import type { DumpOptions as DumpOptionValues } from "./opsApi";
import { FolderField } from "./FolderField";
import { useOpenRead, useViewOnScreen } from "./viewOnScreen";
import { toolDownloadUrl } from "./opsApi";

export type MaintenanceSection = "tools" | "upgrade" | "dump" | "restore" | "clone";
export type MaintenanceMemory = {
  tools: Bound<LifecycleTools> | null;
  upgrade: Bound<UpgradeReport> | null;
  /** Upgrade targets from the vendor's releases and the install method (upgrade-targets-1.0). */
  targets: Bound<UpgradeTargets> | null;
  jobs: Bound<LifecycleJob>[];
};
export const emptyMaintenance: MaintenanceMemory = { tools: null, upgrade: null, targets: null, jobs: [] };
const SECTION_LABEL: Record<MaintenanceSection, string> = { tools: "Tools", upgrade: "Upgrade check", dump: "Dump", restore: "Restore", clone: "Clone" };
/** A preflight older than this is prepared again before anything starts. */
export const PREFLIGHT_TTL_MS = 60_000;
const JOB_POLL_MS = 1500;
/** Finished jobs kept per workspace (the server keeps 20 for an hour). */
const KEEP_FINISHED = 20;
// A convenience for this viewer session only; no directories are created here.
let lastDumpParent = "";
const checkTone = (check: Verification): Tone => check.status === "passed" ? "ok" : check.status === "failed" ? "problem" : "unknown";
const checkWord = (check: Verification) => check.status === "passed" ? "Passed" : check.status === "failed" ? "Failed" : "Not checked";
const validInteger = (value: number, min: number, max: number) => Number.isInteger(value) && value >= min && value <= max;
const VERSION = /^\d+\.\d+\.\d+$/;
/** Restore's "New database…" choice (no database name contains NUL). */
const NEW_DATABASE = "\u0000new";
/** The donor TLS modes Clone offers: it always requires TLS. */
const DONOR_TLS = ["REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"];
type DonorFill = { id: string; name: string; host: string; port: number; user: string; ssl_mode: string; ssl_ca: string; raised: boolean };

/** One upgrade finding's result: what the check found, or that it passed,
 * could not run, or was not checked (never a severity for a check that passed). */
export function findingResult(f: UpgradeFinding): [Tone, string, number] {
  const status = (f.status || "").toLowerCase();
  if (status === "not_checked") return ["unknown", "Not checked", 0];
  if (status === "error" || status === "configuration_error") return ["unknown", "Check failed", 1];
  if (status === "passed" || (status === "ok" && !f.affected_objects.length)) return ["ok", "Passed", 2];
  return f.severity === "error" ? ["problem", "Error", 5] : f.severity === "warning" ? ["watch", "Warning", 4] : ["info", "Notice", 3];
}
/** A job's state in words: Running, Completed, Verification failed… */
export function jobWord(job: LifecycleJob): [Tone, string] {
  if (job.state === "queued") return ["info", "Waiting"];
  if (job.state === "running") return ["info", "Running"];
  if (job.state === "cancelled") return ["unknown", "Cancelled"];
  if (job.state === "refused") return ["problem", "Refused"];
  if (job.state === "failed") return ["problem", "Failed"];
  const status = job.result?.status || "";
  if (status === "completed") return ["ok", "Completed"];
  if (status === "verification_failed") return ["problem", "Verification failed"];
  if (status === "needs_restart") return ["watch", "Needs a manual start"];
  if (status === "needs_verification") return ["watch", "Needs verification"];
  return ["unknown", status ? status.replace(/_/g, " ") : "Done"];
}
/** Merge the jobs the server lists for *key* into memory: known ids take the
 * listed state and this connection (a policy change keeps the endpoint), new
 * ones are added, and finished ones beyond KEEP_FINISHED drop out. */
export function adoptJobs(memory: MaintenanceMemory, key: string, label: string, jobs: LifecycleJob[]): MaintenanceMemory {
  const listed = jobs.filter((job) => job.id);
  const ids = new Set(listed.map((job) => job.id));
  const kept = memory.jobs.filter((b) => !ids.has(b.report.id));
  const merged = [...kept, ...listed.map((job) => ({ key, label, report: job }))];
  const finished = merged.filter((b) => !jobActive(b.report));
  const drop = new Set(finished.slice(0, Math.max(0, finished.length - KEEP_FINISHED)));
  return { ...memory, jobs: merged.filter((b) => !drop.has(b)) };
}

type Prepared = Bound<LifecyclePlan> & { donor?: Connection; at: number; inputKey: string };
/** The last part of a folder path, for a title (the full path is shown below it). */
const folderName = (target: string) => target.replace(/[/\\]+$/, "").split(/[/\\]/).pop() || target;
function dumpContents(plan: LifecyclePlan) {
  const schemas = plan.request.schemas as string[] | undefined;
  const tables = plan.request.tables as string[] | undefined;
  const contents = !schemas?.length ? plan.tool.name === "mysqlsh" ? "Whole instance (no user accounts)"
    : "Whole instance, including mysql system tables and account hashes"
    : tables?.length ? `${schemas.join(", ")}: ${tables.join(", ")} (${plural(tables.length, "table")})`
      : `${schemas.join(", ")} (all tables)`;
  return contents + ((plan.request.options as DumpOptionValues | undefined)?.structure_only ? ", structure only (no rows)" : "");
}
function planMessages(plan: LifecyclePlan) {
  // Legacy plans have one warnings list. Recognize only the two routine
  // caveats; unknown warnings always stay prominent. Never mutate the plan.
  const routine = (line: string) => plan.operation === "dump" &&
    (line.startsWith("A consistent snapshot protects InnoDB data only.") || line.startsWith("The reviewed tool options determine"));
  const warnings = plan.warnings.filter(line => !routine(line));
  const versionWarning = warnings.findIndex(line => line.startsWith("MySQL Shell ") && line.includes("older than the server"));
  if (versionWarning > 0) warnings.unshift(...warnings.splice(versionWarning, 1));
  return { warnings, notes: plan.warnings.filter(routine) };
}
/** The reviewed tool, as the resolver describes it everywhere (clientToolsText). */
function planToolLine(plan: LifecyclePlan) {
  return toolUseLine(plan.tool, { engine: plan.server.engine, version: plan.server.version, product: plan.server.product });
}

function FormField({ label, value, onChange, disabled, type = "text", hint, wide, placeholder }: {
  label: string; value: string; onChange: (value: string) => void;
  disabled?: boolean; type?: "text" | "password"; hint?: string; wide?: boolean; placeholder?: string;
}) {
  return <label className={`ops-field${wide ? " ops-wide" : ""}`}><span>{label}</span><input type={type} value={value} disabled={disabled} placeholder={placeholder} spellCheck={false} autoComplete={type === "password" ? "new-password" : "off"} onChange={(e) => onChange(e.target.value)} />{hint && <span className="hint">{hint}</span>}</label>;
}
const checkLabel = (name: string) => name.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
function Checks({ checks, selection, fix }: { checks: Verification[]; selection?: Selection; fix: (check: Verification) => ReactNode }) {
  const rows = checks.map((check, index) => ({ ...check, row: String(index) })).filter(c => c.status === "failed");
  const other = checks.filter(c => c.status !== "failed");
  const columns: Column<(typeof rows)[number]>[] = [
    { key: "status", label: "Result", sort: (r) => (r.status === "failed" ? 0 : r.status === "not_checked" ? 1 : 2), render: (r) => <StatusPill tone={checkTone(r)} label={checkWord(r)} /> },
    { key: "check", label: "Check", primary: true, sort: (r) => r.check, render: (r) => checkLabel(r.check) },
    { key: "reason", label: "Reason", flex: true, render: (r) => <span className="ops-check-reason">{r.reason}</span> },
    { key: "fix", label: "Next step", render: (r) => fix(r) },
  ];
  return <><DataTable id="maintenance.checks" label="Preflight checks" rows={rows} columns={columns} rowKey={(r) => `check:${r.row}`} selection={selection} defaultSort={{ key: "status", dir: "asc" }} />
    {!!other.length && <details><summary>{plural(checks.filter(c => c.status === "passed").length, "check")} passed{checks.some(c => c.status === "not_checked") ? `, ${checks.filter(c => c.status === "not_checked").length} not checked` : ""}</summary>
      <ul className="ops-verification">{other.map((c, i) => <li key={i}><StatusPill tone={checkTone(c)} label={checkWord(c)} /><strong>{checkLabel(c.check)}</strong><span>{c.reason}</span></li>)}</ul>
    </details>}
  </>;
}

/** Keep the existing privilege/policy gate, including its reason, while
 * retaining focus on the read action that is already running. */
function ReadAction({ busy, busyLabel, children, disabled, blocked, ...props }: ComponentProps<typeof VisiblePrivilegeGate> & { busy: boolean; busyLabel: string; children: string }) {
  return <VisiblePrivilegeGate {...props} className={`${props.className || ""} action-button${busy ? " is-pending" : ""}`}
    disabled={!busy && disabled} blocked={busy ? busyLabel : blocked} aria-busy={busy || undefined}>
    <span className={`action-button-label${busy ? " is-busy" : ""}`} data-reserve={busy ? children : busyLabel}>
      <span>{busy && <span className="spinner" aria-hidden="true" />}{busy ? busyLabel : children}</span>
    </span>
  </VisiblePrivilegeGate>;
}

/** Tool percentages take precedence. Table sizes estimate output, never completion. */
export function lifecycleProgress(job: LifecycleJob) {
  const written = job.output && Number.isFinite(job.output.written_bytes) && job.output.written_bytes >= 0 ? job.output.written_bytes : null;
  const estimate = job.output?.estimated_bytes;
  const estimated = typeof estimate === "number" && Number.isFinite(estimate) && estimate > 0 ? estimate : null;
  const reported = typeof job.progress === "number" && Number.isFinite(job.progress) ? Math.max(0, Math.min(100, job.progress)) : null;
  const value = reported ?? (written !== null && estimated !== null && written < estimated ? written / estimated * 100 : null);
  const approximate = reported === null && value !== null;
  // Rounding an estimate up to 100% would imply completion before verification.
  const percent = value === null ? "" : `${approximate ? "≈" : ""}${approximate ? Math.min(99, Math.round(value)) : Math.round(value)}%`;
  const output = written === null ? "" : estimated === null ? `${bytes(written)} written`
    : written > estimated ? `${bytes(written)} written, more than the ${bytes(estimated)} estimate`
      : `${bytes(written)} written of about ${bytes(estimated)}`;
  return { value, percent, approximate, written, output };
}
export type OutputSample = { written: number; at: number };
/** A rate needs two snapshots of this job, with increasing sample times. */
export function lifecycleRate(previous: OutputSample | null, next: OutputSample): number | null {
  if (!previous || next.at <= previous.at || next.written < previous.written) return null;
  return (next.written - previous.written) / ((next.at - previous.at) / 1000);
}
/** Clock form for the facts line; durationText supplies its accessible wording. */
export function lifecycleElapsed(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "";
  const seconds = Math.max(0, Math.floor(value));
  const tail = String(seconds % 60).padStart(2, "0");
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes}:${tail}` : `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${tail}`;
}
/** Open the folder of a completed dump by itself only for one this view
 * started, once. Not on Windows: File Explorer's /select, argument splits a
 * path at commas (security open lead 17), so there it opens only on a click. */
export function autoRevealDump(job: { id: string | null; directory?: string }, started: boolean, revealed: Set<string>, windows: boolean) {
  return !!job.directory && !!job.id && started && !revealed.has(job.id) && !windows;
}
const ESTIMATE_HINT = "Estimated from the server's table sizes; the SQL dump can be smaller or larger.";
const OPEN_FOLDER_LABEL = MAC ? "Open in Finder" : WINDOWS ? "Open in File Explorer" : "Open folder";
const OPEN_FOLDER_PENDING = MAC ? "Opening in Finder…" : WINDOWS ? "Opening in File Explorer…" : "Opening folder…";
const OPEN_FOLDER_DONE = MAC ? "Opened the dump folder in Finder." : WINDOWS ? "Opened the dump folder in File Explorer." : "Opened the dump folder.";
function showJob(id: string) {
  // Match the attribute as data, without interpolating a job id into a selector.
  const card = Array.from(document.querySelectorAll<HTMLElement>("[data-job]")).find(e => e.dataset.job === id);
  if (!card) return;
  card.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  card.focus({ preventScroll: true });
}

/** Job status is an outcome, not proof of verification: a tool that exited 0
 * can still leave checks not checked. */
function JobProgress({ bound, currentKey, cancelling, onCancel, shown, started, revealed }: {
  bound: Bound<LifecycleJob>; currentKey: string; cancelling: string;
  onCancel: (bound: Bound<LifecycleJob>) => void;
  shown: boolean; started: boolean; revealed: Set<string>;
}) {
  const job = bound.report;
  const other = bound.key !== currentKey;
  const active = jobActive(job);
  const [tone, word] = jobWord(job);
  const cancelSince = useSince(cancelling === job.id);
  const last = job.events.length ? job.events[job.events.length - 1].text : "";
  const progress = lifecycleProgress(job);
  const sample = useRef<OutputSample | null>(null);
  const [rate, setRate] = useState<number | null>(null);
  useEffect(() => {
    if (progress.written === null) { sample.current = null; setRate(null); return; }
    // The job's own clock, so a late poll does not skew the rate.
    const next = { written: progress.written, at: job.elapsed_s != null ? job.elapsed_s * 1000 : performance.now() };
    if (sample.current && next.at === sample.current.at) return;
    setRate(lifecycleRate(sample.current, next));
    sample.current = next;
  }, [job]);
  const reveal = useAction(job.id || "");
  const completed = job.state === "done" && job.result?.status === "completed";
  const directory = job.operation === "dump" && completed ? job.result?.directory : undefined;
  const openFolder = () => reveal.run(() => localReveal(joinPath(directory!, "myflames-manifest.json")), {
    pending: OPEN_FOLDER_PENDING, done: OPEN_FOLDER_DONE,
  });
  useEffect(() => {
    if (!job.id || !autoRevealDump({ id: job.id, directory }, started, revealed, WINDOWS)) return;
    // Claim before sending, including failures: polling never retries a reveal.
    revealed.add(job.id);
    void openFolder();
  }, [directory, job.id, started, revealed]);
  const elapsed = lifecycleElapsed(job.elapsed_s);
  // A finished dump wrote what it wrote; the estimate only mattered while it ran.
  const output = active ? progress.output : progress.written !== null ? `${bytes(progress.written)} written` : "";
  const facts = [active ? job.phase : "", output, active && rate !== null ? `${bytes(rate)}/s` : ""].filter(Boolean);
  // Keep following jobs from this mounted view across Maintenance sections.
  if (!shown) return null;
  return <section className={`ops-job${other ? " is-other-connection" : ""}`} aria-label={`${SECTION_LABEL[job.operation]} on ${bound.label}`} data-job={job.id || undefined} data-state={job.state} data-other-connection={other || undefined} tabIndex={-1}>
    <div className="ops-job-head"><strong>{SECTION_LABEL[job.operation]} · {bound.label}</strong><StatusPill tone={tone} label={word} />{active && <ActionButton busy={cancelling === job.id} busyLabel="Cancelling…" disabled={other || !job.id} title={other ? "Switch back to this connection to cancel it." : undefined} onClick={() => onCancel(bound)}>Cancel</ActionButton>}</div>
    <ActionStatus state={{ phase: cancelling === job.id ? "pending" : "idle", message: "Waiting for the cancellation reply. The job may still be running.", startedAt: cancelSince }} />
    {other && <p className="hint">From {bound.label}. Switch back to that connection to manage this job.</p>}
    {active && <div className="ops-progress" role="status" title={progress.approximate ? ESTIMATE_HINT : undefined}><progress aria-label={`${SECTION_LABEL[job.operation]} progress`} max={100} value={progress.value ?? undefined} />{progress.percent && <span>{progress.percent}</span>}</div>}
    {(facts.length > 0 || elapsed) && <p className="ops-job-facts">{facts.map((fact, i) => <span key={i}>{fact}</span>)}{elapsed && <span>{completed ? "Completed in" : "Elapsed"} <abbr title={durationText(job.elapsed_s)}>{elapsed}</abbr></span>}</p>}
    {directory && <div className="ops-job-folder"><ActionButton busy={reveal.pending} busyLabel={OPEN_FOLDER_PENDING} onClick={() => { void openFolder(); }}>{OPEN_FOLDER_LABEL}</ActionButton></div>}
    <ActionStatus state={reveal} />
    {last && last !== job.phase && <p className="ops-log-line" title={last}>{last}</p>}
    {job.policy?.text && <p>{job.policy.text}</p>}
    {job.error && !job.result?.verification?.some((check) => check.reason === job.error) && <p role="alert" className="error">{job.error}</p>}
    {job.result?.partial_state && <p className="ops-warning">{job.result.partial_state}</p>}
    {job.result?.cancel_error && <p role="alert" className="error">{job.result.cancel_error}</p>}
    {!!job.result?.verification?.length && <ul className="ops-verification" aria-label="Verification">{job.result.verification.map((check, i) => <li key={i}><StatusPill tone={checkTone(check)} label={checkWord(check)} /><strong>{check.check}</strong><span>{check.reason}</span></li>)}</ul>}
    {job.result?.manifest && <details><summary>Manifest</summary><pre className="ops-pre">{JSON.stringify(job.result.manifest, null, 2)}</pre><ToolbarButton onClick={() => download(JSON.stringify(job.result?.manifest, null, 2), "myflames-manifest.json", "application/json")}>Export manifest</ToolbarButton></details>}
    {job.detail && <details><summary>Clone progress and status</summary><pre className="ops-pre">{JSON.stringify(job.detail, null, 2)}</pre></details>}
    <details><summary>Output ({job.events.length.toLocaleString()} lines)</summary><pre className="ops-pre" tabIndex={0}>{job.events.map((event) => `${event.at} ${event.stream}: ${event.text}`).join("\n") || "No output yet."}</pre></details>
  </section>;
}

function JobSummary({ job, onShow }: { job: LifecycleJob; onShow: () => void }) {
  const progress = lifecycleProgress(job);
  return <div className="ops-job-summary">
    <progress aria-label={`${SECTION_LABEL[job.operation]} progress summary`} max={100} value={progress.value ?? undefined} title={progress.approximate ? ESTIMATE_HINT : undefined} />
    <span>{[`${SECTION_LABEL[job.operation]} ${job.state === "queued" ? "waiting" : "running"}`, progress.percent, progress.written !== null ? bytes(progress.written) : ""].filter(Boolean).join(" · ")}</span>
    <button className="text-button" type="button" onClick={onShow}>Show progress</button>
  </div>;
}

export function MaintenanceView({ connection, disabled, onBack, selection, section, onSection, memory, onMemory, targetVersions = [], missingPrivileges = {}, profiles = [] }: {
  connection: Connection; disabled: boolean; onBack?: () => void; selection: Selection;
  section: MaintenanceSection; onSection: (section: MaintenanceSection) => void;
  /** Server owns memory; never serialize this object into the workspace. */
  memory: MaintenanceMemory; onMemory: Dispatch<SetStateAction<MaintenanceMemory>>;
  /** Optional version catalog supplied by the integrator. An exact custom
   * version is always available; the backend validates version/tool ranges. */
  targetVersions?: string[]; missingPrivileges?: MissingPrivileges;
  /** Saved connections (no passwords): Clone's donor can be filled from one. */
  profiles?: Profile[];
}) {
  const reads = useConnectionReads(connection);
  const captureScope = useOpsScope(connection);
  const policy = useWritePolicy();
  const privileges = usePrivileges();
  const privilegesRef = useRef(privileges);
  privilegesRef.current = privileges;
  function denied(value: unknown, from: Connection) {
    const current = privilegesRef.current;
    if (current?.key === connectionKey(from) && isPrivilegeDenial(value)) void current.accountChanged();
  }
  const key = reads.key;
  useEffect(() => { denied(reads.error, connection); }, [reads.error, key]);
  const [target, setTarget] = useState("");
  const [customTarget, setCustomTarget] = useState("");
  const [preferShell, setPreferShell] = useState(true);
  // What to dump: the instance, or databases; with one database, its
  // chosen tables (none chosen: all of them).
  const [scope, setScope] = useState<"instance" | "schemas">("instance");
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tables, setTables] = useState<string[]>([]);
  const [directory, setDirectory] = useState("");
  const [directoryTouched, setDirectoryTouched] = useState(false);
  const [directoryVersion, setDirectoryVersion] = useState(0);
  const [directoryPicking, setDirectoryPicking] = useState(false);
  const [directoryError, setDirectoryError] = useState("");
  const directoryEdit = useRef(0);
  const reservedDirectories = useRef<string[]>([]);
  const [restoreDirectory, setRestoreDirectory] = useState("");
  /** lifecycle-dump-info-1.0 for the folder in the Dump folder field. It only
   * shapes the Default database choices and hints; the preflight stays the
   * authority and Start never waits for this read. */
  const [dumpInfo, setDumpInfo] = useState<{ directory: string; report: DumpInfo | null; error: string } | null>(null);
  const [restoreSchema, setRestoreSchema] = useState("");
  /** Restore into a database the list does not have (typed). */
  const [restoreNew, setRestoreNew] = useState(false);
  const [existing, setExisting] = useState<"refuse" | "ignore" | "drop">("refuse");
  const [allowSource, setAllowSource] = useState(false);
  const [resume, setResume] = useState(false);
  const [threads, setThreads] = useState(4);
  const [sqlDump, setSqlDump] = useState(false);
  const [dumpOptions, setDumpOptions] = useState<DumpOptionValues>({});
  // Donor credentials are deliberately private to this mounted component.
  // Neither the donor nor Prepared is ever passed to onMemory or onSection.
  const [donor, setDonor] = useState<Connection>({ name: "Donor", host: "", port: 3306, user: "", database: "", ssl_mode: "REQUIRED", password: "" });
  const [donorAddress, setDonorAddress] = useState("");
  /** The saved connection the donor fields were last filled from. */
  const [donorFill, setDonorFill] = useState<DonorFill | null>(null);
  const [cloneMode, setCloneMode] = useState<"in_place" | "directory">("in_place");
  const [dataDirectory, setDataDirectory] = useState("");
  const [prepared, setPrepared] = useState<Prepared | null>(null);
  const [attempt, setAttempt] = useState<{ key: string; inputKey: string } | null>(null);
  useEffect(() => { setAttempt(null); }, [donor.password]);
  const resultRef = useRef<HTMLElement>(null);
  const fieldsRef = useRef<HTMLDivElement>(null);
  const [sheet, setSheet] = useState<Prepared | null>(null);
  const sheetRef = useRef(sheet);
  sheetRef.current = sheet;
  const [typed, setTyped] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [startLeft, setStartLeft] = useState(false);
  const startSince = useSince(busy);
  const [checkingUpdates, setCheckingUpdates] = useState(false);
  const [clientToolsOpen, setClientToolsOpen] = useState(false);
  const changeToolsButton = useRef<HTMLButtonElement>(null);
  /** The button the Client tools dialog returns focus to (its opener). */
  const toolsReturnFocus = useRef<HTMLElement | null>(null);
  function openClientTools(from: HTMLElement | null) {
    toolsReturnFocus.current = from;
    setClientToolsOpen(true);
  }
  const [readKind, setReadKind] = useState<"tools" | "upgrade" | "preflight">("tools");
  const readSince = useSince(!!reads.loading);
  const detecting = !!reads.loading && readKind === "tools";
  const checking = !!reads.loading && readKind === "upgrade";
  const preparing = !!reads.loading && readKind === "preflight";
  const sending = useRef(false);
  const [cancelling, setCancelling] = useState("");
  const cancelPending = useRef(new Set<string>());
  const startedJobs = useRef(new Set<string>());
  const revealedJobs = useRef(new Set<string>());
  const [focusJob, setFocusJob] = useState("");
  useEffect(() => {
    if (!focusJob || sheet) return;
    // Wait for the card to render and the sheet's focus restoration to finish.
    const frame = requestAnimationFrame(() => { showJob(focusJob); setFocusJob(""); });
    return () => cancelAnimationFrame(frame);
  }, [focusJob, sheet, memory.jobs]);
  function rememberStart(job: LifecycleJob) {
    if (!job.id || job.state === "refused") return;
    startedJobs.current.add(job.id);
    onSection(job.operation);
    setFocusJob(job.id);
  }
  const [failure, setFailure] = useState<{ key: string; text: string; poll?: boolean } | null>(null);
  const [notice, setNotice] = useState("");
  const [pollRetry, setPollRetry] = useState(0);
  const publish = useRef(onMemory);
  publish.current = onMemory;
  // Credentials stay in page memory. A job retains its starting connection
  // even when the picker changes or its policy is edited.
  const jobConnections = useRef(new Map<string, Connection>());
  function rememberOwners(jobs: LifecycleJob[], c: Connection) {
    for (const job of jobs) if (job.id && !jobConnections.current.has(job.id)) jobConnections.current.set(job.id, { ...c });
  }
  const memoryRef = useRef(memory);
  memoryRef.current = memory;
  const currentKey = useRef(key);
  currentKey.current = key;
  // Nothing reads while the view is off screen (viewOnScreen.ts).
  const onScreen = useViewOnScreen();
  // The one chosen database's tables, read when it is chosen and Dump is on
  // screen. The chosen tables last while that database stays the only one.
  const oneSchema = scope === "schemas" && schemas.length === 1 ? schemas[0] : "";
  const schemaTables = useSchemaTables(connection, oneSchema, onScreen && section === "dump" && !!oneSchema);
  const [missingTables, setMissingTables] = useState<string[]>([]);
  useEffect(() => { setMissingTables([]); }, [key, oneSchema, tables.join("\u0000")]);
  useEffect(() => { setTables([]); }, [key, oneSchema]);
  const loadedTables = new Set(schemaTables.tables.map((t) => t.name));
  const unknownTables = tables.filter((name) => !loadedTables.has(name));
  const tableScope = !!oneSchema && tables.length > 0;
  const rawTools = memory.tools?.key === key ? memory.tools.report : null;
  // The client-missing (or client-auth-plugin) answer still lists the tools
  // and their download links but leaves server and offered empty; guard every
  // offered read.
  const toolsClientError = rawTools?.server_error && ["client-missing", "client-auth-plugin"].includes(rawTools.server_error.code)
    ? rawTools.server_error
    : null;
  const toolsMissing = !!toolsClientError;
  const rawOffered = rawTools?.offered;
  const tools = rawTools ? { ...rawTools, offered: {
    dump: rawOffered?.dump || [],
    upgrade: rawOffered?.upgrade || "",
    clone: rawOffered?.clone === true,
    physical_backup: rawOffered?.physical_backup === true,
    physical_backup_requires_local_files: rawOffered?.physical_backup_requires_local_files === true,
  } } : null;
  const targetReport = memory.targets?.key === key ? memory.targets.report : null;
  /** A target the installed MySQL Shell cannot check (shell.ok false). */
  const shellBlocked = (t: UpgradeTarget) => !!t.shell && !t.shell.ok;
  const shellNeeds = (t: UpgradeTarget) => t.shell?.needs ? `needs MySQL Shell ${t.shell.needs} or newer` : "needs a newer MySQL Shell";
  // The newest release can need a newer MySQL Shell than the installed one:
  // suggest the newest target the installed Shell can actually check.
  const suggestedTarget = targetReport
    ? targetReport.targets.some((t) => t.version === targetReport.recommended && !shellBlocked(t))
      ? targetReport.recommended
      : [...targetReport.targets].reverse().find((t) => !shellBlocked(t))?.version ?? null
    : null;
  // Percona targets are "8.4.11-11"; the checker takes the upstream version.
  const chosenTarget: UpgradeTarget | undefined = targetReport?.targets.find((t) => t.version === target);
  const chosenVersion = () => target === "custom" ? customTarget.trim() : chosenTarget ? chosenTarget.check_version : target;
  // Dump's databases, and Restore's Default database once its form is shown.
  const catalog = useSchemaCatalog(connection, onScreen && ((section === "dump" && scope !== "instance") || (section === "restore" && !!tools)));
  // Opening Dump or Restore re-reads the database list once: a database
  // created or dropped since the cached read shows up without Reload databases.
  const catalogReload = useRef(catalog.reload);
  catalogReload.current = catalog.reload;
  const catalogSection = useRef("");
  useEffect(() => {
    const opened = onScreen && (section === "dump" || section === "restore") ? section : "";
    if (opened && catalogSection.current !== opened) catalogReload.current();
    catalogSection.current = opened;
  }, [section, onScreen]);
  const restoreDatabases = (catalog.catalog?.schemas || []).filter((s) => !s.system).map((s) => s.name);
  // The donor is another server: every saved connection but the one in use.
  // Not one through SSH: CLONE runs from server to server, never through
  // this computer's tunnel (the backend refuses it, lifecycle.refuse_clone_through_ssh).
  const savedDonors = profiles.filter((p) => p.host && p.id !== connection.profile_id && !p.ssh_host);
  const filledFrom = donorFill && donor.host === donorFill.host && donor.port === donorFill.port && donor.user === donorFill.user &&
    (donor.ssl_mode || "REQUIRED") === donorFill.ssl_mode && (donor.ssl_ca || "") === donorFill.ssl_ca ? donorFill : null;
  // The saved connection the donor was filled from may be deleted later: say so.
  const donorGone = !!filledFrom && !savedDonors.some((p) => p.id === filledFrom.id);
  function fillDonor(id: string) {
    const p = savedDonors.find((x) => x.id === id);
    if (!p) return;
    const fields = { host: p.host, port: p.port, user: p.user, ssl_mode: DONOR_TLS.includes(p.ssl_mode) ? p.ssl_mode : "REQUIRED", ssl_ca: p.ssl_ca || "" };
    // Never a password: a remembered one stays in the secret store, and one
    // typed earlier was for the donor these fields named before.
    setDonor((d) => ({ ...d, ...fields, password: "" }));
    setDonorFill({ id: p.id, name: p.name, raised: fields.ssl_mode !== p.ssl_mode, ...fields });
  }
  const dumpToolName = tools?.offered.dump.find(name => !sqlDump || name !== "mysqlsh");
  const dumpTool = dumpToolName ? tools?.tools[dumpToolName] : undefined;
  const dumpCapabilities = tableScope ? dumpTool?.dump_table_capabilities : dumpTool?.dump_capabilities;
  const activeDumpOptions = Object.fromEntries(Object.entries(dumpOptions).filter(([name]) => {
    const option = dumpCapabilities?.options.find(o => o.key === name);
    return !!option && optionShown(option, dumpOptions) && (name !== "compression_level" || ["gzip", "zstd"].includes(String(dumpOptions.compression)));
  }));
  useEffect(() => { setDumpOptions({}); }, [key, dumpToolName]);
  // The roles the resolver could not fill for this server (client-tools-1.0),
  // and its engine note (for example the MariaDB sentence about its tools).
  const missingRole = (role: "dump" | "load") => rawTools?.used?.missing.find((m) => m.role === role);
  const toolNotes = rawTools?.used?.notes || [];
  const noToolSentence = (role: "dump" | "load") =>
    [missingRole(role)?.text || (role === "dump" ? "No dump tool for this server." : ""), ...toolNotes].filter(Boolean).join(" ");
  /** Dump can run only when a tool is offered for the chosen kind; Restore
   * needs the load role. */
  const sqlDumpOffered = !!tools?.offered.dump.some((name) => name !== "mysqlsh");
  const noDumpTool = !!tools && !toolsMissing && !tools.offered.dump.length;
  const noLoadTool = !!tools && !!missingRole("load");
  const dumpToolOptions: ["auto" | "sql", string][] = [];
  if (tools && !noDumpTool) {
    const sqlName = tools.offered.dump.find(name => name !== "mysqlsh") || (tools.server.engine === "mariadb" ? "mariadb-dump" : "mysqldump");
    // Automatic names what it will run here: without MySQL Shell it is the SQL dump tool.
    dumpToolOptions.push(["auto", rawTools?.used?.shell ? `Automatic (MySQL Shell ${rawTools.used.shell.version} first)`
      : tools.offered.dump.includes("mysqlsh") ? "Automatic (MySQL Shell first)" : `Automatic (${sqlName}; no MySQL Shell found)`]);
    if (sqlDumpOffered) dumpToolOptions.push(["sql", rawTools?.used?.dump ? `SQL dump (${rawTools.used.dump.program} ${rawTools.used.dump.version} · ${rawTools.used.dump.origin_label})` : `SQL dump (${sqlName})`]);
  }
  useEffect(() => { if (!sqlDumpOffered) setSqlDump(false); }, [sqlDumpOffered]);
  // The dump-info answer counts only for the folder still in the field.
  const folderInfo = dumpInfo && dumpInfo.directory === restoreDirectory.trim() ? dumpInfo : null;
  const remapDenied = folderInfo?.report?.remap_allowed === false;
  useEffect(() => {
    // A dump that creates its own databases restores into them only.
    if (!remapDenied) return;
    // Say so when it replaces a database the person had chosen.
    if (restoreNew || restoreSchema) setNotice(`Default database is back to As in the dump: ${folderInfo?.report?.remap_reason || "this dump restores into its own databases."}`);
    setRestoreNew(false); setRestoreSchema("");
  }, [remapDenied]);
  const plan = prepared?.report;
  const upgrade = memory.upgrade?.report || null;
  const shownPlan = plan?.operation === section && prepared?.key === key ? prepared : null;
  const planMatches = !!shownPlan && shownPlan.inputKey === requestKey() &&
    (plan?.operation !== "clone" || shownPlan.donor?.password === donor.password);
  const preflightError = readKind === "preflight" && attempt?.key === key && attempt.inputKey === requestKey() ? reads.error : "";
  const failedChecks = shownPlan?.report.checks.filter(c => c.status === "failed") || [];
  const tableFailure = planMatches ? failedChecks.find(c => c.check === "tables") : undefined;
  const folderFailure = planMatches ? failedChecks.find(c => ["output_folder", "free_space", "staging_space"].includes(c.check)) : undefined;
  const shownUpgrade = section === "upgrade" ? memory.upgrade : null;
  const oldData = shownPlan || shownUpgrade || memory.tools;
  const other = oldData && oldData.key !== key ? oldData.label : null;
  const blocked = policy?.blocked || (connection.read_only ? "Read-only: myflames refuses every change on this connection." : "");
  const activeJob = memory.jobs.find((b) => b.key === key && jobActive(b.report));
  const activeHere = !!activeJob;
  const dumpStem = dumpFolderStem(oneSchema || connection.name || connection.host);
  const defaultDumpParent = tools?.default_dump_parent || "";
  const finishedDumps = memory.jobs.filter(b => b.key === key && b.report.operation === "dump" && !jobActive(b.report)).map(b => b.report.id).join(",");
  useEffect(() => {
    if (section !== "dump" || !onScreen || directoryTouched || directoryPicking || busy || activeHere || (!lastDumpParent && !defaultDumpParent)) return;
    const controller = new AbortController();
    const edit = directoryEdit.current;
    setDirectoryError("");
    // Clearing a field often precedes typing a replacement. Let that edit
    // finish, and never apply a suggestion over a newer keystroke.
    const timer = window.setTimeout(() => {
      void suggestDumpFolder(lastDumpParent, dumpStem, controller.signal, reservedDirectories.current, defaultDumpParent).then(path => {
        if (!controller.signal.aborted && edit === directoryEdit.current) setDirectory(path);
      }).catch(e => {
        if (!controller.signal.aborted && edit === directoryEdit.current) setDirectoryError(`Could not suggest an output folder: ${(e as Error).message} Browse or enter a new folder path.`);
      });
    }, 250);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [key, section, onScreen, dumpStem, defaultDumpParent, scope, schemas.join("\u0000"), directoryTouched, directoryVersion, directoryPicking, busy, activeHere, finishedDumps]);
  function changeDirectory(path: string) {
    directoryEdit.current++;
    setDirectoryVersion(n => n + 1);
    setDirectoryTouched(!!path); setDirectory(path); setDirectoryError("");
  }
  // Read the restore folder's dump summary once typing or picking settles. A
  // folder change cancels the read in flight; a late answer for a replaced
  // folder is dropped (the abort guard), so the latest request always wins.
  useEffect(() => {
    const directory = restoreDirectory.trim();
    if (!directory) { setDumpInfo(null); return; }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      readDumpInfo(directory, controller.signal).then(
        (report) => { if (!controller.signal.aborted) setDumpInfo({ directory, report, error: "" }); },
        (e: unknown) => { if (!controller.signal.aborted) setDumpInfo({ directory, report: null, error: e instanceof Error ? e.message : String(e) }); },
      );
    }, 300);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [restoreDirectory]);
  function removeTables(names: string[]) {
    const remaining = tables.filter(name => !names.includes(name));
    setTables(remaining);
    if (!remaining.length) setNotice(`No tables are selected. Start dump… will run a fresh preflight for every table in ${oneSchema}.`);
  }
  const formDisabled = disabled || busy || (!!reads.loading && readKind !== "tools") || activeHere;
  const operation = section === "dump" || section === "restore" || section === "clone" ? section : null;
  const recipientGate = usePrivilege("clone_recipient");
  const restoreGate = usePrivilege("restore");
  const currentPlan = prepared?.key === key && prepared.inputKey === requestKey() &&
    (operation !== "clone" || prepared.donor?.password === donor.password) ? prepared.report : undefined;
  function lifecycleGate(op: "dump" | "restore" | "clone", p?: LifecyclePlan) {
    if (op === "clone") {
      // Each side is checked with its own credentials. The provider describes
      // only the recipient's in-place action, never the donor or directory mode.
      const directoryMode = p ? !!p.request.data_directory : cloneMode === "directory";
      const recipient = p?.checks.find(c => c.check === "recipient_privilege");
      const source = p?.checks.find(c => c.check === "donor_privilege");
      const checkGate = (check?: Verification) => check ? targetPrivilege(privilegeResult(), {
        allowed: check.status === "passed" ? true : check.status === "failed" ? false : null, note: check.reason,
      }) : privilegeResult();
      const local = recipient ? checkGate(recipient) : directoryMode ? privilegeResult() : recipientGate;
      const remote = checkGate(source);
      return [local, remote].find(g => !!g.disabledReason) || [local, remote].find(g => g.state === "unknown") || local;
    }
    const action = op === "restore" ? "restore" : ({ mysqlsh: "dump_shell", mysqldump: "dump_mysqldump", "mariadb-dump": "dump_mariadb" } as Record<string, string>)[p?.tool?.name || dumpToolName || ""];
    const generic = op === "restore" ? restoreGate : privilegeResult(action ? privileges?.report?.actions[action] : undefined, privileges?.report?.account);
    // Legacy dump/restore checks cover only a subset of grants. Object-grant
    // absence is not definitive denial, nor is this a complete allowance.
    const check = p?.checks?.find(c => c.check === "privileges");
    return check?.status === "not_checked" && !generic.disabledReason ? privilegeResult() : generic;
  }
  const operationGate = operation ? lifecycleGate(operation, currentPlan) : privilegeResult();
  const privilegeBlock = operation ? missingPrivileges[operation] || operationGate.disabledReason : "";
  const sheetGate = sheet ? lifecycleGate(sheet.report.operation, sheet.report) : privilegeResult();
  const sheetBlock = sheet ? blocked || missingPrivileges[sheet.report.operation] || sheetGate.disabledReason : "";
  const activeIds = memory.jobs.filter((b) => b.key === key && b.report.id && jobActive(b.report)).map((b) => b.report.id as string).sort().join(",");

  // The jobs this myflames holds for the connection in use: after a reload,
  // a switch back, or a policy change, they come back here to be followed and
  // cancelled. It reads no database and never starts anything.
  useEffect(() => {
    const controller = new AbortController();
    const c = { ...connection };
    listLifecycleJobs(c, controller.signal)
      .then((answer) => {
        if (controller.signal.aborted || currentKey.current !== key || !answer.jobs.length) return;
        rememberOwners(answer.jobs, c);
        publish.current((m) => adoptJobs(m, key, connectionLabel(c), answer.jobs));
      })
      .catch(() => {});
    return () => controller.abort();
    // Once per connection in use.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // Poll one batch at a time, not setInterval: slow status responses never
  // overlap. A connection switch aborts polling and greys the old jobs.
  useEffect(() => {
    if (!activeIds) return;
    const ids = activeIds.split(",");
    const controller = new AbortController();
    let timer: number | undefined;
    async function poll() {
      const answers = await Promise.allSettled(ids.map((id) => readLifecycleJob(jobConnections.current.get(id) || connection, id, controller.signal)));
      if (controller.signal.aborted || currentKey.current !== key) return;
      const errors: string[] = [];
      for (const answer of answers) {
        if (answer.status === "rejected") { errors.push(String((answer.reason as Error).message)); continue; }
        const job = answer.value;
        denied(job.error, jobConnections.current.get(job.id || "") || connection);
        publish.current((previous) => ({ ...previous, jobs: previous.jobs.map((bound) => bound.key === key && bound.report.id === job.id ? { ...bound, report: job } : bound) }));
      }
      if (errors.length) { setFailure({ key, poll: true, text: `Job status could not be read: ${errors.join("; ")}. Retry status; never start the same operation again while it may be running.` }); return; }
      setFailure((f) => (f?.poll ? null : f));
      timer = window.setTimeout(() => { void poll(); }, JOB_POLL_MS);
    }
    void poll();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [activeIds, key, pollRetry]);

  // Only a sheet keeps its original request connection. Never persist it.
  const sheetConnection = useRef<Connection | null>(null);
  function pConnection(p: Prepared): Connection {
    return sheetConnection.current && connectionKey(sheetConnection.current) === p.key ? sheetConnection.current : connection;
  }
  function decline(p: Prepared) {
    void recordDeclined(p.report.operation, p.report.commands, pConnection(p), {
      target: p.report.target, server: p.report.server.identity, binding: p.report.binding,
    });
  }
  useEffect(() => {
    const p = sheetRef.current;
    if (p && p.key !== key && !sending.current) { decline(p); setSheet(null); sheetConnection.current = null; }
    setTyped(""); setAcknowledged(false); setNotice("");
    setDonor((c) => ({ ...c, password: "" }));
    setPrepared((p) => p ? { ...p, donor: undefined, inputKey: "" } : p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  useEffect(() => () => { const p = sheetRef.current; if (p && !sending.current) decline(p); }, []);

  async function loadTools(checkUpdates = false) {
    if (disabled || busy || reads.running()) return;
    setReadKind("tools");
    setCheckingUpdates(checkUpdates);
    setFailure(null);
    await reads.run(checkUpdates ? "maintenance-tools:updates" : "maintenance-tools", checkUpdates ? "Checking tool updates…" : "Finding MySQL and MariaDB tools on this computer…", (c, signal) => readLifecycleTools(c, signal, checkUpdates), (bound) => onMemory((m) => ({ ...m, tools: bound })));
  }
  useOpenRead({ key, reportKey: tools ? key : null, ready: !disabled && !busy,
    busy: !!reads.loading || reads.running(), read: () => { void loadTools(); } });
  // The target list reads once per connection when Upgrade check opens; it
  // runs beside the view's reads so it never holds up Check upgrade.
  const targetsTried = useRef("");
  const [targetsLoading, setTargetsLoading] = useState(false);
  const [targetsError, setTargetsError] = useState<{ key: string; text: string } | null>(null);
  useEffect(() => {
    if (section !== "upgrade" || !tools || disabled || memory.targets?.key === key || targetsTried.current === key) return;
    targetsTried.current = key;
    const from = connection, label = connectionLabel(connection);
    const controller = new AbortController();
    setTargetsLoading(true); setTargetsError(null);
    readUpgradeTargets(from, controller.signal).then((report) => {
      onMemory((m) => ({ ...m, targets: { key: connectionKey(from), label, report } }));
    }, (e: unknown) => {
      if (!controller.signal.aborted) setTargetsError({ key: connectionKey(from), text: e instanceof Error ? e.message : String(e) });
    }).finally(() => { if (!controller.signal.aborted) setTargetsLoading(false); });
    return () => { controller.abort(); setTargetsLoading(false); if (targetsTried.current === key && memory.targets?.key !== key) targetsTried.current = ""; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [section, !!tools, disabled, key]);
  useEffect(() => { setTarget(""); setCustomTarget(""); }, [key]);
  useEffect(() => { if (suggestedTarget) setTarget((t) => t || suggestedTarget); }, [targetReport]);
  // An upgrade check's error belongs to the Upgrade check tab: leaving the
  // tab drops it (a later check replaces it, as every read does).
  useEffect(() => {
    if (section !== "upgrade" && readKind === "upgrade") reads.clearError();
  }, [section, readKind]);
  useEffect(() => {
    if (!preflightError && !failedChecks.length) return;
    resultRef.current?.focus();
    resultRef.current?.scrollIntoView({ block: "nearest" });
  }, [shownPlan, preflightError]);
  function focusFix(check: Verification) {
    const selector = check.check === "tables" ? "#ops-dump-tables" : ["shell_version", "dry_run"].includes(check.check) ? "#ops-dump-tool"
      : operation === "dump" ? '.ops-folder-input button' : operation === "restore" ? '.ops-folder-input button' : 'input';
    const control = fieldsRef.current?.querySelector<HTMLElement>(selector);
    // After the click's own row selection settles (the inspector would take focus).
    requestAnimationFrame(() => { control?.focus(); control?.scrollIntoView({ block: "center" }); });
  }
  function fixCheck(check: Verification) {
    const missing = check.check === "tables" ? missingTables.filter(n => tables.includes(n)) : [];
    // The fix buttons sit inside a check row: they act without selecting it.
    if (check.check === "tables" && missing.length) return <button type="button" className="text-button" onMouseDown={(e) => e.stopPropagation()} onClick={(e) => {
      e.stopPropagation(); removeTables(missing); schemaTables.reload();
    }}>Remove {missing.join(", ")} from the selection</button>;
    if (["output_folder", "free_space", "staging_space", "tables", "shell_version", "dry_run"].includes(check.check))
      return <button type="button" className="text-button" onMouseDown={(e) => e.stopPropagation()} onClick={(e) => { e.stopPropagation(); focusFix(check); }}>{["output_folder", "free_space", "staging_space"].includes(check.check) ? "Choose another folder…" : "Review fields"}</button>;
    // Server configuration and grants have no field here. Keep their full
    // evidence available without suggesting an unrelated form change.
    return <button type="button" className="text-button" onClick={() => selection.select(`check:${shownPlan?.report.checks.findIndex(c => c.check === check.check)}`, true)}>Review check</button>;
  }
  async function runUpgrade() {
    const version = chosenVersion();
    if (formDisabled || reads.running() || !VERSION.test(version) || missingPrivileges.upgrade_check) return;
    setNotice(""); setFailure(null);
    selection.select("", false);
    setReadKind("upgrade");
    await reads.run(`maintenance-upgrade:${JSON.stringify([version, preferShell])}`, "Checking the upgrade…", (c, signal) => checkUpgrade(c, version, preferShell, signal), (bound) => onMemory((m) => ({ ...m, upgrade: bound })));
  }
  function request(): PrepareRequest | null {
    if (section === "dump") return { operation: "dump", directory: directory.trim(), schemas: scope === "instance" ? [] : schemas, tables: tableScope ? tables : [], threads, prefer_shell: !sqlDump, options: activeDumpOptions };
    if (section === "restore") return { operation: "restore", directory: restoreDirectory.trim(), schema: restoreSchema.trim(), existing, allow_source: allowSource, resume, threads };
    if (section === "clone") return { operation: "clone", donor: { ...donor }, donor_address: donorAddress.trim(), data_directory: cloneMode === "directory" ? dataDirectory.trim() : "", require_ssl: true };
    return null;
  }
  function requestKey() {
    const r = request();
    return JSON.stringify(r?.operation === "clone" ? { ...r, donor: { ...r.donor, password: undefined } } : r);
  }
  async function prepare() {
    if (formDisabled || reads.running() || blocked || privilegeBlock || !operation) return;
    const payload = request();
    if (!payload) return;
    setNotice(""); setFailure(null);
    setAttempt({ key, inputKey: requestKey() });
    const donorSnapshot = payload.operation === "clone" ? { ...payload.donor } : undefined;
    const from = { ...connection };
    // No secret in the shared read's parameter/cache key. A forced read
    // ensures that edits to the donor's transient password are never joined.
    setReadKind("preflight");
    const result = await reads.run("maintenance-prepare", "Running the preflight…", (c, signal) => prepareLifecycle(c, payload, signal), () => {}, { force: true, onError: () => setPrepared(null) });
    if (!result) return;
    const p: Prepared = { ...result, donor: donorSnapshot, at: Date.now(), inputKey: requestKey() };
    setPrepared(p);
    selection.select("", false);
    if (p.report.ready) { sheetConnection.current = from; setTyped(""); setAcknowledged(false); setStartLeft(false); setSheet(p); }
    else {
      const failed = p.report.checks.find(c => c.check === "tables" && c.status === "failed");
      if (failed) {
        setMissingTables(Array.isArray(failed.missing) ? failed.missing.filter((n): n is string => typeof n === "string") : []);
        schemaTables.reload();
      }
    }
  }
  async function start() {
    const p = sheet;
    if (!p || sheetBlock || sending.current || p.key !== reads.currentKey() || !p.report.ready) return;
    if (p.report.operation !== "dump" && typed !== p.report.target) return;
    if (p.report.required_acknowledgement && !acknowledged) return;
    const live = captureScope();
    sending.current = true; setStartLeft(false); setBusy(true); setFailure(null);
    let sent = false;
    const known = new Set(memoryRef.current.jobs.map((b) => b.report.id));
    const from = pConnection(p);
    try {
      const checked = policy ? await policy.check() : from;
      if (!live() || connectionKey(checked) !== p.key) throw new Error("The connection or its write policy changed. Nothing was started. Run the preflight and confirm again.");
      // Python rebuilds the complete preflight on a fresh connection before
      // starting. An old local plan is also reviewed again explicitly.
      if (Date.now() - p.at > PREFLIGHT_TTL_MS) {
        decline(p); setSheet(null); setNotice("The preflight expired. Run it again to review a fresh plan. Nothing was started."); return;
      }
      sent = true;
      const job = await startLifecycle(checked, p.report, p.report.operation === "dump" ? p.report.target : typed, p.donor,
        acknowledged ? p.report.required_acknowledgement || undefined : undefined);
      if (p.report.operation === "dump" && job.state !== "refused") {
        lastDumpParent = parentPath(p.report.target);
        reservedDirectories.current = [...reservedDirectories.current.slice(-KEEP_FINISHED), p.report.target];
        setDirectoryTouched(false);
        // A started dump ends its table picking: the next dump starts from All tables.
        setTables([]);
      }
      denied(job.error, checked);
      rememberOwners([job], checked);
      rememberStart(job);
      publish.current((previous) => adoptJobs(previous, p.key, p.label, [job]));
      setSheet(null); sheetConnection.current = null;
      if (live()) {
        setPrepared(null);
        if (job.state === "refused") setFailure({ key: p.key, text: job.error || "myflames refused to start it. Nothing was started." });
        else setNotice(job.policy?.text || "");
        setPollRetry((n) => n + 1);
      }
    } catch (e) {
      denied(e, from);
      if (!sent) { if (live()) setFailure({ key: p.key, text: (e as Error).message }); return; }
      const failed = writeFailure(e, { audited: true });
      if (failed.phase === "failed") {
        if (live()) setFailure({ key: p.key, text: failed.message });
        return;
      }
      // The reply was lost or failed: the job may be running. Look for it
      // instead of starting again.
      let found: LifecycleJob | undefined;
      try {
        const answer = await listLifecycleJobs(from);
        found = answer.jobs.find((job) => job.id && !known.has(job.id) && job.operation === p.report.operation);
        if (found) {
          if (p.report.operation === "dump" && found.state !== "refused") {
            lastDumpParent = parentPath(p.report.target);
            reservedDirectories.current = [...reservedDirectories.current.slice(-KEEP_FINISHED), p.report.target];
            setDirectoryTouched(false);
            setTables([]);
          }
          rememberOwners([found], from);
          rememberStart(found);
          publish.current((m) => adoptJobs(m, p.key, p.label, [found as LifecycleJob]));
        }
      } catch {
        found = undefined;
      }
      if (live()) {
        if (found) { setPrepared(null); setNotice(`The Start reply was lost, but the ${SECTION_LABEL[p.report.operation].toLowerCase()} is in myflames' job list and is shown below. It was not started again.`); }
        else setFailure({ key: p.key, text: `${(e as Error).message} Whether it started is unknown. Do not start it again before you check the target and the Audit log.` });
      }
    } finally {
      sending.current = false; setBusy(false); setStartLeft(false);
      setSheet(null); sheetConnection.current = null;
    }
  }
  async function cancel(bound: Bound<LifecycleJob>) {
    const id = bound.report.id;
    if (!id || bound.key !== reads.currentKey() || cancelPending.current.has(id)) return;
    cancelPending.current.add(id); setCancelling(id);
    const live = captureScope();
    try {
      const job = await cancelLifecycleJob(jobConnections.current.get(id) || connection, id);
      if (live()) {
        onMemory((m) => ({ ...m, jobs: m.jobs.map((b) => b.key === bound.key && b.report.id === id ? { ...b, report: job } : b) }));
        setPollRetry((n) => n + 1);
      }
    } catch (e) {
      const failed = writeFailure(e, { audited: true });
      if (live()) setFailure({ key: bound.key, text: `${failed.phase === "unknown" ? "Outcome unknown. " : ""}${failed.message}` });
    }
    finally { cancelPending.current.delete(id); setCancelling((pending) => pending === id ? "" : pending); }
  }

  // lifecycle-tools-2.0: one row per copy found (the tools record's entry for
  // the copy's name supplies its update check and download link). Without
  // copies (older answer), the record alone lists the tools.
  const toolCopies = memory.tools?.report.copies;
  const toolRows = (toolCopies?.length
    ? toolCopies
    : Object.entries(memory.tools?.report.tools || {}).map(([name, tool]) => ({ ...tool, name }))
  ).map((copy, i) => ({
    ...(memory.tools?.report.tools[copy.name || ""] || {}),
    ...copy,
    name: copy.name || "",
    // The update check covered the copy in the tools record for the name
    // (tools[name]); any other copy, even one used for another role (a
    // query-only client), was never checked.
    ...(toolCopies?.length && copy.path !== memory.tools?.report.tools[copy.name || ""]?.path ? { update: undefined } : {}),
    row: `tool:${copy.path || `${copy.name}:${i}`}`,
  }));
  const updateWord = (tool: Tool): string => {
    const update = tool.update;
    if (tool.deprecated) return "Deprecated";
    if (!tool.available) return "Not installed";
    if (!update || update.status === "not_checked") return "Not checked";
    if (update.status === "available") return `Update available: ${update.latest_version}`;
    if (update.status === "current") return "Current in checked series";
    return "Could not check";
  };
  const availableUpdates = toolRows.filter(t => t.update?.status === "available" && t.available && !t.deprecated);
  const toolColumns: Column<(typeof toolRows)[number]>[] = [
    { key: "name", label: "Tool", primary: true, sort: (r) => r.name, render: (r) => r.name },
    { key: "status", label: "Status", sort: (r) => Number(r.available), render: (r) => <StatusPill tone={r.deprecated ? "unknown" : r.available ? "ok" : "unknown"} label={r.deprecated ? "Deprecated" : r.available ? "Available" : "Not found"} /> },
    { key: "version", label: "Version", sort: (r) => (r.version_tuple || []).join("."), render: (r) => (r.version_tuple?.length ? <span title={r.version}>{r.version_tuple.join(".")}</span> : r.version || r.error || "Not available") },
    { key: "from", label: "From", sort: (r) => r.origin_label || "", render: (r) => r.origin_label || "Not available" },
    { key: "path", label: "Path", flex: true, render: (r) => (r.path ? <code className="client-path" title={r.realpath || r.path}>{r.path}</code> : "Not found") },
    { key: "update", label: "Updates", render: (r) => <span title={r.update?.message}><StatusPill tone={r.update?.status === "available" ? "watch" : "unknown"} label={updateWord(r)} /></span> },
    { key: "download", label: "Download", render: (r) => toolDownloadUrl(r.download_url) ? <a className="ops-tool-link" href={toolDownloadUrl(r.download_url)} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} aria-label={`Official download page for ${r.name}`}>Official site</a> : "Not available" },
    { key: "engine", label: "Engine", render: (r) => (r.engine ? engineName(r.engine) : "Not available") },
    { key: "used", label: "Used", render: (r) => (r.used ? <StatusPill tone="ok" label="Used" /> : "") },
  ];
  const findings = (upgrade?.checks || []).map((finding, index) => ({ ...finding, row: String(index) }));
  const findingColumns: Column<(typeof findings)[number]>[] = [
    { key: "result", label: "Result", sort: (r) => findingResult(r)[2], render: (r) => { const [tone, word] = findingResult(r); return <StatusPill tone={tone} label={word} />; } },
    { key: "check", label: "Check", primary: true, flex: true, sort: (r) => r.title, render: (r) => r.title },
    { key: "objects", label: "Objects", num: true, sort: (r) => r.affected_objects.length, render: (r) => <span title={r.affected_objects.join(", ") || undefined}>{r.affected_objects.length.toLocaleString()}</span> },
  ];
  const selectedTool = section === "tools" ? toolRows.find((t) => t.row === selection.key) : undefined;
  const selectedFinding = section === "upgrade" ? findings.find((f) => `finding:${f.row}` === selection.key) : undefined;
  const selectedCheck = operation ? shownPlan?.report.checks.find((_, i) => `check:${i}` === selection.key) : undefined;
  let inspector: InspectorContent | null = null;
  if (selectedTool) inspector = {
    key: selectedTool.row, title: selectedTool.name,
    meta: <StatusPill tone={selectedTool.deprecated ? "unknown" : selectedTool.available ? "ok" : "unknown"} label={selectedTool.deprecated ? "Deprecated" : selectedTool.available ? "Available" : "Not found"} />,
    body: <InspectorSection title="Detected tool"><Fields rows={[["Path", selectedTool.path || "Not found"], ["Version", selectedTool.version || "Not available"], ["Engine", selectedTool.engine ? engineName(selectedTool.engine) : "Not available"], !!selectedTool.origin_label && ["From", selectedTool.origin_label], !!selectedTool.reason && ["Why this copy", reasonSentence(selectedTool.reason, selectedTool, tools?.server)], !!selectedTool.used && ["Used", "This copy runs the job on this server"], !!selectedTool.error && ["Probe", selectedTool.error]]} />{selectedTool.deprecated && <p>Deprecated tools are listed for information and never offered.</p>}
      {selectedTool.update && <><p>{selectedTool.update.message}</p><Fields rows={[["Latest checked", selectedTool.update.latest_version || "Unknown"], ["Checked at", selectedTool.update.checked_at || "Not checked"]]} /></>}
      {toolDownloadUrl(selectedTool.download_url) && <a className="ops-tool-link" href={toolDownloadUrl(selectedTool.download_url)} target="_blank" rel="noopener noreferrer">Official download page</a>}
    </InspectorSection>,
  };
  if (selectedFinding) {
    const [tone, word] = findingResult(selectedFinding);
    const docs = documentationUrl(selectedFinding.doc_link);
    inspector = {
      key: `finding:${selectedFinding.row}`, title: selectedFinding.title, meta: <StatusPill tone={tone} label={word} />,
      body: <>
        <InspectorSection title="Finding">{selectedFinding.description && <p>{selectedFinding.description}</p>}{docs && <p><a href={docs} target="_blank" rel="noreferrer">Documentation</a></p>}</InspectorSection>
        <InspectorSection title={`Objects (${selectedFinding.affected_objects.length.toLocaleString()})`}><pre className="ops-pre">{selectedFinding.affected_objects.join("\n") || "No affected objects reported."}</pre></InspectorSection>
        {(selectedFinding.problems || selectedFinding.details) && <details className="ops-raw"><summary>As the checker reported it</summary><pre className="ops-pre">{JSON.stringify(selectedFinding.problems || selectedFinding.details, null, 2)}</pre></details>}
      </>,
    };
  }
  if (selectedCheck) inspector = { key: selection.key, title: selectedCheck.check, meta: <StatusPill tone={checkTone(selectedCheck)} label={checkWord(selectedCheck)} />, body: <InspectorSection title="Preflight check">{!planMatches && <p className="hint">From the last preflight; settings changed since. Start again to check the new settings.</p>}<p>{selectedCheck.reason}</p><details className="ops-raw"><summary>As the preflight reported it</summary><pre className="ops-pre">{JSON.stringify(selectedCheck, null, 2)}</pre></details></InspectorSection> };

  const shellVersion = tools?.tools.mysqlsh?.available ? tools.tools.mysqlsh.version_tuple?.join(".") || "" : "";
  const versions = [...new Set([...targetVersions, shellVersion].filter((v) => VERSION.test(v)))];
  const version = chosenVersion();
  // Newest first; the suggested target says so. Without a target list (still
  // reading, or it failed) the MySQL Shell version stays the one offer.
  const targetOptions: [string, string][] = targetReport && targetReport.targets.length
    ? [...targetReport.targets].reverse().map((t): [string, string] => [t.version,
      [t.version, t.path_label, shellBlocked(t) ? shellNeeds(t) : "", t.version === suggestedTarget ? "suggested" : ""].filter(Boolean).join(" · ")])
    : targetReport?.server.engine === "mariadb" ? [] : versions.map((v): [string, string] => [v, v]);
  // Chosen tables come from the loaded list: one a reload no longer lists blocks the preflight.
  const dumpValid = !!directory.trim() && validInteger(threads, 1, 64) && (scope === "instance" || schemas.length > 0) && !unknownTables.length && !missingTables.length;
  const restoreValid = !!restoreDirectory.trim() && validInteger(threads, 1, 64);
  const cloneValid = !!donor.host.trim() && !!donor.user.trim() && validInteger(donor.port, 1, 65535) && (cloneMode === "in_place" || dataDirectory.startsWith("/"));
  const canPrepare = operation === "dump" ? dumpValid && !!tools?.offered.dump.some((name) => !sqlDump || name !== "mysqlsh") : operation === "restore" ? restoreValid && !!tools : operation === "clone" ? cloneValid && !!tools?.offered.clone : false;
  const controls = <>
    <Segmented label="Maintenance task" value={section} options={(Object.keys(SECTION_LABEL) as MaintenanceSection[]).filter(value => value !== "clone" || !tools || tools.offered.clone).map((value) => ({ value, label: SECTION_LABEL[value], disabled: busy || (!!reads.loading && readKind !== "tools") }))} onChange={(next) => { selection.select("", false); setNotice(""); onSection(next); }} />
  </>;
  const form = (children: ReactNode) => <div className="ops-form"><fieldset disabled={formDisabled}>{children}</fieldset></div>;
  const statusError = failure?.key === key ? failure.text : readKind === "preflight" || (readKind === "upgrade" && section !== "upgrade") ? "" : reads.error;
  const retryRead = () => { void (readKind === "upgrade" ? runUpgrade() : readKind === "preflight" ? prepare() : loadTools(checkingUpdates)); };
  const readStatus = (kind: typeof readKind) => <ActionStatus keepLine state={{ phase: reads.loading && readKind === kind ? "pending" : "idle", message: reads.loading, startedAt: readSince }}
    onStop={reads.stop} stopLabel="Stop waiting" />;
  const counts = upgrade?.counts;
  // A partial check never reads as a green light: with nothing found it says Partial.
  const upgradeTone: Tone | undefined = !upgrade || upgrade.method === "mariadb_upgrade_guidance" ? undefined : counts?.error ? "problem" : counts?.warning ? "watch" : upgrade.partial ? "unknown" : "ok";
  const upgradeToneLabel = upgradeTone === "unknown" ? "Partial" : upgradeTone === "ok" ? "No findings" : undefined;
  const kindOfCheck = upgrade ? (upgrade.method === "mariadb_upgrade_guidance" ? "mariadb-upgrade steps" : upgrade.partial ? "Partial check (native subset)" : "MySQL Shell") : "";
  const summary = section === "upgrade" && upgrade
    ? [
        `${engineName(upgrade.server.engine, upgrade.server.version, upgrade.server.product)} to ${upgrade.target_version}`,
        kindOfCheck,
        upgrade.method === "mariadb_upgrade_guidance" ? "" : [plural(counts?.error || 0, "error"), plural(counts?.warning || 0, "warning"), plural(counts?.notice || 0, "notice")].join(", "),
      ].filter(Boolean).join(" · ")
    : tools
      ? `${toolsMissing ? "Server unreadable" : engineName(tools.server.engine, tools.server.version, tools.server.product)} · ${SECTION_LABEL[section]}`
      : "";
  const upgradeNotes = section === "upgrade" && upgrade ? upgrade.limitations : undefined;
  return <>
    <ViewLayout id="maintenance" title="Maintenance" className="ops-view" onBack={onBack} controls={controls} onRefresh={section === "tools" ? () => { void loadTools(); } : undefined} refreshLabel="Detect again" refreshDisabled={disabled || busy || !!reads.loading} loading={!!reads.loading} inspector={inspector} otherConnection={other}
      inspectorEmpty={<p>Select a tool, a finding or a check to see its details here.</p>}
      info={<><p>Detect the MySQL and MariaDB tools on this computer, check an upgrade, or dump, restore or clone the connection in use.</p><p>The upgrade check and every preflight only read. A dump, restore or clone starts only after you review its exact commands and confirm (restore and clone also ask you to type the target), and it is recorded in the Audit log.</p></>}
      status={<><ViewStatus loading={readKind === "tools" && tools && !checkingUpdates ? reads.loading : ""} error={statusError} errorCode={failure?.key === key || readKind === "preflight" ? "" : reads.errorCode}
        onStop={reads.loading ? reads.stop : undefined} onRetry={reads.error && readKind !== "preflight" && failure?.key !== key ? retryRead : undefined} doneAt={reads.doneAt}>{failure?.key === key && failure.poll && <button className="text-button" type="button" onClick={() => { setFailure(null); setPollRetry((n) => n + 1); }}>Retry status</button>}</ViewStatus>
        {startLeft && busy && sheet && <section className="ops-job" aria-label={`${SECTION_LABEL[sheet.report.operation]} start on ${sheet.label}`}>
          <strong>{SECTION_LABEL[sheet.report.operation]} · {sheet.label}</strong>
          <ActionStatus state={{ phase: "pending", message: "Waiting for the Start reply. Whether it started is unknown; do not start it again.", startedAt: startSince }} />
        </section>}
        {memory.jobs.map((bound, i) => <JobProgress key={bound.report.id || `${bound.key}:${i}`} bound={bound} shown={bound.report.operation === section} started={startedJobs.current.has(bound.report.id || "")} revealed={revealedJobs.current} currentKey={key} cancelling={cancelling} onCancel={(b) => { void cancel(b); }} />)}</>}>
      {summary && (
        <SummaryLine tone={section === "upgrade" ? upgradeTone : undefined} toneLabel={section === "upgrade" ? upgradeToneLabel : undefined} notes={upgradeNotes}
          extra={section === "upgrade" && upgrade ? <ToolbarButton onClick={() => download(JSON.stringify(upgrade, null, 2), "myflames-upgrade-check.json", "application/json")}>Export JSON</ToolbarButton> : undefined}>
          {summary}
        </SummaryLine>
      )}
      {!tools && <div className="ops-tool-loading">{detecting ? "Finding MySQL and MariaDB tools on this computer…" : reads.error ? "Tool detection failed. Try again above to continue." : "Waiting to detect local tools…"}{readStatus("tools")}</div>}
      {section === "tools" && memory.tools && <>
        {!toolsMissing && memory.tools.report.used && (
          <div className="ops-used client-tools-used-block">
            <strong>Used for this server</strong>
            <UsedForServer used={memory.tools.report.used} server={memory.tools.report.server} onChange={() => openClientTools(changeToolsButton.current)} changeRef={changeToolsButton} />
          </div>
        )}
        <div className="ops-actions"><ActionButton busy={detecting && checkingUpdates} busyLabel="Checking…" disabled={disabled || busy || (!!reads.loading && !checkingUpdates)} onClick={() => { void loadTools(true); }}>Check for updates</ActionButton>{tools && <ActionStatus keepLine state={{ phase: detecting && checkingUpdates ? "pending" : "idle", message: reads.loading, startedAt: readSince }} onStop={reads.stop} stopLabel="Stop waiting" />}</div>
        {!!availableUpdates.length && <div className="ops-tool-updates" role="status">Updates available for {availableUpdates.map(t => t.name).join(", ")}.</div>}
        <DataTable id="maintenance.tools" label="Detected tools" rows={toolRows} columns={toolColumns} rowKey={(r) => r.row} selection={selection} />
        {toolsMissing ? (
          <ClientMissingNotice className="ops-offered" text={toolsClientError?.code === "client-auth-plugin" ? toolsClientError.error : ""} onRetry={() => { void loadTools(); }} />
        ) : (
        <div className="ops-offered"><Fields label="Offered for this server" rows={[["Dump", memory.tools.report.offered.dump.join(", ") || "No dump tool for this engine"], ["Upgrade check", memory.tools.report.offered.upgrade === "mysqlsh" ? "MySQL Shell (full check)" : memory.tools.report.offered.upgrade === "native_subset" ? "Partial check (native subset)" : "mariadb-upgrade steps to run on the server host"], ["Clone", memory.tools.report.offered.clone ? "Offered after its preflight" : "Not offered"], ["Physical backup", memory.tools.report.offered.physical_backup ? "Detected; runs on the server host" : "Not available"]]} /></div>
        )}
      </>}
      {section === "upgrade" && tools && <>
        {form(<>
          <label className="ops-field"><span>Target version</span><Select label="Target version" value={target} options={[["", targetsLoading ? "Reading releases…" : "Choose a version"], ...targetOptions, ["custom", "Other version…"]]} onChange={setTarget} /></label>
          {target === "custom" && <FormField label="Exact version" value={customTarget} onChange={setCustomTarget} placeholder="8.4.0" hint="major.minor.patch" />}
          <UpgradeTargetNotes report={targetReport} chosen={chosenTarget} loading={targetsLoading} error={targetsError?.key === key ? targetsError.text : ""} />
          <Checkbox checked={!preferShell} onChange={(v) => setPreferShell(!v)}>Partial check without MySQL Shell (native subset)</Checkbox>
        </>)}
        <div className="ops-actions"><ReadAction busy={checking} busyLabel="Checking…" gate={privilegeResult()} write={false} className="primary" disabled={formDisabled || detecting || !VERSION.test(version)} blocked={missingPrivileges.upgrade_check} onClick={() => { void runUpgrade(); }}>Check upgrade</ReadAction>{readStatus("upgrade")}<span className="hint">Only reads. Never says an upgrade is safe.</span></div>
        {upgrade && <>
          {upgrade.method === "mariadb_upgrade_guidance" ? <div className="ops-result">{upgrade.limitations.map((line, i) => <p key={i}>{line}</p>)}</div> : <DataTable id="maintenance.upgrade" label="Upgrade findings" columns={findingColumns} rows={findings} rowKey={(r) => `finding:${r.row}`} selection={selection} defaultSort={{ key: "result", dir: "desc" }} />}
          {upgrade.manual_checks.length > 0 && <details className="ops-details"><summary>Manual checks ({upgrade.manual_checks.length.toLocaleString()})</summary><pre className="ops-pre">{JSON.stringify(upgrade.manual_checks, null, 2)}</pre></details>}
        </>}
      </>}
      <div ref={fieldsRef} className="ops-task-fields">
      {section === "dump" && tools && noDumpTool && (
        <PrerequisiteGate action={<button type="button" className="button small" onClick={(e) => openClientTools(e.currentTarget)}>Choose client tools…</button>}>
          {noToolSentence("dump")}
        </PrerequisiteGate>
      )}
      {section === "restore" && tools && noLoadTool && (
        <PrerequisiteGate action={<button type="button" className="button small" onClick={(e) => openClientTools(e.currentTarget)}>Choose client tools…</button>}>
          {noToolSentence("load")}
        </PrerequisiteGate>
      )}
      {section === "dump" && tools && <div className="ops-form ops-dump"><fieldset disabled={formDisabled}>
        <FormGrid>
          <FormRow label="Scope" htmlFor="ops-dump-scope">
            <Select id="ops-dump-scope" label="Scope" value={scope} options={[["instance", "Whole instance"], ["schemas", "Databases"]]} onChange={(v) => { if (v === "instance" || v === "schemas") setScope(v); }} />
          </FormRow>
          {scope !== "instance" && <div className="form-row">
            <span className="form-row-label">Databases</span>
            <div className="form-row-control">
              <SchemaPicker id="ops-dump-schemas" value={schemas} onChange={(next) => { setSchemas(next); setTables([]); }} catalog={catalog.catalog} error={catalog.error} loading={catalog.loading} onReload={catalog.reload} disabled={formDisabled} emptyLabel="No database chosen" />
              <p className="hint">{!schemas.length ? "Choose at least one database." : schemas.length > 1 ? "Choose one database to pick its tables." : ""}</p>
            </div>
          </div>}
          {oneSchema && <div className={`form-row${tableFailure || missingTables.length ? " ops-invalid" : ""}`} role="group" aria-label="Tables" aria-invalid={!!tableFailure || !!missingTables.length || undefined} data-dump-tables={oneSchema}>
            <span className="form-row-label">Tables</span>
            <div className="form-row-control">
              <TablePicker id="ops-dump-tables" schema={oneSchema} value={tables} onChange={setTables} tables={schemaTables} disabled={formDisabled} />
              <p className={tableFailure || missingTables.length || unknownTables.length || schemaTables.error ? "form-error" : "hint"} role={tableFailure || missingTables.length || unknownTables.length || schemaTables.error ? "alert" : undefined}>
                {missingTables.length ? `Not found in ${oneSchema}: ${missingTables.join(", ")}.` : tableFailure ? tableFailure.reason : unknownTables.length && schemaTables.truncated ? `Not in the tables loaded so far: ${unknownTables.join(", ")}. Load more tables to confirm them.`
                  : unknownTables.length ? `No longer in ${oneSchema}: ${unknownTables.join(", ")}. Choose tables from the list again.`
                  : schemaTables.error ? `Could not read the tables of ${oneSchema}: ${schemaTables.error}`
                    : schemaTables.loading && !schemaTables.tables.length ? `Reading the tables of ${oneSchema}…`
                      : tables.length ? `${tables.length.toLocaleString()} of ${schemaTables.truncated ? `${schemaTables.tables.length.toLocaleString()}+` : schemaTables.tables.length.toLocaleString()} tables.`
                        : `Every table of ${oneSchema}; choose tables to dump only those.`}
              </p>
              {(missingTables.length ? missingTables : !schemaTables.truncated && !schemaTables.loading && !schemaTables.error ? unknownTables : []).map(name =>
                <button key={name} type="button" className="text-button" onClick={() => removeTables([name])}>Remove {name} from the selection</button>)}
            </div>
          </div>}
          <FormRow label="Tool" htmlFor="ops-dump-tool">
            {noDumpTool ? (
              <select id="ops-dump-tool" className="select" aria-label="Tool" disabled value="">
                <option value="" disabled>No dump tool for this server</option>
              </select>
            ) : (
              <Select id="ops-dump-tool" label="Tool" value={sqlDump ? "sql" : "auto"} options={dumpToolOptions} onChange={(v) => setSqlDump(v === "sql")} />
            )}
          </FormRow>
          <div className={`ops-folder-check${folderFailure ? " ops-invalid" : ""}`} role="group" aria-label="Output folder" aria-invalid={!!folderFailure || undefined}><DumpFolderField key={`dump:${key}`} value={directory} stem={dumpStem} onChange={changeDirectory} onPicking={picking => { directoryEdit.current++; setDirectoryPicking(picking); }} disabled={formDisabled} />{directoryError && <p className="form-error" role="alert">{directoryError}</p>}{folderFailure && <p className="form-error">{folderFailure.reason}</p>}</div>
        </FormGrid>
        <details className="ops-dump-options"><summary>Options{dumpToolName ? ` for ${dumpToolName}` : ""}{dumpCapabilities && <span className="hint">{optionsSummary(dumpCapabilities, dumpOptions, dumpToolName === "mysqlsh" ? threads : undefined) || "Tool defaults"}</span>}</summary>
          {dumpCapabilities && <DumpOptions catalog={dumpCapabilities} value={dumpOptions} onChange={setDumpOptions} disabled={formDisabled}
            threads={dumpToolName === "mysqlsh" ? threads : undefined} onThreads={dumpToolName === "mysqlsh" ? setThreads : undefined} />}
        </details>
      </fieldset></div>}
      {section === "restore" && tools && form(<>
        <div className="ops-target"><span>Restores into</span><strong>{connectionLabel(connection)}</strong></div>
        <FolderField key={`restore:${key}`} label="Dump folder" value={restoreDirectory} onChange={setRestoreDirectory} disabled={formDisabled}
          hint={folderInfo?.error ? folderInfo.error : "A completed myflames dump with its checksummed manifest."} />
        <details><summary>Options</summary>
          {remapDenied ? (
            <label className="ops-field"><span>Default database</span>
              <Select label="Default database" value="" options={[["", "As in the dump"]]} onChange={() => {}} />
              <span className="hint">{folderInfo?.report?.remap_reason}</span>
            </label>
          ) : catalog.error ? (
            <FormField label="Default database" value={restoreSchema} onChange={setRestoreSchema} placeholder="As in the dump"
              hint={`Could not list databases (${catalog.error}). Type a name, or leave it empty. SQL dumps can name other databases; this does not confine the restore.`} />
          ) : <>
            <label className="ops-field"><span>Default database</span>
              <Select label="Default database" value={restoreNew ? NEW_DATABASE : restoreSchema}
                options={[["", "As in the dump"],
                  ...(restoreSchema && !restoreNew && !restoreDatabases.includes(restoreSchema) ? [[restoreSchema, restoreSchema] as [string, string]] : []),
                  ...restoreDatabases.map((name): [string, string] => [name, name]),
                  [NEW_DATABASE, "New database…"]]}
                onChange={(v) => { setRestoreNew(v === NEW_DATABASE); setRestoreSchema(v === NEW_DATABASE ? "" : v); }} />
              <span className="hint">{catalog.loading ? "Listing databases…" : "SQL dumps can name other databases; this does not confine the restore."}</span>
            </label>
            {restoreNew && <FormField label="New database name" value={restoreSchema} onChange={setRestoreSchema} />}
          </>}
          <label className="ops-field"><span>Existing objects</span><Select label="Existing objects" value={existing} options={[["refuse", "Stop if any exist"], ["ignore", "Skip existing (MySQL Shell only)"], ["drop", "Drop and recreate"]]} onChange={(v) => { if (v === "refuse" || v === "ignore" || v === "drop") setExisting(v); }} /></label>
          <Checkbox checked={allowSource} onChange={setAllowSource}>Allow restoring into the server the dump came from</Checkbox>
          <Checkbox checked={resume} onChange={setResume}>Resume an interrupted MySQL Shell load</Checkbox>
          <label className="ops-field"><span>Threads</span><NumberField min={1} max={64} value={threads} onChange={(v) => setThreads(+v)} /></label>
        </details>
      </>)}
      {section === "clone" && tools && (!tools.offered.clone ? <PrerequisiteGate>{tools.server.engine === "mariadb" ? "MariaDB has no clone plugin: copy a server with mariadb-backup on its host instead." : "Clone is not offered for this server version."}</PrerequisiteGate> : form(<>
        <div className="ops-target"><span>Recipient (the connection in use)</span><strong>{connectionLabel(connection)}</strong></div>
        {(savedDonors.length > 0 || donorGone) && <label className="ops-field"><span>Fill from saved connection</span>
          <Select label="Fill from saved connection" value={filledFrom?.id || ""} onChange={fillDonor}
            options={[["", "Choose a connection"], ...savedDonors.map((p): [string, string] => [p.id, `${p.name} · ${p.host}:${p.port}`]),
              ...(donorGone && filledFrom ? [[filledFrom.id, deletedConnectionLabel(filledFrom.id)] as [string, string]] : [])]} />
          <span className="hint" aria-live="polite">{filledFrom
            ? donorGone
              ? `Filled from ${filledFrom.name}, which was deleted; the copied fields stay. Enter the password.`
              : `Filled from ${filledFrom.name}${filledFrom.raised ? "; TLS set to Required, which clone needs" : ""}. Enter the password.`
            : "Fills host, port, user and TLS, never a password."}</span>
        </label>}
        <FormField label="Donor host" value={donor.host} onChange={(v) => setDonor({ ...donor, host: v })} />
        <label className="ops-field"><span>Donor port</span><NumberField min={1} max={65535} value={donor.port} onChange={(v) => setDonor({ ...donor, port: +v })} /></label>
        <FormField label="Donor user" value={donor.user} onChange={(v) => setDonor({ ...donor, user: v })} />
        <FormField label="Donor password" type="password" value={donor.password || ""} onChange={(v) => setDonor({ ...donor, password: v })} hint="Kept in this page only, never saved." />
        <FormField label="Donor address as the recipient reaches it" value={donorAddress} onChange={setDonorAddress} placeholder="host:port" hint="Only when it differs from the host and port above." />
        <label className="ops-field"><span>Mode</span><Select label="Mode" value={cloneMode} options={[["in_place", "Replace the recipient's data"], ["directory", "Into a new data directory"]]} onChange={(v) => setCloneMode(v === "directory" ? "directory" : "in_place")} /></label>
        {cloneMode === "directory" ? <FormField wide label="Data directory" value={dataDirectory} onChange={setDataDirectory} placeholder="/var/lib/mysql-clone" hint="Absolute path on the recipient host. It must not exist and must be outside its data directory." /> : <p className="ops-warning">Replacing the recipient's data drops all its user data and restarts the server. This cannot be undone.</p>}
        <details><summary>Donor TLS</summary><label className="ops-field"><span>TLS mode</span><Select label="TLS mode" value={donor.ssl_mode || "REQUIRED"} options={[["REQUIRED", "Required"], ["VERIFY_CA", "Verify CA"], ["VERIFY_IDENTITY", "Verify identity"]]} onChange={(v) => setDonor({ ...donor, ssl_mode: v })} /></label><FormField label="CA file" value={donor.ssl_ca || ""} onChange={(v) => setDonor({ ...donor, ssl_ca: v })} /></details>
      </>))}
      </div>
      {operation && tools && (operation !== "clone" || tools.offered.clone) && <div className="ops-actions"><ReadAction busy={preparing} busyLabel="Checking…" action={operation === "clone" ? "clone_recipient" : operation === "restore" ? "restore" : undefined} gate={operationGate} className="primary" disabled={formDisabled || detecting || !canPrepare} blocked={blocked || privilegeBlock} onClick={() => { void prepare(); }}>{`Start ${operation}…`}</ReadAction>{readStatus("preflight")}{activeJob ? <JobSummary job={activeJob.report} onShow={() => { onSection(activeJob.report.operation); setFocusJob(activeJob.report.id || ""); }} /> : operation === "dump" && noDumpTool ? <span className="hint">{noToolSentence("dump")}</span> : operation === "restore" && noLoadTool ? <span className="hint">{noToolSentence("load")}</span> : !blocked && !privilegeBlock && <span className="hint">Checks first (reads only), then shows the exact commands for you to confirm.</span>}</div>}
      {notice && <p className="ops-note" role="status">{notice}</p>}
      <section ref={resultRef} className={`ops-preflight-result${preflightError || failedChecks.length ? " has-result" : ""}`} role="alert" tabIndex={-1}>
        {preflightError && <><p>Start {operation} is blocked: {preflightError} Fix the fields, then click Start {operation}… again.</p><button type="button" className="text-button" onClick={() => focusFix({ check: "input", status: "failed", reason: preflightError })}>Review fields</button></>}
        {shownPlan && !shownPlan.report.ready && <>
          {!planMatches && <p className="hint">From the last preflight; settings changed since. Start again to check the new settings.</p>}
          <p>{planMatches ? `Start ${operation} is blocked by` : "The last preflight found"} {plural(failedChecks.length, "failed check")}: {failedChecks.map(c => checkLabel(c.check)).join(", ")}. Fix {failedChecks.length === 1 ? "it" : "them"}, then click Start {operation}… again.</p>
          <h3>Fix before the {operation} can start ({failedChecks.length})</h3>
          <Checks checks={shownPlan.report.checks} selection={selection} fix={fixCheck} />
        </>}
      </section>
      {shownPlan && planMatches && <details className="ops-details"><summary>Planned commands and warnings</summary><Commands label="Planned commands" text={shownPlan.report.commands.join("\n")} />{shownPlan.report.warnings.map((line, i) => <p key={i}>{line}</p>)}</details>}

    </ViewLayout>
    {sheet && !startLeft && <ConfirmSheet className="ops-confirm"
      // Dump keeps the risky policy, review and audit gates, with a primary
      // confirmation because its destination must be absent or empty.
      confirmTone={sheet.report.operation === "dump" ? "primary" : "danger"}
      band={<ConnectionTypeBand connection={pConnection(sheet)} label={sheet.label} />}
      eyebrow={sheet.report.operation.toUpperCase()}
      title={sheet.report.operation === "dump" ? `Dump into the folder ${folderName(sheet.report.target)}?` : sheet.report.operation === "restore" ? `Restore into ${sheet.report.target}?` : `Clone into ${sheet.report.target}?`}
      description={sheet.report.operation === "dump" ? `Reads the server and writes only into this folder.${sheet.report.tool.name === "mysqlsh" ? " MySQL Shell briefly takes a global read lock." : ""}` : "This changes the server and cannot be undone."}
      busy={busy} busyLabel="Starting…" progress="Waiting for the server to start the job" onLeave={() => setStartLeft(true)}
      leaveText="Close keeps the Start request running. The job or its unknown outcome appears in this view; do not start it again."
      confirmLabel={<VisiblePrivilegeConfirmLabel gate={{ ...sheetGate, disabledReason: sheetBlock }}>{`Start ${sheet.report.operation}`}</VisiblePrivilegeConfirmLabel>}
      confirmDisabled={sheet.key !== key || (sheet.report.operation !== "dump" && typed !== sheet.report.target) || (!!sheet.report.required_acknowledgement && !acknowledged)}
      onCancel={() => { decline(sheet); setSheet(null); sheetConnection.current = null; setTyped(""); setAcknowledged(false); }} onConfirm={() => { void start(); }}>
      <div className="ops-target"><span>{sheet.report.operation === "dump" ? "Folder" : "Target"}</span><strong>{sheet.report.target}</strong></div>
      {sheet.report.operation === "dump" && planMessages(sheet.report).warnings.map((line, i) => <p className="ops-warning" key={i}>{line}</p>)}
      <Fields rows={[
        ...(sheet.report.operation === "dump" ? [["Contents", dumpContents(sheet.report)] as [string, string]] : []),
        ["Connection", sheet.label],
        ...(sheet.report.operation === "clone" && sheet.report.expect ? [
          ["Donor", sheet.report.expect.donor.endpoint],
          ["Donor type", `${TYPE_LABEL[sheet.report.expect.donor.type]}${sheet.report.expect.donor.read_only ? ", read-only" : ""}`],
          ["Recipient", sheet.report.expect.recipient.endpoint],
          ["Recipient type", `${TYPE_LABEL[sheet.report.expect.recipient.type]}${sheet.report.expect.recipient.read_only ? ", read-only" : ""}`],
          ["Donor address used by recipient", sheet.report.expect.donor_address],
        ] as [string, string][] : []),
        ["Server", engineName(sheet.report.server.engine, sheet.report.server.version, sheet.report.server.product)],
        !!sheet.report.server.identity?.hostname && ["Host name", String(sheet.report.server.identity.hostname)],
        ["Tool", <span className="ops-tool-line">{planToolLine(sheet.report).line}{sheet.report.tool.path ? <><br /><code className="client-path hint">{sheet.report.tool.path}</code></> : null}</span>],
        ["Preflight", `${plural(sheet.report.checks.filter((c) => c.status === "passed").length, "check")} passed${sheet.report.checks.some((c) => c.status === "not_checked") ? `, ${sheet.report.checks.filter((c) => c.status === "not_checked").length.toLocaleString()} not checked` : ""}`],
      ]} />
      {sheet.report.tool.warning && <p className="ops-warning">{sheet.report.tool.warning}</p>}
      {sheet.report.policy?.text && <p>{sheet.report.policy.text}</p>}
      {sheet.report.operation !== "dump" && sheet.report.warnings.map((line, i) => <p className="ops-warning" key={i}>{line}</p>)}
      {!!planMessages(sheet.report).notes.length && <ul className="hint">{planMessages(sheet.report).notes.map((line, i) => <li key={i}>{line}</li>)}</ul>}
      <Commands label="Exact commands" text={sheet.report.commands.join("\n")} />
      <details><summary>Preflight checks</summary><ul className="ops-verification">{sheet.report.checks.map((check, i) => <li key={i}><StatusPill tone={checkTone(check)} label={checkWord(check)} /><strong>{check.check}</strong><span>{check.reason}</span></li>)}</ul></details>
      {sheet.report.operation !== "dump" && <FormField label={`Type ${sheet.report.target} to confirm`} value={typed} onChange={setTyped} disabled={busy} />}
      {sheet.report.required_acknowledgement && <div className="ops-system-ack">
        <p className="ops-warning">This SQL backup replaces the mysql system schema, including accounts, passwords and grants. Accounts absent from the backup can be dropped, including the account this connection uses.</p>
        <Checkbox checked={acknowledged} disabled={busy} onChange={setAcknowledged}>I acknowledge replacing mysql system tables and accounts on {sheet.report.target}.</Checkbox>
      </div>}
      <p className="hint">myflames repeats the preflight right before starting and stops if anything changed. Cancelling a running job may leave partial data to clean up.</p>
    </ConfirmSheet>}
    {clientToolsOpen && (
      <ClientToolsDialog
        returnFocus={toolsReturnFocus}
        onClose={() => setClientToolsOpen(false)}
        onChanged={() => { void loadTools(); }}
      />
    )}
  </>;
}

const INSTALL_HINT: Record<string, string> = { high: "", medium: " (likely)", low: " (uncertain)" };

/** Where the target list came from, how the server was installed and what that means for the upgrade. */
function UpgradeTargetNotes({ report, chosen, loading, error }: { report: UpgradeTargets | null; chosen?: UpgradeTarget; loading: boolean; error: string }) {
  if (loading && !report) return <p className="hint ops-targets" role="status">Reading the releases this server can upgrade to…</p>;
  if (!report) return error ? <p className="hint ops-targets">Could not list target versions ({error}). Choose Other version… to type one.</p> : null;
  const { install, source, guidance } = report;
  // Only a Percona line name (ps-84-lts) fills the placeholder; a function replacer keeps "$" literal.
  const repository = chosen?.repository && /^ps-[0-9a-z]{2}(?:-lts|-innovation)?$/.test(chosen.repository) ? chosen.repository : "";
  const command = !guidance.command ? "" : guidance.command.includes("{repository}")
    ? (repository ? guidance.command.replace("{repository}", () => repository) : "") : guidance.command;
  const read = source.checked_at ? ` · read ${source.checked_at.slice(11, 16)} UTC` : "";
  const releaseNotes = upgradeLinkUrl(chosen?.url);
  const guide = upgradeLinkUrl(guidance.url);
  return <div className="ops-targets">
    <p className="hint">
      <strong>Installed from: {install.label}{INSTALL_HINT[install.confidence]}</strong>
      {" · "}{source.label}{source.kind !== "none" && `${read} · ${report.targets.length.toLocaleString()} ${report.targets.length === 1 ? "release" : "releases"} offered`}
      {source.error && source.kind !== "bundled" ? ` (partly read: ${source.error})` : ""}
    </p>
    {guidance.text && <p className="hint">{guidance.text}{guide && <> <a href={guide} target="_blank" rel="noopener noreferrer">{guide.includes("percona") ? "percona-release guide" : "Repository guide"}</a></>}</p>}
    {command && <Commands text={command} label="Repository setup command" />}
    {chosen && <p className="hint">
      {chosen.version}{chosen.date ? ` (${chosen.date})` : ""}: {chosen.path_label}{chosen.docker_only ? " · Docker image only" : ""}{chosen.packages?.length ? ` · packages for ${chosen.packages.join(", ")}` : ""}
      {chosen.shell && !chosen.shell.ok ? ` · needs MySQL Shell ${chosen.shell.needs || "newer than the server"} or newer for the full check` : ""}
      {releaseNotes && <> · <a href={releaseNotes} target="_blank" rel="noopener noreferrer">Release notes</a></>}
    </p>}
    {report.notes.map((note, i) => <p key={i} className="hint">{note}</p>)}
    {(install.evidence.length > 0 || report.not_direct.length > 0 || report.unclassified.length > 0 || (report.unavailable || []).length > 0) && <details className="ops-details">
      <summary>How this was decided</summary>
      <ul className="ops-evidence">
        {install.evidence.map((e, i) => <li key={`e${i}`}>{e}</li>)}
        {install.container && <li>The host name looks like a container ID.</li>}
        {[...report.not_direct, ...report.unclassified].map((n) => <li key={`n${n.series}`}>{n.series} (newest {n.newest}): {n.reason}</li>)}
        {(report.unavailable || []).map((u) => <li key={`u${u.version}`}>{u.version}: {u.reason}</li>)}
      </ul>
    </details>}
  </div>;
}
