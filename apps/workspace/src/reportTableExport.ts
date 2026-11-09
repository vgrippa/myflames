/** Text-only exports. Names and values are untrusted server metadata. */
export type ExportColumn<T> = { label: string; value: (row: T) => string | number | null | undefined };
export function csvCell(value: unknown): string {
  let text = value == null ? "" : String(value);
  // Spreadsheet programs can evaluate formulas even inside quoted CSV fields.
  if (/^[\s\u0000-\u001f]*[=+@-]/.test(text) || /^[\t\r\n]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g, '""') + '"';
}
export function csvReport<T>(columns: ExportColumn<T>[], rows: T[]): string {
  return "\uFEFF" + [columns.map(c => csvCell(c.label)).join(","), ...rows.map(r => columns.map(c => csvCell(c.value(r))).join(","))].join("\r\n") + "\r\n";
}
export function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
}
export function htmlReport<T>(title: string, context: string, columns: ExportColumn<T>[], rows: T[]): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>${escapeHtml(title)}</title><style>
body{font:13px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#202124;background:#fff;margin:24px}h1{font-size:20px}p{color:#444}table{border-collapse:collapse;width:100%;font-size:11px;table-layout:fixed}th,td{padding:6px 8px;border:1px solid #aaa;text-align:left;vertical-align:top;overflow-wrap:anywhere;white-space:pre-wrap}th{background:#f3f4f5}thead{display:table-header-group}tr{break-inside:avoid}@page{margin:12mm}@media print{body{margin:0}}
</style></head><body><h1>${escapeHtml(title)}</h1><p>${escapeHtml(context)} · ${rows.length.toLocaleString()} rows</p><table><thead><tr>${columns.map(c => `<th>${escapeHtml(c.label)}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${columns.map(c => `<td>${escapeHtml(c.value(r))}</td>`).join("")}</tr>`).join("")}</tbody></table><footer><p>Inspired by <a href="https://github.com/brendangregg/FlameGraph">Brendan Gregg's FlameGraph</a> and <a href="https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/">Tanel Poder's SQL Plan FlameGraphs</a>.</p></footer></body></html>`;
}
export function downloadReport(content: string, mime: string, filename: string) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const a = document.createElement("a"); a.href = url; a.download = filename;
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
/** Native/browser print preserves Unicode, repeats headers and paginates every
 * row. The system dialog supplies Save as PDF without privileged renderer IPC. */
export function printReport(content: string, onError: (error: string) => void = () => {}) {
  document.querySelectorAll("iframe[data-report-print]").forEach(frame => frame.remove());
  const frame = document.createElement("iframe");
  frame.title = "Printable report";
  frame.dataset.reportPrint = "true";
  frame.style.cssText = "position:fixed;left:-10000px;top:0;width:1200px;height:800px;border:0";
  frame.onload = () => {
    const win = frame.contentWindow;
    if (!win) { frame.remove(); return; }
    win.addEventListener("afterprint", () => frame.remove(), { once: true });
    try { win.focus(); win.print(); }
    catch { frame.remove(); onError("The print dialog could not open. Export HTML and print it from your browser."); }
  };
  frame.srcdoc = content;
  document.body.append(frame);
}
