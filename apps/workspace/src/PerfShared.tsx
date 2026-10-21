// Shared pieces of the Server views: number formatting, the schema field, the
// sampling window, the database picker, and copyable SQL. Layout components
// (tables, grouped lists, inspector) are in kit.tsx.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, formatNumber, type Connection, type SchemaCatalog } from "./api";
import { DataTable, Popover, Select, closePopovers } from "./kit";
import { connectionKey } from "./binding";
import { Icon, type IconName } from "./components";
import "./perf.css";

/** Full-width details replace the list, so opening them never hides columns.
 * Explicit column choices remain under DataTable's existing persistence rules.
 * *defaultHidden* columns start off so a wide list fits beside the list of
 * views at 1440 px: the Columns menu shows one (its width is then kept with
 * the workspace) and Reset columns turns it off again. Their values stay in
 * the inspector. */
export function PriorityTable<T>({
  detailColumns,
  defaultHidden,
  ...props
}: Parameters<typeof DataTable<T>>[0] & { detailColumns: string[]; defaultHidden?: string[] }) {
  if (defaultHidden?.length) return <DataTable {...props} detailColumns={defaultHidden} compact />;
  return <DataTable {...props} detailColumns={detailColumns} compact={false} />;
}

/** Milliseconds as "1.23 s", "45.6 ms", "120 µs", "80 ns". */
export function ms(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(2)} s`;
  if (value >= 1) return `${value.toFixed(1)} ms`;
  if (value >= 0.001) return `${Math.round(value * 1000)} µs`;
  if (value > 0) return `${Math.round(value * 1e6)} ns`;
  return "0";
}

/** Bytes as "1.5 MiB". */
export function bytes(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const units: [number, string][] = [
    [2 ** 40, "TiB"],
    [2 ** 30, "GiB"],
    [2 ** 20, "MiB"],
    [2 ** 10, "KiB"],
  ];
  for (const [limit, unit] of units)
    if (Math.abs(value) >= limit) return `${(value / limit).toFixed(1)} ${unit}`;
  return `${Math.round(value)} B`;
}

/** Counts with grouping; small fractions keep two digits. */
export function count(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const digits = Number.isInteger(value) || Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  return formatNumber(value, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Large counts, compact: "159.0B", "1.78B", "14.2M", "927.2M" (three or
 * four significant digits). Below a million the exact value is shown. */
export function compact(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const units: [number, string][] = [
    [1e6, "M"],
    [1e9, "B"],
    [1e12, "T"],
  ];
  const abs = Math.abs(value);
  if (abs < 1e6) return count(value);
  let i = units.length - 1;
  while (i > 0 && abs < units[i][0]) i--;
  let v = value / units[i][0];
  // 999,999,999 is "1.00B", not "1000.0M".
  if (Math.abs(+v.toFixed(1)) >= 1000 && i < units.length - 1) v = value / units[++i][0];
  return `${v.toFixed(Math.abs(+v.toFixed(2)) < 10 ? 2 : 1)}${units[i][1]}`;
}

/** A large count in a table cell: compact text, the exact value with
 * separators as its tooltip and accessible name. Sort on the raw number. */
export function CompactNumber({ value }: { value: number | null | undefined }) {
  const tiny = value != null && Number.isFinite(value) && value !== 0 && Math.abs(value) < 0.01;
  const text = tiny ? formatNumber(value, { maximumSignificantDigits: 3 }) : compact(value);
  const exact = value != null && Number.isFinite(value)
    ? formatNumber(value, { maximumFractionDigits: 20 }) : "—";
  if (text === exact) return <>{text}</>;
  return (
    <abbr className="compact-number" title={exact} aria-label={exact}>
      {text}
    </abbr>
  );
}

export function percent(share: number | null | undefined) {
  if (share === null || share === undefined || !Number.isFinite(share)) return "—";
  return `${Math.round(share * 100)}%`;
}

/** "MySQL 8.4.11", "MariaDB 11.8.9-MariaDB-log", "Percona Server 8.4.11-11": the
 * report's server_product when it names one (Percona keeps engine "mysql"). */
export function engineLabel(report: { engine: string | null; server_version: string | null; server_product?: string | null }) {
  return `${report.server_product || (report.engine === "mariadb" ? "MariaDB" : "MySQL")} ${report.server_version || "?"}`;
}

/** SQL the user may run themselves, with a Copy button. myflames never runs it. */
export function CopySql({ sql, label }: { sql: string; label: string }) {
  const [copied, setCopied] = useState("");
  return (
    <div className="perf-sql">
      <pre tabIndex={0} aria-label={label}>
        <code>{sql}</code>
      </pre>
      <button
        type="button"
        className="text-button"
        aria-label={`Copy: ${label}`}
        onClick={() => {
          navigator.clipboard
            .writeText(sql)
            .then(() => setCopied("Copied. Run it yourself in a SQL client if you decide to."))
            .catch(() => setCopied("Select the text to copy it."));
        }}
      >
        Copy
      </button>
      {copied && (
        <span className="hint" role="status">
          {copied}
        </span>
      )}
    </div>
  );
}

export function SchemaField({
  value,
  onChange,
  disabled,
  id,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  id: string;
}) {
  return (
    <label className="toolbar-field perf-schema" htmlFor={id}>
      <span>Schema</span>
      <input
        id={id}
        value={value}
        maxLength={64}
        spellCheck={false}
        autoCapitalize="off"
        autoCorrect="off"
        placeholder="All schemas"
        disabled={disabled}
        onChange={(e) => onChange(e.target.value.replace(/[\n\r\0]/g, ""))}
      />
    </label>
  );
}

export function WindowSelect({
  value,
  options,
  onChange,
  disabled,
}: {
  value: number;
  options: number[];
  onChange: (seconds: number) => void;
  disabled?: boolean;
}) {
  return (
    <label className="toolbar-field perf-window">
      <span>Window</span>
      <Select value={value} disabled={disabled} onChange={(v) => onChange(+v)} options={options.map((v): [number, string] => [v, `${v} s`])} />
    </label>
  );
}

// ---- Database picker (Indexes and Tables) -----------------------------------

/** One list per connection while the workspace is open (one cheap read:
 * schema names and table counts, perf_common.schema_catalog). */
const catalogs = new Map<string, Promise<SchemaCatalog>>();

/** The database list for *connection*: loaded when *enabled*, cached. */
export function useSchemaCatalog(connection: Connection, enabled = true) {
  // The same identity as every Server report (binding.ts): saved connection
  // id, host, port, user and TLS settings including the CA file.
  const key = connectionKey(connection);
  const [state, setState] = useState<{ key: string; catalog: SchemaCatalog | null; error: string; loading: boolean }>(
    { key: "", catalog: null, error: "", loading: false },
  );
  const connectionRef = useRef(connection);
  connectionRef.current = connection;
  const load = useCallback(
    (refresh = false) => {
      if (refresh || !catalogs.has(key)) {
        const request = api<SchemaCatalog>("live/schemas", { connection: connectionRef.current });
        catalogs.set(key, request);
        // A failed read is forgotten, unless a newer one already replaced it.
        request.catch(() => {
          if (catalogs.get(key) === request) catalogs.delete(key);
        });
      }
      const request = catalogs.get(key)!;
      // Only the latest read of the connection on screen may answer: an older
      // read of the same connection (before Reload list) is dropped.
      // (A failed read removes itself from the cache first.)
      const latest = () => !catalogs.has(key) || catalogs.get(key) === request;
      setState((s) => ({ key, catalog: s.key === key ? s.catalog : null, error: "", loading: true }));
      request.then(
        (catalog) => setState((s) => (s.key === key && latest() ? { key, catalog, error: "", loading: false } : s)),
        (e) => setState((s) => (s.key === key && latest() ? { key, catalog: null, error: (e as Error).message, loading: false } : s)),
      );
    },
    [key],
  );
  useEffect(() => {
    if (enabled && state.key !== key) load();
  }, [enabled, key, load, state.key]);
  const current = state.key === key ? state : { catalog: null, error: "", loading: enabled };
  return { catalog: current.catalog, error: current.error, loading: current.loading, reload: () => load(true) };
}

function schemaSummary(value: string[]) {
  if (!value.length) return "All user databases";
  if (value.length <= 2) return value.join(", ");
  return `${value.length} databases`;
}

/** Multi-select of databases with search. An empty selection means every
 * database except mysql, sys, performance_schema and information_schema.
 * The floating kit popover keeps the toolbar height stable. */
export function SchemaPicker({
  id,
  value,
  onChange,
  catalog,
  error,
  loading,
  onReload,
  disabled,
}: {
  id: string;
  value: string[];
  onChange: (value: string[]) => void;
  catalog: SchemaCatalog | null;
  error: string;
  loading: boolean;
  onReload: () => void;
  disabled?: boolean;
}) {
  const control = useRef<HTMLFieldSetElement>(null);
  const [showSystem, setShowSystem] = useState(false);
  return (
    <div className="toolbar-field perf-picker">
      <span aria-hidden="true">Databases</span>
      <fieldset ref={control} className="perf-picker-control" disabled={disabled}>
        <legend className="visually-hidden">Databases</legend>
        <Popover
          label="Choose databases"
          className="perf-picker-popover"
          buttonClassName="perf-picker-button"
          buttonTitle="Choose databases"
          trigger={
            <>
              <span className="visually-hidden">Databases: </span>
              <span id={id} className="perf-picker-summary">{schemaSummary(value)}</span>
            </>
          }
        >
          <SchemaPickerPanel
            id={id}
            value={value}
            onChange={onChange}
            catalog={catalog}
            error={error}
            loading={loading}
            onReload={onReload}
            showSystem={showSystem}
            onShowSystem={setShowSystem}
            onDone={() => {
              closePopovers();
              control.current?.querySelector("button")?.focus();
            }}
          />
        </Popover>
      </fieldset>
    </div>
  );
}

/** Mounted only while open: focus search once and forget its text on dismissal.
 * The chosen databases and system visibility remain owned by the picker. */
function SchemaPickerPanel({
  id, value, onChange, catalog, error, loading, onReload, showSystem, onShowSystem, onDone,
}: Omit<Parameters<typeof SchemaPicker>[0], "disabled"> & {
  showSystem: boolean;
  onShowSystem: (value: boolean) => void;
  onDone: () => void;
}) {
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);
  const hintId = useId();
  // The popover is placed (and made visible) in a layout effect that renders
  // again; focus after that frame, a hidden field cannot take focus.
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => searchRef.current?.focus({ preventScroll: true }));
    return () => window.cancelAnimationFrame(frame);
  }, []);
  const schemas = catalog?.schemas || [];
  const needle = query.trim().toLowerCase();
  const matches = (name: string) => !needle || name.toLowerCase().includes(needle);
  const user = schemas.filter((s) => !s.system && matches(s.name));
  const system = schemas.filter((s) => s.system && matches(s.name));
  // Chosen names the list does not have (typed, or from a restored workspace).
  const extra = value.filter((v) => !schemas.some((s) => s.name === v) && matches(v));
  const typed = query.trim().slice(0, 64);
  const canAdd = !!typed && !/[\n\r\0]/.test(typed) && !schemas.some((s) => s.name === typed) && !value.includes(typed);
  const toggle = (name: string) =>
    onChange(value.includes(name) ? value.filter((v) => v !== name) : [...value, name]);
  const tables = (n: number) => `${n.toLocaleString()} ${n === 1 ? "table" : "tables"}`;
  const row = (name: string, count: number | null) => (
    <li key={name}>
      <label className="perf-picker-option">
        <input type="checkbox" checked={value.includes(name)} onChange={() => toggle(name)} />
        <code>{name}</code>
        {count !== null && <span className="hint">{tables(count)}</span>}
      </label>
    </li>
  );
  return (
    <div id={`${id}-panel`} className="perf-picker-panel" role="group" aria-label="Databases">
      <div className="perf-picker-head">
        <input
          ref={searchRef}
          type="search"
          className="perf-picker-search"
          placeholder="Search databases"
          aria-label="Search databases"
          aria-describedby={hintId}
          value={query}
          maxLength={64}
          spellCheck={false}
          autoCapitalize="off"
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && canAdd) {
              e.preventDefault();
              onChange([...value, typed]);
              setQuery("");
            }
          }}
        />
        <label className="perf-picker-option perf-picker-all">
          <input type="checkbox" checked={!value.length} onChange={() => onChange([])} />
          All user databases
          {catalog && <span className="hint">{tables(catalog.user_tables)}</span>}
        </label>
      </div>
      <p id={hintId} className="hint perf-picker-hint">
        {loading
          ? "Listing databases…"
          : error
            ? `Could not list databases (${error}). Type a name and press Enter to use it.`
            : "Tick databases to read only those. All user databases leaves out mysql, sys, performance_schema and information_schema."}
      </p>
      <ul className="perf-picker-list" aria-label="Databases">
        {extra.map((name) => row(name, null))}
        {user.map((s) => row(s.name, s.tables))}
        {showSystem && system.map((s) => row(s.name, s.tables))}
      </ul>
      {!user.length && !extra.length && !loading && catalog && (
        <p className="hint">{needle ? `No database matches “${query.trim()}”.` : "No user databases."}</p>
      )}
      <div className="perf-picker-foot">
        {canAdd && (
          <button
            type="button"
            className="text-button"
            onClick={() => {
              onChange([...value, typed]);
              setQuery("");
            }}
          >
            Use “{typed}”
          </button>
        )}
        {!!schemas.some((s) => s.system) && (
          <label className="perf-toggle">
            <input type="checkbox" checked={showSystem} onChange={(e) => onShowSystem(e.target.checked)} />
            Show system databases
          </label>
        )}
        <button type="button" className="text-button" disabled={loading} onClick={onReload}>
          Reload list
        </button>
        <button type="button" className="button" onClick={onDone}>
          Done
        </button>
      </div>
    </div>
  );
}

export type RowMenuItem =
  | { label: string; icon?: IconName; onSelect: () => void; disabled?: boolean; danger?: boolean; title?: string }
  | "divider";

/** A row's ⋯ menu in the trailing column of a DataTable (its rowAction), the
 * pattern of Processlist: shown on the hovered, focused or selected row. An
 * item runs after the menu has closed and handed focus back to ⋯, so a
 * review dialog it opens returns focus there. */
export function RowActionMenu({ label, items, disabled = false }: { label: string; items: RowMenuItem[]; disabled?: boolean }) {
  return (
    <span className="row-menu-anchor" onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
      <Popover menu label={label} buttonTitle={label} buttonClassName="icon-button row-more" disabled={disabled} trigger={<Icon name="more" />}>
        <div className="row-action-menu">
          {items.map((item, i) =>
            item === "divider" ? (
              <div key={`divider-${i}`} className="row-action-divider" role="separator" />
            ) : (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                className={item.danger ? "is-danger" : undefined}
                disabled={item.disabled}
                title={item.title}
                onClick={() => {
                  closePopovers();
                  window.setTimeout(item.onSelect, 0);
                }}
              >
                {item.icon && <Icon name={item.icon} size={14} />}
                {item.label}
              </button>
            ),
          )}
        </div>
      </Popover>
    </span>
  );
}
