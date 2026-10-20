// Server > Setup > Maintenance: detect tools, check an upgrade, and dump,
// restore or clone. The upgrade check and every preflight only read; each
// start needs an explicit confirmation that shows the exact commands (and the
// typed target for restore and clone). Job results are memory only and bound
// to the connection they ran on; the jobs themselves live in the myflames
// process, which lists them again after a reload or a lost Start reply.
import { useEffect, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { privilegeResult, targetPrivilege, usePrivilege, usePrivileges } from "./privileges";
import { download, recordDeclined, type Connection } from "./api";
import { connectionKey, connectionLabel, useConnectionReads, type Bound } from "./binding";
import { TYPE_LABEL, useWritePolicy } from "./Connections";
import { SchemaPicker, useSchemaCatalog } from "./PerfShared";
import {
  Checkbox, ConfirmSheet, DataTable, EmptyState, Fields, InspectorSection,
  NumberField, PrerequisiteGate, Segmented, Select, StatusPill, SummaryLine,
  ToolbarButton, ViewLayout, ViewStatus,
  type Column, type InspectorContent, type Selection, type Tone,
} from "./kit";
import { Commands, engineName, plural } from "./OpsShared";
import {
  cancelLifecycleJob, checkUpgrade, documentationUrl, jobActive, listLifecycleJobs,
  prepareLifecycle, readLifecycleJob, readLifecycleTools, startLifecycle, useOpsScope,
  type LifecycleJob, type LifecyclePlan, type LifecycleTools,
  type MissingPrivileges, type PrepareRequest, type Tool, type UpgradeFinding,
  type UpgradeReport, type Verification,
} from "./opsApi";
import { isPrivilegeDenial } from "./privileges";
import { VisiblePrivilegeGate, VisiblePrivilegeConfirmLabel } from "./Locks";
import "./ops.css";
import { DumpOptions } from "./DumpOptions";
import type { DumpOptions as DumpOptionValues } from "./opsApi";
import { FolderField } from "./FolderField";
import { toolDownloadUrl } from "./opsApi";

export type MaintenanceSection = "tools" | "upgrade" | "dump" | "restore" | "clone";
export type MaintenanceMemory = {
  tools: Bound<LifecycleTools> | null;
  upgrade: Bound<UpgradeReport> | null;
  jobs: Bound<LifecycleJob>[];
};
export const emptyMaintenance: MaintenanceMemory = { tools: null, upgrade: null, jobs: [] };
const SECTION_LABEL: Record<MaintenanceSection, string> = { tools: "Tools", upgrade: "Upgrade check", dump: "Dump", restore: "Restore", clone: "Clone" };
/** A preflight older than this is prepared again before anything starts. */
export const PREFLIGHT_TTL_MS = 60_000;
const JOB_POLL_MS = 1500;
/** Finished jobs kept per workspace (the server keeps 20 for an hour). */
const KEEP_FINISHED = 20;
const checkTone = (check: Verification): Tone => check.status === "passed" ? "ok" : check.status === "failed" ? "problem" : "unknown";
const checkWord = (check: Verification) => check.status === "passed" ? "Passed" : check.status === "failed" ? "Failed" : "Not checked";
const validInteger = (value: number, min: number, max: number) => Number.isInteger(value) && value >= min && value <= max;
const namedTables = (value: string) => value.split(",").map((name) => name.trim()).filter(Boolean);
const VERSION = /^\d+\.\d+\.\d+$/;

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
const folderName = (target: string) => target.replace(/\/+$/, "").split("/").pop() || target;
/** "mysqlsh 9.6.0" (the full --version banner is in the plan's tool). */
// A plan of another step (or one still loading) may carry no tool yet.
const toolName = (tool?: Tool) => (tool ? [tool.name, tool.version_tuple?.length ? tool.version_tuple.join(".") : tool.version].filter(Boolean).join(" ") : "") || "Not available";

function FormField({ label, value, onChange, disabled, type = "text", hint, wide, placeholder }: {
  label: string; value: string; onChange: (value: string) => void;
  disabled?: boolean; type?: "text" | "password"; hint?: string; wide?: boolean; placeholder?: string;
}) {
  return <label className={`ops-field${wide ? " ops-wide" : ""}`}><span>{label}</span><input type={type} value={value} disabled={disabled} placeholder={placeholder} spellCheck={false} autoComplete={type === "password" ? "new-password" : "off"} onChange={(e) => onChange(e.target.value)} />{hint && <span className="hint">{hint}</span>}</label>;
}
function Checks({ checks, selection }: { checks: Verification[]; selection?: Selection }) {
  const rows = checks.map((check, index) => ({ ...check, row: String(index) }));
  const columns: Column<(typeof rows)[number]>[] = [
    { key: "status", label: "Result", sort: (r) => (r.status === "failed" ? 0 : r.status === "not_checked" ? 1 : 2), render: (r) => <StatusPill tone={checkTone(r)} label={checkWord(r)} /> },
    { key: "check", label: "Check", primary: true, sort: (r) => r.check, render: (r) => r.check },
    { key: "reason", label: "Reason", flex: true, render: (r) => r.reason },
  ];
  return <DataTable id="maintenance.checks" label="Preflight checks" rows={rows} columns={columns} rowKey={(r) => `check:${r.row}`} selection={selection} defaultSort={{ key: "status", dir: "asc" }} />;
}

/** Job status is an outcome, not proof of verification: a tool that exited 0
 * can still leave checks not checked. */
function JobProgress({ bound, currentKey, cancelling, onCancel }: {
  bound: Bound<LifecycleJob>; currentKey: string; cancelling: string;
  onCancel: (bound: Bound<LifecycleJob>) => void;
}) {
  const job = bound.report;
  const other = bound.key !== currentKey;
  const active = jobActive(job);
  const [tone, word] = jobWord(job);
  const last = job.events.length ? job.events[job.events.length - 1].text : "";
  return <section className={`ops-job${other ? " is-other-connection" : ""}`} aria-label={`${SECTION_LABEL[job.operation]} on ${bound.label}`} data-job={job.id || undefined} data-state={job.state} data-other-connection={other || undefined}>
    <div className="ops-job-head"><strong>{SECTION_LABEL[job.operation]} · {bound.label}</strong><StatusPill tone={tone} label={word} />{active && <ToolbarButton disabled={other || !job.id || cancelling === job.id} title={other ? "Switch back to this connection to cancel it." : undefined} onClick={() => onCancel(bound)}>{cancelling === job.id ? "Cancelling…" : "Cancel"}</ToolbarButton>}</div>
    {other && <p className="hint">From {bound.label}. Switch back to that connection to manage this job.</p>}
    {active && <div className="ops-progress" role="status"><progress aria-label={`${SECTION_LABEL[job.operation]} progress`} max={100} value={job.progress ?? undefined} /><span>{job.progress === null ? word : `${job.progress.toFixed(0)}%`}</span></div>}
    {last && <p className="ops-log-line" title={last}>{last}</p>}
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

export function MaintenanceView({ connection, disabled, onBack, selection, section, onSection, memory, onMemory, targetVersions = [], missingPrivileges = {} }: {
  connection: Connection; disabled: boolean; onBack?: () => void; selection: Selection;
  section: MaintenanceSection; onSection: (section: MaintenanceSection) => void;
  /** Server owns memory; never serialize this object into the workspace. */
  memory: MaintenanceMemory; onMemory: Dispatch<SetStateAction<MaintenanceMemory>>;
  /** Optional version catalog supplied by the integrator. An exact custom
   * version is always available; the backend validates version/tool ranges. */
  targetVersions?: string[]; missingPrivileges?: MissingPrivileges;
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
  const [scope, setScope] = useState<"instance" | "schemas" | "tables">("instance");
  const [schemas, setSchemas] = useState<string[]>([]);
  const [tables, setTables] = useState("");
  const [directory, setDirectory] = useState("");
  const [restoreDirectory, setRestoreDirectory] = useState("");
  const [restoreSchema, setRestoreSchema] = useState("");
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
  const [cloneMode, setCloneMode] = useState<"in_place" | "directory">("in_place");
  const [dataDirectory, setDataDirectory] = useState("");
  const [prepared, setPrepared] = useState<Prepared | null>(null);
  const [sheet, setSheet] = useState<Prepared | null>(null);
  const sheetRef = useRef(sheet);
  sheetRef.current = sheet;
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const [cancelling, setCancelling] = useState("");
  const cancelPending = useRef(new Set<string>());
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
  const catalog = useSchemaCatalog(connection, section === "dump" && scope !== "instance");
  const tools = memory.tools?.key === key ? memory.tools.report : null;
  const dumpToolName = tools?.offered.dump.find(name => !sqlDump || name !== "mysqlsh");
  const dumpTool = dumpToolName ? tools?.tools[dumpToolName] : undefined;
  const dumpCapabilities = scope === "tables" ? dumpTool?.dump_table_capabilities : dumpTool?.dump_capabilities;
  const activeDumpOptions = Object.fromEntries(Object.entries(dumpOptions).filter(([name]) =>
    dumpCapabilities?.options.some(option => option.key === name) &&
    (name !== "compression_level" || ["gzip", "zstd"].includes(String(dumpOptions.compression))) &&
    (name !== "bytes_per_chunk" || dumpOptions.chunking !== false)));
  useEffect(() => { setDumpOptions({}); }, [key, dumpToolName]);
  const plan = prepared?.report;
  const upgrade = memory.upgrade?.report || null;
  const shownPlan = plan?.operation === section ? prepared : null;
  const shownUpgrade = section === "upgrade" ? memory.upgrade : null;
  const oldData = shownPlan || shownUpgrade || memory.tools;
  const other = oldData && oldData.key !== key ? oldData.label : null;
  const blocked = policy?.blocked || (connection.read_only ? "Read-only: myflames refuses every change on this connection." : "");
  const activeHere = memory.jobs.some((b) => b.key === key && jobActive(b.report));
  const formDisabled = disabled || busy || !!reads.loading || activeHere;
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
    const action = op === "restore" ? "restore" : ({ mysqlsh: "dump_shell", mysqldump: "dump_mysqldump", "mariadb-dump": "dump_mariadb" } as Record<string, string>)[p?.tool?.name || ""];
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
    setTyped(""); setNotice("");
    setDonor((c) => ({ ...c, password: "" }));
    setPrepared((p) => p ? { ...p, donor: undefined, inputKey: "" } : p);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  useEffect(() => () => { const p = sheetRef.current; if (p && !sending.current) decline(p); }, []);

  async function loadTools() {
    if (disabled || busy) return;
    await reads.run("maintenance-tools", "Detecting tools…", readLifecycleTools, (bound) => onMemory((m) => ({ ...m, tools: bound })));
  }
  async function runUpgrade() {
    const version = target === "custom" ? customTarget.trim() : target;
    if (formDisabled || !VERSION.test(version) || missingPrivileges.upgrade_check) return;
    setNotice(""); setFailure(null);
    selection.select("", false);
    await reads.run(`maintenance-upgrade:${JSON.stringify([version, preferShell])}`, "Checking the upgrade…", (c, signal) => checkUpgrade(c, version, preferShell, signal), (bound) => onMemory((m) => ({ ...m, upgrade: bound })));
  }
  function request(): PrepareRequest | null {
    if (section === "dump") return { operation: "dump", directory: directory.trim(), schemas: scope === "instance" ? [] : schemas, tables: scope === "tables" ? namedTables(tables) : [], threads, prefer_shell: !sqlDump, options: activeDumpOptions };
    if (section === "restore") return { operation: "restore", directory: restoreDirectory.trim(), schema: restoreSchema.trim(), existing, allow_source: allowSource, resume, threads };
    if (section === "clone") return { operation: "clone", donor: { ...donor }, donor_address: donorAddress.trim(), data_directory: cloneMode === "directory" ? dataDirectory.trim() : "", require_ssl: true };
    return null;
  }
  function requestKey() {
    const r = request();
    return JSON.stringify(r?.operation === "clone" ? { ...r, donor: { ...r.donor, password: undefined } } : r);
  }
  async function prepare() {
    if (formDisabled || blocked || privilegeBlock || !operation) return;
    const payload = request();
    if (!payload) return;
    setNotice(""); setFailure(null);
    const donorSnapshot = payload.operation === "clone" ? { ...payload.donor } : undefined;
    const from = { ...connection };
    // No secret in the shared read's parameter/cache key. A forced read
    // ensures that edits to the donor's transient password are never joined.
    const result = await reads.run("maintenance-prepare", "Running the preflight…", (c, signal) => prepareLifecycle(c, payload, signal), () => {}, { force: true });
    if (!result) return;
    const p: Prepared = { ...result, donor: donorSnapshot, at: Date.now(), inputKey: requestKey() };
    setPrepared(p);
    selection.select("", false);
    if (p.report.ready) { sheetConnection.current = from; setTyped(""); setSheet(p); }
    else setNotice("The preflight found problems, so nothing can start. Select a failed check for its reason, fix it, then run the preflight again.");
  }
  async function start() {
    const p = sheet;
    if (!p || sheetBlock || sending.current || p.key !== reads.currentKey() || !p.report.ready) return;
    if (p.report.operation !== "dump" && typed !== p.report.target) return;
    const live = captureScope();
    sending.current = true; setBusy(true); setFailure(null);
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
      const job = await startLifecycle(checked, p.report, p.report.operation === "dump" ? p.report.target : typed, p.donor);
      // An authorized job keeps its original binding even if the user
      // switched while start was in flight; keep its id for Cancel.
      denied(job.error, checked);
      rememberOwners([job], checked);
      publish.current((previous) => adoptJobs(previous, p.key, p.label, [job]));
      setSheet(null); sheetConnection.current = null;
      if (live()) {
        setPrepared(null);
        if (job.state === "refused") setFailure({ key: p.key, text: job.error || "myflames refused to start it. Nothing was started." });
        else setNotice(job.policy?.text || "");
        setPollRetry((n) => n + 1);
      }
    } catch (e) {
      setSheet(null); sheetConnection.current = null;
      denied(e, from);
      if (!sent) { if (live()) setFailure({ key: p.key, text: (e as Error).message }); return; }
      // The reply was lost or failed: the job may be running. Look for it
      // instead of starting again.
      let found: LifecycleJob | undefined;
      try {
        const answer = await listLifecycleJobs(from);
        found = answer.jobs.find((job) => job.id && !known.has(job.id) && job.operation === p.report.operation);
        if (found) {
          rememberOwners([found], from);
          publish.current((m) => adoptJobs(m, p.key, p.label, [found as LifecycleJob]));
        }
      } catch {
        found = undefined;
      }
      if (live()) {
        if (found) { setPrepared(null); setNotice(`The Start reply was lost, but the ${SECTION_LABEL[p.report.operation].toLowerCase()} is in myflames' job list and is shown below. It was not started again.`); }
        else setFailure({ key: p.key, text: `${(e as Error).message} Whether it started is unknown. Do not start it again before you check the target and the Audit log.` });
      }
    } finally { sending.current = false; setBusy(false); }
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
    } catch (e) { if (live()) setFailure({ key: bound.key, text: (e as Error).message }); }
    finally { cancelPending.current.delete(id); setCancelling((pending) => pending === id ? "" : pending); }
  }

  const toolRows = Object.entries(memory.tools?.report.tools || {}).map(([name, tool]) => ({ ...tool, name }));
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
  const toolColumns: Column<Tool & { name: string }>[] = [
    { key: "name", label: "Tool", primary: true, sort: (r) => r.name, render: (r) => r.name },
    { key: "status", label: "Status", sort: (r) => Number(r.available), render: (r) => <StatusPill tone={r.deprecated ? "unknown" : r.available ? "ok" : "unknown"} label={r.deprecated ? "Deprecated" : r.available ? "Available" : "Not found"} /> },
    { key: "version", label: "Version", flex: true, sort: (r) => (r.version_tuple || []).join("."), render: (r) => (r.version_tuple?.length ? <span title={r.version}>{r.version_tuple.join(".")}</span> : r.version || r.error || "Not available") },
    { key: "update", label: "Updates", render: (r) => <span title={r.update?.message}><StatusPill tone={r.update?.status === "available" ? "watch" : "unknown"} label={updateWord(r)} /></span> },
    { key: "download", label: "Download", render: (r) => toolDownloadUrl(r.download_url) ? <a className="ops-tool-link" href={toolDownloadUrl(r.download_url)} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} aria-label={`Official download page for ${r.name}`}>Official site</a> : "Not available" },
    { key: "engine", label: "Engine", render: (r) => (r.engine ? engineName(r.engine) : "Not available") },
  ];
  const findings = (upgrade?.checks || []).map((finding, index) => ({ ...finding, row: String(index) }));
  const findingColumns: Column<(typeof findings)[number]>[] = [
    { key: "result", label: "Result", sort: (r) => findingResult(r)[2], render: (r) => { const [tone, word] = findingResult(r); return <StatusPill tone={tone} label={word} />; } },
    { key: "check", label: "Check", primary: true, flex: true, sort: (r) => r.title, render: (r) => r.title },
    { key: "objects", label: "Objects", num: true, sort: (r) => r.affected_objects.length, render: (r) => <span title={r.affected_objects.join(", ") || undefined}>{r.affected_objects.length.toLocaleString()}</span> },
  ];
  const selectedTool = section === "tools" ? toolRows.find((t) => `tool:${t.name}` === selection.key) : undefined;
  const selectedFinding = section === "upgrade" ? findings.find((f) => `finding:${f.row}` === selection.key) : undefined;
  const selectedCheck = operation ? shownPlan?.report.checks.find((_, i) => `check:${i}` === selection.key) : undefined;
  let inspector: InspectorContent | null = null;
  if (selectedTool) inspector = {
    key: `tool:${selectedTool.name}`, title: selectedTool.name,
    meta: <StatusPill tone={selectedTool.deprecated ? "unknown" : selectedTool.available ? "ok" : "unknown"} label={selectedTool.deprecated ? "Deprecated" : selectedTool.available ? "Available" : "Not found"} />,
    body: <InspectorSection title="Detected tool"><Fields rows={[["Path", selectedTool.path || "Not found"], ["Version", selectedTool.version || "Not available"], ["Engine", selectedTool.engine ? engineName(selectedTool.engine) : "Not available"], !!selectedTool.error && ["Probe", selectedTool.error]]} />{selectedTool.deprecated && <p>Deprecated tools are listed for information and never offered.</p>}
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
  if (selectedCheck) inspector = { key: selection.key, title: selectedCheck.check, meta: <StatusPill tone={checkTone(selectedCheck)} label={checkWord(selectedCheck)} />, body: <InspectorSection title="Preflight check"><p>{selectedCheck.reason}</p><details className="ops-raw"><summary>Details</summary><pre className="ops-pre">{JSON.stringify(selectedCheck, null, 2)}</pre></details></InspectorSection> };

  const shellVersion = tools?.tools.mysqlsh?.available ? tools.tools.mysqlsh.version_tuple?.join(".") || "" : "";
  const versions = [...new Set([...targetVersions, shellVersion].filter((v) => VERSION.test(v)))];
  const version = target === "custom" ? customTarget.trim() : target;
  const dumpValid = !!directory.trim() && validInteger(threads, 1, 64) && (scope === "instance" || schemas.length > 0) && (scope !== "tables" || (schemas.length === 1 && namedTables(tables).length > 0));
  const restoreValid = !!restoreDirectory.trim() && validInteger(threads, 1, 64);
  const cloneValid = !!donor.host.trim() && !!donor.user.trim() && validInteger(donor.port, 1, 65535) && (cloneMode === "in_place" || dataDirectory.startsWith("/"));
  const canPrepare = operation === "dump" ? dumpValid && !!tools?.offered.dump.some((name) => !sqlDump || name !== "mysqlsh") : operation === "restore" ? restoreValid && !!tools : operation === "clone" ? cloneValid && !!tools?.offered.clone : false;
  const controls = <>
    <Segmented label="Maintenance task" value={section} options={(Object.keys(SECTION_LABEL) as MaintenanceSection[]).filter(value => value !== "clone" || !tools || tools.offered.clone).map((value) => ({ value, label: SECTION_LABEL[value], disabled: busy }))} onChange={(next) => { selection.select("", false); setNotice(""); onSection(next); }} />
  </>;
  const form = (children: ReactNode) => <div className="ops-form"><fieldset disabled={formDisabled}>{children}</fieldset></div>;
  const hereJobs = memory.jobs.filter((b) => b.report.operation === section);
  const statusError = failure?.key === key ? failure.text : reads.error;
  const counts = upgrade?.counts;
  // A partial check never reads as a green light: with nothing found it says Partial.
  const upgradeTone: Tone | undefined = !upgrade || upgrade.method === "mariadb_upgrade_guidance" ? undefined : counts?.error ? "problem" : counts?.warning ? "watch" : upgrade.partial ? "unknown" : "ok";
  const upgradeToneLabel = upgradeTone === "unknown" ? "Partial" : upgradeTone === "ok" ? "No findings" : undefined;
  const kindOfCheck = upgrade ? (upgrade.method === "mariadb_upgrade_guidance" ? "mariadb-upgrade steps" : upgrade.partial ? "Partial check (native subset)" : "MySQL Shell") : "";
  const summary = section === "upgrade" && upgrade
    ? [
        `${engineName(upgrade.server.engine, upgrade.server.version)} to ${upgrade.target_version}`,
        kindOfCheck,
        upgrade.method === "mariadb_upgrade_guidance" ? "" : [plural(counts?.error || 0, "error"), plural(counts?.warning || 0, "warning"), plural(counts?.notice || 0, "notice")].join(", "),
      ].filter(Boolean).join(" · ")
    : tools
      ? `${engineName(tools.server.engine, tools.server.version)} · ${SECTION_LABEL[section]}`
      : "";
  const upgradeNotes = section === "upgrade" && upgrade ? upgrade.limitations : undefined;
  return <>
    <ViewLayout id="maintenance" title="Maintenance" className="ops-view" onBack={onBack} controls={controls} onRefresh={() => { void loadTools(); }} refreshDisabled={disabled || busy || !!reads.loading} loading={!!reads.loading} inspector={inspector} otherConnection={other}
      inspectorEmpty={<p>Select a tool, a finding or a check to see its details here.</p>}
      info={<><p>Detect the MySQL and MariaDB tools on this computer, check an upgrade, or dump, restore or clone the connection in use.</p><p>The upgrade check and every preflight only read. A dump, restore or clone starts only after you review its exact commands and confirm (restore and clone also ask you to type the target), and it is recorded in the Audit log.</p></>}
      status={<><ViewStatus loading={reads.loading} error={statusError}>{failure?.key === key && failure.poll && <button className="text-button" type="button" onClick={() => { setFailure(null); setPollRetry((n) => n + 1); }}>Retry status</button>}</ViewStatus>{hereJobs.map((bound, i) => <JobProgress key={`${bound.key}:${bound.report.id || i}`} bound={bound} currentKey={key} cancelling={cancelling} onCancel={(b) => { void cancel(b); }} />)}</>}>
      {summary && (
        <SummaryLine tone={section === "upgrade" ? upgradeTone : undefined} toneLabel={section === "upgrade" ? upgradeToneLabel : undefined} notes={upgradeNotes}
          extra={section === "upgrade" && upgrade ? <ToolbarButton onClick={() => download(JSON.stringify(upgrade, null, 2), "myflames-upgrade-check.json", "application/json")}>Export JSON</ToolbarButton> : undefined}>
          {summary}
        </SummaryLine>
      )}
      {!tools && section !== "tools" && <PrerequisiteGate action={<ToolbarButton disabled={disabled || !!reads.loading} onClick={() => { void loadTools(); }}>Detect tools</ToolbarButton>}>Detect the tools for this connection first.</PrerequisiteGate>}
      {section === "tools" && (memory.tools ? <>
        {!!availableUpdates.length && <div className="ops-tool-updates" role="status">Updates available for {availableUpdates.map(t => t.name).join(", ")}.</div>}
        <DataTable id="maintenance.tools" label="Detected tools" rows={toolRows} columns={toolColumns} rowKey={(r) => `tool:${r.name}`} selection={selection} />
        <div className="ops-offered"><Fields label="Offered for this server" rows={[["Dump", memory.tools.report.offered.dump.join(", ") || "No dump tool for this engine"], ["Upgrade check", memory.tools.report.offered.upgrade === "mysqlsh" ? "MySQL Shell (full check)" : memory.tools.report.offered.upgrade === "native_subset" ? "Partial check (native subset)" : "mariadb-upgrade steps to run on the server host"], ["Clone", memory.tools.report.offered.clone ? "Offered after its preflight" : "Not offered"], ["Physical backup", memory.tools.report.offered.physical_backup ? "Detected; runs on the server host" : "Not available"]]} /></div>
      </> : <EmptyState icon="maintenance" title="Detect maintenance tools" action={<ToolbarButton primary disabled={disabled || !!reads.loading} onClick={() => { void loadTools(); }}>Detect tools</ToolbarButton>}>Find installed tools and check official release pages for updates. No connection details are sent to those sites.</EmptyState>)}
      {section === "upgrade" && tools && <>
        {form(<>
          <label className="ops-field"><span>Target version</span><Select label="Target version" value={target} options={[["", "Choose a version"], ...versions.map((v): [string, string] => [v, v]), ["custom", "Other version…"]]} onChange={setTarget} /></label>
          {target === "custom" && <FormField label="Exact version" value={customTarget} onChange={setCustomTarget} placeholder="8.4.0" hint="major.minor.patch" />}
          <Checkbox checked={!preferShell} onChange={(v) => setPreferShell(!v)}>Partial check without MySQL Shell (native subset)</Checkbox>
        </>)}
        <div className="ops-actions"><VisiblePrivilegeGate gate={privilegeResult()} write={false} className="primary" disabled={formDisabled || !VERSION.test(version)} blocked={missingPrivileges.upgrade_check} onClick={() => { void runUpgrade(); }}>Check upgrade</VisiblePrivilegeGate><span className="hint">Only reads. Never says an upgrade is safe.</span></div>
        {upgrade && <>
          {upgrade.method === "mariadb_upgrade_guidance" ? <div className="ops-result">{upgrade.limitations.map((line, i) => <p key={i}>{line}</p>)}</div> : <DataTable id="maintenance.upgrade" label="Upgrade findings" columns={findingColumns} rows={findings} rowKey={(r) => `finding:${r.row}`} selection={selection} defaultSort={{ key: "result", dir: "desc" }} />}
          {upgrade.manual_checks.length > 0 && <details className="ops-details"><summary>Manual checks ({upgrade.manual_checks.length.toLocaleString()})</summary><pre className="ops-pre">{JSON.stringify(upgrade.manual_checks, null, 2)}</pre></details>}
        </>}
      </>}
      {section === "dump" && tools && form(<>
        <label className="ops-field"><span>Scope</span><Select label="Scope" value={scope} options={[["instance", "Whole instance"], ["schemas", "Databases"], ["tables", "Tables of one database"]]} onChange={(v) => { if (v === "instance" || v === "schemas" || v === "tables") setScope(v); }} /></label>
        {scope !== "instance" && <div className="ops-field"><SchemaPicker id="ops-dump-schemas" value={schemas} onChange={setSchemas} catalog={catalog.catalog} error={catalog.error} loading={catalog.loading} onReload={catalog.reload} disabled={formDisabled} />{!schemas.length && <span className="hint">Choose {scope === "tables" ? "one database" : "at least one database"}.</span>}</div>}
        {scope === "tables" && <FormField label="Tables" value={tables} onChange={setTables} hint="Names separated by commas." />}
        <label className="ops-field"><span>Tool</span><Select label="Tool" value={sqlDump ? "sql" : "auto"} options={[["auto", "Automatic (MySQL Shell first)"], ["sql", "SQL dump (mysqldump or mariadb-dump)"]]} onChange={(v) => setSqlDump(v === "sql")} /></label>
        <FolderField key={`dump:${key}`} label="Output folder" value={directory} onChange={setDirectory} disabled={formDisabled} hint="On this computer. Choose an empty folder, or enter a new folder path." />
        <details className="ops-dump-options"><summary>Options{dumpToolName ? ` for ${dumpToolName}` : ""}</summary>
          {dumpToolName === "mysqlsh" && <label className="ops-field"><span>Threads</span><NumberField min={1} max={64} value={threads} onChange={(v) => setThreads(+v)} /></label>}
          {dumpCapabilities && <DumpOptions catalog={dumpCapabilities} value={dumpOptions} onChange={setDumpOptions} />}
        </details>
      </>)}
      {section === "restore" && tools && form(<>
        <div className="ops-target"><span>Restores into</span><strong>{connectionLabel(connection)}</strong></div>
        <FolderField key={`restore:${key}`} label="Dump folder" value={restoreDirectory} onChange={setRestoreDirectory} disabled={formDisabled} hint="A completed myflames dump with its checksummed manifest." />
        <details><summary>Options</summary>
          <FormField label="Default database" value={restoreSchema} onChange={setRestoreSchema} hint="SQL dumps can name other databases; this does not confine the restore." />
          <label className="ops-field"><span>Existing objects</span><Select label="Existing objects" value={existing} options={[["refuse", "Stop if any exist"], ["ignore", "Skip existing (MySQL Shell only)"], ["drop", "Drop and recreate"]]} onChange={(v) => { if (v === "refuse" || v === "ignore" || v === "drop") setExisting(v); }} /></label>
          <Checkbox checked={allowSource} onChange={setAllowSource}>Allow restoring into the server the dump came from</Checkbox>
          <Checkbox checked={resume} onChange={setResume}>Resume an interrupted MySQL Shell load</Checkbox>
          <label className="ops-field"><span>Threads</span><NumberField min={1} max={64} value={threads} onChange={(v) => setThreads(+v)} /></label>
        </details>
      </>)}
      {section === "clone" && tools && (!tools.offered.clone ? <PrerequisiteGate>{tools.server.engine === "mariadb" ? "MariaDB has no clone plugin: copy a server with mariadb-backup on its host instead." : "Clone is not offered for this server version."}</PrerequisiteGate> : form(<>
        <div className="ops-target"><span>Recipient (the connection in use)</span><strong>{connectionLabel(connection)}</strong></div>
        <FormField label="Donor host" value={donor.host} onChange={(v) => setDonor({ ...donor, host: v })} />
        <label className="ops-field"><span>Donor port</span><NumberField min={1} max={65535} value={donor.port} onChange={(v) => setDonor({ ...donor, port: +v })} /></label>
        <FormField label="Donor user" value={donor.user} onChange={(v) => setDonor({ ...donor, user: v })} />
        <FormField label="Donor password" type="password" value={donor.password || ""} onChange={(v) => setDonor({ ...donor, password: v })} hint="Kept in this page only, never saved." />
        <FormField label="Donor address as the recipient reaches it" value={donorAddress} onChange={setDonorAddress} placeholder="host:port" hint="Only when it differs from the host and port above." />
        <label className="ops-field"><span>Mode</span><Select label="Mode" value={cloneMode} options={[["in_place", "Replace the recipient's data"], ["directory", "Into a new data directory"]]} onChange={(v) => setCloneMode(v === "directory" ? "directory" : "in_place")} /></label>
        {cloneMode === "directory" ? <FormField wide label="Data directory" value={dataDirectory} onChange={setDataDirectory} placeholder="/var/lib/mysql-clone" hint="Absolute path on the recipient host. It must not exist and must be outside its data directory." /> : <p className="ops-warning">Replacing the recipient's data drops all its user data and restarts the server. This cannot be undone.</p>}
        <details><summary>Donor TLS</summary><label className="ops-field"><span>TLS mode</span><Select label="TLS mode" value={donor.ssl_mode || "REQUIRED"} options={[["REQUIRED", "Required"], ["VERIFY_CA", "Verify CA"], ["VERIFY_IDENTITY", "Verify identity"]]} onChange={(v) => setDonor({ ...donor, ssl_mode: v })} /></label><FormField label="CA file" value={donor.ssl_ca || ""} onChange={(v) => setDonor({ ...donor, ssl_ca: v })} /></details>
      </>))}
      {operation && tools && (operation !== "clone" || tools.offered.clone) && <div className="ops-actions"><VisiblePrivilegeGate action={operation === "clone" ? "clone_recipient" : operation === "restore" ? "restore" : undefined} gate={operationGate} className="primary" disabled={formDisabled || !canPrepare} blocked={blocked || privilegeBlock} onClick={() => { void prepare(); }}>Run preflight…</VisiblePrivilegeGate>{!blocked && !privilegeBlock && <span className="hint">{activeHere ? "A job is running on this connection." : "Only reads; the confirmation comes next."}</span>}</div>}
      {notice && <p className="ops-note" role="status">{notice}</p>}
      {shownPlan && !shownPlan.report.ready && <Checks checks={shownPlan.report.checks} selection={selection} />}
      {shownPlan && <details className="ops-details"><summary>Planned commands and warnings</summary><Commands label="Planned commands" text={shownPlan.report.commands.join("\n")} />{shownPlan.report.warnings.map((line, i) => <p key={i}>{line}</p>)}</details>}

    </ViewLayout>
    {sheet && <ConfirmSheet className="ops-confirm" eyebrow={sheet.report.operation.toUpperCase()}
      title={sheet.report.operation === "dump" ? `Dump into the folder ${folderName(sheet.report.target)}?` : sheet.report.operation === "restore" ? `Restore into ${sheet.report.target}?` : `Clone into ${sheet.report.target}?`}
      description={sheet.report.operation === "dump" ? "The dump reads the server and writes only into the folder." : "This changes the server and cannot be undone."}
      busy={busy} confirmLabel={<VisiblePrivilegeConfirmLabel gate={{ ...sheetGate, disabledReason: sheetBlock }}>{busy ? "Starting…" : `Start ${sheet.report.operation}`}</VisiblePrivilegeConfirmLabel>}
      confirmDisabled={sheet.key !== key || (sheet.report.operation !== "dump" && typed !== sheet.report.target)}
      onCancel={() => { decline(sheet); setSheet(null); sheetConnection.current = null; setTyped(""); }} onConfirm={() => { void start(); }}>
      <div className="ops-target"><span>{sheet.report.operation === "dump" ? "Folder" : "Target"}</span><strong>{sheet.report.target}</strong></div>
      <Fields rows={[
        ["Connection", sheet.label],
        ...(sheet.report.operation === "clone" && sheet.report.expect ? [
          ["Donor", sheet.report.expect.donor.endpoint],
          ["Donor type", `${TYPE_LABEL[sheet.report.expect.donor.type]}${sheet.report.expect.donor.read_only ? ", read-only" : ""}`],
          ["Recipient", sheet.report.expect.recipient.endpoint],
          ["Recipient type", `${TYPE_LABEL[sheet.report.expect.recipient.type]}${sheet.report.expect.recipient.read_only ? ", read-only" : ""}`],
          ["Donor address used by recipient", sheet.report.expect.donor_address],
        ] as [string, string][] : []),
        ["Server", engineName(sheet.report.server.engine, sheet.report.server.version)],
        !!sheet.report.server.identity?.hostname && ["Host name", String(sheet.report.server.identity.hostname)],
        ["Tool", toolName(sheet.report.tool)],
        ["Preflight", `${plural(sheet.report.checks.filter((c) => c.status === "passed").length, "check")} passed${sheet.report.checks.some((c) => c.status === "not_checked") ? `, ${sheet.report.checks.filter((c) => c.status === "not_checked").length.toLocaleString()} not checked` : ""}`],
      ]} />
      {sheet.report.policy?.text && <p>{sheet.report.policy.text}</p>}
      {sheet.report.warnings.map((line, i) => <p className="ops-warning" key={i}>{line}</p>)}
      <Commands label="Exact commands" text={sheet.report.commands.join("\n")} />
      <details><summary>Preflight checks</summary><ul className="ops-verification">{sheet.report.checks.map((check, i) => <li key={i}><StatusPill tone={checkTone(check)} label={checkWord(check)} /><strong>{check.check}</strong><span>{check.reason}</span></li>)}</ul></details>
      {sheet.report.operation !== "dump" && <FormField label={`Type ${sheet.report.target} to confirm`} value={typed} onChange={setTyped} disabled={busy} />}
      <p className="hint">myflames repeats the preflight right before starting and stops if anything changed. Cancelling a running job may leave partial data to clean up.</p>
    </ConfirmSheet>}
  </>;
}
