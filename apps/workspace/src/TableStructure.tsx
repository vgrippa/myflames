// Table structure (Server > Tables, the inspector's action bar and the row
// menus): one table's columns, indexes, foreign keys and its exact SHOW CREATE
// TABLE, read when the sheet opens from the connection the list was read
// with (/api/live/table-structure, table-structure-1.0 from
// myflames/schema_browser.py). Read-only: Copy and Download .sql only.
// TextSheet, the dialog shell, is shared with Schema versions' Export as SQL.
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { api, download, type Connection } from "./api";
import { connectionLabel } from "./binding";
import { ActionStatus, Segmented, copyToClipboard, useAction, useFocusScope } from "./kit";
import "./table-structure.css";

export type TableStructureColumn = {
  name: string;
  position: number | null;
  type: string;
  nullable: boolean;
  /** COLUMN_DEFAULT as the server shows it (MariaDB quotes strings and writes NULL). */
  default: string | null;
  extra: string;
  comment: string;
  character_set: string | null;
  collation: string | null;
  key: string;
  generated: string | null;
};
export type TableStructureIndex = {
  name: string;
  primary: boolean;
  unique: boolean;
  type: string;
  comment: string;
  /** IS_VISIBLE (MySQL 8.0+) or not IGNORED (MariaDB 10.6+); null when the server has neither. */
  visible: boolean | null;
  columns: { name: string | null; expression: string | null; sub_part: number | null; order: "asc" | "desc" | null }[];
};
export type TableStructureForeignKey = {
  name: string;
  columns: string[];
  referenced_schema: string | null;
  referenced_table: string | null;
  referenced_columns: string[];
  on_update: string | null;
  on_delete: string | null;
};
export type TableStructure = {
  schema_version: "table-structure-1.0";
  schema: string;
  table: string;
  type: "table" | "view" | "system view" | "sequence";
  engine: string | null;
  collation: string | null;
  comment: string;
  columns: TableStructureColumn[];
  indexes: TableStructureIndex[];
  foreign_keys: TableStructureForeignKey[];
  /** The exact SHOW CREATE TABLE text, or null with create_error. */
  create_table: string | null;
  create_error: string | null;
  /** "<schema>.<table>.sql" */
  filename: string;
};

export const readTableStructure = (connection: Connection, schema: string, table: string, signal?: AbortSignal) =>
  api<TableStructure>("live/table-structure", { connection, schema, table }, signal);

/** Where focus returns when a sheet closes: the control that opened it, or
 * the ⋯ button of the menu it was chosen from. */
function openerOf(): HTMLElement | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || active === document.body) return null;
  const menu = active.closest<HTMLElement>('[role="menu"]');
  if (!menu) return active;
  return menu.parentElement?.querySelector<HTMLElement>(':scope > [aria-haspopup="menu"][aria-expanded="true"]') ?? null;
}

/** A modal sheet for reading and taking away text (Table structure, Export
 * as SQL): a title, a body that scrolls, then one row of actions that never
 * leaves the window, Close first. Escape and Close return focus to the opener. */
export function TextSheet({ eyebrow, title, lead, className = "", status, actions, onClose, children }: {
  eyebrow: string;
  title: ReactNode;
  lead?: ReactNode;
  className?: string;
  /** A short outcome beside the actions ("Copied."). */
  status?: ReactNode;
  /** Buttons after Close; the last one is the primary action. */
  actions?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const leadId = useId();
  const [opener] = useState(openerOf);
  useFocusScope(dialog, { open: true, onClose, initialFocus: () => close.current, returnFocus: () => opener });
  return createPortal(
    <dialog ref={dialog} className={`confirm-sheet text-sheet ${className}`.trim()} role="dialog" tabIndex={-1}
      aria-labelledby={titleId} aria-describedby={lead ? leadId : undefined}
      onCancel={(e) => { e.preventDefault(); onClose(); }}>
      <div className="dialog-heading">
        <div>
          <div className="eyebrow">{eyebrow}</div>
          <h2 id={titleId}>{title}</h2>
        </div>
      </div>
      <div className="sheet-body">
        {lead && <p id={leadId} className="sheet-lead">{lead}</p>}
        {children}
      </div>
      <div className="dialog-actions sheet-actions text-sheet-actions">
        <span className="text-sheet-status" role="status" aria-live="polite">{status}</span>
        <div className="sheet-buttons">
          <button ref={close} type="button" className="button" onClick={onClose}>Close</button>
          {actions}
        </div>
      </div>
    </dialog>,
    document.body,
  );
}

type Tab = "columns" | "indexes" | "foreign_keys" | "create";

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

/** The Table structure sheet: reads once when it opens (Try again after a
 * failure), from *connection*, the connection the Tables list was read with. */
export function TableStructureSheet({ connection, schema, table, onClose }: {
  connection: Connection;
  schema: string;
  table: string;
  onClose: () => void;
}) {
  const reading = useAction(`${schema}.${table}`);
  const [report, setReport] = useState<TableStructure | null>(null);
  const [tab, setTab] = useState<Tab>("columns");
  const [copied, setCopied] = useState("");
  const name = `${schema}.${table}`;
  async function load() {
    const result = await reading.run((signal) => readTableStructure(connection, schema, table, signal), {
      pending: `Reading the structure of ${name}…`, done: "",
    });
    if (result.ok) setReport(result.value);
  }
  useEffect(() => { void load(); }, []);
  const ddl = report?.create_table ? `${report.create_table.replace(/;?\s*$/, "")};\n` : "";
  const facts = report ? [
    report.type !== "table" ? report.type[0].toUpperCase() + report.type.slice(1) : report.engine || "",
    report.collation || "",
    `${plural(report.columns.length, "column")}, ${plural(report.indexes.length, "index", "indexes")}, ${plural(report.foreign_keys.length, "foreign key")}`,
  ].filter(Boolean).join(" · ") : "";
  return (
    <TextSheet eyebrow="Table structure" title={name} className="table-structure-sheet" onClose={onClose}
      lead={<>{facts ? `${facts} · ` : ""}Read from {connectionLabel(connection)}. Nothing on the server changes.</>}
      status={copied}
      actions={<>
        <button type="button" className="button" disabled={!ddl}
          title={ddl ? "Copy the CREATE TABLE statement" : report?.create_error || "Wait for the structure"}
          onClick={() => void copyToClipboard(ddl).then((ok) => setCopied(ok ? "Copied." : "Select the text to copy it."))}>
          Copy CREATE TABLE
        </button>
        <button type="button" className="button primary" disabled={!ddl || !report}
          title={ddl && report ? `Save ${report.filename}` : report?.create_error || "Wait for the structure"}
          onClick={() => { if (ddl && report) { download(ddl, report.filename, "application/sql"); setCopied(`Saved ${report.filename}.`); } }}>
          Download .sql
        </button>
      </>}>
      <ActionStatus className="table-structure-status" state={reading} onStop={reading.cancel} onRetry={() => void load()} />
      {report && <>
        {report.comment && <p className="table-structure-comment">{report.comment}</p>}
        <Segmented<Tab> label="Table structure" value={tab} onChange={setTab} tabs="table-structure"
          options={[
            { value: "columns", label: "Columns", count: report.columns.length },
            { value: "indexes", label: "Indexes", count: report.indexes.length },
            { value: "foreign_keys", label: "Foreign keys", count: report.foreign_keys.length },
            { value: "create", label: "CREATE TABLE" },
          ]} />
        <div id={`table-structure-panel-${tab}`} role="tabpanel" aria-labelledby={`table-structure-tab-${tab}`}
          className="table-structure-panel">
          {tab === "columns" ? <ColumnsTable columns={report.columns} />
            : tab === "indexes" ? <IndexesTable indexes={report.indexes} />
            : tab === "foreign_keys" ? <ForeignKeysTable keys={report.foreign_keys} schema={report.schema} />
            : report.create_table
              ? <pre className="table-structure-ddl" tabIndex={0} aria-label={`CREATE TABLE of ${name}`}>{report.create_table}</pre>
              : <p className="form-error" role="alert">{report.create_error}</p>}
        </div>
      </>}
    </TextSheet>
  );
}

function Muted({ children, title }: { children: ReactNode; title?: string }) {
  return <span className="muted" title={title}>{children}</span>;
}

function defaultText(c: TableStructureColumn): ReactNode {
  if (c.generated) return <Muted title={`Generated: ${c.generated}`}>Generated</Muted>;
  if (c.default === null) return c.nullable ? <Muted>NULL</Muted> : <Muted title="No default value">—</Muted>;
  return <code>{c.default}</code>;
}

function ColumnsTable({ columns }: { columns: TableStructureColumn[] }) {
  if (!columns.length) return <p className="muted">No columns are visible to this account.</p>;
  return <div className="table-structure-frame" tabIndex={0} role="region" aria-label="Columns">
    <table className="table-structure-table">
      <thead><tr>
        <th scope="col">Name</th><th scope="col">Type</th><th scope="col">Null</th><th scope="col">Default</th>
        <th scope="col">Extra</th><th scope="col">Character set and collation</th><th scope="col">Comment</th>
      </tr></thead>
      <tbody>{columns.map((c) => <tr key={c.name}>
        <th scope="row"><code>{c.name}</code>{c.key === "PRI" && <span className="table-structure-key" title="Part of the primary key">PK</span>}</th>
        <td><code>{c.type}</code>{c.generated && <span className="table-structure-generated" title={c.generated}> AS ({c.generated})</span>}</td>
        <td>{c.nullable ? "Yes" : "No"}</td>
        <td>{defaultText(c)}</td>
        <td>{c.extra || <Muted>—</Muted>}</td>
        <td>{c.character_set ? `${c.character_set} / ${c.collation || "—"}` : <Muted>—</Muted>}</td>
        <td className="table-structure-text">{c.comment || <Muted>—</Muted>}</td>
      </tr>)}</tbody>
    </table>
  </div>;
}

function indexColumns(index: TableStructureIndex): string {
  return index.columns.map((c) => {
    const part = c.expression ? `(${c.expression})` : c.name || "?";
    return `${part}${c.sub_part ? `(${c.sub_part})` : ""}${c.order === "desc" ? " DESC" : ""}`;
  }).join(", ");
}

function IndexesTable({ indexes }: { indexes: TableStructureIndex[] }) {
  if (!indexes.length) return <p className="muted">This table has no indexes.</p>;
  return <div className="table-structure-frame" tabIndex={0} role="region" aria-label="Indexes">
    <table className="table-structure-table">
      <thead><tr>
        <th scope="col">Name</th><th scope="col">Columns</th><th scope="col">Unique</th><th scope="col">Type</th>
        <th scope="col" title="No for an invisible index (MySQL) or an ignored index (MariaDB): the optimizer leaves it out (on MySQL unless use_invisible_indexes is on)">Visible</th>
        <th scope="col">Comment</th>
      </tr></thead>
      <tbody>{indexes.map((x) => <tr key={x.name}>
        <th scope="row"><code>{x.name}</code></th>
        <td><code>{indexColumns(x)}</code></td>
        <td>{x.primary ? "Primary key" : x.unique ? "Yes" : "No"}</td>
        <td>{x.type || <Muted>—</Muted>}</td>
        <td>{x.visible === null ? <Muted title="The server does not report it">—</Muted> : x.visible ? "Yes" : "No"}</td>
        <td className="table-structure-text">{x.comment || <Muted>—</Muted>}</td>
      </tr>)}</tbody>
    </table>
  </div>;
}

function ForeignKeysTable({ keys, schema }: { keys: TableStructureForeignKey[]; schema: string }) {
  if (!keys.length) return <p className="muted">This table has no foreign keys.</p>;
  return <div className="table-structure-frame" tabIndex={0} role="region" aria-label="Foreign keys">
    <table className="table-structure-table">
      <thead><tr>
        <th scope="col">Name</th><th scope="col">Columns</th><th scope="col">References</th>
        <th scope="col">On update</th><th scope="col">On delete</th>
      </tr></thead>
      <tbody>{keys.map((k) => <tr key={k.name}>
        <th scope="row"><code>{k.name}</code></th>
        <td><code>{k.columns.join(", ")}</code></td>
        <td><code>{k.referenced_schema && k.referenced_schema !== schema ? `${k.referenced_schema}.` : ""}{k.referenced_table} ({k.referenced_columns.join(", ")})</code></td>
        <td>{k.on_update || <Muted>—</Muted>}</td>
        <td>{k.on_delete || <Muted>—</Muted>}</td>
      </tr>)}</tbody>
    </table>
  </div>;
}
