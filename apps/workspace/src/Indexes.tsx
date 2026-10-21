import { bt, dropIndexSql, indexVisibilitySql, showIndexSql, tableMaintenanceSql } from "./sqlIdent";
// Server > Indexes: unused and redundant indexes and tables read mostly by
// full scans (indexes-1.0 from myflames/indexes.py). One read per click: the
// Performance Schema tables behind it cost time on servers with many tables,
// so there is no automatic refresh. All user databases is the default only
// up to 2,000 tables (perf_common.ALL_SCHEMAS_TABLE_LIMIT); above that the
// user chooses databases in the picker, and the server enforces the same.
// One list at a time (segmented control); the why and the statements to copy
// are in the inspector.
import { useEffect, useMemo, useRef, useState } from "react";
import { useOpenRead, useViewOnScreen } from "./viewOnScreen";
import {
  api,
  type Connection,
  type FullScanTable,
  type IndexesReport,
  type IndexInventoryRow,
  type RedundantIndex,
  type TopStatement,
  type UnusedIndex,
} from "./api";
import { StatementsBehind, type DigestAction, type LabSettings, type OpenTop } from "./SlowQueries";
import { uptime } from "./Health";
import { useConnectionReads, type Bound } from "./binding";
import { CompactNumber, CopySql, RowActionMenu, SchemaPicker, count, engineLabel, ms, percent, useSchemaCatalog, type RowMenuItem } from "./PerfShared";
import { InstrumentsLink } from "./Instruments";
import type { TableFocus } from "./Tables";
import {
  SearchField,
  InfoPopover,
  EmptyState,
  FactStrip,
  Fields,
  InspectorSection,
  Pill,
  PrerequisiteGate,
  Segmented,
  StatusPill,
  SummaryLine,
  ViewLayout,
  ViewStatus,
  type Column,
  type InspectorContent,
  type Selection,
  copyToClipboard,
} from "./kit";

import { PaginatedReportTable } from "./PaginatedReportTable";
import { ReviewedSqlAction, ReviewedSqlBridger, ReviewedSqlProvider, ReviewedSqlStatus, useReviewedSql } from "./ReviewedSqlAction";

const SOURCES: Record<string, string> = {
  performance_schema: "Performance Schema",
  userstat: "User statistics (userstat)",
};
/** "Keep: backs a foreign key", "Keep: read per user statistics", "Candidate". */
function verdictLabel(u: UnusedIndex, short = false) {
  if (u.verdict !== "keep") return "Candidate";
  if (short) return "Keep";
  return u.keep_reason === "userstat" ? "Keep: read per user statistics" : "Keep: backs a foreign key";
}
const NO_DATA: Record<string, string> = {
  not_opened: "not opened since the counts started",
  no_activity: "no rows read or written",
  not_instrumented: "not instrumented (setup_objects)",
  not_read: "no reads since userstat started",
  partitioned: "partitioned, and Performance Schema does not count every read through a partitioned table's indexes",
  partitioning_unknown: "partitioning could not be read, so index use cannot be judged",
  userstat_unknown: "partitioned tables could not be listed, and Percona Server's user statistics do not count them",
};
type List = "all" | "unused" | "redundant" | "scans" | "nodata";
type NoData = IndexesReport["no_data"][number];

function parseVersion(v: string | null | undefined): { major: number; minor: number; patch: number } {
  if (!v) return { major: 0, minor: 0, patch: 0 };
  const m = v.match(/^(\d+)(?:\.(\d+))?(?:\.(\d+))?/);
  if (!m) return { major: 0, minor: 0, patch: 0 };
  return { major: Number(m[1] || 0), minor: Number(m[2] || 0), patch: Number(m[3] || 0) };
}

function supportsInvisibleIndexes(engine: string | undefined, versionStr: string | null | undefined): boolean {
  const isMaria = (engine || "").toLowerCase() === "mariadb";
  const { major, minor } = parseVersion(versionStr);
  if (isMaria) {
    return major > 10 || (major === 10 && minor >= 6);
  }
  return major >= 8;
}

function IndexRowActions({
  schema,
  table,
  index,
  visible,
  engine,
  serverVersion,
  actionBlockedReason,
  onTableHealth,
  disabled,
}: {
  schema: string;
  table: string;
  index: string;
  visible?: boolean;
  engine: string;
  serverVersion: string | null | undefined;
  actionBlockedReason?: string;
  onTableHealth?: () => void;
  disabled: boolean;
}) {
  const reviewedSql = useReviewedSql();
  const isMaria = (engine || "").toLowerCase() === "mariadb";
  const canInvisible = supportsInvisibleIndexes(engine, serverVersion);
  const isPrimary = index.toUpperCase() === "PRIMARY";

  if (actionBlockedReason) {
    return onTableHealth ? <RowActionMenu label={`Actions for ${schema}.${table}.${index}`} disabled={disabled}
      items={[{ label: "Table health", icon: "statements", onSelect: onTableHealth }]} /> : null;
  }

  const isCurrentlyVisible = visible !== false;
  const toggleVisibilitySql = indexVisibilitySql(schema, table, index, engine, isCurrentlyVisible);
  const toggleVisibilityLabel = isMaria
    ? (isCurrentlyVisible ? "Set ignored" : "Set not ignored")
    : (isCurrentlyVisible ? "Set invisible" : "Set visible");

  const dropSql = dropIndexSql(schema, table, index);

  const items: RowMenuItem[] = [
    ...(!isPrimary && canInvisible ? [{
      label: `${toggleVisibilityLabel}…`, icon: "view" as const, disabled: reviewedSql?.blocked,
      onSelect: () => reviewedSql?.start(toggleVisibilitySql, `${toggleVisibilityLabel} (${index})`, isCurrentlyVisible
        ? "The query optimizer will ignore this index, but writes will still maintain it. Use this to safely test index removal."
        : "The query optimizer will now consider this index in query plans."),
    }] : []),
    {
      label: isPrimary ? "Drop primary key…" : "Drop index…", icon: "trash", danger: true, disabled: reviewedSql?.blocked,
      onSelect: () => reviewedSql?.start(dropSql, isPrimary ? "Drop primary key" : `Drop index ${index}`),
    },
    ...(onTableHealth ? ["divider" as const, { label: "Table health", icon: "statements" as const, onSelect: onTableHealth }] : []),
  ];
  return <RowActionMenu label={`Actions for ${schema}.${table}.${index}`} disabled={disabled} items={items} />;
}

function IndexInspectorActions({
  schema,
  table,
  index,
  visible,
  engine,
  serverVersion,
  actionBlockedReason,
  onTableHealth,
  disabled,
}: {
  schema: string;
  table: string;
  index: string;
  visible?: boolean;
  engine: string;
  serverVersion: string | null | undefined;
  actionBlockedReason?: string;
  onTableHealth?: () => void;
  disabled: boolean;
}) {
  const isMaria = (engine || "").toLowerCase() === "mariadb";
  const canInvisible = supportsInvisibleIndexes(engine, serverVersion);
  const isPrimary = index.toUpperCase() === "PRIMARY";

  if (actionBlockedReason) {
    return onTableHealth ? <button type="button" className="button small" disabled={disabled} onClick={onTableHealth}>
      Table health
    </button> : null;
  }

  const isCurrentlyVisible = visible !== false;
  const toggleVisibilitySql = indexVisibilitySql(schema, table, index, engine, isCurrentlyVisible);
  const toggleVisibilityLabel = isMaria
    ? (isCurrentlyVisible ? "Set ignored" : "Set not ignored")
    : (isCurrentlyVisible ? "Set invisible" : "Set visible");

  const dropSql = dropIndexSql(schema, table, index);

  return (
    <div className="index-inspector-actions" style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
      {onTableHealth && (
        <button type="button" className="button small" disabled={disabled} onClick={onTableHealth}>
          Table health
        </button>
      )}
      {!isPrimary && canInvisible && (
        <ReviewedSqlAction
          action="alter_table"
          sql={toggleVisibilitySql}
          label={toggleVisibilityLabel}
          disabled={disabled}
          warning={isCurrentlyVisible ? "The query optimizer will ignore this index for query execution, but writes will still maintain it." : "The query optimizer will consider this index for query plans."}
        />
      )}
      <ReviewedSqlAction
        action="alter_table"
        sql={dropSql}
        label="Drop index"
        disabled={disabled}
        warning={isPrimary ? "Dropping a PRIMARY KEY will fail if an AUTO_INCREMENT column is part of it." : `Dropping index ${index} will permanently remove it from ${table}.`}
      />
    </div>
  );
}

function FixStatements({ item }: { item: UnusedIndex | RedundantIndex }) {
  if (item.action_blocked_reason || (!item.test_sql && !item.drop_sql && !item.caveat)) return null;
  return (
    <InspectorSection title="What to do">
      {item.test_sql && (
        <>
          <p className="hint">Test first: the optimizer stops using it, writes still maintain it.</p>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap", marginBlock: "var(--space-1)" }}>
            <ReviewedSqlAction action="alter_table" sql={item.test_sql} label={`Set invisible (${item.index})`} warning={item.caveat} />
            <CopySql sql={item.test_sql} label={`Test without ${item.index}`} />
          </div>
        </>
      )}
      {item.drop_sql && (
        <>
          <p className="hint">Then, when nothing slowed down:</p>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center", flexWrap: "wrap", marginBlock: "var(--space-1)" }}>
            <ReviewedSqlAction action="alter_table" sql={item.drop_sql} label={`Drop ${item.index}`} warning={item.caveat} />
            <CopySql sql={item.drop_sql} label={`Drop ${item.index}`} />
          </div>
        </>
      )}
      {item.caveat && <p className="hint perf-caveat">{item.caveat}</p>}
    </InspectorSection>
  );
}

export function IndexesView({
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
  onOpenTables,
  settings,
  onDigest,
  onOpenTop,
  onOpenSql,
}: {
  connection: Connection;
  /** Query editor settings for Run analysis on a statement that names a table. */
  settings: LabSettings;
  onDigest: (row: TopStatement, rank: number, action: DigestAction, expect: string) => void;
  /** Top statements filtered to one table (order no_index); *from* is the report's connection key. */
  onOpenTop: OpenTop;
  disabled: boolean;
  onBack?: () => void;
  selection: Selection;
  /** Chosen databases; empty means all user databases. */
  schemas: string[];
  onSchemasChange: (schemas: string[]) => void;
  report: Bound<IndexesReport> | null;
  onReport: (report: Bound<IndexesReport>) => void;
  /** Set when another view links here: load once, then clear it. */
  loadRequest: number;
  onLoadRequestDone: () => void;
  /** A table another view linked to: only its rows are shown until cleared. */
  focus: TableFocus;
  onFocusChange: (focus: TableFocus) => void;
  /** Links that read with this report's table; *from* is the report's connection key. */
  onOpenTables: (schema: string, table: string, from: string) => void;
  /** Put a statement in the Query editor's current tab, without running it. */
  onOpenSql?: (sql: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [list, setList] = useState<List>("all");
  const reviewedSqlRef = useRef<ReturnType<typeof useReviewedSql>>(null);

  const getIndexRowContextMenu = (schema: string, table: string, index: string, visible?: boolean, actionBlockedReason?: string) => {
    const runner = reviewedSqlRef.current;
    const isMaria = (report?.engine || "").toLowerCase() === "mariadb";
    const canInvisible = supportsInvisibleIndexes(report?.engine || "", report?.server_version);
    const isPrimary = index.toUpperCase() === "PRIMARY";

    const isCurrentlyVisible = visible !== false;
    const toggleVisibilitySql = indexVisibilitySql(schema, table, index, report?.engine || "", isCurrentlyVisible);
    const toggleVisibilityLabel = isMaria
      ? (isCurrentlyVisible ? "Set ignored" : "Set not ignored")
      : (isCurrentlyVisible ? "Set invisible" : "Set visible");

    const dropSql = dropIndexSql(schema, table, index);

    return [
      ...(!actionBlockedReason && !isPrimary && canInvisible
        ? [
            {
              label: `${toggleVisibilityLabel}…`,
              icon: "view",
              disabled: runner?.blocked,
              onClick: () => {
                runner?.start(
                  toggleVisibilitySql,
                  `${toggleVisibilityLabel} (${index})`,
                  isCurrentlyVisible
                    ? "The query optimizer will ignore this index, but writes will still maintain it. Use this to safely test index removal."
                    : "The query optimizer will now consider this index in query plans."
                );
              },
            },
          ]
        : []),
      {
        label: "Update index statistics (Analyze)…",
        icon: "statements",
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(tableMaintenanceSql("ANALYZE", schema, table), `Analyze statistics for ${table}`);
        },
      },
      {
        label: "Rebuild index / table (Optimize)…",
        icon: "optimize",
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(tableMaintenanceSql("OPTIMIZE", schema, table), `Optimize ${table} (rebuild index)`);
        },
      },
      { divider: true as const },
      {
        label: "Show full scan queries on this table",
        icon: "search",
        onClick: () => {
          if (bound) onOpenTop("no_index", { schema, table }, bound.key);
        },
      },
      {
        label: "View table health",
        icon: "statements",
        onClick: () => {
          if (bound) onOpenTables(schema, table, bound.key);
        },
      },
      { divider: true as const },
      ...(onOpenSql
        ? [
            {
              label: "Show index definition (SHOW INDEX)",
              icon: "index",
              onClick: () => {
                onOpenSql(showIndexSql(schema, table, index));
              },
            },
          ]
        : []),
      {
        label: `Copy index name (\`${index}\`)`,
        icon: "copy",
        onClick: () => void copyToClipboard(bt(index)),
      },
      ...(!actionBlockedReason ? [{
        label: isPrimary ? "Copy DROP PRIMARY KEY SQL" : "Copy DROP INDEX SQL",
        icon: "copy",
        onClick: () => void copyToClipboard(dropSql + ";"),
      },
      { divider: true as const },
      {
        label: isPrimary ? "Drop primary key…" : "Drop index…",
        icon: "trash",
        danger: true,
        disabled: runner?.blocked,
        onClick: () => {
          runner?.start(
            dropSql,
            isPrimary ? "Drop primary key" : `Drop index ${index}`,
            isPrimary
              ? "Dropping a PRIMARY KEY will fail if an AUTO_INCREMENT column is part of it, and will force a clustered index rebuild in InnoDB."
              : `Dropping index ${index} will permanently remove it from ${table}.`
          );
        },
      }] : []),
    ];
  };
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const onScreen = useViewOnScreen();
  const catalog = useSchemaCatalog(connection, onScreen);
  const limit = catalog.catalog?.all_limit ?? 2000;
  const scopeTables = catalog.catalog ? (schemas.length ? catalog.catalog.schemas.filter(s => schemas.includes(s.name)).reduce((n, s) => n + s.tables, 0) : catalog.catalog.user_tables) : 0;
  const tooMany = scopeTables > limit || error.includes("explicit all_schemas / --all-schemas override");
  const canLoad = !tooMany;

  /** Read index usage; a read for other databases (a link from Tables or
   * I/O) replaces one still running. */
  async function load(allSchemas = false) {
    if (!canLoad && !allSchemas) return;
    await reads.run(
      `indexes:${JSON.stringify(schemas)}${allSchemas ? ":all" : ""}`,
      `Reading index usage for ${schemas.length ? schemas.join(", ") : "all user databases"}…`,
      (c, signal) => api<IndexesReport>("live/indexes", { connection: c, ...(schemas.length ? { schemas } : {}), ...(allSchemas ? { all_schemas: true } : {}) }, signal),
      onReport,
    );
  }
  const loadRef = useRef(load);
  loadRef.current = load;
  // A link from another view asks for one read; the request is then cleared.
  useEffect(() => {
    if (!loadRequest) return;
    onLoadRequestDone();
    loadRef.current();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadRequest]);

  useOpenRead({ key: reads.key, reportKey: bound?.key, ready: !disabled && canLoad, busy: !!loading || reads.running(), read: () => { void load(); } });

  const s = report?.summary;
  const chosen = report ? report.scope.schemas || (report.scope.schema ? [report.scope.schema] : null) : null;
  const scope = !report ? "" : !chosen ? "all user databases" : chosen.length <= 3 ? chosen.join(", ") : `${chosen.length} databases`;
  // Filtered once per report, focus and filter, not on every render (a
  // selection in a list of 5,000 indexes).
  const { inventory, unused, redundant, scans, noData } = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    const shown = (r: { schema: string; table: string; index?: string }) => (!focus || (r.schema === focus.schema && r.table === focus.table)) && `${r.schema}.${r.table}.${r.index || ""}`.toLowerCase().includes(needle);
    return {
      inventory: (report?.indexes || []).filter(shown),
      unused: report ? report.unused.filter(shown) : [],
      redundant: report ? report.redundant.filter(shown) : [],
      scans: report ? report.full_scans.filter(shown) : [],
      noData: report ? report.no_data.filter(shown) : [],
    };
  }, [report, focus, filter]);
  const exportContext = `${bound?.label || ""} · ${scope} · ${report?.taken_at || ""}`;
  const resetKey = `${reads.key}:${filter}:${JSON.stringify(focus)}`;
  const available = !!report?.available;
  const requested = list === "all" && !report?.indexes ? "unused" : list;
  const current: List = !available && (requested === "unused" || requested === "scans") ? "redundant" : requested;

  const tableName = (r: { schema: string; table: string }) => (
    <code title={`${r.schema}.${r.table}`}>
      {r.schema}.{r.table}
    </code>
  );
  // Each row's ⋯ menu in the table's trailing column (rowAction), as in Processlist.
  const indexRowAction = (r: { schema: string; table: string; index: string; visible?: boolean; action_blocked_reason?: string }) => (
    <IndexRowActions
      schema={r.schema}
      table={r.table}
      index={r.index}
      visible={r.visible}
      actionBlockedReason={r.action_blocked_reason}
      engine={report?.engine || ""}
      serverVersion={report?.server_version}
      disabled={!!other}
      onTableHealth={() => bound && onOpenTables(r.schema, r.table, bound.key)}
    />
  );
  const inventoryColumns: Column<IndexInventoryRow>[] = [
    {key:"table",label:"Table",sort:r=>`${r.schema}.${r.table}`,render:tableName},
    {key:"index",label:"Index",sort:r=>r.index,render:r=><code>{r.index}</code>},
    {key:"columns",label:"Columns",sort:r=>r.columns,render:r=>r.columns},
    {key:"type",label:"Type",sort:r=>r.type,render:r=>r.type},
    {key:"unique",label:"Unique",sort:r=>Number(r.unique),render:r=>r.unique?"Yes":"No"},
    {key:"visible",label:"Optimizer",sort:r=>Number(r.visible),render:r=>r.visible?"Visible":report?.engine==="mariadb"?"Ignored":"Invisible"},
  ];
  const unusedColumns: Column<UnusedIndex>[] = [
    {
      key: "verdict",
      label: "Verdict",
      sort: (u) => (u.verdict === "keep" ? 1 : 0),
      render: (u) => (
        <span className={`perf-verdict is-${u.verdict}`}>
          <StatusPill tone={u.verdict === "keep" ? "ok" : "watch"} label={verdictLabel(u)} />
        </span>
      ),
    },
    { key: "table", label: "Table", sort: (u) => `${u.schema}.${u.table}`, render: tableName },
    {
      key: "index",
      label: "Index",
      sort: (u) => u.index,
      render: (u) => (
        <span className="cell-name">
          <code>{u.index}</code> <span className="cell-muted">({u.columns})</span>
          {!u.visible && <Pill>{report?.engine === "mariadb" ? "already ignored" : "already invisible"}</Pill>}
        </span>
      ),
    },
  ];
  const redundantColumns: Column<RedundantIndex>[] = [
    { key: "table", label: "Table", sort: (r) => `${r.schema}.${r.table}`, render: tableName },
    {
      key: "index",
      label: "Index",
      sort: (r) => r.index,
      render: (r) => (
        <>
          <code>{r.index}</code> <span className="cell-muted">({r.columns})</span>
        </>
      ),
    },
    {
      key: "covered",
      label: "Covered by",
      sort: (r) => r.dominant,
      render: (r) => (
        <>
          <span className="cell-muted">{r.reason === "duplicates" ? "duplicates" : "prefix of"}</span> <code>{r.dominant}</code>{" "}
          <span className="cell-muted">({r.dominant_columns})</span>
        </>
      ),
    },
  ];
  const scanColumns: Column<FullScanTable>[] = [
    {
      key: "table",
      label: "Table",
      primary: true,
      flex: true,
      minWidth: 240,
      sort: (f) => `${f.schema}.${f.table}`,
      render: (f) => (
        <span className="cell-name">
          {tableName(f)}
          {f.mostly && <Pill tone="warn">Mostly full scans</Pill>}
        </span>
      ),
    },
    { key: "rows", label: "Rows without index", num: true, sort: (f) => f.rows_without_index, render: (f) => <CompactNumber value={f.rows_without_index} /> },
    { key: "with", label: "Through indexes", num: true, sort: (f) => f.rows_with_index, render: (f) => <CompactNumber value={f.rows_with_index} /> },
    { key: "time", label: "Read time without index", num: true, sort: (f) => f.fetch_ms_without_index, render: (f) => ms(f.fetch_ms_without_index) },
    {
      key: "share",
      label: "Share",
      num: true,
      sort: (f) => f.share_time ?? f.share_rows,
      render: (f) => `${percent(f.share_time ?? f.share_rows)} of ${f.share_time !== null ? "read time" : "rows"}`,
    },
  ];
  const noDataColumns: Column<NoData>[] = [
    { key: "table", label: "Table", sort: (n) => `${n.schema}.${n.table}`, render: tableName },
    { key: "indexes", label: "Indexes", num: true, sort: (n) => n.indexes, render: (n) => count(n.indexes) },
    { key: "reason", label: "Why there is no data", sort: (n) => n.reason, render: (n) => n.reason_text || NO_DATA[n.reason] || n.reason },
  ];

  let inspector: InspectorContent | null = null;
  const [type, name] = selection.key.split(/:(.*)/s);
  const tableHealth = (r: { schema: string; table: string }) => (
    <button type="button" className="button" disabled={!!other} onClick={() => bound && onOpenTables(r.schema, r.table, bound.key)}>
      Table health
    </button>
  );
  if (report && type === "index") {
    const row = report.indexes?.find(r => `${r.schema}.${r.table}.${r.index}` === name);
    if (row)
      inspector = {
        key: selection.key,
        title: row.index,
        actions: (
          <IndexInspectorActions
            schema={row.schema}
            table={row.table}
            index={row.index}
            visible={row.visible}
            actionBlockedReason={row.action_blocked_reason}
            engine={report.engine || ""}
            serverVersion={report.server_version}
            disabled={!!other}
            onTableHealth={() => bound && onOpenTables(row.schema, row.table, bound.key)}
          />
        ),
        body: (
          <InspectorSection title="Index">
            {row.action_blocked_reason && <p>{row.action_blocked_reason}</p>}
            <Fields
              rows={[
                ["Table", `${row.schema}.${row.table}`],
                ["Columns", row.columns],
                ["Type", row.type],
                ["Unique", row.unique ? "Yes" : "No"],
                ["Optimizer", row.visible ? "Visible" : report.engine === "mariadb" ? "Ignored" : "Invisible"],
              ]}
            />
          </InspectorSection>
        ),
      };
  } else if (report && type === "unused") {
    const u = report.unused.find((x) => `${x.schema}.${x.table}.${x.index}` === name);
    if (u)
      inspector = {
        key: selection.key,
        title: u.index,
        meta: (
          <>
            <StatusPill tone={u.verdict === "keep" ? "ok" : "watch"} label={verdictLabel(u, true)} />
            <code>
              {u.schema}.{u.table}
            </code>
          </>
        ),
        actions: (
          <IndexInspectorActions
            schema={u.schema}
            table={u.table}
            index={u.index}
            visible={u.visible}
            actionBlockedReason={u.action_blocked_reason}
            engine={report.engine || ""}
            serverVersion={report.server_version}
            disabled={!!other}
            onTableHealth={() => bound && onOpenTables(u.schema, u.table, bound.key)}
          />
        ),
        body: (
          <>
            <InspectorSection title="Why">
              <p>{u.why}</p>
            </InspectorSection>
            <InspectorSection title="Index">
              <Fields
                rows={[
                  ["Columns", u.columns],
                  ["Visible", u.visible ? "Yes" : report.engine === "mariadb" ? "No (ignored)" : "No (invisible)"],
                  u.foreign_keys.length > 0 && ["Foreign keys", u.foreign_keys.join(", ")],
                  u.userstat_rows_read != null && ["Rows read (user statistics)", count(u.userstat_rows_read)],
                ]}
              />
            </InspectorSection>
            <FixStatements item={u} />
          </>
        ),
      };
  } else if (report && type === "redundant") {
    const r = report.redundant.find((x) => `${x.schema}.${x.table}.${x.index}` === name);
    if (r)
      inspector = {
        key: selection.key,
        title: r.index,
        meta: (
          <code>
            {r.schema}.{r.table}
          </code>
        ),
        actions: (
          <IndexInspectorActions
            schema={r.schema}
            table={r.table}
            index={r.index}
            visible={r.visible}
            actionBlockedReason={r.action_blocked_reason}
            engine={report.engine || ""}
            serverVersion={report.server_version}
            disabled={!!other}
            onTableHealth={() => bound && onOpenTables(r.schema, r.table, bound.key)}
          />
        ),
        body: (
          <>
            <InspectorSection title="Why">
              <p>{r.why}</p>
              {r.action_blocked_reason && <p>{r.action_blocked_reason}</p>}
            </InspectorSection>
            <InspectorSection title="Covered by">
              <Fields
                rows={[
                  ["This index", `${r.index} (${r.columns})`],
                  [r.reason === "duplicates" ? "Duplicates" : "Prefix of", `${r.dominant} (${r.dominant_columns})`],
                  r.foreign_keys.length > 0 && ["Foreign keys", r.foreign_keys.join(", ")],
                ]}
              />
            </InspectorSection>
            <FixStatements item={r} />
          </>
        ),
      };
  } else if (report && type === "scan") {
    const f = report.full_scans.find((x) => `${x.schema}.${x.table}` === name);
    if (f)
      inspector = {
        key: selection.key,
        title: `${f.schema}.${f.table}`,
        meta: f.mostly ? <Pill tone="warn">Mostly full scans</Pill> : undefined,
        actions: tableHealth(f),
        body: (
          <>
            <InspectorSection title="Why">
              <p>{f.why}</p>
            </InspectorSection>
            <InspectorSection title="Rows read">
              <Fields
                rows={[
                  ["Without an index", count(f.rows_without_index)],
                  ["Through indexes", count(f.rows_with_index)],
                  ["Read time without", ms(f.fetch_ms_without_index)],
                  ["Read time through indexes", ms(f.fetch_ms_with_index)],
                  ["Share", `${percent(f.share_time ?? f.share_rows)} of ${f.share_time !== null ? "read time" : "rows"}`],
                ]}
              />
              <p className="hint">Rows read by table scans and row-position reads. A scan of a small lookup table is normal: look at the read time.</p>
            </InspectorSection>
            <InspectorSection
              title="Statements that name it"
              info={
                <>
                  <p>
                    Recorded statements that read some table without an index and whose digest text names this table,
                    heaviest first. A text match, not proof: a view, a routine or a trigger can read it without naming
                    it, and a statement can match because it scanned another table it reads.
                  </p>
                  <p>Their counters run since the server started or the digest table was truncated.</p>
                </>
              }
            >
              <StatementsBehind
                key={`${f.schema}.${f.table}|${reads.key}`}
                connection={connection}
                disabled={disabled}
                other={!!other}
                from={bound?.key || ""}
                order="no_index"
                table={{ schema: f.schema, table: f.table }}
                settings={settings}
                onDigest={onDigest}
                onOpenTop={onOpenTop}
              />
            </InspectorSection>
          </>
        ),
      };
  } else if (report && type === "nodata") {
    const n = report.no_data.find((x) => `${x.schema}.${x.table}` === name);
    if (n)
      inspector = {
        key: selection.key,
        title: `${n.schema}.${n.table}`,
        actions: tableHealth(n),
        body: (
          <InspectorSection title="No data">
            <p>
              {n.indexes} {n.indexes === 1 ? "index" : "indexes"}, {n.reason_text || NO_DATA[n.reason] || n.reason}. Never listed as unused:
              there is nothing to judge.
            </p>
          </InspectorSection>
        ),
      };
  }

  const loadHint = tooMany
    ? `This scope has ${scopeTables.toLocaleString()} tables in user databases, more than ${limit.toLocaleString()}: choose databases. ${catalog.catalog?.all_note || ""}`
    : "";
  return (
    <ReviewedSqlProvider connection={connection} disabled={disabled || !!other} onComplete={() => void load()}>
      <ReviewedSqlBridger bridgerRef={reviewedSqlRef} />
      <ViewLayout
        id="indexes"
        title="Indexes"
        className="indexes-panel"
        onBack={onBack}
        info={
          <>
            <p>
              Indexes nothing has used since the server started, indexes another index already covers, and tables read
              mostly without an index.
            </p>
            <p>One read when you click; usage counts start at server start. Above {limit.toLocaleString()} tables, choose databases first. It only reads.</p>
          </>
        }
        controls={
          <SchemaPicker
            id="indexes-schemas"
            value={schemas}
            onChange={onSchemasChange}
            catalog={catalog.catalog}
            error={catalog.error}
            loading={catalog.loading}
            onReload={catalog.reload}
            disabled={loading}
          />
        }
        onRefresh={() => void load()}
        refreshDisabled={disabled || !canLoad}
        loading={loading}
        otherConnection={other}
        inspector={inspector}
        inspectorEmpty={<p>Select an index or a table to see why it is listed and the statements to copy.</p>}
        status={
          <>
            <ViewStatus loading={reads.loading} error={error} />
            <ReviewedSqlStatus />
          </>
        }
      >
      {tooMany && (
        <PrerequisiteGate>
          <span id="indexes-load-hint">{loadHint}</span>
          <button type="button" className="button" disabled={disabled || loading} onClick={() => void load(true)}>Read all anyway</button>
        </PrerequisiteGate>
      )}
      {!report && !loading && !error && (
        <EmptyState
          icon="index"
          title="No index report yet"
          action={
            <button
              type="button"
              className="button primary"
              disabled={disabled || !canLoad}
              aria-describedby={tooMany ? "indexes-load-hint" : undefined}
              onClick={() => void load()}
            >
              Load index health
            </button>
          }
        >
          Reads all user databases by default, or the ones you choose. It only reads.
        </EmptyState>
      )}
      {report && s && (
        <>
          <SummaryLine
            notes={report.available ? report.notes : report.notes.slice(1)}
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
              {[
                ...(report.available ? [`${count(s.unused_candidates)} unused`, `${count(s.keep_foreign_key)} for foreign keys`] : []),
                `${count(s.redundant)} redundant`,
                report.available ? `${count(s.mostly_full_scans)} mostly full scans` : "full scans not available",
                ...(s.no_data ? [`${count(s.no_data)} without data`] : []),
              ].join(" · ")}
            </span>
          </SummaryLine>
          <FactStrip
            label="Index health"
            facts={[
              ["Server", engineLabel(report)],
              ["Uptime", uptime(report.uptime_s)],
              ["Databases", scope],
              ["Usage from", report.source
                ? `${SOURCES[report.source]}${report.userstat?.role === "evidence" ? " and user statistics" : ""}`
                : "not available"],
              ["Tables", count(s.tables)],
              ["Indexes", count(s.indexes)],
            ]}
          />
          <section className="view-section" aria-labelledby="indexes-list-h">
            <h3 id="indexes-list-h" className="visually-hidden">
              Index lists
            </h3>
            {!available && (list === "unused" || list === "scans") && (
              <PrerequisiteGate action={<InstrumentsLink row={report.performance_schema ? "group:wait/io/table" : "ps"} />}>
                {report.notes[0] || "Index usage is not available on this server."}
              </PrerequisiteGate>
            )}
            {(() => {
              const listFilters = (
                <>
                  <Segmented<List>
                    label="List"
                    value={current}
                    onChange={setList}
                    options={[
                      ...(report.indexes ? [{value:"all" as List,label:"All indexes",count:inventory.length}] : []),
                      { value: "unused", label: "Unused", count: available ? unused.length : null },
                      { value: "redundant", label: "Redundant", count: redundant.length },
                      { value: "scans", label: "Full scans", count: available ? scans.length : null },
                      ...(noData.length ? [{ value: "nodata" as List, label: "No data", count: noData.length }] : []),
                    ]}
                  />
                  <SearchField value={filter} onChange={setFilter} label="Filter indexes" placeholder="Filter tables or indexes" />
                  <InfoPopover label="About index fragmentation"><p>Index fragmentation percentages are not available in this report. InnoDB DATA_FREE measures reusable tablespace allocation, not fragmentation of an individual index. Use Tables → Free space to filter that table-level metric.</p></InfoPopover>
                </>
              );
              return (
                <>
                  {current === "all" && (
                    <PaginatedReportTable id="indexes.inventory" label="All indexes" columns={inventoryColumns} rows={inventory}
                      rowAction={indexRowAction}
                      filters={listFilters}
                      rowKey={r=>`index:${r.schema}.${r.table}.${r.index}`} selection={selection} defaultSort={{key:"table",dir:"asc"}}
                      rowAttrs={r=>({"data-index":`${r.table}.${r.index}`})} exportContext={exportContext} resetKey={resetKey}
                      rowContextMenu={r => getIndexRowContextMenu(r.schema, r.table, r.index, r.visible, r.action_blocked_reason)}
                      exportColumns={[{label:"Database",value:r=>r.schema},{label:"Table",value:r=>r.table},{label:"Index",value:r=>r.index},{label:"Columns",value:r=>r.columns},{label:"Type",value:r=>r.type},{label:"Unique",value:r=>r.unique?"Yes":"No"},{label:"Optimizer",value:r=>r.visible?"Visible":report.engine==="mariadb"?"Ignored":"Invisible"}]}
                      empty="No indexes match these filters." />
                  )}
                  {current === "unused" && (
                    <PaginatedReportTable
                      filters={listFilters}
                      exportContext={exportContext} resetKey={resetKey}
                      exportColumns={[{label:"Database",value:u=>u.schema},{label:"Table",value:u=>u.table},{label:"Index",value:u=>u.index},{label:"Columns",value:u=>u.columns},{label:"Verdict",value:u=>u.verdict}]}
                      id="indexes.unused"
                      label="Unused indexes"
                      columns={unusedColumns}
                      rows={unused}
                      rowKey={(u) => `unused:${u.schema}.${u.table}.${u.index}`}
                      rowAction={indexRowAction}
                      selection={selection}
                      rowAttrs={(u) => ({ "data-index": `${u.table}.${u.index}` })}
                      rowContextMenu={u => getIndexRowContextMenu(u.schema, u.table, u.index, u.visible, u.action_blocked_reason)}
                      empty={`No unused indexes in ${scope}.`}
                    />
                  )}
                  {current === "redundant" && (
                    <PaginatedReportTable
                      filters={listFilters}
                      exportContext={exportContext} resetKey={resetKey}
                      exportColumns={[{label:"Database",value:r=>r.schema},{label:"Table",value:r=>r.table},{label:"Index",value:r=>r.index},{label:"Columns",value:r=>r.columns},{label:"Covered by",value:r=>r.dominant},{label:"Covering columns",value:r=>r.dominant_columns},{label:"Reason",value:r=>r.reason}]}
                      id="indexes.redundant"
                      label="Redundant indexes"
                      columns={redundantColumns}
                      rows={redundant}
                      rowKey={(r) => `redundant:${r.schema}.${r.table}.${r.index}`}
                      rowAction={indexRowAction}
                      selection={selection}
                      rowAttrs={(r) => ({ "data-index": `${r.table}.${r.index}` })}
                      rowContextMenu={r => getIndexRowContextMenu(r.schema, r.table, r.index, r.visible, r.action_blocked_reason)}
                      empty={`No redundant indexes in ${scope}.`}
                    />
                  )}
                  {current === "scans" && (
                    <PaginatedReportTable
                      filters={listFilters}
                      exportContext={exportContext} resetKey={resetKey}
                      exportColumns={[{label:"Database",value:f=>f.schema},{label:"Table",value:f=>f.table},{label:"Rows without index",value:f=>f.rows_without_index},{label:"Rows through indexes",value:f=>f.rows_with_index},{label:"Read time without index (ms)",value:f=>f.fetch_ms_without_index},{label:"Read time share %",value:f=>f.share_time},{label:"Row share %",value:f=>f.share_rows}]}
                      id="indexes.scans"
                      label="Tables read without an index"
                      columns={scanColumns}
                      rows={scans}
                      rowKey={(f) => `scan:${f.schema}.${f.table}`}
                      selection={selection}
                      rowAttrs={(f) => ({ "data-table": f.table })}
                      empty={`No rows were read without an index in ${scope}.`}
                    />
                  )}
                  {current === "nodata" && (
                    <PaginatedReportTable
                      filters={listFilters}
                      exportContext={exportContext} resetKey={resetKey}
                      exportColumns={[{label:"Database",value:n=>n.schema},{label:"Table",value:n=>n.table},{label:"Reason",value:n=>n.reason}]}
                      id="indexes.nodata"
                      label="Tables with no data"
                      columns={noDataColumns}
                      rows={noData}
                      rowKey={(n) => `nodata:${n.schema}.${n.table}`}
                      selection={selection}
                      rowAttrs={(n) => ({ "data-table": n.table })}
                    />
                  )}
                </>
              );
            })()}
            {(() => {
              // Every list is cut at the server's limit: say how many are not shown, and how to see them.
              const more = { all: report.more.indexes, unused: report.more.unused, redundant: report.more.redundant, scans: report.more.full_scans, nodata: report.more.no_data }[current] || 0;
              return more > 0 ? (
                <p className="hint table-more" data-more={current}>
                  {count(more)} more not shown. Choose fewer databases to see them.
                </p>
              ) : null;
            })()}
          </section>
        </>
      )}
    </ViewLayout>
    </ReviewedSqlProvider>
  );
}
