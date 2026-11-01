// Import CSV: typed calls for /api/import/csv/* (csv_import_api.py; contracts
// csv-inspect-1.0, csv-import-plan-1.0 and csv-import-result-1.0 in
// docs/schemas/), the pure helpers the wizard uses (separator names, column
// matching, names from a file), the entry-point gate, and the owner hook that
// keeps one import per view (the wizard is a lazy chunk, ImportCsv.tsx).
//
// Safety (db-operations): an import is a write. Choosing and inspecting a
// file reads only this computer; planning reads the server; only the reviewed
// plan's token is sent to apply, once, and an answer that never comes is an
// unknown outcome, never retried.
import { createElement, useCallback, useRef, useState, type ReactNode } from "react";
import { api, type Connection } from "./api";
import { connectionKey } from "./binding";
import { useWritePolicy } from "./Connections";
import { lazyComponent } from "./lazy";
import { usePrivilege, usePrivileges } from "./privileges";

// ---- Contracts ------------------------------------------------------------------

/** How the file is read. `detected` in csv-inspect-1.0 uses the same keys. */
export type CsvFormat = {
  /** "utf-8", "utf-8-sig", "utf-16-le", "utf-16-be" or "cp1252". */
  encoding: string;
  /** One character: "," ";" "\t" "|" or any other. */
  separator: string;
  /** '"', "'" or "" (none). */
  quote: string;
  /** "" (quotes doubled) or "\\". */
  escape: string;
  /** The first row holds column names. */
  header: boolean;
  line_ending: string;
  /** Lines skipped before the header (or the first row). */
  skip_lines: number;
  /** What an empty field becomes. */
  empty: "null" | "empty";
  /** Fields equal to one of these are NULL: a subset of ["\\N", "NULL"] (default ["\\N"]). */
  null_markers?: string[];
};
export type CsvFile = { file_id: string; name: string; size: number; modified: string | number | null };
export type CsvChoice = CsvFile | { cancelled: true };
export type CsvColumn = {
  index: number;
  header: string;
  /** The column name suggested for a new table (unique, at most 64 characters). */
  name?: string;
  inferred: { sql_type: string; nullable: boolean; max_length: number | null };
  samples: string[];
};
export type CsvProblem = { line: number; message: string; column?: string | null; value?: string | null };
export type CsvInspect = {
  schema_version: "csv-inspect-1.0";
  file: { file_id?: string; name: string; size: number; modified: string | number | null };
  detected: Partial<CsvFormat>;
  format: CsvFormat;
  columns: CsvColumn[];
  /** null is a NULL by the format; lines are the CSV line of each row. */
  preview: { rows: (string | null)[][]; lines?: number[]; truncated: boolean };
  /** Exact count when the whole file was read. */
  rows?: number | null;
  rows_estimate?: number | null;
  rows_counted?: boolean;
  problems: CsvProblem[];
  problems_total?: number;
  notes?: string[];
};
export type CsvMapping = { csv_index: number | null; column: string };
export type CsvNewColumn = { name: string; sql_type: string; nullable: boolean };
export type CsvTargetRequest = {
  mode: "existing" | "new";
  schema: string;
  table: string;
  mapping: CsvMapping[];
  new_columns?: CsvNewColumn[];
  add_id_key?: boolean;
};
export type CsvMethod = "insert" | "load_data";
export type CsvTransaction = { mode: "all" | "every"; every: number };
export type CsvOnError = "stop" | "skip";
export type CsvPlanRequest = {
  connection: Connection;
  file_id: string;
  format: CsvFormat;
  target: CsvTargetRequest;
  method: CsvMethod;
  transaction: CsvTransaction;
  on_error: CsvOnError;
  /** Follow the check through /progress and cancel it with /stop. */
  progress_id?: string;
};
export type CsvPrivilege = { action?: string; state: "allowed" | "denied" | "unknown"; reason: string; need?: string };
export type CsvPolicy = {
  action: "allow" | "confirm" | "refuse";
  tone?: "danger" | "primary";
  title?: string;
  text: string;
  connection_type?: "production" | "development";
  read_only?: boolean;
};
export type CsvPlan = {
  schema_version: "csv-import-plan-1.0";
  /** Null while the plan cannot run (see blockers). */
  token: string | null;
  ready?: boolean;
  /** Why it cannot run now: bad rows with Stop, all or nothing on MyISAM, a denied privilege, Read-only. */
  blockers?: string[];
  expires_in?: number;
  file?: { file_id: string; name: string; size: number; modified: string | number | null; sha256?: string };
  connection?: { name?: string; host: string; port: number; user: string; type?: string; read_only?: boolean;
    engine?: string; server_version?: string; server_product?: string };
  target: {
    schema?: string;
    table?: string;
    exists: boolean;
    engine: string | null;
    columns?: { name: string; type: string; nullable?: boolean }[];
    rows_estimate: number | null;
    triggers: number | null;
    foreign_keys: number | null;
    primary_key: string[] | null;
    transactional?: boolean | null;
    check_constraints?: number | null;
  };
  mapping: (CsvMapping & { csv_header?: string | null; type?: string | null })[];
  /** setup: the session statements sent first (sql_mode, lock wait timeouts). */
  statements: { create: string | null; data_sample: string | null; data_summary: string; setup?: string[] };
  batches: number;
  rows: { total: number; valid: number; invalid: number; skipped: number };
  problems: CsvProblem[];
  problems_total?: number;
  method: { chosen: CsvMethod; load_data_available: boolean; local_infile: boolean | null; why: string };
  transaction: CsvTransaction & { commits?: number | null };
  on_error?: CsvOnError;
  /** A new table: the DROP TABLE to run by hand if the rows fail (the table stays). */
  drop_hint?: string | null;
  privileges: CsvPrivilege[];
  policy: CsvPolicy | null;
  warnings: string[];
  notes?: string[];
};
export type CsvServerWarning = string | { level?: string; code?: number | string; message?: string };
export type CsvOutcome = "done" | "failed" | "stopped" | "unknown";
export type CsvResult = {
  schema_version: "csv-import-result-1.0";
  outcome: CsvOutcome;
  rows: { total?: number; sent: number; committed: number; skipped: number };
  failed_at: { line: number | null; message: string } | null;
  /** The first CSV line not committed (null when every row was). */
  next_line?: number | null;
  last_committed_line?: number | null;
  server_error: { code: number | null; message: string } | string | null;
  /** LOAD DATA: the count and the first 20. */
  warnings?: { count: number; first?: CsvServerWarning[]; items?: CsvServerWarning[] } | CsvServerWarning[] | null;
  duration_ms: number | null;
  table_rows_after: number | null;
  /** The exact DROP TABLE for a new table left empty; never sent by myflames. */
  drop_hint: string | null;
  audit: { id: string | null } | null;
  message?: string;
  /** Rows not imported (skipped, or refused by the server), at most 100. */
  problems?: CsvProblem[];
  problems_total?: number;
  table_created?: boolean;
  method?: CsvMethod;
  transaction?: CsvTransaction;
};
/** csv-import-progress-1.0: a check (kind "plan") or an import (kind "apply"). */
export type CsvProgress = {
  schema_version?: string;
  kind?: "plan" | "apply";
  phase: string;
  rows_total: number | null;
  rows_checked?: number;
  rows_sent: number;
  rows_committed?: number;
  batches_done: number;
  batches_total: number | null;
  /** A data statement is with the server now (Stop then ends LOAD DATA with KILL QUERY). */
  in_flight?: boolean;
  started_at: string | number | null;
  elapsed_s?: number | null;
  stop_requested?: boolean;
};

// ---- Calls ----------------------------------------------------------------------

/** The native chooser (.csv, .tsv, .txt). */
export const chooseCsvFile = () => api<CsvChoice>("import/csv/choose", {});
/** The typed-path fallback: an existing regular file of at most 4 GiB. */
export const openCsvPath = (path: string) => api<CsvChoice>("import/csv/open-path", { path });
/** Detect (no format) or re-read with the chosen format; reads only this computer. */
export const inspectCsv = (fileId: string, format: CsvFormat | null, signal?: AbortSignal) =>
  api<CsvInspect>("import/csv/inspect", format ? { file_id: fileId, format } : { file_id: fileId }, signal);
/** Reads the target and checks every row; sends nothing that changes the server. */
export const planCsvImport = (request: CsvPlanRequest, signal?: AbortSignal) => api<CsvPlan>("import/csv/plan", request, signal);
/** Sends the reviewed plan once. A reply with an error (4xx) means nothing was sent. */
export const applyCsvImport = (connection: Connection, token: string, progressId: string) =>
  api<CsvResult>("import/csv/apply", { connection, token, progress_id: progressId });
export const csvImportProgress = (progressId: string, signal?: AbortSignal) =>
  api<CsvProgress>("import/csv/progress", { progress_id: progressId }, signal);
/** Between batches: all or nothing rolls back, Commit every N keeps the committed rows. */
export const stopCsvImport = (progressId: string) =>
  api<{ stop_requested: boolean; message?: string }>("import/csv/stop", { progress_id: progressId });

// ---- Pure helpers (importCsvRules.ts) ---------------------------------------------

export * from "./importCsvRules";

// ---- Entry points ---------------------------------------------------------------

/** What an entry point opens with: an existing table, or a new table in a database. */
export type ImportCsvTarget = { mode: "existing" | "new"; schema: string; table?: string };

/**
 * Why Import CSV cannot start on the connection in use, or "" when it can:
 * Read-only, the write policy not known yet, or a privilege SHOW GRANTS
 * proves missing (INSERT; CREATE too for a new table). Unknown leaves it
 * enabled: the server decides, and the review names the privileges again.
 */
export function useImportCsvGate(mode: "existing" | "new"): string {
  const context = usePrivileges();
  const policy = useWritePolicy();
  const insert = usePrivilege("insert");
  const create = usePrivilege("create_table");
  if (context?.readOnly) return "This connection is read-only.";
  if (policy?.blocked) return policy.blocked;
  if (mode === "new" && create.disabledReason) return create.disabledReason;
  return insert.disabledReason || "";
}

const ImportCsvSheet = lazyComponent(() => import("./ImportCsv").then((m) => m.ImportCsv));
/** One import of a view: its connection (as it was when opened) and target. */
export type ImportCsvSession = { id: number; connection: Connection; target: ImportCsvTarget };

/**
 * The owner side of Import CSV (Schemas panel, Server > Tables): `start`
 * opens the wizard with a pre-filled target; `element` is placed where its
 * status line belongs (the wizard itself is a modal sheet). The import stays
 * mounted after its sheet closes, so a slow import and its outcome stay in
 * the view. While one runs, `start` shows it again instead of a second one.
 */
export function useImportCsv(connection: Connection): { start: (target: ImportCsvTarget) => void; element: ReactNode; running: boolean } {
  const [session, setSession] = useState<ImportCsvSession | null>(null);
  const [show, setShow] = useState(0);
  const [running, setRunning] = useState(false);
  const runningRef = useRef(false);
  const seq = useRef(0);
  const latest = useRef(connection);
  latest.current = connection;
  const start = useCallback((target: ImportCsvTarget) => {
    if (runningRef.current) { setShow((n) => n + 1); return; }
    seq.current += 1;
    setSession({ id: seq.current, connection: { ...latest.current }, target });
  }, []);
  const element = session
    ? createElement(ImportCsvSheet, {
      key: session.id,
      session,
      currentKey: connectionKey(connection),
      show,
      onRunning: (value: boolean) => { runningRef.current = value; setRunning(value); },
      onEnd: () => { runningRef.current = false; setRunning(false); setSession(null); },
    })
    : null;
  return { start, element, running };
}
