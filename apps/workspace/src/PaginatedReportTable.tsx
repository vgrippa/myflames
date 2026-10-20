import { useEffect, useState, type ReactNode } from "react";
import { DataTable, sortRows, type SortState } from "./kit";
import { csvReport, downloadReport, htmlReport, printReport, type ExportColumn } from "./reportTableExport";
import "./report-table.css";

type Props<T> = Parameters<typeof DataTable<T>>[0] & {
  exportColumns: ExportColumn<T>[];
  exportContext: string;
  /** Filters and connection identity; changing them returns to the first page. */
  resetKey?: string;
  /** Filter controls rendered on the same toolbar row as the row count and Export. */
  filters?: ReactNode;
};
export function PaginatedReportTable<T>({ exportColumns, exportContext, resetKey = "", filters, ...props }: Props<T>) {
  const [page, setPage] = useState(0);
  const [size, setSize] = useState(50);
  const [sort, setSort] = useState<SortState | null>(props.defaultSort ?? null);
  const [message, setMessage] = useState("");
  useEffect(() => { setPage(0); setMessage(""); }, [resetKey, exportContext]);
  const sorted = sortRows(props.rows, sort, props.columns.find(c => c.key === sort?.key)?.sort);
  const pages = Math.max(1, Math.ceil(sorted.length / size));
  const current = Math.min(page, pages - 1);
  const start = current * size;
  function changeSort(next: SortState) { setSort(next); setPage(0); }
  function exportRows(format: string) {
    if (!format) return;
    const filename = (props.id || "report").replace(/[^a-z0-9.-]/gi, "_");
    const html = format === "csv" ? "" : htmlReport(props.label, exportContext, exportColumns, sorted);
    if (format === "pdf") {
      printReport(html, setMessage);
      setMessage("Choose Save as PDF in the print dialog. The report includes every filtered row.");
    } else {
      downloadReport(format === "csv" ? csvReport(exportColumns, sorted) : html,
        format === "csv" ? "text/csv;charset=utf-8" : "text/html;charset=utf-8", `${filename}.${format}`);
      setMessage(`Exported ${sorted.length.toLocaleString()} filtered rows.`);
    }
  }
  return <div className="report-table" data-report-table={props.id}>
    <div className="report-table-tools">
      {filters}
      <span className="report-table-count" role="status">{sorted.length ? `${(start + 1).toLocaleString()} to ${Math.min(start + size, sorted.length).toLocaleString()} of ${sorted.length.toLocaleString()}` : "0 rows"}</span>
      <label>Rows <select aria-label={`Rows per page for ${props.label}`} value={size} onChange={e => { setSize(Number(e.target.value)); setPage(0); }}>
        {[25, 50, 100, 200].map(n => <option key={n} value={n}>{n}</option>)}
      </select></label>
      <select aria-label={`Export ${props.label}`} value="" disabled={!sorted.length} onChange={e => exportRows(e.target.value)}>
        <option value="">Export…</option><option value="csv">CSV</option><option value="html">HTML</option><option value="pdf">PDF…</option>
      </select>
    </div>
    {pages > 1 && (
      <nav className="report-table-pages" aria-label={`Pages for ${props.label}`}>
        <button type="button" className="button" disabled={!current} onClick={() => setPage(0)}>First</button>
        <button type="button" className="button" disabled={!current} onClick={() => setPage(current - 1)}>Previous</button>
        <span>Page {current + 1} of {pages}</span>
        <button type="button" className="button" disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>Next</button>
        <button type="button" className="button" disabled={current + 1 >= pages} onClick={() => setPage(pages - 1)}>Last</button>
      </nav>
    )}
    <DataTable {...props} rows={sorted.slice(start, start + size)} sort={sort} onSortChange={changeSort} />
    {message && <p className="hint" role="status">{message}</p>}
  </div>;
}
