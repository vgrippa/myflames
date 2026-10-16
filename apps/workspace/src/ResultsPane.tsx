// The Query editor's results: one tab per result set, Messages (every
// statement of the run with its outcome, errors with a jump to their
// position), and the plan tab of the last Explain or Run analysis. Grids use
// the shared DataTable; values are drawn as text only (NULL as a token, an
// empty string as an empty cell, binary as the client's 0x hex).
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import {
  api,
  download,
  type EditorColumn,
  type EditorError,
  type EditorExport,
  type EditorResult,
  type EditorRows,
  type EditorRun,
  type EditorStatement,
} from "./api";
import { Icon } from "./components";
import { DataTable, Popover, StatusPill, closePopovers, useFocusScope, type Column, type Tone } from "./kit";

const NUMBER = /^-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?$/;
const PLAIN = /^(-?)(\d*)(?:\.(\d*))?$/;

/**
 * A key that sorts numbers written as text in numeric order without
 * rounding them (BIGINT past 2^53, DECIMAL with many digits): sign, digit
 * count, digits, fraction. Values with an exponent (FLOAT, DOUBLE) are
 * floating point already and sort as numbers (see numericSort).
 */
export function numberKey(value: string): string {
  const m = PLAIN.exec(value);
  if (!m) return value;
  const int = m[2].replace(/^0+/, "");
  const frac = (m[3] || "").replace(/0+$/, "");
  if (!int && !frac) return "P00000.";
  if (m[1] !== "-") return `P${String(int.length).padStart(5, "0")}${int}.${frac}`;
  // Larger magnitudes first: the length and every digit complemented, and a
  // terminator above every digit so 1.5 sorts after 1.55 (-1.5 > -1.55).
  const flip = (d: string) => d.replace(/\d/g, (c) => String(9 - Number(c)));
  return `N${String(99999 - int.length).padStart(5, "0")}${flip(int)}.${flip(frac)}~`;
}
/** The sort accessor for a column of numbers: exact keys, or doubles when
 * a value has an exponent. */
export function numericSort(values: (string | null)[]): (v: string | null) => string | number | null {
  const exponent = values.some((v) => v !== null && /[eE]/.test(v));
  return exponent ? (v) => (v === null ? null : Number(v)) : (v) => (v === null ? null : numberKey(v));
}
const HEX = /^0x(?:[0-9A-F]{2})*$/;

export function count(n: number) {
  return n.toLocaleString();
}
export function duration(ms: number | null | undefined) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "";
  if (ms >= 60_000) return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.round(ms)} ms`;
}
const rowsText = (n: number) => `${count(n)} ${n === 1 ? "row" : "rows"}`;

/** The rows results of a run, numbered as their tabs are ("Result 1"). */
export function rowResults(run: EditorRun | null) {
  return (run?.results || []).filter((r): r is EditorRows => r.type === "rows");
}

/** "Result 1 · 1,000 rows · 12 ms" */
export function resultLabel(r: EditorRows, n: number) {
  const parts = [`Result ${n}`, r.columns_known ? rowsText(r.row_count) : "No rows"];
  const time = duration(r.duration_ms);
  if (time) parts.push(time);
  return parts.join(" · ");
}

/** Tab-separated text with the CSV rules (NULL is an empty field). */
export function tsv(rows: (string | null)[][]) {
  const field = (v: string | null) =>
    v === null ? "" : /[\t"\r\n]/.test(v) || v === "" ? `"${v.replace(/"/g, '""')}"` : v;
  return rows.map((r) => r.map(field).join("\t")).join("\r\n");
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Fallback for pages without clipboard permission.
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  }
}

/** One value as text: NULL a token, "" an empty cell, binary hex with its size. */
export function Value({ value, hex }: { value: string | null; hex: boolean }) {
  if (value === null) return <span className="sql-null">NULL</span>;
  if (value === "")
    return (
      <span className="sql-empty" title="Empty string">
        <span className="visually-hidden">empty string</span>
      </span>
    );
  if (hex && HEX.test(value)) {
    const bytes = (value.length - 2) / 2;
    return (
      <span className="sql-hex" title={`${count(bytes)} ${bytes === 1 ? "byte" : "bytes"}, shown as hex`}>
        {value}
      </span>
    );
  }
  return <>{value}</>;
}

type GridRow = { key: string; i: number; v: (string | null)[] };
type Menu = { x: number; y: number; row: number; col: number; opener: HTMLElement } | null;

/** A result grid: row numbers, sortable loaded rows, copy from a context menu. */
function ResultGrid({ result, label, onCopied }: { result: EditorRows; label: string; onCopied: (text: string) => void }) {
  const [selected, setSelected] = useState("");
  const [menu, setMenu] = useState<Menu>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const rows = useMemo<GridRow[]>(() => result.rows.map((v, i) => ({ key: String(i), i, v })), [result.rows]);
  const numeric = useMemo(
    () =>
      result.columns.map((_, j) => {
        let seen = false;
        for (const row of result.rows) {
          const v = row[j];
          if (v === null) continue;
          if (!NUMBER.test(v)) return false;
          seen = true;
        }
        return seen;
      }),
    [result],
  );
  const sorters = useMemo(
    () =>
      result.columns.map((_, j) => {
        const key = numericSort(result.rows.map((r) => r[j] ?? null));
        return (r: GridRow) => key(r.v[j] ?? null);
      }),
    [result],
  );
  const columns = useMemo<Column<GridRow>[]>(
    () => [
      {
        key: "#",
        label: "#",
        name: "Row number",
        title: "Row number in the order the server sent them",
        num: true,
        className: "cell-rownum",
        sort: (r) => r.i,
        render: (r) => r.i + 1,
      },
      ...result.columns.map(
        (c: EditorColumn, j): Column<GridRow> => ({
          key: `c${j}`,
          label: c.name,
          name: c.name,
          title: c.hex ? `${c.name}: every value looks like binary (0x hex); a guess` : c.name,
          num: numeric[j],
          className: "cell-value",
          sort: numeric[j] ? sorters[j] : (r) => r.v[j],
          render: (r) => <Value value={r.v[j]} hex={c.hex} />,
        }),
      ),
    ],
    [result.columns, numeric, sorters],
  );
  useEffect(() => {
    if (!menu) return;
    const close = (e: Event) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenu(null);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [menu]);
  useFocusScope(menuRef, {
    open: !!menu, onClose: () => setMenu(null), menu: true,
    returnFocus: () => menu?.opener,
  });
  useEffect(() => setMenu(null), [result]);
  function cellAt(target: EventTarget | null) {
    const el = target as HTMLElement | null;
    const tr = el?.closest?.("tr[data-row-key]") as HTMLElement | null;
    const td = el?.closest?.("td[data-col]") as HTMLElement | null;
    if (!tr) return null;
    const row = Number(tr.dataset.rowKey);
    const colKey = td?.dataset.col || "c0";
    const col = colKey.startsWith("c") ? Number(colKey.slice(1)) : 0;
    return { row, col, tr };
  }
  function openMenu(e: MouseEvent<HTMLDivElement>) {
    const at = cellAt(e.target);
    if (!at) return;
    e.preventDefault();
    setSelected(String(at.row));
    setMenu({ x: e.clientX, y: e.clientY, row: at.row, col: at.col, opener: at.tr });
  }
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const at = cellAt(document.activeElement);
    if (!at) return;
    if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) {
      e.preventDefault();
      const box = at.tr.getBoundingClientRect();
      setMenu({ x: box.left + 24, y: box.bottom, row: at.row, col: 0, opener: document.activeElement as HTMLElement });
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "c" && !window.getSelection()?.toString()) {
      e.preventDefault();
      void copy(tsv([result.rows[at.row]]), "Row copied as tab-separated text.");
    }
  }
  async function copy(text: string, said: string) {
    setMenu(null);
    if (await copyText(text)) onCopied(said);
    else onCopied("The browser did not allow copying.");
  }
  const name = (col: number) => result.columns[col]?.name || `column ${col + 1}`;
  return (
    <div className="result-grid" onContextMenu={openMenu} onKeyDown={onKey}>
      <DataTable<GridRow>
        label={label}
        columns={columns}
        rows={rows}
        rowKey={(r) => r.key}
        selection={{ key: selected, select: (key) => setSelected(key) }}
        className="result-table"
        rowAction={(r) => <button type="button" className="icon-button row-more" aria-label={`More actions for row ${r.i + 1}`}
          title="More actions" aria-haspopup="menu" aria-expanded={menu?.row === r.i}
          onClick={(e) => {
            e.stopPropagation();
            const box = e.currentTarget.getBoundingClientRect();
            setSelected(r.key);
            setMenu({ x: box.left, y: box.bottom, row: r.i, col: 0, opener: e.currentTarget });
          }}><Icon name="more" /></button>}
      />
      {menu && (
        <div
          ref={menuRef}
          className="context-menu"
          role="menu"
          aria-label="Copy"
          style={{ left: Math.min(menu.x, window.innerWidth - 220), top: Math.min(menu.y, window.innerHeight - 140) }}
          tabIndex={-1}
        >
          <button type="button" role="menuitem" onClick={() => void copy(tsv([[result.rows[menu.row][menu.col] ?? null]]), `Value of ${name(menu.col)} copied.`)}>
            Copy value ({name(menu.col)})
          </button>
          <button type="button" role="menuitem" onClick={() => void copy(tsv([result.rows[menu.row]]), "Row copied as tab-separated text.")}>
            Copy row
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => void copy(tsv(result.rows.map((r) => [r[menu.col]])), `Column ${name(menu.col)} copied (${rowsText(result.rows.length)} loaded).`)}
          >
            Copy column ({name(menu.col)})
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => void copy(tsv([result.columns.map((c) => c.name), ...result.rows]), "Result copied with its header.")}
          >
            Copy all rows with header
          </button>
        </div>
      )}
    </div>
  );
}

/** Export one result: CSV, JSON, SQL INSERT, Markdown, or the clipboard (TSV). */
function ExportMenu({
  result,
  n,
  engine,
  binary,
  onBinary,
  onDone,
}: {
  result: EditorRows;
  n: number;
  engine: string | null;
  binary: number[];
  onBinary: (column: number, on: boolean) => void;
  onDone: (text: string, error?: boolean) => void;
}) {
  const [table, setTable] = useState("");
  const [schema, setSchema] = useState("");
  const [guard, setGuard] = useState(false);
  const [busy, setBusy] = useState("");
  const hexColumns = result.columns.map((c, j) => [c, j] as const).filter(([c]) => c.hex);
  async function run(format: "csv" | "json" | "sql" | "markdown" | "tsv") {
    setBusy(format);
    try {
      const reply = await api<EditorExport>("editor/export", {
        format,
        result: {
          columns: result.columns.map((c, j) => ({ name: c.name, hex: c.hex, binary: binary.includes(j), type: c.type })),
          rows: result.rows,
        },
        engine: engine || "mysql",
        ...(format === "sql" ? { table: table.trim(), schema: schema.trim() || undefined } : {}),
        options: format === "csv" ? { header: true, formula_guard: guard } : {},
      });
      if (format === "tsv") {
        onDone((await copyText(reply.content)) ? `Result ${n} copied as tab-separated text (${rowsText(result.rows.length)}).` : "The browser did not allow copying.");
      } else {
        download(reply.content, reply.filename, reply.mime);
        onDone(`Result ${n} exported as ${reply.filename}.`);
      }
      closePopovers();
    } catch (e) {
      onDone((e as Error).message, true);
    } finally {
      setBusy("");
    }
  }
  return (
    <Popover
      label={`Export result ${n}`}
      className="result-export"
      buttonClassName="button small"
      buttonTitle="Export or copy this result"
      wide
      trigger={
        <>
          <Icon name="export" size={14} />
          Export
        </>
      }
    >
      <h3 className="popover-title">Export result {n}</h3>
      <p className="hint">
        {rowsText(result.rows.length)} as loaded. NULL is an empty CSV field and JSON null; an empty string is "".
      </p>
      <div className="export-actions">
        <button type="button" className="button small" disabled={!!busy} onClick={() => void run("csv")}>
          CSV
        </button>
        <button type="button" className="button small" disabled={!!busy} onClick={() => void run("json")}>
          JSON
        </button>
        <button type="button" className="button small" disabled={!!busy} onClick={() => void run("markdown")}>
          Markdown
        </button>
        <button type="button" className="button small" disabled={!!busy} onClick={() => void run("tsv")}>
          <Icon name="copy" size={14} />
          Copy as TSV
        </button>
      </div>
      <label className="checkbox export-guard">
        <input type="checkbox" checked={guard} onChange={(e) => setGuard(e.target.checked)} />
        <span>CSV: prefix values that start with = + - @ with ' (spreadsheet formulas)</span>
      </label>
      <fieldset className="export-sql">
        <legend>SQL INSERT</legend>
        <label className="field">
          Table
          <input value={table} placeholder="Target table name" onChange={(e) => setTable(e.target.value)} />
        </label>
        <label className="field">
          Schema (optional)
          <input value={schema} onChange={(e) => setSchema(e.target.value)} />
        </label>
        <button type="button" className="button small" disabled={!!busy || !table.trim()} onClick={() => void run("sql")}>
          Download SQL
        </button>
      </fieldset>
      {hexColumns.length > 0 && (
        <fieldset className="export-binary">
          <legend>Binary columns</legend>
          <p className="hint">These look like binary (0x hex), a guess. Marked ones are written as X'..' in SQL; others stay quoted text.</p>
          {hexColumns.map(([c, j]) => (
            <label className="checkbox" key={j}>
              <input type="checkbox" checked={binary.includes(j)} onChange={(e) => onBinary(j, e.target.checked)} />
              <span>Treat {c.name} as binary</span>
            </label>
          ))}
        </fieldset>
      )}
    </Popover>
  );
}

const STATUS: Record<EditorStatement["status"], [Tone, string]> = {
  pending: ["unknown", "Waiting"],
  running: ["info", "Running"],
  ok: ["ok", "Done"],
  error: ["problem", "Error"],
  stopped: ["watch", "Stopped"],
  not_run: ["off", "Not run"],
  session_ended: ["problem", "Outcome unknown"],
};

/** One statement in Messages: its outcome, then any error with a jump to it. */
function StatementMessage({
  statement,
  results,
  base,
  stale,
  onJump,
}: {
  statement: EditorStatement;
  results: EditorResult[];
  base: number;
  stale: boolean;
  onJump: (offset: number) => void;
}) {
  const [tone, word] = STATUS[statement.status] || STATUS.pending;
  const verb = statement.verb ? statement.verb.toUpperCase() : "";
  const facts: string[] = [];
  for (const r of results) {
    if (r.type === "rows") facts.push(r.columns_known ? rowsText(r.row_count) + (r.label ? ` (${r.label})` : "") : "No rows");
    if (r.type === "ok") {
      if (r.affected_rows !== null && r.affected_rows >= 0) facts.push(`${count(r.affected_rows)} ${r.affected_rows === 1 ? "row" : "rows"} affected`);
      if (r.info) facts.push(r.info);
    }
    if (r.type !== "error" && r.warnings) facts.push(`${count(r.warnings)} ${r.warnings === 1 ? "warning" : "warnings"}`);
  }
  const time = duration(statement.duration_ms);
  if (time) facts.push(time);
  const errors = results.filter((r): r is EditorError => r.type === "error");
  const first = statement.text.trim().split("\n")[0];
  return (
    <li className={`message-row is-${statement.status}`} data-statement={statement.index}>
      <div className="message-head">
        <StatusPill tone={tone} label={word} />
        <span className="message-title">
          Statement {statement.index + 1}
          {verb ? ` · ${verb}` : ""}
        </span>
        {facts.length > 0 && <span className="message-facts">{facts.join(" · ")}</span>}
      </div>
      <code className="message-sql" title={statement.text}>
        {first.length > 160 ? first.slice(0, 160) + "…" : first}
      </code>
      {errors.map((e, k) => (
        <div className="message-error" key={k} role={k === 0 ? "note" : undefined}>
          <span>
            {e.interrupted ? "Stopped: " : `ERROR ${e.code ?? ""}${e.sqlstate ? ` (${e.sqlstate})` : ""}: `}
            {e.message}
          </span>
          {e.position && (
            <button
              type="button"
              className="text-button"
              title={stale ? "The text changed since this run, so the position may have moved" : "Put the cursor at the error"}
              onClick={() => onJump(base + e.position!.offset)}
            >
              Go to line {e.position.line}, column {e.position.column}
            </button>
          )}
        </div>
      ))}
    </li>
  );
}

export type ResultsPaneProps = {
  run: EditorRun | null;
  running: boolean;
  /** Editor offset of the run's sql[0] (positions in the run are relative to it). */
  base: number;
  /** The editor text changed since the run. */
  stale: boolean;
  /** "messages", "plan" or "result:N" (0-based among the rows results). */
  selected: string;
  onSelect: (key: string) => void;
  engine: string | null;
  plan: ReactNode | null;
  onJump: (offset: number) => void;
  onLoadMore: (result: EditorRows) => void;
  /** Columns marked binary for export, per rows result (0-based). */
  binary: Record<number, number[]>;
  onBinary: (result: number, column: number, on: boolean) => void;
  /** Results reloaded with Load more: their note. */
  reloaded: Record<number, string>;
  onNote: (text: string, error?: boolean) => void;
  /** Statements that run read only (Load more re-runs only those). */
  canLoadMore: (result: EditorRows) => boolean;
  empty: ReactNode;
};

/** The results area: result tabs, Messages, and the plan tab. */
export function ResultsPane(props: ResultsPaneProps) {
  const { run, running, selected, onSelect, plan } = props;
  // While a run is out its rows are not sent yet: Messages shows its progress.
  const rows = running ? [] : rowResults(run);
  const errors = run ? run.results.filter((r) => r.type === "error" && !r.interrupted).length : 0;
  const keys: { key: string; label: ReactNode; title: string }[] = rows.map((r, i) => ({
    key: `result:${i}`,
    label: resultLabel(r, i + 1),
    title: `Statement ${r.statement + 1}`,
  }));
  if (run) keys.push({ key: "messages", label: <>Messages{errors > 0 && <span className="tab-count is-error">{errors}</span>}</>, title: "Every statement of the run" });
  if (plan) keys.push({ key: "plan", label: "Plan", title: "The last Explain or Run analysis of this tab" });
  const active = keys.some((k) => k.key === selected) ? selected : keys[0]?.key || "";
  const list = useRef<HTMLDivElement>(null);
  function onKey(e: KeyboardEvent<HTMLButtonElement>, i: number) {
    const next =
      e.key === "ArrowRight" ? (i + 1) % keys.length : e.key === "ArrowLeft" ? (i - 1 + keys.length) % keys.length : e.key === "Home" ? 0 : e.key === "End" ? keys.length - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    onSelect(keys[next].key);
    list.current?.querySelector<HTMLElement>(`[data-key="${keys[next].key}"]`)?.focus();
  }
  if (!keys.length)
    return (
      <div className="results-pane is-empty" aria-label="Results">
        {props.empty}
      </div>
    );
  const index = active.startsWith("result:") ? Number(active.slice(7)) : -1;
  const result = index >= 0 ? rows[index] : null;
  return (
    <div className="results-pane">
      <div className="results-tabs" role="tablist" aria-label="Results" ref={list}>
        {keys.map((k, i) => (
          <button
            key={k.key}
            type="button"
            role="tab"
            id={`results-tab-${k.key.replace(":", "-")}`}
            data-key={k.key}
            aria-selected={k.key === active}
            aria-controls="results-panel"
            tabIndex={k.key === active ? 0 : -1}
            title={k.title}
            onClick={() => onSelect(k.key)}
            onKeyDown={(e) => onKey(e, i)}
          >
            {k.label}
          </button>
        ))}
        {running && (
          <span className="results-running" role="status">
            <span className="spinner" /> Running…
          </span>
        )}
      </div>
      <div
        className="results-panel"
        id="results-panel"
        role="tabpanel"
        tabIndex={0}
        aria-labelledby={`results-tab-${active.replace(":", "-")}`}
      >
        {result && <ResultView {...props} result={result} n={index + 1} />}
        {active === "messages" && run && <Messages {...props} run={run} />}
        {active === "plan" && plan}
      </div>
    </div>
  );
}

function ResultView({
  result,
  n,
  engine,
  binary,
  onBinary,
  onLoadMore,
  canLoadMore,
  reloaded,
  onNote,
  run,
}: ResultsPaneProps & { result: EditorRows; n: number }) {
  const statement = run?.statements[result.statement];
  const index = rowResults(run).indexOf(result);
  const cut = result.stopped !== null || result.truncated;
  const more = cut && canLoadMore(result) && result.limit < 100_000;
  if (!result.columns_known)
    return (
      <div className="result-empty">
        <strong>No rows</strong>
        <code title={statement?.text}>{statement?.text.trim().split("\n")[0]}</code>
      </div>
    );
  return (
    <div className="result-view">
      <div className="result-bar">
        <span className="result-meta">
          {cut ? (
            <span className="result-cut">{result.label || `first ${rowsText(result.row_count)}`}</span>
          ) : (
            rowsText(result.row_count)
          )}
          {result.warnings ? ` · ${count(result.warnings)} ${result.warnings === 1 ? "warning" : "warnings"}` : ""}
          {duration(result.duration_ms) ? ` · ${duration(result.duration_ms)}` : ""}
          <span className="result-sortnote" title="Sorting a column orders the rows loaded here, not the whole table">
            {" "}
            · sorting orders loaded rows only
          </span>
        </span>
        {reloaded[index] && <span className="result-note">{reloaded[index]}</span>}
        <span className="result-actions">
          {more && (
            <button
              type="button"
              className="button small"
              title="Runs this statement again with a larger row limit; rows may differ from this run"
              onClick={() => onLoadMore(result)}
            >
              Load more
            </button>
          )}
          <ExportMenu
            result={result}
            n={n}
            engine={engine}
            binary={binary[index] || []}
            onBinary={(column, on) => onBinary(index, column, on)}
            onDone={onNote}
          />
        </span>
      </div>
      <ResultGrid result={result} label={`Result ${n}`} onCopied={(text) => onNote(text)} />
    </div>
  );
}

function Messages({ run, base, stale, onJump, running }: ResultsPaneProps & { run: EditorRun }) {
  const c = run.counts;
  const summary = [
    `${count(c.statements)} ${c.statements === 1 ? "statement" : "statements"}`,
    c.run !== c.statements ? `${count(c.run)} run` : "",
    c.errors ? `${count(c.errors)} ${c.errors === 1 ? "error" : "errors"}` : "",
    c.affected_rows ? `${count(c.affected_rows)} rows affected` : "",
    c.warnings ? `${count(c.warnings)} ${c.warnings === 1 ? "warning" : "warnings"}` : "",
    duration(run.duration_ms),
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="messages">
      <p className="messages-summary">
        {running ? "Running: " : ""}
        {summary}
      </p>
      {run.stopped && <p className="messages-note">{run.stopped.message}</p>}
      {run.message && run.state !== "complete" && <p className="messages-note">{run.message}</p>}
      <ol className="message-list">
        {run.statements.map((s) => (
          <StatementMessage
            key={s.index}
            statement={s}
            results={run.results.filter((r) => r.statement === s.index)}
            base={base}
            stale={stale}
            onJump={onJump}
          />
        ))}
      </ol>
    </div>
  );
}
