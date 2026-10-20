// Server > Tables: tables without a primary key or a usable unique key, and
// InnoDB free space (schema-2.0 from myflames/schema_health.py). One read per
// click over the chosen databases (all user databases by default); myflames
// runs an optional OPTIMIZE only after a separate SQL review. One sortable list at a
// time; the why, the checks and the fix statements are in the inspector.
import { useEffect, useMemo, useRef, useState } from "react";
import { useOpenRead, useViewOnScreen } from "./viewOnScreen";
import { api, type Connection, type FreeSpaceRow, type MissingPrimaryKey, type TablesReport, type TableInventoryRow } from "./api";
import { CompactNumber, CopySql, SchemaPicker, bytes, count, engineLabel, useSchemaCatalog } from "./PerfShared";
import { uptime } from "./Health";
import { useConnectionReads, type Bound } from "./binding";
import {
  Checkbox,
  closePopovers,
  EmptyState,
  FactStrip,
  Fields,
  GroupedList,
  GroupedRow,
  InfoPopover,
  InspectorSection,
  Pill,
  PrerequisiteGate,
  SearchField,
  Segmented,
  StatusPill,
  SummaryLine,
  ToolbarSelect,
  ViewLayout,
  ViewStatus,
  type Column,
  type InspectorContent,
  type Selection,
  type Tone,
  copyToClipboard,
} from "./kit";
import "./tables.css";
import { PaginatedReportTable } from "./PaginatedReportTable";
import { ReviewedSqlAction, ReviewedSqlBridger, ReviewedSqlProvider, ReviewedSqlStatus, useReviewedSql } from "./ReviewedSqlAction";
import { Icon } from "./components";
import { AddColumnDialog, RemoveColumnDialog } from "./TableAlterDialog";

export type TableFocus = { schema: string; table: string } | null;
const STATUS_RANK: Record<string, number> = { problem: 0, watch: 1, info: 2, ok: 3, shared: 4, unknown: 5 };
const STATUS_LABEL: Record<string, string> = {
  problem: "Problem",
  watch: "Watch",
  info: "Reusable space",
  ok: "OK",
  shared: "Shared tablespace",
  unknown: "Not judged",
};
function statusTone(status: string): Tone {
  return status === "problem" || status === "watch" || status === "ok" ? status : (status === "shared" || status === "info") ? "info" : "unknown";
}
function Status({ status }: { status: string }) {
  return (
    <span className={`tables-status is-${status}`}>
      <StatusPill tone={statusTone(status)} label={STATUS_LABEL[status] || status} />
    </span>
  );
}

function Statements({ items, hint }: { items: { label: string; sql: string; template?: boolean }[]; hint: string }) {
  if (!items.length) return null;
  return (
    <>
      <p className="hint">{hint}</p>
      {items.map((x) => (
        <div key={x.sql}>
          <p className="tables-statement-label">{x.label}</p>
          <CopySql sql={x.sql} label={x.label} />
        </div>
      ))}
    </>
  );
}

function scopeLabel(schemas: string[] | null) {
  if (!schemas || !schemas.length) return "All user databases";
  return schemas.length <= 3 ? schemas.join(", ") : `${schemas.length} databases`;
}

type List = "all" | "missing" | "free";

function TableRowActions({
  schema,
  table,
  onAddColumn,
  onRemoveColumn,
  onIndexes,
  disabled,
}: {
  schema: string;
  table: string;
  onAddColumn: () => void;
  onRemoveColumn: () => void;
  onIndexes: () => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const reviewedSql = useReviewedSql();

  useEffect(() => {
    if (!open) return;
    const handleOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", handleOutside);
    return () => window.removeEventListener("mousedown", handleOutside);
  }, [open]);

  return (
    <div className="table-row-actions-wrap" ref={ref} onClick={(e) => e.stopPropagation()}>
      <button
        type="button"
        className="button small table-actions-trigger"
        disabled={disabled}
        onClick={() => setOpen(!open)}
        title={`Actions for ${schema}.${table}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        Actions ▾
      </button>
      {open && (
        <div className="table-actions-menu" role="menu">
          <button
            type="button"
            className="table-action-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onAddColumn();
            }}
          >
            <Icon name="plus" /> Add Column
          </button>
          <button
            type="button"
            className="table-action-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onRemoveColumn();
            }}
          >
            <Icon name="close" /> Remove Column
          </button>
          <div className="table-action-divider" />
          <button
            type="button"
            className="table-action-item"
            role="menuitem"
            disabled={reviewedSql?.blocked}
            onClick={() => {
              setOpen(false);
              reviewedSql?.start(`OPTIMIZE TABLE \`${schema}\`.\`${table}\``, `Optimize ${table}`);
            }}
          >
            <Icon name="optimize" /> Optimize Table
          </button>
          <button
            type="button"
            className="table-action-item"
            role="menuitem"
            disabled={reviewedSql?.blocked}
            onClick={() => {
              setOpen(false);
              reviewedSql?.start(`ANALYZE TABLE \`${schema}\`.\`${table}\``, `Analyze ${table}`);
            }}
          >
            <Icon name="statements" /> Analyze Table
          </button>
          <button
            type="button"
            className="table-action-item"
            role="menuitem"
            disabled={reviewedSql?.blocked}
            onClick={() => {
              setOpen(false);
              reviewedSql?.start(`CHECK TABLE \`${schema}\`.\`${table}\``, `Check ${table}`);
            }}
          >
            <Icon name="shield" /> Check Table
          </button>
          <div className="table-action-divider" />
          <button
            type="button"
            className="table-action-item"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onIndexes();
            }}
          >
            <Icon name="index" /> View Indexes
          </button>
        </div>
      )}
    </div>
  );
}

function TableAlterRunner({
  alter,
  connection,
  onClose,
}: {
  alter: { mode: "add" | "remove"; schema: string; table: string } | null;
  connection: Connection;
  onClose: () => void;
}) {
  const reviewedSql = useReviewedSql();
  if (!alter) return null;

  if (alter.mode === "add") {
    return (
      <AddColumnDialog
        connection={connection}
        schema={alter.schema}
        table={alter.table}
        onClose={onClose}
        onSubmit={(sql, label) => {
          reviewedSql?.start(sql, label);
        }}
      />
    );
  }

  return (
    <RemoveColumnDialog
      connection={connection}
      schema={alter.schema}
      table={alter.table}
      onClose={onClose}
      onSubmit={(sql, label) => {
        reviewedSql?.start(sql, label);
      }}
    />
  );
}

export function TablesView({
  connection,
  disabled,
  onBack,
  selection,
  schemas,
  onSchemasChange,
  report: bound,
  onReport,
  loadRequest,
  onLoadRequestDone,
  focus,
  onFocusChange,
  onOpenIndexes,
  onOpenSql,
}: {
  connection: Connection;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  /** Chosen databases; empty means all user databases. */
  schemas: string[];
  onSchemasChange: (schemas: string[]) => void;
  report: Bound<TablesReport> | null;
  onReport: (report: Bound<TablesReport>) => void;
  /** Set when another view links here: load once, then clear it. */
  loadRequest: number;
  onLoadRequestDone: () => void;
  /** A table another view linked to: only its rows are shown until cleared. */
  focus: TableFocus;
  onFocusChange: (focus: TableFocus) => void;
  /** Reads Indexes for this report's table; *from* is the report's connection key. */
  onOpenIndexes: (schema: string, table: string, from: string) => void;
  /** Put a statement in the Query editor's current tab, without running it. */
  onOpenSql?: (sql: string) => void;
}) {
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const [fresh, setFresh] = useState(false);
  const [filter, setFilter] = useState("");
  const [list, setList] = useState<List>("all");
  const [tableAlter, setTableAlter] = useState<{ mode: "add" | "remove"; schema: string; table: string } | null>(null);
  const reviewedSqlRef = useRef<ReturnType<typeof useReviewedSql>>(null);
  const currentList = list === "all" && !report?.tables ? "missing" : list;

  const getTableRowContextMenu = (schema: string, table: string) => {
    const runner = reviewedSqlRef.current;
    const qTable = `\`${schema}\`.\`${table}\``;
    return [
      ...(onOpenSql
        ? [
            {
              label: "Open in Query Editor",
              icon: "query",
              onClick: () => {
                onOpenSql(`SELECT * FROM ${qTable} LIMIT 100;`);
              },
            },
            {
              label: "Show CREATE TABLE (DDL)",
              icon: "table",
              onClick: () => {
                onOpenSql(`SHOW CREATE TABLE ${qTable};`);
              },
            },
            { divider: true as const },
          ]
        : []),
      {
        label: "Optimize table",
        icon: "optimize",
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(`OPTIMIZE TABLE ${qTable}`, `Optimize ${table}`);
        },
      },
      {
        label: "Analyze table",
        icon: "statements",
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(`ANALYZE TABLE ${qTable}`, `Analyze ${table}`);
        },
      },
      {
        label: "Check table",
        icon: "shield",
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(`CHECK TABLE ${qTable}`, `Check ${table}`);
        },
      },
      {
        label: "Checksum table",
        icon: "check",
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(`CHECKSUM TABLE ${qTable} EXTENDED`, `Checksum ${table}`);
        },
      },
      { divider: true as const },
      {
        label: "Add column",
        icon: "plus",
        disabled: runner?.blocked,
        onClick: () => {
          setTableAlter({ mode: "add", schema, table });
        },
      },
      {
        label: "Remove column",
        icon: "minus",
        disabled: runner?.blocked,
        onClick: () => {
          setTableAlter({ mode: "remove", schema, table });
        },
      },
      {
        label: "View indexes",
        icon: "index",
        onClick: () => {
          if (bound) onOpenIndexes(schema, table, bound.key);
        },
      },
      { divider: true as const },
      {
        label: `Copy table name (${qTable})`,
        icon: "copy",
        onClick: () => void copyToClipboard(qTable),
      },
      {
        label: "Copy SELECT query",
        icon: "copy",
        onClick: () => void copyToClipboard(`SELECT * FROM ${qTable} LIMIT 100;`),
      },
      {
        label: "Copy SELECT COUNT(*)",
        icon: "copy",
        onClick: () => void copyToClipboard(`SELECT COUNT(*) AS total_rows FROM ${qTable};`),
      },
      { divider: true as const },
      {
        label: "Truncate table…",
        icon: "clean",
        danger: true,
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(
            `SET SESSION lock_wait_timeout = 5;\nTRUNCATE TABLE ${qTable}`,
            `Truncate ${table}`,
            `Warning: All data in ${table} will be permanently deleted. This operation cannot be rolled back.`
          );
        },
      },
      {
        label: "Drop table…",
        icon: "trash",
        danger: true,
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(
            `SET SESSION lock_wait_timeout = 5;\nDROP TABLE ${qTable}`,
            `Drop ${table}`,
            `Danger: Table ${table} and all its data, indexes, and triggers will be permanently deleted.`
          );
        },
      },
    ];
  };
  const [minimumFree, setMinimumFree] = useState(0);
  const [pkStatus, setPkStatus] = useState("all");
  const [engine, setEngine] = useState("all");
  const [freeStatus, setFreeStatus] = useState("info");
  const onScreen = useViewOnScreen();
  const catalog = useSchemaCatalog(connection, onScreen);
  const engineName = report?.engine || catalog.catalog?.engine || "";
  // The backend gates the selected scope, including cached MySQL reads.
  const inScope = catalog.catalog
    ? schemas.length
      ? catalog.catalog.schemas.filter((x) => schemas.includes(x.name)).reduce((n, x) => n + x.tables, 0)
      : catalog.catalog.user_tables
    : 0;
  const heavy = catalog.catalog && inScope > catalog.catalog.all_limit ? inScope : 0;
  const gated = !!heavy || error.includes("explicit all_schemas / --all-schemas override");

  /** Read the tables; a read for other databases (a link from Indexes)
   * replaces one still running. */
  async function load(allSchemas = false) {
    if (gated && !allSchemas) return;
    const freshNumbers = fresh && engineName !== "mariadb";
    await reads.run(
      `tables:${JSON.stringify(schemas)}:${freshNumbers}${allSchemas ? ":all" : ""}`,
      `Reading tables in ${scopeLabel(schemas).toLowerCase()}…`,
      (c, signal) => api<TablesReport>("live/tables", {
        connection: c, ...(schemas.length ? { schemas } : {}), fresh: freshNumbers,
        ...(allSchemas ? { all_schemas: true } : {}),
      }, signal),
      onReport,
    );
  }
  const loadRef = useRef(load);
  loadRef.current = load;
  useEffect(() => {
    if (!loadRequest) return;
    onLoadRequestDone();
    loadRef.current();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadRequest]);

  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled && !gated, busy: !!loading || reads.running(), read: () => { void load(); } });

  const needle = filter.trim().toLowerCase();
  const visible = (r: { schema: string; table: string }) =>
    (!focus || (r.schema === focus.schema && r.table === focus.table)) &&
    (!needle || `${r.schema}.${r.table}`.toLowerCase().includes(needle));
  const missing = useMemo(
    () =>
      (report?.missing_primary_keys || []).filter(
        (m) =>
          visible(m) &&
          (pkStatus === "all" || m.status === pkStatus) &&
          (engine === "all" || (engine === "innodb") === ((m.engine || "").toLowerCase() === "innodb")),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [report, needle, focus, pkStatus, engine],
  );
  const free = useMemo(
    () =>
      (report?.fragmentation || []).filter(
        (f) =>
          visible(f) &&
          (!minimumFree || (f.status !== "shared" && f.status !== "unknown" && f.free_pct !== null && f.free_pct >= minimumFree)) &&
          (freeStatus === "all" ||
            f.status === freeStatus),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [report, needle, focus, freeStatus, minimumFree],
  );
  const inventory = (report?.tables || []).filter(visible);
  const exportContext = `${bound?.label || ""} · ${scopeLabel(report?.scope.schemas || null)} · ${report?.taken_at || ""}`;
  const resetKey = `${reads.key}:${filter}:${JSON.stringify(focus)}:${pkStatus}:${engine}:${freeStatus}:${minimumFree}`;
  const gipk = (report?.generated_primary_keys || []).filter(visible);

  const s = report?.summary;
  const stats = report?.statistics;
  const statsText = !report
    ? ""
    : report.engine === "mariadb"
      ? "read now (MariaDB has no cache)"
      : stats?.fresh
        ? "fresh from InnoDB"
        : `cached, up to ${uptime(stats?.cache_expiry_s ?? null)} old`;

  const tableCell = (r: { schema: string; table: string; partitioned: boolean }) => (
    <span className="cell-name">
      <code>
        {r.schema}.{r.table}
      </code>
      {r.partitioned && <Pill>partitioned</Pill>}
    </span>
  );
  const makeActionsCol = <T extends { schema: string; table: string }>(): Column<T> => ({
    key: "actions",
    label: "Actions",
    width: 96,
    render: (r) => (
      <TableRowActions
        schema={r.schema}
        table={r.table}
        disabled={!!other}
        onAddColumn={() => setTableAlter({ mode: "add", schema: r.schema, table: r.table })}
        onRemoveColumn={() => setTableAlter({ mode: "remove", schema: r.schema, table: r.table })}
        onIndexes={() => bound && onOpenIndexes(r.schema, r.table, bound.key)}
      />
    ),
  });
  const inventoryColumns: Column<TableInventoryRow>[] = [
    { key: "table", label: "Table", sort: r => `${r.schema}.${r.table}`, render: tableCell },
    { key: "engine", label: "Engine", sort: r => r.engine, render: r => r.engine || "Unknown" },
    { key: "rows", label: "Rows (est.)", num: true, sort: r => r.rows, render: r => <CompactNumber value={r.rows} /> },
    { key: "data", label: "Data", num: true, sort: r => r.data_bytes, render: r => bytes(r.data_bytes) },
    { key: "indexes", label: "Indexes", num: true, sort: r => r.index_bytes, render: r => bytes(r.index_bytes) },
    makeActionsCol<TableInventoryRow>(),
  ];
  const missingColumns: Column<MissingPrimaryKey>[] = [
    {
      key: "status",
      label: "Status",
      sort: (m) => STATUS_RANK[m.status] * 1e15 - (m.size_bytes || 0),
      render: (m) => <Status status={m.status} />,
    },
    { key: "table", label: "Table", sort: (m) => `${m.schema}.${m.table}`.toLowerCase(), render: tableCell },
    {
      key: "engine",
      label: "Engine",
      sort: (m) => (m.engine || "").toLowerCase(),
      render: (m) => (
        <span className="cell-name">
          {m.engine || "—"}
          {m.engine && m.engine.toLowerCase() !== "innodb" && <Pill tone="warn">not InnoDB</Pill>}
        </span>
      ),
    },
    { key: "rows", label: "Rows (est.)", num: true, width: 112, sort: (m) => m.rows, render: (m) => <CompactNumber value={m.rows} /> },
    { key: "size", label: "Size", num: true, width: 112, sort: (m) => m.size_bytes, render: (m) => bytes(m.size_bytes) },
    {
      key: "clustered",
      label: "Clustered on",
      sort: (m) => (m.clustered_on || "").toLowerCase(),
      render: (m) =>
        m.clustered_on === "hidden row ID" ? (
          <span className="cell-muted">hidden row ID</span>
        ) : m.clustered_on ? (
          <code>{m.clustered_on}</code>
        ) : (
          <span className="cell-muted">no clustered index</span>
        ),
    },
    makeActionsCol<MissingPrimaryKey>(),
  ];
  const freeColumns: Column<FreeSpaceRow>[] = [
    {
      key: "status",
      label: "Status",
      sort: (f) => STATUS_RANK[f.status] * 1e15 - (f.free_bytes || 0),
      render: (f) => <Status status={f.status} />,
    },
    { key: "table", label: "Table", sort: (f) => `${f.schema}.${f.table}`.toLowerCase(), render: tableCell },
    { key: "rows", label: "Rows (est.)", num: true, width: 112, sort: (f) => f.rows, render: (f) => <CompactNumber value={f.rows} /> },
    {
      key: "used",
      label: "Data + index",
      num: true,
      width: 128,
      sort: (f) => (f.data_bytes || 0) + (f.index_bytes || 0),
      render: (f) => bytes((f.data_bytes || 0) + (f.index_bytes || 0)),
    },
    { key: "free", label: "Free", num: true, width: 112, sort: (f) => f.free_bytes, render: (f) => bytes(f.free_bytes) },
    {
      key: "pct",
      label: "Free %",
      num: true,
      width: 96,
      sort: (f) => f.free_pct,
      render: (f) => (f.status === "shared" || f.status === "unknown" || f.free_pct === null ? "Unknown" : `${Math.round(f.free_pct)}%`),
    },
    makeActionsCol<FreeSpaceRow>(),
  ];

  let inspector: InspectorContent | null = null;
  const [type, name] = selection.key.split(/:(.*)/s);
  const tableInspectorActions = (r: { schema: string; table: string }) => (
    <div className="table-inspector-actions" style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
      <ReviewedSqlAction
        action="optimize_table"
        sql={`OPTIMIZE TABLE \`${r.schema}\`.\`${r.table}\``}
        label={`Optimize ${r.table}`}
      />
      <ReviewedSqlAction
        action="analyze_table"
        sql={`ANALYZE TABLE \`${r.schema}\`.\`${r.table}\``}
        label={`Analyze ${r.table}`}
      />
      <ReviewedSqlAction
        action="check_table"
        sql={`CHECK TABLE \`${r.schema}\`.\`${r.table}\``}
        label={`Check ${r.table}`}
      />
      <button
        type="button"
        className="button small"
        disabled={!!other}
        onClick={() => bound && onOpenIndexes(r.schema, r.table, bound.key)}
      >
        Indexes
      </button>
      <button
        type="button"
        className="button small"
        disabled={!!other}
        onClick={() => setTableAlter({ mode: "add", schema: r.schema, table: r.table })}
      >
        Add column
      </button>
      <button
        type="button"
        className="button small"
        disabled={!!other}
        onClick={() => setTableAlter({ mode: "remove", schema: r.schema, table: r.table })}
      >
        Remove column
      </button>
    </div>
  );
  if (report && selection.key === "topic:why-pk") {
    inspector = {
      key: selection.key,
      title: "Why a primary key matters",
      body: (
        <dl className="tables-why">
          {report.why.map((w) => (
            <div key={w.key}>
              <dt>{w.title}</dt>
              <dd>{w.text}</dd>
            </div>
          ))}
        </dl>
      ),
    };
  } else if (report && type === "table") {
    const t = report.tables?.find(x => `${x.schema}.${x.table}` === name);
    if (t) inspector = { key: selection.key, title: `${t.schema}.${t.table}`, actions: tableInspectorActions(t), body:
      <InspectorSection title="Table"><Fields rows={[["Engine", t.engine || "Unknown"], ["Rows (estimated)", count(t.rows)], ["Data", bytes(t.data_bytes)], ["Indexes", bytes(t.index_bytes)]]} /></InspectorSection> };
  } else if (report && type === "missing") {
    const m = report.missing_primary_keys.find((x) => `${x.schema}.${x.table}` === name);
    if (m)
      inspector = {
        key: selection.key,
        title: `${m.schema}.${m.table}`,
        meta: (
          <>
            <StatusPill tone={statusTone(m.status)} label={STATUS_LABEL[m.status]} />
            <span>{m.engine || "—"}</span>
          </>
        ),
        actions: tableInspectorActions(m),
        body: (
          <>
            <InspectorSection title="Why">
              <p>{m.why}</p>
            </InspectorSection>
            {m.unique_keys.length > 0 && (
              <InspectorSection title="Unique keys">
                <ul>
                  {m.unique_keys.map((u) => (
                    <li key={u.index}>
                      <code>{u.index}</code> ({u.columns}){u.usable ? ": usable" : u.why_not ? `: ${u.why_not}` : ""}
                    </li>
                  ))}
                </ul>
              </InspectorSection>
            )}
            <InspectorSection title="What to do">
              <Statements items={m.checks} hint="Check first (each is a read; on a large table run it on a replica or at a quiet time):" />
              <Statements items={m.fixes} hint="Then, if the check passes:" />
              <p className="hint perf-caveat">{m.caveat}</p>
            </InspectorSection>
          </>
        ),
      };
  } else if (report && type === "free") {
    const f = report.fragmentation.find((x) => `${x.schema}.${x.table}` === name);
    if (f)
      inspector = {
        key: selection.key,
        title: `${f.schema}.${f.table}`,
        meta: <StatusPill tone={statusTone(f.status)} label={STATUS_LABEL[f.status]} />,
        actions: tableInspectorActions(f),
        body: (
          <>
            <InspectorSection title="Why">
              <p>{f.why}</p>
            </InspectorSection>
            <InspectorSection title="Space">
              <Fields
                rows={[
                  ["Data", bytes(f.data_bytes)],
                  ["Indexes", bytes(f.index_bytes)],
                  ["Free", `${bytes(f.free_bytes)}${f.status === "shared" || f.status === "unknown" || f.free_pct === null ? "" : ` (${Math.round(f.free_pct)}%)`}`],
                  ["Tablespace", f.tablespace_name || f.tablespace || "—"],
                ]}
              />
            </InspectorSection>
            {f.fix_sql && (
              <InspectorSection title="What to do">
                <CopySql sql={f.fix_sql} label={`Rebuild ${f.table}`} />
                <ReviewedSqlAction action="optimize_table" sql={f.fix_sql} label={`Optimize ${f.table}`} warning={f.caveat} />
                <p className="hint">The same rebuild, refused instead of blocking writes when it cannot run online:</p>
                <CopySql sql={f.online_sql || ""} label={`Rebuild ${f.table} online`} />
                <p className="hint perf-caveat">{f.caveat}</p>
              </InspectorSection>
            )}
          </>
        ),
      };
  }

  // Why a primary key matters: a topic in the inspector, reached from the
  // view's ⓘ and from the empty inspector (not a link in the toolbar row).
  const whyButton = (
    <button
      type="button"
      className={`text-button tables-why-button${selection.key === "topic:why-pk" ? " is-selected" : ""}`}
      onClick={() => {
        closePopovers();
        selection.select("topic:why-pk", true);
      }}
    >
      Why a primary key matters
    </button>
  );
  return (
    <ReviewedSqlProvider connection={connection} disabled={disabled || !!other} onComplete={() => void load()}>
    <ReviewedSqlBridger bridgerRef={reviewedSqlRef} />
    <TableAlterRunner alter={tableAlter} connection={connection} onClose={() => setTableAlter(null)} />
    <ViewLayout
      id="tables"
      title="Tables"
      className="tables-panel"
      onBack={onBack}
      info={
        <>
          <p>
            Tables without a primary key (or a unique key InnoDB can use instead) and InnoDB tables with free space a
            rebuild could return. It reads information_schema for the chosen databases.
          </p>
          <p>Refresh only reads. Optional Optimize actions show the exact SQL and require confirmation.</p>
          {report && <p>{whyButton}</p>}
        </>
      }
      controls={
        <>
          <SchemaPicker
            id="tables-schemas"
            value={schemas}
            onChange={onSchemasChange}
            catalog={catalog.catalog}
            error={catalog.error}
            loading={catalog.loading}
            onReload={catalog.reload}
            disabled={loading}
          />
          {engineName !== "mariadb" && (
            <span className="toolbar-check tables-fresh">
              <Checkbox className="toolbar-check" checked={fresh} disabled={loading} onChange={setFresh}>
                Fresh numbers
              </Checkbox>
              <InfoPopover label="About Fresh numbers">
                <p>
                  Fresh numbers sets information_schema_stats_expiry = 0 for myflames' own connection only, not the
                  server. It asks InnoDB for every table, so it is slower with many tables.
                </p>
              </InfoPopover>
            </span>
          )}
          {report && <SearchField label="Filter tables" value={filter} onChange={setFilter} placeholder="Filter tables" />}
        </>
      }
      onRefresh={() => void load()}
      refreshDisabled={disabled || gated}
      loading={loading}
      otherConnection={other}
      inspector={inspector}
      inspectorEmpty={
        <>
          <p>Select a table to see why it is listed, the check to run first, and the fix to copy.</p>
          {report && <p>{whyButton}</p>}
        </>
      }
      status={<><ViewStatus loading={reads.loading} error={error} /><ReviewedSqlStatus /></>}
    >
      {gated && (
        <PrerequisiteGate action={
          <button type="button" className="button" disabled={disabled || loading || !!other} onClick={() => void load(true)}>
            Read all anyway
          </button>
        }>
          <span id="tables-load-hint">
            This scope exceeds the {(catalog.catalog?.all_limit ?? 2000).toLocaleString()} table limit; choose fewer databases or read all anyway.
          </span>
        </PrerequisiteGate>
      )}
      {!report && !loading && !error && (
        <EmptyState
          icon="table"
          title="No table report yet"
          action={
            <button type="button" className="button primary" disabled={disabled || gated} onClick={() => void load()}>
              Load table health
            </button>
          }
        >
          Reads all user databases by default, or the ones you choose. It only reads.
        </EmptyState>
      )}
      {report && s && (
        <>
          <SummaryLine
            tone={s.status}
            notes={report.notes}
            extra={
              focus && (
                <p className="perf-focus" role="status">
                  Showing only{" "}
                  <code>
                    {focus.schema}.{focus.table}
                  </code>
                  <button type="button" className="text-button" onClick={() => onFocusChange(null)}>
                    Show all tables
                  </button>
                </p>
              )
            }
          >
            <span className="perf-summary-line">
              {s.no_usable_key} {s.no_usable_key === 1 ? "table" : "tables"} without a usable key, {s.not_null_unique} with only a
              unique NOT NULL key · {s.free_info ?? report.fragmentation.filter(row => row.status === "info").length} tables with reusable free space
            </span>
          </SummaryLine>
          <FactStrip
            label="Table health"
            facts={[
              ["Server", engineLabel(report)],
              ["Databases", scopeLabel(report.scope.schemas)],
              ["Tables", `${count(s.tables)} (${count(s.innodb)} InnoDB)`],
              ["Statistics", statsText],
            ]}
          />
          <section className="view-section" aria-labelledby="tables-list-h">
            <h3 id="tables-list-h" className="visually-hidden">
              Table lists
            </h3>
            {(() => {
              const listFilters = (
                <>
                  <Segmented<List>
                    label="List"
                    value={currentList}
                    onChange={setList}
                    options={[
                      ...(report.tables ? [{ value: "all" as List, label: "All tables", count: inventory.length }] : []),
                      { value: "missing", label: "Missing primary keys", count: missing.length },
                      { value: "free", label: "Free space", count: free.length },
                    ]}
                  />
                  {currentList === "missing" ? (
                    <>
                      <ToolbarSelect
                        label="Status"
                        className="tables-pk-status"
                        value={pkStatus}
                        onChange={setPkStatus}
                        options={[
                          ["all", "All"],
                          ["problem", "Problem"],
                          ["watch", "Watch"],
                        ]}
                      />
                      <ToolbarSelect
                        label="Engine"
                        className="tables-engine"
                        value={engine}
                        onChange={setEngine}
                        options={[
                          ["all", "All"],
                          ["innodb", "InnoDB"],
                          ["other", "Other engines"],
                        ]}
                      />
                    </>
                  ) : currentList === "free" ? (
                    <>
                      <ToolbarSelect
                        label="Show"
                        className="tables-free-show"
                        value={freeStatus}
                        onChange={setFreeStatus}
                        options={[
                          ["info", "With reusable free space"],
                          ["all", "All InnoDB tables"],
                          ["shared", "In a shared tablespace"],
                          ["unknown", "Not judged"],
                        ]}
                      />
                      <label className="tables-min-free">Minimum reusable space % <input type="number" aria-label="Minimum reusable table space percent" min="0" max="100" step="1" value={minimumFree} onChange={e => setMinimumFree(Math.min(100, Math.max(0, Number(e.target.value) || 0)))} /></label>
                    </>
                  ) : null}
                </>
              );
              return currentList === "all" ? (
                <PaginatedReportTable id="tables.inventory" label="All tables" columns={inventoryColumns} rows={inventory}
                  filters={listFilters}
                  rowKey={r => `table:${r.schema}.${r.table}`} selection={selection} defaultSort={{key:"table",dir:"asc"}}
                  rowAttrs={r => ({"data-table": `${r.schema}.${r.table}`})} empty="No tables match these filters."
                  rowContextMenu={r => getTableRowContextMenu(r.schema, r.table)}
                  exportContext={exportContext} resetKey={resetKey} exportColumns={[
                    {label:"Database",value:r=>r.schema},{label:"Table",value:r=>r.table},{label:"Engine",value:r=>r.engine},
                    {label:"Rows (estimated)",value:r=>r.rows},{label:"Data bytes",value:r=>r.data_bytes},{label:"Index bytes",value:r=>r.index_bytes},
                  ]} />
              ) : currentList === "missing" ? (
                <>
                  <PaginatedReportTable
                    filters={listFilters}
                    exportContext={exportContext} resetKey={resetKey}
                    exportColumns={[{label:"Database",value:m=>m.schema},{label:"Table",value:m=>m.table},{label:"Status",value:m=>STATUS_LABEL[m.status] || m.status},{label:"Engine",value:m=>m.engine},{label:"Rows (estimated)",value:m=>m.rows},{label:"Size bytes",value:m=>m.size_bytes},{label:"Clustered on",value:m=>m.clustered_on}]}
                    id="tables.missing"
                    label="Tables without a primary key"
                    columns={missingColumns}
                    rows={missing}
                    rowKey={(m) => `missing:${m.schema}.${m.table}`}
                    selection={selection}
                    defaultSort={{ key: "status", dir: "asc" }}
                    rowAttrs={(m) => ({ "data-table": `${m.schema}.${m.table}` })}
                    rowContextMenu={m => getTableRowContextMenu(m.schema, m.table)}
                    empty={
                      report.missing_primary_keys.length
                        ? "No table matches these filters."
                        : `Every table in ${scopeLabel(report.scope.schemas).toLowerCase()} has a primary key.`
                    }
                  />
                  {report.more.missing_primary_keys > 0 && <p className="hint table-more">{report.more.missing_primary_keys} more not shown.</p>}
                  {gipk.length > 0 && (
                    <p className="grouped-footer tables-gipk">
                      Generated invisible primary key (my_row_id), nothing to fix:{" "}
                      {gipk.map((g, i) => (
                        <span key={`${g.schema}.${g.table}`}>
                          {i > 0 && ", "}
                          <code>
                            {g.schema}.{g.table}
                          </code>
                        </span>
                      ))}
                    </p>
                  )}
                </>
              ) : (
                <>
                  <PaginatedReportTable
                    filters={listFilters}
                    exportContext={exportContext} resetKey={resetKey}
                    exportColumns={[{label:"Database",value:f=>f.schema},{label:"Table",value:f=>f.table},{label:"Status",value:f=>STATUS_LABEL[f.status] || f.status},{label:"Rows (estimated)",value:f=>f.rows},{label:"Data bytes",value:f=>f.data_bytes},{label:"Index bytes",value:f=>f.index_bytes},{label:"Reusable bytes",value:f=>f.free_bytes},{label:"Reusable space %",value:f=>f.status === "shared" || f.status === "unknown" ? null : f.free_pct}]}
                    id="tables.free"
                    label="Free space by table"
                    columns={freeColumns}
                    rows={free}
                    rowKey={(f) => `free:${f.schema}.${f.table}`}
                    selection={selection}
                    defaultSort={{ key: "status", dir: "asc" }}
                    rowAttrs={(f) => ({ "data-table": `${f.schema}.${f.table}` })}
                    rowContextMenu={f => getTableRowContextMenu(f.schema, f.table)}
                    empty={freeStatus === "info" ? "No reusable free space reported for individual tables." : "No table matches these filters."}
                  />
                {report.more.fragmentation > 0 && <p className="hint table-more">{report.more.fragmentation} more not shown.</p>}
                <p className="grouped-footer">Rule of thumb: {report.rules.free}.</p>
                {report.shared_tablespaces.length > 0 && (
                  <GroupedList
                    id="tables-shared-h"
                    title="Shared tablespaces"
                    className="tables-shared"
                    info={<p>Free space in a shared tablespace is not per table: a rebuild moves rows but the file does not shrink.</p>}
                  >
                    {report.shared_tablespaces.map((t) => (
                      <GroupedRow
                        key={t.name || "shared"}
                        rowKey={`shared:${t.name}`}
                        label={
                          <>
                            <code>{t.name || "shared tablespace"}</code> <span className="cell-muted">({t.type})</span>
                          </>
                        }
                        detail={t.why}
                        value={bytes(t.free_bytes)}
                        secondary="free"
                      />
                    ))}
                  </GroupedList>
                )}
              </>
              );
            })()}
          </section>
        </>
      )}
    </ViewLayout>
    </ReviewedSqlProvider>
  );
}
