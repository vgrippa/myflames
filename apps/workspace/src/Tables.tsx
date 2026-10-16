// Server > Tables: tables without a primary key or a usable unique key, and
// InnoDB free space (schema-1.0 from myflames/schema_health.py). One read per
// click over the chosen databases (all user databases by default); myflames
// never runs the ALTER or OPTIMIZE statements it shows. One sortable list at a
// time; the why, the checks and the fix statements are in the inspector.
import { useEffect, useMemo, useRef, useState } from "react";
import { api, type Connection, type FreeSpaceRow, type MissingPrimaryKey, type TablesReport } from "./api";
import { CompactNumber, CopySql, SchemaPicker, bytes, count, engineLabel, useSchemaCatalog } from "./PerfShared";
import { uptime } from "./Health";
import { useConnectionReads, type Bound } from "./binding";
import {
  Checkbox,
  DataTable,
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
} from "./kit";
import "./tables.css";

export type TableFocus = { schema: string; table: string } | null;
const STATUS_RANK: Record<string, number> = { problem: 0, watch: 1, ok: 2, shared: 3, unknown: 4 };
const STATUS_LABEL: Record<string, string> = {
  problem: "Problem",
  watch: "Watch",
  ok: "OK",
  shared: "Shared tablespace",
  unknown: "Not judged",
};
function statusTone(status: string): Tone {
  return status === "problem" || status === "watch" || status === "ok" ? status : status === "shared" ? "info" : "unknown";
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

type List = "missing" | "free";

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
}) {
  const reads = useConnectionReads(connection);
  const loading = !!reads.loading;
  const error = reads.error;
  const report = bound?.report ?? null;
  const other = bound && bound.key !== reads.key ? bound.label : null;
  const [fresh, setFresh] = useState(false);
  const [filter, setFilter] = useState("");
  const [list, setList] = useState<List>("missing");
  const [pkStatus, setPkStatus] = useState("all");
  const [engine, setEngine] = useState("all");
  const [freeStatus, setFreeStatus] = useState("flagged");
  const catalog = useSchemaCatalog(connection);
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
          (freeStatus === "all" ||
            (freeStatus === "flagged" ? f.status === "watch" || f.status === "problem" : f.status === freeStatus)),
      ),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [report, needle, focus, freeStatus],
  );
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
          <Pill tone="warn">hidden row ID</Pill>
        ) : m.clustered_on ? (
          <code>{m.clustered_on}</code>
        ) : (
          <span className="cell-muted">no clustered index</span>
        ),
    },
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
      render: (f) => (f.status === "shared" ? "—" : `${Math.round(f.free_pct)}%`),
    },
  ];

  let inspector: InspectorContent | null = null;
  const [type, name] = selection.key.split(/:(.*)/s);
  const indexesButton = (r: { schema: string; table: string }) => (
    <button type="button" className="button" disabled={!!other} onClick={() => bound && onOpenIndexes(r.schema, r.table, bound.key)}>
      Indexes
    </button>
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
        actions: indexesButton(m),
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
        actions: indexesButton(f),
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
                  ["Free", `${bytes(f.free_bytes)}${f.status === "shared" ? "" : ` (${Math.round(f.free_pct)}%)`}`],
                  ["Tablespace", f.tablespace_name || f.tablespace || "—"],
                ]}
              />
            </InspectorSection>
            {f.fix_sql && (
              <InspectorSection title="What to do">
                <CopySql sql={f.fix_sql} label={`Rebuild ${f.table}`} />
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
          <p>One read when you click. Nothing is changed: the fix statements are for you to copy.</p>
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
      status={<ViewStatus loading={reads.loading} error={error} />}
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
              unique NOT NULL key · free space: {s.free_problem} {s.free_problem === 1 ? "problem" : "problems"}, {s.free_watch} to
              watch
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
            <div className="view-section-head">
              <h3 id="tables-list-h" className="visually-hidden">
                Table lists
              </h3>
              <Segmented<List>
                label="List"
                value={list}
                onChange={setList}
                options={[
                  { value: "missing", label: "Missing primary keys", count: missing.length },
                  { value: "free", label: "Free space", count: free.length },
                ]}
              />
              {list === "missing" ? (
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
              ) : (
                <ToolbarSelect
                  label="Show"
                  className="tables-free-show"
                  value={freeStatus}
                  onChange={setFreeStatus}
                  options={[
                    ["flagged", "Watch or problem"],
                    ["all", "All InnoDB tables"],
                    ["shared", "In a shared tablespace"],
                    ["unknown", "Not judged"],
                  ]}
                />
              )}
            </div>
            {list === "missing" ? (
              <>
                <DataTable
                  id="tables.missing"
                  label="Tables without a primary key"
                  columns={missingColumns}
                  rows={missing}
                  rowKey={(m) => `missing:${m.schema}.${m.table}`}
                  selection={selection}
                  defaultSort={{ key: "status", dir: "asc" }}
                  rowAttrs={(m) => ({ "data-table": `${m.schema}.${m.table}` })}
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
                <DataTable
                  id="tables.free"
                  label="Free space by table"
                  columns={freeColumns}
                  rows={free}
                  rowKey={(f) => `free:${f.schema}.${f.table}`}
                  selection={selection}
                  defaultSort={{ key: "status", dir: "asc" }}
                  rowAttrs={(f) => ({ "data-table": `${f.schema}.${f.table}` })}
                  empty={freeStatus === "flagged" ? "No table at the watch level." : "No table matches these filters."}
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
            )}
          </section>
        </>
      )}
    </ViewLayout>
  );
}
