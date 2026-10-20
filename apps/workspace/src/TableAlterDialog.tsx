// Table Alter dialogs: Add column and Remove column with workbench-like ease.
// Generates reviewed ALTER TABLE statements executed safely via ReviewedSqlAction.
import { useEffect, useState } from "react";
import {
  fetchTableColumns,
  type Connection,
  type TableColumnInfo,
} from "./api";
import { Icon } from "./components";
import { Checkbox } from "./kit";

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
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-card table-alter-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-col-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-title-wrap">
            <h3 id="add-col-title">
              <Icon name="plus" /> Add Column to {schema}.{table}
            </h3>
            <p className="modal-sub">
              Define the new column. The exact ALTER TABLE statement will be reviewed before execution.
            </p>
          </div>
          <button type="button" className="button icon-only close-button" aria-label="Close" onClick={onClose}>
            <Icon name="close" />
          </button>
        </div>

        <div className="table-alter-form">
          <label className="field-group">
            <span className="field-label">Column Name *</span>
            <input
              type="text"
              className="text-input"
              value={colName}
              placeholder="e.g. is_active, total_amount"
              autoFocus
              onChange={(e) => setColName(e.target.value)}
            />
          </label>

          <div className="form-row-grid">
            <label className="field-group">
              <span className="field-label">Data Type</span>
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
                <span className="field-label">Custom Type</span>
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
              <span className="field-label">Default Value (Optional)</span>
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
                Allow NULL (Nullable)
              </Checkbox>
            </div>
          </div>

          {generatedSql && (
            <div className="sql-preview-box">
              <span className="sql-preview-label">SQL Preview</span>
              <pre>
                <code>{generatedSql}</code>
              </pre>
            </div>
          )}
        </div>

        <div className="modal-foot">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="button primary"
            disabled={!validName || !resolvedType}
            onClick={handleApply}
          >
            Review and Apply…
          </button>
        </div>
      </div>
    </div>
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
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal-card table-alter-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="remove-col-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <div className="modal-title-wrap">
            <h3 id="remove-col-title">
              <Icon name="warning" /> Remove Column from {schema}.{table}
            </h3>
            <p className="modal-sub">
              Select the column to drop. Dropping a column permanently deletes its data.
            </p>
          </div>
          <button type="button" className="button icon-only close-button" aria-label="Close" onClick={onClose}>
            <Icon name="close" />
          </button>
        </div>

        {error && <div className="note danger">{error}</div>}

        <div className="table-alter-form">
          {loading ? (
            <p className="hint">Reading table columns…</p>
          ) : (
            <>
              {isLastCol && (
                <div className="note warn">
                  Cannot drop the only column: a table must have at least one column.
                </div>
              )}
              <label className="field-group">
                <span className="field-label">Select Column to Remove</span>
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
                    <div className="note danger" style={{ marginTop: 8 }}>
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
                  <span className="sql-preview-label">SQL Preview</span>
                  <pre>
                    <code>{generatedSql}</code>
                  </pre>
                </div>
              )}
            </>
          )}
        </div>

        <div className="modal-foot">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="button"
            className="button danger"
            disabled={!selectedCol || loading || isLastCol}
            onClick={handleDrop}
          >
            Review and Drop Column…
          </button>
        </div>
      </div>
    </div>
  );
}
