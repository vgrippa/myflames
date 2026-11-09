// Import CSV: one wizard sheet in five steps (File and format, Target and
// columns, Check, Review, Import), opened from the Schemas panel and from
// Server > Tables with the target filled in. It never writes before the
// Review step's Import, which sends the reviewed plan's token once.
//
// - File and format: the native chooser (or a typed path), the separator,
//   header row, encoding, quote, empty fields and skipped lines, detected
//   values chosen and named, and a preview read again after every change.
// - Target and columns: an existing table (each CSV column to a table column,
//   matched by header name, then position) or a new table (names and types
//   from the file, editable; an id primary key).
// - Check: every row checked against the target (the plan; reads only), the
//   method (INSERT batches, or LOAD DATA LOCAL only when the server already
//   has local_infile ON), what happens on a failure and with bad rows.
// - Review: the connection band, the exact CREATE TABLE, the data summary and
//   the first statement, privileges and the write policy; Cancel is focused
//   and Cancel or Escape records a declined attempt in the Audit log.
// - Import: rows, batches and seconds with Stop; Close on a slow import keeps
//   the request, and its progress and outcome stay in the view (the line this
//   component draws where the owner placed it).
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { openAuditLog } from "./ActionReview";
import { api, ApiError, recordDeclined, type CatalogColumn, type CatalogTable, type CatalogTables, type Connection } from "./api";
import { connectionKey, connectionLabel } from "./binding";
import { ConnectionTypeBand, TYPE_LABEL, useWritePolicy } from "./Connections";
import {
  ACTION_SLOW_AFTER_MS, ActionButton, ActionStatus, Checkbox, Disclosure, Fields, IDLE_ACTION, NotesButton, NumberField, Segmented, Select,
  StatusPill, errorText, useElapsed, useFocusScope, writeFailure, type ActionState, type Tone,
} from "./kit";
import { CopySql, bytes, useSchemaCatalog } from "./PerfShared";
import {
  ENCODINGS, QUOTES, SEPARATORS, TYPE_CHOICES, applyCsvImport, chooseCsvFile, columnNames, csvImportProgress, encodingLabel, encodingValue,
  inspectCsv, matchColumns, nameProblem, openCsvPath, planCsvImport, progressId, separatorName, stopCsvImport, tableNameFromFile, typeProblem,
  type CsvChoice, type CsvFile, type CsvFormat, type CsvInspect, type CsvMethod, type CsvOnError, type CsvPlan, type CsvPlanRequest,
  type CsvProgress, type CsvResult, type CsvServerWarning, type ImportCsvSession,
} from "./importCsvApi";
import "./import-csv.css";

type Step = 1 | 2 | 3 | 4 | 5;
const STEPS = ["File and format", "Target and columns", "Check", "Review", "Import"] as const;
const POLL_MS = 500;
const OPERATION = "import_csv";
/** The words a field may equal to read as NULL (format.null_markers). */
const NULL_MARKERS: [string, string][] = [["\\N", "Treat \\N as NULL"], ["NULL", "Treat the word NULL as NULL"]];

/** A finished import as the view shows it: the server's result, or what the
 * workspace knows when nothing was sent or no reply arrived. */
type Outcome =
  | { kind: "result"; value: CsvResult }
  /** Refused before anything was sent; auditId only when a record names it. */
  | { kind: "refused"; message: string; auditId: string | null }
  | { kind: "unknown"; message: string };
type Run = {
  id: string;
  startedAt: number;
  plan: CsvPlan;
  progress: CsvProgress | null;
  outcome: Outcome | null;
  stopping: number | null;
  stopError: string;
};
type NewColumn = { include: boolean; name: string; type: string; custom: boolean; nullable: boolean };
type Loaded<T> = { key: string; loading: boolean; error: string; data: T | null };

const n = (value: number | null | undefined) => (value === null || value === undefined || !Number.isFinite(value) ? "—" : value.toLocaleString());
const plural = (value: number, one: string, many = `${one}s`) => `${value.toLocaleString()} ${value === 1 ? one : many}`;
function fileTime(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "";
  const date = typeof value === "number" ? new Date(value < 1e12 ? value * 1000 : value) : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
const qualified = (schema: string, table: string) => `${schema}.${table}`;
const formatKey = (f: CsvFormat | null) => (f ? JSON.stringify(f) : "");
const warningText = (w: CsvServerWarning) =>
  typeof w === "string" ? w : [w.level, w.code].filter((x) => x !== undefined && x !== null && x !== "").join(" ") + (w.message ? `: ${w.message}` : "");
function serverWarnings(value: CsvResult["warnings"]): { count: number; items: CsvServerWarning[] } {
  if (!value) return { count: 0, items: [] };
  if (Array.isArray(value)) return { count: value.length, items: value };
  return { count: value.count || 0, items: value.items || value.first || [] };
}
const serverErrorText = (e: CsvResult["server_error"]) =>
  !e ? "" : typeof e === "string" ? e : `${e.code ? `ERROR ${e.code}: ` : ""}${e.message}`;

const OUTCOME: Record<string, [Tone, string]> = {
  done: ["ok", "Done"],
  failed: ["problem", "Failed"],
  stopped: ["unknown", "Stopped"],
  unknown: ["watch", "Outcome unknown"],
  refused: ["unknown", "Nothing sent"],
};
function outcomeWord(o: Outcome): [Tone, string] {
  return o.kind === "result" ? OUTCOME[o.value.outcome] || OUTCOME.unknown : OUTCOME[o.kind];
}
const PHASE_TEXT: Record<string, string> = {
  received: "Checking the review and the write policy…",
  checking: "Checking the file and the table again before sending…",
  creating: "Creating the table…",
  sending: "Sending rows…",
  committing: "Committing…",
  rolling_back: "Rolling back…",
  finished: "Finishing…",
};

/** The wizard of one import (its owner keeps it mounted until dismissed). */
export function ImportCsv({ session, currentKey, show, onRunning, onEnd }: {
  session: ImportCsvSession;
  /** connectionKey of the connection in use now. */
  currentKey: string;
  /** Increased by the owner to show the sheet again. */
  show: number;
  onRunning: (running: boolean) => void;
  onEnd: () => void;
}) {
  const conn = session.connection;
  const key = connectionKey(conn);
  const label = connectionLabel(conn);
  const mismatch = currentKey !== key;
  const policy = useWritePolicy();
  const [open, setOpen] = useState(true);
  const [step, setStep] = useState<Step>(1);
  const dialog = useRef<HTMLDialogElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const doneButton = useRef<HTMLButtonElement>(null);
  const stepsRef = useRef<HTMLOListElement>(null);
  const titleId = useId();
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const firstShow = useRef(show);
  useEffect(() => { if (show !== firstShow.current) setOpen(true); }, [show]);

  // ---- Step 1: file and format ----------------------------------------------
  const [file, setFile] = useState<CsvFile | null>(null);
  const [inspect, setInspect] = useState<CsvInspect | null>(null);
  const [format, setFormat] = useState<CsvFormat | null>(null);
  const [sepMode, setSepMode] = useState<string>(",");
  const [otherSep, setOtherSep] = useState("");
  const [reading, setReading] = useState<ActionState>(IDLE_ACTION);
  const [choosing, setChoosing] = useState<ActionState>(IDLE_ACTION);
  const [pathShown, setPathShown] = useState(false);
  const [path, setPath] = useState("");
  const [opening, setOpening] = useState(false);
  const [pathError, setPathError] = useState("");
  const inspected = useRef("");
  const inspectSeq = useRef(0);
  const inspectCtl = useRef<AbortController | null>(null);
  const pathInput = useRef<HTMLInputElement>(null);
  const pathId = useId();

  async function runInspect(f: CsvFile, fmt: CsvFormat | null) {
    inspectCtl.current?.abort();
    const controller = new AbortController();
    inspectCtl.current = controller;
    const seq = ++inspectSeq.current;
    inspected.current = formatKey(fmt);
    setReading({ phase: "pending", message: fmt ? "Reading the file with this format…" : `Reading ${f.name} and detecting its format…`, startedAt: Date.now(), endedAt: null });
    try {
      const report = await inspectCsv(f.file_id, fmt, controller.signal);
      if (!alive.current || seq !== inspectSeq.current) return;
      setInspect(report);
      if (!fmt) {
        // Older replies may leave out the optional keys: their defaults.
        const effective = Object.assign({ escape: "", skip_lines: 0, empty: "null" }, report.format) as CsvFormat;
        inspected.current = formatKey(effective);
        setFormat(effective);
        const named = SEPARATORS.some((s) => s.value === report.format.separator);
        setSepMode(named ? report.format.separator : "other");
        setOtherSep(named ? "" : report.format.separator);
      }
      setReading(IDLE_ACTION);
    } catch (e) {
      if (!alive.current || seq !== inspectSeq.current || controller.signal.aborted) return;
      setReading({ phase: "failed", message: `Could not read the file: ${errorText(e)}`, startedAt: null, endedAt: Date.now() });
    }
  }
  function stopReading() {
    inspectCtl.current?.abort();
    inspectSeq.current += 1;
    setReading({ phase: "failed", message: inspect ? "Stopped reading. The preview shows the last format read." : "Stopped reading the file.", startedAt: null, endedAt: Date.now() });
  }
  function adopt(choice: CsvChoice) {
    if ("cancelled" in choice) return false;
    setFile(choice);
    setInspect(null);
    setFormat(null);
    // A new file suggests its own table name.
    setNewName(null);
    setPathShown(false);
    setPathError("");
    void runInspect(choice, null);
    return true;
  }
  async function chooseFile() {
    if (choosing.phase === "pending" || opening) return;
    setChoosing({ phase: "pending", message: "Choose a file in the system dialog.", startedAt: Date.now(), endedAt: null });
    try {
      const choice = await chooseCsvFile();
      if (!alive.current) return;
      setChoosing("cancelled" in choice
        ? { phase: "done", message: file ? `No other file chosen; ${file.name} stays.` : "No file chosen.", startedAt: null, endedAt: Date.now() }
        : IDLE_ACTION);
      adopt(choice);
    } catch (e) {
      if (!alive.current) return;
      setChoosing({ phase: "failed", message: `${errorText(e)} You can type the path instead.`, startedAt: null, endedAt: Date.now() });
      setPathShown(true);
      window.requestAnimationFrame(() => pathInput.current?.focus());
    }
  }
  async function openPath() {
    const typed = path.trim();
    if (!typed || opening) { if (!typed) setPathError("Enter the full path of the file."); return; }
    setOpening(true);
    setPathError("");
    try {
      const choice = await openCsvPath(typed);
      if (!alive.current) return;
      if (!adopt(choice)) setPathError("No file opened.");
      else setChoosing(IDLE_ACTION);
    } catch (e) {
      if (alive.current) setPathError(errorText(e));
    } finally {
      if (alive.current) setOpening(false);
    }
  }
  function change(patch: Partial<CsvFormat>) {
    setChoosing((c) => (c.phase === "done" ? IDLE_ACTION : c));
    setFormat((f) => (f ? { ...f, ...patch } : f));
  }
  // Read the file again after every format change (after a short pause, so a
  // typed separator or line count reads once).
  const fKey = formatKey(format);
  useEffect(() => {
    if (!file || !format || fKey === inspected.current) return;
    if (!format.separator) return;
    const timer = window.setTimeout(() => { void runInspect(file, format); }, 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fKey, file]);
  useEffect(() => () => inspectCtl.current?.abort(), []);

  const columns = inspect?.columns || [];
  const csvSig = JSON.stringify([columns.map((c) => c.header), format?.header]);
  const csvHeader = (i: number) => {
    const c = columns[i];
    return c && format?.header && c.header.trim() ? c.header : `Column ${i + 1}`;
  };
  const fileReady = !!file && !!inspect && !!format && reading.phase !== "pending" && !!columns.length &&
    fKey === inspected.current && reading.phase !== "failed";

  // ---- Step 2: target and columns ---------------------------------------------
  const [mode, setMode] = useState<"existing" | "new">(session.target.mode);
  const [schema, setSchema] = useState(session.target.schema);
  const [table, setTable] = useState(session.target.table || "");
  const [newName, setNewName] = useState<string | null>(null);
  const [assign, setAssign] = useState<{ key: string; value: (string | null)[] } | null>(null);
  const [newCols, setNewCols] = useState<{ key: string; value: NewColumn[] } | null>(null);
  const [addId, setAddId] = useState<boolean | null>(null);
  const [tables, setTables] = useState<Loaded<CatalogTables>>({ key: "", loading: false, error: "", data: null });
  const [tableInfo, setTableInfo] = useState<Loaded<CatalogTable>>({ key: "", loading: false, error: "", data: null });
  const catalog = useSchemaCatalog(conn, step >= 2 && !mismatch);
  const userSchemas = (catalog.catalog?.schemas || []).filter((s) => !s.system).map((s) => s.name);

  async function readTables(name: string) {
    const k = `${key}\u0000${name}`;
    setTables({ key: k, loading: true, error: "", data: null });
    try {
      const data = await api<CatalogTables>("live/catalog", { connection: conn, schema: name });
      if (alive.current) setTables((t) => (t.key === k ? { key: k, loading: false, error: "", data } : t));
    } catch (e) {
      if (alive.current) setTables((t) => (t.key === k ? { key: k, loading: false, error: errorText(e), data: null } : t));
    }
  }
  async function readColumns(s: string, t: string) {
    const k = `${key}\u0000${s}\u0000${t}`;
    setTableInfo({ key: k, loading: true, error: "", data: null });
    try {
      const data = await api<CatalogTable>("live/catalog", { connection: conn, schema: s, table: t });
      if (alive.current) setTableInfo((x) => (x.key === k ? { key: k, loading: false, error: "", data } : x));
    } catch (e) {
      if (alive.current) setTableInfo((x) => (x.key === k ? { key: k, loading: false, error: errorText(e), data: null } : x));
    }
  }
  const tablesKey = `${key}\u0000${schema}`;
  // Typed names (when a list cannot be read) read once the typing pauses.
  useEffect(() => {
    if (step < 2 || mismatch || !schema || tables.key === tablesKey) return;
    const timer = window.setTimeout(() => { void readTables(schema); }, 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, schema, mismatch]);
  const infoKey = `${key}\u0000${schema}\u0000${table}`;
  useEffect(() => {
    if (step < 2 || mismatch || mode !== "existing" || !schema || !table || tableInfo.key === infoKey) return;
    const timer = window.setTimeout(() => { void readColumns(schema, table); }, 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, schema, table, mode, mismatch]);
  const tableNames = (tables.key === tablesKey ? tables.data?.tables || [] : []).filter((t) => t.type === "table").map((t) => t.name);
  const tablesComplete = tables.key === tablesKey && !!tables.data && !tables.data.next_after && !tables.data.truncated;
  const targetColumns: CatalogColumn[] = (tableInfo.key === infoKey ? tableInfo.data?.columns || [] : []).filter((c) => !c.generated);
  const generatedColumns = (tableInfo.key === infoKey ? tableInfo.data?.columns || [] : []).filter((c) => c.generated);
  const assignKey = `${infoKey}\u0000${csvSig}`;
  useEffect(() => {
    if (mode !== "existing" || !targetColumns.length || !columns.length || assign?.key === assignKey) return;
    setAssign({ key: assignKey, value: matchColumns(columns, targetColumns.map((c) => c.name), !!format?.header) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [assignKey, targetColumns.length, mode]);
  useEffect(() => {
    if (!columns.length || newCols?.key === csvSig) return;
    const fallback = columnNames(columns.map((c) => (format?.header ? c.header : "")));
    const names = columns.map((c, i) => (format?.header && c.name ? c.name : fallback[i]));
    setNewCols({ key: csvSig, value: columns.map((c, i) => {
      const type = c.inferred?.sql_type || "TEXT";
      return { include: true, name: names[i], type, custom: false, nullable: c.inferred?.nullable !== false };
    }) });
    setAddId(!names.some((x) => x.toLowerCase() === "id"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [csvSig]);
  useEffect(() => { if (file && newName === null) setNewName(tableNameFromFile(file.name)); }, [file, newName]);
  const assigned = assign?.key === assignKey ? assign.value : null;
  const created = newCols?.key === csvSig ? newCols.value : null;
  const targetTable = mode === "existing" ? table : (newName || "").trim();
  const targetText = schema && targetTable ? qualified(schema, targetTable) : schema ? `a new table in ${schema}` : "";

  const targetProblems = useMemo(() => {
    const out: string[] = [];
    if (!schema) return ["Choose a database."];
    if (mode === "existing") {
      if (!table) return ["Choose a table."];
      if (tableInfo.key !== infoKey || tableInfo.loading) return ["Reading the table's columns…"];
      if (tableInfo.error) return [`Could not read the table's columns: ${tableInfo.error}`];
      if (!assigned) return ["Matching columns…"];
      if (!assigned.some(Boolean)) out.push("Choose a table column for at least one CSV column.");
      const seen = new Map<string, number>();
      assigned.forEach((c) => { if (c) seen.set(c, (seen.get(c) || 0) + 1); });
      for (const [c, k] of seen) if (k > 1) out.push(`${k} CSV columns go to ${c}. Choose one.`);
    } else {
      const problem = nameProblem(newName || "");
      if (problem) out.push(`Table name: ${problem}`);
      else if (tableNames.some((t) => t.toLowerCase() === (newName || "").trim().toLowerCase()))
        out.push(`${qualified(schema, (newName || "").trim())} exists. Choose Existing table to add rows to it, or another name.`);
      if (!created) return ["Reading the file…"];
      const included = created.filter((c) => c.include);
      if (!included.length) out.push("Include at least one column.");
      const names = new Map<string, number>();
      if (addId) names.set("id", 1);
      for (const c of included) {
        const p = nameProblem(c.name);
        if (p) { out.push(`Column ${c.name || "(no name)"}: ${p}`); continue; }
        const t = typeProblem(c.type);
        if (t) out.push(`Column ${c.name}: ${t}`);
        const k = c.name.toLowerCase();
        names.set(k, (names.get(k) || 0) + 1);
      }
      for (const [c, k] of names) if (k > 1) out.push(c === "id" && addId ? "A column is named id: rename it, or turn off Add an id primary key." : `Two columns are named ${c}.`);
    }
    return out;
  }, [schema, mode, table, tableInfo, infoKey, assigned, newName, tableNames, created, addId]);
  const unfilled = mode === "existing" && assigned ? targetColumns.filter((c) => !assigned.includes(c.name)) : [];
  const required = unfilled.filter((c) => !c.nullable && c.default === null && !/auto_increment/i.test(c.extra || ""));

  // ---- Step 3: check (the plan) -------------------------------------------------
  const [method, setMethod] = useState<CsvMethod>("insert");
  const [txMode, setTxMode] = useState<"all" | "every">("all");
  const [every, setEvery] = useState("1000");
  const [onError, setOnError] = useState<CsvOnError>("stop");
  const everyN = /^\d+$/.test(every.trim()) ? Number(every.trim()) : NaN;
  const everyProblem = txMode === "every" && !(Number.isSafeInteger(everyN) && everyN >= 1 && everyN <= 1_000_000)
    ? "A whole number of rows from 1 to 1,000,000." : "";
  const request: Omit<CsvPlanRequest, "connection"> | null = useMemo(() => {
    if (!file || !format || !fileReady || targetProblems.length || everyProblem) return null;
    const target = mode === "existing"
      ? { mode, schema, table, mapping: targetColumns.map((c) => ({ csv_index: assigned ? (assigned.indexOf(c.name) >= 0 ? assigned.indexOf(c.name) : null) : null, column: c.name })) }
      : {
        mode, schema, table: (newName || "").trim(), add_id_key: !!addId,
        new_columns: (created || []).filter((c) => c.include).map((c) => ({ name: c.name, sql_type: c.type.trim(), nullable: c.nullable })),
        mapping: (created || []).map((c, i) => ({ c, i })).filter(({ c }) => c.include).map(({ c, i }) => ({ csv_index: i, column: c.name })),
      };
    return { file_id: file.file_id, format, target, method, transaction: { mode: txMode, every: txMode === "every" ? everyN : 0 }, on_error: onError };
  }, [file, format, fileReady, targetProblems, everyProblem, mode, schema, table, targetColumns, assigned, newName, addId, created, method, txMode, everyN, onError]);
  const planKey = request ? `${key}\u0000${JSON.stringify(request)}` : "";
  const [plan, setPlan] = useState<{ key: string; value: CsvPlan } | null>(null);
  const [checking, setChecking] = useState<ActionState>(IDLE_ACTION);
  const [checkProgress, setCheckProgress] = useState<CsvProgress | null>(null);
  const planSeq = useRef(0);
  /** The check in flight: the server reads the whole file for it, so a check
   * that is superseded, stopped or left (Back and a change, Cancel, Escape,
   * closing the owner) is stopped on the server too, not only its fetch. */
  const planCtl = useRef<{ controller: AbortController; id: string; key: string } | null>(null);
  function cancelPlan(): boolean {
    const current = planCtl.current;
    if (!current) return false;
    planCtl.current = null;
    planSeq.current += 1;
    current.controller.abort();
    void stopCsvImport(current.id).catch(() => null);
    return true;
  }
  async function runPlan() {
    if (!request) return;
    cancelPlan();
    const controller = new AbortController();
    const id = progressId();
    const asked = planKey;
    planCtl.current = { controller, id, key: asked };
    const seq = ++planSeq.current;
    setCheckProgress(null);
    setChecking({ phase: "pending", message: `Checking every row against ${targetText}…`, startedAt: Date.now(), endedAt: null });
    let polling = true;
    void (async () => {
      while (polling && alive.current && seq === planSeq.current) {
        await new Promise((r) => window.setTimeout(r, POLL_MS));
        if (!polling || seq !== planSeq.current) return;
        try {
          const p = await csvImportProgress(id, controller.signal);
          if (polling && seq === planSeq.current && alive.current) setCheckProgress(p);
        } catch { /* The plan's own answer decides. */ }
      }
    })();
    try {
      const value = await planCsvImport({ ...request, connection: conn, progress_id: id }, controller.signal);
      if (!alive.current || seq !== planSeq.current) return;
      setPlan({ key: asked, value });
      if (value.method && !value.method.load_data_available && method === "load_data") setMethod("insert");
      setChecking(IDLE_ACTION);
    } catch (e) {
      if (!alive.current || seq !== planSeq.current || controller.signal.aborted) return;
      setChecking({ phase: "failed", message: `Could not check the import: ${errorText(e)}`, startedAt: null, endedAt: Date.now() });
    } finally {
      polling = false;
      if (planCtl.current?.id === id) planCtl.current = null;
    }
  }
  function stopChecking() {
    cancelPlan();
    setCheckProgress(null);
    setChecking({ phase: "failed", message: "Stopped checking. Nothing was sent.", startedAt: null, endedAt: Date.now() });
  }
  const planFresh = !!plan && plan.key === planKey;
  useEffect(() => {
    // A check for choices that changed (on any step) answers nothing now.
    if (planCtl.current && planCtl.current.key !== planKey) {
      cancelPlan();
      setCheckProgress(null);
      setChecking(IDLE_ACTION);
    }
    if (step !== 3 || !planKey || mismatch || planFresh) return;
    const timer = window.setTimeout(() => { void runPlan(); }, 250);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, planKey, mismatch, planFresh]);
  useEffect(() => () => { cancelPlan(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const shownPlan = plan?.value || null;
  const blockers = planFresh ? shownPlan?.blockers || [] : [];
  const deniedPrivileges = planFresh ? (shownPlan?.privileges || []).filter((p) => p.state === "denied") : [];
  const planReady = planFresh && !!shownPlan && checking.phase !== "pending" && !!shownPlan.token && shownPlan.ready !== false &&
    !blockers.length && shownPlan.policy?.action !== "refuse" && !deniedPrivileges.length && (shownPlan.rows?.valid ?? 0) > 0;

  // ---- Step 4: review (declined unless Import) -----------------------------------
  const declined = useRef(new Set<string>());
  function declineReview() {
    const p = plan?.value;
    if (!p || !p.token || declined.current.has(p.token) || run) return;
    declined.current.add(p.token);
    void recordDeclined(OPERATION, [p.statements.create, p.statements.data_summary].filter(Boolean) as string[], conn, {
      target: targetText, file: file?.name, rows: p.rows?.total, method: p.method?.chosen, transaction: p.transaction, on_error: p.on_error ?? onError,
    });
  }
  // Leaving the review any way but Import (the owner closing, Start fresh)
  // records the decline too; the latest plan is read through a ref.
  const stepRef = useRef(step);
  stepRef.current = step;
  const declineRef = useRef(declineReview);
  declineRef.current = declineReview;
  useEffect(() => () => { if (stepRef.current === 4) declineRef.current(); }, []);

  // ---- Step 5: import -----------------------------------------------------------
  const [run, setRun] = useState<Run | null>(null);
  const runRef = useRef(run);
  runRef.current = run;
  const [refusing, setRefusing] = useState(false);
  const running = !!run && !run.outcome;
  useEffect(() => { onRunning(running); }, [running]); // eslint-disable-line react-hooks/exhaustive-deps

  async function startImport() {
    const p = plan?.value;
    if (!p || !p.token || !planReady || mismatch || runRef.current || refusing) return;
    setRefusing(true);
    let checked: Connection = conn;
    // Stopped here before sending: recorded as refused, and the result names
    // the Audit log only when the record was written.
    const refuse = async (reason: "policy_unknown" | "policy_changed", message: string) => {
      declined.current.add(p.token!);
      const record = await api<{ audit_id?: string }>("audit/declined", { operation: OPERATION,
        statements: [p.statements.create, p.statements.data_summary].filter(Boolean), connection: conn, reason,
        target: { target: targetText, file: file?.name } }).catch(() => null);
      setRefusing(false);
      finishLocal(p, { kind: "refused", message, auditId: record?.audit_id || null });
    };
    try {
      if (policy) checked = await policy.check();
    } catch (e) {
      await refuse("policy_unknown", `The saved connections could not be read (${errorText(e)}). Nothing was sent.`);
      return;
    }
    if (connectionKey(checked) !== key) {
      await refuse("policy_changed", "The connection or its write policy changed. Nothing was sent. Check the import again.");
      return;
    }
    setRefusing(false);
    const id = progressId();
    declined.current.add(p.token);
    const fresh: Run = { id, startedAt: Date.now(), plan: p, progress: null, outcome: null, stopping: null, stopError: "" };
    setRun(fresh);
    setStep(5);
    let polling = true;
    let lastPhase = "";
    void (async () => {
      while (polling) {
        await new Promise((r) => window.setTimeout(r, POLL_MS));
        if (!polling) return;
        try {
          const snap = await csvImportProgress(id);
          if (!polling) return;
          if (snap.phase && snap.phase !== "unknown") lastPhase = snap.phase;
          setRun((r) => (r && r.id === id && !r.outcome ? { ...r, progress: snap } : r));
        } catch { /* Progress only describes the import; its own answer decides. */ }
      }
    })();
    try {
      const value = await applyCsvImport(checked, p.token, id);
      polling = false;
      setRun((r) => (r && r.id === id ? { ...r, outcome: { kind: "result", value } } : r));
    } catch (e) {
      polling = false;
      // A reply with an error (4xx) came before anything was sent, unless the
      // progress already said rows were with the server; no reply is unknown.
      const failure = writeFailure(e);
      const withServer = ["creating", "sending", "committing", "rolling_back"].includes(lastPhase);
      // The refusal names its Audit log record only when the reply carries
      // one (an expired or evicted review is refused without a record).
      const data = e instanceof ApiError ? e.data : null;
      const recorded = data && typeof data.audit_id === "string" ? data.audit_id
        : data && data.audit && typeof (data.audit as { id?: unknown }).id === "string" ? (data.audit as { id: string }).id : null;
      const outcome: Outcome = failure.phase === "failed" && !withServer
        ? { kind: "refused", message: `${errorText(e)}${/nothing was sent/i.test(errorText(e)) ? "" : " Nothing was sent."}`, auditId: recorded }
        : { kind: "unknown", message: `${failure.message} Whether rows were committed is unknown: check ${targetText} before importing again.` };
      setRun((r) => (r && r.id === id ? { ...r, outcome } : r));
    }
  }
  function finishLocal(p: CsvPlan, outcome: Outcome) {
    setRun({ id: "", startedAt: Date.now(), plan: p, progress: null, outcome, stopping: null, stopError: "" });
    setStep(5);
  }
  async function stopImport() {
    const r = runRef.current;
    if (!r || r.outcome || r.stopping) return;
    setRun((x) => (x && x.id === r.id ? { ...x, stopping: Date.now(), stopError: "" } : x));
    try {
      await stopCsvImport(r.id);
    } catch (e) {
      setRun((x) => (x && x.id === r.id && !x.outcome ? { ...x, stopping: null, stopError: `Could not ask the import to stop: ${errorText(e)}` } : x));
    }
  }

  // ---- Navigation, focus, Escape ------------------------------------------------------
  const runElapsed = useElapsed(running ? run!.startedAt : null);
  const canLeave = running && runElapsed * 1000 >= ACTION_SLOW_AFTER_MS;
  function end() {
    inspectCtl.current?.abort();
    cancelPlan();
    onEnd();
  }
  function cancel() {
    if (step === 5) {
      if (running) { if (canLeave) setOpen(false); return; }
      setOpen(false);
      return;
    }
    if (step === 4) declineReview();
    end();
  }
  useFocusScope(dialog, {
    open,
    onClose: cancel,
    initialFocus: () => (step === 4 ? cancelButton.current : stepsRef.current?.querySelector<HTMLElement>("[aria-current=step]")),
  });
  useEffect(() => {
    if (!open) return;
    window.requestAnimationFrame(() => {
      if (step === 4) cancelButton.current?.focus();
      else if (step === 5 && run?.outcome) doneButton.current?.focus();
      else stepsRef.current?.querySelector<HTMLElement>("[aria-current=step]")?.focus();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step, open, !!run?.outcome]);
  function go(next: Step) {
    if (next === 3 && step < 3 && plan && plan.key !== planKey) setChecking(IDLE_ACTION);
    setStep(next);
  }
  const nextDisabled = mismatch || (step === 1 ? !fileReady : step === 2 ? targetProblems.length > 0 : step === 3 ? !planReady : false);
  const nextReason = mismatch ? `This import belongs to ${label}. Switch back to it to continue.`
    : step === 1 ? (!file ? "Choose a file first." : !fileReady ? (reading.phase === "pending" ? "Reading the file…" : "The file could not be read with this format.") : "")
      : step === 2 ? targetProblems[0] || ""
        : step === 3 ? (checking.phase === "pending" ? "Checking…" : !planFresh ? "The check is out of date." : blockers[0] || deniedPrivileges[0]?.reason ||
          (shownPlan?.policy?.action === "refuse" ? shownPlan.policy.text : (shownPlan?.rows?.valid ?? 0) === 0 ? "No row can be imported." : ""))
          : "";
  // The footer names why Next is off only when the step does not already
  // show it (a list of problems or blockers says it once, in the body).
  const footerReason = !nextDisabled || mismatch ? ""
    : step === 1 ? nextReason
      : step === 2 ? (/…$/.test(nextReason) ? nextReason : "")
        : step === 3 && checking.phase !== "pending" && !blockers.length && !deniedPrivileges.length && shownPlan?.policy?.action !== "refuse" ? nextReason : "";

  // ---- Rendering --------------------------------------------------------------------
  const p = shownPlan;
  const type = p?.policy?.connection_type || p?.connection?.type || conn.type || "production";
  const readOnly = p?.policy?.read_only === true || conn.read_only === true;
  const title = step === 4 && p
    ? `Import ${plural(p.rows.valid, "row")} into ${targetText}?`
    : step === 5 && run
      ? (run.outcome ? `Import into ${targetText}` : `Importing into ${targetText}`)
      : mode === "new" ? `Import CSV into a new table ${targetTable ? qualified(schema, targetTable) : `in ${schema}`}` : `Import CSV into ${targetText || "a table"}`;

  const sheet = createPortal(
    <dialog ref={dialog} className="confirm-sheet import-csv-sheet" role={step === 4 ? "alertdialog" : "dialog"} tabIndex={-1}
      aria-labelledby={titleId} aria-busy={running || undefined} data-step={step}
      onCancel={(e) => { e.preventDefault(); cancel(); }}>
      <ConnectionTypeBand connection={conn} label={label} type={type} readOnly={readOnly} />
      <div className="dialog-heading">
        <div>
          <div className="eyebrow">{step === 4 && p ? (p.statements.create ? "CREATE TABLE · " : "") + (p.method.chosen === "load_data" ? "LOAD DATA LOCAL INFILE" : "INSERT") : "Import CSV"}</div>
          <h2 id={titleId}>{title}</h2>
        </div>
      </div>
      <ol className="import-csv-steps" aria-label="Steps" ref={stepsRef}>
        {STEPS.map((name, i) => (
          <li key={name} tabIndex={i + 1 === step ? -1 : undefined} aria-current={i + 1 === step ? "step" : undefined}
            className={i + 1 < step ? "is-done" : undefined}>
            <span className="import-csv-step-number" aria-hidden="true">{i + 1}</span>
            <span className="import-csv-step-label">{name}</span>
            {i + 1 === step && <span className="visually-hidden">, step {i + 1} of {STEPS.length}</span>}
          </li>
        ))}
      </ol>
      {mismatch && step < 5 && (
        <p className="import-csv-banner" role="alert">
          This import belongs to {label}, and the connection in use changed. Switch back to {label} to continue; nothing was sent.
        </p>
      )}
      <div className="sheet-body import-csv-body">
        {step === 1 && fileStep()}
        {step === 2 && targetStep()}
        {step === 3 && checkStep()}
        {step === 4 && p && reviewStep(p)}
        {step === 5 && run && importStep(run)}
        {canLeave && open && <p className="sheet-leave-note">Close keeps the import running. Its progress and outcome stay in the view; nothing is sent again.</p>}
      </div>
      <div className="dialog-actions sheet-actions import-csv-actions">
        <span className="sheet-progress import-csv-reason" role="status" aria-live="polite">
          {step < 4 ? footerReason : ""}
        </span>
        <div className="sheet-buttons">
          {step === 5 ? (
            running ? <>
              <button ref={cancelButton} type="button" className="button" disabled={!canLeave} onClick={cancel}
                title={canLeave ? undefined : "Close is offered after 10 seconds; the import keeps running."}>Close</button>
              <ActionButton danger busy={!!run?.stopping} busyLabel="Stopping…" onClick={() => void stopImport()}>Stop</ActionButton>
            </> : <>
              {run?.outcome?.kind === "refused" && (
                <button type="button" className="button" onClick={() => { setRun(null); setPlan(null); setStep(3); }}>Check again</button>
              )}
              <button ref={doneButton} type="button" className="button primary" onClick={cancel}>Close</button>
            </>
          ) : <>
            <button ref={cancelButton} type="button" className="button" onClick={cancel}>Cancel</button>
            {step > 1 && <button type="button" className="button" disabled={refusing} onClick={() => go((step - 1) as Step)}>Back</button>}
            {step < 4 ? (
              <button type="button" className="button primary" aria-disabled={nextDisabled || undefined}
                aria-describedby={nextDisabled ? `${titleId}-why` : undefined}
                onClick={() => { if (!nextDisabled) go((step + 1) as Step); }}>
                {step === 3 ? "Review…" : "Next"}
              </button>
            ) : (
              <ActionButton className={(p?.policy?.tone || "danger") === "danger" ? "danger-solid" : "primary"} busy={refusing} busyLabel="Checking…"
                disabled={!planReady || mismatch} onClick={() => void startImport()}>Import</ActionButton>
            )}
          </>}
          <span id={`${titleId}-why`} className="visually-hidden">{nextReason}</span>
        </div>
      </div>
    </dialog>,
    document.body,
  );

  // The view's line: an import whose sheet was closed, then its outcome.
  const statusLine = !open && run ? <ImportStatus run={run} target={targetText} file={file?.name || "the file"}
    other={mismatch ? label : ""} onShow={() => setOpen(true)} onStop={() => void stopImport()} onDismiss={end} /> : null;

  return <>{open && sheet}{statusLine}</>;

  // ---- Steps (closures over the wizard's state) ----------------------------------------

  function fileStep() {
    const detected = inspect?.detected || {};
    // A field equal to a marker is NULL; the backend's default is \N alone.
    const markers = format?.null_markers ?? ["\\N"];
    const sepOptions = [...SEPARATORS.map((s) => ({ value: s.value, label: s.label })), { value: "other", label: "Other" }];
    const rows = inspect?.rows ?? null;
    const estimate = inspect?.rows_estimate ?? null;
    const problems = inspect?.problems || [];
    const total = inspect?.problems_total ?? problems.length;
    return <section className="import-csv-step" aria-label="File and format">
      <div className="import-csv-file">
        <ActionButton busy={choosing.phase === "pending"} busyLabel="Choosing…" primary={!file} onClick={() => void chooseFile()}>
          {file ? "Choose another file…" : "Choose file…"}
        </ActionButton>
        {file && (
          <span className="import-csv-file-name" title={file.name}>
            <strong>{file.name}</strong>
            <span>{[bytes(file.size), fileTime(file.modified) ? `modified ${fileTime(file.modified)}` : ""].filter(Boolean).join(" · ")}</span>
          </span>
        )}
        {!pathShown && <button type="button" className="text-button" onClick={() => { setPathShown(true); window.requestAnimationFrame(() => pathInput.current?.focus()); }}>Type a path instead</button>}
      </div>
      <ActionStatus className="import-csv-line" state={choosing} slowAfter={Number.POSITIVE_INFINITY} />
      {pathShown && (
        <div className="import-csv-path">
          <label htmlFor={pathId}>Path of the file</label>
          <div className="import-csv-path-row">
            <input id={pathId} ref={pathInput} value={path} spellCheck={false} autoComplete="off" placeholder="/Users/me/Downloads/orders.csv"
              aria-describedby={`${pathId}-hint`} aria-invalid={pathError ? true : undefined}
              onChange={(e) => { setPath(e.target.value); setPathError(""); }}
              onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void openPath(); } }} />
            <ActionButton busy={opening} busyLabel="Opening…" onClick={() => void openPath()}>Open</ActionButton>
          </div>
          <p id={`${pathId}-hint`} className={pathError ? "form-error" : "hint"} role={pathError ? "alert" : undefined}>
            {pathError || "The full path of a .csv, .tsv or .txt file on this computer."}
          </p>
        </div>
      )}
      {!file && <p className="hint import-csv-intro">The file is read on this computer. Nothing is sent to the server until you review the import and choose Import.</p>}
      {file && format && <>
        <div className="import-csv-grid-fields">
          <div className="import-csv-field import-csv-wide">
            <span className="import-csv-label" id={`${pathId}-sep`}>Separator
              {detected.separator !== undefined && <span className="import-csv-detected">Detected: {separatorName(detected.separator)}</span>}</span>
            <div className="import-csv-inline">
              <Segmented label="Separator" value={sepMode} options={sepOptions} onChange={(v) => {
                setSepMode(v);
                if (v !== "other") change({ separator: v });
                else if (otherSep) change({ separator: otherSep });
              }} />
              {sepMode === "other" && (
                <input className="import-csv-char" aria-label="Other separator (one character)" maxLength={1} value={otherSep} spellCheck={false}
                  placeholder="~" onChange={(e) => { const v = e.target.value.slice(-1); setOtherSep(v); if (v) change({ separator: v }); }} />
              )}
            </div>
            {sepMode === "other" && !otherSep && <span className="hint">Type the one character that separates fields.</span>}
          </div>
          <div className="import-csv-field">
            <span className="import-csv-label">Header row
              {detected.header !== undefined && <span className="import-csv-detected">Detected: {detected.header ? "yes" : "no"}</span>}</span>
            <Checkbox checked={format.header} onChange={(v) => change({ header: v })}>First row has column names</Checkbox>
          </div>
          <label className="import-csv-field">
            <span className="import-csv-label">Encoding
              {detected.encoding && <span className="import-csv-detected">Detected: {encodingLabel(detected.encoding)}</span>}</span>
            <Select value={encodingValue(format.encoding)} onChange={(v) => change({ encoding: v })}
              options={ENCODINGS.some(([v]) => v === encodingValue(format.encoding)) ? ENCODINGS : [...ENCODINGS, [format.encoding, format.encoding]]} />
          </label>
          <label className="import-csv-field">
            <span className="import-csv-label">Quote</span>
            <Select value={format.quote} onChange={(v) => change({ quote: v })} options={QUOTES} />
          </label>
          <div className="import-csv-field">
            <span className="import-csv-label">Empty fields are</span>
            <Segmented label="Empty fields are" value={format.empty} onChange={(v) => change({ empty: v as "null" | "empty" })}
              options={[{ value: "null", label: "NULL" }, { value: "empty", label: "Empty string" }]} />
          </div>
          <div className="import-csv-field" role="group" aria-label="NULL markers">
            <span className="import-csv-label">NULL markers</span>
            {NULL_MARKERS.map(([marker, text]) => (
              <Checkbox key={marker} checked={markers.includes(marker)} onChange={(on) => change({
                null_markers: NULL_MARKERS.map(([m]) => m).filter((m) => (m === marker ? on : markers.includes(m))),
              })}>{text}</Checkbox>
            ))}
          </div>
          <label className="import-csv-field">
            <span className="import-csv-label">Skip first lines</span>
            <NumberField value={format.skip_lines} min={0} max={1000} step={1} unit="lines"
              onChange={(v) => { const k = Math.max(0, Math.min(1000, Math.trunc(Number(v) || 0))); change({ skip_lines: k }); }} />
          </label>
        </div>
        <div className="import-csv-preview-head">
          <h3>Preview</h3>
          {inspect && <span className="import-csv-counts">
            {[rows !== null ? plural(rows, "row") : estimate !== null ? `about ${plural(estimate, "row")}` : "",
              plural(columns.length, "column"), total ? plural(total, "problem") : ""].filter(Boolean).join(" · ")}
          </span>}
          {!!inspect?.notes?.length && <NotesButton notes={inspect.notes} label="Notes about this file" />}
        </div>
        <ActionStatus className="import-csv-line" state={reading} onStop={stopReading} stopLabel="Stop" slowText="Still reading the file."
          onRetry={reading.phase === "failed" && file ? () => void runInspect(file, inspect ? format : null) : undefined} />
        {inspect && previewTable(inspect, reading.phase === "pending")}
        {problems.length > 0 && problemList(problems, total, false)}
      </>}
    </section>;
  }

  function previewTable(report: CsvInspect, stale: boolean) {
    const rows = report.preview?.rows || [];
    const lines = report.preview?.lines || [];
    return <div className={`import-csv-table${stale ? " is-stale" : ""}`} tabIndex={0} role="region" aria-label="Preview of the first rows">
      <table>
        <thead><tr><th scope="col" className="import-csv-line-col">Line</th>
          {report.columns.map((c, i) => <th key={i} scope="col"><span className="import-csv-cell" title={csvHeader(i)}>{csvHeader(i)}</span>
            <small>{c.inferred?.sql_type || ""}</small></th>)}</tr></thead>
        <tbody>
          {rows.map((row, r) => <tr key={r}>
            <td className="import-csv-line-col">{lines[r] ?? r + 1}</td>
            {report.columns.map((_c, i) => {
              const v = row[i];
              return <td key={i}>{v === null || v === undefined ? <span className="import-csv-null">NULL</span>
                : <span className="import-csv-cell" title={v}>{v === "" ? " " : v}</span>}</td>;
            })}
          </tr>)}
        </tbody>
      </table>
      {report.preview?.truncated && <p className="hint import-csv-table-note">Showing the first {plural(rows.length, "row")}.</p>}
    </div>;
  }

  function problemList(problems: { line: number; message: string; column?: string | null; value?: string | null }[], total: number, columnsShown: boolean, title = "Problems") {
    return <div className="import-csv-problems">
      <h3>{total > problems.length ? `${title}: the first ${problems.length.toLocaleString()} of ${total.toLocaleString()}` : `${title} (${total.toLocaleString()})`}</h3>
      <div className="import-csv-table" tabIndex={0} role="region" aria-label="Problems by line">
        <table>
          <thead><tr><th scope="col" className="import-csv-line-col">Line</th>
            {columnsShown && <><th scope="col">Column</th><th scope="col">Value</th></>}
            <th scope="col">Problem</th></tr></thead>
          <tbody>{problems.map((x, i) => <tr key={i}>
            <td className="import-csv-line-col">{x.line}</td>
            {columnsShown && <><td><span className="import-csv-cell" title={x.column || ""}>{x.column || "—"}</span></td>
              <td><span className="import-csv-cell" title={x.value ?? ""}>{x.value === null || x.value === undefined ? "—" : x.value}</span></td></>}
            <td className="import-csv-wrap">{x.message}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </div>;
  }

  function targetStep() {
    const schemaOptions: [string, string][] = [...new Set([...(schema ? [schema] : []), ...userSchemas])].map((s) => [s, s]);
    const listFailed = !!catalog.error && !userSchemas.length;
    return <section className="import-csv-step" aria-label="Target and columns">
      <div className="import-csv-grid-fields">
        <div className="import-csv-field">
          <span className="import-csv-label">Into</span>
          <Segmented label="Into" value={mode} onChange={(v) => setMode(v as "existing" | "new")}
            options={[{ value: "existing", label: "Existing table" }, { value: "new", label: "New table" }]} />
        </div>
        <label className="import-csv-field">
          <span className="import-csv-label">Database</span>
          {listFailed ? <input value={schema} spellCheck={false} maxLength={64} aria-describedby="import-csv-db-hint"
            onChange={(e) => { setSchema(e.target.value.trim()); setTable(""); }} />
            : <Select value={schema} onChange={(v) => { setSchema(v); setTable(""); }} options={schemaOptions.length ? schemaOptions : [["", catalog.loading ? "Reading…" : "No databases"]]} />}
          {listFailed && <span id="import-csv-db-hint" className="hint">Could not list databases ({catalog.error}). Type the name.</span>}
        </label>
        {mode === "existing" ? (
          <label className="import-csv-field">
            <span className="import-csv-label">Table</span>
            {tablesComplete && (tableNames.length || !table)
              ? <Select value={table} onChange={setTable} options={[["", tableNames.length ? "Choose a table" : "No tables"] as [string, string],
                ...[...new Set([...(table ? [table] : []), ...tableNames])].map((t) => [t, t] as [string, string])]} />
              : <><input value={table} list="import-csv-tables" spellCheck={false} maxLength={64} placeholder={tables.loading ? "Reading tables…" : "Table name"}
                onChange={(e) => setTable(e.target.value)} />
                <datalist id="import-csv-tables">{tableNames.map((t) => <option key={t} value={t} />)}</datalist></>}
            {tables.error && <span className="hint">Could not list tables ({tables.error}). Type the name.</span>}
          </label>
        ) : (
          <label className="import-csv-field">
            <span className="import-csv-label">New table name</span>
            <input value={newName || ""} spellCheck={false} maxLength={64} onChange={(e) => setNewName(e.target.value)} />
          </label>
        )}
      </div>
      {mode === "existing" ? existingColumns() : newColumns()}
      {targetProblems.length > 0 && !/…$/.test(targetProblems[0]) && (
        <ul className="import-csv-warnings" aria-label="To fix before checking">
          {targetProblems.map((x) => <li key={x}>{x}</li>)}
        </ul>
      )}
    </section>;
  }

  function existingColumns() {
    if (!table) return null;
    const reading = tableInfo.key !== infoKey || tableInfo.loading;
    return <div className="import-csv-columns">
      <h3>Columns</h3>
      <ActionStatus className="import-csv-line" state={reading ? { phase: "pending", message: `Reading the columns of ${qualified(schema, table)}…`, startedAt: null }
        : tableInfo.error ? { phase: "failed", message: `Could not read the columns: ${tableInfo.error}`, startedAt: null } : IDLE_ACTION}
        onRetry={tableInfo.error ? () => void readColumns(schema, table) : undefined} />
      {assigned && targetColumns.length > 0 && <>
        <div className="import-csv-table" tabIndex={0} role="region" aria-label="CSV columns and the table columns they fill">
          <table>
            <thead><tr><th scope="col">CSV column</th><th scope="col">Example</th><th scope="col">Table column</th><th scope="col">Type</th></tr></thead>
            <tbody>{columns.map((c, i) => {
              const chosen = assigned[i];
              const info = targetColumns.find((t) => t.name === chosen);
              return <tr key={i}>
                <th scope="row"><span className="import-csv-cell" title={csvHeader(i)}>{csvHeader(i)}</span></th>
                <td><span className="import-csv-cell import-csv-muted" title={c.samples?.[0] || ""}>{c.samples?.[0] ?? ""}</span></td>
                <td><Select label={`Table column for ${csvHeader(i)}`} value={chosen || ""} onChange={(v) => {
                  setAssign({ key: assignKey, value: assigned.map((x, j) => (j === i ? v || null : x)) });
                }} options={[["", "Ignore"], ...targetColumns.map((t) => [t.name, t.name] as [string, string])]} /></td>
                <td><span className="import-csv-muted">{info ? `${info.type}${info.nullable ? "" : " NOT NULL"}` : "—"}</span></td>
              </tr>;
            })}</tbody>
          </table>
        </div>
        <div className="import-csv-after-table">
          <button type="button" className="text-button" onClick={() => setAssign({ key: assignKey, value: matchColumns(columns, targetColumns.map((t) => t.name), !!format?.header) })}>
            Match by name again
          </button>
          {unfilled.length > 0 && <span className="hint">Not filled from the file: {unfilled.map((c) => c.name).join(", ")}{generatedColumns.length ? `; generated by the server: ${generatedColumns.map((c) => c.name).join(", ")}` : ""}.</span>}
        </div>
        {required.length > 0 && <p className="import-csv-watch"><StatusPill tone="watch" label="Watch" /> {required.map((c) => c.name).join(", ")} {required.length === 1 ? "has" : "have"} no default and cannot be NULL: choose a CSV column for {required.length === 1 ? "it" : "them"}, or every row fails.</p>}
      </>}
    </div>;
  }

  function newColumns() {
    if (!created) return null;
    const set = (i: number, patch: Partial<NewColumn>) => setNewCols({ key: csvSig, value: created.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
    return <div className="import-csv-columns">
      <h3>Columns of the new table</h3>
      <Checkbox checked={!!addId} onChange={setAddId}>Add an id primary key <span className="import-csv-muted">(id BIGINT UNSIGNED AUTO_INCREMENT)</span></Checkbox>
      <div className="import-csv-table" tabIndex={0} role="region" aria-label="Columns of the new table">
        <table>
          <thead><tr><th scope="col">Import</th><th scope="col">Column name</th><th scope="col">Type</th><th scope="col">NULL</th><th scope="col">Example</th></tr></thead>
          <tbody>{created.map((c, i) => {
            const inferred = columns[i]?.inferred?.sql_type || "";
            const choices: [string, string][] = [
              ...(inferred ? [[inferred, `${inferred} (suggested)`] as [string, string]] : []),
              ...TYPE_CHOICES.filter((t) => t !== inferred).map((t) => [t, t] as [string, string]),
              ["__other", "Other…"],
            ];
            const known = !c.custom && choices.some(([v]) => v === c.type);
            return <tr key={i} className={c.include ? undefined : "is-off"}>
              <td><Checkbox checked={c.include} onChange={(v) => set(i, { include: v })}><span className="visually-hidden">Import {csvHeader(i)}</span></Checkbox></td>
              <td><input aria-label={`Name of the column from ${csvHeader(i)}`} value={c.name} maxLength={64} spellCheck={false} disabled={!c.include}
                aria-invalid={c.include && !!nameProblem(c.name) ? true : undefined} onChange={(e) => set(i, { name: e.target.value })} /></td>
              <td className="import-csv-type">
                <Select label={`Type of ${c.name || csvHeader(i)}`} disabled={!c.include} value={known ? c.type : "__other"} options={choices}
                  onChange={(v) => set(i, v === "__other" ? { custom: true } : { type: v, custom: false })} />
                {!known && <input aria-label={`Other type of ${c.name || csvHeader(i)}`} value={c.type} spellCheck={false} maxLength={200} disabled={!c.include}
                  aria-invalid={c.include && !!typeProblem(c.type) ? true : undefined} onChange={(e) => set(i, { type: e.target.value, custom: true })} />}
              </td>
              <td><Checkbox checked={c.nullable} disabled={!c.include} onChange={(v) => set(i, { nullable: v })}><span className="visually-hidden">{c.name} allows NULL</span></Checkbox></td>
              <td><span className="import-csv-cell import-csv-muted" title={columns[i]?.samples?.[0] || ""}>{columns[i]?.samples?.[0] ?? ""}</span></td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      <p className="hint">Types are suggested from the values in the file; every row is checked against them in the next step.</p>
    </div>;
  }

  function checkStep() {
    const fresh = planFresh ? p : null;
    const loadData = fresh?.method || p?.method || null;
    const loadDataOff = !!loadData && !loadData.load_data_available;
    // The backend's sentence, shown once as it is; a reply without one gets
    // the workspace's own words.
    const loadWhy = loadData && !loadData.load_data_available
      ? loadData.why || (loadData.local_infile === false ? "The server has local_infile OFF. myflames does not change this setting."
        : "LOAD DATA LOCAL is not available here. myflames does not change local_infile.")
      : "";
    const loadBatches = fresh && fresh.method.chosen === "load_data" && fresh.batches ? fresh.batches : 0;
    const progressText = checking.phase === "pending" && checkProgress && checkProgress.rows_total
      ? `Checked ${n(checkProgress.rows_checked ?? 0)} of ${n(checkProgress.rows_total)} rows against ${targetText}…` : checking.message;
    return <section className="import-csv-step" aria-label="Check">
      <ActionStatus className="import-csv-line" state={{ ...checking, message: progressText }} onStop={stopChecking} stopLabel="Stop" slowText="Still checking."
        onRetry={checking.phase === "failed" ? () => void runPlan() : undefined} />
      {fresh && <p className="import-csv-summary">
        {[plural(fresh.rows.total, "row"), `${fresh.rows.valid.toLocaleString()} ready`, fresh.rows.invalid ? `${fresh.rows.invalid.toLocaleString()} with problems` : "",
          fresh.batches ? plural(fresh.batches, "statement") : ""].filter(Boolean).join(" · ")}
      </p>}
      {!fresh && p && checking.phase !== "failed" && <p className="hint">Checking again with the new choices…</p>}
      <div className="import-csv-grid-fields">
        <div className="import-csv-field import-csv-wide">
          <span className="import-csv-label">Method</span>
          <Segmented label="Method" value={method} onChange={(v) => setMethod(v as CsvMethod)}
            options={[{ value: "insert", label: "INSERT batches" }, { value: "load_data", label: "LOAD DATA LOCAL", disabled: loadDataOff || !loadData, title: loadWhy || undefined }]} />
          <span className="hint">{loadDataOff ? `LOAD DATA LOCAL is off: ${loadWhy}`
            : method === "load_data" ? `Faster: the server reads a checked copy of the rows${loadBatches ? `, in ${plural(loadBatches, "statement")}` : ""}. myflames checks every row first and compares the row count after.`
              : "Works with local_infile OFF. Up to 1,000 rows per statement; every bad row is reported with its line."}</span>
        </div>
        <div className="import-csv-field import-csv-wide">
          <span className="import-csv-label">If something fails</span>
          <div className="import-csv-inline">
            <Segmented label="If something fails" value={txMode} onChange={(v) => setTxMode(v as "all" | "every")}
              options={[{ value: "all", label: "Keep nothing" }, { value: "every", label: "Commit every N rows" }]} />
            {txMode === "every" && <NumberField label="Rows per commit" value={every} min={1} max={1_000_000} step={100} unit="rows" onChange={setEvery} />}
          </div>
          <span className={everyProblem ? "form-error" : "hint"}>{everyProblem || (txMode === "all"
            ? "One transaction: a failure or Stop rolls back every row."
            : "Rows committed before a failure or Stop stay; the result names the first line not committed.")}</span>
        </div>
        <div className="import-csv-field import-csv-wide">
          <span className="import-csv-label">Bad rows</span>
          <Segmented label="Bad rows" value={onError} onChange={(v) => setOnError(v as CsvOnError)}
            options={[{ value: "stop", label: "Stop" }, { value: "skip", label: "Skip and report" }]} />
          <span className="hint">{onError === "stop" ? "The import stops at the first row the server or the check refuses." : "Rows with problems are left out and listed by line in the result."}</span>
        </div>
      </div>
      {fresh && (blockers.length > 0 || deniedPrivileges.length > 0 || fresh.policy?.action === "refuse") && (
        <ul className="import-csv-blockers" role="alert" aria-label="Why it cannot run now">
          {fresh.policy?.action === "refuse" && <li>{fresh.policy.text}</li>}
          {blockers.map((x) => <li key={x}>{x}</li>)}
          {deniedPrivileges.map((x, i) => <li key={`p${i}`}>{x.reason}</li>)}
        </ul>
      )}
      {fresh && fresh.warnings.length > 0 && (
        <ul className="import-csv-warnings" aria-label="Before you import">
          {fresh.warnings.map((x) => <li key={x}>{x}</li>)}
        </ul>
      )}
      {fresh && fresh.problems.length > 0 && problemList(fresh.problems, fresh.problems_total ?? fresh.rows.invalid ?? fresh.problems.length, true)}
      {fresh && !fresh.problems.length && fresh.rows.invalid === 0 && <p className="import-csv-ok"><StatusPill tone="ok" label="OK" /> Every row fits {targetText}.</p>}
      {!!fresh?.notes?.length && <NotesButton notes={fresh.notes} label="Notes about this check" />}
    </section>;
  }

  function reviewStep(rp: CsvPlan) {
    const endpoint = `${conn.host || "127.0.0.1"}:${conn.port || 3306}`;
    const tx = rp.transaction?.mode === "every" ? `Commit every ${plural(rp.transaction.every, "row")}${rp.transaction.commits ? ` (${plural(rp.transaction.commits, "commit")})` : ""}` : "Keep nothing: one transaction, rolled back on a failure or Stop";
    const privilegeWord = (state: string) => (state === "allowed" ? "Allowed" : state === "denied" ? "Denied" : "Unverified");
    return <section className="import-csv-step" aria-label="Review">
      <p className="sheet-lead">Before sending, myflames checks the file, the table and the connection's write policy again, and sends nothing if any of them changed.</p>
      <Fields label="What will be sent" rows={[
        ["Connection", label.includes(endpoint) ? label : `${label} (${endpoint})`],
        ["Type", `${TYPE_LABEL[type === "development" ? "development" : "production"]}${readOnly ? ", read-only" : ""}`],
        ["Target", rp.statements.create ? `${targetText} (new table)` : `${targetText}${rp.target.engine ? ` · ${rp.target.engine}` : ""}${rp.target.rows_estimate !== null && rp.target.rows_estimate !== undefined ? ` · about ${plural(rp.target.rows_estimate, "row")} now` : ""}`],
        ["File", `${file?.name || "—"} · ${plural(rp.rows.total, "row")}`],
        ["Rows to import", `${rp.rows.valid.toLocaleString()}${rp.rows.invalid ? ` (${plural(rp.rows.invalid, "row")} with problems: ${onError === "skip" ? "skipped and reported" : "the import stops there"})` : ""}`],
        ["Method", rp.method.chosen === "load_data" ? `LOAD DATA LOCAL INFILE (${plural(rp.batches, "statement")}; the server reads a checked copy of the rows)` : `INSERT batches (${plural(rp.batches, "statement")})`],
        ["If something fails", tx],
        ["Privileges", <span className="import-csv-privileges">{(rp.privileges || []).length ? rp.privileges.map((x, i) =>
          <span key={i}><strong>{privilegeWord(x.state)}</strong>{x.action ? ` ${x.action.toUpperCase().replace(/_/g, " ")}` : ""}: {x.reason}</span>) : "Not checked; the server decides."}</span>],
      ]} />
      {rp.policy?.text && <p className={rp.policy.action === "refuse" ? "import-csv-blockers" : "import-csv-policy"}>{rp.policy.text}</p>}
      {rp.statements.create && <div className="import-csv-sql">
        <h3>CREATE TABLE, sent first on its own</h3>
        <pre tabIndex={0} aria-label="CREATE TABLE statement">{rp.statements.create}</pre>
      </div>}
      <div className="import-csv-sql">
        <h3>Rows</h3>
        {!!rp.statements.setup?.length && <>
          <p className="hint">First, in the import's own session:</p>
          <pre tabIndex={0} aria-label="Session statements sent first">{rp.statements.setup.join(";\n")};</pre>
        </>}
        <p>{rp.statements.data_summary}</p>
        {rp.statements.data_sample && <>
          <pre tabIndex={0} aria-label="First data statement, shortened">{rp.statements.data_sample}</pre>
          <span className="hint">The first statement, shortened. The row values are not stored in the Audit log.</span>
        </>}
      </div>
      {rp.mapping?.length > 0 && <Disclosure title="Columns" count={rp.mapping.filter((m) => m.csv_index !== null).length}>
        <ul className="import-csv-mapping">{rp.mapping.map((m, i) => <li key={i}>
          {m.csv_index === null ? <span className="import-csv-muted">not from the file</span> : <span>{m.csv_header || csvHeader(m.csv_index)}</span>} → <code>{m.column}</code>{m.type ? <span className="import-csv-muted"> {m.type}</span> : null}
        </li>)}</ul>
      </Disclosure>}
      {rp.warnings.length > 0 && <ul className="import-csv-warnings" aria-label="Warnings">{rp.warnings.map((x) => <li key={x}>{x}</li>)}</ul>}
      {rp.drop_hint && <div className="import-csv-sql">
        <h3>If the rows fail</h3>
        <p className="hint">The new table stays, empty. To remove it, run this yourself; myflames never sends it.</p>
        <pre tabIndex={0} aria-label="DROP TABLE statement">{rp.drop_hint}</pre>
      </div>}
    </section>;
  }

  function importStep(r: Run) {
    const pr = r.progress;
    const total = pr?.rows_total ?? r.plan.rows.valid ?? 0;
    const sent = pr?.rows_sent || 0;
    const allMode = (r.plan.transaction?.mode || txMode) === "all";
    if (!r.outcome) {
      return <section className="import-csv-step" aria-label="Import">
        <p className="import-csv-phase" role="status" aria-live="polite">
          <span className="spinner" aria-hidden="true" /> {r.stopping
            ? (pr?.in_flight && r.plan.method.chosen === "load_data" ? "Stopping: ending the running LOAD DATA (KILL QUERY)…" : "Stopping after the statement with the server…")
            : PHASE_TEXT[pr?.phase || ""] || "Sending the reviewed import…"}
        </p>
        <div className="import-csv-meter">
          <progress max={Math.max(1, total)} value={Math.min(sent, total)} aria-label="Rows sent" />
          <span>{`${n(sent)} of ${n(total)} rows`}{pr?.batches_total ? ` · batch ${n(pr.batches_done)} of ${n(pr.batches_total)}` : ""}
            {pr?.rows_committed !== undefined && !allMode ? ` · ${n(pr.rows_committed)} committed` : ""} · {runElapsed} s</span>
        </div>
        <p className="hint">Sent once, never again. {allMode ? "Stop rolls back every row sent so far." : "Stop keeps the rows already committed."}</p>
        {r.stopping && <ActionStatus className="import-csv-line" slowAfter={Number.POSITIVE_INFINITY}
          state={{ phase: "pending", message: `Stopping: ${allMode ? "nothing is kept; the transaction rolls back" : "rows already committed stay"}.`, startedAt: r.stopping }} />}
        {r.stopError && <p className="form-error" role="alert">{r.stopError} <button type="button" className="text-button" onClick={() => void stopImport()}>Try again</button></p>}
      </section>;
    }
    return <section className="import-csv-step" aria-label="Result">{resultBody(r)}</section>;
  }

  function resultBody(r: Run) {
    const o = r.outcome!;
    const [tone, word] = outcomeWord(o);
    if (o.kind !== "result") {
      return <div className="import-csv-outcome" data-outcome={o.kind}>
        <p className="import-csv-outcome-head"><StatusPill tone={tone} label={word} /> {o.message}</p>
        {o.kind === "unknown" ? (
          <p className="hint">If the import reached the server, the Audit log has its record.{" "}
            <button type="button" className="text-button" onClick={() => { setOpen(false); openAuditLog(null); }}>Open the Audit log</button></p>
        ) : o.auditId ? (
          <p className="hint">Recorded in the Audit log.{" "}
            <button type="button" className="text-button" onClick={() => { setOpen(false); openAuditLog(o.auditId); }}>Open the Audit log</button></p>
        ) : null}
      </div>;
    }
    const v = o.value;
    const warnings = serverWarnings(v.warnings);
    const all = (v.transaction?.mode || r.plan.transaction?.mode || txMode) === "all";
    const message = v.message || resultSentence(v, all, targetText);
    return <div className="import-csv-outcome" data-outcome={v.outcome}>
      <p className="import-csv-outcome-head"><StatusPill tone={tone} label={word} /> {message}</p>
      <Fields label="Result" rows={[
        ["Rows committed", n(v.rows.committed)],
        ["Rows sent", n(v.rows.sent)],
        v.rows.skipped ? ["Rows skipped", n(v.rows.skipped)] : null,
        v.failed_at ? ["Failing line", `${v.failed_at.line !== null ? `Line ${v.failed_at.line}: ` : ""}${v.failed_at.message}`] : null,
        v.next_line ? ["First line not committed", `Line ${v.next_line}`] : null,
        serverErrorText(v.server_error) ? ["Server error", serverErrorText(v.server_error)] : null,
        v.table_rows_after !== null && v.table_rows_after !== undefined ? ["The table now has", plural(v.table_rows_after, "row")] : null,
        v.duration_ms !== null && v.duration_ms !== undefined ? ["Time", `${(v.duration_ms / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} s`] : null,
      ]} />
      {!!v.problems?.length && problemList(v.problems, v.problems_total ?? v.problems.length, true, "Rows not imported")}
      {warnings.count > 0 && <Disclosure title="Server warnings" count={warnings.count}>
        <ul className="import-csv-mapping">{warnings.items.map((w, i) => <li key={i}>{warningText(w)}</li>)}</ul>
        {warnings.count > warnings.items.length && <p className="hint">The first {warnings.items.length.toLocaleString()} of {warnings.count.toLocaleString()}.</p>}
      </Disclosure>}
      {v.drop_hint && <div className="import-csv-sql">
        <h3>The new table is still there</h3>
        <p className="hint">It was created before the rows and stays, empty. To remove it, run this yourself; myflames never sends it.</p>
        <CopySql sql={v.drop_hint} label="DROP TABLE statement" />
      </div>}
      <p className="hint">{v.audit?.id ? (v.outcome === "unknown" ? "Recorded in the Audit log as Outcome unknown." : "Recorded in the Audit log.") : "The Audit log has the attempt."}{" "}
        <button type="button" className="text-button" onClick={() => { setOpen(false); openAuditLog(v.audit?.id || null); }}>Open the Audit log</button></p>
    </div>;
  }
}

/** A sentence for a result the server did not word. */
function resultSentence(v: CsvResult, all: boolean, target: string): string {
  const line = v.failed_at?.line ?? v.next_line ?? null;
  if (v.outcome === "done") return `Imported ${plural(v.rows.committed, "row")} into ${target}.${v.rows.skipped ? ` ${plural(v.rows.skipped, "bad row")} skipped.` : ""}`;
  if (v.outcome === "failed") return all ? `Nothing was kept: the import rolled back${line !== null ? ` at line ${line}` : ""}.`
    : `${plural(v.rows.committed, "row")} committed before${line !== null ? ` line ${line}` : " the failure"} stay in ${target}.`;
  if (v.outcome === "stopped") return all ? "Stopped. Nothing was kept: the transaction rolled back." : `Stopped. ${plural(v.rows.committed, "row")} committed stay in ${target}.`;
  return `No reply arrived, so whether rows were committed is unknown. Nothing will be sent again: check ${target} before importing again.`;
}

/** The view's line for an import whose sheet is closed: progress with Show and
 * Stop, then the outcome with Show result and Dismiss. */
function ImportStatus({ run, target, file, other, onShow, onStop, onDismiss }: {
  run: Run; target: string; file: string; other: string; onShow: () => void; onStop: () => void; onDismiss: () => void;
}) {
  const where = other ? ` on ${other}` : "";
  let state: ActionState;
  if (!run.outcome) {
    const pr = run.progress;
    const total = pr?.rows_total ?? run.plan.rows.valid;
    state = { phase: "pending", startedAt: run.startedAt, endedAt: null,
      message: `${run.stopping ? "Stopping the import of" : "Importing"} ${file} into ${target}${where}: ${n(pr?.rows_sent || 0)} of ${n(total)} rows.` };
  } else {
    const [, word] = outcomeWord(run.outcome);
    const o = run.outcome;
    const detail = o.kind === "result" ? `${n(o.value.rows.committed)} rows committed into ${target}${where}.` : o.message;
    state = { phase: o.kind === "unknown" || (o.kind === "result" && o.value.outcome === "unknown") ? "unknown"
      : o.kind === "result" && o.value.outcome === "done" ? "done" : "failed", startedAt: null, endedAt: Date.now(),
    message: `Import CSV: ${word}. ${detail}` };
  }
  return <div className="import-csv-status" data-import-status="">
    <ActionStatus state={state} slowAfter={Number.POSITIVE_INFINITY} onDismiss={run.outcome ? onDismiss : undefined}>
      <button type="button" className="text-button" onClick={onShow}>{run.outcome ? "Show result" : "Show import"}</button>
      {!run.outcome && !run.stopping && <button type="button" className="text-button" onClick={onStop}>Stop</button>}
    </ActionStatus>
  </div>;
}

