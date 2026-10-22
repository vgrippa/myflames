// Maintenance > Dump: the tables of the one chosen database, read when it is
// chosen (catalog-1.0 with stats: engine, estimated rows and size, 200 per
// page), in a searchable multi-select whose default is All tables. Pages
// belong to one connection and database; an answer for another is dropped.
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api, type Connection } from "./api";
import { connectionKey } from "./binding";
import { Popover, closePopovers } from "./kit";
import { bytes, compact } from "./PerfShared";
import type { SchemaTable, SchemaTables } from "./opsApi";

/** done: a first page (or an error) arrived for *key*. */
type Loaded = { key: string; tables: SchemaTable[]; truncated: boolean; next: string | null; loading: boolean; error: string; done: boolean };
const EMPTY: Loaded = { key: "", tables: [], truncated: false, next: null, loading: false, error: "", done: false };

/** The tables of *schema* on *connection*, read the first time it is chosen
 * while *enabled* (Dump on screen). The pages loaded stay while the view is
 * hidden; a read stopped by hiding it starts again when it is shown; Reload
 * list reads again. Only the latest read for the current key may answer. */
export function useSchemaTables(connection: Connection, schema: string, enabled: boolean) {
  const key = schema ? `${connectionKey(connection)}\n${schema}` : "";
  const [state, setState] = useState<Loaded>(EMPTY);
  const latest = useRef(key);
  latest.current = key;
  const connectionRef = useRef(connection);
  connectionRef.current = connection;
  const controller = useRef<AbortController | null>(null);
  const stop = () => {
    if (!controller.current) return;
    controller.current.abort();
    controller.current = null;
    setState(s => s.loading ? { ...s, loading: false } : s);
  };
  const read = useCallback((after: string | null) => {
    if (!key) return;
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setState(s => ({ ...(s.key === key ? s : { ...EMPTY, key }), loading: true, error: "" }));
    const settle = (next: (s: Loaded) => Loaded) => {
      if (abort.signal.aborted || latest.current !== key) return;
      controller.current = null;
      setState(s => next(s.key === key ? s : { ...EMPTY, key }));
    };
    api<SchemaTables>("live/catalog", { connection: connectionRef.current, schema, stats: true, after }, abort.signal)
      .then(page => settle(s => {
        const before = after ? s.tables : [];
        const seen = new Set(before.map(t => t.name));
        return { key, tables: [...before, ...page.tables.filter(t => !seen.has(t.name))], truncated: page.truncated,
          next: page.next_after, loading: false, error: "", done: true };
      }))
      .catch(e => settle(s => ({ ...s, loading: false, error: (e as Error).message, done: true })));
  }, [key, schema]);
  const done = state.key === key && state.done;
  useEffect(() => {
    if (enabled && key && !done && !controller.current) read(null);
  }, [enabled, key, done, read]);
  // Hiding the view or choosing another database stops a read in flight.
  useEffect(() => { if (!enabled) stop(); }, [enabled]);
  useEffect(() => () => stop(), [key]);
  const current = state.key === key ? state : { ...EMPTY, key, loading: !!key && enabled };
  return { ...current, more: () => { if (current.next && !current.loading) read(current.next); }, reload: () => read(null) };
}

export function tableSummary(value: string[], total: number | null) {
  if (!value.length) return total === null ? "All tables" : `All tables (${total.toLocaleString()})`;
  if (value.length <= 2) return value.join(", ");
  return `${value.length.toLocaleString()} tables`;
}

/** "InnoDB · ≈1.2M rows · 24.0 MiB", "≈1 row", "view". TABLE_ROWS is an estimate. */
export function tableFacts(table: SchemaTable) {
  if (table.type !== "table") return table.type;
  const rows = table.rows != null ? `≈${compact(table.rows)} ${table.rows === 1 ? "row" : "rows"}` : "";
  return [table.engine, rows, table.bytes != null ? bytes(table.bytes) : ""].filter(Boolean).join(" · ");
}

/** Multi-select of the database's tables. An empty selection is All tables. */
export function TablePicker({ id, schema, value, onChange, tables, disabled }: {
  id: string; schema: string; value: string[]; onChange: (value: string[]) => void;
  tables: ReturnType<typeof useSchemaTables>; disabled?: boolean;
}) {
  const control = useRef<HTMLFieldSetElement>(null);
  const total = tables.loading && !tables.tables.length ? null : tables.truncated ? null : tables.tables.length;
  return <div className="dump-table-picker">
    <fieldset ref={control} className="perf-picker-control" disabled={disabled}>
      <legend className="visually-hidden">Tables of {schema}</legend>
      <Popover label={`Choose tables of ${schema}`} className="perf-picker-popover dump-table-popover" buttonClassName="perf-picker-button"
        buttonTitle="Choose tables" wide
        trigger={<><span className="visually-hidden">Tables: </span><span id={id} className="perf-picker-summary">{tableSummary(value, total)}</span></>}>
        <TablePanel schema={schema} value={value} onChange={onChange} tables={tables}
          onDone={() => { closePopovers(); control.current?.querySelector("button")?.focus(); }} />
      </Popover>
    </fieldset>
  </div>;
}

function TablePanel({ schema, value, onChange, tables, onDone }: {
  schema: string; value: string[]; onChange: (value: string[]) => void;
  tables: ReturnType<typeof useSchemaTables>; onDone: () => void;
}) {
  const [query, setQuery] = useState("");
  const search = useRef<HTMLInputElement>(null);
  const hintId = useId();
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => search.current?.focus({ preventScroll: true }));
    return () => window.cancelAnimationFrame(frame);
  }, []);
  const needle = query.trim().toLowerCase();
  const rows = tables.tables.filter(t => !needle || t.name.toLowerCase().includes(needle));
  const toggle = (name: string) => onChange(value.includes(name) ? value.filter(v => v !== name) : [...value, name]);
  const size = tables.tables.reduce((sum, t) => sum + (t.bytes || 0), 0);
  return <div className="perf-picker-panel dump-table-panel" role="group" aria-label={`Tables of ${schema}`}>
    <div className="perf-picker-head">
      <input ref={search} type="search" className="perf-picker-search" placeholder={`Search tables of ${schema}`} aria-label="Search tables"
        aria-describedby={hintId} value={query} maxLength={64} spellCheck={false} autoCapitalize="off" onChange={e => setQuery(e.target.value)} />
      <label className="perf-picker-option perf-picker-all">
        <input type="checkbox" checked={!value.length} onChange={() => onChange([])} />
        All tables
        {!tables.loading && tables.tables.length > 0 && <span className="hint">{tables.truncated ? `${tables.tables.length.toLocaleString()}+` : tables.tables.length.toLocaleString()} · {bytes(size)}</span>}
      </label>
    </div>
    <p id={hintId} className="hint perf-picker-hint" role={tables.error ? "alert" : undefined}>
      {tables.loading && !tables.tables.length ? `Reading the tables of ${schema}…`
        : tables.error ? `Could not read the tables of ${schema}: ${tables.error}`
          : tables.truncated ? `The first ${tables.tables.length.toLocaleString()} tables, by name. Load more to search the rest.`
            : "Tick tables to dump only those. Rows and sizes are the server's estimates."}
    </p>
    <ul className="perf-picker-list dump-table-list" aria-label={`Tables of ${schema}`}>
      {rows.map(t => <li key={t.name}>
        <label className="perf-picker-option dump-table-option" data-table={t.name}>
          <input type="checkbox" checked={value.includes(t.name)} onChange={() => toggle(t.name)} />
          <code>{t.name}</code>
          <span className="hint">{tableFacts(t)}</span>
        </label>
      </li>)}
    </ul>
    {!rows.length && !tables.loading && !tables.error && <p className="hint">{needle ? `No table matches “${query.trim()}”.` : `${schema} has no tables.`}</p>}
    <div className="perf-picker-foot">
      {tables.truncated && <button type="button" className="text-button" disabled={tables.loading} onClick={tables.more}>{tables.loading ? "Loading…" : "Load more tables"}</button>}
      <button type="button" className="text-button" disabled={tables.loading} onClick={tables.reload}>Reload list</button>
      <button type="button" className="button" onClick={onDone}>Done</button>
    </div>
  </div>;
}
