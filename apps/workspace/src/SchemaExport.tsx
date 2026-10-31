// Export as SQL (Schema versions): the script that recreates a saved version,
// or a database read now through the connection in use, on another server.
// Python writes the script and every sentence (schema_export.py through
// /api/schema-versions/export-sql); this sheet chooses the options and offers
// Copy script, Download .sql and Open in Query editor. myflames never runs the
// script; the Query editor runs it with its own confirmations. A database read
// now is read once: a changed option rebuilds the script from that read.
import { useEffect, useMemo, useRef, useState } from "react";
import { download, type Connection } from "./api";
import { connectionLabel } from "./binding";
import { ActionStatus, Checkbox, Disclosure, FormGrid, FormRow, Pill, Select, copyToClipboard, useAction } from "./kit";
import { TextSheet } from "./TableStructure";
import {
  exportSql, TYPE_LABELS, TYPE_ORDER, TYPE_PLURALS, versionTitle,
  type ExportSql, type ExportSqlOptions, type ExportSqlSource, type VersionItem,
} from "./schemaVersionsApi";

export type ExportSource =
  | { kind: "version"; item: VersionItem }
  | { kind: "live"; connection: Connection; database: string; databases: string[] };

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

function summaryText(result: ExportSql): string {
  const objects = TYPE_ORDER.filter((t) => t !== "database" && (result.summary.objects[t] || 0) > 0)
    .map((t) => `${result.summary.objects[t]} ${TYPE_PLURALS[t][result.summary.objects[t] === 1 ? 0 : 1]}`);
  const parts = [`${plural(result.summary.statements, "statement")} for ${result.database}`,
    objects.join(", ") || "no objects"];
  if (result.summary.not_exported) parts.push(`${result.summary.not_exported.toLocaleString()} not exported`);
  parts.push(result.summary.warnings ? plural(result.summary.warnings, "warning") : "no warnings");
  return parts.join(" · ");
}

/** A long script is shown as plain text: one element per line would slow
 * the sheet down for a large schema (comment lines are then not dimmed). */
const PLAIN_AFTER = 5000;

const optionsKey = (options: ExportSqlOptions, database: string) => JSON.stringify([options, database]);

export function SchemaExportSheet({ source, onOpenSql, onClose }: {
  source: ExportSource;
  /** Open SQL in a new Query editor tab (nothing runs). */
  onOpenSql: (sql: string) => void;
  onClose: () => void;
}) {
  const live = source.kind === "live";
  const [database, setDatabase] = useState(live ? source.database : source.item.database);
  const [options, setOptions] = useState<ExportSqlOptions>({ database, create_database: false, keep_definers: false });
  const [name, setName] = useState(database);
  const nameEdited = useRef(false);
  const [result, setResult] = useState<ExportSql | null>(null);
  const [builtFor, setBuiltFor] = useState("");
  const [copied, setCopied] = useState("");
  const building = useAction();
  // The kept read of a database read now (its database and key).
  const kept = useRef<{ database: string; id: string } | null>(null);

  async function build(next: ExportSqlOptions, db: string) {
    if (live && !db) return;
    building.cancel();
    setCopied("");
    const reuse = live && kept.current?.database === db ? kept.current.id : "";
    const request: ExportSqlSource = !live ? { id: source.item.id }
      : reuse ? { export_id: reuse } : { live: { connection: source.connection, database: db } };
    const outcome = await building.run((signal) => exportSql(request, next, signal), {
      pending: live && !reuse ? `Reading ${db}…` : "Writing the script…", done: "",
    });
    if (outcome.ok) {
      setResult(outcome.value);
      setBuiltFor(optionsKey(next, db));
      if (outcome.value.export_id) kept.current = { database: db, id: outcome.value.export_id };
    } else if (outcome.reason === "failed" && reuse) {
      // The kept read expired: Try again reads the database again.
      kept.current = null;
    }
  }
  useEffect(() => { void build(options, database); }, []);

  // The name as typed: the script, Copy and Download wait until it is used.
  const cleanName = name.trim() || database;
  function change(next: Partial<ExportSqlOptions>) {
    const merged = { ...options, database: cleanName, ...next };
    setOptions(merged);
    void build(merged, database);
  }
  function commitName() {
    const clean = name.trim() || database;
    setName(clean);
    if (clean !== options.database) change({ database: clean });
  }
  function chooseDatabase(db: string) {
    const target = nameEdited.current ? options.database : db;
    if (db === database && (building.pending || builtFor === optionsKey({ ...options, database: target }, db))) return;
    setDatabase(db);
    if (!nameEdited.current) setName(db);
    const merged = { ...options, database: target };
    setOptions(merged);
    void build(merged, db);
  }

  const nameWaits = cleanName !== options.database;
  const current = !!result && !building.pending && !nameWaits && builtFor === optionsKey(options, database);
  const lines = useMemo(() => (result ? result.script.split("\n") : []), [result]);
  const title = live ? database || "Choose a database" : `${source.item.database} · ${versionTitle(source.item)}`;
  const from = live ? `Reads ${database || "the database"} through ${connectionLabel(source.connection)}, read-only.`
    : `From the saved version (${result?.label || source.item.database}).`;
  const notReady = !current ? (nameWaits ? "Press Enter in Name in the script to use the new name" : "Wait for the script to update") : "";
  return (
    <TextSheet eyebrow="Export as SQL" title={title} className="schema-export-sheet" onClose={onClose}
      lead={<>A script that recreates the schema on another server: it creates objects and drops nothing. {from} myflames
        never runs it.</>}
      status={copied}
      actions={<>
        <button type="button" className="button" disabled={!current} title={notReady || "Copy the script"}
          onClick={() => { if (current && result) void copyToClipboard(result.script).then((ok) => setCopied(ok ? "Copied." : "Select the text to copy it.")); }}>
          Copy script
        </button>
        <button type="button" className="button" disabled={!current} title={notReady || (result ? `Save ${result.filename}` : "")}
          onClick={() => { if (current && result) { download(result.script, result.filename, "application/sql"); setCopied(`Saved ${result.filename}.`); } }}>
          Download .sql
        </button>
        <button type="button" className="button primary" disabled={!current}
          title={notReady || "Open the script in a new Query editor tab, on the connection in use; nothing runs until you run it there"}
          onClick={() => { if (current && result) { onOpenSql(result.script); onClose(); } }}>
          Open in Query editor
        </button>
      </>}>
      <FormGrid className="schema-export-options">
        {live && <FormRow label="Database to read" htmlFor="schema-export-source">
          {source.databases.length ? (
            <Select id="schema-export-source" label="Database to read" value={database}
              options={[...(database ? [] : [["", "Choose a database"] as [string, string]]),
                ...(database && !source.databases.includes(database) ? [[database, database] as [string, string]] : []),
                ...source.databases.map((d) => [d, d] as [string, string])]}
              onChange={chooseDatabase} />
          ) : (
            <input id="schema-export-source" value={database} maxLength={64} spellCheck={false} autoCapitalize="off"
              onChange={(e) => setDatabase(e.target.value)} onBlur={() => chooseDatabase(database.trim())}
              onKeyDown={(e) => { if (e.key === "Enter") chooseDatabase(database.trim()); }} />
          )}
        </FormRow>}
        <FormRow label="Name in the script" htmlFor="schema-export-name"
          hint="The script switches to this database; references to the source's name are changed to it.">
          <input id="schema-export-name" value={name} maxLength={64} spellCheck={false} autoCapitalize="off"
            onChange={(e) => { nameEdited.current = true; setName(e.target.value); }}
            onBlur={commitName} onKeyDown={(e) => { if (e.key === "Enter") commitName(); }} />
        </FormRow>
        <FormRow label="Options">
          <div className="schema-export-checks">
          <Checkbox checked={options.create_database} onChange={(value) => change({ create_database: value })}
            title="Start with CREATE DATABASE, with the source's character set and collation. Leave it off to run the script in an existing empty database.">
            Create the database</Checkbox>
          <Checkbox checked={options.keep_definers} onChange={(value) => change({ keep_definers: value })}
            title="Keep the DEFINER of views, routines, triggers and events. Left out, the account that runs the script becomes their definer.">
            Keep definers</Checkbox>
          </div>
        </FormRow>
      </FormGrid>
      <ActionStatus className="schema-export-status" state={building} onStop={building.cancel}
        onRetry={() => void build(options, database)} />
      {live && !database && !result && <p className="muted">Choose a database to read.</p>}
      {result && <div className={`schema-export-result${current ? "" : " is-stale"}`} aria-busy={!current}>
        <p className="schema-export-summary">{summaryText(result)}</p>
        {(result.warnings.length > 0 || result.notes.length > 0) &&
          <Disclosure title="Review before running" count={result.warnings.length || undefined} open={result.warnings.length > 0}>
            <ul className="schema-notes">
              {result.warnings.map((text, i) => <li key={`w${i}`}><Pill tone="warn">Warning</Pill>{text}</li>)}
              {result.notes.map((text, i) => <li key={`n${i}`}>{text}</li>)}
            </ul>
          </Disclosure>}
        {result.not_exported.length > 0 && <p className="schema-export-missing">
          Not in the script: {result.not_exported.map((o) => `${TYPE_LABELS[o.type].toLowerCase()} ${o.name}`).join(", ")}.
        </p>}
        <pre className="schema-script" tabIndex={0} aria-label="Export script">
          {lines.length > PLAIN_AFTER ? result.script
            : lines.map((line, i) => <span key={i} className={line.startsWith("--") ? "sql-comment" : undefined}>{line}{"\n"}</span>)}
        </pre>
      </div>}
    </TextSheet>
  );
}
