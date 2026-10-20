// Table Alter dialogs: Add column and Remove column with workbench-like ease.
// Generates reviewed ALTER TABLE statements executed safely via ReviewedSqlAction.
import { useEffect, useId, useRef, useState, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import {
  fetchTableColumns,
  type Connection,
  type TableColumnInfo,
} from "./api";
import { Icon } from "./components";
import { Checkbox, useFocusScope } from "./kit";

const COMMON_DATA_TYPES = [
  "VARCHAR(255)",
  "VARCHAR(100)",
  "VARCHAR(50)",
  "INT",
  "BIGINT",
  "TINYINT(1)",
  "DATETIME",
  "TIMESTAMP",
  "TEXT",
  "MEDIUMTEXT",
  "LONGTEXT",
  "DECIMAL(10,2)",
  "FLOAT",
  "DOUBLE",
  "JSON",
  "BOOLEAN",
  "DATE",
  "TIME",
  "BLOB",
];

/** A name in backticks, with any backtick doubled. */
const bt = (name: string) => "`" + name.replace(/`/g, "``") + "`";

/** Where focus returns when the sheet closes: the control that opened it, or
 * the button of the menu it was chosen from (that menu closes with the choice).
 * Null lets the focus scope use whatever holds focus once the sheet opens. */
function openerOf(): HTMLElement | null {
  const active = document.activeElement;
  if (!(active instanceof HTMLElement) || active === document.body) return null;
  const menu = active.closest<HTMLElement>('[role="menu"]');
  if (!menu) return active;
  return menu.parentElement?.querySelector<HTMLElement>(':scope > [aria-haspopup="menu"][aria-expanded="true"]') ?? null;
}

/** The modal sheet both dialogs share, built like ConfirmSheet: a title, a
 * body that scrolls, and Cancel plus the action always in view (the 720x520
 * window has 492 px of content). Focus moves in and stays inside; Escape and
 * Cancel close it and return focus to the opener. */
function AlterSheet({
  title,
  lead,
  tone,
  initialFocus,
  confirmLabel,
  confirmDisabled,
  onConfirm,
  onClose,
  children,
}: {
  title: ReactNode;
  lead: ReactNode;
  tone: "primary" | "danger";
  /** Focused first; Cancel when absent (a destructive change). */
  initialFocus?: RefObject<HTMLElement>;
  confirmLabel: ReactNode;
  confirmDisabled: boolean;
  onConfirm: () => void;
  onClose: () => void;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const leadId = useId();
  const [opener] = useState(openerOf);
  useFocusScope(dialog, {
    open: true,
    onClose,
    initialFocus: () => initialFocus?.current || cancel.current,
    returnFocus: () => opener,
  });
  return createPortal(
    <dialog
      ref={dialog}
      className="confirm-sheet table-alter-dialog"
      role={tone === "danger" ? "alertdialog" : "dialog"}
      tabIndex={-1}
      aria-labelledby={titleId}
      aria-describedby={leadId}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <div className="dialog-heading">
        <h2 id={titleId}>{title}</h2>
      </div>
      <div className="sheet-body">
        <p id={leadId} className="sheet-lead">
          {lead}
        </p>
        {children}
      </div>
      <div className="dialog-actions sheet-actions">
        <button ref={cancel} type="button" className="button" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className={`button ${tone === "danger" ? "danger-solid" : "primary"}`}
          disabled={confirmDisabled}
          onClick={onConfirm}
        >
          {confirmLabel}
        </button>
      </div>
    </dialog>,
    document.body,
  );
}

export function AddColumnDialog({
  connection,
  schema,
  table,
  onClose,
  onSubmit,
}: {
  connection: Connection;
  schema: string;
  table: string;
  onClose: () => void;
  onSubmit: (sql: string, label: string) => void;
}) {
  const [colName, setColName] = useState("");
  const [dataType, setDataType] = useState("VARCHAR(255)");
  const [customType, setCustomType] = useState("");
  const [nullable, setNullable] = useState(true);
  const [defaultValue, setDefaultValue] = useState("");
  const [afterCol, setAfterCol] = useState("");
  const [existingCols, setExistingCols] = useState<TableColumnInfo[]>([]);
  const nameInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let active = true;
    fetchTableColumns(connection, schema, table)
      .then((res) => {
        if (active) setExistingCols(res.columns);
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [connection, schema, table]);

  const resolvedType = dataType === "CUSTOM" ? customType.trim() : dataType;
  const validName = colName.trim();

  let generatedSql = "";
  if (validName && resolvedType) {
    let sql = `SET SESSION lock_wait_timeout = 5;\nALTER TABLE ${bt(schema)}.${bt(table)} ADD COLUMN ${bt(validName)} ${resolvedType}`;
    if (!nullable) sql += " NOT NULL";
    else sql += " NULL";
    if (defaultValue.trim()) {
      const def = defaultValue.trim();
      if (
        def.toUpperCase() === "CURRENT_TIMESTAMP" ||
        def.toUpperCase() === "NULL" ||
        /^\d+(\.\d+)?$/.test(def)
      ) {
        sql += ` DEFAULT ${def}`;
      } else {
        sql += ` DEFAULT '${def.replace(/'/g, "''")}'`;
      }
    }
    if (afterCol === "FIRST") {
      sql += " FIRST";
    } else if (afterCol) {
      sql += ` AFTER ${bt(afterCol)}`;
    }
    generatedSql = sql;
  }

  function handleApply() {
    if (!generatedSql) return;
    onSubmit(generatedSql, `Add column ${validName} to ${table}`);
    onClose();
  }

  return (
    <AlterSheet
      title={`Add column to ${schema}.${table}`}
      lead="Define the new column. The exact ALTER TABLE statement will be reviewed before execution."
      tone="primary"
      initialFocus={nameInput}
      confirmLabel="Review and apply…"
      confirmDisabled={!validName || !resolvedType}
      onConfirm={handleApply}
      onClose={onClose}
    >
      <div className="table-alter-form">
        <label className="field-group">
          <span className="field-label">Column name</span>
          <input
            ref={nameInput}
            type="text"
            className="text-input"
            value={colName}
            placeholder="e.g. is_active, total_amount"
            required
            onChange={(e) => setColName(e.target.value)}
          />
        </label>

        <div className="form-row-grid">
          <label className="field-group">
            <span className="field-label">Data type</span>
            <select
              className="field-select"
              value={dataType}
              onChange={(e) => setDataType(e.target.value)}
            >
              {COMMON_DATA_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
              <option value="CUSTOM">Custom…</option>
            </select>
          </label>

          {dataType === "CUSTOM" && (
            <label className="field-group">
              <span className="field-label">Custom type</span>
              <input
                type="text"
                className="text-input"
                value={customType}
                placeholder="e.g. ENUM('a','b')"
                onChange={(e) => setCustomType(e.target.value)}
              />
            </label>
          )}

          <label className="field-group">
            <span className="field-label">Position</span>
            <select
              className="field-select"
              value={afterCol}
              onChange={(e) => setAfterCol(e.target.value)}
            >
              <option value="">Default (at end of table)</option>
              <option value="FIRST">FIRST (first column)</option>
              {existingCols.map((c) => (
                <option key={c.name} value={c.name}>
                  AFTER {c.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="form-row-grid">
          <label className="field-group">
            <span className="field-label">Default value (optional)</span>
            <input
              type="text"
              className="text-input"
              value={defaultValue}
              placeholder="e.g. 0, active, CURRENT_TIMESTAMP"
              onChange={(e) => setDefaultValue(e.target.value)}
            />
          </label>

          <div className="field-group checkbox-field">
            <Checkbox checked={nullable} onChange={setNullable}>
              Allow NULL
            </Checkbox>
          </div>
        </div>

        {generatedSql && (
          <div className="sql-preview-box">
            <span className="sql-preview-label">SQL preview</span>
            <pre>
              <code>{generatedSql}</code>
            </pre>
          </div>
        )}
      </div>
    </AlterSheet>
  );
}

export function RemoveColumnDialog({
  connection,
  schema,
  table,
  onClose,
  onSubmit,
}: {
  connection: Connection;
  schema: string;
  table: string;
  onClose: () => void;
  onSubmit: (sql: string, label: string) => void;
}) {
  const [columns, setColumns] = useState<TableColumnInfo[]>([]);
  const [selectedCol, setSelectedCol] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string>("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    fetchTableColumns(connection, schema, table)
      .then((res) => {
        if (!active) return;
        setColumns(res.columns);
        if (res.columns.length > 0) setSelectedCol(res.columns[0].name);
        setLoading(false);
      })
      .catch((e) => {
        if (!active) return;
        setError((e as Error).message || "Could not load table columns.");
        setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [connection, schema, table]);

  const targetCol = columns.find((c) => c.name === selectedCol);
  const isLastCol = columns.length === 1;
  const isPrimaryKey = targetCol?.key === "PRI";
  const generatedSql = selectedCol
    ? `SET SESSION lock_wait_timeout = 5;\nALTER TABLE ${bt(schema)}.${bt(table)} DROP COLUMN ${bt(selectedCol)}`
    : "";

  function handleDrop() {
    if (!generatedSql || !selectedCol || isLastCol) return;
    onSubmit(generatedSql, `Drop column ${selectedCol} from ${table}`);
    onClose();
  }

  return (
    <AlterSheet
      title={`Remove column from ${schema}.${table}`}
      lead="Select the column to drop. Dropping a column permanently deletes its data."
      tone="danger"
      confirmLabel="Review and drop column…"
      confirmDisabled={!selectedCol || loading || isLastCol}
      onConfirm={handleDrop}
      onClose={onClose}
    >
      {error && <div className="note danger" role="alert">{error}</div>}

      <div className="table-alter-form">
        {loading ? (
          <p className="hint" role="status">Reading table columns…</p>
        ) : (
          <>
            {isLastCol && (
              <div className="note warn">
                Cannot drop the only column: a table must have at least one column.
              </div>
            )}
            <label className="field-group">
              <span className="field-label">Column to remove</span>
              <select
                className="field-select"
                value={selectedCol}
                onChange={(e) => setSelectedCol(e.target.value)}
              >
                {columns.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name} ({c.type}) {c.key ? `[${c.key}]` : ""}
                  </option>
                ))}
              </select>
            </label>

            {targetCol && (
              <div className="col-info-card">
                <div className="col-info-row">
                  <span>Type:</span> <strong>{targetCol.type}</strong>
                </div>
                <div className="col-info-row">
                  <span>Nullable:</span> {targetCol.nullable ? "YES" : "NO"}
                </div>
                {targetCol.default != null && (
                  <div className="col-info-row">
                    <span>Default:</span> <code>{targetCol.default}</code>
                  </div>
                )}
                {isPrimaryKey ? (
                  <div className="note danger">
                    <Icon name="warning" /> <strong>Primary Key Column:</strong> Dropping this column directly will fail because it is part of the primary key!
                  </div>
                ) : targetCol.key ? (
                  <div className="col-info-row warn-key">
                    <span>Key:</span> <strong>{targetCol.key}</strong> (Dropping an indexed column may affect constraints or secondary indexes!)
                  </div>
                ) : null}
              </div>
            )}

            <div className="note danger">
              <Icon name="warning" /> <strong>Warning:</strong> This operation will drop the column and all stored values in it.
            </div>

            {generatedSql && !isLastCol && (
              <div className="sql-preview-box">
                <span className="sql-preview-label">SQL preview</span>
                <pre>
                  <code>{generatedSql}</code>
                </pre>
              </div>
            )}
          </>
        )}
      </div>
    </AlterSheet>
  );
}
